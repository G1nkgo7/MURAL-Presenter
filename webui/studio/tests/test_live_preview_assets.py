"""实时片段内联后仍从本 Deck 的 files/ 路由加载素材，不改作者文件。"""
import os
import re
from urllib.parse import urljoin, urlsplit

from PIL import Image
import pytest

from app import main
from test_delivery_download import download_case, request


@pytest.fixture(params=[False, True], ids=["fragments", "hybrid"])
def live_case(download_case, request):
    root = download_case["root"]
    (root / "present.html").unlink()
    (root / "slides").mkdir()
    (root / "assets").mkdir()
    for name in ("hero", "texture", "shared"):
        Image.new("RGB", (32, 16), "orange").save(root / "assets" / f"{name}.png")
    (root / "base.css").write_text(
        ":root{--canvas-w:320px;--canvas-h:180px}"
        ".deck{position:absolute;width:320px;height:180px}"
        ".slide{display:none;width:320px;height:180px}.slide.active{display:block}"
        ".tile,.shared{width:32px;height:16px}"
        ".shared{background-image:url(assets/shared.png)}"
    )
    (root / "slides/slide_01.html").write_text(
        '<link rel="stylesheet" href="../base.css">'
        '<section class="slide" data-slide="01">'
        '<img id="hero" src="../assets/hero.png" alt="施工示意">'
        '<div class="tile" style="background-image:url(\'../assets/texture.png\')"></div>'
        '<div class="shared"></div></section>'
    )
    if request.param:
        (root / "slides/slide_02.html").write_text(
            '<!doctype html><html><body><img id="standalone" '
            'src="../assets/hero.png"></body></html>'
        )
    download_case["con"].execute("UPDATE decks SET status = 'running'")
    return {**download_case, "hybrid": request.param}


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_live_http_preview_rebases_images_and_css_without_changing_workspace(live_case):
    before = snapshot(live_case["root"])
    status, headers, body = request("/api/decks/1/files/present.html")
    assert status == 200 and headers[b"x-sensenova-preview"] == b"provisional"
    html = body.decode()
    source = re.search(r'<img id="hero" src="([^"]+)"', html).group(1)
    resolved = urljoin("/api/decks/1/files/present.html", source)
    assert resolved == "/api/decks/1/files/assets/hero.png"
    assert request(resolved)[0] == 200
    assert "url('assets/texture.png')" in html
    assert "url(assets/shared.png)" in html
    assert "../base.css" not in html
    if live_case["hybrid"]:
        assert 'src="slides/slide_02.html"' in html
        standalone = request("/api/decks/1/files/slides/slide_02.html")[2]
        assert b'src="../assets/hero.png"' in standalone
    assert snapshot(live_case["root"]) == before


@pytest.mark.parametrize("reference", [
    "assets/hero.png", "https://example.test/image.png", "//example.test/image.png",
    "data:image/png;base64,AA==", "blob:http://example.test/image", "#symbol",
    "/api/decks/1/files/assets/hero.png", "../_state/private.json",
])
def test_preview_preserves_non_fragment_resource_references(reference):
    fragment = f'<img src="{reference}"><div style="background:url(\'{reference}\')"></div>'
    html = main._inline_fragment_present_html("", [(1, fragment)], 320, 180)
    assert fragment in html


def test_preview_rebases_resource_attributes_but_keeps_private_files_forbidden(live_case):
    fragment = '<video poster="../assets/hero.png"></video><svg><use href="../assets/icons.svg#mark"/></svg>'
    html = main._inline_fragment_present_html("", [(1, fragment)], 320, 180)
    assert 'poster="assets/hero.png"' in html
    assert 'href="assets/icons.svg#mark"' in html
    assert request("/api/decks/1/files/_state/workspace.json")[0] in {403, 404}


def test_real_chromium_loads_live_images_backgrounds_and_standalone_page(live_case, monkeypatch):
    from playwright.sync_api import sync_playwright

    root = live_case["root"]
    before = snapshot(root)
    responses = {path: request(path) for path in [
        "/api/decks/1/files/present.html",
        "/api/decks/1/files/assets/hero.png",
        "/api/decks/1/files/assets/texture.png",
        "/api/decks/1/files/assets/shared.png",
    ]}
    if live_case["hybrid"]:
        path = "/api/decks/1/files/slides/slide_02.html"
        responses[path] = request(path)
    assert all(value[0] == 200 for value in responses.values())
    browser_tmp = root.parent / (root.name + "-browser")
    browser_tmp.mkdir()
    for key in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(key, str(browser_tmp))
    env = main.engine.render_env(dict(os.environ))
    executable = env.get("PPT_SKILL_BROWSER_EXE")
    assert executable, "formal Chromium runtime is required for this regression"
    requested, missing = set(), []

    def serve(route):
        path = urlsplit(route.request.url).path
        requested.add(path)
        if path not in responses:
            missing.append(path)
            route.fulfill(status=404, body="not found")
            return
        status, headers, body = responses[path]
        route.fulfill(status=status, content_type=headers[b"content-type"].decode(), body=body)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=executable, env=env,
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            page = browser.new_page(viewport={"width": 320, "height": 180})
            page.route("**/*", serve)
            page.goto("http://preview.test/api/decks/1/files/present.html", wait_until="networkidle")
            assert page.locator("#hero").evaluate("img => [img.naturalWidth,img.naturalHeight]") == [32, 16]
            assert "/api/decks/1/files/assets/texture.png" in requested
            assert "/api/decks/1/files/assets/shared.png" in requested
            if live_case["hybrid"]:
                page.evaluate("window.cleanDeck.go(2)")
                assert page.frame_locator("iframe").locator("#standalone").evaluate(
                    "img => [img.naturalWidth,img.naturalHeight]") == [32, 16]
            assert not missing
        finally:
            browser.close()
    assert snapshot(root) == before
