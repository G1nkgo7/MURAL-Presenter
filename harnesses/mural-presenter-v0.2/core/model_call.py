"""Small synchronous Anthropic tool-calling client used by the Clean harness."""
from __future__ import annotations

import os
import json
import threading
import time
import uuid
from typing import Any

import anthropic
import httpx

from . import config

_local = threading.local()
_cost_lock = threading.Lock()
_usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}


def cost_summary() -> dict[str, Any]:
    with _cost_lock:
        return dict(_usage)


def _client(base_url: str | None = None) -> anthropic.Anthropic:
    resolved_base_url = (base_url or config.ANTHROPIC_BASE_URL).rstrip("/")
    clients = getattr(_local, "clients", None)
    if clients is None:
        clients = {}
        _local.clients = clients
    client = clients.get(resolved_base_url)
    if client is None:
        client = anthropic.Anthropic(
            api_key=os.environ["ANTHROPIC_API_KEY"],
            base_url=resolved_base_url,
            # Gate raw capture requires one recorder attempt per physical HTTP
            # request.  SDK-internal retries are opaque to the recorder and can
            # also multiply a 1200s timeout.  All retries therefore belong to
            # call_with_tools' explicit, append-only attempt loop.
            max_retries=0,
            http_client=httpx.Client(trust_env=False, timeout=config.MODEL_TIMEOUT_S),
        )
        clients[resolved_base_url] = client
    return client


def call_with_tools(
    *,
    model: str,
    system: str,
    messages: list[dict],
    tools: list[dict] | None,
    max_tokens: int,
    thinking: bool,
    effort: str,
    log=print,
    max_retries: int = 6,
    first_attempt_timeout_s: int | None = None,
    deadline_monotonic: float | None = None,
    nova_recorder=None,
    request_kind: str = "hermes_main",
    parent_tool_use_id: str = "",
    base_url: str | None = None,
) -> Any | None:
    kwargs: dict[str, Any] = {
        "model": model,
        "system": system,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if tools:
        kwargs["tools"] = tools
    if thinking:
        # Opus 5 returns only an opaque signature unless summarized display is
        # requested explicitly. Effort belongs to output_config, not thinking.
        kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}
        kwargs["output_config"] = {"effort": effort}
    elif "deepseek" in model.lower():
        # TokenHub's DeepSeek V4 routes think by default even when the field is
        # omitted.  An explicit disabled value is therefore required for a
        # genuinely no-thinking rollout; other Anthropic-compatible models keep
        # their existing omit-the-field behavior.
        kwargs["thinking"] = {"type": "disabled"}

    invocation_id = (
        nova_recorder.new_invocation_id(request_kind)
        if nova_recorder is not None
        else ""
    )
    for attempt in range(1, max_retries + 1):
        attempt_recorded = False
        request_staged = False
        attempt_id = f"attempt-{attempt:03d}-{uuid.uuid4().hex}"
        request_id = f"request-{uuid.uuid4().hex}"
        try:
            remaining_s = (
                deadline_monotonic - time.monotonic()
                if deadline_monotonic is not None
                else None
            )
            if remaining_s is not None and remaining_s <= 0:
                log("[api stopped] role runtime boundary reached")
                return None
            call_kwargs = dict(kwargs)
            request_timeout_s = (
                first_attempt_timeout_s
                if attempt == 1 and first_attempt_timeout_s
                else config.MODEL_TIMEOUT_S
            )
            if remaining_s is not None:
                request_timeout_s = max(1, min(request_timeout_s, int(remaining_s)))
            call_kwargs["timeout"] = request_timeout_s
            request_payload = {
                key: value for key, value in call_kwargs.items() if key != "timeout"
            }
            if nova_recorder is not None:
                context = nova_recorder.context(
                    request_kind=request_kind,
                    invocation_id=invocation_id,
                    attempt_id=attempt_id,
                    request_id=request_id,
                    parent_tool_use_id=parent_tool_use_id,
                )
                request_payload.update({
                    "stream": False,
                    "nova_include_trace": True,
                    "nova_trace_context": context,
                })
                call_kwargs["stream"] = False
                call_kwargs["extra_body"] = {
                    "nova_include_trace": True,
                    "nova_trace_context": context,
                }
                nova_recorder.begin_attempt(
                    request_kind=request_kind,
                    invocation_id=invocation_id,
                    attempt_id=attempt_id,
                    parent_tool_use_id=parent_tool_use_id,
                    request_payload=request_payload,
                )
                request_staged = True
                raw_response = _client(base_url).messages.with_raw_response.create(**call_kwargs)
                http_status = int(
                    getattr(getattr(raw_response, "http_response", None), "status_code", 200)
                    or 200
                )
                raw_content = raw_response.content
                response_bytes = (
                    raw_content.encode("utf-8")
                    if isinstance(raw_content, str)
                    else bytes(raw_content)
                )
                try:
                    response_payload = json.loads(response_bytes)
                except (TypeError, ValueError) as exc:
                    response_payload = {}
                    nova_recorder.stage_response(
                        request_kind=request_kind,
                        invocation_id=invocation_id,
                        attempt_id=attempt_id,
                        parent_tool_use_id=parent_tool_use_id,
                        response_bytes=response_bytes,
                        response_payload=None,
                    )
                    nova_recorder.record_attempt(
                        request_kind=request_kind,
                        invocation_id=invocation_id,
                        attempt_id=attempt_id,
                        request_id=request_id,
                        parent_tool_use_id=parent_tool_use_id,
                        request_payload=request_payload,
                        response_bytes=response_bytes,
                        response_payload=None,
                        selected=False,
                        status="invalid_json",
                        error=f"{type(exc).__name__}: {exc}",
                        staged=True,
                        http_status=http_status,
                    )
                    attempt_recorded = True
                    raise RuntimeError("Nova returned a non-JSON response") from exc
                # Contract requirement: exact raw bytes and trace are committed
                # before the SDK is allowed to parse the provider response.
                nova_recorder.stage_response(
                    request_kind=request_kind,
                    invocation_id=invocation_id,
                    attempt_id=attempt_id,
                    parent_tool_use_id=parent_tool_use_id,
                    response_bytes=response_bytes,
                    response_payload=response_payload,
                )
                trace = response_payload.get("nova_internal_trace")
                trace_ok = (
                    isinstance(trace, dict)
                    and trace.get("schema_version") == "nova_agent.internal_model_calls.v1"
                    and isinstance(trace.get("status"), dict)
                    and trace["status"].get("ok") is True
                )
                if not trace_ok:
                    nova_recorder.record_attempt(
                        request_kind=request_kind,
                        invocation_id=invocation_id,
                        attempt_id=attempt_id,
                        request_id=request_id,
                        parent_tool_use_id=parent_tool_use_id,
                        request_payload=request_payload,
                        response_bytes=response_bytes,
                        response_payload=response_payload,
                        selected=False,
                        status="missing_or_invalid_nova_trace",
                        error="top-level nova_internal_trace is missing or invalid",
                        staged=True,
                        http_status=http_status,
                    )
                    attempt_recorded = True
                    if attempt < max_retries:
                        wait = min(2 ** attempt, 45)
                        if deadline_monotonic is not None:
                            wait = min(
                                wait,
                                max(0, deadline_monotonic - time.monotonic()),
                            )
                            if wait <= 0:
                                log("[api stopped] role runtime boundary reached")
                                return None
                        log(
                            f"[nova trace retry {attempt}/{max_retries}] "
                            f"missing or invalid trace; {wait}s"
                        )
                        time.sleep(wait)
                        continue
                    log("[nova trace failed] missing or invalid top-level trace")
                    return None
                try:
                    response = raw_response.parse()
                except Exception as exc:  # noqa: BLE001
                    nova_recorder.record_attempt(
                        request_kind=request_kind,
                        invocation_id=invocation_id,
                        attempt_id=attempt_id,
                        request_id=request_id,
                        parent_tool_use_id=parent_tool_use_id,
                        request_payload=request_payload,
                        response_bytes=response_bytes,
                        response_payload=response_payload,
                        selected=False,
                        status="sdk_parse_error",
                        error=f"{type(exc).__name__}: {str(exc)[:500]}",
                        staged=True,
                        http_status=http_status,
                    )
                    attempt_recorded = True
                    raise
                nova_recorder.record_attempt(
                    request_kind=request_kind,
                    invocation_id=invocation_id,
                    attempt_id=attempt_id,
                    request_id=request_id,
                    parent_tool_use_id=parent_tool_use_id,
                    request_payload=request_payload,
                    response_bytes=response_bytes,
                    response_payload=response_payload,
                    selected=True,
                    status="ok",
                    staged=True,
                    http_status=http_status,
                )
                attempt_recorded = True
            else:
                response = _client(base_url).messages.create(**call_kwargs)
            usage = getattr(response, "usage", None)
            with _cost_lock:
                _usage["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
                _usage["output_tokens"] += getattr(usage, "output_tokens", 0) or 0
                _usage["calls"] += 1
            return response
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            if not attempt_recorded:
                _record_failed_nova_attempt(
                    nova_recorder, request_kind, invocation_id, attempt_id, request_id,
                    parent_tool_use_id, locals().get("request_payload", {}), exc,
                    request_staged=request_staged,
                )
            log(f"[api auth] {type(exc).__name__}: {str(exc)[:180]}")
            return None
        except anthropic.BadRequestError as exc:
            if not attempt_recorded:
                _record_failed_nova_attempt(
                    nova_recorder, request_kind, invocation_id, attempt_id, request_id,
                    parent_tool_use_id, locals().get("request_payload", {}), exc,
                    request_staged=request_staged,
                )
            log(f"[api 400] {str(exc)[:260]}")
            return None
        except Exception as exc:  # network, rate limit, or server failure
            # Invalid JSON was already committed above.
            if not attempt_recorded:
                _record_failed_nova_attempt(
                    nova_recorder, request_kind, invocation_id, attempt_id, request_id,
                    parent_tool_use_id, locals().get("request_payload", {}), exc,
                    request_staged=request_staged,
                )
            retryable = isinstance(
                exc,
                (anthropic.RateLimitError, anthropic.APIConnectionError,
                 anthropic.APITimeoutError, anthropic.InternalServerError,
                 httpx.TransportError),
            )
            if retryable and attempt < max_retries:
                wait = min(2 ** attempt, 45)
                if deadline_monotonic is not None:
                    wait = min(wait, max(0, deadline_monotonic - time.monotonic()))
                    if wait <= 0:
                        log("[api stopped] role runtime boundary reached")
                        return None
                log(f"[api retry {attempt}/{max_retries}] {type(exc).__name__}; {wait}s")
                time.sleep(wait)
                continue
            log(f"[api failed] {type(exc).__name__}: {str(exc)[:180]}")
            return None
    return None


def _record_failed_nova_attempt(
    recorder,
    request_kind: str,
    invocation_id: str,
    attempt_id: str,
    request_id: str,
    parent_tool_use_id: str,
    request_payload: dict[str, Any],
    exc: Exception,
    *,
    request_staged: bool,
) -> None:
    if recorder is None:
        return
    # Failure happened before the request body was durably staged, therefore no
    # physical HTTP attempt is claimed or fabricated.
    if not request_staged:
        return
    response = getattr(exc, "response", None)
    raw_content = getattr(response, "content", b"") or b""
    response_bytes = (
        raw_content.encode("utf-8")
        if isinstance(raw_content, str)
        else bytes(raw_content)
    )
    response_payload = None
    http_status = getattr(response, "status_code", None)
    if response_bytes:
        try:
            parsed = json.loads(response_bytes)
            response_payload = parsed if isinstance(parsed, dict) else None
        except (TypeError, ValueError):
            pass
    recorder.stage_response(
        request_kind=request_kind,
        invocation_id=invocation_id,
        attempt_id=attempt_id,
        parent_tool_use_id=parent_tool_use_id,
        response_bytes=response_bytes,
        response_payload=response_payload,
        error=f"{type(exc).__name__}: {str(exc)[:500]}",
    )
    recorder.record_attempt(
        request_kind=request_kind,
        invocation_id=invocation_id,
        attempt_id=attempt_id,
        request_id=request_id,
        parent_tool_use_id=parent_tool_use_id,
        request_payload=request_payload,
        response_bytes=response_bytes,
        response_payload=response_payload,
        selected=False,
        status="http_error" if response_bytes else "transport_error",
        error=f"{type(exc).__name__}: {str(exc)[:500]}",
        staged=request_staged,
        http_status=int(http_status) if http_status is not None else None,
    )


def call_vision_auxiliary(
    *,
    agent,
    image_bytes: bytes,
    media_type: str,
    question: str,
    source_path: str,
    parent_tool_use_id: str,
) -> str:
    """Send one text-producing visual auxiliary request through Nova Proxy."""
    recorder = getattr(agent, "nova_raw", None)
    if recorder is not None:
        recorder.add_asset(image_bytes, media_type, source_path)
    import base64

    prompt = str(question or "").strip() or (
        "Inspect the image carefully. Describe layout, legibility, clipping, "
        "overlap, visual hierarchy, and any concrete defects that should be fixed."
    )
    response = call_with_tools(
        model=agent.model,
        system=(
            "You are the visual inspection auxiliary agent. Use the built-in "
            "vision_reader supplied by Nova, inspect the provided image, and "
            "return a concise evidence-based answer to the exact question."
        ),
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64.b64encode(image_bytes).decode("ascii"),
                    },
                },
                {"type": "text", "text": prompt},
            ],
        }],
        tools=None,
        max_tokens=config.VISION_NOVA_MAX_TOKENS,
        thinking=True,
        effort=agent.effort,
        log=agent.log,
        max_retries=6,
        first_attempt_timeout_s=config.VISION_NOVA_TIMEOUT_S,
        deadline_monotonic=agent.deadline_monotonic,
        nova_recorder=recorder,
        request_kind="vision_analyze_aux",
        parent_tool_use_id=parent_tool_use_id,
        base_url=config.NOVA_VISION_PROXY_BASE_URL,
    )
    if response is None:
        raise RuntimeError("Nova auxiliary request failed")
    text = "\n".join(
        str(getattr(block, "text", "")).strip()
        for block in getattr(response, "content", [])
        if getattr(block, "type", "") == "text" and str(getattr(block, "text", "")).strip()
    ).strip()
    if not text:
        raise RuntimeError("Nova auxiliary request returned an empty final answer")
    return text
