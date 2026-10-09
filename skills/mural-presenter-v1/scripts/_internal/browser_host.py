"""一次 Harness 生命周期拥有一个 Chromium；渲染客户端只借用独立 Context。"""
from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit


ENDPOINT_ENV = "MURAL_RENDER_BROWSER_ENDPOINT"


def validate_endpoint(endpoint: str) -> str:
    parts = urlsplit(endpoint)
    if (parts.scheme != "ws" or parts.hostname != "127.0.0.1" or not parts.port
            or not parts.path.startswith("/devtools/browser/") or parts.username
            or parts.password or parts.query or parts.fragment):
        raise ValueError("renderer browser endpoint must be an internal loopback CDP websocket")
    return endpoint


def _kill_group(pid: int | None) -> None:
    if pid:
        try:
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _process_state(pid: int) -> tuple[str, int, str] | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[0], int(fields[1]), fields[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


def _descendants(pid: int, started: str) -> dict[int, str]:
    states = {}
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            state = _process_state(int(entry.name))
            if state:
                states[int(entry.name)] = state
    if pid not in states or states[pid][2] != started:
        return {}
    owned = {pid}
    while children := {child for child, state in states.items() if state[1] in owned} - owned:
        owned.update(children)
    return {child: states[child][2] for child in owned - {pid}}


def _stop_owned(processes: dict[int, str]) -> None:
    def alive(pid):
        state = _process_state(pid)
        return state is not None and state[0] != "Z" and state[2] == processes[pid]
    for pid in processes:
        if alive(pid):
            _kill_group(pid)
    deadline = time.monotonic() + 2
    while any(alive(pid) for pid in processes):
        if time.monotonic() >= deadline:
            raise TimeoutError("owned browser processes did not exit; temporary files retained")
        time.sleep(.01)


class BrowserHost:
    def __init__(self, root: Path, environment: dict[str, str]):
        self.root = root
        self.environment = dict(environment)
        self.environment.pop(ENDPOINT_ENV, None)
        self._lock = threading.Lock()
        self._process = None
        self._host_started = None
        self._owned = {}
        self._stderr = None
        self._temporary = None
        self._browser_pid = None
        self._buffer = b""
        atexit.register(self.close)

    def _receive(self, deadline: float) -> dict:
        while b"\n" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self._process.stdout], [], [], remaining)[0]:
                raise TimeoutError("renderer browser startup/health deadline exceeded")
            chunk = os.read(self._process.stdout.fileno(), 4096)
            if not chunk:
                self._stderr.seek(0)
                raise RuntimeError("renderer browser host exited: " + self._stderr.read(4000).decode(errors="replace"))
            self._buffer += chunk
            if len(self._buffer) > 65536:
                raise RuntimeError("invalid renderer browser host response")
        line, self._buffer = self._buffer.split(b"\n", 1)
        result = json.loads(line)
        validate_endpoint(result["endpoint"])
        self._browser_pid = int(result["browser_pid"])
        self._owned.update(_descendants(self._process.pid, self._host_started))
        return result

    def acquire(self, deadline: float) -> dict:
        if not self._lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise TimeoutError("renderer browser startup lock deadline exceeded")
        try:
            if self._process is not None and self._process.poll() is None:
                try:
                    self._process.stdin.write(b"ping\n")
                    self._process.stdin.flush()
                    return {**self._receive(deadline), "reused": True}
                except (BrokenPipeError, RuntimeError):
                    self._stop()
                except TimeoutError:
                    self._stop()
                    raise
            else:
                self._stop()
            self._stderr = tempfile.TemporaryFile(dir=self.environment["TMPDIR"])
            self._temporary = Path(tempfile.mkdtemp(prefix="chromium-host-", dir=self.environment["TMPDIR"]))
            try:
                self._process = subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), str(self._temporary)], cwd=self.root,
                    env=self.environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=self._stderr, start_new_session=True,
                )
                self._host_started = _process_state(self._process.pid)[2]
                return {**self._receive(deadline), "reused": False}
            except BaseException:
                self._stop()
                raise
        finally:
            self._lock.release()

    def _stop(self) -> None:
        process = self._process
        if process is not None:
            self._owned.update(_descendants(process.pid, self._host_started))
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            try:
                # 先让 EOF 走完整个 finally，避免 SIGTERM 二次打断正在收口的 Context。
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self._owned.update(_descendants(process.pid, self._host_started))
                _stop_owned(self._owned)
                _stop_owned({process.pid: self._host_started})
                process.wait(timeout=5)
            finally:
                process.stdout.close()
        _stop_owned(self._owned)
        self._process = None
        self._host_started = None
        if self._temporary is not None:
            if self._temporary.exists():
                shutil.rmtree(self._temporary)
            self._temporary = None
        self._owned.clear()
        self._browser_pid = None
        self._buffer = b""
        if self._stderr is not None:
            self._stderr.close()
            self._stderr = None

    def close(self) -> None:
        with self._lock:
            self._stop()


def _serve(temporary: str) -> None:
    from playwright.sync_api import sync_playwright
    from render_deck import ensure_browser_libs, reset_child_process_signals

    reset_child_process_signals()
    ensure_browser_libs()
    temporary = Path(temporary)
    if (temporary.is_symlink() or temporary.parent.resolve() != Path(os.environ["TMPDIR"]).resolve()
            or not temporary.name.startswith("chromium-host-") or any(temporary.iterdir())):
        raise ValueError("browser temporary directory must be the empty directory created by its Host")
    closing = threading.Event()
    cleanup_lock = threading.Lock()
    owned = {}
    started = _process_state(os.getpid())[2]

    def terminate(*_args):
        if not closing.is_set():
            raise SystemExit(0)

    def cleanup():
        with cleanup_lock:
            if temporary.exists():
                shutil.rmtree(temporary)

    signal.signal(signal.SIGTERM, terminate)
    try:
        browser_env = {**os.environ, "TMPDIR": str(temporary), "TMP": str(temporary), "TEMP": str(temporary)}
        playwright = sync_playwright().start()
        context = None
        browser_pid = None
        try:
            profile = Path(temporary) / "profile"
            context = playwright.chromium.launch_persistent_context(
                str(profile), headless=True, env=browser_env,
                args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                      "--use-gl=swiftshader", "--enable-unsafe-swiftshader",
                      "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0"],
            )
            cdp = context.browser.new_browser_cdp_session()
            process_info = cdp.send("SystemInfo.getProcessInfo")["processInfo"]
            browser_pid = next(int(item["id"]) for item in process_info if item["type"] == "browser")
            port, endpoint_path = (profile / "DevToolsActivePort").read_text().splitlines()[:2]
            ready = {"endpoint": validate_endpoint(f"ws://127.0.0.1:{port}{endpoint_path}"),
                     "browser_pid": browser_pid, "host_pid": os.getpid()}
            print(json.dumps(ready), flush=True)
            for command in sys.stdin:
                if command.strip() != "ping":
                    break
                cdp.send("Browser.getVersion")
                print(json.dumps(ready), flush=True)
        finally:
            closing.set()
            owned.update(_descendants(os.getpid(), started))
            # 强制关闭也先确认本次子进程退出，再清理自己的精确目录；不绕过清理直接退出。
            def stop_stalled():
                exit_code = 1
                try:
                    _stop_owned(owned)
                    cleanup()
                    exit_code = 0
                finally:
                    os._exit(exit_code)
            timer = threading.Timer(4, stop_stalled)
            timer.daemon = True
            timer.start()
            try:
                try:
                    if context is not None:
                        context.close()
                finally:
                    playwright.stop()
            finally:
                timer.cancel()
                _stop_owned(owned)
                cleanup()
    except BaseException:
        owned.update(_descendants(os.getpid(), started))
        _stop_owned(owned)
        cleanup()
        raise


if __name__ == "__main__":
    _serve(sys.argv[1])
