#!/usr/bin/env python3
"""Audit bounded Slide-worker behavior and parallel overlap in existing traces."""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
from pathlib import Path
from typing import Any, Iterable


RUNS = [
    {
        "run_id": "skill_enum3_exclusion_0727_0330_adhoc",
        "batch": "skill_enum3_exclusion_0727_0330",
        "manifest": "skill_enum3_exclusion_0727_0330.manifest.jsonl",
    },
    {
        "run_id": "skill_enum6_0727_0305_adhoc",
        "batch": "skill_enum6_0727_0305",
        "manifest": "skill_enum6_0727_0305.manifest.jsonl",
    },
]

PEER_PATH_PATTERN = re.compile(r"(?:plan|slides|renders)/slide_(\d{2})")
WHOLE_DECK_TOKENS = (
    "plan/deck.md",
    "plan/pages.json",
    "deck.html",
    "present.html",
    "research/knowledge-brief.md",
    "research/research.md",
)
SHARED_WRITE_TOKENS = (
    "base.css",
    "speech.md",
    "plan/",
    "research/",
    "assets/catalog.md",
    "deck.html",
    "present.html",
)
WRITE_TOOLS = {"write_file", "write_files", "edit", "patch", "apply_patch"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pipeline-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def flatten_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from flatten_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from flatten_strings(item)


def load_manifest(path: Path, run_id: str) -> dict[str, Any]:
    for line in path.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        if item.get("sample_id") == run_id:
            return item
    raise RuntimeError(f"{run_id} not found in {path}")


def interval_peak(intervals: list[tuple[float, float]]) -> int:
    points: list[tuple[float, int]] = []
    for start, end in intervals:
        points.append((start, 1))
        points.append((end, -1))
    active = 0
    peak = 0
    for _, delta in sorted(points, key=lambda item: (item[0], item[1])):
        active += delta
        peak = max(peak, active)
    return peak


def event_intervals(events_path: Path) -> tuple[list[dict[str, Any]], float, float]:
    events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    by_label: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        if re.fullmatch(r"slide_\d{2}", event.get("label", "")):
            by_label.setdefault(event["label"], []).append(event)

    workers: list[dict[str, Any]] = []
    for label, rows in sorted(by_label.items()):
        start = min(float(row["epoch"]) for row in rows)
        completed = [
            row for row in rows if "轨迹已写" in str(row.get("message", ""))
        ]
        end = (
            max(float(row["epoch"]) for row in completed)
            if completed
            else max(float(row["epoch"]) for row in rows)
        )
        workers.append(
            {
                "worker": label,
                "page": int(label.split("_")[1]),
                "start_epoch": start,
                "end_epoch": end,
                "duration_seconds": round(end - start, 3),
                "completion_event_found": bool(completed),
            }
        )
    if not workers:
        raise RuntimeError(f"no Slide workers found in {events_path}")
    return workers, min(row["start_epoch"] for row in workers), max(
        row["end_epoch"] for row in workers
    )


def audit_worker_tools(trace_root: Path, worker: dict[str, Any]) -> dict[str, Any]:
    label = worker["worker"]
    own_page = int(worker["page"])
    log_path = trace_root / "subagents" / label / "tool_log.json"
    records = json.loads(log_path.read_text(encoding="utf-8"))

    peer_reads: list[dict[str, Any]] = []
    whole_deck_reads: list[dict[str, Any]] = []
    shared_writes: list[dict[str, Any]] = []
    peer_enumerations: list[dict[str, Any]] = []

    for record in records:
        name = str(record.get("name", ""))
        args = record.get("args", {})
        strings = list(flatten_strings(args))
        joined = "\n".join(strings)
        path_hits = [
            (match.group(0), int(match.group(1)))
            for match in PEER_PATH_PATTERN.finditer(joined)
        ]

        is_read_like = name in {"read_file", "bash", "terminal", "vision_analyze"}
        if is_read_like:
            for path, page in path_hits:
                if page != own_page:
                    peer_reads.append(
                        {
                            "tool": name,
                            "path": path,
                            "turn": record.get("turn"),
                        }
                    )
            for token in WHOLE_DECK_TOKENS:
                if token in joined:
                    whole_deck_reads.append(
                        {
                            "tool": name,
                            "path": token,
                            "turn": record.get("turn"),
                        }
                    )
            if re.search(r"\b(?:ls|find|rg|grep)\b[^\n]*(?:slides/?|plan/?)", joined):
                if not path_hits and (
                    "ls slides" in joined
                    or "find slides" in joined
                    or "rg " in joined and "slides" in joined
                ):
                    peer_enumerations.append(
                        {
                            "tool": name,
                            "command": joined[:500],
                            "turn": record.get("turn"),
                        }
                    )

        if name in WRITE_TOOLS:
            allowed = f"slides/slide_{own_page:02d}.html"
            write_targets = [
                text
                for text in strings
                if any(token in text for token in SHARED_WRITE_TOKENS)
                or "slides/slide_" in text
            ]
            for target in write_targets:
                if allowed not in target or any(
                    token in target for token in SHARED_WRITE_TOKENS
                ):
                    shared_writes.append(
                        {
                            "tool": name,
                            "target_excerpt": target[:500],
                            "turn": record.get("turn"),
                        }
                    )

    def unique(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        output: list[dict[str, Any]] = []
        for row in rows:
            key = json.dumps(row, ensure_ascii=False, sort_keys=True)
            if key not in seen:
                output.append(row)
                seen.add(key)
        return output

    peer_reads = unique(peer_reads)
    whole_deck_reads = unique(whole_deck_reads)
    shared_writes = unique(shared_writes)
    peer_enumerations = unique(peer_enumerations)
    return {
        **worker,
        "tool_calls": len(records),
        "peer_read_violations": peer_reads,
        "whole_deck_read_violations": whole_deck_reads,
        "shared_write_violations": shared_writes,
        "peer_enumeration_warnings": peer_enumerations,
        "strict_conformance": not (
            peer_reads
            or whole_deck_reads
            or shared_writes
            or peer_enumerations
        ),
    }


def analyze_run(pipeline_root: Path, spec: dict[str, str]) -> dict[str, Any]:
    manifest = load_manifest(
        pipeline_root / "logs" / spec["manifest"], spec["run_id"]
    )
    run_dir = Path(manifest["run_dir"])
    trace_root = run_dir / "_trace"
    workers, stage_start, stage_end = event_intervals(trace_root / "events.jsonl")
    audited_workers = [
        audit_worker_tools(trace_root, worker) for worker in workers
    ]
    intervals = [
        (float(worker["start_epoch"]), float(worker["end_epoch"]))
        for worker in audited_workers
    ]
    wall_span = stage_end - stage_start
    worker_sum = sum(worker["duration_seconds"] for worker in audited_workers)

    return {
        "run_id": spec["run_id"],
        "batch": spec["batch"],
        "status": manifest["status"],
        "n_pages": manifest["accept_detail"]["n_pages"],
        "worker_count": len(audited_workers),
        "worker_completion_events": sum(
            worker["completion_event_found"] for worker in audited_workers
        ),
        "slide_stage_wall_seconds": round(wall_span, 3),
        "slide_worker_sum_seconds": round(worker_sum, 3),
        "parallel_compression": round(worker_sum / wall_span, 3),
        "peak_concurrent_workers": interval_peak(intervals),
        "worker_duration_min_seconds": round(
            min(worker["duration_seconds"] for worker in audited_workers), 3
        ),
        "worker_duration_median_seconds": round(
            statistics.median(
                worker["duration_seconds"] for worker in audited_workers
            ),
            3,
        ),
        "worker_duration_max_seconds": round(
            max(worker["duration_seconds"] for worker in audited_workers), 3
        ),
        "peer_read_violations": sum(
            len(worker["peer_read_violations"]) for worker in audited_workers
        ),
        "whole_deck_read_violations": sum(
            len(worker["whole_deck_read_violations"])
            for worker in audited_workers
        ),
        "shared_write_violations": sum(
            len(worker["shared_write_violations"]) for worker in audited_workers
        ),
        "peer_enumeration_warnings": sum(
            len(worker["peer_enumeration_warnings"])
            for worker in audited_workers
        ),
        "strictly_conforming_workers": sum(
            worker["strict_conformance"] for worker in audited_workers
        ),
        "review_completed": bool(
            manifest.get("accept_detail", {}).get("review_completed")
        ),
        "model_calls": manifest["model_calls"],
        "input_tokens": manifest["input_tokens"],
        "output_tokens": manifest["output_tokens"],
        "n_renders": manifest["n_renders"],
        "n_views": manifest["n_views"],
        "workers": audited_workers,
    }


def main() -> None:
    args = parse_args()
    pipeline_root = args.pipeline_root.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    runs = [analyze_run(pipeline_root, spec) for spec in RUNS]
    result = {
        "pilot": "Pilot B — existing production-trace audit",
        "new_model_calls": 0,
        "method": (
            "Intervals use each slide_NN label's first event through its trace-completion "
            "event. Tool-boundary findings are conservative string-based audits of persisted "
            "tool logs and do not inspect hidden provider-side context."
        ),
        "runs": runs,
    }
    (output_dir / "pilot_b_trace_audit.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    run_fields = [
        "run_id",
        "n_pages",
        "worker_count",
        "slide_stage_wall_seconds",
        "slide_worker_sum_seconds",
        "parallel_compression",
        "peak_concurrent_workers",
        "strictly_conforming_workers",
        "peer_read_violations",
        "whole_deck_read_violations",
        "shared_write_violations",
        "peer_enumeration_warnings",
        "model_calls",
        "input_tokens",
        "output_tokens",
        "n_renders",
        "n_views",
        "review_completed",
    ]
    with (output_dir / "pilot_b_trace_summary.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=run_fields)
        writer.writeheader()
        for run in runs:
            writer.writerow({field: run[field] for field in run_fields})

    with (output_dir / "pilot_b_worker_intervals.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        fields = [
            "run_id",
            "worker",
            "page",
            "start_epoch",
            "end_epoch",
            "duration_seconds",
            "tool_calls",
            "strict_conformance",
            "peer_read_violations",
            "whole_deck_read_violations",
            "shared_write_violations",
            "peer_enumeration_warnings",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for run in runs:
            for worker in run["workers"]:
                writer.writerow(
                    {
                        "run_id": run["run_id"],
                        "worker": worker["worker"],
                        "page": worker["page"],
                        "start_epoch": worker["start_epoch"],
                        "end_epoch": worker["end_epoch"],
                        "duration_seconds": worker["duration_seconds"],
                        "tool_calls": worker["tool_calls"],
                        "strict_conformance": worker["strict_conformance"],
                        "peer_read_violations": len(
                            worker["peer_read_violations"]
                        ),
                        "whole_deck_read_violations": len(
                            worker["whole_deck_read_violations"]
                        ),
                        "shared_write_violations": len(
                            worker["shared_write_violations"]
                        ),
                        "peer_enumeration_warnings": len(
                            worker["peer_enumeration_warnings"]
                        ),
                    }
                )

    markdown = [
        "# Pilot B — Existing production-trace audit",
        "",
        "No new model calls were made.",
        "",
        "| Run | Pages | Slide wall (s) | Worker sum (s) | Compression | Peak workers | Strict workers | Peer reads | Whole-deck reads | Shared writes |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for run in runs:
        markdown.append(
            f"| {run['run_id']} | {run['n_pages']} | "
            f"{run['slide_stage_wall_seconds']:.1f} | "
            f"{run['slide_worker_sum_seconds']:.1f} | "
            f"{run['parallel_compression']:.2f}× | "
            f"{run['peak_concurrent_workers']} | "
            f"{run['strictly_conforming_workers']}/{run['worker_count']} | "
            f"{run['peer_read_violations']} | "
            f"{run['whole_deck_read_violations']} | "
            f"{run['shared_write_violations']} |"
        )
    markdown.extend(
        [
            "",
            "Interpretation boundary: this audit verifies observed scheduling and logged file/tool "
            "behavior. It does not estimate quality gains, causal token savings, or performance "
            "against another system.",
            "",
        ]
    )
    (output_dir / "pilot_b_trace_summary.md").write_text(
        "\n".join(markdown), encoding="utf-8"
    )

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

