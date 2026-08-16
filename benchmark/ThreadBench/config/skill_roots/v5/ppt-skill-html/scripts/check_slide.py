#!/usr/bin/env python3
"""Render/lint one slide with a dependency hash and reusable JSON check report."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit


ADVISORY_RENDER_CODES = {
    "WIDOW-LINE",
    "INNER-GAP",
    "IMG-LONELY",
    "SVG-SMALL",
    "SPARSE",
    "VBALANCE",
    "COVER-OOB",
    "ON-IMG-NOSCRIM",
    "CJK-PUNCT",   # 2026-07 控成本:半角标点是 cosmetic 排版小疵,不该当 hard 逼 slide 死磕→撞 cap→BLOCKED→重派(纯浪费)。归 advisory,slide 有余量可顺手修,不为它返工
}
WARNING_RE = re.compile(r"⚠\s*([A-Z][A-Z0-9-]+)")
LINT_HIT_RE = re.compile(r"^\s+\S+:\d+\s+\[([^\]]+)\]")


class _DependencyParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.refs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        key = "src" if tag in {"img", "script"} else "href" if tag == "link" else None
        if key and values.get(key):
            self.refs.append(str(values[key]))


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _local_dependencies(html: Path, root: Path) -> list[Path]:
    parser = _DependencyParser()
    parser.feed(html.read_text(encoding="utf-8", errors="replace"))
    deps: list[Path] = []
    for raw in parser.refs:
        parsed = urlsplit(raw)
        if parsed.scheme or raw.startswith(("/", "#")):
            continue
        path = (html.parent / unquote(parsed.path)).resolve()
        if _inside(path, root):
            deps.append(path)
    return sorted(set(deps))


def compute_source_hash(
    html: Path,
    base_css: Path,
    root: Path,
    extra_dependencies: tuple[Path, ...] = (),
) -> tuple[str, list[Path]]:
    digest = hashlib.sha256()
    dependencies = [
        html.resolve(),
        base_css.resolve(),
        *_local_dependencies(html, root),
        *(path.resolve() for path in extra_dependencies),
    ]
    unique = sorted(set(dependencies))
    for path in unique:
        digest.update(str(path.relative_to(root) if _inside(path, root) else path).encode())
        if path.is_file():
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        else:
            digest.update(b"<missing>")
    return digest.hexdigest(), unique


def _classify_render(output: str) -> tuple[list[str], list[str]]:
    hard: list[str] = []
    advisory: list[str] = []
    for code in WARNING_RE.findall(output):
        bucket = advisory if code in ADVISORY_RENDER_CODES else hard
        if code not in bucket:
            bucket.append(code)
    return hard, advisory


def _classify_lint(output: str) -> tuple[list[str], list[str]]:
    hard: list[str] = []
    advisory: list[str] = []
    section = ""
    for line in output.splitlines():
        if line.startswith("-- HARD"):
            section = "hard"
            continue
        if line.startswith("-- SOFT"):
            section = "soft"
            continue
        match = LINT_HIT_RE.match(line)
        if match:
            code = "LINT:" + match.group(1)
            target = hard if section == "hard" else advisory
            if code not in target:
                target.append(code)
    summary = re.search(r"AI-SLOP-LINT:\s*HARD=(\d+)", output)
    if summary and int(summary.group(1)) > 0 and not hard:
        hard.append("LINT:HARD")
    return hard, advisory


def _pending_for_dependencies(deps: list[Path], root: Path) -> list[str]:
    pending: list[str] = []
    by_id = (root / "assets/by-id").resolve()
    for dep in deps:
        if dep.parent == by_id:
            marker = root / "assets/pending" / f"{dep.stem}.json"
            if marker.is_file():
                pending.append("ASSET_PENDING:" + dep.stem)
    return sorted(set(pending))


def _atomic_json(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _sha256(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("html", type=Path)
    parser.add_argument("png", type=Path)
    parser.add_argument("base_css", type=Path)
    parser.add_argument("check_json", type=Path)
    profile = os.environ.get("PPT_CAPABILITY_PROFILE", "visual").strip().lower().replace("_", "-")
    default_mode = "novisual" if profile in {"novisual", "no-visual", "no-vision"} else "visual"
    parser.add_argument("--mode", choices=("visual", "novisual"), default=default_mode,
                        help="Compatibility override; defaults from PPT_CAPABILITY_PROFILE.")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--render-script", type=Path)
    parser.add_argument("--lint-script", type=Path)
    args = parser.parse_args()

    html = args.html.resolve()
    png = args.png.resolve()
    base_css = args.base_css.resolve()
    check_json = args.check_json.resolve()
    root = html.parent.parent.resolve()
    script_root = Path(__file__).resolve().parent
    render_script = (args.render_script or script_root / "render.py").resolve()
    lint_script = (args.lint_script or script_root / "ai_slop_lint.py").resolve()

    if not html.is_file() or not base_css.is_file():
        print(json.dumps({"status": "error", "error": "missing html or base_css"}, ensure_ascii=False))
        return 2

    source_hash, deps = compute_source_hash(
        html,
        base_css,
        root,
        (render_script, lint_script, Path(__file__).resolve()),
    )
    if not args.force and check_json.is_file() and png.is_file() and png.stat().st_size > 0:
        try:
            cached = json.loads(check_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached = {}
        if (
            cached.get("source_hash") == source_hash
            and cached.get("mode") == args.mode
            and cached.get("png_sha256") == _sha256(png)
            and cached.get("render_status") == "ok"
        ):
            cached["cache_hit"] = True
            print(json.dumps(cached, ensure_ascii=False))
            return 0

    png.parent.mkdir(parents=True, exist_ok=True)
    render = subprocess.run(
        [sys.executable, str(render_script), str(html), str(png)],
        cwd=root,
        capture_output=True,
        text=True,
    )
    lint = subprocess.run(
        [sys.executable, str(lint_script), str(html), str(base_css)],
        cwd=root,
        capture_output=True,
        text=True,
    )
    render_output = "\n".join(part for part in (render.stdout, render.stderr) if part)
    lint_output = "\n".join(part for part in (lint.stdout, lint.stderr) if part)
    render_hard, render_advisory = _classify_render(render_output)
    lint_hard, lint_advisory = _classify_lint(lint_output)
    hard = render_hard + [code for code in lint_hard if code not in render_hard]
    advisory = render_advisory + [code for code in lint_advisory if code not in render_advisory]
    advisory.extend(code for code in _pending_for_dependencies(deps, root) if code not in advisory)
    if render.returncode != 0:
        hard.insert(0, "EXEC:RENDER")
    if lint.returncode not in (0,):
        hard.insert(0, "EXEC:LINT")
    if not png.is_file() or png.stat().st_size == 0:
        if "EXEC:RENDER" not in hard:
            hard.insert(0, "EXEC:RENDER")

    report = {
        "schema_version": 1,
        "slide": html.stem,
        "mode": args.mode,
        "source_hash": source_hash,
        "dependencies": [str(p.relative_to(root)) if _inside(p, root) else str(p) for p in deps],
        "cache_hit": False,
        "render_status": "ok" if render.returncode == 0 and png.is_file() and png.stat().st_size > 0 else "failed",
        "png_sha256": _sha256(png),
        "render_returncode": render.returncode,
        "lint_returncode": lint.returncode,
        "hard": hard,
        "hard_count": len(hard),
        "advisory": advisory,
        "advisory_count": len(advisory),
        "render_warnings": [line for line in render_output.splitlines() if "⚠" in line],
        "lint_summary": [line for line in lint_output.splitlines() if line.startswith("AI-SLOP-LINT:")],
        "checked_at_unix": int(time.time()),
    }
    _atomic_json(check_json, report)
    print(json.dumps(report, ensure_ascii=False))
    if render.returncode != 0 or lint.returncode != 0:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
