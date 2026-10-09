"""在已验证的内嵌 HTML 快照中打印原生 PDF，不消费整页 PNG。"""
from __future__ import annotations

import asyncio
import http.client
from importlib.metadata import version
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urlsplit

import pymupdf as fitz


_ORIGIN = "https://mural-export.invalid/slide"


async def _serve_remote(route, remote_fetch, failures):
    request = route.request
    if (remote_fetch is None or request.method != "GET" or urlsplit(request.url).scheme not in {"http", "https"}
            or urlsplit(request.url).hostname == "mural-export.invalid"):
        return False
    try:
        result = await asyncio.to_thread(remote_fetch, request.url)
        if result["status"] >= 400:
            failures.append(f"HTTP {result['status']}: {request.url}")
        await route.fulfill(status=result["status"], headers=result["headers"], body=result["body"])
    except (OSError, ValueError, http.client.HTTPException) as exc:
        failures.append(f"{request.url}: {exc}")
        await route.abort()
    return True


def source_identity():
    from state.workspace import file_sha256
    return {"adapter": file_sha256(Path(__file__)), "playwright": version("playwright"),
            "pymupdf": version("PyMuPDF"), "mode": "html-native"}


def validate_single_file_html(html, canvas, *, scratch: Path, deadline_monotonic: float, page_numbers, remote_fetch=None):
    """在不提供任何伴随资源的浏览器中验收单文件；不猜测或改写脚本里的路径。"""
    from playwright.async_api import async_playwright, Error as BrowserError

    async def validate():
        with tempfile.TemporaryDirectory(prefix="html-browser-", dir=scratch) as temporary:
            env = {**os.environ, **dict.fromkeys(("TMPDIR", "TMP", "TEMP"), temporary)}
            async with async_playwright() as pw:
                options = {"headless": True, "env": env, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
                if env.get("PPT_SKILL_BROWSER_EXE"):
                    options["executable_path"] = env["PPT_SKILL_BROWSER_EXE"]
                browser = await pw.chromium.launch(**options)
                try:
                    context = await browser.new_context(viewport={"width": int(canvas["width_px"]),
                        "height": int(canvas["height_px"])}, accept_downloads=False, service_workers="block")
                    failures = []
                    await context.route_web_socket("**/*", lambda socket: failures.append("websocket"))

                    async def route(request_route):
                        request = request_route.request
                        if request.url.split("?", 1)[0] == _ORIGIN and request.is_navigation_request() and request.method == "GET":
                            await request_route.fulfill(status=200, content_type="text/html", body=html)
                        elif not await _serve_remote(request_route, remote_fetch, failures):
                            failures.append(request.url)
                            await request_route.abort()

                    await context.route("**/*", route)
                    page = await context.new_page()
                    page.on("pageerror", lambda error: failures.append("script-error"))
                    for number in page_numbers:
                        await page.goto(f"{_ORIGIN}?slide={number}", wait_until="networkidle")
                        await page.evaluate("document.fonts.ready")
                        await page.wait_for_timeout(3000)
                        broken = await page.evaluate("[...document.images].filter(i => !i.complete || !i.naturalWidth).length")
                        if failures or broken or await page.evaluate("[...document.fonts].some(f => f.status === 'error')"):
                            raise ValueError(f"single-file HTML page {number} has unavailable runtime resources: {failures}")
                finally:
                    await browser.close()

    async def bounded():
        try:
            await asyncio.wait_for(validate(), max(0, deadline_monotonic - time.monotonic()))
        except (TimeoutError, BrowserError) as exc:
            raise ValueError("single-file HTML resource validation failed or timed out") from exc

    asyncio.run(bounded())


def write_native_pdf(documents, target: Path, canvas, *, scratch: Path, deadline_monotonic: float, resources=None, remote_fetch=None):
    """每份文档只打印一页；CSS 几何来自现有画布，分页异常明确拒绝。"""
    from playwright.async_api import Error as BrowserError, TimeoutError as BrowserTimeout
    async def bounded():
        try:
            await asyncio.wait_for(_write_native_pdf(documents, target, canvas, scratch=scratch,
                deadline_monotonic=deadline_monotonic, resources=resources, remote_fetch=remote_fetch), max(0, deadline_monotonic-time.monotonic()))
        except (TimeoutError, BrowserTimeout) as exc:
            raise ValueError("native PDF render timeout") from exc
        except BrowserError as exc:
            raise ValueError("native PDF failed Chromium setup or cleanup") from exc
    asyncio.run(bounded())


async def _write_native_pdf(documents, target: Path, canvas, *, scratch: Path, deadline_monotonic: float, resources=None, remote_fetch=None):
    from playwright.async_api import async_playwright
    width, height = int(canvas["width_px"]), int(canvas["height_px"])

    def remaining():
        value = deadline_monotonic - time.monotonic()
        if value <= 0:
            raise ValueError("native PDF render timeout")
        return max(1, int(value * 1000))

    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pdf-browser-", dir=scratch) as temporary, fitz.open() as merged:
        async with async_playwright() as pw:
            await _print_documents(pw, temporary, merged, documents, canvas, width, height, remaining, resources or {}, remote_fetch)
        remaining()
        if not documents or len(merged) != len(documents):
            raise ValueError("native PDF output page count differs from its source")
        target.parent.mkdir(parents=True, exist_ok=True)
        merged.save(target, garbage=3, deflate=True)


async def _print_documents(pw, temporary, merged, documents, canvas, width, height, remaining, resources, remote_fetch):
    from playwright.async_api import Error as BrowserError, TimeoutError as BrowserTimeout
    environment = {**os.environ, **dict.fromkeys(("TMPDIR", "TMP", "TEMP"), temporary)}
    options = {"headless": True, "env": environment, "timeout": remaining(),
               "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
    if environment.get("PPT_SKILL_BROWSER_EXE"):
        options["executable_path"] = environment["PPT_SKILL_BROWSER_EXE"]
    browser = await pw.chromium.launch(**options)
    try:
        for number, html in documents:
            context = await browser.new_context(viewport={"width": width, "height": height},
                accept_downloads=False, service_workers="block")
            failures = []
            await context.route_web_socket("**/*", lambda socket: failures.append("external-websocket"))

            async def route(request_route):
                request = request_route.request
                if request.url == _ORIGIN and request.is_navigation_request() and request.method == "GET":
                    await request_route.fulfill(status=200, content_type="text/html", body=html)
                elif request.method == "GET" and request.url.split("?", 1)[0] in resources:
                    media_type, data = resources[request.url.split("?", 1)[0]]
                    await request_route.fulfill(status=200, content_type=media_type, body=data)
                elif not await _serve_remote(request_route, remote_fetch, failures):
                    failures.append("external-resource")
                    await request_route.abort()

            await context.route("**/*", route)
            page = await context.new_page()
            page.on("pageerror", lambda error: failures.append("script-error"))
            try:
                page.set_default_timeout(remaining())
                await page.emulate_media(media="screen")
                await page.goto(_ORIGIN, wait_until="networkidle", timeout=remaining())
                await page.wait_for_function("document.fonts.status === 'loaded' && [...document.images].every(i => i.complete)", timeout=remaining())
                if await page.evaluate("[...document.fonts].some(font => font.status === 'error')"):
                    raise ValueError(f"native PDF page {number} has unavailable fonts")
                await page.evaluate("Promise.all([...document.images].map(i => i.decode()))")
                needs_settle = await page.evaluate("""() => document.getAnimations().length > 0 ||
                  [...document.scripts].some(script => !script.type || /(?:java|ecma)script|module/i.test(script.type))""")
                if needs_settle:
                    await page.wait_for_timeout(3000)
                await page.evaluate("""async () => {
                  for (const animation of document.getAnimations()) animation.pause();
                  await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
                }""")
                visible_text = await page.evaluate("""({width,height}) => {
                  const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
                  while (walker.nextNode()) {
                    const node=walker.currentNode;
                    if (!node.data.trim() || node.parentElement.closest('script,style,noscript,template')) continue;
                    let hidden=false;
                    for (let el=node.parentElement;el;el=el.parentElement) {
                      const s=getComputedStyle(el),r=el.getBoundingClientRect();
                      if (s.display==='none' || s.visibility!=='visible' || Number(s.opacity)===0 || s.contentVisibility==='hidden' ||
                          (r.width<=1 && r.height<=1 && (s.overflow==='hidden' || s.clip!=='auto' || s.clipPath!=='none'))) {
                        hidden=true;break;
                      }
                    }
                    if (hidden) continue;
                    const range=document.createRange();range.selectNodeContents(node);
                    if ([...range.getClientRects()].some(r=>r.width>1 && r.height>1 && r.right>0 && r.bottom>0 && r.x<width && r.y<height)) return true;
                  }
                  return false;
                }""", {"width":width,"height":height})
                await page.add_style_tag(content=f"@page {{ size: {canvas['width_in']}in {canvas['height_in']}in !important; margin: 0 !important; }}")
                await page.evaluate("""({width,height}) => {
                  const candidates=[...document.querySelectorAll('.slide[data-slide],.mural-slide,.slide')];
                  const root=candidates.find(el => {
                    const r=el.getBoundingClientRect(),s=getComputedStyle(el);
                    return s.display!=='none' && s.visibility==='visible' && Number(s.opacity)>0 &&
                      Math.abs(r.width-width)<2 && Math.abs(r.height-height)<2;
                  }) || document.body;
                  const box=root.getBoundingClientRect();
                  if (Math.abs(box.width-width)>2 || Math.abs(box.height-height)>2)
                    throw Error('native PDF canvas geometry mismatch');
                  const geometry=[root,...root.querySelectorAll('*')].map(el=>({el,box:el.getBoundingClientRect()}))
                    .filter(item=>item.box.width>0 || item.box.height>0);
                  // 只移动打印视口原点，不改作者根节点及其后代布局。
                  const style=document.documentElement.style;
                  for (const [key,value] of Object.entries({position:'fixed',width:width+'px',height:height+'px',
                      overflow:'hidden',left:-box.x+'px',top:-box.y+'px',margin:'0',
                      'print-color-adjust':'exact','-webkit-print-color-adjust':'exact'})) style.setProperty(key,value,'important');
                  const after=root.getBoundingClientRect();
                  if (Math.abs(after.x)>1 || Math.abs(after.y)>1 || Math.abs(after.width-box.width)>1 || Math.abs(after.height-box.height)>1)
                    throw Error('native PDF print viewport changed canvas geometry');
                  for (const item of geometry) {
                    const now=item.el.getBoundingClientRect(),old=item.box;
                    if (Math.abs(now.x-after.x-old.x+box.x)>1 || Math.abs(now.y-after.y-old.y+box.y)>1 ||
                        Math.abs(now.width-old.width)>1 || Math.abs(now.height-old.height)>1)
                      throw Error('native PDF print viewport changed descendant geometry');
                  }
                }""", {"width": width, "height": height})
                remaining()
                data = await page.pdf(width=f"{canvas['width_in']}in", height=f"{canvas['height_in']}in",
                    margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
                    print_background=True, prefer_css_page_size=True, display_header_footer=False,
                    scale=1, tagged=True)
                if failures:
                    raise ValueError(f"native PDF page {number} requested a resource outside its verified snapshot or had a script error")
                with fitz.open(stream=data, filetype="pdf") as single:
                    if len(single) != 1:
                        raise ValueError(f"native PDF page {number} produced unexpected pagination")
                    if visible_text and not single[0].get_text().strip():
                        raise ValueError(f"native PDF page {number} has visible HTML text but no searchable PDF text")
                    rect = single[0].rect
                    expected = fitz.Rect(0, 0, canvas["width_in"]*72, canvas["height_in"]*72)
                    if abs(rect.width-expected.width)>0.75 or abs(rect.height-expected.height)>0.75:
                        raise ValueError(f"native PDF page {number} has unexpected physical dimensions: {rect.width} x {rect.height} pt")
                    # Chromium 纸张单位有亚像素取整；只校正页面边界，不缩放文字/图像。
                    single[0].set_mediabox(expected)
                    merged.insert_pdf(single)
            except BrowserTimeout as exc:
                raise ValueError("native PDF render timeout") from exc
            except BrowserError as exc:
                raise ValueError(f"native PDF page {number} failed Chromium rendering") from exc
            finally:
                await context.close()
    finally:
        await browser.close()
