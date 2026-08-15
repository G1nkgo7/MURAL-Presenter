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
        if workspace_changed:
            # A real artifact change starts a new progress window. Re-reading
            # the same source in a later phase must not inherit stale counts.
            self.action_counts.clear()
            self.action_counts.update(actions)
            self.identical_turns = 1
            self.error_turns = 1 if error_signature else 0
            self.warned.clear()
        if workspace_changed or novel_action:
            self.no_progress_turns = 0
        else:
            self.no_progress_turns += 1

        repeated_action = max(
            (self.action_counts[action] for action in actions),
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
状态为 omitted 时不得探测该文件。直供视觉素材只交给 Image；风格参考图由
Orchestrator 在规划前用 Vision 各查看一次，不得当作内容证据。Image=bitmap_unavailable 时，每页必须写
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
Only Image consumes supplied visual assets. Before planning, the Orchestrator views each
style-reference image once; never treat it as factual evidence.
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
Skill、调用工具并交付一套简短完整的示范稿（至少包含封面、内容页、结尾页），不得以
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
complete demonstration deck with at least a cover, content slide, and closing slide.
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
complete demonstration deck with at least a cover, content slide, and closing slide.
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
    """Load a v0.3 role contract outside the model-visible paged file tool."""
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
        role_card = _injected_role_card(self.ws, self.skill_name, self.role)
        if role_card:
            self.system = f"{self.system.rstrip()}\n\n{role_card}"
        self.role_card_injected = bool(role_card)
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
        self.image_required = False
        self.image_completed = False
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
        self.repair_required_reason = ""
        self.vision_critic_results: dict[str, dict] = {}
        self.required_review_pages: tuple[int, ...] = ()
        self.review_viewed_page_hashes: dict[int, str] = {}
        self.review_contact_sheet_inspected = False
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


def _call(
    agent: Agent,
    messages: list[dict],
    with_tools: bool = True,
    *,
    first_response: bool = False,
):
    visible_tools = agent.tool_schemas if with_tools else None
    model_messages = _messages_for_model(messages)
    pending = tools.pending_read_requirement(agent)
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
            agent.log(
                "[pending_read tool surface] only read_file "
                f"path={required_path} offset={required_offset}"
            )
    elif with_tools and _slide_authoring_stop_line(agent):
        # Once a page has consumed its three checked pixel states and the
        # current pixels still carry a known issue, further source edits are
        # blind.  Close the child as a non-fatal repair handoff instead of
        # allowing dozens of unverified patches.
        visible_tools = []
        model_messages = [
            *model_messages,
            {
                "role": "user",
                "content": (
                    "[Harness render stop line] The checked authoring budget is "
                    "exhausted while a current-pixel issue remains. Do not claim "
                    "ready. Return the structured status `repair_required` with "
                    "pages, issue_type, pixel evidence, and a proposed fix for Review."
                ),
            },
        ]
        agent.log(
            "[render stop line] tools hidden; require repair_required handoff"
        )
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
            re.escape(label) + r"_(?:r|retry|verify)(\d+)", path.name
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
        suffix = "verify" if recovery_kind == "verification" else "retry"
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
    child = Agent(
        parent.sid,
        parent.ws,
        task_text,
        child_cfg,
        role=role,
        label=label,
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
    material_status = ""
    if role == "material":
        material_path = Path(parent.ws) / "research" / "material.md"
        try:
            material_text = material_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            material_text = ""
        if re.search(
            r"(?mi)^\s*(?:[-*]\s*)?status\s*:\s*material_blocked\s*$",
            material_text,
        ):
            ok = False
            material_status = "material_blocked"
            child.exit_reason = "material_blocked"
            child.log(
                "[material blocked] 附件无法形成可靠证据；停止 Research 与规划，"
                "不允许用外部检索猜测缺失内容。"
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
            child.completed_slide_pages = tuple(
                int(page) for page in repair_issue.get("pages", [])
            )
            child.incomplete_slide_pages = ()
    review_status = ""
    review_fields: dict[str, str] = {}
    if role == "review":
        review_fields = _contract_fields(child.final_text)
        review_status = review_fields.get("status", "").lower()
        review_gap = _review_required_view_gap(child)
        if review_status == "needs_orchestrator" or review_gap:
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
            else material_status or review_status or ("ready" if ok else "failed")
        ),
        "repair_issue": repair_issue,
        "required_review_pages": list(
            getattr(child, "required_review_pages", ()) or ()
        ),
        "attempt": trace_attempt,
        "blocking": review_fields.get("blocking", "").lower(),
        "issue_type": review_fields.get("issue_type", "").lower(),
        "pages": review_fields.get("pages", ""),
        "evidence": review_fields.get("evidence", "")[-1200:],
        "input_fingerprint": (
            _review_delivery_fingerprint(parent.ws) if role == "review" else ""
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


def _open_vision_issues(parent: Agent) -> list[dict]:
    path = Path(parent.ws) / "_trace" / "vision-issues.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [
        dict(item) for item in payload.get("issues", [])
        if isinstance(item, dict) and item.get("status") == "open"
    ] if isinstance(payload, dict) else []


def _mandatory_fullres_review_pages(parent: Agent) -> set[int]:
    """Image-heavy, dense, and deterministically flagged pages need full pixels."""
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
        if re.search(r"(?mi)^\s*-\s*composition\s*:\s*(?:matrix|data-focus)\s*$", text):
            pages.add(page)
    try:
        manifest = json.loads((root / "renders/render.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = {}
    for row in manifest.get("pages", []) if isinstance(manifest, dict) else []:
        if not isinstance(row, dict):
            continue
        geometry = row.get("geometry") if isinstance(row.get("geometry"), dict) else {}
        boxes = geometry.get("text_boxes") if isinstance(geometry.get("text_boxes"), list) else []
        text_chars = sum(len(str(box.get("text") or "")) for box in boxes if isinstance(box, dict))
        if len(boxes) >= 10 or text_chars >= 700:
            try:
                pages.add(int(row.get("page")))
            except (TypeError, ValueError):
                pass
    geometry = manifest.get("special_page_geometry") if isinstance(manifest, dict) else {}
    for warning in geometry.get("warnings", []) if isinstance(geometry, dict) else []:
        match = re.search(r"\bpage\s+(\d+)\b", str(warning))
        if match:
            pages.add(int(match.group(1)))
    layout_defects = manifest.get("layout_defects") if isinstance(manifest, dict) else {}
    if isinstance(layout_defects, dict):
        for page_text, defects in layout_defects.items():
            if isinstance(defects, list) and defects and str(page_text).isdigit():
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
        current_pixels = bool(getattr(parent, "finalize_succeeded", False)) and bool(
            result.get("final_view_after_review")
        )
    return bool(
        not bool(result.get("ok"))
        and result.get("status") == "needs_orchestrator"
        and str(result.get("blocking") or "no").lower() != "yes"
        and bool(result.get("final_pixels_inspected"))
        and current_pixels
    )


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
                "复杂编辑路由尚未建立影响图。先只读检查现有计划、页面、素材、讲稿与渲染，"
                "把事实、叙事、页序、全局样式、素材、页面和讲稿的影响范围写入 "
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
            }).union(_mandatory_fullres_review_pages(parent))
        )
        spec["required_review_pages"] = required_pages
        if required_pages:
            ledger_path = Path(parent.ws) / "_trace" / "review-issues.json"
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
            review_task += (
                " 先读取 `_trace/review-issues.json`；其中的图片页、密集页或已知问题页 "
                f"{pages_text} 必须逐页打开当前 PNG，不能只看联系表。修复后重新 finalize "
                "并复看这些页的当前最终像素，关闭 issue 后才能返回 ready。"
                if parent.skill_language == "zh"
                else
                " First read `_trace/review-issues.json`. The image-heavy, dense, or flagged pages "
                f"{pages_text} are mandatory individual current-PNG inspections, not "
                "contact-sheet-only checks. After a repair, finalize and reopen their "
                "current final pixels before returning ready."
            )
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
            if _operational_child_retry_allowed(previous):
                spec["_recovery_kind"] = "operational"
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
    if "image" in roles and parent.image_completed and not any(
        str(spec.get("role", "")).lower() == "image" and bool(spec.get("repair"))
        for spec in specs
    ):
        return "Image 已完成：直接使用 assets/catalog.md 与本地素材。"
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
    if "review" in roles and not (root / "renders" / "contact-sheet.png").is_file():
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
            result = job.result()
            results[index] = result
            if index == image_index and not released:
                released = True
                if bool(result.get("ok")):
                    parent.image_completed = True
                    parent.log(
                        "Image 已完成；立即释放等待位图的 Slide，"
                        "无需等待仍在运行的 needs_bitmap:false 页面。"
                    )
                    for blocked_index in sorted(blocked_indices):
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
        str(spec.get("role", "")).lower() == "image" and bool(result.get("ok"))
        for spec, result in zip(specs, results)
    ):
        parent.image_required = True
        parent.image_completed = True
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
            "exit_reason": item.get("exit_reason"),
            "completed_pages": item.get("completed_pages", []),
            "incomplete_pages": item.get("incomplete_pages", []),
            "summary": str(item.get("summary", ""))[-800:],
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


def _delegate_task(parent: Agent, args: dict) -> str:
    """Map Hermes' singular delegation surface to presentation roles."""
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
                    if len(pages) != 1 or group_id:
                        return (
                            f"delegate_task 错误：task {index} 当前 topology=single，"
                            "每个结构化 slide 只能含一个 page 且不设置 group_id"
                        )
                    number = pages[0]
                    slide_numbers.append(number)
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
                if raw_pages is not None or task.get("group_id"):
                    return f"delegate_task 错误：task {index} 的 {structured_role} 不接受 pages/group_id"
                specs.append({
                    "role": structured_role,
                    "label": structured_role,
                    "task": goal or f"Complete the canonical {structured_role} handoff",
                    **({"repair": True} if task.get("repair") else {}),
                })
            continue
        # Compatibility adapter for historical snapshots that still encode the
        # role and ownership unit in a goal prefix. New v0.3 calls use fields.
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
                elif str(agent.repair_required_reason).startswith(prefix):
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
        if rendered_pages:
            agent.n_renders += len(rendered_pages)
            if agent.role == "slide":
                for page in rendered_pages:
                    path_text = getattr(agent, "expected_output_paths", {}).get(page)
                    if path_text:
                        agent.rendered_output_hashes[page] = _file_content_digest(
                            Path(path_text)
                        )
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
            agent.final_render_after_review = False
            agent.final_view_after_review = False
            agent.finalize_succeeded = False
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
    path = Path(agent.ws) / "_trace" / "vision-issues.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {"schema": "mural.vision-issues.v1", "issues": []}
    records = payload.get("issues") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        records = []
    page_match = re.search(r"(?:^|/)renders/slide_0*(\d+)\.png$", source)
    page = int(page_match.group(1)) if page_match else None
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
    if not declared and not runtime_reason and not exhausted_pages:
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
    """Stop blind page edits after the final checked pixel state stays open."""
    if getattr(agent, "role", "") != "slide":
        return False
    pages = tuple(getattr(agent, "assigned_slide_pages", ()) or ())
    exhausted = False
    for page in pages:
        used, limit = _page_render_budget(
            Path(agent.ws),
            int(page),
            str(getattr(agent, "trace_label", "") or ""),
        )
        if limit and used >= limit:
            exhausted = True
            break
    if not exhausted:
        return False
    if _unresolved_vision_critic_issues(agent):
        return True
    return bool(str(getattr(agent, "repair_required_reason", "") or "").strip())


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
    open_ledger = _open_vision_issues(agent)
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
    """Require original-resolution evidence for every cataloged bitmap."""
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
    paths = sorted(set(re.findall(
        r"(?mi)^\s*-\s*path\s*:\s*(assets/[^\s#]+)\s*$",
        text,
    )))
    results = dict(getattr(agent, "vision_critic_results", {}) or {})
    required = ["assets/contact-sheet.png", *paths]
    missing: list[str] = []
    stale: list[str] = []
    for source in required:
        record = results.get(source)
        if not isinstance(record, dict):
            missing.append(source)
            continue
        expected = str(record.get("source_sha256") or "")
        if expected and expected != _file_content_digest(root / source):
            stale.append(source)
    if missing or stale:
        details = []
        if missing:
            details.append("未看原图：" + ", ".join(missing))
        if stale:
            details.append("检查后文件已变化：" + ", ".join(stale))
        return (
            "Image 全分辨率验收未闭合（联系表只能定位异常，不能证明无水印/Logo/假字）："
            + "；".join(details)
            + "。逐张调用 vision_analyze，核验可见主体、四边、水印、Logo、文字、畸形与裁切；"
            "只替换有明确问题的素材。"
        )
    unresolved = _unresolved_vision_critic_issues(agent)
    if unresolved:
        details = "; ".join(
            f"{source}: {str(record.get('summary') or record.get('verdict'))[:200]}"
            for source, record in unresolved[:8]
        )
        return (
            "Image 原图仍有 repair_required/uncertain：" + details + "。"
            "按素材预算做一次定向替换并复验；仍不可得时返回 failed，不得声称 bitmap_ready。"
        )
    return ""


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
        if not current_sheet_hash or viewed_sheet_hash != current_sheet_hash:
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
    if agent.role == "review":
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
        incomplete_pages = [
            page
            for page in planned_pages
            if not (
                isinstance(outcomes.get(f"slide_{page:02d}"), dict)
                and bool(outcomes[f"slide_{page:02d}"].get("ok"))
            )
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
    progress_guard = _ProgressGuard(agent.ws)

    def append_message(message: dict) -> None:
        messages.append(message)
        trace_messages.append(copy.deepcopy(message))

    for turn in range(agent.max_turns):
        if turn > 0 and (
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
                if gap and closure_nudges < 2:
                    closure_nudges += 1
                    agent.log(f"[closure] {gap}")
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
        if progress_action == "stop":
            agent.exit_reason = "stalled_repetition"
            agent.log(f"[stalled] {progress_reason}")
            break
    else:
        agent.exit_reason = "max_turns"

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
2. Complex edit: any topic/entity correction, factual or evidence change, narrative/page-order responsibility change, new asset need, global style/font/shared-structure change, or uncertain impact. Before mutation, write `plan/revision-impact.md` mapping impact on facts, narrative, page order, global style, assets, pages, speech, and—when present—production groups. {complex_route_en} After affected pages close, finalize and delegate exactly one `Review: mode=final_review`.

A correction such as changing what the named subject refers to is complex even when phrased in one sentence: rerun entity resolution, update all affected plans/evidence/assets/speech, and rebuild every affected page. Never treat it as a local wording patch. The Harness injects the original query and latest revision separately into Research. Finish with fresh final pixels, synchronized speech, and a rebuilt `present.html`."""
        return f"""这是对工作区现有静态演示的续编修订，不是从头生成。

最新用户修改要求：
{instruction}

原始用户请求：
{original_query}

先只读检查现有 plan、HTML、素材、讲稿与当前渲染，再按影响而非页数锁定且只选择一条路由：

1. 简单编辑：核心论点、页序、页面职责、跨页叙事、Research/Material/Image 证据、整册样式、字体和共享结构都继续有效，且修改范围明确、可在少量已知页面内安全完成。只委派唯一 `Review: mode=simple_edit; ...`，goal 必须包含用户要求、目标页和不可改变项。Orchestrator 不直接改 Slide HTML，也不委派 Research、Material、Image、Slide 或第二个 Review。
2. 复杂编辑：主题/实体纠正、事实或证据变化、叙事/页序/页面职责变化、新素材需求、全局样式/字体/共享结构变化，或影响范围不确定，任一成立即走此路由。修改前写 `plan/revision-impact.md`，分别列出事实、叙事、页序、全局样式、素材、页面、讲稿以及存在时的 production group 影响。{complex_route_zh}受影响页面闭环后 finalize，最后只委派一次 `Review: mode=final_review`。

具名主题指代发生纠正时，即使用户只说一句也属于复杂编辑：必须重新消歧，更新全部受影响的计划、证据、素材和讲稿，并重做每张受影响页，不能当成局部换字。Harness 会把原始 query 和最新修改分别注入 Research。最后以新鲜最终像素、同步讲稿和重建后的 `present.html` 交付。"""
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
