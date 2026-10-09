#!/usr/bin/env python3
"""Shared deterministic implementation for MuralPresenter v1.

Canonical inputs: config.json, deck.md, briefs/slide_NN.md, and authored CSS/HTML.

This module is not a model-facing CLI. Public Role entry points live one level
above as ``orchestrator.py``, ``image.py``, and ``slide.py``.
They all import this implementation so planning, assets, rendering, delivery,
font handling, and workspace policy never fork into duplicated copies.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import html
from html.parser import HTMLParser
from io import BytesIO
import json
import math
import os
import fcntl
from pathlib import Path
from typing import Sequence
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import unquote, urlsplit, urlunsplit
import uuid

import requests

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

_PREVIOUS_DONT_WRITE_BYTECODE = sys.dont_write_bytecode
sys.dont_write_bytecode = True
try:
    from .geometry import read_geometry, validate_png
    from .deck_state import compile_deck_state
    from .deck_plan import headings, read_deck
    from .deck_config import read_config
    from .font_bundle import (
        AUTHORING_START,
        bundle_workspace,
        prepare_authoring_fonts,
        resolve_shared_font_reference,
        validate_font_bundle,
    )
    from .image_background import (
        inspect_image,
        print_report as print_image_report,
        remove_checkerboard,
    )
    from .workspace_policy import (
        assert_workspace_clean,
        atomic_copy,
        atomic_write_bytes,
        atomic_write_text,
        audit_workspace,
        clean_workspace,
        page_render_lock,
        recover_stale_render_previews,
        temporary_text,
    )
finally:
    sys.dont_write_bytecode = _PREVIOUS_DONT_WRITE_BYTECODE

CANVAS_W: int
CANVAS_H: int
# 渲染状态数仅作轨迹提示，不等于修复轮次，也不拒绝有效渲染。
MAX_PAGE_RENDER_STATES = 8
AUTHORING_PAGE_RENDER_STATES = 3
MAX_ASSET_DOWNLOAD_BYTES = 50 * 1024 * 1024
# Asset CDNs commonly apply one rate limit per egress IP. Six same-host
# requests saved a few seconds in the happy path but repeatedly turned an
# otherwise valid Image handoff into a provider-wide 429 followed by many
# model-driven retries. Keep acquisition serial by default; deployments with
# independent provider capacity may opt in to more workers explicitly.
try:
    ASSET_DOWNLOAD_WORKERS = max(
        1, int(os.environ.get("CLEAN_ASSET_DOWNLOAD_WORKERS", "1"))
    )
except ValueError:
    ASSET_DOWNLOAD_WORKERS = 1
# External assets are optional production inputs, not a reason to hold the
# whole Deck indefinitely.  Keep every single asset transaction bounded even
# when a remote server accepts the connection but stops making progress.
ASSET_DOWNLOAD_CONNECT_TIMEOUT_S = 5
ASSET_DOWNLOAD_READ_TIMEOUT_S = 20
ASSET_DOWNLOAD_TOTAL_TIMEOUT_S = 45
WIKIMEDIA_SAFE_THUMB_WIDTH = 1280
RASTER_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
ASSET_KINDS = {"real", "generated", "material", "user"}
_RENDER_ENV_ALLOW = {
    "MURAL_RENDER_BROWSER_ENDPOINT",
    "HOME",
    "USER",
    "LOGNAME",
    "PATH",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "XDG_CACHE_HOME",
    "PYTHONPATH",
    "CONDA_PREFIX",
    "LD_LIBRARY_PATH",
    "FONTCONFIG_FILE",
    "PLAYWRIGHT_BROWSERS_PATH",
    "PPT_RENDER_LOCK_PATH",
    "PPT_RENDER_LOCK_DIR",
    "PPT_RENDER_CONCURRENCY",
    "PPT_RENDER_MAX_PAGES",
    "PPT_SKILL_BROWSER_EXE",
    "PPT_SKILL_BROWSER_LIB_DIRS",
    "PPT_SKILL_PYTHON",
}


def _renderer_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key in _RENDER_ENV_ALLOW}


def _run_renderer(cmd: list[str]) -> None:
    broker_value = os.environ.get("CLEAN_RENDER_BROKER_DIR")
    if not broker_value:
        subprocess.run(cmd, check=True, env=_renderer_env())
        return
    broker = Path(broker_value)
    if not (broker / "ready").is_file():
        raise RuntimeError(f"render broker is not ready: {broker}")
    request_id = f"{os.getpid()}-{uuid.uuid4().hex}"
    request = broker / f"{request_id}.request.json"
    response = broker / f"{request_id}.response.json"
    temporary = broker / f".{request_id}.tmp"
    temporary.write_text(json.dumps({"cmd": cmd}, ensure_ascii=False), encoding="utf-8")
    temporary.replace(request)
    timeout = max(
        120,
        int(
            os.environ.get(
                "CLEAN_RENDER_CLIENT_TIMEOUT",
                str(int(os.environ.get("CLEAN_RENDER_JOB_TIMEOUT", "1800")) + 60),
            )
        ),
    )
    deadline = time.monotonic() + timeout
    try:
        while not response.is_file():
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(cmd, timeout)
            time.sleep(0.05)
        result = json.loads(response.read_text(encoding="utf-8"))
        stdout = str(result.get("stdout", ""))
        stderr = str(result.get("stderr", ""))
        if stdout:
            print(stdout, end="" if stdout.endswith("\n") else "\n")
        if stderr:
            print(stderr, end="" if stderr.endswith("\n") else "\n", file=sys.stderr)
        returncode = int(result.get("returncode", 1))
        if returncode:
            raise subprocess.CalledProcessError(
                returncode, cmd, output=stdout, stderr=stderr
            )
    finally:
        temporary.unlink(missing_ok=True)
        request.unlink(missing_ok=True)
        response.unlink(missing_ok=True)


def _root(value: str) -> Path:
    return Path(value).expanduser().resolve()


def _read_author_geometry(root: Path) -> dict:
    global CANVAS_W, CANVAS_H
    geometry = read_geometry(root)
    CANVAS_W, CANVAS_H = geometry["width_px"], geometry["height_px"]
    return geometry


def prepare(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    for rel in ("slides", "assets", "renders"):
        path = root / rel
        if not path.exists():
            path.mkdir(parents=True)
            created.append(rel + "/")
    css = root / "base.css"
    if not css.exists():
        atomic_copy(
            root,
            Path(__file__).resolve().parents[2] / "assets" / "base.css",
            css,
        )
        created.append("base.css")
    print("status:PASS")
    print("created:", ", ".join(created) if created else "none")


def _ensure_authoring_runtime_dependencies(root: Path, plans: list[dict]) -> None:
    """Expose only dependencies required to render the current authored pages.

    Large portable dependencies belong to export packaging.  ECharts is the
    exception needed during authoring; expose its frozen Skill copy as a
    hard-link so every workspace does not consume another full copy.  Export
    staging naturally turns it into an ordinary packaged file.
    """
    uses_echarts = any(
        re.search(r"\becharts\.init\s*\(|echarts\.min\.js", path.read_text(encoding="utf-8"))
        for plan in plans
        if (path := root / "slides" / f"slide_{plan['number']:02d}.html").is_file()
    )
    if not uses_echarts:
        return
    source = (
        Path(__file__).resolve().parents[2]
        / "assets"
        / "vendor"
        / "echarts.min.js"
    )
    target = root / "assets" / "vendor" / "echarts.min.js"
    if not source.is_file() or not source.stat().st_size:
        raise FileNotFoundError("frozen ECharts authoring dependency is missing")
    if target.is_file() and target.stat().st_size:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    try:
        os.link(source, target)
    except OSError:
        atomic_copy(root, source, target)


def restore_base(root: Path) -> None:
    source = Path(__file__).resolve().parents[2] / "assets" / "base.css"
    if not source.is_file():
        raise FileNotFoundError(f"frozen Skill base.css is missing: {source}")
    atomic_copy(root, source, root / "base.css")
    _read_author_geometry(root)
    print("status:PASS")
    print("restored: base.css")


def _normalize_fragment_resource_paths(value: object) -> str:
    """Normalize the harmless fragment-relative spelling of local resources.

    Slide fragments live under ``slides/``, so a model may naturally write
    ``../assets/foo.png``.  The controlled renderer and assembled deck both
    resolve resources from the workspace root and therefore use
    ``assets/foo.png``.  This is a mechanical path spelling, not a
    semantic authoring failure: normalize it in memory for render/finalize
    instead of reopening a completed Owner.  Other parent-directory paths stay
    invalid.
    """
    text = str(value or "")
    text = re.sub(
        r'''((?:src|href|poster)\s*=\s*["'])(?:\.\./)+(assets|fonts)/''',
        r"\1\2/",
        text,
        flags=re.I,
    )
    return re.sub(
        r'''(url\(\s*["']?)(?:\.\./)+(assets|fonts)/''',
        r"\1\2/",
        text,
        flags=re.I,
    )




def _first_heading(text: str, fallback: str) -> str:
    return next((title for _, _, level, title in headings(text) if level == 1), fallback)


def _plan_inventory_payload(root: Path, expected: int | None = None) -> dict:
    _, pages = read_deck(root, expected)
    config = read_config(root)
    return {"authoritative": True, "source": "briefs/slide_NN.md",
            "expected": config["target_page_count"], "requested_check": expected,
            "actual": len(pages), "pages": [page["number"] for page in pages],
            "contiguous": True, "missing": [], "extra": []}


def inspect_plans(root: Path, expected: int | None = None) -> dict:
    """Print the canonical page inventory without shell-based file counting."""
    payload = _plan_inventory_payload(root, expected)
    complete = (
        payload["contiguous"]
        and not payload["missing"]
        and not payload["extra"]
        and (
            payload["expected"] is None
            or payload["actual"] == payload["expected"]
        )
    )
    print("status:PASS" if complete else "status:REPAIR_REQUIRED")
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return payload


def _load_plans(root: Path, expected: int | None = None, *, pages: Sequence[int] | None = None) -> tuple[dict, list[dict]]:
    """读取当前配置与原始文稿；不从制作正文推断控制字段或改写 CSS。"""
    config = read_config(root, require_complete=True)
    selected = tuple(pages) if pages is not None else None
    if selected is not None and config["target_page_count"] is not None:
        if any(type(number) is not int or number < 1 or number > config["target_page_count"] for number in selected):
            raise ValueError("selected pages are outside config.json target_page_count")
    shared, pages = read_deck(root, expected, pages=selected)
    deck = {
        "path": root / "deck.md", "text": shared,
        "language": config["presentation_language"], "creation_config": config,
        "pages": len(pages), "title": _first_heading(shared, "Presentation"),
        "warnings": [],
    }
    plans = [{
        "path": root / page["path"], "plan_section": page["section"], "text": page["text"],
        "number": page["number"], "title": page["title"],
        "speech": page["speech"],
    } for page in pages]
    return deck, plans


def _check_target_count(deck: dict) -> None:
    target = deck["creation_config"]["target_page_count"]
    if target is not None and deck["pages"] != target:
        raise ValueError(f"config.json target_page_count={target}, found {deck['pages']} briefs; use explicit structural edits, not an implicit page-count rewrite")


def validate_plans(root: Path, expected: int | None = None) -> list[dict]:
    deck, plans = _load_plans(root, expected)
    _check_target_count(deck)
    compile_deck_state(root, deck, plans)
    print("status:PASS")
    print(f"plans:{len(plans)}")
    print("[plan-inventory] " + json.dumps(_plan_inventory_payload(root, expected), ensure_ascii=False))
    return plans




def _ensure_authoring_font_bundle(root: Path, *, report: bool = False, update_css: bool = True,
                                  pages: Sequence[int] | None = None) -> dict:
    """从本批计划准备固定字体；便携导出仍在隔离目录读取整册并子集化。"""
    try:
        manifest = prepare_authoring_fonts(root, from_plans=True, update_css=update_css,
                                           pages=tuple(pages) if pages is not None else None)
    except Exception as exc:
        raise RuntimeError(
            "authoring font preflight failed; no slide will be rendered with an "
            f"uncontrolled system fallback: {type(exc).__name__}: {exc}"
        ) from exc
    if report:
        print("typography:" + json.dumps({
            "effective_token_stacks": manifest["token_stacks"],
            "source": "base.css and user font settings after font preparation",
        }, ensure_ascii=False, sort_keys=True))
    return manifest




def _slide_skeleton(number: int) -> str:
    """只建立页面身份与统一容器，不读取内容意图或预置可见标题。"""
    return f"""<section class="slide" id="slide-{number:02d}" data-slide="{number:02d}" data-layout-contract="page-owned">
  <div class="page-background" data-slide-owned="background" aria-hidden="true"></div>
  <div class="page-stage" data-slide-owned="body">
  <!-- SLIDE_AGENT_FILL_START -->
  <!-- SLIDE_AGENT_FILL_END -->
  </div>
</section>
"""


def _skeleton_is_unfilled(text: str) -> bool:
    match = re.search(
        r"<!-- SLIDE_AGENT_FILL_START -->(.*?)<!-- SLIDE_AGENT_FILL_END -->",
        text,
        flags=re.S,
    )
    if not match:
        return False
    return not match.group(1).strip()


def _require_page_body(text: str, page: int) -> None:
    if _skeleton_is_unfilled(text):
        raise ValueError(
            f"page_body_unfilled: slides/slide_{page:02d}.html has an empty "
            "SLIDE_AGENT_FILL_START/SLIDE_AGENT_FILL_END region. "
            "Write the page body inside it; if content already exists elsewhere "
            "outside these markers, move that existing body into the region. "
            "Keep a single visible title; do not add hidden placeholder content. "
            "This check does not mean the whole page is blank."
        )


GROUND_TOKENS = {"dark", "light", "paper"}  # Backward-compatible built-in aliases.
GROUND_CLASS_ALIASES = {
    "dark": {"ground-dark", "page--dark", "theme-dark", "deck-ground-dark", "is-dark"},
    "light": {"ground-light", "page--light", "theme-light", "deck-ground-light", "is-light"},
    "paper": {"ground-paper", "page--paper", "theme-paper", "deck-ground-paper", "is-paper"},
}
GROUND_THEME_VARIABLES = {"--bg", "--content-canvas", "--page-bg", "--body-ink"}


def _parse_ground_token(value: object) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "black": "dark",
        "deep": "dark",
        "night": "dark",
        "white": "light",
        "bright": "light",
        "warm-white": "paper",
        "warm white": "paper",
        "cream": "paper",
    }
    text = aliases.get(text, text)
    return text if re.fullmatch(r"[a-z][a-z0-9_-]*", text) else ""


def _ground_lines(text: str, key: str) -> list[str]:
    pattern = rf"^\s*(?:[-*]\s*)?{re.escape(key).replace('_', r'[_ -]?')}\s*[:=]\s*(.+?)\s*$"
    return [match.group(1).strip() for match in re.finditer(pattern, text, flags=re.I | re.M)]


def _parse_ground_map(text: str) -> dict[int, str]:
    result: dict[int, str] = {}
    for line in [*_ground_lines(text, "ground_map"), *_ground_lines(text, "page_ground_map")]:
        for page, token in re.findall(r"(?:p(?:age)?\s*)?0*([1-9]\d*)\s*(?:=|:|->)\s*([A-Za-z][\w -]*)", line, flags=re.I):
            parsed = _parse_ground_token(token)
            if parsed:
                result[int(page)] = parsed
    return result


def _ground_plan(deck: dict, plans: Sequence[dict]) -> dict[str, object]:
    shared = str(deck.get("text") or "")
    base = next((_parse_ground_token(value) for value in _ground_lines(shared, "base_ground")
                 if _parse_ground_token(value)), "")
    mapping = _parse_ground_map(shared)
    brief = {}
    for plan in plans:
        token = next((_parse_ground_token(value) for value in _ground_lines(str(plan.get("text") or ""), "ground")
                      if _parse_ground_token(value)), "")
        if token:
            brief[int(plan["number"])] = token
    return {"base": base, "map": mapping, "brief": brief}


def _opening_tag(text: str) -> str:
    match = re.search(r"<section\b[^>]*>", text, flags=re.I)
    return match.group(0) if match else ""


def _fragment_ground_token(fragment: str) -> str:
    opening = _opening_tag(fragment)
    classes = set(str(_attribute(opening, "class") or "").split())
    tokens = {token for token, aliases in GROUND_CLASS_ALIASES.items()
              if classes & aliases}
    for css_class in classes:
        for prefix in ("ground-", "page--", "theme-", "deck-ground-"):
            if css_class.startswith(prefix):
                token = _parse_ground_token(css_class[len(prefix):])
                if token:
                    tokens.add(token)
                break
    data_ground = _parse_ground_token(_attribute(opening, "data-ground"))
    if data_ground:
        tokens.add(data_ground)
    if len(tokens) > 1:
        raise ValueError("multiple ground tokens on one slide root: " + ", ".join(sorted(tokens)))
    return next(iter(tokens), "")


def _style_blocks(fragment: str) -> list[str]:
    return [match.group(1) for match in re.finditer(r"<style\b[^>]*>(.*?)</style>", fragment, flags=re.I | re.S)]


def _declarations(text: str) -> dict[str, str]:
    result = {}
    for item in text.split(";"):
        if ":" not in item:
            continue
        key, value = item.split(":", 1)
        result[key.strip().lower()] = value.strip()
    return result


def _selector_targets_slide_root(selector: str, page: int) -> bool:
    selector = re.sub(r"\s+", " ", selector.strip())
    root_selectors = {
        ".slide",
        "section.slide",
        f"#slide-{page:02d}",
        f"#slide-{page:02d}.slide",
        f".slide#slide-{page:02d}",
        f'[data-slide="{page:02d}"]',
        f"[data-slide='{page:02d}']",
        f"[data-slide={page:02d}]",
    }
    return selector in root_selectors


def _fragment_ground_contract_errors(page: int, fragment: str) -> list[str]:
    errors: list[str] = []
    opening = _opening_tag(fragment)
    inline = _attribute(opening, "style") or ""
    inline_decls = _declarations(inline)
    for name in sorted(GROUND_THEME_VARIABLES):
        if name in inline_decls:
            errors.append(f"page {page}: slide root overrides shared ground token {name}; use deck base_ground/ground_map and shared classes")
    if any(name.startswith("background") for name in inline_decls):
        errors.append(f"page {page}: slide root sets background inline; use the declared ground class plus local child layers")
    for block in _style_blocks(fragment):
        for selector, body in re.findall(r"([^{}]+)\{([^{}]+)\}", block, flags=re.S):
            declarations = _declarations(body)
            selectors = [item.strip() for item in selector.split(",")]
            if any(":root" in item for item in selectors):
                touched = sorted(GROUND_THEME_VARIABLES & declarations.keys())
                if touched:
                    errors.append(f"page {page}: page-local style changes :root ground tokens {', '.join(touched)}")
            touched = sorted(GROUND_THEME_VARIABLES & declarations.keys())
            if touched:
                errors.append(f"page {page}: page-local style overrides shared ground tokens {', '.join(touched)}")
            if any(_selector_targets_slide_root(item, page) for item in selectors):
                if any(name.startswith("background") for name in declarations):
                    errors.append(f"page {page}: page-local CSS sets the slide root background; use ground_map/base_ground instead")
    return errors


def _validate_ground_contract(deck: dict, plans: Sequence[dict], rows: Sequence[tuple[int, str]]) -> None:
    plan = _ground_plan(deck, plans)
    base = str(plan["base"] or "")
    mapping = dict(plan["map"])
    brief = dict(plan["brief"])
    fragments = {int(number): fragment for number, fragment in rows}
    tokens: dict[int, str] = {}
    errors: list[str] = []
    for number, fragment in fragments.items():
        errors.extend(_fragment_ground_contract_errors(number, fragment))
        try:
            token = _fragment_ground_token(fragment)
        except ValueError as exc:
            errors.append(f"page {number}: {exc}")
            token = ""
        tokens[number] = token
        expected = mapping.get(number) or brief.get(number) or base
        explicit = number in mapping or number in brief
        if expected and token and token != expected:
            errors.append(f"page {number}: ground token {token!r} does not match declared {expected!r}")
        if explicit and not token:
            errors.append(f"page {number}: declared ground {expected!r} must appear on the slide root class or data-ground")
    if not mapping and not brief:
        used = {token for token in tokens.values() if token}
        if len(used) > 1:
            errors.append(
                "mixed page ground tokens without ground_map: "
                + ", ".join(f"P{page:02d}={token}" for page, token in sorted(tokens.items()) if token)
                + "; default decks keep one base ground unless deck.md declares a justified ground_map"
            )
    if errors:
        raise ValueError("ground_contract_violation:\n- " + "\n- ".join(errors))



def sync_speech(root: Path, expected: int | None = None) -> dict:
    deck, plans = _load_plans(root, expected)
    config = deck["creation_config"]
    language = config.get("speech_language", config["presentation_language"])
    heading = "# 演讲备注" if language.split("-")[0].lower() == "zh" else "# Speaker notes"
    rows = [heading, ""]
    missing, format_warnings = [], []
    for plan in plans:
        speech = plan["speech"].strip()
        if not speech:
            missing.append(plan["number"])
        rows.extend(
            [
                f"## Slide {plan['number']:02d} — {plan['title']}",
                "",
                speech,
                "",
            ]
        )
    count = len(plans) - len(missing)
    if count:
        atomic_write_text(root, root / "speech.md", "\n".join(rows).rstrip() + "\n")
    else:
        (root / "speech.md").unlink(missing_ok=True)
    status = "complete" if not missing else "partial" if count else "omitted"
    report = {"status": status, "page_count": len(plans), "notes_count": count,
              "missing_pages": missing, "format_warnings": format_warnings,
              "omission_reason": "no_valid_speaker_notes" if not count else ""}
    print("status:" + ("PASS" if status == "complete" else status.upper()))
    print(f"speech:{count}/{len(plans)} pages with notes")
    print("[speech] " + json.dumps(report, ensure_ascii=False))
    return report


def scaffold_from_plans(
    root: Path,
    expected: int | None = None,
    *,
    force: bool = False,
    pages: Sequence[int] | None = None,
    update_shared_css: bool = True,
) -> list[int]:
    deck, plans = _load_plans(root, expected, pages=pages)
    if pages is None:
        _check_target_count(deck)
    for plan in plans:
        path = root / "slides" / f"slide_{plan['number']:02d}.html"
        if path.absolute() != path.resolve() or (path.exists() and not path.is_file()):
            raise ValueError(f"scaffold path must be a regular workspace file: {path}")
    if force:
        authored: list[str] = []
        for plan in plans:
            path = root / "slides" / f"slide_{plan['number']:02d}.html"
            if not path.is_file():
                continue
            try:
                # 填充区为空不代表作者没改过标题、背景或区域外内容。
                if path.read_text(encoding="utf-8").strip() != _slide_skeleton(plan["number"]).strip():
                    authored.append(path.name)
            except OSError:
                authored.append(path.name)
        rendered = sorted(
            path.name
            for path in (root / "renders").glob("slide_*.png")
            if path.is_file() and path.stat().st_size > 0
        )
        if authored or rendered:
            details = []
            if authored:
                details.append("authored HTML: " + ", ".join(authored))
            if rendered:
                details.append("render checkpoints: " + ", ".join(rendered))
            raise ValueError(
                "refusing scaffold --force after page production has started; "
                "it would destroy completed Slide work (" + "; ".join(details) + "). "
                "Repair the affected scaffold contract or regenerate only missing pages."
            )
    _read_author_geometry(root)
    _ensure_authoring_runtime_dependencies(root, plans)
    # 在 Owner 并行前准备共享字体，回执报告实际 CSS 栈而非计划中的字体名。
    _ensure_authoring_font_bundle(root, report=True, update_css=update_shared_css, pages=pages)
    written: list[str] = []
    preserved: list[str] = []
    for plan in plans:
        path = root / "slides" / f"slide_{plan['number']:02d}.html"
        if path.exists() and not force:
            preserved.append(path.name)
            continue
        if atomic_write_text(root, path, _slide_skeleton(plan["number"]), overwrite=force):
            written.append(path.name)
        else:
            preserved.append(path.name)
    print("status:PASS")
    print(f"scaffolded:{len(written)} preserved:{len(preserved)}")
    if written:
        print("written:", ", ".join(written))
    return [plan["number"] for plan in plans]


def _attribute(tag: str, name: str) -> str | None:
    match = re.search(
        rf"\b{re.escape(name)}=[\"']([^\"']*)[\"']",
        tag,
        flags=re.I,
    )
    return match.group(1) if match else None


_STANDARD_DOM_NAMESPACE_URI = re.compile(
    r"(?P<quote>[\"'`])https?://www\.w3\.org/"
    r"(?:2000/svg|1999/xhtml|1999/xlink|XML/1998/namespace)"
    r"(?P=quote)",
    flags=re.I,
)


def _fragments(
    root: Path,
    *,
    expected: int | None = None,
    only_page: int | None = None,
) -> tuple[dict, list[dict], list[tuple[int, str]]]:
    deck, plans = _load_plans(root, expected, pages=(only_page,) if only_page is not None else None)
    plan_map = {plan["number"]: plan for plan in plans}
    selected = [only_page] if only_page is not None else list(plan_map)
    rows: list[tuple[int, str]] = []
    for number in selected:
        path = root / "slides" / f"slide_{number:02d}.html"
        if not path.is_file():
            raise FileNotFoundError(f"{path.name} is missing")
        text = path.read_text(encoding="utf-8").strip()
        text = _normalize_fragment_resource_paths(text)
        opening = re.search(r"<section\b[^>]*>", text, flags=re.I)
        if not opening:
            raise ValueError(f"{path.name} must contain a slide root <section>")
        raw_slide = _attribute(opening.group(0), "data-slide")
        try:
            actual_slide = int(str(raw_slide or ""))
        except ValueError as exc:
            raise ValueError(
                f"{path.name} root <section> needs numeric data-slide={number:02d}"
            ) from exc
        if actual_slide != number:
            raise ValueError(
                f"{path.name} data-slide={actual_slide:02d}, expected {number:02d}"
            )
        if re.search(
            r'''(?:src|href|poster)\s*=\s*["'](?:\.\./)+''', text, flags=re.I
        ):
            raise ValueError(
                f"{path.name} contains a parent-directory asset path; use a "
                "workspace-root-relative assets/ path"
            )
        _require_page_body(text, number)
        rows.append((number, text))
    _validate_ground_contract(deck, plans, rows)
    return deck, plans, rows


_IMAGE_METADATA_DEFAULTS = {
    "kind": "generated", "slides": [], "source": "", "download": "", "purpose": "",
    "crop": "", "display": "", "quality_intent": "", "expect_transparent": False,
    "subject_region": "", "text_safe_region": "", "notes": "",
}


def _registered_images(root: Path) -> list[dict]:
    """图片声明只读 Harness 真源；catalog.md 不参与执行。"""
    state_path = root / "_state" / "artifacts.json"
    if not state_path.is_file():
        return []
    state = json.loads(state_path.read_text(encoding="utf-8"))
    entries = []
    for record in state["artifacts"].values():
        metadata = record.get("image_metadata")
        if record.get("kind") != "image" or not metadata:
            continue
        entries.append({
            **_IMAGE_METADATA_DEFAULTS, **metadata,
            "id": record["artifact_id"], "path": record["path"],
            "owner_id": record["owner_id"],
            "status": {"declared": "pending", "committed": "ready", "broken": "unavailable"}.get(record["status"], record["status"]),
            "failure_reason": record.get("reason", ""),
            "sha256": record.get("sha256", ""),
        })
    return entries


def _image_registration_receipt(entry: dict, status: str, reason: str = "") -> None:
    """每项结果立即输出；调用方沿现有工具事务写入 ArtifactStore。"""
    print("MURAL_ASSET " + json.dumps({
        "artifact_id": entry["id"], "path": entry["path"], "status": status,
        "source_url": entry.get("source", ""), "reason": reason,
        **({"reviewed_sha256": entry["reviewed_sha256"]} if entry.get("reviewed_sha256") else {}),
        "image_metadata": {key: entry.get(key, value) for key, value in _IMAGE_METADATA_DEFAULTS.items()},
    }, ensure_ascii=False), flush=True)


def register_image(root: Path, asset_id: str, path_value: str, *, status: str = "available",
                   reason: str = "", reviewed: bool = False, **metadata) -> None:
    """校验显式声明并返回收据；不在 Skill 内建立第二个素材数据库。"""
    from PIL import Image

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", asset_id):
        raise ValueError("--id must contain only letters, numbers, dot, underscore or hyphen")
    if status not in {"available", "pending", "unavailable"}:
        raise ValueError("--status must be available, pending or unavailable")
    unknown = set(metadata) - set(_IMAGE_METADATA_DEFAULTS)
    if unknown:
        raise ValueError(f"unknown image metadata: {sorted(unknown)}")
    entry = {**_IMAGE_METADATA_DEFAULTS, **metadata, "id": asset_id}
    path = _asset_path(root, path_value)
    entry["path"] = path.relative_to(root.resolve()).as_posix()
    if path.suffix.lower() not in RASTER_SUFFIXES:
        raise ValueError("--path must name a raster image directly under assets/")
    if entry["kind"] not in ASSET_KINDS:
        raise ValueError(f"--kind must be one of {sorted(ASSET_KINDS)}")
    if not isinstance(entry["slides"], list) or any(type(page) is not int or page < 1 for page in entry["slides"]):
        raise ValueError("--slides must contain comma-separated positive page numbers")
    entry["slides"] = sorted(set(entry["slides"]))
    if reviewed and (status != "available" or not entry["slides"]):
        raise ValueError("--reviewed requires --status available and explicit --slides consumers")
    if status == "pending" and (entry["kind"] != "real" or not str(entry["download"]).startswith(("https://", "http://"))):
        raise ValueError("pending registration requires --kind real and --download URL")
    if status == "unavailable" and not reason.strip():
        raise ValueError("unavailable registration requires --reason")
    if status == "available":
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"registered image is missing or empty: {entry['path']}")
        if path.stat().st_size > MAX_ASSET_DOWNLOAD_BYTES:
            raise ValueError("registered image exceeds the asset size limit")
        with Image.open(path) as image:
            expected_format = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}[path.suffix.lower()]
            if image.format != expected_format:
                raise ValueError(f"image bytes are {image.format}, but extension declares {expected_format}: {entry['path']}")
            image.verify()
        if entry["kind"] in {"material", "user"}:
            filename = "material-figures.json" if entry["kind"] == "material" else "user-images.json"
            receipt_path = root / "_trace" / filename
            records = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.is_file() else {}
            receipt = records.get("assets", {}).get(entry["path"], {})
            if receipt.get("sha256") != hashlib.sha256(path.read_bytes()).hexdigest():
                raise ValueError(f"{entry['kind']} image needs a current image.py crop-material/register-user receipt: {entry['path']}")
    if reviewed:
        entry["reviewed_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    _image_registration_receipt(entry, status, reason)


def _catalog_failed_only_pages(entries: list[dict]) -> set[int]:
    """Return pages whose catalog has failures but no usable alternative asset.

    A page may intentionally request several assets.  One failed candidate must
    not poison the whole page when another resolved asset is still available.
    """
    failed_pages = {
        number
        for entry in entries
        if entry.get("status") in {"failed", "unavailable", "pending"}
        for number in entry["slides"]
    }
    usable_pages = {
        number
        for entry in entries
        if entry.get("status") not in {"failed", "unavailable", "pending"}
        for number in entry["slides"]
    }
    return failed_pages - usable_pages


def _asset_path(root: Path, value: str) -> Path:
    path = (root / value).resolve()
    assets = root.resolve() / "assets"
    if path.parent != assets or Path(value).is_absolute() or ".." in Path(value).parts:
        raise ValueError(f"asset must be directly under assets/: {value}")
    return path


def _wikimedia_transport_candidates(url: str) -> list[tuple[str, str]]:
    """Return a bounded set of equivalent Wikimedia transport URLs.

    Image search occasionally returns a valid Commons file while the model
    synthesizes a non-whitelisted thumbnail width (for example ``1600px``).
    That is a transport mistake, not a semantic asset failure.  Recover it
    deterministically inside the same acquisition attempt so it does not burn
    a model turn, a replacement budget, or strand a Slide behind a task
    barrier.  The original file is the final same-identity fallback and remains
    protected by ``MAX_ASSET_DOWNLOAD_BYTES``.
    """
    parsed = urlsplit(str(url or "").strip())
    candidates: list[tuple[str, str]] = [(url, "catalog")]
    if parsed.netloc.lower() != "upload.wikimedia.org":
        return candidates
    match = re.fullmatch(
        r"(?P<prefix>/wikipedia/commons)/thumb/"
        r"(?P<shard>[^/]+/[^/]+)/(?P<filename>[^/]+)/(?P<thumb>[^/]+)",
        parsed.path,
    )
    if not match:
        return candidates
    thumb = match.group("thumb")
    width_match = re.match(r"^(?P<width>\d+)px-(?P<suffix>.+)$", thumb)
    if not width_match:
        return candidates

    safe_thumb = (
        f"{WIKIMEDIA_SAFE_THUMB_WIDTH}px-{width_match.group('suffix')}"
    )
    if thumb != safe_thumb:
        safe_path = (
            f"{match.group('prefix')}/thumb/{match.group('shard')}/"
            f"{match.group('filename')}/{safe_thumb}"
        )
        candidates.append((
            urlunsplit((
                parsed.scheme,
                parsed.netloc,
                safe_path,
                parsed.query,
                "",
            )),
            f"wikimedia-{WIKIMEDIA_SAFE_THUMB_WIDTH}px",
        ))
    original_path = (
        f"{match.group('prefix')}/{match.group('shard')}/"
        f"{match.group('filename')}"
    )
    candidates.append((
        urlunsplit((
            parsed.scheme,
            parsed.netloc,
            original_path,
            parsed.query,
            "",
        )),
        "wikimedia-original",
    ))

    deduped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for candidate, reason in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        deduped.append((candidate, reason))
    return deduped


def _download_image_payload(
    url: str,
    *,
    referer: str,
    deadline: float,
) -> bytes:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("asset download exceeded its total time budget")
    connect_timeout = max(
        1.0,
        min(float(ASSET_DOWNLOAD_CONNECT_TIMEOUT_S), remaining),
    )
    read_timeout = max(
        1.0,
        min(float(ASSET_DOWNLOAD_READ_TIMEOUT_S), remaining),
    )
    with requests.get(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 CleanPresentationImage/1.0",
            "Referer": referer or url,
        },
        stream=True,
        timeout=(connect_timeout, read_timeout),
    ) as response:
        response.raise_for_status()
        declared = int(response.headers.get("content-length") or 0)
        if declared > MAX_ASSET_DOWNLOAD_BYTES:
            raise ValueError(
                f"download is larger than {MAX_ASSET_DOWNLOAD_BYTES // 1024 // 1024} MiB"
            )
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(chunk_size=256 * 1024):
            if time.monotonic() > deadline:
                raise TimeoutError("asset download exceeded its total time budget")
            if not chunk:
                continue
            size += len(chunk)
            if size > MAX_ASSET_DOWNLOAD_BYTES:
                raise ValueError(
                    f"download exceeded {MAX_ASSET_DOWNLOAD_BYTES // 1024 // 1024} MiB"
                )
            chunks.append(chunk)
    payload = b"".join(chunks)
    if not payload:
        raise ValueError("download returned an empty response")
    return payload


def _download_image_asset(root: Path, entry: dict, replace: bool) -> tuple[str, str]:
    target = _asset_path(root, entry["path"])
    if target.is_file() and target.stat().st_size > 0 and not replace:
        receipt = _load_download_receipts(root).get(entry["id"], {})
        _validate_download_receipt(entry, receipt, hashlib.sha256(target.read_bytes()).hexdigest(), required=True)
        return entry["id"], "reused"

    url = str(entry.get("download") or "").strip()
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        raise ValueError("download must be a public http(s) URL without credentials")

    deadline = time.monotonic() + ASSET_DOWNLOAD_TOTAL_TIMEOUT_S
    payload = b""
    resolved_url = url
    fallback_reason = "catalog"
    errors: list[str] = []
    entry["_download_attempts"] = []
    candidates = _wikimedia_transport_candidates(url)
    for candidate_index, (candidate_url, reason) in enumerate(candidates):
        attempt_started = time.monotonic()
        try:
            payload = _download_image_payload(
                candidate_url,
                referer=str(entry.get("source") or url),
                deadline=deadline,
            )
            resolved_url = candidate_url
            fallback_reason = reason
            entry["_download_attempts"].append({"attempt": candidate_index + 1, "source_url": candidate_url,
                                               "status": "succeeded", "transport": reason,
                                               "duration_ms": round((time.monotonic() - attempt_started) * 1000)})
            break
        except Exception as exc:  # noqa: BLE001 - preserve bounded fallback evidence
            errors.append(f"{reason}: {type(exc).__name__}: {exc}")
            status_code = (
                int(exc.response.status_code)
                if isinstance(exc, requests.HTTPError)
                and getattr(exc, "response", None) is not None
                else 0
            )
            entry["_download_attempts"].append({
                "attempt": candidate_index + 1, "source_url": candidate_url, "status": "failed", "transport": reason,
                "error_kind": "http" if status_code else "timeout" if isinstance(exc, (TimeoutError, requests.Timeout)) else "network" if isinstance(exc, requests.RequestException) else "download_validation",
                "error_type": type(exc).__name__, "error_message": str(exc), "http_status": status_code or None,
                "duration_ms": round((time.monotonic() - attempt_started) * 1000),
            })
            # Authentication/rate-limit failures apply to the provider rather
            # than the synthesized thumbnail width.  Do not hammer the same
            # CDN with equivalent URLs; return immediately so Image can choose
            # another source or hand the page to a code-visual fallback.
            if status_code == 429:
                errors[-1] += (
                    f"; provider_rate_limited host={parsed.netloc}; "
                    "do not retry another URL on this host in the current run; "
                    "choose an equivalent asset from a different provider or "
                    "mark this asset unavailable"
                )
            elif status_code in {401, 403}:
                errors[-1] += (
                    f"; provider_access_blocked host={parsed.netloc}; "
                    "choose an equivalent public source instead of retrying "
                    "this protected provider"
                )
            if status_code in {401, 403, 429}:
                break
            if candidate_index + 1 >= len(candidates):
                break
            if time.monotonic() >= deadline:
                break
    if not payload:
        raise ValueError(
            "asset download failed within bounded fallback policy: "
            + " | ".join(errors)
        )

    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = 200_000_000
    try:
        with Image.open(BytesIO(payload)) as source:
            image = ImageOps.exif_transpose(source).copy()
    except Exception as exc:
        entry["_image_error_kind"] = "image_decode"
        raise ValueError(f"response is not a decodable image: {exc}") from exc

    image.thumbnail((3200, 3200), Image.Resampling.LANCZOS)
    suffix = target.suffix.lower()
    has_alpha = image.mode in {"RGBA", "LA"} or "transparency" in image.info
    buffer = BytesIO()
    if suffix == ".png":
        image.convert("RGBA" if has_alpha else "RGB").save(
            buffer,
            format="PNG",
            optimize=True,
        )
    elif suffix == ".webp":
        image.convert("RGBA" if has_alpha else "RGB").save(
            buffer,
            format="WEBP",
            quality=92,
            method=6,
        )
    elif suffix in {".jpg", ".jpeg"}:
        image.convert("RGB").save(
            buffer,
            format="JPEG",
            quality=92,
            optimize=True,
        )
    else:
        raise ValueError(
            f"download target must use one of {sorted(RASTER_SUFFIXES)}"
        )
    atomic_write_bytes(root, target, buffer.getvalue())
    entry["_resolved_download"] = resolved_url
    entry["_transport_fallback"] = fallback_reason
    return entry["id"], "downloaded"


def _download_receipt_path(root: Path) -> Path:
    return root / "_trace" / "image-download-receipts.json"


def _load_download_receipts(root: Path) -> dict[str, dict]:
    try:
        payload = json.loads(_download_receipt_path(root).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ValueError(f"image_download_receipt_io: cannot read {_download_receipt_path(root)}: {exc}; preserve the receipt and fix access before retrying") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"image_download_receipt_invalid: {_download_receipt_path(root)} is corrupt; restore the recorded receipt before writing new entries") from exc
    assets = payload.get("assets") if isinstance(payload, dict) else None
    if not isinstance(assets, dict) or any(not isinstance(receipt, dict) for receipt in assets.values()):
        raise ValueError("image_download_receipt_invalid: assets must be an object of receipts; original file not overwritten")
    return {
        str(asset_id): dict(receipt)
        for asset_id, receipt in assets.items()
    }


def _validate_download_receipt(entry: dict, receipt: dict, current_sha: str, *, required: bool) -> None:
    """下载事实与可编辑来源定位分开；保留原始下载凭据，不为元数据变化重新下载。"""
    identity = entry["id"]
    if not receipt:
        if required:
            raise ValueError(f"image_download_receipt_missing: {identity!r} has no matching download receipt; "
                "file existence does not prove a successful fetch. Restore its original successful receipt, "
                f"or explicitly fetch the declared URL with image.py fetch . --replace --asset {identity}")
        return
    expected = {"path": entry["path"], "sha256": current_sha}
    if entry.get("download"):
        expected["download"] = entry["download"]
    differences = {key: {"download_receipt": receipt.get(key), "current": value}
                   for key, value in expected.items() if str(receipt.get(key) or "") != str(value or "")}
    if differences:
        steps = []
        if "path" in differences:
            steps.append("correct the registered path or restore the original downloaded file; do not infer a move")
        if "sha256" in differences:
            steps.append("current bytes differ from the download; restore them or register the deliberately transformed asset with its own provenance")
        if "download" in differences:
            steps.append("correct --download if only registration is wrong; use fetch --replace --asset "
                         + identity + " only when replacing bytes from the new URL is intended")
        raise ValueError("image_download_receipt_mismatch: " + identity + "; "
            + json.dumps(differences, ensure_ascii=False) + "; " + "; ".join(steps))
    if str(receipt.get("source") or "") != str(entry.get("source") or ""):
        print("MURAL_DIAGNOSTIC " + json.dumps({"type": "image_download", "asset_id": identity,
            "operation": "receipt_check", "status": "source_metadata_changed",
            "download_receipt_source": receipt.get("source", ""), "registered_source": entry.get("source", ""),
            "note": "Source locator changed; downloaded bytes and URL still match. Original receipt retained, no re-download or new source verification claimed."
        }, ensure_ascii=False), flush=True)


def _write_download_receipts(root: Path, receipts: dict[str, dict]) -> None:
    atomic_write_text(
        root,
        _download_receipt_path(root),
        json.dumps(
            {
                "schema": "mural.image-download-receipts.v1",
                "assets": receipts,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n",
    )


def _record_download_receipts(root: Path, entries: list[dict]) -> None:
    receipts = _load_download_receipts(root)
    for entry in entries:
        path = _asset_path(root, entry["path"])
        if not path.is_file() or path.stat().st_size == 0:
            continue
        receipts[entry["id"]] = {
            "path": entry["path"],
            "source": entry.get("source", ""),
            "download": entry.get("download", ""),
            "resolved_download": entry.get(
                "_resolved_download", entry.get("download", "")
            ),
            "transport_fallback": entry.get("_transport_fallback", "catalog"),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    _write_download_receipts(root, receipts)


def _image_diagnostic(entry: dict, status: str, *, started: float, error: Exception | None = None) -> None:
    """逐资产输出机器可读诊断；身份、脱敏与落盘由调用方的工具日志层统一处理。"""
    attempts = entry.get("_download_attempts") or []
    last = attempts[-1] if attempts else {}
    print("MURAL_DIAGNOSTIC " + json.dumps({
        "type": "image_download", "asset_id": entry["id"], "operation": "fetch", "status": status,
        "source_url": entry.get("download", ""), "output_path": entry["path"],
        "attempt": len(attempts), "attempts": attempts,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "error_kind": (entry.get("_image_error_kind") or last.get("error_kind") or "download_validation") if error else "",
        "error_type": type(error).__name__ if error else "", "error_message": str(error) if error else "",
        "http_status": last.get("http_status"),
    }, ensure_ascii=False), flush=True)


def fetch_images(
    root: Path,
    *,
    replace: bool = False,
    asset_ids: list[str] | tuple[str, ...] | None = None,
) -> None:
    entries = _registered_images(root)
    real_entries = [
        entry
        for entry in entries
        if entry["kind"] == "real"
        and entry.get("status") not in {"failed", "unavailable"}
    ]
    selected_ids = {
        str(asset_id).strip()
        for asset_id in (asset_ids or [])
        if str(asset_id).strip()
    }
    if replace and not selected_ids:
        raise ValueError(
            "replacement fetch requires at least one explicit --asset ASSET_ID"
        )
    if selected_ids:
        known = {entry["id"] for entry in real_entries}
        unknown = sorted(selected_ids - known)
        if unknown:
            raise ValueError(
                "replacement asset is missing, unavailable, or not kind real: "
                + ", ".join(unknown)
            )
        real_entries = [
            entry for entry in real_entries if entry["id"] in selected_ids
        ]
    if replace:
        pending = [
            entry
            for entry in real_entries
            if entry.get("download")
        ]
    else:
        pending = [
            entry
            for entry in real_entries
            if entry.get("status") == "pending"
            or not _asset_path(root, entry["path"]).is_file()
            or _asset_path(root, entry["path"]).stat().st_size == 0
        ]
        missing = [entry["id"] for entry in pending if not entry["download"]]
        if missing:
            raise ValueError(
                "registered real images need --download URL before image.py fetch: "
                + ", ".join(missing)
            )

    results: list[tuple[str, str]] = []
    errors: list[str] = []
    workers = min(ASSET_DOWNLOAD_WORKERS, max(1, len(pending)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        for entry in pending:
            started = time.monotonic()
            _image_diagnostic(entry, "started", started=started)
            futures[executor.submit(_download_image_asset, root, entry, replace)] = (entry, started)
        for future in as_completed(futures):
            entry, started = futures[future]
            try:
                completed = future.result()
                results.append(completed)
                if completed[1] == "downloaded":
                    _record_download_receipts(root, [entry])
                _image_registration_receipt(entry, "available")
                _image_diagnostic(entry, "succeeded", started=started)
            except Exception as exc:
                errors.append(f"{entry['id']}: {exc}")
                _image_registration_receipt(entry, "unavailable", str(exc))
                _image_diagnostic(entry, "failed", started=started, error=exc)
    reused = len(real_entries) - len(pending) + sum(status == "reused" for _, status in results)
    if errors:
        print("status:PARTIAL")
        print(f"errors:{len(errors)}")
        for error in sorted(errors):
            print(f"  {error}")
    else:
        print("status:PASS")
    downloaded = sum(status == "downloaded" for _, status in results)
    print(f"downloaded:{downloaded}")
    print(f"reused:{reused}")
    for asset_id, status in sorted(results):
        print(f"{asset_id}:{status}")
    successful_ids = {asset_id for asset_id, _ in results}
    successful_entries = [
        entry for entry in pending if entry["id"] in successful_ids
    ]
    for entry in successful_entries:
        resolved = str(entry.get("_resolved_download") or "")
        requested = str(entry.get("download") or "")
        if resolved and resolved != requested:
            print(
                f"{entry['id']}:transport_fallback:"
                f"{entry.get('_transport_fallback')}:{resolved}"
            )
    if errors:
        raise ValueError(
            "one or more image downloads failed; per-asset results were emitted above"
        )


def _normalize_fragment_base_css(text: str) -> str:
    """Remove only the standalone shared-CSS link, preserving other HTML bytes."""
    class SharedCSSLinks(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.removals = []
            self.line_offsets = [0]
            self.line_offsets.extend(match.end() for match in re.finditer("\n", text))

        def handle_starttag(self, tag, attrs):
            if tag != "link":
                return
            values = dict(attrs)
            # Duplicate identity attributes are ambiguous; never discard them.
            if any(sum(key == name for key, _ in attrs) != 1 for name in ("rel", "href")):
                return
            if ("stylesheet" not in str(values.get("rel") or "").lower().split()
                    or str(values.get("href") or "").strip() != "../base.css"):
                return
            line, column = self.getpos()
            start = self.line_offsets[line - 1] + column
            self.removals.append((start, start + len(self.get_starttag_text())))

    parser = SharedCSSLinks()
    parser.feed(text)
    parser.close()
    for start, end in reversed(parser.removals):
        text = text[:start] + text[end:]
    return text


def _document(css: str, fragments: list[tuple[int, str]], language: str) -> str:
    # The deck inlines base.css; use the same fragment bytes as dependency hashing.
    sections = "\n\n".join(_normalize_fragment_base_css(text) for _, text in fragments)
    html_language = html.escape(language, quote=True)
    return f"""<!doctype html>
<html lang="{html_language}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Presentation</title>
  <style>
{css}
  </style>
</head>
<body>
  <div class="stage">
    <main class="deck" id="deck">
{sections}
    </main>
  </div>
  <script>
  (() => {{
    const slides = [...document.querySelectorAll('.slide[data-slide]')];
    const byNumber = new Map(slides.map(s => [Number(s.dataset.slide), s]));
    const ordered = [...byNumber.keys()].sort((a, b) => a - b);
    let current = Number(new URLSearchParams(location.search).get('slide')) || ordered[0] || 1;
    function fitDeck() {{
      const deck = document.getElementById('deck');
      const w = deck.offsetWidth, h = deck.offsetHeight;
      const scale = Math.min(innerWidth / w, innerHeight / h);
      deck.style.left = `${{(innerWidth - w * scale) / 2}}px`;
      deck.style.top = `${{(innerHeight - h * scale) / 2}}px`;
      deck.style.transform = `scale(${{scale}})`;
    }}
    function go(number) {{
      if (!byNumber.has(number)) return;
      current = number;
      slides.forEach(s => {{
        s.classList.toggle('active', Number(s.dataset.slide) === current);
      }});
      dispatchEvent(new CustomEvent('slidechange', {{ detail: {{ slide: current }} }}));
    }}
    function step(delta) {{
      const index = ordered.indexOf(current);
      go(ordered[Math.max(0, Math.min(ordered.length - 1, index + delta))]);
    }}
    addEventListener('keydown', event => {{
      if (['ArrowRight', 'PageDown', ' '].includes(event.key)) step(1);
      if (['ArrowLeft', 'PageUp'].includes(event.key)) step(-1);
    }});
    addEventListener('resize', fitDeck);
    const fontsReady = Promise.resolve(document.fonts?.ready).catch(() => undefined);
    const initial = byNumber.has(current) ? current : ordered[0];
    fitDeck();
    window.cleanDeck = {{ go, step, count: slides.length, fontsReady }};
    go(initial);
    const start = () => requestAnimationFrame(() => {{
      fitDeck();
      window.focus();
    }});
    if (document.readyState === 'complete') start();
    else addEventListener('load', start, {{ once: true }});
  }})();
  </script>
</body>
</html>
"""


def build(root: Path, expected: int | None = None) -> list[int]:
    _read_author_geometry(root)
    css_path = root / "base.css"
    if not css_path.is_file():
        raise FileNotFoundError("base.css is missing; run prepare first")
    deck, plans, fragments = _fragments(root, expected=expected)
    _ensure_authoring_runtime_dependencies(root, plans)
    _ensure_authoring_font_bundle(root)
    reconciled_assets = (
        []
    )
    atomic_write_text(
        root,
        root / "_trace" / "asset-usage.json",
        json.dumps(
            {
                "schema": "mural.asset-usage.v1",
                "assets": [
                    {
                        "id": str(entry.get("id") or ""),
                        "path": str(entry.get("path") or ""),
                        "actual_slides": list(entry.get("slides") or []),
                    }
                    for entry in sorted(
                        reconciled_assets,
                        key=lambda value: str(value.get("id") or ""),
                    )
                ],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    css = css_path.read_text(encoding="utf-8")
    document = _document(css, fragments, deck["language"])
    atomic_write_text(root, root / "present.html", document)
    print("built:", root / "present.html")
    return [number for number, _ in fragments]


def _page_render_state(
    root: Path,
    page: int,
) -> tuple[Path, list[str], dict[str, list[str]], str, list[dict]]:
    path = root / "_trace" / "slide-render-states" / f"page_{page:02d}.json"
    if not path.is_file():
        return path, [], {}, "", []
    payload = json.loads(path.read_text(encoding="utf-8"))
    hashes = payload.get("hashes") if isinstance(payload, dict) else None
    if not isinstance(hashes, list) or not all(isinstance(value, str) for value in hashes):
        raise ValueError(f"{path.relative_to(root)} is malformed")
    attempt_hashes = payload.get("attempt_hashes") if isinstance(payload, dict) else None
    if not isinstance(attempt_hashes, dict):
        attempt_hashes = {"legacy": list(hashes)}
    normalized_attempts: dict[str, list[str]] = {}
    for key, values in attempt_hashes.items():
        if isinstance(key, str) and isinstance(values, list) and all(
            isinstance(value, str) for value in values
        ):
            normalized_attempts[key] = list(values)
    published_hash = str(payload.get("published_hash") or "")
    layout_defects = payload.get("layout_defects") if isinstance(payload, dict) else []
    if not isinstance(layout_defects, list):
        layout_defects = []
    return path, list(hashes), normalized_attempts, published_hash, layout_defects


def _render_input_digest(root: Path, css: str, fragment: str, loaded_resources=(), *, presentation_language: str | None = None) -> str:
    """像素输入包含呈现语言；工作语言及目标页数不进入渲染缓存键。"""
    fragment = _normalize_fragment_base_css(fragment)
    def css_references(text: str) -> list[str]:
        values = [match.strip().strip("\"'") for match in re.findall(r"url\(([^)]+)\)", text, re.I)]
        values.extend(re.findall(r"@import\s+['\"]([^'\"]+)['\"]", text, re.I))
        return values

    class Resources(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.values = []
            self.in_style = False

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag in {"img", "image", "use", "object", "script"}:
                self.values.extend(attrs[key] for key in ("src", "href", "xlink:href", "data") if attrs.get(key))
            if tag == "link" and "stylesheet" in str(attrs.get("rel") or "").lower() and attrs.get("href"):
                self.values.append(attrs["href"])
            if tag in {"img", "source"}:
                self.values.extend(item.strip().split()[0] for item in str(attrs.get("srcset") or "").split(",") if item.strip())
            self.values.extend(css_references(attrs.get("style") or ""))
            if tag == "style":
                self.in_style = True

        def handle_endtag(self, tag):
            if tag == "style":
                self.in_style = False

        def handle_data(self, text):
            if self.in_style:
                self.values.extend(css_references(text))

    def markup_references(text: str) -> list[str]:
        parser = Resources()
        parser.feed(text)
        return parser.values

    root = root.resolve()
    if presentation_language is None:
        presentation_language = read_config(root, require_complete=True)["presentation_language"]
    digest = hashlib.sha256()
    digest.update(json.dumps({"presentation_language": presentation_language}, sort_keys=True).encode("utf-8"))
    digest.update(b"\0")
    digest.update(css.encode("utf-8"))
    digest.update(b"\0")
    digest.update(fragment.encode("utf-8"))
    queue = [(root / "base.css", value) for value in css_references(css)]
    queue.extend((root / "present.html", value) for value in markup_references(fragment))
    queue.extend((root / "present.html", value) for value in loaded_resources if not urlsplit(value).scheme)
    dependencies: dict[str, bytes] = {}
    while queue:
        origin, value = queue.pop(0)
        value = value.strip()
        parsed = urlsplit(value)
        if not value or value.startswith("#") or parsed.scheme in {"data", "http", "https"}:
            continue
        if parsed.scheme == "file":
            _source, expected = resolve_shared_font_reference(root, value)
            dependencies[value] = expected.encode("ascii")
            continue
        if parsed.scheme or parsed.netloc:
            raise ValueError(f"unsupported render dependency: {value}")
        raw = unquote(parsed.path)
        if origin.suffix.lower() in {".css", ".svg"} and not raw.startswith("/"):
            path = (origin.parent / raw).resolve()
        else:
            raw = re.sub(r"^(?:\.\./)+(assets|fonts)/", r"\1/", raw.lstrip("/"))
            path = (root / raw).resolve()
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError as exc:
            raise ValueError(f"render dependency escapes workspace: {value}") from exc
        if relative in dependencies:
            continue
        try:
            data = path.read_bytes()
        except OSError:
            data = b"<missing>"
        dependencies[relative] = data
        if data != b"<missing>" and path.suffix.lower() in {".css", ".svg"}:
            text = data.decode("utf-8")
            nested = css_references(text) if path.suffix.lower() == ".css" else markup_references(text)
            queue.extend((path, reference) for reference in nested)
    for value, data in sorted(dependencies.items()):
        digest.update(b"\0dep:")
        digest.update(value.encode("utf-8"))
        digest.update(data)
    return digest.hexdigest()


def _page_digest(root: Path, page: int) -> str:
    css = (root / "base.css").read_text(encoding="utf-8")
    fragment = (root / "slides" / f"slide_{page:02d}.html").read_text(
        encoding="utf-8"
    ).strip()
    fragment = _normalize_fragment_resource_paths(fragment)
    return _render_input_digest(root, css, fragment, _page_render_metadata(root, page).get("loaded_resources", []))


def _validate_render_inputs(root: Path, input_hashes: dict) -> None:
    for reference, expected in input_hashes.items():
        scheme = urlsplit(reference).scheme
        if scheme in {"http", "https"}:
            # 远程 hash 记录本次实际加载的响应；不是本地文件身份，也不重新请求替换捕获证据。
            continue
        if scheme == "file":
            _, actual = resolve_shared_font_reference(root, reference)
        else:
            path = (root / reference).resolve()
            if not path.is_relative_to(root.resolve()):
                raise ValueError(f"render source escapes workspace: {reference}")
            actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
        if actual != expected:
            raise ValueError(f"render input changed after capture: {reference}; inspect the current version")


def _is_authoring_attempt(attempt: str) -> bool:
    return str(attempt).startswith(("slide", "author-orchestrator-"))


def _publish_render_state(
    root: Path,
    page: int,
    path: Path,
    hashes: list[str],
    attempt_hashes: dict[str, list[str]],
    digest: str,
    layout_defects: list[dict] | None = None,
) -> None:
    existing_payload: dict = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            existing_payload = loaded
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass

    defect_history = existing_payload.get("layout_defect_history")
    if not isinstance(defect_history, list):
        defect_history = []
    defect_history = [
        dict(entry)
        for entry in defect_history
        if isinstance(entry, dict)
        and isinstance(entry.get("defects"), list)
        and entry.get("defects")
    ]

    def remember_defects(render_hash: str, defects: object) -> None:
        if not render_hash or not isinstance(defects, list) or not defects:
            return
        normalized = [dict(item) for item in defects if isinstance(item, dict)]
        if not normalized:
            return
        if any(
            str(entry.get("render_hash") or "") == render_hash
            for entry in defect_history
        ):
            return
        defect_history.append({
            "render_hash": render_hash,
            "defects": normalized[:12],
        })

    remember_defects(
        str(existing_payload.get("published_hash") or ""),
        existing_payload.get("layout_defects"),
    )
    remember_defects(digest, layout_defects)
    defect_history = defect_history[-32:]

    authoring_hashes = list(dict.fromkeys(
        value
        for attempt, values in attempt_hashes.items()
        if _is_authoring_attempt(attempt)
        for value in values
    ))
    atomic_write_text(
        root,
        path,
        json.dumps(
            {
                "page": page,
                "limit": MAX_PAGE_RENDER_STATES,
                "authoring_attempt_limit": AUTHORING_PAGE_RENDER_STATES,
                "authoring_target_mode": "soft",
                "authoring_target_exceeded": (
                    len(authoring_hashes) > AUTHORING_PAGE_RENDER_STATES
                ),
                "limit_scope": "page_lifecycle",
                "mode": "attempted-filled-html-css",
                "hashes": hashes,
                "attempt_hashes": attempt_hashes,
                "authoring_hashes": authoring_hashes,
                "published_hash": digest,
                "layout_defects": list(layout_defects or []),
                "layout_defect_history": defect_history,
                "quality_action_count": len(layout_defects or []),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )


def _page_render_metadata_path(root: Path, page: int) -> Path:
    return root / "_trace" / "render-cache" / f"page_{page:02d}.json"


def _page_render_metadata(root: Path, page: int) -> dict:
    path = _page_render_metadata_path(root, page)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("pages", []) if isinstance(payload, dict) else []
        return rows[0] if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict) else {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}


def _isolated_render_geometry(root: Path, page: int) -> dict:
    """Read the current page-local renderer geometry, including on cache hits."""
    geometry = _page_render_metadata(root, page).get("geometry", {})
    return geometry if isinstance(geometry, dict) else {}


def _broken_visible_images(geometry: dict) -> list[dict]:
    """Return visible ``img`` elements whose decoded pixels are unavailable."""
    broken: list[dict] = []
    for item in geometry.get("images", []):
        if not isinstance(item, dict):
            continue
        src = str(item.get("src") or "").strip()
        try:
            width = float(item.get("width") or 0)
            height = float(item.get("height") or 0)
            raw_opacity = item.get("opacity")
            opacity = float(raw_opacity if raw_opacity is not None else 1)
            natural_width = float(item.get("natural_width") or 0)
            natural_height = float(item.get("natural_height") or 0)
        except (TypeError, ValueError):
            continue
        if not src or width <= 1 or height <= 1 or opacity <= 0.01:
            continue
        if natural_width > 0 and natural_height > 0:
            continue
        broken.append({
            "src": src[:500],
            "alt": str(item.get("alt") or "")[:500],
            "display_width": round(width, 1),
            "display_height": round(height, 1),
            "natural_width": round(natural_width, 1),
            "natural_height": round(natural_height, 1),
        })
    return broken


def _discard_unpublished_render_digest(
    digest: str,
    hashes: list[str],
    attempt_hashes: dict[str, list[str]],
) -> None:
    """Do not spend a page refinement state on a mechanical render failure."""
    hashes[:] = [value for value in hashes if value != digest]
    for key, values in list(attempt_hashes.items()):
        attempt_hashes[key] = [value for value in values if value != digest]


def _used_render_states(
    hashes: list[str],
    attempt_hashes: dict[str, list[str]],
    attempt_id: str,
) -> int:
    if not _is_authoring_attempt(attempt_id):
        return len(dict.fromkeys(hashes))
    return len(dict.fromkeys(
        value
        for attempt, values in attempt_hashes.items()
        if _is_authoring_attempt(attempt)
        for value in values
    ))


def _print_broken_image_recovery(
    page: int,
    broken_images: list[dict],
    used_states: int,
    attempt_limit: int,
    attempt_id: str,
) -> None:
    print("status:REPAIR_REQUIRED")
    print("render-integrity:broken-visible-image")
    print("broken-images:" + json.dumps(broken_images, ensure_ascii=False))
    print(
        f"render-state:{used_states}/{attempt_limit} "
        f"scope={'page-lifecycle' if _is_authoring_attempt(attempt_id) else 'all-states'} "
        f"attempt={attempt_id}"
    )
    print(
        f"next: P{page:02d} contains a visible image that did not decode. Keep the "
        "same Owner/session, correct the src or restore the local asset, then run "
        "the same page render again. This diagnostic pixel is not a valid "
        "inspection target and the failed render did not consume a refinement state."
    )


def _page_typography_warnings(root: Path, page: int, geometry: dict) -> list[str]:
    text_boxes = [item for item in geometry.get("text_boxes", []) if isinstance(item, dict)]
    return [
        *_rendered_typography_warnings(page, text_boxes),
        *_unsupported_rendered_font_weights(page, text_boxes, _bundled_weight_support(root)),
    ]


def _page_render_quality_status(
    layout_defects: list[dict],
    typography_warnings: list[str],
    composition_advisories: list[dict] | None = None,
) -> str:
    return "advisory" if layout_defects or typography_warnings or composition_advisories else "clear"


def _write_page_render_quality_state(
    root: Path,
    page: int,
    quality_status: str,
    typography_warnings: list[str],
) -> None:
    """Attach current quality evidence to the existing atomic render receipt."""
    path = root / "_trace" / "slide-render-states" / f"page_{page:02d}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return
    payload["quality_status"] = str(quality_status or "clear")
    payload["typography_warnings"] = [
        str(item)[:2000] for item in typography_warnings[:64]
    ]
    atomic_write_text(
        root,
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def _print_page_render_feedback(
    layout_defects: list[dict],
    typography_warnings: list[str],
    composition_advisories: list[dict] | None = None,
) -> str:
    quality_status = _page_render_quality_status(
        layout_defects,
        typography_warnings,
        composition_advisories,
    )
    print("render-ok:true")
    print(f"quality-status:{quality_status}")
    print(
        "layout-defects:"
        + (json.dumps(layout_defects, ensure_ascii=False) if layout_defects else "none")
    )
    print(
        "typography-warnings:"
        + (
            json.dumps(typography_warnings, ensure_ascii=False)
            if typography_warnings
            else "none"
        )
    )
    advisories = list(composition_advisories or [])
    print(
        "composition-advisories:"
        + (json.dumps(advisories, ensure_ascii=False) if advisories else "none")
    )
    if typography_warnings or advisories or layout_defects:
        print("observations: these are measured/source facts, not repair commands; "
              "judge readability, intended layering and whitespace from the current PNG and page goal.")
    return quality_status


def render(root: Path, page: int, expected: int | None) -> bool:
    with page_render_lock(root, page):
        return _render_locked(root, page, expected)


def _render_locked(root: Path, page: int, expected: int | None) -> bool:
    renderer = Path(__file__).with_name("render_deck.py")
    deck, plans = _load_plans(root, expected, pages=(page,))
    plan_map = {plan["number"]: plan for plan in plans}
    if page not in plan_map:
        raise ValueError(f"page {page:02d} is not present in the canonical plans")
    fragment_path = root / "slides" / f"slide_{page:02d}.html"
    if not fragment_path.is_file():
        raise FileNotFoundError(f"{fragment_path.name} is missing")
    fragment = fragment_path.read_text(encoding="utf-8").strip()
    fragment = _normalize_fragment_resource_paths(fragment)
    _require_page_body(fragment, page)
    _validate_ground_contract(deck, [plan_map[page]], [(page, fragment)])
    _read_author_geometry(root)
    # 先报告结构错误；仅将实际可渲染的内容计入渲染状态。
    opening = re.search(r"<section\b[^>]*>", fragment, flags=re.I)
    if not opening or _attribute(opening.group(0), "data-slide") not in {
        str(page),
        f"{page:02d}",
    }:
        raise ValueError(
            f"slide_{page:02d}.html needs one root slide section with "
            f"data-slide=\"{page:02d}\""
        )
    # 首次单页渲染可能在 scaffold 之后才新增依赖，须在哈希及浏览器启动前准备。
    _ensure_authoring_runtime_dependencies(root, [plan_map[page]])
    _ensure_authoring_font_bundle(root, pages=(page,))
    # 字体准备可能规范化源码；预览和版本证据必须来自准备后的同一份字节。
    sources = {"base.css": (root / "base.css").read_bytes(),
               f"slides/slide_{page:02d}.html": fragment_path.read_bytes()}
    source_hashes = {path: hashlib.sha256(data).hexdigest() for path, data in sources.items()}
    css = sources["base.css"].decode("utf-8")
    fragment = _normalize_fragment_resource_paths(sources[f"slides/slide_{page:02d}.html"].decode("utf-8").strip())
    previous_metadata = _page_render_metadata(root, page)
    digest = _render_input_digest(root, css, fragment, previous_metadata.get("loaded_resources", []),
                                  presentation_language=deck["language"])
    (
        state_path,
        hashes,
        attempt_hashes,
        published_hash,
        layout_defects,
    ) = _page_render_state(root, page)
    attempt_id = re.sub(
        r"[^A-Za-z0-9_.-]+",
        "-",
        os.environ.get("MURAL_RENDER_ATTEMPT_ID", "unscoped"),
    ).strip("-") or "unscoped"
    current_attempt = attempt_hashes.setdefault(attempt_id, [])
    authoring_hashes = list(dict.fromkeys(
        value
        for attempt, values in attempt_hashes.items()
        if _is_authoring_attempt(attempt)
        for value in values
    ))
    attempt_limit = (
        AUTHORING_PAGE_RENDER_STATES
        if _is_authoring_attempt(attempt_id)
        else MAX_PAGE_RENDER_STATES
    )
    final_png = root / "renders" / f"slide_{page:02d}.png"
    is_new_state = digest not in hashes
    used_states = len(authoring_hashes) if _is_authoring_attempt(attempt_id) else len(hashes)
    render_target_exceeded = bool(is_new_state and used_states >= attempt_limit)
    if is_new_state:
        hashes.append(digest)
        current_attempt.append(digest)
        if _is_authoring_attempt(attempt_id):
            authoring_hashes.append(digest)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        _publish_render_state(
            root,
            page,
            state_path,
            hashes,
            attempt_hashes,
            published_hash,
            layout_defects,
        )
    if (digest == published_hash and final_png.is_file()
            and previous_metadata.get("presentation_language") == deck["language"]
            and all(previous_metadata.get("input_hashes", {}).get(key) == value for key, value in source_hashes.items())
            and previous_metadata.get("png_sha256") == hashlib.sha256(final_png.read_bytes()).hexdigest()):
        _validate_render_inputs(root, previous_metadata["input_hashes"])
        if read_config(root, require_complete=True)["presentation_language"] != deck["language"]:
            raise ValueError("presentation language changed during render; inspect the current version")
        cached_geometry = _isolated_render_geometry(root, page)
        broken_images = _broken_visible_images(cached_geometry)
        if broken_images:
            _discard_unpublished_render_digest(digest, hashes, attempt_hashes)
            _publish_render_state(
                root,
                page,
                state_path,
                hashes,
                attempt_hashes,
                "",
                layout_defects,
            )
            used_states = _used_render_states(hashes, attempt_hashes, attempt_id)
            _print_broken_image_recovery(
                page, broken_images, used_states, attempt_limit, attempt_id
            )
            return False
        typography_warnings = _page_typography_warnings(root, page, cached_geometry)
        composition_advisories = _page_composition_advisories(page, cached_geometry)
        print("status:RENDER_OK")
        print("render:cached unchanged HTML/CSS")
        print(
            f"render-state:{len(authoring_hashes) if _is_authoring_attempt(attempt_id) else len(hashes)}/{attempt_limit} "
            f"scope={'page-lifecycle' if _is_authoring_attempt(attempt_id) else 'all-states'} "
            f"attempt={attempt_id} total={len(hashes)}"
        )
        quality_status = _print_page_render_feedback(
            layout_defects, typography_warnings, composition_advisories
        )
        _write_page_render_quality_state(
            root, page, quality_status, typography_warnings
        )
        print(
            "next: inspect the current PNG; judge these observations against the page goal before editing"
        )
        print(final_png)
        return True
    fragments = [(page, fragment)]
    isolated = root / "renders" / f".page_{page:02d}"
    isolated.mkdir(parents=True, exist_ok=True)
    (isolated / "render.json").unlink(missing_ok=True)
    # BUG-J:单页预览 HTML 写在 _runtime/tmp/(距根两级),旧 `<base href="../">`
    # 只上溯一级→资源(assets/x)被解析到 _runtime/assets/ → chromium ERR_FILE_NOT_FOUND、
    # 图片 natural 0×0、渲染无 PNG。改用 workspace 根的绝对 file:// URI 作 base,使
    # `assets/x` 与 `../assets/x` 都稳定解析到根 assets/,不受预览文件所在层级影响。
    root_base = root.resolve().as_uri().rstrip("/") + "/"
    preview = _document(css, fragments, deck["language"]).replace(
        "<head>", f'<head>\n  <base href="{root_base}">', 1
    )
    broken_images: list[dict] = []
    try:
        with temporary_text(
            root,
            prefix=f"preview_slide_{page:02d}.",
            suffix=".html",
            content=preview,
        ) as preview_path:
            _run_renderer(
                [
                    sys.executable,
                    str(renderer),
                    "--workspace", str(root),
                    str(preview_path),
                    str(isolated),
                    "--page",
                    "1",
                ]
            )
            metadata = json.loads((isolated / "render.json").read_text(encoding="utf-8"))
            rendered_pages = metadata.get("pages", [])
            if len(rendered_pages) != 1 or rendered_pages[0].get("page") != 1:
                raise ValueError("render source evidence requires one captured page")
            evidence = rendered_pages[0]
            evidence["presentation_language"] = deck["language"]
            # 同一文件若也通过动态请求读取，两个版本必须相同，不能合并时覆盖旧证据。
            for key, value in source_hashes.items():
                if key in evidence["input_hashes"] and evidence["input_hashes"][key] != value:
                    raise ValueError(f"render source changed during capture: {key}")
            evidence["input_hashes"].update(source_hashes)
            _validate_render_inputs(root, evidence["input_hashes"])
            if evidence["png_sha256"] != hashlib.sha256((isolated / "slide_01.png").read_bytes()).hexdigest():
                raise ValueError("render pixels changed after capture")
            rendered_geometry = evidence.get("geometry", {})
            failed_resources = evidence.get("failed_resources", [])
            if failed_resources:
                raise ValueError(f"render resources failed or still pending: {failed_resources}")
            broken_images = _broken_visible_images(rendered_geometry)
            if not broken_images:
                # 新发现的动态依赖参与缓存键；只在版本已核实后计算、发布。
                if read_config(root, require_complete=True)["presentation_language"] != deck["language"]:
                    raise ValueError("presentation language changed during render; inspect the current version")
                observed_digest = _render_input_digest(root, css, fragment, evidence["loaded_resources"],
                                                       presentation_language=deck["language"])
                atomic_copy(root, isolated / "slide_01.png", final_png)
                atomic_write_bytes(root, _page_render_metadata_path(root, page),
                                   (json.dumps(metadata, ensure_ascii=False) + "\n").encode("utf-8"))
    except BaseException:
        if is_new_state:
            _discard_unpublished_render_digest(digest, hashes, attempt_hashes)
        _publish_render_state(root, page, state_path, hashes, attempt_hashes, published_hash, layout_defects)
        raise
    finally:
        # The published PNG and trace metadata are the canonical outputs.  The
        # isolated renderer directory is only an atomic staging area; retaining
        # it doubled page-image storage and leaked hidden .page_NN directories.
        shutil.rmtree(isolated, ignore_errors=True)
    if broken_images:
        _discard_unpublished_render_digest(digest, hashes, attempt_hashes)
        _publish_render_state(
            root,
            page,
            state_path,
            hashes,
            attempt_hashes,
            published_hash,
            layout_defects,
        )
        used_states = _used_render_states(hashes, attempt_hashes, attempt_id)
        _print_broken_image_recovery(
            page, broken_images, used_states, attempt_limit, attempt_id
        )
        return False
    if observed_digest != digest:
        if is_new_state:
            _discard_unpublished_render_digest(digest, hashes, attempt_hashes)
        digest = observed_digest
        if digest not in hashes:
            hashes.append(digest)
            attempt_hashes.setdefault(attempt_id, []).append(digest)
        authoring_hashes = list(dict.fromkeys(
            value for attempt, values in attempt_hashes.items()
            if _is_authoring_attempt(attempt) for value in values
        ))
    layout_defects = _page_layout_defects(page, rendered_geometry)
    typography_warnings = _page_typography_warnings(
        root, page, rendered_geometry
    )
    composition_advisories = _page_composition_advisories(page, rendered_geometry)
    _publish_render_state(
        root,
        page,
        state_path,
        hashes,
        attempt_hashes,
        digest,
        layout_defects,
    )
    print("status:RENDER_OK")
    print(
        f"render-state:{len(authoring_hashes) if _is_authoring_attempt(attempt_id) else len(hashes)}/{attempt_limit} "
        f"scope={'page-lifecycle' if _is_authoring_attempt(attempt_id) else 'all-states'} "
        f"attempt={attempt_id} total={len(hashes)}"
    )
    if render_target_exceeded:
        print(
            f"render-soft-target-exceeded: the configured {attempt_limit}-state "
            "target is a soft repair discipline, not a failure gate. Prefer the best "
            "current verified version and record residual issues honestly; only "
            "continue when one consolidated repair is likely to fix a concrete defect."
        )
    quality_status = _print_page_render_feedback(
        layout_defects, typography_warnings, composition_advisories
    )
    _write_page_render_quality_state(
        root, page, quality_status, typography_warnings
    )
    if layout_defects:
        hints = list(dict.fromkeys(
            str(item.get("ancestor_hint") or item.get("element") or "")
            for item in layout_defects
            if str(item.get("ancestor_hint") or item.get("element") or "").strip()
        ))
        print(
            "layout-locators: measured elements and ancestors"
            + (" (" + "; ".join(hints[:3]) + ")" if hints else "")
            + "; if the current PNG confirms a problem, inspect the matched selectors and containing boxes before editing."
        )
    print(
        "next: inspect the current PNG; judge these observations against the page goal before editing"
    )
    print(final_png)
    return True


def render_group(
    root: Path,
    group: str,
    pages_text: str,
    expected: int | None,
) -> None:
    group_id = str(group or "").strip().lower().replace("_", "-")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", group_id):
        raise ValueError(f"invalid group id: {group!r}")
    pages: list[int] = []
    for item in str(pages_text or "").split(","):
        token = item.strip()
        if not token.isdigit() or int(token) < 1:
            raise ValueError(f"invalid group page: {token!r}")
        pages.append(int(token))
    if not pages or len(set(pages)) != len(pages):
        raise ValueError("group pages must be non-empty and unique")
    target = root / "renders" / f"contact-sheet-group-{group_id}.png"
    published = {page: render(root, page, expected) for page in pages}
    unpublished = [page for page, ready in published.items() if not ready]
    if unpublished:
        target.unlink(missing_ok=True)
        print("status:REPAIR_REQUIRED")
        print(f"group:{group_id}")
        print("unpublished-pages:" + ",".join(f"{page:02d}" for page in unpublished))
        print(
            "next: keep the same Group Owner/session, repair the reported page-local "
            "render-integrity issue, then rerun this group render. No group contact "
            "sheet was published from mixed valid/invalid pixels."
        )
        return
    paths = [root / "renders" / f"slide_{page:02d}.png" for page in pages]
    missing = [str(path.relative_to(root)) for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"group render is missing pages: {missing}")
    _contact_sheet(paths, target, labels=pages)
    print("status:PASS")
    print(f"group:{group_id}")
    print("pages:" + ",".join(f"{page:02d}" for page in pages))
    print(target)




def _font_weight_number(value: object) -> int:
    token = str(value or "").strip().lower()
    if token == "normal":
        return 400
    if token == "bold":
        return 700
    try:
        return int(float(token))
    except ValueError:
        return 400


def _bundled_weight_support(root: Path) -> dict[str, list[tuple[int, int]]]:
    """Map delivery font-family names to the real bundled weight ranges."""
    try:
        manifest = json.loads(
            (root / "_runtime/tmp/fonts/manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return {}
    support: dict[str, list[tuple[int, int]]] = {}
    for face in manifest.get("faces", []) if isinstance(manifest, dict) else []:
        if not isinstance(face, dict):
            continue
        family = str(face.get("delivery_family") or "").strip().lower()
        values = re.findall(r"\d+", str(face.get("weight") or ""))
        if not family or not values:
            continue
        low = int(values[0])
        high = int(values[-1]) if len(values) > 1 else low
        support.setdefault(family, []).append((low, high))
    return support


def _unsupported_rendered_font_weights(
    number: int,
    text_boxes: list[dict],
    support: dict[str, list[tuple[int, int]]],
) -> list[str]:
    unsupported: dict[tuple[str, int], list[str]] = {}
    for item in text_boxes:
        requested = _font_weight_number(item.get("font_weight"))
        families = [
            token.strip().strip("'\"").lower()
            for token in str(item.get("font_family") or "").split(",")
        ]
        delivery = next((family for family in families if family in support), "")
        if not delivery:
            continue
        if any(low <= requested <= high for low, high in support[delivery]):
            continue
        key = (delivery, requested)
        unsupported.setdefault(key, []).append(str(item.get("text") or "")[:36])
    return [
        f"page {number}: bundled family {family!r} has no real {weight} weight; "
        "use a supported face/weight instead of browser-synthesized bold (examples: "
        + ", ".join(repr(text) for text in examples[:3])
        + ")"
        for (family, weight), examples in sorted(unsupported.items())
    ]


def _rendered_typography_warnings(number: int, text_boxes: list[dict]) -> list[str]:
    """只报告可见文字的实际字号，不从标签/类名推断正文、装饰或修复责任。"""
    samples = [item for item in text_boxes if str(item.get("text") or "").strip()
               and 0 < float(item.get("font_size") or 0) < 24]
    if not samples:
        return []
    return [f"page {number}: {len(samples)} sampled text leaves below 24px; "
            + ", ".join(f"{float(item['font_size']):.1f}px {str(item['text'])[:48]!r}"
                        for item in samples[:6])
            + ". Assess readability in the current PNG and viewing context."]


def audit(root: Path) -> None:
    _read_author_geometry(root)
    violations = audit_workspace(root)
    css = (root / "base.css").read_text(encoding="utf-8")
    if AUTHORING_START in css:
        # 只审实际引用；旧 subset 留存不再表示当前作者字体。
        for uri in re.findall(r'url\("(file:[^\"]+)"\)', css):
            try:
                resolve_shared_font_reference(root, uri)
            except (OSError, ValueError) as exc:
                violations.append(str(exc))
    elif (root / "_runtime/tmp/fonts/manifest.json").is_file():
        violations.extend(validate_font_bundle(root))
    if violations:
        raise ValueError("workspace output audit failed:\n- " + "\n- ".join(violations))
    print("status:PASS")
    print("workspace-output-audit: clean")


def clean(root: Path, *, unused_assets: bool = False) -> None:
    moved = clean_workspace(root, unused_assets=unused_assets)
    print("status:PASS")
    print("quarantined:", ", ".join(moved) if moved else "none")


_MATERIAL_CROP_POLICIES = {
    "subject-only",
    "preserve-complete-figure",
    "document-facsimile",
}


def _normalized_layout_box(row: dict, layout: dict) -> tuple[float, float, float, float] | None:
    page_width, page_height = layout.get("page_points", [0, 0])
    bbox = row.get("bbox_pdf", [])
    if len(bbox) == 4 and page_width and page_height:
        return (
            float(bbox[0]) / float(page_width),
            float(bbox[1]) / float(page_height),
            float(bbox[2]) / float(page_width),
            float(bbox[3]) / float(page_height),
        )
    image_width, image_height = layout.get("image_pixels", [0, 0])
    polygon = row.get("polygon", [])
    if polygon and image_width and image_height:
        xs = [float(point[0]) / float(image_width) for point in polygon]
        ys = [float(point[1]) / float(image_height) for point in polygon]
        return min(xs), min(ys), max(xs), max(ys)
    return None


def _box_intersection(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    x0, y0 = max(left[0], right[0]), max(left[1], right[1])
    x1, y1 = min(left[2], right[2]), min(left[3], right[3])
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def _box_area(box: tuple[float, float, float, float]) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _subject_crop_analysis(
    layout: dict,
    requested: tuple[float, float, float, float],
    *,
    crop_policy: str,
    preserve_internal_labels: bool,
) -> dict:
    """Suggest one conservative visual-subject box from PDF layout evidence."""
    text_rows: list[tuple[dict, tuple[float, float, float, float], str]] = []
    for key in ("text_blocks", "ocr_blocks"):
        for row in layout.get(key, []):
            box = _normalized_layout_box(row, layout)
            if box and _box_intersection(box, requested) > 0:
                text_rows.append((row, box, key))

    visual_rows: list[tuple[dict, tuple[float, float, float, float]]] = []
    clipped_visual_regions: list[list[float]] = []
    unsegmented_raster = bool(layout.get("raster_page"))
    for key in ("image_blocks", "drawing_blocks"):
        # 扫描 PDF 的整页条带是存储分块，不是物体边界，不能据此自动收框。
        if key == "image_blocks" and layout.get("raster_page"):
            continue
        for row in layout.get(key, []):
            box = _normalized_layout_box(row, layout)
            if not box:
                continue
            overlap = _box_intersection(box, requested)
            if key == "image_blocks" and _box_area(box) > 0.9:
                unsegmented_raster = True
            if 0.001 <= _box_area(box) <= 0.9 and 0 < overlap < _box_area(box) * 0.98:
                clipped_visual_regions.append(list(box))
            if overlap <= 0 or overlap / max(_box_area(box), 1e-9) < 0.5:
                continue
            if _box_area(box) < 0.001 or _box_area(box) > 0.9:
                continue
            visual_rows.append((row, box))

    suggested = requested
    if crop_policy != "document-facsimile" and visual_rows and not clipped_visual_regions:
        if crop_policy == "subject-only":
            selected = [max(visual_rows, key=lambda item: _box_area(item[1]))]
        else:
            selected = visual_rows
        boxes = [box for _, box in selected]
        vx0 = min(box[0] for box in boxes)
        vy0 = min(box[1] for box in boxes)
        vx1 = max(box[2] for box in boxes)
        vy1 = max(box[3] for box in boxes)
        visual_union = (vx0, vy0, vx1, vy1)
        if crop_policy == "preserve-complete-figure" or preserve_internal_labels:
            nearby = (
                max(requested[0], vx0 - 0.025),
                max(requested[1], vy0 - 0.025),
                min(requested[2], vx1 + 0.025),
                min(requested[3], vy1 + 0.025),
            )
            for row, box, source_kind in text_rows:
                text = str(row.get("text") or "").strip()
                center = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
                if (
                    text
                    and len(text) <= 120
                    and not (
                        source_kind == "ocr_blocks"
                        and (
                            bool(row.get("uncertain"))
                            or row.get("confidence") is None
                        )
                    )
                    and nearby[0] <= center[0] <= nearby[2]
                    and nearby[1] <= center[1] <= nearby[3]
                ):
                    vx0, vy0 = min(vx0, box[0]), min(vy0, box[1])
                    vx1, vy1 = max(vx1, box[2]), max(vy1, box[3])
        margin = 0.012
        candidate = (
            max(requested[0], vx0 - margin),
            max(requested[1], vy0 - margin),
            min(requested[2], vx1 + margin),
            min(requested[3], vy1 + margin),
        )
        if _box_area(candidate) >= 0.02 and _box_area(candidate) < _box_area(requested) * 0.98:
            suggested = candidate

    edge_fragments: list[str] = []
    excluded_regions: list[dict[str, object]] = []
    uncertain_ocr_regions: list[dict[str, object]] = []
    edge_tolerance = 0.012
    for row, box, source_kind in text_rows:
        text = re.sub(r"\s+", " ", str(row.get("text") or "")).strip()
        if not text:
            continue
        # 诊断实际请求框，而非尚未应用的建议框；否则未 tighten 的残字会被报成已排除。
        intersection = _box_intersection(box, requested)
        if (
            source_kind == "ocr_blocks"
            and (
                bool(row.get("uncertain"))
                or row.get("confidence") is None
            )
            and intersection > 0
        ):
            uncertain_ocr_regions.append({
                "text": text[:96],
                "confidence": row.get("confidence"),
                "polygon": row.get("polygon", []),
            })
        block_area = max(_box_area(box), 1e-9)
        partial = 0 < intersection < block_area * 0.9
        near_edge = (
            abs(box[0] - requested[0]) <= edge_tolerance
            or abs(box[1] - requested[1]) <= edge_tolerance
            or abs(box[2] - requested[2]) <= edge_tolerance
            or abs(box[3] - requested[3]) <= edge_tolerance
        )
        if partial or near_edge:
            edge_fragments.append(text[:96])
        if _box_intersection(box, suggested) <= 0:
            center_y = (box[1] + box[3]) / 2
            if center_y < 0.12:
                kind = "header"
            elif center_y > 0.88:
                kind = "footer"
            elif re.match(r"(?i)^(?:fig(?:ure)?|table)\b", text) or len(text) > 160:
                kind = "caption_or_body_text"
            else:
                kind = "surrounding_text"
            excluded_regions.append({
                "kind": kind,
                "text": text[:96],
                "ocr_confidence": (
                    row.get("confidence") if source_kind == "ocr_blocks" else None
                ),
            })
    return {
        "layout_evidence": {
            "text": ("ocr" if layout.get("ocr_status") == "completed" else
                     "native" if layout.get("text_blocks") else "unavailable"),
            "visual": ("regions" if visual_rows else "unsegmented_raster" if unsegmented_raster else "unavailable"),
            "ocr_status": layout.get("ocr_status", "unavailable"),
        },
        "suggested_subject_box": [round(value, 6) for value in suggested],
        "clipped_visual_regions": clipped_visual_regions[:12],
        "edge_text_fragments": list(dict.fromkeys(edge_fragments))[:8],
        "excluded_regions": excluded_regions[:12],
        "uncertain_ocr_regions": uncertain_ocr_regions[:12],
        "visual_region_count": len(visual_rows),
    }


def _alpha_cutout_subject(image):
    """Extract one isolated foreground while preserving the source RGB pixels."""
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise ValueError(
            "cutout-alpha requires the OpenCV runtime; use subject-only crop when "
            "the segmentation backend is unavailable"
        ) from exc
    from PIL import Image

    def edge_ratio(values):
        return round(float(np.concatenate((values[0], values[-1], values[:, 0], values[:, -1])).mean()), 4)

    rgba = image.convert("RGBA")
    source_alpha = np.asarray(rgba.getchannel("A"))
    if source_alpha.min() == 0:
        if not source_alpha.any():
            raise ValueError("cutout-alpha source has no visible foreground")
        return rgba, {
            "alpha_source": "preserved",
            "foreground_ratio": round(float((source_alpha > 0).mean()), 4),
            "edge_contact_ratio": edge_ratio(source_alpha > 0),
            "edge_contact_basis": "source_alpha",
        }

    rgb = np.asarray(image.convert("RGB"))
    height, width = rgb.shape[:2]
    if min(width, height) < 180:
        raise ValueError("cutout-alpha source is too small for reliable foreground extraction")

    border = max(4, int(round(min(width, height) * 0.025)))
    border_pixels = np.concatenate(
        (
            rgb[:border].reshape(-1, 3),
            rgb[-border:].reshape(-1, 3),
            rgb[:, :border].reshape(-1, 3),
            rgb[:, -border:].reshape(-1, 3),
        ),
        axis=0,
    ).astype(np.float32)
    background = np.median(border_pixels, axis=0)
    distance = np.linalg.norm(rgb.astype(np.float32) - background, axis=2)
    border_distance = np.linalg.norm(border_pixels - background, axis=1)
    background_tolerance = float(
        min(46.0, max(12.0, np.percentile(border_distance, 80) + 8.0))
    )

    # 边缘不是天然背景；只有颜色证据支持的边缘像素才能成为确定背景种子。
    border_mask = np.ones((height, width), dtype=bool)
    border_mask[border:height - border, border:width - border] = False
    background_seed = border_mask & (distance <= background_tolerance)
    background_support = float(background_seed.sum() / border_mask.sum())
    if background_support < 0.6:
        raise ValueError(
            "cutout-alpha cannot establish a reliable background from the source edges; "
            "include more clear background in --box or keep a framed crop"
        )
    mask = np.full((height, width), cv2.GC_PR_FGD, dtype=np.uint8)
    mask[distance <= background_tolerance] = cv2.GC_PR_BGD
    mask[background_seed] = cv2.GC_BGD
    foreground_seed = distance >= max(22.0, background_tolerance * 1.55)
    if int(foreground_seed.sum()) < max(64, int(width * height * 0.002)):
        raise ValueError(
            "cutout-alpha could not distinguish the subject from its background; "
            "tighten --box or keep the source as a framed artwork/figure"
        )
    mask[foreground_seed] = cv2.GC_FGD

    bg_model = np.zeros((1, 65), np.float64)
    fg_model = np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(
            cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR),
            mask,
            None,
            bg_model,
            fg_model,
            5,
            cv2.GC_INIT_WITH_MASK,
        )
    except cv2.error as exc:
        raise ValueError(
            "cutout-alpha segmentation failed; tighten --box or use subject-only crop"
        ) from exc

    foreground = np.isin(mask, (cv2.GC_FGD, cv2.GC_PR_FGD)).astype(np.uint8)
    kernel = np.ones((3, 3), np.uint8)
    foreground = cv2.morphologyEx(foreground, cv2.MORPH_CLOSE, kernel, iterations=2)
    foreground = cv2.morphologyEx(foreground, cv2.MORPH_OPEN, kernel, iterations=1)

    component_count, labels, stats, _ = cv2.connectedComponentsWithStats(
        foreground, connectivity=8
    )
    if component_count <= 1:
        raise ValueError(
            "cutout-alpha found no stable foreground; tighten --box or use subject-only crop"
        )
    areas = stats[1:, cv2.CC_STAT_AREA]
    largest = int(areas.max())
    keep = np.zeros_like(foreground)
    min_component = max(int(width * height * 0.0015), int(largest * 0.025))
    for component, area in enumerate(areas, start=1):
        if int(area) >= min_component:
            keep[labels == component] = 1

    foreground_ratio = float(keep.mean())
    if foreground_ratio < 0.04 or foreground_ratio > 0.90:
        raise ValueError(
            f"cutout-alpha produced an implausible foreground ratio "
            f"({foreground_ratio:.1%}); tighten --box or keep a framed crop"
        )
    alpha = cv2.GaussianBlur(keep * 255, (0, 0), sigmaX=0.8, sigmaY=0.8)
    alpha = np.minimum(alpha, source_alpha)
    rgba = np.dstack((rgb, alpha.astype(np.uint8)))
    return Image.fromarray(rgba, mode="RGBA"), {
        "foreground_ratio": round(foreground_ratio, 4),
        # 输入边缘风险不因分割/删边而消失；它不是主体完整性的视觉验收结论。
        "edge_contact_ratio": edge_ratio(distance > background_tolerance),
        "edge_contact_basis": "source_background_difference",
        "background_rgb": [int(round(value)) for value in background],
        "background_tolerance": round(background_tolerance, 2),
        "background_border_fraction": round(background_support, 4),
    }


def material_figure(
    root: Path,
    source_value: str,
    output_value: str,
    box_value: str,
    crop_policy: str = "preserve-complete-figure",
    preserve_internal_labels: bool = False,
    auto_tighten: bool = False,
    alpha_cutout: bool = False,
    exclude_boxes: Sequence[str] = (),
) -> None:
    """Crop a workspace attachment or asset with explicit normalized coordinates."""
    from PIL import Image, ImageOps

    root = root.resolve()
    source = (root / source_value).resolve()
    output = _asset_path(root, output_value)
    if (not any(root / name in source.parents for name in ("inputs", "assets"))
            or Path(source_value).is_absolute() or ".." in Path(source_value).parts):
        raise ValueError("crop source must resolve to an image under inputs/ or assets/")
    if source == output:
        raise ValueError("crop output must differ from source; preserve the original image")
    if output.suffix.lower() != ".png":
        raise ValueError("material figure output must be assets/NAME.png")
    source_value, output_value = source.relative_to(root).as_posix(), output.relative_to(root).as_posix()
    if not source.is_file() or source.stat().st_size <= 0:
        raise ValueError(f"material page is missing: {source_value}")
    if source.stat().st_size > MAX_ASSET_DOWNLOAD_BYTES:
        raise ValueError("crop source exceeds the asset size limit")
    try:
        x0, y0, x1, y1 = [float(value.strip()) for value in box_value.split(",")]
    except (TypeError, ValueError):
        raise ValueError("--box must be normalized x0,y0,x1,y1") from None
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        raise ValueError("--box coordinates must satisfy 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1")
    policy = str(crop_policy or "preserve-complete-figure").strip().lower()
    if policy not in _MATERIAL_CROP_POLICIES:
        raise ValueError(
            f"--crop-policy must be one of {sorted(_MATERIAL_CROP_POLICIES)}"
        )
    if alpha_cutout and policy != "subject-only":
        raise ValueError("cutout-alpha is only valid with crop_policy=subject-only")
    if exclude_boxes and not alpha_cutout:
        raise ValueError("--exclude-box only changes Alpha in extract-subject")
    exclusions = []
    for value in exclude_boxes:
        try:
            ex0, ey0, ex1, ey1 = [float(part.strip()) for part in value.split(",")]
        except (TypeError, ValueError):
            raise ValueError("--exclude-box must be normalized x0,y0,x1,y1") from None
        if not (x0 <= ex0 < ex1 <= x1 and y0 <= ey0 < ey1 <= y1):
            raise ValueError("--exclude-box must lie inside the requested --box")
        exclusions.append([ex0, ey0, ex1, ey1])
    requested_box = (x0, y0, x1, y1)
    layout_path = source.with_suffix(".json")
    try:
        layout = json.loads(layout_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        layout = {}
    crop_analysis = _subject_crop_analysis(
        layout,
        requested_box,
        crop_policy=policy,
        preserve_internal_labels=preserve_internal_labels,
    )
    suggested_box = tuple(crop_analysis["suggested_subject_box"])
    if auto_tighten and suggested_box != requested_box:
        x0, y0, x1, y1 = suggested_box
    applied_box = (x0, y0, x1, y1)
    if applied_box != requested_box:
        applied_analysis = _subject_crop_analysis(layout, applied_box,
            crop_policy=policy, preserve_internal_labels=preserve_internal_labels)
        for key in ("edge_text_fragments", "uncertain_ocr_regions", "clipped_visual_regions"):
            crop_analysis[key] = applied_analysis[key]
    else:
        crop_analysis["excluded_regions"] = []
    area_ratio = _box_area(applied_box)

    with Image.open(source) as opened:
        opened = ImageOps.exif_transpose(opened)
        has_alpha = "A" in opened.getbands() or "transparency" in opened.info
        image = opened.convert("RGBA" if has_alpha else "RGB")
    width, height = image.size
    pixels = (
        int(round(x0 * width)),
        int(round(y0 * height)),
        int(round(x1 * width)),
        int(round(y1 * height)),
    )
    # 归一化框取整后必须包含真实像素；正尺寸裁图的用途和可用性由作者看图判断。
    if pixels[2] <= pixels[0] or pixels[3] <= pixels[1]:
        raise ValueError(
            f"--box maps to an empty pixel region {pixels} on a {width}x{height} image"
        )
    crop = image.crop(pixels)

    text_chars = 0
    text_area = 0.0
    if layout:
        page_width, page_height = layout.get("page_points", [0, 0])
        if page_width and page_height:
            for row in layout.get("text_blocks", []):
                bbox = row.get("bbox_pdf", [])
                if len(bbox) != 4:
                    continue
                bx0, by0, bx1, by1 = (
                    float(bbox[0]) / float(page_width),
                    float(bbox[1]) / float(page_height),
                    float(bbox[2]) / float(page_width),
                    float(bbox[3]) / float(page_height),
                )
                ix0, iy0 = max(x0, bx0), max(y0, by0)
                ix1, iy1 = min(x1, bx1), min(y1, by1)
                if ix1 <= ix0 or iy1 <= iy0:
                    continue
                overlap = (ix1 - ix0) * (iy1 - iy0)
                block_area = max(1e-9, (bx1 - bx0) * (by1 - by0))
                if overlap / block_area >= 0.35:
                    text_chars += int(row.get("chars", 0) or 0)
                    text_area += overlap
        image_width, image_height = layout.get("image_pixels", [0, 0])
        if image_width and image_height:
            for row in layout.get("ocr_blocks", []):
                polygon = row.get("polygon", [])
                if not polygon:
                    continue
                xs = [float(point[0]) / float(image_width) for point in polygon]
                ys = [float(point[1]) / float(image_height) for point in polygon]
                bx0, by0, bx1, by1 = min(xs), min(ys), max(xs), max(ys)
                ix0, iy0 = max(x0, bx0), max(y0, by0)
                ix1, iy1 = min(x1, bx1), min(y1, by1)
                if ix1 <= ix0 or iy1 <= iy0:
                    continue
                overlap = (ix1 - ix0) * (iy1 - iy0)
                block_area = max(1e-9, (bx1 - bx0) * (by1 - by0))
                if overlap / block_area >= 0.35:
                    text_chars += int(row.get("chars", 0) or 0)
                    text_area += overlap
    text_coverage = text_area / max(area_ratio, 1e-9)

    cutout_report: dict[str, object] = {}
    rendered_crop = crop
    if alpha_cutout:
        rendered_crop, cutout_report = _alpha_cutout_subject(crop)
        alpha = rendered_crop.getchannel("A")
        for ex0, ey0, ex1, ey1 in exclusions:
            box = (max(0, round(ex0 * width) - pixels[0]), max(0, round(ey0 * height) - pixels[1]),
                   min(crop.width, round(ex1 * width) - pixels[0]), min(crop.height, round(ey1 * height) - pixels[1]))
            if box[0] < box[2] and box[1] < box[3]:
                alpha.paste(0, box)
        rendered_crop.putalpha(alpha)
        cutout_report["foreground_ratio"] = round(sum(alpha.histogram()[1:]) / (crop.width * crop.height), 4)

    output.parent.mkdir(parents=True, exist_ok=True)
    buffer = BytesIO()
    rendered_crop.save(buffer, format="PNG", optimize=True)
    payload = buffer.getvalue()
    atomic_write_bytes(root, output, payload)
    registry_path = root / "_trace" / "material-figures.json"
    entry = {
        "source": source_value,
        "source_document": layout.get("source_path", ""),
        "source_page": layout.get("page"),
        "requested_box": list(requested_box),
        "box": list(applied_box),
        "suggested_subject_box": crop_analysis["suggested_subject_box"],
        "crop_policy": policy,
        "preserve_internal_labels": bool(preserve_internal_labels),
        "auto_tightened": bool(auto_tighten and suggested_box != requested_box),
        "extraction_mode": "cutout-alpha" if alpha_cutout else "rectangular-crop",
        "has_alpha": "A" in rendered_crop.getbands(),
        "cutout": cutout_report,
        "alpha_exclusions": exclusions,
        "excluded_regions": crop_analysis["excluded_regions"],
        "layout_evidence": crop_analysis["layout_evidence"],
        "clipped_visual_regions": crop_analysis["clipped_visual_regions"],
        "quality_status": "unreviewed",
        "edge_text_fragments": crop_analysis["edge_text_fragments"],
        "uncertain_ocr_regions": crop_analysis["uncertain_ocr_regions"],
        "requires_full_resolution_review": True,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "pixels": [crop.width, crop.height],
        "page_area": round(area_ratio, 4),
        "native_text_chars": text_chars,
        "native_text_coverage": round(text_coverage, 4),
    }
    lock_path = root / "_state" / "state.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.02)
        try:
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            registry = {"schema": "mural.material-figures.v1", "assets": {}}
        registry["assets"][output_value] = entry
        atomic_write_text(root, registry_path, json.dumps(registry, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({
        "status": "PASS",
        "source": source_value,
        "output": output_value,
        "pixels": [crop.width, crop.height],
        "page_area": round(area_ratio, 4),
        "requested_box": list(requested_box),
        "applied_box": list(applied_box),
        "suggested_subject_box": crop_analysis["suggested_subject_box"],
        "crop_policy": policy,
        "auto_tightened": bool(auto_tighten and suggested_box != requested_box),
        "extraction_mode": "cutout-alpha" if alpha_cutout else "rectangular-crop",
        "has_alpha": "A" in rendered_crop.getbands(),
        "cutout": cutout_report,
        "alpha_exclusions": exclusions,
        "excluded_regions": crop_analysis["excluded_regions"],
        "layout_evidence": crop_analysis["layout_evidence"],
        "clipped_visual_regions": crop_analysis["clipped_visual_regions"],
        "quality_status": "unreviewed",
        "edge_text_fragments": crop_analysis["edge_text_fragments"],
        "uncertain_ocr_regions": crop_analysis["uncertain_ocr_regions"],
        "requires_full_resolution_review": True,
        "native_text_chars": text_chars,
        "native_text_coverage": round(text_coverage, 4),
    }, ensure_ascii=False))


def register_user_image(root: Path, source_value: str, output_value: str) -> None:
    """Normalize one user-supplied image into an audited local asset."""
    from PIL import Image, ImageOps

    root = root.resolve()
    source = (root / source_value).resolve()
    output = _asset_path(root, output_value)
    if root / "inputs" not in source.parents or Path(source_value).is_absolute() or ".." in Path(source_value).parts:
        raise ValueError("user image source must be under inputs/")
    if output.suffix.lower() != ".png":
        raise ValueError("user image output must be assets/NAME.png")
    source_value, output_value = source.relative_to(root).as_posix(), output.relative_to(root).as_posix()
    if not source.is_file() or source.stat().st_size <= 0:
        raise ValueError(f"user image is missing or empty: {source_value}")
    if source.stat().st_size > MAX_ASSET_DOWNLOAD_BYTES:
        raise ValueError("user image exceeds the delivery asset size limit")
    with Image.open(source) as opened:
        opened.load()
        opened = ImageOps.exif_transpose(opened)
        has_alpha = "A" in opened.getbands() or "transparency" in opened.info
        image = opened.convert("RGBA" if has_alpha else "RGB")
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    payload = buffer.getvalue()
    atomic_write_bytes(root, output, payload)
    registry_path = root / "_trace" / "user-images.json"
    entry = {
        "source": source_value,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "pixels": [image.width, image.height],
        "has_alpha": has_alpha,
    }
    lock_path = root / "_state" / "state.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.02)
        try:
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            registry = {"schema": "mural.user-images.v1", "assets": {}}
        registry["assets"][output_value] = entry
        atomic_write_text(root, registry_path, json.dumps(registry, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({
        "status": "PASS",
        "source": source_value,
        "output": output_value,
        "pixels": [image.width, image.height],
        "has_alpha": has_alpha,
    }, ensure_ascii=False))


def _difference_hash(path: Path) -> int:
    from PIL import Image

    with Image.open(path) as source:
        image = source.convert("L").resize((9, 8))
    pixels = list(image.get_flattened_data())
    value = 0
    for row in range(8):
        for column in range(8):
            left = pixels[row * 9 + column]
            right = pixels[row * 9 + column + 1]
            value = (value << 1) | int(left > right)
    return value


def _asset_image_geometry(path: Path, canvas: dict) -> dict:
    """只记录实际尺寸与铺满倍率，不据此宣称素材可用或清晰。"""
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = 200_000_000
    with Image.open(path) as source:
        width, height = ImageOps.exif_transpose(source).size
    full_bleed_scale = max(
        canvas["width_px"] / max(1, width), canvas["height_px"] / max(1, height)
    )
    return {
        "width": width,
        "height": height,
        "full_bleed_scale": round(full_bleed_scale, 2),
    }


def _image_sheet(root: Path, rows: list[tuple[str, str]], *, name: str = "contact-sheet") -> Path:
    from PIL import Image, ImageDraw, ImageFont

    columns = min(4, max(1, len(rows)))
    cell_w, image_h, label_h = 400, 225, 48
    lines = math.ceil(len(rows) / columns)
    sheet = Image.new(
        "RGB",
        (columns * cell_w, lines * (image_h + label_h)),
        (242, 242, 238),
    )
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for index, (asset_id, relative) in enumerate(rows):
        with Image.open(root / relative) as source:
            preview = source.convert("RGBA")
            preview.thumbnail((cell_w - 24, image_h - 24))
        column, row = index % columns, index // columns
        x, y = column * cell_w, row * (image_h + label_h)
        px = x + (cell_w - preview.width) // 2
        py = y + (image_h - preview.height) // 2
        sheet.paste(preview, (px, py), preview)
        draw.rectangle(
            (x, y, x + cell_w - 1, y + image_h + label_h - 1),
            outline=(170, 170, 166),
        )
        draw.text((x + 14, y + image_h + 13), asset_id, fill=(18, 18, 18), font=font)
    target = root / "_trace" / "image" / f"{name}.png"
    buffer = BytesIO()
    sheet.save(buffer, format="PNG")
    atomic_write_bytes(root, target, buffer.getvalue())
    # 公开图片不经软链接跨入私有 Trace 读域；同文件系统用硬链接复用原始字节。
    alias = root / "assets" / f"{name}.png"
    alias.parent.mkdir(parents=True, exist_ok=True)
    alias.unlink(missing_ok=True)
    try:
        os.link(target, alias)
    except OSError:
        # 不支持硬链接时，仍按原始字节建立同一公开图片。
        atomic_copy(root, target, alias)
    return alias


def _catalog_guidance(entry: dict) -> list[str]:
    return [f"- {key}: " + str(entry.get(key, "")).replace("\n", "\n  ")
            for key in ("crop", "subject_region", "text_safe_region", "notes") if entry.get(key)]


def _image_usage_role(entry: dict) -> str:
    text = "\n".join(str(entry.get(key) or "") for key in ("purpose", "crop", "display", "notes"))
    if re.search(
        r"(?:原件|原图).{0,16}(?:留存|保留).{0,80}(?:不(?:与[^，。；;]{0,20})?重复上屏|不(?:与[^，。；;]{0,20})?重复使用|版面优先|实际版面优先|优先用预裁|优先使用预裁)",
        text,
        re.IGNORECASE,
    ):
        return "reference_only"
    return ""


def write_image_catalog(root: Path, entries: list[dict]) -> None:
    """登记与修订共用只读投影，不解析旧 Markdown，也不重新获取或验收素材。"""
    failed = {"failed", "unavailable", "pending"}
    state = "empty" if not entries else "partial" if any(e.get("status") in failed for e in entries) else "available"
    lines = ["# Asset catalog", "", f"catalog_state: {state}", ""]
    for entry in entries:
        unavailable = entry.get("status") in failed
        usage_role = _image_usage_role(entry)
        lines.extend([
            f"## {entry['id']}",
            f"- slides: {', '.join(str(value) for value in entry['slides'])}",
            f"- kind: {entry['kind']}",
            f"- path: {entry['path']}",
            f"- owner_id: {entry.get('owner_id', '')}",
            f"- source: {entry['source'] or ('unavailable' if unavailable else 'generated/local')}",
            f"- purpose: {entry['purpose'] or ('planned deck visual' if unavailable else 'deck visual')}",
            *_catalog_guidance(entry),
            *([f"- usage_role: {usage_role}"] if usage_role else []),
            f"- display: {entry['display'] or 'large'}",
            f"- quality_intent: {entry['quality_intent'] or 'standard'}",
        ])
        if unavailable:
            lines.extend([
                "- status: " + ("pending" if entry["status"] == "pending" else "unavailable"),
                "- failure_reason: " + (entry.get("failure_reason") or
                    ("awaiting explicit acquisition" if entry["status"] == "pending" else "asset acquisition or quality check failed")),
            ])
        else:
            lines.extend([
                "- expect_transparent: " + ("true" if entry["expect_transparent"] else "false"),
            ])
        lines.append("")
    atomic_write_text(root, root / "assets/catalog.md", "\n".join(lines).rstrip() + "\n")


def finalize_assets(root: Path, *, owner_id: str = "") -> None:
    canvas = _read_author_geometry(root)
    try:
        _, plans = _load_plans(root)
    except (OSError, ValueError):
        plans = []
    parsed_entries = _registered_images(root)
    if owner_id:
        parsed_entries = [entry for entry in parsed_entries if entry["owner_id"] == owner_id]
    thin_asset_warnings: list[str] = []
    if plans:
        plan_numbers = {
            int(plan["number"])
            for plan in plans
            if isinstance(plan, dict) and int(plan.get("number") or 0) > 0
        }
        for entry in parsed_entries:
            declared = sorted({int(page) for page in entry.get("slides", [])})
            invalid = sorted(set(declared) - plan_numbers)
            if not invalid:
                continue
            raise ValueError(
                f"{entry['id']} has unknown slide references "
                + ",".join(f"{page:02d}" for page in invalid)
                + "; run image.py register with the corrected --slides; registered consumers were not changed"
            )
    exact_digest_owner: dict[str, str] = {}
    for entry in parsed_entries:
        if entry.get("status") in {"failed", "unavailable", "pending"}:
            continue
        path = _asset_path(root, entry["path"])
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"registered image {entry['id']}: {entry['path']} is missing or empty; register an available replacement or --status unavailable --reason ...")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != entry["sha256"]:
            raise ValueError(f"registered image {entry['id']}: {entry['path']} changed since commit; inspect it and run image.py register again")
        prior_id = exact_digest_owner.get(digest)
        if prior_id:
            thin_asset_warnings.append(
                f"{entry['id']} has the same pixels as {prior_id}; this may be "
                "intentional reuse, so both catalog entries are retained"
            )
        else:
            exact_digest_owner[digest] = entry["id"]
    receipts = _load_download_receipts(root)
    for entry in parsed_entries:
        if entry["kind"] != "real" or entry.get("status") in {"failed", "unavailable", "pending"}:
            continue
        receipt = receipts.get(entry["id"], {})
        path = _asset_path(root, entry["path"])
        current_sha = (
            hashlib.sha256(path.read_bytes()).hexdigest()
            if path.is_file() and path.stat().st_size > 0
            else ""
        )
        _validate_download_receipt(entry, receipt, current_sha, required=bool(entry.get("download")))
    failed_entries = [
        entry for entry in parsed_entries
        if entry.get("status") in {"failed", "unavailable", "pending"}
    ]
    declared_failed_pages = _catalog_failed_only_pages(parsed_entries)
    entries = (
        [
            entry
            for entry in parsed_entries
            if entry.get("status") not in {"failed", "unavailable", "pending"}
        ]
    )
    seen: set[str] = set()
    hashes: list[tuple[str, int]] = []
    warnings: list[str] = list(thin_asset_warnings)
    rows: list[tuple[str, str]] = []
    catalog_state = "empty" if not parsed_entries else "partial" if failed_entries else "available"
    for entry in entries:
        if entry["id"] in seen:
            raise ValueError(f"duplicate asset id: {entry['id']}")
        seen.add(entry["id"])
        path = _asset_path(root, entry["path"])
        dimensions = _asset_image_geometry(path, canvas)
        print(f"[asset-geometry] {entry['id']}: pixel_size={dimensions['width']}x{dimensions['height']}; full_bleed_scale={dimensions['full_bleed_scale']}")
        if path.suffix.lower() not in RASTER_SUFFIXES:
            raise ValueError(f"asset must be a raster image: {entry['path']}")
        digest = _difference_hash(path)
        for prior_id, prior_digest in hashes:
            distance = (digest ^ prior_digest).bit_count()
            if distance <= 4:
                warnings.append(f"{entry['id']}/{prior_id}: perceptual hash distance={distance}; "
                                "compare actual images before deciding whether they duplicate content")
        hashes.append((entry["id"], digest))
        if entry["expect_transparent"] and path.suffix.lower() in {".png", ".webp"}:
            report = inspect_image(root, entry["path"], True)
            if report["status"] != "PASS":
                message = (
                    f"asset {entry['id']!r} contains a baked checkerboard; "
                    f"run remove-checkerboard for {entry['path']}"
                    if report["baked_checkerboard"]
                    else f"asset {entry['id']!r} expects transparency but has no useful alpha"
                )
                warnings.append(message + "; the usable raster is retained")
        rows.append((entry["id"], entry["path"]))
    # 全局目录只投影登记事实，不读取其他作者正在替换的文件；锁内重读避免覆盖新登记。
    with (root / "_state/state.lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        write_image_catalog(root, _registered_images(root))
    suffix = hashlib.sha256((owner_id + json.dumps([(entry["id"], entry["sha256"]) for entry in entries], sort_keys=True)).encode()).hexdigest()[:16]
    sheet = _image_sheet(root, rows, name=f"contact-sheet-{suffix}" if owner_id else "contact-sheet") if rows else None
    pages_with_assets = {
        page
        for entry in entries
        for page in entry["slides"]
    }
    bitmap_ready = sorted(pages_with_assets - declared_failed_pages)
    failed_pages = sorted(declared_failed_pages - set(bitmap_ready))
    failed_candidate_pages = {
        page
        for entry in failed_entries
        for page in entry["slides"]
    }
    asset_gap_pages = sorted(failed_candidate_pages - set(failed_pages))
    print("status:PASS")
    print(f"catalog_state:{catalog_state}")
    print(f"assets:{len(entries)}")
    print(
        "bitmap_ready:"
        + (",".join(f"{page:02d}" for page in bitmap_ready) or "none")
    )
    print(
        "failed:"
        + (",".join(f"{page:02d}" for page in failed_pages) or "none")
    )
    # A page can remain production-ready when one optional/secondary asset
    # failed but another valid raster still covers the page.  Surface that
    # distinction explicitly so Image does not contradict the deterministic
    # page receipt in its prose handoff.
    print(
        "asset_gaps:"
        + (",".join(f"{page:02d}" for page in asset_gap_pages) or "none")
    )
    if sheet:
        print(sheet)
    for warning in warnings:
        print(f"[asset-warning] {warning}")


def _contact_sheet(paths: list[Path], target: Path, *, labels: list[int] | None = None) -> None:
    from .render_deck import write_contact_sheet

    write_contact_sheet(paths, target, width=CANVAS_W, height=CANVAS_H, labels=labels)


def _boxes_overlap(first: dict, second: dict) -> bool:
    return not (
        float(first.get("x", 0)) + float(first.get("width", 0))
        <= float(second.get("x", 0))
        or float(second.get("x", 0)) + float(second.get("width", 0))
        <= float(first.get("x", 0))
        or float(first.get("y", 0)) + float(first.get("height", 0))
        <= float(second.get("y", 0))
        or float(second.get("y", 0)) + float(second.get("height", 0))
        <= float(first.get("y", 0))
    )




def _layout_defects_enabled() -> bool:
    return str(os.environ.get("MURAL_LAYOUT_DEFECT", "on")).strip().lower() not in {
        "0", "false", "off", "no",
    }




def _layout_element(box: dict) -> str:
    tag = str(box.get("tag") or "text")
    classes = [
        token for token in re.split(r"\s+", str(box.get("class_name") or "").strip())
        if token
    ]
    return tag + "".join(f".{token}" for token in classes[:3])


def _layout_ancestor_hint(box: dict) -> str:
    chain = box.get("ancestor_chain")
    if not isinstance(chain, list):
        return ""
    return " > ".join(str(value) for value in chain[:4] if str(value).strip())




def _page_layout_defects(number: int, geometry: dict) -> list[dict]:
    """沿用提示通道报告实际溢出/矩形交叠，是否影响内容由看当前 PNG 的模型判断。"""
    if not _layout_defects_enabled() or not isinstance(geometry, dict):
        return []
    texts = [row for row in geometry.get("text_boxes", [])
             if isinstance(row, dict) and str(row.get("text") or "").strip()]
    blocks = [row for row in geometry.get("layout_blocks", [])
              if isinstance(row, dict) and row.get("parent_key")]
    facts: list[dict] = []

    def rect(row):
        return {key: float(row.get(key) or 0) for key in ("x", "y", "width", "height")}

    for kind, rows in (("text", texts), ("block", blocks)):
        for row in rows:
            box = rect(row)
            x = max(0.0, float(row.get("overflow_px_x") or 0))
            y = max(0.0, float(row.get("overflow_px_y") or 0))
            edge = max(0.0, -box["x"], -box["y"], box["x"] + box["width"] - CANVAS_W,
                       box["y"] + box["height"] - CANVAS_H)
            if max(x, y, edge) <= 8:
                continue
            facts.append({
                "type": kind + "_overflow", "severity": "advisory",
                "element": _layout_element(row), "ancestor_hint": _layout_ancestor_hint(row),
                "box": box, "overflow_px": {"x": round(x, 1), "y": round(y, 1), "canvas_edge": round(edge, 1)},
                "text": str(row.get("text") or "")[:96],
                "evidence": f"P{number:02d} measured overflow: x={x:.1f}px, y={y:.1f}px, canvas={edge:.1f}px.",
            })
        for index, left in enumerate(rows):
            for right in rows[index + 1:]:
                if kind == "block" and left.get("parent_key") != right.get("parent_key"):
                    continue
                if not _boxes_overlap(left, right):
                    continue
                left_parts = left.get("fragments", [left]) if kind == "text" else [left]
                right_parts = right.get("fragments", [right]) if kind == "text" else [right]
                ratio, sample = 0.0, None
                for a in left_parts:
                    for b in right_parts:
                        a, b = rect(a), rect(b)
                        x = max(0.0, min(a["x"] + a["width"], b["x"] + b["width"]) - max(a["x"], b["x"]))
                        y = max(0.0, min(a["y"] + a["height"], b["y"] + b["height"]) - max(a["y"], b["y"]))
                        area = min(a["width"] * a["height"], b["width"] * b["height"])
                        measured = x * y / area if area > 0 else 0.0
                        if measured > ratio:
                            ratio, sample = measured, [a, b]
                if ratio < 0.18:
                    continue
                facts.append({
                    "type": kind + "_collision", "severity": "advisory",
                    "elements": [_layout_element(left), _layout_element(right)],
                    "ancestor_hint": _layout_ancestor_hint(left) or _layout_ancestor_hint(right),
                    "overlap_ratio": round(ratio, 3), "fragments": sample,
                    "text": [str(row.get("text") or "")[:96] for row in (left, right)],
                    "evidence": f"P{number:02d} sampled {kind} rectangles overlap by {ratio:.0%} of the smaller rectangle; "
                                "this measurement alone does not establish an incorrect composition.",
                })
    for fact in facts:
        identity = {key: fact[key] for key in ("type", "text", "fragments", "box") if key in fact}
        identity["page"] = number
        fact["id"] = "LAYOUT-" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12].upper()
    return facts


def _page_composition_advisories(number: int, geometry: dict) -> list[dict]:
    """报告采样文字/媒体框之间的实际纵向空带，不推断留白意图或强制重排。"""
    intervals = []
    for item in [*geometry.get("text_boxes", []), *geometry.get("media", []), *geometry.get("images", [])]:
        if not isinstance(item, dict):
            continue
        top = max(0.0, float(item.get("y") or 0))
        bottom = min(float(CANVAS_H), float(item.get("y") or 0) + float(item.get("height") or 0))
        if bottom > top and float(item.get("width") or 0) > 0:
            intervals.append((top, bottom))
    if not intervals:
        return []
    merged = []
    for top, bottom in sorted(intervals):
        if not merged or top > merged[-1][1]:
            merged.append([top, bottom])
        else:
            merged[-1][1] = max(merged[-1][1], bottom)
    gaps = [(0.0, merged[0][0]), (merged[-1][1], float(CANVAS_H)),
            *((left[1], right[0]) for left, right in zip(merged, merged[1:]))]
    top, bottom = max(gaps, key=lambda row: row[1] - row[0])
    height = bottom - top
    if height < 100 or height / CANVAS_H < 0.18:
        return []
    evidence = (f"P{number:02d} sampled text/media boxes leave a {height:.1f}px vertical gap "
                f"at y={top:.1f}; sampling does not establish whether this whitespace is intentional.")
    return [{
        "id": "SPACE-" + hashlib.sha256(f"{number}:{top}:{bottom}".encode()).hexdigest()[:12].upper(),
        "type": "unowned_vertical_band", "severity": "advisory",
        "gap": {"y": round(top, 1), "height": round(height, 1), "stage_fraction": round(height / CANVAS_H, 3)},
        "evidence": evidence,
    }]


def _undersized_content_bitmaps(images: list[dict]) -> list[dict]:
    """报告图框的尺寸、面积和 cover 几何损失，不判定主体语义或强制复检。"""
    candidates: list[dict] = []
    page_area = float(CANVAS_W * CANVAS_H)
    for item in images:
        width = max(0.0, float(item.get("width") or 0))
        height = max(0.0, float(item.get("height") or 0))
        if width <= 0 or height <= 0:
            continue
        alt = str(item.get("alt") or "").strip()
        src = str(item.get("src") or "").strip()
        area_ratio = (width * height) / page_area
        short_side = min(width, height)
        long_side = max(width, height)
        display_aspect = long_side / max(1.0, short_side)
        natural_width = max(0.0, float(item.get("natural_width") or 0))
        natural_height = max(0.0, float(item.get("natural_height") or 0))
        crop_mismatch = 1.0
        if natural_width > 0 and natural_height > 0:
            natural_aspect = natural_width / natural_height
            cover_ratio = (width / height) / natural_aspect
            crop_mismatch = max(cover_ratio, 1.0 / cover_ratio)
        reasons: list[str] = []
        if area_ratio < 0.018:
            reasons.append("tiny_canvas_area")
        if short_side < 90:
            reasons.append("very_short_display_side")
        if short_side < 130 and display_aspect >= 4.0:
            reasons.append("collapsed_to_narrow_strip")
        if (
            str(item.get("object_fit") or "").lower() == "cover"
            and short_side < 180
            and crop_mismatch >= 3.5
        ):
            reasons.append("extreme_cover_crop")
        if not reasons:
            continue
        candidates.append({
            "src": src[:500],
            "alt": alt[:500],
            "display_width": round(width, 1),
            "display_height": round(height, 1),
            "area_ratio": round(area_ratio, 4),
            "display_aspect": round(display_aspect, 2),
            "crop_mismatch": round(crop_mismatch, 2),
            "reasons": reasons,
        })
    return candidates


_HAN_TEXT_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


def _font_token_stacks(root: Path) -> dict[str, list[str]]:
    for path in (
        root / "_trace/font-resolution.json",
        root / "_runtime/tmp/fonts/manifest.json",
    ):
        try:
            value = json.loads(path.read_text(encoding="utf-8")).get("token_stacks")
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
        if isinstance(value, dict):
            return {
                str(token): [str(family) for family in stack if str(family).strip()]
                for token, stack in value.items()
                if isinstance(stack, list)
            }
    return {}


def _locked_font_scripts() -> dict[str, frozenset[str]]:
    try:
        families = json.loads(
            Path(__file__).with_name("FONT-MANIFEST.lock.json").read_text(encoding="utf-8")
        ).get("families")
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}
    if not isinstance(families, dict):
        return {}
    return {
        str(family): frozenset(
            str(script)
            for face in (record.get("faces") or [])
            if isinstance(face, dict)
            for script in (face.get("scripts") or [])
        )
        for family, record in families.items()
        if isinstance(record, dict)
    }


def _first_known_han_family(
    stack: list[str],
    scripts: dict[str, frozenset[str]],
) -> str:
    """Return the first known Han face; unknown custom faces make it inconclusive."""
    for family in stack:
        supported = scripts.get(family)
        if supported is None:
            return ""
        if "hani" in supported:
            return family
    return ""


def _page_number_summary(numbers: list[int], limit: int = 12) -> str:
    visible = ",".join(str(number) for number in numbers[:limit])
    omitted = len(numbers) - limit
    return visible + (f" (+{omitted} more)" if omitted > 0 else "")


def _visual_contract_warnings(root: Path, plans: list[dict], rendered: dict) -> list[str]:
    """Cheap, non-blocking signals consumed in the existing contact-sheet review."""
    rows = {
        int(row.get("page", -1)): row
        for row in rendered.get("pages", [])
        if isinstance(row, dict)
    }
    numbers = sorted(
        int(plan["number"])
        for plan in plans
        if isinstance(plan, dict) and str(plan.get("number", "")).isdigit()
    )
    warnings: list[str] = []

    stacks = _font_token_stacks(root)
    display_stack = stacks.get("--font-display") or stacks.get("--font-title") or []
    body_stack = stacks.get("--font-body") or stacks.get("--font-sans") or []
    if display_stack and body_stack:
        scripts = _locked_font_scripts()
        display_han = _first_known_han_family(display_stack, scripts)
        body_han = _first_known_han_family(body_stack, scripts)
        display_primary = display_stack[0]
        body_primary = body_stack[0]
        display_pages = []
        body_pages = []
        for number in numbers:
            text_boxes = rows.get(number, {}).get("geometry", {}).get("text_boxes", [])
            large_han_families = [
                [
                    token.strip().strip("'\"")
                    for token in str(item.get("font_family") or "").split(",")
                    if token.strip()
                ]
                for item in text_boxes
                if isinstance(item, dict)
                and _HAN_TEXT_RE.search(str(item.get("text") or ""))
                and float(item.get("font_size") or 0) >= 36
            ]
            if any(families[:1] == [display_primary] for families in large_han_families):
                display_pages.append(number)
            if any(families[:1] == [body_primary] for families in large_han_families):
                body_pages.append(number)
        distinct_display_route = display_primary != body_primary
        if (
            distinct_display_route
            and display_han
            and display_han == body_han
            and display_pages
        ):
            warnings.append(
                "Chinese display text on page(s) "
                + _page_number_summary(display_pages)
                + f" resolves to the body Han family {display_han!r}; the leading display family "
                "changes only Latin/numerals. If the visual contract calls for a distinct Chinese "
                "display voice, choose a Han-capable display face. This is advisory, not a failure."
            )
        elif distinct_display_route and body_pages and not display_pages:
            warnings.append(
                "Large Chinese anchor text on page(s) "
                + _page_number_summary(body_pages)
                + " uses the body font while the separate display stack is not visibly applied. "
                "If the display role is intentionally limited to Latin/numerals, keep the current "
                "result; otherwise apply a Han-capable display voice to the intended anchor text. "
                "This is advisory and does not require another review round."
            )
    return warnings


def _geometry_audit(
    root: Path,
    plans: list[dict],
    rendered: dict,
) -> dict:
    rows = {int(row.get("page", -1)): row for row in rendered.get("pages", [])}
    errors: list[str] = []
    warnings: list[str] = []
    layout_defects: dict[str, list[dict]] = {}
    typography_flags: dict[str, list[str]] = {}
    composition_advisories: dict[str, list[dict]] = {}
    media_review_candidates: dict[str, dict] = {}
    font_weight_support = _bundled_weight_support(root)

    def geometry(number: int) -> dict:
        value = rows.get(number, {}).get("geometry", {})
        if not isinstance(value, dict) or not value:
            errors.append(f"page {number}: rendered geometry is missing")
            return {}
        return value

    for plan in plans:
        number = plan["number"]
        value = geometry(number)
        if not value:
            continue
        text_boxes = [
            box for box in value.get("text_boxes", []) if isinstance(box, dict)
        ]
        page_typography = [
            *_rendered_typography_warnings(number, text_boxes),
            *_unsupported_rendered_font_weights(
                number, text_boxes, font_weight_support
            ),
        ]
        warnings.extend(page_typography)
        if page_typography:
            typography_flags[f"{number:02d}"] = page_typography
        page_defects = _page_layout_defects(number, value)
        value["layout_defects"] = page_defects
        if page_defects:
            layout_defects[f"{number:02d}"] = page_defects
            warnings.append(
                f"page {number}: measured layout observations: "
                + ", ".join(str(item.get("type")) for item in page_defects)
            )
        page_composition = _page_composition_advisories(number, value)
        value["composition_advisories"] = page_composition
        if page_composition:
            composition_advisories[f"{number:02d}"] = page_composition
            warnings.append(
                f"page {number}: soft composition advisories: "
                + ", ".join(str(item.get("type")) for item in page_composition)
            )
        overflow_boxes = [
            box for box in text_boxes if box.get("overflow_x") or box.get("overflow_y")
        ]
        if overflow_boxes:
            warnings.append(
                f"page {number}: DOM text overflow in "
                + ", ".join(
                    repr(str(box.get("text") or "")[:48]) for box in overflow_boxes[:4]
                )
            )
        images = [item for item in value.get("images", []) if isinstance(item, dict)]
        media = [item for item in value.get("media", []) if isinstance(item, dict)]
        # CSS background images are a first-class authoring primitive in the
        # thin canvas.  Include them in the same identity/evidence geometry
        # triage as ordinary <img> elements; the renderer records their actual
        # painted box, source and background-size above.
        review_bitmaps = [
            *images,
            *[
                item for item in media
                if str(item.get("kind") or "") == "bitmap"
                and bool(item.get("background"))
            ],
        ]
        undersized_content = _undersized_content_bitmaps(review_bitmaps)
        if undersized_content:
            page_key = f"{number:02d}"
            candidate = {
                "type": "undersized_content_bitmap",
                "candidate_only": True,
                "items": undersized_content,
                "verification": (
                    f"Use full-resolution P{number:02d} to judge whether these measured "
                    "image sizes/crops suit the page; geometry does not establish subject completeness."
                ),
            }
            media_review_candidates[page_key] = candidate
            value["media_review_candidate"] = candidate
        value["media_inventory"] = {
            "kinds": sorted({str(item.get("kind") or "") for item in media if item.get("kind")}),
            "count": len(media),
            "boxes": [{key: item.get(key) for key in ("kind", "src", "x", "y", "width", "height", "area_ratio")} for item in media],
        }

    warnings.extend(_visual_contract_warnings(root, plans, rendered))
    return {
        "status": "FAIL" if errors else "PASS",
        "errors": errors,
        "warnings": warnings,
        "layout_defects": layout_defects,
        "typography_flags": typography_flags,
        "composition_advisories": composition_advisories,
        "media_review_candidates": media_review_candidates,
    }


def _incremental_manifest_seed(root: Path, numbers: list[int]) -> dict:
    """Recover page metadata from prior final or isolated Slide renders."""
    manifest_path = root / "renders" / "render.json"
    seed: dict = {}
    try:
        seed = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        seed = {}
    rows = {
        int(row.get("page", -1)): dict(row)
        for row in seed.get("pages", [])
        if isinstance(row, dict) and str(row.get("page", "")).lstrip("-").isdigit()
    }
    errors = list(seed.get("console_errors") or [])
    for page in numbers:
        isolated_path = _page_render_metadata_path(root, page)
        if not isolated_path.is_file():
            isolated_path = root / "renders" / f".page_{page:02d}" / "render.json"
        try:
            isolated = json.loads(isolated_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        candidates = isolated.get("pages") or []
        if not candidates or not isinstance(candidates[0], dict):
            continue
        # A Slide Owner may have rendered and inspected a newer page after the
        # previous whole-deck manifest was written.  When that page checkpoint
        # matches the current HTML/CSS/assets, its isolated geometry is the
        # authoritative metadata even if the final manifest still contains an
        # older row.  Otherwise finalize can reuse current PNG bytes while
        # retaining stale warnings and text boxes from the prior page version.
        if page in rows and not _render_checkpoint_current(root, page, {page}):
            continue
        row = dict(candidates[0])
        row["page"] = page
        row["png"] = str(root / "renders" / f"slide_{page:02d}.png")
        rows[page] = row
        errors.extend(str(item) for item in isolated.get("console_errors") or [])
    ordered = [rows[page] for page in numbers if page in rows]
    canvas = read_geometry(root)
    return {
        "w": CANVAS_W,
        "h": CANVAS_H,
        "canvas": canvas,
        "mode": "incremental-finalize",
        "nav": "verified-query-jump",
        "n_pages": len(numbers),
        "pages": ordered,
        "console_errors": list(dict.fromkeys(errors)),
        "blank_pages": [int(row["page"]) for row in ordered if row.get("blank")],
        "static_pages": [int(row["page"]) for row in ordered if row.get("static")],
        "capture_recovered_pages": [
            int(row["page"]) for row in ordered if row.get("capture_recovered")
        ],
    }


def _render_checkpoint_current(root: Path, page: int, manifest_pages: set[int]) -> bool:
    png = root / "renders" / f"slide_{page:02d}.png"
    if page not in manifest_pages or not png.is_file() or png.stat().st_size < 1:
        return False
    try:
        _path, _hashes, _attempts, published_hash, _defects = _page_render_state(
            root, page
        )
        return bool(published_hash and published_hash == _page_digest(root, page))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False


def finalize(root: Path, expected: int | None = None) -> None:
    deck, plans = _load_plans(root, expected)
    _check_target_count(deck)
    recovered_previews = recover_stale_render_previews(root)
    if recovered_previews:
        print(
            "[workspace-recovery] quarantined stale renderer previews: "
            + ", ".join(recovered_previews)
        )
    assert_workspace_clean(root)
    sync_speech(root, expected)
    numbers = build(root, expected)
    renderer = Path(__file__).with_name("render_deck.py")
    manifest_path = root / "renders" / "render.json"
    manifest = _incremental_manifest_seed(root, numbers)
    atomic_write_text(
        root,
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    manifest_pages = {
        int(row.get("page", -1))
        for row in manifest.get("pages", [])
        if isinstance(row, dict)
    }
    changed_pages = [
        page for page in numbers
        if not _render_checkpoint_current(root, page, manifest_pages)
    ]
    for page in changed_pages:
        page_png = root / "renders" / f"slide_{page:02d}.png"
        try:
            stat = page_png.stat()
            before_token = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        except OSError:
            before_token = None
        _run_renderer(
            [
                sys.executable,
                str(renderer),
                "--workspace", str(root),
                str(root / "present.html"),
                str(root / "renders"),
                "--page",
                str(page),
            ]
        )
        try:
            stat = page_png.stat()
            after_token = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        except OSError as exc:
            raise RuntimeError(
                f"renderer reported success but slide_{page:02d}.png is missing"
            ) from exc
        if after_token[2] < 1 or after_token == before_token:
            raise RuntimeError(
                f"renderer reported success but slide_{page:02d}.png was not refreshed"
            )
        state_path, hashes, attempts, _published, _defects = _page_render_state(
            root, page
        )
        digest = _page_digest(root, page)
        if digest not in hashes:
            hashes.append(digest)
            attempts.setdefault("finalize", []).append(digest)
        row = next(
            (
                value for value in manifest.get("pages", [])
                if isinstance(value, dict) and int(value.get("page", -1)) == page
            ),
            {},
        )
        defects = _page_layout_defects(
            page,
            row.get("geometry", {}) if isinstance(row, dict) else {},
        )
        _publish_render_state(
            root, page, state_path, hashes, attempts, digest, defects
        )
    if not manifest_path.is_file():
        raise FileNotFoundError("renders/render.json is missing after final render")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rendered_numbers = [int(row.get("page", -1)) for row in manifest.get("pages", [])]
    if rendered_numbers != numbers:
        raise ValueError(
            f"final render pages differ: expected {numbers}, found {rendered_numbers}"
        )
    geometry = _geometry_audit(root, plans, manifest)
    manifest["present"] = str(root / "present.html")
    manifest["delivery_source"] = "present.html"
    manifest["mode"] = "incremental-finalize"
    manifest["rendered_pages"] = changed_pages
    manifest["reused_pages"] = [page for page in numbers if page not in changed_pages]
    manifest["special_page_geometry"] = geometry
    manifest["layout_defects"] = geometry.get("layout_defects", {})
    manifest["typography_flags"] = geometry.get("typography_flags", {})
    manifest["composition_advisories"] = geometry.get("composition_advisories", {})
    manifest["media_review_candidates"] = geometry.get(
        "media_review_candidates", {}
    )
    atomic_write_text(
        root,
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    # Preserve a complete visual handoff even when deterministic geometry finds
    # a hard defect. Review needs the whole-deck contact sheet to locate and
    # repair that defect; raising before the sheet existed created a deadlock
    # where finalize failed, Review was blocked, and the completed Slide owner
    # correctly could not be re-created as _r2.
    page_paths = [root / "renders" / f"slide_{page:02d}.png" for page in numbers]
    contact_sheet = root / "renders" / "contact-sheet.png"
    _contact_sheet(page_paths, contact_sheet, labels=numbers)
    if not contact_sheet.is_file():
        raise FileNotFoundError("renders/contact-sheet.png is missing")
    if geometry["errors"]:
        raise ValueError(
            "layout/canvas audit failed:\n- " + "\n- ".join(geometry["errors"])
        )
    # 先输出机械回执，避免长提示截断后丢失；整册判断仍由 Orchestrator 完成。
    assert_workspace_clean(root)
    print("status:PASS")
    for warning in geometry["warnings"]:
        print(f"[deterministic-visual-warning] {warning}")
    print(
        "[workflow] Orchestrator reviews deck-level coverage, evidence and narrative; "
        "inspect the whole-deck contact sheet, then open pages with concrete concerns."
    )
    print(
        "incremental-finalize: rendered="
        + (",".join(f"{page:02d}" for page in changed_pages) or "none")
        + " reused="
        + (",".join(f"{page:02d}" for page in numbers if page not in changed_pages) or "none")
    )
    print(contact_sheet)
