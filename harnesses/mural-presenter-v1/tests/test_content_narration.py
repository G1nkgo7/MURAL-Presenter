"""后处理读取最终讲稿与当前图文证据，不回到创作循环触发审计。"""
import hashlib
import json
from pathlib import Path

import pytest

from qc.content import ContentQC, extract_visible_text
from qc.content_review import CONTENT_RUBRIC, CONTENT_RUBRIC_VERSION, run_content_review
from qc.extrinsic import RUBRIC_VERSION
from runtime.shared_objects import SharedSkillStore
from runtime.skill_runtime import current_critic_state
from test_content_review import _ready_workspace, _run_spec


SKILL = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1"


@pytest.mark.parametrize("html,expected", [
    ('<div aria-hidden="true">辅助技术不读，像素可见<span>内部文字</span></div>', '辅助技术不读，像素可见 内部文字'),
    ('<svg aria-hidden="true"><text>融化</text><text>凝固</text><text>例：白汽</text></svg>', '融化 凝固 例：白汽'),
    ('<div style="display:none"><svg aria-hidden="false"><text>隐藏文字</text></svg></div><p>可见</p>', '可见'),
    ('<div style=" DISPLAY : none !important "><span>隐藏文字</span></div><p>可见</p>', '可见'),
    ('<p hidden aria-hidden="false">隐藏文字</p><p aria-hidden="true">可见</p>', '可见'),
    ('<p style="display:none">隐藏文字</p><p>可见</p>', '可见'),
    ('<p style="display:none; display:block">可见</p>', '可见'),
    ('<p style="display:none!important; display:block">隐藏文字</p><p>可见</p>', '可见'),
    ('<p style="display:block!important; display:none">可见</p>', '可见'),
    ('<script>隐藏</script><style>隐藏</style><template>隐藏</template><noscript>隐藏</noscript><p>可见</p>', '可见'),
])
def test_static_text_separates_accessibility_hidden_from_nonrendering_content(html, expected):
    assert extract_visible_text(html) == expected


def _plans(root):
    shared = "# Deck\n## Brief\n- language: zh\n- page_count: 1\n## Visual Direction\n清楚的对比。\n"
    body = ("- job: explain\n- must_show: 来源与边界\n- medium: chart\n"
            "## Evidence\n保留原文定位，不是经核实事实。\n\n"
            "## 讲稿\n旧定义仍待纠正。\n这是完整的第二行讲稿。\n")
    (root / "deck.md").write_text(shared)
    (root / "briefs").mkdir()
    path = root / "briefs/slide_01.md"
    path.write_text("# P01 — 主题\n" + body)
    return path


@pytest.mark.parametrize("document", ["deck.md", "briefs/slide_01.md"])
def test_packet_preserves_complete_actual_author_documents_separately_from_sources(tmp_path, document):
    workspace, _, _ = _ready_workspace(tmp_path)
    _plans(tmp_path)
    plan = tmp_path / document
    # 字节哈希与送审文本对应；不通过通用换行读法改变源内容。
    data = plan.read_bytes().replace(b"\n", b"\r\n")
    plan.write_bytes(data)
    speech = tmp_path / "speech.md"
    speech.write_text("# 演讲备注\n\n旧定义仍待纠正。\n")
    packet = ContentQC.build_packet(_run_spec(tmp_path), workspace, pages=[1])
    assert packet.sources == ()
    author = next(row for row in packet.authored_plans if row.path == plan.relative_to(tmp_path).as_posix())
    assert author.text == data.decode() and author.sha256 == hashlib.sha256(data).hexdigest()
    assert any("这是完整的第二行讲稿。" in row.text for row in packet.authored_plans)
    assert packet.derived_speech.text == speech.read_text()
    assert packet.derived_speech.sha256 == hashlib.sha256(speech.read_bytes()).hexdigest()
    assert packet.pages[0].png_sha256 == hashlib.sha256(workspace.paths.page_png(1).read_bytes()).hexdigest()


@pytest.mark.parametrize("changed", ["briefs/slide_01.md", "deck.md", "speech.md"])
def test_postprocess_packet_tracks_current_author_and_speech_without_changing_pixels(tmp_path, changed):
    workspace, artifacts, _ = _ready_workspace(tmp_path)
    _plans(tmp_path)
    (tmp_path / "speech.md").write_text("旧讲稿")
    def packet():
        return ContentQC.build_packet(_run_spec(tmp_path), workspace, pages=[1],
            render_receipts={1: artifacts.render_receipt(1)})
    first = packet()
    assert first == packet()
    path = tmp_path / changed
    path.write_text(path.read_text() + "\n本次修改：讲稿定义修正，页面像素未变。\n")
    second = packet()
    assert first.pages == second.pages
    assert first.to_dict() != second.to_dict()


def test_postprocess_reviews_the_speech_already_derived_by_delivery(tmp_path):
    from export.delivery import _sync_speech

    workspace, artifacts, _ = _ready_workspace(tmp_path)
    plan = _plans(tmp_path)
    SharedSkillStore.for_workspace(tmp_path).pin(workspace.paths, SKILL, skill_name=SKILL.name)
    (tmp_path / "speech.md").write_text("未更新的历史讲稿")
    pixels_before = workspace.paths.page_png(1).read_bytes()
    html_before = workspace.paths.page_html(1).read_bytes()
    _sync_speech(workspace.paths)
    def packet():
        return ContentQC.build_packet(_run_spec(tmp_path), workspace, pages=[1],
            render_receipts={1: artifacts.render_receipt(1)})
    first = packet()
    assert "未更新的历史讲稿" not in first.derived_speech.text
    assert "旧定义仍待纠正。" in first.derived_speech.text
    plan.write_text(plan.read_text().replace("旧定义仍待纠正。", "新定义与当前页面对齐。"))
    _sync_speech(workspace.paths)
    second = packet()
    assert "新定义与当前页面对齐。" in second.derived_speech.text
    assert second.derived_speech.sha256 == hashlib.sha256((tmp_path / "speech.md").read_bytes()).hexdigest()
    assert first.derived_speech != second.derived_speech
    assert workspace.paths.page_png(1).read_bytes() == pixels_before
    assert workspace.paths.page_html(1).read_bytes() == html_before


def test_postprocess_packet_uses_only_current_visual_observation(tmp_path):
    workspace, artifacts, _ = _ready_workspace(tmp_path)
    page = workspace.pages()[0]
    record = {"page_uid": page.page_uid, "inspection_id": page.inspection_id,
              "html_sha256": page.html_sha256, "png_sha256": page.png_sha256,
              "critic_model": "test-model", "rubric_version": RUBRIC_VERSION,
              "completed": True, "body": "当前图像中的物态与右侧文字标签冲突。"}
    records = [{**record, "png_sha256": "old-pixels", "body": "过期观察不应送审"}]
    def packet():
        evidence = current_critic_state(records, workspace.pages(), critic_model="test-model",
            rubric_version=RUBRIC_VERSION)["current_inspections"]
        return ContentQC.build_packet(_run_spec(tmp_path), workspace, pages=[1],
            render_receipts={1: artifacts.render_receipt(1)}, visual_evidence=evidence)
    assert packet().visual_evidence[0]["completed"] is False
    records.append(record)
    current = packet()
    assert current.visual_evidence[0]["completed"]
    assert current.visual_evidence[0]["feedback"] == record["body"]
    assert "过期观察" not in json.dumps(current.to_dict(), ensure_ascii=False)


def test_rendered_image_instances_preserve_distinct_labels_and_stale_pixels_fail(tmp_path):
    from PIL import Image

    workspace, artifacts, _ = _ready_workspace(tmp_path)
    image = workspace.paths.assets / "state.png"
    Image.new("RGB", (10, 10), "white").save(image)
    receipt = dict(artifacts.render_receipt(1))
    receipt["diagnostics"] = {"visible_images": [
        {"src": "../assets/state.png", "alt": label, "x": x, "y": 100, "width": 100, "height": 100}
        for label, x in [("第一处的作者标签", 10), ("第二处的作者标签", 200)]], "rendered_text": ["当前图注"]}
    packet = ContentQC.build_packet(_run_spec(tmp_path), workspace, pages=[1], render_receipts={1: receipt})
    assert len(packet.pages[0].used_images) == 1
    assert [row["alt"] for row in packet.pages[0].used_images[0]["rendered_instances"]] == [
        "第一处的作者标签", "第二处的作者标签"]
    Image.new("RGB", (1600, 1200), "red").save(workspace.paths.page_png(1))
    with pytest.raises(ValueError, match="PNG is stale"):
        ContentQC.build_packet(_run_spec(tmp_path), workspace, pages=[1], render_receipts={1: receipt})


def test_postprocess_reviewer_receives_narration_and_explicit_evidence_boundaries(tmp_path):
    workspace, _, _ = _ready_workspace(tmp_path)
    _plans(tmp_path)
    (tmp_path / "speech.md").write_text("实际送审的讲稿")
    packet = ContentQC.build_packet(_run_spec(tmp_path), workspace, pages=[1])
    packets = []
    result = run_content_review(packet, lambda facts: packets.append(facts) or "[]", reviewer_model="offline")
    assert result.passed and packets == [packet]
    assert packets[0].derived_speech.text == "实际送审的讲稿"
    assert CONTENT_RUBRIC_VERSION == "content-audit-rubric.v5"
    for clause in ("作者计划不是事实来源", "完整讲稿章节", "derived_speech", "没有直接收到图片",
                   "不代表 Critic 赞同或科学正确", "高置信的基础概念/逻辑错误", "采样缺失也不能证明未上屏",
                   "不把参考文献页要求扩大成每页、每图的来源脚注", "未要求引用且无必需署名时不强加页脚"):
        assert clause in CONTENT_RUBRIC
