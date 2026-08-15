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
- early（资源稳定并重触发 active 入场后 ~0.25s）
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
import json
import math
import os
import signal
import sys
import tempfile
import time
import warnings
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path

# Re-exec with the Harness interpreter when a direct caller used the wrong Python.
if 'PLAYWRIGHT_REEXEC' not in os.environ:
    try:
        import playwright  # noqa: F401
    except ImportError:
        _repo_python = Path(__file__).resolve().parents[3] / '.venv/bin/python'
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
DECK_W, DECK_H = 1600, 900
DEVICE_SCALE = 1               # CPU-only swiftshader 下 2x 会让重型 3D 页光栅慢一倍易超时
MAX_PAGES = max(1, int(os.environ.get("PPT_RENDER_MAX_PAGES", "200")))
EARLY_MS = 120                 # motion-check：重触发后尽早采样，避免短入场被缓动提前吃完
FINAL_CAPTURE_MS = max(3000, int(os.environ.get("PPT_RENDER_FINAL_MS", "3000")))
FINAL_EXTRA_MS = FINAL_CAPTURE_MS - EARLY_MS
                               # 正式截图距重触发至少 3 秒；给入场动画、字体和延迟布局留足时间。
LIVE_EXTRA_MS = 600            # final 之后再等这么久截 live 帧（探测持续动画）
GOTO_TIMEOUT_MS = 30000
NETWORKIDLE_TIMEOUT_MS = 8000
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
SPECIAL_PAGE_TYPES = {"cover", "section-divider", "closing"}


def _temporary_parent(path: Path) -> Path:
    parent = path.parent
    if parent.name == "renders":
        controlled = parent.parent / "tmp"
        controlled.mkdir(parents=True, exist_ok=True)
        return controlled
    return parent


def _cleanup_temporary_parent(path: Path, temporary_parent: Path) -> None:
    if temporary_parent == path.parent.parent / "tmp":
        try:
            temporary_parent.rmdir()
        except OSError:
            pass


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
  const paint = (selector) => {
    const element = slide.querySelector(selector);
    if (!element) return null;
    const style = getComputedStyle(element);
    return {
      background_color: style.backgroundColor || '',
      background_image: style.backgroundImage || '',
    };
  };
  return {
    page_type: slide.dataset.pageType || '',
    frame: slide.dataset.frame || '',
    page_family: slide.dataset.pageFamily || '',
    special_layout: slide.dataset.specialLayout || '',
    canvas_variant: slide.dataset.canvasVariant || '',
    special_background_paint: paint('.special-background'),
    boxes: {
      page_frame: box('.page-frame'),
      special_background: box('.special-background'),
      special_overlay: box('.special-overlay'),
      special_safe: box('.special-safe'),
      header: box('.page-header, .special-header'),
      title: box('.page-title'),
      subtitle: box('.page-subtitle'),
      section_number: box('.section-number'),
      divider_copy: box('.divider-copy'),
      cover_visual: box('.slot-cover-visual'),
      section_motif: box('.slot-section-motif'),
      footer: box('.page-footer, .special-footer'),
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


class DeckRenderer:
    """单次 CLI 调用：起独立 chromium → 渲染 → 关闭。"""

    def __init__(self, html_path: Path, out_dir: Path, motion_check: bool = True) -> None:
        self.html_path = html_path
        self.out_dir = out_dir
        self.motion_check = motion_check
        self.console_errors: list[str] = []
        self.rendered_this_run: set[int] = set()
        self.meta: dict = {
            "present": str(html_path),
            "w": DECK_W, "h": DECK_H,
            "mode": "", "nav": "",
            "n_pages": 0,
            "pages": [],            # [{page, png, blank, entrance_diff, live_diff, static}]
            "console_errors": [],
            "static_pages": [],
            "blank_pages": [],
            "capture_retries": [],
            "capture_recovered_pages": [],
            "special_contact_sheet": "",
            "notes": "",
        }
        self._pw = None
        self._browser = None
        self._page = None

    # ── 浏览器生命周期 ────────────────────────────────────────────────
    def start(self) -> None:
        ensure_browser_libs()
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                  "--use-gl=swiftshader", "--enable-unsafe-swiftshader"],
        )
        ctx = self._browser.new_context(
            viewport={"width": DECK_W, "height": DECK_H},
            device_scale_factor=DEVICE_SCALE,
            ignore_https_errors=True,
            bypass_csp=True,
        )
        self._page = ctx.new_page()
        self._page.set_default_timeout(GOTO_TIMEOUT_MS)
        self._page.on("console", self._on_console)
        self._page.on("pageerror", self._on_pageerror)

    def close(self) -> None:
        for closer in (lambda: self._browser.close(), lambda: self._pw.stop()):
            try:
                closer()
            except Exception:
                pass

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

    # ── 加载与导航 ───────────────────────────────────────────────────
    def _load(self, query: str = "") -> None:
        """加载 deck，不做长 settle（采样时序由 _sample_page 控制，否则 early 帧会错过入场动画）。"""
        url = self.html_path.resolve().as_uri() + query
        self._page.goto(url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
        try:
            self._page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_TIMEOUT_MS)
        except Exception:
            pass
        self._page.set_default_timeout(SCREENSHOT_TIMEOUT_MS)
        # 很多 deck 的 fitDeck()/相机只在 resize 时重排，载入即触发一次促其归位
        try:
            self._page.evaluate("() => window.dispatchEvent(new Event('resize'))")
        except Exception:
            pass

    def _grab(self) -> tuple[bytes, str, float, tuple]:
        png = self._page.screenshot(clip={"x": 0, "y": 0, "width": DECK_W, "height": DECK_H})
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
        networkidle can consume the complete short entrance before the renderer
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
        self._page.wait_for_timeout(BLANK_RETRY_MS)
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
        if not self.motion_check:
            remain = max(0, EARLY_MS + FINAL_EXTRA_MS - already_waited_ms)
            self._page.wait_for_timeout(remain)
            png, std, blocks, retried, recovered = self._grab_with_blank_retry()
            return {"png": png, "std": std, "blocks": blocks,
                    "entrance_cells": None, "live_cells": None,
                    "geometry": self._layout_geometry(),
                    "capture_retry": retried,
                    "capture_recovered": recovered}

        # early 帧（重触发后约 0.25s；已等超过就立刻采）
        self._page.wait_for_timeout(max(0, EARLY_MS - already_waited_ms))
        early_png = self._page.screenshot(clip={"x": 0, "y": 0, "width": DECK_W, "height": DECK_H})
        # final 帧（正式截图）
        self._page.wait_for_timeout(FINAL_EXTRA_MS)
        png, std, blocks, retried, recovered = self._grab_with_blank_retry()
        geometry = self._layout_geometry()
        # live 帧（探测持续动画）
        self._page.wait_for_timeout(LIVE_EXTRA_MS)
        live_png = self._page.screenshot(clip={"x": 0, "y": 0, "width": DECK_W, "height": DECK_H})
        final_m = _motion_blocks(png)
        return {
            "png": png, "std": std, "blocks": blocks,
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
        self._restart_active_motion()
        s = self._sample_page(already_waited_ms=0)
        out = self.out_dir / f"slide_{n:02d}.png"
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
            self._restart_active_motion()
            samples.append(self._sample_page(already_waited_ms=0))

        # 落盘
        paths: list[Path] = []
        for i, s in enumerate(samples, start=1):
            out = self.out_dir / f"slide_{i:02d}.png"
            _atomic_bytes(out, s["png"], suffix=".png")
            self._add_page_meta(i, out, s)
            paths.append(out)
        self.meta["n_pages"] = len(paths)
        if total0 > MAX_PAGES:
            self._note(f"page_count_capped({total0}>{MAX_PAGES})")
        special_rows = [
            (index + 1, paths[index])
            for index, sample in enumerate(samples)
            if sample.get("geometry", {}).get("page_type")
            in SPECIAL_PAGE_TYPES
        ]
        if special_rows:
            special = self._contact_sheet(
                [path for _, path in special_rows],
                name="contact-sheet-special.png",
                labels=[number for number, _ in special_rows],
            )
            if special:
                self.meta["special_contact_sheet"] = str(special)
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
            from PIL import Image, ImageDraw

            # Four 400×225 columns exactly match the 1600 px vision edge used by
            # Review, keeping divider motifs and small labels legible.
            thumb_w, thumb_h = 400, 225
            cols = min(4, max(1, len(paths)))
            rows = math.ceil(len(paths) / cols)
            sheet = Image.new("RGB", (cols * thumb_w, rows * thumb_h), (24, 24, 24))
            draw = ImageDraw.Draw(sheet)
            for i, p in enumerate(paths):
                im = Image.open(p).convert("RGB").resize((thumb_w, thumb_h))
                x, y = (i % cols) * thumb_w, (i // cols) * thumb_h
                sheet.paste(im, (x, y))
                draw.rectangle([x, y, x + 34, y + 18], fill=(0, 0, 0))
                label = labels[i] if labels is not None else i + 1
                draw.text((x + 6, y + 3), f"{label:02d}", fill=(255, 255, 255))
            out = self.out_dir / name
            payload = BytesIO()
            sheet.save(payload, format="PNG")
            _atomic_bytes(out, payload.getvalue(), suffix=".png")
            return out
        except Exception as e:  # noqa: BLE001
            self._note(f"contact_sheet failed: {type(e).__name__}")
            return None

    def _note(self, text: str) -> None:
        self.meta["notes"] = (self.meta["notes"] + "; " if self.meta["notes"] else "") + text

    # ── 收尾 ─────────────────────────────────────────────────────────
    def finalize(self) -> None:
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
        r = DeckRenderer(html_path, out_dir, motion_check=not args.no_motion_check)
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
