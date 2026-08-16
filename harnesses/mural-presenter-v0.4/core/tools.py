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
import threading
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

import requests

from . import config, model_call

IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_SCRATCH_SUFFIXES = (".new", ".tmp", ".bak", ".orig", ".rej")
_MODEL_OUTPUT_DIRS = {"research", "plan", "slides", "assets", "checks"}
_MODEL_OUTPUT_FILES = {"base.css"}
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
    # A single-topology deck may deliberately co-author only its non-contiguous
    # bookends or divider family in one visual-memory unit.  Tool permissions
    # follow the concrete assignment, not just the ordinary content topology.
    assigned = tuple(getattr(agent, "assigned_slide_pages", ()) or ())
    if (
        getattr(agent, "role", "") == "slide"
        and len(assigned) > 1
        and str(getattr(agent, "slide_group_id", "") or "").strip()
    ):
        return True
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


_ARTIFACT_COMPLETION_RETRY_REASONS = {"max_turns", "stalled_repetition"}
_OPERATIONAL_CHILD_RETRY_REASONS_TOOLS = {
    "api_failed", "runtime_timeout", "empty_giveup", "asset_pending",
}


def _outcome_for_page_tools(outcomes: dict, page: int) -> tuple[str, dict | None]:
    """Find the child_outcomes entry covering *page* (single or grouped)."""
    single_label = f"slide_{page:02d}"
    if single_label in outcomes:
        return single_label, outcomes[single_label]
    for label, outcome in outcomes.items():
        if not label.startswith("slide_group_"):
            continue
        if not isinstance(outcome, dict):
            continue
        all_pages: set[int] = set()
        for key in ("completed_pages", "incomplete_pages"):
            for p in (outcome.get(key) or []):
                if isinstance(p, int) and not isinstance(p, bool) and p > 0:
                    all_pages.add(p)
        if not all_pages:
            pages_field = outcome.get("pages")
            if isinstance(pages_field, list):
                all_pages = {
                    int(p) for p in pages_field
                    if isinstance(p, int) and not isinstance(p, bool) and p > 0
                }
        if page in all_pages:
            return label, outcome
    return "", None


def _responsibility_pages_from_outcome_tools(label: str, outcome: dict) -> tuple[int, ...]:
    """Extract responsibility pages from an outcome dict."""
    all_pages: set[int] = set()
    for key in ("completed_pages", "incomplete_pages"):
        for p in (outcome.get(key) or []):
            if isinstance(p, int) and not isinstance(p, bool) and p > 0:
                all_pages.add(p)
    if not all_pages and label.startswith("slide_"):
        match = re.fullmatch(r"slide_(\d+)", label)
        if match:
            all_pages.add(int(match.group(1)))
    return tuple(sorted(all_pages))


def _page_has_terminal_slide_failure_from_outcomes(agent, page: int) -> bool:
    """True when Slide for *page* failed terminally (no retry left).

    Works for both Single (slide_NN) and Grouped (slide_group_*) labels.
    A page is terminal when:
      - child ran (has trace_label), is not ok
      - not eligible for operational retry
      - not eligible for artifact-completion retry (still has missing pixels
        among responsibility pages)
    """
    outcomes = getattr(agent, "child_outcomes", None)
    if not isinstance(outcomes, dict):
        return False
    label, previous = _outcome_for_page_tools(outcomes, page)
    if not isinstance(previous, dict):
        return False
    if bool(previous.get("ok")):
        return False
    if not previous.get("trace_label"):
        return False
    exit_reason = str(previous.get("exit_reason") or "")
    attempt = int(previous.get("attempt") or 1)
    if exit_reason in _OPERATIONAL_CHILD_RETRY_REASONS_TOOLS and attempt < 2:
        return False
    if exit_reason in _ARTIFACT_COMPLETION_RETRY_REASONS and not previous.get("repair_issue") and attempt < 2:
        responsibility_pages = _responsibility_pages_from_outcome_tools(label, previous)
        if responsibility_pages:
            renders = Path(agent.ws) / "renders"
            has_missing = any(
                not (renders / f"slide_{p:02d}.png").is_file()
                for p in responsibility_pages
            )
            if has_missing:
                return False
        elif not previous.get("completed_pages"):
            return False
    return True


def _agent_role_script(agent) -> str:
    """Canonical v0.4 Role entry, with a legacy test/edition compatibility fallback."""
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
    if normalized == "speech.md":
        return (
            "speech.md 是从 plan/slide_NN.md 的 `## 初版口语讲稿` 确定性派生的文件，"
            "任何 Agent 都不得直接编辑。请修改对应逐页计划，再由 Orchestrator 或 Review "
            "运行角色专属的 `sync-speech`。"
        )
    top = parts[0] if parts else ""
    if normalized not in _MODEL_OUTPUT_FILES and top not in _MODEL_OUTPUT_DIRS:
        return (
            f"写入路径 `{path}` 不在模型正式产物白名单内。"
            "模型只能写 research/、plan/、slides/、assets/、checks/、"
            "或 base.css；speech.md、present.html、renders/、"
            "_trace/ 与 tmp/ 由受控脚本持有。"
        )

    role = str(getattr(agent, "role", "") or "")
    label = str(getattr(agent, "label", "") or "")
    inline_image_skill = _is_inline_image_skill(agent)

    pre_slide_media_replan = False
    plan_match = re.fullmatch(r"plan/slide_(\d+)\.md", normalized)
    if plan_match and role == "orchestrator":
        page = int(plan_match.group(1))
        failed_pages = {
            int(value)
            for value in (getattr(agent, "image_failed_pages", ()) or ())
        }
        # A bitmap-dependent Slide that was held back after Image's bounded
        # acquisition failure has not acquired page ownership yet.  Permit one
        # explicit Orchestrator reclassification while no page pixels exist;
        # once a Slide renders, the normal production freeze applies again.
        pre_slide_media_replan = bool(
            page in failed_pages
            and not (Path(agent.ws) / "renders" / f"slide_{page:02d}.png").is_file()
        )
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
        role == "orchestrator"
        and _is_current_variant_skill(agent)
        and top == "assets"
    ):
        return (
            "assets/** 由 Image 独占。Orchestrator 只能在逐页计划中决定图片来源类型，"
            "不得亲自新增、删除或 patch catalog/图片；素材缺口应消费 Image 的"
            "结构化交接，并释放已有可用素材的页面或在像素生产前重分类计划。"
        )
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
        and _is_current_variant_skill(agent)
        and not bool(getattr(agent, "revision_mode", False))
        and re.fullmatch(r"plan/slide_\d+\.md", normalized)
        and "slide" in {
            str(value).lower()
            for value in (getattr(agent, "delegated_roles", set()) or set())
        }
        and not pre_slide_media_replan
    ):
        return (
            "页面生产已经开始，canonical slide plan 与 primary_visual_medium 已冻结。"
            "Orchestrator 不得为绕过实现/渲染错误把 ECharts、位图或其他承诺降级成另一种"
            "媒介，也不得重写页面合同。保留当前计划，把未完成页面与精确报错交给唯一 "
            "Review；若没有任何有效产物则明确失败并在修复 Harness 后重跑。"
        )
    if (
        role == "orchestrator"
        and bool(getattr(agent, "revision_mode", False))
        and _is_current_variant_skill(agent)
        and top == "slides"
    ):
        return (
            "v0.4 编辑路由不允许 Orchestrator 直接修改 Slide HTML。"
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
        if len(assigned_pages) > 1 and re.fullmatch(r"slides/slide_\d+\.html", normalized):
            pass
        unresolved_assets = _grouped_unresolved_asset_pages(agent)
        if unresolved_assets:
            pages_text = ",".join(f"{page:02d}" for page in unresolved_assets)
            return (
                f"asset_pending：本组 P{pages_text} 的 needs_bitmap 本地素材尚未交付。"
                "停止写页并返回 asset_pending；等 Image 完成 image.py finalize 后再重试本组。"
                "不得在 Slide 内静默改成 SVG 或纯排印。"
            )
        # v0.4 keeps the normal two-inspection authoring budget in the Skill,
        # but does not freeze the source file at the renderer safety ceiling.
        # A later repair Agent must retain one legal way to close a real defect.
    if role == "review" and not (
        normalized in {"base.css", "plan/deck.md"}
        or top == "slides"
        or (
            top == "plan"
            and re.fullmatch(r"plan/slide_\d+\.md", normalized)
        )
    ):
        return (
            "Review 只能修改 slides/、逐页 plan、plan/deck.md 或 base.css；"
            "speech.md 必须由 sync-speech 派生。"
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
    special = raw.get("special_page_geometry")
    if not isinstance(special, dict):
        special = {}

    def short_list(value, limit=6):
        return [str(item)[:240] for item in list(value or [])[:limit]]

    def flagged_keys(value):
        if not isinstance(value, dict):
            return []
        return [str(key) for key, item in value.items() if item not in (None, "", [], {})][:20]

    def flagged_details(value, limit=12):
        if not isinstance(value, dict):
            return {}
        return {
            str(key): item
            for key, item in list(value.items())[:limit]
            if item not in (None, "", [], {})
        }

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
        "visual_review_notes": raw.get(
            "visual_review_notes", raw.get("anti_slop_warnings", [])
        ),
        "special_page_geometry": {
            "status": special.get("status"),
            "errors": short_list(special.get("errors")),
            "warnings": short_list(special.get("warnings")),
        },
        "layout_defect_pages": flagged_keys(raw.get("layout_defects", {})),
        "layout_defects": flagged_details(raw.get("layout_defects", {})),
        "typography_flag_pages": flagged_keys(raw.get("typography_flags", {})),
        "typography_flags": flagged_details(raw.get("typography_flags", {})),
        "media_mismatch_pages": flagged_keys(raw.get("media_mismatches", {})),
        "media_mismatches": flagged_details(raw.get("media_mismatches", {})),
        "placeholder_flag_pages": flagged_keys(raw.get("placeholder_flags", {})),
        "placeholder_flags": flagged_details(raw.get("placeholder_flags", {})),
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
            class_name = str(box.get("class_name") or "").lower()
            furniture = any(
                token in class_name
                for token in ("footer", "eyebrow", "page-number", "page-num")
            )
            if bool(box.get("overflow_x")) or bool(box.get("overflow_y")) or (
                isinstance(size, (int, float)) and size < 18 and not furniture
            ):
                text_alerts.append(
                    {
                        "text": str(box.get("text") or "")[:120],
                        "font_size": size,
                        "overflow_x": bool(box.get("overflow_x")),
                        "overflow_y": bool(box.get("overflow_y")),
                    }
                )
                if len(text_alerts) >= 3:
                    break
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
                if len(image_alerts) >= 4:
                    break
        page_summary = {
            "page": item.get("page"),
            "png": os.path.basename(str(item.get("png") or "")),
            "page_type": geometry.get("page_type"),
            "frame": geometry.get("frame"),
            "page_family": geometry.get("page_family"),
        }
        if item.get("blank"):
            page_summary["blank"] = True
        if item.get("static"):
            page_summary["static"] = True
        if text_alerts:
            page_summary["text_alerts"] = text_alerts
        if image_alerts:
            page_summary["image_alerts"] = image_alerts
        errors = short_list(geometry.get("errors"))
        warnings = short_list(geometry.get("warnings"))
        defects = list(geometry.get("layout_defects") or [])[:4]
        if errors:
            page_summary["errors"] = errors
        if warnings:
            page_summary["warnings"] = warnings
        if defects:
            page_summary["layout_defects"] = defects
        summary["pages"].append(page_summary)
    return json.dumps(summary, ensure_ascii=False, separators=(",", ":"))


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
    completed_reads = getattr(agent, "_completed_file_reads", None)
    if not isinstance(completed_reads, set):
        completed_reads = set()
        agent._completed_file_reads = completed_reads
    if (
        _is_current_variant_skill(agent)
        and role == "slide"
        and normalized in completed_reads
        and normalized in {"plan/deck.md", "assets/catalog.md"}
    ):
        assigned_pages = {
            int(page)
            for page in tuple(getattr(agent, "assigned_slide_pages", ()) or ())
            if isinstance(page, int) and not isinstance(page, bool) and page > 0
        }
        required_inputs = {"plan/deck.md", "assets/catalog.md"}
        required_inputs.update(
            f"plan/slide_{page:02d}.md" for page in assigned_pages
        )
        required_inputs.update(
            f"slides/slide_{page:02d}.html" for page in assigned_pages
        )
        # Once a Slide has consumed every immutable production input, another
        # EOF read cannot reveal new evidence.  Close the read surface on the
        # next model turn instead of letting a confused model reread the final
        # catalog line until the generic repetition guard kills useful work.
        if required_inputs.issubset(completed_reads):
            agent._slide_inputs_read_closed = True
            return (
                f"read_file 无需重复：`{normalized}` 已完整读到 EOF，且本责任单元的"
                " deck、逐页计划、HTML 骨架与素材目录均已读取。下一轮 Harness 将隐藏 "
                "read_file；请使用 catalog 中实际存在的 ready 素材继续写 HTML。若计划"
                "期望多张图片而 catalog 只有一张，就放大该图并用排印/图形完成其余信息，"
                "不要继续猜 offset 或等待不存在的素材。"
            )
    if (
        _is_current_variant_skill(agent)
        and role in {"orchestrator", "review"}
        and normalized == "renders/render.json"
    ):
        stale_pending = getattr(agent, "_pending_read_continuations", None)
        if isinstance(stale_pending, dict):
            stale_pending.pop(normalized, None)
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
        # ``pending_read`` is a transport state, not a model-controlled
        # pagination workflow.  Some providers emit ``limit=1`` while trying
        # to obey the exact offset enum, which used to turn a short canonical
        # file into dozens of model turns.  Once continuation is mandatory,
        # consume a useful chunk deterministically; the character cap below
        # still bounds every tool result.
        limit = max(500, int(limit or 500))
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
            "read_file 错误：v0.4 的 scripts/** 是只执行能力面，模型不可读取实现。"
            "请使用当前 Role 卡列出的唯一公开入口；_internal/** 仅供 Harness 与共享实现。"
            "validate-plans/finalize 的报错已经是完整修复合同，直接按其中给出的精确"
            "文件、字段或首行格式修改当前产物，不要读取或搜索校验器。"
            "若 render 报告 Canvas/ECharts 缺少元素或脚本，直接按该错误回执修改当前 "
            "slide HTML，不需要也不得读取校验器实现。"
        )
    if _is_current_variant_skill(agent) and role == "orchestrator":
        if normalized == "base.css":
            return (
                "read_file 无需执行：base.css 由 scaffold/finalize 的共享系统管理，"
                "Orchestrator 只在 plan/deck.md 冻结 Theme Tokens；页面实现由 Slide，"
                "全局修复由 Review 负责。不要分页读取 base.css。"
            )
        if normalized.endswith("/references/layout-patterns.md"):
            return (
                "read_file 无需执行：layout-patterns.md 是 Slide 的实现词汇库。"
                "Orchestrator 只需 page-patterns、creative-direction 与 plan-contract，"
                "不要把具体 DOM/CSS 骨架读入规划上下文。"
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
        and role in {"orchestrator", "review"}
        and normalized == "renders/render.json"
    ):
        try:
            # Review and its parent Orchestrator never need the thousand-line
            # raw DOM inventory. Ignore speculative offsets and clear a stale
            # continuation so one follow-up read cannot hide delegation,
            # Vision, write, or finalize behind the pending-read-only surface.
            stale_pending = getattr(agent, "_pending_read_continuations", None)
            if isinstance(stale_pending, dict):
                stale_pending.pop(normalized, None)
            return (
                f"[{role.title()} compact render summary；原始 DOM box 清单已省略，"
                "所有 offset 均返回本摘要；不要分页读取原始 JSON。"
                "Review 应据此选择并用 vision_analyze 打开当前单页 PNG；"
                "Orchestrator 只消费结构化结论，不重新诊断或重派已完成 Slide]\n"
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
        completed_reads.add(normalized)
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
        completed_reads.add(normalized)
        if numbered:
            numbered += f"\n\n[EOF：已读至第 {total} 行；pending_read 已解除]"
    elif sel:
        # A model may deliberately request fewer lines than remain. Advance
        # the deterministic continuation even when this chunk did not hit the
        # character cap; otherwise pending_read points at the same offset and
        # forces an unrecoverable identical-read loop.
        next_offset = start + len(sel)
        pending[normalized] = next_offset
        numbered += (
            f"\n\n[… 已显示第 {start}–{next_offset - 1} 行（共 {total} 行）；"
            f"续读 offset={next_offset}]"
        )
    return numbered


_HTML_VOID_ELEMENTS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


class _SingleRootSectionParser(HTMLParser):
    """Recognize one top-level section while allowing arbitrary nested sections."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.root_opening: str | None = None
        self.root_closed = False
        self.invalid = False

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[no-untyped-def]
        tag = tag.lower()
        if not self.stack:
            if self.root_opening is not None or self.root_closed or tag != "section":
                self.invalid = True
                return
            self.root_opening = self.get_starttag_text()
        if tag not in _HTML_VOID_ELEMENTS:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs) -> None:  # type: ignore[no-untyped-def]
        # A self-closing top-level section cannot own the authored page body.
        if not self.stack:
            self.invalid = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if not self.stack or self.stack[-1] != tag:
            self.invalid = True
            return
        self.stack.pop()
        if not self.stack:
            self.root_closed = True

    def handle_data(self, data: str) -> None:
        if not self.stack and data.strip():
            self.invalid = True

    def handle_entityref(self, name: str) -> None:
        if not self.stack:
            self.invalid = True

    def handle_charref(self, name: str) -> None:
        if not self.stack:
            self.invalid = True

    def handle_decl(self, decl: str) -> None:
        if not self.stack:
            self.invalid = True

    def unknown_decl(self, data: str) -> None:
        if not self.stack:
            self.invalid = True


def _single_root_section_opening(content: str) -> str | None:
    parser = _SingleRootSectionParser()
    try:
        parser.feed(content)
        parser.close()
    except Exception:
        return None
    if parser.invalid or parser.stack or not parser.root_closed:
        return None
    return parser.root_opening


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
    new_tag = _single_root_section_opening(str(content or ""))
    if not old_root or not new_tag:
        return (
            "slide_contract：必须保留骨架唯一的根 `<section class=\"slide\">...</section>`；"
            "允许在根内使用嵌套 section；请勿新增顶层兄弟节点，并确保标签闭合"
        )
    old_tag = old_root.group(0)
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


_SLIDE_PRE_RENDER_MUTATION_LIMIT = 6


def _slide_prerender_mutation_error(agent, normalized: str) -> str | None:
    """Keep page edits inside a render -> inspect -> merged-fix cadence.

    The old guard stopped applying forever after *any* render command, including
    a failed validation attempt.  A Slide could therefore spend dozens of turns
    patching an unseen page.  The mutation counter is reset on every render
    attempt; successful pixels must additionally be inspected before another
    edit.  This does not consume or reduce the three-state pixel budget.
    """
    if not (
        _is_current_variant_skill(agent)
        and getattr(agent, "role", "") == "slide"
        and re.fullmatch(r"slides/slide_(\d+)\.html", normalized)
    ):
        return None
    page = int(re.search(r"(\d+)", normalized).group(1))
    rendered = str(
        dict(getattr(agent, "rendered_output_hashes", {}) or {}).get(page, "") or ""
    )
    viewed = str(
        dict(getattr(agent, "viewed_output_hashes", {}) or {}).get(page, "") or ""
    )
    if rendered and viewed != rendered:
        return (
            f"P{page:02d} 已生成新的当前像素，但尚未检查。"
            "不要在看图前继续猜测式修改；先调用 vision_analyze 检查 "
            f"renders/slide_{page:02d}.png，再按同一份诊断合并修复。"
        )
    counts = dict(getattr(agent, "prerender_slide_mutations", {}) or {})
    count = int(counts.get(page, 0))
    if count < _SLIDE_PRE_RENDER_MUTATION_LIMIT:
        return None
    stage = "上一轮像素检查后" if rendered else "当前渲染尝试前"
    return (
        f"P{page:02d} {stage}已连续修改 {count} 次。"
        "不要继续拆成零散 patch；现在先运行当前 Role 的 render --page "
        f"{page}。若校验失败，报错后的定向修复仍可继续；若渲染成功，按 Vision "
        "清单合并修复。这个顺序提醒不消耗像素检查预算。"
    )


def _record_slide_prerender_mutation(agent, normalized: str) -> None:
    if not (
        _is_current_variant_skill(agent)
        and getattr(agent, "role", "") == "slide"
        and re.fullmatch(r"slides/slide_(\d+)\.html", normalized)
    ):
        return
    page = int(re.search(r"(\d+)", normalized).group(1))
    counts = dict(getattr(agent, "prerender_slide_mutations", {}) or {})
    counts[page] = int(counts.get(page, 0)) + 1
    agent.prerender_slide_mutations = counts


def _explicit_surface_color_conflict(agent, content: str) -> str | None:
    """Catch direct user color exclusions being renamed on large surfaces.

    This is a narrow user-contract check, not an aesthetic palette score.  It
    only runs when the raw request explicitly excludes a recurring color
    family and only inspects canvas/background/surface assignment lines.
    Accent colors and ordinary descriptive prose remain model-owned.
    """
    cfg = getattr(agent, "cfg", {})
    raw_query = str(cfg.get("_raw_user_query") or "") if isinstance(cfg, dict) else ""
    families = {
        "米黄": ("米黄", "米白", "暖白", "奶油", "象牙", "暖米", "浅沙"),
        "电蓝": ("电蓝", "电青", "霓虹蓝", "荧光蓝", "赛博蓝"),
    }
    conflicts: list[str] = []
    for excluded, aliases in families.items():
        if not re.search(
            rf"(?:不要|避免|禁用|禁止|不用|不使用)[^，,。；;:：.!?\n]{{0,12}}{excluded}",
            raw_query,
        ):
            continue
        for line in str(content or "").splitlines():
            lowered = line.lower()
            if not any(
                marker in lowered
                for marker in ("content-canvas", "surface", "画布", "底色", "背景")
            ):
                continue
            if any(
                marker in line
                for marker in ("禁止", "不要", "不用", "不使用", "避免", "反默认")
            ):
                continue
            # Color explanations often contrast the chosen cool value with an
            # excluded family ("与米黄不同").  Only the assignment/name before
            # the first hex literal identifies the selected large-surface
            # family; prose after the literal is evidence, not another color
            # assignment.
            assignment = line.split("#", 1)[0]
            if any(alias in assignment for alias in aliases):
                conflicts.append(line.strip()[:240])
    if not conflicts:
        return None
    return (
        "用户明确排除的色系又被用于大面积 canvas/surface："
        + "；".join(conflicts[:3])
        + "。不能靠‘不是/非某色’或改 token 名解除限制。请保留主题依据，"
        "把大画布换到明显不同的色温或明度家族；相关颜色仍可作为小面积、"
        "有语义的 accent。不要新增解释表。"
    )


def write_file(agent, path, content):
    normalized = str(path or "").replace("\\", "/").lstrip("./")
    research_over_budget = False
    if (
        _is_current_variant_skill(agent)
        and getattr(agent, "role", "") == "image"
        and bool(getattr(agent, "image_repair_mode", False))
        and normalized == "assets/catalog.md"
        and (Path(agent.ws) / normalized).is_file()
    ):
        return (
            "write_file 错误[image_repair_catalog_preservation]：定向 Image 修复不得"
            "整体覆盖既有 catalog，否则会删除其他页面已验收素材。请 read_file 当前"
            " catalog，并用 patch 只修改或追加目标条目；随后重新 finalize 整份 catalog。"
        )
    if (
        _is_current_variant_skill(agent)
        and getattr(agent, "role", "") == "orchestrator"
        and normalized == "plan/deck.md"
    ):
        color_conflict = _explicit_surface_color_conflict(agent, str(content or ""))
        if color_conflict:
            return "write_file 错误[explicit_surface_color_conflict]：" + color_conflict
    brief_limit = research_brief_char_limit(agent)
    requested_pages = int(getattr(agent, "requested_slide_count", 0) or 0)
    if (
        _is_current_variant_skill(agent)
        and getattr(agent, "role", "") == "orchestrator"
        and not bool(getattr(agent, "revision_mode", False))
        and normalized == "plan/deck.md"
        and requested_pages
    ):
        page_match = re.search(
            r"(?mi)^\s*-\s*page_count\s*:\s*(\d+)\s*$", str(content or "")
        )
        authored_pages = int(page_match.group(1)) if page_match else 0
        if authored_pages != requested_pages:
            return (
                "write_file 错误[page_count_contract]：本任务必须恰好 "
                f"{requested_pages} 页（已包含封面、结尾和过渡页），但 deck.md 写成 "
                f"{authored_pages or 'missing'}。请在同一总页数内合并页面职责，"
                "不得为了额外结尾页扩页。"
            )
    if (
        _is_current_variant_skill(agent)
        and getattr(agent, "role", "") == "research"
        and normalized == "research/knowledge-brief.md"
        and str(getattr(agent, "evidence_scope", "") or "") == "verify_external"
    ):
        cfg = getattr(agent, "cfg", {})
        raw_query = str(cfg.get("_raw_user_query") or cfg.get("query") or "") \
            if isinstance(cfg, dict) else ""
        reproduction_requested = bool(re.search(
            r"(?i)(?:忠实|复用|重绘|reproduce|redraw).{0,24}"
            r"(?:figure|table|图表|表格|定量|数据)",
            raw_query,
        ))
        indirect_payload = bool(re.search(
            r"(?i)(?:见|参见|详见|see|refer\s+to).{0,28}"
            r"(?:research/)?material\.md",
            str(content or ""),
        ))
        if reproduction_requested and indirect_payload:
            return (
                "write_file 错误[knowledge_brief_indirect_artifact]：用户要求忠实复用/"
                "重绘附件图表或定量结果，但 brief 仍以“见 material.md”代替制作数据。"
                "有 Research 时 Orchestrator 无权回读 material.md。请在同一 brief 中内联"
                "至少满足用户数量要求的独立 artifact payload：Figure/Table 编号与页码、"
                "caption、全部将使用的行列标签、数值、单位、误差/缺失值和口径；删减泛化"
                "背景以控制长度。"
            )
    if (
        _is_current_variant_skill(agent)
        and getattr(agent, "role", "") == "research"
        and normalized == "research/knowledge-brief.md"
    ):
        content_len = len(str(content or ""))
        ceiling = research_brief_hard_ceiling(agent)
        if _research_brief_receipt_exists(agent):
            return (
                "write_file 错误[knowledge_brief_locked]：Research 简报已被接受并锁定，"
                "不允许重写。请立即结束 Research 交接。"
            )
        if content_len > ceiling:
            return (
                "write_file 错误[knowledge_brief_too_long]："
                f"当前 {content_len} 字符，绝对上限 {ceiling} 字符。"
                "请压缩为证据账本：每条事实只出现一次，删除检索过程、候选页表、"
                "重复来源与叙事性复述；保留实体消歧、关键事实/数字、边界、视觉线索和 URL。"
            )
        research_over_budget = content_len > brief_limit
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
                "write_file 错误：v0.4 首次规划不得逐页串行写 plan/slide_NN.md。"
                "请调用 write_plan_batch；一批放连续 2–6 页，8 页 Deck 用 2 批。"
                "若这些页已经存在但合同需要整体纠正，请在 scaffold/页面生产前调用 "
                "write_plan_batch 并设置 replace_existing=true；"
                "只有全册最后一个缺页可单独恢复。"
            )
    error = _write_policy_error(agent, path)
    if error:
        return f"write_file 错误：{error}"
    prerender_error = _slide_prerender_mutation_error(agent, normalized)
    if prerender_error:
        return f"write_file 顺序提醒：{prerender_error}"
    fp = agent.safe(path)
    if (
        _is_current_variant_skill(agent)
        and getattr(agent, "role", "") == "image"
        and normalized == "assets/catalog.md"
        and Path(fp).is_file()
    ):
        current = Path(fp).read_text(encoding="utf-8", errors="replace")
        if current == str(content or ""):
            return (
                "write_file 未改动[image_catalog_noop]：提交内容与当前 assets/catalog.md "
                "逐字相同，不能达到你描述的 URL/状态修复。不要再次整份重写；若 catalog "
                "已正确就运行 image.py finalize 并按 ready/failed 收尾，否则用 patch 精确"
                "替换目标条目的旧字段。"
            )
        rewrites = int(getattr(agent, "_image_catalog_rewrites", 0) or 0)
        if rewrites >= 1:
            return (
                "write_file 错误[image_catalog_rewrite_budget]：catalog 首次整写后只允许一次"
                "完整纠正；后续 URL、download、crop 或 status 变化必须用 patch 精确替换"
                "目标条目，避免反复重发整份素材清单。"
            )
    contract_error = _slide_root_contract_error(agent, normalized, fp, content)
    if contract_error:
        return f"write_file 错误：{contract_error}"
    _atomic_write_text(agent.ws, fp, content)
    if (
        _is_current_variant_skill(agent)
        and getattr(agent, "role", "") == "research"
        and normalized == "research/knowledge-brief.md"
    ):
        _persist_research_handoff_receipt(
            agent, str(content or ""),
            over_budget=research_over_budget,
        )
    if (
        _is_current_variant_skill(agent)
        and getattr(agent, "role", "") == "image"
        and normalized == "assets/catalog.md"
        and 'current' in locals()
    ):
        agent._image_catalog_rewrites = int(
            getattr(agent, "_image_catalog_rewrites", 0) or 0
        ) + 1
    _record_slide_prerender_mutation(agent, normalized)
    if research_over_budget:
        return (
            f"已写入 {len(content.encode())} 字节到 {path}  "
            f"⚠ 简报 {len(str(content or ''))} 字符超出推荐预算 {brief_limit}，"
            "已作为首次写入接受并锁定——不允许后续 write_file 或 patch 修改。"
            "请立即结束 Research 交接。"
        )
    return f"已写入 {len(content.encode())} 字节到 {path}"


def research_brief_char_limit(agent) -> int:
    """Return the page-aware recommended budget for the Research handoff.

    This is the *recommended* target, not the hard ceiling.  Briefs above this
    but below ``research_brief_hard_ceiling`` are accepted once with a warning.
    The allowance scales with the requested deck size and is disclosed to the
    Research role before it writes.
    """
    configured = str(
        getattr(agent, "cfg", {}).get("research_brief_max_chars", "")
        if isinstance(getattr(agent, "cfg", None), dict)
        else ""
    ).strip()
    if configured.isdigit():
        return max(8000, min(int(configured), 30000))
    raw_query = ""
    cfg = getattr(agent, "cfg", None)
    if isinstance(cfg, dict):
        raw_query = str(cfg.get("_raw_user_query") or "")
    requested = int(getattr(agent, "requested_slide_count", 0) or 0)
    range_match = re.search(
        r"(?i)(?<![\d\-–—~～])(\d{1,3})\s*(?:[-–—~～至到])\s*(\d{1,3})\s*"
        r"(?:页|pages?|slides?)(?:\D|$)",
        raw_query,
    )
    page_match = re.search(
        r"(?i)(?:^|\D)(\d{1,3})[-\s]*(?:页|pages?|slides?)(?:\D|$)",
        raw_query,
    )
    if requested > 0:
        pages = requested
    elif range_match and max(
        int(range_match.group(1)), int(range_match.group(2))
    ) <= 100:
        pages = max(int(range_match.group(1)), int(range_match.group(2)))
    else:
        pages = int(page_match.group(1)) if page_match else 15
    if str(getattr(agent, "evidence_scope", "") or "") == "verify_external":
        # Attachment verification briefs carry compact evidence IDs, figure /
        # table descriptions and external resolution notes.  Rejecting an
        # otherwise useful 17--19k handoff forces a second long model rewrite,
        # which is slower and often drops evidence.  Keep this recommended
        # budget large enough for a compact attachment handoff; the separate
        # hard ceiling still bounds safe first-write acceptance.
        return max(20000, min(28000, 10000 + 1000 * pages))
    return max(12000, min(24000, 6000 + 650 * pages))


def _persist_research_handoff_receipt(
    agent, content: str, *, over_budget: bool = False,
) -> None:
    """Record the exact accepted Research handoff outside model-owned files.

    A provider/process interruption can occur after ``write_file`` durably
    commits the brief but before the Agent returns its final text.  The receipt
    lets the parent recover only that exact, already-validated payload instead
    of treating any pre-existing or partial Markdown file as complete.
    """
    encoded = content.encode("utf-8")
    recommended = research_brief_char_limit(agent)
    ceiling = research_brief_hard_ceiling(agent)
    receipt = {
        "version": 2,
        "path": "research/knowledge-brief.md",
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "bytes": len(encoded),
        "characters": len(content),
        "recommended_characters": recommended,
        "hard_ceiling_characters": ceiling,
        "over_budget": over_budget,
    }
    _atomic_write_text(
        agent.ws,
        agent.safe("_trace/research-handoff.json"),
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
    )


def research_handoff_is_valid(agent) -> bool:
    """Return whether the canonical brief matches its accepted write receipt."""
    brief = Path(agent.ws) / "research" / "knowledge-brief.md"
    receipt_path = Path(agent.ws) / "_trace" / "research-handoff.json"
    try:
        payload = brief.read_bytes()
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    if not payload or not isinstance(receipt, dict):
        return False
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return False
    version = receipt.get("version")
    if version not in (1, 2):
        return False
    if receipt.get("path") != "research/knowledge-brief.md":
        return False
    if receipt.get("sha256") != hashlib.sha256(payload).hexdigest():
        return False
    if receipt.get("bytes") != len(payload):
        return False
    if receipt.get("characters") != len(text):
        return False
    if version == 1:
        return len(text) <= research_brief_char_limit(agent)
    recommended = research_brief_char_limit(agent)
    ceiling = research_brief_hard_ceiling(agent)
    stored_over = receipt.get("over_budget")
    if not isinstance(stored_over, bool):
        return False
    if receipt.get("recommended_characters") != recommended:
        return False
    if receipt.get("hard_ceiling_characters") != ceiling:
        return False
    if stored_over != (len(text) > recommended):
        return False
    if len(text) > ceiling:
        return False
    return True


def research_brief_hard_ceiling(agent) -> int:
    """Absolute ceiling above which briefs are always rejected."""
    return min(40000, int(research_brief_char_limit(agent) * 1.6))


def _research_brief_receipt_exists(agent) -> bool:
    """Return True only when a fully valid research handoff receipt exists."""
    return research_handoff_is_valid(agent)


def _decode_plan_transport_text(value: str) -> str:
    """Decode one transport layer without corrupting literal ``\\n`` text."""
    value = re.sub(r"(?<!\\)\\r\\n", "\n", value)
    value = re.sub(r"(?<!\\)\\n", "\n", value)
    value = re.sub(r"(?<!\\)\\t", "\t", value)
    value = re.sub(r'(?<!\\)\\"', '"', value)
    return value.replace("\\\\", "\\")


def _recover_content_only_plan_batch(raw: str) -> list[dict[str, str]]:
    """Recover a transport-flattened plan batch from canonical headings.

    Some Hermes/OpenAI-compatible adapters have been observed to collapse an
    array of ``{path, content}`` objects into one JSON-looking object with
    repeated ``content`` keys and no paths.  JSON parsing either fails at the
    final brace or would keep only the last duplicate key.  Extract only
    individually valid JSON string values and derive a path solely from the
    canonical ``# slide_NN`` heading.  Any malformed value, missing/duplicate
    heading, or non-contiguous sequence rejects the entire recovery.
    """
    markers = list(re.finditer(r'"content"\s*:\s*', raw))
    if not markers or len(markers) > 12:
        return []
    decoder = json.JSONDecoder()
    values: list[str] = []
    strict_ok = True
    for index, marker in enumerate(markers):
        try:
            value, value_end = decoder.raw_decode(raw[marker.end():])
        except (json.JSONDecodeError, TypeError):
            strict_ok = False
            break
        absolute_end = marker.end() + value_end
        if index + 1 < len(markers):
            separator = raw[absolute_end:markers[index + 1].start()]
            if not re.fullmatch(r"\s*,\s*", separator):
                strict_ok = False
                break
        elif not re.fullmatch(r"\s*\}?\s*\]\s*", raw[absolute_end:]):
            strict_ok = False
            break
        if not isinstance(value, str):
            strict_ok = False
            break
        values.append(value)

    if not strict_ok:
        # A second observed adapter shape decodes the outer argument before
        # passing it to the tool, so ordinary quotes inside Markdown are no
        # longer escaped.  JSONDecoder then stops at the first title quote.
        # Bound values only by the repeated top-level content markers.  A
        # marker-like phrase inside prose makes a fragment fail the canonical
        # heading checks below, rejecting the whole batch rather than guessing.
        values = []
        for index, marker in enumerate(markers):
            end = (
                markers[index + 1].start()
                if index + 1 < len(markers)
                else len(raw)
            )
            envelope = raw[marker.end():end].strip()
            if index + 1 < len(markers):
                if not envelope.endswith(","):
                    return []
                envelope = envelope[:-1].rstrip()
            else:
                closing = re.search(r"\s*\}?\s*\]\s*$", envelope)
                if not closing:
                    return []
                envelope = envelope[:closing.start()].rstrip()
            if len(envelope) < 2 or not (
                envelope.startswith('"') and envelope.endswith('"')
            ):
                return []
            value = _decode_plan_transport_text(envelope[1:-1])
            values.append(value)

    recovered: list[dict[str, str]] = []
    pages: list[int] = []
    for value in values:
        if len(value.strip()) < 40:
            return []
        heading = re.match(r"\s*#\s*slide_(\d{2})\b", value)
        if not heading:
            return []
        page = int(heading.group(1))
        if page < 1:
            return []
        pages.append(page)
        recovered.append({
            "path": f"plan/slide_{page:02d}.md",
            "content": value,
        })
    if len(set(pages)) != len(pages) or pages != sorted(pages):
        return []
    if pages != list(range(pages[0], pages[0] + len(pages))):
        return []
    return recovered


def write_plan_batch(agent, files, replace_existing=False):
    """Serialize and apply canonical slide plans without model-side JSON escaping.

    The model-facing payload is most efficient at 2--6 files, but several
    OpenAI-compatible transports do not enforce JSON Schema.  Accept a final
    singleton or a 7--12 page payload here and normalize it at the adapter edge
    instead of spending another model turn rewriting identical plans.
    """
    if getattr(agent, "role", "") != "orchestrator":
        return "write_plan_batch 错误：只有 Orchestrator 可以写逐页计划批次。"
    if bool(getattr(agent, "revision_mode", False)):
        return "write_plan_batch 错误：编辑模式请按影响图 patch 现有逐页计划。"
    if not isinstance(replace_existing, bool):
        return "write_plan_batch 错误[invalid_mode]：replace_existing 必须是布尔值。"
    delegated = {
        str(role).lower()
        for role in (getattr(agent, "delegated_roles", set()) or set())
    }
    slides_dir = Path(agent.ws) / "slides"
    produced_slides = list(slides_dir.glob("slide_*.html")) if slides_dir.is_dir() else []
    production_started = bool("slide" in delegated or produced_slides)
    if replace_existing and production_started:
        return (
            "write_plan_batch 错误[production_started]：页面生产或 scaffold 已开始，"
            "不得批量覆盖 canonical plans；请停止改拓扑，并按影响范围 patch 后走修复路由。"
        )
    if isinstance(files, str):
        # A few OpenAI-compatible transports stringify an otherwise valid
        # array argument even though the public schema declares an array.
        # Decode that envelope once at the adapter edge; the model must never
        # rebuild a giant JSON-inside-JSON manifest merely to recover.
        raw_files = files
        flattened_content_only = (
            len(re.findall(r'"content"\s*:', raw_files)) > 1
            and not re.search(r'"path"\s*:', raw_files)
        )
        # Duplicate JSON object keys are syntactically valid, but json.loads
        # keeps only the final value. Detect this provider flattening *before*
        # parsing so an eight-page batch cannot silently become slide 08 only.
        if flattened_content_only:
            content_only = _recover_content_only_plan_batch(raw_files)
            if not content_only:
                return (
                    "write_plan_batch 错误[invalid_content_only_string]：transport "
                    "把多页数组压成重复 content，且 canonical # slide_NN 标题无法形成"
                    "唯一、升序、连续批次；整批未写入。"
                )
            files = content_only
        else:
            try:
                files = json.loads(raw_files)
            except json.JSONDecodeError as exc:
            # Preserve the older explicit-path recovery for envelopes whose
            # Markdown contains unescaped quotes.  Content-only recovery is
            # reserved for the observed transport flattening that removed all
            # path keys; otherwise a JSON decoder could stop at the first
            # unescaped quote and silently truncate a plan.
                content_only = (
                    _recover_content_only_plan_batch(raw_files)
                    if not re.search(r'"path"\s*:', raw_files)
                    else []
                )
                if content_only:
                    files = content_only
                else:
                    # A recurring compatibility shape is JSON-looking text
                    # whose Markdown contains ordinary, unescaped quotation
                    # marks. Recover only records with an explicit canonical
                    # path marker; never guess list boundaries or page numbers.
                    recovered = re.findall(
                        r'\{\s*"content"\s*:\s*"(.*?)"\s*,\s*'
                        r'"path"\s*:\s*"(plan/slide_\d{2}\.md)"\s*\}',
                        raw_files,
                        flags=re.DOTALL,
                    )
                    declared_paths = re.findall(
                        r'"path"\s*:\s*"(plan/slide_\d{2}\.md)"', raw_files
                    )
                    declared_contents = len(
                        re.findall(r'\{\s*"content"\s*:', raw_files)
                    )
                    if (
                        recovered
                        and len(recovered) == len(declared_paths) == declared_contents
                    ):
                        files = [
                            {
                                "path": path,
                                "content": _decode_plan_transport_text(content),
                            }
                            for content, path in recovered
                        ]
                    else:
                        return (
                            "write_plan_batch 错误[invalid_json_string]：files 被传成字符串且"
                            f"无法解码（line={exc.lineno}, column={exc.colno}）。"
                            "请直接按工具 schema 传数组，不要手工转义 Markdown。"
                        )
    if not isinstance(files, list):
        return "write_plan_batch 错误[invalid_type]：files 必须是数组。"
    if not files:
        return "write_plan_batch 错误[empty]：files 不能为空。"
    if len(files) > 32:
        return (
            f"write_plan_batch 错误[too_many]：收到 {len(files)} 页，兼容传输上限为 32 页。"
            "请按连续页码拆分，以免一次模型响应过长。"
        )
    requested_pages = int(getattr(agent, "requested_slide_count", 0) or 0)
    normalized: list[dict[str, str]] = []
    numbers: list[int] = []
    for index, item in enumerate(files, start=1):
        if not isinstance(item, dict):
            return f"write_plan_batch 错误：files[{index}] 必须是对象。"
        path = str(item.get("path") or "").replace("\\", "/").lstrip("./")
        content = item.get("content")
        match = re.fullmatch(r"plan/slide_(\d{2})\.md", path)
        if not match and isinstance(content, str):
            # Recover a missing path only when the canonical heading says the
            # exact page number. Never infer from list position.
            heading = re.match(r"\s*#\s*slide_(\d{2})\b", content)
            if heading:
                path = f"plan/slide_{heading.group(1)}.md"
                match = re.fullmatch(r"plan/slide_(\d{2})\.md", path)
        if not match or not isinstance(content, str) or not content.strip():
            return (
                f"write_plan_batch 错误：files[{index}] 需要规范 path "
                "plan/slide_NN.md 和非空 content。"
            )
        numbers.append(int(match.group(1)))
        normalized.append({"path": path, "content": content})
    if requested_pages and any(number > requested_pages for number in numbers):
        return (
            "write_plan_batch 错误[page_count_contract]：本任务页码范围只能是 "
            f"01–{requested_pages:02d}；收到 "
            + ",".join(f"{number:02d}" for number in numbers if number > requested_pages)
            + "。封面和结尾必须计入这个总数。"
        )
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
    auto_replace_existing = False
    if not replace_existing:
        conflicts = []
        for item in normalized:
            target = Path(agent.safe(item["path"]))
            if target.is_file() and target.read_text(encoding="utf-8") != item["content"]:
                conflicts.append(item["path"])
        if conflicts:
            if production_started:
                return (
                    "write_plan_batch 错误[production_started]：以下计划已存在且内容不同："
                    + ", ".join(conflicts)
                    + "。页面生产或 scaffold 已开始，禁止覆盖 canonical plans；"
                    "请按影响范围 patch 后走修复路由。"
                )
            recoveries = int(
                getattr(agent, "plan_batch_auto_recoveries", 0) or 0
            )
            if recoveries >= 1:
                return (
                    "write_plan_batch 错误[auto_recovery_exhausted]：Harness 已完成一次"
                    "页面生产前的冲突批次原子恢复；若仍需再次整体纠正，请明确设置 "
                    "replace_existing=true。"
                )
            # Before scaffold/page production, Orchestrator is the sole owner
            # of canonical plans.  OpenAI-compatible models occasionally
            # repeat a corrected continuous batch while omitting the explicit
            # boolean even after the tool tells them how to recover.  Apply
            # that pre-production correction atomically instead of spending
            # several model turns in an already_exists loop.  Once any page
            # HTML exists, the production_started branch above remains a hard
            # stop and this recovery is unavailable.
            replace_existing = True
            auto_replace_existing = True
            agent.plan_batch_auto_recoveries = recoveries + 1
    chunks: list[list[dict[str, str]]] = []
    remaining = list(normalized)
    while len(remaining) > 6:
        # Avoid leaving an illegal singleton for the internal 2--6-file
        # primitive (7 -> 5+2, 8 -> 6+2, 12 -> 6+6).
        take = 5 if len(remaining) == 7 else 6
        chunks.append(remaining[:take])
        remaining = remaining[take:]
    if remaining:
        chunks.append(remaining)

    role_script = Path(_agent_role_script(agent))
    bootstrap = role_script.parent / "_internal" / "bootstrap.py"
    script = agent.safe(str(bootstrap))
    outputs: list[str] = []
    if auto_replace_existing:
        outputs.append(
            "status:RECOVERED pre-production plan conflicts were atomically "
            "replaced (auto_replace_existing=true); no Slide HTML/PNG existed."
        )
    for chunk in chunks:
        if len(chunk) == 1:
            target = agent.safe(chunk[0]["path"])
            _atomic_write_text(agent.ws, target, chunk[0]["content"])
            page_number = int(re.search(r"(\d{2})", chunk[0]["path"]).group(1))
            outputs.append(
                "status:PASS additive single-page compatibility write applied: "
                f"pages={page_number:02d} changed=1 replace_existing={str(replace_existing).lower()}. "
                "This created or updated only that canonical page and did not overwrite "
                "any other plan. Continue with write_plan_batch for the next missing "
                "continuous pages; do not switch to serial write_file."
            )
            continue

        manifest = agent.safe("plan/plan-batch.json")
        _atomic_write_text(
            agent.ws,
            manifest,
            json.dumps(
                {"files": chunk, "replace_existing": replace_existing},
                ensure_ascii=False,
                indent=2,
            ) + "\n",
        )
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
            # plan-batch.json is a transient transport manifest, never a
            # canonical artifact.
            Path(manifest).unlink(missing_ok=True)
        output = "\n".join(
            part for part in (completed.stdout.strip(), completed.stderr.strip()) if part
        )
        if completed.returncode != 0:
            return f"write_plan_batch 错误：apply-plan-batch 失败\n{output[-4000:]}"
        outputs.append(output or f"status:PASS\nwritten:{len(chunk)}")
    return "\n".join(outputs)


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
    if old_string == new_string:
        return (
            "patch 错误[noop_identical]：old_string 与 new_string 完全相同，"
            "这不会修改文件。不要原样重试；核对真正要修的字符（尤其 O/0、l/1、"
            "连字符和空格），用当前文件中的精确短片段作为 old_string，并提交一个"
            "确实不同的 new_string。"
        )
    normalized = str(path or "").replace("\\", "/").lstrip("./")
    if (
        _is_current_variant_skill(agent)
        and normalized == "base.css"
        and getattr(agent, "role", "") in {"slide", "review"}
        and re.search(r"#slide-\d{2}\b", old_string + "\n" + new_string, flags=re.I)
    ):
        return (
            "patch 错误[page_scoped_css_in_base]：页面专属 #slide-NN 规则不能写入 "
            "base.css；这会让一次局部修复失效整册渲染缓存。请把该规则放回对应 "
            "slides/slide_NN.html 的本页 <style>，base.css 只修改真正共享的 token/组件。"
        )
    prerender_error = _slide_prerender_mutation_error(agent, normalized)
    if prerender_error:
        return f"patch 顺序提醒：{prerender_error}"
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
        signature = hashlib.sha256(
            (normalized + "\0" + old_string + "\0" + new_string).encode("utf-8")
        ).hexdigest()
        misses = dict(getattr(agent, "_patch_miss_counts", {}) or {})
        misses[signature] = int(misses.get(signature, 0) or 0) + 1
        agent._patch_miss_counts = misses
        if misses[signature] == 1:
            agent._patch_recovery_path = normalized
        if misses[signature] > 1:
            return (
                f"patch 错误[stale_patch_repeated]：{path} 的同一 old_string 在重新读取后"
                "仍不存在；此 patch 路径已作废。不得再次提交它，改用刚读取的当前短片段，"
                "或放弃可选替换并保留最近一次 finalize 回执。"
            )
        return (
            f"patch 错误：在 {path} 里找不到 old_string。"
            "Harness 下一轮只开放该文件的 read_file；读取当前内容后最多再试一次，"
            "不要重复相同 patch"
        )
    if n > 1 and not replace_all:
        return (
            f"patch 错误：old_string 出现了 {n} 次（不唯一）；"
            "确认要全改请传 replace_all=true"
        )
    updated = s.replace(old_string, new_string)
    if (
        _is_current_variant_skill(agent)
        and getattr(agent, "role", "") == "research"
        and normalized == "research/knowledge-brief.md"
    ):
        if _research_brief_receipt_exists(agent):
            return (
                "patch 错误[knowledge_brief_locked]：Research 简报已被接受并锁定，"
                "不允许后续 patch。请立即结束 Research 交接。"
            )
        ceiling = research_brief_hard_ceiling(agent)
        if len(updated) > ceiling:
            return (
                "patch 错误[knowledge_brief_too_long]：修改后简报为 "
                f"{len(updated)} 字符，绝对上限 {ceiling} 字符。"
                "请用更短的替换合并重复事实；不得借增量 patch 绕过交接上限。"
            )
    contract_error = _slide_root_contract_error(agent, normalized, fp, updated)
    if contract_error:
        return f"patch 错误：{contract_error}"
    _atomic_write_text(agent.ws, fp, updated)
    if (
        _is_current_variant_skill(agent)
        and getattr(agent, "role", "") == "research"
        and normalized == "research/knowledge-brief.md"
    ):
        _persist_research_handoff_receipt(
            agent, updated,
            over_budget=len(updated) > research_brief_char_limit(agent),
        )
    _record_slide_prerender_mutation(agent, normalized)
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
                        "v0.4 scripts/** 是只执行能力面：禁止列举、读取或执行其他 Role "
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


def missing_rendered_plan_pages(agent) -> tuple[int, ...]:
    """Return canonical plan pages that do not yet have a rendered PNG."""
    root = Path(agent.ws)
    pages = []
    for plan_path in sorted((root / "plan").glob("slide_[0-9][0-9].md")):
        match = re.fullmatch(r"slide_(\d+)\.md", plan_path.name)
        if not match:
            continue
        page = int(match.group(1))
        render = root / "renders" / f"slide_{page:02d}.png"
        if not render.is_file() or render.stat().st_size == 0:
            pages.append(page)
    return tuple(pages)


def _plan_needs_bitmap(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    match = re.search(r"(?mi)^-\s*needs_bitmap\s*:\s*(true|false)\s*$", text)
    return bool(match and match.group(1).lower() == "true")


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
    # ``subprocess.run`` already captures and caps Role-script output.  Models
    # sometimes append a harmless ``2>&1 | head/tail`` display filter anyway;
    # normalize that wrapper so the deterministic command and its durable
    # finalize receipt are still recognized.
    role_script = _agent_role_script(agent)
    output_filter = re.fullmatch(
        rf"(?P<base>python(?:3)?\s+{re.escape(role_script)}\s+[^|<>`]*?)"
        rf"(?:\s+2>&1)?\s*\|\s*(?:head|tail)"
        rf"(?:\s+-n\s+\d+|\s+-\d+|\s+\d+)\s*",
        cmd,
    )
    if output_filter:
        cmd = output_filter.group("base").strip()
        agent.log(
            "[terminal canonicalized] removed redundant head/tail output filter "
            "from deterministic Role command"
        )
    if (
        _is_current_variant_skill(agent)
        and getattr(agent, "role", "") == "orchestrator"
        and re.search(r"\borchestrator\.py\s+(?:finalize|audit)\s+\.", cmd)
    ):
        missing_pages = missing_rendered_plan_pages(agent)
        failed_bitmap_pages = {
            int(page)
            for page in (getattr(agent, "image_failed_pages", ()) or ())
            if isinstance(page, int) and not isinstance(page, bool) and page > 0
        }
        blocked_pages = tuple(
            page
            for page in missing_pages
            if page in failed_bitmap_pages
            and _plan_needs_bitmap(
                Path(agent.ws) / "plan" / f"slide_{page:02d}.md"
            )
        )
        delegatable_pages = tuple(
            page for page in missing_pages
            if page not in set(blocked_pages)
            and not _page_has_terminal_slide_failure_from_outcomes(agent, page)
        )
        terminal_artifact_pages = tuple(
            page for page in missing_pages
            if _page_has_terminal_slide_failure_from_outcomes(agent, page)
        )
        if delegatable_pages:
            agent.pending_slide_delegation_pages = delegatable_pages
            pages_text = ", ".join(
                f"P{page:02d}" for page in delegatable_pages
            )
            blocked_note = (
                " Image 已报告但仍未重分类的页面暂不进入责任门："
                + ", ".join(f"P{page:02d}" for page in blocked_pages)
                + "；本次 Slide 提交成功、责任门解除后，再根据真实素材缺口修改其"
                "未启动计划并重新 validate-plans。"
                if blocked_pages else ""
            )
            terminal_note = (
                " 已彻底失败的页面不再委派："
                + ", ".join(f"P{page:02d}" for page in terminal_artifact_pages)
                + "。"
                if terminal_artifact_pages else ""
            )
            return (
                "terminal 错误[unstarted_slide_responsibility]：以下计划页尚无当前 PNG："
                f"{pages_text}。不能用 finalize/audit 替代页面制作。下一步只提交一次 "
                "delegate_task，覆盖这些缺失责任单元；Harness 将暂时只开放委派工具，"
                "成功提交后再恢复正常工具面。" + blocked_note + terminal_note
            )
        if terminal_artifact_pages and not blocked_pages:
            pages_text = ", ".join(
                f"P{page:02d}" for page in terminal_artifact_pages
            )
            agent.terminal_slide_failures = tuple(terminal_artifact_pages)
            return (
                f"[missing_slide_artifact] 以下页面已彻底失败且无法恢复：{pages_text}。"
                "所有可委派页面已完成或终止。请以 missing_slide_artifact 结束本任务。"
            )
        if blocked_pages:
            return (
                "terminal 错误[image_contract_unresolved]：以下尚未启动页面仍要求 Image "
                "已明确报告不可得的位图："
                + ", ".join(f"P{page:02d}" for page in blocked_pages)
                + "。不要再次委派 Image。若整册已有其他 ready 位图且用户未要求每页"
                "必须配图，请一次性把这些未启动页改成可独立表达的 Canvas/SVG/"
                "ECharts/排印方案并重新 validate-plans；否则如实以 image_blocked 收口。"
            )
    requested_pages = int(getattr(agent, "requested_slide_count", 0) or 0)
    expected_match = re.search(
        r"\borchestrator\.py\s+(?:validate-plans|scaffold|finalize|audit)\s+\."
        r"(?:\s+[^;&|<>`]*)?\s--expected\s+(\d+)\b",
        cmd,
    )
    count_checked_action = re.search(
        r"\borchestrator\.py\s+(?:validate-plans|scaffold|finalize|audit)\s+\.(?:\s|$)",
        cmd,
    )
    if (
        _is_current_variant_skill(agent)
        and getattr(agent, "role", "") == "orchestrator"
        and requested_pages
        and count_checked_action
        and not expected_match
    ):
        return (
            "terminal 错误[page_count_contract]：本任务的 validate/scaffold/finalize/audit "
            f"必须显式携带 `--expected {requested_pages}`，让每一阶段都核对用户指定总页数。"
        )
    if (
        _is_current_variant_skill(agent)
        and getattr(agent, "role", "") == "orchestrator"
        and requested_pages
        and expected_match
        and int(expected_match.group(1)) != requested_pages
    ):
        return (
            "terminal 错误[page_count_contract]：--expected 必须是用户指定的总页数 "
            f"{requested_pages}（包含封面/结尾/过渡页），不能使用 "
            f"{expected_match.group(1)}。先把 deck.md 与逐页计划压回正确页数。"
        )
    if (
        _is_current_variant_skill(agent)
        and re.search(r"(?:^|[\s'\"])(?:\./)?_trace(?:/|\b)", cmd.replace("\\", "/"))
    ):
        return (
            "terminal 错误[trace_internal]：_trace/** 是 Harness 控制面，不是 Role 输入。"
            "不得用 ls/grep/cat/tail 反查子 Agent 或状态实现；依赖完成、失败页、修复路由"
            "与精简摘要会由 Harness 结构化注入。"
        )
    if (
        _is_current_variant_skill(agent)
        and getattr(agent, "role", "") == "review"
        and "renders/render.json" in cmd.replace("\\", "/")
        and "review.py finalize" not in cmd
    ):
        manifest = os.path.join(str(agent.ws), "renders", "render.json")
        try:
            return (
                "terminal 只读改写[review_compact_render_summary]：不要 grep/wc/分页读取"
                "原始 DOM manifest；以下摘要已包含页码、确定性告警与缓存状态。"
                "页面视觉结论请直接打开对应 PNG。\n"
                + _review_render_summary(manifest)
            )
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            return "terminal 错误：renders/render.json 缺失或不可解析；返回 needs_orchestrator。"
    image_replacement_command = bool(
        _is_current_variant_skill(agent)
        and getattr(agent, "role", "") == "image"
        and re.fullmatch(
            rf"python(?:3)?\s+{re.escape(_agent_role_script(agent))}\s+fetch\s+\."
            rf"(?:\s+[^;&|<>`]*)?\s+--replace(?:\s+[^;&|<>`]*)?",
            cmd,
        )
    )
    image_initial_fetch_command = bool(
        _is_current_variant_skill(agent)
        and getattr(agent, "role", "") == "image"
        and re.fullmatch(
            rf"python(?:3)?\s+{re.escape(_agent_role_script(agent))}\s+fetch\s+\.",
            cmd,
        )
    )
    image_finalize_command = bool(
        _is_current_variant_skill(agent)
        and getattr(agent, "role", "") == "image"
        and re.fullmatch(
            rf"python(?:3)?\s+{re.escape(_agent_role_script(agent))}\s+finalize\s+\."
            rf"(?:\s+[^;&|<>`]*)?",
            cmd,
        )
    )
    image_crop_output = ""
    image_crop_source = ""
    if (
        _is_current_variant_skill(agent)
        and getattr(agent, "role", "") == "image"
    ):
        try:
            crop_tokens = shlex.split(cmd)
        except (TypeError, ValueError):
            crop_tokens = []
        if (
            len(crop_tokens) >= 6
            and os.path.basename(crop_tokens[0]) in {"python", "python3"}
            and crop_tokens[1] == _agent_role_script(agent)
            and crop_tokens[2:4] == ["crop-material", "."]
            and "--output" in crop_tokens
        ):
            output_index = crop_tokens.index("--output") + 1
            if output_index < len(crop_tokens):
                image_crop_output = str(crop_tokens[output_index]).replace("\\", "/")
            if "--source" in crop_tokens:
                source_index = crop_tokens.index("--source") + 1
                if source_index < len(crop_tokens):
                    image_crop_source = str(crop_tokens[source_index]).replace("\\", "/")
    if image_crop_output:
        crop_attempts = dict(getattr(agent, "_image_crop_attempts", {}) or {})
        crop_state = dict(crop_attempts.get(image_crop_output, {}) or {})
        successes = int(crop_state.get("success", 0) or 0)
        failures = int(crop_state.get("failure", 0) or 0)
        if successes >= 2 or failures >= 2:
            return (
                "terminal 止损[material_crop_budget]：同一附件素材 `"
                + image_crop_output
                + "` 已用完首稿与一次定向修复预算。不要继续微调裁切框或换名重裁；"
                "若当前文件仍不合格，在 assets/catalog.md 将对应条目标为 failed，"
                "运行一次 image.py finalize .，并把受影响页结构化交给 Orchestrator "
                "改为 HTML/CSS/SVG/ECharts 忠实重绘。已有通过素材照常交接。"
            )
        source_attempts = dict(
            getattr(agent, "_image_crop_source_attempts", {}) or {}
        )
        source_state = dict(source_attempts.get(image_crop_source, {}) or {})
        source_failures = int(source_state.get("failure", 0) or 0)
        if image_crop_source and source_failures >= 3:
            return (
                "terminal 止损[material_source_crop_budget]：同一附件页 `"
                + image_crop_source
                + f"` 已有 {source_failures} 次裁取失败，说明继续换输出名或微调 box "
                "不会形成可靠素材。"
                "保留已通过裁图；其余条目标为 failed，finalize 后将对应页面交给 "
                "Orchestrator 改为忠实代码重绘。"
            )
    image_plain_fetch_after_critic = bool(
        _is_current_variant_skill(agent)
        and getattr(agent, "role", "") == "image"
        and re.search(r"\bimage\.py\s+fetch\s+\.\s*(?:$|--(?!replace\b))", cmd)
        and not image_replacement_command
        and isinstance(getattr(agent, "vision_critic_results", None), dict)
        and "assets/contact-sheet.png" in agent.vision_critic_results
    )
    if image_plain_fetch_after_critic:
        return (
            "terminal 止损[image_post_critic_replacement]：联系表已经完成首轮诊断；"
            "此后不得继续普通 `fetch .` 抽图。把所有明确失败项一次性更新到 catalog，"
            "再且仅再运行一次 `image.py fetch . --replace`；仍失败则逐项标记 failed 并交接。"
        )
    if image_initial_fetch_command and int(
        getattr(agent, "_image_initial_fetch_passes", 0) or 0
    ) >= 1:
        return (
            "terminal 止损[image_initial_fetch_budget]：首轮批量 fetch 已经执行。"
            "不要为单个失败 URL 反复普通 fetch；一次性更新全部失败项后，运行且仅运行"
            "一次 `image.py fetch . --replace`。仍失败的条目标为 failed，再 finalize 并"
            "交接已有素材。"
        )
    if image_replacement_command:
        # The contact sheet is the locator.  Do not force Image to inspect every
        # catalog row at full resolution before its one coordinated replacement
        # pass: that makes a small suspected defect expand into an O(N) vision
        # loop and contradicts the targeted-inspection contract.  Image chooses
        # the suspicious rows, patches them together, and receives one pass.
        replacement_passes = int(
            getattr(agent, "_image_replacement_passes", 0) or 0
        )
        if replacement_passes >= 1:
            return (
                "terminal 止损[image_replacement_budget]：本 Image Agent 已完成一次"
                "定向替换批次；不得继续用同义搜索和 --replace 重做素材。请保留"
                "通过检查的资产，把仍不合格项在 assets/catalog.md 标为 failed 并"
                "结构化交给 Orchestrator，不把整册拖入素材循环。"
            )
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
            grouped_actions = "render|render-group" if _is_grouped_skill(agent) else "render"
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
                        "slide.py render --page NN / render-group。"
                        if _is_grouped_skill(agent)
                        else "slide.py render。"
                    )
                )
                + "读取请用 read_file，页面修改请用 write_file / patch；"
                "不搜索 CSS、脚本或目录。若刚才的 render 已给出缺少 Canvas/ECharts "
                "元素或脚本的错误，直接在当前 slide HTML 中补齐并重跑同一 render。"
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
        if len(assigned_pages) > 1:
            is_group_render = bool(group_match)
            if is_group_render and rendered_pages != assigned_pages:
                return (
                    "terminal 错误[group_page_coverage]：最终 render-group 必须覆盖完整责任组 "
                    + ",".join(f"{page:02d}" for page in sorted(assigned_pages))
                    + "。"
                )
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
    if image_replacement_command:
        # The replacement budget is a pass/attempt budget, not a success
        # budget. A provider 429, timeout, or bad URL is exactly when retrying
        # the identical whole batch would create a runaway loop; consume the
        # single pass before execution and require a partial failed handoff.
        agent._image_replacement_passes = int(
            getattr(agent, "_image_replacement_passes", 0) or 0
        ) + 1
    try:
        command_env = os.environ.copy()
        command_env["MURAL_RENDER_ATTEMPT_ID"] = str(
            getattr(agent, "trace_label", "")
            or getattr(agent, "label", "")
            or getattr(agent, "role", "unscoped")
        )
        r = subprocess.run(
            cmd,
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
    if image_crop_output:
        crop_attempts = dict(getattr(agent, "_image_crop_attempts", {}) or {})
        crop_state = dict(crop_attempts.get(image_crop_output, {}) or {})
        outcome = "success" if r.returncode == 0 else "failure"
        crop_state[outcome] = int(crop_state.get(outcome, 0) or 0) + 1
        crop_attempts[image_crop_output] = crop_state
        agent._image_crop_attempts = crop_attempts
        if image_crop_source:
            source_attempts = dict(
                getattr(agent, "_image_crop_source_attempts", {}) or {}
            )
            source_state = dict(source_attempts.get(image_crop_source, {}) or {})
            source_state[outcome] = int(source_state.get(outcome, 0) or 0) + 1
            source_attempts[image_crop_source] = source_state
            agent._image_crop_source_attempts = source_attempts
    out = (r.stdout or "")
    if r.stderr:
        out += ("\n[stderr] " + r.stderr)
    if r.returncode:
        out += f"\n[exit_code={r.returncode}]"
    if (
        image_crop_output
        and r.returncode != 0
        and int(
            dict(
                dict(getattr(agent, "_image_crop_attempts", {}) or {}).get(
                    image_crop_output, {}
                )
            ).get("failure", 0)
            or 0
        ) >= 2
    ):
        out += (
            "\n[material_crop_route_required] 同一输出已连续两次裁取失败；"
            "停止修改 box。把该 catalog 条目标为 failed，finalize 已通过的其他素材，"
            "并要求受影响页从 bitmap-material 改为忠实代码重绘。"
        )
    source_failures = int(
        dict(
            dict(getattr(agent, "_image_crop_source_attempts", {}) or {}).get(
                image_crop_source, {}
            )
        ).get("failure", 0)
        or 0
    ) if image_crop_source else 0
    if image_crop_source and r.returncode != 0 and source_failures == 2:
        out += (
            "\n[crop_escalation_required] 此附件页已发生第 2 次裁取失败。"
            "若独立视觉本身仍然文字密集，立即标记 failed 并路由到 "
            "needs_bitmap:false 的忠实代码重绘；不要通过更换输出名继续试裁。"
        )
    if image_initial_fetch_command:
        agent._image_initial_fetch_passes = int(
            getattr(agent, "_image_initial_fetch_passes", 0) or 0
        ) + 1
    if r.returncode == 0 and image_finalize_command and "status:PASS" in out:
        catalog_path = os.path.join(str(agent.ws), "assets", "catalog.md")
        digest = hashlib.sha256()
        try:
            with open(catalog_path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            agent.image_finalized_catalog_digest = ""
        else:
            agent.image_finalized_catalog_digest = digest.hexdigest()
        # ``image.py finalize`` is the deterministic asset handoff receipt.
        # Persist its page partition immediately instead of depending on a
        # later prose response: a provider timeout or a failed optional
        # replacement must not erase already-finalized assets.
        for field, attribute in (
            ("bitmap_ready", "image_ready_pages"),
            ("failed", "image_failed_pages"),
        ):
            match = re.search(
                rf"(?mi)^\s*{field}\s*:\s*([^\n]*)$",
                out,
            )
            if match:
                pages = tuple(sorted({
                    int(value)
                    for value in re.findall(r"\b\d{1,3}\b", match.group(1))
                }))
                setattr(agent, attribute, pages)
        agent.image_handoff_summary = out[-1800:]
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
        inspection_query = (
            "This is a low-resolution Slide Group consistency montage. Inspect only "
            "cross-page visual DNA, hierarchy rhythm, repeated geometry, relative density, "
            "and transitions. Do not declare page-local overlap, clipping, typography, "
            "crop, or edge safety resolved from this montage. Full-resolution single-page "
            "Critic evidence is authoritative for those local defects; if the montage "
            "appears to contradict an open page issue, preserve the page issue. "
            "Additional requested focus: " + inspection_query
        )
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
        if hasattr(agent, "group_rendered_page_hashes"):
            sheet_path = Path(agent.ws) / normalized_path
            try:
                sheet_hash = hashlib.sha256(sheet_path.read_bytes()).hexdigest()
            except OSError:
                sheet_hash = ""
            generated_sheet_hash = str(
                getattr(agent, "group_rendered_contact_hash", "") or ""
            )
            generated_pages = dict(
                getattr(agent, "group_rendered_page_hashes", {}) or {}
            )
            if generated_sheet_hash != sheet_hash or any(
                str(generated_pages.get(int(page), "") or "")
                != str(rendered.get(int(page), "") or "")
                for page in assigned_pages
            ):
                return (
                    "vision_analyze 错误：这张组联系表不是由当前全部页面像素生成。"
                    "请先对完整责任组重新执行 render-group；旧联系表和旧 Vision 缓存"
                    "不能用于关闭新的组状态。"
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


def _consume_research_search_budget(agent, query: str = "") -> str:
    """Bound repeated Research and Image synonym-search loops.

    One ``web_search`` already performs a deterministic simplified-query retry
    on an empty result, so another dozen near-duplicate tool calls rarely adds
    evidence.  The cap is intentionally larger for open research than for the
    attachment verification route.  Exhaustion is a soft evidence boundary:
    Research must preserve unresolved items in its brief, not fail the deck.
    """
    role = str(getattr(agent, "role", "") or "").lower()
    if role not in {"research", "image"}:
        return ""
    scope = str(getattr(agent, "evidence_scope", "") or "open_research")
    pages = int(getattr(agent, "requested_slide_count", 0) or 0)
    if role == "image":
        # Image search already returns direct image candidates and their
        # source pages. Keep enough room for one focused acquisition pass,
        # while stopping synonym/source-page loops before they consume a run.
        maximum = 12 if pages <= 12 else 16
    elif scope == "verify_external":
        maximum = 6
    elif 0 < pages <= 8:
        maximum = 6
    elif 0 < pages <= 15:
        maximum = 8
    else:
        maximum = 12
    lock = getattr(agent, "_research_search_budget_lock", None)
    if lock is None:
        lock = threading.Lock()
        agent._research_search_budget_lock = lock
    with lock:
        normalized_query = re.sub(r"\s+", " ", str(query or "")).strip().casefold()
        seen_queries = set(getattr(agent, "_research_search_queries", set()) or set())
        if normalized_query and normalized_query in seen_queries:
            if role == "image":
                agent.image_search_closed = True
            return (
                "web_search duplicate_query：相同 query 已在本 Agent 中执行；复用已有结果，"
                + (
                    "Image 搜索阶段现已关闭；使用已返回候选与 image_generate 完成 catalog，"
                    "不可得项标为 failed 后 finalize，不要再搜索。"
                    if role == "image"
                    else "不要并行或串行重复同一检索。"
                )
            )
        if normalized_query:
            seen_queries.add(normalized_query)
            agent._research_search_queries = seen_queries
        used = int(getattr(agent, "_research_search_calls", 0) or 0)
        if used >= maximum:
            if role == "image":
                return (
                    f"web_search budget_exhausted：本 Image 已达到 {maximum} 次工具级素材检索上限。"
                    "停止更换同义 query；保留已通过的素材，把所有已知不合格项合并为"
                    "唯一一次 `image.py fetch . --replace`，仍不可得则在 catalog 标为 "
                    "failed 并结构化交接，不得继续抽图。"
                )
            return (
                "web_search budget_exhausted：本 Research 已达到 "
                f"{maximum} 次工具级检索上限（每次空结果已自动简化重试）。"
                "请立即使用已有权威证据写 research/knowledge-brief.md；仍未闭合的"
                "事项列为 unresolved/boundary，不要继续换同义 query。此状态不应"
                "阻断整套演示。"
            )
        agent._research_search_calls = used + 1
    return ""


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
    budget = _consume_research_search_budget(agent, str(query))
    if budget:
        return budget
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


def _consume_web_extract_budget(agent, url: str) -> str:
    """Deduplicate and bound source-page extraction for evidence roles."""
    role = str(getattr(agent, "role", "") or "").lower()
    if role not in {"research", "image"}:
        return ""
    scope = str(getattr(agent, "evidence_scope", "") or "open_research")
    pages = int(getattr(agent, "requested_slide_count", 0) or 0)
    if role == "image":
        maximum = 24
    elif scope == "verify_external":
        maximum = 6
    elif 0 < pages <= 8:
        maximum = 6
    elif 0 < pages <= 15:
        maximum = 8
    else:
        maximum = 12
    lock = getattr(agent, "_web_extract_budget_lock", None)
    if lock is None:
        lock = threading.Lock()
        agent._web_extract_budget_lock = lock
    canonical = str(url or "").strip().split("#", 1)[0]
    with lock:
        seen = set(getattr(agent, "_web_extract_urls", set()) or set())
        if canonical in seen:
            return (
                "web_extract duplicate_url：相同 URL 已抽取；复用已有正文，不要重复"
                "请求。Image 应改用已得到的 image URL/source，Research 应写入已有证据。"
            )
        used = int(getattr(agent, "_web_extract_calls", 0) or 0)
        if used >= maximum:
            if role == "image":
                return (
                    f"web_extract budget_exhausted：本 Image 已达到 {maximum} 个唯一来源页"
                    "抽取上限。停止扩展候选；保留已通过素材，缺失项在 catalog 标为 failed，"
                    "完成 finalize 后交给对应页面使用代码视觉或审慎 fallback。"
                )
            return (
                f"web_extract budget_exhausted：本 Research 已达到 {maximum} 个唯一来源页"
                "抽取上限。用已有权威证据写 knowledge-brief；未闭合项记录为 boundary。"
            )
        seen.add(canonical)
        agent._web_extract_urls = seen
        agent._web_extract_calls = used + 1
    return ""


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
    budget = _consume_web_extract_budget(agent, str(url))
    if budget:
        return budget
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
        "建议一次写入 2–6 个连续的 plan/slide_NN.md；若 provider 只保留 1 页，Harness "
        "会把它作为 additive compatibility write 只新增该页（绝不会覆盖其他页），"
        "随后继续用本工具写后续缺页，不要改用逐页 write_file。7–32 页自动分批；"
        "模型仍应优先 2–6 页，但已经生成的合法长批次不会被丢弃。"
        "直接传结构化 files；"
        "Harness 负责 JSON 序列化与 apply-plan-batch，禁止手写 plan-batch.json。"
        "首次批次默认只创建缺页；若计划校验前发现已写页面合同错误，可设置 "
        "replace_existing=true 批量恢复。若漏传该值，页面生产前的冲突批次会原子"
        "恢复一次；页面生产开始后禁止覆盖。"
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
                "minItems": 1,
                "maxItems": 32,
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
        "正文 mono/无主题依据的通用视觉套路/入场动画"
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
                    "prompt 必须从 plan/deck.md 的 palette_anchor 带入本 Deck 实际主题色、"
                    "材质与光线，并明确 no text、no watermark；不要复制工具说明中的固定色板。"
                    "返回 assets/ 下的路径。"),
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
        # Research owns one canonical brief.  ``patch`` lets it append or
        # tighten a focused section after the initial write instead of
        # retransmitting an increasingly large whole-file payload.  The same
        # role/path allowlist still confines every edit to that brief.
        "research": ["read_file", "write_file", "patch", "web_search", "web_extract"],
        "image": [
            "read_file", "write_file", "patch", "terminal", "vision_analyze",
            "web_search",
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
    if name == "patch":
        # Some OpenAI-compatible transports retain the older Hermes aliases
        # old_value/new_value even though this runtime advertises
        # old_string/new_string. Normalize that harmless envelope mismatch at
        # the adapter edge so an Image URL correction does not degrade into
        # script introspection or repeated whole-catalog rewrites.
        args = dict(args)
        if "old_string" not in args and isinstance(args.get("old_value"), str):
            args["old_string"] = args.pop("old_value")
        if "new_string" not in args and isinstance(args.get("new_value"), str):
            args["new_string"] = args.pop("new_value")
        args.setdefault("mode", "replace")
    if name == "write_plan_batch":
        # Some Hermes/OpenAI-compatible providers use the tool name as the
        # payload key even though the advertised canonical argument is
        # ``files``. Normalize the envelope at the adapter edge; the tool
        # itself still validates ordering, page count, paths, and overwrite
        # policy exactly once.
        args = dict(args)
        if "files" not in args and "plan_batch" in args:
            args["files"] = args.pop("plan_batch")
    if (
        name == "write_file"
        and "path" not in args
        and isinstance(args.get("content"), str)
        and str(getattr(agent, "role", "") or "").strip().lower() == "slide"
    ):
        # A Single Slide owns exactly one deterministic output path. Some
        # OpenAI-compatible transports occasionally preserve the HTML payload
        # but omit the sibling ``path`` field. Recover only this unambiguous
        # envelope: Grouped/multi-page Slides and every other role still receive
        # the normal missing-parameter error. The regular ownership check in
        # write_file remains authoritative after normalization.
        assigned_pages = tuple(
            page
            for page in (getattr(agent, "assigned_slide_pages", ()) or ())
            if isinstance(page, int) and page > 0
        )
        if len(assigned_pages) == 1:
            args = dict(args)
            args["path"] = f"slides/slide_{assigned_pages[0]:02d}.html"
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
