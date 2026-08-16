#!/usr/bin/env python3
"""Write one immutable generation-attempt record.

This helper intentionally does not decide whether a process should fail. It records
the wrapper exit code and independently audits any exported visual artifacts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from generation.generation_artifact_provenance import (
    explicitly_unmodified_artifacts,
)


SLIDE_RE = re.compile(r"slide_([0-9]{2})\.html$")
RENDER_RE = re.compile(r"slide_([0-9]{2})\.png$")


def load_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def indexed_files(directory: Path, pattern: re.Pattern[str]) -> dict[int, Path] | None:
    if not directory.is_dir():
        return None
    result: dict[int, Path] = {}
    for path in directory.iterdir():
        if not path.is_file() or not path.name.startswith("slide_"):
            continue
        match = pattern.fullmatch(path.name)
        if match is None or path.stat().st_size == 0:
            return None
        index = int(match.group(1))
        if index in result:
            return None
        result[index] = path
    return result


def audit_artifacts(output_dir: Path) -> tuple[bool, bool, list[str]]:
    violations: list[str] = []
    native_artifact_success = False
    metadata = load_object(output_dir / "generation_metadata.json")
    if metadata is not None:
        success = metadata.get("success")
        artifact_audit = metadata.get("artifact_audit")
        normalization = metadata.get("normalization")
        artifact_ok = (
            success.get("artifact_contract_satisfied")
            if isinstance(success, dict)
            else None
        )
        mode = artifact_audit.get("mode") if isinstance(artifact_audit, dict) else None
        if (
            metadata.get("generation_contract_satisfied") is not True
            or artifact_ok is not True
            or not isinstance(artifact_audit, dict)
            or mode not in {
                "read_only_native_output",
                "limited_artifact_adaptation",
            }
        ):
            violations.append("metadata_artifact_contract_unsatisfied")
        elif mode == "limited_artifact_adaptation" and (
            not isinstance(normalization, dict)
            or normalization.get("source_run_unchanged") is not True
            or normalization.get("canvas_fit_performed") is not False
            or normalization.get("truncated_indices") != []
            or normalization.get("filled_or_synthesized_indices") != []
        ):
            violations.append("adaptation_exceeds_allowed_scope")
        native_artifact_success = bool(
            isinstance(success, dict)
            and success.get("native_artifact_contract_satisfied") is True
            and success.get("native_generation_success") is True
        )
    else:
        # Historical runs may only have the legacy recovery sidecar.
        recovery = load_object(output_dir / "recovery.json")
        if recovery is None:
            violations.append("missing_or_invalid_generation_record")
        elif (
            recovery.get("benchmark_pass") is not True
            or recovery.get("raw_status") != "completed"
            or recovery.get("visual_artifact_available") is not True
            or not explicitly_unmodified_artifacts(recovery)
        ):
            violations.append("visual_artifact_unavailable")
        else:
            native_artifact_success = True

    slides = indexed_files(output_dir / "slides", SLIDE_RE)
    renders = indexed_files(output_dir / "renders", RENDER_RE)
    if slides is None:
        violations.append("invalid_slide_files")
    if renders is None:
        violations.append("invalid_render_files")
    if slides is None or renders is None:
        return False, native_artifact_success, violations

    indices = sorted(slides)
    if not indices:
        violations.append("no_canonical_slides")
    if set(slides) != set(renders):
        violations.append("unpaired_slide_and_render_indices")
    if indices != list(range(1, len(indices) + 1)):
        violations.append("nonconsecutive_slide_indices")

    for index in indices:
        html_prefix = slides[index].read_text(encoding="utf-8", errors="ignore")[:4096].lower()
        if "<html" not in html_prefix and "<!doctype html" not in html_prefix:
            violations.append(f"invalid_html_{index:02d}")
        render = renders.get(index)
        if render is None or render.stat().st_size <= 8:
            violations.append(f"invalid_png_{index:02d}")
        else:
            with render.open("rb") as handle:
                if handle.read(8) != b"\x89PNG\r\n\x1a\n":
                    violations.append(f"invalid_png_{index:02d}")
    return not violations, native_artifact_success, violations


def reserve_path(root: Path, run_id: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    safe_run_id = re.sub(r"[^A-Za-z0-9._-]", "_", run_id) or "invalid-run-id"
    candidate = root / f"{safe_run_id}.json"
    suffix = 1
    while candidate.exists():
        suffix += 1
        candidate = root / f"{safe_run_id}.duplicate-{suffix}.json"
    return candidate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempt-root", required=True, type=Path)
    parser.add_argument("--case-dir", required=True, type=Path)
    parser.add_argument("--model-tag", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--benchmark-attempt-limit",
        "--max-attempts",
        dest="benchmark_attempt_limit",
        required=True,
    )
    parser.add_argument("--presenter-max-attempts", required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--exit-code", required=True, type=int)
    parser.add_argument("--started-at", required=True)
    parser.add_argument("--started-epoch", required=True, type=int)
    parser.add_argument("--finished-at", required=True)
    parser.add_argument("--finished-epoch", required=True, type=int)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--dry-run", required=True)
    args = parser.parse_args()

    artifact_success: bool | None
    native_artifact_success: bool | None
    violations: list[str]
    dry_run = args.dry_run == "1"
    if dry_run:
        artifact_success, native_artifact_success, violations = None, None, []
    else:
        (
            artifact_success,
            native_artifact_success,
            violations,
        ) = audit_artifacts(args.output_dir)

    process_success = args.exit_code == 0
    generation_success = (
        None if dry_run else bool(process_success and artifact_success is True)
    )
    native_generation_success = (
        None
        if dry_run
        else bool(process_success and native_artifact_success is True)
    )
    if dry_run:
        state = "dry_run_completed" if process_success else "dry_run_failed"
    elif process_success and artifact_success:
        state = (
            "completed"
            if native_generation_success
            else "completed_with_limited_adaptation"
        )
    elif process_success:
        state = "completed_without_valid_artifact"
    else:
        state = "failed"

    record = {
        "schema_version": "generation_attempt_v1",
        "case_dir": str(args.case_dir.resolve()),
        "case_id": args.case_dir.name,
        "model_tag": args.model_tag,
        "run_id": args.run_id,
        "benchmark_attempt_limit": (
            int(args.benchmark_attempt_limit)
            if args.benchmark_attempt_limit.isdigit()
            and int(args.benchmark_attempt_limit) > 0
            else None
        ),
        "benchmark_attempt_limit_raw": args.benchmark_attempt_limit,
        "presenter_max_attempts": (
            int(args.presenter_max_attempts)
            if args.presenter_max_attempts.isdigit()
            and int(args.presenter_max_attempts) > 0
            else None
        ),
        "presenter_max_attempts_raw": args.presenter_max_attempts,
        "dry_run": dry_run,
        "state": state,
        "exit_code": args.exit_code,
        "process_success": process_success,
        "artifact_success": artifact_success,
        "generation_success": generation_success,
        "native_artifact_success": native_artifact_success,
        "native_generation_success": native_generation_success,
        "failure_stage": (
            None
            if state
            in {
                "completed",
                "completed_with_limited_adaptation",
                "dry_run_completed",
            }
            else args.stage
        ),
        "artifact_violations": violations,
        "started_at": args.started_at,
        "finished_at": args.finished_at,
        "duration_seconds": max(0, args.finished_epoch - args.started_epoch),
        "output_dir": str(args.output_dir.resolve()),
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    destination = reserve_path(args.attempt_root / args.model_tag, args.run_id)
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, destination)
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
