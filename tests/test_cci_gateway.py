from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import threading

ROOT = Path(__file__).resolve().parents[1]


def test_gateway_preserves_dashboard_site_and_studio_requests():
    class Upstream(BaseHTTPRequestHandler):
        def do_GET(self):
            body = self.rfile.read(int(self.headers.get("content-length", 0)))
            payload = json.dumps({"service": self.server.label, "path": self.path,
                                  "method": self.command, "body": body.decode()}).encode()
            if self.path == "/events":
                payload = b"data: ready\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if self.path == "/events" else "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_POST = do_GET

        def log_message(self, *_args):
            pass

    services = []
    for label in ("dashboard", "site", "studio"):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        server.label = label
        threading.Thread(target=server.serve_forever, daemon=True).start()
        services.append(server)
    env = {**os.environ, "PORT": "0", "STUDIO_BASE_PATH": "/studio"}
    env.update({key: str(server.server_port) for key, server in zip(
        ["DASHBOARD_PORT", "MURAL_PORT", "STUDIO_PORT"], services)})
    process = subprocess.Popen(["node", str(ROOT / "webui/scripts/cci-gateway.mjs")],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        line = process.stdout.readline()
        port = int(re.search(r"gateway :(\d+)", line).group(1))

        def request(path, method="GET", body=None):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request(method, path, body)
            response = connection.getresponse()
            result = response.status, dict(response.getheaders()), response.read()
            connection.close()
            return result

        for path, service, upstream in [
            ("/", "dashboard", "/"),
            ("/api/existing?x=1", "dashboard", "/api/existing?x=1"),
            ("/studio-other", "dashboard", "/studio-other"),
            ("/mural-presenter/paper.pdf", "site", "/mural-presenter/paper.pdf"),
            ("/studio/", "studio", "/"),
            ("/studio/api/files/a%20b?rel=one%2Ftwo", "studio", "/api/files/a%20b?rel=one%2Ftwo"),
        ]:
            status, _, body = request(path)
            assert status == 200
            assert json.loads(body)["service"] == service
            assert json.loads(body)["path"] == upstream
        status, headers, _ = request("/studio?auth=login")
        assert status == 308 and headers["location"] == "/studio/?auth=login"
        _, _, body = request("/studio/api/save", "POST", "unchanged body")
        assert json.loads(body)["body"] == "unchanged body"
        assert json.loads(body)["method"] == "POST"
        status, headers, body = request("/studio/events")
        assert status == 200 and headers["content-type"] == "text/event-stream"
        assert body == b"data: ready\n\n"
    finally:
        process.terminate()
        process.wait(timeout=5)
        for server in services:
            server.shutdown()
            server.server_close()
