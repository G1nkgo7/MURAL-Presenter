"""主 Agent 直做的生产工具链；只替代浏览器像素和 Provider，不替代权限、归属与检查事务。"""
from __future__ import annotations

import json
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier, RLock
from types import SimpleNamespace

import pytest
from PIL import Image

from export.delivery import DeliveryExporter
from geometry_fixture import geometry, write_geometry
from infer import PresenterKernel
from owner.registry import OwnerRegistry, OwnerStatus
from runtime.agent import AgentProviderError
from runtime.config import RunMode
from runtime.production import ProductionSettings, ProductionToolServices, SkillRenderer
from runtime.diagnostics import command_scope
from runtime.recovery import SegmentCheckpoint, tool_boundary
from runtime.shared_objects import SharedSkillStore, load_skill_module
from runtime.session import ProviderChannel, UsageLedger
from runtime.skill_runtime import SkillRuntime, SkillRuntimeError
from runtime.tools import ToolCall, ToolExecutionError, register_tools, replay_tool_transactions
from state.artifacts import ArtifactStateError
from state.workspace import PageLifecycle, WorkspaceState, file_sha256
from test_inspection_transactions import ScriptedTransport, _call, _payload_and_parts, _response, _run
from test_native_inspect import _analyzer
from test_shared_entry import _run_spec


@pytest.fixture
def direct_authoring(tmp_path, monkeypatch):
    dimensions = geometry(width_px=1600, height_px=900)
    monkeypatch.setattr(WorkspaceState, "require_geometry", lambda _: dict(dimensions))
    write_geometry(tmp_path, dimensions)
    reference = SharedSkillStore.for_workspace(tmp_path).load_ref(WorkspaceState(tmp_path).paths)
    render_module = load_skill_module(reference.object_root, reference.tree_sha256, "render_deck")
    monkeypatch.setattr(render_module, "read_geometry", lambda _: dict(dimensions))
    renderer_calls, instances = [], []

    def renderer(html, png, canvas):
        renderer_calls.append(html.name)
        rgb = tuple(bytes.fromhex(file_sha256(html)[:6]))
        Image.new("RGB", (canvas["width_px"], canvas["height_px"]), rgb).save(png)
        return {"geometry": canvas, "loaded_resources": [], "failed_resources": []}

    def prepare(*, pages=(1,), resume=False, run_mode=RunMode.SYNTHESIS, revision_request=None):
        spec = replace(_run_spec(tmp_path, run_mode), revision_request=revision_request)
        kernel = PresenterKernel(spec, renderer=renderer,
            vision_analyzer=_analyzer, max_workers=2,
            role_runners={role: lambda *_: {"status": "repair_handoff"} for role in ("slide", "research")})
        instances.append(kernel)
        workspace = kernel.workspace
        if not resume:
            workspace.register_pages(pages)
            for page in pages:
                workspace.paths.page_html(page).write_text(
                    f'<section class="slide" id="slide-{page:02d}">Skeleton {page}</section>')
                workspace.register_scaffold(page)
        snapshot = SimpleNamespace(name=reference.skill_name, target=reference.object_root,
            tree_sha256=reference.tree_sha256, release="direct-authoring-test",
            resolve_skill_path=lambda logical: reference.object_root / logical.split("/", 2)[2])
        production = ProductionToolServices(kernel.run_spec, snapshot,
            SimpleNamespace(command_timeout_s=30), {}, workspace, kernel.artifacts, renderer, None)
        register_tools(kernel.tools, production.base_tool_handlers(), require_complete=False)
        runtime = SkillRuntime.__new__(SkillRuntime)
        runtime.prepared = SimpleNamespace(snapshot=snapshot)
        runtime._tool_services = production
        runtime._staged_attachment_paths = lambda: ()
        runtime._attachment_manifest = lambda: {}
        context = SkillRuntime._orchestrator_context(runtime, kernel.services)
        return SimpleNamespace(kernel=kernel, workspace=workspace, production=production,
            context=context, runtime=runtime, renderer_calls=renderer_calls)

    yield prepare
    for kernel in instances:
        kernel.orchestration.close(wait=False)


def _execute(state, name, arguments, tool_id, context=None):
    return state.kernel.tools.execute(context or state.context, ToolCall(tool_id, name, arguments))


def _write(state, page=1, *, context=None, tool_id="write-page"):
    return _execute(state, "write_file", {"path": f"slides/slide_{page:02d}.html",
        "content": f'<section class="slide" id="slide-{page:02d}"><h1>Authored {page}</h1></section>'},
        tool_id, context)


def _delegate(state, role, pages=(), task_id="child"):
    result = _execute(state, "delegate_submit", {"tasks": [{"task_id": task_id, "role": role,
        "pages": list(pages), "goal": "Complete the specified work", "group_id": task_id,
        "depends_on": []}]}, "submit-" + task_id)
    assert result.ok, result.content
    task = state.kernel.orchestration.task(task_id)
    context = SkillRuntime._owner_tool_context(state.runtime, state.kernel.services, task)
    return task, context, json.loads(result.content)


def _checkpoint_runtime(state):
    runtime = SkillRuntime.__new__(SkillRuntime)
    runtime.prepared = SimpleNamespace(snapshot=state.runtime.prepared.snapshot,
        run_spec=state.kernel.services.run_spec)
    runtime._checkpoint_attachments = []
    runtime._recovered_provider_attempt_ids = ()
    runtime._attachment_manifest = lambda: {}
    runtime._tool_services = state.production
    return runtime


@pytest.fixture
def automatic_scaffold(direct_authoring, monkeypatch):
    from test_creation_plan_contract import write_plan
    state = direct_authoring(pages=())
    root = state.workspace.paths.root
    write_plan(root, {"working_language": "en", "presentation_language": "en", "target_page_count": 3})
    first = (root / "briefs/slide_01.md").read_text()
    for page in (2, 3):
        (root / f"briefs/slide_{page:02d}.md").write_text(first)
    snapshot = state.production.snapshot
    core = load_skill_module(snapshot.target, snapshot.tree_sha256, "deck_core")
    monkeypatch.setattr(core, "_read_author_geometry", lambda _: geometry(width_px=1600, height_px=900))
    preparations = []
    monkeypatch.setattr(core, "_ensure_authoring_font_bundle",
        lambda root, **kwargs: preparations.append(kwargs["update_css"]) or {})
    manager = state.kernel.orchestration
    manager.prepare_submission = lambda tasks: SkillRuntime._prepare_slide_submission(
        state.runtime, state.kernel.services, tasks)
    manager.validate_submission = lambda tasks: SkillRuntime._validate_slide_submission(
        state.runtime, state.kernel.services, tasks)
    return state, preparations


def test_delegation_automatically_prepares_only_missing_pages_and_preserves_authored_work(automatic_scaffold):
    state, preparations = automatic_scaffold
    task, context, _ = _delegate(state, "slide", [1], task_id="first")
    assert state.workspace.page(1).lifecycle == PageLifecycle.SCAFFOLDED
    assert not state.workspace.paths.page_html(2).exists()
    assert _write(state, context=context).ok
    before = state.workspace.paths.page_html(1).read_bytes()
    page_before = state.workspace.page(1).to_dict()
    css = (state.workspace.paths.root / "base.css").read_bytes()
    _, _, receipt = _delegate(state, "slide", [2, 3], task_id="remaining")
    assert receipt["new_task_ids"] == ["remaining"]
    assert state.workspace.paths.page_html(1).read_bytes() == before
    assert state.workspace.page(1).to_dict() == page_before
    assert (state.workspace.paths.root / "base.css").read_bytes() == css
    assert all(state.workspace.page(page).lifecycle == PageLifecycle.SCAFFOLDED for page in (2, 3))
    assert preparations == [True, False]
    _delegate(state, "slide", [2, 3], task_id="remaining")
    assert preparations == [True, False]
    events = [json.loads(line) for line in state.kernel.live_trace.path.read_text().splitlines()]
    prepared = [event for event in events if event["event_type"] == "slide_preparation_completed"]
    assert [event["payload"]["created_pages"] for event in prepared] == [[1], [2, 3]]
    assert all(event["payload"]["actor"] == "harness" for event in prepared)


@pytest.mark.parametrize("invalid", ["page-count", "missing-brief", "font"])
def test_automatic_preparation_failure_does_not_accept_tasks(automatic_scaffold, monkeypatch, invalid):
    state, preparations = automatic_scaffold
    root = state.workspace.paths.root
    if invalid == "page-count":
        configuration = json.loads((root / "config.json").read_text())
        configuration["target_page_count"] = 2
        (root / "config.json").write_text(json.dumps(configuration))
    elif invalid == "missing-brief":
        (root / "briefs/slide_02.md").unlink()
    else:
        snapshot = state.production.snapshot
        core = load_skill_module(snapshot.target, snapshot.tree_sha256, "deck_core")
        def fail(*args, **kwargs):
            raise RuntimeError("authoring font preflight failed: missing pinned font")
        monkeypatch.setattr(core, "_ensure_authoring_font_bundle", fail)
    result = _execute(state, "delegate_submit", {"tasks": [
        {"task_id": "first", "role": "slide", "pages": [1, 2, 3]}]}, "invalid-preparation")
    assert not result.ok and result.error_code == "slide_preparation_failed"
    assert state.kernel.orchestration.snapshot()["tasks"] == {}
    assert not list(state.workspace.paths.slides.glob("*.html"))
    assert state.kernel.owners.page_owner(1) is None


def test_non_slide_delegation_does_not_prepare_pages(automatic_scaffold):
    state, preparations = automatic_scaffold
    _delegate(state, "research", task_id="research")
    assert preparations == []
    assert not list(state.workspace.paths.slides.glob("*.html"))


def test_direct_first_write_needs_no_scaffold_and_ignores_unrelated_briefs(automatic_scaffold):
    state, preparations = automatic_scaffold
    root = state.workspace.paths.root
    (root / "briefs/slide_01.md").unlink()
    (root / "briefs/slide_02.md").write_text("Incomplete unrelated draft")
    assert _write(state, page=3).ok
    assert state.kernel.owners.page_owner(3).owner_id == "orchestrator"
    assert state.workspace.page(3).lifecycle == PageLifecycle.AUTHORED
    assert not state.workspace.paths.page_html(1).exists()
    coverage = state.kernel.orchestration.page_coverage()
    assert coverage["target_page_count"] == 3 and coverage["unassigned_pages"] == [1, 2]
    assert preparations == [True]
    with pytest.raises(ToolExecutionError, match="target_page_count"):
        state.kernel.services._completion_pages()


def test_partial_delegation_prepares_noncontiguous_ready_pages(automatic_scaffold):
    state, preparations = automatic_scaffold
    (state.workspace.paths.root / "briefs/slide_02.md").unlink()
    _delegate(state, "slide", [1, 3], task_id="ready-pages")
    coverage = state.kernel.orchestration.page_coverage()
    assert coverage["target_page_count"] == 3 and coverage["unassigned_pages"] == [2]
    assert state.workspace.pages()[0].page == 1
    assert not state.workspace.paths.page_html(2).exists()


def test_interrupted_scaffold_registration_resumes_without_rewriting_or_changing_task_id(
    automatic_scaffold, monkeypatch,
):
    state, preparations = automatic_scaffold
    register = state.workspace.register_scaffold
    def interrupt(page):
        raise InterruptedError("stopped after HTML publication, before state registration")
    monkeypatch.setattr(state.workspace, "register_scaffold", interrupt)
    arguments = {"tasks": [{"task_id": "same-task", "role": "slide", "pages": [1, 2, 3]}]}
    failed = _execute(state, "delegate_submit", arguments, "interrupted-submit")
    assert not failed.ok and state.kernel.orchestration.snapshot()["tasks"] == {}
    before = {page: state.workspace.paths.page_html(page).read_bytes() for page in (1, 2, 3)}
    assert state.kernel.owners.page_owner(1) is None
    monkeypatch.setattr(state.workspace, "register_scaffold", register)
    recovered = _execute(state, "delegate_submit", arguments, "recovered-submit")
    assert recovered.ok, recovered.content
    assert json.loads(recovered.content)["new_task_ids"] == ["same-task"]
    assert before == {page: state.workspace.paths.page_html(page).read_bytes() for page in (1, 2, 3)}
    assert preparations == [True, False]
    assert all(state.workspace.page(page).lifecycle == PageLifecycle.SCAFFOLDED for page in (1, 2, 3))
    replay = replay_tool_transactions(state.kernel.tools.events)
    assert replay.open_ids == () and len(replay.closed_ids) == 2


def test_repair_preparation_restores_only_missing_original_owner_page(automatic_scaffold):
    from test_orchestration_manager import drain
    state, preparations = automatic_scaffold
    task, context, _ = _delegate(state, "slide", [1, 2], task_id="original")
    drain(state.kernel.orchestration)
    for page in (1, 2):
        assert _write(state, page, context=context, tool_id=f"author-{page}").ok
    preserved = state.workspace.page(2).to_dict()
    source = state.workspace.paths.page_html(2).read_bytes()
    state.workspace.paths.page_html(1).unlink()
    result = _execute(state, "delegate_submit", {"tasks": [{"task_id": "restore",
        "role": "slide", "pages": [1, 2], "group_id": task.group_id, "repair": True}]}, "repair-submit")
    assert result.ok, result.content
    assert state.kernel.orchestration.task("restore").owner_id == task.owner_id
    assert state.workspace.paths.page_html(1).is_file()
    assert state.workspace.page(2).to_dict() == preserved
    assert state.workspace.paths.page_html(2).read_bytes() == source
    assert preparations == [True, False]


@pytest.mark.parametrize("name", ["write_file", "patch"])
def test_first_successful_edit_claims_real_main_owner_without_child_task(direct_authoring, name):
    state = direct_authoring()
    assert state.context.metadata["pages"] == []
    arguments = ({"path": "slides/slide_01.html", "content": "<main>Authored</main>"}
        if name == "write_file" else {"path": "slides/slide_01.html",
            "old_text": "Skeleton 1", "new_text": "Authored"})
    result = _execute(state, name, arguments, "first-authoring")
    assert result.ok, result.content
    owner = state.kernel.owners.page_owner(1)
    assert (owner.owner_id, owner.role, owner.pages) == ("orchestrator", "orchestrator", (1,))
    assert state.kernel.artifacts.artifact("page-html-01").owner_id == "orchestrator"
    assert state.workspace.page(1).lifecycle == PageLifecycle.AUTHORED
    assert state.kernel.orchestration.snapshot()["tasks"] == {}
    coverage = state.kernel.orchestration.page_coverage()
    assert coverage["self_authored_pages"] == [1]
    assert coverage["delegated_pages"] == coverage["unassigned_pages"] == []
    replayed = _execute(state, name, arguments, "first-authoring")
    assert replayed == result
    assert len(state.kernel.tools.events) == 2


def test_failed_patch_and_unknown_page_do_not_claim_or_modify(direct_authoring):
    state = direct_authoring()
    before = state.workspace.paths.page_html(1).read_bytes()
    mismatch = _execute(state, "patch", {"path": "slides/slide_01.html",
        "old_text": "not present", "new_text": "replacement"}, "failed-patch")
    assert not mismatch.ok and mismatch.error_code == "patch_match_count"
    assert state.workspace.paths.page_html(1).read_bytes() == before
    assert state.kernel.owners.page_owner(1) is None
    missing = _write(state, 2, tool_id="unknown-page")
    assert not missing.ok and missing.error_code == "slide_preparation_failed"
    assert "write_file" in missing.content and "task_id" not in missing.content
    assert not state.workspace.paths.page_html(2).exists()
    assert state.kernel.owners.page_owner(2) is None


@pytest.mark.parametrize("incorrect", [
    '- **① 现象判断**\n> "可复现的异常。"',
    '**① 现象判断**\n' + r'> \"可复现的异常。\"',
    '1|**① 现象判断**\n2|> "可复现的异常。"',
])
def test_patch_literal_mismatch_preserves_file_and_recovers_from_current_read(direct_authoring, incorrect):
    state = direct_authoring()
    source = '**① 现象判断**\n> "可复现的异常。"\n'
    assert _execute(state, "write_file", {"path": "deck.md", "content": source}, "initial-plan").ok
    mismatch = _execute(state, "patch", {"path": "deck.md", "old_text": incorrect,
        "new_text": "Incorrect replacement"}, "incorrect-patch")
    assert not mismatch.ok and mismatch.error_code == "patch_match_count"
    assert "文件未修改" in mismatch.content and "read_file" in mismatch.content
    assert state.workspace.paths.path("deck").read_text() == source

    read = _execute(state, "read_file", {"path": "deck.md", "offset": 1, "limit": 2}, "read-current")
    assert read.ok, read.content
    lines = json.loads(read.content)["content"].splitlines()
    old = "\n".join(line.split("|", 1)[1] for line in lines)
    replacement = '**① 现象判断**\n> "核对后的异常。"'
    arguments = json.loads(json.dumps({"path": "deck.md", "old_text": old, "new_text": replacement}))
    fixed = _execute(state, "patch", arguments, "corrected-patch")
    assert fixed.ok, fixed.content
    assert json.loads(fixed.content)["replacements"] == 1
    assert state.workspace.paths.path("deck").read_text() == replacement + "\n"
    assert _execute(state, "patch", arguments, "corrected-patch") == fixed
    assert not replay_tool_transactions(state.kernel.tools.events).open_ids


def test_patch_match_count_is_total_not_a_partial_replacement_limit(direct_authoring):
    state = direct_authoring()
    source = "First: repeated\nSecond: repeated\n"
    assert _execute(state, "write_file", {"path": "deck.md", "content": source}, "initial-plan").ok
    mismatch = _execute(state, "patch", {"path": "deck.md", "old_text": "repeated",
        "new_text": "updated"}, "ambiguous-patch")
    assert not mismatch.ok and mismatch.error_code == "patch_match_count"
    assert state.workspace.paths.path("deck").read_text() == source
    changed = _execute(state, "patch", {"path": "deck.md", "old_text": "repeated",
        "new_text": "updated", "expected_replacements": 2}, "replace-both")
    assert changed.ok, changed.content
    assert json.loads(changed.content)["replacements"] == 2
    assert state.workspace.paths.path("deck").read_text() == source.replace("repeated", "updated")


def test_failed_first_commit_does_not_reserve_artifact_against_later_child(direct_authoring, monkeypatch):
    state = direct_authoring()
    before = state.workspace.paths.page_html(1).read_bytes()

    def fail_commit(**_):
        raise ArtifactStateError("fixture: artifact commit interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(state.kernel.artifacts, "commit", fail_commit)
        failed = _write(state, tool_id="interrupted-commit")
    assert not failed.ok and failed.error_code == "slide_artifact_commit_failed"
    assert state.workspace.paths.page_html(1).read_bytes() == before
    assert state.kernel.owners.page_owner(1) is None
    task, child, _ = _delegate(state, "slide", (1,))
    authored = _write(state, context=child, tool_id="child-after-failure")
    assert authored.ok, authored.content
    assert state.kernel.artifacts.artifact("page-html-01").owner_id == task.owner_id


def test_direct_authoring_delayed_native_inspection_ready_and_raw_identity(direct_authoring):
    state = direct_authoring()
    critic_calls = []

    def critic(context, call, payload):
        critic_calls.append((context.owner_id, context.role, payload["png_sha256"]))
        return {**payload, "critic_feedback": {"completed": True, "body": "当前像素反馈。"}}

    state.kernel.tools.inspection_feedback = critic

    def consume_pixels(request):
        payload, parts = _payload_and_parts(request)
        assert any(part["type"] == "image" for part in parts)
        pending = payload["pending_inspection"]
        assert (pending["owner_id"], pending["role"]) == ("orchestrator", "orchestrator")
        assert payload["critic_feedback"]["completed"]
        assert state.workspace.page(1).lifecycle == PageLifecycle.RENDERED
        assert state.workspace.inspection_receipt(pending["scope_key"], pending["members"]) is None
        return _response()

    result = _run(ScriptedTransport([
        _call("write_file", {"path": "slides/slide_01.html", "content":
            '<section class="slide" id="slide-01"><h1>Ready</h1></section>'}, "author"),
        _call("inspect", {"page": [1]}, "inspect-main"), consume_pixels,
    ]), state.kernel.tools, state.context,
        idle_handler=lambda: SkillRuntime._orchestrator_idle(state.runtime, state.kernel.services))
    assert state.workspace.page(1).lifecycle == PageLifecycle.READY
    assert critic_calls == [("orchestrator", "orchestrator", state.workspace.page(1).png_sha256)]
    assert state.renderer_calls == ["slide_01.html"]
    assert tool_boundary(result.checkpoint.messages).is_closed
    transaction = replay_tool_transactions(state.kernel.tools.events)
    assert not transaction.open_ids and transaction.uses == transaction.results == 2
    assert all(event["tool_name"] != "verification_submit" for event in state.kernel.tools.events)
    completion = [json.loads(line) for line in state.kernel.live_trace.path.read_text().splitlines()
                  if json.loads(line)["event_type"] == "deck_completion_checked"]
    assert len(completion) == 1 and completion[0]["payload"]["actor"] == "harness"
    assert all(event["owner_id"] == event["role"] == "orchestrator" for event in state.kernel.tools.events)
    assert state.kernel.orchestration.snapshot()["tasks"] == {}

    state.kernel.services.capture_training_segment("orchestrator", "orchestrator", result.checkpoint,
        artifact_paths=("slides/slide_01.html", "renders/slide_01.png"))
    raw = [json.loads(path.read_text()) for path in (state.workspace.paths.trace / "raw/segments").glob("*.json")]
    assert len(raw) == 1 and raw[0]["owner_id"] == raw[0]["role"] == "orchestrator"
    assert not (state.workspace.paths.trace / "training").exists()

    manifest = DeliveryExporter(state.workspace, state.kernel.artifacts, state.kernel.issues).export(
        include_pdf=False, include_pptx=False, include_zip=False, title="Direct authoring")
    assert manifest.status == "complete" and len(manifest.pages) == 1
    assert set(manifest.outputs) == {"present_html", "contact_sheet"}
    assert json.loads(state.workspace.paths.path("result").read_text())["status"] == "completed"


def test_dynamic_authority_separates_main_child_and_observer(direct_authoring):
    state = direct_authoring(pages=(1, 2))
    assert _write(state).ok
    task, child, coverage = _delegate(state, "slide", (2,))
    assert coverage["self_authored_pages"] == [1] and coverage["delegated_pages"] == [2]
    assert coverage["unassigned_pages"] == []
    assert not state.context.may_write("slides/slide_02.html")
    assert not child.may_write("slides/slide_01.html")
    for context, page in ((state.context, 2), (child, 1)):
        denied = _write(state, page, context=context, tool_id=f"denied-{page}")
        assert not denied.ok and denied.error_code == "write_scope_denied"
        if context.role == "slide":
            denied = _execute(state, "inspect", {"page": [page]}, f"denied-inspect-{page}", context)
            assert not denied.ok and denied.error_code == "read_scope_denied"
    assert _write(state, 2, context=child, tool_id="child-author").ok
    state.kernel.artifacts.render_pages([2], state.production.renderer)
    _run(ScriptedTransport([_call("inspect", {"page": [2],
        "question": "Compare the page"}, "observe-child"), _response()]),
        state.kernel.tools, state.context)
    assert state.workspace.page(2).lifecycle == PageLifecycle.RENDERED
    assert state.kernel.owners.page_owner(2).owner_id == task.owner_id
    _run(ScriptedTransport([_call("inspect", {"page": [2]}, "inspect-child"), _response()]),
        state.kernel.tools, child)
    assert state.workspace.page(2).lifecycle == PageLifecycle.INSPECTED


def test_render_preflights_all_pages_and_main_review_cannot_replace_author(direct_authoring):
    state = direct_authoring(pages=(1, 2))
    assert _write(state).ok
    task, child, _ = _delegate(state, "slide", (2,))
    assert _write(state, 2, context=child, tool_id="child-author").ok

    denied = _execute(state, "render", {"pages": [2, 1]}, "mixed-render", child)
    assert not denied.ok and denied.error_code == "render_scope_denied"
    assert state.renderer_calls == []
    assert all(state.workspace.page(page).lifecycle == PageLifecycle.AUTHORED for page in (1, 2))
    assert all(not state.workspace.paths.page_png(page).exists() for page in (1, 2))

    own = _execute(state, "render", {"pages": [2]}, "own-render", child)
    assert own.ok, own.content
    assert state.renderer_calls == ["slide_02.html"]
    assert state.workspace.page(2).lifecycle == PageLifecycle.RENDERED

    reviewed = _execute(state, "render", {"pages": [1, 2]}, "main-review-render")
    assert reviewed.ok, reviewed.content
    assert state.renderer_calls == ["slide_02.html", "slide_01.html"]
    assert state.kernel.owners.page_owner(2).owner_id == task.owner_id
    assert all(state.workspace.page(page).lifecycle == PageLifecycle.RENDERED for page in (1, 2))
    observed = _execute(state, "inspect", {"page": [2]}, "main-inspect-child")
    assert observed.ok and not json.loads(observed.content)["author_inspection"]
    result = _run(ScriptedTransport([_call("inspect", {"page": [2],
        "question": "Review without replacing the author"}, "main-review-child"),
        _response()]), state.kernel.tools, state.context)
    assert state.workspace.page(2).lifecycle == PageLifecycle.RENDERED
    assert state.kernel.owners.page_owner(2).owner_id == task.owner_id
    assert tool_boundary(result.checkpoint.messages).is_closed
    transactions = replay_tool_transactions(state.kernel.tools.events)
    assert not transactions.open_ids and transactions.uses == transactions.results


def test_main_author_receives_real_critic_coordinator_feedback_and_raw_segment(direct_authoring):
    from test_critic_feedback_runtime import Transport
    from test_skill_runtime import _response as critic_response, _text

    state = direct_authoring()
    assert _write(state).ok
    runtime = SkillRuntime.__new__(SkillRuntime)
    runtime._trace_lock = RLock()
    runtime._critic_records = []
    runtime._critic_state = state.workspace.paths.state / "critic-results.json"
    spec = replace(state.kernel.run_spec, provider=replace(state.kernel.run_spec.provider, api_format="anthropic"))
    transport = Transport(critic_response(_text("主标题清晰，主体完整。")))
    runtime.prepared = SimpleNamespace(run_spec=spec, environment={}, transport=transport,
        channels=(ProviderChannel(key="fixture-critic-key", channel_id="fixture"),), usage=UsageLedger(),
        snapshot=state.runtime.prepared.snapshot,
        slots=SimpleNamespace(acquire=lambda *_args, **_kwargs: nullcontext(0)),
        settings=ProductionSettings(skill_source=Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1",
            enable_external_visual_critic=True, thinking=False))
    runtime._build_review_coordinator(state.kernel.services)
    try:
        owner_transport = ScriptedTransport([_call("inspect", {
            "page": [1], "review_stage": "final",
        }, "inspect-real-critic"), _response()])
        _run(owner_transport, state.kernel.tools, state.context)
        payload, _ = _payload_and_parts(owner_transport.requests[1])
        assert payload["critic_feedback"]["completed"]
        assert "主标题清晰" in payload["critic_feedback"]["body"]
        assert len(transport.requests) == 1 and transport.requests[0].request_kind == "critic_visual"
        assert runtime._critic_records[0]["agent_id"] == "orchestrator"
        assert state.workspace.page(1).lifecycle == PageLifecycle.INSPECTED
        segments = state.kernel.services.training_snapshot()
        assert len(segments) == 1 and segments[0].role == "critic"
        raw = json.loads(next((state.workspace.paths.trace / "raw/segments").glob("*.json")).read_text())
        assert raw["agent_context"][0]["metadata"]["owner_id"] == "orchestrator"
        assert raw["role"] == "critic" and raw["messages"][0]["content"][0]["type"] == "image"
    finally:
        runtime._critic_executor.shutdown(wait=True, cancel_futures=True)


def test_knowledge_write_authority_stays_with_orchestrator_after_research_delegation(direct_authoring):
    state = direct_authoring()
    written = _execute(state, "write_file", {"path": "knowledge.md", "content": "Initial evidence"}, "main-research")
    assert written.ok, written.content
    task, research, _ = _delegate(state, "research", task_id="research")
    assert state.context.may_write("knowledge.md") and not research.may_write("knowledge.md")
    denied = _execute(state, "write_file", {"path": "knowledge.md", "content": "Overwrite"}, "research-overwrite", research)
    assert not denied.ok and denied.error_code == "write_scope_denied"
    accepted = _execute(state, "write_file", {"path": "knowledge.md", "content": "Adopted research evidence"},
                        "main-integrate")
    assert accepted.ok, accepted.content
    assert state.workspace.paths.root.joinpath("knowledge.md").read_text() == "Adopted research evidence"
    assert task.owner_id == research.owner_id and research.role == "research"


def test_main_group_inspection_uses_live_ownership_not_initial_metadata(direct_authoring):
    state = direct_authoring(pages=(1, 2, 3))
    assert _write(state, 1, tool_id="author-one").ok
    assert _write(state, 2, tool_id="author-two").ok
    assert state.context.metadata["pages"] == []
    assert state.kernel.owners.page_owner(1).pages == (1, 2)
    _run(ScriptedTransport([
        _call("inspect", {"page": [1]}, "inspect-one"),
        _call("inspect", {"page": [2]}, "inspect-two"),
        _call("inspect", {"page": [1, 2]}, "main-group"), _response(),
    ]), state.kernel.tools, state.context)
    assert all(state.workspace.page(page).lifecycle == PageLifecycle.INSPECTED for page in (1, 2))
    task, child, _ = _delegate(state, "slide", (3,))
    assert _write(state, 3, context=child, tool_id="child-three").ok
    mixed = _execute(state, "inspect", {"page": [1, 3]}, "mixed-ownership")
    assert mixed.ok and not json.loads(mixed.content)["author_inspection"]
    assert state.workspace.page(3).lifecycle == PageLifecycle.RENDERED
    assert state.renderer_calls == ["slide_01.html", "slide_02.html", "slide_03.html"]


def test_render_attempt_marks_only_actual_main_authorship(direct_authoring):
    state = direct_authoring(pages=(1, 2))
    assert _write(state).ok
    _delegate(state, "slide", (2,))
    snapshot = state.runtime.prepared.snapshot
    renderer = SkillRenderer(state.workspace.paths.root,
        ProductionSettings(skill_source=snapshot.target), {}, None, snapshot)
    with command_scope("attempts", {"owner_id": "orchestrator", "role": "orchestrator", "attempt": 2}, lambda _: None):
        own = renderer._render_environment(page=1)["MURAL_RENDER_ATTEMPT_ID"]
        review = renderer._render_environment(page=2)["MURAL_RENDER_ATTEMPT_ID"]
    core = load_skill_module(snapshot.target, snapshot.tree_sha256, "deck_core")
    assert own == "author-orchestrator-orchestrator-attempt-2"
    assert core._is_authoring_attempt(own)
    assert review == "orchestrator-orchestrator-attempt-2"
    assert not core._is_authoring_attempt(review)


def test_direct_inspection_failure_resumes_same_owner_without_reexecuting_tools(direct_authoring):
    state = direct_authoring()
    assert _write(state).ok
    checkpoints = []
    with pytest.raises(AgentProviderError):
        _run(ScriptedTransport([_call("inspect", {"page": [1]}, "pending-inspect"),
            ValueError("image request failed")]), state.kernel.tools, state.context,
            checkpoint_sink=checkpoints.append)
    assert state.workspace.page(1).lifecycle == PageLifecycle.RENDERED
    assert tool_boundary(checkpoints[-1].messages).is_closed
    resumed = direct_authoring(resume=True)
    transport = ScriptedTransport([_response()])
    runtime = _checkpoint_runtime(resumed)
    result = _run(transport, resumed.kernel.tools, resumed.context, checkpoint=checkpoints[-1],
        idle_handler=lambda: runtime._orchestrator_idle(resumed.kernel.services))
    payload, parts = _payload_and_parts(transport.requests[0])
    assert payload["pending_inspection"]["owner_id"] == "orchestrator"
    assert any(part["type"] == "image" for part in parts)
    assert resumed.workspace.page(1).lifecycle == PageLifecycle.READY
    assert resumed.kernel.owners.page_owner(1).owner_id == "orchestrator"
    assert resumed.kernel.tools.events == ()
    assert state.renderer_calls == ["slide_01.html"]
    assert tool_boundary(result.checkpoint.messages).is_closed


@pytest.mark.parametrize("target", ["base.css", "renders/slide_01.png"])
def test_direct_ready_rejects_changed_resource_or_pixels(direct_authoring, target):
    state = direct_authoring()
    assert _write(state).ok
    _run(ScriptedTransport([_call("inspect", {"page": [1]}, "inspect-before-tamper"), _response()]),
         state.kernel.tools, state.context)
    path = state.workspace.paths.resolve(target)
    if target.endswith(".png"):
        Image.new("RGB", (1600, 900), "red").save(path)
    else:
        path.write_text(path.read_text() + "\nh1 {color: red}\n")
    with pytest.raises(ToolExecutionError) as ready:
        state.kernel.services.complete_deck()
    assert ready.value.code in {"verification_resources_stale", "verification_pages_not_inspected"}
    assert state.workspace.page(1).lifecycle != PageLifecycle.READY


def _inspect_direct(state, page=1):
    assert _write(state, page=page, tool_id=f"author-{page}").ok
    return _run(ScriptedTransport([_call("inspect", {"page": [page]}, f"inspect-{page}"), _response()]),
                state.kernel.tools, state.context)


def test_automatic_completion_returns_current_missing_page_and_preserves_checkpoint(direct_authoring):
    state = direct_authoring(pages=(1, 2))
    checkpoint = _inspect_direct(state).checkpoint
    message = SkillRuntime._orchestrator_idle(state.runtime, state.kernel.services)
    gap = json.loads(message["content"])
    assert gap["harness_event"] == "completion_gap"
    assert gap["error_code"] == "verification_pages_not_inspected"
    assert gap["unassigned_pages"] == [2]
    assert state.workspace.page(1).lifecycle == PageLifecycle.INSPECTED
    assert tool_boundary(checkpoint.messages).is_closed
    assert not any(event["tool_name"] == "verification_submit" for event in state.kernel.tools.events)
    _inspect_direct(state, page=2)
    gap = json.loads(SkillRuntime._orchestrator_idle(
        state.runtime, state.kernel.services)["content"])
    assert gap["error_code"] == "verification_group_inspection_missing_or_stale"
    _run(ScriptedTransport([
        _call("inspect", {"page": [1, 2]}, "inspect-group"), _response(),
    ]), state.kernel.tools, state.context)
    assert SkillRuntime._orchestrator_idle(state.runtime, state.kernel.services) is None
    assert all(page.lifecycle == PageLifecycle.READY for page in state.workspace.pages())


def test_automatic_completion_rechecks_current_plan_instead_of_old_success(direct_authoring):
    state = direct_authoring()
    _inspect_direct(state)
    state.kernel.services.complete_deck()
    index = state.workspace.paths.state / "page-index.json"
    index.write_text(json.dumps({"pages": [
        {"position": 1, "page_uid": state.workspace.page(1).page_uid},
        {"position": 2, "page_uid": "not-yet-registered"}]}))
    before = state.kernel.issues.path.read_bytes()
    gap = json.loads(SkillRuntime._orchestrator_idle(state.runtime, state.kernel.services)["content"])
    assert gap["error_code"] == "verification_page_coverage" and gap["unassigned_pages"] == [2]
    assert state.kernel.issues.path.read_bytes() == before


def test_explicit_ready_is_rejected_without_promoting_pages(direct_authoring):
    state = direct_authoring()
    _inspect_direct(state)
    before = state.workspace.path.read_bytes()
    result = _execute(state, "verification_submit", {"status": "ready", "issues": []}, "old-ready")
    assert not result.ok and result.error_code == "invalid_arguments"
    assert state.workspace.path.read_bytes() == before
    state.kernel.tools.assert_closed()


def test_issue_resolution_is_explicit_partial_and_does_not_declare_deck_ready(direct_authoring):
    state = direct_authoring()
    _inspect_direct(state)
    reported = _execute(state, "verification_submit", {"status": "issues_found", "issues": [
        {"positions": [1], "problem": problem, "suggested_fix": "修正并核对当前版本"}
        for problem in ("图注需核对", "标题需核对")]}, "report-problems")
    assert reported.ok, reported.content
    ids = json.loads(reported.content)["issue_ids"]
    before = state.kernel.issues.path.read_bytes()
    gap = json.loads(SkillRuntime._orchestrator_idle(state.runtime, state.kernel.services)["content"])
    assert gap["error_code"] == "verification_rejected" and len(gap["blocking_issues"]) == 2
    assert state.kernel.issues.path.read_bytes() == before
    for index, issue_id in enumerate(ids):
        resolved = _execute(state, "verification_submit", {"status": "issues_resolved", "issues": [],
            "resolved_issue_ids": [issue_id]}, f"resolve-{index}")
        assert resolved.ok, resolved.content
        assert "verdict" not in json.loads(resolved.content)
        assert len(state.kernel.issues.blocking_issues()) == 1 - index
        assert state.workspace.page(1).lifecycle == PageLifecycle.INSPECTED
    assert SkillRuntime._orchestrator_idle(state.runtime, state.kernel.services) is None
    ledger = json.loads(state.kernel.issues.path.read_text())
    passes = [event for event in ledger["events"] if event.get("verdict") == "pass"]
    assert len(passes) == 1 and passes[0]["actor"] == "harness"


@pytest.mark.parametrize("defect", ["unknown_id", "uninspected", "stale_resource"])
def test_resolution_rejects_invalid_or_stale_evidence_atomically(direct_authoring, defect):
    state = direct_authoring()
    _inspect_direct(state)
    report = _execute(state, "verification_submit", {"status": "issues_found", "issues": [
        {"positions": [1], "problem": "待核对的文案", "suggested_fix": "核对并更正"}]}, "report")
    issue_id, = json.loads(report.content)["issue_ids"]
    if defect == "uninspected":
        state.workspace.paths.page_html(1).write_text("changed without inspection")
    elif defect == "stale_resource":
        css = state.workspace.paths.root / "base.css"
        css.write_text(css.read_text() + "\nh1 { color: #123456; }\n")
    before = state.kernel.issues.path.read_bytes()
    result = _execute(state, "verification_submit", {"status": "issues_resolved", "issues": [],
        "resolved_issue_ids": [issue_id, "unknown"] if defect == "unknown_id" else [issue_id]}, "resolve")
    assert not result.ok
    assert state.kernel.issues.path.read_bytes() == before
    state.kernel.tools.assert_closed()


def test_automatic_completion_waits_for_pending_requests_and_checks_closed_transactions(direct_authoring):
    state = direct_authoring()
    _inspect_direct(state)
    manager = state.kernel.orchestration
    with manager._condition:
        manager._state["requests"] = {"question": {"status": "pending", "task_id": "prior-task"}}
    event = json.loads(SkillRuntime._orchestrator_idle(state.runtime, state.kernel.services)["content"])
    assert event["harness_event"] == "orchestration_update" and event["pending_requests"]
    with pytest.raises(ToolExecutionError, match="requests"):
        state.kernel.services.complete_deck()
    with manager._condition:
        manager._state["requests"] = {}
    state.kernel.tools._append_event("tool_use", state.context, ToolCall("open", "read_file", {"path": "deck.md"}))
    with pytest.raises(RuntimeError, match="open tool transactions"):
        state.kernel.services.complete_deck()
    assert state.workspace.page(1).lifecycle == PageLifecycle.INSPECTED
    state.kernel.tools._append_event("tool_result", state.context, ToolCall("open", "read_file", {"path": "deck.md"}),
                                    ok=False, content="interrupted", error_code="owner_interrupted")


def test_current_critic_attempt_is_not_replaced_by_automatic_completion(direct_authoring):
    state = direct_authoring()
    _inspect_direct(state)
    state.kernel.services.visual_review = lambda: {"effective": True,
        "current_inspections": [{"page": 1, "attempted": False}]}
    with pytest.raises(ToolExecutionError) as failure:
        state.kernel.services.complete_deck()
    assert failure.value.code == "visual_review_not_started"
    assert state.workspace.page(1).lifecycle == PageLifecycle.INSPECTED


@pytest.mark.parametrize("pending", ["revision", "artifact_lease"])
def test_automatic_completion_does_not_close_pending_structural_or_asset_work(direct_authoring, pending):
    state = direct_authoring()
    _inspect_direct(state)
    if pending == "revision":
        pointer = state.workspace.paths.state / "active-revision.json"
        pointer.write_text(json.dumps({"revision_id": "unfinished-edit", "status": "prepared"}))
        expected = "revision_incomplete"
    else:
        transaction = state.kernel.artifacts.begin_transaction("orchestrator", ["page-html-01"])
        expected = "unfinished_artifact_transactions"
    with pytest.raises(ToolExecutionError) as error:
        state.kernel.services.complete_deck()
    assert error.value.code == expected
    assert state.workspace.page(1).lifecycle == PageLifecycle.INSPECTED
    if pending == "revision":
        pointer.write_text(json.dumps({"revision_id": "unfinished-edit", "status": "committed"}))
    else:
        state.kernel.artifacts.abort_transaction(transaction, owner_id="orchestrator")
    assert state.kernel.services.complete_deck()["status"] == "ready"


def test_main_write_and_delegation_cannot_claim_same_page(direct_authoring):
    state = direct_authoring()
    barrier = Barrier(2)

    def write():
        barrier.wait(timeout=10)
        return _write(state)

    def delegate():
        barrier.wait(timeout=10)
        return _execute(state, "delegate_submit", {"tasks": [{"task_id": "competing-child",
            "role": "slide", "pages": [1], "goal": "Author page one", "depends_on": []}]}, "competing-submit")

    with ThreadPoolExecutor(max_workers=2) as pool:
        writing, submitting = pool.submit(write), pool.submit(delegate)
        results = writing.result(timeout=15), submitting.result(timeout=15)
    assert sum(result.ok for result in results) == 1
    assert next(result for result in results if not result.ok).error_code in {"write_scope_denied", "delegation_rejected"}
    assert "no runner" not in next(result for result in results if not result.ok).content
    owner = state.kernel.owners.page_owner(1)
    assert owner.owner_id == "orchestrator" if results[0].ok else owner.role == "slide"
    assert not replay_tool_transactions(state.kernel.tools.events).open_ids


@pytest.mark.parametrize("previous_revision", [None, {"goal": "第一次：仅更新数据"}])
def test_inference_new_revision_is_appended_once_to_same_direct_owner_checkpoint(direct_authoring, previous_revision):
    state = direct_authoring(run_mode=RunMode.INFERENCE, revision_request=previous_revision)
    assert _write(state).ok
    runtime, services = _checkpoint_runtime(state), state.kernel.services
    initial = runtime._orchestrator_input(services)
    previous = SegmentCheckpoint.initial((initial, {"role": "assistant", "content": "本次修改完成。"}),
        segment_id="direct-author-segment")
    runtime._save_orchestrator_checkpoint(services, previous)
    unchanged = runtime._orchestrator_checkpoint(services, initial)
    assert unchanged.messages == previous.messages
    assert unchanged.segment_id == previous.segment_id

    revision = {"goal": "只将标题改为 REVISED TITLE；保留图表、数据和版式"}
    services.run_spec = replace(services.run_spec, revision_request=revision)
    runtime.prepared.run_spec = services.run_spec
    current = runtime._orchestrator_input(services)
    updated = runtime._orchestrator_checkpoint(services, current)
    assert updated.messages == (*previous.messages, current)
    assert updated.segment_id == previous.segment_id
    assert updated.predecessor_segment_id == previous.predecessor_segment_id
    assert tool_boundary(updated.messages).is_closed
    assert json.loads(updated.messages[-1]["content"])["revision_request"] == revision
    assert "REVISED TITLE" not in json.dumps(previous.messages)

    runtime._save_orchestrator_checkpoint(services, updated)
    repeated = runtime._orchestrator_checkpoint(services, current)
    assert repeated.messages == updated.messages
    assert repeated.segment_id == updated.segment_id
    assert state.kernel.owners.page_owner(1).owner_id == "orchestrator"
    assert state.kernel.orchestration.snapshot()["tasks"] == {}


def test_new_revision_does_not_bypass_fixed_identity_or_artifact_checks(direct_authoring):
    state = direct_authoring(run_mode=RunMode.INFERENCE)
    assert _write(state).ok
    runtime, services = _checkpoint_runtime(state), state.kernel.services
    initial = runtime._orchestrator_input(services)
    runtime._save_orchestrator_checkpoint(services, SegmentCheckpoint.initial((initial,)))
    original = services.run_spec
    services.run_spec = replace(original, raw_user_query="另一个原始任务", revision_request={"goal": "编辑"})
    runtime.prepared.run_spec = services.run_spec
    with pytest.raises(SkillRuntimeError, match="orchestrator_checkpoint_stale"):
        runtime._orchestrator_checkpoint(services, runtime._orchestrator_input(services))

    services.run_spec = replace(original, revision_request={"goal": "编辑"})
    runtime.prepared.run_spec = services.run_spec
    state.workspace.paths.page_html(1).write_text("<main>uncheckpointed external change</main>")
    with pytest.raises(SkillRuntimeError, match="orchestrator_checkpoint_stale"):
        runtime._orchestrator_checkpoint(services, runtime._orchestrator_input(services))


@pytest.mark.parametrize("status", ["completed", "failed", "interrupted"])
def test_direct_owner_resume_keeps_real_identity_and_refreshes_attempt(direct_authoring, status):
    state = direct_authoring()
    assert _write(state).ok
    original = state.kernel.owners.page_owner(1)
    state.kernel.owners.transition(original.owner_id, attempt=original.attempt, status=status)
    registry = OwnerRegistry(state.workspace.paths.root, deck_id=state.workspace.deck_id)
    resumed = registry.resume_orchestrator()
    assert resumed.owner_id == resumed.group_id == resumed.role == "orchestrator"
    assert resumed.pages == original.pages == (1,)
    assert resumed.attempt == original.attempt + 1 and resumed.status == OwnerStatus.RUNNING
    context = _checkpoint_runtime(state)._orchestrator_context(state.kernel.services)
    assert context.owner_id == context.role == "orchestrator"
    assert context.metadata["attempt"] == resumed.attempt and context.metadata["pages"] == [1]
    assert context.may_write("slides/slide_01.html")
    assert state.kernel.orchestration.snapshot()["tasks"] == {}


def test_main_checkpoint_captures_actual_owned_html_and_png(direct_authoring, monkeypatch):
    state = direct_authoring(revision_request={"goal": "仅更新说明"})
    assert _write(state).ok
    state.kernel.artifacts.render_pages([1], state.production.renderer)
    runtime, services = _checkpoint_runtime(state), state.kernel.services
    checkpoint = SegmentCheckpoint.initial((runtime._orchestrator_input(services),))
    captured = []
    capture = services.capture_training_segment

    def record(owner_id, role, segment, *, artifact_paths=()):
        captured.append((owner_id, role, segment, tuple(artifact_paths)))
        return capture(owner_id, role, segment, artifact_paths=artifact_paths)

    monkeypatch.setattr(services, "capture_training_segment", record)
    runtime._save_orchestrator_checkpoint(services, checkpoint)
    assert len(captured) == 1 and captured[0][:3] == ("orchestrator", "orchestrator", checkpoint)
    required = {"slides/slide_01.html", "renders/slide_01.png"}
    assert required <= set(captured[0][3])
    segment = services.training_snapshot()[0]
    stored = services.owner_contexts.load_orchestrator()
    assert stored["identity"]["revision_request"] == {"goal": "仅更新说明"}
    for relative in required:
        digest = file_sha256(state.workspace.paths.resolve(relative))
        assert stored["artifact_hashes"][relative] == digest
        assert segment.artifact_hashes[relative] == digest
        assert segment.artifact_blobs[relative]["sha256"] == digest
