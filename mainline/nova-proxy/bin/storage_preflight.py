#!/usr/bin/env python3
"""Fail-closed AFS write probe for a rollout root.

The probe exercises both data allocation and metadata creation before any
teacher request is started.  Its temporary directory is always scoped beneath
the requested rollout root and is removed on success or failure.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--write-mib", type=int, default=64)
    parser.add_argument("--small-files", type=int, default=256)
    parser.add_argument("--minimum-free-gib", type=int, default=5)
    args = parser.parse_args()

    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(root)
    minimum = args.minimum_free_gib * 1024**3
    if usage.free < minimum:
        raise SystemExit(
            f"storage preflight failed: only {usage.free} bytes free; "
            f"minimum is {minimum}"
        )

    probe = Path(tempfile.mkdtemp(prefix=".storage-preflight-", dir=root))
    started = datetime.now(timezone.utc)
    try:
        large = probe / "allocation.bin"
        block = bytes(1024 * 1024)
        with large.open("wb") as handle:
            for _ in range(args.write_mib):
                handle.write(block)
            handle.flush()
            os.fsync(handle.fileno())

        small = probe / "metadata"
        small.mkdir()
        for index in range(args.small_files):
            path = small / f"{index:06d}.json"
            with path.open("x", encoding="utf-8") as handle:
                handle.write("{}\n")
                handle.flush()
                os.fsync(handle.fileno())
        directory_fd = os.open(small, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
            raise SystemExit(
                f"storage preflight failed: {type(exc).__name__}: {exc}"
            ) from exc
        raise
    finally:
        shutil.rmtree(probe, ignore_errors=True)

    finished = datetime.now(timezone.utc)
    atomic_json(
        args.report,
        {
            "schema": "mural.storage-preflight.v1",
            "status": "PASS",
            "root": str(root),
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "write_mib": args.write_mib,
            "small_files": args.small_files,
            "disk_total_bytes": usage.total,
            "disk_used_bytes": usage.used,
            "disk_free_bytes_before_probe": usage.free,
        },
    )
    print(f"storage_preflight=PASS root={root}")


if __name__ == "__main__":
    main()
