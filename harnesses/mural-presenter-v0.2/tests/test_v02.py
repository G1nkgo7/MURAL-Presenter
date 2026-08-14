from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pymupdf

HARNESS = Path(__file__).resolve().parents[1]
REPO = HARNESS.parents[1]
sys.path.insert(0, str(HARNESS))

from core import tools  # noqa: E402
from core.agent_loop import _delegate_task, _finish_gap  # noqa: E402
from core.run_batch import _extract_material  # noqa: E402


def test_slide_group_is_rejected() -> None:
    class Parent:
        skill_language = "zh"
        skill_name = "mural-presenter-v0-2-zh"
        response_language = "zh"

    result = _delegate_task(
        Parent(),
        {"tasks": [{"goal": "SlideGroup hero [01,02]: build both pages"}]},
    )
    assert "只接受一页一个" in result


def test_finish_gap_nudges_only_incomplete_single_pages(tmp_path: Path) -> None:
    plan = tmp_path / "plan"
    plan.mkdir()
    (plan / "slide_01.md").write_text("# P1\n", encoding="utf-8")
    (plan / "slide_02.md").write_text("# P2\n", encoding="utf-8")

    class Agent:
        role = "orchestrator"
        skill_language = "zh"
        material_required = False
        research_required = False
        research_completed = True
        delegated_roles = {"slide"}
        child_outcomes = {
            "slide_01": {"ok": True},
            "slide_02": {"ok": False},
        }
        ws = str(tmp_path)

    gap = _finish_gap(Agent())
    assert "Slide 02:" in gap
    assert "Slide 01:" not in gap
    assert "不要合并为 SlideGroup" in gap


def test_pdf_ingestion_writes_complete_text_and_page_derivatives(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    source = inputs / "paper.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "Principal finding: attachment evidence is preserved.")
    document.save(source)
    document.close()

    companion = inputs / "paper.pdf.md"
    assert _extract_material(source, companion)
    text = companion.read_text(encoding="utf-8")
    assert "Principal finding" in text
    assert "text_mode: native" in text
    assert (inputs / "paper.pdf.pages/page_001.png").is_file()
    layout = json.loads(
        (inputs / "paper.pdf.pages/page_001.json").read_text(encoding="utf-8")
    )
    assert layout["text_mode"] == "native"
    assert layout["image_pixels"][0] >= 1400


def test_attachment_pixels_are_rejected_by_crop_and_catalog(tmp_path: Path) -> None:
    from PIL import Image

    page_dir = tmp_path / "inputs/paper.pdf.pages"
    page_dir.mkdir(parents=True)
    source = page_dir / "page_001.png"
    Image.new("RGB", (1600, 2000), "white").save(source)
    source.with_suffix(".json").write_text(
        json.dumps({"page_points": [800, 1000], "text_blocks": []}),
        encoding="utf-8",
    )
    script = (
        REPO
        / "skills/mural-presenter-v0.2/mural-presenter-v0-2-en/scripts/deck.py"
    )
    blocked_crop = subprocess.run(
        [
            sys.executable,
            str(script),
            "material-figure",
            str(tmp_path),
            "--source",
            "inputs/paper.pdf.pages/page_001.png",
            "--output",
            "assets/figure.png",
            "--box",
            "0.10,0.10,0.90,0.35",
        ],
        capture_output=True,
        text=True,
    )
    assert blocked_crop.returncode != 0
    assert "material-figure is disabled" in blocked_crop.stderr
    assert not (tmp_path / "assets/figure.png").exists()

    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "figure.png").write_bytes(source.read_bytes())
    (assets / "catalog.md").write_text(
        """# Asset catalog

## forbidden-attachment
- slides: 1
- kind: material
- path: assets/figure.png
- purpose: copied from attachment
""",
        encoding="utf-8",
    )
    blocked_catalog = subprocess.run(
        [sys.executable, str(script), "fetch-images", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert blocked_catalog.returncode != 0
    assert "kind must be one of" in blocked_catalog.stderr
    assert "generated" in blocked_catalog.stderr
    assert "real" in blocked_catalog.stderr


def test_image_role_cannot_read_or_view_attachment_pixels(tmp_path: Path) -> None:
    class ImageAgent:
        role = "image"

    read_result = tools.read_file(
        ImageAgent(),
        "inputs/paper.pdf.pages/page_001.json",
    )
    assert "Image 不读取原始附件" in read_result

    view_result = tools.vision_analyze(
        ImageAgent(),
        "inputs/paper.pdf.pages/page_001.png",
    )
    assert "Image 不查看或裁切附件像素" in view_result


def test_image_terminal_does_not_expose_material_figure(tmp_path: Path) -> None:
    script = (
        REPO
        / "skills/mural-presenter-v0.2/mural-presenter-v0-2-en/scripts/deck.py"
    )

    class ImageAgent:
        role = "image"
        ws = str(tmp_path)
        render_script = str(script)
        bash_relaxed = False

    result = tools.terminal(
        ImageAgent(),
        f"python {script} material-figure . --source inputs/page.png "
        "--output assets/figure.png --box 0.1,0.1,0.9,0.9",
    )
    assert "不允许的 deck.py 动作 `material-figure`" in result
