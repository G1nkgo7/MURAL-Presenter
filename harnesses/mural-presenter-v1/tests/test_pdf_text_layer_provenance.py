"""PDF 页级文本层来自原件索引；解析块的 type 或 OCR 标志不能替代来源证据。"""
from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw
import pytest

from runtime import attachments, mineru
from test_attachment_parse import api, corpus, stage


def _pdf(path, kinds):
    image = Image.new("RGB", (360, 240), "white")
    ImageDraw.Draw(image).text((20, 20), "Raster invoice 12345", fill="black", font_size=22)
    pixels = BytesIO()
    image.save(pixels, format="PNG")
    with pymupdf.open() as document:
        for kind in kinds:
            page = document.new_page(width=200, height=240)
            if kind in {"native", "mixed"}:
                page.insert_text((20, 35), "Native heading")
            if kind in {"scan", "mixed"}:
                page.insert_image(pymupdf.Rect(10, 70, 190, 190), stream=pixels.getvalue())
        document.save(path)
    return path


def _rows(root, receipt):
    view = attachments.attachment_content_view(root, receipt["content_list_path"])
    return view, [json.loads(line) for line in view["content"].split("\n")]


@pytest.mark.parametrize("kinds,expected", [
    (("native", "native"), [True, True]),
    (("scan", "scan"), [False, False]),
    (("native", "scan"), [True, False]),
    (("mixed", "scan"), [True, False]),
])
@pytest.mark.parametrize("ocr_enable", [True, False])
def test_pdf_page_facts_ignore_parser_block_type_and_global_ocr(tmp_path, api, monkeypatch, kinds, expected, ocr_enable):
    source = _pdf(tmp_path / "synthetic.pdf", kinds)
    probes = []
    get_text = pymupdf.Page.get_text
    def native_text(page, option="text", **kwargs):
        probes.append((page.number, option))
        return get_text(page, option, **kwargs)
    monkeypatch.setattr(pymupdf.Page, "get_text", native_text)
    monkeypatch.setattr(pymupdf.Page, "get_textpage_ocr", lambda *_a, **_kw: pytest.fail("index must not OCR"))
    monkeypatch.setattr(pymupdf.Page, "get_pixmap", lambda *_a, **_kw: pytest.fail("index must not rasterize"))
    root, record = stage(tmp_path, source)
    assert probes == [(0, "text"), (1, "text")]
    descriptors = [(root / path).with_suffix(".json") for path in record.page_image_paths]
    for path, present in zip(descriptors, expected):
        descriptor = json.loads(path.read_text())
        assert descriptor["native_text_present"] is present
        assert descriptor["text_mode"] == "unparsed" and descriptor["text_blocks"] == []
        assert "native_text" not in descriptor
    assert all(not (root / path).exists() for path in record.page_image_paths)
    assert record.text_path is None
    assert attachments.attachment_content_view(root, f"{record.extracted_root}/mineru/fixed/content-list.json") is None
    monkeypatch.setattr(pymupdf.Page, "get_text", lambda *_a, **_kw: pytest.fail("views and resume must reuse the page index"))
    def mutate(value):
        data = value["results"]["synthetic"]
        middle = json.loads(data["middle_json"])
        middle["_ocr_enable"] = ocr_enable
        data["middle_json"] = json.dumps(middle)
    api[0]["mutate"] = mutate
    receipt = attachments.parse_attachment(root, record.attachment_id)
    assert receipt["status"] == "completed" and not receipt["failover"]
    assert [item["native_text_present"] for item in receipt["coverage"]["locations"]] == expected
    before = {path: path.read_bytes() for path in [*descriptors, root / record.original_path,
              root / receipt["content_list_path"], root / "_trace/attachment-manifest.json"]}
    view, rows = _rows(root, receipt)
    for row in rows:
        location = row["source"]
        assert location["native_text_present"] is expected[location["page"] - 1]
        assert location["native_text_evidence_path"] == str(Path(location["page_image_path"]).with_suffix(".json"))
        assert "native_text_present" not in row
    assert all(row["type"] == "text" for row in rows[:2])
    assert "True does not prove any parsed block is native" in view["note"]
    assert "false means parsed words on that page are not native PDF text" in view["note"]
    assert all(path.read_bytes() == raw for path, raw in before.items())
    attachments.AttachmentStager(root).stage([source])
    monkeypatch.setattr(mineru, "_request", lambda *_a, **_kw: pytest.fail("resume must not reparse"))
    resumed = attachments.parse_attachment(root, record.attachment_id)
    assert resumed["cached"] and resumed["coverage"] == receipt["coverage"]
    assert _rows(root, resumed)[0] == view
    assert all(path.read_bytes() == before[path] for path in descriptors)
    assert api[0]["posts"] == 1 and api[1]["gets"] == api[1]["posts"] == 0


@pytest.mark.parametrize("missing", ["unparsed", "absent", "malformed", "wrong_source", "wrong_page",
                                   "wrong_image", "converted_page", "invalid_boolean", "mode_only"])
def test_missing_or_unbound_page_evidence_is_unknown_even_after_cached_parse(tmp_path, corpus, api, monkeypatch, missing):
    root, record = stage(tmp_path, corpus["pdf"])
    receipt = attachments.parse_attachment(root, record.attachment_id)
    path = (root / record.page_image_paths[1]).with_suffix(".json")
    descriptor = json.loads(path.read_text())
    if missing == "absent":
        path.unlink()
    elif missing == "malformed":
        path.write_text("not-json")
    else:
        descriptor.pop("native_text_present")
        if missing == "wrong_source":
            descriptor.update(native_text_present=True, source_path="inputs/originals/att-ffffffffffff/other.pdf")
        elif missing == "wrong_page":
            descriptor.update(native_text_present=True, page=1)
        elif missing == "wrong_image":
            descriptor.update(native_text_present=True, page_image=record.page_image_paths[0])
        elif missing == "converted_page":
            descriptor.update(native_text_present=True, backing_pdf="converted.pdf")
        elif missing == "invalid_boolean":
            descriptor["native_text_present"] = "false"
        elif missing == "mode_only":
            descriptor["text_mode"] = "native"
        path.write_text(json.dumps(descriptor))
    monkeypatch.setattr(pymupdf.Page, "get_text", lambda *_a, **_kw: pytest.fail("missing evidence must not trigger extraction"))
    monkeypatch.setattr(mineru, "_request", lambda *_a, **_kw: pytest.fail("missing evidence must not reparse"))
    resumed = attachments.parse_attachment(root, record.attachment_id)
    assert resumed["cached"] and not resumed["failover"]
    assert resumed["coverage"]["locations"][0]["native_text_present"] is True
    assert resumed["coverage"]["locations"][1]["native_text_present"] is None
    view, rows = _rows(root, resumed)
    assert rows[1]["source"]["page"] == 2 and rows[1]["source"]["native_text_present"] is None
    assert rows[1]["source"]["native_text_evidence_path"] is None
    assert "null means no page evidence" in view["note"]
    assert api[0]["posts"] == 1 and api[1]["gets"] == api[1]["posts"] == 0


def test_existing_materialized_native_evidence_and_sparse_page_order_are_reused(tmp_path, corpus, api, monkeypatch):
    root, record = stage(tmp_path, corpus["pdf"])
    receipt = attachments.parse_attachment(root, record.attachment_id)
    for relative in record.page_image_paths:
        image = attachments.materialize_page_image(root, relative)
        descriptor_path = image.with_suffix(".json")
        descriptor = json.loads(descriptor_path.read_text())
        present = descriptor.pop("native_text_present")
        assert present is bool(descriptor["native_text"].strip())
        descriptor_path.write_text(json.dumps(descriptor))
    monkeypatch.setattr(pymupdf.Page, "get_text", lambda *_a, **_kw: pytest.fail("existing native evidence must be reused"))
    sparse = replace(record, page_image_paths=(record.page_image_paths[1],))
    locations = attachments.attachment_receipt_view(receipt, record=sparse)["coverage"]["locations"]
    assert locations[0]["native_text_present"] is None
    assert locations[1]["native_text_present"] is False
    _, rows = _rows(root, receipt)
    assert rows[0]["source"]["native_text_present"] is True
    assert rows[1]["source"]["native_text_present"] is False


def test_text_layer_evidence_keeps_existing_attachment_path_and_source_identity_boundaries(tmp_path, corpus, api):
    root, record = stage(tmp_path, corpus["pdf"])
    receipt = attachments.parse_attachment(root, record.attachment_id)
    wrong_identity = {**receipt, "source_sha256": "0" * 64}
    unknown = attachments.attachment_receipt_view(wrong_identity, record=record)
    assert all(item["native_text_present"] is None for item in unknown["coverage"]["locations"])
    descriptor = (root / record.page_image_paths[0]).with_suffix(".json")
    outside = tmp_path / "outside.json"
    outside.write_bytes(descriptor.read_bytes())
    descriptor.unlink()
    descriptor.symlink_to(outside)
    with pytest.raises(attachments.AttachmentError):
        _rows(root, receipt)


@pytest.mark.parametrize("ext", ["png", "jpg", "docx", "pptx", "xlsx"])
def test_non_pdf_locations_do_not_claim_native_pdf_text(tmp_path, corpus, api, ext):
    root, record = stage(tmp_path, corpus[ext])
    receipt = attachments.parse_attachment(root, record.attachment_id)
    _, rows = _rows(root, receipt)
    for location in [*receipt["coverage"]["locations"], *(row["source"] for row in rows)]:
        assert "native_text_present" not in location and "native_text_evidence_path" not in location
