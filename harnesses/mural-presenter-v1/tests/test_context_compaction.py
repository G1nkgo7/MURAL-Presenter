import copy
from contextlib import nullcontext
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

import runtime.agent as agent_module
from runtime.agent import (AgentContextWindowError, AgentInterruptedError, AgentProtocolError,
    AgentProviderError, AgentRuntime, RepeatedToolFailureError)
from runtime.live_trace import LiveEventJournal, iter_jsonl_events
from runtime.language_policy import visible_output_language_reminder
from runtime.recovery import CompactionSummaryError, ProviderFailure, SegmentCheckpoint, context_compaction_summary, tool_boundary
from runtime.session import (ModelSession, ProviderChannel, ProviderProtocolError, SessionResult,
    estimate_request_tokens)
from runtime.tools import ToolContext, ToolExecutionError, ToolRuntime
from runtime.vision import image_blocks, visual_compaction_tail
from test_agent_runtime import (ScriptedTransport, _definition, _response, _settings, _text, _tool,
    _summary_invoker, _summary_result)
from test_visual_context import _history, _source


def _runtime(transport, tools, *, context_window=20000, **kwargs):
    kwargs.setdefault("summary_invoker", _summary_invoker)
    return AgentRuntime(settings=replace(_settings(max_attempts=1), context_window=context_window, context_safety_buffer=1000),
        channels=(ProviderChannel("fixture-secret", "a"),), transport=transport, tools=tools, **kwargs)


def _run(runtime, context, **kwargs):
    return runtime.run(agent_id=context.owner_id, role=context.role, system="fixture", tool_context=context,
        max_turns=8, max_tokens=128, thinking=False, effort="low", **kwargs)


def _summarize(messages, invoke, *, current=None, context_window=131072, max_tokens=40960):
    return context_compaction_summary(messages, agent_id="slide-3", role="slide", current_task=current,
        settings=replace(_settings(), context_window=context_window, context_safety_buffer=8192),
        max_tokens=max_tokens, thinking=True, effort="xhigh", target_tokens=8192, invoke=invoke,
        training_sample_max_tokens=131072)


@pytest.mark.parametrize("max_tokens,expected", [(40960, 81920), (32768, 90112)])
def test_soft_watermark_and_output_reservation_use_effective_training_window(max_tokens, expected):
    calls = []
    runtime = AgentRuntime(settings=replace(_settings(), context_window=262144, context_safety_buffer=8192),
        context_window_limit=131072, channels=(ProviderChannel("test", "a"),),
        transport=ScriptedTransport([]), tools=ToolRuntime(),
        summary_invoker=lambda request, **kw: calls.append(request) or _summary_result())
    messages = [{"role": "user", "content": "x" * int((expected + 200) / 0.6)}]
    before = copy.deepcopy(messages)
    selected, metrics = runtime._compact_messages(messages, system="system", tools=(), max_tokens=max_tokens,
        agent_id="owner", role="slide", thinking=True, effort="xhigh")
    assert metrics["input_budget"] == expected
    assert metrics["soft_watermark_tokens"] == int(131072 * .9)
    assert metrics["pressure"] == ["tokens"]
    assert metrics["tokens_after"] <= 49152 and metrics["target_met"]
    assert all(request.max_tokens == 16384 and not request.thinking and request.effort == "" for request in calls)
    assert messages == before and tool_boundary(selected).is_closed


def test_soft_watermark_can_trigger_before_request_capacity():
    messages = [{"role": "user", "content": "x" * 200000}]
    runtime = _runtime(ScriptedTransport([]), ToolRuntime(), context_window=131072)
    _, metrics = runtime._compact_messages(messages, system="fixture", tools=(), max_tokens=128,
        agent_id="owner", role="slide")
    assert metrics["pressure"] == ["soft_tokens"]
    assert metrics["tokens_before"] < metrics["input_budget"]


def test_two_summaries_replace_prior_handoff_and_preserve_constraints_in_real_next_input():
    constraint = "P2 只改数据不改变版式；P3 不再使用被否定的环形图。"
    first = constraint + " P3 尚需修图；对比图说明保留。"
    second = constraint + " P3 已修正；接下来检查来源限定。"
    calls = []
    def invoke(request, **kw):
        calls.append(request)
        return _summary_result(first)
    runtime = _runtime(ScriptedTransport([]), ToolRuntime(), context_window=131072, summary_invoker=invoke)
    current = {"goal": constraint, "current_instructions": {"briefs/slide_03.md": "shared plan " * 20000},
               "artifact_hashes": {"briefs/slide_03.md": "a" * 64}}
    history = [{"role": "user", "content": constraint}, {"role": "assistant", "content": "old log " * 30000}]
    one, metrics = runtime._compact_messages(history, system="fixture", tools=(), max_tokens=40960,
        agent_id="slide-3", role="slide", continuation_context=lambda: current)
    assert constraint in one[0]["content"] and "shared plan " * 20000 not in str(one)
    # 长当前输入可能需要多次容量内摘要调用；后续断言只检查接线，不用 fixture 证明模型理解质量。
    calls.clear()
    def update(request, **kw):
        calls.append(request)
        assert first in request.messages[0]["content"] or second in request.messages[0]["content"]
        return _summary_result(second)
    runtime.summary_invoker = update
    two, _ = runtime._compact_messages([*one, {"role": "user", "content": "new evidence " * 20000}],
        system="fixture", tools=(), max_tokens=40960, agent_id="slide-3", role="slide")
    assert two[0]["content"].count("[compacted context") == 1
    assert second in two[0]["content"] and "尚需修图" not in two[0]["content"]
    assert str(two).count("[compacted context") == 1
    assert "尚需修图" not in str(two)
    assert metrics["summary_calls"] >= 1 and tool_boundary(two).is_closed


def test_long_evidence_is_fully_chunked_with_preflight_and_no_hidden_thinking():
    raw = {"role": "user", "content": "BEGIN-" + "0123456789abcdef" * 22000 + "-END"}
    requests = []
    def invoke(request):
        requests.append(request)
        assert estimate_request_tokens(request.system, request.messages, request.tools) <= 81920
        assert request.max_tokens == 16384 and not request.thinking and request.effort == ""
        assert request.request_kind == "context_compaction" and request.tools == ()
        return _summary_result("Keep the unresolved constraint and source boundary.")
    summary, metrics = _summarize([raw], invoke)
    fragments = [request.messages[0]["content"].split("Read the next ordered evidence fragments:\n", 1)[1]
                 for request in requests]
    reassembled = "".join(fragment.split(":\n", 1)[1] for fragment in fragments)
    assert reassembled == json.dumps(raw, ensure_ascii=False)
    assert len(requests) > 1 and metrics["summary_calls"] == len(requests)
    assert metrics["summary_usage"]["output_tokens"] == 30 * len(requests)
    assert "Keep the unresolved" in summary

    captured = []
    _summarize([{"role": "assistant", "content": [
        {"type": "thinking", "thinking": "PRIVATE", "signature": "SIGNED"},
        {"type": "text", "text": "visible decision"}]}],
        lambda request: captured.append(request) or _summary_result())
    assert "PRIVATE" not in str(captured) and "SIGNED" not in str(captured)
    assert "visible decision" in str(captured)


def test_k3_compaction_fragments_and_short_retry_share_one_deadline(monkeypatch):
    calls = []
    monkeypatch.setattr(agent_module.time, "monotonic", lambda: 500.0)

    def invoke(request, **context):
        calls.append((request, context))
        if len(calls) == 1:
            return _summary_result("unfinished", stop_reason="max_tokens")
        return _summary_result("Keep the verified facts and unresolved source boundary.")

    settings = replace(_settings(max_attempts=1), api_format="openai", model="kimi-k3",
        context_window=131072, context_safety_buffer=1000)
    runtime = AgentRuntime(settings=settings, channels=(ProviderChannel("secret", "a"),),
        transport=ScriptedTransport([]), tools=ToolRuntime(), summary_invoker=invoke)
    history = [{"role": "user", "content": "evidence " * 22000}]
    original = copy.deepcopy(history)
    selected, metrics = runtime._compact_messages(history, system="fixture", tools=(),
        max_tokens=40960, agent_id="research", role="research", thinking=True, effort="high")
    assert len(calls) >= 3 and {context["deadline_monotonic"] for _, context in calls} == {530.0}
    assert all(request.thinking and request.effort == "low" for request, _ in calls)
    assert all("thinking disabled" not in request.system for request, _ in calls)
    assert metrics["summary_thinking"] and metrics["summary_effort"] == "low"
    assert metrics["summary_retries"] == 1 and calls[1][0].max_tokens == 16384
    assert all(request.max_tokens > 8192 for request, _ in calls)
    assert history == original and tool_boundary(selected).is_closed


def test_summary_max_tokens_retries_once_with_terse_no_thinking_prompt():
    requests = []
    outcomes = [_summary_result("unfinished", stop_reason="max_tokens"),
                _summary_result("Compact complete handoff.")]
    def invoke(request):
        requests.append(request)
        return outcomes.pop(0)
    summary, metrics = _summarize([{"role": "user", "content": "Keep current goal."}], invoke)
    assert "Compact complete handoff" in summary
    assert len(requests) == 2
    assert all(not request.thinking and request.effort == "" for request in requests)
    assert requests[0].max_tokens == 16384
    assert requests[1].max_tokens == 4096
    assert "terse, complete handoff" in requests[1].system
    assert metrics["summary_retries"] == 1
    assert metrics["summary_stop_reasons"] == ["max_tokens", "end_turn"]


def test_compaction_summary_survives_one_malformed_provider_response():
    transport = ScriptedTransport([
        ProviderProtocolError("Chat tool arguments contain invalid JSON"),
        _summary_result("Keep the completed pages and continue the same task.").response,
    ])
    session = ModelSession(
        settings=_settings(max_attempts=2),
        channels=(ProviderChannel("fixture-secret", "a"),),
        transport=transport,
        sleeper=lambda _delay: pytest.fail("protocol regeneration must not back off"),
    )
    summary, metrics = _summarize(
        [{"role": "user", "content": "Keep completed page evidence."}],
        session.call,
    )
    assert "Keep the completed pages" in summary
    assert metrics["summary_calls"] == 1
    assert len(transport.calls) == 2
    assert all(request.request_kind == "context_compaction" for _, request in transport.calls)
    assert all(request.tools == () for _, request in transport.calls)


@pytest.mark.parametrize("result", [
    _summary_result(""), _summary_result("unfinished", stop_reason="max_tokens"),
    _summary_result("unfinished", stop_reason="length"),
    _summary_result("tool", tool_calls=({"type": "tool_use", "id": "not-executed", "name": "patch", "input": {}},)),
    SessionResult(None, "test", 1, failure=ProviderFailure("http_error", "fixture unavailable", status_code=503)),
])
def test_summary_failure_keeps_old_checkpoint_and_does_not_create_new_segment(result):
    checkpoints, events, requests = [], [], []
    original = SegmentCheckpoint.initial([{"role": "user", "content": "evidence " * 20000}])
    runtime = _runtime(ScriptedTransport([]), ToolRuntime(), event_sink=events.append,
        summary_invoker=lambda request, **kw: requests.append(request) or result)
    with pytest.raises(AgentInterruptedError):
        _run(runtime, ToolContext("slide", "slide"), checkpoint=original, checkpoint_sink=checkpoints.append)
    response = getattr(result, "response", None)
    stop = getattr(response, "stop_reason", "")
    has_text = any(str(block.get("text") or "").strip()
                   for block in getattr(response, "visible_blocks", ()) if block.get("type") == "text")
    expected_requests = 2 if response is not None and not response.tool_calls and (
        stop in {"max_tokens", "length"}
        or (stop in {"end_turn", "stop", "stop_sequence", "completed"} and not has_text)
    ) else 1
    assert checkpoints == [original] and len(requests) == expected_requests
    assert not any(event["event_type"] == "agent_context_compacted" for event in events)
    assert events[-2]["event_type"] == "agent_context_compaction_failed"
    assert events[-2]["recoverable"] is True
    assert not runtime.tools.events


def test_summary_interruption_keeps_original_owner_checkpoint():
    saved = []
    original = SegmentCheckpoint.initial([{"role": "user", "content": "evidence " * 20000}])
    def interrupt(request, **context):
        raise InterruptedError("owner_interrupted: summary request cancelled")
    runtime = _runtime(ScriptedTransport([]), ToolRuntime(), summary_invoker=interrupt)
    with pytest.raises(InterruptedError, match="owner_interrupted"):
        _run(runtime, ToolContext("slide", "slide"), checkpoint=original, checkpoint_sink=saved.append)
    assert saved == [original] and not runtime.tools.events


def _stub_compaction_tail(monkeypatch, tail):
    monkeypatch.setattr(agent_module, "context_compaction_summary", lambda *args, **kwargs: (
        "[compacted context for slide owner]\nKeep the current task.", {"summary_calls": 1}
    ))
    monkeypatch.setattr(agent_module, "visual_compaction_tail", lambda *args, **kwargs: (
        copy.deepcopy(tail), {"target_unmet_reason": ""}
    ))


def test_compaction_appends_continuation_after_closed_tool_history_and_counts_it_in_budget(monkeypatch):
    terminal = {"role": "assistant", "content": [{"type": "text", "text": "Prior step completed."}]}
    paired_tail = [
        {"role": "assistant", "content": [{
            "type": "tool_use", "id": "call-1", "name": "read_file", "input": {"path": "brief.md"},
        }]},
        {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "call-1", "content": "current evidence",
        }]},
        terminal,
    ]
    _stub_compaction_tail(monkeypatch, paired_tail)
    summary = {"role": "user", "content": "[compacted context for slide owner]\nKeep the current task."}
    language_metadata = {"working_language": "zh-CN", "presentation_language": "en-US"}
    continuation_text = (
        "Continue from the completed work above. Perform only the next necessary action, "
        "or briefly report completion."
        + "\n" + visible_output_language_reminder(language_metadata, trigger="context_compaction")
    )
    continuation = {"role": "user", "content": [{"type": "text", "text": continuation_text}]}
    without_continuation = estimate_request_tokens("fixture", [summary, *paired_tail], ())
    with_continuation = estimate_request_tokens("fixture", [summary, *paired_tail, continuation], ())
    settings = replace(
        _settings(max_attempts=1),
        context_window=with_continuation + 128 + 100,
        context_safety_buffer=100,
    )
    runtime = AgentRuntime(
        settings=settings,
        channels=(ProviderChannel("fixture-secret", "a"),),
        transport=ScriptedTransport([]),
        tools=ToolRuntime(),
        compact_target_tokens=without_continuation,
        compact_summary_target_tokens=1,
    )

    selected, metrics = runtime._compact_messages(
        [{"role": "user", "content": "old evidence " * 10000}],
        system="fixture", tools=(), max_tokens=128, agent_id="owner", role="slide",
        language_metadata=language_metadata,
    )

    assert tool_boundary(selected).is_closed
    assert selected[-2] == terminal
    assert selected[-1]["role"] == "user"
    assert selected[-1]["content"][0]["text"].startswith("Continue from the completed work above.")
    assert "working_language=zh-CN" in selected[-1]["content"][0]["text"]
    assert selected[1:3] == paired_tail[:2]
    assert metrics["tokens_after"] == estimate_request_tokens("fixture", selected, ())
    assert metrics["tokens_after"] > without_continuation
    assert metrics["tokens_after"] <= metrics["input_budget"]
    assert metrics["tokens_after"] == with_continuation
    assert metrics["input_budget"] - metrics["tokens_after"] <= 1
    assert not metrics["target_met"]


@pytest.mark.parametrize("overflow", ["tokens", "bytes"])
def test_compaction_continuation_cannot_cross_hard_request_capacity(monkeypatch, overflow):
    tail = [{"role": "assistant", "content": [{"type": "text", "text": "Prior step completed."}]}]
    _stub_compaction_tail(monkeypatch, tail)
    summary = {"role": "user", "content": "[compacted context for slide owner]\nKeep the current task."}
    before_continuation = [summary, *tail]
    settings = replace(_settings(max_attempts=1), context_window=20000, context_safety_buffer=100)
    if overflow == "tokens":
        settings = replace(settings, context_window=(
            estimate_request_tokens("fixture", before_continuation, ()) + 128 + 100
        ))
    else:
        byte_count = agent_module.vision_budget.request_bytes({
            "system": "fixture", "messages": before_continuation, "tools": (),
        })
        monkeypatch.setattr(agent_module.vision_budget, "MAX_REQUEST_BYTES", byte_count + 65536)
    runtime = AgentRuntime(settings=settings, channels=(ProviderChannel("fixture-secret", "a"),),
                           transport=ScriptedTransport([]), tools=ToolRuntime())
    original = [{"role": "user", "content": "old evidence " * 10000}]
    unchanged = copy.deepcopy(original)
    with pytest.raises(CompactionSummaryError, match="continuation exceeds request capacity"):
        runtime._compact_messages(original, system="fixture", tools=(), max_tokens=128,
                                  agent_id="owner", role="slide")
    assert original == unchanged


def test_compaction_keeps_terminal_user_message_unchanged(monkeypatch):
    terminal_user = {"role": "user", "content": [{"type": "text", "text": "Current evidence."}]}
    _stub_compaction_tail(monkeypatch, [terminal_user])
    runtime = _runtime(ScriptedTransport([]), ToolRuntime(), context_window=20000)

    selected, _ = runtime._compact_messages(
        [{"role": "user", "content": "old evidence " * 10000}],
        system="fixture", tools=(), max_tokens=128, agent_id="owner", role="slide",
    )

    assert selected[-1] == terminal_user


@pytest.mark.parametrize("tail", [
    [{"role": "assistant", "content": [{"type": "thinking", "thinking": "private"}]}],
    [{"role": "assistant", "content": [{
        "type": "tool_use", "id": "open-1", "name": "read_file", "input": {"path": "brief.md"},
    }]}],
])
def test_noncontinuable_compaction_tail_is_recoverable_and_retains_checkpoint(monkeypatch, tail):
    _stub_compaction_tail(monkeypatch, tail)
    checkpoints, events = [], []
    original = SegmentCheckpoint.initial([{"role": "user", "content": "old evidence " * 10000}])
    runtime = _runtime(ScriptedTransport([]), ToolRuntime(), context_window=20000, event_sink=events.append)

    with pytest.raises(AgentInterruptedError, match="context_compaction_recoverable"):
        _run(runtime, ToolContext("owner", "slide"), checkpoint=original, checkpoint_sink=checkpoints.append)

    assert checkpoints == [original]
    failure = [event for event in events if event["event_type"] == "agent_context_compaction_failed"]
    assert len(failure) == 1
    assert failure[0]["error_type"] == "CompactionSummaryError"
    assert failure[0]["checkpoint_retained"] is True and failure[0]["recoverable"] is True


def test_no_summary_invoker_or_summary_input_capacity_fails_explicitly():
    with pytest.raises(CompactionSummaryError, match="not configured"):
        _summarize([{"role": "user", "content": "task"}], None)
    with pytest.raises(CompactionSummaryError, match="no capacity"):
        _summarize([{"role": "user", "content": "task"}], lambda request: pytest.fail("must not send"),
                   context_window=40960, max_tokens=40960)


def test_recent_context_is_token_selected_without_splitting_tools_or_images():
    history = _history(8, keep=(0,))
    history.extend([
        {"role": "assistant", "content": [{"type": "tool_use", "id": "long-read", "name": "read_file", "input": {"path": "large.html"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "long-read", "content": "large-html " * 30000}]},
        {"role": "assistant", "content": "Next: preserve accepted P1 and repair P3."},
    ])
    before = copy.deepcopy(history)
    tail, metrics = visual_compaction_tail(history, target_tokens=22000, input_budget_tokens=30000)
    assert tool_boundary(tail).is_closed and history == before
    assert "large-html " * 30000 not in str(tail)
    assert "Next: preserve" in str(tail) and "hash-0" in str(tail)
    assert sum(1 for message in tail for _ in image_blocks(message.get("content"))) >= 2
    assert metrics["image_estimated_tokens"] > 0


def test_protected_images_may_exceed_target_but_not_capacity():
    history = _history(5, keep=tuple(range(5)))
    runtime = _runtime(ScriptedTransport([]), ToolRuntime(), context_window=80000,
        compact_target_tokens=16384, compact_summary_target_tokens=4096)
    history.insert(0, {"role": "user", "content": "older evidence " * 15000})
    selected, metrics = runtime._compact_messages(history, system="fixture", tools=(), max_tokens=128,
        agent_id="owner", role="slide")
    assert sum(1 for message in selected for _ in image_blocks(message.get("content"))) == 5
    assert not metrics["target_met"] and metrics["target_unmet_reason"] == "protected_inputs_exceed_target"
    assert metrics["tokens_after"] < metrics["input_budget"]

    saved = []
    runtime = _runtime(ScriptedTransport([]), ToolRuntime(), context_window=20000)
    original = SegmentCheckpoint.initial(history)
    with pytest.raises(AgentInterruptedError, match="protected context"):
        _run(runtime, ToolContext("owner", "slide"), checkpoint=original, checkpoint_sink=saved.append)
    assert saved == [original]


def test_pending_images_survive_summary_without_being_consumed():
    history = _history(1)
    history[0]["content"] = "long prior work " * 8000
    commits = []
    tools = ToolRuntime()
    tools.deferred_inspection_committer = lambda descriptor: commits.append(descriptor)
    runtime = _runtime(ScriptedTransport([]), tools, context_window=40000)
    selected, _ = runtime._compact_messages(history, system="fixture", tools=(), max_tokens=128,
        agent_id="material", role="material")
    assert sum(1 for message in selected for _ in image_blocks(message.get("content"))) == 1
    assert selected[-2:] == history[-2:] and tool_boundary(selected).is_closed and commits == []


def test_internal_summary_uses_existing_provider_raw_but_emits_no_creation_messages(tmp_path):
    from runtime.skill_runtime import SkillRuntime
    from runtime.config import RunMode
    from runtime.production import ProductionSettings
    from runtime.session import UsageLedger
    from runtime.raw_trace import capture
    host = SkillRuntime.__new__(SkillRuntime)
    class RecordingTransport(ScriptedTransport):
        def complete(self, channel, request, *, total_timeout_s):
            capture("request", {"system": request.system, "messages": request.messages,
                "max_tokens": request.max_tokens, "tools": request.tools}, {})
            result = super().complete(channel, request, total_timeout_s=total_timeout_s)
            capture("response", {"content": result.message.content, "stop_reason": result.message.stop_reason})
            return result
    transport = RecordingTransport([_summary_result("Real visible handoff").response])
    events = []
    settings = ProductionSettings(skill_source=tmp_path)
    host.prepared = SimpleNamespace(run_spec=SimpleNamespace(provider=_settings(), run_mode=RunMode.SYNTHESIS),
        settings=settings, environment={}, channels=(ProviderChannel("test", "a"),),
        transport=transport, usage=UsageLedger(), slots=SimpleNamespace(acquire=lambda *a, **k: nullcontext()))
    services = SimpleNamespace(workspace=SimpleNamespace(paths=SimpleNamespace(root=tmp_path)),
        live_trace=SimpleNamespace(record_runtime_event=events.append))
    summary, _ = _summarize([{"role": "user", "content": "Keep the blue layout"}],
        lambda request: host._invoke_compaction_summary(services, request, agent_id="slide-1",
            role="slide", segment_id="creation-a"))
    assert "Real visible handoff" in summary
    assert all(event["role"] == "harness" and event["request_kind"] == "context_compaction" for event in events)
    assert not any(event["event_type"] in {"agent_context", "assistant_message", "tool_use", "inspection_committed"} for event in events)
    assert any(event["event_type"] == "provider_attempt_succeeded" for event in events)
    raw_requests = list((tmp_path / "_trace/raw/provider").glob("*.request.json"))
    assert raw_requests
    assert all(json.loads(path.read_text())["request_kind"] == "context_compaction" for path in raw_requests)


def test_training_limit_does_not_expand_smaller_provider_window():
    runtime = _runtime(ScriptedTransport([]), ToolRuntime(), context_window=65536, context_window_limit=131072)
    assert runtime.context_window == 65536
    with pytest.raises(AgentContextWindowError, match="leave no room"):
        runtime._compact_messages([], system="fixture", tools=(), max_tokens=65536, agent_id="owner", role="slide")


def test_summary_raw_is_separate_and_only_actual_continuation_enters_creation_export(tmp_path):
    from export.training import TrainingExporter
    from geometry_fixture import geometry, write_geometry
    from qc.routing import IssueLedger
    from runtime.config import RunMode
    from runtime.production import ProductionSettings
    from runtime.raw_trace import capture
    from runtime.session import TransportResponse, UsageLedger
    from runtime.skill_runtime import SkillRuntime
    from state.workspace import WorkspaceState

    write_geometry(tmp_path, geometry(width_px=1600, height_px=900))
    workspace = WorkspaceState(tmp_path)
    journal = LiveEventJournal(workspace.paths.trace / "live-events.jsonl")
    exporter = TrainingExporter(workspace, SimpleNamespace(), IssueLedger(tmp_path), run_mode="synthesis")
    provider = replace(_settings(max_attempts=1), context_window=65536, context_safety_buffer=1024)
    requests = []
    class RecordingTransport:
        def complete(self, channel, request, *, total_timeout_s):
            requests.append(request)
            capture("request", {"system": request.system, "messages": request.messages, "tools": request.tools}, {})
            response = _summary_result("Preserve P2 layout." if request.request_kind == "context_compaction" else "Done.").response
            capture("response", {"content": response.content, "stop_reason": response.stop_reason})
            return TransportResponse(response, first_byte_s=.01)
    host = SkillRuntime.__new__(SkillRuntime)
    transport, channels, usage = RecordingTransport(), (ProviderChannel("test", "a"),), UsageLedger()
    host.prepared = SimpleNamespace(run_spec=SimpleNamespace(provider=provider, run_mode=RunMode.SYNTHESIS),
        settings=ProductionSettings(skill_source=tmp_path), environment={}, channels=channels,
        transport=transport, usage=usage, slots=SimpleNamespace(acquire=lambda *a, **k: nullcontext()))
    services = SimpleNamespace(workspace=workspace, live_trace=journal)
    runtime = AgentRuntime(settings=provider, channels=channels, transport=transport, tools=ToolRuntime(),
        usage=usage, event_sink=journal.record_runtime_event,
        summary_invoker=lambda request, **context: host._invoke_compaction_summary(services, request, **context))
    runtime.run(agent_id="slide-2", role="slide", system="creation rules", max_tokens=32768,
        max_turns=2, thinking=True, effort="xhigh",
        tool_context=ToolContext("slide-2", "slide", metadata={"workspace_root": str(tmp_path)}),
        initial_messages=({"role": "user", "content": "Preserve P2 layout. " + "old evidence " * 10000},),
        checkpoint_sink=lambda checkpoint: exporter.capture("slide-2", "slide", checkpoint))
    segments = exporter.load_segments()
    assert len(segments) == 2 and {segment.owner_id for segment in segments} == {"slide-2"}
    assert {segment.role for segment in segments} == {"slide"}
    events = list(iter_jsonl_events(workspace.paths.trace / "live-events.jsonl"))
    internal = [event for event in events if event["role"] == "harness"]
    assert any(event["event_type"] == "provider_raw_request" for event in internal)
    assert any(event["event_type"] == "provider_raw_response" for event in internal)
    exporter.export(segments, SimpleNamespace(status="complete", outputs={}, path=""))
    final = json.loads((workspace.paths.root / json.loads(exporter.rows_path.read_text().splitlines()[-1])["checkpoint_path"]).read_text())
    actual_request = requests[-1]
    assert actual_request.request_kind != "context_compaction"
    assert final["agent_context"][0]["messages"] == list(actual_request.messages)
    assert final["messages"][:-1] == list(actual_request.messages)
    assert "Preserve P2 layout." in final["messages"][0]["content"]
    assert "Create a working handoff" not in str(final)
    assert len(final["raw_provider_refs"]) == 2


def test_progress_rebase_forgets_omitted_reads_but_not_tool_failures():
    from runtime.agent import _ToolProgress
    from runtime.tools import ToolCall, ToolResult
    progress = _ToolProgress()
    call = ToolCall("read", "read_file", {"path": "deck.md"})
    result = ToolResult("read", "read_file", True, json.dumps({"path": "deck.md", "content": "plan"}))
    for _ in range(9):
        progress.observe(call, result)
    assert progress.check("orchestrator", [call]) is True
    progress.failure_key = ("patch", "patch_match_count", "anchor")
    progress.exact_count = 2
    progress.rebase_evidence([{"role": "user", "content": "Summary only; deck.md must be reopened."}])
    assert progress.exact_count == 2 and progress.failure_key[1] == "patch_match_count"
    assert progress.evidence_repeats == 0 and not progress.evidence_warned
    progress.observe(call, result)
    assert progress.evidence_repeats == 0


def test_progress_rebase_keeps_repeat_detection_for_retained_receipts():
    from runtime.agent import _ToolProgress
    from runtime.tools import ToolCall, ToolResult
    call = ToolCall("r", "read_file", {"path": "deck.md"})
    payload = json.dumps({"path": "deck.md", "content": "plan"})
    progress = _ToolProgress()
    result = ToolResult("r", "read_file", True, payload)
    for _ in range(9):
        progress.observe(call, result)
    assert progress.check("orchestrator", [call]) is True
    progress.rebase_evidence([
        {"role": "assistant", "content": [{"type": "tool_use", "id": "r", "name": "read_file", "input": call.arguments}]},
        {"role": "user", "content": [result.as_message_block()]},
    ])
    progress.observe(call, result)
    with pytest.raises(RepeatedToolFailureError):
        progress.check("orchestrator", [call])


def test_repeated_failure_after_compaction_is_closed_and_keeps_attempt_reason():
    tools, checkpoints = ToolRuntime(), []
    def fail(*_):
        raise ToolExecutionError("patch_anchor_missing", "Old anchor absent; reread slides/slide-3.html.")
    tools.register(_definition("patch", fail))
    transport = ScriptedTransport([
        _response(_text("Keep accepted page 1; repair page 3 only."), _tool("f1", "patch", {"path": "slides/slide-3.html"}), stop_reason="tool_use"),
        _response(_text("x" * 60000), _tool("f2", "patch", {"path": "slides/slide-3.html"}), stop_reason="tool_use"),
        _response(_tool("f3", "patch", {"path": "slides/slide-3.html"}), stop_reason="tool_use"),
    ])
    with pytest.raises(RepeatedToolFailureError):
        _run(_runtime(transport, tools), ToolContext("slide-3", "slide"),
            initial_messages=({"role": "user", "content": "Repair the citation, not the layout."},), checkpoint_sink=checkpoints.append)
    sent = str(transport.calls[-1][1].messages)
    assert "Old anchor absent" in sent and "patch_anchor_missing" in sent
    assert "Keep accepted page 1" in sent
    assert all(tool_boundary(checkpoint.messages).is_closed for checkpoint in checkpoints)
    assert len([event for event in tools.events if event["event_type"] == "tool_result"]) == 3
    tools.assert_closed()


@pytest.mark.parametrize("prior_owner", ["slide-3", "slide-1"])
def test_compacted_resume_rejects_journaled_ids_without_reexecuting_or_leaking_another_owner(tmp_path, prior_owner):
    journal = LiveEventJournal(tmp_path / "_trace/live-events.jsonl")
    tools = ToolRuntime(event_sink=journal.record_tool_event)
    tools.register(_definition("read_file", lambda *_: {"text": "prior-private-result"}))
    from runtime.tools import ToolCall
    tools.execute(ToolContext(prior_owner, "slide"), ToolCall("old-id", "read_file", {"path": "source.md"}))
    checkpoints = []
    transport = ScriptedTransport([ValueError("provider unavailable")])
    context = ToolContext("slide-3", "slide", metadata={"workspace_root": str(tmp_path)})
    with pytest.raises(AgentProviderError):
        _run(_runtime(transport, tools), context, initial_messages=({"role": "user", "content": "x" * 60000},), checkpoint_sink=checkpoints.append)
    saved = SegmentCheckpoint.from_dict(json.loads(json.dumps(checkpoints[-1].to_dict())))
    assert saved.reason == "context_compaction" and "old-id" not in str(saved.messages)
    fresh = ToolRuntime(event_sink=journal.record_tool_event)
    fresh.register(_definition("read_file", lambda *_: pytest.fail("journaled transaction must not rerun")))
    retry = ScriptedTransport([_response(_tool("old-id", "read_file", {"path": "source.md"}), stop_reason="tool_use")])
    with pytest.raises(AgentProtocolError, match="reused prior tool_use_id"):
        _run(_runtime(retry, fresh), context, checkpoint=saved)
    assert not fresh.events and "prior-private-result" not in str(retry.calls)
    events = list(iter_jsonl_events(journal.path))
    assert sum(event["event_type"] == "tool_use" for event in events) == 1
    assert sum(event["event_type"] == "tool_result" for event in events) == 1


def test_compaction_keeps_completed_ids_in_process_without_a_persistent_workspace():
    calls = []
    tools = ToolRuntime()
    tools.register(_definition("read_file", lambda *_: calls.append(1) or {"text": "x" * 60000}))
    transport = ScriptedTransport([
        _response(_tool("already-read", "read_file", {"path": "source.md"}), stop_reason="tool_use"),
        _response(_tool("already-read", "read_file", {"path": "source.md"}), stop_reason="tool_use"),
    ])
    with pytest.raises(AgentProtocolError, match="reused prior tool_use_id"):
        _run(_runtime(transport, tools), ToolContext("slide-3", "slide"), initial_messages=({"role": "user", "content": "Continue repair"},))
    assert calls == [1] and len(tools.events) == 2
    assert transport.calls[1][1].messages[0]["content"].startswith("[compacted context")


def test_unclosed_journal_id_is_rejected_and_partial_tail_does_not_invent_a_transaction(tmp_path):
    journal = LiveEventJournal(tmp_path / "_trace/live-events.jsonl")
    journal.record_tool_event({"event_type": "tool_use", "tool_use_id": "accepted-open", "tool_name": "read_file",
        "owner_id": "slide-3", "role": "slide", "arguments": {"path": "source.md"}})
    with journal.path.open("ab") as handle:
        handle.write(b'{"event_type":"tool_use","payload":{"tool_use_id":"partial"}}')
    tools = ToolRuntime()
    tools.register(_definition("read_file", lambda *_: pytest.fail("unknown transaction must not be replayed")))
    context = ToolContext("slide-3", "slide", metadata={"workspace_root": str(tmp_path)})
    from runtime.agent import _recorded_tool_use_ids
    assert _recorded_tool_use_ids(tools, context) == {"accepted-open"}
    transport = ScriptedTransport([_response(_tool("accepted-open", "read_file", {"path": "source.md"}), stop_reason="tool_use")])
    with pytest.raises(AgentProtocolError, match="reused prior tool_use_id"):
        _run(_runtime(transport, tools), context, checkpoint=SegmentCheckpoint.initial([{"role": "user", "content": "Resume"}]))
    assert not tools.events


def test_owner_save_does_not_compact_at_64_messages_or_drop_baselines(tmp_path):
    from owner.context import OwnerContextStore, OwnerPacket
    from owner.registry import OwnerRegistry
    from runtime.vision import image_blocks
    from test_owner_context import _run_spec
    spec = _run_spec(tmp_path)
    owner = OwnerRegistry(tmp_path, deck_id="retained-baseline").accept(role="slide", pages=[3], group_id="compare")
    packet = OwnerPacket.compile(owner, spec, goal="Compare retained baseline page 1 and repaired page 3")
    visual = _history(2, keep=(0,))
    history = [*visual[:3], *[{"role": "assistant", "content": f"Repair note {index}"} for index in range(70)], *visual[3:]]
    store = OwnerContextStore(tmp_path)
    original = SegmentCheckpoint.initial(history)
    store.save(owner=owner, packet=packet, segment=original, artifact_hashes={})
    first = store.load(owner.owner_id)
    assert first.segment == original and len(first.messages) > 64
    for _ in range(2):
        current = store.load(owner.owner_id)
        results = [block for message in current.messages if isinstance(message.get("content"), list)
                   for block in message["content"] if block.get("type") == "tool_result"]
        assert [block["tool_use_id"] for block in results] == ["0", "1"]
        assert len([image for message in current.messages for image in image_blocks(message.get("content"))]) == 2
        assert results[0] == visual[2]["content"][0] and results[1] == visual[4]["content"][0]
        assert tool_boundary(current.messages).is_closed
        store.save(owner=owner, packet=packet, segment=current.segment, artifact_hashes={})
    assert list(original.messages) == history


def test_text_pressure_keeps_all_images_that_still_fit():
    from runtime.vision import image_blocks
    history = _history(8)
    history.append({"role": "assistant", "content": "long execution note " * 6000})
    before = [block for message in history for block in image_blocks(message.get("content"))]
    transport = ScriptedTransport([_response(_text("done"))])
    result = _run(_runtime(transport, ToolRuntime(), context_window=80000), ToolContext("slide-3", "slide"), initial_messages=history)
    sent = transport.calls[0][1].messages
    assert [block for message in sent for block in image_blocks(message.get("content"))] == before
    assert len(result.segments) == 2 and tool_boundary(sent).is_closed


def test_large_text_request_bytes_do_not_evict_small_images(monkeypatch):
    from runtime import vision
    monkeypatch.setattr(vision, "MAX_REQUEST_BYTES", 200000)
    history = _history(6)
    history[0]["content"] = "text " * 100000
    transport = ScriptedTransport([_response(_text("done"))])
    result = _run(_runtime(transport, ToolRuntime(), context_window=1000000), ToolContext("slide-3", "slide"), initial_messages=history)
    sent = transport.calls[0][1].messages
    assert sum(1 for message in sent for _ in vision.image_blocks(message.get("content"))) == 6
    assert len(result.segments) == 2


def test_under_budget_keeps_original_image_prefix_without_reading_recovery_state():
    history = _history(6)
    transport = ScriptedTransport([_response(_text("done"))])

    def forbidden():
        pytest.fail("No per-turn task re-read when no compaction is needed")

    result = _run(_runtime(transport, ToolRuntime(), context_window=80000), ToolContext("slide-3", "slide"),
                  initial_messages=history, continuation_context=forbidden)
    assert list(transport.calls[0][1].messages) == history
    assert len(result.segments) == 1
