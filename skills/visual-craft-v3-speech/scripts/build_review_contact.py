#!/usr/bin/env python3
"""Build readable whole-deck contact sheets for the single Review agent.

Default mode creates one compact overview plus an adaptive set of detailed
groups.  Focus mode creates one verification sheet for pages changed by Review.
The script only reads ``renders/slide_NN.png`` and writes deterministic review
artifacts under ``renders/``.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import tempfile

from PIL import Image, ImageDraw, ImageFont, ImageOps


PAGE_RE = re.compile(r"^slide_(\d+)\.png$")
MAX_DETAIL_SHEETS = 5


def _font(size: int) -> ImageFont.ImageFont:
    for name in ("DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _atomic_image(path: Path, image: Image.Image) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".png", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        image.save(temporary, format="PNG", compress_level=1)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _available_pages(render_dir: Path) -> list[int]:
    pages: list[int] = []
    for path in render_dir.iterdir() if render_dir.is_dir() else ():
        match = PAGE_RE.match(path.name)
        if match:
            pages.append(int(match.group(1)))
    return sorted(set(pages))


def _parse_focus(raw: str) -> list[int]:
    pages: set[int] = set()
    for part in re.split(r"[\s,]+", raw.strip()):
        if not part:
            continue
        if "-" in part:
            left, right = part.split("-", 1)
            start, end = int(left), int(right)
            if start > end:
                raise ValueError(f"invalid descending range: {part}")
            pages.update(range(start, end + 1))
        else:
            pages.add(int(part))
    if not pages or min(pages) < 1:
        raise ValueError("--focus requires positive page numbers")
    return sorted(pages)


def _resolve_pages(render_dir: Path, expected: int | None, focus: str | None) -> list[int]:
    available = _available_pages(render_dir)
    if not available:
        raise SystemExit(f"no slide PNGs found in {render_dir}")
    if focus:
        pages = _parse_focus(focus)
    elif expected:
        pages = list(range(1, expected + 1))
    else:
        pages = list(range(1, max(available) + 1))
    missing = [page for page in pages if page not in available]
    if missing:
        joined = ",".join(f"{page:02d}" for page in missing)
        raise SystemExit(f"missing rendered pages: {joined}")
    return pages


def _make_sheet(
    render_dir: Path,
    pages: list[int],
    *,
    columns: int,
    thumb_width: int,
    label_height: int,
    title: str,
) -> Image.Image:
    thumb_height = round(thumb_width * 9 / 16)
    gap = max(14, thumb_width // 24)
    header = 54
    rows = math.ceil(len(pages) / columns)
    width = gap + columns * (thumb_width + gap)
    height = header + rows * (thumb_height + label_height + gap)
    sheet = Image.new("RGB", (width, height), "#17191D")
    draw = ImageDraw.Draw(sheet)
    draw.text((gap, 14), title, fill="#F2F3F5", font=_font(24))
    label_font = _font(max(16, label_height - 12))

    for index, page in enumerate(pages):
        row, column = divmod(index, columns)
        x = gap + column * (thumb_width + gap)
        y = header + row * (thumb_height + label_height + gap)
        with Image.open(render_dir / f"slide_{page:02d}.png") as source:
            frame = ImageOps.contain(source.convert("RGB"), (thumb_width, thumb_height), Image.Resampling.LANCZOS)
        px = x + (thumb_width - frame.width) // 2
        py = y + (thumb_height - frame.height) // 2
        sheet.paste(frame, (px, py))
        draw.rectangle((x, y, x + thumb_width - 1, y + thumb_height - 1), outline="#555B66", width=2)
        draw.text((x + 4, y + thumb_height + 5), f"SLIDE {page:02d}", fill="#F2F3F5", font=label_font)
    return sheet


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _build_default(root: Path, render_dir: Path, pages: list[int]) -> dict:
    for stale in render_dir.glob("contact-sheet-review-*.png"):
        stale.unlink()
    (render_dir / "contact-sheet-focus.png").unlink(missing_ok=True)

    overview_path = render_dir / "contact-sheet.png"
    overview = _make_sheet(
        render_dir,
        pages,
        columns=6,
        thumb_width=260,
        label_height=28,
        title=f"DECK OVERVIEW · {len(pages)} PAGES",
    )
    _atomic_image(overview_path, overview)

    group_count = min(MAX_DETAIL_SHEETS, max(1, math.ceil(len(pages) / 8)))
    group_size = math.ceil(len(pages) / group_count)
    groups = [pages[index:index + group_size] for index in range(0, len(pages), group_size)]
    detail_entries = []
    for index, group in enumerate(groups, start=1):
        path = render_dir / f"contact-sheet-review-{index:02d}.png"
        sheet = _make_sheet(
            render_dir,
            group,
            columns=min(4, len(group)),
            thumb_width=400,
            label_height=34,
            title=f"REVIEW GROUP {index:02d} · PAGES {group[0]:02d}–{group[-1]:02d}",
        )
        _atomic_image(path, sheet)
        detail_entries.append({"path": _relative(path, root), "pages": group})

    return {
        "mode": "full",
        "page_count": len(pages),
        "pages": pages,
        "overview": _relative(overview_path, root),
        "groups": detail_entries,
    }


def _build_focus(root: Path, render_dir: Path, pages: list[int]) -> dict:
    path = render_dir / "contact-sheet-focus.png"
    sheet = _make_sheet(
        render_dir,
        pages,
        columns=min(3, len(pages)),
        thumb_width=500,
        label_height=40,
        title="REVIEW FOCUS · CHANGED PAGES " + ", ".join(f"{page:02d}" for page in pages),
    )
    _atomic_image(path, sheet)
    return {"mode": "focus", "pages": pages, "focus": _relative(path, root)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", nargs="?", default=".")
    parser.add_argument("--expected", type=int)
    parser.add_argument("--focus", help="Comma-separated pages or ranges, e.g. 3,7,12-14")
    args = parser.parse_args()

    root = Path(args.workspace).resolve()
    render_dir = root / "renders"
    pages = _resolve_pages(render_dir, args.expected, args.focus)
    payload = _build_focus(root, render_dir, pages) if args.focus else _build_default(root, render_dir, pages)
    manifest = render_dir / "review-contact.json"
    if args.focus and manifest.is_file():
        try:
            audit = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            audit = {}
    else:
        audit = {}
    audit["focus" if args.focus else "full"] = payload
    _atomic_json(manifest, audit)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
