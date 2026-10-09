"""Local source-pixel PDF capture tests; no external endpoints or model calls."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

import fitz
from PIL import Image
import pytest

from runtime import web_browser


@pytest.fixture
def pdf_bytes():
    with fitz.open() as document:
        for color, text in [((1, 0, 0), "First physical page"),
                            ((0, 1, 0), "Literal atlas statement"),
                            ((0, 0, 1), "Third physical page")]:
            page = document.new_page(width=300, height=600)
            page.draw_rect(page.rect, color=color, fill=color)
            page.insert_text((20, 40), text)
        document.set_metadata({"title": "Authentic fixture atlas"})
        return document.tobytes()


def capture(tmp_path, body, *, fragment="", query="", deadline=None):
    output = tmp_path / "capture.png"
    receipt = web_browser._render_pdf(body, "https://example.test/atlas.pdf" + fragment,
        "https://example.test/final.pdf", output, query=query,
        deadline=time.monotonic() + 5 if deadline is None else deadline)
    return output, receipt


def test_chosen_physical_page_preserves_pixels_and_receipt(tmp_path, pdf_bytes):
    output, receipt = capture(tmp_path, pdf_bytes, fragment="#page=3", query="absent text")
    assert receipt == {"url": "https://example.test/atlas.pdf#page=3",
        "effective_url": "https://example.test/final.pdf", "title": "Authentic fixture atlas",
        "query": "absent text", "viewport": {"width": 1280, "height": 900},
        "capture_scope": "pdf_page", "source_page": 3, "source_page_count": 3,
        "source_doc_sha256": hashlib.sha256(pdf_bytes).hexdigest(), "resource_warnings": []}
    with Image.open(output) as image:
        assert image.size == (1280, 900)
        assert image.getpixel((640, 450)) == (0, 0, 255)
        assert image.getpixel((0, 450)) == (255, 255, 255)
        assert image.getpixel((414, 450)) == (255, 255, 255)
        assert image.getpixel((416, 450)) == (0, 0, 255)


@pytest.mark.parametrize("query,number,color", [("", 1, (255, 0, 0)),
    ("Literal atlas", 2, (0, 255, 0))])
def test_default_and_literal_query_page(tmp_path, pdf_bytes, query, number, color):
    output, receipt = capture(tmp_path, pdf_bytes, query=query)
    assert receipt["source_page"] == number
    with Image.open(output) as image:
        assert image.getpixel((640, 450)) == color


@pytest.mark.parametrize("fragment,query,code", [
    ("", "literal atlas", "web_extract_query_not_found"),
    ("", "invented statement", "web_extract_query_not_found"),
    ("#page=0", "", "web_extract_pdf_page_invalid"),
    ("#page=4", "", "web_extract_pdf_page_invalid"),
    ("#page=wrong", "", "web_extract_pdf_page_invalid"),
    ("#page=1&page=2", "", "web_extract_pdf_page_invalid"),
])
def test_nonmatch_and_invalid_page_fail_closed(tmp_path, pdf_bytes, fragment, query, code):
    with pytest.raises(web_browser.WebBrowserError) as error:
        capture(tmp_path, pdf_bytes, fragment=fragment, query=query)
    assert error.value.code == code
    assert not (tmp_path / "capture.png").exists()


def test_scanned_page_has_no_invented_text_match(tmp_path):
    with fitz.open() as document:
        document.new_page().draw_rect(fitz.Rect(0, 0, 100, 100), fill=(0, 1, 0))
        body = document.tobytes()
    with pytest.raises(web_browser.WebBrowserError) as error:
        capture(tmp_path, body, query="atlas")
    assert error.value.code == "web_extract_query_not_found"


@pytest.mark.parametrize("encrypted", [False, True])
def test_broken_and_encrypted_pdf(tmp_path, pdf_bytes, encrypted):
    body = b"%PDF-1.7\nbroken"
    if encrypted:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
            body = document.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256,
                owner_pw="owner", user_pw="reader")
    with pytest.raises(web_browser.WebBrowserError) as error:
        capture(tmp_path, body)
    assert error.value.code == ("web_extract_pdf_encrypted" if encrypted else "web_extract_pdf_invalid")
    assert not (tmp_path / "capture.png").exists()


def test_expired_deadline(tmp_path, pdf_bytes):
    with pytest.raises(web_browser.WebBrowserError) as error:
        capture(tmp_path, pdf_bytes, deadline=time.monotonic() - 1)
    assert error.value.code == "web_extract_timeout"
    assert not (tmp_path / "capture.png").exists()


@pytest.mark.parametrize("content_type", ["application/pdf; charset=binary", "application/octet-stream"])
def test_loopback_navigation_interception_uses_single_pinned_fetch(tmp_path, monkeypatch, pdf_bytes, content_type):
    counts = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            counts.append(self.path)
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/atlas.pdf")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Disposition", 'attachment; filename="atlas.pdf"')
            self.end_headers()
            self.wfile.write(pdf_bytes)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    path = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1/scripts/_internal/remote_resources.py"
    module = web_browser._resource_module(path)
    original = module.validate_url
    def validate(url):
        parsed = urlsplit(url)
        if parsed.hostname == "127.0.0.1" and parsed.port == server.server_port:
            return parsed, parsed.hostname, parsed.port
        return original(url)
    monkeypatch.setattr(module, "validate_url", validate)
    monkeypatch.setattr(web_browser, "_resource_module", lambda _path: module)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        output = tmp_path / "navigation.png"
        receipt = web_browser.render_html(base + "/redirect#page=2", output,
            timeout_s=10, max_bytes=len(pdf_bytes) + 100, screenshot=True, resource_module=path)
        assert receipt["capture_scope"] == "pdf_page" and receipt["source_page"] == 2
        assert receipt["effective_url"] == base + "/atlas.pdf"
        assert counts == ["/redirect", "/atlas.pdf"]
        with Image.open(output) as image:
            assert image.size == (1280, 900) and image.getpixel((640, 450)) == (0, 255, 0)
        with pytest.raises(web_browser.WebBrowserError) as error:
            web_browser.render_html(base + "/atlas.pdf", tmp_path / "too-large.png",
                timeout_s=10, max_bytes=100, screenshot=True, resource_module=path)
        assert error.value.code == "web_extract_too_large"
        assert not (tmp_path / "too-large.png").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
