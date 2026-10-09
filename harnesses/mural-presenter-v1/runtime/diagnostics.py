"""受控子进程的原始输出：先持久化诊断，再发布有界摘要；不改写正文。"""
from __future__ import annotations

import codecs
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time

from .base64_sanitize import scrub_inline_base64, scrub_inline_base64_text
from .live_trace import preview_value


OUTPUT_LIMIT = 10 * 1024 * 1024
LINE_LIMIT = 64 * 1024
EXCERPT_LIMIT = 6000
_scope = ContextVar("tool_diagnostics", default=None)


@contextmanager
def command_scope(tool_use_id, metadata, emit):
    token = _scope.set({"tool_use_id": tool_use_id, "metadata": metadata, "emit": emit, "command": 0})
    try:
        yield
    finally:
        _scope.reset(token)


def command_identity():
    """复用当前工具事务的真实身份，避免共享 Renderer 在并发 Owner 间串写身份。"""
    scope = _scope.get()
    return ({key: scope["metadata"].get(key, "")
             for key in ("owner_id", "role", "task_id", "attempt", "segment_id", "turn")}
            if scope else {})


@dataclass
class CommandResult:
    returncode: int | None
    stdout: str
    stderr: str
    diagnostics: dict


class CommandFailed(RuntimeError):
    def __init__(self, result):
        self.result = result
        super().__init__(json.dumps({"returncode": result.returncode, "stdout": result.stdout,
                                    "stderr": result.stderr, "diagnostics": result.diagnostics}, ensure_ascii=False))


class OutputCapture:
    def __init__(self, *, limit=OUTPUT_LIMIT, on_stdout_line=None):
        self.scope = _scope.get()
        self.on_stdout_line = on_stdout_line
        self.limit = limit
        self.pending = {"stdout": "", "stderr": ""}
        self.decoders = {name: codecs.getincrementaldecoder("utf-8")("replace") for name in self.pending}
        self.discard_line = set()
        self.excerpts = {name: "" for name in self.pending}
        self.observed = {name: 0 for name in self.pending}
        self.excerpt_truncated = set()
        self.truncated = False
        self.omission_reasons = set()
        self.capped = False
        self.records = []
        self.path = None
        self.ref = ""
        self.command = 1
        if self.scope:
            self.scope["command"] += 1
            self.command = self.scope["command"]
            root = self.scope["metadata"].get("workspace_root")
            if root:
                self.ref = "_trace/tool-output/" + hashlib.sha256(self.scope["tool_use_id"].encode()).hexdigest() + ".jsonl"
                self.path = Path(root).resolve() / self.ref
                if self.path.resolve() != self.path:
                    raise ValueError("diagnostic path escapes workspace")
                self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, rows, *, terminal=False):
        if not rows or not self.path or (self.capped and not terminal):
            return
        offset = self.path.stat().st_size if self.path.exists() else 0
        data = bytearray()
        for row in rows:
            encoded = (json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), "command_index": self.command, **row}, ensure_ascii=False) + "\n").encode()
            if not terminal and offset + len(data) + len(encoded) > self.limit:
                self.truncated = True
                self.capped = True
                self.omission_reasons.add("output_limit")
                break
            data.extend(encoded)
        if not data:
            return
        with self.path.open("ab") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            cursor = handle.tell()
        self.scope["emit"]({"diagnostic_ref": self.ref, "output_cursor": cursor,
                            "command_index": self.command, "truncated": self.truncated})

    def feed(self, stream, data, *, final=False):
        self.observed[stream] += len(data)
        text = self.pending[stream] + self.decoders[stream].decode(data, final=final)
        if stream == "stdout" and self.on_stdout_line is not None:
            parts = text.split("\n")
            tail = parts.pop()
            lines = [part + "\n" for part in parts]
            self.pending[stream] = tail if not final else ""
            if final and tail:
                lines.append(tail)
        else:
            lines = text.splitlines(keepends=True)
            self.pending[stream] = ""
            if lines and not lines[-1].endswith(("\n", "\r")) and not final:
                self.pending[stream] = lines.pop()
        # 业务收据先交调用方；日志与摘要不改变实际登记内容。
        if stream == "stdout" and self.on_stdout_line is not None:
            if len(self.pending[stream]) > LINE_LIMIT or any(len(line) > LINE_LIMIT for line in lines):
                raise ValueError("command receipt line exceeds 64 KiB; shorten metadata and retry registration")
            for line in lines:
                self.on_stdout_line(line)
        rows = []
        for line in lines:
            if stream in self.discard_line:
                self.discard_line.remove(stream)
                continue
            if len(line) > LINE_LIMIT:
                self.truncated = True
                self.omission_reasons.add("line_limit")
                line = "[overlong output line omitted]\n"
            raw = line
            structured = raw.lstrip().startswith(("{", "["))
            diagnostic = raw.startswith("MURAL_DIAGNOSTIC ")
            receipt_prefix = next((prefix for prefix in ("MURAL_ASSET ", "MURAL_ATTACHMENT ", "MURAL_MINERU_EVENT ") if raw.startswith(prefix)), "")
            value = None
            if structured or diagnostic or receipt_prefix:
                try:
                    value = json.loads(raw.removeprefix("MURAL_DIAGNOSTIC ").removeprefix(receipt_prefix))
                    value = scrub_inline_base64(value)
                except ValueError:
                    pass
            public = scrub_inline_base64_text(raw)
            row = {"stream": stream, "text": public}
            if diagnostic and isinstance(value, dict):
                row["record"] = value
                self.records.append(value)
                self.records = self.records[-100:]
            rows.append(row)
            excerpt = self.excerpts[stream] + public
            if len(excerpt) > EXCERPT_LIMIT:
                self.excerpt_truncated.add(stream)
                excerpt = excerpt[:EXCERPT_LIMIT // 2] + excerpt[-EXCERPT_LIMIT // 2:]
            self.excerpts[stream] = excerpt
        if len(self.pending[stream]) > LINE_LIMIT:
            self.pending[stream] = ""
            self.discard_line.add(stream)
            self.truncated = True
            self.omission_reasons.add("line_limit")
            rows.append({"stream": stream, "text": "[overlong output line omitted]\n"})
        self.write(rows)

    def finish(self, returncode, duration_ms, termination):
        for stream in self.pending:
            self.feed(stream, b"", final=True)
        records = []
        record_bytes = 0
        for record in self.records:
            if record.get("status") == "started":
                continue
            public = preview_value(record, max_string=1200)
            record_bytes += len(json.dumps(public, ensure_ascii=False).encode())
            if record_bytes > 12000:
                break
            records.append(public)
        summary = {"termination": termination, "returncode": returncode, "duration_ms": duration_ms,
                   "observed_bytes": self.observed, "truncated": self.truncated, "limit_bytes": self.limit,
                   "omission_reasons": sorted(self.omission_reasons), "retention": "prefix", "redacted": False,
                   "excerpt_truncated": sorted(self.excerpt_truncated), "excerpt_retention": "head_and_tail",
                   "records": records, "summary_records_omitted": len(self.records) - len(records)}
        if self.ref:
            summary["ref"] = self.ref
        if returncode is not None and returncode < 0:
            summary["signal"] = signal.Signals(-returncode).name
        self.write([{"stream": "meta", "record": {key: value for key, value in summary.items() if key != "records"}}], terminal=True)
        return summary


def run_command(argv, *, cwd, timeout_s, environment, output_limit=OUTPUT_LIMIT, pass_fds=(), on_stdout_line=None):
    capture = OutputCapture(limit=output_limit, on_stdout_line=on_stdout_line)
    identity = command_identity()
    capture.write([{"stream": "meta", "record": {"status": "started", "argv": list(map(str, argv)), **identity}}])
    started = time.monotonic()
    termination = "exited"
    env = {**environment, "PYTHONUNBUFFERED": "1"}
    try:
        process = subprocess.Popen(list(argv), cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, pass_fds=pass_fds)
    except OSError as exc:
        capture.feed("stderr", f"{type(exc).__name__}: {exc}\n".encode())
        summary = capture.finish(None, round((time.monotonic() - started) * 1000), "spawn_failed")
        raise CommandFailed(CommandResult(None, "", capture.excerpts["stderr"], summary)) from exc
    with process:
        with selectors.DefaultSelector() as selector:
            for stream in ("stdout", "stderr"):
                pipe = getattr(process, stream)
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, stream)
            try:
                while selector.get_map():
                    if time.monotonic() - started >= timeout_s and termination != "timeout":
                        termination = "timeout"
                        os.killpg(process.pid, signal.SIGKILL)
                    for key, _mask in selector.select(0.1):
                        data = os.read(key.fd, 65536)
                        if data:
                            capture.feed(key.data, data)
                        else:
                            selector.unregister(key.fileobj)
                    if termination == "timeout" and time.monotonic() - started > timeout_s + 2:
                        break
                process.wait(timeout=max(0.1, timeout_s - (time.monotonic() - started)))
            except subprocess.TimeoutExpired:
                termination = "timeout"
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            except BaseException:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                capture.on_stdout_line = None
                capture.finish(process.returncode, round((time.monotonic() - started) * 1000), "interrupted")
                raise
        code = process.returncode
    if code < 0 and termination == "exited":
        termination = "signal"
    summary = capture.finish(code, round((time.monotonic() - started) * 1000), termination)
    result = CommandResult(code, capture.excerpts["stdout"], capture.excerpts["stderr"], summary)
    if code or termination != "exited":
        raise CommandFailed(result)
    return result
