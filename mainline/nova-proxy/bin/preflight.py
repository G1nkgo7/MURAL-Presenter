#!/usr/bin/env python3
from __future__ import annotations

import json
import inspect
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MURAL_ROOT = HERE.parents[2]
MURAL_VERSION = os.environ.get("MURAL_VERSION", "0.4")
if MURAL_VERSION not in {"0.2", "0.4"}:
    raise SystemExit(f"unsupported MURAL_VERSION={MURAL_VERSION}; expected 0.2 or 0.4")
HARNESS = MURAL_ROOT / "harnesses" / f"mural-presenter-v{MURAL_VERSION}"
sys.path.insert(0, str(HARNESS))

from core.nova_raw import validate_proxy_health  # noqa: E402


def main() -> None:
    base_url = os.environ.get(
        "NOVA_PROXY_BASE_URL",
        f"http://127.0.0.1:{os.environ.get('NOVA_PORT', '8001')}/v1",
    )
    kwargs = {
        "timeout_s": int(os.environ.get("NOVA_HEALTH_TIMEOUT", "10")),
        "expected_builtin_vision_reader": True,
        "gate_v1": True,
        "expected_agent_model": os.environ.get(
            "CLEAN_NOVA_GATE_AGENT_MODEL", "claude-opus-5"
        ),
    }
    if "expected_thinking_gate" in inspect.signature(validate_proxy_health).parameters:
        kwargs.update({
            "expected_thinking_gate": os.environ.get(
                "CLEAN_NOVA_GATE_THINKING_GATE", "visible_signed"
            ),
            "expected_thinking_retries": int(os.environ.get(
                "CLEAN_NOVA_GATE_THINKING_RETRIES", "1"
            )),
        })
    health = validate_proxy_health(base_url, **kwargs)
    public = {
        key: health.get(key)
        for key in (
            "ok",
            "agent_model",
            "agent_payload_format",
            "agent_prompt_protocol",
            "vision_model",
            "vision_backend",
            "builtin_trace_format",
            "thinking_policy",
            "anthropic_agent_thinking_gate",
            "config_sha256",
            "code_sha256",
            "agent_request_url",
            "vision_request_url",
        )
    }
    print(json.dumps(public, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
