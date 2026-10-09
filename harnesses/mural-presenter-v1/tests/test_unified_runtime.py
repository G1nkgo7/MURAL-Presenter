"""统一配置、后台解析与角色合并的跨模块边界，不调用模型或外部解析服务。"""
from concurrent.futures import Future, ThreadPoolExecutor
import json
from pathlib import Path
import threading
from types import SimpleNamespace

from PIL import Image
import pytest

from orchestration.manager import OrchestrationManager, TaskStatus
from owner.context import OwnerContextStore
from runtime.attachments import AttachmentStager, _set_attachment_parse_status
from runtime.config import RunSpec, load_runtime_config
from runtime.production import ProductionSettings, read_scopes_for_role, write_scopes_for_role
from runtime.recovery import SegmentCheckpoint
from runtime.shared_objects import SharedSkillStore
from runtime.skill_runtime import SkillRuntime
from runtime.tools import ToolContext
from state.artifacts import ArtifactStore
from state.workspace import WorkspaceState
from test_orchestration_manager import drain


def configured(tmp_path, change):
    config = load_runtime_config()
    change(config)
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(config))
    return {"MURAL_RUNTIME_CONFIG": str(path)}


@pytest.mark.parametrize("change", [
    lambda c: c.pop("decks"),
    lambda c: c.update(extra={}),
    lambda c: c["decks"].update(owners=True),
    lambda c: c["decks"].update(per_model=13),
    lambda c: c["resources"].update(directory="relative"),
    lambda c: c["resources"].update(provider=0),
    lambda c: c["mineru"].update(endpoints=["https://user:secret@host", "http://cpu"]),
    lambda c: c["mineru"].update(endpoints=["http://host?key=secret", "http://cpu"]),
    lambda c: c["mineru"].update(endpoints=["http://host\n", "http://cpu"]),
    lambda c: c["mineru"].update(endpoints=["http://host:99999", "http://cpu"]),
    lambda c: c["mineru"].update(endpoints=["file:///host", "http://cpu"]),
    lambda c: c["mineru"].update(endpoints=["http://same", "http://same/"]),
    lambda c: c["mineru"]["cpu"].update(cores=3),
])
def test_single_config_rejects_invalid_shapes_types_and_urls(tmp_path, change):
    with pytest.raises(ValueError):
        load_runtime_config(configured(tmp_path, change))


def test_legacy_environment_cannot_override_governed_values(tmp_path):
    env = configured(tmp_path, lambda c: None)
    env.update(MODEL_CONCURRENCY="999", RENDER_CONCURRENCY="999", OWNER_CONCURRENCY="999",
               MURAL_MINERU_ENDPOINTS="http://ignored", MURAL_RESOURCE_SLOTS_DIR="/ignored")
    config = load_runtime_config(env)
    settings = ProductionSettings.from_environment(env)
    assert config["decks"] == {"total": 12, "per_model": 8, "owners": 12}
    assert (settings.model_concurrency, settings.render_concurrency) == (64, 32)
    assert config["mineru"]["gpu"] == {"concurrency": 4, "window": 64}
    assert config["mineru"]["cpu"] == {"concurrency": 4, "window": 16, "cores": 48}


def test_background_eight_attachments_queue_before_workers_and_do_not_block_agent(tmp_path, monkeypatch):
    root = tmp_path / "deck"
    workspace = WorkspaceState(root)
    sources = []
    for index in range(8):
        path = tmp_path / f"image-{index}.png"
        Image.new("RGB", (12, 12), (index * 20, 30, 40)).save(path)
        sources.append(path)
    manifest = AttachmentStager(root).stage(sources)
    runtime = SkillRuntime.__new__(SkillRuntime)
    runtime.prepared = SimpleNamespace(attachments=manifest, environment={})
    runtime._attachment_futures = {}
    runtime._attachment_stop = threading.Event()
    started, release, agent_started = threading.Event(), threading.Event(), threading.Event()
    guard = threading.Lock()
    active, peak = 0, 0

    def parse(root, attachment_id, **kwargs):
        nonlocal active, peak
        _set_attachment_parse_status(root, attachment_id, "processing")
        with guard:
            active += 1
            peak = max(peak, active)
            if active == 4:
                started.set()
        assert release.wait(30)
        with guard:
            active -= 1
        _set_attachment_parse_status(root, attachment_id, "completed")
        return {"status": "completed", "cached": False, "unresolved_items": []}

    monkeypatch.setattr("runtime.skill_runtime.parse_attachment", parse)
    manager = OrchestrationManager(root, deck_id="background")
    manager.register_runner("slide", lambda *_: agent_started.set() or {})
    services = SimpleNamespace(workspace=workspace, orchestration=manager,
                               live_trace=SimpleNamespace(append=lambda **_: None))
    try:
        runtime._start_attachment_parsing(services)
        assert started.wait(2)
        status = [r["parse_status"] for r in runtime._attachment_manifest()["records"]]
        assert status.count("processing") == 4 and status.count("queued") == 4
        assert manager.wait(timeout_s=.01) is None
        manager.submit_tasks([{"task_id": "independent", "role": "slide", "pages": [1]}])
        assert agent_started.wait(1)
        release.set()
        events = drain(manager, timeout=30)
        done = [event for event in events if event["type"] == "attachment_parse_finished"]
        assert len(done) == 8 and peak == 4
        assert all(not event["owner_id"] and not event["task_id"] for event in done)
        assert all(future.done() for future in runtime._attachment_futures.values())
        assert not (root / "knowledge.md").exists()
        assert not (root / "material.md").exists() and not (root / "research.md").exists()
    finally:
        release.set()
        runtime._attachment_stop.set()
        for future in runtime._attachment_futures.values():
            future.result(timeout=30)
        manager.close()


def test_background_completion_and_durable_event_are_one_wait_boundary(tmp_path, monkeypatch):
    manager = OrchestrationManager(tmp_path, deck_id="completion")
    future = Future()
    manager.background_pending = lambda: not future.done()
    writing, release = threading.Event(), threading.Event()
    persist = manager._persist_locked

    def write():
        if not writing.is_set():
            writing.set()
            assert release.wait(3)
        persist()

    monkeypatch.setattr(manager, "_persist_locked", write)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            publication = pool.submit(manager.publish_background_event, "attachment_parse_finished",
                {"attachment_id": "att-test", "status": "completed"}, on_published=lambda: future.set_result({}))
            assert writing.wait(1) and not future.done()
            waiter = pool.submit(manager.wait, timeout_s=2)
            assert not waiter.done()
            release.set()
            publication.result(timeout=2)
            update = waiter.result(timeout=2)
            assert update["all_terminal"]
            assert update["events"][0]["type"] == "attachment_parse_finished"
        restored = OrchestrationManager(tmp_path, deck_id="completion")
        assert restored.wait(after_sequence=0, timeout_s=0)["events"][0]["sequence"] == update["cursor"]
        restored.close()
    finally:
        release.set()
        manager.close()


@pytest.mark.parametrize("parse_state", ["queued", "running", "cancelled"])
def test_research_runs_independent_work_without_waiting_for_all_attachments(tmp_path, monkeypatch, parse_state):
    from infer import PresenterKernel
    workspace = WorkspaceState(tmp_path)
    SharedSkillStore.for_workspace(tmp_path).pin(workspace.paths,
        Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1", skill_name="mural-presenter-v1")
    (tmp_path / "config.json").write_text(json.dumps({"working_language": None,
        "presentation_language": None, "target_page_count": None}))
    attachments = AttachmentStager(tmp_path).stage([])
    spec = RunSpec.from_inputs(raw_user_query="Verify public terminology; material analysis is separate", output_workspace=tmp_path)
    pending = Future()
    if parse_state == "running":
        pending.set_running_or_notify_cancel()
    elif parse_state == "cancelled":
        pending.cancel()
    monkeypatch.setattr(pending, "result", lambda **_: pytest.fail("must not block on unrelated parsing"))
    runtime = SkillRuntime.__new__(SkillRuntime)
    runtime._attachment_futures = {"att-pending": pending}
    runtime._attachment_stop = threading.Event()
    runtime.prepared = SimpleNamespace(attachments=attachments,
        snapshot=SimpleNamespace(name="mural-presenter-v1", tree_sha256="test-research", release="test", role_text=lambda _: "Research"),
        settings=SimpleNamespace(owner_max_turns=4, max_turns=4, owner_max_tokens=256, thinking=False, reasoning_effort="low"))
    kernel = PresenterKernel(spec)
    manager, services = kernel.orchestration, kernel.services
    calls = []
    def agent_run(**kwargs):
        calls.append(kwargs)
        packet = json.loads(kwargs["initial_messages"][0]["content"])["owner_packet"]
        assert packet["handoffs"]["research_output_path"].startswith("research/")
        checkpoint = kwargs["checkpoint"] or SegmentCheckpoint.initial(kwargs["initial_messages"])
        return SimpleNamespace(checkpoint=checkpoint, final_text="Public facts checked; material pending", review_handoff=None, to_dict=lambda: {})
    monkeypatch.setattr(runtime, "_record_result", lambda _: None)
    monkeypatch.setattr(runtime, "_owner_tool_context", lambda _s, task: ToolContext(task.owner_id, task.role,
        write_scopes=write_scopes_for_role(task.role, output_path=task.output_path)))
    manager.register_runner("research", runtime._owner_runner(services, SimpleNamespace(run=agent_run), "research"))
    try:
        manager.submit_tasks([{"task_id": "public", "role": "research"}])
        drain(manager)
        assert manager.task("public").status == TaskStatus.COMPLETED, manager.task("public").error
        assert len(calls) == 1
        assert manager.task("public").result["output_recorded"] is False
    finally:
        manager.close()


def test_image_sequential_batches_keep_owner_and_accept_new_page_set(tmp_path):
    manager = OrchestrationManager(tmp_path, deck_id="sequential-images")
    seen = []
    manager.register_runner("image", lambda task, _: seen.append((task.owner_id, task.pages)) or {})
    try:
        for task_id, pages in (("first", [1]), ("second", [2, 3]), ("global", [])):
            manager.submit_tasks([{"task_id": task_id, "role": "image", "group_id": "images", "pages": pages}])
            drain(manager)
        assert len({owner for owner, _ in seen}) == 1
        assert [pages for _, pages in seen] == [(1,), (2, 3), ()]
    finally:
        manager.close()


def test_knowledge_is_orchestrator_owned_and_research_has_no_unscoped_write():
    for role in ("research", "image", "slide", "orchestrator"):
        context = ToolContext(role, role,
            read_scopes=read_scopes_for_role(role, skill_name="alpha", pages=[1],
                attachment_paths=["inputs/originals/att-012345abcdef/source.pdf"]),
            write_scopes=write_scopes_for_role(role, pages=[1]))
        assert context.may_read("knowledge.md")
        assert context.may_write("knowledge.md") == (role == "orchestrator")
        assert context.may_read("research/source.md") == (role in {"orchestrator", "research"})
        assert not context.may_write("inputs/extracted/att-012345abcdef/document.md")
        assert not context.may_read("inputs/originals/att-000000000000/private.pdf")
        if role == "orchestrator":
            assert context.may_read("slides/slide_01.html")
            assert not context.may_write("slides/slide_01.html")
