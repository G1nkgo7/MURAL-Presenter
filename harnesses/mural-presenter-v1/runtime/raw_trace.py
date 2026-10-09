"""原样保存 Provider 正文与流；连接认证头不入盘，签名仅留受限 raw。"""
from __future__ import annotations

import base64
import binascii
import copy
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

from .base64_sanitize import scrub_inline_base64_text


_CURRENT = ContextVar("mural_raw_attempt", default=None)
_AUTH_HEADERS = {"authorization", "proxy-authorization", "api-key", "api_key", "apikey",
                 "x-api-key", "cookie", "set-cookie"}


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def recorded_headers(headers):
    """仅处理 HTTP 连接头，不扫描正文、工具参数或图片。"""
    return {key: value for key, value in headers.items() if str(key).lower() not in _AUTH_HEADERS}


def without_context_signatures(messages):
    """只剥离原生 assistant thinking 的协议签名，不进入工具参数或解释正文。"""
    copied = copy.deepcopy(list(messages))
    for message in copied:
        if message.get("role") != "assistant" or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if isinstance(block, dict) and block.get("type") == "thinking":
                block.pop("signature", None)
                block.pop("signature_metadata", None)
    return copied


def _write(path, payload, *, append=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_EXCL), 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def externalize_binary(root, value):
    root = Path(root)
    if isinstance(value, dict):
        if value.get("type") == "base64" and isinstance(value.get("data"), str):
            return _blob(root, value["data"], str(value.get("media_type") or "application/octet-stream"))
        return {key: externalize_binary(root, item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [externalize_binary(root, item) for item in value]
    if isinstance(value, str) and value.startswith("data:") and ";base64," in value:
        header, data = value.split(";base64,", 1)
        try:
            return _blob(root, data, header[5:])
        except (binascii.Error, ValueError):
            return scrub_inline_base64_text(value)
    if isinstance(value, str):
        return scrub_inline_base64_text(value)
    return value


def _blob(root, data, mime):
    payload = base64.b64decode(data, validate=True)
    return {"type": "blob_ref", "mime": mime, **store_blob(root, payload)}


def store_blob(root, payload):
    """raw、恢复点和训练引用共用一份按内容寻址的不可变字节。"""
    root = Path(root).resolve()
    digest = hashlib.sha256(payload).hexdigest()
    raw_root = root / "_trace/raw"
    if raw_root.resolve() != raw_root.absolute():
        raise ValueError("raw binary directory must not use symlinks")
    raw_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw_root.chmod(0o700)
    target = raw_root / "blobs" / digest
    if target.resolve() != target:
        raise ValueError("raw binary object escapes workspace")
    if not target.exists():
        temporary = target.with_name(f".{digest}.{uuid.uuid4().hex}")
        try:
            _write(temporary, payload)
            try:
                os.link(temporary, target)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
            descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
        raise ValueError("raw binary object hash mismatch")
    return {"path": target.relative_to(root).as_posix(),
            "bytes": len(payload), "sha256": digest}


def checkpoint_images(root, messages, *, restore=False):
    """只转换真实消息图片，不解释工具参数；恢复时验证字节并还原原生输入。"""
    from .vision import image_blocks
    root = Path(root).resolve()
    copied = copy.deepcopy(list(messages))
    for message in copied:
        for block in image_blocks(message.get("content")):
            source = block.get("source") or {}
            if not restore and source.get("type") == "base64":
                blob = store_blob(root, base64.b64decode(source["data"], validate=True))
                block["source"] = {"type": "blob_ref", "media_type": source["media_type"], **blob}
            elif restore and source.get("type") == "blob_ref":
                digest = source.get("sha256", "")
                if not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise ValueError("checkpoint image hash is invalid")
                path = root / "_trace/raw/blobs" / digest
                if source.get("path") != path.relative_to(root).as_posix() or path.resolve() != path:
                    raise ValueError("checkpoint image path is invalid")
                payload = path.read_bytes()
                if hashlib.sha256(payload).hexdigest() != digest:
                    raise ValueError("checkpoint image hash mismatch")
                block["source"] = {"type": "base64", "media_type": source["media_type"],
                                   "data": base64.b64encode(payload).decode("ascii")}
    return copied


class RawAttempt:
    def __init__(self, root, attempt_id, *, emit, metadata):
        if not re.fullmatch(r"attempt-[A-Za-z0-9_-]+", attempt_id):
            raise ValueError("invalid Provider attempt identity")
        self.root = Path(root).resolve()
        self.directory = self.root / "_trace/raw/provider"
        if self.directory.resolve() != self.directory:
            raise ValueError("raw trace escapes workspace")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.parent.chmod(0o700)
        self.attempt_id, self.emit, self.metadata = attempt_id, emit, metadata
        self.started = time.monotonic()
        self.requested = False
        self.anthropic_reasoning_mode = "full"
        self.body = None
        self.status = None
        self.headers = {}
        self.cursor = 0
        self.first_byte_s = None
        self.reasoning = {}
        self.signature = {}
        self.token = None
        self.pending_stream = []
        self.pending_reasoning = {}
        self.reasoning_revision = 0
        self.reasoning_published_at = 0.0

    def __enter__(self):
        self.token = _CURRENT.set(self)
        return self

    def path(self, kind):
        suffix = "jsonl" if kind == "stream" else "json"
        return self.directory / f"{self.attempt_id}.{kind}.{suffix}"

    def ref(self, kind):
        return self.path(kind).relative_to(self.root).as_posix()

    def clean(self, value):
        return externalize_binary(self.root, value)

    def request(self, body, headers, *, method="POST", timeout=None):
        if self.requested:
            raise ValueError("multiple HTTP requests in one Provider attempt")
        self.requested = True
        self.anthropic_reasoning_mode = ("summary" if (body.get("thinking") or {}).get("display") == "summarized" else "full")
        value = self.clean({**self.metadata, "attempt_id": self.attempt_id,
                           "timestamp": now(), "method": method, "headers": recorded_headers(headers),
                           "timeout": timeout, "body": body})
        digest = hashlib.sha256(encode(value)).hexdigest()
        _write(self.path("request"), encode({**value, "sha256": digest}))
        public = {
            key: value for key, value in self.metadata.items()
            if key in {
                "provider_id", "channel_id", "model", "request_kind",
                "request_kind_label", "request_kind_note", "actual_max_tokens",
                "api_format", "streaming",
            }
        }
        self.emit("provider_raw_request", attempt_id=self.attempt_id, raw_ref=self.ref("request"),
                  stream_ref=self.ref("stream"), sha256=digest, **public)

    def response_headers(self, status, headers):
        self.status, self.headers = int(status), recorded_headers(headers)
        if self.first_byte_s is None:
            self.first_byte_s = time.monotonic() - self.started

    def _remember(self, mode, index, text, *, replace=False):
        if not isinstance(text, str) or not text:
            return
        key = (mode, str(index))
        self.reasoning[key] = text if replace else self.reasoning.get(key, "") + text

    def stream(self, event):
        if self.first_byte_s is None:
            self.first_byte_s = time.monotonic() - self.started
        self.cursor += 1
        self.pending_stream.append({"sequence": self.cursor, "timestamp": now(),
            "attempt_id": self.attempt_id, "partial": True, "event": copy.deepcopy(event)})
        self._flush_stream()
        self.observe_stream(event)

    def _flush_stream(self, *, final=False):
        if self.pending_stream:
            clean_rows = [self.clean(row) for row in self.pending_stream]
            _write(self.path("stream"), b"".join(encode(row) for row in clean_rows), append=True)
            for row in clean_rows:
                for mode, index, text in self.reasoning_parts(row["event"]):
                    key = (mode, str(index))
                    self.pending_reasoning[key] = self.pending_reasoning.get(key, "") + text
            self.pending_stream.clear()
            if not final and time.monotonic() - self.reasoning_published_at >= 0.2:
                for (mode, index), content in self.pending_reasoning.items():
                    self.reasoning_revision += 1
                    self.emit("assistant_reasoning_delta", attempt_id=self.attempt_id,
                        revision=self.reasoning_revision, mode=mode, block_index=index, content=content,
                        source="provider_response", partial=True, interrupted=False,
                        raw_ref=self.ref("stream"), provenance=self.metadata)
                self.pending_reasoning.clear()
                self.reasoning_published_at = time.monotonic()

    def reasoning_parts(self, event):
        kind, index = event.get("type", ""), event.get("index", 0)
        block = event.get("content_block", {}) if kind == "content_block_start" else event.get("delta", {})
        if isinstance(block, dict) and isinstance(block.get("thinking"), str):
            yield self.anthropic_reasoning_mode, index, block["thinking"]
        for choice in event.get("choices") or []:
            text = (choice.get("delta") or {}).get("reasoning_content")
            if isinstance(text, str):
                yield "full", choice.get("index", 0), text
        if kind in {"response.reasoning_summary_text.delta", "response.reasoning_text.delta"}:
            if isinstance(event.get("delta"), str):
                yield ("summary" if "summary" in kind else "full",
                       event.get("item_id", event.get("output_index", 0)), event["delta"])

    def observe_stream(self, event):
        for mode, index, text in self.reasoning_parts(event):
            self._remember(mode, index, text)
        block = event.get("content_block", {}) if event.get("type") == "content_block_start" else event.get("delta", {})
        if isinstance(block, dict) and block.get("signature"):
            index = str(event.get("index", 0))
            self.signature[index] = self.signature.get(index, "") + block["signature"]

    def response(self, body):
        self.body = body
        if self.cursor:
            return
        for index, block in enumerate(body.get("content") or []):
            if isinstance(block, dict) and block.get("type") == "thinking":
                self._remember(self.anthropic_reasoning_mode, index, block.get("thinking"), replace=True)
                if block.get("signature"):
                    self.signature[str(index)] = block["signature"]
        for choice in body.get("choices") or []:
            self._remember("full", choice.get("index", 0),
                           (choice.get("message") or {}).get("reasoning_content"), replace=True)
        for item in body.get("output") or []:
            if item.get("type") == "reasoning":
                for mode, key in (("summary", "summary"), ("full", "content")):
                    for index, block in enumerate(item.get(key) or []):
                        if isinstance(block, dict):
                            self._remember(mode, f"{item.get('id', '')}:{index}", block.get("text"), replace=True)

    def __exit__(self, exc_type, exc, traceback):
        try:
            if not self.requested:
                return
            self._flush_stream(final=True)
            response = getattr(exc, "response", None)
            if response is not None:
                self.response_headers(response.status_code, response.headers)
                if getattr(response, "is_stream_consumed", False):
                    try:
                        self.body = response.json()
                    except ValueError:
                        self.body = {"text": response.text}
            interrupted = exc_type is not None
            state = "interrupted" if interrupted else "completed"
            value = self.clean({**self.metadata, "attempt_id": self.attempt_id,
                "timestamp": now(), "status": state, "http_status": self.status,
                "headers": self.headers, "body": self.body,
                "reasoning_available": bool(self.reasoning), "stream_ref": self.ref("stream") if self.cursor else None,
                "stream_events": self.cursor, "partial": interrupted, "interrupted": interrupted,
                "first_byte_s": self.first_byte_s, "elapsed_s": time.monotonic() - self.started,
                "error_type": exc_type.__name__ if exc_type else None})
            digest = hashlib.sha256(encode(value)).hexdigest()
            _write(self.path("response"), encode({**value, "sha256": digest}))
            if self.cursor:
                _write(self.path("stream"), encode({"sequence": self.cursor + 1, "timestamp": now(),
                       "attempt_id": self.attempt_id, "status": state, "partial": interrupted,
                       "interrupted": interrupted}), append=True)
            self.emit("provider_raw_response", attempt_id=self.attempt_id,
                      raw_ref=self.ref("response"), sha256=digest, status=state,
                      reasoning_available=bool(self.reasoning))
            self.emit_reasoning(interrupted)
        finally:
            _CURRENT.reset(self.token)

    def emit_reasoning(self, interrupted):
        for (mode, index), content in self.reasoning.items():
            signature = self.signature.get(index, "").encode()
            self.emit("assistant_reasoning", attempt_id=self.attempt_id,
                mode=mode, block_index=index, source="provider_response", content=content,
                partial=interrupted, interrupted=interrupted, raw_ref=self.ref("response"),
                provenance=self.metadata, signature_metadata={"present": bool(signature),
                "bytes": len(signature), "sha256": hashlib.sha256(signature).hexdigest(),
                "raw_ref": self.ref("stream") if self.cursor else self.ref("response")})


def capture(kind, *args, **kwargs):
    attempt = _CURRENT.get()
    if attempt is not None:
        getattr(attempt, kind)(*args, **kwargs)


async def request_hook(request):
    capture("request", json.loads(request.content), request.headers, method=request.method,
            timeout=request.extensions.get("timeout"))


async def response_hook(response):
    capture("response_headers", response.status_code, response.headers)


async def stream_lines(lines):
    async for line in lines:
        text = line.decode() if isinstance(line, bytes) else line
        if text.startswith("data:") and text[5:].strip() == "[DONE]":
            capture("stream", {"sse_done": True})
        elif text.startswith("data:"):
            try:
                event = json.loads(text[5:])
            except ValueError:
                event = {"unparsed_data": text[5:]}
            if isinstance(event, dict):
                capture("stream", event)
        elif text.strip():
            capture("stream", {"sse_line": text})
        yield line


def recover_raw_attempt(root, source_event, emit):
    """只用已落盘原件补齐进程中断记录，不重发 Provider 请求或修改原始流。"""
    root = Path(root)
    attempt_id = source_event["payload"]["attempt_id"]
    if not re.fullmatch(r"attempt-[A-Za-z0-9_-]+", attempt_id):
        raise ValueError("invalid Provider attempt identity")
    request_path = root / "_trace/raw/provider" / (attempt_id + ".request.json")
    request = json.loads(request_path.read_text())
    metadata = {key: request[key] for key in (
        "provider", "channel_id", "credential_ref", "model", "api_format", "streaming", "endpoint_sha256")
        if key in request}
    attempt = RawAttempt(root, attempt_id, emit=emit, metadata=metadata)
    attempt.anthropic_reasoning_mode = ("summary" if ((request.get("body") or {}).get("thinking") or {}).get("display") == "summarized" else "full")
    stream = attempt.path("stream")
    if stream.is_file():
        with stream.open("rb") as handle:
            for line in handle:
                if not line.endswith(b"\n"):
                    break
                row = json.loads(line)
                if isinstance(row.get("event"), dict):
                    attempt.observe_stream(row["event"])
                    attempt.cursor += 1
    response_path = attempt.path("response")
    if response_path.is_file():
        response = json.loads(response_path.read_text())
        if isinstance(response.get("body"), dict):
            attempt.response(response["body"])
    else:
        response = {**metadata, "attempt_id": attempt_id, "timestamp": now(), "status": "interrupted",
            "http_status": None, "body": None, "stream_ref": attempt.ref("stream") if stream.exists() else None,
            "stream_events": attempt.cursor, "partial": True, "interrupted": True,
            "elapsed_s": None, "error_type": "ProcessInterrupted", "closed_during_workspace_resume": True}
        response["sha256"] = hashlib.sha256(encode(response)).hexdigest()
        _write(response_path, encode(response))
    emit("provider_raw_response", attempt_id=attempt_id, raw_ref=attempt.ref("response"),
         sha256=response["sha256"], status=response["status"], reasoning_available=bool(attempt.reasoning))
    attempt.emit_reasoning(bool(response.get("interrupted")))
