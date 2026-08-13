#!/usr/bin/env python3
"""Single-request vision proxy for text-only Presenter main models.

Each ``vision_analyze`` call sends the image to a dedicated OpenAI-compatible
vision model and returns only its textual verdict to the main agent.  The main
model therefore never receives image content blocks.
"""
from __future__ import annotations

import base64
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
        "one_shot", "oneshot", "internal_one_shot",
    }


def _base_url() -> str:
    value = str(
        os.environ.get("VISION_ONESHOT_BASE_URL")
        or os.environ.get("TOKENHUB_BASE_URL")
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


def _timeout() -> float:
    return max(30.0, float(os.environ.get("VISION_ONESHOT_TIMEOUT", "600")))


def _response_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    message = choices[0].get("message") if choices and isinstance(choices[0], dict) else {}
    content = (message or {}).get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text") or block.get("content") or "").strip()
            for block in content
            if isinstance(block, dict) and block.get("type") in {"text", "output_text"}
        ).strip()
    return ""


def _append_ledger(agent, record: dict[str, Any]) -> None:
    target = Path(agent.trace.sub_dir) / "aux_calls" / "vision-one-shot.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock, target.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
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
    if not parent_tool_use_id:
        raise RuntimeError("one-shot Vision call is missing parent_tool_use_id")
    model = _model()
    if not model:
        raise RuntimeError("VISION_ONESHOT_MODEL is empty")
    language = str(getattr(agent, "prompt_language", "zh") or "zh").lower()
    prompt = str(question or "").strip() or (
        "Inspect the image and report concrete pixel-grounded findings."
        if language == "en"
        else "检查图片并给出有像素依据的具体结论。"
    )
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a single-turn visual inspection tool. Inspect only the "
                    "supplied image, answer the exact question, and return concise "
                    "pixel-grounded evidence. Do not call tools or plan future steps."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": (
                        "data:" + media_type + ";base64," +
                        base64.b64encode(image_bytes).decode("ascii")
                    )}},
                ],
            },
        ],
        "max_tokens": int(os.environ.get("VISION_ONESHOT_MAX_TOKENS", "4096")),
        "temperature": float(os.environ.get("VISION_ONESHOT_TEMPERATURE", "0.1")),
    }
    started = time.monotonic()
    record: dict[str, Any] = {
        "schema": "lhp.vision-one-shot.v1",
        "tool_use_id": parent_tool_use_id,
        "source_path": source_path.replace(os.sep, "/"),
        "image_sha256": hashlib.sha256(image_bytes).hexdigest(),
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
            timeout=_timeout(),
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"one-shot Vision HTTP {response.status_code}: {response.text[:300]}"
            )
        body = response.json()
        choices = body.get("choices") or []
        finish_reason = (
            str(choices[0].get("finish_reason") or "").strip().lower()
            if choices and isinstance(choices[0], dict)
            else ""
        )
        if finish_reason in {"length", "max_tokens"}:
            raise RuntimeError(
                "one-shot Vision output was truncated by max tokens; retry is allowed"
            )
        text = _response_text(body)
        if not text:
            raise RuntimeError("one-shot Vision returned empty text")
        shot = agent.trace.snapshot_image(parent_tool_use_id, image_bytes)
        record.update({
            "status": "completed",
            "wall_seconds": round(time.monotonic() - started, 3),
            "shot": shot.replace(os.sep, "/"),
            "response_text": text,
            "usage": body.get("usage") or {},
            "finish_reason": finish_reason or "stop",
        })
        _append_ledger(agent, record)
        return text
    except Exception as exc:
        record.update({
            "status": "failed",
            "wall_seconds": round(time.monotonic() - started, 3),
            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
        })
        _append_ledger(agent, record)
        raise
