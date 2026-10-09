"""CSS 改造后保留资源 hash 和软修复预算回归。"""
from __future__ import annotations
import importlib
import hashlib
import json
from pathlib import Path
import pytest
from geometry_fixture import geometry, write_geometry
from state.workspace import WorkspaceState
from test_editable_pptx import browser_environment

@pytest.fixture
def skill_canvas(monkeypatch):
    scripts = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1/scripts"
    monkeypatch.syspath_prepend(str(scripts))
    return importlib.import_module("_internal.geometry")

def _author_workspace(root):
    return write_geometry(root, geometry(width_px=1600, height_px=900))

def _render_fixture(command, pixels):
    output = Path(command[5])
    (output / "slide_01.png").write_bytes(pixels)
    (output / "render.json").write_text(json.dumps({"pages": [{"page": 1, "geometry": {},
        "loaded_resources": [], "failed_resources": [], "input_hashes": {},
        "png_sha256": hashlib.sha256(pixels).hexdigest()}]}))

def test_scaffold_uses_author_css_geometry_not_markdown_tokens(
    tmp_path, skill_canvas, monkeypatch, browser_environment,
):
    core = importlib.import_module("_internal.deck_core")
    _shell_plans(tmp_path, core)
    plan = tmp_path / "deck.md"
    plan.write_text(plan.read_text() +
        '\n## Theme Tokens\n```css\n:root { --canvas-h: 1200px; --safe-x: 104px; }\n```\n')
    css = ":root {\n  --canvas-w: 1600px;\n  --canvas-h: var(--canvas-h);\n}\n"
    (tmp_path / "base.css").write_text(css)
    monkeypatch.setattr(core, "_ensure_authoring_font_bundle", lambda *_args, **_kwargs: {})
    with pytest.raises(ValueError, match="must define --canvas-w and --canvas-h"):
        core.scaffold_from_plans(tmp_path, 4)
    assert (tmp_path / "base.css").read_text() == css
    authored = css.replace("var(--canvas-h)", "calc(450px * 2)")
    (tmp_path / "base.css").write_text(authored)
    assert core.scaffold_from_plans(tmp_path, 4) == [1, 2, 3, 4]
    assert skill_canvas.read_geometry(tmp_path) == geometry(width_px=1600, height_px=900)
    assert (tmp_path / "base.css").read_text() == authored
    assert "--safe-x: 104px" not in authored

@pytest.mark.parametrize("relative", ["assets/photo.png", "_runtime/tmp/fonts/body.woff2"])
def test_render_digest_tracks_actual_local_asset_bytes(tmp_path, skill_canvas, relative):
    core = importlib.import_module("_internal.deck_core")
    (tmp_path / "config.json").write_text(json.dumps({"working_language": "en",
        "presentation_language": "en", "target_page_count": 1}))
    asset = tmp_path / relative
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(b"first content")
    css = f'@font-face {{ src: url("{relative}"); }}'
    before = core._render_input_digest(tmp_path, css, "<section>Same HTML</section>")
    assert before == core._render_input_digest(tmp_path, css, "<section>Same HTML</section>")
    asset.write_bytes(b"second content")
    assert before != core._render_input_digest(tmp_path, css, "<section>Same HTML</section>")

@pytest.mark.parametrize("attempt", ["slide-owner", "unscoped"])
def test_render_count_never_blocks_new_pixels(tmp_path, skill_canvas, monkeypatch, attempt):
    core = importlib.import_module("_internal.deck_core")
    WorkspaceState(tmp_path)
    _author_workspace(tmp_path)
    monkeypatch.setenv("CLEAN_SLIDE_HARD_BUDGET", "1")
    monkeypatch.setenv("MURAL_RENDER_ATTEMPT_ID", attempt)
    monkeypatch.setattr(core, "_load_plans", lambda *_, pages=None: ({"language": "en"}, [{"number": 1}]))
    monkeypatch.setattr(core, "_ensure_authoring_font_bundle", lambda *_, pages=None: {})
    captured = []
    def renderer(command):
        captured.append(Path(command[4]).read_text())
        _render_fixture(command, f"render-fixture-{len(captured)}".encode())
    monkeypatch.setattr(core, "_run_renderer", renderer)
    canvas = (tmp_path / "base.css").read_bytes()
    for revision in range(9):
        source = (f'<section class="slide" data-slide="01"><h1 style="font-family:Unknown Brush,serif">'
                  f'Version {revision}</h1></section>')
        (tmp_path / "slides/slide_01.html").write_text(source)
        assert core.render(tmp_path, 1, 1)
        assert (tmp_path / "slides/slide_01.html").read_text() == source
        assert (tmp_path / "renders/slide_01.png").read_bytes() == f"render-fixture-{revision + 1}".encode()
    assert len(captured) == 9
    assert (tmp_path / "base.css").read_bytes() == canvas
    state = json.loads((tmp_path / "_trace/slide-render-states/page_01.json").read_text())
    assert len(state["hashes"]) == 9 and state["authoring_target_mode"] == "soft"


def test_first_single_page_chart_render_prepares_dependency_before_hash_and_browser(tmp_path, skill_canvas, monkeypatch):
    core = importlib.import_module("_internal.deck_core")
    _author_workspace(tmp_path)
    monkeypatch.setattr(core, "_load_plans", lambda *_, pages=None: ({"language": "en"}, [{"number": 1}]))
    monkeypatch.setattr(core, "_ensure_authoring_font_bundle", lambda *_, pages=None: {})
    source = '<section class="slide" data-slide="01"><h1>Measured series</h1>' \
             '<script src="../assets/vendor/echarts.min.js"></script></section>'
    html = tmp_path / "slides/slide_01.html"
    html.write_text(source)
    library = tmp_path / "assets/vendor/echarts.min.js"
    assert not library.exists()
    seen = []
    digest = core._render_input_digest
    def hash_after_preparation(*args, **kwargs):
        assert library.is_file() and library.stat().st_size > 0
        seen.append("hash")
        return digest(*args, **kwargs)
    def capture(command):
        assert library.is_file() and library.stat().st_size > 0
        seen.append("browser")
        _render_fixture(command, b"test pixels")
    monkeypatch.setattr(core, "_render_input_digest", hash_after_preparation)
    monkeypatch.setattr(core, "_run_renderer", capture)
    assert core.render(tmp_path, 1, 1)
    assert seen == ["hash", "browser", "hash"]
    assert html.read_text() == source


def test_first_single_page_echarts_renders_real_pixels_without_eager_vendor(tmp_path, monkeypatch, browser_environment):
    from PIL import Image
    from runtime.shared_objects import workspace_skill_module
    _author_workspace(tmp_path)
    core = workspace_skill_module(tmp_path, "deck_core")
    monkeypatch.setattr(core, "_load_plans", lambda *_, pages=None: ({"language": "en"}, [{"number": 1}]))
    monkeypatch.setattr(core, "_ensure_authoring_font_bundle", lambda *_, pages=None: {})
    css = tmp_path / 'base.css'
    css.write_text(css.read_text() + '\nbody{margin:0}#deck{width:var(--canvas-w);height:var(--canvas-h)}\n')
    source = '''<section class="slide" data-slide="01">
      <h1>Actual chart</h1><div id="chart" style="width:600px;height:400px"></div>
      <script src="../assets/vendor/echarts.min.js"></script>
      <script>echarts.init(document.getElementById('chart')).setOption({animation:false,
        color:['#1284e8'],xAxis:{type:'category',data:['A','B','C']},yAxis:{type:'value'},
        series:[{type:'bar',data:[2,5,3]}]});</script></section>'''
    html = tmp_path / 'slides/slide_01.html'
    html.write_text(source)
    library = tmp_path / 'assets/vendor/echarts.min.js'
    assert not library.exists()
    renderer = core._run_renderer
    def checked_renderer(command):
        assert library.is_file() and library.stat().st_size > 0
        return renderer(command)
    monkeypatch.setattr(core, '_run_renderer', checked_renderer)
    assert core.render(tmp_path, 1, 1)
    with Image.open(tmp_path / 'renders/slide_01.png') as image:
        assert image.size == (1600, 900)
        assert sum(count for count, color in image.convert('RGB').getcolors(1600 * 900)
                   if color == (18, 132, 232)) > 1000
    assert html.read_text() == source


def _shell_plans(root, core):
    WorkspaceState(root)
    (root / "config.json").write_text(json.dumps({"working_language": "en",
        "presentation_language": "en", "target_page_count": 4}))
    (root / "briefs").mkdir(exist_ok=True)
    (root / "deck.md").write_text(
        "# Shared shell\n## Overall design\nPage-scoped composition; any footer is authored in HTML.\n")
    for number, intent in enumerate(("Open with a viewpoint", "Explain the evidence", "Introduce the next chapter", "Close with the next step"), 1):
        (root / f"briefs/slide_{number:02d}.md").write_text(
            f"# Page {number}\n## Page task\n{intent}.\n"
            "## Content and evidence\nKeep the source limitation.\n"
            "## Visual expression\nUse an authored arrangement.\n"
            "## Speaker notes\nPresent this page without adding claims.\n")
    return core._load_plans(root, 4)


def _filled_slide(core, page: int, *, ground: str = "") -> str:
    source = core._slide_skeleton(page).replace(
        "<!-- SLIDE_AGENT_FILL_START -->",
        "<!-- SLIDE_AGENT_FILL_START --><h1>Page body</h1>",
    )
    if ground:
        source = source.replace('class="slide"', f'class="slide ground-{ground}"', 1)
    return source


def test_shell_keeps_identity_and_empty_guard_without_layout_or_title_classification(tmp_path, skill_canvas):
    from bs4 import BeautifulSoup
    core = importlib.import_module("_internal.deck_core")
    deck, plans = _shell_plans(tmp_path, core)
    for plan in plans:
        assert not {"page_type", "special_layout", "composition", "show_footer"} & plan.keys()
        source = core._slide_skeleton(plan["number"])
        slide = BeautifulSoup(source, "html.parser").section
        assert slide["id"] == f'slide-{plan["number"]:02d}'
        assert slide["data-slide"] == f'{plan["number"]:02d}'
        assert slide["data-layout-contract"] == "page-owned"
        assert slide["class"] == ["slide"]
        assert len(slide.select(".page-stage[data-slide-owned=body]")) == 1
        assert not slide.select("[data-page-type], [data-frame], [data-slot], [data-layout-seed], [data-split-contract]")
        assert not slide.select("header, h1, h2, footer")
        assert not slide.get_text(strip=True)
        assert core._skeleton_is_unfilled(source)
        assert not core._skeleton_is_unfilled(source.replace(
            "<!-- SLIDE_AGENT_FILL_START -->", "<!-- SLIDE_AGENT_FILL_START --><p>Actual body</p>"))


def test_empty_body_receipt_is_specific_and_precedes_browser_work(tmp_path, skill_canvas, monkeypatch):
    core = importlib.import_module("_internal.deck_core")
    deck, plans = _shell_plans(tmp_path, core)
    source = core._slide_skeleton(plans[0]["number"]).replace(
        "</section>", '<div class="authored-body"><p>Existing author body</p></div></section>')
    path = tmp_path / "slides/slide_01.html"
    path.write_text(source)
    monkeypatch.setattr(core, "_read_author_geometry", lambda *_: pytest.fail("empty body launched geometry browser"))
    errors = []
    for operation in (lambda: core.render(tmp_path, 1, 4),
                      lambda: core._fragments(tmp_path, expected=4, only_page=1)):
        with pytest.raises(ValueError, match="^page_body_unfilled: slides/slide_01.html") as caught:
            operation()
        errors.append(str(caught.value))
        assert path.read_text() == source
    assert errors[0] == errors[1]
    assert "move that existing body" in errors[0]
    assert "does not mean the whole page is blank" in errors[0]
    repaired = source.replace('<!-- SLIDE_AGENT_FILL_START -->',
                              '<!-- SLIDE_AGENT_FILL_START --><p>Existing author body</p>').replace(
        '<div class="authored-body"><p>Existing author body</p></div>', '')
    path.write_text(repaired)
    assert core._fragments(tmp_path, expected=4, only_page=1)[2] == [(1, repaired.strip())]


def test_mixed_page_grounds_require_declared_ground_map(tmp_path, skill_canvas):
    core = importlib.import_module("_internal.deck_core")
    _shell_plans(tmp_path, core)
    (tmp_path / "slides").mkdir(exist_ok=True)
    for page, ground in enumerate(("dark", "light", "dark", "light"), 1):
        (tmp_path / f"slides/slide_{page:02d}.html").write_text(_filled_slide(core, page, ground=ground))
    with pytest.raises(ValueError, match="(?s)ground_contract_violation:.*mixed page ground tokens"):
        core._fragments(tmp_path, expected=4)

    (tmp_path / "deck.md").write_text((tmp_path / "deck.md").read_text() + "\nbase_ground: dark\nground_map: 1=dark,2=light,3=dark,4=light\n")
    assert [page for page, _ in core._fragments(tmp_path, expected=4)[2]] == [1, 2, 3, 4]


def test_deck_defined_semantic_ground_roles_are_not_limited_to_color_presets(tmp_path, skill_canvas):
    core = importlib.import_module("_internal.deck_core")
    _shell_plans(tmp_path, core)
    (tmp_path / "slides").mkdir(exist_ok=True)
    roles = ("primary-field", "primary-field", "evidence-focus", "primary-field")
    for page, role in enumerate(roles, 1):
        source = _filled_slide(core, page).replace(
            'class="slide"', f'class="slide ground-{role}"', 1)
        (tmp_path / f"slides/slide_{page:02d}.html").write_text(source)
    (tmp_path / "deck.md").write_text(
        (tmp_path / "deck.md").read_text()
        + "\nbase_ground: primary-field\n"
        + "ground_map: P3=evidence-focus\n"
    )
    assert [page for page, _ in core._fragments(tmp_path, expected=4)[2]] == [1, 2, 3, 4]


def test_page_local_theme_token_override_is_rejected_before_render(tmp_path, skill_canvas):
    core = importlib.import_module("_internal.deck_core")
    _shell_plans(tmp_path, core)
    (tmp_path / "slides").mkdir(exist_ok=True)
    source = _filled_slide(core, 1) + "\n<style>#slide-01 { --bg: #fff; background: #fff; }</style>"
    (tmp_path / "slides/slide_01.html").write_text(source)
    with pytest.raises(ValueError, match="(?s)ground_contract_violation:.*--bg"):
        core._fragments(tmp_path, expected=4, only_page=1)


@pytest.mark.parametrize("arguments", [["render", "--page", "1"],
    ["render-group", "--group", "opening", "--pages", "1"]])
def test_real_cli_empty_body_reaches_harness_with_stable_code(tmp_path, skill_canvas, arguments):
    import os
    import sys
    from runtime.production import _run_command
    from runtime.tools import ToolExecutionError
    core = importlib.import_module("_internal.deck_core")
    deck, plans = _shell_plans(tmp_path, core)
    source = core._slide_skeleton(plans[0]["number"]).replace(
        "</section>", '<div class="authored-body">Existing body</div></section>')
    path = tmp_path / "slides/slide_01.html"
    path.write_text(source)
    script = Path(core.__file__).resolve().parents[1] / "slide.py"
    # 无 base.css 的隔离工作区：若错误检查晚于几何读取，这里不会收到目标错误码。
    assert not (tmp_path / "base.css").exists()
    with pytest.raises(ToolExecutionError) as caught:
        _run_command([sys.executable, str(script), arguments[0], str(tmp_path), *arguments[1:]],
            cwd=tmp_path, timeout_s=15,
            environment={**os.environ, "TMPDIR": str(tmp_path), "TMP": str(tmp_path), "TEMP": str(tmp_path)})
    assert caught.value.code == "page_body_unfilled"
    assert "slides/slide_01.html" in str(caught.value)
    assert "move that existing body" in str(caught.value)
    assert caught.value.diagnostics["returncode"] == 2
    assert path.read_text() == source
    assert not (tmp_path / "renders/slide_01.png").exists()


@pytest.mark.parametrize("edit", ["outside_body", "title", "local_style", "unchanged"])
def test_force_scaffold_only_replaces_unchanged_shell(tmp_path, skill_canvas, monkeypatch, edit):
    core = importlib.import_module("_internal.deck_core")
    deck, plans = _shell_plans(tmp_path, core)
    source = core._slide_skeleton(plans[0]["number"])
    if edit == "outside_body":
        source = source.replace("</section>", '<div class="authored-body">Authored body</div></section>')
    elif edit == "title":
        source = source.replace("</section>", '<h1>Authored title</h1></section>')
    elif edit == "local_style":
        source += '<style>#slide-01 { background: #cda; }</style>'
    path = tmp_path / "slides/slide_01.html"
    path.write_text(source)
    assert core._skeleton_is_unfilled(source)
    for name in ("_read_author_geometry", "_ensure_authoring_runtime_dependencies", "_ensure_authoring_font_bundle"):
        monkeypatch.setattr(core, name, lambda *_args, **_kwargs: None)
    if edit == "unchanged":
        assert core.scaffold_from_plans(tmp_path, 4, force=True) == [1, 2, 3, 4]
    else:
        monkeypatch.setattr(core, "_read_author_geometry", lambda *_: pytest.fail("unsafe force launched browser"))
        with pytest.raises(ValueError, match="refusing scaffold --force.*authored HTML: slide_01.html"):
            core.scaffold_from_plans(tmp_path, 4, force=True)
        assert not (tmp_path / "slides/slide_02.html").exists()
    assert path.read_text() == source


@pytest.mark.parametrize("size", [(1600, 900), (1600, 1200), (2245, 3179), (900, 1600), (1200, 1200), (1728, 1000)])
def test_shared_shell_geometry_navigation_media_and_local_layout(
    tmp_path, skill_canvas, browser_environment, size,
):
    from playwright.sync_api import sync_playwright
    core = importlib.import_module("_internal.deck_core")
    css = (Path(core.__file__).resolve().parents[2] / "assets/base.css").read_text()
    width, height = size
    css += f"\n:root{{--canvas-w:{width}px;--canvas-h:{height}px}}"
    deck, plans = _shell_plans(tmp_path, core)
    fragments = []
    for plan in plans:
        number = plan["number"]
        source = core._slide_skeleton(number).replace("<!-- SLIDE_AGENT_FILL_START -->",
            f'<!-- SLIDE_AGENT_FILL_START --><h1 class="page-title">{plan["title"]}</h1>'
            '<p>Authored body</p><footer class="page-footer">Authored navigation</footer>')
        source += (f'<style>#slide-{number:02d} .page-footer {{position:absolute;'
                   'left:var(--safe-x);right:var(--safe-x);bottom:var(--safe-y)}</style>')
        fragments.append((plan["number"], source))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=browser_environment["PPT_SKILL_BROWSER_EXE"],
            headless=True, args=["--no-sandbox"],
        )
        try:
            page = browser.new_page(viewport={"width": width, "height": height})
            page.set_content(core._document(css, fragments, "en"))
            assert skill_canvas.measure_geometry(page) == geometry(width_px=width, height_px=height)
            for number in range(1, 5):
                page.evaluate("n => window.cleanDeck.go(n)", number)
                facts = page.evaluate("""() => {
                  const slide = document.querySelector('.slide.active');
                  const rect = e => {const r=e.getBoundingClientRect(); return [r.x,r.y,r.width,r.height]};
                  const title = slide.querySelector('.page-title');
                  const body = slide.querySelector('[data-slide-owned=body]');
                  const footer = slide.querySelector('footer');
                  return {active:[...document.querySelectorAll('.slide')].filter(
                    e=>getComputedStyle(e).visibility==='visible' && +getComputedStyle(e).opacity>0).map(e=>e.dataset.slide),
                    title:getComputedStyle(title).fontSize, titleRect:rect(title), body:rect(body),
                    display:getComputedStyle(body).display,
                    footer:rect(footer), animations:document.getAnimations().length};
                }""")
                assert facts["active"] == [f"{number:02d}"]
                assert facts["title"] == "60px"
                assert facts["animations"] == 0
                assert facts["body"] == [0, 0, width, height]
                assert facts["display"] == "block"
                assert facts["titleRect"][:2] == [90, 60]
                assert facts["footer"][1] + facts["footer"][3] == pytest.approx(height - 60)
            page.keyboard.press("ArrowLeft")
            assert page.locator(".slide.active").get_attribute("data-slide") == "03"
            page.evaluate("window.cleanDeck.go(1)")
            page.add_style_tag(content="""#slide-01 .page-title {
              position:absolute;inset:auto 90px 120px 90px;text-align:right;font-size:88px;
            }""")
            assert page.locator("#slide-01 .page-title").evaluate("e=>getComputedStyle(e).fontSize") == "88px"
            assert page.locator("#slide-02 .page-title").evaluate("e=>getComputedStyle(e).fontSize") == "60px"
            before = page.locator("#slide-01 .page-title").bounding_box()
            page.locator("#slide-01 .page-background").evaluate("""e => {
              const figure=document.createElement('figure');figure.style.cssText='width:100%;height:100%';
              const img=document.createElement('img');
              img.src='data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" width="40" height="80"><rect width="40" height="80" fill="red"/></svg>';
              img.style.cssText='display:block;width:100%;height:100%;object-fit:cover';
              figure.append(img);e.append(figure);
            }""")
            assert page.locator("#slide-01 img").bounding_box() == {"x": 0, "y": 0, "width": width, "height": height}
            assert page.locator("#slide-01 figure").evaluate("e=>getComputedStyle(e).margin") == "0px"
            assert page.locator("#slide-01 .page-title").bounding_box() == before
            page.set_viewport_size({"width": width // 2, "height": height // 2})
            page.wait_for_function("document.getElementById('deck').getBoundingClientRect().width === innerWidth")
            assert skill_canvas.measure_geometry(page) == geometry(width_px=width, height_px=height)
        finally:
            browser.close()


def test_prepare_does_not_replace_existing_authored_css(tmp_path, skill_canvas):
    core = importlib.import_module("_internal.deck_core")
    WorkspaceState(tmp_path)
    css = ":root { --canvas-w: 1234px; --canvas-h: 987px; }\n.local-author-choice { color: #a45; }\n"
    (tmp_path / "base.css").write_text(css)
    core.prepare(tmp_path)
    assert (tmp_path / "base.css").read_text() == css
