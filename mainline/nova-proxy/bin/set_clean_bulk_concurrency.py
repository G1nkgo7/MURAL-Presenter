#!/usr/bin/env python3
"""Atomically distribute one Deck-concurrency target across three CCI routes."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def atomic_int(path: Path, value: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temporary.write_text(f"{value}\n", encoding="utf-8")
    os.replace(temporary, path)


def allocations(total: int, route_cap: int = 22) -> list[int]:
    if not 0 <= total <= route_cap * 3:
        raise ValueError(f"total must be in 0..{route_cap * 3}, got {total}")
    base, extra = divmod(total, 3)
    # All three routes use Opus 4.7 Thinking with distinct Cloud keys. Each
    # Deck stays pinned to one route, so no stateful conversation crosses keys.
    values = [base + (1 if index < extra else 0) for index in range(3)]
    if any(value > route_cap for value in values):
        raise ValueError(f"per-route allocation exceeds cap {route_cap}: {values}")
    return values


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_root", type=Path)
    parser.add_argument("total", type=int)
    parser.add_argument("--route-cap", type=int, default=22)
    args = parser.parse_args()
    root = args.run_root.resolve()
    values = allocations(args.total, args.route_cap)
    atomic_int(root / "live" / "total-concurrency", args.total)
    for index, value in enumerate(values, 1):
        atomic_int(root / "routes" / f"cloud{index}" / "live" / "deck-concurrency", value)
    print(f"total={args.total} routes={values} root={root}")


if __name__ == "__main__":
    main()
