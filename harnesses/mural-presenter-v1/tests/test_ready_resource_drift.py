"""资源原路径内容漂移必须在 ready / commit 成功记录之前被拒绝。"""
import hashlib
import json
import os
import time
from types import SimpleNamespace

import pytest

from infer import PresenterKernel
from orchestration.manager import TaskRecord, TaskStatus
from owner.registry import OwnerRegistry
from runtime.config import RunSpec
from runtime.shared_objects import SharedSkillStore, SharedObjectError
from runtime.tools import ToolCall, ToolExecutionError
from state.revisions import MARKER, RevisionTransaction
from state.artifacts import ArtifactStore
from state.workspace import PageLifecycle, WorkspaceState, WorkspaceStateError
from test_owner_continuation import continuation
from test_workspace_artifacts import _commit_page, _inspected_render_fixture, _register_image, _write_png


def change_resource(workspace, resource):
    path = workspace.paths.root / resource
    if resource.endswith(".png"):
        _write_png(path, (100, 20, 40))
    else:
        path.write_text(path.read_text() + "\nmain {color: red}", encoding="utf-8")


def _inspected_deck(root, count):
    workspace, store, renderer, _ = _inspected_render_fixture(root)
    for number in range(2, count + 1):
        workspace.register_pages([number])
        workspace.paths.page_html(number).write_text(workspace.paths.page_html(1).read_text() + f"<p>Page {number}</p>")
        _commit_page(workspace, store, page=number)
        store.render_pages([number], renderer)
        store.record_inspection(number, inspection_id=f"inspection-{number}")
    return workspace, store, renderer


def test_verification_validates_each_page_once_and_writes_state_once(tmp_path, monkeypatch, record_property):
    from PIL import Image
    from state.workspace import file_sha256

    workspace, _, _ = _inspected_deck(tmp_path, 12)
    pages = workspace.pages()
    sheet_path = workspace.paths.renders / 'ready-contact-sheet.png'
    sheet = Image.new('RGB', (640, 360))
    for index, page in enumerate(pages):
        with Image.open(workspace.paths.resolve(page.png_path)) as image:
            image.thumbnail((160, 120))
            sheet.paste(image, ((index % 4) * 160, (index // 4) * 120))
    sheet.save(sheet_path)
    workspace.record_group_inspection({
        'kind': 'group', 'owner_id': 'orchestrator', 'role': 'orchestrator',
        'inspection_id': 'ready-fixture', 'scope_key': 'ready-fixture-group',
        'path': workspace.paths.relative(sheet_path), 'sha256': file_sha256(sheet_path),
        'presentation_config': workspace.presentation_config(),
        'members': [{key: getattr(page, key) for key in
                     ('page', 'page_uid', 'html_sha256', 'png_sha256')} for page in pages],
    })
    kernel = PresenterKernel(RunSpec.from_inputs(raw_user_query="12 pages", output_workspace=tmp_path))
    checked, writes, programs = [], [], []
    validate, write = ArtifactStore.validate_render_receipt, kernel.workspace._write_state
    load = SharedSkillStore.load_ref
    def validate_once(store, number, **kwargs):
        checked.append(number)
        return validate(store, number, **kwargs)
    def write_once(state):
        writes.append(True)
        return write(state)
    def load_once(store, *args, **kwargs):
        programs.append(True)
        return load(store, *args, **kwargs)
    monkeypatch.setattr(ArtifactStore, "validate_render_receipt", validate_once)
    monkeypatch.setattr(kernel.workspace, "_write_state", write_once)
    monkeypatch.setattr(SharedSkillStore, "load_ref", load_once)
    try:
        started = time.monotonic()
        result = kernel.services.complete_deck()
        record_property("ready_seconds", time.monotonic() - started)
        record_property("resource_validations", len(checked))
        record_property("workspace_writes", len(writes))
        record_property("skill_reference_loads", len(programs))
        assert result["status"] == "ready"
        assert checked == list(range(1, 13))
        assert len(writes) == 1
        assert len(programs) == 2  # 固定配置解析器、字体解析器各加载一次，不随页数重复。
        assert all(page.lifecycle == PageLifecycle.READY for page in kernel.workspace.pages())
        checked.clear()
        writes.clear()
        result = kernel.services.complete_deck()
        assert result["status"] == "ready"
        assert checked == list(range(1, 13)) and writes == []
        assert len(programs) == 2
    finally:
        kernel.orchestration.close(wait=True)


def test_batch_ready_rejects_last_members_resource_without_partial_state(tmp_path):
    workspace, store, renderer = _inspected_deck(tmp_path, 3)
    image = workspace.paths.assets / "third.png"
    _write_png(image)
    _register_image(store, image)
    workspace.paths.page_html(3).write_text('<main>Third page</main><img src="../assets/third.png">')
    _commit_page(workspace, store, page=3)
    store.render_pages([3], renderer)
    store.record_inspection(3, inspection_id="third-inspected")
    before = workspace.path.read_bytes()
    change_resource(workspace, "assets/third.png")
    with pytest.raises(WorkspaceStateError, match="third.png"):
        workspace.mark_ready_pages((1, 2, 3))
    assert workspace.path.read_bytes() == before
    assert all(page.lifecycle == PageLifecycle.INSPECTED for page in workspace.pages())


def test_geometry_program_is_reused_but_css_and_resume_are_rechecked(tmp_path, monkeypatch):
    from state import workspace as workspace_module
    _inspected_render_fixture(tmp_path)
    load, calls = workspace_module.workspace_skill_module, []
    def counted(root, name):
        calls.append(name)
        return load(root, name)
    monkeypatch.setattr(workspace_module, "workspace_skill_module", counted)
    workspace = WorkspaceState(tmp_path)
    assert workspace.require_geometry()["height_px"] == 1200
    css = tmp_path / "base.css"
    css.write_text(css.read_text().replace("--canvas-h: 1200px", "--canvas-h: 900px"))
    assert workspace.require_geometry()["height_px"] == 900
    assert calls == ["geometry"]
    resumed = WorkspaceState(tmp_path)
    assert resumed.require_geometry()["height_px"] == 900
    assert calls == ["geometry", "geometry"]
    ref = json.loads(workspace.paths.skill_ref.read_text())
    ref["tree_sha256"] = "0" * 64
    workspace.paths.skill_ref.write_text(json.dumps(ref))
    with pytest.raises(SharedObjectError):
        WorkspaceState(tmp_path).require_geometry()


@pytest.mark.parametrize("defect", ["font_bytes", "font_authorization"])
def test_ready_shares_font_program_without_caching_content_or_authorization(tmp_path, monkeypatch, defect):
    from runtime import shared_fonts
    workspace, store, renderer = _inspected_deck(tmp_path, 2)
    shared = SharedSkillStore.for_workspace(tmp_path).studio_data
    data = b"ready-font-fixture"
    digest = hashlib.sha256(data).hexdigest()
    font = shared / "shared/fonts/objects" / f"{digest}.ttf"
    font.parent.mkdir(parents=True, exist_ok=True)
    font.write_bytes(data)
    manifest = {"schema": "mural.font-manifest.v2", "studio_data_root": str(shared),
                "source_faces": [{"sha256": digest, "object_ref": font.relative_to(shared).as_posix(), "scope": "public"}]}
    workspace.paths.font_manifest.write_text(json.dumps(manifest))
    css = tmp_path / "base.css"
    css.write_text(css.read_text() + f'\n@font-face{{font-family:"fixture";src:url("{font.as_uri()}")}}')
    factory, loads, checked = shared_fonts.font_reference_resolver, [], []
    def counted(root):
        loads.append(root)
        resolve = factory(root)
        def current(reference):
            checked.append(reference)
            return resolve(reference)
        return current
    monkeypatch.setattr(shared_fonts, "font_reference_resolver", counted)
    store.render_pages([1, 2], renderer)
    for number in (1, 2):
        store.record_inspection(number, inspection_id=f"font-inspection-{number}")
    workspace.mark_ready_pages((1, 2))
    workspace.assert_ready_resources((1, 2))
    assert loads == [tmp_path]
    assert ArtifactStore(tmp_path, workspace_state=workspace, create=False)._shared_font(font.as_uri()) == (font, digest)
    previous_checks = len(checked)
    before = workspace.path.read_bytes()
    if defect == "font_bytes":
        stamp = font.stat()
        font.write_bytes(b"x" * len(data))
        os.utime(font, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    else:
        manifest["source_faces"] = []
        workspace.paths.font_manifest.write_text(json.dumps(manifest))
    with pytest.raises(WorkspaceStateError, match="hash mismatch|not pinned"):
        workspace.mark_ready_pages((1, 2))
    assert workspace.path.read_bytes() == before
    assert len(checked) > previous_checks and loads == [tmp_path]
    with pytest.raises(WorkspaceStateError, match="hash mismatch|not pinned"):
        WorkspaceState(tmp_path).assert_ready_resources((1, 2))
    assert loads == [tmp_path, tmp_path]


@pytest.mark.parametrize('arguments', [
    {'status': 'issues_resolved', 'issues': [{'positions': [1], 'problem': 'Still wrong', 'suggested_fix': 'Fix it'}], 'resolved_issue_ids': ['missing']},
    {'status': 'issues_resolved', 'issues': [], 'resolved_issue_ids': ['missing-issue']},
])
def test_invalid_resolution_fails_before_reviews_resources_or_promotion(tmp_path, monkeypatch, arguments):
    workspace, _, _, _ = _inspected_render_fixture(tmp_path)
    kernel = PresenterKernel(RunSpec.from_inputs(raw_user_query='one page', output_workspace=tmp_path))
    calls = []
    monkeypatch.setattr(kernel.workspace, 'assert_ready_resources', lambda *_: calls.append('resources'))
    kernel.services.visual_review = lambda: calls.append('visual') or {}
    try:
        state_before, issues_before = workspace.path.read_bytes(), kernel.issues.path.read_bytes()
        result = kernel.tools.execute(kernel.services.orchestrator_tool_context(),
            ToolCall('invalid-ready', 'verification_submit', arguments))
        assert not result.ok and result.error_code == 'verification_rejected'
        assert calls == []
        assert workspace.path.read_bytes() == state_before
        assert kernel.issues.path.read_bytes() == issues_before
    finally:
        kernel.orchestration.close(wait=True)


@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("resource", ["assets/picture.png", "assets/theme.css", "base.css"])
def test_direct_ready_rejects_resource_drift_without_state_writes(tmp_path, ready, resource):
    workspace, store, renderer, _ = _inspected_render_fixture(tmp_path)
    if ready:
        workspace.mark_ready(1)
    change_resource(workspace, resource)
    before = workspace.path.read_bytes()
    with pytest.raises(WorkspaceStateError, match="visual resources changed.*render.*inspect") as error:
        workspace.mark_ready(1)
    assert resource in str(error.value)
    assert workspace.path.read_bytes() == before
    if resource.endswith(".png"):
        _register_image(store, workspace.paths.root / resource)
    store.render_pages([1], renderer)
    store.record_inspection(1, inspection_id="repaired-inspection")
    assert workspace.mark_ready(1).lifecycle == PageLifecycle.READY


@pytest.mark.parametrize("missing", ["assets/picture.png", "assets/theme.css", "_state/artifacts.json"])
def test_direct_ready_rejects_missing_resources_or_receipt_without_creating_it(tmp_path, missing):
    workspace, _store, _renderer, _ = _inspected_render_fixture(tmp_path)
    path = tmp_path / missing
    path.unlink()
    before = workspace.path.read_bytes()
    with pytest.raises(WorkspaceStateError, match="render.*inspect"):
        workspace.mark_ready(1)
    assert workspace.path.read_bytes() == before
    assert not path.exists()


@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("resource", ["assets/picture.png", "assets/theme.css"])
def test_verification_rejects_drift_before_success_ledger_and_recovers(tmp_path, ready, resource):
    workspace, store, renderer, _ = _inspected_render_fixture(tmp_path)
    if ready:
        workspace.mark_ready(1)
    kernel = PresenterKernel(RunSpec.from_inputs(raw_user_query="one page", output_workspace=tmp_path))
    try:
        change_resource(workspace, resource)
        before = kernel.issues.path.read_bytes()
        with pytest.raises(ToolExecutionError) as failed:
            kernel.services.complete_deck()
        assert failed.value.code == "verification_resources_stale"
        assert resource in str(failed.value) and "render" in str(failed.value) and "inspect" in str(failed.value)
        assert kernel.issues.path.read_bytes() == before
        if resource.endswith(".png"):
            _register_image(store, workspace.paths.root / resource)
        store.render_pages([1], renderer)
        store.record_inspection(1, inspection_id="repaired-inspection")
        assert kernel.services.complete_deck()["status"] == "ready"
        assert workspace.page(1).lifecycle == PageLifecycle.READY
        assert kernel.issues.path.read_bytes() != before
    finally:
        kernel.orchestration.close(wait=True)


def test_verification_checks_already_ready_members_before_promoting_other_pages(tmp_path):
    workspace, store, renderer, _ = _inspected_render_fixture(tmp_path)
    workspace.register_pages([2])
    workspace.paths.page_html(2).write_text('<img src="../assets/picture-two.png"><main>Second page</main>')
    _write_png(workspace.paths.assets / "picture-two.png")
    _register_image(store, workspace.paths.assets / "picture-two.png")
    _commit_page(workspace, store, page=2)
    store.render_pages([2], renderer)
    store.record_inspection(2, inspection_id="second-page-inspection")
    workspace.mark_ready(2)
    change_resource(workspace, "assets/picture-two.png")
    kernel = PresenterKernel(RunSpec.from_inputs(raw_user_query="two pages", output_workspace=tmp_path))
    try:
        before, ledger = workspace.path.read_bytes(), kernel.issues.path.read_bytes()
        with pytest.raises(ToolExecutionError) as failed:
            kernel.services.complete_deck()
        assert failed.value.code == "verification_resources_stale"
        assert "picture-two.png" in str(failed.value)
        assert workspace.path.read_bytes() == before and kernel.issues.path.read_bytes() == ledger
    finally:
        kernel.orchestration.close(wait=True)


@pytest.mark.parametrize("during", ["review", "promotion"])
def test_verification_has_no_success_ledger_when_resources_change_during_completion(tmp_path, monkeypatch, during):
    workspace, _store, _renderer, _ = _inspected_render_fixture(tmp_path)
    kernel = PresenterKernel(RunSpec.from_inputs(raw_user_query="one page", output_workspace=tmp_path))
    if during == "review":
        def review():
            change_resource(workspace, "assets/picture.png")
            return {"effective": True, "passed": True}
        kernel.services.visual_review = review
    else:
        original = kernel.workspace.mark_ready_pages
        def mark_ready(pages):
            change_resource(workspace, "assets/picture.png")
            return original(pages)
        monkeypatch.setattr(kernel.workspace, "mark_ready_pages", mark_ready)
    try:
        before = kernel.issues.path.read_bytes()
        with pytest.raises(ToolExecutionError) as failed:
            kernel.services.complete_deck()
        assert failed.value.code == "verification_resources_stale"
        assert kernel.issues.path.read_bytes() == before
    finally:
        kernel.orchestration.close(wait=True)


@pytest.mark.parametrize("ready", [False, True])
def test_owner_wrapper_rechecks_even_an_already_ready_page(tmp_path, ready):
    workspace, _store, _renderer, _ = _inspected_render_fixture(tmp_path)
    if ready:
        workspace.mark_ready(1)
    change_resource(workspace, "assets/picture.png")
    wrapped = PresenterKernel._wrap_runner(SimpleNamespace(workspace=workspace), lambda *_: {"status": "completed"})
    with pytest.raises(WorkspaceStateError, match="visual resources changed"):
        wrapped(SimpleNamespace(role="slide", pages=(1,)), None)


@pytest.mark.parametrize("ready", [False, True])
def test_skill_owner_completion_rechecks_resources_before_completed_result(tmp_path, monkeypatch, ready):
    runtime, services, agent, _calls, _old, _segment = continuation(tmp_path, monkeypatch)
    workspace, store, _renderer, _ = _inspected_render_fixture(tmp_path)
    services.workspace, services.artifacts = workspace, store
    monkeypatch.setattr(runtime, "_owner_handoffs", lambda *_: {})
    recorded = []
    monkeypatch.setattr(runtime, "_record_result", recorded.append)
    if ready:
        workspace.mark_ready(1)
    change_resource(workspace, "assets/picture.png")
    owner = services.owners.accept(role="slide", pages=[1], group_id="one")
    task = TaskRecord("repair", owner.owner_id, owner.attempt, "slide", (1,), "one", (), TaskStatus.RUNNING)
    with pytest.raises(WorkspaceStateError, match="visual resources changed"):
        runtime._owner_runner(services, agent, "slide")(task, None)
    assert recorded == []


@pytest.mark.parametrize("resource", ["assets/picture.png", "assets/theme.css"])
@pytest.mark.parametrize("ready", [False, True])
def test_revision_commit_checks_resources_before_assignment_or_snapshot_writes(tmp_path, resource, ready):
    workspace, store, renderer, _ = _inspected_render_fixture(tmp_path)
    if ready:
        workspace.mark_ready(1)
    impact_path = tmp_path / "_state/revisions/revision-1/impact.json"
    impact_path.parent.mkdir(parents=True)
    impact_path.write_text(json.dumps({"assignments": [{"status": "pending"}]}))
    (tmp_path / "_state/active-revision.json").write_text(json.dumps({"impact_path": impact_path.relative_to(tmp_path).as_posix()}))
    (tmp_path / "_state/deck-graph.json").write_text(json.dumps({"deck_version": 2}))
    transaction = RevisionTransaction(workspace, store, OwnerRegistry(tmp_path, deck_id=workspace.deck_id))
    change_resource(workspace, resource)
    before = impact_path.read_bytes()
    calls = []
    with pytest.raises(WorkspaceStateError, match="visual resources changed.*render.*inspect"):
        transaction.run(lambda *_: calls.append(True), structural=False)
    assert not calls and impact_path.read_bytes() == before
    assert not (tmp_path / MARKER).exists()
    assert not (tmp_path / "_state/structural-snapshots").exists()
    if resource.endswith(".png"):
        _register_image(store, workspace.paths.root / resource)
    store.render_pages([1], renderer)
    store.record_inspection(1, inspection_id="repaired-inspection")
    if ready:
        workspace.mark_ready(1)
    transaction.run(lambda *_: calls.append(True), structural=False)
    assert calls == [True]
    assert json.loads(impact_path.read_text())["assignments"][0]["status"] == "verified"


def test_plain_page_reads_do_not_validate_resource_closure(tmp_path, monkeypatch):
    workspace, store, _renderer, _ = _inspected_render_fixture(tmp_path)
    workspace.mark_ready(1)
    def forbidden(*_args):
        pytest.fail("ordinary state reads must not traverse visual dependencies")
    monkeypatch.setattr(type(store), "validate_render_receipt", forbidden)
    assert workspace.page(1).lifecycle == PageLifecycle.READY
    assert workspace.pages()[0].lifecycle == PageLifecycle.READY
