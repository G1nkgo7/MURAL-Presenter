from __future__ import annotations

from geometry_fixture import geometry, write_geometry

import base64
import hashlib
import json
import runpy
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
from pptx import Presentation
from pptx.util import Inches
import pytest

from export.delivery import DeliveryError, DeliveryExporter
from export.training import (
    TrainingExporter,
    TrainingExportError,
    TrainingSegmentInput,
    _scrub_ephemeral_training_text,
)
from qc.routing import IssueLedger
from qc.visual import ReviewVerdict, VisualCandidate, VisualQC
from runtime.config import RunMode
from runtime.recovery import SegmentCheckpoint
from state.artifacts import ArtifactStore
from state.workspace import WorkspaceState, file_sha256
from test_editable_pptx import browser_environment
from test_v1_font_pipeline import pinned_font_sources, _pin_font_manifest


def test_offline_training_cli_preserves_completed_raw_and_reports_partial_failure(tmp_path, monkeypatch, capsys):
    from export.training import main, export_workspace
    from test_shared_entry import _backend, _renderer, _analyzer, _run_spec
    from infer import run_presenter
    complete, missing, unfinished = (tmp_path / name for name in ("complete", "missing", "unfinished"))
    run_presenter(_run_spec(complete, RunMode.SYNTHESIS), _backend, renderer=_renderer,
        vision_analyzer=_analyzer, include_pdf=False, include_pptx=False, include_zip=False)
    unfinished.mkdir()
    (unfinished / "_state").mkdir()
    (unfinished / "result.json").write_text('{"status":"error","artifact_status":"complete"}')
    (unfinished / "_state/run.json").write_text('{"run_mode":"synthesis"}')
    before = {path: path.read_bytes() for path in (complete / "_trace/raw/segments").glob("*.json")}
    before[complete / "result.json"] = (complete / "result.json").read_bytes()
    assert main([str(complete), str(missing), str(unfinished)]) == 1
    reports = json.loads(capsys.readouterr().out)
    assert [row["status"] for row in reports] == ["exported", "error", "error"]
    assert not missing.exists() and not (unfinished / "_trace").exists()
    assert all(path.read_bytes() == data for path, data in before.items())
    assert not (complete / "_trace/training/segments").exists()
    assert not (complete / "_trace/training/blobs").exists()
    host = complete / "_state/skill-runtime.json"
    host.write_text('{"status":"running"}')
    with pytest.raises(TrainingExportError, match="active or resumable"):
        export_workspace(complete)


@pytest.mark.parametrize("with_zip", [False, True])
def test_training_identity_comes_from_workspace_pins_not_optional_zip(tmp_path, with_zip):
    workspace = WorkspaceState(tmp_path)
    workspace.paths.skill_ref.write_text(json.dumps({"schema": "mural.skill-ref.v1", "tree_sha256": "a" * 64}))
    workspace.paths.font_manifest.write_text('{"schema":"mural.font-manifest.v2","selected_faces":[]}')
    outputs = {"portable_zip": {"skill_tree_sha256": "obsolete", "font_manifest_sha256": "obsolete"}} if with_zip else {}
    checkpoint = SegmentCheckpoint.initial((
        {"role": "user", "content": "query"}, {"role": "assistant", "content": "done"},
    ), segment_id="s0")
    manifest = TrainingExporter(workspace, SimpleNamespace(), IssueLedger(tmp_path), run_mode="synthesis").export(
        [TrainingSegmentInput("owner", "slide", checkpoint)],
        SimpleNamespace(status="complete", outputs=outputs, path=""))
    assert manifest["skill_tree_sha256"] == "a" * 64
    assert manifest["font_manifest_sha256"] == file_sha256(workspace.paths.font_manifest)
    assert manifest["training_admission"] == "accepted"


def test_training_text_scrub_redacts_ephemeral_ids_without_touching_image_refs():
    messages = [{
        "role": "user",
        "content": [
            {"type": "image", "source": {"type": "blob_ref", "sha256": "a" * 64,
                "path": "_trace/raw/blobs/image.png"}},
            {"type": "text", "text": "检查对象：P2；page_uid：pg_02_adca4ec3d6c6482a。\n"
                "inspection-240a4e249fa66ab1a11b 77b31b51fc888e6d8bc71795200ec0f63ebaa96a42efeeb0799a2ae0d90a4f20"},
        ],
    }, {
        "role": "assistant",
        "content": [{"type": "text", "text": "已根据 page_uid：pg_02_adca4ec3d6c6482a 与 "
            "inspection-240a4e249fa66ab1a11b 77b31b51fc888e6d8bc71795200ec0f63ebaa96a42efeeb0799a2ae0d90a4f20 继续。"}],
    }, {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "inspect-1", "content": json.dumps({
            "inspection_id": "inspection-240a4e249fa66ab1a11b",
            "page_uid": "pg_02_adca4ec3d6c6482a",
            "png_sha256": "b" * 64,
            "critic_question": "用途：整页验收。",
            "owner_question": "请检查页面。",
            "critic_prompt_version": "visual-critic-question.v4",
            "render_diagnostics": {"layout": "private"},
            "inspect_timings_s": {"total": 1.25},
            "opened_visual_sources": [{"path": "old.png"}],
            "opened_not_verified": "debug notice",
            "members": [{"page_uid": "pg_02_adca4ec3d6c6482a"}],
            "model_config": {"max_tokens": 8192},
            "source_refs": [{"raw": "trace"}],
            "critic_feedback": {
                "completed": True,
                "body": "右下角稍拥挤。",
                "critic_question": "hidden",
                "model_config": {"temperature": 0},
                "source_refs": ["trace"],
            },
            "visual_source": {
                "owner_id": "slide-1",
                "path": "renders/slide_02.png",
                "sha256": "d" * 64,
                "transport_media_type": "image/png",
            },
            "keep": "human text",
        })}],
    }, {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "inspect-2", "content": {
            "inspection_id": "inspection-240a4e249fa66ab1a11b",
            "tool_use_id": "inner-inspect-2",
            "nested": [{"page_uid": "pg_02_adca4ec3d6c6482a"}],
            "image": {"source": {"type": "blob_ref", "sha256": "c" * 64,
                "path": "_trace/raw/blobs/other.png"}},
        }}],
    }]

    scrubbed = _scrub_ephemeral_training_text(messages)
    user_text = scrubbed[0]["content"][1]["text"]
    assistant_text = scrubbed[1]["content"][0]["text"]
    result = json.loads(scrubbed[2]["content"][0]["content"])
    structured = scrubbed[3]["content"][0]

    assert "pg_02_adca4ec3d6c6482a" in user_text
    assert "77b31b51fc888e6d8bc71795200ec0f63ebaa96a42efeeb0799a2ae0d90a4f20" in user_text
    assert "page_uid" not in assistant_text
    assert "pg_02_adca4ec3d6c6482a" not in assistant_text
    assert "inspection-240a4e249fa66ab1a11b" not in assistant_text
    assert "77b31b51fc888e6d8bc71795200ec0f63ebaa96a42efeeb0799a2ae0d90a4f20" not in assistant_text
    assert scrubbed[0]["content"][0]["source"]["sha256"] == "a" * 64
    assert scrubbed[2]["content"][0]["tool_use_id"] == "inspect-1"
    assert result["keep"] == "human text"
    assert result["critic_feedback"] == {"completed": True, "body": "右下角稍拥挤。"}
    assert result["visual_source"] == {"path": "renders/slide_02.png", "transport_media_type": "image/png"}
    for key in (
        "critic_prompt_version",
        "critic_question",
        "inspection_id",
        "inspect_timings_s",
        "members",
        "model_config",
        "opened_not_verified",
        "opened_visual_sources",
        "owner_question",
        "page_uid",
        "png_sha256",
        "render_diagnostics",
        "source_refs",
    ):
        assert key not in result
    assert structured["tool_use_id"] == "inspect-2"
    assert "inspection_id" not in structured["content"]
    assert "tool_use_id" not in structured["content"]
    assert "page_uid" not in structured["content"]["nested"][0]
    assert structured["content"]["image"]["source"]["sha256"] == "c" * 64


def test_training_projection_removes_legacy_private_web_cache_paths():
    messages = [{
        "role": "user",
        "content": [{
            "type": "tool_result",
            "tool_use_id": "extract-1",
            "content": json.dumps({
                "url": "https://example.test/report",
                "content": "Evidence excerpt",
                "content_path": "_trace/web-extract/source.md",
                "full_body_path": "_trace/web-extract/source.full.md",
                "metadata_path": "_trace/web-extract/source.json",
                "history_note": "Continue at _trace/web-extract/source.full.md",
            }),
        }],
    }]

    scrubbed = _scrub_ephemeral_training_text(messages)
    result = json.loads(scrubbed[0]["content"][0]["content"])

    assert result["url"] == "https://example.test/report"
    assert result["content"] == "Evidence excerpt"
    assert not {"content_path", "full_body_path", "metadata_path"} & result.keys()
    assert "_trace/" not in json.dumps(result)
    assert result["history_note"] == "Continue at <private-runtime-path>"


def test_oversized_tool_result_is_preserved_with_budget_diagnostics_not_silently_truncated(tmp_path):
    from runtime.live_trace import LiveEventJournal
    from runtime.recovery import tool_boundary
    from runtime.session import estimate_request_tokens

    workspace = WorkspaceState(tmp_path)
    exporter = TrainingExporter(workspace, SimpleNamespace(), IssueLedger(tmp_path), run_mode="synthesis")
    live = LiveEventJournal(workspace.paths.trace / "live-events.jsonl")
    large_output = "long tool output\n" * 30000
    messages = (
        {"role": "user", "content": "read input"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "read-1", "name": "read_file", "input": {"path": "large.md"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "read-1", "content": large_output}]},
    )
    first = SegmentCheckpoint.initial(messages, segment_id="oversized")
    second = SegmentCheckpoint(segment_id="continuation", predecessor_segment_id=first.segment_id,
        messages=({"role": "user", "content": "actual compacted context"}, {"role": "assistant", "content": "done"}),
        provider_affinity_id="", excluded_channel_ids=(), reason="completed", invalid_signature_recoveries=0)
    system, tools = "the real system", [{"name": "read_file", "input_schema": {"type": "object"}}]
    for checkpoint in (first, second):
        live.append(event_type="agent_context", agent_id="owner", role="slide", segment_id=checkpoint.segment_id,
            payload={"system": system, "tools": tools, "messages": checkpoint.messages[:1],
                "model": "fixture", "training_sample_max_tokens": 131072, "max_tokens": 32768})
    segments = [exporter.capture("owner", "slide", checkpoint) for checkpoint in (first, second)]
    before = {path: path.read_bytes() for path in exporter.segment_dir.glob("*.json")}
    manifest = exporter.export(segments, SimpleNamespace(status="complete", outputs={}, path=""))
    rows = [json.loads(line) for line in exporter.rows_path.read_text().splitlines()]
    assert rows[0]["token_budget"]["status"] == "estimated_over_budget"
    assert rows[0]["token_budget"]["estimated_total_tokens"] == estimate_request_tokens(system, messages, tools)
    assert rows[1]["token_budget"]["status"] == "estimated_within_budget"
    assert rows[1]["predecessor_segment_id"] == first.segment_id
    assert manifest["token_budget"]["estimated_over_budget_segments"] == 1
    assert manifest["token_budget"]["exact_validation_required"]
    assert not manifest["token_budget"]["token_lengths_verified"]
    assert all(path.read_bytes() == data for path, data in before.items())
    captured = json.loads((tmp_path / rows[0]["checkpoint_path"]).read_text())
    assert captured["messages"] == list(messages) and tool_boundary(captured["messages"]).is_closed


@pytest.mark.parametrize("kind", ["critic", "content"])
def test_incomplete_qc_preserves_raw_as_pending_not_quality_rejection(tmp_path, kind):
    workspace = WorkspaceState(tmp_path)
    exporter = TrainingExporter(workspace, SimpleNamespace(), IssueLedger(tmp_path), run_mode="synthesis")
    checkpoint = SegmentCheckpoint(segment_id="s0", predecessor_segment_id=None,
        messages=({"role": "user", "content": "query"}, {"role": "assistant", "content": "done"}),
        provider_affinity_id="", excluded_channel_ids=(), reason="completed", invalid_signature_recoveries=0)
    manifest = exporter.export([TrainingSegmentInput("owner", "slide", checkpoint)],
        SimpleNamespace(status="complete", outputs={}, path=""),
        critic={"effective": True, "critic_incomplete": kind == "critic"},
        content_qc={"effective": True, "content_qc_incomplete": kind == "content"})
    assert manifest["training_admission"] == "pending"
    assert manifest["eligible_for_training"] is False
    row = json.loads(exporter.rows_path.read_text())
    assert row["training_admission"] == "pending" and not row["trainable"]
    assert (tmp_path / row["checkpoint_path"]).is_file()


@pytest.mark.parametrize("feedback", ["没有明显的可读性问题。", "右下角图例可能太小，建议核对。"])
def test_completed_critic_feedback_is_not_a_semantic_admission_gate(tmp_path, feedback):
    workspace = WorkspaceState(tmp_path)
    exporter = TrainingExporter(workspace, SimpleNamespace(), IssueLedger(tmp_path), run_mode="synthesis")
    messages = (
        {"role": "user", "content": "请检查当前页面。"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "inspect-1", "name": "inspect", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "inspect-1", "content": feedback}]},
        {"role": "assistant", "content": "核对后确认图例在当前尺寸可读，页面完成。"},
    )
    checkpoint = SegmentCheckpoint.initial(messages, segment_id="owner-segment")
    manifest = exporter.export([TrainingSegmentInput("owner", "slide", checkpoint)],
        SimpleNamespace(status="complete", outputs={}, path=""),
        critic={"effective": True, "critic_incomplete": False, "unresolved_valid_revise": True,
                "current_inspections": [{"inspection_id": "inspect-1", "completed": True, "body": feedback}]})
    assert manifest["training_admission"] == "accepted"
    assert manifest["rejection_reasons"] == []
    assert "不证明语义质量" in manifest["contract"]
    assert "unresolved_valid_revise" not in manifest["external_visual_critic"]
    row = json.loads(exporter.rows_path.read_text())
    assert row["trainable"] is True and "critic_annotation" not in row
    captured = json.loads((tmp_path / row["checkpoint_path"]).read_text())
    assert captured["messages"] == list(messages)
    assert "loss_mask" not in json.dumps(captured)


def test_explicit_non_model_transaction_is_preserved_but_not_trainable(tmp_path):
    workspace = WorkspaceState(tmp_path)
    exporter = TrainingExporter(workspace, SimpleNamespace(), IssueLedger(tmp_path), run_mode="synthesis")
    messages = (
        {"role": "user", "content": "input"},
        {"role": "assistant", "content": "done"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "synthetic", "name": "think", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "synthetic", "content": "feedback"}],
         "_critic_provenance": {"generated_by": "external_visual_critic", "tool_use_id": "synthetic"}},
    )
    checkpoint = SegmentCheckpoint.initial(messages, segment_id="non-model")
    manifest = exporter.export([TrainingSegmentInput("owner", "slide", checkpoint)],
        SimpleNamespace(status="complete", outputs={}, path=""))
    assert manifest["rejection_reasons"] == ["non_model_transaction=non-model"]
    row = json.loads(exporter.rows_path.read_text())
    assert row["trainable"] is False and "critic_annotation" not in row
    captured = json.loads((tmp_path / row["checkpoint_path"]).read_text())
    assert captured["messages"] == list(messages)
    assert "loss_mask" not in json.dumps(captured)


def test_critic_training_uses_independent_contexts_and_frozen_pngs_without_duplicate_capture(tmp_path):
    from runtime.live_trace import LiveEventJournal
    from runtime.session import ModelRequest

    workspace = WorkspaceState(tmp_path)
    exporter = TrainingExporter(workspace, ArtifactStore(tmp_path, workspace_state=workspace),
        IssueLedger(tmp_path), run_mode="synthesis")
    live = LiveEventJournal(workspace.paths.trace / "live-events.jsonl")
    rubric = "仅依据当前图像给出公开观察；不要推断用户意图。"
    for page, color in ((1, "blue"), (2, "red")):
        png = workspace.paths.page_png(page)
        png.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8) if page == 1 else (3000, 1500), color).save(png)
        identity = {"inspection_id": f"inspect-{page}", "page": page, "page_uid": f"page-{page}",
                    "html_sha256": "a" * 64, "png_sha256": file_sha256(png),
                    "owner_id": "slide-owner", "tool_use_id": f"tool-inspect-{page}"}
        initial = [{"role": "user", "content": [{"type": "image", "source": {
            "type": "base64", "media_type": "image/png", "data": base64.b64encode(png.read_bytes()).decode()}}]}]
        request = ModelRequest(model="same-owner-model", system=rubric, messages=tuple(initial))
        initial = list(request.messages)
        transmitted = base64.b64decode(initial[0]["content"][0]["source"]["data"])
        identity["image_sha256"] = hashlib.sha256(transmitted).hexdigest()
        owner_id, segment_id = f"critic-inspect-{page}", "independent-review"
        live.append(agent_id=owner_id, role="critic", segment_id=segment_id, event_type="agent_context",
            payload={"system": rubric, "tools": [], "model": "same-owner-model", "thinking": False,
                     "messages": initial, "metadata": identity})
        messages = initial + [{"role": "assistant", "content": [
            {"type": "thinking", "thinking": "private hidden reasoning", "signature": "test-signature"},
            {"type": "text", "text": f"第 {page} 张图的右侧标签建议核对。test-private-token"}]}]
        checkpoint = SegmentCheckpoint.initial(messages, segment_id=segment_id)
        exporter.capture(owner_id, "critic", checkpoint, artifact_paths=[f"renders/slide_{page:02d}.png"])
        exporter.capture(owner_id, "critic", checkpoint, artifact_paths=[f"renders/slide_{page:02d}.png"])

    segments = exporter.load_segments()
    assert len(segments) == 2
    manifest = exporter.export(segments, SimpleNamespace(status="complete", outputs={}, path=""),
        critic={"effective": True, "critic_incomplete": False})
    assert manifest["trainable_segment_count"] == 2
    rows = [json.loads(line) for line in exporter.rows_path.read_text().splitlines()]
    assert len({row["sample_id"] for row in rows}) == 2
    for row in rows:
        payload = json.loads((tmp_path / row["checkpoint_path"]).read_text())
        context, = payload["agent_context"]
        identity = context["metadata"]
        page = identity["page"]
        assert context["system"] == rubric and context["tools"] == []
        assert context["model"] == "same-owner-model" and context["thinking"] is False
        assert identity["inspection_id"] == f"inspect-{page}"
        assert row["role"] == "critic" and row["predecessor_segment_id"] is None
        assert len(payload["messages"]) == 2
        source = payload["messages"][0]["content"][0]["source"]
        assert source["type"] == "blob_ref" and source["sha256"] == identity["image_sha256"]
        assert file_sha256(tmp_path / source["path"]) == identity["image_sha256"]
        assert file_sha256(workspace.paths.page_png(page)) == identity["png_sha256"]
        assert row["artifact_hashes"][f"renders/slide_{page:02d}.png"] == identity["png_sha256"]
        if page == 2:
            assert identity["image_sha256"] != identity["png_sha256"]
            with Image.open(tmp_path / source["path"]) as sent_image:
                assert sent_image.size == (2000, 1000)
            with Image.open(workspace.paths.page_png(page)) as original_image:
                assert original_image.size == (3000, 1500)
        assert payload["messages"][1]["content"] == [
            {"type": "text", "text": f"第 {page} 张图的右侧标签建议核对。test-private-token"}]
        serialized = json.dumps(payload)
        assert all(value not in serialized for value in (
            "private hidden reasoning", "test-signature", "loss_mask", "critic_annotation"))


@pytest.mark.parametrize("missing", ["context", "system", "metadata", "png_hash", "image_hash", "image_hash_missing", "public_reply", "question_missing", "question_changed"])
def test_incomplete_critic_input_is_not_trainable_but_does_not_poison_owner_segment(tmp_path, missing):
    from runtime.live_trace import LiveEventJournal

    workspace = WorkspaceState(tmp_path)
    exporter = TrainingExporter(workspace, ArtifactStore(tmp_path, workspace_state=workspace),
        IssueLedger(tmp_path), run_mode="synthesis")
    png = workspace.paths.page_png(1)
    png.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), "blue").save(png)
    initial = [{"role": "user", "content": [{"type": "image", "source": {
        "type": "base64", "media_type": "image/png", "data": base64.b64encode(png.read_bytes()).decode()}}]}]
    if missing in {"question_missing", "question_changed"}:
        initial[0]["content"].append({"type": "text", "text": "本次检查的补充重点：标题是否受背景干扰"})
    context = {"system": "actual rubric", "tools": [], "model": "same-owner-model", "messages": initial,
        "metadata": {"inspection_id": "inspection", "page_uid": "page-1", "page": 1, "html_sha256": "a" * 64,
                     "png_sha256": file_sha256(png), "image_sha256": file_sha256(png),
                     "owner_id": "owner", "tool_use_id": "inspect-tool"}}
    if missing in {"system", "metadata"}:
        context.pop(missing)
    if missing == "png_hash":
        context["metadata"]["png_sha256"] = ""
    if missing == "image_hash":
        context["metadata"]["image_sha256"] = "b" * 64
    if missing == "image_hash_missing":
        context["metadata"].pop("image_sha256")
    if missing != "context":
        LiveEventJournal(workspace.paths.trace / "live-events.jsonl").append(
            agent_id="critic", role="critic", segment_id="critic-segment", event_type="agent_context", payload=context)
    if missing == "question_missing":
        initial[0]["content"].pop()
    elif missing == "question_changed":
        initial[0]["content"][1]["text"] = "这不是当时实际检查的问题"
    reply = "" if missing == "public_reply" else "建议核对右侧图例。"
    critic = TrainingSegmentInput("critic", "critic", SegmentCheckpoint.initial(
        initial + [{"role": "assistant", "content": reply}], segment_id="critic-segment"))
    owner = TrainingSegmentInput("owner", "slide", SegmentCheckpoint.initial(
        [{"role": "user", "content": "input"}, {"role": "assistant", "content": "done"}], segment_id="owner-segment"))
    manifest = exporter.export([owner, critic], SimpleNamespace(status="complete", outputs={}, path=""),
        critic={"effective": True, "critic_incomplete": False})
    assert manifest["trainable_segment_count"] == 1
    rows = {row["role"]: row for row in map(json.loads, exporter.rows_path.read_text().splitlines())}
    assert rows["slide"]["trainable"] is True
    assert rows["critic"]["trainable"] is False
    assert rows["critic"]["segment_rejection_reasons"] == ["critic_input_incomplete=critic-segment"]


def test_workspace_policy_accepts_generated_delivery_directory(
    tmp_path: Path,
) -> None:
    policy_path = (
        Path(__file__).resolve().parents[3]
        / "skills"
        / "mural-presenter-v1"
        / "scripts"
        / "_internal"
        / "workspace_policy.py"
    )
    audit_workspace = runpy.run_path(str(policy_path))["audit_workspace"]
    (tmp_path / "delivery").mkdir()
    (tmp_path / "delivery" / "presentation.pdf").write_bytes(b"pdf")

    (tmp_path / "font-intent.json").write_text('{"schema":"mural.font-intent.v1"}')
    assert audit_workspace(tmp_path) == []
    (tmp_path / "unknown.json").write_text("{}")
    assert audit_workspace(tmp_path) == ["root output is outside the allowlist: unknown.json"]


def _write_png(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1600, 1200), color).save(path)


def _ready_workspace(
    root: Path, *, pages: int = 2
) -> tuple[WorkspaceState, ArtifactStore, IssueLedger]:
    # layout v2: the manifest must be the first persistent file.
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
    workspace.register_pages(range(1, pages + 1))
    for page in range(1, pages + 1):
        html_path = workspace.paths.page_html(page)
        html_path.write_text(
            "<!doctype html><html><head>"
            '<link rel="stylesheet" href="../base.css">'
            f"</head><body><main class=\"mural-slide\">P{page:02d}</main></body></html>",
            encoding="utf-8",
        )
        workspace.register_scaffold(page)
        artifact_id = f"page-html-{page:02d}"
        artifacts.declare(
            artifact_id=artifact_id,
            kind="page_html",
            path=html_path,
            owner_id="owner-slide",
            page=page,
        )
        artifacts.commit(
            owner_id="owner-slide",
            artifact_ids=[artifact_id],
            source_hashes={},
        )

    def renderer(_html: Path, png: Path, _canvas: dict):
        page = int(png.stem.rsplit("_", 1)[1])
        _write_png(png, (20 * page, 70, 140))
        return {"geometry": _canvas, "loaded_resources": [], "failed_resources": []}

    artifacts.render_pages(range(1, pages + 1), renderer)
    for page in range(1, pages + 1):
        artifacts.record_inspection(page, inspection_id=f"inspection-{page}")
        workspace.mark_ready(page)
    return workspace, artifacts, issues


def test_delivery_exports_all_formats_with_one_canvas(tmp_path: Path, monkeypatch, pinned_font_sources, browser_environment) -> None:
    monkeypatch.setenv('MURAL_STUDIO_DATA_DIR', pinned_font_sources['studio_data_root'])
    workspace, artifacts, issues = _ready_workspace(tmp_path)
    _pin_font_manifest(tmp_path, pinned_font_sources)
    font_path = tmp_path / "assets" / "font.woff2"
    font_path.write_bytes(b"font")
    speech_path = tmp_path / "speech.md"
    speech_path.write_text("讲稿", encoding="utf-8")
    restricted = [
        workspace.paths.page_html(1),
        workspace.paths.page_png(1),
        workspace.paths.root / "base.css",
        font_path,
        speech_path,
    ]
    for path in restricted:
        path.chmod(0o600)
    exporter = DeliveryExporter(workspace, artifacts, issues)

    manifest = exporter.export(title="测试演示")

    assert manifest.status == "complete"
    assert manifest.canvas["width_px"] == 1600
    assert set(manifest.outputs) == {
        "contact_sheet",
        "pdf",
        "pptx",
        "present_html",
        "portable_zip",
    }
    assert (tmp_path / "present.html").is_file()
    assert (tmp_path / "result.json").is_file()
    assert "--canvas-w: 1600px" in (tmp_path / "present.html").read_text(encoding="utf-8")

    presentation = Presentation(tmp_path / "delivery" / "presentation.pptx")
    assert len(presentation.slides) == 2
    assert presentation.slide_width == Inches(1600 / 96)
    assert presentation.slide_height == Inches(1200 / 96)
    assert all(path.stat().st_mode & 0o004 for path in restricted)
    assert all(
        path.stat().st_mode & 0o001
        for path in (tmp_path, tmp_path / "assets", tmp_path / "slides", tmp_path / "renders")
    )
    exporter.verify(manifest)


def test_portable_zip_is_closed_and_excludes_internal_state(tmp_path: Path) -> None:
    import zipfile

    workspace, artifacts, issues = _ready_workspace(tmp_path, pages=1)
    exporter = DeliveryExporter(workspace, artifacts, issues)
    manifest = exporter.export(include_pdf=False, include_pptx=False, title="ZIP")

    zip_path = tmp_path / "delivery" / "presentation.zip"
    assert zip_path.is_file()
    with zipfile.ZipFile(zip_path, "r") as archive:
        names = set(archive.namelist())
    # whitelist: only present/base/speech + slides/*.html + assets/* + package manifest.
    assert "present.html" in names
    assert "base.css" in names
    assert "package-manifest.json" in names
    assert any(name.startswith("slides/") and name.endswith(".html") for name in names)
    # internal state, inputs, traces and source briefs must never enter the ZIP.
    for name in names:
        assert not name.startswith("_state/")
        assert not name.startswith("_trace/")
        assert not name.startswith("_runtime/")
        assert not name.startswith("inputs/")
        assert not name.startswith("briefs/")
        assert name not in {"canvas.json", "deck.md", "design-brief.md", "material.md", "research.md"}
        assert not name.startswith("delivery/")
        assert "assets/catalog.md" != name
    # output record carries consistent package + font provenance hashes.
    record = manifest.outputs["portable_zip"]
    assert record["package_manifest_sha256"]
    exporter.verify(manifest)


def test_delivery_refreshes_contact_sheets_from_final_pixels(tmp_path, monkeypatch):
    from runtime.shared_objects import SharedSkillStore

    monkeypatch.setenv("MURAL_STUDIO_DATA_DIR", str(tmp_path / "store"))
    workspace, artifacts, issues = _ready_workspace(tmp_path / "deck")
    root = workspace.paths.root
    skill = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1"
    SharedSkillStore.for_workspace(root).pin(workspace.paths, skill, skill_name=skill.name)
    html = workspace.paths.page_html(1)
    html.write_text(html.read_text().replace('<main class=', '<main data-page-type="cover" class='))
    artifacts.commit(owner_id="owner-slide", artifact_ids=["page-html-01"], source_hashes={})

    def render(_html, png, canvas):
        _write_png(png, (20, 70, 140))
        return {"geometry": canvas, "loaded_resources": [], "failed_resources": []}

    artifacts.render_pages([1], render)
    artifacts.record_inspection(1, inspection_id="cover-current")
    workspace.mark_ready(1)
    unchanged = {path: path.read_bytes() for path in [root / "base.css", html,
                 workspace.paths.page_png(1), workspace.paths.page_png(2)]}
    sheet = root / "renders/contact-sheet.png"
    special = root / "renders/contact-sheet-special.png"
    Image.new("RGB", (10, 10), "magenta").save(sheet)
    exporter = DeliveryExporter(workspace, artifacts, issues)
    first = exporter.export(include_pdf=False, include_pptx=False, include_zip=False)
    with Image.open(sheet) as image:
        assert image.size == (800, 322)
        assert image.getpixel((100, 100)) == (20, 70, 140)
        assert image.getpixel((500, 100)) == (40, 70, 140)
    assert not special.exists()
    assert {path: path.read_bytes() for path in unchanged} == unchanged
    assert first.outputs["contact_sheet"]["sha256"] == file_sha256(sheet)
    assert "special_contact_sheet" not in first.outputs

    html.write_text(html.read_text().replace('data-page-type="cover"', 'data-page-type="content"')
                    .replace("P01", "Revised P01"))
    artifacts.commit(owner_id="owner-slide", artifact_ids=["page-html-01"], source_hashes={})
    def revised_render(_html, png, canvas):
        _write_png(png, (200, 70, 140))
        return {"geometry": canvas, "loaded_resources": [], "failed_resources": []}
    artifacts.render_pages([1], revised_render)
    artifacts.record_inspection(1, inspection_id="revised-current")
    workspace.mark_ready(1)
    second = exporter.export(include_pdf=False, include_pptx=False, include_zip=False)
    assert not special.exists()
    assert "special_contact_sheet" not in second.outputs
    assert second.outputs["contact_sheet"]["sha256"] != first.outputs["contact_sheet"]["sha256"]
    with Image.open(sheet) as image:
        assert image.getpixel((100, 100)) == (200, 70, 140)
    exporter.verify(second)
    Image.new("RGB", (10, 10), "magenta").save(sheet)
    with pytest.raises(DeliveryError, match="contact_sheet"):
        exporter.verify(second)


def test_pinned_skill_zip_renders_fragments_fonts_and_navigation(tmp_path, monkeypatch):
    import functools
    import http.server
    import os
    import sys
    import threading
    import zipfile
    from playwright.sync_api import sync_playwright
    from runtime.shared_objects import SharedSkillStore
    from test_v1_font_pipeline import font_bundle, REPO, RELEASE_FONT_DIR
    sys.path.insert(0, str(REPO / "webui/studio"))
    from app.engine import render_env
    monkeypatch.setenv("MURAL_STUDIO_DATA_DIR", str(tmp_path.parent / "portable-shared-store"))
    monkeypatch.setenv("PATH", f'{Path(sys.executable).parent}:{os.environ.get("PATH", "")}')
    monkeypatch.setattr(font_bundle, "_font_source_dirs", lambda: [RELEASE_FONT_DIR])
    workspace, artifacts, issues = _ready_workspace(tmp_path)
    SharedSkillStore.for_workspace(tmp_path).pin(workspace.paths, REPO / "skills/mural-presenter-v1", skill_name="mural-presenter-v1")
    fragments = []
    for page in (1, 2):
        html = f'<section class="slide" data-slide="{page}"><h1>Page {page} — café 中文</h1></section>'
        fragments.append(html)
        workspace.paths.page_html(page).write_text(html, encoding="utf-8")
        artifacts.commit(owner_id="owner-slide", artifact_ids=[f"page-html-{page:02d}"], source_hashes={})
    css = (':root{--canvas-w:1600px;--canvas-h:1200px;--font-body:"Manrope",sans-serif}html,body{margin:0}'
           '.deck{position:absolute;width:1600px;height:1200px}'
           '.slide{display:none;width:1600px;height:1200px;background:#123456;color:white}'
           '.slide.active{display:block}h1{margin:0;font:48px var(--font-body)}')
    (tmp_path / "base.css").write_text(font_bundle.bundle_fonts(tmp_path, css, fragments), encoding="utf-8")
    (tmp_path / "present.html").write_text("STALE PRESENT MUST NOT BE REUSED")
    def renderer(_html, png, _canvas):
        _write_png(png, (18, 52, 86))
        return {"geometry": _canvas, "loaded_resources": [], "failed_resources": []}
    artifacts.render_pages([1, 2], renderer)
    for page in (1, 2):
        artifacts.record_inspection(page, inspection_id=f"portable-{page}")
        workspace.mark_ready(page)
    source_hashes = [file_sha256(workspace.paths.page_html(p)) for p in (1, 2)]
    css_hash = file_sha256(tmp_path / "base.css")
    manifest = DeliveryExporter(workspace, artifacts, issues).export(include_pdf=False, include_pptx=False)
    assert manifest.status == "complete"
    assert source_hashes == [file_sha256(workspace.paths.page_html(p)) for p in (1, 2)]
    assert css_hash == file_sha256(tmp_path / "base.css")
    package = tmp_path.parent / "portable-browser-package"
    with zipfile.ZipFile(tmp_path / "delivery/presentation.zip") as archive:
        archive.extractall(package)  # exporter.verify 已检查 ZIP 路径与文件 hash。
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(package)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    env = render_env({**os.environ, "TMPDIR": str(tmp_path)})
    failures, external = [], []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=env["PPT_SKILL_BROWSER_EXE"], env=env, headless=True, args=["--no-sandbox"])
            try:
                page = browser.new_page(viewport={"width": 1600, "height": 1200})
                def route(request):
                    if request.request.url.startswith(base + "/"):
                        request.continue_()
                    else:
                        external.append(request.request.url)
                        request.abort()
                page.route("**/*", route)
                page.on("requestfailed", lambda request: failures.append(request.url))
                page.goto(base + "/present.html?slide=2", wait_until="networkidle")
                page.evaluate("document.fonts.ready")
                assert page.locator(".slide.active h1").inner_text() == "Page 2 — café 中文"
                assert page.evaluate("document.documentElement.lang") == "en"
                assert page.locator(".slide.active").evaluate("e=>getComputedStyle(e).backgroundColor") == "rgb(18, 52, 86)"
                cdp = page.context.new_cdp_session(page)
                document = cdp.send("DOM.getDocument")
                node = cdp.send("DOM.querySelector", {"nodeId": document["root"]["nodeId"], "selector": ".slide.active h1"})
                cdp.send("DOM.enable")
                cdp.send("CSS.enable")
                fonts = cdp.send("CSS.getPlatformFontsForNode", {"nodeId": node["nodeId"]})["fonts"]
                visible_fonts = [font for font in fonts if font["glyphCount"] > 0]
                assert visible_fonts and all(font["isCustomFont"] for font in visible_fonts)
                page.keyboard.press("ArrowLeft")
                assert page.locator(".slide.active h1").inner_text() == "Page 1 — café 中文"
                assert not failures and not external
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()


def test_unpinned_fragment_delivery_fails_instead_of_publishing_unstyled_frames(tmp_path):
    workspace, artifacts, issues = _ready_workspace(tmp_path, pages=1)
    from export.delivery import _present_html
    workspace.paths.page_html(1).write_text('<section class="slide" data-slide="1">Fragment</section>')
    actual = workspace.require_geometry()
    workspace.paths.skill_ref.unlink()
    with pytest.raises(DeliveryError, match="pinned Skill assembler"):
        _present_html(workspace.paths, actual, workspace.pages(), title="Test", language="en")


def test_pinned_fragment_delivery_normalizes_parent_asset_urls(tmp_path, monkeypatch):
    from export.delivery import _present_html
    from runtime.shared_objects import SharedSkillStore

    repo = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("MURAL_STUDIO_DATA_DIR", str(tmp_path.parent / "shared-store"))
    workspace, _artifacts, _issues = _ready_workspace(tmp_path, pages=1)
    SharedSkillStore.for_workspace(tmp_path).pin(
        workspace.paths,
        repo / "skills/mural-presenter-v1",
        skill_name="mural-presenter-v1",
    )
    workspace.paths.page_html(1).write_text(
        '<section class="slide" data-slide="1"><style>'
        '.hero{background-image:url("../assets/hero.jpg")}'
        '</style><div class="hero"></div></section>',
        encoding="utf-8",
    )

    document = _present_html(
        workspace.paths,
        workspace.require_geometry(),
        workspace.pages(),
        title="Test",
        language="en",
    )

    assert 'url("assets/hero.jpg")' in document
    assert "../assets/hero.jpg" not in document


def test_portable_zip_export_failure_does_not_replace_existing(tmp_path: Path) -> None:
    workspace, artifacts, issues = _ready_workspace(tmp_path, pages=1)
    exporter = DeliveryExporter(workspace, artifacts, issues)
    manifest = exporter.export(include_pdf=False, include_pptx=False, title="ZIP")
    zip_path = tmp_path / "delivery" / "presentation.zip"
    good_bytes = zip_path.read_bytes()

    # Removing the author base.css must fail-close the next export and leave the
    # previously delivered ZIP untouched.
    (tmp_path / "base.css").unlink()
    with pytest.raises(DeliveryError, match="base.css"):
        exporter.export(include_pdf=False, include_pptx=False, title="ZIP")
    assert zip_path.read_bytes() == good_bytes


def test_delivery_detects_page_change_after_manifest(tmp_path: Path) -> None:
    workspace, artifacts, issues = _ready_workspace(tmp_path, pages=1)
    exporter = DeliveryExporter(workspace, artifacts, issues)
    manifest = exporter.export(include_pdf=False, include_pptx=False)

    workspace.paths.page_html(1).write_text(
        "<html><body>changed after delivery</body></html>", encoding="utf-8"
    )

    try:
        exporter.verify(manifest)
    except DeliveryError as exc:
        assert "no longer ready" in str(exc) or "hash changed" in str(exc)
    else:
        raise AssertionError("changed page unexpectedly passed delivery verification")


def test_delivery_rejects_shared_visual_resource_changed_after_render(
    tmp_path: Path,
) -> None:
    workspace, artifacts, issues = _ready_workspace(tmp_path, pages=1)
    (tmp_path / "base.css").write_text(
        ":root{--canvas-w:999px;--canvas-h:1200px}", encoding="utf-8"
    )

    try:
        DeliveryExporter(workspace, artifacts, issues).export(
            include_pdf=False, include_pptx=False
        )
    except DeliveryError as exc:
        assert "page 1 canvas mismatch" in str(exc) and "999" in str(exc) and "1600" in str(exc)
    else:
        raise AssertionError("changed shared CSS unexpectedly passed delivery")


@pytest.mark.parametrize("resource", ["picture.png", "theme.css"])
@pytest.mark.parametrize("publish_result", [True, False])
def test_export_rechecks_dependencies_before_publishing_success(tmp_path, monkeypatch, resource, publish_result):
    from export import delivery
    from test_workspace_artifacts import _inspected_render_fixture
    workspace, store, _, _ = _inspected_render_fixture(tmp_path)
    workspace.mark_ready(1)
    exporter = DeliveryExporter(workspace, store, IssueLedger(tmp_path))
    options = dict(include_pdf=False, include_pptx=True, include_zip=False)
    exporter.export(**options)
    previous = {path: path.read_bytes() for path in (exporter.manifest_path, exporter.result_path)}
    source_hashes = (workspace.page(1).html_sha256, workspace.page(1).png_sha256)
    sync = delivery._sync_contact_sheets
    def replace_after_render(*args):
        result = sync(*args)
        path = workspace.paths.assets / resource
        if resource.endswith(".png"):
            Image.new("RGB", (24, 16), "red").save(path)
        else:
            path.write_text("main {color: red}")
        return result
    monkeypatch.setattr(delivery, "_sync_contact_sheets", replace_after_render)
    with pytest.raises(DeliveryError, match="visual resources changed after render"):
        exporter.export(**options, publish_result=publish_result)
    assert {path: path.read_bytes() for path in previous} == previous
    assert (workspace.page(1).html_sha256, workspace.page(1).png_sha256) == source_hashes


@pytest.mark.parametrize("defect", ["image", "css", "font_bytes", "font_authorization"])
def test_verify_and_delayed_result_recheck_current_resources(tmp_path, defect):
    from runtime.shared_objects import SharedSkillStore
    from test_workspace_artifacts import _inspected_render_fixture
    workspace, store, renderer, _ = _inspected_render_fixture(tmp_path)
    if defect.startswith("font_"):
        shared = SharedSkillStore.for_workspace(tmp_path).studio_data
        data = b"delivery-font-fixture"
        digest = hashlib.sha256(data).hexdigest()
        font = shared / "shared/fonts/objects" / f"{digest}.ttf"
        font.parent.mkdir(parents=True, exist_ok=True)
        font.write_bytes(data)
        font_manifest = {"schema": "mural.font-manifest.v2", "studio_data_root": str(shared),
            "source_faces": [{"sha256": digest, "object_ref": font.relative_to(shared).as_posix(), "scope": "public"}]}
        workspace.paths.font_manifest.write_text(json.dumps(font_manifest))
        css = tmp_path / "base.css"
        css.write_text(css.read_text() + f'\n@font-face{{font-family:"fixture";src:url("{font.as_uri()}")}}')
        store.render_pages([1], renderer)
        store.record_inspection(1, inspection_id="font-inspection")
    workspace.mark_ready(1)
    exporter = DeliveryExporter(workspace, store, IssueLedger(tmp_path))
    manifest = exporter.export(include_pdf=False, include_pptx=False, include_zip=False, publish_result=False)
    if defect == "image":
        Image.new("RGB", (24, 16), "red").save(workspace.paths.assets / "picture.png")
    elif defect == "css":
        (workspace.paths.assets / "theme.css").write_text("main {color: red}")
    elif defect == "font_bytes":
        font.write_bytes(b"x" * len(data))
    else:
        font_manifest["source_faces"] = []
        workspace.paths.font_manifest.write_text(json.dumps(font_manifest))
    for publish in (exporter.verify, exporter.write_result):
        with pytest.raises(DeliveryError, match="visual resources changed|hash mismatch|not pinned"):
            publish(manifest)
    assert not exporter.result_path.exists()


def test_immediate_result_reuses_final_resource_verification(tmp_path, monkeypatch):
    from test_workspace_artifacts import _inspected_render_fixture
    workspace, store, _, _ = _inspected_render_fixture(tmp_path)
    workspace.mark_ready(1)
    exporter = DeliveryExporter(workspace, store, IssueLedger(tmp_path))
    validate, checked = store.validate_render_receipt, []
    def counted(page, **kwargs):
        checked.append(page)
        return validate(page, **kwargs)
    monkeypatch.setattr(store, "validate_render_receipt", counted)
    manifest = exporter.export(include_pdf=False, include_pptx=False, include_zip=False)
    assert checked == [1, 1]
    assert json.loads(exporter.result_path.read_text())["status"] == "completed"
    exporter.write_result(manifest)
    assert checked == [1, 1, 1]


def test_training_export_externalizes_images_and_accepts_valid_lineage(
    tmp_path: Path,
) -> None:
    workspace, artifacts, issues = _ready_workspace(tmp_path, pages=1)
    delivery = DeliveryExporter(workspace, artifacts, issues).export(
        include_pdf=False, include_pptx=False
    )
    image_bytes = workspace.paths.page_png(1).read_bytes()
    encoded = base64.b64encode(image_bytes).decode("ascii")
    first = SegmentCheckpoint(
        segment_id="segment-1",
        predecessor_segment_id=None,
        messages=(
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": encoded,
                        },
                    }
                ],
            },
        ),
    )
    second = SegmentCheckpoint(
        segment_id="segment-2",
        predecessor_segment_id="segment-1",
        messages=({"role": "user", "content": "继续完成页面。"},),
        reason="context_compaction",
    )
    artifact_hash = artifacts.artifact("page-html-01").sha256
    inputs = [
        TrainingSegmentInput(
            owner_id="owner-slide",
            role="slide",
            checkpoint=first,
            artifact_hashes={"page-html-01": artifact_hash},
            image_paths=("renders/slide_01.png",),
        ),
        TrainingSegmentInput(
            owner_id="owner-slide",
            role="slide",
            checkpoint=second,
            artifact_hashes={"page-html-01": artifact_hash},
        ),
    ]

    manifest = TrainingExporter(
        workspace, artifacts, issues, run_mode=RunMode.SYNTHESIS
    ).export(inputs, delivery)

    assert manifest["training_admission"] == "accepted"
    rows = [
        json.loads(line)
        for line in (
            tmp_path / "_trace" / "training" / "training-segments.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    assert rows[0]["predecessor_segment_id"] is None
    assert rows[1]["predecessor_segment_id"] == "segment-1"
    checkpoint = json.loads(
        (tmp_path / rows[0]["checkpoint_path"]).read_text(encoding="utf-8")
    )
    assert encoded not in json.dumps(checkpoint)
    assert checkpoint["messages"][0]["content"][0]["source"]["type"] == "blob_ref"
    assert checkpoint["image_blobs"]


def test_training_captures_boundary_versions_and_resumes_all_segments(tmp_path):
    workspace, artifacts, issues = _ready_workspace(tmp_path, pages=1)
    delivery = DeliveryExporter(workspace, artifacts, issues).export(include_pdf=False, include_pptx=False)
    exporter = TrainingExporter(workspace, artifacts, issues, run_mode="synthesis")
    source = tmp_path / "material.md"
    source.write_text("version one")
    encoded = base64.b64encode(workspace.paths.page_png(1).read_bytes()).decode()
    first = SegmentCheckpoint.initial([{"role": "user", "content": [{"type": "image", "source": {
        "type": "base64", "media_type": "image/png", "data": encoded}}]}], segment_id="first")
    captured = exporter.capture("owner", "material", first, artifact_paths=["material.md"])
    first_hash = captured.artifact_hashes["material.md"]
    source.write_text("version two")
    second = SegmentCheckpoint("second", "first", ({"role": "assistant", "content": "done"},),
        reason="provider_failover", invalid_signature_recoveries=1)
    exporter.capture("owner", "material", second, artifact_paths=["material.md"])
    exporter.capture("owner", "material", second, artifact_paths=["material.md"])
    source.write_text("final shared file after owner completed")
    resumed = TrainingExporter(workspace, artifacts, issues, run_mode="synthesis")
    segments = resumed.load_segments()
    assert [segment.checkpoint.segment_id for segment in segments] == ["first", "second"]
    assert segments[0].artifact_hashes["material.md"] == first_hash
    assert segments[1].artifact_hashes["material.md"] != first_hash
    assert segments[1].checkpoint.invalid_signature_recoveries == 1
    assert encoded not in json.dumps(segments[0].checkpoint.messages)
    manifest = resumed.export(segments, delivery)
    assert manifest["training_admission"] == "accepted"
    rows = [json.loads(line) for line in resumed.rows_path.read_text().splitlines()]
    assert all(row["artifact_validation"]["material.md"] == "checkpoint_snapshot" for row in rows)
    assert rows[0]["image_blobs"] and rows[1]["predecessor_segment_id"] == "first"


@pytest.mark.parametrize("damage", ["missing", "changed", "escape"])
def test_training_rejects_bad_snapshot_even_when_current_file_matches(tmp_path, damage):
    workspace, artifacts, issues = _ready_workspace(tmp_path, pages=1)
    delivery = DeliveryExporter(workspace, artifacts, issues).export(include_pdf=False, include_pptx=False)
    exporter = TrainingExporter(workspace, artifacts, issues, run_mode="synthesis")
    (tmp_path / "material.md").write_text("unchanged original")
    checkpoint = SegmentCheckpoint.initial([{"role": "user", "content": "input"}])
    segment = exporter.capture("owner", "material", checkpoint, artifact_paths=["material.md"])
    blob = segment.artifact_blobs["material.md"]
    if damage == "missing":
        (tmp_path / blob["path"]).unlink()
    elif damage == "changed":
        (tmp_path / blob["path"]).write_text("corrupt")
    else:
        blob["path"] = "material.md"
    manifest = exporter.export([segment], delivery)
    assert manifest["training_admission"] == "rejected"
    assert "artifact_snapshot_invalid=material.md" in manifest["rejection_reasons"]


def test_capture_rejects_open_transaction_and_resume_rejects_missing_predecessor(tmp_path):
    workspace = WorkspaceState(tmp_path)
    exporter = TrainingExporter(workspace, ArtifactStore(tmp_path, workspace_state=workspace),
        IssueLedger(tmp_path), run_mode="synthesis")
    open_checkpoint = SegmentCheckpoint.initial([{"role": "assistant", "content": [
        {"type": "tool_use", "id": "unfinished", "name": "write_file", "input": {}}]}])
    with pytest.raises(TrainingExportError, match="open tool transaction"):
        exporter.capture("owner", "slide", open_checkpoint)
    exporter.capture("owner", "slide", SegmentCheckpoint("next", "missing", ()))
    with pytest.raises(TrainingExportError, match="lineage"):
        exporter.load_segments()


def test_training_keeps_visible_text_without_native_protocol_signatures(tmp_path):
    workspace = WorkspaceState(tmp_path)
    exporter = TrainingExporter(workspace, ArtifactStore(tmp_path, workspace_state=workspace),
        IssueLedger(tmp_path), run_mode="synthesis")
    messages = ({"role": "assistant", "content": [
        {"type": "thinking", "thinking": "private reasoning", "signature": "private-signature"},
        {"type": "text", "text": "public opaque-test-credential"}]},)
    checkpoint = SegmentCheckpoint.initial(messages)
    exporter.capture("owner", "slide", checkpoint)
    projected = next(exporter.segment_dir.glob("*.json")).read_text()
    assert all(value not in projected for value in ("private reasoning", "private-signature", '"signature"'))
    assert "public opaque-test-credential" in projected
    assert checkpoint.messages == messages


def test_training_preserves_tool_fields_that_look_like_auth_or_protocol(tmp_path):
    workspace = WorkspaceState(tmp_path)
    exporter = TrainingExporter(workspace, ArtifactStore(tmp_path, workspace_state=workspace),
        IssueLedger(tmp_path), run_mode="synthesis")
    arguments = {"signature": "author signature", "api_key": "example", "source": {"password": "example"},
                 "content": "--font-body: Inter; EMPTY; Bearer example; reasoning_content=example"}
    messages = [{"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "example", "input": arguments}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": json.dumps(arguments)}]}]
    projected, blobs = exporter._externalize_images(messages)
    assert projected == messages and blobs == []


def test_training_accepts_checkpoint_attested_artifact_superseded_by_repair(
    tmp_path: Path,
) -> None:
    workspace, artifacts, issues = _ready_workspace(tmp_path, pages=1)
    html_path = workspace.paths.page_html(1)
    first_hash = artifacts.artifact("page-html-01").sha256
    first = SegmentCheckpoint(
        segment_id="segment-before-repair",
        predecessor_segment_id=None,
        messages=(
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "write-before-repair",
                        "name": "write_file",
                        "input": {"path": "slides/slide_01.html"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "write-before-repair",
                        "content": json.dumps(
                            {
                                "path": "slides/slide_01.html",
                                "sha256": first_hash,
                            }
                        ),
                    }
                ],
            },
        ),
    )

    html_path.write_text(
        html_path.read_text(encoding="utf-8").replace("P01", "P01 repaired"),
        encoding="utf-8",
    )
    artifacts.commit(
        owner_id="owner-slide",
        artifact_ids=["page-html-01"],
        source_hashes={},
    )

    def renderer(_html: Path, png: Path, _canvas: dict):
        _write_png(png, (80, 90, 160))
        return {"geometry": _canvas, "loaded_resources": [], "failed_resources": []}

    artifacts.render_pages([1], renderer)
    artifacts.record_inspection(1, inspection_id="inspection-repaired")
    workspace.mark_ready(1)
    final_hash = artifacts.artifact("page-html-01").sha256
    repaired = SegmentCheckpoint(
        segment_id="segment-after-repair",
        predecessor_segment_id=None,
        messages=({"role": "user", "content": "repair completed"},),
    )
    delivery = DeliveryExporter(workspace, artifacts, issues).export(
        include_pdf=False, include_pptx=False
    )

    manifest = TrainingExporter(
        workspace, artifacts, issues, run_mode=RunMode.SYNTHESIS
    ).export(
        [
            TrainingSegmentInput(
                "owner-slide",
                "slide",
                first,
                artifact_hashes={"page-html-01": first_hash},
            ),
            TrainingSegmentInput(
                "owner-slide",
                "slide",
                repaired,
                artifact_hashes={"page-html-01": final_hash},
            ),
        ],
        delivery,
    )

    assert manifest["training_admission"] == "accepted"
    rows = [
        json.loads(line)
        for line in (
            tmp_path / "_trace" / "training" / "training-segments.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    assert rows[0]["artifact_validation"]["page-html-01"] == (
        "checkpoint_attested_revision"
    )
    assert rows[1]["artifact_validation"]["page-html-01"] == "current"


def test_training_rejects_unattested_artifact_hash_mismatch(tmp_path: Path) -> None:
    workspace, artifacts, issues = _ready_workspace(tmp_path, pages=1)
    delivery = DeliveryExporter(workspace, artifacts, issues).export(
        include_pdf=False, include_pptx=False
    )
    checkpoint = SegmentCheckpoint.initial(
        ({"role": "user", "content": "no artifact receipt"},),
        segment_id="segment-unattested",
    )

    manifest = TrainingExporter(
        workspace, artifacts, issues, run_mode=RunMode.SYNTHESIS
    ).export(
        [
            TrainingSegmentInput(
                "owner-slide",
                "slide",
                checkpoint,
                artifact_hashes={"page-html-01": "0" * 64},
            )
        ],
        delivery,
    )

    assert manifest["training_admission"] == "rejected"
    assert "artifact_hash_mismatch=page-html-01" in manifest["rejection_reasons"]


def test_training_rejects_forward_lineage_open_tools_and_blocking_major(
    tmp_path: Path,
) -> None:
    workspace, artifacts, issues = _ready_workspace(tmp_path, pages=1)
    delivery = DeliveryExporter(workspace, artifacts, issues).export(
        include_pdf=False, include_pptx=False
    )
    candidate = VisualCandidate(
        candidate_id="candidate-blocking",
        kind="projection_readability",
        severity="major",
        page=1,
        summary="投影可读性仍未确认。",
        owner_hint="slide",
        objective=True,
    )
    issues.record_visual_reviews(
        [
            VisualQC.review_candidates(
                [candidate], lambda _item: ReviewVerdict.UNRESOLVED
            )[0]
        ]
    )
    forward = SegmentCheckpoint(
        segment_id="segment-forward",
        predecessor_segment_id="segment-future",
        messages=(
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "open", "name": "render"}],
            },
        ),
    )
    future = SegmentCheckpoint(
        segment_id="segment-future",
        predecessor_segment_id=None,
        messages=({"role": "user", "content": "future"},),
    )

    manifest = TrainingExporter(
        workspace, artifacts, issues, run_mode=RunMode.SYNTHESIS
    ).export(
        [
            TrainingSegmentInput("owner-slide", "slide", forward),
            TrainingSegmentInput("owner-slide", "slide", future),
        ],
        delivery,
    )

    assert manifest["training_admission"] == "rejected"
    reasons = "\n".join(manifest["rejection_reasons"])
    assert "lineage_first_predecessor_not_null" in reasons
    assert "lineage_invalid" in reasons
    assert "open_tool_transaction" in reasons
    assert "blocking_issue" in reasons


def test_inference_training_admission_is_not_applicable(tmp_path: Path) -> None:
    workspace, artifacts, issues = _ready_workspace(tmp_path, pages=1)
    delivery = DeliveryExporter(workspace, artifacts, issues).export(
        include_pdf=False, include_pptx=False
    )
    checkpoint = SegmentCheckpoint.initial(
        ({"role": "user", "content": "diagnostic"},),
        segment_id="segment-inference",
    )

    manifest = TrainingExporter(
        workspace, artifacts, issues, run_mode=RunMode.INFERENCE
    ).export(
        [TrainingSegmentInput("owner-slide", "slide", checkpoint)], delivery
    )

    assert manifest["training_admission"] == "not_applicable"
    assert manifest["eligible_for_training"] is False


@pytest.mark.parametrize("tool_rounds", [64, 111, 256], ids=["129-messages", "223-messages", "513-messages"])
def test_long_segments_export_without_truncation_or_artificial_boundaries(tmp_path: Path, tool_rounds: int) -> None:
    workspace = WorkspaceState(tmp_path)
    exporter = TrainingExporter(workspace, ArtifactStore(tmp_path, workspace_state=workspace),
        IssueLedger(tmp_path), run_mode=RunMode.SYNTHESIS)
    messages = [{"role": "user", "content": "Keep the current layout; only update the data."}]
    for index in range(tool_rounds):
        messages.extend([
            {"role": "assistant", "content": [{"type": "tool_use", "id": f"read-{index}",
                "name": "read_file", "input": {"path": "knowledge.md"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"read-{index}",
                "content": f"Evidence at round {index}"}]},
        ])
    first = SegmentCheckpoint.initial(messages, segment_id="segment-long")
    second = SegmentCheckpoint("segment-next", first.segment_id,
        tuple(messages[-2:]) + ({"role": "assistant", "content": "Done"},), reason="context_compaction")
    for checkpoint in (first, second):
        exporter.capture("owner-slide", "slide", checkpoint)
    before = {path: path.read_bytes() for path in exporter.segment_dir.glob("*.json")}
    resumed = TrainingExporter(workspace, exporter.artifacts, exporter.issues, run_mode=RunMode.SYNTHESIS)
    segments = resumed.load_segments()
    assert [segment.checkpoint for segment in segments] == [first, second]

    manifest = resumed.export(segments, SimpleNamespace(status="complete", outputs={}, path=""))

    assert manifest["training_admission"] == "accepted"
    assert manifest["rejection_reasons"] == []
    assert manifest["segment_count"] == manifest["trainable_segment_count"] == 2
    rows = [json.loads(line) for line in resumed.rows_path.read_text().splitlines()]
    assert [row["message_count"] for row in rows] == [1 + 2 * tool_rounds, 3]
    assert [row["segment_id"] for row in rows] == [first.segment_id, second.segment_id]
    assert [row["predecessor_segment_id"] for row in rows] == [None, first.segment_id]
    assert {path: path.read_bytes() for path in resumed.segment_dir.glob("*.json")} == before
    for row, checkpoint in zip(rows, (first, second)):
        path = tmp_path / row["checkpoint_path"]
        assert row["trainable"] is True and row["segment_rejection_reasons"] == []
        assert row["checkpoint_sha256"] == file_sha256(path)
        assert json.loads(path.read_text())["messages"] == list(checkpoint.messages)


def test_open_tool_segment_is_isolated_not_deck_wide_rejection(tmp_path: Path) -> None:
    # 删除消息条数门，不放宽真实事务完整性；坏段仍单独拒收。
    workspace, artifacts, issues = _ready_workspace(tmp_path, pages=1)
    delivery = DeliveryExporter(workspace, artifacts, issues).export(
        include_pdf=False, include_pptx=False
    )
    clean = SegmentCheckpoint.initial(
        ({"role": "user", "content": "one clean turn"},),
        segment_id="segment-clean",
    )
    open_checkpoint = SegmentCheckpoint.initial(
        (
            {"role": "user", "content": "read the evidence"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "unfinished",
                "name": "read_file", "input": {"path": "knowledge.md"}}]},
        ),
        segment_id="segment-open",
    )
    manifest = TrainingExporter(
        workspace, artifacts, issues,
        run_mode=RunMode.SYNTHESIS,
    ).export(
        [
            TrainingSegmentInput("owner-clean", "slide", clean),
            TrainingSegmentInput("owner-image", "image", open_checkpoint),
        ],
        delivery,
    )

    # 整册没有全局门 → accepted,且报告有 1 个可训练段。
    assert manifest["training_admission"] == "accepted"
    assert manifest["eligible_for_training"] is True
    assert manifest["trainable_segment_count"] == 1
    assert manifest["global_rejection_reasons"] == []
    # 诊断仍汇总段级原因供排障。
    assert manifest["rejection_reasons"] == ["open_tool_transaction=segment-open"]

    rows = {
        json.loads(line)["owner_id"]: json.loads(line)
        for line in (
            tmp_path / "_trace" / "training" / "training-segments.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    }
    # 干净段仍可供后处理筛选；坏段保留诊断但不可训练。
    assert rows["owner-clean"]["training_admission"] == "accepted"
    assert rows["owner-clean"]["trainable"] is True
    assert rows["owner-clean"]["segment_rejection_reasons"] == []
    assert rows["owner-image"]["training_admission"] == "rejected"
    assert rows["owner-image"]["trainable"] is False
    assert rows["owner-image"]["segment_rejection_reasons"] == ["open_tool_transaction=segment-open"]


def test_legacy_toolcall_tags_in_assistant_text_reject_only_that_segment(tmp_path: Path) -> None:
    workspace, artifacts, issues = _ready_workspace(tmp_path, pages=1)
    delivery = DeliveryExporter(workspace, artifacts, issues).export(
        include_pdf=False, include_pptx=False
    )
    clean = SegmentCheckpoint.initial(
        (
            {"role": "user", "content": "这里提到了 </invoke>，只是用户在描述错误。"},
            {"role": "assistant", "content": "我会检查导出格式。"},
        ),
        segment_id="segment-clean-tags-in-user",
    )
    leaked = SegmentCheckpoint.initial(
        (
            {"role": "user", "content": "继续制作页面"},
            {"role": "assistant", "content": "准备调用工具 </invoke><parameter=name>write_file</parameter>"},
        ),
        segment_id="segment-leaked-tool-tags",
    )
    exporter = TrainingExporter(
        workspace, artifacts, issues,
        run_mode=RunMode.SYNTHESIS,
    )

    manifest = exporter.export(
        [
            TrainingSegmentInput("owner-clean", "slide", clean),
            TrainingSegmentInput("owner-leaked", "slide", leaked),
        ],
        delivery,
    )

    assert manifest["training_admission"] == "accepted"
    assert manifest["trainable_segment_count"] == 1
    assert manifest["rejection_reasons"] == [
        "toolcall_template_tag_in_assistant_text=segment-leaked-tool-tags"
    ]
    rows = {
        json.loads(line)["owner_id"]: json.loads(line)
        for line in exporter.rows_path.read_text(encoding="utf-8").splitlines()
    }
    assert rows["owner-clean"]["trainable"] is True
    assert rows["owner-clean"]["segment_rejection_reasons"] == []
    assert rows["owner-leaked"]["trainable"] is False
    assert rows["owner-leaked"]["segment_rejection_reasons"] == [
        "toolcall_template_tag_in_assistant_text=segment-leaked-tool-tags"
    ]
    checkpoint = json.loads((tmp_path / rows["owner-leaked"]["checkpoint_path"]).read_text())
    assert checkpoint["toolcall_text_validation"]["status"] == "failed"
    assert checkpoint["toolcall_text_validation"]["forbidden_markers"][0]["marker"] == "</invoke"


def test_internal_runtime_paths_in_assistant_output_reject_only_that_segment(
    tmp_path: Path,
) -> None:
    workspace, artifacts, issues = _ready_workspace(tmp_path, pages=1)
    delivery = DeliveryExporter(workspace, artifacts, issues).export(
        include_pdf=False, include_pptx=False
    )
    clean = SegmentCheckpoint.initial(
        (
            {"role": "user", "content": "请确认训练样本里不要出现 _state/ 或 _trace/。"},
            {"role": "assistant", "content": "我会只保留模型工作所需的信息。"},
        ),
        segment_id="segment-clean-internal-paths-in-user",
    )
    leaked = SegmentCheckpoint.initial(
        (
            {"role": "user", "content": "读取刚才的工具诊断。"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "read-internal-diagnostic",
                        "name": "read_file",
                        "input": {"path": "_trace/tool-output/example.jsonl"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "read-internal-diagnostic",
                        "content": "permission denied",
                    }
                ],
            },
        ),
        segment_id="segment-leaked-internal-path",
    )
    exporter = TrainingExporter(
        workspace, artifacts, issues, run_mode=RunMode.SYNTHESIS
    )

    manifest = exporter.export(
        [
            TrainingSegmentInput("owner-clean", "slide", clean),
            TrainingSegmentInput("owner-leaked", "slide", leaked),
        ],
        delivery,
    )

    assert manifest["training_admission"] == "accepted"
    assert manifest["trainable_segment_count"] == 1
    assert manifest["rejection_reasons"] == [
        "internal_runtime_path_in_assistant_output=segment-leaked-internal-path"
    ]
    rows = {
        json.loads(line)["owner_id"]: json.loads(line)
        for line in exporter.rows_path.read_text(encoding="utf-8").splitlines()
    }
    assert rows["owner-clean"]["trainable"] is True
    assert rows["owner-clean"]["internal_path_validation"]["status"] == "passed"
    assert rows["owner-leaked"]["trainable"] is False
    assert rows["owner-leaked"]["segment_rejection_reasons"] == [
        "internal_runtime_path_in_assistant_output=segment-leaked-internal-path"
    ]
    checkpoint = json.loads((tmp_path / rows["owner-leaked"]["checkpoint_path"]).read_text())
    assert checkpoint["internal_path_validation"]["status"] == "failed"
    assert checkpoint["internal_path_validation"]["forbidden_markers"][0]["marker"] == "_trace/"


def test_global_gate_still_rejects_all_segments(tmp_path: Path) -> None:
    # 对照:全局门(此处用未闭合工具事务触发的 delivery 未就绪 + blocking)整册拒,
    # 段级隔离不豁免全局问题。用一个真正的全局门:交付未完成。
    workspace, artifacts, issues = _ready_workspace(tmp_path, pages=1)
    # 不导出 delivery(status 缺失)→ 全局门 delivery_status。
    from export.delivery import DeliveryManifest

    empty_delivery = DeliveryManifest(
        status="",
        canvas={},
        pages=(),
        outputs={},
        image_catalog_sha256="",
        accepted_degradations=(),
        path="delivery/manifest.json",
    )
    clean_a = SegmentCheckpoint.initial(
        ({"role": "user", "content": "a"},), segment_id="segment-a"
    )
    clean_b = SegmentCheckpoint.initial(
        ({"role": "user", "content": "b"},), segment_id="segment-b"
    )
    manifest = TrainingExporter(
        workspace, artifacts, issues, run_mode=RunMode.SYNTHESIS
    ).export(
        [
            TrainingSegmentInput("owner-a", "slide", clean_a),
            TrainingSegmentInput("owner-b", "slide", clean_b),
        ],
        empty_delivery,
    )
    assert manifest["training_admission"] == "rejected"
    assert manifest["trainable_segment_count"] == 0
    assert any("delivery_status" in r for r in manifest["global_rejection_reasons"])
    rows = [
        json.loads(line)
        for line in (
            tmp_path / "_trace" / "training" / "training-segments.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    assert all(row["trainable"] is False for row in rows)


def test_readable_projection_uses_live_context_not_training_or_page_name_guessing(tmp_path):
    from export.readable import export_readable_by_role
    from runtime.live_trace import LiveEventJournal
    workspace = WorkspaceState(tmp_path)
    live = LiveEventJournal(workspace.paths.trace / "live-events.jsonl")
    for agent, role, group in (("orchestrator", "orchestrator", ""), ("owner-slide", "slide", "evidence")):
        live.append(agent_id=agent, role=role, segment_id="a", event_type="agent_context",
            payload={"system": "actual", "messages": [{"role": "user", "content": "mention slide_99, not ownership"}],
                     "tools": [], "group_id": group, "pages": [1, 2] if group else [], "attempt": 1})
    (workspace.paths.trace / "training-segments.jsonl").write_text("invalid and deliberately unused")
    index = export_readable_by_role(workspace)
    assert {row["path"] for row in index["agents"]} == {"_trace/orchestrator", "_trace/subagents/slide_group_evidence"}
    assert not (workspace.paths.trace / "by-role").exists()
    slide = next(row for row in index["agents"] if row["role"] == "slide")
    assert slide["pages"] == [1, 2]


def test_readable_projection_keeps_actual_compaction_contexts(tmp_path):
    from export.readable import export_readable_by_role
    from runtime.live_trace import LiveEventJournal
    workspace = WorkspaceState(tmp_path)
    live = LiveEventJournal(workspace.paths.trace / "live-events.jsonl")
    for segment, text in (("a", "original request"), ("b", "compacted summary")):
        live.append(agent_id="owner-slide", role="slide", segment_id=segment, event_type="agent_context",
            payload={"system": "actual", "messages": [{"role": "user", "content": text}],
                     "tools": [], "group_id": "facts", "attempt": 1})
    index = export_readable_by_role(workspace)
    slide = index["agents"][0]
    assert slide["segment_ids"] == ["a", "b"]
    config = json.loads((tmp_path / slide["path"] / "config.json").read_text())
    assert [item["messages"][0]["content"] for item in config["contexts"]] == ["original request", "compacted summary"]


def test_training_reasoning_and_summary_are_independent_and_segment_scoped(tmp_path):
    from runtime.live_trace import LiveEventJournal
    workspace, artifacts, issues = _ready_workspace(tmp_path, pages=1)
    live = LiveEventJournal(workspace.paths.trace / "live-events.jsonl")
    for segment, mode, text in (("a", "full", "actual full reasoning"), ("a", "summary", "actual summary"), ("b", "full", "different segment")):
        live.append(agent_id="owner-slide", role="slide", segment_id=segment, event_type="assistant_reasoning",
            payload={"mode": mode, "source": "provider_response", "content": text, "partial": False,
                     "signature": "never-export-this", "attempt_id": "attempt-" + segment})
    checkpoint = SegmentCheckpoint.initial(({"role": "user", "content": "request"},), segment_id="a")
    exporter = TrainingExporter(workspace, artifacts, issues, run_mode=RunMode.SYNTHESIS)
    result = exporter._observable_trace(TrainingSegmentInput("owner-slide", "slide", checkpoint))
    assert result["assistant_reasoning"][0]["content"] == "actual full reasoning"
    assert result["assistant_analysis_summary"][0]["content"] == "actual summary"
    serialized = json.dumps(result)
    assert "different segment" not in serialized and "signature" not in serialized and "never-export-this" not in serialized


@pytest.mark.parametrize("partial", [False, True])
def test_training_admission_without_delivery_preserves_partial_reasoning_gate(tmp_path, partial):
    from runtime.live_trace import LiveEventJournal
    workspace, artifacts, issues = _ready_workspace(tmp_path, pages=1)
    exporter = DeliveryExporter(workspace, artifacts, issues)
    generation = exporter.export(include_pdf=False, include_pptx=False, include_zip=False)
    assert generation.path == "" and not (tmp_path / "delivery").exists()
    live = LiveEventJournal(workspace.paths.trace / "live-events.jsonl")
    live.append(agent_id="owner-slide", role="slide", segment_id="actual", event_type="assistant_reasoning",
        payload={"mode": "full", "content": "API-observable reasoning", "partial": partial,
                 "interrupted": partial, "source": "provider_response", "attempt_id": "attempt-observed"})
    checkpoint = SegmentCheckpoint.initial(({"role": "user", "content": "request"},), segment_id="actual")
    manifest = TrainingExporter(workspace, artifacts, issues, run_mode=RunMode.SYNTHESIS).export(
        [TrainingSegmentInput("owner-slide", "slide", checkpoint)], generation)
    assert manifest["training_admission"] == ("rejected" if partial else "accepted")
    assert any("partial_provider_reasoning" in reason for reason in manifest["rejection_reasons"]) is partial
    assert not (tmp_path / "delivery").exists()
