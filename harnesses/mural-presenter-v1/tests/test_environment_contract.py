from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import json

import pytest

from runtime.production import ProductionSettings, _same_interpreter


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
INFERENCE_PYTHON = REPOSITORY_ROOT / "webui/inference/.venv/bin/python"


def test_formal_inference_runtime_imports_attachment_and_ocr_dependencies() -> None:
    modules = (
        "PIL",
        "pymupdf",
        "cv2",
        "rapidocr_onnxruntime",
        "docx",
        "pptx",
        "openpyxl",
    )
    probe = "; ".join(f"import {module}" for module in modules)

    result = subprocess.run(
        [str(INFERENCE_PYTHON), "-c", probe],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


def test_runtime_identity_compares_the_managed_interpreter() -> None:
    assert _same_interpreter(sys.executable, sys.executable)
    assert not _same_interpreter(sys.executable, "/missing/legacy-normalize/python")


def test_owner_turn_environment_is_independent_and_uses_only_v1_names():
    settings = ProductionSettings.from_environment({'AGENT_MAX_TURNS': '80', 'OWNER_MAX_TURNS': '60'})
    assert (settings.max_turns, settings.owner_max_turns) == (80, 60)
    defaults = ProductionSettings.from_environment({'AGENT_MAX_TURNS': '17', 'CLEAN_CHILD_MAX_TURNS': '1'})
    assert defaults.owner_max_turns == 17


@pytest.mark.parametrize("outcome", ["success", "exit_error", "timeout", "wrong_geometry", "worker_failure"])
def test_browser_preflight_is_once_per_environment_and_fails_without_per_test_retries(tmp_path, monkeypatch, outcome):
    import geometry_fixture as fixture

    monkeypatch.setattr(fixture, "_browser_results", {})
    calls, killed = [], []

    class Process:
        pid = 123456
        returncode = 1 if outcome in {"exit_error", "worker_failure"} else 0

        def __init__(self, command, **options):
            calls.append((command, options))
            self.communications = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def communicate(self, text=None, timeout=None):
            self.communications += 1
            if outcome == "timeout" and self.communications == 1:
                assert timeout == 8
                raise subprocess.TimeoutExpired("test-browser", timeout)
            value = fixture.geometry(width_px=3 if outcome == "wrong_geometry" else 2, height_px=2)
            return json.dumps(value), "synthetic browser failure"

    monkeypatch.setattr(fixture.subprocess, "Popen", Process)
    monkeypatch.setattr(fixture.os, "killpg", lambda pid, signal: killed.append(pid))
    if outcome == "worker_failure":
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as executor:
            with pytest.raises(RuntimeError, match="browser_preflight_failed"):
                executor.submit(fixture.require_browser, tmp_path).result(timeout=1)
    if outcome == "success":
        fixture.require_browser(tmp_path)
        fixture.require_browser(tmp_path)
        assert len(calls) == 1
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "changed-browser"))
        fixture.require_browser(tmp_path)
        assert len(calls) == 2
    else:
        with pytest.raises(pytest.exit.Exception, match="browser_preflight_failed") as error:
            fixture.require_browser(tmp_path)
        assert error.value.returncode == 4
        assert len(calls) == 1
        with pytest.raises(pytest.exit.Exception, match="browser_preflight_failed"):
            fixture.check_cached_browser_failure()
        assert len(calls) == 1
    assert killed == ([Process.pid] if outcome == "timeout" else [])
    assert all(options["start_new_session"] for _, options in calls)
    assert all(Path(options["env"]["TMPDIR"]).parent == tmp_path for _, options in calls)
    assert not list(tmp_path.glob("browser-preflight-*"))


def test_browser_preflight_is_lazy_for_geometry_value_helpers(monkeypatch):
    import geometry_fixture as fixture
    monkeypatch.setattr(fixture.subprocess, "Popen", lambda *a, **kw: pytest.fail("pure helpers must not launch Chromium"))
    assert fixture.geometry(width_px=1600, height_px=900)["aspect_ratio"] == 1600 / 900
