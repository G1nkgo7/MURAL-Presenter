"""补查统一解析契约中的结构缺口、安全引用与缓存身份边界。"""
import json
import os

import pytest

from runtime import attachments, mineru
from test_attachment_parse import api, corpus, stage


def _result(value):
    return next(iter(value["results"].values()))


@pytest.mark.parametrize("reference", [
    "../private.png", "/private.png", "images/../../private.png",
    "images\\private.png", "https://private.invalid/image.png",
])
def test_nested_image_references_reject_unsafe_paths_without_failover(tmp_path, corpus, api, reference):
    root, record = stage(tmp_path, corpus["pdf"])

    def mutate(value):
        data = _result(value)
        content = json.loads(data["content_list"])
        content[0]["table_body"] = f'<table><tr><td><img src="{reference}"></td></tr></table>'
        data["content_list"] = json.dumps(content)

    api[0]["mutate"] = mutate
    events = []
    receipt = attachments.parse_attachment(root, record.attachment_id, event_sink=events.append)
    assert receipt["status"] == "failed" and not receipt["failover"]
    assert receipt["unresolved_items"] == [{"reason": "mineru_unsafe_image_reference"}]
    assert api[1]["gets"] == api[1]["posts"] == 0
    assert reference not in json.dumps(events)


def test_server_partial_is_cached_and_never_selects_cpu(tmp_path, corpus, api):
    root, record = stage(tmp_path, corpus["pdf"])
    api[0]["mutate"] = lambda value: value.update(status="partial")
    receipt = attachments.parse_attachment(root, record.attachment_id)
    assert receipt["status"] == "partial" and not receipt["failover"]
    assert receipt["unresolved_items"] == [{"reason": "mineru_reported_partial"}]
    assert attachments.parse_attachment(root, record.attachment_id)["cached"]
    assert api[0]["posts"] == 1 and api[1]["gets"] == 0


def test_noncanonical_source_symlink_is_rejected_before_health(tmp_path, corpus, api):
    root, record = stage(tmp_path, corpus["pdf"])
    original = root / record.original_path
    original.unlink()
    original.symlink_to(corpus["pdf"])
    with pytest.raises(attachments.AttachmentError, match="路径|符号链接"):
        attachments.parse_attachment(root, record.attachment_id)
    assert api[0]["gets"] == api[1]["gets"] == 0


def test_same_endpoint_url_cannot_impersonate_the_other_backend_cache(tmp_path, corpus, api, monkeypatch):
    root, record = stage(tmp_path, corpus["pdf"])
    first = attachments.parse_attachment(root, record.attachment_id)
    from pathlib import Path
    path = Path(os.environ["MURAL_RUNTIME_CONFIG"])
    config = json.loads(path.read_text())
    config["mineru"]["endpoints"].reverse()
    path.write_text(json.dumps(config))
    api[0]["kind"], api[1]["kind"] = "cpu", "gpu"
    second = attachments.parse_attachment(root, record.attachment_id)
    assert not second["cached"] and second["text_path"] != first["text_path"]
    assert second["service_kind"] == "gpu" and api[1]["posts"] == 1
    assert (root / first["content_list_path"]).is_file()


@pytest.mark.parametrize("size", [float("nan"), float("inf")])
def test_nonfinite_page_geometry_is_invalid_schema_without_failover(tmp_path, corpus, api, size):
    root, record = stage(tmp_path, corpus["pdf"])

    def mutate(value):
        data = _result(value)
        middle = json.loads(data["middle_json"])
        middle["pdf_info"][0]["page_size"] = [size, 842]
        data["middle_json"] = json.dumps(middle)

    api[0]["mutate"] = mutate
    receipt = attachments.parse_attachment(root, record.attachment_id)
    assert receipt["status"] == "failed"
    assert receipt["unresolved_items"] == [{"reason": "mineru_invalid_page_geometry"}]
    assert not receipt["failover"] and api[1]["gets"] == 0


@pytest.mark.parametrize("missing_units", [(0, 1), (1,)])
def test_pptx_missing_native_tables_is_partial_without_failover(tmp_path, corpus, api, missing_units):
    root, record = stage(tmp_path, corpus["pptx"])

    def mutate(value):
        data = _result(value)
        data["content_list"] = json.dumps([
            item for item in json.loads(data["content_list"])
            if item.get("type") != "table" or item["page_idx"] not in missing_units
        ])

    api[0]["mutate"] = mutate
    receipt = attachments.parse_attachment(root, record.attachment_id)
    assert receipt["status"] == "partial"
    assert receipt["unresolved_items"] == [
        {"reason": "mineru_source_unit_table_missing", "granularity": "slide", "unit": index + 1}
        for index in missing_units
    ]
    assert not receipt["failover"] and api[1]["gets"] == 0


@pytest.mark.parametrize("missing_units", [(0, 1), (1,)])
def test_xlsx_data_sheet_missing_table_structure_is_partial(tmp_path, api, missing_units):
    from openpyxl import Workbook

    source = tmp_path / "cells.xlsx"
    book = Workbook()
    for sheet in (book.active, book.create_sheet("Summary")):
        if sheet == book.active:
            sheet.title = "Sales"
        sheet.append(["Metric", "Value"])
        sheet.append(["Revenue", 12345])
    book.save(source)
    root, record = stage(tmp_path, source)

    def mutate(value):
        data = _result(value)
        data["content_list"] = json.dumps([
            item for item in json.loads(data["content_list"])
            if item.get("type") != "table" or item["page_idx"] not in missing_units
        ])

    api[0]["mutate"] = mutate
    receipt = attachments.parse_attachment(root, record.attachment_id)
    assert receipt["status"] == "partial"
    assert receipt["unresolved_items"] == [
        {"reason": "mineru_source_unit_table_missing", "granularity": "worksheet", "unit": ("Sales", "Summary")[index]}
        for index in missing_units
    ]
    assert not receipt["failover"] and api[1]["gets"] == 0
