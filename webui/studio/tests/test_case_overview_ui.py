"""完成任务重新打开时展示 PPT，并始终保留当前任务的模型和原始需求。"""
import os
from pathlib import Path
from urllib.parse import urlparse

from app import engine


def test_browser_completed_case_preview_and_context(tmp_path, monkeypatch):
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    from playwright.sync_api import sync_playwright

    studio = Path(__file__).resolve().parents[1]
    template = Environment(loader=FileSystemLoader(studio / "templates"),
                           autoescape=select_autoescape()).get_template("app.html")
    html = template.render(request={"scope": {}}, authenticated=True, auth_enabled=False,
                           dynamic_enabled=True, edition="test", asset_ver="test",
                           user={"display_name": "测试用户", "username": "test", "role": "user"})
    for name in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(name, str(tmp_path))
    env = engine.render_env(dict(os.environ))
    query = "Create a presentation about renewable energy.\n" + "Compare solar, wind and storage. " * 80 + "<script>unsafe()</script>"
    base = {"title": "Renewable energy", "query": "internal augmented query",
            "user_query": query, "status": "completed", "phase": "done", "slides_total": 3,
            "rendered": [1, 2, 3], "authored": [1, 2, 3], "slides_rendered": 3,
            "html_ready": True, "html_final": True, "model": "astra", "model_label": "GPT-6 Astra",
            "skill_version": "mural-presenter-v1", "elapsed_s": 42}
    cases = {
        "901": {**base, "id": 901},
        "902": {**base, "id": 902, "status": "running", "phase": "rendering",
                "model_label": "Opus 5", "user_query": "制作三页中文演示。"},
        "903": {**base, "id": 903, "html_ready": False},
        "904": {**base, "id": 904, "status": "failed", "error": "生成失败"},
    }
    deck_html = '''<!doctype html><html><head><style>
body { margin:0; background:#f1f3f8; font-family:Arial; color:#172b4d; }
.slide { display:none; padding:90px; width:1280px; height:720px; }
.slide.active { display:block; } h1 { font-size:72px; } p { font-size:32px; }
</style></head><body><div id="deck">
<section class="slide active" data-slide="1"><p>01 / ENERGY TRANSITION</p><h1>Renewable energy</h1><p>Solar · Wind · Storage</p></section>
<section class="slide" data-slide="2"><h1>Solar and wind</h1></section>
<section class="slide" data-slide="3"><h1>Energy storage</h1></section></div>
<script>window.__deckGo=i=>document.querySelectorAll('.slide').forEach((s,n)=>s.classList.toggle('active',n===i));
window.cleanDeck={go:n=>window.__deckGo(n-1),count:3,fontsReady:document.fonts.ready};</script></body></html>'''
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=env["PPT_SKILL_BROWSER_EXE"], env=env,
                                     headless=True, args=["--no-sandbox"])
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 960})
            page.set_default_timeout(12000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def serve(route):
                path = urlparse(route.request.url).path
                if path == "/":
                    route.fulfill(content_type="text/html; charset=utf-8", body=html)
                elif path.startswith("/static/") and (studio / path.lstrip("/")).is_file():
                    mime = {".js": "application/javascript", ".css": "text/css", ".svg": "image/svg+xml",
                            ".ttf": "font/ttf"}.get(Path(path).suffix, "application/octet-stream")
                    route.fulfill(content_type=mime, body=(studio / path.lstrip("/")).read_bytes())
                elif path == "/api/decks":
                    route.fulfill(json={"decks": list(cases.values())})
                elif path.endswith("/progress"):
                    route.fulfill(json=cases[path.split("/")[3]])
                elif path.endswith("/files/present.html") or path == "/dynamic-deck.html":
                    route.fulfill(content_type="text/html", body=deck_html)
                elif path == "/api/dynamic/conversation":
                    route.fulfill(json={"active": False, "meta": {"title": "Dynamic case", "status": "completed",
                        "model_label": "Dynamic model", "user_query": "Create an animated presentation."}, "events": [
                        {"kind": "deck_update", "seq": 1, "deck_url": "/dynamic-deck.html", "n_pages": 3},
                        {"kind": "done", "seq": 2, "status": "completed"}]})
                elif path.startswith("/api/"):
                    route.fulfill(json={})
                else:
                    route.abort()

            page.route("**/*", serve)
            page.goto("http://case.test/#static-901", wait_until="domcontentloaded")
            page.wait_for_selector("#editor.workspace-ppt", state="visible")
            assert page.evaluate("ed.sel") == 1
            page.locator("#canvas-deck.font-ready").wait_for(state="visible")
            assert page.locator("#case-model").inner_text() == "GPT-6 Astra"
            assert page.locator("#case-query-preview").text_content() == query
            assert page.locator("#case-query").get_attribute("open") is None

            for theme in ("light", "dark"):
                page.evaluate("theme=>document.documentElement.dataset.theme=theme", theme)
                for width in (1440, 1024, 390):
                    page.set_viewport_size({"width": width, "height": 960})
                    assert page.locator("#case-context").is_visible()
                    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
                    page.locator("#case-query summary").click()
                    assert page.locator("#case-query-full").text_content() == query
                    assert page.locator("#case-query-full script").count() == 0
                    assert page.locator("#case-query-full").evaluate("n=>n.scrollHeight>n.clientHeight")
                    assert page.locator(".ed-body").bounding_box()["height"] > 200
                    page.screenshot(path=str(tmp_path / f"case-context-{theme}-{width}.png"), animations="disabled")
                    page.locator("#case-query summary").click()

            page.set_viewport_size({"width": 1440, "height": 960})
            page.locator('[data-workspace-view="workflow"]').click()
            page.evaluate("p=>apply(p)", cases["901"])
            assert page.evaluate("ed.workspaceView") == "workflow"
            assert page.locator("#case-context").is_visible()
            page.locator("#case-query summary").click()
            page.evaluate("id=>openDeck(id)", 902)
            assert page.evaluate("ed.workspaceView") == "workflow"
            assert page.locator("#case-model").inner_text() == "Opus 5"
            assert page.locator("#case-query-preview").text_content() == "制作三页中文演示。"
            assert page.locator("#case-query").get_attribute("open") is None
            for deck_id in (903, 904):
                page.evaluate("id=>openDeck(id)", deck_id)
                assert page.evaluate("ed.workspaceView") == "workflow"
                assert page.locator('[data-workspace-view="ppt"]').is_disabled()
            page.evaluate("openDeck(901)")
            assert page.evaluate("ed.workspaceView") == "ppt"
            assert page.evaluate("ed.sel") == 1
            page.locator("#canvas-deck.font-ready").wait_for(state="visible")
            page.screenshot(path=str(tmp_path / "completed-case-preview.png"), animations="disabled")

            page.evaluate("openDynamic('dynamic-case')")
            assert page.evaluate("ed.workspaceView") == "ppt"
            assert page.evaluate("ed.sel") == 1
            assert page.locator("#case-model").inner_text() == "Dynamic model"
            assert page.locator("#case-query-preview").text_content() == "Create an animated presentation."
            page.evaluate("closeEditor()")
            assert page.locator("#case-context").is_hidden()
            assert not errors, errors
        finally:
            browser.close()
