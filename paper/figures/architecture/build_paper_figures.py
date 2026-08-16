#!/usr/bin/env python3
"""Package the author-approved MURAL diagrams for paper and repository use.

The approved PNG masters under ``approved/`` are the visual source of truth.
This script preserves those pixels for PNG consumers and wraps the same masters
as PDF/SVG assets for the paper. The older deterministic drawing helpers remain
below only as provenance; ``main`` never redraws or stylistically alters the
approved figures.
"""

from __future__ import annotations

import base64
import shutil
from datetime import datetime, timezone
from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
from matplotlib.patches import Arc, Circle, FancyArrowPatch, FancyBboxPatch, Polygon, Rectangle
from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
MANUSCRIPT_FIGURES = ROOT / "manuscript" / "figures"
FONT_FILE = HERE / "assets" / "Archivo.ttf"
APPROVED_DIR = HERE / "approved"

INK = "#203239"
TEXT_2 = "#52636A"
LINE = "#8FA2A8"
PANEL = "#F3F6F7"
BLUE = "#2F73B8"
BLUE_SOFT = "#E3F0FB"
TEAL = "#168B84"
TEAL_SOFT = "#E1F4F1"
AMBER = "#E29A2D"
AMBER_SOFT = "#FFF1D8"
CORAL = "#D85F50"
CORAL_SOFT = "#FCE7E2"
WHITE = "#FFFFFF"
BOT_CREAM = "#F6EBD2"
BOT_VISOR = "#172428"
BOT_CYAN = "#6DE7E5"

FP = FontProperties(fname=str(FONT_FILE))

mpl.rcParams.update(
    {
        "font.family": FP.get_name(),
        "svg.fonttype": "none",
        "svg.hashsalt": "mural-paper-figures-v3",
        "pdf.fonttype": 42,
        "axes.linewidth": 0,
    }
)


def setup(
    width: float,
    height: float,
    *,
    canvas_width: float | None = None,
    canvas_height: float | None = None,
) -> tuple[plt.Figure, plt.Axes]:
    fig = plt.figure(figsize=(width, height), facecolor=WHITE)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_xlim(0, canvas_width if canvas_width is not None else width)
    ax.set_ylim(0, canvas_height if canvas_height is not None else height)
    ax.set_aspect("equal")
    ax.axis("off")
    return fig, ax


def rr(
    ax: plt.Axes,
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    fill: str = WHITE,
    edge: str = LINE,
    lw: float = 1.4,
    radius: float = 0.08,
    linestyle: str = "-",
    zorder: float = 1,
) -> FancyBboxPatch:
    patch = FancyBboxPatch(
        (x, y),
        w,
        h,
        boxstyle=f"round,pad=0.012,rounding_size={radius}",
        facecolor=fill,
        edgecolor=edge,
        linewidth=lw,
        linestyle=linestyle,
        zorder=zorder,
    )
    ax.add_patch(patch)
    return patch


def text(
    ax: plt.Axes,
    x: float,
    y: float,
    value: str,
    *,
    size: float = 14,
    color: str = INK,
    weight: str = "normal",
    ha: str = "center",
    va: str = "center",
    linespacing: float = 1.05,
    rotation: float = 0,
    zorder: float = 10,
) -> None:
    ax.text(
        x,
        y,
        value,
        fontproperties=FP,
        fontsize=size,
        color=color,
        fontweight=weight,
        horizontalalignment=ha,
        verticalalignment=va,
        linespacing=linespacing,
        rotation=rotation,
        zorder=zorder,
    )


def arrow(
    ax: plt.Axes,
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    color: str = INK,
    lw: float = 1.4,
    style: str = "-|>",
    dashed: bool = False,
    curve: float = 0,
    head: float = 11,
    zorder: float = 4,
) -> FancyArrowPatch:
    patch = FancyArrowPatch(
        start,
        end,
        arrowstyle=style,
        mutation_scale=head,
        linewidth=lw,
        color=color,
        linestyle=(0, (4, 3)) if dashed else "-",
        connectionstyle=f"arc3,rad={curve}",
        shrinkA=0,
        shrinkB=0,
        zorder=zorder,
    )
    ax.add_patch(patch)
    return patch


def line(
    ax: plt.Axes,
    points: list[tuple[float, float]],
    *,
    color: str = LINE,
    lw: float = 1.2,
    dashed: bool = False,
    zorder: float = 3,
) -> None:
    xs, ys = zip(*points)
    ax.plot(
        xs,
        ys,
        color=color,
        linewidth=lw,
        linestyle=(0, (4, 3)) if dashed else "-",
        solid_capstyle="round",
        zorder=zorder,
    )


def document(
    ax: plt.Axes,
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    accent: str = BLUE,
    label: str | None = None,
    small_label: str | None = None,
    stack: bool = False,
) -> None:
    if stack:
        rr(ax, x + 0.10, y + 0.10, w, h, fill=WHITE, edge=LINE, lw=1.0, radius=0.035)
        rr(ax, x + 0.05, y + 0.05, w, h, fill=WHITE, edge=LINE, lw=1.0, radius=0.035)
    rr(ax, x, y, w, h, fill=WHITE, edge=INK, lw=1.2, radius=0.035, zorder=3)
    fold = min(0.18, w * 0.28)
    ax.add_patch(
        Polygon(
            [(x + w - fold, y + h), (x + w, y + h - fold), (x + w - fold, y + h - fold)],
            closed=True,
            facecolor="#E6ECEF",
            edgecolor=INK,
            linewidth=0.9,
            zorder=4,
        )
    )
    y_top = y + h - 0.25
    for idx, frac in enumerate((0.72, 0.58, 0.66)):
        ax.plot(
            [x + 0.12, x + 0.12 + w * frac],
            [y_top - idx * 0.16, y_top - idx * 0.16],
            color=accent if idx == 0 else LINE,
            linewidth=2.0 if idx == 0 else 1.3,
            solid_capstyle="round",
            zorder=5,
        )
    if small_label:
        text(ax, x + w / 2, y + 0.10, small_label, size=10.5, color=TEXT_2)
    if label:
        text(ax, x + w / 2, y - 0.16, label, size=13, color=TEXT_2, va="top")


def agent(
    ax: plt.Axes,
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    label: str,
    accent: str = BLUE,
    fill: str = WHITE,
    enforced: bool = False,
    label_size: float = 12.5,
) -> None:
    edge = accent if enforced else INK
    rr(ax, x, y, w, h, fill=fill, edge=edge, lw=1.45 if enforced else 1.2, radius=0.09, zorder=4)
    cx = x + w / 2
    r = min(w, h) * 0.105
    head_w = min(w * 0.56, r * 5.2)
    head_h = min(h * 0.34, r * 3.15)
    head_y = y + h * 0.53
    rr(
        ax,
        cx - head_w / 2,
        head_y,
        head_w,
        head_h,
        fill=BOT_CREAM,
        edge=INK,
        lw=0.8,
        radius=head_h * 0.28,
        zorder=6,
    )
    visor_w = head_w * 0.72
    visor_h = head_h * 0.52
    rr(
        ax,
        cx - visor_w / 2,
        head_y + head_h * 0.26,
        visor_w,
        visor_h,
        fill=BOT_VISOR,
        edge=BOT_VISOR,
        lw=0.4,
        radius=visor_h * 0.34,
        zorder=7,
    )
    eye_y = head_y + head_h * 0.53
    eye_dx = visor_w * 0.22
    for eye_x in (cx - eye_dx, cx + eye_dx):
        ax.add_patch(
            Arc(
                (eye_x, eye_y),
                visor_w * 0.20,
                visor_h * 0.34,
                angle=0,
                theta1=15,
                theta2=165,
                edgecolor=BOT_CYAN,
                linewidth=1.15,
                zorder=8,
            )
        )
    ax.add_patch(Circle((cx - head_w * 0.52, head_y + head_h * 0.50), head_h * 0.16, facecolor=accent, edgecolor=INK, linewidth=0.55, zorder=7))
    ax.add_patch(Circle((cx + head_w * 0.52, head_y + head_h * 0.50), head_h * 0.16, facecolor=accent, edgecolor=INK, linewidth=0.55, zorder=7))
    ax.plot([cx, cx], [head_y + head_h, head_y + head_h * 1.18], color=INK, linewidth=0.7, zorder=7)
    ax.add_patch(Circle((cx, head_y + head_h * 1.23), head_h * 0.08, facecolor=accent, edgecolor=INK, linewidth=0.45, zorder=8))
    text(ax, cx, y + h * 0.20, label, size=label_size, weight="semibold")


def slide(
    ax: plt.Axes,
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    label: str | None = None,
    accent: str = BLUE,
    owned: bool = False,
    label_size: float = 10.5,
) -> None:
    rr(
        ax,
        x,
        y,
        w,
        h,
        fill=WHITE,
        edge=accent if owned else INK,
        lw=1.55 if owned else 1.15,
        radius=0.035,
        zorder=4,
    )
    ax.add_patch(Rectangle((x + 0.08, y + h - 0.16), w * 0.48, 0.055, facecolor=accent, edgecolor="none", zorder=5))
    ax.add_patch(Rectangle((x + 0.08, y + 0.10), w * 0.38, h * 0.38, facecolor="#DCE7ED", edgecolor="none", zorder=5))
    ax.add_patch(Rectangle((x + w * 0.53, y + 0.10), w * 0.34, h * 0.22, facecolor="#E8EEF1", edgecolor="none", zorder=5))
    if label:
        text(ax, x + w / 2, y - 0.12, label, size=label_size, color=TEXT_2, va="top")


def slide_stack(
    ax: plt.Axes,
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    label: str | None = None,
    accent: str = TEAL,
    count: int = 3,
) -> None:
    """Draw a compact multi-slide output owned by one production group."""
    offsets = [(0.055 * idx, 0.055 * idx) for idx in range(max(1, count) - 1, -1, -1)]
    for dx, dy in offsets:
        slide(ax, x + dx, y + dy, w, h, accent=accent, owned=True)
    if label:
        text(ax, x + w / 2 + 0.055 * (count - 1), y - 0.10, label, size=9.0, color=TEXT_2, va="top")


def state_stack(
    ax: plt.Axes,
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    label: str,
    accent: str = TEAL,
    label_size: float = 12.5,
) -> None:
    for dx, dy in ((0.12, 0.12), (0.06, 0.06), (0, 0)):
        rr(ax, x + dx, y + dy, w, h, fill=WHITE, edge=accent, lw=1.15, radius=0.045, zorder=3 + dx)
    ax.plot(
        [x + 0.13, x + w * 0.76],
        [y + h - 0.19, y + h - 0.19],
        color=accent,
        linewidth=2.2,
        solid_capstyle="round",
        zorder=6,
    )
    ax.plot(
        [x + 0.13, x + w * 0.62],
        [y + h - 0.37, y + h - 0.37],
        color=LINE,
        linewidth=1.5,
        solid_capstyle="round",
        zorder=6,
    )
    text(ax, x + w / 2 + 0.05, y - 0.14, label, size=label_size, color=TEXT_2, va="top")


def process(
    ax: plt.Axes,
    x: float,
    y: float,
    w: float,
    h: float,
    label: str,
    *,
    fill: str = WHITE,
    edge: str = INK,
    size: float = 13,
    weight: str = "semibold",
    dashed: bool = False,
) -> None:
    rr(
        ax,
        x,
        y,
        w,
        h,
        fill=fill,
        edge=edge,
        lw=1.25,
        radius=0.075,
        linestyle=(0, (4, 3)) if dashed else "-",
        zorder=4,
    )
    text(ax, x + w / 2, y + h / 2, label, size=size, weight=weight)


def pill(
    ax: plt.Axes,
    x: float,
    y: float,
    w: float,
    h: float,
    label: str,
    *,
    fill: str = WHITE,
    edge: str = LINE,
    color: str = TEXT_2,
    size: float = 10.5,
) -> None:
    rr(ax, x, y, w, h, fill=fill, edge=edge, lw=0.9, radius=h / 2, zorder=4)
    text(ax, x + w / 2, y + h / 2, label, size=size, color=color)


def panel(ax: plt.Axes, x: float, y: float, w: float, h: float, index: str, title: str, *, method: bool = False) -> None:
    rr(
        ax,
        x,
        y,
        w,
        h,
        fill=TEAL_SOFT if method else PANEL,
        edge=TEAL if method else "#CCD4D8",
        lw=1.35,
        radius=0.11,
        zorder=0,
    )
    ax.plot([x, x + w], [y + h - 0.58, y + h - 0.58], color=TEAL if method else "#CCD4D8", linewidth=1.1, zorder=2)
    text(ax, x + 0.22, y + h - 0.29, f"({index})", size=15, color=TEXT_2, ha="left")
    if len(title) > 16:
        text(ax, x + 0.72, y + h - 0.29, title, size=13.8, weight="semibold", ha="left")
    else:
        text(ax, x + w / 2, y + h - 0.29, title, size=18, weight="semibold")


def _build_figure1_v2() -> plt.Figure:
    # Keep a 14×4.8 logical grid.  A 9-inch source width makes the final
    # \textwidth scaling approximately 0.75, keeping the smallest labels near
    # normal paper-caption size without increasing the figure's page height.
    fig, ax = setup(9.0, 3.086, canvas_width=14.0, canvas_height=4.8)
    y0, ph = 0.25, 4.30
    panel_specs = ((0.15, 4.05), (4.35, 4.05), (8.55, 5.30))

    # (a) One evolving deck state carried through a serial page path.
    x0, pw = panel_specs[0]
    panel(ax, x0, y0, pw, ph, "a", "Sequential")
    document(ax, x0 + 0.24, y0 + 2.55, 0.66, 0.84, accent=BLUE, stack=True)
    text(ax, x0 + 0.70, y0 + 2.38, "deck context", size=10.8, color=TEXT_2, va="top")
    agent(ax, x0 + 1.47, y0 + 2.55, 0.88, 0.88, label="Agent", accent=BLUE)
    arrow(ax, (x0 + 0.97, y0 + 2.98), (x0 + 1.43, y0 + 2.98), color=TEXT_2)
    slide_x = [x0 + 0.29, x0 + 1.19, x0 + 2.09, x0 + 2.99]
    slide_labels = ["Slide 1", "Slide 2", "…", "Slide N"]
    for xx, lab in zip(slide_x, slide_labels):
        if lab == "…":
            text(ax, xx + 0.31, y0 + 1.27, "···", size=20, color=TEXT_2)
        else:
            slide(ax, xx, y0 + 0.93, 0.62, 0.50, label=lab, accent=BLUE)
    arrow(ax, (x0 + 1.91, y0 + 2.49), (x0 + 0.60, y0 + 1.49), color=TEXT_2, curve=0.10)
    for left, right in zip(slide_x, slide_x[1:]):
        arrow(ax, (left + 0.64, y0 + 1.18), (right - 0.03, y0 + 1.18), color=TEXT_2, lw=1.15, head=9)
    arrow(
        ax,
        (x0 + 3.31, y0 + 1.51),
        (x0 + 2.30, y0 + 3.34),
        color=CORAL,
        lw=1.25,
        dashed=True,
        curve=0.28,
        head=9,
    )
    text(ax, x0 + 3.05, y0 + 2.50, "growing\nhistory", size=11.8, color=CORAL)
    line(ax, [(x0 + 0.28, y0 + 0.55), (x0 + 3.62, y0 + 0.55)], color=TEXT_2, lw=1.0)
    line(ax, [(x0 + 0.28, y0 + 0.55), (x0 + 0.28, y0 + 0.69)], color=TEXT_2, lw=1.0)
    line(ax, [(x0 + 3.62, y0 + 0.55), (x0 + 3.62, y0 + 0.69)], color=TEXT_2, lw=1.0)
    text(ax, x0 + 1.95, y0 + 0.37, "shared evolving state", size=11.8, color=TEXT_2)

    # (b) Parallel agents receive complete replicated context. Their pages still
    # require ordinary assembly; assembly itself is not the proposed distinction.
    x0, pw = panel_specs[1]
    panel(ax, x0, y0, pw, ph, "b", "Full-context parallel")
    state_stack(ax, x0 + 0.28, y0 + 3.02, 1.02, 0.55, label="full deck context", accent=CORAL, label_size=10.2)
    context_bus_x = x0 + 0.79
    line(ax, [(context_bus_x, y0 + 3.01), (context_bus_x, y0 + 0.83)], color=CORAL, lw=1.15, dashed=True)
    ys = [y0 + 2.10, y0 + 1.38, y0 + 0.66]
    labels = ["01", "02", "N"]
    merge_x = x0 + 2.89
    for yy, label_value in zip(ys, labels):
        arrow(ax, (context_bus_x, yy + 0.25), (x0 + 1.01, yy + 0.25), color=CORAL, lw=1.05, dashed=True, head=7)
        agent(ax, x0 + 1.05, yy, 0.84, 0.50, label=f"Agent {label_value}", accent=BLUE, label_size=9.3)
        arrow(ax, (x0 + 1.92, yy + 0.25), (x0 + 2.14, yy + 0.25), color=TEXT_2, lw=1.0, head=7)
        slide(ax, x0 + 2.18, yy + 0.04, 0.56, 0.40, accent=BLUE)
        line(ax, [(x0 + 2.77, yy + 0.24), (merge_x, yy + 0.24)], color=TEXT_2, lw=0.95)
    line(ax, [(merge_x, ys[-1] + 0.24), (merge_x, ys[0] + 0.24)], color=TEXT_2, lw=0.95)
    process(
        ax,
        x0 + 3.06,
        y0 + 1.18,
        0.74,
        0.74,
        "assemble\npages",
        fill=WHITE,
        edge=LINE,
        size=10.2,
    )
    arrow(ax, (merge_x, y0 + 1.55), (x0 + 3.02, y0 + 1.55), color=TEXT_2, lw=1.0, head=7)
    text(ax, x0 + 1.91, y0 + 0.36, "independent slide ownership", size=10.2, color=TEXT_2)

    # (c) MURAL externalizes shared deck state, compiles related slides into
    # production groups, closes each group jointly, and then restores a
    # whole-deck decision after deterministic assembly.
    x0, pw = panel_specs[2]
    panel(ax, x0, y0, pw, ph, "c", "MURAL", method=True)
    process(ax, x0 + 0.24, y0 + 3.13, 1.20, 0.46, "research + plan", fill=WHITE, edge=TEAL, size=9.4)
    state_stack(ax, x0 + 1.78, y0 + 3.13, 1.18, 0.46, label="shared deck blueprint", accent=TEAL, label_size=9.2)
    arrow(ax, (x0 + 1.48, y0 + 3.36), (x0 + 1.74, y0 + 3.36), color=TEAL, lw=1.25, head=8)

    group_bus_x = x0 + 0.49
    group_bus_y = y0 + 2.65
    line(
        ax,
        [(x0 + 3.08, y0 + 3.12), (x0 + 3.08, group_bus_y), (group_bus_x, group_bus_y)],
        color=TEAL,
        lw=1.15,
    )
    text(ax, x0 + 0.24, y0 + 2.79, "group briefs", size=9.6, color=TEAL, ha="left")

    ys = [y0 + 2.05, y0 + 1.34, y0 + 0.63]
    labels = ["G1", "G2", "GN"]
    group_names = ["Group 1", "Group 2", "Group N"]
    merge_x = x0 + 3.52
    for yy, label_value, group_name in zip(ys, labels, group_names):
        document(ax, x0 + 0.25, yy, 0.48, 0.46, accent=TEAL, small_label=label_value)
        line(ax, [(group_bus_x, group_bus_y), (group_bus_x, yy + 0.23)], color=TEAL, lw=1.0)
        arrow(ax, (x0 + 0.76, yy + 0.23), (x0 + 1.02, yy + 0.23), color=TEAL, lw=1.05, head=7)
        agent(ax, x0 + 1.06, yy - 0.01, 1.00, 0.50, label=group_name, accent=TEAL, fill=WHITE, enforced=True, label_size=9.4)
        arrow(ax, (x0 + 2.09, yy + 0.23), (x0 + 2.32, yy + 0.23), color=TEXT_2, lw=1.0, head=7)
        slide_stack(ax, x0 + 2.36, yy + 0.04, 0.45, 0.34, accent=TEAL, count=3 if label_value != "GN" else 2)
        line(ax, [(x0 + 3.02, yy + 0.23), (merge_x, yy + 0.23)], color=TEXT_2, lw=0.95)
    line(ax, [(merge_x, ys[-1] + 0.23), (merge_x, ys[0] + 0.23)], color=TEXT_2, lw=0.95)

    process(
        ax,
        x0 + 3.76,
        y0 + 1.96,
        1.22,
        0.53,
        "assemble +\nfinalize",
        fill=WHITE,
        edge=AMBER,
        size=10.7,
    )
    process(
        ax,
        x0 + 3.76,
        y0 + 0.83,
        1.22,
        0.68,
        "whole-deck\nReview",
        fill=AMBER_SOFT,
        edge=AMBER,
        size=10.9,
    )
    arrow(ax, (merge_x, y0 + 2.23), (x0 + 3.72, y0 + 2.23), color=TEXT_2, lw=1.0, head=7)
    arrow(ax, (x0 + 4.37, y0 + 1.92), (x0 + 4.37, y0 + 1.55), color=AMBER, lw=1.05, head=7)
    arrow(
        ax,
        (x0 + 3.72, y0 + 1.15),
        (x0 + 3.02, y0 + 1.15),
        color=AMBER,
        lw=0.95,
        dashed=True,
        curve=0.10,
        head=7,
    )
    text(ax, x0 + 1.84, y0 + 0.35, "group ownership + deck review", size=9.8, color=TEAL)

    return fig


def stage(
    ax: plt.Axes,
    x: float,
    y: float,
    w: float,
    h: float,
    number: str,
    title_value: str,
    *,
    fill: str,
    edge: str,
) -> None:
    rr(ax, x, y, w, h, fill=fill, edge=edge, lw=1.25, radius=0.11, zorder=0)
    ax.add_patch(Circle((x + 0.30, y + h - 0.33), 0.17, facecolor=edge, edgecolor="none", zorder=5))
    text(ax, x + 0.30, y + h - 0.33, number, size=13, color=WHITE, weight="semibold")
    title_size = 13.8 if len(title_value) > 20 else 15.8
    text(ax, x + 0.58, y + h - 0.33, title_value, size=title_size, weight="semibold", ha="left")
    ax.plot([x, x + w], [y + h - 0.66, y + h - 0.66], color=edge, linewidth=1.0, zorder=2)


def contact_sheet(ax: plt.Axes, x: float, y: float, w: float, h: float, *, accent: str) -> None:
    rr(ax, x, y, w, h, fill=WHITE, edge=accent, lw=1.1, radius=0.045, zorder=4)
    gap_x = w * 0.07
    gap_y = h * 0.10
    cell_w = (w - gap_x * 4) / 3
    cell_h = (h - gap_y * 3) / 2
    for row in range(2):
        for col in range(3):
            cx = x + gap_x + col * (cell_w + gap_x)
            cy = y + gap_y + (1 - row) * (cell_h + gap_y)
            ax.add_patch(
                Rectangle(
                    (cx, cy),
                    cell_w,
                    cell_h,
                    facecolor="#E5ECEF" if (row + col) % 2 else "#D7E5E3",
                    edgecolor=LINE,
                    linewidth=0.45,
                    zorder=5,
                )
            )
            ax.add_patch(
                Rectangle(
                    (cx + cell_w * 0.08, cy + cell_h * 0.68),
                    cell_w * 0.48,
                    cell_h * 0.10,
                    facecolor=accent,
                    edgecolor="none",
                    zorder=6,
                )
            )


def _build_figure2_v2() -> plt.Figure:
    fig, ax = setup(14.0, 6.2)
    y0, sh = 0.28, 5.64
    stage(ax, 0.10, y0, 3.22, sh, "1", "Material & research", fill=BLUE_SOFT, edge=BLUE)
    stage(ax, 3.44, y0, 3.36, sh, "2", "Plan & compile", fill=CORAL_SOFT, edge=CORAL)
    stage(ax, 6.92, y0, 3.38, sh, "3", "Group authoring", fill=TEAL_SOFT, edge=TEAL)
    stage(ax, 10.42, y0, 3.46, sh, "4", "Review, revise & deliver", fill=AMBER_SOFT, edge=AMBER)

    # 1. Material and research: the orchestrator decides which evidence paths
    # are needed, while optional attachments remain explicit inputs.
    document(ax, 0.35, 4.45, 0.52, 0.66, accent=BLUE)
    document(ax, 1.02, 4.45, 0.52, 0.66, accent=BLUE, stack=True)
    text(ax, 0.61, 4.30, "user\nbrief", size=8.4, color=TEXT_2, va="top")
    text(ax, 1.28, 4.30, "attachments\n(optional)", size=7.8, color=TEXT_2, va="top")
    agent(ax, 1.91, 4.37, 1.07, 0.78, label="Orchestrator", accent=BLUE, fill=WHITE, enforced=True, label_size=9.3)
    line(ax, [(0.88, 4.78), (1.72, 4.78)], color=BLUE, lw=1.05)
    line(ax, [(1.55, 4.78), (1.72, 4.78)], color=BLUE, lw=1.05)
    arrow(ax, (1.72, 4.78), (1.87, 4.78), color=BLUE, lw=1.1, head=8)

    agent(ax, 0.48, 2.92, 1.08, 0.76, label="Material", accent=BLUE, fill=WHITE, label_size=10.0)
    agent(ax, 1.86, 2.92, 1.08, 0.76, label="Research", accent=BLUE, fill=WHITE, label_size=10.0)
    line(ax, [(2.44, 4.33), (2.44, 3.96), (1.02, 3.96), (1.02, 3.72)], color=BLUE, lw=1.05)
    arrow(ax, (1.02, 3.72), (1.02, 3.71), color=BLUE, lw=1.05, head=8)
    arrow(ax, (2.44, 3.96), (2.40, 3.72), color=BLUE, lw=1.05, head=8)
    text(ax, 0.50, 2.63, "if supplied", size=8.3, color=TEXT_2, ha="left")
    pill(ax, 0.42, 1.90, 1.17, 0.40, "material.md", fill=WHITE, edge=BLUE, size=9.4)
    pill(ax, 1.73, 1.90, 1.28, 0.40, "knowledge brief", fill=WHITE, edge=BLUE, size=8.8)
    arrow(ax, (1.02, 2.88), (1.02, 2.34), color=BLUE, lw=1.0, head=8)
    arrow(ax, (2.40, 2.88), (2.40, 2.34), color=BLUE, lw=1.0, head=8)
    process(ax, 0.42, 0.73, 2.59, 0.58, "evidence · constraints · visual cues", fill=WHITE, edge=BLUE, size=9.4)
    arrow(ax, (1.71, 1.86), (1.71, 1.35), color=BLUE, lw=1.0, head=8)

    # 2. Planning externalizes global choices into a shared blueprint and
    # compiles narratively related slides into executable group briefs.
    agent(ax, 3.73, 4.37, 1.05, 0.78, label="Orchestrator", accent=CORAL, fill=WHITE, enforced=True, label_size=9.2)
    state_stack(ax, 5.26, 4.41, 0.98, 0.52, label="shared deck blueprint", accent=CORAL, label_size=8.7)
    arrow(ax, (4.82, 4.76), (5.22, 4.76), color=CORAL, lw=1.2, head=9)
    process(ax, 4.48, 3.43, 1.30, 0.50, "validate + compile", fill=WHITE, edge=CORAL, size=10.0)
    arrow(ax, (5.75, 4.38), (5.14, 3.97), color=CORAL, lw=1.1, head=8)
    pill(ax, 3.72, 2.69, 0.92, 0.38, "deck.md", fill=WHITE, edge=CORAL, size=9.5)
    pill(ax, 4.78, 2.69, 1.02, 0.38, "theme tokens", fill=WHITE, edge=CORAL, size=8.6)
    pill(ax, 5.94, 2.69, 0.62, 0.38, "assets", fill=WHITE, edge=CORAL, size=9.2)
    line(ax, [(5.13, 3.40), (5.13, 3.22), (4.18, 3.22), (4.18, 3.10)], color=CORAL, lw=1.0)
    line(ax, [(5.13, 3.22), (5.29, 3.22), (5.29, 3.10)], color=CORAL, lw=1.0)
    line(ax, [(5.13, 3.22), (6.25, 3.22), (6.25, 3.10)], color=CORAL, lw=1.0)
    text(ax, 5.12, 2.30, "group briefs + shared skeletons", size=9.6, color=CORAL, weight="semibold")
    brief_xs = [3.78, 4.86, 5.94]
    for bx, code in zip(brief_xs, ("G1", "G2", "GN")):
        document(ax, bx, 1.41, 0.56, 0.58, accent=CORAL, small_label=code)
    line(ax, [(5.13, 2.66), (5.13, 2.12), (4.06, 2.12), (4.06, 2.03)], color=CORAL, lw=1.0)
    line(ax, [(5.13, 2.12), (5.14, 2.12), (5.14, 2.03)], color=CORAL, lw=1.0)
    line(ax, [(5.13, 2.12), (6.22, 2.12), (6.22, 2.03)], color=CORAL, lw=1.0)
    process(ax, 3.78, 0.62, 2.72, 0.50, "explicit state for downstream agents", fill=WHITE, edge=CORAL, size=9.1)

    # Stage handoff: material/research artifacts become part of the planning
    # state, and compiled briefs are the ownership boundary for group agents.
    arrow(ax, (3.05, 1.01), (3.39, 1.01), color=CORAL, lw=1.2, head=9)
    arrow(ax, (6.55, 1.70), (6.87, 1.70), color=TEAL, lw=1.2, head=9)

    # 3. A single centralized image stage prepares reusable assets. Each group
    # agent then jointly authors and visually closes all slides in its group.
    agent(ax, 7.18, 4.40, 1.05, 0.76, label="Image", accent=TEAL, fill=WHITE, label_size=10.0)
    process(ax, 8.70, 4.49, 1.18, 0.54, "asset catalog", fill=WHITE, edge=TEAL, size=10.0)
    arrow(ax, (8.27, 4.78), (8.66, 4.78), color=TEAL, lw=1.2, head=9)
    text(ax, 8.53, 4.18, "one shared image stage", size=8.8, color=TEXT_2)

    group_rows = [3.23, 2.20, 1.17]
    group_labels = ["Group 1", "Group 2", "Group N"]
    group_codes = ["G1", "G2", "GN"]
    for yy, group_label, group_code in zip(group_rows, group_labels, group_codes):
        document(ax, 7.16, yy + 0.06, 0.48, 0.50, accent=TEAL, small_label=group_code)
        agent(ax, 7.89, yy, 1.04, 0.64, label=group_label, accent=TEAL, fill=WHITE, enforced=True, label_size=9.2)
        slide_stack(ax, 9.34, yy + 0.10, 0.42, 0.34, accent=TEAL, count=3 if group_code != "GN" else 2)
        arrow(ax, (7.68, yy + 0.32), (7.85, yy + 0.32), color=TEAL, lw=1.0, head=7)
        arrow(ax, (8.97, yy + 0.32), (9.30, yy + 0.32), color=INK, lw=1.0, head=7)
    asset_bus_x = 9.08
    line(ax, [(9.29, 4.46), (asset_bus_x, 4.16), (asset_bus_x, 1.49)], color=TEAL, lw=0.95, dashed=True)
    for yy in group_rows:
        arrow(ax, (asset_bus_x, yy + 0.50), (8.97, yy + 0.50), color=TEAL, lw=0.9, dashed=True, head=6)
    pill(ax, 7.56, 0.56, 2.06, 0.40, "render · inspect · revise", fill=WHITE, edge=TEAL, color=TEAL, size=9.2)
    line(ax, [(9.90, 1.42), (10.02, 1.42), (10.02, 3.60), (9.90, 3.60)], color=TEAL, lw=1.1)
    text(ax, 9.96, 2.49, "parallel groups", size=8.0, color=TEAL, rotation=90, weight="semibold")

    # 4. Deterministic assembly restores the deck view. Review and later human
    # edits share the same scope-aware router: page patch, group replay, or replan.
    process(ax, 10.72, 4.54, 1.43, 0.58, "assemble + finalize", fill=WHITE, edge=AMBER, size=10.2)
    state_stack(ax, 12.55, 4.57, 0.78, 0.50, label="rendered deck", accent=AMBER, label_size=8.8)
    arrow(ax, (12.19, 4.83), (12.51, 4.83), color=AMBER, lw=1.15, head=8)
    contact_sheet(ax, 10.76, 3.22, 1.40, 0.80, accent=AMBER)
    text(ax, 11.46, 3.04, "contact sheet", size=8.9, color=TEXT_2, va="top")
    agent(ax, 12.45, 3.11, 1.12, 0.92, label="Whole-deck\nReview", accent=AMBER, fill=WHITE, enforced=True, label_size=8.7)
    arrow(ax, (12.95, 4.53), (13.01, 4.07), color=AMBER, lw=1.05, head=8)
    arrow(ax, (12.20, 3.62), (12.41, 3.62), color=AMBER, lw=1.05, head=8)
    process(ax, 10.74, 2.03, 1.43, 0.56, "revision router", fill=WHITE, edge=AMBER, size=10.0)
    pill(ax, 12.45, 2.12, 1.13, 0.38, "human edit", fill=WHITE, edge=AMBER, size=9.0)
    arrow(ax, (12.97, 3.07), (11.50, 2.63), color=AMBER, lw=1.0, curve=0.10, head=8)
    arrow(ax, (12.41, 2.31), (12.21, 2.31), color=AMBER, lw=1.0, head=7)

    route_y = 1.31
    pill(ax, 10.58, route_y, 0.92, 0.36, "page patch", fill=WHITE, edge=AMBER, size=8.2)
    pill(ax, 11.63, route_y, 0.92, 0.36, "group replay", fill=WHITE, edge=AMBER, size=8.0)
    pill(ax, 12.68, route_y, 0.92, 0.36, "deck replan", fill=WHITE, edge=AMBER, size=8.0)
    line(ax, [(11.46, 2.00), (11.46, 1.82), (11.04, 1.82), (11.04, 1.71)], color=AMBER, lw=0.95)
    line(ax, [(11.46, 1.82), (12.09, 1.82), (12.09, 1.71)], color=AMBER, lw=0.95)
    line(ax, [(11.46, 1.82), (13.14, 1.82), (13.14, 1.71)], color=AMBER, lw=0.95)

    process(ax, 12.44, 0.49, 1.22, 0.54, "HTML · PPTX\nPDF · PNG", fill=WHITE, edge=TEAL, size=8.6)
    arrow(ax, (13.41, 3.08), (13.10, 1.08), color=TEAL, lw=1.05, curve=-0.12, head=8)
    text(ax, 13.52, 2.78, "accept", size=8.0, color=TEAL)

    # Scope-aware replay loops. The long lower rail makes the lifecycle—not
    # merely initial generation—visible in the main method figure.
    arrow(ax, (11.04, 1.27), (11.38, 4.50), color=AMBER, lw=0.9, dashed=True, curve=-0.28, head=7)
    arrow(ax, (11.60, 1.46), (10.26, 1.48), color=AMBER, lw=1.0, dashed=True, head=7)
    line(ax, [(13.14, 1.27), (13.14, 0.39), (5.14, 0.39)], color=AMBER, lw=0.95, dashed=True)
    arrow(ax, (5.14, 0.39), (5.14, 3.38), color=AMBER, lw=0.95, dashed=True, head=7)
    text(ax, 8.76, 0.43, "replan only when the edit changes deck-level decisions", size=7.6, color=AMBER)

    # Group outputs converge on deterministic finalization.
    merge_x = 10.18
    line(ax, [(merge_x, 1.48), (merge_x, 4.83)], color=INK, lw=0.95)
    for yy in group_rows:
        line(ax, [(9.95, yy + 0.31), (merge_x, yy + 0.31)], color=INK, lw=0.9)
    arrow(ax, (merge_x, 4.83), (10.68, 4.83), color=INK, lw=1.05, head=8)

    return fig


def panel_v3(
    ax: plt.Axes,
    x: float,
    y: float,
    w: float,
    h: float,
    index: str,
    title_value: str,
    *,
    fill: str,
    accent: str,
) -> None:
    """High-contrast comparison panel used by the camera-ready topology figure."""
    rr(ax, x, y, w, h, fill=fill, edge=accent, lw=1.55, radius=0.12, zorder=0)
    ax.add_patch(
        Rectangle(
            (x + 0.03, y + h - 0.67),
            w - 0.06,
            0.64,
            facecolor=WHITE,
            edgecolor="none",
            zorder=1,
        )
    )
    ax.plot(
        [x + 0.03, x + w - 0.03],
        [y + h - 0.67, y + h - 0.67],
        color=accent,
        linewidth=1.35,
        zorder=2,
    )
    ax.plot(
        [x + 0.16, x + w - 0.16],
        [y + h - 0.10, y + h - 0.10],
        color=accent,
        linewidth=3.0,
        solid_capstyle="round",
        zorder=2,
    )
    text(ax, x + 0.22, y + h - 0.37, f"({index})", size=14.8, color=accent, weight="semibold", ha="left")
    title_size = 11.7 if len(title_value) > 16 else 15.5
    text(ax, x + w / 2 + 0.10, y + h - 0.37, title_value, size=title_size, color=accent, weight="semibold")


def stage_v3(
    ax: plt.Axes,
    x: float,
    y: float,
    w: float,
    h: float,
    number: str,
    title_value: str,
    *,
    fill: str,
    accent: str,
) -> None:
    """Lifecycle stage with a stronger header that survives paper-width scaling."""
    rr(ax, x, y, w, h, fill=fill, edge=accent, lw=1.45, radius=0.12, zorder=0)
    ax.add_patch(
        Rectangle(
            (x + 0.03, y + h - 0.73),
            w - 0.06,
            0.70,
            facecolor=WHITE,
            edgecolor="none",
            zorder=1,
        )
    )
    ax.plot(
        [x + 0.03, x + w - 0.03],
        [y + h - 0.73, y + h - 0.73],
        color=accent,
        linewidth=1.25,
        zorder=2,
    )
    ax.add_patch(Circle((x + 0.32, y + h - 0.37), 0.19, facecolor=accent, edgecolor="none", zorder=5))
    text(ax, x + 0.32, y + h - 0.37, number, size=13.5, color=WHITE, weight="semibold")
    if len(title_value) > 22:
        title_size = 10.0
    elif len(title_value) > 17:
        title_size = 10.8
    elif len(title_value) > 14:
        title_size = 11.8
    else:
        title_size = 12.6
    text(ax, x + 0.62, y + h - 0.37, title_value, size=title_size, color=accent, weight="semibold", ha="left")


def build_figure1() -> plt.Figure:
    """Responsibility topologies with a compact post-generation replay path."""
    fig, ax = setup(9.2, 3.22, canvas_width=14.4, canvas_height=5.04)
    y0, ph = 0.18, 4.68
    panel_specs = ((0.12, 3.82), (4.06, 3.82), (8.00, 6.28))

    # (a) Sequential generation carries one evolving trajectory across pages.
    x0, pw = panel_specs[0]
    panel_v3(ax, x0, y0, pw, ph, "a", "Sequential", fill=BLUE_SOFT, accent=BLUE)
    document(ax, x0 + 0.27, y0 + 3.34, 0.66, 0.72, accent=BLUE, stack=True)
    text(ax, x0 + 0.70, y0 + 3.16, "deck context", size=10.7, color=TEXT_2, va="top")
    agent(ax, x0 + 1.48, y0 + 3.27, 0.92, 0.82, label="Agent", accent=BLUE, label_size=10.8)
    arrow(ax, (x0 + 1.00, y0 + 3.70), (x0 + 1.44, y0 + 3.70), color=BLUE, lw=1.2, head=8)
    slide_x = [x0 + 0.27, x0 + 1.15, x0 + 2.03, x0 + 2.91]
    slide_labels = ["Slide 1", "Slide 2", "…", "Slide N"]
    for xx, lab in zip(slide_x, slide_labels):
        if lab == "…":
            text(ax, xx + 0.30, y0 + 1.73, "···", size=20, color=TEXT_2)
        else:
            slide(ax, xx, y0 + 1.46, 0.60, 0.50, label=lab, accent=BLUE, label_size=10.0)
    arrow(ax, (x0 + 1.94, y0 + 3.22), (x0 + 0.58, y0 + 2.02), color=BLUE, curve=0.09, head=8)
    for left, right in zip(slide_x, slide_x[1:]):
        arrow(ax, (left + 0.62, y0 + 1.71), (right - 0.03, y0 + 1.71), color=INK, lw=1.1, head=8)
    arrow(
        ax,
        (x0 + 3.23, y0 + 2.03),
        (x0 + 2.33, y0 + 3.56),
        color=CORAL,
        lw=1.25,
        dashed=True,
        curve=0.26,
        head=8,
    )
    text(ax, x0 + 3.10, y0 + 2.82, "growing\nhistory", size=11.0, color=CORAL)
    line(ax, [(x0 + 0.27, y0 + 0.67), (x0 + 3.52, y0 + 0.67)], color=BLUE, lw=1.05)
    line(ax, [(x0 + 0.27, y0 + 0.67), (x0 + 0.27, y0 + 0.84)], color=BLUE, lw=1.05)
    line(ax, [(x0 + 3.52, y0 + 0.67), (x0 + 3.52, y0 + 0.84)], color=BLUE, lw=1.05)
    text(ax, x0 + 1.90, y0 + 0.43, "shared evolving state", size=10.9, color=BLUE, weight="semibold")

    # (b) Full-context workers independently own one slide and merge afterward.
    x0, pw = panel_specs[1]
    panel_v3(ax, x0, y0, pw, ph, "b", "Full-context parallel", fill=CORAL_SOFT, accent=CORAL)
    state_stack(ax, x0 + 0.27, y0 + 3.48, 1.05, 0.50, label="full deck context", accent=CORAL, label_size=10.0)
    text(ax, x0 + 0.79, y0 + 3.08, "replicated", size=9.3, color=CORAL)
    context_bus_x = x0 + 0.79
    line(ax, [(context_bus_x, y0 + 3.43), (context_bus_x, y0 + 1.17)], color=CORAL, lw=1.2, dashed=True)
    ys = [y0 + 2.62, y0 + 1.84, y0 + 1.06]
    merge_x = x0 + 2.89
    for yy, label_value in zip(ys, ("01", "02", "N")):
        arrow(ax, (context_bus_x, yy + 0.28), (x0 + 1.01, yy + 0.28), color=CORAL, lw=1.05, dashed=True, head=7)
        agent(ax, x0 + 1.05, yy, 0.88, 0.56, label=f"Agent {label_value}", accent=CORAL, label_size=9.3)
        arrow(ax, (x0 + 1.96, yy + 0.28), (x0 + 2.14, yy + 0.28), color=INK, lw=1.0, head=7)
        slide(ax, x0 + 2.18, yy + 0.07, 0.55, 0.40, accent=CORAL)
        line(ax, [(x0 + 2.76, yy + 0.27), (merge_x, yy + 0.27)], color=INK, lw=0.95)
    line(ax, [(merge_x, ys[-1] + 0.27), (merge_x, ys[0] + 0.27)], color=INK, lw=0.95)
    process(ax, x0 + 3.02, y0 + 1.67, 0.64, 0.80, "assemble\npages", fill=WHITE, edge=CORAL, size=9.6)
    arrow(ax, (merge_x, y0 + 2.07), (x0 + 2.98, y0 + 2.07), color=INK, lw=1.0, head=7)
    text(ax, x0 + 1.91, y0 + 0.43, "independent slide ownership", size=9.9, color=CORAL, weight="semibold")

    # (c) MuralPresenter compiles related pages into jointly owned groups. Review findings
    # and later edits can reactivate the affected responsibility unit.
    x0, pw = panel_specs[2]
    panel_v3(ax, x0, y0, pw, ph, "c", "MuralPresenter", fill=TEAL_SOFT, accent=TEAL)
    process(ax, x0 + 0.23, y0 + 3.55, 1.22, 0.50, "research + plan", fill=WHITE, edge=TEAL, size=9.7)
    state_stack(ax, x0 + 1.72, y0 + 3.56, 1.24, 0.48, label="shared deck blueprint", accent=TEAL, label_size=8.4)
    arrow(ax, (x0 + 1.49, y0 + 3.80), (x0 + 1.68, y0 + 3.80), color=TEAL, lw=1.25, head=8)
    group_bus_x = x0 + 0.48
    group_bus_y = y0 + 3.16
    line(ax, [(x0 + 3.08, y0 + 3.56), (x0 + 3.08, group_bus_y), (group_bus_x, group_bus_y)], color=TEAL, lw=1.15)
    pill(ax, x0 + 0.20, y0 + 3.02, 1.08, 0.30, "group briefs", fill=WHITE, edge=TEAL, color=TEAL, size=8.8)

    ys = [y0 + 2.55, y0 + 1.82, y0 + 1.09]
    group_codes = ["G1", "G2", "GN"]
    group_names = ["Group 1", "Group 2", "Group N"]
    merge_x = x0 + 3.18
    for yy, code, group_name in zip(ys, group_codes, group_names):
        document(ax, x0 + 0.23, yy, 0.48, 0.48, accent=TEAL, small_label=code)
        line(ax, [(group_bus_x, group_bus_y), (group_bus_x, yy + 0.24)], color=TEAL, lw=1.0)
        arrow(ax, (x0 + 0.74, yy + 0.24), (x0 + 1.00, yy + 0.24), color=TEAL, lw=1.05, head=7)
        agent(ax, x0 + 1.04, yy - 0.01, 1.02, 0.54, label=group_name, accent=TEAL, fill=WHITE, enforced=True, label_size=9.5)
        arrow(ax, (x0 + 2.09, yy + 0.25), (x0 + 2.29, yy + 0.25), color=INK, lw=1.0, head=7)
        slide_stack(ax, x0 + 2.33, yy + 0.06, 0.45, 0.34, accent=TEAL, count=3 if code != "GN" else 2)
        line(ax, [(x0 + 2.99, yy + 0.25), (merge_x, yy + 0.25)], color=INK, lw=0.95)
    line(ax, [(merge_x, ys[-1] + 0.25), (merge_x, ys[0] + 0.25)], color=INK, lw=0.95)
    process(ax, x0 + 3.40, y0 + 2.09, 1.14, 0.62, "assemble +\nfinalize", fill=WHITE, edge=AMBER, size=9.8)
    process(ax, x0 + 4.82, y0 + 2.01, 1.18, 0.78, "whole-deck\nReview", fill=AMBER_SOFT, edge=AMBER, size=10.0)
    arrow(ax, (merge_x, y0 + 2.40), (x0 + 3.36, y0 + 2.40), color=INK, lw=1.0, head=7)
    arrow(ax, (x0 + 4.58, y0 + 2.40), (x0 + 4.78, y0 + 2.40), color=AMBER, lw=1.1, head=8)

    process(ax, x0 + 3.36, y0 + 0.62, 1.42, 0.56, "scope affected\ngroup", fill=WHITE, edge=AMBER, size=8.3)
    pill(ax, x0 + 4.96, y0 + 0.71, 1.02, 0.38, "later edit", fill=WHITE, edge=AMBER, color=AMBER, size=9.2)
    arrow(ax, (x0 + 4.92, y0 + 0.90), (x0 + 4.82, y0 + 0.90), color=AMBER, lw=1.05, head=7)
    arrow(ax, (x0 + 5.41, y0 + 1.97), (x0 + 4.08, y0 + 1.20), color=AMBER, lw=1.0, dashed=True, curve=0.09, head=7)
    arrow(ax, (x0 + 3.33, y0 + 0.90), (x0 + 2.09, y0 + 2.08), color=AMBER, lw=1.1, dashed=True, curve=-0.20, head=8)
    text(ax, x0 + 1.63, y0 + 0.42, "joint group ownership", size=9.8, color=TEAL, weight="semibold")
    text(ax, x0 + 4.67, y0 + 0.42, "review + scoped replay", size=9.3, color=AMBER, weight="semibold")

    return fig


def build_figure2() -> plt.Figure:
    """Full MuralPresenter lifecycle with explicit acceptance and impact-aware revision."""
    fig, ax = setup(9.2, 4.09, canvas_width=14.4, canvas_height=6.4)
    y0, sh = 0.22, 5.98
    stage_v3(ax, 0.10, y0, 3.10, sh, "1", "Material & research", fill=BLUE_SOFT, accent=BLUE)
    stage_v3(ax, 3.33, y0, 3.18, sh, "2", "Plan & compile", fill=CORAL_SOFT, accent=CORAL)
    stage_v3(ax, 6.64, y0, 3.25, sh, "3", "Group authoring", fill=TEAL_SOFT, accent=TEAL)
    stage_v3(ax, 10.02, y0, 4.28, sh, "4", "Review, revise & deliver", fill=AMBER_SOFT, accent=AMBER)

    # 1. Ground user intent and optional source material before planning.
    process(ax, 0.30, 4.75, 1.00, 0.50, "user brief", fill=WHITE, edge=BLUE, size=9.5)
    process(ax, 1.48, 4.75, 1.38, 0.50, "attachments\n(optional)", fill=WHITE, edge=BLUE, size=8.8)
    line(ax, [(0.80, 4.71), (0.80, 4.58), (1.60, 4.58)], color=BLUE, lw=1.05)
    line(ax, [(2.17, 4.71), (2.17, 4.58), (1.60, 4.58)], color=BLUE, lw=1.05)
    arrow(ax, (1.60, 4.58), (1.60, 4.48), color=BLUE, lw=1.1, head=8)
    agent(ax, 1.00, 3.65, 1.22, 0.80, label="Orchestrator", accent=BLUE, fill=WHITE, enforced=True, label_size=9.6)
    agent(ax, 0.40, 2.46, 1.08, 0.76, label="Material", accent=BLUE, fill=WHITE, label_size=9.8)
    agent(ax, 1.78, 2.46, 1.08, 0.76, label="Research", accent=BLUE, fill=WHITE, label_size=9.8)
    line(ax, [(1.61, 3.61), (1.61, 3.40), (0.94, 3.40), (0.94, 3.26)], color=BLUE, lw=1.05)
    line(ax, [(1.61, 3.40), (2.32, 3.40), (2.32, 3.26)], color=BLUE, lw=1.05)
    arrow(ax, (0.94, 3.26), (0.94, 3.25), color=BLUE, lw=1.05, head=8)
    arrow(ax, (2.32, 3.26), (2.32, 3.25), color=BLUE, lw=1.05, head=8)
    text(ax, 0.42, 2.17, "if supplied", size=8.2, color=TEXT_2, ha="left")
    pill(ax, 0.34, 1.66, 1.14, 0.46, "material.md", fill=WHITE, edge=BLUE, color=BLUE, size=8.0)
    pill(ax, 1.72, 1.64, 1.14, 0.50, "knowledge\nbrief", fill=WHITE, edge=BLUE, color=BLUE, size=8.6)
    arrow(ax, (0.94, 2.42), (0.94, 2.14), color=BLUE, lw=1.0, head=7)
    arrow(ax, (2.32, 2.42), (2.32, 2.14), color=BLUE, lw=1.0, head=7)
    process(ax, 0.38, 0.68, 2.52, 0.64, "evidence · audience\nvisual cues", fill=WHITE, edge=BLUE, size=9.0)
    line(ax, [(0.94, 1.64), (0.94, 1.48), (1.64, 1.48)], color=BLUE, lw=1.0)
    line(ax, [(2.32, 1.64), (2.32, 1.48), (1.64, 1.48)], color=BLUE, lw=1.0)
    arrow(ax, (1.64, 1.48), (1.64, 1.34), color=BLUE, lw=1.0, head=8)

    # 2. Externalize deck-level decisions and compile slide-group ownership.
    agent(ax, 3.58, 4.66, 1.08, 0.82, label="Orchestrator", accent=CORAL, fill=WHITE, enforced=True, label_size=9.6)
    state_stack(ax, 5.13, 4.73, 1.00, 0.52, label="shared deck blueprint", accent=CORAL, label_size=8.3)
    arrow(ax, (4.70, 5.05), (5.09, 5.05), color=CORAL, lw=1.2, head=8)
    process(ax, 4.32, 3.66, 1.38, 0.56, "validate + compile", fill=WHITE, edge=CORAL, size=10.1)
    arrow(ax, (5.63, 4.69), (5.03, 4.26), color=CORAL, lw=1.1, head=8)
    process(ax, 3.68, 2.74, 2.48, 0.54, "deck.md · theme · skeletons", fill=WHITE, edge=CORAL, size=8.9)
    arrow(ax, (5.01, 3.62), (4.94, 3.32), color=CORAL, lw=1.0, head=8)
    text(ax, 4.92, 2.30, "compiled group briefs", size=9.2, color=CORAL, weight="semibold")
    brief_xs = [3.69, 4.75, 5.81]
    for bx, code in zip(brief_xs, ("G1", "G2", "GN")):
        document(ax, bx, 1.48, 0.57, 0.60, accent=CORAL, small_label=code)
    line(ax, [(4.92, 2.70), (4.92, 2.20), (3.98, 2.20), (3.98, 2.12)], color=CORAL, lw=1.0)
    line(ax, [(4.92, 2.20), (5.04, 2.20), (5.04, 2.12)], color=CORAL, lw=1.0)
    line(ax, [(4.92, 2.20), (6.10, 2.20), (6.10, 2.12)], color=CORAL, lw=1.0)
    process(ax, 3.68, 0.68, 2.48, 0.56, "explicit group responsibility", fill=WHITE, edge=CORAL, size=8.8)
    arrow(ax, (3.03, 1.13), (3.28, 1.13), color=CORAL, lw=1.2, head=8)
    arrow(ax, (6.20, 1.78), (6.59, 1.78), color=TEAL, lw=1.2, head=8)

    # 3. Shared assets feed parallel Group Agents; each jointly closes its pages.
    agent(ax, 6.90, 4.70, 1.08, 0.78, label="Image", accent=TEAL, fill=WHITE, label_size=10.1)
    process(ax, 8.28, 4.80, 1.36, 0.56, "asset catalog", fill=WHITE, edge=TEAL, size=9.3)
    arrow(ax, (8.02, 5.08), (8.24, 5.08), color=TEAL, lw=1.2, head=8)
    text(ax, 8.30, 4.49, "shared across groups", size=8.8, color=TEAL)
    group_rows = [3.50, 2.50, 1.50]
    group_labels = ["Group 1", "Group 2", "Group N"]
    group_codes = ["G1", "G2", "GN"]
    for yy, group_label, group_code in zip(group_rows, group_labels, group_codes):
        document(ax, 6.88, yy + 0.07, 0.48, 0.52, accent=TEAL, small_label=group_code)
        agent(ax, 7.60, yy, 1.08, 0.68, label=group_label, accent=TEAL, fill=WHITE, enforced=True, label_size=9.4)
        slide_stack(ax, 9.09, yy + 0.12, 0.42, 0.34, accent=TEAL, count=3 if group_code != "GN" else 2)
        arrow(ax, (7.40, yy + 0.34), (7.56, yy + 0.34), color=TEAL, lw=1.0, head=7)
        arrow(ax, (8.72, yy + 0.34), (9.05, yy + 0.34), color=INK, lw=1.0, head=7)
    asset_bus_x = 8.92
    line(ax, [(8.99, 4.76), (asset_bus_x, 4.50), (asset_bus_x, 1.84)], color=TEAL, lw=0.95, dashed=True)
    for yy in group_rows:
        arrow(ax, (asset_bus_x, yy + 0.55), (8.72, yy + 0.55), color=TEAL, lw=0.9, dashed=True, head=6)
    pill(ax, 6.91, 0.62, 2.66, 0.52, "write · render\ninspect · revise", fill=WHITE, edge=TEAL, color=TEAL, size=9.4)

    # Group outputs converge on deterministic finalization.
    merge_x = 9.82
    line(ax, [(merge_x, 1.84), (merge_x, 5.04)], color=INK, lw=0.95)
    for yy in group_rows:
        line(ax, [(9.67, yy + 0.34), (merge_x, yy + 0.34)], color=INK, lw=0.9)
    arrow(ax, (merge_x, 5.04), (10.26, 5.04), color=INK, lw=1.05, head=8)

    # 4. Restore the deck view, accept it, or route review/human edits by scope.
    process(ax, 10.30, 4.70, 1.45, 0.66, "assemble +\nfinalize", fill=WHITE, edge=AMBER, size=9.1)
    state_stack(ax, 12.18, 4.78, 0.82, 0.50, label="rendered deck", accent=AMBER, label_size=9.0)
    arrow(ax, (11.79, 5.03), (12.14, 5.03), color=AMBER, lw=1.15, head=8)
    contact_sheet(ax, 10.33, 3.52, 1.45, 0.82, accent=AMBER)
    text(ax, 11.05, 3.32, "contact sheet", size=9.1, color=TEXT_2, va="top")
    agent(ax, 12.08, 3.38, 1.36, 1.00, label="Whole-deck\nReview", accent=AMBER, fill=WHITE, enforced=True, label_size=9.0)
    arrow(ax, (12.59, 4.74), (12.74, 4.42), color=AMBER, lw=1.05, head=8)
    arrow(ax, (11.82, 3.93), (12.04, 3.93), color=AMBER, lw=1.05, head=8)
    process(ax, 10.32, 2.39, 1.48, 0.58, "impact router", fill=WHITE, edge=AMBER, size=10.2)
    pill(ax, 12.10, 2.49, 1.34, 0.40, "human edit", fill=WHITE, edge=AMBER, color=AMBER, size=9.4)
    arrow(ax, (12.58, 3.34), (11.55, 3.01), color=AMBER, lw=1.0, curve=0.10, head=8)
    arrow(ax, (12.06, 2.69), (11.84, 2.69), color=AMBER, lw=1.0, head=7)

    route_y = 1.42
    process(ax, 10.20, route_y, 1.10, 0.58, "page\npatch", fill=WHITE, edge=AMBER, size=10.0)
    process(ax, 11.47, route_y, 1.10, 0.58, "group\nreplay", fill=WHITE, edge=AMBER, size=10.0)
    process(ax, 12.74, route_y, 1.10, 0.58, "deck\nreplan", fill=WHITE, edge=AMBER, size=10.0)
    line(ax, [(11.06, 2.35), (11.06, 2.18), (10.75, 2.18), (10.75, 2.04)], color=AMBER, lw=0.95)
    line(ax, [(11.06, 2.18), (12.02, 2.18), (12.02, 2.04)], color=AMBER, lw=0.95)
    line(ax, [(11.06, 2.18), (13.29, 2.18), (13.29, 2.04)], color=AMBER, lw=0.95)

    # Accepted decks are delivered; each revision branch returns to its true owner.
    process(ax, 12.63, 0.48, 1.30, 0.62, "HTML · PPTX\nPDF · PNG", fill=WHITE, edge=TEAL, size=8.8)
    arrow(ax, (13.48, 3.53), (13.32, 1.15), color=TEAL, lw=1.15, curve=-0.10, head=8)
    text(ax, 13.63, 3.14, "accept", size=8.5, color=TEAL, weight="semibold")
    line(ax, [(10.75, 1.38), (10.13, 1.38), (10.13, 5.03)], color=AMBER, lw=1.0, dashed=True)
    arrow(ax, (10.13, 5.03), (10.26, 5.03), color=AMBER, lw=1.0, dashed=True, head=8)
    line(ax, [(12.02, 1.38), (12.02, 1.26), (6.70, 1.26), (6.70, 2.84)], color=AMBER, lw=1.0, dashed=True)
    arrow(ax, (6.70, 2.84), (6.84, 2.84), color=AMBER, lw=1.05, dashed=True, head=8)
    line(ax, [(13.29, 1.38), (13.29, 0.36), (6.37, 0.36)], color=AMBER, lw=1.0, dashed=True)
    line(ax, [(6.37, 0.36), (6.37, 3.94)], color=AMBER, lw=1.0, dashed=True)
    arrow(ax, (6.37, 3.94), (5.74, 3.94), color=AMBER, lw=1.0, dashed=True, head=8)

    return fig


def save(fig: plt.Figure, stem: str) -> None:
    HERE.mkdir(parents=True, exist_ok=True)
    MANUSCRIPT_FIGURES.mkdir(parents=True, exist_ok=True)
    paths = {
        "svg": HERE / f"{stem}.svg",
        "pdf": HERE / f"{stem}.pdf",
        "png": HERE / f"{stem}.png",
    }
    fixed_time = datetime(2026, 7, 28, tzinfo=timezone.utc)
    fig.savefig(
        paths["svg"],
        facecolor=WHITE,
        bbox_inches=None,
        metadata={"Date": "2026-08-07", "Creator": "MURAL figure builder"},
    )
    fig.savefig(
        paths["pdf"],
        facecolor=WHITE,
        bbox_inches=None,
        metadata={
            "CreationDate": fixed_time,
            "ModDate": fixed_time,
            "Creator": "MURAL figure builder",
        },
    )
    fig.savefig(
        paths["png"],
        facecolor=WHITE,
        dpi=300,
        bbox_inches=None,
        metadata={"Software": "MURAL figure builder"},
    )
    for path in paths.values():
        shutil.copy2(path, MANUSCRIPT_FIGURES / path.name)
    plt.close(fig)


def package_approved(master: Path, stem: str) -> None:
    """Preserve an approved PNG and create paper-compatible raster wrappers."""
    if not master.is_file():
        raise FileNotFoundError(f"missing approved figure master: {master}")

    HERE.mkdir(parents=True, exist_ok=True)
    MANUSCRIPT_FIGURES.mkdir(parents=True, exist_ok=True)
    png_path = HERE / f"{stem}.png"
    pdf_path = HERE / f"{stem}.pdf"
    svg_path = HERE / f"{stem}.svg"
    shutil.copy2(master, png_path)

    with Image.open(master) as image:
        width, height = image.size
        image.convert("RGB").save(
            pdf_path,
            format="PDF",
            resolution=300.0,
            title=f"MURAL {stem}",
            author="MURAL",
            subject="Author-approved paper figure",
        )

    encoded = base64.b64encode(master.read_bytes()).decode("ascii")
    svg_path.write_text(
        "\n".join(
            [
                '<?xml version="1.0" encoding="UTF-8"?>',
                f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">',
                f'  <image width="{width}" height="{height}" href="data:image/png;base64,{encoded}"/>',
                "</svg>",
                "",
            ]
        ),
        encoding="utf-8",
    )

    for path in (png_path, pdf_path, svg_path):
        shutil.copy2(path, MANUSCRIPT_FIGURES / path.name)


def main() -> None:
    package_approved(APPROVED_DIR / "fig1_page_topologies.png", "fig1_page_topologies")
    package_approved(APPROVED_DIR / "fig2_release_workflow.png", "fig2_release_workflow")
    print("packaged approved fig1_page_topologies.{svg,pdf,png}")
    print("packaged approved fig2_release_workflow.{svg,pdf,png}")


if __name__ == "__main__":
    main()
