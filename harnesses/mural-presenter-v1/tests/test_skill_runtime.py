from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
from PIL import Image

from infer import run_presenter
from runtime.config import RunMode, RunSpec
from runtime.production import ProductionSettings, read_scopes_for_role
from runtime.production import ProductionToolServices
from runtime.session import (
    ModelRequest,
    ProviderChannel,
    TransportResponse,
    replay_provider_events,
)
from runtime.skill_runtime import (
    SkillRuntime,
    _attachment_manifest_prompt_view,
    _completion_gap_prompt_view,
    _is_image_metadata_only_task,
)
from owner.context import OwnerPacket
from orchestration.manager import TaskRecord, TaskStatus
from runtime.tools import ToolContext, ToolContractError, ToolExecutionError


REPOSITORY = Path(__file__).resolve().parents[3]
SKILL = REPOSITORY / "skills" / "mural-presenter-v1"
SKILL_NAME = SKILL.name


DECK_PLAN = """# Runtime Vertical Slice
## 任务说明
清楚显示 Agent 决策与 Harness 机械执行的边界。
## 整体设计规范
浅色画布，深蓝标题，暖橙强调；正文与标题使用清晰的无衬线字体。
"""

PAGE_PLAN = """# Agent 决策，Harness 校验并传播
## 本页任务
用一页解释共享运行时边界。
## 页面内容与证据
Agent 决定内容与视觉；Harness 校验权限、闭合工具并传播结果。
## 视觉表达与约束
三段水平流程；三个阶段共享中心线；箭头只连接相邻阶段。
## 讲稿
模型负责判断，运行时负责把判断安全、可恢复地变成产物。
"""

SLIDE_HTML = """<section class="slide frame-content page-content family-freeform composition-sequence canvas-variant--base page-owned-composition title-long" id="slide-01" data-slide="01" data-page-type="content" data-frame="content" data-page-family="freeform" data-composition="sequence" data-canvas-variant="base" data-layout-contract="page-owned">
  <div class="page-background deck-background" data-slide-owned="background" aria-hidden="true"></div>
  <div class="slide-inner">
    <div class="page-frame page-frame--content">
      <header class="page-header page-header--content" data-scaffold-owned="header">
        <h1 class="page-title">Agent 决策，Harness 校验并传播</h1>
      </header>
      <main class="page-body"><div class="content-stage" data-slide-owned="body">
        <div class="composition composition--sequence" data-layout-seed="sequence" style="display:flex;gap:40px">
          <strong>Agent 决策</strong><strong>Harness 校验</strong><strong>Harness 传播</strong>
        </div>
      </div></main>
    </div>
  </div>
</section>
"""


def test_image_metadata_only_repair_detection_is_narrow() -> None:
    metadata_task = TaskRecord(
        task_id="image-p3-metadata-close",
        owner_id="owner-image",
        attempt=2,
        role="image",
        pages=(),
        group_id="image",
        dependencies=(),
        status=TaskStatus.PENDING,
        repair=True,
        asset_ids=("vent-rov-sampling",),
        goal="仅补 P3 既有素材的 source/license/purpose/notes；不要下载、裁切、替换图片或重做像素检查。",
    )
    replacement_task = TaskRecord(
        task_id="image-p3-replace",
        owner_id="owner-image",
        attempt=2,
        role="image",
        pages=(3,),
        group_id="image",
        dependencies=(),
        status=TaskStatus.PENDING,
        repair=True,
        goal="为 P3 重新寻找并替换低清素材，必要时下载和裁切。",
    )
    assert _is_image_metadata_only_task(metadata_task) is True
    assert _is_image_metadata_only_task(replacement_task) is False


def test_owner_packet_exposes_source_policy_not_internal_profile() -> None:
    packet = OwnerPacket(
        owner_id="owner-image",
        role="image",
        pages=(),
        group_id="image",
        attempt=1,
        dependencies=(),
        raw_user_query="只基于附件制作一页。",
        attachment_paths=(),
        authoring_profile="material-grounded",
    ).to_dict()
    assert "authoring_profile" not in packet
    assert packet["source_policy"].startswith("按用户请求、附件和来源边界选择材料")


def test_model_read_scopes_do_not_expose_internal_trace_or_state() -> None:
    for role in ("orchestrator", "research", "image", "slide"):
        scopes = read_scopes_for_role(role, skill_name=SKILL.name, pages=[1])
        exposed = [scope.path for scope in scopes if scope.path.startswith(("_state", "_trace"))]
        assert exposed == []


def _response(*blocks, stop_reason: str = "end_turn"):
    from runtime.session import NormalizedAssistantResponse
    return NormalizedAssistantResponse(
        content=tuple(vars(block) for block in blocks),
        analysis_summaries=(),
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=8,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            output_tokens=5,
        ),
    )


def _text(value: str):
    return SimpleNamespace(type="text", text=value)


def _tool(tool_id: str, name: str, arguments: dict[str, object]):
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=arguments)


def _is_orchestrator_request(request: ModelRequest) -> bool:
    return any(tool.get("name") == "delegate_submit" for tool in request.tools)


class ScriptedDeckTransport:
    """按公开工具能力路由，模拟 Orchestrator 与 Slide Owner。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._turns = {"orchestrator": 0, "slide": 0}
        self.requests: list[ModelRequest] = []

    def complete(self, _channel, request, *, total_timeout_s):
        with self._lock:
            self.requests.append(copy.deepcopy(request))
            if request.request_kind == "content_review":
                raise AssertionError("raw generation must not invoke postprocess content QC")
            # 隔离视觉 Critic 用全新 system(视觉 Rubric)、request_kind=critic_visual、
            # 消息只含冻结 PNG；返回独立视觉观察。
            # native inspect 关闭页面检查后触发,不占 Slide/Orchestrator 的 turn 计数。
            if request.request_kind == "critic_visual":
                return TransportResponse(_response(_text("READY")), first_byte_s=0.01)
            role = "orchestrator" if _is_orchestrator_request(request) else "slide"
            self._turns[role] += 1
            turn = self._turns[role]
        response = (
            self._orchestrator(turn, request)
            if role == "orchestrator"
            else self._slide(turn)
        )
        return TransportResponse(response, first_byte_s=0.01)

    def _orchestrator(self, turn: int, request: ModelRequest):
        if turn == 1:
            return _response(
                _tool("config-write", "write_file", {"path": "config.json", "content": json.dumps({
                    "working_language": "zh", "presentation_language": "zh", "target_page_count": 1})}),
                _tool("deck-write", "write_file", {"path": "deck.md", "content": DECK_PLAN}),
                _tool("brief-write", "write_file", {"path": "briefs/slide_01.md", "content": PAGE_PLAN}),
                _tool(
                    "validate-plans",
                    "terminal",
                    {
                        "command": "python skills/mural-presenter-v1/scripts/orchestrator.py validate-plans . --expected 1"
                    },
                ),
                _tool(
                    "delegate-1",
                    "delegate_submit",
                    {
                        "tasks": [
                            {
                                "task_id": "page-01",
                                "role": "slide",
                                "pages": [1],
                                "group_id": "page-01",
                                "goal": "完成 P01 并看当前像素。",
                                "repair": False,
                            }
                        ]
                    },
                ),
                stop_reason="tool_use",
            )
        if turn == 2:
            return _response(_text("当前没有其他独立工作，等待页面结果。"))
        if turn == 3:
            return _response(_text("整册已完成并通过最终验收。"))
        return _response(_text("整册已完成并通过最终验收。"))

    def _slide(self, turn: int):
        if turn == 1:
            return _response(
                _tool(
                    "slide-write",
                    "write_file",
                    {"path": "slides/slide_01.html", "content": SLIDE_HTML},
                ),
                _tool(
                    "slide-inspect",
                    "inspect",
                    {
                        "page": [1],
                        "question": "检查页面完整、无裁切并适合投影阅读。",
                        "review_stage": "final",
                    },
                ),
                stop_reason="tool_use",
            )
        return _response(_text("status: ready\npages: [1]\nsummary: 当前像素已检查。"))


class FakeRenderer:
    def __init__(self) -> None:
        self.last_stdout = ""

    def __call__(self, _html: Path, png: Path, canvas: dict):
        png.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (canvas["width_px"], canvas["height_px"]), (244, 241, 234)).save(png)
        self.last_stdout = "status:PASS"
        return {"geometry": canvas, "loaded_resources": [], "failed_resources": []}


def _tool_result_value(request: ModelRequest, tool_use_id: str) -> dict[str, object]:
    for message in reversed(request.messages):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and block.get("tool_use_id") == tool_use_id
            ):
                value = json.loads(str(block.get("content") or "{}"))
                assert isinstance(value, dict)
                return value
    raise AssertionError(f"missing tool result {tool_use_id}")


def _run_spec(root: Path, mode: RunMode, attachment: Path) -> RunSpec:
    return RunSpec.from_inputs(
        raw_user_query="根据附件制作一页中文演示，说明 Agent 与 Harness 的职责边界。",
        output_workspace=root,
        attachments=[attachment],
        run_mode=mode,
        language="zh",
        environment={
            "MODEL_API_FORMAT": "openai",
            "MODEL": "scripted-model",
            "MODEL_BASE_URL": "https://provider.test/v1",
            "MODEL_STREAMING": "0",
            "MODEL_MAX_ATTEMPTS": "2",
        },
    )


def _execute(root: Path, mode: RunMode, attachment: Path, *, owner_max_turns=None,
             transport=None, run_spec=None, enable_visual_critic=False):
    run_spec = run_spec or _run_spec(root, mode, attachment)
    settings = ProductionSettings(
        skill_source=SKILL,
        max_turns=12,
        owner_max_turns=owner_max_turns,
        max_tokens=1024,
        owner_max_tokens=1024,
        thinking=False,
        model_concurrency=1,
        enable_external_visual_critic=enable_visual_critic,
    )
    transport = transport or ScriptedDeckTransport()
    renderer = FakeRenderer()
    runtime = SkillRuntime(
        run_spec,
        os.environ,
        settings=settings,
        transport=transport,
        channels=(ProviderChannel("secret", "ch-scripted"),),
        renderer=renderer,
        validate_environment=False,
    )
    outcome = run_presenter(
        run_spec,
        runtime.backend,
        renderer=renderer,
        vision_analyzer=runtime.vision_analyzer,
        include_pdf=False,
        include_pptx=False,
        include_zip=False,
        title="Vertical Slice",
    )
    return outcome, runtime, transport


@pytest.mark.parametrize("mode", [RunMode.INFERENCE, RunMode.SYNTHESIS])
def test_four_repairs_can_finish_but_real_issues_and_unchecked_pixels_still_block(tmp_path, mode):
    from export.delivery import DeliveryError, DeliveryExporter
    from qc.routing import IssueLedger
    from state.artifacts import ArtifactStore
    from state.workspace import PageLifecycle, WorkspaceState

    class RepairTransport(ScriptedDeckTransport):
        def _slide(self, turn):
            if turn > 5:
                return super()._slide(turn)
            return _response(
                _tool(f"write-{turn}", "write_file", {
                    "path": "slides/slide_01.html",
                    "content": SLIDE_HTML + f"\n<!-- repair version {turn} -->",
                    "repair_reason": "正文靠近页脚，调整底部留白以避免遮挡。",
                }),
                _tool(f"inspect-{turn}", "inspect", {"page": [1]}),
                stop_reason="tool_use",
            )

    root = tmp_path / "workspace"
    attachment = tmp_path / "brief.txt"
    attachment.write_text("附件原始事实。", encoding="utf-8")
    transport = RepairTransport()
    _execute(root, mode, attachment, transport=transport)
    workspace = WorkspaceState(root)
    page = workspace.page(1)
    assert page.lifecycle == PageLifecycle.READY
    assert "repair version 5" in (root / page.html_path).read_text()
    assert transport._turns["slide"] == 6
    budget = json.loads(workspace.path.read_text())["pages"]["1"]["repair_budgets"]["initial"]
    assert len(budget["batches"]) == 4 and not budget["open"]
    issues = IssueLedger(root)
    assert not issues.blocking_issues()
    assert (root / "result.json").exists()
    events = [json.loads(line) for line in (root / "_trace" / "tool-events.jsonl").read_text().splitlines()]
    assert not any(event.get("error_code") == "repair_budget_exhausted" for event in events)

    issues.record_orchestrator_issues([{
        "positions": [1], "page_uids": [page.page_uid], "problem": "原材料中的必须信息仍缺失",
        "suggested_fix": "补回原材料要求保留的事实"}])
    exporter = DeliveryExporter(workspace, ArtifactStore(root, workspace_state=workspace), issues)
    with pytest.raises(DeliveryError, match="unresolved major issues"):
        exporter.export(include_pdf=False, include_pptx=False, include_zip=False)
    Image.new("RGB", (1280, 720), "navy").save(root / page.png_path)
    with pytest.raises(DeliveryError, match="not ready"):
        exporter.export(include_pdf=False, include_pptx=False, include_zip=False)


def test_skill_snapshot_attachment_and_slide_read_contract(tmp_path: Path) -> None:
    attachment = tmp_path / "brief.txt"
    attachment.write_text("附件中的原始事实。", encoding="utf-8")
    run_spec = _run_spec(tmp_path / "workspace", RunMode.INFERENCE, attachment)
    runtime = SkillRuntime(
        run_spec,
        {},
        settings=ProductionSettings(skill_source=SKILL, thinking=False),
        transport=ScriptedDeckTransport(),
        channels=(ProviderChannel("secret", "ch-scripted"),),
        renderer=FakeRenderer(),
        validate_environment=False,
    )

    snapshot = runtime.prepared.snapshot
    assert snapshot.target.is_dir() and not snapshot.target.is_symlink()
    assert snapshot.main_text() == (SKILL / "SKILL.md").read_text(encoding="utf-8") + snapshot.prompt_suffix
    host_path = run_spec.output_workspace / "_state/skill-runtime.json"
    prepared = json.loads(host_path.read_text())
    timings = prepared["preparation_timings_ms"]
    assert set(timings) == {"skill_object", "font_pin", "attachments", "doctor", "provider_setup", "total"}
    assert timings["doctor"] is None
    assert all(isinstance(v, int) and v >= 0 for v in timings.values() if v is not None)
    assert timings["total"] >= max(v for v in timings.values() if v is not None)
    runtime._write_host_state(status="completed")
    assert json.loads(host_path.read_text())["preparation_timings_ms"] == timings
    provenance = json.loads(
        (run_spec.output_workspace / "_trace" / "skill-provenance.json").read_text(
            encoding="utf-8"
        )
    )
    assert provenance["tree_sha256"] == snapshot.tree_sha256
    manifest = runtime.prepared.attachments.to_dict()
    assert manifest["input_stage"] == "direct_text"
    assert not (run_spec.output_workspace / "knowledge.md").exists()
    assert not (run_spec.output_workspace / "assets/catalog.md").exists()
    slide_scopes = read_scopes_for_role("slide", skill_name=SKILL.name, pages=[1])
    assert not any(scope.permits("canvas.json") for scope in slide_scopes)
    assert any(scope.permits("base.css") for scope in slide_scopes)
    assert any(scope.permits("deck.md") for scope in slide_scopes)
    assert any(scope.permits("slides/slide_01.html") for scope in slide_scopes)
    assert not any(scope.permits("briefs/slide_02.md") for scope in slide_scopes)
    from runtime.production import write_scopes_for_role
    writes = write_scopes_for_role("slide", pages=[1])
    assert not any(scope.permits("design-brief.md") for scope in writes)


@pytest.mark.parametrize("documents_exist", [False, True])
@pytest.mark.parametrize("role", ["research", "image", "slide"])
def test_owner_handoffs_only_advertise_role_readable_paths(role, documents_exist, tmp_path):
    if documents_exist:
        (tmp_path / "knowledge.md").write_text("Actual attachment evidence")
        (tmp_path / "assets").mkdir()
        (tmp_path / "assets/catalog.md").write_text("Actual image record")
    runtime = SimpleNamespace(_attachment_manifest=lambda: {"records": [{"path": "inputs/source.pdf"}]})
    dependency = {"task_id": "source", "result": {"summary": "read material.md"}}
    services = SimpleNamespace(
        orchestration=SimpleNamespace(task=lambda _: SimpleNamespace(to_dict=lambda: dependency)),
        artifacts=SimpleNamespace(catalog=lambda: {"images": []}),
        workspace=SimpleNamespace(paths=SimpleNamespace(root=tmp_path, knowledge=tmp_path / "knowledge.md")))
    handoffs = SkillRuntime._owner_handoffs(runtime, services,
        SimpleNamespace(role=role, dependencies=("source",), reference_pages=(),
                        output_path="", handoff_from="", asset_ids=()))
    assert handoffs["dependency_results"] == [dependency]
    assert ("attachment_manifest" in handoffs) == (role in {"research", "image"})
    assert "image_catalog" not in handoffs
    assert ("knowledge_path" in handoffs) == documents_exist
    assert ("image_catalog_path" in handoffs) == (role in {"image", "slide"} and documents_exist)
    scopes = read_scopes_for_role(role, skill_name=SKILL.name, pages=[1], attachment_paths=["inputs/source.pdf"])
    for key in ("knowledge_path", "image_catalog_path"):
        if key in handoffs:
            assert any(scope.permits(handoffs[key]) for scope in scopes)
    if role == "research":
        assert "inputs/source.pdf" in json.dumps(handoffs)


def test_attachment_prompt_view_keeps_read_entrypoints_without_parser_state():
    media = [f"inputs/extracted/att-012345abcdef/images/figure-{index:03d}.png"
             for index in range(80)]
    receipt = {
        "status": "completed",
        "text_path": "inputs/extracted/att-012345abcdef/document.md",
        "content_list_path": "inputs/extracted/att-012345abcdef/content-list.json",
        "media_paths": media,
        "unresolved_items": [],
    }
    manifest = {
        "schema": "private-parser-state",
        "input_stage": "parsed",
        "created_at": "not-for-model",
        "records": [{
            "attachment_id": "att-012345abcdef",
            "name": "source.pdf",
            "kind": "document",
            "path": "inputs/originals/att-012345abcdef/source.pdf",
            "parse_status": "completed",
            "extracted_root": "inputs/extracted/att-012345abcdef",
            "page_image_pattern": "inputs/extracted/att-012345abcdef/pages/page_NNN.png",
            "page_image_paths": [f"page-{index}" for index in range(20)],
            "media_paths": media,
            "sha256": "a" * 64,
            "parse": {
                "current": "parse-key",
                "results": {
                    "parse-key": {
                        "receipt": receipt,
                        "identity": {"endpoint": "private"},
                        "outputs": {path: "b" * 64 for path in media},
                    }
                },
            },
        }],
    }

    visible = _attachment_manifest_prompt_view(manifest)
    encoded = json.dumps(visible, ensure_ascii=False)
    record = visible["records"][0]

    assert record["companion_path"].endswith("document.md")
    assert record["content_list_path"].endswith("content-list.json")
    assert record["media_count"] == 80 and record["page_count"] == 20
    assert "parse" not in record and "media_paths" not in record and "page_image_paths" not in record
    assert "outputs" not in encoded and "identity" not in encoded and "sha256" not in encoded
    assert len(encoded) < 3000


def test_completion_gap_prompt_view_deduplicates_repair_contracts():
    goal = "只复核当前 P1 渲染，不重做其他页面。" * 40
    submit = {
        "task_id": "completion-recheck-01",
        "role": "slide",
        "pages": [1],
        "group_id": "slide-01",
        "goal": goal,
        "repair": True,
    }
    visible = _completion_gap_prompt_view({
        "page_issues": [{
            "page": 1,
            "page_uid": "internal-page-id",
            "html_path": "slides/slide_01.html",
            "png_path": "renders/slide_01.png",
            "lifecycle": "rendered",
            "previously_inspected": True,
            "reasons": ["current pixels not consumed"],
        }],
        "recovery": [{
            "action": "delegate_submit",
            "affected_pages": [1],
            "goal": goal,
            "group_id": "slide-01",
            "owner_id": "internal-owner-id",
            "pages": [1],
            "submit_task": submit,
        }],
    })
    encoded = json.dumps(visible, ensure_ascii=False)

    assert "goal" not in visible["recovery"][0]
    assert "internal-page-id" not in encoded and "internal-owner-id" not in encoded
    assert visible["recovery"][0]["submit_task"]["task_id"] == "completion-recheck-01"


def test_completion_gap_keeps_complete_page_arguments_and_contact_sheet_stage():
    pages = list(range(1, 41))
    visible = _completion_gap_prompt_view({
        "pages": pages,
        "recovery": [{"action": "inspect_current_deck_contact_sheet", "pages": pages,
                      "review_stage": "draft"},
                     {"action": "delegate_submit", "affected_pages": pages,
                      "submit_task": {"role": "slide", "pages": pages,
                                      "reference_pages": pages, "repair": True}}],
    })
    assert visible["pages"] == pages
    assert visible["recovery"][0] == {
        "action": "inspect_current_deck_contact_sheet", "pages": pages, "review_stage": "draft"}
    assert visible["recovery"][1]["affected_pages"] == pages
    assert visible["recovery"][1]["submit_task"]["pages"] == pages
    assert visible["recovery"][1]["submit_task"]["reference_pages"] == pages


def test_continuation_reads_current_goal_issue_answers_and_file_versions(tmp_path):
    from state.workspace import WorkspaceState, file_sha256
    from geometry_fixture import geometry, write_geometry
    write_geometry(tmp_path, geometry(width_px=1600, height_px=900))
    workspace = WorkspaceState(tmp_path)
    brief = tmp_path / "briefs/slide_03.md"
    brief.parent.mkdir(exist_ok=True)
    brief.write_text("新计划：只改数据，保留版式与来源。")
    runtime = object.__new__(SkillRuntime)
    task = SimpleNamespace(owner_id="same-owner", role="slide", task_id="repair-3", goal="P3 不再使用上一轮否定的图解。" * 60,
                           pages=(3,), page_uids=("stable-page-3",), base_deck_version=2, reference_pages=(),
                           output_path="", handoff_from="", asset_ids=())
    runtime._owner_handoffs = lambda *_: pytest.fail("compaction must not reinsert the entire handoff packet")
    (tmp_path / "deck.md").write_text("Shared direction: preserve current typography.")
    (brief.parent / "slide_02.md").write_text("unrelated page content")
    requests = {"r1": {"owner_id": "same-owner", "response": {"answer": "保留已批准配色"}},
                "r2": {"owner_id": "another-owner", "response": {"answer": "other owner private history"}}}
    issues = [SimpleNamespace(pages=(3,), to_dict=lambda: {"issue_id": "i1", "pages": [3], "status": "open",
        "summary": "原图解已否定", "resolution": "不要再改为环形"}),
        SimpleNamespace(pages=(2,), to_dict=lambda: {"issue_id": "not-this-page"})]
    services = SimpleNamespace(workspace=workspace, run_spec=SimpleNamespace(raw_user_query="原始要求", revision_request={"goal": "仅更新数据"}),
        issues=SimpleNamespace(issues=lambda: issues), orchestration=SimpleNamespace(snapshot=lambda: {"requests": requests}))
    first = runtime._continuation_context(services, task=task)
    assert first["goal"] == task.goal and len(task.goal) > 400
    assert first["issues"][0]["resolution"] == "不要再改为环形"
    assert len(first["issues"]) == len(first["owner_requests"]) == 1
    assert "other owner private history" not in str(first)
    brief.write_text("再修订：来源期限改为 2025，其余保持。")
    second = runtime._continuation_context(services, task=task)
    assert first["artifact_hashes"]["briefs/slide_03.md"] != second["artifact_hashes"]["briefs/slide_03.md"]
    assert second["artifact_hashes"]["briefs/slide_03.md"] == file_sha256(brief)
    assert second["current_instructions"]["briefs/slide_03.md"] == brief.read_text()
    assert second["current_instructions"] != first["current_instructions"]
    assert "unrelated page content" not in str(second)
    assert "plan" not in second and "image_catalog" not in second
    assert second["goal"] == first["goal"]


def test_orchestrator_can_read_manifest_extracted_text_without_broad_input_access(tmp_path):
    from state.workspace import WorkspaceState
    from geometry_fixture import geometry, write_geometry
    write_geometry(tmp_path, geometry(width_px=1600, height_px=900))
    workspace = WorkspaceState(tmp_path)
    text_path = "inputs/extracted/att-012345abcdef/text.md"
    target = tmp_path / text_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Page 1\nReliable attachment evidence\n")
    (tmp_path / "_trace/attachment-manifest.json").write_text(json.dumps({
        "attachments": [{"attachment_id": "att-012345abcdef", "original_path": "inputs/originals/att-012345abcdef/source.pdf",
                         "extracted_root": "inputs/extracted/att-012345abcdef", "ordinal": 1}],
        "records": [{"attachment_id": "att-012345abcdef", "kind": "document", "companion_path": text_path,
                     "path": "inputs/originals/att-012345abcdef/source.pdf"}],
    }))
    runtime = object.__new__(SkillRuntime)
    runtime.prepared = SimpleNamespace(snapshot=SimpleNamespace(name=SKILL.name, tree_sha256="fixture", release="fixture"),
        attachments=SimpleNamespace(path=tmp_path / "_trace/attachment-manifest.json"))
    from state.artifacts import ArtifactStore
    run_spec = SimpleNamespace(authoring_profile="general", language="en")
    backend = ProductionToolServices(
        run_spec, runtime.prepared.snapshot, ProductionSettings(skill_source=SKILL),
        {}, workspace, ArtifactStore(tmp_path, workspace_state=workspace), None, None,
    )
    runtime._tool_services = backend
    services = SimpleNamespace(workspace=workspace, run_spec=run_spec, owners=backend.owners)
    context = runtime._orchestrator_context(services)
    assert "Reliable attachment evidence" in backend.read_file(context, {"path": text_path})["content"]
    assert context.may_read("inputs/originals/att-012345abcdef/source.pdf")
    assert not context.may_read("inputs/originals/att-000000000000/private.pdf")
    assert not context.may_read("inputs/extracted/other/text.md")
    slide = ToolContext("slide", "slide", read_scopes=read_scopes_for_role("slide", skill_name=SKILL.name, pages=[1]))
    with pytest.raises(ToolContractError) as error:
        backend.read_file(slide, {"path": text_path})
    assert error.value.code == "read_scope_denied"


def test_style_reference_attachment_paths_are_granted_to_material_owners(tmp_path):
    """Style-only images are still user materials for Image/Research owners."""
    attachment_path = "inputs/originals/att-012345abcdef/poster.png"
    runtime = object.__new__(SkillRuntime)
    runtime._attachment_manifest = lambda: {
        "attachment_paths": [],
        "style_reference_paths": [attachment_path],
        "visual_asset_paths": [attachment_path],
        "records": [{
            "attachment_id": "att-012345abcdef",
            "kind": "image",
            "intent": "style_reference",
            "path": attachment_path,
        }],
    }
    staged = runtime._staged_attachment_paths()
    assert staged == (attachment_path,)
    for role in ("research", "image"):
        scopes = read_scopes_for_role(role, skill_name=SKILL.name, pages=[1], attachment_paths=staged)
        context = ToolContext(f"{role}-owner", role, read_scopes=scopes)
        assert context.may_read(attachment_path)
        assert context.may_read("inputs/extracted/att-012345abcdef/pages/page_001.png")


def test_image_recovery_handoff_lists_existing_uncommitted_images(tmp_path):
    (tmp_path / "assets").mkdir()
    Image.new("RGB", (16, 16), "red").save(tmp_path / "assets/generated.png")
    services = SimpleNamespace(workspace=SimpleNamespace(paths=SimpleNamespace(root=tmp_path, knowledge=tmp_path / "knowledge.md")),
        artifacts=SimpleNamespace(catalog=lambda: {"images": []}))
    runtime = SimpleNamespace(_attachment_manifest=lambda: {})
    handoff = SkillRuntime._owner_handoffs(runtime, services, SimpleNamespace(role="image", dependencies=(), reference_pages=(),
        output_path="", handoff_from="", asset_ids=()))
    existing = handoff["existing_image_files"]
    assert len(existing) == 1 and existing[0]["path"] == "assets/generated.png"
    assert "sha256" not in existing[0] and existing[0]["bytes"] > 0
    assert "image_catalog" not in handoff


def test_real_critic_feedback_event_reaches_live_trace_and_deduplicates(tmp_path):
    from runtime.live_trace import LiveEventJournal
    runtime = object.__new__(SkillRuntime)
    journal = LiveEventJournal(tmp_path / "_trace/live-events.jsonl")
    services = SimpleNamespace(live_trace=journal)
    event = dict(event_type="critic_completed", owner_id="owner-slide", tool_use_id="inspect-1",
                 inspection_id="inspection-1", png_sha256="pixels", completed=True, body="标题清晰。")
    runtime._record_critic_event(services, event)
    runtime._record_critic_event(services, event)
    rows = [json.loads(line) for line in journal.path.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["role"] == "critic"
    assert rows[0]["event_type"] == "critic_completed"
    assert rows[0]["payload"]["owner_id"] == "owner-slide"
    assert rows[0]["payload"]["body"] == "标题清晰。"
    assert not (tmp_path / "_trace/agent-events.jsonl").exists()
    assert journal.sequence == 1


def test_skill_runtime_closes_provider_attempt_left_open_by_process_exit(
    tmp_path: Path,
) -> None:
    attachment = tmp_path / "brief.txt"
    attachment.write_text("附件中的原始事实。", encoding="utf-8")
    workspace = tmp_path / "workspace"
    from state.workspace import WorkspaceState
    WorkspaceState(workspace)
    from runtime.live_trace import LiveEventJournal, iter_replay_events
    journal = LiveEventJournal(workspace / "_trace/live-events.jsonl")
    journal.record_runtime_event(
            {
                "event_type": "provider_attempt_started",
                "attempt_id": "attempt-interrupted",
                "agent_id": "owner-research",
                "role": "research",
                "provider_id": "provider-test",
                "channel_id": "ch-a",
                "attempt": 1,
                "request_kind": "hermes_main",
                "model": "scripted-model",
            }
    )
    run_spec = _run_spec(workspace, RunMode.INFERENCE, attachment)
    runtime = SkillRuntime(
        run_spec,
        {},
        settings=ProductionSettings(skill_source=SKILL, thinking=False),
        transport=ScriptedDeckTransport(),
        channels=(ProviderChannel("secret", "ch-scripted"),),
        renderer=FakeRenderer(),
        validate_environment=False,
    )

    recovered = journal.close_interrupted_events()
    replay = replay_provider_events(iter_replay_events(journal.path, "provider"))
    assert replay.attempts == replay.failures == 1
    assert recovered[0]["payload"]["attempt_id"] == "attempt-interrupted"
    assert journal.close_interrupted_events() == ()
    assert not (workspace / "_trace/provider-events.jsonl").exists()
    terminal = list(iter_replay_events(journal.path, "provider"))[-1]
    assert terminal["error_kind"] == "process_interrupted"
    assert terminal["closed_during_workspace_resume"] is True


def test_orchestrator_input_exposes_existing_tasks_for_same_workspace_resume(
    tmp_path: Path,
) -> None:
    attachment = tmp_path / "brief.txt"
    attachment.write_text("附件中的原始事实。", encoding="utf-8")
    run_spec = _run_spec(tmp_path / "workspace", RunMode.INFERENCE, attachment)
    runtime = SkillRuntime(
        run_spec,
        {},
        settings=ProductionSettings(skill_source=SKILL, thinking=False),
        transport=ScriptedDeckTransport(),
        channels=(ProviderChannel("secret", "ch-scripted"),),
        renderer=FakeRenderer(),
        validate_environment=False,
    )
    from owner.registry import OwnerRegistry
    from state.workspace import WorkspaceState
    workspace = WorkspaceState(run_spec.output_workspace)
    workspace.register_pages([1])
    owners = OwnerRegistry(workspace.paths.root, deck_id=workspace.deck_id)
    owner = owners.accept(role="slide", pages=[1], group_id="page-01")
    owners.transition(owner.owner_id, attempt=owner.attempt, status="running")
    snapshot = {
        "tasks": {
            "task-stable": {
                "task_id": "task-stable",
                "owner_id": owner.owner_id,
                "attempt": owner.attempt,
                "role": "slide",
                "pages": [1],
                "group_id": "page-01",
                "status": "running",
                "repair": False,
            }
        },
        "cursors": {"orchestrator": 3},
    }
    coverage = {"planned_page_count": 1, "self_authored_pages": [],
                "delegated_pages": [1], "unassigned_pages": []}
    services = SimpleNamespace(
        orchestration=SimpleNamespace(snapshot=lambda: copy.deepcopy(snapshot),
                                      page_coverage=lambda: copy.deepcopy(coverage)),
        workspace=workspace,
        owners=owners,
    )

    message = runtime._orchestrator_input(services)
    value = json.loads(str(message["content"]))
    assert value["resume_state"]["cursor"] == 3
    assert "waves" not in value["resume_state"]
    assert value["resume_state"]["tasks"][0]["task_id"] == "task-stable"
    assert value["resume_state"]["tasks"][0]["owner_id"] == owner.owner_id
    assert value["resume_state"]["page_coverage"] == coverage


def test_tool_contract_resume_input_keeps_only_the_actionable_failure_hint():
    runtime = SkillRuntime.__new__(SkillRuntime)
    runtime._recovered_provider_attempt_ids = ()
    runtime._previous_tool_failure = {"tool": "patch", "error_code": "write_scope_denied"}
    services = SimpleNamespace(
        owners=SimpleNamespace(get_group=lambda _group: SimpleNamespace(pages=(1,))),
        workspace=SimpleNamespace(pages=lambda: []),
        orchestration=SimpleNamespace(snapshot=lambda: {"tasks": {}}, page_coverage=lambda: {}),
    )

    result = runtime._resume_state(services)
    hint = result["previous_tool_failure"]
    assert set(hint) == {"tool", "error_code", "instruction"}
    assert hint["tool"] == "patch" and hint["error_code"] == "write_scope_denied"
    assert "original group_id" in hint["instruction"] and "Do not resubmit succeeded tasks" in hint["instruction"]
    assert "_state/" not in json.dumps(hint) and "_trace/" not in json.dumps(hint)
    runtime._previous_tool_failure = None
    assert "previous_tool_failure" not in runtime._resume_state(services)


def test_synthesis_resume_links_last_durable_segment_without_replaying_old_messages(tmp_path):
    from export.training import TrainingSegmentInput
    from runtime.live_trace import LiveEventJournal
    from runtime.recovery import SegmentCheckpoint, tool_boundary

    runtime = SkillRuntime.__new__(SkillRuntime)
    old = SegmentCheckpoint.initial((
        {"role": "user", "content": "old task"},
        {"role": "assistant", "content": "old action, not a new training target"},
    ), segment_id="z-old")
    from dataclasses import replace
    latest = replace(SegmentCheckpoint.initial(old.messages, segment_id="a-latest"),
                     excluded_channel_ids=("rejected-account",), invalid_signature_recoveries=1)
    slide = SegmentCheckpoint.initial(segment_id="slide-segment")
    captured = [TrainingSegmentInput("orchestrator", "orchestrator", latest),
                TrainingSegmentInput("orchestrator", "orchestrator", old),
                TrainingSegmentInput("slide-owner", "slide", slide)]
    services = SimpleNamespace(run_spec=SimpleNamespace(run_mode=RunMode.SYNTHESIS),
        workspace=SimpleNamespace(paths=SimpleNamespace(trace=tmp_path)),
        training_snapshot=lambda: tuple(captured))
    live = LiveEventJournal(tmp_path / "live-events.jsonl")
    for owner, segment in (("orchestrator", old.segment_id), ("orchestrator", latest.segment_id),
                           ("slide-owner", slide.segment_id), ("orchestrator", "not-checkpointed")):
        live.append(agent_id=owner, role="slide" if owner == "slide-owner" else "orchestrator",
                    segment_id=segment, event_type="agent_context", payload={})
    live.append(agent_id="orchestrator", role="orchestrator", segment_id=old.segment_id,
                event_type="provider_failed", payload={"closed_during_workspace_resume": True})
    current = {"role": "user", "content": '{"resume_state":{"tasks":[{"task_id":"existing"}]}}'}
    previous_value = latest.to_dict()
    for _ in range(2):
        resumed = runtime._orchestrator_checkpoint(services, current)
        assert resumed.predecessor_segment_id == latest.segment_id
        assert resumed.segment_id != latest.segment_id
        assert resumed.reason == "workspace_resume"
        assert resumed.messages == (current,)
        assert tool_boundary(resumed.messages).is_closed
        assert not resumed.provider_affinity_id
        assert resumed.excluded_channel_ids == ("rejected-account",)
        assert resumed.invalid_signature_recoveries == 1
        assert latest.to_dict() == previous_value
        captured.append(TrainingSegmentInput("orchestrator", "orchestrator", resumed))
        live.append(agent_id="orchestrator", role="orchestrator", segment_id=resumed.segment_id,
                    event_type="agent_context", payload={})
        latest, previous_value = resumed, resumed.to_dict()


def test_synthesis_resume_requires_trace_for_an_existing_segment_but_not_a_fresh_run(tmp_path):
    from export.training import TrainingSegmentInput
    from runtime.recovery import SegmentCheckpoint
    from runtime.skill_runtime import SkillRuntimeError

    captured = []
    services = SimpleNamespace(run_spec=SimpleNamespace(run_mode=RunMode.SYNTHESIS),
        workspace=SimpleNamespace(paths=SimpleNamespace(trace=tmp_path)),
        training_snapshot=lambda: tuple(captured))
    runtime = SkillRuntime.__new__(SkillRuntime)
    current = {"role": "user", "content": "current task"}
    assert runtime._orchestrator_checkpoint(services, current) is None
    captured.append(TrainingSegmentInput("orchestrator", "orchestrator", SegmentCheckpoint.initial()))
    with pytest.raises(SkillRuntimeError, match="orchestrator_checkpoint_trace_missing"):
        runtime._orchestrator_checkpoint(services, current)


def test_synthesis_process_resume_preserves_predecessor_and_actual_starting_input(tmp_path):
    from runtime.recovery import ResourceSlotTimeout
    from state.workspace import file_sha256

    class PausedTransport(ScriptedDeckTransport):
        def _orchestrator(self, turn, request):
            if turn == 3:
                event = json.loads(request.messages[-1]["content"])
                assert event["harness_event"] == "orchestration_update" and event["all_terminal"]
                raise ResourceSlotTimeout("test process pause before final deck verification")
            return super()._orchestrator(turn, request)

    class ResumedTransport(ScriptedDeckTransport):
        def _orchestrator(self, turn, request):
            assert turn == 1
            return _response(_text("沿用已完成页面，等待 Harness 校验当前交付。"))

    root, attachment = tmp_path / "workspace", tmp_path / "brief.txt"
    attachment.write_text("附件原始事实。", encoding="utf-8")
    with pytest.raises(ResourceSlotTimeout):
        _execute(root, RunMode.SYNTHESIS, attachment, transport=PausedTransport())
    segment_dir = root / "_trace/raw/segments"
    old = {p.name: (file_sha256(p), json.loads(p.read_text())) for p in segment_dir.glob("segment-*.json")}
    prior = [value for _, value in old.values() if value["owner_id"] == "orchestrator"]
    assert len(prior) == 1
    owners_before = json.loads((root / "_state/owners.json").read_text())
    outcome, _, transport = _execute(root, RunMode.SYNTHESIS, attachment, transport=ResumedTransport())
    assert outcome.delivery.status == "complete"
    assert json.loads((root / "_state/owners.json").read_text()) == owners_before
    assert all(task["status"] == "succeeded" for task in
               json.loads((root / "_state/orchestration.json").read_text())["tasks"].values())
    assert transport._turns["slide"] == 0
    assert not (root / "_trace/training").exists()
    from export.training import export_workspace
    export_workspace(root)
    rows = [json.loads(line) for line in (root / "_trace/training/training-segments.jsonl").open()]
    resumed_row, = [row for row in rows if row["owner_id"] == "orchestrator"
                    and row["segment_id"] != prior[0]["segment_id"]]
    assert resumed_row["predecessor_segment_id"] == prior[0]["segment_id"]
    resumed = json.loads((root / resumed_row["checkpoint_path"]).read_text())
    assert resumed["reason"] == "workspace_resume"
    actual = next(r for r in transport.requests if "Harness same-workspace resume" in r.system)
    assert len(actual.messages) == 1
    assert "resume_state" in json.loads(actual.messages[0]["content"])
    assert resumed["agent_context"][0]["messages"] == list(actual.messages)
    assert resumed["messages"][:1] == list(actual.messages)
    assert file_sha256(root / next(row["checkpoint_path"] for row in rows
        if row["segment_id"] == prior[0]["segment_id"])) == next(
            digest for digest, value in old.values() if value["owner_id"] == "orchestrator")


def test_batch_429_recovery_uses_real_workspace_checkpoints_without_remaking_pages(tmp_path):
    import httpx
    from runtime.agent import AgentProviderError
    from runtime.batch import BatchArgs, BatchPaths, run_batch
    from runtime.recovery import tool_boundary, validate_segment_lineage
    from export.training import TrainingExporter
    from state.artifacts import ArtifactStore
    from qc.routing import IssueLedger
    from state.workspace import WorkspaceState, file_sha256

    class FailedTransport(ScriptedDeckTransport):
        def _orchestrator(self, turn, request):
            if turn >= 3:
                event = json.loads(request.messages[-1]["content"])
                assert event["harness_event"] == "orchestration_update" and event["all_terminal"]
                wire = httpx.Request("POST", "https://provider.test/v1/messages")
                raise httpx.HTTPStatusError("system memory limit, please slow down", request=wire,
                    response=httpx.Response(429, request=wire, headers={"Retry-After": "0"}))
            return super()._orchestrator(turn, request)

    class ResumedTransport(ScriptedDeckTransport):
        def _orchestrator(self, turn, request):
            assert turn == 1
            return _response(_text("沿用已完成页面，等待 Harness 校验当前交付。"))

    attachment, queries = tmp_path / "brief.txt", tmp_path / "queries.jsonl"
    attachment.write_text("附件原始事实。")
    queries.write_text(json.dumps({"query_id": "unit", "query": "根据附件制作一页中文演示，说明 Agent 与 Harness 的职责边界。",
        "language": "zh", "attachments": [{"path": str(attachment)}]}, ensure_ascii=False) + "\n")
    attempts, before = [], {}

    def execute(spec, environment):
        transport = FailedTransport() if not attempts else ResumedTransport()
        attempts.append(transport)
        if before:
            assert all(file_sha256(spec.output_workspace / path) == digest for path, digest in before.items())
        try:
            return _execute(spec.output_workspace, RunMode.SYNTHESIS, attachment,
                            transport=transport, run_spec=spec)[0]
        except AgentProviderError as exc:
            assert exc.failure.status_code == 429 and exc.retry_after_s == 0
            for path in ("_state/owners.json", "slides/slide_01.html", "renders/slide_01.png"):
                before[path] = file_sha256(spec.output_workspace / path)
            raise

    paths = BatchPaths(runs_dir=tmp_path / "runs", logs_dir=tmp_path / "logs")
    args = BatchArgs(queries=queries, batch="resume", workers=1, max_attempts=2, run_mode=RunMode.SYNTHESIS)
    environment = {"MODEL_API_FORMAT": "openai", "MODEL": "scripted-model",
        "MODEL_BASE_URL": "https://provider.test/v1", "MODEL_STREAMING": "0", "MODEL_MAX_ATTEMPTS": "1"}
    assert run_batch(args, paths=paths, environment=environment, run_deck=execute) == 0
    assert len(attempts) == 2 and attempts[-1]._turns["slide"] == 0
    root = paths.runs_dir / "resume/resume_unit"
    assert all(file_sha256(root / path) == digest for path, digest in before.items())
    assert not (root / "_trace/training").exists()
    segments = TrainingExporter(WorkspaceState(root), ArtifactStore(root), IssueLedger(root),
                                run_mode=RunMode.SYNTHESIS).load_segments()
    orch = [segment.checkpoint for segment in segments if segment.owner_id == "orchestrator"]
    assert len(orch) == 2 and len([segment for segment in orch if segment.predecessor_segment_id]) == 1
    validate_segment_lineage(orch)
    assert all(tool_boundary(segment.messages).is_closed for segment in orch)
    assert all("system memory limit" not in json.dumps(segment.messages) for segment in orch)
    rows = [json.loads(line) for line in (paths.logs_dir / "resume.manifest.jsonl").read_text().splitlines()]
    assert [row["execution"] for row in rows if row["status"] != "running"] == ["fresh", "resume"]


@pytest.mark.parametrize("pending", [False, True])
def test_idle_review_guidance_preserves_events_without_creating_review_or_completion(pending):
    event = {"all_terminal": not pending,
        "events": [{"type": "owner_request" if pending else "task_completed", "task_id": "slide-1"}],
        "pending_requests": [{"request_id": "help-1"}] if pending else []}
    before = copy.deepcopy(event)
    completed = []
    services = SimpleNamespace(
        orchestration=SimpleNamespace(wait=lambda **_: event),
        complete_deck=lambda: completed.append(True))

    message = SkillRuntime._orchestrator_idle(SimpleNamespace(), services)
    payload = json.loads(message["content"])
    assert message["role"] == "user"
    assert payload.pop("harness_event") == "orchestration_update"
    assert "inspect" in payload.pop("notice")
    assert payload == before == event
    assert not completed

    event.update(all_terminal=True, events=[], pending_requests=[])
    assert SkillRuntime._orchestrator_idle(SimpleNamespace(), services) is None
    assert completed == [True]


def test_idle_preserves_unresolved_failure_instead_of_completing():
    event = {"all_terminal": True, "events": [], "pending_requests": [],
             "unresolved_failures": [{"task_id": "failed", "error": "missing input"}]}
    services = SimpleNamespace(orchestration=SimpleNamespace(wait=lambda **_: event),
        complete_deck=lambda: pytest.fail("an unresolved failed Owner still needs a decision"))
    message = SkillRuntime._orchestrator_idle(SimpleNamespace(), services)
    assert json.loads(message["content"])["unresolved_failures"] == event["unresolved_failures"]


def test_skill_backed_inference_and_synthesis_share_generation_path(
    tmp_path: Path,
) -> None:
    class NaturalIdleTransport(ScriptedDeckTransport):
        def _orchestrator(self, turn, request):
            if turn == 3:
                event = json.loads(request.messages[-1]["content"])
                assert event["harness_event"] == "orchestration_update" and event["all_terminal"]
            return super()._orchestrator(turn, request)
    attachment = tmp_path / "brief.txt"
    attachment.write_text("附件中的原始事实。", encoding="utf-8")
    inference, inference_runtime, inference_transport = _execute(
        tmp_path / "inference", RunMode.INFERENCE, attachment
    )
    synthesis, synthesis_runtime, synthesis_transport = _execute(
        tmp_path / "synthesis", RunMode.SYNTHESIS, attachment, transport=NaturalIdleTransport()
    )

    assert inference.delivery.status == synthesis.delivery.status == "complete"
    assert inference.delivery.pages[0]["html_sha256"] == synthesis.delivery.pages[0][
        "html_sha256"
    ]
    assert inference.delivery.pages[0]["png_sha256"] == synthesis.delivery.pages[0][
        "png_sha256"
    ]
    assert inference.training_manifest is None
    assert synthesis.training_manifest is None
    assert not (synthesis.workspace / "_trace/training").exists()
    from export.training import export_workspace
    count = len(synthesis_transport.requests)
    manifest = export_workspace(synthesis.workspace)
    assert len(synthesis_transport.requests) == count
    assert manifest["training_admission"] == "accepted"
    assert manifest["segment_count"] == 2
    assert manifest["trainable_segment_count"] == 2
    assert manifest["data_stage"] == "raw"
    assert manifest["content_qc"] == {"stage": "postprocess", "status": "pending"}
    assert not any(request.request_kind == "critic_visual" for request in synthesis_transport.requests)

    for runtime, transport in (
        (inference_runtime, inference_transport),
        (synthesis_runtime, synthesis_transport),
    ):
        assert all(request.request_kind != "content_review" for request in transport.requests)
        root = runtime.prepared.run_spec.output_workspace
        assert "content_qc" not in (inference if runtime is inference_runtime else synthesis).backend_result
        assert not (root / "_trace/content-qc.json").exists()
        assert "content_qc_" not in (root / "_trace/live-events.jsonl").read_text()
        orchestrator = next(
            request
            for request in transport.requests
            if _is_orchestrator_request(request)
        )
        slide = next(
            request
            for request in transport.requests
            if not _is_orchestrator_request(request)
            and any(tool.get("name") == "inspect" for tool in request.tools)
        )
        assert orchestrator.system.startswith(runtime.prepared.snapshot.main_text())
        assert slide.system.startswith(runtime.prepared.snapshot.role_text("slide"))
        assert "Runtime language policy" in orchestrator.system
        assert "Runtime language policy" in slide.system
        assert all(
            {tool["name"] for tool in request.tools}.isdisjoint(
                {"render", "artifact_commit", "delegate_wait"}
            )
            for request in transport.requests
        )
        assert "mural-presenter-v1.0" not in repr(transport.requests)
        initial = json.loads(str(orchestrator.messages[0]["content"]))
        assert "run_mode" not in initial
        assert initial["attachment_manifest"]["input_stage"] == "direct_text"
        owner_input = json.loads(str(slide.messages[0]["content"]))
        assert "deck.md" in owner_input["allowed_read_paths"]
        plan = owner_input["owner_packet"]["handoffs"]["plan"]
        assert plan["source"] == "deck.md"
        assert [page["number"] for page in plan["pages"]] == [1]
        assert plan["pages"][0]["source"] == "briefs/slide_01.md"
        assert plan["pages"][0]["text"] == PAGE_PLAN.strip()
        assert "职责边界" not in plan["shared"] or "Visual Direction" in plan["shared"]
        assert "briefs/**" not in owner_input["allowed_read_paths"]
        assert "slides/slide_01.html" in owner_input["allowed_write_paths"]

    owner_contexts = list(
        (synthesis.workspace / "_state" / "owner-context").glob("*.json")
    )
    from owner.context import OwnerContextStore
    store = OwnerContextStore(synthesis.workspace)
    assert len(owner_contexts) == 2
    owner_context = next(path for path in owner_contexts if path != store.path_for("orchestrator"))
    context = json.loads(owner_context.read_text(encoding="utf-8"))
    assert context["packet"]["goal"] == "完成 P01 并看当前像素。"
    assert context["packet"]["pages"] == [1]
    from runtime.vision import image_blocks
    assert "omitted_base64" not in json.dumps(context["segment"])
    assert "messages" not in context
    checkpoint_images = [image for message in store.load(context["owner"]["owner_id"]).segment.messages
                         for image in image_blocks(message.get("content"))]
    assert checkpoint_images and all(image["source"]["type"] == "base64" for image in checkpoint_images)
    assert owner_context.stat().st_mode & 0o777 == 0o600

    training_rows = [
        json.loads(line)
        for line in (
            synthesis.workspace / "_trace" / "training" / "training-segments.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    assert {row["role"] for row in training_rows} == {"orchestrator", "slide"}
    assert sum(row["role"] == "critic" for row in training_rows) == 0
    assert all(row["sample_kind"] == "agent_segment" and row["trainable"]
               and row["training_admission"] == "accepted" for row in training_rows)
    slide_row = next(row for row in training_rows if row["role"] == "slide")
    checkpoint = json.loads(
        (synthesis.workspace / slide_row["checkpoint_path"]).read_text(
            encoding="utf-8"
        )
    )
    assert checkpoint["image_blobs"]
    assert '"type": "base64"' not in json.dumps(checkpoint)
    for blob in checkpoint["image_blobs"]:
        assert (synthesis.workspace / blob["path"]).is_file()
    tool_events_text = (
        synthesis.workspace / "_trace" / "tool-events.jsonl"
    ).read_text(encoding="utf-8")
    assert '"tool_name":"inspect"' in tool_events_text
    assert '"tool_name":"verification_submit"' not in tool_events_text
    assert "orchestrator.py scaffold" not in tool_events_text
    assert "orchestrator.py finalize" not in tool_events_text
    assert '"tool_name":"render"' not in tool_events_text
    assert '"tool_name":"delegate_wait"' not in tool_events_text
    tool_events = [json.loads(line) for line in tool_events_text.splitlines()]
    results = {
        event["tool_use_id"]: event
        for event in tool_events
        if event["event_type"] == "tool_result"
    }
    required_ids = {
        "deck-write",
        "brief-write",
        "validate-plans",
        "slide-inspect",
    }
    assert required_ids <= results.keys()
    assert all(results[tool_use_id]["ok"] is True for tool_use_id in required_ids)
    # 自然结束后的 DeliveryExporter 是后台收尾边界；模型不需要先制造一条
    # finalize/render 工具轨迹，仍会得到播放器、联系表与派生讲稿。
    assert (synthesis.workspace / "present.html").is_file()
    assert (synthesis.workspace / "renders/contact-sheet.png").is_file()
    assert (synthesis.workspace / "speech.md").is_file()
    assert synthesis.delivery.outputs["present_html"]["path"] == "present.html"
    live_events = [
        json.loads(line)
        for line in (
            synthesis.workspace / "_trace" / "live-events.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    assert [event["sequence"] for event in live_events] == list(
        range(1, len(live_events) + 1)
    )
    completion = [event for event in live_events if event["event_type"] == "deck_completion_checked"]
    assert len(completion) == 1
    assert completion[0]["payload"]["actor"] == "harness"
    assert completion[0]["payload"]["status"] == "ready"
    assert completion[0]["payload"]["automatic_finalization"] == "complete"
    # 默认关闭外部 Critic 时，inspect 仍保留 Owner 的真实像素检查记录。
    assert {event["role"] for event in live_events} == {
        "harness",
        "orchestrator",
        "slide",
    }
    preparation = [event for event in live_events if event["event_type"] == "slide_preparation_completed"]
    assert len(preparation) == 1 and preparation[0]["role"] == "harness"
    live_tool_events = [
        event for event in live_events
        if event["event_type"] in {"tool_use", "tool_result"}
    ]
    live_pairs = {}
    for event in live_tool_events:
        live_pairs.setdefault(event["payload"]["tool_use_id"], []).append(
            event["event_type"]
        )
    assert all(pair == ["tool_use", "tool_result"] for pair in live_pairs.values())
    agent_events_text = (
        synthesis.workspace / "_trace" / "agent-events.jsonl"
    ).read_text(encoding="utf-8")
    assert '"event_type":"assistant_message"' in agent_events_text
    workspace_state = json.loads(
        (synthesis.workspace / "_state" / "workspace.json").read_text(
            encoding="utf-8"
        )
    )
    transitions = [
        event["type"]
        for event in workspace_state["events"]
        if event.get("page") == 1
    ]
    assert transitions[-3:] == ["page_rendered", "page_inspected", "page_ready"]
    assert transitions.count("page_rendered") == 1
    authoring = next(event for event in workspace_state["events"] if event["type"] == "page_inspected")
    assert workspace_state["pages"]["1"]["last_inspection"]["inspection_id"] == authoring["inspection_id"]


def test_orchestrator_read_scope_allows_knowledge_and_own_public_script() -> None:
    """公开知识及本角色入口可读，执行权限与内部脚本边界不变。"""
    context = ToolContext(
        owner_id="orchestrator",
        role="orchestrator",
        read_scopes=read_scopes_for_role("orchestrator", skill_name=SKILL.name),
    )
    # 明确允许：SKILL 主文件与任意 reference。
    assert context.may_read(f"skills/{SKILL_NAME}/SKILL.md")
    assert context.may_read(f"skills/{SKILL_NAME}/references/runtime/html-contract.md")
    assert context.may_read(f"skills/{SKILL_NAME}/references/style-index.md")
    assert context.may_read(
        f"skills/{SKILL_NAME}/scripts/orchestrator.py"
    )
    assert not context.may_read(f"skills/{SKILL_NAME}/scripts/slide.py")
    assert not context.may_read(
        f"skills/{SKILL_NAME}/scripts/_internal/deck_core.py"
    )
    assert context.may_read(f"skills/{SKILL_NAME}/roles/slide.md")
    assert not context.may_read(f"skills/{SKILL_NAME}/agents/orchestrator.md")


def test_orchestrator_terminal_command_still_parses_after_allowlist() -> None:
    """读白名单收紧不影响受控 scripts/orchestrator.py 的 terminal 执行路径。"""
    services = ProductionToolServices.__new__(ProductionToolServices)
    # _validated_command 只在角色/脚本/动作校验通过后才比对根目录。
    services.workspace = SimpleNamespace(paths=SimpleNamespace(root=Path.cwd().resolve()))
    # layout-v2 把逻辑 skills/... 别名解析到固定 object，argv[1] 会被重写为物理路径。
    object_root = Path("/frozen/skill-object")

    def _resolve(logical: str) -> Path:
        prefix = f"skills/{SKILL_NAME}/"
        assert logical.startswith(prefix)
        return object_root / logical.removeprefix(prefix)

    services.snapshot = SimpleNamespace(name=SKILL.name, target=SKILL, tree_sha256="test-cli", resolve_skill_path=_resolve)
    argv, script_role, action, request = services._validated_command(
        "orchestrator",
        f"python skills/{SKILL_NAME}/scripts/orchestrator.py validate-plans . "
        "--expected 1",
    )
    assert script_role == "orchestrator"
    assert action == "validate-plans"
    assert argv[1] == str(object_root / "scripts/orchestrator.py")
    assert argv[3] == "."
    # scaffold 同样是被允许的确定性动作。
    _, _, scaffold_action, _ = services._validated_command(
        "orchestrator",
        f"python skills/{SKILL_NAME}/scripts/orchestrator.py scaffold . --expected 1",
    )
    assert scaffold_action == "scaffold"
    for suffix in ('--help', 'move-page --help', 'move-page . --help'):
        argv, role, action, request = services._validated_command('orchestrator',
            f'python skills/{SKILL_NAME}/scripts/orchestrator.py {suffix}')
        assert role == 'orchestrator' and action == 'help'
        assert argv[-1] == '--help' and argv[1] == str(object_root/'scripts/orchestrator.py')
    for suffix in ('revision .', 'move-page . --uid hidden', 'move-page /outside --uid p --after q --revision-id r --operation-id o'):
        with pytest.raises(ToolContractError):
            services._validated_command('orchestrator',
                f'python skills/{SKILL_NAME}/scripts/orchestrator.py {suffix}')


def test_resume_facts_reach_runtime_system_not_only_message_envelope(tmp_path, monkeypatch):
    attachment = tmp_path / 'brief.txt'
    attachment.write_text('test source')
    stale_handoff = 'Permanent waiver: no more inspections; every page exhausted its quota.'
    resume_state = {'cursor': 0, 'tasks': [{'task_id': 'historical-repair', 'status': 'cancelled',
                    'goal': stale_handoff, 'result': stale_handoff}]}
    monkeypatch.setattr(SkillRuntime, '_resume_state', lambda *_: resume_state)
    _, _, transport = _execute(tmp_path/'workspace', RunMode.INFERENCE, attachment)
    orchestrator = next(r for r in transport.requests if _is_orchestrator_request(r))
    assert 'Harness same-workspace resume' in orchestrator.system
    assert 'durable task execution status, ownership and artifact references' in orchestrator.system
    assert 'historical model context' in orchestrator.system
    assert 'not additional user requirements' in orchestrator.system
    assert 'cannot waive current completion checks' in orchestrator.system
    assert 'forbid required inspections or introduce inspection quotas' in orchestrator.system
    assert 'preserve genuine unresolved issues' in orchestrator.system
    assert 'saved results as runtime truth' not in orchestrator.system
    assert json.loads(orchestrator.messages[0]['content'])['resume_state'] == resume_state


@pytest.mark.parametrize("mode", [RunMode.INFERENCE, RunMode.SYNTHESIS])
def test_owner_turn_budget_reaches_agent_runtime(tmp_path, monkeypatch, mode):
    from runtime.agent import AgentRuntime
    original = AgentRuntime.run
    observed = {}
    monkeypatch.setenv("MODEL_CHANNEL_BALANCE", "primary-first")

    def record(self, **kwargs):
        assert self.balance == "primary-first"
        assert self.settings.context_window == 262144
        assert self.context_window == (131072 if mode == RunMode.SYNTHESIS else 262144)
        observed[kwargs['role']] = kwargs['max_turns']
        return original(self, **kwargs)

    monkeypatch.setattr(AgentRuntime, 'run', record)
    attachment = tmp_path / 'brief.txt'
    attachment.write_text('test source')
    _execute(tmp_path / 'workspace', mode, attachment, owner_max_turns=9)
    assert observed == {'orchestrator': 12, 'slide': 9}
    state = json.loads((tmp_path / 'workspace/_state/skill-runtime.json').read_text())
    assert state['training_sample_max_tokens'] == (131072 if mode == RunMode.SYNTHESIS else None)


@pytest.mark.parametrize('role, expected', [('slide', 'inspect(page='), ('image', 'inspect(path=')])
def test_binary_read_error_names_available_role_tool(tmp_path, role, expected):
    from runtime.tools import WriteScope
    services = ProductionToolServices.__new__(ProductionToolServices)
    path = tmp_path / 'image.png'
    path.write_bytes(b'\x89PNG\r\n\x1a\n')
    services.workspace = SimpleNamespace(paths=SimpleNamespace(root=tmp_path, resolve=lambda _: path))
    context = ToolContext('owner', role, read_scopes=(WriteScope('image.png', recursive=False),))
    with pytest.raises(ToolExecutionError, match=expected.replace('(', r'\(')) as error:
        services.read_file(context, {'path': 'image.png'})
    assert error.value.code == 'read_file_binary'
    if role == 'slide':
        assert 'inspect(path=' not in str(error.value)


def test_read_file_search_is_literal_bounded_and_keeps_source_line_numbers(tmp_path):
    from runtime.tools import WriteScope
    from state.workspace import WorkspaceState
    root = tmp_path / "workspace"
    services = ProductionToolServices.__new__(ProductionToolServices)
    services.workspace = WorkspaceState(root)
    source = root / "knowledge.md"
    source.write_text("\n".join(["## Page 1", "introduction", "CPU [M3]", "context", "more", "## Page 2", "GPU [m3]", "last"]))
    context = ToolContext("research", "research", read_scopes=(WriteScope("knowledge.md", recursive=False),))
    args = {"path": "knowledge.md", "query": "[m3]", "limit": 4}
    first = services.read_file(context, args)
    second = services.read_file(context, {**args, "offset": first["next_offset"]})
    assert first["line_count"] == 8 and first["content"].startswith("1|## Page 1")
    assert "3|CPU [M3]" in first["content"] and "7|GPU [m3]" in second["content"]
    assert len(first["content"].splitlines()) <= 4 and second["next_offset"] is None
    assert services.read_file(context, {"path": "knowledge.md", "query": "not present"})["content"] == ""
    with pytest.raises(ToolContractError, match="may not read"):
        services.read_file(context, {"path": "other.md", "query": "secret"})


def test_read_file_search_preserves_context_across_truncated_windows(tmp_path):
    from runtime.tools import WriteScope
    from state.workspace import WorkspaceState
    services = ProductionToolServices.__new__(ProductionToolServices)
    services.workspace = WorkspaceState(tmp_path / "workspace")
    source = services.workspace.paths.root / "knowledge.md"
    source.write_text("\n".join(["one", "two", "ROUTE [name]", "four", "five", "unread six", "unread seven"]))
    context = ToolContext("research", "research", read_scopes=(WriteScope("knowledge.md", recursive=False),))
    arguments = {"path": "knowledge.md", "query": "[name]", "limit": 3}
    first = services.read_file(context, arguments)
    assert first["mode"] == "search_snippets" and first["returned_line_count"] == 3
    assert first["next_offset"] == 4 and first["line_count"] == 7
    second = services.read_file(context, {**arguments, "offset": first["next_offset"]})
    assert second["content"] == "4|four\n5|five"
    assert second["returned_line_count"] == 2 and second["next_offset"] is None
    assert "not a complete file or section read" in second["note"]
    empty = services.read_file(context, {**arguments, "query": "absent"})
    assert empty["content"] == "" and empty["returned_line_count"] == 0
    assert empty["mode"] == "search_snippets" and empty["next_offset"] is None
    full_range = services.read_file(context, {"path": "knowledge.md", "offset": 3, "limit": 5})
    assert full_range["mode"] == "line_range" and full_range["returned_line_count"] == 5
    assert full_range["content"].endswith("7|unread seven") and "note" not in full_range


def test_read_file_line_range_is_prompt_bounded_without_losing_continuation(tmp_path):
    from runtime.tools import WriteScope
    from state.workspace import WorkspaceState
    services = ProductionToolServices.__new__(ProductionToolServices)
    services.workspace = WorkspaceState(tmp_path / "workspace")
    source = services.workspace.paths.root / "knowledge.md"
    lines = [f"line {index:03d} " + ("x" * 500) for index in range(1, 201)]
    source.write_text("\n".join(lines))
    context = ToolContext("research", "research", read_scopes=(WriteScope("knowledge.md", recursive=False),))
    result = services.read_file(context, {"path": "knowledge.md", "limit": 1000})
    assert result["content_truncated_for_prompt"] is True
    assert result["content_limit_bytes"] == 48 * 1024
    assert result["content_bytes"] <= result["content_limit_bytes"]
    assert 0 < result["returned_line_count"] < len(lines)
    assert result["next_offset"] == result["returned_line_count"] + 1
    resumed = services.read_file(context, {"path": "knowledge.md", "offset": result["next_offset"], "limit": 1})
    assert resumed["content"].startswith(f"{result['next_offset']}|{lines[result['next_offset'] - 1]}")


def test_orchestrator_direct_authoring_warns_without_page_count_threshold(tmp_path, monkeypatch):
    from owner.registry import OwnerRegistry
    import runtime.production as production_module
    from state.artifacts import ArtifactStore
    from state.workspace import WorkspaceState

    def config_module(count):
        return SimpleNamespace(read_config=lambda _root, require_complete=False: {
            "working_language": "zh", "presentation_language": "zh", "target_page_count": count})

    services = ProductionToolServices.__new__(ProductionToolServices)
    services.workspace = WorkspaceState(tmp_path / "workspace")
    services.workspace._config_module = config_module(5)
    services.snapshot = SimpleNamespace(name=SKILL.name, target=SKILL, tree_sha256="test-skill")
    services.artifacts = ArtifactStore(services.workspace.paths.root, workspace_state=services.workspace)
    services.settings = SimpleNamespace()
    services.run_spec = SimpleNamespace(revision_request=None)
    services.owners = OwnerRegistry(services.workspace.paths.root, deck_id=services.workspace.deck_id)
    context = ToolContext("orchestrator", "orchestrator")

    def fake_scaffold(root, *, pages, **_kwargs):
        (Path(root) / "slides").mkdir(exist_ok=True)
        for page in pages:
            (Path(root) / f"slides/slide_{page:02d}.html").write_text("<section>scaffold</section>")

    monkeypatch.setattr(production_module, "load_skill_module",
                        lambda *_args, **_kwargs: SimpleNamespace(scaffold_from_plans=fake_scaffold))

    result = services.write_file(context, {"path": "slides/slide_01.html", "content": "<section></section>"})
    assert result["parallelization_advisory"]["code"] == "orchestrator_direct_authoring_guardrail"
    assert "delegate_submit" in result["parallelization_advisory"]["message"]
    assert "页数" not in result["parallelization_advisory"]["message"]
    assert services.workspace.paths.page_html(1).exists()
    assert services.owners.page_owner(1).owner_id == "orchestrator"

    services.workspace._config_module = config_module(1)
    assert services._orchestrator_direct_authoring_advisory(context, 1) is None


@pytest.mark.parametrize('command,help_pattern', [
    ('cat renders/slide_01.png', 'scripts/slide.py --help'),
    ('ls renders/ | sh', '不执行 shell'),
])
def test_invalid_shell_command_returns_role_help_without_execution(command, help_pattern):
    services = ProductionToolServices.__new__(ProductionToolServices)
    services.snapshot = SimpleNamespace(name=SKILL.name)
    with pytest.raises(ToolContractError, match=help_pattern):
        services._validated_command('slide', command)


def test_role_command_accepts_legal_root_positions_without_relaxing_scope(tmp_path):
    services = ProductionToolServices.__new__(ProductionToolServices)
    services.workspace = SimpleNamespace(paths=SimpleNamespace(root=tmp_path))
    services.snapshot = SimpleNamespace(name=SKILL.name, target=SKILL, tree_sha256="test-cli", resolve_skill_path=lambda _: tmp_path / "shared/slide.py")
    command = "python skills/mural-presenter-v1/scripts/slide.py render"
    argv, role, action, request = services._validated_command("slide", command + " --page 3 .")
    assert argv[3:] == ["--page", "3", "."]
    assert request.page == 3
    with pytest.raises(ToolContractError) as error:
        services._validated_command("slide", command + " /outside --page 3")
    assert error.value.code == "terminal_root_denied"
    argv, role, action, request = services._validated_command("slide", command + " . --page 3")
    assert argv[3:] == [".", "--page", "3"]
    assert (role, action) == ("slide", "render")


@pytest.mark.parametrize("entrypoint", ["main_runtime_env", "single_deck_environment"])
def test_owner_concurrency_uses_per_run_config_through_real_entrypoints(tmp_path, monkeypatch, entrypoint):
    import infer
    from runtime.config import load_runtime_config

    config = load_runtime_config()
    config["resources"]["directory"] = str(tmp_path / "resource-slots")
    config["decks"]["owners"] = 2
    run_config = tmp_path / "run-runtime.json"
    run_config.write_text(json.dumps(config))
    environment = {"MURAL_RUNTIME_CONFIG": str(run_config)}
    if entrypoint == "main_runtime_env":
        # CLI 的显式进程环境优先；此处文件指针与默认 JSON 区分，不改变该优先级。
        monkeypatch.delenv("MURAL_RUNTIME_CONFIG", raising=False)
        runtime_env = tmp_path / "runtime.env"
        runtime_env.write_text(f"MURAL_RUNTIME_CONFIG={run_config}\n")
    else:
        process_config = tmp_path / "process-runtime.json"
        config["decks"]["owners"] = 7
        process_config.write_text(json.dumps(config))
        monkeypatch.setenv("MURAL_RUNTIME_CONFIG", str(process_config))
    process_pointer = os.environ.get("MURAL_RUNTIME_CONFIG")
    assert load_runtime_config()["decks"]["owners"] != 2
    observed = []

    class BackendReached(Exception):
        pass

    def backend(services):
        observed.append(services.orchestration._max_workers)
        assert services.orchestration._max_workers == 2
        raise BackendReached

    workspace = tmp_path / "workspace"
    with pytest.raises(BackendReached):
        if entrypoint == "main_runtime_env":
            infer.main(["--query", "Check configuration wiring", "--workspace", str(workspace),
                        "--runtime-env", str(runtime_env)], backend=backend)
        else:
            spec = RunSpec.from_inputs(raw_user_query="Check configuration wiring", output_workspace=workspace,
                                       environment=environment)
            infer._run_single_deck(spec, environment, mode=RunMode.INFERENCE, backend=backend)
    assert observed == [2]
    assert os.environ.get("MURAL_RUNTIME_CONFIG") == process_pointer
