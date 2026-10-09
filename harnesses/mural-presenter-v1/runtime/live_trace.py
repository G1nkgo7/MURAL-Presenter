"""面向 WebUI 的追加式实时轨迹。

该日志保存实际角色上下文、消息、API 可观测 reasoning、工具事务和 Provider 状态。
full reasoning 与显式 summary 分开；正文不改写，连接凭证不传入此边界。
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import threading
import time
from typing import Iterable, Mapping

from .base64_sanitize import scrub_inline_base64_text


LIVE_EVENT_SCHEMA = "mural.live-event.v1"
LIVE_EVENT_TYPES = frozenset(
    {
        "agent_context",
        "assistant_reasoning",
        "assistant_reasoning_delta",
        "provider_raw_request",
        "provider_raw_response",
        "trace_projection_failed",
        "agent_started",
        "agent_completed",
        "agent_failed",
        "agent_context_compacted",
        "agent_context_compaction_failed",
        "agent_segment_rolled_over",
        "agent_visual_context_released",
        "agent_turn_completed",
        "deck_completion_checked",
        "slide_preparation_completed",
        "slide_preparation_failed",
        "page_render_completed",
        "agent_empty_response_recovered",
        "agent_empty_tool_call_recovered",
        "agent_no_action_response",
        "agent_repeated_evidence_warning",
        "language_drift",
        "assistant_analysis_summary",
        "assistant_message",
        "assistant_message_delta",
        "assistant_analysis_summary_delta",
        "tool_use",
        "tool_result",
        "tool_output",
        "provider_started",
        "provider_succeeded",
        "provider_failed",
        "provider_failover",
        "mineru_started",
        "mineru_failed",
        "mineru_failover",
        "mineru_succeeded",
        "attachment_parse_queued",
        "attachment_parse_started",
        "attachment_parse_finished",
        "image_handoff_ready",
        "resource_wait_started",
        "resource_wait_completed",
        "resource_wait_failed",
        # 外部视觉 Critic 与制作角色复用真实事件。
        "critic_started",
        "critic_reused",
        "critic_failed",
        "critic_completed",
        "content_qc_started",
        "content_qc_completed",
        "content_qc_failed",
    }
)

_OMITTED_TOOL_FIELDS = frozenset(
    {
        "content",
        "html",
        "css",
        "javascript",
        "script",
        "source",
        "data",
        "image",
        "bytes",
        "base64",
        "request",
        "messages",
        "system",
        "prompt",
        "body",
    }
)


class LiveTraceError(RuntimeError):
    """表示实时轨迹无法满足持久化或事件合同。"""


class PublicResponseStream:
    """按请求发布节流后的累计预览；不接收工具参数或私有推理。"""

    def __init__(self, emit, attempt_id: str, *, monotonic=time.monotonic):
        self.emit = emit
        self.attempt_id = attempt_id
        self.monotonic = monotonic
        self.blocks: dict[tuple[str, str], str] = {}
        self.published: dict[str, str] = {}
        self.last_emit: dict[str, float] = {}
        self.revision = 0

    def add(self, kind: str, block: str, text: str) -> None:
        if kind not in {"message", "summary"} or not text:
            return
        key = (kind, str(block))
        self.blocks[key] = self.blocks.get(key, "") + text
        self._publish(kind)

    def finish(self) -> None:
        for kind in ("message", "summary"):
            self._publish(kind, complete=True)

    def _publish(self, kind: str, *, complete: bool = False) -> None:
        now = self.monotonic()
        if not complete and now - self.last_emit.get(kind, -1e9) < 0.2:
            return
        value = "\n\n".join(text for (k, _), text in self.blocks.items() if k == kind)
        if not value.strip() or value == self.published.get(kind):
            return
        self.revision += 1
        self.emit(
            "assistant_message_delta" if kind == "message" else "assistant_analysis_summary_delta",
            attempt_id=self.attempt_id,
            revision=self.revision,
            **{kind: value},
        )
        self.published[kind] = value
        self.last_emit[kind] = now


def _observe_artifacts(root, arguments, payload):
    records = {}
    def visit(value):
        if isinstance(value, str):
            try:
                visit(json.loads(value))
            except (ValueError, RecursionError):
                pass
        elif isinstance(value, dict):
            for key, item in value.items():
                if key in {"path", "html_path", "png_path", "image_path", "relative_path"} and isinstance(item, str):
                    path = (root / item).resolve()
                    if (path.is_file() and path.is_relative_to(root)
                            and not path.is_relative_to(root / "_trace")
                            and not path.is_relative_to(root / "_state")):
                        data = path.read_bytes()
                        relative = path.relative_to(root).as_posix()
                        records[relative] = {"path": relative,
                            "mime": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                            "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                            "tool_use_id": payload["tool_use_id"], "action": "observed",
                            "page": value.get("page")}
                elif isinstance(item, (dict, list)):
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(arguments)
    visit(payload.get("result"))
    return list(records.values())


def _acquire_journal_lock(descriptor, path, timeout_s=120.0):
    started = time.monotonic()
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = timeout_s - (time.monotonic() - started)
            if remaining <= 0:
                raise LiveTraceError(f"live_trace_lock_timeout waited_s={time.monotonic() - started:.3f} lock_path={path}")
            time.sleep(min(0.05, remaining))


class LiveEventJournal:
    """按全局 sequence 追加并同步一个可跨进程恢复的 JSONL journal。"""

    def __init__(self, path: str | Path, *, projector=None, full_payload=False) -> None:
        """读取已有完整事件；残缺尾行会在下一次追加前被隔离。"""
        self.path = Path(path)
        self.projector = projector
        self.full_payload = full_payload or projector is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._tool_arguments = {}
        self._sequence = 0
        self._event_ids: set[str] = set()
        self._loaded_size = 0
        self._pending = b""
        self._identity: tuple[int, int] | None = None
        descriptor = os.open(
            self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644
        )
        acquired = False
        try:
            _acquire_journal_lock(descriptor, self.path)
            acquired = True
            self._refresh(descriptor)
        finally:
            if acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @property
    def sequence(self) -> int:
        """返回当前进程已观察到的最大持久 sequence。"""
        with self._lock:
            return self._sequence

    def append(
        self,
        *,
        event_type: str,
        agent_id: str,
        role: str,
        turn: int = 0,
        segment_id: str = "",
        payload: Mapping[str, object] | None = None,
        event_id: str = "",
        timestamp: str = "",
    ) -> dict[str, object] | None:
        """持久化一个公开事件；同 ``event_id`` 的恢复重放不会重复追加。"""
        normalized_type = str(event_type or "").strip()
        if normalized_type not in LIVE_EVENT_TYPES:
            raise LiveTraceError(f"unsupported live event type {normalized_type!r}")
        normalized_agent = str(agent_id or "").strip()
        normalized_role = str(role or "").strip().lower()
        if not normalized_agent or not normalized_role:
            raise LiveTraceError("live event requires agent_id and role")
        try:
            normalized_turn = max(0, int(turn or 0))
        except (TypeError, ValueError) as exc:
            raise LiveTraceError("live event turn must be an integer") from exc
        public_payload = copy.deepcopy(dict(payload or {}))
        if normalized_type == "agent_context" and "messages" in public_payload:
            from .raw_trace import without_context_signatures
            public_payload["messages"] = without_context_signatures(public_payload["messages"])
        if normalized_type in {"assistant_reasoning", "assistant_reasoning_delta"}:
            public_payload.pop("signature", None)
            public_payload.pop("thinking_signature", None)
        if normalized_type in {"agent_context", "assistant_reasoning", "assistant_reasoning_delta", "provider_raw_request", "provider_raw_response"} or (
                self.full_payload and normalized_type in {"assistant_message", "tool_use", "tool_result"}):
            from .raw_trace import externalize_binary
            public_payload = externalize_binary(self.path.parent.parent, public_payload)
        else:
            public_payload = preview_value(public_payload)
        if not isinstance(public_payload, dict):
            raise LiveTraceError("live event payload must be an object")
        stable_id = str(event_id or "").strip() or _derived_event_id(
            normalized_type,
            normalized_agent,
            normalized_role,
            normalized_turn,
            str(segment_id or ""),
            public_payload,
        )

        with self._lock:
            descriptor = os.open(
                self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644
            )
            acquired = False
            try:
                _acquire_journal_lock(descriptor, self.path)
                acquired = True
                self._refresh(descriptor)
                if stable_id in self._event_ids:
                    return None
                self._separate_incomplete_tail(descriptor)
                sequence = self._sequence + 1
                event = {
                    "schema": LIVE_EVENT_SCHEMA,
                    "sequence": sequence,
                    "timestamp": _public_timestamp(timestamp),
                    "event_id": stable_id,
                    "agent_id": normalized_agent,
                    "role": normalized_role,
                    "turn": normalized_turn,
                    "segment_id": str(segment_id or ""),
                    "event_type": normalized_type,
                    "payload": public_payload,
                }
                encoded = (
                    json.dumps(
                        event,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("utf-8")
                _write_all(descriptor, encoded)
                os.fsync(descriptor)
                self._sequence = sequence
                self._event_ids.add(stable_id)
                self._loaded_size = os.fstat(descriptor).st_size
                self._pending = b""
                if self.projector is not None:
                    try:
                        self.projector(event)
                    except Exception as exc:
                        diagnostic = {**event, "sequence": sequence + 1,
                            "event_id": "projection:" + stable_id, "event_type": "trace_projection_failed",
                            "payload": {"last_sequence": sequence, "error_type": type(exc).__name__,
                                        "error_summary": str(preview_value(str(exc)))[:500]}}
                        _write_all(descriptor, (json.dumps(diagnostic, ensure_ascii=False) + "\n").encode())
                        os.fsync(descriptor)
                        self._sequence += 1
                        self._event_ids.add(diagnostic["event_id"])
                        self._loaded_size = os.fstat(descriptor).st_size
                return copy.deepcopy(event)
            finally:
                if acquired:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def record_runtime_event(
        self, event: Mapping[str, object]
    ) -> tuple[dict[str, object], ...]:
        """把实际运行事件写入唯一追加日志；失败可能同时产生 failover 事实。"""
        source_type = str(event.get("event_type") or "")
        mapped = {
            "provider_attempt_started": "provider_started",
            "provider_attempt_succeeded": "provider_succeeded",
            "provider_attempt_failed": "provider_failed",
        }.get(source_type, source_type)
        if mapped not in LIVE_EVENT_TYPES:
            return ()
        agent_id = str(event.get("agent_id") or "")
        role = str(event.get("role") or "")
        turn = _safe_int(event.get("turn"))
        segment_id = str(event.get("segment_id") or "")
        structural = {
            "event_type",
            "agent_id",
            "role",
            "turn",
            "segment_id",
            "timestamp",
        }
        payload = {
            str(key): copy.deepcopy(value)
            for key, value in event.items()
            if key not in structural
        }
        if mapped == "assistant_message":
            payload = {"message": str(event.get("message") or "")}
        elif mapped == "assistant_analysis_summary":
            payload = {"summary": str(event.get("summary") or "")}
        elif mapped == "agent_no_action_response":
            payload = {
                key: copy.deepcopy(event[key])
                for key in (
                    "reason", "stop_reason", "retry_allowed", "recovery_action",
                    "thinking", "max_tokens", "configuration_changed",
                    "recovery_configuration",
                )
                if key in event
            }
        if mapped in {"assistant_message", "assistant_analysis_summary"}:
            if event.get("attempt_id"):
                payload["attempt_id"] = str(event["attempt_id"])
            for key in (
                "model", "request_kind", "request_kind_label", "request_kind_note",
                "actual_max_tokens", "language_policy_version",
                "working_language", "presentation_language",
            ):
                if key in event:
                    payload[key] = copy.deepcopy(event[key])
        source_id = _runtime_source_id(event, mapped)
        written: list[dict[str, object]] = []
        primary = self.append(
            event_type=mapped,
            agent_id=agent_id,
            role=role,
            turn=turn,
            segment_id=segment_id,
            payload=payload,
            event_id=source_id,
            timestamp=str(event.get("timestamp") or ""),
        )
        if primary is not None:
            written.append(primary)
        if source_type == "provider_attempt_started":
            current = str(event.get("failover_from_channel_id") or "")
            following = str(event.get("channel_id") or "")
            if (
                current and following
                and following != current
            ):
                failover = self.append(
                    event_type="provider_failover",
                    agent_id=agent_id,
                    role=role,
                    turn=turn,
                    segment_id=segment_id,
                    payload={
                        "attempt_id": str(event.get("attempt_id") or ""),
                        "previous_attempt_id": str(event.get("previous_attempt_id") or ""),
                        "from_channel_id": current,
                        "to_channel_id": following,
                        "recovery_boundary": "last_closed_tool_transaction",
                    },
                    event_id=(
                        f"provider:{event.get('attempt_id')}:provider_failover"
                    ),
                    timestamp=str(event.get("timestamp") or ""),
                )
                if failover is not None:
                    written.append(failover)
        return tuple(written)

    def record_tool_event(
        self, event: Mapping[str, object]
    ) -> dict[str, object] | None:
        """把 ToolRuntime 事件投影为参数/结果可展示的闭合事务。"""
        event_type = str(event.get("event_type") or "")
        if event_type not in {"tool_use", "tool_result", "tool_output"}:
            return None
        tool_use_id = str(event.get("tool_use_id") or "")
        tool_name = str(event.get("tool_name") or event.get("name") or "tool")
        payload: dict[str, object] = {
            "tool_use_id": tool_use_id,
            "name": tool_name,
            "task_id": str(event.get("task_id") or ""),
            "attempt": _safe_int(event.get("attempt")),
            "wave_id": str(event.get("wave_id") or ""),
            "group_id": str(event.get("group_id") or ""),
            "pages": list(event.get("pages") or []),
            "model": str(event.get("model") or ""),
            "request_kind": str(event.get("request_kind") or ""),
            "request_kind_label": str(event.get("request_kind_label") or ""),
            "request_kind_note": str(event.get("request_kind_note") or ""),
            "actual_max_tokens": _safe_int(event.get("actual_max_tokens")),
        }
        if event_type == "tool_use":
            self._tool_arguments[tool_use_id] = event.get("arguments", {})
            payload["arguments"] = (event.get("arguments") if self.full_payload
                                    else summarize_tool_arguments(event.get("arguments")))
            payload["arguments_summary"] = summarize_tool_arguments(event.get("arguments"))
        elif event_type == "tool_output":
            payload.update({key: event.get(key) for key in ("diagnostic_ref", "output_cursor", "command_index", "truncated")})
        else:
            payload.update(
                {
                    "ok": bool(event.get("ok")),
                    "result": event.get("content") if self.full_payload else summarize_tool_result(event.get("content")),
                    "duration_ms": _safe_int(event.get("duration_ms")),
                    "error_code": str(event.get("error_code") or ""),
                }
            )
        if event_type == "tool_result" and self.full_payload:
            payload["artifacts"] = _observe_artifacts(
                self.path.parent.parent, self._tool_arguments.pop(tool_use_id, {}), payload)
        return self.append(
            event_type=event_type,
            agent_id=str(event.get("owner_id") or event.get("agent_id") or ""),
            role=str(event.get("role") or ""),
            turn=_safe_int(event.get("turn")),
            segment_id=str(event.get("segment_id") or ""),
            payload=payload,
            event_id=f"tool:{tool_use_id}:{event_type}" + (f":{event.get('output_cursor')}" if event_type == "tool_output" else ""),
            timestamp=str(event.get("timestamp") or ""),
        )

    def close_interrupted_events(self) -> tuple[dict[str, object], ...]:
        """只从追加日志闭合上个进程遗留的 Provider、工具、Critic 和 Agent。"""
        open_tools: dict[str, dict[str, object]] = {}
        open_agents: dict[tuple[str, str], dict[str, object]] = {}
        open_providers: dict[str, dict[str, object]] = {}
        open_critics: dict[tuple[str, str, str], dict[str, object]] = {}
        raw_requests = []
        for event in _iter_live_rows(self.path):
            event_type = str(event.get("event_type") or "")
            payload = event.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            if event_type == "provider_raw_request":
                raw_requests.append(event)
            elif event_type == "provider_started":
                open_providers[str(payload["attempt_id"])] = event
            elif event_type in {"provider_succeeded", "provider_failed"}:
                open_providers.pop(str(payload["attempt_id"]), None)
            if event_type == "tool_use":
                tool_use_id = str(payload.get("tool_use_id") or "")
                if tool_use_id:
                    open_tools[tool_use_id] = event
            elif event_type == "tool_result":
                open_tools.pop(str(payload.get("tool_use_id") or ""), None)
            if event_type in {"critic_started", "critic_reused", "critic_completed", "critic_failed"}:
                review_key = tuple(str(payload.get(key) or "") for key in ("inspection_id", "owner_id", "tool_use_id"))
                if review_key[0]:
                    if event_type == "critic_started" or (event_type == "critic_reused" and payload.get("pending") is True):
                        open_critics[review_key] = event
                    elif event_type in {"critic_completed", "critic_failed"}:
                        open_critics.pop(review_key, None)
            key = (
                str(event.get("agent_id") or ""),
                str(event.get("segment_id") or ""),
            )
            if event_type == "agent_started" and all(key):
                open_agents[key] = event
            elif event_type in {"agent_completed", "agent_failed"}:
                open_agents.pop(key, None)

        written: list[dict[str, object]] = []
        if raw_requests:
            from .raw_trace import recover_raw_attempt
            for source in raw_requests:
                def emit(kind, **payload):
                    written.extend(self.record_runtime_event({
                        "event_type": kind, "agent_id": source["agent_id"], "role": source["role"],
                        "segment_id": source["segment_id"], "turn": source["turn"], **payload}))
                recover_raw_attempt(self.path.parent.parent, source, emit)
        for attempt_id, source in open_providers.items():
            written.extend(self.record_runtime_event({
                **source["payload"],
                **{key: source[key] for key in ("agent_id", "role", "turn", "segment_id")},
                "event_type": "provider_attempt_failed", "attempt_id": attempt_id,
                "error_kind": "process_interrupted", "timeout_phase": "", "retryable": True,
                "next_channel_id": "", "recovery_boundary": "last_closed_tool_transaction",
                "partial_response_discarded": True, "segment_rollover_required": False,
                "closed_during_workspace_resume": True,
            }))
        for tool_use_id, source in sorted(
            open_tools.items(), key=lambda item: _safe_int(item[1].get("sequence"))
        ):
            payload = source.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            result = self.append(
                event_type="tool_result",
                agent_id=str(source.get("agent_id") or ""),
                role=str(source.get("role") or ""),
                turn=_safe_int(source.get("turn")),
                segment_id=str(source.get("segment_id") or ""),
                payload={
                    "tool_use_id": tool_use_id,
                    "name": str(payload.get("name") or "tool"),
                    "ok": False,
                    "result": "previous process ended before tool_result was recorded",
                    "duration_ms": 0,
                    "error_code": "process_interrupted",
                    "closed_during_workspace_resume": True,
                },
                event_id=f"tool:{tool_use_id}:tool_result",
            )
            if result is not None:
                written.append(result)
        for source in sorted(open_critics.values(), key=lambda event: _safe_int(event.get("sequence"))):
            result = self.append(
                event_type="critic_failed",
                agent_id=str(source["agent_id"]), role=str(source["role"]),
                turn=_safe_int(source.get("turn")), segment_id=str(source.get("segment_id") or ""),
                payload={**source["payload"], "completed": False, "pending": False, "body": "",
                    "incomplete_reason": "process_interrupted: previous process ended before Critic completion",
                    "cache_hit": source["event_type"] == "critic_reused",
                    "closed_during_workspace_resume": True},
                event_id=f"critic:{source['event_id']}:process_interrupted",
            )
            if result is not None:
                written.append(result)
        for (_agent_id, _segment_id), source in sorted(
            open_agents.items(), key=lambda item: _safe_int(item[1].get("sequence"))
        ):
            result = self.append(
                event_type="agent_failed",
                agent_id=str(source.get("agent_id") or ""),
                role=str(source.get("role") or ""),
                turn=_latest_agent_turn(self.path, source),
                segment_id=str(source.get("segment_id") or ""),
                payload={
                    "error_type": "ProcessInterrupted",
                    "error_summary": "previous process ended before agent completion",
                    "closed_during_workspace_resume": True,
                },
            )
            if result is not None:
                written.append(result)
        return tuple(written)

    def _refresh(self, descriptor: int) -> None:
        """在文件锁内吸收其他进程已经追加的完整事件。"""
        stat = os.fstat(descriptor)
        identity = (int(stat.st_dev), int(stat.st_ino))
        if self._identity != identity or stat.st_size < self._loaded_size:
            self._identity = identity
            self._loaded_size = 0
            self._pending = b""
            self._sequence = 0
            self._event_ids.clear()
        if stat.st_size == self._loaded_size:
            return
        chunk = os.pread(
            descriptor, stat.st_size - self._loaded_size, self._loaded_size
        )
        self._loaded_size = stat.st_size
        data = self._pending + chunk
        self._pending = b""
        if data and not data.endswith(b"\n"):
            complete, separator, pending = data.rpartition(b"\n")
            if separator:
                data = complete + b"\n"
                self._pending = pending
            else:
                self._pending = data
                data = b""
        for raw in data.splitlines():
            try:
                item = json.loads(raw)
            except (UnicodeDecodeError, ValueError, TypeError):
                continue
            if not isinstance(item, dict) or item.get("schema") != LIVE_EVENT_SCHEMA:
                continue
            sequence = _safe_int(item.get("sequence"))
            event_id = str(item.get("event_id") or "")
            if sequence > self._sequence:
                self._sequence = sequence
            if event_id:
                self._event_ids.add(event_id)

    def _separate_incomplete_tail(self, descriptor: int) -> None:
        """不改写残缺字节，只追加换行，保证下一条事件仍可独立恢复。"""
        if not self._pending:
            return
        _write_all(descriptor, b"\n")
        os.fsync(descriptor)
        self._loaded_size = os.fstat(descriptor).st_size
        self._pending = b""


def read_live_events(
    path: str | Path,
    *,
    cursor: int = 0,
    limit: int = 500,
) -> dict[str, object]:
    """读取 ``cursor`` 之后的完整事件，安全忽略并发写入中的残缺尾行。"""
    target = Path(path)
    normalized_cursor = max(0, int(cursor or 0))
    normalized_limit = min(2000, max(1, int(limit or 500)))
    events: list[dict[str, object]] = []
    last_sequence = normalized_cursor
    more = False
    if target.is_file():
        with target.open("rb") as handle:
            for raw in handle:
                if not raw.endswith(b"\n"):
                    continue
                try:
                    item = json.loads(raw)
                except (UnicodeDecodeError, ValueError, TypeError):
                    continue
                if not isinstance(item, dict) or item.get("schema") != LIVE_EVENT_SCHEMA:
                    continue
                sequence = _safe_int(item.get("sequence"))
                if sequence <= normalized_cursor:
                    continue
                if len(events) >= normalized_limit:
                    more = True
                    break
                events.append(item)
                last_sequence = max(last_sequence, sequence)
    return {
        "schema": LIVE_EVENT_SCHEMA,
        "cursor": normalized_cursor,
        "next_cursor": last_sequence,
        "has_more": more,
        "events": events,
    }


def preview_value(
    value: object,
    *,
    max_string: int = 6000,
    max_depth: int = 8,
    max_items: int = 100,
    _depth: int = 0,
) -> object:
    """只限制展示负载；不按字段名或文本内容猜测敏感信息。"""
    if _depth >= max_depth:
        return "[nested value omitted]"
    if isinstance(value, Mapping):
        cleaned: dict[str, object] = {}
        for raw_key, item in list(value.items())[:max_items]:
            key = str(raw_key)
            cleaned[key] = preview_value(
                item,
                max_string=max_string,
                max_depth=max_depth,
                max_items=max_items,
                _depth=_depth + 1,
            )
        return cleaned
    if isinstance(value, (list, tuple)):
        return [
            preview_value(
                item,
                max_string=max_string,
                max_depth=max_depth,
                max_items=max_items,
                _depth=_depth + 1,
            )
            for item in list(value)[:max_items]
        ]
    if isinstance(value, bytes):
        return f"[binary value omitted; {len(value)} bytes]"
    if isinstance(value, str):
        scrubbed = scrub_inline_base64_text(value)
        if len(scrubbed) > max_string:
            return scrubbed[:max_string] + f"… [truncated {len(scrubbed) - max_string} chars]"
        return scrubbed
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return preview_value(
        str(value),
        max_string=max_string,
        max_depth=max_depth,
        _depth=_depth + 1,
    )


def summarize_tool_arguments(value: object) -> object:
    """保留工具控制参数，省略正文、代码、请求体和二进制负载。"""
    if not isinstance(value, Mapping):
        return preview_value(value, max_string=1200, max_depth=5)
    result: dict[str, object] = {}
    for raw_key, item in list(value.items())[:80]:
        key = str(raw_key)
        normalized = key.strip().lower().replace("-", "_")
        if normalized in _OMITTED_TOOL_FIELDS:
            size = len(item) if isinstance(item, (str, bytes, list, dict)) else 0
            result[key] = f"[omitted from live trace; size={size}]"
            continue
        result[key] = preview_value(
            item, max_string=1200, max_depth=5
        )
    return result


def summarize_tool_result(value: object) -> object:
    """把 JSON 结果结构化展示；仅限制摘要长度。"""
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            decoded = value
        return preview_value(decoded, max_string=3000, max_depth=6)
    return preview_value(value, max_string=3000, max_depth=6)


def preview_trace_event(event: Mapping[str, object]) -> dict[str, object]:
    """给兼容 JSONL 使用同一公开边界，同时保留其既有顶层结构。"""
    cleaned = preview_value(copy.deepcopy(dict(event)))
    if not isinstance(cleaned, dict):
        raise LiveTraceError("trace event must remain an object after sanitization")
    return cleaned


def iter_jsonl_events(path: str | Path) -> Iterable[dict[str, object]]:
    """逐行读取完整 JSON 对象，忽略中断写入的尾行。"""
    target = Path(path)
    if not target.is_file():
        return
    with target.open("rb") as handle:
        for raw in handle:
            if not raw.endswith(b"\n"):
                continue
            try:
                value = json.loads(raw)
            except (UnicodeDecodeError, ValueError, TypeError):
                continue
            if isinstance(value, dict):
                yield value


def iter_replay_events(path: str | Path, kind: str) -> Iterable[dict[str, object]]:
    """从唯一追加日志派生离线接口；这些投影不再作为运行或恢复输入。"""
    provider_types = {f"provider_{state}": f"provider_attempt_{state}"
                      for state in ("started", "succeeded", "failed")}
    sequence = 0
    for event in _iter_live_rows(path):
        event_type, payload = event["event_type"], event["payload"]
        identity = {key: event[key] for key in ("timestamp", "agent_id", "role", "turn", "segment_id")}
        if kind == "tool":
            if event_type not in {"tool_use", "tool_result"}:
                continue
            value = {**identity, "schema": "mural.tool-event.v1", "event_type": event_type,
                     "tool_use_id": payload["tool_use_id"],
                     "tool_name": payload["name"]}
            value["owner_id"] = value.pop("agent_id")
            if event_type == "tool_use":
                value["arguments"] = payload.get("arguments", {})
            else:
                result = payload.get("result")
                value.update(ok=payload.get("ok") is True,
                    content=result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    error_code=payload.get("error_code", ""), duration_ms=payload.get("duration_ms", 0))
        elif kind == "provider":
            if event_type not in provider_types:
                continue
            value = {**payload, **identity, "event_type": provider_types[event_type]}
        elif kind == "agent":
            if (event_type == "agent_context" or event_type.endswith("_delta") or
                not (event_type.startswith("agent_") or event_type.startswith("resource_wait_") or
                     event_type in {"assistant_message", "assistant_analysis_summary"})):
                continue
            value = {**payload, **identity, "event_type": event_type}
        else:
            raise ValueError(f"unsupported replay projection: {kind}")
        sequence += 1
        yield {**preview_trace_event(value), "sequence": sequence}


def _iter_live_rows(path: str | Path) -> Iterable[dict[str, object]]:
    for value in iter_jsonl_events(path):
        if value.get("schema") == LIVE_EVENT_SCHEMA:
            yield value


def _latest_agent_turn(path: str | Path, source: Mapping[str, object]) -> int:
    agent_id = str(source.get("agent_id") or "")
    segment_id = str(source.get("segment_id") or "")
    turn = _safe_int(source.get("turn"))
    for event in _iter_live_rows(path):
        if (
            str(event.get("agent_id") or "") == agent_id
            and str(event.get("segment_id") or "") == segment_id
        ):
            turn = max(turn, _safe_int(event.get("turn")))
    return turn


def _runtime_source_id(event: Mapping[str, object], mapped_type: str) -> str:
    attempt_id = str(event.get("attempt_id") or "")
    if mapped_type == "assistant_reasoning" and attempt_id:
        return f"reasoning:{attempt_id}:{event.get('mode')}:{event.get('block_index')}"
    if mapped_type == "agent_context":
        fields = {key: value for key, value in event.items() if key != "timestamp"}
        return "context:" + hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if mapped_type.endswith("_delta"):
        return f"stream:{attempt_id}:{mapped_type}:{_safe_int(event.get('revision'))}"
    if attempt_id and mapped_type.startswith("provider_"):
        return f"provider:{attempt_id}:{mapped_type}"
    if attempt_id and mapped_type.startswith("resource_wait_"):
        return f"resource:{attempt_id}:{mapped_type}"
    agent_id = str(event.get("agent_id") or "")
    segment_id = str(event.get("segment_id") or "")
    turn = _safe_int(event.get("turn"))
    if mapped_type in {"assistant_message", "assistant_analysis_summary"}:
        field = "message" if mapped_type == "assistant_message" else "summary"
        digest = hashlib.sha256(
            str(event.get(field) or "").encode("utf-8")
        ).hexdigest()[:16]
        block_index = _safe_int(event.get("block_index"))
        return (
            f"assistant:{agent_id}:{segment_id}:{turn}:"
            f"{mapped_type}:{block_index}:{digest}"
        )
    if mapped_type in {"agent_started", "agent_completed", "agent_failed"}:
        return f"agent:{agent_id}:{segment_id}:{turn}:{mapped_type}"
    return _derived_event_id(mapped_type, agent_id, str(event.get("role") or ""), turn, segment_id,
                             {key: value for key, value in event.items() if key != "timestamp"})


def _derived_event_id(
    event_type: str,
    agent_id: str,
    role: str,
    turn: int,
    segment_id: str,
    payload: Mapping[str, object],
) -> str:
    canonical = json.dumps(
        [event_type, agent_id, role, turn, segment_id, payload],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "event-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def _public_timestamp(value: str) -> str:
    raw = str(value or "").strip()
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.astimezone(timezone.utc).isoformat().replace(
                    "+00:00", "Z"
                )
        except ValueError:
            pass
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    written = 0
    while written < len(view):
        count = os.write(descriptor, view[written:])
        if count <= 0:
            raise LiveTraceError("live trace append made no progress")
        written += count
