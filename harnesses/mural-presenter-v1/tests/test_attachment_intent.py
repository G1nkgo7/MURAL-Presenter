"""FEAT-2.3：显式附件 intent 从 CLI → RunSpec → stage → manifest 的端到端透传合同。

只校验用户显式声明的用途被忠实保留，且不给 intent 时行为与历史完全一致（保守 mixed）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from runtime.attachments import AttachmentError, AttachmentStager
from runtime.config import RunSpec

import infer


def _png(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    from PIL import Image

    Image.new("RGB", (48, 32), (10, 120, 200)).save(path)
    return path


def _staged_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "_state").mkdir(parents=True)
    return ws


def _record_by_name(manifest: dict, name: str) -> dict:
    return next(item for item in manifest["records"] if item["name"] == name)


def test_style_reference_intent_populates_style_paths(tmp_path: Path) -> None:
    """style_reference 图片进 style_reference_paths，且 intent 不被拍平成 mixed。"""
    png = _png(tmp_path / "src" / "look.png")
    ws = _staged_workspace(tmp_path)
    manifest = AttachmentStager(ws).stage([png], intents=["style_reference"]).value
    record = _record_by_name(manifest, "look.png")
    assert record["intent"] == "style_reference"
    assert record["needs_style_reference"] is True
    assert record["needs_visual_reuse"] is True
    # 仅作风格参照：不抽正文，也不进内容 material。
    assert record["needs_content_extraction"] is False
    rel = record["path"]
    assert manifest["style_reference_paths"] == [rel]
    assert rel not in manifest["attachment_paths"]


def test_no_intent_keeps_conservative_mixed(tmp_path: Path) -> None:
    """不给 intent 时图片仍默认 mixed、style_reference_paths 保持空（向后兼容）。"""
    png = _png(tmp_path / "src" / "plain.png")
    ws = _staged_workspace(tmp_path)
    manifest = AttachmentStager(ws).stage([png]).value
    record = _record_by_name(manifest, "plain.png")
    assert record["intent"] == "mixed"
    assert record["needs_style_reference"] is False
    assert record["needs_visual_reuse"] is True
    assert record["needs_content_extraction"] is True
    assert manifest["style_reference_paths"] == []


def test_content_source_intent_disables_visual_reuse(tmp_path: Path) -> None:
    """content_source 图片只作事实内容，不进视觉复用，也不进 style_reference。"""
    png = _png(tmp_path / "src" / "chart.png")
    ws = _staged_workspace(tmp_path)
    manifest = AttachmentStager(ws).stage([png], intents=["content_source"]).value
    record = _record_by_name(manifest, "chart.png")
    assert record["intent"] == "content_source"
    assert record["needs_visual_reuse"] is False
    assert record["needs_content_extraction"] is True
    assert record["needs_style_reference"] is False
    assert manifest["visual_asset_paths"] == []
    assert manifest["style_reference_paths"] == []


def test_mixed_intent_matches_default(tmp_path: Path) -> None:
    """显式 mixed 与不给 intent 对同一图片产生一致的路由标记。"""
    png = _png(tmp_path / "src" / "same.png")
    ws_a = _staged_workspace(tmp_path / "a")
    ws_b = _staged_workspace(tmp_path / "b")
    explicit = _record_by_name(
        AttachmentStager(ws_a).stage([png], intents=["mixed"]).value, "same.png"
    )
    default = _record_by_name(AttachmentStager(ws_b).stage([png]).value, "same.png")
    for key in ("intent", "needs_visual_reuse", "needs_content_extraction",
                "needs_style_reference"):
        assert explicit[key] == default[key]


def test_empty_intent_token_is_treated_as_unspecified(tmp_path: Path) -> None:
    """空串 intent（serve_one 对齐占位）等同未指定，回落保守默认。"""
    png = _png(tmp_path / "src" / "gap.png")
    ws = _staged_workspace(tmp_path)
    manifest = AttachmentStager(ws).stage([png], intents=[""]).value
    record = _record_by_name(manifest, "gap.png")
    assert record["intent"] == "mixed"
    assert manifest["style_reference_paths"] == []


def test_intents_must_align_with_attachments(tmp_path: Path) -> None:
    png = _png(tmp_path / "src" / "one.png")
    ws = _staged_workspace(tmp_path)
    with pytest.raises(AttachmentError):
        AttachmentStager(ws).stage([png], intents=["mixed", "style_reference"])


def test_unknown_intent_is_rejected(tmp_path: Path) -> None:
    png = _png(tmp_path / "src" / "bad.png")
    ws = _staged_workspace(tmp_path)
    with pytest.raises(AttachmentError):
        AttachmentStager(ws).stage([png], intents=["hero_background"])


def test_runspec_carries_and_validates_attachment_intents(tmp_path: Path) -> None:
    """RunSpec.from_inputs 保留对齐的 intents，长度不齐时明确拒绝。"""
    png = _png(tmp_path / "src" / "ref.png")
    ws = _staged_workspace(tmp_path)
    spec = RunSpec.from_inputs(
        raw_user_query="make a deck",
        output_workspace=ws,
        attachments=[str(png)],
        attachment_intents=["style_reference"],
        environment={"MODEL_API_FORMAT": "anthropic"},
    )
    assert spec.attachment_intents == ("style_reference",)
    assert spec.to_manifest()["attachment_intents"] == ["style_reference"]
    # stage() 读取 spec 的 intents 并透传（模拟 SkillRuntime 的接线）。
    manifest = AttachmentStager(ws).stage(
        spec.attachments, intents=(spec.attachment_intents or None)
    ).value
    assert manifest["style_reference_paths"] == [
        _record_by_name(manifest, "ref.png")["path"]
    ]
    with pytest.raises(ValueError):
        RunSpec.from_inputs(
            raw_user_query="q",
            output_workspace=ws,
            attachments=[str(png)],
            attachment_intents=["style_reference", "content_source"],
            environment={"MODEL_API_FORMAT": "anthropic"},
        )


def test_runspec_default_has_empty_intents(tmp_path: Path) -> None:
    """不传 attachment_intents 时保持空 tuple（历史行为）。"""
    png = _png(tmp_path / "src" / "d.png")
    ws = _staged_workspace(tmp_path)
    spec = RunSpec.from_inputs(
        raw_user_query="q",
        output_workspace=ws,
        attachments=[str(png)],
        environment={"MODEL_API_FORMAT": "anthropic"},
    )
    assert spec.attachment_intents == ()


def test_cli_parses_parallel_attachment_intent() -> None:
    """--attachment-intent 与 --attachment 顺序对齐解析成并行列表。"""
    parser = infer._argument_parser()
    args = parser.parse_args(
        [
            "--query", "hi",
            "--workspace", "/tmp/ws",
            "--attachment", "/a/one.png",
            "--attachment-intent", "style_reference",
            "--attachment", "/a/two.pdf",
            "--attachment-intent", "content_source",
        ]
    )
    assert args.attachment == ["/a/one.png", "/a/two.pdf"]
    assert args.attachment_intent == ["style_reference", "content_source"]


def test_cli_attachment_intent_defaults_empty() -> None:
    """不给 --attachment-intent 时默认空列表，单 deck 入口会转成 None。"""
    parser = infer._argument_parser()
    args = parser.parse_args(
        ["--query", "hi", "--workspace", "/tmp/ws", "--attachment", "/a/x.png"]
    )
    assert args.attachment_intent == []
    assert (args.attachment_intent or None) is None
