#!/usr/bin/env python3
"""Build, validate, render, and audit a Static HTML Presentation workspace.

Canonical planning files:
  plan/deck.md
  plan/slide_01.md ... plan/slide_NN.md

Public commands:
  deck.py prepare ROOT
  deck.py restore-base ROOT
  deck.py validate-plans ROOT [--expected N]
  deck.py scaffold-from-plans ROOT [--expected N] [--force]
  deck.py sync-speech ROOT [--expected N]
  deck.py build ROOT [--expected N]
  deck.py render ROOT --page NN
  deck.py fetch-images ROOT [--replace]
  deck.py assets-finalize ROOT
  deck.py inspect-image ROOT --asset PATH [--expect-transparent]
  deck.py remove-checkerboard ROOT --asset PATH
  deck.py clean ROOT [--unused-assets]
  deck.py audit ROOT
  deck.py finalize ROOT [--expected N]

The script is a deterministic guard and renderer. It applies the composition
chosen in the plan but does not choose it or score aesthetics; Slide and Review
make those judgments from PNGs.
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
    from image_background import (
        inspect_image,
        print_report as print_image_report,
        remove_checkerboard,
    )
    from workspace_policy import (
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
# Normal authoring should finish in two visual inspections. This higher safety
# ceiling exists only so a later repair Agent can recover a real defect instead
# of inheriting an immutable page at the end of the run.
MAX_PAGE_RENDER_STATES = 8
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
VISUAL_EVIDENCE = {"required", "preferred", "code_only", "none"}
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
ASSET_KINDS = {"real", "generated"}
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
            Path(__file__).resolve().parents[1] / "assets" / "base.css",
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
    source = Path(__file__).resolve().parents[1] / "assets" / "base.css"
    if not source.is_file():
        raise FileNotFoundError(f"frozen Skill base.css is missing: {source}")
    atomic_copy(root, source, root / "base.css")
    print("status:PASS")
    print("restored: base.css")


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
        for key in ("language", "page_count", "audience", "image_mode", "rationale")
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
        for key in ("role", "page_type", "page_family", "visual_evidence")
        if not meta.get(key)
    ]
    if missing_meta:
        raise ValueError(f"{path.name} missing metadata: {missing_meta}")
    page_type = _token(meta["page_type"], field=f"{path.name} page_type")
    page_family = _token(meta["page_family"], field=f"{path.name} page_family")
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
    visual_evidence = meta["visual_evidence"].strip().lower()
    if visual_evidence not in VISUAL_EVIDENCE:
        raise ValueError(
            f"{path.name} visual_evidence must be one of {sorted(VISUAL_EVIDENCE)}"
        )
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
    if visual_evidence != "none" and not sections.get("visual"):
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
        "special_layout": special_layout,
        "visual_evidence": visual_evidence,
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
    print("status:PASS")
    print(f"plans:{len(plans)} language:{deck['language']}")
    for warning in deck["warnings"]:
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
            "v0.2 uses OCR/text grounding and reacquired or generated assets instead"
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
            "real catalog entries need a direct `download` URL before fetch-images: "
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
        if entry["kind"] == "real" and not entry["source"]:
            raise ValueError(f"real asset {entry['id']!r} needs a source")
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
        if plan["visual_evidence"] != "required":
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
    const initial = byNumber.has(current) ? current : ordered[0];
    fitDeck();
    window.cleanDeck = {{ go, step, count: slides.length }};
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
    css = css_path.read_text(encoding="utf-8")
    document = _document(css, fragments, deck["language"])
    atomic_write_text(root, root / "present.html", document)
    print("built:", root / "present.html")
    return [number for number, _ in fragments]


def _page_render_state(root: Path, page: int) -> tuple[Path, list[str]]:
    path = root / "_trace" / "slide-render-states" / f"page_{page:02d}.json"
    if not path.is_file():
        return path, []
    payload = json.loads(path.read_text(encoding="utf-8"))
    hashes = payload.get("hashes") if isinstance(payload, dict) else None
    if not isinstance(hashes, list) or not all(isinstance(value, str) for value in hashes):
        raise ValueError(f"{path.relative_to(root)} is malformed")
    return path, hashes


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
    css = (root / "base.css").read_text(encoding="utf-8")
    digest = hashlib.sha256((css + "\0" + fragment).encode("utf-8")).hexdigest()
    state_path, hashes = _page_render_state(root, page)
    final_png = root / "renders" / f"slide_{page:02d}.png"
    is_new_state = digest not in hashes
    if is_new_state and len(hashes) >= MAX_PAGE_RENDER_STATES:
        raise ValueError(
            f"page {page:02d} reached the {MAX_PAGE_RENDER_STATES}-state render "
            "stop line; simplify the last composition or report a shared issue"
        )
    if is_new_state:
        hashes.append(digest)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            root,
            state_path,
            json.dumps(
                {
                    "page": page,
                    "limit": MAX_PAGE_RENDER_STATES,
                    "mode": "attempted-filled-html-css",
                    "hashes": hashes,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
    try:
        _validate_fragment(
            fragment_path,
            page,
            fragment,
            plan_map[page],
            deck,
        )
    except Exception as exc:
        if is_new_state:
            raise ValueError(
                f"{exc}\nrender-attempt-state: "
                f"{len(hashes)}/{MAX_PAGE_RENDER_STATES} consumed"
            ) from exc
        raise
    if digest in hashes and not is_new_state and final_png.is_file():
        print("status:PASS")
        print("render:cached unchanged HTML/CSS")
        print(f"render-state:{len(hashes)}/{MAX_PAGE_RENDER_STATES}")
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
    print("status:PASS")
    print(f"render-state:{len(hashes)}/{MAX_PAGE_RENDER_STATES}")
    print("next: inspect this PNG before any further page edit")
    print(final_png)


def audit(root: Path) -> None:
    violations = audit_workspace(root)
    if violations:
        raise ValueError("workspace output audit failed:\n- " + "\n- ".join(violations))
    print("status:PASS")
    print("workspace-output-audit: clean")


def clean(root: Path, *, unused_assets: bool = False) -> None:
    moved = clean_workspace(root, unused_assets=unused_assets)
    print("status:PASS")
    print("quarantined:", ", ".join(moved) if moved else "none")


def material_figure(root: Path, source_value: str, output_value: str, box_value: str) -> None:
    """Reject document-pixel reuse in the v0.2 replacement-visual policy."""
    raise ValueError(
        "material-figure is disabled in mural-presenter-v0.2; use OCR/text grounding, "
        "then reacquire a sourced real image, generate a conceptual image, or rebuild "
        "verified data/relationships in HTML/CSS/SVG"
    )

    # Kept below only for source compatibility with older frozen traces. The
    # unconditional policy gate above prevents any new v0.2 attachment crop.
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
    atomic_write_bytes(root, output, buffer.getvalue())
    print(json.dumps({
        "status": "PASS",
        "source": source_value,
        "output": output_value,
        "pixels": [crop.width, crop.height],
        "page_area": round(area_ratio, 4),
        "native_text_chars": text_chars,
        "native_text_coverage": round(text_coverage, 4),
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
    required_ready = [
        plan["number"]
        for plan in plans
        if plan["visual_evidence"] == "required"
    ]
    preferred_resolved = [
        plan["number"]
        for plan in plans
        if plan["visual_evidence"] == "preferred"
        and plan["number"] in pages_with_assets
    ]
    preferred_skipped = [
        plan["number"]
        for plan in plans
        if plan["visual_evidence"] == "preferred"
        and plan["number"] not in pages_with_assets
    ]
    print("status:PASS")
    print(f"assets:{len(entries)}")
    print(
        "required_ready:"
        + (",".join(f"{page:02d}" for page in required_ready) or "none")
    )
    print(
        "preferred_resolved:"
        + (",".join(f"{page:02d}" for page in preferred_resolved) or "none")
    )
    print(
        "preferred_skipped:"
        + (",".join(f"{page:02d}" for page in preferred_skipped) or "none")
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
    }


def finalize(root: Path, expected: int | None = None) -> None:
    assert_workspace_clean(root)
    deck, plans = _load_plans(root, expected)
    numbers = build(root, expected)
    renderer = Path(__file__).with_name("render_deck.py")
    _run_renderer(
        [
            sys.executable,
            str(renderer),
            str(root / "present.html"),
            str(root / "renders"),
            "--all",
        ]
    )
    manifest_path = root / "renders" / "render.json"
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
    manifest["special_page_geometry"] = geometry
    atomic_write_text(
        root,
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    if geometry["errors"]:
        raise ValueError(
            "layout/canvas audit failed:\n- " + "\n- ".join(geometry["errors"])
        )
    contact_sheet = root / "renders" / "contact-sheet.png"
    if not contact_sheet.is_file():
        raise FileNotFoundError("renders/contact-sheet.png is missing")
    assert_workspace_clean(root)
    print(
        "[workflow] Review owns the final pixel decision; inspect the whole-deck "
        "and special-page contact sheets once, then open only flagged pages."
    )
    print(contact_sheet)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p_prepare = sub.add_parser("prepare")
    p_prepare.add_argument("root")
    p_restore = sub.add_parser("restore-base")
    p_restore.add_argument("root")
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
    p_fetch = sub.add_parser("fetch-images")
    p_fetch.add_argument("root")
    p_fetch.add_argument("--replace", action="store_true")
    p_material_figure = sub.add_parser("material-figure")
    p_material_figure.add_argument("root")
    p_material_figure.add_argument("--source", required=True)
    p_material_figure.add_argument("--output", required=True)
    p_material_figure.add_argument("--box", required=True)
    p_clean = sub.add_parser("clean")
    p_clean.add_argument("root")
    p_clean.add_argument("--unused-assets", action="store_true")
    p_audit = sub.add_parser("audit")
    p_audit.add_argument("root")
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
    elif args.command == "fetch-images":
        fetch_images(root, replace=args.replace)
    elif args.command == "material-figure":
        material_figure(root, args.source, args.output, args.box)
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
