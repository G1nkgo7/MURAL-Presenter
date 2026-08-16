#!/usr/bin/env python3
"""Run PresentBench's real parsing and scoring path without external API credentials.

This smoke intentionally replaces only the remote judge call. It still uses a real
downloaded PresentBench case, creates and parses a PDF, merges the case/domain
checklists, executes deterministic checklist functions, writes judge YAML, and runs
the official weighted scoring code.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pymupdf
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import judge  # noqa: E402


CASE_REL = Path("talk/middle_school_presentation/13")


class OfflineJudge:
    """Minimal JudgeAPI-compatible replacement for smoke testing."""

    def __init__(self) -> None:
        self.calls = 0

    def upload_file(self, file_path: str) -> str:
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(path)
        return str(path)

    def generate_content(self, **_: object) -> tuple[str, dict]:
        self.calls += 1
        return (
            "Offline smoke verdict: the transport adapter is intentionally mocked. "
            "\\boxed{yes}",
            {"offline_smoke": True},
        )


def _write_smoke_pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    pages = (
        ("China's Space Program", "A concise classroom presentation"),
        ("From unmanned missions to human spaceflight", "Milestones and context"),
        ("Discussion", "What makes a space program sustainable?"),
    )
    for title, subtitle in pages:
        page = doc.new_page(width=960, height=540)
        page.insert_text((72, 130), title, fontsize=30)
        page.insert_text((72, 190), subtitle, fontsize=18)
    doc.save(path)
    doc.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=REPO_ROOT / "smoke" / "results" / "offline-judge",
    )
    args = parser.parse_args()

    case_dir = REPO_ROOT / "data" / CASE_REL
    material = case_dir / "material.md"
    judge_prompt = case_dir / "generation_task" / "judge_prompt.json"
    common_prompt = REPO_ROOT / "data" / "talk" / "common_judge_prompt.json"
    weights = REPO_ROOT / "data" / "talk" / "judge_weights.yaml"
    required = (material, judge_prompt, common_prompt, weights)
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError("PresentBench data is incomplete: " + ", ".join(missing))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = args.output_root.resolve() / stamp
    slides = run_dir / "slides.pdf"
    result_yaml = run_dir / "offline-smoke.yaml"
    score_yaml = run_dir / "offline-smoke_score.yaml"
    _write_smoke_pdf(slides)

    offline_judge = OfflineJudge()
    judge_args = argparse.Namespace(
        api_type="gemini_inline",
        model="offline-smoke-judge",
        thinking_level=None,
        slides=str(slides),
        material=[str(material)],
        judge_prompt=str(judge_prompt),
        common_judge_prompt=str(common_prompt),
        weights_path=str(weights),
        output=str(result_yaml),
        output_dir=None,
        temperature=0.0,
        seed=7,
        retry=1,
        debug=True,
        zero_score=False,
        min_timestamp=None,
    )

    logging.basicConfig(level=logging.WARNING)
    with mock.patch.object(judge, "create_judge_api", return_value=offline_judge):
        judge.main(args=judge_args)

    if offline_judge.calls <= 0:
        raise RuntimeError("Smoke did not exercise any model-judged checklist item")
    if not result_yaml.is_file() or not score_yaml.is_file():
        raise RuntimeError("Judge or score artifact was not written")

    result = yaml.safe_load(result_yaml.read_text(encoding="utf-8"))
    score = yaml.safe_load(score_yaml.read_text(encoding="utf-8"))
    if not result.get("material_independent") or not result.get("material_dependent"):
        raise RuntimeError("Judge result is missing one of the two evaluation sections")

    summary = {
        "status": "passed",
        "mode": "offline_remote_adapter",
        "case": str(CASE_REL),
        "remote_judge_calls_mocked": offline_judge.calls,
        "slides": str(slides),
        "judge_result": str(result_yaml),
        "score_result": str(score_yaml),
        "score_total": score.get("total", {}),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
