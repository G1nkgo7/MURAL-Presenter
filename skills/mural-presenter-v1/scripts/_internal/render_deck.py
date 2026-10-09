#!/usr/bin/env python3
"""Static HTML Presentation renderer: render a deck to page PNGs for visual review.

用法（在仓库根目录、已安装 Playwright 的 Python 环境下）：
    python path/to/render_deck.py <present.html> <out_dir> --page N
    python path/to/render_deck.py <present.html> <out_dir> --all

行为契约（agent loop / accept 依赖，勿随意更改）：
- stdout 每行一个生成的 PNG 路径，**末行保证是一个 PNG 路径**（--all 时末行是 contact-sheet.png）。
- 告警（console error / 静态页 / 导航恢复）打印在 PNG 路径之前，前缀 [console]/[static]/[nav]。
- 渲染元数据写 <out_dir>/render.json（console_errors / static_pages / blank_pages / nav / 页数）。
- 失败（页面打不开 / 一页都没截到）：stderr + 退出码非 0。

页面导航：
- --page N 用 `file://present.html?slide=N` 直跳，再调用交付页的
  `window.cleanDeck.go()` 并等待活动页信号与可见性同时确认；无法确认时停止，
  不会抓取一个已知错误的页面。
- --all 先读取总页数，再用 `?slide=1..N` 确定性逐页渲染，避免动态页面、
  动画或键盘焦点造成漏页。键盘导航属于交付页面交互，不用于决定截图是否收齐。

动态检测（--motion-check，默认开）：对每页采样三帧——
- early（资源稳定并重触发 active 入场后 ~0.12s）
- final（重触发后至少 3.0s，等待入场动画和延迟布局完全落定，作为正式截图）
- live （final 后再 ~0.6s，探测持续型动画）
entrance_diff = diff(early, final)，live_diff = diff(final, live)。两者都 ≈0 → 该页记入
static_pages（仅是信号，不是错误：庄重场景的静帧页可以是有意识的设计，由 agent/judge 结合场景判断）。

移植自 visual_qc/render_shots.py（活动页信号 JS / 翻页收敛 / blank 检测 / 浏览器库注入），
本脚本必须保持自包含（skill 可整体拷走），如修 bug 请两边同步。
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import mimetypes
import os
import signal
import sys
import tempfile
import time
import warnings
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

try:
    from .geometry import read_geometry, measure_geometry, validate_png
except ImportError:
    from geometry import read_geometry, measure_geometry, validate_png

# Re-exec with the Harness interpreter when a direct caller used the wrong Python.
if 'PLAYWRIGHT_REEXEC' not in os.environ:
    try:
        import playwright  # noqa: F401
    except ImportError:
        _repo_python = Path(__file__).resolve().parents[4] / '.venv/bin/python'
        for _cand in filter(None, [
            os.environ.get('PPT_SKILL_PYTHON'),
            str(_repo_python),
        ]):
            _candidate = Path(_cand).expanduser()
            if _candidate.is_file() and _candidate.absolute() != Path(sys.executable).absolute():
                os.environ['PLAYWRIGHT_REEXEC'] = '1'
                os.environ.pop('PLAYWRIGHT_BROWSERS_PATH', None)
                _env_root = _candidate.parent.parent
                if (_env_root / 'conda-meta').is_dir():
                    os.environ['CONDA_PREFIX'] = str(_env_root)
                os.execv(str(_candidate), [str(_candidate), __file__] + sys.argv[1:])
# 即便是原生 fancy-sft 环境启动，也要清掉 cursor 的假路径
if os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '').startswith('/tmp/cursor-sandbox-cache'):
    os.environ.pop('PLAYWRIGHT_BROWSERS_PATH', None)
if 'CONDA_PREFIX' not in os.environ:
    _active_prefix = Path(sys.executable).parent.parent
    if (_active_prefix / 'conda-meta').is_dir():
        os.environ['CONDA_PREFIX'] = str(_active_prefix)

warnings.filterwarnings("ignore", category=DeprecationWarning)  # PIL getdata 噪声

# ── 画布尺寸 / 时间常量 ─────────────────────────────────────────────────
DEVICE_SCALE = 1               # CPU-only swiftshader 下 2x 会让重型 3D 页光栅慢一倍易超时
MAX_PAGES = max(1, int(os.environ.get("PPT_RENDER_MAX_PAGES", "200")))
EARLY_MS = 120                 # motion-check：重触发后尽早采样，避免短入场被缓动提前吃完
FINAL_CAPTURE_MS = max(3000, int(os.environ.get("PPT_RENDER_FINAL_MS", "3000")))
                               # 正式截图距重触发至少 3 秒；给入场动画、字体和延迟布局留足时间。
LIVE_EXTRA_MS = 600            # final 之后再等这么久截 live 帧（探测持续动画）
GOTO_TIMEOUT_MS = 30000
RESOURCE_LOAD_TIMEOUT_MS = 8000
SCREENSHOT_TIMEOUT_MS = 60000
ACTIVE_READY_TIMEOUT_MS = 3000
BLANK_RETRY_MS = 650
RENDER_LOCK_PATH = os.environ.get(
    "PPT_RENDER_LOCK_PATH", "/tmp/ppt-skill-html-clean-render.lock"
)
RENDER_CONCURRENCY = max(1, int(os.environ.get("PPT_RENDER_CONCURRENCY", "1")))
RENDER_LOCK_DIR = os.environ.get(
    "PPT_RENDER_LOCK_DIR", "/tmp/ppt-skill-html-clean-render-slots"
)

BLANK_STD_THRESH = 3.0         # 灰度标准差 < 此值视为空白/近纯色页
MAX_CONSOLE_ERRORS = 20        # console 错误最多记录条数（去重后）


def _temporary_parent(path: Path) -> Path:
    parent = path.parent
    if parent.name == "renders":
        # layout-v2:scratch 落在 _runtime/tmp,而非 workspace 根的 tmp/
        # (后者会被 v2 布局校验判为 forbidden legacy path,导致 finalize 拒绝)。
        controlled = parent.parent / "_runtime" / "tmp"
        controlled.mkdir(parents=True, exist_ok=True)
        return controlled
    return parent


def _cleanup_temporary_parent(path: Path, temporary_parent: Path) -> None:
    if temporary_parent == path.parent.parent / "_runtime" / "tmp":
        # ``_runtime/tmp/`` is shared Harness metadata during a concurrent child
        # wave. Keep the empty directory present; delivery audit checks that no
        # files remain and removes it only after all children have joined.
        temporary_parent.mkdir(parents=True, exist_ok=True)


def _atomic_bytes(path: Path, payload: bytes, *, suffix: str = ".tmp") -> None:
    """Publish bytes from ``tmp/`` or an allowed ``renders/.page_NN/`` directory."""
    temporary_parent = _temporary_parent(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f"{path.name}.",
        suffix=suffix,
        dir=temporary_parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
        _cleanup_temporary_parent(path, temporary_parent)


def _atomic_json(path: Path, value: object) -> None:
    """Publish render metadata only after the complete JSON is durable."""
    _atomic_bytes(
        path,
        json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"),
    )


@contextmanager
def render_slot():
    """Acquire one global Chromium slot without serializing all model work."""
    if RENDER_CONCURRENCY == 1:
        paths = [Path(RENDER_LOCK_PATH)]
    else:
        root = Path(RENDER_LOCK_DIR)
        root.mkdir(parents=True, exist_ok=True)
        start = os.getpid() % RENDER_CONCURRENCY
        paths = [
            root / f"slot-{(start + offset) % RENDER_CONCURRENCY:03d}.lock"
            for offset in range(RENDER_CONCURRENCY)
        ]
    while True:
        for lock_path in paths:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = lock_path.open("a+")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                continue
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
            return
        time.sleep(0.1)

# 动态检测（64x64 网格）：无损截图下静态内容逐像素恒等，任何超出噪声的局部差异都是运动。
# 16x16 均值对小粒子/扫描线太钝（被平均稀释），改为统计"发生变化的格子数"。
MOTION_GRID = 64               # 下采样网格边长
MOTION_CELL_EPS = 4            # 格子灰度差 >= 此值算"变化格"（容忍抗锯齿微差）
MOTION_MIN_CELLS = 5           # 变化格数 < 此值（/4096）视为无运动


def ensure_browser_libs() -> list[str]:
    """Expose browser libraries supplied by the active environment or deployment."""
    configured = os.environ.get("PPT_SKILL_BROWSER_LIB_DIRS", "")
    candidates = [
        str(Path(value).expanduser())
        for value in configured.split(os.pathsep)
        if value
    ]
    # An explicit deployment bundle is authoritative. Mixing it with every
    # library from a newer Conda environment can inject glibc-incompatible NSPR
    # or NSS binaries into an older production container.
    prefix = os.environ.get("CONDA_PREFIX")
    active_lib = Path(sys.prefix) / "lib"
    pwdeps_dirs = (Path.home() / "pwdeps/lib",)
    if not candidates:
        if prefix:
            candidates.append(str(Path(prefix) / "lib"))
        if (active_lib / "libnspr4.so").is_file():
            candidates.append(str(active_lib))
        for pwdeps in pwdeps_dirs:
            if (pwdeps / "libgbm.so.1").is_file():
                candidates.append(str(pwdeps))
    else:
        # A configured deployment bundle is authoritative for ABI-sensitive
        # libraries such as NSPR/NSS. Never mix in the active Conda lib dir:
        # it may require a newer glibc than the production container. GBM may
        # still be supplied by the narrow, deployment-owned pwdeps bundle.
        configured_paths = [Path(value) for value in candidates]
        if not any((path / "libgbm.so.1").is_file() for path in configured_paths):
            for pwdeps in pwdeps_dirs:
                if (pwdeps / "libgbm.so.1").is_file():
                    candidates.insert(0, str(pwdeps))
                    break
    if configured:
        # An explicit deployment bundle must replace inherited Conda paths.
        # Keeping the latter can make Chromium load a newer NSPR/NSS build
        # that requires a newer glibc than the ACP runtime provides.
        parts = list(dict.fromkeys(candidates))
    else:
        cur = os.environ.get("LD_LIBRARY_PATH", "")
        parts = [p for p in cur.split(":") if p]
        for libdir in reversed(candidates):
            if libdir not in parts:
                parts.insert(0, libdir)
    if parts:
        os.environ["LD_LIBRARY_PATH"] = ":".join(parts)
    # chromium 静态链接的 fontconfig 默认读 /etc/fonts/fonts.conf；本 pod 没有 /etc/fonts，
    # 不设 FONTCONFIG_FILE 会让整个字体系统失效（连 webfont 文本都画不出来，页面只剩图形）。
    if not Path("/etc/fonts/fonts.conf").exists() and "FONTCONFIG_FILE" not in os.environ:
        if prefix and (Path(prefix) / "etc/fonts/fonts.conf").exists():
            os.environ["FONTCONFIG_FILE"] = str(Path(prefix) / "etc/fonts/fonts.conf")
    return parts


def reset_child_process_signals() -> None:
    """Start Playwright with normal signal handling after threaded model calls."""
    try:
        signal.pthread_sigmask(signal.SIG_SETMASK, [])
    except (AttributeError, OSError):
        pass
    try:
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    except (AttributeError, OSError, ValueError):
        pass


# 读"当前活动页"信号：返回 "活动页索引:总页数"（如 "2:6"）；测不到返回 "-1:N"。
# 优先级：显式 active/current 类 → 最可见(opacity 高且占满)的 slide 元素。
_ACTIVE_SIGNAL_JS = r"""
() => {
  // 只认 deck 契约的幻灯片标记：class="slide"（精确 token，不匹配 .slide-xxx）或 data-slide。
  // 不要用 [class*="page"]/section 等宽匹配——会把 .page-pad 等内容类名误数成幻灯片，触发假 [nav] 告警。
  const sel = '.slide,[data-slide]';
  let els = [...document.querySelectorAll(sel)];
  // 总页数按 DOM 契约计数，不按 bounding box 过滤。某些页型会用 display:none
  // 隐藏非活动页；若按尺寸过滤，total 会随当前页变化并导致直跳/整册渲染漏页。
  const total = els.length;
  if (total === 0) return "-1:0";
  for (let i = 0; i < total; i++) {
    const c = (els[i].className && els[i].className.baseVal !== undefined)
      ? els[i].className.baseVal : (els[i].className || '');
    if (/(^|[\s_-])(active|current|is-active|selected|on|visible|show)([\s_-]|$)/i.test(c))
      return i + ":" + total;
  }
  let best = -1, bo = -1;
  els.forEach((e, i) => {
    const s = getComputedStyle(e);
    if (s.display === 'none' || s.visibility === 'hidden') return;
    const o = parseFloat(s.opacity || '1');
    if (o > bo) { bo = o; best = i; }
  });
  return best + ":" + total;
}
"""

_LAYOUT_GEOMETRY_JS = r"""
() => {
  const slide = document.querySelector('.slide.active[data-slide]');
  if (!slide) return {};
  const slideRect = slide.getBoundingClientRect();
  const box = (selector) => {
    const element = slide.querySelector(selector);
    if (!element) return null;
    const rect = element.getBoundingClientRect();
    const round = (value) => Math.round(value * 1000) / 1000;
    return {
      x: round(rect.left - slideRect.left),
      y: round(rect.top - slideRect.top),
      width: round(rect.width),
      height: round(rect.height),
      cx: round(rect.left - slideRect.left + rect.width / 2),
      cy: round(rect.top - slideRect.top + rect.height / 2),
    };
  };
  const relativeRect = (rect) => {
    const round = (value) => Math.round(value * 1000) / 1000;
    return {
      x: round(rect.left - slideRect.left), y: round(rect.top - slideRect.top),
      width: round(rect.width), height: round(rect.height),
    };
  };
  const relativeBox = (element) => relativeRect(element.getBoundingClientRect());
  const visible = (element) => {
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      parseFloat(style.opacity || '1') > 0.02 && rect.width > 0 && rect.height > 0;
  };
  const className = (value) => {
    if (!value) return '';
    return value.baseVal !== undefined ? String(value.baseVal) : String(value);
  };
  const elementKey = (element) => {
    if (!element) return '';
    const classes = className(element.className).trim().split(/\s+/).filter(Boolean);
    return element.tagName.toLowerCase() + classes.slice(0, 3).map(name => '.' + name).join('');
  };
  const ancestorChain = (element) => {
    const chain = [];
    for (let cursor = element.parentElement;
         cursor && cursor !== slide && chain.length < 4;
         cursor = cursor.parentElement) {
      chain.push(elementKey(cursor));
    }
    return chain;
  };
  const mediaLayout = (element) => {
    const describe = (node) => {
      const style = getComputedStyle(node);
      return { element: elementKey(node), ...relativeBox(node), display: style.display,
        position: style.position, margin: style.margin, overflow: style.overflow,
        css_width: style.width, css_height: style.height };
    };
    const parents = [];
    for (let node = element.parentElement; node && node !== slide && parents.length < 3; node = node.parentElement)
      parents.push(describe(node));
    return { ...describe(element), parents };
  };
  // 只求明确矩形裁切的可见边界；非矩形路径及旋转后的局部裁切仍交给像素检查。
  const clippingRects = (element, style) => {
    const box = element.getBoundingClientRect();
    const width = element.offsetWidth, height = element.offsetHeight;
    if (!Number.isFinite(width) || !Number.isFinite(height)) return [];
    const absolute = ['absolute', 'fixed'].includes(style.position);
    const shapes = [['rect', absolute ? style.clip : 'auto'], ['inset', style.clipPath]];
    const result = [];
    for (const [kind, value] of shapes) {
      const match = kind === 'rect' ? /^rect\(([^()]*)\)$/.exec(value || '')
        : /^inset\(([^()]*)\)(?: border-box)?$/.exec(value || '');
      if (!match) continue;
      let parts = match[1].split(/\s+round\s+/)[0].trim().split(/[\s,]+/);
      if (kind === 'inset' && parts.length >= 1 && parts.length <= 4)
        parts = [parts[0], parts[1] || parts[0], parts[2] || parts[0], parts[3] || parts[1] || parts[0]];
      if (parts.length !== 4) continue;
      const defaults = [0, width, height, 0];
      const sides = parts.map((part, index) => {
        if (part === 'auto' && kind === 'rect') return defaults[index];
        if (!/^-?(?:\d+(?:\.\d*)?|\.\d+)(?:px|%)?$/.test(part)) return NaN;
        return parseFloat(part) * (part.endsWith('%') ? (index % 2 ? width : height) / 100 : 1);
      });
      if (!sides.every(Number.isFinite)) continue;
      const [t, r, b, l] = kind === 'inset'
        ? [sides[0], width - sides[1], height - sides[2], sides[3]] : sides;
      if (r <= l || b <= t) {
        result.push({left: 0, right: 0, top: 0, bottom: 0});
        continue;
      }
      if (!(width > 0 && height > 0)) continue;
      let axisAligned = true;
      for (let cursor = element; cursor; cursor = cursor.parentElement) {
        const current = getComputedStyle(cursor);
        const matrix = new DOMMatrixReadOnly(current.transform === 'none' ? undefined : current.transform);
        if (!matrix.is2D || matrix.b !== 0 || matrix.c !== 0 || matrix.a <= 0 || matrix.d <= 0
          || !['none', '0deg'].includes(current.rotate)
          || (current.scale !== 'none' && current.scale.split(/\s+/).some(value => Number(value) <= 0))) {
          axisAligned = false;
          break;
        }
      }
      if (axisAligned) result.push({left: box.left + l * box.width / width,
        right: box.left + r * box.width / width, top: box.top + t * box.height / height,
        bottom: box.top + b * box.height / height});
    }
    return result;
  };
  // 按真实文本节点采样，避免 innerText 漏掉 SVG 和父元素的混合文本；Range 只读，不改作者页面。
  // 显式隐藏/裁切可过滤；遮挡、同色描边等像素可读性仍由截图检查判断。
  const visibleTextRects = (element, range) => {
    const style = getComputedStyle(element);
    if (['hidden', 'collapse'].includes(style.visibility) || parseFloat(style.fontSize) === 0)
      return [];
    const painted = value => value && value !== 'none' && value !== 'transparent' &&
      !/^rgba\([^)]*,\s*0(?:\.0+)?\s*\)$/.test(value) && !/\/\s*0(?:\.0+)?%?\s*\)$/.test(value);
    const svgText = element.namespaceURI === 'http://www.w3.org/2000/svg';
    const fill = svgText
      ? painted(style.fill) && parseFloat(style.fillOpacity) > 0
      : painted(style.webkitTextFillColor || style.color) ||
        (style.backgroundClip === 'text' && style.backgroundImage !== 'none');
    const stroke = svgText
      ? painted(style.stroke) && parseFloat(style.strokeWidth) > 0 && parseFloat(style.strokeOpacity) > 0
      : painted(style.webkitTextStrokeColor) && parseFloat(style.webkitTextStrokeWidth) > 0;
    if (!fill && !stroke && style.textShadow === 'none') return [];
    let opacity = 1;
    let left = slideRect.left, right = slideRect.right, top = slideRect.top, bottom = slideRect.bottom;
    for (let cursor = element; cursor; cursor = cursor.parentElement) {
      const ancestorStyle = getComputedStyle(cursor);
      opacity *= parseFloat(ancestorStyle.opacity || '1');
      if (opacity <= .02 || ancestorStyle.display === 'none' || ancestorStyle.contentVisibility === 'hidden')
        return [];
      const rect = cursor.getBoundingClientRect();
      if (/^(hidden|clip|scroll|auto)$/.test(ancestorStyle.overflowX)) {
        left = Math.max(left, rect.left); right = Math.min(right, rect.right);
      }
      if (/^(hidden|clip|scroll|auto)$/.test(ancestorStyle.overflowY)) {
        top = Math.max(top, rect.top); bottom = Math.min(bottom, rect.bottom);
      }
      for (const clip of clippingRects(cursor, ancestorStyle)) {
        left = Math.max(left, clip.left); right = Math.min(right, clip.right);
        top = Math.max(top, clip.top); bottom = Math.min(bottom, clip.bottom);
      }
      if (right <= left || bottom <= top) return [];
    }
    return [...range.getClientRects()].map(rect => {
      const x = Math.max(left, rect.left), y = Math.max(top, rect.top);
      return {left: x, top: y, width: Math.min(right, rect.right) - x,
              height: Math.min(bottom, rect.bottom) - y};
    }).filter(rect => rect.width > 0 && rect.height > 0);
  };
  const textBoxes = [];
  const visibleTextByElement = new Map();
  const textWalker = document.createTreeWalker(slide, NodeFilter.SHOW_TEXT);
  for (let node = textWalker.nextNode(); node; node = textWalker.nextNode()) {
      const text = node.textContent.trim().replace(/\s+/g, ' ');
      const element = node.parentElement;
      if (!text || !element || element.closest('script,style,template,noscript,defs,clipPath,mask,title,desc'))
        continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      const fragments = visibleTextRects(element, range);
      if (!fragments.length) continue;
      for (let cursor = element; cursor && cursor !== slide; cursor = cursor.parentElement)
        visibleTextByElement.set(cursor, ((visibleTextByElement.get(cursor) || '') + ' ' + text).trim().slice(0, 160));
      // 原始文字范围保留溢出证据；碰撞使用下方裁切后的可见行片段。
      const rect = range.getBoundingClientRect();
      const style = getComputedStyle(element);
      const overflowPxX = Math.max(0, (element.scrollWidth || 0) - (element.clientWidth || 0));
      const overflowPxY = Math.max(0, (element.scrollHeight || 0) - (element.clientHeight || 0));
      textBoxes.push({
        id: textBoxes.length,
        tag: element.tagName.toLowerCase(),
        class_name: className(element.className),
        ancestor_chain: ancestorChain(element),
        text: text.slice(0, 160),
        font_size: parseFloat(style.fontSize || '0'),
        font_weight: style.fontWeight || '',
        font_family: style.fontFamily || '',
        letter_spacing: style.letterSpacing || '',
        line_height: parseFloat(style.lineHeight || '0') || 0,
        overflow_x: overflowPxX > 1,
        overflow_y: overflowPxY > 1,
        overflow_px_x: overflowPxX,
        overflow_px_y: overflowPxY,
        overflow_mode_x: style.overflowX || '',
        overflow_mode_y: style.overflowY || '',
        fragments: fragments.map(relativeRect),
        ...relativeRect(rect),
      });
  }
  const elements = [slide, ...slide.querySelectorAll('*')];
  const nodeKeys = new Map(elements.map((element, index) => [element, `dom-${index}`]));
  const layoutBlocks = elements.slice(1)
    .filter(element => visible(element) && visibleTextByElement.has(element) &&
      ['block', 'flow-root', 'flex', 'grid', 'list-item', 'table', 'table-cell'].includes(getComputedStyle(element).display))
    .map((element, index) => {
      const style = getComputedStyle(element);
      const overflowPxX = Math.max(0, element.scrollWidth - element.clientWidth);
      const overflowPxY = Math.max(0, element.scrollHeight - element.clientHeight);
      return {
        id: index,
        tag: element.tagName.toLowerCase(),
        class_name: className(element.className),
        parent_key: nodeKeys.get(element.parentElement),
        ancestor_chain: ancestorChain(element),
        text: visibleTextByElement.get(element),
        position: style.position || '',
        overflow_mode_x: style.overflowX || '',
        overflow_mode_y: style.overflowY || '',
        overflow_px_x: overflowPxX,
        overflow_px_y: overflowPxY,
        ...relativeBox(element),
      };
    });
  const images = [...slide.querySelectorAll('img')]
    .filter(visible)
    .map((element, index) => {
      const style = getComputedStyle(element);
      return {
        id: index,
        src: element.getAttribute('src') || '',
        alt: element.getAttribute('alt') || '',
        class_name: className(element.className),
        opacity: parseFloat(style.opacity || '1'),
        object_fit: style.objectFit || '',
        object_position: style.objectPosition || '',
        natural_width: element.naturalWidth || 0,
        natural_height: element.naturalHeight || 0,
        ...mediaLayout(element),
      };
    });
  const nativeMedia = [...slide.querySelectorAll('img,svg,canvas')]
    .filter(visible)
    .map((element, index) => {
      const tag = element.tagName.toLowerCase();
      const rect = relativeBox(element);
      const echartsHost = tag === 'canvas'
        ? element.closest('[_echarts_instance_]')
        : element.closest('[_echarts_instance_]');
      const kind = tag === 'img' ? 'bitmap' : echartsHost ? 'echarts' : tag;
      return {
        id: index,
        kind: kind,
        class_name: className(element.className),
        area_ratio: Math.round((rect.width * rect.height / Math.max(1, slideRect.width * slideRect.height)) * 10000) / 10000,
        ...rect,
      };
    });
  const codeMedia = [...slide.querySelectorAll(
      '[data-visual-medium="code-visual"],[role="img"],'
      + '[class*="chart"],[class*="diagram"],[class*="plot"],[class*="bars"]'
    )]
    .filter(element => visible(element)
      && !['img', 'svg', 'canvas'].includes(element.tagName.toLowerCase())
      && !element.closest('svg,canvas,[_echarts_instance_]'))
    .map((element, index) => {
      const rect = relativeBox(element);
      return {
        id: nativeMedia.length + index,
        kind: 'code-visual',
        class_name: className(element.className),
        area_ratio: Math.round((rect.width * rect.height / Math.max(1, slideRect.width * slideRect.height)) * 10000) / 10000,
        ...rect,
      };
    });
  const backgroundMedia = [...slide.querySelectorAll('*')]
    .filter(element => {
      if (!visible(element)) return false;
      const value = getComputedStyle(element).backgroundImage || '';
      // Chromium resolves relative CSS URLs to absolute file/http URLs in
      // computed style.  Match the assets path anywhere inside url(...), not
      // only at its beginning, otherwise real CSS-background photography is
      // invisible to geometry/media audit.
      return /url\([^)]*assets\//i.test(value);
    })
    .map((element, index) => {
      const style = getComputedStyle(element);
      const value = style.backgroundImage || '';
      const source = value.match(/url\(["']?([^"')]+)["']?\)/i);
      const rect = relativeBox(element);
      return {
        id: nativeMedia.length + codeMedia.length + index,
        kind: 'bitmap',
        class_name: className(element.className),
        background: true,
        src: source ? source[1] : value,
        alt: element.getAttribute('aria-label') ||
          element.getAttribute('data-asset-purpose') ||
          element.getAttribute('title') || '',
        object_fit: style.backgroundSize || '',
        background_position: style.backgroundPosition || '',
        area_ratio: Math.round((rect.width * rect.height / Math.max(1, slideRect.width * slideRect.height)) * 10000) / 10000,
        ...mediaLayout(element),
      };
    });
  const media = [...nativeMedia, ...codeMedia, ...backgroundMedia];
  return {
    text_boxes: textBoxes,
    layout_blocks: layoutBlocks,
    images: images,
    media: media,
    boxes: {
      page_stage: box('.page-stage'),
      page_background: box('.page-background'),
      title: box('.page-title'),
      subtitle: box('.page-subtitle'),
      footer: box('.page-footer'),
    },
  };
}
"""


def _img_stats(png_bytes: bytes) -> tuple[float, tuple]:
    """返回 (灰度标准差, 16x16 缩略灰度块) —— 用于空白检测 + 感知差异。"""
    from PIL import Image

    im = Image.open(BytesIO(png_bytes)).convert("L").resize((16, 16))
    px = list(im.get_flattened_data())
    n = len(px)
    mean = sum(px) / n
    var = sum((p - mean) ** 2 for p in px) / n
    return var ** 0.5, tuple(px)


def _motion_blocks(png_bytes: bytes) -> tuple:
    """64x64 灰度网格（动态检测专用，比 16x16 phash 细得多）。"""
    from PIL import Image

    im = Image.open(BytesIO(png_bytes)).convert("L").resize((MOTION_GRID, MOTION_GRID))
    return tuple(im.get_flattened_data())


def _moving_cells(a: tuple, b: tuple) -> int:
    """两帧之间灰度差 >= MOTION_CELL_EPS 的格子数（0..MOTION_GRID^2）。"""
    if not a or not b or len(a) != len(b):
        return -1
    return sum(1 for x, y in zip(a, b) if abs(x - y) >= MOTION_CELL_EPS)


def _parse_sig(sig: str) -> tuple[int, int]:
    try:
        i, t = sig.split(":")
        return int(i), int(t)
    except Exception:
        return -1, 0


def write_contact_sheet(paths: list[Path], target: Path, *, width: int, height: int,
                        labels: list[int] | None = None) -> Path:
    """制作与导出共用像素投影；页码位于图外，不覆盖标题或透明图层。"""
    from PIL import Image, ImageDraw

    if not paths or width <= 0 or height <= 0:
        raise ValueError("contact sheet requires images and positive canvas dimensions")
    labels = list(range(1, len(paths) + 1)) if labels is None else labels
    if len(labels) != len(paths):
        raise ValueError("contact sheet labels must match images")
    scale = 400 / max(width, height)
    thumb_w, thumb_h = max(1, round(width * scale)), max(1, round(height * scale))
    cols, label_h = min(4, len(paths)), 22
    cell_h = thumb_h + label_h
    sheet = Image.new("RGB", (cols * thumb_w, math.ceil(len(paths) / cols) * cell_h), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    for index, path in enumerate(paths):
        with Image.open(path) as source:
            rgba = source.convert("RGBA")
            image = Image.alpha_composite(Image.new("RGBA", rgba.size, (24, 24, 24, 255)), rgba)
            image = image.convert("RGB").resize((thumb_w, thumb_h), Image.Resampling.LANCZOS)
        x, y = index % cols * thumb_w, index // cols * cell_h
        draw.text((x + 6, y + 3), f"{labels[index]:02d}", fill=(232, 232, 232))
        sheet.paste(image, (x, y + label_h))
    payload = BytesIO()
    sheet.save(payload, format="PNG")
    _atomic_bytes(target, payload.getvalue(), suffix=".png")
    return target


class DeckRenderer:
    """单次调用拥有独立 Context；可借用本次 Harness 固定 Skill 的 Chromium。"""

    def __init__(self, html_path: Path, out_dir: Path, *, workspace: Path, motion_check: bool = True) -> None:
        self.html_path = html_path
        self.workspace = workspace.resolve()
        self._requests = {}
        self._response_hashes = {}
        self._started = time.monotonic()
        self.out_dir = out_dir
        self.motion_check = motion_check
        self.canvas = read_geometry(workspace)
        self.deck_w = self.canvas["width_px"]
        self.deck_h = self.canvas["height_px"]
        self.console_errors: list[str] = []
        self.rendered_this_run: set[int] = set()
        self.meta: dict = {
            "present": str(html_path),
            "w": self.deck_w, "h": self.deck_h,
            "canvas": self.canvas,
            "mode": "", "nav": "",
            "n_pages": 0,
            "pages": [],            # [{page, png, blank, entrance_diff, live_diff, static}]
            "console_errors": [],
            "static_pages": [],
            "blank_pages": [],
            "capture_retries": [],
            "capture_recovered_pages": [],
            "notes": "",
            "timings_s": {},
        }
        self._pw = None
        self._browser = None
        self._page = None
        self._context = None
        self._browser_tmp = None

    # ── 浏览器生命周期 ────────────────────────────────────────────────
    def start(self) -> None:
        ensure_browser_libs()
        from playwright.sync_api import sync_playwright

        with self._timed("playwright_start"):
            self._pw = sync_playwright().start()
        endpoint = os.environ.get("MURAL_RENDER_BROWSER_ENDPOINT", "")
        with self._timed("browser_connect_or_launch"):
            if endpoint:
                try:
                    from .browser_host import validate_endpoint
                except ImportError:
                    from browser_host import validate_endpoint
                self._browser = self._pw.chromium.connect_over_cdp(validate_endpoint(endpoint))
                self.meta["browser_connection"] = "shared_cdp"
            else:
                self._browser_tmp = tempfile.TemporaryDirectory(
                    prefix="chromium-", dir=os.environ.get("TMPDIR") or None
                )
                browser_env = {**os.environ, **dict.fromkeys(("TMPDIR", "TMP", "TEMP"), self._browser_tmp.name)}
                self._browser = self._pw.chromium.launch(
                    headless=True, env=browser_env,
                    args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                          "--use-gl=swiftshader", "--enable-unsafe-swiftshader"],
                )
                self.meta["browser_connection"] = "cold_launch"
        with self._timed("context_create"):
            self._context = self._browser.new_context(
                viewport={"width": self.deck_w, "height": self.deck_h},
                device_scale_factor=DEVICE_SCALE, ignore_https_errors=True, bypass_csp=True, service_workers="block",
            )
            self._context.route("file://**/*", self._serve_file)
            self._context.route("http://**/*", self._serve_remote)
            self._context.route("https://**/*", self._serve_remote)
            self._context.route_web_socket("**/*", lambda socket: socket.close())
            self._page = self._context.new_page()
        self._page.set_default_timeout(GOTO_TIMEOUT_MS)
        self._page.on("console", self._on_console)
        self._page.on("pageerror", self._on_pageerror)
        self._page.on("request", lambda request: self._requests.update({request: "pending"}))
        self._page.on("requestfinished", self._request_finished)
        self._page.on("requestfailed", lambda request: self._requests.update({request: "failed"}))

    def close(self) -> None:
        for closer in (lambda: self._context.close(), lambda: self._browser.close(), lambda: self._pw.stop()):
            try:
                closer()
            except Exception:
                pass
        if self._browser_tmp is not None:
            self._browser_tmp.cleanup()
            self._browser_tmp = None

    @contextmanager
    def _timed(self, stage: str):
        started = time.monotonic()
        try:
            yield
        finally:
            timings = self.meta["timings_s"]
            timings[stage] = round(timings.get(stage, 0.0) + time.monotonic() - started, 6)

    def _screenshot(self) -> bytes:
        with self._timed("screenshot"):
            return self._page.screenshot(clip={"x": 0, "y": 0, "width": self.deck_w, "height": self.deck_h})

    def _settle(self, milliseconds: float) -> None:
        with self._timed("settle"):
            self._page.wait_for_timeout(milliseconds)

    def _on_console(self, msg) -> None:
        try:
            if msg.type == "error":
                self._record_error(f"console: {msg.text[:300]}")
        except Exception:
            pass

    def _on_pageerror(self, exc) -> None:
        self._record_error(f"pageerror: {str(exc)[:300]}")

    def _record_error(self, text: str) -> None:
        if text not in self.console_errors and len(self.console_errors) < MAX_CONSOLE_ERRORS:
            self.console_errors.append(text)

    def _serve_file(self, route) -> None:
        request = route.request
        parsed = urlsplit(request.url)
        path = Path(unquote(parsed.path)).resolve()
        try:
            if parsed.netloc:
                raise ValueError("remote file host is not allowed")
            expected = None
            if not path.is_relative_to(self.workspace):
                try:
                    from .font_bundle import resolve_shared_font_reference
                except ImportError:
                    from font_bundle import resolve_shared_font_reference
                path, expected = resolve_shared_font_reference(self.workspace, request.url, verify=False)
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if expected is not None and digest != expected:
                raise ValueError("pinned font changed before capture")
        except (OSError, ValueError) as error:
            self._record_error(f"resource: {request.url}: {error}")
            route.abort()
            return
        # 发送与记录同一份字节；不依赖会淘汰大字体响应的 DevTools 正文缓存。
        self._response_hashes[request] = digest
        route.fulfill(status=200, body=data,
                      content_type=mimetypes.guess_type(path)[0] or "application/octet-stream")

    def _serve_remote(self, route) -> None:
        try:
            from .remote_resources import fulfill
        except ImportError:
            from remote_resources import fulfill
        fulfill(route, hashes=self._response_hashes, errors=self.console_errors, root=self.workspace)

    def _request_finished(self, request) -> None:
        response = request.response()
        missing_bytes = urlsplit(request.url).scheme == "file" and request not in self._response_hashes
        self._requests[request] = "failed" if response is None or response.status >= 400 or missing_bytes else "loaded"

    def _resource_receipt(self) -> dict:
        loaded, failed = set(), set()
        input_hashes = {}
        for request, status in self._requests.items():
            reference = request.url
            parsed = urlsplit(reference)
            if parsed.scheme in {"data", "blob"}:
                continue
            if parsed.scheme == "file":
                path = Path(unquote(parsed.path)).resolve()
                if path == self.html_path.resolve():
                    continue
                if path.is_relative_to(self.workspace):
                    reference = quote(path.relative_to(self.workspace).as_posix(), safe="/")
            (loaded if status == "loaded" else failed).add(reference)
            if status == "loaded" and request in self._response_hashes:
                key = unquote(reference) if not urlsplit(reference).scheme else reference
                digest = self._response_hashes[request]
                if key in input_hashes and input_hashes[key] != digest:
                    failed.add(reference)
                input_hashes[key] = digest
        return {"loaded_resources": sorted(loaded), "failed_resources": sorted(failed),
                "input_hashes": input_hashes}

    # ── 加载与导航 ───────────────────────────────────────────────────
    def _load(self, query: str = "") -> None:
        """加载 deck，不做长 settle（采样时序由 _sample_page 控制，否则 early 帧会错过入场动画）。"""
        url = self.html_path.resolve().as_uri() + query
        self._requests.clear()
        self._response_hashes.clear()
        with self._timed("navigation"):
            self._page.goto(url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        timed_out = False
        try:
            with self._timed("resource_wait"):
                self._page.wait_for_load_state("load", timeout=RESOURCE_LOAD_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            timed_out = True
            self._record_error("resource_load_timeout: document load did not complete")
        # load 等待文档依赖；字体、激活页和像素稳定仍各自检查，不以全局网络安静作为页面就绪条件。
        self.meta["resource_wait"] = {"condition": "load", "timed_out": timed_out,
            "pending_resources": [request.url for request, status in self._requests.items() if status == "pending"]}
        self._page.set_default_timeout(SCREENSHOT_TIMEOUT_MS)
        # 很多 deck 的 fitDeck()/相机只在 resize 时重排，载入即触发一次促其归位
        try:
            self._page.evaluate("() => window.dispatchEvent(new Event('resize'))")
        except Exception:
            pass

    def _wait_fonts(self) -> None:
        # 等待页面实际使用的字体，不因渲染器硬编码的字体名额外加载未使用的字形。
        try:
            with self._timed("font_ready"):
                self._page.evaluate(
                    "() => (document.fonts && document.fonts.ready) "
                    "? document.fonts.ready.then(() => true) "
                    ": true"
                )
        except Exception:
            pass
        # Record which font families actually resolved, for trace/debug of any
        # authoring↔export font drift.
        try:
            self.loaded_font_families = self._page.evaluate(
                "() => { const s=new Set(); "
                "try{ for (const f of document.fonts) if (f.status==='loaded') s.add(f.family); }catch(e){} "
                "return [...s]; }"
            )
        except Exception:
            self.loaded_font_families = []
        failures = self._page.evaluate("""() => [
          ...[...document.images].filter(i => i.getClientRects().length && getComputedStyle(i).visibility !== 'hidden'
            && (!i.complete || !i.naturalWidth)).map(i => i.currentSrc || i.src),
          ...[...document.fonts].filter(f => f.status === 'error').map(f => 'font: ' + f.family)
        ]""")
        if failures:
            raise ValueError(f"render_resource_unavailable: {failures}")

    def _grab(self) -> tuple[bytes, str, float, tuple]:
        png = self._screenshot()
        with self._timed("geometry_check"):
            measure_geometry(self._page, self.canvas)
        sig = self._page.evaluate(_ACTIVE_SIGNAL_JS)
        std, blocks = _img_stats(png)
        return png, sig, std, blocks

    def _signal(self) -> tuple[int, int]:
        try:
            return _parse_sig(self._page.evaluate(_ACTIVE_SIGNAL_JS))
        except Exception:
            return -1, 0

    def _layout_geometry(self) -> dict:
        try:
            value = self._page.evaluate(_LAYOUT_GEOMETRY_JS)
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _ensure_target_active(self, n: int) -> bool:
        """Activate the nth DOM slide and prove it is the sole visible target.

        Returns True when the URL/player had not already landed on the target.
        The renderer never captures after a known navigation mismatch.
        """
        before_idx, before_total = self._signal()
        activated = self._page.evaluate(
            """n => {
              const slides = [...document.querySelectorAll('.slide[data-slide]')];
              const target = slides[n - 1];
              if (!target) return {ok:false, reason:'missing-target', total:slides.length};
              if (!window.cleanDeck || typeof window.cleanDeck.go !== 'function') {
                return {ok:false, reason:'missing-cleanDeck', total:slides.length};
              }
              window.cleanDeck.go(Number(target.dataset.slide));
              return {ok:true, total:slides.length};
            }""",
            n,
        )
        if not isinstance(activated, dict) or not activated.get("ok"):
            reason = activated.get("reason") if isinstance(activated, dict) else "invalid-result"
            raise RuntimeError(f"cannot activate target page {n}: {reason}")
        # BUG-M:特殊封面/自由版式页的入场 CSS 可能在 3s 内 opacity 稳定不到 0.99
        # (渐入落在阈值下、或 .active 未套到 special layout),旧代码 wait_for_function
        # 超时直接硬 raise → 整页 render_failed → owner 熔断 → 整册被一页拖垮。
        # 软兜底:就绪门只是"最佳时机"提示,超时就记一笔继续截图(略低 opacity 的
        # 封面截图仍可用),不因单页就绪门失败而 sink 掉整个 deck。
        try:
            self._page.wait_for_function(
                """n => {
              const slides = [...document.querySelectorAll('.slide[data-slide]')];
              const target = slides[n - 1];
              const active = slides.filter(slide => slide.classList.contains('active'));
              if (!target || active.length !== 1 || active[0] !== target) return false;
              const style = getComputedStyle(target);
              const rect = target.getBoundingClientRect();
              return style.display !== 'none'
                && style.visibility !== 'hidden'
                && Number(style.opacity || '1') >= 0.99
                && rect.width > 0 && rect.height > 0;
            }""",
                arg=n,
                timeout=ACTIVE_READY_TIMEOUT_MS,
            )
        except Exception as exc:  # noqa: BLE001
            # 只软兜底就绪门的超时;其它异常照常抛出。playwright 的 TimeoutError 在此处
            # 惰性导入不便直接引用,按类名判定既避免 import 顺序问题又不吞真错误。
            if type(exc).__name__ not in {"TimeoutError", "PlaywrightTimeoutError", "Error"}:
                raise
            # 就绪门超时:不硬失败,记一笔后继续截图。after 的 _signal 仍会校验目标页
            # 确实是当前活动页(idx/total),真正没切到目标页才会 RuntimeError。
            self._note(f"active_ready_gate_timeout(page={n})")
        after_idx, after_total = self._signal()
        if after_idx != n - 1 or after_total < n:
            raise RuntimeError(
                f"target page {n} did not settle "
                f"(idx={after_idx}, total={after_total})"
            )
        corrected = before_idx != n - 1 or before_total < n
        if corrected:
            self._note(
                f"query_jump_recovered(page={n}, idx={before_idx}, total={before_total})"
            )
        return corrected

    def _restart_active_motion(self) -> None:
        """Restart the current page entrance after loading has settled.

        The player activates its first page during window load. Waiting for
        Resource/font loading can consume the complete short entrance before the renderer
        reaches its early sample. Toggling the same active class with a forced
        layout keeps the screenshot timeline honest without changing delivery
        HTML or extending the user-visible motion.
        """
        self._page.evaluate(
            """() => {
              const slide = document.querySelector('.slide.active[data-slide]');
              if (!slide) return false;
              slide.classList.remove('active');
              void slide.offsetWidth;
              slide.classList.add('active');
              return true;
            }"""
        )

    def _grab_with_blank_retry(self) -> tuple[bytes, float, tuple, bool, bool]:
        """Capture a final frame and retry once when the first frame is blank."""
        png, _sig, std, blocks = self._grab()
        if std >= BLANK_STD_THRESH:
            return png, std, blocks, False, False
        self._settle(BLANK_RETRY_MS)
        retry_png, _retry_sig, retry_std, retry_blocks = self._grab()
        if retry_std >= std:
            png, std, blocks = retry_png, retry_std, retry_blocks
        return png, std, blocks, True, std >= BLANK_STD_THRESH

    # ── 三帧采样（motion check）────────────────────────────────────────
    def _sample_page(self, already_waited_ms: int = 0) -> dict:
        """对"刚到达"的当前页采样。返回 {png, std, blocks, entrance_diff, live_diff}。

        already_waited_ms：调用前已经等过的时间（goto 的 settle / 翻页的 NAV_SETTLE）。
        无 motion-check 时只补足到 final 时刻截一帧。
        """
        sample_started = time.monotonic() - already_waited_ms / 1000
        if not self.motion_check:
            remain = max(0, FINAL_CAPTURE_MS - already_waited_ms)
            self._settle(remain)
            png, std, blocks, retried, recovered = self._grab_with_blank_retry()
            return {"png": png, "std": std, "blocks": blocks,
                    **self._resource_receipt(),
                    "entrance_cells": None, "live_cells": None,
                    "geometry": self._layout_geometry(),
                    "capture_retry": retried,
                    "capture_recovered": recovered}

        # early 帧（重触发后约 0.12s；已等超过就立刻采）
        self._settle(max(0, EARLY_MS - already_waited_ms))
        early_png = self._screenshot()
        # early 截图期间的真实耗时也属于稳定窗口；正式帧仍不得早于约定时刻。
        self._settle(max(0, FINAL_CAPTURE_MS - (time.monotonic() - sample_started) * 1000))
        png, std, blocks, retried, recovered = self._grab_with_blank_retry()
        geometry = self._layout_geometry()
        # live 帧（探测持续动画）
        self._settle(LIVE_EXTRA_MS)
        live_png = self._screenshot()
        final_m = _motion_blocks(png)
        return {
            "png": png, "std": std, "blocks": blocks,
            **self._resource_receipt(),
            "entrance_cells": _moving_cells(_motion_blocks(early_png), final_m),
            "live_cells": _moving_cells(final_m, _motion_blocks(live_png)),
            "geometry": geometry,
            "capture_retry": retried,
            "capture_recovered": recovered,
        }

    # ── 单页渲染（--page N）──────────────────────────────────────────
    def render_page(self, n: int) -> list[Path]:
        self.meta["mode"] = f"page:{n}"
        self._load(f"?slide={n}")
        corrected = self._ensure_target_active(n)
        self.meta["nav"] = "cleanDeck-recovered" if corrected else "query-jump"
        self._wait_fonts()
        self._restart_active_motion()
        s = self._sample_page(already_waited_ms=0)
        out = self.out_dir / f"slide_{n:02d}.png"
        with self._timed("png_write"):
            _atomic_bytes(out, s["png"], suffix=".png")
        self._add_page_meta(n, out, s)
        self.meta["n_pages"] = max(self.meta["n_pages"], n)
        return [out]

    # ── 全部页渲染（--all）───────────────────────────────────────────
    def render_all(self) -> list[Path]:
        self.meta["mode"] = "all"
        self._load("?slide=1")
        self._ensure_target_active(1)
        _idx0, total0 = self._signal()
        if total0 < 1:
            raise RuntimeError("deck has no detectable slides")
        total = min(total0, MAX_PAGES)
        self.meta["nav"] = "verified-query-jump"
        samples: list[dict] = []
        for n in range(1, total + 1):
            if n > 1:
                self._load(f"?slide={n}")
                self._ensure_target_active(n)
            self._wait_fonts()
            self._restart_active_motion()
            samples.append(self._sample_page(already_waited_ms=0))

        # 落盘
        paths: list[Path] = []
        for i, s in enumerate(samples, start=1):
            out = self.out_dir / f"slide_{i:02d}.png"
            with self._timed("png_write"):
                _atomic_bytes(out, s["png"], suffix=".png")
            self._add_page_meta(i, out, s)
            paths.append(out)
        self.meta["n_pages"] = len(paths)
        if total0 > MAX_PAGES:
            self._note(f"page_count_capped({total0}>{MAX_PAGES})")
        # contact sheet（拼图，整 deck 一眼扫）
        sheet = self._contact_sheet(paths)
        if sheet:
            paths.append(sheet)
        return paths

    def _add_page_meta(self, n: int, png_path: Path, s: dict) -> None:
        self.rendered_this_run.add(n)
        blank = s["std"] < BLANK_STD_THRESH
        static = (
            s["entrance_cells"] is not None
            and 0 <= s["entrance_cells"] < MOTION_MIN_CELLS
            and 0 <= s["live_cells"] < MOTION_MIN_CELLS
        )
        self.meta["pages"].append({
            "page": n, "png": str(png_path), "blank": blank,
            "entrance_cells": s["entrance_cells"], "live_cells": s["live_cells"],
            "static": static, "geometry": s.get("geometry", {}),
            "loaded_resources": s["loaded_resources"],
            "failed_resources": s["failed_resources"],
            "input_hashes": s["input_hashes"],
            "png_sha256": hashlib.sha256(s["png"]).hexdigest(),
            "capture_retry": bool(s.get("capture_retry")),
            "capture_recovered": bool(s.get("capture_recovered")),
        })
        if blank:
            self.meta["blank_pages"].append(n)
        if s.get("capture_retry"):
            self.meta["capture_retries"].append(n)
            if s.get("capture_recovered"):
                self.meta["capture_recovered_pages"].append(n)
            else:
                self._note(f"blank_retry_failed(page={n})")
        if static:
            self.meta["static_pages"].append(n)

    def _contact_sheet(
        self,
        paths: list[Path],
        *,
        name: str = "contact-sheet.png",
        labels: list[int] | None = None,
    ) -> Path | None:
        try:
            return write_contact_sheet(paths, self.out_dir / name,
                                       width=self.deck_w, height=self.deck_h, labels=labels)
        except Exception as e:  # noqa: BLE001
            self._note(f"contact_sheet failed: {type(e).__name__}")
            return None

    def _note(self, text: str) -> None:
        self.meta["notes"] = (self.meta["notes"] + "; " if self.meta["notes"] else "") + text

    # ── 收尾 ─────────────────────────────────────────────────────────
    def finalize(self) -> None:
        self.meta["elapsed_before_cleanup_s"] = round(time.monotonic() - self._started, 6)
        self.meta["console_errors"] = self.console_errors
        # --page 模式合并进已有 render.json（保留其他页的记录）；--all 整体覆盖
        manifest = self.out_dir / "render.json"
        if self.meta["mode"].startswith("page:") and manifest.exists():
            try:
                old = json.loads(manifest.read_text(encoding="utf-8"))
                kept = [p for p in old.get("pages", [])
                        if p["page"] not in {q["page"] for q in self.meta["pages"]}]
                self.meta["pages"] = sorted(kept + self.meta["pages"], key=lambda p: p["page"])
                self.meta["n_pages"] = max(self.meta["n_pages"], old.get("n_pages", 0))
                old_errs = [e for e in old.get("console_errors", []) if e not in self.console_errors]
                self.meta["console_errors"] = (old_errs + self.console_errors)[:MAX_CONSOLE_ERRORS]
                self.meta["blank_pages"] = sorted({p["page"] for p in self.meta["pages"] if p["blank"]})
                self.meta["static_pages"] = sorted(
                    {p["page"] for p in self.meta["pages"] if p.get("static")})
                self.meta["capture_retries"] = sorted(
                    {p["page"] for p in self.meta["pages"] if p.get("capture_retry")})
                self.meta["capture_recovered_pages"] = sorted({
                    p["page"]
                    for p in self.meta["pages"]
                    if p.get("capture_recovered")
                })
            except Exception:
                pass
        _atomic_json(manifest, self.meta)


def main() -> int:
    global MOTION_MIN_CELLS
    reset_child_process_signals()
    ap = argparse.ArgumentParser(description="渲染完整 HTML 演示为逐页 PNG")
    ap.add_argument("presentation", help="present.html 路径")
    ap.add_argument("--workspace", type=Path, required=True)
    ap.add_argument("out_dir", help="截图输出目录（render.json 也写在这里）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--page", type=int, help="渲染第 N 页（1-based），优先 ?slide=N 直跳")
    g.add_argument("--all", action="store_true", help="按键翻页渲染全部页 + contact sheet")
    ap.add_argument("--no-motion-check", action="store_true", help="关闭三帧动态采样（更快）")
    ap.add_argument("--motion-min-cells", type=int, default=MOTION_MIN_CELLS,
                    help=f"变化格数 < 此值视为无运动（默认 {MOTION_MIN_CELLS}/4096）")
    args = ap.parse_args()
    MOTION_MIN_CELLS = args.motion_min_cells

    html_path = Path(args.presentation)
    if not html_path.exists():
        print(f"present.html 不存在: {html_path}", file=sys.stderr)
        return 2
    if args.page is not None and args.page < 1:
        print(f"--page 必须 >= 1，收到 {args.page}", file=sys.stderr)
        return 2
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Slide workers may author pages in parallel, but launching several independent
    # Chromium processes at the same instant is unreliable in the production pod
    # (Playwright reports TargetClosed before a page is created). Queue only the
    # browser-backed section; model work and HTML editing remain fully parallel.
    with render_slot():
        r = DeckRenderer(html_path, out_dir, workspace=args.workspace, motion_check=not args.no_motion_check)
        try:
            r.start()
            paths = r.render_page(args.page) if args.page is not None else r.render_all()
            # Publish PNG metadata before Chromium cleanup. Some production
            # browsers finish the screenshot but hang while closing; the Broker
            # can now verify the completed artifacts and stop only that stale
            # process instead of forcing the Slide to render again.
            r.finalize()
        except Exception as e:  # noqa: BLE001
            print(f"渲染失败: {type(e).__name__}: {e}", file=sys.stderr)
            r.finalize()
            return 1
        finally:
            r.close()

    if not paths:
        print("一页都没截到（present.html 打不开或无内容）", file=sys.stderr)
        return 1

    # 告警先打，PNG 路径在后（末行保证是路径）
    for err in r.console_errors:
        print(f"[console] {err}")
    for p in r.meta["pages"]:
        if p["page"] not in r.rendered_this_run:
            continue  # --page 模式下 render.json 合并了历史页，告警只报本次渲染的页
        if p.get("static"):
            print(f"[static] page {p['page']}: entrance_cells={p['entrance_cells']} "
                  f"live_cells={p['live_cells']} — 本页无可见动态（若是有意的静帧设计可忽略）")
        if p.get("blank"):
            print(f"[blank] page {p['page']}: 近纯色/空白页")
    for p in paths:
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
