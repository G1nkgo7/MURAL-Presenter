"""同角色独立执行、明确输出归属与显式交接；不访问模型服务。"""
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from PIL import Image

from orchestration.manager import OrchestrationError, OrchestrationManager, TaskRecord
from runtime.production import ProductionToolServices, read_scopes_for_role, write_scopes_for_role
from runtime.skill_runtime import SkillRuntime
from runtime.tools import ToolCall, ToolContext, ToolRuntime, register_tools
from state.artifacts import ArtifactStateError, ArtifactStore
from state.workspace import WorkspaceState
from test_orchestration_manager import drain


def test_two_research_and_two_image_tasks_overlap_with_independent_identities(tmp_path):
    WorkspaceState(tmp_path)
    manager = OrchestrationManager(tmp_path, deck_id="parallel", max_workers=4)
    barrier, release = threading.Barrier(5), threading.Event()
    calls = []

    def run(task, _):
        calls.append(task.task_id)
        barrier.wait(timeout=10)
        assert release.wait(10)
        return {"task": task.task_id}

    for role in ("research", "image"):
        manager.register_runner(role, run)
    specs = [{"task_id": f"{role}-{n}", "role": role} for role in ("research", "image") for n in (1, 2)]
    try:
        receipt = manager.submit_tasks(specs)
        barrier.wait(timeout=10)
        assert len(set(row["owner_id"] for row in receipt["tasks"])) == 4
        assert len({row["output_path"] for row in receipt["tasks"] if row["role"] == "research"}) == 2
        assert all(row["status"] == "running" for row in manager.snapshot()["tasks"].values())
        assert manager.submit_tasks(specs)["new_task_ids"] == []
        release.set()
        drain(manager)
        assert sorted(calls) == sorted(row["task_id"] for row in specs)
    finally:
        release.set()
        manager.close()


def test_research_output_collision_is_atomic_and_continuation_keeps_path(tmp_path):
    WorkspaceState(tmp_path)
    manager = OrchestrationManager(tmp_path, deck_id="research", max_workers=2)
    manager.register_runner("research", lambda *_: {})
    try:
        specs = [{"task_id": name, "role": "research", "output_path": "research/source.md"} for name in ("a", "b")]
        with pytest.raises(OrchestrationError, match="research_output_conflict"):
            manager.submit_tasks(specs)
        assert not manager.snapshot()["tasks"]
        first = manager.submit_tasks(specs[:1])["tasks"][0]
        drain(manager)
        resumed = manager.submit_tasks([{**specs[0], "task_id": "a2", "group_id": first["group_id"]}])["tasks"][0]
        drain(manager)
        assert resumed["owner_id"] == first["owner_id"] and resumed["attempt"] == 2
        assert TaskRecord.from_dict(resumed).output_path == "research/source.md"
        with pytest.raises(OrchestrationError, match="research_output_conflict"):
            manager.submit_tasks(specs[1:])
    finally:
        manager.close()


@pytest.mark.parametrize("path", ["knowledge.md", "../outside.md", "research/../../outside.md", "/outside.md", "research/result.json"])
def test_research_output_scope_rejects_invalid_paths_before_start(tmp_path, path):
    manager = OrchestrationManager(tmp_path, deck_id="paths")
    manager.register_runner("research", lambda *_: pytest.fail("must not execute"))
    try:
        with pytest.raises(OrchestrationError):
            manager.submit_tasks([{"task_id": "r", "role": "research", "output_path": path}])
        assert not manager.snapshot()["tasks"]
    finally:
        manager.close()


def test_research_writes_are_isolated_and_orchestrator_integrates_knowledge(tmp_path):
    workspace = WorkspaceState(tmp_path)
    backend = ProductionToolServices.__new__(ProductionToolServices)
    backend.workspace = workspace
    from owner.registry import OwnerRegistry
    backend.owners = OwnerRegistry(tmp_path, deck_id=workspace.deck_id)
    owners = [backend.owners.accept(role="research", group_id=name, output_path=f"research/{name}.md") for name in ("material", "public")]
    tools = ToolRuntime()
    register_tools(tools, {"write_file": backend.write_file, "read_file": backend.read_file}, require_complete=False)

    def context(owner_id, role, output=""):
        ctx = ToolContext(owner_id, role, read_scopes=read_scopes_for_role(role, skill_name="mural-presenter-v1"),
                          write_scopes=write_scopes_for_role(role, output_path=output))
        return replace(ctx, write_authorizer=lambda path: backend.may_write(ctx, path))

    contexts = [context(owner.owner_id, "research", owner.output_path) for owner in owners]
    for index, (owner, ctx) in enumerate(zip(owners, contexts)):
        assert tools.execute(ctx, ToolCall(f"write-{index}", "write_file", {"path": owner.output_path, "content": f"Evidence {index}: source, conditions"})).ok
        for denied in ("knowledge.md", owners[1-index].output_path):
            assert not tools.execute(ctx, ToolCall(f"deny-{index}-{denied}", "write_file", {"path": denied, "content": "overwrite"})).ok
    main = context("orchestrator", "orchestrator")
    for owner in owners:
        assert tools.execute(main, ToolCall("read-" + owner.group_id, "read_file", {"path": owner.output_path})).ok
        assert not main.may_write(owner.output_path)
    assert tools.execute(main, ToolCall("adopt", "write_file", {"path": "knowledge.md", "content": "Adopted evidence with conditions and research/material.md reference"})).ok
    assert WorkspaceState(tmp_path).paths.resolve("research/material.md").is_file()


def test_output_symlinks_and_existing_unassigned_draft_rejected(tmp_path):
    WorkspaceState(tmp_path)
    drafts = tmp_path / "research"
    drafts.mkdir()
    (drafts / "existing.md").write_text("do not overwrite")
    (drafts / "alias.md").symlink_to(drafts / "existing.md")
    manager = OrchestrationManager(tmp_path, deck_id="safe-path")
    manager.register_runner("research", lambda *_: {})
    try:
        for path in ("research/existing.md", "research/alias.md"):
            with pytest.raises(OrchestrationError):
                manager.submit_tasks([{"task_id": "r", "role": "research", "output_path": path}])
        assert not manager.snapshot()["tasks"]
        assert (drafts / "existing.md").read_text() == "do not overwrite"
    finally:
        manager.close()


def test_targeted_image_handoff_does_not_take_over_other_assets(tmp_path):
    workspace = WorkspaceState(tmp_path)
    store = ArtifactStore(tmp_path, workspace_state=workspace)
    manager = OrchestrationManager(tmp_path, deck_id=workspace.deck_id)
    manager.register_runner("image", lambda *_: {})
    try:
        old = manager.submit_tasks([{"task_id": "old", "role": "image"}])["tasks"][0]
        drain(manager)
        for name in ("cover", "figure"):
            Image.new("RGB", (8, 8), "red").save(tmp_path / f"assets/{name}.png")
            store.record_image_result(artifact_id=name, owner_id=old["owner_id"], path=f"assets/{name}.png", status="available")
        specs = [{"task_id": "new", "role": "image", "handoff_from": old["owner_id"], "asset_ids": ["cover"]}]
        new = manager.submit_tasks(specs)["tasks"][0]
        drain(manager)
        assert store.artifact("cover").owner_id == new["owner_id"]
        assert store.artifact("figure").owner_id == old["owner_id"]
        assert manager.submit_tasks(specs)["reused_task_ids"] == ["new"]
        with pytest.raises(ArtifactStateError):
            store.declare(artifact_id="cover", kind="image", path="assets/cover.png", owner_id=old["owner_id"])
    finally:
        manager.close()


def test_explicit_page_handoff_rejects_active_or_partial_group_and_survives_commit_crash(tmp_path, monkeypatch):
    workspace = WorkspaceState(tmp_path)
    workspace.register_pages([1, 2])
    manager = OrchestrationManager(tmp_path, deck_id=workspace.deck_id)
    release = threading.Event()
    manager.register_runner("slide", lambda *_: release.wait(10))
    try:
        old = manager.submit_tasks([{"task_id": "old", "role": "slide", "pages": [1, 2]}])["tasks"][0]
        spec = {"task_id": "new", "role": "slide", "pages": [1, 2], "handoff_from": old["owner_id"]}
        with pytest.raises(OrchestrationError):
            manager.submit_tasks([spec])
        release.set()
        drain(manager)
        with pytest.raises(OrchestrationError, match="complete previous page group"):
            manager.submit_tasks([{**spec, "pages": [1]}])
        persist = manager._set_task_map_locked
        def crash(_):
            raise RuntimeError("after ownership commit")
        monkeypatch.setattr(manager, "_set_task_map_locked", crash)
        with pytest.raises(RuntimeError, match="after ownership"):
            manager.submit_tasks([spec])
        monkeypatch.setattr(manager, "_set_task_map_locked", persist)
        new = manager.submit_tasks([spec])["tasks"][0]
        drain(manager)
        assert new["owner_id"] != old["owner_id"]
        assert manager.registry.page_owner(1).owner_id == new["owner_id"]
        assert any(owner.owner_id == old["owner_id"] for owner in manager.registry.list_archived())
    finally:
        release.set()
        manager.close()


def test_concurrent_research_cannot_claim_the_same_output(tmp_path):
    manager = OrchestrationManager(tmp_path, deck_id="output-race", max_workers=2)
    barrier, release = threading.Barrier(2), threading.Event()
    manager.register_runner("research", lambda *_: release.wait(30))
    def submit(task_id):
        barrier.wait(timeout=10)
        try:
            manager.submit_tasks([{"task_id": task_id, "role": "research", "output_path": "research/same.md"}])
            return True
        except OrchestrationError as exc:
            assert "research_output_conflict" in str(exc)
            return False
    try:
        with ThreadPoolExecutor(2) as pool:
            assert sorted(pool.map(submit, ["first", "second"])) == [False, True]
        assert len(manager.snapshot()["tasks"]) == 1
    finally:
        release.set()
        manager.close()


def test_research_handoff_preserves_output_and_revokes_previous_writer(tmp_path):
    workspace = WorkspaceState(tmp_path)
    manager = OrchestrationManager(tmp_path, deck_id=workspace.deck_id)
    def fail(*_):
        raise RuntimeError("previous research failed")
    manager.register_runner("research", fail)
    backend = ProductionToolServices.__new__(ProductionToolServices)
    backend.workspace, backend.owners = workspace, manager.registry
    release = threading.Event()
    try:
        old = manager.submit_tasks([{"task_id": "old", "role": "research", "output_path": "research/facts.md"}])["tasks"][0]
        drain(manager)
        drafts = tmp_path / "research"
        drafts.mkdir()
        (drafts / "facts.md").write_text("Current evidence and source boundaries")
        manager.register_runner("research", lambda *_: release.wait(30))
        new = manager.submit_tasks([{"task_id": "new", "role": "research", "handoff_from": old["owner_id"],
                                    "output_path": old["output_path"]}])["tasks"][0]
        assert new["owner_id"] != old["owner_id"]
        assert (drafts / "facts.md").read_text() == "Current evidence and source boundaries"
        assert not backend.may_write(ToolContext(old["owner_id"], "research"), old["output_path"])
        assert backend.may_write(ToolContext(new["owner_id"], "research"), new["output_path"])
        release.set()
        drain(manager)
        assert manager.wait(timeout_s=0)["unresolved_failures"] == []
        # 新任务再次续做时仍使用同一文件与身份。
        continued = manager.submit_tasks([{"task_id": "more", "role": "research", "group_id": new["group_id"],
            "output_path": new["output_path"]}])["tasks"][0]
        assert continued["owner_id"] == new["owner_id"]
        assert new["output_path"] in SkillRuntime._owner_artifact_paths(TaskRecord.from_dict(new))
    finally:
        release.set()
        manager.close()


def test_research_raw_segments_keep_separate_authors_and_immutable_drafts(tmp_path):
    from export.training import TrainingExporter
    from qc.routing import IssueLedger
    from runtime.recovery import SegmentCheckpoint
    workspace = WorkspaceState(tmp_path)
    exporter = TrainingExporter(workspace, ArtifactStore(tmp_path), IssueLedger(tmp_path), run_mode="synthesis")
    drafts = tmp_path / "research"
    drafts.mkdir()
    first_path, second_path = "research/material.md", "research/public.md"
    (tmp_path / first_path).write_text("Material method and source boundary")
    (tmp_path / second_path).write_text("Public definition and uncertainty")
    original = exporter.capture("research-material", "research", SegmentCheckpoint.initial(
        [{"role": "assistant", "content": "Material findings"}], segment_id="material-first"), artifact_paths=[first_path])
    exporter.capture("research-public", "research", SegmentCheckpoint.initial(
        [{"role": "assistant", "content": "Public findings"}], segment_id="public-first"), artifact_paths=[second_path])
    (tmp_path / first_path).write_text("Material explanation, revised")
    exporter.capture("research-material", "research", SegmentCheckpoint("material-next", "material-first",
        ({"role": "assistant", "content": "Targeted correction"},)), artifact_paths=[first_path])
    segments = {item.checkpoint.segment_id: item for item in exporter.load_segments()}
    assert len(segments) == 3
    assert segments["material-first"].artifact_hashes == original.artifact_hashes
    blob = original.artifact_blobs[first_path]
    assert workspace.paths.resolve(blob["path"]).read_text() == "Material method and source boundary"
    assert segments["material-next"].checkpoint.predecessor_segment_id == "material-first"
    assert segments["public-first"].checkpoint.predecessor_segment_id is None
    assert segments["public-first"].owner_id != segments["material-first"].owner_id
    assert all(item.role == "research" for item in segments.values())
