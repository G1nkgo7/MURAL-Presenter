#!/usr/bin/env python3
"""Stop all rollout process groups when a new ENOSPC signal appears."""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


PATTERN = re.compile(
    rb"No space left on device|\bENOSPC\b|Disk quota exceeded|\bEDQUOT\b",
    re.IGNORECASE,
)


def atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def snapshot(paths: list[Path], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(
        output,
        {
            "schema": "mural.enospc-watch-cursor.v1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "offsets": {
                str(path): path.stat().st_size if path.is_file() else 0
                for path in paths
            },
        },
    )


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_groups(pids: list[int]) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in pids:
            try:
                os.killpg(pid, sig)
            except ProcessLookupError:
                pass
        if sig == signal.SIGTERM:
            time.sleep(5)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--log", action="append", type=Path, default=[])
    parser.add_argument("--pid", action="append", type=int, default=[])
    parser.add_argument("--cursor", required=True, type=Path)
    parser.add_argument("--snapshot-only", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=3.0)
    args = parser.parse_args()

    if args.snapshot_only:
        snapshot(args.log, args.cursor)
        return

    state = json.loads(args.cursor.read_text(encoding="utf-8"))
    offsets = {Path(path): int(value) for path, value in state["offsets"].items()}
    root = args.run_root.resolve()
    while any(process_alive(pid) for pid in args.pid):
        for path in args.log:
            try:
                size = path.stat().st_size
                offset = min(offsets.get(path, 0), size)
                with path.open("rb") as handle:
                    handle.seek(offset)
                    appended = handle.read()
                offsets[path] = size
            except FileNotFoundError:
                continue
            if not PATTERN.search(appended):
                continue

            excerpt = PATTERN.search(appended)
            payload = {
                "schema": "mural.storage-stop.v1",
                "status": "STOPPED_ENOSPC",
                "detected_at": datetime.now(timezone.utc).isoformat(),
                "log": str(path),
                "matched": excerpt.group(0).decode("utf-8", errors="replace") if excerpt else "ENOSPC",
                "policy": "Stop all Deck workers; do not launch or retry until storage_preflight passes.",
            }
            try:
                atomic_json(root / "NO_SPACE_STOP.json", payload)
            except OSError:
                pass
            for control in [root / "live" / "total-concurrency", *(
                root / "routes" / f"cloud{index}" / "live" / "deck-concurrency"
                for index in range(1, 4)
            )]:
                try:
                    control.parent.mkdir(parents=True, exist_ok=True)
                    control.write_text("0\n", encoding="utf-8")
                except OSError:
                    pass
            if os.environ.get("MURAL_ENOSPC_NOTIFY", "1") != "0":
                notifier = os.environ.get("FEISHU_NOTIFY")
                if notifier:
                    subprocess.run(
                        [
                            "python3",
                            notifier,
                        "【MURAL 数据 agent｜已止损】检测到 ENOSPC/空间配额错误，"
                        "已停止所有 Deck worker，禁止继续领新 Query。清理空间并通过写入探针后才能恢复。\n"
                        f"日志：{path}\n批次：{root}",
                        ],
                        check=False,
                    )
            stop_groups(args.pid)
            raise SystemExit(errno.ENOSPC)
        time.sleep(max(0.5, args.poll_seconds))


if __name__ == "__main__":
    main()
