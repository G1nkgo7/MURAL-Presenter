"""真实本机动态站点验收，不访问第三方网页或模型。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import threading
import time

import pytest

from runtime.tools import ToolContext, ToolExecutionError
from runtime.production import (
    ProductionSettings,
    ProductionToolServices,
    ResourceSlots,
    read_scopes_for_role,
)
from test_explicit_asset_commands import SKILL, services_for


@pytest.fixture
def website():
    counts = {}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            counts["POST"] = counts.get("POST", 0) + 1
            self.send_response(200)
            self.end_headers()

        def do_GET(self):
            counts[self.path] = counts.get(self.path, 0) + 1
            if self.path == "/disconnect":
                self.close_connection = True
                return
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/nested/report")
                self.end_headers()
                return
            if self.path == "/delayed-unavailable":
                time.sleep(.15)
            self.send_response(403 if self.path == "/denied" else 503 if self.path in {"/unavailable", "/delayed-unavailable"} else 200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if self.path == "/evidence":
                self.wfile.write(b'<h1>Hydrated evidence</h1><table><tr><th>Value</th></tr><tr><td>42</td></tr></table><a href="source?id=7">Source</a>')
            elif self.path == "/large":
                self.wfile.write(b"<p>" + b"evidence " * 10000 + b"</p>")
            elif self.path == "/large-dom":
                self.wfile.write(b'<body><script>document.body.append("evidence ".repeat(10000))</script></body>')
            elif self.path == "/dependency-failure":
                self.wfile.write(b'<main>Loading research evidence...</main><script>fetch("/unavailable").catch(()=>{})</script>')
            elif self.path == "/script-failure":
                self.wfile.write(b'<main>Loading research evidence...</main><script src="/unavailable"></script>')
            elif self.path == "/stylesheet-failure":
                self.wfile.write(b'<link rel="stylesheet" href="/unavailable"><main>Loading research evidence...</main>')
            elif self.path == "/network-failure":
                self.wfile.write(b'<main>Loading research evidence...</main><script>const request=new XMLHttpRequest();request.open("GET","/disconnect");request.send();</script>')
            elif self.path == "/socket-fallback-failure":
                self.wfile.write(b'''<main>Loading fallback evidence...</main><script>
                const ws=new WebSocket('ws://'+location.host+'/ws');
                ws.onclose=()=>fetch('/delayed-unavailable').then(r=>r.text()).then(t=>document.querySelector('main').innerHTML=t).catch(()=>{});</script>''')
            else:
                self.wfile.write(b'''<html><head><title>Dynamic test</title></head><body><main>Loading</main><p id="socket-status"></p>
                <script>fetch('/evidence').then(r=>r.text()).then(t=>document.querySelector('main').innerHTML=t);
                fetch('/mutate',{method:'POST'}).catch(()=>{});
                const ws=new WebSocket('ws://'+location.host+'/ws'); ws.onopen=()=>ws.send('mutation');
                ws.onclose=()=>document.querySelector('#socket-status').textContent='WebSocket blocked';</script></body></html>''')
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", counts
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def services(tmp_path, *, max_bytes=200000, timeout=15):
    result = services_for(tmp_path)
    result.settings = ProductionSettings(skill_source=SKILL,
        web_extract_timeout_s=timeout, web_extract_max_bytes=max_bytes)
    result.environment = dict(os.environ)
    result.slots = ResourceSlots(tmp_path, result.settings, environment={})
    result._command_environment = lambda: ProductionToolServices._command_environment(result)
    result._ensure_external_allowed = lambda _context: None
    return result


def test_real_browser_hydrates_and_separates_http_cache_then_resume(tmp_path, website):
    url, counts = website
    owner = ToolContext(
        "research",
        "research",
        read_scopes=read_scopes_for_role("research", skill_name=SKILL.name),
    )
    backend = services(tmp_path)
    raw = backend.web_extract(owner, {"url": url + "/redirect"})
    assert "Hydrated evidence" not in raw["content"] and raw["fetch_method"] == "http"
    rendered = backend.web_extract(owner, {"url": url + "/redirect", "render_js": True})
    assert rendered["fetch_method"] == "browser" and rendered["source_ref"] != raw["source_ref"]
    assert "Hydrated evidence" in rendered["content"] and "| 42" in rendered["content"]
    full = backend.read_file(owner, {"path": rendered["source_ref"], "query": "WebSocket blocked"})
    assert "WebSocket blocked" in full["content"]
    assert "_trace/" not in str(rendered)
    assert rendered["extraction_scope"] == "main"
    assert url + "/nested/source?id=7" in rendered["content"]
    assert counts.get("POST", 0) == 0
    assert counts.get("/ws", 0) == 0
    before = dict(counts)
    again = services(tmp_path).web_extract(owner, {"url": url + "/redirect", "render_js": True})
    assert again["cached"] and counts == before
    assert not list(backend.workspace.paths.runtime_tmp.glob("web-*"))


@pytest.mark.parametrize("path,limit,error", [
    ("/denied", 200000, "web_extract_http_403"),
    ("/large", 1000, "web_extract_too_large"),
    ("/large-dom", 1000, "web_extract_too_large"),
    ("/disconnect", 200000, "web_extract_browser_failed"),
    ("/dependency-failure", 200000, "fetch:http_503"),
    ("/script-failure", 200000, "script:http_503"),
    ("/stylesheet-failure", 200000, "stylesheet:http_503"),
    ("/network-failure", 200000, "xhr:network_failed"),
    ("/socket-fallback-failure", 200000, "fetch:http_503"),
])
def test_browser_failure_or_limit_never_publishes_success_cache(tmp_path, website, path, limit, error):
    url, _counts = website
    backend = services(tmp_path, max_bytes=limit)
    with pytest.raises(ToolExecutionError) as caught:
        backend.web_extract(ToolContext("research", "research"), {"url": url + path, "render_js": True})
    assert error in str(caught.value) or error == caught.value.code
    assert caught.value.code == (error if error.startswith('web_extract_') else 'web_extract_dependency_failed')
    assert caught.value.diagnostics["error_code"] == caught.value.code
    assert caught.value.diagnostics["recommended_action"]
    assert "stderr" not in caught.value.diagnostics
    assert "diagnostics" not in caught.value.diagnostics
    assert not list((tmp_path / "_trace/web-extract").glob("*.json"))
    assert not list(backend.workspace.paths.runtime_tmp.glob("web-*"))


def test_browser_command_deadline_and_failure_remain_real_errors(tmp_path, monkeypatch):
    backend = services(tmp_path)
    seen = []
    def command(argv, **options):
        seen.append((argv, options))
        raise ToolExecutionError("handler_error", "timeout", diagnostics={"termination": "timeout"})
    monkeypatch.setattr("runtime.production._run_command", command)
    with pytest.raises(ToolExecutionError):
        backend.web_extract(ToolContext("research", "research"),
                            {"url": "https://example.test/dynamic", "render_js": True})
    argv, options = seen[0]
    assert argv[1].endswith("web_browser.py")
    assert 0 < options["timeout_s"] <= 15
    assert options["environment"]["TMPDIR"].startswith(str(tmp_path))
    assert not list((tmp_path / "_trace/web-extract").glob("*.json"))
