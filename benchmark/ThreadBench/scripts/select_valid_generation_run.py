#!/usr/bin/env python3
"""Print the newest complete visual generation run for one case/model pair."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from generation.generation_run_validation import run_is_valid, valid_run


def benchmark_passed(run_dir: Path) -> bool:
    """Return true only for a run whose recorded generation attempt succeeded."""
    metadata_path = run_dir / "generation_metadata.json"
    recovery_path = run_dir / "recovery.json"
    try:
        if metadata_path.is_file():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            success = metadata.get("success")
            return bool(
                isinstance(success, dict)
                and success.get("artifact_contract_satisfied") is True
            ) or metadata.get("generation_contract_satisfied") is True
        if recovery_path.is_file():
            recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
            return recovery.get("benchmark_pass") is True
    except (OSError, json.JSONDecodeError):
        return False
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-dir", required=True, type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--require-benchmark-pass",
        action="store_true",
        help="Exclude recovered/failed attempts even when visual artifacts are complete.",
    )
    args = parser.parse_args()
    case_dir = args.case_dir.resolve()
    if args.require_benchmark_pass:
        model_dir = case_dir / "outputs" / args.model
        candidates = sorted(
            (path for path in model_dir.iterdir() if path.is_dir() and not path.name.startswith(".")),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        ) if model_dir.is_dir() else []
        run = next(
            (
                path
                for path in candidates
                if benchmark_passed(path) and run_is_valid(case_dir, path)
            ),
            None,
        )
    else:
        run = valid_run(case_dir, args.model)
    if run is None:
        parser.error(
            f"no complete visual generation run found for {args.case_dir} / {args.model}"
        )
    print(run.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
