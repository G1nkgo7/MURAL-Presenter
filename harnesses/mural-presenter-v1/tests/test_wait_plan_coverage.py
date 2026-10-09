"""提交和等待回执只报告既有计划/任务事实，不创建任务或扩大等待屏障。"""
import json
import threading
from types import SimpleNamespace

import pytest

from infer import PresenterKernel
from orchestration.manager import OrchestrationManager
from runtime.tools import ToolContext
from state.workspace import WorkspaceState


def make_kernel(tmp_path, fail=False):
    manager = OrchestrationManager(tmp_path, deck_id="wait-coverage")

    def slide(*_):
        if fail:
            raise RuntimeError("existing failure")
        return {"authored": True}

    manager.register_runner("slide", slide)
    kernel = PresenterKernel.__new__(PresenterKernel)
    kernel.workspace = SimpleNamespace(paths=SimpleNamespace(state=tmp_path / "_state"))
    kernel.orchestration = manager
    return kernel


def write_index(kernel, identities):
    path = kernel.workspace.paths.state / "page-index.json"
    path.write_text(json.dumps({"deck_version": 1, "pages": [
        {"position": position, "page_uid": uid} for position, uid in enumerate(identities, 1)
    ]}), encoding="utf-8")


def wait(kernel):
    return kernel._delegate_wait_handler()(ToolContext("orchestrator", "orchestrator"), {})


def finish(kernel):
    while True:
        result = wait(kernel)
        if result["all_terminal"]:
            return result


def test_submit_reports_coverage_before_any_owner_finishes_and_replay_is_idempotent(tmp_path, monkeypatch):
    kernel = make_kernel(tmp_path)
    monkeypatch.setattr(kernel.orchestration, "_dispatch_ready_locked", lambda: None)
    handler = kernel._delegate_submit_handler()
    context = ToolContext("orchestrator", "orchestrator")
    tasks = [{"task_id": f"p{page}", "role": "slide", "pages": [page]} for page in range(1, 6)]
    try:
        write_index(kernel, [f"p-{i}" for i in range(1, 11)])
        result = handler(context, {"tasks": tasks})
        assert result["planned_page_count"] == 10
        assert result["page_coverage_source"] == "plan_index"
        assert result["delegated_pages"] == [1, 2, 3, 4, 5]
        assert result["undelegated_pages"] == [6, 7, 8, 9, 10]
        assert all(task["status"] == "queued" for task in result["tasks"])
        state = kernel.orchestration.snapshot()
        assert kernel.orchestration.wait(timeout_s=0) is None
        assert kernel.orchestration.wait(after_sequence=0, timeout_s=0) is None
        assert kernel.orchestration.snapshot() == state
        replay = handler(context, {"tasks": tasks})
        assert replay["reused_task_ids"] == [task["task_id"] for task in tasks]
        assert replay["delegated_pages"] == result["delegated_pages"]
        assert kernel.orchestration.wait(timeout_s=0) is None
        assert len([event for event in state["events"] if event["type"] == "task_submitted"]) == 5
        assert kernel.orchestration.snapshot() == state
    finally:
        kernel.orchestration.close()


def test_broad_image_submission_returns_parallelization_advisories(tmp_path, monkeypatch):
    kernel = make_kernel(tmp_path)
    kernel.orchestration.register_runner("image", lambda *_: {"images": []})
    monkeypatch.setattr(kernel.orchestration, "_dispatch_ready_locked", lambda: None)
    write_index(kernel, [f"p-{i}" for i in range(1, 6)])

    try:
        result = kernel._delegate_submit_handler()(ToolContext("orchestrator", "orchestrator"), {
            "tasks": [{
                "task_id": "image-all",
                "role": "image",
                "goal": "为 P1 封面、P3 生态和 P5 收束寻找真实深海素材",
            }]
        })
        codes = {row["code"] for row in result["parallelization_advisories"]}
        assert "image_task_split_preferred" in codes
        assert "slide_parallelism_available" in codes
        assert "do_not_wait_for_full_image_batch_before_slides" in codes
    finally:
        kernel.orchestration.close()


def test_slide_parallelism_advisory_is_not_page_count_thresholded():
    from infer import _parallelization_advisories

    payload = {"planned_page_count": 2, "undelegated_pages": [1, 2], "tasks": []}
    codes = {row["code"] for row in _parallelization_advisories(payload)}
    assert "slide_parallelism_available" in codes


def test_research_completion_nudges_orchestrator_to_adopt_knowledge(tmp_path):
    kernel = make_kernel(tmp_path)
    kernel.orchestration.register_runner("research", lambda *_: {"status": "done"})
    try:
        kernel._delegate_submit_handler()(ToolContext("orchestrator", "orchestrator"), {
            "tasks": [{
                "task_id": "facts",
                "role": "research",
                "output_path": "research/facts.md",
                "goal": "核验关键事实并交回来源与边界",
            }],
        })
        result = finish(kernel)
        advisories = result["knowledge_integration_advisories"]
        assert advisories[0]["code"] == "research_ready_for_knowledge_adoption"
        assert advisories[0]["research_outputs"] == [{
            "task_id": "facts",
            "output_path": "research/facts.md",
        }]
        assert "knowledge.md" in advisories[0]["message"]
        assert "briefs/slide_NN.md" in advisories[0]["message"]
    finally:
        kernel.orchestration.close()


@pytest.mark.parametrize("event_before_submit", [True, False])
def test_submit_coverage_does_not_consume_unseen_decision_events(tmp_path, monkeypatch, event_before_submit):
    kernel = make_kernel(tmp_path)
    manager = kernel.orchestration
    monkeypatch.setattr(manager, "_dispatch_ready_locked", lambda: None)

    def publish():
        manager.publish_background_event("attachment_parse_completed", {"attachment_id": "source"},
                                         on_published=lambda: None)

    try:
        write_index(kernel, ["first", "missing"])
        if event_before_submit:
            publish()
        receipt = kernel._delegate_submit_handler()(ToolContext("orchestrator", "orchestrator"),
            {"tasks": [{"task_id": "first", "role": "slide", "pages": [1]}]})
        assert receipt["undelegated_pages"] == [2]
        if not event_before_submit:
            publish()
        trace = manager.snapshot()["events"]
        result = manager.wait(timeout_s=0)
        assert result is not None and not result["all_terminal"]
        assert result["undelegated_pages"] == [2] and result["delegated_pages"] == [1]
        assert [event["type"] for event in result["events"]] == ["attachment_parse_completed"]
        assert manager.snapshot()["events"] == trace
        assert manager.wait(timeout_s=0) is None
    finally:
        kernel.orchestration.close()


def test_slide_completion_with_uncovered_pages_is_logged_but_waits_for_all_submitted_tasks(tmp_path):
    kernel = make_kernel(tmp_path)
    manager = kernel.orchestration
    releases = {key: threading.Event() for key in ("first", "second")}

    def slide(task, _):
        assert releases[task.task_id].wait(5)
        return {"authored": True}

    manager.register_runner("slide", slide)
    try:
        write_index(kernel, ["first", "second", "missing"])
        receipt = kernel._delegate_submit_handler()(ToolContext("orchestrator", "orchestrator"),
            {"tasks": [{"task_id": key, "role": "slide", "pages": [page]}
                       for page, key in enumerate(releases, 1)]})
        assert receipt["undelegated_pages"] == [3]
        assert manager.wait(timeout_s=0) is None
        releases["first"].set()
        with manager._condition:
            assert manager._condition.wait_for(lambda: manager.task("first").terminal, timeout=3)
        persisted = json.loads(manager.path.read_text())
        assert persisted["tasks"]["first"]["result"] == {"authored": True}
        completed = [event for event in persisted["events"] if event["type"] == "task_completed"]
        assert len(completed) == 1 and completed[0]["task_id"] == "first"
        assert not completed[0]["actionable"]
        assert manager.wait(timeout_s=0) is None
        assert manager.snapshot()["cursors"] == {}
        releases["second"].set()
        with manager._condition:
            assert manager._condition.wait_for(lambda: manager.task("second").terminal, timeout=3)
        update = manager.wait(timeout_s=0)
        assert update is not None and update["all_terminal"]
        assert update["undelegated_pages"] == [3]
        assert [event["task_id"] for event in update["events"]] == ["first", "second"]
        assert {task["task_id"] for task in update["tasks"]} == {"first", "second"}
        assert len(manager.snapshot()["tasks"]) == 2
        assert manager.wait(timeout_s=0)["events"] == []
    finally:
        for release in releases.values():
            release.set()
        manager.close()


def test_five_of_ten_reports_remaining_pages_even_when_submitted_tasks_finish(tmp_path):
    kernel = make_kernel(tmp_path)
    try:
        write_index(kernel, [f"p-{i}" for i in range(1, 11)])
        kernel.orchestration.submit_tasks([
            {"task_id": f"slide-{p}", "role": "slide", "pages": [p]} for p in (1, 3, 4, 5, 6)
        ])
        result = finish(kernel)
        assert result["planned_page_count"] == 10
        assert result["undelegated_pages"] == [2, 7, 8, 9, 10]
        assert len(kernel.orchestration.snapshot()["tasks"]) == 5
        events = kernel.orchestration.snapshot()["events"]
        assert wait(kernel)["undelegated_pages"] == [2, 7, 8, 9, 10]
        assert kernel.orchestration.snapshot()["events"] == events
        kernel.orchestration.submit_tasks([
            {"task_id": "remaining-group", "role": "slide", "pages": [2, 7, 8, 9, 10]}
        ])
        assert finish(kernel)["undelegated_pages"] == []
    finally:
        kernel.orchestration.close()


@pytest.mark.parametrize("fail", [False, True])
def test_failed_or_completed_task_is_not_mislabeled_as_never_delegated(tmp_path, fail):
    kernel = make_kernel(tmp_path, fail=fail)
    try:
        write_index(kernel, ["assigned", "not-assigned"])
        kernel.orchestration.submit_tasks([{"task_id": "slide", "role": "slide", "pages": [1]}])
        result = finish(kernel)
        assert result["undelegated_pages"] == [2]
        assert kernel.orchestration.task("slide").status == ("failed" if fail else "succeeded")
    finally:
        kernel.orchestration.close()


def test_current_uids_keep_coverage_correct_after_insert_move_delete_and_resume(tmp_path):
    kernel = make_kernel(tmp_path)
    try:
        write_index(kernel, ["a", "b", "c"])
        kernel.orchestration.submit_tasks([{"task_id": "first", "role": "slide", "pages": [1, 2]}])
        assert finish(kernel)["undelegated_pages"] == [3]
        write_index(kernel, ["c", "a", "new"])
        assert wait(kernel)["undelegated_pages"] == [1, 3]
        events = kernel.orchestration.snapshot()["events"]
    finally:
        kernel.orchestration.close()
    resumed = make_kernel(tmp_path)
    try:
        assert wait(resumed)["undelegated_pages"] == [1, 3]
        assert resumed.orchestration.snapshot()["events"] == events
    finally:
        resumed.orchestration.close()


def test_absent_plan_does_not_fabricate_a_page_count_or_empty_coverage(tmp_path):
    kernel = make_kernel(tmp_path)
    try:
        result = wait(kernel)
        assert result["all_terminal"]
        assert "planned_page_count" not in result and "undelegated_pages" not in result
    finally:
        kernel.orchestration.close()


def test_registered_pages_without_compiled_index_report_coverage_and_bind_uids(tmp_path):
    workspace = WorkspaceState(tmp_path)
    workspace.register_pages(range(1, 11))
    before = workspace.path.read_bytes()
    kernel = make_kernel(tmp_path)
    kernel.workspace = workspace
    try:
        kernel.orchestration.submit_tasks([
            {"task_id": "representative", "role": "slide", "pages": [3, 4]}
        ])
        result = finish(kernel)
        assert result["planned_page_count"] == 10
        assert result["undelegated_pages"] == [1, 2, 5, 6, 7, 8, 9, 10]
        assert result["page_coverage_source"] == "registered_pages"
        task = kernel.orchestration.task("representative")
        assert task.page_uids == (workspace.page(3).page_uid, workspace.page(4).page_uid)
        assert not (workspace.paths.state / "page-index.json").exists()
        assert workspace.path.read_bytes() == before
    finally:
        kernel.orchestration.close()


def test_unindexed_binding_survives_later_index_compile_reorder_and_resume(tmp_path):
    workspace = WorkspaceState(tmp_path)
    registered = workspace.register_pages([1, 2, 3])
    kernel = make_kernel(tmp_path)
    try:
        kernel.orchestration.submit_tasks([{"task_id": "first", "role": "slide", "pages": [1]}])
        assert finish(kernel)["undelegated_pages"] == [2, 3]
        write_index(kernel, [registered[1].page_uid, registered[0].page_uid, "new-page"])
        result = wait(kernel)
        assert result["page_coverage_source"] == "plan_index"
        assert result["undelegated_pages"] == [1, 3]
    finally:
        kernel.orchestration.close()
    resumed = make_kernel(tmp_path)
    try:
        assert wait(resumed)["undelegated_pages"] == [1, 3]
    finally:
        resumed.orchestration.close()


def test_unindexed_coverage_ignores_retired_pages_and_does_not_hash_artifacts(tmp_path, monkeypatch):
    workspace = WorkspaceState(tmp_path)
    workspace.register_pages([1, 2, 3])
    workspace.reconcile_pages([1, 2])
    monkeypatch.setattr(WorkspaceState, "pages", lambda _: pytest.fail("wait must read identities, not hash artifacts"))
    kernel = make_kernel(tmp_path)
    kernel.workspace = workspace
    try:
        result = wait(kernel)
        assert result["planned_page_count"] == 2
        assert result["undelegated_pages"] == [1, 2]
    finally:
        kernel.orchestration.close()


def test_task_before_registration_is_not_called_undelegated_in_unindexed_layout(tmp_path):
    workspace = WorkspaceState(tmp_path)
    kernel = make_kernel(tmp_path)
    try:
        kernel.orchestration.submit_tasks([{"task_id": "first", "role": "slide", "pages": [1]}])
        assert "planned_page_count" not in finish(kernel)
        workspace.register_pages([1, 2])
        assert wait(kernel)["undelegated_pages"] == [2]
        write_index(kernel, ["new-b", "new-a"])
        # 不能在结构身份已经明确后把旧页号当成新页身份。
        assert wait(kernel)["undelegated_pages"] == [1, 2]
    finally:
        kernel.orchestration.close()
