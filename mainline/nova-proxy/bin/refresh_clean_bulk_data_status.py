#!/usr/bin/env python3
"""Refresh the authoritative usability index for a resumed 3-route rollout.

The formal boundary is evaluated against the root orchestrator task start, not
the manifest finish time.  This prevents a legacy attempt that merely finishes
after the configuration change from being mislabeled as formal data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROUTES = ("cloud1", "cloud2", "cloud3")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_latest_manifest(path: Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return latest
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        sample_id = str(row.get("sample_id") or "")
        if sample_id:
            latest[sample_id] = row
    return latest


def expected_samples(root: Path, route: str) -> list[dict[str, Any]]:
    query_path = root / "queries" / f"{route}.jsonl"
    batch = f"clean-1k-{route}-v1"
    seen: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(
        query_path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not raw.strip():
            continue
        query = json.loads(raw)
        qid = query.get("qid")
        if qid:
            base = f"{batch}_{qid}"
        else:
            digest = hashlib.sha1(
                json.dumps(query, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()[:12]
            base = f"{batch}_{digest}"
        seen[base] = seen.get(base, 0) + 1
        sample_id = base if seen[base] == 1 else f"{base}_{seen[base]}"
        rows.append(
            {
                "sample_id": sample_id,
                "query_id": query.get("query_id") or query.get("qid"),
                "query_line": line_number,
            }
        )
    return rows


def root_attempts(route_root: Path) -> tuple[dict[str, list[dict]], dict[str, dict]]:
    by_sample: dict[str, list[dict]] = {}
    by_trajectory: dict[str, dict] = {}
    candidates = list((route_root / "raw" / "tasks").glob("*/task_meta.json"))
    candidates += list((route_root / "raw" / "quarantine").glob("*/task_meta.json"))
    for path in candidates:
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        # Review is launched as an independent depth-0 Nova trajectory and is
        # therefore also stamped trajectory_kind=orchestrator.  Only the role
        # identifies the Deck's authoritative root Orchestrator attempt.
        if meta.get("role") != "orchestrator":
            continue
        sample_id = str(meta.get("sample_id") or "")
        trajectory_id = str(meta.get("main_trajectory_id") or "")
        try:
            started_epoch = float(meta["created_at_epoch"])
        except (KeyError, TypeError, ValueError):
            continue
        item = {
            "sample_id": sample_id,
            "trajectory_id": trajectory_id,
            "started_epoch": started_epoch,
            "started_at": datetime.fromtimestamp(
                started_epoch, timezone.utc
            ).isoformat(),
            "task_meta": str(path),
        }
        result_path = path.parent / "task_result.json"
        if result_path.is_file():
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                result = {}
            item.update(
                {
                    "task_status": str(result.get("status") or ""),
                    "task_exit_reason": str(result.get("exit_reason") or ""),
                    "task_error": str(result.get("error") or ""),
                    "task_result": str(result_path),
                }
            )
        by_sample.setdefault(sample_id, []).append(item)
        by_trajectory[trajectory_id] = item
    for attempts in by_sample.values():
        attempts.sort(key=lambda item: item["started_epoch"])
    return by_sample, by_trajectory


def manifest_root_id(row: dict[str, Any] | None) -> str:
    if not row:
        return ""
    for trajectory_id in row.get("nova_main_trajectory_ids") or []:
        if "-orchestrator-" in str(trajectory_id):
            return str(trajectory_id)
    return ""


def classify(
    latest_attempt: dict | None,
    latest_manifest: dict | None,
    manifest_attempt: dict | None,
    boundary_epoch: float,
    suspended_epoch: float | None,
) -> tuple[str, bool, bool, str]:
    if latest_attempt is None:
        return "not_started", False, False, "Waiting for its first formal run."
    if latest_attempt["started_epoch"] < boundary_epoch:
        return (
            "legacy_preboundary_not_usable",
            False,
            True,
            "Latest root orchestrator started before the formal configuration boundary.",
        )
    # A runner-level failure can prevent the clean manifest row from being
    # appended at all (ENOSPC is the important example).  The immutable Nova
    # root task_result is therefore also an authoritative terminal signal.
    # Without this fallback a failed Deck is incorrectly left "in progress"
    # forever and can be omitted from the rerun lane.
    root_status = str(latest_attempt.get("task_status") or "")
    if root_status in {"failed", "quarantine", "reject", "rejected"}:
        exit_reason = str(latest_attempt.get("task_exit_reason") or "unknown")
        error = str(latest_attempt.get("task_error") or "").strip()
        suffix = f": {error}" if error else ""
        return (
            "formal_failed_rerun_required",
            False,
            True,
            f"Root task ended with status={root_status}, exit_reason={exit_reason}{suffix}",
        )
    if manifest_attempt is None or manifest_attempt["trajectory_id"] != latest_attempt["trajectory_id"]:
        if suspended_epoch is not None and latest_attempt["started_epoch"] <= suspended_epoch:
            return (
                "formal_failed_rerun_required",
                False,
                True,
                "Root attempt was interrupted when the rollout was explicitly suspended; rerun from a fresh conversation.",
            )
        return (
            "formal_in_progress",
            False,
            False,
            "A formal post-boundary attempt is active and has no terminal manifest row yet.",
        )
    status = str((latest_manifest or {}).get("status") or "unknown")
    detail = (latest_manifest or {}).get("accept_detail") or {}
    special_geometry = detail.get("special_page_geometry") or {}
    gates_ok = (
        status == "completed"
        and (latest_manifest or {}).get("nova_raw_v2") is True
        and (latest_manifest or {}).get("nova_raw_precheck_ok") is True
        and detail.get("review_completed") is True
        and detail.get("final_view_after_review") is True
        and detail.get("finalize_succeeded") is True
        and detail.get("blank_pages") == []
        and detail.get("console_errors") == []
        and detail.get("unresolved_child_failures") == []
        and special_geometry.get("status") == "PASS"
    )
    if gates_ok:
        return (
            "formal_usable",
            True,
            False,
            "Post-boundary attempt completed and passed raw, review, render, and finalize gates.",
        )
    if status in {"failed", "quarantine", "reject", "rejected"}:
        return (
            "formal_failed_rerun_required",
            False,
            True,
            f"Post-boundary attempt ended with status={status}.",
        )
    return (
        "formal_completed_not_usable",
        False,
        True,
        "Post-boundary terminal row did not pass every direct-use gate.",
    )


def refresh_status(root: Path) -> dict[str, Any]:
    root = root.resolve()
    formal_path = root / "FORMAL_DATA_BOUNDARY.json"
    if not formal_path.is_file():
        raise SystemExit(f"missing immutable formal boundary: {formal_path}")
    formal = json.loads(formal_path.read_text(encoding="utf-8"))
    boundary = parse_time(formal["formal_data_from"])
    boundary_epoch = boundary.timestamp()
    suspended_path = root / "ROLLOUT_SUSPENDED.json"
    suspended_epoch: float | None = None
    if suspended_path.is_file():
        suspended = json.loads(suspended_path.read_text(encoding="utf-8"))
        suspended_epoch = parse_time(suspended["suspended_at"]).timestamp()
    index: list[dict[str, Any]] = []

    for route in ROUTES:
        route_root = root / "routes" / route
        batch = f"clean-1k-{route}-v1"
        manifest_path = route_root / "clean" / "logs" / f"{batch}.manifest.jsonl"
        manifests = load_latest_manifest(manifest_path)
        attempts_by_sample, attempts_by_trajectory = root_attempts(route_root)
        for expected in expected_samples(root, route):
            sample_id = expected["sample_id"]
            attempts = attempts_by_sample.get(sample_id, [])
            latest_attempt = attempts[-1] if attempts else None
            manifest = manifests.get(sample_id)
            root_id = manifest_root_id(manifest)
            manifest_attempt = attempts_by_trajectory.get(root_id)
            formal_manifest_matched = bool(
                latest_attempt
                and manifest_attempt
                and latest_attempt["started_epoch"] >= boundary_epoch
                and manifest_attempt["trajectory_id"] == latest_attempt["trajectory_id"]
            )
            state, direct_use, rerun_required, reason = classify(
                latest_attempt,
                manifest,
                manifest_attempt,
                boundary_epoch,
                suspended_epoch,
            )
            index.append(
                {
                    **expected,
                    "route": route,
                    "state": state,
                    "direct_use": direct_use,
                    "rerun_required": rerun_required,
                    "reason": reason,
                    "latest_root_trajectory_id": (
                        latest_attempt or {}
                    ).get("trajectory_id"),
                    "latest_root_started_at": (
                        latest_attempt or {}
                    ).get("started_at"),
                    "latest_root_task_status": (
                        latest_attempt or {}
                    ).get("task_status"),
                    "latest_root_exit_reason": (
                        latest_attempt or {}
                    ).get("task_exit_reason"),
                    "latest_root_error": (
                        latest_attempt or {}
                    ).get("task_error"),
                    "manifest_root_trajectory_id": root_id or None,
                    "manifest_status": (manifest or {}).get("status"),
                    "manifest_finished_at": (manifest or {}).get("finished_at"),
                    "formal_manifest_matched": formal_manifest_matched,
                    "prompt_cache_hit_rate": (
                        (manifest or {}).get("prompt_cache_hit_rate")
                        if formal_manifest_matched
                        else None
                    ),
                    "run_dir": (manifest or {}).get("run_dir"),
                }
            )

    index.sort(key=lambda row: (row["route"], row["query_line"]))
    counts = Counter(row["state"] for row in index)
    route_counts = {
        route: dict(sorted(Counter(
            row["state"] for row in index if row["route"] == route
        ).items()))
        for route in ROUTES
    }
    formal_cache_rates = [
        float(row["prompt_cache_hit_rate"])
        for row in index
        if isinstance(row.get("prompt_cache_hit_rate"), (int, float))
    ]
    summary = {
        "schema": "mural.formal-data-status-summary.v1",
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
        "formal_data_from": boundary.isoformat(),
        "authoritative_clock": "root orchestrator task_meta.created_at_epoch",
        "rollout_suspended_at": (
            datetime.fromtimestamp(suspended_epoch, timezone.utc).isoformat()
            if suspended_epoch is not None else None
        ),
        "total_queries": len(index),
        "direct_use_count": sum(bool(row["direct_use"]) for row in index),
        "rerun_required_count": sum(bool(row["rerun_required"]) for row in index),
        "not_started_count": counts.get("not_started", 0),
        "formal_in_progress_count": counts.get("formal_in_progress", 0),
        "states": dict(sorted(counts.items())),
        "routes": route_counts,
        "formal_terminal_cache_hit_rate_avg": (
            sum(formal_cache_rates) / len(formal_cache_rates)
            if formal_cache_rates else None
        ),
        "index": str(root / "data-status" / "index.jsonl"),
    }
    status_root = root / "data-status"
    atomic_write(
        status_root / "index.jsonl",
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in index),
    )
    atomic_write(
        status_root / "summary.json",
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
    )
    selections = {
        "direct-use.sample_ids.txt": [
            row["sample_id"] for row in index if row["direct_use"]
        ],
        "rerun-required.sample_ids.txt": [
            row["sample_id"] for row in index if row["rerun_required"]
        ],
        "formal-in-progress.sample_ids.txt": [
            row["sample_id"] for row in index if row["state"] == "formal_in_progress"
        ],
        "not-started.sample_ids.txt": [
            row["sample_id"] for row in index if row["state"] == "not_started"
        ],
    }
    for filename, values in selections.items():
        atomic_write(status_root / filename, "".join(value + "\n" for value in values))
    readme = f"""# 正式刷数数据状态

不可变的正式数据边界：`{boundary.isoformat()}`。

归类只使用根 Orchestrator 的 `task_meta.created_at_epoch`。完成时间不能用于
判断数据是否属于正式区间，防止“旧配置启动、配置后才完成”的轨迹混入。

- `formal_usable`：正式完成且全部 Gate 通过，可进入后续 QC/训练准备。
- `formal_in_progress`：使用正式配置运行中，完成前不能使用。
- `formal_failed_rerun_required` / `formal_completed_not_usable`：需重刷或修复。
- `legacy_preboundary_not_usable`：旧配置轨迹，仅保留原始记录，不能直接使用。
- `not_started`：尚未启动，等待正式运行。

`direct-use.sample_ids.txt` 是下游唯一允许直接使用的白名单；任何导出都不得
绕过该文件。分类器不删除、不覆盖原始轨迹。
"""
    atomic_write(status_root / "README.md", readme)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_root", type=Path)
    args = parser.parse_args()
    print(json.dumps(refresh_status(args.run_root), ensure_ascii=False))


if __name__ == "__main__":
    main()
