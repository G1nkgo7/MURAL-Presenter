"""后台附件证据由 Research 读取；移除的 Material CLI 不再是 Agent 合同。"""
from dataclasses import replace
import json
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pymupdf
from PIL import Image
import pytest

from owner.registry import OwnerRegistry
from runtime.attachments import AttachmentStager, attachment_receipt_view, resolve_attachment_records
from runtime.production import read_scopes_for_role, write_scopes_for_role
from runtime.tools import ToolCall, ToolContext, ToolContractError, ToolRuntime, register_tools
from test_explicit_asset_commands import services_for as _services_for


SKILL = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1"


def services_for(root):
    services = _services_for(root)
    services.snapshot = SimpleNamespace(name=SKILL.name, target=SKILL, tree_sha256="test-alpha-attachments",
        resolve_skill_path=lambda logical: SKILL / Path(logical).relative_to(f"skills/{SKILL.name}"),
        capture_runtime_fonts=lambda: None)
    return services


def _research(root, tmp_path):
    source = tmp_path / "evidence.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((30, 30), "Annual revenue is 12345. Source evidence.")
        pdf.save(source)
    manifest = AttachmentStager(root).stage([source]).value
    record = resolve_attachment_records(root)[0]
    context = ToolContext("research-owner", "research",
        read_scopes=read_scopes_for_role("research", skill_name=SKILL.name,
            attachment_paths=manifest["attachment_paths"]),
        write_scopes=write_scopes_for_role("research"))
    return context, record


def test_research_can_read_same_attachment_layout_but_not_other_evidence(tmp_path):
    services = services_for(tmp_path / "workspace")
    context, record = _research(services.workspace.paths.root, tmp_path)
    layout = str(Path(record.page_image_paths[0]).with_suffix(".json"))
    result = services.read_file(context, {"path": layout})
    assert '"text_mode": "unparsed"' in result["content"]
    original = record.original_path
    attachment_root = record.extracted_root
    assert context.may_read(attachment_root + "/mineru/fixed/document.md")
    assert not context.may_write(layout) and not context.may_write(original)
    with pytest.raises(ToolContractError) as caught:
        services.read_file(context, {"path": "inputs/extracted/att-ffffffffffff/pages/page_001.json"})
    assert caught.value.code == "read_scope_denied"
    resumed = ToolContext("research-owner", "research", read_scopes=read_scopes_for_role(
        "research", skill_name=SKILL.name,
        attachment_paths=[original, attachment_root + "/mineru/fixed/document.md"]))
    assert resumed.may_read(layout)


@pytest.mark.parametrize("role", ["material", "research", "image", "slide", "orchestrator"])
@pytest.mark.parametrize("flags", ["", " --url http://unauthorized.invalid", " --backend cpu"])
def test_removed_material_cli_is_denied_before_any_subprocess(tmp_path, monkeypatch, role, flags):
    services = services_for(tmp_path / "workspace")
    _context, record = _research(services.workspace.paths.root, tmp_path)
    assert not (SKILL / "scripts/material.py").exists()
    monkeypatch.setattr("runtime.production._run_command",
        lambda *_args, **_kwargs: pytest.fail("removed Agent parser must not execute"))
    original = (record.root / record.original_path).read_bytes()
    command = f"python skills/{SKILL.name}/scripts/material.py parse . --attachment {record.attachment_id}" + flags
    with pytest.raises(ToolContractError) as caught:
        services.terminal(ToolContext("removed-entry", role), {"command": command})
    assert caught.value.code == "terminal_role_denied"
    assert (record.root / record.original_path).read_bytes() == original
    assert not list((record.root / record.extracted_root).rglob("*.png"))


def test_research_writes_only_assigned_report_not_shared_knowledge_or_inputs(tmp_path):
    services = services_for(tmp_path / "workspace")
    context, record = _research(services.workspace.paths.root, tmp_path)
    report_path = "research/source-evidence.md"
    services.owners = OwnerRegistry(
        record.root, deck_id=services.workspace.deck_id
    )
    owner = services.owners.accept(
        role="research", group_id="source-evidence", output_path=report_path
    )
    context = replace(
        context,
        owner_id=owner.owner_id,
        write_scopes=write_scopes_for_role("research", output_path=report_path),
    )
    original = (record.root / record.original_path).read_bytes()
    tools = ToolRuntime()
    register_tools(tools, {"write_file": services.write_file}, require_complete=False)
    written = tools.execute(context, ToolCall("report", "write_file",
        {"path": report_path, "content": "Source page 1; partial evidence is explicit."}))
    assert written.ok
    assert (record.root / report_path).read_text().startswith("Source page 1")
    for index, target in enumerate((record.original_path, record.page_image_paths[0], "knowledge.md",
                                    "material.md", "research.md", "assets/catalog.md",
                                    "slides/slide_01.html")):
        denied = tools.execute(context, ToolCall(f"denied-{index}", "write_file", {"path": target, "content": "forbidden"}))
        assert not denied.ok and denied.error_code == "write_scope_denied"
    assert (record.root / record.original_path).read_bytes() == original
    tools.assert_closed()
    assert read_scopes_for_role("material", skill_name=SKILL.name) == ()
    assert write_scopes_for_role("material") == ()


@pytest.mark.parametrize("cached", [False, True])
def test_large_background_receipt_view_preserves_existing_manifest_facts(tmp_path, cached):
    services = services_for(tmp_path / "workspace")
    context, record = _research(services.workspace.paths.root, tmp_path)
    receipt = {"status": "completed", "backend": "mineru", "attachment_id": record.attachment_id,
        "source_sha256": record.sha256, "cached": cached,
        "original_path": record.original_path, "content_list_path": f"{record.extracted_root}/mineru/fixed/content-list.json",
        "page_image_pattern": f"{record.extracted_root}/pages/page_NNN.png",
        "coverage": {"granularity": "page", "expected_units": [1], "locations": [{"page": 1}]},
        "media_paths": [f"{record.extracted_root}/mineru/" + "a" * 128 + f"/images/{i}.jpg" for i in range(700)]}
    manifest_path = record.root / "_trace/attachment-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["records"][0]["parse"] = {"current": "fixed", "results": {"fixed": {"receipt": receipt}}}
    manifest_path.write_text(json.dumps(manifest))
    before = manifest_path.read_bytes()
    result = attachment_receipt_view(receipt)
    assert not context.may_read("_trace/attachment-manifest.json")
    assert len(result["media_paths"]) == 700 and result["media_count"] == 700
    assert result["cached"] is cached
    assert result["coverage"]["locations"][0]["page_image_path"] == record.page_image_paths[0]
    assert manifest_path.read_bytes() == before


def _fixed_content(record, rows):
    relative = f"{record.extracted_root}/mineru/fixed/content-list.json"
    path = record.root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(rows, ensure_ascii=False, indent=4).encode()
    path.write_bytes(raw)
    receipt = {"content_list_path": relative, "media_paths": [], "source_sha256": record.sha256,
        "original_path": record.original_path, "text_path": f"{record.extracted_root}/mineru/fixed/document.md",
        "page_image_pattern": f"{record.extracted_root}/pages/page_NNN.png",
        "coverage": {"granularity": "page", "expected_units": [1], "locations": [{"page": 1}],
                     "content_bbox_units": "normalized_0_1000"}}
    manifest_path = record.root / "_trace/attachment-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["records"][0]["parse"] = {"current": "fixed", "results": {"fixed": {
        "receipt": receipt, "outputs": {relative: hashlib.sha256(raw).hexdigest()}}}}
    manifest_path.write_text(json.dumps(manifest))
    return relative


def test_content_view_paginates_whole_json_items_and_keeps_literal_search_contract(tmp_path):
    services = services_for(tmp_path / "workspace")
    context, record = _research(services.workspace.paths.root, tmp_path)
    rows = [{"type": "text", "page_idx": 0, "text": f"evidence {i}", "bbox": [322, 176, 671, 732]}
            for i in range(47)]
    rows[0]["text"] = "alpha\u2028beta\u0085gamma"
    path = _fixed_content(record, rows)
    original = (record.root / path).read_bytes()
    first = services.read_file(context, {"path": path})
    assert first["format"] == "source_locations_jsonl" and first["offset_unit"] == "content_item"
    entries = [json.loads(line) for line in first["content"].split("\n")]
    assert entries[0]["text"] == rows[0]["text"]
    assert first["line_count"] == 47 and first["returned_line_count"] == 40 and first["next_offset"] == 41
    tail = services.read_file(context, {"path": path, "offset": 41})
    assert tail["returned_line_count"] == 7 and tail["next_offset"] is None
    found = services.read_file(context, {"path": path, "query": "evidence 20", "limit": 1})
    assert "not visual verification" in found["note"] and "Literal match excerpts" in found["note"]
    assert found["mode"] == "search_snippets" and found["next_offset"] is not None
    last = services.read_file(context, {"path": path, "offset": 48})
    assert last["content"] == "" and last["returned_line_count"] == 0 and last["next_offset"] is None
    with pytest.raises(ToolContractError, match="may not read"):
        services.read_file(ToolContext("different-owner", "research"), {"path": path})
    assert (record.root / path).read_bytes() == original


def test_read_file_exposes_related_mineru_figures_for_multimodal_transport(tmp_path):
    services = services_for(tmp_path / "workspace")
    context, record = _research(services.workspace.paths.root, tmp_path)
    base = Path(record.extracted_root) / "mineru" / "fixed"
    content_path = (base / "content-list.json").as_posix()
    markdown_path = (base / "document.md").as_posix()
    image_path = (base / "images" / "chart.png").as_posix()
    (record.root / image_path).parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (24, 16), "navy").save(record.root / image_path)
    rows = [
        {"type": "text", "page_idx": 0, "text": "Benchmark results"},
        {"type": "image", "page_idx": 0, "img_path": "images/chart.png"},
    ]
    (record.root / content_path).write_text(json.dumps(rows), encoding="utf-8")
    (record.root / markdown_path).write_text(
        "# Benchmark results\n\n![Chart](images/chart.png)\n", encoding="utf-8"
    )
    receipt = {
        "content_list_path": content_path,
        "text_path": markdown_path,
        "media_paths": [image_path],
        "source_sha256": record.sha256,
        "original_path": record.original_path,
        "page_image_pattern": f"{record.extracted_root}/pages/page_NNN.png",
        "coverage": {"granularity": "page", "expected_units": [1], "locations": [{"page": 1}]},
    }
    outputs = {
        path: hashlib.sha256((record.root / path).read_bytes()).hexdigest()
        for path in (content_path, markdown_path, image_path)
    }
    manifest_path = record.root / "_trace/attachment-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["records"][0]["parse"] = {
        "current": "fixed",
        "results": {"fixed": {"receipt": receipt, "outputs": outputs}},
    }
    manifest_path.write_text(json.dumps(manifest))

    content = services.read_file(context, {"path": content_path, "query": "Benchmark"})
    markdown = services.read_file(context, {"path": markdown_path})

    assert content["_multimodal_images"] == [{"path": image_path, "page": 1}]
    assert markdown["_multimodal_images"] == [{"path": image_path}]


def test_long_content_view_keeps_references_and_reports_excerpts_and_continuation(tmp_path):
    services = services_for(tmp_path / "workspace")
    context, record = _research(services.workspace.paths.root, tmp_path)
    path = _fixed_content(record, [{"type": "table", "page_idx": 0, "table_body": "材料" * 100000 + "end-marker",
                                  "bbox": [0, 0, 1000, 1000]} for _ in range(15)])
    result = services.read_file(context, {"path": path, "limit": 1000})
    assert len(json.dumps(result, ensure_ascii=False).encode()) < 48 * 1024
    entries = [json.loads(line) for line in result["content"].split("\n")]
    assert 0 < len(entries) < 15 and result["next_offset"] == len(entries) + 1
    assert entries[0]["content_truncated"] is True
    assert entries[0]["full_text_path"].endswith("/document.md")
    assert entries[0]["source"]["original_path"] == record.original_path
    assert entries[0]["crop_source_path"] == record.page_image_paths[0]
    assert entries[0]["crop_box"] == [0, 0, 1, 1]
    assert len(json.loads((record.root / path).read_bytes())[0]["table_body"]) == 200010
    found = services.read_file(context, {"path": path, "query": "end-marker", "limit": 1})
    assert found["returned_line_count"] == 1
    assert json.loads(found["content"])["content_truncated"] is True
    assert "_search_lines" not in found


def test_unbound_content_filename_keeps_raw_read_semantics(tmp_path):
    services = services_for(tmp_path / "workspace")
    context, record = _research(services.workspace.paths.root, tmp_path)
    path = record.root / record.extracted_root / "content-list.json"
    path.write_text('[{"example": 1}]')
    result = services.read_file(context, {"path": path.relative_to(record.root).as_posix()})
    assert result["content"] == '1|[{"example": 1}]'
    assert "format" not in result


def test_partial_receipt_without_content_list_still_reports_actual_media_count():
    result = attachment_receipt_view({"content_list_path": None, "media_paths": ["images/a.png", "images/b.jpg"]})
    assert result["media_count"] == 2 and "content_list_view" not in result
