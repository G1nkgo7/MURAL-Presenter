"""Bounded public-web capture; no production endpoints or model requests."""
from dataclasses import replace
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import gzip
import json
import os
from pathlib import Path
import shlex
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

from PIL import Image
import pytest

from runtime import web_browser
from runtime.diagnostics import CommandResult
from runtime.production import ProductionSettings, ResourceSlots
from runtime.recovery import ResourceSlotTimeout
from runtime.tools import ToolCall, ToolExecutionError, ToolRuntime, register_tools
from state.workspace import file_sha256
from test_explicit_asset_commands import SKILL, image_context, services_for


def backend(root):
    service = services_for(root)
    service.settings = ProductionSettings(skill_source=SKILL, web_extract_timeout_s=15)
    service.environment = dict(os.environ)
    service.slots = ResourceSlots(root, service.settings, environment={})
    return service


def fake_capture(monkeypatch, *, error="", malformed=False, stdout_excerpt=None, receipt_extra=None):
    from runtime.production import _run_command
    calls = []
    def command(argv, **options):
        if Path(argv[1]).name != "web_browser.py":
            return _run_command(argv, **options)
        calls.append((argv, options))
        if error:
            options["on_stdout_line"](json.dumps({"error_code": error, "detail": "Source unavailable."}))
            raise ToolExecutionError("skill_command_failed", "worker exited")
        Image.new("RGB", (1280, 900), "green").save(argv[3])
        capture = {"url": argv[2], "effective_url": "https://example.test/official",
            "title": "Official source", "query": argv[argv.index("--query") + 1],
            "viewport": {"width": 1280, "height": 900}, "capture_scope": "viewport", "resource_warnings": []}
        capture.update(receipt_extra or {})
        if malformed:
            capture.pop("title")
        stdout = json.dumps(capture)
        options["on_stdout_line"](stdout + "\n")
        return CommandResult(returncode=0,
            stdout=stdout if stdout_excerpt is None else stdout_excerpt,
            stderr="", diagnostics={})
    monkeypatch.setattr("runtime.production._run_command", command)
    return calls


def test_capture_registers_one_real_identity_and_reuses_across_owners(tmp_path, monkeypatch):
    calls = fake_capture(monkeypatch)
    service = backend(tmp_path)
    context = image_context()
    args = {"url": "https://example.test/source", "query": "more diverse"}
    result = service.web_screenshot(context, args)
    assert result["path"].startswith("assets/web-capture-") and result["capture_scope"] == "viewport"
    assert "_trace" not in json.dumps(result) and "base64" not in json.dumps(result)
    record = service.artifacts.artifact(result["artifact_id"])
    assert record.status.value == "committed" and record.image_metadata["kind"] == "real"
    assert record.source_url == result["source_url"] and "reviewed_sha256" not in record.image_metadata
    other = replace(context, owner_id="orchestrator", role="orchestrator")
    reused = service.web_screenshot(other, args)
    assert reused["reused"] and reused["owner_id"] == context.owner_id
    assert reused["artifact_id"] == result["artifact_id"] and len(calls) == 1
    assert "--screenshot" in calls[0][0] and calls[0][1]["timeout_s"] <= 15
    assert not list(service.workspace.paths.runtime_tmp.glob("web-capture-*"))

    seen = replace(context, metadata={"consumed_visual_sources": [{"owner_id": context.owner_id,
        "path": result["path"], "sha256": file_sha256(tmp_path / result["path"])}]})
    command = shlex.join(["python", f"skills/{SKILL.name}/scripts/image.py", "register", ".",
        "--id", result["artifact_id"], "--path", result["path"], "--kind", "real",
        "--source", result["source_url"], "--slides", "2", "--purpose", "original brand statement", "--reviewed"])
    service.terminal(seen, {"command": command})
    updated = service.artifacts.artifact(result["artifact_id"])
    assert updated.image_metadata["reviewed_sha256"] == updated.sha256
    assert len(service.artifacts.catalog()["images"]) == 1
    assert service.web_screenshot(other, args)["artifact_id"] == result["artifact_id"]


def test_capture_error_is_closed_unavailable_and_shared_denial_is_not_retried(tmp_path, monkeypatch):
    calls = fake_capture(monkeypatch, error="web_extract_http_403")
    service = backend(tmp_path)
    runtime = ToolRuntime()
    register_tools(runtime, service.base_tool_handlers(), require_complete=False)
    args = {"url": "https://example.test/denied"}
    for number in range(2):
        result = runtime.execute(image_context(), ToolCall(f"capture-{number}", "web_screenshot", args))
        assert not result.ok and result.error_code == "web_screenshot_http_403"
        assert "_trace/" not in result.content
    runtime.assert_closed()
    assert len(calls) == 1
    assert service.artifacts.catalog()["images"][0]["status"] == "unavailable"
    state = json.loads(service.artifacts.path.read_text())
    assert all(t["status"] != "open" for t in state["transactions"].values())
    assert not list((tmp_path / "assets").glob("web-capture-*.png"))


def test_pdf_receipt_preserves_original_page_and_reuses_without_audit_metadata(tmp_path, monkeypatch):
    calls = fake_capture(monkeypatch, receipt_extra={"capture_scope": "pdf_page", "source_page": 4,
        "source_page_count": 12, "source_doc_sha256": "audit-only"})
    service = backend(tmp_path)
    args = {"url": "https://example.test/original.pdf#page=4"}
    result = service.web_screenshot(image_context(), args)
    assert result["capture_scope"] == "pdf_page" and result["source_page"] == 4
    assert result["source_page_count"] == 12 and "PDF" in result["registration_note"]
    assert "source_doc_sha256" not in result and "_trace" not in json.dumps(result)
    assert service.web_screenshot(image_context(), args)["reused"] and len(calls) == 1


@pytest.mark.parametrize("page,count", [(0, 12), (13, 12), (True, 12), (1, "12")])
def test_pdf_invalid_physical_page_receipt_does_not_commit(tmp_path, monkeypatch, page, count):
    fake_capture(monkeypatch, receipt_extra={"capture_scope": "pdf_page", "source_page": page,
        "source_page_count": count})
    service = backend(tmp_path)
    with pytest.raises(ToolExecutionError, match="PDF physical page"):
        service.web_screenshot(image_context(), {"url": "https://example.test/original.pdf"})
    assert service.artifacts.catalog()["images"][0]["status"] == "unavailable"


def test_capture_uses_complete_source_receipt_not_diagnostic_excerpt(tmp_path, monkeypatch):
    calls = fake_capture(monkeypatch, stdout_excerpt='{"url": "truncated')
    service = backend(tmp_path)
    result = service.web_screenshot(image_context(), {"url": "https://example.test/source"})
    record = service.artifacts.artifact(result["artifact_id"])
    assert record.status.value == "committed"
    assert result["source_url"] == "https://example.test/official"
    assert result["title"] == "Official source" and len(calls) == 1


def test_render_queue_wait_does_not_consume_capture_budget(tmp_path, monkeypatch):
    calls = fake_capture(monkeypatch)
    service = backend(tmp_path)
    clock = [1000.0]

    @contextmanager
    def queued(resource, *, deadline_monotonic):
        assert resource == "render" and deadline_monotonic == 1120.0
        clock[0] += 40.0
        yield 40.0

    monkeypatch.setattr(service.slots, "acquire", queued)
    monkeypatch.setattr("runtime.production.time", SimpleNamespace(
        monotonic=lambda: clock[0], strftime=time.strftime, gmtime=time.gmtime))
    result = service.web_screenshot(image_context(), {"url": "https://example.test/source"})
    assert result["path"] and len(calls) == 1
    assert calls[0][1]["timeout_s"] == 15.0
    assert float(calls[0][0][calls[0][0].index("--timeout") + 1]) == 15.0


def test_resource_wait_failure_does_not_poison_source_and_can_retry(tmp_path, monkeypatch):
    calls = fake_capture(monkeypatch)
    service = backend(tmp_path)
    attempts = []

    @contextmanager
    def queued(resource, *, deadline_monotonic):
        attempts.append(resource)
        if len(attempts) == 1:
            raise ResourceSlotTimeout("render admission deadline exceeded")
        yield 0.0

    monkeypatch.setattr(service.slots, "acquire", queued)
    runtime = ToolRuntime()
    register_tools(runtime, service.base_tool_handlers(), require_complete=False)
    args = {"url": "https://example.test/source"}
    failed = runtime.execute(image_context(), ToolCall("capture-busy", "web_screenshot", args))
    assert not failed.ok and failed.error_code == "web_screenshot_resource_busy"
    feedback = json.loads(failed.content)
    assert feedback["status"] == "deferred" and feedback["source_access"] == "not_attempted"
    assert feedback["retry_recommended"] is True and "Retry this URL" in feedback["recommended_action"]
    assert "_trace" not in failed.content and not calls
    assert service._cached_web_extract_failure(args["url"], True) is None
    state = json.loads(service.artifacts.path.read_text())
    assert all(t["status"] != "open" for t in state["transactions"].values())
    recovered = runtime.execute(image_context(), ToolCall("capture-retry", "web_screenshot", args))
    assert recovered.ok and len(calls) == 1
    assert service.artifacts.catalog()["images"][0]["status"] == "committed"
    runtime.assert_closed()


def test_capture_without_source_receipt_does_not_publish(tmp_path, monkeypatch):
    from runtime.production import _run_command
    def command(argv, **options):
        if Path(argv[1]).name == "web_browser.py":
            return CommandResult(returncode=0, stdout="", stderr="", diagnostics={})
        return _run_command(argv, **options)
    monkeypatch.setattr("runtime.production._run_command", command)
    service = backend(tmp_path)
    with pytest.raises(ToolExecutionError, match="source receipt"):
        service.web_screenshot(image_context(), {"url": "https://example.test/source"})
    assert service.artifacts.catalog()["images"][0]["status"] == "unavailable"
    assert not list((tmp_path / "assets").glob("web-capture-*.png"))


def test_capture_pixel_tampering_is_not_silently_replaced(tmp_path, monkeypatch):
    calls = fake_capture(monkeypatch)
    service = backend(tmp_path)
    args = {"url": "https://example.test/source"}
    result = service.web_screenshot(image_context(), args)
    Image.new("RGB", (1280, 900), "red").save(tmp_path / result["path"])
    with pytest.raises(ToolExecutionError, match="preserve"):
        service.web_screenshot(image_context(), args)
    assert len(calls) == 1


@pytest.mark.parametrize("error,malformed,expected", [
    ("web_extract_timeout", False, "web_screenshot_timeout"),
    ("", True, "web_screenshot_failed"),
])
def test_worker_failure_closes_lease_without_publishing(tmp_path, monkeypatch, error, malformed, expected):
    fake_capture(monkeypatch, error=error, malformed=malformed)
    service = backend(tmp_path)
    runtime = ToolRuntime()
    register_tools(runtime, service.base_tool_handlers(), require_complete=False)
    result = runtime.execute(image_context(), ToolCall("capture", "web_screenshot", {"url": "https://example.test/source"}))
    assert not result.ok and result.error_code == expected
    runtime.assert_closed()
    assert service.artifacts.catalog()["images"][0]["status"] == "unavailable"
    state = json.loads(service.artifacts.path.read_text())
    assert all(t["status"] != "open" for t in state["transactions"].values())
    assert not list((tmp_path / "assets").glob("web-capture-*.png"))
    assert not list(service.workspace.paths.runtime_tmp.glob("web-capture-*"))


@pytest.fixture
def website():
    counts = {}
    pixel = BytesIO()
    Image.new("RGB", (120, 120), (0, 220, 0)).save(pixel, format="PNG")
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_POST(self):
            counts["POST"] = counts.get("POST", 0) + 1
            self.send_response(200)
            self.end_headers()
        def do_GET(self):
            counts[self.path] = counts.get(self.path, 0) + 1
            if self.path in {"/redirect", "/private-redirect", "/redirect-loop"}:
                self.send_response(302)
                self.send_header("Location", "/redirect-loop" if self.path == "/redirect-loop" else "/page" if self.path == "/redirect"
                    else f"http://127.0.0.2:{self.server.server_port}/page")
                self.end_headers()
                return
            self.send_response(403 if self.path == "/denied" else 200)
            self.send_header("Content-Type", "image/png" if self.path == "/pixel.png" else "text/html")
            if self.path == "/compressed":
                self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            if self.path == "/private-image":
                self.wfile.write(b'<title>Source fixture</title><img src="/private-redirect">')
                return
            self.wfile.write(pixel.getvalue() if self.path == "/pixel.png" else
                gzip.compress(b"x" * 100000) if self.path == "/compressed" else b'''<!doctype html>
                <title>Official source fixture</title><body style="margin:0;background:#101820">
                <img src="/pixel.png" width="120" height="120">
                <section style="margin-top:1200px;height:300px;background:#ee3300;color:white">
                <h1>More diverse voices and bodies</h1><p>A genuine source statement.</p></section>
                <script>fetch('/mutate',{method:'POST'}).catch(()=>{});
                const ws=new WebSocket('ws://'+location.host+'/ws');</script></body>''')
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", counts
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def fixture_resources(monkeypatch, url):
    path = SKILL / "scripts/_internal/remote_resources.py"
    module = web_browser._resource_module(path)
    original = module.validate_url
    expected = urlsplit(url)
    def validate(value):
        parsed = urlsplit(value)
        if parsed.hostname == expected.hostname and parsed.port == expected.port:
            return parsed, parsed.hostname, parsed.port
        return original(value)
    monkeypatch.setattr(module, "validate_url", validate)
    monkeypatch.setattr(web_browser, "_resource_module", lambda _path: module)
    return path, module


def test_real_viewport_loads_image_and_query_scrolls_without_writes(tmp_path, monkeypatch, website):
    url, counts = website
    path, _module = fixture_resources(monkeypatch, url)
    for attempt in range(3):
        first = tmp_path / f"top-{attempt}.png"
        capture = web_browser.render_html(url + "/redirect", first, timeout_s=15, max_bytes=200000,
            screenshot=True, resource_module=path)
        assert capture["effective_url"] == url + "/page" and capture["title"] == "Official source fixture"
        with Image.open(first) as image:
            assert image.size == (1280, 900) and image.getpixel((50, 50))[:3] == (0, 220, 0)
    second = tmp_path / "query.png"
    capture = web_browser.render_html(url + "/page", second, timeout_s=15, max_bytes=200000,
        screenshot=True, query="More diverse voices and bodies", resource_module=path)
    assert capture["scroll_y"] > 500
    with Image.open(second) as image:
        assert any(r > 180 and g < 100 for r, g, b in image.convert("RGB").resize((128, 90)).get_flattened_data())
    assert counts.get("POST", 0) == 0 and counts.get("/ws", 0) == 0


@pytest.mark.parametrize("suffix,query,code", [
    ("/denied", "", "web_extract_http_403"),
    ("/page", "not in the original", "web_extract_query_not_found"),
    ("/private-redirect", "", "web_extract_url"),
    ("/redirect-loop", "", "web_extract_redirect_failed"),
])
def test_real_capture_failure_does_not_publish_image(tmp_path, monkeypatch, website, suffix, query, code):
    url, _counts = website
    path, _module = fixture_resources(monkeypatch, url)
    output = tmp_path / "failed.png"
    with pytest.raises(web_browser.WebBrowserError) as error:
        web_browser.render_html(url + suffix, output, timeout_s=15, max_bytes=200000,
            screenshot=True, query=query, resource_module=path)
    assert error.value.code == code and not output.exists()


def test_redirected_image_also_uses_public_reader(tmp_path, monkeypatch, website):
    url, counts = website
    path, _module = fixture_resources(monkeypatch, url)
    output = tmp_path / "source.png"
    capture = web_browser.render_html(url + "/private-image", output, timeout_s=15, max_bytes=200000,
        screenshot=True, resource_module=path)
    assert output.exists() and "image:network_failed" in capture["resource_warnings"]
    assert counts.get("/private-redirect", 0) == 1 and "/page" not in counts


def test_public_reader_denies_loopback_and_bounds_compressed_responses(monkeypatch, website):
    url, counts = website
    path = SKILL / "scripts/_internal/remote_resources.py"
    original = web_browser._resource_module(path)
    with pytest.raises(ValueError, match="scope_denied"):
        original.fetch(url + "/page", timeout=2, max_bytes=2000)
    assert counts == {}
    _path, allowed = fixture_resources(monkeypatch, url)
    with pytest.raises(ValueError, match="too_large"):
        allowed.fetch(url + "/compressed", timeout=2, max_bytes=2000)
