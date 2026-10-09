"""网页全文缓存与展示窗口分离；无网络、浏览器或模型调用。"""
from hashlib import sha256
import json
from urllib.parse import parse_qs, urlsplit

import pymupdf
import pytest

from runtime.web_extract import WebExtractError, extract_web_document
from state.workspace import WorkspaceState


@pytest.fixture
def workspace(tmp_path):
    return WorkspaceState(tmp_path).paths.root


def extract(workspace, data, *, url="https://example.test/report", max_chars=12000, max_bytes=4 * 1024 * 1024):
    return extract_web_document(workspace, url, max_chars=max_chars, max_bytes=max_bytes, download=lambda: data)


def test_html_retains_title_headings_lists_tables_links_and_images(workspace):
    source = b'''<html><head><title>Evidence report</title><base href="/sources/">
      <style>PRIVATE_STYLE</style><script>PRIVATE_SCRIPT</script></head><body>
      <h1>Overview</h1><h2>Findings</h2><p>First paragraph.</p><p>Second paragraph.</p>
      <ul><li>Alpha<ul><li>Nested</li></ul></li><li>Beta</li></ul>
      <ol><li>Step one</li><li>Step two</li></ol>
      <table><tr><th>Year</th><th>Value</th></tr><tr><td>2026</td><td>42</td></tr></table>
      <a href="evidence">Source evidence</a><img src="chart.png" alt="Trend chart">
      <noscript>PRIVATE_NOSCRIPT</noscript></body></html>'''
    result = extract(workspace, source)
    text = (workspace / result["content_path"]).read_text()
    for expected in ("# Evidence report", "# Overview", "## Findings", "- Alpha", "Nested", "- Beta",
                     "1. Step one", "2. Step two", "| Year | Value |", "| 2026 | 42 |",
                     "[Source evidence](https://example.test/sources/evidence)",
                     "![Trend chart](https://example.test/sources/chart.png)"):
        assert expected in text
    assert "First paragraph.\n\nSecond paragraph." in text
    assert all(value not in text for value in ("PRIVATE_STYLE", "PRIVATE_SCRIPT", "PRIVATE_NOSCRIPT"))
    assert result["source_sha256"] == sha256(source).hexdigest()
    assert result["content_sha256"] == sha256(text.encode()).hexdigest()
    assert not result["truncated"] and result["format"] == "html"


@pytest.mark.parametrize("wrapper", ["main", 'div role="main"', "article"])
def test_main_content_omits_navigation_but_complete_body_remains_readable(workspace, wrapper):
    tag = wrapper.split()[0]
    source = f'''<title>Report</title><body><nav>Site navigation</nav><{wrapper}>
      <h2>Key finding</h2><nav>Article navigation</nav><p>Core evidence.</p>
      <table><tr><th>Year</th><th>Value</th></tr><tr><td>2026</td><td>42</td></tr></table>
      <figure><img src="chart.png" alt="Trend"><figcaption>Sample boundary</figcaption></figure>
      <a href="/source">Original evidence</a></{tag}><aside>Recommended reading</aside>
      <footer>Outside caveat https://example.test/note?token=SECRET&amp;id=9</footer></body>'''.encode()
    first = extract(workspace, source)
    for kept in ("## Key finding", "Core evidence", "| 2026 | 42 |", "Sample boundary",
                 "![Trend](https://example.test/chart.png)", "[Original evidence](https://example.test/source)"):
        assert kept in first["content"]
    assert all(noise not in first["content"] for noise in ("Site navigation", "Article navigation", "Recommended reading"))
    full = (workspace / first["full_body_path"]).read_bytes()
    assert "Outside caveat" in full.decode() and b"Recommended reading" in full
    assert b"SECRET" in full and first["full_body_sha256"] == sha256(full).hexdigest()
    assert first["extraction_scope"] in {"main", "article"}
    resumed = extract(workspace, b"must not overwrite saved source")
    assert resumed["cached"] and resumed["content"] == first["content"]
    assert resumed["full_body_path"] == first["full_body_path"]


def test_article_inside_main_retains_caption_and_excludes_recommendations(workspace):
    result = extract(workspace, b'<main><article><h1>Evidence</h1><p>Observation</p></article>'
                     b'<aside>Recommendations</aside></main>')
    assert result["extraction_scope"] == "article" and "Recommendations" not in result["content"]
    assert "Recommendations" in (workspace / result["full_body_path"]).read_text()


@pytest.mark.parametrize("source", [
    b'<div><h1>Report</h1><p>Evidence</p></div><aside>Useful limitations</aside>',
    b'<article>First evidence</article><article>Second evidence</article>',
    b'<main>First evidence</main><main>Second evidence</main>',
    b'<main><nav>Navigation only</nav></main><p>Evidence outside</p>',
])
def test_ambiguous_or_empty_main_returns_explicit_complete_body(workspace, source):
    result = extract(workspace, source)
    assert result["extraction_scope"] == "full_body"
    assert "complete body" in result["extraction_notice"]
    assert result["full_body_path"] == result["content_path"]
    assert len(list((workspace / "_trace/web-extract").iterdir())) == 2
    if b"Second evidence" in source:
        assert "First evidence" in result["content"] and "Second evidence" in result["content"]


@pytest.mark.parametrize("damage", ["change", "missing", "symlink"])
def test_main_cache_validates_complete_body_without_silent_refetch(workspace, damage):
    result = extract(workspace, b'<nav>Navigation</nav><main><p>Evidence</p></main>')
    full = workspace / result["full_body_path"]
    if damage == "change":
        full.write_text("changed")
    else:
        full.unlink()
        if damage == "symlink":
            full.symlink_to(workspace / result["content_path"])
    with pytest.raises(WebExtractError) as failed:
        extract(workspace, b"new version")
    assert failed.value.code == "web_extract_cache_invalid"


@pytest.mark.parametrize("scope", ["main", "article"])
@pytest.mark.parametrize("damage", ["missing", "empty"])
def test_selected_content_cannot_masquerade_as_complete_body(workspace, scope, damage):
    result = extract(workspace, f'<nav>Source caveat</nav><{scope}>Evidence</{scope}>'.encode())
    metadata_path = workspace / result["metadata_path"]
    metadata = json.loads(metadata_path.read_text())
    if damage == "missing":
        del metadata["full_body_sha256"]
    else:
        metadata["full_body_sha256"] = ""
    metadata_path.write_text(json.dumps(metadata))
    before = metadata_path.read_bytes()
    def forbidden():
        pytest.fail("invalid completeness must not silently refetch evidence")
    with pytest.raises(WebExtractError) as failed:
        extract_web_document(workspace, "https://example.test/report", max_chars=200,
                             max_bytes=1024, download=forbidden)
    assert failed.value.code == "web_extract_cache_invalid"
    assert metadata_path.read_bytes() == before


def test_cached_complete_body_without_selection_metadata_still_resumes(workspace):
    result = extract(workspace, b'<p>Complete source</p><aside>Important caveat</aside>')
    metadata_path = workspace / result["metadata_path"]
    metadata = json.loads(metadata_path.read_text())
    del metadata["extraction_scope"], metadata["extraction_notice"]
    metadata_path.write_text(json.dumps(metadata))
    resumed = extract(workspace, b"must not replace this evidence")
    assert resumed["cached"] and resumed["full_body_path"] == resumed["content_path"]
    assert resumed["extraction_scope"] == "full_body" and "Important caveat" in resumed["content"]


def test_full_bounded_document_survives_excerpt_and_resume_with_a_larger_window(workspace):
    source = ("<h1>Long source</h1>" + "<p>Evidence paragraph.</p>" * 5000 + "<p>TAIL_SENTINEL</p>").encode()
    calls = []
    def download():
        calls.append(True)
        return source
    first = extract_web_document(workspace, "https://example.test/report#first", max_chars=80,
        max_bytes=len(source), download=download)
    assert first["truncated"] and "TAIL_SENTINEL" not in first["content"]
    assert "TAIL\\_SENTINEL" in (workspace / first["content_path"]).read_text()
    resumed = WorkspaceState(workspace).paths.root
    second = extract_web_document(resumed, "https://example.test/report#second", max_chars=1000000,
        max_bytes=len(source), download=download)
    assert second["cached"] and not second["truncated"] and "TAIL\\_SENTINEL" in second["content"]
    assert first["content_path"] == second["content_path"] and calls == [True]
    assert second["characters"] == len(second["content"])
    assert len(list((workspace / "_trace/web-extract").iterdir())) == 2
    assert all((workspace / second[name]).stat().st_mode & 0o444 == 0o444
               for name in ("content_path", "metadata_path"))


def test_complete_pdf_extracts_after_page_fifty_and_beyond_display_budget(workspace):
    with pymupdf.open() as document:
        for number in range(65):
            document.new_page().insert_text((72, 72), f"Evidence on page {number + 1}")
        source = document.tobytes()
    result = extract(workspace, source, max_chars=30)
    text = (workspace / result["content_path"]).read_text()
    assert result["truncated"] and result["page_count"] == 65 and result["textless_pages"] == []
    assert "[page 65]" in text and "Evidence on page 65" in text
    assert text.count("## [page ") == 65


@pytest.mark.parametrize("kind", ["empty_html", "title_only", "empty_pdf", "truncated_pdf", "damaged_pdf", "oversize", "empty_download"])
def test_empty_incomplete_or_oversized_sources_are_never_cached(workspace, kind):
    source = b"<body>valid</body>"
    code, max_bytes = "web_extract_empty", 1024 * 1024
    if kind == "empty_html":
        source = b"<body><script>only script</script><style>only style</style></body>"
    elif kind == "title_only":
        source = b"<html><head><title>Only title</title></head><body></body></html>"
    elif kind in {"empty_pdf", "truncated_pdf"}:
        with pymupdf.open() as document:
            page = document.new_page()
            if kind == "truncated_pdf":
                page.insert_text((72, 72), "Readable before download was truncated")
            source = document.tobytes()
        if kind == "truncated_pdf":
            source = source[:-40]
            code = "web_extract_pdf"
    elif kind == "damaged_pdf":
        source = b"%PDF-1.7\nnot a PDF document\n%%EOF"
        code = "web_extract_pdf"
    elif kind == "oversize":
        max_bytes = len(source) - 1
        code = "web_extract_too_large"
    else:
        source = b""
    with pytest.raises(WebExtractError) as failed:
        extract(workspace, source, max_bytes=max_bytes)
    assert failed.value.code == code
    assert not list((workspace / "_trace").glob("web-extract/*"))


def test_failed_download_can_be_retried_without_a_poisoned_cache(workspace):
    calls = []
    def download():
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("fixture HTTP 403")
        return b"<p>Valid source after retry</p>"
    with pytest.raises(RuntimeError, match="HTTP 403"):
        extract_web_document(workspace, "https://example.test/report", max_chars=200, max_bytes=1024, download=download)
    result = extract_web_document(workspace, "https://example.test/report", max_chars=200, max_bytes=1024, download=download)
    assert not result["cached"] and len(calls) == 2


@pytest.mark.parametrize("damage", ["content_changed", "content_newlines_changed", "content_missing", "metadata_invalid", "metadata_hash_changed"])
def test_damaged_cache_fails_explicitly_without_fetch_or_overwrite(workspace, damage):
    result = extract(workspace, b"<p>Verified source body</p>")
    content_path, metadata_path = (workspace / result[name] for name in ("content_path", "metadata_path"))
    if damage == "content_changed":
        content_path.write_text("tampered source body")
    elif damage == "content_newlines_changed":
        content_path.write_bytes(content_path.read_bytes().replace(b"\n", b"\r\n"))
    elif damage == "content_missing":
        content_path.unlink()
    elif damage == "metadata_invalid":
        metadata_path.write_text("not JSON")
    else:
        metadata = json.loads(metadata_path.read_text())
        metadata["content_sha256"] = "0" * 64
        metadata_path.write_text(json.dumps(metadata))
    before = metadata_path.read_bytes()
    def forbidden():
        pytest.fail("a damaged cache must not silently fetch or overwrite evidence")
    with pytest.raises(WebExtractError) as failed:
        extract_web_document(workspace, "https://example.test/report", max_chars=200, max_bytes=1024, download=forbidden)
    assert failed.value.code == "web_extract_cache_invalid" and metadata_path.read_bytes() == before


def test_metadata_keeps_source_query_and_userinfo_is_rejected_before_download(workspace):
    result = extract(workspace, b"<p>Evidence</p>", url="https://example.test/report?token=private-signature#anchor")
    metadata = (workspace / result["metadata_path"]).read_text()
    assert "private-signature" in metadata
    assert parse_qs(urlsplit(result["url"]).query)["token"] == ["private-signature"]
    assert "private-signature" not in result["metadata_path"] and "REDACTED" not in result["url"]
    def forbidden():
        pytest.fail("URL userinfo must be rejected before download")
    with pytest.raises(WebExtractError) as failed:
        extract_web_document(workspace, "https://user:password@example.test/report", max_chars=200,
            max_bytes=1024, download=forbidden)
    assert failed.value.code == "web_extract_url" and "password@" not in str(failed.value)


def test_unpublished_content_can_be_retried_after_metadata_write_failure(workspace, monkeypatch):
    import runtime.web_extract as module
    original = module._atomic_text_write
    def fail_metadata(path, text):
        if path.suffix == ".json":
            raise OSError("fixture metadata write failed")
        original(path, text)
    monkeypatch.setattr(module, "_atomic_text_write", fail_metadata)
    with pytest.raises(OSError, match="metadata write failed"):
        extract(workspace, b"<p>First uncommitted extraction</p>")
    assert len(list((workspace / "_trace/web-extract").glob("*.md"))) == 1
    assert not list((workspace / "_trace/web-extract").glob("*.json"))
    monkeypatch.setattr(module, "_atomic_text_write", original)
    result = extract(workspace, b"<p>Second complete extraction</p>")
    assert not result["cached"] and "Second complete" in result["content"]


def test_cached_source_still_respects_a_smaller_current_byte_limit(workspace):
    source = b"<p>Evidence with some length</p>"
    extract(workspace, source)
    with pytest.raises(WebExtractError) as failed:
        extract(workspace, source, max_bytes=len(source) - 1)
    assert failed.value.code == "web_extract_too_large"


def test_code_block_line_endings_are_preserved_and_verified_as_exact_bytes(workspace):
    source = b"<pre>first\r\nsecond\r\n</pre>"
    first = extract(workspace, source)
    saved = (workspace / first["content_path"]).read_bytes()
    assert b"first\r\nsecond" in saved
    assert first["content_sha256"] == sha256(saved).hexdigest()
    second = extract(workspace, source)
    assert second["cached"] and second["content"] == first["content"]


def test_query_parameters_and_relative_images_are_not_rewritten(workspace):
    source = b'''<p><a href="/article?id=42&amp;page=2&amp;q=rain+garden&amp;token=private-token">Evidence</a></p>
      <img src="figures/chart.png?id=7&amp;X-Amz-Signature=private-signature" alt="Trend">
      <p>https://example.test/report?id=42&amp;key=private-key</p>'''
    result = extract(workspace, source, url="https://example.test/research/report?id=42&page=2&q=rain+garden&token=private-token")
    query = parse_qs(urlsplit(result["url"]).query)
    assert query == {"id": ["42"], "page": ["2"], "q": ["rain garden"], "token": ["private-token"]}
    text = (workspace / result["content_path"]).read_text()
    assert "https://example.test/article?id=42&page=2&q=rain+garden&token=private-token" in text
    assert "https://example.test/research/figures/chart.png?id=7&X-Amz-Signature=private-signature" in text
    combined = text + (workspace / result["metadata_path"]).read_text()
    assert all(secret in combined for secret in ("private-token", "private-signature", "private-key"))
    other = extract(workspace, source, url="https://example.test/research/report?id=43")
    assert other["content_path"] != result["content_path"] and not other["cached"]


def test_inline_image_reference_and_long_text_are_not_discarded_or_globally_truncated(workspace):
    tail = "End of intact evidence"
    source = ("<p>" + "a" * 70000 + tail + '</p><img src="data:image/png;base64,iVBORw0KGgo=" alt="Inline chart">').encode()
    result = extract(workspace, source, max_chars=80000)
    assert not result["truncated"] and tail in result["content"]
    assert "Inline chart" in result["content"]
    assert "inline-binary-omitted" in result["content"]
    assert "data:image" not in result["content"]
    assert "iVBORw0KGgo=" not in result["content"]


@pytest.mark.parametrize("tag,attribute", [("img", "alt"), ("img", "title"), ("a", "title")])
@pytest.mark.parametrize("render_js", [False, True])
def test_markdown_caption_urls_remain_intact(workspace, tag, attribute, render_js):
    caption = "Caption https://example.test/read?id=42&amp;page=2&amp;token=SYNTHETIC-SECRET evidence"
    source = (f'<p><img src="chart.png" {attribute}="{caption}"></p>' if tag == "img" else
              f'<p><a href="/read?id=42" {attribute}="{caption}">Source</a></p>').encode()
    calls = []
    def download():
        calls.append(True)
        return source
    first = extract_web_document(workspace, "https://example.test/report", max_chars=12000,
        max_bytes=1024 * 1024, download=download, render_js=render_js)
    text = (workspace / first["content_path"]).read_text()
    assert "Caption https://example.test/read?id=42&page=2&token=SYNTHETIC-SECRET evidence" in text
    assert first["content_sha256"] == sha256(text.encode()).hexdigest()
    resumed = extract_web_document(workspace, "https://example.test/report", max_chars=12000,
        max_bytes=1024 * 1024, download=download, render_js=render_js)
    assert resumed["cached"] and resumed["content"] == text and len(calls) == 1
