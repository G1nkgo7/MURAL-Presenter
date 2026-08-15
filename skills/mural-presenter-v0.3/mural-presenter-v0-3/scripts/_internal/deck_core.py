#!/usr/bin/env python3
"""Shared deterministic implementation for MuralPresenter v0.3.

Canonical planning files:
  plan/deck.md
  plan/slide_01.md ... plan/slide_NN.md

This module is not a model-facing CLI. Public Role entry points live one level
above as ``orchestrator.py``, ``image.py``, ``slide.py``, and ``review.py``.
They all import this implementation so planning, assets, rendering, delivery,
font handling, and workspace policy never fork into duplicated copies.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import html
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time
from urllib.parse import urlsplit
import uuid

import requests

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

_PREVIOUS_DONT_WRITE_BYTECODE = sys.dont_write_bytecode
sys.dont_write_bytecode = True
try:
    from .font_bundle import bundle_workspace, validate_font_bundle
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
        temporary_text,
    )
finally:
    sys.dont_write_bytecode = _PREVIOUS_DONT_WRITE_BYTECODE

CANVAS_W = 1600
CANVAS_H = 900
# Normal authoring has a three-state soft ceiling per page lifecycle. The higher
# safety ceiling remains available to deterministic finalization/Review, but a
# re-dispatched Slide Agent inherits the page's existing authoring states.
MAX_PAGE_RENDER_STATES = 8
AUTHORING_PAGE_RENDER_STATES = 3
MAX_ASSET_DOWNLOAD_BYTES = 50 * 1024 * 1024
ASSET_DOWNLOAD_WORKERS = 6
SLIDE_RE = re.compile(r"slide_(\d+)\.html$")
PLAN_RE = re.compile(r"slide_(\d+)\.md$")
SPECIAL_TYPES = {"cover", "section-divider", "closing"}
SPECIAL_LAYOUTS = {
    "cover": {"split", "centered", "lower-third", "poster"},
    "section-divider": {
        "number-copy",
        "centered",
        "visual-split",
        "vertical-rail",
        "banded",
    },
    "closing": {"centered", "split", "lower-third"},
}
OWNERSHIP_TOPOLOGIES = {"single", "grouped"}
BITMAP_STRATEGIES = {"active", "unavailable", "user-forbidden", "not-beneficial"}
COMPOSITIONS = {
    "visual-split",
    "data-focus",
    "comparison",
    "sequence",
    "matrix",
    "editorial",
    "freeform",
}
RASTER_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
ASSET_KINDS = {"real", "generated", "material", "user"}
SECTION_ALIASES = {
    "narrative": {"narrative", "叙事"},
    "evidence": {"evidence", "内容证据", "证据"},
    "copy": {
        "on-screen copy (exact)",
        "on-screen copy",
        "屏显文案（定版）",
        "屏显文案(定版)",
        "屏显文案",
    },
    "visual": {"semantic visual need", "语义视觉需求"},
    "composition": {"composition blueprint", "构图蓝图"},
    "anchors": {"render anchors", "渲染锚点"},
    "constraints": {"constraints", "约束"},
    "speech": {"speech beat", "讲稿节拍"},
    "resolved": {"resolved deck brief"},
    "theme": {"theme tokens", "主题变量"},
}
STRUCTURAL_SUBJECTS = {
    "slide-inner",
    "page-frame",
    "page-frame--content",
    "page-header",
    "page-footer",
    "special-background",
    "special-overlay",
    "special-safe",
}
STRUCTURAL_LAYOUT_PROPERTIES = {
    "position",
    "inset",
    "top",
    "right",
    "bottom",
    "left",
    "width",
    "height",
    "min-width",
    "max-width",
    "min-height",
    "max-height",
    "display",
    "grid",
    "grid-template",
    "grid-template-columns",
    "grid-template-rows",
    "grid-column",
    "grid-row",
    "align-content",
    "align-items",
    "align-self",
    "justify-content",
    "justify-items",
    "justify-self",
    "place-content",
    "place-items",
    "gap",
    "row-gap",
    "column-gap",
    "margin",
    "margin-top",
    "margin-right",
    "margin-bottom",
    "margin-left",
    "padding",
    "padding-top",
    "padding-right",
    "padding-bottom",
    "padding-left",
    "transform",
    "z-index",
    "overflow",
}
BACKGROUND_PROPERTIES = {"background", "background-color", "background-image"}
_RENDER_ENV_ALLOW = {
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


def prepare(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    for rel in ("plan", "slides", "assets", "research", "renders"):
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
    catalog = root / "assets" / "catalog.md"
    if not catalog.exists():
        atomic_write_text(
            root,
            catalog,
            "# Asset catalog\n\nNo local image assets selected yet.\n",
        )
        created.append("assets/catalog.md")
    print("status:PASS")
    print("created:", ", ".join(created) if created else "none")


def restore_base(root: Path) -> None:
    source = Path(__file__).resolve().parents[2] / "assets" / "base.css"
    if not source.is_file():
        raise FileNotFoundError(f"frozen Skill base.css is missing: {source}")
    atomic_copy(root, source, root / "base.css")
    print("status:PASS")
    print("restored: base.css")


def apply_plan_batch(root: Path) -> None:
    """Expand one model-written manifest into canonical per-page plans."""
    batch_path = root / "plan" / "plan-batch.json"
    if not batch_path.is_file():
        raise FileNotFoundError("plan/plan-batch.json is missing")
    try:
        payload = json.loads(batch_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"plan batch is invalid JSON: {exc}") from exc
    allowed_keys = {"files", "replace_existing"}
    if (
        not isinstance(payload, dict)
        or "files" not in payload
        or not set(payload).issubset(allowed_keys)
    ):
        raise ValueError(
            "plan batch must contain files and optional replace_existing"
        )
    replace_existing = payload.get("replace_existing", False)
    if not isinstance(replace_existing, bool):
        raise ValueError("plan batch replace_existing must be a boolean")
    files = payload["files"]
    if not isinstance(files, list) or not 2 <= len(files) <= 6:
        raise ValueError("plan batch must contain 2-6 files")
    prepared: list[tuple[int, Path, str]] = []
    seen: set[int] = set()
    for index, item in enumerate(files, start=1):
        if not isinstance(item, dict) or set(item) != {"path", "content"}:
            raise ValueError(f"plan batch item {index} must contain path and content")
        relative = item.get("path")
        content = item.get("content")
        match = (
            re.fullmatch(r"plan/slide_(\d{2})\.md", relative)
            if isinstance(relative, str)
            else None
        )
        if not match:
            raise ValueError(f"plan batch item {index} has noncanonical path: {relative!r}")
        page = int(match.group(1))
        if page < 1 or page in seen:
            raise ValueError(f"plan batch has invalid or duplicate page: {page}")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"plan batch item {index} content is empty")
        seen.add(page)
        prepared.append((page, root / relative, content))
    pages = sorted(seen)
    if pages != list(range(pages[0], pages[-1] + 1)):
        raise ValueError(f"plan batch pages must be consecutive: {pages}")
    for page, target, content in prepared:
        if (
            not replace_existing
            and target.is_file()
            and target.read_text(encoding="utf-8") != content
        ):
            raise ValueError(
                f"plan/slide_{page:02d}.md already differs; during pre-production "
                "recovery call write_plan_batch with replace_existing=true, or batch "
                "only missing pages"
            )
    changed = 0
    for _, target, content in prepared:
        if not target.is_file() or target.read_text(encoding="utf-8") != content:
            atomic_write_text(root, target, content)
            changed += 1
    batch_path.unlink()
    print("status:PASS")
    print(
        f"plan batch applied: pages={','.join(f'{page:02d}' for page in pages)} "
        f"changed={changed} replace_existing={str(replace_existing).lower()}"
    )


def _token(value: object, *, field: str) -> str:
    token = str(value or "").strip().lower().replace("_", "-")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", token):
        raise ValueError(f"{field} must be a lowercase token: {value!r}")
    return token


def _bool(value: object, *, default: bool) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in {"true", "yes", "1", "on"}:
        return True
    if text in {"false", "no", "0", "off"}:
        return False
    raise ValueError(f"boolean value expected, found {value!r}")


def _html_text(value: object) -> str:
    return "<br>".join(
        html.escape(part.strip())
        for part in str(value or "").splitlines()
        if part.strip()
    )


def _copy_shape(value: object) -> str:
    length = len(re.sub(r"\s+", "", str(value or "")))
    if length <= 10:
        return "short"
    if length <= 20:
        return "medium"
    if length <= 34:
        return "long"
    return "extended"


def _heading_key(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value.strip().lower())
    for key, aliases in SECTION_ALIASES.items():
        if normalized in aliases:
            return key
    return normalized


def _sections(text: str) -> dict[str, str]:
    matches = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", text))
    result: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        result[_heading_key(match.group(1))] = text[match.end():end].strip()
    return result


def _preamble(text: str) -> str:
    match = re.search(r"(?m)^##\s+", text)
    return text[: match.start()] if match else text


def _bullet_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^\s*-\s*([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$", line)
        if match:
            values[match.group(1).lower().replace("-", "_")] = match.group(2).strip()
    return values


def _theme_tokens(section: str) -> dict[str, str]:
    fence = re.search(r"```(?:css)?\s*(.*?)```", section, flags=re.I | re.S)
    source = fence.group(1).strip() if fence else section.strip()
    root_match = re.fullmatch(r":root\s*\{(.*)\}\s*", source, flags=re.S)
    if not root_match:
        raise ValueError("deck.md Theme Tokens must contain one short :root { ... } block")
    body = re.sub(r"/\*.*?\*/", "", root_match.group(1), flags=re.S)
    tokens: dict[str, str] = {}
    for match in re.finditer(r"(--[A-Za-z0-9_-]+)\s*:\s*([^;{}]+);", body):
        tokens[match.group(1)] = match.group(2).strip()
    residue = re.sub(r"--[A-Za-z0-9_-]+\s*:\s*[^;{}]+;", "", body)
    if residue.strip():
        raise ValueError("Theme Tokens may contain CSS custom-property declarations only")
    if "--content-canvas" not in tokens:
        raise ValueError("Theme Tokens must define --content-canvas")
    self_references = [
        name
        for name, value in tokens.items()
        if re.fullmatch(rf"var\(\s*{re.escape(name)}\s*\)", value, flags=re.I)
    ]
    if self_references:
        raise ValueError(
            "Theme Tokens contain invalid self-references: "
            + ", ".join(self_references)
            + "; omit unchanged tokens or point the role to a different base token"
        )
    return tokens


def _parse_deck(root: Path, expected: int | None = None) -> dict:
    path = root / "plan" / "deck.md"
    if not path.is_file():
        raise FileNotFoundError("plan/deck.md is missing")
    text = path.read_text(encoding="utf-8")
    meta = _bullet_values(_preamble(text))
    sections = _sections(text)
    if not meta.get("title"):
        raise ValueError("plan/deck.md missing metadata: ['title']")
    if "resolved" not in sections:
        raise ValueError("plan/deck.md is missing ## Resolved deck brief")
    resolved = _bullet_values(sections["resolved"])
    missing = [
        key
        for key in (
            "language",
            "page_count",
            "audience",
            "image_mode",
            "bitmap_strategy",
            "bitmap_rationale",
            "ownership_topology",
            "ownership_rationale",
            "rationale",
        )
        if not resolved.get(key)
    ]
    if missing:
        raise ValueError(f"Resolved deck brief missing fields: {missing}")
    language = resolved["language"].lower()
    if language not in {"zh", "en"}:
        raise ValueError("Resolved deck brief language must be zh or en")
    try:
        pages = int(resolved["page_count"])
    except ValueError as exc:
        raise ValueError("Resolved deck brief page_count must be an integer") from exc
    if pages < 1:
        raise ValueError("Resolved deck brief page_count must be positive")
    if expected is not None and pages != expected:
        raise ValueError(
            f"expected {expected} pages, Resolved deck brief declares {pages}"
        )
    ownership_topology = resolved["ownership_topology"].strip().lower()
    if ownership_topology not in OWNERSHIP_TOPOLOGIES:
        raise ValueError(
            "Resolved deck brief ownership_topology must be single or grouped"
        )
    bitmap_strategy = resolved["bitmap_strategy"].strip().lower()
    if bitmap_strategy not in BITMAP_STRATEGIES:
        raise ValueError(
            "Resolved deck brief bitmap_strategy must be one of "
            f"{sorted(BITMAP_STRATEGIES)}"
        )
    if "theme" not in sections:
        raise ValueError("plan/deck.md is missing ## Theme Tokens")
    tokens = _theme_tokens(sections["theme"])
    warnings: list[str] = []
    for key, aliases in {
        "audience": {"audience and objective", "受众与目标"},
        "narrative": {"narrative and page map", "叙事与页面地图"},
        "storyboard": {"visual storyboard", "视觉故事板"},
        "visual": {"visual contract", "视觉契约"},
        "special": {"special pages", "特殊页"},
    }.items():
        if not any(alias in sections for alias in aliases):
            warnings.append(f"deck.md should include {sorted(aliases)[0]!r}")
    return {
        "path": path,
        "text": text,
        "language": language,
        "ownership_topology": ownership_topology,
        "bitmap_strategy": bitmap_strategy,
        "pages": pages,
        "title": meta["title"],
        "footer": meta.get("footer", ""),
        "resolved": resolved,
        "tokens": tokens,
        "sections": sections,
        "warnings": warnings,
    }


def _infer_composition(page_type: str, page_family: str) -> str:
    semantics = f"{page_type} {page_family}"
    choices = (
        ("comparison", ("comparison", "compare", "versus", "before-after", "pros-cons")),
        ("sequence", ("process", "timeline", "roadmap", "journey", "workflow", "steps")),
        ("matrix", ("matrix", "table", "checklist", "scorecard", "rubric")),
        (
            "data-focus",
            (
                "chart",
                "data",
                "kpi",
                "metric",
                "map",
                "diagram",
                "architecture",
                "system",
                "dashboard",
            ),
        ),
        ("editorial", ("statement", "quote", "manifesto", "thesis", "agenda")),
    )
    for composition, markers in choices:
        if any(marker in semantics for marker in markers):
            return composition
    return "visual-split"


def _parse_slide(path: Path, number: int) -> dict:
    text = path.read_text(encoding="utf-8")
    heading = re.search(r"(?mi)^#\s*slide[_\s-]*0*(\d+)\s*$", text)
    if not heading or int(heading.group(1)) != number:
        raise ValueError(f"{path.name} must begin with # slide_{number:02d}")
    meta = _bullet_values(_preamble(text))
    sections = _sections(text)
    missing_meta = [
        key
        for key in ("role", "page_type", "page_family", "production_group", "needs_bitmap")
        if not meta.get(key)
    ]
    if missing_meta:
        raise ValueError(f"{path.name} missing metadata: {missing_meta}")
    page_type = _token(meta["page_type"], field=f"{path.name} page_type")
    page_family = _token(meta["page_family"], field=f"{path.name} page_family")
    production_group = _token(
        meta["production_group"], field=f"{path.name} production_group"
    )
    special = page_type in SPECIAL_TYPES
    special_layout = ""
    if special:
        if not meta.get("special_layout"):
            raise ValueError(f"{path.name} special page needs special_layout")
        special_layout = _token(
            meta["special_layout"],
            field=f"{path.name} special_layout",
        )
        allowed_layouts = SPECIAL_LAYOUTS[page_type]
        if special_layout not in allowed_layouts:
            raise ValueError(
                f"{path.name} special_layout for {page_type} must be one of "
                f"{sorted(allowed_layouts)}"
            )
    elif meta.get("special_layout"):
        raise ValueError(f"{path.name} special_layout is only for special pages")
    bitmap_token = meta["needs_bitmap"].strip().lower()
    if bitmap_token not in {"true", "false"}:
        raise ValueError(f"{path.name} needs_bitmap must be true or false")
    needs_bitmap = bitmap_token == "true"
    composition = ""
    composition_explicit = False
    if special:
        if meta.get("composition"):
            raise ValueError(f"{path.name} composition is only for content pages")
    else:
        if meta.get("composition"):
            composition_explicit = True
            composition = _token(
                meta["composition"],
                field=f"{path.name} composition",
            )
            if composition not in COMPOSITIONS:
                raise ValueError(
                    f"{path.name} composition must be one of "
                    f"{sorted(COMPOSITIONS)}"
                )
        else:
            composition = _infer_composition(page_type, page_family)
    canvas_variant = _token(
        meta.get("canvas_variant", "base"),
        field=f"{path.name} canvas_variant",
    )
    copy = _bullet_values(sections.get("copy", ""))
    if not copy.get("title"):
        raise ValueError(f"{path.name} needs title in ## On-screen copy (exact)")
    required_sections = ["narrative", "copy", "speech"]
    missing_sections = [name for name in required_sections if not sections.get(name)]
    if missing_sections:
        raise ValueError(f"{path.name} missing sections: {missing_sections}")
    if not special and not sections.get("evidence"):
        raise ValueError(f"{path.name} content page needs ## Evidence")
    if not sections.get("visual"):
        raise ValueError(f"{path.name} needs a Semantic visual need")
    section_index = copy.get("section_index", "")
    if page_type == "section-divider":
        if section_index and not re.fullmatch(r"\d{1,3}", section_index):
            raise ValueError(
                f"{path.name} section_index must be numeric, not a CHAPTER label"
            )
    show_footer = _bool(
        meta.get("show_footer"),
        default=not special,
    )
    return {
        "path": path,
        "text": text,
        "number": number,
        "role": meta["role"],
        "page_type": page_type,
        "page_family": page_family,
        "production_group": production_group,
        "special_layout": special_layout,
        "needs_bitmap": needs_bitmap,
        "composition": composition,
        "composition_explicit": composition_explicit,
        "canvas_variant": canvas_variant,
        "show_footer": show_footer,
        "eyebrow": copy.get("eyebrow", ""),
        "title": copy["title"],
        "subtitle": copy.get("subtitle", ""),
        "section_index": section_index,
        "narrative": sections.get("narrative", ""),
        "evidence": sections.get("evidence", ""),
        "visual": sections.get("visual", ""),
        "composition_blueprint": sections.get("composition", ""),
        "anchors": sections.get("anchors", ""),
        "constraints": sections.get("constraints", ""),
        "speech": sections.get("speech", ""),
    }


def _plan_paths(root: Path) -> list[tuple[int, Path]]:
    rows: list[tuple[int, Path]] = []
    for path in (root / "plan").glob("slide_*.md"):
        match = PLAN_RE.fullmatch(path.name)
        if match:
            rows.append((int(match.group(1)), path))
    rows.sort()
    return rows


def _load_plans(
    root: Path,
    expected: int | None = None,
) -> tuple[dict, list[dict]]:
    deck = _parse_deck(root, expected)
    rows = _plan_paths(root)
    numbers = [number for number, _ in rows]
    wanted = list(range(1, deck["pages"] + 1))
    if numbers != wanted:
        raise ValueError(f"expected plan pages {wanted}, found {numbers}")
    plans = [_parse_slide(path, number) for number, path in rows]
    return deck, plans


def validate_plans(root: Path, expected: int | None = None) -> list[dict]:
    deck, plans = _load_plans(root, expected)
    grouped_pages: dict[str, list[int]] = {}
    for plan in plans:
        grouped_pages.setdefault(plan["production_group"], []).append(plan["number"])
    if deck["ownership_topology"] == "single":
        reused = {
            group: pages for group, pages in grouped_pages.items() if len(pages) != 1
        }
        if reused:
            raise ValueError(
                "single ownership requires one unique production_group per page; "
                f"reused groups: {reused}"
            )
    else:
        oversized = {
            group: pages for group, pages in grouped_pages.items() if len(pages) > 4
        }
        non_contiguous = {
            group: pages
            for group, pages in grouped_pages.items()
            if pages != list(range(min(pages), max(pages) + 1))
        }
        if oversized:
            raise ValueError(
                "grouped ownership allows at most four adjacent pages per group; "
                f"oversized groups: {oversized}"
            )
        if non_contiguous:
            raise ValueError(
                "grouped ownership requires contiguous pages inside each group; "
                f"non-contiguous groups: {non_contiguous}"
            )
        if len(plans) > 1 and not any(len(pages) >= 2 for pages in grouped_pages.values()):
            raise ValueError(
                "grouped ownership must contain at least one multi-page dependency group"
            )
    bitmap_pages = [plan["number"] for plan in plans if plan["needs_bitmap"]]
    strategy = deck["bitmap_strategy"]
    runtime_requirements: dict = {}
    try:
        runtime_payload = json.loads(
            (root / "_trace" / "runtime-capabilities.json").read_text(
                encoding="utf-8"
            )
        )
        if isinstance(runtime_payload, dict) and isinstance(
            runtime_payload.get("requirements"), dict
        ):
            runtime_requirements = dict(runtime_payload["requirements"])
    except (OSError, UnicodeError, json.JSONDecodeError):
        runtime_requirements = {}
    if bool(runtime_requirements.get("bitmap_required")) and not bitmap_pages:
        raise ValueError(
            "user requirement bitmap_required=true requires at least one "
            "needs_bitmap:true page; image acquisition failure must remain "
            "image_blocked and cannot be silently downgraded to a zero-bitmap deck"
        )
    if strategy == "active" and not bitmap_pages:
        raise ValueError(
            "bitmap_strategy=active requires at least one needs_bitmap:true page; "
            "reconsider cover, transition, closing, and narrative-peak pages"
        )
    if strategy != "active" and bitmap_pages:
        raise ValueError(
            f"bitmap_strategy={strategy} conflicts with needs_bitmap:true pages {bitmap_pages}"
        )
    content_pages = [
        plan["number"] for plan in plans if plan["page_type"] not in SPECIAL_TYPES
    ]
    divider_pages = [
        plan["number"] for plan in plans if plan["page_type"] == "section-divider"
    ]
    special_pages = [
        plan["number"] for plan in plans if plan["page_type"] in SPECIAL_TYPES
    ]
    structure_warnings: list[str] = []
    divider_limit = max(1, len(content_pages) // 3)
    if len(divider_pages) > divider_limit:
        structure_warnings.append(
            "section-divider quota exceeded: "
            f"{len(divider_pages)} divider pages {divider_pages} for "
            f"{len(content_pages)} content pages (recommended maximum {divider_limit}); "
            "merge minor chapter breaks into the next content page eyebrow or section label"
        )
    special_ratio = len(special_pages) / len(plans)
    if special_ratio > 0.40:
        structure_warnings.append(
            "special-page ratio exceeds 40%: "
            f"{len(special_pages)}/{len(plans)} pages ({special_ratio:.1%}) are "
            f"cover, closing, or section-divider pages {special_pages}; "
            "keep content pages as the deck majority"
        )
    for index, plan in enumerate(plans):
        if plan["page_type"] != "section-divider":
            continue
        followers: list[int] = []
        for candidate in plans[index + 1:]:
            if candidate["page_type"] in {"section-divider", "closing"}:
                break
            if candidate["page_type"] not in SPECIAL_TYPES:
                followers.append(candidate["number"])
        if len(followers) < 2:
            structure_warnings.append(
                f"section-divider slide_{plan['number']:02d} leads only "
                f"{len(followers)} content page(s) {followers or 'none'}; "
                "remove the standalone divider and carry its chapter label into "
                "the next content page unless this pause is essential to the talk"
            )
    print("status:PASS")
    print(
        f"plans:{len(plans)} language:{deck['language']} "
        f"ownership:{deck['ownership_topology']} bitmap_pages:"
        + (",".join(f"{page:02d}" for page in bitmap_pages) or "none")
    )
    for warning in [
        *deck["warnings"],
        *structure_warnings,
        *_anti_slop_warnings(root, deck, plans),
    ]:
        print(f"[plan-warning] {warning}")
    for plan in plans:
        if plan["page_type"] in SPECIAL_TYPES:
            continue
        if not plan["composition_explicit"]:
            print(
                f"[plan-warning] slide_{plan['number']:02d}.md omitted "
                f"composition; inferred {plan['composition']!r}"
            )
        if not plan["composition_blueprint"]:
            print(
                f"[plan-warning] slide_{plan['number']:02d}.md omitted "
                "Composition blueprint"
            )
    return plans


def _apply_theme_tokens(root: Path, deck: dict) -> None:
    path = root / "base.css"
    if not path.is_file():
        raise FileNotFoundError("base.css is missing; run prepare first")
    css = path.read_text(encoding="utf-8")
    match = re.search(r":root\s*\{(?P<body>.*?)\n\}", css, flags=re.S)
    if not match:
        raise ValueError("base.css is missing its root token block")
    body = match.group("body")
    for name, value in deck["tokens"].items():
        pattern = re.compile(rf"(?m)^(\s*){re.escape(name)}\s*:\s*[^;]+;")
        if pattern.search(body):
            body = pattern.sub(lambda item: f"{item.group(1)}{name}: {value};", body)
        else:
            body += f"\n  {name}: {value};"
    updated = css[: match.start("body")] + body + css[match.end("body"):]
    if updated != css:
        atomic_write_text(root, path, updated)


def _copy_html(plan: dict) -> tuple[str, str, str]:
    eyebrow = (
        f'      <span class="eyebrow">{_html_text(plan["eyebrow"])}</span>\n'
        if plan["eyebrow"]
        else ""
    )
    title = f'      <h1 class="page-title">{_html_text(plan["title"])}</h1>\n'
    subtitle = (
        f'      <p class="page-subtitle">{_html_text(plan["subtitle"])}</p>\n'
        if plan["subtitle"]
        else ""
    )
    return eyebrow, title, subtitle


def _composition_skeleton(composition: str) -> str:
    slots = {
        "visual-split": ("visual", "copy"),
        "data-focus": ("primary", "support"),
        "comparison": ("left", "right"),
        "sequence": ("primary", "support"),
        "matrix": ("primary", "support"),
        "editorial": ("primary", "support"),
        "freeform": ("primary",),
    }[composition]
    rows = [
        f'          <div class="composition composition--{composition}" '
        f'data-composition="{composition}">'
    ]
    for index, slot in enumerate(slots):
        role = "primary" if index == 0 else "secondary"
        rows.append(
            f'            <div class="composition__{role} slot slot-{slot}" '
            f'data-slot="{slot}"></div>'
        )
    rows.append("          </div>")
    return "\n".join(rows)


def _slide_skeleton(plan: dict, deck: dict) -> str:
    number = plan["number"]
    total = deck["pages"]
    page_type = plan["page_type"]
    family = plan["page_family"]
    eyebrow, title, subtitle = _copy_html(plan)
    footer_text = html.escape(deck["footer"])
    if page_type in SPECIAL_TYPES:
        kind = "divider" if page_type == "section-divider" else page_type
        special_layout = plan["special_layout"]
        root_classes = (
            f"slide special-canvas special-{kind} page-{page_type} "
            f"family-{family} layout-{special_layout} "
            f"title-{_copy_shape(plan['title'])}"
        )
        if page_type == "section-divider":
            number_html = (
                '      <span class="section-number" '
                'data-scaffold-owned="section-number">'
                f'{html.escape(plan["section_index"])}</span>\n'
                if plan["section_index"]
                else ""
            )
            header = f"""    <header class="special-header special-header--divider" data-scaffold-owned="header">
{number_html}      <div class="divider-copy">
{eyebrow}{title}{subtitle}      </div>
    </header>"""
            slot = "section-motif"
        else:
            header = f"""    <header class="special-header special-header--{kind}" data-scaffold-owned="header">
{eyebrow}{title}{subtitle}    </header>"""
            slot = "cover-visual" if page_type == "cover" else "closing-motif"
        footer = ""
        if plan["show_footer"]:
            footer = f"""
    <footer class="special-footer" data-scaffold-owned="footer">
      <span class="footer-text">{footer_text}</span>
    </footer>"""
        return f"""<section class="{root_classes}" id="slide-{number:02d}" data-slide="{number:02d}" data-page-type="{page_type}" data-frame="special" data-page-family="{family}" data-special-layout="{special_layout}" data-canvas-variant="special">
  <div class="special-background" data-slide-owned="background" aria-hidden="true"></div>
  <div class="special-overlay" data-slide-owned="overlay" aria-hidden="true"></div>
  <div class="special-safe">
{header}
    <main class="special-body">
      <div class="special-stage" data-slide-owned="body">
      <!-- SLIDE_AGENT_FILL_START -->
        <div class="slot slot-{slot}" data-slot="{slot}"></div>
      <!-- SLIDE_AGENT_FILL_END -->
      </div>
    </main>{footer}
  </div>
</section>
"""
    footer = ""
    if plan["show_footer"]:
        footer = f"""
      <footer class="page-footer" data-shared-footer="true" data-scaffold-owned="footer">
        <span class="footer-text">{footer_text}</span>
        <span class="page-no">{number:02d} / {total:02d}</span>
      </footer>"""
    root_classes = (
        f"slide frame-content page-{page_type} family-{family} "
        f"composition-{plan['composition']} "
        f"canvas-variant--{plan['canvas_variant']} "
        f"title-{_copy_shape(plan['title'])}"
    )
    composition = _composition_skeleton(plan["composition"])
    return f"""<section class="{root_classes}" id="slide-{number:02d}" data-slide="{number:02d}" data-page-type="{page_type}" data-frame="content" data-page-family="{family}" data-composition="{plan['composition']}" data-canvas-variant="{plan['canvas_variant']}">
  <div class="slide-inner">
    <div class="page-frame page-frame--content">
      <header class="page-header page-header--content" data-scaffold-owned="header">
{eyebrow}{title}{subtitle}      </header>
      <main class="page-body">
        <div class="content-stage" data-slide-owned="body">
        <!-- SLIDE_AGENT_FILL_START -->
{composition}
        <!-- SLIDE_AGENT_FILL_END -->
        </div>
      </main>{footer}
    </div>
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
    body = re.sub(
        r'\s*<div class="slot slot-[a-z0-9-]+" data-slot="[a-z0-9-]+"></div>\s*',
        "",
        match.group(1),
    )
    body = re.sub(
        r"</?div\b[^>]*\bclass=[\"'][^\"']*\bcomposition(?:__"
        r"(?:primary|secondary)|--[a-z0-9-]+)?\b[^\"']*[\"'][^>]*>",
        "",
        body,
        flags=re.I,
    )
    return not body.strip()


def sync_speech(root: Path, expected: int | None = None) -> None:
    deck, plans = _load_plans(root, expected)
    heading = "# 演讲备注" if deck["language"] == "zh" else "# Speaker notes"
    rows = [heading, ""]
    for plan in plans:
        rows.extend(
            [
                f"## Slide {plan['number']:02d} — {plan['title']}",
                "",
                plan["speech"].strip(),
                "",
            ]
        )
        if plan["evidence"].strip():
            rows.extend(
                [
                    "### Evidence and sources",
                    "",
                    plan["evidence"].strip(),
                    "",
                ]
            )
    atomic_write_text(root, root / "speech.md", "\n".join(rows).rstrip() + "\n")
    print("status:PASS")
    print(f"speech:{len(plans)} pages")


def scaffold_from_plans(
    root: Path,
    expected: int | None = None,
    *,
    force: bool = False,
) -> list[int]:
    deck, plans = _load_plans(root, expected)
    if force:
        authored: list[str] = []
        for plan in plans:
            path = root / "slides" / f"slide_{plan['number']:02d}.html"
            if not path.is_file():
                continue
            try:
                if not _skeleton_is_unfilled(path.read_text(encoding="utf-8")):
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
    _apply_theme_tokens(root, deck)
    written: list[str] = []
    preserved: list[str] = []
    for plan in plans:
        path = root / "slides" / f"slide_{plan['number']:02d}.html"
        if path.exists() and not force:
            preserved.append(path.name)
            continue
        atomic_write_text(root, path, _slide_skeleton(plan, deck))
        written.append(path.name)
    sync_speech(root, expected)
    print("status:PASS")
    print(f"scaffolded:{len(written)} preserved:{len(preserved)}")
    if written:
        print("written:", ", ".join(written))
    return [plan["number"] for plan in plans]


def _css_properties(declarations: str) -> set[str]:
    return {
        match.group(1).lower()
        for match in re.finditer(r"(?:^|;)\s*([\w-]+)\s*:", declarations)
    }


def _selector_subject(selector: str) -> str:
    branch = selector.split(",")[-1].strip()
    parts = [item for item in re.split(r"(?:\s+|[>+~])", branch) if item]
    return parts[-1] if parts else ""


def _subject_has_class(subject: str, class_name: str) -> bool:
    return bool(re.search(rf"\.{re.escape(class_name)}(?![\w-])", subject))


def _validate_page_css(path: Path, text: str, plan: dict) -> None:
    for style in re.findall(r"<style\b[^>]*>(.*?)</style>", text, flags=re.I | re.S):
        clean = re.sub(r"/\*.*?\*/", "", style, flags=re.S)
        for selector, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", clean):
            selector = selector.strip()
            subject = _selector_subject(selector)
            if "::" in subject:
                continue
            properties = _css_properties(declarations)
            for class_name in STRUCTURAL_SUBJECTS:
                if not _subject_has_class(subject, class_name):
                    continue
                blocked = sorted(properties & STRUCTURAL_LAYOUT_PROPERTIES)
                if class_name in {"special-background", "special-overlay"}:
                    blocked = []
                if blocked:
                    raise ValueError(
                        f"{path.name} must not move shared `{selector}`; "
                        f"blocked properties: {', '.join(blocked)}"
                    )
            if plan["page_type"] in SPECIAL_TYPES:
                if _subject_has_class(subject, "special-safe") and (
                    properties & BACKGROUND_PROPERTIES
                ):
                    raise ValueError(
                        f"{path.name} must paint the full canvas on "
                        ".special-background or .special-overlay, not .special-safe"
                    )
                continue
            root_subject = (
                _subject_has_class(subject, "slide")
                or _subject_has_class(subject, "slide-inner")
                or _subject_has_class(subject, "page-frame")
                or _subject_has_class(subject, "page-frame--content")
            )
            if not root_subject or not (properties & BACKGROUND_PROPERTIES):
                continue
            planned_class = f"canvas-variant--{plan['canvas_variant']}"
            allowed_variant = (
                plan["canvas_variant"] != "base"
                and _subject_has_class(subject, planned_class)
                and _subject_has_class(subject, "slide")
            )
            if not allowed_variant:
                raise ValueError(
                    f"{path.name} changes the deck-owned content canvas in "
                    f"`{selector}`; keep backgrounds local, or declare a purposeful "
                    "canvas_variant in the page plan and target that root class"
                )


def _attribute(tag: str, name: str) -> str | None:
    match = re.search(
        rf"\b{re.escape(name)}=[\"']([^\"']*)[\"']",
        tag,
        flags=re.I,
    )
    return match.group(1) if match else None


def _normalized_block(text: str, tag: str) -> str:
    match = re.search(rf"<{tag}\b.*?</{tag}>", text, flags=re.I | re.S)
    if not match:
        return ""
    return re.sub(r">\s+<", "><", match.group(0).strip())


def _inline_copy_text(value: str) -> str | None:
    for tag in re.findall(r"<[^>]+>", value):
        if re.fullmatch(r"</span\s*>", tag, flags=re.I):
            continue
        if re.fullmatch(
            r"<span(?:\s+class=[\"'][A-Za-z0-9 _-]+[\"'])?\s*>",
            tag,
            flags=re.I,
        ):
            continue
        if re.fullmatch(r"<br\s*/?>", tag, flags=re.I):
            continue
        return None
    plain = re.sub(r"<br\s*/?>", " ", value, flags=re.I)
    plain = re.sub(r"</?span\b[^>]*>", "", plain, flags=re.I)
    return re.sub(r"\s+", " ", html.unescape(plain)).strip()


def _header_copy_compatible(text: str, expected: str) -> bool:
    current = re.search(r"<header\b.*?</header>", text, flags=re.I | re.S)
    canonical = re.search(r"<header\b.*?</header>", expected, flags=re.I | re.S)
    if not current or not canonical:
        return False
    current_block = current.group(0).strip()
    canonical_block = canonical.group(0).strip()
    for tag, class_name in (("h1", "page-title"), ("p", "page-subtitle")):
        pattern = re.compile(
            rf"(<{tag}\b(?=[^>]*class=[\"'][^\"']*\b{class_name}\b"
            rf"[^\"']*[\"'])[^>]*>)(.*?)(</{tag}>)",
            flags=re.I | re.S,
        )
        left = pattern.search(current_block)
        right = pattern.search(canonical_block)
        if bool(left) != bool(right):
            return False
        if not left:
            continue
        left_text = _inline_copy_text(left.group(2))
        right_text = _inline_copy_text(right.group(2))
        if left_text is None or left_text != right_text:
            return False
        current_block = (
            current_block[: left.start(2)]
            + "__LOCKED_COPY__"
            + current_block[left.end(2):]
        )
        right = pattern.search(canonical_block)
        canonical_block = (
            canonical_block[: right.start(2)]
            + "__LOCKED_COPY__"
            + canonical_block[right.end(2):]
        )
    normalize = lambda value: re.sub(r">\s+<", "><", value.strip())
    return normalize(current_block) == normalize(canonical_block)


def _validate_fragment(
    path: Path,
    number: int,
    text: str,
    plan: dict,
    deck: dict,
) -> None:
    if re.search(r"<script\b", text, flags=re.I):
        raise ValueError(f"{path.name} must not contain scripts")
    if text.count("<section") != 1 or text.count("</section>") != 1:
        raise ValueError(f"{path.name} must contain exactly one section fragment")
    opening_match = re.search(r"<section\b[^>]*>", text, flags=re.I)
    if not opening_match:
        raise ValueError(f"{path.name} is missing its root section")
    opening = opening_match.group(0)
    expected_attrs = {
        "id": f"slide-{number:02d}",
        "data-slide": f"{number:02d}",
        "data-page-type": plan["page_type"],
        "data-page-family": plan["page_family"],
        "data-frame": "special" if plan["page_type"] in SPECIAL_TYPES else "content",
        "data-canvas-variant": (
            "special" if plan["page_type"] in SPECIAL_TYPES else plan["canvas_variant"]
        ),
    }
    if plan["page_type"] in SPECIAL_TYPES:
        expected_attrs["data-special-layout"] = plan["special_layout"]
    else:
        expected_attrs["data-composition"] = plan["composition"]
    for name, expected in expected_attrs.items():
        actual = _attribute(opening, name)
        if actual != expected:
            raise ValueError(
                f"{path.name} changed scaffold-owned {name}: "
                f"expected {expected!r}, found {actual!r}"
            )
    classes = set((_attribute(opening, "class") or "").split())
    expected_classes = {"slide", f"page-{plan['page_type']}", f"family-{plan['page_family']}"}
    if plan["page_type"] in SPECIAL_TYPES:
        expected_classes.update(
            {"special-canvas", f"layout-{plan['special_layout']}"}
        )
    else:
        expected_classes.update(
            {
                "frame-content",
                f"composition-{plan['composition']}",
                f"canvas-variant--{plan['canvas_variant']}",
            }
        )
    if not expected_classes.issubset(classes):
        raise ValueError(f"{path.name} changed scaffold-owned root classes")
    expected = _slide_skeleton(plan, deck)
    if not _header_copy_compatible(text, expected):
        raise ValueError(f"{path.name} changed locked title copy or header structure")
    expected_footer_count = 1 if plan["show_footer"] else 0
    actual_footer_count = len(re.findall(r"<footer\b", text, flags=re.I))
    if actual_footer_count != expected_footer_count:
        raise ValueError(
            f"{path.name} must contain {expected_footer_count} footer element(s), "
            f"found {actual_footer_count}"
        )
    if _normalized_block(text, "footer") != _normalized_block(expected, "footer"):
        raise ValueError(f"{path.name} changed scaffold-owned footer")
    if text.count("<!-- SLIDE_AGENT_FILL_START -->") != 1 or (
        text.count("<!-- SLIDE_AGENT_FILL_END -->") != 1
    ):
        raise ValueError(f"{path.name} must preserve one pair of fill markers")
    if plan["page_type"] in SPECIAL_TYPES:
        for class_name in ("special-background", "special-overlay", "special-safe"):
            if len(
                re.findall(
                    rf'class=["\'][^"\']*\b{class_name}\b[^"\']*["\']',
                    text,
                )
            ) != 1:
                raise ValueError(f"{path.name} must preserve one .{class_name}")
        for forbidden in ("slide-inner", "page-frame", "page-body"):
            if re.search(
                rf'class=["\'][^"\']*\b{forbidden}\b[^"\']*["\']',
                text,
            ):
                raise ValueError(
                    f"{path.name} special page must not reuse content .{forbidden}"
                )
    else:
        for class_name in ("slide-inner", "page-frame", "page-body", "content-stage"):
            if len(
                re.findall(
                    rf'class=["\'][^"\']*\b{class_name}\b[^"\']*["\']',
                    text,
                )
            ) != 1:
                raise ValueError(f"{path.name} must preserve one .{class_name}")
        if "special-canvas" in classes:
            raise ValueError(f"{path.name} content page must not use special-canvas")
    url_scan_text = re.sub(
        r"<style\b.*?</style>",
        "",
        text,
        flags=re.I | re.S,
    )
    url_scan_text = re.sub(
        r"""\s+xmlns(?:\:[A-Za-z_][\w.-]*)?\s*=\s*(["'])https?://[^"']+\1""",
        "",
        url_scan_text,
        flags=re.I,
    )
    if re.search(r"https?://", url_scan_text):
        raise ValueError(
            f"{path.name} contains a visible/external URL; keep full sources in speech.md"
        )
    if re.search(
        r"""(?:src|href|poster)\s*=\s*["'](?:\.?/)*inputs/"""
        r"""|url\(\s*["']?(?:\.?/)*inputs/""",
        text,
        flags=re.I,
    ):
        raise ValueError(
            f"{path.name} references attachment-derived pixels under inputs/; "
            "use an audited assets/ catalog entry (including kind: material) instead"
        )
    if re.search(
        r"""(?:src|href|poster)\s*=\s*["'](?:\.\./)+assets/"""
        r"""|url\(\s*["']?(?:\.\./)+assets/""",
        text,
        flags=re.I,
    ):
        raise ValueError(
            f"{path.name} uses ../assets; copy the workspace-root-relative "
            "assets/NAME path from assets/catalog.md exactly"
        )
    _validate_page_css(path, text, plan)


def _fragments(
    root: Path,
    *,
    expected: int | None = None,
    only_page: int | None = None,
) -> tuple[dict, list[dict], list[tuple[int, str]]]:
    deck, plans = _load_plans(root, expected)
    plan_map = {plan["number"]: plan for plan in plans}
    selected = [only_page] if only_page is not None else list(plan_map)
    rows: list[tuple[int, str]] = []
    for number in selected:
        path = root / "slides" / f"slide_{number:02d}.html"
        if not path.is_file():
            raise FileNotFoundError(f"{path.name} is missing")
        text = path.read_text(encoding="utf-8").strip()
        _validate_fragment(path, number, text, plan_map[number], deck)
        if _skeleton_is_unfilled(text):
            raise ValueError(
                f"slide_{number:02d}.html is still an empty scaffold; complete "
                "the page body before build or render"
            )
        rows.append((number, text))
    return deck, plans, rows


def _parse_catalog(root: Path) -> list[dict]:
    path = root / "assets" / "catalog.md"
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    matches = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", text))
    entries: list[dict] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        values = _bullet_values(text[match.end():end])
        asset_id = _token(match.group(1), field="asset id")
        if not values.get("path"):
            raise ValueError(f"catalog asset {asset_id!r} is missing path")
        kind = values.get("kind", "").lower()
        if kind not in ASSET_KINDS:
            raise ValueError(
                f"catalog asset {asset_id!r} kind must be one of {sorted(ASSET_KINDS)}"
            )
        if kind == "material":
            registry_path = root / "_trace" / "material-figures.json"
            try:
                registry = json.loads(registry_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                registry = {}
            registered = registry.get("assets") if isinstance(registry, dict) else None
            if not isinstance(registered, dict) or values["path"] not in registered:
                raise ValueError(
                    f"catalog material asset {asset_id!r} was not produced by "
                    f"image.py crop-material: {values['path']}"
                )
        if kind == "user":
            registry_path = root / "_trace" / "user-images.json"
            try:
                registry = json.loads(registry_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                registry = {}
            registered = registry.get("assets") if isinstance(registry, dict) else None
            if not isinstance(registered, dict) or values["path"] not in registered:
                raise ValueError(
                    f"catalog user asset {asset_id!r} was not produced by "
                    f"image.py register-user: {values['path']}"
                )
        slide_values = re.findall(r"\d+", values.get("slides", ""))
        slides = sorted({int(value) for value in slide_values})
        if not slides:
            raise ValueError(f"catalog asset {asset_id!r} is missing slides")
        entries.append(
            {
                "id": asset_id,
                "path": values["path"],
                "kind": kind,
                "slides": slides,
                "source": values.get("source", ""),
                "download": values.get("download", ""),
                "purpose": values.get("purpose", ""),
                "crop": values.get("crop", "page-specific"),
                "display": values.get("display", "").strip().lower(),
                "quality_intent": values.get("quality_intent", "").strip().lower(),
                "expect_transparent": _bool(
                    values.get("expect_transparent"),
                    default=False,
                ),
            }
        )
    return entries


def _asset_path(root: Path, value: str) -> Path:
    path = (root / value).resolve()
    assets = (root / "assets").resolve()
    if path.parent != assets:
        raise ValueError(f"asset must be directly under assets/: {value}")
    return path


def _download_image_asset(root: Path, entry: dict, replace: bool) -> tuple[str, str]:
    target = _asset_path(root, entry["path"])
    if target.is_file() and target.stat().st_size > 0 and not replace:
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

    with requests.get(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 CleanPresentationImage/1.0",
            "Referer": url,
        },
        stream=True,
        timeout=(15, 60),
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

    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = 200_000_000
    try:
        with Image.open(BytesIO(payload)) as source:
            image = ImageOps.exif_transpose(source).copy()
    except Exception as exc:
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
    return entry["id"], "downloaded"


def fetch_images(root: Path, *, replace: bool = False) -> None:
    entries = _parse_catalog(root)
    real_entries = [entry for entry in entries if entry["kind"] == "real"]
    pending = [
        entry
        for entry in real_entries
        if replace
        or not _asset_path(root, entry["path"]).is_file()
        or _asset_path(root, entry["path"]).stat().st_size == 0
    ]
    missing = [entry["id"] for entry in pending if not entry["download"]]
    if missing:
        raise ValueError(
            "real catalog entries need a direct `download` URL before image.py fetch: "
            + ", ".join(missing)
        )

    results: list[tuple[str, str]] = []
    errors: list[str] = []
    workers = min(ASSET_DOWNLOAD_WORKERS, max(1, len(pending)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_download_image_asset, root, entry, replace): entry
            for entry in pending
        }
        for future in as_completed(futures):
            entry = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                errors.append(f"{entry['id']}: {exc}")
    if errors:
        raise ValueError("asset download failed:\n- " + "\n- ".join(sorted(errors)))

    reused = len(real_entries) - len(pending)
    downloaded = sum(status == "downloaded" for _, status in results)
    print("status:PASS")
    print(f"downloaded:{downloaded}")
    print(f"reused:{reused}")
    for asset_id, status in sorted(results):
        print(f"{asset_id}:{status}")


def _validate_asset_requirements(
    root: Path,
    plans: list[dict],
    fragments: list[tuple[int, str]] | None = None,
) -> list[dict]:
    entries = _parse_catalog(root)
    by_page: dict[int, list[dict]] = {}
    for entry in entries:
        path = _asset_path(root, entry["path"])
        if not path.is_file() or path.stat().st_size == 0:
            raise FileNotFoundError(f"catalog asset is missing or empty: {entry['path']}")
        if entry["kind"] in {"real", "user"} and not entry["source"]:
            raise ValueError(f"{entry['kind']} asset {entry['id']!r} needs a source")
        for number in entry["slides"]:
            by_page.setdefault(number, []).append(entry)
    fragment_map = dict(fragments or [])
    plan_numbers = {plan["number"] for plan in plans}
    for entry in entries:
        invalid_pages = sorted(set(entry["slides"]) - plan_numbers)
        if invalid_pages:
            raise ValueError(
                f"catalog asset {entry['id']!r} assigns unknown slides: "
                + ", ".join(str(number) for number in invalid_pages)
            )
        if fragments is None:
            continue
        for number in entry["slides"]:
            references = set(
                re.findall(
                    r"""(?:src|href|poster)\s*=\s*["']([^"']+)["']"""
                    r"""|url\(\s*["']?([^"'()]+)["']?\s*\)""",
                    fragment_map[number],
                    flags=re.I,
                )
            )
            flattened = {
                value.strip()
                for pair in references
                for value in pair
                if value.strip()
            }
            if entry["path"] not in flattened:
                raise ValueError(
                    f"slide {number:02d} is assigned catalog asset "
                    f"{entry['path']!r} but does not reference that exact "
                    "workspace-root-relative path"
                )
    for plan in plans:
        if not plan["needs_bitmap"]:
            continue
        assigned = [
            entry
            for entry in by_page.get(plan["number"], [])
            if Path(entry["path"]).suffix.lower() in RASTER_SUFFIXES
        ]
        if not assigned:
            raise ValueError(
                f"slide {plan['number']:02d} requires a resolved raster asset"
            )
    return entries


def _document(css: str, fragments: list[tuple[int, str]], language: str) -> str:
    # Standalone slides link ../base.css for direct preview. The assembled deck
    # already inlines that stylesheet; retaining the relative link points
    # outside the workspace and creates a false ERR_FILE_NOT_FOUND.
    sections = "\n\n".join(
        re.sub(
            r'''<link\s+rel=["']stylesheet["']\s+href=["']\.\./base\.css["']\s*/?>''',
            "",
            text,
            flags=re.I,
        )
        for _, text in fragments
    )
    html_language = "zh-CN" if language == "zh" else "en"
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
      const scale = Math.min(innerWidth / {CANVAS_W}, innerHeight / {CANVAS_H});
      const deck = document.getElementById('deck');
      deck.style.left = `${{(innerWidth - {CANVAS_W} * scale) / 2}}px`;
      deck.style.top = `${{(innerHeight - {CANVAS_H} * scale) / 2}}px`;
      deck.style.transform = `scale(${{scale}})`;
    }}
    function go(number) {{
      if (!byNumber.has(number)) return;
      current = number;
      slides.forEach(s => s.classList.toggle('active', Number(s.dataset.slide) === current));
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
    css_path = root / "base.css"
    if not css_path.is_file():
        raise FileNotFoundError("base.css is missing; run prepare first")
    deck, plans, fragments = _fragments(root, expected=expected)
    _validate_asset_requirements(root, plans, fragments)
    # Server-side screenshots can see worker-local fonts, while a remote WebUI
    # cannot. Freeze the deck's actual characters and font roles before
    # assembly so the PNG and browser player resolve exactly the same faces.
    bundle_workspace(root)
    font_errors = validate_font_bundle(root)
    if font_errors:
        raise ValueError("portable font audit failed:\n- " + "\n- ".join(font_errors))
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


def _render_input_digest(root: Path, css: str, fragment: str) -> str:
    """Hash HTML/CSS and every local bitmap/font dependency they reference."""
    digest = hashlib.sha256()
    digest.update(css.encode("utf-8"))
    digest.update(b"\0")
    digest.update(fragment.encode("utf-8"))
    combined = css + "\n" + fragment
    refs: set[str] = set()
    for match in re.finditer(
        r"(?:\bsrc|\bhref)\s*=\s*[\"']([^\"']+)[\"']|"
        r"url\(\s*[\"']?([^\"')]+)",
        combined,
        flags=re.I,
    ):
        value = str(match.group(1) or match.group(2) or "").strip()
        value = value.split("#", 1)[0].split("?", 1)[0].lstrip("./")
        if value.startswith(("assets/", "fonts/")):
            refs.add(value)
    for value in sorted(refs):
        path = root / value
        digest.update(b"\0dep:")
        digest.update(value.encode("utf-8"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<missing>")
    return digest.hexdigest()


def _page_digest(root: Path, page: int) -> str:
    css = (root / "base.css").read_text(encoding="utf-8")
    fragment = (root / "slides" / f"slide_{page:02d}.html").read_text(
        encoding="utf-8"
    ).strip()
    return _render_input_digest(root, css, fragment)


def _publish_render_state(
    root: Path,
    page: int,
    path: Path,
    hashes: list[str],
    attempt_hashes: dict[str, list[str]],
    digest: str,
    layout_defects: list[dict] | None = None,
) -> None:
    authoring_hashes = list(dict.fromkeys(
        value
        for attempt, values in attempt_hashes.items()
        if str(attempt).startswith("slide")
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
                "limit_scope": "page_lifecycle",
                "mode": "attempted-filled-html-css",
                "hashes": hashes,
                "attempt_hashes": attempt_hashes,
                "authoring_hashes": authoring_hashes,
                "published_hash": digest,
                "layout_defects": list(layout_defects or []),
                "quality_action_count": len(layout_defects or []),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )


def render(root: Path, page: int, expected: int | None) -> None:
    renderer = Path(__file__).with_name("render_deck.py")
    deck, plans = _load_plans(root, expected)
    plan_map = {plan["number"]: plan for plan in plans}
    if page not in plan_map:
        raise ValueError(f"page {page:02d} is not present in the canonical plans")
    fragment_path = root / "slides" / f"slide_{page:02d}.html"
    if not fragment_path.is_file():
        raise FileNotFoundError(f"{fragment_path.name} is missing")
    fragment = fragment_path.read_text(encoding="utf-8").strip()
    if _skeleton_is_unfilled(fragment):
        raise ValueError(
            f"slide_{page:02d}.html is still an empty scaffold; complete the "
            "main body before the first render"
        )
    # Contract-invalid HTML never reached the renderer and therefore must not
    # consume the finite pixel-refinement budget. Give the Agent the precise
    # schema error first; count only fragments that are actually renderable.
    _validate_fragment(
        fragment_path,
        page,
        fragment,
        plan_map[page],
        deck,
    )
    css = (root / "base.css").read_text(encoding="utf-8")
    digest = _render_input_digest(root, css, fragment)
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
        if str(attempt).startswith("slide")
        for value in values
    ))
    attempt_limit = (
        AUTHORING_PAGE_RENDER_STATES
        if attempt_id.startswith("slide")
        else MAX_PAGE_RENDER_STATES
    )
    final_png = root / "renders" / f"slide_{page:02d}.png"
    is_new_state = digest not in hashes
    used_states = len(authoring_hashes) if attempt_id.startswith("slide") else len(hashes)
    if is_new_state and used_states >= attempt_limit:
        raise ValueError(
            f"page {page:02d} reached the {attempt_limit}-state render "
            "soft stop line for this page lifecycle; a re-dispatch does not reset "
            "the budget. Preserve the last verified pixels and report a repair issue."
        )
    if is_new_state:
        hashes.append(digest)
        current_attempt.append(digest)
        if attempt_id.startswith("slide"):
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
    if digest == published_hash and final_png.is_file():
        print("status:PASS")
        print("render:cached unchanged HTML/CSS")
        print(
            f"render-state:{len(authoring_hashes) if attempt_id.startswith('slide') else len(hashes)}/{attempt_limit} "
            f"scope={'page-lifecycle' if attempt_id.startswith('slide') else 'all-states'} "
            f"attempt={attempt_id} total={len(hashes)}"
        )
        print(
            "layout-defects:"
            + (json.dumps(layout_defects, ensure_ascii=False) if layout_defects else "none")
        )
        print(final_png)
        return
    fragments = [(page, fragment)]
    isolated = root / "renders" / f".page_{page:02d}"
    isolated.mkdir(parents=True, exist_ok=True)
    (isolated / "render.json").unlink(missing_ok=True)
    preview = _document(css, fragments, deck["language"]).replace(
        "<head>", '<head>\n  <base href="../">', 1
    )
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
                str(preview_path),
                str(isolated),
                "--page",
                "1",
            ]
        )
        atomic_copy(root, isolated / "slide_01.png", final_png)
    try:
        isolated_manifest = json.loads(
            (isolated / "render.json").read_text(encoding="utf-8")
        )
        rendered_rows = isolated_manifest.get("pages", [])
        rendered_geometry = (
            rendered_rows[0].get("geometry", {})
            if isinstance(rendered_rows, list)
            and rendered_rows
            and isinstance(rendered_rows[0], dict)
            else {}
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        rendered_geometry = {}
    layout_defects = _page_layout_defects(page, rendered_geometry)
    _publish_render_state(
        root,
        page,
        state_path,
        hashes,
        attempt_hashes,
        digest,
        layout_defects,
    )
    print("status:PASS")
    print(
        f"render-state:{len(authoring_hashes) if attempt_id.startswith('slide') else len(hashes)}/{attempt_limit} "
        f"scope={'page-lifecycle' if attempt_id.startswith('slide') else 'all-states'} "
        f"attempt={attempt_id} total={len(hashes)}"
    )
    print(
        "layout-defects:"
        + (json.dumps(layout_defects, ensure_ascii=False) if layout_defects else "none")
    )
    if layout_defects:
        hints = list(dict.fromkeys(
            str(item.get("ancestor_hint") or item.get("element") or "")
            for item in layout_defects
            if str(item.get("ancestor_hint") or item.get("element") or "").strip()
        ))
        print(
            "layout-repair-order: inspect .page-body/.content-stage and ancestor ownership first"
            + (" (" + "; ".join(hints[:3]) + ")" if hints else "")
            + "; then child grid/flex min-size and alignment; only then reduce content density. "
            "Do not conceal with !important, overflow:hidden, or absolute-positioned body copy."
        )
    print("next: inspect this PNG before any further page edit")
    print(final_png)


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
    for page in pages:
        render(root, page, expected)
    paths = [root / "renders" / f"slide_{page:02d}.png" for page in pages]
    missing = [str(path.relative_to(root)) for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"group render is missing pages: {missing}")
    target = root / "renders" / f"contact-sheet-group-{group_id}.png"
    _contact_sheet(paths, target)
    print("status:PASS")
    print(f"group:{group_id}")
    print("pages:" + ",".join(f"{page:02d}" for page in pages))
    print(target)


def _typography_quality_warnings(root: Path) -> list[str]:
    """Find likely body-copy monospace use as a non-blocking quality signal."""
    warnings: list[str] = []
    paths = [root / "base.css", *sorted((root / "slides").glob("slide_*.html"))]
    body_like = re.compile(
        r"(^|[\s,.#>+~])(body|p|li|td|th|blockquote|copy|body-copy|description|"
        r"summary|subtitle|caption|speaker-copy)([\s,.#:{>+~]|$)",
        re.I,
    )
    intentional = re.compile(
        r"code|pre|mono|tech|identifier|section-number|page-number|page-no|eyebrow|"
        r"kicker|metadata|meta-label|thead|tbody\s+th|row-header|column-header",
        re.I,
    )
    for path in paths:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        rules = re.finditer(
            r"([^{}]+)\{([^{}]*font-family\s*:[^{};]+;?[^{}]*)\}",
            text,
            re.I,
        )
        for match in rules:
            selector, declaration = match.group(1).strip(), match.group(2)
            if not re.search(
                r"--font-mono|ui-monospace|\bmonospace\b", declaration, re.I
            ):
                continue
            if body_like.search(selector) and not intentional.search(selector):
                warnings.append(
                    f"{path.relative_to(root)} likely applies monospace to body copy via "
                    f"selector {selector[:120]!r}; reserve mono for code, IDs, coordinates, "
                    "and compact metadata"
                )
    return warnings


def _anti_slop_warnings(root: Path, deck: dict, plans: list[dict]) -> list[str]:
    """Return high-confidence deck-level template/slop signals without blocking."""
    warnings: list[str] = []
    sections = deck.get("sections", {}) if isinstance(deck.get("sections"), dict) else {}
    visual = "\n".join(
        str(value)
        for key, value in sections.items()
        if str(key).strip().lower() in {
            "visual", "visual contract", "style lock", "视觉契约", "视觉锁",
        }
    ).lower()
    tokens = {str(k): str(v).lower() for k, v in deck.get("tokens", {}).items()}
    token_text = " ".join(tokens.values())
    banned = {
        "#6366f1", "#4f46e5", "#4338ca", "#3730a3",
        "#8b5cf6", "#7c3aed", "#a855f7",
    }
    used_banned = sorted(color for color in banned if color in token_text)
    color_evidence = re.search(
        r"indigo|violet|purple|靛蓝|紫色|品牌色|brand|主题来源|色彩来源",
        visual,
    )
    if used_banned and not color_evidence:
        warnings.append(
            "default indigo/purple token without subject evidence: "
            + ", ".join(used_banned)
        )

    dark_blue = bool(re.search(r"#(?:0[0-9a-f]|1[0-9a-f])(?:1[0-9a-f]|2[0-9a-f])(?:3[0-9a-f]|4[0-9a-f])", token_text))
    warm_gold = bool(re.search(r"#(?:c[0-9a-f]|d[0-9a-f]|e[0-9a-f])(?:8[0-9a-f]|9[0-9a-f]|a[0-9a-f])(?:2[0-9a-f]|3[0-9a-f]|4[0-9a-f]|5[0-9a-f])", token_text))
    gold_evidence = re.search(r"gold|amber|金色|琥珀|金属|奢侈|能源|奖项", visual)
    if dark_blue and warm_gold and not gold_evidence:
        warnings.append(
            "high-risk deep-blue + gold default lacks a subject-derived color rationale"
        )

    canvas_value = tokens.get("--content-canvas", "")
    special_value = tokens.get("--special-bg", "")
    all_base = all(
        plan["page_type"] in SPECIAL_TYPES or plan.get("canvas_variant") == "base"
        for plan in plans
    )
    environment_evidence = re.search(
        r"texture|grain|paper|pattern|environment|motif|vignette|photo|image|"
        r"纹理|颗粒|纸|环境|母题|暗角|照片|图片|背景配方",
        visual,
    )
    topic = f"{deck.get('title', '')} {deck.get('resolved', '')}".lower()
    dark_blue_canvas = False
    color_match = re.fullmatch(r"#([0-9a-f]{6})", canvas_value)
    if color_match:
        value = color_match.group(1)
        red, green, blue = (int(value[index:index + 2], 16) for index in (0, 2, 4))
        dark_blue_canvas = (
            max(red, green, blue) <= 88
            and blue >= red * 1.25
            and blue >= green * 1.08
        )
    subject_blue_evidence = re.search(
        r"ocean|sea|night|sky|space|marine|underwater|blue brand|"
        r"海洋|大海|夜空|天空|太空|水下|蓝色品牌|官方蓝",
        topic,
    )
    if dark_blue_canvas and not subject_blue_evidence:
        warnings.append(
            f"generic dark navy/indigo content canvas {canvas_value} lacks subject-derived "
            "color evidence; vary the environment or choose a palette grounded in the case"
        )
    explicit_uniform_solid = bool(re.search(
        r"纯色|solid(?:-color| color)|全册统一(?:[^。\n]{0,20})(?:底|背景)|"
        r"same background|uniform background",
        visual,
    ))
    if (
        all_base
        and canvas_value
        and (
            explicit_uniform_solid
            or (
                special_value
                and canvas_value == special_value
                and not environment_evidence
            )
        )
    ):
        warnings.append(
            "all content pages share a uniform solid background; add restrained texture, "
            "tonal fields, subject-derived pattern, or image-backed peak states instead of "
            "using one flat canvas everywhere"
        )

    slide_texts: dict[int, str] = {}
    for plan in plans:
        path = root / "slides" / f"slide_{plan['number']:02d}.html"
        try:
            slide_texts[plan["number"]] = path.read_text(
                encoding="utf-8", errors="replace"
            ).lower()
        except OSError:
            continue
    if slide_texts:
        radial_pages = [
            page for page, text in slide_texts.items() if "radial-gradient(" in text
        ]
        if len(radial_pages) >= max(3, math.ceil(len(slide_texts) * 0.5)):
            warnings.append(
                "radial-gradient background treatment repeats across pages "
                + ",".join(f"{page:02d}" for page in radial_pages)
            )
        card_heavy = [
            page for page, text in slide_texts.items()
            if len(re.findall(r'class=["\'][^"\']*\b(?:card|tile|panel)\b', text)) >= 3
        ]
        if len(card_heavy) >= max(3, math.ceil(len(slide_texts) * 0.6)):
            warnings.append(
                "three-or-more repeated card/tile/panel geometries dominate pages "
                + ",".join(f"{page:02d}" for page in card_heavy)
            )
        masked_image_pages = [
            page for page, text in slide_texts.items()
            if "<img" in text
            and len(re.findall(r"rgba\(\s*0\s*,\s*0\s*,\s*0\s*,\s*0\.[6-9]", text)) >= 1
        ]
        bitmap_pages = [plan["number"] for plan in plans if plan.get("needs_bitmap")]
        if bitmap_pages and len(masked_image_pages) == len(bitmap_pages):
            warnings.append(
                "every bitmap page applies a heavy black mask; images may have been "
                "reduced from identity/evidence to generic atmosphere"
            )
    divider_layouts: dict[str, list[int]] = {}
    for plan in plans:
        if plan["page_type"] == "section-divider":
            divider_layouts.setdefault(plan.get("special_layout", ""), []).append(
                plan["number"]
            )
    for layout, pages in divider_layouts.items():
        if len(pages) >= 3:
            warnings.append(
                f"section-divider template {layout!r} repeats on pages {pages}; "
                "verify that the special-page series is not a copied empty shell"
            )
    return warnings


def audit(root: Path) -> None:
    violations = audit_workspace(root)
    if (root / "present.html").is_file():
        violations.extend(validate_font_bundle(root))
    if violations:
        raise ValueError("workspace output audit failed:\n- " + "\n- ".join(violations))
    print("status:PASS")
    print("workspace-output-audit: clean")
    for warning in _typography_quality_warnings(root):
        print(f"[quality-warning] {warning}")
    try:
        deck, plans = _load_plans(root)
    except (OSError, ValueError):
        deck, plans = {}, []
    for warning in _anti_slop_warnings(root, deck, plans) if plans else []:
        print(f"[anti-slop-warning] {warning}")


def clean(root: Path, *, unused_assets: bool = False) -> None:
    moved = clean_workspace(root, unused_assets=unused_assets)
    print("status:PASS")
    print("quarantined:", ", ".join(moved) if moved else "none")


def material_figure(root: Path, source_value: str, output_value: str, box_value: str) -> None:
    """Extract one presentation-usable visual subject from a staged attachment page."""
    from PIL import Image

    source = (root / source_value).resolve()
    output = (root / output_value).resolve()
    inputs_root = (root / "inputs").resolve()
    assets_root = (root / "assets").resolve()
    if inputs_root not in source.parents or not source.name.startswith("page_"):
        raise ValueError("material figure source must be an inputs/**/*.pages/page_NNN.png derivative")
    if assets_root not in output.parents or output.suffix.lower() != ".png":
        raise ValueError("material figure output must be assets/NAME.png")
    if not source.is_file():
        raise ValueError(f"material page is missing: {source_value}")
    try:
        x0, y0, x1, y1 = [float(value.strip()) for value in box_value.split(",")]
    except (TypeError, ValueError):
        raise ValueError("--box must be normalized x0,y0,x1,y1") from None
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        raise ValueError("--box coordinates must satisfy 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1")
    area_ratio = (x1 - x0) * (y1 - y0)
    edge_hits = sum((x0 <= 0.015, y0 <= 0.015, x1 >= 0.985, y1 >= 0.985))
    if area_ratio > 0.72 or edge_hits >= 3:
        raise ValueError(
            "crop is a page facsimile, not a visual subject; tighten the box around the figure"
        )

    with Image.open(source) as opened:
        image = opened.convert("RGB")
    width, height = image.size
    pixels = (
        int(round(x0 * width)),
        int(round(y0 * height)),
        int(round(x1 * width)),
        int(round(y1 * height)),
    )
    crop = image.crop(pixels)
    if crop.width < 480 or crop.height < 240 or crop.width * crop.height < 220_000:
        raise ValueError(
            f"crop resolution {crop.width}x{crop.height} is too small for presentation use"
        )

    layout_path = source.with_suffix(".json")
    text_chars = 0
    text_area = 0.0
    if layout_path.is_file():
        layout = json.loads(layout_path.read_text(encoding="utf-8"))
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
    if text_chars > 600 or text_coverage > 0.34:
        raise ValueError(
            f"crop is text-heavy ({text_chars} native-text chars, {text_coverage:.1%} text area); "
            "crop only the visual subject and leave prose/caption to HTML"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    buffer = BytesIO()
    crop.save(buffer, format="PNG", optimize=True)
    payload = buffer.getvalue()
    atomic_write_bytes(root, output, payload)
    registry_path = root / "_trace" / "material-figures.json"
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        registry = {}
    if not isinstance(registry, dict):
        registry = {}
    registry["schema"] = "mural.material-figures.v1"
    assets = registry.setdefault("assets", {})
    if not isinstance(assets, dict):
        assets = {}
        registry["assets"] = assets
    assets[output_value] = {
        "source": source_value,
        "box": [x0, y0, x1, y1],
        "sha256": hashlib.sha256(payload).hexdigest(),
        "pixels": [crop.width, crop.height],
    }
    atomic_write_text(
        root,
        registry_path,
        json.dumps(registry, ensure_ascii=False, indent=2) + "\n",
    )
    print(json.dumps({
        "status": "PASS",
        "source": source_value,
        "output": output_value,
        "pixels": [crop.width, crop.height],
        "page_area": round(area_ratio, 4),
        "native_text_chars": text_chars,
        "native_text_coverage": round(text_coverage, 4),
    }, ensure_ascii=False))


def register_user_image(root: Path, source_value: str, output_value: str) -> None:
    """Normalize one user-supplied image into an audited local asset."""
    from PIL import Image

    source = (root / source_value).resolve()
    output = (root / output_value).resolve()
    inputs_root = (root / "inputs").resolve()
    assets_root = (root / "assets").resolve()
    if inputs_root not in source.parents:
        raise ValueError("user image source must be under inputs/")
    if assets_root not in output.parents or output.suffix.lower() != ".png":
        raise ValueError("user image output must be assets/NAME.png")
    if not source.is_file() or source.stat().st_size <= 0:
        raise ValueError(f"user image is missing or empty: {source_value}")
    if source.stat().st_size > MAX_ASSET_DOWNLOAD_BYTES:
        raise ValueError("user image exceeds the delivery asset size limit")
    with Image.open(source) as opened:
        opened.load()
        if opened.width < 32 or opened.height < 32:
            raise ValueError(
                f"user image {opened.width}x{opened.height} is too small to be a usable asset"
            )
        has_alpha = "A" in opened.getbands() or "transparency" in opened.info
        image = opened.convert("RGBA" if has_alpha else "RGB")
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    payload = buffer.getvalue()
    atomic_write_bytes(root, output, payload)
    registry_path = root / "_trace" / "user-images.json"
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        registry = {}
    if not isinstance(registry, dict):
        registry = {}
    registry["schema"] = "mural.user-images.v1"
    assets = registry.setdefault("assets", {})
    if not isinstance(assets, dict):
        assets = {}
        registry["assets"] = assets
    assets[output_value] = {
        "source": source_value,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "pixels": [image.width, image.height],
        "has_alpha": has_alpha,
    }
    atomic_write_text(
        root,
        registry_path,
        json.dumps(registry, ensure_ascii=False, indent=2) + "\n",
    )
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


def _asset_visual_quality(path: Path, display: str = "") -> dict:
    """Return advisory presentation-use metadata without rejecting archival art."""
    from PIL import Image, ImageFilter, ImageOps, ImageStat

    Image.MAX_IMAGE_PIXELS = 200_000_000
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        width, height = image.size
        sample = image.convert("L")
        sample.thumbnail((800, 800), Image.Resampling.LANCZOS)
        edge_variance = float(
            ImageStat.Stat(sample.filter(ImageFilter.FIND_EDGES)).var[0]
        )
    full_bleed_scale = max(1600 / max(1, width), 900 / max(1, height))
    reasons: list[str] = []
    if full_bleed_scale > 1.25:
        reasons.append(f"needs {full_bleed_scale:.2f}x upscaling for a 1600x900 canvas")
    if edge_variance < 180:
        reasons.append(f"low visible detail/sharpness score ({edge_variance:.1f})")
    return {
        "width": width,
        "height": height,
        "edge_variance": round(edge_variance, 1),
        "full_bleed_scale": round(full_bleed_scale, 2),
        "full_bleed_ready": not reasons,
        "reasons": reasons,
    }


def _image_sheet(root: Path, rows: list[tuple[str, str]]) -> Path:
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
            preview = source.convert("RGB")
            preview.thumbnail((cell_w - 24, image_h - 24))
        column, row = index % columns, index // columns
        x, y = column * cell_w, row * (image_h + label_h)
        px = x + (cell_w - preview.width) // 2
        py = y + (image_h - preview.height) // 2
        sheet.paste(preview, (px, py))
        draw.rectangle(
            (x, y, x + cell_w - 1, y + image_h + label_h - 1),
            outline=(170, 170, 166),
        )
        draw.text((x + 14, y + image_h + 13), asset_id, fill=(18, 18, 18), font=font)
    target = root / "assets" / "contact-sheet.png"
    buffer = BytesIO()
    sheet.save(buffer, format="PNG")
    atomic_write_bytes(root, target, buffer.getvalue())
    return target


def finalize_assets(root: Path) -> None:
    _, plans = _load_plans(root)
    entries = _validate_asset_requirements(root, plans)
    seen: set[str] = set()
    hashes: list[tuple[str, int]] = []
    warnings: list[str] = []
    rows: list[tuple[str, str]] = []
    catalog = ["# Asset catalog", ""]
    for entry in entries:
        if entry["id"] in seen:
            raise ValueError(f"duplicate asset id: {entry['id']}")
        seen.add(entry["id"])
        path = _asset_path(root, entry["path"])
        if path.suffix.lower() not in RASTER_SUFFIXES:
            raise ValueError(f"asset must be a raster image: {entry['path']}")
        digest = _difference_hash(path)
        display = entry["display"]
        if not display and re.search(
            r"full[- ]?bleed|满幅|满铺|全出血", entry["crop"], re.I
        ):
            display = "full-bleed"
        quality = _asset_visual_quality(path, display)
        if display in {"full-bleed", "bleed", "background"} and not quality["full_bleed_ready"]:
            intent = entry["quality_intent"] or "standard"
            treatment = (
                "keep it documentary: use a bounded editorial frame, contact-sheet crop, "
                "matte/texture treatment, or pair it with typography; do not upscale it as "
                "a clean full-canvas photograph"
                if intent == "archival"
                else "use a bounded crop/texture treatment or acquire a sharper alternative "
                "instead of stretching it across the canvas"
            )
            warnings.append(
                f"{entry['id']} is not full-bleed-ready "
                f"({'; '.join(quality['reasons'])}); {treatment}"
            )
        for prior_id, prior_digest in hashes:
            if (digest ^ prior_digest).bit_count() <= 4:
                warnings.append(f"{entry['id']} is near-duplicate with {prior_id}")
        hashes.append((entry["id"], digest))
        if entry["expect_transparent"] and path.suffix.lower() in {".png", ".webp"}:
            report = inspect_image(root, entry["path"], True)
            if report["status"] != "PASS":
                if report["baked_checkerboard"]:
                    raise ValueError(
                        f"asset {entry['id']!r} contains a baked checkerboard; "
                        f"run remove-checkerboard for {entry['path']}"
                    )
                raise ValueError(
                    f"asset {entry['id']!r} expects transparency but has no useful alpha"
                )
        rows.append((entry["id"], entry["path"]))
        catalog.extend(
            [
                f"## {entry['id']}",
                f"- slides: {', '.join(str(value) for value in entry['slides'])}",
                f"- kind: {entry['kind']}",
                f"- path: {entry['path']}",
                f"- source: {entry['source'] or 'generated/local'}",
                f"- purpose: {entry['purpose'] or 'deck visual'}",
                f"- crop: {entry['crop']}",
                f"- display: {display or 'large'}",
                f"- quality_intent: {entry['quality_intent'] or 'standard'}",
                f"- pixel_size: {quality['width']}x{quality['height']}",
                f"- full_bleed_scale: {quality['full_bleed_scale']}",
                (
                    "- full_bleed_ready: true"
                    if quality["full_bleed_ready"]
                    else "- full_bleed_ready: false"
                ),
                (
                    "- expect_transparent: true"
                    if entry["expect_transparent"]
                    else "- expect_transparent: false"
                ),
                "",
            ]
        )
    atomic_write_text(
        root,
        root / "assets" / "catalog.md",
        "\n".join(catalog).rstrip() + "\n",
    )
    sheet = _image_sheet(root, rows) if rows else None
    pages_with_assets = {
        page
        for entry in entries
        for page in entry["slides"]
    }
    bitmap_ready = [
        plan["number"]
        for plan in plans
        if plan["needs_bitmap"] and plan["number"] in pages_with_assets
    ]
    print("status:PASS")
    print(f"assets:{len(entries)}")
    print(
        "bitmap_ready:"
        + (",".join(f"{page:02d}" for page in bitmap_ready) or "none")
    )
    if sheet:
        print(sheet)
    for warning in warnings:
        print(f"[asset-warning] {warning}")


def _contact_sheet(paths: list[Path], target: Path) -> None:
    from PIL import Image, ImageDraw

    columns = min(4, max(1, len(paths)))
    rows = math.ceil(len(paths) / columns)
    thumb_w, thumb_h = 400, 225
    sheet = Image.new("RGB", (columns * thumb_w, rows * thumb_h), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    for index, path in enumerate(paths):
        with Image.open(path) as source:
            image = source.convert("RGB").resize((thumb_w, thumb_h))
        x = (index % columns) * thumb_w
        y = (index // columns) * thumb_h
        sheet.paste(image, (x, y))
        draw.rectangle((x, y, x + 34, y + 20), fill=(12, 14, 18))
        draw.text((x + 7, y + 4), f"{index + 1:02d}", fill=(245, 247, 250))
    sheet.save(target)


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


_BODY_TEXT_TAGS = {
    "p", "li", "td", "th", "blockquote", "figcaption", "label", "div", "span",
}
_INLINE_BODY_TEXT_CLASS = re.compile(
    r"(?:copy|body|caption|label|title|name|desc|text|head|hint|value|note|"
    r"quote|reply|boundary|field|step|stage|rule|penalty)",
    re.I,
)
_DECORATIVE_TEXT_CLASS = re.compile(
    r"(?:ornament|decor|ghost|watermark|page-number|page-no|eyebrow|kicker|"
    r"section-number|display-only)",
    re.I,
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
    """Return high-confidence, non-blocking page layout defects.

    This deliberately excludes whitespace balance, sparse compositions, image
    overlap, and display typography. Those are too contextual for a
    deterministic gate. The returned evidence drives one bounded page repair
    and Review prioritization; it never changes renderer exit status.
    """
    if not _layout_defects_enabled() or not isinstance(geometry, dict):
        return []
    boxes: list[dict] = []
    for value in geometry.get("text_boxes", []):
        if not isinstance(value, dict):
            continue
        tag = str(value.get("tag") or "").lower()
        class_name = str(value.get("class_name") or "")
        if tag not in _BODY_TEXT_TAGS:
            continue
        if tag in {"div", "span"} and not _INLINE_BODY_TEXT_CLASS.search(class_name):
            continue
        if _DECORATIVE_TEXT_CLASS.search(class_name):
            continue
        boxes.append(value)
    defects: list[dict] = []
    for item in boxes:
        overflow_x = max(0.0, float(item.get("overflow_px_x") or 0))
        overflow_y = max(0.0, float(item.get("overflow_px_y") or 0))
        edge = max(
            0.0,
            -float(item.get("x") or 0),
            -float(item.get("y") or 0),
            float(item.get("x") or 0) + float(item.get("width") or 0) - CANVAS_W,
            float(item.get("y") or 0) + float(item.get("height") or 0) - CANVAS_H,
        )
        if max(overflow_x, overflow_y, edge) <= 8:
            continue
        element = _layout_element(item)
        ancestor = _layout_ancestor_hint(item)
        defects.append({
            "type": "text_overflow",
            "severity": "major",
            "element": element,
            "ancestor_hint": ancestor,
            "overflow_px": {
                "x": round(overflow_x, 1),
                "y": round(overflow_y, 1),
                "canvas_edge": round(edge, 1),
            },
            "text": str(item.get("text") or "")[:96],
            "evidence": (
                f"P{number:02d} {element} exceeds its text/canvas box by more than 8px"
                + (f" under {ancestor}" if ancestor else "")
            ),
        })

    for left_index, left in enumerate(boxes):
        for right in boxes[left_index + 1:]:
            if not _boxes_overlap(left, right):
                continue
            x_overlap = max(
                0.0,
                min(float(left.get("x", 0)) + float(left.get("width", 0)),
                    float(right.get("x", 0)) + float(right.get("width", 0)))
                - max(float(left.get("x", 0)), float(right.get("x", 0))),
            )
            y_overlap = max(
                0.0,
                min(float(left.get("y", 0)) + float(left.get("height", 0)),
                    float(right.get("y", 0)) + float(right.get("height", 0)))
                - max(float(left.get("y", 0)), float(right.get("y", 0))),
            )
            smaller = min(
                float(left.get("width", 0)) * float(left.get("height", 0)),
                float(right.get("width", 0)) * float(right.get("height", 0)),
            )
            ratio = (x_overlap * y_overlap) / smaller if smaller > 0 else 0.0
            if ratio < 0.18:
                continue
            left_element = _layout_element(left)
            right_element = _layout_element(right)
            ancestor = _layout_ancestor_hint(left) or _layout_ancestor_hint(right)
            defects.append({
                "type": "text_collision",
                "severity": "major",
                "elements": [left_element, right_element],
                "ancestor_hint": ancestor,
                "overlap_ratio": round(ratio, 3),
                "text": [
                    str(left.get("text") or "")[:72],
                    str(right.get("text") or "")[:72],
                ],
                "evidence": (
                    f"P{number:02d} body-text leaves {left_element} and {right_element} "
                    f"overlap by {ratio:.0%} of the smaller box"
                    + (f" near {ancestor}" if ancestor else "")
                ),
            })

    blocks = [
        value for value in geometry.get("layout_blocks", [])
        if isinstance(value, dict)
        and str(value.get("parent_key") or "").strip()
        and not _DECORATIVE_TEXT_CLASS.search(str(value.get("class_name") or ""))
    ]
    for item in blocks:
        overflow_x = max(0.0, float(item.get("overflow_px_x") or 0))
        overflow_y = max(0.0, float(item.get("overflow_px_y") or 0))
        edge = max(
            0.0,
            -float(item.get("x") or 0),
            -float(item.get("y") or 0),
            float(item.get("x") or 0) + float(item.get("width") or 0) - CANVAS_W,
            float(item.get("y") or 0) + float(item.get("height") or 0) - CANVAS_H,
        )
        visible_overflow = (
            str(item.get("overflow_mode_x") or "visible") == "visible"
            or str(item.get("overflow_mode_y") or "visible") == "visible"
        )
        if edge <= 8 and (max(overflow_x, overflow_y) <= 16 or not visible_overflow):
            continue
        element = _layout_element(item)
        ancestor = _layout_ancestor_hint(item)
        defects.append({
            "type": "block_overflow",
            "severity": "major",
            "element": element,
            "ancestor_hint": ancestor,
            "overflow_px": {
                "x": round(overflow_x, 1),
                "y": round(overflow_y, 1),
                "canvas_edge": round(edge, 1),
            },
            "evidence": (
                f"P{number:02d} semantic block {element} exceeds its owned box by more than 8px"
                + (f" under {ancestor}" if ancestor else "")
            ),
        })
    for left_index, left in enumerate(blocks):
        for right in blocks[left_index + 1:]:
            if left.get("parent_key") != right.get("parent_key"):
                continue
            if not _boxes_overlap(left, right):
                continue
            x_overlap = max(
                0.0,
                min(float(left.get("x", 0)) + float(left.get("width", 0)),
                    float(right.get("x", 0)) + float(right.get("width", 0)))
                - max(float(left.get("x", 0)), float(right.get("x", 0))),
            )
            y_overlap = max(
                0.0,
                min(float(left.get("y", 0)) + float(left.get("height", 0)),
                    float(right.get("y", 0)) + float(right.get("height", 0)))
                - max(float(left.get("y", 0)), float(right.get("y", 0))),
            )
            smaller = min(
                float(left.get("width", 0)) * float(left.get("height", 0)),
                float(right.get("width", 0)) * float(right.get("height", 0)),
            )
            ratio = (x_overlap * y_overlap) / smaller if smaller > 0 else 0.0
            if ratio < 0.45:
                continue
            left_element = _layout_element(left)
            right_element = _layout_element(right)
            defects.append({
                "type": "block_collision",
                "severity": "major",
                "elements": [left_element, right_element],
                "ancestor_hint": str(left.get("parent_key") or ""),
                "overlap_ratio": round(ratio, 3),
                "text": [
                    str(left.get("text") or "")[:72],
                    str(right.get("text") or "")[:72],
                ],
                "evidence": (
                    f"P{number:02d} sibling semantic blocks {left_element} and "
                    f"{right_element} overlap by {ratio:.0%} of the smaller box"
                ),
            })
    for defect in defects:
        signature = json.dumps(
            {
                "page": number,
                "type": defect.get("type"),
                "element": defect.get("element"),
                "elements": defect.get("elements"),
                "ancestor_hint": defect.get("ancestor_hint"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        defect["id"] = "LAYOUT-" + hashlib.sha256(
            signature.encode("utf-8")
        ).hexdigest()[:12].upper()
    return defects


def _border_signature(path: Path) -> tuple[float, float, float]:
    from PIL import Image

    with Image.open(path) as source:
        image = source.convert("RGB")
        width, height = image.size
        points: list[tuple[int, int]] = []
        for x in range(0, width, 24):
            points.extend(((x, 3), (x, height - 4)))
        for y in range(0, height, 24):
            points.extend(((3, y), (width - 4, y)))
        pixels = [image.getpixel(point) for point in points]
    return tuple(statistics.fmean(pixel[channel] for pixel in pixels) for channel in range(3))


def _special_has_rails(path: Path) -> bool:
    from PIL import Image, ImageStat

    with Image.open(path) as source:
        image = source.convert("RGB")
        width, height = image.size
        border = Image.new("RGB", (width * 2 + height * 2, 16))
        pieces = [
            image.crop((0, 0, width, 16)).resize((width, 16)),
            image.crop((0, height - 16, width, height)).resize((width, 16)),
            image.crop((0, 0, 16, height)).rotate(90, expand=True).resize((height, 16)),
            image.crop((width - 16, 0, width, height)).rotate(90, expand=True).resize((height, 16)),
        ]
        cursor = 0
        for piece in pieces:
            border.paste(piece, (cursor, 0))
            cursor += piece.width
        interior = image.crop((70, 50, width - 70, height - 50)).resize((400, 225))
    border_stat = ImageStat.Stat(border)
    inner_stat = ImageStat.Stat(interior)
    border_std = statistics.fmean(border_stat.stddev)
    border_luma = statistics.fmean(border_stat.mean)
    inner_luma = statistics.fmean(inner_stat.mean)
    border_chroma = max(border_stat.mean) - min(border_stat.mean)
    mean_delta = math.sqrt(
        sum((border_stat.mean[index] - inner_stat.mean[index]) ** 2 for index in range(3))
    )
    # The historical failure is a light content-page paper rail surrounding a
    # dark special-page panel. A uniformly painted special background is valid,
    # so do not reject every quiet edge merely because the center contains a
    # bright Hero.
    return (
        border_std < 8
        and border_luma > 205
        and border_chroma < 20
        and inner_luma < border_luma - 35
        and mean_delta > 42
    )


def _geometry_audit(
    root: Path,
    plans: list[dict],
    rendered: dict,
) -> dict:
    rows = {int(row.get("page", -1)): row for row in rendered.get("pages", [])}
    errors: list[str] = []
    warnings: list[str] = []
    special_pages: list[int] = []
    content_pages: list[int] = []
    content_groups: dict[str, list[tuple[int, tuple[float, float, float]]]] = {}
    content_reference: dict[str, tuple[int, dict]] = {}
    layout_defects: dict[str, list[dict]] = {}

    def geometry(number: int) -> dict:
        value = rows.get(number, {}).get("geometry", {})
        if not isinstance(value, dict) or not value:
            errors.append(f"page {number}: rendered geometry is missing")
            return {}
        return value

    def box(value: dict, name: str) -> dict:
        candidate = value.get("boxes", {}).get(name)
        return candidate if isinstance(candidate, dict) else {}

    for plan in plans:
        number = plan["number"]
        value = geometry(number)
        if not value:
            continue
        text_boxes = [
            box for box in value.get("text_boxes", []) if isinstance(box, dict)
        ]
        page_defects = _page_layout_defects(number, value)
        value["layout_defects"] = page_defects
        if page_defects:
            layout_defects[f"{number:02d}"] = page_defects
            warnings.append(
                f"page {number}: deterministic layout defects: "
                + ", ".join(str(item.get("type")) for item in page_defects)
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
        tiny_boxes = [
            box for box in text_boxes
            if float(box.get("font_size") or 0) < 11.5
            and str(box.get("tag") or "") in {"p", "li", "td", "blockquote", "figcaption"}
            and len(str(box.get("text") or "")) > 24
        ]
        if tiny_boxes:
            warnings.append(
                f"page {number}: long DOM text below 11.5px in "
                + ", ".join(
                    repr(str(box.get("text") or "")[:48]) for box in tiny_boxes[:4]
                )
            )
        collisions: list[tuple[dict, dict]] = []
        for left_index, left in enumerate(text_boxes):
            for right in text_boxes[left_index + 1:]:
                if not _boxes_overlap(left, right):
                    continue
                x_overlap = max(
                    0.0,
                    min(float(left.get("x", 0)) + float(left.get("width", 0)),
                        float(right.get("x", 0)) + float(right.get("width", 0)))
                    - max(float(left.get("x", 0)), float(right.get("x", 0))),
                )
                y_overlap = max(
                    0.0,
                    min(float(left.get("y", 0)) + float(left.get("height", 0)),
                        float(right.get("y", 0)) + float(right.get("height", 0)))
                    - max(float(left.get("y", 0)), float(right.get("y", 0))),
                )
                smaller = min(
                    float(left.get("width", 0)) * float(left.get("height", 0)),
                    float(right.get("width", 0)) * float(right.get("height", 0)),
                )
                if smaller > 0 and (x_overlap * y_overlap) / smaller >= 0.18:
                    collisions.append((left, right))
        if collisions:
            warnings.append(
                f"page {number}: possible DOM text collision between "
                + "; ".join(
                    f"{str(left.get('text') or '')[:28]!r} / {str(right.get('text') or '')[:28]!r}"
                    for left, right in collisions[:4]
                )
            )
        images = [item for item in value.get("images", []) if isinstance(item, dict)]
        if plan.get("needs_bitmap") and images:
            visible_area = sum(
                max(0.0, float(item.get("width", 0)))
                * max(0.0, float(item.get("height", 0)))
                * max(0.0, min(1.0, float(item.get("opacity", 1))))
                for item in images
            )
            if visible_area / (CANVAS_W * CANVAS_H) < 0.035:
                warnings.append(
                    f"page {number}: assigned bitmap occupies under 3.5% of visible canvas area"
                )
        png = root / "renders" / f"slide_{number:02d}.png"
        if png.is_file():
            try:
                from PIL import Image, ImageStat

                with Image.open(png) as opened:
                    gray = opened.convert("L").resize((160, 90))
                    pixels = list(gray.getdata())
                    dark_fraction = sum(pixel < 18 for pixel in pixels) / len(pixels)
                    deviation = float(ImageStat.Stat(gray).stddev[0])
                if dark_fraction > 0.78 and deviation < 24:
                    warnings.append(
                        f"page {number}: {dark_fraction:.0%} near-black pixels with low "
                        f"texture variance ({deviation:.1f}); inspect for an excessive mask/black field"
                    )
            except (OSError, ValueError):
                pass
        if value.get("page_family") != plan["page_family"]:
            errors.append(f"page {number}: page-family drift")
        png = root / "renders" / f"slide_{number:02d}.png"
        if plan["page_type"] in SPECIAL_TYPES:
            special_pages.append(number)
            if value.get("special_layout") != plan["special_layout"]:
                errors.append(f"page {number}: special-layout drift")
            if value.get("frame") != "special":
                errors.append(f"page {number}: special page uses a content frame")
            for name in ("special_background", "special_overlay"):
                candidate = box(value, name)
                if not candidate or any(
                    abs(float(candidate.get(key, 0)) - expected) > 1
                    for key, expected in (
                        ("x", 0),
                        ("y", 0),
                        ("width", CANVAS_W),
                        ("height", CANVAS_H),
                    )
                ):
                    errors.append(f"page {number}: {name} does not cover 1600×900")
            safe = box(value, "special_safe")
            if not safe or (
                float(safe.get("x", 0)) < 60
                or float(safe.get("y", 0)) < 40
                or float(safe.get("x", 0)) + float(safe.get("width", 0)) > 1540
                or float(safe.get("y", 0)) + float(safe.get("height", 0)) > 860
            ):
                errors.append(f"page {number}: special-safe leaves the text safe zone")
            title = box(value, "title")
            if title and (
                float(title.get("x", 0)) < 55
                or float(title.get("y", 0)) < 35
                or float(title.get("x", 0)) + float(title.get("width", 0)) > 1545
                or float(title.get("y", 0)) + float(title.get("height", 0)) > 865
            ):
                errors.append(f"page {number}: special-page title leaves the safe zone")
            section_number = box(value, "section_number")
            divider_copy = box(value, "divider_copy")
            if section_number and divider_copy and _boxes_overlap(
                section_number, divider_copy
            ):
                errors.append(f"page {number}: section-number overlaps divider-copy")
            if png.is_file() and _special_has_rails(png):
                errors.append(
                    f"page {number}: pixel audit found a uniform outer rail around "
                    "the special canvas"
                )
            continue

        content_pages.append(number)
        if value.get("frame") != "content":
            errors.append(f"page {number}: content page uses a special frame")
        header = box(value, "header")
        if plan["show_footer"]:
            footer = box(value, "footer")
            if not footer or float(footer.get("y", 0)) < 790:
                errors.append(f"page {number}: content footer is missing or misplaced")
        if plan["canvas_variant"] not in content_reference:
            content_reference[plan["canvas_variant"]] = (number, header)
        else:
            reference_number, reference = content_reference[plan["canvas_variant"]]
            if header and reference:
                delta = max(
                    abs(float(header.get(key, 0)) - float(reference.get(key, 0)))
                    for key in ("x", "y", "width", "height")
                )
                if delta > 2:
                    errors.append(
                        f"pages {reference_number}/{number}: content header geometry "
                        f"differs by {delta:.2f}px"
                    )
        if png.is_file():
            content_groups.setdefault(plan["canvas_variant"], []).append(
                (number, _border_signature(png))
            )

    for variant, samples in content_groups.items():
        if len(samples) < 2:
            continue
        reference_number, reference = samples[0]
        for number, signature in samples[1:]:
            delta = math.sqrt(
                sum((reference[index] - signature[index]) ** 2 for index in range(3))
            )
            if delta > 26:
                errors.append(
                    f"pages {reference_number}/{number}: content canvas variant "
                    f"{variant!r} drifts at the outer edge ({delta:.1f})"
                )
    return {
        "status": "FAIL" if errors else "PASS",
        "special_pages": special_pages,
        "content_pages": content_pages,
        "errors": errors,
        "warnings": warnings,
        "layout_defects": layout_defects,
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
        if page in rows:
            continue
        isolated_path = root / "renders" / f".page_{page:02d}" / "render.json"
        try:
            isolated = json.loads(isolated_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        candidates = isolated.get("pages") or []
        if not candidates or not isinstance(candidates[0], dict):
            continue
        row = dict(candidates[0])
        row["page"] = page
        row["png"] = str(root / "renders" / f"slide_{page:02d}.png")
        rows[page] = row
        errors.extend(str(item) for item in isolated.get("console_errors") or [])
    ordered = [rows[page] for page in numbers if page in rows]
    return {
        "w": CANVAS_W,
        "h": CANVAS_H,
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
    assert_workspace_clean(root)
    deck, plans = _load_plans(root, expected)
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
        _run_renderer(
            [
                sys.executable,
                str(renderer),
                str(root / "present.html"),
                str(root / "renders"),
                "--page",
                str(page),
            ]
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
    manifest["anti_slop_warnings"] = _anti_slop_warnings(root, deck, plans)
    manifest["special_page_geometry"] = geometry
    manifest["layout_defects"] = geometry.get("layout_defects", {})
    atomic_write_text(
        root,
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    if geometry["errors"]:
        raise ValueError(
            "layout/canvas audit failed:\n- " + "\n- ".join(geometry["errors"])
        )
    for warning in geometry["warnings"]:
        print(f"[deterministic-visual-warning] {warning}")
    for warning in manifest["anti_slop_warnings"]:
        print(f"[anti-slop-warning] {warning}")
    page_paths = [root / "renders" / f"slide_{page:02d}.png" for page in numbers]
    contact_sheet = root / "renders" / "contact-sheet.png"
    _contact_sheet(page_paths, contact_sheet)
    special_paths = [
        root / "renders" / f"slide_{plan['number']:02d}.png"
        for plan in plans if plan["page_type"] in SPECIAL_TYPES
    ]
    if special_paths:
        _contact_sheet(
            special_paths,
            root / "renders" / "contact-sheet-special.png",
        )
    if not contact_sheet.is_file():
        raise FileNotFoundError("renders/contact-sheet.png is missing")
    assert_workspace_clean(root)
    print(
        "[workflow] Review owns the final pixel decision; inspect the whole-deck "
        "and special-page contact sheets once, then open only flagged pages."
    )
    print(
        "incremental-finalize: rendered="
        + (",".join(f"{page:02d}" for page in changed_pages) or "none")
        + " reused="
        + (",".join(f"{page:02d}" for page in numbers if page not in changed_pages) or "none")
    )
    print(contact_sheet)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p_prepare = sub.add_parser("prepare")
    p_prepare.add_argument("root")
    p_restore = sub.add_parser("restore-base")
    p_restore.add_argument("root")
    p_plan_batch = sub.add_parser("apply-plan-batch")
    p_plan_batch.add_argument("root")
    p_validate = sub.add_parser("validate-plans")
    p_validate.add_argument("root")
    p_validate.add_argument("--expected", type=int)
    p_scaffold = sub.add_parser("scaffold-from-plans")
    p_scaffold.add_argument("root")
    p_scaffold.add_argument("--expected", type=int)
    p_scaffold.add_argument("--force", action="store_true")
    p_speech = sub.add_parser("sync-speech")
    p_speech.add_argument("root")
    p_speech.add_argument("--expected", type=int)
    p_build = sub.add_parser("build")
    p_build.add_argument("root")
    p_build.add_argument("--expected", type=int)
    p_render = sub.add_parser("render")
    p_render.add_argument("root")
    p_render.add_argument("--page", type=int, required=True)
    p_render.add_argument("--expected", type=int)
    p_render_group = sub.add_parser("render-group")
    p_render_group.add_argument("root")
    p_render_group.add_argument("--group", required=True)
    p_render_group.add_argument("--pages", required=True)
    p_render_group.add_argument("--expected", type=int)
    p_fetch = sub.add_parser("fetch-images")
    p_fetch.add_argument("root")
    p_fetch.add_argument("--replace", action="store_true")
    p_material_figure = sub.add_parser("material-figure")
    p_material_figure.add_argument("root")
    p_material_figure.add_argument("--source", required=True)
    p_material_figure.add_argument("--output", required=True)
    p_material_figure.add_argument("--box", required=True)
    p_user_image = sub.add_parser("register-user-image")
    p_user_image.add_argument("root")
    p_user_image.add_argument("--source", required=True)
    p_user_image.add_argument("--output", required=True)
    p_clean = sub.add_parser("clean")
    p_clean.add_argument("root")
    p_clean.add_argument("--unused-assets", action="store_true")
    p_audit = sub.add_parser("audit")
    p_audit.add_argument("root")
    p_audit.add_argument("--expected", type=int)
    p_inspect = sub.add_parser("inspect-image")
    p_inspect.add_argument("root")
    p_inspect.add_argument("--asset", required=True)
    p_inspect.add_argument("--expect-transparent", action="store_true")
    p_checker = sub.add_parser("remove-checkerboard")
    p_checker.add_argument("root")
    p_checker.add_argument("--asset", required=True)
    p_assets = sub.add_parser("assets-finalize")
    p_assets.add_argument("root")
    p_finalize = sub.add_parser("finalize")
    p_finalize.add_argument("root")
    p_finalize.add_argument("--expected", type=int)
    args = parser.parse_args()
    root = _root(args.root)
    if args.command == "prepare":
        prepare(root)
    elif args.command == "restore-base":
        restore_base(root)
    elif args.command == "apply-plan-batch":
        apply_plan_batch(root)
    elif args.command == "validate-plans":
        validate_plans(root, args.expected)
    elif args.command == "scaffold-from-plans":
        scaffold_from_plans(root, args.expected, force=args.force)
    elif args.command == "sync-speech":
        sync_speech(root, args.expected)
    elif args.command == "build":
        build(root, args.expected)
    elif args.command == "render":
        render(root, args.page, args.expected)
    elif args.command == "render-group":
        render_group(root, args.group, args.pages, args.expected)
    elif args.command == "fetch-images":
        fetch_images(root, replace=args.replace)
    elif args.command == "material-figure":
        material_figure(root, args.source, args.output, args.box)
    elif args.command == "register-user-image":
        register_user_image(root, args.source, args.output)
    elif args.command == "clean":
        clean(root, unused_assets=args.unused_assets)
    elif args.command == "audit":
        audit(root)
    elif args.command == "inspect-image":
        print_image_report(inspect_image(root, args.asset, args.expect_transparent))
    elif args.command == "remove-checkerboard":
        print_image_report(remove_checkerboard(root, args.asset))
    elif args.command == "assets-finalize":
        finalize_assets(root)
    else:
        finalize(root, args.expected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
