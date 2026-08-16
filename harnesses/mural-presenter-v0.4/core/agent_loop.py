"""Parallel role harness for ppt-skill-html-clean.

The Harness exposes either one configured instruction edition or both equivalent
editions for a model-visible first-read choice. Workflow and design rules stay
in the Skill; legacy editions may keep root-role details in a role card.
"""
from __future__ import annotations

import base64
import copy
import concurrent.futures as futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter, deque
from pathlib import Path

from . import config, model_call, nova_raw, runtime_capabilities, tools
from .language import infer_deck_language, normalize_language
from .run_profiles import resolve_run_profile
from .trace_mode import write_trace

ROLES = {"material", "research", "image", "slide", "review"}
LEGACY_GROUPED_SKILL_NAMES = {
    "long-horizon-html-ppt-grouped",
    "long-horizon-html-ppt-grouped-inline-image",
}
CURRENT_SKILL_BASE = config.SKILL_NAME.removesuffix("-grouped")
CURRENT_GROUPED_SKILL_NAMES: set[str] = set()
CURRENT_VARIANT_SKILL_NAMES = {CURRENT_SKILL_BASE}
GROUPED_SKILL_NAMES = LEGACY_GROUPED_SKILL_NAMES | CURRENT_GROUPED_SKILL_NAMES
INLINE_IMAGE_SKILL = "long-horizon-html-ppt-grouped-inline-image"
ROLE_SCRIPT_NAMES = {
    "orchestrator": "orchestrator.py",
    "image": "image.py",
    "slide": "slide.py",
    "review": "review.py",
}


def _role_script_path(skill_name: str, role: str) -> str:
    """Return the sole public deterministic entry point exposed to one Role."""
    script_name = ROLE_SCRIPT_NAMES.get(str(role or ""), "")
    if not skill_name or not script_name:
        return ""
    return f"skills/{skill_name}/scripts/{script_name}"


def _is_grouped_skill_name(value: object) -> bool:
    return str(value or "") in GROUPED_SKILL_NAMES


def _planned_ownership_topology(workspace: str) -> str:
    path = Path(workspace) / "plan" / "deck.md"
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = re.search(
        r"(?mi)^\s*-?\s*ownership_topology\s*:\s*(single|grouped)\s*$",
        content,
    )
    return match.group(1).lower() if match else ""


def _ownership_topology(agent) -> str:
    skill_name = str(getattr(agent, "skill_name", "") or "")
    if _is_grouped_skill_name(skill_name):
        return "grouped"
    # The canonical plan may be patched during pre-production recovery. Always
    # prefer its current value over a cached hint so delegation cannot continue
    # under a stale ownership topology.
    workspace = str(getattr(agent, "ws", "") or "")
    planned = _planned_ownership_topology(workspace) if workspace else ""
    if planned:
        agent.ownership_topology = planned
        return planned
    current = str(getattr(agent, "ownership_topology", "") or "").lower()
    return current if current in {"single", "grouped"} else ""


def _uses_grouped_ownership(agent) -> bool:
    return _ownership_topology(agent) == "grouped"


def _uses_legacy_grouped_contract(value: object) -> bool:
    """Return whether the older grouped workflow, not just grouped ownership, applies."""
    return str(value or "") in LEGACY_GROUPED_SKILL_NAMES


def _is_current_variant_skill_name(value: object) -> bool:
    return str(value or "") in CURRENT_VARIANT_SKILL_NAMES


def _is_inline_image_skill_name(value: object) -> bool:
    return str(value or "") == INLINE_IMAGE_SKILL
CONTINUE_PROMPT = "[系统] 上一条回复被长度限制截断了。请从断点继续。"
NUDGE_PROMPT = "[系统] 请调用工具继续任务，或用一段可见文字总结已完成的结果。"
EMPTY_RECOVERY_PROMPTS = (
    "[系统] 上一轮响应为空。不要复述或重新规划；请从工作区当前产物继续，并立即调用一个能推进任务的具体工具。",
    "[系统] 连续收到空响应。请把工作区正式文件视为真相源，必要时先读取当前计划，然后只执行下一项具体操作；必须返回可见文字或工具调用。",
    "[系统] 响应仍为空。停止长篇内部推演，选择当前最小可执行步骤并调用工具；若确实无法继续，请用可见文字准确说明阻塞原因。",
)
FINAL_PROMPT = "[系统] 立即停止工具调用，用一段简短文字总结已完成内容和遗留问题。"


def _runtime_time_context(started_epoch: float, language: str) -> str:
    """Return one authoritative, stable temporal anchor for every role."""
    started_utc = time.strftime(
        "%Y-%m-%d %H:%M:%S UTC",
        time.gmtime(float(started_epoch)),
    )
    if language == "zh":
        return (
            f"权威时间上下文：本任务开始于 {started_utc}。判断已发生、当前、未来和"
            "资料日期时以此为准，不使用模型记忆中的当前日期。"
        )
    return (
        f"Authoritative time context: this task started at {started_utc}. "
        "Use this timestamp for past/current/future and source-date judgments; "
        "do not rely on a remembered current date."
    )


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _persist_image_handoff(agent) -> None:
    """Persist the validated ready/failed partition for deterministic audit."""
    _write_json(
        Path(agent.ws) / "_trace" / "image-handoff.json",
        {
            "schema": "mural.image-handoff.v1",
            "ready_pages": list(getattr(agent, "image_ready_pages", ()) or ()),
            "failed_pages": list(getattr(agent, "image_failed_pages", ()) or ()),
            "summary": str(getattr(agent, "image_handoff_summary", "") or "")[-1800:],
        },
    )


def _image_handoff_declared_pages(text: str, field: str) -> set[int]:
    """Parse an Image ready/failed declaration without imposing prose style.

    Image agents commonly use Markdown emphasis and put failed pages in a
    bullet list below an otherwise empty ``failed:`` heading.  The handoff is
    a semantic contract, so accepting only an unadorned one-line spelling
    needlessly turns a truthful bounded failure into a second Image dispatch.
    Keep the parser narrow: page numbers are read from the heading value, or
    from page-leading bullets in the same section, never from arbitrary prose
    such as pixel dimensions or source dates.
    """
    token = re.escape(field)
    heading = re.search(
        rf"(?mi)^\s*(?:[-+]\s*)?(?:\*\*|__)?{token}\s*:\s*"
        rf"(.*?)(?:\*\*|__)?\s*$",
        str(text or ""),
    )
    if not heading:
        return set()
    inline = re.sub(r"(?:\*\*|__)\s*$", "", heading.group(1)).strip()
    pages = {int(value) for value in re.findall(r"\b\d{1,3}\b", inline)}
    if pages:
        return pages
    tail = str(text or "")[heading.end():]
    for line in tail.splitlines():
        if re.match(r"^\s*##(?:\s|$)", line):
            break
        if re.match(
            r"^\s*(?:[-+]?\s*)?(?:\*\*|__)?(?:bitmap_ready|failed)\s*:",
            line,
            flags=re.IGNORECASE,
        ):
            break
        bullet = re.match(
            r"^\s*[-+]\s*(?:`)?(?:slide\s*)?(\d{1,3})\b",
            line,
            flags=re.IGNORECASE,
        )
        if bullet:
            pages.add(int(bullet.group(1)))
    return pages


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _tool_action_signature(use) -> str:
    payload = {
        "name": str(getattr(use, "name", "")),
        "input": getattr(use, "input", {})
        if isinstance(getattr(use, "input", {}), dict)
        else {},
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _tool_result_text(result: dict) -> str:
    content = result.get("content", "")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            parts.append(str(item))
        elif item.get("type") == "text":
            parts.append(str(item.get("text", "")))
        elif item.get("type") == "image":
            parts.append("[image]")
    return "\n".join(parts)


def _tool_turn_signature(tool_uses, tool_results: list[dict]) -> str:
    payload = [
        {
            "action": _tool_action_signature(use),
            "result": _tool_result_text(result),
        }
        for use, result in zip(tool_uses, tool_results)
    ]
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _collapse_same_turn_tool_uses(content) -> tuple[list, list, int]:
    """Collapse exact duplicate tool calls emitted in one model response.

    This is a generic protocol guard, not a task rule. A duplicated deterministic
    call has no new information and can otherwise execute hundreds of times before
    the cross-turn progress guard gets a chance to observe it. Image generation is
    exempt because identical prompts may intentionally request independent variants.
    """

    blocks: list = []
    calls: list = []
    seen: set[str] = set()
    collapsed = 0
    for block in content:
        if block.type != "tool_use":
            blocks.append(block)
            continue
        signature = _tool_action_signature(block)
        if block.name != "image_generate" and signature in seen:
            collapsed += 1
            continue
        seen.add(signature)
        blocks.append(block)
        calls.append(block)
    return blocks, calls, collapsed


_ERROR_RESULT_RE = re.compile(
    r"(?:\u9519\u8bef\uff1a|\u5de5\u5177\u7b56\u7565\u8fdd\u89c4|\[exit_code=[1-9]\d*\]|"
    r"traceback|\b(?:failed|failure|timed?\s*out|timeout)\b|"
    r"\bhttp\s+(?:4\d\d|5\d\d)\b|\b(?:429|403)\b|render stop line)",
    flags=re.I,
)


def _tool_error_signature(tool_uses, tool_results: list[dict]) -> str:
    errors: list[str] = []
    for use, result in zip(tool_uses, tool_results):
        text = _tool_result_text(result)
        if not _ERROR_RESULT_RE.search(text):
            continue
        normalized = re.sub(r"https?://\S+", "<url>", text[:1200], flags=re.I)
        normalized = re.sub(
            r"\b[0-9a-f]{12,}\b|\b\d{5,}\b", "<id>", normalized, flags=re.I
        )
        normalized = re.sub(r"\s+", " ", normalized).strip().casefold()
        errors.append(f"{getattr(use, 'name', '')}:{normalized}")
    if not errors:
        return ""
    return hashlib.sha256("\n".join(sorted(errors)).encode("utf-8")).hexdigest()


_UNRECOVERABLE_ERROR_FAMILIES = (
    (
        "closed_responsibility",
        re.compile(
            r"(?:责任单元\s+\S+\s+已结束|不是允许创建新\s*Agent|"
            r"普通生产中\s*repair=true|already completed responsibility)",
            re.I,
        ),
    ),
    (
        "duplicate_delegation",
        re.compile(r"(?:重复委派错误|禁止.*重复委派|duplicate delegation)", re.I),
    ),
    (
        "phase_order",
        re.compile(r"(?:阶段顺序错误|stage order error)", re.I),
    ),
    (
        "tool_policy",
        re.compile(
            r"(?:工具策略违规|不允许动作|只允许动作|tool policy violation)", re.I
        ),
    ),
    (
        "stale_renderer_preview",
        re.compile(
            r"(?:controlled temporary directory retained files|workspace output audit failed)"
            r"[\s\S]{0,1200}preview_slide_\d{2}",
            re.I,
        ),
    ),
)


def _tool_unrecoverable_error_families(tool_results: list[dict]) -> set[str]:
    """Classify high-confidence policy failures independent of varied arguments."""
    families: set[str] = set()
    for result in tool_results:
        text = _tool_result_text(result)
        for family, pattern in _UNRECOVERABLE_ERROR_FAMILIES:
            if pattern.search(text):
                families.add(family)
    return families


class _ProgressGuard:
    """Generic repeated-action guard; it has no knowledge of PPT semantics."""

    _EXCLUDED_ROOTS = {"_trace", "skills", "inputs"}

    def __init__(self, workspace: str):
        self.root = Path(workspace)
        self.file_cache: dict[str, tuple[int, int, str]] = {}
        self.workspace_signature = self._workspace_signature()
        self.seen_actions: set[str] = set()
        self.action_counts: Counter[str] = Counter()
        self.last_turn_signature = ""
        self.identical_turns = 0
        self.last_error_signature = ""
        self.error_turns = 0
        self.error_family_counts: Counter[str] = Counter()
        self.no_progress_turns = 0
        self.warned: set[str] = set()

    def _workspace_signature(self) -> str:
        rows: list[tuple[str, str]] = []
        if not self.root.is_dir():
            return ""
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(self.root)
            if relative.parts and relative.parts[0] in self._EXCLUDED_ROOTS:
                continue
            if "__pycache__" in relative.parts or path.suffix == ".pyc":
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            key = relative.as_posix()
            cached = self.file_cache.get(key)
            marker = (stat.st_size, stat.st_mtime_ns)
            # Some shared filesystems keep the same nanosecond mtime for two
            # rapid, same-size rewrites. Canonical text artifacts are small,
            # so hash them again instead of trusting a stale stat marker;
            # retain the cache only for large binary assets.
            if cached and cached[:2] == marker and stat.st_size > 1_000_000:
                digest = cached[2]
            else:
                try:
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError:
                    continue
                self.file_cache[key] = (stat.st_size, stat.st_mtime_ns, digest)
            rows.append((key, digest))
        return hashlib.sha256(_canonical_json(rows).encode("utf-8")).hexdigest()

    def observe(self, tool_uses, tool_results: list[dict]) -> tuple[str, str]:
        actions = [_tool_action_signature(use) for use in tool_uses]
        novel_action = any(action not in self.seen_actions for action in actions)
        self.seen_actions.update(actions)
        self.action_counts.update(actions)

        turn_signature = _tool_turn_signature(tool_uses, tool_results)
        if turn_signature == self.last_turn_signature:
            self.identical_turns += 1
        else:
            self.last_turn_signature = turn_signature
            self.identical_turns = 1

        error_signature = _tool_error_signature(tool_uses, tool_results)
        if error_signature and error_signature == self.last_error_signature:
            self.error_turns += 1
        elif error_signature:
            self.last_error_signature = error_signature
            self.error_turns = 1
        else:
            self.last_error_signature = ""
            self.error_turns = 0

        new_workspace_signature = self._workspace_signature()
        workspace_changed = new_workspace_signature != self.workspace_signature
        self.workspace_signature = new_workspace_signature
        error_families = _tool_unrecoverable_error_families(tool_results)
        if workspace_changed:
            # A real artifact change starts a new progress window. Re-reading
            # the same source in a later phase must not inherit stale counts.
            self.action_counts.clear()
            self.action_counts.update(actions)
            self.identical_turns = 1
            self.error_turns = 1 if error_signature else 0
            self.error_family_counts.clear()
            self.warned.clear()
        self.error_family_counts.update(error_families)
        if workspace_changed or novel_action:
            self.no_progress_turns = 0
        else:
            self.no_progress_turns += 1

        repeated_action = max(
            (self.action_counts[action] for action in actions),
            default=0,
        )
        repeated_error_family = max(
            self.error_family_counts.values(),
            default=0,
        )
        checks = (
            (
                "identical",
                self.identical_turns,
                config.STALL_IDENTICAL_TURNS,
                "\u8fde\u7eed\u91cd\u590d\u5b8c\u5168\u76f8\u540c\u7684\u5de5\u5177\u8c03\u7528\u4e0e\u7ed3\u679c",
            ),
            (
                "action",
                repeated_action,
                config.STALL_ACTION_REPEATS,
                "\u540c\u4e00\u5de5\u5177\u53c2\u6570\u88ab\u8fc7\u5ea6\u91cd\u590d",
            ),
            (
                "error",
                self.error_turns,
                config.STALL_ERROR_TURNS,
                "\u8fde\u7eed\u6536\u5230\u540c\u4e00\u7c7b\u4e0d\u53ef\u6062\u590d\u9519\u8bef",
            ),
            (
                "error_family",
                repeated_error_family,
                config.STALL_ERROR_TURNS,
                "\u5728\u4ea7\u7269\u672a\u53d8\u5316\u65f6\u53cd\u590d\u7ed5\u8fc7\u540c\u4e00\u7c7b\u6743\u9650\u6216\u9636\u6bb5\u9519\u8bef",
            ),
            (
                "progress",
                self.no_progress_turns,
                config.STALL_NO_PROGRESS_TURNS,
                "\u5de5\u5177\u884c\u4e3a\u4e0e\u5de5\u4f5c\u533a\u4ea7\u7269\u5747\u65e0\u65b0\u8fdb\u5c55",
            ),
        )
        for key, value, limit, description in checks:
            if value >= limit:
                return "stop", f"{description}\uff08{value}/{limit}\uff09"
        for key, value, limit, description in checks:
            if value >= max(2, limit - 1) and key not in self.warned:
                self.warned.add(key)
                return "warn", f"{description}\uff08{value}/{limit}\uff09"
        return "ok", ""


def _history_capsule(messages: list[dict]) -> str:
    tool_counts: Counter[str] = Counter()
    recent_actions: deque[str] = deque(maxlen=8)
    recent_notes: deque[str] = deque(maxlen=6)
    recent_errors: deque[str] = deque(maxlen=6)
    for message in messages:
        content = message.get("content")
        blocks = content if isinstance(content, list) else [
            {"type": "text", "text": str(content or "")}
        ]
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "tool_use":
                name = str(block.get("name", "tool"))
                tool_counts[name] += 1
                args = block.get("input", {})
                recent_actions.append(f"{name}({_canonical_json(args)[:280]})")
            elif block_type == "tool_result":
                text = _tool_result_text(block)
                if _ERROR_RESULT_RE.search(text):
                    recent_errors.append(re.sub(r"\s+", " ", text)[:500])
            elif block_type == "text":
                text = re.sub(r"\s+", " ", str(block.get("text", ""))).strip()
                if text and not text.startswith("[Harness context capsule]"):
                    recent_notes.append(text[:500])
    lines = [
        "[Harness context capsule]",
        f"Archived messages: {len(messages)}. Full text remains in _trace/orchestrator or the child trace.",
    ]
    if tool_counts:
        lines.append(
            "Tool counts: "
            + ", ".join(f"{name}={count}" for name, count in sorted(tool_counts.items()))
        )
    if recent_actions:
        lines.append("Recent archived actions:\n- " + "\n- ".join(recent_actions))
    if recent_errors:
        lines.append("Unresolved/recent errors:\n- " + "\n- ".join(recent_errors))
    if recent_notes:
        lines.append("Recent archived notes:\n- " + "\n- ".join(recent_notes))
    lines.append(
        "Re-read canonical workspace files when exact prior content is needed; do not repeat a tool only to reconstruct history."
    )
    return "\n".join(lines)


def _compact_live_history(messages: list[dict], keep_recent: int) -> bool:
    """Compact only the live request; callers retain a separate full trace."""
    if len(messages) <= keep_recent + 3:
        return False
    cutoff = len(messages) - keep_recent
    if cutoff % 2 == 0:
        cutoff -= 1
    if cutoff <= 1:
        return False
    archived = messages[1:cutoff]
    recent = messages[cutoff:]
    messages[:] = [
        messages[0],
        {"role": "assistant", "content": _history_capsule(archived)},
        {
            "role": "user",
            "content": (
                "[\u7cfb\u7edf] \u4ee5\u4e0a\u662f\u5386\u53f2\u538b\u7f29\u80f6\u56ca\u3002\u7ee7\u7eed\u5f53\u524d\u4efb\u52a1\uff0c\u4ee5\u5de5\u4f5c\u533a\u6b63\u5f0f\u4ea7\u7269\u4e3a\u771f\u76f8\u6e90\u3002"
            ),
        },
        *recent,
    ]
    return True


def _compact_live_history_for_profile(
    agent: object,
    messages: list[dict],
    keep_recent: int,
) -> bool:
    """Apply active-history compaction only for profiles that permit it."""
    profile = getattr(agent, "profile", None) or resolve_run_profile("inference")
    if not profile.compact_active_history:
        return False
    return _compact_live_history(messages, keep_recent)


def _live_history_chars(messages: list[dict]) -> int:
    return len(_canonical_json(messages))


def _response_language_rule(language: str) -> str:
    """Return one stable rule for visible agent prose, independent of Skill edition."""
    if language == "zh":
        return (
            "所有可见的过程说明、工具调用前说明和最终状态都使用中文；代码、命令、"
            "路径、原文引语和专有名词可以保留原文。不要因为 HTML/CSS 或英文工具名"
            "切换整段回复语言。"
        )
    if language == "en":
        return (
            "Use English for all visible progress notes, tool-call preambles, and final "
            "status messages. Code, commands, paths, source quotations, and proper nouns "
            "may remain in their original form; do not switch the surrounding prose just "
            "because HTML/CSS or tool names are English."
        )
    return (
        "Use the user's primary requested delivery language for all visible progress notes, "
        "tool-call preambles, and final status messages."
    )


def _runtime_capability_context(profile: dict, language: str) -> str:
    """Render the deterministic capability overlay injected after the frozen Skill."""
    if not profile:
        return ""
    roles = ", ".join(str(role) for role in profile.get("active_roles", [])) or "none"
    omitted = ", ".join(str(role) for role in profile.get("omitted_roles", [])) or "none"
    workflow = profile.get("workflow") if isinstance(profile.get("workflow"), dict) else {}
    tool_flags = profile.get("tools") if isinstance(profile.get("tools"), dict) else {}
    inputs = profile.get("inputs") if isinstance(profile.get("inputs"), dict) else {}
    requirements = (
        profile.get("requirements")
        if isinstance(profile.get("requirements"), dict)
        else {}
    )
    bitmap_required = bool(requirements.get("bitmap_required"))
    visual_assets = ", ".join(str(path) for path in inputs.get("visual_asset_paths", [])) or "none"
    style_refs = ", ".join(str(path) for path in inputs.get("style_reference_paths", [])) or "none"
    if language == "zh":
        return f"""运行时能力合同（由 Harness 启动前探测，优先级高于 Skill 中的静态可选路线）：
- 可委派角色：{roles}
- 本次省略角色：{omitted}
- Material：{workflow.get('material', 'omitted')}；交接到：{workflow.get('material_handoff', 'none')}
- Research：{workflow.get('research', 'omitted')}（mode={workflow.get('research_mode', 'off')}；原因={workflow.get('research_reason', 'unspecified')}）
- Material 状态：{workflow.get('material_stage', 'omitted')}
- 用户直供视觉素材：{visual_assets}
- 风格参考图：{style_refs}
- Image：{workflow.get('image', 'bitmap_unavailable')}
- 用户位图硬约束：{'至少一页必须 needs_bitmap:true；获取失败保持 image_blocked，不得静默降级' if bitmap_required else '无'}
- Vision Critic：{workflow.get('vision', 'disabled')}（独立无状态请求，只回传结构化文字）
- web_search/web_extract：{'可用' if tool_flags.get('web_search') else '不可用，不得委派 Research 或伪造外部检索'}
- image_generate：{'可用' if tool_flags.get('image_generate') else '不可用，不得写入生成图路线'}
只执行本合同列出的角色和工具。Material 交接到 Research 时，Orchestrator 禁止打开
`research/material.md`，只能等 Research 写完并读取 `research/knowledge-brief.md`；
Material 直接交接到 Orchestrator 时，才基于 `research/material.md` 规划，未核实事实必须明确保留为边界。Material 状态为
`direct_text` 时直接读取 Harness 生成的 `research/material.md`，不要委派 Material；
状态为 omitted 时不得探测该文件。`用户直供视觉素材` 列表也包含用户明确要求复用的
文档逐页像素；当语义视觉需求指向其中已有的照片/Figure 时，Orchestrator 必须规划为
`bitmap-material`，由 Image 裁取独立主体，不能误写成需要联网的 `bitmap-real`。直供视觉
素材只交给 Image；风格参考图由 Orchestrator 在规划前用 Vision 各查看一次，不得当作
内容证据。Image=bitmap_unavailable 时，每页必须写
`needs_bitmap: false`，不得委派 Image。"""
    return f"""Runtime capability contract (detected by the Harness before execution and
authoritative over optional routes described by the frozen Skill):
- delegable roles: {roles}
- omitted roles: {omitted}
- Material: {workflow.get('material', 'omitted')}; handoff: {workflow.get('material_handoff', 'none')}
- Research: {workflow.get('research', 'omitted')} (mode={workflow.get('research_mode', 'off')}; reason={workflow.get('research_reason', 'unspecified')})
- Material stage: {workflow.get('material_stage', 'omitted')}
- User-supplied visual assets: {visual_assets}
- Style-reference images: {style_refs}
- Image: {workflow.get('image', 'bitmap_unavailable')}
- User bitmap hard requirement: {'at least one needs_bitmap:true page; acquisition failure remains image_blocked and must not be silently downgraded' if bitmap_required else 'none'}
- Vision Critic: {workflow.get('vision', 'disabled')} (isolated stateless request; structured text only)
- web_search/web_extract: {'available' if tool_flags.get('web_search') else 'unavailable; do not delegate Research or claim external retrieval'}
- image_generate: {'available' if tool_flags.get('image_generate') else 'unavailable; do not plan generated-image routes'}
Use only the roles and tools listed here. When Material hands off to Research, the
Orchestrator must not open `research/material.md`; wait for and read only
`research/knowledge-brief.md`. Only when Material hands off directly to the
Orchestrator may it plan from `research/material.md`, preserving unresolved facts as
explicit boundaries. When Material stage is `direct_text`, read the Harness-generated
`research/material.md` without delegating Material. When omitted, do not probe it.
The supplied-visual list also contains rendered document pages when the user explicitly
asks to reuse photographs or figures from an attachment. When a requested visual already
exists there, the Orchestrator must plan `bitmap-material` and let Image crop the independent
subject; it must not misroute that asset as network-dependent `bitmap-real`. Only Image
consumes supplied visual assets. Before planning, the Orchestrator views each style-reference
image once; never treat it as factual evidence.
When Image=bitmap_unavailable, every slide must set `needs_bitmap: false` and Image
must not be delegated."""


def _direct_text_injection(workspace: str, language: str, limit: int = 60000) -> str:
    """Inject direct text once so agents do not page through a deterministic handoff.

    Short handoffs are included verbatim. Large handoffs become a deterministic
    source map containing the document metadata, every Markdown heading, and
    bounded opening/closing evidence. The canonical file remains available for
    a targeted lookup, but manual sequential pagination is no longer the default.
    """
    path = Path(workspace) / "research" / "material.md"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) <= limit:
        payload = text
        mode = "verbatim"
    else:
        headings = [
            line for line in text.splitlines()
            if re.match(r"^#{1,6}\s+\S", line)
        ]
        heading_block = "\n".join(headings)[:12000]
        head_budget = max(12000, (limit - len(heading_block)) // 2)
        tail_budget = max(8000, limit - len(heading_block) - head_budget)
        payload = (
            text[:head_budget].rstrip()
            + "\n\n[... deterministic middle source map ...]\n"
            + heading_block
            + "\n\n[... deterministic closing evidence ...]\n"
            + text[-tail_budget:].lstrip()
        )[:limit]
        mode = "compact_source_map"
    if language == "zh":
        lead = (
            "以下是 Harness 在首次调用前一次性注入的直接文本证据。"
            "无需分页读取 research/material.md；只有确需核对未包含的具体字段时才做一次定向读取。"
        )
    else:
        lead = (
            "The Harness injected this direct-text evidence once before the first call. "
            "Do not page through research/material.md; make one targeted read only when a "
            "specific omitted field must be verified."
        )
    return (
        f"<direct_text_handoff mode=\"{mode}\" source=\"research/material.md\">\n"
        f"{lead}\n\n{payload.rstrip()}\n"
        "</direct_text_handoff>"
    )


def _style_lock_injection(workspace: str, language: str, limit: int = 14000) -> str:
    """Extract the canonical compact Style Lock for every visual worker."""
    path = Path(workspace) / "plan" / "deck.md"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    matches = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", text))
    wanted = {
        "visual contract", "视觉契约", "style lock", "视觉锁",
        "theme tokens", "主题变量", "special pages", "特殊页",
    }
    blocks: list[str] = []
    for index, match in enumerate(matches):
        heading = re.sub(r"\s+", " ", match.group(1).strip().lower())
        if heading not in wanted:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        blocks.append(text[match.start():end].strip())
    if not blocks:
        return ""
    payload = "\n\n".join(blocks)[:limit]
    rule = (
        "这是 Harness 从 canonical deck.md 提取的整册 Style Lock。必须兑现其主题证据、"
        "背景配方、字体角色、图片处理与反默认项；不得自行换成通用暗底模板。"
        if language == "zh"
        else "This is the canonical deck-level Style Lock extracted by the Harness. "
        "Honor its subject evidence, background recipe, font roles, image treatment, "
        "and anti-defaults; do not replace it with a generic dark template."
    )
    return f"<style_lock source=\"plan/deck.md\">\n{rule}\n\n{payload}\n</style_lock>"


def _planned_delivery_language(workspace: str) -> str:
    """Read the resolved plan language when planning has already completed."""
    path = Path(workspace) / "plan" / "deck.md"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = re.search(r"(?mi)^\s*-\s*language\s*:\s*([^\s#]+)", text)
    return normalize_language(match.group(1)) if match else ""


def _orchestrator_system(
    skill_name: str,
    skill_language: str,
    staged_materials: tuple[str, ...] = (),
    response_language: str = "auto",
    image_generation_enabled: bool = True,
    runtime_profile: dict | None = None,
) -> str:
    """Keep the Harness thin; read the selected Skill contract at runtime."""
    root_workflow = skill_name == "long-horizon-html-ppt-grouped"
    startup_zh = (
        f"开工第一步读取 `skills/{skill_name}/SKILL.md`。\n"
        "随后严格按该入口 Skill 的根工作流执行。"
        if root_workflow
        else (
            f"开工第一步读取 `skills/{skill_name}/SKILL.md`。\n"
            "Orchestrator 角色卡已由 Harness 完整注入 system context，"
            "不要再用 read_file 分页读取它。"
        )
    )
    startup_en = (
        f"Your first action is to read `skills/{skill_name}/SKILL.md`.\n"
        "Then follow that root Skill for both workflow and responsibility."
        if root_workflow
        else (
            f"Your first action is to read `skills/{skill_name}/SKILL.md`.\n"
            "The Harness has injected the complete Orchestrator role card into the "
            "system context; do not page-read that file."
        )
    )
    runtime_inputs = (runtime_profile or {}).get("inputs") or {}
    material_paths = list(runtime_inputs.get("material_agent_paths") or [])
    material_lines = "\n".join(f"- `{path}`" for path in material_paths)
    material_context_zh = (
        "\n系统已挂载以下附件路径；读完角色说明后，把这些路径交给一个 Material：\n"
        f"{material_lines}\n"
        if material_lines
        else ""
    )
    material_context_en = (
        "\nThe system mounted these attachment paths. After reading the role "
        "instructions, pass them to one Material task:\n"
        f"{material_lines}\n"
        if material_lines
        else ""
    )
    profile_roles = list((runtime_profile or {}).get("active_roles") or ROLES)
    runtime_tools = (runtime_profile or {}).get("tools") or {}
    search_enabled = bool(runtime_tools.get("web_search", True))
    if skill_language == "zh":
        tool_surface_zh = (
            "运行时工具面：web_search/web_extract 本次"
            + ("已启用；" if search_enabled else "未启用；")
            + "image_generate 本次"
            + ("已启用。" if image_generation_enabled else "未启用。")
        )
        child_roles_zh = "、".join(profile_roles)
        return f"""\
你是 HTML 演示文稿 Orchestrator，负责受众、叙事、设计系统、角色编排和最终交付。
固定任务身份：当前调用始终是静态 HTML 演示文稿生产，不是开放域聊天；首条用户消息
只是内容 brief。即使 brief 只有 `test`、`hello` 或一句无法确定主题的短句，也必须读取
Skill、调用工具并交付一套简短完整的示范稿；仅当用户没有指定总页数时，才默认包含
封面、内容页与结尾页。用户指定的总页数始终优先，不得为特殊页扩页。不得以
“连接正常 / 我可以帮你 / 想做什么”等通用寒暄结束。
Harness 已选择中文说明版 `{skill_name}`；本任务不得切换 Skill 版本。
Harness 已在首次模型调用前完成确定性工作区准备。
{tool_surface_zh}只把实际可用的路线写进计划。
{_response_language_rule(response_language)}

{startup_zh}
{material_context_zh}
按其中导航按需读取 references；不要一次通读整个 references 或 base.css。
确定性命令由你直接执行；子角色只使用
{child_roles_zh}。结束时简短报告交付路径与遗留问题。
"""
    else:
        tool_surface_en = (
            "Runtime tool surface: web_search/web_extract are "
            + ("enabled; " if search_enabled else "disabled; ")
            + "image_generate is "
            + ("enabled." if image_generation_enabled else "disabled.")
        )
        child_roles_en = ", ".join(profile_roles)
        return f"""\
You are the HTML presentation Orchestrator responsible for audience, narrative,
the design system, role coordination, and final delivery. This invocation is always
a static HTML presentation production job, never an open-ended chat. Treat the first
user message only as the content brief. Even when it is merely `test`, `hello`, or an
otherwise underspecified phrase, read the Skill, use tools, and deliver a compact but
complete demonstration deck. Only when the user did not specify a total slide count,
default to a cover, content slide, and closing slide. An explicit total always wins;
never add slides merely to preserve special pages.
Never end with a generic readiness or connection-test response. The Harness selected the
English instruction edition `{skill_name}`; do not switch Skill editions.
{tool_surface_en} Plan only routes that are available.
{_response_language_rule(response_language)}

{startup_en}
{material_context_en}
Read references only when their navigation says they are relevant; do not preload the
entire reference library or base.css. The Harness completed deterministic
workspace preparation before the first model call; run the remaining deterministic
deck commands yourself. The only child roles are {child_roles_en}. Finish with a
concise delivery summary and any remaining issue.
"""


def _auto_orchestrator_system(
    staged_materials: tuple[str, ...] = (),
    response_language: str = "auto",
    image_generation_enabled: bool = True,
    runtime_profile: dict | None = None,
) -> str:
    """Route a model through one of two equivalent instruction editions."""
    runtime_inputs = (runtime_profile or {}).get("inputs") or {}
    material_paths = list(runtime_inputs.get("material_agent_paths") or [])
    material_lines = "\n".join(f"- `{path}`" for path in material_paths)
    material_context = (
        "\nMounted attachments (pass these paths to one Material task after "
        "choosing the instruction edition):\n"
        f"{material_lines}\n"
        if material_lines
        else ""
    )
    profile_roles = list((runtime_profile or {}).get("active_roles") or ROLES)
    runtime_tools = (runtime_profile or {}).get("tools") or {}
    search_enabled = bool(runtime_tools.get("web_search", True))
    return f"""\
You are the HTML presentation Orchestrator responsible for audience, narrative,
the design system, role coordination, and final delivery. This invocation is always
a static HTML presentation production job, never an open-ended chat. Treat the first
user message only as the content brief. Even when it is merely `test`, `hello`, or an
otherwise underspecified phrase, read one Skill, use tools, and deliver a compact but
complete demonstration deck. Only when the user did not specify a total slide count,
default to a cover, content slide, and closing slide. An explicit total always wins;
never add slides merely to preserve special pages.
Never end with a generic readiness or connection-test response.
Runtime tool surface: web_search/web_extract are {'enabled' if search_enabled else 'disabled'};
image_generate is {'enabled' if image_generation_enabled else 'disabled'}. Plan only routes that are available.
{_response_language_rule(response_language)}

Two behaviorally equivalent instruction editions are available:
- `skills/{config.SKILL_NAME_ZH}/SKILL.md` (Chinese instructions)
- `skills/{config.SKILL_NAME_EN}/SKILL.md` (English instructions)

Choose the one instruction language you can follow most reliably for the user's
request. This choice does not determine the presentation's delivery language;
derive delivery language only from the user's query. Your first action must be
to read exactly one of the two `SKILL.md` files. That first Skill read locks the
edition for this Deck, so do not inspect or compare both editions. Follow the
chosen Skill for workflow, then read its matching `roles/orchestrator.md` for
root-role responsibility.
{material_context}
Read references only when the chosen Skill says they are relevant; do not preload
the full reference library or base.css. The workspace is already prepared. The
only child roles are {', '.join(profile_roles)}. Finish with a
concise delivery summary and any remaining issue.
"""


def _child_system(
    role: str,
    skill_name: str,
    skill_language: str,
    response_language: str = "auto",
    raw_user_query: str = "",
    revision_instruction: str = "",
) -> str:
    """Give children a small routing prompt; Harness injects the full role card."""
    raw_query = str(raw_user_query or "").strip()
    revision_query = str(revision_instruction or "").strip()
    raw_query_context = ""
    if role == "research" and raw_query:
        encoded_query = json.dumps(raw_query, ensure_ascii=False)
        raw_query_context = (
            "\nHarness 注入的只读 `raw_user_query`（JSON 字符串，逐字保留）：\n"
            f"{encoded_query}\n"
            + (
                "Harness 注入的最新用户修改要求（逐字保留）：\n"
                f"{json.dumps(revision_query, ensure_ascii=False)}\n"
                "把原始请求与最新修改视为一份有时序的用户合同；明确的最新修改优先，"
                "但不得丢失未被修改的原始约束。"
                if revision_query
                else ""
            )
            + "委派 goal 只是 Orchestrator 的研究假设；若它与用户合同冲突，必须以用户合同为准，"
            "先检索其中完整中心短语并在 brief 中纠偏。"
            if skill_language == "zh"
            else
            "\nHarness-injected immutable `raw_user_query` (a verbatim JSON string):\n"
            f"{encoded_query}\n"
            + (
                "Harness-injected latest user revision (verbatim):\n"
                f"{json.dumps(revision_query, ensure_ascii=False)}\n"
                "Treat the original request and latest revision as a chronological user "
                "contract. An explicit latest revision controls while all unmodified original "
                "constraints remain in force."
                if revision_query
                else ""
            )
            + "The delegated goal is only the Orchestrator's research hypothesis. If it conflicts "
            "with the user contract, preserve the contract, search its complete head phrase first, "
            "and correct the brief."
        )
    material_note = (
        "\nResearch 已收到 research/material.md 时不要重新打开 inputs/ 原附件；只有笔记明确缺失内容时才回查。"
        if role == "research" and skill_language == "zh"
        else
        "\nWhen research/material.md is available, Research must not reopen raw files under inputs/ unless the note explicitly reports missing content."
        if role == "research"
        else ""
    )
    if skill_language == "zh":
        return f"""\
你是 Static HTML Presentation 的 {role.upper()} 角色。
Harness 已选择 `{skill_name}`。{_response_language_rule(response_language)}
Harness 已把 `roles/{role}.md` 全文注入 system context；不要再通过 read_file 读取角色卡，
也不要读取完整 `SKILL.md`、其他角色卡或整个 references 目录。
只完成任务分配给你的职责；完成后简短列出产物和遗留问题。{material_note}{raw_query_context}
"""
    else:
        return f"""\
You are the {role.upper()} role in Static HTML Presentation.
The Harness selected `{skill_name}`. {_response_language_rule(response_language)}
The Harness injected the complete `roles/{role}.md` into the system context. Do not
read that role card again, the full `SKILL.md`, other role cards, or the whole
reference directory.
Complete only the assigned role work, then report outputs and remaining issues
concisely.{material_note}{raw_query_context}
"""


def _injected_role_card(workspace: str, skill_name: str, role: str) -> str:
    """Load a v0.4 role contract outside the model-visible paged file tool."""
    if not _is_current_variant_skill_name(skill_name):
        return ""
    candidates = (
        Path(workspace) / "skills" / skill_name / "roles" / f"{role}.md",
        Path(config.SKILLS_DIR) / skill_name / "roles" / f"{role}.md",
    )
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        raise RuntimeError(
            "missing injected role card: "
            + ", ".join(str(candidate) for candidate in candidates)
        )
    content = path.read_text(encoding="utf-8")
    return (
        f"<injected_role_card path=\"roles/{role}.md\">\n"
        f"{content.rstrip()}\n"
        "</injected_role_card>\n"
    )


DELEGATE_TASK_SCHEMA = {
    "name": "delegate_task",
    "description": (
        "Delegate one presentation role or one Slide ownership unit per task. "
        "Use structured role/pages/group_id fields; the Harness derives stable labels "
        "and validates them against ownership_topology. For a single Slide set "
        "role=slide and pages=[NN]. For grouped ownership set role=slide, the complete "
        "pages list, and group_id. goal is optional case-specific context, not syntax. "
        "After planning, submit the optional Image task and all Slide units in one call: "
        "needs_bitmap:false units start immediately, while bitmap-dependent units are "
        "released as soon as Image succeeds. "
        "Keep goals to case-specific objectives and paths; the role "
        "card supplies the method. A completed Slide ownership unit is immutable "
        "within one production run: repair_required is handed to the single final "
        "Review, never reopened as another Slide agent. repair=true is reserved for "
        "an Image asset replacement or an explicit user revision run."
    ),
    "input_schema": {
        "type": "object",
        "required": ["tasks"],
        "additionalProperties": False,
        "properties": {
            "tasks": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["role"],
                    "additionalProperties": False,
                    "properties": {
                        "role": {
                            "type": "string",
                            "enum": ["material", "research", "image", "slide", "review"],
                        },
                        "pages": {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 1},
                            "minItems": 1,
                        },
                        "group_id": {"type": "string"},
                        "goal": {"type": "string"},
                        "repair": {"type": "boolean"},
                    },
                },
            }
        },
    },
}


class Agent:
    def __init__(
        self,
        sid: str,
        workspace: str,
        initial_user: str,
        cfg: dict,
        *,
        role: str = "orchestrator",
        label: str = "orchestrator",
    ) -> None:
        self.sid = sid
        self.ws = os.path.abspath(workspace)
        self.role = role
        self.label = label
        self.cfg = cfg
        try:
            self.requested_slide_count = max(
                0, int(self.cfg.get("_requested_slide_count") or 0)
            )
        except (TypeError, ValueError):
            self.requested_slide_count = 0
        self.profile = resolve_run_profile(self.cfg.get("run_mode"))
        self.run_mode = self.profile.name
        self.trace_mode_status = {
            "mode": self.run_mode,
            "complete": True,
            "image_count": 0,
        }
        self.initial_user = initial_user
        self.started = time.time()
        self.task_started_epoch = float(
            self.cfg.setdefault("_task_started_epoch", self.started)
        )
        self.task_started_utc = time.strftime(
            "%Y-%m-%d %H:%M:%S UTC",
            time.gmtime(self.task_started_epoch),
        )
        trace_root = Path(self.ws) / "_trace"
        trace_namespace = str(cfg.get("_trace_namespace") or "").strip("/")
        if trace_namespace:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", trace_namespace):
                raise ValueError(f"非法 trace namespace：{trace_namespace!r}")
            trace_root /= trace_namespace
        self.trace_label = str(cfg.get("_trace_label") or label)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.trace_label):
            raise ValueError(f"非法 trace label：{self.trace_label!r}")
        self.trace_dir = (
            trace_root / "orchestrator"
            if role == "orchestrator"
            else trace_root / "subagents" / self.trace_label
        )
        (self.trace_dir / "images").mkdir(parents=True, exist_ok=True)

        self.runtime_capabilities = (
            dict(cfg.get("_runtime_capabilities"))
            if isinstance(cfg.get("_runtime_capabilities"), dict)
            else {}
        )
        self.available_roles = set(
            self.runtime_capabilities.get("active_roles") or ROLES
        )
        runtime_tools = (
            self.runtime_capabilities.get("tools")
            if isinstance(self.runtime_capabilities.get("tools"), dict)
            else {}
        )
        self.search_enabled = bool(runtime_tools.get("web_search", True))
        self.vision_enabled = bool(runtime_tools.get("vision_analyze", True))
        runtime_inputs = (
            self.runtime_capabilities.get("inputs")
            if isinstance(self.runtime_capabilities.get("inputs"), dict)
            else {}
        )
        self.style_reference_paths = tuple(
            str(path) for path in runtime_inputs.get("style_reference_paths", []) if str(path)
        )
        self.enable_image_gen = bool(
            cfg.get("enable_image_gen", config.ENABLE_IMAGE_GEN)
            and runtime_tools.get("image_generate", True)
        )
        self.img_base = cfg.get("openai_base_url", config.IMAGE_BASE_URL).rstrip("/")
        self.img_key = os.environ.get("IMAGE_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
        self.image_model = cfg.get("image_model", config.IMAGE_MODEL)
        self.img_n = 0
        self.skill_name = str(cfg.get("skill_name", config.SKILL_NAME))
        self.skill_language = str(cfg.get("skill_language", "zh"))
        self.query_language_hint = str(
            cfg.get("query_language_hint") or cfg.get("deck_language") or "auto"
        )
        self.response_language = (
            normalize_language(cfg.get("response_language"))
            or normalize_language(self.query_language_hint)
            or normalize_language(infer_deck_language({"query": initial_user}))
        )
        if not self.response_language:
            self.response_language = "zh" if self.skill_language == "zh" else "en"
        self.cfg["response_language"] = self.response_language
        self.deck_language = "auto"
        if self.skill_language not in {"zh", "en", "auto"}:
            query_language = infer_deck_language({"query": initial_user})
            self.skill_language = "zh" if query_language == "zh" else "en"
            self.skill_name = (
                config.SKILL_NAME_ZH
                if self.skill_language == "zh"
                else config.SKILL_NAME_EN
            )
        if self.skill_language == "auto":
            self.skill_name = "auto"
            self.allowed_skill_names = {
                config.SKILL_NAME_ZH,
                config.SKILL_NAME_EN,
            }
            self.role_script = ""
        else:
            self.allowed_skill_names = {self.skill_name}
            self.role_script = _role_script_path(self.skill_name, self.role)
        self.bash_timeout = config.BASH_TIMEOUT_S
        self.max_vision_edge = config.MAX_VISION_EDGE
        self.evidence_scope = str(
            cfg.get("_evidence_scope") or cfg.get("evidence_scope") or "open_research"
        ).strip().lower()
        if self.evidence_scope not in {
            "off",
            "attachment_only",
            "verify_external",
            "open_research",
        }:
            self.evidence_scope = "open_research"

        if role == "orchestrator" and self.skill_language == "auto":
            self.system = _auto_orchestrator_system(
                tuple(str(path) for path in cfg.get("_staged_materials", [])),
                response_language=self.response_language,
                image_generation_enabled=self.enable_image_gen,
                runtime_profile=self.runtime_capabilities,
            )
        elif role == "orchestrator":
            self.system = _orchestrator_system(
                self.skill_name,
                self.skill_language,
                tuple(str(path) for path in cfg.get("_staged_materials", [])),
                response_language=self.response_language,
                image_generation_enabled=self.enable_image_gen,
                runtime_profile=self.runtime_capabilities,
            )
        else:
            self.system = _child_system(
                role,
                self.skill_name,
                self.skill_language,
                response_language=self.response_language,
                raw_user_query=str(cfg.get("_raw_user_query") or ""),
                revision_instruction=str(cfg.get("_revision_instruction") or ""),
            )
        prompt_language = "zh" if self.skill_language == "zh" else "en"
        self.system = (
            f"{self.system.rstrip()}\n\n"
            f"{_runtime_time_context(self.task_started_epoch, prompt_language)}\n"
            + (
                f"\n当前证据范围：`{self.evidence_scope}`。这是 Harness 权限合同；"
                "不得自行扩大。附件状态以随后注入的 material_stage 为准；"
                "omitted 时不得探测 material.md。\n"
                if prompt_language == "zh"
                else f"\nEvidence scope: `{self.evidence_scope}`. This is a Harness "
                "permission contract and must not be widened. Follow the injected "
                "material_stage; when omitted, do not probe material.md.\n"
            )
        )
        if self.role == "orchestrator" and self.requested_slide_count:
            count_rule = (
                f"Harness 页数硬合同：最终演示必须恰好 {self.requested_slide_count} 页，"
                "这个总数已经包含封面、结尾和任何过渡页；不得为了保留特殊页额外加页，"
                "必须在指定总数内合并内容。plan/deck.md 的 page_count、逐页计划、"
                "validate/scaffold/finalize 的 expected 必须全部使用这个值。"
                if prompt_language == "zh"
                else f"Harness page-count contract: the final deck must contain exactly "
                f"{self.requested_slide_count} slides. This total already includes cover, "
                "closing, and any dividers; never add a slide to preserve a special page. "
                "Use this exact value in deck.md, per-slide plans, and every expected flag."
            )
            self.system = f"{self.system.rstrip()}\n\n{count_rule}\n"
        role_card = _injected_role_card(self.ws, self.skill_name, self.role)
        if role_card:
            self.system = f"{self.system.rstrip()}\n\n{role_card}"
        self.role_card_injected = bool(role_card)
        if self.role == "research" and _is_current_variant_skill_name(self.skill_name):
            self.research_brief_char_limit = tools.research_brief_char_limit(self)
            _hard_ceiling = tools.research_brief_hard_ceiling(self)
            self.system = (
                f"{self.system.rstrip()}\n\n"
                "Harness 交接约束：`research/knowledge-brief.md` 推荐预算 "
                f"{self.research_brief_char_limit} 字符，绝对安全上限 "
                f"{_hard_ceiling} 字符。6–12 KB 为目标，不要故意写满。"
                "首次写入超出推荐预算但未达绝对上限时仍被接受并立即锁定，不允许重写。"
                "首次成功写入唯一 brief 后，本 Research 会话由 "
                "Harness 确定性收口，不要继续搜索、重复写或另写总结。\n"
            )
        else:
            self.research_brief_char_limit = 0
        workflow = (
            self.runtime_capabilities.get("workflow")
            if isinstance(self.runtime_capabilities.get("workflow"), dict)
            else {}
        )
        if (
            workflow.get("material_stage") == "direct_text"
            and self.role in {"orchestrator", "research"}
        ):
            direct_text = _direct_text_injection(
                self.ws,
                "zh" if self.skill_language == "zh" else "en",
            )
            if direct_text:
                self.system = f"{self.system.rstrip()}\n\n{direct_text}"
        capability_context = _runtime_capability_context(
            self.runtime_capabilities,
            prompt_language,
        )
        if capability_context:
            # Keep the deterministic, run-specific overlay last so a static
            # role card cannot accidentally re-enable an omitted route.
            self.system = f"{self.system.rstrip()}\n\n{capability_context}\n"
        self.revision_mode = bool(cfg.get("_revision_mode", False))
        self.tool_schemas = tools.agent_tools(
            role,
            enable_image_gen=self.enable_image_gen,
            enable_web_search=self.search_enabled,
            enable_vision=self.vision_enabled,
            render_script=self.role_script,
            skill_name=self.skill_name,
            revision_mode=self.revision_mode,
            evidence_scope=self.evidence_scope,
            enable_attachment_vision=bool(self.style_reference_paths),
        )
        if role == "orchestrator":
            self.tool_schemas.append(DELEGATE_TASK_SCHEMA)

        self.nova_raw = None
        self.nova_precheck: dict | None = None
        if bool(cfg.get("nova_raw_v2", config.NOVA_RAW_V2)):
            self.nova_raw = nova_raw.NovaRawRecorder(
                root=Path(cfg.get("nova_raw_root", config.NOVA_RAW_ROOT)),
                run_id=str(cfg.get("nova_run_id") or cfg.get("batch") or "run"),
                sample_id=self.sid,
                role=self.role,
                label=self.label,
                tools=self.tool_schemas,
                initial_user=self.initial_user,
            )

        self.model = cfg.get("model", config.ANTHROPIC_MODEL)
        self.max_turns = int(
            cfg.get("max_turns", config.MAX_TURNS)
            if role == "orchestrator"
            else cfg.get("child_max_turns", config.CHILD_MAX_TURNS)
        )
        self.max_tokens = int(cfg.get("max_tokens", config.PER_TURN_MAX_TOKENS))
        self.first_response_timeout_s = int(
            cfg.get("first_response_timeout_s", config.FIRST_RESPONSE_TIMEOUT_S)
        )
        self.active_response_timeout_s = int(
            cfg.get("active_response_timeout_s", config.ACTIVE_RESPONSE_TIMEOUT_S)
        )
        self.deck_timeout_s = int(cfg.get("deck_timeout_s", config.DECK_TIMEOUT_S))
        parent_deadline = cfg.get("_parent_deadline_monotonic")
        self.deadline_monotonic = time.monotonic() + self.deck_timeout_s
        if parent_deadline is not None:
            self.deadline_monotonic = min(
                self.deadline_monotonic, float(parent_deadline)
            )
        self.runtime_limit_s = max(
            1, int(self.deadline_monotonic - time.monotonic())
        )
        self.thinking = bool(cfg.get("thinking", config.THINKING))
        self.effort = cfg.get("effort", config.THINK_EFFORT)
        self.child_concurrency_initial = max(
            1,
            int(cfg.get("child_concurrency", config.CHILD_CONCURRENCY)),
        )
        self.child_concurrency_max = max(
            self.child_concurrency_initial,
            int(
                cfg.get(
                    "child_pool_max_workers",
                    config.CHILD_POOL_MAX_WORKERS,
                )
            ),
        )
        self.child_concurrency_file = str(
            cfg.get(
                "child_concurrency_file",
                config.CHILD_CONCURRENCY_FILE,
            )
            or ""
        ).strip()
        self.child_concurrency_last_valid = self.child_concurrency_initial
        self.remote_tool_concurrency = max(
            1,
            int(
                cfg.get(
                    "remote_tool_concurrency",
                    config.REMOTE_TOOL_CONCURRENCY,
                )
            ),
        )

        self.final_text = ""
        self.exit_reason: str | None = None
        self.turn = 0
        self.peak_input_tokens = 0
        self.last_input_tokens = 0
        self.forced_summary = False
        self.n_renders = 0
        self.n_views = 0
        self.tool_policy_violations: list[dict] = []
        self.workspace_policy_violations: list[str] = []
        self.image_by_tool: dict[str, str] = {}
        self.children: list[Agent] = []
        self.child_outcomes: dict[str, dict] = {}
        self.delegated_roles: list[str] = []
        self.material_required = False
        self.material_completed = False
        self.material_blocked = False
        self.research_required = (
            "research" in self.available_roles
            and not _uses_legacy_grouped_contract(self.skill_name)
        )
        self.research_completed = False
        self.research_blocked = False
        self.image_required = False
        self.image_completed = False
        self.image_ready_pages: tuple[int, ...] = ()
        self.image_failed_pages: tuple[int, ...] = ()
        self.image_handoff_summary = ""
        self.image_finalized_catalog_digest = ""
        self.review_completed = False
        self.quality_status = "ready"
        self.review_changed = False
        self.final_render_after_review = False
        self.final_view_after_review = False
        self.finalize_attempted = False
        self.finalize_succeeded = False
        self.finalize_failure = ""
        self.expected_output_path = ""
        self.expected_output_initial_hash = ""
        self.assigned_slide_pages: tuple[int, ...] = ()
        self.slide_group_id = ""
        self.expected_output_paths: dict[int, str] = {}
        self.expected_output_initial_hashes: dict[int, str] = {}
        self.rendered_output_hashes: dict[int, str] = {}
        self.viewed_output_hashes: dict[int, str] = {}
        self.slide_pixel_inspections = 0  # compatibility metric; budgets are persistent per unit
        self.slide_inspection_state = ""
        self.group_viewed_contact_hash = ""
        self.group_viewed_page_hashes: dict[int, str] = {}
        # Exact page-pixel provenance of the most recent render-group output.
        # This prevents an older montage (and its Vision cache entry) from
        # closing a group after any member page has changed.
        self.group_rendered_contact_hash = ""
        self.group_rendered_page_hashes: dict[int, str] = {}
        self.repair_required_reason = ""
        self.vision_critic_results: dict[str, dict] = {}
        self.required_review_pages: tuple[int, ...] = ()
        self.review_viewed_page_hashes: dict[int, str] = {}
        self.review_contact_sheet_inspected = False
        # A Review round is one coordinated mutation batch followed by a
        # successful finalize and inspection of the resulting whole-deck
        # contact sheet.  Keep this lifecycle budget inside the single Review
        # Agent; it must not be confused with review_r2/review_r3 retries.
        self.review_revision_rounds = 0
        self.review_mutation_since_contact_scan = False
        self.review_patches_since_finalize = 0
        self.review_closure_only = False
        self.revision_route = ""
        self.ownership_topology = ""

    def _record_event(self, event: dict, display: str) -> None:
        """Append one trace event and mirror a concise line to stdout."""
        event_path = Path(self.ws) / "_trace" / "events.jsonl"
        try:
            payload = (
                json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
            ).encode("utf-8")
            fd = os.open(event_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
        except OSError:
            # Live trace persistence must never interrupt deck generation.
            pass
        print(
            f"[{time.strftime('%H:%M:%S')} +"
            f"{event['agent_elapsed_seconds']:6.1f}s] "
            f"[{self.sid}/{self.label}] {display}",
            flush=True,
        )

    def log(self, message: str) -> None:
        now = time.time()
        elapsed = now - self.started
        event = {
            "epoch": now,
            "clock": time.strftime("%H:%M:%S", time.gmtime(now)),
            "sid": self.sid,
            "label": self.label,
            "trace_label": self.trace_label,
            "agent_elapsed_seconds": max(0, int(elapsed)),
            "message": str(message),
        }
        self._record_event(event, str(message))

    def log_structured(self, name: str, **fields: object) -> None:
        """Write a machine-readable operational event without a sidecar file."""
        now = time.time()
        event = {
            "epoch": now,
            "clock": time.strftime("%H:%M:%S", time.gmtime(now)),
            "sid": self.sid,
            "label": self.label,
            "agent_elapsed_seconds": max(0, int(now - self.started)),
            "event": name,
            **fields,
        }
        self._record_event(
            event,
            f"{name} {json.dumps(fields, ensure_ascii=False, separators=(',', ':'))}",
        )

    def safe(self, path: str) -> str:
        root = os.path.realpath(self.ws)
        target = os.path.realpath(os.path.join(root, path))
        if target != root and not target.startswith(root + os.sep):
            raise ValueError(f"路径越出工作区: {path}")
        return target

    def _lock_skill(self, skill_name: str) -> None:
        if self.skill_language != "auto":
            return
        language_by_name = {
            config.SKILL_NAME_ZH: "zh",
            config.SKILL_NAME_EN: "en",
        }
        language = language_by_name.get(skill_name)
        if language is None:
            raise ValueError(f"未知 Skill 版本：{skill_name}")
        self.skill_name = skill_name
        self.skill_language = language
        self.allowed_skill_names = {skill_name}
        self.role_script = _role_script_path(skill_name, self.role)
        self.tool_schemas = tools.agent_tools(
            self.role,
            enable_image_gen=self.enable_image_gen,
            enable_web_search=self.search_enabled,
            enable_vision=self.vision_enabled,
            render_script=self.role_script,
            skill_name=self.skill_name,
            evidence_scope=self.evidence_scope,
            enable_attachment_vision=bool(self.style_reference_paths),
        )
        if self.role == "orchestrator":
            self.tool_schemas.append(DELEGATE_TASK_SCHEMA)
        self.cfg["skill_name"] = skill_name
        self.cfg["skill_language"] = language
        self.log_structured(
            "skill_selection",
            mode="model_first_read",
            selected=skill_name,
            instruction_language=language,
            query_language_hint=self.query_language_hint,
        )

    def read_path(self, path: str) -> str:
        if path == "skills" or path.startswith("skills/"):
            root = os.path.realpath(config.SKILLS_DIR)
            rel = path[len("skills"):].lstrip("/")
            first = rel.split("/", 1)[0] if rel else ""
            if first and first not in self.allowed_skill_names:
                raise ValueError(
                    f"当前 Deck 未暴露 Skill {first!r}；"
                    f"可用版本为 {sorted(self.allowed_skill_names)}"
                )
            if first:
                self._lock_skill(first)
            target = os.path.realpath(os.path.join(root, rel))
            if target != root and not target.startswith(root + os.sep):
                raise ValueError("路径越出 skills")
            return target
        return self.safe(path)

    def snapshot(self) -> None:
        (self.trace_dir / "system_prompt.md").write_text(self.system, encoding="utf-8")
        _write_json(self.trace_dir / "tools.json", self.tool_schemas)
        _write_json(self.trace_dir / "config.json", {
            "sample_id": self.sid,
            "label": self.label,
            "role": self.role,
            "task": self.initial_user,
            "model": self.model,
            "model_base_url": self.cfg.get("model_base_url", config.ANTHROPIC_BASE_URL),
            "thinking": self.thinking,
            "thinking_effort": self.effort,
            "run_mode": self.run_mode,
            "run_profile": {
                "release_consumed_images": self.profile.release_consumed_images,
                "compact_active_history": self.profile.compact_active_history,
                "require_complete_trace": self.profile.require_complete_trace,
            },
            "skill_name": self.skill_name,
            "skill_language": self.skill_language,
            "query_language_hint": self.query_language_hint,
            "response_language": self.response_language,
            "deck_language": self.deck_language,
            "batch_workers": self.cfg.get("batch_workers"),
            "child_concurrency_initial": self.child_concurrency_initial,
            "child_concurrency_max": self.child_concurrency_max,
            "child_concurrency_file": self.child_concurrency_file or None,
            "remote_tool_concurrency": self.remote_tool_concurrency,
            "vision_backend_requested": config.VISION_BACKEND_REQUESTED,
            "vision_backend_effective": config.VISION_BACKEND,
            "vision_critic_model": (
                self.model
                if config.VISION_BACKEND == "same_model_aux"
                else config.VISION_CRITIC_MODEL
                if config.VISION_BACKEND == "external_model"
                else config.VISION_BACKEND
            ),
            "max_attempts": self.cfg.get("max_attempts"),
            "max_turns": self.max_turns,
            "max_tokens": self.max_tokens,
            "model_timeout_s": self.cfg.get(
                "model_timeout_s", config.MODEL_TIMEOUT_S
            ),
            "first_response_timeout_s": self.first_response_timeout_s,
            "active_response_timeout_s": self.active_response_timeout_s,
            "deck_timeout_s": self.deck_timeout_s,
            "runtime_limit_s": self.runtime_limit_s,
            "bash_timeout_s": self.bash_timeout,
            "max_heals": self.cfg.get("max_heals", config.MAX_HEALS),
            "max_vision_edge": self.max_vision_edge,
            "deck_size": [
                self.cfg.get("deck_width", config.DECK_W),
                self.cfg.get("deck_height", config.DECK_H),
            ],
            "image_generation_enabled": self.enable_image_gen,
            "image_model": self.image_model,
            "image_base_url": self.img_base,
            "authoritative_task_started_at": self.task_started_utc,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })

    def finish_snapshot(self) -> None:
        path = self.trace_dir / "config.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            payload = {}
        payload.update({
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_s": round(time.time() - self.started, 3),
            "exit_reason": self.exit_reason,
            "forced_summary": self.forced_summary,
            "peak_input_tokens": self.peak_input_tokens,
            "child_outcomes": self.child_outcomes,
            "renders": self.n_renders,
            "views": self.n_views,
            "skill_name": self.skill_name,
            "skill_language": self.skill_language,
            "response_language": self.response_language,
            "deck_language": self.deck_language,
            "run_mode": self.run_mode,
            "multimodal_trace": self.trace_mode_status,
        })
        _write_json(path, payload)

    @staticmethod
    def blocks(content) -> list[dict]:
        result: list[dict] = []
        for block in content:
            if block.type == "text":
                result.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                result.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
            elif block.type == "thinking":
                item = {"type": "thinking", "thinking": block.thinking}
                if getattr(block, "signature", None):
                    item["signature"] = block.signature
                result.append(item)
            elif block.type == "redacted_thinking":
                result.append({"type": "redacted_thinking", "data": block.data})
        return result

    def compact_message(self, message: dict) -> dict:
        """Serialize trace images as permanent snapshot references, never bytes."""
        if not isinstance(message.get("content"), list):
            return message
        content = []
        for item in message["content"]:
            if item.get("type") == "tool_result" and isinstance(item.get("content"), list):
                shot = self.image_by_tool.get(item.get("tool_use_id"))
                compact = []
                has_image = False
                for part in item["content"]:
                    if isinstance(part, dict) and part.get("type") == "image":
                        has_image = True
                        compact.append({"type": "image", "shot": shot})
                    else:
                        compact.append(part)
                # Old turns have already released their base64 image block from
                # the live model context. The persisted copy must still retain
                # the immutable on-disk snapshot identity for the dashboard and
                # SFT exporter.
                if shot and not has_image:
                    compact.append({"type": "image", "shot": shot})
                content.append({**item, "content": compact})
            else:
                content.append(item)
        return {**message, "content": content}

    def token_limit(self) -> int:
        # Let the provider enforce the model's real context window. Do not shrink
        # output or force closure from a guessed local context budget.
        return self.max_tokens


def _messages_for_model(messages: list[dict]) -> list[dict]:
    """Return a replay-safe copy without mutating the durable trajectory.

    OpenAI-compatible providers expose useful unsigned reasoning as a thinking
    block, while Anthropic rejects replaying such a block without a signature.
    Preserve it in messages.json/SFT data and omit it only from the next API
    request. Signed thinking remains unchanged.
    """
    changed = False
    replay: list[dict] = []
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            replay.append(message)
            continue
        filtered = [
            block
            for block in content
            if not (
                isinstance(block, dict)
                and block.get("type") == "thinking"
                and not block.get("signature")
            )
        ]
        if len(filtered) != len(content):
            replay.append({**message, "content": filtered})
            changed = True
        else:
            replay.append(message)
    return replay if changed else messages


REVIEW_CLOSURE_ONLY_TURN_FRACTION = 0.70
REVIEW_CLOSURE_TAIL_BUDGET = 6


def _review_should_enter_closure_only(agent: Agent, turn: int) -> bool:
    """Return whether Review should be restricted to closure-only tools.

    Trigger only when Review has unfinalized patches and has used at least 70%
    of its turn budget. Individual patch calls are implementation operations,
    not effective pixel revision rounds; the authoritative three-round limit is
    ``review_revision_rounds``.

    This is a process safety rail that prevents unbounded mutation loops.
    """
    if str(getattr(agent, "role", "") or "").lower() != "review":
        return False
    if bool(getattr(agent, "review_closure_only", False)):
        return True
    patches = int(getattr(agent, "review_patches_since_finalize", 0) or 0)
    if patches == 0:
        return False
    budget = agent.max_turns
    if turn >= int(budget * REVIEW_CLOSURE_ONLY_TURN_FRACTION):
        return True
    return False


def _review_budget_pending_page_views(agent: Agent) -> list[int]:
    """Return required pages whose current pixels still need final inspection."""
    if str(getattr(agent, "role", "") or "").lower() != "review":
        return []
    if int(getattr(agent, "review_revision_rounds", 0) or 0) < int(
        config.REVIEW_MAX_ATTEMPTS
    ):
        return []
    root = Path(agent.ws)
    viewed = dict(getattr(agent, "review_viewed_page_hashes", {}) or {})
    return [
        int(page)
        for page in tuple(getattr(agent, "required_review_pages", ()) or ())
        if viewed.get(int(page))
        != _file_content_digest(root / "renders" / f"slide_{int(page):02d}.png")
    ]


def _review_in_session_closure_nudge_limit(agent: Agent) -> int:
    """Return remaining same-session Review closure opportunities.

    The value follows the Review repair lifecycle, not generic model closure
    retries.  A Review that has completed one of three coordinated repair
    rounds therefore still gets two opportunities to continue in the same
    child instead of being recreated as ``review_r2``.
    """
    if str(getattr(agent, "role", "") or "").lower() != "review":
        return 0
    rounds = int(getattr(agent, "review_revision_rounds", 0) or 0)
    if rounds >= int(config.REVIEW_MAX_ATTEMPTS):
        return 0
    return max(1, int(config.REVIEW_MAX_ATTEMPTS) - rounds)


def _call(
    agent: Agent,
    messages: list[dict],
    with_tools: bool = True,
    *,
    first_response: bool = False,
):
    visible_tools = agent.tool_schemas if with_tools else None
    model_messages = _messages_for_model(messages)
    if str(getattr(agent, "role", "") or "").lower() == "slide":
        # A Grouped owner closes one page at a time.  Keep this state on the
        # Agent so the tool layer can reject out-of-order writes/renders even
        # when a model emits several calls in one response.
        agent.active_slide_page = _slide_group_active_page(agent)
        if with_tools and int(agent.active_slide_page or 0) > 0:
            model_messages = [
                *model_messages,
                {
                    "role": "user",
                    "content": (
                        "[Harness grouped page cursor] Finish the current full-resolution "
                        f"page loop for P{int(agent.active_slide_page):02d} before authoring "
                        "or rendering another member. A page closes only on a current-hash "
                        "ready verdict, or after its three checked states are exhausted and "
                        "the open issue is preserved for Review. The group contact sheet is "
                        "a later cross-page consistency check and cannot overrule this page."
                    ),
                },
            ]
    if (
        with_tools
        and str(getattr(agent, "role", "") or "").lower() == "image"
        and bool(getattr(agent, "image_search_closed", False))
    ):
        visible_tools = [
            schema for schema in (visible_tools or [])
            if schema.get("name") != "web_search"
        ]
        model_messages = [
            *model_messages,
            {
                "role": "user",
                "content": (
                    "[Harness bounded image search] An exact query was repeated, so "
                    "web_search is closed for this Image session. Reuse the candidates "
                    "already returned, use image_generate for planned generated media, "
                    "write/finalize the catalog, and mark any genuinely unavailable item "
                    "failed. Do not wait for another search tool."
                ),
            },
        ]
        agent.log("[image search closed] duplicate query; web_search removed")
    if (
        with_tools
        and str(getattr(agent, "role", "") or "").lower() == "slide"
        and bool(getattr(agent, "_slide_inputs_read_closed", False))
    ):
        visible_tools = [
            schema for schema in (visible_tools or [])
            if schema.get("name") != "read_file"
        ]
        model_messages = [
            *model_messages,
            {
                "role": "user",
                "content": (
                    "[Harness immutable inputs complete] The assigned plan, scaffold, "
                    "deck contract, and final asset catalog have already been read to "
                    "EOF. read_file is now closed for this Slide session. Continue with "
                    "write/patch, render, and Vision. Use only ready catalog assets; if "
                    "fewer images survived than the visual wish-list, redesign around "
                    "the available image instead of rereading or inventing a path."
                ),
            },
        ]
        agent.log("[slide immutable inputs complete] read_file removed")
    pending = tools.pending_read_requirement(agent)
    delivery_close_status = _orchestrator_delivery_close_status(agent)
    if with_tools and delivery_close_status:
        # Review owns the last admissible repair cycle. Once current pixels are
        # finalized and inspected, continued probing or re-delegation can only
        # create a permission/stall loop. Preserve a usable deck as
        # needs_improvement when bounded soft findings remain.
        agent._pending_read_continuations = {}
        agent._patch_recovery_path = ""
        agent.pending_slide_delegation_pages = ()
        pending = None
        visible_tools = []
        model_messages = [
            *model_messages,
            {
                "role": "user",
                "content": (
                    "[Harness final delivery] The unique Review has completed, the "
                    "current deck was finalized, and its final pixels were inspected. "
                    f"Deliver now with quality_status: {delivery_close_status}. Do not "
                    "inspect source, run audit/finalize again, or re-delegate any role. "
                    "If the status is needs_improvement, summarize the bounded residual "
                    "quality note without turning the usable deck into a failed job."
                ),
            },
        ]
        agent.log(
            "[final delivery stop line] tools hidden after current reviewed delivery: "
            + delivery_close_status
        )
    REVIEW_CLOSURE_ONLY_TOOLS = {"terminal", "vision_analyze"}
    if (
        with_tools
        and not delivery_close_status
        and _review_should_enter_closure_only(agent, int(getattr(agent, "turn", 0) or 0))
    ):
        agent.review_closure_only = True
        visible_tools = [
            schema for schema in (visible_tools or [])
            if schema.get("name") in REVIEW_CLOSURE_ONLY_TOOLS
        ]
        model_messages = [
            *model_messages,
            {
                "role": "user",
                "content": (
                    "[Harness closure-only mode] You have unfinalized patches and are "
                    "approaching the turn budget. Only finalize and Vision inspection "
                    "are available now.\n"
                    "Required sequence: (1) finalize current files, (2) inspect the new "
                    "contact sheet and every changed/required page at full resolution, "
                    "(3) return structured status: ready or needs_improvement.\n"
                    "Do NOT attempt further patches — they will be rejected."
                ),
            },
        ]
        agent.log(
            "[review closure-only] patch/read_file removed; "
            f"patches_since_finalize={agent.review_patches_since_finalize} "
            f"turn={agent.turn}/{agent.max_turns}"
        )
    if with_tools and pending is not None:
        required_path, required_offset = pending
        read_schema = next(
            (
                copy.deepcopy(schema)
                for schema in agent.tool_schemas
                if schema.get("name") == "read_file"
            ),
            None,
        )
        if read_schema is not None:
            input_schema = read_schema.setdefault("input_schema", {})
            properties = input_schema.setdefault("properties", {})
            properties["path"] = {
                "type": "string",
                "enum": [required_path],
                "description": "The only file allowed while pending_read is active.",
            }
            properties["offset"] = {
                "type": "integer",
                "enum": [required_offset],
                "description": "The exact continuation offset required by Harness.",
            }
            required_fields = list(input_schema.get("required") or [])
            for field in ("path", "offset"):
                if field not in required_fields:
                    required_fields.append(field)
            input_schema["required"] = required_fields
            visible_tools = [read_schema]
    pending_slide_pages = tuple(
        int(page)
        for page in (
            getattr(agent, "pending_slide_delegation_pages", ()) or ()
        )
        if isinstance(page, int) and not isinstance(page, bool) and page > 0
    )
    terminal_failures = tuple(
        int(page)
        for page in (getattr(agent, "terminal_slide_failures", ()) or ())
        if isinstance(page, int) and not isinstance(page, bool) and page > 0
    )
    if with_tools and terminal_failures:
        visible_tools = []
        agent.pending_slide_delegation_pages = ()
        pages_text = ", ".join(f"P{page:02d}" for page in terminal_failures)
        inject = (
            f"[terminal_slide_failure] 以下页面已彻底失败且不可恢复：{pages_text}。"
            "不得再次委派这些页面。请以 missing_slide_artifact 结束本任务。"
        )
        model_messages = [
            *model_messages,
            {"role": "user", "content": inject},
        ]
        agent.log(inject)
    elif with_tools and pending is None and pending_slide_pages:
        terminal_pages = tuple(
            page for page in pending_slide_pages
            if _page_has_terminal_slide_failure(agent, page)
        )
        if terminal_pages and len(terminal_pages) == len(pending_slide_pages):
            visible_tools = []
            agent.pending_slide_delegation_pages = ()
            agent.terminal_slide_failures = terminal_pages
            pages_text = ", ".join(f"P{page:02d}" for page in terminal_pages)
            inject = (
                f"[terminal_slide_failure] 以下页面已彻底失败且不可恢复：{pages_text}。"
                "不得再次委派这些页面。请以 missing_slide_artifact 结束本任务。"
            )
            model_messages = [
                *model_messages,
                {"role": "user", "content": inject},
            ]
            agent.log(inject)
        else:
            delegate_schema = next(
                (
                    copy.deepcopy(schema)
                    for schema in agent.tool_schemas
                    if schema.get("name") == "delegate_task"
                ),
                None,
            )
            if delegate_schema is not None:
                visible_tools = [delegate_schema]
                agent.log(
                    "[pending slide responsibility] only delegate_task for "
                    + ",".join(f"P{page:02d}" for page in pending_slide_pages)
                )
    elif with_tools and str(getattr(agent, "_patch_recovery_path", "") or ""):
        recovery_path = str(agent._patch_recovery_path)
        read_schema = next(
            (
                copy.deepcopy(schema)
                for schema in agent.tool_schemas
                if schema.get("name") == "read_file"
            ),
            None,
        )
        if read_schema is not None:
            properties = read_schema.setdefault("input_schema", {}).setdefault(
                "properties", {}
            )
            properties["path"] = {
                "type": "string",
                "enum": [recovery_path],
                "description": "Read the current file after an exact patch miss.",
            }
            visible_tools = [read_schema]
            model_messages = [
                *model_messages,
                {
                    "role": "user",
                    "content": (
                        "[Harness stale patch recovery] The requested old_string is not "
                        f"present. Read the current `{recovery_path}` now; no mutation "
                        "tool is available until this read completes. Build any retry "
                        "from the returned current text, and attempt it at most once."
                    ),
                },
            ]
            agent.log(
                "[stale patch tool surface] only read_file path=" + recovery_path
            )
    elif (
        with_tools
        and agent.role == "review"
        and int(getattr(agent, "review_revision_rounds", 0) or 0)
        >= int(config.REVIEW_MAX_ATTEMPTS)
    ):
        stale_pages = _review_budget_pending_page_views(agent)
        if stale_pages:
            vision_schema = next(
                (
                    copy.deepcopy(schema)
                    for schema in agent.tool_schemas
                    if schema.get("name") == "vision_analyze"
                ),
                None,
            )
            if vision_schema is not None:
                image_paths = [
                    f"renders/slide_{int(page):02d}.png" for page in stale_pages
                ]
                properties = vision_schema.setdefault(
                    "input_schema", {}
                ).setdefault("properties", {})
                properties["image"] = {
                    "type": "string",
                    "enum": image_paths,
                    "description": (
                        "Only the current mandatory page pixels may be inspected "
                        "after the third Review repair round."
                    ),
                }
                visible_tools = [vision_schema]
                model_messages = [
                    *model_messages,
                    {
                        "role": "user",
                        "content": (
                            "[Harness Review stop line] Three coordinated repair rounds "
                            "are complete. Source mutation is now frozen. Inspect only "
                            "the still-stale mandatory current PNGs: "
                            + ", ".join(image_paths)
                        ),
                    },
                ]
                agent.log(
                    "[review stop line] only final mandatory page inspection: "
                    + ",".join(f"P{page:02d}" for page in stale_pages)
                )
        else:
            visible_tools = []
            unresolved = _unresolved_vision_critic_issues(agent)
            evidence = "; ".join(
                str(record.get("summary") or record.get("verdict") or "")[:240]
                for _, record in unresolved[:4]
            ) or "bounded Review completed; retain any remaining non-blocking quality note"
            model_messages = [
                *model_messages,
                {
                    "role": "user",
                    "content": (
                        "[Harness Review stop line] The three-round soft repair budget is "
                        "exhausted and current mandatory pixels have been inspected. Do "
                        "not edit or render again. Return a concise structured handoff: "
                        "`status: needs_orchestrator`, the affected pages and issue_type, "
                        f"`evidence: {evidence}`, `blocking: no`, and "
                        "`final_pixels_inspected: yes`. This is a deliverable "
                        "`needs_improvement` outcome, not a whole-deck failure."
                    ),
                },
            ]
            agent.log(
                "[review stop line] tools hidden after three coordinated repair rounds"
            )
    elif with_tools and _slide_pending_render_views(agent):
        # Every newly rendered pixel state must be inspected before the model
        # may mutate source again.  This also preserves the third and final
        # render/view cycle: after render #3, expose only Vision; freeze the
        # page on the following turn once that exact hash has been checked.
        pending_pages = _slide_pending_render_views(agent)
        vision_schema = next(
            (
                copy.deepcopy(schema)
                for schema in agent.tool_schemas
                if schema.get("name") == "vision_analyze"
            ),
            None,
        )
        if vision_schema is not None:
            image_paths = [
                f"renders/slide_{int(page):02d}.png" for page in pending_pages
            ]
            properties = vision_schema.setdefault("input_schema", {}).setdefault(
                "properties", {}
            )
            properties["image"] = {
                "type": "string",
                "enum": image_paths,
                "description": "Current uninspected render; no other tool is allowed first.",
            }
            visible_tools = [vision_schema]
            model_messages = [
                *model_messages,
                {
                    "role": "user",
                    "content": (
                        "[Harness pixel check] Source was rendered and the current PNG "
                        "hash has not been inspected. Before any read, patch, write, or "
                        "new render, call vision_analyze for: " + ", ".join(image_paths)
                    ),
                },
            ]
            agent.log(
                "[render/view ordering] only vision_analyze for "
                + ",".join(f"P{int(page):02d}" for page in pending_pages)
            )
    elif with_tools and (pending_group_view := _slide_pending_group_view(agent)):
        # Once every current page PNG has been inspected, a multi-page owner
        # gets exactly one consistency decision for the current group sheet.
        # Do not let it patch first or repeatedly re-open already checked page
        # pixels with differently worded questions.
        if pending_group_view == "__render_group_required__":
            terminal_schema = next(
                (
                    copy.deepcopy(schema)
                    for schema in agent.tool_schemas
                    if schema.get("name") == "terminal"
                ),
                None,
            )
            if terminal_schema is not None:
                visible_tools = [terminal_schema]
                model_messages = [
                    *model_messages,
                    {
                        "role": "user",
                        "content": (
                            "[Harness group provenance] A member page changed after the "
                            "last group montage. Run the assigned slide.py render-group "
                            "command now; an older contact sheet cannot be reused."
                        ),
                    },
                ]
                agent.log("[group provenance] only render-group is allowed")
        else:
            vision_schema = next(
                (
                    copy.deepcopy(schema)
                    for schema in agent.tool_schemas
                    if schema.get("name") == "vision_analyze"
                ),
                None,
            )
            if vision_schema is not None:
                properties = vision_schema.setdefault("input_schema", {}).setdefault(
                    "properties", {}
                )
                properties["image"] = {
                    "type": "string",
                    "enum": [pending_group_view],
                    "description": "Current uninspected Slide Group consistency sheet.",
                }
                visible_tools = [vision_schema]
                model_messages = [
                    *model_messages,
                    {
                        "role": "user",
                        "content": (
                            "[Harness group pixel check] Every current page PNG has been "
                            "inspected. Before any source mutation or completion, inspect "
                            f"the current group sheet once: {pending_group_view}"
                        ),
                    },
                ]
                agent.log(
                    "[group render/view ordering] only vision_analyze for "
                    + pending_group_view
                )
    elif with_tools and _slide_authoring_stop_line(agent):
        # Once a page has consumed its three checked pixel states, every later
        # source edit would be blind because no fourth authoring render may
        # verify it.  Close the child now: unresolved pixels become a non-fatal
        # Review handoff, while clean current pixels remain ready.
        has_open_issue = bool(
            _unresolved_vision_critic_issues(agent)
            or str(getattr(agent, "repair_required_reason", "") or "").strip()
        )
        visible_tools = []
        model_messages = [
            *model_messages,
            {
                "role": "user",
                "content": (
                    "[Harness render stop line] The three checked authoring pixel "
                    "states are exhausted; no further source mutation can be verified. "
                    + (
                        "A current-pixel issue remains. Do not claim ready. Return the "
                        "structured status `repair_required` with pages, issue_type, "
                        "pixel evidence, and a proposed fix for Review."
                        if has_open_issue
                        else "The current checked pixels have no open critic issue. "
                        "Do not edit again; return structured `ready` now."
                    )
                ),
            },
        ]
        agent.log(
            "[render stop line] tools hidden; require repair_required handoff"
        )
    elif with_tools and getattr(agent, "role", "") == "slide":
        # The current page/group pixels already have a critic decision.  A
        # second question against the same bytes does not add evidence and can
        # create long prompt-rephrasing loops.  Keep edit/render tools so the
        # owner can apply one merged repair, but withhold Vision until a new
        # render hash exists.
        visible_tools = [
            schema
            for schema in visible_tools
            if schema.get("name") != "vision_analyze"
        ]
    return model_call.call_with_tools(
        model=agent.model,
        system=agent.system,
        messages=model_messages,
        tools=visible_tools,
        max_tokens=agent.token_limit(),
        thinking=agent.thinking,
        effort=agent.effort,
        log=agent.log,
        first_attempt_timeout_s=(
            agent.first_response_timeout_s
            if first_response
            else agent.active_response_timeout_s
        ),
        deadline_monotonic=agent.deadline_monotonic,
        nova_recorder=agent.nova_raw,
        request_kind="hermes_main",
    )


def _auto_continue_read_allowed(agent: Agent, path: str) -> bool:
    """Continue canonical or actively edited long files without model ping-pong."""
    if not tools._is_current_variant_skill(agent):
        return False
    normalized = str(path or "").replace("\\", "/").lstrip("./")
    role = str(getattr(agent, "role", "") or "")
    if role == "material":
        return bool(
            normalized == "research/material.md"
            or (
                normalized.startswith("inputs/")
                and normalized.endswith((".md", ".txt", ".csv"))
            )
        )
    if role == "research":
        return normalized in {
            "research/material.md",
            "research/knowledge-brief.md",
        }
    if role in {"orchestrator", "image"}:
        allowed = {"research/knowledge-brief.md", "plan/deck.md"}
        if role == "orchestrator":
            # In attachment-only runs Research is intentionally omitted and
            # Material hands this canonical file directly to Orchestrator.
            # The read policy still rejects this path whenever Research is
            # enabled, so auto-continuation cannot bypass evidence routing.
            allowed.add("research/material.md")
            if re.fullmatch(r"plan/slide_\d{2}\.md", normalized):
                return True
        if role == "image":
            allowed.add("assets/catalog.md")
        return normalized in allowed
    if role in {"slide", "review"}:
        # Once a role has intentionally opened a page source, a hard pending
        # read must not leave unrelated tools visible and then reject every
        # attempted patch/render.  Stream the rest deterministically in the
        # same logical read; this is still bounded to one authored page file,
        # not the whole workspace or reference library.
        return bool(
            normalized == "plan/deck.md"
            or re.fullmatch(r"(?:plan/slide_\d{2}\.md|slides/slide_\d{2}\.html)", normalized)
        )
    return False


def _spec_slide_pages(spec: dict, fallback: int) -> tuple[int, ...]:
    raw_pages = spec.get("pages")
    if isinstance(raw_pages, list):
        pages = tuple(
            int(page)
            for page in raw_pages
            if isinstance(page, int) and not isinstance(page, bool) and page > 0
        )
        if pages:
            return pages
    naming_text = f"{spec.get('label', '')} {spec.get('task', '')}"
    page_match = re.search(r"slide[_\s-]*0*(\d+)", naming_text, flags=re.IGNORECASE)
    if not page_match:
        page_match = re.search(r"第\s*0*(\d+)\s*页", naming_text)
    return (int(page_match.group(1)) if page_match else fallback,)


def _planned_production_group(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = re.search(
        r"(?mi)^\s*-\s*production_group\s*:\s*([a-z0-9][a-z0-9_-]*)\s*$",
        text,
    )
    return match.group(1).lower().replace("_", "-") if match else ""


def _planned_page_type(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = re.search(
        r"(?mi)^\s*-\s*page_type\s*:\s*([a-z0-9][a-z0-9_-]*)\s*$",
        text,
    )
    return match.group(1).lower().replace("_", "-") if match else ""


def _single_special_memory_group(
    workspace: Path,
    group_id: str,
    pages: list[int],
) -> bool:
    """Allow one compact cross-deck memory unit without grouping content pages."""
    if group_id not in {"bookends", "dividers"} or not pages:
        return False
    planned_members: list[int] = []
    page_types: dict[int, str] = {}
    for path in sorted((workspace / "plan").glob("slide_[0-9][0-9].md")):
        match = re.fullmatch(r"slide_(\d+)\.md", path.name)
        if not match or _planned_production_group(path) != group_id:
            continue
        number = int(match.group(1))
        planned_members.append(number)
        page_types[number] = _planned_page_type(path)
    if pages != planned_members:
        return False
    if group_id == "bookends":
        if len(pages) == 1:
            return set(page_types.values()) <= {"cover", "closing"}
        return len(pages) == 2 and set(page_types.values()) == {"cover", "closing"}
    return all(
        value == "section-divider" for value in page_types.values()
    )


def _planned_special_group_members(workspace: Path, group_id: str) -> list[int]:
    if group_id not in {"bookends", "dividers"}:
        return []
    members: list[int] = []
    for path in sorted((workspace / "plan").glob("slide_[0-9][0-9].md")):
        match = re.fullmatch(r"slide_(\d+)\.md", path.name)
        if match and _planned_production_group(path) == group_id:
            members.append(int(match.group(1)))
    return members


def _planned_needs_bitmap(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    match = re.search(r"(?mi)^\s*-\s*needs_bitmap\s*:\s*(true|false)\s*$", text)
    return bool(match and match.group(1).lower() == "true")


def _spec_needs_bitmap(root: Path, spec: dict, fallback: int) -> bool:
    if str(spec.get("role", "")).lower() != "slide":
        return False
    return any(
        _planned_needs_bitmap(root / "plan" / f"slide_{page:02d}.md")
        for page in _spec_slide_pages(spec, fallback)
    )


def _child_task_language_envelope(task: str, language: str) -> str:
    """Keep a delegated role's visible prose aligned with the resolved query language.

    Child goals are an internal task envelope, not the user's original message.  Keeping
    the reminder next to the delegated task is important for smaller models: after they
    consume English tool schemas or source pages, a system-only language rule is too easy
    to lose.  The orchestrator still receives the raw query byte-for-byte.
    """
    normalized = normalize_language(language)
    if normalized == "zh":
        reminder = (
            "工作语言：中文。所有可见的过程说明、工具调用前说明、判断与最终状态均使用"
            "中文；代码、命令、路径、原文引语和专有名词可保留原文。"
        )
    elif normalized == "en":
        reminder = (
            "Working language: English. Use English for every visible progress note, "
            "tool-call preamble, judgment, and final status; code, commands, paths, "
            "source quotations, and proper nouns may remain in their original form."
        )
    else:
        return task
    return f"{reminder}\n\n{task}"


def _slide_assignment_envelope(
    task: str,
    pages: tuple[int, ...],
    group_id: str,
    language: str,
) -> str:
    """Make structured ownership authoritative over ambiguous free-form goals."""
    labels = ", ".join(f"P{int(page):02d}" for page in pages)
    if normalize_language(language) == "zh":
        contract = (
            f"[Harness 页面所有权合同] 本 Agent 唯一且完整负责：{labels}。"
            "结构化 pages 高于前面的自然语言 goal；即使 goal 只点名其中一页，也必须完成"
            "全部分配页的 HTML、render 与当前像素检查。"
        )
        if group_id:
            contract += (
                f"这是 `{group_id}` 视觉记忆组；全部页完成后还必须运行 render-group，"
                "查看当前组联系表并做一次组内一致性结论。不得把任一成员称为另一个"
                "责任单元。"
            )
    else:
        contract = (
            f"[Harness page-ownership contract] This Agent exclusively owns all of: {labels}. "
            "The structured pages field overrides narrower wording in the preceding free-form goal; "
            "author, render, and inspect every assigned page."
        )
        if group_id:
            contract += (
                f" This is the `{group_id}` visual-memory group; after all members are complete, "
                "run render-group, inspect the current group contact sheet, and close group coherence."
            )
    # Keep the authoritative ownership receipt *after* the model-authored
    # free-form goal.  A real run (#165) delegated bookends [01,10] while the
    # goal's last paragraph incorrectly claimed that P10 was already complete.
    # Smaller models followed that later sentence and never authored P10 even
    # though the leading receipt said otherwise.  Repeating the compact,
    # structured contract at the end resolves the prompt conflict without
    # parsing or rejecting the Orchestrator's prose.
    return f"{task}\n\n{contract}"


def _persisted_trace_attempts(parent: Agent, label: str) -> int:
    """Count legacy and explicit operational recoveries across resumes."""
    root = Path(parent.ws) / "_trace" / "subagents"
    if not root.is_dir():
        return 0
    highest = 0
    for path in root.iterdir():
        if not path.is_dir():
            continue
        if path.name == label:
            highest = max(highest, 1)
            continue
        match = re.fullmatch(
            re.escape(label) + r"_(?:r|retry|verify|source)(\d+)", path.name
        )
        if match:
            highest = max(highest, int(match.group(1)))
    return highest


def _run_child(parent: Agent, index: int, spec: dict) -> dict:
    role = str(spec.get("role", "")).lower()
    if role not in ROLES:
        return {"label": spec.get("label"), "role": role, "ok": False, "error": "unknown role"}
    if role == "slide":
        slide_pages = _spec_slide_pages(spec, index)
        if len(slide_pages) == 1 and not spec.get("group_id"):
            label = f"slide_{slide_pages[0]:02d}"
        else:
            group_id = re.sub(
                r"[^a-z0-9_-]+",
                "-",
                str(spec.get("group_id") or f"group-{slide_pages[0]:02d}").lower(),
            ).strip("-_")
            group_label = group_id or f"{slide_pages[0]:02d}"
            label = f"slide_group_{group_label}"
    elif role in {"material", "research", "image", "review"}:
        label = role
    else:
        label = f"{role}_{index:02d}"
    child_cfg = dict(parent.cfg)
    in_memory_attempts = sum(
        1 for candidate in parent.children if candidate.label == label
    )
    prior_attempts = max(
        in_memory_attempts,
        _persisted_trace_attempts(parent, label),
    )
    trace_attempt = prior_attempts + 1
    recovery_kind = str(spec.get("_recovery_kind") or "").strip().lower()
    child_cfg["_trace_label"] = label
    if trace_attempt > 1:
        suffix = (
            "source" if recovery_kind == "source_route"
            else "verify" if recovery_kind == "verification"
            else "retry"
        )
        child_cfg["_trace_label"] = f"{label}_{suffix}{trace_attempt}"
    child_cfg["_parent_deadline_monotonic"] = parent.deadline_monotonic
    child_cfg["deck_timeout_s"] = int(
        parent.cfg.get("child_wall_timeout_s", config.CHILD_WALL_TIMEOUT_S)
    )
    child_cfg["response_language"] = (
        _planned_delivery_language(parent.ws) or parent.response_language
    )
    task_text = _child_task_language_envelope(
        str(spec.get("task", "")), child_cfg["response_language"]
    )
    if role == "slide":
        task_text = _slide_assignment_envelope(
            task_text,
            slide_pages,
            str(spec.get("group_id") or ""),
            child_cfg["response_language"],
        )
    child = Agent(
        parent.sid,
        parent.ws,
        task_text,
        child_cfg,
        role=role,
        label=label,
    )
    child.image_repair_mode = bool(role == "image" and spec.get("repair"))
    if child.image_repair_mode:
        if recovery_kind == "source_route":
            repair_contract = (
                "这是一次且仅一次的生产前来源改路续作。只处理上一轮 failed 且尚未渲染的"
                "页面，严格执行当前逐页计划中已改为 bitmap-material/bitmap-generated 的"
                "路线；保留任何 ready 条目，用 patch 修正 failed 条目，重新 finalize 整份"
                "catalog 并复验联系表。不得恢复旧 bitmap-real 路线。"
            )
        else:
            repair_contract = (
                "这是 Review 证据驱动的定向素材修复。必须保留 assets/catalog.md 中所有"
                "未受影响条目；只能用 patch 修改或追加目标条目，不得用 write_file 整体"
                "覆盖 catalog。完成后重新 finalize 整份 catalog，并只报告实际变化的素材"
                "与页面。"
            )
        child.system = (
            f"{child.system.rstrip()}\n\n"
            f"<image_repair_contract>{repair_contract}</image_repair_contract>"
        )
    if role in {"slide", "image", "review"}:
        style_lock = _style_lock_injection(
            parent.ws,
            "zh" if child.skill_language == "zh" else "en",
        )
        if style_lock:
            child.system = f"{child.system.rstrip()}\n\n{style_lock}"
    child.material_required = parent.material_required
    child.material_completed = parent.material_completed
    child.ownership_topology = _ownership_topology(parent)
    if role == "slide":
        child.assigned_slide_pages = slide_pages
        child.slide_group_id = str(spec.get("group_id") or "")
        child.expected_output_paths = {
            page: str(Path(parent.ws) / "slides" / f"slide_{page:02d}.html")
            for page in slide_pages
        }
        child.expected_output_initial_hashes = {
            page: _file_content_digest(Path(path))
            for page, path in child.expected_output_paths.items()
        }
        if len(slide_pages) == 1:
            child.expected_output_path = child.expected_output_paths[slide_pages[0]]
            child.expected_output_initial_hash = child.expected_output_initial_hashes[
                slide_pages[0]
            ]
    elif role == "review":
        child.required_review_pages = tuple(
            sorted(
                {
                    int(page)
                    for page in spec.get("required_review_pages", [])
                    if isinstance(page, int) and not isinstance(page, bool) and page > 0
                }
            )
        )
    parent.children.append(child)
    ok = run_loop(child)
    research_status = ""
    if role == "research" and not ok:
        if tools.research_handoff_is_valid(child):
            ok = True
            child.exit_reason = "recovered_ready_handoff"
            child.final_text = (
                "status: ready\n"
                "artifact: research/knowledge-brief.md\n"
                "evidence: accepted handoff receipt matches the durable brief"
            )
            child.log(
                "[durable research handoff] Research 异常退出，但 canonical brief "
                "与成功写入回执完全一致；直接恢复完成，不创建新 Research Agent。"
            )
            child.finish_snapshot()
        elif (
            str(child.exit_reason or "") not in _OPERATIONAL_CHILD_RETRY_REASONS
            or trace_attempt >= 2
        ):
            research_status = "research_blocked"
            child.exit_reason = "research_blocked"
            child.log(
                "[research blocked] 没有带有效成功回执的 canonical brief；"
                "允许的一次基础设施恢复已用尽或本次并非基础设施中断。"
                "停止下游规划与重复委派。"
            )
            child.finish_snapshot()
    if (
        role == "image"
        and not ok
        and _recover_image_operational_handoff(child, trace_attempt)
    ):
        ok = True
        child.exit_reason = (
            "partial_ready_handoff"
            if child.image_failed_pages
            else "recovered_ready_handoff"
        )
        child.log(
            "[durable image handoff] 从当前 catalog、finalize 回执与视觉检查"
            "恢复保守交接；未被当前证据覆盖的位图页保持 failed，"
            "由 Slide 降级并交给最终 Review。"
        )
        child.finish_snapshot()
    review_stale_pixels = (
        role == "review"
        and not ok
        and _is_durable_review_exit(child.exit_reason)
        and int(getattr(child, "review_patches_since_finalize", 0) or 0) > 0
    )
    if review_stale_pixels:
        child.final_text = (
            "status: needs_orchestrator\n"
            "pages: stale — mutations after last finalize\n"
            "issue_type: review_incomplete_current_pixels\n"
            "evidence: Review ended with unfinalized mutations; "
            f"exit_reason={child.exit_reason}, "
            f"patches_since_finalize={child.review_patches_since_finalize}\n"
            "proposed_fix: resumable closure — finalize current files and "
            "inspect current pixels before delivery\n"
            "blocking: no\n"
            "final_pixels_inspected: no"
        )
        child.exit_reason = "review_incomplete_current_pixels"
        child.log(
            "[review incomplete pixels] Review ended with unfinalized mutations; "
            f"patches_since_finalize={child.review_patches_since_finalize}"
        )
        child.finish_snapshot()
    if (
        role == "review"
        and not ok
        and not review_stale_pixels
        and _is_durable_review_exit(child.exit_reason)
        and _review_has_current_contact_delivery(child)
    ):
        fullres_complete = _review_has_current_inspected_delivery(child)
        review_gap = _review_required_view_gap(child)
        findings = _review_manifest_quality_findings(child)
        open_issues = _open_vision_issues(child)
        evidence_parts = [
            f"bounded Review ended with {child.exit_reason}",
            "the latest finalize succeeded",
            (
                "the current contact sheet and every mandatory full-resolution page were inspected"
                if fullres_complete
                else "the current contact sheet was inspected but mandatory full-resolution coverage is incomplete"
            ),
        ]
        if review_gap and not fullres_complete:
            evidence_parts.append("unclosed Review coverage: " + review_gap[:900])
        if findings:
            evidence_parts.append("deterministic findings remain: " + "; ".join(findings))
        if open_issues:
            evidence_parts.append(f"open Vision issues remain: {len(open_issues)}")
        pages_text = (
            "inspected delivery surface"
            if fullres_complete
            else "current contact sheet; incomplete mandatory full-resolution coverage"
        )
        child.final_text = (
            "status: needs_orchestrator\n"
            f"pages: {pages_text}\n"
            "issue_type: bounded_review_incomplete\n"
            "evidence: " + "; ".join(evidence_parts) + "\n"
            "proposed_fix: retain the current usable deck and address the remaining issue ledger in a later edit\n"
            "blocking: no\n"
            "final_pixels_inspected: yes"
        )
        child.exit_reason = "needs_orchestrator"
        child.log(
            "[durable review handoff] Review 已有当前 finalize 与整册联系表证据；"
            "触发有界止损时，未完成的必看单页显式保留为 needs_improvement，"
            "不创建相同输入的 Review，也不把部分检查冒充 ready。"
        )
        child.finish_snapshot()
    material_status = ""
    if role == "material":
        material_path = Path(parent.ws) / "research" / "material.md"
        try:
            material_text = material_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            material_text = ""
        blocked_decision = _material_blocked_decision(material_text)
        if blocked_decision == "blocked":
            ok = False
            material_status = "material_blocked"
            child.exit_reason = "material_blocked"
            child.log(
                "[material blocked] 附件无法形成可靠证据（blocking: yes）；"
                "停止 Research 与规划，不允许用外部检索猜测缺失内容。"
            )
            child.finish_snapshot()
        elif blocked_decision == "ready_with_gaps":
            ok = True
            material_status = "ready"
            child.exit_reason = "material_partial"
            child.log(
                "[material partial] 文件写了 material_blocked 但无 blocking: yes "
                "且含已成功提取的证据段；视为 ready + unresolved gaps。"
            )
            child.finish_snapshot()
    repair_issue = _slide_repair_issue(child) if role == "slide" else None
    if role == "slide":
        gap = _slide_deliverable_gap(child)
        if not ok and not repair_issue:
            authored_baseline = all(
                bool(_file_content_digest(Path(child.expected_output_paths.get(page, ""))))
                and _file_content_digest(Path(child.expected_output_paths.get(page, "")))
                != str(child.expected_output_initial_hashes.get(page, "") or "")
                for page in slide_pages
            )
            candidate = {
                "status": "repair_required",
                "pages": list(slide_pages),
                "issue_type": "page_authoring",
                "evidence": (
                    f"Slide Agent exited with {child.exit_reason or 'unknown'} after "
                    "producing a durable HTML/PNG baseline; final Review must inspect "
                    "and close the current pixels"
                ),
                "proposed_fix": "inspect the current pixels and apply one bounded Review repair if needed",
                "render_budget_exhausted_pages": [],
            }
            if authored_baseline and _repair_candidate_is_routable(child, candidate):
                repair_issue = candidate
                ok = True
                child.exit_reason = "repair_required_handoff"
                child.log(
                    "[durable handoff] 子 Agent 未正常文字收尾，但 HTML/PNG 基线存在；"
                    "不创建新的 Slide Agent，作为开放 issue 交给最终 Review。"
                )
                child.finish_snapshot()
        if gap and not (
            repair_issue and _repair_candidate_is_routable(child, repair_issue)
        ):
            ok = False
            if child.exit_reason == "text_response":
                child.exit_reason = "incomplete_deliverable"
            child.log(f"[deliverable rejected] {gap}")
            child.finish_snapshot()
        elif repair_issue:
            # A routable pixel/layout issue is a completed ownership handoff,
            # even when the provider timed out before the child could emit its
            # final prose. Review owns the bounded repair; leaving ``ok=False``
            # would incorrectly turn durable HTML/PNG into an unresolved child
            # failure and invite a forbidden Slide recreation.
            ok = True
            child.exit_reason = "repair_required_handoff"
            child.completed_slide_pages = tuple(
                int(page) for page in repair_issue.get("pages", [])
            )
            child.incomplete_slide_pages = ()
    review_status = ""
    review_fields: dict[str, str] = {}
    if role == "review":
        review_fields = _contract_fields(child.final_text)
        review_status = review_fields.get("status", "").lower()
        quality_findings = _review_manifest_quality_findings(child)
        review_gap = _review_required_view_gap(child)
        if (
            review_status == "ready"
            and (quality_findings or review_gap)
            and _review_has_current_inspected_delivery(child)
        ):
            evidence = "; ".join(quality_findings)
            if review_gap:
                evidence = (evidence + "; " if evidence else "") + review_gap[:900]
            child.final_text = (
                "status: needs_orchestrator\n"
                "pages: inspected delivery surface\n"
                "issue_type: unresolved_quality_findings\n"
                "evidence: " + evidence + "\n"
                "proposed_fix: retain the usable deck as needs_improvement and repair the flagged pages in a later edit\n"
                "blocking: no\n"
                "final_pixels_inspected: yes"
            )
            review_fields = _contract_fields(child.final_text)
            review_status = "needs_orchestrator"
            child.log(
                "[quality downgrade] Review 的普通 ready 不能覆盖当前 render.json 中"
                "仍存在的确定性质量问题或未关闭 Vision issue；降级为 needs_improvement。"
            )
        if (review_status == "needs_orchestrator" or review_gap) and not review_stale_pixels:
            ok = False
            child.exit_reason = (
                "needs_orchestrator"
                if review_status == "needs_orchestrator"
                else "required_review_page_not_inspected"
            )
            if review_gap:
                child.log(f"[deliverable rejected] {review_gap}")
            child.finish_snapshot()
    return {
        "label": label,
        "trace_label": child.trace_label,
        "role": role,
        "ok": ok,
        "exit_reason": child.exit_reason,
        "renders": child.n_renders,
        "views": child.n_views,
        "completed_pages": list(getattr(child, "completed_slide_pages", ())),
        "incomplete_pages": list(getattr(child, "incomplete_slide_pages", ())),
        "final_render_after_review": bool(
            getattr(child, "final_render_after_review", False)
        ),
        "final_view_after_review": bool(
            getattr(child, "final_view_after_review", False)
        ),
        "finalize_attempted": bool(getattr(child, "finalize_attempted", False)),
        "finalize_succeeded": bool(getattr(child, "finalize_succeeded", False)),
        "finalize_failure": str(getattr(child, "finalize_failure", ""))[-1200:],
        "trace_mode": dict(getattr(child, "trace_mode_status", {}) or {}),
        "status": (
            "repair_required"
            if repair_issue
            else "partial_ready"
            if role == "image" and getattr(child, "image_failed_pages", ())
            else material_status
            or research_status
            or review_status
            or ("ready" if ok else "failed")
        ),
        "repair_issue": repair_issue,
        "image_ready_pages": list(getattr(child, "image_ready_pages", ()) or ()),
        "image_failed_pages": list(getattr(child, "image_failed_pages", ()) or ()),
        "image_handoff_summary": str(
            getattr(child, "image_handoff_summary", "") or ""
        )[-1800:],
        "required_review_pages": list(
            getattr(child, "required_review_pages", ()) or ()
        ),
        "attempt": trace_attempt,
        "blocking": review_fields.get("blocking", "").lower(),
        "issue_type": review_fields.get("issue_type", "").lower(),
        "pages": review_fields.get("pages", ""),
        "evidence": review_fields.get("evidence", "")[-1200:],
        "input_fingerprint": (
            _review_delivery_fingerprint(parent.ws)
            if role == "review"
            else _image_route_fingerprint(parent.ws)
            if role == "image"
            else ""
        ),
        "final_pixels_inspected": (
            review_fields.get("final_pixels_inspected", "").lower() == "yes"
        ),
        "review_changed": bool(getattr(child, "review_changed", False)),
        "summary": child.final_text[-1800:],
    }


def read_child_concurrency_target(
    control_file: str | None,
    default: int,
    maximum: int,
    previous_valid: int | None,
) -> tuple[int, str, str | None]:
    """Resolve one child-wave target without disturbing active children.

    Invalid or temporarily unreadable content preserves the last valid target.
    Numeric values are safely clamped to the configured pool boundary.
    """
    maximum = max(1, int(maximum))
    initial = max(1, min(maximum, int(default)))
    previous = (
        initial
        if previous_valid is None
        else max(1, min(maximum, int(previous_valid)))
    )
    if not control_file:
        return previous, "static", None
    try:
        raw = Path(control_file).read_text(encoding="utf-8").strip()
    except OSError:
        return previous, "unreadable", None
    try:
        requested = int(raw)
    except ValueError:
        return previous, "invalid", raw[:80]
    if requested < 1:
        return 1, "clamped_min", raw[:80]
    if requested > maximum:
        return maximum, "clamped_max", raw[:80]
    return requested, "ok", raw[:80]


def _child_wave_limit(parent: Agent, task_count: int) -> int:
    """Read the live target once for one synchronous ``delegate_task`` wave."""
    initial = max(
        1,
        int(
            getattr(
                parent,
                "child_concurrency_initial",
                config.CHILD_CONCURRENCY,
            )
        ),
    )
    maximum = max(
        initial,
        int(
            getattr(
                parent,
                "child_concurrency_max",
                config.CHILD_POOL_MAX_WORKERS,
            )
        ),
    )
    control_file = str(
        getattr(
            parent,
            "child_concurrency_file",
            config.CHILD_CONCURRENCY_FILE,
        )
        or ""
    ).strip()
    previous = getattr(parent, "child_concurrency_last_valid", initial)
    target, status, raw = read_child_concurrency_target(
        control_file,
        initial,
        maximum,
        previous,
    )
    parent.child_concurrency_last_valid = target
    effective = max(1, min(target, task_count))
    event = {
        "event": "child_parallelism",
        "raw": raw,
        "target": target,
        "effective": effective,
        "task_count": task_count,
        "maximum": maximum,
        "source": control_file or "static",
        "status": status,
        "level": "warning" if status not in {"ok", "static"} else "info",
    }
    if isinstance(parent, Agent):
        parent.log_structured(
            "child_parallelism",
            **{key: value for key, value in event.items() if key != "event"},
        )
    else:
        parent.log(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
    return effective


def _pending_slide_repair_issues(parent: Agent) -> list[dict]:
    outcomes = getattr(parent, "child_outcomes", {})
    if not isinstance(outcomes, dict):
        return []
    issues: list[dict] = []
    for label, outcome in sorted(outcomes.items()):
        if not isinstance(outcome, dict):
            continue
        issue = outcome.get("repair_issue")
        if not isinstance(issue, dict) or issue.get("status") != "repair_required":
            continue
        issues.append({"label": label, **issue})
    return issues


_MATERIAL_BLOCKING_REASONS = frozenset({
    "unreadable_required_page",
    "no_usable_evidence",
    "unsupported_format",
})


def _material_blocked_decision(text: str) -> str:
    """Classify a material.md that contains ``status: material_blocked``.

    Returns:
      "blocked"         — true terminal failure (blocking: yes with valid reason,
                          or conservative fallback when no evidence of successful
                          ingestion exists).
      "ready_with_gaps" — the file wrote material_blocked but evidence sections
                          show successful ingestion; treat as ready + unresolved.
      ""                — status is not material_blocked at all.
    """
    if not re.search(
        r"(?mi)^\s*(?:[-*]\s*)?status\s*:\s*material_blocked\s*$", text
    ):
        return ""
    has_explicit_blocking = bool(
        re.search(r"(?mi)^\s*(?:[-*]\s*)?blocking\s*:\s*yes\s*$", text)
    )
    blocking_reason_match = re.search(
        r"(?mi)^\s*(?:[-*]\s*)?blocking_reason\s*:\s*(\S+)", text
    )
    blocking_reason = (
        blocking_reason_match.group(1).strip().lower()
        if blocking_reason_match else ""
    )
    if has_explicit_blocking and blocking_reason in _MATERIAL_BLOCKING_REASONS:
        return "blocked"
    if _material_has_ingestion_evidence(text):
        return "ready_with_gaps"
    return "blocked"


def _material_has_ingestion_evidence(text: str) -> bool:
    """Conservative check: did Material successfully ingest at least one attachment?

    Looks for signs of successful structured extraction beyond mere headers:
    - At least one numbered section heading (## N. or 1. / 2. top-level)
    - Multiple evidence list items with source citations (file/page refs)
    - Total content suggesting real extraction (not just a stub error report)
    """
    if len(text) < 2048:
        return False
    section_headings = len(re.findall(
        r"(?m)^#{1,3}\s+\d+[\.\)]\s+\S", text
    ))
    numbered_items = len(re.findall(
        r"(?m)^\s*\d+[.)]\s+\S", text
    ))
    evidence_items = len(re.findall(
        r"(?m)^\s*[-*]\s+\S", text
    ))
    has_source_refs = bool(re.search(
        r"(?:page|页|p\.\s*\d|附件|attachment|Figure|Table)", text, re.IGNORECASE
    ))
    structured_items = section_headings + numbered_items + evidence_items
    return structured_items >= 12 and has_source_refs


def _material_handoff_gap(agent: Agent) -> str:
    """Keep a recoverable Material status correction in the same Agent.

    Missing concepts in otherwise readable attachments are evidence gaps, not
    ingestion failures.  Give the owner a bounded chance to correct the
    canonical status before the post-child conservative fallback is needed.
    """
    if str(getattr(agent, "role", "") or "").lower() != "material":
        return ""
    path = Path(agent.ws) / "research" / "material.md"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if _material_blocked_decision(text) != "ready_with_gaps":
        return ""
    return (
        "附件已成功提取出可追溯证据；当前缺失的是附件未覆盖的概念，不是附件不可读。"
        "请在同一 research/material.md 中把 status 改为 ready，删除 blocking 字段，"
        "保留现有证据，并把缺失概念放入 unresolved_items 后重新收尾。"
    )


def _open_vision_issues(parent: Agent) -> list[dict]:
    path = Path(parent.ws) / "_trace" / "vision-issues.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [
        dict(item) for item in payload.get("issues", [])
        if (
            isinstance(item, dict)
            and item.get("status") == "open"
            and isinstance(item.get("page"), int)
            and not isinstance(item.get("page"), bool)
            and int(item.get("page")) > 0
        )
    ] if isinstance(payload, dict) else []


def _mandatory_fullres_review_pages(parent: Agent) -> set[int]:
    """Identity-critical and high-confidence flagged pages need full pixels."""
    root = Path(parent.ws)
    pages: set[int] = set()
    for path in sorted((root / "plan").glob("slide_[0-9][0-9].md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = re.search(r"slide_(\d+)", path.stem)
        if not match:
            continue
        page = int(match.group(1))
        if re.search(r"(?mi)^\s*-\s*needs_bitmap\s*:\s*true\s*$", text):
            pages.add(page)
    try:
        manifest = json.loads((root / "renders/render.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = {}
    # The contact sheet covers every page. Generic type-size, possible-DOM,
    # composition and raw-density warnings are model-facing context, not proof
    # that a full-resolution page is mandatory. Durable Slide/Vision handoffs
    # are unioned by the caller; only high-confidence manifest findings below
    # are promoted here.
    layout_defects = manifest.get("layout_defects") if isinstance(manifest, dict) else {}
    if isinstance(layout_defects, dict):
        for page_text, defects in layout_defects.items():
            if isinstance(defects, list) and defects and str(page_text).isdigit():
                pages.add(int(page_text))
    for key in ("media_mismatches", "placeholder_flags"):
        flags = manifest.get(key) if isinstance(manifest, dict) else {}
        if not isinstance(flags, dict):
            continue
        for page_text, evidence in flags.items():
            if evidence and str(page_text).isdigit():
                pages.add(int(page_text))
    return {page for page in pages if page > 0}


def _review_can_complete_needs_improvement(parent: Agent, result: dict) -> bool:
    """Return whether a bounded, inspected Review issue is deliverable.

    ``blocking`` describes whether the issue is still must-fix for a polished
    release; it must not turn a usable, finalized deck into a failed job after
    the Review's bounded in-session repair cycle. The distinction remains
    visible through the top-level ``needs_improvement`` quality status.
    """
    if bool(result.get("review_changed")):
        current_pixels = bool(result.get("finalize_succeeded")) and bool(
            result.get("final_view_after_review")
        )
    else:
        # A recovered no-edit Review may have completed its own successful
        # finalize even though the parent has not yet consumed that child
        # result. Trust the child-local receipt carried in ``result``; relying
        # on the still-stale parent flag turns a fully inspected usable Deck
        # into a false whole-run failure.
        current_pixels = bool(result.get("finalize_succeeded")) and bool(
            result.get("final_view_after_review")
        )
    return bool(
        not bool(result.get("ok"))
        and result.get("status") == "needs_orchestrator"
        and str(result.get("blocking") or "no").lower() != "yes"
        and bool(result.get("final_pixels_inspected"))
        and current_pixels
    )


def _review_has_current_inspected_delivery(agent: Agent) -> bool:
    """Return whether a bounded Review can safely hand off current pixels.

    This is deliberately weaker than ``_review_required_view_gap``: open
    quality issues are allowed because the recovered outcome is
    ``needs_improvement``, never ``ready``.  It still requires a successful
    finalize after the last edit, the current whole-deck contact sheet, and
    every mandatory full-resolution page at its current hash.
    """
    if str(getattr(agent, "role", "") or "").lower() != "review":
        return False
    if not bool(getattr(agent, "finalize_succeeded", False)):
        return False
    if not bool(getattr(agent, "final_view_after_review", False)):
        return False
    if not bool(getattr(agent, "review_contact_sheet_inspected", False)):
        return False
    root = Path(agent.ws)
    viewed = dict(getattr(agent, "review_viewed_page_hashes", {}) or {})
    for page in tuple(getattr(agent, "required_review_pages", ()) or ()):
        digest = _file_content_digest(root / "renders" / f"slide_{int(page):02d}.png")
        if not digest or viewed.get(int(page)) != digest:
            return False
    return True


def _review_has_current_contact_delivery(agent: Agent) -> bool:
    """Return whether Review has a current whole-deck delivery surface.

    This is the bounded ``needs_improvement`` floor, not the ``ready`` gate.
    It requires a successful finalize after the last edit and a fresh
    whole-deck contact-sheet inspection. Missing mandatory full-resolution
    pages remain explicit unresolved evidence and can never be promoted to
    ``ready``, but they also do not make an otherwise usable Deck fail as an
    infrastructure error after the single Review lifecycle has stopped.
    """
    if str(getattr(agent, "role", "") or "").lower() != "review":
        return False
    return bool(
        getattr(agent, "finalize_succeeded", False)
        and getattr(agent, "final_render_after_review", False)
        and getattr(agent, "final_view_after_review", False)
        and getattr(agent, "review_contact_sheet_inspected", False)
    )


def _review_manifest_quality_findings(agent: Agent) -> list[str]:
    """Return current deterministic quality findings that forbid plain ready.

    These findings are soft delivery signals by design: they downgrade the
    result to ``needs_improvement`` after bounded Review, but never make an
    otherwise renderable Deck fail as a whole.
    """
    try:
        manifest = json.loads(
            (Path(agent.ws) / "renders" / "render.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    findings: list[str] = []
    for key in (
        "layout_defects",
        "typography_flags",
        "media_mismatches",
        "placeholder_flags",
    ):
        payload = manifest.get(key) if isinstance(manifest, dict) else None
        if not isinstance(payload, dict):
            continue
        pages = sorted(
            str(page) for page, evidence in payload.items() if evidence
        )
        if pages:
            findings.append(f"{key}:" + ",".join(pages))
    console_errors = manifest.get("console_errors") if isinstance(manifest, dict) else None
    if isinstance(console_errors, list) and console_errors:
        findings.append(f"console_errors:{len(console_errors)}")
    return findings


def _render_console_errors(ws: str) -> list[str]:
    """Extract top-level console_errors from render.json for Review injection."""
    try:
        manifest = json.loads(
            (Path(ws) / "renders" / "render.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    errors = manifest.get("console_errors") if isinstance(manifest, dict) else None
    if not isinstance(errors, list) or not errors:
        return []
    return [str(e)[:200] for e in errors[:8]]


def _pages_with_active_media(ws: str) -> set[int]:
    """Return page numbers that have Canvas, ECharts, or SVG media in render.json."""
    try:
        manifest = json.loads(
            (Path(ws) / "renders" / "render.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return set()
    pages: set[int] = set()
    for item in manifest.get("pages", []) if isinstance(manifest, dict) else []:
        if not isinstance(item, dict):
            continue
        geo = item.get("geometry")
        if not isinstance(geo, dict):
            continue
        inv = geo.get("media_inventory", {})
        if not isinstance(inv, dict):
            continue
        kinds = set(inv.get("kinds", []))
        if kinds.intersection({"canvas", "echarts", "svg"}):
            page_num = item.get("page") or geo.get("page_number")
            if isinstance(page_num, int) and page_num > 0:
                pages.add(page_num)
    return pages

def _review_typography_evidence(ws: str, language: str) -> str:
    """Build the typography evidence tag injected into Review task text.

    Pure function: reads render.json from *ws*, formats at most 8 pages with
    up to 3 items each, returns the full XML tag string or empty string.
    """
    try:
        manifest = json.loads(
            (Path(ws) / "renders" / "render.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ""
    typo_flags = manifest.get("typography_flags") if isinstance(manifest, dict) else None
    if not isinstance(typo_flags, dict) or not typo_flags:
        return ""
    severe_summary: list[str] = []
    for page_key, evidence_list in sorted(typo_flags.items()):
        if not evidence_list:
            continue
        if isinstance(evidence_list, list):
            items = ", ".join(str(e)[:80] for e in evidence_list[:3])
        else:
            items = str(evidence_list)[:200]
        severe_summary.append(f"P{page_key}: {items}")
    if not severe_summary:
        return ""
    typo_text = "; ".join(severe_summary[:8])
    if language == "zh":
        return (
            "<harness_typography_evidence>"
            "以下页面在 render.json 中有投影字号"
            "警告（< 20px 正文/标签/图例），"
            "属于 Skill 明确禁止的尺寸。不能以"
            "“有意为之的报告风格”笼统"
            "关闭；必须针对每页给出像素"
            "级理由或修复。"
            f"\n{typo_text}</harness_typography_evidence>"
        )
    return (
        "<harness_typography_evidence>The following pages have projection-"
        "readability typography warnings (< 20px body/labels/legends) which "
        "the Skill explicitly prohibits. Generic dismissal such as 'intentional "
        "report style' is insufficient; provide a page-specific pixel reason or "
        f"repair.\n{typo_text}</harness_typography_evidence>"
    )


DURABLE_REVIEW_EXIT_REASONS: frozenset[str] = frozenset({
    "incomplete_closure",
    "stalled_repetition",
    "max_turns",
    "required_review_page_not_inspected",
})


def _is_durable_review_exit(reason: str) -> bool:
    """Return whether a Review exit reason qualifies for durable handoff."""
    return str(reason or "") in DURABLE_REVIEW_EXIT_REASONS


def _auto_retry_unstarted_slide_interruptions(
    parent: Agent,
    specs: list[dict],
    results: list[dict],
    limit: int,
) -> list[dict]:
    """Retry infrastructure-interrupted Slides once inside the same wave.

    A Slide with durable HTML/PNG or a routable repair issue is already a
    Review handoff and must never be recreated. Only an operational first
    attempt with no completed page baseline is eligible. Keeping this retry
    inside ``delegate_task`` avoids requiring another Orchestrator model turn
    merely to repeat the identical structured responsibility spec.
    """
    retry_indices = [
        index
        for index, (spec, result) in enumerate(zip(specs, results))
        if str(spec.get("role", "")).lower() == "slide"
        and isinstance(result, dict)
        and not bool(result.get("ok"))
        and str(result.get("exit_reason") or "")
        in _OPERATIONAL_CHILD_RETRY_REASONS
        # ``asset_pending`` requires an Orchestrator plan decision first.  It
        # is retryable on a later delegate call after the page medium changes,
        # but must never be auto-replayed unchanged inside the same wave.
        and str(result.get("exit_reason") or "") != "asset_pending"
        and int(result.get("attempt") or 1) < 2
        and not result.get("completed_pages")
        and not result.get("repair_issue")
    ]
    if not retry_indices:
        return results
    parent.log(
        "Harness 在同一委派波内自动恢复无首稿的基础设施中断 Slide："
        + ", ".join(str(specs[index].get("label") or index + 1) for index in retry_indices)
    )
    with futures.ThreadPoolExecutor(max_workers=max(1, min(limit, len(retry_indices)))) as pool:
        jobs = {}
        for index in retry_indices:
            retry_spec = dict(specs[index])
            retry_spec["_recovery_kind"] = "operational"
            jobs[pool.submit(_run_child, parent, index + 1, retry_spec)] = index
        for job in futures.as_completed(tuple(jobs)):
            results[jobs[job]] = job.result()
    return results


_OPERATIONAL_CHILD_RETRY_REASONS = {
    "api_failed",
    "runtime_timeout",
    "empty_giveup",
    "asset_pending",
}


def _operational_child_retry_allowed(previous: dict) -> bool:
    """Only one infrastructure interruption recovery may create a fresh child."""
    return bool(
        isinstance(previous, dict)
        and not bool(previous.get("ok"))
        and str(previous.get("exit_reason") or "")
        in _OPERATIONAL_CHILD_RETRY_REASONS
        and int(previous.get("attempt") or 1) < 2
    )


_ARTIFACT_COMPLETION_RETRY_REASONS = {"max_turns", "stalled_repetition"}


def _artifact_completion_retry_allowed(
    previous: dict, ws: str = "", pages: tuple[int, ...] = (),
) -> bool:
    """Allow exactly one retry when a Slide died without complete deliverable pixels.

    Conditions (ALL must hold):
      - exit_reason in {max_turns, stalled_repetition}
      - no repair_issue (not a quality handoff)
      - at least one responsibility page still lacks a PNG on disk
      - attempt < 2 (first failure only)

    For Single slides, completed_pages being non-empty implies a PNG exists and
    disqualifies retry. For Grouped slides, partial completion is expected —
    the check is whether ANY responsibility page still has no PNG.
    """
    if not isinstance(previous, dict):
        return False
    if bool(previous.get("ok")):
        return False
    exit_reason = str(previous.get("exit_reason") or "")
    if exit_reason not in _ARTIFACT_COMPLETION_RETRY_REASONS:
        return False
    if previous.get("repair_issue"):
        return False
    if int(previous.get("attempt") or 1) >= 2:
        return False
    if ws and pages:
        renders = Path(ws) / "renders"
        has_missing_png = any(
            not (renders / f"slide_{page:02d}.png").is_file()
            for page in pages
        )
        if not has_missing_png:
            return False
    elif not pages:
        if previous.get("completed_pages"):
            return False
    return True


def _outcome_for_page(outcomes: dict, page: int) -> tuple[str, dict] | tuple[str, None]:
    """Find the child_outcomes entry covering *page* (single or grouped)."""
    single_label = f"slide_{page:02d}"
    if single_label in outcomes:
        return single_label, outcomes[single_label]
    for label, outcome in outcomes.items():
        if not label.startswith("slide_group_"):
            continue
        if not isinstance(outcome, dict):
            continue
        all_pages = set(outcome.get("completed_pages") or []) | set(
            outcome.get("incomplete_pages") or []
        )
        if not all_pages:
            pages_field = outcome.get("pages")
            if isinstance(pages_field, list):
                all_pages = set(pages_field)
        if page in all_pages:
            return label, outcome
    return "", None


def _page_has_terminal_slide_failure(parent: "Agent", page: int) -> bool:
    """True when the Slide for *page* failed and no further retry is allowed.

    Terminal means: the child ran (has trace_label), is not ok, and qualifies
    for neither operational retry nor artifact-completion retry.
    Works for both Single (slide_NN) and Grouped (slide_group_*) labels.
    """
    outcomes = dict(getattr(parent, "child_outcomes", {}) or {})
    label, previous = _outcome_for_page(outcomes, page)
    if not isinstance(previous, dict):
        return False
    if bool(previous.get("ok")):
        return False
    if not previous.get("trace_label"):
        return False
    if _operational_child_retry_allowed(previous):
        return False
    ws = str(getattr(parent, "ws", "") or "")
    responsibility_pages = _responsibility_pages_from_outcome(label, previous)
    if _artifact_completion_retry_allowed(previous, ws=ws, pages=responsibility_pages):
        return False
    return True


def _responsibility_pages_from_outcome(label: str, outcome: dict) -> tuple[int, ...]:
    """Extract the full set of responsibility pages from an outcome dict."""
    all_pages: set[int] = set()
    for key in ("completed_pages", "incomplete_pages"):
        for page in (outcome.get(key) or []):
            if isinstance(page, int) and not isinstance(page, bool) and page > 0:
                all_pages.add(page)
    if not all_pages and label.startswith("slide_"):
        match = re.fullmatch(r"slide_(\d+)", label)
        if match:
            all_pages.add(int(match.group(1)))
    return tuple(sorted(all_pages))


def _review_verification_allowed(parent: Agent, previous: dict) -> tuple[bool, str]:
    """Allow a post-handoff verifier only after the delivery surface changed."""
    if not isinstance(previous, dict):
        return False, "missing previous Review state"
    if str(previous.get("status") or "") != "needs_orchestrator":
        return False, "previous Review did not request an external handoff"
    if int(previous.get("attempt") or 1) >= config.REVIEW_MAX_ATTEMPTS:
        return False, "Review lifecycle has reached its bounded verification limit"
    before = str(previous.get("input_fingerprint") or "")
    after = _review_delivery_fingerprint(parent.ws)
    if not before or before == after:
        return False, (
            "Review 请求外部修复后，plan/slides/assets/base.css/speech/present.html "
            "均未发生变化；禁止在相同输入上重复验收"
        )
    return True, ""


def _reconcile_review_issues_ledger(parent: Agent) -> None:
    """Sync review-issues.json with current vision state after Review completion."""
    ledger_path = Path(parent.ws) / "_trace" / "review-issues.json"
    if not ledger_path.is_file():
        return
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    still_open = [
        str(issue.get("id"))
        for issue in _open_vision_issues(parent)
        if issue.get("id")
    ]
    ledger["open_vision_issue_ids"] = still_open
    if not still_open:
        for issue in ledger.get("issues", []):
            if isinstance(issue, dict) and issue.get("status") == "repair_required":
                issue["status"] = "closed_by_review"
        ledger["status"] = "resolved"
    _write_json(ledger_path, ledger)


def _invalidate_review_after_image_repair(parent: Agent) -> None:
    """Reopen Review after its external asset repair changed the deck."""
    parent.review_completed = False
    parent.final_render_after_review = False
    parent.final_view_after_review = False
    parent.finalize_attempted = False
    parent.finalize_succeeded = False
    parent.finalize_failure = ""
    parent.quality_status = "needs_improvement"

    outcomes = getattr(parent, "child_outcomes", None)
    if not isinstance(outcomes, dict):
        return
    previous = outcomes.get("review")
    if not isinstance(previous, dict):
        return
    reopened = dict(previous)
    reopened.update({
        "ok": False,
        "status": "needs_orchestrator",
        "exit_reason": "external_repair_completed",
        "blocking": False,
    })
    outcomes["review"] = reopened


def _claim_image_repair_evidence(parent: Agent) -> tuple[bool, str]:
    """Consume the one deck-level Review-driven Image repair opportunity."""
    evidence = ""
    for child in reversed(tuple(getattr(parent, "children", ()) or ())):
        if str(getattr(child, "role", "") or "").lower() != "review":
            continue
        final_text = str(getattr(child, "final_text", "") or "")
        if _contract_fields(final_text).get("issue_type", "").lower() == "asset_quality":
            evidence = final_text
            break
    if not evidence:
        return False, "当前没有 Review 返回的 issue_type: asset_quality 证据"
    state_path = Path(parent.ws) / "_trace" / "image-repair-state.json"
    if state_path.is_file():
        return False, (
            "本 Deck 的一次 Review 驱动 Image repair 已经消费；旧 asset_quality "
            "证据不能再次创建 image_verify Agent"
        )
    _write_json(
        state_path,
        {
            "schema": "mural.image-repair-state.v1",
            "status": "claimed",
            "review_evidence_sha256": hashlib.sha256(
                evidence.encode("utf-8")
            ).hexdigest(),
            "claimed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    )
    return True, ""


def _orchestrator_delivery_close_status(agent: Agent) -> str:
    """Return a final delivery status once no authorized repair route remains."""
    if str(getattr(agent, "role", "") or "").lower() != "orchestrator":
        return ""
    if not (
        bool(getattr(agent, "review_completed", False))
        and bool(getattr(agent, "finalize_succeeded", False))
        and bool(getattr(agent, "final_view_after_review", False))
    ):
        return ""
    review = dict(getattr(agent, "child_outcomes", {}) or {}).get("review")
    issue_type = (
        str(review.get("issue_type") or "").strip().lower()
        if isinstance(review, dict)
        else ""
    )
    repair_state = Path(agent.ws) / "_trace" / "image-repair-state.json"
    if issue_type == "asset_quality" and not repair_state.is_file():
        return ""
    return str(getattr(agent, "quality_status", "") or "ready")


def _review_preflight_repair_pages(parent: Agent) -> tuple[int, ...]:
    """Return page-local finalize blockers that Review may repair before montage.

    This escape hatch is deliberately narrow: every planned page must already
    have durable HTML and single-page pixels, finalize must have been attempted,
    and the failure must name a concrete Slide HTML plus a deterministic page
    contract/CSS violation. Missing pages, dependencies, render capture, or an
    unattempted final build remain hard stage blockers.
    """
    if not bool(getattr(parent, "finalize_attempted", False)):
        return ()
    if bool(getattr(parent, "finalize_succeeded", False)):
        return ()
    failure = str(getattr(parent, "finalize_failure", "") or "")
    lower = failure.lower()
    if not any(
        marker in lower
        for marker in (
            "must not move shared",
            "blocked properties",
            "_validate_page_css",
            "page css",
            "fragment contract",
            "changed scaffold-owned",
            "root classes",
        )
    ):
        return ()
    pages = tuple(
        sorted({int(value) for value in re.findall(r"slide_0*(\d+)\.html", failure, re.I)})
    )
    if not pages:
        return ()
    root = Path(parent.ws)
    planned = sorted(
        int(match.group(1))
        for path in (root / "plan").glob("slide_[0-9][0-9].md")
        if (match := re.fullmatch(r"slide_(\d+)\.md", path.name))
    )
    if not planned:
        return ()
    for page in planned:
        html = root / "slides" / f"slide_{page:02d}.html"
        png = root / "renders" / f"slide_{page:02d}.png"
        if not html.is_file() or html.stat().st_size < 1:
            return ()
        if not png.is_file() or png.stat().st_size < 1:
            return ()
    return tuple(page for page in pages if page in set(planned))


def _revision_delegation_error(parent: Agent, specs: list[dict]) -> str:
    """Lock one explicit edit route before a revision starts mutating pages."""
    if not bool(getattr(parent, "revision_mode", False)):
        return ""
    roles = [str(spec.get("role", "")).lower() for spec in specs]
    simple_review = bool(
        len(specs) == 1
        and roles == ["review"]
        and re.search(
            r"\bmode\s*=\s*simple_edit\b",
            str(specs[0].get("task") or ""),
            flags=re.IGNORECASE,
        )
    )
    route = str(getattr(parent, "revision_route", "") or "")
    if not route:
        if simple_review:
            parent.revision_route = "simple_edit"
            return ""
        impact_map = Path(parent.ws) / "plan" / "revision-impact.md"
        if not impact_map.is_file() or not impact_map.read_text(
            encoding="utf-8", errors="replace"
        ).strip():
            return (
                "复杂编辑路由尚未建立影响图。先只读检查现有计划、页面、素材、逐页计划中的初版口语讲稿与渲染，"
                "把事实、叙事、页序、全局样式、素材、页面和初版口语讲稿的影响范围写入 "
                "`plan/revision-impact.md`，再只委派实际受影响的角色。"
            )
        parent.revision_route = "complex_edit"
        route = "complex_edit"
    if route == "simple_edit" and not simple_review:
        return (
            "编辑路由已锁定为 simple_edit：只能使用唯一 "
            "`Review: mode=simple_edit`，不得再委派 Research、Material、Image 或 Slide。"
        )
    if route == "complex_edit":
        if simple_review:
            return (
                "编辑路由已锁定为 complex_edit，不能切换到 simple_edit。"
                "按影响图完成受影响角色后，使用 `Review: mode=final_review` 收口。"
            )
        if "review" in roles and len(roles) > 1:
            return (
                "复杂编辑的最终 Review 必须单独委派，并等待所有受影响的 "
                "Research、Material、Image 与 Slide 完成。"
            )
        if "review" in roles and not re.search(
            r"\bmode\s*=\s*final_review\b",
            str(specs[0].get("task") or ""),
            flags=re.IGNORECASE,
        ):
            return "复杂编辑的 Review goal 必须显式包含 `mode=final_review`。"
    return ""


def _delegate(parent: Agent, args: dict) -> str:
    specs = args.get("tasks")
    if not isinstance(specs, list) or not specs:
        return "delegate_task 错误：tasks 必须是非空数组"
    specs = [dict(spec) for spec in specs]
    route_error = _revision_delegation_error(parent, specs)
    if route_error:
        return route_error
    requested_roles = {str(spec.get("role", "")).lower() for spec in specs}
    review_preflight_pages = (
        _review_preflight_repair_pages(parent)
        if "review" in requested_roles
        else ()
    )
    for spec in specs:
        if str(spec.get("role", "")).lower() == "material":
            # Material is a singleton role. One agent reads every attachment
            # and keeps per-file provenance inside one canonical note.
            spec["label"] = "material"
        if str(spec.get("role", "")).lower() == "research":
            # Research is a singleton role. Keep its trace name canonical so a
            # one-agent run never looks like a sharded research_01/research_02 flow.
            spec["label"] = "research"
        if str(spec.get("role", "")).lower() == "image":
            spec["label"] = "image"
        if str(spec.get("role", "")).lower() != "review":
            continue
        spec["label"] = "review"
        requested_task = str(spec.get("task") or "")
        simple_edit = bool(
            re.search(
                r"\bmode\s*=\s*simple_edit\b",
                requested_task,
                flags=re.IGNORECASE,
            )
        )
        if simple_edit:
            instruction = str(parent.cfg.get("_revision_instruction") or "").strip()
            review_task = (
                "mode=simple_edit。只按用户修改要求和 goal 中界定的目标页执行 Review 快修；"
                "不得扩展为整册重写，也不得启动其他角色。用户修改要求："
                f"{instruction}\n受控 goal：{requested_task}"
                if parent.skill_language == "zh"
                else
                "mode=simple_edit. Apply only the user revision and target-page scope "
                "bounded by the goal; do not expand into a whole-deck rewrite or start "
                f"another role. User revision: {instruction}\nControlled goal: {requested_task}"
            )
        elif review_preflight_pages:
            page_text = ", ".join(f"P{page:02d}" for page in review_preflight_pages)
            failure = str(getattr(parent, "finalize_failure", "") or "")[-1200:]
            review_task = (
                "mode=preflight_repair。全部逐页 HTML/PNG 已存在，但首次整册 finalize 在生成 "
                f"contact sheet 前被具体页面合同错误阻断：{page_text}。不要等待不存在的联系表，"
                "先读取这些页源码，按下列确定性错误做一次最小协调 patch；随后运行 review.py "
                "finalize，成功生成当前 contact sheet 后，再按最终 Review 清单检查整册与修改页。"
                f"\nfinalize_error:\n{failure}"
                if parent.skill_language == "zh"
                else
                "mode=preflight_repair. All per-page HTML/PNG baselines exist, but the first "
                f"whole-deck finalize was blocked before contact-sheet creation by a page-local "
                f"contract error on {page_text}. Do not wait for the absent contact sheet. Apply "
                "one minimal coordinated patch to those page sources, run review.py finalize, then "
                "inspect the resulting current contact sheet and changed pages."
                f"\nfinalize_error:\n{failure}"
            )
            spec["_review_preflight_repair_pages"] = list(review_preflight_pages)
        else:
            review_task = (
                "mode=final_review。读取 renders/contact-sheet.png、可用时的 "
                "contact-sheet-special.png、plan/deck.md 与 speech.md，按 Review "
                "角色卡完成整册复审。"
                if parent.skill_language == "zh"
                else
                "mode=final_review. Review the completed deck from "
                "renders/contact-sheet.png, the special contact sheet when present, "
                "plan/deck.md, and speech.md using the Review role card."
            )
        repair_issues = _pending_slide_repair_issues(parent)
        vision_issues = _open_vision_issues(parent)
        console_errors = _render_console_errors(parent.ws)
        console_error_pages = (
            _pages_with_active_media(parent.ws) if console_errors else set()
        )
        required_pages = sorted(
            {
                int(page)
                for issue in repair_issues
                for page in issue.get("pages", [])
                if isinstance(page, int) and not isinstance(page, bool) and page > 0
            }.union({
                int(issue["page"])
                for issue in vision_issues
                if isinstance(issue.get("page"), int) and issue["page"] > 0
            }).union(_mandatory_fullres_review_pages(parent)).union(review_preflight_pages)
            .union(console_error_pages)
        )
        spec["required_review_pages"] = required_pages
        if required_pages or console_errors:
            ledger_path = Path(parent.ws) / "_trace" / "review-issues.json"
            injected_issues = {
                "required_review_pages": required_pages,
                "slide_handoffs": repair_issues,
                "open_vision_issues": [
                    {
                        key: issue.get(key)
                        for key in ("id", "page", "type", "severity", "summary", "evidence")
                        if issue.get(key) not in (None, "")
                    }
                    for issue in vision_issues
                ],
            }
            if console_errors:
                injected_issues["console_errors"] = console_errors
            _write_json(
                ledger_path,
                {
                    "status": "repair_required",
                    "required_review_pages": required_pages,
                    "issues": repair_issues,
                    "vision_issue_ledger": "_trace/vision-issues.json",
                    "open_vision_issue_ids": [
                        str(issue.get("id")) for issue in vision_issues if issue.get("id")
                    ],
                },
            )
            pages_text = ", ".join(f"P{page:02d}" for page in required_pages)
            issue_text = json.dumps(
                injected_issues,
                ensure_ascii=False,
                separators=(",", ":"),
            )[:5000]
            review_task += (
                " Harness 已直接注入精简 issue 摘要，不要读取 `_trace/**`。其中的图片页、"
                f"密集页或已知问题页 {pages_text} 必须逐页打开当前 PNG，不能只看联系表。"
                "修复后重新 finalize 并复看这些页的当前最终像素，关闭 issue 后才能返回 "
                f"ready。\n<harness_review_issues>{issue_text}</harness_review_issues>"
                if parent.skill_language == "zh"
                else
                " Harness has injected the compact issue summary below; do not read `_trace/**`. "
                f"The image-heavy, dense, or flagged pages {pages_text} require individual "
                "current-PNG inspection, not contact-sheet-only checks. After repair, finalize "
                "and reopen their current final pixels before returning ready."
                f"\n<harness_review_issues>{issue_text}</harness_review_issues>"
            )
        _typo_evidence = _review_typography_evidence(
            parent.ws, parent.skill_language or "zh"
        )
        if _typo_evidence:
            review_task += "\n" + _typo_evidence
        spec["task"] = review_task

    # Labels are durable responsibility identities. A Slide that returned
    # ``repair_required`` is complete-with-an-open-issue, not permission to
    # create slide_*_r2. The final Review inherits that issue. Fresh child
    # instances are reserved for operational interruptions; a Review verifier
    # additionally requires an actual delivery-surface change after its
    # structured external handoff.
    requested_count = len(specs)
    existing_outcomes = getattr(parent, "child_outcomes", None)
    if not isinstance(existing_outcomes, dict):
        existing_outcomes = {}
        parent.child_outcomes = existing_outcomes
    skipped_completed_labels: list[str] = []
    pending_specs: list[dict] = []
    for spec in specs:
        label = str(spec.get("label") or "")
        previous = existing_outcomes.get(label) if label else None
        role = str(spec.get("role", "")).lower()
        if not isinstance(previous, dict):
            pending_specs.append(spec)
            continue

        if role == "slide":
            if bool(previous.get("ok")):
                skipped_completed_labels.append(label)
                continue
            if (
                str(previous.get("exit_reason") or "")
                in {"asset_pending", "image_failed"}
                and not previous.get("trace_label")
                and not previous.get("completed_pages")
            ):
                # This was a scheduler placeholder for a Slide that never
                # started, not a failed Agent attempt. After Image recovery or
                # explicit plan reclassification, run the responsibility once
                # under its canonical label (no slide_*_retry suffix).
                pending_specs.append(spec)
                continue
            if _operational_child_retry_allowed(previous):
                spec["_recovery_kind"] = "operational"
                pending_specs.append(spec)
                continue
            responsibility_pages = _responsibility_pages_from_outcome(label, previous)
            if not responsibility_pages:
                responsibility_pages = tuple(
                    int(p) for p in (spec.get("pages") or [])
                    if isinstance(p, int) and not isinstance(p, bool) and p > 0
                )
            if _artifact_completion_retry_allowed(
                previous, ws=str(getattr(parent, "ws", "") or ""),
                pages=responsibility_pages,
            ):
                spec["_recovery_kind"] = "artifact_completion"
                pending_specs.append(spec)
                continue
            return (
                f"责任单元 {label} 已结束，exit_reason="
                f"{previous.get('exit_reason') or 'unknown'}；这不是可创建新 Slide Agent "
                "的基础设施中断。已有 HTML/PNG 或开放问题交给最终 Review；若没有任何"
                "有效产物则保留为明确失败，不要重复委派。"
            )

        if role == "review":
            if bool(previous.get("ok")):
                skipped_completed_labels.append(label)
                continue
            allowed, reason = _review_verification_allowed(parent, previous)
            if allowed:
                spec["_recovery_kind"] = "verification"
                pending_specs.append(spec)
                continue
            if _operational_child_retry_allowed(previous):
                spec["_recovery_kind"] = "operational"
                pending_specs.append(spec)
                continue
            return "Review 重复委派错误：" + reason + "。"

        if bool(previous.get("ok")) and not bool(spec.get("repair")):
            skipped_completed_labels.append(label)
            continue
        if bool(previous.get("ok")) and bool(spec.get("repair")):
            if role != "image" and not bool(getattr(parent, "revision_mode", False)):
                return (
                    f"{label} 已完成；普通生产中 repair=true 只允许用于有明确素材证据的 "
                    "Image 替换。Slide 问题由最终 Review 接手。"
                )
            spec["_recovery_kind"] = "verification"
            pending_specs.append(spec)
            continue
        if _operational_child_retry_allowed(previous):
            spec["_recovery_kind"] = "operational"
            pending_specs.append(spec)
            continue
        return (
            f"{label} 上次退出 {previous.get('exit_reason') or 'unknown'}，"
            "不是允许创建新 Agent 的基础设施中断；请按结构化状态继续或结束。"
        )
    if not pending_specs:
        if "image" in {
            str(spec.get("role", "")).lower() for spec in specs
        } and bool(getattr(parent, "image_completed", False)):
            missing_renders = set(tools.missing_rendered_plan_pages(parent))
            ready_pages = {
                int(page)
                for page in (
                    getattr(parent, "image_ready_pages", ()) or ()
                )
                if isinstance(page, int) and not isinstance(page, bool) and page > 0
            }
            delegatable = {
                page
                for page in missing_renders
                if (
                    page in ready_pages
                    or not _planned_needs_bitmap(
                        Path(parent.ws) / "plan" / f"slide_{page:02d}.md"
                    )
                )
                and not _page_has_terminal_slide_failure(parent, page)
            }
            if delegatable:
                parent.pending_slide_delegation_pages = tuple(sorted(delegatable))
                pages_text = ", ".join(
                    f"P{page:02d}" for page in sorted(delegatable)
                )
                return (
                    "Image 已完成，且以下未制作页面已有匹配素材或不再需要位图："
                    f"{pages_text}。不要再次委派 Image；下一步只提交一次 delegate_task "
                    "覆盖这些 Slide 责任单元。Harness 将暂时只开放委派工具。"
                )
        return json.dumps(
            {
                "status": "already_completed",
                "requested": requested_count,
                "executed": 0,
                "succeeded": requested_count,
                "failed": 0,
                "succeeded_labels": skipped_completed_labels,
                "skipped_completed_labels": skipped_completed_labels,
                "failures": [],
            },
            ensure_ascii=False,
            indent=2,
        )
    specs = pending_specs
    roles = [str(spec.get("role", "")).lower() for spec in specs]
    available_roles = set(getattr(parent, "available_roles", ROLES) or ())
    unavailable = sorted(set(roles) - available_roles)
    if unavailable:
        return (
            "委派错误：运行时能力检查已省略以下角色："
            + "、".join(unavailable)
            + "。不要按静态 Skill 的可选路线调用它们；请按注入的运行时能力合同继续。"
        )
    if roles.count("material") > 1:
        return (
            "Material 委派错误：每个 Deck 只使用一个 Material Agent。"
            "把全部附件路径合并到同一个 Material 任务，并统一写入 "
            "research/material.md。"
        )
    if "material" in roles and parent.material_completed:
        next_step = (
            "请委派 Research 读取 research/material.md；Orchestrator 等待后只读取 "
            "research/knowledge-brief.md"
            if bool(getattr(parent, "research_required", False))
            else "直接使用 research/material.md"
        )
        return (
            f"Material 已完成：{next_step}，"
            "不要重复委派或按附件拆成多个 Material Agent。"
        )
    if roles.count("research") > 1:
        return (
            "Research 委派错误：每个 Deck 只使用一个 Research Agent。"
            "把多个 topic 合并到同一个 Research 任务。"
        )
    if "research" in roles:
        # Grouped Skill makes Research a model decision. Once requested, its
        # evidence brief becomes a real dependency and cannot be silently
        # skipped after a failed child run.
        parent.research_required = True
    if "research" in roles and bool(getattr(parent, "research_blocked", False)):
        return (
            "Research 已进入 research_blocked：没有带有效成功回执的 canonical "
            "knowledge brief，且允许的一次基础设施恢复已经结束。不要重复委派；"
            "本任务将以明确阻塞状态收口。"
        )
    if "research" in roles and parent.research_completed:
        return (
            "Research 已完成：直接使用 research/knowledge-brief.md，"
            "不要重复委派。"
        )
    if roles.count("image") > 1:
        return (
            "Image 委派错误：每个 Deck 只使用一个逻辑 Image Agent。"
            "让它一次读取全部逐页计划并统一处理素材。"
        )
    inline_image_skill = _is_inline_image_skill_name(
        getattr(parent, "skill_name", "")
    )
    if inline_image_skill and "image" in roles:
        return (
            "委派错误：inline-image 版本不使用独立 Image Agent。"
            "请委派完整 SlideGroup，由各组在写 HTML 前解析并消费本组图片。"
        )
    if "image" in roles and parent.image_completed:
        image_repairs = [
            spec
            for spec in specs
            if str(spec.get("role", "")).lower() == "image" and bool(spec.get("repair"))
        ]
        review_asset_quality = any(
            str(getattr(child, "role", "") or "").lower() == "review"
            and _contract_fields(str(getattr(child, "final_text", "") or "")).get(
                "issue_type", ""
            ).lower() == "asset_quality"
            for child in (getattr(parent, "children", ()) or ())
        )
        if not image_repairs:
            missing_renders = set(tools.missing_rendered_plan_pages(parent))
            ready_pages = {
                int(page)
                for page in (
                    getattr(parent, "image_ready_pages", ()) or ()
                )
                if isinstance(page, int) and not isinstance(page, bool) and page > 0
            }
            delegatable = {
                page
                for page in missing_renders
                if (
                    page in ready_pages
                    or not _planned_needs_bitmap(
                        Path(parent.ws) / "plan" / f"slide_{page:02d}.md"
                    )
                )
                and not _page_has_terminal_slide_failure(parent, page)
            }
            if delegatable:
                parent.pending_slide_delegation_pages = tuple(sorted(delegatable))
                pages_text = ", ".join(f"P{page:02d}" for page in sorted(delegatable))
                return (
                    "Image 已完成，且以下未制作页面已有匹配素材或不再需要位图："
                    f"{pages_text}。不要再次委派 Image；下一步只提交一次 delegate_task "
                    "覆盖这些 Slide 责任单元。Harness 将暂时只开放委派工具。"
                )
            return "Image 已完成：直接使用 assets/catalog.md 与本地素材。"
        if not review_asset_quality:
            previous = existing_outcomes.get("image") or {}
            claimed, reason = _claim_image_source_route_repair(parent, previous)
            if not claimed:
                return (
                    "Image 已完成首轮有界获取；只有当前像素 Review 的 asset_quality "
                    "证据，或未启动 failed 页发生了真实 material/generated 来源改路，"
                    "才允许一次 repair:true 的 Image 续作。当前不满足来源改路条件："
                    + reason + "。"
                )
            for image_repair in image_repairs:
                image_repair["_recovery_kind"] = "source_route"
        else:
            claimed, reason = _claim_image_repair_evidence(parent)
            if not claimed:
                return "Image repair 重复委派错误：" + reason + "。"
    root = Path(parent.ws)
    grouped_skill = _uses_grouped_ownership(parent)
    research_required = bool(
        getattr(
            parent,
            "research_required",
            not _uses_legacy_grouped_contract(getattr(parent, "skill_name", "")),
        )
    )
    if "research" in roles and parent.material_required and not parent.material_completed:
        return (
            "阶段顺序错误：本任务有附件，Research 必须等待 research/material.md 完成。"
            "请先单独委派 Material，完成后再委派 Research。"
        )
    if "image" in roles:
        required_image_inputs = [
            root / "plan" / "deck.md",
            root / "speech.md",
        ]
        if research_required:
            required_image_inputs.insert(
                0, root / "research" / "knowledge-brief.md"
            )
        page_plans = sorted((root / "plan").glob("slide_[0-9][0-9].md"))
        page_fragments = sorted((root / "slides").glob("slide_[0-9][0-9].html"))
        missing_image_inputs = [
            str(path.relative_to(root))
            for path in required_image_inputs
            if not path.is_file()
        ]
        if (
            missing_image_inputs
            or not page_plans
            or len(page_plans) != len(page_fragments)
        ):
            detail = ", ".join(missing_image_inputs) or (
                f"page plans={len(page_plans)} HTML skeletons={len(page_fragments)}"
            )
            return (
                "阶段顺序错误：Image 必须等待已选择的证据阶段、完整逐页计划与轻骨架完成；"
                f"当前未就绪：{detail}。先完成规划与 scaffold，再委派 Image。"
            )
    if "slide" in roles:
        if research_required and not parent.research_completed:
            return (
                "阶段顺序错误：本任务已选择 Research，"
                "由 Research 写出 knowledge-brief 后才能委派 Slide。"
            )
        required_plan = [
            root / "plan" / "deck.md",
            root / "speech.md",
        ]
        if research_required:
            required_plan.insert(0, root / "research" / "knowledge-brief.md")
        if any(not path.is_file() for path in required_plan):
            return (
                "阶段顺序错误：Slide 必须等待已选择的证据产物、plan/deck.md、"
                "speech.md、逐页 plan 与 HTML 骨架完成。"
            )
        missing_page_plan = []
        missing_page_html = []
        mismatched_page_groups: list[tuple[int, str, str]] = []
        for spec_index, spec in enumerate(specs, start=1):
            if str(spec.get("role", "")).lower() != "slide":
                continue
            for page in _spec_slide_pages(spec, spec_index):
                plan_path = root / "plan" / f"slide_{page:02d}.md"
                if not plan_path.is_file():
                    missing_page_plan.append(page)
                elif spec.get("group_id"):
                    planned_group = _planned_production_group(plan_path)
                    expected_group = str(spec.get("group_id") or "")
                    if planned_group != expected_group:
                        mismatched_page_groups.append(
                            (page, planned_group or "missing", expected_group)
                        )
                if not (root / "slides" / f"slide_{page:02d}.html").is_file():
                    missing_page_html.append(page)
        if missing_page_plan:
            return f"阶段顺序错误：缺少逐页计划 {sorted(set(missing_page_plan))}。"
        if missing_page_html:
            return f"阶段顺序错误：缺少 HTML 骨架 {sorted(set(missing_page_html))}。"
        if mismatched_page_groups:
            detail = ", ".join(
                f"P{page:02d} plan={planned} task={expected}"
                for page, planned, expected in mismatched_page_groups
            )
            return f"SlideGroup 与逐页计划不一致：{detail}。"
        bitmap_specs = [
            spec
            for index, spec in enumerate(specs, start=1)
            if _spec_needs_bitmap(root, spec, index)
        ]
        if bitmap_specs and not parent.image_completed and "image" not in roles:
            labels = ", ".join(str(spec.get("label") or "slide") for spec in bitmap_specs)
            return (
                "阶段顺序错误：以下责任单元需要位图但 Image 尚未完成："
                f"{labels}。把唯一 Image 与全部 Slide 放在同一次 delegate_task；"
                "Harness 会立即运行 needs_bitmap:false 页面，并在 Image 完成时释放依赖页面。"
            )
        if bitmap_specs and parent.image_completed:
            ready_pages = {
                int(page)
                for page in (getattr(parent, "image_ready_pages", ()) or ())
                if isinstance(page, int) and not isinstance(page, bool) and page > 0
            }
            for spec_index, spec in enumerate(specs, start=1):
                if str(spec.get("role", "")).lower() != "slide":
                    continue
                pages = set(_spec_slide_pages(spec, spec_index))
                if not pages.intersection(ready_pages):
                    continue
                spec["task"] = (
                    str(spec.get("task") or "")
                    + "\n\n[Harness ready-asset boundary] Image 已结束，assets/catalog.md "
                    "只保留实际可用的 ready 素材。若某页原计划希望多张图片但当前只留下"
                    "一张，围绕这张图重新组织构图，并用排印、Canvas 或小型 SVG 承担"
                    "其余信息；不要反复读取 catalog、猜测缺失路径或自行重新搜索。"
                )
        failed_image_pages = set(
            int(page) for page in (getattr(parent, "image_failed_pages", ()) or ())
        )
        if parent.image_completed and failed_image_pages:
            handoff = str(getattr(parent, "image_handoff_summary", "") or "")[-1800:]
            for spec_index, spec in enumerate(specs, start=1):
                if str(spec.get("role", "")).lower() != "slide":
                    continue
                pages = set(_spec_slide_pages(spec, spec_index))
                if not pages.intersection(failed_image_pages):
                    continue
                if _spec_needs_bitmap(root, spec, spec_index):
                    affected = ", ".join(
                        f"P{page:02d}"
                        for page in sorted(pages.intersection(failed_image_pages))
                    )
                    return (
                        "Image 已明确报告素材缺口，且以下页面计划仍要求位图："
                        f"{affected}。这些 Slide 尚未启动，不能带着矛盾合同直接释放。"
                        "若 runtime bitmap_required=false 且同一事实意图可由代码视觉完整"
                        "承担，先一次性修改这些逐页计划的 needs_bitmap 与 "
                        "primary_visual_medium，重新 validate-plans，再只重提这些责任单元；"
                        "否则保留 image_blocked。"
                    )
                spec["task"] = (
                    str(spec.get("task") or "")
                    + "\n\n[Harness Image partial handoff] 本责任单元含未完全匹配的"
                    "位图素材，但逐页计划已经由 Orchestrator 显式重分类并重新校验。"
                    "按新的 ECharts/SVG/Canvas/code-visual 合同制作，不得伪称素材身份，"
                    "也不得自行再次搜索。\n"
                    + handoff
                )
    if (
        "review" in roles
        and not (root / "renders" / "contact-sheet.png").is_file()
        and not review_preflight_pages
    ):
        return "阶段顺序错误：Review 必须等待 Orchestrator 完成整册 render 与 contact-sheet。"
    if (
        "review" in roles
        and (
            _uses_legacy_grouped_contract(getattr(parent, "skill_name", ""))
            or _is_current_variant_skill_name(getattr(parent, "skill_name", ""))
        )
        and not bool(getattr(parent, "finalize_attempted", False))
    ):
        return (
            "阶段顺序错误：Grouped Review 必须等待 Orchestrator 先成功调用或明确尝试 "
            "finalize；手工逐页 render/contact-sheet 不能替代最终质量门。"
        )
    parent.delegated_roles.extend(str(spec.get("role", "")).lower() for spec in specs)
    if "image" in roles:
        parent.image_required = True
    limit = _child_wave_limit(parent, len(specs))
    parent.log(f"并行委派 {len(specs)} 项，concurrency={limit}")
    results: list[dict] = [None] * len(specs)  # type: ignore[list-item]
    with futures.ThreadPoolExecutor(max_workers=limit) as pool:
        image_index = next(
            (index for index, spec in enumerate(specs) if str(spec.get("role", "")).lower() == "image"),
            None,
        )
        blocked_indices = {
            index
            for index, spec in enumerate(specs)
            if image_index is not None and _spec_needs_bitmap(root, spec, index + 1)
        }
        jobs = {
            pool.submit(_run_child, parent, index + 1, spec): index
            for index, spec in enumerate(specs)
            if index not in blocked_indices
        }
        released = not blocked_indices
        while jobs:
            job = next(futures.as_completed(tuple(jobs)))
            index = jobs.pop(job)
            try:
                result = job.result()
            except Exception as child_exc:  # noqa: BLE001
                spec = specs[index]
                label = str(spec.get("label") or f"child_{index}")
                parent.log(
                    f"[child crash] {label}: {type(child_exc).__name__}: {child_exc}"
                )
                result = {
                    "label": label,
                    "role": str(spec.get("role", "")).lower(),
                    "ok": False,
                    "status": "child_exception",
                    "exit_reason": "child_exception",
                    "attempt": _persisted_trace_attempts(parent, label),
                    "summary": f"{type(child_exc).__name__}: {child_exc}"[-800:],
                }
            results[index] = result
            if index == image_index and not released:
                released = True
                if bool(result.get("ok")):
                    parent.image_completed = True
                    parent.image_ready_pages = tuple(result.get("image_ready_pages", ()))
                    parent.image_failed_pages = tuple(result.get("image_failed_pages", ()))
                    parent.image_handoff_summary = str(
                        result.get("image_handoff_summary")
                        or result.get("summary")
                        or ""
                    )[-1800:]
                    _persist_image_handoff(parent)
                    parent.log(
                        "Image 已完成；立即释放等待位图的 Slide，"
                        "无需等待仍在运行的 needs_bitmap:false 页面。"
                    )
                    for blocked_index in sorted(blocked_indices):
                        blocked_pages = set(
                            _spec_slide_pages(specs[blocked_index], blocked_index + 1)
                        )
                        if blocked_pages.intersection(set(parent.image_failed_pages)):
                            blocked_spec = specs[blocked_index]
                            pages = sorted(
                                blocked_pages.intersection(
                                    set(parent.image_failed_pages)
                                )
                            )
                            results[blocked_index] = {
                                "label": blocked_spec.get("label"),
                                "trace_label": None,
                                "role": "slide",
                                "ok": False,
                                "status": "asset_pending",
                                "exit_reason": "asset_pending",
                                "attempt": 1,
                                "completed_pages": [],
                                "incomplete_pages": pages,
                                "summary": (
                                    "Image 已完成有界获取，但以下页没有匹配素材："
                                    + ",".join(f"P{page:02d}" for page in pages)
                                    + "。这些 Slide 尚未启动。若 bitmap_required=false，"
                                    "Orchestrator 可一次性把对应计划显式改为可独立承担信息的 "
                                    "ECharts/SVG/Canvas/code-visual，重新 validate-plans 后仅"
                                    "重提这些责任单元；否则保留 image_blocked。Image 交接："
                                    + parent.image_handoff_summary
                                )[-1800:],
                            }
                            continue
                        jobs[pool.submit(
                            _run_child,
                            parent,
                            blocked_index + 1,
                            specs[blocked_index],
                        )] = blocked_index
                else:
                    for blocked_index in sorted(blocked_indices):
                        blocked_spec = specs[blocked_index]
                        results[blocked_index] = {
                            "label": blocked_spec.get("label"),
                            "role": "slide",
                            "ok": False,
                            "status": "asset_pending",
                            "exit_reason": "image_failed",
                            "completed_pages": [],
                            "incomplete_pages": list(
                                _spec_slide_pages(blocked_spec, blocked_index + 1)
                            ),
                            "summary": "Image 未成功交付，位图依赖页面未启动。",
                        }
    results = _auto_retry_unstarted_slide_interruptions(
        parent,
        specs,
        results,
        limit,
    )
    # A bounded Review may leave a non-blocking aesthetic issue after genuine
    # inspection.  Preserve the usable Deck as ``needs_improvement`` instead of
    # converting that quality warning into an unusable whole-run failure. Hard
    # delivery failures (blocking=yes), stale pixels, or an unfinalized edit are
    # never promoted.
    for spec, result in zip(specs, results):
        if str(spec.get("role", "")).lower() != "review":
            continue
        if _review_can_complete_needs_improvement(parent, result):
            result["ok"] = True
            result["status"] = "needs_improvement"
            result["exit_reason"] = "needs_improvement"
    outcomes = existing_outcomes
    for result in results:
        label = str(result.get("label") or "")
        if label:
            outcomes[label] = {
                "role": result.get("role"),
                "trace_label": result.get("trace_label"),
                "ok": bool(result.get("ok")),
                "exit_reason": result.get("exit_reason"),
                "completed_pages": result.get("completed_pages", []),
                "incomplete_pages": result.get("incomplete_pages", []),
                "trace_mode": dict(result.get("trace_mode") or {}),
                "status": result.get("status"),
                "repair_issue": result.get("repair_issue"),
                "blocking": result.get("blocking"),
                "issue_type": result.get("issue_type"),
                "pages": result.get("pages"),
                "evidence": result.get("evidence"),
                "input_fingerprint": result.get("input_fingerprint"),
                "attempt": result.get("attempt"),
            }
    if any(
        str(spec.get("role", "")).lower() == "review"
        and str(result.get("exit_reason") or "") == "review_incomplete_current_pixels"
        for spec, result in zip(specs, results)
    ):
        parent.review_terminal_failure = True
        parent.log(
            "[review terminal failure] closure tail exhausted with stale pixels; "
            "deterministic stop — no further tool calls permitted"
        )
    if any(
        str(spec.get("role", "")).lower() == "review" and bool(result.get("ok"))
        for spec, result in zip(specs, results)
    ):
        parent.review_completed = True
        review_result = next(
            result
            for spec, result in zip(specs, results)
            if str(spec.get("role", "")).lower() == "review" and result.get("ok")
        )
        parent.final_render_after_review = bool(
            review_result.get("final_render_after_review")
        )
        parent.final_view_after_review = bool(
            review_result.get("final_view_after_review")
        )
        parent.quality_status = str(review_result.get("status") or "ready")
        if bool(review_result.get("finalize_attempted")):
            parent.finalize_attempted = True
            parent.finalize_succeeded = bool(
                review_result.get("finalize_succeeded")
            )
            parent.finalize_failure = str(
                review_result.get("finalize_failure") or ""
            )[-1200:]
        _reconcile_review_issues_ledger(parent)
    if any(
        str(spec.get("role", "")).lower() == "material" and bool(result.get("ok"))
        for spec, result in zip(specs, results)
    ):
        parent.material_completed = True
    if any(
        str(spec.get("role", "")).lower() == "material"
        and result.get("status") == "material_blocked"
        for spec, result in zip(specs, results)
    ):
        parent.material_blocked = True
    if any(
        str(spec.get("role", "")).lower() == "research" and bool(result.get("ok"))
        for spec, result in zip(specs, results)
    ):
        parent.research_completed = True
    if any(
        str(spec.get("role", "")).lower() == "research"
        and result.get("status") == "research_blocked"
        for spec, result in zip(specs, results)
    ):
        parent.research_blocked = True
    if any(
        str(spec.get("role", "")).lower() == "image" and bool(result.get("ok"))
        for spec, result in zip(specs, results)
    ):
        parent.image_required = True
        parent.image_completed = True
        image_spec, image_result = next(
            (spec, result)
            for spec, result in zip(specs, results)
            if str(spec.get("role", "")).lower() == "image" and result.get("ok")
        )
        parent.image_ready_pages = tuple(image_result.get("image_ready_pages", ()))
        parent.image_failed_pages = tuple(image_result.get("image_failed_pages", ()))
        parent.image_handoff_summary = str(
            image_result.get("image_handoff_summary") or ""
        )[-1800:]
        _persist_image_handoff(parent)
        if bool(image_spec.get("repair")):
            # External asset repair changed the delivery surface. The prior
            # Review outcome is no longer final; require one bounded verifier
            # continuation rather than silently shipping stale pixels.
            _invalidate_review_after_image_repair(parent)
    parent.n_renders += sum(int(item.get("renders", 0)) for item in results)
    parent.n_views += sum(int(item.get("views", 0)) for item in results)
    succeeded = [
        str(item.get("label"))
        for item in results
        if item.get("ok")
    ]
    failed = [
        {
            "label": item.get("label"),
            "role": item.get("role"),
            "status": item.get("status"),
            "exit_reason": item.get("exit_reason"),
            "completed_pages": item.get("completed_pages", []),
            "incomplete_pages": item.get("incomplete_pages", []),
            "summary": str(item.get("summary", ""))[-800:],
            **(
                {
                    "next_action": (
                        "Image did not close. Do not claim bitmap_ready, do not "
                        "dispatch bitmap-dependent Slides, and do not finalize. "
                        "Only an API/timeout/empty-response interruption permits "
                        "one operational retry. A truthful ready/failed partition "
                        "must close as partial_ready; after that, only one real "
                        "pre-production material/generated source-route change or "
                        "one Review asset_quality handoff may reopen Image."
                    )
                }
                if str(item.get("role") or "").lower() == "image"
                else {}
            ),
        }
        for item in results
        if not item.get("ok")
    ]
    repair_required = [
        item.get("repair_issue")
        for item in results
        if item.get("ok") and isinstance(item.get("repair_issue"), dict)
    ]
    # Child traces contain the full per-agent responses. Returning every Slide
    # summary here can exceed the tool-result cap on long decks; the truncated
    # result then makes the Orchestrator believe later pages never returned and
    # it delegates the whole deck again. Keep the control-plane result compact.
    all_succeeded = skipped_completed_labels + succeeded
    payload_record = {
        "status": (
            "completed_with_failures"
            if failed
            else "repair_required"
            if repair_required
            else "completed"
        ),
        "requested": requested_count,
        "executed": len(results),
        "succeeded": len(all_succeeded),
        "failed": len(failed),
        "succeeded_labels": all_succeeded,
        "skipped_completed_labels": skipped_completed_labels,
        "failures": failed,
        "repair_required": repair_required,
    }
    if roles != ["slide"] * len(roles):
        payload_record["agent_summaries"] = [
            {
                "label": item.get("label"),
                "role": item.get("role"),
                "ok": item.get("ok"),
                "exit_reason": item.get("exit_reason"),
                "summary": str(item.get("summary", ""))[-1200:],
            }
            for item in results
        ]
    return json.dumps(payload_record, ensure_ascii=False, indent=2)


def _parse_slide_group_pages(value: str) -> list[int]:
    pages: list[int] = []
    for raw_part in value.split(","):
        part = raw_part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            if not start_text.strip().isdigit() or not end_text.strip().isdigit():
                raise ValueError(f"invalid page range {part!r}")
            start, end = int(start_text), int(end_text)
            if start < 1 or end < start:
                raise ValueError(f"invalid page range {part!r}")
            pages.extend(range(start, end + 1))
        elif part.isdigit() and int(part) > 0:
            pages.append(int(part))
        else:
            raise ValueError(f"invalid page number {part!r}")
    if not pages:
        raise ValueError("page list is empty")
    if len(set(pages)) != len(pages):
        duplicates = sorted(page for page in set(pages) if pages.count(page) > 1)
        if len(set(pages)) == 1:
            page = duplicates[0]
            raise ValueError(
                f"page {page:02d} is repeated; for an isolated one-page group use "
                f"[{page:02d}], never [{page:02d},{page:02d}]"
            )
        raise ValueError(
            "page list contains duplicates: "
            + ",".join(f"{page:02d}" for page in duplicates)
        )
    return pages


def _rendered_pages_from_command(command: str) -> set[int]:
    pages = {
        int(value)
        for value in re.findall(
            r"(?:deck|slide)\.py\s+render\s+\.\s+--page\s+0*(\d+)",
            str(command or ""),
        )
    }
    group_match = re.search(
        r"(?:deck|slide)\.py\s+render-group\s+\.\s+.*?--pages\s+([^\s]+)",
        str(command or ""),
    )
    if group_match:
        try:
            pages.update(_parse_slide_group_pages(group_match.group(1)))
        except ValueError:
            return set()
    return pages


def _grouped_exhausted_page_tool_error(
    agent: Agent,
    tool_name: str,
    args: dict,
) -> str:
    """Freeze only exhausted Group members while siblings remain editable.

    The three checked authoring states are page-local.  Hiding every tool when
    one Group member reaches that line strands unfinished siblings; leaving all
    tools open lets the model blindly mutate the exhausted page.  This helper
    takes the middle path: preserve that page's last verified bytes and point
    the same Agent at the next unfinished member.  It is a lifecycle guard, not
    an aesthetic acceptance gate.
    """
    if str(getattr(agent, "role", "") or "").lower() != "slide":
        return ""
    assigned = tuple(
        int(page) for page in (getattr(agent, "assigned_slide_pages", ()) or ())
    )
    if len(assigned) < 2 or not str(
        getattr(agent, "slide_group_id", "") or ""
    ).strip():
        return ""

    target_pages: set[int] = set()
    if tool_name in {"write_file", "patch"}:
        normalized = str(args.get("path") or "").replace("\\", "/").lstrip("./")
        match = re.fullmatch(r"slides/slide_(\d+)\.html", normalized)
        if match:
            target_pages.add(int(match.group(1)))
    elif tool_name == "terminal":
        command = str(args.get("command") or args.get("cmd") or "")
        # render-group is the required cached consistency pass after every
        # member has closed; it must remain available even though it mentions
        # the already-frozen pages.
        if not re.search(r"(?:deck|slide)\.py\s+render-group\s+\.", command):
            target_pages.update(_rendered_pages_from_command(command))
    if not target_pages:
        return ""

    exhausted: list[int] = []
    for page in sorted(target_pages.intersection(assigned)):
        used, limit = _page_render_budget(
            Path(agent.ws),
            page,
            str(getattr(agent, "trace_label", "") or ""),
        )
        if limit and used >= limit:
            exhausted.append(page)
    if not exhausted:
        return ""

    next_page = _slide_group_active_page(agent)
    if normalize_language(
        str(getattr(agent, "response_language", "") or "")
    ) == "en":
        continuation = (
            f" Continue with P{next_page:02d} now; its independent page budget and tools remain available."
            if next_page and next_page not in exhausted
            else " Continue with the next unfinished Group member, then run render-group."
        )
        return (
            "tool sequence reminder [group_page_frozen]: "
            + ", ".join(f"P{page:02d}" for page in exhausted)
            + " already used its three checked authoring states. Preserve its last "
            "verified HTML/PNG and leave its open issue for final Review; this call "
            "was not executed."
            + continuation
        )
    continuation = (
        f"现在继续 P{next_page:02d}；其独立页面预算和工具仍可使用。"
        if next_page and next_page not in exhausted
        else "继续下一个未完成的组成员，最后运行 render-group。"
    )
    return (
        "tool sequence 提醒[group_page_frozen]："
        + "、".join(f"P{page:02d}" for page in exhausted)
        + " 已用完三个有效像素状态。保留该页最后一次已验证的 HTML/PNG，"
        "将开放问题交给最终 Review；本次调用未执行。"
        + continuation
    )


def _delegate_task(parent: Agent, args: dict) -> str:
    """Map Hermes' singular delegation surface to presentation roles."""
    if bool(getattr(parent, "review_terminal_failure", False)):
        return (
            "delegate_task 错误[review_closure_failed]：Review closure tail 已耗尽，"
            "当前像素仍为 stale。这是确定性终止——不允许任何后续委派。"
            "请立即以 review_closure_failed 结束。"
        )
    incomplete_read = tools.pending_read_error(parent)
    if incomplete_read:
        return f"delegate_task 错误：{incomplete_read}"
    if getattr(parent, "skill_language", None) == "auto":
        return (
            "delegate_task 错误：先读取一个候选 SKILL.md 锁定说明版，"
            "再委派子角色。"
        )
    raw_tasks = args.get("tasks")
    # Some OpenAI-compatible model servers occasionally serialize a valid
    # array-valued tool argument as a JSON string. Keep the public Hermes
    # schema unchanged, but accept that transport quirk at the adapter edge so
    # one malformed envelope does not collapse a parallel wave into retries.
    if isinstance(raw_tasks, str):
        try:
            raw_tasks = json.loads(raw_tasks)
        except json.JSONDecodeError:
            raw_tasks = None
    if not isinstance(raw_tasks, list) or not raw_tasks:
        task_shape = (
            "每组使用 `{role: slide, group_id: GROUP, pages: [NN,NN]}`"
            if _uses_grouped_ownership(parent)
            else "每页使用一个 `{role: slide, pages: [NN]}`（若尚未选择，先在 plan/deck.md 写 ownership_topology）"
        )
        return (
            "delegate_task 错误：tasks 必须是非空数组；请直接传数组，不要把数组再包成"
            f"字符串。{task_shape}，也不要改由 Orchestrator 接管 Slide HTML。"
        )

    specs: list[dict] = []
    slide_numbers: list[int] = []
    slide_group_ids: list[str] = []
    ownership_topology = _ownership_topology(parent)
    grouped_skill = ownership_topology == "grouped"
    inline_image_skill = _is_inline_image_skill_name(
        getattr(parent, "skill_name", "")
    )
    delegated_language = (
        normalize_language(getattr(parent, "response_language", ""))
        or normalize_language(getattr(parent, "query_language_hint", ""))
        or normalize_language(getattr(parent, "skill_language", ""))
        or "en"
    )
    for index, task in enumerate(raw_tasks, start=1):
        if not isinstance(task, dict):
            return f"delegate_task 错误：task {index} 必须是对象"
        structured_role = str(task.get("role") or "").strip().lower()
        goal = str(task.get("goal") or "").strip()
        if structured_role:
            if structured_role not in ROLES:
                return f"delegate_task 错误：task {index} role 无效：{structured_role}"
            raw_pages = task.get("pages")
            if structured_role == "slide":
                if not isinstance(raw_pages, list) or not raw_pages:
                    return f"delegate_task 错误：task {index} 的 slide 缺少 pages"
                try:
                    pages = [int(page) for page in raw_pages]
                except (TypeError, ValueError):
                    return f"delegate_task 错误：task {index} 的 pages 必须是正整数数组"
                if any(page < 1 for page in pages) or len(set(pages)) != len(pages):
                    return f"delegate_task 错误：task {index} 的 pages 必须为不重复正整数"
                if pages != sorted(pages):
                    return f"delegate_task 错误：task {index} 的 pages 必须升序"
                group_id = str(task.get("group_id") or "").strip().lower().replace("_", "-")
                if ownership_topology == "single":
                    workspace = Path(parent.ws)
                    planned_special_group = (
                        _planned_production_group(
                            workspace / "plan" / f"slide_{pages[0]:02d}.md"
                        )
                        if len(pages) == 1
                        else ""
                    )
                    planned_special_members = _planned_special_group_members(
                        workspace, planned_special_group
                    )
                    if (
                        not group_id
                        and planned_special_group in {"bookends", "dividers"}
                        and len(planned_special_members) >= 2
                    ):
                        return (
                            f"delegate_task 错误：task {index} 的 P{pages[0]:02d} 属于计划中的 "
                            f"`{planned_special_group}` 视觉记忆组，不能拆成 Single；请一次"
                            "委派该组在计划中的全部成员。"
                        )
                    special_memory = _single_special_memory_group(
                        workspace, group_id, pages
                    )
                    if (len(pages) != 1 or group_id) and not special_memory:
                        return (
                            f"delegate_task 错误：task {index} 当前 topology=single，"
                            "普通内容页每个结构化 slide 只能含一个 page 且不设置 "
                            "group_id；只有计划中完整的 `bookends` 或 `dividers` "
                            "特殊页记忆组可使用 group_id"
                        )
                    slide_numbers.extend(pages)
                    if special_memory:
                        slide_group_ids.append(group_id)
                        specs.append({
                            "role": "slide",
                            "label": f"slide_group_{group_id}",
                            "group_id": group_id,
                            "pages": pages,
                            "task": goal or (
                                f"Complete the canonical special-page memory group {group_id}"
                            ),
                            **({"repair": True} if task.get("repair") else {}),
                        })
                    else:
                        number = pages[0]
                        specs.append({
                            "role": "slide",
                            "label": f"slide_{number:02d}",
                            "pages": [number],
                            "task": goal or f"Complete canonical Slide {number:02d}",
                            **({"repair": True} if task.get("repair") else {}),
                        })
                elif ownership_topology == "grouped":
                    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", group_id):
                        return f"delegate_task 错误：task {index} grouped slide 缺少合法 group_id"
                    planned_special = any(
                        _planned_production_group(
                            Path(parent.ws) / "plan" / f"slide_{page:02d}.md"
                        ) in {"bookends", "dividers"}
                        for page in pages
                    ) or group_id in {"bookends", "dividers"}
                    if planned_special and not _single_special_memory_group(
                        Path(parent.ws), group_id, pages
                    ):
                        return (
                            f"delegate_task 错误：task {index} 必须一次委派计划中完整的 "
                            f"`{group_id or 'special'}` 视觉记忆组，不能拆分成员。"
                        )
                    slide_numbers.extend(pages)
                    slide_group_ids.append(group_id)
                    specs.append({
                        "role": "slide",
                        "label": f"slide_group_{group_id}",
                        "group_id": group_id,
                        "pages": pages,
                        "task": goal or f"Complete canonical Slide Group {group_id}",
                        **({"repair": True} if task.get("repair") else {}),
                    })
                else:
                    return "delegate_task 错误：先在 plan/deck.md 锁定 ownership_topology"
            else:
                # Compatible providers sometimes copy the page scope mentioned
                # in an Image/Research goal into the shared ``pages`` field.
                # Ownership fields have no authority for non-Slide roles, so
                # discard them instead of spending another model turn on a
                # schema-only retry. Role permissions and handoff paths remain
                # unchanged.
                specs.append({
                    "role": structured_role,
                    "label": structured_role,
                    "task": goal or f"Complete the canonical {structured_role} handoff",
                    **({"repair": True} if task.get("repair") else {}),
                })
            continue
        # Compatibility adapter for historical snapshots that still encode the
        # role and ownership unit in a goal prefix. New v0.4 calls use fields.
        if not goal:
            return f"delegate_task 错误：task {index} 缺少 role（旧轨迹至少需要 goal）"
        group_match = re.match(
            r"^\s*slide\s*group\s+([a-z0-9][a-z0-9_-]*)\s*"
            r"\[\s*([0-9,\s-]+)\s*\]\s*:",
            goal,
            flags=re.IGNORECASE,
        )
        if group_match:
            if ownership_topology != "grouped":
                return (
                    f"delegate_task 错误：task {index} 使用了 SlideGroup。"
                    "请先在 plan/deck.md 写 `ownership_topology: grouped`；"
                    "若已选择 single，请把该组展开为独立页面任务。"
                )
            group_id = group_match.group(1).lower().replace("_", "-")
            try:
                pages = _parse_slide_group_pages(group_match.group(2))
            except ValueError as exc:
                return f"delegate_task 错误：task {index} 的 SlideGroup 页码无效：{exc}"
            slide_numbers.extend(pages)
            slide_group_ids.append(group_id)
            specs.append({
                "role": "slide",
                "label": f"slide_group_{group_id}",
                "group_id": group_id,
                "pages": pages,
                "task": goal.strip(),
                **({"repair": True} if task.get("repair") else {}),
            })
            continue
        slide_match = re.match(
            r"^\s*slide[_\s-]*0*(\d+)\s*:",
            goal,
            flags=re.IGNORECASE,
        )
        if slide_match:
            number = int(slide_match.group(1))
            if ownership_topology != "single":
                return (
                    "delegate_task 错误：请先在 plan/deck.md 写 "
                    "`ownership_topology: single` 后再使用结构化单页任务；"
                    "若已选择 grouped，请传入 group_id 和完整 pages 数组。"
                )
            slide_numbers.append(number)
            specs.append({
                "role": "slide",
                "label": f"slide_{number:02d}",
                "task": (
                    f"Slide {number:02d}: complete the assigned page from canonical plans"
                    if grouped_skill
                    else goal.strip()
                ),
                **({"repair": True} if task.get("repair") else {}),
            })
            continue
        role_match = re.match(
            r"^\s*(material|research|image|review)\s*:",
            goal,
            flags=re.IGNORECASE,
        )
        if not role_match:
            slide_shape = (
                "{role: slide, group_id: GROUP, pages: [NN,NN]}"
                if grouped_skill
                else "{role: slide, pages: [NN]}"
            )
            return (
                f"delegate_task 错误：task {index} 缺少结构化 role；页面任务格式为 {slide_shape}"
            )
        role = role_match.group(1).lower()
        task_text = goal.strip()
        if inline_image_skill and role == "image":
            return (
                "delegate_task 错误：inline-image 版本没有 Image 角色；"
                "将图片任务保留在对应 SlideGroup 中。"
            )
        if (
            _uses_legacy_grouped_contract(getattr(parent, "skill_name", ""))
            or _is_current_variant_skill_name(getattr(parent, "skill_name", ""))
        ) and role == "image":
            task_text = (
                "Image：根据 plan/deck.md 与正式逐页计划解析并交付已规划视觉素材"
                if delegated_language == "zh"
                else (
                    "Image: resolve the planned visual assets from plan/deck.md and "
                    "the canonical slide plans"
                )
            )
        elif (
            _uses_legacy_grouped_contract(getattr(parent, "skill_name", ""))
            or _is_current_variant_skill_name(getattr(parent, "skill_name", ""))
        ) and role == "review":
            task_text = (
                "Review：依据最终像素完成整册质量收口"
                if delegated_language == "zh"
                else "Review: close the completed deck from final pixels"
            )
        specs.append({
            "role": role,
            "label": role,
            "task": task_text,
            **({"repair": True} if task.get("repair") else {}),
        })
    if len(set(slide_numbers)) != len(slide_numbers):
        return "delegate_task 错误：同一批中不能重复委派同一页"
    if len(set(slide_group_ids)) != len(slide_group_ids):
        return "delegate_task 错误：同一批中不能重复使用同一个 SlideGroup ID"
    if os.environ.get("CLEAN_PLAN_ONLY_EVAL", "0") == "1":
        blocked = sorted({
            str(spec.get("role", "")).lower()
            for spec in specs
            if str(spec.get("role", "")).lower() in {"image", "slide", "review"}
        })
        if blocked:
            return (
                "plan-only evaluation：Material、Research 与规划完成后必须停止；"
                "本次禁止委派 " + ", ".join(blocked)
            )
    pending_slide_pages = {
        int(page)
        for page in (
            getattr(parent, "pending_slide_delegation_pages", ()) or ()
        )
        if isinstance(page, int) and not isinstance(page, bool) and page > 0
    }
    if pending_slide_pages:
        non_slide = sorted({
            str(spec.get("role", "") or "").lower()
            for spec in specs
            if str(spec.get("role", "") or "").lower() != "slide"
        })
        if non_slide:
            return (
                "delegate_task 错误[unstarted_slide_responsibility]：缺页责任门只接受 "
                "Slide 责任单元；不得夹带 " + ", ".join(non_slide) + "。"
            )
        covered = set(slide_numbers)
        if not pending_slide_pages.issubset(covered):
            missing = ", ".join(
                f"P{page:02d}" for page in sorted(pending_slide_pages - covered)
            )
            return (
                "delegate_task 错误[unstarted_slide_responsibility]：当前委派仍未覆盖 "
                f"{missing}。请在这一次 tasks 中提交全部缺失责任单元；不要再次委派 "
                "Image、Research 或已完成页面。"
            )
        extra = covered - pending_slide_pages
        if extra:
            return (
                "delegate_task 错误[unstarted_slide_responsibility]：当前仅允许补齐 "
                + ",".join(f"P{page:02d}" for page in sorted(pending_slide_pages))
                + "；不要夹带已完成或无关页面 "
                + ",".join(f"P{page:02d}" for page in sorted(extra))
                + "。"
            )
        result = _delegate(parent, {"tasks": specs})
        try:
            accepted = int(json.loads(result).get("executed", 0) or 0) > 0
        except (TypeError, ValueError, json.JSONDecodeError):
            accepted = False
        if accepted:
            parent.pending_slide_delegation_pages = ()
        else:
            all_terminal = all(
                _page_has_terminal_slide_failure(parent, page)
                for page in pending_slide_pages
            )
            if all_terminal:
                parent.pending_slide_delegation_pages = ()
        return result
    return _delegate(parent, {"tasks": specs})


def _compact_consumed_images(messages: list[dict]) -> None:
    """Remove image bytes after the model has consumed that tool result once.

    Keeping every prior PNG as base64 in the live request makes visual agents'
    request bodies grow by megabytes and eventually fail with HTTP 400. The
    trace already stores each image separately, and the text marker preserves
    the conversational fact that the image was inspected.
    """
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if item.get("type") != "tool_result":
                continue
            parts = item.get("content")
            if not isinstance(parts, list):
                continue
            compacted = []
            removed = 0
            for part in parts:
                if isinstance(part, dict) and part.get("type") == "image":
                    removed += 1
                    continue
                compacted.append(part)
            if removed:
                compacted.append({
                    "type": "text",
                    "text": f"[已在上一回合查看 {removed} 张图片；图像字节已从上下文释放]",
                })
                item["content"] = compacted


def _release_consumed_images_for_profile(
    agent: object,
    messages: list[dict],
) -> bool:
    """Release consumed pixels only in the delivery-oriented profile."""
    profile = getattr(agent, "profile", None) or resolve_run_profile("inference")
    if not profile.release_consumed_images:
        return False
    _compact_consumed_images(messages)
    return True


def _tool_results(agent: Agent, tool_uses, turn: int, tool_log: list[dict]) -> list[dict]:
    results: list[dict] = []
    remote_names = {"image_generate", "web_search"}
    # Preserve tool order when this turn contains reads. A read may reveal that
    # its output was cut by the transport cap; later actions in the same turn
    # must then wait for the explicit continuation instead of being prefetched.
    has_reads = any(use.name == "read_file" for use in tool_uses)
    remote_uses = (
        []
        if has_reads
        else [use for use in tool_uses if use.name in remote_names]
    )
    prefetched: dict[str, object] = {}
    if len(remote_uses) > 1:
        for use in remote_uses:
            args = use.input if isinstance(use.input, dict) else {}
            agent.log(
                f"🔧 {use.name}({json.dumps(args, ensure_ascii=False)[:180]}) "
                "[parallel batch]"
            )

        def dispatch_remote(use) -> object:
            args = use.input if isinstance(use.input, dict) else {}
            incomplete_read = tools.pending_read_error(agent)
            if incomplete_read:
                return f"{use.name} 错误：{incomplete_read}"
            try:
                return tools.dispatch(agent, use.name, args)
            except Exception as exc:  # noqa: BLE001
                return f"{use.name} 错误：{type(exc).__name__}: {exc}"

        with futures.ThreadPoolExecutor(
            max_workers=min(agent.remote_tool_concurrency, len(remote_uses))
        ) as pool:
            jobs = {
                pool.submit(dispatch_remote, use): use.id
                for use in remote_uses
            }
            for job in futures.as_completed(jobs):
                prefetched[jobs[job]] = job.result()

    for use in tool_uses:
        args = use.input if isinstance(use.input, dict) else {}
        if use.id not in prefetched:
            agent.log(
                f"🔧 {use.name}({json.dumps(args, ensure_ascii=False)[:180]})"
            )
        log_entry = {
            "turn": turn,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_s": round(time.time() - agent.started, 3),
            "tool_use_id": use.id,
            "name": use.name,
            "args": args,
        }
        tool_log.append(log_entry)
        try:
            if use.id in prefetched:
                value = prefetched[use.id]
            elif (
                tuple(
                    int(page)
                    for page in (
                        getattr(agent, "pending_slide_delegation_pages", ()) or ()
                    )
                    if isinstance(page, int)
                    and not isinstance(page, bool)
                    and page > 0
                )
                and use.name != "delegate_task"
            ):
                pending_pages = tuple(
                    int(page)
                    for page in (
                        getattr(agent, "pending_slide_delegation_pages", ()) or ()
                    )
                    if isinstance(page, int)
                    and not isinstance(page, bool)
                    and page > 0
                )
                value = (
                    f"{use.name} 错误[unstarted_slide_responsibility]：缺页责任门已激活，"
                    "当前只执行一次 delegate_task，精确覆盖 "
                    + ", ".join(f"P{page:02d}" for page in pending_pages)
                    + "；隐藏工具即使被模型输出也不会执行。"
                )
            elif (
                agent.role == "orchestrator"
                and bool(getattr(agent, "review_terminal_failure", False))
            ):
                value = (
                    "tool sequence 错误[review_closure_failed]：Review closure tail "
                    "已耗尽且像素仍为 stale；确定性终止已激活，所有后续工具不执行。"
                    "请立即结束。"
                )
            elif (
                agent.role == "orchestrator"
                and _orchestrator_delivery_close_status(agent)
            ):
                value = (
                    "tool sequence 错误：[reviewed_delivery_closed] 唯一 Review 已完成，"
                    "当前成品已 finalize 并检查最终像素；本批次后续工具未执行。"
                    "请直接交付 ready/needs_improvement 状态。"
                )
            elif (
                agent.role == "slide"
                and (pending_group_view := _slide_pending_group_view(agent))
                and pending_group_view == "__render_group_required__"
                and (
                    use.name != "terminal"
                    or not re.search(
                        r"(?:deck|slide)\.py\s+render-group\s+\.",
                        str(args.get("command") or args.get("cmd") or ""),
                    )
                )
            ):
                value = (
                    "tool sequence 错误：[stale_group_sheet] 页面像素已变化，旧组联系表"
                    "及其 Vision 缓存不能复用；当前只允许对完整责任组执行 render-group。"
                )
            elif (
                agent.role == "slide"
                and (pending_group_view := _slide_pending_group_view(agent))
                and (
                    use.name != "vision_analyze"
                    or str(args.get("image") or args.get("image_url") or "")
                    .replace("\\", "/")
                    .lstrip("./")
                    != pending_group_view
                )
            ):
                value = (
                    "tool sequence 错误：[group_view_before_mutation] 当前页面像素已逐页"
                    "检查，但组级一致性联系表尚未检查；当前只允许 vision_analyze "
                    f"打开 {pending_group_view}。"
                )
            elif (
                agent.role == "slide"
                and _slide_authoring_stop_line(agent)
                and not _slide_pending_group_view(agent)
            ):
                # Tool schemas are selected once per model call, but a model may
                # emit several tools in that call.  A Vision result earlier in
                # the same batch can close the third pixel state; reject every
                # later mutation/render from that stale batch instead of letting
                # it bypass the next-turn stop-line surface.
                value = (
                    "tool sequence 错误：[slide_authoring_budget] 当前责任单元的三次"
                    "有效像素状态已经完成；本批次后续工具未执行。请结束当前责任单元，"
                    "由最终 Review 接手已经存在的未关闭问题。"
                )
            elif (
                agent.role == "slide"
                and (
                    grouped_freeze_error := _grouped_exhausted_page_tool_error(
                        agent, use.name, args
                    )
                )
            ):
                value = grouped_freeze_error
            elif (
                agent.role == "slide"
                and _slide_pending_render_views(agent)
                and use.name != "vision_analyze"
            ):
                pending_pages = _slide_pending_render_views(agent)
                value = (
                    "tool sequence 错误：[render_before_mutation] 同一批次刚生成了新像素；"
                    "必须先在下一步用 vision_analyze 检查 "
                    + ", ".join(
                        f"renders/slide_{int(page):02d}.png"
                        for page in pending_pages
                    )
                    + "，当前 patch/write/render 未执行。"
                )
            elif (
                agent.role == "slide"
                and use.name == "vision_analyze"
                and not _slide_pending_render_views(agent)
                # A grouped Slide has already inspected each member page at
                # this point, but still owes one current contact-sheet view.
                # Do not let the generic same-pixel guard shadow that distinct
                # group-level responsibility (deck #148 reproduced this as an
                # impossible "please inspect" / "already inspected" loop).
                and not _slide_pending_group_view(agent)
            ):
                value = (
                    "vision_analyze 提醒：[current_pixels_already_inspected] 当前责任单元"
                    "的页面/组像素已经有一次独立 Critic 结论。请据此一次性修改后重渲，"
                    "或结束责任单元；不要换措辞重复检查同一像素。"
                )
            elif use.name != "read_file" and (
                incomplete_read := tools.pending_read_error(agent)
            ):
                value = f"{use.name} 错误：{incomplete_read}"
            elif use.name == "delegate_task":
                value = _delegate_task(agent, args)
            else:
                value = tools.dispatch(
                    agent,
                    use.name,
                    args,
                    internal_context={"parent_tool_use_id": use.id},
                )
        except Exception as exc:  # noqa: BLE001
            value = f"{use.name} 错误：{type(exc).__name__}: {exc}"

        command = str(args.get("command", ""))
        value_text = str(value)
        actual_vision_payload = bool(
            isinstance(value, dict)
            and ("image_b64" in value or "vision_analysis" in value)
        )
        if agent.role == "slide":
            layout_match = re.search(
                r"(?m)^layout-defects:(.+)$",
                value_text,
            )
            if layout_match:
                layout_value = layout_match.group(1).strip()
                prefix = "Deterministic layout defects unresolved: "
                if layout_value.lower() == "none":
                    if str(agent.repair_required_reason).startswith(prefix):
                        agent.repair_required_reason = ""
                else:
                    agent.repair_required_reason = prefix + layout_value[-1050:]
            elif (
                "repair_required" in value_text
                or "render stop line" in value_text.lower()
            ):
                agent.repair_required_reason = value_text[-1200:]
        local_revision_reviewer = bool(
            agent.role == "orchestrator"
            and getattr(agent, "revision_mode", False)
            and (
                _uses_legacy_grouped_contract(agent.skill_name)
                or _is_current_variant_skill_name(agent.skill_name)
            )
        )
        tool_succeeded = not any(
            marker in value_text
            for marker in ("错误：", "工具策略违规", "[exit_code=")
        )
        if use.name == "read_file" and tool_succeeded:
            requested_path = str(args.get("path") or "").replace("\\", "/").lstrip("./")
            recovery_path = str(
                getattr(agent, "_patch_recovery_path", "") or ""
            ).replace("\\", "/").lstrip("./")
            if recovery_path and requested_path == recovery_path:
                agent._patch_recovery_path = ""
                agent.log(
                    "[stale patch recovery] current file read completed; "
                    "mutation tools restored"
                )
        if use.name == "vision_analyze" and tool_succeeded and actual_vision_payload:
            source_path = str(
                value.get("path", "") if isinstance(value, dict) else ""
            ) or str(args.get("image") or args.get("image_url") or "")
            source_key = source_path.replace("\\", "/").lstrip("./")
            verdict = str(value.get("vision_verdict") or "uncertain").strip().lower()
            summary = str(value.get("vision_summary") or "").strip()
            critic_result = (
                dict(value.get("vision_result"))
                if isinstance(value.get("vision_result"), dict)
                else {}
            )
            agent.vision_critic_results[source_key] = {
                "source_sha256": str(value.get("source_sha256") or ""),
                "verdict": verdict,
                "summary": summary,
                "issues": list(critic_result.get("issues") or []),
                "backend": str(value.get("vision_backend") or ""),
            }
            _update_vision_issue_ledger(
                agent,
                source_key,
                str(value.get("source_sha256") or ""),
                critic_result,
            )
            log_entry.update({
                "vision_verdict": verdict,
                "vision_summary": summary[:1200],
                "source_sha256": str(value.get("source_sha256") or ""),
            })
            if agent.role == "slide":
                prefix = "Vision Critic unresolved: "
                if verdict in {"repair_required", "uncertain"}:
                    existing = str(agent.repair_required_reason or "").strip()
                    vision_reason = prefix + (summary or verdict)
                    agent.repair_required_reason = (
                        existing + " | " + vision_reason
                        if existing.startswith("Deterministic layout defects unresolved: ")
                        else vision_reason
                    )
                elif (
                    str(agent.repair_required_reason).startswith(prefix)
                    and re.search(r"(?:^|/)renders/slide_0*\d+\.png$", source_key)
                    and not _unresolved_vision_critic_issues(agent)
                ):
                    # A ready group montage cannot erase a finer page-local
                    # finding. Clear the transient reason only after the
                    # repaired full-resolution page itself closes every
                    # current-hash Critic obligation.
                    agent.repair_required_reason = ""
        if (
            local_revision_reviewer
            and use.name == "terminal"
            and tool_succeeded
            and re.search(r"deck\.py\s+repair-contract\b", command)
        ):
            agent.review_completed = False
            agent.final_render_after_review = False
            agent.final_view_after_review = False
            agent.finalize_succeeded = False
        elif (
            local_revision_reviewer
            and use.name == "terminal"
            and tool_succeeded
            and re.search(r"(?:deck|orchestrator|review)\.py\s+sync-speech\b", command)
        ):
            agent.review_completed = False
            agent.final_render_after_review = False
            agent.finalize_succeeded = False
        if use.name == "terminal" and re.search(
            r"(?:deck|orchestrator|review)\.py\s+finalize\b", command
        ):
            agent.finalize_attempted = True
            agent.finalize_succeeded = tool_succeeded
            agent.finalize_failure = "" if tool_succeeded else value_text[-1200:]
            if tool_succeeded and agent.role == "review":
                agent.review_patches_since_finalize = 0
            if local_revision_reviewer and tool_succeeded:
                agent.final_render_after_review = True
                agent.review_completed = bool(agent.final_view_after_review)
        if "工具策略违规" in value_text:
            log_entry["policy_violation"] = (
                agent.tool_policy_violations[-1]
                if agent.tool_policy_violations
                else {"severity": "low", "artifact_changed": False}
            )
        rendered_pages = (
            _rendered_pages_from_command(command)
            if use.name == "terminal" and tool_succeeded
            else set()
        )
        if use.name == "terminal":
            attempted_pages = _rendered_pages_from_command(command)
            if attempted_pages:
                seen_attempts = set(
                    getattr(agent, "render_attempted_pages", set()) or set()
                )
                seen_attempts.update(attempted_pages)
                agent.render_attempted_pages = seen_attempts
                # The edit-cadence guard is scoped to the interval between
                # render attempts.  A failed deterministic validation grants a
                # fresh, bounded correction batch; a successful render then
                # requires Vision inspection before any further page edit.
                mutation_counts = dict(
                    getattr(agent, "prerender_slide_mutations", {}) or {}
                )
                for page in attempted_pages:
                    mutation_counts[int(page)] = 0
                agent.prerender_slide_mutations = mutation_counts
        if rendered_pages:
            agent.n_renders += len(rendered_pages)
            if agent.role == "slide":
                for page in rendered_pages:
                    path_text = getattr(agent, "expected_output_paths", {}).get(page)
                    if path_text:
                        agent.rendered_output_hashes[page] = _file_content_digest(
                            Path(path_text)
                        )
                group_match = re.search(
                    r"(?:deck|slide)\.py\s+render-group\s+\.\s+.*?--group\s+"
                    r"([^\s]+).*?--pages\s+([^\s]+)",
                    command,
                )
                if group_match:
                    group_id = str(group_match.group(1) or "").strip().lower()
                    assigned_group = str(
                        getattr(agent, "slide_group_id", "") or ""
                    ).strip().lower()
                    if group_id == assigned_group:
                        sheet = (
                            Path(agent.ws)
                            / "renders"
                            / f"contact-sheet-group-{group_id}.png"
                        )
                        agent.group_rendered_contact_hash = _file_content_digest(sheet)
                        agent.group_rendered_page_hashes = {
                            int(page): str(
                                agent.rendered_output_hashes.get(int(page), "") or ""
                            )
                            for page in tuple(
                                getattr(agent, "assigned_slide_pages", ()) or ()
                            )
                        }
        if (
            use.name == "terminal"
            and agent.role == "review"
            and tool_succeeded
            and re.search(r"(?:deck|orchestrator|review)\.py\s+finalize\b", command)
        ):
            agent.final_render_after_review = True
        if (
            use.name == "vision_analyze"
            and agent.role == "review"
            and tool_succeeded
            and actual_vision_payload
        ):
            agent.final_view_after_review = True
            source_path = str(
                value.get("path", "") if isinstance(value, dict) else ""
            ) or str(args.get("image") or args.get("image_url") or "")
            if re.search(r"(?:^|/)renders/contact-sheet\.png$", source_path):
                agent.review_contact_sheet_inspected = True
                if (
                    bool(getattr(agent, "review_mutation_since_contact_scan", False))
                    and bool(getattr(agent, "final_render_after_review", False))
                ):
                    agent.review_revision_rounds = int(
                        getattr(agent, "review_revision_rounds", 0) or 0
                    ) + 1
                    agent.review_mutation_since_contact_scan = False
                    agent.log(
                        "[review round] coordinated repair + final contact-sheet "
                        f"inspection {agent.review_revision_rounds}/"
                        f"{config.REVIEW_MAX_ATTEMPTS}"
                    )
            page_match = re.search(
                r"(?:^|/)renders/slide_0*(\d+)\.png$",
                source_path,
            )
            if page_match:
                page = int(page_match.group(1))
                digest = _file_content_digest(
                    Path(agent.ws) / "renders" / f"slide_{page:02d}.png"
                )
                if digest:
                    agent.review_viewed_page_hashes[page] = digest
        if (
            use.name == "vision_analyze"
            and local_revision_reviewer
            and tool_succeeded
        ):
            agent.final_view_after_review = True
            if agent.finalize_succeeded:
                agent.review_completed = True
        if (
            agent.role == "review"
            and use.name in {"patch", "write_file"}
            and tool_succeeded
        ):
            agent.review_changed = True
            agent.review_mutation_since_contact_scan = True
            agent.review_patches_since_finalize = int(
                getattr(agent, "review_patches_since_finalize", 0) or 0
            ) + 1
            agent.final_render_after_review = False
            agent.final_view_after_review = False
            agent.finalize_succeeded = False
            # Any page edit makes the prior whole-deck montage stale. Keep
            # unchanged full-resolution page hashes, but require a fresh
            # contact-sheet scan after the next finalize before Review can
            # close or downgrade the current delivery surface.
            agent.review_contact_sheet_inspected = False
        if (
            local_revision_reviewer
            and use.name in {"patch", "write_file"}
            and tool_succeeded
        ):
            agent.review_completed = False
            agent.final_render_after_review = False
            agent.final_view_after_review = False
            agent.finalize_succeeded = False
        if (
            use.name == "vision_analyze"
            and tool_succeeded
            and actual_vision_payload
            and agent.role == "slide"
        ):
            source_path = str(
                value.get("path", "") if isinstance(value, dict) else ""
            ) or str(args.get("image") or args.get("image_url") or "")
            page_match = re.search(r"(?:^|/)renders/slide_0*(\d+)\.png$", source_path)
            if page_match:
                page = int(page_match.group(1))
                digest = agent.rendered_output_hashes.get(page, "")
                if digest:
                    agent.viewed_output_hashes[page] = digest
            group_match = re.search(
                r"(?:^|/)renders/contact-sheet-group-([a-z0-9-]+)\.png$",
                source_path,
                flags=re.IGNORECASE,
            )
            if group_match and group_match.group(1).lower() == str(
                getattr(agent, "slide_group_id", "")
            ).lower():
                agent.group_viewed_contact_hash = _file_content_digest(
                    Path(agent.ws) / source_path
                )
                agent.group_viewed_page_hashes = {}
                for page in tuple(getattr(agent, "assigned_slide_pages", ()) or ()):
                    digest = agent.rendered_output_hashes.get(page, "")
                    if digest:
                        agent.group_viewed_page_hashes[int(page)] = digest
        if isinstance(value, dict) and "vision_analysis" in value:
            cached_vision = bool(value.get("vision_cached"))
            if not cached_vision:
                agent.n_views += 1
            extension = (
                "jpg" if value.get("media_type") == "image/jpeg" else "png"
            )
            if not cached_vision:
                rel = f"images/view_{agent.n_views:03d}.{extension}"
                snapshot = base64.b64decode(value["image_b64"])
                (agent.trace_dir / rel).write_bytes(snapshot)
                agent.image_by_tool[use.id] = rel
                log_entry.update({
                    "snapshot": rel,
                    "snapshot_sha256": hashlib.sha256(snapshot).hexdigest(),
                    "snapshot_bytes": len(snapshot),
                    "source_path": value.get("path") or args.get("image") or args.get("image_url"),
                    "media_type": value.get("media_type", "image/jpeg"),
                    "vision_backend": value.get("vision_backend", "external"),
                })
            else:
                log_entry.update({
                    "vision_cached": True,
                    "source_path": value.get("path") or args.get("image") or args.get("image_url"),
                    "vision_backend": value.get("vision_backend", "persistent-cache"),
                })
            results.append({
                "type": "tool_result",
                "tool_use_id": use.id,
                "content": str(value["vision_analysis"])[:12000],
            })
        elif isinstance(value, dict) and "image_b64" in value:
            agent.n_views += 1
            extension = (
                "jpg" if value.get("media_type") == "image/jpeg" else "png"
            )
            rel = f"images/view_{agent.n_views:03d}.{extension}"
            snapshot = base64.b64decode(value["image_b64"])
            (agent.trace_dir / rel).write_bytes(snapshot)
            agent.image_by_tool[use.id] = rel
            log_entry.update({
                "snapshot": rel,
                "snapshot_sha256": hashlib.sha256(snapshot).hexdigest(),
                "snapshot_bytes": len(snapshot),
                "source_path": value.get("path") or args.get("image") or args.get("image_url"),
                "media_type": value.get("media_type", "image/png"),
                "vision_backend": value.get("vision_backend", "legacy_image_payload"),
            })
            results.append({
                "type": "tool_result",
                "tool_use_id": use.id,
                "content": [
                    {"type": "text", "text": value.get("summary", "")},
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": value.get("media_type", "image/png"),
                        "data": value["image_b64"],
                    }},
                ],
            })
        else:
            results.append({"type": "tool_result", "tool_use_id": use.id, "content": str(value)[:12000]})
    return results


def _research_handoff_written(
    agent: Agent,
    tool_uses: list,
    tool_results: list[dict],
) -> bool:
    """Close Research immediately after its canonical handoff is durable."""
    if agent.role != "research" or len(tool_uses) != len(tool_results):
        return False
    written = False
    for use, result in zip(tool_uses, tool_results):
        if use.name != "write_file":
            continue
        args = use.input if isinstance(use.input, dict) else {}
        normalized = str(args.get("path") or "").replace("\\", "/").lstrip("./")
        if normalized != "research/knowledge-brief.md":
            continue
        content = result.get("content") if isinstance(result, dict) else ""
        if isinstance(content, list):
            content = " ".join(
                str(item.get("text") or "") if isinstance(item, dict) else str(item)
                for item in content
            )
        if "已写入" in str(content) and "错误" not in str(content):
            written = True
    path = Path(agent.ws) / "research" / "knowledge-brief.md"
    return written and path.is_file() and path.stat().st_size > 0


def _flush(agent: Agent, messages: list[dict], tool_log: list[dict]) -> None:
    cleaned = [agent.compact_message(m) for m in messages]
    agent.trace_mode_status = write_trace(
        agent.trace_dir,
        cleaned,
        tool_log,
        agent.image_by_tool,
        agent.run_mode,
    )


def _file_content_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _vision_issue_id(source: str, issue: dict) -> str:
    identity = "|".join((
        source,
        str(issue.get("type") or "visual_defect").strip().lower(),
        re.sub(r"\s+", " ", str(issue.get("location") or "unspecified").strip().lower()),
    ))
    return "VIS-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12].upper()


def _update_vision_issue_ledger(
    agent: Agent,
    source: str,
    source_sha256: str,
    critic_result: dict,
) -> None:
    """Append/close stable visual issues using original and repaired pixel evidence."""
    normalized_source = str(source or "").replace("\\", "/")
    # The Image contact sheet is only a low-resolution triage surface.  A
    # suspicious thumbnail must trigger inspection of the corresponding
    # original asset; it must not itself become a durable defect that blocks
    # Image closure after every original has been checked.  Page contact
    # Contact sheets locate candidate assets/pages. Durable obligations are
    # opened only from current full-resolution originals, never thumbnails.
    if (
        str(getattr(agent, "role", "") or "").lower() == "image"
        and normalized_source == "assets/contact-sheet.png"
    ):
        return
    if (
        str(getattr(agent, "role", "") or "").lower() in {"review", "slide"}
        and re.fullmatch(
            r"renders/contact-sheet(?:-[a-z0-9_-]+)?\.png",
            normalized_source,
            flags=re.I,
        )
    ):
        return
    page_match = re.search(
        r"(?:^|/)renders/slide_0*(\d+)\.png$", normalized_source, flags=re.I
    )
    # Source documents, attachment page renders, catalog assets and contact
    # sheets are diagnostic surfaces owned by Material/Image.  They may guide
    # acquisition, but only current authored slide pixels are durable Review
    # obligations.  Persisting source findings as page=None made an immutable
    # paper defect look like an uncloseable final-page defect.
    if not page_match:
        return
    path = Path(agent.ws) / "_trace" / "vision-issues.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {"schema": "mural.vision-issues.v1", "issues": []}
    records = payload.get("issues") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        records = []
    page = int(page_match.group(1))
    verdict = str(critic_result.get("verdict") or "uncertain").lower()
    issues = []
    for item in critic_result.get("issues", []):
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity") or "minor").strip().lower()
        # A ready critic may still return optional aesthetic observations.
        # They remain visible in the tool result but do not become durable
        # repair obligations unless they are major/critical.  This preserves
        # needs_improvement semantics and prevents minor contact-sheet layout
        # notes from blocking final Review forever.
        if verdict in {"repair_required", "uncertain"} or severity in {
            "major", "critical", "blocker",
        }:
            issues.append(dict(item))
    if verdict == "uncertain" and not issues:
        issues = [{
            "severity": "major",
            "type": "uncertain_visual_evidence",
            "location": "whole page",
            "evidence": str(critic_result.get("summary") or "Visual evidence is uncertain."),
            "suggested_fix": "Inspect a clear full-resolution current page render.",
        }]
    scan = critic_result.get("scan") if isinstance(critic_result.get("scan"), dict) else {}
    edges = scan.get("edges") if isinstance(scan.get("edges"), dict) else {}
    scan_complete = bool(
        scan.get("visible_subjects") is not None
        and scan.get("text_regions") is not None
        and scan.get("regions") is not None
        and all(str(edges.get(edge) or "").strip() for edge in ("top", "right", "bottom", "left"))
    )
    current_ids: set[str] = set()
    now = int(time.time())
    for issue in issues:
        issue_id = _vision_issue_id(source, issue)
        current_ids.add(issue_id)
        existing = next(
            (item for item in records if isinstance(item, dict) and item.get("id") == issue_id),
            None,
        )
        record = {
            "id": issue_id,
            "status": "open",
            "page": page,
            "source": source,
            "severity": issue.get("severity", "minor"),
            "type": issue.get("type", "visual_defect"),
            "location": issue.get("location", "unspecified"),
            "evidence": issue.get("evidence", ""),
            "suggested_fix": issue.get("suggested_fix", ""),
            "opened_pixel_sha256": (
                existing.get("opened_pixel_sha256") if existing else source_sha256
            ),
            "current_pixel_sha256": source_sha256,
            "opened_by": existing.get("opened_by") if existing else str(
                getattr(agent, "trace_label", getattr(agent, "role", "vision"))
            ),
            "first_seen_at": existing.get("first_seen_at") if existing else now,
            "last_seen_at": now,
        }
        if existing:
            existing.clear()
            existing.update(record)
        else:
            records.append(record)
    if scan_complete:
        for record in records:
            if not isinstance(record, dict) or record.get("status") != "open":
                continue
            if record.get("source") != source or record.get("id") in current_ids:
                continue
            opened_hash = str(record.get("current_pixel_sha256") or "")
            if source_sha256 and source_sha256 != opened_hash:
                record.update({
                    "status": "closed",
                    "closed_pixel_sha256": source_sha256,
                    "closed_by": str(
                        getattr(agent, "trace_label", getattr(agent, "role", "vision"))
                    ),
                    "closed_at": now,
                    "closure_verdict": verdict,
                    "closure_summary": str(critic_result.get("summary") or "")[:1200],
                })
    payload = {
        "schema": "mural.vision-issues.v1",
        "updated_at": now,
        "issues": records,
    }
    _write_json(path, payload)


def _contract_fields(text: str) -> dict[str, str]:
    """Parse the compact role return contract without interpreting free prose."""
    fields: dict[str, str] = {}
    for raw_line in str(text or "").splitlines():
        match = re.match(
            r"^\s*(status|pages|issue_type|evidence|proposed_fix|blocking|"
            r"final_pixels_inspected)\s*:\s*(.*?)\s*$",
            raw_line,
            flags=re.IGNORECASE,
        )
        if match:
            fields[match.group(1).lower()] = match.group(2).strip()
    return fields


def _page_render_budget(
    root: Path,
    page: int,
    attempt_id: str = "",
) -> tuple[int, int]:
    path = root / "_trace" / "slide-render-states" / f"page_{page:02d}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return 0, 0
    hashes = payload.get("authoring_hashes") if isinstance(payload, dict) else None
    attempt_hashes = payload.get("attempt_hashes") if isinstance(payload, dict) else None
    if not isinstance(hashes, list) and isinstance(attempt_hashes, dict):
        hashes = list(dict.fromkeys(
            value
            for attempt, values in attempt_hashes.items()
            if str(attempt).startswith("slide") and isinstance(values, list)
            for value in values
            if isinstance(value, str)
        ))
    limit = payload.get("authoring_attempt_limit", 3) if isinstance(payload, dict) else 3
    return (
        len(hashes) if isinstance(hashes, list) else 0,
        int(limit) if isinstance(limit, int) and limit > 0 else 0,
    )


def _slide_page_current_verdict(agent: Agent, page: int) -> str:
    """Return the Critic verdict for the current full-resolution page bytes."""
    source = f"renders/slide_{int(page):02d}.png"
    record = dict(getattr(agent, "vision_critic_results", {}) or {}).get(source)
    if not isinstance(record, dict):
        return ""
    expected = str(record.get("source_sha256") or "")
    current = _file_content_digest(Path(agent.ws) / source)
    if not current or (expected and expected != current):
        return ""
    return str(record.get("verdict") or "").strip().lower()


def _slide_page_authoring_terminal(agent: Agent, page: int) -> bool:
    """Whether one Grouped member may yield to the next member.

    Page-local full-resolution evidence is authoritative.  A group montage is
    deliberately absent from this decision: it can judge rhythm/coherence only.
    """
    html_path = Path(agent.ws) / "slides" / f"slide_{int(page):02d}.html"
    current_html = _file_content_digest(html_path)
    rendered_html = str(
        dict(getattr(agent, "rendered_output_hashes", {}) or {}).get(int(page), "") or ""
    )
    viewed_html = str(
        dict(getattr(agent, "viewed_output_hashes", {}) or {}).get(int(page), "") or ""
    )
    if not current_html or current_html != rendered_html or viewed_html != rendered_html:
        return False
    verdict = _slide_page_current_verdict(agent, int(page))
    if verdict == "ready" and _slide_render_quality_actions(agent, int(page)) == 0:
        return True
    used, limit = _page_render_budget(
        Path(agent.ws), int(page), str(getattr(agent, "trace_label", "") or "")
    )
    return bool(limit and used >= limit and verdict in {"repair_required", "uncertain"})


def _slide_group_active_page(agent: Agent) -> int:
    """Return the one page a Grouped Slide owner may currently mutate/render."""
    if str(getattr(agent, "role", "") or "").lower() != "slide":
        return 0
    pages = tuple(int(page) for page in (getattr(agent, "assigned_slide_pages", ()) or ()))
    if len(pages) < 2 or not str(getattr(agent, "slide_group_id", "") or "").strip():
        return pages[0] if len(pages) == 1 else 0
    # Compatibility for recovered legacy receipts that predate per-page path
    # ownership. New v0.4 children always carry this map.
    if not hasattr(agent, "expected_output_paths"):
        return 0
    for page in pages:
        if not _slide_page_authoring_terminal(agent, page):
            return int(page)
    return 0


def _slide_repair_issue(agent: Agent) -> dict | None:
    """Return a structured non-fatal repair handoff for a Slide child."""
    if agent.role != "slide":
        return None
    fields = _contract_fields(agent.final_text)
    declared = fields.get("status", "").lower() == "repair_required"
    pages = tuple(getattr(agent, "assigned_slide_pages", ()) or ())
    exhausted_pages: list[int] = []
    for page in pages:
        used, limit = _page_render_budget(
            Path(agent.ws),
            page,
            str(getattr(agent, "trace_label", "") or ""),
        )
        if limit and used >= limit:
            exhausted_pages.append(page)
    runtime_reason = str(getattr(agent, "repair_required_reason", "") or "").strip()
    unresolved = _unresolved_vision_critic_issues(agent)
    if not declared and not runtime_reason and not (exhausted_pages and unresolved):
        return None
    evidence = fields.get("evidence", "").strip()
    if not evidence:
        if runtime_reason:
            evidence = runtime_reason
        elif exhausted_pages:
            evidence = (
                "per-page render-state recovery ceiling reached; final Review must "
                "open and close this page before ready"
            )
    issue_type = fields.get("issue_type", "page_authoring").strip().lower()
    if issue_type not in {
        "page_authoring", "shared_system", "render_capture", "asset_quality",
    }:
        issue_type = "page_authoring"
    return {
        "status": "repair_required",
        "pages": list(pages),
        "issue_type": issue_type,
        "evidence": evidence[:1200],
        "proposed_fix": fields.get("proposed_fix", "")[:1200],
        "render_budget_exhausted_pages": exhausted_pages,
    }


def _slide_authoring_stop_line(agent: Agent) -> bool:
    """Stop every unverified edit after the final checked pixel state."""
    if getattr(agent, "role", "") != "slide":
        return False
    pages = tuple(getattr(agent, "assigned_slide_pages", ()) or ())
    states: list[bool] = []
    for page in pages:
        used, limit = _page_render_budget(
            Path(agent.ws),
            int(page),
            str(getattr(agent, "trace_label", "") or ""),
        )
        states.append(bool(limit and used >= limit))
    if not states or _slide_pending_render_views(agent):
        return False
    # Single owns one page. Grouped owns a page set, so one page reaching its
    # soft ceiling must not stop unfinished siblings; the role card keeps that
    # page frozen while the remaining pages close.
    return states[0] if len(states) == 1 else all(states)


def _slide_pending_render_views(agent: Agent) -> tuple[int, ...]:
    """Return pages whose latest successful render hash has not been viewed."""
    if getattr(agent, "role", "") != "slide":
        return ()
    rendered = dict(getattr(agent, "rendered_output_hashes", {}) or {})
    viewed = dict(getattr(agent, "viewed_output_hashes", {}) or {})
    pending = tuple(
        int(page)
        for page in tuple(getattr(agent, "assigned_slide_pages", ()) or ())
        if str(rendered.get(int(page), "") or "")
        and str(rendered.get(int(page), "") or "")
        != str(viewed.get(int(page), "") or "")
    )
    active = _slide_group_active_page(agent)
    if len(tuple(getattr(agent, "assigned_slide_pages", ()) or ())) > 1 and active:
        return tuple(page for page in pending if page == active)
    return pending


def _slide_pending_group_view(agent: Agent) -> str:
    """Return the current group sheet when its page-set evidence is stale."""
    if getattr(agent, "role", "") != "slide":
        return ""
    pages = tuple(getattr(agent, "assigned_slide_pages", ()) or ())
    group_id = str(getattr(agent, "slide_group_id", "") or "").strip().lower()
    if (
        len(pages) < 2
        or not group_id
        or _slide_pending_render_views(agent)
        or _slide_group_active_page(agent)
    ):
        return ""
    relative = f"renders/contact-sheet-group-{group_id}.png"
    sheet = Path(agent.ws) / relative
    current_sheet_hash = _file_content_digest(sheet)
    if not current_sheet_hash:
        # Every member has reached a page-local terminal state, so the only
        # remaining Group responsibility is to create the consistency sheet.
        # Returning an empty value here used to let the generic three-state
        # stop line hide every tool when all members had exhausted their page
        # budgets, making render-group impossible.
        return "__render_group_required__"
    rendered = dict(getattr(agent, "rendered_output_hashes", {}) or {})
    if hasattr(agent, "group_rendered_page_hashes"):
        generated_sheet_hash = str(
            getattr(agent, "group_rendered_contact_hash", "") or ""
        )
        generated_pages = dict(
            getattr(agent, "group_rendered_page_hashes", {}) or {}
        )
        if generated_sheet_hash != current_sheet_hash or any(
            str(generated_pages.get(int(page), "") or "")
            != str(rendered.get(int(page), "") or "")
            for page in pages
        ):
            return "__render_group_required__"
    viewed_pages = dict(getattr(agent, "group_viewed_page_hashes", {}) or {})
    if str(getattr(agent, "group_viewed_contact_hash", "") or "") != current_sheet_hash:
        return relative
    if any(
        str(viewed_pages.get(int(page), "") or "")
        != str(rendered.get(int(page), "") or "")
        for page in pages
    ):
        return relative
    return ""


def _repair_candidate_is_routable(agent: Agent, issue: dict) -> bool:
    """Allow Review to inherit a candidate only when a baseline PNG is durable."""
    pages = [int(page) for page in issue.get("pages", []) if int(page) > 0]
    if not pages:
        return False
    root = Path(agent.ws)
    for page in pages:
        html = root / "slides" / f"slide_{page:02d}.html"
        png = root / "renders" / f"slide_{page:02d}.png"
        if not html.is_file() or html.stat().st_size < 1:
            return False
        if not png.is_file() or png.stat().st_size < 1:
            return False
    return True


def _unresolved_vision_critic_issues(agent: Agent) -> list[tuple[str, dict]]:
    """Return non-ready critic results that still match the current pixels."""
    root = Path(agent.ws)
    unresolved: list[tuple[str, dict]] = []
    for source, record in dict(
        getattr(agent, "vision_critic_results", {}) or {}
    ).items():
        normalized_source = str(source).replace("\\", "/")
        # Image may inspect immutable attachment pages to choose a crop. Those
        # observations are immediate evidence, not deliverables that Image can
        # repair. Only current catalog assets may keep the Image role open.
        if (
            str(getattr(agent, "role", "") or "").lower() == "image"
            and not normalized_source.startswith("assets/")
        ):
            continue
        # Image contact-sheet findings are candidate locators, not final
        # evidence.  Only full-resolution catalog assets may keep Image open.
        if (
            str(getattr(agent, "role", "") or "").lower() == "image"
            and normalized_source == "assets/contact-sheet.png"
        ):
            continue
        if (
            str(getattr(agent, "role", "") or "").lower() == "review"
            and re.fullmatch(
                r"renders/contact-sheet(?:-[a-z0-9_-]+)?\.png",
                str(source).replace("\\", "/"),
                flags=re.I,
            )
        ):
            # Review contact sheets locate candidate pages.  Current
            # full-resolution page verdicts and the durable issue ledger are
            # the closure evidence; a stale montage verdict must not keep the
            # whole deck open after those pages were inspected or repaired.
            continue
        if not isinstance(record, dict):
            continue
        verdict = str(record.get("verdict") or "").lower()
        if verdict not in {"repair_required", "uncertain"}:
            continue
        expected = str(record.get("source_sha256") or "")
        if expected and _file_content_digest(root / source) != expected:
            continue
        unresolved.append((source, record))
    return unresolved


def _review_self_repair_gap(agent: Agent) -> str:
    """Keep locally repairable Review findings inside the same Agent.

    While a long file has a pending continuation, ``_call`` intentionally
    exposes only ``read_file``. A model can mistake that temporary surface for
    permanent lack of edit permission and return ``needs_orchestrator`` even
    though Review owns page-level and shared-system patches. Nudge that exact
    no-edit handoff once the read is complete. After Review has made a real
    edit, bounded unresolved findings may still leave as needs_improvement.
    """
    if str(getattr(agent, "role", "") or "").lower() != "review":
        return ""
    if bool(getattr(agent, "review_changed", False)):
        return ""
    fields = _contract_fields(str(getattr(agent, "final_text", "") or ""))
    if fields.get("status", "").strip().lower() != "needs_orchestrator":
        return ""
    issue_type = fields.get("issue_type", "").strip().lower()
    if issue_type not in {"page_authoring", "shared_system"}:
        return ""
    return (
        "Review 已把问题分类为可由本角色直接修复的 "
        f"{issue_type}，但尚未成功修改任何交付文件。pending_read 续读期间只显示 "
        "read_file 是临时工具面；EOF 后 patch 与 review.py finalize 已恢复。"
        "不要把可本地修复的问题退回 Orchestrator：现在按已给出的完整缺陷计划，"
        "对全部相关页做一次协调 patch，运行一次 finalize，并只复验修改页与当前联系表。"
    )


def _review_required_view_gap(agent: Agent) -> str:
    required = tuple(getattr(agent, "required_review_pages", ()) or ())
    if agent.role != "review":
        return ""
    if not bool(getattr(agent, "review_contact_sheet_inspected", False)):
        return (
            "Review 尚未先检查当前 renders/contact-sheet.png。每轮必须先完成整册扫描、"
            "一次列出完整缺陷与修复计划，再进入单页修复。"
        )
    root = Path(agent.ws)
    viewed = dict(getattr(agent, "review_viewed_page_hashes", {}) or {})
    stale_or_missing = [
        page
        for page in required
        if viewed.get(page) != _file_content_digest(
            root / "renders" / f"slide_{page:02d}.png"
        )
    ]
    if stale_or_missing:
        pages = ", ".join(f"P{page:02d}" for page in stale_or_missing)
        return (
            "Review 未打开并核验 Slide 明确标记的当前最终像素："
            f"{pages}。这些页不能只依赖联系表；逐页查看、修复或给出明确证据后再返回 ready。"
        )
    unresolved = _unresolved_vision_critic_issues(agent)
    if unresolved:
        details = "; ".join(
            f"{source}: {str(record.get('summary') or record.get('verdict'))[:240]}"
            for source, record in unresolved[:6]
        )
        return (
            "独立 Vision Critic 对当前像素仍返回 repair_required/uncertain："
            f"{details}。Review 必须修复后重新渲染并复验，或返回 needs_orchestrator；"
            "不能以普通 ready 覆盖。"
        )
    # Review owns the delivery surface, not the uncropped source library.
    # Image-level issues remain durable provenance, but a source watermark or
    # rough edge may be legitimately excluded by the authored crop.  Review
    # therefore blocks only on defects observed in final slide pixels; every
    # bitmap page is already mandatory full-resolution evidence, so any source
    # defect that remains visible will be reopened against renders/slide_NN.png.
    open_ledger = [
        item for item in _open_vision_issues(agent)
        if isinstance(item.get("page"), int)
        or re.search(
            r"(?:^|/)renders/slide_0*\d+\.png$",
            str(item.get("source") or "").replace("\\", "/"),
        )
    ]
    if open_ledger:
        details = "; ".join(
            f"{item.get('id')} P{int(item.get('page')):02d} {item.get('type')}"
            if isinstance(item.get("page"), int)
            else f"{item.get('id')} {item.get('type')}"
            for item in open_ledger[:8]
        )
        return (
            "Vision issue ledger 仍有未关闭问题：" + details + "。"
            "普通 ready 不能覆盖旧问题；必须在变化后的当前像素上完成固定开放扫描，"
            "由 ledger 记录 closed_pixel_sha256，或返回 blocking=no 的 needs_orchestrator "
            "作为 needs_improvement 交付。"
        )
    return ""


def _image_required_fullres_gap(agent: Agent) -> str:
    """Require a current catalog receipt and targeted visual inspection.

    The contact sheet locates anomalies.  Full-resolution inspection is
    required only for assets the critic or Image Agent marks suspicious, not
    mechanically for every catalog row.
    """
    if getattr(agent, "role", "") != "image":
        return ""
    root = Path(agent.ws)
    catalog = root / "assets" / "catalog.md"
    if not catalog.is_file():
        return "Image 尚未写入唯一 assets/catalog.md。"
    try:
        text = catalog.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "Image 无法读取 assets/catalog.md。"
    planned_bitmap_pages = {
        int(match.group(1))
        for plan_path in sorted((root / "plan").glob("slide_[0-9][0-9].md"))
        if (match := re.search(r"slide_(\d+)", plan_path.stem))
        and _planned_needs_bitmap(plan_path)
    }
    final_text = str(getattr(agent, "final_text", "") or "")
    declared_ready = _image_handoff_declared_pages(final_text, "bitmap_ready")
    declared_failed = _image_handoff_declared_pages(final_text, "failed")
    catalog_failed_pages: set[int] = set()
    catalog_ready_paths: list[str] = []
    for block in re.split(r"(?m)^##\s+", text)[1:]:
        status_match = re.search(
            r"(?mi)^\s*-\s*status\s*:\s*(ready|failed)\s*$", block
        )
        path_match = re.search(
            r"(?mi)^\s*-\s*path\s*:\s*(assets/[^\s#]+)\s*$", block
        )
        slides_match = re.search(
            r"(?mi)^\s*-\s*slides?\s*:\s*([^\n#]+)", block
        )
        pages = {
            int(value)
            for value in re.findall(
                r"\b\d{1,3}\b", slides_match.group(1) if slides_match else ""
            )
        }
        if status_match and status_match.group(1).lower() == "failed":
            catalog_failed_pages.update(pages)
        elif path_match:
            catalog_ready_paths.append(path_match.group(1))

    # A truthful zero-asset result is a complete bounded Image handoff, not a
    # request to inspect a contact sheet that cannot exist.  Preserve every
    # failed page for Orchestrator route correction or explicit image_blocked
    # delivery; do not turn this into incomplete_closure and a retry loop.
    if (
        planned_bitmap_pages
        and not declared_ready
        and declared_failed == planned_bitmap_pages
        and catalog_failed_pages.issuperset(planned_bitmap_pages)
        and not catalog_ready_paths
    ):
        agent.image_ready_pages = ()
        agent.image_failed_pages = tuple(sorted(declared_failed))
        agent.image_handoff_summary = final_text[-1800:]
        agent.log(
            "[image all-failed handoff] no contact sheet can be generated; "
            f"failed={sorted(declared_failed)}"
        )
        return ""
    # Failed catalog entries are a truthful bounded handoff, not current
    # deliverables.  Do not reopen visual inspection merely because an old
    # rejected file still exists at their former path.
    paths = sorted(set(catalog_ready_paths))
    results = dict(getattr(agent, "vision_critic_results", {}) or {})
    contact_sheet = "assets/contact-sheet.png"
    contact_record = results.get(contact_sheet)
    stale: list[str] = []
    for source in paths:
        record = results.get(source)
        if not isinstance(record, dict):
            continue
        expected = str(record.get("source_sha256") or "")
        if expected and expected != _file_content_digest(root / source):
            stale.append(source)
    if not isinstance(contact_record, dict) or stale:
        details = []
        if not isinstance(contact_record, dict):
            details.append("尚未检查 assets/contact-sheet.png")
        if stale:
            details.append("检查后文件已变化：" + ", ".join(stale))
        return (
            "Image 视觉验收未闭合："
            + "；".join(details)
            + "。先用联系表定位异常；只对身份关键、低清、水印/Logo、假字、畸形或裁切"
            "可疑项打开原图，并只替换有明确问题的素材。"
        )
    if _is_current_variant_skill_name(getattr(agent, "skill_name", "")):
        finalized_catalog_digest = str(
            getattr(agent, "image_finalized_catalog_digest", "") or ""
        )
        current_catalog_digest = _file_content_digest(catalog)
        if (
            not finalized_catalog_digest
            or finalized_catalog_digest != current_catalog_digest
        ):
            return (
                "Image catalog 尚未在当前内容上完成最终确认：每次修改来源、下载地址、"
                "裁切或状态后，都必须重新运行一次 `image.py finalize .`；只有其成功后"
                "且 catalog 未再变化，才能声明 bitmap_ready/failed 或触发保守交接。"
            )
    receipt_ready = set(getattr(agent, "image_ready_pages", ()) or ()) or (
        _image_handoff_declared_pages(final_text, "bitmap_ready")
    )
    receipt_failed = set(getattr(agent, "image_failed_pages", ()) or ()) or (
        _image_handoff_declared_pages(final_text, "failed")
    )
    if (
        planned_bitmap_pages
        and (
            not receipt_ready.isdisjoint(receipt_failed)
            or receipt_ready | receipt_failed != planned_bitmap_pages
        )
    ):
        return (
            "Image finalize receipt 未覆盖全部 needs_bitmap 页面。重新运行一次 "
            "`image.py finalize .`，并保留其 bitmap_ready/failed 分区；不要靠后续长篇"
            "文字重新声明。"
        )
    unresolved = _unresolved_vision_critic_issues(agent)
    if unresolved:
        # A bounded Image pass may legitimately end with unavailable or
        # unverifiable assets.  If every current original was inspected and
        # the agent explicitly partitions every planned bitmap page into
        # ready/failed, accept that truthful partial handoff.  Do not require
        # a *successful* ``fetch --replace`` command here: the replacement
        # route itself may be unavailable (empty search, provider failure, or
        # no faithful candidate), and making success a prerequisite turns a
        # valid bounded failure into an Image retry loop.
        ready_pages = _image_handoff_declared_pages(final_text, "bitmap_ready")
        failed_pages = _image_handoff_declared_pages(final_text, "failed")
        asset_pages: dict[str, set[int]] = {}
        for block in re.split(r"(?m)^##\s+", text)[1:]:
            path_match = re.search(
                r"(?mi)^\s*-\s*path\s*:\s*(assets/[^\s#]+)\s*$", block
            )
            slides_match = re.search(
                r"(?mi)^\s*-\s*slides?\s*:\s*([^\n#]+)", block
            )
            if path_match:
                asset_pages[path_match.group(1)] = {
                    int(value)
                    for value in re.findall(
                        r"\b\d{1,3}\b", slides_match.group(1) if slides_match else ""
                    )
                }
        unresolved_pages = {
            page
            for source, _record in unresolved
            for page in asset_pages.get(source, set())
        }
        declared_pages = ready_pages | failed_pages
        if (
            unresolved_pages
            and planned_bitmap_pages
            and declared_pages == planned_bitmap_pages
            and ready_pages.isdisjoint(failed_pages)
            and unresolved_pages.issubset(failed_pages)
        ):
            agent.image_ready_pages = tuple(sorted(ready_pages))
            agent.image_failed_pages = tuple(sorted(failed_pages))
            agent.image_handoff_summary = final_text[-1800:]
            agent.log(
                "[image partial handoff] bounded asset pass complete; "
                f"ready={sorted(ready_pages)} failed={sorted(failed_pages)}"
            )
            return ""
        details = "; ".join(
            f"{source}: {str(record.get('summary') or record.get('verdict'))[:200]}"
            for source, record in unresolved[:8]
        )
        return (
            "Image 原图仍有 repair_required/uncertain：" + details + "。"
            "按素材预算做一次定向替换并复验；仍不可得时，必须把每个计划中 "
            "needs_bitmap:true 页面恰好声明在 bitmap_ready 或 failed 之一，不得遗漏、"
            "重复或声称未验证页面 bitmap_ready。"
        )
    return ""


def _image_catalog_has_current_fullres_coverage(agent: Agent) -> bool:
    """Return whether a current deterministic Image delivery receipt exists.

    The contact sheet is the normal anomaly locator; only suspicious assets
    need an additional full-resolution Vision call.  Requiring one Vision call
    per catalog row makes image-rich decks linearly slower and discards a valid
    ``image.py finalize`` receipt when an optional replacement later stalls.
    Keep the historical helper name for compatibility while checking the
    actual durable boundary: current catalog digest, present files, viewed
    contact sheet, and a complete ready/failed page partition.
    """
    root = Path(agent.ws)
    try:
        catalog_text = (root / "assets" / "catalog.md").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return False
    if _is_current_variant_skill_name(getattr(agent, "skill_name", "")):
        if str(getattr(agent, "image_finalized_catalog_digest", "") or "") != (
            _file_content_digest(root / "assets" / "catalog.md")
        ):
            return False
    sources = re.findall(
        r"(?mi)^\s*-\s*path\s*:\s*(assets/[^\s#]+)\s*$",
        catalog_text,
    )
    if not sources:
        return False
    if any(not _file_content_digest(root / source) for source in sources):
        return False
    results = dict(getattr(agent, "vision_critic_results", {}) or {})
    contact_sheet = root / "assets" / "contact-sheet.png"
    contact_record = results.get("assets/contact-sheet.png")
    if not contact_sheet.is_file() or not isinstance(contact_record, dict):
        return False
    planned_pages = {
        int(match.group(1))
        for plan_path in sorted((root / "plan").glob("slide_[0-9][0-9].md"))
        if (match := re.search(r"slide_(\d+)", plan_path.stem))
        and _planned_needs_bitmap(plan_path)
    }
    ready_pages = set(getattr(agent, "image_ready_pages", ()) or ())
    failed_pages = set(getattr(agent, "image_failed_pages", ()) or ())
    return bool(
        planned_pages
        and ready_pages.isdisjoint(failed_pages)
        and ready_pages | failed_pages == planned_pages
    )


def _recover_image_operational_handoff(agent: Agent, trace_attempt: int) -> bool:
    """Recover a conservative Image handoff after a bounded failure.

    Image generation and inspection may durably finish before a provider
    timeout prevents the final text response.  Requiring another child after
    the one permitted operational retry deadlocks bitmap-dependent Slides.
    Recover only pages backed by a current, full-resolution ``ready`` verdict;
    every other planned bitmap page remains an explicit failed/fallback page.

    The same conservative recovery is valid after ``incomplete_closure`` when
    every current catalog original has already been inspected.  This handles a
    model that keeps claiming a known-bad asset is ready: the Harness derives
    the partition from pixel evidence instead of failing the whole Deck or
    creating another Image Agent.
    """
    if str(getattr(agent, "role", "") or "").lower() != "image":
        return False
    reason = str(getattr(agent, "exit_reason", "") or "")
    operational_recovery = (
        reason in _OPERATIONAL_CHILD_RETRY_REASONS and int(trace_attempt) >= 2
    )
    bounded_closure_recovery = (
        reason in {"incomplete_closure", "stalled_repetition", "max_turns"}
        and _image_catalog_has_current_fullres_coverage(agent)
    )
    if not (operational_recovery or bounded_closure_recovery):
        return False
    root = Path(agent.ws)
    planned_pages = {
        int(match.group(1))
        for plan_path in sorted((root / "plan").glob("slide_[0-9][0-9].md"))
        if (match := re.search(r"slide_(\d+)", plan_path.stem))
        and _planned_needs_bitmap(plan_path)
    }
    if not planned_pages:
        return False

    receipt_ready = set(getattr(agent, "image_ready_pages", ()) or ())
    receipt_failed = set(getattr(agent, "image_failed_pages", ()) or ())
    if (
        receipt_ready.isdisjoint(receipt_failed)
        and receipt_ready | receipt_failed == planned_pages
        and str(getattr(agent, "image_finalized_catalog_digest", "") or "")
        == _file_content_digest(root / "assets" / "catalog.md")
    ):
        ready_text = ", ".join(
            f"{page:02d}" for page in sorted(receipt_ready)
        ) or "none"
        failed_text = ", ".join(
            f"{page:02d}" for page in sorted(receipt_failed)
        ) or "none"
        prior = str(getattr(agent, "image_handoff_summary", "") or "")[-1000:]
        agent.image_handoff_summary = (
            "Harness preserved the current deterministic Image finalize receipt.\n"
            f"bitmap_ready: {ready_text}\n"
            f"failed: {failed_text}\n"
            + (f"finalize_receipt: {prior}" if prior else "")
        )
        return True

    try:
        catalog_text = (root / "assets" / "catalog.md").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        catalog_text = ""
    results = dict(getattr(agent, "vision_critic_results", {}) or {})
    page_asset_states: dict[int, list[bool]] = {
        page: [] for page in planned_pages
    }
    verified_assets: list[str] = []
    for block in re.split(r"(?m)^##\s+", catalog_text)[1:]:
        path_match = re.search(
            r"(?mi)^\s*-\s*path\s*:\s*(assets/[^\s#]+)\s*$", block
        )
        slides_match = re.search(
            r"(?mi)^\s*-\s*slides?\s*:\s*([^\n#]+)", block
        )
        if not path_match or not slides_match:
            continue
        source = path_match.group(1)
        digest = _file_content_digest(root / source)
        record = results.get(source)
        current_ready = bool(
            digest
            and isinstance(record, dict)
            and str(record.get("source_sha256") or "") == digest
            and str(record.get("verdict") or "").lower() == "ready"
        )
        asset_pages = {
            int(value)
            for value in re.findall(r"\b\d{1,3}\b", slides_match.group(1))
        }
        for page in asset_pages & planned_pages:
            page_asset_states.setdefault(page, []).append(current_ready)
        if current_ready:
            verified_assets.append(source)
    # A page is ready only when it has at least one assigned asset and every
    # assigned asset is current-hash ready.  One good image must not mask a
    # second uncertain/repair-required image on the same page.
    ready_pages = {
        page
        for page, states in page_asset_states.items()
        if states and all(states)
    }
    failed_pages = planned_pages - ready_pages
    agent.image_ready_pages = tuple(sorted(ready_pages))
    agent.image_failed_pages = tuple(sorted(failed_pages))
    ready_text = ", ".join(f"{page:02d}" for page in sorted(ready_pages)) or "none"
    failed_text = ", ".join(f"{page:02d}" for page in sorted(failed_pages)) or "none"
    evidence_text = ", ".join(verified_assets) or "none"
    agent.image_handoff_summary = (
        "Harness durable recovery after the bounded operational retry.\n"
        f"bitmap_ready: {ready_text}\n"
        f"failed: {failed_text}\n"
        f"current_fullres_verified_assets: {evidence_text}\n"
        "未列为 bitmap_ready 的页面不得假定素材已完成；读取现有 catalog，"
        "仅在像素与文案能真实表达页面意图时使用，否则采用计划允许的代码视觉并"
        "把媒介缺口交给 Review。"
    )
    return True


def _slide_has_current_render(
    agent: Agent,
    page: int | None = None,
    path_text: str = "",
) -> bool:
    """Return whether the current fragment/CSS pair has a published PNG.

    A re-dispatched Slide agent may inherit a valid render produced by an
    earlier agent.  Requiring a new render command in that child invocation
    creates a false failure and can force the page past its render-state stop
    line.  The Skill renderer records the exact CSS+fragment digest, so use
    that durable evidence instead of the child-local counter.
    """
    if not path_text:
        path_text = str(getattr(agent, "expected_output_path", "") or "")
    if not path_text:
        return False
    fragment_path = Path(path_text)
    if not fragment_path.is_file():
        return False
    if page is None:
        match = re.fullmatch(r"slide_(\d+)\.html", fragment_path.name)
        if not match:
            return False
        page = int(match.group(1))
    root = Path(agent.ws)
    css_path = root / "base.css"
    png_path = root / "renders" / f"slide_{page:02d}.png"
    grouped = _uses_grouped_ownership(agent)
    legacy_grouped_state = grouped and not _is_current_variant_skill_name(
        getattr(agent, "skill_name", "")
    )
    state_path = (
        root / "_trace" / "render-state.json"
        if legacy_grouped_state
        else root / "_trace" / "slide-render-states" / f"page_{page:02d}.json"
    )
    if (
        not css_path.is_file()
        or not png_path.is_file()
        or png_path.stat().st_size < 1
        or not state_path.is_file()
    ):
        return False
    try:
        css = css_path.read_text(encoding="utf-8")
        fragment = fragment_path.read_text(encoding="utf-8").strip()
        digest = _render_input_digest(Path(agent.ws), css, fragment)
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        if legacy_grouped_state:
            pages = raw.get("pages") if isinstance(raw, dict) else None
            payload = pages.get(f"{page:02d}") if isinstance(pages, dict) else None
        else:
            payload = raw
        hashes = payload.get("hashes") if isinstance(payload, dict) else None
        published_hash = (
            payload.get("published_hash") if isinstance(payload, dict) else None
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(hashes, list):
        return False
    if not isinstance(published_hash, str) or not published_hash:
        published_hash = hashes[-1] if hashes else ""
    return digest == published_hash


def _render_input_digest(root: Path, css: str, fragment: str) -> str:
    """Mirror the frozen renderer fingerprint, including local dependencies."""
    digest = hashlib.sha256()
    digest.update(css.encode("utf-8"))
    digest.update(b"\0")
    digest.update(fragment.encode("utf-8"))
    refs: set[str] = set()
    for match in re.finditer(
        r"(?:\bsrc|\bhref)\s*=\s*[\"']([^\"']+)[\"']|"
        r"url\(\s*[\"']?([^\"')]+)",
        css + "\n" + fragment,
        flags=re.I,
    ):
        value = str(match.group(1) or match.group(2) or "").strip()
        value = value.split("#", 1)[0].split("?", 1)[0].lstrip("./")
        if value.startswith(("assets/", "fonts/")):
            refs.add(value)
    for value in sorted(refs):
        digest.update(b"\0dep:")
        digest.update(value.encode("utf-8"))
        try:
            digest.update((root / value).read_bytes())
        except OSError:
            digest.update(b"<missing>")
    return digest.hexdigest()


def _slide_render_quality_actions(agent: Agent, page: int) -> int:
    """Return ACTIONs still owned by Slide before its pixel-state stop line."""
    grouped = _uses_grouped_ownership(agent)
    legacy_grouped_state = grouped and not _is_current_variant_skill_name(
        getattr(agent, "skill_name", "")
    )
    path = (
        Path(agent.ws) / "_trace" / "render-state.json"
        if legacy_grouped_state
        else Path(agent.ws) / "_trace" / "slide-render-states" / f"page_{page:02d}.json"
    )
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
        if legacy_grouped_state:
            pages = registry.get("pages") if isinstance(registry, dict) else None
            payload = pages.get(f"{page:02d}") if isinstance(pages, dict) else None
        else:
            payload = registry if isinstance(registry, dict) else None
        value = payload.get("quality_action_count", 0) if isinstance(payload, dict) else 0
        if not value and isinstance(payload, dict):
            defects = payload.get("layout_defects")
            value = len(defects) if isinstance(defects, list) else 0
        hashes = payload.get("authoring_hashes") if isinstance(payload, dict) else None
        limit = payload.get("limit", 7) if isinstance(payload, dict) else 7
        if not legacy_grouped_state and isinstance(payload, dict):
            attempt_hashes = payload.get("attempt_hashes")
            if not isinstance(hashes, list) and isinstance(attempt_hashes, dict):
                hashes = list(dict.fromkeys(
                    value
                    for attempt, values in attempt_hashes.items()
                    if str(attempt).startswith("slide") and isinstance(values, list)
                    for value in values
                    if isinstance(value, str)
                ))
            limit = payload.get("authoring_attempt_limit", 3)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return 0
    if isinstance(hashes, list) and isinstance(limit, int) and len(hashes) >= limit:
        # The role card routes this exceptional state to Review. Finalize still
        # blocks while quality_action_count is non-zero, so it cannot ship.
        return 0
    return value if isinstance(value, int) and value > 0 else 0


def _slide_deliverable_gap(agent: Agent) -> str:
    if agent.role != "slide":
        return ""
    pages = tuple(getattr(agent, "assigned_slide_pages", ()) or ())
    paths = dict(getattr(agent, "expected_output_paths", {}) or {})
    if not pages:
        path_text = str(getattr(agent, "expected_output_path", "") or "")
        match = re.search(r"slide_(\d+)\.html$", path_text)
        if match:
            pages = (int(match.group(1)),)
            paths[pages[0]] = path_text
    if not pages:
        agent.completed_slide_pages = ()
        agent.incomplete_slide_pages = ()
        return "Slide 没有绑定目标 HTML。"

    completed: list[int] = []
    gaps: dict[int, list[str]] = {}
    for page in pages:
        path_text = str(paths.get(page, "") or "")
        current = _file_content_digest(Path(path_text)) if path_text else ""
        durable_render = _slide_has_current_render(agent, page, path_text)
        rendered = str(agent.rendered_output_hashes.get(page, "") or "")
        viewed = str(agent.viewed_output_hashes.get(page, "") or "")
        legacy_single_render = len(pages) == 1 and agent.n_renders >= 1
        page_gaps: list[str] = []
        if not current:
            page_gaps.append("HTML 不存在")
        initial_hashes = dict(
            getattr(agent, "expected_output_initial_hashes", {}) or {}
        )
        initial = str(initial_hashes.get(page, "") or "")
        if not initial and len(pages) == 1:
            initial = str(getattr(agent, "expected_output_initial_hash", "") or "")
        if current and initial and current == initial and not durable_render:
            page_gaps.append("HTML 仍与初始骨架完全相同")
        if not rendered and not durable_render and not legacy_single_render:
            page_gaps.append("未完成 render")
        elif rendered and current != rendered:
            page_gaps.append("HTML 在最后一次 render 后又被修改")
        action_count = _slide_render_quality_actions(agent, page)
        if durable_render and action_count:
            page_gaps.append(
                f"当前像素仍有 {action_count} 个确定性 ACTION，尚未达到质量门"
            )
        # Durable render evidence may come from an earlier child, but this child
        # must still inspect the current pixels. A group sheet can close every
        # assigned page after render-group has registered their hashes.
        legacy_single_view = (
            len(pages) == 1
            and agent.n_views >= 1
            and (durable_render or legacy_single_render)
        )
        if (not viewed or (rendered and viewed != rendered)) and not legacy_single_view:
            page_gaps.append("未检查当前 render 的最终 PNG 像素")
        if page_gaps:
            gaps[page] = page_gaps
        else:
            completed.append(page)

    group_gap = ""
    if len(pages) > 1:
        group_id = str(getattr(agent, "slide_group_id", "") or "").strip().lower()
        sheet = Path(agent.ws) / "renders" / f"contact-sheet-group-{group_id}.png"
        current_sheet_hash = _file_content_digest(sheet)
        viewed_sheet_hash = str(
            getattr(agent, "group_viewed_contact_hash", "") or ""
        )
        viewed_page_hashes = dict(
            getattr(agent, "group_viewed_page_hashes", {}) or {}
        )
        current_page_hashes = {
            int(page): str(agent.rendered_output_hashes.get(page, "") or "")
            for page in pages
        }
        stale_pages = [
            int(page) for page in pages
            if not current_page_hashes[int(page)]
            or viewed_page_hashes.get(int(page)) != current_page_hashes[int(page)]
        ]
        generated_sheet_hash = str(
            getattr(agent, "group_rendered_contact_hash", "") or ""
        )
        generated_page_hashes = dict(
            getattr(agent, "group_rendered_page_hashes", {}) or {}
        )
        has_provenance = hasattr(agent, "group_rendered_page_hashes")
        provenance_stale = has_provenance and (
            not generated_sheet_hash
            or generated_sheet_hash != current_sheet_hash
            or any(
                generated_page_hashes.get(int(page))
                != current_page_hashes[int(page)]
                for page in pages
            )
        )
        if provenance_stale:
            group_gap = "当前页面像素尚未重新生成组联系表"
        elif not current_sheet_hash or viewed_sheet_hash != current_sheet_hash:
            group_gap = "未检查当前组联系表"
        elif stale_pages:
            group_gap = (
                "组联系表检查早于页面最终像素："
                + ", ".join(f"P{page:02d}" for page in stale_pages)
            )

    incomplete = set(gaps)
    if group_gap:
        incomplete.update(int(page) for page in pages)
    agent.completed_slide_pages = tuple(completed if not group_gap else ())
    agent.incomplete_slide_pages = tuple(sorted(incomplete))
    if not gaps and not group_gap:
        return ""
    detail = "；".join(
        f"P{page:02d}：{'、'.join(items)}"
        for page, items in sorted(gaps.items())
    )
    if group_gap:
        detail = (detail + "；" if detail else "") + "Group：" + group_gap
    return (
        detail
        + "。请只补齐这些页面，并完成当前 HTML 对应的 render → vision 闭环；"
        "缓存 render 也有效，不要为增加计数改动页面。"
    )


def _finish_gap(agent: Agent) -> str:
    """Keep the last execution role on the Orchestrator without scoring aesthetics."""
    incomplete_read = tools.pending_read_error(agent)
    if incomplete_read:
        return incomplete_read
    if agent.role == "slide":
        gap = _slide_deliverable_gap(agent)
        issue = _slide_repair_issue(agent)
        if issue and _repair_candidate_is_routable(agent, issue):
            pages = tuple(int(page) for page in issue.get("pages", []))
            agent.completed_slide_pages = pages
            agent.incomplete_slide_pages = ()
            return ""
        return gap
    if agent.role == "material":
        return _material_handoff_gap(agent)
    if agent.role == "review":
        self_repair_gap = _review_self_repair_gap(agent)
        if self_repair_gap:
            return self_repair_gap
        if int(getattr(agent, "review_revision_rounds", 0) or 0) >= int(
            config.REVIEW_MAX_ATTEMPTS
        ):
            fields = _contract_fields(str(getattr(agent, "final_text", "") or ""))
            bounded_handoff = bool(
                fields.get("status", "").strip().lower() == "needs_orchestrator"
                and fields.get("blocking", "no").strip().lower() != "yes"
                and fields.get("final_pixels_inspected", "").strip().lower() == "yes"
                and _review_has_current_inspected_delivery(agent)
            )
            if bounded_handoff:
                return ""
        return _review_required_view_gap(agent)
    if agent.role == "image":
        return _image_required_fullres_gap(agent)
    if agent.role != "orchestrator":
        return ""
    if agent.skill_language == "auto":
        return (
            "尚未选择说明版 Skill。请根据你最能可靠遵循的说明语言，"
            f"读取 `skills/{config.SKILL_NAME_ZH}/SKILL.md` 或 "
            f"`skills/{config.SKILL_NAME_EN}/SKILL.md` 之一；首次读取即锁定。"
        )
    if bool(getattr(agent, "material_blocked", False)):
        return ""
    if bool(getattr(agent, "research_blocked", False)):
        return ""
    if agent.material_required and not agent.material_completed:
        return (
            "本任务有附件，但 Material 尚未完成。请先委派 Material 整理附件，"
            "不要由编排器直接读取附件代替材料阶段。"
        )
    if agent.research_required and not agent.research_completed:
        material_stage = str(
            (getattr(agent, "runtime_capabilities", {}).get("workflow") or {}).get(
                "material_stage", "omitted"
            )
        )
        material_clause = (
            "与 research/material.md "
            if material_stage in {"direct_text", "agent_required", "mixed"}
            else "（本次 material_stage=omitted，不要探测 material.md）"
        )
        return (
            "Research 尚未完成。请把完整 query " + material_clause + "交给 Research，"
            "由 Research 一次写出 research/knowledge-brief.md 后再继续规划。"
        )
    if os.environ.get("CLEAN_PLAN_ONLY_EVAL", "0") == "1":
        root = Path(agent.ws)
        plans = sorted((root / "plan").glob("slide_[0-9][0-9].md"))
        if not (root / "plan" / "deck.md").is_file() or len(plans) < 2:
            return (
                "plan-only evaluation 尚未完成：请写完 plan/deck.md 与全部逐页计划，"
                "运行 validate-plans 后直接结束；不要进入 Image、Slide 或 Review。"
            )
        return ""
    revision_mode = bool(getattr(agent, "revision_mode", False))
    revision_route = str(getattr(agent, "revision_route", "") or "")
    if revision_mode and not revision_route:
        return (
            "尚未锁定编辑路由。先只读建立影响范围：简单编辑只委派唯一 "
            "`Review: mode=simple_edit`；复杂编辑先写 `plan/revision-impact.md`，"
            "再委派实际受影响角色。"
        )
    page_plans = list((Path(agent.ws) / "plan").glob("slide_[0-9][0-9].md"))
    if (
        not revision_mode
        and len(page_plans) >= 2
        and "slide" not in agent.delegated_roles
    ):
        return (
            f"当前已有 {len(page_plans)} 个逐页计划，但尚未委派 Slide。"
            "上下文长度、成本或预算不是跳过并行页面阶段的理由；"
            "请委派全部页面，等待完成后再 finalize。"
        )
    if "slide" not in agent.delegated_roles and not revision_mode:
        return ""
    planned_pages = sorted(
        int(path.stem.rsplit("_", 1)[-1])
        for path in page_plans
    )
    outcomes = getattr(agent, "child_outcomes", {})
    grouped_skill = _uses_grouped_ownership(agent)
    if grouped_skill and revision_mode:
        incomplete_pages = sorted({
            int(page)
            for outcome in outcomes.values()
            if isinstance(outcome, dict)
            for page in outcome.get("incomplete_pages", [])
            if str(page).isdigit()
        })
    elif grouped_skill:
        completed_pages = {
            int(page)
            for outcome in outcomes.values()
            if isinstance(outcome, dict) and bool(outcome.get("ok"))
            for page in outcome.get("completed_pages", [])
            if str(page).isdigit()
        }
        incomplete_pages = [
            page for page in planned_pages if page not in completed_pages
        ]
    elif revision_mode:
        incomplete_pages = sorted(
            int(label.rsplit("_", 1)[-1])
            for label, outcome in outcomes.items()
            if re.fullmatch(r"slide_\d+", str(label))
            and isinstance(outcome, dict)
            and not bool(outcome.get("ok"))
        )
    else:
        # Single content ownership may still contain the explicit bookends or
        # dividers visual-memory group.  Count durable completed_pages from any
        # successful child instead of assuming every outcome label is slide_NN.
        completed_pages = {
            int(page)
            for label, outcome in outcomes.items()
            if isinstance(outcome, dict) and bool(outcome.get("ok"))
            for page in (
                outcome.get("completed_pages", [])
                if outcome.get("completed_pages")
                else (
                    [int(str(label).rsplit("_", 1)[-1])]
                    if re.fullmatch(r"slide_\d+", str(label))
                    else []
                )
            )
            if str(page).isdigit()
        }
        incomplete_pages = [
            page
            for page in planned_pages
            if page not in completed_pages
        ]
    if incomplete_pages:
        if grouped_skill:
            grouped: dict[str, list[int]] = {}
            for page in incomplete_pages:
                group_id = _planned_production_group(
                    Path(agent.ws) / "plan" / f"slide_{page:02d}.md"
                ) or f"page-{page:02d}"
                grouped.setdefault(group_id, []).append(page)
            labels = ", ".join(
                f"SlideGroup {group_id} [{','.join(f'{page:02d}' for page in pages)}]:"
                for group_id, pages in grouped.items()
            )
            return (
                "以下页组没有形成可交给 Review 的持久 HTML/PNG 基线："
                f"{labels}。不要创建新的 SlideGroup 实例；只有 Harness 明确判定为 API/"
                "超时等基础设施中断时才允许一次 operational retry，否则保留明确失败并结束。"
            )
        labels = ", ".join(f"Slide {page:02d}:" for page in incomplete_pages)
        return (
            "以下单页没有形成可交给 Review 的持久 HTML/PNG 基线："
            f"{labels}。不要创建新的 Slide Agent；只有 Harness 明确判定为 API/超时等"
            "基础设施中断时才允许一次 operational retry，否则保留明确失败并结束。"
        )
    if not agent.review_completed:
        review_outcome = dict(getattr(agent, "child_outcomes", {}) or {}).get("review")
        if (
            isinstance(review_outcome, dict)
            and str(review_outcome.get("exit_reason") or "")
            == "review_incomplete_current_pixels"
        ):
            return (
                "Review 已尝试但因未 finalize 的修改而以 review_incomplete_current_pixels "
                "结束。当前像素是 stale 的，不能交付。这是一次性诚实失败——不要重复委派 "
                "Review，不要声称 ready，不要 incomplete_closure 循环。以 Review closure "
                "failed 诚实结束。"
            )
        return (
            "Slide 阶段已发生，但唯一 Review 尚未完成。先整册 render，再委派一次 Review；"
            "同一输入上不得创建 review_r2/review_r3。Review 的检查、集中修复和复验在"
            "其自身一次会话中闭环。"
        )
    if not bool(getattr(agent, "finalize_attempted", False)):
        return (
            "尚未执行最终 finalize 质量门。委派 Review 运行 review.py finalize；"
            "只有退出码为 0 才能交付。"
        )
    if not bool(getattr(agent, "finalize_succeeded", False)):
        failure = str(getattr(agent, "finalize_failure", "") or "")[-600:]
        detail = f"最近失败：{failure}。" if failure else ""
        return (
            "最近一次 finalize 仍是硬失败。Review ready、Vision pass 或 "
            "orchestrator.py audit PASS 都不能覆盖该结论。"
            + detail
            + "唯一 Review 应在自身会话中修复并复验；若它已结束，则相同输入上不再"
            "创建新 Review，保留明确硬失败。只有外部修复改变交付面后才允许 verify continuation。"
        )
    if not agent.final_view_after_review:
        return (
            "Review 尚未完成最终像素闭环。若 Review 请求了跨角色修复，先完成该修复；"
            "只有交付面 fingerprint 已变化时才允许一次 verify continuation。相同输入上"
            "禁止重复 Review。"
        )
    return ""


def _run_loop_impl(agent: Agent) -> bool:
    agent.snapshot()
    if agent.role == "orchestrator" and agent.skill_language == "auto":
        agent.log(
            "Harness 同时暴露中英文说明版；首次读取其中一个 SKILL.md "
            "即锁定本 Deck 的说明版本，成品语言仍由 query 决定。"
        )
    elif agent.role == "orchestrator":
        agent.log(
            f"Harness 只暴露说明版 {agent.skill_name}"
            f"（{agent.skill_language}）；该选择不决定成品语言。"
            "Orchestrator 角色卡已完整注入；只需按入口读取 SKILL.md"
        )
    else:
        agent.log(
            f"Harness 沿用 {agent.skill_name}（{agent.skill_language}）"
            f"；roles/{agent.role}.md 已完整注入，不需要分页读取"
        )
    messages = [{"role": "user", "content": agent.initial_user}]
    trace_messages = copy.deepcopy(messages)
    tool_log: list[dict] = []
    heals = 0
    closure_nudges = 0
    # Review owns up to REVIEW_MAX_ATTEMPTS coordinated repair rounds inside
    # one child session.  Do not spend that lifecycle budget on the generic
    # two-nudge closure counter used by the other roles: doing so used to end a
    # Review after its first repair when the current pixels still exposed a
    # second issue, inviting the parent to attempt a forbidden review_r2.
    review_closure_nudges = 0
    progress_guard = _ProgressGuard(agent.ws)

    def append_message(message: dict) -> None:
        messages.append(message)
        trace_messages.append(copy.deepcopy(message))

    for turn in range(agent.max_turns):
        agent.turn = turn
        pending_at_turn_start = tools.pending_read_requirement(agent)
        if pending_at_turn_start is None and turn > 0 and (
            agent.last_input_tokens >= config.HISTORY_COMPACT_INPUT_TOKENS
            or (
                turn % 8 == 0
                and _live_history_chars(messages) >= config.HISTORY_COMPACT_CHARS
            )
        ):
            before = len(messages)
            if _compact_live_history_for_profile(
                agent,
                messages,
                config.HISTORY_KEEP_RECENT_MESSAGES,
            ):
                agent.log(
                    "[context compacted] live messages "
                    f"{before}->{len(messages)}; full trace retained"
                )
                agent.last_input_tokens = 0
        if time.monotonic() >= agent.deadline_monotonic:
            agent.exit_reason = "runtime_timeout"
            agent.log(
                f"Harness 存活保护触发：{agent.label} 已运行约 "
                f"{agent.runtime_limit_s}s"
            )
            break
        pending_read = pending_at_turn_start
        if pending_read is not None and _auto_continue_read_allowed(
            agent, pending_read[0]
        ):
            required_path, required_offset = pending_read
            agent.log(
                "[pending_read auto-continue] read_file "
                f"path={required_path} offset={required_offset}"
            )
            value = tools.read_file(
                agent,
                required_path,
                offset=required_offset,
                limit=500,
            )
            append_message({
                "role": "user",
                "content": (
                    "[Harness automatic read continuation; this is a tool result, "
                    "not a new user instruction]\n" + str(value)
                ),
            })
            continue
        response = _call(agent, messages, first_response=(turn == 0))
        if response is None:
            agent.exit_reason = "api_failed"
            break
        input_tokens = getattr(getattr(response, "usage", None), "input_tokens", 0) or 0
        agent.last_input_tokens = input_tokens
        agent.peak_input_tokens = max(agent.peak_input_tokens, input_tokens)
        response_blocks, calls, collapsed_calls = _collapse_same_turn_tool_uses(
            response.content
        )
        if collapsed_calls:
            agent.log(
                "[same-turn dedupe] collapsed "
                f"{collapsed_calls} exact duplicate tool call(s)"
            )
        append_message(
            {"role": "assistant", "content": agent.blocks(response_blocks)}
        )
        # Inference releases image bytes after one model-visible consumption.
        # Synthesis keeps the exact active multimodal history; its persisted
        # trace still stores immutable shot references instead of base64.
        _release_consumed_images_for_profile(agent, messages[:-1])

        text = ""
        for block in response_blocks:
            if block.type == "text" and block.text.strip():
                text = block.text.strip()
                agent.final_text = text
                agent.log(f"[{turn}] 💬 {text[:180]}")
        if not calls:
            if text:
                incomplete_read = tools.pending_read_error(agent)
                if incomplete_read and closure_nudges < 2:
                    closure_nudges += 1
                    agent.log(f"[read continuation] {incomplete_read}")
                    append_message(
                        {"role": "user", "content": f"[系统] {incomplete_read}"}
                    )
                    continue
                if incomplete_read:
                    agent.exit_reason = "incomplete_read"
                    agent.log(f"[read continuation failed] {incomplete_read}")
                    break
                pending_assets = tuple(
                    getattr(agent, "_grouped_asset_pending_pages", ()) or ()
                )
                if agent.role == "slide" and pending_assets:
                    agent.exit_reason = "asset_pending"
                    page_text = ",".join(
                        f"{int(page):02d}" for page in pending_assets
                    )
                    agent.log(
                        "[asset_pending] 当前页组启动时素材尚未交付："
                        f"P{page_text}；结束误启动实例。Image 完成后仅允许一次显式 "
                        "operational retry，不计作页面 revision"
                    )
                    break
                gap = _finish_gap(agent)
                review_nudge_limit = _review_in_session_closure_nudge_limit(agent)
                review_can_continue = bool(
                    gap
                    and agent.role == "review"
                    and review_closure_nudges < review_nudge_limit
                )
                generic_can_continue = bool(
                    gap and agent.role != "review" and closure_nudges < 2
                )
                if review_can_continue or generic_can_continue:
                    if agent.role == "review":
                        review_closure_nudges += 1
                        nudge_label = (
                            "review in-session closure "
                            f"{review_closure_nudges}/"
                            f"{review_nudge_limit}"
                        )
                    else:
                        closure_nudges += 1
                        nudge_label = "closure"
                    agent.log(f"[{nudge_label}] {gap}")
                    append_message(
                        {"role": "user", "content": f"[系统] {gap}"}
                    )
                    continue
                if gap:
                    agent.exit_reason = "incomplete_closure"
                    agent.log(f"[closure failed] {gap}")
                    break
                agent.exit_reason = "text_response"
                break
            if heals < config.MAX_HEALS:
                heals += 1
                if heals >= 2:
                    before = len(messages)
                    if _compact_live_history_for_profile(
                        agent,
                        messages,
                        max(8, min(config.HISTORY_KEEP_RECENT_MESSAGES, 12)),
                    ):
                        agent.log(
                            "[empty recovery] compacted live messages "
                            f"{before}->{len(messages)} before retry {heals}"
                        )
                        agent.last_input_tokens = 0
                append_message(
                    {
                        "role": "user",
                        "content": (
                            CONTINUE_PROMPT
                            if response.stop_reason == "max_tokens" and heals == 1
                            else EMPTY_RECOVERY_PROMPTS[
                                min(heals - 1, len(EMPTY_RECOVERY_PROMPTS) - 1)
                            ]
                        ),
                    }
                )
                continue
            agent.exit_reason = "empty_giveup"
            break

        # Recovery is about consecutive empty responses. Any concrete tool call
        # proves that the agent resumed and restores the full recovery budget.
        heals = 0
        tool_results = _tool_results(agent, calls, turn, tool_log)
        progress_action, progress_reason = progress_guard.observe(
            calls,
            tool_results,
        )
        if progress_action == "warn":
            tool_results.append(
                {
                    "type": "text",
                    "text": (
                        "[系统] 检测到可能停滞："
                        f"{progress_reason}。停止重复同一路径；换一种方法，"
                        "或在错误不可恢复时如实结束。"
                    ),
                }
            )
        append_message({"role": "user", "content": tool_results})
        _flush(agent, trace_messages, tool_log)
        if bool(getattr(agent, "review_terminal_failure", False)):
            agent.final_text = (
                "status: failed\n"
                "issue_type: review_closure_failed\n"
                "evidence: current pixels stale after bounded closure tail\n"
                "blocking: yes\n"
                "final_pixels_inspected: no"
            )
            agent.exit_reason = "review_closure_failed"
            agent.log(
                "[review_closure_failed] deterministic terminal stop — "
                "review_terminal_failure flag set, loop exiting"
            )
            break
        if _research_handoff_written(agent, calls, tool_results):
            agent.final_text = (
                "status: ready\n"
                "artifact: research/knowledge-brief.md\n"
                "evidence: canonical research handoff written successfully"
            )
            agent.exit_reason = "text_response"
            agent.log(
                "[research auto-close] canonical knowledge brief is durable; "
                "further search and duplicate rewrites skipped"
            )
            break
        if progress_action == "stop":
            agent.exit_reason = "stalled_repetition"
            agent.log(f"[stalled] {progress_reason}")
            break
    else:
        agent.exit_reason = "max_turns"

    # ─── Review closure tail ───────────────────────────────────────────
    # When Review hits max_turns with unfinalized mutations and closure-only
    # is already active, grant a bounded tail (same identity, no review_r2)
    # whose only goal is: finalize + inspect current pixels + return status.
    if (
        agent.exit_reason == "max_turns"
        and str(getattr(agent, "role", "") or "").lower() == "review"
        and bool(getattr(agent, "review_closure_only", False))
        and int(getattr(agent, "review_patches_since_finalize", 0) or 0) > 0
    ):
        agent.log(
            f"[review closure tail] granting {REVIEW_CLOSURE_TAIL_BUDGET} "
            f"extra closure-only turns; patches_since_finalize="
            f"{agent.review_patches_since_finalize}"
        )
        agent.review_closure_tail_used = 0
        for tail_turn in range(REVIEW_CLOSURE_TAIL_BUDGET):
            agent.review_closure_tail_used = tail_turn + 1
            agent.turn = agent.max_turns + tail_turn
            response = _call(agent, messages, with_tools=True)
            if response is None:
                break
            calls = [b for b in response.content if b.type == "tool_use"]
            text_parts = [
                b.text.strip()
                for b in response.content
                if b.type == "text" and b.text.strip()
            ]
            if text_parts:
                agent.final_text = text_parts[-1]
            append_message(
                {"role": "assistant", "content": agent.blocks(response.content)}
            )
            if not calls:
                agent.exit_reason = "text_response"
                agent.log("[review closure tail] text response — tail complete")
                break
            tool_results = _tool_results(agent, calls, agent.turn, tool_log)
            append_message({"role": "user", "content": tool_results})
            _flush(agent, trace_messages, tool_log)
            if int(getattr(agent, "review_patches_since_finalize", 0) or 0) == 0:
                agent.log(
                    "[review closure tail] finalize succeeded in tail — "
                    "continuing to inspect pixels"
                )
        else:
            agent.log(
                "[review closure tail exhausted] still stale after "
                f"{REVIEW_CLOSURE_TAIL_BUDGET} turns"
            )

    if agent.exit_reason == "max_turns":
        append_message({"role": "user", "content": FINAL_PROMPT})
        response = _call(agent, messages, with_tools=False)
        if response is not None:
            text = next((b.text.strip() for b in response.content if b.type == "text" and b.text.strip()), "")
            if text:
                agent.final_text = text
                agent.forced_summary = True
                append_message(
                    {"role": "assistant", "content": agent.blocks(response.content)}
                )
                # The bounded no-tool call is a real final-response slot, not
                # merely a diagnostic summary.  When all durable role gates
                # are already closed, accept its structured handoff instead
                # of discarding a valid Review/Image result solely because
                # the last normal turn was a tool result.  Child-specific
                # deliverable checks still run after ``run_loop`` returns, so
                # this cannot turn missing pixels or open critic issues into
                # success.
                forced_gap = _finish_gap(agent)
                if not forced_gap:
                    agent.exit_reason = "text_response"
                    agent.log(
                        "[bounded closure] final no-tool response accepted; "
                        "all durable role gates are closed"
                    )
                else:
                    agent.log(
                        "[bounded closure rejected] " + forced_gap[:1200]
                    )
    _flush(agent, trace_messages, tool_log)
    if agent.nova_raw is not None:
        workspace_root = Path(agent.ws)
        artifact_candidates: list[Path] = []
        if agent.role == "orchestrator":
            artifact_candidates.extend([
                workspace_root / "present.html",
                workspace_root / "speech.md",
            ])
            artifact_candidates.extend(sorted((workspace_root / "renders").glob("*.png")))
        elif agent.role == "slide":
            for page in tuple(agent.assigned_slide_pages or ()):
                artifact_candidates.extend([
                    workspace_root / "slides" / f"slide_{int(page):02d}.html",
                    workspace_root / "renders" / f"slide_{int(page):02d}.png",
                ])
        elif agent.role == "material":
            artifact_candidates.append(workspace_root / "research" / "material.md")
        elif agent.role == "research":
            artifact_candidates.append(workspace_root / "research" / "knowledge-brief.md")
        elif agent.role == "image":
            artifact_candidates.append(workspace_root / "assets" / "catalog.md")
        elif agent.role == "review":
            artifact_candidates.extend(sorted((workspace_root / "renders").glob("*.png")))
        nova_artifacts = []
        for candidate in artifact_candidates:
            if not candidate.is_file():
                continue
            payload = candidate.read_bytes()
            nova_artifacts.append({
                "workspace_path": str(candidate.relative_to(agent.ws)),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
            })
        agent.nova_precheck = agent.nova_raw.finalize(
            messages=messages,
            final_answer=agent.final_text,
            status=("completed" if agent.exit_reason == "text_response" else "failed"),
            exit_reason=agent.exit_reason or "unknown",
            artifacts=nova_artifacts,
        )
    agent.finish_snapshot()
    agent.log(
        f"轨迹已写 turns={len(tool_log)} renders={agent.n_renders} "
        f"views={agent.n_views} exit={agent.exit_reason}"
    )
    return agent.exit_reason == "text_response"


def run_loop(agent: Agent) -> bool:
    """Run one Agent and quarantine incomplete Nova raw state on exceptions."""
    try:
        return _run_loop_impl(agent)
    except BaseException as exc:
        if agent.nova_raw is not None:
            agent.nova_raw.abort(f"{type(exc).__name__}: {str(exc)[:1000]}")
            agent.nova_precheck = {"ok": False, "errors": [str(exc)]}
        raise


def _link_skill(workspace: str, skill_names: set[str]) -> None:
    """Materialize per-Deck Skill snapshots; never expose mutable source links."""
    root = Path(workspace) / "skills"
    if root.is_symlink():
        legacy_root = root.resolve()
        root.unlink()
        root.mkdir(parents=True, exist_ok=True)
        for name in sorted(skill_names):
            legacy_source = legacy_root / name
            if legacy_source.is_dir():
                shutil.copytree(legacy_source, root / name)
    else:
        root.mkdir(parents=True, exist_ok=True)
    for name in sorted(skill_names):
        source = Path(config.SKILLS_DIR) / name
        if not (source / "SKILL.md").is_file():
            raise FileNotFoundError(f"Skill 不完整：{source}")
        target = root / name
        if target.is_symlink():
            # Materialize legacy per-edition links on resume so later source
            # edits cannot continue changing this Deck.
            resolved = target.resolve()
            temporary = root / f".{name}.snapshot"
            if temporary.exists():
                shutil.rmtree(temporary)
            shutil.copytree(resolved, temporary)
            target.unlink()
            temporary.rename(target)
            continue
        if target.exists():
            continue
        shutil.copytree(source, target)


def _prepare_workspace(workspace: str, skill_names: set[str]) -> str:
    """Prepare a Deck deterministically before the first model call.

    The Chinese and English editions share an identical runtime contract. For an
    automatic language choice, either frozen edition can therefore initialize
    the common workspace without preselecting the model-facing Skill.
    """
    names = sorted(name for name in skill_names if name)
    if not names:
        raise ValueError("没有可用于工作区准备的 Skill")
    root = Path(workspace)
    script = root / "skills" / names[0] / "scripts" / "_internal" / "bootstrap.py"
    if not script.is_file():
        raise FileNotFoundError(f"工作区准备脚本不存在：{script}")
    result = subprocess.run(
        [sys.executable, str(script), "prepare", "."],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=config.WORKSPACE_IO_TIMEOUT_S,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "no output").strip()[-2000:]
        raise RuntimeError(
            f"确定性工作区准备失败（exit={result.returncode}）：{detail}"
        )
    # ``tmp/`` is Harness-owned shared scratch space.  Keep the empty directory
    # present across a child wave so concurrent atomic writers cannot race with
    # another writer removing it between mkdir and mkstemp.
    (root / "tmp").mkdir(parents=True, exist_ok=True)
    required = [
        root / "plan",
        root / "slides",
        root / "assets",
        root / "research",
        root / "renders",
        root / "tmp",
        root / "base.css",
        root / "assets" / "catalog.md",
    ]
    missing = [str(path.relative_to(root)) for path in required if not path.exists()]
    if missing:
        raise RuntimeError(f"工作区准备未生成必要产物：{missing}")
    return (result.stdout or "").strip()


def _quarantine_noncanonical_artifacts(workspace: str) -> list[str]:
    """Move model-authored scratch sidecars out of the delivery surface."""
    root = Path(workspace)
    candidates: set[Path] = set()
    search_groups = [
        [path for path in root.iterdir() if path.is_file()],
    ]
    for parent in (
        root / "plan",
        root / "slides",
        root / "assets",
        root / "research",
        root / "checks",
    ):
        if not parent.is_dir():
            continue
        search_groups.append(list(parent.rglob("*")))
    for paths in search_groups:
        for path in paths:
            if not path.is_file():
                continue
            name = path.name.lower()
            if (
                name.startswith("base.css.")
                or name.endswith((".new", ".tmp", ".bak", ".orig", ".rej"))
                or name.endswith("~")
            ):
                candidates.add(path)
    if not candidates:
        return []
    quarantine = root / "_trace" / "noncanonical-artifacts"
    quarantine.mkdir(parents=True, exist_ok=True)
    moved = []
    for source in sorted(candidates):
        relative = source.relative_to(root)
        flattened = "__".join(relative.parts)
        target = quarantine / flattened
        counter = 1
        while target.exists():
            target = quarantine / f"{flattened}.{counter}"
            counter += 1
        source.replace(target)
        moved.append(str(relative))
    return moved


def _workspace_output_violations(workspace: str, skill_name: str) -> list[str]:
    """Run the frozen Skill's read-only audit before any quarantine mutates evidence."""
    root = Path(workspace)
    # Child waves have joined before delivery audit.  Remove only an empty
    # controlled scratch directory here so older Skill audits that predate the
    # persistent concurrency-safe ``tmp/`` convention remain compatible.
    try:
        (root / "tmp").rmdir()
    except (FileNotFoundError, OSError):
        pass
    script = root / "skills" / skill_name / "scripts" / "orchestrator.py"
    if not script.is_file():
        return [f"workspace audit script is missing: {script.relative_to(root)}"]
    try:
        completed = subprocess.run(
            [sys.executable, str(script), "audit", "."],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=config.WORKSPACE_IO_TIMEOUT_S,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return [f"workspace audit could not run: {type(exc).__name__}: {exc}"]
    if completed.returncode == 0:
        return []
    detail = (completed.stderr or completed.stdout or "workspace audit failed").strip()
    violations = [
        line[2:].strip()
        for line in detail.splitlines()
        if line.strip().startswith("- ")
    ]
    return violations or [detail[-2000:]]


def _accept(agent: Agent, workspace: str) -> tuple[bool, str, dict]:
    """Minimal integrity gate; aesthetics stay in the visual review."""
    unresolved_children = [
        {"label": label, **outcome}
        for label, outcome in sorted(
            getattr(agent, "child_outcomes", {}).items()
        )
        if not bool(outcome.get("ok"))
    ]
    policy_detail = {
        "tool_policy_violations": list(
            getattr(agent, "tool_policy_violations", [])
        ),
        "workspace_policy_violations": list(
            getattr(agent, "workspace_policy_violations", [])
        ),
        "unresolved_child_failures": unresolved_children,
    }
    if bool(getattr(agent, "material_blocked", False)):
        return False, "material_blocked", {
            "material_blocked": True,
            **policy_detail,
        }
    if bool(getattr(agent, "research_blocked", False)):
        return False, "research_blocked", {
            "research_blocked": True,
            **policy_detail,
        }
    if os.environ.get("CLEAN_PLAN_ONLY_EVAL", "0") == "1":
        root = Path(workspace)
        plans = sorted((root / "plan").glob("slide_[0-9][0-9].md"))
        required = [root / "plan" / "deck.md"]
        if bool(getattr(agent, "material_required", True)):
            required.insert(0, root / "research" / "material.md")
        if bool(getattr(agent, "research_required", True)):
            required.insert(0, root / "research" / "knowledge-brief.md")
        missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
        detail = {
            "plan_only": True,
            "n_slide_plans": len(plans),
            "missing": missing,
            **policy_detail,
        }
        if unresolved_children:
            return False, "unresolved child agent failure", detail
        if missing or len(plans) < 2:
            return False, "plan-only deliverables incomplete", detail
        return True, "plan-only deliverables complete", detail
    if unresolved_children:
        return False, "unresolved child agent failure", policy_detail
    if policy_detail["workspace_policy_violations"]:
        return False, "workspace output policy violation", policy_detail
    presentation = Path(workspace) / "present.html"
    meta_path = Path(workspace) / "renders" / "render.json"
    if not presentation.is_file() or presentation.stat().st_size < 500:
        return False, "present.html missing", {}
    if agent.n_renders < 1:
        return False, "no render command recorded", {}
    if not meta_path.is_file():
        return False, "renders/render.json missing", {}
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return False, f"invalid render.json: {exc}", {}
    detail = {
        "n_pages": meta.get("n_pages", 0),
        "blank_pages": meta.get("blank_pages", []),
        "console_errors": meta.get("console_errors", []),
        "static_pages": meta.get("static_pages", []),
        "special_page_geometry": meta.get("special_page_geometry", {}),
        "deck_language": getattr(agent, "deck_language", "auto"),
        "review_completed": agent.review_completed,
        "final_render_after_review": agent.final_render_after_review,
        "final_view_after_review": agent.final_view_after_review,
        "finalize_attempted": bool(getattr(agent, "finalize_attempted", False)),
        "finalize_succeeded": bool(getattr(agent, "finalize_succeeded", False)),
        "finalize_failure": str(getattr(agent, "finalize_failure", ""))[-1200:],
        "quality_status": str(getattr(agent, "quality_status", "ready") or "ready"),
        **policy_detail,
    }
    if detail["n_pages"] < 1:
        return False, "renderer found no pages", detail
    if detail["blank_pages"]:
        return False, f"blank pages: {detail['blank_pages']}", detail
    if detail["console_errors"]:
        return False, f"console errors: {len(detail['console_errors'])}", detail
    if not detail["finalize_attempted"]:
        return False, "finalize quality gate was not run", detail
    if not detail["finalize_succeeded"]:
        return False, "latest finalize quality gate failed", detail
    if not detail["review_completed"]:
        return False, "final pixel review was not completed", detail
    if not detail["final_view_after_review"]:
        return False, "Review did not inspect pixels after its final page edit", detail
    root = Path(workspace)
    required = [
        root / "plan" / "deck.md",
        root / "speech.md",
        root / "assets" / "catalog.md",
    ]
    research_required = bool(getattr(agent, "research_required", True))
    if research_required:
        required.append(root / "research" / "knowledge-brief.md")
    slide_plans = sorted((root / "plan").glob("slide_[0-9][0-9].md"))
    if len(slide_plans) != int(detail["n_pages"]):
        return False, (
            f"slide plan count {len(slide_plans)} != rendered pages {detail['n_pages']}"
        ), detail
    if agent.material_required and (
        not agent.material_completed
        or not (root / "research" / "material.md").is_file()
    ):
        return False, "Material is required but research/material.md is missing", detail
    if research_required and not agent.research_completed:
        return False, "Research is required but was not completed", detail
    if agent.image_required:
        image_files = [
            path for path in (root / "assets").iterdir()
            if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
        ]
        if not agent.image_completed or not image_files:
            return False, "Image was required but no completed local image asset exists", detail
    missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
    if missing:
        return False, f"missing intermediate artifacts: {missing}", detail
    deck_plan = (root / "plan" / "deck.md").read_text(
        encoding="utf-8",
        errors="replace",
    )
    language_match = re.search(
        r"(?mi)^\s*-\s*language\s*:\s*(zh|en)\s*$",
        deck_plan,
    )
    actual_language = language_match.group(1).lower() if language_match else ""
    if actual_language not in {"zh", "en"}:
        return False, "plan/deck.md is missing a resolved delivery language", detail
    agent.deck_language = actual_language
    detail["deck_language"] = actual_language
    detail["plan_language"] = actual_language
    special_geometry = detail["special_page_geometry"]
    if special_geometry and special_geometry.get("status") != "PASS":
        return False, "special-page geometry audit failed", detail
    return True, "ok", detail


def _revision_fingerprint(workspace: str) -> str:
    """Hash the editable delivery surface, excluding trace/runtime metadata."""
    root = Path(workspace)
    candidates = [root / "base.css", root / "present.html", root / "speech.md"]
    for relative in ("plan", "slides", "assets"):
        parent = root / relative
        if parent.is_dir():
            candidates.extend(path for path in parent.rglob("*") if path.is_file())
    digest = hashlib.sha256()
    for path in sorted(set(candidates), key=lambda item: item.as_posix()):
        if not path.is_file():
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _review_delivery_fingerprint(workspace: str) -> str:
    """Hash editable sources, assets, and current final pixels for Review routing."""
    root = Path(workspace)
    digest = hashlib.sha256()
    digest.update(_revision_fingerprint(workspace).encode("ascii"))
    renders = root / "renders"
    if renders.is_dir():
        for path in sorted(
            (candidate for candidate in renders.rglob("*") if candidate.is_file()),
            key=lambda item: item.as_posix(),
        ):
            if path.suffix.lower() not in {".png", ".json"}:
                continue
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def _image_route_fingerprint(workspace: str) -> str:
    """Hash only the planned bitmap source routes and supplied-visual manifest."""
    root = Path(workspace)
    digest = hashlib.sha256()
    for path in sorted((root / "plan").glob("slide_[0-9][0-9].md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        needs = re.search(
            r"(?mi)^\s*-\s*needs_bitmap\s*:\s*(true|false)\s*$", text
        )
        medium = re.search(
            r"(?mi)^\s*-\s*primary_visual_medium\s*:\s*([^\s#]+)", text
        )
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update((needs.group(1).lower() if needs else "missing").encode("utf-8"))
        digest.update(b"\0")
        digest.update((medium.group(1).lower() if medium else "missing").encode("utf-8"))
        digest.update(b"\0")
    manifest = root / "_trace" / "attachment-manifest.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    visuals = payload.get("visual_asset_paths", []) if isinstance(payload, dict) else []
    digest.update(_canonical_json(sorted(str(path) for path in visuals)).encode("utf-8"))
    return digest.hexdigest()


def _claim_image_source_route_repair(
    parent: Agent,
    previous: dict,
) -> tuple[bool, str]:
    """Allow one pre-production Image continuation after a real route change.

    This is not a blind second acquisition pass.  Every failed bitmap page must
    still be unrendered, its canonical source medium must have changed, and the
    newly selected material/generated route must be available in the runtime
    capability contract.
    """
    if not isinstance(previous, dict) or not bool(previous.get("ok")):
        return False, "previous Image did not produce a bounded partial handoff"
    if str(previous.get("status") or "") != "partial_ready":
        return False, "previous Image is not a partial_ready source-route handoff"
    failed_pages = {
        int(page)
        for page in (getattr(parent, "image_failed_pages", ()) or ())
        if isinstance(page, int) and not isinstance(page, bool) and page > 0
    }
    if not failed_pages:
        return False, "previous Image handoff has no failed pages to reroute"
    root = Path(parent.ws)
    if any((root / "renders" / f"slide_{page:02d}.png").is_file() for page in failed_pages):
        return False, "a failed bitmap page already has pixels; route repair is no longer pre-production"
    before = str(previous.get("input_fingerprint") or "")
    after = _image_route_fingerprint(parent.ws)
    if not before or before == after:
        return False, "canonical bitmap source routes have not changed"

    media: dict[int, str] = {}
    for page in failed_pages:
        path = root / "plan" / f"slide_{page:02d}.md"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False, f"missing plan/slide_{page:02d}.md"
        match = re.search(
            r"(?mi)^\s*-\s*primary_visual_medium\s*:\s*([^\s#]+)", text
        )
        media[page] = match.group(1).lower() if match else ""
    allowed_media = {
        "bitmap-material", "mixed-material", "bitmap-generated", "mixed-generated"
    }
    invalid = {page: value for page, value in media.items() if value not in allowed_media}
    if invalid:
        return False, "failed pages were not rerouted to material/generated sources: " + str(invalid)

    capabilities = getattr(parent, "runtime_capabilities", {}) or {}
    inputs = capabilities.get("inputs") if isinstance(capabilities, dict) else {}
    tool_flags = capabilities.get("tools") if isinstance(capabilities, dict) else {}
    needs_material = any(value.endswith("material") for value in media.values())
    needs_generated = any(value.endswith("generated") for value in media.values())
    if needs_material and not list((inputs or {}).get("visual_asset_paths") or []):
        return False, "bitmap-material was selected but no supplied/material visual paths are available"
    if needs_generated and not bool((tool_flags or {}).get("image_generate")):
        return False, "bitmap-generated was selected but image_generate is unavailable"

    state = root / "_trace" / "image-source-route-repair.json"
    if state.is_file():
        return False, "the one bounded pre-production source-route repair was already consumed"
    _write_json(state, {
        "status": "claimed",
        "failed_pages": sorted(failed_pages),
        "before": before,
        "after": after,
        "media": {str(page): value for page, value in sorted(media.items())},
    })
    return True, ""


def _revision_prompt(
    seed: dict,
    revision: dict,
    *,
    grouped: bool = False,
    legacy_grouped: bool = False,
) -> str:
    instruction = str(revision.get("instruction") or "").strip()
    original_query = str(seed.get("user_query") or seed.get("query") or "").strip()
    if not instruction:
        raise ValueError("revision instruction 不能为空")
    language = (
        normalize_language(seed.get("lang"))
        or normalize_language(infer_deck_language(seed))
        or "zh"
    )
    complex_route_en = (
        "Then delegate only missing Research/Material/Image work and one complete "
        "structured `{role: slide, group_id: GROUP, pages: [NN,NN]}` task for each "
        "affected `production_group`; never "
        "split an affected group into individual pages. Preserve every unaffected "
        "group and artifact."
        if grouped
        else
        "Then delegate only missing Research/Material/Image work and one structured "
        "`{role: slide, pages: [NN]}` task per affected page. Preserve every unaffected artifact."
    )
    complex_route_zh = (
        "随后只补缺失的 Research/Material/Image，并把每个受影响的 "
        "`production_group` 作为完整的结构化 `{role: slide, group_id: GROUP, pages: [NN,NN]}` 委派；"
        "不得把受影响页组拆成单页。保持所有未受影响页组和产物不变。"
        if grouped
        else
        "随后只补缺失的 Research/Material/Image，并把每张受影响页分别委派为一个 "
        "结构化 `{role: slide, pages: [NN]}` 任务；保持所有未受影响产物不变。"
    )
    if not legacy_grouped:
        if language == "en":
            return f"""Revise the existing static presentation in this workspace. This is a continuation, not a fresh generation.

Latest user revision:
{instruction}

Original raw request:
{original_query}

First inspect the existing plan, HTML, assets, speech, and current renders read-only. Classify the revision by impact, not page count, and lock exactly one route:

1. Simple edit: the core argument, page order, page responsibilities, cross-page narrative, research/material/image evidence, deck-wide style, fonts, and shared structures all remain valid, and the bounded change is safe on a small known set of pages. Delegate exactly one `Review: mode=simple_edit; ...` containing the user revision, target pages, and invariants. Do not edit Slide HTML yourself and do not delegate Research, Material, Image, Slide, or another Review.
2. Complex edit: any topic/entity correction, factual or evidence change, narrative/page-order responsibility change, new asset need, global style/font/shared-structure change, or uncertain impact. Before mutation, write `plan/revision-impact.md` mapping impact on facts, narrative, page order, global style, assets, pages, the initial spoken-script sections in per-slide plans, and—when present—production groups. {complex_route_en} After affected pages close, finalize and delegate exactly one `Review: mode=final_review`.

A correction such as changing what the named subject refers to is complex even when phrased in one sentence: rerun entity resolution, update all affected plans/evidence/assets and the initial spoken-script sections in those plans, then rebuild every affected page. Never edit `speech.md` directly; derive it with `sync-speech`. Never treat the correction as a local wording patch. The Harness injects the original query and latest revision separately into Research. Finish with fresh final pixels, synchronized speech, and a rebuilt `present.html`."""
        return f"""这是对工作区现有静态演示的续编修订，不是从头生成。

最新用户修改要求：
{instruction}

原始用户请求：
{original_query}

先只读检查现有 plan、HTML、素材、讲稿与当前渲染，再按影响而非页数锁定且只选择一条路由：

1. 简单编辑：核心论点、页序、页面职责、跨页叙事、Research/Material/Image 证据、整册样式、字体和共享结构都继续有效，且修改范围明确、可在少量已知页面内安全完成。只委派唯一 `Review: mode=simple_edit; ...`，goal 必须包含用户要求、目标页和不可改变项。Orchestrator 不直接改 Slide HTML，也不委派 Research、Material、Image、Slide 或第二个 Review。
2. 复杂编辑：主题/实体纠正、事实或证据变化、叙事/页序/页面职责变化、新素材需求、全局样式/字体/共享结构变化，或影响范围不确定，任一成立即走此路由。修改前写 `plan/revision-impact.md`，分别列出事实、叙事、页序、全局样式、素材、页面、逐页计划中的初版口语讲稿以及存在时的 production group 影响。{complex_route_zh}受影响页面闭环后 finalize，最后只委派一次 `Review: mode=final_review`。

具名主题指代发生纠正时，即使用户只说一句也属于复杂编辑：必须重新消歧，更新全部受影响的计划、证据、素材与计划中的初版口语讲稿，并重做每张受影响页；禁止直接编辑 `speech.md`，统一由 `sync-speech` 派生，不能当成局部换字。Harness 会把原始 query 和最新修改分别注入 Research。最后以新鲜最终像素、同步讲稿和重建后的 `present.html` 交付。"""
    grouped_instruction_en = ""
    grouped_instruction_zh = ""
    if grouped:
        grouped_instruction_en = """

Follow the selected Grouped Skill's revision routing. A local change with a clearly bounded affected page, no new evidence or asset, and no shared-system impact must be completed by this agent without delegation: minimally update the affected source files, render and inspect only the changed pixels, then finalize. Delegate only when the change genuinely affects a production group, shared system, research, or image assets."""
        grouped_instruction_zh = """

遵循所选 Grouped Skill 的续编路由。受影响页面明确、不需要新事实/素材、也不影响共享系统的局部修改，必须由当前 Agent 直接完成，不做任何委派：最小修改相关真相源，只渲染并检查变化像素，然后 finalize。只有确实影响页组、共享系统、Research 或 Image 时才启动对应的最小角色集合。"""
    review_instruction_en = (
        "Pixel-check the changed slides and finalize"
        if grouped
        else "Pixel-check the changed slides, run whole-deck Review, and finalize"
    )
    review_instruction_zh = (
        "执行相关页像素检查和最终 finalize"
        if grouped
        else "执行相关页像素检查、整册 Review 和最终 finalize"
    )
    if language == "en":
        return f"""Revise the existing static presentation in this workspace. This is a continuation, not a fresh generation.

User follow-up:
{instruction}

Original request:
{original_query}

Treat the existing `plan/deck.md`, per-slide plans, HTML, assets, speech, and rendered output as the current source of truth. First identify the affected slides. Preserve unaffected slides and the deck's visual language; change only the plans, slides, assets, and speech needed to satisfy the follow-up. Reuse verified research and assets. Delegate new research or image work only when the follow-up creates a real evidence or visual gap. {review_instruction_en} so `present.html`, `speech.md`, PNGs, and plans agree. Do not delete, regenerate, or redesign unrelated slides.{grouped_instruction_en}"""
    return f"""这是对工作区中现有静态演示的续编修订，不是从头生成。

用户追加要求：
{instruction}

原始任务：
{original_query}

把现有 `plan/deck.md`、逐页计划、HTML、素材、讲稿和渲染结果视为当前真相源。先定位受影响页面，保留未受影响页面及整册视觉语言；只修改满足追加要求所必需的计划、页面、素材与讲稿。复用已核验的 Research 和素材，只有追加要求带来新的事实或视觉缺口时才补充对应角色。修改后{review_instruction_zh}，确保 `present.html`、`speech.md`、PNG 与计划同步。不要删除、重写或重新设计无关页面。{grouped_instruction_zh}"""




def run_sample(sample_id: str, seed: dict, workspace: str, cfg: dict) -> dict:
    cfg = dict(cfg)
    # Preserve the user's actual request independently from the Orchestrator's
    # delegated goal. Children inherit cfg, so Research always receives this
    # immutable value even when the Orchestrator has formed a wrong hypothesis.
    cfg["_raw_user_query"] = str(
        seed.get("user_query") or seed.get("query") or ""
    ).strip()
    try:
        structured_slide_count = max(0, int(seed.get("slide_count") or 0))
    except (TypeError, ValueError):
        structured_slide_count = 0
    cfg["_requested_slide_count"] = structured_slide_count
    cfg["_requested_slide_count_source"] = (
        "seed.slide_count" if structured_slide_count else "unspecified"
    )
    requested_scope = str(
        seed.get("evidence_scope") or cfg.get("evidence_scope") or ""
    ).strip().lower()
    scope_was_explicit = bool(requested_scope)
    attachment_profile = (
        dict(seed.get("_attachment_profile"))
        if isinstance(seed.get("_attachment_profile"), dict)
        else {}
    )
    evidence_material_present = str(
        attachment_profile.get("material_stage") or ""
    ) in {"direct_text", "agent_required", "mixed"}
    if (
        not scope_was_explicit
        and str(attachment_profile.get("material_stage") or "") == "direct_text"
        and not list(attachment_profile.get("unresolved_items") or [])
    ):
        # A deterministic direct-text handoff is already complete evidence.
        # Do not start Research merely because a search key exists.
        requested_scope = "attachment_only"
    requested_scope, _research_reason = runtime_capabilities.classify_research_mode(
        cfg["_raw_user_query"],
        has_material=evidence_material_present or (
            not attachment_profile and bool(seed.get("_staged_materials"))
        ),
        requested_mode=requested_scope,
    )
    cfg["_evidence_scope"] = requested_scope
    cfg["_evidence_scope_explicit"] = scope_was_explicit
    revision = seed.get("_revision") if isinstance(seed.get("_revision"), dict) else None
    cfg["_revision_instruction"] = (
        str(revision.get("instruction") or "").strip() if revision else ""
    )
    if revision:
        # Studio clones every revision into a fresh workspace, so the canonical
        # root trace is both collision-free and visible to the existing live UI.
        cfg["_task_started_epoch"] = time.time()
    cfg["query_language_hint"] = str(
        cfg.get("deck_language") or infer_deck_language(seed)
    )
    cfg["response_language"] = (
        normalize_language(cfg.get("response_language"))
        or normalize_language(cfg["query_language_hint"])
        or "auto"
    )
    skill_name = str(cfg.get("skill_name", config.SKILL_NAME))
    skill_language = str(cfg.get("skill_language", "zh"))
    if skill_language not in {"zh", "en", "auto"}:
        skill_language = (
            "zh"
            if infer_deck_language({"query": seed.get("query", "")}) == "zh"
            else "en"
        )
        skill_name = (
            config.SKILL_NAME_ZH
            if skill_language == "zh"
            else config.SKILL_NAME_EN
        )
        cfg["skill_name"] = skill_name
        cfg["skill_language"] = skill_language
    exposed = (
        {config.SKILL_NAME_ZH, config.SKILL_NAME_EN}
        if skill_language == "auto"
        else {skill_name}
    )
    _link_skill(workspace, exposed)
    grouped_revision = bool(revision) and _planned_ownership_topology(workspace) == "grouped"
    legacy_grouped_revision = any(
        _uses_legacy_grouped_contract(name) for name in exposed
    )
    query = (
        _revision_prompt(
            seed,
            revision,
            grouped=grouped_revision,
            legacy_grouped=legacy_grouped_revision,
        )
        if revision
        else str(seed.get("query", str(seed)))
    )
    staged_materials = [str(path) for path in seed.get("_staged_materials", [])]
    cfg["_staged_materials"] = staged_materials
    cfg["_attachment_profile"] = (
        dict(seed.get("_attachment_profile"))
        if isinstance(seed.get("_attachment_profile"), dict)
        else {}
    )
    cfg["_revision_mode"] = bool(revision)
    plan_only = os.environ.get("CLEAN_PLAN_ONLY_EVAL", "0") == "1"
    capability_profile = runtime_capabilities.detect_runtime_capabilities(
        cfg,
        staged_materials,
        skill_names=exposed,
        revision=bool(revision),
        plan_only=plan_only,
    )
    if (
        not scope_was_explicit
        and str(attachment_profile.get("material_stage") or "") == "direct_text"
        and not list(attachment_profile.get("unresolved_items") or [])
        and isinstance(capability_profile.get("workflow"), dict)
    ):
        capability_profile["workflow"]["research_reason"] = (
            "direct_text_complete_no_unresolved"
        )
    cfg["_runtime_capabilities"] = capability_profile
    capability_path = Path(workspace) / "_trace" / "runtime-capabilities.json"
    _write_json(capability_path, capability_profile)
    if capability_profile.get("fatal_errors"):
        raise RuntimeError(
            "runtime preflight blocked before model execution: "
            + "; ".join(str(item) for item in capability_profile["fatal_errors"])
        )
    if plan_only and not revision:
        query += """

[PLAN-ONLY EVALUATION]
本次只评估输入到规划的保真链路。先服从 Harness 注入的运行时能力合同：只执行其中
启用的 Material/Research 阶段，然后写完 plan/deck.md 与全部 plan/slide_NN.md，
并运行 validate-plans。完成后立即结束。禁止 scaffold-from-plans，禁止委派 Image、
Slide 或 Review，也不要制作 HTML、PNG、speech.md 或 present.html。逐页计划仍须完整
包含证据、视觉语义、来源与口语讲稿字段。
"""
    agent = Agent(sample_id, workspace, query, cfg)
    active_roles = set(capability_profile.get("active_roles") or ())
    agent.material_required = "material" in active_roles and not revision
    agent.research_required = (
        not revision
        and "research" in active_roles
        and not any(_uses_legacy_grouped_contract(name) for name in exposed)
    )
    agent.image_required = False
    revision_before = ""
    if revision:
        required_existing = [
            Path(workspace) / "plan" / "deck.md",
            Path(workspace) / "present.html",
            Path(workspace) / "base.css",
        ]
        missing_existing = [
            str(path.relative_to(workspace))
            for path in required_existing
            if not path.is_file()
        ]
        if missing_existing:
            raise RuntimeError(f"revision 缺少父版本产物：{missing_existing}")
        revision_before = _revision_fingerprint(workspace)
        agent.log(
            f"开始静态续编 revision {int(revision.get('revision_no') or 1)}；"
            "保留现有工作区，只修改追加要求涉及的页面。"
        )
    else:
        _prepare_workspace(workspace, exposed)
        agent.log(
            "工作区已准备好（Harness，首次模型调用前）："
            "plan/、slides/、assets/、research/、renders/、base.css、assets/catalog.md"
        )
        agent.log(
            "运行时能力检查完成：active_roles="
            + ",".join(capability_profile.get("active_roles", []))
            + "；omitted_roles="
            + ",".join(capability_profile.get("omitted_roles", []))
        )
    loop_ok = False
    try:
        loop_ok = run_loop(agent)
    finally:
        agent.workspace_policy_violations = (
            []
            if os.environ.get("CLEAN_PLAN_ONLY_EVAL", "0") == "1"
            else _workspace_output_violations(workspace, agent.skill_name)
        )
        if agent.workspace_policy_violations:
            agent.log(
                "工作区输出审计发现违规；本次样本将被拒绝："
                + "、".join(agent.workspace_policy_violations)
            )
        scratch = _quarantine_noncanonical_artifacts(workspace)
        if scratch:
            agent.log(
                "已将非规范临时产物移出交付根目录："
                + "、".join(scratch)
            )
    accept_ok, accept_reason, detail = _accept(agent, workspace)
    if revision:
        revision_after = _revision_fingerprint(workspace)
        detail = {
            **detail,
            "revision_no": int(revision.get("revision_no") or 1),
            "revision_changed": revision_after != revision_before,
            "parent_deck_id": revision.get("parent_deck_id"),
        }
        if revision_after == revision_before:
            accept_ok = False
            accept_reason = "revision produced no delivery changes"
    if loop_ok:
        ok = accept_ok
        reason = accept_reason
    else:
        ok = False
        reason = f"agent loop did not complete: {agent.exit_reason or 'unknown'}"
        detail = {
            **detail,
            "acceptance_ok": accept_ok,
            "acceptance_reason": accept_reason,
            "loop_ok": False,
        }
    if agent.profile.require_complete_trace:
        trace_statuses = [
            ("orchestrator", dict(agent.trace_mode_status or {})),
            *[
                (child.trace_label, dict(child.trace_mode_status or {}))
                for child in agent.children
            ],
        ]
        incomplete_traces = [
            label
            for label, trace_status in trace_statuses
            if not trace_status or trace_status.get("complete") is not True
        ]
        detail = {
            **detail,
            "multimodal_trace_statuses": [
                {"label": label, **trace_status}
                for label, trace_status in trace_statuses
            ],
            "multimodal_trace_incomplete": incomplete_traces,
        }
        if incomplete_traces:
            ok = False
            reason = "合成模式轨迹不完整: " + ", ".join(incomplete_traces[:8])
    agent.finish_snapshot()
    usage = model_call.cost_summary()
    # Provider/auth failures are operationally retryable batch failures, not
    # content rejections.  Keeping the distinction in the manifest prevents a
    # quota outage from being presented as a completed/rejected data outcome.
    operational_failure = (
        (not loop_ok)
        or bool(detail.get("unresolved_child_failures"))
        or agent.exit_reason in {
        "api_failed",
        "runtime_timeout",
        "max_turns",
        "stalled_repetition",
        "empty_giveup",
        "incomplete_closure",
        }
    )
    nova_reports = [
        candidate.nova_precheck
        for candidate in [agent, *agent.children]
        if candidate.nova_raw is not None
    ]
    nova_ok = all(report and report.get("ok") for report in nova_reports)
    if nova_reports and not nova_ok:
        ok = False
        reason = "Nova raw V2 precheck failed; trajectory quarantined"
    status = (
        "material_blocked"
        if bool(getattr(agent, "material_blocked", False))
        else "research_blocked"
        if bool(getattr(agent, "research_blocked", False))
        else "quarantine"
        if nova_reports and not nova_ok
        else (
            "needs_improvement"
            if ok and detail.get("quality_status") == "needs_improvement"
            else "completed"
            if ok
            else "failed"
            if operational_failure
            else "rejected"
        )
    )
    return {
        "status": status,
        "reason": reason,
        "skill_name": agent.skill_name,
        "skill_language": agent.skill_language,
        "response_language": agent.response_language,
        "deck_language": agent.deck_language,
        "query_language_hint": agent.query_language_hint,
        "exit_reason": agent.exit_reason,
        "n_renders": agent.n_renders,
        "n_views": agent.n_views,
        "tool_policy_violations": agent.tool_policy_violations,
        "workspace_policy_violations": agent.workspace_policy_violations,
        "model_calls": usage["calls"],
        "input_tokens": usage["input_tokens"],
        "cache_read_input_tokens": usage["cache_read_input_tokens"],
        "cache_creation_input_tokens": usage["cache_creation_input_tokens"],
        "prompt_cache_hit_rate": (
            usage["cache_read_input_tokens"]
            / (
                usage["input_tokens"]
                + usage["cache_read_input_tokens"]
                + usage["cache_creation_input_tokens"]
            )
            if (
                usage["input_tokens"]
                + usage["cache_read_input_tokens"]
                + usage["cache_creation_input_tokens"]
            )
            else 0.0
        ),
        "output_tokens": usage["output_tokens"],
        "accept_detail": detail,
        "nova_raw_v2": bool(nova_reports),
        "nova_raw_precheck_ok": nova_ok if nova_reports else None,
        "nova_main_trajectory_ids": [
            candidate.nova_raw.main_trajectory_id
            for candidate in [agent, *agent.children]
            if candidate.nova_raw is not None
        ],
        "run_mode": agent.run_mode,
        "multimodal_trace": dict(agent.trace_mode_status or {}),
        "runtime_capabilities": {
            "status": capability_profile.get("status"),
            "active_roles": capability_profile.get("active_roles", []),
            "omitted_roles": capability_profile.get("omitted_roles", []),
            "workflow": capability_profile.get("workflow", {}),
            "tools": capability_profile.get("tools", {}),
            "warnings": capability_profile.get("warnings", []),
        },
        "pid": os.getpid(),
    }
