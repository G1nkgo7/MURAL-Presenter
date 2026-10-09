from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from runtime.config import ProviderSettings
from runtime.provider_health import ProviderHealthStore as FilesystemProviderHealthStore
from runtime.recovery import ProviderRequestTimeout, SegmentCheckpoint
import runtime.session as session_module
from runtime.session import (
    ChannelPool,
    ModelRequest,
    ModelSession,
    OPENAI_TOOL_ERROR_MARKER,
    ProviderChannel,
    ProviderProtocolError,
    TransportResponse,
    openai_chat_body,
    openai_responses_body,
    provider_channels,
    replay_provider_events,
    to_openai_messages,
)


@pytest.fixture(autouse=True)
def isolated_provider_health(tmp_path, monkeypatch) -> None:
    """Legacy cases reuse fixture credentials; inject isolated health state."""
    monkeypatch.setattr(
        session_module,
        "ProviderHealthStore",
        lambda _directory: FilesystemProviderHealthStore(tmp_path / "provider-health"),
    )


class FakeTransport:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, ModelRequest]] = []

    def complete(self, channel, request, *, total_timeout_s):
        self.calls.append((channel.channel_id, copy.deepcopy(request)))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return TransportResponse(outcome, first_byte_s=0.25)


def _settings() -> ProviderSettings:
    return ProviderSettings(
        api_format="anthropic",
        model="claude-test",
        base_url="https://provider.example",
        total_timeout_s=600,
        channel_cooldown_s=120,
        max_attempts=3,
        streaming=True,
        prompt_cache=False,
        prompt_cache_messages=False,
    )


@pytest.mark.parametrize("model", [
    "claude-opus-5", "claude-opus-4-7-thinking", "claude-sonnet-5",
    "claude-sonnet-4-6", "claude-haiku-4-5", "deepseek-v4-pro",
])
@pytest.mark.parametrize("request_kind", ["hermes_main", "critic_visual"])
def test_anthropic_wire_explicitly_disables_supported_model_thinking(model, request_kind):
    request = ModelRequest(model=model, system="system", messages=(),
                           thinking=False, effort="low", request_kind=request_kind)
    wire = json.loads(json.dumps(request.anthropic_kwargs(_settings())))
    assert wire["thinking"] == {"type": "disabled"}
    assert wire.get("output_config") == ({"effort": "low"} if model.startswith("claude-opus-") else None)
    assert request.thinking is False


@pytest.mark.parametrize("model", [
    "claude-mythos-5", "claude-fable-5", "claude-test",
    "qwen3.5", "thirdparty-claude-opus-5",
])
def test_anthropic_wire_does_not_disable_unknown_or_always_thinking_models(model):
    request = ModelRequest(model=model, system="system", messages=(), thinking=False)
    wire = request.anthropic_kwargs(_settings())
    assert "thinking" not in wire
    assert "output_config" not in wire


@pytest.mark.parametrize("effort", ["max", "xhigh"])
def test_opus5_disabled_thinking_rejects_effort_requiring_thinking(effort):
    request = ModelRequest(model="claude-opus-5", system="system", messages=(),
                           thinking=False, effort=effort)
    with pytest.raises(ValueError, match="requires thinking enabled"):
        request.anthropic_kwargs(_settings())


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
@pytest.mark.parametrize("model", ["claude-opus-4-7-thinking", "claude-opus-5"])
def test_anthropic_enabled_adaptive_thinking_is_unchanged(effort, model):
    request = ModelRequest(model=model, system="system", messages=(),
                           thinking=True, effort=effort)
    wire = request.anthropic_kwargs(_settings())
    assert wire["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert wire["output_config"] == {"effort": effort}


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_opus47_effort_is_sent_without_thinking(effort):
    request = ModelRequest(model="claude-opus-4-7-thinking", system="system", messages=(),
                           thinking=False, effort=effort)
    wire = request.anthropic_kwargs(_settings())
    assert wire["thinking"] == {"type": "disabled"}
    assert wire["output_config"] == {"effort": effort}


@pytest.mark.parametrize("thinking", [False, True])
def test_anthropic_disable_setting_does_not_leak_to_other_protocols(thinking):
    request = ModelRequest(model="claude-opus-5", system="system", messages=(),
                           thinking=thinking, effort="low")
    chat = openai_chat_body(request, reasoning_transport="reasoning_effort")
    responses = openai_responses_body(request)
    for body in (chat, responses):
        assert "thinking" not in body
        assert "output_config" not in body
    assert chat.get("reasoning_effort") == ("low" if thinking else None)
    assert responses.get("reasoning") == (
        {"summary": "auto", "effort": "low"} if thinking else None
    )


@pytest.mark.parametrize("effort,expected", [("", "max"), ("low", "low"), ("high", "high"), ("max", "max")])
def test_kimi_chat_contract_sends_native_effort_and_output_limit(effort, expected):
    request = ModelRequest(
        model="kimi-k3",
        system="system",
        messages=(),
        thinking=True,
        effort=effort,
    )
    body = openai_chat_body(request, reasoning_transport="chat_template_kwargs")
    assert body["reasoning_effort"] == expected
    assert body["max_completion_tokens"] == request.max_tokens
    for field in ("chat_template_kwargs", "thinking", "max_tokens", "temperature", "top_p", "n", "presence_penalty", "frequency_penalty"):
        assert field not in body


@pytest.mark.parametrize("thinking,effort", [(False, ""), (True, "medium"), (True, "xhigh")])
def test_kimi_chat_rejects_invalid_reasoning(thinking, effort):
    with pytest.raises(ValueError, match="thinking|low, high, max"):
        openai_chat_body(ModelRequest(model="kimi-k3", system="", messages=(), thinking=thinking, effort=effort))


def test_kimi_structured_assistant_and_tool_history_is_preserved():
    history = ({"role": "assistant", "content": None, "reasoning_content": "full reasoning",
                "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "tool", "arguments": '{ "x": 1 }'}}]},
               {"role": "tool", "tool_call_id": "call-1", "content": "result"})
    body = openai_chat_body(ModelRequest(model="kimi-k3", system="", messages=history, effort="high"))
    assert body["messages"] == list(history)
    assert body["messages"][0] is not history[0]


@pytest.mark.parametrize("count", [1, 20])
@pytest.mark.parametrize("image_format", ["PNG", "JPEG"])
def test_transport_image_is_stable_from_first_attachment(count, image_format):
    import base64
    from io import BytesIO
    from PIL import Image
    buffer = BytesIO()
    Image.new("RGB", (2200, 1100), "navy").save(buffer, format=image_format)
    source = {"type": "base64", "media_type": Image.MIME[image_format], "data": base64.b64encode(buffer.getvalue()).decode()}
    parts = [{"type": "image", "source": source} for _ in range(count)]
    request = ModelRequest(model="test", system="test", messages=(
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "vision", "content": parts}]},))
    before = copy.deepcopy(request.messages)
    wire = request.anthropic_kwargs(_settings())["messages"][0]["content"][0]["content"]
    assert len(wire) == count and request.messages == before
    for block in wire:
        assert block["source"]["media_type"] == source["media_type"]
        with Image.open(BytesIO(base64.b64decode(block["source"]["data"]))) as image:
            assert image.size == (2000, 1000)
    again = ModelRequest(model="test", system="test", messages=request.messages)
    assert again.messages == request.messages
    assert source["data"] == base64.b64encode(buffer.getvalue()).decode()


def _response(*, signed: bool = False):
    content = []
    if signed:
        content.append(SimpleNamespace(type="thinking", signature="signed"))
    return SimpleNamespace(
        content=content,
        usage=SimpleNamespace(
            input_tokens=3,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            output_tokens=2,
        ),
    )


def _request(messages=None) -> ModelRequest:
    return ModelRequest(
        model="claude-test",
        system="system",
        messages=tuple(messages or [{"role": "user", "content": "continue"}]),
        max_tokens=32,
    )


@pytest.mark.parametrize("channel_count,expected_budgets,elapsed", [(1, [8], 10), (2, [8, 2], 9)])
def test_request_budget_includes_queue_request_and_backoff(channel_count, expected_budgets, elapsed):
    clock, budgets, events = [0.0], [], []

    @contextmanager
    def slot():
        clock[0] += 2
        yield

    class Transport:
        def complete(self, channel, request, *, total_timeout_s):
            budgets.append(total_timeout_s)
            if len(budgets) == 1:
                clock[0] += 4
                raise ProviderRequestTimeout("total_request", 4)
            clock[0] += min(1, total_timeout_s)
            return TransportResponse(_response(), first_byte_s=3)

    session = ModelSession(settings=replace(_settings(), channel_cooldown_s=0),
        channels=(ProviderChannel("synthetic-a", "A"), ProviderChannel("synthetic-b", "B"))[:channel_count],
        transport=Transport(), slot_factory=slot, monotonic=lambda: clock[0],
        sleeper=lambda delay: clock.__setitem__(0, clock[0] + delay), event_sink=events.append)
    result = session.call(_request(), request_budget_s=10)
    assert result.ok is (channel_count == 2)
    assert budgets == expected_budgets
    assert clock[0] == elapsed
    waits = [event for event in events if event["event_type"] == "resource_wait_completed"]
    assert all(event["queue_wait_s"] == 2 for event in waits)


def test_exhausted_review_request_budget_does_not_start_another_queue_or_http():
    clock, calls, queues = [0.0], [], []

    @contextmanager
    def slot():
        queues.append(True)
        clock[0] += 100
        yield

    class Transport:
        def complete(self, channel, request, *, total_timeout_s):
            calls.append(total_timeout_s)
            clock[0] += total_timeout_s
            raise ProviderRequestTimeout("total_request", total_timeout_s)

    session = ModelSession(settings=_settings(), channels=(ProviderChannel("synthetic", "A"),),
        transport=Transport(), slot_factory=slot, monotonic=lambda: clock[0], sleeper=lambda _: None)
    result = session.call(_request(), request_budget_s=4)
    assert result.failure.kind == "resource_queue_timeout"
    assert calls == [] and queues == [True]


def test_retry_backoff_cannot_overrun_remaining_request_budget():
    clock, sleeps, calls = [0.0], [], []

    class Transport:
        def complete(self, *args, **kwargs):
            calls.append(True)
            clock[0] += 2.5
            raise ProviderRequestTimeout("total_request", 2.5)

    def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    session = ModelSession(settings=_settings(), channels=(ProviderChannel("synthetic", "A"),),
        transport=Transport(), monotonic=lambda: clock[0], sleeper=sleep)
    result = session.call(_request(), request_budget_s=3)
    assert result.failure.kind == "timeout"
    assert result.failure.timeout_phase == "total_request"
    assert sleeps == [.5] and clock[0] == 3 and calls == [True]


@pytest.mark.parametrize("timeout,budget", [(0, 600), (10, 10)])
def test_default_budget_is_shared_across_retries_and_deadline_reaches_queue(timeout, budget):
    clock, calls, deadlines = [0.0], [], []

    @contextmanager
    def slot(*, deadline_monotonic):
        deadlines.append(deadline_monotonic)
        clock[0] += 1
        yield

    class Transport:
        def complete(self, channel, request, *, total_timeout_s):
            calls.append(total_timeout_s)
            clock[0] += total_timeout_s - 1
            raise ProviderRequestTimeout("total_request", total_timeout_s - 1)

    session = ModelSession(settings=replace(_settings(), total_timeout_s=timeout),
        channels=(ProviderChannel("synthetic", "A"),), transport=Transport(), slot_factory=slot,
        monotonic=lambda: clock[0], sleeper=lambda delay: clock.__setitem__(0, clock[0] + delay))
    result = session.call(_request())
    assert result.failure.kind == "timeout"
    assert result.failure.timeout_phase == "total_request"
    assert calls == [budget - 1] and deadlines == [budget] and clock[0] == budget


def test_configured_decision_budget_preserves_full_per_attempt_timeout():
    clock, budgets = [0.0], []

    @contextmanager
    def slot():
        clock[0] += 150
        yield

    class Transport:
        def complete(self, channel, request, *, total_timeout_s):
            budgets.append(total_timeout_s)
            if len(budgets) == 1:
                clock[0] += total_timeout_s
                raise ProviderRequestTimeout("total_request", total_timeout_s)
            clock[0] += 1
            return TransportResponse(_response(), first_byte_s=1)

    settings = replace(_settings(), total_timeout_s=600, request_budget_s=1500,
                       channel_cooldown_s=0, max_attempts=2)
    session = ModelSession(settings=settings,
        channels=(ProviderChannel("synthetic-a", "A"), ProviderChannel("synthetic-b", "B")),
        transport=Transport(), slot_factory=slot, monotonic=lambda: clock[0], sleeper=lambda _: None)
    assert session.call(_request()).ok
    assert budgets == [600, 600] and clock[0] == 901
    assert session.call(_request(), deadline_monotonic=clock[0]).failure.kind == "deadline"


def test_decision_budget_environment_is_explicit_and_validated():
    from runtime.config import provider_request_budget_s
    settings = ProviderSettings.from_environment({"MODEL": "kimi-k3", "MODEL_TIMEOUT_S": "600",
        "MODEL_REQUEST_BUDGET_S": "1500"})
    assert settings.total_timeout_s == 600 and provider_request_budget_s(settings) == 1500
    assert provider_request_budget_s(replace(settings, request_budget_s=0)) == 600
    with pytest.raises(ValueError, match="request budget"):
        replace(settings, request_budget_s=-1)


def _masked_gateway_400(*, param="", message="请求参数有误，请检查请求格式与参数", code="invalid_request"):
    response = httpx.Response(400, request=httpx.Request("POST", "https://tokenhub.sensetime.com/v1/chat/completions"),
        json={"error": {"type": "invalid_request_error", "code": code, "param": param, "message": message}})
    return httpx.HTTPStatusError("400 Bad Request", request=response.request, response=response)


def test_masked_k3_gateway_400_fails_over_once_without_changing_history_or_cot():
    settings = replace(_settings(), api_format="openai", model="kimi-k3",
        base_url="https://tokenhub.sensetime.com/v1", max_attempts=6, channel_cooldown_s=0)
    messages = [{"role": "user", "content": "continue"},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "actual provider reasoning"},
            {"type": "tool_use", "id": "call_stable", "name": "read_file", "input": {"path": "deck.md"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_stable", "content": "plan"}]}]
    request = ModelRequest(model="kimi-k3", system="system", messages=tuple(messages), thinking=True, effort="high")
    response = _response()
    response.content = [{"type": "thinking", "thinking": "new actual provider reasoning"},
                        {"type": "text", "text": "done"}]
    events = []
    transport = FakeTransport([_masked_gateway_400(), response])
    session = ModelSession(settings=settings,
        channels=(ProviderChannel("synthetic-a", "A"), ProviderChannel("synthetic-b", "B")),
        transport=transport, event_sink=events.append)
    result = session.call(request, preferred_channel_id="A")
    assert result.ok and result.attempts == 2
    assert [channel for channel, _ in transport.calls] == ["A", "B"]
    assert transport.calls[0][1] == transport.calls[1][1] == request
    assert result.response.content == response.content
    failure = next(event for event in events if event["event_type"] == "provider_attempt_failed")
    assert failure["error_kind"] == "upstream_ambiguous_400" and failure["next_channel_id"] == "B"


def test_masked_k3_gateway_400_does_not_retry_forever_or_retry_the_same_channel():
    settings = replace(_settings(), api_format="openai", model="kimi-k3",
        base_url="https://tokenhub.sensetime.com/v1", max_attempts=6, channel_cooldown_s=0)
    request = ModelRequest(model="kimi-k3", system="system", messages=(), thinking=True, effort="high")
    for channels, expected in [((ProviderChannel("synthetic-a", "A"),), 1),
            ((ProviderChannel("synthetic-a", "A"), ProviderChannel("synthetic-b", "B")), 2)]:
        transport = FakeTransport([_masked_gateway_400(), _masked_gateway_400(), _response()])
        result = ModelSession(settings=settings, channels=channels, transport=transport).call(request)
        assert not result.ok and result.failure.kind == "http_error"
        assert len(transport.calls) == expected


@pytest.mark.parametrize("model,base_url,error", [
    ("kimi-k3", "https://tokenhub.sensetime.com/v1", {"param": "messages"}),
    ("kimi-k3", "https://tokenhub.sensetime.com/v1", {"message": "Invalid tool pairing"}),
    ("kimi-k3", "https://tokenhub.sensetime.com/v1", {"code": "invalid_tool_call"}),
    ("kimi-k3", "https://provider.example/v1", {}),
    ("qwen3.8", "https://tokenhub.sensetime.com/v1", {}),
])
def test_masked_k3_gateway_400_keeps_explicit_and_other_provider_errors_terminal(model, base_url, error):
    settings = replace(_settings(), api_format="openai", model=model, base_url=base_url,
                       max_attempts=6, channel_cooldown_s=0)
    request = ModelRequest(model=model, system="system", messages=(), thinking=True, effort="high")
    transport = FakeTransport([_masked_gateway_400(**error), _response()])
    result = ModelSession(settings=settings,
        channels=(ProviderChannel("synthetic-a", "A"), ProviderChannel("synthetic-b", "B")),
        transport=transport).call(request)
    assert not result.ok and result.failure.kind == "http_error" and len(transport.calls) == 1


def test_explicit_deadline_takes_priority_over_default_budget():
    clock, deadlines = [5.0], []

    @contextmanager
    def slot(*, deadline_monotonic):
        deadlines.append(deadline_monotonic)
        clock[0] = deadline_monotonic
        yield

    transport = FakeTransport([])
    session = ModelSession(settings=_settings(), channels=(ProviderChannel("synthetic", "A"),),
        transport=transport, slot_factory=slot, monotonic=lambda: clock[0])
    result = session.call(_request(), deadline_monotonic=8)
    assert result.failure.kind == "resource_queue_timeout"
    assert deadlines == [8] and not transport.calls


@pytest.mark.parametrize("budget,deadline", [(0, None), (-1, None), (1, 5)])
def test_request_budget_has_one_clock_contract(budget, deadline):
    session = ModelSession(settings=_settings(), channels=(ProviderChannel("synthetic", "A"),),
        transport=FakeTransport([]))
    with pytest.raises(ValueError, match="request budget"):
        session.call(_request(), request_budget_s=budget, deadline_monotonic=deadline)


def test_anthropic_refusal_is_a_typed_provider_failure_without_empty_retry():
    from runtime.session import _normalize_anthropic_response

    response = _normalize_anthropic_response(SimpleNamespace(
        content=[],
        stop_reason="refusal",
        stop_details=SimpleNamespace(
            type="refusal",
            category="reasoning_extraction",
            explanation="must remain raw-only",
        ),
        usage=_response().usage,
    ))
    assert response.stop_details == {
        "type": "refusal",
        "category": "reasoning_extraction",
    }

    transport = FakeTransport([response, _response()])
    events = []
    session = ModelSession(
        settings=_settings(),
        channels=(ProviderChannel("synthetic", "A"),),
        transport=transport,
        event_sink=events.append,
    )
    result = session.call(_request())

    assert not result.ok
    assert result.failure is not None
    assert result.failure.kind == "provider_policy_refusal"
    assert not result.failure.retryable
    assert len(transport.calls) == 1
    failed = [event for event in events if event["event_type"] == "provider_attempt_failed"]
    assert len(failed) == 1
    assert failed[0]["semantic_response_failure"] is True
    assert failed[0]["response_diagnostics"]["stop_details"] == response.stop_details
    assert not [event for event in events if event["event_type"] == "provider_attempt_succeeded"]
    usage = session.usage.summary()
    assert usage["failed_attempts"] == 1
    assert usage["provider_channel_successes"] == {}


@pytest.mark.parametrize("block", [
    {"type": "thinking", "thinking": "Synthetic summary", "signature": "synthetic-signature"},
    {"type": "redacted_thinking", "data": "synthetic-redacted-data"},
])
def test_normalized_signed_response_pins_successful_failover_channel(block):
    from runtime.session import _normalize_anthropic_response

    normalized = _normalize_anthropic_response(SimpleNamespace(
        content=[block, {"type": "text", "text": "ready"}],
        stop_reason="end_turn", usage=_response().usage,
    ))
    assert isinstance(normalized.content, tuple)
    response = httpx.Response(503, request=httpx.Request("POST", "https://provider.test"))
    error = httpx.HTTPStatusError("No available channel", request=response.request, response=response)
    channels = (ProviderChannel("synthetic-i", "I"), ProviderChannel("synthetic-l", "L"))
    transport = FakeTransport([error, normalized, _response()])
    events = []
    session = ModelSession(settings=_settings(), channels=channels, transport=transport,
        event_sink=events.append, balance="primary-first", monotonic=lambda: 0.0)

    assert session.call(_request()).ok
    assert session.pool.on_cooldown(channels[0])
    assert session.provider_affinity_id == "L"
    messages = [
        {"role": "user", "content": "start"},
        {"role": "assistant", "content": list(normalized.content)},
        {"role": "user", "content": "next"},
    ]
    assert session.call(_request(messages)).ok
    assert [channel for channel, _ in transport.calls] == ["I", "L", "L"]
    started = [event for event in events if event["event_type"] == "provider_attempt_started"][-1]
    assert started["channel_affinity_id"] == "L" and started["channel_affinity_enforced"]
    assert replay_provider_events(events).failovers == 1
    assert "synthetic-signature" not in json.dumps(events)
    assert "synthetic-redacted-data" not in json.dumps(events)


def test_resource_queue_timeout_does_not_fail_over_or_consume_provider_attempts(tmp_path):
    from runtime.production import ResourceSlotTimeout
    from runtime.live_trace import LiveEventJournal

    clock = [0.0]
    blocked = [True]
    events = []

    @contextmanager
    def slot():
        clock[0] += 7
        if blocked[0]:
            raise ResourceSlotTimeout("model queue deadline reached")
        yield

    transport = FakeTransport([_response()])
    checkpoint = SegmentCheckpoint.initial(segment_id="same-segment")
    session = ModelSession(settings=_settings(),
        channels=(ProviderChannel("secret-a", "ch-a"), ProviderChannel("secret-b", "ch-b")),
        transport=transport, checkpoint=checkpoint, event_sink=events.append,
        balance="primary-first", slot_factory=slot, monotonic=lambda: clock[0])
    failed = session.call(_request())
    assert failed.failure.kind == "resource_queue_timeout"
    assert failed.failure.timeout_phase == "queue_wait"
    assert failed.attempts == 0 and not failed.segment_rollover_required
    assert not transport.calls
    assert session.usage.summary()["attempts"] == 0
    assert [e["event_type"] for e in events] == ["resource_wait_started", "resource_wait_failed"]
    assert events[-1]["queue_wait_s"] == 7
    assert replay_provider_events(events).attempts == 0

    blocked[0] = False
    assert session.call(_request()).ok
    assert [channel for channel, _ in transport.calls] == ["ch-a"]
    assert session.checkpoint == checkpoint
    assert session.usage.summary()["provider_failovers"] == 0
    journal = LiveEventJournal(tmp_path / "live-events.jsonl")
    for event in events:
        public_event = {**event, "agent_id": "owner-a", "role": "slide", "turn": 1,
                        "segment_id": "same-segment"}
        assert len(journal.record_runtime_event(public_event)) == 1
        assert not journal.record_runtime_event(public_event)
    public = [json.loads(line) for line in journal.path.read_text().splitlines()]
    assert len(public) == 6
    assert "provider_failed" not in {e["event_type"] for e in public}
    assert "secret-a" not in json.dumps(public)


def test_provider_trace_write_failure_does_not_send_or_count_request():
    transport = FakeTransport([_response()])

    def fail_started(event):
        if event["event_type"] == "provider_attempt_started":
            raise RuntimeError("trace storage unavailable")

    session = ModelSession(settings=_settings(), channels=(ProviderChannel("secret", "ch-a"),),
        transport=transport, event_sink=fail_started)
    with pytest.raises(RuntimeError, match="trace storage unavailable"):
        session.call(_request())
    assert not transport.calls
    assert session.usage.summary()["attempts"] == 0
    assert session.usage.summary()["failed_attempts"] == 0


def test_provider_elapsed_excludes_local_queue_wait():
    clock = [0.0]
    events = []

    @contextmanager
    def slot():
        clock[0] += 7
        yield

    class TimedTransport(FakeTransport):
        def complete(self, *args, **kwargs):
            clock[0] += 3
            return super().complete(*args, **kwargs)

    session = ModelSession(settings=_settings(), channels=(ProviderChannel("secret", "ch-a"),),
        transport=TimedTransport([_response()]), event_sink=events.append,
        slot_factory=slot, monotonic=lambda: clock[0])
    assert session.call(_request()).ok
    assert [e["event_type"] for e in events] == ["resource_wait_started", "resource_wait_completed",
        "provider_attempt_started", "provider_attempt_succeeded"]
    assert events[1]["queue_wait_s"] == 7
    assert events[2]["queue_wait_s"] == 7
    assert events[3]["elapsed_s"] == 3


def test_deadline_reached_in_queue_never_contacts_provider():
    clock = [0.0]

    @contextmanager
    def slot():
        clock[0] = 5
        yield

    transport = FakeTransport([_response()])
    session = ModelSession(settings=_settings(), channels=(ProviderChannel("secret", "ch-a"),),
        transport=transport, slot_factory=slot, monotonic=lambda: clock[0])
    result = session.call(_request(), deadline_monotonic=4)
    assert result.failure.kind == "resource_queue_timeout"
    assert result.attempts == 0
    assert not transport.calls


@pytest.mark.parametrize('thinking,effort', [(False, 'medium'), (True, 'low'), (True, 'high')])
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('api_format', ['anthropic', 'openai_chat_compatible', 'openai_responses'])
def test_provider_started_records_only_safe_effective_runtime_config(tmp_path, thinking, effort, streaming, api_format):
    from dataclasses import replace
    from runtime.live_trace import LiveEventJournal
    events = []
    session = ModelSession(settings=replace(_settings(), streaming=streaming, api_format=api_format), channels=(ProviderChannel('private-key', 'ch-a'),),
                           transport=FakeTransport([_response()]), event_sink=events.append)
    model = 'claude-opus-4-7-thinking' if api_format == 'anthropic' else 'gpt-5.2'
    assert session.call(replace(_request(), model=model, thinking=thinking, effort=effort)).ok
    started = next(e for e in events if e['event_type'] == 'provider_attempt_started')
    assert started['effective_thinking'] is thinking
    sent_effort = effort if thinking or api_format == 'anthropic' else ''
    assert started['effective_effort'] == sent_effort
    assert started['configured_effort'] == effort
    assert started['server_confirmed_effort'] is None
    assert not {'messages','system','thinking','signature','key'} & started.keys()
    assert 'private-key' not in json.dumps(events)
    journal = LiveEventJournal(tmp_path / 'live-events.jsonl')
    public = journal.record_runtime_event({**started, 'agent_id':'orchestrator',
                                          'role':'orchestrator', 'segment_id':'s1'})[0]
    assert public['event_type'] == 'provider_started'
    assert public['payload']['effective_thinking'] is thinking
    assert public['payload']['effective_effort'] == sent_effort
    assert public['payload']['sent_reasoning_parameters'] == started['sent_reasoning_parameters']
    assert public['payload']['server_confirmed_effort'] is None
    assert public['payload']['read_timeout_s'] == (120 if streaming else started['total_timeout_s'])
    assert 0 < public['payload']['read_timeout_s'] <= 600


@pytest.mark.parametrize("model,thinking,effort,sent", [
    ("Qwen3.8-27B", True, "low", "low"), ("Qwen3.8-27B", True, "medium", "medium"),
    ("Qwen3.8-27B", True, "xhigh", "xhigh"), ("Qwen3.8-27B", False, "xhigh", ""),
    ("Qwen3.5-27B", True, "medium", ""), ("generic-chat", True, "medium", ""),
])
def test_chat_effort_event_records_only_serialized_parameters(model, thinking, effort, sent):
    from dataclasses import replace
    events = []
    settings = replace(_settings(), api_format="openai_chat_compatible", model=model)
    session = ModelSession(settings=settings, channels=(ProviderChannel("fixture-key", "test"),),
        transport=FakeTransport([_response()]), event_sink=events.append)
    request = replace(_request(), model=model, thinking=thinking, effort=effort)
    assert session.call(request).ok
    event = next(item for item in events if item["event_type"] == "provider_attempt_started")
    assert event["configured_effort"] == effort and event["effective_effort"] == sent
    assert event["server_confirmed_effort"] is None
    wire = openai_chat_body(request)
    expected = {"chat_template_kwargs.enable_thinking": thinking} if model.startswith("Qwen") else {}
    if sent:
        expected["chat_template_kwargs.reasoning_effort"] = wire["chat_template_kwargs"]["reasoning_effort"]
        if "reasoning_effort" in wire:
            expected["reasoning_effort"] = wire["reasoning_effort"]
    assert event["sent_reasoning_parameters"] == expected


def test_invalid_effort_does_not_trigger_provider_retry_or_failover():
    from dataclasses import replace
    transport, events = FakeTransport([]), []
    session = ModelSession(settings=_settings(), channels=(ProviderChannel("first", "a"), ProviderChannel("second", "b")),
        transport=transport, event_sink=events.append)
    with pytest.raises(ValueError, match="requires thinking enabled"):
        session.call(replace(_request(), model="claude-opus-5", thinking=False, effort="max"))
    assert not transport.calls and not events


@pytest.mark.parametrize("message,recovers", [
    ("Access to Anthropic models is not allowed for this account.", True),
    ("messages: invalid tool schema", False),
])
def test_gateway_account_rejection_can_fail_over_but_invalid_requests_cannot(message, recovers):
    from anthropic import BadRequestError
    response = httpx.Response(400, request=httpx.Request("POST", "https://provider.test"))
    error = BadRequestError(message, response=response, body={"error": {"message": message}})
    transport = FakeTransport([error, _response()])
    events = []
    session = ModelSession(settings=_settings(),
        channels=(ProviderChannel("secret-a", "ch-a"), ProviderChannel("secret-b", "ch-b")),
        transport=transport, event_sink=events.append, balance="primary-first")
    result = session.call(_request())
    assert result.ok is recovers
    assert len(transport.calls) == (2 if recovers else 1)
    replay = replay_provider_events(events)
    assert replay.failovers == int(recovers)
    assert not replay.open_attempt_ids
    assert "secret-a" not in repr(events) and "secret-b" not in repr(events)


def test_invalid_chat_tool_json_regenerates_once_before_any_tool_is_visible():
    from runtime.session import _normalize_openai_response

    class ChatTransport:
        def __init__(self):
            self.calls = []
            self.arguments = ['{"path":"deck.html"', '{"path":"deck.html"}']

        def complete(self, channel, request, *, total_timeout_s):
            self.calls.append((channel.channel_id, copy.deepcopy(request)))
            arguments = self.arguments.pop(0)
            decoded = {
                "choices": [{
                    "message": {"role": "assistant", "tool_calls": [{
                        "id": "new-call",
                        "type": "function",
                        "function": {"name": "write_file", "arguments": arguments},
                    }]},
                    "finish_reason": "tool_calls",
                }],
                "usage": {},
            }
            return TransportResponse(_normalize_openai_response(decoded), first_byte_s=0.25)

    completed_history = [
        {"role": "assistant", "content": [{
            "type": "tool_use", "id": "done-call", "name": "write_file",
            "input": {"path": "prior.html"},
        }]},
        {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "done-call", "content": "written",
        }]},
        {"role": "user", "content": "continue"},
    ]
    events = []
    transport = ChatTransport()
    session = ModelSession(settings=replace(
            _settings(), api_format="openai_chat_compatible", model="Qwen3.8-27B"),
        channels=(ProviderChannel("secret-a", "ch-a"), ProviderChannel("secret-b", "ch-b")),
        transport=transport, event_sink=events.append, balance="primary-first",
        sleeper=lambda _delay: pytest.fail("protocol regeneration must not use transport backoff"))
    request = _request(completed_history)
    result = session.call(request)
    assert result.ok and result.attempts == 2
    assert [channel for channel, _ in transport.calls] == ["ch-a", "ch-a"]
    assert all(call.messages == request.messages for _, call in transport.calls)
    assert result.response.tool_calls == ({
        "type": "tool_use", "id": "new-call", "name": "write_file",
        "input": {"path": "deck.html"},
    },)
    replay = replay_provider_events(events)
    assert replay.failovers == 0 and not replay.open_attempt_ids
    failures = [event for event in events if event["event_type"] == "provider_attempt_failed"]
    assert len(failures) == 1
    assert failures[0]["error_kind"] == "provider_protocol_error"
    assert failures[0]["partial_response_discarded"] is True


def test_repeated_provider_protocol_error_is_terminal_without_failover():
    events = []
    transport = FakeTransport([
        ProviderProtocolError("Chat tool arguments contain invalid JSON"),
        ProviderProtocolError("Chat tool arguments contain invalid JSON"),
        _response(),
    ])
    session = ModelSession(settings=_settings(),
        channels=(ProviderChannel("secret-a", "ch-a"), ProviderChannel("secret-b", "ch-b")),
        transport=transport, event_sink=events.append, balance="primary-first",
        sleeper=lambda _delay: pytest.fail("protocol regeneration must not use transport backoff"))
    result = session.call(_request())
    assert not result.ok and result.failure.kind == "provider_protocol_error"
    assert result.failure.retryable is False
    assert [channel for channel, _ in transport.calls] == ["ch-a", "ch-a"]
    assert result.attempts == 2
    replay = replay_provider_events(events)
    assert replay.failovers == 0 and not replay.open_attempt_ids


@pytest.mark.parametrize("field_words", [False, True])
def test_provider_failure_keeps_error_message_but_not_private_request_envelope(field_words):
    message = ('invalid tool argument for secret-channel-key' if not field_words else
               'body: invalid tool argument; system: configured; token: 2048; thinking_signature=private-signature')
    response = httpx.Response(400, request=httpx.Request("POST", "https://provider.test"),
        json={"error": {"type": "invalid_request_error", "message": message},
              "request": {"messages": ["private-request"], "thinking_signature": "private-signature"}})
    failure = httpx.HTTPStatusError("sensitive exception private-request", request=response.request, response=response)
    events = []
    session = ModelSession(settings=_settings(), channels=(ProviderChannel("secret-channel-key", "ch-a"),),
        transport=FakeTransport([failure]), event_sink=events.append)
    assert not session.call(_request()).ok
    event = next(e for e in events if e['event_type'] == 'provider_attempt_failed')
    assert event['status_code'] == 400
    assert 'invalid_request_error' in event['error_summary']
    assert 'invalid tool argument' in event['error_summary']
    if field_words:
        assert 'system: configured' in event['error_summary'] and 'token: 2048' in event['error_summary']
    encoded = json.dumps(events)
    assert message in event['error_summary']
    assert 'private-request' not in encoded


def test_unread_http_error_stream_does_not_mask_provider_failure():
    response = httpx.Response(400, request=httpx.Request("POST", "https://provider.test"),
        stream=httpx.ByteStream(b"private response"))
    failure = httpx.HTTPStatusError("request rejected", request=response.request, response=response)
    events = []
    session = ModelSession(settings=_settings(), channels=(ProviderChannel("secret", "ch-a"),),
        transport=FakeTransport([failure]), event_sink=events.append)
    assert session.call(_request()).failure.status_code == 400
    assert events[-1]['error_summary'] == 'HTTPStatusError'
    assert not response.is_stream_consumed


def test_first_byte_timeout_fails_over_without_mutating_messages() -> None:
    request = _request()
    original = copy.deepcopy(request.messages)
    transport = FakeTransport(
        [ProviderRequestTimeout("first_byte", 120), _response()]
    )
    events: list[dict] = []
    sleeps: list[float] = []
    session = ModelSession(
        settings=_settings(),
        channels=(ProviderChannel("secret-a", "ch-a"), ProviderChannel("secret-b", "ch-b")),
        transport=transport,
        event_sink=lambda event: events.append(dict(event)),
        balance="primary-first",
        sleeper=sleeps.append,
    )
    result = session.call(request)
    assert result.ok
    assert [channel for channel, _request_value in transport.calls] == ["ch-a", "ch-b"]
    assert request.messages == original
    assert sleeps == []
    replay = replay_provider_events(events)
    assert (replay.attempts, replay.failures, replay.successes) == (2, 1, 1)
    assert replay.failovers == 1
    assert replay.timeout_phases == {"first_byte": 1}
    assert "secret-a" not in repr(events) and "secret-b" not in repr(events)
    usage = session.usage.summary()
    assert usage["attempts"] == 2
    assert usage["failed_attempts"] == 1
    assert usage["provider_failovers"] == 1


def test_all_channels_are_tried_before_waiting_for_earliest_cooldown() -> None:
    transport = FakeTransport(
        [
            ProviderRequestTimeout("first_byte", 120),
            ProviderRequestTimeout("first_byte", 120),
            _response(),
        ]
    )
    sleeps, clock = [], [0.0]
    def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay
    session = ModelSession(
        settings=_settings(),
        channels=(ProviderChannel("secret-a", "ch-a"), ProviderChannel("secret-b", "ch-b")),
        transport=transport,
        balance="primary-first",
        sleeper=sleep,
        monotonic=lambda: clock[0],
    )
    result = session.call(_request(), max_attempts=3)
    assert result.ok
    assert [channel for channel, _value in transport.calls] == ["ch-a", "ch-b", "ch-a"]
    assert sleeps == [120]


def test_three_account_429_rotation_cooldown_and_actual_failover_trace():
    clock, active, sleeps, events = [0.0], [False], [], []
    channels = tuple(ProviderChannel(f"fixture-{name}", name) for name in "ABC")
    request = httpx.Request("POST", "https://provider.test/v1/messages")
    rejected = httpx.HTTPStatusError("system memory limit, please slow down", request=request,
        response=httpx.Response(429, request=request, headers={"Retry-After": "7"}))
    transport = FakeTransport([rejected, rejected, rejected, _response()])

    @contextmanager
    def slot():
        assert not active[0]
        active[0] = True
        try:
            yield
        finally:
            active[0] = False

    def sleep(delay):
        assert not active[0]
        sleeps.append(delay)
        clock[0] += delay

    session = ModelSession(settings=_settings(), channels=channels, transport=transport,
        balance="primary-first", slot_factory=slot, monotonic=lambda: clock[0],
        sleeper=sleep, event_sink=events.append)
    assert session.call(_request(), max_attempts=4).ok
    assert [channel for channel, _ in transport.calls] == ["A", "B", "C", "A"]
    assert sleeps == [7]
    waits = [e for e in events if e.get("resource") == "provider_cooldown"]
    assert [e["event_type"] for e in waits] == ["resource_wait_started", "resource_wait_completed"]
    assert waits[-1]["queue_wait_s"] == 7
    assert replay_provider_events(events).failovers == session.usage.summary()["provider_failovers"] == 3


def test_retry_exhaustion_does_not_claim_an_unexecuted_account_switch():
    transport = FakeTransport([ProviderRequestTimeout("first_byte", 60)])
    events = []
    session = ModelSession(settings=_settings(), transport=transport, event_sink=events.append,
        channels=(ProviderChannel("fixture-a", "A"), ProviderChannel("fixture-b", "B")),
        balance="primary-first", sleeper=lambda _: pytest.fail("no wait after final attempt"))
    result = session.call(_request(), max_attempts=1)
    assert not result.ok and len(transport.calls) == 1
    failed = next(e for e in events if e["event_type"] == "provider_attempt_failed")
    assert not failed["next_channel_id"] and not failed["retryable"]
    assert session.usage.summary()["provider_failovers"] == replay_provider_events(events).failovers == 0
    # 旧日志曾在最终失败上写“下一个账号”；没有后续真实开始，不算发生切换。
    failed.update(next_channel_id="B", retryable=True)
    assert replay_provider_events(events).failovers == 0


def test_cooldown_cancellation_never_enters_resource_slot_or_http():
    class Stop:
        cancelled = False
        def is_set(self):
            return self.cancelled
        def wait(self, delay):
            assert delay == 120
            self.cancelled = True
            return True
    channel = ProviderChannel("fixture", "A")
    events = []
    session = ModelSession(settings=_settings(), channels=(channel,), transport=FakeTransport([]),
        monotonic=lambda: 0, stop_event=Stop(), event_sink=events.append)
    session.pool.cool_down(channel)
    with pytest.raises(InterruptedError, match="cooldown cancelled"):
        session.call(_request())
    assert session.usage.summary()["attempts"] == 0
    assert events[-1]["cancelled"] is True


def test_cooldown_keeps_signed_affinity_until_closed_segment_rollover():
    channels = tuple(ProviderChannel(f"fixture-{name}", name) for name in "ABC")
    pool = ChannelPool(channels, cooldown_s=120, balance="primary-first", monotonic=lambda: 0)
    pool.cool_down(channels[0])
    ordered, locked = pool.request_order(affinity_id="A", signed_history=True)
    assert locked and ordered == (channels[0],)
    assert pool.select(ordered, set(), 1) == channels[0]
    assert pool.alternative(channels[0], attempted_ids={"A", "B"}) == channels[2]


@pytest.mark.parametrize('primary_status,backup_status', [(403, 200), (504, 200), (403, 403)])
def test_primary_backup_quota_and_gateway_failures_are_bounded(primary_status, backup_status):
    def outcome(status):
        if status == 200:
            return _response()
        request = httpx.Request('POST', 'https://provider.test/v1/messages')
        return httpx.HTTPStatusError('quota precharge rejected' if status == 403 else 'gateway timeout',
            request=request, response=httpx.Response(status, request=request))
    transport = FakeTransport([outcome(primary_status), outcome(backup_status)])
    events, sleeps = [], []
    session = ModelSession(settings=_settings(),
        channels=(ProviderChannel('fixture-i', 'I'), ProviderChannel('fixture-j', 'J')),
        transport=transport, balance='primary-first', event_sink=events.append, sleeper=sleeps.append)
    request = _request()
    result = session.call(request)
    assert result.ok == (backup_status == 200)
    assert [channel for channel, _ in transport.calls] == ['I', 'J']
    assert all(value.messages == request.messages for _, value in transport.calls)
    assert not sleeps
    replay = replay_provider_events(events)
    assert replay.attempts == 2 and replay.failovers == 1
    if not result.ok:
        assert result.failure.status_code == 403 and result.attempts == 2


def test_signed_affinity_rolls_over_before_trying_another_channel() -> None:
    signed_messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "", "signature": "signed-by-a"}
            ],
        }
    ]
    checkpoint = SegmentCheckpoint(
        segment_id="segment-1",
        predecessor_segment_id=None,
        messages=tuple(signed_messages),
        provider_affinity_id="ch-a",
    )
    transport = FakeTransport([ProviderRequestTimeout("first_byte", 120)])
    session = ModelSession(
        settings=_settings(),
        channels=(ProviderChannel("secret-a", "ch-a"), ProviderChannel("secret-b", "ch-b")),
        transport=transport,
        checkpoint=checkpoint,
        balance="primary-first",
        sleeper=lambda _seconds: None,
    )
    result = session.call(_request(signed_messages))
    assert not result.ok
    assert result.segment_rollover_required
    assert result.next_channel_id == "ch-b"
    assert [channel for channel, _request_value in transport.calls] == ["ch-a"]
    recovered = session.roll_over_after_failure(
        failed_channel_id="ch-a",
        reason="channel_rollover_required",
        new_segment_id="segment-2",
    )
    assert recovered.provider_affinity_id == ""
    assert "signature" not in repr(recovered.messages)


def test_timeout_phase_distinguishes_first_byte_and_stream_idle() -> None:
    request = httpx.Request("POST", "https://provider.example/v1/messages")
    first = FakeTransport([httpx.ReadTimeout("idle", request=request)])
    session = ModelSession(
        settings=_settings(),
        channels=(ProviderChannel("secret-a", "ch-a"),),
        transport=first,
        balance="primary-first",
        sleeper=lambda _seconds: None,
    )
    result = session.call(_request(), max_attempts=1)
    assert result.failure and result.failure.timeout_phase == "first_byte"


@pytest.mark.parametrize("cooldown", [0, 120])
@pytest.mark.parametrize("transient", [
    ProviderRequestTimeout("first_byte", 120),
    httpx.ConnectError("temporary connection failure"),
])
def test_transient_signed_rollover_can_return_to_primary_when_backup_is_unavailable(transient, cooldown):
    from anthropic import InternalServerError
    from dataclasses import replace

    messages = [
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "private", "signature": "signed-by-j"},
            {"type": "tool_use", "id": "committed-write", "name": "write_file", "input": {"path": "deck.md"}},
        ]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "committed-write", "content": "already written"}]},
    ]
    unavailable = InternalServerError("No available channel for model claude-opus-5",
        response=httpx.Response(503, request=httpx.Request("POST", "https://provider.test")),
        body={"error": {"code": "model_not_found", "message": "No available channel"}})
    transport = FakeTransport([transient, unavailable, _response()])
    events = []
    session = ModelSession(settings=replace(_settings(), channel_cooldown_s=cooldown),
        channels=(ProviderChannel("secret-j", "ch-j"), ProviderChannel("secret-l", "ch-l")),
        checkpoint=SegmentCheckpoint(segment_id="signed-j", predecessor_segment_id=None,
                                     messages=tuple(messages), provider_affinity_id="ch-j"),
        transport=transport, event_sink=events.append, balance="primary-first", sleeper=lambda _: None)
    failed = session.call(_request(messages))
    assert failed.segment_rollover_required and failed.next_channel_id == "ch-l"
    recovered = session.roll_over_after_failure(failed_channel_id=failed.channel_id,
                                               reason=failed.failure.kind)
    result = session.call(_request(recovered.messages), preferred_channel_id=failed.next_channel_id)
    assert result.ok, "A transient primary failure must not permanently exclude the only working channel"
    assert [channel for channel, _ in transport.calls] == ["ch-j", "ch-l", "ch-j"]
    assert recovered.excluded_channel_ids == ()
    for _, request in transport.calls[1:]:
        assert "signed-by-j" not in repr(request.messages)
        assert "committed-write" in repr(request.messages) and "already written" in repr(request.messages)
    assert "secret-j" not in repr(events) and "secret-l" not in repr(events)


def test_channel_ids_are_stable_anonymous_and_labelled() -> None:
    environment = {
        "MODEL_API_KEY": "secret-k",
        "MODEL_API_KEYS": "secret-k,secret-a",
        "MODEL_CHANNEL_LABELS": "k,a",
    }
    first = provider_channels(environment)
    second = provider_channels(environment)
    assert first == second
    assert [channel.channel_id.split("-")[1] for channel in first] == ["k", "a"]
    assert "secret" not in repr(first)


def test_balanced_pool_rotates_new_unsigned_segments() -> None:
    pool = ChannelPool(
        (ProviderChannel("secret-a", "ch-a"), ProviderChannel("secret-b", "ch-b")),
        cooldown_s=0,
        balance="balanced",
    )
    first, first_locked = pool.request_order()
    second, second_locked = pool.request_order()
    assert not first_locked and not second_locked
    assert first[0].channel_id == "ch-a"
    assert second[0].channel_id == "ch-b"


def test_independent_sessions_spread_initial_channels_and_keep_signed_affinity():
    from collections import Counter
    from concurrent.futures import ThreadPoolExecutor
    from runtime.recovery import SegmentCheckpoint

    channels = tuple(ProviderChannel(f"fixture-{i}", f"ch-{i}") for i in range(3))

    def first_channel(index):
        checkpoint = SegmentCheckpoint.initial(segment_id=f"independent-deck-owner-{index}")
        transport = FakeTransport([_response(signed=True), _response(signed=True)])
        session = ModelSession(settings=_settings(), channels=channels, transport=transport,
                               checkpoint=checkpoint, balance="balanced")
        assert session.call(_request()).ok
        selected = transport.calls[0][0]
        assert session.provider_affinity_id == selected
        assert session.call(_request()).ok
        assert transport.calls[1][0] == selected
        restored = SegmentCheckpoint.from_dict(session.checkpoint.to_dict())
        resumed_transport = FakeTransport([_response(signed=True)])
        resumed = ModelSession(settings=_settings(), channels=channels, transport=resumed_transport,
                               checkpoint=restored, balance="balanced")
        assert resumed.call(_request()).ok
        assert resumed_transport.calls[0][0] == selected
        return selected

    with ThreadPoolExecutor(max_workers=8) as pool:
        counts = Counter(pool.map(first_channel, range(90)))
    assert set(counts) == {"ch-0", "ch-1", "ch-2"}
    assert all(15 <= count <= 45 for count in counts.values())


def test_initial_segment_seed_is_stable_but_primary_first_is_unchanged():
    channels = tuple(ProviderChannel(f"fixture-{i}", f"ch-{i}") for i in range(3))
    for index in range(12):
        seed = f"fixed-segment-{index}"
        before = ChannelPool(channels, balance="balanced", initial_segment_id=seed)
        after = ChannelPool(channels, balance="balanced", initial_segment_id=seed)
        assert before.request_order() == after.request_order()
        primary = ChannelPool(channels, balance="primary-first", initial_segment_id=seed)
        assert primary.request_order()[0][0] == channels[0]
        assert primary.request_order(affinity_id="ch-2")[0] == (channels[2],)
        ordered, locked = before.request_order(excluded_ids=("ch-0",))
        assert not locked and {channel.channel_id for channel in ordered} == {"ch-1", "ch-2"}


def test_auth_rejection_stays_excluded_after_compaction_and_resume():
    from runtime.recovery import compact_segment
    error = httpx.HTTPStatusError("quota exhausted", request=httpx.Request("POST", "https://provider.test"),
        response=httpx.Response(403))
    channels = (ProviderChannel("secret-j", "J"), ProviderChannel("secret-k", "K"))
    transport = FakeTransport([error, _response()])
    session = ModelSession(settings=_settings(), channels=channels, transport=transport,
        balance="primary-first", sleeper=lambda _: None)
    assert session.call(_request()).ok
    assert session.checkpoint.excluded_channel_ids == ("J",)
    compacted = compact_segment(session.checkpoint, summary="current work", reason="text_budget")
    saved = SegmentCheckpoint.from_dict(json.loads(json.dumps(compacted.to_dict())))
    next_transport = FakeTransport([_response()])
    resumed = ModelSession(settings=_settings(), channels=channels, checkpoint=saved,
        transport=next_transport, balance="primary-first")
    assert resumed.call(_request(saved.messages)).ok
    assert [channel for channel, _ in next_transport.calls] == ["K"]


def test_exhausted_channels_do_not_reenter_pool_on_resume():
    checkpoint = SegmentCheckpoint(segment_id="failed", predecessor_segment_id=None,
        messages=(), excluded_channel_ids=("J", "K"))
    transport = FakeTransport([])
    session = ModelSession(settings=_settings(),
        channels=(ProviderChannel("secret-j", "J"), ProviderChannel("secret-k", "K")),
        checkpoint=checkpoint, transport=transport)
    result = session.call(_request())
    assert not result.ok and result.failure.kind == "channel_pool_exhausted"
    assert not result.segment_rollover_required and result.attempts == 0 and not transport.calls


def test_zero_response_timeout_still_has_finite_default_request_budget():
    from runtime.session import _request_timeout
    for api_format in ("anthropic", "openai_chat_compatible", "openai_responses"):
        for streaming in (False, True):
            settings = ProviderSettings(api_format=api_format, model="test", base_url="https://provider.test",
                total_timeout_s=0, idle_timeout_s=1, streaming=streaming)
            phase = _request_timeout(settings, None, streaming=streaming)
            assert phase.read is None and phase.connect == settings.connect_timeout_s
            budgets, events = [], []
            class Transport(FakeTransport):
                def complete(self, channel, request, *, total_timeout_s):
                    budgets.append(total_timeout_s)
                    return super().complete(channel, request, total_timeout_s=total_timeout_s)
            session = ModelSession(settings=settings, channels=(ProviderChannel("secret", "local"),),
                transport=Transport([_response()]), event_sink=events.append)
            assert session.call(_request()).ok
            assert 0 < budgets[0] <= 600
            event = next(e for e in events if e["event_type"] == "provider_attempt_started")
            assert 0 < event["total_timeout_s"] <= 600
            assert 0 < event["request_budget_s"] <= 600


def test_any_api_format_can_use_an_explicit_authoritative_pool(tmp_path: Path) -> None:
    pool_file = tmp_path / "provider.env"
    pool_file.write_text(
        "MODEL_API_KEY=approved-k\n"
        "MODEL_API_KEYS=approved-k,approved-a\n"
        "MODEL_API_KEY_FALLBACKS=approved-a\n"
        "MODEL_CHANNEL_LABELS=k,a\n",
        encoding="utf-8",
    )
    channels = provider_channels(
        {
            "MODEL_POOL_ENV_PATH": str(pool_file),
            "MODEL_API_KEY": "retired-key",
        }
    )
    assert [channel.key for channel in channels] == ["approved-k", "approved-a"]
    assert all(channel.key != "retired-key" for channel in channels)


def test_real_trace_projection_replays_the_observed_failover() -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "provider-failover-189.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    summary = replay_provider_events(fixture["events"])
    assert fixture["source_sha256"] == (
        "c6ef3cf7724982dd9b83a76353daceee93fc31022bb23b543063215e1c5a7871"
    )
    assert summary.attempts == 2
    assert summary.failures == summary.failovers == 1
    assert summary.successes == 1
    assert summary.timeout_phases == {"first_byte": 1}


def test_failed_trace_replay_can_report_an_open_attempt_without_inventing_result() -> None:
    events = [
        {
            "event_type": "provider_attempt_started",
            "attempt_id": "attempt-open",
            "channel_id": "ch-a",
        }
    ]
    with pytest.raises(ValueError, match="unclosed provider attempts"):
        replay_provider_events(events)
    summary = replay_provider_events(events, allow_open=True)
    assert summary.attempts == 1
    assert summary.successes == summary.failures == 0
    assert summary.open_attempt_ids == ("attempt-open",)


def _openai_settings() -> ProviderSettings:
    return ProviderSettings(
        api_format="openai",
        model="student-test",
        base_url="https://provider.example/v1",
        total_timeout_s=600,
        channel_cooldown_s=120,
        max_attempts=3,
        streaming=False,
        prompt_cache=False,
        prompt_cache_messages=False,
    )


def _error_tool_turn():
    """A closed assistant tool_use followed by an is_error tool_result."""
    return [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call-1", "name": "read_file", "input": {"path": "x"}}
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-1",
                    "content": "owner may not read x",
                    "is_error": True,
                    "error_code": "read_scope_denied",
                }
            ],
        },
    ]


def test_openai_tool_result_carries_error_marker_and_code() -> None:
    messages = to_openai_messages("", _error_tool_turn())
    tool_messages = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    content = tool_messages[0]["content"]
    assert OPENAI_TOOL_ERROR_MARKER in content
    assert "error_code=read_scope_denied" in content
    # 原始错误正文仍保留，供模型据此改路。
    assert "owner may not read x" in content


def test_openai_successful_tool_result_has_no_error_marker() -> None:
    ok_turn = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "ok-1", "name": "write_file", "input": {}}
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "ok-1", "content": "wrote 1 file"}
            ],
        },
    ]
    tool_messages = [
        m for m in to_openai_messages("", ok_turn) if m.get("role") == "tool"
    ]
    assert len(tool_messages) == 1
    assert OPENAI_TOOL_ERROR_MARKER not in tool_messages[0]["content"]


def test_anthropic_wire_strips_the_internal_error_code_key() -> None:
    request = ModelRequest(
        model="claude-test",
        system="sys",
        messages=tuple(_error_tool_turn()),
    )
    kwargs = request.anthropic_kwargs(_settings())
    result_block = kwargs["messages"][-1]["content"][0]
    # is_error 属于 Anthropic 合法 schema，保留；error_code 是内部键，必须剥离。
    assert result_block["is_error"] is True
    assert "error_code" not in result_block


def test_anthropic_wire_drops_empty_thinking_blocks_but_keeps_real_ones() -> None:
    # opus-5 adaptive thinking 对平凡 prompt 跳过思考却留下空 thinking 分块;回放给
    # Anthropic 会 400 "each thinking block must contain thinking"。空块必须剔除,
    # 但有内容/有 redacted data 的思考分块(携带签名)必须保留。
    messages = (
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "", "signature": "sig-empty"},
                {"type": "thinking", "thinking": "真实推理", "signature": "sig-real"},
                {"type": "redacted_thinking", "data": "enc-blob"},
                {"type": "redacted_thinking", "data": ""},
                {"type": "text", "text": "可见回答"},
            ],
        },
        {"role": "user", "content": [{"type": "text", "text": "继续"}]},
    )
    request = ModelRequest(model="claude-test", system="sys", messages=messages)
    kwargs = request.anthropic_kwargs(_settings())
    kept = kwargs["messages"][0]["content"]
    types = [(b.get("type"), b.get("thinking") or b.get("data") or b.get("text")) for b in kept]
    assert ("thinking", "真实推理") in types
    assert ("redacted_thinking", "enc-blob") in types
    assert ("text", "可见回答") in types
    # 两个空块被剔除。
    assert all(not (t == "thinking" and v == "") for t, v in types)
    assert all(not (t == "redacted_thinking" and v == "") for t, v in types)
    assert len(kept) == 3
    # 未触及的 user 消息原样保留。
    assert kwargs["messages"][1]["content"][0]["text"] == "继续"


def test_anthropic_wire_drops_empty_text_without_changing_raw_or_tool_pairing():
    from runtime.recovery import tool_boundary
    call = {"type": "tool_use", "id": "inspect-1", "name": "inspect",
            "input": {"path": "renders/slide_05.png", "question": ""}}
    result = {"type": "tool_result", "tool_use_id": "inspect-1", "content": "view delivered"}
    history = (
        {"role": "user", "content": "continue"},
        {"role": "assistant", "content": [{"type": "text", "text": ""}, call]},
        {"role": "user", "content": [result]},
        {"role": "assistant", "content": [{"type": "text", "text": ""}]},
        {"role": "user", "content": "next"},
    )
    original = copy.deepcopy(history)
    request = ModelRequest(model="claude-test", system="sys", messages=history)
    wire = request.anthropic_kwargs(replace(_settings(), prompt_cache=False))["messages"]
    assert history == original and request.messages == original
    assert wire[1]["content"] == [call] and wire[2]["content"] == [result]
    assert wire[1]["content"][0]["input"]["question"] == ""
    assert len(wire) == 4 and tool_boundary(wire).is_closed
    assert all(message.get("content") for message in wire)


def test_response_diagnostics_flags_empty_tool_calls() -> None:
    # BUG-Q/BUG-P 排障:provider-events 要能一眼看出 opus-5 发出空参/残缺工具调用。
    from runtime.session import _response_diagnostics

    response = SimpleNamespace(
        content=[
            {"type": "text", "text": "I'll submit"},
            {"type": "tool_use", "name": "delegate_submit", "input": {}},
            {"type": "tool_use", "name": "write_file",
             "input": {"path": "a.md", "content": "x"}},
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(output_tokens=1234),
    )
    diag = _response_diagnostics(response)
    assert diag["stop_reason"] == "tool_use"
    assert diag["output_tokens"] == 1234
    assert diag["block_count"] == 3
    assert diag["has_empty_tool_call"] is True
    empty = next(b for b in diag["blocks"] if b.get("tool_name") == "delegate_submit")
    assert empty["empty_args"] is True and empty["arg_keys"] == []
    full = next(b for b in diag["blocks"] if b.get("tool_name") == "write_file")
    assert full["empty_args"] is False and full["arg_keys"] == ["content", "path"]
    # text 只记长度不落正文。
    text_block = next(b for b in diag["blocks"] if b["type"] == "text")
    assert text_block["text_len"] == len("I'll submit") and "text" not in text_block


def test_response_diagnostics_reads_tuple_content_from_normalized_response() -> None:
    # 回归:NormalizedAssistantResponse.content 是 tuple(非 list)。诊断早前只认
    # list,把归一响应误判成空 blocks → has_empty_tool_call 永远 False、全程漏报。
    # 这里用 tuple content 复刻真实归一响应,确保 tool_use 分块被真正解析。
    from runtime.session import _response_diagnostics

    response = SimpleNamespace(
        content=(
            {"type": "tool_use", "name": "delegate_submit", "input": {}},
        ),
        stop_reason="tool_use",
        usage=SimpleNamespace(output_tokens=81),
    )
    diag = _response_diagnostics(response)
    assert diag["block_count"] == 1
    assert diag["has_empty_tool_call"] is True
    only = diag["blocks"][0]
    assert only["tool_name"] == "delegate_submit"
    assert only["empty_args"] is True and only["arg_keys"] == []


# --- 层②:输入 token 估算(BUG-1 pre-send 预算门用) ---


def test_estimate_request_tokens_scales_with_text_length() -> None:
    from runtime.session import estimate_request_tokens

    small = estimate_request_tokens("sys", [{"role": "user", "content": "hi"}], ())
    big_text = "x" * 10000
    big = estimate_request_tokens(
        "sys", [{"role": "user", "content": big_text}], ()
    )
    assert big > small
    # 保守高估:1万字符应估到数千 token 量级(系数 ~0.6)。
    assert big >= 5000


def test_estimate_request_tokens_counts_system_tools_and_blocks() -> None:
    from runtime.session import estimate_request_tokens

    messages = [
        {"role": "user", "content": "abc"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "wrote it"},
                {"type": "tool_use", "id": "t1", "name": "write_file", "input": {"path": "p", "content": "hello"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
            ],
        },
    ]
    tools = ({"name": "write_file", "description": "d", "input_schema": {"type": "object"}},)
    with_tools = estimate_request_tokens("system prompt", messages, tools)
    without_tools = estimate_request_tokens("system prompt", messages, ())
    assert with_tools > without_tools > 0


def test_estimate_request_tokens_does_not_explode_on_images() -> None:
    from runtime.session import estimate_request_tokens

    # 一张巨大的 base64 图片不能按字符数×0.6 计,否则每个 vision 轮误触发压缩。
    huge_b64 = "A" * 500000
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": huge_b64}},
            ],
        }
    ]
    estimated = estimate_request_tokens("", messages, ())
    # 固定图片常量兜住:远小于把 50 万字符当文本(~30 万 token)。
    assert estimated < 10000


def test_request_timeout_read_covers_full_generation_when_non_streaming() -> None:
    # 非流式:整个生成期间不发字节,read 必须给到整个 total,否则长生成会 ReadTimeout 误断;
    # 流式:read = chunk 间 idle 间隔。
    from runtime.session import _request_timeout

    settings = _settings()  # total=600, connect=30, idle=120
    stream_t = _request_timeout(settings, 600, streaming=True)
    nonstream_t = _request_timeout(settings, 600, streaming=False)
    assert stream_t.read == 120.0        # 流式:chunk 间 idle
    assert nonstream_t.read == 600.0     # 非流式:覆盖整个生成
    assert nonstream_t.connect == stream_t.connect == 30.0
    # write 与 read 同步(非流式发大 body 也不该被 idle 掐断)。
    assert nonstream_t.write == 600.0 and stream_t.write == 120.0


def test_non_streaming_anthropic_call_passes_explicit_timeout_to_bypass_guard() -> None:
    # 非流式路径必须显式传 timeout(httpx.Timeout),据此绕过 SDK 的 10min 非流式守卫;
    # 否则 max_tokens>~21333 会被 SDK 直接 ValueError 拒。
    import httpx
    from runtime.session import AnthropicTransport, ProviderChannel

    captured: dict[str, object] = {}

    class FakeMessages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return _response()

    class FakeClient:
        def __init__(self):
            self.messages = FakeMessages()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    settings = ProviderSettings(
        api_format="anthropic", model="claude-opus-5",
        base_url="https://provider.example", total_timeout_s=600,
        channel_cooldown_s=120, max_attempts=3, streaming=False,
        prompt_cache=False, prompt_cache_messages=False,
    )
    transport = AnthropicTransport(settings)
    transport._client = lambda channel: FakeClient()
    request = ModelRequest(
        model="claude-opus-5", system="s",
        messages=({"role": "user", "content": "go"},), max_tokens=40960,
    )
    transport.complete(ProviderChannel("secret", "ch-a"), request, total_timeout_s=600)
    # 显式 timeout 已传 → is_given=True → SDK 守卫被跳过。
    assert isinstance(captured.get("timeout"), httpx.Timeout)
    assert captured["timeout"].read == 600.0


@pytest.mark.parametrize("content", [
    [{"type": "thinking", "thinking": "synthetic"}],
    [{"type": "tool_use", "id": "empty", "name": "write_file", "input": {}}],
])
def test_anthropic_empty_action_is_returned_once_and_its_usage_is_recorded(content):
    from dataclasses import replace
    from runtime.session import AnthropicTransport

    calls = []

    class FakeClient:
        def __init__(self):
            self.messages = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(content=content, stop_reason="end_turn", usage=_response().usage)

    settings = replace(_settings(), streaming=False)
    transport = AnthropicTransport(settings)
    transport._client = lambda channel: FakeClient()
    events = []
    session = ModelSession(settings=settings, channels=(ProviderChannel("synthetic", "a"),),
        transport=transport, event_sink=events.append)
    result = session.call(_request())
    assert result.ok and list(result.response.content) == content
    assert len(calls) == session.usage.summary()["attempts"] == 1
    assert session.usage.summary()["output_tokens"] == 2
    assert replay_provider_events(events).successes == 1
    assert "degenerate_resamples" not in events[-1]
