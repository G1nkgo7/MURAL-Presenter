"""Credential-free local Chromium broker for deterministic deck rendering."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

_ENV_ALLOW = {
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
    "CLEAN_RENDER_CONCURRENCY_FILE",
    "CLEAN_RENDER_POOL_MAX_WORKERS",
    "CLEAN_RENDER_STATUS_FILE",
    "CLEAN_RENDER_JOB_TIMEOUT",
    "CLEAN_RENDER_CLEANUP_GRACE",
}


def _render_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key in _ENV_ALLOW}


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False),
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_concurrency_target(
    control_path: Path | None,
    fallback: int,
    maximum: int,
) -> int:
    """Read a live target; zero pauses new work without cancelling active jobs."""
    fallback = max(0, min(int(fallback), int(maximum)))
    if control_path is None:
        return fallback
    try:
        value = int(control_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return fallback
    return max(0, min(value, int(maximum)))


def _response_path(request: Path) -> Path:
    name = request.name
    for suffix in (".request.json", ".processing.json"):
        if name.endswith(suffix):
            return request.with_name(name[: -len(suffix)] + ".response.json")
    raise ValueError(f"invalid render request filename: {name}")


def _render_artifacts_ready(cmd: list[object], started_at: float) -> tuple[bool, str]:
    """Verify that a renderer produced a fresh, internally consistent result."""
    try:
        rendered = [str(part) for part in cmd]
        out_dir = Path(rendered[3])
        manifest = out_dir / "render.json"
        if not manifest.is_file() or manifest.stat().st_mtime < started_at - 1:
            return False, "fresh render.json is missing"
        meta = json.loads(manifest.read_text(encoding="utf-8"))
        pages = {
            int(item["page"])
            for item in meta.get("pages", [])
            if isinstance(item, dict) and "page" in item
        }
        if "--all" in rendered:
            count = int(meta.get("n_pages", 0))
            expected = set(range(1, count + 1))
            complete = (
                count > 0
                and pages == expected
                and (out_dir / "contact-sheet.png").is_file()
                and all(
                    (out_dir / f"slide_{number:02d}.png").stat().st_size > 0
                    for number in expected
                )
            )
            return complete, f"all-pages={count}"
        page_index = rendered.index("--page") + 1
        page = int(rendered[page_index])
        png = out_dir / f"slide_{page:02d}.png"
        complete = page in pages and png.is_file() and png.stat().st_size > 0
        return complete, f"page={page}"
    except Exception as exc:  # noqa: BLE001
        return False, f"artifact verification failed: {type(exc).__name__}: {exc}"


def _stop_process_group(process: subprocess.Popen) -> tuple[str, str]:
    """Stop only the renderer process group and collect its small text output."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        return process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return process.communicate()


def _run_render_command(cmd: list[object]) -> dict[str, object]:
    """Run until exit, verified artifacts plus cleanup grace, or a high wall clock."""
    rendered = [str(part) for part in cmd]
    started_at = time.time()
    timeout_s = max(60, int(os.environ.get("CLEAN_RENDER_JOB_TIMEOUT", "1800")))
    cleanup_grace_s = max(
        1, int(os.environ.get("CLEAN_RENDER_CLEANUP_GRACE", "15"))
    )
    process = subprocess.Popen(
        rendered,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_render_env(),
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout_s
    artifacts_ready_at: float | None = None
    artifact_detail = ""
    while process.poll() is None:
        artifacts_ready, artifact_detail = _render_artifacts_ready(cmd, started_at)
        if artifacts_ready:
            artifacts_ready_at = artifacts_ready_at or time.monotonic()
            if time.monotonic() - artifacts_ready_at >= cleanup_grace_s:
                stdout, stderr = _stop_process_group(process)
                note = (
                    "render artifacts verified; terminated stalled browser cleanup "
                    f"after {cleanup_grace_s}s ({artifact_detail})"
                )
                return {
                    "returncode": 0,
                    "stdout": stdout,
                    "stderr": f"{stderr.rstrip()}\n{note}".lstrip(),
                    "artifact_recovery": True,
                }
        if time.monotonic() >= deadline:
            stdout, stderr = _stop_process_group(process)
            artifacts_ready, artifact_detail = _render_artifacts_ready(cmd, started_at)
            if artifacts_ready:
                return {
                    "returncode": 0,
                    "stdout": stdout,
                    "stderr": (
                        f"{stderr.rstrip()}\nrender wall clock reached {timeout_s}s, "
                        f"but fresh artifacts are complete ({artifact_detail})"
                    ).lstrip(),
                    "artifact_recovery": True,
                }
            return {
                "returncode": 124,
                "stdout": stdout,
                "stderr": (
                    f"{stderr.rstrip()}\nrender broker timeout after {timeout_s}s: "
                    f"{artifact_detail}"
                ).lstrip(),
            }
        time.sleep(0.1)

    stdout, stderr = process.communicate()
    return {
        "returncode": int(process.returncode or 0),
        "stdout": stdout,
        "stderr": stderr,
    }


def _handle(request: Path) -> None:
    response = _response_path(request)
    try:
        payload = json.loads(request.read_text(encoding="utf-8"))
        cmd = payload.get("cmd")
        if (
            not isinstance(cmd, list)
            or len(cmd) < 5
            or Path(str(cmd[1])).name != "render_deck.py"
        ):
            raise ValueError("invalid render broker command")
        record = _run_render_command(cmd)
    except Exception as exc:  # noqa: BLE001
        record = {
            "returncode": 1,
            "stdout": "",
            "stderr": f"render broker error: {type(exc).__name__}: {exc}",
        }
    _atomic_json(response, record)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m core.render_broker BROKER_DIR", file=sys.stderr)
        return 2
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    initial = max(1, int(os.environ.get("CLEAN_RENDER_CONCURRENCY", "1")))
    maximum = max(
        initial,
        int(os.environ.get("CLEAN_RENDER_POOL_MAX_WORKERS", str(initial))),
    )
    control_value = os.environ.get("CLEAN_RENDER_CONCURRENCY_FILE", "").strip()
    control_path = Path(control_value) if control_value else None
    status_value = os.environ.get("CLEAN_RENDER_STATUS_FILE", "").strip()
    status_path = Path(status_value) if status_value else root / "status.json"
    (root / "ready").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "initial": initial,
                "maximum": maximum,
                "control_file": str(control_path) if control_path else None,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    active = {}
    last_status_at = 0.0
    try:
        with ThreadPoolExecutor(max_workers=maximum) as executor:
            while not (root / "stop").exists() or active:
                for future in tuple(active):
                    if future.done():
                        processing = active.pop(future)
                        try:
                            future.result()
                        finally:
                            processing.unlink(missing_ok=True)
                concurrency = _read_concurrency_target(
                    control_path,
                    initial,
                    maximum,
                )
                if not (root / "stop").exists():
                    capacity = max(0, concurrency - len(active))
                    for request in sorted(root.glob("*.request.json"))[:capacity]:
                        processing = request.with_name(
                            request.name.replace(".request.json", ".processing.json")
                        )
                        try:
                            request.replace(processing)
                        except FileNotFoundError:
                            continue
                        active[executor.submit(_handle, processing)] = processing
                now = time.monotonic()
                if now - last_status_at >= 1:
                    _atomic_json(
                        status_path,
                        {
                            "pid": os.getpid(),
                            "state": (
                                "draining"
                                if (root / "stop").exists()
                                else "running"
                            ),
                            "target": concurrency,
                            "active": len(active),
                            "queued": sum(1 for _ in root.glob("*.request.json")),
                            "maximum": maximum,
                            "control_file": (
                                str(control_path) if control_path else None
                            ),
                            "updated_at": time.strftime(
                                "%Y-%m-%dT%H:%M:%SZ",
                                time.gmtime(),
                            ),
                        },
                    )
                    last_status_at = now
                if active:
                    wait(tuple(active), timeout=0.03, return_when=FIRST_COMPLETED)
                else:
                    time.sleep(0.03)
    finally:
        (root / "ready").unlink(missing_ok=True)
        _atomic_json(
            status_path,
            {
                "pid": os.getpid(),
                "state": "stopped",
                "target": 0,
                "active": 0,
                "queued": sum(1 for _ in root.glob("*.request.json")),
                "maximum": maximum,
                "control_file": str(control_path) if control_path else None,
                "updated_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ",
                    time.gmtime(),
                ),
            },
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
