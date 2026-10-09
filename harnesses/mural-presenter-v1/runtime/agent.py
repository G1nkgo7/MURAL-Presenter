"""运行 Provider 中立的多轮 Agent 与结构化工具闭环。

本模块只处理模型消息、工具事务、原生图片回灌和可恢复 segment。它不知道页面布局、
Owner 分组、审美标准或角色结束口令；这些语义全部由调用方提供的正式 Skill prompt 决定。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shlex
import threading
import time
from typing import Any, Callable, ContextManager, Mapping, Sequence

from .config import ProviderSettings, maintenance_reasoning, provider_request_budget_s
from .live_trace import iter_jsonl_events
from .language_policy import (
    append_language_reminder,
    extract_language_policy,
    language_drift_warning,
    visible_output_language_reminder,
)
from .request_labels import request_kind_public
from .prompt_visibility import model_visible_messages, terminal_visible_payload
from .recovery import (
    ProviderFailure,
    CompactionSummaryError,
    RecoveryBoundaryError,
    ResourceSlotTimeout,
    SegmentCheckpoint,
    compact_segment,
    context_compaction_summary,
    strip_signed_thinking,
    tool_boundary,
)
from .session import (
    ModelRequest,
    ModelSession,
    ProviderChannel,
    ProviderTransport,
    UsageLedger,
    estimate_request_tokens,
    request_context_budget,
)
from .tools import ToolCall, ToolContext, ToolResult, ToolRuntime, _is_native_inspection_tool
from .tool_batch import execute_tool_batch
from . import vision as vision_budget
from .vision import (
    image_source,
    next_turn_protected_image_count,
    normalize_images,
    opened_visual_sources,
    release_consumed_visual_context,
    visual_compaction_tail,
)


AgentEventSink = Callable[[Mapping[str, object]], None]
CheckpointSink = Callable[[SegmentCheckpoint], None]

# 同一 Agent 连续第 N 次提交机械指纹完全相同的失败工具调用时熔断，
# 不再空转到 max_turns。指纹 = agent_id + tool_name + canonical(arguments) + error_code。
REPEATED_TOOL_FAILURE_LIMIT = 3
# 粗粒度熔断阈值:同一 tool+error_code 连续失败(忽略 arguments 漂移)。
# 高于精确指纹阈值,兜住模型改写参数逃避精确指纹的退化循环。
COARSE_TOOL_FAILURE_LIMIT = 8
# 同一 completion gap / Owner 终态失败 fingerprint 连续出现后才中断。
# 第一次注入后再给 3 轮决策（共 4 次相同 fingerprint），避免一轮空决策就判死 attempt。
TERMINAL_FAILURE_IDLE_LIMIT = 4
COMPLETION_GAP_STALL_MESSAGE = (
    "owner_interrupted: identical completion gap remained unresolved "
    "after additional recovery decisions; checkpoint retained for artifact-first recovery"
)
OWNER_FAILURE_STALL_MESSAGE = (
    "owner_interrupted: identical terminal Owner failure remained unresolved "
    "after additional recovery decisions; checkpoint retained for targeted recovery"
)
# 截断、无动作、空回复与空参数工具调用共享一次纠正机会。成功的实际动作会重置预算，
# 但同一次退化不能通过切换错误形态叠加多套重试。
ACTION_RECOVERY_LIMIT = 1
# 真正的 thinking-only/普通空回复只需要短促执行窗口；大工具参数被截断时保留原预算。
NO_ACTION_RECOVERY_MAX_TOKENS = 4096
# 可纠正参数错误按模型收到反馈的回合累计，不按同轮批量调用数累计。
CORRECTABLE_ARGUMENT_ERRORS = frozenset({
    "repair_reason_required",
    "image_review_required",
    "missing_arguments", "invalid_arguments", "unknown_arguments", "terminal_argument_invalid",
})
# Scope errors also need feedback rounds, but an unrelated successful call does
# not prove that the denied action was corrected.
FEEDBACK_ROUND_TOOL_ERRORS = CORRECTABLE_ARGUMENT_ERRORS | frozenset({
    "write_scope_denied", "read_scope_denied", "terminal_role_denied",
    "tool_permission_denied", "delegation_rejected", "asset_owner_conflict", "asset_write_conflict",
})
PROMPT_INSPECTION_TEXT_LIMIT = 1200
PROMPT_CONTROL_RESULT_CHAR_LIMIT = 16 * 1024
PROMPT_TERMINAL_STDOUT_LIMIT = 6000
PROMPT_TERMINAL_STDERR_LIMIT = 1600

_INSPECTION_DESCRIPTOR_PROMPT_KEYS = (
    "evidence_key",
    "kind",
    "scope_key",
    "inspection_id",
    "path",
    "sha256",
    "review_stage",
    "members",
    "presentation_config",
    "owner_id",
    "role",
    "page",
    "page_uid",
    "html_sha256",
    "png_sha256",
    "reused_from_inspection_id",
)
_INSPECTION_PROMPT_KEYS = (
    "evidence_key",
    "visual_payload_status",
    "pixels_delivered_to_this_owner",
    "pixels_attached_in_this_result",
    "pixels_active_in_current_context",
    "asset_usage_status",
    "checked_scope",
    "consumer_pages",
    "cross_owner_review_reused",
    "inspection_id",
    "kind",
    "missing_required_assets",
    "next_required_action",
    "not_checked",
    "pages",
    "page",
    "path",
    "sha256",
    "png_sha256",
    "scope_key",
    "review_stage",
    "inspection_cached",
    "review_convergence",
    "native_multimodal",
    "pixel_unchanged",
    "repair_hint",
    "note",
    "diagnostics",
)
_INSPECTION_USEFUL_ANALYSIS_KEYS = (
    "summary",
    "observations",
    "issues",
    "warnings",
    "recommendations",
    "analysis",
    "answer",
    "result",
)
_CRITIC_FEEDBACK_PROMPT_KEYS = (
    "asset_usage_status",
    "completed",
    "checked_scope",
    "skipped",
    "skip_reason",
    "incomplete_reason",
    "body",
    "body_truncated_for_prompt",
    "body_char_count_full",
    "cache_hit",
    "auto_resolved_issue_ids",
    "missing_required_assets",
    "next_required_action",
    "not_checked",
    "reused_from",
    "status",
)


def _targeted_recovery_configuration(
    max_tokens: int, *, preserve_output_budget: bool = False, model: str = ""
) -> dict[str, object]:
    """Use the model's minimal reasoning mode for one targeted execution window."""
    thinking, effort = maintenance_reasoning(model)
    return {
        "thinking": thinking,
        "effort": effort,
        "max_tokens": max(
            1,
            int(max_tokens)
            if preserve_output_budget
            else min(int(max_tokens), NO_ACTION_RECOVERY_MAX_TOKENS),
        ),
    }


def _prompt_clip_value(value: object, *, limit: int = PROMPT_INSPECTION_TEXT_LIMIT) -> object:
    """Bound inspection text that is useful to the Owner but not worth carrying in full."""
    if isinstance(value, str):
        if len(value) <= limit:
            return value
        suffix = f"\n[truncated for prompt history; omitted {len(value) - limit} chars]"
        return value[:max(0, limit - len(suffix))].rstrip() + suffix
    if isinstance(value, Mapping):
        return {
            str(key): _prompt_clip_value(item, limit=limit)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_prompt_clip_value(item, limit=limit) for item in value[:12]]
    return copy.deepcopy(value)


def _prompt_json_chars(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _terminal_failure_idle_fingerprint(message: object) -> str:
    """Identify an unchanged terminal failure/completion gap after an Agent decision."""
    if not isinstance(message, Mapping) or message.get("role") != "user":
        return ""
    content = message.get("content")
    if not isinstance(content, str):
        return ""
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, Mapping):
        return ""
    if (payload.get("all_terminal") is not True
            or payload.get("events")
            or payload.get("pending_requests")):
        return ""
    event = payload.get("harness_event")
    if event == "orchestration_update":
        failures = payload.get("unresolved_failures")
        if not isinstance(failures, list) or not failures:
            return ""
        stable: object = failures
        kind = "owner_failure"
    elif event == "completion_gap" and payload.get("error_code"):
        # Recovery contains freshly generated task IDs, so it cannot participate in
        # equality. The actual gate, page/issue evidence and artifact fingerprints do.
        stable = {
            "error_code": payload.get("error_code"),
            "detail": payload.get("detail"),
            "diagnostics": _stable_completion_gap_value(payload.get("diagnostics")),
            "blocking_issues": _stable_completion_gap_value(
                payload.get("blocking_issues")
            ),
        }
        kind = "completion_gap"
    else:
        return ""
    encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{kind}:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _stable_completion_gap_value(value: object) -> object:
    """Remove volatile routing instructions while retaining completion evidence."""
    if isinstance(value, Mapping):
        return {
            str(key): _stable_completion_gap_value(item)
            for key, item in value.items()
            if key not in {"recovery", "submit_task", "task_id", "attempt_id"}
        }
    if isinstance(value, list):
        return [_stable_completion_gap_value(item) for item in value]
    return copy.deepcopy(value)


def _delegate_submit_prompt_payload(value: object) -> object:
    """Do not echo task goals and durable task internals after a successful submit."""
    if not isinstance(value, Mapping):
        return _prompt_clip_value(value)
    task_keys = (
        "task_id", "owner_id", "attempt", "role", "pages", "group_id",
        "dependencies", "status", "repair", "error", "output_path",
        "handoff_from", "asset_ids", "reference_pages",
    )
    raw_tasks = value.get("tasks") or []
    tasks = [
        {
            key: _prompt_clip_value(task[key], limit=800)
            for key in task_keys
            if isinstance(task, Mapping) and key in task and task[key] not in (None, "", [], {})
        }
        for task in raw_tasks
        if isinstance(task, Mapping)
    ] if isinstance(raw_tasks, list) else []
    top_keys = (
        "new_task_ids", "reused_task_ids", "reused_failed_task_ids", "replay_notice",
        "planned_page_count", "target_page_count", "page_coverage_source",
        "delegated_pages", "unassigned_pages", "undelegated_pages",
        "self_authored_pages", "parallelization_advisories",
        "knowledge_integration_advisories",
    )
    result = {
        key: _prompt_clip_value(value[key], limit=800)
        for key in top_keys if key in value and value[key] not in (None, "", [], {})
    }
    result["tasks"] = tasks
    if _prompt_json_chars(result) <= PROMPT_CONTROL_RESULT_CHAR_LIMIT:
        return result
    result["tasks"] = [
        {
            key: (_prompt_clip_value(task[key], limit=300) if key == "error" else task[key])
            for key in ("task_id", "role", "pages", "status", "output_path", "error")
            if key in task
        }
        for task in tasks[:32]
    ]
    if len(tasks) > 32:
        result["task_receipts_omitted"] = len(tasks) - 32
    if _prompt_json_chars(result) > PROMPT_CONTROL_RESULT_CHAR_LIMIT:
        coverage_keys = (
            "planned_page_count", "target_page_count", "page_coverage_source",
            "delegated_pages", "unassigned_pages", "undelegated_pages",
            "self_authored_pages", "replay_notice",
        )
        result = {
            key: _prompt_clip_value(value[key], limit=500)
            for key in coverage_keys if key in value and value[key] not in (None, "", [], {})
        }
        result["tasks"] = [
            {key: task[key] for key in ("task_id", "role", "pages", "status") if key in task}
            for task in tasks[:24]
        ]
        if len(tasks) > 24:
            result["task_receipts_omitted"] = len(tasks) - 24
    return result


def _terminal_diagnostics_prompt_view(value: object) -> object:
    if not isinstance(value, Mapping):
        return _prompt_clip_value(value, limit=800)
    keys = (
        "termination", "returncode", "truncated", "omission_reasons",
        "excerpt_truncated", "summary_records_omitted", "signal",
    )
    result = {
        key: _prompt_clip_value(value[key], limit=600)
        for key in keys if key in value and value[key] not in (None, "", [], {})
    }
    records = value.get("records")
    if isinstance(records, list) and records:
        result["records"] = [
            _prompt_clip_value(record, limit=600) for record in records[:8]
        ]
        if len(records) > 8:
            result["records_omitted"] = len(records) - 8
    return result


def _terminal_prompt_payload(value: object) -> object:
    """Keep one useful representation of terminal output; the full event stays in trace."""
    value = terminal_visible_payload(value)
    if not isinstance(value, Mapping):
        return _prompt_clip_value(value, limit=PROMPT_TERMINAL_STDOUT_LIMIT)
    special = {"stdout", "stderr", "diagnostics", "directories", "matches"}
    result = {
        str(key): _prompt_clip_value(item, limit=800)
        for key, item in value.items() if key not in special
    }
    raw_matches = value.get("matches")
    if isinstance(raw_matches, list):
        result["matches"] = [
            {
                key: _prompt_clip_value(match[key], limit=600)
                for key in ("path", "line", "text", "line_truncated")
                if isinstance(match, Mapping) and key in match
            }
            for match in raw_matches[:48]
            if isinstance(match, Mapping)
        ]
        if len(raw_matches) > 48:
            result["matches_omitted"] = len(raw_matches) - 48
        # stdout is only a second rendering of these structured matches.
    else:
        stdout = str(value.get("stdout") or "")
        if stdout:
            result["stdout"] = _prompt_clip_value(
                stdout, limit=PROMPT_TERMINAL_STDOUT_LIMIT
            )
    raw_directories = value.get("directories")
    if isinstance(raw_directories, list):
        directory_keys = (
            "path", "returncode", "error_code", "error", "returned_count",
            "truncated", "command_index", "stderr_suppressed",
        )
        result["directories"] = [
            {
                key: _prompt_clip_value(row[key], limit=600)
                for key in directory_keys
                if isinstance(row, Mapping) and key in row and row[key] not in (None, "", [], {})
            }
            for row in raw_directories[:24]
            if isinstance(row, Mapping)
        ]
        if len(raw_directories) > 24:
            result["directories_omitted"] = len(raw_directories) - 24
    stderr = str(value.get("stderr") or "")
    if stderr:
        result["stderr"] = _prompt_clip_value(
            stderr, limit=PROMPT_TERMINAL_STDERR_LIMIT
        )
    diagnostics = _terminal_diagnostics_prompt_view(value.get("diagnostics"))
    if diagnostics not in ({}, "", None):
        result["diagnostics"] = diagnostics
    if _prompt_json_chars(result) <= PROMPT_CONTROL_RESULT_CHAR_LIMIT:
        return result
    if isinstance(result.get("matches"), list):
        result["matches"] = result["matches"][:24]
        result["matches_omitted"] = max(
            int(result.get("matches_omitted") or 0),
            max(0, len(raw_matches) - 24) if isinstance(raw_matches, list) else 0,
        )
    if isinstance(result.get("directories"), list):
        result["directories"] = result["directories"][:12]
        result["directories_omitted"] = max(
            int(result.get("directories_omitted") or 0),
            max(0, len(raw_directories) - 12) if isinstance(raw_directories, list) else 0,
        )
    if isinstance(result.get("images"), list):
        images = result["images"]
        result["images"] = images[:6]
        if len(images) > 6:
            result["images_omitted"] = len(images) - 6
    if isinstance(result.get("stdout"), str):
        result["stdout"] = _prompt_clip_value(result["stdout"], limit=3000)
    if isinstance(result.get("diagnostics"), Mapping):
        result["diagnostics"] = {
            key: result["diagnostics"][key]
            for key in ("termination", "returncode", "truncated", "signal")
            if key in result["diagnostics"]
        }
    if _prompt_json_chars(result) > PROMPT_CONTROL_RESULT_CHAR_LIMIT:
        fallback = {
            key: _prompt_clip_value(value[key], limit=500)
            for key in (
                "returncode", "status", "path", "returned_count", "truncated",
                "error_code", "error", "mode", "pattern", "note",
            )
            if key in value and value[key] not in (None, "", [], {})
        }
        if isinstance(result.get("matches"), list):
            fallback["matches"] = [
                {
                    key: _prompt_clip_value(match[key], limit=300)
                    for key in ("path", "line", "text", "line_truncated") if key in match
                }
                for match in result["matches"][:12]
            ]
            fallback["matches_omitted"] = max(0, len(raw_matches) - 12) if isinstance(raw_matches, list) else 0
        if isinstance(result.get("directories"), list):
            fallback["directories"] = result["directories"][:8]
            fallback["directories_omitted"] = max(0, len(raw_directories) - 8) if isinstance(raw_directories, list) else 0
        raw_images = value.get("images")
        if isinstance(raw_images, list):
            fallback["images"] = [
                {
                    key: _prompt_clip_value(image[key], limit=300)
                    for key in ("artifact_id", "path", "status", "reason")
                    if isinstance(image, Mapping) and key in image and image[key] not in (None, "")
                }
                for image in raw_images[:4]
                if isinstance(image, Mapping)
            ]
            fallback["image_catalog_path"] = "assets/catalog.md"
            if len(raw_images) > 4:
                fallback["images_omitted"] = len(raw_images) - 4
        stdout = str(value.get("stdout") or "")
        stderr = str(value.get("stderr") or "")
        if stdout and not isinstance(raw_matches, list):
            fallback["stdout"] = _prompt_clip_value(stdout, limit=2000)
        if stderr:
            fallback["stderr"] = _prompt_clip_value(stderr, limit=800)
        result = fallback
    return result


def _slim_inspection_descriptor(value: object) -> dict[str, object]:
    """Keep only transaction fields needed for inspection replay/commit."""
    if not isinstance(value, Mapping):
        return {}
    result = {
        key: copy.deepcopy(value[key])
        for key in _INSPECTION_DESCRIPTOR_PROMPT_KEYS
        if key in value
    }
    return result


def _slim_critic_feedback(value: object) -> dict[str, object]:
    """Return only the Critic judgement useful for the next Owner action."""
    if not isinstance(value, Mapping):
        return {}
    compact = {
        key: _prompt_clip_value(value[key], limit=900)
        for key in _CRITIC_FEEDBACK_PROMPT_KEYS
        if key in value
    }
    body = str(compact.get("body") or "").strip()
    if compact.get("skipped"):
        compact.setdefault("body", "")
    elif not body and compact.get("completed"):
        compact["body"] = "未返回可见阻塞问题。"
    return compact


def _inspection_prompt_status(payload: Mapping[str, object], feedback: Mapping[str, object]) -> str:
    if payload.get("render_ok") is False or payload.get("diagnostics"):
        return "inspect_failed"
    if feedback:
        if feedback.get("skipped"):
            return "critic_skipped"
        if feedback.get("completed") is False or feedback.get("incomplete_reason"):
            return "critic_incomplete"
        if feedback.get("status") == "pass_no_blockers":
            return "pass_no_blockers"
        if str(feedback.get("body") or "").strip():
            return "critic_feedback_available"
        if feedback.get("completed"):
            return "pass_no_blockers"
    if payload.get("inspection_cached"):
        return "cached_current_inspection"
    if payload.get("native_multimodal"):
        return "pixels_attached_for_owner_review"
    return "ok"


def _prompt_safe_inspection_payload(payload: Mapping[str, object]) -> dict[str, object]:
    """Project full inspect output to the short receipt kept in Owner prompt history."""
    descriptor_key = "pending_inspection" if isinstance(payload.get("pending_inspection"), Mapping) else (
        "current_inspection" if isinstance(payload.get("current_inspection"), Mapping) else ""
    )
    descriptor = _slim_inspection_descriptor(payload.get(descriptor_key)) if descriptor_key else {}
    feedback = _slim_critic_feedback(payload.get("critic_feedback"))
    result: dict[str, object] = {
        key: copy.deepcopy(payload[key])
        for key in _INSPECTION_PROMPT_KEYS
        if key in payload
    }
    if descriptor_key:
        result[descriptor_key] = descriptor
        result.setdefault("kind", descriptor.get("kind"))
        result.setdefault("path", descriptor.get("path"))
        result.setdefault("sha256", descriptor.get("sha256"))
        result.setdefault("scope_key", descriptor.get("scope_key"))
        result.setdefault("inspection_id", descriptor.get("inspection_id"))
        result.setdefault("review_stage", descriptor.get("review_stage"))
    if feedback:
        result["critic_feedback"] = feedback
    result["status"] = _inspection_prompt_status(payload, feedback)
    for key in _INSPECTION_USEFUL_ANALYSIS_KEYS:
        if key in payload:
            result[key] = _prompt_clip_value(payload[key])
    return {key: value for key, value in result.items() if value not in (None, "", [], {})}


# 成功工具也可能空转：反复写入已见过的内容，或重复请求同一缓存证据。
# 新内容、新查询或新抓取重置相应计数；普通读取/看图不算进展。合法回退后继续精修不受影响。
NO_PROGRESS_LIMIT = 8


class AgentRuntimeError(RuntimeError):
    """表示 Agent 无法在声明的机械边界内继续运行。"""


class AgentContextWindowError(AgentRuntimeError):
    """表示历史即使在有界 compact 后仍超出模型上下文窗口。"""


class AgentProtocolError(AgentRuntimeError):
    """表示 Provider 返回的消息不满足闭合 Agent 协议。"""


class AgentProviderError(AgentRuntimeError):
    """表示 Provider 重试和安全 rollover 后仍未得到响应。"""

    def __init__(self, detail: str, *, failure: ProviderFailure | None = None, retry_after_s: float = 0) -> None:
        self.failure = failure
        self.retry_after_s = retry_after_s
        super().__init__(detail)


class AgentTurnLimitError(AgentRuntimeError):
    """表示 Agent 在最大模型回合数内没有自然结束。"""


class AgentInterruptedError(AgentRuntimeError, InterruptedError):
    """表示 Owner 已被调度器中断，闭合当前工具批次后停止请求模型。"""


class RepeatedToolFailureError(AgentRuntimeError):
    """表示 Agent 反复提交同一个失败的工具调用，触发熔断。"""

    def __init__(
        self,
        detail: str,
        *,
        tool_name: str,
        error_code: str,
        arguments_summary: str,
        repeat_count: int,
        diagnostics: Mapping[str, object] | None = None,
    ) -> None:
        """保存熔断时最后一次失败调用的机械指纹，供上层错误面板使用。"""
        self.tool_name = str(tool_name)
        self.error_code = str(error_code)
        self.arguments_summary = str(arguments_summary)
        self.repeat_count = int(repeat_count)
        self.diagnostics = dict(diagnostics or {})
        super().__init__(detail)


@dataclass
class _ToolProgress:
    """集中记录工具失败与产物进展；不写 Trace，不修改 checkpoint 或修复预算。"""

    failure_key: tuple[str, str, str] | None = None
    failed_call: ToolCall | None = None
    exact_count: int = 0
    coarse_count: int = 0
    failed_ids: list[str] = field(default_factory=list)
    recent_ids: list[str] = field(default_factory=list)
    write_path: str = ""
    write_tool: str = ""
    write_repeats: int = 0
    seen_writes: set[str] = field(default_factory=set)
    evidence_key: str = ""
    evidence_repeats: int = 0
    seen_evidence: set[str] = field(default_factory=set)
    evidence_warned: bool = False

    def rebase_evidence(self, messages: Sequence[Mapping[str, Any]]) -> None:
        """只将当前输入仍含完整回执的内容算作已见；不清除真实工具失败或重复写入计数。"""
        calls = {}
        visible = set()
        for message in messages:
            content = message.get("content")
            if not isinstance(content, (list, tuple)):
                continue
            for block in content:
                if not isinstance(block, Mapping):
                    continue
                if block.get("type") == "tool_use":
                    call = ToolCall(str(block.get("id") or ""), str(block.get("name") or ""),
                                    dict(block.get("input") or {}))
                    calls[call.tool_use_id] = call
                elif block.get("type") == "tool_result" and not block.get("is_error"):
                    call = calls.get(str(block.get("tool_use_id") or ""))
                    if call is not None:
                        evidence = _repeatable_evidence(call, block.get("content"))
                        if evidence is not None:
                            visible.add(evidence[0])
        self.seen_evidence = visible
        if self.evidence_key not in visible:
            self.evidence_key = ""
            self.evidence_repeats, self.evidence_warned = 0, False

    def observe(
        self, call: ToolCall, result: ToolResult,
        *, argument_failures: set[tuple[str, str]] | None = None,
        repeated_evidence: set[str] | None = None,
    ) -> None:
        self.recent_ids = (self.recent_ids + [call.tool_use_id])[-COARSE_TOOL_FAILURE_LIMIT:]
        if not result.ok:
            key = (call.name, result.error_code, _canonical_arguments(call.arguments))
            if result.error_code in FEEDBACK_ROUND_TOOL_ERRORS and argument_failures is not None:
                if key[:2] in argument_failures:
                    return
                argument_failures.add(key[:2])
            self.exact_count = self.exact_count + 1 if key == self.failure_key else 1
            if self.failure_key is not None and key[:2] == self.failure_key[:2]:
                self.coarse_count += 1
            else:
                self.coarse_count, self.failed_ids = 1, []
            self.failure_key, self.failed_call = key, call
            self.failed_ids = (self.failed_ids + [call.tool_use_id])[-COARSE_TOOL_FAILURE_LIMIT:]
            return
        argument_failure = self.failure_key is not None and self.failure_key[1] in CORRECTABLE_ARGUMENT_ERRORS
        resolved_failure = self.failure_key is not None and (
            call.name == self.failure_key[0]
            and (argument_failure or _canonical_arguments(call.arguments) == self.failure_key[2])
        )
        made_progress = False
        if call.name in {"patch", "write_file"}:
            self.write_tool = call.name
            path, digest = _patched_path(result, call), _patched_sha256(result)
            if path != self.write_path or not path:
                self.write_path, self.seen_writes, self.write_repeats = path, set(), 0
            if digest and digest in self.seen_writes:
                self.write_repeats += 1
            else:
                self.write_repeats = 0
                if digest:
                    self.seen_writes.add(digest)
                    made_progress = True
                    self.evidence_repeats, self.evidence_warned = 0, False
        else:
            evidence = _repeatable_evidence(call, result.content)
            if evidence is not None:
                fingerprint, reusable = evidence
                made_progress = fingerprint not in self.seen_evidence
                if reusable and fingerprint in self.seen_evidence:
                    if repeated_evidence is None or not repeated_evidence:
                        self.evidence_repeats += 1
                    if repeated_evidence is not None:
                        repeated_evidence.add(fingerprint)
                else:
                    self.evidence_repeats, self.evidence_warned = 0, False
                self.seen_evidence.add(fingerprint)
                self.evidence_key = fingerprint
        # 无关成功或旧证据不等于失败已解决；首次诊断和真实写入仍能开始下一次修复。
        if resolved_failure or (made_progress and not argument_failure):
            self.failure_key, self.failed_call = None, None
            self.exact_count = self.coarse_count = 0
            self.failed_ids = []

    def check(self, agent_id: str, calls: Sequence[ToolCall]) -> bool:
        """闭合工具批次后检查；True 表示首次证据重复警告，原阈值与优先级不变。"""
        if self.exact_count >= REPEATED_TOOL_FAILURE_LIMIT or self.coarse_count >= COARSE_TOOL_FAILURE_LIMIT:
            tool_name, error_code, _ = self.failure_key
            exact = self.exact_count >= REPEATED_TOOL_FAILURE_LIMIT
            count = self.exact_count if exact else self.coarse_count
            threshold = REPEATED_TOOL_FAILURE_LIMIT if exact else COARSE_TOOL_FAILURE_LIMIT
            trigger = "exact_repeat" if exact else "coarse_repeat"
            count_unit = "feedback_rounds" if error_code in FEEDBACK_ROUND_TOOL_ERRORS else "calls"
            raise RepeatedToolFailureError(
                f"Agent {agent_id!r}: {trigger} circuit opened after {count}/{threshold} "
                f"failing {tool_name!r} {count_unit} (error_code={error_code!r}; "
                f"exact_count={self.exact_count}, coarse_count={self.coarse_count})",
                tool_name=tool_name, error_code=error_code,
                arguments_summary=_arguments_summary(self.failed_call.arguments), repeat_count=count,
                diagnostics={"trigger": trigger, "trigger_count": count, "threshold": threshold,
                             "exact_count": self.exact_count, "coarse_count": self.coarse_count,
                             "count_unit": count_unit,
                             "related_tool_use_ids": self.failed_ids[-count:]})
        if self.write_repeats >= NO_PROGRESS_LIMIT:
            raise RepeatedToolFailureError(
                f"Agent {agent_id!r} rewrote {self.write_path!r} {self.write_repeats} times without net progress "
                "(content revisited previously seen states)",
                tool_name=self.write_tool,
                error_code="no_progress_patch_loop" if self.write_tool == "patch" else "no_progress_write_loop",
                arguments_summary=f"path={self.write_path}", repeat_count=self.write_repeats,
                diagnostics={"trigger": "no_progress", "trigger_count": self.write_repeats,
                             "threshold": NO_PROGRESS_LIMIT, "related_tool_use_ids": self.recent_ids})
        tool_name, _, digest = self.evidence_key.partition(":")
        if self.evidence_repeats >= NO_PROGRESS_LIMIT and any(call.name == tool_name for call in calls):
            if not self.evidence_warned:
                self.evidence_warned = True
                return True
            raise RepeatedToolFailureError(
                f"Agent {agent_id!r} requested already-seen evidence "
                f"{self.evidence_repeats} times without new evidence or artifact progress after a progress warning. "
                "Use the returned evidence to perform the next required workflow action "
                "or report a specific unresolved blocker.",
                tool_name=tool_name,
                error_code=("no_progress_directory_loop" if tool_name == "terminal" and digest.startswith("directory:")
                            else "no_progress_cached_web_loop" if tool_name in {"web_search", "web_extract"}
                            else "no_progress_file_read_loop" if tool_name == "read_file"
                            else "no_progress_render_loop" if tool_name in {"render", "terminal"}
                            else "no_progress_visual_evidence_loop"),
                arguments_summary=f"evidence_sha256={digest}", repeat_count=self.evidence_repeats,
                diagnostics={"trigger": "no_progress", "trigger_count": self.evidence_repeats,
                             "threshold": NO_PROGRESS_LIMIT, "related_tool_use_ids": self.recent_ids})
        return False


@dataclass(frozen=True)
class AgentRunResult:
    """保存一次角色运行的最终文本、闭合 segment 与工具统计。"""

    agent_id: str
    role: str
    final_text: str
    stop_reason: str
    turns: int
    tool_calls: int
    checkpoint: SegmentCheckpoint
    segments: tuple[SegmentCheckpoint, ...]
    vision_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """冻结可变的 segment 和图片路径集合。"""
        object.__setattr__(self, "segments", tuple(self.segments))
        object.__setattr__(
            self,
            "vision_paths",
            tuple(dict.fromkeys(str(path) for path in self.vision_paths)),
        )

    def to_dict(self) -> dict[str, object]:
        """返回不复制图片字节的运行摘要。"""
        return {
            "agent_id": self.agent_id,
            "role": self.role,
            "final_text": self.final_text,
            "stop_reason": self.stop_reason,
            "turns": self.turns,
            "tool_calls": self.tool_calls,
            "segment_ids": [segment.segment_id for segment in self.segments],
            "vision_paths": list(self.vision_paths),
        }


class AgentRuntime:
    """使用同一个 ``ModelSession`` 状态机运行任意正式角色。"""

    def __init__(
        self,
        *,
        settings: ProviderSettings,
        channels: Sequence[ProviderChannel],
        transport: ProviderTransport,
        tools: ToolRuntime,
        usage: UsageLedger | None = None,
        event_sink: AgentEventSink | None = None,
        slot_factory: Callable[..., ContextManager[object]] | None = None,
        balance: str = "balanced",
        context_window_limit: int | None = None,
        compact_trigger_ratio: float = 0.90,
        compact_target_tokens: int = 131072,
        compact_summary_target_tokens: int = 8192,
        max_context_images: int = vision_budget.MAX_CONTEXT_IMAGES,
        summary_invoker: Callable | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        """绑定共享 Provider、工具和观测依赖，不保存任何角色语义。"""
        if not channels:
            raise ValueError("AgentRuntime requires at least one Provider Channel")
        if context_window_limit is not None and context_window_limit <= 0:
            raise ValueError("context_window_limit must be positive")
        self.settings = settings
        self.training_sample_max_tokens = context_window_limit
        self.context_window = (min(settings.context_window, context_window_limit)
                               if context_window_limit is not None else settings.context_window)
        self.channels = tuple(channels)
        self.transport = transport
        self.tools = tools
        self.usage = usage or UsageLedger()
        self.event_sink = event_sink
        self.slot_factory = slot_factory
        self.balance = str(balance)
        if not 0 < compact_trigger_ratio < 1:
            raise ValueError("compact_trigger_ratio must be between 0 and 1")
        if not 0 < compact_summary_target_tokens < compact_target_tokens:
            raise ValueError("compaction targets must be positive; summary target must be below total input target")
        if not 0 < int(max_context_images) <= vision_budget.MAX_CONTEXT_IMAGES:
            raise ValueError(
                f"max_context_images must be between 1 and {vision_budget.MAX_CONTEXT_IMAGES}"
            )
        self.compact_trigger_ratio = compact_trigger_ratio
        self.compact_target_tokens = compact_target_tokens
        self.compact_summary_target_tokens = compact_summary_target_tokens
        self.max_context_images = int(max_context_images)
        self.summary_invoker = summary_invoker
        self.environment = dict(environment) if environment is not None else None
        self._trace_local = threading.local()

    def run(
        self,
        *,
        agent_id: str,
        role: str,
        system: str,
        tool_context: ToolContext,
        initial_messages: Sequence[Mapping[str, Any]] = (),
        checkpoint: SegmentCheckpoint | None = None,
        max_turns: int,
        max_tokens: int,
        thinking: bool,
        effort: str,
        request_kind: str = "hermes_main",
        checkpoint_sink: CheckpointSink | None = None,
        continuation_context: Callable[[], Mapping[str, Any]] | None = None,
        idle_handler: Callable[[], Mapping[str, Any] | None] | None = None,
        stop_event: threading.Event | None = None,
    ) -> AgentRunResult:
        """运行一个角色，并把所有未正常返回的路径闭合为 ``agent_failed``。"""
        previous_trace = {
            name: getattr(self._trace_local, name)
            for name in ("turn", "segment_id") if hasattr(self._trace_local, name)
        }
        try:
            return self._run(
                agent_id=agent_id,
                role=role,
                system=system,
                tool_context=tool_context,
                initial_messages=initial_messages,
                checkpoint=checkpoint,
                max_turns=max_turns,
                max_tokens=max_tokens,
                thinking=thinking,
                effort=effort,
                request_kind=request_kind,
                checkpoint_sink=checkpoint_sink,
                continuation_context=continuation_context,
                idle_handler=idle_handler,
                stop_event=stop_event,
            )
        except Exception as exc:
            failure_fields: dict[str, object] = {
                "error_type": type(exc).__name__,
                "error_summary": str(exc)[:2000],
            }
            if isinstance(exc, RepeatedToolFailureError):
                failure_fields["last_failed_tool"] = exc.tool_name
                failure_fields["last_failed_error_code"] = exc.error_code
                failure_fields["arguments_summary"] = exc.arguments_summary
                failure_fields["repeat_count"] = exc.repeat_count
                failure_fields.update(exc.diagnostics)
            self._emit(
                str(agent_id or "unknown-agent"),
                str(role or "unknown").lower(),
                "agent_failed",
                turn=int(getattr(self._trace_local, "turn", 0) or 0),
                segment_id=str(
                    getattr(self._trace_local, "segment_id", "") or ""
                ),
                **failure_fields,
            )
            raise
        finally:
            for name in ("turn", "segment_id"):
                if name in previous_trace:
                    setattr(self._trace_local, name, previous_trace[name])
                elif hasattr(self._trace_local, name):
                    delattr(self._trace_local, name)

    def _run(
        self,
        *,
        agent_id: str,
        role: str,
        system: str,
        tool_context: ToolContext,
        initial_messages: Sequence[Mapping[str, Any]] = (),
        checkpoint: SegmentCheckpoint | None = None,
        max_turns: int,
        max_tokens: int,
        thinking: bool,
        effort: str,
        request_kind: str = "hermes_main",
        checkpoint_sink: CheckpointSink | None = None,
        continuation_context: Callable[[], Mapping[str, Any]] | None = None,
        idle_handler: Callable[[], Mapping[str, Any] | None] | None = None,
        stop_event: threading.Event | None = None,
    ) -> AgentRunResult:
        """运行到模型自然结束，并在每个闭合工具边界发布 checkpoint。"""
        normalized_agent = str(agent_id).strip()
        normalized_role = str(role).strip().lower()
        if not normalized_agent or not normalized_role:
            raise ValueError("agent_id and role must not be empty")
        if tool_context.owner_id != normalized_agent:
            raise ValueError("ToolContext owner_id must match agent_id")
        if tool_context.role.strip().lower() != normalized_role:
            raise ValueError("ToolContext role must match Agent role")
        if not str(system).strip():
            raise ValueError("Agent system prompt must not be empty")
        if int(max_turns) < 1 or int(max_tokens) < 1:
            raise ValueError("Agent turn and token limits must be positive")
        if checkpoint is not None and initial_messages:
            raise ValueError("provide either checkpoint or initial_messages, not both")

        active = checkpoint or SegmentCheckpoint.initial(initial_messages)
        boundary = tool_boundary(active.messages)
        if not boundary.is_closed:
            raise AgentProtocolError(
                "Agent must start from the last closed tool transaction"
            )
        messages = copy.deepcopy(list(active.messages))
        normalize_images(messages)
        logical_turn = _assistant_message_count(messages)
        trace_context: dict[str, object] = {
            "turn": logical_turn + 1,
            "segment_id": active.segment_id,
        }
        self._trace_local.turn = logical_turn
        self._trace_local.segment_id = active.segment_id
        provenance = tool_context.metadata.get("provenance") or {}
        run_identity = ((provenance.get("deck_id") if isinstance(provenance, Mapping) else "")
                        or tool_context.metadata.get("workspace_root"))
        session = ModelSession(
            settings=self.settings,
            environment=self.environment,
            channels=self.channels,
            transport=self.transport,
            checkpoint=active,
            event_sink=lambda event: self._emit(
                normalized_agent,
                normalized_role,
                str(event.get("event_type") or "provider_event"),
                **{
                    **{
                        str(key): copy.deepcopy(value)
                        for key, value in event.items()
                        if key != "event_type"
                    },
                    "turn": int(trace_context["turn"]),
                    "segment_id": str(trace_context["segment_id"]),
                },
            ),
            usage=self.usage,
            balance=self.balance,
            slot_factory=self.slot_factory,
            trace_root=tool_context.metadata.get("workspace_root"),
            stop_event=stop_event,
            cache_identity=json.dumps([str(run_identity), normalized_agent]) if run_identity else "",
        )
        segments: dict[str, SegmentCheckpoint] = {}
        vision_paths: list[str] = []
        opened_sources = opened_visual_sources(messages, owner_id=normalized_agent)
        pending_inspections: list[Mapping[str, object]] = []
        for tool_name, descriptor, pending in _image_receipts(messages):
            relative = str(descriptor.get("path") or "")
            if relative:
                vision_paths.append(relative)
            if pending:
                pending_inspections.append(descriptor)
        tool_call_count = 0
        turns = 0
        rollover_count = 0
        preferred_channel_id = ""
        request_deadline: float | None = None
        maintenance_thinking, maintenance_effort = maintenance_reasoning(self.settings.model)
        recovery_thinking_notice = (
            "下一轮使用 low 思考档位" if maintenance_thinking else "下一轮关闭 thinking"
        )
        progress = _ToolProgress()
        progress.rebase_evidence(messages)
        action_recovery_attempts = 0
        no_action_recovery_override: dict[str, object] | None = None
        terminal_failure_fingerprint = ""
        terminal_failure_repeats = 0

        def accept_idle_input(event_input: Mapping[str, object], *, language_drift: bool) -> None:
            nonlocal terminal_failure_fingerprint, terminal_failure_repeats
            fingerprint = _terminal_failure_idle_fingerprint(event_input)
            if fingerprint:
                if fingerprint == terminal_failure_fingerprint:
                    terminal_failure_repeats += 1
                else:
                    terminal_failure_fingerprint = fingerprint
                    terminal_failure_repeats = 1
                if terminal_failure_repeats >= TERMINAL_FAILURE_IDLE_LIMIT:
                    stall_kind = fingerprint.split(":", 1)[0]
                    self._emit(
                        normalized_agent, normalized_role, "agent_orchestration_stalled",
                        turn=logical_turn, segment_id=session.checkpoint.segment_id,
                        recoverable=True, repeat_count=terminal_failure_repeats,
                        recovery_action=("artifact_first_completion_recovery"
                            if stall_kind == "completion_gap" else "resolve_or_redelegate_failed_owner"),
                    )
                    if stall_kind == "completion_gap":
                        raise AgentInterruptedError(COMPLETION_GAP_STALL_MESSAGE)
                    raise AgentInterruptedError(OWNER_FAILURE_STALL_MESSAGE)
            else:
                terminal_failure_fingerprint = ""
                terminal_failure_repeats = 0
            messages.append(copy.deepcopy(dict(event_input)))
            language_reminder = visible_output_language_reminder(
                tool_context.metadata, trigger="orchestration_update",
            )
            append_language_reminder(messages, language_reminder, force=language_drift)
            session.checkpoint = _checkpoint_with_messages(session.checkpoint, messages)
            self._publish_checkpoint(session.checkpoint, segments, checkpoint_sink)

        completed_tool_use_ids = _recorded_tool_use_ids(self.tools, tool_context) | _tool_use_ids(messages)
        known_tool_use_ids = set(completed_tool_use_ids)
        self._emit(
            normalized_agent,
            normalized_role,
            "agent_started",
            turn=logical_turn,
            segment_id=active.segment_id,
            attempt=int(tool_context.metadata.get("attempt") or 1),
            task_id=str(tool_context.metadata.get("task_id") or ""),
            wave_id=str(tool_context.metadata.get("wave_id") or ""),
            group_id=str(tool_context.metadata.get("group_id") or ""),
            pages=list(tool_context.metadata.get("pages") or []),
        )

        context_segments: set[str] = set()
        while turns < int(max_turns):
            if stop_event is not None and stop_event.is_set():
                self._publish_checkpoint(_checkpoint_with_messages(session.checkpoint, messages), segments, checkpoint_sink)
                raise AgentInterruptedError("owner_interrupted: stopped at a closed tool boundary; resume this workspace")
            trace_context["turn"] = logical_turn + 1
            trace_context["segment_id"] = session.checkpoint.segment_id
            advertised_tools = tuple(self.tools.advertised_tools(normalized_role))
            # Slide 的高强度 thinking 只用于首轮理解 brief/规划。完成任一真实工具轮后
            # 进入执行态；medium/low 等既有配置不变，避免无证据地改变其他模型配方。
            slide_execution_turn = (
                normalized_role == "slide"
                and bool(thinking)
                and str(effort).lower() in {"high", "xhigh", "max"}
                and tool_call_count > 0
            )
            configured_turn_thinking = bool(thinking and not slide_execution_turn)
            configured_turn_effort = str("" if slide_execution_turn else effort)
            effective_thinking = bool(
                no_action_recovery_override.get("thinking")
                if no_action_recovery_override is not None
                else configured_turn_thinking
            )
            effective_effort = str(
                no_action_recovery_override.get("effort")
                if no_action_recovery_override is not None
                else configured_turn_effort
            )
            if not effective_thinking and maintenance_thinking:
                effective_thinking, effective_effort = maintenance_thinking, maintenance_effort
            effective_max_tokens = int(
                no_action_recovery_override.get("max_tokens")
                if no_action_recovery_override is not None
                else max_tokens
            )
            try:
                compacted = self._compact_messages(messages, system=str(system), tools=advertised_tools,
                    max_tokens=effective_max_tokens, agent_id=normalized_agent, role=normalized_role,
                    thinking=effective_thinking, effort=effective_effort, stop_event=stop_event,
                    continuation_context=continuation_context,
                    language_metadata=tool_context.metadata)
            except InterruptedError:
                self._publish_checkpoint(_checkpoint_with_messages(session.checkpoint, messages), segments, checkpoint_sink)
                raise
            except (CompactionSummaryError, AgentContextWindowError, vision_budget.VisionBudgetError) as exc:
                self._publish_checkpoint(_checkpoint_with_messages(session.checkpoint, messages), segments, checkpoint_sink)
                self._emit(normalized_agent, normalized_role, "agent_context_compaction_failed",
                    segment_id=session.checkpoint.segment_id, error_type=type(exc).__name__,
                    error_summary=str(exc), checkpoint_retained=True, recoverable=True,
                    recovery_action="restart_or_redelegate_narrower_owner",
                    **getattr(exc, "metrics", {}))
                raise AgentInterruptedError(
                    "owner_interrupted: context_compaction_recoverable; checkpoint retained; "
                    "restart this Owner or delegate a narrower task after reviewing the compaction event: "
                    + str(exc)[:1000]
                ) from exc
            if compacted is not None:
                next_messages, metrics = compacted
                previous = _checkpoint_with_messages(session.checkpoint, messages)
                self._publish_checkpoint(previous, segments, checkpoint_sink)
                session.checkpoint = compact_segment(previous,
                    summary="Unified context compaction", reason=str(metrics["reason"]))
                messages = next_messages
                progress.rebase_evidence(messages)
                logical_turn = _assistant_message_count(messages)
                known_tool_use_ids = completed_tool_use_ids | _tool_use_ids(messages)
                trace_context.update(turn=logical_turn + 1, segment_id=session.checkpoint.segment_id)
                session.checkpoint = _checkpoint_with_messages(session.checkpoint, messages)
                self._trace_local.turn = logical_turn
                self._trace_local.segment_id = session.checkpoint.segment_id
                self._publish_checkpoint(session.checkpoint, segments, checkpoint_sink)
                self._emit(normalized_agent, normalized_role, "agent_context_compacted",
                    turn=logical_turn, segment_id=session.checkpoint.segment_id,
                    predecessor_segment_id=previous.segment_id, **metrics)
            request = ModelRequest(
                model=self.settings.model,
                system=str(system),
                messages=tuple(messages),
                tools=advertised_tools,
                max_tokens=effective_max_tokens,
                thinking=effective_thinking,
                effort=effective_effort,
                request_kind=str(request_kind),
            )
            request_public = request_kind_public(request.request_kind)
            trace_context.update(
                model=request.model,
                actual_max_tokens=request.max_tokens,
                **request_public,
            )
            if session.checkpoint.segment_id not in context_segments:
                self._emit(normalized_agent, normalized_role, "agent_context",
                    turn=logical_turn, segment_id=session.checkpoint.segment_id,
                    **{
                        **dict(tool_context.metadata), "system": request.system,
                        "messages": list(request.messages), "tools": list(request.tools),
                        "model": request.model, "api_format": self.settings.api_format,
                        "thinking": request.thinking, "effort": request.effort,
                        "max_turns": max_turns, "max_tokens": request.max_tokens,
                        "actual_max_tokens": request.max_tokens,
                        **request_public,
                        "history_breakdown": _message_history_breakdown(
                            request.messages, system=request.system, tools=request.tools),
                        "compaction_policy": {"compact_trigger_ratio": self.compact_trigger_ratio,
                            "compact_target_tokens": self.compact_target_tokens,
                            "compact_summary_target_tokens": self.compact_summary_target_tokens},
                        **request_context_budget(self.settings, max_tokens=request.max_tokens,
                            training_sample_max_tokens=self.training_sample_max_tokens),
                        "streaming": self.settings.streaming,
                        "timeout": {key: getattr(self.settings, key) for key in (
                            "total_timeout_s", "connect_timeout_s", "idle_timeout_s", "queue_timeout_s")},
                    })
                context_segments.add(session.checkpoint.segment_id)
                # 起始输入也是 raw：即使本段首次请求就失败，声明的前驱链仍须完整落盘。
                self._publish_checkpoint(
                    _checkpoint_with_messages(session.checkpoint, messages), segments, checkpoint_sink
                )
            try:
                if request_deadline is None:
                    request_deadline = session.monotonic() + provider_request_budget_s(self.settings)
                result = session.call(request, preferred_channel_id=preferred_channel_id,
                    deadline_monotonic=request_deadline)
            except InterruptedError:
                self._publish_checkpoint(_checkpoint_with_messages(session.checkpoint, messages), segments, checkpoint_sink)
                raise
            preferred_channel_id = ""
            if not result.ok:
                if result.failure is not None and result.failure.kind == "resource_queue_timeout":
                    self._publish_checkpoint(
                        _checkpoint_with_messages(session.checkpoint, messages), segments, checkpoint_sink
                    )
                    raise ResourceSlotTimeout(f"resource_queue_timeout: {result.failure.detail}")
                if result.segment_rollover_required:
                    rollover_count += 1
                    if rollover_count > self.settings.max_attempts:
                        self._publish_checkpoint(
                            _checkpoint_with_messages(session.checkpoint, messages), segments, checkpoint_sink
                        )
                        raise AgentProviderError(
                            "Provider segment rollover budget was exhausted", failure=result.failure,
                            retry_after_s=self.settings.channel_cooldown_s,
                        )
                    session.checkpoint = _checkpoint_with_messages(
                        session.checkpoint, messages
                    )
                    self._publish_checkpoint(
                        session.checkpoint, segments, checkpoint_sink
                    )
                    failure = result.failure
                    recovered = session.roll_over_after_failure(
                        failed_channel_id=result.channel_id,
                        reason=(
                            failure.kind
                            if failure is not None
                            else "provider_segment_rollover"
                        ),
                        invalid_signature=bool(
                            failure is not None and failure.invalid_signature
                        ),
                    )
                    messages = copy.deepcopy(list(recovered.messages))
                    progress.rebase_evidence(messages)
                    preferred_channel_id = result.next_channel_id
                    known_tool_use_ids = completed_tool_use_ids | _tool_use_ids(messages)
                    logical_turn = _assistant_message_count(messages)
                    trace_context["turn"] = logical_turn + 1
                    trace_context["segment_id"] = recovered.segment_id
                    self._trace_local.turn = logical_turn
                    self._trace_local.segment_id = recovered.segment_id
                    self._emit(
                        normalized_agent,
                        normalized_role,
                        "agent_segment_rolled_over",
                        segment_id=recovered.segment_id,
                        predecessor_segment_id=recovered.predecessor_segment_id,
                        failed_channel_id=result.channel_id,
                    )
                    continue
                failure = result.failure
                detail = failure.detail if failure is not None else "unknown failure"
                kind = failure.kind if failure is not None else "provider_error"
                self._publish_checkpoint(
                    _checkpoint_with_messages(session.checkpoint, messages), segments, checkpoint_sink
                )
                raise AgentProviderError(f"{kind}: {detail}", failure=failure,
                    retry_after_s=(failure.retry_after_s if failure and failure.retry_after_s is not None
                                   else self.settings.channel_cooldown_s))

            # 到这里说明刚发出的请求被 Provider 成功消费。若上一轮附了 Native
            # inspect 图片，模型已在本次请求里看到它，此刻才提交延迟 inspection；
            # Provider 失败路径（上面 rollover/raise）永不到达这里，故失败不误提交。
            inspection_updates = self._flush_pending_inspections(
                pending_inspections,
                normalized_agent,
                normalized_role,
                session.checkpoint.segment_id,
            )

            response = result.response
            blocks = _response_blocks(response)
            id_changes = _canonicalize_tool_use_ids(
                blocks, known_tool_use_ids,
                namespace=f"{session.checkpoint.segment_id}:{logical_turn + 1}",
                namespace_provider_ids=(self.settings.api_format == "openai_chat_compatible"
                                        and request.model.strip().lower() == "kimi-k3"),
            )
            tool_calls = _extract_tool_calls(blocks)
            current_ids = [call.tool_use_id for call in tool_calls]
            if id_changes:
                self._emit(normalized_agent, normalized_role, "tool_call_ids_remapped",
                           attempt_id=result.attempt_id, turn=logical_turn + 1,
                           segment_id=session.checkpoint.segment_id, mappings=id_changes)
            known_tool_use_ids.update(current_ids)
            messages.append({"role": "assistant", "content": blocks})
            request_deadline = None
            turns += 1
            logical_turn += 1
            self._trace_local.turn = logical_turn
            self._trace_local.segment_id = session.checkpoint.segment_id
            stop_reason = str(getattr(response, "stop_reason", "") or "end_turn")
            language_drift_detected = False
            for block_index, block in enumerate(blocks):
                block_type = str(block.get("type") or "")
                if block_type == "text" and str(block.get("text") or "").strip():
                    message = str(block.get("text") or "").strip()
                    self._emit(
                        normalized_agent,
                        normalized_role,
                        "assistant_message",
                        attempt_id=result.attempt_id,
                        turn=logical_turn,
                        segment_id=session.checkpoint.segment_id,
                        block_index=block_index,
                        message=message,
                        model=request.model,
                        actual_max_tokens=request.max_tokens,
                        **request_kind_public(request.request_kind),
                    )
                    warning = language_drift_warning(message, tool_context.metadata)
                    if warning is not None:
                        language_drift_detected = True
                        self._emit(
                            normalized_agent,
                            normalized_role,
                            "language_drift",
                            attempt_id=result.attempt_id,
                            turn=logical_turn,
                            segment_id=session.checkpoint.segment_id,
                            block_index=block_index,
                            model=request.model,
                            actual_max_tokens=request.max_tokens,
                            **request_kind_public(request.request_kind),
                            **warning,
                        )
            # Provider 中立:只要归一响应带公开分析摘要就发,不关心它来自
            # Anthropic summarized thinking 还是 OpenAI Responses reasoning summary。
            # 自托管 Chat 兼容端点没有公开摘要(analysis_summaries 为空)→ 不发事件,
            # raw reasoning 永不进公开 Trace。
            for summary_index, summary in enumerate(
                getattr(response, "analysis_summaries", ()) or ()
            ):
                text = str(summary or "").strip()
                if not text:
                    continue
                self._emit(
                    normalized_agent,
                    normalized_role,
                    "assistant_analysis_summary",
                    attempt_id=result.attempt_id,
                    turn=logical_turn,
                    segment_id=session.checkpoint.segment_id,
                    block_index=summary_index,
                        summary=text,
                        model=request.model,
                        actual_max_tokens=request.max_tokens,
                        **request_public,
                    )
            self._emit(
                normalized_agent,
                normalized_role,
                "agent_turn_completed",
                turn=logical_turn,
                stop_reason=stop_reason,
                tool_call_count=len(tool_calls),
                segment_id=session.checkpoint.segment_id,
                task_id=str(tool_context.metadata.get("task_id") or ""),
                wave_id=str(tool_context.metadata.get("wave_id") or ""),
                group_id=str(tool_context.metadata.get("group_id") or ""),
                pages=list(tool_context.metadata.get("pages") or []),
                model=request.model,
                actual_max_tokens=request.max_tokens,
                **request_public,
            )

            # 截断的工具调用(BUG-P):stop_reason=max_tokens/length 且带 tool_use 时,
            # 模型是在“吐工具参数 JSON 的中途”被输出上限截断的——最后一个 tool_use 的
            # input 往往残缺(如只有 path 缺 content)甚至为空。直接执行会得到
            # missing_arguments,反复重试仍被同样截断→熔断→整册挂(opus-5 写大文件尤甚)。
            # 与无工具的 length-stop 同构地软恢复:丢弃这轮截断的 assistant(只保留可见
            # 文本、剔除残缺 tool_use,保持 tool 事务闭合),提示精简后有界重试。
            if stop_reason in {"max_tokens", "length"} and tool_calls:
                if action_recovery_attempts < ACTION_RECOVERY_LIMIT:
                    action_recovery_attempts += 1
                    no_action_recovery_override = _targeted_recovery_configuration(
                        max_tokens, preserve_output_budget=True, model=self.settings.model
                    )
                    partial_text = _visible_text(blocks)
                    # 用纯文本版替换刚追加的(含残缺 tool_use 的)assistant,避免留下
                    # 无 tool_result 配对的 open transaction 破坏 checkpoint 边界。
                    if messages and messages[-1].get("role") == "assistant":
                        messages.pop()
                    if partial_text:
                        messages.append(
                            {"role": "assistant", "content": [{"type": "text", "text": partial_text}]}
                        )
                    messages.append(
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "上一条回复在发出工具调用时因超出输出上限被截断,工具参数不完整。"
                                        "请不要重复已写内容。已有文件时优先用一次小范围 patch;尚无文件时只用 "
                                        "write_file 写一份短而完整的骨架。write_file 会覆盖整份文件,不能用于分段追加。"
                                        f"{recovery_thinking_notice}并保留原输出预算;只提交一个最小、参数完整的工具动作,"
                                        "不要展开长篇分析或全文复述。"
                                    ),
                                }
                            ],
                        }
                    )
                    known_tool_use_ids = completed_tool_use_ids | _tool_use_ids(messages)
                    messages.extend(inspection_updates)
                    session.checkpoint = _checkpoint_with_messages(
                        session.checkpoint, messages
                    )
                    self._publish_checkpoint(
                        session.checkpoint, segments, checkpoint_sink
                    )
                    continue
                raise AgentProtocolError(
                    "Provider truncated a tool call at the token limit after one targeted recovery"
                )

            empty_response = not tool_calls and not _visible_text(blocks).strip()
            no_action_kind = ""
            if empty_response:
                if stop_reason in {"max_tokens", "length"}:
                    no_action_kind = "output_budget_exhausted_no_action"
                elif stop_reason == "end_turn" and (
                    any(block.get("type") in {"thinking", "redacted_thinking"} for block in blocks)
                    or getattr(response, "analysis_summaries", ())
                ):
                    no_action_kind = "reasoning_only_no_action"
            if no_action_kind:
                # 输出耗尽可能是隐藏的大工具参数；保留原预算但关闭 thinking。纯思考空耗则
                # 使用短执行窗口。两者共享同一次纠正机会。
                messages.pop()
                retry_allowed = action_recovery_attempts < ACTION_RECOVERY_LIMIT
                recovery_configuration: dict[str, object] = {}
                if retry_allowed:
                    recovery_configuration = _targeted_recovery_configuration(
                        max_tokens,
                        model=self.settings.model,
                        preserve_output_budget=(
                            no_action_kind == "output_budget_exhausted_no_action"
                        ),
                    )
                preserved_budget = bool(
                    recovery_configuration
                    and recovery_configuration.get("max_tokens") == int(max_tokens)
                )
                self._emit(
                    normalized_agent, normalized_role, "agent_no_action_response",
                    turn=logical_turn, segment_id=session.checkpoint.segment_id,
                    reason=no_action_kind or "empty_response_after_no_action_recovery",
                    stop_reason=stop_reason, retry_allowed=retry_allowed,
                    recovery_action=(
                        "retry_" + ("low_thinking" if maintenance_thinking else "without_thinking")
                        + ("_preserved_budget" if preserved_budget else "_reduced_budget")
                        if retry_allowed else "checkpoint_and_stop"),
                    thinking=request.thinking, max_tokens=request.max_tokens,
                    configuration_changed=retry_allowed,
                    recovery_configuration=recovery_configuration,
                )
                if retry_allowed:
                    action_recovery_attempts += 1
                    no_action_recovery_override = recovery_configuration
                    messages.append({"role": "user", "content": [{"type": "text", "text": (
                        "上一轮已耗尽输出预算却没有正文或工具动作。" if no_action_kind == "output_budget_exhausted_no_action"
                        else "上一轮只有思考，没有可用正文或工具动作。"
                    ) + (
                        "请依据已有上下文直接提交下一项最小、参数完整的工具动作，或简短报告真实完成情况/具体阻塞。"
                        "不要继续扩展分析、重读已有证据或重复已完成工作。已有文件时用一次小范围 patch；"
                        f"尚无文件时只写短而完整的骨架，不要重发整篇文件。{recovery_thinking_notice}；"
                        "这是一次纠正机会，仍无动作将保存 checkpoint 后停止。"
                    )}]})
                messages.extend(inspection_updates)
                session.checkpoint = _checkpoint_with_messages(session.checkpoint, messages)
                self._publish_checkpoint(session.checkpoint, segments, checkpoint_sink)
                if not retry_allowed:
                    raise AgentProtocolError(
                        f"{no_action_kind or 'empty_response_after_no_action_recovery'}: "
                        "no usable action after one targeted recovery in minimal reasoning mode; "
                        "Owner checkpoint saved for resume"
                    )
                continue

            no_action_recovery_override = None

            # 只把确有必填字段的空参骨架视为退化；合法无参工具照常执行。
            if empty_response or (
                tool_calls
                and stop_reason == "end_turn"
                and all(not call.arguments for call in tool_calls)
                and all(any(spec["name"] == call.name and spec.get("input_schema", {}).get("required")
                            for spec in advertised_tools) for call in tool_calls)
            ):
                if action_recovery_attempts < ACTION_RECOVERY_LIMIT:
                    action_recovery_attempts += 1
                    no_action_recovery_override = _targeted_recovery_configuration(
                        max_tokens, model=self.settings.model
                    )
                    self._emit(
                        normalized_agent,
                        normalized_role,
                        "agent_empty_response_recovered" if empty_response else "agent_empty_tool_call_recovered",
                        turn=logical_turn,
                        segment_id=session.checkpoint.segment_id,
                        tool_names=[call.name for call in tool_calls],
                        retry=action_recovery_attempts,
                        recovery_configuration=no_action_recovery_override,
                    )
                    partial_text = _visible_text(blocks)
                    # 用纯文本版替换刚追加的(含空壳 tool_use 的)assistant,保持事务闭合。
                    if messages and messages[-1].get("role") == "assistant":
                        messages.pop()
                    if partial_text:
                        messages.append(
                            {"role": "assistant", "content": [{"type": "text", "text": partial_text}]}
                        )
                    tool_label = ", ".join(
                        dict.fromkeys(call.name for call in tool_calls)
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "上一条回复没有可用的正文或工具动作。请给出简短的完成说明，"
                                        "或发出下一个参数完整的工具调用；不要重复已完成的工作。"
                                        f"{recovery_thinking_notice}，且这是本次退化唯一一次纠正机会。"
                                    ) if empty_response else (
                                        f"上一条回复发出了 {len(tool_calls)} 个空参数的工具调用"
                                        f"({tool_label}),没有携带任何必填参数,无法执行。"
                                        "请不要重复措辞,只发出**一个**结构完整的工具调用,"
                                        "并确保其参数对象包含该工具全部必填字段的真实内容"
                                        "(例如 delegate_submit 必须带非空的 tasks 数组)。"
                                    ),
                                }
                            ],
                        }
                    )
                    known_tool_use_ids = completed_tool_use_ids | _tool_use_ids(messages)
                    messages.extend(inspection_updates)
                    session.checkpoint = _checkpoint_with_messages(
                        session.checkpoint, messages
                    )
                    self._publish_checkpoint(
                        session.checkpoint, segments, checkpoint_sink
                    )
                    continue
                raise AgentProtocolError(
                    "Provider returned empty replies or empty-argument tool calls after one targeted recovery"
                )

            if tool_calls:
                terminal_failure_fingerprint = ""
                terminal_failure_repeats = 0
                result_blocks: list[dict[str, object]] = []
                visible_read_fingerprints = _visible_read_fingerprints(messages)
                argument_failures: set[tuple[str, str]] = set()
                repeated_evidence: set[str] = set()
                execution_context = replace(
                    tool_context,
                    metadata={
                        **dict(tool_context.metadata),
                        "turn": logical_turn,
                        "segment_id": session.checkpoint.segment_id,
                        "model": request.model,
                        "request_kind": request.request_kind,
                        "actual_max_tokens": request.max_tokens,
                        **request_public,
                        # 只采用刚成功请求中实际附带的图片；打开记录或同轮看图不等于已消费。
                        "consumed_visual_sources": opened_visual_sources(
                            request.messages, owner_id=normalized_agent, attached_only=True),
                    },
                )
                visible_attachment_paths = {
                    str(item.get("path"))
                    for item in opened_visual_sources(
                        messages, owner_id=normalized_agent, attached_only=True
                    )
                    if item.get("path")
                }
                # Reserve protected history and native inspections before admitting attachment images.
                available_image_slots = max(
                    0, self.max_context_images - next_turn_protected_image_count(messages)
                )
                inspection_image_allowance = min(
                    available_image_slots,
                    sum(_is_native_inspection_tool(call.name) for call in tool_calls),
                )
                attachment_image_allowance = available_image_slots - inspection_image_allowance
                deferred_visual_paths = _deferred_visual_paths(messages)
                for call, tool_result in execute_tool_batch(self.tools, execution_context, tool_calls):
                    progress.observe(call, tool_result, argument_failures=argument_failures,
                                     repeated_evidence=repeated_evidence)
                    tool_call_count += 1
                    if tool_result.ok:
                        # 读取/自述没有推进任务，不能给反复耗尽输出预算续次。
                        if call.name not in {"read_file", "think"}:
                            action_recovery_attempts = 0
                    result_block = tool_result.as_message_block()
                    if not tool_result.ok and tool_result.error_code:
                        # 携带机械错误码给 OpenAI 转换层使用；Anthropic wire 会剥离该内部键。
                        result_block["error_code"] = tool_result.error_code
                    if _is_native_inspection_tool(call.name) and tool_result.ok:
                        payload = _decode_tool_payload(tool_result.content)
                        prompt_payload = _prompt_safe_inspection_payload(payload)
                        if not payload and isinstance(tool_result.content, str) and tool_result.content.strip():
                            prompt_payload = {
                                "status": "ok",
                                "result": _prompt_clip_value(tool_result.content),
                            }
                        # inspect 回执给出真实图片路径；旧 checkpoint 的路径参数仍如实可读。
                        relative = str(
                            payload.get("path")
                            or call.arguments.get("path")
                            or ""
                        )
                        pixel_digest = str(
                            payload.get("png_sha256") or payload.get("sha256") or ""
                        )
                        already_opened = any(
                            isinstance(item, Mapping)
                            and item.get("path") == relative
                            and (not pixel_digest or item.get("sha256") == pixel_digest)
                            for item in opened_sources
                        )
                        feedback_needs_pixels = isinstance(payload.get("critic_feedback"), Mapping) and not (
                            already_opened or payload.get("pixels_delivered_to_this_owner")
                        )
                        attach_requested = bool(relative) and (
                            payload.get("native_multimodal")
                            or call.name == "vision_analyze"
                            or call.arguments.get("retain")
                            or feedback_needs_pixels
                        ) and (
                            not payload.get("inspection_cached")
                            or call.arguments.get("retain")
                            or feedback_needs_pixels
                            or (relative in deferred_visual_paths and not already_opened)
                        )
                        attach = attach_requested and inspection_image_allowance > 0
                        if attach_requested and not attach:
                            prompt_payload.update({
                                "pixels_attached_in_this_result": False,
                                "status": "visual_payload_deferred",
                                "visual_payload_status": "deferred_by_turn_image_budget",
                                "next_action": (
                                    "Use the existing text receipt now. If pixels are still necessary, "
                                    "inspect only the highest-priority deferred target in a later turn."
                                ),
                            })
                        result_block["content"] = json.dumps(prompt_payload, ensure_ascii=False)
                        if attach:
                            result_block = _vision_result_block(
                                result_block,
                                relative,
                                execution_context,
                                opened_sources=opened_sources,
                            )
                            fresh_sources = opened_visual_sources(
                                ({"role": "user", "content": [result_block]},),
                                owner_id=normalized_agent,
                            )
                            seen_sources = {
                                (item.get("owner_id"), item.get("path"), item.get("sha256"))
                                for item in opened_sources
                                if isinstance(item, Mapping)
                            }
                            opened_sources = [
                                *opened_sources,
                                *[
                                    item for item in fresh_sources
                                    if (item.get("owner_id"), item.get("path"), item.get("sha256"))
                                    not in seen_sources
                                ],
                            ]
                            if call.arguments.get("retain"):
                                result_block["vision_keep"] = True
                            inspection_image_allowance -= 1
                            vision_paths.append(relative)
                        else:
                            result_block["content"] = json.dumps(prompt_payload, ensure_ascii=False)
                        commit = payload.get("pending_inspection")
                        if attach and isinstance(commit, Mapping):
                            descriptor = copy.deepcopy(dict(commit))
                            pending_inspections.append(descriptor)
                    elif call.name == "delegate_submit" and tool_result.ok:
                        payload = _decode_tool_payload(tool_result.content)
                        compact = _delegate_submit_prompt_payload(
                            payload if payload else tool_result.content
                        )
                        result_block["content"] = (
                            json.dumps(compact, ensure_ascii=False)
                            if not isinstance(compact, str)
                            else compact
                        )
                    elif call.name == "terminal":
                        payload = _decode_tool_payload(tool_result.content)
                        compact = _terminal_prompt_payload(
                            payload if payload else tool_result.content
                        )
                        result_block["content"] = (
                            json.dumps(compact, ensure_ascii=False)
                            if not isinstance(compact, str)
                            else compact
                        )
                    elif call.name == "read_file" and tool_result.ok:
                        payload = _decode_tool_payload(tool_result.content)
                        prompt_payload = _deduplicate_read_payload(payload, visible_read_fingerprints)
                        if prompt_payload != payload:
                            result_block["content"] = json.dumps(prompt_payload, ensure_ascii=False)
                        if payload.get("_multimodal_images"):
                            result_block, attached_paths = _attachment_read_result_block(
                                result_block,
                                execution_context,
                                image_allowance=attachment_image_allowance,
                                visible_paths=frozenset(visible_attachment_paths),
                            )
                            attachment_image_allowance -= len(attached_paths)
                            visible_attachment_paths.update(attached_paths)
                            vision_paths.extend(attached_paths)
                    result_blocks.append(result_block)
                messages.append({"role": "user", "content": result_blocks})
                messages.extend(inspection_updates)
                language_reminder = visible_output_language_reminder(
                    tool_context.metadata,
                    tool_names=tuple(call.name for call in tool_calls),
                    trigger="language_drift" if language_drift_detected else "tool_result",
                )
                append_language_reminder(messages, language_reminder, force=language_drift_detected)
                completed_tool_use_ids.update(call.tool_use_id for call in tool_calls)
                session.checkpoint = _checkpoint_with_messages(
                    session.checkpoint, messages
                )
                self._publish_checkpoint(
                    session.checkpoint, segments, checkpoint_sink
                )
                # tool_use/tool_result 已成对闭合并持久化 checkpoint 后再熔断，
                # 不空转到 max_turns，也不破坏事务配对。run() 包装层负责发布
                # 带 last_failed_tool / repeat_count 的 agent_failed 事件。
                for block in result_blocks:
                    if block.get("error_code") in {"owner_interrupted", "font_integrity_failed"}:
                        raise AgentInterruptedError(
                            f"{block['error_code']}: {block['content']}; checkpoint saved for workspace resume")
                if any(block.get("error_code") == "resource_queue_timeout" for block in result_blocks):
                    raise ResourceSlotTimeout("resource_queue_timeout: dependent Owner paused; resume this workspace")
                progress_warning = progress.check(normalized_agent, tool_calls)
                if (progress_warning and normalized_role == "orchestrator" and idle_handler is not None
                        and progress.evidence_key.startswith("terminal:directory:")):
                    event_input = idle_handler()
                    if event_input is not None:
                        accept_idle_input(event_input, language_drift=language_drift_detected)
                        progress.evidence_repeats, progress.evidence_warned = 0, False
                        progress_warning = False
                        self._emit(normalized_agent, normalized_role, "agent_directory_poll_waited",
                            turn=logical_turn, segment_id=session.checkpoint.segment_id,
                            recovery_action="wait_for_orchestration_event")
                if progress_warning:
                    messages.append({"role": "user", "content": [{"type": "text", "text": (
                        "[Harness progress check] These calls returned already-seen evidence or unchanged artifacts. "
                        "Repeating the same read, render or inspection does not modify a page. Do not repeat the same checks. "
                        "Use the evidence already in context "
                        "to perform the next required workflow action, or report a specific unresolved blocker. "
                        "This warning is not approval of the work; required verification still applies."
                    )}]})
                    session.checkpoint = _checkpoint_with_messages(session.checkpoint, messages)
                    self._publish_checkpoint(session.checkpoint, segments, checkpoint_sink)
                    self._emit(normalized_agent, normalized_role, "agent_repeated_evidence_warning",
                               tool_name=progress.evidence_key.partition(":")[0], repeat_count=progress.evidence_repeats,
                               turn=logical_turn, segment_id=session.checkpoint.segment_id)
                # The current assistant action has now consumed any image returned by an
                # earlier tool result.  Preserve this completed action in the current
                # training segment, then start a new inherited-context segment without
                # retransmitting those pixels.  This keeps runtime and SFT semantics
                # aligned: the action that used the image retains it; later actions do not.
                released_messages, release_metrics = release_consumed_visual_context(
                    messages,
                    max_retained_images=min(
                        vision_budget.MAX_RETAINED_TOOL_IMAGES,
                        self.max_context_images,
                    ),
                )
                if release_metrics["images_released"]:
                    previous = session.checkpoint
                    fresh = SegmentCheckpoint.initial(released_messages)
                    session.checkpoint = replace(
                        fresh,
                        predecessor_segment_id=previous.segment_id,
                        provider_affinity_id=previous.provider_affinity_id,
                        excluded_channel_ids=previous.excluded_channel_ids,
                        reason="visual_context_release",
                        invalid_signature_recoveries=previous.invalid_signature_recoveries,
                    )
                    messages = released_messages
                    progress.rebase_evidence(messages)
                    known_tool_use_ids = completed_tool_use_ids | _tool_use_ids(messages)
                    trace_context["segment_id"] = session.checkpoint.segment_id
                    self._trace_local.segment_id = session.checkpoint.segment_id
                    self._publish_checkpoint(session.checkpoint, segments, checkpoint_sink)
                    self._emit(
                        normalized_agent,
                        normalized_role,
                        "agent_visual_context_released",
                        turn=logical_turn,
                        segment_id=session.checkpoint.segment_id,
                        predecessor_segment_id=previous.segment_id,
                        **release_metrics,
                    )
                elif release_metrics["images_retained_one_extra_turn"]:
                    messages = released_messages
                    session.checkpoint = _checkpoint_with_messages(
                        session.checkpoint, messages
                    )
                    self._publish_checkpoint(
                        session.checkpoint, segments, checkpoint_sink
                    )
                continue

            if inspection_updates:
                messages.extend(inspection_updates)
                session.checkpoint = _checkpoint_with_messages(session.checkpoint, messages)
                self._publish_checkpoint(session.checkpoint, segments, checkpoint_sink)
                continue
            if stop_reason in {"max_tokens", "length"}:
                # 27b 冗长响应命中输出上限却未发出工具调用:不直接失败整个 task,
                # 而是把已生成的可见文本作为一轮 assistant 保留,追加一条简短提示让它
                # 收敛并立即发出工具调用,有界重试。超过上限才硬失败。
                if action_recovery_attempts < ACTION_RECOVERY_LIMIT:
                    action_recovery_attempts += 1
                    no_action_recovery_override = _targeted_recovery_configuration(
                        max_tokens, preserve_output_budget=True, model=self.settings.model
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "上一条回复因超出输出上限被截断。请大幅精简:不要重复已写内容,"
                                        "直接发出下一个工具调用(如 write_file/terminal/inspect/"
                                        "delegate_* 等)以推进任务;若已可结束则简短收尾。"
                                        "读取和自述不会重置截断恢复预算；使用已有上下文完成最小可执行的下一步，"
                                        f"不要重复读取已返回的内容。{recovery_thinking_notice}并保留原输出预算；"
                                        "这是本次退化唯一一次纠正机会。"
                                    ),
                                }
                            ],
                        }
                    )
                    session.checkpoint = _checkpoint_with_messages(
                        session.checkpoint, messages
                    )
                    self._publish_checkpoint(
                        session.checkpoint, segments, checkpoint_sink
                    )
                    continue
                raise AgentProtocolError(
                    "Provider stopped at the token limit without a tool call after one targeted recovery"
                )
            final_text = _visible_text(blocks)
            if not final_text:
                raise AgentProtocolError(
                    "Provider ended the Agent run without visible role output"
                )
            session.checkpoint = _checkpoint_with_messages(
                session.checkpoint, messages
            )
            self._publish_checkpoint(session.checkpoint, segments, checkpoint_sink)
            if idle_handler is not None:
                if not tool_boundary(messages).is_closed:
                    raise AgentProtocolError("cannot wait across an open tool transaction")
                event_input = idle_handler()
                if event_input is not None:
                    accept_idle_input(event_input, language_drift=language_drift_detected)
                    continue
            ordered_segments = tuple(segments.values())
            self._emit(
                normalized_agent,
                normalized_role,
                "agent_completed",
                turn=logical_turn,
                segment_id=session.checkpoint.segment_id,
                turns=turns,
                tool_calls=tool_call_count,
                segment_ids=[item.segment_id for item in ordered_segments],
            )
            return AgentRunResult(
                agent_id=normalized_agent,
                role=normalized_role,
                final_text=final_text,
                stop_reason=stop_reason,
                turns=turns,
                tool_calls=tool_call_count,
                checkpoint=session.checkpoint,
                segments=ordered_segments,
                vision_paths=tuple(vision_paths),
            )

        raise AgentTurnLimitError(
            f"Agent {normalized_agent!r} exceeded max_turns={int(max_turns)}"
        )

    def _compact_messages(self, messages, *, system, tools, max_tokens, agent_id, role,
                          continuation_context=None, language_metadata=None, thinking=False,
                          effort="medium", stop_event=None):
        """一次评估并生成候选；摘要失败或必要输入放不下时，不改当前段。"""
        capacity = request_context_budget(self.settings, max_tokens=max_tokens,
            training_sample_max_tokens=self.training_sample_max_tokens)
        budget = capacity["input_budget_tokens"]
        if budget <= 0:
            raise AgentContextWindowError("output budget and safety buffer leave no room in the effective context window")
        soft_watermark = int(capacity["effective_context_window"] * self.compact_trigger_ratio)

        def usage(items):
            return {"tokens": estimate_request_tokens(system, items, tools),
                    "images": sum(1 for message in items for _ in vision_budget.image_blocks(message.get("content"))),
                    "bytes": vision_budget.request_bytes({"system": system, "messages": items, "tools": tools})}

        before = usage(messages)
        causes = [key for key, limit in (("tokens", budget), ("images", self.max_context_images),
                  ("bytes", vision_budget.MAX_REQUEST_BYTES - 65536)) if before[key] > limit]
        if before["tokens"] > soft_watermark and "tokens" not in causes:
            causes.append("soft_tokens")
        if not causes:
            return None
        if not tool_boundary(messages).is_closed:
            raise AgentProtocolError("cannot compact an open tool transaction")
        current = continuation_context() if continuation_context is not None else None
        summary_deadline = time.monotonic() + provider_request_budget_s(self.settings)
        invoker = (lambda request: self.summary_invoker(request, agent_id=agent_id, role=role,
            segment_id=str(getattr(self._trace_local, "segment_id", "")), stop_event=stop_event,
            deadline_monotonic=summary_deadline)
            ) if self.summary_invoker is not None else None
        summary, summary_metrics = context_compaction_summary(messages, agent_id=agent_id, role=role,
            current_task=current, settings=self.settings, max_tokens=max_tokens, thinking=thinking, effort=effort,
            target_tokens=self.compact_summary_target_tokens, invoke=invoker,
            training_sample_max_tokens=self.training_sample_max_tokens,
            language_policy=extract_language_policy(system))
        prefix = [{"role": "user", "content": summary}]
        effective_compact_target_tokens = min(self.compact_target_tokens, budget)
        try:
            tail, tail_metrics = visual_compaction_tail(messages, owner_id=agent_id, system=system, tools=tools,
                prefix=prefix, target_tokens=effective_compact_target_tokens, input_budget_tokens=budget,
                max_images=self.max_context_images)
        except vision_budget.VisionBudgetError as exc:
            raise CompactionSummaryError(str(exc), metrics=summary_metrics) from exc
        candidate = [*prefix, *tail]
        if not tool_boundary(candidate).is_closed:
            raise CompactionSummaryError(
                "compaction produced an open tool transaction", metrics=summary_metrics
            )
        if candidate and candidate[-1].get("role") == "assistant":
            terminal_content = candidate[-1].get("content")
            has_tool_use = isinstance(terminal_content, list) and any(
                isinstance(block, Mapping) and block.get("type") == "tool_use"
                for block in terminal_content
            )
            has_visible_text = (
                isinstance(terminal_content, str) and bool(terminal_content.strip())
            ) or (
                isinstance(terminal_content, list) and any(
                    isinstance(block, Mapping)
                    and block.get("type") == "text"
                    and bool(str(block.get("text") or "").strip())
                    for block in terminal_content
                )
            )
            if has_tool_use or not has_visible_text:
                raise CompactionSummaryError(
                    "compaction retained a non-continuable terminal assistant message",
                    metrics=summary_metrics,
                )
            continuation = (
                "Continue from the completed work above. Perform only the next necessary action, "
                "or briefly report completion."
            )
            language_reminder = visible_output_language_reminder(
                language_metadata, trigger="context_compaction"
            )
            if language_reminder:
                continuation += "\n" + language_reminder
            candidate.append({"role": "user", "content": [{"type": "text", "text": continuation}]})
        after = usage(candidate)
        exceeded = [key for key, limit in (
            ("tokens", budget), ("images", self.max_context_images),
            ("bytes", vision_budget.MAX_REQUEST_BYTES - 65536),
        ) if after[key] > limit]
        if exceeded:
            raise CompactionSummaryError(
                "compaction continuation exceeds request capacity: " + ", ".join(exceeded),
                metrics={**summary_metrics, "input_budget": budget,
                         **{f"{key}_after": value for key, value in after.items()}},
            )
        unmet = tail_metrics["target_unmet_reason"]
        if after["tokens"] > effective_compact_target_tokens and not unmet:
            unmet = "required_summary_or_visual_inputs_exceed_target"
        return candidate, {"reason": "visual_budget" if causes == ["images"] else "context_compaction",
            "pressure": causes, "estimated_tokens": before["tokens"], "input_budget": budget,
            **capacity, "compact_trigger_ratio": self.compact_trigger_ratio,
            "soft_watermark_tokens": soft_watermark, "compact_target_tokens": effective_compact_target_tokens,
            "configured_compact_target_tokens": self.compact_target_tokens,
            "target_met": after["tokens"] <= effective_compact_target_tokens,
            "system_tools_estimated_tokens": estimate_request_tokens(system, (), tools),
            **{f"{key}_before": value for key, value in before.items()},
            **{f"{key}_after": value for key, value in after.items()},
            **summary_metrics, **tail_metrics, "target_unmet_reason": unmet,
            "length_measurement": "estimate_not_target_template_encoding",
            "images_released": before["images"] - after["images"]}

    def _publish_checkpoint(
        self,
        checkpoint: SegmentCheckpoint,
        segments: dict[str, SegmentCheckpoint],
        sink: CheckpointSink | None,
    ) -> None:
        """更新当前 segment 的最新闭合快照，并通知可选持久化回调。"""
        boundary = tool_boundary(checkpoint.messages)
        if not boundary.is_closed:
            raise AgentProtocolError("attempted to publish an open tool transaction")
        segments[checkpoint.segment_id] = checkpoint
        if sink is not None:
            sink(checkpoint)

    def _flush_pending_inspections(
        self,
        pending: list[Mapping[str, object]],
        agent_id: str,
        role: str,
        segment_id: str,
    ) -> list[dict[str, object]]:
        """在图片被下一次主模型请求成功消费后提交延迟 inspection。

        只有 Provider 调用成功后才会到达调用点，故失败请求永不提交 inspection。
        逐条调用外部提交器（由宿主接线到 ArtifactStore），并另发一个内部
        ``inspection_committed`` 子事件；agent.py 自身不认识页面语义。
        """
        if not pending:
            return []
        drained = list(pending)
        pending.clear()
        committer = getattr(self.tools, "deferred_inspection_committer", None)
        updates = []
        for descriptor in drained:
            fields = {
                str(key): copy.deepcopy(value)
                for key, value in dict(descriptor).items()
                if key not in {"agent_id", "role", "event_type", "segment_id"}
            }
            outcome = committer(dict(descriptor)) if committer is not None else None
            if isinstance(outcome, Mapping) and outcome.get("status") == "stale":
                self._emit(agent_id, role, "inspection_invalidated", segment_id=segment_id, **dict(outcome))
                updates.append({"role": "user", "content": json.dumps({
                    "harness_event": "inspection_invalidated", **dict(outcome),
                    "notice": "这些图片已被本次请求消费，但对应输入随后变化，旧检查不能作为当前回执。保留已完成工作和旧版视觉意见；重新渲染、检查受影响页面，不重启整个 Owner。"
                }, ensure_ascii=False)})
                continue
            if isinstance(outcome, Mapping) and outcome.get("status") == "blocked":
                self._emit(
                    agent_id,
                    role,
                    "inspection_commit_blocked",
                    segment_id=segment_id,
                    **dict(outcome),
                )
                updates.append({"role": "user", "content": json.dumps({
                    "harness_event": "inspection_commit_blocked",
                    **dict(outcome),
                    "notice": (
                        "这些图片已在上一轮成功送达，但验收回执因当前产物前置条件未满足而无法提交。"
                        "按 diagnostics 修复素材登记或提交状态，再检查受影响页面；保留现有页面和已完成工作，"
                        "不要重启整个 Owner，也不要把此状态误报为未收到图片。"
                    ),
                }, ensure_ascii=False)})
                continue
            if outcome is False:
                continue
            self._emit(
                agent_id,
                role,
                "inspection_committed",
                segment_id=segment_id,
                **fields,
            )
        return updates

    def _emit(
        self,
        agent_id: str,
        role: str,
        event_type: str,
        **fields: object,
    ) -> None:
        """把 Agent 身份附加到事件，同时不暴露 Provider 凭证。"""
        if self.event_sink is None:
            return
        self.event_sink(
            {
                "event_type": str(event_type),
                "agent_id": str(agent_id),
                "role": str(role),
                "timestamp": datetime.now(timezone.utc).isoformat().replace(
                    "+00:00", "Z"
                ),
                **copy.deepcopy(fields),
            }
        )


def _checkpoint_with_messages(
    checkpoint: SegmentCheckpoint,
    messages: Sequence[Mapping[str, Any]],
) -> SegmentCheckpoint:
    """保留 segment 身份和 Provider 亲和性，只替换闭合消息。"""
    return SegmentCheckpoint(
        segment_id=checkpoint.segment_id,
        predecessor_segment_id=checkpoint.predecessor_segment_id,
        messages=tuple(copy.deepcopy([dict(message) for message in messages])),
        provider_affinity_id=checkpoint.provider_affinity_id,
        excluded_channel_ids=checkpoint.excluded_channel_ids,
        reason=checkpoint.reason,
        invalid_signature_recoveries=checkpoint.invalid_signature_recoveries,
    )


def _response_blocks(response: object) -> list[dict[str, Any]]:
    """把 Anthropic SDK 或 OpenAI 归一响应转换为持久内容分块。"""
    raw = getattr(response, "content", None)
    # Anthropic SDK 返回 list;Provider 中立的 NormalizedAssistantResponse 用 tuple。
    # 两者都是合法的持久分块序列;仅拒绝非序列(如 None/str/dict)。
    if isinstance(raw, (list, tuple)):
        raw = list(raw)
    else:
        raise AgentProtocolError("Provider response content must be an array")
    blocks: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, Mapping):
            value = copy.deepcopy(dict(item))
        elif callable(getattr(item, "model_dump", None)):
            value = copy.deepcopy(item.model_dump(exclude_none=True))
        else:
            block_type = str(getattr(item, "type", "") or "")
            value = {"type": block_type}
            fields = {
                "text": "text",
                "thinking": "thinking",
                "signature": "signature",
                "data": "data",
                "id": "id",
                "name": "name",
                "input": "input",
            }
            for target, attribute in fields.items():
                candidate = getattr(item, attribute, None)
                if candidate is not None:
                    value[target] = copy.deepcopy(candidate)
        block_type = str(value.get("type") or "")
        if block_type not in {
            "text",
            "thinking",
            "redacted_thinking",
            "tool_use",
        }:
            raise AgentProtocolError(
                f"unsupported assistant content block {block_type!r}"
            )
        if block_type == "tool_use":
            tool_use_id = str(value.get("id") or "").strip()
            name = str(value.get("name") or "").strip()
            arguments = value.get("input")
            if not tool_use_id or not name or not isinstance(arguments, Mapping):
                raise AgentProtocolError("tool_use block is missing id, name, or object input")
            value["id"] = tool_use_id
            value["name"] = name
            value["input"] = copy.deepcopy(dict(arguments))
        blocks.append(value)
    return blocks


def _canonicalize_tool_use_ids(
    blocks: Sequence[dict[str, Any]], known_ids: set[str], *, namespace: str,
    namespace_provider_ids: bool = False,
) -> list[dict[str, str]]:
    """Keep same-turn IDs strict; isolate K3's short IDs across Owners and turns."""
    current_ids = [str(block["id"]) for block in blocks if block.get("type") == "tool_use"]
    if len(current_ids) != len(set(current_ids)):
        raise AgentProtocolError("assistant message contains duplicate tool_use_id")
    repeated = sorted(set(current_ids) & known_ids)
    if repeated and not namespace_provider_ids:
        raise AgentProtocolError(f"assistant reused prior tool_use_id values: {repeated}")
    if not namespace_provider_ids:
        return []
    occupied = known_ids | set(current_ids)
    changes = []
    for index, block in enumerate(blocks):
        if block.get("type") != "tool_use":
            continue
        original = str(block["id"])
        suffix = 0
        while True:
            digest = hashlib.sha256(f"{namespace}:{index}:{suffix}".encode()).hexdigest()[:24]
            canonical = "call_" + digest
            if canonical not in occupied:
                break
            suffix += 1
        block["id"] = canonical
        occupied.add(canonical)
        changes.append({"provider_tool_use_id": original, "tool_use_id": canonical})
    return changes


def _extract_tool_calls(blocks: Sequence[Mapping[str, Any]]) -> list[ToolCall]:
    """按消息顺序提取所有结构化工具调用。"""
    return [
        ToolCall(
            tool_use_id=str(block["id"]),
            name=str(block["name"]),
            arguments=copy.deepcopy(dict(block["input"])),
        )
        for block in blocks
        if block.get("type") == "tool_use"
    ]


def _tool_use_ids(messages: Sequence[Mapping[str, Any]]) -> set[str]:
    """收集已有历史中的工具 ID，阻止 Provider 重用历史事务身份。"""
    values: set[str] = set()
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "tool_use":
                tool_use_id = str(block.get("id") or "")
                if tool_use_id:
                    values.add(tool_use_id)
    return values


def _recorded_tool_use_ids(tools: ToolRuntime, context: ToolContext) -> set[str]:
    """复用全 workspace 工具身份；已接受但未闭合的 ID 同样禁止猜测重放。"""
    values = {str(event["tool_use_id"]) for event in tools.events
              if event.get("event_type") in {"tool_use", "tool_result"} and event.get("tool_use_id")}
    root = context.metadata.get("workspace_root")
    if root:
        for event in iter_jsonl_events(Path(root) / "_trace" / "live-events.jsonl"):
            payload = event.get("payload")
            if event.get("event_type") in {"tool_use", "tool_result"} and isinstance(payload, Mapping):
                tool_id = str(payload.get("tool_use_id") or "")
                if tool_id:
                    values.add(tool_id)
    return values


def _canonical_arguments(arguments: Mapping[str, Any]) -> str:
    """把工具参数规范成稳定文本，使重复失败指纹与键序和格式无关。"""
    try:
        return json.dumps(
            dict(arguments), ensure_ascii=False, sort_keys=True, default=str
        )
    except (TypeError, ValueError):
        return repr(sorted((str(key), str(value)) for key, value in arguments.items()))


def _arguments_summary(arguments: Mapping[str, Any], *, limit: int = 240) -> str:
    """返回参数的短摘要，用于错误面板，不泄露大段内容。"""
    canonical = _canonical_arguments(arguments)
    return canonical if len(canonical) <= limit else canonical[: limit - 1] + "…"


def _assistant_message_count(messages: Sequence[Mapping[str, Any]]) -> int:
    """返回 checkpoint 中已有的逻辑 Assistant 回合数。"""
    return sum(
        1
        for message in messages
        if isinstance(message, Mapping) and message.get("role") == "assistant"
    )


def _json_chars(value: object) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(repr(value))


def _message_history_breakdown(
    messages: Sequence[Mapping[str, Any]], *, system: str = "", tools: Sequence[Mapping[str, Any]] = (),
) -> dict[str, object]:
    """Coarse prompt-size attribution for trace diagnostics; not used for budgeting."""
    messages = model_visible_messages(messages)
    by_role: dict[str, int] = {}
    by_block_type: dict[str, int] = {}
    tool_names: dict[str, str] = {}
    tool_result_by_name: dict[str, int] = {}
    for message in messages:
        role = str(message.get("role") or "unknown")
        by_role[role] = by_role.get(role, 0) + _json_chars(message)
        content = message.get("content")
        if not isinstance(content, (list, tuple)):
            by_block_type["message_text"] = by_block_type.get("message_text", 0) + _json_chars(content)
            continue
        for block in content:
            if not isinstance(block, Mapping):
                by_block_type["block_literal"] = by_block_type.get("block_literal", 0) + _json_chars(block)
                continue
            block_type = str(block.get("type") or "unknown")
            block_chars = _json_chars(block)
            by_block_type[block_type] = by_block_type.get(block_type, 0) + block_chars
            if block_type == "tool_use":
                tool_id = str(block.get("id") or "")
                if tool_id:
                    tool_names[tool_id] = str(block.get("name") or "unknown")
            elif block_type == "tool_result":
                tool_name = tool_names.get(str(block.get("tool_use_id") or ""), "unknown")
                tool_result_by_name[tool_name] = tool_result_by_name.get(tool_name, 0) + block_chars
    return {
        "message_count": len(messages),
        "system_chars": len(str(system)),
        "tools_chars": _json_chars(list(tools)),
        "messages_chars": sum(by_role.values()),
        "by_role_chars": by_role,
        "by_block_type_chars": by_block_type,
        "tool_result_chars_by_tool": tool_result_by_name,
    }




def _visible_text(blocks: Sequence[Mapping[str, Any]]) -> str:
    """连接可见文本，不把隐藏 thinking 当作角色交付。"""
    return "\n".join(
        str(block.get("text") or "").strip()
        for block in blocks
        if block.get("type") == "text" and str(block.get("text") or "").strip()
    ).strip()


def _decode_tool_payload(content: object) -> dict[str, Any]:
    """把工具回执文本解析回对象；非 JSON 或非对象时返回空映射。"""
    if isinstance(content, Mapping):
        return dict(content)
    if not isinstance(content, str) or not content.strip():
        return {}
    try:
        value = json.loads(content)
    except (ValueError, TypeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _deferred_visual_paths(messages: Sequence[Mapping[str, object]]) -> set[str]:
    """Find pixels this Owner explicitly missed because the prior turn was full."""
    paths: set[str] = set()
    for message in messages:
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, (list, tuple)):
            continue
        for block in content:
            if not isinstance(block, Mapping) or block.get("type") != "tool_result":
                continue
            payload = _decode_tool_payload(block.get("content"))
            path = payload.get("path")
            if (
                payload.get("visual_payload_status") == "deferred_by_turn_image_budget"
                and isinstance(path, str)
                and path
            ):
                paths.add(path)
    return paths


def _patched_path(tool_result: object, call: ToolCall) -> str:
    """取一次成功文本写入的逻辑路径。"""
    payload = _decode_tool_payload(getattr(tool_result, "content", None))
    path = payload.get("logical_path") or payload.get("path")
    if not path:
        path = call.arguments.get("path")
    return str(path or "")


def _patched_sha256(tool_result: object) -> str:
    """取 patch/write_file 回执中的实际文件 hash。"""
    payload = _decode_tool_payload(getattr(tool_result, "content", None))
    return str(payload.get("sha256") or "")


def _image_receipts(messages: Sequence[Mapping[str, Any]]) -> list[tuple[str, dict, bool]]:
    """从闭合工具历史恢复已检查身份与末尾未消费图片，缓存回执无需重新附图。"""
    last_assistant = next((i for i in range(len(messages) - 1, -1, -1)
                           if messages[i].get("role") == "assistant"), -1)
    if last_assistant < 0:
        return []
    calls = {}
    receipts = []
    for index, message in enumerate(messages):
        content = message.get("content")
        if message.get("role") == "assistant" and isinstance(content, list):
            calls.update({call.tool_use_id: call.name for call in _extract_tool_calls(content)})
        if message.get("role") != "user" or not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, Mapping) or block.get("type") != "tool_result" or block.get("is_error"):
                continue
            name = calls.get(str(block.get("tool_use_id") or ""), "")
            if not _is_native_inspection_tool(name):
                continue
            raw = block.get("content")
            parts = raw if isinstance(raw, list) else [{"text": raw}]
            has_image = any(part.get("type") == "image" for part in parts if isinstance(part, Mapping))
            for part in parts:
                payload = _decode_tool_payload(part.get("text")) if isinstance(part, Mapping) else {}
                current = payload.get("current_inspection")
                if payload.get("inspection_cached") and isinstance(current, Mapping):
                    receipts.append((name, copy.deepcopy(dict(current)), False))
                descriptor = payload.get("pending_inspection")
                if (has_image or index < last_assistant) and payload.get("native_multimodal") and isinstance(descriptor, Mapping):
                    receipts.append((name, copy.deepcopy(dict(descriptor)), index > last_assistant))
    return receipts


def _read_fingerprint(payload: Mapping[str, Any]) -> str:
    content = payload.get("content")
    if not isinstance(content, str) or len(content) < 2000 or not payload.get("path"):
        return ""
    identity = {key: payload.get(key) for key in ("path", "logical_path", "offset", "mode", "content")}
    return hashlib.sha256(_canonical_arguments(identity).encode("utf-8")).hexdigest()


def _visible_read_fingerprints(messages: Sequence[Mapping[str, Any]]) -> set[str]:
    """Only full file excerpts still in the live context may replace a repeated return."""
    calls: dict[str, str] = {}
    seen: set[str] = set()
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, Mapping):
                continue
            if message.get("role") == "assistant" and block.get("type") == "tool_use":
                calls[str(block.get("id"))] = str(block.get("name"))
            elif (message.get("role") == "user" and block.get("type") == "tool_result"
                  and not block.get("is_error") and calls.get(str(block.get("tool_use_id"))) == "read_file"):
                raw = block.get("content")
                parts = raw if isinstance(raw, list) else [{"type": "text", "text": raw}]
                for part in parts:
                    if isinstance(part, Mapping) and part.get("type") == "text":
                        fingerprint = _read_fingerprint(_decode_tool_payload(part.get("text")))
                        if fingerprint:
                            seen.add(fingerprint)
    return seen


def _deduplicate_read_payload(payload: dict[str, Any], seen: set[str]) -> dict[str, Any]:
    fingerprint = _read_fingerprint(payload)
    if not fingerprint:
        return payload
    if fingerprint not in seen:
        seen.add(fingerprint)
        return payload
    result = {key: value for key, value in payload.items() if key != "content"}
    result["content_status"] = "unchanged_excerpt_already_in_context"
    result["note"] = "The identical excerpt at this path and offset is already in the current context; reuse it."
    return result


def _repeatable_evidence(call: ToolCall, content: object) -> tuple[str, bool] | None:
    """文件片段、缓存检索和原生像素共用进展身份；换参数不使同一内容变成新证据。"""
    if isinstance(content, list):
        content = next((part.get("text") for part in content
                        if isinstance(part, Mapping) and part.get("type") == "text"), "")
    payload = _decode_tool_payload(content)
    if call.name == "terminal" and payload.get("path") and isinstance(payload.get("stdout"), str):
        try:
            command = shlex.split(str(call.arguments.get("command") or ""))
        except ValueError:
            command = []
        if command[:1] == ["ls"] and payload.get("returncode") == 0:
            identity = _canonical_arguments({"path": payload["path"], "listing": payload["stdout"]})
            return "terminal:directory:" + hashlib.sha256(identity.encode("utf-8")).hexdigest(), True
    if call.name == "read_file" and "content" in payload and payload.get("path"):
        identity = _canonical_arguments({"path": payload.get("logical_path") or payload["path"],
                                         "offset": payload.get("offset", 1), "content": payload["content"]})
        return call.name + ":" + hashlib.sha256(identity.encode("utf-8")).hexdigest(), True
    if call.name in {"web_search", "web_extract"}:
        digest = hashlib.sha256(_canonical_arguments(call.arguments).encode("utf-8")).hexdigest()
        return call.name + ":" + digest, payload.get("cached") is True
    if _is_native_inspection_tool(call.name):
        digest = payload.get("png_sha256") or payload.get("sha256")
        if digest:
            evidence_key = str(payload.get("evidence_key") or "")
            if not evidence_key:
                descriptor = payload.get("pending_inspection") or payload.get("current_inspection") or {}
                descriptor = descriptor if isinstance(descriptor, Mapping) else {}
                state = {"kind": payload.get("kind") or descriptor.get("kind"),
                         "review_stage": payload.get("review_stage") or descriptor.get("review_stage"),
                         "pages": payload.get("pages") or descriptor.get("pages") or payload.get("page"),
                         "sha256": str(digest)}
                evidence_key = hashlib.sha256(_canonical_arguments(state).encode("utf-8")).hexdigest()
            return call.name + ":" + evidence_key, True
    pages = payload.get("pages")
    if call.name in {"render", "terminal"} and isinstance(pages, list) and pages and all(
        isinstance(page, Mapping) and page.get("html_sha256") and page.get("png_sha256") for page in pages
    ):
        state = [{key: page.get(key) for key in ("page", "page_uid", "html_sha256", "png_sha256")} for page in pages]
        identity = _canonical_arguments({"pages": sorted(state, key=lambda p: str(p['page']))})
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return call.name + ":" + digest, True
    return None


def _vision_result_block(
    result_block: Mapping[str, object],
    relative_path: str,
    context: ToolContext,
    *,
    opened_sources: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    """把 vision 工具的文本回执与真实图片放回同一个 tool_result。"""
    workspace_value = context.metadata.get("workspace_root")
    if not isinstance(workspace_value, str) or not workspace_value.strip():
        raise AgentProtocolError(
            "inspect requires workspace_root in ToolContext metadata"
        )
    root = Path(workspace_value).expanduser().resolve()
    target = (root / relative_path).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise AgentProtocolError("vision image path escapes the workspace") from exc
    if not target.is_file():
        raise AgentProtocolError(f"vision image is missing after tool success: {relative_path}")
    payload = target.read_bytes()
    source = image_source(payload)
    receipt = _decode_tool_payload(result_block.get("content")) or {"result": str(result_block.get("content") or "")}
    opened = {"owner_id": context.owner_id, "path": target.relative_to(root).as_posix(),
              "sha256": hashlib.sha256(payload).hexdigest()}
    page = re.fullmatch(r"inputs/extracted/(att-[0-9a-f]{12})/pages/page_(\d{3,})\.png", opened["path"])
    if page and int(page[2]) > 0:
        opened.update(attachment_id=page[1], page=int(page[2]))
    receipt["visual_source"] = {**opened, "transport_media_type": source["media_type"]}
    receipt.update(visual_payload_status="pixels_attached_for_owner_review",
                   pixels_delivered_to_this_owner=True, pixels_attached_in_this_result=True,
                   pixels_active_in_current_context=True)
    value = copy.deepcopy(dict(result_block))
    value["content"] = [
        {"type": "text", "text": json.dumps(receipt, ensure_ascii=False)},
        {"type": "image", "source": source},
    ]
    return value


def _attachment_read_result_block(
    result_block: Mapping[str, object],
    context: ToolContext,
    *,
    image_allowance: int,
    visible_paths: frozenset[str] = frozenset(),
) -> tuple[dict[str, object], tuple[str, ...]]:
    """Attach MinerU figures to a read_file result without invoking visual review."""
    payload = _decode_tool_payload(result_block.get("content"))
    raw_images = payload.pop("_multimodal_images", [])
    if not isinstance(raw_images, list):
        raise AgentProtocolError("read_file multimodal image list must be an array")
    workspace_value = context.metadata.get("workspace_root")
    if not isinstance(workspace_value, str) or not workspace_value.strip():
        raise AgentProtocolError("multimodal read_file requires workspace_root in ToolContext metadata")
    root = Path(workspace_value).expanduser().resolve()
    blocks = []
    public_images = []
    attached_paths = []
    opened_sources = []
    seen = set()
    for item in raw_images:
        if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
            raise AgentProtocolError("read_file multimodal image entry is invalid")
        relative = str(item["path"])
        if relative in seen:
            continue
        seen.add(relative)
        if not context.may_read(relative):
            raise AgentProtocolError(f"read_file returned an image outside the Owner read scope: {relative}")
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise AgentProtocolError("read_file image path escapes the workspace") from exc
        if not target.is_file():
            raise AgentProtocolError(f"read_file image is missing after tool success: {relative}")
        already_visible = relative in visible_paths
        deferred = not already_visible and len(attached_paths) >= image_allowance
        public_images.append({
            "path": relative,
            **({"page": item["page"]} if type(item.get("page")) is int else {}),
            "status": "already_in_context" if already_visible else "deferred" if deferred else "attached",
            **({"reason": "batch_image_limit"} if deferred else {}),
        })
        if already_visible or deferred:
            continue
        original = target.read_bytes()
        blocks.append({"type": "image", "source": image_source(original)})
        opened_sources.append({
            "owner_id": context.owner_id, "path": relative,
            "sha256": hashlib.sha256(original).hexdigest(),
        })
        attached_paths.append(relative)
    if public_images:
        payload["attached_images"] = public_images
    if any(item["status"] == "deferred" for item in public_images):
        payload["multimodal_next_action"] = (
            "Use read_file on this source document with a narrower query and/or offset/limit "
            "to load deferred images in a later turn."
        )
    value = copy.deepcopy(dict(result_block))
    if opened_sources:
        value["opened_visual_sources"] = opened_sources
    value["content"] = [
        {"type": "text", "text": json.dumps(payload, ensure_ascii=False)},
        *blocks,
    ] if blocks else json.dumps(payload, ensure_ascii=False)
    return value, tuple(attached_paths)


__all__ = [
    "AgentContextWindowError",
    "AgentProtocolError",
    "AgentProviderError",
    "AgentRunResult",
    "AgentRuntime",
    "AgentRuntimeError",
    "AgentTurnLimitError",
    "RepeatedToolFailureError",
]
