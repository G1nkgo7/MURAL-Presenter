#!/usr/bin/env python3
"""Appendix audit figure: benchmark dependency spans and legacy trace intervals."""

from __future__ import annotations

from pathlib import Path
import shutil

import matplotlib.pyplot as plt
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUTPUT = Path(__file__).resolve().parent
MANUSCRIPT_OUTPUT = ROOT.parent / "manuscript" / "figures"

BLUE = "#0077BB"
CYAN = "#33BBEE"
TEAL = "#009988"
ORANGE = "#EE7733"
INK = "#333840"
GRID = "#D8DCE2"


def setup_style() -> None:
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 8,
            "axes.titlesize": 9,
            "axes.labelsize": 8,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.edgecolor": INK,
            "axes.linewidth": 0.7,
            "xtick.labelsize": 7,
            "ytick.labelsize": 7,
            "grid.color": GRID,
            "grid.linewidth": 0.6,
            "grid.alpha": 0.8,
            "legend.frameon": False,
            "savefig.dpi": 450,
            "savefig.bbox": "tight",
            "svg.fonttype": "none",
            "svg.hashsalt": "mural-presenter-pilot-audit",
        }
    )


def draw_interval_panel(
    ax: plt.Axes,
    rows: pd.DataFrame,
    title: str,
    compression: str,
) -> None:
    rows = rows.sort_values("page")
    origin = rows["start_epoch"].min()
    height = 0.58
    for y, (_, row) in enumerate(rows.iterrows()):
        start = row["start_epoch"] - origin
        color = BLUE if bool(row["strict_conformance"]) else ORANGE
        ax.broken_barh(
            [(start, row["duration_seconds"])],
            (y - height / 2, height),
            facecolors=color,
            edgecolors=INK,
            linewidth=0.45,
        )
        if not bool(row["strict_conformance"]):
            ax.plot(
                start + row["duration_seconds"],
                y,
                marker="x",
                markersize=5,
                markeredgewidth=1.2,
                color=ORANGE,
                clip_on=False,
            )
            ax.text(
                start + row["duration_seconds"] + 12,
                y,
                "peer read",
                va="center",
                fontsize=6.5,
                color=ORANGE,
            )
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([f"S{page}" for page in rows["page"]])
    ax.invert_yaxis()
    ax.set_xlabel("Elapsed time (s)")
    ax.set_title(title, loc="left", fontweight="bold")
    ax.grid(axis="x")
    ax.text(
        0.97,
        0.91,
        f"compression {compression}",
        transform=ax.transAxes,
        ha="right",
        va="top",
        fontsize=6.5,
        color=INK,
        bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.82, "pad": 1.5},
    )


def main() -> None:
    setup_style()
    distance = pd.read_csv(DATA / "pilot_a_distance_distribution.csv")
    workers = pd.read_csv(DATA / "pilot_b_worker_intervals.csv")

    fig = plt.figure(figsize=(7.0, 3.15), constrained_layout=True)
    grid = fig.add_gridspec(1, 3, width_ratios=[0.95, 1.15, 1.2])

    ax0 = fig.add_subplot(grid[0, 0])
    bars = ax0.bar(
        distance["distance_bin"],
        distance["checks"],
        color=[TEAL, TEAL, TEAL, CYAN],
        edgecolor=INK,
        linewidth=0.45,
        width=0.72,
    )
    ax0.bar_label(bars, padding=2, fontsize=7)
    ax0.set_ylabel("Source-to-target checks")
    ax0.set_xlabel("Dependency distance (pages)")
    ax0.set_title("A  Checks by span", loc="left", fontweight="bold")
    ax0.grid(axis="y")
    ax0.set_ylim(0, max(distance["checks"]) * 1.18)

    run3 = workers[
        workers["run_id"] == "skill_enum3_exclusion_0727_0330_adhoc"
    ]
    run6 = workers[workers["run_id"] == "skill_enum6_0727_0305_adhoc"]
    draw_interval_panel(
        fig.add_subplot(grid[0, 1]),
        run3,
        "B  Legacy trace · 3 pages",
        "1.86×",
    )
    ax2 = fig.add_subplot(grid[0, 2])
    draw_interval_panel(ax2, run6, "C  Legacy trace · 6 pages", "2.85×")

    png_path = OUTPUT / "fig_pilot_audit.png"
    svg_path = OUTPUT / "fig_pilot_audit.svg"
    MANUSCRIPT_OUTPUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(
        png_path,
        dpi=450,
        metadata={"Software": "MURAL pilot-audit figure builder"},
    )
    fig.savefig(
        svg_path,
        metadata={"Date": "2026-07-31", "Creator": "MURAL pilot-audit figure builder"},
    )
    shutil.copy2(png_path, MANUSCRIPT_OUTPUT / png_path.name)
    shutil.copy2(svg_path, MANUSCRIPT_OUTPUT / svg_path.name)
    plt.close(fig)


if __name__ == "__main__":
    main()
