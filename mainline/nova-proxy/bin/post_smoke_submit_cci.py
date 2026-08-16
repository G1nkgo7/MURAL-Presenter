#!/usr/bin/env python3
"""Wait cheaply for three smoke cases, fail closed, then submit one CCI app."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


WS = os.environ.get("SCO_WORKSPACE_NAME", "")
SCO = os.environ.get("SCO_BIN", "sco")
FEISHU = os.environ.get("FEISHU_NOTIFY", "")
HERE = Path(__file__).resolve().parent


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def notify(message: str) -> None:
    if FEISHU:
        subprocess.run(["python3", FEISHU, f"【MURAL 数据 agent】{message}"], check=False)


def latest_manifest(path: Path) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    if not path.is_file():
        return latest
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        row = json.loads(raw)
        latest[str(row.get("sample_id") or "")] = row
    return latest


def raw_gate(smoke_root: Path) -> tuple[bool, list[str]]:
    errors: list[str] = []
    task_prechecks = sorted((smoke_root / "raw" / "tasks").glob("*/precheck.json"))
    if not task_prechecks:
        errors.append("raw/tasks has no finalized prechecks")
    for path in task_prechecks:
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{path.parent.name}: unreadable precheck ({exc})")
            continue
        if report.get("ok") is not True:
            errors.append(f"{path.parent.name}: {report.get('errors')}")
    quarantine = [path for path in (smoke_root / "raw" / "quarantine").glob("*") if path.is_dir()]
    if quarantine:
        errors.append(f"raw/quarantine contains {len(quarantine)} trajectories")
    return not errors, errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke-root", required=True, type=Path)
    parser.add_argument("--smoke-batch", required=True)
    parser.add_argument("--smoke-pid-file", required=True, type=Path)
    parser.add_argument("--bulk-root", required=True, type=Path)
    parser.add_argument("--queries", required=True, type=Path)
    parser.add_argument("--app-name", required=True)
    parser.add_argument("--expected-smoke-count", type=int, default=3)
    parser.add_argument("--poll-seconds", type=int, default=300)
    args = parser.parse_args()
    if not WS:
        raise SystemExit("SCO_WORKSPACE_NAME must be set")
    manifest = args.smoke_root / "clean" / "logs" / f"{args.smoke_batch}.manifest.jsonl"
    state_path = args.smoke_root / "launcher" / "post-smoke-state.json"

    while True:
        rows = latest_manifest(manifest)
        if len(rows) >= args.expected_smoke_count:
            failures = [
                f"{sid}: status={row.get('status')} precheck={row.get('nova_raw_precheck_ok')}"
                for sid, row in sorted(rows.items())
                if row.get("status") != "completed" or row.get("nova_raw_precheck_ok") is not True
            ]
            raw_ok, raw_errors = raw_gate(args.smoke_root)
            failures.extend(raw_errors)
            if failures:
                atomic_json(state_path, {
                    "status": "smoke_failed",
                    "at": datetime.now(timezone.utc).isoformat(),
                    "failures": failures,
                })
                notify(
                    f"{args.expected_smoke_count} 条 smoke 未通过 fail-closed Gate，"
                    "未提交 CCI：" + "; ".join(failures[:3])
                )
                return
            if raw_ok:
                break

        try:
            pid = int(args.smoke_pid_file.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)
        except Exception:
            atomic_json(state_path, {
                "status": "smoke_process_exited_early",
                "at": datetime.now(timezone.utc).isoformat(),
                "completed_manifest_rows": len(rows),
            })
            notify(
                f"smoke 进程提前退出，仅完成 {len(rows)}/"
                f"{args.expected_smoke_count}；未提交 CCI，请查看 {args.smoke_root}"
            )
            return
        time.sleep(max(60, args.poll_seconds))

    prepare = subprocess.run([
        "python3", str(HERE / "prepare_clean_bulk_cci.py"), str(args.bulk_root),
        "--app-name", args.app_name,
        "--queries", str(args.queries),
        "--initial-concurrency", "32",
    ], text=True, capture_output=True)
    if prepare.returncode != 0:
        notify(f"smoke 已通过，但 CCI 配置生成失败：{prepare.stderr[-500:]}")
        return
    app_yaml = args.bulk_root / "cci" / "app.yaml"
    create = subprocess.run([
        SCO, "cci", "apps", "create", args.app_name,
        "--workspace-name", WS, "--config", str(app_yaml),
    ], text=True, capture_output=True)
    (args.bulk_root / "cci" / "create.stdout.log").write_text(create.stdout, encoding="utf-8")
    (args.bulk_root / "cci" / "create.stderr.log").write_text(create.stderr, encoding="utf-8")
    if create.returncode != 0:
        atomic_json(state_path, {
            "status": "cci_submit_failed",
            "at": datetime.now(timezone.utc).isoformat(),
            "returncode": create.returncode,
        })
        notify(f"smoke 已通过，但 CCI 提交失败；请查看 {args.bulk_root}/cci/create.stderr.log")
        return
    atomic_json(state_path, {
        "status": "cci_submitted",
        "at": datetime.now(timezone.utc).isoformat(),
        "app_name": args.app_name,
        "bulk_root": str(args.bulk_root),
        "initial_concurrency": 32,
    })
    notify(
        f"{args.expected_smoke_count} 条 smoke 全部通过，CCI 已提交：{args.app_name}；"
        f"32c128g/0GPU，Deck 并发 32（Cloud1/2：Opus4.7 Thinking，各 11；"
        f"Cloud3：Opus5，10），"
        f"请调度资源。产物：{args.bulk_root}"
    )


if __name__ == "__main__":
    main()
