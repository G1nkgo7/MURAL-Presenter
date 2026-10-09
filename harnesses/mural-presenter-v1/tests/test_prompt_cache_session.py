from dataclasses import replace
import json

import httpx
import pytest

from runtime.agent import AgentRuntime
from runtime.config import ProviderSettings
from runtime.provider_health import ProviderHealthStore
from runtime.recovery import SegmentCheckpoint
from runtime.session import (
    ModelRequest, ModelSession, ProviderChannel, TransportResponse, UsageLedger,
    _normalize_openai_responses_response, openai_responses_body,
)
from runtime.tools import ToolContext, ToolRuntime


MODEL = "gpt-6-astra/azure/sfa"
CHANNELS = (ProviderChannel("fixture-a", "a", model=MODEL),
            ProviderChannel("fixture-b", "b", model="gpt-6-astra/azure_L/qwb"))


def _response():
    return _normalize_openai_responses_response({"status": "completed", "output": [
        {"type": "message", "content": [{"type": "output_text", "text": "done"}]}],
        "usage": {"input_tokens": 100, "output_tokens": 10,
                  "input_tokens_details": {"cached_tokens": 60, "cache_write_tokens": 30}}})


class _Transport:
    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.calls = []

    def complete(self, channel, request, *, total_timeout_s):
        self.calls.append((channel, request))
        outcome = self.outcomes.pop(0) if self.outcomes else _response()
        if isinstance(outcome, Exception):
            raise outcome
        return TransportResponse(outcome, first_byte_s=0.01)


def _settings(**changes):
    return replace(ProviderSettings(api_format="openai_responses", model=MODEL,
        base_url="https://provider.example/v1", channel_cooldown_s=0, max_attempts=2), **changes)


def _request():
    return ModelRequest(model=MODEL, system="test", max_tokens=64, thinking=True, effort="high",
                        messages=({"role": "user", "content": "hello"},))


def _session(tmp_path, transport, *, run="run-1", agent="slide-1", **kwargs):
    return ModelSession(settings=_settings(), channels=CHANNELS, transport=transport,
        provider_health=ProviderHealthStore(tmp_path / "health"),
        cache_identity=json.dumps([run, agent]), **kwargs)


def test_cache_key_is_stable_across_turns_rollover_and_resume_but_scoped_by_run_agent_kind(tmp_path):
    transport = _Transport()
    request = _request()
    session = _session(tmp_path, transport)
    assert session.call(request).ok
    assert session.call(replace(request, messages=(*request.messages, {"role": "assistant", "content": "done"}))).ok
    session.checkpoint = SegmentCheckpoint.initial(segment_id="new-compact-segment")
    assert session.call(request).ok
    resumed = _session(tmp_path, transport, checkpoint=session.checkpoint)
    assert resumed.call(request).ok
    keys = [sent.prompt_cache_key for _, sent in transport.calls]
    assert keys[0] and len(keys[0]) <= 64 and len(set(keys)) == 1
    for session, scoped_request in (
        (_session(tmp_path, transport, agent="slide-2"), request),
        (_session(tmp_path, transport, run="run-2"), request),
        (resumed, replace(request, request_kind="context_compaction")),
    ):
        assert session.call(scoped_request).ok
        assert transport.calls[-1][1].prompt_cache_key != keys[0]
    assert request.prompt_cache_key == ""
    body = openai_responses_body(transport.calls[0][1])
    assert body["prompt_cache_key"] == keys[0]
    assert keys[0] not in json.dumps(body["input"]) + body["instructions"]
    assert "prompt_cache_options" not in body and "cache_control" not in json.dumps(body)


@pytest.mark.parametrize("status", [403, 429, 502])
def test_cache_affinity_reuses_successful_pair_but_failure_can_switch_route_and_key(tmp_path, status):
    response = httpx.Response(status, request=httpx.Request("POST", "https://provider.example/v1/responses"))
    error = httpx.HTTPStatusError("fixture failure", request=response.request, response=response)
    transport = _Transport([_response(), _response(), error, _response(), _response()])
    events = []
    session = _session(tmp_path, transport, event_sink=events.append)
    request = _request()
    assert session.call(request, preferred_channel_id="a").ok
    assert session.call(request).ok
    assert session.call(request).ok
    assert session.call(request).ok
    assert [(channel.channel_id, sent.model) for channel, sent in transport.calls] == [
        ("a", CHANNELS[0].model), ("a", CHANNELS[0].model), ("a", CHANNELS[0].model),
        ("b", CHANNELS[1].model), ("b", CHANNELS[1].model)]
    assert len({sent.prompt_cache_key for _, sent in transport.calls}) == 1
    assert session.provider_affinity_id == ""
    assert all(not event["channel_affinity_enforced"] for event in events
               if event["event_type"] == "provider_attempt_started")


def test_cache_affinity_does_not_wait_on_cooling_channel_if_another_is_healthy(tmp_path):
    transport = _Transport()
    session = _session(tmp_path, transport)
    assert session.call(_request(), preferred_channel_id="a").ok
    session.pool.cool_down(CHANNELS[0], 60)
    assert session.call(_request()).ok
    assert transport.calls[-1][0].channel_id == "b"


def test_signed_history_hard_lock_takes_precedence_over_cache_affinity(tmp_path):
    transport = _Transport()
    checkpoint = replace(SegmentCheckpoint.initial(), provider_affinity_id="a")
    session = _session(tmp_path, transport, checkpoint=checkpoint)
    session._cache_affinity_id = "b"
    assert session.call(_request()).ok
    assert transport.calls[-1][0].channel_id == "a"
    session._refresh_health_snapshot()
    assert session._request_order(False) == ((CHANNELS[0],), True)


@pytest.mark.parametrize("api_format,model,prompt_cache", [
    ("openai_responses", MODEL, False), ("anthropic", "claude-opus-5", True),
    ("openai_chat_compatible", "Qwen3.8-27B", True),
])
def test_non_gpt6_or_disabled_cache_preserves_channel_rotation(tmp_path, api_format, model, prompt_cache):
    transport = _Transport()
    channels = tuple(replace(channel, model="") for channel in CHANNELS)
    session = ModelSession(settings=_settings(api_format=api_format, model=model, prompt_cache=prompt_cache),
        channels=channels, transport=transport, cache_identity="private-identity",
        provider_health=ProviderHealthStore(tmp_path / "health"))
    request = replace(_request(), model=model, effort="medium")
    assert session.call(request).ok and session.call(request).ok
    assert transport.calls[0][0].channel_id != transport.calls[1][0].channel_id
    assert all(not sent.prompt_cache_key for _, sent in transport.calls)


def test_agent_cache_identity_uses_persistent_deck_identity_without_polluting_context_or_checkpoint(tmp_path):
    transport = _Transport()
    events = []
    runtime = AgentRuntime(settings=_settings(), channels=CHANNELS, transport=transport,
        tools=ToolRuntime(), event_sink=events.append,
        environment={})
    for root in (tmp_path / "original", tmp_path / "relocated"):
        context = ToolContext(owner_id="slide-1", role="slide", metadata={
            "workspace_root": str(root), "provenance": {"deck_id": "stable-deck"}})
        result = runtime.run(agent_id=context.owner_id, role=context.role, system="test",
            tool_context=context, initial_messages=_request().messages,
            max_turns=2, max_tokens=64, thinking=True, effort="high")
        key = transport.calls[-1][1].prompt_cache_key
        assert key and key not in json.dumps(result.to_dict())
        assert "prompt_cache_key" not in json.dumps(result.checkpoint.to_dict())
    assert transport.calls[0][1].prompt_cache_key == transport.calls[1][1].prompt_cache_key
    assert key not in json.dumps(events)


def test_usage_is_counted_once_per_http_attempt_including_billable_refusals():
    ledger = UsageLedger()
    response = _response()
    for _ in range(2):
        ledger.record_attempt("a", attempt_id="one")
        ledger.record_success("a", response, attempt_id="one")
        ledger.record_attempt("b", attempt_id="two")
        ledger.record_rejected_response("b", response, attempt_id="two")
    ledger.record_failure("a", attempt_id="one")
    usage = ledger.summary()
    assert usage["attempts"] == 2 and usage["calls"] == usage["failed_attempts"] == 1
    assert usage["input_tokens"] == 200 and usage["output_tokens"] == 20
    assert usage["cache_read_input_tokens"] == 120 and usage["cache_creation_input_tokens"] == 60
