from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest

from runtime.agent import (
    AgentInterruptedError,
    AgentProtocolError,
    AgentProviderError,
    AgentRuntime,
    AgentTurnLimitError,
    RepeatedToolFailureError,
)
from runtime.config import ProviderSettings
from runtime.recovery import CompactionSummaryError, ProviderRequestTimeout, ResourceSlotTimeout, SegmentCheckpoint, tool_boundary
from runtime.session import ModelRequest, ProviderChannel, TransportResponse
from runtime.tools import (
    ToolContext,
    ToolDefinition,
    ToolExecutionError,
    ToolRuntime,
    WriteScope,
)


class ScriptedTransport:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, ModelRequest]] = []

    def complete(self, channel, request, *, total_timeout_s):
        self.calls.append((channel.channel_id, copy.deepcopy(request)))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return TransportResponse(outcome, first_byte_s=0.01)


def _summary_result(text="Continue the accepted task. Keep accepted page 1; repair page 3 only. Old anchor absent; patch_anchor_missing. Do not repeat the rejected layout.", *, stop_reason="end_turn", tool_calls=()):
    from runtime.session import NormalizedAssistantResponse, SessionResult
    blocks = (*([{"type": "text", "text": text}] if text else []), *tool_calls)
    return SessionResult(NormalizedAssistantResponse(content=blocks, analysis_summaries=(),
        stop_reason=stop_reason, usage={"input_tokens": 100, "output_tokens": 30}), "summary-fixture", 1)


def _summary_invoker(request, **context):
    assert request.request_kind == "context_compaction" and not request.tools
    return _summary_result()


def _settings(*, max_attempts: int = 2) -> ProviderSettings:
    return ProviderSettings(
        api_format="anthropic",
        model="model-test",
        base_url="https://provider.test",
        total_timeout_s=30,
        connect_timeout_s=5,
        idle_timeout_s=10,
        channel_cooldown_s=0,
        max_attempts=max_attempts,
        streaming=False,
        prompt_cache=False,
        prompt_cache_messages=False,
    )


def _response(*blocks, stop_reason: str = "end_turn", analysis_summaries=(), stop_details=None):
    # 归一响应契约:content 供协议/签名检测消费,analysis_summaries 是**已由
    # transport 决定可公开**的分析摘要(Anthropic summarized thinking 或 OpenAI
    # Responses reasoning summary)。AgentRuntime 只读 analysis_summaries、不看来源。
    return SimpleNamespace(
        content=list(blocks),
        stop_reason=stop_reason,
        stop_details=dict(stop_details or {}),
        analysis_summaries=tuple(analysis_summaries),
        usage=SimpleNamespace(
            input_tokens=3,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            output_tokens=2,
        ),
    )


def _text(value: str):
    return SimpleNamespace(type="text", text=value)


def _tool(tool_id: str, name: str, arguments: dict[str, object]):
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=arguments)


def _thinking(value: str, signature: str = "signed-private"):
    return SimpleNamespace(type="thinking", thinking=value, signature=signature)


def _definition(name: str, handler, *, role: str = "slide", required=()) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=name,
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": True,
            "required": list(required),
        },
        handler=handler,
        roles=frozenset({role}),
    )


def _run(
    transport: ScriptedTransport,
    tools: ToolRuntime,
    context: ToolContext,
    **overrides,
):
    runtime = AgentRuntime(
        settings=_settings(),
        channels=(ProviderChannel("secret-a", "ch-a"),),
        transport=transport,
        tools=tools,
        balance="primary-first",
    )
    values = {
        "agent_id": context.owner_id,
        "role": context.role,
        "system": "正式角色卡",
        "tool_context": context,
        "initial_messages": ({"role": "user", "content": "开始"},),
        "max_turns": 8,
        "max_tokens": 128,
        "thinking": False,
        "effort": "medium",
    }
    values.update(overrides)
    return runtime.run(**values)


def test_duplicate_read_history_is_short_but_full_tool_trace_is_preserved():
    payload = {"path": "knowledge.md", "offset": 1, "mode": "lines", "content": "Evidence.\n" * 1200}
    tools = ToolRuntime()
    tools.register(_definition("read_file", lambda *_: payload))
    transport = ScriptedTransport([
        _response(_tool("read-first", "read_file", {"path": "knowledge.md"})),
        _response(_tool("read-repeat", "read_file", {"path": "knowledge.md"})),
        _response(_text("Done")),
    ])
    result = _run(transport, tools, ToolContext("owner-slide", "slide"))
    history = [block for message in result.checkpoint.messages
               if isinstance(message.get("content"), list) for block in message["content"]
               if block.get("type") == "tool_result"]
    assert json.loads(history[0]["content"])["content"] == payload["content"]
    assert "content" not in json.loads(history[1]["content"])
    assert json.loads(history[1]["content"])["content_status"] == "unchanged_excerpt_already_in_context"
    traced = [event for event in tools.events if event.get("event_type") == "tool_result"]
    assert all(json.loads(event["content"])["content"] == payload["content"] for event in traced)
    assert len(traced) == 2
    assert tool_boundary(result.checkpoint.messages).is_closed


def test_text_response_finishes_with_a_closed_checkpoint() -> None:
    transport = ScriptedTransport([_response(_text("完成"))])
    tools = ToolRuntime()
    checkpoints: list[SegmentCheckpoint] = []

    result = _run(
        transport,
        tools,
        ToolContext("owner-slide", "slide"),
        checkpoint_sink=checkpoints.append,
    )

    assert result.final_text == "完成"
    assert result.turns == 1 and result.tool_calls == 0
    assert [message["role"] for message in result.checkpoint.messages] == [
        "user",
        "assistant",
    ]
    assert len(result.segments) == 1
    assert checkpoints[-1] == result.checkpoint
    assert checkpoints[0].messages == ({"role": "user", "content": "开始"},)
    assert checkpoints[0].segment_id == result.checkpoint.segment_id
    assert tool_boundary(result.checkpoint.messages).is_closed


def test_idle_event_continues_same_owner_without_synthetic_tool_transaction():
    transport = ScriptedTransport([_response(_text("等待后台任务")), _response(_text("已完成"))])
    tools, checkpoints = ToolRuntime(), []
    event = {"role": "user", "content": '{"harness_event":"orchestration_update","all_terminal":true}'}
    events = iter([event, None])
    result = _run(transport, tools, ToolContext("orchestrator", "orchestrator"),
                  idle_handler=lambda: next(events), checkpoint_sink=checkpoints.append)
    assert result.turns == 2 and result.tool_calls == 0
    assert tools.events == () or not tools.events
    assert transport.calls[1][1].messages[-1] == event
    assert result.agent_id == "orchestrator"
    assert all(tool_boundary(checkpoint.messages).is_closed for checkpoint in checkpoints)
    assert len({checkpoint.segment_id for checkpoint in checkpoints}) == 1


def test_idle_interruption_preserves_closed_checkpoint_and_does_not_call_provider_again():
    transport = ScriptedTransport([_response(_text("暂时没有其他工作"))])
    checkpoints = []
    def interrupted():
        raise ResourceSlotTimeout("owner resource unavailable")
    with pytest.raises(ResourceSlotTimeout):
        _run(transport, ToolRuntime(), ToolContext("orchestrator", "orchestrator"),
             idle_handler=interrupted, checkpoint_sink=checkpoints.append)
    assert len(transport.calls) == 1 and checkpoints
    assert tool_boundary(checkpoints[-1].messages).is_closed


def test_identical_terminal_owner_failure_pauses_after_additional_recovery_decisions():
    transport = ScriptedTransport([
        _response(_text("等待后台任务")),
        _response(_text("该失败仍未解决")),
        _response(_text("继续针对失败修复")),
        _response(_text("仍未解决")),
        _response(_text("must not spin")),
    ])
    checkpoints, events = [], []
    update = {"role": "user", "content": json.dumps({
        "harness_event": "orchestration_update",
        "all_terminal": True,
        "events": [],
        "pending_requests": [],
        "unresolved_failures": [{"task_id": "image-failed", "error": "missing asset"}],
    })}
    runtime = AgentRuntime(
        settings=_settings(),
        channels=(ProviderChannel("secret-a", "ch-a"),),
        transport=transport,
        tools=ToolRuntime(),
        event_sink=events.append,
    )

    with pytest.raises(AgentInterruptedError, match="identical terminal Owner failure"):
        runtime.run(
            agent_id="orchestrator",
            role="orchestrator",
            system="正式角色卡",
            tool_context=ToolContext("orchestrator", "orchestrator"),
            initial_messages=({"role": "user", "content": "开始"},),
            max_turns=20,
            max_tokens=128,
            thinking=False,
            effort="medium",
            idle_handler=lambda: copy.deepcopy(update),
            checkpoint_sink=checkpoints.append,
        )

    assert len(transport.calls) == 4
    assert checkpoints and tool_boundary(checkpoints[-1].messages).is_closed
    stalled = [event for event in events if event["event_type"] == "agent_orchestration_stalled"]
    assert len(stalled) == 1 and stalled[0]["repeat_count"] == 4


def test_identical_completion_gap_pauses_without_being_hidden_by_new_recovery_task_ids():
    transport = ScriptedTransport([
        _response(_text("检查收尾缺口")),
        _response(_text("缺口仍无法关闭")),
        _response(_text("继续按缺口修复")),
        _response(_text("缺口仍在")),
        _response(_text("must not spin")),
    ])
    checkpoints, emitted = [], []
    calls = 0

    def completion_gap():
        nonlocal calls
        calls += 1
        return {"role": "user", "content": json.dumps({
            "harness_event": "completion_gap",
            "all_terminal": True,
            "events": [],
            "pending_requests": [],
            "error_code": "verification_pages_not_inspected",
            "detail": "P2 current inspection evidence is missing",
            "diagnostics": {
                "page_issues": [{
                    "page": 2,
                    "lifecycle": "rendered",
                    "reasons": [{"code": "never_inspected", "detail": "P2"}],
                }],
                "recovery": [{
                    "action": "delegate_submit",
                    "submit_task": {"task_id": f"completion-recheck-{calls}"},
                }],
            },
            "blocking_issues": [],
        })}

    runtime = AgentRuntime(
        settings=_settings(),
        channels=(ProviderChannel("secret-a", "ch-a"),),
        transport=transport,
        tools=ToolRuntime(),
        event_sink=emitted.append,
    )

    with pytest.raises(AgentInterruptedError, match="identical completion gap"):
        runtime.run(
            agent_id="orchestrator",
            role="orchestrator",
            system="正式角色卡",
            tool_context=ToolContext("orchestrator", "orchestrator"),
            initial_messages=({"role": "user", "content": "开始"},),
            max_turns=20,
            max_tokens=128,
            thinking=False,
            effort="medium",
            idle_handler=completion_gap,
            checkpoint_sink=checkpoints.append,
        )

    assert calls == 4
    assert len(transport.calls) == 4
    assert checkpoints and tool_boundary(checkpoints[-1].messages).is_closed
    stalled = [event for event in emitted if event["event_type"] == "agent_orchestration_stalled"]
    assert len(stalled) == 1
    assert stalled[0]["recovery_action"] == "artifact_first_completion_recovery"


def test_provider_failover_does_not_reexecute_completed_command_or_duplicate_output(tmp_path):
    import os
    import sys
    from runtime.diagnostics import run_command
    from runtime.live_trace import LiveEventJournal, read_live_events
    journal = LiveEventJournal(tmp_path / "_trace/live-events.jsonl")
    tools = ToolRuntime(event_sink=journal.record_tool_event)
    executions = []
    def command(_context, _arguments):
        executions.append(1)
        result = run_command([sys.executable, "-c", "print('durable before failover')"], cwd=tmp_path, timeout_s=5, environment=os.environ)
        return {"stdout": result.stdout, "diagnostics": result.diagnostics}
    tools.register(_definition("terminal", command))
    transport = ScriptedTransport([
        _response(_tool("before-failover", "terminal", {}), stop_reason="tool_use"),
        ProviderRequestTimeout("stream_idle", 1),
        _response(_text("recovered")),
    ])
    agent = AgentRuntime(settings=_settings(), channels=(ProviderChannel("secret-a", "ch-a"), ProviderChannel("secret-b", "ch-b")), transport=transport, tools=tools, event_sink=journal.record_runtime_event)
    result = agent.run(agent_id="slide-owner", role="slide", system="fixture", tool_context=ToolContext("slide-owner", "slide", metadata={"workspace_root": str(tmp_path), "task_id": "slide-task", "attempt": 1}), initial_messages=({"role": "user", "content": "fixture"},), max_turns=3, max_tokens=128, thinking=False, effort="low")
    rows = read_live_events(journal.path)["events"]
    assert result.final_text == "recovered" and executions == [1]
    assert any(row["event_type"] == "provider_failover" for row in rows)
    assert sum(row["event_type"] == "tool_use" for row in rows) == 1
    assert sum(row["event_type"] == "tool_result" for row in rows) == 1
    output = next(row for row in rows if row["event_type"] == "tool_output")
    path = tmp_path / output["payload"]["diagnostic_ref"]
    assert "durable before failover" in path.read_text()
    saved = path.read_bytes()
    reopened = LiveEventJournal(journal.path)
    assert reopened.append(**{key: output[key] for key in ("event_type", "agent_id", "role", "turn", "segment_id", "payload", "event_id")}) is None
    assert path.read_bytes() == saved
    assert [row["sequence"] for row in rows] == list(range(1, len(rows) + 1))
    assert len({row["event_id"] for row in rows}) == len(rows)
    assert len(result.segments) == 1
    assert tool_boundary(result.checkpoint.messages).is_closed


def test_agent_emits_public_analysis_summaries_provider_agnostically() -> None:
    # AgentRuntime 只判归一响应是否带公开摘要,不关心它来自 Anthropic 还是 OpenAI。
    # 带 analysis_summaries 的响应(无论 api_format)都要发 assistant_analysis_summary;
    # 且 content 里的签名思考绝不进公开事件。
    events = []
    runtime = AgentRuntime(
        settings=_settings(),
        channels=(ProviderChannel("secret-a", "ch-a"),),
        transport=ScriptedTransport(
            [
                _response(
                    _thinking("可展示的短分析"),
                    _text("完成"),
                    analysis_summaries=("可展示的短分析",),
                )
            ]
        ),
        tools=ToolRuntime(),
        event_sink=events.append,
    )
    runtime.run(
        agent_id="owner-slide",
        role="slide",
        system="正式角色卡",
        tool_context=ToolContext("owner-slide", "slide"),
        initial_messages=({"role": "user", "content": "开始"},),
        max_turns=2,
        max_tokens=128,
        thinking=True,
        effort="medium",
    )
    summaries = [
        event for event in events
        if event["event_type"] == "assistant_analysis_summary"
    ]
    assert [event["summary"] for event in summaries] == ["可展示的短分析"]
    # 签名(私有)绝不出现在任何公开事件里。
    assert "signed-private" not in repr(events)


def test_agent_emits_no_summary_when_response_has_no_public_summary() -> None:
    # 自托管 Chat 兼容端点没有可公开摘要(analysis_summaries 为空);即便 content 里
    # 带 raw reasoning 的 thinking 分块,也绝不发分析事件、绝不泄露 raw reasoning/签名。
    events = []
    runtime = AgentRuntime(
        settings=replace(_settings(), api_format="openai_chat_compatible"),
        channels=(ProviderChannel("secret-a", "ch-a"),),
        transport=ScriptedTransport(
            [
                _response(
                    _thinking("不可作为公开摘要"),
                    _text("完成"),
                    analysis_summaries=(),
                )
            ]
        ),
        tools=ToolRuntime(),
        event_sink=events.append,
    )
    runtime.run(
        agent_id="owner-slide",
        role="slide",
        system="正式角色卡",
        tool_context=ToolContext("owner-slide", "slide"),
        initial_messages=({"role": "user", "content": "开始"},),
        max_turns=2,
        max_tokens=128,
        thinking=True,
        effort="medium",
    )
    assert not any(
        event["event_type"] == "assistant_analysis_summary" for event in events
    )
    assert "不可作为公开摘要" not in repr(events)
    assert "signed-private" not in repr(events)


def test_multiple_tools_are_executed_and_returned_in_one_user_message() -> None:
    seen: list[str] = []
    tools = ToolRuntime()
    tools.register(_definition("first", lambda _context, _arguments: seen.append("first") or {"n": 1}))
    tools.register(_definition("second", lambda _context, _arguments: seen.append("second") or {"n": 2}))
    transport = ScriptedTransport(
        [
            _response(
                _text("执行"),
                _tool("tool-1", "first", {}),
                _tool("tool-2", "second", {}),
                stop_reason="tool_use",
            ),
            _response(_text("已闭合")),
        ]
    )

    result = _run(transport, tools, ToolContext("owner-slide", "slide"))

    assert seen == ["first", "second"]
    assert result.tool_calls == 2 and result.turns == 2
    second_request = transport.calls[1][1]
    result_message = second_request.messages[-1]
    assert result_message["role"] == "user"
    assert [block["tool_use_id"] for block in result_message["content"]] == [
        "tool-1",
        "tool-2",
    ]
    assert all(block.get("is_error") is None for block in result_message["content"])


def test_tool_failure_is_closed_and_visible_to_the_next_model_turn() -> None:
    def fail(_context, _arguments):
        raise ToolExecutionError("known_failure", "可恢复失败")

    tools = ToolRuntime()
    tools.register(_definition("fragile", fail))
    transport = ScriptedTransport(
        [
            _response(
                _tool("tool-fail", "fragile", {}), stop_reason="tool_use"
            ),
            _response(_text("已根据错误收口")),
        ]
    )

    result = _run(transport, tools, ToolContext("owner-slide", "slide"))

    block = transport.calls[1][1].messages[-1]["content"][0]
    assert block["tool_use_id"] == "tool-fail"
    assert block["is_error"] is True
    assert block["content"] == "可恢复失败"
    assert result.final_text == "已根据错误收口"


def test_vision_tool_returns_the_real_image_to_the_same_session(
    tmp_path: Path,
) -> None:
    render = tmp_path / "renders" / "slide_01.png"
    render.parent.mkdir(parents=True)
    Image.new("RGB", (32, 18), (18, 42, 76)).save(render)
    tools = ToolRuntime()
    tools.register(
        ToolDefinition(
            name="vision_analyze",
            description="vision",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "question": {"type": "string"},
                    "scope": {"type": "string"},
                },
                "required": ["path", "question", "scope"],
                "additionalProperties": False,
            },
            handler=lambda _context, _arguments: {
                "inspection_id": "inspection-1",
                "native_multimodal": True,
            },
            roles=frozenset({"slide"}),
        )
    )
    transport = ScriptedTransport(
        [
            _response(
                _tool(
                    "vision-1",
                    "vision_analyze",
                    {
                        "path": "renders/slide_01.png",
                        "question": "检查当前像素",
                        "scope": "page",
                    },
                ),
                stop_reason="tool_use",
            ),
            _response(_text("图片已检查")),
        ]
    )
    context = ToolContext(
        "owner-slide",
        "slide",
        metadata={"workspace_root": str(tmp_path)},
    )

    result = _run(transport, tools, context)

    block = transport.calls[1][1].messages[-1]["content"][0]
    assert block["type"] == "tool_result"
    assert [part["type"] for part in block["content"]] == ["text", "image"]
    assert block["content"][1]["source"]["media_type"] == "image/png"
    assert block["content"][1]["source"]["data"]
    assert result.vision_paths == ("renders/slide_01.png",)


def test_mineru_read_file_attaches_related_image_once_without_inspect(tmp_path: Path) -> None:
    image = tmp_path / "inputs" / "extracted" / "att-123456789abc" / "images" / "chart.png"
    image.parent.mkdir(parents=True)
    Image.new("RGB", (40, 24), "teal").save(image)
    relative = image.relative_to(tmp_path).as_posix()
    tools = ToolRuntime()
    tools.register(_definition("read_file", lambda *_: {
        "path": "document.md",
        "content": "Figure 2 shows the benchmark.",
        "_multimodal_images": [{"path": relative, "page": 3}],
    }, role="research"))
    transport = ScriptedTransport([
        _response(_tool("read-1", "read_file", {"path": "document.md"}), stop_reason="tool_use"),
        _response(_tool("read-2", "read_file", {"path": "document.md"}), stop_reason="tool_use"),
        _response(_text("done")),
    ])
    context = ToolContext(
        "research-owner",
        "research",
        read_scopes=(WriteScope("inputs/extracted", recursive=True),),
        metadata={"workspace_root": str(tmp_path)},
    )

    result = _run(transport, tools, context)

    first = transport.calls[1][1].messages[-1]["content"][0]["content"]
    assert [part["type"] for part in first] == ["text", "image"]
    first_payload = json.loads(first[0]["text"])
    assert "_multimodal_images" not in first_payload
    assert first_payload["attached_images"] == [{"path": relative, "page": 3, "status": "attached"}]
    second = transport.calls[2][1].messages[-1]["content"][0]["content"]
    assert isinstance(second, str)
    assert json.loads(second)["attached_images"][0]["status"] == "already_in_context"
    assert result.vision_paths == (relative,)


def test_image_confirmation_context_only_includes_successfully_consumed_images(tmp_path):
    from state.workspace import file_sha256
    path = tmp_path / "hero.png"
    Image.new("RGB", (32, 18), "navy").save(path)
    tools, seen = ToolRuntime(), []
    tools.register(_definition("vision_analyze", lambda *_: {"native_multimodal": True}, role="image"))
    tools.register(_definition("terminal", lambda context, _: seen.append(context.metadata["consumed_visual_sources"]) or {}, role="image"))
    transport = ScriptedTransport([
        _response(_tool("v1", "vision_analyze", {"path": "hero.png"}), _tool("r1", "terminal", {}), stop_reason="tool_use"),
        _response(_tool("r2", "terminal", {}), stop_reason="tool_use"), _response(_text("done"))])
    _run(transport, tools, ToolContext("image-owner", "image", metadata={"workspace_root": str(tmp_path)}))
    assert seen == [[], [{"owner_id": "image-owner", "path": "hero.png", "sha256": file_sha256(path)}]]
    tools.assert_closed()


def test_attached_source_filter_does_not_turn_historical_open_ledger_into_pixels():
    import json
    from runtime.vision import opened_visual_sources
    source = {"owner_id": "image-owner", "path": "hero.png", "sha256": "current"}
    old = {**source, "path": "old.png"}
    payload = {"visual_source": source, "opened_visual_sources": [old, source]}
    parts = [{"type": "text", "text": json.dumps(payload)}]
    messages = [{"role": "user", "content": [{"type": "tool_result", "content": parts}]}]
    assert opened_visual_sources(messages, attached_only=True) == []
    parts.append({"type": "image", "source": {"type": "base64", "data": "fixture"}})
    assert opened_visual_sources(messages, attached_only=True) == [source]
    assert opened_visual_sources(messages, owner_id="other", attached_only=True) == []


def test_visual_history_releases_consumed_pixels_and_survives_resume(tmp_path):
    png = tmp_path / "page.png"
    Image.new("RGB", (24, 16), "blue").save(png)
    tools = ToolRuntime()
    tools.register(_definition("vision_analyze", lambda *_: {"native_multimodal": True}, role="material"))
    transport = ScriptedTransport([
        _response(_tool("v1", "vision_analyze", {"path": "page.png"}), stop_reason="tool_use"),
        _response(_text("First page observation"), _tool("v2", "vision_analyze", {"path": "page.png"}), stop_reason="tool_use"),
        _response(_text("Second page observation")),
    ])
    result = _run(transport, tools, ToolContext("material", "material", metadata={"workspace_root": str(tmp_path)}))
    messages = transport.calls[2][1].messages
    old = messages[2]["content"][0]
    pending = messages[-1]["content"][0]
    assert old["tool_use_id"] == "v1"
    assert not any(part["type"] == "image" for part in old["content"])
    assert "pixels_delivered_in_previous_model_turn" in str(old["content"])
    assert "missing pixels" in str(old["content"])
    assert transport.calls[1][1].messages[2]["content"][0]["content"][-1]["type"] == "image"
    assert pending["tool_use_id"] == "v2" and pending["content"][-1]["type"] == "image"
    assert "First page observation" in str(messages)
    assert len(result.segments) == 2
    assert result.segments[1].reason == "visual_context_release"
    resumed = copy.deepcopy(list(messages))
    runtime = AgentRuntime(settings=_settings(), channels=(ProviderChannel("test", "a"),),
        transport=ScriptedTransport([]), tools=ToolRuntime())
    assert runtime._compact_messages(resumed, system="fixture", tools=(), max_tokens=128,
        agent_id="material", role="material") is None
    assert resumed == list(messages)
    assert tool_boundary(resumed).is_closed


def test_signed_failure_rolls_over_from_the_last_closed_boundary(monkeypatch) -> None:
    import runtime.agent as agent_module
    clock, deadlines, budgets = [0.0], [], []
    original_session = agent_module.ModelSession

    class ClockedSession(original_session):
        def __init__(self, **kwargs):
            super().__init__(**kwargs, monotonic=lambda: clock[0])

        def call(self, request, **kwargs):
            deadlines.append(kwargs["deadline_monotonic"])
            return super().call(request, **kwargs)

    class ClockedTransport(ScriptedTransport):
        def complete(self, channel, request, *, total_timeout_s):
            budgets.append(total_timeout_s)
            if len(budgets) == 1:
                clock[0] += 7
            return super().complete(channel, request, total_timeout_s=total_timeout_s)

    monkeypatch.setattr(agent_module, "ModelSession", ClockedSession)
    signed = SegmentCheckpoint(
        segment_id="segment-signed",
        predecessor_segment_id=None,
        messages=(
            {"role": "user", "content": "继续"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "",
                        "signature": "provider-a",
                    },
                    {"type": "text", "text": "上次闭合"},
                ],
            },
        ),
        provider_affinity_id="ch-a",
    )
    transport = ClockedTransport(
        [ProviderRequestTimeout("first_byte", 2), _response(_text("恢复完成"))]
    )
    tools = ToolRuntime()
    runtime = AgentRuntime(
        settings=_settings(max_attempts=2),
        channels=(
            ProviderChannel("secret-a", "ch-a"),
            ProviderChannel("secret-b", "ch-b"),
        ),
        transport=transport,
        tools=tools,
        balance="primary-first",
    )

    result = runtime.run(
        agent_id="owner-slide",
        role="slide",
        system="正式角色卡",
        tool_context=ToolContext("owner-slide", "slide"),
        checkpoint=signed,
        max_turns=4,
        max_tokens=128,
        thinking=False,
        effort="medium",
    )

    assert [channel for channel, _request in transport.calls] == ["ch-a", "ch-b"]
    assert len(result.segments) == 2
    assert result.segments[1].predecessor_segment_id == "segment-signed"
    assert "signature" not in repr(result.segments[1].messages)
    assert deadlines == [30.0, 30.0]
    assert budgets == [30.0, 23.0]


def test_nonrecoverable_provider_failure_is_explicit() -> None:
    transport = ScriptedTransport([ValueError("bad provider payload")])
    with pytest.raises(AgentProviderError, match="provider_error"):
        _run(transport, ToolRuntime(), ToolContext("owner-slide", "slide"))


def test_provider_refusal_never_becomes_an_agent_empty_retry() -> None:
    events, checkpoints = [], []
    transport = ScriptedTransport([
        _response(
            stop_reason="refusal",
            stop_details={"type": "refusal", "category": "reasoning_extraction"},
        ),
        _response(_text("must not retry")),
    ])
    runtime = AgentRuntime(
        settings=_settings(max_attempts=2),
        channels=(ProviderChannel("secret-a", "ch-a"),),
        transport=transport,
        tools=ToolRuntime(),
        event_sink=events.append,
    )

    with pytest.raises(AgentProviderError) as excinfo:
        runtime.run(
            agent_id="owner-slide",
            role="slide",
            system="正式角色卡",
            tool_context=ToolContext("owner-slide", "slide"),
            initial_messages=({"role": "user", "content": "开始"},),
            max_turns=4,
            max_tokens=128,
            thinking=True,
            effort="medium",
            checkpoint_sink=checkpoints.append,
        )

    assert excinfo.value.failure is not None
    assert excinfo.value.failure.kind == "provider_policy_refusal"
    assert len(transport.calls) == 1
    assert checkpoints and tool_boundary(checkpoints[-1].messages).is_closed
    assert not [event for event in events if event["event_type"] in {
        "agent_empty_response_recovered", "agent_no_action_response"
    }]


@pytest.mark.parametrize("after_tool", [False, True])
def test_resource_queue_timeout_preserves_closed_checkpoint_and_local_identity(after_tool):
    events, checkpoints, executed = [], [], []
    tools = ToolRuntime()
    tools.register(_definition("work", lambda *_: executed.append("once") or {"ok": True}))
    transport = ScriptedTransport([
        _response(_tool("work-1", "work", {}), stop_reason="tool_use")
    ] if after_tool else [])
    acquired = 0

    @contextmanager
    def slot():
        nonlocal acquired
        acquired += 1
        if acquired > int(after_tool):
            raise ResourceSlotTimeout("local capacity unavailable")
        yield

    runtime = AgentRuntime(settings=_settings(), channels=(ProviderChannel("test-key", "ch-a"),),
                           transport=transport, tools=tools, event_sink=events.append, slot_factory=slot)
    arguments = dict(agent_id="owner-slide", role="slide", system="正式角色卡",
                     tool_context=ToolContext("owner-slide", "slide"), max_turns=3,
                     max_tokens=128, thinking=False, effort="medium")
    with pytest.raises(ResourceSlotTimeout, match="resource_queue_timeout"):
        runtime.run(**arguments, initial_messages=({"role": "user", "content": "开始"},),
                    checkpoint_sink=checkpoints.append)
    assert checkpoints and tool_boundary(checkpoints[-1].messages).is_closed
    assert len(transport.calls) == int(after_tool)
    assert events[-1]["error_type"] == "ResourceSlotTimeout"
    assert events[-1]["turn"] == int(after_tool)
    assert not any(event["event_type"] == "provider_failover" for event in events)
    resumed = ScriptedTransport([_response(_text("完成"))])
    runtime = AgentRuntime(settings=_settings(), channels=(ProviderChannel("test-key", "ch-a"),),
                           transport=resumed, tools=tools, event_sink=events.append)
    result = runtime.run(**arguments, checkpoint=checkpoints[-1])
    assert result.final_text == "完成"
    assert executed == (["once"] if after_tool else [])
    assert resumed.calls[0][1].messages == checkpoints[-1].messages
    assert [event["turn"] for event in events if event["event_type"] == "agent_turn_completed"] == list(
        range(1, int(after_tool) + 2))


def test_owner_queue_collect_closes_all_tool_results_before_pausing():
    events, checkpoints, executed = [], [], []
    tools = ToolRuntime()
    def collect(*_):
        raise ToolExecutionError("resource_queue_timeout", "Owner is waiting for local capacity")
    tools.register(_definition("collect", collect, role="orchestrator"))
    tools.register(_definition("work", lambda *_: executed.append("once") or {}, role="orchestrator"))
    transport = ScriptedTransport([_response(_tool("collect-1", "collect", {}),
        _tool("work-1", "work", {}), stop_reason="tool_use"), _response(_text("must not request"))])
    runtime = AgentRuntime(settings=_settings(), channels=(ProviderChannel("test", "ch-a"),),
        transport=transport, tools=tools, event_sink=events.append)
    with pytest.raises(ResourceSlotTimeout):
        runtime.run(agent_id="orchestrator", role="orchestrator", system="test",
            tool_context=ToolContext("orchestrator", "orchestrator"),
            initial_messages=({"role": "user", "content": "continue"},), max_turns=3,
            max_tokens=128, thinking=False, effort="medium", checkpoint_sink=checkpoints.append)
    assert len(transport.calls) == 1 and executed == ["once"]
    assert tool_boundary(checkpoints[-1].messages).is_closed
    results = checkpoints[-1].messages[-1]["content"]
    assert [block["tool_use_id"] for block in results] == ["collect-1", "work-1"]
    assert results[0]["error_code"] == "resource_queue_timeout"
    assert events[-1]["error_type"] == "ResourceSlotTimeout"


def test_delegate_wait_adapter_preserves_local_queue_error(tmp_path, monkeypatch):
    from infer import PresenterKernel
    from runtime.config import RunSpec
    from runtime.tools import ToolCall
    kernel = PresenterKernel(RunSpec.from_inputs(raw_user_query="queue", output_workspace=tmp_path))
    def collect(*_args, **_kwargs):
        raise ResourceSlotTimeout("Owner model queue expired")
    monkeypatch.setattr(kernel.orchestration, "wait", collect)
    try:
        result = kernel.tools.execute(kernel.services.orchestrator_tool_context(),
                                      ToolCall("wait-1", "delegate_wait", {}))
        assert not result.ok and result.error_code == "resource_queue_timeout"
        kernel.tools.assert_closed()
    finally:
        kernel.orchestration.close(wait=True)


def test_delegate_wait_prompt_payload_is_compacted_but_keeps_task_identity():
    from infer import _compact_delegate_wait_payload
    long_text = "start " + ("x" * 6000) + " end"
    payload = {
        "sequence": 12,
        "tasks": [{
            "task_id": "task-1",
            "owner_id": "slide-owner-1",
            "role": "slide",
            "pages": [3],
            "page_uids": ["slide_03"],
            "base_deck_version": "deck-v7",
            "goal": "repair slide 3 " + ("g" * 3000),
            "result": {"final_text": long_text, "notes": [long_text for _ in range(20)]},
            "debug_history": long_text,
        }],
        "events": [{"kind": "completed", "details": long_text, "items": list(range(30))}],
        "pending_requests": [{"prompt": long_text}],
        "unresolved_failures": [],
    }
    compact = _compact_delegate_wait_payload(payload)
    task = compact["tasks"][0]
    assert compact["history_compacted"] is True
    assert "full_state_ref" not in compact
    assert task["page_uids"] == ["slide_03"] and task["base_deck_version"] == "deck-v7"
    assert "debug_history" not in task
    assert "omitted" in task["goal"] and "omitted" in task["result"]["summary"]
    assert "notes" not in task["result"]
    assert compact["events"][0]["items"][-1]["omitted_items"] == 18
    assert "_state/orchestration.json" not in json.dumps(compact)
    assert payload["tasks"][0]["result"]["final_text"] == long_text


def test_delegate_wait_hides_persisted_state_and_trace_references():
    from infer import _compact_delegate_wait_payload

    compact = _compact_delegate_wait_payload({
        "events": [{
            "sequence": 7,
            "type": "attachment_parse_finished",
            "attachment_id": "att-012345abcdef",
            "status": "completed",
            "manifest_path": "_trace/attachment-manifest.json",
            "state_path": "_state/private.json",
            "trace_path": "_trace/live-events.jsonl",
            "full_output_ref": "_trace/tool-output/private.jsonl",
        }],
    })

    event = compact["events"][0]
    assert event["attachment_id"] == "att-012345abcdef"
    assert event["status"] == "completed"
    assert not {"manifest_path", "state_path", "trace_path", "full_output_ref"} & event.keys()
    assert "_state/" not in json.dumps(compact) and "_trace/" not in json.dumps(compact)


def test_recovery_parameters_survive_owner_and_wait_prompt_projection():
    from infer import _compact_delegate_task, _compact_delegate_wait_payload
    from runtime.skill_runtime import _task_prompt_view

    pages = list(range(1, 25))
    recovery = {
        "failed_task_id": "slide-original", "failed_owner_id": "owner-original",
        "action": "delegate_submit",
        "continuation_params": {"role": "slide", "group_id": "original-group", "pages": pages, "repair": True},
        "handoff_params": {"role": "slide", "handoff_from": "owner-original", "pages": pages, "repair": True},
    }
    task = {"task_id": "slide-original", "role": "slide", "status": "failed", "recovery": recovery}
    for projector in (_compact_delegate_task, _task_prompt_view):
        projected = projector(task)
        assert projected["recovery"] == recovery
    projected = _compact_delegate_wait_payload({"unresolved_failures": [task]})
    assert projected["unresolved_failures"][0]["recovery"] == recovery
    assert recovery["continuation_params"]["pages"] == pages


def test_wait_budget_fallback_keeps_executable_recovery_parameters():
    from infer import _compact_delegate_wait_payload

    recovery = {"action": "delegate_submit", "handoff_params": {
        "role": "slide", "handoff_from": "owner-original", "pages": list(range(1, 25)), "repair": True}}
    projected = _compact_delegate_wait_payload({
        "status": "idle", "recovery": [recovery],
        "unresolved_failures": [{"task_id": "slide-original", "recovery": recovery}],
        "diagnostics": {f"block-{index}": "x" * 10000 for index in range(100)},
    })
    assert projected["prompt_omissions"]["payload_budget_fallback"] is True
    assert projected["recovery"] == [recovery]
    assert projected["unresolved_failures"][0]["recovery"] == recovery


def test_delegate_wait_compacts_a_large_image_handoff_backlog_as_one_control_message():
    from infer import DELEGATE_WAIT_PROMPT_CHAR_LIMIT, _compact_delegate_wait_payload

    long_text = "verified pixels " + ("x" * 5000)
    events = []
    for index in range(40):
        artifact = f"image-{index % 20:02d}"
        events.append({
            "sequence": index + 1,
            "type": "image_handoff_ready",
            "task_id": "",
            "owner_id": "image-owner",
            "actionable": True,
            "dedup_key": f"revision-{index}",
            "consumer_pages": [index + 1],
            "image": {
                "artifact_id": artifact,
                "path": f"assets/{artifact}.png",
                "sha256": "a" * 64,
                "image_metadata": {"notes": long_text, "source": long_text},
            },
        })
    tasks = [{
        "task_id": f"task-{index:02d}",
        "owner_id": f"owner-{index:02d}",
        "role": "image",
        "pages": [index + 1],
        "status": "succeeded",
        "result": {
            "status": "completed",
            "final_text": long_text,
            "segment_ids": ["segment-a", "segment-b"],
            "tool_calls": 99,
            "vision_paths": ["one.png", "two.png"],
        },
    } for index in range(20)]

    compact = _compact_delegate_wait_payload({
        "status": "running",
        "all_terminal": False,
        "cursor": 40,
        "events": events,
        "tasks": tasks,
        "pending_requests": [],
        "unresolved_failures": [],
    })
    encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))

    assert len(encoded) <= DELEGATE_WAIT_PROMPT_CHAR_LIMIT
    handoffs = [event for event in compact["events"]
                if event.get("type") == "image_handoff_ready"]
    assert len(handoffs) == 20
    assert len({event["image"]["artifact_id"] for event in handoffs}) == 20
    assert next(event for event in handoffs
                if event["image"]["artifact_id"] == "image-00")["consumer_pages"] == [21]
    assert "image_metadata" not in encoded
    assert "segment_ids" not in encoded and "tool_calls" not in encoded and "vision_paths" not in encoded


def test_large_natural_orchestration_update_keeps_its_event_semantics():
    from infer import DELEGATE_WAIT_PROMPT_CHAR_LIMIT, _compact_delegate_wait_payload

    compact = _compact_delegate_wait_payload({
        "harness_event": "orchestration_update",
        "status": "running",
        "all_terminal": False,
        "cursor": 88,
        "notice": "处理当前后台状态。",
        "events": [{"type": "task_completed", "task_id": f"task-{index}"}
                   for index in range(80)],
        "tasks": [{
            "task_id": f"task-{index}",
            "role": "slide",
            "status": "succeeded",
            "result": {"final_text": "x" * 8000},
        } for index in range(80)],
        "pending_requests": [],
        "unresolved_failures": [],
    })
    encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))

    assert len(encoded) <= DELEGATE_WAIT_PROMPT_CHAR_LIMIT
    assert compact["harness_event"] == "orchestration_update"
    assert compact["status"] == "running" and compact["cursor"] == 88


def test_delegate_submit_does_not_echo_long_goals_or_results_into_model_history():
    from runtime.agent import PROMPT_CONTROL_RESULT_CHAR_LIMIT

    long_text = "task detail " + ("x" * 12000)
    payload = {
        "tasks": [{
            "task_id": f"slide-{index:02d}",
            "owner_id": f"owner-{index:02d}",
            "attempt": 1,
            "role": "slide",
            "pages": [index + 1],
            "group_id": f"page-{index + 1}",
            "status": "queued",
            "goal": long_text,
            "result": {"final_text": long_text, "tool_calls": 99},
            "request_key": "internal-request-key",
        } for index in range(12)],
        "new_task_ids": [f"slide-{index:02d}" for index in range(12)],
        "delegated_pages": list(range(1, 13)),
        "undelegated_pages": [],
    }
    tools = ToolRuntime()
    tools.register(_definition("delegate_submit", lambda *_: payload, role="orchestrator"))
    transport = ScriptedTransport([
        _response(_tool("submit-1", "delegate_submit", {"tasks": []}), stop_reason="tool_use"),
        _response(_text("委派完成")),
    ])

    _run(transport, tools, ToolContext("orchestrator", "orchestrator"))

    content = transport.calls[1][1].messages[-1]["content"][0]["content"]
    visible = json.loads(content)
    assert len(content) <= PROMPT_CONTROL_RESULT_CHAR_LIMIT
    assert len(visible["tasks"]) == 12
    assert all("goal" not in task and "result" not in task and "request_key" not in task
               for task in visible["tasks"])
    assert long_text in tools.events[-1]["content"]


def test_terminal_prompt_result_removes_duplicate_stdout_but_trace_keeps_it():
    from runtime.agent import PROMPT_CONTROL_RESULT_CHAR_LIMIT

    matches = [{
        "path": f"slides/slide_{index:02d}.html",
        "line": index,
        "text": "matched layout text " + ("x" * 500),
        "line_truncated": False,
    } for index in range(1, 81)]
    stdout = "\n".join(f"{row['path']}:{row['line']}:{row['text']}" for row in matches)
    payload = {
        "returncode": 0,
        "mode": "fixed_text",
        "matches": matches,
        "returned_count": len(matches),
        "stdout": stdout,
        "diagnostics": {"ref": "_trace/tool-output/full.jsonl", "records": matches},
    }
    tools = ToolRuntime()
    tools.register(_definition("terminal", lambda *_: payload))
    transport = ScriptedTransport([
        _response(_tool("terminal-1", "terminal", {"command": "rg layout slides"}), stop_reason="tool_use"),
        _response(_text("检索完成")),
    ])

    _run(transport, tools, ToolContext("owner-slide", "slide"))

    content = transport.calls[1][1].messages[-1]["content"][0]["content"]
    visible = json.loads(content)
    assert len(content) <= PROMPT_CONTROL_RESULT_CHAR_LIMIT
    assert "stdout" not in visible
    assert len(visible["matches"]) <= 24
    assert visible["matches_omitted"] >= 56
    assert "full_output_ref" not in visible and "ref" not in visible["diagnostics"]
    traced = json.loads(tools.events[-1]["content"])
    assert traced["stdout"] == stdout and len(traced["matches"]) == 80


def test_control_prompt_compactors_have_a_pathological_payload_fallback():
    from runtime.agent import (
        PROMPT_CONTROL_RESULT_CHAR_LIMIT,
        _delegate_submit_prompt_payload,
        _prompt_json_chars,
        _terminal_prompt_payload,
    )

    long_text = "x" * 10000
    submit = _delegate_submit_prompt_payload({
        "tasks": [{
            "task_id": f"task-{index:03d}",
            "role": "slide",
            "pages": [index + 1],
            "status": "failed",
            "error": long_text,
            "goal": long_text,
            "result": {"final_text": long_text},
        } for index in range(100)],
        "planned_page_count": 100,
        "delegated_pages": list(range(1, 101)),
        "parallelization_advisories": [{"message": long_text} for _ in range(20)],
    })
    terminal = _terminal_prompt_payload({
        "returncode": 0,
        "images": [{
            "artifact_id": f"image-{index:03d}",
            "path": f"assets/image-{index:03d}.png",
            "status": "available",
            "reason": long_text,
            "image_metadata": {f"field-{field}": long_text for field in range(30)},
        } for index in range(50)],
        "stdout": long_text,
        "diagnostics": {"records": [{"detail": long_text} for _ in range(100)]},
    })

    assert _prompt_json_chars(submit) <= PROMPT_CONTROL_RESULT_CHAR_LIMIT
    assert _prompt_json_chars(terminal) <= PROMPT_CONTROL_RESULT_CHAR_LIMIT
    assert all("goal" not in task and "result" not in task for task in submit["tasks"])
    assert terminal["image_catalog_path"] == "assets/catalog.md"


def test_agent_failure_emits_a_terminal_lifecycle_event() -> None:
    events = []
    runtime = AgentRuntime(
        settings=_settings(max_attempts=1),
        channels=(ProviderChannel("secret-a", "ch-a"),),
        transport=ScriptedTransport([ValueError("bad provider payload")]),
        tools=ToolRuntime(),
        event_sink=events.append,
    )
    with pytest.raises(AgentProviderError):
        runtime.run(
            agent_id="owner-slide",
            role="slide",
            system="正式角色卡",
            tool_context=ToolContext("owner-slide", "slide"),
            initial_messages=({"role": "user", "content": "开始"},),
            max_turns=2,
            max_tokens=128,
            thinking=False,
            effort="medium",
        )
    assert events[-1]["event_type"] == "agent_failed"
    assert events[-1]["error_type"] == "AgentProviderError"


@pytest.mark.parametrize("child_fails", [False, True])
def test_nested_agent_restores_outer_trace_identity_and_command_scope(child_fails):
    from runtime.diagnostics import _scope

    events = []
    observations = []
    tools = ToolRuntime()
    child_response = (_response(_tool("child-tool", "noop", {}), stop_reason="tool_use")
                      if child_fails else _response(_text("Child complete")))
    transport = ScriptedTransport([
        _response(_tool("outer-tool", "nested", {}), stop_reason="tool_use"), child_response,
    ])
    runtime = AgentRuntime(settings=_settings(), channels=(ProviderChannel("secret-a", "ch-a"),),
                           transport=transport, tools=tools, event_sink=events.append)

    def nested(_context, _arguments):
        before = (runtime._trace_local.turn, runtime._trace_local.segment_id, _scope.get())
        try:
            runtime.run(agent_id="child", role="slide", system="child", tool_context=ToolContext("child", "slide"),
                        initial_messages=({"role": "user", "content": "Child task"},),
                        max_turns=1, max_tokens=128, thinking=False, effort="low")
        except AgentTurnLimitError:
            assert child_fails
        observations.append((before, (getattr(runtime._trace_local, "turn", None),
                                     getattr(runtime._trace_local, "segment_id", None), _scope.get())))
        return {"child": "handled"}

    tools.register(_definition("nested", nested))
    tools.register(_definition("noop", lambda _context, _arguments: {"ok": True}))
    with pytest.raises(AgentTurnLimitError):
        runtime.run(agent_id="outer", role="slide", system="outer", tool_context=ToolContext("outer", "slide"),
                    initial_messages=({"role": "user", "content": "Outer task"},),
                    max_turns=1, max_tokens=128, thinking=False, effort="low")
    outer_failure = next(row for row in events if row["event_type"] == "agent_failed" and row["agent_id"] == "outer")
    outer_start = next(row for row in events if row["event_type"] == "agent_started" and row["agent_id"] == "outer")
    assert outer_failure["turn"] == 1
    assert outer_failure["segment_id"] == outer_start["segment_id"]
    assert observations[0][0] == observations[0][1]
    assert _scope.get() is None
    assert not hasattr(runtime._trace_local, "turn") and not hasattr(runtime._trace_local, "segment_id")
    tools.assert_closed()


def test_max_turns_fails_instead_of_silently_accepting_partial_work() -> None:
    tools = ToolRuntime()
    tools.register(_definition("again", lambda _context, _arguments: {"ok": True}))
    transport = ScriptedTransport(
        [
            _response(_tool("again-1", "again", {}), stop_reason="tool_use"),
            _response(_tool("again-2", "again", {}), stop_reason="tool_use"),
        ]
    )

    with pytest.raises(AgentTurnLimitError, match="max_turns=2"):
        _run(
            transport,
            tools,
            ToolContext("owner-slide", "slide"),
            max_turns=2,
        )


def _denied_definition() -> ToolDefinition:
    def deny(_context, _arguments):
        raise ToolExecutionError("read_scope_denied", "may not read scripts")

    return _definition("read_file", deny, role="orchestrator")


def test_repeated_identical_failure_trips_the_circuit_breaker() -> None:
    tools = ToolRuntime()
    tools.register(_denied_definition())
    # 每回合都提交完全相同的失败调用；应在到达 max_turns 前熔断。
    transport = ScriptedTransport(
        [
            _response(
                _tool(f"call-{i}", "read_file", {"path": "skills/x/scripts/y.py"}),
                stop_reason="tool_use",
            )
            for i in range(8)
        ]
    )
    events: list[dict] = []
    runtime = AgentRuntime(
        settings=_settings(),
        channels=(ProviderChannel("secret-a", "ch-a"),),
        transport=transport,
        tools=tools,
        event_sink=events.append,
    )
    checkpoints: list[SegmentCheckpoint] = []
    with pytest.raises(RepeatedToolFailureError) as excinfo:
        runtime.run(
            agent_id="orchestrator",
            role="orchestrator",
            system="正式角色卡",
            tool_context=ToolContext("orchestrator", "orchestrator"),
            initial_messages=({"role": "user", "content": "开始"},),
            max_turns=240,
            max_tokens=128,
            thinking=False,
            effort="medium",
            checkpoint_sink=checkpoints.append,
        )
    # 熔断阈值内触发，绝不空转到 max_turns。
    assert excinfo.value.repeat_count == 3
    assert excinfo.value.tool_name == "read_file"
    assert excinfo.value.error_code == "read_scope_denied"
    assert len(transport.calls) == 3
    # tool_use/tool_result 全部成对闭合，最后一个 checkpoint 也是闭合边界。
    assert tool_boundary(checkpoints[-1].messages).is_closed
    failed = [event for event in events if event["event_type"] == "agent_failed"]
    assert failed and failed[-1]["error_type"] == "RepeatedToolFailureError"
    assert failed[-1]["repeat_count"] == 3
    assert failed[-1]["last_failed_tool"] == "read_file"
    assert failed[-1]["last_failed_error_code"] == "read_scope_denied"


def test_different_argument_failures_are_not_treated_as_repeats() -> None:
    tools = ToolRuntime()
    tools.register(_denied_definition())
    # 三次失败但参数各不相同：不构成同一指纹，不应熔断；随后自然收口。
    transport = ScriptedTransport(
        [
            _response(
                _tool("c1", "read_file", {"path": "skills/x/scripts/a.py"}),
                stop_reason="tool_use",
            ),
            _response(
                _tool("c2", "read_file", {"path": "skills/x/scripts/b.py"}),
                stop_reason="tool_use",
            ),
            _response(
                _tool("c3", "read_file", {"path": "skills/x/scripts/c.py"}),
                stop_reason="tool_use",
            ),
            _response(_text("换一种做法后收口")),
        ]
    )

    result = _run(
        transport,
        tools,
        ToolContext("orchestrator", "orchestrator"),
        max_turns=240,
    )

    assert result.final_text == "换一种做法后收口"
    assert len(transport.calls) == 4


def test_argument_drift_failure_trips_the_coarse_breaker() -> None:
    tools = ToolRuntime()
    tools.register(_denied_definition())
    # 同一 tool + 同一 error_code,但每次 arguments 都在增长(模型幻觉出越来越长的
    # 子命令名),精确指纹永不重复。粗粒度熔断应在 COARSE_TOOL_FAILURE_LIMIT 内触发,
    # 而不是空转到 max_turns。
    transport = ScriptedTransport(
        [
            _response(
                _tool(
                    f"call-{i}",
                    "read_file",
                    {"path": "skills/x/scripts/" + ("y" * (i + 1)) + ".py"},
                ),
                stop_reason="tool_use",
            )
            for i in range(12)
        ]
    )
    events: list[dict] = []
    runtime = AgentRuntime(
        settings=_settings(),
        channels=(ProviderChannel("secret-a", "ch-a"),),
        transport=transport,
        tools=tools,
        event_sink=events.append,
    )
    with pytest.raises(RepeatedToolFailureError) as excinfo:
        runtime.run(
            agent_id="orchestrator",
            role="orchestrator",
            system="正式角色卡",
            tool_context=ToolContext("orchestrator", "orchestrator"),
            initial_messages=({"role": "user", "content": "开始"},),
            max_turns=240,
            max_tokens=128,
            thinking=False,
            effort="medium",
        )
    # 在粗粒度阈值(8)内触发,远不到 max_turns=240。
    assert excinfo.value.error_code == "read_scope_denied"
    assert excinfo.value.tool_name == "read_file"
    assert len(transport.calls) == 8
    assert excinfo.value.repeat_count == 8
    failure = [event for event in events if event["event_type"] == "agent_failed"][-1]
    assert failure["trigger"] == "coarse_repeat"
    assert failure["trigger_count"] == failure["threshold"] == failure["coarse_count"] == 8
    assert failure["exact_count"] == 1
    assert failure["related_tool_use_ids"] == [f"call-{i}" for i in range(8)]


def test_length_stop_recovers_then_completes() -> None:
    tools = ToolRuntime()
    # 首轮因输出上限被截断(length,无工具调用),一次定向恢复后正常收口。
    transport = ScriptedTransport(
        [
            _response(_text("超长内容第一段……"), stop_reason="length"),
            _response(_text("精简收口。")),
        ]
    )
    result = _run(
        transport,
        tools,
        ToolContext("slide", "slide"),
        max_turns=240,
        max_tokens=32768,
        thinking=True,
        effort="medium",
    )
    assert result.final_text == "精简收口。"
    assert len(transport.calls) == 2
    assert transport.calls[0][1].thinking and transport.calls[0][1].max_tokens == 32768
    assert not transport.calls[1][1].thinking and transport.calls[1][1].max_tokens == 32768


def test_length_stop_hard_fails_past_the_retry_limit() -> None:
    tools = ToolRuntime()
    # 一次定向恢复仍无工具调用/收口时硬失败。
    transport = ScriptedTransport(
        [_response(_text("又超长……"), stop_reason="length") for _ in range(6)]
    )
    with pytest.raises(AgentProtocolError):
        _run(
            transport,
            tools,
            ToolContext("slide", "slide"),
            max_turns=240,
        )
    assert len(transport.calls) == 2


@pytest.mark.parametrize("read_tool", ["read_file", "think"])
def test_readonly_tools_cannot_renew_length_recovery_budget(read_tool):
    tools = ToolRuntime()
    calls = []
    tools.register(_definition(read_tool, lambda _ctx, args: calls.append(args) or {"text": "already available context"}))
    arguments = {"path": "base.css"} if read_tool == "read_file" else {"thought": "Use the existing context."}
    truncated = lambda: _response(_text("unfinished"), stop_reason="max_tokens")
    transport = ScriptedTransport([
        truncated(), _response(_tool("read-1", read_tool, arguments)),
        truncated(), _response(_tool("read-2", read_tool, arguments)),
        truncated(), _response(_text("should not reach a fourth budget")),
    ])
    checkpoints = []
    with pytest.raises(AgentProtocolError, match="token limit"):
        _run(transport, tools, ToolContext("slide", "slide"), max_turns=20,
             checkpoint_sink=checkpoints.append)
    assert len(transport.calls) == 3
    assert len(calls) == 1
    assert all(tool_boundary(checkpoint.messages).is_closed for checkpoint in checkpoints)


def test_writing_progress_can_renew_length_recovery_budget():
    tools = ToolRuntime()
    tools.register(_definition("write_file", lambda _ctx, args: {
        "path": "slides/slide_01.html", "sha256": args["content"]}))
    truncated = lambda: _response(_text("unfinished"), stop_reason="max_tokens")
    transport = ScriptedTransport([
        truncated(),
        _response(_tool("write-1", "write_file", {"path": "slides/slide_01.html", "content": "first"})),
        truncated(),
        _response(_tool("write-2", "write_file", {"path": "slides/slide_01.html", "content": "second"})),
        _response(_text("done")),
    ])
    result = _run(transport, tools, ToolContext("slide", "slide"), max_turns=20)
    assert result.final_text == "done"
    assert tool_boundary(result.checkpoint.messages).is_closed


def test_empty_argument_tool_calls_recover_then_complete() -> None:
    tools = ToolRuntime()
    recorded: list[dict] = []

    def submit(_context, arguments):
        recorded.append(dict(arguments))
        return {"ok": True, "wave_id": "w1"}

    tools.register(_definition("delegate_submit", submit, role="slide", required=("tasks",)))
    # BUG-Q:opus-5 复读退化——第一轮吐出 3 个空参 delegate_submit(stop=end_turn,
    # 躲过 length-stop 恢复)。软恢复应丢弃该退化轮、提示重发,第二轮带完整参数收口,
    # 而不是把 3 个空调用执行成 missing_arguments → 熔断判失败。
    transport = ScriptedTransport(
        [
            _response(
                _tool("call-a", "delegate_submit", {}),
                _tool("call-b", "delegate_submit", {}),
                _tool("call-c", "delegate_submit", {}),
                stop_reason="end_turn",
            ),
            _response(
                _tool("call-ok", "delegate_submit", {"tasks": [{"role": "slide"}]}),
                stop_reason="tool_use",
            ),
            _response(_text("已提交。")),
        ]
    )
    result = _run(
        transport,
        tools,
        ToolContext("slide", "slide"),
        max_turns=240,
    )
    assert result.final_text == "已提交。"
    # 空参轮未被执行(未落到 submit),只有带 tasks 的那次真正执行。
    assert recorded == [{"tasks": [{"role": "slide"}]}]
    # 首轮退化 + 恢复轮 + 收口 = 3 次 provider 调用(退化轮未空转、未熔断)。
    assert len(transport.calls) == 3


def test_empty_argument_tool_calls_hard_fail_past_the_retry_limit() -> None:
    tools = ToolRuntime()
    tools.register(_definition("delegate_submit", lambda _c, _a: {"ok": True}, required=("tasks",)))
    # 一次定向恢复仍为空参工具调用时硬失败,不空转到 max_turns。
    transport = ScriptedTransport(
        [
            _response(_tool(f"c{i}", "delegate_submit", {}), stop_reason="end_turn")
            for i in range(6)
        ]
    )
    with pytest.raises(AgentProtocolError):
        _run(
            transport,
            tools,
            ToolContext("slide", "slide"),
            max_turns=240,
        )
    assert len(transport.calls) == 2


def test_empty_response_recovers_without_publishing_an_invented_message_or_tool():
    tools = ToolRuntime()
    tools.register(_definition("write_file", lambda *_: {"sha256": "new"}))
    transport = ScriptedTransport([
        _response(),
        _response(_tool("write", "write_file", {"path": "research.md", "content": "Evidence"})),
        _response(), _response(_text("Delivered")),
    ])
    events = []
    runtime = AgentRuntime(settings=_settings(), channels=(ProviderChannel("secret-a", "ch-a"),),
        transport=transport, tools=tools, event_sink=events.append)
    result = runtime.run(agent_id="slide", role="slide", system="Role", tool_context=ToolContext("slide", "slide"),
        initial_messages=({"role":"user", "content":"Start"},), max_turns=10, max_tokens=128, thinking=False, effort="medium")
    assert result.final_text == "Delivered"
    assert result.tool_calls == 1
    assert sum(e["event_type"] == "agent_empty_response_recovered" for e in events) == 2
    assert [e["message"] for e in events if e["event_type"] == "assistant_message"] == ["Delivered"]
    assert tool_boundary(result.checkpoint.messages).is_closed
    assert all(m.get("content") for m in result.checkpoint.messages)


@pytest.mark.parametrize("mixed", [False, True])
def test_empty_response_and_empty_tool_calls_share_one_bounded_budget(mixed):
    tools = ToolRuntime()
    tools.register(_definition("write_file", lambda *_: {"ok": True}, required=("path", "content")))
    outcomes = [_response() for _ in range(6)]
    if mixed:
        outcomes[1] = _response(_tool("unused", "write_file", {}), stop_reason="end_turn")
    transport = ScriptedTransport(outcomes)
    checkpoints = []
    with pytest.raises(AgentProtocolError, match="empty replies or empty-argument"):
        _run(transport, tools, ToolContext("slide", "slide"), max_turns=20, checkpoint_sink=checkpoints.append)
    assert len(transport.calls) == 2
    assert tool_boundary(checkpoints[-1].messages).is_closed


def test_valid_zero_argument_tool_is_not_rejected_as_empty_action():
    calls = []
    tools = ToolRuntime()
    tools.register(_definition("read_status", lambda *_: calls.append("executed") or {"status": "pending"}))
    transport = ScriptedTransport([_response(_tool("status", "read_status", {}), stop_reason="end_turn"),
                                   _response(_text("Status read."))])
    result = _run(transport, tools, ToolContext("slide", "slide"))
    assert calls == ["executed"] and result.tool_calls == 1
    assert tool_boundary(result.checkpoint.messages).is_closed


def test_whitespace_only_keeps_normal_empty_response_budget():
    tools = ToolRuntime()
    block = {"type": "text", "text": "   "}
    transport = ScriptedTransport([_response(block), _response(_text("Delivered"))])
    result = _run(transport, tools, ToolContext("slide", "slide"))
    assert len(transport.calls) == result.turns == 2
    assert result.final_text == "Delivered" and result.tool_calls == 0
    assert tool_boundary(result.checkpoint.messages).is_closed

    transport = ScriptedTransport([_response(block) for _ in range(6)])
    with pytest.raises(AgentProtocolError, match="empty replies or empty-argument"):
        _run(transport, tools, ToolContext("slide", "slide"))
    assert len(transport.calls) == 2


@pytest.mark.parametrize("stop_reason,blocks,kind,recovery_tokens", [
    ("max_tokens", [], "output_budget_exhausted_no_action", 32768),
    ("length", [], "output_budget_exhausted_no_action", 32768),
    ("max_tokens", [_thinking("private-reasoning")], "output_budget_exhausted_no_action", 32768),
    ("end_turn", [_thinking("private-reasoning")], "reasoning_only_no_action", 4096),
    ("end_turn", [{"type": "redacted_thinking", "data": "private-data"}], "reasoning_only_no_action", 4096),
])
def test_no_action_classification_uses_failure_shaped_execution_window(
    stop_reason, blocks, kind, recovery_tokens
):
    import json
    tools, events, checkpoints = ToolRuntime(), [], []
    transport = ScriptedTransport([
        _response(*blocks, stop_reason=stop_reason), _response(_text("Delivered")),
    ])
    runtime = AgentRuntime(settings=_settings(), channels=(ProviderChannel("secret-a", "ch-a"),
        ProviderChannel("secret-b", "ch-b")), transport=transport, tools=tools, event_sink=events.append,
        balance="primary-first")
    result = runtime.run(agent_id="slide", role="slide", system="fixture", tool_context=ToolContext("slide", "slide"),
        initial_messages=({"role": "user", "content": "Start"},), max_turns=6, max_tokens=32768,
        thinking=True, effort="medium", checkpoint_sink=checkpoints.append)
    assert result.final_text == "Delivered" and len(transport.calls) == 2
    assert [channel for channel, _ in transport.calls] == ["ch-a", "ch-a"]
    first_request = transport.calls[0][1]
    recovery_request = transport.calls[1][1]
    assert first_request.thinking and first_request.max_tokens == 32768 and first_request.effort == "medium"
    assert not recovery_request.thinking
    assert recovery_request.max_tokens == recovery_tokens
    assert recovery_request.effort == ""
    feedback = transport.calls[1][1].messages[-1]["content"][0]["text"]
    assert "参数完整" in feedback and "关闭 thinking" in feedback
    no_action = [event for event in events if event["event_type"] == "agent_no_action_response"]
    assert len(no_action) == 1 and no_action[0]["reason"] == kind
    assert no_action[0]["retry_allowed"] and no_action[0]["configuration_changed"]
    assert no_action[0]["recovery_configuration"] == {
        "thinking": False,
        "effort": "",
        "max_tokens": recovery_tokens,
    }
    assert not any(event["event_type"] in {"agent_empty_response_recovered", "provider_failed", "provider_failover"}
                   for event in events)
    assert "private-reasoning" not in json.dumps(events) and "signed-private" not in json.dumps(events)
    assert checkpoints and all(tool_boundary(checkpoint.messages).is_closed for checkpoint in checkpoints)
    assert not tools.events


@pytest.mark.parametrize("first,second", [
    (_response(stop_reason="max_tokens"), _response(stop_reason="max_tokens")),
    (_response(_thinking("private")), _response(_thinking("private"))),
    (_response(stop_reason="max_tokens"), _response(_thinking("private"))),
    (_response(_thinking("private")), _response()),
    (_response(analysis_summaries=("Allowed brief summary",)), _response()),
])
def test_no_action_recovery_stops_after_one_changed_attempt_and_preserves_checkpoint(first, second):
    tools, checkpoints = ToolRuntime(), []
    transport = ScriptedTransport([first, second, _response(_text("must not reach"))])
    with pytest.raises(AgentProtocolError, match="one targeted recovery"):
        _run(transport, tools, ToolContext("same-owner", "slide"), thinking=True,
             max_turns=20, checkpoint_sink=checkpoints.append)
    assert len(transport.calls) == 2 and not tools.events
    assert len({checkpoint.segment_id for checkpoint in checkpoints}) == 1
    assert all(tool_boundary(checkpoint.messages).is_closed for checkpoint in checkpoints)
    resumed = _run(ScriptedTransport([_response(_text("resumed"))]), tools,
                   ToolContext("same-owner", "slide"), initial_messages=(), checkpoint=checkpoints[-1])
    assert resumed.agent_id == "same-owner" and resumed.final_text == "resumed"


@pytest.mark.parametrize("error_code", ["repair_reason_required", "missing_arguments", "invalid_arguments", "unknown_arguments", "image_review_required", "terminal_argument_invalid"])
def test_eight_argument_failures_get_feedback_before_owner_circuit(error_code):
    tools, checkpoints = ToolRuntime(), []
    reason = "右侧标签覆盖数值，保留数据与图例，仅移动该标签。"
    def patch(_context, arguments):
        if not arguments.get("repair_reason"):
            raise ToolExecutionError(error_code, "provide a real repair_reason")
        return {"path": arguments["path"], "sha256": "changed"}
    tools.register(_definition("patch", patch))
    failed = [_tool(f"bad-{index}", "patch", {"path": f"slides/slide_{index + 1:02d}.html"}) for index in range(8)]
    transport = ScriptedTransport([
        _response(*failed, stop_reason="tool_use"),
        _response(_tool("fixed", "patch", {"path": "slides/slide_01.html", "repair_reason": reason})),
        _response(_text("done")),
    ])
    result = _run(transport, tools, ToolContext("slide", "slide"), checkpoint_sink=checkpoints.append)
    assert result.final_text == "done" and result.tool_calls == 9
    feedback = transport.calls[1][1].messages[-1]["content"]
    assert len(feedback) == 8 and [block["tool_use_id"] for block in feedback] == [f"bad-{i}" for i in range(8)]
    assert all(block["error_code"] == error_code for block in feedback)
    assert all(tool_boundary(checkpoint.messages).is_closed for checkpoint in checkpoints)
    tools.assert_closed()


def test_batch_denied_page_patches_get_feedback_without_granting_write_access():
    tools, checkpoints, executed = ToolRuntime(), [], []

    def patch(_context, arguments):
        executed.append(arguments["path"])
        return {"path": arguments["path"], "sha256": "saved"}

    tools.register(replace(_definition("patch", patch, role="orchestrator"),
                           mutates_workspace=True, path_argument="path"))
    context = ToolContext("orchestrator", "orchestrator", write_scopes=(WriteScope("deck.md"),))
    transport = ScriptedTransport([
        _response(*[_tool(f"denied-{i}", "patch", {"path": f"slides/slide_{i + 1:02d}.html"})
                    for i in range(8)]),
        _response(_tool("allowed", "patch", {"path": "deck.md"})),
        _response(_text("done")),
    ])

    result = _run(transport, tools, context, checkpoint_sink=checkpoints.append)
    feedback = transport.calls[1][1].messages[-1]["content"]
    assert result.tool_calls == 9 and executed == ["deck.md"]
    assert len(feedback) == 8 and all(block["error_code"] == "write_scope_denied" for block in feedback)
    assert all("original Slide Owner" in block["content"] for block in feedback)
    assert not context.may_write("slides/slide_01.html")
    assert all(tool_boundary(checkpoint.messages).is_closed for checkpoint in checkpoints)
    tools.assert_closed()


def test_delegate_rejection_keeps_current_owner_and_correction_path_in_next_request():
    tools = ToolRuntime()
    feedback = {
        "status": "rejected", "submission_accepted": False,
        "current_tasks": [{"task_id": "active-task", "group_id": "original", "role": "slide",
                           "pages": [1, 2], "status": "running",
                           "continuation_after_task_finishes": {"group_id": "original", "pages": [1, 2], "repair": True}}],
        "next_action": "Wait for the current task. Repair through its original group_id after it finishes.",
    }

    def rejected(_context, _arguments):
        raise ToolExecutionError("delegation_rejected", "active ownership", diagnostics=feedback)

    tools.register(_definition("delegate_submit", rejected, role="orchestrator"))
    transport = ScriptedTransport([
        _response(_tool("rejected", "delegate_submit", {"tasks": []})),
        _response(_text("Wait for the active Owner.")),
    ])
    _run(transport, tools, ToolContext("orchestrator", "orchestrator"))
    receipt = transport.calls[1][1].messages[-1]["content"][0]
    assert receipt["error_code"] == "delegation_rejected"
    assert json.loads(receipt["content"]) == feedback
    tools.assert_closed()


@pytest.mark.parametrize("error_code,expected_rounds", [("repair_reason_required", 3), ("image_review_required", 3), ("write_scope_denied", 3), ("terminal_argument_invalid", 3)])
def test_batch_argument_feedback_still_trips_repeats_without_weakening_scope_checks(error_code, expected_rounds):
    tools, checkpoints = ToolRuntime(), []
    def denied(_context, _arguments):
        raise ToolExecutionError(error_code, "fixture")
    tools.register(_definition("patch", denied))
    outcomes = [_response(*[_tool(f"r{turn}-{item}", "patch", {"path": "slides/slide_01.html"}) for item in range(8)],
                          stop_reason="tool_use") for turn in range(5)]
    transport = ScriptedTransport(outcomes)
    with pytest.raises(RepeatedToolFailureError) as caught:
        _run(transport, tools, ToolContext("same-owner", "slide"), max_turns=20, checkpoint_sink=checkpoints.append)
    assert len(transport.calls) == expected_rounds and caught.value.error_code == error_code
    assert caught.value.diagnostics["count_unit"] == ("feedback_rounds" if expected_rounds > 1 else "calls")
    if expected_rounds > 1:
        assert caught.value.repeat_count == 3
        assert all(len(request.messages[-1]["content"]) == 8 for _, request in transport.calls[1:])
    assert all(tool_boundary(checkpoint.messages).is_closed for checkpoint in checkpoints)
    assert len([event for event in tools.events if event["event_type"] == "tool_result"]) == expected_rounds * 8
    tools.assert_closed()


def test_unrelated_new_read_does_not_reset_missing_argument_feedback_count():
    import json
    from runtime.agent import _ToolProgress
    from runtime.tools import ToolCall, ToolResult
    progress = _ToolProgress()
    for turn in range(3):
        call = ToolCall(f"failure-{turn}", "patch", {"path": "slides/slide_01.html"})
        progress.observe(call, ToolResult(call.tool_use_id, call.name, False, "give a reason", "repair_reason_required"),
                         argument_failures=set())
        read = ToolCall(f"read-{turn}", "read_file", {"path": f"new-{turn}.md"})
        progress.observe(read, ToolResult(read.tool_use_id, read.name, True,
            json.dumps({"path": f"new-{turn}.md", "content": f"new evidence {turn}"})))
    with pytest.raises(RepeatedToolFailureError) as caught:
        progress.check("slide", [call, read])
    assert caught.value.repeat_count == 3 and caught.value.diagnostics["count_unit"] == "feedback_rounds"


def _patch_definition(sha_by_call, *, role: str = "image"):
    """成功 patch 处理器:按调用顺序回执给定的内容 sha256,模拟内容演化/打转。"""
    state = {"n": 0}

    def patch(_context, arguments):
        index = state["n"]
        state["n"] += 1
        return {
            "path": str(arguments.get("path") or ""),
            "logical_path": str(arguments.get("path") or ""),
            "replacements": 1,
            "sha256": sha_by_call(index, arguments),
        }

    return _definition("patch", patch, role=role)


def test_no_progress_patch_loop_trips_the_breaker() -> None:
    tools = ToolRuntime()
    # BUG-N:同一文件被反复成功 patch,但内容始终在两个旧 sha256 间抖动(无净进展)。
    # 成功调用无 error_code,躲过按失败计数的熔断;无进展熔断应在 max_turns 前触发。
    tools.register(_patch_definition(lambda i, _a: "aaaa" if i % 2 == 0 else "bbbb"))
    transport = ScriptedTransport(
        [
            _response(
                _tool(f"call-{i}", "patch", {"path": "assets/catalog.md"}),
                stop_reason="tool_use",
            )
            for i in range(60)
        ]
    )
    events: list[dict] = []
    runtime = AgentRuntime(
        settings=_settings(),
        channels=(ProviderChannel("secret-a", "ch-a"),),
        transport=transport,
        tools=tools,
        event_sink=events.append,
    )
    with pytest.raises(RepeatedToolFailureError) as excinfo:
        runtime.run(
            agent_id="owner-image",
            role="image",
            system="正式角色卡",
            tool_context=ToolContext("owner-image", "image"),
            initial_messages=({"role": "user", "content": "开始"},),
            max_turns=240,
            max_tokens=128,
            thinking=False,
            effort="medium",
        )
    assert excinfo.value.error_code == "no_progress_patch_loop"
    assert excinfo.value.tool_name == "patch"
    # 阈值(8)内触发,远不到 max_turns=240;首个新 hash 不计,之后每个旧 hash 计一次。
    assert len(transport.calls) < 20
    failed = [event for event in events if event["event_type"] == "agent_failed"]
    assert failed and failed[-1]["last_failed_error_code"] == "no_progress_patch_loop"


def test_legitimate_refinement_with_revert_does_not_trip() -> None:
    tools = ToolRuntime()
    # Master 关切的合法场景:v1→v2→发现 v2 更差→退回 v1→再前进 v3→v4…。退回会短暂
    # 命中旧 hash,但下一步产生新 hash 立即清零,故绝不误杀往复式打磨。构造若干步,
    # 每步基本是净新版本,中间穿插两次“退回旧版本”,最后自然收口。
    sequence: list[str] = []
    seen: list[str] = []
    for i in range(20):
        if i in (5, 11) and len(seen) >= 2:
            sequence.append(seen[-2])  # 退回到更早的旧版本(合法回退)
        else:
            fresh = f"sha-{i:03d}"
            seen.append(fresh)
            sequence.append(fresh)
    outcomes = [
        _response(
            _tool(f"call-{i}", "patch", {"path": "slides/slide_01.html"}),
            stop_reason="tool_use",
        )
        for i in range(len(sequence))
    ]
    outcomes.append(_response(_text("打磨完成,收口。")))
    tools.register(_patch_definition(lambda i, _a: sequence[i], role="slide"))
    transport = ScriptedTransport(outcomes)
    result = _run(
        transport,
        tools,
        ToolContext("owner-slide", "slide"),
        agent_id="owner-slide",
        role="slide",
        max_turns=240,
    )
    assert result.final_text == "打磨完成,收口。"
    # 全程无熔断:所有 patch + 收口都被消费。
    assert len(transport.calls) == len(sequence) + 1


@pytest.mark.parametrize("name", ["patch", "write_file"])
def test_read_and_inspect_do_not_reset_unchanged_edit_detection(name):
    tools = ToolRuntime()
    tools.register(_definition(name, lambda _c, a: {"path": a["path"], "sha256": "same"}))
    tools.register(_definition("read_file", lambda *_: {"content": "current HTML"}))
    tools.register(_definition("inspect", lambda *_: {"png_sha256": "same-pixels"}))
    outcomes = []
    for i in range(30):
        for tool, args in [(name, {"path": "slides/slide_01.html"}),
                           ("read_file", {"path": "slides/slide_01.html"}),
                           ("inspect", {"page": 1})]:
            outcomes.append(_response(_tool(f"{i}-{tool}", tool, args), stop_reason="tool_use"))
    transport = ScriptedTransport(outcomes)
    checkpoints = []
    with pytest.raises(RepeatedToolFailureError) as error:
        _run(transport, tools, ToolContext("owner-slide", "slide"), max_turns=100,
             checkpoint_sink=checkpoints.append)
    assert error.value.tool_name == name
    assert error.value.error_code == ("no_progress_patch_loop" if name == "patch" else "no_progress_write_loop")
    assert len(transport.calls) < 30
    assert tool_boundary(checkpoints[-1].messages).is_closed


def _write_file_definition(role: str = "slide") -> ToolDefinition:
    def write_file(_context, arguments):
        if "content" not in arguments or "path" not in arguments:
            raise ToolExecutionError(
                "missing_arguments",
                f"$ is missing required fields "
                f"{[k for k in ('path', 'content') if k not in arguments]}",
            )
        return {"path": arguments["path"], "sha256": "deadbeef"}

    return _definition("write_file", write_file, role=role)


@pytest.mark.parametrize("name,argument", [("web_search", "query"), ("web_extract", "url")])
@pytest.mark.parametrize("queries", [1, 2])
def test_repeated_cached_evidence_stops_at_a_closed_successful_boundary(name, argument, queries):
    tools = ToolRuntime()
    tools.register(_definition(name, lambda *_: {"cached": True, "results": []}))
    tools.register(_definition("read_file", lambda *_: {"content": "existing evidence"}))
    outcomes = []
    for i in range(30):
        outcomes.append(_response(_tool(f"web-{i}", name, {argument: f"private-query-token-{i % queries}"})))
        outcomes.append(_response(_tool(f"read-{i}", "read_file", {"path": "research.md"})))
    transport = ScriptedTransport(outcomes)
    checkpoints = []
    with pytest.raises(RepeatedToolFailureError) as error:
        _run(transport, tools, ToolContext("slide", "slide"), max_turns=100,
             checkpoint_sink=checkpoints.append)
    assert error.value.error_code == "no_progress_cached_web_loop"
    assert error.value.tool_name == name
    assert "private-query-token" not in str(error.value)
    assert "private-query-token" not in error.value.arguments_summary
    assert len(transport.calls) == 17 + 2 * queries
    assert tool_boundary(checkpoints[-1].messages).is_closed
    assert all(not block.get("is_error") for message in checkpoints[-1].messages
               if message["role"] == "user" and isinstance(message["content"], list)
               for block in message["content"] if block.get("type") == "tool_result")


@pytest.mark.parametrize("progress", ["new_query", "new_content", "uncached_result"])
def test_cached_evidence_allows_real_progress(progress):
    tools = ToolRuntime()
    cache_hits = iter([progress != "uncached_result" or i % 6 != 0 for i in range(18)])
    tools.register(_definition("web_search", lambda *_: {"cached": next(cache_hits)}))
    tools.register(_definition("write_file", lambda _c, a: {"path": a["path"], "sha256": a["content"]}))
    outcomes = []
    for cycle in range(3):
        for i in range(6):
            arguments = {"query": str(cycle) if progress == "new_query" else "same"}
            outcomes.append(_response(_tool(f"web-{cycle}-{i}", "web_search", arguments)))
        if progress == "new_content":
            outcomes.append(_response(_tool(f"write-{cycle}", "write_file",
                                           {"path": "research.md", "content": str(cycle)})))
    outcomes.append(_response(_text("Evidence delivered")))
    transport = ScriptedTransport(outcomes)
    assert _run(transport, tools, ToolContext("slide", "slide"), max_turns=30).final_text == "Evidence delivered"


@pytest.mark.parametrize("behavior", ["repeat", "repeat_with_think", "finish_after_warning",
                                      "pagination", "changed_content", "write_progress"])
def test_file_reads_share_bounded_evidence_progress(behavior):
    tools = ToolRuntime()
    reads = iter(range(30))
    def read(_context, arguments):
        index = next(reads)
        return {"path": arguments["path"], "offset": arguments["offset"],
                "content": f"private-content-{index if behavior == 'changed_content' else 0}"}
    tools.register(_definition("read_file", read))
    tools.register(_definition("think", lambda *_: {"accepted": True}))
    tools.register(_definition("write_file", lambda _c, a: {"path": a["path"], "sha256": a["content"]}))
    count = 9 if behavior == "finish_after_warning" else 24
    outcomes = []
    for i in range(count):
        outcomes.append(_response(_tool(f"read-{i}", "read_file", {
            "path": "slides/slide_03.html", "offset": i + 1 if behavior == "pagination" else 14})))
        if behavior == "repeat_with_think":
            outcomes.append(_response(_tool(f"think-{i}", "think", {"thought": "Use the current evidence."})))
        if behavior == "write_progress" and i % 6 == 5:
            outcomes.append(_response(_tool(f"write-{i}", "write_file", {
                "path": "slides/slide_03.html", "content": str(i)})))
    outcomes.append(_response(_text("Reported the remaining issue")))
    transport = ScriptedTransport(outcomes)
    checkpoints = []
    if behavior.startswith("repeat"):
        with pytest.raises(RepeatedToolFailureError) as error:
            _run(transport, tools, ToolContext("slide", "slide"), max_turns=60,
                 checkpoint_sink=checkpoints.append)
        assert error.value.error_code == "no_progress_file_read_loop"
        assert error.value.tool_name == "read_file"
        assert "private-content" not in str(error.value)
        assert len(transport.calls) < 24
    else:
        result = _run(transport, tools, ToolContext("slide", "slide"), max_turns=60,
                      checkpoint_sink=checkpoints.append)
        assert result.final_text == "Reported the remaining issue"
    assert tool_boundary(checkpoints[-1].messages).is_closed
    warnings = [m for m in checkpoints[-1].messages if "Harness progress check" in str(m.get("content"))]
    assert len(warnings) == (1 if behavior.startswith("repeat") or behavior == "finish_after_warning" else 0)


def test_eight_repeated_reads_are_one_feedback_round_not_eight():
    import json
    from runtime.agent import _ToolProgress
    from runtime.tools import ToolCall, ToolResult
    progress = _ToolProgress()
    call = ToolCall("r", "read_file", {"path": "deck.md"})
    result = ToolResult("r", "read_file", True, json.dumps({"path": "deck.md", "content": "plan"}))
    progress.observe(call, result)
    for batch in range(9):
        repeated = set()
        for _ in range(8):
            progress.observe(call, result, repeated_evidence=repeated)
        assert progress.evidence_repeats == batch + 1
        if batch < 7:
            assert progress.check("orchestrator", [call]) is False
        elif batch == 7:
            assert progress.check("orchestrator", [call]) is True
        else:
            with pytest.raises(RepeatedToolFailureError):
                progress.check("orchestrator", [call])


def test_successful_action_ends_a_no_action_recovery_episode():
    tools = ToolRuntime()
    tools.register(_definition("write_file", lambda _c, a: {"path": a["path"], "sha256": a["content"]}))
    transport = ScriptedTransport([
        _response(_thinking("first temporary no-action response")),
        _response(_tool("write-1", "write_file", {"path": "page.html", "content": "first version"})),
        _response(_thinking("a separate no-action response after real work")),
        _response(_tool("write-2", "write_file", {"path": "page.html", "content": "second version"})),
        _response(_text("done")),
    ])
    result = _run(transport, tools, ToolContext("slide", "slide"), max_turns=8)
    assert result.final_text == "done"
    assert [(request.thinking, request.effort, request.max_tokens) for _, request in transport.calls] == [
        (False, "medium", 128),
        (False, "", 128),
        (False, "medium", 128),
        (False, "", 128),
        (False, "medium", 128),
    ]
    tools.assert_closed()


def test_slide_high_thinking_is_reserved_for_the_initial_planning_turn():
    tools = ToolRuntime()
    tools.register(_definition(
        "read_file", lambda _context, _arguments: {"content": "brief"}
    ))
    transport = ScriptedTransport([
        _response(_tool("read-1", "read_file", {"path": "briefs/slide_01.md"})),
        _response(_text("done")),
    ])

    result = _run(
        transport,
        tools,
        ToolContext("slide", "slide"),
        thinking=True,
        effort="high",
    )

    assert result.final_text == "done"
    assert [(request.thinking, request.effort) for _, request in transport.calls] == [
        (True, "high"),
        (False, ""),
    ]


@pytest.mark.parametrize("first,expected_tokens", [
    (_response(_tool("read-1", "read_file", {"path": "briefs/slide_01.md"})), 32768),
    (_response(_thinking("private")), 4096),
    (_response(stop_reason="max_tokens"), 32768),
])
def test_k3_execution_and_targeted_recovery_use_low_reasoning(first, expected_tokens):
    tools, events = ToolRuntime(), []
    tools.register(_definition("read_file", lambda _context, _arguments: {"content": "brief"}))
    transport = ScriptedTransport([first, _response(_text("done"))])
    runtime = AgentRuntime(settings=replace(_settings(), api_format="openai", model="kimi-k3"),
        channels=(ProviderChannel("secret", "a"),), transport=transport, tools=tools,
        event_sink=events.append)
    result = runtime.run(agent_id="slide", role="slide", system="fixture",
        tool_context=ToolContext("slide", "slide"), max_turns=4, max_tokens=32768,
        thinking=True, effort="high")
    assert result.final_text == "done"
    assert [(request.thinking, request.effort) for _, request in transport.calls] == [
        (True, "high"), (True, "low")]
    assert transport.calls[1][1].max_tokens == expected_tokens
    recoveries = [event for event in events if event["event_type"] == "agent_no_action_response"]
    if recoveries:
        assert recoveries[0]["recovery_configuration"]["thinking"]
        assert recoveries[0]["recovery_configuration"]["effort"] == "low"
        assert "low 思考档位" in repr(transport.calls[1][1].messages)
        assert "关闭 thinking" not in repr(transport.calls[1][1].messages)


@pytest.mark.parametrize("behavior", ["repeat_cycle", "new_directory", "changed_listing", "write_progress", "finish_after_warning"])
def test_directory_listing_uses_existing_evidence_progress(behavior):
    tools = ToolRuntime()
    reads = iter(range(60))
    def listing(_context, arguments):
        index = next(reads)
        path = arguments["command"].split()[-1].rstrip("/")
        return {"path": path, "returncode": 0, "truncated": False,
                "stdout": f"file\\t{index if behavior == 'changed_listing' else 12}\\t{path}/reference.md"}
    tools.register(_definition("terminal", listing))
    tools.register(_definition("write_file", lambda _c, a: {"path": a["path"], "sha256": a["content"]}))
    outcomes = []
    count = 19 if behavior == "finish_after_warning" else 45
    for i in range(count):
        directory = i if behavior == "new_directory" else i % 11
        outcomes.append(_response(_tool(f"ls-{i}", "terminal", {
            "command": f"ls -la skills/test/references/section-{directory}/"})))
        if behavior == "write_progress" and i % 6 == 5:
            outcomes.append(_response(_tool(f"write-{i}", "write_file", {
                "path": "slides/slide_01.html", "content": str(i)})))
    outcomes.append(_response(_text("Proceed using the listed evidence")))
    transport = ScriptedTransport(outcomes)
    checkpoints = []
    if behavior == "repeat_cycle":
        with pytest.raises(RepeatedToolFailureError) as error:
            _run(transport, tools, ToolContext("slide", "slide"), max_turns=100,
                 checkpoint_sink=checkpoints.append)
        assert error.value.error_code == "no_progress_directory_loop"
        assert len(transport.calls) == 20
    else:
        assert _run(transport, tools, ToolContext("slide", "slide"), max_turns=100,
                    checkpoint_sink=checkpoints.append).final_text == "Proceed using the listed evidence"
    assert tool_boundary(checkpoints[-1].messages).is_closed
    warnings = [m for m in checkpoints[-1].messages if "Harness progress check" in str(m.get("content"))]
    assert len(warnings) == (1 if behavior in {"repeat_cycle", "finish_after_warning"} else 0)


def test_orchestrator_directory_polling_waits_for_existing_events_at_a_closed_checkpoint():
    tools, checkpoints, waits, events = ToolRuntime(), [], [], []
    tools.register(_definition("terminal", lambda _c, _a: {
        "path": "slides", "returncode": 0, "stdout": "slide_01.html", "truncated": False,
    }, role="orchestrator"))
    tools.register(_definition("write_file", lambda _c, a: {
        "path": a["path"], "sha256": "new-brief",
    }, role="orchestrator"))
    update = {"role": "user", "content": "Slide Owner finished the requested page; inspect the delivered result."}

    def idle():
        assert checkpoints and tool_boundary(checkpoints[-1].messages).is_closed
        waits.append(checkpoints[-1])
        return update if len(waits) == 1 else None

    transport = ScriptedTransport([
        *[_response(_tool(f"ls-{i}", "terminal", {"command": "ls slides/"})) for i in range(9)],
        _response(_tool("write", "write_file", {"path": "deck.md", "content": "updated plan"})),
        _response(_text("done")),
    ])
    runtime = AgentRuntime(settings=_settings(), channels=(ProviderChannel("secret-a", "ch-a"),),
                           transport=transport, tools=tools, event_sink=events.append)
    result = runtime.run(agent_id="orchestrator", role="orchestrator", system="role card",
                         tool_context=ToolContext("orchestrator", "orchestrator"),
                         initial_messages=({"role": "user", "content": "start"},), max_turns=15,
                         max_tokens=128, thinking=False, effort="medium", idle_handler=idle,
                         checkpoint_sink=checkpoints.append)

    assert result.final_text == "done" and len(waits) == 2
    assert update in transport.calls[9][1].messages
    assert len([event for event in events if event["event_type"] == "agent_directory_poll_waited"]) == 1
    assert not any(event["event_type"] == "agent_repeated_evidence_warning" for event in events)
    assert all(tool_boundary(checkpoint.messages).is_closed for checkpoint in checkpoints)
    tools.assert_closed()


@pytest.mark.parametrize("name", ["vision_analyze", "inspect", "inspect_group", "render", "terminal"])
@pytest.mark.parametrize("behavior", ["finish_after_warning", "repeat_after_warning", "new_pixels"])
def test_repeated_pixels_have_one_progress_warning_and_a_bounded_exit(name, behavior):
    tools = ToolRuntime()
    def receipt(_context, arguments):
        pixels = str(arguments['image'] if behavior == 'new_pixels' else arguments['image'] % 7)
        if name in {"render", "terminal"}:
            return {"pages": [{"page": pixels, "page_uid": f"p-{pixels}", "html_sha256": pixels, "png_sha256": pixels}]}
        return {"png_sha256": pixels}
    tools.register(_definition(name, receipt))
    turns = 15 if behavior == "finish_after_warning" else 30
    outcomes = [_response(_tool(f"vision-{i}", name, {"image": i, "question": f"different question {i}"}))
                for i in range(turns)]
    outcomes.append(_response(_text("Verified and delivered")))
    transport = ScriptedTransport(outcomes)
    checkpoints = []
    if behavior == "repeat_after_warning":
        with pytest.raises(RepeatedToolFailureError) as error:
            _run(transport, tools, ToolContext("slide", "slide"), max_turns=40, checkpoint_sink=checkpoints.append)
        assert error.value.error_code == ("no_progress_render_loop" if name in {"render", "terminal"}
                                          else "no_progress_visual_evidence_loop")
        assert len(transport.calls) == 16
    else:
        result = _run(transport, tools, ToolContext("slide", "slide"), max_turns=40, checkpoint_sink=checkpoints.append)
        assert result.final_text == "Verified and delivered"
    assert tool_boundary(checkpoints[-1].messages).is_closed
    warnings = [m for m in checkpoints[-1].messages if "Harness progress check" in str(m.get("content"))]
    assert len(warnings) == (0 if behavior == "new_pixels" else 1)


def test_truncated_tool_call_recovers_then_completes() -> None:
    tools = ToolRuntime()
    tools.register(_write_file_definition(role="slide"))
    # BUG-P:opus-5 写大 content 时在“吐工具参数中途”撞 max_tokens →最后一个 tool_use
    # 的 input 残缺(只有 path 缺 content)。直接执行会 missing_arguments;应软恢复:丢弃
    # 截断轮 + 提示,重试后模型发出完整 write_file 才收口。
    transport = ScriptedTransport(
        [
            _response(
                _tool("t1", "write_file", {"path": "research.md"}),  # 截断:缺 content
                stop_reason="max_tokens",
            ),
            _response(
                _tool("t2", "write_file", {"path": "research.md", "content": "完整正文"}),
                stop_reason="tool_use",
            ),
            _response(_text("已写入,收口。")),
        ]
    )
    events: list[dict] = []
    runtime = AgentRuntime(
        settings=_settings(),
        channels=(ProviderChannel("secret-a", "ch-a"),),
        transport=transport,
        tools=tools,
        event_sink=events.append,
    )
    result = runtime.run(
        agent_id="owner-slide",
        role="slide",
        system="正式角色卡",
        tool_context=ToolContext("owner-slide", "slide"),
        initial_messages=({"role": "user", "content": "开始"},),
        max_turns=240,
        max_tokens=32768,
        thinking=True,
        effort="medium",
    )
    assert result.final_text == "已写入,收口。"
    # 截断轮未执行(否则会 missing_arguments 失败事件);完整轮成功。
    assert not [e for e in events if e["event_type"] == "agent_failed"]
    # 截断的 write_file 未被当作工具执行(无对应失败的 tool_result 熔断)。
    assert len(transport.calls) == 3
    assert transport.calls[0][1].thinking and transport.calls[0][1].max_tokens == 32768
    assert not transport.calls[1][1].thinking and transport.calls[1][1].max_tokens == 32768
    assert transport.calls[2][1].thinking and transport.calls[2][1].max_tokens == 32768


def test_persistent_tool_call_truncation_hard_fails_past_the_limit() -> None:
    tools = ToolRuntime()
    tools.register(_write_file_definition(role="slide"))
    # 一次定向恢复后仍拿不到完整工具调用时硬失败,不空转。
    transport = ScriptedTransport(
        [
            _response(
                _tool(f"t{i}", "write_file", {"path": "research.md"}),
                stop_reason="max_tokens",
            )
            for i in range(6)
        ]
    )
    with pytest.raises(AgentProtocolError):
        _run(
            transport,
            tools,
            ToolContext("owner-slide", "slide"),
            agent_id="owner-slide",
            role="slide",
            max_turns=240,
        )
    assert len(transport.calls) == 2


def test_success_between_failures_resets_the_breaker() -> None:
    calls = {"n": 0}

    def flaky(_context, _arguments):
        calls["n"] += 1
        # 第 2 次调用成功，用来清零连续失败计数。
        if calls["n"] == 2:
            return {"ok": True}
        raise ToolExecutionError("known_failure", "可恢复失败")

    tools = ToolRuntime()
    tools.register(_definition("flaky", flaky, role="orchestrator"))
    # 失败、成功、失败、失败：成功清零后只累计到 2 次，未达阈值 3，不熔断。
    transport = ScriptedTransport(
        [
            _response(_tool("f1", "flaky", {}), stop_reason="tool_use"),
            _response(_tool("f2", "flaky", {}), stop_reason="tool_use"),
            _response(_tool("f3", "flaky", {}), stop_reason="tool_use"),
            _response(_tool("f4", "flaky", {}), stop_reason="tool_use"),
            _response(_text("最终收口")),
        ]
    )

    result = _run(
        transport,
        tools,
        ToolContext("orchestrator", "orchestrator"),
        max_turns=240,
    )

    assert result.final_text == "最终收口"


@pytest.mark.parametrize("success", ["unrelated", "cached-render", "same-read"])
def test_unrelated_success_does_not_erase_an_unresolved_failure(success):
    tools = ToolRuntime()
    name = "read_file" if success == "same-read" else "terminal"
    payload = ({"path": "base.css", "content": "existing CSS", "offset": 1} if success == "same-read"
               else {"pages": [{"page": 2, "html_sha256": "html", "png_sha256": "png"}]}
               if success == "cached-render" else {"stdout": "help text", "returncode": 0})
    def terminal(_context, arguments):
        if arguments["command"] == "render . --page 1":
            raise ToolExecutionError("render_source_uncommitted", "先写入 slides/slide_01.html")
        return payload
    tools.register(_definition("terminal", terminal))
    if name == "read_file":
        tools.register(_definition(name, lambda *_: payload))
    success_call = lambda i: _response(_tool(f"success-{i}", name, {"command": "--help"}))
    outcomes = [success_call(0)]
    for i in range(4):
        outcomes += [_response(_tool(f"fail-{i}", "terminal", {"command": "render . --page 1"})), success_call(i + 1)]
    transport = ScriptedTransport(outcomes)
    with pytest.raises(RepeatedToolFailureError) as caught:
        _run(transport, tools, ToolContext("slide", "slide"), max_turns=20)
    assert caught.value.error_code == "render_source_uncommitted"
    assert caught.value.repeat_count == 3
    assert len(transport.calls) == 6
    tools.assert_closed()


@pytest.mark.parametrize("next_action", ["read_file", "write_file", "patch"])
def test_new_diagnostic_or_authored_source_allows_the_first_repair(next_action):
    from runtime.agent import _ToolProgress
    from runtime.tools import ToolCall, ToolResult
    progress = _ToolProgress()
    for i in range(2):
        call = ToolCall(f"failure-{i}", "render", {"page": 1})
        progress.observe(call, ToolResult(call.tool_use_id, call.name, False, "source uncommitted", "render_source_uncommitted"))
    repair = ToolCall("repair", next_action, {"path": "slides/slide_01.html"})
    import json
    payload = {"path": "slides/slide_01.html", "sha256": "new-authored-version", "content": "source diagnostic"}
    progress.observe(repair, ToolResult(repair.tool_use_id, repair.name, True, json.dumps(payload)))
    assert progress.exact_count == progress.coarse_count == 0
    assert not progress.check("slide", [repair])


def _inspect_tools(tmp_path: Path) -> ToolRuntime:
    """注册一个把当前 PNG 当 native inspect 回投、并带延迟提交描述子的桩工具。"""
    render = tmp_path / "renders" / "slide_01.png"
    render.parent.mkdir(parents=True)
    Image.new("RGB", (32, 18), (18, 42, 76)).save(render)
    tools = ToolRuntime()
    tools.register(
        ToolDefinition(
            name="inspect",
            description="inspect",
            input_schema={
                "type": "object",
                "properties": {"page": {"type": "array", "minItems": 1, "items": {"type": "integer"}}},
                "required": ["page"],
                "additionalProperties": False,
            },
            handler=lambda _c, _a: {
                "native_multimodal": True,
                "path": "renders/slide_01.png",
                "inspection_id": "inspection-1",
                "pending_inspection": {"page": 1, "inspection_id": "inspection-1"},
            },
            roles=frozenset({"slide"}),
        )
    )
    return tools


def test_inspect_attaches_image_and_commits_after_image_is_consumed(
    tmp_path: Path,
) -> None:
    tools = _inspect_tools(tmp_path)
    committed: list[dict] = []
    tools.deferred_inspection_committer = lambda d: committed.append(dict(d))
    transport = ScriptedTransport(
        [
            _response(_tool("insp-1", "inspect", {"page": [1]}), stop_reason="tool_use"),
            _response(_text("已看到当前像素")),
        ]
    )
    context = ToolContext(
        "owner-slide", "slide", metadata={"workspace_root": str(tmp_path)}
    )
    result = _run(transport, tools, context)
    # 图片被放回下一次请求。
    block = transport.calls[1][1].messages[-1]["content"][0]
    assert [part["type"] for part in block["content"]] == ["text", "image"]
    # 延迟提交:图片被下一次成功请求消费后才提交,恰好一次。
    assert committed == [{"page": 1, "inspection_id": "inspection-1"}]
    assert result.vision_paths == ("renders/slide_01.png",)


def test_inspection_is_not_committed_when_the_next_request_fails(
    tmp_path: Path,
) -> None:
    tools = _inspect_tools(tmp_path)
    committed: list[dict] = []
    tools.deferred_inspection_committer = lambda d: committed.append(dict(d))
    # 第二次请求(消费图片的那次)非可恢复失败 → agent 失败,inspection 绝不提交。
    transport = ScriptedTransport(
        [
            _response(_tool("insp-1", "inspect", {"page": [1]}), stop_reason="tool_use"),
            ValueError("provider exploded while consuming the image"),
        ]
    )
    context = ToolContext(
        "owner-slide", "slide", metadata={"workspace_root": str(tmp_path)}
    )
    with pytest.raises(AgentProviderError):
        _run(transport, tools, context)
    assert committed == []


def test_cached_deck_vision_receipt_does_not_attach_another_image(tmp_path):
    tools = ToolRuntime()
    tools.register(_definition("vision_analyze", lambda *_: {
        "path": "renders/slide_01.png", "sha256": "same-pixels",
        "scope_key": "page-layout", "inspection_cached": True, "native_multimodal": False,
    }, role="orchestrator"))
    transport = ScriptedTransport([
        _response(_tool("cached", "vision_analyze", {"path": "renders/slide_01.png"}), stop_reason="tool_use"),
        _response(_text("复用当前检查回执")),
    ])
    result = _run(transport, tools, ToolContext("orchestrator", "orchestrator", metadata={"workspace_root": str(tmp_path)}))
    assert result.vision_paths == ()
    assert isinstance(transport.calls[1][1].messages[-1]["content"][0]["content"], str)


def test_more_than_three_repairs_continue_until_model_finishes():
    repaired = []
    def patch(_context, arguments):
        repaired.append(arguments["version"])
        return {"path": "slides/slide_01.html", "sha256": str(arguments["version"])}
    tools = ToolRuntime()
    tools.register(_definition("patch", patch))
    transport = ScriptedTransport([
        *[_response(_tool(f"repair-{version}", "patch", {"version": version}), stop_reason="tool_use")
          for version in range(1, 5)],
        _response(_text("第四轮修复完成，继续依据实际问题判断。")),
    ])
    result = _run(transport, tools, ToolContext("owner", "slide"))
    assert repaired == [1, 2, 3, 4] and len(transport.calls) == 5
    assert result.stop_reason == "end_turn"
    assert tool_boundary(result.checkpoint.messages).is_closed
    assert [event["event_type"] for event in tools.events] == ["tool_use", "tool_result"] * 4


# --- 层②:上下文窗口 compact 兜底(BUG-1) ---


def _tiny_window_settings(*, context_window: int, safety_buffer: int = 0):
    return replace(
        _settings(max_attempts=2),
        context_window=context_window,
        context_safety_buffer=safety_buffer,
    )


def _runtime_with_settings(transport, tools, settings, **kw):
    kw.setdefault("summary_invoker", _summary_invoker)
    return AgentRuntime(
        settings=settings,
        channels=(ProviderChannel("secret-a", "ch-a"),),
        transport=transport,
        tools=tools,
        balance="primary-first",
        **kw,
    )


def test_small_context_does_not_compact() -> None:
    from runtime.agent import estimate_request_tokens  # noqa: F401 - reachability
    events: list[dict] = []
    transport = ScriptedTransport([_response(_text("done"))])
    runtime = _runtime_with_settings(
        transport,
        ToolRuntime(),
        _tiny_window_settings(context_window=262144),
    )
    runtime.event_sink = events.append
    result = runtime.run(
        agent_id="owner-slide",
        role="slide",
        system="role card",
        tool_context=ToolContext("owner-slide", "slide"),
        initial_messages=({"role": "user", "content": "short"},),
        max_turns=4,
        max_tokens=128,
        thinking=False,
        effort="low",
    )
    assert result.final_text == "done"
    assert not any(e.get("event_type") == "agent_context_compacted" for e in events)


def test_near_window_triggers_compaction_then_continues() -> None:
    events: list[dict] = []
    # 大初始消息(~6万字符 → 估算数万 token)配一个小窗口,第一轮就超预算触发压缩。
    big = "字" * 60000
    transport = ScriptedTransport([_response(_text("finished"))])
    # window 小到:压缩后的摘要(几百字符)才装得下,原始 6万字符装不下。
    settings = _tiny_window_settings(context_window=20000, safety_buffer=1000)
    runtime = _runtime_with_settings(transport, ToolRuntime(), settings)
    runtime.event_sink = events.append
    result = runtime.run(
        agent_id="owner-slide",
        role="slide",
        system="role card",
        tool_context=ToolContext("owner-slide", "slide"),
        initial_messages=({"role": "user", "content": big},),
        max_turns=4,
        max_tokens=2000,
        thinking=False,
        effort="low",
    )
    assert result.final_text == "finished"
    compactions = [e for e in events if e.get("event_type") == "agent_context_compacted"]
    assert len(compactions) == 1
    # 压缩后真正发给 Provider 的第一条请求已显著变短:不含原始 6万字符正文。
    sent = transport.calls[0][1]
    blob = "".join(str(m.get("content")) for m in sent.messages)
    assert big not in blob
    assert len(blob) < len(big)
    # 结束 checkpoint 仍是闭合边界。
    assert tool_boundary(result.checkpoint.messages).is_closed


def test_compaction_preserves_tool_transaction_pairing() -> None:
    # 历史里有一对已闭合 tool_use/tool_result;压缩发生在闭合边界,不破坏配对。
    calls: list = []

    def tool_handler(_ctx, _args):
        calls.append(1)
        return {"ok": True}

    tools = ToolRuntime()
    tools.register(_definition("noop", tool_handler))

    big = "长" * 60000
    prior_messages = (
        {"role": "user", "content": big},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "u1", "name": "noop", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "u1", "content": "prior"}],
        },
    )
    checkpoint = SegmentCheckpoint.initial(prior_messages)
    settings = _tiny_window_settings(context_window=20000, safety_buffer=1000)
    transport = ScriptedTransport([_response(_text("closed"))])
    runtime = _runtime_with_settings(transport, tools, settings)
    result = runtime.run(
        agent_id="owner-slide",
        role="slide",
        system="role card",
        tool_context=ToolContext("owner-slide", "slide"),
        checkpoint=checkpoint,
        max_turns=4,
        max_tokens=2000,
        thinking=False,
        effort="low",
    )
    assert result.final_text == "closed"
    # 压缩后的请求消息中没有任何未配对的 tool_use / tool_result。
    sent = transport.calls[0][1]
    assert tool_boundary(sent.messages).is_closed
    # 原来的 tool handler 不会因压缩被重复执行。
    assert calls == []


def test_compaction_is_bounded_and_fails_clearly_when_still_over_window() -> None:
    from runtime.agent import AgentContextWindowError

    events: list[dict] = []
    big = "字" * 60000
    # 窗口极小:即便压缩成摘要也仍超预算(safety_buffer 吃满预算) → 有界后清晰失败。
    settings = _tiny_window_settings(context_window=200, safety_buffer=0)
    # 永不消费的响应:必须在发请求前就因窗口失败。
    transport = ScriptedTransport([_response(_text("never"))])
    runtime = _runtime_with_settings(transport, ToolRuntime(), settings)
    runtime.event_sink = events.append
    with pytest.raises(AgentInterruptedError) as excinfo:
        runtime.run(
            agent_id="owner-slide",
            role="slide",
            system="role card",
            tool_context=ToolContext("owner-slide", "slide"),
            initial_messages=({"role": "user", "content": big},),
            max_turns=8,
            max_tokens=64,
            thinking=False,
            effort="low",
        )
    # 有界:不是 opaque 400,错误信息带估算/预算/窗口。
    assert "summary request has no capacity" in str(excinfo.value)
    # 没有请求真的被发出(在 pre-send 门就失败)。
    assert transport.calls == []
    compactions = [e for e in events if e.get("event_type") == "agent_context_compacted"]
    assert compactions == []  # 不创建无法发送的新段，也不对同一次超限重复压缩。


def test_separate_successful_context_compactions_do_not_exhaust_failure_budget():
    from runtime.session import estimate_request_tokens

    episodes = 6
    tools = ToolRuntime()
    calls, events = [], []
    def load_page(_context, arguments):
        calls.append(arguments["page"])
        return {"page": arguments["page"], "text": "当前页面源码" * 12000}
    tools.register(_definition("load_page", load_page))
    transport = ScriptedTransport([
        *[_response(_tool(f"load-{page}", "load_page", {"page": page}), stop_reason="tool_use")
          for page in range(1, episodes + 1)],
        _response(_text("All page contexts consumed.")),
    ])
    runtime = _runtime_with_settings(transport, tools,
        _tiny_window_settings(context_window=20000, safety_buffer=1000))
    runtime.event_sink = events.append
    result = runtime.run(agent_id="group-owner", role="slide", system="role card",
        tool_context=ToolContext("group-owner", "slide"),
        initial_messages=({"role": "user", "content": "Inspect the group."},),
        max_turns=episodes + 2, max_tokens=2000, thinking=False, effort="low")
    assert result.final_text == "All page contexts consumed."
    assert calls == list(range(1, episodes + 1))
    compactions = [event for event in events if event["event_type"] == "agent_context_compacted"]
    assert len(compactions) == episodes
    for _, request in transport.calls:
        assert estimate_request_tokens(request.system, request.messages, request.tools) <= 17000
        assert tool_boundary(request.messages).is_closed


def test_resume_preserves_inspect_feedback_as_tool_context_without_reconciliation(tmp_path, monkeypatch):
    from test_inspection_transactions import (
        _fixture, _run as inspection_run, _call, _response as inspection_response,
        ScriptedTransport as InspectionTransport,
    )
    workspace, _, context, arguments, tools, committed = _fixture(tmp_path, monkeypatch, "inspect")
    tools.inspection_feedback = lambda context, call, output: {
        **output, "critic_feedback": {"completed": True, "body": "右侧留白较多，请结合布局自行判断。"}}
    inspection_run(InspectionTransport([_call("inspect", arguments), inspection_response()]), tools, context)
    checkpoints = []
    with pytest.raises(AgentProviderError):
        inspection_run(InspectionTransport([_call("inspect", arguments, "cached"), ValueError("fixture failure")]),
            tools, context, checkpoint_sink=checkpoints.append)
    transport = InspectionTransport([inspection_response()])
    result = inspection_run(transport, tools, context, checkpoint=checkpoints[-1])
    assert result.stop_reason == "end_turn" and "review_handoff" not in result.to_dict()
    assert len(committed) == 1
    parts = transport.requests[0].messages[-1]["content"][0]["content"]
    assert [part["type"] for part in parts] == ["text", "image"]
    assert "右侧留白" in parts[0]["text"]


@pytest.mark.parametrize("name", ["inspect", "group", "path"])
def test_compaction_preserves_unconsumed_native_image_transaction(tmp_path, monkeypatch, name):
    from test_inspection_transactions import (
        _fixture, _run as inspection_run, _call, _response as inspection_response,
        _payload_and_parts, ScriptedTransport as InspectionTransport,
    )
    workspace, _, context, arguments, tools, committed = _fixture(tmp_path, monkeypatch, name)
    checkpoints = []
    with pytest.raises(AgentProviderError):
        inspection_run(InspectionTransport([_call("inspect", arguments), ValueError("fixture failure")]),
            tools, context, checkpoint_sink=checkpoints.append)
    checkpoint = replace(checkpoints[-1], messages=(
        {"role": "user", "content": "字" * 60000}, *checkpoints[-1].messages[1:]))
    events = []
    consumed = []
    def consume(request):
        payload, parts = _payload_and_parts(request)
        assert committed == []
        assert [part["type"] for part in parts] == ["text", "image"]
        assert tool_boundary(request.messages).is_closed
        consumed.append(payload["pending_inspection"])
        return inspection_response()
    transport = InspectionTransport([consume])
    runtime = _runtime_with_settings(transport, tools,
        _tiny_window_settings(context_window=20000, safety_buffer=1000))
    runtime.event_sink = events.append
    result = runtime.run(agent_id=context.owner_id, role=context.role, system="fixture",
        tool_context=context, checkpoint=checkpoint, max_turns=8, max_tokens=128,
        thinking=False, effort="low")
    assert any(event["event_type"] == "agent_context_compacted" for event in events)
    assert len(committed) == 1 and committed == consumed
    assert tool_boundary(result.checkpoint.messages).is_closed
    assert len(tools.events) == 2
