from __future__ import annotations

import copy
from dataclasses import replace

import pytest

from runtime.recovery import (
    RecoveryAction,
    RecoveryBoundaryError,
    SegmentCheckpoint,
    compact_segment,
    contains_signed_thinking,
    decide_recovery,
    ProviderFailure,
    roll_over_segment,
    tool_boundary,
    validate_segment_lineage,
    classify_provider_exception,
    CompactionSummaryError,
    context_compaction_summary,
)


def _closed_messages() -> list[dict]:
    return [
        {"role": "user", "content": "render page 2"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "private",
                    "signature": "signed-by-channel-a",
                },
                {"type": "tool_use", "id": "tool-1", "name": "render", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tool-1", "content": "ok"}
            ],
        },
    ]


def test_compaction_waits_for_an_open_tool_transaction() -> None:
    messages = _closed_messages()[:-1]
    checkpoint = SegmentCheckpoint.initial(messages, segment_id="segment-1")
    assert tool_boundary(messages).open_tool_use_ids == ("tool-1",)
    with pytest.raises(RecoveryBoundaryError, match="cannot compact"):
        compact_segment(checkpoint, summary="resume summary")


def test_closed_transaction_compacts_into_an_unsigned_segment() -> None:
    checkpoint = SegmentCheckpoint.initial(_closed_messages(), segment_id="segment-1")
    compacted = compact_segment(
        checkpoint,
        summary="Page 2 rendered and the transaction is closed.",
        new_segment_id="segment-2",
    )
    assert compacted.predecessor_segment_id == "segment-1"
    assert compacted.provider_affinity_id == ""
    assert not contains_signed_thinking(compacted.messages)
    validate_segment_lineage([checkpoint, compacted])


def test_channel_rollover_keeps_closed_results_but_drops_signed_thinking() -> None:
    checkpoint = SegmentCheckpoint(
        segment_id="segment-1",
        predecessor_segment_id=None,
        messages=tuple(_closed_messages()),
        provider_affinity_id="ch-a",
    )
    recovered = roll_over_segment(
        checkpoint,
        failed_channel_id="ch-a",
        reason="invalid_thinking_signature_recovery",
        invalid_signature=True,
        new_segment_id="segment-2",
    )
    assert recovered.provider_affinity_id == ""
    assert recovered.excluded_channel_ids == ("ch-a",)
    assert not contains_signed_thinking(recovered.messages)
    assert "tool_result" in repr(recovered.messages)
    with pytest.raises(RecoveryBoundaryError, match="limit was reached"):
        roll_over_segment(
            recovered,
            failed_channel_id="ch-b",
            reason="invalid_thinking_signature_recovery",
            invalid_signature=True,
        )


def test_signed_segment_requires_rollover_before_cross_channel_retry() -> None:
    decision = decide_recovery(
        ProviderFailure("timeout", "first byte", retryable=True),
        current_channel_id="ch-a",
        alternative_channel_id="ch-b",
        segment_locked=True,
        attempts_remaining=True,
    )
    assert decision.action is RecoveryAction.ROLL_OVER_SEGMENT
    assert decision.recovery_boundary == "last_closed_tool_transaction"


def test_protocol_regeneration_stays_on_same_channel_before_failover() -> None:
    decision = decide_recovery(
        ProviderFailure(
            "provider_protocol_error",
            "Chat tool arguments contain invalid JSON",
            retryable=True,
        ),
        current_channel_id="ch-a",
        alternative_channel_id="ch-b",
        segment_locked=False,
        attempts_remaining=True,
    )
    assert decision.action == RecoveryAction.RETRY_SAME_CHANNEL
    assert decision.next_channel_id == "ch-a"
    assert decision.partial_response_discarded is True


def test_rejected_channel_without_an_alternative_does_not_retry_bad_credentials() -> None:
    decision = decide_recovery(
        ProviderFailure(
            "authentication",
            "credential rejected",
            status_code=401,
            retryable=True,
            channel_specific=True,
        ),
        current_channel_id="ch-a",
        alternative_channel_id="",
        attempts_remaining=True,
    )
    assert decision.action is RecoveryAction.STOP


@pytest.mark.parametrize("header,expected", [("7", 7), ("0", 0), ("-1", None),
    ("nan", None), ("inf", None), ("not-a-date", None)])
def test_retry_after_header_is_preserved_for_provider_cooldown(header, expected):
    import httpx
    request = httpx.Request("POST", "https://provider.test/v1/messages")
    response = httpx.Response(429, request=request, headers={"Retry-After": header})
    failure = classify_provider_exception(httpx.HTTPStatusError("limited", request=request, response=response))
    assert failure.retryable and failure.retry_after_s == expected


def test_retry_after_http_date_is_supported():
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime
    import httpx
    request = httpx.Request("POST", "https://provider.test/v1/messages")
    header = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=90), usegmt=True)
    response = httpx.Response(503, request=request, headers={"Retry-After": header})
    failure = classify_provider_exception(httpx.HTTPStatusError("busy", request=request, response=response))
    assert 88 <= failure.retry_after_s <= 90


def test_final_unsigned_failure_stops_instead_of_promising_another_account():
    decision = decide_recovery(ProviderFailure("rate_limit", "busy", retryable=True),
        current_channel_id="A", alternative_channel_id="B", attempts_remaining=False)
    assert decision.action is RecoveryAction.STOP
    assert not decision.next_channel_id


@pytest.mark.parametrize("status", [400, 401, 403])
def test_policy_disabled_access_stops_even_with_an_alternative_channel(status) -> None:
    class RejectedAccount(Exception):
        status_code = status

    failure = classify_provider_exception(RejectedAccount(
        "Your access has been disabled for a suspected violation of our Usage Policy."
    ))
    assert failure.kind == "access_disabled"
    assert not failure.retryable and not failure.channel_specific
    decision = decide_recovery(
        failure, current_channel_id="ch-a", alternative_channel_id="ch-b",
        segment_locked=True, attempts_remaining=True,
    )
    assert decision.action is RecoveryAction.STOP


def test_forward_or_self_referencing_lineage_is_rejected() -> None:
    initial = SegmentCheckpoint.initial(segment_id="segment-1")
    forward = SegmentCheckpoint(
        segment_id="segment-2",
        predecessor_segment_id="segment-3",
        messages=(),
    )
    with pytest.raises(ValueError, match="missing or forward"):
        validate_segment_lineage([initial, forward])
    with pytest.raises(ValueError, match="own predecessor"):
        SegmentCheckpoint(
            segment_id="segment-loop",
            predecessor_segment_id="segment-loop",
            messages=(),
        )


def test_invalid_signature_is_a_recoverable_segment_boundary() -> None:
    class BadRequest(Exception):
        status_code = 400

    failure = classify_provider_exception(
        BadRequest("Bedrock ValidationException: invalid `signature` in thinking block")
    )
    assert failure.kind == "bad_request"
    assert failure.invalid_signature
    assert failure.retryable


def test_only_empty_sdk_assertions_are_transport_retryable() -> None:
    assert classify_provider_exception(AssertionError()).retryable
    assert not classify_provider_exception(
        AssertionError("invalid local response contract")
    ).retryable


def test_lazy_anthropic_sdk_keeps_real_exception_classification():
    import anthropic
    import httpx
    request = httpx.Request("POST", "https://fixture.invalid/v1/messages")
    timeout = anthropic.APITimeoutError(request=request)
    assert classify_provider_exception(timeout).timeout_phase == "first_byte"
    assert classify_provider_exception(timeout, response_started=True).timeout_phase == "stream_idle"
    assert classify_provider_exception(anthropic.APIConnectionError(request=request)).retryable
    for cls, status in ((anthropic.RateLimitError, 429), (anthropic.InternalServerError, 500),
                        (anthropic.BadRequestError, 400)):
        error = cls("fixture", response=httpx.Response(status, request=request), body={})
        failure = classify_provider_exception(error)
        assert failure.status_code == status
        assert failure.retryable == (status != 400)


def test_duplicate_tool_use_id_cannot_become_a_recovery_boundary() -> None:
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tool-1", "name": "render"},
                {"type": "tool_use", "id": "tool-1", "name": "render"},
            ],
        }
    ]
    with pytest.raises(RecoveryBoundaryError, match="duplicate tool_use_id"):
        tool_boundary(messages)


@pytest.mark.parametrize("text,stop_reason", [("", "stop"), ("", "end_turn"), (" \n\t", "stop")])
def test_empty_compaction_retries_once_with_k3_contract_and_shared_deadline(
    monkeypatch, text, stop_reason,
) -> None:
    import runtime.agent as agent_module
    from runtime.agent import AgentRuntime
    from runtime.session import ProviderChannel
    from runtime.tools import ToolRuntime
    from test_agent_runtime import ScriptedTransport, _settings, _summary_result

    monkeypatch.setattr(agent_module.time, "monotonic", lambda: 500.0)
    first = _summary_result(text, stop_reason=stop_reason)
    original_response = copy.deepcopy(first.response)
    outcomes = [first, _summary_result("Keep page 2 and check its source.")]
    calls = []

    def invoke(request, **context):
        calls.append((request, context))
        return outcomes.pop(0)

    settings = replace(_settings(max_attempts=1), api_format="openai", model="kimi-k3",
        context_window=131072, context_safety_buffer=1000)
    runtime = AgentRuntime(settings=settings, channels=(ProviderChannel("secret", "a"),),
        transport=ScriptedTransport([]), tools=ToolRuntime(), summary_invoker=invoke,
        compact_trigger_ratio=0.5)
    history = _closed_messages() + [{"role": "user", "content": "evidence " * 15000}]
    original = copy.deepcopy(history)
    selected, metrics = runtime._compact_messages(history, system="fixture", tools=(),
        max_tokens=40960, agent_id="slide", role="slide", thinking=True, effort="high")
    assert len(calls) == 2
    assert {context["deadline_monotonic"] for _, context in calls} == {530.0}
    assert all(request.thinking and request.effort == "low" and request.tools == ()
        and request.request_kind == "context_compaction" for request, _ in calls)
    assert "terse, complete handoff" in calls[1][0].system
    assert calls[1][0].max_tokens == 16384
    assert metrics["summary_calls"] == 2 and metrics["summary_retries"] == 1
    assert "Keep page 2" in str(selected) and tool_boundary(selected).is_closed
    assert history == original and first.response == original_response
    assert not runtime.tools.events


@pytest.mark.parametrize("stop_reason", ["stop", "end_turn", "length", "max_tokens"])
@pytest.mark.parametrize("fallback_text", ["", " \n\t"])
def test_compaction_empty_fallback_retains_metrics_and_original_checkpoint(stop_reason, fallback_text) -> None:
    from test_agent_runtime import _settings, _summary_result

    checkpoint = SegmentCheckpoint.initial(_closed_messages(), segment_id="original")
    original = checkpoint.to_dict()
    first = _summary_result("unfinished" if stop_reason in {"length", "max_tokens"} else "",
        stop_reason=stop_reason)
    first.response.usage["output_tokens"] = 626
    second = _summary_result(fallback_text, stop_reason="end_turn")
    responses = [first, second]
    before = copy.deepcopy(responses)
    calls = []

    def invoke(request):
        calls.append(request)
        return responses[len(calls) - 1]

    with pytest.raises(CompactionSummaryError, match="summary has no visible handoff") as caught:
        context_compaction_summary(checkpoint.messages, agent_id="slide", role="slide",
            current_task=None, settings=_settings(), max_tokens=40960, thinking=True,
            effort="high", target_tokens=8192, invoke=invoke)
    metrics = caught.value.metrics
    assert len(calls) == metrics["summary_calls"] == 2
    assert metrics["summary_retries"] == 1
    assert metrics["summary_stop_reasons"] == [stop_reason, "end_turn"]
    assert metrics["summary_usage"] == {"input_tokens": 200, "output_tokens": 656}
    assert metrics["summary_duration_ms"] >= 0
    assert checkpoint.to_dict() == original and responses == before
    assert all(request.tools == () for request in calls)


@pytest.mark.parametrize("stop_reason", ["end_turn", "length", "max_tokens", "tool_use"])
def test_failed_compaction_preserves_parent_checkpoint(stop_reason) -> None:
    from runtime.agent import AgentInterruptedError
    from runtime.tools import ToolContext, ToolRuntime
    from test_agent_runtime import ScriptedTransport, _summary_result
    from test_context_compaction import _run, _runtime

    checkpoint = SegmentCheckpoint.initial(
        [{"role": "user", "content": "evidence " * 20000}], segment_id="original")
    original = checkpoint.to_dict()
    checkpoints, events, requests = [], [], []
    result = _summary_result("", stop_reason=stop_reason, tool_calls=(
        {"type": "tool_use", "id": "not-executed", "name": "patch", "input": {}},
    ) if stop_reason == "tool_use" else ())
    runtime = _runtime(ScriptedTransport([]), ToolRuntime(), event_sink=events.append,
        summary_invoker=lambda request, **kw: requests.append(request) or result)
    with pytest.raises(AgentInterruptedError):
        _run(runtime, ToolContext("slide", "slide"), checkpoint=checkpoint,
            checkpoint_sink=checkpoints.append)
    expected_requests = 1 if stop_reason == "tool_use" else 2
    assert len(requests) == expected_requests and checkpoints == [checkpoint]
    assert checkpoint.to_dict() == original
    assert not any(event["event_type"] == "agent_context_compacted" for event in events)
    assert events[-2]["event_type"] == "agent_context_compaction_failed"
    assert events[-2]["recoverable"] is True
    assert not runtime.tools.events
