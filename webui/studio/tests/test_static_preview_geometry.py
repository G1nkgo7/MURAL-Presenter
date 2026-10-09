"""Preview a single page even when an older inline player omits its geometry."""

import os
from pathlib import Path
import re

import pytest
from playwright.sync_api import sync_playwright

from app import engine


@pytest.mark.parametrize("width,height", [(1600, 900), (1600, 1200), (900, 1600)])
def test_inline_player_preserves_page_geometry_and_navigation(tmp_path, monkeypatch, width, height):
    studio = Path(__file__).resolve().parents[1]
    source = (studio / "static/app.js").read_text()
    functions = []
    for name in (
        "activeCanvasSize", "setCanvasSpec", "fitCanonicalStaticFrame",
        "resetCanonicalStaticFrame", "guardSlideDocument", "stabilizeStaticDeckPlayer",
        "stabilizeDynamicDeckPlayer", "fitDeckViewport",
    ):
        start = re.search(rf"^function {name}\(", source, re.M).start()
        end = re.search(r"^(?:async )?function ", source[start + 1:], re.M)
        functions.append(source[start:start + 1 + end.start()])
    for key in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(key, str(tmp_path))
    env = engine.render_env(dict(os.environ))
    html = """<!doctype html><html><body><div id="editor"><div id="canvas">
<iframe id="canvas-deck" data-deck-kind="static"></iframe></div></div></body></html>"""
    slides = "".join(
        f'<section class="slide" data-slide="{n}" style="background:rgb({n * 40},80,100)">'
        f'<h1>Page {n}</h1><div class="marker"></div></section>'
        for n in range(1, 6)
    )
    deck_html = f"""<!doctype html><html><head><style>
html,body {{margin:0;padding:0}}
.slide {{position:relative;width:{width}px;height:{height}px;overflow:hidden}}
.marker {{position:absolute;right:12px;bottom:12px;width:20px;height:20px;background:white}}
</style></head><body><main id="deck">{slides}</main><script>
window.cleanDeck = {{go: n => document.querySelectorAll('.slide').forEach(
    s => s.classList.toggle('active', Number(s.dataset.slide) === n))}};
cleanDeck.go(5);
function fit() {{
    const deck = document.getElementById('deck');
    const w = deck.offsetWidth, h = deck.offsetHeight;
    const scale = Math.min(innerWidth/w, innerHeight/h);
    deck.style.left = `${{(innerWidth - w*scale)/2}}px`;
    deck.style.top = `${{(innerHeight - h*scale)/2}}px`;
    deck.style.transform = `scale(${{scale}})`;
}}
addEventListener('resize', fit);fit();
</script></body></html>"""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=env["PPT_SKILL_BROWSER_EXE"], env=env,
            headless=True, args=["--no-sandbox"],
        )
        try:
            page = browser.new_page(viewport={"width": 1920, "height": 960})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.set_content(html)
            page.add_style_tag(path=str(studio / "static/app.css"))
            page.add_style_tag(content="""
#canvas {width: min(900px, calc(100vw - 24px));height:500px;aspect-ratio:auto}
#canvas-deck {border:0}
""")
            page.add_script_tag(content="\n".join(functions) + """
const $ = s => document.querySelector(s), ed = {};
const editorPresentationKind = () => 'static', scheduleDeckViewportFit = () => {};
""")
            page.locator("#canvas-deck").evaluate("(f, html) => f.srcdoc = html", deck_html)
            page.wait_for_function("!!document.querySelector('#canvas-deck').contentWindow.cleanDeck")
            page.evaluate("([w,h]) => setCanvasSpec({width_px:w,height_px:h,source:'renders/slide_01.png'})", [width, height])
            assert page.locator("#canvas-deck").evaluate("f => f.contentDocument.querySelector('#deck').offsetHeight") == height * 5
            page.evaluate("fitCanonicalStaticFrame(document.querySelector('#canvas-deck'))")
            assert page.evaluate("activeCanvasSize()") == {"width": width, "height": height, "ratio": width / height}

            for viewport in ({"width": 1920, "height": 960}, {"width": 420, "height": 850}):
                page.set_viewport_size(viewport)
                for n in (5, 1, 3):
                    state = page.evaluate("""n => {
                        const f = document.querySelector('#canvas-deck'), d = f.contentDocument;
                        f.contentWindow.cleanDeck.go(n);fitDeckViewport(f);fitDeckViewport(f);
                        const deck = d.querySelector('#deck'), active = d.querySelector('.slide.active');
                        return {size:activeCanvasSize(), deck:[deck.offsetWidth,deck.offsetHeight],
                            active:Number(active.dataset.slide), page:[active.offsetWidth,active.offsetHeight],
                            top:active.offsetTop,left:active.offsetLeft,
                            visible:[...d.querySelectorAll('.slide')].filter(s => getComputedStyle(s).visibility !== 'hidden').length,
                            guards:d.querySelectorAll('#studio-static-inline-page-guard').length};
                    }""", n)
                    assert state == {
                        "size": {"width": width, "height": height, "ratio": width / height},
                        "deck": [width, height], "active": n, "page": [width, height],
                        "top": 0, "left": 0, "visible": 1, "guards": 1,
                    }
                page.screenshot(path=str(tmp_path / f"preview-{viewport['width']}.png"))

            page.evaluate("setCanvasSpec(null)")
            page.evaluate("fitDeckViewport(document.querySelector('#canvas-deck'))")
            assert page.evaluate("activeCanvasSize()") == {"width": width, "height": height, "ratio": width / height}

            bounds = page.locator("#canvas-deck").evaluate("""(f, size) => {
                f.style.setProperty('width', `${size[0] * 1.2}px`, 'important');
                f.style.setProperty('height', `${size[1] * 1.2}px`, 'important');
                f.contentWindow.dispatchEvent(new Event('resize'));
                return f.contentDocument.querySelector('.slide.active').getBoundingClientRect().toJSON();
            }""", [width, height])
            assert bounds["left"] == pytest.approx(0, abs=1)
            assert bounds["top"] == pytest.approx(0, abs=1)
            assert bounds["width"] == pytest.approx(width * 1.2, abs=1)
            assert bounds["height"] == pytest.approx(height * 1.2, abs=1)

            legacy = '<html><body><main id="deck"><div class="slide-shell" data-slide="1"><iframe></iframe></div></main></body></html>'
            page.locator("#canvas-deck").evaluate("(f, html) => f.srcdoc = html", legacy)
            page.wait_for_function("!!document.querySelector('#canvas-deck').contentDocument.querySelector('.slide-shell')")
            page.evaluate("fitDeckViewport(document.querySelector('#canvas-deck'))")
            assert page.locator("#canvas-deck").evaluate("f => !f.contentDocument.querySelector('#studio-static-inline-page-guard')")
            assert page.evaluate("activeCanvasSize()") == {"width": width, "height": height, "ratio": width / height}
            assert not errors
        finally:
            browser.close()
