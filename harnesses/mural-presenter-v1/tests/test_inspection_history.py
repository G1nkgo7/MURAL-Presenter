"""同检查目的保留已消费像素历史；A → B → A 不再请求模型重看 A。"""
from __future__ import annotations

import pytest
from PIL import Image

from infer import build_inspect_handlers
from runtime.tools import ToolContext, ToolExecutionError, WriteScope
from state.artifacts import ArtifactStore
from state.workspace import PageLifecycle, WorkspaceState, file_sha256
from test_native_inspect import (
    _analyzer, _author_page, _group_workspace, _make_workspace, _renderer, _slide_context,
)


def _changed_pixels(_html, png, canvas):
    Image.new("RGB", (canvas["width_px"], canvas["height_px"]), (25, 60, 110)).save(png)
    return {"geometry": canvas, "loaded_resources": [], "failed_resources": []}


def test_ineffective_edit_reports_pixel_unchanged_without_new_visual_check(tmp_path):
    workspace, artifacts = _make_workspace(tmp_path)
    inspect, commit = build_inspect_handlers(workspace, artifacts, _renderer, _analyzer)
    first = inspect(_slide_context(workspace), {"page": 1})
    assert first["pixel_unchanged"] is False
    commit(first["pending_inspection"])
    _author_page(workspace, artifacts, 1, "<html><body>same pixels<!-- ineffective CSS edit --></body></html>")
    result = inspect(_slide_context(workspace), {"page": 1})
    assert result["pixel_unchanged"] and result["inspection_cached"]
    assert not result["native_multimodal"] and "pending_inspection" not in result
    assert result["html_sha256"] != first["html_sha256"] and result["png_sha256"] == first["png_sha256"]
    assert "parent geometry" in result["repair_hint"]
    assert workspace.page(1).lifecycle == PageLifecycle.INSPECTED


@pytest.mark.parametrize("html_comment", ["", "<!-- new source version, identical pixels -->"])
def test_page_returns_to_consumed_pixels_without_new_image_after_resume(tmp_path, html_comment):
    workspace, artifacts = _make_workspace(tmp_path)
    context = _slide_context(workspace)
    inspect, commit = build_inspect_handlers(workspace, artifacts, _renderer, _analyzer)
    first = inspect(context, {"page": 1})
    commit(first["pending_inspection"])
    _author_page(workspace, artifacts, 1, "<html><body>version-B</body></html>")
    artifacts.render_pages([1], _changed_pixels)
    second = inspect(context, {"page": 1})
    assert second["native_multimodal"] and second["sha256"] != first["sha256"]
    commit(second["pending_inspection"])

    resumed = WorkspaceState(tmp_path)
    artifacts = ArtifactStore(tmp_path, workspace_state=resumed)
    inspect, _ = build_inspect_handlers(resumed, artifacts, _renderer, _analyzer)
    _author_page(resumed, artifacts, 1, "<html><body>v1</body></html>" + html_comment)
    returned = inspect(context, {"page": 1})
    assert returned["sha256"] == first["sha256"]
    assert returned["inspection_cached"] and not returned["native_multimodal"]
    assert "pending_inspection" not in returned
    assert returned["current_inspection"]["reused_from_inspection_id"] == first["inspection_id"]
    assert returned["current_inspection"]["html_sha256"] == resumed.page(1).html_sha256
    assert resumed.page(1).lifecycle == PageLifecycle.INSPECTED
    history = list(resumed._read_state()["inspection_receipts"].values())
    assert len(history) == 2
    assert {receipt["sha256"] for receipt in history} == {first["sha256"], second["sha256"]}
    with pytest.raises(ToolExecutionError) as error:
        inspect(context, {"page": 1, "question": "different purpose"})
    assert error.value.code == "inspection_limit_reached"


@pytest.mark.parametrize("html_comment", ["", "<!-- returned source, same group pixels -->"])
def test_group_returns_to_consumed_pixels_even_when_contact_sheet_was_overwritten(tmp_path, monkeypatch, html_comment):
    workspace, artifacts, (inspect, commit) = _group_workspace(tmp_path, monkeypatch)
    context = _slide_context(workspace, (1, 2))
    first = inspect(context, {"page": [1, 2]})
    commit(first["pending_inspection"])
    _author_page(workspace, artifacts, 2, "<html><body>version-B</body></html>")
    artifacts.render_pages([2], _changed_pixels)
    commit(inspect(context, {"page": 2})["pending_inspection"])
    second = inspect(context, {"page": [1, 2]})
    commit(second["pending_inspection"])
    assert first["path"] != second["path"] and first["sha256"] != second["sha256"]

    resumed = WorkspaceState(workspace.paths.root)
    artifacts = ArtifactStore(resumed.paths.root, workspace_state=resumed)
    inspect, _ = build_inspect_handlers(resumed, artifacts, _renderer, _analyzer)
    _author_page(resumed, artifacts, 2, "<html><body>second</body></html>" + html_comment)
    assert inspect(context, {"page": 2})["inspection_cached"]
    returned = inspect(context, {"page": [1, 2]})
    assert returned["sha256"] == first["sha256"]
    assert returned["inspection_cached"] and not returned["native_multimodal"]
    assert "pending_inspection" not in returned
    assert returned["current_inspection"]["members"][1]["html_sha256"] == resumed.page(2).html_sha256
    if html_comment:
        assert returned["current_inspection"]["reused_from_inspection_id"] == first["inspection_id"]
    history = [row for row in resumed._read_state()["inspection_receipts"].values() if row["kind"] == "group"]
    assert len(history) == 2
    assert {receipt["sha256"] for receipt in history} == {first["sha256"], second["sha256"]}
    with pytest.raises(ToolExecutionError) as error:
        inspect(context, {"page": [1, 2], "question": "new group purpose"})
    assert error.value.code == "inspection_limit_reached"


def test_orchestrator_reuses_current_image_handoff_instead_of_reopening_raw_asset(tmp_path):
    workspace, artifacts = _make_workspace(tmp_path)
    target = workspace.paths.assets / "reviewed-cover.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (80, 60), "navy").save(target)
    digest = file_sha256(target)
    artifacts.record_image_result(
        artifact_id="reviewed-cover",
        owner_id="image-owner",
        path="assets/reviewed-cover.png",
        status="available",
        image_metadata={"slides": [1], "purpose": "cover"},
        reviewed_sha256=digest,
    )
    analyzer_calls = []
    inspect, _ = build_inspect_handlers(
        workspace,
        artifacts,
        _renderer,
        lambda *args: analyzer_calls.append(args) or {},
    )
    context = ToolContext(
        "orchestrator",
        "orchestrator",
        read_scopes=(WriteScope("assets", recursive=True),),
        metadata={"workspace_root": str(workspace.paths.root)},
    )
    reused = inspect(context, {"path": "assets/reviewed-cover.png"})
    assert reused["cross_owner_review_reused"]
    assert reused["inspection_cached"] and not reused["native_multimodal"]
    assert reused["consumer_pages"] == [1]
    assert analyzer_calls == []

    reopened = inspect(context, {"path": "assets/reviewed-cover.png", "retain": True})
    assert reopened["native_multimodal"] and not reopened["inspection_cached"]
    assert len(analyzer_calls) == 1
