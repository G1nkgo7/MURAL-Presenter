"""画布只来自作者 CSS；工具与恢复不维护另一份选择状态。"""
from __future__ import annotations

import importlib
from pathlib import Path
import json
import re

from PIL import Image
import pytest

from geometry_fixture import geometry, write_geometry
from runtime.shared_objects import workspace_skill_module
from runtime.tools import STABLE_TOOL_SCHEMAS, ToolCall, ToolContext, ToolRuntime, register_tools
from state.artifacts import ArtifactStore, ArtifactStateError
from state.workspace import WorkspaceState, file_sha256
from test_editable_pptx import browser_environment
from test_v1_font_pipeline import pinned_font_sources, _pin_font_manifest

GEOMETRY_CASES = [
    pytest.param(1600, 900, id="wide-16-9"),
    pytest.param(1920, 1080, id="explicit-1920-1080-preserved"),
    pytest.param(1600, 1200, id="standard-4-3"),
    pytest.param(2245, 3179, id="a1-portrait-96dpi"),
    pytest.param(3179, 2245, id="a1-landscape-96dpi"),
    pytest.param(900, 1600, id="free-portrait"),
    pytest.param(1200, 1200, id="free-square"),
    pytest.param(1728, 1000, id="free-custom"),
]


def test_planning_starts_without_canvas_tool_or_file(tmp_path):
    workspace = WorkspaceState(tmp_path)
    runtime = ToolRuntime()
    register_tools(runtime, {name: lambda *_: {"ok": True} for name in STABLE_TOOL_SCHEMAS}, require_complete=False)
    assert "canvas_select" not in {tool["name"] for tool in runtime.advertised_tools("orchestrator")}
    result = runtime.execute(ToolContext(owner_id="orchestrator", role="orchestrator"),
        ToolCall("delegate-1", "delegate_submit", {"tasks": [{"task_id": "facts", "role": "research"}]}))
    assert result.ok
    assert not (tmp_path / "canvas.json").exists()
    assert "canvas" not in workspace.paths.paths
    from runtime.production import read_scopes_for_role, write_scopes_for_role
    assert any(scope.permits("base.css") for scope in read_scopes_for_role("orchestrator", skill_name="mural-presenter-v1"))
    assert any(scope.permits("base.css") for scope in write_scopes_for_role("orchestrator"))
    assert not any(scope.permits("base.css") for scope in write_scopes_for_role("slide", pages=[1]))
    assert not hasattr(runtime, "_canvas_selected")
    assert WorkspaceState(tmp_path).deck_id == workspace.deck_id


@pytest.mark.parametrize("width,height", GEOMETRY_CASES)
def test_actual_css_geometry_and_resume(tmp_path, width, height):
    expected = write_geometry(tmp_path, geometry(width_px=width, height_px=height))
    original = (tmp_path / "base.css").read_bytes()
    assert WorkspaceState(tmp_path).require_geometry() == expected
    assert WorkspaceState(tmp_path).require_geometry() == expected
    assert (tmp_path / "base.css").read_bytes() == original
    assert not (tmp_path / "canvas.json").exists()


def test_browser_resolves_cascade_calc_without_rewriting_css(tmp_path):
    write_geometry(tmp_path, geometry(width_px=1600, height_px=900))
    css = ":root{--unit:400px;--canvas-w:calc(var(--unit) * 4);--canvas-h:1200px}"
    (tmp_path / "base.css").write_text(css)
    assert WorkspaceState(tmp_path).require_geometry() == geometry(width_px=1600, height_px=1200)
    assert (tmp_path / "base.css").read_text() == css


@pytest.mark.parametrize("canvas", [
    ":root {--canvas-w:1600px;--canvas-h:900px}", "",
])
def test_geometry_reports_failed_stylesheet_without_ignoring_or_rewriting(
    tmp_path, browser_environment, canvas,
):
    write_geometry(tmp_path, geometry(width_px=1600, height_px=900))
    css = '@import url("https://fonts.example.invalid/font.css?private=never-echo");\n' + canvas
    (tmp_path / "base.css").write_text(css)
    with pytest.raises(ValueError, match="css_geometry_dependency_failed") as caught:
        WorkspaceState(tmp_path).require_geometry()
    assert "https://fonts.example.invalid/font.css" in str(caught.value)
    assert "never-echo" in str(caught.value)
    assert (tmp_path / "base.css").read_text() == css


def test_skill_prepare_only_copies_default_css(tmp_path, monkeypatch):
    WorkspaceState(tmp_path)
    scripts = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1/scripts"
    monkeypatch.syspath_prepend(str(scripts))
    core = importlib.import_module("_internal.deck_core")
    core.prepare(tmp_path)
    metrics = importlib.import_module("_internal.geometry").read_geometry(tmp_path)
    assert (metrics["width_px"], metrics["height_px"]) == (1600, 900)
    assert not (tmp_path / "canvas.json").exists()


@pytest.mark.parametrize("override", [
    "#slide-02 {width:1500px}", "#slide-02 {--canvas-h:1000px}",
    "#slide-02 {transform:scale(.9)}",
])
def test_browser_rejects_page_geometry_override(tmp_path, override, monkeypatch):
    from playwright.sync_api import sync_playwright
    write_geometry(tmp_path, geometry(width_px=1600, height_px=1200))
    module = workspace_skill_module(tmp_path, "geometry")
    css = (tmp_path / "base.css").read_text()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        try:
            page = browser.new_page()
            page.set_content('<style>' + css + override + '</style><section class="slide" id="slide-01" data-slide="01"></section><section class="slide" id="slide-02" data-slide="02"></section>')
            with pytest.raises(Exception, match="page 02"):
                module.measure_geometry(page)
        finally:
            browser.close()


def test_harness_rejects_png_not_matching_dom(tmp_path):
    expected = write_geometry(tmp_path, geometry(width_px=1600, height_px=1200))
    state = WorkspaceState(tmp_path)
    store = ArtifactStore(tmp_path, workspace_state=state)
    html = state.paths.page_html(1)
    html.write_text('<link rel="stylesheet" href="../base.css"><section class="slide" data-slide="01">Page</section>')
    state.mark_authored(1, html_sha256=file_sha256(html))
    def renderer(source, target, actual):
        Image.new("RGB", (1600, 900)).save(target)
        return {"geometry": actual}
    with pytest.raises(ArtifactStateError, match="page 1.*expected.*1600, 1200.*actual.*1600, 900"):
        store.render_pages([1], renderer)


def test_production_has_no_canvas_protocol():
    root = Path(__file__).resolve().parents[3]
    for folder in (root / "harnesses/mural-presenter-v1", root / "skills/mural-presenter-v1/scripts"):
        for path in folder.rglob("*.py"):
            if "tests" in path.parts:
                continue
            source = path.read_text()
            for token in ("canvas_select", "canvas_selected", "canvas.json", "normalize_canvas_spec", "freeze_canvas_spec"):
                assert token not in source, (path, token)


@pytest.mark.parametrize("width,height", GEOMETRY_CASES)
def test_three_real_browser_pages_render_inspect_and_export(tmp_path, monkeypatch, width, height, pinned_font_sources, browser_environment):
    from export.delivery import DeliveryExporter
    from qc.routing import IssueLedger
    from pptx import Presentation
    from pypdf import PdfReader
    import zipfile

    monkeypatch.setenv('MURAL_STUDIO_DATA_DIR', pinned_font_sources['studio_data_root'])
    expected = write_geometry(tmp_path, geometry(width_px=width, height_px=height))
    _pin_font_manifest(tmp_path, pinned_font_sources)
    core = workspace_skill_module(tmp_path, "deck_core")
    renderer_module = workspace_skill_module(tmp_path, "render_deck")
    monkeypatch.setenv("PPT_RENDER_FINAL_MS", "3000")
    css = (Path(core.__file__).resolve().parents[2] / "assets/base.css").read_text() + (
        f"\n:root{{--canvas-w:{width}px;--canvas-h:{height}px;--font-number:var(--font-body)}}\n"
    )
    (tmp_path / "base.css").write_text(css)
    (tmp_path / "deck.md").write_text("# Shell delivery\n## Page roles\n"
        "P1 opens the subject; P2 explains the evidence; P3 closes with the next step.\n")
    (tmp_path / "briefs").mkdir(exist_ok=True)
    for number, intent in enumerate(("Open the subject", "Explain the evidence", "Close with the next step"), 1):
        (tmp_path / f"briefs/slide_{number:02d}.md").write_text(
            f"# Page {number}\n## Page task\n{intent}.\n")
    core.scaffold_from_plans(tmp_path, 3)
    (tmp_path / "assets").mkdir(exist_ok=True)
    Image.new("RGB", (80, 120), (215, 231, 239)).save(tmp_path / "assets/shell-media.png")
    fragments = []
    for number in (1, 2, 3):
        source = (tmp_path / f"slides/slide_{number:02d}.html").read_text()
        assert '<h1' not in source and 'data-page-type' not in source
        source = source.replace('<!-- SLIDE_AGENT_FILL_START -->',
            f'<!-- SLIDE_AGENT_FILL_START --><h1 class="page-title">Page {number}</h1>'
            '<figure class="authored-media">'
            '<img src="assets/shell-media.png" style="display:block;width:100%;height:100%;object-fit:cover">'
            '</figure>', 1)
        inset = "60px 90px" if number == 2 else "0"
        source += (f'<style>#slide-{number:02d} .authored-media {{position:absolute;inset:{inset}}}'
            f'#slide-{number:02d} .page-title {{position:relative;z-index:1}}</style>')
        assert not core._skeleton_is_unfilled(source)
        fragments.append((number, source))
    css = (tmp_path / "base.css").read_text()
    (tmp_path / "present.html").write_text(core._document(css, fragments, "en"))
    state = WorkspaceState(tmp_path)
    store = ArtifactStore(tmp_path, workspace_state=state)
    store.declare(artifact_id="shell-media", kind="image", path="assets/shell-media.png", owner_id="image")
    store.commit(owner_id="image", artifact_ids=["shell-media"], source_hashes={})
    for number, fragment in fragments:
        path = state.paths.page_html(number)
        path.write_text(fragment)
        state.mark_authored(number, html_sha256=file_sha256(path))
    renderer = renderer_module.DeckRenderer(tmp_path / "present.html", tmp_path / "renders",
        workspace=tmp_path, motion_check=False)
    try:
        renderer.start()
        renderer.render_all()
        renderer.finalize()
        assert renderer.meta["canvas"] == expected
    finally:
        renderer.close()
    def receipt(source, *_):
        number = int(source.stem.split("_")[-1])
        measured = next(row for row in renderer.meta["pages"] if row["page"] == number)
        return {**measured, "geometry": renderer.meta["canvas"]}
    store.render_pages([1, 2, 3], receipt)
    for number in (1, 2, 3):
        with Image.open(state.paths.page_png(number)) as image:
            assert image.size == (width, height)
            corners = ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1))
            expected_corner = (244, 247, 249) if number == 2 else (215, 231, 239)
            assert all(image.convert("RGB").getpixel(point) == expected_corner for point in corners)
        store.record_inspection(number, inspection_id=f"fixture-pixel-inspection-{number}")
        state.mark_ready(number)
    exporter = DeliveryExporter(state, store, IssueLedger(tmp_path))
    manifest = exporter.export()
    assert manifest.canvas == expected
    deck = Presentation(tmp_path / "delivery/presentation.pptx")
    assert deck.slide_width / deck.slide_height == pytest.approx(width / height)
    assert deck.slide_width / 914400 == pytest.approx(width / 96)
    assert deck.slide_height / 914400 == pytest.approx(height / 96)
    pdf = PdfReader(tmp_path / "delivery/presentation.pdf")
    assert len(pdf.pages) == 3
    for number,page in enumerate(pdf.pages,1):
        assert f'Page {number}' in page.extract_text()
        assert float(page.mediabox.width / page.mediabox.height) == pytest.approx(width / height)
        assert float(page.mediabox.width) == pytest.approx(width * 72 / 96, abs=.1)
        assert float(page.mediabox.height) == pytest.approx(height * 72 / 96, abs=.1)
    with zipfile.ZipFile(tmp_path / "delivery/presentation.zip") as archive:
        assert "canvas.json" not in archive.namelist()
        assert "base.css" in archive.namelist()
    assert not (tmp_path / "canvas.json").exists()
    assert WorkspaceState(tmp_path).require_geometry() == expected
    assert (tmp_path / "base.css").read_text() == css
