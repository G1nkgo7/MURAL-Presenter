#!/usr/bin/env python3
"""Cross-platform launcher for SenseNova Present Studio.

The launcher deliberately uses only the Python standard library so it can
bootstrap the two managed ``uv`` environments before Studio dependencies are
installed.  Configuration precedence is:

    command line > process environment > .env file > built-in defaults
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STUDIO_ROOT = PROJECT_ROOT / "studio"
ENGINE_ROOT = PROJECT_ROOT / "inference"
MURAL_PRESENTER_SUITE = PROJECT_ROOT.parent


def _verify_mural_release() -> dict:
    guard_path = (
        MURAL_PRESENTER_SUITE
        / "harnesses/mural-presenter-v1/runtime/release_guard.py"
    )
    spec = importlib.util.spec_from_file_location("mural_v1_release_guard", guard_path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Cannot load MURAL release guard: {guard_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        return module.verify_runtime_release(
            "webui", repository_root=MURAL_PRESENTER_SUITE
        )
    except RuntimeError as exc:
        raise SystemExit(f"MURAL release verification failed: {exc}") from exc


def _load_env_file(path: Path) -> None:
    """Load a small dotenv/shell-export file without overriding the shell."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SystemExit(f"Cannot read environment file {path}: {exc}") from exc
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise SystemExit(f"Invalid environment entry at {path}:{number}")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "A").isalnum() or key[0].isdigit():
            raise SystemExit(f"Invalid environment name at {path}:{number}: {key!r}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _python_in(project: Path) -> Path:
    if os.name == "nt":
        return project / ".venv" / "Scripts" / "python.exe"
    return project / ".venv" / "bin" / "python"


def _find_browser(root: Path) -> Path | None:
    names = {"chrome-headless-shell", "chrome-headless-shell.exe"}
    if not root.exists():
        return None
    for candidate in root.rglob("*"):
        if candidate.is_file() and candidate.name in names:
            return candidate
    return None


def _run(command: list[str], *, label: str) -> None:
    print(f"[SenseNova Present] {label}", flush=True)
    try:
        subprocess.run(command, cwd=PROJECT_ROOT, check=True)
    except FileNotFoundError as exc:
        raise SystemExit(f"Command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"{label} failed (exit={exc.returncode})") from exc


def _flag_default(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"", "0", "false", "no", "off"}


def _font_has_weight_axis(
    engine_python: Path,
    path: Path,
    minimum: float,
    maximum: float,
) -> bool:
    probe = (
        "from fontTools.ttLib import TTFont; import sys; "
        "f=TTFont(sys.argv[1], lazy=False); "
        "a=next((x for x in f['fvar'].axes if x.axisTag=='wght'), None) "
        "if 'fvar' in f else None; "
        "raise SystemExit(0 if a and a.minValue<=float(sys.argv[2]) "
        "and a.maxValue>=float(sys.argv[3]) else 1)"
    )
    try:
        return subprocess.run(
            [str(engine_python), "-c", probe, str(path), str(minimum), str(maximum)],
            cwd=PROJECT_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _python_imports_runtime_modules(python: Path) -> bool:
    """Verify attachment/runtime modules inside the managed engine venv."""
    modules = (
        "playwright",
        "PIL",
        "numpy",
        "cv2",
        "pptx",
        "docx",
        "openpyxl",
        "pymupdf",
        "rapidocr_onnxruntime",
        "fontTools",
    )
    probe = "; ".join(f"import {name}" for name in modules)
    try:
        return subprocess.run(
            [str(python), "-c", probe],
            cwd=PROJECT_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _presenter_runtime_ready(
    engine_python: Path,
    fonts_dir: Path,
) -> bool:
    sans = fonts_dir / "NotoSansSC.ttf"
    serif = fonts_dir / "NotoSerifSC.ttf"
    return (
        engine_python.is_file()
        and _python_imports_runtime_modules(engine_python)
        and sans.is_file()
        and serif.is_file()
        and _font_has_weight_axis(engine_python, sans, 100, 900)
        and _font_has_weight_axis(engine_python, serif, 200, 900)
    )


def _prepare_presenter_runtime(engine_python: Path, fonts_dir: Path) -> tuple[Path, Path]:
    """依赖由 inference 的 uv.lock 安装，字体随唯一源码交付，不执行历史安装器。"""
    if _presenter_runtime_ready(engine_python, fonts_dir):
        return engine_python, fonts_dir
    raise SystemExit(
        f"Incomplete inference runtime or packaged fonts: {engine_python}; {fonts_dir}. "
        f"Run uv sync --project {ENGINE_ROOT} --frozen and restore the release fonts directory."
    )


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "configured"
    return f"{value[:3]}…{value[-3:]}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Start SenseNova Present WebUI on Linux, macOS, or Windows."
    )
    parser.add_argument(
        "--language", choices=("zh", "en"),
        default=os.environ.get("STUDIO_LANGUAGE", "zh").strip().lower() or "zh",
        help="initial WebUI language (default: zh)",
    )
    parser.add_argument(
        "--host", default=os.environ.get("SENSE_NOVA_LOCAL_HOST", "127.0.0.1"),
        help="listen address (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("SENSE_NOVA_LOCAL_PORT", "8001")),
        help="listen port (default: 8001)",
    )
    parser.add_argument(
        "--edition", choices=("v1", "full"),
        default=os.environ.get("STUDIO_EDITION", "v1").strip().lower() or "v1",
        help="product profile (default: v1)",
    )
    parser.add_argument(
        "--env-file", type=Path,
        default=Path(os.environ.get("SENSENOVA_ENV_FILE", PROJECT_ROOT / ".env")),
        help="dotenv file loaded below existing shell variables (default: ./.env)",
    )
    parser.add_argument("--reload", action="store_true", help="enable Uvicorn development reload")
    parser.add_argument("--no-install", action="store_true", help="reuse existing virtual environments")
    parser.add_argument(
        "--no-browser-install", action="store_true",
        help="do not install Playwright Chromium when it is missing",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="validate and print the effective non-secret configuration without starting",
    )
    return parser


def main() -> int:
    # Read the conventional file first so its values can become argparse
    # defaults. Existing exported variables remain authoritative.
    preliminary = argparse.ArgumentParser(add_help=False)
    preliminary.add_argument(
        "--env-file", type=Path,
        default=Path(os.environ.get("SENSENOVA_ENV_FILE", PROJECT_ROOT / ".env")),
    )
    known, _ = preliminary.parse_known_args()
    env_file = known.env_file.expanduser().resolve()
    _load_env_file(env_file)
    sys.path.insert(0, str(MURAL_PRESENTER_SUITE / "scripts"))
    from runtime_environment import prepare_environment
    prepare_environment(MURAL_PRESENTER_SUITE)
    args = _parser().parse_args()

    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")

    pipeline_root = Path(
        os.environ.get("PPTAGENT_CLEAN_PIPELINE_ROOT", PROJECT_ROOT / "vendor/static_ppt-clean-current")
    ).expanduser().resolve()
    presenter_skill = (
        MURAL_PRESENTER_SUITE
        / "skills/mural-presenter-v1/SKILL.md"
    )
    presenter_v1_harness = (
        MURAL_PRESENTER_SUITE / "harnesses/mural-presenter-v1/infer.py"
    )
    has_presenter = presenter_skill.is_file() and presenter_v1_harness.is_file()
    if not has_presenter and not (pipeline_root / "infer.py").is_file():
        raise SystemExit(
            "No runnable static generation pair was found. Expected either "
            f"{presenter_skill} + {presenter_v1_harness}, or "
            f"{pipeline_root / 'infer.py'}."
        )
    if args.reload:
        os.environ.setdefault("MURAL_RELEASE_POLICY", "development")
    release_report = _verify_mural_release() if has_presenter else None

    uv_bin = os.environ.get("SENSE_NOVA_UV_BIN") or shutil.which("uv")
    if not args.no_install:
        if not uv_bin:
            raise SystemExit(
                "uv is required. Run start.sh/start.ps1 for automatic setup, "
                "or install it from https://docs.astral.sh/uv/."
            )
        _run([uv_bin, "sync", "--project", str(STUDIO_ROOT), "--frozen"], label="Preparing WebUI runtime")
        _run([uv_bin, "sync", "--project", str(ENGINE_ROOT), "--frozen"], label="Preparing generation runtime")

    studio_python = _python_in(STUDIO_ROOT)
    engine_python = _python_in(ENGINE_ROOT)
    if not studio_python.is_file():
        raise SystemExit(f"Studio runtime is missing: {studio_python}")
    if not engine_python.is_file():
        raise SystemExit(f"Generation runtime is missing: {engine_python}")

    playwright_root = Path(
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH", STUDIO_ROOT / "data/ms-playwright")
    ).expanduser().resolve()
    normalize_python = engine_python
    fonts_dir = MURAL_PRESENTER_SUITE / "fonts"
    if not args.no_install and has_presenter:
        normalize_python, fonts_dir = _prepare_presenter_runtime(engine_python, fonts_dir)
    browser = _find_browser(playwright_root)
    if browser is None and not args.no_browser_install:
        env = os.environ.copy()
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(playwright_root)
        print("[SenseNova Present] Installing Chromium renderer (first run only)", flush=True)
        subprocess.run(
            [str(engine_python), "-m", "playwright", "install", "chromium"],
            cwd=PROJECT_ROOT, env=env, check=True,
        )
        browser = _find_browser(playwright_root)

    is_v1 = args.edition == "v1"
    defaults = {
        "PPTAGENT_CLEAN_PIPELINE_ROOT": str(pipeline_root),
        "PPTAGENT_INFERENCE_ROOT": str(ENGINE_ROOT),
        "PPTAGENT_ENGINE_PYTHON": str(engine_python),
        "PLAYWRIGHT_BROWSERS_PATH": str(playwright_root),
        "NORMALIZE_PY": str(normalize_python),
        "PPT_FONT_SOURCE_DIRS": str(fonts_dir),
        "STUDIO_SESSION_COOKIE": "sense_nova_present_session",
        "STUDIO_EDITION": args.edition,
        "STUDIO_AUTH_ENABLED": "0" if is_v1 else "1",
        "STUDIO_DYNAMIC_ENABLED": "0" if is_v1 else "1",
        "STUDIO_SINGLE_USER_USERNAME": "user",
        "STUDIO_AGENT_MAX_TOKENS": "40960",
        "STUDENT_TEMPERATURE": "0.3",
        # Release deployments start with no internal test model exposed.
        # Operators may register one model through SENSENOVA_MODEL_* and users
        # can add/remove further OpenAI-compatible models in the WebUI.
        "PPTAGENT_PUBLIC_MODEL_KEYS": "",
    }
    if not is_v1:
        defaults.update({
            "DYNAMIC_RENDER_PYTHON": str(engine_python),
            "AGENTIC_SKILLS_DIR": str(PROJECT_ROOT / "dynamic/skills"),
        })
    if has_presenter:
        defaults.update({
            "MURAL_SKILL_DIR": str(MURAL_PRESENTER_SUITE / "skills/mural-presenter-v1"),
            "PPTAGENT_MURAL_V1_HARNESS_ROOT": str(MURAL_PRESENTER_SUITE / "harnesses/mural-presenter-v1"),
            "PPTAGENT_MURAL_V1_HARNESS_ENTRY": "infer.py",
            "PPTAGENT_PUBLIC_SKILL_KEYS": "mural-presenter-v1",
            "PPTAGENT_DEFAULT_SKILL": "mural-presenter-v1",
        })
    for key, value in defaults.items():
        os.environ.setdefault(key, value)
    # These paths are managed runtime identities. Generation, attachment/OCR
    # workers and model-issued shell commands must all resolve the same venv.
    os.environ["PPTAGENT_INFERENCE_ROOT"] = str(ENGINE_ROOT)
    os.environ["PPTAGENT_ENGINE_PYTHON"] = str(engine_python)
    os.environ["NORMALIZE_PY"] = str(engine_python)
    configured_data_dir = os.environ.get("STUDIO_DATA_DIR", "").strip()
    if configured_data_dir:
        data_path = Path(configured_data_dir).expanduser()
        if not data_path.is_absolute():
            data_path = PROJECT_ROOT / data_path
        os.environ["STUDIO_DATA_DIR"] = str(data_path.resolve())
    os.environ["STUDIO_EDITION"] = args.edition
    os.environ["STUDIO_LANGUAGE"] = args.language
    os.environ["SENSE_NOVA_LOCAL_HOST"] = args.host
    os.environ["SENSE_NOVA_LOCAL_PORT"] = str(args.port)
    if browser:
        os.environ.setdefault("PPT_SKILL_BROWSER_EXE", str(browser))

    environment_model_url = os.environ.get("SENSENOVA_MODEL_BASE_URL", "").strip()
    environment_model_name = os.environ.get("SENSENOVA_MODEL_NAME", "").strip()
    if bool(environment_model_url) != bool(environment_model_name):
        raise SystemExit(
            "SENSENOVA_MODEL_BASE_URL and SENSENOVA_MODEL_NAME must be configured together"
        )

    # 启动诊断复用 WebUI 的 stdlib 配置读取器，不再展示已停用的 Provider 别名。
    sys.path.insert(0, str(PROJECT_ROOT))
    from studio.app import service_config

    external_env = service_config.system_runtime_env()
    public = {
        "url": f"http://{args.host}:{args.port}",
        "language": args.language,
        "edition": args.edition,
        "auth_enabled": _flag_default("STUDIO_AUTH_ENABLED", not is_v1),
        "dynamic_enabled": _flag_default("STUDIO_DYNAMIC_ENABLED", not is_v1),
        "pipeline_root": str(pipeline_root),
        "presenter_suite": str(
            MURAL_PRESENTER_SUITE if has_presenter else "external configuration"
        ),
        "default_skill": os.environ.get("PPTAGENT_DEFAULT_SKILL", "environment default"),
        "public_model_keys": [
            item.strip()
            for item in os.environ.get("PPTAGENT_PUBLIC_MODEL_KEYS", "").split(",")
            if item.strip()
        ],
        "environment_model": (
            os.environ.get("SENSENOVA_MODEL_DISPLAY_NAME", "").strip()
            or os.environ.get("SENSENOVA_MODEL_NAME", "").strip()
            or "not configured"
        ),
        "data_dir": os.environ.get("STUDIO_DATA_DIR", str(STUDIO_ROOT / "data")),
        "environment_file": str(env_file),
        "browser": str(browser or "not installed"),
        "engine_python": str(engine_python),
        "normalize_python": str(normalize_python),
        "font_source_dirs": os.environ.get("PPT_FONT_SOURCE_DIRS", str(fonts_dir)),
        "mural_release": (
            {
                "release": release_report["release"],
                "source_commit": release_report["source_commit"],
                "dirty_override": release_report["dirty_override"],
            }
            if release_report
            else None
        ),
        "external_services": {
            "provider_environment_file": str(service_config.PROVIDER_ENV_PATH),
            "model_key": _mask(os.environ.get("SENSENOVA_MODEL_API_KEY", "")),
            "image_url": external_env.get("IMAGE_BASE_URL", ""),
            "image_model": external_env.get("IMAGE_MODEL", ""),
            "image_key": _mask(external_env.get("IMAGE_API_KEY", "")),
            "search_url": external_env.get("SERPER_BASE_URL", ""),
            "search_key": _mask(external_env.get("SERPER_API_KEY", "")),
        },
    }
    keep_awake = (
        sys.platform == "darwin"
        and _flag_default("SENSENOVA_KEEP_AWAKE", True)
        and bool(shutil.which("caffeinate"))
    )
    public["macos_keep_awake"] = keep_awake
    if args.check:
        print(json.dumps(public, ensure_ascii=False, indent=2))
        return 0

    command = [
        str(studio_python), "-m", "uvicorn", "app.main:app",
        "--host", args.host, "--port", str(args.port),
        "--timeout-graceful-shutdown", "5",
    ]
    root_path = os.environ.get("STUDIO_BASE_PATH", "").rstrip("/")
    if root_path:
        command.extend(["--root-path", root_path])
    if args.reload:
        command.append("--reload")
    if keep_awake:
        # Prevent idle sleep from pausing long generation jobs. Closing the
        # laptop lid can still suspend macOS and is outside process control.
        command = [shutil.which("caffeinate"), "-i", *command]
        print("[SenseNova Present] macOS idle-sleep prevention is active (caffeinate -i)", flush=True)
    print(f"[SenseNova Present] Ready at {public['url']} (language={args.language}, edition={args.edition})")
    try:
        return subprocess.call(command, cwd=STUDIO_ROOT, env=os.environ.copy())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
