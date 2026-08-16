#!/usr/bin/env python3
"""Audit exported Long-Horizon Presenter artifacts and write generation metadata.

The source Presenter run is immutable. The exported copy may have undergone
the narrowly bounded adaptation described by artifact_adaptation.json; this
helper records native and final artifact success separately.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


SLIDE_RE = re.compile(r"slide_([0-9]{2})\.html$")
RENDER_RE = re.compile(r"slide_([0-9]{2})\.png$")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
RUNTIME_SETTING_NAMES = {
    "MAX_TURNS",
    "MAX_TOKENS",
    "SUBAGENT_MAX_TOKENS",
    "THINKING",
    "THINK_EFFORT",
    "MAX_CONCURRENT_CHILDREN",
    "SLIDE_CONCURRENCY",
    "TURN_TOOL_PARALLEL",
    "SLIDE_MAX_TURNS_BASE",
    "SLIDE_MAX_TURNS_CAP",
    "MAX_VISION_EDGE",
    "POOL_MAX_WORKERS",
    "RENDER_GLOBAL_LIMIT",
    "RENDER_LOCK_DIR",
}


def nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)) and value >= 0:
        return int(value)
    return 0


def collect_token_usage(
    run_dir: Path,
    manifest_record: dict[str, Any] | None = None,
    manifest_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    records = manifest_records or (
        [manifest_record] if isinstance(manifest_record, dict) else []
    )
    if len(records) > 1:
        input_tokens = sum(nonnegative_int(item.get("input_tokens")) for item in records)
        output_tokens = sum(
            nonnegative_int(item.get("output_tokens")) for item in records
        )
        model_calls = sum(nonnegative_int(item.get("model_calls")) for item in records)
        return {
            "available": input_tokens > 0 or output_tokens > 0 or model_calls > 0,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "uncached_input_tokens": input_tokens,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "turns": model_calls,
            "model_calls": model_calls,
            "usage_files": 0,
            "attempt_records": len(records),
            "source": "manifest_records",
        }

    totals = {
        "uncached_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "output_tokens": 0,
        "turns": 0,
    }
    usage_files = 0
    trace_dir = run_dir / "_trace"
    paths = sorted(trace_dir.glob("**/usage.json")) if trace_dir.is_dir() else []
    for path in paths:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        usage_files += 1
        totals["uncached_input_tokens"] += nonnegative_int(value.get("sum_input"))
        totals["cache_read_input_tokens"] += nonnegative_int(value.get("sum_cache_read"))
        totals["cache_creation_input_tokens"] += nonnegative_int(value.get("sum_cache_create"))
        totals["output_tokens"] += nonnegative_int(value.get("sum_output"))
        totals["turns"] += nonnegative_int(value.get("n_turns"))
    input_tokens = (
        totals["uncached_input_tokens"]
        + totals["cache_read_input_tokens"]
        + totals["cache_creation_input_tokens"]
    )
    if usage_files == 0 and isinstance(manifest_record, dict):
        input_tokens = nonnegative_int(manifest_record.get("input_tokens"))
        output_tokens = nonnegative_int(manifest_record.get("output_tokens"))
        model_calls = nonnegative_int(manifest_record.get("model_calls"))
        return {
            "available": input_tokens > 0 or output_tokens > 0 or model_calls > 0,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "uncached_input_tokens": input_tokens,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "turns": model_calls,
            "model_calls": model_calls,
            "usage_files": 0,
            "source": "manifest_record",
        }
    return {
        "available": usage_files > 0,
        "input_tokens": input_tokens,
        "output_tokens": totals["output_tokens"],
        "total_tokens": input_tokens + totals["output_tokens"],
        **totals,
        "usage_files": usage_files,
        "source": "trace_usage_files",
    }


def collect_tool_calls(run_dir: Path) -> dict[str, Any]:
    """Count native trace tool-call records without counting tool results."""
    by_name: dict[str, int] = {}
    seen_call_ids: set[str] = set()
    total_calls = 0
    unnamed_calls = 0
    message_files = 0
    invalid_message_files = 0
    trace_dir = run_dir / "_trace"
    paths = sorted(trace_dir.glob("**/messages.json")) if trace_dir.is_dir() else []

    def add_call(value: dict[str, Any]) -> None:
        nonlocal total_calls, unnamed_calls
        call_id = value.get("id")
        if isinstance(call_id, str) and call_id:
            if call_id in seen_call_ids:
                return
            seen_call_ids.add(call_id)
        function = value.get("function")
        name = value.get("name")
        if not isinstance(name, str) and isinstance(function, dict):
            name = function.get("name")
        total_calls += 1
        if isinstance(name, str) and name:
            by_name[name] = by_name.get(name, 0) + 1
        else:
            unnamed_calls += 1

    for path in paths:
        try:
            messages = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            invalid_message_files += 1
            continue
        if not isinstance(messages, list):
            invalid_message_files += 1
            continue
        message_files += 1
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") in {
                        "tool_use",
                        "tool_call",
                        "function_call",
                    }:
                        add_call(block)
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                for call in tool_calls:
                    if isinstance(call, dict):
                        add_call(call)

    return {
        "available": message_files > 0,
        "total_calls": total_calls,
        "by_name": dict(sorted(by_name.items())),
        "unnamed_calls": unnamed_calls,
        "message_files": message_files,
        "invalid_message_files": invalid_message_files,
    }


def load_manifest_records(manifest: Path, run_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SystemExit(f"cannot read pipeline manifest: {manifest}: {exc}") from exc
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(
                f"invalid pipeline manifest JSON at line {line_number}: {exc}"
            ) from exc
        if isinstance(value, dict):
            records.append(value)
    matches: list[dict[str, Any]] = []
    for item in records:
        candidate = item.get("run_dir")
        if not isinstance(candidate, str) or not candidate:
            continue
        if Path(candidate).resolve() == run_dir:
            matches.append(item)
    if matches:
        return matches
    if len(records) == 1:
        return records
    raise SystemExit(f"pipeline manifest has no unambiguous record for {run_dir}")


def parse_runtime_settings(values: list[str]) -> dict[str, str]:
    settings: dict[str, str] = {}
    for value in values:
        name, separator, configured = value.partition("=")
        if not separator or name not in RUNTIME_SETTING_NAMES or not configured:
            raise SystemExit(f"invalid --runtime-setting: {value!r}")
        if name in settings:
            raise SystemExit(f"duplicate --runtime-setting: {name}")
        settings[name] = configured
    return dict(sorted(settings.items()))


def indexed_files(
    directory: Path, pattern: re.Pattern[str]
) -> tuple[dict[int, Path], list[str]]:
    indexed: dict[int, Path] = {}
    noncanonical: list[str] = []
    if not directory.is_dir():
        return indexed, ["<missing-directory>"]
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        match = pattern.fullmatch(path.name)
        if match is None:
            if path.name.startswith("slide_"):
                noncanonical.append(path.name)
            continue
        index = int(match.group(1))
        if path.stat().st_size == 0 or index in indexed:
            noncanonical.append(path.name)
            continue
        indexed[index] = path
    return indexed, noncanonical


def audit_artifacts(output_dir: Path, dry_run: bool) -> dict[str, Any]:
    slides, noncanonical_slides = indexed_files(output_dir / "slides", SLIDE_RE)
    renders, noncanonical_renders = indexed_files(output_dir / "renders", RENDER_RE)
    indices = sorted(slides)
    missing_renders = sorted(set(slides) - set(renders))
    extra_renders = sorted(set(renders) - set(slides))
    violations: list[str] = []
    if not dry_run:
        if not indices:
            violations.append("no_canonical_slides")
        if indices != list(range(1, len(indices) + 1)):
            violations.append("nonconsecutive_slide_numbers")
        if noncanonical_slides:
            violations.append("noncanonical_slide_filenames")
        if noncanonical_renders:
            violations.append("noncanonical_render_filenames")
        if missing_renders:
            violations.append("missing_matching_renders")
        if extra_renders:
            violations.append("renders_without_matching_slides")
        for index, path in slides.items():
            prefix = path.read_text(encoding="utf-8", errors="ignore")[:4096].lower()
            if "<html" not in prefix and "<!doctype html" not in prefix:
                violations.append(f"invalid_html_{index:02d}")
        for index, path in renders.items():
            if path.stat().st_size <= len(PNG_SIGNATURE):
                violations.append(f"invalid_png_{index:02d}")
                continue
            with path.open("rb") as handle:
                if handle.read(len(PNG_SIGNATURE)) != PNG_SIGNATURE:
                    violations.append(f"invalid_png_{index:02d}")
    return {
        "artifact_contract_satisfied": None if dry_run else not violations,
        "actual_slide_count": len(indices),
        "slide_indices": indices,
        "missing_render_indices": missing_renders,
        "extra_render_indices": extra_renders,
        "noncanonical_slide_files": noncanonical_slides,
        "noncanonical_render_files": noncanonical_renders,
        "violations": violations,
    }


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def load_recovery_summary(output_dir: Path) -> dict[str, Any] | None:
    path = output_dir / "recovery.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid recovery metadata: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"recovery metadata must be a JSON object: {path}")
    return {
        "path": "recovery.json",
        "schema_version": value.get("schema_version"),
        "raw_status": value.get("raw_status"),
        "raw_reason": value.get("raw_reason"),
        "normalization_performed": value.get("normalization_performed"),
        "render_performed": value.get("render_performed"),
        "canvas_fit_performed": value.get("canvas_fit_performed"),
        "rendered_indices": value.get("rendered_indices"),
        "fitted_indices": value.get("fitted_indices"),
        "source_htmls": value.get("source_htmls"),
        "normalized_htmls": value.get("normalized_htmls"),
        "normalized_renders": value.get("normalized_renders"),
    }


def load_adaptation_summary(
    path: Path | None,
    output_dir: Path,
) -> dict[str, Any] | None:
    if path is None:
        return load_recovery_summary(output_dir)
    resolved = path.resolve()
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid artifact adaptation metadata: {resolved}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"artifact adaptation metadata must be a JSON object: {resolved}")
    if value.get("schema_version") != "limited_artifact_adaptation_v1":
        raise SystemExit(f"unsupported artifact adaptation schema: {resolved}")
    return {
        "path": str(resolved.relative_to(output_dir))
        if resolved.is_relative_to(output_dir)
        else str(resolved),
        **value,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--instruction", required=True, type=Path)
    parser.add_argument("--model-tag", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--backend", required=True)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--artifact-mode",
        default="read_only_native_output",
        choices=(
            "read_only_native_output",
            "limited_artifact_adaptation",
            "dry_run_copy",
        ),
    )
    parser.add_argument("--adaptation-record", type=Path)
    parser.add_argument("--pipeline-name", required=True)
    parser.add_argument("--pipeline-root", required=True)
    parser.add_argument("--pipeline-source-root", required=True)
    parser.add_argument("--pipeline-version", required=True)
    parser.add_argument("--skill-name", required=True)
    parser.add_argument("--skill-root", required=True)
    parser.add_argument("--skill-version", required=True)
    parser.add_argument("--presenter-source-sha256", required=True)
    parser.add_argument("--skill-source-sha256", required=True)
    parser.add_argument("--harness-source-sha256", required=True)
    parser.add_argument("--started-at", required=True)
    parser.add_argument("--finished-at", required=True)
    parser.add_argument("--duration-seconds", required=True, type=float)
    parser.add_argument("--generation-timeout-seconds", required=True, type=int)
    parser.add_argument("--presenter-max-attempts", required=True, type=int)
    parser.add_argument("--presenter-invocations", required=True, type=int)
    parser.add_argument(
        "--runtime-setting",
        action="append",
        default=[],
        help="Non-secret public Presenter environment setting in NAME=VALUE form.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    for name in (
        "presenter_source_sha256",
        "skill_source_sha256",
        "harness_source_sha256",
    ):
        if re.fullmatch(r"[0-9a-f]{64}", getattr(args, name)) is None:
            raise SystemExit(f"--{name.replace('_', '-')} must be a lowercase SHA-256 hex digest")

    run_dir = args.run_dir.resolve()
    output_dir = args.output_dir.resolve()
    instruction = args.instruction.resolve()
    manifest_records = load_manifest_records(args.manifest.resolve(), run_dir)
    manifest_record = manifest_records[-1]
    runtime_settings = parse_runtime_settings(args.runtime_setting)
    if args.presenter_max_attempts <= 0:
        raise SystemExit("--presenter-max-attempts must be positive")
    if args.presenter_invocations <= 0:
        raise SystemExit("--presenter-invocations must be positive")
    if args.presenter_invocations > args.presenter_max_attempts:
        raise SystemExit("Presenter invocations exceed the configured attempt limit")
    if len(manifest_records) > args.presenter_max_attempts:
        raise SystemExit("manifest attempts exceed the configured Presenter attempt limit")
    if len(manifest_records) > args.presenter_invocations:
        raise SystemExit("manifest attempt count exceeds Presenter invocation count")
    audit = audit_artifacts(output_dir, args.dry_run)
    adaptation = load_adaptation_summary(args.adaptation_record, output_dir)
    if adaptation is not None and args.adaptation_record is not None:
        if adaptation.get("mode") != args.artifact_mode:
            raise SystemExit(
                "artifact mode disagrees with artifact_adaptation.json: "
                f"{args.artifact_mode!r} != {adaptation.get('mode')!r}"
            )
        if adaptation.get("source_run_unchanged") is not True:
            raise SystemExit("artifact adaptation must leave the source run unchanged")
        if adaptation.get("canvas_fit_performed") is not False:
            raise SystemExit("canvas fitting is not allowed")
        if adaptation.get("truncated_indices") != []:
            raise SystemExit("page truncation is not allowed")
        if adaptation.get("filled_or_synthesized_indices") != []:
            raise SystemExit("page filling or synthesis is not allowed")
    upstream_completed = manifest_record.get("status") == "completed"
    artifact_ok = audit["artifact_contract_satisfied"]
    native_artifact_ok = (
        adaptation.get("native_artifact_contract_satisfied")
        if isinstance(adaptation, dict)
        and args.adaptation_record is not None
        else artifact_ok
    )
    if not args.dry_run and not isinstance(native_artifact_ok, bool):
        raise SystemExit(
            "artifact_adaptation.json must record native_artifact_contract_satisfied"
        )
    generation_success = (
        None if args.dry_run else bool(upstream_completed and artifact_ok is True)
    )
    native_generation_success = (
        None
        if args.dry_run
        else bool(upstream_completed and native_artifact_ok is True)
    )
    adaptation_performed = (
        None
        if args.dry_run
        else bool(
            isinstance(adaptation, dict)
            and adaptation.get("normalization_performed") is True
        )
    )
    if not args.dry_run and not upstream_completed:
        audit["violations"].append("upstream_status_not_completed")

    failed_upstream_attempts = sum(
        1 for item in manifest_records if item.get("status") != "completed"
    )
    generation_metrics = {
        "started_at": args.started_at,
        "finished_at": args.finished_at,
        "duration_seconds": round(args.duration_seconds, 3),
        "token_usage": collect_token_usage(
            run_dir,
            manifest_record,
            manifest_records,
        ),
        "tool_calls": {
            **collect_tool_calls(run_dir),
            "coverage": (
                "all_attempts"
                if len(manifest_records) == 1
                else "final_workspace_only"
            ),
        },
        "failure_statistics": {
            "sample_failed": (
                None if args.dry_run else generation_success is not True
            ),
            "upstream_attempts": len(manifest_records),
            "failed_upstream_attempts": failed_upstream_attempts,
            "upstream_attempt_failure_rate": round(
                failed_upstream_attempts / len(manifest_records),
                6,
            ),
        },
    }
    attempt_summaries = [
        {
            "attempt": index,
            "status": item.get("status"),
            "reason": item.get("reason"),
            "error": item.get("error"),
        }
        for index, item in enumerate(manifest_records, 1)
    ]
    completed_attempt = next(
        (
            item["attempt"]
            for item in attempt_summaries
            if item["status"] == "completed"
        ),
        None,
    )

    metadata = {
        "schema_version": "generation_metadata_v4",
        "case_id": args.case_id,
        "input": {
            "instruction": str(instruction),
            "instruction_sha256": hashlib.sha256(instruction.read_bytes()).hexdigest(),
            "query_passthrough": True,
            "benchmark_prompt_appended": False,
        },
        "generation": {
            "model_tag": args.model_tag,
            "model": args.model,
            "backend": args.backend,
            "batch": args.batch,
            "run_id": args.run_id,
            "dry_run": args.dry_run,
            "pipeline_name": args.pipeline_name,
            "pipeline_root": args.pipeline_root,
            "pipeline_source_root": args.pipeline_source_root,
            "pipeline_version": args.pipeline_version,
            "skill_name": args.skill_name,
            "skill_root": args.skill_root,
            "skill_version": args.skill_version,
            "presenter_source_sha256": args.presenter_source_sha256,
            "skill_source_sha256": args.skill_source_sha256,
            "harness_source_sha256": args.harness_source_sha256,
            "started_at": args.started_at,
            "finished_at": args.finished_at,
            "duration_seconds": round(args.duration_seconds, 3),
        },
        "runtime_policy": {
            "owner": "benchmark",
            "interface": "long_horizon_presenter_cli_and_environment",
            "protocol_version": "lh_presenter_local_v1",
            "reporting_mode": (
                "single_shot"
                if args.presenter_max_attempts == 1
                else "pass_at_n"
            ),
            "external_wall_clock_budget_seconds": args.generation_timeout_seconds,
            "cli": {
                "workers": 1,
                "limit": 1,
                "max_attempts": args.presenter_max_attempts,
            },
            "environment_overrides": runtime_settings,
            "presenter_dotenv_loaded": False,
        },
        "presentation": {
            "slide_count": audit["actual_slide_count"],
        },
        "generation_metrics": generation_metrics,
        "success": {
            "upstream_completed": upstream_completed,
            "artifact_contract_satisfied": artifact_ok,
            "generation_success": generation_success,
            "native_artifact_contract_satisfied": native_artifact_ok,
            "native_generation_success": native_generation_success,
            "adaptation_performed": adaptation_performed,
        },
        "upstream": {
            "run_dir": str(run_dir),
            "manifest": str(args.manifest.resolve()),
            "manifest_record": manifest_record,
            "manifest_records": manifest_records,
        },
        "artifact_audit": {
            "mode": args.artifact_mode,
            **audit,
        },
        "normalization": adaptation,
        "retry": {
            "owner": "long-horizon-presenter",
            "scope": "sample_deck",
            "page_retry_configured_by_benchmark": False,
            "max_attempts": args.presenter_max_attempts,
            "attempts_observed": len(manifest_records),
            "presenter_invocations": args.presenter_invocations,
            "resume_used": args.presenter_invocations > 1,
            "resume_without_new_attempt": (
                args.presenter_invocations > len(manifest_records)
            ),
            "completed_on_attempt": completed_attempt,
            "attempts": attempt_summaries,
            "interface": ["--max-attempts", "--resume"],
            "result_must_be_reported_as": (
                "single_shot"
                if args.presenter_max_attempts == 1
                else f"pass@{args.presenter_max_attempts}"
            ),
        },
        "generation_contract_satisfied": generation_success,
        "benchmark_eligibility": generation_success,
        "native_benchmark_eligibility": native_generation_success,
    }
    write_json(output_dir / "generation_metadata.json", metadata)

    print(f"actual_slide_count: {audit['actual_slide_count']}")
    print(f"upstream_completed: {str(upstream_completed).lower()}")
    if generation_success is None:
        print("generation_success: n/a")
    else:
        print(f"generation_success: {str(generation_success).lower()}")
        print(
            "native_generation_success: "
            f"{str(native_generation_success).lower()}"
        )
    if audit["violations"]:
        print("artifact_violations: " + ", ".join(audit["violations"]))
    return 0 if args.dry_run or generation_success else 1


if __name__ == "__main__":
    raise SystemExit(main())
