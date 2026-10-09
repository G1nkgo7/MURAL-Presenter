"""裁图反馈绑定实际框；没有几何证据不能冒充图像质量通过。"""
import json
from pathlib import Path

from PIL import Image
import pytest

from runtime.attachments import AttachmentStager, materialize_page_image, resolve_attachment_records, resolve_page_assets
from state.artifacts import ArtifactStore
from state.workspace import WorkspaceState


@pytest.fixture
def core(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1/scripts"))
    from _internal import deck_core
    return deck_core


def analyze(core, layout, box=(0.1, 0.1, 0.9, 0.7)):
    return core._subject_crop_analysis(layout, box, crop_policy="subject-only", preserve_internal_labels=False)


def test_missing_layout_and_full_page_raster_are_not_clean_subject_evidence(core):
    empty = analyze(core, {})
    assert empty["layout_evidence"]["text"] == empty["layout_evidence"]["visual"] == "unavailable"
    scan = analyze(core, {"page_points": [1000, 1000], "image_blocks": [{"bbox_pdf": [0, 0, 1000, 1000]}]})
    assert scan["layout_evidence"]["visual"] == "unsegmented_raster"
    assert scan["visual_region_count"] == 0
    tiled = analyze(core, {"raster_page": True, "page_points": [1000, 1000],
        "image_blocks": [{"bbox_pdf": [0, 100, 1000, 350]}, {"bbox_pdf": [0, 350, 1000, 650]}]})
    assert tiled["layout_evidence"]["visual"] == "unsegmented_raster"
    assert tiled["visual_region_count"] == 0 and not tiled["clipped_visual_regions"]


def test_detected_subject_cut_in_half_warns_and_does_not_tighten(core):
    box = (0.1, 0.1, 0.9, 0.6)
    result = analyze(core, {"page_points": [1000, 1000],
        "image_blocks": [{"bbox_pdf": [200, 200, 800, 850]}]}, box)
    assert result["clipped_visual_regions"] == [[0.2, 0.2, 0.8, 0.85]]
    assert result["suggested_subject_box"] == list(box)


def test_edge_fragment_uses_actual_box_not_unapplied_tighten_suggestion(core, tmp_path, capsys):
    root = tmp_path / "workspace"
    WorkspaceState(root)
    page = root / "inputs/page_001.png"
    Image.new("RGB", (1500, 1500), "white").save(page)
    page.with_suffix(".json").write_text(json.dumps({"page_points": [1000, 1000],
        "source_path": "inputs/originals/material.pdf", "page": 7,
        "text_blocks": [{"bbox_pdf": [200, 680, 600, 740], "text": "All Colors", "chars": 10}],
        "image_blocks": [{"bbox_pdf": [200, 200, 800, 550]}]}))
    for tighten in (False, True):
        core.material_figure(root, "inputs/page_001.png", "assets/colors.png", "0.1,0.1,0.9,0.7",
                           crop_policy="subject-only", auto_tighten=tighten)
        result = json.loads(capsys.readouterr().out)
        assert ("All Colors" in result["edge_text_fragments"]) is not tighten
        assert bool(result["excluded_regions"]) is tighten
        assert result["quality_status"] == "unreviewed"
        assert result["requires_full_resolution_review"]
        receipt = json.loads((root / "_trace/material-figures.json").read_text())["assets"]["assets/colors.png"]
        assert receipt["source_page"] == 7
        assert receipt["source_document"] == "inputs/originals/material.pdf"
        assert receipt["edge_text_fragments"] == result["edge_text_fragments"]


def test_pdf_ingestion_to_crop_keeps_boundary_text_evidence(tmp_path, core, capsys):
    import pymupdf as fitz
    source = tmp_path / "source.pdf"
    with fitz.open() as doc:
        page = doc.new_page(width=600, height=800)
        page.insert_text((180, 490), "All Colors", fontsize=30)
        doc.save(source)
    root = tmp_path / "workspace"
    WorkspaceState(root)
    AttachmentStager(root).stage([source])
    page = resolve_page_assets(resolve_attachment_records(root)[0])[0]
    relative = page.relative_to(root).as_posix()
    materialize_page_image(root, relative)
    core.material_figure(root, relative, "assets/figure.png", "0.1,0.2,0.9,0.6", crop_policy="subject-only")
    result = json.loads(capsys.readouterr().out)
    assert result["edge_text_fragments"]
    assert result["layout_evidence"]["text"] == "native"
    assert result["quality_status"] == "unreviewed"


@pytest.mark.parametrize("equals", [False, True])
def test_image_terminal_materializes_v2_page_before_skill_runs(tmp_path, monkeypatch, equals):
    from types import SimpleNamespace
    from runtime import production
    from runtime.tools import ToolContext, WriteScope
    import pymupdf as fitz

    source = tmp_path / "source.pdf"
    with fitz.open() as doc:
        doc.new_page().insert_text((72, 72), "Figure")
        doc.save(source)
    workspace = WorkspaceState(tmp_path / "workspace")
    root = workspace.paths.root
    AttachmentStager(root).stage([source])
    page = resolve_page_assets(resolve_attachment_records(root)[0])[0]
    assert not page.exists()
    backend = production.ProductionToolServices.__new__(production.ProductionToolServices)
    backend.workspace = workspace
    backend.artifacts = ArtifactStore(root, workspace_state=workspace)
    backend.settings = SimpleNamespace(command_timeout_s=10)
    skill_root = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1"
    backend.snapshot = SimpleNamespace(name="mural-presenter-v1",
        target=skill_root, tree_sha256="test-command-contract",
        resolve_skill_path=lambda _logical: skill_root / "scripts/image.py")
    backend._command_environment = lambda: {}
    run_command = production._run_command
    def execute(*args, **kwargs):
        assert page.is_file()
        return run_command(*args, **kwargs)
    monkeypatch.setattr(production, "_run_command", execute)
    context = ToolContext(owner_id="image", role="image", read_scopes=production.read_scopes_for_role(
        "image", skill_name="mural-presenter-v1",
        attachment_paths=[record.original_path for record in resolve_attachment_records(root)]),
        write_scopes=(WriteScope("assets", recursive=True),))
    flag = "--source=" if equals else "--source "
    result = backend.terminal(context, {"command": "python skills/mural-presenter-v1/scripts/image.py crop-material . "
        + flag + page.relative_to(root).as_posix() + " --output assets/figure.png --box 0,0,1,1"})
    assert result["returncode"] == 0, result
    assert result["images"][0]["owner_id"] == "image"
    assert result["images"][0]["path"] == "assets/figure.png"
    receipt = json.loads(result["stdout"])
    assert receipt["quality_status"] == "unreviewed" and receipt["page_area"] == 1.0
    with Image.open(root / "assets/figure.png") as cropped, Image.open(page) as original:
        assert cropped.size == original.size


def test_shared_chart_track_matches_pixel_axis_for_all_values(tmp_path):
    """复现标签挤压各行绘图区，再验证同列宽方案的实际 DOM 与 PNG 端点。"""
    from playwright.sync_api import sync_playwright

    html = '''<style>
      body{margin:0;background:white;font:24px sans-serif}
      .chart{width:800px;display:grid;grid-template-columns:150px 500px 120px;gap:20px 15px;padding:50px}
      .track{height:40px;position:relative}.bar{height:100%;background:rgb(0,80,240)}
      .axis{height:2px;background:black}.axis-labels{position:relative;height:40px}
      .axis-labels span{position:absolute;transform:translateX(-50%)}
      .broken .track{display:flex}.broken .bar{flex-shrink:1}.broken .value{margin-left:14px}
    </style><div class="chart">
      <span>M3</span><div class="track"><div class="bar" style="width:95%"></div></div><span>1.9×</span>
      <span>M1</span><div class="track"><div class="bar" style="width:50%"></div></div><span>1.0×</span>
      <span></span><div><div class="axis"></div><div class="axis-labels"><span style="left:0">0</span><span style="left:50%">1.0</span><span style="left:100%">2.0×</span></div></div>
    </div>'''
    (tmp_path / "chart.html").write_text(html)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        try:
            page = browser.new_page(viewport={"width": 1000, "height": 400})
            page.set_content(html)
            axis = page.locator(".axis").bounding_box()
            for index in range(3):
                tick = page.locator(".axis-labels span").nth(index).bounding_box()
                assert tick["x"] + tick["width"] / 2 == pytest.approx(axis["x"] + axis["width"] * index / 2)
            page.screenshot(path=str(tmp_path / "chart.png"))
            with Image.open(tmp_path / "chart.png") as png:
                for index, value in enumerate((1.9, 1.0)):
                    box = page.locator(".bar").nth(index).bounding_box()
                    assert box["x"] == axis["x"]
                    assert 2 * box["width"] / axis["width"] == pytest.approx(value)
                    pixels = [x for x in range(png.width)
                              if png.getpixel((x, int(box["y"] + 20)))[:3] == (0, 80, 240)]
                    assert 2 * len(pixels) / axis["width"] == pytest.approx(value, abs=0.004)
            page.evaluate('''() => {
                document.querySelector('.chart').classList.add('broken');
                const value=document.createElement('span'); value.className='value'; value.textContent='1.9×';
                document.querySelector('.track').append(value);
            }''')
            assert page.locator('.bar').first.bounding_box()['width'] / axis['width'] != pytest.approx(.95)
        finally:
            browser.close()


def test_explicit_cutout_exclusion_changes_only_alpha(tmp_path, core, monkeypatch, capsys):
    root = tmp_path / "workspace"
    WorkspaceState(root)
    source = root / "inputs/page_001.png"
    Image.new("RGB", (1000, 1000), (20, 40, 60)).save(source)
    monkeypatch.setattr(core, "_alpha_cutout_subject", lambda crop: (crop.convert("RGBA"), {}))
    core.material_figure(root, "inputs/page_001.png", "assets/subject.png", "0.1,0.1,0.9,0.9",
        crop_policy="subject-only", alpha_cutout=True, exclude_boxes=["0.6,0.2,0.8,0.4"])
    result = json.loads(capsys.readouterr().out)
    assert result["alpha_exclusions"] == [[0.6, 0.2, 0.8, 0.4]]
    with Image.open(root / "assets/subject.png") as image:
        assert image.getpixel((600, 200)) == (20, 40, 60, 0)
        assert image.getpixel((200, 600)) == (20, 40, 60, 255)
    before = (root / "assets/subject.png").read_bytes()
    with pytest.raises(ValueError, match="inside the requested"):
        core.material_figure(root, "inputs/page_001.png", "assets/subject.png", "0.1,0.1,0.9,0.9",
            crop_policy="subject-only", alpha_cutout=True, exclude_boxes=["0,0,1,1"])
    assert (root / "assets/subject.png").read_bytes() == before
