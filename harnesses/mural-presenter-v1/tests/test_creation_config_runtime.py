"""当前创作配置复用固定 Skill 协议；不访问模型或浏览器服务。"""
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from export.delivery import DeliveryError, DeliveryExporter, _presentation_metadata
from owner.context import OwnerPacket
from runtime.production import ProductionSettings, SkillSnapshot, read_scopes_for_role, write_scopes_for_role
from runtime.config import RunSpec
from runtime.session import ProviderChannel
from runtime.shared_objects import SharedSkillStore
from runtime.skill_runtime import SkillRuntime, SkillRuntimeError
from runtime.tools import ToolContext, ToolExecutionError
from state.artifacts import ArtifactStateError
from state.workspace import PageLifecycle, WorkspaceState, WorkspaceStateError
from state.workspace import file_sha256
from test_direct_authoring import direct_authoring, _execute, _inspect_direct
from test_inspection_transactions import ScriptedTransport, _call, _response, _run


def _config(state, **changes):
    value = {**state.workspace.creation_config(), **changes}
    result = _execute(state, "write_file", {"path": "config.json", "content": json.dumps(value)},
                      "config-" + str(len(state.kernel.tools.events)))
    assert result.ok, result.content
    return value


@pytest.mark.parametrize("role", ["orchestrator", "research", "image", "slide"])
def test_config_permissions_are_current_and_role_scoped(role):
    context = ToolContext("orchestrator" if role == "orchestrator" else "owner", role,
        read_scopes=read_scopes_for_role(role, skill_name="mural-presenter-v1", pages=[1]),
        write_scopes=write_scopes_for_role(role, pages=[1]))
    assert context.may_read("config.json")
    assert context.may_write("config.json") == (role == "orchestrator")


@pytest.mark.parametrize("path", ["config.json", "./config.json"])
@pytest.mark.parametrize("payload", ["not json", "{}", '{"working_language":"en","presentation_language":"en","target_page_count":1,"provider":"unsafe"}'])
def test_invalid_config_write_is_atomic(direct_authoring, path, payload):
    state = direct_authoring()
    before = (state.workspace.paths.root / "config.json").read_bytes()
    result = _execute(state, "write_file", {"path": path, "content": payload}, "bad-config")
    assert not result.ok and result.error_code == "creation_config_invalid"
    assert (state.workspace.paths.root / "config.json").read_bytes() == before
    state.kernel.tools.assert_closed()


def test_invalid_config_patch_and_expert_writes_preserve_original(direct_authoring):
    state = direct_authoring()
    path = state.workspace.paths.root / "config.json"
    before = path.read_bytes()
    failed = _execute(state, "patch", {"path": "config.json", "old_text": '"target_page_count": null',
        "new_text": '"target_page_count": false'}, "bad-patch")
    assert not failed.ok and failed.error_code == "creation_config_invalid"
    assert path.read_bytes() == before
    expert = ToolContext("research-owner", "research", write_scopes=write_scopes_for_role("research"))
    denied = _execute(state, "write_file", {"path": "config.json", "content": before.decode()}, "expert-write", expert)
    assert not denied.ok and denied.error_code == "write_scope_denied"
    assert path.read_bytes() == before


def test_working_language_does_not_invalidate_pixels_or_inspection(direct_authoring):
    state = direct_authoring()
    _inspect_direct(state)
    state.kernel.services.complete_deck()
    before = state.workspace.page(1)
    _config(state, working_language="zh")
    state.kernel.artifacts.validate_render_receipt(1)
    assert state.workspace.page(1) == before
    result = _execute(state, "inspect", {"page": [1]}, "same-pixels")
    assert result.ok and json.loads(result.content)["inspection_cached"]
    assert state.renderer_calls == ["slide_01.html"]
    state.kernel.services.complete_deck()


def test_presentation_change_requires_new_render_and_real_visual_consumption(direct_authoring):
    state = direct_authoring()
    _inspect_direct(state)
    state.kernel.services.complete_deck()
    before = state.workspace.page(1)
    old = next(iter(json.loads(state.workspace.path.read_text())["inspection_receipts"].values()))
    _config(state, presentation_language="zh")
    assert state.workspace.page(1).lifecycle == PageLifecycle.AUTHORED
    with pytest.raises(ArtifactStateError, match="presentation language changed"):
        state.kernel.artifacts.validate_render_receipt(1)
    with pytest.raises(ToolExecutionError):
        state.kernel.services.complete_deck()
    result = _execute(state, "inspect", {"page": [1]}, "new-language-pixels")
    receipt = json.loads(result.content)
    assert result.ok and not receipt["inspection_cached"] and receipt["native_multimodal"]
    assert state.workspace.page(1).png_sha256 == before.png_sha256
    assert state.workspace.page(1).lifecycle == PageLifecycle.RENDERED
    assert len(state.renderer_calls) == 2
    with pytest.raises(WorkspaceStateError, match="presentation language changed"):
        state.workspace.mark_inspected(1, inspection_id=old["inspection_id"], expected=old, receipt=old)
    assert not any("config.json" in hashes for hashes in
        [state.kernel.artifacts.render_receipt(1)["resource_hashes"]])
    _run(ScriptedTransport([_call("inspect", {"page": [1]}, "consume-new-language"), _response()]),
         state.kernel.tools, state.context)
    assert state.workspace.page(1).inspection_id != before.inspection_id
    assert state.kernel.services.complete_deck()["status"] == "ready"


def test_target_count_changes_do_not_edit_pages_but_block_completion_and_export(direct_authoring):
    state = direct_authoring()
    _inspect_direct(state)
    state.kernel.services.complete_deck()
    before = state.workspace.page(1)
    _config(state, target_page_count=2)
    assert state.workspace.pages() == (before,)
    with pytest.raises(ToolExecutionError, match="target_page_count=2"):
        state.kernel.services.complete_deck()
    with pytest.raises(DeliveryError, match="target_page_count=2"):
        DeliveryExporter(state.workspace, state.kernel.artifacts, state.kernel.issues)._validate_pages()
    _config(state, target_page_count=1)
    assert state.kernel.services.complete_deck()["status"] == "ready"


def test_packets_recovery_and_export_use_current_config_not_run_spec(direct_authoring):
    state = direct_authoring()
    _inspect_direct(state)
    current = _config(state, working_language="zh", presentation_language="en", speech_language="zh", target_page_count=1)
    stale = replace(state.kernel.run_spec, language="zh", page_count_hint=99)
    owner = state.kernel.owners.page_owner(1)
    packet = OwnerPacket.compile(owner, stale)
    assert packet.to_dict()["creation_config"] == current
    assert "language" not in packet.to_dict()
    assert OwnerPacket.from_dict(packet.to_dict()) == packet
    hashes = SkillRuntime._orchestrator_artifact_hashes(state.runtime, state.kernel.services)
    assert "config.json" in hashes
    (state.workspace.paths.root / "present.html").write_text('<html lang="fr"><title>Title</title></html>')
    assert _presentation_metadata(state.workspace.paths)["language"] == "en"


def test_old_workspace_without_config_does_not_backfill_or_modify(direct_authoring):
    state = direct_authoring()
    (state.workspace.paths.root / "config.json").unlink()
    before = {path.relative_to(state.workspace.paths.root): path.read_bytes()
              for path in state.workspace.paths.root.rglob("*") if path.is_file()}
    with pytest.raises(SkillRuntimeError, match="creation_config_missing"):
        SkillRuntime(state.kernel.run_spec, {}, validate_environment=False)
    after = {path.relative_to(state.workspace.paths.root): path.read_bytes()
             for path in state.workspace.paths.root.rglob("*") if path.is_file()}
    assert before == after


def test_original_run_manifest_is_not_rewritten_on_resume(direct_authoring):
    state = direct_authoring()
    path = state.workspace.paths.state / "run.json"
    before = path.read_bytes()
    state.kernel.run_spec = replace(state.kernel.run_spec, language="zh", page_count_hint=15)
    state.kernel._write_run_manifest()
    assert path.read_bytes() == before


def test_runtime_seeds_only_new_workspace_and_resume_keeps_current_config(direct_authoring, tmp_path, monkeypatch):
    state = direct_authoring()
    root = tmp_path / "new-workspace"
    paths = WorkspaceState(root).paths
    store = SharedSkillStore.for_workspace(root)
    source = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1"
    ref = store.pin(paths, source, skill_name="mural-presenter-v1")
    snapshot = SimpleNamespace(target=ref.object_root, tree_sha256=ref.tree_sha256,
        main_sha256="main", role_sha256={}, preparation_timings_ms={})
    monkeypatch.setattr(SkillSnapshot, "stage", lambda *_: snapshot)
    spec = RunSpec.from_inputs(raw_user_query="中文协作，英文页面", output_workspace=root,
        language="en", page_count_hint=2)
    options = dict(settings=ProductionSettings(skill_source=source), transport=object(),
        channels=(ProviderChannel("test", "fixture"),), renderer=object(), validate_environment=False)
    SkillRuntime(spec, {}, **options)
    assert json.loads((root / "config.json").read_text()) == {
        "working_language": None, "presentation_language": "en", "target_page_count": 2}
    chosen = {"working_language": "zh", "presentation_language": "en", "target_page_count": 1}
    (root / "config.json").write_text(json.dumps(chosen))
    SkillRuntime(replace(spec, language="zh", page_count_hint=8), {}, **options)
    assert json.loads((root / "config.json").read_text()) == chosen


def test_critic_only_resume_rejects_changed_creation_config(direct_authoring):
    state = direct_authoring()
    root = state.workspace.paths.root
    (root / "result.json").write_text(json.dumps({"exit_reason": "critic_incomplete",
        "external_visual_critic": {"critic_incomplete": True}}))
    host = root / "_state/skill-runtime.json"
    host.write_text(json.dumps({"status": "completed", "checkpoint_attachments": [],
        "creation_config_sha256": file_sha256(root / "config.json"), "outcome": {"prior": True}}))
    runtime = SimpleNamespace(prepared=SimpleNamespace(run_spec=state.kernel.run_spec),
        _host_state=host, _checkpoint_attachments=[])
    assert SkillRuntime._load_critic_resume_outcome(runtime) == {"prior": True}
    _config(state, presentation_language="zh")
    assert SkillRuntime._load_critic_resume_outcome(runtime) is None
