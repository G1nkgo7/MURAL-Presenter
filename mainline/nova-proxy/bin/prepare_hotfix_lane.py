#!/usr/bin/env python3
"""Freeze a non-overlapping query lane from one rollout status snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--status-index", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--state", default="not_started")
    args = parser.parse_args()

    index_rows = [
        json.loads(line)
        for line in args.status_index.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    state_counts = Counter(str(row.get("state") or "") for row in index_rows)
    selected_ids = {
        str(row.get("query_id") or "")
        for row in index_rows
        if row.get("state") == args.state
    }
    if "" in selected_ids:
        raise SystemExit("status index contains an empty selected query_id")

    source_rows = [
        json.loads(line)
        for line in args.source.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    source_by_id = {str(row.get("query_id") or ""): row for row in source_rows}
    if len(source_by_id) != len(source_rows) or "" in source_by_id:
        raise SystemExit("source query_id values must be non-empty and unique")
    missing = sorted(selected_ids - source_by_id.keys())
    if missing:
        raise SystemExit(f"selected query IDs missing from source: {missing[:10]}")

    selected_rows = [
        row for row in source_rows if str(row.get("query_id") or "") in selected_ids
    ]
    if len(selected_rows) != len(selected_ids):
        raise SystemExit("selected query count is not bijective")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.tmp.{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in selected_rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(temporary, args.output)

    manifest = {
        "schema": "mural.hotfix-query-lane.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "selection_state": args.state,
        "source": str(args.source.resolve()),
        "source_sha256": sha256(args.source),
        "status_index": str(args.status_index.resolve()),
        "status_index_sha256": sha256(args.status_index),
        "status_counts_at_freeze": dict(sorted(state_counts.items())),
        "selected_count": len(selected_rows),
        "selected_query_ids_sha256": hashlib.sha256(
            ("\n".join(sorted(selected_ids)) + "\n").encode("utf-8")
        ).hexdigest(),
        "output": str(args.output.resolve()),
        "output_sha256": sha256(args.output),
        "non_overlap_policy": (
            "Only queries whose frozen v3 state was not_started are present; "
            "formal_usable, in_progress, failed, and legacy attempts are excluded."
        ),
    }
    atomic_json(args.output.with_suffix(args.output.suffix + ".manifest.json"), manifest)
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
