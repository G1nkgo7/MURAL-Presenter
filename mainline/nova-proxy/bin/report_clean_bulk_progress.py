#!/usr/bin/env python3
"""Refresh data usability state and build one compact progress summary."""

from __future__ import annotations

import argparse
from pathlib import Path

from refresh_clean_bulk_data_status import refresh_status


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_root", type=Path)
    args = parser.parse_args()
    root = args.run_root.resolve()
    marker = root / "FORMAL_DATA_BOUNDARY.json"
    status_summary = refresh_status(root)
    chunks = []
    for route in ("cloud1", "cloud2", "cloud3"):
        states = status_summary["routes"][route]
        usable = states.get("formal_usable", 0)
        running = states.get("formal_in_progress", 0)
        pending = states.get("not_started", 0)
        rerun = sum(
            count for name, count in states.items()
            if name in {
                "legacy_preboundary_not_usable",
                "formal_failed_rerun_required",
                "formal_completed_not_usable",
            }
        )
        chunks.append(
            f"{route}: 可用={usable}, 在跑={running}, 待启动={pending}, 需重刷={rerun}"
        )
    cache_rate = status_summary["formal_terminal_cache_hit_rate_avg"]
    cache = (
        f"正式完成项 cache_hit(avg)={cache_rate:.1%}"
        if isinstance(cache_rate, (int, float))
        else "正式完成项 cache_hit=等待首批完成"
    )
    marker_state = "formal_boundary=locked" if marker.is_file() else "formal_boundary=missing"
    concurrency_path = root / "live" / "total-concurrency"
    try:
        current_concurrency = int(concurrency_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        current_concurrency = "unknown"
    usable = status_summary["direct_use_count"]
    in_progress = status_summary["formal_in_progress_count"]
    pending = status_summary["not_started_count"]
    rerun = status_summary["rerun_required_count"]
    print(
        f"【MURAL 数据 agent】1K Nova rollout 每小时进度（{current_concurrency} 并发）\n"
        + "；".join(chunks)
        + f"\n正式可直接用={usable}；正式在跑={in_progress}；待正式启动={pending}；旧配置/失败需重刷={rerun}"
        + f"\n{cache}；{marker_state}\n{root}"
    )


if __name__ == "__main__":
    main()
