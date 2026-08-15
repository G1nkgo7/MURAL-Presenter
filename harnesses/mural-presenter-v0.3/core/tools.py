"""Lean workspace, visual, image, and research tools for the Clean harness.

Each role receives only the tools it normally needs. Model-authored writes are
restricted to canonical source artifacts; generated delivery files and temporary
files belong to deterministic scripts.

工具是自由函数，第一个参数是 agent 上下文对象（提供 ws / safe / read_path / 生图端点 / 计数器）。
"""
from __future__ import annotations

import base64
import hashlib
import io
import inspect
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from html import unescape
from pathlib import Path

import requests

from . import config, model_call

IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_SCRATCH_SUFFIXES = (".new", ".tmp", ".bak", ".orig", ".rej")
_MODEL_OUTPUT_DIRS = {"research", "plan", "slides", "assets", "checks"}
_MODEL_OUTPUT_FILES = {"base.css", "speech.md"}
_LEGACY_GROUPED_SKILL_NAMES = {
    "long-horizon-html-ppt-grouped",
    "long-horizon-html-ppt-grouped-inline-image",
}
_CURRENT_SKILL_BASE = config.SKILL_NAME.removesuffix("-grouped")
_CURRENT_GROUPED_SKILL_NAMES: set[str] = set()
_CURRENT_VARIANT_SKILL_NAMES = {_CURRENT_SKILL_BASE}
_GROUPED_SKILL_NAMES = _LEGACY_GROUPED_SKILL_NAMES | _CURRENT_GROUPED_SKILL_NAMES
_INLINE_IMAGE_SKILL = "long-horizon-html-ppt-grouped-inline-image"
_ROLE_SCRIPT_NAMES = {
    "orchestrator": "orchestrator.py",
    "image": "image.py",
    "slide": "slide.py",
    "review": "review.py",
}
_ROLE_SCRIPT_ACTIONS = {
    "orchestrator": {"validate-plans", "scaffold", "sync-speech", "finalize", "audit"},
    "image": {"register-user", "crop-material", "fetch", "finalize", "inspect", "remove-checkerboard"},
    "slide": {"render", "render-group"},
    "review": {"sync-speech", "finalize"},
}


def _skill_name(agent) -> str:
    return str(getattr(agent, "skill_name", "") or "")


def _is_grouped_skill(agent) -> bool:
    if _skill_name(agent) in _GROUPED_SKILL_NAMES:
        return True
    # plan/deck.md is canonical. Re-read it before using the in-memory hint so
    # an Orchestrator patch cannot leave tool permissions on the old topology.
    workspace = Path(str(getattr(agent, "ws", "") or ""))
    try:
        deck = (workspace / "plan" / "deck.md").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return False
    match = re.search(
        r"(?mi)^\s*-?\s*ownership_topology\s*:\s*(single|grouped)\s*$",
        deck,
    )
    if match:
        agent.ownership_topology = match.group(1).lower()
        return match.group(1).lower() == "grouped"
    topology = str(getattr(agent, "ownership_topology", "") or "").lower()
    return topology == "grouped"


def _uses_legacy_grouped_contract(agent) -> bool:
    return _skill_name(agent) in _LEGACY_GROUPED_SKILL_NAMES


def _is_inline_image_skill(agent) -> bool:
    return _skill_name(agent) == _INLINE_IMAGE_SKILL


def _is_current_variant_skill(agent) -> bool:
    return _skill_name(agent) in _CURRENT_VARIANT_SKILL_NAMES


def _agent_role_script(agent) -> str:
    """Canonical v0.3 Role entry, with a legacy test/edition compatibility fallback."""
    return str(
        getattr(agent, "role_script", "")
        or getattr(agent, "render_script", "")
        or ""
    )


def _grouped_allowed_skill_reads(agent) -> set[str]:
    """Return the exact Skill files relevant to this role.

    This list limits accidental context expansion; it does not force a role to
    read every allowed file. Read-to-EOF applies only after a file is opened.
    """
    skill = _skill_name(agent)
    if skill not in _LEGACY_GROUPED_SKILL_NAMES:
        return set()
    prefix = f"skills/{skill}"
    role = str(getattr(agent, "role", "") or "")
    shared = {
        "orchestrator": {
            f"{prefix}/SKILL.md",
            f"{prefix}/references/visual-direction.md",
            f"{prefix}/references/page-patterns.md",
            f"{prefix}/references/plan-contract.md",
            f"{prefix}/references/materials-and-images.md",
            f"{prefix}/references/charts-and-diagrams.md",
            f"{prefix}/references/fonts-and-type.md",
        },
        "material": {f"{prefix}/roles/material.md"},
        "research": {f"{prefix}/roles/research.md"},
        "image": {
            f"{prefix}/roles/image.md",
            f"{prefix}/references/materials-and-images.md",
        },
        "slide": {
            f"{prefix}/roles/slide.md",
            f"{prefix}/references/html-contract.md",
            f"{prefix}/references/page-patterns.md",
            f"{prefix}/references/pixel-review-rubric.md",
            f"{prefix}/references/charts-and-diagrams.md",
            f"{prefix}/references/fonts-and-type.md",
        },
        "review": {
            f"{prefix}/roles/review.md",
            f"{prefix}/references/pixel-review-rubric.md",
            f"{prefix}/references/html-contract.md",
        },
    }
    allowed = set(shared.get(role, set()))
    if role == "orchestrator" and _is_inline_image_skill(agent):
        allowed.add(f"{prefix}/roles/orchestrator.md")
    if role == "slide" and _is_inline_image_skill(agent):
        allowed.add(f"{prefix}/references/slide-image-routing.md")
    return allowed

# bash 黑名单：网络/安装/提权/破坏性命令一律拒绝（teacher 生成训练数据，杜绝越界副作用）。
_BASH_BLACKLIST = re.compile(
    r"\b(curl|wget|pip|pip3|conda|npm|yarn|apt|apt-get|yum|sudo|ssh|scp|rsync|nc|telnet|"
    r"systemctl|kill|pkill|reboot|shutdown|mkfs|dd|chmod\s+777|chown)\b"
    r"|rm\s+-rf\s+/|>\s*/etc|:\(\)\s*\{")
# bash 允许的首词（跑渲染脚本 / 看产物 / 轻量文件操作 / 只读检索）
# grep 是只读检索：模型在大 HTML 里定位编辑锚点、数元素、查类名时本能就用它，放行省掉每次先撞墙。
_BASH_ALLOW_FIRST = (
    "python", "python3", "ls", "cat", "head", "tail", "find", "echo",
    "wc", "grep", "rg",
)
# bash_relaxed（仅 studio 在线体验置位）额外放行无文件输出参数的只读命令。
# 文件创建、删除、复制、移动与重定向仍由当前 Role 的受控入口负责。
_BASH_ALLOW_FIRST_RELAXED = _BASH_ALLOW_FIRST + (
    "diff", "tr", "cut",
    "printf", "basename", "dirname", "pwd", "cd", "true", "test")
# relaxed 下做路径守卫时：以这些词开头的命令会写/删文件系统，其路径必须落在工作区内。
_BASH_WRITE_CMDS = {"mv", "cp", "rm", "touch", "tee", "mkdir", "sed", "awk"}
# 写类命令（按参数路径写/删文件系统的）。其余写出只能经重定向（单独拦）。
_BASH_WRITE_BY_ARG = {"mv", "cp", "rm", "touch", "mkdir", "tee", "rmdir", "ln", "install", "rsync"}
# 重定向目标（> >>，绝对或相对都抓；含 awk/sed 程序串里的 print > "x"，宁可过拦也不放过）。
_REDIR_RE = re.compile(r">>?\s*['\"]?([^\s'\";|&<>]+)")
# 命令分段：在 ; | & && || 与换行处切，逐段查首词是否写类命令。
# `2>&1` / `1<&0` 是文件描述符重定向，`&>file` 是合并重定向；
# 其中的 `&` 不是新执行器。独立后台执行符仍然作为分段边界。
_SEG_SPLIT_RE = re.compile(r"\|\||&&|[;|\n]|(?<![<>])&(?!>)")


# =============================================================== 工具实现

def _noncanonical_write_error(path: str) -> str | None:
    """Keep model-authored scratch files out of the delivery workspace."""
    normalized = str(path).replace("\\", "/").rstrip("/")
    name = normalized.rsplit("/", 1)[-1].lower()
    if (
        name.startswith("base.css.")
        or name.endswith(_SCRATCH_SUFFIXES)
        or name.endswith("~")
    ):
        return (
            f"写入路径 `{path}` 是临时/旁路文件，不属于正式产物。"
            "请直接用 patch 修改现有 base.css 或目标正式文件；"
            "不要创建 *.new、*.tmp、*.bak、*.orig、*.rej 或 *~。"
        )
    return None


def _grouped_unresolved_asset_pages(agent) -> list[int]:
    """Return assigned needs_bitmap pages whose asset handoff is unfinished."""
    if not _is_grouped_skill(agent) or _is_inline_image_skill(agent):
        return []
    workspace = str(getattr(agent, "ws", "") or "")
    pages = sorted(
        {
            int(page)
            for page in tuple(getattr(agent, "assigned_slide_pages", ()) or ())
            if isinstance(page, int) and page > 0
        }
    )
    if not workspace or not pages:
        return []

    cached = getattr(agent, "_grouped_asset_pending_pages", None)
    if cached is not None:
        return sorted(int(page) for page in cached)

    catalog_path = os.path.join(workspace, "assets", "catalog.md")
    try:
        with open(catalog_path, encoding="utf-8") as handle:
            catalog = handle.read()
    except OSError:
        catalog = ""
    ready: set[int] = set()
    headings = list(re.finditer(r"(?m)^##\s+.+$", catalog))
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(catalog)
        block = catalog[heading.end():end]
        path_match = re.search(r"(?mi)^-\s*path\s*:\s*(\S.+?)\s*$", block)
        slides_match = re.search(r"(?mi)^-\s*slides\s*:\s*(.+)$", block)
        if not path_match or not slides_match:
            continue
        relative = path_match.group(1).strip().strip("'\"")
        target = os.path.join(workspace, relative)
        if not os.path.isfile(target) or os.path.getsize(target) <= 0:
            continue
        ready.update(int(value) for value in re.findall(r"\d+", slides_match.group(1)))

    unresolved: list[int] = []
    for page in pages:
        plan_path = os.path.join(workspace, "plan", f"slide_{page:02d}.md")
        try:
            with open(plan_path, encoding="utf-8") as handle:
                plan = handle.read()
        except OSError:
            continue
        match = re.search(r"(?mi)^-\s*needs_bitmap\s*:\s*(true|false)\s*$", plan)
        if not match or match.group(1).lower() != "true":
            continue
        if page in ready:
            continue
        unresolved.append(page)
    agent._grouped_asset_pending_pages = tuple(unresolved)
    return unresolved


def _model_write_error(agent, path: str) -> str | None:
    """Enforce the model-facing output allowlist and narrow role ownership."""
    normalized = str(path).replace("\\", "/").strip()
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if not normalized or normalized.startswith("/") or ".." in parts:
        return f"写入路径 `{path}` 不是规范的工作区相对路径。"
    normalized = "/".join(parts)
    top = parts[0] if parts else ""
    if normalized not in _MODEL_OUTPUT_FILES and top not in _MODEL_OUTPUT_DIRS:
        return (
            f"写入路径 `{path}` 不在模型正式产物白名单内。"
            "模型只能写 research/、plan/、slides/、assets/、checks/、"
            "base.css 或 speech.md；present.html、renders/、"
            "_trace/ 与 tmp/ 由受控脚本持有。"
        )

    role = str(getattr(agent, "role", "") or "")
    label = str(getattr(agent, "label", "") or "")
    inline_image_skill = _is_inline_image_skill(agent)
    if (
        role == "orchestrator"
        and _is_current_variant_skill(agent)
        and normalized == "plan/plan-batch.json"
    ):
        return (
            "plan-batch.json 是 Harness 内部临时文件；不要手写嵌套 JSON。"
            "请调用结构化 write_plan_batch 工具。"
        )
    if role == "material" and normalized != "research/material.md":
        return "Material 只能写 research/material.md。"
    if role == "research" and normalized != "research/knowledge-brief.md":
        return "Research 只能写唯一正式交接 research/knowledge-brief.md。"
    if (
        (_uses_legacy_grouped_contract(agent) or _is_current_variant_skill(agent))
        and normalized == "research/knowledge-brief.md"
        and role != "research"
    ):
        return (
            "research/knowledge-brief.md 由 Research 独占。"
            "请委派 Research 并传入原始 query；Orchestrator 不得代写证据简报。"
        )
    if role == "image" and normalized != "assets/catalog.md":
        return "Image 只能写唯一素材登记 `assets/catalog.md`。"
    if (
        (_uses_legacy_grouped_contract(agent) or _is_current_variant_skill(agent))
        and role == "orchestrator"
        and top == "slides"
        and not bool(getattr(agent, "revision_mode", False))
    ):
        return (
            "Orchestrator 不直接改写 Slide HTML。"
            "骨架合同错误应停止页面生产并修正计划后重新 scaffold；页面内容由所选 Single/Grouped "
            "Slide 所有权单元完成，最终像素修复由 Review 负责。"
        )
    if (
        role == "orchestrator"
        and bool(getattr(agent, "revision_mode", False))
        and _is_current_variant_skill(agent)
        and top == "slides"
    ):
        return (
            "v0.3 编辑路由不允许 Orchestrator 直接修改 Slide HTML。"
            "简单编辑交给唯一 `Review: mode=simple_edit`；复杂编辑先写 "
            "plan/revision-impact.md，再把受影响页面按当前 Single/Grouped 所有权委派。"
        )
    if role == "slide":
        assigned_pages = tuple(getattr(agent, "assigned_slide_pages", ()) or ())
        allowed = {
            f"slides/slide_{int(page):02d}.html"
            for page in assigned_pages
            if isinstance(page, int) and page > 0
        }
        if not allowed and re.fullmatch(r"slide_\d+", label):
            allowed.add(f"slides/{label}.html")
        group_id = str(getattr(agent, "slide_group_id", "") or "").strip()
        group_catalog = f"assets/catalog-{group_id}.md" if group_id else ""
        if inline_image_skill and normalized == group_catalog:
            allowed.add(normalized)
        if normalized not in allowed:
            if normalized == "assets/catalog.md" or normalized.startswith("assets/"):
                if inline_image_skill:
                    return (
                        "Inline-image SlideGroup 只能写自己唯一的 "
                        f"`{group_catalog or 'assets/catalog-GROUP.md'}`；"
                        "生成图片由 image_generate 落盘，真实图由 "
                        "assets-resolve-group 下载。"
                    )
                return (
                    "Slide 不得写 Image 持有的 catalog 或图片资产。"
                    "若本组素材尚未解析，停止页面工作并返回 asset_pending。"
                )
            expected = ", ".join(f"`{path}`" for path in sorted(allowed))
            return (
                "Slide 只能写自己分配的正式页面集合"
                + (f"：{expected}。" if expected else " `slides/slide_NN.html`。")
            )
        unresolved_assets = _grouped_unresolved_asset_pages(agent)
        if unresolved_assets:
            pages_text = ",".join(f"{page:02d}" for page in unresolved_assets)
            return (
                f"asset_pending：本组 P{pages_text} 的 needs_bitmap 本地素材尚未交付。"
                "停止写页并返回 asset_pending；等 Image 完成 image.py finalize 后再重试本组。"
                "不得在 Slide 内静默改成 SVG 或纯排印。"
            )
        # v0.3 keeps the normal two-inspection authoring budget in the Skill,
        # but does not freeze the source file at the renderer safety ceiling.
        # A later repair Agent must retain one legal way to close a real defect.
    if role == "review" and not (
        normalized in {"base.css", "speech.md", "plan/deck.md"}
        or top == "slides"
        or (
            top == "plan"
            and re.fullmatch(r"plan/slide_\d+\.md", normalized)
        )
    ):
        return (
            "Review 只能修改 slides/、逐页 plan、plan/deck.md、base.css 或 speech.md。"
        )
    return None


def pending_read_error(agent) -> str | None:
    """Return the next required offsets for model-visible reads cut by the tool cap.

    This is transport hygiene, not a presentation rule: once ``read_file`` says
    that its result was truncated, a later write/tool phase must not silently
    assume the omitted tail was read.  Only files the model actually opened are
    tracked; unrelated references are never added implicitly.
    """
    pending = getattr(agent, "_pending_read_continuations", None)
    if not isinstance(pending, dict) or not pending:
        return None
    ordered = sorted(
        (str(path), int(offset))
        for path, offset in pending.items()
        if isinstance(path, str) and str(offset).isdigit()
    )
    if not ordered:
        return None
    shown = "；".join(
        f"`{path}` 从 offset={offset} 续读"
        for path, offset in ordered[:4]
    )
    suffix = f"；另有 {len(ordered) - 4} 个文件" if len(ordered) > 4 else ""
    return (
        "pending_read：read_file 尚有自动截断内容未读完："
        f"{shown}{suffix}。请按提示继续读取，直到结果不再出现 `续读 offset=`；"
        "Harness 在此状态只暴露所需的 read_file 续读；到达 EOF 后会确定性解除。"
    )


def pending_read_requirement(agent) -> tuple[str, int] | None:
    """Return the one deterministic continuation currently exposed to a model."""
    pending = getattr(agent, "_pending_read_continuations", None)
    if not isinstance(pending, dict) or not pending:
        return None
    valid = sorted(
        (str(path), int(offset))
        for path, offset in pending.items()
        if isinstance(path, str) and str(offset).isdigit() and int(offset) > 0
    )
    return valid[0] if valid else None


def _write_policy_error(agent, path: str) -> str | None:
    return (
        pending_read_error(agent)
        or _noncanonical_write_error(path)
        or _model_write_error(agent, path)
    )


def _record_tool_policy_violation(agent, code: str, detail: str) -> str:
    """Record a blocked call as low-priority policy telemetry, not a render fault."""
    item = {
        "severity": "low",
        "code": code,
        "detail": detail,
        "artifact_changed": False,
    }
    violations = getattr(agent, "tool_policy_violations", None)
    if violations is None:
        violations = []
        agent.tool_policy_violations = violations
    violations.append(item)
    return (
        "terminal 工具策略违规（低优先级；未修改任何产物）："
        f"{detail} 请使用当前 Role 的受控入口或正式写入工具。"
    )


def _parse_page_argument(value: str) -> set[int]:
    pages: set[int] = set()
    for raw_part in str(value or "").split(","):
        part = raw_part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            if not start_text.isdigit() or not end_text.isdigit():
                raise ValueError(f"invalid page range {part!r}")
            start, end = int(start_text), int(end_text)
            if start < 1 or end < start:
                raise ValueError(f"invalid page range {part!r}")
            pages.update(range(start, end + 1))
        elif part.isdigit() and int(part) > 0:
            pages.add(int(part))
        else:
            raise ValueError(f"invalid page number {part!r}")
    if not pages:
        raise ValueError("empty page list")
    return pages


def _atomic_write_text(workspace: str, path: str, content: str) -> None:
    """Promote text atomically from the controlled workspace ``tmp/`` directory."""
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    descriptor, temporary = _controlled_mkstemp(
        workspace,
        prefix=f"{os.path.basename(path)}.",
        suffix=".tmp",
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_write_bytes(workspace: str, path: str, content: bytes) -> None:
    """Promote bytes atomically from the controlled workspace ``tmp/``."""
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    descriptor, temporary = _controlled_mkstemp(
        workspace,
        prefix=f"{os.path.basename(path)}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _controlled_mkstemp(workspace: str, **kwargs):
    """Create a scratch file despite concurrent empty-``tmp/`` cleanup.

    Frozen Skill helpers remove an empty controlled scratch directory after an
    atomic promotion.  Under child-agent concurrency that cleanup can land
    between another writer's ``makedirs`` and ``mkstemp``.  Retrying the whole
    mkdir+create operation closes that TOCTOU window without weakening the
    delivery-surface audit or touching an existing artifact.
    """
    temporary_dir = os.path.join(workspace, "tmp")
    for attempt in range(16):
        os.makedirs(temporary_dir, exist_ok=True)
        try:
            return tempfile.mkstemp(dir=temporary_dir, **kwargs)
        except FileNotFoundError:
            if attempt == 15:
                raise


def _review_render_summary(path: str) -> str:
    """Return the Review-relevant portion of a verbose renderer manifest.

    The raw manifest contains every DOM box and can exceed one thousand lines.
    Review needs the deterministic failures and enough page metadata to decide
    which full-resolution PNGs to inspect, not a paginated DOM dump that hides
    its vision tools behind the pending-read state machine.
    """
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    summary = {
        "mode": raw.get("mode"),
        "n_pages": raw.get("n_pages"),
        "console_errors": raw.get("console_errors", []),
        "static_pages": raw.get("static_pages", []),
        "blank_pages": raw.get("blank_pages", []),
        "capture_retries": raw.get("capture_retries", []),
        "capture_recovered_pages": raw.get("capture_recovered_pages", []),
        "rendered_pages": raw.get("rendered_pages", []),
        "reused_pages": raw.get("reused_pages", []),
        "anti_slop_warnings": raw.get("anti_slop_warnings", []),
        "special_page_geometry": raw.get("special_page_geometry", {}),
        "layout_defects": raw.get("layout_defects", {}),
        "pages": [],
    }
    for item in raw.get("pages", []):
        if not isinstance(item, dict):
            continue
        geometry = item.get("geometry")
        if not isinstance(geometry, dict):
            geometry = {}
        text_alerts = []
        for box in geometry.get("text_boxes", []):
            if not isinstance(box, dict):
                continue
            size = box.get("font_size")
            if bool(box.get("overflow_x")) or bool(box.get("overflow_y")) or (
                isinstance(size, (int, float)) and size < 18
            ):
                text_alerts.append(
                    {
                        "text": str(box.get("text") or "")[:120],
                        "font_size": size,
                        "overflow_x": bool(box.get("overflow_x")),
                        "overflow_y": bool(box.get("overflow_y")),
                    }
                )
        image_alerts = []
        for image in geometry.get("images", []):
            if not isinstance(image, dict):
                continue
            natural_w = image.get("natural_width")
            natural_h = image.get("natural_height")
            display_w = image.get("width")
            display_h = image.get("height")
            if (
                not natural_w
                or not natural_h
                or (
                    isinstance(display_w, (int, float))
                    and isinstance(display_h, (int, float))
                    and (display_w > natural_w * 1.5 or display_h > natural_h * 1.5)
                )
            ):
                image_alerts.append(
                    {
                        "src": image.get("src"),
                        "natural": [natural_w, natural_h],
                        "display": [display_w, display_h],
                    }
                )
        summary["pages"].append(
            {
                "page": item.get("page"),
                "png": os.path.basename(str(item.get("png") or "")),
                "blank": bool(item.get("blank")),
                "static": bool(item.get("static")),
                "page_type": geometry.get("page_type"),
                "frame": geometry.get("frame"),
                "page_family": geometry.get("page_family"),
                "canvas_variant": geometry.get("canvas_variant"),
                "text_alerts": text_alerts,
                "image_alerts": image_alerts,
                "errors": geometry.get("errors", []),
                "warnings": geometry.get("warnings", []),
                "layout_defects": geometry.get("layout_defects", []),
            }
        )
    return json.dumps(summary, ensure_ascii=False, indent=2)


def read_file(agent, path, offset=1, limit=500):
    """只读文本（图片用 vision_analyze）。输出带行号 'LINE_NUM|CONTENT'，用 offset/limit 分页。
    可读工作区文件与只读的 skills/ 树；目录则列条目（相当于 ls）。

    大文件防读不全：单次输出过长会在**行边界**截断，并附 '共 N 行 / 续读 offset=M' 提示——
    不会像旧实现那样静默砍断，让模型误以为已读到文件末尾。CAP 取 7800（低于 agent_loop 对
    普通工具结果的 12000 字符上限），保证截断提示本身不会再被外层 loop 砍掉。"""
    normalized = str(path).replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.rstrip("/")
    role = str(getattr(agent, "role", "") or "")
    required = pending_read_requirement(agent)
    if required is not None:
        required_path, required_offset = required
        requested_offset = max(1, int(offset or 1))
        if normalized != required_path or requested_offset != required_offset:
            return (
                "read_file 错误：pending_read_only；当前只允许续读 "
                f"`{required_path}` offset={required_offset}。Harness 已隐藏其他工具，"
                "请原样调用该续读；不得新开文件或提前写入。"
            )
    if _is_current_variant_skill(agent):
        injected_role = (
            f"skills/{getattr(agent, 'skill_name', '')}/roles/{role}.md"
            if role else ""
        )
        if injected_role and normalized == injected_role:
            return (
                "read_file 无需执行：当前 Role 卡已由 Harness 在首次模型调用前完整注入 "
                "system context，不存在分页或未读尾部。"
            )
        if normalized == "_trace" or normalized.startswith("_trace/"):
            allowed_trace_reads = (
                {"_trace/attachment-manifest.json"}
                if role in {"material", "image"}
                else set()
            )
            if normalized not in allowed_trace_reads:
                return (
                    "read_file 错误：trace_internal；_trace/** 是 Harness 运行记录，"
                    "不是 Role 输入，不得读取或分页追踪。运行时合同、重试范围与"
                    "最终检查页已由 Harness 直接注入。"
                )
    if _is_current_variant_skill(agent) and (
        "/scripts/" in f"/{normalized}" or normalized.endswith("/scripts")
    ):
        return (
            "read_file 错误：v0.3 的 scripts/** 是只执行能力面，模型不可读取实现。"
            "请使用当前 Role 卡列出的唯一公开入口；_internal/** 仅供 Harness 与共享实现。"
        )
    if (
        role == "orchestrator"
        and (normalized == "inputs" or normalized.startswith("inputs/"))
        and bool(getattr(agent, "material_required", False))
        and not bool(getattr(agent, "material_completed", False))
    ):
        handoff = (
            "之后委派 Research；Research 写完 research/knowledge-brief.md 后只读取该 brief。"
            if bool(getattr(agent, "research_required", False))
            else "之后只读取 research/material.md。"
        )
        return (
            "read_file 错误：附件尚未完成 Material 交接。Orchestrator 不得直接读取 "
            f"inputs/**；请先委派唯一 Material Agent，{handoff}"
        )
    if (
        role == "orchestrator"
        and normalized == "research/material.md"
        and bool(getattr(agent, "research_required", False))
    ):
        return (
            "read_file 错误：research_route_violation；本任务启用了 Research。"
            "Orchestrator 不得通读 research/material.md；Material 将原始附件完整交给 "
            "Research，Research 压缩为 research/knowledge-brief.md 后，Orchestrator "
            "只读取 brief 与其中的证据索引。"
        )
    if role == "research" and (
        normalized == "inputs" or normalized.startswith("inputs/")
    ):
        return (
            "read_file 错误：Research 不直接重读原始附件或附件派生文件。"
            "请以 research/material.md 作为附件证据真源；解析缺口应由 Material "
            "结构化报告，不能由 Research 绕过交接。"
        )
    uses_legacy_grouped_contract = _uses_legacy_grouped_contract(agent)
    if (
        _is_grouped_skill(agent)
        and getattr(agent, "role", "") == "slide"
        and normalized == "assets/catalog.md"
        and not hasattr(agent, "_grouped_asset_pending_pages")
    ):
        # Freeze the dependency decision for this agent turn.  A group that
        # started before Image completed must return asset_pending and be
        # re-delegated, even if files arrive while it is already authoring from
        # a stale catalog snapshot.
        agent._grouped_asset_pending_pages = tuple(
            _grouped_unresolved_asset_pages(agent)
        )
    if uses_legacy_grouped_contract and normalized.startswith("skills/"):
        allowed_skill_reads = _grouped_allowed_skill_reads(agent)
        if normalized not in allowed_skill_reads:
            role = str(getattr(agent, "role", "") or "当前角色")
            allowed_text = "、".join(
                f"`{path}`" for path in sorted(allowed_skill_reads)
            ) or "角色卡内嵌说明"
            if role == "image" and "/scripts/" in f"/{normalized}":
                return (
                    "read_file 错误：Image 不读取 Skill 脚本实现。"
                    f"本角色可按需读取：{allowed_text}。"
                )
            if role == "slide":
                return (
                    "read_file 错误：Grouped Slide 不探索 Skill 的其他参考、"
                    f"资产或实现文件。本角色可按需读取：{allowed_text}。"
                )
            return (
                f"read_file 错误：Grouped {role} 不读取与本职责无关的 Skill 文件。"
                f"本角色可按需读取：{allowed_text}。"
            )
    if getattr(agent, "role", "") == "slide":
        if "/scripts/" in f"/{normalized}" or normalized.endswith("/scripts"):
            return (
                "read_file 错误：Slide 不读取 Skill 脚本实现。请按当前工具错误修分配页；"
                "若图片依赖未解析，直接返回 asset_pending。"
            )
        if normalized in {"slides", "plan", "."}:
            return (
                "read_file 错误：Slide 不列举全册计划或页面目录；"
                "只读取本组 plan/slide_NN.md 与 slides/slide_NN.html。"
            )
        if uses_legacy_grouped_contract and normalized == "assets":
            return (
                "read_file 错误：Slide 不列举整个 assets 目录；"
                "只读取全局 catalog、本组 catalog，并使用工具返回的精确素材路径。"
            )
        if uses_legacy_grouped_contract and normalized.startswith("assets/catalog-"):
            own_group = str(getattr(agent, "slide_group_id", "") or "").strip()
            own_catalog = f"assets/catalog-{own_group}.md" if own_group else ""
            if normalized != own_catalog:
                return (
                    "read_file 错误：SlideGroup 不能读取其他页组的素材登记；"
                    f"当前只允许 `{own_catalog or 'assets/catalog-GROUP.md'}`。"
                )
        if uses_legacy_grouped_contract and (
            normalized == "research" or normalized.startswith("research/")
        ):
            return (
                "read_file 错误：Slide 不重读 Research 过程产物；"
                "本页可用的事实与来源已经由 Orchestrator 写入逐页计划。"
            )
        assigned_pages = {
            int(page)
            for page in tuple(getattr(agent, "assigned_slide_pages", ()) or ())
            if isinstance(page, int) and page > 0
        }
        page_match = re.fullmatch(r"(?:plan|slides)/slide_(\d+)\.(?:md|html)", normalized)
        if page_match and int(page_match.group(1)) not in assigned_pages:
            return (
                "read_file 错误：Slide 不能读取组外页面；"
                f"当前只分配了 {sorted(assigned_pages)}。"
            )
    if os.path.splitext(path)[1].lower() in IMG_EXT:
        return f"read_file 错误：'{path}' 是图片，请用 vision_analyze 查看。"
    fp = agent.read_path(path)
    if os.path.isdir(fp):
        entries = sorted(os.listdir(fp))
        if not entries:
            return f"{path}/ (空目录)"
        listing = [e + ("/" if os.path.isdir(os.path.join(fp, e)) else "") for e in entries]
        return f"{path}/ 下的条目：\n" + "\n".join(listing)
    if (
        _is_current_variant_skill(agent)
        and role == "review"
        and normalized == "renders/render.json"
        and max(1, int(offset or 1)) == 1
    ):
        try:
            return (
                "[Review compact render summary；原始 DOM box 清单已省略，"
                "请据此选择并用 vision_analyze 打开当前单页 PNG]\n"
                + _review_render_summary(fp)
            )
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            # A malformed manifest should remain visible through the ordinary
            # line-numbered path so Review can report the exact source error.
            pass
    with open(fp, encoding="utf-8") as f:
        all_lines = f.read().splitlines()
    total = len(all_lines)
    start = max(1, int(offset or 1))
    pending = getattr(agent, "_pending_read_continuations", None)
    if not isinstance(pending, dict):
        pending = {}
        agent._pending_read_continuations = pending
    if start > total:
        pending.pop(normalized, None)
        return f"[EOF：`{normalized}` 共 {total} 行；pending_read 已解除]"
    lim = int(limit or 500)
    sel = all_lines[start - 1:start - 1 + lim]
    numbered = "\n".join(f"{start + i}|{ln}" for i, ln in enumerate(sel))
    CAP = 7800
    if len(numbered) > CAP:
        cut = numbered[:CAP].rsplit("\n", 1)[0]
        last = start + cut.count("\n")
        pending[normalized] = last + 1
        numbered = cut + f"\n\n[… 截断：已显示第 {start}–{last} 行（共 {total} 行）；续读 offset={last + 1}]"
    elif start + len(sel) - 1 >= total:
        pending.pop(normalized, None)
        if numbered:
            numbered += f"\n\n[EOF：已读至第 {total} 行；pending_read 已解除]"
    return numbered


def _slide_root_contract_error(agent, normalized: str, fp: str, content: str) -> str | None:
    """Keep scaffold-owned root metadata immutable before a render is attempted."""
    if not (
        _is_current_variant_skill(agent)
        and getattr(agent, "role", "") in {"slide", "review"}
        and re.fullmatch(r"slides/slide_\d+\.html", normalized)
        and os.path.isfile(fp)
    ):
        return None
    try:
        existing = open(fp, encoding="utf-8").read()
    except OSError:
        return None
    old_root = re.search(r"<section\b[^>]*>", existing, flags=re.I)
    new_roots = re.findall(r"<section\b[^>]*>", str(content or ""), flags=re.I)
    if not old_root or len(new_roots) != 1 or len(re.findall(r"</section\s*>", str(content or ""), re.I)) != 1:
        return (
            "slide_contract：必须保留骨架唯一的根 `<section class=\"slide\">...</section>`；"
            "请只填充 markers 内正文，或复制原 opening tag 后再重写内容"
        )
    old_tag, new_tag = old_root.group(0), new_roots[0]
    new_class = re.search(r"\bclass=[\"']([^\"']*)[\"']", new_tag, flags=re.I)
    if not new_class or "slide" not in new_class.group(1).split():
        return "slide_contract：根 section 必须保留 class `slide`。"
    locked = (
        "id",
        "data-slide",
        "data-page-type",
        "data-page-family",
        "data-frame",
        "data-canvas-variant",
        "data-special-layout",
    )
    changed: list[str] = []
    for name in locked:
        old_match = re.search(
            rf"\b{re.escape(name)}=[\"']([^\"']*)[\"']", old_tag, flags=re.I
        )
        if not old_match:
            continue
        new_match = re.search(
            rf"\b{re.escape(name)}=[\"']([^\"']*)[\"']", new_tag, flags=re.I
        )
        if not new_match or new_match.group(1) != old_match.group(1):
            changed.append(f"{name}={old_match.group(1)!r}")
    if changed:
        return (
            "slide_contract：不得删除或改写 scaffold-owned 根属性："
            + ", ".join(changed)
            + "。保留原 opening tag，只修改 fill markers 内内容和本页限定 CSS。"
        )
    return None


def write_file(agent, path, content):
    normalized = str(path or "").replace("\\", "/").lstrip("./")
    if (
        _is_current_variant_skill(agent)
        and getattr(agent, "role", "") == "research"
        and normalized == "research/knowledge-brief.md"
        and len(str(content or "")) > 10000
    ):
        return (
            "write_file 错误[knowledge_brief_too_long]："
            f"当前 {len(str(content or ''))} 字符，最多 10000 字符。"
            "请压缩为证据账本：每条事实只出现一次，删除检索过程、候选页表、"
            "重复来源与叙事性复述；保留实体消歧、关键事实/数字、边界、视觉线索和 URL。"
        )
    if (
        getattr(agent, "role", "") == "orchestrator"
        and _is_current_variant_skill(agent)
        and not bool(getattr(agent, "revision_mode", False))
        and re.fullmatch(r"plan/slide_\d+\.md", normalized)
    ):
        deck_path = os.path.join(agent.ws, "plan", "deck.md")
        expected = 0
        try:
            deck_text = open(deck_path, encoding="utf-8").read()
            match = re.search(
                r"(?mi)^\s*-\s*page_count\s*:\s*(\d+)\s*$", deck_text
            )
            expected = int(match.group(1)) if match else 0
        except OSError:
            pass
        existing = {
            int(match.group(1))
            for name in os.listdir(os.path.join(agent.ws, "plan"))
            if (match := re.fullmatch(r"slide_(\d+)\.md", name))
            and os.path.getsize(os.path.join(agent.ws, "plan", name)) > 0
        }
        target = int(re.search(r"(\d+)", normalized).group(1))
        missing = set(range(1, expected + 1)).difference(existing) if expected else set()
        if missing != {target}:
            return (
                "write_file 错误：v0.3 首次规划不得逐页串行写 plan/slide_NN.md。"
                "请调用 write_plan_batch；一批放连续 2–6 页，8 页 Deck 用 2 批。"
                "若这些页已经存在但合同需要整体纠正，请在 scaffold/页面生产前调用 "
                "write_plan_batch 并设置 replace_existing=true；"
                "只有全册最后一个缺页可单独恢复。"
            )
    error = _write_policy_error(agent, path)
    if error:
        return f"write_file 错误：{error}"
    fp = agent.safe(path)
    contract_error = _slide_root_contract_error(agent, normalized, fp, content)
    if contract_error:
        return f"write_file 错误：{contract_error}"
    _atomic_write_text(agent.ws, fp, content)
    return f"已写入 {len(content.encode())} 字节到 {path}"


def write_plan_batch(agent, files, replace_existing=False):
    """Serialize and apply 2-6 canonical slide plans without model-side JSON escaping."""
    if getattr(agent, "role", "") != "orchestrator":
        return "write_plan_batch 错误：只有 Orchestrator 可以写逐页计划批次。"
    if bool(getattr(agent, "revision_mode", False)):
        return "write_plan_batch 错误：编辑模式请按影响图 patch 现有逐页计划。"
    if not isinstance(replace_existing, bool):
        return "write_plan_batch 错误[invalid_mode]：replace_existing 必须是布尔值。"
    if replace_existing:
        delegated = {
            str(role).lower()
            for role in (getattr(agent, "delegated_roles", set()) or set())
        }
        slides_dir = Path(agent.ws) / "slides"
        produced_slides = list(slides_dir.glob("slide_*.html")) if slides_dir.is_dir() else []
        if "slide" in delegated or produced_slides:
            return (
                "write_plan_batch 错误[production_started]：页面生产或 scaffold 已开始，"
                "不得批量覆盖 canonical plans；请停止改拓扑，并按影响范围 patch 后走修复路由。"
            )
    if isinstance(files, str):
        # A few OpenAI-compatible transports stringify an otherwise valid
        # array argument even though the public schema declares an array.
        # Decode that envelope once at the adapter edge; the model must never
        # rebuild a giant JSON-inside-JSON manifest merely to recover.
        try:
            files = json.loads(files)
        except json.JSONDecodeError as exc:
            return (
                "write_plan_batch 错误[invalid_json_string]：files 被传成字符串且"
                f"无法解码（line={exc.lineno}, column={exc.colno}）。"
                "请直接按工具 schema 传数组，不要手工转义 Markdown。"
            )
    if not isinstance(files, list):
        return "write_plan_batch 错误[invalid_type]：files 必须是数组。"
    if len(files) < 2:
        return (
            f"write_plan_batch 错误[too_few]：收到 {len(files)} 页，批量接口至少需要 2 页。"
            "若全册只剩最后 1 个缺页，请对该 plan/slide_NN.md 使用 write_file；"
            "否则继续补齐连续页面后再调用。"
        )
    if len(files) > 6:
        return (
            f"write_plan_batch 错误[too_many]：收到 {len(files)} 页，上限为 6 页。"
            "请按连续页码拆成多个 2–6 页批次。"
        )
    normalized: list[dict[str, str]] = []
    numbers: list[int] = []
    for index, item in enumerate(files, start=1):
        if not isinstance(item, dict):
            return f"write_plan_batch 错误：files[{index}] 必须是对象。"
        path = str(item.get("path") or "").replace("\\", "/").lstrip("./")
        content = item.get("content")
        match = re.fullmatch(r"plan/slide_(\d{2})\.md", path)
        if not match or not isinstance(content, str) or not content.strip():
            return (
                f"write_plan_batch 错误：files[{index}] 需要规范 path "
                "plan/slide_NN.md 和非空 content。"
            )
        numbers.append(int(match.group(1)))
        normalized.append({"path": path, "content": content})
    duplicates = sorted(number for number in set(numbers) if numbers.count(number) > 1)
    if duplicates:
        return (
            "write_plan_batch 错误[duplicate]：页码重复 "
            + ",".join(f"{number:02d}" for number in duplicates)
            + "；每页只能出现一次。"
        )
    if numbers != sorted(numbers):
        return (
            "write_plan_batch 错误[out_of_order]：页码必须严格升序；收到 "
            + ",".join(f"{number:02d}" for number in numbers)
            + "。"
        )
    expected_numbers = list(range(numbers[0], numbers[0] + len(numbers)))
    if numbers != expected_numbers:
        missing = sorted(set(expected_numbers).difference(numbers))
        return (
            "write_plan_batch 错误[non_contiguous]：批次页码不连续；收到 "
            + ",".join(f"{number:02d}" for number in numbers)
            + ("，缺少 " + ",".join(f"{number:02d}" for number in missing) if missing else "")
            + "。请拆成连续批次。"
        )
    manifest = agent.safe("plan/plan-batch.json")
    _atomic_write_text(
        agent.ws,
        manifest,
        json.dumps(
            {
                "files": normalized,
                "replace_existing": replace_existing,
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
    )
    role_script = Path(_agent_role_script(agent))
    bootstrap = role_script.parent / "_internal" / "bootstrap.py"
    script = agent.safe(str(bootstrap))
    try:
        completed = subprocess.run(
            [sys.executable, script, "apply-plan-batch", "."],
            cwd=agent.ws,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    finally:
        # plan-batch.json is a transient transport manifest, never a canonical
        # artifact. Remove it after success, validation failure, or timeout so
        # a recoverable batch error cannot poison final workspace audit.
        Path(manifest).unlink(missing_ok=True)
    output = "\n".join(part for part in (completed.stdout.strip(), completed.stderr.strip()) if part)
    if completed.returncode != 0:
        return f"write_plan_batch 错误：apply-plan-batch 失败\n{output[-4000:]}"
    return output or f"status:PASS\nwritten:{len(normalized)}"


def patch(
    agent,
    mode="replace",
    path=None,
    old_string=None,
    new_string=None,
    replace_all=False,
):
    """Hermes ``patch`` with the exact-replace subset used by this pipeline."""
    if mode != "replace":
        return "patch 错误：只支持 mode='replace'"
    if not isinstance(path, str) or not path:
        return "patch 错误：缺少 path"
    if not isinstance(old_string, str):
        return "patch 错误：缺少 old_string"
    if not isinstance(new_string, str):
        return "patch 错误：缺少 new_string"
    error = _write_policy_error(agent, path)
    if error:
        return f"patch 错误：{error}"
    fp = agent.safe(path)
    with open(fp, encoding="utf-8") as f:
        s = f.read()
    n = s.count(old_string)
    if n == 0:
        if new_string and new_string in s:
            return (
                f"已处于目标状态：{path} 已包含 new_string；"
                "不要重复 patch，继续下一步"
            )
        return (
            f"patch 错误：在 {path} 里找不到 old_string。"
            "读取相关短片段后最多再试一次；不要重复相同 patch"
        )
    if n > 1 and not replace_all:
        return (
            f"patch 错误：old_string 出现了 {n} 次（不唯一）；"
            "确认要全改请传 replace_all=true"
        )
    updated = s.replace(old_string, new_string)
    normalized = str(path or "").replace("\\", "/").lstrip("./")
    contract_error = _slide_root_contract_error(agent, normalized, fp, updated)
    if contract_error:
        return f"patch 错误：{contract_error}"
    _atomic_write_text(agent.ws, fp, updated)
    return f"已编辑 {path}"


def _bash_path_guard(agent, cmd):
    """relaxed 模式下把写/删类命令关在当前对话工作区内。返回 None 放行，或错误字符串。

    思路：任何"会落地到文件系统"的路径——重定向目标(> >>)、写类命令(mv/cp/rm/touch/mkdir/tee/ln…)
    的参数、cd 目标——一律相对 ws 解析后取 realpath，必须落在 ws 内，否则拒绝。relpath 解析+realpath
    自动覆盖三类逃逸：① 绝对路径越界 ② 相对 `..` 上跳 ③ 经 skills 等符号链接穿到训练目录。
    用 ;|&&|| 等分段、逐段查首词，堵住"管道/串联里的写命令"(echo x|tee /etc, ls;rm /x)。
    只读命令(cat/grep/find/sed 不带 -i/awk 不重定向…)不拦——它们顶多读到越界内容，不改文件系统。"""
    ws = os.path.realpath(agent.ws)

    def _inside(p):
        full = p if os.path.isabs(p) else os.path.join(ws, p)
        rp = os.path.realpath(full)
        return rp == ws or rp.startswith(ws + os.sep)

    def _toks(seg):
        try:
            return shlex.split(seg)
        except Exception:  # noqa: BLE001  引号不闭合等 → 退化按空白切
            return seg.split()

    # 1) 所有重定向目标（绝对/相对都抓；含 awk/sed 程序串里的 > "x"）必须在 ws 内
    for target in _REDIR_RE.findall(cmd):
        if target in {"/dev/null", "&1", "&2"}:
            continue
        if not _inside(target):
            return f"terminal 错误：禁止写到工作区外 '{target}'（只能在当前对话工作区内操作）。"

    # 2) 逐段：写类命令的每个路径参数、cd 目标，必须在 ws 内
    for seg in _SEG_SPLIT_RE.split(cmd):
        toks = _toks(seg)
        if not toks:
            continue
        c0 = os.path.basename(toks[0])
        if c0 == "cd":
            for t in toks[1:]:
                if not t.startswith("-") and not _inside(t):
                    return f"terminal 错误：禁止 cd 到工作区外 '{t}'。"
            continue
        is_write = c0 in _BASH_WRITE_BY_ARG or (c0 in ("sed", "perl") and any(
            t == "-i" or t.startswith("-i") for t in toks[1:])) or (
            c0 == "find" and any(f in toks for f in ("-delete", "-exec", "-execdir", "-fprint", "-fprintf")))
        if not is_write:
            continue
        for t in toks[1:]:
            if t.startswith("-"):
                continue                       # 选项，不是路径
            looks_path = ("/" in t) or (".." in t) or os.path.exists(os.path.join(ws, t))
            if looks_path and not _inside(t):
                return (f"terminal 错误：'{c0}' 不能操作工作区外的路径 '{t}'"
                        f"（只能在当前对话工作区内操作）。")
    return None


def _bash_mutation_error(agent, cmd: str) -> str | None:
    """Reject shell-level mutation and arbitrary Python before subprocess starts."""
    if "`" in cmd or "$(" in cmd:
        return "禁止命令替换；确定性命令必须直接写出。"
    allowed_sinks = {"/dev/null", "&1", "&2"}
    for target in _REDIR_RE.findall(cmd):
        if target not in allowed_sinks:
            return f"禁止 shell 重定向写文件 `{target}`。"

    forbidden = {
        "rm", "mv", "cp", "touch", "mkdir", "tee", "rmdir", "ln",
        "install", "rsync",
    }
    for segment in _SEG_SPLIT_RE.split(cmd):
        try:
            tokens = shlex.split(segment)
        except Exception:  # noqa: BLE001
            tokens = segment.split()
        if not tokens:
            continue
        command = os.path.basename(tokens[0])
        if _is_current_variant_skill(agent):
            expected_script = _agent_role_script(agent)
            for index, token in enumerate(tokens):
                normalized_token = str(token).replace("\\", "/").rstrip("/")
                if "/scripts/" not in f"/{normalized_token}" and not normalized_token.endswith("/scripts"):
                    continue
                is_exact_role_execution = (
                    command in {"python", "python3"}
                    and index == 1
                    and normalized_token == expected_script
                )
                if not is_exact_role_execution:
                    return (
                        "v0.3 scripts/** 是只执行能力面：禁止列举、读取或执行其他 Role "
                        "入口与 _internal/**。"
                    )
        if command in forbidden:
            return f"禁止 agent 直接运行 `{command}` 创建、删除、复制或移动文件。"
        if command in {"sed", "perl"} and any(
            token == "-i" or token.startswith("-i") for token in tokens[1:]
        ):
            return f"禁止 `{command} -i` 直接改写文件；使用 patch。"
        if command == "find" and any(
            flag in tokens for flag in (
                "-delete", "-exec", "-execdir", "-fprint", "-fprintf"
            )
        ):
            return "禁止 find 的写入、删除或执行动作。"
        if command in {"python", "python3"}:
            expected_script = _agent_role_script(agent)
            if _is_current_variant_skill(agent):
                role = str(getattr(agent, "role", "") or "")
                allowed_actions = _ROLE_SCRIPT_ACTIONS.get(role, set())
                expected_name = _ROLE_SCRIPT_NAMES.get(role, "")
                if not expected_script or not expected_name:
                    return f"{role or '当前角色'} 没有 terminal 或确定性脚本入口。"
                if len(tokens) < 4 or tokens[1] != expected_script:
                    return (
                        f"{role} 只能运行其冻结入口 `python {expected_script} ACTION .`；"
                        "禁止跨 Role 脚本、_internal/**、python -c 与临时辅助程序。"
                    )
                if os.path.basename(tokens[1]) != expected_name:
                    return f"Role–Script 不匹配：{role} 必须使用 {expected_name}。"
                if tokens[2] not in allowed_actions:
                    return (
                        f"{role} 不允许动作 `{tokens[2]}`；可用动作："
                        + ", ".join(sorted(allowed_actions))
                        + "。"
                    )
                if tokens[3] != ".":
                    return f"{expected_name} 只能以当前工作区 `.` 为 ROOT。"
                continue
            if len(tokens) < 4 or tokens[1] != expected_script:
                script = expected_script or "skills/<current-skill>/scripts/deck.py"
                action = (
                    tokens[2]
                    if len(tokens) > 2 and tokens[2] in {
                        "restore-base", "apply-plan-batch", "validate-plans", "scaffold-from-plans",
                        "repair-contract", "sync-speech", "build", "render", "render-group", "finalize", "clean",
                        "audit", "fetch-images", "assets-resolve-group", "assets-finalize", "inspect-image",
                        "remove-checkerboard", "material-figure", "register-user-image",
                    }
                    else "fetch-images"
                )
                return (
                    "Python 仅可运行当前 Skill 的 deck.py；路径与当前 Skill 不匹配。"
                    f"请使用 `python {script} {action} .`；"
                    "禁止 -c、其他脚本或临时辅助程序。"
                )
            if tokens[2] not in {
                "restore-base", "apply-plan-batch", "validate-plans", "scaffold-from-plans", "repair-contract", "sync-speech",
                "build", "render", "render-group", "finalize", "clean", "audit", "fetch-images",
                "assets-resolve-group", "assets-finalize", "inspect-image", "remove-checkerboard",
                "material-figure", "register-user-image",
            }:
                return f"不允许的 deck.py 动作 `{tokens[2]}`。"
            if tokens[3] != ".":
                return "deck.py 只能以当前工作区 `.` 为 ROOT。"
    return None


def terminal(agent, command, timeout=None):
    """在工作区目录下执行 shell 命令。
    默认仅允许 Clean deck 命令、ls、查看 assets 与只读检索。
    relaxed（仅 studio 在线体验，agent.bash_relaxed=True）额外允许少量只读
    文本检查；任何文件系统变更仍只能通过当前 Role 的受控入口。"""
    if not isinstance(command, str) or not command.strip():
        return "terminal 错误：command 不能为空"
    incomplete_read = pending_read_error(agent)
    if incomplete_read:
        return f"terminal 错误：{incomplete_read}"
    cmd = command.strip()
    if getattr(agent, "role", "") == "slide" and "--review-repair" in cmd:
        return (
            "terminal 错误：--review-repair 只供最终 Review 对已确认硬伤做受控复验；"
            "Slide 到达页面状态线后必须保留最后已验证状态，并报告所有未关闭缺陷。"
        )
    is_managed_slide = (
        getattr(agent, "role", "") == "slide"
        and (
            _uses_legacy_grouped_contract(agent)
            or _is_current_variant_skill(agent)
        )
    )
    if is_managed_slide:
        expected_script = re.escape(_agent_role_script(agent))
        if _is_current_variant_skill(agent):
            grouped_actions = "render-group" if _is_grouped_skill(agent) else "render"
        else:
            grouped_actions = (
                "render-group|repair-contract"
                if _is_grouped_skill(agent)
                else "render|repair-contract"
            )
            if _is_inline_image_skill(agent):
                grouped_actions += "|assets-resolve-group"
        allowed = re.fullmatch(
            rf"python(?:3)?\s+{expected_script}\s+(?:{grouped_actions})\s+\."
            rf"(?:\s+[^;&|<>`]+)?",
            cmd,
        )
        if not allowed:
            safe = re.match(
                rf"(python(?:3)?\s+{expected_script}\s+(?:{grouped_actions})\s+\."
                rf"(?:\s+[^;&|<>`]*)?)",
                cmd,
            )
            retry = re.sub(r"\s+", " ", safe.group(1)).strip() if safe else ""
            if retry:
                return (
                    "terminal 错误：不要重试带 `2>&1`、echo、分号或管道的复合命令。"
                    f"请下一步原样执行 `{retry}`；终端结果已经包含退出状态和错误文本。"
                )
            return (
                "terminal 错误：Slide 的终端只用于当前 Skill 和所选所有权拓扑的 "
                + (
                    "deck.py assets-resolve-group / render / render-group / "
                    "repair-contract。"
                    if _is_inline_image_skill(agent)
                    else (
                        "slide.py render-group。"
                        if _is_grouped_skill(agent)
                        else "slide.py render。"
                    )
                )
                + "读取请用 read_file，页面修改请用 write_file / patch；"
                "不搜索 CSS、脚本或目录。"
            )
    is_managed_orchestrator = (
        getattr(agent, "role", "") == "orchestrator"
        and (
            _uses_legacy_grouped_contract(agent)
            or _is_current_variant_skill(agent)
        )
    )
    if is_managed_orchestrator:
        deck_actions = re.findall(r"deck\.py\s+([a-z-]+)", cmd)
        revision_mode = bool(getattr(agent, "revision_mode", False))
        blocked_actions = {"build"}
        if not revision_mode:
            blocked_actions.update({"render", "render-group"})
        if any(action in blocked_actions for action in deck_actions):
            return (
                "terminal 错误：Orchestrator 首次生成时不做单页或手工整册渲染；"
                "Slide 按所选拓扑渲染，Review 使用受控修复渲染。"
                "续编模式仅允许当前 Orchestrator 对已界定的局部变化使用 render / "
                "render-group；build 仍不允许。"
            )
    if getattr(agent, "role", "") == "slide" and "deck.py" in cmd:
        actions = re.findall(r"deck\.py\s+([a-z-]+)", cmd)
        allowed_slide_actions = {"render", "render-group", "repair-contract"}
        if _is_inline_image_skill(agent):
            allowed_slide_actions.add("assets-resolve-group")
        if not actions or any(
            action not in allowed_slide_actions for action in actions
        ):
            if _is_inline_image_skill(agent):
                return (
                    "terminal 错误：Slide 只使用 deck.py assets-resolve-group / "
                    "render / render-group / repair-contract。不要搜索脚本、CSS 或目录。"
                )
            return (
                "terminal 错误：Slide 只使用 deck.py render / render-group / repair-contract。"
                "不要搜索校验脚本、下载素材或运行其他动作；素材未就绪时返回 asset_pending。"
            )
    mutation_error = _bash_mutation_error(agent, cmd)
    if mutation_error:
        return _record_tool_policy_violation(
            agent,
            "direct-filesystem-mutation",
            mutation_error,
        )
    if getattr(agent, "role", "") == "slide":
        assigned_pages = {
            int(page)
            for page in tuple(getattr(agent, "assigned_slide_pages", ()) or ())
            if isinstance(page, int) and page > 0
        }
        if not assigned_pages:
            assigned_match = re.fullmatch(
                r"slide_(\d+)", str(getattr(agent, "label", ""))
            )
            if assigned_match:
                assigned_pages = {int(assigned_match.group(1))}
        if _is_inline_image_skill(agent) and "deck.py assets-resolve-group" in cmd:
            group_match = re.search(r"--group\s+([a-z0-9][a-z0-9_-]*)", cmd, re.I)
            pages_match = re.search(r"--pages\s+([^\s]+)", cmd)
            actual_group = (
                group_match.group(1).lower().replace("_", "-")
                if group_match
                else ""
            )
            expected_group = str(getattr(agent, "slide_group_id", "") or "")
            try:
                actual_pages = {
                    int(value)
                    for value in re.findall(r"\d+", pages_match.group(1))
                } if pages_match else set()
            except (TypeError, ValueError):
                actual_pages = set()
            if actual_group != expected_group or actual_pages != assigned_pages:
                return (
                    "terminal 错误：assets-resolve-group 只能解析当前完整页组；"
                    f"请使用 --group {expected_group} --pages "
                    + ",".join(f"{page:02d}" for page in sorted(assigned_pages))
                )
        rendered_pages = {
            int(value)
            for value in re.findall(
                r"(?:deck|slide)\.py\s+render\s+\.\s+--page\s+0*(\d+)",
                cmd,
            )
        }
        rendered_pages.update(
            int(value)
            for value in re.findall(
                r"deck\.py\s+repair-contract\s+\.\s+--page\s+0*(\d+)",
                cmd,
            )
        )
        group_match = re.search(
            r"(?:deck|slide)\.py\s+render-group\s+\.\s+.*?--pages\s+([^\s]+)",
            cmd,
        )
        if group_match:
            try:
                rendered_pages.update(_parse_page_argument(group_match.group(1)))
            except ValueError as exc:
                return f"terminal 错误：render-group 页码无效：{exc}"
        if rendered_pages and not rendered_pages.issubset(assigned_pages):
            return (
                "terminal 错误：Slide 只能渲染自己分配的页面 "
                f"{sorted(assigned_pages)}，不能渲染 {sorted(rendered_pages)}。"
            )
    relaxed = bool(getattr(agent, "bash_relaxed", False))
    if _BASH_BLACKLIST.search(cmd):
        return ("terminal 错误：命令含被禁止的操作（网络/安装/提权/破坏性）。terminal 只用于跑 "
                "Clean deck 渲染、ls/查看 assets。")
    allow = _BASH_ALLOW_FIRST_RELAXED if relaxed else _BASH_ALLOW_FIRST
    segment_commands: list[str] = []
    for segment in _SEG_SPLIT_RE.split(cmd):
        try:
            tokens = shlex.split(segment)
        except Exception:  # noqa: BLE001
            tokens = segment.split()
        if tokens:
            segment_commands.append(os.path.basename(tokens[0]))
    invalid_segments = [
        command for command in segment_commands if command not in allow
    ]
    if invalid_segments:
        return _record_tool_policy_violation(
            agent,
            "unapproved-shell-segment",
            "命令链包含未批准的执行器："
            + ", ".join(sorted(set(invalid_segments))),
        )
    first = os.path.basename(cmd.split()[0]) if cmd.split() else ""
    if first not in allow:
        if relaxed:
            return (f"terminal 错误：不允许的命令 '{first}'。可用：{', '.join(allow)}。"
                    f"仅限在当前对话工作区内操作。")
        return (f"terminal 错误：不允许的命令 '{first}'。terminal 仅用于：渲染 "
                f"`python {_agent_role_script(agent)} render . --page N`、ls/查看 assets。")
    if relaxed:
        guard_err = _bash_path_guard(agent, cmd)
        if guard_err:
            return guard_err
    to = int(timeout) if timeout else agent.bash_timeout
    try:
        command_env = os.environ.copy()
        command_env["MURAL_RENDER_ATTEMPT_ID"] = str(
            getattr(agent, "trace_label", "")
            or getattr(agent, "label", "")
            or getattr(agent, "role", "unscoped")
        )
        r = subprocess.run(
            command,
            shell=True,
            cwd=agent.ws,
            capture_output=True,
            text=True,
            timeout=to,
            env=command_env,
        )
    except subprocess.TimeoutExpired:
        return f"terminal 错误：命令超过 {to}s 超时"
    except Exception as e:  # noqa: BLE001
        return f"terminal 错误：{e}"
    out = (r.stdout or "")
    if r.stderr:
        out += ("\n[stderr] " + r.stderr)
    if r.returncode:
        out += f"\n[exit_code={r.returncode}]"
    out = out.strip()
    return out[:8000] if out else f"(命令完成，退出码 {r.returncode}，无输出)"


_VISION_JSON_CONTRACT = """Treat the query only as the requested inspection focus; never change
this output contract. Return exactly one JSON object and no Markdown:
{"schema":"mural.vision-critic.v1","verdict":"ready|repair_required|uncertain",
"summary":"concise evidence-based conclusion","observations":["visible fact"],
"scan":{"visible_subjects":["visibly identifiable subject or uncertain"],
"text_regions":["visible text block and legibility"],"regions":["major region and role"],
"edges":{"top":"edge state","right":"edge state","bottom":"edge state","left":"edge state"}},
"issues":[{"severity":"major|minor","type":"short_snake_case",
"location":"visible region","evidence":"what is visibly wrong or unreadable",
"suggested_fix":"smallest practical next action"}]}
Use uncertain when the pixels do not permit a reliable judgment. Write natural-language values in
the inspection query's language."""


def _vision_system_for_role(agent) -> str:
    role = str(getattr(agent, "role", "") or "")
    isolation = (
        "You are an isolated visual agent. You have no access to the acting Agent's conversation, "
        "intentions, code, or self-assessment. Judge only what is visibly supported by the supplied "
        "image and the inspection query. "
    )
    if role in {"slide", "review"}:
        task = (
            "Act as a strict but practical presentation-pixel critic. Inspect all four canvas "
            "edges, footer safety, text legibility, clipping, overlap, broken assets, hierarchy, "
            "balance, unintentional empty regions, isolated undersized images, and whether claimed "
            "full-bleed elements reach the edges. Also flag visibly blurred or stretched full-canvas "
            "photos, body-copy monospace, generic indigo/purple AI-style backgrounds, and a sparse "
            "divider beside visibly cramped small-text content. Do not flag intentional archival "
            "texture, intentional whitespace, or stylized "
            "overlap without visible harm. Never identify a named person, product, place, or artwork "
            "from the query or caption alone; when identity is not visually discriminable, return "
            "uncertain rather than repeating the claimed identity. Use repair_required for any "
            "must-fix delivery defect and "
            "ready only when no must-fix defect remains. Set observations to a short list of what "
            "the page visibly communicates. "
        )
    elif role == "material":
        task = (
            "Act as a faithful attachment-evidence reader. Extract only visible text, numbers, "
            "labels, objects, spatial relationships, and uncertainty requested by the query. Never "
            "invent obscured content. Use ready when the requested evidence is readable, uncertain "
            "when it is not, and repair_required only when a new crop/render is needed. Put extracted "
            "evidence in observations. "
        )
    elif role == "image":
        task = (
            "Act as an image-asset critic. Check identity, relevance, crop safety, resolution, "
            "watermarks, fake text, visual corruption, and the requested use. Put visible asset "
            "facts in observations; use repair_required when the asset should be replaced or recropped. "
        )
    else:
        task = (
            "Act as a style-reference analyst. Describe only visible composition, grid, typography "
            "roles, color relationships, imagery, texture, density, and recurring motifs. Do not "
            "treat the image as factual content evidence. Put reusable style observations in "
            "observations and use uncertain when a requested property is not visible. "
        )
    return isolation + task + _VISION_JSON_CONTRACT


def _vision_critic_query(value: str) -> str:
    query = str(value or "").strip()
    focus = query or "No additional focus was requested."
    return (
        "FIXED OPEN SCAN — complete this before the requested focus: inventory every visibly "
        "identifiable subject without using captions as identity evidence; list visible text "
        "regions and whether each is legible; describe the major canvas regions; inspect top, "
        "right, bottom, and left edges; then check clipping, collision, overlap, small text, "
        "broken images, excessive black masks, low-texture dark areas, hierarchy, and balance. "
        "Only after that scan, evaluate the page contract and this additional focus:\n" + focus
    )


def _normalize_vision_critic_result(raw: str) -> dict:
    text = str(raw or "").strip()
    candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
    parsed = None
    try:
        parsed = json.loads(candidate)
    except (TypeError, ValueError):
        start, end = candidate.find("{"), candidate.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(candidate[start:end + 1])
            except (TypeError, ValueError):
                parsed = None
    if not isinstance(parsed, dict):
        ready = bool(re.search(
            r"(?:no|没有|未发现).{0,16}(?:must[-_ ]?fix|必须修复|明显问题|defect)|"
            r"\b(?:ready|pass)\b",
            text,
            re.I,
        ))
        repair = bool(re.search(
            r"repair_required|must[-_ ]?fix|必须修复|严重|裁切|遮挡|溢出|破图|"
            r"isolated[_ -]?small[_ -]?image|unreadable|clipp(?:ed|ing)|overlap",
            text,
            re.I,
        )) and not ready
        verdict = "repair_required" if repair else "ready" if ready else "uncertain"
        issues = [] if verdict == "ready" else [{
            "severity": "major" if repair else "minor",
            "type": "unstructured_critic_response",
            "location": "unspecified",
            "evidence": text[:1200] or "Vision Critic returned no parseable evidence.",
            "suggested_fix": (
                "Use the visible evidence above for one bounded repair."
                if repair else "Re-run after producing a clear current render."
            ),
        }]
        return {
            "schema": "mural.vision-critic.v1",
            "verdict": verdict,
            "summary": text[:1200] or "Vision Critic response was empty.",
            "observations": [],
            "scan": {},
            "issues": issues,
        }

    aliases = {
        "pass": "ready", "passed": "ready", "ok": "ready",
        "fail": "repair_required", "failed": "repair_required",
        "needs_repair": "repair_required", "needs_fix": "repair_required",
    }
    verdict = str(parsed.get("verdict") or "").strip().lower().replace("-", "_")
    verdict = aliases.get(verdict, verdict)
    if verdict not in {"ready", "repair_required", "uncertain"}:
        verdict = "uncertain"
    normalized_issues: list[dict] = []
    observations: list[str] = []
    scan = parsed.get("scan") if isinstance(parsed.get("scan"), dict) else {}
    normalized_scan = {
        "visible_subjects": [
            str(item).strip()[:600]
            for item in scan.get("visible_subjects", [])[:16]
            if str(item).strip()
        ] if isinstance(scan.get("visible_subjects"), list) else [],
        "text_regions": [
            str(item).strip()[:600]
            for item in scan.get("text_regions", [])[:20]
            if str(item).strip()
        ] if isinstance(scan.get("text_regions"), list) else [],
        "regions": [
            str(item).strip()[:600]
            for item in scan.get("regions", [])[:16]
            if str(item).strip()
        ] if isinstance(scan.get("regions"), list) else [],
        "edges": {
            edge: str((scan.get("edges") or {}).get(edge) or "").strip()[:400]
            for edge in ("top", "right", "bottom", "left")
        } if isinstance(scan.get("edges"), dict) else {},
    }
    raw_observations = parsed.get("observations")
    if isinstance(raw_observations, list):
        observations = [
            str(item).strip()[:1200]
            for item in raw_observations[:16]
            if str(item).strip()
        ]
    raw_issues = parsed.get("issues")
    if isinstance(raw_issues, list):
        for item in raw_issues[:12]:
            if not isinstance(item, dict):
                continue
            severity = str(item.get("severity") or "minor").strip().lower()
            if severity not in {"major", "minor"}:
                severity = "major" if verdict == "repair_required" else "minor"
            normalized_issues.append({
                "severity": severity,
                "type": re.sub(
                    r"[^a-z0-9_]+", "_",
                    str(item.get("type") or "visual_defect").strip().lower(),
                ).strip("_") or "visual_defect",
                "location": str(item.get("location") or "unspecified").strip()[:240],
                "evidence": str(item.get("evidence") or "").strip()[:1200],
                "suggested_fix": str(item.get("suggested_fix") or "").strip()[:1200],
            })
    if verdict == "repair_required" and not normalized_issues:
        verdict = "uncertain"
    if verdict == "ready" and any(
        item.get("severity") == "major" for item in normalized_issues
    ):
        verdict = "repair_required"
    summary = str(parsed.get("summary") or "").strip()[:1200]
    if not summary:
        summary = (
            "No must-fix visual defect was found."
            if verdict == "ready" else "The visual judgment requires attention."
        )
    return {
        "schema": "mural.vision-critic.v1",
        "verdict": verdict,
        "summary": summary,
        "observations": observations,
        "scan": normalized_scan,
        "issues": normalized_issues,
    }


def _external_vision_analysis(
    data: bytes,
    media_type: str,
    query: str,
    system_prompt: str,
) -> str:
    key = (
        os.environ.get("VISION_CRITIC_API_KEY")
        or os.environ.get("VISION_ONESHOT_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
        or ""
    ).strip()
    if not key:
        raise RuntimeError("VISION_CRITIC_API_KEY is missing")
    encoded = base64.b64encode(data).decode()
    response = requests.post(
        f"{config.VISION_CRITIC_BASE_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        json={
            "model": config.VISION_CRITIC_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": [
                    {"type": "text", "text": query},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{media_type};base64,{encoded}",
                        },
                    },
                ]},
            ],
            "max_tokens": config.VISION_CRITIC_MAX_TOKENS,
            "temperature": 0,
        },
        timeout=config.VISION_CRITIC_TIMEOUT_S,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"HTTP {response.status_code}: {response.text[:240]}"
        )
    payload = response.json()
    analysis = str(
        payload.get("choices", [{}])[0].get("message", {}).get("content", "")
        or ""
    ).strip()
    if not analysis:
        raise RuntimeError("external Vision Critic returned empty content")
    return analysis


def _vision_budget_identity(agent, normalized_path: str):
    """Return the persistent Slide inspection unit for a rendered artifact."""
    if getattr(agent, "role", "") != "slide":
        return None
    page_match = re.fullmatch(r"renders/slide_0*(\d+)\.png", normalized_path)
    if page_match:
        page = int(page_match.group(1))
        assigned = tuple(getattr(agent, "assigned_slide_pages", ()) or ())
        if assigned and page not in assigned:
            return None
        return "page", f"{page:02d}", config.SLIDE_PAGE_MAX_INSPECTIONS
    group_match = re.fullmatch(
        r"renders/contact-sheet-group-([a-z0-9-]+)\.png",
        normalized_path,
        flags=re.IGNORECASE,
    )
    if group_match:
        group_id = group_match.group(1).lower()
        expected = str(getattr(agent, "slide_group_id", "") or "").lower()
        if expected and group_id == expected:
            return "group", group_id, config.SLIDE_GROUP_MAX_INSPECTIONS
    return None


def _vision_budget_path(agent, kind: str, unit: str) -> Path:
    safe_unit = re.sub(r"[^a-zA-Z0-9_.-]+", "-", unit).strip("-") or "unknown"
    return Path(agent.ws) / "_trace" / "vision-budgets" / f"{kind}-{safe_unit}.json"


def _load_vision_budget(agent, kind: str, unit: str, limit: int) -> dict:
    path = _vision_budget_path(agent, kind, unit)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        payload = {}
    checks = payload.get("checks") if isinstance(payload, dict) else None
    if not isinstance(checks, list):
        checks = []
    return {
        "schema": "mural.vision-budget.v1",
        "kind": kind,
        "unit": unit,
        "limit": int(limit),
        "checks": [item for item in checks if isinstance(item, dict)],
    }


def _save_vision_budget(agent, payload: dict) -> None:
    path = _vision_budget_path(agent, str(payload["kind"]), str(payload["unit"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def vision_analyze(
    agent,
    image="",
    query="",
    image_url="",
    question="",
    _parent_tool_use_id="",
):
    """Run an isolated visual critic and return text-only structured evidence."""
    if config.VISION_BACKEND == "disabled":
        return (
            "vision_analyze 不可用：当前没有可用的多模态 Vision Critic。"
            "Harness 已阻止把图片误送入同模型并触发 HTTP 400。Material 应使用确定性 "
            "OCR/Office companion，并把无法确认的视觉语义列为 unresolved；制作与 Review "
            "必须改用真正支持图像输入的主模型。"
        )
    path = str(image or image_url or "").strip()
    inspection_query = _vision_critic_query(str(query or question or ""))
    critic_system = _vision_system_for_role(agent)
    if not path:
        return "vision_analyze 错误：image 不能为空"
    normalized_path = str(path).replace("\\", "/").lstrip("./")
    if (
        getattr(agent, "role", "") == "review"
        and not bool(getattr(agent, "review_contact_sheet_inspected", False))
        and normalized_path != "renders/contact-sheet.png"
    ):
        return (
            "vision_analyze 错误：每个 Review 轮次必须先打开当前 "
            "renders/contact-sheet.png，完成整册扫描并形成完整缺陷清单，再检查单页或修改。"
        )
    if (
        (_uses_legacy_grouped_contract(agent) or _is_current_variant_skill(agent))
        and getattr(agent, "role", "") == "orchestrator"
        and not bool(getattr(agent, "revision_mode", False))
        and normalized_path.startswith("renders/")
        and normalized_path.endswith(".png")
    ):
        return (
            "vision_analyze 错误：Orchestrator 不复审最终页面像素。"
            "请把 renders/contact-sheet.png、特殊页联系表与 render.json 交给唯一 Review；"
            "Review 直接修复并给出最终像素结论。"
        )
    assigned_pages = tuple(getattr(agent, "assigned_slide_pages", ()) or ())
    group_match = re.fullmatch(
        r"renders/contact-sheet-group-([a-z0-9-]+)\.png",
        normalized_path,
        flags=re.IGNORECASE,
    )
    if (
        group_match
        and _is_grouped_skill(agent)
        and getattr(agent, "role", "") == "slide"
        and len(assigned_pages) > 1
    ):
        rendered = dict(getattr(agent, "rendered_output_hashes", {}) or {})
        viewed = dict(getattr(agent, "viewed_output_hashes", {}) or {})
        pending_pages = [
            int(page) for page in assigned_pages
            if not rendered.get(int(page))
            or viewed.get(int(page)) != rendered.get(int(page))
        ]
        if pending_pages:
            return (
                "vision_analyze 错误：组内一致性检查必须在每页当前单页 PNG 都完成检查后进行。"
                "请先检查 " + ", ".join(f"P{page:02d}" for page in pending_pages)
                + "，再打开组联系表。"
            )
    fp = agent.read_path(path)
    if not os.path.exists(fp) or os.path.isdir(fp):
        return f"vision_analyze 错误：没有这张图 {path}"
    # A healthy visual loop is render → inspect → patch → render → inspect.
    # Reopening the same unchanged PNG many times adds no evidence and can trap a
    # Review agent in a long loop. A Slide gets one decision per pixel state;
    # Review/Image may ask one follow-up question before the file must change.
    # Compare actual bytes rather than mtime. Re-rendering unchanged HTML rewrites
    # the PNG and changes its timestamp, but does not create new pixel evidence.
    digest = hashlib.sha256()
    with open(fp, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    signature = digest.hexdigest()
    budget_identity = _vision_budget_identity(agent, normalized_path)
    budget = None
    if budget_identity is not None:
        kind, unit, limit = budget_identity
        budget = _load_vision_budget(agent, kind, unit, limit)
        cached = next(
            (
                item for item in budget["checks"]
                if str(item.get("source_sha256") or "") == signature
            ),
            None,
        )
        if cached is not None:
            cached_result = dict(cached.get("vision_result") or {})
            return {
                "vision_analysis": json.dumps(cached_result, ensure_ascii=False, indent=2),
                "vision_result": cached_result,
                "vision_verdict": str(cached_result.get("verdict") or "uncertain"),
                "vision_summary": str(cached_result.get("summary") or ""),
                "path": os.path.relpath(fp, agent.ws),
                "source_sha256": signature,
                "query": inspection_query,
                "vision_backend": str(cached.get("vision_backend") or "persistent-cache"),
                "vision_cached": True,
            }
        if len(budget["checks"]) >= int(limit):
            label = "页面" if kind == "page" else "Slide Group 联系表"
            reason = (
                f"{label} {unit} 已达到 {limit} 个有效新像素检查的生命周期软上限；"
                "重新委派不会重置。保留最后已验证像素并把未关闭问题交给 Review。"
            )
            agent.repair_required_reason = reason
            return f"vision_analyze repair_required：{reason}"
    seen = getattr(agent, "_vision_seen", {})
    previous = seen.get(fp)
    count = previous[1] + 1 if previous and previous[0] == signature else 1
    seen[fp] = (signature, count)
    agent._vision_seen = seen
    unchanged_view_limit = 1 if getattr(agent, "role", "") == "slide" else 2
    if count > unchanged_view_limit:
        return (
            f"vision_analyze 提醒：{path} 自上次渲染后未变化，已经查看 {count - 1} 次。"
            "现有视觉证据没有新增；请据此总结、修改后重渲，或继续检查尚未查看的页面，"
            "不要重复打开同一张未变化的 PNG。"
        )
    try:
        import io

        from PIL import Image
        with Image.open(fp) as im:
            im = im.convert("RGB")
            w, h = im.size
            scale = agent.max_vision_edge / max(w, h)
            if scale < 1:
                im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            data = buf.getvalue()
            media_type = "image/png"
            # Anthropic-compatible endpoints commonly cap the base64 source at
            # 5 MiB. A 1600 px photographic PNG can exceed that even though its
            # dimensions are valid, because base64 adds roughly 33% overhead.
            # Preserve lossless PNG for slides and diagrams; only transcode
            # unusually large inputs, keeping a safe margin below the API cap.
            max_raw_bytes = 3_600_000
            if len(data) > max_raw_bytes:
                media_type = "image/jpeg"
                for quality in (88, 78, 68, 58):
                    buf = io.BytesIO()
                    im.save(buf, format="JPEG", quality=quality, optimize=True)
                    data = buf.getvalue()
                    if len(data) <= max_raw_bytes:
                        break
                while len(data) > max_raw_bytes and max(im.size) > 800:
                    im = im.resize(
                        (max(1, int(im.width * 0.85)), max(1, int(im.height * 0.85))),
                        Image.LANCZOS,
                    )
                    buf = io.BytesIO()
                    im.save(buf, format="JPEG", quality=68, optimize=True)
                    data = buf.getvalue()
    except ImportError:
        with open(fp, "rb") as f:
            data = f.read()
        media_type = "image/png"
    except Exception as e:  # noqa: BLE001
        return (f"vision_analyze 错误：{path} 不是可解析的图片（{type(e).__name__}）。"
                f"只能看 PNG/JPG；deck 页请先渲染："
                f"python {_agent_role_script(agent)} render . --page N")
    if config.VISION_BACKEND == "nova":
        if not _parent_tool_use_id:
            return "vision_analyze 错误：Nova 辅助调用缺少 parent_tool_use_id"
        try:
            analysis = model_call.call_vision_auxiliary(
                agent=agent,
                image_bytes=data,
                media_type=media_type,
                question=inspection_query,
                source_path=os.path.relpath(fp, agent.ws),
                parent_tool_use_id=_parent_tool_use_id,
                system_prompt=critic_system,
            )
        except Exception as exc:  # noqa: BLE001
            if callable(getattr(agent, "log", None)):
                agent.log(
                    "vision_analyze: Nova 辅助后端失败 — "
                    f"{type(exc).__name__}: {str(exc)[:180]}"
                )
            return (
                "vision_analyze 错误：Nova auxiliary model 不可用；"
                f"{type(exc).__name__}: {str(exc)[:180]}"
            )
        backend = "nova_auxiliary_model"
    elif config.VISION_BACKEND == "external_model":
        try:
            analysis = _external_vision_analysis(
                data, media_type, inspection_query, critic_system
            )
        except Exception as exc:  # noqa: BLE001
            if callable(getattr(agent, "log", None)):
                agent.log(
                    "vision_analyze: external Vision Critic 失败 — "
                    f"{type(exc).__name__}: {str(exc)[:180]}"
                )
            return (
                "vision_analyze 错误：external Vision Critic 不可用；"
                f"{type(exc).__name__}: {str(exc)[:180]}"
            )
        backend = f"external_model:{config.VISION_CRITIC_MODEL}"
    elif config.VISION_BACKEND == "same_model_aux":
        try:
            analysis = model_call.call_vision_same_model_auxiliary(
                agent=agent,
                image_bytes=data,
                media_type=media_type,
                query=inspection_query,
                system_prompt=critic_system,
            )
        except Exception as exc:  # noqa: BLE001
            if callable(getattr(agent, "log", None)):
                agent.log(
                    "vision_analyze: same-model Vision Critic 失败 — "
                    f"{type(exc).__name__}: {str(exc)[:180]}"
                )
            return (
                "vision_analyze 错误：same-model Vision Critic 不可用；"
                f"{type(exc).__name__}: {str(exc)[:180]}"
            )
        backend = f"same_model_aux:{getattr(agent, 'model', 'unknown')}"
    else:
        return (
            "vision_analyze 错误：未知 VISION_BACKEND="
            f"{config.VISION_BACKEND!r}"
        )

    result = _normalize_vision_critic_result(analysis)
    if budget is not None:
        budget["checks"].append({
            "source_sha256": signature,
            "path": os.path.relpath(fp, agent.ws),
            "vision_result": result,
            "vision_backend": backend,
            "query": inspection_query,
        })
        _save_vision_budget(agent, budget)
    result_text = json.dumps(result, ensure_ascii=False, indent=2)
    return {
        "vision_analysis": result_text,
        "vision_result": result,
        "vision_verdict": result["verdict"],
        "vision_summary": result["summary"],
        "image_b64": base64.b64encode(data).decode(),
        "media_type": media_type,
        "path": os.path.relpath(fp, agent.ws),
        "source_sha256": signature,
        "query": inspection_query,
        "vision_backend": backend,
    }


def image_generate(agent, prompt, aspect_ratio="landscape"):
    """生成照片/插画类配图（不用于图表），存到 assets/，返回相对路径。"""
    if not getattr(agent, "enable_image_gen", False):
        return "image_generate 不可用（本批次未启用生图）。一切视觉请用 SVG/Canvas/CSS 代码绘制。"
    normalized_ratio = (
        str(aspect_ratio)
        if str(aspect_ratio) in {"landscape", "portrait", "square"}
        else "landscape"
    )
    size = {
        "landscape": "1536x1024",
        "portrait": "1024x1536",
        "square": "1024x1024",
    }[normalized_ratio]
    digest = hashlib.sha256(
        f"{normalized_ratio}\0{prompt}".encode("utf-8")
    ).hexdigest()[:16]
    rel = f"assets/generated-{digest}.png"
    if os.path.isfile(agent.safe(rel)):
        return rel
    try:
        response = requests.post(
            f"{agent.img_base}/images/generations",
            headers={
                "Authorization": f"Bearer {agent.img_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": agent.image_model,
                "prompt": prompt,
                "size": size,
                "n": 1,
            },
            timeout=180,
        )
    except Exception as exc:  # noqa: BLE001
        return (
            "image_generate 错误[transport_error]："
            f"requested_model={agent.image_model}; {type(exc).__name__}: {exc}"
        )
    try:
        d = response.json()
    except Exception:  # noqa: BLE001
        d = {"raw": response.text[:1000]}
    if not response.ok:
        provider_error = d.get("error", d) if isinstance(d, dict) else d
        return (
            "image_generate 错误[provider_unavailable]："
            f"requested_model={agent.image_model}; http_status={response.status_code}; "
            "provider_error="
            + json.dumps(provider_error, ensure_ascii=False)[:1200]
            + "。这是图片服务/路由状态，不得把 needs_bitmap:true 静默改为 false。"
        )
    if "data" not in d or not d["data"]:
        return (
            "image_generate 错误[empty_provider_result]："
            f"requested_model={agent.image_model}; response="
            f"{json.dumps(d, ensure_ascii=False)[:1200]}。"
            "不得把 needs_bitmap:true 静默改为 false。"
        )
    it = d["data"][0]
    if it.get("b64_json"):
        data = base64.b64decode(it["b64_json"])
    elif it.get("url"):
        try:
            data = requests.get(it["url"], timeout=120).content
        except Exception as e:  # noqa: BLE001
            return f"image_generate 错误：下载图片失败 {e}"
    else:
        return "image_generate 错误：没有返回图片"
    _atomic_write_bytes(agent.ws, agent.safe(rel), data)
    return rel


def _external_evidence_gate(agent, *, purpose: str = "factual_research") -> str:
    scope = str(getattr(agent, "evidence_scope", "") or "open_research")
    if scope == "attachment_only":
        return (
            "当前 evidence_scope=attachment_only，Harness 已禁止外部检索。只能使用 "
            "raw_user_query 与 research/material.md；把材料缺口如实写入 brief。"
        )
    if scope != "verify_external":
        return ""
    if purpose == "visual_asset_acquisition" and str(
        getattr(agent, "role", "") or ""
    ).lower() == "image":
        # ``unresolved_items`` limits factual expansion by Research. It must
        # not disable the separate Image responsibility of acquiring a real
        # planned bitmap (for example a named person's portrait).
        return ""
    material_path = Path(str(getattr(agent, "ws", ""))) / "research" / "material.md"
    try:
        material = material_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return (
            "evidence_scope=verify_external 但 research/material.md 不可读；"
            "不能绕过 Material 直接外搜。"
        )
    match = re.search(r"(?mi)^\s*(?:[-*]\s*)?unresolved_items\s*:\s*(.+?)\s*$", material)
    empty_values = {"", "[]", "none", "null", "无", "无。", "n/a", "no"}
    if match and match.group(1).strip().lower() not in empty_values:
        return ""
    return (
        "evidence_scope=verify_external 仅允许核验 Material 结构化列出的 unresolved_items；"
        "当前没有可核验项，因此 Harness 已阻止百科式外部扩展。"
    )


def _search_locale(query: str, language: str = "auto", region: str = "auto") -> tuple[str, str]:
    requested_language = str(language or "auto").strip().lower()
    requested_region = str(region or "auto").strip().lower()
    if requested_language == "auto":
        requested_language = "zh" if re.search(r"[\u3400-\u9fff]", query) else "en"
    hl = "zh-cn" if requested_language.startswith("zh") else "en"
    if requested_region == "auto":
        gl = "cn" if hl.startswith("zh") else "us"
    else:
        gl = requested_region[:2]
    return hl, gl


def _simplify_search_query(query: str) -> str:
    simplified = re.sub(r"[\"'“”‘’]", " ", str(query or ""))
    simplified = re.sub(r"\b(?:site|intitle|inurl|filetype):\S+", " ", simplified, flags=re.I)
    simplified = re.sub(r"[()\[\]{}:;|,+]", " ", simplified)
    tokens = re.findall(r"[\u3400-\u9fff]+|[A-Za-z0-9][A-Za-z0-9._/-]*", simplified)
    candidate = " ".join(tokens[:6]).strip()
    original = re.sub(r"\s+", " ", str(query or "")).strip()
    if candidate.casefold() == original.casefold() and len(tokens) > 3:
        candidate = " ".join(tokens[:-1][:5]).strip()
    return candidate


def web_search(
    agent,
    query,
    limit=5,
    search_type="search",
    language="auto",
    region="auto",
):
    """Search with the configured Serper service."""
    if not bool(getattr(agent, "search_enabled", True)):
        return "web_search 错误：本任务启动前未检测到搜索服务，工具未注入。"
    purpose = (
        "visual_asset_acquisition"
        if str(getattr(agent, "role", "") or "").lower() == "image"
        else "factual_research"
    )
    gate = _external_evidence_gate(agent, purpose=purpose)
    if gate:
        return f"web_search 错误：{gate}"
    key = os.environ.get("SERPER_API_KEY", "").strip()
    if not key:
        return "web_search 错误：SERPER_API_KEY 未配置；Harness 不使用隐式公共站点回退。"
    endpoint = "images" if search_type == "images" else "search"
    serper_base = os.environ.get("SERPER_BASE_URL", "https://google.serper.dev").rstrip("/")
    serper_url = serper_base if serper_base.endswith(f"/{endpoint}") else f"{serper_base}/{endpoint}"
    hl, gl = _search_locale(str(query), str(language), str(region))
    attempts: list[dict] = []
    queries = [str(query).strip()]
    simplified = _simplify_search_query(str(query))
    if simplified and simplified.casefold() != queries[0].casefold():
        queries.append(simplified)
    key_name = "images" if endpoint == "images" else "organic"
    rows: list[dict] = []
    effective_query = queries[0]
    for attempt_no, candidate in enumerate(queries, start=1):
        response = requests.post(
            serper_url,
            headers={"X-API-KEY": key, "Content-Type": "application/json"},
            json={
                "q": candidate,
                "num": max(1, min(int(limit), 10)),
                "hl": hl,
                "gl": gl,
            },
            timeout=45,
        )
        response.raise_for_status()
        data = response.json()
        items = list(data.get(key_name, []) or [])
        attempts.append({"query": candidate, "result_count": len(items)})
        effective_query = candidate
        if items or attempt_no == len(queries):
            break
    for item in items[: int(limit)]:
        if endpoint == "images":
            # Serper exposes the downloadable image and its source page as
            # separate fields.  Returning the page URL as ``url`` discards
            # the only value that fetch-images can consume and encourages
            # agents to guess protected CDN paths.  Keep ``url`` compatible
            # with the keyless Wikimedia image fallback: it is always the
            # image candidate, while ``source`` is the page that hosts it.
            rows.append({
                "title": item.get("title"),
                "url": item.get("imageUrl") or item.get("link"),
                "source": item.get("link") or item.get("source"),
                "source_name": item.get("source"),
                "width": item.get("imageWidth"),
                "height": item.get("imageHeight"),
                "thumbnail_url": item.get("thumbnailUrl"),
            })
        else:
            rows.append({
                "title": item.get("title"),
                "url": item.get("link"),
                "source": item.get("source"),
                "snippet": item.get("snippet"),
            })
    return json.dumps(
        {
            "query": str(query),
            "effective_query": effective_query,
            "locale": {"hl": hl, "gl": gl},
            "retried_with_simplified_query": len(attempts) > 1,
            "attempts": attempts,
            "results": rows,
        },
        ensure_ascii=False,
        indent=2,
    )


_DIRECT_IMAGE_URL_RE = re.compile(
    r"\.(?:avif|bmp|gif|jpe?g|png|svg|webp)(?:$|[?#])|/(?:download|original)/[^/?#]+(?:$|[?#])",
    re.IGNORECASE,
)

_DIRECT_DOCUMENT_URL_RE = re.compile(
    r"\.(?:pdf|docx?|pptx?|xlsx?)(?:$|[?#])",
    re.IGNORECASE,
)


def _image_download_guidance(agent) -> str:
    script = str(
        _agent_role_script(agent)
        or "skills/<current-skill>/scripts/image.py"
    )
    return (
        "web_extract 只读取网页正文，不会保存图片二进制内容。"
        "请把图片直链和来源页写入 `assets/catalog.md` 的 `download` / `source` 字段，"
        f"然后运行 `python {script} fetch .`。"
    )


def _document_extract_guidance() -> str:
    return (
        "web_extract 只读取公开 HTML 网页正文，不下载或解析 PDF/Office 附件。"
        "请使用搜索结果摘要，或改用承载同一事实的公开 HTML 来源页；"
        "不要对同一附件直链重试。"
    )


def web_extract(agent, url, max_chars=12000):
    """Fetch a public webpage and return readable plain text."""
    if not bool(getattr(agent, "search_enabled", True)):
        return "web_extract 错误：本任务启动前未检测到搜索服务，工具未注入。"
    purpose = (
        "visual_asset_acquisition"
        if str(getattr(agent, "role", "") or "").lower() == "image"
        else "factual_research"
    )
    gate = _external_evidence_gate(agent, purpose=purpose)
    if gate:
        return f"web_extract 错误：{gate}"
    if _DIRECT_IMAGE_URL_RE.search(str(url)):
        return _image_download_guidance(agent)
    if _DIRECT_DOCUMENT_URL_RE.search(str(url)):
        return _document_extract_guidance()
    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 CleanPresentationResearch/1.0"},
        timeout=45,
    )
    response.raise_for_status()
    text = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", response.text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(re.sub(r"\s+", " ", text)).strip()
    return text[: max(1000, min(int(max_chars), 30000))]


# =============================================================== 工具 schema

_READ = {
    "name": "read_file",
    "description": ("读文件内容（只读文本；图片用 vision_analyze）。输出带行号 'LINE_NUM|CONTENT'，"
                    "用 offset/limit 分页（默认从第 1 行起、最多 500 行）。可读工作区文件；"
                    "路径是目录时返回条目清单（相当于 ls，"
                    "可查看 assets/）。单次输出过长会在行边界截断并提示总行数与续读 offset——"
                    "读大文件（如填了很多页的 present.html）务必按提示用 offset 续读到读全，"
                    "不要以为第一次就读到了文件末尾。"),
    "input_schema": {"type": "object", "required": ["path"], "properties": {
        "path": {"type": "string", "description": "文件或目录路径（相对工作区）"},
        "offset": {"type": "number", "description": "起始行(1 开始，默认 1)", "default": 1},
        "limit": {"type": "number", "description": "最多读多少行(默认 500)", "default": 500}}},
}
_WRITE = {
    "name": "write_file",
    "description": (
        "把内容原子写入正式文件；不存在则创建，存在则覆盖，自动建父目录。"
        "修改现有 base.css 使用 patch；不要创建 *.new/*.tmp/*.bak 等旁路文件。"
    ),
    "input_schema": {"type": "object", "required": ["path", "content"], "properties": {
        "path": {"type": "string", "description": "文件路径(相对工作区)"},
        "content": {"type": "string", "description": "要写入的内容"}}},
}
_WRITE_PLAN_BATCH = {
    "name": "write_plan_batch",
    "description": (
        "一次写入 2–6 个连续的 plan/slide_NN.md。直接传结构化 files；"
        "Harness 负责 JSON 序列化与 apply-plan-batch，禁止手写 plan-batch.json。"
        "首次批次默认只创建缺页；若计划校验前发现已写页面合同错误，可设置 "
        "replace_existing=true 批量恢复。页面生产开始后禁止覆盖。"
    ),
    "input_schema": {
        "type": "object",
        "required": ["files"],
        "additionalProperties": False,
        "properties": {
            "replace_existing": {
                "type": "boolean",
                "default": False,
                "description": "仅在 scaffold/页面生产前批量覆盖已有逐页计划，用于规划恢复。",
            },
            "files": {
                "type": "array",
                "minItems": 2,
                "maxItems": 6,
                "items": {
                    "type": "object",
                    "required": ["path", "content"],
                    "additionalProperties": False,
                    "properties": {
                        "path": {"type": "string", "pattern": "^plan/slide_[0-9]{2}\\.md$"},
                        "content": {"type": "string", "minLength": 1},
                    },
                },
            }
        },
    },
}
_PATCH = {
    "name": "patch",
    "description": (
        "对正式文本文件做精确替换。只开放 Hermes patch 的 replace 模式；"
        "old_string 必须精确匹配且唯一，除非 replace_all=true。"
    ),
    "input_schema": {"type": "object", "required": ["mode", "path", "old_string", "new_string"], "properties": {
        "mode": {"type": "string", "enum": ["replace"], "default": "replace"},
        "path": {"type": "string", "description": "要编辑的文件路径"},
        "old_string": {"type": "string", "description": "要替换的精确文本"},
        "new_string": {"type": "string", "description": "替换后的文本"},
        "replace_all": {"type": "boolean", "description": "是否替换全部出现(默认 false)"}}},
}
_TERMINAL = {
    "name": "terminal",
    "description": ("在工作区目录下执行前台命令。只允许当前 Role 的冻结薄入口和其公开动作；"
                    "不得读取或执行其他 Role 入口与 _internal/**。"
                    "成功时 stdout 末行是 PNG 路径，告警以 [console]/[static]/[blank]/[nav] 前缀打在前面；"
                    "渲染元数据写 renders/render.json。渲染后用 vision_analyze 看 PNG。"
                    "禁止 rm/mv/cp、重定向、python -c、网络/安装及其他直接文件变更；"
                    "被拦截的调用只记为低优先级工具违规，不是渲染故障。"),
    "input_schema": {"type": "object", "required": ["command"], "properties": {
        "command": {"type": "string", "description": "当前 Role 的受控命令或只读查看"},
        "timeout": {"type": "integer", "minimum": 1}}},
}
_VISION = {
    "name": "vision_analyze",
    "description": (
        "把一张图片和检查 query 交给无历史上下文的独立 Vision Critic；只返回结构化文字"
        "诊断，图片不会进入当前 Agent 对话。"
        "逐页检查时先扫描上、右、下、左四边，再看内容；明确检查最后一行内容与 "
        "footer 安全带、图表/SVG 标签、裁切与 full-bleed 是否真的"
        "到达四个画布边缘，以及溢出/遮挡/占位/破图/豆腐块/对比/意外空区/低清全屏图/"
        "正文 mono/通用靛紫 AI 背景/入场动画"
        "是否到最终态。返回 verdict=ready|repair_required|uncertain 和可见证据。"
        "修复局部问题后仍要对新像素复验。先渲后查，只读代码不算自检。"
    ),
    "input_schema": {
        "type": "object",
        "required": ["image", "query"],
        "additionalProperties": False,
        "properties": {
            "image": {"type": "string", "description": "图片路径，如 renders/slide_03.png"},
            "query": {"type": "string", "minLength": 1,
                      "description": "要由独立像素审校员核验的简短检查清单"},
        },
    },
}
_IMAGE_GEN = {
    "name": "image_generate",
    "description": ("生成照片/插画类配图：适合非特定氛围、原创概念场景，或无法通过"
                    "代码视觉表达的画面。具名真实人物/地点/产品/作品/事件优先搜真图；"
                    "图表、数据、流程、架构、几何装饰、图标和 UI 用 HTML/SVG/CSS。"
                    "prompt 必须带本 deck 的色板词（如 'deep navy and muted gold palette, low saturation, "
                    "editorial photography, no text, no watermark'）。返回 assets/ 下的路径。"),
    "input_schema": {"type": "object", "required": ["prompt"], "properties": {
        "prompt": {"type": "string", "description": "生成描述（务必含色板/质感/no text）"},
        "aspect_ratio": {"type": "string", "enum": ["landscape", "portrait", "square"],
                         "description": "画面方向，默认 landscape"}}},
}
_WEB_SEARCH = {
    "name": "web_search",
    "description": (
        "搜索网页或图片线索。自动按 query 设置 hl/gl，空结果时只做一次简化查询重试；"
        "返回 attempts 与 results。中文任务应混合中文语义查询和必要的英文一手来源查询。"
        "Research 用 search；Image 可用 images。保留来源 URL。"
    ),
    "input_schema": {"type": "object", "required": ["query"], "properties": {
        "query": {"type": "string"},
        "limit": {"type": "integer", "default": 5},
        "search_type": {"type": "string", "enum": ["search", "images"], "default": "search"},
        "language": {"type": "string", "enum": ["auto", "zh", "en"], "default": "auto"},
        "region": {"type": "string", "description": "两字母地区代码；默认按查询语言选择", "default": "auto"}}},
}
_WEB_EXTRACT = {
    "name": "web_extract",
    "description": (
        "读取一个公开 HTML 网页的正文，用于核验搜索结果。不要传图片或 PDF/Office 附件直链；"
        "图片应登记到 assets/catalog.md，再用 Image Role 的 image.py fetch 下载。"
    ),
    "input_schema": {"type": "object", "required": ["url"], "properties": {
        "url": {"type": "string"},
        "max_chars": {"type": "integer", "default": 12000}}},
}
BUILTINS = {
    "read_file": read_file, "write_file": write_file, "write_plan_batch": write_plan_batch,
    "patch": patch, "terminal": terminal,
    "vision_analyze": vision_analyze, "image_generate": image_generate,
    "web_search": web_search, "web_extract": web_extract,
}
_ALL_SCHEMAS = {s["name"]: s for s in (
    _READ, _WRITE, _WRITE_PLAN_BATCH, _PATCH, _TERMINAL, _VISION, _IMAGE_GEN, _WEB_SEARCH,
    _WEB_EXTRACT)}


def _terminal_schema(render_script: str = "", role: str = "") -> dict:
    schema = dict(_TERMINAL)
    examples = {
        "orchestrator": (
            "validate-plans . --expected N；scaffold . --expected N；"
            "sync-speech . --expected N；finalize . --expected N；audit ."
        ),
        "image": (
            "register-user .；crop-material .；fetch .；finalize .；"
            "inspect .；remove-checkerboard ."
        ),
        "slide": "render . --page N；或 render-group . --group GROUP --pages N,N",
        "review": "sync-speech . --expected N；finalize . --expected N",
    }
    script = render_script or f"skills/<current-skill>/scripts/{_ROLE_SCRIPT_NAMES.get(role, 'ROLE.py')}"
    schema["description"] = (
        f"{schema['description']}\n当前角色：{role}。唯一脚本：`{script}`。"
        f"允许动作：{examples.get(role, '无')}。调用格式：`python {script} ACTION . ...`。"
    )
    return schema


def agent_tools(
    role: str,
    enable_image_gen: bool = True,
    enable_web_search: bool = True,
    enable_vision: bool = True,
    render_script: str = "",
    skill_name: str = "",
    revision_mode: bool = False,
    evidence_scope: str = "open_research",
    enable_attachment_vision: bool = False,
) -> list[dict]:
    """Return the small tool surface appropriate for one role."""
    names_by_role = {
        "orchestrator": [
            "read_file", "write_file", "write_plan_batch", "patch", "terminal"
        ],
        "material": ["read_file", "write_file", "vision_analyze"],
        "research": ["read_file", "write_file", "web_search", "web_extract"],
        "image": [
            "read_file", "write_file", "terminal", "vision_analyze",
            "web_search", "web_extract",
        ],
        "slide": ["read_file", "write_file", "patch", "terminal", "vision_analyze"],
        "review": ["read_file", "patch", "terminal", "vision_analyze"],
    }
    names = names_by_role.get(role, names_by_role["orchestrator"]).copy()
    if not enable_vision:
        names = [name for name in names if name != "vision_analyze"]
    if not enable_web_search:
        names = [name for name in names if name not in {"web_search", "web_extract"}]
    if role == "research" and evidence_scope == "attachment_only":
        names = [name for name in names if name not in {"web_search", "web_extract"}]
    if (
        role == "orchestrator"
        and (revision_mode or enable_attachment_vision)
        and skill_name in (_GROUPED_SKILL_NAMES | _CURRENT_VARIANT_SKILL_NAMES)
    ):
        names.append("vision_analyze")
    if role == "slide" and skill_name == _INLINE_IMAGE_SKILL and enable_web_search:
        names.extend(["web_search", "web_extract"])
    if role == "image" and enable_image_gen:
        names.append("image_generate")
    if (
        role == "slide"
        and skill_name == _INLINE_IMAGE_SKILL
        and enable_image_gen
    ):
        names.append("image_generate")
    return [
        _terminal_schema(render_script, role) if name == "terminal" else _ALL_SCHEMAS[name]
        for name in names
    ]


def dispatch(agent, name, args, *, internal_context=None):
    fn = BUILTINS.get(name)
    if not fn:
        return f"未知工具 {name}"
    if not isinstance(args, dict):
        return f"{name} 错误：参数必须是 JSON 对象"
    # 参数校验：缺必填→友好提示而非崩溃；未知参数名（如把 prompt 拼成 prameter）→ 忽略并提示，不崩。
    params = list(inspect.signature(fn).parameters.values())[1:]   # 跳过第一个 agent
    valid = {p.name for p in params if not p.name.startswith("_")}
    required = [
        p.name for p in params
        if p.default is inspect.Parameter.empty and not p.name.startswith("_")
    ]
    missing = [r for r in required if r not in args]
    if missing:
        return (f"{name} 错误：缺少必填参数 {missing}。该工具参数：{sorted(valid)}。补齐后重试。")
    unknown = [k for k in args if k not in valid]
    clean = {k: v for k, v in args.items() if k in valid}
    if name == "vision_analyze" and isinstance(internal_context, dict):
        clean["_parent_tool_use_id"] = str(
            internal_context.get("parent_tool_use_id") or ""
        )
    try:
        res = fn(agent, **clean)
    except Exception as e:  # noqa: BLE001
        return f"{name} 错误：{type(e).__name__}: {e}"
    if unknown and isinstance(res, str):
        return f"{res}（忽略了未知参数 {unknown}，请检查参数名）"
    return res
