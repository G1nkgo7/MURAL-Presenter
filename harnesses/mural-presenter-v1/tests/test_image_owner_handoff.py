from geometry_fixture import geometry, write_geometry
"""Image 可独立并发，同一素材仍只有一个写者。"""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from types import SimpleNamespace

from PIL import Image
import pytest

from test_orchestration_manager import drain
from orchestration.manager import OrchestrationError, OrchestrationManager
from runtime.config import RunSpec
from runtime.recovery import SegmentCheckpoint
from runtime.skill_runtime import SkillRuntime
from runtime.production import ProductionToolServices
from runtime.tools import ToolContext
from state.artifacts import ArtifactStateError, ArtifactStore
from state.workspace import WorkspaceState
from infer import PresenterKernel


def test_reviewed_image_wakes_orchestrator_before_batch_and_preserves_explicit_dependencies(tmp_path):
    from runtime.live_trace import LiveEventJournal
    from state.workspace import file_sha256
    workspace = WorkspaceState(tmp_path)
    artifacts = ArtifactStore(tmp_path, workspace_state=workspace)
    manager = OrchestrationManager(tmp_path, deck_id="incremental", max_workers=2)
    finish, image_started, first_slide, blocked_slide = (threading.Event() for _ in range(4))
    def image_runner(*_):
        image_started.set()
        assert finish.wait(20)
    def slide_runner(task, *_):
        (first_slide if task.pages == (1,) else blocked_slide).set()
    manager.register_runner("image", image_runner)
    manager.register_runner("slide", slide_runner)
    runtime = SkillRuntime.__new__(SkillRuntime)
    live = LiveEventJournal(workspace.paths.trace / "live-events.jsonl")
    services = SimpleNamespace(workspace=workspace, orchestration=manager, live_trace=live)
    try:
        manager.submit_tasks([{"task_id": "images", "role": "image", "group_id": "images"},
            {"task_id": "whole-batch", "role": "slide", "pages": [2], "depends_on": ["images"]}])
        assert image_started.wait(3)
        cursor = manager.snapshot()["sequence"]
        path = workspace.paths.assets / "hero.png"
        Image.new("RGB", (8, 8), "navy").save(path)
        row = artifacts.record_image_result(artifact_id="hero", owner_id=manager.task("images").owner_id,
            path="assets/hero.png", status="available", image_metadata={"slides": [1]},
            reviewed_sha256=file_sha256(path)).to_dict()
        runtime._publish_image_handoff(services, row)
        update = manager.wait(after_sequence=cursor, timeout_s=1)
        assert not update["all_terminal"]
        handoffs = [event for event in update["events"] if event["type"] == "image_handoff_ready"]
        assert len(handoffs) == 1 and handoffs[0]["consumer_pages"] == [1]
        manager.submit_tasks([{"task_id": "ready-page", "role": "slide", "pages": [1]}])
        assert first_slide.wait(3) and not blocked_slide.is_set()
        assert manager.task("whole-batch").dependencies == ("images",)
        runtime._publish_image_handoff(services, row)
        assert len([event for event in manager.snapshot()["events"] if event["type"] == "image_handoff_ready"]) == 1
        finish.set()
        assert blocked_slide.wait(3)
    finally:
        finish.set()
        manager.close()
    assert blocked_slide.is_set()
    restored = OrchestrationManager(tmp_path, deck_id="incremental", max_workers=2)
    services.orchestration = restored
    try:
        runtime._publish_image_handoff(services, row)
        assert len([event for event in restored.snapshot()["events"] if event["type"] == "image_handoff_ready"]) == 1
        Image.new("RGB", (8, 8), "red").save(path)
        runtime._publish_image_handoff(services, row)
        assert len([event for event in restored.snapshot()["events"] if event["type"] == "image_handoff_ready"]) == 1
        events = [json.loads(line) for line in live.path.read_text().splitlines()]
        assert len([event for event in events if event["event_type"] == "image_handoff_ready"]) == 1
    finally:
        restored.close()


def _image(workspace, store, owner_id="previous-image"):
    path = workspace.paths.assets / "hero.png"
    Image.new("RGB", (8, 8), "navy").save(path)
    store.record_image_result(artifact_id="hero", owner_id=owner_id,
                              path="assets/hero.png", status="available")
    workspace.paths.path("asset_catalog").write_text(
        "# Assets\n\n## hero\n- path: assets/hero.png\n- status: ready\n", encoding="utf-8")


def test_new_image_group_can_overlap_independent_writer(tmp_path):
    WorkspaceState(tmp_path)
    manager = OrchestrationManager(tmp_path, deck_id="test", max_workers=2)
    release, started = threading.Event(), threading.Event()

    def run(*_):
        started.set()
        return release.wait(5)

    manager.register_runner("image", run)
    try:
        manager.submit_tasks([{"task_id": "first", "role": "image", "group_id": "first"}])["tasks"][0]
        assert started.wait(3)
        assert manager.submit_tasks([{"task_id": "first", "role": "image", "group_id": "first"}])["reused_task_ids"] == ["first"]
        second = manager.submit_tasks([{"task_id": "independent", "role": "image"}])["tasks"][0]
        assert second["owner_id"] != manager.task("first").owner_id
        assert len(manager.snapshot()["tasks"]) == 2
    finally:
        release.set()
        manager.close()


def test_concurrent_image_submissions_create_independent_writers(tmp_path):
    WorkspaceState(tmp_path)
    manager = OrchestrationManager(tmp_path, deck_id="test", max_workers=2)
    start, finish = threading.Barrier(2), threading.Event()
    manager.register_runner("image", lambda *_: finish.wait(5))

    def submit(group):
        start.wait(timeout=5)
        try:
            manager.submit_tasks([{"task_id": group, "role": "image", "group_id": group}])
            return "accepted"
        except OrchestrationError:
            return "rejected"

    try:
        with ThreadPoolExecutor(max_workers=2) as callers:
            assert sorted(callers.map(submit, ["first", "second"])) == ["accepted", "accepted"]
        assert len(manager.snapshot()["tasks"]) == 2
        assert len(manager.registry.list_current()) == 2
    finally:
        finish.set()
        manager.close()


def test_explicit_image_transfer_preserves_identity_and_rejects_old_writer(tmp_path):
    workspace = WorkspaceState(tmp_path)
    store = ArtifactStore(tmp_path, workspace_state=workspace)
    _image(workspace, store)
    before = store.catalog()["images"][0]
    store.transfer_image_owners(owner_id="next-image", previous_owner_ids=["previous-image"], artifact_ids=["hero"])
    after = store.catalog()["images"][0]
    assert after["owner_id"] == "next-image"
    assert {k: v for k, v in before.items() if k != "owner_id"} == {
        k: v for k, v in after.items() if k != "owner_id"}
    with pytest.raises(ArtifactStateError, match="may not commit"):
        store.commit(owner_id="previous-image", artifact_ids=["hero"], source_hashes={})
    store.record_image_result(artifact_id="hero", owner_id="next-image",
                              path="assets/hero.png", status="available")
    recovered = ArtifactStore(tmp_path, workspace_state=workspace)
    assert recovered.catalog()["images"][0]["owner_id"] == "next-image"
    unchanged = recovered.path.read_bytes()
    recovered.transfer_image_owners(owner_id="next-image", previous_owner_ids=[], artifact_ids=["hero"])
    assert recovered.path.read_bytes() == unchanged
    with pytest.raises(ArtifactStateError, match="declared differently"):
        recovered.declare(artifact_id="hero", kind="image", path="assets/other.png",
                          owner_id="next-image")


@pytest.mark.parametrize("leased", [False, True])
def test_image_transfer_rejects_unknown_predecessor_and_open_lease(tmp_path, leased):
    workspace = WorkspaceState(tmp_path)
    store = ArtifactStore(tmp_path, workspace_state=workspace)
    _image(workspace, store)
    if leased:
        store.begin_transaction("previous-image", ["hero"])
    before = store.path.read_bytes()
    with pytest.raises(ArtifactStateError):
        store.transfer_image_owners(owner_id="next-image",
                                    previous_owner_ids=["previous-image"] if leased else [], artifact_ids=["hero"])
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("previous_failed", [False, True])
def test_new_image_runner_can_finalize_previous_owners_catalog(tmp_path, monkeypatch, previous_failed):
    spec = RunSpec.from_inputs(raw_user_query="one page", output_workspace=tmp_path,
                               environment={"MODEL_API_FORMAT": "openai", "MODEL": "fake"})
    kernel = PresenterKernel(spec)
    write_geometry(tmp_path, geometry(width_px=1280, height_px=720))
    manager = kernel.orchestration
    def initial_image(task, _manager):
        _image(kernel.workspace, kernel.artifacts, task.owner_id)
        if previous_failed:
            raise RuntimeError("previous image failed after writing its first asset")

    manager.register_runner("image", initial_image)
    initial = manager.submit_tasks([{"task_id": "first", "role": "image", "group_id": "first"}])["tasks"][0]
    drain(manager)
    old_owner = initial["owner_id"]
    runtime = SkillRuntime.__new__(SkillRuntime)
    runtime.prepared = SimpleNamespace(run_spec=spec, settings=SimpleNamespace(
        owner_max_turns=4, max_turns=4, owner_max_tokens=256, thinking=False, reasoning_effort=""),
        snapshot=SimpleNamespace(role_text=lambda _: "Image"))
    monkeypatch.setattr(runtime, "_staged_attachment_paths", lambda: ())
    monkeypatch.setattr(runtime, "_owner_tool_context", lambda _s, task: ToolContext(task.owner_id, "image"))
    monkeypatch.setattr(runtime, "_owner_handoffs", lambda *_: {})
    monkeypatch.setattr(runtime, "_record_result", lambda *_: None)
    production = ProductionToolServices.__new__(ProductionToolServices)
    production.workspace, production.artifacts = kernel.workspace, kernel.artifacts

    def run(**kwargs):
        owner = kwargs["agent_id"]
        assert owner != old_owner
        # 调度器已转交登记；finalize 不再反向解析可读 Markdown。
        assert production.artifacts.catalog()["images"][0]["owner_id"] == owner
        production.artifacts.record_image_result(artifact_id="hero", owner_id=owner,
            path="assets/hero.png", status="available")
        return SimpleNamespace(checkpoint=SegmentCheckpoint("image-repair", None, ()),
                               final_text="ready", review_handoff=None, to_dict=lambda: {})

    manager.register_runner("image", runtime._owner_runner(kernel.services, SimpleNamespace(run=run), "image"))
    try:
        repair = manager.submit_tasks([{"task_id": "repair", "role": "image", "group_id": "repair",
            "handoff_from": old_owner, "asset_ids": ["hero"]}])["tasks"][0]
        drain(manager)
        task = manager.task(repair["task_id"])
        assert task.status.value == "succeeded", task.error
        assert kernel.artifacts.catalog()["images"][0]["owner_id"] == repair["owner_id"]
    finally:
        manager.close()
