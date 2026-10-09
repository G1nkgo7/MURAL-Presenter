"""附件 originals/extracted v2 布局、身份/去重/resume 与统一解析器的机械合同。"""
from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import pytest

from runtime.attachments import (
    ATTACHMENT_LAYOUT_VERSION,
    ATTACHMENT_SCHEMA_VERSION,
    AttachmentStager,
    materialize_page_image,
    resolve_attachment_records,
    resolve_extracted_text,
    resolve_original_path,
    resolve_page_assets,
)


def _txt(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _pdf(path: Path, pages: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import pymupdf as fitz
    except ImportError:  # pragma: no cover - env guard
        import fitz  # type: ignore[no-redef]
    document = fitz.open()
    for text in pages:
        document.new_page().insert_text((72, 72), text)
    document.save(path)
    document.close()
    return path


def _png(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    from PIL import Image

    Image.new("RGB", (48, 32), (10, 120, 200)).save(path)
    return path


def test_single_pdf_produces_originals_extracted_layout(tmp_path: Path) -> None:
    pdf = _pdf(tmp_path / "src" / "report.pdf", ["Alpha", "Beta"])
    ws = tmp_path / "ws"
    (ws / "_state").mkdir(parents=True)
    manifest = AttachmentStager(ws).stage([pdf]).value
    assert manifest["schema_version"] == ATTACHMENT_SCHEMA_VERSION
    assert manifest["layout_version"] == ATTACHMENT_LAYOUT_VERSION
    assert manifest["input_set_id"].startswith("attset-")
    record = resolve_attachment_records(ws)[0]
    assert record.attachment_id.startswith("att-")
    original = resolve_original_path(record)
    assert original.parent.parent.name == "originals" and original.is_file()
    text = resolve_extracted_text(record)
    assert text is None  # 全文解析由 Material 显式请求。
    assert "extracted" in record.extracted_root
    assert len(resolve_page_assets(record)) == 2


def test_pdf_and_office_descriptors_preserve_rotated_layout_blocks(tmp_path):
    import pymupdf as fitz
    from PIL import Image

    source = tmp_path / "layout.pdf"
    with fitz.open() as doc:
        page = doc.new_page(width=600, height=800)
        page.insert_text((80, 120), "Boundary text")
        page.draw_rect(fitz.Rect(60, 160, 180, 250))
        payload = BytesIO()
        Image.new("RGB", (100, 80), "blue").save(payload, format="PNG")
        page.insert_image(fitz.Rect(200, 160, 400, 300), stream=payload.getvalue())
        page.set_rotation(90)
        doc.save(source)
    root = tmp_path / "workspace"
    stager = AttachmentStager(root)
    stager.stage([source])
    record = resolve_attachment_records(root)[0]
    logical = resolve_page_assets(record)[0]
    materialize_page_image(root, record.page_image_paths[0])
    layout = json.loads(logical.with_suffix(".json").read_text())
    assert layout["page_points"] == [800, 600]
    assert layout["text_blocks"][0]["text"] == "Boundary"
    assert "ocr_status" not in layout
    assert layout["image_blocks"] and layout["drawing_blocks"]
    x0, y0, x1, y1 = layout["text_blocks"][0]["bbox_pdf"]
    assert x0 > 650 and y0 == 80 and x1 > x0 and y1 > y0
    office_pages = root / "inputs" / "office-pages"
    office_pages.mkdir()
    paths = stager._pdf_descriptors_from_backing(resolve_original_path(record), resolve_original_path(record), office_pages)
    materialize_page_image(root, paths[0])
    converted = json.loads((root / paths[0]).with_suffix(".json").read_text())
    for key in ("text_blocks", "image_blocks", "drawing_blocks", "page_points"):
        assert converted[key] == layout[key]


def test_manifest_has_no_absolute_or_temp_paths(tmp_path: Path) -> None:
    pdf = _pdf(tmp_path / "src" / "h.pdf", ["only"])
    png = _png(tmp_path / "src" / "h.png")
    ws = tmp_path / "ws"
    (ws / "_state").mkdir(parents=True)
    manifest = AttachmentStager(ws).stage([pdf, png]).value
    blob = json.dumps(manifest, ensure_ascii=False)
    assert str(tmp_path) not in blob
    assert "attachment-ocr" not in blob
    assert "/tmp/" not in blob


def test_same_name_different_content_gets_distinct_ids(tmp_path: Path) -> None:
    first = _txt(tmp_path / "a" / "note.txt", "content ONE")
    second = _txt(tmp_path / "b" / "note.txt", "content TWO")
    ws = tmp_path / "ws"
    (ws / "_state").mkdir(parents=True)
    AttachmentStager(ws).stage([first, second])
    records = resolve_attachment_records(ws)
    assert records[0].attachment_id != records[1].attachment_id
    assert resolve_original_path(records[0]).read_text() == "content ONE"
    assert resolve_original_path(records[1]).read_text() == "content TWO"


def test_same_content_twice_reuses_single_identity(tmp_path: Path) -> None:
    first = _txt(tmp_path / "a" / "same.txt", "identical")
    second = _txt(tmp_path / "b" / "same.txt", "identical")
    ws = tmp_path / "ws"
    (ws / "_state").mkdir(parents=True)
    manifest = AttachmentStager(ws).stage([first, second]).value
    records = resolve_attachment_records(ws)
    assert len({record.attachment_id for record in records}) == 1
    assert len(manifest["active_attachment_ids"]) == 1


def test_same_pdf_bytes_with_different_names_keep_one_canonical_parse_on_resume(tmp_path, monkeypatch):
    from runtime import attachments

    first = _pdf(tmp_path / "first.pdf", ["Native source content"])
    renamed = tmp_path / "renamed.pdf"
    renamed.write_bytes(first.read_bytes())
    parses = []
    original_extract = AttachmentStager._index_pdf
    def extract(self, source, output):
        parses.append(source)
        return original_extract(self, source, output)
    monkeypatch.setattr(AttachmentStager, "_index_pdf", extract)
    root = tmp_path / "workspace"
    AttachmentStager(root).stage([first, renamed])
    records = resolve_attachment_records(root)
    assert len(parses) == 1
    assert [record.original_name for record in records] == ["first.pdf", "renamed.pdf"]
    assert records[0].original_path == records[1].original_path
    descriptor_path = (root / records[0].page_image_paths[0]).with_suffix(".json")
    assert json.loads(descriptor_path.read_text())["source_path"] == records[0].original_path
    materialize_page_image(root, records[0].page_image_paths[0])
    before = descriptor_path.read_bytes()
    AttachmentStager(root).stage([renamed, first])
    resumed = resolve_attachment_records(root)
    assert len(parses) == 1 and descriptor_path.read_bytes() == before
    assert all(record.original_path == records[0].original_path for record in resumed)
    assert materialize_page_image(root, resumed[0].page_image_paths[0]).is_file()


def test_resume_same_hash_does_not_reparse(tmp_path: Path) -> None:
    pdf = _pdf(tmp_path / "src" / "r.pdf", ["one", "two", "three"])
    ws = tmp_path / "ws"
    (ws / "_state").mkdir(parents=True)
    first = AttachmentStager(ws).stage([pdf]).value
    text = resolve_page_assets(resolve_attachment_records(ws)[0])[0].with_suffix(".json")
    before = text.stat().st_mtime_ns
    second = AttachmentStager(ws).stage([pdf]).value
    after = resolve_page_assets(resolve_attachment_records(ws)[0])[0].with_suffix(".json").stat().st_mtime_ns
    assert before == after
    assert first["created_at"] == second["created_at"]
    assert len(resolve_attachment_records(ws)) == 1


def test_resume_retries_failed_index_without_parsing_content(tmp_path, monkeypatch):
    source = _pdf(tmp_path / "source.pdf", ["First", "Second"])
    root = tmp_path / "workspace"
    original = AttachmentStager._index_pdf
    def interrupted(*_args):
        raise RuntimeError("interrupted index")
    monkeypatch.setattr(AttachmentStager, "_index_pdf", interrupted)
    assert AttachmentStager(root).stage([source]).value["unresolved_items"]
    monkeypatch.setattr(AttachmentStager, "_index_pdf", original)
    assert not AttachmentStager(root).stage([source]).value["unresolved_items"]
    record = resolve_attachment_records(root)[0]
    assert len(resolve_page_assets(record)) == 2 and resolve_extracted_text(record) is None


def test_interrupt_then_resume_keeps_single_record(tmp_path: Path) -> None:
    note = _txt(tmp_path / "src" / "n.txt", "hello")
    ws = tmp_path / "ws"
    (ws / "_state").mkdir(parents=True)
    for _ in range(3):
        AttachmentStager(ws).stage([note])
    assert len(resolve_attachment_records(ws)) == 1


def test_changed_content_makes_new_dir_preserving_old(tmp_path: Path) -> None:
    path = _txt(tmp_path / "src" / "changing.txt", "original body")
    ws = tmp_path / "ws"
    (ws / "_state").mkdir(parents=True)
    first_id = None
    first_id = AttachmentStager(ws).stage([path]).value["attachments"][0]["attachment_id"]
    path.write_text("MUTATED body", encoding="utf-8")
    second_id = AttachmentStager(ws).stage([path]).value["attachments"][0]["attachment_id"]
    assert first_id != second_id
    old = ws / "inputs" / "originals" / first_id / "changing.txt"
    assert old.is_file() and old.read_text() == "original body"


def test_resolve_page_assets_materialize_on_demand(tmp_path: Path) -> None:
    pdf = _pdf(tmp_path / "src" / "m.pdf", ["p1", "p2"])
    ws = tmp_path / "ws"
    (ws / "_state").mkdir(parents=True)
    AttachmentStager(ws).stage([pdf])
    record = resolve_attachment_records(ws)[0]
    pages = resolve_page_assets(record)
    assert pages and not any(page.exists() for page in pages)
    relative = record.page_image_paths[0]
    materialized = materialize_page_image(ws, relative)
    assert materialized.is_file() and materialized.stat().st_size > 0
    # resume must not drop the logical page set after materialization.
    AttachmentStager(ws).stage([pdf])
    assert resolve_attachment_records(ws)[0].page_image_paths == record.page_image_paths


def test_ordinal_only_carries_order_not_identity(tmp_path: Path) -> None:
    one = _txt(tmp_path / "src" / "one.txt", "first")
    two = _txt(tmp_path / "src" / "two.txt", "second")
    ws_a = tmp_path / "wsa"
    ws_b = tmp_path / "wsb"
    (ws_a / "_state").mkdir(parents=True)
    (ws_b / "_state").mkdir(parents=True)
    ids_a = [r.attachment_id for r in _staged(ws_a, [one, two])]
    ids_b = [r.attachment_id for r in _staged(ws_b, [two, one])]
    assert set(ids_a) == set(ids_b)
    assert [r.ordinal for r in _staged(ws_a, [one, two])] == [1, 2]


def _staged(ws: Path, files: list[Path]):
    AttachmentStager(ws).stage(files)
    return resolve_attachment_records(ws)


@pytest.mark.parametrize("budget", ["10000", "500", "0"])
def test_direct_text_stays_complete_without_an_automatic_knowledge_ledger(tmp_path, monkeypatch, budget):
    monkeypatch.setenv("ATTACHMENT_TEXT_BUDGET_CHARS", budget)
    body = "A" * 4000 + "TAILMARK" + "B" * 4000
    source = _txt(tmp_path / "src" / "evidence.txt", body)
    workspace = tmp_path / "workspace"
    manifest = AttachmentStager(workspace).stage([source]).value
    original = manifest["attachments"][0]["original_path"]
    assert (workspace / original).read_text() == body
    assert manifest["attachment_paths"] == [original]
    assert manifest["input_stage"] == "direct_text"
    assert not (workspace / "material.md").exists() and not (workspace / "knowledge.md").exists()
    assert not any(key.startswith("material_") for key in manifest)
