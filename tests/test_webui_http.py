from pathlib import Path
import sys

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


def test_webui_page_assets_and_active_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("STUDIO_DATA_DIR", str(tmp_path / "studio"))
    monkeypatch.setenv("MURAL_SECRETS_ENV_PATH", str(tmp_path / "no-secrets.env"))
    monkeypatch.setenv("MURAL_SKILL_DIR", str(ROOT / "skills/mural-presenter-v1"))
    sys.path.insert(0, str(ROOT / "webui/studio"))
    from app import main, engine

    with TestClient(main.app) as client:
        assert client.get("/healthz").status_code == 200
        page = client.get("/")
        assert page.status_code == 200
        assert "text/html" in page.headers["content-type"]
        for asset in ["app.js", "app.css", "page-editor.js", "page-editor.css"]:
            response = client.get(f"/static/{asset}")
            assert response.status_code == 200, asset
            assert response.content
        skill = engine._mural_v1_skill()
        assert skill["ready"], skill.get("unavailable_reason")
        assert skill["pairing"] == "mural-presenter-v1"
