"""把完整、有界下载转为可续读正文；下载权限与超时仍由调用方控制。"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from html import escape
import json
from pathlib import Path
import re
from typing import Callable
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, NavigableString, Tag
try:
    from markdownify import markdownify
except ModuleNotFoundError:
    def markdownify(html: str, **_options: object) -> str:
        return _fallback_markdownify(html)

from state.locking import workspace_lock
from state.workspace import _atomic_text_write
from .base64_sanitize import scrub_inline_base64_text
from .workspace_paths import WorkspacePaths


class WebExtractError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


WEB_SOURCE_REF_RE = re.compile(
    r"^sources/web/(?P<key>[0-9a-f]{64})(?P<full>\.full)?\.md$"
)


def web_source_ref(result: dict) -> str:
    """Return a model-safe logical reference for one private cached source."""
    raw = str(result.get("full_body_path") or result.get("content_path") or "")
    name = Path(raw).name
    if not WEB_SOURCE_REF_RE.fullmatch(f"sources/web/{name}"):
        raise WebExtractError(
            "web_extract_cache_invalid",
            "Cached source has no valid public continuation reference.",
        )
    return f"sources/web/{name}"


def resolve_web_source_ref(workspace: str | Path, reference: str) -> Path:
    """Resolve a public source reference without exposing its private cache path."""
    match = WEB_SOURCE_REF_RE.fullmatch(str(reference or ""))
    if match is None:
        raise WebExtractError(
            "web_extract_source_ref",
            "Invalid web source reference; use the exact source_ref returned by web_extract.",
        )
    paths = WorkspacePaths.from_root(workspace)
    directory = paths.resolve("_trace/web-extract")
    key = match.group("key")
    filename = key + (".full.md" if match.group("full") else ".md")
    content_path = directory / filename
    metadata_path = directory / f"{key}.json"
    for path in (paths.trace, directory, content_path, metadata_path):
        if path.is_symlink():
            raise WebExtractError(
                "web_extract_cache_invalid",
                "Cached source failed integrity validation.",
            )
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        data = content_path.read_bytes()
        expected_key = "full_body_sha256" if match.group("full") else "content_sha256"
        if (
            metadata.get("schema") != "mural.web-extract.v1"
            or metadata.get("format") not in {"html", "pdf"}
            or metadata.get(expected_key) != sha256(data).hexdigest()
            or not data.strip()
        ):
            raise ValueError("invalid cached source")
        data.decode("utf-8")
    except (OSError, UnicodeDecodeError, ValueError, TypeError) as exc:
        raise WebExtractError(
            "web_extract_cache_invalid",
            "Cached source failed integrity validation; extract the source again.",
        ) from exc
    return content_path


def _escape_markdown_text(text: str) -> str:
    return text.replace("_", r"\_")


def _inline_text(node: Tag) -> str:
    return _escape_markdown_text(" ".join(node.get_text(" ", strip=True).split()))


def _attribute_text(node: Tag, attribute: str) -> str:
    return _escape_markdown_text(str(node.get(attribute) or "").strip())


def _fallback_children(node: Tag) -> str:
    return " ".join(part for part in (_fallback_markdown_node(child) for child in node.children) if part).strip()


def _fallback_table(node: Tag) -> str:
    rows: list[list[str]] = []
    for row in node.find_all("tr"):
        cells = [_inline_text(cell) for cell in row.find_all(["th", "td"])]
        if cells:
            rows.append(cells)
    if not rows:
        return _inline_text(node)
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    header, body = rows[0], rows[1:]
    parts = ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * width) + " |"]
    parts.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(parts)


def _fallback_markdown_node(node: object) -> str:
    if isinstance(node, NavigableString):
        return _escape_markdown_text(" ".join(str(node).split()))
    if not isinstance(node, Tag):
        return ""
    name = node.name.lower()
    if name in {"script", "style", "noscript", "template", "head"}:
        return ""
    if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        level = int(name[1])
        return f"{'#' * level} {_inline_text(node)}"
    if name == "pre":
        return node.get_text()
    if name == "br":
        return "\n"
    if name == "a":
        text = _fallback_children(node) or _inline_text(node)
        href = str(node.get("href") or "").strip()
        title = _attribute_text(node, "title")
        title_part = f' "{title}"' if title else ""
        return f"[{text}]({href}{title_part})" if href and text else (text or title)
    if name == "img":
        alt = _inline_text(node) or _attribute_text(node, "alt") or _attribute_text(node, "title")
        src = str(node.get("src") or "").strip()
        title = _attribute_text(node, "title")
        title_part = f' "{title}"' if title and title != alt else ""
        return f"![{alt}]({src}{title_part})" if src else alt
    if name == "table":
        return _fallback_table(node)
    if name in {"ul", "ol"}:
        lines = []
        for index, child in enumerate(node.find_all("li", recursive=False), start=1):
            marker = f"{index}." if name == "ol" else "-"
            lines.append(f"{marker} {_fallback_children(child)}".rstrip())
        return "\n".join(lines)
    if name == "li":
        return _fallback_children(node)
    text = _fallback_children(node)
    if name in {"p", "div", "section", "article", "main", "header", "footer", "body", "html"}:
        return text
    return text or _inline_text(node)


def _fallback_markdownify(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.body or soup
    blocks = [_fallback_markdown_node(child) for child in root.children]
    return "\n\n".join(block for block in blocks if block).strip()


def _source_url(url: str) -> str:
    try:
        parts = urlsplit(str(url).strip())
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            raise ValueError("unsupported source")
        if parts.username is not None or parts.password is not None:
            raise ValueError("URL userinfo is not allowed")
        return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path, parts.query, ""))
    except ValueError as exc:
        raise WebExtractError("web_extract_url", "Use an http/https URL without username or password.") from exc


def _public_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None:
        raise WebExtractError("web_extract_url", "Source links must not contain URL username or password.")
    return url


def _html_markdown(data: bytes, url: str) -> tuple[str, dict, str | None]:
    soup = BeautifulSoup(data, "html.parser")
    title = " ".join(soup.title.get_text(" ", strip=True).split()) if soup.title else ""
    base = soup.find("base", href=True)
    base_url = urljoin(url, base["href"]) if base else url
    for node in soup.select("script, style, noscript, template, head"):
        node.decompose()
    for node in soup.find_all(["a", "img"]):
        attribute = "href" if node.name == "a" else "src"
        if node.get(attribute):
            target = urljoin(base_url, node[attribute])
            if urlsplit(target).scheme in {"http", "https", "mailto"}:
                node[attribute] = _public_url(target)
            elif node.name == "img" and target.startswith("data:image/"):
                node[attribute] = scrub_inline_base64_text(target)
            else:
                del node[attribute]
    options = {"heading_style": "ATX", "bullets": "-", "escape_misc": True,
               "keep_inline_images_in": ["a", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6"]}
    root = soup.body or soup
    full_body = scrub_inline_base64_text(markdownify(str(root), **options).strip())
    if not full_body:
        raise WebExtractError("web_extract_empty", "HTML has no extractable body; do not claim the source was verified.")
    # 只信明确的语义边界；不按关键词/字数猜正文。被排除部分仍可从完整正文核对。
    mains = root.select('main, [role="main"]')
    mains = [node for node in mains if not any(parent in mains for parent in node.parents)]
    selected, scope = root, "full_body"
    if len(mains) == 1:
        selected, scope = mains[0], "main"
        articles = selected.find_all("article")
        articles = [node for node in articles if not any(parent in articles for parent in node.parents)]
        if len(articles) == 1:
            selected, scope = articles[0], "article"
    elif not mains:
        articles = root.find_all("article")
        articles = [node for node in articles if not any(parent in articles for parent in node.parents)]
        if len(articles) == 1:
            selected, scope = articles[0], "article"
    body = scrub_inline_base64_text(markdownify(str(selected), **options).strip())
    if scope != "full_body":
        for node in selected.select('nav, [role="navigation"]'):
            node.decompose()
        body = scrub_inline_base64_text(markdownify(str(selected), **options).strip())
        if not body:
            body, scope = full_body, "full_body"
    prefix = markdownify(f"<h1>{escape(title)}</h1>", **options).strip() + "\n\n" if title else ""
    details = {"format": "html", "title": title[:512], "extraction_scope": scope,
               "extraction_notice": ("Selected explicit main content; full_body_path preserves surrounding evidence and navigation."
                   if scope != "full_body" else "No unambiguous main content boundary; using the complete body.")}
    return prefix + body + "\n", details, prefix + full_body + "\n" if scope != "full_body" else None


def _pdf_markdown(data: bytes) -> tuple[str, dict]:
    import pymupdf

    if not data.rstrip().endswith(b"%%EOF"):
        raise WebExtractError("web_extract_pdf", "PDF download is incomplete; retry a complete source.")
    try:
        with pymupdf.open(stream=data, filetype="pdf") as document:
            if document.is_repaired or document.needs_pass or not document.page_count:
                raise WebExtractError("web_extract_pdf", "PDF is damaged, encrypted, or empty; use a complete readable source.")
            parts, textless = [], []
            for page in document:
                text = scrub_inline_base64_text(page.get_text(sort=True).strip())
                if not text:
                    textless.append(page.number + 1)
                parts.append(f"## [page {page.number + 1}]\n\n{text}\n")
            if len(textless) == document.page_count:
                raise WebExtractError("web_extract_empty", "PDF has no extractable text; use a readable source instead.")
            return "\n".join(parts), {"format": "pdf", "title": "", "page_count": document.page_count,
                                      "textless_pages": textless}
    except WebExtractError:
        raise
    except RuntimeError as exc:
        raise WebExtractError("web_extract_pdf", "PDF is incomplete or cannot be parsed; retry a complete source.") from exc


def _cached(metadata_path: Path, content_path: Path, public_url: str, max_bytes: int, method: str) -> tuple[dict, str] | None:
    if not metadata_path.exists():
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        content_bytes = content_path.read_bytes()
        content = content_bytes.decode("utf-8")
        if (metadata["schema"] != "mural.web-extract.v1" or metadata["url"] != public_url
                or metadata.get("fetch_method") != method
                or metadata["format"] not in {"html", "pdf"}
                or metadata["content_sha256"] != sha256(content_bytes).hexdigest()
                or metadata["characters"] != len(content) or not content.strip()
                or not isinstance(metadata["source_sha256"], str) or len(metadata["source_sha256"]) != 64
                or type(metadata["source_bytes"]) is not int or metadata["source_bytes"] <= 0):
            raise ValueError("invalid extraction receipt")
        scope = metadata.get("extraction_scope", "full_body")
        if metadata["format"] == "html" and scope not in {"main", "article", "full_body"}:
            raise ValueError("invalid extraction scope")
        if scope in {"main", "article"} or metadata.get("full_body_sha256"):
            full_path = content_path.with_suffix(".full.md")
            if (not metadata.get("full_body_sha256") or full_path.is_symlink()
                    or sha256(full_path.read_bytes()).hexdigest() != metadata["full_body_sha256"]):
                raise ValueError("invalid complete body receipt")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise WebExtractError("web_extract_cache_invalid",
            "Cached source text failed integrity validation; restore or remove its content and metadata files before retrying.") from exc
    if metadata["source_bytes"] > max_bytes:
        raise WebExtractError("web_extract_too_large", "Cached source exceeds the current download byte limit.")
    scrubbed = scrub_inline_base64_text(content)
    full_path = content_path.with_suffix(".full.md")
    full_content = None
    full_scrubbed = None
    if metadata.get("full_body_sha256") and full_path.exists() and not full_path.is_symlink():
        full_content = full_path.read_text(encoding="utf-8")
        full_scrubbed = scrub_inline_base64_text(full_content)
    if scrubbed != content or (full_scrubbed is not None and full_scrubbed != full_content):
        metadata = dict(metadata)
        metadata["content_sha256"] = sha256(scrubbed.encode()).hexdigest()
        metadata["characters"] = len(scrubbed)
        metadata["inline_binary_omitted"] = True
        _atomic_text_write(content_path, scrubbed)
        if full_scrubbed is not None:
            metadata["full_body_sha256"] = sha256(full_scrubbed.encode()).hexdigest()
            _atomic_text_write(full_path, full_scrubbed)
        _atomic_text_write(metadata_path, json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
        content = scrubbed
    return metadata, content


def extract_web_document(
    workspace: str | Path,
    url: str,
    *,
    max_chars: int,
    max_bytes: int,
    download: Callable[[], bytes | tuple[bytes, str]],
    render_js: bool = False,
) -> dict:
    """复用 URL 级持久正文；max_chars 只控制展示，不改变来源缓存。"""
    if max_chars <= 0 or max_bytes <= 0:
        raise ValueError("max_chars and max_bytes must be positive")
    url = _source_url(url)
    public_url = _public_url(url)
    paths = WorkspacePaths.from_root(workspace)
    directory = paths.resolve("_trace/web-extract")
    method = "browser" if render_js else "http"
    key = sha256((url + ("\nrender_js" if render_js else "")).encode()).hexdigest()
    content_path, metadata_path = directory / f"{key}.md", directory / f"{key}.json"
    full_body_path = content_path.with_suffix(".full.md")
    for path in (paths.trace, directory, content_path, metadata_path, full_body_path):
        if path.is_symlink():
            raise WebExtractError("web_extract_cache_invalid", "Source cache paths must not be symlinks.")

    def response(metadata: dict, content: str, *, cached: bool) -> dict:
        details = {}
        if metadata["format"] == "html":
            details = {"extraction_scope": metadata.get("extraction_scope", "full_body"),
                       "extraction_notice": metadata.get("extraction_notice", "Cached complete body; no main-content selection was applied."),
                       "full_body_path": paths.relative(full_body_path if metadata.get("full_body_sha256") else content_path)}
        return {**metadata, **details, "content": content[:max_chars], "truncated": len(content) > max_chars,
                "cached": cached, "content_path": paths.relative(content_path),
                "metadata_path": paths.relative(metadata_path)}

    with workspace_lock(paths.root):
        prior = _cached(metadata_path, content_path, public_url, max_bytes, method)
        if prior is not None:
            return response(*prior, cached=True)
    downloaded = download()
    effective_url = None
    if isinstance(downloaded, tuple):
        data, effective_url = downloaded
        effective_url = _source_url(effective_url)
    else:
        data = downloaded
    if len(data) > max_bytes:
        raise WebExtractError("web_extract_too_large", "Source exceeds the download byte limit; no truncated source was cached.")
    if not data:
        raise WebExtractError("web_extract_empty", "Source download is empty; no source was cached.")
    full_body = None
    if data.startswith(b"%PDF-"):
        content, details = _pdf_markdown(data)
    else:
        content, details, full_body = _html_markdown(data, effective_url or url)
    metadata = {"schema": "mural.web-extract.v1", "url": public_url, "fetch_method": method, **details,
                "source_bytes": len(data), "source_sha256": sha256(data).hexdigest(),
                "content_sha256": sha256(content.encode()).hexdigest(), "characters": len(content),
                "fetched_at": datetime.now(timezone.utc).isoformat()}
    if effective_url is not None:
        metadata["effective_url"] = _public_url(effective_url)
    if full_body is not None:
        metadata["full_body_sha256"] = sha256(full_body.encode()).hexdigest()
    with workspace_lock(paths.root):
        prior = _cached(metadata_path, content_path, public_url, max_bytes, method)
        if prior is not None:
            return response(*prior, cached=True)
        directory.mkdir(parents=True, exist_ok=True)
        _atomic_text_write(content_path, content)
        if full_body is not None:
            _atomic_text_write(full_body_path, full_body)
        _atomic_text_write(metadata_path, json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    return response(metadata, content, cached=False)
