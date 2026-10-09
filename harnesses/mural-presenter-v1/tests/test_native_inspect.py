"""统一 inspect：实际目标、渲染复用、延迟回执及查看与作者权限边界。"""
from __future__ import annotations

from geometry_fixture import geometry, require_browser, write_geometry

from pathlib import Path
import os
import sys

import pytest
from PIL import Image

from infer import build_inspect_handlers, _page_render_is_current
from owner.registry import OwnerRegistry

from runtime.tools import (
    STABLE_TOOL_ROLES,
    ToolCall,
    ToolContext,
    ToolExecutionError,
    ToolRuntime,
    WriteScope,
    _is_native_inspection_tool,
    register_tools,
)
from state.artifacts import ArtifactStore
from state.workspace import PageLifecycle, WorkspaceState, WorkspaceStateError, file_sha256


def test_shared_stage_has_no_template_grid_and_allows_owner_layout(tmp_path):
    require_browser(tmp_path)
    from playwright.sync_api import sync_playwright
    root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(root / "webui/studio"))
    from app.engine import render_env
    env = render_env({**os.environ, "TMPDIR": str(tmp_path)})
    css = (root / "skills/mural-presenter-v1/assets/base.css").read_text()
    html = '''<section class="slide active" id="slide-02"><div class="page-stage">
      <header class="authored-title">Title</header>
      <main class="authored-body">Body</main></div></section>'''
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=env["PPT_SKILL_BROWSER_EXE"],
            env=env, headless=True, args=["--no-sandbox"])
        try:
            page = browser.new_page(viewport={"width": 1600, "height": 900})
            page.set_content(f"<style>{css}</style>{html}")
            assert page.locator('.page-stage').evaluate('e=>getComputedStyle(e).display') == 'block'
            columns = lambda: page.evaluate("""() => ['.authored-title','.authored-body'].map(
              s=>{const e=document.querySelector(s); return [e.getBoundingClientRect().x, getComputedStyle(e).gridColumn]})""")
            assert columns()[0] == columns()[1]
            page.add_style_tag(content="#slide-02 .page-stage {display:grid;grid-template-columns:1fr 1fr} #slide-02 .authored-body {grid-column:2}")
            placed = columns()
            assert placed[1][0] > placed[0][0]
            assert placed[1][1] == "2"
        finally:
            browser.close()


def test_media_receipt_exposes_figure_parent_and_ineffective_object_position(tmp_path, monkeypatch):
    require_browser(tmp_path)
    import base64
    from io import BytesIO
    from playwright.sync_api import sync_playwright
    skill_scripts = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1/scripts"
    monkeypatch.syspath_prepend(str(skill_scripts))
    from _internal.render_deck import _LAYOUT_GEOMETRY_JS
    source = Image.new("RGB", (180, 320), "red")
    source.paste("blue", (0, 160, 180, 320))
    buf = BytesIO(); source.save(buf, format="PNG")
    data = base64.b64encode(buf.getvalue()).decode()
    html = f'''<!doctype html><style>body{{margin:0}}.slide{{position:relative;width:960px;height:540px;overflow:hidden}}
      .stage{{position:absolute;inset:0}}img{{display:block;width:100%;height:100%;object-fit:cover;object-position:50% 0%}}
      </style><section class="slide active" data-slide="1"><div class="stage"><figure class="closing-figure">
      <img src="data:image/png;base64,{data}"></figure></div></section>'''
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"], env={**os.environ, "TMPDIR": str(tmp_path)})
        try:
            page = browser.new_page(viewport={"width": 960, "height": 540})
            page.set_content(html)
            page.wait_for_function("document.querySelector('img').complete")
            before = page.screenshot()
            page.add_style_tag(content="img{object-position:50% 100%}")
            assert page.screenshot() == before
            broken = page.evaluate(_LAYOUT_GEOMETRY_JS)["images"][0]
            assert broken["natural_width"] == 180 and broken["natural_height"] == 320
            parent = broken["parents"][0]
            assert parent["element"] == "figure.closing-figure" and "40px" in parent["margin"]
            assert parent["height"] > 540 and broken["object_position"] == "50% 100%"
            page.add_style_tag(content="figure.closing-figure{position:absolute;inset:0;margin:0}")
            repaired = page.evaluate(_LAYOUT_GEOMETRY_JS)["images"][0]
            assert (repaired["x"], repaired["y"], repaired["width"], repaired["height"]) == (0, 0, 960, 540)
            assert page.screenshot() != before
        finally:
            browser.close()


def _make_workspace(tmp_path: Path, *, pages=(1,), author_role="slide") -> tuple[WorkspaceState, ArtifactStore]:
    """冻结画布并登记第 1 页（authored，未渲染）。"""
    workspace = WorkspaceState(tmp_path)
    write_geometry(
        tmp_path,
        geometry(
            width_px=1600,
            height_px=900,
        ),
    )
    artifacts = ArtifactStore(tmp_path, workspace_state=workspace)
    workspace.register_pages(pages)
    registry = OwnerRegistry(tmp_path, deck_id=workspace.deck_id)
    if author_role == "orchestrator":
        for page in pages:
            registry.claim_orchestrator_page(page)
    else:
        registry.accept(role="slide", pages=pages, group_id="fixture-pages")
    _author_page(workspace, artifacts, 1, "<html><body>v1</body></html>")
    return workspace, artifacts


def _author_page(
    workspace: WorkspaceState,
    artifacts: ArtifactStore,
    page: int,
    html: str,
) -> None:
    """写入并提交某页 canonical HTML，模拟 Slide Owner 的 write_file+auto-commit。"""
    path = workspace.paths.page_html(page)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    sha = file_sha256(path)
    workspace.mark_authored(page, html_sha256=sha)
    artifact_id = f"page-html-{page:02d}"
    owner = OwnerRegistry(workspace.paths.root, deck_id=workspace.deck_id).page_owner(page)
    assert owner is not None
    try:
        artifacts.artifact(artifact_id)
    except KeyError:
        artifacts.declare(
            artifact_id=artifact_id,
            kind="page_html",
            path=path,
            owner_id=owner.owner_id,
            page=page,
        )
    artifacts.commit(
        owner_id=owner.owner_id, artifact_ids=[artifact_id], source_hashes={}
    )


def _renderer(_html: Path, png: Path, canvas: dict):
    """确定性渲染桩：写一张同尺寸纯色 PNG。"""
    png.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (canvas["width_px"], canvas["height_px"]), (240, 236, 228)).save(png)
    return {"geometry": canvas, "loaded_resources": [], "failed_resources": []}


def _analyzer(target: Path, _q: str, _scope: str, _canvas: dict):
    """像素身份桩。"""
    return {"native_multimodal": True, "sha256": file_sha256(target)}


def _slide_context(workspace, pages=(1,)) -> ToolContext:
    owner = OwnerRegistry(workspace.paths.root, deck_id=workspace.deck_id).page_owner(pages[0])
    assert owner is not None
    return ToolContext(
        owner_id=owner.owner_id,
        role=owner.role,
        metadata={"pages": list(pages)},
    )


def _group_workspace(tmp_path, monkeypatch, *, inspected=True):
    from runtime.shared_objects import SharedSkillStore
    studio = tmp_path / "studio"
    monkeypatch.setenv("MURAL_STUDIO_DATA_DIR", str(studio))
    workspace, artifacts = _make_workspace(tmp_path / "deck", pages=(1, 2))
    _author_page(workspace, artifacts, 2, "<html><body>second</body></html>")
    skill = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1"
    SharedSkillStore(studio).pin(workspace.paths, skill, skill_name="mural-presenter-v1")
    handlers = build_inspect_handlers(workspace, artifacts, _renderer, _analyzer)
    for number in (1, 2) if inspected else ():
        payload = handlers[0](_slide_context(workspace, (1, 2)), {"page": number})
        handlers[1](payload["pending_inspection"])
    return workspace, artifacts, handlers


def test_group_inspection_has_real_pixels_and_only_completed_scopes_are_cached(tmp_path, monkeypatch):
    workspace, artifacts, (inspect, commit) = _group_workspace(tmp_path, monkeypatch)
    context = _slide_context(workspace, (1, 2))
    first = inspect(context, {"page": [1, 2]})
    assert first["native_multimodal"] is True
    with Image.open(workspace.paths.resolve(first["path"])) as sheet:
        assert sheet.width == 800 and sheet.height > 225
    assert first["pending_inspection"]["kind"] == "group"
    assert [item["page_uid"] for item in first["pending_inspection"]["members"]] == [workspace.page(n).page_uid for n in (1, 2)]
    assert inspect(context, {"page": [1, 2]})["native_multimodal"] is True
    commit(first["pending_inspection"])
    resumed = WorkspaceState(workspace.paths.root)
    inspect, _ = build_inspect_handlers(resumed, ArtifactStore(resumed.paths.root, workspace_state=resumed), _renderer, _analyzer)
    cached = inspect(context, {"page": [1, 2]})
    assert cached["inspection_cached"] is True and not cached["native_multimodal"]
    assert "pending_inspection" not in cached
    with pytest.raises(ToolExecutionError) as error:
        inspect(context, {"page": [1, 2], "question": "compare density"})
    assert error.value.code == "inspection_limit_reached"


@pytest.mark.parametrize("pages", [[1], [1, 2]])
def test_registered_inspect_array_reaches_pixels_and_commits_actual_scope(tmp_path, monkeypatch, pages):
    import json
    workspace, _, (handler, commit) = _group_workspace(tmp_path, monkeypatch, inspected=False)
    runtime = ToolRuntime()
    register_tools(runtime, {"inspect": handler}, require_complete=False)
    context = _slide_context(workspace, (1, 2))
    result = runtime.execute(context, ToolCall("native-array", "inspect", {"page": pages}))
    assert result.ok, result.content
    payload = json.loads(result.content)
    assert payload["pages"] == pages
    assert payload["kind"] == ("page" if len(pages) == 1 else "group")
    with Image.open(workspace.paths.resolve(payload["path"])) as pixels:
        assert pixels.width > 0 and pixels.height > 0
    assert workspace.page(1).lifecycle == PageLifecycle.RENDERED
    assert workspace.group_inspection_receipt(context.owner_id, (1, 2)) is None
    assert commit(payload["pending_inspection"]) is True
    if len(pages) == 1:
        assert workspace.page(1).lifecycle == PageLifecycle.INSPECTED
        assert workspace.group_inspection_receipt(context.owner_id, (1, 2)) is None
    else:
        pending = payload["pending_inspection"]
        receipt = workspace.inspection_receipt(pending["scope_key"], pending["members"])
        assert receipt["inspection_id"] == payload["inspection_id"]
        assert receipt["kind"] == "group"
        assert workspace.group_inspection_receipt(context.owner_id, (1, 2)) is None
        assert workspace.page(1).lifecycle == PageLifecycle.RENDERED
    assert len(runtime.events) == 2


def test_page_inspect_limits_repeated_new_questions_on_same_pixels(tmp_path):
    workspace, artifacts = _make_workspace(tmp_path / "deck")
    inspect, commit = build_inspect_handlers(workspace, artifacts, _renderer, _analyzer)
    context = _slide_context(workspace)
    payload = inspect(context, {"page": 1, "review_stage": "final", "question": "pass 0"})
    assert payload["native_multimodal"] is True
    assert commit(payload["pending_inspection"]) is True
    with pytest.raises(ToolExecutionError) as error:
        inspect(context, {"page": 1, "review_stage": "final", "question": "second fresh pass"})
    assert error.value.code == "inspection_limit_reached"
    assert error.value.diagnostics["prior_attempts"] == 1
    with pytest.raises(ToolExecutionError) as error:
        inspect(context, {"page": 1, "review_stage": "blocking_recheck",
                          "question": "merely claiming a blocking repair"})
    assert error.value.code == "inspection_limit_reached"


def test_blocking_recheck_cannot_bypass_unchanged_pixel_limit(tmp_path):
    workspace, artifacts = _make_workspace(tmp_path / "deck")
    inspect, commit = build_inspect_handlers(workspace, artifacts, _renderer, _analyzer)
    context = _slide_context(workspace)
    payload = inspect(context, {"page": 1, "review_stage": "blocking_recheck", "question": "retry 0"})
    assert commit(payload["pending_inspection"])
    with pytest.raises(ToolExecutionError) as error:
        inspect(context, {"page": 1, "review_stage": "blocking_recheck", "question": "same picture again"})
    assert error.value.code == "inspection_limit_reached"
    cached = inspect(context, {"page": 1, "review_stage": "blocking_recheck", "question": "retry 0"})
    assert cached["inspection_cached"] is True


def test_draft_checks_cannot_consume_first_final_review_budget(tmp_path):
    workspace, artifacts = _make_workspace(tmp_path / "deck")
    inspect, commit = build_inspect_handlers(workspace, artifacts, _renderer, _analyzer)
    context = _slide_context(workspace)
    payload = inspect(context, {"page": 1, "review_stage": "draft", "question": "draft 0"})
    assert commit(payload["pending_inspection"])
    final = inspect(context, {"page": 1, "review_stage": "final"})
    assert final["native_multimodal"] is True
    assert final["review_convergence"]["completed_page_checks"] == 1
    assert final["review_convergence"]["target_page_checks"] == 1
    assert commit(final["pending_inspection"])


def test_page_review_convergence_counts_across_real_pixel_changes(tmp_path):
    workspace, artifacts = _make_workspace(tmp_path / "deck")

    def changing_renderer(html, png, canvas):
        shade = len(html.read_text()) % 255
        png.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (canvas["width_px"], canvas["height_px"]), (shade, 100, 100)).save(png)
        return {"geometry": canvas, "loaded_resources": [], "failed_resources": []}

    inspect, commit = build_inspect_handlers(workspace, artifacts, changing_renderer, _analyzer)
    context = _slide_context(workspace)
    hashes = set()
    for index in range(3):
        _author_page(workspace, artifacts, 1, "<html><body>" + "x" * (index + 1) + "</body></html>")
        payload = inspect(context, {"page": 1, "review_stage": "final"})
        hashes.add(payload["png_sha256"])
        assert commit(payload["pending_inspection"])
    assert len(hashes) == 3
    assert workspace.inspection_attempt_count(kind="page", page=1) == 3
    _author_page(workspace, artifacts, 1, "<html><body>another repaired version</body></html>")
    payload = inspect(context, {"page": 1, "review_stage": "final"})
    assert payload["review_convergence"]["completed_page_checks"] == 3
    assert payload["review_convergence"]["target_page_checks"] == 1
    assert workspace.page(1).lifecycle == PageLifecycle.RENDERED
    assert commit(payload["pending_inspection"])
    assert workspace.inspection_attempt_count(kind="page", page=1) == 4


def test_group_completion_distinguishes_generated_and_consumed_sheet(tmp_path, monkeypatch):
    workspace, _, (inspect, commit) = _group_workspace(tmp_path, monkeypatch)
    context = _slide_context(workspace, (1, 2))
    assert workspace.group_inspection_receipt(context.owner_id, (1, 2)) is None
    pending = inspect(context, {"page": [1, 2]})
    assert workspace.paths.resolve(pending["path"]).is_file()
    assert workspace.group_inspection_receipt(context.owner_id, (1, 2)) is None
    commit(pending["pending_inspection"])
    resumed = WorkspaceState(workspace.paths.root)
    current = resumed.group_inspection_receipt(context.owner_id, (1, 2))
    assert current["inspection_id"] == pending["inspection_id"]
    assert resumed.group_inspection_receipt("other-owner", (1, 2)) is None
    assert resumed.group_inspection_receipt(context.owner_id, (1,)) is None
    assert resumed.group_inspection_receipt(context.owner_id, (2, 1)) is None


@pytest.mark.parametrize("change", ["html", "png", "contact_sheet", "presentation_language"])
def test_group_completion_does_not_report_stale_receipt_as_current(tmp_path, monkeypatch, change):
    import json
    workspace, artifacts, (inspect, commit) = _group_workspace(tmp_path, monkeypatch)
    context = _slide_context(workspace, (1, 2))
    pending = inspect(context, {"page": [1, 2]})
    commit(pending["pending_inspection"])
    if change == "html":
        _author_page(workspace, artifacts, 2, "<html><body>changed</body></html>")
    elif change in {"png", "contact_sheet"}:
        target = workspace.paths.page_png(2) if change == "png" else workspace.paths.resolve(pending["path"])
        Image.new("RGB", (800, 450), "blue").save(target)
    else:
        path = workspace.paths.root / "config.json"
        path.write_text(json.dumps({**json.loads(path.read_text()), "presentation_language": "zh"}))
    assert workspace.group_inspection_receipt(context.owner_id, (1, 2)) is None


def test_group_reminder_continues_original_owner_once_without_fabricated_tools(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from runtime.recovery import tool_boundary
    from runtime.skill_runtime import SkillRuntime
    from runtime.tools import ToolRuntime
    from test_agent_runtime import ScriptedTransport, _response, _run, _text

    workspace, _, (inspect, commit) = _group_workspace(tmp_path, monkeypatch)
    context = _slide_context(workspace, (1, 2))
    services = SimpleNamespace(workspace=workspace,
        owners=OwnerRegistry(workspace.paths.root, deck_id=workspace.deck_id))
    task = SimpleNamespace(role="slide", owner_id=context.owner_id, task_id="group-first")
    runtime = SkillRuntime.__new__(SkillRuntime)
    transport = ScriptedTransport([_response(_text("成员页已完成")), _response(_text("组复核尚有阻塞"))])
    checkpoints, tools = [], ToolRuntime()
    result = _run(transport, tools, context, agent_id=context.owner_id,
        idle_handler=runtime._group_review_reminder(services, task, None), checkpoint_sink=checkpoints.append)
    assert result.turns == 2 and result.tool_calls == 0 and not tools.events
    assert result.agent_id == context.owner_id
    event = json.loads(transport.calls[1][1].messages[-1]["content"])
    assert event["harness_event"] == "group_review_gap" and event["pages"] == [1, 2]
    assert event["uninspected_pages"] == []
    assert all(tool_boundary(checkpoint.messages).is_closed for checkpoint in checkpoints)
    assert runtime._group_review_gap(services, task) is not None
    assert runtime._group_review_reminder(services, task, result.checkpoint)() is None
    followup = SimpleNamespace(**{**vars(task), "task_id": "group-repair"})
    assert runtime._group_review_reminder(services, followup, result.checkpoint)() is not None
    pending = inspect(context, {"page": [1, 2]})
    assert runtime._group_review_gap(services, task) is not None
    commit(pending["pending_inspection"])
    assert runtime._group_review_gap(services, task) is None
    assert runtime._group_review_reminder(services, followup, None)() is None


@pytest.mark.parametrize("role,pages", [("slide", (1,)), ("image", (1, 2)), ("research", (1, 2))])
def test_group_reminder_does_not_add_review_to_single_or_experts(role, pages):
    from types import SimpleNamespace
    from runtime.skill_runtime import SkillRuntime
    runtime = SkillRuntime.__new__(SkillRuntime)
    services = SimpleNamespace(owners=SimpleNamespace(get=lambda _: SimpleNamespace(pages=pages)))
    task = SimpleNamespace(role=role, owner_id="owner")
    assert runtime._group_review_gap(services, task) is None
    assert runtime._group_review_reminder(services, task, None) is None


def test_registered_vision_page_waits_for_successful_consumption(tmp_path):
    workspace, artifacts = _make_workspace(tmp_path, author_role="orchestrator")
    artifacts.render_pages([1], _renderer)
    handler = build_inspect_handlers(workspace, artifacts, None, _analyzer)[0]
    context = ToolContext(owner_id="orchestrator", role="orchestrator", read_scopes=(WriteScope("renders"),))
    args = {"path": "renders/slide_01.png", "question": "read typography"}
    pending = handler(context, args)
    assert workspace.page(1).lifecycle == PageLifecycle.RENDERED
    assert pending["pending_inspection"]["page_uid"] == workspace.page(1).page_uid
    assert handler(context, args)["native_multimodal"] is True
    _, commit = build_inspect_handlers(workspace, artifacts, _renderer, _analyzer)
    commit(pending["pending_inspection"])
    assert workspace.page(1).lifecycle == PageLifecycle.INSPECTED
    assert handler(context, args)["inspection_cached"] is True
    with pytest.raises(ToolExecutionError) as error:
        handler(context, {**args, "question": "check source labels"})
    assert error.value.code == "inspection_limit_reached"
    assert handler(ToolContext(owner_id="other", role="orchestrator", read_scopes=(WriteScope("renders"),)), args)["native_multimodal"] is True


def test_completed_pixel_receipt_rebinds_same_pixels_after_html_only_change(tmp_path):
    workspace, artifacts = _make_workspace(tmp_path)
    inspect, commit = build_inspect_handlers(workspace, artifacts, _renderer, _analyzer)
    context = _slide_context(workspace)
    first = inspect(context, {"page": 1})
    commit(first["pending_inspection"])
    _author_page(workspace, artifacts, 1, "<html><body>v1</body><!-- comment --></html>")
    cached = inspect(context, {"page": 1})
    assert cached["inspection_cached"] and not cached["native_multimodal"]
    assert "pending_inspection" not in cached
    assert workspace.page(1).lifecycle == PageLifecycle.INSPECTED
    receipt = workspace.inspection_receipt(cached["scope_key"], [_inspection_member_for_test(workspace, 1)])
    assert receipt["reused_from_inspection_id"] == first["inspection_id"]
    assert workspace._read_state()["events"][-1]["type"] == "inspection_reused"
    with pytest.raises(ToolExecutionError) as error:
        inspect(context, {"page": 1, "question": "another visual purpose"})
    assert error.value.code == "inspection_limit_reached"


def _inspection_member_for_test(workspace, number):
    page = workspace.page(number)
    return {key: getattr(page, key) for key in ("page", "page_uid", "html_sha256", "png_sha256")}


def test_group_pixel_receipt_rebinds_without_regenerating_contact_sheet(tmp_path, monkeypatch):
    workspace, artifacts, (inspect, commit) = _group_workspace(tmp_path, monkeypatch)
    context = _slide_context(workspace, (1, 2))
    first = inspect(context, {"page": [1, 2]})
    commit(first["pending_inspection"])
    _author_page(workspace, artifacts, 2, "<html><body>second</body><!-- comment --></html>")
    assert inspect(context, {"page": 2})["inspection_cached"]
    monkeypatch.setattr("infer._write_group_contact_sheet", lambda *_: pytest.fail("unchanged pixels rebuilt"))
    cached = inspect(context, {"page": [1, 2]})
    assert cached["inspection_cached"] and not cached["native_multimodal"]
    assert cached["current_inspection"]["reused_from_inspection_id"] == first["inspection_id"]
    assert cached["current_inspection"]["members"][1]["html_sha256"] == workspace.page(2).html_sha256


def test_delayed_inspection_rejects_new_version_even_when_new_render_is_current(tmp_path):
    workspace, artifacts = _make_workspace(tmp_path)
    inspect, commit = build_inspect_handlers(workspace, artifacts, _renderer, _analyzer)
    old = inspect(_slide_context(workspace), {"page": 1})
    _author_page(workspace, artifacts, 1, "<html><body>changed while pending</body></html>")
    artifacts.render_pages([1], _renderer)
    rejected = commit(old["pending_inspection"])
    assert rejected["status"] == "stale"
    assert rejected["diagnostics"]["code"] == "inspection_version_changed"
    assert workspace.page(1).lifecycle == PageLifecycle.RENDERED


@pytest.mark.parametrize("result", [{"status": "repair_required"}, {"status": "repair_handoff"}])
def test_owner_repair_handoff_is_not_automatically_ready(result):
    from types import SimpleNamespace
    from infer import PresenterKernel
    changes = []
    kernel = SimpleNamespace(workspace=SimpleNamespace(
        page=lambda _: SimpleNamespace(lifecycle=PageLifecycle.INSPECTED),
        mark_ready=changes.append))
    wrapped = PresenterKernel._wrap_runner(kernel, lambda *_: result)
    assert wrapped(SimpleNamespace(role="slide", pages=[1]), None) == result
    assert changes == []


# --- 角色边界 -----------------------------------------------------------------


def test_all_visual_roles_use_one_public_inspect_tool():
    assert STABLE_TOOL_ROLES["inspect"] == frozenset({"slide", "orchestrator", "research", "image", "critic"})
    assert "inspect_group" not in STABLE_TOOL_ROLES
    assert "vision_analyze" not in STABLE_TOOL_ROLES


def test_asset_inspection_needs_no_deck_plan_and_never_advances_pages(tmp_path):
    from qc.review_hook import DEFAULT_INSPECTION_QUESTIONS
    workspace = WorkspaceState(tmp_path)
    target = tmp_path / "assets/photo.png"
    target.parent.mkdir(exist_ok=True)
    Image.new("RGB", (64, 96), "red").save(target)
    context = ToolContext("image-task", "image", read_scopes=(WriteScope("assets"),))
    inspect, commit = build_inspect_handlers(workspace, ArtifactStore(tmp_path, workspace_state=workspace), None, _analyzer)
    result = inspect(context, {"path": "assets/photo.png", "question": "重点检查主体"})
    assert result["kind"] == "asset" and not result["deck_page_inspection"]
    assert result["question"].startswith(DEFAULT_INSPECTION_QUESTIONS["asset"])
    assert result["question"].endswith("重点检查主体")
    assert result["pages"] == [] and not result["author_inspection"]
    assert commit(result["pending_inspection"])
    assert workspace.pages() == ()
    assert inspect(context, {"path": "assets/photo.png", "question": "重点检查主体"})["inspection_cached"]
    with pytest.raises(ToolExecutionError) as error:
        inspect(context, {"path": "assets/photo.png", "question": "检查图例"})
    assert error.value.code == "inspection_limit_reached"
    Image.new("RGB", (64, 96), "blue").save(target)
    assert commit(result["pending_inspection"])["status"] == "stale"


def test_group_default_question_checks_semantic_color_system_not_uniformity():
    from qc.review_hook import DEFAULT_INSPECTION_QUESTIONS, InspectionClosed, inspection_with_critic_question
    question = DEFAULT_INSPECTION_QUESTIONS["group"]
    assert "不同页不必完全同色" in question
    assert "相同语义角色应稳定" in question
    assert "不同模板" in question and "ground_map" in question
    prepared = inspection_with_critic_question(InspectionClosed(
        agent_id="orchestrator", role="orchestrator", inspection_id="group-1",
        html_sha256="", png_sha256="png", png_path="renders/group.png",
        workspace_root="/tmp/deck", kind="group",
        members=({"page": 1}, {"page": 2}), question=question,
    ))
    assert "属于同一设计系统" in prepared.critic_question
    assert "不同模板拼接" in prepared.critic_question


@pytest.mark.parametrize("target", ["../outside.png", "private/photo.png", "assets/escape.png", "assets/alias.png", "assets/missing.png", "assets/broken.png"])
def test_inspect_path_safety_and_decode_errors_do_not_change_state(tmp_path, target):
    from runtime.tools import ToolContractError, ToolExecutionError
    workspace = WorkspaceState(tmp_path / "deck")
    assets = workspace.paths.root / "assets"
    assets.mkdir(exist_ok=True)
    private = workspace.paths.root / "private"
    private.mkdir()
    Image.new("RGB", (20, 20), "blue").save(private / "photo.png")
    Image.new("RGB", (20, 20), "red").save(tmp_path / "outside.png")
    (assets / "escape.png").symlink_to(tmp_path / "outside.png")
    (assets / "alias.png").symlink_to(private / "photo.png")
    (assets / "broken.png").write_text("not an image")
    before = workspace.path.read_bytes()
    inspect, _ = build_inspect_handlers(workspace, ArtifactStore(workspace.paths.root, workspace_state=workspace), None, _analyzer)
    with pytest.raises((ToolExecutionError, ToolContractError)):
        inspect(ToolContext("image-task", "image", read_scopes=(WriteScope("assets"),)), {"path": target})
    assert workspace.path.read_bytes() == before


def test_native_inspection_helper_recognizes_the_authoring_surface() -> None:
    assert _is_native_inspection_tool("inspect")
    assert _is_native_inspection_tool("inspect_group")
    assert _is_native_inspection_tool("vision_analyze")
    assert not _is_native_inspection_tool("render")


# --- 四条渲染路径 -------------------------------------------------------------


def test_inspect_renders_when_no_png_exists(tmp_path: Path) -> None:
    workspace, artifacts = _make_workspace(tmp_path)
    inspect, _commit = build_inspect_handlers(
        workspace, artifacts, _renderer, _analyzer
    )
    assert workspace.page(1).lifecycle == PageLifecycle.AUTHORED
    payload = inspect(_slide_context(workspace), {"page": 1})
    assert payload["native_multimodal"] is True
    assert payload["path"] == "renders/slide_01.png"
    assert workspace.paths.resolve("renders/slide_01.png").is_file()
    # 渲染但未提交 inspection（延迟提交）：仍是 RENDERED，未 INSPECTED。
    assert workspace.page(1).lifecycle == PageLifecycle.RENDERED
    assert "pending_inspection" in payload


def test_inspect_reuses_current_render_without_re_rendering(tmp_path: Path) -> None:
    workspace, artifacts = _make_workspace(tmp_path)
    inspect, _commit = build_inspect_handlers(
        workspace, artifacts, _renderer, _analyzer
    )
    inspect(_slide_context(workspace), {"page": 1})
    assert _page_render_is_current(workspace, artifacts, 1)
    calls: list[int] = []

    def counting_renderer(html, png, canvas):
        calls.append(1)
        return _renderer(html, png, canvas)

    inspect2, _c2 = build_inspect_handlers(
        workspace, artifacts, counting_renderer, _analyzer
    )
    payload = inspect2(_slide_context(workspace), {"page": 1})
    assert calls == []  # 复用，不重复渲染
    assert payload["native_multimodal"] is True


def test_inspect_re_renders_after_html_changes(tmp_path: Path) -> None:
    workspace, artifacts = _make_workspace(tmp_path)
    calls: list[int] = []

    def counting_renderer(html, png, canvas):
        calls.append(1)
        return _renderer(html, png, canvas)

    inspect, _commit = build_inspect_handlers(
        workspace, artifacts, counting_renderer, _analyzer
    )
    inspect(_slide_context(workspace), {"page": 1})
    assert calls == [1]
    # 编辑页面：旧渲染失效，重复 inspect 触发一次新渲染。
    _author_page(workspace, artifacts, 1, "<html><body>v2 edited</body></html>")
    assert not _page_render_is_current(workspace, artifacts, 1)
    inspect(_slide_context(workspace), {"page": 1})
    assert calls == [1, 1]


def test_identical_html_commit_preserves_inspection_and_cached_render(tmp_path):
    workspace, artifacts = _make_workspace(tmp_path)
    inspect, commit = build_inspect_handlers(workspace, artifacts, _renderer, _analyzer)
    inspected = inspect(_slide_context(workspace), {"page": 1})
    commit(inspected["pending_inspection"])
    before = workspace.page(1)
    _author_page(workspace, artifacts, 1, workspace.paths.page_html(1).read_text())
    assert workspace.page(1) == before
    def must_not_render(*_):
        pytest.fail("an identical write must not invalidate current pixels")
    inspect, _ = build_inspect_handlers(workspace, artifacts, must_not_render, _analyzer)
    assert inspect(_slide_context(workspace), {"page": 1})["png_sha256"] == before.png_sha256


def test_native_render_diagnostics_survive_cached_inspect_and_resume(tmp_path):
    workspace, artifacts = _make_workspace(tmp_path)
    calls = []
    diagnostic = {'stdout': 'page-body is outside the canvas', 'stderr': '',
                  'ref': '_trace/tool-output/render-diagnostic.jsonl', 'truncated': False}
    def renderer(html, png, canvas):
        calls.append(1)
        return {**_renderer(html, png, canvas), 'diagnostics': diagnostic}
    from infer import PresenterKernel
    handler = PresenterKernel._render_handler(type('Services', (), {
        'artifacts': artifacts, 'workspace': workspace})(), renderer)
    result = handler(_slide_context(workspace), {'pages': [1]})
    recorded = result['pages'][0]['render_diagnostics']
    assert {key: recorded[key] for key in diagnostic} == diagnostic
    assert set(recorded['artifact_timings_s']) == {'png_validation', 'resource_validation', 'workspace_commit'}
    assert all(value >= 0 for value in recorded['artifact_timings_s'].values())
    assert result['pages'][0]['geometry'] == workspace.require_geometry()
    resumed = WorkspaceState(tmp_path)
    resumed_artifacts = ArtifactStore(tmp_path, workspace_state=resumed)
    inspect, _ = build_inspect_handlers(resumed, resumed_artifacts, renderer, _analyzer)
    result = inspect(_slide_context(workspace), {'page': 1})
    assert result['render_diagnostics'] == recorded
    assert result['geometry'] == workspace.require_geometry()
    assert calls == [1]
    assert resumed.page(1).lifecycle == PageLifecycle.RENDERED


@pytest.mark.parametrize("page_geometry", [{}, {
    "images": [{"src": "assets/keyboard.png"}],
    "media": [{"background": True, "src": "assets/colors.png"}, {"kind": "code-visual"}],
    "text_boxes": [{"text": "Sources: pp. 2, 10"}],
}])
def test_skill_renderer_keeps_its_sanitized_command_diagnostics(tmp_path, monkeypatch, page_geometry):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from runtime import production
    from runtime.diagnostics import command_scope
    workspace, _ = _make_workspace(tmp_path)
    result = SimpleNamespace(stdout='block_overflow in page-body', stderr='',
        diagnostics={'ref': '_trace/tool-output/public.jsonl', 'redacted': True})
    _renderer(workspace.paths.page_html(1), workspace.paths.page_png(1), workspace.require_geometry())
    cache = tmp_path / "_trace/render-cache/page_01.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(__import__("json").dumps({"canvas": workspace.require_geometry(),
        "pages": [{"page": 1, "geometry": page_geometry,
                   "presentation_language": workspace.creation_config()["presentation_language"],
                   "input_hashes": {"slides/slide_01.html": file_sha256(workspace.paths.page_html(1)),
                                    "base.css": file_sha256(tmp_path / "base.css")},
                   "png_sha256": file_sha256(workspace.paths.page_png(1)),
                   "loaded_resources": ["assets/dynamic.png"], "failed_resources": []}]}))
    environments = []
    def command(*_args, **kwargs):
        environments.append(kwargs['environment'])
        return result
    monkeypatch.setattr(production, '_run_command', command)
    snapshot = SimpleNamespace(name="mural-presenter-v1", target=tmp_path/'pinned-skill',
        resolve_skill_path=lambda _: tmp_path/'approved.py',
        capture_runtime_fonts=lambda: None)
    slots = SimpleNamespace(acquire=lambda *args, **kwargs: nullcontext())
    skill = Path(__file__).resolve().parents[3] / 'skills/mural-presenter-v1'
    renderer = production.SkillRenderer(tmp_path, production.ProductionSettings(skill_source=skill),
        {}, slots, snapshot)
    from runtime.live_trace import LiveEventJournal, iter_jsonl_events
    journal = LiveEventJournal(tmp_path / "_trace/live-events.jsonl")
    renderer.bind_live_trace(journal)
    with command_scope('inspect-render', {'role': 'slide', 'owner_id': 'owner-A', 'attempt': 2}, lambda _: None):
        receipt = renderer(workspace.paths.page_html(1), workspace.paths.page_png(1), workspace.require_geometry())
    renders = [event for event in iter_jsonl_events(journal.path)
               if event.get("event_type") == "page_render_completed"]
    assert len(renders) == 1
    assert renders[0]["role"] == "slide"
    assert renders[0]["payload"]["page"] == 1
    assert set(renders[0]["payload"]) >= {"page", "slot_wait_s", "browser_host_ready_s", "skill_command_s", "total_s", "browser_reused"}
    assert environments[0]['MURAL_RENDER_ATTEMPT_ID'] == 'slide-owner-A-attempt-2'
    assert 'MURAL_RENDER_ATTEMPT_ID' not in renderer._render_environment()
    diagnostic = {'stdout': result.stdout, 'stderr': '', **result.diagnostics}
    if page_geometry:
        diagnostic.update(visible_images=[page_geometry['images'][0], page_geometry['media'][0]],
                          rendered_text=['Sources: pp. 2, 10'])
    actual = dict(receipt['diagnostics'])
    timings = actual.pop('timings_s')
    assert set(timings) == {'slot_wait', 'browser_host_ready', 'skill_command', 'font_receipt', 'receipt_decode', 'total'}
    assert all(value >= 0 for value in timings.values())
    assert actual.pop('renderer_timings_s') == {}
    assert actual.pop('browser_reused') is False
    assert actual.pop('browser_pid') is None
    assert actual == diagnostic
    assert receipt['loaded_resources'] == ['assets/dynamic.png']
    assert receipt['failed_resources'] == []


def test_renderer_attempt_identity_is_isolated_between_concurrent_owners(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from runtime.production import ProductionSettings, SkillRenderer
    from runtime.diagnostics import command_scope
    workspace, _ = _make_workspace(tmp_path)
    skill = Path(__file__).resolve().parents[3] / 'skills/mural-presenter-v1'
    renderer = SkillRenderer(workspace.paths.root, ProductionSettings(skill_source=skill),
                             {'MURAL_RENDER_ATTEMPT_ID': 'stale-inherited-id'}, None, None)
    barrier = Barrier(2)
    def attempt(role, owner):
        with command_scope('render-' + owner, {'role': role, 'owner_id': owner, 'attempt': 3}, lambda _: None):
            barrier.wait(timeout=10)
            return renderer._render_environment()['MURAL_RENDER_ATTEMPT_ID']
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(attempt, 'slide', 'A'), pool.submit(attempt, 'orchestrator', 'B')
        assert first.result() == 'slide-A-attempt-3'
        assert second.result() == 'orchestrator-B-attempt-3'
    assert 'MURAL_RENDER_ATTEMPT_ID' not in renderer._render_environment()


def test_inspect_render_failure_returns_diagnostics_without_image(
    tmp_path: Path,
) -> None:
    workspace, artifacts = _make_workspace(tmp_path)

    def failing_renderer(html, png, canvas):
        # 渲染桩报告坏资源，让 render_pages 抛 ArtifactStateError。
        png.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (canvas["width_px"], canvas["height_px"]), (0, 0, 0)).save(png)
        return {"geometry": canvas, "loaded_resources": [], "failed_resources": ["assets/missing.png"]}

    inspect, _commit = build_inspect_handlers(
        workspace, artifacts, failing_renderer, _analyzer
    )
    payload = inspect(_slide_context(workspace), {"page": 1})
    assert payload["render_ok"] is False
    assert payload["native_multimodal"] is False
    assert "diagnostics" in payload
    assert "pending_inspection" not in payload
    # 未推进 INSPECTED。
    assert workspace.page(1).lifecycle != PageLifecycle.INSPECTED


# --- 延迟 inspection 提交 -----------------------------------------------------


def test_commit_inspection_advances_lifecycle_only_when_invoked(
    tmp_path: Path,
) -> None:
    workspace, artifacts = _make_workspace(tmp_path)
    inspect, commit = build_inspect_handlers(
        workspace, artifacts, _renderer, _analyzer
    )
    payload = inspect(_slide_context(workspace), {"page": 1})
    # inspect 本身不提交。
    assert workspace.page(1).lifecycle == PageLifecycle.RENDERED
    commit(payload["pending_inspection"])
    assert workspace.page(1).lifecycle == PageLifecycle.INSPECTED
    assert workspace.page(1).inspection_id == payload["inspection_id"]


def test_inspect_page_scope_denied_for_unowned_page(tmp_path: Path) -> None:
    workspace, artifacts = _make_workspace(tmp_path)
    workspace.register_pages([1, 2])
    OwnerRegistry(tmp_path, deck_id=workspace.deck_id).accept(role="slide", pages=[2], group_id="other-page")
    inspect, _commit = build_inspect_handlers(
        workspace, artifacts, _renderer, _analyzer
    )
    from runtime.tools import ToolExecutionError

    with pytest.raises(ToolExecutionError) as denied:
        inspect(_slide_context(workspace, pages=(2,)), {"page": 1})
    assert denied.value.code == "read_scope_denied"
    assert "无权查看" in str(denied.value)


@pytest.mark.parametrize("page", [1, [1]])
def test_orchestrator_reviews_delegated_page_without_authoring_side_effects(tmp_path, page):
    workspace, artifacts = _make_workspace(tmp_path)
    owner = OwnerRegistry(tmp_path, deck_id=workspace.deck_id).page_owner(1)
    context = ToolContext(owner_id="orchestrator", role="orchestrator", read_scopes=(WriteScope("renders"),))
    before = (tmp_path / "_state/owners.json").read_bytes()
    inspect, commit = build_inspect_handlers(workspace, artifacts, _renderer, _analyzer)
    result = inspect(context, {"page": page})
    assert not result["author_inspection"]
    assert result["rendered_pages"] == [1]
    assert commit(result["pending_inspection"])
    assert workspace.page(1).lifecycle == PageLifecycle.RENDERED
    assert workspace._read_state()["events"][-1]["type"] == "page_observed"
    assert not workspace.page(1).inspection_id
    assert before == (tmp_path / "_state/owners.json").read_bytes()
    assert OwnerRegistry(tmp_path, deck_id=workspace.deck_id).page_owner(1).owner_id == owner.owner_id
    assert not context.may_write("slides/slide_01.html")


def test_group_contact_sheet_renders_only_stale_pages_without_per_page_checks(tmp_path, monkeypatch):
    workspace, artifacts, _ = _group_workspace(tmp_path, monkeypatch, inspected=False)
    artifacts.render_pages([1], _renderer)
    rendered = []
    def render(html, png, canvas):
        rendered.append(png.name)
        return _renderer(html, png, canvas)
    inspect, commit = build_inspect_handlers(workspace, artifacts, render, _analyzer)
    context = _slide_context(workspace, pages=(1, 2))
    result = inspect(context, {"page": [1, 2]})
    assert rendered == ["slide_02.png"]
    assert result["rendered_pages"] == [2]
    assert result["pending_inspection"]["kind"] == "group"
    assert "不替代" in result["note"]
    assert commit(result["pending_inspection"])
    assert all(workspace.page(p).lifecycle == PageLifecycle.RENDERED for p in (1, 2))
    assert not any(workspace.page(p).inspection_id for p in (1, 2))
    cached = inspect(context, {"page": [1, 2]})
    assert cached["inspection_cached"] and cached["render_reused"]
    assert rendered == ["slide_02.png"]


def test_group_receipt_rejects_changed_member_and_rechecks_just_that_group(tmp_path, monkeypatch):
    workspace, artifacts, (inspect, commit) = _group_workspace(tmp_path, monkeypatch)
    context = _slide_context(workspace, (1, 2))
    previous = inspect(context, {"page": [1, 2]})
    _author_page(workspace, artifacts, 2, "<html><body>updated second</body></html>")
    def changed_pixels(_html, png, canvas):
        Image.new("RGB", (canvas["width_px"], canvas["height_px"]), (30, 60, 90)).save(png)
        return {"geometry": canvas, "loaded_resources": [], "failed_resources": []}
    artifacts.render_pages([2], changed_pixels)
    current = inspect(context, {"page": 2})
    commit(current["pending_inspection"])
    rejected = commit(previous["pending_inspection"])
    assert rejected["status"] == "stale"
    assert rejected["diagnostics"]["code"] == "inspection_version_changed"
    new_group = inspect(context, {"page": [1, 2]})
    assert new_group["native_multimodal"] is True
    commit(new_group["pending_inspection"])
    assert inspect(context, {"page": [1, 2]})["inspection_cached"] is True


def test_same_input_rerender_preserves_author_inspection(tmp_path):
    workspace, artifacts = _make_workspace(tmp_path)
    inspect, commit = build_inspect_handlers(workspace, artifacts, _renderer, _analyzer)
    context = _slide_context(workspace)
    first = inspect(context, {"page": 1})
    assert commit(first["pending_inspection"])
    before = workspace.page(1)
    assert before.lifecycle == PageLifecycle.INSPECTED

    Image.new("RGB", (1600, 900), (1, 2, 3)).save(workspace.paths.page_png(1))

    def alternate_pixels(_html: Path, png: Path, canvas: dict):
        Image.new("RGB", (canvas["width_px"], canvas["height_px"]), (20, 30, 40)).save(png)
        return {"geometry": canvas, "loaded_resources": [], "failed_resources": []}

    rendered, = artifacts.render_pages([1], alternate_pixels)
    after = workspace.page(1)
    assert rendered.lifecycle == PageLifecycle.INSPECTED
    assert after.lifecycle == PageLifecycle.INSPECTED
    assert after.inspection_id == before.inspection_id
    assert after.html_sha256 == before.html_sha256
    assert after.png_sha256 != before.png_sha256
    assert after.inspected_png_sha256 == after.png_sha256
    receipt = artifacts.render_receipt(1)
    assert receipt["render_inputs_unchanged"] is True
    assert receipt["pixel_hash_changed_with_same_inputs"] is True
    assert receipt["preserved_inspection_on_equivalent_rerender"] is True


@pytest.mark.parametrize("change_source", [False, True])
def test_page_receipt_rejects_changed_uid_before_any_version_recovery(tmp_path, change_source):
    workspace, artifacts = _make_workspace(tmp_path)
    inspect, commit = build_inspect_handlers(workspace, artifacts, _renderer, _analyzer)
    pending = inspect(_slide_context(workspace), {"page": 1})
    with workspace._lock:
        state = workspace._read_state()
        state["pages"]["1"]["page_uid"] = "replacement-page"
        workspace._write_state(state)
    if change_source:
        _author_page(workspace, artifacts, 1, "<html><body>new identity and source</body></html>")
    invalidated = commit(pending["pending_inspection"])
    assert invalidated["status"] == "stale"
    assert invalidated["diagnostics"]["code"] == "page_identity_changed"
    expected = PageLifecycle.AUTHORED if change_source else PageLifecycle.RENDERED
    assert workspace.page(1).lifecycle == expected
