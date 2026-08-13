from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pymupdf

HARNESS = Path(__file__).resolve().parents[1]
REPO = HARNESS.parents[1]
sys.path.insert(0, str(HARNESS))

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


def test_material_figure_accepts_subject_and_rejects_page_facsimile(tmp_path: Path) -> None:
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
    accepted = subprocess.run(
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
    assert accepted.returncode == 0, accepted.stderr
    assert (tmp_path / "assets/figure.png").is_file()

    rejected = subprocess.run(
        [
            sys.executable,
            str(script),
            "material-figure",
            str(tmp_path),
            "--source",
            "inputs/paper.pdf.pages/page_001.png",
            "--output",
            "assets/page.png",
            "--box",
            "0.01,0.01,0.99,0.99",
        ],
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert "page facsimile" in rejected.stderr
