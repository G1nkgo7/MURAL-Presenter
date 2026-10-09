"""原页预览是少量视觉核验，不是另一个全文提取器。"""
import hashlib
import json
import shutil

import pytest

from runtime import attachments, mineru
from test_attachment_parse import corpus, stage


@pytest.mark.parametrize("ext", ["docx", "pptx", "xlsx"])
def test_office_lazy_original_page_preview(tmp_path, corpus, monkeypatch, ext):
    if not shutil.which("libreoffice"):
        pytest.skip("requires LibreOffice for original-page preview only")
    monkeypatch.setattr(mineru, "_request", lambda *_a, **_kw: pytest.fail("preview is not full parsing"))
    root, record = stage(tmp_path, corpus[ext])
    assert record.page_image_paths == () and record.text_path is None
    original_hash = hashlib.sha256((root / record.original_path).read_bytes()).hexdigest()
    path = f"{record.extracted_root}/pages/page_001.png"
    page = attachments.materialize_page_image(root, path)
    assert page.is_file()
    descriptor = json.loads(page.with_suffix(".json").read_text())
    assert descriptor["source_path"] == record.original_path
    assert descriptor["location_kind"] == "converted_page"
    assert not (root / record.extracted_root / "text.md").exists()
    assert not list((root / "_runtime/tmp/office-preview").iterdir())
    assert hashlib.sha256((root / record.original_path).read_bytes()).hexdigest() == original_hash
    before = page.stat().st_mtime_ns
    attachments.AttachmentStager(root).stage([corpus[ext]])
    assert attachments.materialize_page_image(root, path).stat().st_mtime_ns == before


def test_xlsx_formula_without_cached_value_is_reported_at_real_cell(corpus):
    info = mineru.source_info(corpus["xlsx"])
    assert info["units"] == ["Sales", "Summary"]
    assert info["unresolved_items"] == [
        {"reason": "formula_has_no_cached_value", "worksheet": "Sales", "cell": "B4"},
        {"reason": "formula_has_no_cached_value", "worksheet": "Summary", "cell": "B4"}]


@pytest.mark.parametrize("path", ["../outside.png", "inputs/extracted/att-ffffffffffff/pages/page_001.png"])
def test_preview_does_not_escape_or_invent_attachment(tmp_path, path):
    with pytest.raises((attachments.AttachmentError, FileNotFoundError)):
        attachments.materialize_page_image(tmp_path, path)
