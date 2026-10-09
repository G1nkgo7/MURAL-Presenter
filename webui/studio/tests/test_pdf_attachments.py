import hashlib
import io
from pathlib import Path
from unittest import mock

from fastapi import UploadFile
import pytest

from app import attachments
from app import main


@pytest.mark.parametrize("suffix", ["pdf", "docx", "pptx", "xlsx", "csv", "png"])
def test_upload_only_saves_original_bytes_and_identity(tmp_path, monkeypatch, suffix):
    monkeypatch.setattr(attachments.engine, "deck_uploads_dir", lambda _id: tmp_path)
    data = b"unaltered original bytes\x00\xff"
    name = f"report.{suffix}"
    records = attachments.store_attachments(1, [UploadFile(filename=name, file=io.BytesIO(data))])
    assert (tmp_path / name).read_bytes() == data
    assert list(tmp_path.iterdir()) == [tmp_path / name]
    assert records[0]["source_sha256"] == hashlib.sha256(data).hexdigest()
    assert not any(key in records[0] for key in ("text", "images", "parse_status", "truncated"))
    assert attachments.raw_attachments(records)[0]["source_path"] == str(tmp_path / name)


def test_attachment_handoff_preserves_query_without_modes_or_prompt_injection(tmp_path, monkeypatch):
    monkeypatch.setattr(attachments.engine, "deck_uploads_dir", lambda _id: tmp_path)
    query = "用户的原始要求\n不追加附件指令。"
    seed = {"query": query, "user_query": query}
    con = mock.Mock()
    main._apply_deck_attachments(con, 1, query, seed,
        [UploadFile(filename="source.pdf", file=io.BytesIO(b"original pdf"))])
    assert seed["query"] == seed["user_query"] == query
    assert len(seed["_raw_attachments"]) == 1
    assert "attachment_mode" not in seed and "_attachment_images" not in seed
    assert seed["attachments"][0]["path"] == str(tmp_path / "source.pdf")
    con.execute.assert_called_once()


def test_oversized_upload_is_not_saved_or_staged(tmp_path, monkeypatch):
    monkeypatch.setattr(attachments.engine, "deck_uploads_dir", lambda _id: tmp_path)
    monkeypatch.setattr(attachments, "MAX_FILE_BYTES", 2)
    records = attachments.store_attachments(1, [UploadFile(filename="big.pdf", file=io.BytesIO(b"too big"))])
    assert list(tmp_path.iterdir()) == []
    assert attachments.raw_attachments(records) == []
    assert records[0]["path"] == "" and records[0]["notes"]


def test_pipeline_pdf_preview_uses_dedicated_attachment_route():
    seed = {
        "attachments": [{
            "name": "report.pdf",
            "stored_name": "report.pdf",
            "type": "pdf",
            "size": 123,
            "images": [],
        }],
    }

    payload = main._progress_seed_payload(seed)

    assert payload["attachments"] == [{
        "index": 0,
        "name": "report.pdf",
        "stored_name": "report.pdf",
        "type": "pdf",
        "size": 123,
        "preview_rel": "",
    }]


def test_frontend_prefers_the_dedicated_attachment_endpoint():
    source = (
        Path(__file__).resolve().parents[1] / "static" / "app.js"
    ).read_text(encoding="utf-8")

    assert "url: suppliedUrl || dedicatedUrl" in source
    assert "fallbackUrl: suppliedUrl ? dedicatedUrl : workspaceUrl" in source
