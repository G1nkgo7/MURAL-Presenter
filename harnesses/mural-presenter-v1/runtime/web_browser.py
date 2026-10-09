"""按需运行公开网页 JavaScript；隔离进程的总期限由现有 command runner 管理。"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
import time
from urllib.parse import parse_qs, urljoin, urlsplit


class WebBrowserError(RuntimeError):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(detail)


def _resource_module(path: Path):
    spec = importlib.util.spec_from_file_location("mural_browser_resources", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render_pdf(body: bytes, url: str, effective_url: str, output: Path, *,
                query: str, deadline: float) -> dict:
    import fitz
    from PIL import Image

    def check_deadline():
        if time.monotonic() >= deadline:
            raise WebBrowserError("web_extract_timeout", "PDF capture exceeded the source deadline.")

    check_deadline()
    try:
        with fitz.open(stream=body, filetype="pdf") as document:
            if document.needs_pass or document.is_encrypted:
                raise WebBrowserError("web_extract_pdf_encrypted", "Encrypted PDFs cannot be captured.")
            count = document.page_count
            if count < 1:
                raise WebBrowserError("web_extract_pdf_invalid", "The PDF has no physical pages.")
            selectors = parse_qs(urlsplit(url).fragment, keep_blank_values=True).get("page")
            number = 1
            if selectors is not None:
                if len(selectors) != 1 or not selectors[0].isascii() or not selectors[0].isdigit():
                    raise WebBrowserError("web_extract_pdf_page_invalid", "Use a one-based physical PDF page: #page=N.")
                number = int(selectors[0])
                if not 1 <= number <= count:
                    raise WebBrowserError("web_extract_pdf_page_invalid", "The requested physical PDF page is out of range.")
            elif query:
                for index in range(count):
                    check_deadline()
                    if query in document.load_page(index).get_text("text"):
                        number = index + 1
                        break
                else:
                    raise WebBrowserError("web_extract_query_not_found", "The literal text was not found in PDF text; no OCR was attempted.")
            check_deadline()
            page = document.load_page(number - 1)
            width, height = page.rect.width, page.rect.height
            if not all(math.isfinite(value) and value > 0 for value in (width, height)):
                raise WebBrowserError("web_extract_pdf_invalid", "The PDF page has invalid dimensions.")
            scale = min(1280 / width, 900 / height)
            # Rasterize source graphics only; links/actions are never executed.
            pixels = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False)
            image = Image.frombytes("RGB", (pixels.width, pixels.height), pixels.samples)
            image.thumbnail((1280, 900))
            canvas = Image.new("RGB", (1280, 900), "white")
            canvas.paste(image, ((1280 - image.width) // 2, (900 - image.height) // 2))
            title = (document.metadata.get("title") or urlsplit(effective_url).path.rsplit("/", 1)[-1])[:300]
            check_deadline()
            canvas.save(output, format="PNG")
            return {"url": url, "effective_url": effective_url, "title": title, "query": query,
                "viewport": {"width": 1280, "height": 900}, "capture_scope": "pdf_page",
                "source_page": number, "source_page_count": count,
                "source_doc_sha256": hashlib.sha256(body).hexdigest(), "resource_warnings": []}
    except WebBrowserError:
        raise
    except Exception:
        raise WebBrowserError("web_extract_pdf_invalid", "The PDF could not be decoded or rasterized.") from None


def render_html(url: str, output: Path, *, timeout_s: float, max_bytes: int,
                screenshot: bool = False, query: str = "",
                resource_module: Path | None = None) -> dict | None:
    from playwright.async_api import Error, TimeoutError as BrowserTimeout
    try:
        return asyncio.run(_render_html(url, output, timeout_s=timeout_s, max_bytes=max_bytes,
            screenshot=screenshot, query=query, resource_module=resource_module))
    except Error as exc:
        print(str(exc), file=sys.stderr)
        raise WebBrowserError(
            "web_extract_timeout" if isinstance(exc, BrowserTimeout) else "web_extract_browser_failed",
            "Dynamic extraction did not complete; see command diagnostics. The source is not verified.",
        ) from None


async def _render_html(url: str, output: Path, *, timeout_s: float, max_bytes: int,
                       screenshot: bool = False, query: str = "",
                       resource_module: Path | None = None) -> dict | None:
    from playwright.async_api import TimeoutError as BrowserTimeout, async_playwright

    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise WebBrowserError("web_extract_url", "Use a public HTTP(S) source without userinfo.")
    deadline = time.monotonic() + timeout_s
    resources = _resource_module(resource_module) if screenshot and resource_module else None
    if screenshot and resources is None:
        raise WebBrowserError("web_extract_url", "Screenshot capture requires the selected Skill resource reader.")

    def remaining() -> float:
        return max(1, (deadline - time.monotonic()) * 1000)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, timeout=remaining())
        try:
            context = await browser.new_context(accept_downloads=False, service_workers="block")
            # WS 路由运行在异步调度器中，必须 await close，不能重入同步 API。
            async def block_socket(socket):
                await socket.close()

            await context.route_web_socket("**/*", block_socket)
            page = await context.new_page()
            if screenshot:
                await page.set_viewport_size({"width": 1280, "height": 900})
            received = 0
            exceeded = False
            main_error = None
            main_redirect = None
            main_pdf = None
            read_lock = asyncio.Lock()

            async def read_resource(url):
                nonlocal received, exceeded
                async with read_lock:
                    if exceeded or received >= max_bytes:
                        exceeded = True
                        raise ValueError("remote_resource_too_large")
                    try:
                        result = await asyncio.to_thread(resources.fetch, url,
                            timeout=max(.1, remaining() / 1000), max_bytes=max_bytes - received)
                    except ValueError as exc:
                        if "remote_resource_too_large" in str(exc):
                            exceeded = True
                        raise
                    received += len(result["body"])
                    return result

            # 不带登录态，不提交表单，不执行网页发起的写请求或下载媒体。
            async def route_request(route):
                nonlocal exceeded, main_error, main_redirect, main_pdf
                request = route.request
                if screenshot:
                    if request.method != "GET" or request.resource_type == "media":
                        await route.abort()
                        return
                    try:
                        resource_url = request.url
                        for _ in range(9):
                            result = await read_resource(resource_url)
                            if result["status"] not in {301, 302, 303, 307, 308}:
                                break
                            location = result["headers"].get("location")
                            if not location:
                                raise ValueError("remote_resource_redirect_failed")
                            resource_url = urljoin(resource_url, location)
                            # Browser routes are not called for every redirect hop.
                            # Restart navigation instead of fulfilling a redirect.
                            if request.is_navigation_request() and request.frame == page.main_frame:
                                main_redirect = resource_url
                                await route.abort(error_code="aborted")
                                return
                        else:
                            raise ValueError("remote_resource_redirect_failed")
                        if request.is_navigation_request() and request.frame == page.main_frame:
                            content_type = result["headers"].get("content-type", "").split(";", 1)[0].strip().lower()
                            if content_type == "application/pdf" or result["body"].startswith(b"%PDF-"):
                                if not 200 <= result["status"] < 300:
                                    code = f"web_extract_http_{result['status']}"
                                    main_error = WebBrowserError(code, "The source did not return a successful HTTP response.")
                                else:
                                    main_pdf = (result["body"], resource_url)
                                await route.abort(error_code="aborted")
                                return
                        await route.fulfill(status=result["status"], headers=result["headers"], body=result["body"])
                    except Exception as exc:
                        if "remote_resource_too_large" in str(exc):
                            exceeded = True
                        if request.is_navigation_request() and request.frame == page.main_frame:
                            code = ("web_extract_url" if "remote_resource_scope_denied" in str(exc)
                                or "remote_resource_url_invalid" in str(exc) else "web_extract_redirect_failed"
                                if "remote_resource_redirect_failed" in str(exc) else "web_extract_browser_failed")
                            main_error = WebBrowserError(code, "The source could not be read within the public-web boundary.")
                        await route.abort()
                    return
                if (route.request.method in {"GET", "HEAD"}
                    and urlsplit(route.request.url).scheme in {"http", "https"}
                    and route.request.resource_type not in {"image", "media", "font"}):
                    await route.continue_()
                else:
                    await route.abort()

            await context.route("**/*", route_request)
            failed_dependencies = []
            dependencies = {"script", "stylesheet", "xhr", "fetch"} | ({"image", "font"} if screenshot else set())
            context.on("response", lambda response: failed_dependencies.append(
                f"{response.request.resource_type}:http_{response.status}")
                if response.request.resource_type in dependencies and response.status >= 400 else None)
            context.on("requestfailed", lambda request: failed_dependencies.append(
                f"{request.resource_type}:network_failed")
                if request.method in {"GET", "HEAD"} and request.resource_type in dependencies else None)
            session = await context.new_cdp_session(page)
            await session.send("Network.enable")

            async def account(event):
                nonlocal received, exceeded
                received += int(event.get("dataLength", 0))
                if received > max_bytes and not exceeded:
                    exceeded = True
                    await page.close()

            if not screenshot:
                session.on("Network.dataReceived", account)
            try:
                navigation_url = url
                for _ in range(9):
                    main_redirect = None
                    try:
                        response = await page.goto(navigation_url, wait_until="domcontentloaded", timeout=remaining())
                    except Exception:
                        if main_pdf is not None:
                            break
                        if main_redirect is not None:
                            navigation_url = main_redirect
                            continue
                        if main_error is not None:
                            raise main_error
                        raise
                    break
                else:
                    raise WebBrowserError("web_extract_redirect_failed", "The source exceeded the redirect limit.")
                if main_pdf is not None:
                    return _render_pdf(main_pdf[0], url, main_pdf[1], output, query=query, deadline=deadline)
                if response is None or not response.ok:
                    code = f"web_extract_http_{response.status if response else 'missing'}"
                    raise WebBrowserError(code, f"{code}: the source did not return a successful HTTP response.")
                try:
                    await page.wait_for_load_state("networkidle", timeout=min(5000, remaining()) if screenshot else remaining())
                except BrowserTimeout:
                    if not screenshot:
                        raise
                    failed_dependencies.append("network_not_idle")
                if failed_dependencies and not screenshot:
                    raise WebBrowserError("web_extract_dependency_failed", ", ".join(sorted(set(failed_dependencies))))
                if screenshot:
                    if main_error is not None:
                        raise main_error
                    if main_redirect is not None:
                        raise WebBrowserError("web_extract_redirect_failed", "The page redirected again after loading; use its stable source URL.")
                    if query:
                        matches = page.get_by_text(query, exact=False)
                        match = None
                        for index in range(await matches.count()):
                            candidate = matches.nth(index)
                            if await candidate.is_visible():
                                match = candidate
                                break
                        if match is None:
                            raise WebBrowserError("web_extract_query_not_found", "The requested text was not found in visible page content.")
                        await match.scroll_into_view_if_needed(timeout=remaining())
                    await page.screenshot(path=str(output), full_page=False, timeout=remaining())
                    return {"url": url, "effective_url": page.url, "title": (await page.title())[:300],
                        "viewport": {"width": 1280, "height": 900}, "capture_scope": "viewport",
                        "query": query, "scroll_y": await page.evaluate("window.scrollY"),
                        "resource_warnings": sorted(set(failed_dependencies))[:8]}
                # DOM 快照使用实际跳转后的 URL 解析相对链接，不改变来源请求身份。
                await page.evaluate("""() => {
                    if (!document.querySelector('base[href]')) {
                        const base = document.createElement('base');
                        base.href = location.href; document.head.prepend(base);
                    }
                }""")
                html = (await page.content()).encode("utf-8")
                if len(html) > max_bytes:
                    exceeded = True
                if not exceeded:
                    output.write_bytes(html)
            finally:
                if exceeded:
                    raise WebBrowserError("web_extract_too_large", (
                        f"web_extract_too_large: browser network or DOM exceeded {max_bytes} bytes "
                        f"(network received {received} bytes). Use HTTP extraction or a smaller primary source; "
                        "repeating the same render_js request will not raise this limit."
                    )) from None
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("output", type=Path)
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("--max-bytes", type=int, required=True)
    parser.add_argument("--screenshot", action="store_true")
    parser.add_argument("--query", default="")
    parser.add_argument("--resource-module", type=Path)
    args = parser.parse_args()
    try:
        result = render_html(args.url, args.output, timeout_s=args.timeout, max_bytes=args.max_bytes,
            screenshot=args.screenshot, query=args.query, resource_module=args.resource_module)
        if result is not None:
            print(json.dumps(result), flush=True)
    except WebBrowserError as exc:
        print(json.dumps({"error_code": exc.code, "detail": str(exc)}), flush=True)
        sys.exit(2)
