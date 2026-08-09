#!/usr/bin/env python3
"""Deterministic workspace CLI for Mural Presenter.

It owns environment preflight, workspace preparation, asset registration,
contact sheets, speaker-script synchronization, delivery validation, and the
final ``present.html`` player. It does not make page-design decisions.

在工作区根运行(slides/ 旁边):
    python deck.py preflight ROOT
    python deck.py sync ROOT --expected N
    python deck.py prepare ROOT --expected N
    python deck.py asset-register ROOT --path assets/file.png --origin material
    python deck.py asset-download ROOT --url "https://example.org/photo.jpg"
    python deck.py material-figure ROOT --source materials/_work/material_01/_raw/paper.pdf_pages/p002.png \
      --path assets/paper-figure-01.png --figure-id "Figure 1" --source-page 2 \
      --box 0.12,0.34,0.88,0.72
    python deck.py asset-assign ROOT --path assets/file.png --asset-id cover-hero --group-id hero
    python deck.py asset-contact ROOT --group-id hero
    python deck.py asset-review ROOT --group-id hero --ready cover-hero
    python deck.py contact ROOT [--expected N | --focus 3,7 --label group-id]
    python deck.py build ROOT --expected N

每页是独立的自包含 HTML(各自的 base.css / ECharts / inline style),所以**不能内联拼接**
(CSS/JS 会打架)——用 `<iframe>` 逐页加载即可完美隔离。生成的 present.html:
- **每页一个 iframe、交叉淡入(crossfade),无白闪**:目标页加载好前保持旧页可见,
  已加载过的页切换是瞬时淡入;只创建当前页 + 相邻页(懒加载,省内存);
- **轻量内容入场**:播放器识别标题、正文一级内容和页脚,按阅读顺序依次淡入;
  动效只注入最终播放器,不改逐页 HTML、不影响静态渲染,并尊重 reduced-motion;
- **画布尺寸自适应**:build 时从 base.css 探测 `--canvas-w/--canvas-h`(横版 1600×900 /
  竖版 900×1600 都对),运行时再从实际加载的 `.slide` 复测兜底;按窗口等比缩放居中(letterbox);
- 键盘 ←/→ / 空格 / PageUp/Down 翻页、Home/End 首尾、F 全屏;点击右/左半屏翻页;触摸滑动;
- 顶部进度条、底部 HUD、`#N` URL 深链。
生成的播放器自包含、零运行时依赖；`contact` 子命令使用安装阶段提供的 Pillow。
"""
import argparse
from contextlib import contextmanager
import glob
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from bundle_fonts import (
    FAMILY_FACES,
    _family_available,
    _font_source_dirs,
    _load_custom_config,
    _validate_font_allowlist,
    bundle_workspace,
    render_all,
    validate_font_bundle,
    validate_render_freshness,
)


def _optional_timeout(name: str):
    """Use a wall clock only when deployment explicitly requests one."""
    value = str(os.environ.get(name, "") or "").strip().lower()
    if value in {"", "0", "none", "off", "disabled", "false"}:
        return None
    seconds = float(value)
    return None if seconds <= 0 else seconds


def _reexec_configured_runtime() -> None:
    """Pin deterministic Skill commands to the configured runtime Python.

    Model-written shell prefixes must not silently switch preflight to a base
    interpreter with a missing/broken fontTools installation.  The worker sets
    ``MURAL_RUNTIME_PYTHON``; direct developer invocations keep using the
    current interpreter.
    """
    configured = str(os.environ.get("MURAL_RUNTIME_PYTHON") or "").strip()
    if not configured:
        return
    target = Path(configured).expanduser()
    if not target.is_file() or not os.access(target, os.X_OK):
        raise RuntimeError(f"MURAL_RUNTIME_PYTHON 不可执行: {target}")
    if target.resolve() == Path(sys.executable).resolve():
        return
    os.execve(
        str(target),
        [str(target), str(Path(__file__).resolve()), *sys.argv[1:]],
        os.environ.copy(),
    )


def _detect_canvas(base_dir):
    """从工作区的 base.css 探测画布尺寸(--canvas-w/--canvas-h)。
    找不到就回退默认横版 1600×900;运行时 JS 还会从 .slide 复测兜底。"""
    w, h = 1600, 900
    for cand in ("base.css", os.path.join("slides", "base.css")):
        path = os.path.join(base_dir, cand)
        if not os.path.isfile(path):
            continue
        try:
            css = open(path, encoding="utf-8").read()
        except Exception:
            continue
        mw = re.search(r"--w:\s*([0-9.]+)px", css)
        mh = re.search(r"--h:\s*([0-9.]+)px", css)
        if mw and mh:
            w, h = int(float(mw.group(1))), int(float(mh.group(1)))
            break
    return w, h


def _build_player(root: Path, expected: int | None = None):
    root = root.resolve()
    sdir = root / "slides"
    out = root / "present.html"
    files = sorted(f for f in glob.glob(str(sdir / "slide_*.html"))
                   if ".bak." not in os.path.basename(f))
    if not files:
        print(f"没找到 {sdir}/slide_*.html", file=sys.stderr)
        return 1
    if expected is not None and len(files) != expected:
        print(f"expected {expected} slides, found {len(files)}", file=sys.stderr)
        return 1
    base = str(root)
    try:
        manifest = bundle_workspace(Path(base))
    except Exception as exc:
        print(f"字体打包失败: {exc}", file=sys.stderr)
        return 1
    font_errors = validate_font_bundle(Path(base))
    if font_errors:
        print("字体交付校验失败: " + "; ".join(font_errors), file=sys.stderr)
        return 1
    freshness_errors = validate_render_freshness(Path(base))
    if freshness_errors:
        print("检测到陈旧 PNG，正使用 Deck 自带字体重新渲染", file=sys.stderr)
        try:
            render_all(Path(base))
        except Exception as exc:
            print(f"便携字体重渲失败: {exc}", file=sys.stderr)
            return 1
        freshness_errors = validate_render_freshness(Path(base))
        if freshness_errors:
            print("渲染新鲜度校验失败: " + "; ".join(freshness_errors), file=sys.stderr)
            return 1
    rel = [os.path.relpath(f, base).replace(os.sep, "/") for f in files]
    cw, ch = _detect_canvas(base)
    html = (TPL.replace("__SLIDES__", json.dumps(rel, ensure_ascii=False))
               .replace("__CW__", str(cw)).replace("__CH__", str(ch)))
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(
        f"wrote {out} ({len(rel)} slides, canvas {cw}x{ch}, "
        f"{len(manifest.get('faces', []))} portable font faces)"
    )
    return 0


SPEECH_HEADINGS = {"口语讲稿", "讲述内容", "讲稿", "口播", "spoken script", "speaker script", "speech", "talk track"}
SOURCE_HEADINGS = {"来源", "参考资料", "参考资料（不朗读）", "参考资料(不朗读)", "sources", "sources (not spoken)", "references", "provenance"}
SCREEN_COPY_HEADINGS = {"最终屏显文案", "on-screen copy", "on-screen copy (exact)", "screen copy"}
VISUAL_HEADINGS = {"视觉实现", "visual implementation", "visual handoff", "visual direction"}
PAGE_RE = re.compile(r"^slide_(\d+)\.png$")
_PICTOGRAPH_RE = re.compile(r"[\u2600-\u27bf\U0001f000-\U0001faff]")
_IMAGE_PRESENTATIONS = {"subject-only", "framed-scene", "full-bleed", "evidence-crop"}


def _normalize_heading(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower()).rstrip(":：")


def _sections(text: str) -> dict[str, str]:
    matches = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", text))
    return {
        _normalize_heading(match.group(1)): text[
            match.end(): matches[index + 1].start() if index + 1 < len(matches) else len(text)
        ].strip()
        for index, match in enumerate(matches)
    }


def _first_section(parts: dict[str, str], aliases: set[str]) -> str:
    wanted = {_normalize_heading(alias) for alias in aliases}
    return next((body.strip() for heading, body in parts.items() if heading in wanted), "")


def _plan_title(text: str) -> str:
    match = re.search(
        r"(?mi)^[ \t]*[-*+][ \t]*(?:\*\*)?(?:title|标题|主标题|章节名|幕名)"
        r"(?:\*\*)?(?:[ \t]*(?:\([^\r\n)]*\)|（[^\r\n）]*）))?[ \t]*[:：][ \t]*([^\r\n]+?)[ \t]*$",
        text,
    )
    if match:
        title = re.sub(r"^[-*+]\s+", "", match.group(1).strip().strip("` ")).strip()
        if title:
            return title
    heading = re.search(r"(?mi)^#\s+Slide\s+\d+\s*(?:—|-)\s*(.+?)\s*$", text)
    if heading:
        title = heading.group(1).strip().strip("` ")
        if title:
            return title
    raise ValueError("missing `- 标题：...`, `- title: ...`, or `# Slide NN — ...` title")


def _plan_files(root: Path, expected: int | None) -> list[tuple[int, Path]]:
    found = []
    for path in (root / "plan").glob("slide_*.md"):
        match = re.fullmatch(r"slide_(\d+)\.md", path.name)
        if match:
            found.append((int(match.group(1)), path))
    found.sort()
    if not found:
        raise ValueError("no plan/slide_NN.md files found")
    numbers = [number for number, _ in found]
    wanted = list(range(1, (expected or len(found)) + 1))
    if numbers != wanted:
        raise ValueError(f"plan pages must be continuous: expected {wanted}, got {numbers}")
    return found


def _validate_no_pictographs(root: Path, expected: int | None, *, include_html: bool) -> None:
    """Keep emoji/dingbat glyphs out of audience-facing copy and slide HTML.

    The Skill deliberately routes icons through local SVG/CSS assets.  Checking
    the canonical screen-copy section catches the error before production; the
    HTML pass prevents a Slide worker from reintroducing decorative glyphs.
    """
    errors = []
    for _, path in _plan_files(root, expected):
        screen_copy = _first_section(_sections(path.read_text(encoding="utf-8")), SCREEN_COPY_HEADINGS)
        glyphs = sorted(set(_PICTOGRAPH_RE.findall(screen_copy)))
        if glyphs:
            errors.append(f"{path.relative_to(root)}: {''.join(glyphs)}")
    if include_html:
        slide_paths = sorted((root / "slides").glob("slide_*.html"))
        if expected is not None and len(slide_paths) != expected:
            errors.append(f"slides: expected {expected}, found {len(slide_paths)}")
        for path in slide_paths:
            glyphs = sorted(set(_PICTOGRAPH_RE.findall(path.read_text(encoding="utf-8"))))
            if glyphs:
                errors.append(f"{path.relative_to(root)}: {''.join(glyphs)}")
    if errors:
        raise ValueError(
            "屏显文案/HTML 不得使用 emoji 或 Unicode 图标；"
            "请改用本地 SVG、CSS 形状或文字：\n" + "\n".join(errors)
        )


def _validate_image_presentations(root: Path, expected: int | None) -> None:
    """Require one explicit rendering contract for every planned raster page."""
    errors = []
    for _, path in _plan_files(root, expected):
        visual = _first_section(_sections(path.read_text(encoding="utf-8")), VISUAL_HEADINGS)
        raster_medium = re.search(
            r"(?im)^\s*[-*+]\s*medium\s*[:：].*(?:photo|photograph|generated[ -]?image|bitmap|raster|生成图|位图|照片)",
            visual,
        )
        raster_path = re.search(r"(?i)assets/[A-Za-z0-9_./-]+\.(?:png|jpe?g|webp|gif)", visual)
        if not raster_medium and not raster_path:
            continue
        match = re.search(
            r"(?im)^\s*[-*+]\s*presentation\s*[:：]\s*`?([A-Za-z-]+)", visual
        )
        value = match.group(1).lower() if match else ""
        if value not in _IMAGE_PRESENTATIONS:
            errors.append(
                f"{path.relative_to(root)}: raster page requires presentation="
                "subject-only|framed-scene|full-bleed|evidence-crop"
            )
            continue
        if value == "subject-only" and not re.search(
            r"(?im)^\s*[-*+]\s*subject_only\s*[:：]\s*true\s*$", visual
        ):
            errors.append(f"{path.relative_to(root)}: subject-only requires subject_only: true")
    if errors:
        raise ValueError("位图展示合同不完整：\n" + "\n".join(errors))


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _preflight_attachment_inventory(root: Path) -> tuple[int, list[str], list[str]]:
    """Inspect only attachment metadata; Material still owns content reading."""
    manifest = root / "materials/attachments.json"
    if not manifest.is_file():
        return 0, [], []
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        return 0, [f"附件清单无法解析: {exc}"], []
    entries = payload.get("attachments") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return 0, ["附件清单缺少 attachments 数组"], []
    errors: list[str] = []
    warnings: list[str] = []
    suffixes: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            errors.append("附件清单包含非对象条目")
            continue
        name = str(entry.get("name") or entry.get("raw") or "unnamed")
        status = str(entry.get("status") or "").lower()
        if status in {"missing", "failed"}:
            errors.append(f"附件不可用: {name} ({status})")
        raw = str(entry.get("raw") or "")
        if raw and not (root / raw).is_file():
            errors.append(f"附件原件不存在: {raw}")
        suffixes.add(Path(name).suffix.lower())
    legacy_office = {".doc", ".ppt", ".xls", ".rtf", ".odt", ".odp", ".ods"}
    media = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".mp4", ".mov", ".mkv", ".webm"}
    if suffixes.intersection(legacy_office) and not (
        shutil.which("libreoffice") or shutil.which("soffice")
    ):
        warnings.append("旧 Office/ODF 附件缺少 LibreOffice；Material 可能只能返回 blocked")
    if suffixes.intersection(media) and not (
        shutil.which("ffmpeg") and shutil.which("ffprobe")
    ):
        warnings.append("音视频附件缺少 FFmpeg/ffprobe；需要现成字幕、ASR 或其他媒体转换能力")
    return len(entries), errors, warnings


def _font_probe_cache_path(source_dirs: list[Path]) -> Path:
    digest = hashlib.sha256()
    digest.update(Path(__file__).resolve().with_name("bundle_fonts.py").read_bytes())
    digest.update(sys.executable.encode("utf-8", errors="ignore"))
    digest.update(sys.version.encode("utf-8", errors="ignore"))
    for directory in source_dirs:
        digest.update(str(directory).encode("utf-8", errors="ignore"))
        try:
            stat = directory.stat()
            digest.update(f"{stat.st_mtime_ns}:{stat.st_size}".encode("ascii"))
        except OSError:
            digest.update(b"missing")
        digest.update(b"\0")
    configured = os.environ.get("MURAL_PREFLIGHT_CACHE_DIR", "").strip()
    cache_dir = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".cache/mural-presenter/preflight"
    )
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        cache_dir = Path(tempfile.gettempdir()) / "mural-presenter-preflight"
        cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"fonts-{digest.hexdigest()[:24]}.json"


def _cached_font_probe(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        ttl = max(
            0.0,
            float(os.environ.get("MURAL_PREFLIGHT_FONT_CACHE_TTL", "3600")),
        )
        if ttl > 0 and time.time() - float(payload.get("checked_at", 0)) <= ttl:
            return payload
    except (OSError, ValueError, TypeError):
        pass
    return None


def _preflight_fonts(root: Path) -> dict:
    try:
        from fontTools.ttLib import TTFont  # noqa: F401
    except (ImportError, AttributeError) as exc:
        raise RuntimeError(
            "当前 Python 缺少可用的 fontTools；请使用运行环境配置的 "
            "MURAL_RUNTIME_PYTHON 原样运行 preflight，不要改 PATH，也不要搜索全盘字体"
        ) from exc
    _validate_font_allowlist()
    source_dirs = _font_source_dirs()
    custom, _roles = _load_custom_config(root)
    cache = _font_probe_cache_path(source_dirs)
    with open(str(cache) + ".lock", "a+", encoding="utf-8") as lock:
        try:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        cached = _cached_font_probe(cache)
        if cached is None:
            available = [
                family for family in FAMILY_FACES
                if _family_available(family, source_dirs)
            ]
            if "Noto Sans SC" not in available:
                raise RuntimeError(
                    "缺少可读取的 Noto Sans SC，无法保证中文渲染和便携字体回退；"
                    "字体发现只检查 PPT_FONT_SOURCE_DIRS、Skill fonts、~/.fonts、"
                    "~/.local/share/fonts 和 /usr/share/fonts，不要执行全盘 find"
                )
            cached = {
                "checked_at": time.time(),
                "available": len(available),
                "declared": len(FAMILY_FACES),
            }
            _atomic_text(cache, json.dumps(cached, ensure_ascii=False) + "\n")
    return {
        "available": int(cached["available"]),
        "declared": int(cached["declared"]),
        "custom": len(custom),
    }


def _browser_probe_cache_path(renderer: Path) -> Path:
    digest = hashlib.sha256()
    digest.update(renderer.read_bytes())
    for value in (
        sys.executable,
        sys.version,
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""),
        os.environ.get("PPT_SKILL_BROWSER_EXE", ""),
        os.environ.get("PPT_SKILL_PLAYWRIGHT_PATHS", ""),
    ):
        digest.update(str(value).encode("utf-8", errors="ignore"))
        digest.update(b"\0")
    configured = os.environ.get("MURAL_PREFLIGHT_CACHE_DIR", "").strip()
    cache_dir = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".cache/mural-presenter/preflight"
    )
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        cache_dir = Path(tempfile.gettempdir()) / "mural-presenter-preflight"
        cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{digest.hexdigest()[:24]}.json"


def _cached_browser_probe(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        status = str(payload.get("status") or "")
        ttl_name = (
            "MURAL_PREFLIGHT_CACHE_TTL"
            if status == "ready"
            else "MURAL_PREFLIGHT_FAILURE_TTL"
        )
        ttl_default = "3600" if status == "ready" else "300"
        ttl = max(0.0, float(os.environ.get(ttl_name, ttl_default)))
        if ttl > 0 and time.time() - float(payload.get("checked_at", 0)) <= ttl:
            return payload
    except (OSError, ValueError, TypeError):
        pass
    return None


def _probe_browser_runtime() -> str:
    """Launch one tiny render, cached per runtime so parallel decks do not stampede."""
    renderer = Path(__file__).resolve().with_name("render.py")
    cache = _browser_probe_cache_path(renderer)
    lock = open(str(cache) + ".lock", "a+", encoding="utf-8")
    locked = False
    try:
        try:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            locked = True
        except (ImportError, OSError):
            pass
        cached = _cached_browser_probe(cache)
        if cached:
            if cached.get("status") == "ready":
                return "cached"
            raise RuntimeError(
                "Chromium/Playwright 环境预检近期已失败: "
                + str(cached.get("error") or "unknown error")
            )
        try:
            with tempfile.TemporaryDirectory(prefix="mural-presenter-preflight-") as temporary:
                temporary_path = Path(temporary)
                html = temporary_path / "probe.html"
                png = temporary_path / "probe.png"
                html.write_text(
                    "<!doctype html><meta charset='utf-8'><style>"
                    "html,body{margin:0;width:100%;height:100%;overflow:hidden}"
                    "*{box-sizing:border-box}.slide{width:640px;height:360px;padding:28px;"
                    "display:grid;grid-template-rows:auto 1fr auto;background:#f5f1e8;color:#171717;"
                    "font-family:'Noto Sans SC',sans-serif}.slide-title{font-size:30px;font-weight:700}"
                    ".slide-body{display:grid;place-items:center;font-size:22px}.slide-footer{font-size:14px}"
                    "</style><section class='slide'><header class='slide-title'>环境预检</header>"
                    "<main class='slide-body'>Browser · Fonts · Pixels</main>"
                    "<footer class='slide-footer'>Mural Presenter</footer></section>",
                    encoding="utf-8",
                )
                try:
                    timeout = max(15.0, float(os.environ.get("MURAL_PREFLIGHT_BROWSER_TIMEOUT", "90")))
                except ValueError:
                    timeout = 90.0
                try:
                    result = subprocess.run(
                        [sys.executable, str(renderer), str(html), str(png), "640", "360"],
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=timeout,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeError(f"Chromium 预检在 {timeout:g}s 内未完成") from exc
                if result.returncode or not png.is_file() or not png.stat().st_size:
                    detail = (result.stderr or result.stdout or f"exit={result.returncode}").strip()
                    raise RuntimeError("Chromium/Playwright 无法完成最小渲染: " + detail[-1800:])
        except (OSError, ValueError, RuntimeError) as exc:
            _atomic_text(
                cache,
                json.dumps(
                    {"status": "failed", "checked_at": time.time(), "error": str(exc)[-1800:]},
                    ensure_ascii=False,
                ) + "\n",
            )
            raise
        _atomic_text(
            cache,
            json.dumps({"status": "ready", "checked_at": time.time()}, ensure_ascii=False) + "\n",
        )
        return "fresh"
    finally:
        if locked:
            try:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
        lock.close()


def _preflight_workspace(root: Path, scope: str = "all") -> dict:
    """Validate deployment capabilities and/or one staged Deck workspace."""
    if scope not in {"all", "environment", "workspace"}:
        raise ValueError(f"unsupported preflight scope: {scope}")
    root = root.resolve()
    errors: list[str] = []
    warnings: list[str] = []
    if not root.is_dir():
        errors.append(f"工作区不存在: {root}")
    elif not os.access(root, os.R_OK | os.W_OK | os.X_OK):
        errors.append(f"工作区不可读写: {root}")
    missing_modules: list[str] = []
    fonts = {"available": 0, "declared": len(FAMILY_FACES), "custom": 0}
    browser = "skipped"
    if scope in {"all", "environment"}:
        if sys.version_info < (3, 10):
            errors.append(f"需要 Python 3.10+，当前为 {sys.version.split()[0]}")
        skill_root = Path(__file__).resolve().parent.parent
        for relative in (
            "assets/base-template.css",
            "assets/vendor/echarts.min.js",
            "assets/licenses/OFL-1.1.txt",
            "scripts/render.py",
            "scripts/stage_materials.py",
        ):
            path = skill_root / relative
            if not path.is_file() or not path.stat().st_size:
                errors.append(f"Skill 必需文件缺失或为空: {relative}")
        missing_modules = [
            module for module in ("PIL", "fontTools", "brotli")
            if importlib.util.find_spec(module) is None
        ]
        if missing_modules:
            errors.append("缺少必需 Python 模块: " + ", ".join(missing_modules))
        if not missing_modules:
            try:
                fonts = _preflight_fonts(root)
            except (OSError, ValueError, RuntimeError) as exc:
                errors.append(f"字体环境不可用: {exc}")
        browser = "unavailable"
        if not errors:
            try:
                browser = _probe_browser_runtime()
            except (OSError, ValueError, RuntimeError) as exc:
                errors.append(str(exc))
    attachment_count = 0
    if scope in {"all", "workspace"}:
        attachment_count, attachment_errors, attachment_warnings = _preflight_attachment_inventory(root)
        errors.extend(attachment_errors)
        warnings.extend(attachment_warnings)
        if scope == "workspace":
            try:
                custom, _roles = _load_custom_config(root)
                fonts["custom"] = len(custom)
            except (OSError, ValueError, RuntimeError) as exc:
                errors.append(f"工作区字体配置不可用: {exc}")
    if errors:
        raise RuntimeError("环境预检未通过:\n- " + "\n- ".join(errors))
    for warning in warnings:
        print("preflight:WARN " + warning)
    summary = {
        "scope": scope,
        "python": sys.version.split()[0],
        "browser": browser,
        "fonts": fonts,
        "attachments": attachment_count,
        "warnings": len(warnings),
    }
    print("preflight:PASS " + json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return summary


ASSET_ORIGINS = {"downloaded", "generated", "material", "derived"}

_CANVAS_RESET_MARKER = "/* deck-runtime-canvas-reset */"
_THEME_OVERRIDE_MARKER = "/* deck-theme-overrides */"
_CANVAS_RESET = """/* deck-runtime-canvas-reset */
*, *::before, *::after { box-sizing: border-box; }
html, body { margin: 0; padding: 0; width: 100%; height: 100%; }
body { overflow: hidden; }
"""

_ECHARTS_LOCAL_SRC = "../assets/vendor/echarts.min.js"
_ECHARTS_SCRIPT_RE = re.compile(
    r"(<script\b[^>]*\bsrc=[\"'])([^\"']*echarts[^\"']*)([\"'][^>]*>\s*</script>)",
    re.I,
)
_LOCAL_ATTR_RE = re.compile(
    r"<(?:script|link|img|source|video|audio)\b[^>]*\b(?:src|href)=[\"']([^\"']+)[\"']",
    re.I,
)
_CSS_URL_RE = re.compile(r"url\(\s*['\"]?([^)'\"]+)['\"]?\s*\)", re.I)


def _ensure_runtime_assets(root: Path) -> None:
    """Materialize browser runtime libraries inside the portable Deck."""
    source = Path(__file__).resolve().parent.parent / "assets/vendor/echarts.min.js"
    if not source.is_file() or not source.stat().st_size:
        raise ValueError(f"Skill runtime asset is missing: {source}")
    target = root / "assets/vendor/echarts.min.js"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.is_file() or target.read_bytes() != source.read_bytes():
        shutil.copy2(source, target)


def _materialize_base_css(root: Path) -> None:
    """Build the generated deck stylesheet from the stable asset plus small overrides.

    The Orchestrator writes ``plan/theme.css`` only.  It never needs to read or
    reproduce the long template in a model response.  Re-running ``prepare`` is
    deterministic and also removes stale font-bundle blocks before fonts are
    bundled again later in the command.
    """
    template = Path(__file__).resolve().parent.parent / "assets/base-template.css"
    if not template.is_file():
        raise ValueError(f"base.css template is missing: {template}")
    theme = root / "plan/theme.css"
    overrides = theme.read_text(encoding="utf-8", errors="ignore").strip() if theme.is_file() else ""
    payload = _CANVAS_RESET + "\n" + template.read_text(
        encoding="utf-8", errors="ignore"
    ).lstrip()
    if overrides:
        payload = payload.rstrip() + "\n\n" + _THEME_OVERRIDE_MARKER + "\n" + overrides + "\n"
    path = root / "base.css"
    if not path.is_file() or path.read_text(encoding="utf-8", errors="ignore") != payload:
        _atomic_text(path, payload)


def _ensure_canvas_reset(root: Path) -> None:
    """Keep the browser viewport and the 1600×900 canvas edge-aligned.

    ``base.css`` is materialized during prepare.  The browser's default 8px body
    margin is runtime plumbing rather than art direction, so build still adds
    this tiny idempotent reset if an edited legacy stylesheet omitted it.
    """
    path = root / "base.css"
    if not path.is_file():
        template = Path(__file__).resolve().parent.parent / "assets/base-template.css"
        if not template.is_file():
            raise ValueError(f"base.css and its template are missing: {template}")
        shutil.copy2(template, path)
    text = path.read_text(encoding="utf-8", errors="ignore")
    if _CANVAS_RESET_MARKER not in text:
        _atomic_text(path, _CANVAS_RESET + "\n" + text.lstrip())


def _normalize_runtime_references(root: Path) -> None:
    """Rewrite every ECharts reference to the Deck-local portable copy."""
    for slide in (root / "slides").glob("slide_*.html"):
        text = slide.read_text(encoding="utf-8", errors="ignore")
        normalized = _ECHARTS_SCRIPT_RE.sub(
            lambda match: match.group(1) + _ECHARTS_LOCAL_SRC + match.group(3), text
        )
        if normalized != text:
            _atomic_text(slide, normalized)


def _is_external_reference(value: str) -> bool:
    value = value.strip().lower()
    return value.startswith(("http://", "https://", "//", "data:", "blob:", "#", "javascript:"))


def _resolve_local_reference(root: Path, owner: Path, value: str) -> tuple[Path | None, str | None]:
    clean = value.strip().split("#", 1)[0].split("?", 1)[0]
    if not clean or _is_external_reference(clean):
        return None, None
    candidate = (owner.parent / clean).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return candidate, "escapes the portable Deck"
    if not candidate.is_file() or not candidate.stat().st_size:
        return candidate, "is missing or empty"
    return candidate, None


def _validate_runtime_dependencies(root: Path, expected: int | None = None) -> None:
    """Reject non-portable local CSS/JS/media references before delivery."""
    slides = sorted(
        path for path in (root / "slides").glob("slide_*.html")
        if ".bak." not in path.name
    )
    if expected is not None and len(slides) != expected:
        raise ValueError(f"expected {expected} slides, found {len(slides)}")
    errors = []
    owners = list(slides)
    base_css = root / "base.css"
    if base_css.is_file():
        owners.append(base_css)
    for owner in owners:
        text = owner.read_text(encoding="utf-8", errors="ignore")
        refs = _CSS_URL_RE.findall(text) if owner.suffix.lower() == ".css" else (
            _LOCAL_ATTR_RE.findall(text) + _CSS_URL_RE.findall(text)
        )
        for reference in refs:
            resolved, error = _resolve_local_reference(root, owner, reference)
            if error:
                shown = resolved.relative_to(root).as_posix() if resolved and root.resolve() in resolved.parents else str(resolved)
                errors.append(f"{owner.relative_to(root).as_posix()}: {reference} -> {shown} {error}")
    present = root / "present.html"
    if present.is_file():
        player = present.read_text(encoding="utf-8", errors="ignore")
        missing_in_player = [
            slide.relative_to(root).as_posix() for slide in slides
            if slide.relative_to(root).as_posix() not in player
        ]
        if missing_in_player:
            errors.append("present.html does not route: " + ", ".join(missing_in_player))
    if errors:
        raise ValueError("portable runtime dependency audit failed:\n" + "\n".join(errors))


def _validate_player_runtime(root: Path) -> None:
    render = Path(__file__).resolve().parent / "render.py"
    proc = subprocess.run(
        [sys.executable, str(render), "--audit-player", str(root)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=_optional_timeout("PLAYER_RUNTIME_AUDIT_TIMEOUT"),
        check=False,
    )
    if proc.returncode:
        detail = (proc.stderr or proc.stdout or f"exit={proc.returncode}").strip()
        raise ValueError(detail[-1600:])
    print((proc.stdout or "player-runtime-audit:PASS").strip())


def _asset_catalog(root: Path) -> tuple[Path, dict]:
    path = root / "assets/catalog.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError, TypeError):
        payload = {}
    entries = payload.get("assets") if isinstance(payload, dict) else None
    return path, {"schema_version": 2, "assets": entries if isinstance(entries, list) else []}


@contextmanager
def _asset_catalog_lock(root: Path):
    """Serialize short read-modify-write catalog updates across Image groups."""
    assets = root / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    with (assets / ".catalog.lock").open("a+", encoding="utf-8") as stream:
        try:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        try:
            yield
        finally:
            try:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass


def _write_asset_catalog(path: Path, catalog: dict) -> None:
    catalog["schema_version"] = 2
    catalog["assets"] = sorted(
        catalog.get("assets", []), key=lambda item: str(item.get("path") or "")
    )
    _atomic_text(path, json.dumps(catalog, ensure_ascii=False, indent=2) + "\n")


def _register_asset(
    root: Path,
    relative: str,
    origin: str,
    *,
    source_url: str | None = None,
    source_path: str | None = None,
    generator_model: str | None = None,
    prompt: str | None = None,
    parent_asset: str | None = None,
    material_asset_type: str | None = None,
) -> None:
    relative = Path(relative).as_posix()
    if origin not in ASSET_ORIGINS:
        raise ValueError(f"unsupported asset origin: {origin}")
    if Path(relative).is_absolute() or not relative.startswith("assets/"):
        raise ValueError("asset path must be workspace-relative under assets/")
    asset = (root / relative).resolve()
    if asset.parent != (root / "assets").resolve():
        raise ValueError(f"asset must live directly under assets/: {relative}")
    if origin == "material" and not source_path:
        raise ValueError("material assets require --source-path")
    if origin == "material":
        source_posix = Path(source_path or "").as_posix()
        page_visual = bool(re.search(r"(?:_pages|pdf_pages)/p\d+\.png$", source_posix, re.I))
        if page_visual and material_asset_type != "page-facsimile":
            raise ValueError(
                "PDF page renders are context-only. Use material-figure for a named Figure, "
                "or explicitly pass --material-asset-type page-facsimile when the paper page itself is evidence."
            )
        if material_asset_type is None:
            material_asset_type = "attachment-image"
    if not asset.is_file() and origin == "material" and source_path:
        source_value = Path(source_path)
        source = (root / source_value).resolve()
        if source_value.is_absolute() or root.resolve() not in source.parents or not source.is_file():
            raise ValueError(f"material source is not a workspace file: {source_path}")
        asset.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, asset)
    if not asset.is_file():
        raise ValueError(f"asset does not exist directly under assets/: {relative}")
    if origin == "downloaded" and not source_url:
        raise ValueError("downloaded assets require --source-url")
    if origin == "generated" and not generator_model:
        raise ValueError("generated assets require --generator-model")
    if origin == "derived" and not parent_asset:
        raise ValueError("derived assets require --parent-asset")
    with _asset_catalog_lock(root):
        path, catalog = _asset_catalog(root)
        entry = {
            "path": relative,
            "origin": origin,
            "source_url": source_url,
            "source_path": source_path,
            "generator_model": generator_model,
            "prompt": prompt,
            "parent_asset": parent_asset,
            "material_asset_type": material_asset_type,
            "status": "unassigned",
        }
        entries = [item for item in catalog["assets"]
                   if not isinstance(item, dict) or item.get("path") != relative]
        entries.append(entry)
        catalog["assets"] = entries
        _write_asset_catalog(path, catalog)
    print(f"asset:PASS {relative} origin={origin}")


def _download_assets(root: Path, urls: list[str]) -> None:
    """Download verified raster images and register source provenance.

    This is a deterministic Skill command, not a model-visible custom tool.
    Repeated ``--url`` options let Image localize a candidate batch in one
    terminal call without exposing a custom download function to the model.
    """
    from io import BytesIO
    from PIL import Image

    if not urls:
        raise ValueError("asset-download requires at least one --url")
    assets = root / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    timeout = _optional_timeout("ASSET_DOWNLOAD_TIMEOUT")
    downloaded = []
    for url in urls:
        parsed = urlparse(str(url))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"asset-download only accepts http(s) URLs: {url}")
        request = Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
                ),
                "Referer": f"{parsed.scheme}://{parsed.netloc}/",
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            },
        )
        open_options = {"timeout": timeout} if timeout is not None else {}
        with urlopen(request, **open_options) as response:
            data = response.read(32 * 1024 * 1024 + 1)
        if not data or len(data) > 32 * 1024 * 1024:
            raise ValueError(f"asset-download returned an empty or oversized file: {url}")
        try:
            with Image.open(BytesIO(data)) as image:
                image.verify()
                image_format = str(image.format or "").lower()
        except Exception as exc:
            raise ValueError(
                f"asset-download response is not a valid raster image: {url}"
            ) from exc
        extension = {
            "jpeg": ".jpg", "png": ".png", "webp": ".webp", "gif": ".gif",
        }.get(image_format)
        if not extension:
            raise ValueError(
                f"unsupported downloaded image format {image_format!r}: {url}"
            )
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
        relative = f"assets/web_{digest}{extension}"
        destination = root / relative
        descriptor, temporary = tempfile.mkstemp(
            prefix=destination.name + ".", suffix=".tmp", dir=assets
        )
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        _register_asset(root, relative, "downloaded", source_url=url)
        downloaded.append(relative)
    print("asset-download:PASS " + json.dumps(downloaded, ensure_ascii=False))


def _material_figure_crop(
    root: Path,
    source_relative: str,
    output_relative: str,
    crop_box: str,
    figure_id: str,
    source_page: int | None,
    caption_mode: str,
    padding: int,
) -> None:
    """Crop one named paper figure from a rendered material page and register provenance.

    PDF page rasters are evidence/context, not presentation-ready Figure assets.  This
    command makes the semantic boundary explicit: the page stays under materials/, the
    bounded Figure becomes a derived asset under assets/, and the catalog records the
    exact crop and source page.
    """
    from PIL import Image

    source_value = Path(source_relative)
    output_value = Path(output_relative)
    if source_value.is_absolute() or output_value.is_absolute():
        raise ValueError("source and output paths must be workspace-relative")
    if not source_value.as_posix().startswith("materials/"):
        raise ValueError("material figure source must be under materials/")
    if not output_value.as_posix().startswith("assets/") or output_value.parent.as_posix() != "assets":
        raise ValueError("material figure output must live directly under assets/")
    source = (root / source_value).resolve()
    output = (root / output_value).resolve()
    if root.resolve() not in source.parents or not source.is_file():
        raise ValueError(f"material page image does not exist: {source_relative}")
    try:
        values = [float(item.strip()) for item in crop_box.split(",")]
    except ValueError as exc:
        raise ValueError("--box must be x0,y0,x1,y1 using normalized 0..1 coordinates") from exc
    if len(values) != 4 or not all(0.0 <= item <= 1.0 for item in values):
        raise ValueError("--box must contain four normalized values in the 0..1 range")
    x0, y0, x1, y1 = values
    if x1 <= x0 or y1 <= y0:
        raise ValueError("--box requires x1>x0 and y1>y0")
    padding = max(0, int(padding))
    figure_id = figure_id.strip()
    if not figure_id:
        raise ValueError("--figure-id must be non-empty")

    with Image.open(source) as page_image:
        page_image.load()
        width, height = page_image.size
        left = max(0, int(round(x0 * width)) - padding)
        top = max(0, int(round(y0 * height)) - padding)
        right = min(width, int(round(x1 * width)) + padding)
        bottom = min(height, int(round(y1 * height)) + padding)
        crop_width, crop_height = right - left, bottom - top
        if crop_width < 64 or crop_height < 64:
            raise ValueError("figure crop is too small; inspect the page and provide a valid bounding box")
        page_fraction = (crop_width * crop_height) / float(width * height)
        if page_fraction >= 0.92:
            raise ValueError(
                "figure crop still covers almost the whole paper page; provide a tighter Figure box. "
                "Use a separately declared page facsimile only when the page itself is the intended evidence."
            )
        cropped = page_image.crop((left, top, right, bottom)).convert("RGBA")

    output.parent.mkdir(parents=True, exist_ok=True)
    _save_image_atomic(output, cropped)
    with _asset_catalog_lock(root):
        path, catalog = _asset_catalog(root)
        entry = {
            "path": output_value.as_posix(),
            "origin": "derived",
            "source_path": source_value.as_posix(),
            "parent_asset": source_value.as_posix(),
            "derivative_kind": "material_figure_crop",
            "material_asset_type": "figure_crop",
            "figure_id": figure_id,
            "source_page": source_page,
            "crop_box_normalized": [x0, y0, x1, y1],
            "crop_box_pixels": [left, top, right, bottom],
            "page_fraction": round(page_fraction, 6),
            "caption_mode": caption_mode,
            "status": "unassigned",
        }
        catalog["assets"] = [
            item for item in catalog["assets"]
            if not isinstance(item, dict) or item.get("path") != output_value.as_posix()
        ] + [entry]
        _write_asset_catalog(path, catalog)
    print(json.dumps({
        "status": "ok",
        "path": output_value.as_posix(),
        "figure_id": figure_id,
        "source": source_value.as_posix(),
        "source_page": source_page,
        "size": [crop_width, crop_height],
        "page_fraction": round(page_fraction, 6),
    }, ensure_ascii=False))


def _asset_entry(root: Path, relative: str) -> tuple[Path, dict, dict]:
    relative = Path(relative).as_posix()
    if Path(relative).is_absolute() or not relative.startswith("assets/"):
        raise ValueError("asset path must be workspace-relative under assets/")
    if not (root / relative).is_file():
        raise ValueError(f"asset does not exist: {relative}")
    path, catalog = _asset_catalog(root)
    entry = next(
        (item for item in catalog["assets"]
         if isinstance(item, dict) and item.get("path") == relative),
        None,
    )
    if entry is None:
        raise ValueError(
            f"asset lacks provenance: {relative}; use image tools or asset-register first"
        )
    return path, catalog, entry


def _assign_asset(root: Path, relative: str, asset_id: str, group_id: str) -> None:
    asset_id = asset_id.strip()
    group_id = group_id.strip()
    if not asset_id or not group_id:
        raise ValueError("asset-id and group-id must be non-empty")
    with _asset_catalog_lock(root):
        path, catalog, selected = _asset_entry(root, relative)
        for entry in catalog["assets"]:
            if not isinstance(entry, dict) or entry is selected:
                continue
            if entry.get("asset_id") == asset_id and entry.get("status") != "rejected":
                entry["status"] = "rejected"
                entry["review_note"] = f"superseded by {relative}"
        selected["asset_id"] = asset_id
        selected["group_id"] = group_id
        selected["status"] = "candidate"
        selected.pop("review_note", None)
        _write_asset_catalog(path, catalog)
    print(f"asset-assign:PASS {asset_id} -> {relative} group={group_id}")


def _safe_group_name(group_id: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", group_id.strip()).strip("-.")
    if not value:
        raise ValueError("group-id must contain a usable character")
    return value[:80]


def _make_asset_sheet(root: Path, entries: list[dict], title: str):
    from PIL import Image, ImageDraw, ImageOps

    width, thumb_height, label_height, gap, header = 420, 270, 64, 20, 66
    columns = min(3, len(entries))
    rows = math.ceil(len(entries) / columns)
    sheet = Image.new(
        "RGB",
        (gap + columns * (width + gap), header + rows * (thumb_height + label_height + gap)),
        "#17191D",
    )
    draw = ImageDraw.Draw(sheet)
    draw.text((gap, 16), title, fill="#F2F3F5", font=_sheet_font(26))
    for index, entry in enumerate(entries):
        row, column = divmod(index, columns)
        x = gap + column * (width + gap)
        y = header + row * (thumb_height + label_height + gap)
        source_path = root / str(entry["path"])
        with Image.open(source_path) as source:
            source.load()
            source_size = source.size
            frame = ImageOps.contain(
                source.convert("RGB"), (width, thumb_height), Image.Resampling.LANCZOS
            )
        sheet.paste(
            frame,
            (x + (width - frame.width) // 2, y + (thumb_height - frame.height) // 2),
        )
        draw.rectangle((x, y, x + width - 1, y + thumb_height - 1), outline="#59616E", width=2)
        draw.text(
            (x + 6, y + thumb_height + 5),
            str(entry["asset_id"]),
            fill="#FFFFFF",
            font=_sheet_font(20),
        )
        draw.text(
            (x + 6, y + thumb_height + 34),
            f"{source_size[0]}x{source_size[1]} · {entry.get('status', 'candidate')}",
            fill="#AEB5C0",
            font=_sheet_font(15),
        )
    return sheet


def _build_asset_contact(root: Path, group_id: str) -> None:
    safe_group = _safe_group_name(group_id)
    _, catalog = _asset_catalog(root)
    entries = [
        item for item in catalog["assets"]
        if isinstance(item, dict)
        and item.get("group_id") == group_id
        and item.get("asset_id")
        and item.get("status") in {"candidate", "needs_review", "ready"}
    ]
    if not entries:
        raise ValueError(f"no assigned candidates for group: {group_id}")
    ids = [str(item["asset_id"]) for item in entries]
    if len(ids) != len(set(ids)):
        raise ValueError(f"group contains duplicate asset_id values: {group_id}")
    missing = [str(item["path"]) for item in entries if not (root / str(item["path"])).is_file()]
    if missing:
        raise ValueError("missing candidate files: " + ", ".join(missing))
    entries.sort(key=lambda item: str(item["asset_id"]))
    sheet_path = root / "assets" / f"contact-sheet-{safe_group}.png"
    _save_image_atomic(
        sheet_path,
        _make_asset_sheet(root, entries, f"ASSET GROUP · {group_id} · {len(entries)} ITEMS"),
    )
    manifest = {
        "schema_version": 1,
        "group_id": group_id,
        "contact_sheet": sheet_path.relative_to(root).as_posix(),
        "assets": [
            {
                "asset_id": item["asset_id"],
                "path": item["path"],
                "status": item["status"],
            }
            for item in entries
        ],
    }
    manifest_path = root / "assets" / f"contact-sheet-{safe_group}.json"
    _atomic_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(manifest, ensure_ascii=False))


def _asset_ids(raw: str | None) -> list[str]:
    if not raw:
        return []
    values = [item.strip() for item in raw.split(",") if item.strip()]
    if len(values) != len(set(values)):
        raise ValueError("asset id lists must not contain duplicates")
    return values


def _review_assets(
    root: Path,
    group_id: str,
    *,
    ready: str | None,
    needs_review: str | None,
    rejected: str | None,
) -> None:
    requested = {
        "ready": _asset_ids(ready),
        "needs_review": _asset_ids(needs_review),
        "rejected": _asset_ids(rejected),
    }
    flattened = [asset_id for values in requested.values() for asset_id in values]
    if not flattened:
        raise ValueError("provide at least one of --ready, --needs-review, or --rejected")
    if len(flattened) != len(set(flattened)):
        raise ValueError("the same asset_id cannot receive multiple statuses")
    with _asset_catalog_lock(root):
        path, catalog = _asset_catalog(root)
        by_id = {
            str(item.get("asset_id")): item for item in catalog["assets"]
            if isinstance(item, dict) and item.get("group_id") == group_id and item.get("asset_id")
        }
        missing = sorted(set(flattened) - set(by_id))
        if missing:
            raise ValueError("unknown asset_id for group: " + ", ".join(missing))
        for status, asset_ids in requested.items():
            for asset_id in asset_ids:
                by_id[asset_id]["status"] = status
        _write_asset_catalog(path, catalog)
    print(json.dumps({"group_id": group_id, **requested}, ensure_ascii=False))


def _validate_referenced_assets(root: Path) -> None:
    referenced = set()
    for slide in (root / "slides").glob("slide_*.html"):
        text = slide.read_text(encoding="utf-8", errors="ignore")
        for match in re.finditer(r"(?:\.\./)?assets/[^\s\"'<>?#]+?\.(?:png|jpe?g|webp|gif)", text, re.I):
            referenced.add(match.group(0).removeprefix("../"))
    if not referenced:
        return
    _, catalog = _asset_catalog(root)
    recorded = {
        str(item.get("path")): item for item in catalog["assets"]
        if isinstance(item, dict) and item.get("origin") in ASSET_ORIGINS
    }
    missing = sorted(referenced - set(recorded))
    if missing:
        raise ValueError("referenced raster assets lack provenance: " + ", ".join(missing))
    absent = sorted(path for path in referenced if not (root / path).is_file())
    if absent:
        raise ValueError("referenced raster assets are missing: " + ", ".join(absent))


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_render_quality(root: Path, expected: int | None) -> None:
    """Build/audit consume structured render evidence, never truncated stdout."""
    manifest_path = root / "renders/render.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        pages = manifest.get("pages") or {}
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise ValueError(
            "missing or invalid renders/render.json; run render.py --batch before build"
        ) from exc
    wanted = range(1, int(expected) + 1) if expected else [int(key) for key in pages]
    errors = []
    for number in wanted:
        key = f"{int(number):02d}"
        record = pages.get(key)
        if not isinstance(record, dict):
            errors.append(f"slide_{key}: missing structured render evidence")
            continue
        source = root / str(record.get("source") or f"slides/slide_{key}.html")
        png = root / str(record.get("png") or f"renders/slide_{key}.png")
        if not source.is_file() or not png.is_file():
            errors.append(f"slide_{key}: source or PNG missing")
            continue
        if _sha256_path(source) != record.get("source_sha256"):
            errors.append(f"slide_{key}: HTML changed after structured render")
        if _sha256_path(png) != record.get("png_sha256"):
            errors.append(f"slide_{key}: PNG changed outside canonical renderer")
        # Older renderer snapshots may have persisted ``boxoverflow`` as a
        # hard issue.  It is now an advisory bbox candidate: only current
        # pixel/DOM evidence can turn it into a real repair.  Filtering here
        # keeps historical decks editable without forcing a destructive
        # shrink-to-clear cycle.
        hard = [
            item for item in (record.get("hard_issues") or [])
            if str(item.get("type") or "").lower() != "boxoverflow"
        ]
        if hard:
            kinds = ",".join(str(item.get("type") or "unknown") for item in hard)
            errors.append(f"slide_{key}: unresolved hard render issues [{kinds}]")
    if errors:
        raise ValueError("render quality gate failed:\n" + "\n".join(errors[:40]))
    print(f"render-quality:PASS pages={len(list(wanted))}")


def _sync_speech(root: Path, expected: int | None) -> None:
    files = _plan_files(root, expected)
    texts = [path.read_text(encoding="utf-8") for _, path in files]
    deck_path = root / "plan/deck.md"
    deck_text = deck_path.read_text(encoding="utf-8") if deck_path.exists() else ""
    language_match = re.search(
        r"(?mi)^\s*(?:[-*+]\s*)?(?:language|语言)\s*[:：]\s*(zh|en|中文|英文)\s*$", deck_text
    )
    language = (
        "zh" if language_match and language_match.group(1).lower() in {"zh", "中文"}
        else "en" if language_match
        else "zh" if re.search(r"[\u3400-\u9fff]", "\n".join(texts))
        else "en"
    )
    rows = []
    errors = []
    for (number, path), plan_text in zip(files, texts):
        try:
            title = _plan_title(plan_text)
            parts = _sections(plan_text)
            speech = _first_section(parts, SPEECH_HEADINGS)
            if not speech:
                raise ValueError("missing spoken script section")
            sources = _first_section(parts, SOURCE_HEADINGS)
            if language == "zh":
                rows.extend([f"# 第 {number:02d} 页｜{title}", "", "## 讲述内容", "", speech, "",
                             "## 参考资料（不朗读）", "", sources or "- 本页无外部引用。", ""])
            else:
                rows.extend([f"# Slide {number:02d} | {title}", "", "## Spoken script", "", speech, "",
                             "## Sources (not spoken)", "", sources or "- No external citations.", ""])
        except ValueError as exc:
            errors.append(f"{path.name}: {exc}")
    if errors:
        raise ValueError("\n".join(errors))
    _atomic_text(root / "speech.md", "\n".join(rows).rstrip() + "\n")
    print(f"speech:PASS pages={len(files)} language={language}")


def _prepare_workspace(root: Path, expected: int | None) -> None:
    """Freeze plan-derived speech and portable fonts before page production."""
    _materialize_base_css(root)
    _ensure_canvas_reset(root)
    _ensure_runtime_assets(root)
    _validate_no_pictographs(root, expected, include_html=False)
    _validate_image_presentations(root, expected)
    _sync_speech(root, expected)
    manifest = bundle_workspace(root, from_plans=True)
    print(
        f"prepare:PASS portable_font_faces={len(manifest.get('faces', []))} "
        "runtime=assets/vendor/echarts.min.js"
    )


def _sheet_font(size: int):
    from PIL import ImageFont

    for name in ("DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _parse_pages(raw: str) -> list[int]:
    pages = set()
    for part in re.split(r"[\s,]+", raw.strip()):
        if not part:
            continue
        if "-" in part:
            left, right = part.split("-", 1)
            start, end = int(left), int(right)
            if start > end:
                raise ValueError(f"invalid descending range: {part}")
            pages.update(range(start, end + 1))
        else:
            pages.add(int(part))
    if not pages or min(pages) < 1:
        raise ValueError("page list requires positive page numbers")
    return sorted(pages)


def _available_render_pages(render_dir: Path) -> list[int]:
    pages = []
    for path in render_dir.iterdir() if render_dir.is_dir() else ():
        match = PAGE_RE.match(path.name)
        if match:
            pages.append(int(match.group(1)))
    return sorted(set(pages))


def _make_sheet(render_dir: Path, pages: list[int], columns: int, width: int, title: str):
    from PIL import Image, ImageDraw, ImageOps

    thumb_height = round(width * 9 / 16)
    label_height = max(28, width // 12)
    gap = max(14, width // 24)
    header = 54
    rows = math.ceil(len(pages) / columns)
    sheet = Image.new("RGB", (gap + columns * (width + gap), header + rows * (thumb_height + label_height + gap)), "#17191D")
    draw = ImageDraw.Draw(sheet)
    draw.text((gap, 14), title, fill="#F2F3F5", font=_sheet_font(24))
    for index, page in enumerate(pages):
        row, column = divmod(index, columns)
        x = gap + column * (width + gap)
        y = header + row * (thumb_height + label_height + gap)
        with Image.open(render_dir / f"slide_{page:02d}.png") as source:
            frame = ImageOps.contain(source.convert("RGB"), (width, thumb_height), Image.Resampling.LANCZOS)
        sheet.paste(frame, (x + (width - frame.width) // 2, y + (thumb_height - frame.height) // 2))
        draw.rectangle((x, y, x + width - 1, y + thumb_height - 1), outline="#555B66", width=2)
        draw.text((x + 4, y + thumb_height + 5), f"SLIDE {page:02d}", fill="#F2F3F5", font=_sheet_font(max(16, label_height - 12)))
    return sheet


def _save_image_atomic(path: Path, image):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".png", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        image.save(temporary, format="PNG", compress_level=1)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _contact_snapshot(render_dir: Path, pages: list[int]):
    evidence = []
    for page in pages:
        path = render_dir / f"slide_{page:02d}.png"
        data = path.read_bytes()
        evidence.append({
            "page": page,
            "path": path.relative_to(render_dir.parent).as_posix(),
            "sha256": hashlib.sha256(data).hexdigest(),
            "mtime_ns": path.stat().st_mtime_ns,
        })
    return evidence


def _contact_label(label: str | None, pages: list[int]):
    raw = label or "pages-" + "-".join(f"{page:02d}" for page in pages)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", raw.strip()).strip("-._")
    return (safe or "focus")[:80]


def _build_contact(
    root: Path,
    expected: int | None = None,
    focus: str | None = None,
    label: str | None = None,
):
    render_dir = root / "renders"
    available = _available_render_pages(render_dir)
    if not available:
        raise ValueError(f"no slide PNGs found in {render_dir}")
    pages = _parse_pages(focus) if focus else list(range(1, (expected or max(available)) + 1))
    missing = [page for page in pages if page not in available]
    if missing:
        raise ValueError("missing rendered pages: " + ",".join(f"{page:02d}" for page in missing))
    if focus:
        key = _contact_label(label, pages)
        path = render_dir / f"contact-sheet-focus-{key}.png"
        _save_image_atomic(path, _make_sheet(render_dir, pages, min(3, len(pages)), 500,
                                             "REVIEW FOCUS · " + ", ".join(f"{page:02d}" for page in pages)))
        payload = {
            "mode": "focus",
            "label": key,
            "pages": pages,
            "focus": path.relative_to(root).as_posix(),
            "evidence": _contact_snapshot(render_dir, pages),
        }
        sidecar = path.with_suffix(".json")
        _atomic_text(sidecar, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    else:
        manifest_path = render_dir / "review-contact.json"
        try:
            audit = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
        except (OSError, ValueError):
            audit = {}
        for stale in render_dir.glob("contact-sheet-review-*.png"):
            stale.unlink()
        overview_path = render_dir / "contact-sheet.png"
        _save_image_atomic(overview_path, _make_sheet(render_dir, pages, 6, 260,
                                                       f"DECK OVERVIEW · {len(pages)} PAGES"))
        # 长 Deck 不把过多页缩进同一张联系表。Review 可按 manifest 顺序分批
        # 查看全部 sheet；因此这里保持每张最多 8 页，不封顶联系表数量。
        group_count = max(1, math.ceil(len(pages) / 8))
        group_size = math.ceil(len(pages) / group_count)
        evidence = _contact_snapshot(render_dir, pages)
        evidence_by_page = {int(item["page"]): item for item in evidence}
        groups = []
        for index, start in enumerate(range(0, len(pages), group_size), 1):
            group = pages[start:start + group_size]
            path = render_dir / f"contact-sheet-review-{index:02d}.png"
            _save_image_atomic(path, _make_sheet(render_dir, group, min(4, len(group)), 400,
                                                   f"REVIEW GROUP {index:02d} · {group[0]:02d}–{group[-1]:02d}"))
            groups.append({
                "path": path.relative_to(root).as_posix(),
                "pages": group,
                "evidence": [evidence_by_page[page] for page in group],
            })
        payload = {"mode": "full", "page_count": len(pages), "pages": pages,
                   "overview": overview_path.relative_to(root).as_posix(),
                   "evidence": evidence, "groups": groups}
        audit["full"] = payload
        _atomic_text(manifest_path, json.dumps(audit, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(payload, ensure_ascii=False))


TPL = r"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Presentation</title>
<style>html,body{margin:0;height:100%;background:#000;overflow:hidden;font-family:system-ui,sans-serif}
#stage{position:fixed;inset:0;display:flex;align-items:center;justify-content:center}
#wrap{position:relative;flex:0 0 auto;width:__CW__px;height:__CH__px;transform-origin:center;background:#000;box-shadow:0 0 40px rgba(0,0,0,.6)}
#wrap iframe{position:absolute;inset:0;width:100%;height:100%;border:0;background:transparent;
opacity:0;transition:opacity .28s ease;pointer-events:none}
#wrap iframe.cur{opacity:1;pointer-events:auto}
#bar{position:fixed;left:0;top:0;height:3px;background:#c9a227;width:0;transition:width .2s}
#hud{position:fixed;left:50%;bottom:14px;transform:translateX(-50%);color:#bbb;background:rgba(0,0,0,.55);
padding:4px 12px;border-radius:20px;font-size:13px;opacity:0;transition:opacity .3s;pointer-events:none}
body.hud #hud{opacity:1}</style></head>
<body><div id="stage"><div id="wrap"></div></div>
<div id="bar"></div><div id="hud"><span id="p"></span> · ←/→ 翻页 · F 全屏 · Home/End 首尾</div>
<script>
const S=__SLIDES__;let i=0;const fr=[];
let resolveFontsReady;const fontsReady=new Promise(resolve=>{resolveFontsReady=resolve;});
let CW=__CW__,CH=__CH__;   /* build 时从 base.css 探测的画布尺寸;运行时再从 .slide 复测兜底 */
const wrap=document.getElementById('wrap'),p=document.getElementById('p'),bar=document.getElementById('bar');
const MOTION_STYLE_ID='mural-present-motion';
const MOTION_CSS=`
:root[data-mural-player="true"] .slide [data-mural-reveal-step]{will-change:opacity,translate}
:root[data-mural-player="true"] .slide:not(.mural-present-active) [data-mural-reveal-step]{
  opacity:0;translate:0 12px
}
:root[data-mural-player="true"] .slide.mural-present-active [data-mural-reveal-step]{
  animation:mural-enter .42s cubic-bezier(.22,1,.36,1) both;
  animation-delay:var(--mural-reveal-delay,60ms)
}
@keyframes mural-enter{
  from{opacity:0;translate:0 12px}
  to{opacity:var(--mural-reveal-opacity,1);translate:none}
}
@media (prefers-reduced-motion:reduce){
  :root[data-mural-player="true"] .slide [data-mural-reveal-step]{
    animation:none!important;transition:none!important;
    opacity:var(--mural-reveal-opacity,1)!important;translate:none!important
  }
}`;
const MOTION_DECOR_RE=/(^|[-_])(bleed|scrim|decor|doodle|blob|ornament|watermark|background|overlay|noise|grain|texture|halo|glow)([-_]|$)/i;
function motionDecor(el){
  if(!el||['STYLE','SCRIPT','LINK','TEMPLATE'].includes(el.tagName))return true;
  if(el.getAttribute('aria-hidden')==='true')return true;
  return [...el.classList].some(name=>MOTION_DECOR_RE.test(name));
}
function motionVisible(d,el){
  if(motionDecor(el)||el.getAttribute('data-reveal')==='none')return false;
  try{const s=d.defaultView.getComputedStyle(el);
    return s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity||1)>0;
  }catch(_){return true;}
}
function directMotionChildren(d,el){return el?[...el.children].filter(child=>motionVisible(d,child)):[];}
function motionTargets(d){
  const slide=d.querySelector('.slide')||d.body;if(!slide)return {slide:null,targets:[]};
  if(slide.dataset.motion==='none'||slide.getAttribute('data-reveal')==='none')return {slide,targets:[]};
  const explicit=[...slide.querySelectorAll('[data-reveal]')].filter(el=>motionVisible(d,el));
  if(explicit.length){
    explicit.sort((a,b)=>{const av=Number(a.dataset.revealOrder),bv=Number(b.dataset.revealOrder);
      const ah=Number.isFinite(av),bh=Number.isFinite(bv);if(ah&&bh&&av!==bv)return av-bv;
      if(ah!==bh)return ah?-1:1;return a.compareDocumentPosition(b)&4?-1:1;});
    return {slide,targets:explicit};
  }
  const roots=[...slide.children],targets=[];
  const add=items=>items.forEach(el=>{if(motionVisible(d,el)&&!targets.includes(el))targets.push(el);});
  const isTitle=el=>el.matches('.slide-title,.page-header,header');
  const isFooter=el=>el.matches('.slide-footer,.page-footer,.special-footer,footer');
  const isBody=el=>el.matches('.slide-body,.page-body,.closing-stage,.special-safe,main,[data-slide-owned="body"]');
  add(roots.filter(isTitle));
  const body=roots.find(isBody);
  if(body){
    const special=body.querySelector('.sec-block,.closing-core,.special-copy');
    const parts=directMotionChildren(d,special||body);
    add(parts.length?parts:[body]);
    add(roots.filter(el=>el!==body&&!isTitle(el)&&!isFooter(el)));
  }else{
    add(roots.filter(el=>!isTitle(el)&&!isFooter(el)));
  }
  add(roots.filter(isFooter));
  return {slide,targets};
}
function prepareMotion(e){try{
  const d=e.contentDocument;if(!d||!d.head)return;
  if(!d.getElementById(MOTION_STYLE_ID)){
    const style=d.createElement('style');style.id=MOTION_STYLE_ID;style.textContent=MOTION_CSS;d.head.appendChild(style);
  }
  const {slide,targets}=motionTargets(d);if(!slide)return;
  targets.forEach((el,index)=>{let opacity='1';try{opacity=d.defaultView.getComputedStyle(el).opacity||'1';}catch(_){}
    el.dataset.muralRevealStep=String(index+1);
    el.style.setProperty('--mural-reveal-opacity',opacity);
    el.style.setProperty('--mural-reveal-delay',(60+index*70)+'ms');});
  d.documentElement.dataset.muralPlayer='true';e.dataset.motionTargets=String(targets.length);
}catch(_){}}
function activateMotion(e,active){try{
  const d=e.contentDocument,slide=d&&(d.querySelector('.slide')||d.body);if(!slide)return;
  if(!active){slide.classList.remove('mural-present-active');return;}
  slide.classList.remove('mural-present-active');void slide.offsetWidth;slide.classList.add('mural-present-active');
}catch(_){}}
const fit=()=>{wrap.style.width=CW+'px';wrap.style.height=CH+'px';
wrap.style.transform='scale('+Math.min(innerWidth/CW,innerHeight/CH)+')';};
/* 从已加载的 iframe 里读 .slide 真实像素尺寸,校正画布(处理 base.css 未用 --canvas-* 或 build 探测失准的情况) */
/* 用 offsetWidth/Height(.slide 的 border-box = 画布真实尺寸),不用 scrollWidth/Height:后者会把画到
   画布外、被 .slide overflow:hidden 视觉裁掉的装饰(halftone 圆/斜切色块等,常伸出画布)也算进去,
   于是画布被撑成 1710/1950 之类 → 整册被多缩、右侧留黑边;且各页溢出量不同 + CW/CH 全局共享,
   相邻页懒加载先后会互相污染当前页缩放(偶发)。offset 只量 .slide 盒本身,横竖版都对。 */
function remeasure(e){try{const d=e.contentDocument;if(!d)return;
const el=d.querySelector('.slide')||d.body;if(!el)return;
const w=Math.round(el.offsetWidth||el.scrollWidth),h=Math.round(el.offsetHeight||el.scrollHeight);
if(w>50&&h>50&&(Math.abs(w-CW)>1||Math.abs(h-CH)>1)){CW=w;CH=h;fit();}}catch(err){}}
function ensure(n){if(n<0||n>=S.length)return null;if(fr[n])return fr[n];
const e=document.createElement('iframe');e.dataset.ok='0';e.dataset.slide=String(n+1);e.title='slide '+(n+1);
e.addEventListener('load',async()=>{try{const d=e.contentDocument;if(d&&d.fonts)await d.fonts.ready;}catch(_){}
e.dataset.ok='1';remeasure(e);prepareMotion(e);
try{e.contentDocument.addEventListener('keydown',onKey);}catch(_){}   /* 焦点进 iframe(用户一点幻灯片)也能 ←/→ 翻页:同源,给页内文档挂同一监听 */
if(n===i){reveal();resolveFontsReady();}});
e.src=S[n];wrap.appendChild(e);fr[n]=e;return e;}
let motionRun=0;
function reveal(){const run=++motionRun;fr.forEach((e,k)=>{if(!e)return;const active=k===i;
  if(active)activateMotion(e,true);
  e.classList.toggle('cur',active);e.classList.toggle('active',active);
  if(!active)setTimeout(()=>{if(run===motionRun&&!e.classList.contains('cur'))activateMotion(e,false);},340);
});}
function show(n,push){i=Math.max(0,Math.min(S.length-1,n));
const e=ensure(i);ensure(i-1);ensure(i+1);
if(e.dataset.ok==='1')reveal();   /* 已加载→立即交叉淡入;未加载→其 load 事件再 reveal,旧页保持可见,无白闪 */
p.textContent=(i+1)+' / '+S.length;bar.style.width=((i+1)/S.length*100)+'%';
if(push!==false)location.hash='#'+(i+1);
dispatchEvent(new CustomEvent('slidechange',{detail:{slide:i+1}}));
document.body.classList.add('hud');clearTimeout(show.t);show.t=setTimeout(()=>document.body.classList.remove('hud'),1500);}
const next=()=>show(i+1),prev=()=>show(i-1);
function onKey(e){const k=e.key;
if(['ArrowRight','PageDown',' ','Enter'].includes(k)){next();e.preventDefault();}
else if(['ArrowLeft','PageUp','Backspace'].includes(k)){prev();e.preventDefault();}
else if(k==='Home')show(0);else if(k==='End')show(S.length-1);
else if(k==='f'||k==='F'){document.fullscreenElement?document.exitFullscreen():document.documentElement.requestFullscreen();}}
addEventListener('keydown',onKey);   /* 外层窗口 + 每个 iframe contentDocument(在 ensure 的 load 里挂)都监听→焦点在哪都能翻页 */
addEventListener('resize',fit);
let x=null;addEventListener('touchstart',e=>x=e.touches[0].clientX,{passive:true});
addEventListener('touchend',e=>{if(x==null)return;const d=e.changedTouches[0].clientX-x;if(Math.abs(d)>40)d<0?next():prev();x=null;});
document.getElementById('stage').addEventListener('click',e=>e.clientX>innerWidth/2?next():prev());
window.muralDeck={go:n=>show(Number(n)-1),step:d=>show(i+Number(d||0)),count:S.length,fontsReady};
fit();const s=parseInt((location.hash||'#1').slice(1),10);show(isNaN(s)?0:s-1,false);
</script></body></html>"""


def main(argv=None):
    if argv is None:
        _reexec_configured_runtime()
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in (
        "preflight", "sync", "prepare", "contact", "build", "audit", "asset-register",
        "asset-download", "asset-assign", "asset-contact", "asset-review", "material-figure",
    ):
        command = subparsers.add_parser(name)
        command.add_argument("root", nargs="?", default=".")
        if name == "preflight":
            command.add_argument(
                "--scope", choices=("all", "environment", "workspace"), default="all",
            )
        if name in {"sync", "prepare", "contact", "build", "audit"}:
            command.add_argument("--expected", type=int)
        if name == "contact":
            command.add_argument("--focus", help="comma-separated pages or ranges, e.g. 3,7,12-14")
            command.add_argument("--label", help="stable unique label for a focus contact sheet")
        if name == "asset-register":
            command.add_argument("--path", required=True)
            command.add_argument("--origin", required=True, choices=sorted(ASSET_ORIGINS))
            command.add_argument("--source-url")
            command.add_argument("--source-path")
            command.add_argument("--generator-model")
            command.add_argument("--prompt")
            command.add_argument("--parent-asset")
            command.add_argument("--material-asset-type",
                                 choices=("attachment-image", "page-facsimile"))
        if name == "asset-download":
            command.add_argument(
                "--url", action="append", required=True,
                help="repeat for each image URL; quote URLs containing '&'",
            )
        if name == "asset-assign":
            command.add_argument("--path", required=True)
            command.add_argument("--asset-id", required=True)
            command.add_argument("--group-id", required=True)
        if name == "material-figure":
            command.add_argument("--source", required=True)
            command.add_argument("--path", required=True)
            command.add_argument("--box", required=True,
                                 help="normalized x0,y0,x1,y1 Figure bounds on the page image")
            command.add_argument("--figure-id", required=True)
            command.add_argument("--source-page", type=int)
            command.add_argument("--caption-mode", choices=("excluded", "included", "separate"),
                                 default="excluded")
            command.add_argument("--padding", type=int, default=0)
        if name == "asset-contact":
            command.add_argument("--group-id", required=True)
        if name == "asset-review":
            command.add_argument("--group-id", required=True)
            command.add_argument("--ready")
            command.add_argument("--needs-review")
            command.add_argument("--rejected")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        if args.command == "preflight":
            _preflight_workspace(root, args.scope)
        elif args.command == "sync":
            _sync_speech(root, args.expected)
        elif args.command == "prepare":
            _prepare_workspace(root, args.expected)
        elif args.command == "contact":
            _build_contact(root, args.expected, args.focus, args.label)
        elif args.command == "asset-register":
            _register_asset(
                root, args.path, args.origin, source_url=args.source_url,
                source_path=args.source_path, generator_model=args.generator_model,
                prompt=args.prompt, parent_asset=args.parent_asset,
                material_asset_type=args.material_asset_type,
            )
        elif args.command == "asset-download":
            _download_assets(root, args.url)
        elif args.command == "asset-assign":
            _assign_asset(root, args.path, args.asset_id, args.group_id)
        elif args.command == "material-figure":
            _material_figure_crop(
                root, args.source, args.path, args.box, args.figure_id,
                args.source_page, args.caption_mode, args.padding,
            )
        elif args.command == "asset-contact":
            _build_asset_contact(root, args.group_id)
        elif args.command == "asset-review":
            _review_assets(
                root, args.group_id, ready=args.ready,
                needs_review=args.needs_review, rejected=args.rejected,
            )
        elif args.command == "build":
            _ensure_canvas_reset(root)
            _ensure_runtime_assets(root)
            _normalize_runtime_references(root)
            _validate_no_pictographs(root, args.expected, include_html=True)
            _validate_image_presentations(root, args.expected)
            _sync_speech(root, args.expected)
            _validate_referenced_assets(root)
            _validate_render_quality(root, args.expected)
            if _build_player(root, args.expected):
                return 1
            _validate_runtime_dependencies(root, args.expected)
            _build_contact(root, args.expected)
        else:
            _validate_no_pictographs(root, args.expected, include_html=True)
            _validate_image_presentations(root, args.expected)
            _validate_referenced_assets(root)
            _validate_runtime_dependencies(root, args.expected)
            _validate_render_quality(root, args.expected)
            if not (root / "present.html").is_file():
                raise ValueError("present.html is missing")
            _validate_player_runtime(root)
            print("delivery-audit:PASS")
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"{args.command}:FAIL\n{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
