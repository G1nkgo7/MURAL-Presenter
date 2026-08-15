from __future__ import annotations

from unittest import mock
from types import SimpleNamespace

import pytest

from core import model_call
from core.nova_raw import validate_proxy_health


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def _health(**overrides):
    payload = {
        "ok": True,
        "object": "nova.vision_proxy.health",
        "internal_trace_enabled": True,
        "agent_model": "claude-opus-4-7-thinking",
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
        "vision_model": "claude-opus-4-7-thinking",
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


def test_gate_v1_accepts_explicit_agent_model():
    with mock.patch(
        "core.nova_raw.requests.get", return_value=_Response(_health())
    ):
        result = validate_proxy_health(
            "http://127.0.0.1:8001/v1",
            gate_v1=True,
            expected_agent_model="claude-opus-4-7-thinking",
        )
    assert result["builtin_trace_format"] == "natural"


def test_gate_v1_rejects_wrong_agent_model():
    with mock.patch(
        "core.nova_raw.requests.get", return_value=_Response(_health())
    ):
        with pytest.raises(RuntimeError):
            validate_proxy_health(
                "http://127.0.0.1:8001/v1",
                gate_v1=True,
                expected_agent_model="claude-opus-5",
            )


def test_gate_v1_accepts_gate_off_profile():
    with mock.patch(
        "core.nova_raw.requests.get",
        return_value=_Response(_health(
            anthropic_agent_thinking_gate="off",
            anthropic_agent_thinking_retries=0,
        )),
    ):
        result = validate_proxy_health(
            "http://127.0.0.1:8001/v1",
            gate_v1=True,
            expected_agent_model="claude-opus-4-7-thinking",
            expected_thinking_gate="off",
            expected_thinking_retries=0,
        )
    assert result["anthropic_agent_thinking_gate"] == "off"


def test_anthropic_sdk_retries_are_disabled():
    model_call._local.clients = {}
    with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}), mock.patch.object(
        model_call.anthropic, "Anthropic"
    ) as constructor:
        model_call._client("http://127.0.0.1:8001")
    assert constructor.call_args.kwargs["max_retries"] == 0


def test_main_calls_add_prompt_cache_breakpoints_without_mutating_trace():
    messages = [{"role": "user", "content": "stable history"}]
    response = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=3,
            cache_read_input_tokens=17,
            cache_creation_input_tokens=5,
            output_tokens=2,
        )
    )
    client = mock.Mock()
    client.messages.create.return_value = response
    with mock.patch.object(model_call, "_client", return_value=client), mock.patch.object(
        model_call.config, "PROMPT_CACHE", True
    ), mock.patch.object(model_call.config, "PROMPT_CACHE_MESSAGES", True):
        result = model_call.call_with_tools(
            model="claude-opus-4-7-thinking",
            system="stable system",
            messages=messages,
            tools=[{"name": "read_file", "input_schema": {"type": "object"}}],
            max_tokens=32,
            thinking=False,
            effort="low",
            max_retries=1,
            request_kind="hermes_main",
        )
    assert result is response
    sent = client.messages.create.call_args.kwargs
    assert sent["system"] == [{
        "type": "text",
        "text": "stable system",
        "cache_control": {"type": "ephemeral"},
    }]
    assert sent["messages"][-1]["content"][-1]["cache_control"] == {
        "type": "ephemeral"
    }
    assert messages == [{"role": "user", "content": "stable history"}]


def test_vision_call_does_not_cache_image_message():
    messages = [{
        "role": "user",
        "content": [{"type": "image", "source": {"type": "base64", "data": "AA=="}}],
    }]
    response = SimpleNamespace(usage=SimpleNamespace(input_tokens=1, output_tokens=1))
    client = mock.Mock()
    client.messages.create.return_value = response
    with mock.patch.object(model_call, "_client", return_value=client), mock.patch.object(
        model_call.config, "PROMPT_CACHE", True
    ), mock.patch.object(model_call.config, "PROMPT_CACHE_MESSAGES", True):
        model_call.call_with_tools(
            model="claude-opus-4-7-thinking",
            system="vision system",
            messages=messages,
            tools=None,
            max_tokens=32,
            thinking=False,
            effort="low",
            max_retries=1,
            request_kind="vision_analyze_aux",
        )
    sent = client.messages.create.call_args.kwargs
    assert "cache_control" not in sent["messages"][-1]["content"][-1]
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}
