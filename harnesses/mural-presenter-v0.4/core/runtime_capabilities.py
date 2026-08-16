"""Deterministic per-run capability discovery for MURAL Presenter v0.4.

The canonical Skill stays frozen.  This module produces a small, auditable
runtime overlay that lets the Harness remove unavailable roles and tools before
the first model request.  Secret values are never persisted.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import re
from pathlib import Path

from . import config


RESEARCH_MODES = {"off", "open_research", "attachment_only", "verify_external"}


def user_requires_bitmap(raw_user_query: str) -> bool:
    """Detect an explicit delivery constraint that at least one bitmap is required."""
    query = re.sub(r"\s+", " ", str(raw_user_query or "")).strip()
    if not query:
        return False
    negative = (
        r"(?:不要|无需|不需要|禁止)[^，,。；;:：.!?\n]{0,12}(?:图片|配图|照片|位图|图像)",
        r"\b(?:no|without|do not use|don't use)\s+(?:images?|photos?|bitmaps?)\b",
    )
    if any(re.search(pattern, query, re.IGNORECASE) for pattern in negative):
        return False
    required = (
        r"(?:必须|务必|须|需要|要求|应当|要).{0,18}(?:含|有|使用|加入|包含|配).{0,8}(?:高质量)?(?:相关)?(?:图片|配图|照片|位图|图像)",
        r"(?:须含|必须包含|要求包含).{0,12}(?:图片|配图|照片|位图|图像)",
        r"(?:^|[：:；;。.!]\s*)(?:包含|加入|使用|配)(?:高质量)?(?:相关)?(?:图片|配图|照片|位图|图像)",
        r"(?:能|可以|可)(?:用|配|加)(?:图片|配图|照片|位图|图像).{0,8}(?:就|则)?(?:尽量|多)",
        r"(?:尽量|多|丰富).{0,6}(?:使用|加入|包含|配)?(?:图片|配图|照片|位图|图像)",
        r"(?:图片|配图|照片|位图|图像).{0,8}(?:尽量多|越多越好|丰富)",
        r"\b(?:must|required|require|needs? to)\b.{0,32}\b(?:images?|photos?|bitmaps?)\b",
        r"\b(?:include|use|add)\s+(?:high[- ]quality\s+)?(?:relevant\s+)?(?:images?|photos?|bitmaps?)\b",
        r"\b(?:image[- ]rich|as many (?:relevant )?(?:images?|photos?) as (?:possible|appropriate)|(?:images?|photos?) where possible)\b",
    )
    return any(re.search(pattern, query, re.IGNORECASE) for pattern in required)


def classify_research_mode(
    raw_user_query: str,
    *,
    has_material: bool,
    requested_mode: str = "",
) -> tuple[str, str]:
    """Resolve the evidence route before the first model call.

    This is deliberately conservative: transformations and explicitly offline
    tasks do not start Research merely because a search key happens to exist.
    """
    explicit = str(requested_mode or "").strip().lower()
    if explicit in RESEARCH_MODES:
        if explicit == "attachment_only" and not has_material:
            return "off", "attachment_only_without_material"
        return explicit, "explicit_request"
    query = str(raw_user_query or "").strip()
    offline_patterns = (
        r"(?:不要|禁止|无需)(?:联网|搜索|外部检索|使用外部)",
        r"(?:只|仅)(?:能|可)?(?:使用|基于|依据|参考).{0,10}(?:附件|材料|文档)",
        r"\b(?:do not|don't|no)\s+(?:browse|search|use external)\b",
        r"\b(?:attachment|provided material)s?\s+only\b",
    )
    if any(re.search(pattern, query, re.IGNORECASE) for pattern in offline_patterns):
        return ("attachment_only", "user_forbids_external_research") if has_material else (
            "off",
            "user_forbids_external_research",
        )
    transform_patterns = (
        r"^(?:请|帮我|帮忙)?(?:把|将)?(?:以下|这段|这份|附件)?(?:内容|文字|文档)?"
        r"(?:翻译|改写|润色|校对|排版|压缩|摘要|总结|转成|转换)",
        r"\b(?:translate|rewrite|polish|proofread|reformat|summari[sz]e|convert)\b",
        r"(?:虚构|创作)(?:故事|小说|诗歌|剧本)",
        r"\b(?:fiction|poem|creative writing)\b",
    )
    if any(re.search(pattern, query, re.IGNORECASE) for pattern in transform_patterns):
        return ("attachment_only", "transformation_uses_material") if has_material else (
            "off",
            "pure_transformation_or_fiction",
        )
    if has_material:
        return "verify_external", "material_with_targeted_verification"
    return "open_research", "public_facts_or_named_entities"


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
            # One source is bundled for every requested Noto delivery weight.
            # A static Regular file cannot honestly stand in for 700/900; the
            # source must expose a real axis that covers the delivery range.
            valid = bool(
                weight_axis and weight_axis[0] <= 400 and weight_axis[1] >= 900
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
    raw_user_query = str(cfg.get("_raw_user_query") or "")
    bitmap_required = user_requires_bitmap(raw_user_query)
    attachment_profile = (
        dict(cfg.get("_attachment_profile"))
        if isinstance(cfg.get("_attachment_profile"), dict)
        else {}
    )
    material_stage = str(
        attachment_profile.get("material_stage")
        or ("agent_required" if staged_materials else "omitted")
    ).strip().lower()
    material_agent_required = bool(
        attachment_profile.get("material_agent_required")
        if attachment_profile
        else staged_materials
    ) and not revision
    has_material = material_stage in {"direct_text", "agent_required", "mixed"} and not revision
    visual_asset_paths = [
        str(path) for path in attachment_profile.get("visual_asset_paths", []) if str(path)
    ]
    style_reference_paths = [
        str(path) for path in attachment_profile.get("style_reference_paths", []) if str(path)
    ]
    has_direct_visual_inputs = bool(visual_asset_paths)
    needs_image_ocr = any(
        isinstance(record, dict)
        and record.get("kind") == "image"
        and bool(record.get("needs_content_extraction"))
        for record in attachment_profile.get("records", [])
    )
    evidence_scope, research_reason = classify_research_mode(
        str(cfg.get("_raw_user_query") or ""),
        has_material=has_material,
        requested_mode=str(cfg.get("_evidence_scope") or cfg.get("evidence_scope") or ""),
    )
    if (
        material_stage == "direct_text"
        and not bool(cfg.get("_evidence_scope_explicit"))
        and not list(attachment_profile.get("unresolved_items") or [])
    ):
        evidence_scope = "attachment_only"
        research_reason = "direct_text_complete_no_unresolved"
    doctor = (
        dict(cfg.get("_environment_doctor"))
        if isinstance(cfg.get("_environment_doctor"), dict)
        else {}
    )
    doctor_capabilities = (
        dict(doctor.get("capabilities"))
        if doctor.get("checked") is True and isinstance(doctor.get("capabilities"), dict)
        else {}
    )
    search_configured = _configured("SERPER_API_KEY") and bool(
        doctor_capabilities.get("web_search", True)
    )
    search_enabled = search_configured and evidence_scope in {"open_research", "verify_external"}
    research_enabled = search_enabled
    image_key = _configured("IMAGE_API_KEY") or _configured("OPENAI_API_KEY")
    image_generation_enabled = bool(
        cfg.get("enable_image_gen", config.ENABLE_IMAGE_GEN)
        and image_key
        and doctor_capabilities.get("image_generate", True)
    )
    # Material can hand off reusable figures/photos even when external search
    # and generation are both disabled.
    image_acquisition_enabled = (
        material_agent_required
        or has_direct_visual_inputs
        or search_enabled
        or image_generation_enabled
    )

    active_roles = ["slide", "review"]
    if material_agent_required:
        active_roles.insert(0, "material")
    if research_enabled:
        active_roles.insert(1 if material_agent_required else 0, "research")
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
    if bitmap_required and not image_acquisition_enabled and not plan_only:
        fatal_errors.append(
            "user explicitly requires bitmap imagery but no image acquisition route is available"
        )
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
        warnings.append(f"research mode {evidence_scope} removes external web tools")
    if not image_generation_enabled:
        warnings.append("image generation is unavailable; image_generate is omitted")
    if not image_acquisition_enabled:
        warnings.append("no raster acquisition route; every plan must set needs_bitmap: false")
    if has_material and not research_enabled:
        warnings.append("attachment evidence hands off directly to Orchestrator; unresolved facts stay explicit")
    if needs_image_ocr and not modules["rapidocr"]:
        warnings.append("OCR is unavailable for image-only attachments")
    if not shutil.which("libreoffice") and not shutil.which("soffice"):
        warnings.append("LibreOffice preview rendering is unavailable")

    return {
        "schema_version": 2,
        "status": "blocked" if fatal_errors else ("degraded" if warnings else "ready"),
        "active_roles": active_roles,
        "omitted_roles": [role for role in ("material", "research", "image") if role not in active_roles],
        "workflow": {
            "material": (
                "agent" if material_agent_required
                else "direct_text" if material_stage == "direct_text"
                else "omitted"
            ),
            "research": (
                evidence_scope if research_enabled else "omitted"
            ),
            "research_mode": evidence_scope,
            "research_reason": research_reason,
            "material_stage": material_stage,
            "material_handoff": (
                "research" if has_material and research_enabled
                else "orchestrator" if has_material
                else "none"
            ),
            "image": (
                "user_or_material_bitmap_only"
                if (material_agent_required or has_direct_visual_inputs)
                and not search_enabled and not image_generation_enabled
                else "bitmap_available"
                if image_acquisition_enabled
                else "bitmap_unavailable"
            ),
            "vision": config.VISION_BACKEND,
        },
        "tools": {
            "web_search": search_enabled,
            "web_extract": search_enabled,
            "image_generate": image_generation_enabled,
            "vision_analyze": config.VISION_BACKEND != "disabled",
        },
        "inputs": {
            "material_count": len(staged_materials) if has_material else 0,
            "attachment_count": int(attachment_profile.get("attachment_count") or 0),
            "attachment_manifest": "_trace/attachment-manifest.json" if attachment_profile else "",
            "material_agent_paths": list(attachment_profile.get("material_agent_paths") or []),
            "direct_text_paths": list(attachment_profile.get("direct_text_paths") or []),
            "visual_asset_paths": visual_asset_paths,
            "style_reference_paths": style_reference_paths,
            "revision": bool(revision),
            "plan_only": bool(plan_only),
        },
        "requirements": {
            "bitmap_required": bitmap_required,
            "source": "raw_user_query" if bitmap_required else "none",
            "failure_policy": (
                "keep_needs_bitmap_and_return_image_blocked"
                if bitmap_required
                else "allow_explicit_plan_reclassification"
            ),
        },
        "environment": {
            "doctor_checked": doctor.get("checked") is True,
            "doctor_ready": doctor.get("ready") is True,
            "doctor_status": doctor.get("status", "not_run"),
            "doctor_fingerprint": doctor.get("config_fingerprint", ""),
            "python": sys.executable,
            "model_backend": os.environ.get("MODEL_BACKEND", "anthropic").strip().lower() or "anthropic",
            "model_configured": bool(cfg.get("model")),
            "model_base_url_configured": bool(cfg.get("model_base_url")),
            "vision_critic_model": (
                str(cfg.get("model") or "")
                if config.VISION_BACKEND == "same_model_aux"
                else config.VISION_CRITIC_MODEL
                if config.VISION_BACKEND == "external_model"
                else config.VISION_BACKEND
            ),
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
