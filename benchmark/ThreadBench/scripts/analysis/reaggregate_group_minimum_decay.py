#!/usr/bin/env python3
"""Derive group-decayed score results from an existing evaluation revision."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_ROOT))

from judge.defect_penalty_scoring import apply_group_minimum_decay
from judge.dimension_rubric_contracts import (
    SCORE_CONTRACT_VERSION,
    load_json,
    write_json,
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-dir", required=True, type=Path)
    parser.add_argument("--rubric-revision", required=True)
    parser.add_argument("--source-evaluation-revision", required=True)
    parser.add_argument("--evaluation-revision", required=True)
    parser.add_argument("--alpha", type=float, default=0.3)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 0 <= args.alpha <= 1:
        raise ValueError("--alpha must be in [0, 1]")
    source_root = (
        args.case_dir / "evaluations" / args.source_evaluation_revision
    ).resolve()
    target_root = (args.case_dir / "evaluations" / args.evaluation_revision).resolve()
    if not source_root.is_dir():
        raise FileNotFoundError(f"source evaluation does not exist: {source_root}")
    if target_root.exists():
        raise FileExistsError(f"target evaluation already exists: {target_root}")

    rubric_root = args.case_dir / "rubric_versions" / args.rubric_revision
    rubrics = [
        load_json(rubric_root / "final_case_rubric.json"),
        load_json(rubric_root / "intermediate_case_rubric.json"),
    ]
    score_paths = sorted(source_root.glob("*/*/*/score_result.json"))
    if not score_paths:
        raise ValueError(f"no score_result.json files under {source_root}")

    written: list[dict[str, Any]] = []
    for source_score_path in score_paths:
        relative_profile = source_score_path.parent.relative_to(source_root)
        target_profile = target_root / relative_profile
        target_profile.mkdir(parents=True, exist_ok=False)
        source = load_json(source_score_path)
        source_scores = source.get("scores")
        if not isinstance(source_scores, dict):
            raise ValueError(f"score result has no scores object: {source_score_path}")
        adjusted_scores = apply_group_minimum_decay(
            source_scores,
            rubrics,
            alpha=args.alpha,
            round_to=6,
        )
        derived = {
            **source,
            "contract_version": SCORE_CONTRACT_VERSION,
            "evaluation_revision": args.evaluation_revision,
            "scores": adjusted_scores,
            "derived_from_score_result": {
                "path": str(source_score_path),
                "sha256": sha256_file(source_score_path),
                "evaluation_revision": args.source_evaluation_revision,
                "judge_results_reused": True,
            },
            "score_postprocessing": {
                "mode": "minimum_dimension_multiplicative_decay",
                "alpha": args.alpha,
                "formula": "1 - alpha * (1 - G_min)",
                "combine": "once_per_large_dimension",
                "applied_to": ["Deck", "Page"],
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        }
        write_json(target_profile / "score_result.json", derived)

        source_run_config_path = source_score_path.with_name("run_config.json")
        if source_run_config_path.is_file():
            run_config = load_json(source_run_config_path)
            run_config["contract_version"] = "dimension_rubric_judge_run_v15"
            run_config["evaluation_revision"] = args.evaluation_revision
            run_config["group_minimum_decay"] = {
                "alpha": args.alpha,
                "formula": "1 - alpha * (1 - G_min)",
                "combine": "once_per_large_dimension",
                "applied_to": ["Deck", "Page"],
                "derived_from_evaluation_revision": (
                    args.source_evaluation_revision
                ),
            }
            run_config["reused_judge_evidence"] = {
                "source_directory": str(source_score_path.parent),
                "source_score_result_sha256": sha256_file(source_score_path),
            }
            write_json(target_profile / "run_config.json", run_config)

        written.append(
            {
                "generation_model": derived.get("generation_model"),
                "generation_run": derived.get("generation_run"),
                "score_result": str(target_profile / "score_result.json"),
            }
        )

    write_json(
        target_root / "reaggregation_manifest.json",
        {
            "contract_version": "group_minimum_decay_reaggregation_v1",
            "source_evaluation_revision": args.source_evaluation_revision,
            "evaluation_revision": args.evaluation_revision,
            "rubric_revision": args.rubric_revision,
            "alpha": args.alpha,
            "formula": "1 - alpha * (1 - G_min)",
            "applied_to": ["Deck", "Page"],
            "unchanged_judge_results_reused": True,
            "evidence_storage": "lightweight_manifest_references_source_tree",
            "count": len(written),
            "results": written,
        },
    )
    print(json.dumps({"count": len(written), "output": str(target_root)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
