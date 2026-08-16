#!/usr/bin/env python3
"""Wait for one frozen Deck cohort, validate it, then raise live concurrency.

This is a local-file watcher: it performs no model calls and polls at a low
frequency.  It never interrupts in-flight Decks and never lowers concurrency on
its own.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from refresh_clean_bulk_data_status import refresh_status


ROUTES = ("cloud1", "cloud2", "cloud3")
PASS_PROBES = {
    "chromium:render",
    "runtime:nova-shared-gate-health",
    "live:serper-search",
    "live:serper-images",
}
FEISHU_NOTIFY = os.environ.get("FEISHU_NOTIFY")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def notify(message: str) -> None:
    if not FEISHU_NOTIFY:
        return
    subprocess.run(
        [sys.executable, FEISHU_NOTIFY, message],
        check=False,
    )


def load_index(root: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    path = root / "data-status" / "index.jsonl"
    for raw in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(raw)
        result[row["sample_id"]] = row
    return result


def validate_environment(root: Path) -> list[str]:
    failures: list[str] = []
    formal = json.loads((root / "FORMAL_DATA_BOUNDARY.json").read_text(encoding="utf-8"))
    config = formal.get("configuration") or {}
    for route in ROUTES:
        if config.get(f"{route}_model") != "claude-opus-4-7-thinking":
            failures.append(f"{route} formal model is not claude-opus-4-7-thinking")
        report_path = root / "routes" / route / "runtime" / "clean-environment-preflight.json"
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            failures.append(f"{route} preflight unreadable: {error}")
            continue
        probes = {
            item.get("name"): item.get("status")
            for item in report.get("checks") or []
        }
        if report.get("status") != "PASS":
            failures.append(f"{route} environment preflight status is not PASS")
        for probe in PASS_PROBES:
            if probes.get(probe) != "PASS":
                failures.append(f"{route} probe {probe} is not PASS")
    return failures


def malformed_delegate_summary(root: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    malformed = 0
    recovered = 0
    unrecovered: list[str] = []
    for row in rows:
        if row["route"] != "cloud3":
            continue
        trajectory_id = row.get("latest_root_trajectory_id")
        if not trajectory_id:
            continue
        task_root = root / "routes" / "cloud3" / "raw" / "tasks" / trajectory_id
        types: list[str] = []
        responses = sorted(
            task_root.glob("attempts/main/*/*/response.json"),
            key=lambda path: path.stat().st_mtime,
        )
        for response_path in responses:
            try:
                response = json.loads(response_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for block in response.get("content") or []:
                if not (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("name") == "delegate_task"
                ):
                    continue
                tool_input = block.get("input")
                tasks = tool_input.get("tasks") if isinstance(tool_input, dict) else None
                types.append("list" if isinstance(tasks, list) else "malformed")
        bad_positions = [index for index, value in enumerate(types) if value == "malformed"]
        malformed += len(bad_positions)
        for position in bad_positions:
            if "list" in types[position + 1:]:
                recovered += 1
            else:
                unrecovered.append(row["sample_id"])
    return {
        "malformed_calls": malformed,
        "recovered_calls": recovered,
        "unrecovered_sample_ids": sorted(set(unrecovered)),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_root", type=Path)
    parser.add_argument("--cohort-file", type=Path, required=True)
    parser.add_argument("--expected", type=int, default=16)
    parser.add_argument("--target", type=int, default=32)
    parser.add_argument("--poll-seconds", type=int, default=300)
    args = parser.parse_args()
    root = args.run_root.resolve()
    gate_root = root / "scale32-gate"
    state_path = gate_root / "state.json"
    cohort = [
        value.strip()
        for value in args.cohort_file.read_text(encoding="utf-8").splitlines()
        if value.strip()
    ]
    if len(cohort) != args.expected or len(set(cohort)) != args.expected:
        payload = {
            "status": "blocked_invalid_cohort",
            "at": now(),
            "expected": args.expected,
            "actual": len(cohort),
        }
        atomic_json(state_path, payload)
        notify(f"【MURAL 数据 agent】16→32 自动扩容未启动：cohort 数量异常 {len(cohort)}/{args.expected}；{state_path}")
        raise SystemExit(2)
    atomic_write(gate_root / "cohort.sample_ids.txt", "".join(item + "\n" for item in cohort))
    atomic_json(state_path, {
        "status": "waiting_for_cohort",
        "started_at": now(),
        "cohort_size": len(cohort),
        "target_concurrency": args.target,
        "poll_seconds": args.poll_seconds,
        "model_calls": 0,
    })

    while True:
        refresh_status(root)
        index = load_index(root)
        rows = [index.get(sample_id) for sample_id in cohort]
        missing = [cohort[i] for i, row in enumerate(rows) if row is None]
        if missing:
            payload = {"status": "blocked_missing_index_rows", "at": now(), "sample_ids": missing}
            atomic_json(state_path, payload)
            notify(f"【MURAL 数据 agent】16→32 自动扩容被阻止：状态索引缺 {len(missing)} 条；{state_path}")
            raise SystemExit(3)
        typed_rows = [row for row in rows if row is not None]
        running = [row for row in typed_rows if row["state"] == "formal_in_progress"]
        if running:
            atomic_json(state_path, {
                "status": "waiting_for_cohort",
                "updated_at": now(),
                "cohort_size": len(cohort),
                "remaining": len(running),
                "target_concurrency": args.target,
                "poll_seconds": args.poll_seconds,
                "model_calls": 0,
            })
            time.sleep(args.poll_seconds)
            continue

        unusable = [row for row in typed_rows if row["state"] != "formal_usable"]
        environment_failures = validate_environment(root)
        delegate = malformed_delegate_summary(root, typed_rows)
        failures = list(environment_failures)
        if unusable:
            failures.append(
                f"{len(unusable)}/{len(cohort)} cohort Decks are not formal_usable"
            )
        if delegate["unrecovered_sample_ids"]:
            failures.append(
                "Cloud3 has unrecovered malformed delegate_task calls: "
                + ",".join(delegate["unrecovered_sample_ids"])
            )
        if failures:
            payload = {
                "status": "blocked_gate_failed",
                "at": now(),
                "target_concurrency": args.target,
                "failures": failures,
                "states": {row["sample_id"]: row["state"] for row in typed_rows},
                "cloud3_delegate_task": delegate,
                "action": "kept concurrency unchanged",
            }
            atomic_json(state_path, payload)
            notify(f"【MURAL 数据 agent】首批16条已结束，但16→32检查未通过，保持16并发；{state_path}")
            raise SystemExit(4)

        setter = Path(__file__).with_name("set_clean_bulk_concurrency.py")
        result = subprocess.run(
            [sys.executable, str(setter), str(root), str(args.target)],
            check=True,
            capture_output=True,
            text=True,
        )
        expected_allocations = [11, 11, 10]
        actual_total = int((root / "live" / "total-concurrency").read_text().strip())
        actual_allocations = [
            int((root / "routes" / route / "live" / "deck-concurrency").read_text().strip())
            for route in ROUTES
        ]
        if actual_total != args.target or actual_allocations != expected_allocations:
            payload = {
                "status": "blocked_scale_write_verification",
                "at": now(),
                "actual_total": actual_total,
                "actual_allocations": actual_allocations,
            }
            atomic_json(state_path, payload)
            notify(f"【MURAL 数据 agent】16→32 控制文件写入校验失败；{state_path}")
            raise SystemExit(5)
        payload = {
            "status": "scaled_to_32",
            "at": now(),
            "cohort_size": len(cohort),
            "cohort_formal_usable": len(cohort),
            "total_concurrency": actual_total,
            "route_allocations": actual_allocations,
            "cloud3_delegate_task": delegate,
            "setter_output": result.stdout.strip(),
            "model_calls": 0,
        }
        atomic_json(state_path, payload)
        notify(f"【MURAL 数据 agent】首批16条检查通过，已自动升至32并发（11/11/10）；{state_path}")
        return


if __name__ == "__main__":
    main()
