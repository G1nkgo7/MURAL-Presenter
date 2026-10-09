"""隔离内容审计员（FEAT-2.2）的单元与接线测试。

分三层，全部用脚本化 reviewer/invoker，不发起任何真实 Provider 调用：
- ``qc/content_review`` 组件：build_packet 正确、findings 解析、优雅降级、结论汇总；
- ``export/training`` 准入：未处置高危 finding → rejected；无 finding → accepted；
  incomplete → pending 且不可训练；未接入汇总不据此拒绝；inference 不适用。
"""
from __future__ import annotations

from geometry_fixture import geometry, write_geometry

from pathlib import Path
import pytest
import json

from PIL import Image

from export.delivery import DeliveryExporter
from export.training import TrainingExporter, TrainingSegmentInput
from qc.content import ContentPacket, ContentQC, SourceDocument, VisiblePageText
from qc.content_review import (
    CONTENT_RUBRIC_VERSION,
    ContentReviewResult,
    IsolatedContentReviewer,
    parse_content_findings,
    run_content_review,
)
from qc.routing import IssueLedger
from runtime.config import RunMode, RunSpec
from runtime.recovery import SegmentCheckpoint
from state.artifacts import ArtifactStore
from state.workspace import WorkspaceState


# --------------------------------------------------------------------------- #
# 测试基架
# --------------------------------------------------------------------------- #


def _run_spec(tmp_path: Path, query: str = "对比 2023 与 2024 的营收。") -> RunSpec:
    if not (tmp_path / "config.json").exists():
        write_geometry(tmp_path, geometry(width_px=1600, height_px=900))
    return RunSpec.from_inputs(
        raw_user_query=query,
        output_workspace=tmp_path,
        environment={
            "MODEL_API_FORMAT": "anthropic",
            "MODEL": "test-model",
            "MODEL_BASE_URL": "https://example.test",
        },
    )


def _packet(tmp_path: Path) -> ContentPacket:
    """编译一个含来源 + 一页可见文本的真实事实包。"""
    workspace = WorkspaceState(tmp_path)
    source = tmp_path / "knowledge.md"
    source.write_text("2023 营收 100，2024 营收 120。", encoding="utf-8")
    workspace.register_pages([1])
    workspace.paths.page_html(1).write_text(
        "<html><body><h1>营收对比</h1><p>2023 为 100，2024 为 120</p>"
        "<script>window.x='隐藏'</script></body></html>",
        encoding="utf-8",
    )
    return ContentQC.build_packet(
        _run_spec(tmp_path),
        workspace,
        pages=[1],
        source_paths=["knowledge.md"],
    )


def _ready_workspace(root: Path):
    """复用交付测试的最小 ready workspace（1 页，可导出 delivery=complete）。"""
    workspace = WorkspaceState(root)
    write_geometry(
        root,
        geometry(
            width_px=1600,
            height_px=1200,
        ),
    )
    artifacts = ArtifactStore(root, workspace_state=workspace)
    issues = IssueLedger(root)
    workspace.register_pages([1])
    html_path = workspace.paths.page_html(1)
    html_path.write_text(
        "<!doctype html><html><head>"
        '<link rel="stylesheet" href="../base.css"></head>'
        '<body><main class="mural-slide">P01</main></body></html>',
        encoding="utf-8",
    )
    workspace.register_scaffold(1)
    artifacts.declare(
        artifact_id="page-html-01",
        kind="page_html",
        path=html_path,
        owner_id="owner-slide",
        page=1,
    )
    artifacts.commit(owner_id="owner-slide", artifact_ids=["page-html-01"], source_hashes={})

    def renderer(_html: Path, png: Path, _canvas: dict):
        png.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1600, 1200), (10, 20, 30)).save(png)
        return {"geometry": _canvas, "loaded_resources": [], "failed_resources": []}

    artifacts.render_pages([1], renderer)
    artifacts.record_inspection(1, inspection_id="inspection-1")
    workspace.mark_ready(1)
    return workspace, artifacts, issues


def _synthesis_export(root: Path, *, content_qc):
    """跑一次 synthesis 训练导出，返回 manifest 与 rejection reasons。"""
    workspace, artifacts, issues = _ready_workspace(root)
    delivery = DeliveryExporter(workspace, artifacts, issues).export(
        include_pdf=False, include_pptx=False, include_zip=False
    )
    checkpoint = SegmentCheckpoint(
        segment_id="segment-1",
        predecessor_segment_id=None,
        messages=({"role": "user", "content": "写页面。"},),
    )
    inputs = [
        TrainingSegmentInput(owner_id="owner-slide", role="slide", checkpoint=checkpoint)
    ]
    return TrainingExporter(
        workspace, artifacts, issues, run_mode=RunMode.SYNTHESIS
    ).export(inputs, delivery, content_qc=content_qc)


# --------------------------------------------------------------------------- #
# 组件：build_packet + findings 解析 + 降级
# --------------------------------------------------------------------------- #


def test_build_packet_captures_query_sources_and_visible_text(tmp_path: Path) -> None:
    packet = _packet(tmp_path)
    assert packet.raw_user_query == "对比 2023 与 2024 的营收。"
    assert packet.sources[0].text == "2023 营收 100，2024 营收 120。"
    # 可见文本机械提取，script 被剔除。
    assert packet.pages[0].text == "营收对比 2023 为 100，2024 为 120"
    assert "隐藏" not in packet.pages[0].text
    assert packet.to_dict()["pages"][0]["used_images"] is None


@pytest.mark.parametrize("reference", ["assets/keyboard.png", "../assets/keyboard.png", "file_uri"])
def test_content_packet_joins_actual_image_to_hash_bound_source_page(tmp_path, reference):
    from state.workspace import file_sha256
    workspace = WorkspaceState(tmp_path)
    workspace.register_pages([1])
    html = workspace.paths.page_html(1)
    html.write_text('<p>Sources: pp. 2, 10</p><p style="display:none">page 15</p>')
    asset = workspace.paths.assets / "keyboard.png"
    Image.new("RGB", (40, 40), "white").save(asset)
    if reference == "file_uri":
        reference = asset.as_uri()
    crops = workspace.paths.trace / "material-figures.json"
    crops.write_text(json.dumps({"assets": {"assets/keyboard.png": {
        "sha256": file_sha256(asset), "source_document": "inputs/originals/material.pdf", "source_page": 15}}}))
    receipt = {"html_sha256": file_sha256(html), "diagnostics": {
        "visible_images": [{"src": reference}, {"src": reference}], "rendered_text": ["Sources: pp. 2, 10"]}}
    def packet():
        return ContentQC.build_packet(_run_spec(tmp_path, "Cite every material image on the slide."),
            workspace, pages=[1], render_receipts={1: receipt})
    first = packet()
    assert len(first.pages[0].used_images) == 1
    assert first.pages[0].used_images[0]["source_page"] == 15
    assert first.pages[0].rendered_text == ("Sources: pp. 2, 10",)
    assert "page 15" not in first.pages[0].text  # 明确 display:none 的文字不是上屏引用。
    def reviewer(facts):
        assert facts.pages[0].used_images[0]["source_page"] == 15
        return [{"kind": "citation_gap", "severity": "major", "pages": [1],
                 "owner_hint": "slide", "summary": "Material image from p. 15 is missing from visible sources."}]
    findings = ContentQC.review(first, reviewer)
    assert findings[0].kind == "citation_gap"
    assert IssueLedger(tmp_path).record_content_findings(findings)[0].severity == "major"
    crops.write_text(crops.read_text().replace('"source_page": 15', '"source_page": 7'))
    assert packet().to_dict() != first.to_dict()  # 来源变化使内容审计缓存失效。
    Image.new("RGB", (40, 40), "blue").save(asset)
    assert "source_page" not in packet().pages[0].used_images[0]  # 不沿用旧字节的来源证明。
    html.write_text("Changed after rendering")
    with pytest.raises(ValueError, match="stale.*page 1"):
        packet()


def test_parse_content_findings_reads_array_and_strips_fences() -> None:
    fenced = (
        "```json\n"
        '[{"kind":"coverage_gap","severity":"major","pages":[1],'
        '"summary":"缺 2024 数据","owner_hint":"slide"}]\n'
        "```"
    )
    parsed = parse_content_findings(fenced)
    assert parsed == [
        {
            "kind": "coverage_gap",
            "severity": "major",
            "pages": [1],
            "summary": "缺 2024 数据",
            "owner_hint": "slide",
        }
    ]
    # 前后噪声容忍。
    noisy = '好的，结论如下：[{"kind":"off_topic","severity":"minor","pages":[],"summary":"x","owner_hint":"none"}] 完毕。'
    assert parse_content_findings(noisy)[0]["kind"] == "off_topic"


def test_isolated_reviewer_parses_findings_via_content_qc(tmp_path: Path) -> None:
    packet = _packet(tmp_path)

    def invoker(received: ContentPacket) -> str:
        assert received.raw_user_query == packet.raw_user_query
        return (
            '[{"kind":"factual_conflict","severity":"critical","pages":[1],'
            '"summary":"可见 120 与来源不一致","owner_hint":"slide"}]'
        )

    reviewer = IsolatedContentReviewer(invoker)
    findings = ContentQC.review(packet, reviewer)
    assert len(findings) == 1
    assert findings[0].kind == "factual_conflict"
    assert findings[0].severity == "critical"


def test_run_content_review_completed_with_high_severity_finding(tmp_path: Path) -> None:
    packet = _packet(tmp_path)
    result = run_content_review(
        packet,
        lambda _p: '[{"kind":"coverage_gap","severity":"major","pages":[1],'
        '"summary":"缺关键要求","owner_hint":"slide"}]',
        reviewer_model="reviewer-x",
    )
    assert result.status == "completed"
    assert result.unresolved is True
    assert result.passed is False
    summary = result.to_summary()
    assert summary["unresolved_high_severity"] is True
    assert summary["content_qc_incomplete"] is False


def test_run_content_review_passes_when_no_high_severity(tmp_path: Path) -> None:
    packet = _packet(tmp_path)
    # 空数组 = 无问题；仅 minor = 通过（不算未处置高危）。
    empty = run_content_review(packet, lambda _p: "[]", reviewer_model="rev")
    assert empty.passed is True and empty.unresolved is False
    minor = run_content_review(
        packet,
        lambda _p: '[{"kind":"off_topic","severity":"minor","pages":[],'
        '"summary":"轻微","owner_hint":"none"}]',
        reviewer_model="rev",
    )
    assert minor.passed is True and minor.unresolved is False


def test_run_content_review_degrades_to_incomplete_on_provider_error(
    tmp_path: Path,
) -> None:
    packet = _packet(tmp_path)

    def boom(_p: ContentPacket) -> str:
        raise RuntimeError("provider channel unavailable")

    result = run_content_review(packet, boom, reviewer_model="rev")
    assert result.status == "incomplete"
    assert result.passed is False
    assert "provider channel unavailable" in result.incomplete_reason
    assert result.to_summary()["content_qc_incomplete"] is True


def test_run_content_review_degrades_on_unparseable_output(tmp_path: Path) -> None:
    packet = _packet(tmp_path)
    result = run_content_review(
        packet, lambda _p: "抱歉我无法完成", reviewer_model="rev"
    )
    assert result.status == "incomplete"
    assert "no JSON array" in result.incomplete_reason


@pytest.mark.parametrize("text", ["", "   ", "```json\n```", '[{"kind":'])
def test_incomplete_reviewer_text_is_not_a_pass(tmp_path: Path, text: str) -> None:
    packet = _packet(tmp_path)
    result = run_content_review(packet, lambda _p: text, reviewer_model="rev")
    assert result.status == "incomplete"
    assert result.passed is False
    assert result.unresolved is False


def test_audit_dict_has_no_credentials_and_records_findings(tmp_path: Path) -> None:
    packet = _packet(tmp_path)
    result = run_content_review(
        packet,
        lambda _p: '[{"kind":"coverage_gap","severity":"major","pages":[1],'
        '"summary":"缺","owner_hint":"slide"}]',
        reviewer_model="reviewer-x",
    )
    audit = result.to_audit_dict()
    assert audit["schema"] == "mural.content-qc.v1"
    assert audit["rubric_version"] == CONTENT_RUBRIC_VERSION
    assert audit["findings"][0]["kind"] == "coverage_gap"
    for forbidden in ("api_key", "authorization", "signature", "thinking"):
        assert forbidden not in repr(audit).lower()


# --------------------------------------------------------------------------- #
# 无据现实边界（P07 事实边界回归）：把用户从未提供的、关于演示者自身真实场景的
# 具体安排（时间/房间/日期）当确认事实上屏，应被审计判为高危；用户明确要求的
# 教学虚构案例不受此限，不得误报。
# --------------------------------------------------------------------------- #


def _unsourced_schedule_packet(tmp_path: Path) -> ContentPacket:
    """复刻 R9 P07 形态：无来源、可见文本含虚构的“下一节课时间/教室”。"""
    workspace = WorkspaceState(tmp_path)
    workspace.register_pages([1])
    workspace.paths.page_html(1).write_text(
        "<html><body><h1>Carry these questions into the collaboration course</h1>"
        "<p>Minutes are a list of what happens next.</p>"
        "<footer>Next: Collaboration Course · 11:00 AM · Room 3B</footer></body></html>",
        encoding="utf-8",
    )
    # 原 query 只说“紧接其后的正式课程”，从未给钟点或教室号。
    query = (
        "10 分钟新人培训，讲会议纪要，7 页，末页把问题带进紧接其后的正式协作课程"
        "（the formal collaboration course that follows）。"
    )
    return ContentQC.build_packet(
        _run_spec(tmp_path, query), workspace, pages=[1], source_paths=[]
    )


def test_rubric_identity_and_unsourced_reality_clause() -> None:
    assert CONTENT_RUBRIC_VERSION == "content-audit-rubric.v5"
    # Rubric 明确点出“无据现实”判据与“教学虚构不误报”的区分，供隔离模型据此裁决。
    from qc.content_review import CONTENT_RUBRIC
    assert "无据现实" in CONTENT_RUBRIC
    assert "示例" in CONTENT_RUBRIC and "illustrative" in CONTENT_RUBRIC
    assert "教学" in CONTENT_RUBRIC  # 明示合法教学虚构不报


def test_fabricated_real_schedule_routes_to_admission_rejection(tmp_path: Path) -> None:
    # 审计员（脚本化，模拟按 v2 rubric 抓到无据现实）→ 高危 finding → 训练准入拒绝。
    packet = _unsourced_schedule_packet(tmp_path)
    result = run_content_review(
        packet,
        lambda _p: '[{"kind":"factual_conflict","severity":"major","pages":[1],'
        '"summary":"P07 footer 把用户未提供的 11:00 AM/Room 3B 当作确认现实",'
        '"owner_hint":"orchestrator"}]',
        reviewer_model="reviewer-27b",
    )
    assert result.status == "completed"
    assert result.unresolved is True and result.passed is False
    assert result.source_count == 0
    # 汇总驱动 synthesis 训练准入：高危 → rejected。
    summary = result.to_summary()
    manifest = _synthesis_export(tmp_path / "export", content_qc={
        "effective": True,
        "content_qc_incomplete": summary["content_qc_incomplete"],
        "unresolved_high_severity": summary["unresolved_high_severity"],
    })
    assert manifest["training_admission"] == "rejected"
    assert "content_qc_unresolved" in manifest["rejection_reasons"]


def test_legitimate_teaching_fiction_is_not_flagged(tmp_path: Path) -> None:
    # 对照：用户明确要“会议纪要反例”这类教学虚构，审计员返回空数组 → 通过，不误报。
    workspace = WorkspaceState(tmp_path)
    workspace.register_pages([1])
    workspace.paths.page_html(1).write_text(
        "<html><body><h1>This Is What Everyone Hands In</h1>"
        "<p>10:06 Lena felt we should sync with Design about the onboarding copy.</p>"
        "</body></html>",
        encoding="utf-8",
    )
    query = (
        "10 分钟新人培训，前 2 页用大家常写的“逐字流水账会议纪要”反例快速进入场景。"
    )
    packet = ContentQC.build_packet(
        _run_spec(tmp_path, query), workspace, pages=[1], source_paths=[]
    )
    # 发明的示例会议内容是用户点名要的教学虚构 → 无 finding。
    result = run_content_review(packet, lambda _p: "[]", reviewer_model="reviewer-27b")
    assert result.status == "completed"
    assert result.passed is True and result.unresolved is False


# --------------------------------------------------------------------------- #
# 训练准入接线
# --------------------------------------------------------------------------- #


def test_training_rejected_on_unresolved_high_severity(tmp_path: Path) -> None:
    manifest = _synthesis_export(
        tmp_path,
        content_qc={
            "effective": True,
            "content_qc_incomplete": False,
            "unresolved_high_severity": True,
        },
    )
    assert manifest["training_admission"] == "rejected"
    assert "content_qc_unresolved" in manifest["rejection_reasons"]


def test_training_accepted_when_content_qc_passes(tmp_path: Path) -> None:
    manifest = _synthesis_export(
        tmp_path,
        content_qc={
            "effective": True,
            "content_qc_incomplete": False,
            "unresolved_high_severity": False,
        },
    )
    assert manifest["training_admission"] == "accepted"
    assert "content_qc_unresolved" not in manifest["rejection_reasons"]
    assert manifest["schema"] == "mural.training-export.v2"
    assert manifest["content_qc"]["effective"] is True
    assert "annotation_sidecar_path" not in manifest
    assert not (tmp_path / "_trace/training/training-critic-annotations.json").exists()


def test_training_pending_on_content_qc_incomplete(tmp_path: Path) -> None:
    manifest = _synthesis_export(
        tmp_path,
        content_qc={
            "effective": True,
            "content_qc_incomplete": True,
            "unresolved_high_severity": False,
        },
    )
    assert manifest["training_admission"] == "pending"
    assert manifest["eligible_for_training"] is False
    rows = [json.loads(line) for line in (tmp_path / manifest["segments_path"]).read_text().splitlines()]
    assert rows and all(row["training_admission"] == "pending" and not row["trainable"] for row in rows)
    assert all((tmp_path / row["checkpoint_path"]).is_file() for row in rows)
    assert "content_qc_incomplete" in manifest["rejection_reasons"]


def test_training_pending_when_content_qc_is_not_effective(
    tmp_path: Path,
) -> None:
    # 汇总未实际生效时待补审，不能成为质量负样本，也不能入训。
    manifest = _synthesis_export(
        tmp_path,
        content_qc={"effective": False},
    )
    assert manifest["training_admission"] == "pending"
    assert manifest["eligible_for_training"] is False
    rows = [json.loads(line) for line in (tmp_path / manifest["segments_path"]).read_text().splitlines()]
    assert rows and all(row["training_admission"] == "pending" and not row["trainable"] for row in rows)
    assert all((tmp_path / row["checkpoint_path"]).is_file() for row in rows)
    assert "content_qc_not_effective_in_synthesis" in manifest["rejection_reasons"]


def test_training_not_rejected_when_content_qc_not_provided(tmp_path: Path) -> None:
    # 原始轨迹可供后处理，但未审不能冒充 clean 或已通过内容 QC。
    manifest = _synthesis_export(tmp_path, content_qc=None)
    assert manifest["training_admission"] == "accepted"
    assert manifest["data_stage"] == "raw"
    assert manifest["content_qc"] == {"stage": "postprocess", "status": "pending"}
    for reason in manifest["rejection_reasons"]:
        assert not str(reason).startswith("content_qc")


def test_inference_ignores_content_qc_admission(tmp_path: Path) -> None:
    workspace, artifacts, issues = _ready_workspace(tmp_path)
    delivery = DeliveryExporter(workspace, artifacts, issues).export(
        include_pdf=False, include_pptx=False, include_zip=False
    )
    checkpoint = SegmentCheckpoint(
        segment_id="segment-1", predecessor_segment_id=None,
        messages=({"role": "user", "content": "x"},),
    )
    manifest = TrainingExporter(
        workspace, artifacts, issues, run_mode=RunMode.INFERENCE
    ).export(
        [TrainingSegmentInput(owner_id="owner-slide", role="slide", checkpoint=checkpoint)],
        delivery,
        content_qc={"effective": True, "unresolved_high_severity": True},
    )
    # inference 不做正样本准入，内容 QC 高危也不拒。
    assert manifest["training_admission"] == "not_applicable"


@pytest.mark.parametrize("mode", [RunMode.INFERENCE, RunMode.SYNTHESIS])
def test_ready_is_independent_of_content_qc_and_keeps_tool_transactions_closed(tmp_path, monkeypatch, mode):
    from dataclasses import replace
    from infer import PresenterKernel
    from runtime.tools import ToolCall

    workspace, _, _ = _ready_workspace(tmp_path)
    kernel = PresenterKernel(replace(_run_spec(tmp_path), run_mode=mode))

    def forbidden(*args, **kwargs):
        raise AssertionError("ready must not invoke a content reviewer")

    monkeypatch.setattr(ContentQC, "review", forbidden)
    try:
        for _ in (1, 2):
            assert kernel.services.complete_deck()["status"] == "ready"
        kernel.tools.assert_closed()
        assert not kernel.issues.issues()
        assert not (tmp_path / "_trace/content-qc.json").exists()
        assert "content_qc_" not in kernel.live_trace.path.read_text()
        assert not hasattr(kernel.services, "content_review")
    finally:
        kernel.orchestration.close(wait=True)


def test_production_cannot_reenable_inline_content_qc_with_old_environment_keys():
    from dataclasses import fields
    from runtime.production import ProductionSettings

    settings = ProductionSettings.from_environment({
        "MURAL_SKILL_DIR": str(Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1"),
        "ENABLE_CONTENT_QC": "1", "CONTENT_QC_MODEL": "must-not-run", "CONTENT_QC_TIMEOUT_S": "1"})
    assert not any("content_qc" in field.name for field in fields(settings))
    assert not settings.effective_external_visual_critic(RunMode.SYNTHESIS)
    enabled = ProductionSettings.from_environment({
        "MURAL_SKILL_DIR": str(Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1"),
        "ENABLE_EXTERNAL_VISUAL_CRITIC": "1",
    })
    assert enabled.effective_external_visual_critic(RunMode.SYNTHESIS)


@pytest.mark.parametrize("response,expected", [
    ("[]", "completed"),
    ('[{"kind":"factual_conflict","severity":"major","pages":[1],"summary":"later QC finding","owner_hint":"slide"}]', "completed"),
    ("truncated invalid JSON", "incomplete"),
])
def test_postprocess_review_cannot_mutate_raw_or_reopen_owners(tmp_path, response, expected):
    workspace, artifacts, issues = _ready_workspace(tmp_path)
    packet = ContentQC.build_packet(_run_spec(tmp_path), workspace, pages=[1],
        render_receipts={1: artifacts.render_receipt(1)})
    paths = [workspace.path, artifacts.path, issues.path,
             workspace.paths.page_html(1), workspace.paths.page_png(1)]
    before = {path: path.read_bytes() for path in paths if path.is_file()}
    result = run_content_review(packet, lambda _packet: response, reviewer_model="offline-reviewer")
    assert result.status == expected
    assert {path: path.read_bytes() for path in before} == before
    assert not issues.issues()
    assert not (tmp_path / "_trace/content-qc.json").exists()
