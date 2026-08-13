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

from . import config, model_call, nova_raw, tools
from .language import infer_deck_language, normalize_language

ROLES = {"material", "research", "image", "slide", "review"}
GROUPED_SKILL_NAMES = {
    "long-horizon-html-ppt-grouped",
    "long-horizon-html-ppt-grouped-inline-image",
}
INLINE_IMAGE_SKILL = "long-horizon-html-ppt-grouped-inline-image"


def _is_grouped_skill_name(value: object) -> bool:
    return str(value or "") in GROUPED_SKILL_NAMES


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
) -> str:
    """Keep the Harness thin; read the selected Skill contract at runtime."""
    inline_image = _is_inline_image_skill_name(skill_name)
    root_workflow = skill_name == "long-horizon-html-ppt-grouped"
    startup_zh = (
        f"开工第一步读取 `skills/{skill_name}/SKILL.md`。\n"
        "随后严格按该入口 Skill 的根工作流执行。"
        if root_workflow
        else (
            "开工第一步依次读取：\n"
            f"1. `skills/{skill_name}/SKILL.md`\n"
            f"2. `skills/{skill_name}/roles/orchestrator.md`\n"
            "随后以 Skill 定义流程、以角色卡定义职责。"
        )
    )
    startup_en = (
        f"Your first action is to read `skills/{skill_name}/SKILL.md`.\n"
        "Then follow that root Skill for both workflow and responsibility."
        if root_workflow
        else (
            "Your first action is to read, in order:\n"
            f"1. `skills/{skill_name}/SKILL.md`\n"
            f"2. `skills/{skill_name}/roles/orchestrator.md`\n"
            "Then follow the Skill for workflow and the role card for responsibility."
        )
    )
    material_lines = "\n".join(f"- `{path}`" for path in staged_materials)
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
    if skill_language == "zh":
        tool_surface_zh = (
            "运行时工具面：Research 可使用 web_search；Image 可使用图片搜索，"
            "并且 image_generate 本次"
            + ("已启用。" if image_generation_enabled else "未启用。")
            if inline_image
            else "运行时工具面：Research/Image 可使用 web_search；Image 的 "
            "image_generate 本次"
            + ("已启用。" if image_generation_enabled else "未启用。")
        )
        child_roles_zh = (
            "material、research、slide、review"
            if inline_image
            else "material、research、image、slide、review"
        )
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
            "Runtime tool surface: Research can use web_search; Image can "
            "search images; image_generate is "
            + ("enabled." if image_generation_enabled else "disabled.")
            if inline_image
            else "Runtime tool surface: Research/Image can use web_search; Image "
            "image_generate is "
            + ("enabled." if image_generation_enabled else "disabled.")
        )
        child_roles_en = (
            "material, research, slide, and review"
            if inline_image
            else "material, research, image, slide, and review"
        )
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
) -> str:
    """Route a model through one of two equivalent instruction editions."""
    material_lines = "\n".join(f"- `{path}`" for path in staged_materials)
    material_context = (
        "\nMounted attachments (pass these paths to one Material task after "
        "choosing the instruction edition):\n"
        f"{material_lines}\n"
        if material_lines
        else ""
    )
    return f"""\
You are the HTML presentation Orchestrator responsible for audience, narrative,
the design system, role coordination, and final delivery. This invocation is always
a static HTML presentation production job, never an open-ended chat. Treat the first
user message only as the content brief. Even when it is merely `test`, `hello`, or an
otherwise underspecified phrase, read one Skill, use tools, and deliver a compact but
complete demonstration deck with at least a cover, content slide, and closing slide.
Never end with a generic readiness or connection-test response.
Runtime tool surface: Research/Image can use web_search; Image image_generate is
{'enabled' if image_generation_enabled else 'disabled'}. Plan only routes that are available.
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
only child roles are material, research, image, slide, and review. Finish with a
concise delivery summary and any remaining issue.
"""


def _child_system(
    role: str,
    skill_name: str,
    skill_language: str,
    response_language: str = "auto",
) -> str:
    """Give children a small routing prompt; their role card owns the details."""
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
开工第一步读取
`skills/{skill_name}/roles/{role}.md`，它是职责、输入、行为、输出和所需 reference
的唯一说明。不要读取完整 `SKILL.md`、其他角色卡或整个 references 目录。
只完成任务分配给你的职责；完成后简短列出产物和遗留问题。{material_note}
"""
    else:
        return f"""\
You are the {role.upper()} role in Static HTML Presentation.
The Harness selected `{skill_name}`. {_response_language_rule(response_language)}
Your first action is to read
`skills/{skill_name}/roles/{role}.md`; it is the sole description of your
responsibility, inputs, actions, outputs, and required references. Do not read the
full `SKILL.md`, other role cards, or the whole reference directory.
Complete only the assigned role work, then report outputs and remaining issues
concisely.{material_note}
"""


DELEGATE_TASK_SCHEMA = {
    "name": "delegate_task",
    "description": (
        "Delegate one presentation role or one independent Slide page per task. "
        "Prefix every goal with Material:, Research:, Image:, Review:, or "
        "Slide NN:. This v0.2 runtime does not accept SlideGroup tasks. "
        "Keep goals to case-specific objectives and paths; the role "
        "card supplies the method."
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
                    "required": ["goal"],
                    "additionalProperties": False,
                    "properties": {
                        "goal": {"type": "string"},
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
        self.trace_dir = (
            trace_root / "orchestrator"
            if role == "orchestrator"
            else trace_root / "subagents" / label
        )
        (self.trace_dir / "images").mkdir(parents=True, exist_ok=True)

        self.enable_image_gen = bool(cfg.get("enable_image_gen", config.ENABLE_IMAGE_GEN))
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
            self.render_script = ""
        else:
            self.allowed_skill_names = {self.skill_name}
            self.render_script = f"skills/{self.skill_name}/scripts/deck.py"
        self.bash_timeout = config.BASH_TIMEOUT_S
        self.max_vision_edge = config.MAX_VISION_EDGE

        if role == "orchestrator" and self.skill_language == "auto":
            self.system = _auto_orchestrator_system(
                tuple(str(path) for path in cfg.get("_staged_materials", [])),
                response_language=self.response_language,
                image_generation_enabled=self.enable_image_gen,
            )
        elif role == "orchestrator":
            self.system = _orchestrator_system(
                self.skill_name,
                self.skill_language,
                tuple(str(path) for path in cfg.get("_staged_materials", [])),
                response_language=self.response_language,
                image_generation_enabled=self.enable_image_gen,
            )
        else:
            self.system = _child_system(
                role,
                self.skill_name,
                self.skill_language,
                response_language=self.response_language,
            )
        prompt_language = "zh" if self.skill_language == "zh" else "en"
        self.system = (
            f"{self.system.rstrip()}\n\n"
            f"{_runtime_time_context(self.task_started_epoch, prompt_language)}\n"
        )
        self.revision_mode = bool(cfg.get("_revision_mode", False))
        self.tool_schemas = tools.agent_tools(
            role,
            enable_image_gen=self.enable_image_gen,
            render_script=self.render_script,
            skill_name=self.skill_name,
            revision_mode=self.revision_mode,
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
        self.research_required = not _is_grouped_skill_name(self.skill_name)
        self.research_completed = False
        self.image_required = False
        self.image_completed = False
        self.review_completed = False
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
        self.render_script = f"skills/{skill_name}/scripts/deck.py"
        self.tool_schemas = tools.agent_tools(
            self.role,
            enable_image_gen=self.enable_image_gen,
            render_script=self.render_script,
            skill_name=self.skill_name,
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


def _call(
    agent: Agent,
    messages: list[dict],
    with_tools: bool = True,
    *,
    first_response: bool = False,
):
    return model_call.call_with_tools(
        model=agent.model,
        system=agent.system,
        messages=messages,
        tools=agent.tool_schemas if with_tools else None,
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
    parent.children.append(child)
    ok = run_loop(child)
    if role == "slide":
        gap = _slide_deliverable_gap(child)
        if gap:
            ok = False
            if child.exit_reason == "text_response":
                child.exit_reason = "incomplete_deliverable"
            child.log(f"[deliverable rejected] {gap}")
            child.finish_snapshot()
    return {
        "label": label,
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


def _delegate(parent: Agent, args: dict) -> str:
    specs = args.get("tasks")
    if not isinstance(specs, list) or not specs:
        return "delegate_task 错误：tasks 必须是非空数组"
    specs = [dict(spec) for spec in specs]
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
        spec["task"] = (
            "读取 renders/contact-sheet.png、可用时的 contact-sheet-special.png、"
            "plan/deck.md 与 speech.md，按 Review 角色卡完成整册复审。"
            if parent.skill_language == "zh"
            else
            "Review the completed deck from renders/contact-sheet.png, the special "
            "contact sheet when present, plan/deck.md, and speech.md using the "
            "Review role card."
        )

    # A retry request may include the entire previous wave even when only one
    # child failed. Labels are stable task identities, so successful children
    # are durable within this parent run and must not be executed again. This
    # keeps retry semantics generic: the Harness does not need to know anything
    # about Slide groups or page types.
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
        review_needs_final_view = (
            str(spec.get("role", "")).lower() == "review"
            and (
                not bool(getattr(parent, "final_view_after_review", False))
                or not bool(getattr(parent, "finalize_succeeded", False))
            )
        )
        if (
            isinstance(previous, dict)
            and bool(previous.get("ok"))
            and not review_needs_final_view
        ):
            skipped_completed_labels.append(label)
        else:
            if (
                isinstance(previous, dict)
                and str(spec.get("role", "")).lower() == "slide"
                and isinstance(previous.get("incomplete_pages"), list)
                and previous.get("incomplete_pages")
            ):
                requested_pages = set(_spec_slide_pages(spec, 1))
                retry_pages = sorted(
                    requested_pages.intersection(
                        int(page)
                        for page in previous["incomplete_pages"]
                        if str(page).isdigit()
                    )
                )
                if retry_pages:
                    spec["pages"] = retry_pages
                    page_text = ",".join(f"{page:02d}" for page in retry_pages)
                    spec["task"] = (
                        str(spec.get("task") or "").rstrip()
                        + "\nRetry scope: only pages ["
                        + page_text
                        + "]. Other pages in this group already passed; do not edit them."
                    )
            pending_specs.append(spec)
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
    if roles.count("material") > 1:
        return (
            "Material 委派错误：每个 Deck 只使用一个 Material Agent。"
            "把全部附件路径合并到同一个 Material 任务，并统一写入 "
            "research/material.md。"
        )
    if "material" in roles and parent.material_completed:
        return (
            "Material 已完成：直接使用 research/material.md，"
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
    if "image" in roles and parent.image_completed:
        return "Image 已完成：直接使用 assets/catalog.md 与本地素材。"
    root = Path(parent.ws)
    grouped_skill = _is_grouped_skill_name(getattr(parent, "skill_name", ""))
    research_required = bool(
        getattr(parent, "research_required", not grouped_skill)
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
        if parent.image_required and not parent.image_completed:
            parent.log("顺序提醒：依赖配图的 Slide 应等待 Image；本次仍按任务执行。")
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
    if "review" in roles and not (root / "renders" / "contact-sheet.png").is_file():
        return "阶段顺序错误：Review 必须等待 Orchestrator 完成整册 render 与 contact-sheet。"
    if (
        "review" in roles
        and grouped_skill
        and not bool(getattr(parent, "finalize_attempted", False))
    ):
        return (
            "阶段顺序错误：Grouped Review 必须等待 Orchestrator 先成功调用或明确尝试 "
            "finalize；手工逐页 render/contact-sheet 不能替代最终质量门。"
        )
    parent.delegated_roles.extend(str(spec.get("role", "")).lower() for spec in specs)
    limit = _child_wave_limit(parent, len(specs))
    parent.log(f"并行委派 {len(specs)} 项，concurrency={limit}")
    results: list[dict] = [None] * len(specs)  # type: ignore[list-item]
    with futures.ThreadPoolExecutor(max_workers=limit) as pool:
        jobs = {pool.submit(_run_child, parent, i + 1, spec): i for i, spec in enumerate(specs)}
        for job in futures.as_completed(jobs):
            results[jobs[job]] = job.result()
    outcomes = existing_outcomes
    for result in results:
        label = str(result.get("label") or "")
        if label:
            outcomes[label] = {
                "role": result.get("role"),
                "ok": bool(result.get("ok")),
                "exit_reason": result.get("exit_reason"),
                "completed_pages": result.get("completed_pages", []),
                "incomplete_pages": result.get("incomplete_pages", []),
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
    # Child traces contain the full per-agent responses. Returning every Slide
    # summary here can exceed the tool-result cap on long decks; the truncated
    # result then makes the Orchestrator believe later pages never returned and
    # it delegates the whole deck again. Keep the control-plane result compact.
    all_succeeded = skipped_completed_labels + succeeded
    payload_record = {
        "status": "completed" if not failed else "completed_with_failures",
        "requested": requested_count,
        "executed": len(results),
        "succeeded": len(all_succeeded),
        "failed": len(failed),
        "succeeded_labels": all_succeeded,
        "skipped_completed_labels": skipped_completed_labels,
        "failures": failed,
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
        raise ValueError("page list contains duplicates")
    return pages


def _rendered_pages_from_command(command: str) -> set[int]:
    pages = {
        int(value)
        for value in re.findall(
            r"deck\.py\s+render\s+\.\s+--page\s+0*(\d+)",
            str(command or ""),
        )
    }
    group_match = re.search(
        r"deck\.py\s+render-group\s+\.\s+.*?--pages\s+([^\s]+)",
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
        return (
            "delegate_task 错误：tasks 必须是非空数组；请直接传数组，不要把数组再包成"
            "字符串。每页使用一个 `Slide NN:` 任务，不要把多页合并为 SlideGroup，"
            "也不要改由 Orchestrator 接管 Slide HTML。"
        )

    specs: list[dict] = []
    slide_numbers: list[int] = []
    slide_group_ids: list[str] = []
    grouped_skill = _is_grouped_skill_name(getattr(parent, "skill_name", ""))
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
        goal = task.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            return f"delegate_task 错误：task {index} 缺少 goal"
        group_match = re.match(
            r"^\s*slide\s*group\s+([a-z0-9][a-z0-9_-]*)\s*"
            r"\[\s*([0-9,\s-]+)\s*\]\s*:",
            goal,
            flags=re.IGNORECASE,
        )
        if group_match:
            return (
                f"delegate_task 错误：task {index} 使用了 SlideGroup。"
                "MURAL Presenter v0.2 与训练分布一致，只接受一页一个 `Slide NN:`；"
                "请把该组展开为独立页面任务，并在同一个并行 wave 中委派。"
            )
        slide_match = re.match(
            r"^\s*slide[_\s-]*0*(\d+)\s*:",
            goal,
            flags=re.IGNORECASE,
        )
        if slide_match:
            number = int(slide_match.group(1))
            if grouped_skill:
                return (
                    "delegate_task 错误：Grouped Skill 不接受 `Slide NN:`。"
                    "请复制 scaffold/group-waves 的完整页组并使用 "
                    "`SlideGroup GROUP [NN,NN]:`；即使该组只有一页也必须使用 "
                    "SlideGroup，不能绕过组内设计关系。"
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
            })
            continue
        role_match = re.match(
            r"^\s*(material|research|image|review)\s*:",
            goal,
            flags=re.IGNORECASE,
        )
        if not role_match:
            return (
                f"delegate_task 错误：task {index} 的 goal 必须以 "
                "Material:、Research:、Image:、Review: 或 Slide NN: 开头"
            )
        role = role_match.group(1).lower()
        task_text = goal.strip()
        if inline_image_skill and role == "image":
            return (
                "delegate_task 错误：inline-image 版本没有 Image 角色；"
                "将图片任务保留在对应 SlideGroup 中。"
            )
        if grouped_skill and role == "image":
            task_text = (
                "Image：根据 plan/deck.md 与正式逐页计划解析并交付已规划视觉素材"
                if delegated_language == "zh"
                else (
                    "Image: resolve the planned visual assets from plan/deck.md and "
                    "the canonical slide plans"
                )
            )
        elif grouped_skill and role == "review":
            task_text = (
                "Review：依据最终像素完成整册质量收口"
                if delegated_language == "zh"
                else "Review: close the completed deck from final pixels"
            )
        specs.append({
            "role": role,
            "label": role,
            "task": task_text,
        })
    if len(set(slide_numbers)) != len(slide_numbers):
        return "delegate_task 错误：同一批中不能重复委派同一页"
    if len(set(slide_group_ids)) != len(slide_group_ids):
        return "delegate_task 错误：同一批中不能重复使用同一个 SlideGroup ID"
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
        local_revision_reviewer = bool(
            agent.role == "orchestrator"
            and getattr(agent, "revision_mode", False)
            and _is_grouped_skill_name(agent.skill_name)
        )
        tool_succeeded = not any(
            marker in value_text
            for marker in ("错误：", "工具策略违规", "[exit_code=")
        )
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
            and re.search(r"deck\.py\s+sync-speech\b", command)
        ):
            agent.review_completed = False
            agent.final_render_after_review = False
            agent.finalize_succeeded = False
        if use.name == "terminal" and "deck.py finalize" in command:
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
            and "deck.py finalize" in command
        ):
            agent.final_render_after_review = True
        if (
            use.name == "vision_analyze"
            and agent.role == "review"
            and tool_succeeded
        ):
            agent.final_view_after_review = True
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
        if use.name == "vision_analyze" and tool_succeeded and agent.role == "slide":
            source_path = str(
                value.get("path", "") if isinstance(value, dict) else ""
            ) or str(args.get("image_url", ""))
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
                for page in tuple(getattr(agent, "assigned_slide_pages", ()) or ()):
                    digest = agent.rendered_output_hashes.get(page, "")
                    if digest:
                        agent.viewed_output_hashes[page] = digest
        if isinstance(value, dict) and "vision_analysis" in value:
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
                "source_path": value.get("path") or args.get("image_url"),
                "media_type": value.get("media_type", "image/jpeg"),
                "vision_backend": value.get("vision_backend", "external"),
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
                "source_path": value.get("path") or args.get("image_url"),
                "media_type": value.get("media_type", "image/png"),
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
    _write_json(agent.trace_dir / "messages.json", [agent.compact_message(m) for m in messages])
    _write_json(agent.trace_dir / "tool_log.json", tool_log)


def _file_content_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
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
    grouped = _is_grouped_skill_name(getattr(agent, "skill_name", ""))
    state_path = (
        root / "_trace" / "render-state.json"
        if grouped
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
        digest = hashlib.sha256((css + "\0" + fragment).encode("utf-8")).hexdigest()
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        if grouped:
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


def _slide_render_quality_actions(agent: Agent, page: int) -> int:
    """Return ACTIONs still owned by Slide before its pixel-state stop line."""
    if not _is_grouped_skill_name(getattr(agent, "skill_name", "")):
        return 0
    path = Path(agent.ws) / "_trace" / "render-state.json"
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
        pages = registry.get("pages") if isinstance(registry, dict) else None
        payload = pages.get(f"{page:02d}") if isinstance(pages, dict) else None
        value = payload.get("quality_action_count", 0) if isinstance(payload, dict) else 0
        hashes = payload.get("hashes", []) if isinstance(payload, dict) else []
        limit = payload.get("limit", 7) if isinstance(payload, dict) else 7
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

    agent.completed_slide_pages = tuple(completed)
    agent.incomplete_slide_pages = tuple(sorted(gaps))
    if not gaps:
        return ""
    detail = "；".join(
        f"P{page:02d}：{'、'.join(items)}"
        for page, items in sorted(gaps.items())
    )
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
        return _slide_deliverable_gap(agent)
    if agent.role != "orchestrator":
        return ""
    if agent.skill_language == "auto":
        return (
            "尚未选择说明版 Skill。请根据你最能可靠遵循的说明语言，"
            f"读取 `skills/{config.SKILL_NAME_ZH}/SKILL.md` 或 "
            f"`skills/{config.SKILL_NAME_EN}/SKILL.md` 之一；首次读取即锁定。"
        )
    if agent.material_required and not agent.material_completed:
        return (
            "本任务有附件，但 Material 尚未完成。请先委派 Material 整理附件，"
            "不要由编排器直接读取附件代替材料阶段。"
        )
    if agent.research_required and not agent.research_completed:
        return (
            "Research 尚未完成。请把完整 query 与 research/material.md 交给 Research，"
            "由 Research 一次写出 research/knowledge-brief.md 后再继续规划。"
        )
    page_plans = list((Path(agent.ws) / "plan").glob("slide_[0-9][0-9].md"))
    if len(page_plans) >= 2 and "slide" not in agent.delegated_roles:
        return (
            f"当前已有 {len(page_plans)} 个逐页计划，但尚未委派 Slide。"
            "上下文长度、成本或预算不是跳过并行页面阶段的理由；"
            "请委派全部页面，等待完成后再 finalize。"
        )
    if "slide" not in agent.delegated_roles:
        return ""
    planned_pages = sorted(
        int(path.stem.rsplit("_", 1)[-1])
        for path in page_plans
    )
    outcomes = getattr(agent, "child_outcomes", {})
    incomplete_pages = [
        page
        for page in planned_pages
        if not (
            isinstance(outcomes.get(f"slide_{page:02d}"), dict)
            and bool(outcomes[f"slide_{page:02d}"].get("ok"))
        )
    ]
    if incomplete_pages:
        labels = ", ".join(f"Slide {page:02d}:" for page in incomplete_pages)
        return (
            "以下单页任务尚未形成成功闭环："
            f"{labels}。只重新委派这些独立页面；不要合并为 SlideGroup，"
            "也不要让已成功页面从头重做。"
        )
    if not agent.review_completed:
        return (
            "Slide 阶段已发生，但 Review 尚未完成。先整册 render，再委派一次 review；"
            "Review 返回后才能进入最终交付。"
        )
    if not bool(getattr(agent, "finalize_attempted", False)):
        return (
            "尚未执行最终 finalize 质量门。运行 deck.py finalize；"
            "只有退出码为 0 才能交付。"
        )
    if not bool(getattr(agent, "finalize_succeeded", False)):
        failure = str(getattr(agent, "finalize_failure", "") or "")[-600:]
        detail = f"最近失败：{failure}。" if failure else ""
        return (
            "最近一次 finalize 仍是硬失败。Review ready、Vision pass 或 "
            "deck.py audit PASS 都不能覆盖该结论。"
            + detail
            + "重新委派 Review 修复质量门列出的问题；只有修改、新像素验证与新的成功 finalize 才能关闭。"
        )
    if not agent.final_view_after_review:
        return (
            "Review 尚未完成最终像素闭环。重新委派 Review：若改过页面则由 Review "
            "运行 finalize，并复看最后变更页的当前像素；Orchestrator 不重复看图。"
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
            "请先读取 SKILL.md 与 orchestrator 角色卡"
        )
    else:
        agent.log(
            f"Harness 沿用 {agent.skill_name}（{agent.skill_language}）"
            f"；请先读取 roles/{agent.role}.md"
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
            if _compact_live_history(
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
        # All image tool results already present in messages were consumed by
        # the response above. Release their base64 payloads before the next API
        # call while retaining the on-disk trace snapshots.
        _compact_consumed_images(messages[:-1])
        _compact_consumed_images(trace_messages[:-1])

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
                        f"P{page_text}；结束旧 Agent，等待 Image 后重新委派"
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
                    if _compact_live_history(
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
    script = root / "skills" / names[0] / "scripts" / "deck.py"
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
    script = root / "skills" / skill_name / "scripts" / "deck.py"
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


def _revision_prompt(
    seed: dict,
    revision: dict,
    *,
    grouped: bool = False,
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
    revision = seed.get("_revision") if isinstance(seed.get("_revision"), dict) else None
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
    grouped_revision = any(_is_grouped_skill_name(name) for name in exposed)
    query = (
        _revision_prompt(seed, revision, grouped=grouped_revision)
        if revision
        else str(seed.get("query", str(seed)))
    )
    staged_materials = [str(path) for path in seed.get("_staged_materials", [])]
    cfg["_staged_materials"] = staged_materials
    cfg["_revision_mode"] = bool(revision)
    agent = Agent(sample_id, workspace, query, cfg)
    agent.material_required = bool(staged_materials) and not revision
    agent.research_required = (
        not revision
        and not any(_is_grouped_skill_name(name) for name in exposed)
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
    loop_ok = False
    try:
        loop_ok = run_loop(agent)
    finally:
        agent.workspace_policy_violations = _workspace_output_violations(
            workspace,
            agent.skill_name,
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
        "quarantine"
        if nova_reports and not nova_ok
        else ("completed" if ok else ("failed" if operational_failure else "rejected"))
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
        "pid": os.getpid(),
    }
