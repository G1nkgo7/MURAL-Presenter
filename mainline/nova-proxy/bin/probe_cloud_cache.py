#!/usr/bin/env python3
"""Probe TokenHub Cloud slots without printing or persisting API keys."""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.split(" #", 1)[0].strip().strip("'\"")
    return values


def nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def request_once(url: str, key: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=encoded,
        headers={
            "Content-Type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "interleaved-thinking-2025-05-14",
        },
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            headers = dict(response.headers.items())
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read()
        headers = dict(exc.headers.items()) if exc.headers else {}
        status = exc.code
    elapsed = round(time.monotonic() - started, 3)
    try:
        decoded = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        decoded = {"error": {"type": "non_json_response", "message": body[:300].decode("utf-8", "replace")}}
    usage = decoded.get("usage") if isinstance(decoded, dict) else {}
    usage = usage if isinstance(usage, dict) else {}
    content = decoded.get("content") if isinstance(decoded, dict) else []
    content_types = [str(block.get("type")) for block in content if isinstance(block, dict)] if isinstance(content, list) else []
    error = decoded.get("error") if isinstance(decoded, dict) else None
    if isinstance(error, dict):
        error_summary = {
            "type": str(error.get("type") or "unknown"),
            "message": str(error.get("message") or "")[:500],
        }
    else:
        error_summary = None
    diagnostic_headers = {
        name.lower(): value
        for name, value in headers.items()
        if any(token in name.lower() for token in ("request-id", "channel", "provider", "cache"))
    }
    return {
        "http_status": status,
        "elapsed_seconds": elapsed,
        "usage": {
            "input_tokens": nonnegative_int(usage.get("input_tokens")),
            "cache_creation_input_tokens": nonnegative_int(usage.get("cache_creation_input_tokens")),
            "cache_read_input_tokens": nonnegative_int(usage.get("cache_read_input_tokens")),
            "output_tokens": nonnegative_int(usage.get("output_tokens")),
        },
        "content_types": content_types,
        "stop_reason": decoded.get("stop_reason") if isinstance(decoded, dict) else None,
        "error": error_summary,
        "diagnostic_headers": diagnostic_headers,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prefix-source", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()

    env = load_env(args.env)
    base_url = env.get("TOKENHUB_BASE_URL", "").rstrip("/")
    model = env.get("TOKENHUB_CLAUDE_MODEL_OPUS47_THINKING") or "claude-opus-4-7-thinking"
    if not base_url:
        raise SystemExit("TOKENHUB_BASE_URL is missing")
    source = args.prefix_source.read_text(encoding="utf-8")
    stable = (source + "\n") * max(2, 28000 // max(1, len(source)))
    stable = stable[:28000]
    records: list[dict[str, Any]] = []

    for slot in (1, 2, 3):
        key = env.get(f"TOKENHUB_CLOUD_KEY_{slot}", "")
        if not key:
            raise SystemExit(f"TOKENHUB_CLOUD_KEY_{slot} is missing")
        marker = f"\nCloud cache probe marker: mural-v11-20260815-slot-{slot}."
        payload = {
            "model": model,
            "max_tokens": 64,
            "system": [{
                "type": "text",
                "text": stable + marker,
                "cache_control": {"type": "ephemeral"},
            }],
            "messages": [{"role": "user", "content": "Reply with exactly CACHE_OK."}],
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {"effort": "low"},
        }
        attempts = [request_once(f"{base_url}/v1/messages", key, payload, args.timeout) for _ in range(2)]
        hot = attempts[-1]["usage"]
        denominator = sum(hot.get(name, 0) for name in (
            "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"
        ))
        records.append({
            "slot": slot,
            "model": model,
            "attempts": attempts,
            "second_request_cache_hit_rate": round(hot["cache_read_input_tokens"] / denominator, 6) if denominator else 0.0,
            "cache_supported": hot["cache_read_input_tokens"] > 0,
        })

    report = {
        "schema": "mural.cloud_cache_probe.v1",
        "created_at_epoch": time.time(),
        "endpoint": f"{base_url}/v1/messages",
        "request_count": len(records) * 2,
        "api_keys_persisted": False,
        "records": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, args.output)
    print(args.output)
    return 0 if all(record["attempts"][-1]["http_status"] == 200 for record in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
