"""完成缺口的真实证据与原 Owner 续做参数；像素、Provider 使用本地替身。"""
import copy
import json

import pytest
from PIL import Image

from runtime.recovery import tool_boundary
from runtime.skill_runtime import SkillRuntime
from runtime.tools import ToolExecutionError, replay_tool_transactions
from state import artifacts as artifact_module
from state.workspace import PageLifecycle, WorkspaceStateError
from test_direct_authoring import direct_authoring, _delegate, _execute, _write
from test_inspection_transactions import ScriptedTransport, _call, _response, _run
from test_orchestration_manager import drain


def _config(state, **changes):
    result = _execute(state, "write_file", {"path": "config.json", "content": json.dumps({
        **state.workspace.creation_config(), **changes})}, f"config-{len(state.kernel.tools.events)}")
    assert result.ok, result.content


def _inspect(state, page=1, *, context=None):
    return _run(ScriptedTransport([
        _call("inspect", {"page": [page]}, f"inspect-{page}-{len(state.kernel.tools.events)}"), _response(),
    ]), state.kernel.tools, context or state.context)


def _inspect_group(state, pages):
    return _run(ScriptedTransport([
        _call("inspect", {"page": list(pages)}, f"inspect-group-{len(state.kernel.tools.events)}"),
        _response(),
    ]), state.kernel.tools, state.context)


def _make_inspected(state, page=1, *, context=None):
    result = _write(state, page, context=context, tool_id=f"author-{page}")
    assert result.ok, result.content
    return _inspect(state, page, context=context)


def _patch(state, page=1, *, context=None):
    result = _execute(state, "patch", {"path": f"slides/slide_{page:02d}.html",
        "old_text": f"Authored {page}", "new_text": f"Revised {page}",
        "repair_reason": "按当前修改目标更新标题，保留其余已确认内容。"}, f"revise-{page}", context)
    assert result.ok, result.content


def _gap(state):
    with pytest.raises(ToolExecutionError) as failed:
        state.kernel.services.complete_deck()
    diagnostics = failed.value.diagnostics
    assert isinstance(diagnostics, dict)
    assert isinstance(diagnostics["page_issues"], list)
    assert isinstance(diagnostics["recovery"], list)
    return failed.value, diagnostics


def _page(diagnostics, page):
    rows = [row for row in diagnostics["page_issues"] if row["page"] == page]
    assert len(rows) == 1
    row = rows[0]
    assert row["html_path"] == f"slides/slide_{page:02d}.html"
    assert row["png_path"] == f"renders/slide_{page:02d}.png"
    assert row["page_uid"] and row["lifecycle"]
    assert all(reason.get("code") and reason.get("detail") for reason in row["reasons"])
    return row


def _reason(row, code):
    matches = [reason for reason in row["reasons"] if reason["code"] == code]
    assert len(matches) == 1, row
    return matches[0]


def test_never_inspected_and_unassigned_pages_do_not_gain_fictitious_owners(direct_authoring):
    state = direct_authoring(pages=(1, 2))
    assert _write(state).ok
    before = copy.deepcopy(state.kernel.orchestration.snapshot())
    _, diagnostics = _gap(state)
    for page in (1, 2):
        row = _page(diagnostics, page)
        assert row["previously_inspected"] is False
        _reason(row, "never_inspected")
    recovery = {tuple(item["affected_pages"]): item for item in diagnostics["recovery"]}
    assert recovery[(1,)]["action"] == "continue_self"
    assert recovery[(1,)]["owner_id"] == "orchestrator"
    assert recovery[(2,)]["action"] == "author_or_delegate"
    assert not recovery[(2,)].get("owner_id")
    assert all("submit_task" not in item for item in recovery.values())
    assert state.kernel.owners.page_owner(2) is None
    assert state.kernel.orchestration.snapshot() == before


@pytest.mark.parametrize("before_language", ["en", None])
def test_existing_language_inspection_reports_its_actual_previous_configuration(direct_authoring, before_language):
    state = direct_authoring()
    _config(state, presentation_language=before_language)
    _make_inspected(state)
    previous = state.workspace.page(1)
    assert previous.lifecycle == PageLifecycle.INSPECTED
    _config(state, presentation_language="zh")
    _, diagnostics = _gap(state)
    row = _page(diagnostics, 1)
    assert row["previously_inspected"] is True
    assert "never_inspected" not in {reason["code"] for reason in row["reasons"]}
    reason = _reason(row, "presentation_language_changed")
    assert reason["before"] == before_language and reason["after"] == "zh"
    assert state.workspace.page(1).png_sha256 == previous.png_sha256


def test_working_language_change_keeps_valid_author_inspection(direct_authoring):
    state = direct_authoring()
    _make_inspected(state)
    state.kernel.services.complete_deck()
    previous = state.workspace.page(1)
    _config(state, working_language="zh")
    assert state.workspace.inspection_diagnostics(state.workspace.pages()) == []
    state.workspace.assert_ready_resources([1])
    assert state.kernel.services.complete_deck()["status"] == "ready"
    assert state.workspace.page(1) == previous


def test_automatic_delivery_failure_preserves_ready_pages_for_recovery(direct_authoring):
    state = direct_authoring()
    _make_inspected(state)

    def fail_delivery():
        raise RuntimeError("contact sheet assembly failed")

    state.kernel.services.finalize_delivery = fail_delivery
    with pytest.raises(ToolExecutionError) as failed:
        state.kernel.services.complete_deck()
    assert failed.value.code == "automatic_finalization_failed"
    assert failed.value.diagnostics["stage"] == "delivery_preflight"
    assert state.workspace.page(1).lifecycle == PageLifecycle.READY

    state.kernel.services.finalize_delivery = lambda: {"status": "complete"}
    completed = state.kernel.services.complete_deck()
    assert completed["status"] == "ready"
    assert completed["automatic_finalization"] == "complete"


def test_new_render_after_old_inspection_is_not_called_never_inspected(direct_authoring):
    state = direct_authoring()
    _make_inspected(state)
    _patch(state)
    rendered = _execute(state, "render", {"pages": [1]}, "render-new-version")
    assert rendered.ok, rendered.content
    assert state.workspace.page(1).lifecycle == PageLifecycle.RENDERED
    _, diagnostics = _gap(state)
    row = _page(diagnostics, 1)
    assert row["previously_inspected"] is True
    _reason(row, "inspection_required_after_render")
    assert "never_inspected" not in {reason["code"] for reason in row["reasons"]}


@pytest.mark.parametrize("changed", ["html", "png", "image", "css"])
def test_completion_explains_real_source_pixels_and_dependency_changes(direct_authoring, changed):
    state = direct_authoring()
    if changed == "image":
        picture = state.workspace.paths.assets / "picture.png"
        Image.new("RGB", (32, 24), "blue").save(picture)
        state.kernel.artifacts.record_image_result(artifact_id="picture", owner_id="orchestrator",
            path=picture, status="available")
        authored = _execute(state, "write_file", {"path": "slides/slide_01.html", "content":
            '<section class="slide" id="slide-01"><h1>Authored 1</h1>'
            '<img src="../assets/picture.png"></section>'}, "author-picture")
        assert authored.ok, authored.content
        _inspect(state)
    else:
        _make_inspected(state)
    previous = state.workspace.page(1)
    if changed == "html":
        _patch(state)
    elif changed == "png":
        Image.new("RGB", (1600, 900), "red").save(state.workspace.paths.page_png(1))
    elif changed == "image":
        Image.new("RGB", (32, 24), "red").save(picture)
    else:
        css = state.workspace.paths.root / "base.css"
        css.write_text(css.read_text() + "\nh1 {color: red}\n")
    _, diagnostics = _gap(state)
    row = _page(diagnostics, 1)
    assert row["previously_inspected"] is True
    code = {"html": "html_changed", "png": "png_changed", "image": "resource_changed", "css": "resource_changed"}[changed]
    reason = _reason(row, code)
    assert "never_inspected" not in {item["code"] for item in row["reasons"]}
    if changed in {"image", "css"}:
        path = "assets/picture.png" if changed == "image" else "base.css"
        assert reason["changed_resources"] == [path]
        assert state.workspace.page(1).html_sha256 == previous.html_sha256
        assert state.workspace.page(1).png_sha256 == previous.png_sha256
        with pytest.raises(WorkspaceStateError) as stale:
            state.workspace.assert_ready_resources([1])
        assert stale.value.diagnostics["page_issues"] == diagnostics["page_issues"]


@pytest.mark.parametrize("overwrite", [False, True])
def test_completion_checks_each_pages_resources_once_and_never_partially_readies(direct_authoring, monkeypatch, overwrite):
    state = direct_authoring(pages=(1, 2, 3))
    pictures = {}
    for page in (1, 2, 3):
        picture = state.workspace.paths.assets / f"picture-{page}.png"
        Image.new("RGB", (32, 24), "blue").save(picture)
        pictures[page] = picture
        state.kernel.artifacts.record_image_result(artifact_id=f"picture-{page}", owner_id="orchestrator",
            path=picture, status="available")
        authored = _execute(state, "write_file", {"path": f"slides/slide_{page:02d}.html", "content":
            f'<section class="slide" id="slide-{page:02d}"><h1>Authored {page}</h1>'
            f'<img src="../assets/picture-{page}.png"></section>'}, f"author-picture-{page}")
        assert authored.ok, authored.content
        _inspect(state, page)
    _inspect_group(state, (1, 2, 3))
    before = state.workspace.pages()
    before_state = state.workspace.path.read_bytes()
    if overwrite:
        for page in (1, 3):
            Image.new("RGB", (32, 24), "red").save(pictures[page])
    validated, hashed = [], []
    validate = artifact_module.ArtifactStore.validate_render_receipt
    resource_hashes = artifact_module._html_resource_hashes

    def count_validate(store, page, **kwargs):
        validated.append(page)
        return validate(store, page, **kwargs)

    def count_hashes(root, html_path, **kwargs):
        hashed.append(html_path.name)
        return resource_hashes(root, html_path, **kwargs)

    monkeypatch.setattr(artifact_module.ArtifactStore, "validate_render_receipt", count_validate)
    monkeypatch.setattr(artifact_module, "_html_resource_hashes", count_hashes)
    if overwrite:
        _, diagnostics = _gap(state)
        assert [row["page"] for row in diagnostics["page_issues"]] == [1, 3]
        for page in (1, 3):
            reason = _reason(_page(diagnostics, page), "resource_changed")
            assert reason["changed_resources"] == [f"assets/picture-{page}.png"]
        assert state.workspace.pages() == before
        assert state.workspace.path.read_bytes() == before_state
        assert all(page.lifecycle == PageLifecycle.INSPECTED for page in state.workspace.pages())
    else:
        assert state.kernel.services.complete_deck()["status"] == "ready"
        assert all(page.lifecycle == PageLifecycle.READY for page in state.workspace.pages())
    assert validated == [1, 2, 3]
    assert hashed == ["slide_01.html", "slide_02.html", "slide_03.html"]


def test_partial_group_repair_is_directly_submittable_to_the_original_owner(direct_authoring):
    state = direct_authoring(pages=(1, 2))
    task, context, _ = _delegate(state, "slide", (1, 2), task_id="original-pair")
    drain(state.kernel.orchestration)
    for page in (1, 2):
        _make_inspected(state, page, context=context)
    _patch(state, 2, context=context)
    untouched = state.workspace.page(1)
    original_owner = state.kernel.owners.page_owner(1)
    tasks_before = copy.deepcopy(state.kernel.orchestration.snapshot()["tasks"])
    _, diagnostics = _gap(state)
    assert [row["page"] for row in diagnostics["page_issues"]] == [2]
    recovery, = diagnostics["recovery"]
    assert recovery["action"] == "delegate_submit"
    assert recovery["owner_id"] == task.owner_id
    assert recovery["group_id"] == task.group_id
    assert recovery["pages"] == [1, 2] and recovery["affected_pages"] == [2]
    submission = recovery["submit_task"]
    assert submission["task_id"] not in tasks_before
    assert submission["role"] == "slide" and submission["repair"] is True
    assert submission["pages"] == [1, 2] and submission["group_id"] == task.group_id
    assert submission["depends_on"] == []
    assert "slide_02.html" in submission["goal"]
    assert state.kernel.orchestration.snapshot()["tasks"] == tasks_before
    assert state.kernel.owners.page_owner(1) == original_owner
    submitted = _execute(state, "delegate_submit", {"tasks": [submission]}, "submit-recovery")
    assert submitted.ok, submitted.content
    drain(state.kernel.orchestration)
    resumed = state.kernel.orchestration.task(submission["task_id"])
    assert resumed.owner_id == original_owner.owner_id
    assert resumed.attempt == original_owner.attempt + 1
    assert resumed.group_id == task.group_id and resumed.pages == (1, 2)
    assert state.workspace.page(1) == untouched


def test_missing_html_recovery_requires_restoration_before_original_owner_submission(direct_authoring):
    state = direct_authoring(pages=(1, 2))
    state.kernel.orchestration.validate_submission = lambda tasks: SkillRuntime._validate_slide_submission(
        state.runtime, state.kernel.services, tasks)
    task, context, _ = _delegate(state, "slide", (1, 2), task_id="original-pair")
    drain(state.kernel.orchestration)
    for page in (1, 2):
        _make_inspected(state, page, context=context)
    missing = state.workspace.paths.page_html(1)
    original_source = missing.read_bytes()
    untouched_path = state.workspace.paths.page_html(2)
    untouched_source = untouched_path.read_bytes()
    untouched_state = state.workspace.page(2)
    original_owner = state.kernel.owners.page_owner(1)
    tasks_before = copy.deepcopy(state.kernel.orchestration.snapshot()["tasks"])
    missing.unlink()
    _, diagnostics = _gap(state)
    _reason(_page(diagnostics, 1), "source_missing")
    recovery, = diagnostics["recovery"]
    assert recovery["action"] == "delegate_submit"
    assert recovery["owner_id"] == original_owner.owner_id
    assert recovery["group_id"] == task.group_id
    assert recovery["pages"] == [1, 2] and recovery["affected_pages"] == [1]
    assert recovery["prerequisite"]["code"] == "slide_preparation_missing"
    assert recovery["prerequisite"]["missing_paths"] == ["slides/slide_01.html"]
    assert recovery["prerequisite"]["detail"]
    submission = recovery["submit_task"]
    assert submission["pages"] == [1, 2] and submission["group_id"] == task.group_id
    rejected = _execute(state, "delegate_submit", {"tasks": [submission]}, "before-scaffold-restore")
    assert not rejected.ok and rejected.error_code == "slide_preparation_missing"
    assert state.kernel.orchestration.snapshot()["tasks"] == tasks_before
    assert state.kernel.owners.page_owner(1) == original_owner
    assert not missing.exists()
    assert untouched_path.read_bytes() == untouched_source
    missing.write_bytes(original_source)
    accepted = _execute(state, "delegate_submit", {"tasks": [submission]}, "after-scaffold-restore")
    assert accepted.ok, accepted.content
    drain(state.kernel.orchestration)
    resumed = state.kernel.orchestration.task(submission["task_id"])
    assert resumed.owner_id == original_owner.owner_id
    assert resumed.attempt == original_owner.attempt + 1
    assert resumed.group_id == task.group_id and resumed.pages == (1, 2)
    assert untouched_path.read_bytes() == untouched_source
    assert state.workspace.page(2) == untouched_state
    state.kernel.tools.assert_closed()


def test_only_affected_groups_are_routed_while_self_and_unassigned_pages_stay_explicit(direct_authoring):
    state = direct_authoring(pages=tuple(range(1, 7)))
    first, first_context, _ = _delegate(state, "slide", (1, 2), task_id="affected-pair")
    other, other_context, _ = _delegate(state, "slide", (3, 4), task_id="unchanged-pair")
    drain(state.kernel.orchestration)
    for page, context in ((1, first_context), (2, first_context), (3, other_context), (4, other_context)):
        _make_inspected(state, page, context=context)
    _make_inspected(state, 5)
    _patch(state, 1, context=first_context)
    _patch(state, 5)
    _, diagnostics = _gap(state)
    assert {row["page"] for row in diagnostics["page_issues"]} == {1, 5, 6}
    child, = [row for row in diagnostics["recovery"] if row["action"] == "delegate_submit"]
    assert child["owner_id"] == first.owner_id
    assert child["pages"] == [1, 2] and child["affected_pages"] == [1]
    assert all(row.get("owner_id") != other.owner_id for row in diagnostics["recovery"])
    main, = [row for row in diagnostics["recovery"] if row["action"] == "continue_self"]
    assert main["owner_id"] == "orchestrator" and main["affected_pages"] == [5]
    missing, = [row for row in diagnostics["recovery"] if row["action"] == "author_or_delegate"]
    assert missing["affected_pages"] == [6] and not missing.get("owner_id")
    assert "submit_task" not in main and "submit_task" not in missing


def test_repeated_completion_diagnostics_do_not_close_issues_or_rewrite_history(direct_authoring):
    state = direct_authoring()
    _make_inspected(state)
    report = _execute(state, "verification_submit", {"status": "issues_found", "issues": [{
        "positions": [1], "problem": "已有图注仍需人工核实", "suggested_fix": "核实图注，保留其他内容"}]}, "report-issue")
    assert report.ok, report.content
    _patch(state)
    before_workspace = state.workspace.path.read_bytes()
    before_issues = state.kernel.issues.path.read_bytes()
    before_events = copy.deepcopy(state.kernel.tools.events)
    before_tasks = copy.deepcopy(state.kernel.orchestration.snapshot())
    for _ in range(3):
        _, diagnostics = _gap(state)
        assert _page(diagnostics, 1)["previously_inspected"]
        assert state.workspace.path.read_bytes() == before_workspace
        assert state.kernel.issues.path.read_bytes() == before_issues
        assert state.kernel.tools.events == before_events
        assert state.kernel.orchestration.snapshot() == before_tasks
    assert len(state.kernel.issues.blocking_issues()) == 1


def test_idle_delivers_diagnostics_as_real_harness_input_and_trace_not_tool_actions(direct_authoring):
    state = direct_authoring()
    assert _write(state).ok
    before = copy.deepcopy(state.kernel.tools.events)
    observed = []

    def receive_gap(request):
        gaps = []
        for message in reversed(request.messages):
            if message["role"] != "user" or not isinstance(message["content"], str):
                continue
            try:
                candidate = json.loads(message["content"])
            except json.JSONDecodeError:
                continue
            if candidate.get("harness_event") == "completion_gap" and candidate.get(
                    "diagnostics", {}).get("page_issues"):
                gaps.append(candidate)
        gap = gaps[0]
        assert gap["harness_event"] == "completion_gap"
        assert gap["diagnostics"]["recovery"][0]["action"] == "continue_self"
        page = next(row for row in gap["diagnostics"]["page_issues"] if row["page"] == 1)
        _reason(page, "never_inspected")
        assert state.kernel.tools.events == before
        observed.append(gap)
        return _call("inspect", {"page": [1]}, "actual-author-inspection")

    transport = ScriptedTransport([_response(), receive_gap, _response()])
    result = _run(transport, state.kernel.tools, state.context,
        idle_handler=lambda: SkillRuntime._orchestrator_idle(state.runtime, state.kernel.services),
        event_sink=state.kernel.live_trace.record_runtime_event)
    assert len(observed) == 1 and state.workspace.page(1).lifecycle == PageLifecycle.READY
    assert tool_boundary(result.checkpoint.messages).is_closed
    transactions = replay_tool_transactions(state.kernel.tools.events)
    assert transactions.uses == transactions.results == 2 and not transactions.open_ids
    assert {row["tool_name"] for row in state.kernel.tools.events} == {"write_file", "inspect"}
    events = [json.loads(line) for line in state.kernel.live_trace.path.read_text().splitlines()]
    gaps = [row for row in events if row["event_type"] == "deck_completion_checked"
            and row["payload"].get("status") == "incomplete"]
    assert len(gaps) == 1 and gaps[0]["payload"]["actor"] == "harness"
    full_page = gaps[0]["payload"]["diagnostics"]["page_issues"][0]
    prompt_page = observed[0]["diagnostics"]["page_issues"][0]
    assert full_page["html_path"] == "slides/slide_01.html"
    assert "html_path" not in prompt_page
    assert full_page["reasons"] == prompt_page["reasons"]
