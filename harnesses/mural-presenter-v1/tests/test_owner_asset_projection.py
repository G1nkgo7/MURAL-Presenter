"""Owner 素材上下文只注入一次，并按显式页消费者投影与恢复。"""
from dataclasses import replace
import json
from pathlib import Path
import threading
from types import SimpleNamespace

from PIL import Image
import pytest
from geometry_fixture import geometry, write_geometry

from orchestration.manager import TaskRecord, TaskStatus
from owner.context import OwnerContextStore
from owner.registry import OwnerRegistry
from runtime.attachments import AttachmentStager
from runtime.config import RunSpec
from runtime.production import read_scopes_for_role
from runtime.recovery import SegmentCheckpoint
from runtime.skill_runtime import SkillRuntime, SkillRuntimeError
from runtime.tools import ToolContext
from state.artifacts import ArtifactStore
from state.workspace import WorkspaceState


ASSETS = {
    "page-one": {"slides": [1], "crop": "left subject", "notes": "unique-page-one-detail"},
    "group-two-three": {"slides": [2, 3], "source": "https://example.test/group"},
    "other-page": {"slides": [9], "purpose": "page nine only"},
    "unassigned": {"slides": [], "purpose": "available for shared use"},
    "unknown-consumers": {"notes": "consumer assignment has not been recorded"},
    "no-metadata": {},
}
DISCOVERABLE = {"unassigned", "unknown-consumers", "no-metadata"}
ALPHA = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1"


def _setup(tmp_path, monkeypatch, *, role="slide", pages=(1,)):
    write_geometry(tmp_path, geometry(width_px=1600, height_px=900))
    workspace = WorkspaceState(tmp_path)
    workspace.register_pages([1, 2, 3, 9])
    for page in (1, 2, 3, 9):
        workspace.paths.page_html(page).write_text(f'<section data-slide="{page:02d}"></section>\n', encoding="utf-8")
    store = ArtifactStore(tmp_path, workspace_state=workspace)
    for artifact_id, metadata in ASSETS.items():
        path = workspace.paths.assets / f"{artifact_id}.png"
        Image.new("RGB", (24, 16), "navy").save(path)
        store.record_image_result(artifact_id=artifact_id, owner_id="image-source", path=path,
            status="available", image_metadata=metadata)
    (workspace.paths.assets / "catalog.md").write_text("# Complete readable image catalog\n")
    owners = OwnerRegistry(tmp_path, deck_id=workspace.deck_id)
    owner = owners.accept(role=role, pages=pages, group_id="projection")
    owners.transition(owner.owner_id, attempt=owner.attempt, status="running")
    spec = RunSpec.from_inputs(raw_user_query="Make the selected pages", output_workspace=tmp_path)
    previous = TaskRecord("asset-source", "image-source", 1, "image", (), "asset-source", (),
                          TaskStatus.COMPLETED).to_dict()
    services = SimpleNamespace(workspace=workspace, artifacts=store, owners=owners, run_spec=spec,
        owner_contexts=OwnerContextStore(tmp_path), capture_training_segment=lambda *_a, **_k: None,
        orchestration=SimpleNamespace(snapshot=lambda: {"tasks": {"asset-source": previous}}, task=lambda _id: None))
    runtime = object.__new__(SkillRuntime)
    runtime._attachment_futures = {}
    runtime._attachment_stop = threading.Event()
    runtime.prepared = SimpleNamespace(snapshot=SimpleNamespace(name=ALPHA.name, target=ALPHA,
        tree_sha256="test-alpha-asset-projection", role_text=lambda _: "Owner role"),
        attachments=AttachmentStager(tmp_path).stage([]),
        settings=SimpleNamespace(owner_max_turns=4, max_turns=4, owner_max_tokens=256,
                                 thinking=False, reasoning_effort="low"))
    monkeypatch.setattr(runtime, "_owner_tool_context", lambda _services, task: ToolContext(
        task.owner_id, task.role, read_scopes=read_scopes_for_role(task.role,
            skill_name=ALPHA.name, pages=task.pages)))
    monkeypatch.setattr(runtime, "_record_result", lambda _result: None)
    calls = []
    def run(**kwargs):
        calls.append(kwargs)
        checkpoint = kwargs["checkpoint"] or SegmentCheckpoint.initial(kwargs["initial_messages"])
        return SimpleNamespace(checkpoint=checkpoint, final_text="Finished", review_handoff=None,
                               to_dict=lambda: {})
    task = TaskRecord("projection", owner.owner_id, owner.attempt, role, tuple(pages), "projection", (),
                      TaskStatus.RUNNING, output_path=owner.output_path)
    return runtime, services, task, SimpleNamespace(run=run), calls


@pytest.mark.parametrize("pages,missing_page", [((1,), 1), ((1, 2), 2)])
def test_missing_slide_scaffold_fails_before_owner_transfer_or_model_call(tmp_path, monkeypatch, pages, missing_page):
    runtime, services, task, agent, calls = _setup(tmp_path, monkeypatch, pages=pages)
    html = services.workspace.paths.page_html(missing_page)
    html.unlink()
    before = services.artifacts.path.read_bytes()
    monkeypatch.setattr(services.artifacts, "transfer_page_owner",
                        lambda *_args, **_kwargs: pytest.fail("missing scaffold must be checked before owner transfer"))
    with pytest.raises(SkillRuntimeError, match=rf"slide_preparation_missing: page {missing_page}: slides/slide_{missing_page:02d}.html"):
        runtime._owner_runner(services, agent, "slide")(task, None)
    assert calls == [] and not html.exists()
    assert services.artifacts.path.read_bytes() == before
    assert services.owner_contexts.load(task.owner_id) is None


@pytest.mark.parametrize("role,pages,assigned", [
    ("slide", (1,), {"page-one"}),
    ("slide", (1, 3), {"page-one", "group-two-three"}),
    ("image", (2,), {"group-two-three"}),
    ("image", (), set(ASSETS) - DISCOVERABLE),
])
def test_explicit_consumers_project_useful_records_and_keep_discovery(tmp_path, monkeypatch, role, pages, assigned):
    runtime, services, task, _agent, _calls = _setup(tmp_path, monkeypatch, role=role, pages=pages)
    catalog = services.artifacts.catalog()
    projected = runtime._artifact_refs(services, task)
    assert {item["artifact_id"] for item in projected} == assigned | DISCOVERABLE
    unassigned = next(item for item in projected if item["artifact_id"] == "unassigned")
    unknown = next(item for item in projected if item["artifact_id"] == "unknown-consumers")
    assert unassigned["image_metadata"]["slides"] == []
    assert "slides" not in unknown["image_metadata"]
    assert unassigned["pixel_width"] == 24 and unassigned["aspect_ratio"] == 1.5
    assert all(not ({"owner_id", "revision", "sha256", "source_hashes", "source_paths"} & set(item))
               for item in projected)
    unassigned["image_metadata"]["purpose"] = "only mutate this projection"
    assert services.artifacts.catalog() == catalog


def test_slide_projection_skips_reference_only_source_image_when_crop_is_available(tmp_path, monkeypatch):
    runtime, services, task, _agent, _calls = _setup(tmp_path, monkeypatch, role="slide", pages=(1,))
    for artifact_id, metadata in (
        ("hero-original", {
            "slides": [1],
            "purpose": "P1照片原图留存；实际版面优先 assets/hero-crop.png",
            "display": "原件留存，不与裁图重复上屏",
        }),
        ("hero-crop", {
            "slides": [1],
            "purpose": "P1推荐上屏裁图",
            "display": "用于当前页面主视觉",
        }),
    ):
        path = services.workspace.paths.assets / f"{artifact_id}.png"
        Image.new("RGB", (32, 18), "teal").save(path)
        services.artifacts.record_image_result(
            artifact_id=artifact_id,
            owner_id="image-source",
            path=path,
            status="available",
            image_metadata=metadata,
        )
    projected = runtime._artifact_refs(services, task)
    ids = {item["artifact_id"] for item in projected}
    assert "hero-crop" in ids
    assert "hero-original" not in ids


@pytest.mark.parametrize("role,pages", [("slide", (1,)), ("image", ())])
def test_owner_packet_reads_catalog_once_and_has_one_prompt_record_copy(tmp_path, monkeypatch, role, pages):
    runtime, services, task, agent, calls = _setup(tmp_path, monkeypatch, role=role, pages=pages)
    reads = []
    catalog = services.artifacts.catalog
    monkeypatch.setattr(services.artifacts, "catalog", lambda: reads.append(1) or catalog())
    runtime._owner_runner(services, agent, role)(task, None)
    assert reads == [1]
    packet = json.loads(calls[0]["initial_messages"][0]["content"])["owner_packet"]
    assert "image_catalog" not in packet["handoffs"]
    assert packet["handoffs"]["image_catalog_path"] == "assets/catalog.md"
    assert json.dumps(packet).count("unique-page-one-detail") == 1
    assert calls[0]["tool_context"].may_read("assets/catalog.md")
    persisted = services.owner_contexts.load(task.owner_id).packet.to_dict()
    assert persisted == packet


def test_research_does_not_load_or_receive_image_catalog(tmp_path, monkeypatch):
    role = "research"
    runtime, services, task, agent, calls = _setup(tmp_path, monkeypatch, role=role, pages=())
    monkeypatch.setattr(services.artifacts, "catalog", lambda: pytest.fail("unrelated role loaded image catalog"))
    runtime._owner_runner(services, agent, role)(task, None)
    packet = json.loads(calls[0]["initial_messages"][0]["content"])["owner_packet"]
    assert packet["artifact_refs"] == [] and "image_catalog" not in packet["handoffs"]


def test_resume_appends_changed_relevant_packet_once_but_not_unrelated_assets(tmp_path, monkeypatch):
    runtime, services, task, agent, calls = _setup(tmp_path, monkeypatch)
    runner = runtime._owner_runner(services, agent, "slide")
    runner(task, None)
    original = services.owner_contexts.load(task.owner_id).segment.messages
    services.artifacts.record_image_result(artifact_id="other-page", owner_id="image-source",
        path="assets/other-page.png", status="available", image_metadata={"slides": [9], "notes": "changed outside scope"})
    assert runner(task, None)["resume_mode"] == "resume"
    assert calls[-1]["checkpoint"].messages == original
    services.artifacts.record_image_result(artifact_id="page-one", owner_id="image-source",
        path="assets/page-one.png", status="available", image_metadata={"slides": [1], "crop": "new relevant crop"})
    assert runner(task, None)["resume_mode"] == "resume"
    changed = calls[-1]["checkpoint"].messages
    assert len(changed) == len(original) + 1 and changed[:-1] == original
    packet = json.loads(changed[-1]["content"])["owner_packet"]
    assert next(item for item in packet["artifact_refs"] if item["artifact_id"] == "page-one")["image_metadata"]["crop"] == "new relevant crop"
    runner(task, None)
    assert calls[-1]["checkpoint"].messages == changed
    assert services.owner_contexts.load(task.owner_id).packet.to_dict() == packet


def test_legacy_duplicate_catalog_packet_migrates_once_without_rewriting_history(tmp_path, monkeypatch):
    runtime, services, task, agent, calls = _setup(tmp_path, monkeypatch)
    runner = runtime._owner_runner(services, agent, "slide")
    runner(task, None)
    stored = services.owner_contexts.load(task.owner_id)
    legacy = replace(stored.packet, artifact_refs=tuple(services.artifacts.catalog()["images"]),
                     handoffs={**stored.packet.handoffs, "image_catalog": services.artifacts.catalog()})
    message = {"role": "user", "content": json.dumps({"owner_packet": legacy.to_dict()})}
    checkpoint = replace(stored.segment, messages=(message,))
    services.owner_contexts.save(owner=services.owners.get(task.owner_id), packet=legacy,
                                segment=checkpoint, artifact_hashes=stored.artifact_hashes)
    runner(task, None)
    resumed = calls[-1]["checkpoint"].messages
    assert len(resumed) == 2 and resumed[0] == message
    current = json.loads(resumed[1]["content"])["owner_packet"]
    assert "image_catalog" not in current["handoffs"]
    assert {item["artifact_id"] for item in current["artifact_refs"]} == {"page-one"} | DISCOVERABLE
    runner(task, None)
    assert calls[-1]["checkpoint"].messages == resumed
