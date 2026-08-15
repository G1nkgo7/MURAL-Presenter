"""Deterministic per-run capability discovery for MURAL Presenter v0.2.

The canonical Skill stays frozen.  This module produces a small, auditable
runtime overlay that lets the Harness remove unavailable roles and tools before
the first model request.  Secret values are never persisted.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path

from . import config


def _configured(name: str) -> bool:
    return bool(os.environ.get(name, "").strip())


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, AttributeError, ValueError):
        return False


def _browser_executable() -> str:
    explicit = os.environ.get("PPT_SKILL_BROWSER_EXE", "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    roots = [
        Path(value).expanduser()
        for value in (
            os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""),
            str(Path.home() / ".cache" / "ms-playwright"),
        )
        if value
    ]
    patterns = (
        "chromium-*/chrome-linux/chrome",
        "chromium-*/chrome-linux64/chrome",
        "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
    )
    for root in roots:
        for pattern in patterns:
            for candidate in sorted(root.glob(pattern), reverse=True):
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return str(candidate.resolve())
    return ""


def _font_sources() -> dict[str, object]:
    configured = [
        Path(item).expanduser()
        for item in os.environ.get("PPT_FONT_SOURCE_DIRS", "").split(os.pathsep)
        if item
    ]
    candidates = [
        *configured,
        config.ROOT.parents[1] / "fonts",
        Path.home() / ".fonts",
        Path.home() / ".local/share/fonts",
        Path("/usr/share/fonts/opentype/noto"),
        Path("/usr/share/fonts/truetype/noto"),
    ]
    existing: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        if resolved.is_dir() and str(resolved) not in seen:
            existing.append(resolved)
            seen.add(str(resolved))

    def find(names: tuple[str, ...]) -> str:
        for root in existing:
            for name in names:
                path = root / name
                if path.is_file() and path.stat().st_size:
                    return str(path)
        return ""

    sans = find(("NotoSansSC[wght].ttf", "NotoSansSC.ttf"))
    serif = find(("NotoSerifSC[wght].ttf", "NotoSerifSC.ttf"))

    def weight_profile(path: str) -> dict[str, object]:
        if not path:
            return {"valid_delivery_weight": False}
        try:
            from fontTools.ttLib import TTFont

            font = TTFont(path, lazy=True)
            try:
                static_weight = int(font["OS/2"].usWeightClass)
                axes = {
                    axis.axisTag: (float(axis.minValue), float(axis.maxValue))
                    for axis in font["fvar"].axes
                } if "fvar" in font else {}
            finally:
                font.close()
            weight_axis = axes.get("wght")
            valid = bool(
                (weight_axis and weight_axis[0] <= 400 and weight_axis[1] >= 900)
                or (not weight_axis and static_weight >= 400)
            )
            return {
                "static_weight": static_weight,
                "variable_weight_range": list(weight_axis) if weight_axis else [],
                "valid_delivery_weight": valid,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "valid_delivery_weight": False,
                "validation_error": type(exc).__name__,
            }

    sans_weight = weight_profile(sans)
    serif_weight = weight_profile(serif)
    return {
        "ready": bool(sans and sans_weight.get("valid_delivery_weight")),
        "source_dirs": [str(path) for path in existing],
        "noto_sans_sc": bool(sans),
        "noto_serif_sc": bool(serif),
        "noto_sans_sc_path": sans,
        "noto_serif_sc_path": serif,
        "noto_sans_sc_weight": sans_weight,
        "noto_serif_sc_weight": serif_weight,
    }


def detect_runtime_capabilities(
    cfg: dict,
    staged_materials: list[str] | tuple[str, ...] = (),
    *,
    skill_names: set[str] | None = None,
    revision: bool = False,
    plan_only: bool = False,
) -> dict:
    """Return the immutable runtime overlay for one Deck.

    Missing optional services degrade the workflow instead of failing the
    Deck.  Missing deterministic delivery prerequisites are reported as fatal
    before model work begins (except in plan-only evaluation).
    """
    has_material = bool(staged_materials) and not revision
    evidence_scope = str(
        cfg.get("_evidence_scope") or cfg.get("evidence_scope") or "open_research"
    ).strip().lower()
    search_configured = _configured("SERPER_API_KEY")
    search_enabled = search_configured and evidence_scope != "attachment_only"
    research_enabled = search_configured and (
        evidence_scope != "attachment_only" or has_material
    )
    image_key = _configured("IMAGE_API_KEY") or _configured("OPENAI_API_KEY")
    image_generation_enabled = bool(
        cfg.get("enable_image_gen", config.ENABLE_IMAGE_GEN) and image_key
    )
    image_acquisition_enabled = search_enabled or image_generation_enabled

    active_roles = ["slide", "review"]
    if has_material:
        active_roles.insert(0, "material")
    if research_enabled:
        active_roles.insert(1 if has_material else 0, "research")
    if image_acquisition_enabled:
        active_roles.insert(-2, "image")

    browser = _browser_executable()
    fonts = _font_sources()
    modules = {
        "playwright": _module_available("playwright"),
        "fonttools": _module_available("fontTools"),
        "pymupdf": _module_available("pymupdf"),
        "pillow": _module_available("PIL"),
        "python_docx": _module_available("docx"),
        "python_pptx": _module_available("pptx"),
        "openpyxl": _module_available("openpyxl"),
        "rapidocr": _module_available("rapidocr_onnxruntime"),
    }
    selected_skills = sorted(skill_names or set())
    missing_skills = [
        name
        for name in selected_skills
        if not (config.SKILLS_DIR / name / "SKILL.md").is_file()
    ]
    fatal_errors: list[str] = []
    if not str(cfg.get("model") or "").strip():
        fatal_errors.append("main model is not configured")
    if not str(cfg.get("model_base_url") or "").strip():
        fatal_errors.append("main model base URL is not configured")
    if missing_skills:
        fatal_errors.append("missing Skill entry: " + ", ".join(missing_skills))
    if not plan_only:
        if not browser:
            fatal_errors.append("no executable Playwright Chromium runtime")
        if not modules["playwright"]:
            fatal_errors.append("Python package playwright is unavailable")
        if not modules["fonttools"]:
            fatal_errors.append("Python package fonttools is unavailable")
        if not bool(fonts["ready"]):
            fatal_errors.append("portable Noto Sans SC font source is unavailable")
        if config.VISION_BACKEND == "disabled":
            fatal_errors.append("pixel inspection is unavailable for final slide production")

    warnings: list[str] = []
    if not search_configured:
        warnings.append("Serper is unavailable; Research is omitted")
    elif not search_enabled:
        warnings.append("attachment_only scope removes external web tools")
    if not image_generation_enabled:
        warnings.append("image generation is unavailable; image_generate is omitted")
    if not image_acquisition_enabled:
        warnings.append("no raster acquisition route; plans must use code_only/none visuals")
    if has_material and not research_enabled:
        warnings.append("Material hands off directly to Orchestrator; unresolved facts stay explicit")
    if not modules["rapidocr"]:
        warnings.append("OCR is unavailable for image-only attachments")
    if not shutil.which("libreoffice") and not shutil.which("soffice"):
        warnings.append("LibreOffice preview rendering is unavailable")

    return {
        "schema_version": 1,
        "status": "blocked" if fatal_errors else ("degraded" if warnings else "ready"),
        "active_roles": active_roles,
        "omitted_roles": [role for role in ("material", "research", "image") if role not in active_roles],
        "workflow": {
            "material": "agent" if has_material else "omitted",
            "research": (
                "attachment_only" if research_enabled and not search_enabled
                else "external" if research_enabled
                else "omitted"
            ),
            "material_handoff": (
                "research" if has_material and research_enabled
                else "orchestrator" if has_material
                else "none"
            ),
            "image": "search_or_generate" if image_acquisition_enabled else "code_only",
        },
        "tools": {
            "web_search": search_enabled,
            "web_extract": search_enabled,
            "image_generate": image_generation_enabled,
            "vision_analyze": config.VISION_BACKEND != "disabled",
        },
        "inputs": {
            "material_count": len(staged_materials) if has_material else 0,
            "revision": bool(revision),
            "plan_only": bool(plan_only),
        },
        "environment": {
            "python": sys.executable,
            "model_backend": os.environ.get("MODEL_BACKEND", "anthropic").strip().lower() or "anthropic",
            "model_configured": bool(cfg.get("model")),
            "model_base_url_configured": bool(cfg.get("model_base_url")),
            "serper_key_configured": search_configured,
            "image_key_configured": image_key,
            "browser_executable": browser,
            "fonts": fonts,
            "python_modules": modules,
            "libreoffice": shutil.which("libreoffice") or shutil.which("soffice") or "",
        },
        "fatal_errors": fatal_errors,
        "warnings": warnings,
    }
