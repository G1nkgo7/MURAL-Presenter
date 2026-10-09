"""真实调度求助中断必须穿透工具适配器，在闭合 checkpoint 后停止模型循环。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from infer import PresenterKernel
from orchestration.manager import OrchestrationError, OrchestrationManager, TaskStatus
from runtime.agent import AgentInterruptedError, AgentRuntime
from runtime.live_trace import LiveEventJournal, read_live_events
from runtime.recovery import SegmentCheckpoint, tool_boundary
from runtime.session import ProviderChannel
from runtime.tools import ToolCall, ToolContext, ToolRuntime
from state.workspace import _atomic_text_write
from test_agent_runtime import ScriptedTransport, _definition, _response, _run, _settings, _text, _tool
from test_owner_assistance_scheduling import wait_for
from test_orchestration_manager import drain


@pytest.mark.parametrize("wait", [False, True])
def test_close_stops_provider_after_durable_help_result_and_resumes_same_owner(tmp_path, wait):
    journal = LiveEventJournal(tmp_path / "_trace/live-events.jsonl")
    checkpoint_path = tmp_path / "_state/test-checkpoint.json"
    manager = OrchestrationManager(tmp_path, deck_id="help-stop", max_workers=1)
    transport = ScriptedTransport([
        _response(_tool("help-1", "request_orchestrator", {
            "issue_type": "source", "question": "Need a verified source", "pages": [1],
        }), stop_reason="tool_use"),
        _response(_text("This must not be requested after shutdown")),
    ])
    tools = ToolRuntime(event_sink=journal.record_tool_event)
    handler = PresenterKernel._request_orchestrator_handler(SimpleNamespace(orchestration=manager))
    tools.register(_definition("request_orchestrator", handler))
    runtime = AgentRuntime(settings=_settings(), channels=(ProviderChannel("test", "test"),),
        transport=transport, tools=tools, event_sink=journal.record_runtime_event)
    identities = []

    def arguments(task):
        return dict(agent_id=task.owner_id, role=task.role, system="Owner role",
            tool_context=ToolContext(task.owner_id, task.role, metadata={
                "task_id": task.task_id, "attempt": task.attempt,
            }), max_turns=3, max_tokens=128, thinking=False, effort="medium")

    def owner(task, _manager):
        identities.append((task.owner_id, task.attempt))
        return runtime.run(**arguments(task), initial_messages=({"role": "user", "content": "Make page"},),
            checkpoint_sink=lambda checkpoint: _atomic_text_write(checkpoint_path, json.dumps(checkpoint.to_dict())))

    try:
        manager.register_runner("slide", owner)
        manager.submit_tasks([{"task_id": "page", "role": "slide", "pages": [1]}])
        wait_for(manager, lambda: bool(manager._state["requests"]))
        manager.close(wait=wait)
        manager._futures["page"].result(timeout=5)
        manager.close()
        assert manager.task("page").status == TaskStatus.INTERRUPTED
        assert len(transport.calls) == 1
        tools.assert_closed()
        saved = SegmentCheckpoint.from_dict(json.loads(checkpoint_path.read_text()))
        assert tool_boundary(saved.messages).is_closed
        result = saved.messages[-1]["content"][0]
        assert result["tool_use_id"] == "help-1" and result["error_code"] == "owner_interrupted"
        events = read_live_events(journal.path)["events"]
        assert events[-1]["event_type"] == "agent_failed"
        assert events[-1]["payload"]["error_type"] == "AgentInterruptedError"
        tool_events = [row for row in events if row["event_type"] in {"tool_use", "tool_result"}]
        assert [row["event_type"] for row in tool_events] == ["tool_use", "tool_result"]
    finally:
        manager.close()

    recovered = OrchestrationManager(tmp_path, deck_id="help-stop", max_workers=1)
    resumed_transport = ScriptedTransport([_response(_text("Ready after resume"))])
    resumed_tools = ToolRuntime(event_sink=journal.record_tool_event)
    resumed_runtime = AgentRuntime(settings=_settings(), channels=(ProviderChannel("test", "test"),),
        transport=resumed_transport, tools=resumed_tools, event_sink=journal.record_runtime_event)

    def resume(task, _manager):
        identities.append((task.owner_id, task.attempt))
        return resumed_runtime.run(**arguments(task), checkpoint=saved).final_text

    try:
        recovered.register_runner("slide", resume)
        drain(recovered)
        assert recovered.task("page").status == TaskStatus.COMPLETED
        assert identities[0] == identities[1]
        assert resumed_transport.calls[0][1].messages == saved.messages
        events = read_live_events(journal.path)["events"]
        assert sum(row["event_type"] == "tool_use" for row in events) == 1
        assert len({row["event_id"] for row in events}) == len(events)
        assert [row["sequence"] for row in events] == list(range(1, len(events) + 1))
    finally:
        recovered.close()


def test_ordinary_help_error_remains_a_recoverable_tool_failure():
    def reject(*_args, **_kwargs):
        raise OrchestrationError("invalid request")

    handler = PresenterKernel._request_orchestrator_handler(
        SimpleNamespace(orchestration=SimpleNamespace(request_owner_assistance=reject)))
    tools = ToolRuntime()
    tools.register(_definition("request_orchestrator", handler))
    result = tools.execute(ToolContext("owner", "slide", metadata={"task_id": "page"}),
        ToolCall("help", "request_orchestrator", {"issue_type": "source", "question": "Need source"}))
    assert not result.ok and result.error_code == "owner_request_failed"
    tools.assert_closed()


def test_font_interruption_reaches_orchestrator_wait_and_keeps_owner_for_resume(tmp_path):
    manager = OrchestrationManager(tmp_path, deck_id='font-stop', max_workers=1)
    def owner(*_):
        raise AgentInterruptedError('font_integrity_failed: pinned font identity differs')
    checkpoints = []
    tools = ToolRuntime()
    handler = PresenterKernel._delegate_wait_handler(SimpleNamespace(orchestration=manager))
    tools.register(_definition('delegate_wait', handler, role='orchestrator'))
    transport = ScriptedTransport([
        _response(_tool('wait-1', 'delegate_wait', {}), stop_reason='tool_use'),
        _response(_text('Must not continue while font integrity is broken')),
    ])
    try:
        manager.register_runner('slide', owner)
        manager.submit_tasks([{'task_id': 'page', 'role': 'slide', 'pages': [1]}])
        manager._futures['page'].result(timeout=5)
        original = manager.task('page')
        assert original.status == TaskStatus.INTERRUPTED
        with pytest.raises(AgentInterruptedError, match='font_integrity_failed'):
            _run(transport, tools, ToolContext('orchestrator', 'orchestrator'),
                 checkpoint_sink=checkpoints.append)
        assert len(transport.calls) == 1
        assert tool_boundary(checkpoints[-1].messages).is_closed
        block = checkpoints[-1].messages[-1]['content'][0]
        assert block['error_code'] == 'owner_interrupted'
        assert 'font_integrity_failed' in block['content']
        tools.assert_closed()
    finally:
        manager.close()

    recovered = OrchestrationManager(tmp_path, deck_id='font-stop', max_workers=1)
    try:
        recovered.register_runner('slide', lambda *_: {'status': 'completed'})
        drain(recovered)
        current = recovered.task('page')
        assert current.status == TaskStatus.COMPLETED
        assert (current.owner_id, current.attempt) == (original.owner_id, original.attempt)
    finally:
        recovered.close()
