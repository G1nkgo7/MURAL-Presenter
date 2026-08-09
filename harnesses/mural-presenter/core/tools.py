#!/usr/bin/env python3
"""Mural Presenter Harness 的叶子工具与工具 schema。

工具名、参数和 `parameters` 结构与 Hermes 工具面兼容；描述使用简明中文，
便于中文任务中的模型正确选择工具。core/agent.py 在调 Anthropic Messages API 时
把 `parameters` 适配为 `input_schema`。

工具是**自由函数**,操作一个 `agent` 上下文对象(状态)。循环里调 `dispatch(agent, name, args)`。
子 Agent 的角色由 `delegate_task.tasks[].goal` 的稳定前缀解析。内部角色白名单决定工具，
模型不能选择 toolset、role 或运行时参数。`delegate_task` 需要
递归跑子循环,**实现在 core/agent.py 里**,通过 `agent.extra_tools` 注册;dispatch 先查 extra_tools
再查 BUILTINS。它的 schema(DELEGATE_TASK_SCHEMA)放本文件,和其它 schema 一处。

------------------------------------------------------------------ agent 上下文契约
    agent.ws / agent.safe(rel) / agent.read_path(rel) / agent.writable(rel)
    agent.serper / agent.img_base / agent.img_key / agent.image_model / agent.img_n
    agent.extra_tools
所有 key 从环境变量读(.env 注入,绝不写进仓库)。
"""
import base64
import fcntl
import glob
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import urllib.request

import requests


def parse_optional_timeout(raw_value, default_seconds):
    """Parse an opt-out timeout env value for slow upstream model services.

    ``none``/``off``/``0`` disables the client-side deadline. Progress-based
    repetition guards and role contracts remain independent.
    """
    value = str(default_seconds if raw_value is None else raw_value).strip().lower()
    if value in {"", "0", "none", "off", "disabled", "false"}:
        return None
    seconds = float(value)
    return None if seconds <= 0 else max(30.0, seconds)


IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
TERMINAL_DEFAULT_TIMEOUT = parse_optional_timeout(
    os.environ.get("TERMINAL_DEFAULT_FOREGROUND_TIMEOUT"), None
)
FOREGROUND_MAX_TIMEOUT = parse_optional_timeout(
    os.environ.get("TERMINAL_MAX_FOREGROUND_TIMEOUT"), None
)
IMAGE_GENERATION_CONCURRENCY = max(
    1, int(os.environ.get("IMAGE_GENERATION_CONCURRENCY", "4"))
)
IMAGE_GENERATION_TIMEOUT = parse_optional_timeout(
    os.environ.get("IMAGE_GENERATION_TIMEOUT"), None
)
WEB_REQUEST_TIMEOUT = parse_optional_timeout(
    os.environ.get("WEB_REQUEST_TIMEOUT"), None
)
IMAGE_DOWNLOAD_TIMEOUT = parse_optional_timeout(
    os.environ.get("IMAGE_DOWNLOAD_TIMEOUT"), None
)
ROLE_CARD_READ_CAP = max(
    8000, int(os.environ.get("ROLE_CARD_READ_CAP", "28000"))
)
_IMAGE_GENERATION_SEMAPHORE = threading.BoundedSemaphore(
    IMAGE_GENERATION_CONCURRENCY
)


def _record_asset_provenance(agent, rel, *, origin, source_url=None,
                             generator_model=None, prompt=None,
                             aspect_ratio=None, parent_asset=None):
    """Persist technical image lineage for delivery/UI without model inference.

    Image Agents may run concurrently, so the catalog is updated under a small
    workspace-local file lock and atomically replaced.  Page semantics remain in
    the Skill plan; this record only captures how the raster asset came to exist.
    """
    assets = agent.safe("assets")
    os.makedirs(assets, exist_ok=True)
    catalog_path = os.path.join(assets, "catalog.json")
    lock_path = os.path.join(assets, ".catalog.lock")
    with open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            try:
                with open(catalog_path, encoding="utf-8") as source:
                    catalog = json.load(source)
            except (OSError, ValueError, TypeError):
                catalog = {"schema_version": 2, "assets": []}
            entries = catalog.get("assets")
            if not isinstance(entries, list):
                entries = []
            previous = next(
                (
                    item for item in entries
                    if isinstance(item, dict)
                    and item.get("path") == rel.replace(os.sep, "/")
                ),
                {},
            )
            entry = {
                "path": rel.replace(os.sep, "/"),
                "origin": origin,
                "source_url": source_url,
                "generator_model": generator_model,
                "prompt": prompt,
                "aspect_ratio": aspect_ratio,
                "parent_asset": parent_asset,
                "created_at": int(time.time()),
                # Image tools only create candidates.  A semantic asset_id and
                # final ready status are assigned after the group contact-sheet
                # review by the Skill's deterministic deck.py commands.
                "status": "unassigned",
            }
            # A tool retry may rewrite the same path after the Skill has bound
            # it to a semantic asset.  Keep that review state instead of
            # silently turning a ready item back into an unnamed candidate.
            for key in ("asset_id", "group_id", "status", "review_note"):
                if previous.get(key):
                    entry[key] = previous[key]
            entries = [item for item in entries
                       if not isinstance(item, dict) or item.get("path") != entry["path"]]
            entries.append(entry)
            catalog = {"schema_version": 2, "assets": sorted(
                entries, key=lambda item: str(item.get("path") or "")
            )}
            temporary = catalog_path + f".{os.getpid()}.tmp"
            with open(temporary, "w", encoding="utf-8") as output:
                json.dump(catalog, output, ensure_ascii=False, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, catalog_path)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    # Keep a worker-local record as well.  The shared catalog may be updated by
    # several Image workers concurrently, so the convergence controller must
    # not mistake another worker's download for progress by this worker.
    lock = getattr(agent, "_asset_activity_lock", None)
    if lock is None:
        lock = threading.Lock()
        agent._asset_activity_lock = lock
    with lock:
        created = getattr(agent, "_created_asset_paths", None)
        if not isinstance(created, set):
            created = set(created or [])
            agent._created_asset_paths = created
        created.add(rel.replace(os.sep, "/"))


# =============================================================== 叶子工具实现
# 模型可见 schema 只声明运行时真实支持的能力；不暴露兼容字段或占位模式。

def _read_key(path):
    value = str(path or "").strip().replace("\\", "/")
    if not value:
        return ""
    return os.path.normpath(value).replace("\\", "/")


_NON_REFERENCE_SKILL_ASSET_RE = re.compile(
    r"(?:^|/)skills/mural-presenter/assets/(?:base-template\.css|vendor/.*)$",
    re.I,
)
_SELECT_ONE_REFERENCE_RE = re.compile(
    r"(?:^|/)skills/mural-presenter/references/"
    r"(?P<family>slide-categories|style-families|style-systems)/(?P<name>[^/]+\.md)$",
    re.I,
)


def _skill_read_policy(agent, normalized, offset):
    """Keep immutable Skill reading small and deterministic.

    The model owns *which* task-specific reference it selects.  The runtime only
    enforces the published contract: implementation assets are not references,
    each routing family contributes one selected document, and a static file
    already read to EOF is not streamed into the active context again.
    """
    if _NON_REFERENCE_SKILL_ASSET_RE.search(normalized):
        return (
            "该文件是确定性运行资产，不是设计 reference，禁止读取。"
            "base-template.css 与 vendor 资源由 deck.py prepare/build 自动处理；"
            "请读取角色卡点名的 reference 或直接推进正式产物。"
        )

    role = str(getattr(agent, "role", "") or "").lower()
    selected = _SELECT_ONE_REFERENCE_RE.search(normalized)
    if role == "orchestrator" and selected:
        choices = getattr(agent, "_selected_reference_families", None)
        if not isinstance(choices, dict):
            choices = {}
            agent._selected_reference_families = choices
        family = selected.group("family").lower()
        previous = choices.get(family)
        if previous and previous != normalized:
            return (
                f"{family} 已选择并读过 {previous}；同一任务只选一份，"
                f"不再混读 {normalized}。请基于已选方向继续规划。"
            )
        choices[family] = normalized

    completed = getattr(agent, "_completed_read_paths", None)
    key = _read_key(normalized)
    if (
        normalized.lower().startswith("skills/mural-presenter/")
        and int(offset or 1) == 1
        and isinstance(completed, set)
        and key in completed
    ):
        return (
            f"{normalized} 已完整读取到 EOF；静态 Skill 文件未变化，不重复注入。"
            "请使用阶段记忆和已写正式文件继续；不要从头重读。"
        )
    return None


def read_file(agent, path, offset=1, limit=500, **_extra):
    """读文本文件,按 hermes 输出 'LINE_NUM|CONTENT'。图片用 vision_analyze。可读 ws 内或只读 skill 树。"""
    normalized = str(path or "").replace("\\", "/").lstrip("./")
    policy = _skill_read_policy(agent, normalized, offset)
    if policy:
        return policy
    if (
        str(getattr(agent, "role", "") or "").lower() != "orchestrator"
        and normalized.lower() == "skills/mural-presenter/skill.md"
    ):
        language = str(getattr(agent, "prompt_language", "zh") or "zh").lower()
        return (
            "The root workflow belongs to the Orchestrator. Use your required role card and "
            "only the task-specific plans/references named there; do not reread SKILL.md."
            if language == "en" else
            "根工作流只由编排器读取。请使用已指定的唯一角色卡，以及角色卡点名的计划和参考；"
            "不要重复读取 SKILL.md。"
        )
    if (
        str(getattr(agent, "role", "") or "").lower() == "orchestrator"
        and re.match(r"^materials/(?:_raw|_work)(?:/|$)", normalized, re.I)
    ):
        return (
            "read_file 已阻止编排器直接读取附件正文或解析中间物。"
            "请先委派 Material；其 ready/complete 后，只读取 "
            "materials/summaries/<assignment_id>.md，再结合原始 query 决定 Research。"
        )
    if (
        str(getattr(agent, "role", "") or "").lower() == "orchestrator"
        and re.search(
            r"(?:^|/)_trace/(?:[^/]+/)*subagents/[^/]+/"
            r"(?:messages|tool_log|system_prompt|tools)\.(?:json|md)$",
            normalized,
            re.I,
        )
    ):
        return (
            "read_file 已阻止把子 Agent 完整轨迹灌回父上下文。"
            "delegate_task 已返回结构化 contract、artifact paths 与 handoff_path；"
            "需要核对最终交接时只读对应 handoff.json，不要读取 messages.json/tool_log.json。"
        )
    if os.path.splitext(path)[1].lower() in IMG_EXT:
        return f"read_file 错误:'{path}' 是图片,请用 vision_analyze 查看。"
    fp = agent.read_path(path)
    if os.path.isdir(fp):
        entries = sorted(os.listdir(fp))
        if not entries:
            return f"{path}/ (空目录)"
        listing = [e + ("/" if os.path.isdir(os.path.join(fp, e)) else "") for e in entries]
        return f"{path}/ 下的条目:\n" + "\n".join(listing)
    with open(fp, encoding="utf-8") as f:
        all_lines = f.read().splitlines()
    total = len(all_lines)
    start = max(1, int(offset or 1))
    lim = max(1, int(limit or 500))
    sel = all_lines[start - 1:start - 1 + lim]
    numbered = "\n".join(f"{start + i}|{ln}" for i, ln in enumerate(sel))
    # Role cards are mandatory and static. Returning one in full costs no more
    # tokens than paginating it, but removes 1–3 serial model round trips for
    # every Image/Slide/Review worker.
    role_card = bool(re.fullmatch(
        r"skills/mural-presenter/roles/(?:research|material|image|slide|review)\.md",
        normalized,
        re.I,
    ))
    CAP = ROLE_CARD_READ_CAP if role_card else 7800
    last = start + len(sel) - 1
    if len(numbered) > CAP:
        cut = numbered[:CAP].rsplit("\n", 1)[0]
        shown = max(1, len(cut.splitlines()))
        last = start + shown - 1
        numbered = cut
    has_more = bool(sel) and last < total
    pending = getattr(agent, "_pending_read_continuations", None)
    if not isinstance(pending, dict):
        pending = {}
        setattr(agent, "_pending_read_continuations", pending)
    started = getattr(agent, "_started_read_paths", None)
    if not isinstance(started, set):
        started = set()
        setattr(agent, "_started_read_paths", started)
    completed = getattr(agent, "_completed_read_paths", None)
    if not isinstance(completed, set):
        completed = set()
        setattr(agent, "_completed_read_paths", completed)
    key = _read_key(path)
    if start == 1:
        started.add(key)
        completed.discard(key)
    required_role_card = _read_key(
        getattr(agent, "_required_role_card_path", "")
    )
    if role_card and key == required_role_card and start == 1:
        agent._required_role_card_started = True
    if has_more:
        pending[key] = last + 1
        completed.discard(key)
        numbered += (
            f"\n\n[… 截断:已显示第 {start}–{last} 行(共 {total} 行);"
            f"续读 offset={last + 1}]"
        )
    else:
        pending.pop(key, None)
        if key in started:
            completed.add(key)
        if (
            role_card
            and key == required_role_card
            and getattr(agent, "_required_role_card_started", False)
        ):
            agent._required_role_card_complete = True
    if not sel and start > total:
        pending.pop(key, None)
        return f"{path}: offset={start} 已超过文件末尾(共 {total} 行)"
    return numbered


def _visual_source_path(agent, path):
    try:
        fp = agent.safe(path) if not os.path.isabs(str(path)) else os.path.realpath(str(path))
        rel = os.path.relpath(fp, agent.ws).replace(os.sep, "/")
    except Exception:
        return None
    if rel == "base.css" or re.fullmatch(r"slides/slide_\d+\.html", rel, re.I):
        return fp
    return None


def _mark_visual_source_dirty(agent, path):
    fp = _visual_source_path(agent, path)
    if not fp:
        return
    dirty = getattr(agent, "_dirty_visual_sources", None)
    if not isinstance(dirty, set):
        dirty = set()
        setattr(agent, "_dirty_visual_sources", dirty)
    dirty.add(os.path.realpath(fp))


def _is_review_agent(agent):
    return str(getattr(agent, "label", "") or "").lower().startswith("review")


def _review_baseline_root(agent):
    trace = getattr(agent, "trace", None)
    root = os.path.join(
        str(getattr(trace, "sub_dir", "") or agent.safe("_trace/review")),
        "review-baselines",
    )
    os.makedirs(root, exist_ok=True)
    return root


def _fresh_render_for_source(agent, source, page):
    """Return the current PNG only when it is valid pre-edit pixel evidence."""
    png = os.path.join(agent.ws, "renders", f"slide_{page:02d}.png")
    css = os.path.join(agent.ws, "base.css")
    try:
        source_mtime = max(
            os.stat(source).st_mtime_ns,
            os.stat(css).st_mtime_ns if os.path.isfile(css) else 0,
        )
        if os.stat(png).st_mtime_ns < source_mtime:
            return None
    except OSError:
        return None
    return png


def _snapshot_review_baseline(agent, path):
    """Freeze rollback and comparison evidence before Review changes pixels.

    This is deliberately not an aesthetic checker.  It only guarantees that a
    Review edit has a trustworthy BEFORE state that can later be compared with
    the newly rendered AFTER state.
    """
    source = _visual_source_path(agent, path)
    if not source or not _is_review_agent(agent):
        return None
    root = _review_baseline_root(agent)
    rel = os.path.relpath(source, agent.ws).replace(os.sep, "/")

    if rel == "base.css":
        contact = os.path.join(agent.ws, "renders", "contact-sheet.png")
        if not os.path.isfile(contact):
            return (
                "Review 修改 base.css 前必须先生成 renders/contact-sheet.png，"
                "以便冻结全册基线并在修改后做 BEFORE | AFTER 回归对比。"
            )
        latest_page = max(
            (
                os.stat(png).st_mtime_ns
                for png in glob.glob(os.path.join(agent.ws, "renders", "slide_*.png"))
                if os.path.isfile(png)
            ),
            default=0,
        )
        if os.stat(contact).st_mtime_ns < latest_page:
            return (
                "Review 修改 base.css 前的 renders/contact-sheet.png 已早于最新逐页 PNG。"
                "请先重新生成联系表，再修改全局样式；旧联系表不能作为回归基线。"
            )
        baseline_css = os.path.join(root, "base.css")
        baseline_contact = os.path.join(root, "contact-sheet.png")
        if not os.path.isfile(baseline_css):
            shutil.copy2(source, baseline_css)
        if not os.path.isfile(baseline_contact):
            shutil.copy2(contact, baseline_contact)
        agent._review_global_visual_change = True
        agent._review_last_baseline_source = os.path.relpath(
            baseline_css, agent.ws
        ).replace(os.sep, "/")
        return None

    match = re.fullmatch(r"slides/slide_(\d+)\.html", rel, re.I)
    if not match:
        return None
    page = int(match.group(1))
    png = _fresh_render_for_source(agent, source, page)
    if not png:
        return (
            f"Review 修改 slide_{page:02d}.html 前缺少新鲜基线 PNG。"
            f"请先渲染并查看 renders/slide_{page:02d}.png，再执行修改；"
            "没有 BEFORE 证据的页面不得进入 Review 修复。"
        )
    baseline_html = os.path.join(root, f"slide_{page:02d}.html")
    baseline_png = os.path.join(root, f"slide_{page:02d}.png")
    if not os.path.isfile(baseline_html):
        shutil.copy2(source, baseline_html)
    if not os.path.isfile(baseline_png):
        shutil.copy2(png, baseline_png)
    modified = getattr(agent, "_review_modified_pages", None)
    if not isinstance(modified, set):
        modified = set()
        agent._review_modified_pages = modified
    modified.add(page)
    agent._review_last_baseline_source = os.path.relpath(
        baseline_html, agent.ws
    ).replace(os.sep, "/")
    return None


def _clear_rendered_dirty_sources(agent):
    dirty = getattr(agent, "_dirty_visual_sources", None)
    if not isinstance(dirty, set) or not dirty:
        return False
    remaining = set()
    slides = sorted(glob.glob(os.path.join(agent.ws, "slides", "slide_*.html")))
    for source in dirty:
        if os.path.basename(source) == "base.css":
            if not slides or any(
                not os.path.isfile(os.path.join(agent.ws, "renders", os.path.basename(path).replace(".html", ".png")))
                or os.stat(os.path.join(agent.ws, "renders", os.path.basename(path).replace(".html", ".png"))).st_mtime_ns
                < os.stat(source).st_mtime_ns
                for path in slides
            ):
                remaining.add(source)
            continue
        match = re.fullmatch(r"slide_(\d+)\.html", os.path.basename(source), re.I)
        png = os.path.join(agent.ws, "renders", f"slide_{int(match.group(1)):02d}.png") if match else ""
        if not png or not os.path.isfile(png) or os.stat(png).st_mtime_ns < os.stat(source).st_mtime_ns:
            remaining.add(source)
    changed = len(remaining) < len(dirty)
    agent._dirty_visual_sources = remaining
    return changed and not remaining


def write_file(agent, path, content, **_extra):
    if not getattr(agent, "writable", lambda p: True)(path):
        return (f"write_file 错误:当前角色不允许写 {path}(只读路径)。改写其它路径,"
                f"或通过 delegate_task 委派有权限的子 agent。")
    agent._review_last_baseline_source = None
    baseline_error = _snapshot_review_baseline(agent, path)
    if baseline_error:
        return baseline_error
    fp = agent.safe(path)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w", encoding="utf-8") as f:
        f.write(content)
    _mark_visual_source_dirty(agent, fp)
    baseline_note = (
        f"；Review 回滚基线: {agent._review_last_baseline_source}"
        if getattr(agent, "_review_last_baseline_source", None) else ""
    )
    return f"已写入 {len(content.encode())} 字节到 {path}{baseline_note}"


def patch(agent, mode="replace", path=None, old_string=None, new_string=None,
          replace_all=False, **_extra):
    """Apply the one supported Hermes patch operation: exact replacement."""
    if mode != "replace":
        return "patch 错误:mode 只能是 replace。"
    if not path or old_string is None or new_string is None:
        return "patch 错误:mode='replace' 需要 path、old_string、new_string。"
    if not getattr(agent, "writable", lambda p: True)(path):
        return f"patch 错误:当前角色不允许改 {path}(只读路径)。"
    fp = agent.safe(path)
    with open(fp, encoding="utf-8") as f:
        s = f.read()
    n = s.count(old_string)
    if n == 0:
        return f"patch 错误:在 {path} 里找不到 old_string"
    if n > 1 and not replace_all:
        return f"patch 错误:old_string 出现了 {n} 次(不唯一);确认全改请传 replace_all=true"
    agent._review_last_baseline_source = None
    baseline_error = _snapshot_review_baseline(agent, path)
    if baseline_error:
        return baseline_error
    with open(fp, "w", encoding="utf-8") as f:
        f.write(s.replace(old_string, new_string))
    _mark_visual_source_dirty(agent, fp)
    baseline_note = (
        f"；Review 回滚基线: {agent._review_last_baseline_source}"
        if getattr(agent, "_review_last_baseline_source", None) else ""
    )
    return f"已编辑 {path}{baseline_note}"


_BROAD_SEARCH_ROOT = (
    r"(?:/|/mnt/?|/workspace/?|/home/?|~|\$home|\$\{home\})"
)


def _is_unbounded_host_scan(command: str) -> bool:
    """Reject recursive host scans while preserving workspace-local searches."""
    normalized = " ".join(str(command).lower().split())
    quoted_root = rf"[\"']?{_BROAD_SEARCH_ROOT}[\"']?(?=\s|$)"
    patterns = (
        rf"(?:^|[;&|]\s*)find\s+(?:(?:-\w+|-[a-z]+\s+\S+)\s+)*{quoted_root}",
        rf"(?:^|[;&|]\s*)(?:du|ls)\s+(?:-[^\s]+\s+)*{quoted_root}",
        rf"(?:^|[;&|]\s*)grep\s+(?=[^;&|]*(?:\s-r\b|\s--recursive\b))[^;&|]*\s{quoted_root}",
        rf"(?:^|[;&|]\s*)(?:rg|fd)\s+[^;&|]*\s{quoted_root}",
    )
    return any(re.search(pattern, normalized, re.I) for pattern in patterns)


def terminal(agent, command, timeout=None, **_extra):
    """在工作区下执行前台命令，主要用于 Skill 自带的确定性脚本。"""
    if not isinstance(command, str) or not command.strip():
        return "terminal 错误:command 不能为空"
    # 护栏:渲染环境(playwright/chromium/字体/greenlet)已预装且可用,禁止子 agent 安装/重装/调试。
    # 实测弱模型撞 render 报警后会疯狂 pip install --force-reinstall playwright(单批 300+ 次),
    # 烧光 deck 预算 + 污染共享 venv 引发雪崩;物理禁掉。(2026-07-05 应 Master 要求)
    _norm = " ".join(command.lower().split())
    _forbidden = ("pip install", "pip3 install", "pip uninstall", "uv pip install", "uv add",
                  "conda install", "poetry add", "apt install", "apt-get install", "apt install",
                  "npm install", "yarn add", "pnpm add", "playwright install", "-m playwright",
                  "force-reinstall")
    python_pip = re.search(
        r"(?:^|\s)(?:python(?:3(?:\.\d+)*)?|py)\s+-m\s*"
        r"pip\s+(?:install|uninstall)\b",
        _norm,
    )
    if any(f in _norm for f in _forbidden) or python_pip:
        return ("terminal 拒绝:渲染环境(playwright/chromium/字体)已预装且可用,禁止安装/重装/调试它。"
                "render.py 报错的真实原因几乎都是你的 HTML/CSS 不合法或资源没加载——请重试一次,"
                "仍失败就简化/修正 HTML,绝不要 pip install / playwright install。")
    if os.environ.get("MURAL_PREFLIGHT_DONE") == "1" and "deck.py preflight" in _norm:
        return (
            "terminal 拒绝:环境和工作区预检已经由 Harness 在模型调用前完成。"
            "不要重复 preflight，也不要自主检查或修复 Python、字体、Chromium/Playwright；"
            "请直接开始任务解析和正式产出。"
        )
    if (
        str(getattr(agent, "role", "") or "").lower() == "orchestrator"
        and re.search(r"(?:^|[\s'\"=])materials/(?:_raw|_work)(?:/|[\s'\";|&]|$)", command, re.I)
    ):
        return (
            "terminal 拒绝:编排器不得通过 shell 读取 materials/_raw 或 materials/_work。"
            "请先委派 Material，并在其完成后读取 materials/summaries/ 的正式摘要。"
        )
    if _is_unbounded_host_scan(command):
        return (
            "terminal 拒绝:禁止从 /、/mnt、/workspace、/home 或整个 HOME "
            "开始递归搜索；这会扫描共享文件系统并让任务长时间假死。托管任务中的环境问题"
            "应由 Harness 在模型调用前报告，Agent 不得自行修复。确需只读业务文件检索时，"
            "把范围限定到当前工作区并设置 -maxdepth。"
        )
    # Review visual edits must pass through write_file/patch so the runtime can
    # freeze rollback pixels before changing the source.  Block only obvious
    # shell mutation of visual sources; render/build/inspection remain allowed.
    if (
        _is_review_agent(agent)
        and re.search(r"(?:base\.css|slides?/slide_\d+\.html)", _norm, re.I)
        and re.search(r"(?:\bsed\s+-i\b|\bperl\s+-p?i\b|\bpython\w*\s+(?:-c|-)\b|>>?|\btee\b)", _norm)
    ):
        return (
            "terminal 拒绝:Review 不得用 shell 直接改 base.css 或 slide HTML。"
            "请使用 patch/write_file；Harness 会在写入前自动冻结 HTML 与 PNG 基线，"
            "并在重渲后提供 BEFORE | AFTER 回归证据。"
        )
    quality_command = "deck.py build" in _norm or "render.py" in _norm
    if quality_command and re.search(r"(?:\||;|&&|\|&)\s*(?:tail|head)\b", _norm):
        return (
            "terminal 拒绝:render/build 属于质量门，禁止用 tail/head 截断逐页诊断。"
            "直接运行原命令；结构化结论同时保存在 renders/render.json 和 "
            "_trace/render-issues.json。"
        )
    to = parse_optional_timeout(timeout, TERMINAL_DEFAULT_TIMEOUT)
    if to is not None and FOREGROUND_MAX_TIMEOUT is not None:
        to = min(to, FOREGROUND_MAX_TIMEOUT)
    cwd = agent.ws
    try:
        r = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True, timeout=to)
    except subprocess.TimeoutExpired:
        return f"terminal 错误:命令触发显式等待上限 {to}s"
    except Exception as e:
        return f"terminal 错误:{e}"
    out = (r.stdout or "")
    if r.stderr:
        out += ("\n[stderr] " + r.stderr)
    out = out.strip()
    if r.returncode:
        out = f"[exit_code={r.returncode}]\n" + out
    # 不在工具实现里静默截断；agent 适配层会把超长结果完整落到
    # _trace/.../tool-results/，并返回可继续 read_file 的预览。
    body = out if out else f"(命令完成,退出码 {r.returncode},无输出)"
    if r.returncode == 0 and re.search(r"\basset-download\b", _norm):
        created = getattr(agent, "_created_asset_paths", None)
        if not isinstance(created, set):
            created = set(created or [])
            agent._created_asset_paths = created
        created.update(re.findall(
            r"assets/web_[A-Za-z0-9._-]+\.(?:png|jpe?g|webp|gif)",
            out,
            re.I,
        ))
    if r.returncode == 0 and "render.py" in _norm:
        completed_round = _clear_rendered_dirty_sources(agent)
        if completed_round and str(getattr(agent, "label", "") or "").lower().startswith("review"):
            agent._review_refine_rounds = int(getattr(agent, "_review_refine_rounds", 0) or 0) + 1
        if re.search(r"\b(?:boxoverflow|overlap|innergap)=\d+", out, re.I):
            body += (
                "\n[诊断提示] boxoverflow / overlap / innergap 是 bbox 候选，不是进程失败。"
                "先看本次新 PNG；只有像素或 DOM 证明真实遮挡、裁切、不可读或无职责空洞时"
                "才修改。若像素正常，记录 checker mismatch 并保留当前构图。"
            )
    return body


MAX_VISION_EDGE = int(os.environ.get("MAX_VISION_EDGE", "1536"))   # 送模型的图最长边上限

def _vision_response_language(agent):
    """Return the task-level visible response language for same-model Vision."""
    language = str(getattr(agent, "prompt_language", "") or "").lower()
    if language not in {"zh", "en"}:
        cfg = getattr(agent, "cfg", {}) or {}
        language = str(cfg.get("_prompt_language") or "").lower()
    return language if language in {"zh", "en"} else "zh"


def _slide_vision_freshness_error(agent, fp):
    """Reject stale or no-progress inspection of one rendered slide.

    This is deliberately a small runtime invariant rather than a visual-quality
    policy: once page HTML or shared CSS changes, the previous PNG is no longer
    evidence.  Re-reading identical bytes more than twice without any source or
    render change cannot reveal new pixels and usually indicates a Review loop.
    """
    match = re.fullmatch(r"slide_(\d+)\.png", os.path.basename(fp), re.IGNORECASE)
    if not match or os.path.basename(os.path.dirname(fp)) != "renders":
        return None

    page = match.group(1)
    source_candidates = [
        os.path.join(agent.ws, "slides", f"slide_{page}.html"),
        os.path.join(agent.ws, "base.css"),
    ]
    try:
        png_mtime = os.stat(fp).st_mtime_ns
    except OSError:
        return None
    source_mtime = max(
        (os.stat(path).st_mtime_ns for path in source_candidates if os.path.isfile(path)),
        default=0,
    )
    if source_mtime > png_mtime:
        return (
            f"vision_analyze 已阻止旧像素：slide_{page}.html 或 base.css 在 "
            f"renders/slide_{page}.png 之后发生了修改。先运行 "
            f"render.py --batch . --pages {int(page):02d}，再查看新 PNG；"
            "不得用旧图判断修复是否生效。"
        )

    try:
        with open(fp, "rb") as source:
            digest = hashlib.sha256(source.read()).hexdigest()
    except OSError:
        return None
    observations = getattr(agent, "_slide_vision_observations", None)
    if not isinstance(observations, dict):
        observations = {}
        setattr(agent, "_slide_vision_observations", observations)
    key = os.path.realpath(fp)
    state = (digest, png_mtime, source_mtime)
    previous = observations.get(key)
    if previous and previous.get("state") != state and previous.get("state", (None,))[0] == digest:
        rollback_to_baseline = False
        if _is_review_agent(agent):
            try:
                baseline = os.path.join(
                    _review_baseline_root(agent), f"slide_{int(page):02d}.png"
                )
                with open(baseline, "rb") as source:
                    rollback_to_baseline = hashlib.sha256(source.read()).hexdigest() == digest
            except OSError:
                rollback_to_baseline = False
        observations[key] = {"state": state, "count": 1}
        if not rollback_to_baseline:
            return (
                f"vision_analyze 检测到无效修复：renders/slide_{page}.png 虽已重新生成，"
                "但像素字节与该 Agent 上次查看的版本完全相同。当前修改没有改变页面；"
                "请回到重叠对象的坐标、尺寸或结构根因，不要继续复看相同像素。"
            )
    count = int(previous.get("count", 0)) + 1 if previous and previous.get("state") == state else 1
    observations[key] = {"state": state, "count": count}
    if count > 2:
        return (
            f"vision_analyze 已阻止无进展复看：renders/slide_{page}.png 的像素和相关源文件"
            "均未变化，且同一 Agent 已查看两次。请修改根因并重新渲染，或停止该轮并返回 "
            "blocked；继续询问 Vision 不会产生新证据。"
        )
    return None


def _contact_sheet_freshness_error(agent, fp):
    """Reject a contact sheet that no longer represents current page pixels.

    A group or Review contact sheet is evidence, not a decorative convenience.
    Its sidecar freezes the SHA-256 of every included page.  If a page was
    edited or re-rendered afterwards, the old sheet must never be sent to
    Vision; otherwise a worker can repeatedly diagnose and "repair" pixels that
    no longer exist.
    """
    render_dir = os.path.join(agent.ws, "renders")
    try:
        if os.path.dirname(os.path.realpath(fp)) != os.path.realpath(render_dir):
            return None
    except OSError:
        return None
    name = os.path.basename(fp)
    if not re.fullmatch(r"contact-sheet(?:-focus-[A-Za-z0-9._-]+|-review-\d+)?\.png", name, re.I):
        return None

    evidence = []
    pages = []
    metadata_path = ""
    if name.lower().startswith("contact-sheet-focus-"):
        metadata_path = os.path.splitext(fp)[0] + ".json"
        try:
            payload = json.loads(Path(metadata_path).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            payload = {}
        evidence = payload.get("evidence") or []
        pages = payload.get("pages") or []
    else:
        metadata_path = os.path.join(render_dir, "review-contact.json")
        try:
            full = json.loads(
                Path(metadata_path).read_text(encoding="utf-8")
            ).get("full") or {}
        except (OSError, ValueError, TypeError, AttributeError):
            full = {}
        if name.lower() == "contact-sheet.png":
            evidence = full.get("evidence") or []
            pages = full.get("pages") or []
        else:
            relative = os.path.relpath(fp, agent.ws).replace(os.sep, "/")
            group = next(
                (
                    item for item in full.get("groups") or []
                    if isinstance(item, dict) and item.get("path") == relative
                ),
                {},
            )
            evidence = group.get("evidence") or []
            pages = group.get("pages") or []

    if not pages:
        return (
            f"vision_analyze 已阻止缺少页面快照清单的联系表：{name}。请先运行 "
            "deck.py contact 重新生成联系表及其 JSON 证据，再进行像素判断。"
        )
    evidence_by_page = {
        int(item.get("page")): item
        for item in evidence
        if isinstance(item, dict) and str(item.get("page") or "").isdigit()
    }
    stale = []
    for raw_page in pages:
        try:
            page = int(raw_page)
        except (TypeError, ValueError):
            continue
        item = evidence_by_page.get(page)
        png = os.path.join(render_dir, f"slide_{page:02d}.png")
        if not item or not os.path.isfile(png):
            stale.append(page)
            continue
        expected = str(item.get("sha256") or "")
        try:
            with open(png, "rb") as source:
                current = hashlib.sha256(source.read()).hexdigest()
            png_mtime = os.stat(png).st_mtime_ns
        except OSError:
            stale.append(page)
            continue
        sources = [
            os.path.join(agent.ws, "slides", f"slide_{page:02d}.html"),
            os.path.join(agent.ws, "base.css"),
            os.path.join(agent.ws, "plan", "theme.css"),
        ]
        source_mtime = max(
            (os.stat(path).st_mtime_ns for path in sources if os.path.isfile(path)),
            default=0,
        )
        if not expected or expected != current or source_mtime > png_mtime:
            stale.append(page)
    if stale:
        return (
            f"vision_analyze 已阻止旧联系表：{name} 不再代表当前页面 "
            + ",".join(f"{page:02d}" for page in sorted(set(stale)))
            + "。先重渲受影响页，再运行 deck.py contact 生成新的组联系表；"
              "不得依据旧联系表继续修改。"
        )
    return None


def _review_vision_repeat_error(agent, fp):
    """Review may revisit a path only after its image bytes have changed."""
    label = str(getattr(agent, "label", "") or "").lower()
    if not label.startswith("review"):
        return None
    try:
        with open(fp, "rb") as source:
            digest = hashlib.sha256(source.read()).hexdigest()
    except OSError:
        return None
    seen = getattr(agent, "_review_vision_seen", None)
    if not isinstance(seen, dict):
        seen = {}
        setattr(agent, "_review_vision_seen", seen)
    key = os.path.realpath(fp)
    if digest and seen.get(key) == digest:
        match = re.fullmatch(r"slide_(\d+)\.png", os.path.basename(fp), re.I)
        page = int(match.group(1)) if match and os.path.basename(os.path.dirname(fp)) == "renders" else None
        pending_rollback_comparison = (
            page in getattr(agent, "_review_modified_pages", set())
            and page not in getattr(agent, "_review_comparison_pages", set())
        )
        if pending_rollback_comparison:
            return None
        return (
            f"vision_analyze 已阻止 Review 重复查看未变化像素：{os.path.relpath(fp, agent.ws)}。"
            "先把本轮发现写入唯一问题账本并继续尚未覆盖的页面；只有页面修改并重新渲染、"
            "或联系表重新生成且像素变化后才可复验。改变问题措辞不会产生新证据。"
        )
    if digest:
        seen[key] = digest
    return None


def _review_before_after_image(agent, fp):
    """Create auditable BEFORE/AFTER pixels for Review's first final check.

    The runtime only presents evidence.  It does not decide whether the new
    design is prettier or semantically correct; that judgment remains with the
    same-model Vision pass and the Review contract.
    """
    if not _is_review_agent(agent):
        return fp, None, None
    rel = os.path.relpath(fp, agent.ws).replace(os.sep, "/")
    baseline_root = _review_baseline_root(agent)
    baseline = None
    comparison_key = None
    page = None
    match = re.fullmatch(r"renders/slide_(\d+)\.png", rel, re.I)
    modified = getattr(agent, "_review_modified_pages", set())
    if match and int(match.group(1)) in modified:
        page = int(match.group(1))
        baseline = os.path.join(baseline_root, f"slide_{page:02d}.png")
        comparison_key = f"slide_{page:02d}"
    elif (
        rel == "renders/contact-sheet.png"
        and bool(getattr(agent, "_review_global_visual_change", False))
    ):
        baseline = os.path.join(baseline_root, "contact-sheet.png")
        comparison_key = "contact-sheet"
    if not baseline or not os.path.isfile(baseline):
        return fp, None, None

    try:
        from PIL import Image, ImageDraw

        with Image.open(baseline) as old_source, Image.open(fp) as new_source:
            old = old_source.convert("RGB")
            new = new_source.convert("RGB")
            width = max(old.width, new.width)
            header = 46
            gap = 18
            canvas = Image.new(
                "RGB", (width, header * 2 + old.height + new.height + gap), "#20242a"
            )
            canvas.paste(old, ((width - old.width) // 2, header))
            after_y = header + old.height + gap + header
            canvas.paste(new, ((width - new.width) // 2, after_y))
            draw = ImageDraw.Draw(canvas)
            draw.text((18, 14), "BEFORE — preserve strengths and semantics", fill="#f2f4f7")
            draw.text(
                (18, header + old.height + gap + 14),
                "AFTER — verify improvement and detect regression",
                fill="#f2f4f7",
            )
        output_dir = os.path.join(
            str(getattr(getattr(agent, "trace", None), "sub_dir", "") or baseline_root),
            "review-comparisons",
        )
        os.makedirs(output_dir, exist_ok=True)
        output = os.path.join(output_dir, f"{comparison_key}-before-after.png")
        canvas.save(output, format="PNG")
    except Exception:
        # Failure to create the comparison must not silently count as evidence.
        return fp, None, None

    if page is not None:
        compared = getattr(agent, "_review_comparison_pages", None)
        if not isinstance(compared, set):
            compared = set()
            agent._review_comparison_pages = compared
        compared.add(page)
    else:
        agent._review_global_comparison = True
    if _vision_response_language(agent) == "en":
        instruction = (
            "This is the same page before and after Review (BEFORE above, AFTER below). "
            "Compare openly first: name the preserved strengths, real improvements, new "
            "regressions, and the objects, relationships, directions, and conclusion a viewer "
            "can now read. Only then check the original defect. A vanished warning or deleted "
            "element is not sufficient evidence of improvement."
        )
    else:
        instruction = (
            "这是 Review 修改前后的同页像素证据（上方 BEFORE、下方 AFTER）。"
            "先开放比较：列出保留下来的旧版优点、新版真实改善、新增退化，以及观众现在读出的"
            "对象、关系、方向和结论；之后再核对原缺陷。不得只以告警消失或元素被删作为改善。"
        )
    return output, instruction, os.path.relpath(output, agent.ws).replace(os.sep, "/")


def vision_analyze(agent, image_url, question=None, **_extra):
    """把工作区图片加载给当前 Agent 模型亲自查看。"""
    path = image_url
    response_language = _vision_response_language(agent)
    fp = agent.read_path(path)              # 一律走沙箱
    if not os.path.exists(fp) or os.path.isdir(fp):
        return f"vision_analyze 错误:没有这张图 {path}"
    label = str(getattr(agent, "label", "") or "").lower()
    # Material 可以查看整页、裁图和局部放大，但反复把同一未变化页送进 Vision
    # 不会提高 OCR/理解质量。需要复核局部时应生成新的裁图文件。
    if label.startswith("material"):
        try:
            with open(fp, "rb") as source:
                digest = hashlib.sha256(source.read()).hexdigest()
        except OSError:
            digest = ""
        seen = getattr(agent, "_material_vision_seen", None)
        if not isinstance(seen, dict):
            seen = {}
            setattr(agent, "_material_vision_seen", seen)
        key = os.path.realpath(fp)
        previous_digest, previous_count = seen.get(key, (None, 0))
        count = int(previous_count) + 1 if previous_digest == digest else 1
        state = (digest, count)
        seen[key] = state
        if digest and state[1] > 2:
            return (
                f"vision_analyze 已阻止 Material 无进展复看：{path} 的像素未变化且已查看两次。"
                "若需核对局部，请生成不同的裁图后查看；否则记录当前证据并按返回合同收口。"
            )
    # Image 子代理应对每个最终候选做一次完整检查。改变问题措辞并不会
    # 产生新像素证据；弱模型若在图像上下文释放后反复打开同一文件，会
    # 形成数百次无效 Vision 调用。仅当文件字节发生变化时允许再次检查。
    if label.startswith("image"):
        try:
            with open(fp, "rb") as source:
                digest = hashlib.sha256(source.read()).hexdigest()
        except OSError:
            digest = ""
        seen = getattr(agent, "_image_vision_seen", None)
        if not isinstance(seen, dict):
            seen = {}
            setattr(agent, "_image_vision_seen", seen)
        key = os.path.realpath(fp)
        if digest and seen.get(key) == digest:
            return (
                f"vision_analyze 已阻止重复素材检查：{path} 的像素自上次检查后未变化。"
                "先前视觉结论仍有效；请记录该资产状态并继续尚未检查的候选，"
                "或直接按返回合同收口。只有重生、替换或修图后才可复检。"
            )
        if digest:
            seen[key] = digest
    contact_freshness_error = _contact_sheet_freshness_error(agent, fp)
    if contact_freshness_error:
        return contact_freshness_error
    review_repeat_error = _review_vision_repeat_error(agent, fp)
    if review_repeat_error:
        return review_repeat_error
    freshness_error = _slide_vision_freshness_error(agent, fp)
    if freshness_error:
        return freshness_error
    model_fp, comparison_instruction, comparison_path = _review_before_after_image(agent, fp)
    if comparison_instruction:
        question = f"{comparison_instruction}\n\n{str(question or '').strip()}".strip()
    try:
        import io
        from PIL import Image
        with Image.open(model_fp) as im:
            im = im.convert("RGB")
            w, h = im.size
            edge_limit = max(MAX_VISION_EDGE, 2048) if comparison_path else MAX_VISION_EDGE
            scale = edge_limit / max(w, h)
            if scale < 1:
                im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            data = buf.getvalue()
    except ImportError:
        with open(model_fp, "rb") as f:
            data = f.read()
    except Exception as e:
        return (f"vision_analyze 错误:{path} 不是可解析的图片({type(e).__name__})。"
                f"只能查看 PNG/JPG 等图片;HTML 页请先渲染成 PNG 再看。")
    if os.environ.get("NOVA_RAW_V2", "0") == "1":
        from . import nova_bridge

        parent_tool_use_id = str(_extra.get("_parent_tool_use_id") or "")
        try:
            analysis = nova_bridge.call_vision_auxiliary(
                agent,
                image_bytes=data,
                media_type="image/png",
                source_path=os.path.relpath(model_fp, agent.ws),
                question=str(question or ""),
                parent_tool_use_id=parent_tool_use_id,
            )
        except Exception as exc:  # noqa: BLE001
            return (
                "vision_analyze 错误:Nova auxiliary model 不可用；"
                f"{type(exc).__name__}: {str(exc)[:180]}"
            )
        result = {
            "vision_analysis": f"[Nova 像素审校 · {path}]\n{analysis}",
            "path": os.path.relpath(fp, agent.ws),
            "vision_backend": "nova_auxiliary_model",
        }
        if comparison_path:
            result.update({"comparison_mode": "before_after", "comparison_path": comparison_path})
        return result
    from . import one_shot_vision

    if one_shot_vision.enabled():
        parent_tool_use_id = str(_extra.get("_parent_tool_use_id") or "")
        analysis = one_shot_vision.call(
            agent,
            image_bytes=data,
            media_type="image/png",
            source_path=os.path.relpath(model_fp, agent.ws),
            question=str(question or ""),
            parent_tool_use_id=parent_tool_use_id,
        )
        result = {
            "vision_analysis": f"[独立 Vision 单次审校 · {path}]\n{analysis}",
            "path": os.path.relpath(fp, agent.ws),
            "vision_backend": "internal_one_shot",
            "vision_model": os.environ.get(
                "VISION_ONESHOT_MODEL",
                os.environ.get("TOKENHUB_VISION_MODEL", "gemini-3.5-flash"),
            ),
        }
        if comparison_path:
            result.update({"comparison_mode": "before_after", "comparison_path": comparison_path})
        return result
    if response_language == "en":
        summary = f"Inspecting {path}. Give the visual judgment in English."
    else:
        summary = f"正在查看 {path}。请用中文给出视觉判断。"
    if comparison_instruction:
        summary = f"{summary} {comparison_instruction}"
    result = {"image_b64": base64.b64encode(data).decode(), "media_type": "image/png",
              "path": os.path.relpath(fp, agent.ws), "summary": summary}
    if comparison_path:
        result.update({"comparison_mode": "before_after", "comparison_path": comparison_path})
    return result


def web_search(agent, query, limit=5, search_type="search", **_extra):
    """联网搜索(serper)。search_type="search"(默认)走网页搜索 /search,返回标题 / 链接 / 摘要;
    search_type="images" 走图搜 /images,返回真实图片的直链 URL / 标题 / 来源(配 deck 真图时用这个)。
    注:serper 的网页搜索端点几乎不返图,真要搜图必须显式传 search_type="images"。"""
    if not agent.serper:
        return "web_search 不可用(未配置 serper key)"
    # Serper returns at most ten results from this endpoint call.  Make that
    # per-call transport boundary explicit instead of accepting a larger value
    # and silently pretending it was honored.  It is not a lifetime search cap.
    n = min(int(limit or 5), 10)
    images = str(search_type).lower() == "images"
    endpoint = "https://google.serper.dev/images" if images else "https://google.serper.dev/search"
    try:
        r = requests.post(endpoint,
                          headers={"X-API-KEY": agent.serper, "Content-Type": "application/json"},
                          json={"q": query, "num": n}, timeout=WEB_REQUEST_TIMEOUT).json()
    except Exception as e:
        return f"web_search 错误:{e}"
    out = []
    if images:
        for it in r.get("images", [])[:n]:
            url = it.get("imageUrl") or it.get("link")
            out.append(f"- [图] {it.get('title', '')}\n  {url}\n  来源:{it.get('source', '')}")
        return "\n".join(out) or "(无图片结果)"
    for it in r.get("organic", [])[:n]:
        out.append(f"- {it.get('title')}\n  {it.get('link')}\n  {it.get('snippet', '')}")
    return "\n".join(out) or "(无结果)"


def _pdf_text(data):
    """Extract every page when web_extract receives an online PDF."""
    errors = []
    try:
        import fitz
        document = fitz.open(stream=data, filetype="pdf")
        try:
            return "\n\n".join(
                f"### PDF page {index + 1}\n\n{page.get_text('text').strip()}"
                for index, page in enumerate(document)
            ).strip()
        finally:
            document.close()
    except Exception as exc:
        errors.append(f"PyMuPDF:{type(exc).__name__}")
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join(
            f"### PDF page {index + 1}\n\n{(page.extract_text() or '').strip()}"
            for index, page in enumerate(reader.pages)
        ).strip()
    except Exception as exc:
        errors.append(f"pypdf:{type(exc).__name__}")
    return "PDF 文本提取失败:" + ",".join(errors)


def _fetch_one(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=WEB_REQUEST_TIMEOUT) as resp:
            data = resp.read()
            content_type = str(resp.headers.get("Content-Type") or "").lower()
    except Exception as e:
        return f"fetch 错误:{e}"
    if "application/pdf" in content_type or data[:5] == b"%PDF-":
        return _pdf_text(data)
    html = data.decode("utf-8", "replace")
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", html)).strip()


def web_extract(agent, urls, **_extra):
    """抓取全部给定 URL，抽取 HTML 正文或在线 PDF 的逐页文本。"""
    if isinstance(urls, str):
        urls = [urls]
    if not isinstance(urls, list) or not urls:
        return "web_extract 错误:需要非空 urls 数组"
    parts = []
    for u in urls:
        parts.append(f"## {u}\n\n{_fetch_one(u)}")
    return "\n\n".join(parts)


_ASPECT_SIZE = {"landscape": "1536x1024", "portrait": "1024x1536", "square": "1024x1024"}


def _image_gen_one_serial(agent, prompt, size, aspect_ratio=None):
    """单张出图(退避重试 + 落盘 assets/,内容寻址命名)。返回相对路径或错误串。"""
    # 配图后端偶发瞬时不可用(503/超时/空 data),工具层退避重试,别让子 agent 几次手动重试就放弃丢图。
    # 配图是 deck 质量关键(用图率),这里多扛几次比丢一张 hero 图划算。
    d, last_err = None, ""
    for attempt in range(4):
        try:
            d = requests.post(f"{agent.img_base}/images/generations",
                              headers={"Authorization": f"Bearer {agent.img_key}",
                                       "Content-Type": "application/json"},
                              json={"model": agent.image_model, "prompt": prompt, "size": size, "n": 1},
                              timeout=IMAGE_GENERATION_TIMEOUT).json()
        except Exception as e:
            last_err = str(e)[:160]
            d = None
        if d is not None and "data" in d and d["data"]:
            break
        if d is not None and "data" not in d:
            last_err = json.dumps(d, ensure_ascii=False)[:160]
        if attempt < 3:
            time.sleep(3 * (attempt + 1))   # 3/6/9s 退避
    if d is None or "data" not in d or not d["data"]:
        return f"image_generate 错误(重试 4 次仍失败):{last_err}"
    it = d["data"][0]
    if it.get("b64_json"):
        data = base64.b64decode(it["b64_json"])
    elif it.get("url"):
        try:
            data = requests.get(it["url"], timeout=IMAGE_DOWNLOAD_TIMEOUT).content
        except Exception as e:
            return f"image_generate 错误:下载图片失败 {e}"
    else:
        return "image_generate 错误:没有返回图片"
    # hermes schema 无 out_path:用 prompt 的内容寻址名,保证并行子 agent 不撞名(同 prompt→同文件,无碍)。
    name = f"img_{hashlib.sha1(prompt.encode('utf-8')).hexdigest()[:10]}.png"
    rel = f"assets/{name}"
    os.makedirs(agent.safe("assets"), exist_ok=True)
    with open(agent.safe(rel), "wb") as f:
        f.write(data)
    _record_asset_provenance(
        agent, rel, origin="generated", generator_model=agent.image_model,
        prompt=prompt, aspect_ratio=aspect_ratio,
    )
    return rel


def _image_gen_one(agent, prompt, size, aspect_ratio=None):
    """Generate one image under a backend-specific concurrency gate.

    Image workers may run in parallel with Slide/Research workers, while the
    image endpoint itself can be capped independently when its queue is narrow.
    """
    with _IMAGE_GENERATION_SEMAPHORE:
        return _image_gen_one_serial(agent, prompt, size, aspect_ratio)


def image_generate(agent, prompt, aspect_ratio="landscape", **_extra):
    """根据文本提示生成图片(照片/插画/主视觉,不用于数据图表)。存到 assets/,返回相对路径。
    aspect_ratio:landscape(16:9 宽)/portrait(16:9 高)/square(1:1)。"""
    return _image_gen_one(
        agent, prompt, _ASPECT_SIZE.get(aspect_ratio, "1536x1024"), aspect_ratio
    )


# =============================================================== 工具 schema（Hermes 兼容的 parameters 风格）

READ_FILE_SCHEMA = {
    "name": "read_file",
    "description": "按行读取文本文件。大文件用 offset 和 limit 分段读取；返回中若出现续读 offset，说明文件尚未到底。图片和二进制文件请用 vision_analyze。",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要读取的文件路径"},
            "offset": {"type": "integer", "description": "起始行号，从 1 开始", "default": 1, "minimum": 1},
            "limit": {"type": "integer", "description": "最多读取的行数", "default": 500, "maximum": 2000},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
}

WRITE_FILE_SCHEMA = {
    "name": "write_file",
    "description": "写入完整文件并覆盖原内容，同时自动创建父目录。只改局部时使用 patch。常见结构化文件写入后会自动做语法检查。",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要创建或覆盖的文件路径"},
            "content": {"type": "string", "description": "文件的完整内容"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    },
}

PATCH_SCHEMA = {
    "name": "patch",
    "description": (
        "对正式文本文件做精确替换。只开放实际实现的 replace 模式；"
        "old_string 必须精确匹配且唯一，除非 replace_all=true。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["replace"],
                "description": "编辑模式，固定为 replace",
                "default": "replace",
            },
            "path": {"type": "string", "description": "replace 模式要修改的文件"},
            "old_string": {"type": "string", "description": "replace 模式中要查找的唯一原文；必要时带上上下文"},
            "new_string": {"type": "string", "description": "replace 模式的替换文本；空字符串表示删除"},
            "replace_all": {"type": "boolean", "description": "是否替换所有命中项", "default": False},
        },
        "required": ["mode", "path", "old_string", "new_string"],
        "additionalProperties": False,
    },
}

TERMINAL_TOOL_DESCRIPTION = """在 Linux 环境中执行命令。

- 读文件用 read_file，写文件用 write_file，局部修改用 patch；只读检索可在 terminal 中使用 rg。
- terminal 主要用于运行构建、测试和 Skill 自带脚本。
- 当前运行时以前台方式执行命令并等待其完成；默认没有额外的等待截止。
"""

TERMINAL_SCHEMA = {
    "name": "terminal",
    "description": TERMINAL_TOOL_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的命令"},
            "timeout": {"type": "integer", "description": "可选等待秒数；不填写则等待命令自然结束，部署方可另设保护上限", "minimum": 1},
        },
        "required": ["command"],
        "additionalProperties": False,
    },
}

VISION_ANALYZE_SCHEMA = {
    "name": "vision_analyze",
    "description": (
        "加载工作区中的本地图片并检查真实像素。"
        "请在 question 中写明要判断的具体问题，不要只写“看一下”。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "image_url": {"type": "string", "description": "工作区图片路径"},
            "question": {"type": "string", "description": "需要根据像素回答的具体问题"},
        },
        "required": ["image_url", "question"],
        "additionalProperties": False,
    },
}

WEB_SEARCH_SCHEMA = {
    "name": "web_search",
    "description": "搜索网页或真实图片。search_type='search' 返回网页结果；search_type='images' 返回图片直链和来源站点。具名人物、地点、产品、作品和事件优先搜索真图。",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索词，可包含后端支持的 site:、filetype:pdf 和精确短语等操作符"},
            "limit": {"type": "integer", "description": "本次返回 1–10 条；这是单次批量，不是整个任务的搜索上限", "minimum": 1, "maximum": 10, "default": 5},
            "search_type": {"type": "string", "enum": ["search", "images"], "description": "search 搜网页，images 搜真实图片", "default": "search"},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}

WEB_EXTRACT_SCHEMA = {
    "name": "web_extract",
    "description": "提取给定网页或在线 PDF 的完整文本，并返回 Markdown。若结果较长，运行时会保存全文并给出可续读路径；直接图片 URL 不用此工具。",
    "parameters": {
        "type": "object",
        "properties": {
            "urls": {"type": "array", "items": {"type": "string"}, "description": "本次要提取的 URL 列表；列表中的项目都会处理", "minItems": 1},
        },
        "required": ["urls"],
        "additionalProperties": False,
    },
}

IMAGE_GENERATE_SCHEMA = {
    "name": "image_generate",
    "description": (
        "根据文本提示生成图片。后端和模型由用户配置，Agent 不自行选择。"
        "成功后返回 assets/ 下的工作区相对路径。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "图片的主体、场景、构图、风格、光线和安全区说明"},
            "aspect_ratio": {"type": "string", "enum": ["landscape", "square", "portrait"], "description": "横图、方图或竖图", "default": "landscape"},
        },
        "required": ["prompt"],
        "additionalProperties": False,
    },
}

DELEGATE_TASK_SCHEMA = {
    "name": "delegate_task",
    "description": (
        "在相互隔离的上下文中委派一批子 Agent。每项只写 goal，并以 "
        "Material <assignment_id>:、Research:、Image <group_id>:、Slide Group <group_id> [页码]: "
        "或 Review: 开头。运行时由前缀推导角色、轨迹名和工具白名单。"
    ),
    "parameters": {
        "type": "object",
        "required": ["tasks"],
        "additionalProperties": False,
        "properties": {
            "tasks": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["goal"],
                    "additionalProperties": False,
                    "properties": {
                        "goal": {"type": "string", "description": "包含角色前缀、目标、输入路径和交付要求的自包含任务"},
                    },
                },
                "description": "要并行委派的子任务列表",
            },
        },
    },
}


# name -> 实现
BUILTINS = {
    "read_file": read_file, "write_file": write_file, "patch": patch,
    "terminal": terminal, "vision_analyze": vision_analyze,
    "web_search": web_search, "web_extract": web_extract, "image_generate": image_generate,
}

# name -> schema
SCHEMAS = {s["name"]: s for s in (
    READ_FILE_SCHEMA, WRITE_FILE_SCHEMA, PATCH_SCHEMA, TERMINAL_SCHEMA,
    VISION_ANALYZE_SCHEMA, WEB_SEARCH_SCHEMA, WEB_EXTRACT_SCHEMA, IMAGE_GENERATE_SCHEMA,
    DELEGATE_TASK_SCHEMA)}


# =============================================================== 内部角色工具白名单
# 这些别名只供内部路由，绝不进入模型 schema 或训练正文。
TOOLSETS = {
    "file":       ["read_file", "write_file", "patch"],
    "terminal":   ["terminal"],
    "vision":     ["vision_analyze"],
    "image_gen":  ["image_generate"],
    "web":        ["web_search", "web_extract"],
    "delegation": ["delegate_task"],
}
BASE_TOOL_NAMES = ["read_file"]        # 基础能力:对所有子 agent 默认并入


def normalize_toolset_names(names):
    """把字符串或数组形式的 toolsets 归一为稳定、去重的名称列表。"""
    if isinstance(names, str):
        import json as _json
        s = names.strip()
        parsed = None
        try:
            v = _json.loads(s)
            parsed = v if isinstance(v, list) else ([v] if isinstance(v, str) else None)
        except Exception:
            parsed = None
        if parsed is None:
            parsed = [t.strip().strip('\'"') for t in s.strip('[]').split(',') if t.strip()]
        names = parsed
    out = []
    for name in (names or []):
        value = str(name).strip()
        if value and value not in out:
            out.append(value)
    return out


def resolve_toolsets(names):
    """toolset 名列表 → 去重展开的 schema 列表。未知名忽略并打 warning。"""
    names = normalize_toolset_names(names)
    seen, out = set(), []
    for n in (names or []):
        ts = TOOLSETS.get(n)
        if ts is None:
            print(f"[resolve_toolsets] 未知 toolset:{n!r},忽略", flush=True)
            continue
        for tool in ts:
            if tool not in seen:
                seen.add(tool)
                out.append(SCHEMAS[tool])
    return out


def dispatch(agent, name, args):
    """按名字找工具——先查 agent 专属的 extra_tools(如 delegate_task),再查 BUILTINS。"""
    required_role_card = _read_key(
        getattr(agent, "_required_role_card_path", "")
    )
    if required_role_card and not getattr(
        agent, "_required_role_card_complete", False
    ):
        requested = _read_key((args or {}).get("path")) if name == "read_file" else ""
        if name != "read_file" or requested != required_role_card:
            language = str(getattr(agent, "prompt_language", "zh") or "zh").lower()
            if language == "en":
                return (
                    "Read your required role card to EOF before any other action: "
                    + required_role_card
                )
            return "开始任何其他动作前，必须先将唯一角色卡读到末尾：" + required_role_card
    pending_reads = getattr(agent, "_pending_read_continuations", None)
    if isinstance(pending_reads, dict) and pending_reads and not getattr(
            agent, "_finalization_only", False):
        requested = _read_key((args or {}).get("path")) if name == "read_file" else ""
        expected = pending_reads.get(requested)
        try:
            requested_offset = int((args or {}).get("offset") or 1)
        except (TypeError, ValueError):
            requested_offset = 1
        if name != "read_file" or expected is None or requested_offset != int(expected):
            paths = ", ".join(
                f"{path} (offset={offset})"
                for path, offset in sorted(pending_reads.items())
            )
            language = str(getattr(agent, "prompt_language", "zh") or "zh").lower()
            if language == "en":
                return (
                    "A selected file has not been read to EOF. Continue it exactly as "
                    f"reported before taking another action: {paths}"
                )
            return (
                "已选择的文件尚未读到末尾。开始其他动作前，请严格按返回的 offset 续读："
                + paths
            )
    if getattr(agent, "_finalization_only", False):
        role = str(getattr(agent, "_finalization_role", "") or "").lower()
        path = str((args or {}).get("path") or "").replace("\\", "/").lstrip("./")
        command = str((args or {}).get("command") or (args or {}).get("cmd") or "")
        allowed = False
        if role in {"research", "material"}:
            allowed = name in {"write_file", "patch"}
        elif role == "image":
            allowed = (
                (name in {"write_file", "patch"} and path == "assets/catalog.json")
                or (name == "terminal" and bool(re.search(
                    r"\basset-(?:assign|review|finalize)\b", command
                )))
            )
        elif role == "review":
            allowed = name in {"write_file", "patch"} and path == "_trace/review-issues.md"
        # Slide has no canonical closeout artifact.  It must return blocked in text
        # instead of making one last unverified page edit.
        if allowed:
            if name in agent.extra_tools:
                return agent.extra_tools[name](**args)
            fn = BUILTINS.get(name)
            return fn(agent, **args) if fn else f"未知工具 {name}"
        language = str(getattr(agent, "prompt_language", "zh") or "zh").lower()
        if language == "en":
            return (
                f"{name} is unavailable during {role or 'role'} stall finalization. "
                "Use only the permitted closeout artifact, then return the exact structured "
                "contract required by that role."
            )
        return (
            f"{role or '当前角色'} 停滞收口阶段不再允许 {name}。"
            "请只写允许的收口产物，随后返回该角色要求的准确结构化合同。"
        )
    if name in agent.extra_tools:
        return agent.extra_tools[name](**args)
    fn = BUILTINS.get(name)
    if not fn:
        return f"未知工具 {name}"
    return fn(agent, **args)
