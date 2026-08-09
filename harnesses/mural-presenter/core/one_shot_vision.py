"""Single-request Vision tool transport for the internal distillation route.

Unlike the Nova adapter, this module is deliberately not an Agent loop.  One
``vision_analyze`` tool invocation produces exactly one OpenAI-compatible
``/chat/completions`` request containing the question and the image, then
returns the model's text as the tool result.  The main Presenter Agent never
receives the image bytes, so both text-only and multimodal main models can use
the same workflow.

This transport is internal/audit-friendly rather than Nova exact-raw.  It
stores a compact request/response ledger beside the owning Agent trace and
never records credentials or base64 payloads.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import requests


_write_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get("VISION_BACKEND", "").strip().lower() in {
        "one_shot",
        "oneshot",
        "internal_one_shot",
    }


def _base_url() -> str:
    value = str(
        os.environ.get("VISION_ONESHOT_BASE_URL")
        or os.environ.get("TOKENHUB_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or "https://tokenhub.sensetime.com/v1"
    ).rstrip("/")
    return value if value.endswith("/v1") else value + "/v1"


def _api_key() -> str:
    value = str(
        os.environ.get("VISION_ONESHOT_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
        or ""
    ).strip()
    if not value:
        raise RuntimeError(
            "VISION_ONESHOT_API_KEY/GEMINI_API_KEY is required for one-shot Vision"
        )
    return value


def _model() -> str:
    return str(
        os.environ.get("VISION_ONESHOT_MODEL")
        or os.environ.get("TOKENHUB_VISION_MODEL")
        or "gemini-3.5-flash"
    ).strip()


def _optional_timeout() -> float | None:
    raw = str(os.environ.get("VISION_ONESHOT_TIMEOUT", "0")).strip().lower()
    if raw in {"", "0", "none", "off", "disabled", "false"}:
        return None
    value = float(raw)
    return None if value <= 0 else value


def _response_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in {"text", "output_text"}:
                value = str(block.get("text") or block.get("content") or "").strip()
                if value:
                    parts.append(value)
        return "\n".join(parts).strip()
    return ""


def _append_ledger(agent, record: dict[str, Any]) -> None:
    target = Path(agent.trace.sub_dir) / "aux_calls" / "vision-one-shot.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _write_lock:
        with target.open("a", encoding="utf-8") as stream:
            stream.write(line)
            stream.flush()


def call(
    agent,
    *,
    image_bytes: bytes,
    media_type: str,
    source_path: str,
    question: str,
    parent_tool_use_id: str,
) -> str:
    """Run one and only one Vision model request and return its text."""
    if not parent_tool_use_id:
        raise RuntimeError("one-shot Vision call is missing parent_tool_use_id")
    model = _model()
    if not model:
        raise RuntimeError("VISION_ONESHOT_MODEL is empty")
    language = str(getattr(agent, "prompt_language", "zh") or "zh").lower()
    default_question = (
        "Inspect the image and report concrete pixel-grounded findings."
        if language == "en"
        else "检查图片并给出有像素依据的具体结论。"
    )
    prompt = str(question or "").strip() or default_question
    system = (
        "You are a single-turn visual inspection tool, not an autonomous agent. "
        "Inspect only the supplied image, answer the user's exact question, and "
        "return concise pixel-grounded evidence. Do not call tools, plan future "
        "steps, or invent content that is not visible."
    )
    import base64

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:"
                            + media_type
                            + ";base64,"
                            + base64.b64encode(image_bytes).decode("ascii")
                        },
                    },
                ],
            },
        ],
        "max_tokens": int(os.environ.get("VISION_ONESHOT_MAX_TOKENS", "4096")),
        "temperature": float(os.environ.get("VISION_ONESHOT_TEMPERATURE", "0.1")),
    }
    started = time.monotonic()
    image_sha = hashlib.sha256(image_bytes).hexdigest()
    record: dict[str, Any] = {
        "schema": "mural.vision-one-shot.v1",
        "tool_use_id": parent_tool_use_id,
        "source_path": source_path.replace(os.sep, "/"),
        "image_sha256": image_sha,
        "image_bytes": len(image_bytes),
        "media_type": media_type,
        "model": model,
        "request_count": 1,
        "question": prompt,
    }
    try:
        response = requests.post(
            _base_url() + "/chat/completions",
            headers={
                "Authorization": "Bearer " + _api_key(),
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=_optional_timeout(),
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"one-shot Vision HTTP {response.status_code}: {response.text[:300]}"
            )
        body = response.json()
        text = _response_text(body)
        if not text:
            raise RuntimeError("one-shot Vision returned empty text")
        shot = agent.trace.snapshot_image(parent_tool_use_id, image_bytes)
        record.update(
            {
                "status": "completed",
                "wall_seconds": round(time.monotonic() - started, 3),
                "shot": shot.replace(os.sep, "/"),
                "response_text": text,
                "usage": body.get("usage") or {},
            }
        )
        _append_ledger(agent, record)
        return text
    except Exception as exc:
        record.update(
            {
                "status": "failed",
                "wall_seconds": round(time.monotonic() - started, 3),
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            }
        )
        _append_ledger(agent, record)
        raise
