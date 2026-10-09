"""从 CSS/DOM 读取几何；不选择版式、不写入或同步作者文件。"""
from __future__ import annotations

import json
import hashlib
from html import escape
import mimetypes
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile


# Playwright CSS probe; 45s was too tight under concurrent local CPU.
CSS_GEOMETRY_TIMEOUT_S = 120


MEASURE_JS = r"""() => {
  const probe = document.createElement('div');
  probe.style.cssText = 'position:fixed!important;visibility:hidden!important;'
    + 'box-sizing:border-box!important;padding:0!important;border:0!important;'
    + 'width:var(--canvas-w)!important;height:var(--canvas-h)!important;';
  document.documentElement.append(probe);
  const root = getComputedStyle(document.documentElement);
  if (!root.getPropertyValue('--canvas-w').trim() || !root.getPropertyValue('--canvas-h').trim()) {
    probe.remove(); throw Error('base.css must define --canvas-w and --canvas-h');
  }
  const style = getComputedStyle(probe);
  const width = parseFloat(style.width), height = parseFloat(style.height);
  probe.remove();
  if (![width, height].every(n => Number.isInteger(n) && n > 0))
    throw Error('canvas must resolve to positive integral CSS pixels');
  const slides = [...document.querySelectorAll('.slide[data-slide], .mural-slide')];
  const deck = document.querySelector('#deck');
  if (deck && (deck.offsetWidth !== width || deck.offsetHeight !== height))
    throw Error(`deck canvas mismatch: expected ${width}x${height}, actual ${deck.offsetWidth}x${deck.offsetHeight}`);
  for (const [index, slide] of slides.entries()) {
    const s = getComputedStyle(slide);
    const box = s.boxSizing === 'border-box';
    const w = parseFloat(s.width) + (box ? 0 : ['paddingLeft','paddingRight','borderLeftWidth','borderRightWidth'].reduce((n,k) => n + parseFloat(s[k]), 0));
    const h = parseFloat(s.height) + (box ? 0 : ['paddingTop','paddingBottom','borderTopWidth','borderBottomWidth'].reduce((n,k) => n + parseFloat(s[k]), 0));
    const number = slide.dataset.slide || index + 1;
    if (w !== width || h !== height)
      throw Error(`page ${number} canvas mismatch: expected ${width}x${height}, actual ${w}x${h}`);
    for (const token of ['--canvas-w', '--canvas-h'])
      if (s.getPropertyValue(token).trim() !== root.getPropertyValue(token).trim())
        throw Error(`page ${number} overrides shared ${token}`);
    if (s.transform !== 'none') {
      const m = new DOMMatrixReadOnly(s.transform);
      if (Math.abs(Math.hypot(m.a, m.b) - 1) > .001 || Math.abs(Math.hypot(m.c, m.d) - 1) > .001)
        throw Error(`page ${number} scales its author canvas`);
    }
  }
  return {width_px:width, height_px:height, aspect_ratio:width/height,
          width_in:width/96, height_in:height/96};
}"""


def measure_geometry(page, expected: dict | None = None) -> dict:
    geometry = page.evaluate(MEASURE_JS)
    if expected is not None and geometry != expected:
        pages = page.locator('.slide[data-slide]').evaluate_all("nodes => nodes.map(n => n.dataset.slide)")
        raise ValueError(f"pages {pages} CSS override shared canvas: expected {expected}, actual {geometry}")
    return geometry


_CSS_GEOMETRY: dict[str, dict] = {}


def read_geometry(root: Path) -> dict:
    root = root.resolve()
    css = (root / "base.css").read_text(encoding="utf-8")
    if re.search(r"@import\b", css, re.I):
        return _read_css(root, css)
    if css not in _CSS_GEOMETRY:
        value = _read_css(root, css)
        if len(_CSS_GEOMETRY) >= 32:
            _CSS_GEOMETRY.pop(next(iter(_CSS_GEOMETRY)))
        _CSS_GEOMETRY[css] = value
    return dict(_CSS_GEOMETRY[css])


def _read_css(root: Path, css: str) -> dict:
    # 用浏览器解释 calc/var/级联，避免 Python 正则形成另一套 CSS 语义。
    # 子进程隔离 Playwright 事件循环和临时目录；内存复用只按实际 CSS 内容命中。
    temporary = root / "_runtime" / "tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="geometry-", dir=temporary) as scratch:
        env = {**os.environ, "TMPDIR": scratch, "TMP": scratch, "TEMP": scratch,
               "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run([sys.executable, str(Path(__file__).resolve())],
            input=css, text=True, capture_output=True, env=env, cwd=root, timeout=CSS_GEOMETRY_TIMEOUT_S)
    if result.returncode:
        raise ValueError(f"cannot read base.css geometry: {result.stderr.strip()}")
    return json.loads(result.stdout)


def image_geometry(path: Path) -> dict:
    from PIL import Image
    with Image.open(path) as image:
        width, height = image.size
    return {"width_px": width, "height_px": height, "aspect_ratio": width / height,
            "width_in": width / 96, "height_in": height / 96}


def validate_png(path: Path, geometry: dict, *, scale: float = 1) -> None:
    actual = image_geometry(path)
    expected = (round(geometry["width_px"] * scale), round(geometry["height_px"] * scale))
    size = (actual["width_px"], actual["height_px"])
    if size != expected:
        raise ValueError(f"{path.name} PNG canvas mismatch: expected {expected}, actual {size}")


if __name__ == "__main__":
    from playwright.sync_api import Error as PlaywrightError, sync_playwright
    from remote_resources import fulfill
    from urllib.parse import unquote, urlsplit
    css = sys.stdin.read()
    with sync_playwright() as playwright:
        endpoint = os.environ.get("MURAL_RENDER_BROWSER_ENDPOINT", "")
        if endpoint:
            from browser_host import validate_endpoint
            browser = playwright.chromium.connect_over_cdp(validate_endpoint(endpoint))
        else:
            browser = playwright.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(service_workers="block")
        try:
            page = context.new_page()
            failures = []
            root = Path.cwd().resolve()
            probe_url = (root / "_runtime/geometry-probe.html").as_uri()
            def resource(route):
                parsed = urlsplit(route.request.url)
                if route.request.url == probe_url and route.request.is_navigation_request():
                    route.fulfill(content_type="text/html", body=f'<!doctype html><html><head><base href="{escape(root.as_uri())}/"></head><body></body></html>')
                    return
                if parsed.scheme in {"http", "https"}:
                    fulfill(route, errors=failures)
                    return
                path = Path(unquote(parsed.path)).resolve()
                if parsed.scheme != "file" or parsed.netloc:
                    failures.append(f"read_scope_denied: {route.request.url}")
                    route.abort()
                    return
                try:
                    expected = None
                    if not path.is_relative_to(root):
                        from font_bundle import resolve_shared_font_reference
                        path, expected = resolve_shared_font_reference(root, route.request.url)
                    body = path.read_bytes()
                    if expected and hashlib.sha256(body).hexdigest() != expected:
                        raise ValueError("fixed font bytes changed while measuring CSS")
                    route.fulfill(body=body, content_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream")
                except (OSError, ValueError) as exc:
                    failures.append(str(exc))
                    route.abort()
            page.route("**/*", resource)
            page.on("requestfailed", lambda request: failures.append(request.url))
            page.on("response", lambda response: failures.append(f"HTTP {response.status}: {response.url}") if response.status >= 400 else None)
            page.goto(probe_url, wait_until="load")
            try:
                page.add_style_tag(content=css)
            except PlaywrightError:
                if not failures:
                    raise
                raise ValueError(
                    f"css_geometry_dependency_failed: {sorted(set(failures))}; no source was changed"
                ) from None
            if failures:
                raise ValueError(f"css_geometry_dependency_failed: {sorted(set(failures))}")
            print(json.dumps(measure_geometry(page)))
        finally:
            context.close()
            browser.close()
