"""生产下载入口、全文续读权限和持久缓存接线。"""
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pymupdf
import pytest

from runtime.production import read_scopes_for_role
from runtime.tools import ToolContext, ToolContractError, ToolExecutionError
from test_explicit_asset_commands import SKILL, services_for


def _services(root):
    services = services_for(root)
    services._ensure_external_allowed = lambda _context: None
    services.settings = SimpleNamespace(web_extract_timeout_s=10, web_extract_max_bytes=100000)
    return services


def test_production_returns_structured_excerpt_and_research_can_continue_reading(tmp_path, monkeypatch):
    services = _services(tmp_path)
    calls = []
    html = ("<html><head><title>Source title</title></head><body><h2>Evidence</h2>"
            "<table><tr><th>Year</th><th>Value</th></tr><tr><td>2026</td><td>42</td></tr></table>"
            + "<p>Additional content.</p>" * 2000 + "<h2>Final evidence</h2><p>Tail preserved.</p></body></html>").encode()
    def download(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=html, stderr=b"\nhttps://example.invalid/report")
    monkeypatch.setattr("runtime.production.subprocess.run", download)
    context = ToolContext("research-owner", "research", read_scopes=read_scopes_for_role("research", skill_name=SKILL.name))
    result = services.web_extract(context, {"url": "https://example.invalid/report", "max_chars": 100})
    assert result["truncated"] and "| Year" in result["content"]
    assert result["source_ref"].startswith("sources/web/")
    assert not {"content_path", "full_body_path", "metadata_path"} & result.keys()
    assert "_trace/" not in json.dumps(result)
    full = services.read_file(context, {"path": result["source_ref"], "query": "Tail preserved"})
    assert "Tail preserved" in full["content"]
    assert full["path"] == full["logical_path"] == result["source_ref"]
    resumed = _services(tmp_path)
    again = resumed.web_extract(context, {"url": "https://example.invalid/report", "max_chars": 30000})
    assert again["cached"] and again["truncated"] and len(calls) == 1
    assert again["requested_max_chars"] == 30000 and again["prompt_window_clamped"] is True
    assert "Tail preserved" not in again["content"]
    assert "--max-filesize" in calls[0][0] and calls[0][1]["timeout"] == 13
    with pytest.raises(ToolContractError):
        services.read_file(ToolContext("slide", "slide"), {"path": result["source_ref"]})


def test_production_reports_download_error_without_exposing_url_credentials(tmp_path, monkeypatch):
    services = _services(tmp_path)
    calls = []
    def denied(*_args, **_kwargs):
        calls.append(True)
        return SimpleNamespace(
            returncode=22,
            stdout=b"",
            stderr=b"HTTP403 https://example.invalid/?token=private-value",
        )
    monkeypatch.setattr("runtime.production.subprocess.run", lambda *_a, **_kw:
        denied())
    with pytest.raises(ToolExecutionError) as caught:
        services.web_extract(ToolContext("research", "research"), {"url": "https://example.invalid/report"})
    assert caught.value.code == "web_extract_http_403"
    assert "private-value" not in str(caught.value)
    assert "do not retry the same URL" in str(caught.value)
    assert caught.value.diagnostics["status"] == "access_denied"
    assert caught.value.diagnostics["retry_recommended"] is False
    with pytest.raises(ToolExecutionError) as repeated:
        services.web_extract(
            ToolContext("orchestrator", "orchestrator"),
            {"url": "https://example.invalid/report", "render_js": True},
        )
    assert repeated.value.code == "web_extract_http_403"
    assert repeated.value.diagnostics["cached_failure"] is True
    assert calls == [True]


def test_research_web_pass_signals_sufficiency_and_has_a_bounded_stop(tmp_path, monkeypatch):
    services = _services(tmp_path)
    calls = []

    def download(argv, **_kwargs):
        calls.append(argv[-1])
        return SimpleNamespace(
            returncode=0,
            stdout=b"<html><main>Useful evidence.</main></html>",
            stderr=("\n" + argv[-1]).encode(),
        )

    monkeypatch.setattr("runtime.production.subprocess.run", download)
    monkeypatch.setattr("runtime.production.RESEARCH_WEB_MAX_CALLS", 2)
    monkeypatch.setattr("runtime.production.RESEARCH_WEB_SOFT_CALLS", 99)
    monkeypatch.setattr("runtime.production.RESEARCH_SUCCESSFUL_EXTRACT_TARGET", 2)
    owner = ToolContext("research-owner", "research")
    first = services.web_extract(owner, {"url": "https://one.example/evidence"})
    second = services.web_extract(owner, {"url": "https://two.example/evidence"})
    assert "research_convergence" not in first
    assert second["research_convergence"]["status"] == "sufficient_for_handoff"
    with pytest.raises(ToolExecutionError) as stopped:
        services.web_extract(owner, {"url": "https://three.example/evidence"})
    assert stopped.value.code == "research_web_budget_reached"
    assert stopped.value.diagnostics["retry_recommended"] is False
    assert len(calls) == 2

    # The convergence budget belongs to one Research Owner, not the whole run.
    orchestrator = ToolContext("orchestrator", "orchestrator")
    result = services.web_extract(orchestrator, {"url": "https://three.example/evidence"})
    assert result["content"] and len(calls) == 3


def test_two_access_denials_open_a_shared_host_circuit(tmp_path, monkeypatch):
    services = _services(tmp_path)
    calls = []

    def denied(*_args, **_kwargs):
        calls.append(True)
        return SimpleNamespace(returncode=22, stdout=b"", stderr=b"HTTP403")

    monkeypatch.setattr("runtime.production.subprocess.run", denied)
    owner = ToolContext("research-owner", "research")
    for path in ("first", "second"):
        with pytest.raises(ToolExecutionError) as failed:
            services.web_extract(owner, {"url": f"https://blocked.example/{path}"})
        assert failed.value.code == "web_extract_http_403"
    with pytest.raises(ToolExecutionError) as blocked:
        services.web_extract(owner, {"url": "https://blocked.example/third"})
    assert blocked.value.code == "web_extract_http_403"
    assert blocked.value.diagnostics["cached_failure"] is True
    assert blocked.value.diagnostics["retry_recommended"] is False
    assert calls == [True, True]


@pytest.mark.parametrize(
    "returncode,stderr,expected,retry",
    [
        (28, b"curl: (28) operation timed out", "web_extract_timeout", "once"),
        (60, b"curl: (60) certificate problem", "web_extract_ssl_failed", False),
        (47, b"curl: (47) too many redirects", "web_extract_redirect_failed", False),
        (6, b"curl: (6) could not resolve host", "web_extract_dns_failed", "once"),
    ],
)
def test_production_classifies_curl_failures(
    tmp_path, monkeypatch, returncode, stderr, expected, retry
):
    services = _services(tmp_path)
    monkeypatch.setattr(
        "runtime.production.subprocess.run",
        lambda *_a, **_kw: SimpleNamespace(
            returncode=returncode, stdout=b"", stderr=stderr
        ),
    )
    with pytest.raises(ToolExecutionError) as caught:
        services.web_extract(
            ToolContext("research", "research"),
            {"url": "https://example.invalid/report"},
        )
    assert caught.value.code == expected
    assert caught.value.diagnostics["retry_recommended"] == retry
    assert caught.value.diagnostics["recommended_action"]


@pytest.fixture
def redirected_source():
    counts = {}
    with pymupdf.open() as document:
        document.new_page().insert_text((72, 72), "Complete PDF evidence after redirect")
        pdf = document.tobytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            path = urlsplit(self.path).path
            counts[path] = counts.get(path, 0) + 1
            targets = {"/redirect": "/nested/report", "/redirect-base": "/nested/with-base",
                       "/redirect-pdf": "/documents/evidence.pdf"}
            if path in targets:
                self.send_response(302)
                self.send_header("Location", targets[path] + "?id=7&token=SYNTHETIC-REDIRECT-SECRET")
                self.end_headers()
                return
            self.send_response(200)
            if path.endswith(".pdf"):
                body, content_type = pdf, "application/pdf"
            else:
                base = '<base href="assets/">' if path.endswith("with-base") else ""
                body = (f'<html><head>{base}<title>Redirected source</title></head><body>'
                        '<main><p>Evidence</p><a href="source?id=8">Source</a>'
                        '<img src="chart.png" alt="Chart"></main><footer>Full body caveat</footer>'
                        '</body></html>').encode()
                content_type = "text/html"
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", counts, pdf
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("route,relative_root", [("/redirect", "/nested/"), ("/redirect-base", "/nested/assets/")])
def test_real_http_redirect_resolves_links_from_effective_url_and_keeps_original_cache(tmp_path, redirected_source, route, relative_root):
    base, counts, _pdf = redirected_source
    services = _services(tmp_path)
    owner = ToolContext("research", "research", read_scopes=read_scopes_for_role("research", skill_name=SKILL.name))
    original = base + route
    result = services.web_extract(owner, {"url": original})
    assert f"[Source]({base}{relative_root}source?id=8)" in result["content"]
    assert f"![Chart]({base}{relative_root}chart.png)" in result["content"]
    assert result["url"] == original and not result["cached"]
    assert parse_qs(urlsplit(result["effective_url"]).query) == {"id": ["7"], "token": ["SYNTHETIC-REDIRECT-SECRET"]}
    assert result["source_ref"] == (
        "sources/web/" + sha256(original.encode()).hexdigest() + ".full.md"
    )
    assert not {"content_path", "full_body_path", "metadata_path"} & result.keys()
    full = services.read_file(owner, {"path": result["source_ref"]})
    assert "Full body caveat" in full["content"]
    assert "_trace/" not in json.dumps(result)
    audit = "".join(path.read_text() for path in (tmp_path / "_trace/web-extract").iterdir())
    assert "SYNTHETIC-REDIRECT-SECRET" in audit
    before = dict(counts)
    resumed = _services(tmp_path).web_extract(owner, {"url": original})
    assert resumed["cached"] and resumed["effective_url"] == result["effective_url"] and counts == before


def test_real_http_redirect_keeps_pdf_bytes_separate_from_curl_metadata_and_caches(tmp_path, redirected_source):
    base, counts, pdf = redirected_source
    owner = ToolContext("research", "research")
    result = _services(tmp_path).web_extract(owner, {"url": base + "/redirect-pdf"})
    assert result["format"] == "pdf" and result["page_count"] == 1
    assert "source_sha256" not in result and "source_bytes" not in result
    metadata = json.loads(next((tmp_path / "_trace/web-extract").glob("*.json")).read_text())
    assert metadata["source_sha256"] == sha256(pdf).hexdigest()
    assert metadata["source_bytes"] == len(pdf)
    assert "Complete PDF evidence after redirect" in result["content"]
    assert "SYNTHETIC-REDIRECT-SECRET" in json.dumps(result)
    assert "url_effective" not in result["content"]
    before = dict(counts)
    resumed = _services(tmp_path).web_extract(owner, {"url": base + "/redirect-pdf"})
    assert resumed["cached"] and resumed["content"] == result["content"] and counts == before


@pytest.mark.parametrize("effective", [b"", b"https://name:SYNTHETIC-SECRET@example.invalid/report"])
def test_http_empty_or_credentialed_effective_url_is_not_cached(tmp_path, monkeypatch, effective):
    services = _services(tmp_path)
    monkeypatch.setattr("runtime.production.subprocess.run", lambda *_a, **_kw:
        SimpleNamespace(returncode=0, stdout=b"<p>Evidence</p>", stderr=b"\n" + effective))
    with pytest.raises(ToolExecutionError) as failed:
        services.web_extract(ToolContext("research", "research"), {"url": "https://example.invalid/report"})
    assert failed.value.code == "web_extract_url" and "SYNTHETIC-SECRET" not in str(failed.value)
    assert not list((tmp_path / "_trace/web-extract").glob("*.json"))
