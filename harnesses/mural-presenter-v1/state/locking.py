"""同 workspace 的状态写入共享一个可重入文件锁，结构事务可原子覆盖多个状态文件。"""
from __future__ import annotations

import fcntl
from pathlib import Path
import threading
import time

_LOCKS: dict[str, "WorkspaceLock"] = {}
_GUARD = threading.Lock()


def workspace_lock(root: Path) -> "WorkspaceLock":
    key = str(Path(root).resolve())
    with _GUARD:
        return _LOCKS.setdefault(key, WorkspaceLock(Path(key) / "_state/state.lock"))


def page_render_lock(root: Path, page: int) -> "WorkspaceLock":
    path = Path(root).resolve() / "_state" / f"render-page-{int(page):02d}.lock"
    with _GUARD:
        return _LOCKS.setdefault(str(path), WorkspaceLock(path, timeout_s=1800))


class WorkspaceLock:
    def __init__(self, path: Path, *, timeout_s: float | None = None) -> None:
        self.path = path
        self.timeout_s = timeout_s
        self.thread_lock = threading.RLock()
        self.depth = 0
        self.handle = None

    def __enter__(self):
        deadline = None if self.timeout_s is None else time.monotonic() + self.timeout_s
        if deadline is None:
            self.thread_lock.acquire()
        elif not self.thread_lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise TimeoutError("page render transaction lock timed out")
        try:
            if not self.depth:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.handle = self.path.open("a+b")
                while True:
                    try:
                        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if deadline is not None and time.monotonic() >= deadline:
                            raise TimeoutError("page render transaction lock timed out")
                        time.sleep(0.02)
            self.depth += 1
            return self
        except BaseException:
            if self.handle is not None:
                self.handle.close()
                self.handle = None
            self.thread_lock.release()
            raise

    def __exit__(self, *_exc):
        self.depth -= 1
        if not self.depth:
            self.handle.close()
            self.handle = None
        self.thread_lock.release()
