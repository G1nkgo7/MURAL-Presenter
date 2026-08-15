"""Append-only Nova Proxy V2 raw trajectory capture and contract checks.

This module records provider request/response bytes before SDK parsing.  It is
deliberately separate from QC and training conversion: its only job is to make
the raw evidence complete, immutable, and auditable.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests


SCHEMA = "nova_agent.internal_model_calls.v1"
_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")
NO_NEW_VISION_EVIDENCE_MARKERS = (
    "自上次渲染后未变化",
    "现有视觉证据没有新增",
)


def _safe_id(value: str, fallback: str) -> str:
    cleaned = _ID_RE.sub("-", str(value)).strip("-._")
    return (cleaned or fallback)[:160]


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _atomic_json(path: Path, value: Any) -> None:
    _atomic_bytes(path, _json_bytes(value))


def _append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
    try:
        os.write(descriptor, payload.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _contains_image(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("type") in {"image", "image_url"}:
            return True
        return any(_contains_image(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_image(item) for item in value)
    return isinstance(value, str) and value.startswith("data:image/")


def _contains_key(value: Any, forbidden: str) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).lower() == forbidden.lower() or _contains_key(item, forbidden)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_key(item, forbidden) for item in value)
    return False


def _response_has_text(payload: dict[str, Any]) -> bool:
    content = payload.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list):
        return False
    return any(
        isinstance(item, dict)
        and item.get("type") == "text"
        and str(item.get("text") or "").strip()
        for item in content
    )


def _trace_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    trace = payload.get("nova_internal_trace")
    if isinstance(trace, dict):
        return trace
    # Some compatible gateways place provider extensions under metadata.
    metadata = payload.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("nova_internal_trace"), dict):
        return metadata["nova_internal_trace"]
    return {}


def validate_proxy_health(
    base_url: str,
    timeout_s: int = 10,
    *,
    expected_builtin_vision_reader: bool = True,
    gate_v1: bool = False,
    expected_agent_model: str = "claude-opus-5",
) -> dict[str, Any]:
    """Fail closed unless Nova health matches the raw-V2 routing contract."""
    base = base_url.rstrip("/")
    health_root = base[:-3] if base.endswith("/v1") else base
    response = requests.get(f"{health_root}/health", timeout=timeout_s)
    response.raise_for_status()
    payload = response.json()
    failures: list[str] = []
    expected = {
        "ok": True,
        "object": "nova.vision_proxy.health",
        "builtin_vision_reader_enabled": expected_builtin_vision_reader,
        "builtin_trace_format": "natural" if gate_v1 else "xml",
        "agent_payload_format": "anthropic",
        "agent_vision_input_enabled": False,
        "agent_send_images_field": None,
        "render_vision_placeholders": False,
        "vision_backend": "agent" if gate_v1 else "openai",
        "thinking_policy": "force_on",
        "agent_thinking": {"type": "adaptive", "display": "summarized"},
        "agent_output_config": {"effort": "high"},
        "agent_system_prompt_present": True,
        "agent_system_prompt_contract_ok": True,
    }
    if gate_v1:
        expected.update({
            "internal_trace_enabled": True,
            "agent_model": expected_agent_model,
            "agent_max_tokens": 65536,
            "agent_hard_max_tokens": 65536,
            "agent_prompt_protocol": "anthropic_native",
            "resolved_agent_prompt_protocol": "anthropic_native",
            "vision_model": expected_agent_model,
            "vision_max_tokens": 2048,
            "vision_hard_max_tokens": 4096,
            "vision_retries": 1,
            "anthropic_agent_thinking_gate": "visible_signed",
            "anthropic_agent_thinking_retries": 1,
            "max_steps": 24,
            "empty_output_retries": 2,
            "anthropic_pdf_parsing_enabled": True,
            "streaming": {
                "content_mode": "buffered",
                "early_headers": True,
                "heartbeat_seconds": 5.0,
                "disconnect_cancellation": "provider_boundary",
            },
        })
    for key, value in expected.items():
        if payload.get(key) != value:
            failures.append(f"{key}={payload.get(key)!r}, expected {value!r}")
    anthropic_messages = payload.get("anthropic_messages")
    if not isinstance(anthropic_messages, list) or "/v1/messages" not in anthropic_messages:
        failures.append("anthropic_messages does not expose /v1/messages")
    if gate_v1:
        missing_pdf_tools = payload.get("anthropic_pdf_missing_tools")
        if not isinstance(missing_pdf_tools, list) or missing_pdf_tools:
            failures.append(
                "anthropic_pdf_missing_tools must be an empty array in Gate V1"
            )
        agent_request_url = str(payload.get("agent_request_url") or "")
        vision_request_url = str(payload.get("vision_request_url") or "")
        if not agent_request_url.endswith("/v1/messages"):
            failures.append("agent_request_url does not end with /v1/messages")
        if not vision_request_url.endswith("/v1/messages"):
            failures.append(
                "vision_request_url does not end with /v1/messages"
            )
        if agent_request_url != vision_request_url:
            failures.append("Agent and Vision upstream request URLs must match")
        for hash_key in ("config_sha256", "code_sha256"):
            value = str(payload.get(hash_key) or "")
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                failures.append(f"{hash_key} is not 64 lowercase hex characters")
    proxy = urlparse(health_root)
    for key in ("agent_request_url", "vision_request_url"):
        upstream = str(payload.get(key) or "").strip()
        parsed = urlparse(upstream)
        if not upstream or parsed.scheme not in {"http", "https"} or not parsed.netloc:
            failures.append(f"{key} is empty or invalid")
        elif parsed.hostname == proxy.hostname and parsed.port == proxy.port:
            failures.append(f"{key} points back to Nova Proxy")
    if failures:
        raise RuntimeError("Nova health contract failed: " + "; ".join(failures))
    return payload


class NovaRawRecorder:
    """One independent Hermes Agent conversation = one main trajectory."""

    def __init__(
        self,
        *,
        root: Path,
        run_id: str,
        sample_id: str,
        role: str,
        label: str,
        tools: list[dict[str, Any]],
        initial_user: str = "",
        parent_main_trajectory_id: str = "",
        root_main_trajectory_id: str = "",
        delegation_depth: int = 0,
    ) -> None:
        self.run_id = _safe_id(run_id, "run")
        self.sample_id = str(sample_id)
        self.role = str(role)
        self.label = str(label)
        self.initial_user = str(initial_user)
        prefix = _safe_id(f"{sample_id}-{label}", "trajectory")
        self.main_trajectory_id = f"{prefix}-{uuid.uuid4().hex}"
        self.parent_main_trajectory_id = str(parent_main_trajectory_id or "")
        self.root_main_trajectory_id = str(
            root_main_trajectory_id or self.main_trajectory_id
        )
        self.delegation_depth = max(0, int(delegation_depth or 0))
        if self.delegation_depth == 0 and self.parent_main_trajectory_id:
            raise ValueError("root trajectory cannot have parent_main_trajectory_id")
        if self.delegation_depth > 0 and not self.parent_main_trajectory_id:
            raise ValueError("subagent trajectory requires parent_main_trajectory_id")
        self.run_root = Path(root)
        self.root = self.run_root / "tasks" / self.main_trajectory_id
        # A UUID makes collision vanishingly unlikely; nevertheless never reuse
        # an existing raw directory because this surface is append-only.
        self.root.mkdir(parents=True, exist_ok=False)
        self._lock = threading.Lock()
        self._selected: dict[str, str] = {}
        self._attempts: list[dict[str, Any]] = []
        self._aux_call_ids: set[str] = set()
        self._asset_rows: dict[str, dict[str, Any]] = {}
        self._finalized = False
        _atomic_json(self.root / "tools.json", tools)
        _atomic_json(self.root / "task_meta.json", {
            "run_id": self.run_id,
            "main_trajectory_id": self.main_trajectory_id,
            "sample_id": self.sample_id,
            "role": self.role,
            "label": self.label,
            "trajectory_kind": (
                "orchestrator" if self.delegation_depth == 0 else "subagent"
            ),
            "parent_main_trajectory_id": self.parent_main_trajectory_id,
            "root_main_trajectory_id": self.root_main_trajectory_id,
            "delegation_depth": self.delegation_depth,
            "created_at_epoch": time.time(),
        })

    def new_invocation_id(self, request_kind: str) -> str:
        return f"{_safe_id(request_kind, 'call')}-{uuid.uuid4().hex}"

    def context(
        self,
        *,
        request_kind: str,
        invocation_id: str,
        attempt_id: str,
        request_id: str,
        parent_tool_use_id: str = "",
    ) -> dict[str, str]:
        return {
            "run_id": self.run_id,
            "main_trajectory_id": self.main_trajectory_id,
            "request_kind": request_kind,
            "invocation_id": invocation_id,
            "attempt_id": attempt_id,
            "request_id": request_id,
            "parent_tool_use_id": parent_tool_use_id,
        }

    def add_asset(self, data: bytes, media_type: str, source_path: str) -> dict[str, Any]:
        digest = hashlib.sha256(data).hexdigest()
        extension = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(
            media_type, ".bin"
        )
        relative = f"assets/{digest}{extension}"
        target = self.root / relative
        with self._lock:
            existing = self._asset_rows.get(digest)
            if existing is not None:
                return existing
            if not target.exists():
                _atomic_bytes(target, data)
            row = {
                "path": relative,
                "sha256": digest,
                "bytes": len(data),
                "media_type": media_type,
                "source_path": source_path,
            }
            self._asset_rows[digest] = row
            _append_jsonl(self.root / "assets.jsonl", row)
        return row

    def record_attempt(
        self,
        *,
        request_kind: str,
        invocation_id: str,
        attempt_id: str,
        request_id: str,
        parent_tool_use_id: str,
        request_payload: dict[str, Any],
        response_bytes: bytes,
        response_payload: dict[str, Any] | None,
        selected: bool,
        status: str,
        error: str = "",
        staged: bool = False,
        http_status: int | None = None,
    ) -> None:
        attempt_root = self._attempt_root(
            request_kind, invocation_id, attempt_id, parent_tool_use_id
        )
        trace = _trace_from_payload(response_payload or {})
        row = {
            "run_id": self.run_id,
            "main_trajectory_id": self.main_trajectory_id,
            "request_kind": request_kind,
            "invocation_id": invocation_id,
            "attempt_id": attempt_id,
            "request_id": request_id,
            "parent_tool_use_id": parent_tool_use_id,
            "selected": bool(selected),
            "status": status,
            "http_status": http_status,
            "retry_ordinal": int(match.group(1)) if (
                match := re.match(r"attempt-(\d+)-", attempt_id)
            ) else None,
            "error": error,
            "recorded_at_epoch": time.time(),
            "request": str((attempt_root / "request.json").relative_to(self.root)),
            "response": str((attempt_root / "response.json").relative_to(self.root)),
            "trace": str((attempt_root / "trace.json").relative_to(self.root)),
        }
        with self._lock:
            if selected and invocation_id in self._selected:
                raise RuntimeError(f"invocation already selected: {invocation_id}")
            if not staged:
                self._stage_attempt_files(
                    attempt_root=attempt_root,
                    request_payload=request_payload,
                    response_bytes=response_bytes,
                    trace=trace,
                    error=error,
                )
            elif not all(
                (attempt_root / name).is_file()
                for name in ("request.json", "response.json", "trace.json")
            ):
                raise RuntimeError(f"staged attempt files are incomplete: {attempt_root}")
            _atomic_json(attempt_root / "attempt.json", row)
            _append_jsonl(self.root / "attempts.jsonl", row)
            self._attempts.append(row)
            if selected:
                self._selected[invocation_id] = attempt_id
                if request_kind == "vision_analyze_aux":
                    self._split_aux_calls(
                        trace=trace,
                        invocation_id=invocation_id,
                        attempt_id=attempt_id,
                        request_id=request_id,
                        parent_tool_use_id=parent_tool_use_id,
                    )

    def begin_attempt(
        self,
        *,
        request_kind: str,
        invocation_id: str,
        attempt_id: str,
        parent_tool_use_id: str,
        request_payload: dict[str, Any],
    ) -> None:
        """Persist the final request body before sending the physical request."""
        attempt_root = self._attempt_root(
            request_kind, invocation_id, attempt_id, parent_tool_use_id
        )
        with self._lock:
            if attempt_root.exists():
                raise RuntimeError(f"attempt path already exists: {attempt_root}")
            _atomic_json(attempt_root / "request.json", request_payload)

    def stage_response(
        self,
        *,
        request_kind: str,
        invocation_id: str,
        attempt_id: str,
        parent_tool_use_id: str,
        response_bytes: bytes,
        response_payload: dict[str, Any] | None,
        error: str = "",
    ) -> None:
        """Persist exact response bytes and trace before SDK parsing."""
        attempt_root = self._attempt_root(
            request_kind, invocation_id, attempt_id, parent_tool_use_id
        )
        with self._lock:
            if not (attempt_root / "request.json").is_file():
                raise RuntimeError(f"attempt request is missing: {attempt_root}")
            if (attempt_root / "response.json").exists() or (attempt_root / "trace.json").exists():
                raise RuntimeError(f"attempt response already staged: {attempt_root}")
            _atomic_bytes(
                attempt_root / "response.json",
                response_bytes or _json_bytes({"transport_error": error}),
            )
            _atomic_json(
                attempt_root / "trace.json",
                _trace_from_payload(response_payload or {}),
            )

    def _attempt_root(
        self,
        request_kind: str,
        invocation_id: str,
        attempt_id: str,
        parent_tool_use_id: str,
    ) -> Path:
        if request_kind == "hermes_main":
            return self.root / "attempts" / "main" / invocation_id / attempt_id
        return (
            self.root / "attempts" / "aux" /
            _safe_id(parent_tool_use_id, "missing-parent") / attempt_id
        )

    @staticmethod
    def _stage_attempt_files(
        *,
        attempt_root: Path,
        request_payload: dict[str, Any],
        response_bytes: bytes,
        trace: dict[str, Any],
        error: str,
    ) -> None:
        if attempt_root.exists():
            raise RuntimeError(f"attempt path already exists: {attempt_root}")
        _atomic_json(attempt_root / "request.json", request_payload)
        _atomic_bytes(
            attempt_root / "response.json",
            response_bytes or _json_bytes({"transport_error": error}),
        )
        _atomic_json(attempt_root / "trace.json", trace)

    def _split_aux_calls(
        self,
        *,
        trace: dict[str, Any],
        invocation_id: str,
        attempt_id: str,
        request_id: str,
        parent_tool_use_id: str,
    ) -> None:
        calls = trace.get("agent_model_calls")
        if not isinstance(calls, list):
            return
        evidence = trace.get("vision_model_calls")
        evidence = evidence if isinstance(evidence, list) else []
        call_rows: list[tuple[str, str, Any]] = []
        for index, call in enumerate(calls):
            call_bytes = _json_bytes(call)
            digest = hashlib.sha256(call_bytes).hexdigest()
            aux_call_id = f"{invocation_id}-{index:04d}-{digest[:12]}"
            if aux_call_id in self._aux_call_ids:
                raise RuntimeError(f"duplicate aux_call_id: {aux_call_id}")
            self._aux_call_ids.add(aux_call_id)
            call_rows.append((aux_call_id, digest, call))
        evidence_by_call: dict[int, list[Any]] = {index: [] for index in range(len(calls))}
        orphan_evidence: list[Any] = []
        for item in evidence:
            if not isinstance(item, dict):
                orphan_evidence.append(item)
                continue
            call_index = item.get("agent_step_index")
            if isinstance(call_index, int) and (call_index - 1) in evidence_by_call:
                evidence_by_call[call_index - 1].append(item)
            else:
                orphan_evidence.append(item)
        for index, (aux_call_id, digest, call) in enumerate(call_rows):
            call_dict = call if isinstance(call, dict) else {}
            model_input = call_dict.get("model_input")
            model_input = model_input if isinstance(model_input, dict) else {}
            model_reply = call_dict.get("model_reply")
            model_reply = model_reply if isinstance(model_reply, dict) else {}
            payload = {
                "schema": SCHEMA,
                "aux_call_id": aux_call_id,
                "main_trajectory_id": self.main_trajectory_id,
                "parent_tool_use_id": parent_tool_use_id,
                "invocation_id": invocation_id,
                "attempt_id": attempt_id,
                "request_id": request_id,
                "agent_model_call_index": index + 1,
                "agent_model_call_count": len(calls),
                "rendered_prompt": model_input.get("rendered_prompt", ""),
                "provider_request": call_dict.get("request", {}),
                "provider_response": call_dict.get("response", {}),
                "provider_reasoning": (
                    model_reply.get("reasoning")
                    or model_reply.get("thinking")
                    or {"status": "absent"}
                ),
                "raw_reply": model_reply.get("raw_text", ""),
                "tool_calls": model_reply.get(
                    "tool_calls", call_dict.get("tool_calls", [])
                ),
                "guardrail": call_dict.get(
                    "output_guardrail",
                    call_dict.get("guardrail", {"status": "absent"}),
                ),
                "original_call_sha256": digest,
                "agent_model_call": call,
                "vision_model_calls": evidence_by_call[index],
            }
            _atomic_json(self.root / "aux_calls" / f"{aux_call_id}.json", payload)
        if orphan_evidence:
            _atomic_json(self.root / "aux_calls" / "orphan_vision_evidence.json", {
                "count": len(orphan_evidence),
                "vision_model_calls": orphan_evidence,
            })

    def finalize(
        self,
        *,
        messages: list[dict[str, Any]],
        final_answer: str,
        status: str,
        exit_reason: str,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if self._finalized:
            raise RuntimeError(f"trajectory already finalized: {self.main_trajectory_id}")
        _atomic_json(self.root / "messages.json", messages)
        report = self.precheck()
        effective_status = status if report["ok"] else "quarantine"
        result = {
            "run_id": self.run_id,
            "main_trajectory_id": self.main_trajectory_id,
            "sample_id": self.sample_id,
            "role": self.role,
            "label": self.label,
            "status": effective_status,
            "exit_reason": exit_reason,
            "final_answer": final_answer,
            "artifacts": artifacts or [],
            "selected_main_attempts": [
                {
                    "invocation_id": row["invocation_id"],
                    "attempt_id": row["attempt_id"],
                    "request_id": row["request_id"],
                    "request": row["request"],
                    "response": row["response"],
                    "trace": row["trace"],
                }
                for row in self._attempts
                if row["selected"] and row["request_kind"] == "hermes_main"
            ],
            "aux_trajectory_count": len(self._aux_call_ids),
            "finished_at_epoch": time.time(),
        }
        _atomic_json(self.root / "task_result.json", result)
        _atomic_json(self.root / "precheck.json", report)
        if not report["ok"]:
            quarantine = self.run_root / "quarantine" / self.main_trajectory_id
            quarantine.parent.mkdir(parents=True, exist_ok=True)
            if quarantine.exists():
                raise RuntimeError(f"quarantine trajectory already exists: {quarantine}")
            os.replace(self.root, quarantine)
            self.root = quarantine
        self._finalized = True
        return report

    def abort(self, error: str) -> None:
        """Finalize an interrupted trajectory without inventing an HTTP attempt."""
        if self._finalized:
            return
        if not (self.root / "messages.json").exists():
            _atomic_json(
                self.root / "messages.json",
                [{"role": "user", "content": self.initial_user}],
            )
        report = self.precheck()
        report["ok"] = False
        report["errors"] = [f"runner exception: {error}", *report.get("errors", [])]
        _atomic_json(self.root / "precheck.json", report)
        _atomic_json(self.root / "task_result.json", {
            "run_id": self.run_id,
            "main_trajectory_id": self.main_trajectory_id,
            "sample_id": self.sample_id,
            "role": self.role,
            "label": self.label,
            "status": "quarantine",
            "exit_reason": "runner_exception",
            "final_answer": "",
            "error": error,
            "finished_at_epoch": time.time(),
        })
        quarantine = self.run_root / "quarantine" / self.main_trajectory_id
        quarantine.parent.mkdir(parents=True, exist_ok=True)
        if quarantine.exists():
            raise RuntimeError(f"quarantine trajectory already exists: {quarantine}")
        os.replace(self.root, quarantine)
        self.root = quarantine
        self._finalized = True

    def precheck(self) -> dict[str, Any]:
        errors: list[str] = []
        selected_by_invocation: dict[str, int] = {}
        seen_request_ids: set[str] = set()
        seen_attempt_keys: set[tuple[str, str]] = set()
        expected_aux_calls = 0
        expected_vision_evidence = 0
        for row in self._attempts:
            request_id = str(row.get("request_id") or "")
            attempt_key = (str(row.get("invocation_id") or ""), str(row.get("attempt_id") or ""))
            if not request_id or request_id in seen_request_ids:
                errors.append(f"duplicate or empty request_id: {request_id!r}")
            seen_request_ids.add(request_id)
            if not all(attempt_key) or attempt_key in seen_attempt_keys:
                errors.append(f"duplicate or empty invocation/attempt id: {attempt_key!r}")
            seen_attempt_keys.add(attempt_key)
            if row["selected"]:
                selected_by_invocation[row["invocation_id"]] = (
                    selected_by_invocation.get(row["invocation_id"], 0) + 1
                )
            request = json.loads((self.root / row["request"]).read_text(encoding="utf-8"))
            trace = json.loads((self.root / row["trace"]).read_text(encoding="utf-8"))
            response_payload: dict[str, Any] = {}
            try:
                candidate = json.loads((self.root / row["response"]).read_text(encoding="utf-8"))
                if isinstance(candidate, dict):
                    response_payload = candidate
            except (OSError, ValueError):
                pass
            if _contains_key(request, "authorization"):
                errors.append(f"{row['attempt_id']}: request contains Authorization")
            context = request.get("nova_trace_context")
            if not isinstance(context, dict):
                errors.append(f"{row['attempt_id']}: missing nova_trace_context")
            else:
                for key in (
                    "run_id", "main_trajectory_id", "request_kind", "invocation_id",
                    "attempt_id", "request_id", "parent_tool_use_id",
                ):
                    if context.get(key) != row.get(key):
                        errors.append(f"{row['attempt_id']}: context mismatch for {key}")
            if request.get("stream") is not False or request.get("nova_include_trace") is not True:
                errors.append(f"{row['attempt_id']}: Nova request flags are invalid")
            if row["request_kind"] == "hermes_main" and _contains_image(request.get("messages")):
                errors.append(f"{row['attempt_id']}: main request contains image input")
            if row["request_kind"] == "vision_analyze_aux" and request.get("tools"):
                errors.append(f"{row['attempt_id']}: auxiliary request contains Hermes tools")
            if row["selected"]:
                if trace.get("schema_version") != SCHEMA:
                    errors.append(f"{row['attempt_id']}: trace schema missing or invalid")
                if not isinstance(trace.get("status"), dict) or trace["status"].get("ok") is not True:
                    errors.append(f"{row['attempt_id']}: trace status is not ok")
                runtime = trace.get("runtime")
                runtime = runtime if isinstance(runtime, dict) else {}
                summary = trace.get("summary")
                summary = summary if isinstance(summary, dict) else {}
                tool_sequence = summary.get("tool_sequence")
                tool_sequence = tool_sequence if isinstance(tool_sequence, list) else []
                trace_tools = trace.get("tools")
                trace_tools = trace_tools if isinstance(trace_tools, dict) else {}
                if (
                    "agent_send_images_field" not in runtime
                    or runtime.get("agent_send_images_field") is not None
                ):
                    errors.append(
                        f"{row['attempt_id']}: runtime.agent_send_images_field is missing/non-null"
                    )
                raw_agent_calls = trace.get("agent_model_calls")
                raw_vision_calls = trace.get("vision_model_calls")
                if not isinstance(raw_agent_calls, list):
                    errors.append(f"{row['attempt_id']}: agent_model_calls is not an array")
                if not isinstance(raw_vision_calls, list):
                    errors.append(f"{row['attempt_id']}: vision_model_calls is not an array")
                if not isinstance(trace.get("images"), list):
                    errors.append(f"{row['attempt_id']}: images is not an array")
                agent_calls = raw_agent_calls
                vision_calls = raw_vision_calls
                agent_calls = agent_calls if isinstance(agent_calls, list) else []
                vision_calls = vision_calls if isinstance(vision_calls, list) else []
                if not agent_calls:
                    errors.append(f"{row['attempt_id']}: trace has no agent_model_calls")
                if [call.get("index") for call in agent_calls if isinstance(call, dict)] != list(
                    range(1, len(agent_calls) + 1)
                ):
                    errors.append(f"{row['attempt_id']}: agent call indexes are not contiguous")
                if [call.get("index") for call in vision_calls if isinstance(call, dict)] != list(
                    range(1, len(vision_calls) + 1)
                ):
                    errors.append(f"{row['attempt_id']}: vision call indexes are not contiguous")
                for call in agent_calls:
                    model_input = call.get("model_input", {}) if isinstance(call, dict) else {}
                    call_request = call.get("request") if isinstance(call, dict) else None
                    call_response = call.get("response") if isinstance(call, dict) else None
                    model_reply = call.get("model_reply") if isinstance(call, dict) else None
                    if not str(model_input.get("rendered_prompt") or "").strip():
                        errors.append(f"{row['attempt_id']}: agent call lacks rendered_prompt")
                    if not isinstance(call_request, dict):
                        errors.append(f"{row['attempt_id']}: agent call request is invalid")
                    if not isinstance(call_response, dict) or call_response.get("ok") is not True:
                        errors.append(f"{row['attempt_id']}: agent call response is not ok")
                    if not isinstance(model_reply, dict) or "raw_text" not in model_reply:
                        errors.append(f"{row['attempt_id']}: agent call lacks exact raw_text")
                if row["request_kind"] == "hermes_main":
                    if trace.get("images"):
                        errors.append(f"{row['attempt_id']}: main trace contains images")
                    if vision_calls:
                        errors.append(f"{row['attempt_id']}: main trace contains vision calls")
                    if "vision_reader" in tool_sequence:
                        errors.append(f"{row['attempt_id']}: main trace contains vision_reader")
                else:
                    expected_aux_calls += len(agent_calls)
                    expected_vision_evidence += len(vision_calls)
                    if not _contains_image(request.get("messages")):
                        errors.append(f"{row['attempt_id']}: auxiliary request has no image")
                    if not trace.get("images"):
                        errors.append(f"{row['attempt_id']}: auxiliary trace has no image evidence")
                    if not isinstance(trace_tools.get("user"), list):
                        errors.append(f"{row['attempt_id']}: auxiliary user tools is not an array")
                    elif trace_tools.get("user"):
                        errors.append(f"{row['attempt_id']}: auxiliary trace has user tools")
                    if not vision_calls:
                        errors.append(f"{row['attempt_id']}: auxiliary trace has no vision evidence")
                    if "vision_reader" not in tool_sequence:
                        errors.append(f"{row['attempt_id']}: auxiliary trace lacks vision_reader")
                    if not str(summary.get("final_answer") or "").strip():
                        errors.append(f"{row['attempt_id']}: auxiliary trace final answer is empty")
                    for call in agent_calls:
                        call_dict = call if isinstance(call, dict) else {}
                        call_request = call_dict.get("request")
                        call_request = call_request if isinstance(call_request, dict) else {}
                        payload = call_request.get("payload", {})
                        if isinstance(payload, dict) and (
                            "images" in payload or "multimodal_params" in payload
                        ):
                            errors.append(f"{row['attempt_id']}: Agent upstream payload contains images")
                        model_input = call_dict.get("model_input")
                        model_input = model_input if isinstance(model_input, dict) else {}
                        prompt = model_input.get("rendered_prompt", "")
                        if re.search(r"data:image/[^;]+;base64", str(prompt), flags=re.I):
                            errors.append(f"{row['attempt_id']}: rendered auxiliary prompt contains data URI")
                    for vision in vision_calls:
                        vision_response = (
                            vision.get("response") if isinstance(vision, dict) else None
                        )
                        if not isinstance(vision_response, dict) or vision_response.get("ok") is not True:
                            errors.append(f"{row['attempt_id']}: vision response is not ok")
                            continue
                        step = vision.get("agent_step_index")
                        matching = [
                            call for call in agent_calls
                            if isinstance(call, dict)
                            and call.get("index") == step
                            and any(
                                execution.get("name") == "vision_reader"
                                for execution in (
                                    call.get("builtin_tool_executions", [])
                                    if isinstance(call.get("builtin_tool_executions", []), list)
                                    else []
                                )
                                if isinstance(execution, dict)
                            )
                        ]
                        if len(matching) != 1:
                            errors.append(
                                f"{row['attempt_id']}: vision evidence does not map to one reader call"
                            )
                    if not _response_has_text(response_payload):
                        errors.append(f"{row['attempt_id']}: auxiliary final answer is empty")
                if response_payload.get("nova_internal_trace") != trace:
                    errors.append(f"{row['attempt_id']}: response trace differs from trace.json")
        for invocation_id in {row["invocation_id"] for row in self._attempts}:
            if selected_by_invocation.get(invocation_id, 0) != 1:
                errors.append(
                    f"{invocation_id}: expected exactly one selected attempt, "
                    f"found {selected_by_invocation.get(invocation_id, 0)}"
                )
        for asset in self._asset_rows.values():
            target = self.root / asset["path"]
            if not target.is_file():
                errors.append(f"asset missing: {asset['path']}")
            elif hashlib.sha256(target.read_bytes()).hexdigest() != asset["sha256"]:
                errors.append(f"asset sha256 mismatch: {asset['path']}")
        try:
            messages = json.loads((self.root / "messages.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            messages = []
            errors.append("messages.json is missing or invalid")
        tool_uses: dict[str, tuple[int, str]] = {}
        tool_use_inputs: dict[str, dict[str, Any]] = {}
        tool_results: dict[str, int] = {}
        tool_result_content: dict[str, Any] = {}
        for message_index, message in enumerate(messages if isinstance(messages, list) else []):
            content = message.get("content") if isinstance(message, dict) else None
            blocks = content if isinstance(content, list) else []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    tool_id = str(block.get("id") or "")
                    if not tool_id or tool_id in tool_uses:
                        errors.append(f"duplicate or empty tool_use id: {tool_id!r}")
                    else:
                        tool_uses[tool_id] = (message_index, str(block.get("name") or ""))
                        tool_use_inputs[tool_id] = (
                            block.get("input") if isinstance(block.get("input"), dict) else {}
                        )
                if block.get("type") == "tool_result":
                    tool_id = str(block.get("tool_use_id") or "")
                    if not tool_id or tool_id in tool_results:
                        errors.append(f"duplicate or empty tool_result id: {tool_id!r}")
                    else:
                        tool_results[tool_id] = message_index
                        tool_result_content[tool_id] = block.get("content")
        if set(tool_uses) != set(tool_results):
            errors.append("tool_use/tool_result ids are not bijective")
        if _contains_image(messages):
            errors.append("main trajectory messages contain image input")
        for tool_id, (use_index, _) in tool_uses.items():
            if tool_results.get(tool_id, use_index + 1) <= use_index:
                errors.append(f"tool_result is not after tool_use: {tool_id}")
        selected_aux_parents = {
            row["parent_tool_use_id"]
            for row in self._attempts
            if row["selected"] and row["request_kind"] == "vision_analyze_aux"
        }
        for parent in selected_aux_parents:
            if tool_uses.get(parent, (None, None))[1] != "vision_analyze":
                errors.append(f"selected auxiliary call has unresolved parent: {parent}")
                continue
            image_url = str(tool_use_inputs.get(parent, {}).get("image_url") or "")
            normalized = image_url.replace("\\", "/").lstrip("./")
            if not normalized or not any(
                str(asset.get("source_path") or "").replace("\\", "/").lstrip("./") == normalized
                for asset in self._asset_rows.values()
            ):
                errors.append(f"vision_analyze {parent} has no matching hashed input asset")
        selected_aux_count_by_parent: dict[str, int] = {}
        for row in self._attempts:
            if row["selected"] and row["request_kind"] == "vision_analyze_aux":
                parent = row["parent_tool_use_id"]
                selected_aux_count_by_parent[parent] = selected_aux_count_by_parent.get(parent, 0) + 1
        for tool_id, (_, name) in tool_uses.items():
            if name != "vision_analyze":
                continue
            result_text = json.dumps(tool_result_content.get(tool_id), ensure_ascii=False)
            if "错误" in result_text or "error" in result_text.lower():
                continue
            if all(marker in result_text for marker in NO_NEW_VISION_EVIDENCE_MARKERS):
                # `vision_analyze` intentionally short-circuits repeated reads of
                # an unchanged PNG. The existing evidence ledger is reused, so
                # there is no new auxiliary Nova request to associate.
                continue
            count = selected_aux_count_by_parent.get(tool_id, 0)
            if count != 1:
                errors.append(
                    f"vision_analyze {tool_id} expected one selected auxiliary attempt, found {count}"
                )
        orphan_path = self.root / "aux_calls" / "orphan_vision_evidence.json"
        if orphan_path.exists():
            errors.append("one or more vision_model_calls have no unique auxiliary owner")
        if len(self._aux_call_ids) != expected_aux_calls:
            errors.append(
                f"aux trajectory count mismatch: files={len(self._aux_call_ids)} "
                f"expected={expected_aux_calls}"
            )
        actual_vision_evidence = 0
        for path in (self.root / "aux_calls").glob("*.json") if (self.root / "aux_calls").is_dir() else []:
            if path.name == "orphan_vision_evidence.json":
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                actual_vision_evidence += len(payload.get("vision_model_calls", []))
            except (OSError, ValueError):
                errors.append(f"invalid aux call file: {path.name}")
        if actual_vision_evidence != expected_vision_evidence:
            errors.append(
                f"vision evidence count mismatch: files={actual_vision_evidence} "
                f"expected={expected_vision_evidence}"
            )
        last = messages[-1] if isinstance(messages, list) and messages else {}
        last_content = last.get("content") if isinstance(last, dict) else None
        last_has_text = (
            bool(str(last_content).strip())
            if isinstance(last_content, str)
            else any(
                isinstance(block, dict)
                and block.get("type") == "text"
                and str(block.get("text") or "").strip()
                for block in (last_content if isinstance(last_content, list) else [])
            )
        )
        if last.get("role") != "assistant" or not last_has_text:
            errors.append("messages.json does not end with a non-empty assistant answer")
        return {
            "schema": "nova_raw_v2.precheck.v1",
            "ok": not errors,
            "errors": errors,
            "counts": {
                "attempts": len(self._attempts),
                "selected_attempts": sum(1 for row in self._attempts if row["selected"]),
                "main_attempts": sum(1 for row in self._attempts if row["request_kind"] == "hermes_main"),
                "aux_attempts": sum(1 for row in self._attempts if row["request_kind"] == "vision_analyze_aux"),
                "aux_trajectories": len(self._aux_call_ids),
                "assets": len(self._asset_rows),
            },
        }
