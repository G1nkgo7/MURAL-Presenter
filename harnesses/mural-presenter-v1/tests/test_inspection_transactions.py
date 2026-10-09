"""真实检查 Handler + Agent 事务；Provider 仅使用确定性脚本响应。"""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from infer import build_inspect_handlers
from runtime.agent import AgentProviderError, AgentRuntime
from runtime.config import ProviderSettings
from runtime.recovery import tool_boundary
from runtime.session import ProviderChannel, TransportResponse
from runtime.tools import ToolContext, ToolRuntime, WriteScope, register_tools
from owner.registry import OwnerRegistry
from state.artifacts import ArtifactStore
from state.workspace import PageLifecycle, WorkspaceState
from test_native_inspect import _analyzer, _group_workspace, _make_workspace, _renderer


def _response(tool=None):
    return SimpleNamespace(content=[tool or SimpleNamespace(type="text", text="已检查")],
        stop_reason="tool_use" if tool else "end_turn", analysis_summaries=(),
        usage=SimpleNamespace(input_tokens=3, output_tokens=2,
            cache_read_input_tokens=0, cache_creation_input_tokens=0))


def _call(name, arguments, tool_id="check-1"):
    return _response(SimpleNamespace(type="tool_use", id=tool_id, name=name, input=arguments))


class ScriptedTransport:
    def __init__(self, outcomes):
        self.outcomes, self.requests = list(outcomes), []

    def complete(self, channel, request, *, total_timeout_s):
        self.requests.append(copy.deepcopy(request))
        outcome = self.outcomes.pop(0)
        if callable(outcome):
            outcome = outcome(request)
        if isinstance(outcome, Exception):
            raise outcome
        return TransportResponse(outcome, first_byte_s=.01)


def _tools(workspace, artifacts):
    inspect, commit = build_inspect_handlers(workspace, artifacts, _renderer, _analyzer)
    tools = ToolRuntime()
    register_tools(tools, {"inspect": inspect}, require_complete=False)
    committed = []

    def record(descriptor):
        created = commit(descriptor)
        if created is True:
            committed.append(descriptor)
        return created

    tools.deferred_inspection_committer = record
    return tools, committed


def _fixture(tmp_path, monkeypatch, name):
    if name == "group":
        workspace, artifacts, _ = _group_workspace(tmp_path, monkeypatch)
        arguments = {"page": [1, 2]}
    else:
        workspace, artifacts = _make_workspace(tmp_path / "deck",
            author_role="orchestrator" if name == "path" else "slide")
        arguments = {"page": [1]}
        if name == "path":
            artifacts.render_pages([1], _renderer)
            arguments = {"path": "renders/slide_01.png", "question": "检查标题"}
    role = "orchestrator" if name == "path" else "slide"
    owner = OwnerRegistry(workspace.paths.root, deck_id=workspace.deck_id).page_owner(1)
    context = ToolContext(owner.owner_id, role, read_scopes=(WriteScope("renders"),),
        metadata={"workspace_root": str(workspace.paths.root), "pages": [1, 2]})
    tools, committed = _tools(workspace, artifacts)
    return workspace, artifacts, context, arguments, tools, committed


def _run(transport, tools, context, **overrides):
    settings = ProviderSettings(api_format="anthropic", model="fixture",
        base_url="https://provider.test", total_timeout_s=30, connect_timeout_s=5,
        idle_timeout_s=10, channel_cooldown_s=0, max_attempts=1, streaming=False,
        prompt_cache=False, prompt_cache_messages=False)
    event_sink = overrides.pop("event_sink", None)
    agent = AgentRuntime(settings=settings, channels=(ProviderChannel("fixture", "local"),),
        transport=transport, tools=tools, event_sink=event_sink)
    arguments = dict(agent_id=context.owner_id, role=context.role, system="fixture",
        tool_context=context, max_turns=8, max_tokens=128, thinking=False, effort="low")
    if "checkpoint" not in overrides:
        arguments["initial_messages"] = ({"role": "user", "content": "检查"},)
    return agent.run(**{**arguments, **overrides})


def _payload_and_parts(request):
    block = next(
        (
            item
            for message in reversed(request.messages)
            for item in reversed(message.get("content", ()))
            if isinstance(item, dict) and item.get("type") == "tool_result"
        ),
        None,
    )
    assert block is not None and not block.get("is_error")
    parts = block["content"]
    return json.loads(parts[0]["text"] if isinstance(parts, list) else parts), parts


@pytest.mark.parametrize("next_action", ["text", "tool"])
def test_changed_pixels_do_not_discard_consumed_response_or_restart_owner(tmp_path, monkeypatch, next_action):
    from test_native_inspect import _author_page
    workspace, artifacts, context, arguments, tools, committed = _fixture(tmp_path, monkeypatch, "inspect")
    events, checkpoints = [], []

    def update_during_request(_request):
        _author_page(workspace, artifacts, 1, "<html><body>new version</body></html>")
        artifacts.render_pages([1], _renderer)
        return _call("inspect", arguments, "new-version") if next_action == "tool" else _response()

    outcomes = [_call("inspect", arguments), update_during_request]
    if next_action == "text":
        outcomes.append(_call("inspect", arguments, "new-version"))
    outcomes.append(_response())
    transport = ScriptedTransport(outcomes)
    result = _run(transport, tools, context, checkpoint_sink=checkpoints.append, event_sink=events.append)
    assert result.agent_id == context.owner_id
    assert workspace.page(1).lifecycle == PageLifecycle.INSPECTED
    assert len(committed) == 1
    assert sum(event["event_type"] == "inspection_invalidated" for event in events) == 1
    assert all(tool_boundary(checkpoint.messages).is_closed for checkpoint in checkpoints)
    calls = [block["id"] for message in result.checkpoint.messages if message["role"] == "assistant"
             and isinstance(message["content"], list) for block in message["content"] if block["type"] == "tool_use"]
    assert calls == ["check-1", "new-version"]
    assert "inspection_invalidated" in str(transport.requests[-1].messages)


def test_invalid_inspection_receipt_is_not_silently_recoverable(tmp_path, monkeypatch):
    from state.artifacts import ArtifactStateError
    workspace, artifacts, context, arguments, tools, _ = _fixture(tmp_path, monkeypatch, "inspect")
    def reject(*args):
        raise ArtifactStateError("invalid resource records", diagnostics={"code": "render_resources_invalid"})
    def corrupt(_request):
        monkeypatch.setattr(artifacts, "validate_render_receipt", reject)
        return _response()
    with pytest.raises(ArtifactStateError, match="invalid resource"):
        _run(ScriptedTransport([_call("inspect", arguments), corrupt]), tools, context)


def test_uncommitted_image_blocks_receipt_without_killing_owner(tmp_path, monkeypatch):
    from state.artifacts import ArtifactStateError
    workspace, artifacts, context, arguments, tools, committed = _fixture(
        tmp_path, monkeypatch, "inspect"
    )
    events = []

    def reject(*_args):
        raise ArtifactStateError(
            "page 1 image registrations invalid",
            diagnostics={
                "code": "image_registration_invalid",
                "registration": {
                    "unregistered": [],
                    "uncommitted": ["assets/generated.png"],
                    "stale": [],
                },
            },
        )

    def corrupt(_request):
        monkeypatch.setattr(artifacts, "validate_render_receipt", reject)
        return _response()

    transport = ScriptedTransport([
        _call("inspect", arguments),
        corrupt,
        _response(),
    ])
    result = _run(transport, tools, context, event_sink=events.append)

    assert result.final_text == "已检查"
    assert committed == []
    assert sum(
        event["event_type"] == "inspection_commit_blocked" for event in events
    ) == 1
    prompt = str(transport.requests[-1].messages)
    assert "inspection_commit_blocked" in prompt
    assert "image_registration_invalid" in prompt
    assert "assets/generated.png" in prompt
    assert "不要重启整个 Owner" in prompt


@pytest.mark.parametrize("review_group", [True, False])
def test_owner_runner_reminds_group_and_hands_back_only_actual_gap(tmp_path, monkeypatch, review_group):
    from PIL import Image
    from geometry_fixture import geometry
    from orchestration.manager import TaskRecord, TaskStatus
    from owner.context import OwnerContextStore
    from runtime.config import RunSpec
    from runtime.skill_runtime import SkillRuntime

    # 此项只验 Owner 接线和看图事务；真实 CSS/联系表像素由 native_inspect 覆盖。
    monkeypatch.setattr(WorkspaceState, "require_geometry", lambda _: geometry(width_px=1600, height_px=900))

    def contact_sheet(workspace, pages, name):
        path = workspace.paths.renders / name
        Image.new("RGB", (800, 500), "navy").save(path)
        return path

    monkeypatch.setattr("infer._write_group_contact_sheet", contact_sheet)
    workspace, artifacts, context, arguments, tools, committed = _fixture(tmp_path, monkeypatch, "group")
    owners = OwnerRegistry(workspace.paths.root, deck_id=workspace.deck_id)
    owner = owners.get(context.owner_id)
    services = SimpleNamespace(workspace=workspace, artifacts=artifacts, owners=owners,
        owner_contexts=OwnerContextStore(workspace.paths.root),
        run_spec=RunSpec.from_inputs(raw_user_query="两页连续图解，共同校准后交回。",
            output_workspace=workspace.paths.root, language="en", environment={"MODEL": "fixture"}),
        capture_training_segment=lambda *args, **kwargs: None)
    task = TaskRecord(role="slide", owner_id=owner.owner_id, pages=owner.pages,
        task_id="pair", goal="共同校准连续图解。", attempt=owner.attempt,
        group_id=owner.group_id, dependencies=(), status=TaskStatus.RUNNING)
    runtime = SkillRuntime.__new__(SkillRuntime)
    runtime.prepared = SimpleNamespace(snapshot=SimpleNamespace(role_text=lambda _: "Group Owner"),
        settings=SimpleNamespace(owner_max_turns=8, max_turns=8, owner_max_tokens=128,
                                 thinking=False, reasoning_effort="low"))
    monkeypatch.setattr(runtime, "_owner_handoffs", lambda *_: {})
    monkeypatch.setattr(runtime, "_owner_artifact_hashes", lambda *_: {})
    monkeypatch.setattr(runtime, "_artifact_refs", lambda *_: ())
    monkeypatch.setattr(runtime, "_owner_tool_context", lambda *_: context)
    results = []
    monkeypatch.setattr(runtime, "_record_result", results.append)
    outcomes = [_response(), _call("inspect", arguments), _response()] if review_group else [_response(), _response()]
    transport = ScriptedTransport(outcomes)
    agent = SimpleNamespace(run=lambda **kwargs: _run(transport, tools, context, **kwargs))
    returned = runtime._owner_runner(services, agent, "slide")(task, None)

    assert returned["status"] == "completed"
    assert returned["agent_id"] == owner.owner_id
    assert returned["turns"] == (3 if review_group else 2)
    notice = json.loads(transport.requests[1].messages[-1]["content"])
    assert notice["harness_event"] == "group_review_gap" and notice["pages"] == [1, 2]
    assert ("group_review_gap" in returned) != review_group
    assert len(committed) == int(review_group)
    assert bool(workspace.group_inspection_receipt(owner.owner_id, [1, 2])) == review_group
    stored = services.owner_contexts.load(owner.owner_id)
    assert stored.segment == results[0].checkpoint
    assert all(tool_boundary(segment.messages).is_closed for segment in results[0].segments)
    assert all(event["tool_name"] == "inspect" for event in tools.events)


@pytest.mark.parametrize("name", ["inspect", "group", "path"])
def test_real_inspection_is_committed_only_after_attached_pixels_are_consumed(tmp_path, monkeypatch, name):
    workspace, _, context, arguments, tools, committed = _fixture(tmp_path, monkeypatch, name)
    def consume(request):
        payload, parts = _payload_and_parts(request)
        assert [part["type"] for part in parts] == ["text", "image"]
        descriptor = payload["pending_inspection"]
        for omitted in (
            "critic_question",
            "owner_question",
            "critic_prompt_version",
            "render_committed",
            "geometry",
            "render_diagnostics",
            "inspect_timings_s",
            "opened_visual_sources",
            "opened_not_verified",
        ):
            assert omitted not in payload
            assert omitted not in descriptor
        assert payload["visual_source"]["path"].startswith("renders/")
        assert workspace.inspection_receipt(descriptor["scope_key"], descriptor["members"]) is None
        assert committed == []
        return _response()

    result = _run(ScriptedTransport([_call("inspect", arguments), consume]), tools, context)
    assert len(committed) == 1
    descriptor = committed[0]
    assert workspace.inspection_receipt(descriptor["scope_key"], descriptor["members"])
    assert workspace.page(1).lifecycle == PageLifecycle.INSPECTED
    assert tool_boundary(result.checkpoint.messages).is_closed


def test_consumed_inspect_pixels_roll_to_a_clean_training_segment(tmp_path, monkeypatch):
    from runtime.vision import image_blocks

    _, _, context, arguments, tools, committed = _fixture(tmp_path, monkeypatch, "inspect")
    events, checkpoints = [], []
    transport = ScriptedTransport([
        _call("inspect", arguments, "inspect-first"),
        _call("inspect", arguments, "inspect-cached"),
        _response(),
    ])
    result = _run(
        transport,
        tools,
        context,
        event_sink=events.append,
        checkpoint_sink=checkpoints.append,
    )

    def image_count(messages):
        return sum(
            1
            for message in messages
            for _ in image_blocks(message.get("content"))
        )

    assert image_count(transport.requests[1].messages) == 1
    assert image_count(transport.requests[2].messages) == 0
    assert len(result.segments) == 2
    assert image_count(result.segments[0].messages) == 1
    assert image_count(result.segments[1].messages) == 0
    assert result.segments[1].predecessor_segment_id == result.segments[0].segment_id
    assert result.segments[1].reason == "visual_context_release"
    assert len(committed) == 1
    released = [event for event in events if event["event_type"] == "agent_visual_context_released"]
    assert len(released) == 1 and released[0]["images_released"] == 1


@pytest.mark.parametrize("name", ["inspect", "group", "path"])
def test_failed_pixel_consumption_resumes_once_from_closed_checkpoint(tmp_path, monkeypatch, name):
    workspace, _, context, arguments, tools, committed = _fixture(tmp_path, monkeypatch, name)
    checkpoints = []
    failed = ScriptedTransport([_call("inspect", arguments), ValueError("failed image consumption")])
    with pytest.raises(AgentProviderError):
        _run(failed, tools, context, checkpoint_sink=checkpoints.append)
    payload, _ = _payload_and_parts(failed.requests[-1])
    descriptor = payload["pending_inspection"]
    assert committed == []
    assert workspace.inspection_receipt(descriptor["scope_key"], descriptor["members"]) is None
    assert tool_boundary(checkpoints[-1].messages).is_closed

    resumed = WorkspaceState(workspace.paths.root)
    resumed_tools, commits = _tools(resumed, ArtifactStore(resumed.paths.root, workspace_state=resumed))
    transport = ScriptedTransport([_response()])
    result = _run(transport, resumed_tools, context, checkpoint=checkpoints[-1])
    _, parts = _payload_and_parts(transport.requests[0])
    assert any(part["type"] == "image" for part in parts)
    assert commits == [descriptor]
    assert len(resumed_tools.events) == 0
    assert resumed.inspection_receipt(descriptor["scope_key"], descriptor["members"])

    _run(ScriptedTransport([_response()]), resumed_tools, context, checkpoint=result.checkpoint)
    assert commits == [descriptor]


@pytest.mark.parametrize("name", ["inspect", "group", "path"])
def test_same_scope_has_no_image_but_new_purpose_attaches_current_pixels(tmp_path, monkeypatch, name):
    _, _, context, arguments, tools, committed = _fixture(tmp_path, monkeypatch, name)
    _run(ScriptedTransport([_call("inspect", arguments), _response()]), tools, context)
    transport = ScriptedTransport([_call("inspect", arguments, "cached"),
        _call("inspect", {**arguments, "question": "新目的：只核查标签对比"}, "new-scope"), _response()])
    _run(transport, tools, context)
    cached, parts = _payload_and_parts(transport.requests[1])
    assert cached["inspection_cached"] and isinstance(parts, str)
    fresh, parts = _payload_and_parts(transport.requests[2])
    assert not fresh["inspection_cached"] and any(part["type"] == "image" for part in parts)
    assert cached["scope_key"] != fresh["scope_key"]
    assert len(committed) == 2


def test_orchestrator_observation_preserves_authoring_receipt_and_critic_settlement(tmp_path, monkeypatch):
    from runtime.skill_runtime import current_critic_state
    workspace, _, context, arguments, tools, _ = _fixture(tmp_path, monkeypatch, "inspect")
    _run(ScriptedTransport([_call("inspect", arguments), _response()]), tools, context)
    workspace.mark_ready(1)
    authoring = workspace.page(1)
    records = [{**authoring.to_dict(), "agent_id": context.owner_id,
        "completed": True, "body": "当前像素文字清楚。"}]
    assert not current_critic_state(records, [authoring])["critic_incomplete"]
    orchestrator = ToolContext("orchestrator", "orchestrator", read_scopes=(WriteScope("renders"),), metadata=context.metadata)
    transport = ScriptedTransport([_call("inspect", {"path": "renders/slide_01.png",
        "question": "封面与全册的一致性"}, "observe-1"), _response()])
    _run(transport, tools, orchestrator)
    payload, parts = _payload_and_parts(transport.requests[1])
    assert payload["pending_inspection"]["role"] == "orchestrator"
    assert any(part["type"] == "image" for part in parts)
    observed = workspace.page(1)
    assert observed.inspection_id == authoring.inspection_id
    assert observed.lifecycle == PageLifecycle.READY
    assert not current_critic_state(records, [observed])["critic_incomplete"]
    assert workspace._read_state()["events"][-1]["type"] == "page_observed"


@pytest.mark.parametrize("name", ["inspect", "group", "path"])
def test_crash_after_receipt_before_assistant_checkpoint_does_not_duplicate_events(tmp_path, monkeypatch, name):
    workspace, _, context, arguments, tools, committed = _fixture(tmp_path, monkeypatch, name)
    checkpoints, events = [], []

    def crash_after_public_event(event):
        events.append(event)
        if event["event_type"] == "inspection_committed":
            raise RuntimeError("crash before consumed assistant checkpoint")

    with pytest.raises(RuntimeError, match="crash before"):
        _run(ScriptedTransport([_call("inspect", arguments), _response()]), tools, context,
            checkpoint_sink=checkpoints.append, event_sink=crash_after_public_event)
    assert len(committed) == 1
    before_events = copy.deepcopy(workspace._read_state()["events"])
    resumed = WorkspaceState(workspace.paths.root)
    resumed_tools, _ = _tools(resumed, ArtifactStore(resumed.paths.root, workspace_state=resumed))
    _run(ScriptedTransport([_response()]), resumed_tools, context,
        checkpoint=checkpoints[-1], event_sink=events.append)
    assert resumed._read_state()["events"] == before_events
    assert sum(event["event_type"] == "inspection_committed" for event in events) == 1
