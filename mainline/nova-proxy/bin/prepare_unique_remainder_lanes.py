#!/usr/bin/env python3
"""Build two disjoint rollout lanes after earlier lanes have fully drained."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(temporary, path)


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--status-index", required=True, action="append", type=Path)
    parser.add_argument("--output-a", required=True, type=Path)
    parser.add_argument("--output-b", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()

    source_rows = read_jsonl(args.source)
    source_ids = [str(row.get("query_id") or "") for row in source_rows]
    if "" in source_ids or len(source_ids) != len(set(source_ids)):
        raise SystemExit("source query_id values must be non-empty and unique")

    usable_by_index: list[set[str]] = []
    state_counts: dict[str, dict[str, int]] = {}
    for path in args.status_index:
        rows = read_jsonl(path)
        counts = Counter(str(row.get("state") or "") for row in rows)
        if counts.get("formal_in_progress", 0):
            raise SystemExit(
                f"refusing to split while {path} has "
                f"{counts['formal_in_progress']} in-progress queries"
            )
        usable_by_index.append({
            str(row.get("query_id") or "")
            for row in rows
            if row.get("state") == "formal_usable"
        })
        state_counts[str(path.resolve())] = dict(sorted(counts.items()))

    usable = set().union(*usable_by_index)
    unknown = usable - set(source_ids)
    if unknown:
        raise SystemExit(f"usable IDs missing from source: {sorted(unknown)[:10]}")
    duplicate_usable = (
        set.intersection(*usable_by_index) if len(usable_by_index) > 1 else set()
    )
    remaining = [row for row in source_rows if str(row["query_id"]) not in usable]
    lane_a = remaining[0::2]
    lane_b = remaining[1::2]
    ids_a = {str(row["query_id"]) for row in lane_a}
    ids_b = {str(row["query_id"]) for row in lane_b}
    if ids_a & ids_b or (ids_a | ids_b) != {str(row["query_id"]) for row in remaining}:
        raise SystemExit("lane partition is not disjoint and exhaustive")

    write_jsonl(args.output_a, lane_a)
    write_jsonl(args.output_b, lane_b)
    manifest = {
        "schema": "mural.unique-remainder-lanes.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": str(args.source.resolve()),
        "source_sha256": sha256(args.source),
        "status_indexes": [str(path.resolve()) for path in args.status_index],
        "status_index_sha256": {
            str(path.resolve()): sha256(path) for path in args.status_index
        },
        "status_counts": state_counts,
        "source_count": len(source_rows),
        "usable_unique_count": len(usable),
        "usable_in_multiple_roots_count": len(duplicate_usable),
        "usable_in_multiple_roots_query_ids": sorted(duplicate_usable),
        "remaining_unique_count": len(remaining),
        "lane_a": {
            "path": str(args.output_a.resolve()),
            "count": len(lane_a),
            "sha256": sha256(args.output_a),
        },
        "lane_b": {
            "path": str(args.output_b.resolve()),
            "count": len(lane_b),
            "sha256": sha256(args.output_b),
        },
        "invariants": {
            "lane_overlap": 0,
            "remaining_is_source_minus_usable": True,
            "in_progress_at_split": 0,
        },
    }
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(args.manifest, manifest)
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
