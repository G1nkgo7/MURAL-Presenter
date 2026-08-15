#!/usr/bin/env python3
"""Inference entry point for the bilingual MURAL Presenter v0.4 harness."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
_BROKER_ENV_ALLOW = {
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
    "CLEAN_RENDER_CONCURRENCY",
    "CLEAN_RENDER_CONCURRENCY_FILE",
    "CLEAN_RENDER_POOL_MAX_WORKERS",
    "CLEAN_RENDER_STATUS_FILE",
    "CLEAN_RENDER_JOB_TIMEOUT",
    "CLEAN_RENDER_CLEANUP_GRACE",
}
_DOTENV_BROKER_KEYS = {
    "CONDA_PREFIX",
    "LD_LIBRARY_PATH",
    "FONTCONFIG_FILE",
    "PLAYWRIGHT_BROWSERS_PATH",
    "PPT_RENDER_LOCK_PATH",
    "PPT_RENDER_LOCK_DIR",
    "PPT_RENDER_CONCURRENCY",
    "PPT_RENDER_MAX_PAGES",
    "CLEAN_RENDER_CONCURRENCY",
    "CLEAN_RENDER_CONCURRENCY_FILE",
    "CLEAN_RENDER_POOL_MAX_WORKERS",
    "CLEAN_RENDER_STATUS_FILE",
    "CLEAN_RENDER_JOB_TIMEOUT",
    "CLEAN_RENDER_CLEANUP_GRACE",
    "PPT_SKILL_BROWSER_EXE",
    "PPT_SKILL_BROWSER_LIB_DIRS",
}


def _load_render_environment() -> None:
    """Load only credential-free renderer settings before the broker starts."""
    path = Path(os.environ.get("CLEAN_DOTENV_PATH", ROOT / ".env"))
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in _DOTENV_BROKER_KEYS:
            os.environ[key] = value.strip().strip('"').strip("'")


def _configure_runtime_environment() -> None:
    """Make every Harness subprocess use the interpreter that launched infer.py."""
    executable = Path(sys.executable).absolute()
    runtime_bin = str(executable.parent)
    path_parts = [part for part in os.environ.get("PATH", "").split(os.pathsep) if part]
    path_parts = [part for part in path_parts if part != runtime_bin]
    os.environ["PATH"] = os.pathsep.join([runtime_bin, *path_parts])
    os.environ["PPT_SKILL_PYTHON"] = str(executable)


def _start_render_broker() -> tuple[subprocess.Popen, Path]:
    broker = Path(tempfile.mkdtemp(prefix="ppt-clean-render-"))
    env = {
        key: value for key, value in os.environ.items() if key in _BROKER_ENV_ALLOW
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "core.render_broker", str(broker)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 8
    while not (broker / "ready").is_file():
        if process.poll() is not None:
            raise RuntimeError("render broker exited during startup")
        if time.monotonic() >= deadline:
            process.terminate()
            raise TimeoutError("render broker did not become ready")
        time.sleep(0.05)
    os.environ["CLEAN_RENDER_BROKER_DIR"] = str(broker)
    return process, broker


def _stop_render_broker(process: subprocess.Popen, broker: Path) -> None:
    try:
        (broker / "stop").write_text("stop\n", encoding="utf-8")
        process.wait(timeout=5)
    except Exception:
        process.terminate()
        try:
            process.wait(timeout=2)
        except Exception:
            process.kill()
    finally:
        shutil.rmtree(broker, ignore_errors=True)


if __name__ == "__main__":
    _load_render_environment()
    _configure_runtime_environment()
    process, broker = _start_render_broker()
    try:
        from core.run_batch import main

        main()
    finally:
        _stop_render_broker(process, broker)
