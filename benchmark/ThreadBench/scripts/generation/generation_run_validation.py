"""Read-only validation and selection of generated presentation runs."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


SLIDE_RE = re.compile(r"slide_([0-9]{2})\.html$")
RENDER_RE = re.compile(r"slide_([0-9]{2})\.png$")


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def indexed_files(
    directory: Path,
    pattern: re.Pattern[str],
) -> dict[int, Path] | None:
    if not directory.is_dir():
        return None
    indexed: dict[int, Path] = {}
    for path in directory.iterdir():
        if not path.is_file() or not path.name.startswith("slide_"):
            continue
        match = pattern.fullmatch(path.name)
        if match is None or path.stat().st_size == 0:
            return None
        index = int(match.group(1))
        if index in indexed:
            return None
        indexed[index] = path
    return indexed


def run_is_valid(_case_dir: Path, run_dir: Path) -> bool:
    """Return whether a run satisfies the canonical HTML/PNG artifact contract."""
    metadata = load_json(run_dir / "generation_metadata.json")
    if metadata:
        success = metadata.get("success")
        if (
            not isinstance(success, dict)
            or success.get("artifact_contract_satisfied") is not True
        ):
            return False
    else:
        recovery = load_json(run_dir / "recovery.json")
        if not recovery or recovery.get("visual_artifact_available") is not True:
            return False

    slides = indexed_files(run_dir / "slides", SLIDE_RE)
    renders = indexed_files(run_dir / "renders", RENDER_RE)
    if slides is None or renders is None or set(slides) != set(renders):
        return False
    indices = sorted(slides)
    if not indices or indices != list(range(1, len(indices) + 1)):
        return False

    for index in indices:
        html_prefix = slides[index].read_text(
            encoding="utf-8", errors="ignore"
        )[:4096].lower()
        if "<html" not in html_prefix and "<!doctype html" not in html_prefix:
            return False
        if renders[index].stat().st_size <= 8:
            return False
        with renders[index].open("rb") as handle:
            if handle.read(8) != b"\x89PNG\r\n\x1a\n":
                return False
    return True


def valid_run(case_dir: Path, model: str) -> Path | None:
    """Return the newest valid run for a case/model pair."""
    model_dir = case_dir / "outputs" / model
    if not model_dir.is_dir():
        return None
    candidates = sorted(
        (
            path
            for path in model_dir.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return next((path for path in candidates if run_is_valid(case_dir, path)), None)
