from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[3]
HARNESS = ROOT / "harnesses" / "mural-presenter-v0.2"
sys.path.insert(0, str(HARNESS))

from core.nova_raw import validate_proxy_health  # noqa: E402
from core import model_call  # noqa: E402


def _health(**overrides):
    payload = {
        "ok": True,
        "object": "nova.vision_proxy.health",
        "internal_trace_enabled": True,
        "agent_model": "claude-opus-5",
        "agent_max_tokens": 65536,
        "agent_hard_max_tokens": 65536,
        "builtin_vision_reader_enabled": True,
        "agent_vision_input_enabled": False,
        "agent_send_images_field": None,
        "render_vision_placeholders": False,
        "agent_payload_format": "anthropic",
        "agent_prompt_protocol": "anthropic_native",
        "resolved_agent_prompt_protocol": "anthropic_native",
        "vision_backend": "agent",
        "vision_model": "claude-opus-5",
        "vision_max_tokens": 2048,
        "vision_hard_max_tokens": 4096,
        "vision_retries": 1,
        "builtin_trace_format": "natural",
        "thinking_policy": "force_on",
        "anthropic_agent_thinking_gate": "visible_signed",
        "anthropic_agent_thinking_retries": 1,
        "agent_thinking": {"type": "adaptive", "display": "summarized"},
        "agent_output_config": {"effort": "high"},
        "agent_system_prompt_present": True,
        "agent_system_prompt_contract_ok": True,
        "max_steps": 24,
        "empty_output_retries": 2,
        "anthropic_pdf_parsing_enabled": True,
        "anthropic_pdf_missing_tools": [],
        "anthropic_messages": ["/v1/messages"],
        "agent_request_url": "https://agent.example/v1/messages",
        "vision_request_url": "https://agent.example/v1/messages",
        "config_sha256": "a" * 64,
        "code_sha256": "b" * 64,
        "streaming": {
            "content_mode": "buffered",
            "early_headers": True,
            "heartbeat_seconds": 5.0,
            "disconnect_cancellation": "provider_boundary",
        },
    }
    payload.update(overrides)
    return payload


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_frozen_gate_health_is_accepted():
    with mock.patch(
        "core.nova_raw.requests.get", return_value=_Response(_health())
    ):
        result = validate_proxy_health(
            "http://127.0.0.1:8001/v1", gate_v1=True
        )
    assert result["agent_model"] == "claude-opus-5"


def test_explicit_experimental_agent_model_is_accepted():
    model = "claude-opus-4-7-thinking"
    with mock.patch(
        "core.nova_raw.requests.get",
        return_value=_Response(_health(agent_model=model, vision_model=model)),
    ):
        result = validate_proxy_health(
            "http://127.0.0.1:8001/v1",
            gate_v1=True,
            expected_agent_model=model,
        )
    assert result["agent_model"] == model


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("builtin_trace_format", "xml"),
        ("agent_model", "claude-opus-4-7-thinking"),
        ("agent_vision_input_enabled", True),
        ("anthropic_pdf_missing_tools", ["pdftotext"]),
    ],
)
def test_gate_health_fails_closed(field, value):
    with mock.patch(
        "core.nova_raw.requests.get",
        return_value=_Response(_health(**{field: value})),
    ):
        with pytest.raises(RuntimeError):
            validate_proxy_health(
                "http://127.0.0.1:8001/v1", gate_v1=True
            )


def test_anthropic_sdk_retries_are_disabled_for_physical_attempt_capture():
    model_call._local.clients = {}
    with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}), mock.patch.object(
        model_call.anthropic, "Anthropic"
    ) as constructor:
        model_call._client("http://127.0.0.1:8001")
    assert constructor.call_args.kwargs["max_retries"] == 0
