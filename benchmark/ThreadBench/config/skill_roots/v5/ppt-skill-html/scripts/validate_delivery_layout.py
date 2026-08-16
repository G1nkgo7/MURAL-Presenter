#!/usr/bin/env python3
"""Validate the single canonical root-deck contract before/after delivery."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from check_slide import compute_source_hash


class _LocalResourceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.resources: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        attr = "src" if tag in {"img", "script"} else "href" if tag == "link" else None
        if attr and values.get(attr):
            self.resources.append((tag, str(values[attr])))


def _nonempty(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _check_local_resources(slide: Path, root: Path) -> list[str]:
    errors: list[str] = []
    parser = _LocalResourceParser()
    try:
        parser.feed(slide.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        return [f"cannot read slide HTML {slide.name}: {exc}"]
    for tag, raw in parser.resources:
        parsed = urlsplit(raw)
        if parsed.scheme in {"http", "https", "data"} or raw.startswith("#"):
            continue
        if parsed.scheme or raw.startswith("/"):
            errors.append(f"{slide.name}: non-portable absolute resource in <{tag}>: {raw}")
            continue
        local = (slide.parent / unquote(parsed.path)).resolve()
        if not _inside(local, root):
            errors.append(f"{slide.name}: local resource escapes WORKSPACE_ROOT: {raw}")
        elif not _nonempty(local):
            errors.append(f"{slide.name}: local resource missing or empty: {raw}")
    return errors


def _check_gate_file(path: Path, empty_key: str) -> list[str]:
    if not _nonempty(path):
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    errors: list[str] = []
    if not re.search(r"(?mi)^status:\s*PASS\s*$", text):
        errors.append(f"gate file is not explicit PASS: {path.name}")
    if not re.search(rf"(?mi)^{re.escape(empty_key)}:\s*\[\s*\]\s*$", text):
        errors.append(f"gate file does not declare {empty_key}: []: {path.name}")
    return errors


def _check_asset_manifest(path: Path, expected_pages: int) -> list[str]:
    if not _nonempty(path):
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"invalid plan/assets.json: {exc}"]
    if data.get("schema_version") != 1 or not isinstance(data.get("assets"), list):
        return ["plan/assets.json must contain schema_version=1 and assets[]"]
    errors: list[str] = []
    seen: set[str] = set()
    for item in data["assets"]:
        if not isinstance(item, dict):
            errors.append("plan/assets.json entries must be objects")
            continue
        asset_id = str(item.get("id", ""))
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", asset_id) or asset_id in seen:
            errors.append(f"invalid or duplicate asset id in plan/assets.json: {asset_id!r}")
            continue
        seen.add(asset_id)
        if item.get("path") != f"assets/by-id/{asset_id}.png":
            errors.append(f"non-canonical asset path for {asset_id}: {item.get('path')!r}")
        slides = item.get("slides")
        if not isinstance(slides, list) or any(not isinstance(v, int) or v < 1 or v > expected_pages for v in slides):
            errors.append(f"invalid referenced slides for asset {asset_id}: {slides!r}")
    return errors


def validate(root_arg: str, expected: int, phase: str) -> dict[str, object]:
    supplied = Path(root_arg).expanduser()
    root = supplied.resolve()
    errors: list[str] = []
    if not supplied.is_absolute():
        errors.append("WORKSPACE_ROOT must be the absolute initial task workspace path")
    if Path.cwd().resolve() != root:
        errors.append(f"current cwd is not WORKSPACE_ROOT: cwd={Path.cwd().resolve()} root={root}")
    if expected < 1:
        errors.append("expected page count must be positive")

    slides = root / "slides"
    renders = root / "renders"
    checks = root / "checks"
    expected_html = [f"slide_{i:02d}.html" for i in range(1, expected + 1)]
    expected_png = [f"slide_{i:02d}.png" for i in range(1, expected + 1)]
    actual_html = sorted(p.name for p in slides.glob("slide_*.html")) if slides.is_dir() else []
    actual_png = sorted(p.name for p in renders.glob("slide_*.png")) if renders.is_dir() else []
    if actual_html != expected_html:
        errors.append(f"root slides mismatch: expected={expected_html} actual={actual_html}")
    if actual_png != expected_png:
        errors.append(f"root renders mismatch: expected={expected_png} actual={actual_png}")
    if slides.is_dir():
        unexpected = sorted(p.name for p in slides.iterdir() if p.name not in expected_html)
        if unexpected:
            errors.append(f"unexpected entries in root slides/: {unexpected}")
    if renders.is_dir():
        unexpected = sorted(p.name for p in renders.iterdir() if p.name not in expected_png)
        if unexpected:
            errors.append(f"unexpected entries in root renders/: {unexpected}")
    empty_slides = [name for name in expected_html if not _nonempty(slides / name)]
    empty_renders = [name for name in expected_png if not _nonempty(renders / name)]
    if empty_slides:
        errors.append(f"root slide files missing or empty: {empty_slides}")
    if empty_renders:
        errors.append(f"root render files missing or empty: {empty_renders}")

    expected_checks = [f"slide_{i:02d}.json" for i in range(1, expected + 1)]
    actual_checks = sorted(p.name for p in checks.glob("slide_*.json")) if checks.is_dir() else []
    if actual_checks != expected_checks:
        errors.append(f"root checks mismatch: expected={expected_checks} actual={actual_checks}")
    if checks.is_dir():
        unexpected = sorted(p.name for p in checks.iterdir() if p.name not in expected_checks)
        if unexpected:
            errors.append(f"unexpected entries in root checks/: {unexpected}")

    nested_slides = sorted(
        str(path.relative_to(root))
        for path in root.glob("**/slides/slide_*.html")
        if path.parent.resolve() != slides.resolve()
    )
    if nested_slides:
        errors.append(f"nested parallel deck slides are forbidden: {nested_slides}")
    nested_renders = sorted(
        str(path.relative_to(root))
        for path in root.glob("**/renders/slide_*.png")
        if path.parent.resolve() != renders.resolve()
    )
    if nested_renders:
        errors.append(f"nested parallel deck renders are forbidden: {nested_renders}")
    nested_present = sorted(
        str(path.relative_to(root))
        for path in root.rglob("present.html")
        if path.resolve() != (root / "present.html").resolve()
    )
    if nested_present:
        errors.append(f"nested parallel present.html files are forbidden: {nested_present}")

    required = [
        "plan/deck.md",
        "plan/narrative.md",
        "plan/design-brief.md",
        "plan/art-direction.md",
        "plan/assets.json",
        "base.css",
        "speech.md",
        "memory/design-memory.md",
        "memory/content-memory.md",
        "reviews/designer-audit.md",
        "reviews/presenter-audit.md",
        "audience_feedback.md",
    ]
    required.extend(f"plan/slide_{i:02d}.md" for i in range(1, expected + 1))
    missing = [path for path in required if not _nonempty(root / path)]
    if missing:
        errors.append(f"required delivery files missing or empty: {missing}")
    errors.extend(_check_asset_manifest(root / "plan/assets.json", expected))
    errors.extend(_check_gate_file(root / "reviews/designer-audit.md", "hard"))
    errors.extend(_check_gate_file(root / "reviews/presenter-audit.md", "hard"))
    errors.extend(_check_gate_file(root / "audience_feedback.md", "must_fix"))
    script_root = Path(__file__).resolve().parent
    for index, name in enumerate(expected_checks, 1):
        path = checks / name
        if not _nonempty(path):
            errors.append(f"check report missing or empty: checks/{name}")
            continue
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"invalid check report checks/{name}: {exc}")
            continue
        if report.get("schema_version") != 1 or report.get("render_status") != "ok":
            errors.append(f"check report not render-ok: checks/{name}")
        if report.get("hard") != [] or report.get("hard_count") != 0:
            errors.append(f"check report still has hard issues: checks/{name}")
        png = renders / f"slide_{index:02d}.png"
        current_png_hash = hashlib.sha256(png.read_bytes()).hexdigest() if _nonempty(png) else None
        if report.get("png_sha256") != current_png_hash:
            errors.append(f"render PNG changed after check: renders/slide_{index:02d}.png")
        html = slides / f"slide_{index:02d}.html"
        if _nonempty(html) and _nonempty(root / "base.css"):
            expected_hash, _ = compute_source_hash(
                html,
                root / "base.css",
                root,
                (script_root / "render.py", script_root / "ai_slop_lint.py", script_root / "check_slide.py"),
            )
            if report.get("source_hash") != expected_hash:
                errors.append(f"stale check report after HTML/CSS/asset change: checks/{name}")
    pending_assets = sorted(str(path.relative_to(root)) for path in (root / "assets/pending").glob("*.json")) if (root / "assets/pending").is_dir() else []
    if pending_assets:
        errors.append(f"required assets still pending: {pending_assets}")
    placeholders = sorted(
        str(path.relative_to(root))
        for path in (root / "assets/by-id").glob("*.png")
        if b"ppt-placeholder" in path.read_bytes()
    ) if (root / "assets/by-id").is_dir() else []
    if placeholders:
        errors.append(f"placeholder assets must be finalized or cancelled: {placeholders}")
    temp_backups = sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file()
        and (
            path.name.endswith((".bak", ".tmp", "~"))
            or ".bak." in path.name
            or ("tmp/slide_backups" in path.relative_to(root).as_posix())
        )
    )
    if temp_backups:
        errors.append(f"temporary/backup files must be cleaned before delivery: {temp_backups}")
    for name in expected_html:
        if _nonempty(slides / name):
            errors.extend(_check_local_resources(slides / name, root))
    if phase == "post" and not ((root / "present.html").is_file() and (root / "present.html").stat().st_size > 0):
        errors.append("root present.html is missing or empty after build")

    return {
        "ok": not errors,
        "phase": phase,
        "workspace_root": str(root),
        "expected_pages": expected,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace_root", help="absolute initial task workspace root")
    parser.add_argument("--expected", type=int, required=True, help="locked canonical page count")
    parser.add_argument("--phase", choices=("pre", "post"), required=True)
    args = parser.parse_args()
    result = validate(args.workspace_root, args.expected, args.phase)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
