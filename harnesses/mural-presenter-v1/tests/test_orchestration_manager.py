from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import pytest

from orchestration.manager import ORCHESTRATION_SCHEMA, OrchestrationError, OrchestrationManager, TaskRecord, TaskStatus
from owner.registry import OwnerRegistry, OwnerRegistryError, OwnerStatus
from runtime.tools import (
    STABLE_TOOL_SCHEMAS, ToolCall, ToolContext, ToolDefinition, ToolRuntime,
    replay_tool_transactions, validate_json_value,
)


def drain(manager, timeout=5):
    deadline = time.monotonic() + timeout
    events = []
    while time.monotonic() < deadline:
        update = manager.wait(timeout_s=max(0, deadline - time.monotonic()))
        assert update is not None, manager.snapshot()
        assert not update['pending_requests'], update
        events.extend(update['events'])
        if update['all_terminal']:
            return events
    pytest.fail(f'tasks did not terminate: {manager.snapshot()}')


@pytest.mark.parametrize("role,fields", [
    ("slide", {"pages": [3, 4]}), ("research", {"output_path": "research/custom-topic.md"}),
])
def test_continuation_resolves_original_scope_and_keeps_replay_idempotent(tmp_path, role, fields):
    manager = OrchestrationManager(tmp_path, deck_id="resolve-contract")
    calls = []
    manager.register_runner(role, lambda task, _: calls.append(task) or {})
    try:
        manager.submit_tasks([{"task_id": "first", "role": role, "group_id": "original", **fields}])
        drain(manager)
        first = manager.task("first")
        spec = {"task_id": "repair", "role": role, "group_id": "original", "goal": "Repair only the caption; keep all other content", "repair": True}
        manager.submit_tasks([spec])
        drain(manager)
        repaired = manager.task("repair")
        assert (repaired.owner_id, repaired.pages, repaired.output_path) == (first.owner_id, first.pages, first.output_path)
        replay = manager.submit_tasks([spec])
        assert replay["new_task_ids"] == [] and replay["reused_task_ids"] == ["repair"]
        assert len(calls) == 2
        conflict = {"pages": [3]} if role == "slide" else {"output_path": "research/other.md"}
        with pytest.raises(OrchestrationError, match="page_owner_conflict|original output_path"):
            manager.submit_tasks([{**spec, "task_id": "invalid", **conflict}])
        assert "invalid" not in manager.snapshot()["tasks"]
    finally:
        manager.close()


@pytest.mark.parametrize("group_id,pages", [("original", [1, 2]), ("new-group", [2])])
def test_unfinished_owner_rejection_explains_valid_continuation_without_accepting_tasks(tmp_path, monkeypatch, group_id, pages):
    manager = OrchestrationManager(tmp_path, deck_id="active-conflict")
    manager.register_runner("slide", lambda *_: {})
    monkeypatch.setattr(manager, "_dispatch_ready_locked", lambda: None)
    try:
        manager.submit_tasks([{"task_id": "current", "role": "slide", "group_id": "original", "pages": [1, 2]}])
        before = manager.snapshot()["tasks"]
        with pytest.raises(OrchestrationError) as caught:
            manager.submit_tasks([{"task_id": "rejected", "role": "slide", "group_id": group_id,
                                   "pages": pages, "repair": True}])
        feedback = caught.value.diagnostics
        assert feedback["submission_accepted"] is False
        assert feedback["current_tasks"] == [{
            "task_id": "current", "role": "slide", "group_id": "original", "pages": [1, 2], "status": "queued",
            "continuation_after_task_finishes": {"role": "slide", "group_id": "original", "repair": True, "pages": [1, 2]},
        }]
        assert "not valid now" in feedback["next_action"] and "naturally end" in feedback["next_action"]
        assert "delegate_wait" not in feedback["next_action"]
        assert manager.snapshot()["tasks"] == before
    finally:
        manager.close()


def test_overlap_only_in_submission_does_not_advise_waiting_for_a_nonexistent_owner(tmp_path):
    manager = OrchestrationManager(tmp_path, deck_id="submission-conflict")
    manager.register_runner("slide", lambda *_: {})
    try:
        with pytest.raises(OrchestrationError, match="within this submission") as caught:
            manager.submit_tasks([
                {"task_id": "a", "role": "slide", "group_id": "a", "pages": [1, 2]},
                {"task_id": "b", "role": "slide", "group_id": "b", "pages": [2, 3]},
            ])
        assert caught.value.diagnostics is None
        assert not manager.snapshot()["tasks"]
    finally:
        manager.close()


@pytest.mark.parametrize("role,fields", [
    ("slide", {"pages": [2, 3]}), ("research", {"output_path": "research/custom.md"}),
])
def test_explicit_handoff_can_reuse_scope_without_inventing_new_members(tmp_path, role, fields):
    from geometry_fixture import geometry, write_geometry
    from state.workspace import WorkspaceState
    write_geometry(tmp_path, geometry(width_px=1600, height_px=900))
    WorkspaceState(tmp_path)
    manager = OrchestrationManager(tmp_path, deck_id="resolve-handoff")
    manager.register_runner(role, lambda *_: {})
    try:
        manager.submit_tasks([{"task_id": "original", "role": role, **fields}])
        drain(manager)
        first = manager.task("original")
        spec = {"task_id": "handoff", "role": role, "handoff_from": first.owner_id, "group_id": "new-executor"}
        manager.submit_tasks([spec])
        drain(manager)
        current = manager.task("handoff")
        assert current.owner_id != first.owner_id
        assert (current.pages, current.output_path) == (first.pages, first.output_path)
        assert manager.submit_tasks([spec])["reused_task_ids"] == ["handoff"]
    finally:
        manager.close()


def test_single_task_read_decodes_only_its_record_without_sharing_mutable_state(tmp_path, monkeypatch):
    manager = OrchestrationManager(tmp_path, deck_id='single-read')
    manager.register_runner('slide', lambda *_: None)
    monkeypatch.setattr(manager, '_dispatch_ready_locked', lambda: None)
    try:
        manager.submit_tasks([{'task_id': f'p{page}', 'role': 'slide', 'pages': [page]} for page in range(1, 9)])
        calls = []
        original = TaskRecord.from_dict
        def decode(cls, value):
            calls.append(value['task_id'])
            return original(value)
        monkeypatch.setattr(TaskRecord, 'from_dict', classmethod(decode))
        manager._state['tasks']['p3']['result'] = {'evidence': ['source']}
        task = manager.task('p3')
        assert calls == ['p3']
        task.result['evidence'].append('must not mutate state')
        assert manager._state['tasks']['p3']['result'] == {'evidence': ['source']}
        with pytest.raises(KeyError):
            manager.task('missing')
        manager._state['tasks']['p3']['task_id'] = 'wrong'
        with pytest.raises(OrchestrationError, match='task key'):
            manager.task('p3')
        manager._state['tasks']['p3']['task_id'] = 'p3'
    finally:
        manager.close()


def test_async_results_are_persisted_without_collect_and_replay_is_idempotent(tmp_path):
    started, release = threading.Event(), threading.Event()
    calls = []
    def runner(task, _):
        calls.append(task.task_id)
        started.set()
        assert release.wait(5)
        return {'pages': list(task.pages)}
    manager = OrchestrationManager(tmp_path, deck_id='async')
    manager.register_runner('slide', runner)
    spec = {'task_id': 'opening', 'role': 'slide', 'pages': [1, 2]}
    try:
        manager.submit_tasks([spec])
        assert started.wait(2)
        assert manager.task('opening').status == TaskStatus.RUNNING
        assert manager.submit_tasks([spec])['reused_task_ids'] == ['opening']
        with pytest.raises(OrchestrationError, match='different contract'):
            manager.submit_tasks([{**spec, 'goal': 'changed'}])
        release.set()
        assert any(row['type'] == 'task_completed' for row in drain(manager))
        assert calls == ['opening']
        assert json.loads(manager.path.read_text())['tasks']['opening']['result'] == {'pages': [1, 2]}
        assert 'waves' not in manager.snapshot()
    finally:
        release.set()
        manager.close()


def test_slide_scaffold_preflight_rejects_whole_batch_before_owner_registration(tmp_path):
    from types import SimpleNamespace
    from state.workspace import WorkspaceState
    from runtime.skill_runtime import SkillRuntime
    from runtime.tools import ToolExecutionError
    workspace = WorkspaceState(tmp_path)
    manager = OrchestrationManager(tmp_path, deck_id="scaffold-preflight")
    runtime = SkillRuntime.__new__(SkillRuntime)
    services = SimpleNamespace(workspace=workspace)
    manager.validate_submission = lambda tasks: runtime._validate_slide_submission(services, tasks)
    calls = []
    manager.register_runner("slide", lambda task, _: calls.append(task.task_id) or {})
    specs = [{"task_id": f"p{page}", "role": "slide", "pages": [page]} for page in (1, 2)]
    first = workspace.paths.page_html(1)
    first.parent.mkdir(exist_ok=True)
    first.write_text("authored page must not be overwritten")
    try:
        with pytest.raises(ToolExecutionError, match="slide_02.html") as error:
            manager.submit_tasks(specs)
        assert error.value.code == "slide_preparation_missing"
        assert not manager.snapshot()["tasks"] and not manager.registry.list_current() and not calls
        assert first.read_text() == "authored page must not be overwritten"
        workspace.paths.page_html(2).write_text("new skeleton")
        receipt = manager.submit_tasks(specs)
        assert receipt["new_task_ids"] == ["p1", "p2"] and not receipt["reused_task_ids"]
        drain(manager)
        assert sorted(calls) == ["p1", "p2"]
    finally:
        manager.close()


def test_failed_task_replay_says_no_execution_and_new_id_reuses_original_owner(tmp_path):
    manager = OrchestrationManager(tmp_path, deck_id="retry-contract")
    calls = []
    def runner(task, _):
        calls.append(task.owner_id)
        if task.task_id == "first":
            raise ValueError("fixable prerequisite")
        return {}
    manager.register_runner("slide", runner)
    spec = {"task_id": "first", "role": "slide", "pages": [1]}
    try:
        manager.submit_tasks([spec])
        drain(manager)
        assert manager.task("first").status == TaskStatus.FAILED
        receipt = manager.submit_tasks([spec])
        assert receipt["new_task_ids"] == [] and receipt["reused_failed_task_ids"] == ["first"]
        assert "没有重新执行" in receipt["replay_notice"] and len(calls) == 1
        manager.submit_tasks([{**spec, "task_id": "after-fix"}])
        drain(manager)
        assert len(calls) == 2 and calls[0] == calls[1]
        assert len(manager.registry.list_current()) == 1
    finally:
        manager.close()


def test_submission_preparation_releases_scheduler_and_replay_does_not_prepare(tmp_path):
    manager = OrchestrationManager(tmp_path, deck_id="prepare-outside-scheduler")
    preparing, release = threading.Event(), threading.Event()
    prepared, executions = [], []

    def prepare(tasks):
        prepared.append([task["task_id"] for task in tasks])
        assert not manager._condition._is_owned()
        preparing.set()
        assert release.wait(5)

    manager.prepare_submission = prepare
    manager.register_runner("slide", lambda task, _: executions.append(task.task_id) or {})
    spec = {"task_id": "p1", "role": "slide", "pages": [1]}
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            submission = pool.submit(manager.submit_tasks, [spec])
            assert preparing.wait(2)
            assert pool.submit(manager.snapshot).result(timeout=1)["tasks"] == {}
            release.set()
            assert submission.result(timeout=5)["new_task_ids"] == ["p1"]
        drain(manager)
        assert manager.submit_tasks([spec])["reused_task_ids"] == ["p1"]
        assert prepared == [["p1"]] and executions == ["p1"]
    finally:
        release.set()
        manager.close()


@pytest.mark.parametrize("failure", ["interrupted", "invalid-plan"])
def test_preparation_failure_accepts_nothing_and_same_id_can_retry(tmp_path, failure):
    manager = OrchestrationManager(tmp_path, deck_id="preparation-retry")
    manager.register_runner("slide", lambda *_: {})
    problem = InterruptedError if failure == "interrupted" else ValueError

    def prepare(tasks):
        raise problem("preparation did not finish")

    manager.prepare_submission = prepare
    specs = [{"task_id": f"p{page}", "role": "slide", "pages": [page]} for page in (1, 2)]
    try:
        with pytest.raises(problem):
            manager.submit_tasks(specs)
        assert not manager.snapshot()["tasks"] and not manager.registry.list_current()
        manager.prepare_submission = lambda _: None
        assert manager.submit_tasks(specs)["new_task_ids"] == ["p1", "p2"]
        drain(manager)
    finally:
        manager.close()


def test_submission_checks_conflicts_before_preparation_and_rechecks_after_it(tmp_path):
    manager = OrchestrationManager(tmp_path, deck_id="preparation-conflicts")
    manager.register_runner("slide", lambda *_: {})
    preparations = []
    manager.prepare_submission = lambda tasks: preparations.append(tasks)
    spec = {"task_id": "p1", "role": "slide", "pages": [1]}
    try:
        with pytest.raises(OrchestrationError, match="overlapping"):
            manager.submit_tasks([spec, {**spec, "task_id": "duplicate", "group_id": "another-owner"}])
        with pytest.raises(OrchestrationError, match="unknown dependency"):
            manager.submit_tasks([{**spec, "depends_on": ["missing"]}])
        assert preparations == []

        def claim_during_preparation(tasks):
            manager.registry.claim_orchestrator_page(1)

        manager.prepare_submission = claim_during_preparation
        with pytest.raises(OrchestrationError, match="page_owner_conflict"):
            manager.submit_tasks([spec])
        assert manager.snapshot()["tasks"] == {}
        assert manager.registry.page_owner(1).owner_id == "orchestrator"
    finally:
        manager.close()


def test_orchestrator_natural_idle_waits_for_real_events_and_keeps_uncovered_pages(tmp_path):
    from types import SimpleNamespace
    from runtime.skill_runtime import SkillRuntime
    manager = OrchestrationManager(tmp_path, deck_id="automatic-idle")
    (manager.state_dir / "page-index.json").write_text(json.dumps({"deck_version": 1, "pages": [
        {"position": page, "page_uid": f"p{page}"} for page in (1, 2, 3)]}))
    started, release = threading.Event(), threading.Event()
    def runner(task, _):
        if task.pages == (2,):
            started.set()
            assert release.wait(5)
        return {}
    manager.register_runner("slide", runner)
    runtime = SkillRuntime.__new__(SkillRuntime)
    completed = []
    services = SimpleNamespace(orchestration=manager, complete_deck=lambda: completed.append(True))
    try:
        manager.submit_tasks([{"task_id": f"p{p}", "role": "slide", "pages": [p]} for p in (1, 2)])
        assert started.wait(2)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(runtime._orchestrator_idle, services)
            assert not future.done()
            release.set()
            message = future.result(timeout=5)
        payload = json.loads(message["content"])
        assert message["role"] == "user" and payload["harness_event"] == "orchestration_update"
        assert payload["all_terminal"] and payload["unassigned_pages"] == [3]
        assert all(row["type"] != "tool_result" for row in payload["events"])
        assert completed == []  # 后台刚结束时先让主 Agent 处理真实结果，不自动假定已整册复核。
        assert runtime._orchestrator_idle(services) is None
        assert completed == [True]
    finally:
        release.set()
        manager.close()


def test_slide_completions_and_dependency_start_are_coalesced_in_one_closed_wait(tmp_path):
    entered, release, wait_started = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def runner(task, _):
        calls.append(task.task_id)
        if task.task_id == "second":
            entered.set()
            assert release.wait(5)
        return {"page": task.pages[0]}

    manager = OrchestrationManager(tmp_path, deck_id="coalesced")
    manager.register_runner("slide", runner)
    specs = [{"task_id": "first", "role": "slide", "pages": [1]},
             {"task_id": "second", "role": "slide", "pages": [2], "depends_on": ["first"]}]
    tools = ToolRuntime(event_sink=lambda event: wait_started.set() if event["event_type"] == "tool_use" else None)
    tools.register(ToolDefinition("delegate_wait", "wait", STABLE_TOOL_SCHEMAS["delegate_wait"],
        lambda context, arguments: manager.wait(consumer_id=context.owner_id, timeout_s=5, **arguments),
        roles=frozenset({"orchestrator"})))
    context = ToolContext("orchestrator", "orchestrator")
    call = ToolCall("coalesced-wait", "delegate_wait", {})
    try:
        manager.submit_tasks(specs)
        assert entered.wait(3)
        persisted = json.loads(manager.path.read_text())
        assert persisted["tasks"]["first"]["status"] == "succeeded"
        assert persisted["tasks"]["second"]["status"] == "running"
        completed = [event for event in persisted["events"] if event["type"] == "task_completed"]
        unblocked = [event for event in persisted["events"] if event["type"] == "dependency_unblocked"]
        assert len(completed) == len(unblocked) == 1
        assert not completed[0]["actionable"] and not unblocked[0]["actionable"]
        assert manager.wait(timeout_s=0) is None
        assert manager.snapshot()["cursors"] == {}
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(tools.execute, context, call)
            assert wait_started.wait(2) and not pending.done()
            assert [event["event_type"] for event in tools.events] == ["tool_use"]
            release.set()
            result = pending.result(timeout=5)
        assert result.ok
        payload = json.loads(result.content)
        assert payload["all_terminal"]
        assert [event["task_id"] for event in payload["events"]] == ["first", "second"]
        assert {task["task_id"] for task in payload["tasks"]} == {"first", "second"}
        assert payload["cursor"] == manager.snapshot()["sequence"]
        assert replay_tool_transactions(tools.events).open_ids == ()
        assert [event["event_type"] for event in tools.events] == ["tool_use", "tool_result"]
        assert tools.execute(context, call) == result and len(tools.events) == 2
        expected = manager.snapshot()["events"]
    finally:
        release.set()
        manager.close()

    restored = OrchestrationManager(tmp_path, deck_id="coalesced")
    try:
        restored.register_runner("slide", lambda *_: pytest.fail("completed Owner must not restart"))
        assert restored.wait(timeout_s=0)["events"] == []
        replay = restored.wait(after_sequence=0, timeout_s=0)
        assert [event["task_id"] for event in replay["events"]] == ["first", "second"]
        assert replay["all_terminal"] and calls == ["first", "second"]
        assert restored.submit_tasks(specs)["reused_task_ids"] == ["first", "second"]
        assert restored.snapshot()["events"] == expected
    finally:
        restored.close()


@pytest.mark.parametrize("role,status", [("research", "completed"), ("image", "completed"),
    ("slide", "failed"), ("slide", "repair_required"), ("slide", "repair_handoff"), ("slide", "group_review_gap")])
def test_decision_completions_and_failures_wake_while_other_slide_is_running(tmp_path, role, status):
    started, release = threading.Event(), threading.Event()

    def runner(task, _):
        if task.task_id == "slow":
            started.set()
            assert release.wait(5)
            return {}
        assert started.wait(3)
        if status == "failed":
            raise RuntimeError("specific missing input")
        if status == "group_review_gap":
            return {"status": "completed", "group_review_gap": {"pages": [2, 3],
                    "reason": "group_inspection_missing_or_stale"}}
        return {"status": status, "source": "confirmed"}

    manager = OrchestrationManager(tmp_path, deck_id="decision-events")
    (manager.state_dir / "page-index.json").write_text(json.dumps({"deck_version": 1, "pages": [
        {"position": page, "page_uid": f"page-{page}"} for page in (1, 2, 3)]}))
    for registered_role in {role, "slide"}:
        manager.register_runner(registered_role, runner)
    try:
        manager.submit_tasks([{"task_id": "slow", "role": "slide", "pages": [1]},
                              {"task_id": "decision", "role": role,
                               "pages": [2, 3] if status == "group_review_gap" else [2] if role == "slide" else []}])
        update = manager.wait(timeout_s=5)
        assert update is not None and not update["all_terminal"]
        assert update["unassigned_pages"] == ([] if status == "group_review_gap" else [3] if role == "slide" else [2, 3])
        event = next(event for event in update["events"] if event["task_id"] == "decision")
        assert event["type"] == ("task_failed" if status == "failed" else "task_completed")
        assert event["actionable"]
        if status == "failed":
            assert "specific missing input" in event["error"]
        if status == "group_review_gap":
            assert manager.task("decision").result["group_review_gap"]["pages"] == [2, 3]
        assert manager.task("slow").status == TaskStatus.RUNNING
        assert manager.wait(timeout_s=0) is None
    finally:
        release.set()
        manager.close()


def test_failed_image_task_only_blocks_declared_consumers(tmp_path):
    release, independent = threading.Event(), threading.Event()
    def image(*_):
        assert release.wait(5)
        raise RuntimeError("deterministic image format failure")
    def slide(task, _):
        assert task.task_id == "no-image"
        independent.set()
        return {}
    manager = OrchestrationManager(tmp_path, deck_id="image-dependencies")
    manager.register_runner("image", image)
    manager.register_runner("slide", slide)
    try:
        manager.submit_tasks([
            {"task_id": "images", "role": "image"},
            {"task_id": "needs-image", "role": "slide", "pages": [1], "depends_on": ["images"]},
            {"task_id": "no-image", "role": "slide", "pages": [2]},
        ])
        assert independent.wait(2)
        assert manager.task("needs-image").status == TaskStatus.WAITING_DEPENDENCY
        release.set()
        drain(manager)
        assert manager.task("images").status == TaskStatus.FAILED
        assert manager.task("needs-image").status == TaskStatus.SKIPPED
        assert manager.task("no-image").status == TaskStatus.COMPLETED
    finally:
        release.set()
        manager.close()


def test_image_tasks_wait_for_research_only_when_dependency_is_explicit(tmp_path, monkeypatch):
    manager = OrchestrationManager(tmp_path, deck_id="image-research-dependency")
    manager.register_runner("research", lambda *_: {})
    manager.register_runner("image", lambda *_: {})
    monkeypatch.setattr(manager, "_schedule_ready_locked", lambda: None)
    try:
        manager.submit_tasks([
            {"task_id": "research-source", "role": "research"},
            {"task_id": "image-independent", "role": "image"},
            {
                "task_id": "image-dependent",
                "role": "image",
                "depends_on": ["research-source"],
            },
        ])
        assert manager.task("image-independent").dependencies == ()
        assert manager.task("image-independent").status == TaskStatus.PENDING
        assert manager.task("image-dependent").dependencies == ("research-source",)
        assert manager.task("image-dependent").status == TaskStatus.WAITING_DEPENDENCY
    finally:
        manager.close()


def test_same_owner_can_repair_beyond_three_rounds(tmp_path):
    manager = OrchestrationManager(tmp_path, deck_id='soft-repair')
    owners = []
    manager.register_runner('slide', lambda task, _: owners.append(task.owner_id) or {})
    try:
        for number in range(4):
            manager.submit_tasks([{'task_id': f'repair-{number}', 'role': 'slide', 'pages': [1], 'repair': True}])
            drain(manager)
        assert len(owners) == 4 and len(set(owners)) == 1
    finally:
        manager.close()


@pytest.mark.parametrize("pages,group_id,repair", [
    ([3], "", True), ([3], "contrast", True),
    ([3, 4], "renamed", True), ([3], "", False),
])
def test_finished_group_rejects_new_owner_then_resumes_original_after_restart(tmp_path, pages, group_id, repair):
    manager = OrchestrationManager(tmp_path, deck_id="persistent-group")
    manager.register_runner("slide", lambda *_: {})
    initial = {"task_id": "initial", "role": "slide", "pages": [3, 4], "group_id": "contrast"}
    manager.submit_tasks([initial])
    drain(manager)
    original = manager.task("initial")
    manager.close()
    manager = OrchestrationManager(tmp_path, deck_id="persistent-group")
    calls = []
    manager.register_runner("slide", lambda task, _: calls.append(task) or {})
    before, registry_before = manager.path.read_bytes(), manager.registry.path.read_bytes()
    try:
        with pytest.raises(OrchestrationError, match="page_owner_conflict") as caught:
            manager.submit_tasks([
                {"task_id": "independent", "role": "slide", "pages": [5]},
                {"task_id": "repair", "role": "slide", "pages": pages,
                 "group_id": group_id, "repair": repair},
            ])
        assert original.owner_id in str(caught.value)
        assert "contrast" in str(caught.value) and "[3, 4]" in str(caught.value)
        assert manager.path.read_bytes() == before
        assert manager.registry.path.read_bytes() == registry_before
        assert not calls and "submission" not in manager.snapshot()
        corrected = {**initial, "task_id": "repair", "repair": True,
                     "goal": "Only repair P3; P4 is unchanged."}
        manager.submit_tasks([corrected])
        drain(manager)
        repaired = manager.task("repair")
        assert repaired.owner_id == original.owner_id and repaired.attempt == 2
        assert repaired.pages == (3, 4) and repaired.goal == corrected["goal"]
        assert len(manager.registry.list_current()) == len(calls) == 1
        state = manager.snapshot()
        assert manager.submit_tasks([corrected])["reused_task_ids"] == ["repair"]
        assert manager.snapshot() == state and len(calls) == 1
    finally:
        manager.close()


def test_general_dependencies_and_incremental_submission_have_no_batch_barrier(tmp_path):
    release, started, independent = threading.Event(), threading.Event(), threading.Event()
    order = []
    def research(task, _):
        started.set()
        assert release.wait(5)
        order.append(task.task_id)
        return {'claim': 'checked'}
    def run(task, _):
        order.append(task.task_id)
        if task.task_id == 'cover':
            independent.set()
        return {}
    manager = OrchestrationManager(tmp_path, deck_id='dag')
    manager.register_runner('research', research)
    manager.register_runner('image', run)
    manager.register_runner('slide', run)
    try:
        manager.submit_tasks([{'task_id': 'facts', 'role': 'research'}])
        assert started.wait(2)
        manager.submit_tasks([
            {'task_id': 'visual', 'role': 'image', 'depends_on': ['facts']},
            {'task_id': 'content', 'role': 'slide', 'pages': [2, 3], 'depends_on': ['visual']},
            {'task_id': 'cover', 'role': 'slide', 'pages': [1]},
        ])
        assert independent.wait(2)
        assert manager.task('visual').status == manager.task('content').status == TaskStatus.WAITING_DEPENDENCY
        release.set()
        drain(manager)
        assert order.index('cover') < order.index('facts') < order.index('visual') < order.index('content')
    finally:
        release.set()
        manager.close()


@pytest.mark.parametrize('failed', [False, True])
def test_terminal_resume_is_idle_without_duplicate_events_or_execution(tmp_path, failed):
    def runner(*_):
        if failed:
            raise RuntimeError('image unavailable')
        return {'catalog': 'assets/catalog.md'}
    manager = OrchestrationManager(tmp_path, deck_id='terminal')
    manager.register_runner('image', runner)
    spec = {'task_id': 'images', 'role': 'image'}
    manager.submit_tasks([spec])
    assert drain(manager)
    expected = manager.snapshot()['events']
    assert manager.wait(timeout_s=.01)['events'] == []
    manager.close()
    resumed = OrchestrationManager(tmp_path, deck_id='terminal')
    try:
        resumed.register_runner('image', lambda *_: pytest.fail('must not rerun'))
        assert resumed.wait(timeout_s=.01)['all_terminal']
        replay = resumed.submit_tasks([spec])
        assert replay['reused_task_ids'] == ['images']
        assert replay['tasks'][0]['status'] == ('failed' if failed else 'succeeded')
        assert resumed.snapshot()['events'] == expected
    finally:
        resumed.close()


def test_owner_help_remains_visible_after_cursor_consumption_and_resumes_same_call(tmp_path):
    seen = []
    def runner(task, manager):
        seen.append(manager.request_owner_assistance(task.task_id, request_type='plan_ambiguity', detail='Which source?'))
        return {'continued': True}
    manager = OrchestrationManager(tmp_path, deck_id='request')
    (manager.state_dir / 'page-index.json').write_text(json.dumps({'deck_version': 1, 'pages': [
        {'position': page, 'page_uid': f'page-{page}'} for page in (1, 2)]}))
    manager.register_runner('slide', runner)
    try:
        manager.submit_tasks([{'task_id': 'page', 'role': 'slide', 'pages': [1]}])
        update = manager.wait(timeout_s=5)
        request = update['pending_requests'][0]
        assert update['unassigned_pages'] == [2]
        assert manager.task('page').status == TaskStatus.WAITING
        (tmp_path / 'base.css').write_text(':root { --stub-size: 18px; }')
        assert manager.task('page').status == TaskStatus.WAITING and seen == []
        assert manager.wait(timeout_s=.01)['pending_requests'][0]['request_id'] == request['request_id']
        manager.respond_owner_request(request['request_id'], {'status': 'resolved', 'resolution': 'Use audited data.'})
        drain(manager)
        assert seen == [{'status': 'resolved', 'resolution': 'Use audited data.'}]
    finally:
        manager.close()


def test_failed_dependency_skips_descendants_without_fake_owner_start(tmp_path):
    def fail(*_):
        raise RuntimeError('source unavailable')
    manager = OrchestrationManager(tmp_path, deck_id='failure')
    manager.register_runner('research', fail)
    for role in ('image', 'slide'):
        manager.register_runner(role, lambda *_: pytest.fail('dependent must not start'))
    try:
        manager.submit_tasks([
            {'task_id': 'facts', 'role': 'research'},
            {'task_id': 'visual', 'role': 'image', 'depends_on': ['facts']},
            {'task_id': 'page', 'role': 'slide', 'pages': [1], 'depends_on': ['visual']},
        ])
        events = drain(manager)
        assert manager.task('facts').status == TaskStatus.FAILED
        assert manager.task('visual').status == manager.task('page').status == TaskStatus.SKIPPED
        assert len([row for row in events if row['type'] == 'task_skipped']) == 2
        assert not any(row['type'] == 'task_started' and row['task_id'] != 'facts' for row in manager.snapshot()['events'])
    finally:
        manager.close()


def test_restart_resumes_same_task_and_owner_attempt(tmp_path):
    registry = OwnerRegistry(tmp_path, deck_id='restart')
    owner = registry.accept(role='research', group_id='research')
    registry.transition(owner.owner_id, attempt=1, status=OwnerStatus.RUNNING)
    (tmp_path / '_state/orchestration.json').write_text(json.dumps({
        'schema': ORCHESTRATION_SCHEMA, 'sequence': 0, 'tasks': {
            'facts': {'task_id': 'facts', 'owner_id': owner.owner_id, 'attempt': 1, 'role': 'research',
                      'pages': [], 'group_id': 'research', 'dependencies': [], 'status': 'running'}},
        'requests': {}, 'events': [], 'cursors': {}}))
    manager = OrchestrationManager(tmp_path, deck_id='restart', registry=registry)
    assert manager.task('facts').status == TaskStatus.INTERRUPTED
    try:
        manager.register_runner('research', lambda task, _: {'same': task.task_id})
        drain(manager)
        assert manager.task('facts').result == {'same': 'facts'}
        assert manager.task('facts').attempt == 1
        types = [row['type'] for row in manager.snapshot()['events']]
        assert types.count('task_interrupted') == types.count('task_resumed') == types.count('task_completed') == 1
    finally:
        manager.close()


def test_restart_delivers_unconsumed_coalesced_result_and_only_resumes_interrupted_owner(tmp_path):
    registry = OwnerRegistry(tmp_path, deck_id="partial-restart")
    owners = [registry.accept(role="slide", pages=[page], group_id=f"p{page}") for page in (1, 2)]
    for owner in owners:
        registry.transition(owner.owner_id, attempt=1, status=OwnerStatus.RUNNING)
    registry.transition(owners[0].owner_id, attempt=1, status=OwnerStatus.COMPLETED)
    tasks = {f"p{page}": TaskRecord(
        task_id=f"p{page}", owner_id=owner.owner_id, attempt=1, role="slide", pages=(page,),
        group_id=f"p{page}", dependencies=(),
        status=TaskStatus.COMPLETED if page == 1 else TaskStatus.RUNNING,
        result={"page": 1} if page == 1 else None,
    ).to_dict() for page, owner in enumerate(owners, 1)}
    completed = {"sequence": 1, "type": "task_completed", "task_id": "p1",
                 "owner_id": owners[0].owner_id, "actionable": False}
    (tmp_path / "_state/orchestration.json").write_text(json.dumps({
        "schema": ORCHESTRATION_SCHEMA, "sequence": 1, "tasks": tasks,
        "requests": {}, "events": [completed], "cursors": {}}))
    manager = OrchestrationManager(tmp_path, deck_id="partial-restart", registry=registry)
    calls = []
    try:
        update = manager.wait(timeout_s=0)
        assert [event["type"] for event in update["events"]] == ["task_completed", "task_interrupted"]
        assert next(task for task in update["tasks"] if task["task_id"] == "p1")["result"] == {"page": 1}
        manager.register_runner("slide", lambda task, _: calls.append(task.task_id) or {"page": 2})
        drain(manager)
        assert calls == ["p2"]
        assert manager.task("p2").owner_id == owners[1].owner_id and manager.task("p2").attempt == 1
        events = manager.snapshot()["events"]
        assert [event["task_id"] for event in events if event["type"] == "task_completed"] == ["p1", "p2"]
        assert manager.wait(timeout_s=0)["events"] == []
    finally:
        manager.close()


@pytest.mark.parametrize('tasks, message', [
    ([{'task_id': 'a', 'role': 'slide', 'pages': [1], 'depends_on': ['missing']}], 'unknown dependency'),
    ([{'task_id': 'a', 'role': 'slide', 'pages': [1], 'depends_on': ['b']},
      {'task_id': 'b', 'role': 'slide', 'pages': [2], 'depends_on': ['a']}], 'cycle'),
    ([{'task_id': 'a', 'role': 'slide', 'pages': [1]}, {'task_id': 'a', 'role': 'slide', 'pages': [2]}], 'distinct'),
    ([{'task_id': 'a', 'role': 'slide', 'pages': [1]}, {'task_id': 'b', 'role': 'slide', 'pages': [1], 'group_id': 'other'}], 'overlapping'),
    ([{'task_id': 'a', 'role': 'slide', 'pages': [1], 'depends_on': True}], 'depends_on'),
    ([{'task_id': 'a', 'role': 'slide', 'pages': [1], 'repair': 'false'}], 'boolean'),
])
def test_invalid_graph_rejected_before_owner_write(tmp_path, tasks, message):
    manager = OrchestrationManager(tmp_path, deck_id='invalid')
    manager.register_runner('slide', lambda *_: {})
    before = manager.registry.path.read_bytes() if manager.registry.path.exists() else None
    try:
        with pytest.raises(OrchestrationError, match=message):
            manager.submit_tasks(tasks)
        assert manager.snapshot()['tasks'] == {}
        assert (manager.registry.path.read_bytes() if manager.registry.path.exists() else None) == before
    finally:
        manager.close()


@pytest.mark.parametrize('grouping', ['default', 'distinct', 'dependency'])
def test_same_submission_images_have_independent_identities(tmp_path, grouping):
    manager = OrchestrationManager(tmp_path, deck_id='image-batch')
    manager.register_runner('image', lambda *_: {})
    tasks = [{'task_id': f'image-{number}', 'role': 'image', 'goal': f'asset {number}'}
             for number in range(6)]
    for number, task in enumerate(tasks):
        if grouping == 'distinct':
            task['group_id'] = task['task_id']
        elif grouping == 'dependency' and number:
            task['depends_on'] = [tasks[number - 1]['task_id']]
    validate_json_value({'tasks': tasks}, STABLE_TOOL_SCHEMAS['delegate_submit'])
    try:
        rows = manager.submit_tasks(tasks)['tasks']
        drain(manager)
        assert len({row['owner_id'] for row in rows}) == 6
        assert all(row['status'] == 'succeeded' for row in manager.snapshot()['tasks'].values())
    finally:
        manager.close()


def test_same_submission_group_conflict_is_not_an_existing_task(tmp_path):
    manager = OrchestrationManager(tmp_path, deck_id='slide-batch')
    manager.register_runner('slide', lambda *_: pytest.fail('rejected batch must not run'))
    try:
        with pytest.raises(OrchestrationError, match='current submission repeats owner group'):
            manager.submit_tasks([
                {'task_id': 'first', 'role': 'slide', 'pages': [1]},
                {'task_id': 'second', 'role': 'slide', 'pages': [1]},
            ])
        assert manager.snapshot()['tasks'] == {}
    finally:
        manager.close()


@pytest.mark.parametrize('distinct_group', [False, True])
def test_existing_image_writer_is_not_reported_as_same_batch(tmp_path, distinct_group):
    started, release = threading.Event(), threading.Event()
    def runner(*_):
        started.set()
        assert release.wait(5)
        return {}
    manager = OrchestrationManager(tmp_path, deck_id='active-image')
    manager.register_runner('image', runner)
    try:
        active = manager.submit_tasks([{'task_id': 'active', 'role': 'image'}])['tasks'][0]
        assert started.wait(2)
        task = {'task_id': 'next', 'role': 'image'}
        if distinct_group:
            task['group_id'] = 'another-group'
            manager.submit_tasks([task])
            assert set(manager.snapshot()['tasks']) == {'active', 'next'}
        else:
            task['group_id'] = active['group_id']
            with pytest.raises(OrchestrationError, match='already has an unfinished') as caught:
                manager.submit_tasks([task])
            assert 'current submission' not in str(caught.value)
            assert set(manager.snapshot()['tasks']) == {'active'}
    finally:
        release.set()
        manager.close()


def test_one_image_asset_batch_and_disjoint_slides_remain_accepted(tmp_path):
    manager = OrchestrationManager(tmp_path, deck_id='combined-images')
    calls = []
    def runner(task, _):
        calls.append(task.task_id)
        return {}
    manager.register_runner('image', runner)
    manager.register_runner('slide', runner)
    try:
        manager.submit_tasks([
            {'task_id': 'images', 'role': 'image', 'goal': 'asset A for P1; asset B for P2'},
            {'task_id': 'first', 'role': 'slide', 'pages': [1], 'depends_on': ['images']},
            {'task_id': 'second', 'role': 'slide', 'pages': [2]},
        ])
        drain(manager)
        assert sorted(calls) == ['first', 'images', 'second']
        assert all(row['status'] == 'succeeded' for row in manager.snapshot()['tasks'].values())
    finally:
        manager.close()


def test_submission_journal_recovers_crash_after_owner_accept(tmp_path, monkeypatch):
    manager = OrchestrationManager(tmp_path, deck_id='journal')
    manager.register_runner('slide', lambda *_: pytest.fail('crashed submission cannot run'))
    accept = manager.registry.accept_many
    def crash(rows):
        accept(rows)
        raise OSError('simulated process loss after registry commit')
    monkeypatch.setattr(manager.registry, 'accept_many', crash)
    with pytest.raises(OSError, match='process loss'):
        manager.submit_tasks([{'task_id': 'page', 'role': 'slide', 'pages': [1]}])
    assert json.loads(manager.path.read_text())['submission']
    manager.close()
    resumed = OrchestrationManager(tmp_path, deck_id='journal')
    calls = []
    try:
        resumed.register_runner('slide', lambda task, _: calls.append(task.task_id) or {})
        drain(resumed)
        assert calls == ['page']
        assert resumed.task('page').attempt == 1
        assert 'submission' not in resumed.snapshot()
        assert [row['type'] for row in resumed.snapshot()['events']].count('task_submitted') == 1
    finally:
        resumed.close()


def test_registry_rejection_after_preflight_does_not_leave_replayable_submission(tmp_path, monkeypatch):
    manager = OrchestrationManager(tmp_path, deck_id="registry-rejection")
    manager.register_runner("slide", lambda *_: {})
    accept = manager.registry.accept_many
    def reject(_rows):
        raise OwnerRegistryError("page_owner_conflict: registry changed after preflight")
    monkeypatch.setattr(manager.registry, "accept_many", reject)
    spec = {"task_id": "page", "role": "slide", "pages": [1]}
    try:
        with pytest.raises(OrchestrationError, match="page_owner_conflict"):
            manager.submit_tasks([spec])
        assert "submission" not in manager.snapshot()
        assert manager.snapshot()["tasks"] == {} and manager.snapshot()["events"] == []
        assert not manager.registry.list_current()
        monkeypatch.setattr(manager.registry, "accept_many", accept)
        manager.submit_tasks([spec])
        drain(manager)
        assert manager.task("page").attempt == 1
    finally:
        manager.close()


def test_main_claim_cannot_enter_between_delegation_preflight_and_accept(tmp_path, monkeypatch):
    manager = OrchestrationManager(tmp_path, deck_id="claim-window")
    manager.register_runner("slide", lambda *_: {})
    other = OwnerRegistry(tmp_path, deck_id="claim-window")
    checked, attempted, finished = threading.Event(), threading.Event(), threading.Event()
    validate = manager.registry.validate_page_ownership

    def paused_preflight(rows):
        validate(rows)
        checked.set()
        assert attempted.wait(3)
        assert not finished.wait(.05), "claim entered the preflight-to-accept window"

    def claim():
        assert checked.wait(3)
        attempted.set()
        try:
            with pytest.raises(OwnerRegistryError, match="page_owner_conflict"):
                other.claim_orchestrator_page(1)
        finally:
            finished.set()

    monkeypatch.setattr(manager.registry, "validate_page_ownership", paused_preflight)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed = pool.submit(claim)
            submitted = pool.submit(manager.submit_tasks, [{"task_id": "page", "role": "slide", "pages": [1]}])
            receipt = submitted.result(timeout=5)
            claimed.result(timeout=5)
        drain(manager)
        assert receipt["new_task_ids"] == ["page"]
        assert other.page_owner(1).owner_id == manager.task("page").owner_id
        assert "submission" not in manager.snapshot()
        assert len(manager.registry.list_current()) == 1
    finally:
        manager.close()


def test_main_owned_page_rejects_entire_delegation_before_intent(tmp_path):
    manager = OrchestrationManager(tmp_path, deck_id="main-won")
    calls = []
    manager.register_runner("slide", lambda task, _: calls.append(task.task_id) or {})
    manager.registry.claim_orchestrator_page(1)
    before = manager.registry.path.read_bytes()
    try:
        with pytest.raises(OrchestrationError, match="page_owner_conflict.*orchestrator"):
            manager.submit_tasks([
                {"task_id": "independent", "role": "slide", "pages": [2]},
                {"task_id": "conflict", "role": "slide", "pages": [1]},
            ])
        assert manager.snapshot()["tasks"] == {} and manager.snapshot()["events"] == []
        assert "submission" not in manager.snapshot() and not calls
        assert manager.registry.path.read_bytes() == before
    finally:
        manager.close()


def test_unresolved_failure_is_carried_without_repeated_wakeup_and_superseded_by_retry(tmp_path):
    manager = OrchestrationManager(tmp_path, deck_id="failure-handoff")
    release = threading.Event()
    def run(task, _manager):
        if task.task_id == "broken":
            raise RuntimeError("asset changed")
        if task.task_id == "slow":
            assert release.wait(5)
        return {}
    manager.register_runner("slide", run)
    try:
        manager.submit_tasks([{"task_id": "broken", "role": "slide", "pages": [1], "group_id": "p1"},
                              {"task_id": "slow", "role": "slide", "pages": [2], "group_id": "p2"}])
        first = manager.wait(timeout_s=3)
        assert first["unresolved_failures"][0]["task_id"] == "broken"
        assert manager.wait(timeout_s=.02) is None
        manager.publish_background_event("input_ready", {}, on_published=lambda: None)
        assert manager.wait(timeout_s=0)["unresolved_failures"] == first["unresolved_failures"]
        manager.submit_tasks([{"task_id": "retry", "role": "slide", "pages": [1], "group_id": "p1", "repair": True}])
        manager.publish_background_event("next_input", {}, on_published=lambda: None)
        assert manager.wait(timeout_s=0)["unresolved_failures"] == []
        assert manager.task("retry").owner_id == manager.task("broken").owner_id
    finally:
        release.set()
        manager.close()


@pytest.mark.parametrize("link", ["continuation_params", "handoff_params", "research_task_link_params"])
def test_failure_feedback_provides_executable_research_recovery(tmp_path, link):
    manager = OrchestrationManager(tmp_path, deck_id="actionable-recovery")

    def run(task, _manager):
        if task.task_id == "research-02":
            raise RuntimeError("data_inspection_failed")
        return {"status": "completed"}

    manager.register_runner("research", run)
    original_spec = {"task_id": "research-02", "role": "research",
                     "group_id": "research-research-02", "output_path": "research/original.md"}
    try:
        manager.submit_tasks([original_spec])
        failed = manager.wait(timeout_s=3)
        recovery = failed["unresolved_failures"][0]["recovery"]
        event = next(row for row in failed["events"] if row["type"] == "task_failed")
        assert event["recovery"] == recovery
        assert recovery["failed_task_id"] == "research-02"
        assert recovery["failed_owner_id"] == manager.task("research-02").owner_id
        assert recovery["continuation_params"] == {
            "role": "research", "group_id": "research-research-02",
            "output_path": "research/original.md", "depends_on": [],
        }
        replay = manager.submit_tasks([original_spec])
        assert replay["new_task_ids"] == []
        assert replay["recovery"] == [recovery]
        with pytest.raises(OrchestrationError) as denied:
            manager.submit_tasks([{**original_spec, "goal": "retry"}])
        assert denied.value.diagnostics == {"recovery": recovery}
        params = recovery[link]
        if link == "handoff_params":
            params = {**params, "group_id": "explicit-replacement"}
        manager.submit_tasks([{"task_id": "research-02b", "goal": "Recover missing evidence", **params}])
        completed = manager.wait(timeout_s=3)
        assert completed["all_terminal"]
        assert completed["unresolved_failures"] == []
        assert manager.task("research-02").status == TaskStatus.FAILED
        assert manager.task("research-02b").status == TaskStatus.COMPLETED
    finally:
        manager.close()


def test_successful_research_continuation_can_reference_failed_task_id_as_group(tmp_path):
    manager = OrchestrationManager(tmp_path, deck_id="research-continuation")

    def run(task, _manager):
        if task.task_id == "research-original":
            raise RuntimeError("provider returned malformed tool arguments")
        return {"status": "completed", "output_path": task.output_path}

    manager.register_runner("research", run)
    try:
        manager.submit_tasks([{
            "task_id": "research-original", "role": "research",
            "group_id": "research-topic", "output_path": "research/original.md",
        }])
        failed = manager.wait(timeout_s=3)
        assert [row["task_id"] for row in failed["unresolved_failures"]] == [
            "research-original"
        ]
        manager.submit_tasks([{
            "task_id": "research-continuation-r2", "role": "research",
            "group_id": "research-original",
            "goal": "Continue the failed research scope and write the missing manuscript.",
        }])
        completed = manager.wait(timeout_s=3)
        assert completed["all_terminal"]
        assert completed["unresolved_failures"] == []
        assert manager.task("research-continuation-r2").owner_id != manager.task("research-original").owner_id
    finally:
        manager.close()


def test_unlinked_research_group_does_not_supersede_unrelated_failure(tmp_path):
    manager = OrchestrationManager(tmp_path, deck_id="research-unrelated")

    def run(task, _manager):
        if task.task_id == "research-broken":
            raise RuntimeError("source unavailable")
        return {"status": "completed"}

    manager.register_runner("research", run)
    try:
        manager.submit_tasks([{
            "task_id": "research-broken", "role": "research",
            "group_id": "shared-topic",
        }])
        assert manager.wait(timeout_s=3)["unresolved_failures"]
        manager.submit_tasks([{
            "task_id": "research-broken-b", "role": "research",
            "group_id": "research-research-broken-b",
            "goal": "The old failure is superseded by this successful replacement.",
        }])
        completed = manager.wait(timeout_s=3)
        assert [row["task_id"] for row in completed["unresolved_failures"]] == [
            "research-broken"
        ]
    finally:
        manager.close()


@pytest.mark.parametrize("group_link", [False, True])
def test_successful_research_handoff_resolves_failed_ancestors(tmp_path, group_link):
    manager = OrchestrationManager(tmp_path, deck_id="research-handoff-chain")

    def run(task, _manager):
        if task.task_id != "verified":
            raise RuntimeError("malformed provider tool arguments")
        return {"status": "completed", "output_path": task.output_path}

    manager.register_runner("research", run)
    try:
        manager.submit_tasks([{"task_id": "original", "role": "research"}])
        assert manager.wait(timeout_s=3)["unresolved_failures"]
        original = manager.task("original")
        link = ({"group_id": original.task_id} if group_link else
                {"handoff_from": original.owner_id})
        manager.submit_tasks([{"task_id": "recovery", "role": "research", **link}])
        failed = manager.wait(timeout_s=3)
        assert {row["task_id"] for row in failed["unresolved_failures"]} == {"original", "recovery"}
        manager.submit_tasks([{
            "task_id": "verified", "role": "research",
            "handoff_from": manager.task("recovery").owner_id,
        }])
        completed = manager.wait(timeout_s=3)
        assert completed["all_terminal"]
        assert completed["unresolved_failures"] == []
        assert manager.task("original").status == TaskStatus.FAILED
        assert manager.task("recovery").status == TaskStatus.FAILED
    finally:
        manager.close()


@pytest.mark.parametrize("role", ["research", "slide", "image"])
@pytest.mark.parametrize("status", [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.RUNNING])
def test_handoff_ancestor_resolution_requires_successful_descendant(tmp_path, role, status):
    from state.artifacts import ArtifactStore
    from state.workspace import WorkspaceState

    ArtifactStore(tmp_path, workspace_state=WorkspaceState(tmp_path))
    manager = OrchestrationManager(tmp_path, deck_id="handoff-success-required")
    tasks = {
        task_id: TaskRecord(task_id, owner_id, 1, role, (), task_id, (), task_status,
                            handoff_from=predecessor)
        for task_id, owner_id, task_status, predecessor in (
            ("original", "owner-original", TaskStatus.FAILED, ""),
            ("recovery", "owner-recovery", TaskStatus.FAILED, "owner-original"),
            ("replacement", "owner-replacement", status, "owner-recovery"),
        )
    }
    try:
        with manager._condition:
            resolved = manager._resolved_handoff_owners_locked(tasks)
        expected = {"owner-original", "owner-recovery"} if status == TaskStatus.COMPLETED else set()
        assert resolved == expected
    finally:
        manager.close()


def test_handoff_ancestor_cycle_is_bounded(tmp_path):
    manager = OrchestrationManager(tmp_path, deck_id="handoff-cycle")
    tasks = {
        task_id: TaskRecord(task_id, owner_id, 1, "research", (), task_id, (), status,
                            handoff_from=predecessor)
        for task_id, owner_id, status, predecessor in (
            ("original", "owner-original", TaskStatus.FAILED, "owner-recovery"),
            ("recovery", "owner-recovery", TaskStatus.FAILED, "owner-original"),
            ("replacement", "owner-replacement", TaskStatus.COMPLETED, "owner-recovery"),
        )
    }
    try:
        with manager._condition:
            assert manager._resolved_handoff_owners_locked(tasks) == {"owner-original", "owner-recovery"}
    finally:
        manager.close()


def test_image_handoff_chain_keeps_unfinished_ancestor_unresolved(tmp_path):
    from state.artifacts import ArtifactStore
    from state.workspace import WorkspaceState

    store = ArtifactStore(tmp_path, workspace_state=WorkspaceState(tmp_path))
    store.declare(artifact_id="missing-diagram", kind="image", owner_id="owner-original",
                  path="assets/missing-diagram.png")
    manager = OrchestrationManager(tmp_path, deck_id="partial-image-chain")
    tasks = {
        task_id: TaskRecord(task_id, owner_id, 1, "image", (), task_id, (), status,
                            handoff_from=predecessor)
        for task_id, owner_id, status, predecessor in (
            ("original", "owner-original", TaskStatus.FAILED, ""),
            ("recovery", "owner-recovery", TaskStatus.FAILED, "owner-original"),
            ("replacement", "owner-replacement", TaskStatus.COMPLETED, "owner-recovery"),
        )
    }
    try:
        with manager._condition:
            assert manager._resolved_handoff_owners_locked(tasks) == {"owner-recovery"}
        assert store.artifact("missing-diagram").status.value == "declared"
    finally:
        manager.close()


def test_failed_image_owner_is_superseded_by_explicit_image_handoff(tmp_path):
    from PIL import Image
    from state.artifacts import ArtifactStore
    from state.workspace import WorkspaceState

    workspace = WorkspaceState(tmp_path)
    store = ArtifactStore(tmp_path, workspace_state=workspace)
    manager = OrchestrationManager(tmp_path, deck_id=workspace.deck_id)

    def run(task, _manager):
        if task.task_id == "broken-image":
            raise RuntimeError("candidate inspection loop")
        return {"status": "completed"}

    manager.register_runner("image", run)
    try:
        submitted = manager.submit_tasks([{"task_id": "broken-image", "role": "image"}])["tasks"][0]
        Image.new("RGB", (8, 8), "navy").save(tmp_path / "assets/candidate.png")
        store.record_image_result(artifact_id="candidate", owner_id=submitted["owner_id"],
            path="assets/candidate.png", status="available")
        failed = manager.wait(timeout_s=3)
        assert failed is not None
        assert [row["task_id"] for row in failed["unresolved_failures"]] == ["broken-image"]

        original = manager.task("broken-image")
        manager.submit_tasks([{
            "task_id": "image-handoff",
            "role": "image",
            "handoff_from": original.owner_id,
            "asset_ids": ["candidate"],
            "goal": "Take over the failed image task and finish its remaining registration",
        }])
        completed = manager.wait(timeout_s=3)
        assert completed is not None and completed["all_terminal"]
        assert completed["unresolved_failures"] == []
        assert manager.task("image-handoff").owner_id != original.owner_id
        assert manager.wait(timeout_s=0)["unresolved_failures"] == []
    finally:
        manager.close()


def test_partial_image_handoff_keeps_failed_predecessor_unresolved(tmp_path):
    from PIL import Image
    from state.artifacts import ArtifactStore
    from state.workspace import WorkspaceState

    workspace = WorkspaceState(tmp_path)
    store = ArtifactStore(tmp_path, workspace_state=workspace)
    manager = OrchestrationManager(tmp_path, deck_id=workspace.deck_id)

    def run(task, _manager):
        if task.task_id == "broken-image":
            raise RuntimeError("cover and diagram were not finished")
        return {"status": "completed"}

    manager.register_runner("image", run)
    try:
        submitted = manager.submit_tasks([{"task_id": "broken-image", "role": "image"}])["tasks"][0]
        for artifact_id in ("cover", "diagram"):
            Image.new("RGB", (8, 8), "navy").save(tmp_path / f"assets/{artifact_id}.png")
        store.record_image_result(artifact_id="cover", owner_id=submitted["owner_id"],
            path="assets/cover.png", status="available")
        store.declare(artifact_id="diagram", kind="image", owner_id=submitted["owner_id"],
            path="assets/diagram.png")
        assert manager.wait(timeout_s=3)["unresolved_failures"]

        manager.submit_tasks([{
            "task_id": "cover-handoff",
            "role": "image",
            "handoff_from": submitted["owner_id"],
            "asset_ids": ["cover"],
        }])
        completed = manager.wait(timeout_s=3)
        assert [row["task_id"] for row in completed["unresolved_failures"]] == ["broken-image"]
        assert store.artifact("diagram").owner_id == submitted["owner_id"]
    finally:
        manager.close()


def test_image_handoff_ignores_superseded_source_format_declaration(tmp_path):
    from PIL import Image
    from state.artifacts import ArtifactStore
    from state.workspace import WorkspaceState

    workspace = WorkspaceState(tmp_path)
    store = ArtifactStore(tmp_path, workspace_state=workspace)
    manager = OrchestrationManager(tmp_path, deck_id=workspace.deck_id)

    def run(task, _manager):
        if task.task_id == "broken-image":
            raise RuntimeError("source-format declaration survived conversion")
        return {"status": "completed"}

    manager.register_runner("image", run)
    try:
        submitted = manager.submit_tasks([{
            "task_id": "broken-image", "role": "image"
        }])["tasks"][0]
        Image.new("RGB", (8, 8), "navy").save(tmp_path / "assets/figure-original.jpg")
        Image.new("RGB", (8, 8), "navy").save(tmp_path / "assets/figure-original.png")
        store.declare(artifact_id="stale-source", kind="image",
            owner_id=submitted["owner_id"], path="assets/figure-original.jpg")
        store.record_image_result(artifact_id="verified-derivative",
            owner_id=submitted["owner_id"], path="assets/figure-original.png",
            status="available")
        assert manager.wait(timeout_s=3)["unresolved_failures"]
        manager.submit_tasks([{
            "task_id": "image-handoff", "role": "image",
            "handoff_from": submitted["owner_id"],
            "asset_ids": ["verified-derivative"],
        }])
        completed = manager.wait(timeout_s=3)
        assert completed is not None and completed["all_terminal"]
        assert completed["unresolved_failures"] == []
        assert store.artifact("stale-source").status.value == "declared"
    finally:
        manager.close()


def test_owner_compaction_failure_is_returned_without_interrupting_the_deck(tmp_path):
    manager = OrchestrationManager(tmp_path, deck_id="owner-compact", max_workers=2)

    def run(task, _manager):
        if task.task_id == "compact-failed":
            raise InterruptedError(
                "owner_interrupted: context_compaction_recoverable; checkpoint retained; "
                "restart this Owner or delegate a narrower task"
            )
        return {"task_id": task.task_id}

    manager.register_runner("slide", run)
    try:
        manager.submit_tasks([
            {"task_id": "compact-failed", "role": "slide", "pages": [1], "group_id": "p1"},
            {"task_id": "independent", "role": "slide", "pages": [2], "group_id": "p2"},
        ])
        events = drain(manager)
        failed = manager.task("compact-failed")
        assert failed.status == TaskStatus.FAILED
        event = next(row for row in events if row["task_id"] == "compact-failed")
        assert event["type"] == "task_failed"
        assert event["reason"] == "context_compaction_recoverable"
        assert event["recoverable"] is True
        assert manager.task("independent").status == TaskStatus.COMPLETED

        manager.submit_tasks([{
            "task_id": "compact-retry",
            "role": "slide",
            "pages": [1],
            "group_id": "p1",
            "goal": "Retry with a narrower task after compaction failure",
        }])
        drain(manager)
        assert manager.task("compact-retry").status == TaskStatus.COMPLETED
        assert manager.wait(timeout_s=0)["unresolved_failures"] == []
    finally:
        manager.close()


def test_workspace_resume_cancel_remains_an_interruption(tmp_path):
    manager = OrchestrationManager(tmp_path, deck_id="workspace-cancel")
    manager.register_runner("slide", lambda *_: (_ for _ in ()).throw(
        InterruptedError(
            "owner_interrupted: provider request cancelled for workspace resume; "
            "prior event=context_compaction_recoverable"
        )
    ))
    try:
        manager.submit_tasks([{"task_id": "cancelled", "role": "slide", "pages": [1]}])
        with pytest.raises(InterruptedError, match="resume the same workspace"):
            manager.wait(timeout_s=5)
        assert manager.task("cancelled").status == TaskStatus.INTERRUPTED
    finally:
        manager.close()


@pytest.mark.parametrize("source", ["plan_index", "registered_pages"])
def test_coverage_distinguishes_main_delegated_and_unassigned_and_survives_resume(tmp_path, source):
    manager = OrchestrationManager(tmp_path, deck_id="mixed-coverage")
    pages = [{"position": page, "page_uid": f"p{page}"} for page in range(1, 5)]
    if source == "plan_index":
        (manager.state_dir / "page-index.json").write_text(json.dumps({"deck_version": 1, "pages": pages}))
    else:
        (manager.state_dir / "workspace.json").write_text(json.dumps({"deck_version": 1, "pages": {
            str(row["position"]): {"page": row["position"], "page_uid": row["page_uid"]} for row in pages}}))
    expected = {"planned_page_count": 4, "page_coverage_source": source,
                "self_authored_pages": [1, 3], "delegated_pages": [2],
                "unassigned_pages": [4], "undelegated_pages": [4]}
    manager.registry.claim_orchestrator_page(1)
    manager.registry.claim_orchestrator_page(3)
    manager.register_runner("slide", lambda *_: {})
    manager.register_runner("image", lambda *_: {})
    try:
        manager.submit_tasks([
            {"task_id": "details", "role": "slide", "pages": [2]},
            {"task_id": "visuals", "role": "image", "pages": [4]},
        ])
        drain(manager)
        assert manager.page_coverage() == expected
        update = manager.wait(timeout_s=0)
        assert {key: update[key] for key in expected} == expected
        assert all(task["role"] != "orchestrator" for task in manager.snapshot()["tasks"].values())
    finally:
        manager.close()
    restored = OrchestrationManager(tmp_path, deck_id="mixed-coverage")
    try:
        assert restored.page_coverage() == expected
        assert restored.registry.page_owner(1).owner_id == "orchestrator"
        restored.register_runner("slide", lambda *_: pytest.fail("completed owner must not rerun"))
        assert restored.wait(timeout_s=0)["all_terminal"]
    finally:
        restored.close()


def test_self_authored_deck_can_be_idle_without_fabricated_tasks(tmp_path):
    manager = OrchestrationManager(tmp_path, deck_id="no-children")
    (manager.state_dir / "page-index.json").write_text(json.dumps({"deck_version": 1, "pages": [
        {"position": 1, "page_uid": "p1"}]}))
    manager.registry.claim_orchestrator_page(1)
    try:
        state = manager.wait(timeout_s=0)
        assert state["all_terminal"] and state["self_authored_pages"] == [1]
        assert state["delegated_pages"] == state["unassigned_pages"] == []
        assert not state["events"] and not state["tasks"] and not manager.snapshot()["tasks"]
    finally:
        manager.close()


def test_coverage_does_not_assign_replacement_page_from_old_task_position(tmp_path):
    manager = OrchestrationManager(tmp_path, deck_id="uid-coverage")
    index = manager.state_dir / "page-index.json"
    index.write_text(json.dumps({"deck_version": 1, "pages": [{"position": 1, "page_uid": "old"}]}))
    manager.register_runner("slide", lambda *_: {})
    try:
        manager.submit_tasks([{"task_id": "first", "role": "slide", "pages": [1]}])
        drain(manager)
        index.write_text(json.dumps({"deck_version": 2, "pages": [{"position": 1, "page_uid": "new"}]}))
        assert manager.page_coverage()["unassigned_pages"] == [1]
        assert manager.page_coverage()["delegated_pages"] == []
    finally:
        manager.close()


@pytest.mark.parametrize('status', ['queued', 'running', 'waiting_dependency', 'blocked', 'interrupted'])
def test_finalize_cannot_mutate_deck_while_tasks_are_unfinished(tmp_path, status):
    from types import SimpleNamespace
    from runtime.production import ProductionToolServices
    from runtime.tools import ToolContext, ToolExecutionError
    from state.workspace import WorkspaceState
    service = ProductionToolServices.__new__(ProductionToolServices)
    service.workspace = WorkspaceState(tmp_path)
    service.settings = SimpleNamespace(command_timeout_s=10)
    service._validated_command = lambda *_: ([], 'orchestrator', 'finalize', SimpleNamespace())
    path = tmp_path / '_state/orchestration.json'
    path.write_text(json.dumps({'tasks': {'page': {'status': status}}}))
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    with pytest.raises(ToolExecutionError, match='unfinished tasks'):
        service.terminal(ToolContext('orchestrator', 'orchestrator'), {'command': 'finalize'})
    assert {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before


def test_revision_tasks_pin_page_identity_and_base_version(tmp_path):
    state = tmp_path / '_state'
    state.mkdir()
    (state / 'page-index.json').write_text(json.dumps({'deck_version': 3, 'pages': [{'position': 2, 'page_uid': 'stable-page'}]}))
    (state / 'active-revision.json').write_text(json.dumps({'revision_id': 'revision-4'}))
    manager = OrchestrationManager(tmp_path, deck_id='revision')
    manager.register_runner('slide', lambda *_: {})
    try:
        task = manager.submit_tasks([{'task_id': 'revision-4-page', 'role': 'slide', 'pages': [2]}])['tasks'][0]
        assert task['base_deck_version'] == 3
        assert task['page_uids'] == ['stable-page'] and task['revision_id'] == 'revision-4'
        drain(manager)
    finally:
        manager.close()
