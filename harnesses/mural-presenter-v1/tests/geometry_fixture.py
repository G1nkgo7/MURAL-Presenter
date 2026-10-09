"""测试用作者 CSS 与真实固定 Skill 引用。"""
from pathlib import Path
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading

import pytest

from runtime.shared_objects import SharedSkillStore
from state.workspace import WorkspaceState


_browser_results = {}
_browser_check_lock = threading.Lock()


def _browser_identity():
    keys = ("LD_LIBRARY_PATH", "PLAYWRIGHT_BROWSERS_PATH", "PPT_SKILL_BROWSER_EXE",
            "FONTCONFIG_FILE", "MURAL_RENDER_BROWSER_ENDPOINT")
    return (sys.executable, *(os.environ.get(key, "") for key in keys))


def check_cached_browser_failure() -> None:
    reason = _browser_results.get(_browser_identity(), "")
    if reason:
        message = "browser_preflight_failed: " + reason + (
            "；后续浏览器测试未执行。请核对依赖、端点及当前环境的进程/IPC 权限后重跑，"
            "不要逐例重试或拉长业务超时。")
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError(message)
        pytest.exit(message, returncode=4)


def require_browser(temporary_root: Path) -> None:
    """真正需要浏览器时每个测试进程预检一次；失败中止，不逐例超时或假装跳过即通过。"""
    identity = _browser_identity()
    with _browser_check_lock:
        if identity not in _browser_results:
            _browser_results[identity] = _probe_browser(temporary_root)
    check_cached_browser_failure()


def _probe_browser(temporary_root: Path) -> str:
    script = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1/scripts/_internal/geometry.py"
    temporary_root.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="browser-preflight-", dir=temporary_root) as scratch:
            environment = {**os.environ, "TMPDIR": scratch, "TMP": scratch, "TEMP": scratch,
                           "PYTHONDONTWRITEBYTECODE": "1"}
            with subprocess.Popen([sys.executable, str(script)], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment,
                    start_new_session=True) as process:
                try:
                    stdout, stderr = process.communicate(":root { --canvas-w: 2px; --canvas-h: 2px; }", timeout=8)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
                    return "Chromium 在 8 秒内未完成 CSS/DOM 预检"
            if process.returncode:
                return f"Chromium 退出 {process.returncode}：{stderr[-1200:]}"
            if json.loads(stdout) != geometry(width_px=2, height_px=2):
                return "浏览器未返回预期的实际 DOM 尺寸"
    except (OSError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"
    return ""


def geometry(*, width_px: int, height_px: int) -> dict:
    return {"width_px": width_px, "height_px": height_px,
            "aspect_ratio": width_px / height_px,
            "width_in": width_px / 96, "height_in": height_px / 96}


def write_geometry(root: Path, value: dict) -> dict:
    workspace = WorkspaceState(root)
    skill = Path(__file__).resolve().parents[3] / "skills" / "mural-presenter-v1"
    SharedSkillStore.for_workspace(root).pin(workspace.paths, skill, skill_name="mural-presenter-v1")
    if not (root / "config.json").exists():
        (root / "config.json").write_text(json.dumps({"working_language": "en",
            "presentation_language": "en", "target_page_count": None}), encoding="utf-8")
    (root / "base.css").write_text(
        f":root {{ --canvas-w: {value['width_px']}px; --canvas-h: {value['height_px']}px; }}\n"
        ".slide,.mural-slide {box-sizing:border-box;width:var(--canvas-w);height:var(--canvas-h)}\n",
        encoding="utf-8")
    return value
