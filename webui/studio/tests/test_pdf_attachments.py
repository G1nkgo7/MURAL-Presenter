import fitz

from app import attachments


def _text_pdf(text: str) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def test_pdf_text_extraction_uses_pypdf():
    pages = attachments._extract_pdf_text(_text_pdf("RAVE primary parser"))

    assert pages == ["[Page 1]\nRAVE primary parser"]


def test_pdf_text_extraction_falls_back_to_pymupdf(monkeypatch):
    def fail_pypdf(_data: bytes) -> list[str]:
        raise ModuleNotFoundError("simulated missing pypdf")

    monkeypatch.setattr(attachments, "_extract_pdf_text_pypdf", fail_pypdf)

    pages = attachments._extract_pdf_text(_text_pdf("RAVE fallback parser"))

    assert pages == ["[Page 1]\nRAVE fallback parser"]


def test_pdf_text_extraction_falls_back_when_pypdf_returns_no_text(monkeypatch):
    monkeypatch.setattr(attachments, "_extract_pdf_text_pypdf", lambda _data: [])

    pages = attachments._extract_pdf_text(_text_pdf("RAVE empty-result fallback"))

    assert pages == ["[Page 1]\nRAVE empty-result fallback"]
