#!/usr/bin/env python3
"""Build the paper-native ThreadBench anatomy figure.

The figure is deliberately deterministic: benchmark labels and equations must
remain exact, while the visual grammar follows the approved MuralPresenter
figures (four colored stages, rounded cards, deck thumbnails, and sparse arrows).
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent
FONT_PATH = ROOT / "assets" / "Archivo.ttf"
WIDTH, HEIGHT = 2000, 900

INK = "#162126"
MUTED = "#526066"
GRAY = "#D6DDDF"
LIGHT_GRAY = "#F4F7F7"
GREEN = "#266A35"
ORANGE = "#D97800"
TEAL = "#007C82"
BLUE = "#245CC7"
RED = "#C7352B"


def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_PATH), size=size)


F_HEADER = font(31)
F_SUBHEAD = font(24)
F_BODY = font(21)
F_SMALL = font(18)
F_TINY = font(16)
F_FORMULA = font(23)


def rounded(draw: ImageDraw.ImageDraw, box, radius=18, fill="white", outline=INK, width=2, shadow=False):
    x0, y0, x1, y1 = box
    if shadow:
        draw.rounded_rectangle((x0 + 5, y0 + 6, x1 + 5, y1 + 6), radius, fill="#DDE4E5")
    draw.rounded_rectangle(box, radius, fill=fill, outline=outline, width=width)


def centered(draw: ImageDraw.ImageDraw, xy, text: str, fnt, fill=INK, anchor="mm"):
    draw.text(xy, text, font=fnt, fill=fill, anchor=anchor)


def multiline_center(draw: ImageDraw.ImageDraw, box, lines, fnt=F_BODY, fill=INK, gap=5):
    x0, y0, x1, y1 = box
    heights = [draw.textbbox((0, 0), line, font=fnt)[3] for line in lines]
    total = sum(heights) + gap * (len(lines) - 1)
    y = (y0 + y1 - total) / 2
    for line, h in zip(lines, heights):
        centered(draw, ((x0 + x1) / 2, y + h / 2), line, fnt, fill)
        y += h + gap


def arrow(draw: ImageDraw.ImageDraw, start, end, color=INK, width=4, head=12, dashed=False):
    x0, y0 = start
    x1, y1 = end
    if dashed:
        steps = max(1, int(math.hypot(x1 - x0, y1 - y0) / 15))
        for i in range(0, steps, 2):
            a = i / steps
            b = min(1, (i + 1) / steps)
            draw.line((x0 + (x1 - x0) * a, y0 + (y1 - y0) * a,
                       x0 + (x1 - x0) * b, y0 + (y1 - y0) * b), fill=color, width=width)
    else:
        draw.line((x0, y0, x1, y1), fill=color, width=width)
    angle = math.atan2(y1 - y0, x1 - x0)
    left = (x1 - head * math.cos(angle - math.pi / 6), y1 - head * math.sin(angle - math.pi / 6))
    right = (x1 - head * math.cos(angle + math.pi / 6), y1 - head * math.sin(angle + math.pi / 6))
    draw.polygon([(x1, y1), left, right], fill=color)


def panel(draw: ImageDraw.ImageDraw, box, title, color, tint):
    rounded(draw, box, radius=22, fill=tint, outline=color, width=3)
    x0, y0, x1, _ = box
    centered(draw, ((x0 + x1) / 2, y0 + 38), title, F_HEADER, color)
    draw.line((x0 + 28, y0 + 68, x1 - 28, y0 + 68), fill=color, width=3)


def slide(draw: ImageDraw.ImageDraw, x, y, w=86, h=58, accent=TEAL, label=None, target=False):
    if target:
        draw.rounded_rectangle((x - 5, y - 5, x + w + 5, y + h + 5), 10, fill="#E8F3F4", outline=accent, width=4)
    rounded(draw, (x, y, x + w, y + h), radius=8, fill="white", outline=INK, width=2, shadow=True)
    draw.rectangle((x + 9, y + 10, x + 34, y + 34), fill="#E2F1F1", outline=accent, width=2)
    draw.line((x + 43, y + 13, x + w - 9, y + 13), fill=accent, width=4)
    draw.line((x + 43, y + 24, x + w - 14, y + 24), fill="#879397", width=3)
    draw.line((x + 43, y + 35, x + w - 19, y + 35), fill="#A6B0B3", width=3)
    if label:
        centered(draw, (x + w / 2, y + h + 22), label, F_TINY, MUTED)


def artifact_card(draw: ImageDraw.ImageDraw, x, y, w, h, title, detail, color, icon):
    rounded(draw, (x, y, x + w, y + h), radius=13, fill="white", outline=color, width=2, shadow=True)
    draw.ellipse((x + 14, y + 18, x + 54, y + 58), fill="#F7FAFA", outline=color, width=3)
    centered(draw, (x + 34, y + 39), icon, F_SUBHEAD, color)
    draw.text((x + 67, y + 15), title, font=F_SMALL, fill=INK)
    draw.text((x + 67, y + 43), detail, font=F_TINY, fill=MUTED)


def pill(draw: ImageDraw.ImageDraw, x, y, w, text, color):
    rounded(draw, (x, y, x + w, y + 36), radius=18, fill="white", outline=color, width=2)
    centered(draw, (x + w / 2, y + 18), text, F_TINY, color)


def build() -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), "#FFFFFF")
    draw = ImageDraw.Draw(img)

    margin, gap = 8, 12
    widths = [485, 485, 485, 485]
    xs = [margin]
    for w in widths[:-1]:
        xs.append(xs[-1] + w + gap)
    boxes = [(x, 8, x + w, HEIGHT - 8) for x, w in zip(xs, widths)]
    panel(draw, boxes[0], "REQUIREMENT THREAD", GREEN, "#FBFDF8")
    panel(draw, boxes[1], "PROCESS EVIDENCE", ORANGE, "#FFFCF7")
    panel(draw, boxes[2], "FINAL ARTIFACT", TEAL, "#F7FCFD")
    panel(draw, boxes[3], "INDEPENDENT SCORES", BLUE, "#F8FAFF")

    # Panel 1: a concrete, long horizontal source-to-target requirement thread.
    x = xs[0]
    rounded(draw, (x + 34, 104, x + 451, 196), radius=14, fill="white", outline=GREEN, width=2, shadow=True)
    draw.text((x + 54, 122), "Case-specific requirement", font=F_SMALL, fill=GREEN)
    draw.text((x + 54, 154), "Compare three navigation compasses", font=F_BODY, fill=INK)
    draw.text((x + 54, 178), "under one consistent schema", font=F_SMALL, fill=MUTED)

    sx = [x + 42, x + 151, x + 261, x + 371]
    labels = ["source", "mechanism", "evidence", "target"]
    for i, (px, lab) in enumerate(zip(sx, labels)):
        slide(draw, px, 327, accent=GREEN if i < 3 else TEAL, label=lab, target=i == 3)
    centered(draw, (x + 242, 292), "dependency span  d", F_SMALL, GREEN)
    # Arc-like polyline emphasizes the signature long horizon.
    draw.arc((x + 78, 220, x + 415, 354), 190, 350, fill=GREEN, width=5)
    arrow(draw, (x + 399, 315), (x + 414, 334), GREEN, width=5, head=13)
    for px in (x + 136, x + 246, x + 356):
        centered(draw, (px, 355), "···", F_SUBHEAD, MUTED)

    multiline_center(draw, (x + 39, 442, x + 446, 522),
                     ["Observable closure rule", "source evidence must reappear at the target"],
                     F_BODY, INK, gap=6)
    draw.line((x + 58, 538, x + 427, 538), fill=GREEN, width=3)
    pills = [("FACT", 66), ("TERM", 66), ("NARRATIVE", 108), ("DESIGN", 82), ("TASK", 66)]
    px, py = x + 35, 565
    for text, w in pills:
        if px + w > x + 455:
            px, py = x + 90, py + 50
        pill(draw, px, py, w, text, GREEN)
        px += w + 10
    rounded(draw, (x + 55, 684, x + 430, 815), radius=16, fill="#EEF7EE", outline=GREEN, width=2)
    centered(draw, (x + 242, 712), "Animal-navigation example", F_SMALL, GREEN)
    draw.text((x + 82, 744), "• geomagnetic · solar · stellar", font=F_SMALL, fill=INK)
    draw.text((x + 82, 778), "• closes in a 6 × 3 comparison", font=F_SMALL, fill=INK)

    # Panel 2: process evidence and the four Intermediate dimensions.
    x = xs[1]
    centered(draw, (x + 242, 112), "Intermediate evidence trail", F_SUBHEAD, ORANGE)
    cards = [
        ("Research", "sources + terms", ORANGE, "R"),
        ("Planning", "deck + page contracts", "#C88A00", "P"),
        ("Image", "tasks + verified usage", TEAL, "I"),
        ("Slide Production", "render · repair · rerender", GREEN, "S"),
    ]
    cy = 148
    for idx, (title, detail, color, icon) in enumerate(cards):
        artifact_card(draw, x + 55, cy, 375, 92, title, detail, color, icon)
        if idx < len(cards) - 1:
            arrow(draw, (x + 242, cy + 94), (x + 242, cy + 119), ORANGE, width=4, head=10)
        cy += 126
    rounded(draw, (x + 54, 675, x + 431, 759), radius=14, fill="#FFF4E8", outline=ORANGE, width=2)
    multiline_center(draw, (x + 66, 682, x + 419, 751),
                     ["Criterion-scoped Judge packets", "explicit anchors · only relevant evidence"],
                     F_SMALL, INK, gap=5)
    pill(draw, x + 104, 791, 278, "7 criteria across R · P · I · S", ORANGE)

    # Panel 3: final deck and page checks remain artifact-grounded.
    x = xs[2]
    centered(draw, (x + 242, 112), "Canonical rendered deck", F_SUBHEAD, TEAL)
    for i in range(4):
        slide(draw, x + 47 + i * 97, 154, w=78, h=54, accent=TEAL, target=False)
    arrow(draw, (x + 242, 232), (x + 242, 276), TEAL, width=4, head=11)

    rounded(draw, (x + 36, 285, x + 230, 582), radius=16, fill="white", outline=TEAL, width=2, shadow=True)
    centered(draw, (x + 133, 318), "DECK CHECKS", F_SUBHEAD, TEAL)
    deck_lines = ["Knowledge checklist", "Audience / goal fit", "Content & density", "Narrative & pacing", "Cross-page semantics", "Global visual style"]
    for i, line in enumerate(deck_lines):
        yy = 360 + i * 36
        draw.ellipse((x + 54, yy + 3, x + 66, yy + 15), fill=TEAL)
        draw.text((x + 76, yy), line, font=F_TINY, fill=INK)

    rounded(draw, (x + 255, 285, x + 449, 582), radius=16, fill="white", outline=TEAL, width=2, shadow=True)
    centered(draw, (x + 352, 318), "PAGE CHECKS", F_SUBHEAD, TEAL)
    page_lines = ["Hierarchy / density", "Text–image encoding", "Composition", "Typography / color", "Technical quality"]
    for i, line in enumerate(page_lines):
        yy = 370 + i * 42
        draw.ellipse((x + 273, yy + 3, x + 285, yy + 15), fill="#5AAEB2")
        draw.text((x + 295, yy), line, font=F_TINY, fill=INK)

    arrow(draw, (x + 133, 590), (x + 133, 629), TEAL, width=4, head=11)
    arrow(draw, (x + 352, 590), (x + 352, 629), TEAL, width=4, head=11)
    rounded(draw, (x + 54, 641, x + 431, 745), radius=15, fill="#EAF7F8", outline=TEAL, width=2)
    multiline_center(draw, (x + 68, 650, x + 417, 736),
                     ["Artifact-grounded judgments", "whole-deck context · canonical page pixels"],
                     F_SMALL, INK, gap=6)
    pill(draw, x + 95, 785, 295, "Final = Deck + Page groups", TEAL)

    # Panel 4: two equal outputs and exact aggregation equations.
    x = xs[3]
    rounded(draw, (x + 39, 112, x + 446, 288), radius=17, fill="white", outline=ORANGE, width=3, shadow=True)
    centered(draw, (x + 242, 148), "INTERMEDIATE", F_SUBHEAD, ORANGE)
    centered(draw, (x + 242, 197), "I = mean(R, P, Im, S)", F_FORMULA, INK)
    centered(draw, (x + 242, 245), "applicable dimensions only", F_SMALL, MUTED)

    rounded(draw, (x + 39, 320, x + 446, 565), radius=17, fill="white", outline=TEAL, width=3, shadow=True)
    centered(draw, (x + 242, 356), "FINAL", F_SUBHEAD, TEAL)
    centered(draw, (x + 242, 405), "F = 1/2 (Deck_eff + Page_eff)", F_FORMULA, INK)
    draw.line((x + 78, 443, x + 407, 443), fill=GRAY, width=2)
    centered(draw, (x + 242, 475), "weakest-dimension decay", F_SMALL, MUTED)
    centered(draw, (x + 242, 516), "G_eff = G_base[1 - 0.3(1 - G_min)]", F_SMALL, INK)

    rounded(draw, (x + 68, 610, x + 417, 704), radius=16, fill="#EDF2FF", outline=BLUE, width=3)
    multiline_center(draw, (x + 80, 617, x + 405, 697),
                     ["Two official outputs", "never collapsed into one score"],
                     F_BODY, BLUE, gap=7)
    rounded(draw, (x + 89, 746, x + 396, 817), radius=15, fill="white", outline=BLUE, width=2)
    multiline_center(draw, (x + 100, 752, x + 385, 810),
                     ["Optional Preaudit", "criterion-relevant defects · no score"],
                     F_TINY, MUTED, gap=4)

    # Thick inter-panel arrows echo the approved workflow figures.
    for boundary in (xs[1] - gap / 2, xs[2] - gap / 2, xs[3] - gap / 2):
        arrow(draw, (boundary - 12, 445), (boundary + 12, 445), INK, width=8, head=16)

    return img


def main() -> None:
    img = build()
    outputs = [
        ROOT / "fig4_threadbench_anatomy.png",
        ROOT.parent.parent / "manuscript" / "figures" / "fig4_threadbench_anatomy.png",
    ]
    for path in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)
        img.save(path, format="PNG", optimize=True, dpi=(300, 300))
    pdf_outputs = [p.with_suffix(".pdf") for p in outputs]
    for path in pdf_outputs:
        img.save(path, format="PDF", resolution=300.0)
    print("\n".join(str(p) for p in outputs + pdf_outputs))


if __name__ == "__main__":
    main()
