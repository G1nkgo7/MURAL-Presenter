#!/usr/bin/env python3
"""Cross-platform launcher for SenseNova Present Studio.

The launcher deliberately uses only the Python standard library so it can
bootstrap the two managed ``uv`` environments before Studio dependencies are
installed.  Configuration precedence is:

    command line > process environment > .env file > built-in defaults
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STUDIO_ROOT = PROJECT_ROOT / "studio"
REPOSITORY_ROOT = PROJECT_ROOT.parents[2]
BUNDLED_MURAL_SKILL_ROOT = REPOSITORY_ROOT / "skills" / "mural-presenter"
BUNDLED_MURAL_HARNESS_ROOT = REPOSITORY_ROOT / "harnesses" / "mural-presenter"


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
    parser.add_argument(
        "--ui-only",
        action="store_true",
        default=_flag_default("SENSENOVA_UI_ONLY", False),
        help="start the WebUI without requiring a local generation Harness",
    )
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
    args = _parser().parse_args()

    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")

    mural_skill_root = Path(
        os.environ.get("PPTAGENT_MURAL_PRESENTER_SKILL_ROOT", BUNDLED_MURAL_SKILL_ROOT)
    ).expanduser().resolve()
    mural_harness_root = Path(
        os.environ.get("PPTAGENT_MURAL_PRESENTER_HARNESS_ROOT", BUNDLED_MURAL_HARNESS_ROOT)
    ).expanduser().resolve()
    if not args.ui_only:
        if not (mural_skill_root / "SKILL.md").is_file():
            raise SystemExit(f"MURAL-Presenter Skill not found: {mural_skill_root / 'SKILL.md'}")
        if not (mural_harness_root / "distill_ppt.py").is_file():
            raise SystemExit(
                f"MURAL-Presenter Harness not found: {mural_harness_root / 'distill_ppt.py'}"
            )

    uv_bin = os.environ.get("SENSE_NOVA_UV_BIN") or shutil.which("uv")
    if not args.no_install:
        if not uv_bin:
            raise SystemExit(
                "uv is required. Run start.sh/start.ps1 for automatic setup, "
                "or install it from https://docs.astral.sh/uv/."
            )
        _run([uv_bin, "sync", "--project", str(STUDIO_ROOT), "--frozen"], label="Preparing WebUI runtime")
        if not args.ui_only:
            _run(
                [uv_bin, "sync", "--project", str(mural_harness_root), "--frozen"],
                label="Preparing MURAL-Presenter runtime",
            )

    studio_python = _python_in(STUDIO_ROOT)
    engine_python = _python_in(mural_harness_root)
    if not studio_python.is_file():
        raise SystemExit(f"Studio runtime is missing: {studio_python}")
    if not args.ui_only and not engine_python.is_file():
        raise SystemExit(f"Generation runtime is missing: {engine_python}")

    playwright_root = Path(
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH", STUDIO_ROOT / "data/ms-playwright")
    ).expanduser().resolve()
    browser = _find_browser(playwright_root)
    if not args.ui_only and browser is None and not args.no_browser_install:
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
        "PPTAGENT_MURAL_PRESENTER_SUITE_ROOT": str(REPOSITORY_ROOT),
        "PPTAGENT_MURAL_PRESENTER_SKILL_ROOT": str(mural_skill_root),
        "PPTAGENT_MURAL_PRESENTER_HARNESS_ROOT": str(mural_harness_root),
        "AGENTIC_SKILLS_DIR": str(PROJECT_ROOT / "dynamic/skills"),
        "PLAYWRIGHT_BROWSERS_PATH": str(playwright_root),
        "STUDIO_SESSION_COOKIE": "sense_nova_present_session",
        "STUDIO_EDITION": args.edition,
        "STUDIO_AUTH_ENABLED": "0" if is_v1 else "1",
        "STUDIO_DYNAMIC_ENABLED": "0" if is_v1 or args.ui_only else "1",
        "STUDIO_SINGLE_USER_USERNAME": "user",
        "STUDIO_MAX_PER_MODEL": "0",
        "STUDIO_AGENT_MAX_TOKENS": "40960",
        "STUDENT_TEMPERATURE": "0.3",
    }
    if not args.ui_only:
        defaults.update({
            "PPTAGENT_ENGINE_PYTHON": str(engine_python),
            "DYNAMIC_RENDER_PYTHON": str(engine_python),
        })
    for key, value in defaults.items():
        os.environ.setdefault(key, value)
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
    os.environ["SENSENOVA_UI_ONLY"] = "1" if args.ui_only else "0"
    if browser:
        os.environ.setdefault("PPT_SKILL_BROWSER_EXE", str(browser))

    public = {
        "url": f"http://{args.host}:{args.port}",
        "language": args.language,
        "edition": args.edition,
        "ui_only": args.ui_only,
        "auth_enabled": _flag_default("STUDIO_AUTH_ENABLED", not is_v1),
        "dynamic_enabled": _flag_default("STUDIO_DYNAMIC_ENABLED", not is_v1),
        "mural_skill_root": str(mural_skill_root) if (mural_skill_root / "SKILL.md").is_file() else "not configured",
        "mural_harness_root": str(mural_harness_root) if (mural_harness_root / "distill_ppt.py").is_file() else "not configured",
        "data_dir": os.environ.get("STUDIO_DATA_DIR", str(STUDIO_ROOT / "data")),
        "environment_file": str(env_file),
        "browser": str(browser or "not installed"),
        "external_services": {
            "model_key": _mask(os.environ.get("SENSENOVA_MODEL_API_KEY", "")),
            "image_url": os.environ.get("SENSENOVA_IMAGE_BASE_URL", ""),
            "image_key": _mask(os.environ.get("SENSENOVA_IMAGE_API_KEY", "")),
            "search_url": os.environ.get("SENSENOVA_SEARCH_BASE_URL", ""),
            "search_key": _mask(os.environ.get("SENSENOVA_SEARCH_API_KEY", "")),
        },
    }
    if args.check:
        print(json.dumps(public, ensure_ascii=False, indent=2))
        return 0

    command = [
        str(studio_python), "-m", "uvicorn", "app.main:app",
        "--host", args.host, "--port", str(args.port),
    ]
    if args.reload:
        command.append("--reload")
    print(f"[SenseNova Present] Ready at {public['url']} (language={args.language}, edition={args.edition})")
    try:
        return subprocess.call(command, cwd=STUDIO_ROOT, env=os.environ.copy())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
