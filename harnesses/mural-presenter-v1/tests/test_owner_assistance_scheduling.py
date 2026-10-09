"""求助保留同步调用栈，但不占用可运行 Owner 的执行槽。"""
from __future__ import annotations

from dataclasses import replace
import threading

import pytest

from orchestration.manager import OrchestrationError, OrchestrationManager, TaskStatus
from owner.registry import OwnerStatus
from runtime.recovery import ResourceSlotTimeout
from test_orchestration_manager import drain


def wait_for(manager, predicate, timeout=20):
    with manager._condition:
        assert manager._condition.wait_for(predicate, timeout), manager.snapshot()


@pytest.mark.parametrize(("workers", "count"), [(1, 160), (2, 8)])
def test_waiting_owners_release_slots_and_resume_with_original_limit(tmp_path, workers, count):
    release = threading.Event()
    resumed = threading.Event()
    lock = threading.Lock()
    calls = []
    active = peak = 0

    def slide(task, manager):
        nonlocal active, peak
        identity = (task.owner_id, task.attempt, threading.get_ident())
        result = manager.request_owner_assistance(task.task_id, request_type="source", detail="Need source")
        assert result == {"source": "verified"}
        assert (task.owner_id, task.attempt, threading.get_ident()) == identity
        assert manager.task(task.task_id).status == TaskStatus.RUNNING
        with lock:
            calls.append(task.task_id)
            active += 1
            peak = max(active, peak)
            if active == workers:
                resumed.set()
        try:
            assert release.wait(20)
            return {"same_owner": task.owner_id}
        finally:
            with lock:
                active -= 1

    manager = OrchestrationManager(tmp_path, deck_id="waiting-capacity", max_workers=workers)
    manager.register_runner("slide", slide)
    manager.register_runner("research", lambda *_: {"source": "verified"})
    try:
        manager.submit_tasks([
            {"task_id": f"page-{page:03}", "role": "slide", "pages": [page]}
            for page in range(1, count + 1)
        ])
        wait_for(manager, lambda: len(manager._state["requests"]) == count, timeout=45)
        assert all(row["status"] == "blocked" for row in manager.snapshot()["tasks"].values())
        assert not manager._execution_tasks
        manager.submit_tasks([{"task_id": "research", "role": "research"}])
        assert manager._futures["research"].result(timeout=10) == {"source": "verified"}
        requests = list(manager.snapshot()["requests"].values())
        for request in requests:
            manager.respond_owner_request(request["request_id"], {"source": "verified"})
        assert resumed.wait(10)
        assert active == workers
        assert len(manager._execution_tasks) == workers
        release.set()
        drain(manager, timeout=45)
        assert peak == workers
        assert len(calls) == count
        starts = [event["task_id"] for event in manager.snapshot()["events"] if event["type"] == "task_started"]
        assert len(starts) == len(set(starts)) == count + 1
    finally:
        release.set()
        manager.close()
    assert not manager._execution_tasks
    assert all(future.done() for future in manager._futures.values())


def test_repeated_help_preserves_thread_task_and_attempt(tmp_path):
    identities = []

    def owner(task, manager):
        for turn in range(3):
            identities.append((task.task_id, task.owner_id, task.attempt, threading.get_ident()))
            reply = manager.request_owner_assistance(task.task_id, request_type="help", detail=str(turn))
            assert reply == {"turn": turn}
        return "ready"

    manager = OrchestrationManager(tmp_path, deck_id="repeated-help", max_workers=1)
    manager.register_runner("slide", owner)
    try:
        manager.submit_tasks([{"task_id": "page", "role": "slide", "pages": [1]}])
        for turn in range(3):
            wait_for(manager, lambda: len(manager._state["requests"]) == turn + 1)
            request = next(row for row in manager.snapshot()["requests"].values() if row["status"] == "pending")
            manager.respond_owner_request(request["request_id"], {"turn": turn})
        drain(manager)
        assert len(identities) == 3 and len(set(identities)) == 1
        assert manager.task("page").result == "ready"
        assert len(manager._threads) == 1
    finally:
        manager.close()


def test_timeout_waits_for_execution_slot_and_keeps_fifo_position(tmp_path):
    entered, release = threading.Event(), threading.Event()
    order = []

    def slide(task, manager):
        result = manager.request_owner_assistance(task.task_id, request_type="help", detail="source", timeout_s=.15)
        order.append("resumed")
        return result

    def blocker(*_):
        entered.set()
        assert release.wait(10)
        order.append("blocker")

    manager = OrchestrationManager(tmp_path, deck_id="timeout-fairness", max_workers=1)
    manager.register_runner("slide", slide)
    manager.register_runner("research", blocker)
    manager.register_runner("image", lambda *_: order.append("later"))
    try:
        manager.submit_tasks([{"task_id": "page", "role": "slide", "pages": [1]}])
        wait_for(manager, lambda: bool(manager._state["requests"]))
        manager.submit_tasks([{"task_id": "blocker", "role": "research"}])
        assert entered.wait(5)
        wait_for(manager, lambda: next(iter(manager._state["requests"].values()))["status"] == "timed_out")
        assert manager.task("page").status == TaskStatus.WAITING
        assert manager.registry.get(manager.task("page").owner_id).status == OwnerStatus.WAITING
        assert not order
        manager.submit_tasks([{"task_id": "later", "role": "image"}])
        assert "later" not in manager._threads
        release.set()
        drain(manager)
        assert order == ["blocker", "resumed", "later"]
        assert manager.task("page").result["status"] == "timed_out"
    finally:
        release.set()
        manager.close()


@pytest.mark.parametrize("wait", [False, True])
@pytest.mark.parametrize("responded", [False, True])
def test_close_interrupts_waiters_and_preserves_unstarted_work(tmp_path, wait, responded):
    entered, release = threading.Event(), threading.Event()
    continued = []

    def slide(task, manager):
        manager.request_owner_assistance(task.task_id, request_type="help", detail="source")
        continued.append("resumed")

    def blocker(*_):
        entered.set()
        assert release.wait(10)
        return "finished"

    manager = OrchestrationManager(tmp_path, deck_id="close-waiters", max_workers=1)
    manager.register_runner("slide", slide)
    manager.register_runner("research", blocker)
    manager.register_runner("image", lambda *_: continued.append("queued"))
    closer = None
    try:
        manager.submit_tasks([{"task_id": "page", "role": "slide", "pages": [1]}])
        wait_for(manager, lambda: bool(manager._state["requests"]))
        manager.submit_tasks([{"task_id": "blocker", "role": "research"}])
        assert entered.wait(5)
        request = next(iter(manager.snapshot()["requests"].values()))
        if responded:
            manager.respond_owner_request(request["request_id"], {"source": "ready"})
        manager.submit_tasks([{"task_id": "queued", "role": "image"}])
        closer = threading.Thread(target=lambda: manager.close(wait=wait))
        closer.start()
        wait_for(manager, lambda: manager._closed)
        if not wait:
            closer.join(2)
            assert not closer.is_alive()
        assert manager._futures["queued"].cancelled()
        assert manager.task("queued").status == TaskStatus.PENDING
        release.set()
        closer.join(5)
        assert not closer.is_alive()
        manager.close()
        assert manager.task("page").status == TaskStatus.INTERRUPTED
        assert manager.task("blocker").status == TaskStatus.COMPLETED
        assert not continued
        assert all(future.done() for future in manager._futures.values())
        assert all(row["status"] != "pending" for row in manager.snapshot()["requests"].values())
        assert any(event["type"] == "task_deferred" and event["task_id"] == "queued"
                   for event in manager.snapshot()["events"])
        with pytest.raises(OrchestrationError, match="closed"):
            manager.respond_owner_request(request["request_id"], {})
    finally:
        release.set()
        manager.close()
        if closer is not None:
            closer.join(5)

    recovered = OrchestrationManager(tmp_path, deck_id="close-waiters", max_workers=1)
    try:
        recovered.register_runner("slide", lambda *_: "resumed after reopening")
        recovered.register_runner("image", lambda *_: "started after reopening")
        drain(recovered)
        assert recovered.task("page").owner_id == manager.task("page").owner_id
        assert recovered.task("page").attempt == manager.task("page").attempt
        assert recovered.task("queued").status == TaskStatus.COMPLETED
    finally:
        recovered.close()


def test_cancelled_waiting_owner_does_not_resume_or_hold_a_slot(tmp_path):
    continued = []

    def slide(task, manager):
        manager.request_owner_assistance(task.task_id, request_type="help", detail="source")
        continued.append(task.task_id)

    manager = OrchestrationManager(tmp_path, deck_id="cancel-waiter", max_workers=1)
    manager.register_runner("slide", slide)
    manager.register_runner("research", lambda *_: "ready")
    try:
        manager.submit_tasks([{"task_id": "page", "role": "slide", "pages": [1]}])
        wait_for(manager, lambda: bool(manager._state["requests"]))
        with manager._condition:
            tasks = manager._task_map_locked()
            task = tasks["page"]
            tasks["page"] = replace(task, status=TaskStatus.CANCELLED)
            manager._set_task_map_locked(tasks)
            manager.registry.transition(task.owner_id, attempt=task.attempt, status=OwnerStatus.CANCELLED)
            manager._persist_locked()
            manager._condition.notify_all()
        manager._futures["page"].result(timeout=5)
        manager.submit_tasks([{"task_id": "research", "role": "research"}])
        drain(manager)
        assert manager.task("page").status == TaskStatus.CANCELLED
        assert not continued
        assert next(iter(manager.snapshot()["requests"].values()))["status"] == "cancelled"
    finally:
        manager.close()


def test_dependency_failure_is_reported_while_original_owner_waits(tmp_path):
    def slide(task, manager):
        return manager.request_owner_assistance(task.task_id, request_type="help", detail="source")

    def research(*_):
        raise ValueError("source unavailable")

    manager = OrchestrationManager(tmp_path, deck_id="failed-help-dependency", max_workers=1)
    manager.register_runner("slide", slide)
    manager.register_runner("research", research)
    manager.register_runner("image", lambda *_: pytest.fail("failed dependency was executed"))
    try:
        manager.submit_tasks([{"task_id": "page", "role": "slide", "pages": [1]}])
        wait_for(manager, lambda: bool(manager._state["requests"]))
        manager.submit_tasks([
            {"task_id": "research", "role": "research"},
            {"task_id": "image", "role": "image", "depends_on": ["research"]},
        ])
        manager._futures["research"].result(timeout=5)
        assert manager.task("research").status == TaskStatus.FAILED
        assert manager.task("image").status == TaskStatus.SKIPPED
        assert manager.task("page").status == TaskStatus.WAITING
        request = next(iter(manager.snapshot()["requests"].values()))
        manager.respond_owner_request(request["request_id"], {"resolution": "report evidence gap"})
        drain(manager)
        assert manager.task("page").result == {"resolution": "report evidence gap"}
    finally:
        manager.close()


@pytest.mark.parametrize("after_help", [False, True])
def test_thread_start_failure_closes_future_and_resumes_same_owner(tmp_path, monkeypatch, after_help):
    calls = []
    start = threading.Thread.start
    attempts = []

    def failing_start(thread):
        attempts.append(thread.name)
        if thread.name == "mural-owner-b-source":
            raise RuntimeError("can't start new thread")
        return start(thread)

    def slide(task, manager):
        if task.task_id == "a-page":
            manager.request_owner_assistance(task.task_id, request_type="help", detail="source")
        calls.append(task.task_id)

    manager = OrchestrationManager(tmp_path, deck_id="thread-resource", max_workers=1)
    manager.register_runner("research", lambda task, _: calls.append(task.task_id))
    manager.register_runner("slide", slide)
    manager.register_runner("image", lambda task, _: calls.append(task.task_id))
    specs = [
        {"task_id": "b-source", "role": "research"},
        {"task_id": "image", "role": "image", "depends_on": ["b-source"]},
        {"task_id": "z-later", "role": "slide", "pages": [2]},
    ]
    if after_help:
        specs.insert(0, {"task_id": "a-page", "role": "slide", "pages": [1]})
    try:
        with monkeypatch.context() as patch:
            patch.setattr(threading.Thread, "start", failing_start)
            manager.submit_tasks(specs)
            manager._futures["b-source"].result(timeout=5)
            source = manager.task("b-source")
            assert source.status == TaskStatus.INTERRUPTED
            assert manager.registry.get(source.owner_id).status == OwnerStatus.INTERRUPTED
            assert "owner_thread_start_failed" in source.error and "can't start new thread" in source.error
            assert "b-source" not in manager._execution_tasks
            assert "b-source" not in manager._threads
            assert "b-source" not in calls and "image" not in calls
            assert attempts.count("mural-owner-b-source") == 1
            assert manager.task("image").status == TaskStatus.WAITING_DEPENDENCY
            with pytest.raises(ResourceSlotTimeout, match="resume the same workspace"):
                manager.wait(timeout_s=.1)
            assert not any(event["type"] in {"task_failed", "task_skipped", "owner_registry_conflict"}
                           or (event["type"] == "task_started" and event["task_id"] == "b-source")
                           for event in manager.snapshot()["events"])
    finally:
        manager.close()
    assert all(future.done() for future in manager._futures.values())

    recovered = OrchestrationManager(tmp_path, deck_id="thread-resource", max_workers=1)
    try:
        for role in ("research", "slide", "image"):
            recovered.register_runner(role, lambda task, _: calls.append(task.task_id))
        drain(recovered)
        resumed = recovered.task("b-source")
        assert (resumed.owner_id, resumed.attempt) == (source.owner_id, source.attempt)
        assert resumed.status == TaskStatus.COMPLETED and resumed.error == ""
        assert "b-source" in calls and "image" in calls and "z-later" in calls
    finally:
        recovered.close()
