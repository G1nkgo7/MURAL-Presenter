from dataclasses import replace
import json
from types import SimpleNamespace

import pytest
from geometry_fixture import geometry, write_geometry

from orchestration.manager import TaskRecord, TaskStatus
from owner.context import OwnerContextStore, OwnerPacket, ResumeMode
from owner.registry import OwnerRegistry, OwnerStatus
from runtime.config import RunSpec
from runtime.recovery import SegmentCheckpoint, tool_boundary
from runtime.skill_runtime import SkillRuntime
from runtime.tools import ToolContext
from state.artifacts import ArtifactStore
from state.workspace import WorkspaceState, file_sha256


def continuation(tmp_path, monkeypatch, *, pages=(1,)):
    write_geometry(tmp_path, geometry(width_px=1600, height_px=900))
    workspace = WorkspaceState(tmp_path)
    workspace.register_pages([1, 2])
    for page in (1, 2):
        workspace.paths.page_html(page).write_text(f'<section data-slide="{page:02d}"></section>\n', encoding="utf-8")
    spec = RunSpec.from_inputs(raw_user_query="Initial deck", output_workspace=tmp_path)
    owners = OwnerRegistry(tmp_path, deck_id=workspace.deck_id)
    owner = owners.accept(role="slide", pages=pages, group_id="one")
    contexts = OwnerContextStore(tmp_path)
    packet = OwnerPacket.compile(owner, spec, task_id="initial", goal="Make page one")
    segment = SegmentCheckpoint.initial([
        {"role": "user", "content": "Original page request"},
        {"role": "assistant", "content": "Observed actual pixels: wrapper needs height."},
    ], segment_id="previous-segment")
    initial_hashes = {f"slides/slide_{page:02d}.html": file_sha256(workspace.paths.page_html(page))
                      for page in owner.pages}
    initial_hashes["config.json"] = file_sha256(tmp_path / "config.json")
    initial_hashes["base.css"] = file_sha256(tmp_path / "base.css")
    contexts.save(owner=owner, packet=packet, segment=segment, artifact_hashes=initial_hashes, capsule="Prior observations")
    owners.transition(owner.owner_id, attempt=1, status=OwnerStatus.RUNNING)
    owners.transition(owner.owner_id, attempt=1, status=OwnerStatus.COMPLETED)
    services = SimpleNamespace(workspace=workspace, owners=owners, owner_contexts=contexts, run_spec=spec,
        artifacts=ArtifactStore(tmp_path, workspace_state=workspace), capture_training_segment=lambda *_a, **_k: None,
        orchestration=SimpleNamespace(snapshot=lambda: {"tasks": {}}, task=lambda _id: None))
    runtime = object.__new__(SkillRuntime)
    runtime.prepared = SimpleNamespace(snapshot=SimpleNamespace(role_text=lambda _role: "Owner role"),
        settings=SimpleNamespace(owner_max_turns=10, max_turns=10, owner_max_tokens=256,
                                 thinking=False, reasoning_effort="low"))
    monkeypatch.setattr(runtime, "_owner_tool_context", lambda _s, task: ToolContext(task.owner_id, task.role))
    monkeypatch.setattr(runtime, "_record_result", lambda _result: None)
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        checkpoint = kwargs["checkpoint"] or SegmentCheckpoint.initial(kwargs["initial_messages"])
        return SimpleNamespace(checkpoint=checkpoint, final_text="Finished", review_handoff=None, to_dict=lambda: {})

    return runtime, services, SimpleNamespace(run=run), calls, owner, segment


@pytest.mark.parametrize("reason", ["", "provider_failover", "context_compaction"])
def test_same_owner_new_attempt_keeps_closed_history_and_appends_current_packet(tmp_path, monkeypatch, reason):
    runtime, services, agent, calls, old, segment = continuation(tmp_path, monkeypatch)
    stored = services.owner_contexts.load(old.owner_id)
    segment = replace(segment, reason=reason, excluded_channel_ids=("failed-channel",))
    services.owner_contexts.save(owner=old, packet=stored.packet, segment=segment, artifact_hashes=stored.artifact_hashes)
    owner = services.owners.accept(role="slide", pages=[1], group_id="one")
    task = TaskRecord("repair", owner.owner_id, owner.attempt, "slide", (1,), "one", (),
                      TaskStatus.RUNNING, goal="Set wrapper height; keep the observed crop", repair=True)
    result = runtime._owner_runner(services, agent, "slide")(task, None)
    assert result["resume_mode"] == ResumeMode.RESUME.value
    resumed = calls[0]["checkpoint"]
    assert resumed.messages[:len(segment.messages)] == segment.messages
    assert resumed.segment_id == segment.segment_id and resumed.excluded_channel_ids == ("failed-channel",)
    assert resumed.reason == (reason or "resume") and tool_boundary(resumed.messages).is_closed
    current = json.loads(resumed.messages[-1]["content"])["owner_packet"]
    assert current["task_id"] == "repair" and current["goal"] == task.goal and current["attempt"] == 2
    assert calls[0]["initial_messages"] == ()


def test_group_local_repair_resumes_original_pixel_observations(tmp_path, monkeypatch):
    from orchestration.manager import OrchestrationManager
    from test_orchestration_manager import drain
    runtime, services, agent, calls, original, segment = continuation(tmp_path, monkeypatch, pages=(1, 2))
    manager = OrchestrationManager(tmp_path, deck_id=services.workspace.deck_id, registry=services.owners)
    services.orchestration = manager
    manager.register_runner("slide", runtime._owner_runner(services, agent, "slide"))
    untouched = services.workspace.paths.page_html(2).read_bytes()
    try:
        manager.submit_tasks([{"task_id": "repair-p1", "role": "slide", "pages": [1, 2],
            "group_id": "one", "repair": True, "goal": "Repair P1 wrapper; P2 must not change."}])
        drain(manager)
        task = manager.task("repair-p1")
        assert task.owner_id == original.owner_id and task.attempt == 2
        assert task.result["resume_mode"] == ResumeMode.RESUME.value
        checkpoint = calls[0]["checkpoint"]
        assert checkpoint.messages[:len(segment.messages)] == segment.messages
        packet = json.loads(checkpoint.messages[-1]["content"])["owner_packet"]
        assert packet["pages"] == [1, 2] and packet["goal"] == task.goal
        assert services.workspace.paths.page_html(2).read_bytes() == untouched
    finally:
        manager.close()


@pytest.mark.parametrize("path", ["deck.md", "base.css", "knowledge.md", "assets/catalog.md", "briefs/slide_01.md", "config.json"])
def test_changed_shared_input_retains_history_with_explicit_changed_paths(tmp_path, monkeypatch, path):
    runtime, services, agent, calls, old, _segment = continuation(tmp_path, monkeypatch)
    monkeypatch.setattr(runtime, "_owner_handoffs", lambda *_: {})
    (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / path).write_text(json.dumps({**services.workspace.creation_config(), "working_language": "zh"})
                               if path == "config.json" else "New input")
    owner = services.owners.accept(role="slide", pages=[1], group_id="one")
    task = TaskRecord("repair", owner.owner_id, owner.attempt, "slide", (1,), "one", (), TaskStatus.RUNNING)
    result = runtime._owner_runner(services, agent, "slide")(task, None)
    assert result["resume_mode"] == ResumeMode.RESUME.value
    checkpoint = calls[0]["checkpoint"]
    assert checkpoint is not None and checkpoint.predecessor_segment_id == _segment.segment_id
    assert checkpoint.segment_id != _segment.segment_id and checkpoint.reason == "owner_inputs_updated"
    assert not checkpoint.provider_affinity_id and not checkpoint.excluded_channel_ids
    assert calls[0]["initial_messages"] == () and checkpoint.messages[:-1] == _segment.messages
    assert tool_boundary(checkpoint.messages).is_closed
    packet = json.loads(checkpoint.messages[-1]["content"])["owner_packet"]
    assert packet["handoffs"]["changed_inputs"] == [path]
    assert packet["prior_capsule"] == ""
    if path == "config.json":
        assert packet["creation_config"]["working_language"] == "zh"


def test_changed_owner_members_do_not_reuse_previous_page_history(tmp_path, monkeypatch):
    _runtime, services, _agent, _calls, old, _segment = continuation(tmp_path, monkeypatch)
    assert services.owner_contexts.resume_mode(replace(old, pages=(2,))) == ResumeMode.COLD


def test_required_context_rebuild_carries_versioned_visual_feedback(tmp_path, monkeypatch):
    runtime, services, agent, calls, old, segment = continuation(tmp_path, monkeypatch)
    stored = services.owner_contexts.load(old.owner_id)
    feedback = {"body": "Figure is too small; numbers overlap", "completed": True}
    payload = {"pending_inspection": {"path": "renders/slide_01.png", "members": [
        {"page": 1, "page_uid": "old-page", "png_sha256": "old-pixels"},
        {"page": 2, "page_uid": "other", "png_sha256": "other-pixels"}]}, "critic_feedback": feedback}
    history = (*segment.messages,
        {"role": "assistant", "content": [{"type": "tool_use", "id": "inspect-old", "name": "inspect", "input": {"page": 1}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "inspect-old", "content": [
            {"type": "text", "text": json.dumps(payload)}, {"type": "image", "source": {"type": "omitted_base64"}}]}]})
    services.owner_contexts.save(owner=old, packet=stored.packet, segment=replace(segment, messages=history),
                                artifact_hashes=stored.artifact_hashes, capsule="")
    owner = services.owners.accept(role="slide", pages=[1], group_id="one")
    task = TaskRecord("repair", owner.owner_id, owner.attempt, "slide", (1,), "one", (), TaskStatus.RUNNING)
    result = runtime._owner_runner(services, agent, "slide")(task, None)
    assert result["resume_mode"] == ResumeMode.REBUILD.value
    packet = json.loads(calls[0]["checkpoint"].messages[0]["content"])["owner_packet"]
    observations = packet["handoffs"]["prior_visual_observations"]
    assert len(observations) == 1 and observations[0]["version"]["png_sha256"] == "old-pixels"
    assert observations[0]["feedback"] == feedback
    assert packet["handoffs"]["previous_goal"] == stored.packet.goal
    assert calls[0]["checkpoint"].predecessor_segment_id == segment.segment_id


@pytest.mark.parametrize("image_count", [4, 5])
def test_owner_resume_rebuilds_only_when_stored_images_exceed_active_budget(tmp_path, monkeypatch, image_count):
    import base64
    from runtime.vision import image_blocks

    runtime, services, agent, calls, old, segment = continuation(tmp_path, monkeypatch)
    runtime.prepared.settings.vision_context_images = 4
    stored = services.owner_contexts.load(old.owner_id)
    images = [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
        "data": base64.b64encode(f"original-pixels-{index}".encode()).decode()}}
        for index in range(image_count)]
    previous = replace(segment, messages=(*segment.messages,
        {"role": "assistant", "content": [{"type": "tool_use", "id": "old-inspect", "name": "inspect", "input": {"page": 1}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "old-inspect", "content": images}]}))
    services.owner_contexts.save(owner=old, packet=stored.packet, segment=previous,
        artifact_hashes=stored.artifact_hashes, capsule="Prior observations")
    blobs = {path: path.read_bytes() for path in (tmp_path / "_trace/raw/blobs").iterdir()}
    html_before = services.workspace.paths.page_html(1).read_bytes()
    assert services.owner_contexts.resume_mode(old) == ResumeMode.RESUME
    owner = services.owners.accept(role="slide", pages=[1], group_id="one")
    task = TaskRecord("repair-image-budget", owner.owner_id, owner.attempt, "slide", (1,), "one", (),
        TaskStatus.RUNNING, goal="Repair current page using existing artifacts", repair=True)

    result = runtime._owner_runner(services, agent, "slide")(task, None)
    resumed = calls[0]["checkpoint"]
    assert tool_boundary(resumed.messages).is_closed
    assert calls[0]["agent_id"] == old.owner_id
    if image_count > 4:
        assert result["resume_mode"] == ResumeMode.REBUILD.value
        assert resumed.predecessor_segment_id == previous.segment_id
        assert resumed.reason == "artifact_context_rebuild" and len(resumed.messages) == 1
        assert not any(image_blocks(resumed.messages[0]["content"]))
        packet = json.loads(resumed.messages[0]["content"])["owner_packet"]
        assert packet["prior_capsule"] == "Prior observations"
        assert packet["handoffs"]["previous_goal"] == stored.packet.goal
    else:
        assert result["resume_mode"] == ResumeMode.RESUME.value
        assert resumed.messages[:len(previous.messages)] == previous.messages
    assert services.workspace.paths.page_html(1).read_bytes() == html_before
    assert all(path.read_bytes() == payload for path, payload in blobs.items())


def test_explicit_new_executor_receives_handoff_without_relabeling_old_history(tmp_path, monkeypatch):
    runtime, services, agent, calls, old, segment = continuation(tmp_path, monkeypatch)
    old_state = services.owner_contexts.path_for(old.owner_id).read_bytes()
    fresh = services.owners.accept_many([{"role": "slide", "pages": [1], "group_id": "fresh",
                                         "handoff_from": old.owner_id}])[0]
    task = TaskRecord("fresh-task", fresh.owner_id, fresh.attempt, "slide", (1,), fresh.group_id, (),
                      TaskStatus.RUNNING, goal="Rework the evidence scale, preserve the data", handoff_from=old.owner_id)
    result = runtime._owner_runner(services, agent, "slide")(task, None)
    assert result["resume_mode"] == ResumeMode.COLD.value
    assert calls[0]["checkpoint"] is None and calls[0]["agent_id"] == fresh.owner_id
    packet = json.loads(calls[0]["initial_messages"][0]["content"])["owner_packet"]
    assert packet["handoffs"]["previous_summary"] == "Prior observations"
    assert packet["handoffs"]["previous_goal"] == "Make page one"
    assert packet["handoffs"]["handoff_from"] == old.owner_id
    assert services.owner_contexts.path_for(old.owner_id).read_bytes() == old_state
    saved = services.owner_contexts.load(fresh.owner_id)
    assert saved.segment.segment_id != segment.segment_id
    assert saved.segment.predecessor_segment_id is None
    assert saved.packet.role == "slide" and saved.packet.owner_id == fresh.owner_id


def test_config_rebuild_records_current_input_and_training_predecessor(tmp_path, monkeypatch):
    from pathlib import Path
    from export.training import TrainingExporter
    from qc.routing import IssueLedger
    from runtime.agent import AgentRuntime
    from runtime.live_trace import LiveEventJournal
    from runtime.session import ProviderChannel
    from runtime.tools import ToolRuntime
    from test_agent_runtime import ScriptedTransport, _definition, _response, _settings, _text, _tool

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "curation"))
    from adapters.clean_harness import _build_v2_segment_trajectory, _segment_context_actions

    runtime, services, _agent, _calls, old, previous = continuation(tmp_path, monkeypatch)
    journal = LiveEventJournal(tmp_path / "_trace/live-events.jsonl")
    journal.append(event_type="agent_context", agent_id=old.owner_id, role="slide",
        segment_id=previous.segment_id,
        payload={"system": "Owner role", "tools": [], "model": "model-test", "messages": previous.messages[:1]})
    exporter = TrainingExporter(services.workspace, services.artifacts, IssueLedger(tmp_path), run_mode="synthesis")
    prior = exporter.capture(old.owner_id, "slide", previous)
    prior_row, errors = exporter._export_segment(prior)
    assert not errors
    prior_path = tmp_path / prior_row["checkpoint_path"]
    prior_bytes = prior_path.read_bytes()

    (tmp_path / "config.json").write_text(json.dumps({
        **services.workspace.creation_config(), "working_language": "zh"}), encoding="utf-8")
    owner = services.owners.accept(role="slide", pages=[1], group_id="one")
    task = TaskRecord("current-language", owner.owner_id, owner.attempt, "slide", (1,), "one", (),
        TaskStatus.RUNNING, goal="保持页面内容，按当前协作语言核对修改目标。", repair=True)
    tools = ToolRuntime(event_sink=journal.record_tool_event)
    tools.register(_definition("diagnose", lambda _ctx, _args: {"current": "language confirmed"}))
    transport = ScriptedTransport([
        _response(_tool("current-config-check", "diagnose", {}), stop_reason="tool_use"),
        _response(_text("Current configuration checked")),
    ])
    agent = AgentRuntime(settings=_settings(), channels=(ProviderChannel("fake-key", "ch-test"),),
        transport=transport, tools=tools, event_sink=journal.record_runtime_event)
    services.capture_training_segment = exporter.capture
    result = runtime._owner_runner(services, agent, "slide")(task, None)
    assert result["resume_mode"] == ResumeMode.RESUME.value
    initial = transport.calls[0][1].messages
    assert initial[:-1] == previous.messages
    packet = json.loads(initial[-1]["content"])["owner_packet"]
    assert packet["owner_id"] == old.owner_id
    assert packet["creation_config"]["working_language"] == "zh"
    assert packet["handoffs"]["changed_inputs"] == ["config.json"]
    assert "Observed actual pixels: wrapper needs height." in str(initial)

    segments = exporter.load_segments()
    assert len(segments) == 2
    rebuilt = next(row for row in segments if row.checkpoint.segment_id != previous.segment_id)
    assert rebuilt.owner_id == old.owner_id
    assert rebuilt.checkpoint.predecessor_segment_id == previous.segment_id
    assert tool_boundary(rebuilt.checkpoint.messages).is_closed
    row, errors = exporter._export_segment(rebuilt)
    assert not errors
    frozen = json.loads((tmp_path / row["checkpoint_path"]).read_text())
    assert frozen["agent_context"][0]["messages"] == list(initial)
    converter = SimpleNamespace(SCHEMA_VERSION="test", convert_tools=lambda values: values)
    trajectory, _ = _build_v2_segment_trajectory(run_dir=tmp_path, segment=row, deck_sample_id="deck",
        batch="test", converter=converter, modality="general")
    masks = _segment_context_actions(trajectory)
    assert masks["context_action_ids"]
    assert len(masks["current_action_ids"]) == 2
    current = [trajectory["messages"][index] for index in masks["current_openai_message_indices"]]
    assert current[0]["tool_calls"][0]["id"] == "current-config-check"
    assert current[-1]["content"] == "Current configuration checked"
    assert prior_path.read_bytes() == prior_bytes


def test_same_task_resume_does_not_append_the_same_packet_twice(tmp_path, monkeypatch):
    runtime, services, agent, calls, old, _segment = continuation(tmp_path, monkeypatch)
    task = TaskRecord("initial", old.owner_id, old.attempt, "slide", (1,), "one", (), TaskStatus.RUNNING, goal="Make page one")
    runner = runtime._owner_runner(services, agent, "slide")
    runner(task, None)
    first = calls[0]["checkpoint"].messages
    runner(task, None)
    assert calls[1]["checkpoint"].messages == first


def test_owner_packet_carries_specific_prior_failed_task_and_tool_result(tmp_path, monkeypatch):
    runtime, services, agent, calls, old, _segment = continuation(tmp_path, monkeypatch)
    stored = services.owner_contexts.load(old.owner_id)
    segment = SegmentCheckpoint.initial([
        {"role": "assistant", "content": [{"type": "tool_use", "id": "failed-render", "name": "terminal", "input": {"command": "render . --page 1"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "failed-render", "is_error": True,
           "error_code": "render_source_uncommitted", "content": "Use write_file or patch on slides/slide_01.html; source is still scaffold."}]},
    ])
    services.owner_contexts.save(owner=old, packet=stored.packet, segment=segment, artifact_hashes=stored.artifact_hashes)
    prior = TaskRecord("initial", old.owner_id, 1, "slide", (1,), "one", (), TaskStatus.FAILED,
                       error="RepeatedToolFailureError: render_source_uncommitted")
    services.orchestration = SimpleNamespace(snapshot=lambda: {"tasks": {"initial": prior.to_dict()}}, task=lambda _id: prior)
    owner = services.owners.accept(role="slide", pages=[1], group_id="one")
    task = TaskRecord("repair", owner.owner_id, owner.attempt, "slide", (1,), "one", (), TaskStatus.RUNNING)
    runtime._owner_runner(services, agent, "slide")(task, None)
    packet = json.loads(calls[0]["checkpoint"].messages[-1]["content"])["owner_packet"]
    assert packet["handoffs"]["previous_failure"]["task_error"] == prior.error
    assert "tool_use_id" not in packet["handoffs"]["previous_failure"]["tool_results"][0]
    assert "slides/slide_01.html" in packet["handoffs"]["previous_failure"]["tool_results"][0]["content"]


def test_budget_compacted_resume_preserves_training_parent_and_masks_old_actions(tmp_path, monkeypatch):
    from pathlib import Path
    from export.training import TrainingExporter
    from qc.routing import IssueLedger
    from runtime.agent import AgentRuntime
    from runtime.live_trace import LiveEventJournal
    from runtime.session import ProviderChannel
    from runtime.tools import ToolRuntime
    from runtime.recovery import compact_segment
    from test_agent_runtime import ScriptedTransport, _definition, _response, _settings, _text, _tool
    from test_visual_context import _history

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "curation"))
    from adapters.clean_harness import _build_v2_segment_trajectory, _segment_context_actions
    from quality.review_intervention import build_action_catalog, _apply_segment_context_policy

    runtime, services, _agent, _calls, old, _segment = continuation(tmp_path, monkeypatch)
    stored = services.owner_contexts.load(old.owner_id)
    messages = [{"role": "user", "content": "Original request"}]
    for index in range(34):
        messages.extend([{"role": "assistant", "content": f"Prior observation {index}"},
                         {"role": "user", "content": f"Follow-up {index}"}])
    messages.extend(_history(21)[1:])
    full = SegmentCheckpoint.initial(messages, segment_id="full-unsigned-history")
    journal = LiveEventJournal(tmp_path / "_trace/live-events.jsonl")
    journal.append(event_type="agent_context", agent_id=old.owner_id, role="slide", segment_id=full.segment_id,
                   payload={"system": "Owner role", "tools": [], "model": "model-test", "messages": messages[:1]})
    exporter = TrainingExporter(services.workspace, services.artifacts, IssueLedger(tmp_path), run_mode="synthesis")
    previous = exporter.capture(old.owner_id, "slide", full)
    original_row, _ = exporter._export_segment(previous)
    original_path = tmp_path / original_row["checkpoint_path"]
    original_bytes = original_path.read_bytes()
    saved = services.owner_contexts.save(owner=old, packet=stored.packet, segment=full, artifact_hashes=stored.artifact_hashes)
    assert saved.segment == full and len(saved.messages) > 64
    from test_agent_runtime import _summary_invoker
    budget_runtime = AgentRuntime(summary_invoker=_summary_invoker, settings=_settings(), channels=(ProviderChannel("fake-key", "ch-test"),),
                                  transport=ScriptedTransport([]), tools=ToolRuntime())
    next_messages, metrics = budget_runtime._compact_messages(full.messages, system="Owner role", tools=(),
        max_tokens=256, agent_id=old.owner_id, role="slide")
    assert metrics["pressure"] == ["images"]
    next_segment = replace(compact_segment(full, summary="Unified compaction", reason=metrics["reason"]), messages=tuple(next_messages))
    compacted = services.owner_contexts.save(owner=old, packet=stored.packet, segment=next_segment, artifact_hashes=stored.artifact_hashes)
    assert compacted.segment.segment_id != full.segment_id
    assert compacted.segment.predecessor_segment_id == full.segment_id

    owner = services.owners.accept(role="slide", pages=[1], group_id="one")
    task = TaskRecord("continue", owner.owner_id, owner.attempt, "slide", (1,), "one", (),
                      TaskStatus.RUNNING, goal="Apply the new diagnostic", repair=True)
    tools = ToolRuntime(event_sink=journal.record_tool_event)
    tools.register(_definition("diagnose", lambda _ctx, _args: {"new_evidence": "Parent bbox now measured"}))
    transport = ScriptedTransport([
        _response(_tool("new-diagnostic", "diagnose", {}), stop_reason="tool_use"),
        _response(_text("New corrective conclusion")),
    ])
    agent = AgentRuntime(settings=_settings(), channels=(ProviderChannel("fake-key", "ch-test"),),
                         transport=transport, tools=tools, event_sink=journal.record_runtime_event)
    services.capture_training_segment = exporter.capture
    result = runtime._owner_runner(services, agent, "slide")(task, None)
    assert result["resume_mode"] == "resume"
    assert original_path.read_bytes() == original_bytes
    segments = exporter.load_segments()
    assert len(segments) == 3
    assert list(segments[0].checkpoint.messages) == json.loads(original_bytes)["messages"]
    resumed, visual_release = segments[1:]
    assert resumed.checkpoint.predecessor_segment_id == full.segment_id
    assert resumed.checkpoint.segment_id == compacted.segment.segment_id
    assert visual_release.checkpoint.predecessor_segment_id == resumed.checkpoint.segment_id
    assert visual_release.checkpoint.reason == "visual_context_release"
    resumed_row, errors = exporter._export_segment(resumed)
    assert not errors
    frozen = json.loads((tmp_path / resumed_row["checkpoint_path"]).read_text())
    initial = frozen["agent_context"][0]["messages"]
    assert len(initial) == len(compacted.messages) + 1
    assert json.loads(initial[-1]["content"])["owner_packet"]["attempt"] == 2
    converter = SimpleNamespace(SCHEMA_VERSION="test", convert_tools=lambda values: values)
    current = []
    current_action_ids = []
    for segment in (resumed, visual_release):
        row, errors = exporter._export_segment(segment)
        assert not errors
        trajectory, _ = _build_v2_segment_trajectory(
            run_dir=tmp_path, segment=row, deck_sample_id="deck",
            batch="test", converter=converter, modality="general",
        )
        masks = _segment_context_actions(trajectory)
        assert len(masks["context_action_ids"]) > 20
        assert len(masks["current_action_ids"]) == 1
        current_action_ids.extend(masks["current_action_ids"])
        current.extend(
            trajectory["messages"][index]
            for index in masks["current_openai_message_indices"]
        )
        trajectory["metadata"]["segment_lineage"].update(masks)
        catalog = build_action_catalog(trajectory)
        judged = _apply_segment_context_policy({"actions": [
            {"action_id": action["action_id"], "verdict": "train"}
            for action in catalog["actions"]
        ]}, catalog)
        assert {
            action["action_id"] for action in judged["actions"]
            if action["verdict"] == "train"
        } == set(masks["current_action_ids"])
        assert {
            action["action_id"] for action in judged["actions"]
            if action["verdict"] == "context_only"
        } == set(masks["context_action_ids"])
    assert len(current_action_ids) == 2
    assert current[0]["tool_calls"][0]["id"] == "new-diagnostic"
    assert current[-1]["content"] == "New corrective conclusion"
    assert not any("Prior observation" in str(message) for message in current)
    assert original_path.read_bytes() == original_bytes
