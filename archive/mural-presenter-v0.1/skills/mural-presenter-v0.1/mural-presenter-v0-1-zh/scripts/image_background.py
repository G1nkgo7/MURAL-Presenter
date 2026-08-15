#!/usr/bin/env python3
"""Inspect transparency and remove a baked light checkerboard conservatively."""
from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
from statistics import median
import tempfile

from PIL import Image


def _asset_path(root: Path, relative: str) -> Path:
    value = Path(relative)
    if value.is_absolute():
        raise ValueError("asset path must be workspace-relative")
    path = (root / value).resolve()
    assets = (root / "assets").resolve()
    if path.parent != assets or path.suffix.lower() not in {".png", ".webp"}:
        raise ValueError("asset must be one PNG/WebP directly under assets/")
    if not path.is_file():
        raise FileNotFoundError(f"asset does not exist: {relative}")
    return path


def _run_lengths(values: list[bool]) -> list[int]:
    result: list[int] = []
    if not values:
        return result
    current, length = values[0], 1
    for value in values[1:]:
        if value == current:
            length += 1
        else:
            result.append(length)
            current, length = value, 1
    result.append(length)
    return result


def _edge_report(pixels: list[tuple[int, int, int]]) -> dict:
    neutral = [
        pixel for pixel in pixels
        if max(pixel) - min(pixel) <= 32 and min(pixel) >= 170
    ]
    fraction = len(neutral) / max(1, len(pixels))
    if len(neutral) < 16:
        return {"pattern": False, "neutral_fraction": round(fraction, 4)}
    luminance = sorted(sum(pixel) // 3 for pixel in neutral)
    low = luminance[len(luminance) // 4]
    high = luminance[(len(luminance) * 3) // 4]
    midpoint = (low + high) / 2
    classes = [
        sum(pixel) / 3 >= midpoint
        for pixel in pixels
        if max(pixel) - min(pixel) <= 32 and min(pixel) >= 170
    ]
    runs = [value for value in _run_lengths(classes) if value >= 4]
    typical = float(median(runs)) if runs else 0.0
    high_fraction = sum(classes) / max(1, len(classes))
    return {
        "pattern": (
            fraction >= 0.80
            and high - low >= 7
            and 0.15 <= high_fraction <= 0.85
            and len(runs) >= 6
            and 6 <= typical <= 80
        ),
        "neutral_fraction": round(fraction, 4),
        "tone_separation": high - low,
        "median_run": round(typical, 2),
    }


def inspect_image(root: Path, relative: str, expect_transparent: bool) -> dict:
    path = _asset_path(root.resolve(), relative)
    with Image.open(path) as image:
        rgba = image.convert("RGBA")
        alpha = rgba.getchannel("A")
        alpha_extrema = alpha.getextrema()
        meaningful_alpha = alpha_extrema[0] < 250
        width, height = rgba.size
        rgb = rgba.convert("RGB")

        def visible(x: int, y: int) -> tuple[int, int, int]:
            return (
                rgb.getpixel((x, y))
                if alpha.getpixel((x, y)) >= 250
                else (255, 0, 255)
            )

        edges = {
            "top": [visible(x, 0) for x in range(width)],
            "right": [visible(width - 1, y) for y in range(height)],
            "bottom": [visible(x, height - 1) for x in range(width)],
            "left": [visible(0, y) for y in range(height)],
        }
        reports = {name: _edge_report(values) for name, values in edges.items()}
        checker_edges = [
            name for name, report in reports.items() if report["pattern"]
        ]
        baked_checkerboard = len(checker_edges) >= 2
        status = (
            "PASS"
            if not expect_transparent
            or (meaningful_alpha and not baked_checkerboard)
            else "FAIL"
        )
        return {
            "status": status,
            "asset": relative,
            "format": image.format,
            "mode": image.mode,
            "size": [width, height],
            "meaningful_alpha": meaningful_alpha,
            "alpha_extrema": list(alpha_extrema),
            "baked_checkerboard": baked_checkerboard,
            "checker_edges": checker_edges,
            "edge_reports": reports,
        }


def remove_checkerboard(root: Path, relative: str) -> dict:
    before = inspect_image(root, relative, True)
    if not before["baked_checkerboard"]:
        raise ValueError(
            "no repeated light checkerboard detected; refusing generic removal"
        )
    path = _asset_path(root.resolve(), relative)
    with Image.open(path) as source:
        rgba = source.convert("RGBA")
        rgb = rgba.convert("RGB")
        width, height = rgba.size
        pixels = rgb.load()
        visited = bytearray(width * height)
        background = bytearray(width * height)
        queue: deque[tuple[int, int]] = deque()

        def candidate(x: int, y: int) -> bool:
            red, green, blue = pixels[x, y]
            return min(red, green, blue) >= 170 and (
                max(red, green, blue) - min(red, green, blue) <= 42
            )

        def enqueue(x: int, y: int) -> None:
            index = y * width + x
            if not visited[index] and candidate(x, y):
                visited[index] = 1
                queue.append((x, y))

        for x in range(width):
            enqueue(x, 0)
            enqueue(x, height - 1)
        for y in range(height):
            enqueue(0, y)
            enqueue(width - 1, y)
        while queue:
            x, y = queue.popleft()
            background[y * width + x] = 1
            if x:
                enqueue(x - 1, y)
            if x + 1 < width:
                enqueue(x + 1, y)
            if y:
                enqueue(x, y - 1)
            if y + 1 < height:
                enqueue(x, y + 1)
        removed = sum(background)
        total = len(background)
        if removed < total * 0.05 or removed > total * 0.98:
            raise ValueError("checkerboard mask is implausible; asset preserved")
        alpha = Image.new("L", rgba.size, 255)
        alpha.putdata([0 if value else 255 for value in background])
        rgba.putalpha(alpha)
        temporary_dir = root / "tmp"
        temporary_dir.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=f"{path.name}.", suffix=".tmp", dir=temporary_dir
        )
        os.close(descriptor)
        temporary = Path(name)
        try:
            rgba.save(temporary, format="PNG")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
            try:
                temporary_dir.rmdir()
            except OSError:
                pass
    after = inspect_image(root, relative, True)
    return {
        "status": after["status"],
        "asset": relative,
        "operation": "remove-baked-light-checkerboard",
        "removed_fraction": round(removed / total, 4),
        "before": before,
        "after": after,
    }


def print_report(report: dict) -> None:
    print(json.dumps(report, ensure_ascii=False, indent=2))
