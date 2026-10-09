import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("prefix", ["", "/studio", "/tools/studio"])
def test_deployment_prefix_in_http_assets_redirects_and_previews(prefix, tmp_path):
    env = {**os.environ, "STUDIO_BASE_PATH": prefix,
           "STUDIO_DATA_DIR": str(tmp_path / "data"),
           "MURAL_SECRETS_ENV_PATH": str(tmp_path / "no-secrets.env"),
           "PYTHONPATH": str(ROOT / "webui/studio"), "STUDIO_AUTH_ENABLED": "0"}
    script = r'''
import os
from urllib.parse import urljoin
from fastapi import Response
from fastapi.testclient import TestClient
from app import main, auth, preview_fonts

prefix = os.environ["STUDIO_BASE_PATH"]
with TestClient(main.app) as client:
    for page_path in ["/", "/trajectory-monitor"]:
        page = client.get(prefix + page_path)
        assert page.status_code == 200
        assert f'globalThis.STUDIO_BASE_PATH = "{prefix}";' in page.text
        assert f'src="{prefix}/static/app.js' in page.text
        assert f'href="{prefix}/static/app.css' in page.text
        assert "<base " not in page.text
    # 代理移除前缀后，应用仍应生成包含外部前缀的地址。
    assert f'src="{prefix}/static/app.js' in client.get("/").text
    assert client.get(prefix + "/static/app.js").status_code == 200
    css = client.get(prefix + "/static/app.css")
    assert css.status_code == 200
    assert urljoin(prefix + "/static/app.css", "../api/preview-fonts/NotoSansSC.ttf") == prefix + "/api/preview-fonts/NotoSansSC.ttf"
    assert client.get(prefix + "/healthz").json()["ok"]
    assert client.get(prefix + "/api/models/custom").json() == {"models": []}
    assert client.post(prefix + "/api/models/custom", json={}).status_code == 422
    for path in ["/login", "/register"]:
        response = client.get(prefix + path, follow_redirects=False)
        assert response.headers["location"] == prefix + "/"
    response = client.post(prefix + "/api/auth/logout", follow_redirects=False)
    assert response.headers["location"] == prefix + "/"
    assert f'href="{prefix}/api/preview-fonts.css?' in preview_fonts.inject("<head></head>")
    response = Response()
    auth.set_session_cookie(response, "test-token")
    assert f'Path={prefix}/;' in response.headers["set-cookie"]

# Uvicorn 补回代理移除的前缀，StaticFiles 子应用才能正确解析挂载路径。
import socket
import threading
import time
import httpx
import uvicorn
listener = socket.socket()
listener.bind(("127.0.0.1", 0))
port = listener.getsockname()[1]
server = uvicorn.Server(uvicorn.Config(main.app, root_path=prefix, log_level="error"))
thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
thread.start()
try:
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.02)
    assert server.started
    with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/api/models/custom").json() == {"models": []}
        assert client.get("/login").headers["location"] == prefix + "/"
finally:
    server.should_exit = True
    thread.join(timeout=5)
    listener.close()
'''
    result = subprocess.run([sys.executable, "-c", script], env=env, cwd=ROOT,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
