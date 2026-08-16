"""Deterministic environment preflight for MURAL Presenter v0.4.

The doctor is a Harness-owned outer gate.  It runs before Material parsing or
the first model request, persists a secret-free report, and supplies the
capability truth consumed by ``runtime_capabilities``.  Local batches, Studio
jobs and CI use this same implementation.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable

import requests

from . import config
from . import runtime_capabilities


SCHEMA = "mural.environment-doctor.v1"
_TINY_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAGUlEQVR4nGO8IyfH"
    "QApgIkn1qIZRDUNKAwCMSwE4wenu4QAAAABJRU5ErkJggg=="
)
_CORE_SKILL_FILES = (
    "SKILL.md",
    "assets/base.css",
    "roles/orchestrator.md",
    "roles/slide.md",
    "roles/review.md",
    "scripts/orchestrator.py",
    "scripts/image.py",
    "scripts/slide.py",
    "scripts/review.py",
    "scripts/_internal/deck_core.py",
    "scripts/_internal/renderer.py",
    "scripts/_internal/font_bundle.py",
)
_CORE_HARNESS_FILES = (
    "infer.py",
    "core/agent_loop.py",
    "core/config.py",
    "core/environment_doctor.py",
    "core/runtime_capabilities.py",
    "core/run_batch.py",
    "core/tools.py",
)
_ATTACHMENT_REQUIREMENTS = {
    ".pdf": ("pymupdf",),
    ".docx": ("python_docx",),
    ".pptx": ("python_pptx",),
    ".xlsx": ("openpyxl",),
    ".xlsm": ("openpyxl",),
    ".png": ("pillow",),
    ".jpg": ("pillow",),
    ".jpeg": ("pillow",),
    ".webp": ("pillow",),
    ".gif": ("pillow",),
    ".bmp": ("pillow",),
    ".tif": ("pillow",),
    ".tiff": ("pillow",),
}
_DIRECT_TEXT_SUFFIXES = {
    ".md", ".markdown", ".txt", ".csv", ".tsv", ".json", ".jsonl",
}
_SUPPORTED_ATTACHMENT_SUFFIXES = set(_ATTACHMENT_REQUIREMENTS) | _DIRECT_TEXT_SUFFIXES


def _truthy(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"", "0", "false", "no", "off"}


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, AttributeError, ValueError):
        return False


def _module_version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return ""


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _file_signature(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
        return {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    except OSError:
        return {"path": str(path), "missing": True}


def _credential_marker(value: str) -> str:
    """Fingerprint credential changes without storing credential material."""
    if not value:
        return "absent"
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _doctor_config(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = dict(cfg or {})
    backend = os.environ.get("MODEL_BACKEND", "anthropic").strip().lower() or "anthropic"
    model = str(
        cfg.get("model")
        or os.environ.get("MODEL")
        or os.environ.get("STUDENT_MODEL")
        or os.environ.get("ANTHROPIC_MODEL")
        or config.ANTHROPIC_MODEL
    ).strip()
    base_url = str(
        cfg.get("model_base_url")
        or (
            os.environ.get("STUDENT_BASE_URL", "")
            if backend == "openai"
            else os.environ.get("ANTHROPIC_BASE_URL", config.ANTHROPIC_BASE_URL)
        )
    ).strip().rstrip("/")
    image_enabled = bool(
        cfg.get("enable_image_gen", config.ENABLE_IMAGE_GEN)
        and (os.environ.get("IMAGE_API_KEY") or os.environ.get("OPENAI_API_KEY"))
    )
    return {
        "backend": backend,
        "model": model,
        "model_base_url": base_url,
        "model_key": (
            os.environ.get("STUDENT_API_KEY", "")
            if backend == "openai"
            else os.environ.get("ANTHROPIC_API_KEY", "")
        ),
        "vision_backend": config.VISION_BACKEND,
        "search_base_url": os.environ.get(
            "SERPER_BASE_URL", "https://google.serper.dev"
        ).strip().rstrip("/"),
        "search_key": os.environ.get("SERPER_API_KEY", "").strip(),
        "image_enabled": image_enabled,
        "image_base_url": str(
            cfg.get("openai_base_url")
            or os.environ.get("IMAGE_BASE_URL")
            or config.IMAGE_BASE_URL
        ).strip().rstrip("/"),
        "image_model": str(
            cfg.get("image_model")
            or os.environ.get("IMAGE_MODEL")
            or config.IMAGE_MODEL
        ).strip(),
        "image_key": (
            os.environ.get("IMAGE_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or ""
        ).strip(),
    }


def _module_matrix() -> dict[str, dict[str, Any]]:
    specs = {
        "playwright": ("playwright", "playwright"),
        "fonttools": ("fontTools", "fonttools"),
        "pymupdf": ("pymupdf", "PyMuPDF"),
        "pillow": ("PIL", "Pillow"),
        "python_docx": ("docx", "python-docx"),
        "python_pptx": ("pptx", "python-pptx"),
        "openpyxl": ("openpyxl", "openpyxl"),
        "rapidocr": ("rapidocr_onnxruntime", "rapidocr-onnxruntime"),
    }
    return {
        key: {
            "available": _module_available(module),
            "version": _module_version(distribution),
        }
        for key, (module, distribution) in specs.items()
    }


def environment_fingerprint(
    cfg: dict[str, Any] | None = None,
    *,
    required_attachment_suffixes: tuple[str, ...] | list[str] = (),
) -> str:
    resolved = _doctor_config(cfg)
    browser = runtime_capabilities._browser_executable()
    fonts = runtime_capabilities._font_sources()
    skill_root = config.SKILLS_DIR / config.SKILL_NAME
    payload = {
        "schema": SCHEMA,
        "python": _file_signature(Path(sys.executable)),
        "backend": resolved["backend"],
        "model": resolved["model"],
        "model_base_url": resolved["model_base_url"],
        "model_key": _credential_marker(resolved["model_key"]),
        "vision_backend": resolved["vision_backend"],
        "search_base_url": resolved["search_base_url"],
        "search_key": _credential_marker(resolved["search_key"]),
        "image_enabled": resolved["image_enabled"],
        "image_base_url": resolved["image_base_url"],
        "image_model": resolved["image_model"],
        "image_key": _credential_marker(resolved["image_key"]),
        "browser": _file_signature(Path(browser)) if browser else {"missing": True},
        "fonts": [
            _file_signature(Path(path))
            for path in (fonts.get("noto_sans_sc_path"), fonts.get("noto_serif_sc_path"))
            if path
        ],
        "modules": _module_matrix(),
        "libreoffice": shutil.which("libreoffice") or shutil.which("soffice") or "",
        "skill": [_file_signature(skill_root / rel) for rel in _CORE_SKILL_FILES],
        "harness": [_file_signature(config.ROOT / rel) for rel in _CORE_HARNESS_FILES],
        "attachment_suffixes": sorted({str(item).lower() for item in required_attachment_suffixes}),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _check_record(
    check_id: str,
    ok: bool,
    *,
    required: bool,
    detail: str,
    latency_ms: int | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": check_id,
        "ok": bool(ok),
        "required": bool(required),
        "detail": detail,
    }
    if latency_ms is not None:
        record["latency_ms"] = int(latency_ms)
    return record


def _renderer_probe(browser: str) -> tuple[bool, str, int]:
    started = time.monotonic()
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            instance = playwright.chromium.launch(
                executable_path=browser,
                headless=True,
                args=["--no-sandbox"],
            )
            try:
                page = instance.new_page(viewport={"width": 320, "height": 180})
                page.set_content(
                    "<!doctype html><style>html,body{margin:0}</style>"
                    "<main id='doctor'>MURAL</main>",
                    wait_until="load",
                )
                ok = page.locator("#doctor").inner_text() == "MURAL"
                pixels = page.screenshot(type="png")
                ok = ok and len(pixels) > 100
            finally:
                instance.close()
        detail = f"Chromium launch/render/screenshot {'PASS' if ok else 'FAIL'}: {browser}"
        return ok, detail, round((time.monotonic() - started) * 1000)
    except Exception as exc:  # noqa: BLE001
        return (
            False,
            f"Chromium smoke failed: {type(exc).__name__}: {str(exc)[:300]}",
            round((time.monotonic() - started) * 1000),
        )


def _post_json(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: int,
) -> tuple[requests.Response, Any, int]:
    started = time.monotonic()
    response = requests.post(url, headers=headers, json=payload, timeout=timeout)
    latency = round((time.monotonic() - started) * 1000)
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text[:300]}
    return response, body, latency


def _openai_multimodal_probe(resolved: dict[str, Any]) -> tuple[bool, str, int]:
    headers = {"Content-Type": "application/json"}
    key = str(resolved["model_key"] or "")
    if key and key != "EMPTY":
        headers["Authorization"] = f"Bearer {key}"
    payload: dict[str, Any] = {
        "model": resolved["model"],
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Name the dominant color in the image. Reply with one English color word only."},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{_TINY_PNG}"},
                },
            ],
        }],
        "max_tokens": 16,
        "temperature": 0,
        "stream": False,
    }
    transport = os.environ.get("STUDIO_THINKING_TRANSPORT", "").strip().lower()
    if transport in {"chat_template_kwargs", "openai"}:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    response, body, latency = _post_json(
        f"{resolved['model_base_url']}/chat/completions",
        headers=headers,
        payload=payload,
        timeout=int(os.environ.get("CLEAN_DOCTOR_MODEL_TIMEOUT", "90")),
    )
    choices = body.get("choices") if isinstance(body, dict) else None
    content = ""
    if isinstance(choices, list) and choices:
        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        raw_content = message.get("content", "") if isinstance(message, dict) else ""
        if isinstance(raw_content, str):
            content = raw_content
        elif isinstance(raw_content, list):
            content = " ".join(
                str(item.get("text") or "") for item in raw_content if isinstance(item, dict)
            )
    ok = bool(response.ok and choices and ("red" in content.lower() or "红" in content))
    detail = (
        f"OpenAI-compatible multimodal probe PASS ({response.status_code})"
        if ok
        else f"OpenAI-compatible multimodal probe failed ({response.status_code}): "
        + json.dumps(body.get("error", body), ensure_ascii=False)[:300]
    )
    return ok, detail, latency


def _anthropic_multimodal_probe(resolved: dict[str, Any]) -> tuple[bool, str, int]:
    base = str(resolved["model_base_url"])
    endpoint = f"{base}/messages" if base.endswith("/v1") else f"{base}/v1/messages"
    response, body, latency = _post_json(
        endpoint,
        headers={
            "x-api-key": str(resolved["model_key"]),
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        payload={
            "model": resolved["model"],
            "max_tokens": 16,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/png", "data": _TINY_PNG,
                    }},
                    {"type": "text", "text": "Name the dominant color in the image. Reply with one English color word only."},
                ],
            }],
        },
        timeout=int(os.environ.get("CLEAN_DOCTOR_MODEL_TIMEOUT", "90")),
    )
    blocks = body.get("content") if isinstance(body, dict) else None
    content = " ".join(
        str(item.get("text") or "") for item in (blocks or []) if isinstance(item, dict)
    )
    ok = bool(response.ok and blocks and ("red" in content.lower() or "红" in content))
    detail = (
        f"Anthropic multimodal probe PASS ({response.status_code})"
        if ok
        else f"Anthropic multimodal probe failed ({response.status_code}): "
        + json.dumps(body.get("error", body), ensure_ascii=False)[:300]
    )
    return ok, detail, latency


def _model_probe(resolved: dict[str, Any]) -> tuple[bool, str, int]:
    if resolved["backend"] == "openai":
        return _openai_multimodal_probe(resolved)
    return _anthropic_multimodal_probe(resolved)


def _search_probe(resolved: dict[str, Any]) -> tuple[bool, str, int]:
    base = str(resolved["search_base_url"])
    endpoint = base if base.endswith("/search") else f"{base}/search"
    response, body, latency = _post_json(
        endpoint,
        headers={
            "X-API-KEY": str(resolved["search_key"]),
            "Content-Type": "application/json",
        },
        payload={"q": "MURAL Presenter environment check", "num": 1, "hl": "en", "gl": "us"},
        timeout=int(os.environ.get("CLEAN_DOCTOR_SEARCH_TIMEOUT", "30")),
    )
    ok = bool(response.ok and isinstance(body, dict))
    detail = (
        f"search provider probe PASS ({response.status_code})"
        if ok
        else f"search provider probe failed ({response.status_code}): "
        + json.dumps(body.get("error", body), ensure_ascii=False)[:300]
    )
    return ok, detail, latency


def _image_probe(resolved: dict[str, Any]) -> tuple[bool, str, int]:
    response, body, latency = _post_json(
        f"{resolved['image_base_url']}/images/generations",
        headers={
            "Authorization": f"Bearer {resolved['image_key']}",
            "Content-Type": "application/json",
        },
        payload={
            "model": resolved["image_model"],
            "prompt": "Environment check: a plain neutral gray square, no text.",
            "size": "1024x1024",
            "n": 1,
        },
        timeout=int(os.environ.get("CLEAN_DOCTOR_IMAGE_TIMEOUT", "180")),
    )
    ok = bool(
        response.ok
        and isinstance(body, dict)
        and isinstance(body.get("data"), list)
        and body["data"]
    )
    detail = (
        f"image generation probe PASS ({response.status_code}, model={resolved['image_model']})"
        if ok
        else f"image generation probe failed ({response.status_code}, model={resolved['image_model']}): "
        + json.dumps(body.get("error", body), ensure_ascii=False)[:300]
    )
    return ok, detail, latency


def _safe_live_probe(
    probe: Callable[[dict[str, Any]], tuple[bool, str, int]],
    resolved: dict[str, Any],
) -> tuple[bool, str, int]:
    try:
        return probe(resolved)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:300]}", 0


def _cache_path(fingerprint: str) -> Path:
    default_root = Path(
        os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))
    ).expanduser() / "mural-presenter-v0.4" / "environment-doctor"
    root = Path(
        os.environ.get(
            "CLEAN_DOCTOR_CACHE_DIR",
            str(default_root),
        )
    ).expanduser()
    return root / f"{fingerprint}.json"


def _load_cache(path: Path, fingerprint: str, ttl_s: int) -> dict[str, Any] | None:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if (
        report.get("schema") != SCHEMA
        or report.get("config_fingerprint") != fingerprint
        or report.get("checked") is not True
        or report.get("ready") is not True
    ):
        return None
    checked_epoch = float(report.get("checked_epoch") or 0)
    if time.time() - checked_epoch > ttl_s:
        return None
    report["cache_hit"] = True
    return report


def _run_environment_doctor_unlocked(
    cfg: dict[str, Any] | None = None,
    *,
    report_path: str | Path | None = None,
    required_attachment_suffixes: tuple[str, ...] | list[str] = (),
    live: bool | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Run or reuse the shared preflight and return a secret-free report."""
    resolved = _doctor_config(cfg)
    suffixes = tuple(sorted({str(item).lower() for item in required_attachment_suffixes}))
    fingerprint = environment_fingerprint(cfg, required_attachment_suffixes=suffixes)
    target = Path(report_path) if report_path else _cache_path(fingerprint)
    ttl_s = max(0, int(os.environ.get("CLEAN_DOCTOR_TTL_S", "1800")))
    live = _truthy("CLEAN_DOCTOR_LIVE", True) if live is None else bool(live)
    if not force and ttl_s:
        cached = _load_cache(target, fingerprint, ttl_s)
        if cached:
            return cached

    started_epoch = time.time()
    checking = {
        "schema": SCHEMA,
        "checked": False,
        "ready": False,
        "status": "checking",
        "config_fingerprint": fingerprint,
        "started_epoch": started_epoch,
        "live_probes": live,
        "required_attachment_suffixes": list(suffixes),
    }
    _atomic_json(target, checking)

    required_checks: list[dict[str, Any]] = []
    optional_checks: list[dict[str, Any]] = []
    modules = _module_matrix()
    skill_root = config.SKILLS_DIR / config.SKILL_NAME
    missing_skill = [rel for rel in _CORE_SKILL_FILES if not (skill_root / rel).is_file()]
    missing_harness = [rel for rel in _CORE_HARNESS_FILES if not (config.ROOT / rel).is_file()]
    pair_ok = not missing_skill and not missing_harness
    required_checks.append(_check_record(
        "skill_harness_pair",
        pair_ok,
        required=True,
        detail=(
            f"paired Skill/Harness present: {skill_root}"
            if pair_ok
            else "missing: " + ", ".join([*missing_skill, *missing_harness])
        ),
    ))

    browser = runtime_capabilities._browser_executable()
    renderer_prereq = bool(browser and modules["playwright"]["available"])
    if renderer_prereq:
        renderer_ok, renderer_detail, renderer_latency = _renderer_probe(browser)
    else:
        renderer_ok, renderer_latency = False, 0
        renderer_detail = (
            "missing executable Chromium" if not browser else "Python package playwright unavailable"
        )
    required_checks.append(_check_record(
        "renderer",
        renderer_ok,
        required=True,
        detail=renderer_detail,
        latency_ms=renderer_latency,
    ))

    fonts = runtime_capabilities._font_sources()
    required_checks.append(_check_record(
        "fonts",
        bool(fonts.get("ready")),
        required=True,
        detail=(
            "portable Noto Sans SC delivery weights available"
            if fonts.get("ready")
            else "portable Noto Sans SC 400-900 source unavailable or invalid"
        ),
    ))
    optional_checks.append(_check_record(
        "serif_font",
        bool(fonts.get("noto_serif_sc") and fonts.get("noto_serif_sc_weight", {}).get("valid_delivery_weight")),
        required=False,
        detail=(
            "portable Noto Serif SC available"
            if fonts.get("noto_serif_sc")
            else "Noto Serif SC unavailable; serif choices will degrade"
        ),
    ))

    model_configured = bool(resolved["model"] and resolved["model_base_url"])
    if resolved["backend"] != "openai":
        model_configured = model_configured and bool(resolved["model_key"])
    if not model_configured:
        model_ok, model_detail, model_latency = False, "main model endpoint/model/key is incomplete", 0
    elif resolved["vision_backend"] == "disabled":
        model_ok, model_detail, model_latency = False, "selected main model is configured as text-only", 0
    elif live:
        model_ok, model_detail, model_latency = _safe_live_probe(_model_probe, resolved)
    else:
        model_ok, model_detail, model_latency = True, "configuration-only check; live multimodal probe skipped", 0
    required_checks.append(_check_record(
        "main_model_multimodal",
        model_ok,
        required=True,
        detail=model_detail,
        latency_ms=model_latency,
    ))

    unsupported_suffixes = [
        suffix for suffix in suffixes if suffix not in _SUPPORTED_ATTACHMENT_SUFFIXES
    ]
    parser_failures: list[str] = [
        f"{suffix}:unsupported_attachment_type" for suffix in unsupported_suffixes
    ]
    for suffix in suffixes:
        for key in _ATTACHMENT_REQUIREMENTS.get(suffix, ()):
            if not modules.get(key, {}).get("available"):
                parser_failures.append(f"{suffix}:{key}")
    libreoffice = shutil.which("libreoffice") or shutil.which("soffice") or ""
    for suffix in suffixes:
        if suffix in {".docx", ".pptx"} and not libreoffice:
            parser_failures.append(f"{suffix}:libreoffice_page_preview")
    required_checks.append(_check_record(
        "requested_attachment_parsers",
        not parser_failures,
        required=True,
        detail=(
            "all parsers required by this run are available"
            if not parser_failures
            else "missing parser routes: " + ", ".join(parser_failures)
        ),
    ))
    for key in ("pymupdf", "pillow", "python_docx", "python_pptx", "openpyxl", "rapidocr"):
        optional_checks.append(_check_record(
            f"attachment_{key}",
            bool(modules[key]["available"]),
            required=False,
            detail=(
                f"{key} {modules[key]['version'] or 'available'}"
                if modules[key]["available"]
                else f"{key} unavailable"
            ),
        ))
    optional_checks.append(_check_record(
        "libreoffice_preview",
        bool(libreoffice),
        required=False,
        detail=libreoffice or "LibreOffice unavailable; Office page-preview rendering is disabled",
    ))

    live_jobs: dict[str, Callable[[dict[str, Any]], tuple[bool, str, int]]] = {}
    if resolved["search_key"] and live:
        live_jobs["web_search"] = _search_probe
    if resolved["image_enabled"] and resolved["image_key"] and live:
        live_jobs["image_generate"] = _image_probe
    live_results: dict[str, tuple[bool, str, int]] = {}
    if live_jobs:
        with cf.ThreadPoolExecutor(max_workers=len(live_jobs)) as pool:
            futures = {
                name: pool.submit(_safe_live_probe, probe, resolved)
                for name, probe in live_jobs.items()
            }
            for name, future in futures.items():
                live_results[name] = future.result()

    service_failures: list[str] = []
    search_available = bool(resolved["search_key"])
    if not resolved["search_key"]:
        search_detail, search_latency = "not configured; Research/web tools will be omitted", 0
    elif live:
        search_available, search_detail, search_latency = live_results["web_search"]
        if not search_available:
            service_failures.append("configured search service is unavailable")
    else:
        search_detail, search_latency = "configured; live probe skipped", 0
    optional_checks.append(_check_record(
        "web_search",
        search_available,
        required=False,
        detail=search_detail,
        latency_ms=search_latency,
    ))

    image_available = bool(resolved["image_enabled"] and resolved["image_key"])
    if not image_available:
        image_detail, image_latency = "not configured; image generation will be omitted", 0
    elif live:
        image_available, image_detail, image_latency = live_results["image_generate"]
        if not image_available:
            service_failures.append("configured image generation service is unavailable")
    else:
        image_detail, image_latency = "configured; live probe skipped", 0
    optional_checks.append(_check_record(
        "image_generate",
        image_available,
        required=False,
        detail=image_detail,
        latency_ms=image_latency,
    ))

    fail_on_broken_optional = _truthy("CLEAN_DOCTOR_FAIL_ON_CONFIGURED_OPTIONAL", True)
    fatal_errors = [
        f"{item['id']}: {item['detail']}"
        for item in required_checks
        if not item["ok"]
    ]
    if fail_on_broken_optional:
        fatal_errors.extend(service_failures)
    warnings = [
        f"{item['id']}: {item['detail']}"
        for item in optional_checks
        if not item["ok"]
    ]
    ready = not fatal_errors
    checked_epoch = time.time()
    report = {
        "schema": SCHEMA,
        "checked": True,
        "ready": ready,
        "status": "blocked" if not ready else "degraded" if warnings else "ready",
        "config_fingerprint": fingerprint,
        "started_epoch": started_epoch,
        "checked_epoch": checked_epoch,
        "duration_ms": round((checked_epoch - started_epoch) * 1000),
        "cache_hit": False,
        "live_probes": live,
        "required_attachment_suffixes": list(suffixes),
        "required_checks": required_checks,
        "optional_checks": optional_checks,
        "capabilities": {
            "renderer": renderer_ok,
            "fonts": bool(fonts.get("ready")),
            "main_model_multimodal": model_ok,
            "web_search": search_available,
            "image_generate": image_available,
            "attachments": {
                key: bool(value["available"])
                for key, value in modules.items()
                if key in {"pymupdf", "pillow", "python_docx", "python_pptx", "openpyxl", "rapidocr"}
            },
            "libreoffice": bool(libreoffice),
        },
        "environment": {
            "python": sys.executable,
            "model_backend": resolved["backend"],
            "model": resolved["model"],
            "model_base_url": resolved["model_base_url"],
            "vision_backend": resolved["vision_backend"],
            "browser_executable": browser,
            "font_sources": {
                "sans": fonts.get("noto_sans_sc_path", ""),
                "serif": fonts.get("noto_serif_sc_path", ""),
            },
            "search_configured": bool(resolved["search_key"]),
            "image_generation_configured": bool(resolved["image_enabled"] and resolved["image_key"]),
            "image_model": resolved["image_model"],
            "libreoffice": libreoffice,
            "python_modules": modules,
        },
        "fatal_errors": fatal_errors,
        "warnings": warnings,
    }
    _atomic_json(target, report)
    return report


def run_environment_doctor(
    cfg: dict[str, Any] | None = None,
    *,
    report_path: str | Path | None = None,
    required_attachment_suffixes: tuple[str, ...] | list[str] = (),
    live: bool | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Serialize equal-fingerprint probes so a batch pays for them only once."""
    suffixes = tuple(sorted({str(item).lower() for item in required_attachment_suffixes}))
    fingerprint = environment_fingerprint(cfg, required_attachment_suffixes=suffixes)
    target = Path(report_path) if report_path else _cache_path(fingerprint)
    lock_path = target.with_name(target.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            # Native Windows deployments use one Studio dispatcher process;
            # POSIX CI/local batches receive the cross-process lock above.
            pass
        return _run_environment_doctor_unlocked(
            cfg,
            report_path=target,
            required_attachment_suffixes=suffixes,
            live=live,
            force=force,
        )


def ensure_environment_ready(
    cfg: dict[str, Any] | None = None,
    *,
    report_path: str | Path | None = None,
    required_attachment_suffixes: tuple[str, ...] | list[str] = (),
    live: bool | None = None,
    force: bool = False,
) -> dict[str, Any]:
    report = run_environment_doctor(
        cfg,
        report_path=report_path,
        required_attachment_suffixes=required_attachment_suffixes,
        live=live,
        force=force,
    )
    if report.get("checked") is not True or report.get("ready") is not True:
        raise RuntimeError(
            "environment doctor blocked execution: "
            + "; ".join(str(item) for item in report.get("fatal_errors") or ["unknown failure"])
        )
    return report


def persist_report(report: dict[str, Any], path: str | Path) -> None:
    """Copy an already-checked report into a batch/deck trace."""
    _atomic_json(Path(path), dict(report))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MURAL Presenter environment doctor")
    parser.add_argument("--json", dest="json_path", help="write report to this path")
    parser.add_argument("--no-live", action="store_true", help="configuration-only CI check")
    parser.add_argument("--force", action="store_true", help="ignore a valid cached report")
    parser.add_argument(
        "--attachment-suffix",
        action="append",
        default=[],
        help="attachment extension required by the planned run (repeatable)",
    )
    args = parser.parse_args(argv)
    report = run_environment_doctor(
        report_path=args.json_path,
        required_attachment_suffixes=args.attachment_suffix,
        live=not args.no_live,
        force=args.force,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ready") else 2


if __name__ == "__main__":
    raise SystemExit(main())
