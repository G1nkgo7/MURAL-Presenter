"""MURAL 加载品牌画面有真实读取边界；失败可重试，旧请求不能覆盖当前 Deck。"""
import os
from pathlib import Path
import re

from app import engine


def test_chromium_deck_loading_success_retry_timeout_and_navigation(tmp_path, monkeypatch):
    from playwright.sync_api import sync_playwright

    studio = Path(__file__).resolve().parents[1]
    source = (studio / "static/app.js").read_text()
    functions = []
    for name in ("openDeck", "closeEditor", "renderDeckLoadState", "syncViewToggle", "jget", "muralMakingMarkup", "escapeHtml", "workflowSplitRatio"):
        start = re.search(rf"^(?:async )?function {name}\(", source, re.M).start()
        end = re.search(r"^(?:async )?function ", source[start + 1:], re.M)
        functions.append(source[start:start + 1 + end.start()])
    for key in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(key, str(tmp_path))
    env = engine.render_env(dict(os.environ))
    setup = """
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const ed={id:null,kind:'static'}, trajectoryMode=false, pageViewTransitionTarget=null;
const noop=()=>{}, clearStudioNotices=noop, setProcRailOpen=noop, cancelWorkflowAgentTraceRequest=noop,
  stopLive=noop, showComposer=noop, setCanvasSpec=noop, setRailLabels=noop, buildFilmstrip=noop,
  renderWorkflowCanvas=noop,
  finishComposerLaunchMorph=noop, setTopbar=noop, syncPageSpeechDrawer=noop,
  subscribe=noop, startLive=noop, tick=noop, editorPresentationKind=()=> 'static',
  transitionComposerToEditor=async()=>{}, isStaticActiveStatus=s=>s==='running';
const applied=[];
const apply=p=>{applied.push(p.title);ed.status=p.status;$('#canvas-empty').innerHTML='<p>任务记录已载入</p>';syncViewToggle()};
const originalTimeout=window.setTimeout;
window.setTimeout=(callback,delay)=>{if(delay===20000){window.loadDeadline=callback;return 0}return originalTimeout(callback,delay)};
"""
    html = '''<!doctype html><html data-theme="dark"><head><meta charset="utf-8"></head><body>
<div class="test-shell"><aside class="test-sidebar">MURAL Presenter</aside>
<div id="editor" class="editor"><header class="test-top">返回 · 演示文稿</header><div class="ed-body">
<div class="canvas-wrap"><div id="canvas" class="canvas"><div id="canvas-empty" class="canvas-empty"></div>
<img id="canvas-img" hidden><iframe id="canvas-deck" hidden></iframe></div></div></div></div></div><div hidden>'''
    html += ''.join(f'<div id="{name}"></div>' for name in (
        "plan-panel", "pr-feed", "pr-act", "pr-count", "pr-bar", "follow-btn", "ed-title"))
    html += '</div></body></html>'
    requests = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=env["PPT_SKILL_BROWSER_EXE"], env=env,
                                    headless=True, args=["--no-sandbox"])
        try:
            page = browser.new_page(viewport={"width": 1920, "height": 960})
            page.set_default_timeout(10000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def serve(route):
                if route.request.url.endswith("/progress"):
                    requests.append(route)
                elif route.request.url == "http://loading.test/":
                    route.fulfill(content_type="text/html", body=html)
                elif route.request.url.endswith("/static/logo.svg"):
                    route.fulfill(content_type="image/svg+xml", body=(studio / "static/logo.svg").read_text())
                else:
                    route.abort()

            page.route("**/*", serve)
            page.goto("http://loading.test/")
            page.add_style_tag(path=str(studio / "static/app.css"))
            page.add_style_tag(content='''
.test-shell { display: flex; height: 100%; }
.test-sidebar { flex: 0 0 248px; padding: 24px; border-right: 1px solid #34323c; }
.test-top { flex: 0 0 64px; padding: 20px; border-bottom: 1px solid #34323c; }
@media (max-width: 760px) { .test-sidebar { display: none; } }
''')
            page.add_script_tag(content="\n".join(functions) + setup)

            def start(deck):
                previous = len(requests)
                with page.expect_request(f"**/api/decks/{deck}/progress"):
                    page.evaluate("id=>{void openDeck(id)}", deck)
                page.wait_for_function("!!ed.deckLoadController")
                assert len(requests) > previous

            # 读取阶段使用品牌动画，但不伪造制作页数或百分比。
            start("one")
            assert "正在读取任务记录" in page.locator("#canvas-empty").inner_text()
            assert page.locator(".develop-ring").count() == 0
            assert page.locator(".mural-making-hero.active").count() == 1
            assert page.locator(".mural-making-brand strong").inner_text() == "MURAL"
            assert page.locator(".mural-making-brand img").get_attribute("src") == "/static/logo.svg"
            assert page.locator(".mural-making-brand progress").count() == 0
            assert "%" not in page.locator(".mural-making-hero").inner_text()
            page.wait_for_function("document.querySelector('.mural-making-brand img').naturalWidth>0")

            def assert_full_loading():
                stage = page.locator(".canvas-wrap").bounding_box()
                hero = page.locator(".mural-making-hero").bounding_box()
                brand = page.locator(".mural-making-brand").bounding_box()
                items = page.locator(".mural-making-brand > *").evaluate_all(
                    "nodes=>nodes.map(n=>{const b=n.getBoundingClientRect();return {top:b.top,bottom:b.bottom}})")
                for axis, extent in (("x", "width"), ("y", "height")):
                    assert 0 <= hero[axis] - stage[axis] <= 15
                    assert 0 <= stage[axis] + stage[extent] - hero[axis] - hero[extent] <= 15
                assert abs((items[0]["top"] + items[-1]["bottom"]) / 2
                           - (brand["y"] + brand["height"] / 2)) < 12
                assert page.locator("#canvas-empty").evaluate("n=>n.scrollHeight<=n.clientHeight+1")
                assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")

            assert_full_loading()
            page.wait_for_function("getComputedStyle(document.querySelector('.mural-making-brand img')).opacity==='1'")
            page.screenshot(path=str(tmp_path / "mural-record-loading.png"), full_page=True)
            for width, height in ((2560, 1440), (1100, 850), (420, 850)):
                page.set_viewport_size({"width": width, "height": height})
                assert_full_loading()
            page.screenshot(path=str(tmp_path / "mural-record-loading-mobile.png"), full_page=True)
            page.set_viewport_size({"width": 1920, "height": 960})
            assert page.locator("#canvas-empty").evaluate("n=>getComputedStyle(n).animationName") == "none"
            requests[-1].fulfill(json={"title": "任务一", "status": "completed"})
            page.wait_for_function("ed.deckLoadController===null")
            assert page.evaluate("applied") == ["任务一"]
            assert page.locator(".deck-load-state").count() == 0
            assert "workspace-workflow" in page.locator("#editor").get_attribute("class")

            start("failure")
            requests[-1].fulfill(status=503, body="unavailable")
            page.locator("[data-deck-load-retry]").wait_for()
            assert "不代表生成失败" in page.locator("#canvas-empty").inner_text()
            with page.expect_request("**/api/decks/failure/progress"):
                page.locator("[data-deck-load-retry]").click()
            requests[-1].fulfill(json={"title": "重试成功", "status": "completed"})
            page.wait_for_function("ed.deckLoadController===null")

            start("timeout")
            page.evaluate("loadDeadline()")
            page.locator("[data-deck-load-retry]").wait_for()
            assert page.locator(".develop-ring").count() == 0
            assert page.locator(".mural-making-hero.active").count() == 0
            assert page.locator(".mural-making-brand strong").evaluate("n=>getComputedStyle(n).animationName") == "none"
            page.screenshot(path=str(tmp_path / "load-failure.png"), full_page=True)
            page.set_viewport_size({"width": 740, "height": 380})
            page.locator("[data-deck-load-retry]").scroll_into_view_if_needed()
            assert page.locator("[data-deck-load-retry]").bounding_box()["y"] < 380
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            page.set_viewport_size({"width": 1920, "height": 960})

            start("same")
            page.evaluate("window.previousLoad=ed.deckLoadController")
            start("same")
            assert page.evaluate("previousLoad.signal.aborted")
            assert page.locator("[data-deck-load-retry]").count() == 0
            requests[-1].fulfill(json={"title": "当前任务", "status": "completed"})
            page.wait_for_function("ed.deckLoadController===null")
            assert page.evaluate("applied") == ["任务一", "重试成功", "当前任务"]

            start("closing")
            page.evaluate("closeEditor()")
            page.wait_for_function("ed.id===null")
            assert page.locator("[data-deck-load-retry]").count() == 0
            assert not errors
        finally:
            browser.close()
