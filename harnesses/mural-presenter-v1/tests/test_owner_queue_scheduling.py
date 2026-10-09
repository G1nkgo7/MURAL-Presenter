"""Owner 线程队列不伪造启动；资源中断保留同一任务及其依赖。"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import threading
import time

import pytest

from test_orchestration_manager import drain
from orchestration.manager import OrchestrationManager, TaskStatus
from owner.registry import OwnerStatus
from runtime.production import ProductionSettings, ResourceSlots
from runtime.recovery import ResourceSlotTimeout


@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize("group_size", [1, 3])
def test_executor_pending_owners_are_not_reported_as_started(tmp_path, workers, group_size):
    release = threading.Event()
    ready = threading.Event()
    lock = threading.Lock()
    started = []
    active = 0
    peak = 0

    def runner(task, manager):
        nonlocal active, peak
        assert manager.task(task.task_id).status == TaskStatus.RUNNING
        with lock:
            active += 1
            peak = max(peak, active)
            started.append(task.task_id)
            if len(started) == workers:
                ready.set()
        try:
            assert release.wait(5)
            return {"same_task": task.task_id}
        finally:
            with lock:
                active -= 1

    manager = OrchestrationManager(tmp_path, deck_id="queued", max_workers=workers)
    manager.register_runner("slide", runner)
    try:
        wave = manager.submit_tasks([
            {"task_id": f"page-{page}", "role": "slide",
             "pages": list(range((page - 1) * group_size + 1, page * group_size + 1)), "group_id": f"page-{page}"}
            for page in range(1, 4)
        ])
        assert ready.wait(3)
        snapshot = manager.snapshot()
        assert sum(task["status"] == "running" for task in snapshot["tasks"].values()) == workers
        pending = [task for task in snapshot["tasks"].values() if task["status"] == "queued"]
        assert len(pending) == 3 - workers
        assert all(manager.registry.get(task["owner_id"]).status == OwnerStatus.PENDING for task in pending)
        events = [event for event in snapshot["events"] if event["type"] == "task_started"]
        assert {event["task_id"] for event in events} == set(started)
        release.set()
        drain(manager)
        assert peak <= workers
        starts = [event["task_id"] for event in manager.snapshot()["events"] if event["type"] == "task_started"]
        assert len(starts) == len(set(starts)) == 3
    finally:
        release.set()
        manager.close()


def test_cancelled_executor_pending_owner_is_not_started(tmp_path):
    release = threading.Event()
    started = threading.Event()
    calls = []

    def runner(task, manager):
        calls.append(task.task_id)
        started.set()
        assert release.wait(5)
        return {}

    manager = OrchestrationManager(tmp_path, deck_id="cancelled-queue", max_workers=1)
    manager.register_runner("slide", runner)
    try:
        wave = manager.submit_tasks([
            {"task_id": f"page-{page}", "role": "slide", "pages": [page], "group_id": f"page-{page}"}
            for page in (1, 2)
        ])
        assert started.wait(3)
        with manager._condition:
            tasks = manager._task_map_locked()
            queued = next(task for task in tasks.values() if task.status == TaskStatus.PENDING)
            tasks[queued.task_id] = replace(queued, status=TaskStatus.CANCELLED)
            manager._set_task_map_locked(tasks)
            manager.registry.transition(queued.owner_id, attempt=queued.attempt, status=OwnerStatus.CANCELLED)
            manager._persist_locked()
        release.set()
        drain(manager)
        manager._futures[queued.task_id].result(timeout=3)
        assert manager.task(queued.task_id).status == TaskStatus.CANCELLED
        assert queued.task_id not in calls
        assert not any(event["type"] == "task_started" and event["task_id"] == queued.task_id
                       for event in manager.snapshot()["events"])
    finally:
        release.set()
        manager.close()


def test_single_slot_owner_interruptions_resume_same_tasks_without_failing_dependencies(tmp_path):
    skill = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1"
    slots = ResourceSlots(tmp_path, ProductionSettings(skill_source=skill, model_concurrency=1), environment={})
    calls = []

    def runner(task, manager):
        calls.append((task.task_id, task.owner_id, task.attempt))
        with slots.acquire("model", deadline_monotonic=time.monotonic() + .2):
            return {"same_task": task.task_id}

    manager = OrchestrationManager(tmp_path, deck_id="resumable", max_workers=2)
    manager.register_runner("research", runner)
    manager.register_runner("slide", runner)
    try:
        with slots.acquire("model"):
            submission = manager.submit_tasks([
                {"task_id": "source", "role": "research", "group_id": "sources"},
                {"task_id": "dependent", "role": "slide", "pages": [1], "depends_on": ["source"]},
                {"task_id": "transitive", "role": "slide", "pages": [2], "depends_on": ["dependent"]},
                {"task_id": "independent", "role": "slide", "pages": [3]},
            ])
            rows = {row["task_id"]: row for row in submission["tasks"]}
            source, dependent, transitive, independent = [rows[key] for key in ("source", "dependent", "transitive", "independent")]
            for task in (source, independent):
                manager._futures[task["task_id"]].result(timeout=3)
                assert manager.task(task["task_id"]).status == TaskStatus.INTERRUPTED
                assert manager.registry.get(task["owner_id"]).status == OwnerStatus.INTERRUPTED
            for task in (dependent, transitive):
                assert manager.task(task["task_id"]).status == TaskStatus.WAITING_DEPENDENCY
            events = manager.snapshot()["events"]
            interrupted = [row for row in events if row["type"] == "task_interrupted"]
            assert len(interrupted) == 2
            assert all(row["reason"] == "resource_queue_timeout" and row["recoverable"] for row in interrupted)
            assert {row["task_id"] for row in events if row["type"] == "dependency_interrupted"} == {"dependent", "transitive"}
            with pytest.raises(ResourceSlotTimeout, match="resume the same workspace"):
                manager.wait(timeout_s=.05)
            manager.register_runner("research", runner)
            assert len(calls) == 2  # 同一进程的 collect/重新注册不暗中重试排队中断。
            before = manager.snapshot()
            assert not any(event["type"] in {"task_failed", "task_skipped"} for event in before["events"])
    finally:
        manager.close()

    resumed = OrchestrationManager(tmp_path, deck_id="resumable", max_workers=2)
    try:
        resumed.register_runner("research", runner)
        resumed.register_runner("slide", runner)
        drain(resumed)
        assert all(row["status"] == "succeeded" for row in resumed.snapshot()["tasks"].values())
        after = resumed.snapshot()
        assert set(after["tasks"]) == set(before["tasks"])
        for task_id, previous in before["tasks"].items():
            current = after["tasks"][task_id]
            assert (current["owner_id"], current["attempt"]) == (previous["owner_id"], previous["attempt"])
            assert current["error"] == ""
        assert len(calls) == 6
        assert [event["sequence"] for event in after["events"]] == list(range(1, after["sequence"] + 1))
        assert after["events"][:len(before["events"])] == before["events"]
        starts = [event["task_id"] for event in after["events"] if event["type"] == "task_started"]
        assert starts.count(source["task_id"]) == starts.count(independent["task_id"]) == 2
        assert starts.count(dependent["task_id"]) == starts.count(transitive["task_id"]) == 1
    finally:
        resumed.close()
