#!/usr/bin/env python3
"""Recompute manuscript-facing THREAD-Bench coverage statistics."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bench-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def distance_bin(distance: int) -> str:
    if distance <= 4:
        return "1-4"
    if distance <= 9:
        return "5-9"
    if distance <= 14:
        return "10-14"
    return "15+"


def load_cases(root: Path) -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((root / "cases").glob("SPB-*.json"))
    ]


def main() -> None:
    args = parse_args()
    root = args.bench_root.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    benchmark = json.loads((root / "benchmark.json").read_text(encoding="utf-8"))
    cases = load_cases(root)
    requirements = [item for case in cases for item in case["requirements"]]
    dependencies = [item for case in cases for item in case["dependencies"]]
    targets = [target for dependency in dependencies for target in dependency["targets"]]
    distances = [int(target["distance_pages"]) for target in targets]
    max_spans = [int(case["horizon"]["max_dependency_span_pages"]) for case in cases]

    distributions = {
        field: dict(sorted(Counter(case[field] for case in cases).items()))
        for field in [
            "primary_capability",
            "horizon",
            "language",
            "material_mode",
            "split",
        ]
        if field != "horizon"
    }
    distributions["horizon_bucket"] = dict(
        sorted(Counter(case["horizon"]["bucket"] for case in cases).items())
    )
    distributions["target_slides"] = dict(
        sorted(
            Counter(
                str(case["horizon"]["target_slides"]["exact"]) for case in cases
            ).items(),
            key=lambda item: int(item[0]),
        )
    )
    distributions["dependency_distance"] = {
        key: Counter(distance_bin(distance) for distance in distances).get(key, 0)
        for key in ["1-4", "5-9", "10-14", "15+"]
    }

    suite_rows: list[dict[str, Any]] = []
    by_suite: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        by_suite[case["primary_capability"]].append(case)
    for suite, rows in sorted(by_suite.items()):
        buckets = Counter(row["horizon"]["bucket"] for row in rows)
        suite_rows.append(
            {
                "suite": suite,
                "cases": len(rows),
                "medium": buckets["medium"],
                "long": buckets["long"],
                "ultra": buckets["ultra"],
                "avg_slides": round(
                    statistics.mean(
                        row["horizon"]["target_slides"]["exact"] for row in rows
                    ),
                    2,
                ),
                "avg_dependency_groups": round(
                    statistics.mean(len(row["dependencies"]) for row in rows), 2
                ),
                "avg_source_target_checks": round(
                    statistics.mean(
                        sum(len(item["targets"]) for item in row["dependencies"])
                        for row in rows
                    ),
                    2,
                ),
            }
        )

    grounded_cases = [
        case for case in cases if case.get("material_mode") == "grounded"
    ]
    summary = {
        "benchmark_name": "THREAD-Bench",
        "benchmark_id": benchmark["benchmark_id"],
        "benchmark_version": benchmark["benchmark_version"],
        "case_count": len(cases),
        "expert_constructed": True,
        "total_target_slides": sum(
            case["horizon"]["target_slides"]["exact"] for case in cases
        ),
        "requirements": {
            "total": len(requirements),
            "must": sum(item["severity"] == "must" for item in requirements),
            "should": sum(item["severity"] == "should" for item in requirements),
        },
        "dependency_groups": len(dependencies),
        "source_target_checks": len(targets),
        "targets_distance_15_plus": sum(distance >= 15 for distance in distances),
        "dependency_distance": {
            "minimum": min(distances),
            "median": statistics.median(distances),
            "mean": round(statistics.mean(distances), 3),
            "maximum": max(distances),
        },
        "case_max_dependency_span": {
            "median": statistics.median(max_spans),
            "maximum": max(max_spans),
        },
        "grounded_cases": len(grounded_cases),
        "grounded_source_files": sum(
            len(case.get("attachments", [])) for case in grounded_cases
        ),
        "distributions": distributions,
        "suite_rows": suite_rows,
        "official_scalar_quality_score": bool(
            benchmark.get("headline_policy", {}).get("scalar_quality_score", False)
        ),
    }

    (output_dir / "pilot_a_benchmark_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    with (output_dir / "pilot_a_suite_coverage.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=list(suite_rows[0]))
        writer.writeheader()
        writer.writerows(suite_rows)

    with (output_dir / "pilot_a_distance_distribution.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=["distance_bin", "checks"])
        writer.writeheader()
        for key, value in distributions["dependency_distance"].items():
            writer.writerow({"distance_bin": key, "checks": value})

    markdown = [
        "# Pilot A — THREAD-Bench legacy frozen-snapshot audit",
        "",
        f"- Cases: {summary['case_count']}",
        f"- Target slides: {summary['total_target_slides']}",
        (
            f"- Requirements: {summary['requirements']['total']} "
            f"({summary['requirements']['must']} must, "
            f"{summary['requirements']['should']} should)"
        ),
        f"- Dependency groups: {summary['dependency_groups']}",
        f"- Source-to-target checks: {summary['source_target_checks']}",
        f"- Targets at distance 15+: {summary['targets_distance_15_plus']}",
        (
            "- Case maximum dependency span: "
            f"median {summary['case_max_dependency_span']['median']}, "
            f"maximum {summary['case_max_dependency_span']['maximum']}"
        ),
        (
            f"- Grounded cases/files: {summary['grounded_cases']}/"
            f"{summary['grounded_source_files']}"
        ),
        (
            "- Official scalar quality score: "
            f"{str(summary['official_scalar_quality_score']).lower()}"
        ),
        "",
        "| Suite | Cases | Medium | Long | Ultra | Avg slides | Avg dependency groups | Avg checks |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in suite_rows:
        markdown.append(
            f"| {row['suite']} | {row['cases']} | {row['medium']} | "
            f"{row['long']} | {row['ultra']} | {row['avg_slides']:.2f} | "
            f"{row['avg_dependency_groups']:.2f} | "
            f"{row['avg_source_target_checks']:.2f} |"
        )
    markdown.extend(
        [
            "",
            "This audit verifies release integrity and manuscript statistics. "
            "It is not a generation-system comparison.",
            "",
        ]
    )
    (output_dir / "pilot_a_benchmark_summary.md").write_text(
        "\n".join(markdown), encoding="utf-8"
    )

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
