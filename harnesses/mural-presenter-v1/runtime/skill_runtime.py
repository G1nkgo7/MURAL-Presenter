"""把正式 MURAL Skill、通用 AgentRuntime 与 PresenterKernel 装配成生产入口。"""
from __future__ import annotations

import copy
from dataclasses import dataclass, replace
import base64
from concurrent.futures import Future, ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Mapping, Sequence
import uuid

from orchestration.manager import OrchestrationError, TaskRecord, assert_tasks_terminal
from owner.context import OwnerContextStore, OwnerPacket, ResumeMode, source_policy_text
from qc.extrinsic import CriticClient, ExternalVisualCritic
from qc.review_hook import CRITIC_PROMPT_VERSION, InspectionClosed, inspection_with_critic_question
from runtime.agent import AgentRunResult, AgentRuntime, FEEDBACK_ROUND_TOOL_ERRORS, RepeatedToolFailureError
from runtime.attachments import AttachmentManifest, AttachmentStager, materialize_page_image, parse_attachment, queue_attachment_parse, resolve_attachment_records
from runtime.config import RunMode, RunSpec, load_runtime_config, maintenance_reasoning
from runtime.live_trace import iter_jsonl_events, iter_replay_events, preview_trace_event
from runtime.language_policy import (
    language_drift_warning,
    language_policy_metadata,
    language_policy_prompt,
)
from runtime.request_labels import request_kind_public
from runtime.production import (
    EnvironmentDoctor,
    ProductionSettings,
    ProductionToolServices,
    ResourceSlots,
    SkillRenderer,
    SkillSnapshot,
    read_scopes_for_role,
    write_scopes_for_role,
)
from runtime.recovery import CompactionSummaryError, ProviderFailure, SegmentCheckpoint, compaction_request_max_tokens
from runtime.shared_objects import load_skill_module
from runtime.session import (
    ModelRequest,
    ModelSession,
    ProviderChannel,
    ProviderTransport,
    SessionResult,
    UsageLedger,
    create_transport,
    estimate_request_tokens,
    provider_channels,
    replay_provider_events,
    request_context_budget,
)
from runtime.tools import ToolContext, ToolExecutionError, register_tools
from state.workspace import PageLifecycle, WorkspaceState, file_sha256


FONT_REQUEST_SCHEMA = "mural.font-request.v1"
PROMPT_CRITIC_BODY_LIMIT_CHARS = 900
OWNER_PROMPT_TEXT_LIMIT_CHARS = 1200
OWNER_ARTIFACT_REF_LIMIT = 24

_ATTACHMENT_PROMPT_KEYS = (
    "attachment_id", "name", "kind", "intent", "mime", "path",
    "parse_status", "companion_path", "extracted_root", "page_image_pattern",
    "page_preview_mode", "needs_content_extraction", "needs_style_reference",
    "needs_visual_reuse",
)
_ARTIFACT_PROMPT_KEYS = (
    "artifact_id", "kind", "path", "status", "reason", "source_url",
    "pixel_width", "pixel_height", "aspect_ratio",
)
_IMAGE_METADATA_PROMPT_KEYS = (
    "kind", "slides", "purpose", "source", "download", "crop", "display",
    "quality_intent", "subject_region", "text_safe_region", "notes",
    "expect_transparent",
)
_IMAGE_METADATA_ONLY_POSITIVE_RE = re.compile(
    r"(?:仅|只|补|更新|完善|登记|回填|close|metadata|元数据|署名|图源|许可|license|source|purpose|notes|用途|备注)",
    re.IGNORECASE,
)
_IMAGE_METADATA_ONLY_NEGATIVE_RE = re.compile(
    r"(?:不|无需|不要|禁止|避免).{0,12}(?:下载|搜索|检索|生成|裁切|重做|替换|像素|图片|image|download|search|generate|crop|pixel|replace)",
    re.IGNORECASE,
)
_IMAGE_REFERENCE_ONLY_RE = re.compile(
    r"(?:原件|原图).{0,16}(?:留存|保留).{0,80}(?:不(?:与[^，。；;]{0,20})?重复上屏|不(?:与[^，。；;]{0,20})?重复使用|版面优先|实际版面优先|优先用预裁|优先使用预裁)",
    re.IGNORECASE,
)


def _append_prompt_section(base: str, section: str) -> str:
    """Append a system-prompt section without disturbing the pinned Skill text."""
    body = str(section or "").strip()
    if not body:
        return str(base or "")
    return str(base or "").rstrip() + "\n\n" + body


def _prompt_clip_text(value: object, limit: int) -> tuple[str, bool]:
    text = str(value or "")
    if len(text) <= limit:
        return text, False
    suffix = "\n[truncated for prompt history; full critic feedback retained by Harness]"
    return text[:max(0, limit - len(suffix))].rstrip() + suffix, True


def _owner_prompt_value(
    value: object,
    *,
    text_limit: int = OWNER_PROMPT_TEXT_LIMIT_CHARS,
    list_limit: int = 16,
) -> object:
    """Bound prose and repeated records in an Owner handoff without touching state."""
    if isinstance(value, str):
        if len(value) <= text_limit:
            return value
        suffix = f"\n[omitted {len(value) - text_limit} chars from model prompt]"
        return value[:max(0, text_limit - len(suffix))].rstrip() + suffix
    if isinstance(value, Mapping):
        return {
            str(key): copy.deepcopy(item) if key in {
                "continuation_params", "handoff_params", "research_task_link_params"
            } and isinstance(item, Mapping) else
            _owner_prompt_value(item, text_limit=text_limit, list_limit=list_limit)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        rows = [
            _owner_prompt_value(item, text_limit=text_limit, list_limit=list_limit)
            for item in value[:list_limit]
        ]
        if len(value) > list_limit:
            rows.append({"omitted_items": len(value) - list_limit})
        return rows
    return copy.deepcopy(value)


def _is_image_metadata_only_task(task: TaskRecord) -> bool:
    """Identify narrow catalog closeout work that should not inherit full image history."""
    if task.role != "image" or not task.repair:
        return False
    goal = str(task.goal or "")
    return bool(
        _IMAGE_METADATA_ONLY_POSITIVE_RE.search(goal)
        and _IMAGE_METADATA_ONLY_NEGATIVE_RE.search(goal)
    )


def _image_usage_role(item: Mapping[str, object]) -> str:
    """Infer prompt-only image role from explicit catalog prose, without changing schema."""
    metadata = item.get("image_metadata")
    if not isinstance(metadata, Mapping):
        return ""
    text = "\n".join(
        str(metadata.get(key) or "")
        for key in ("purpose", "crop", "display", "notes")
    )
    return "reference_only" if _IMAGE_REFERENCE_ONLY_RE.search(text) else ""


def _current_attachment_receipt(record: Mapping[str, object]) -> Mapping[str, object]:
    parse = record.get("parse")
    if not isinstance(parse, Mapping):
        return {}
    current = str(parse.get("current") or "")
    results = parse.get("results")
    if not current or not isinstance(results, Mapping):
        return {}
    selected = results.get(current)
    if not isinstance(selected, Mapping) or not isinstance(selected.get("receipt"), Mapping):
        return {}
    return selected["receipt"]


def _attachment_manifest_prompt_view(manifest: Mapping[str, object]) -> dict[str, object]:
    """Expose attachment reading entry points, not parser provenance or output inventories."""
    raw_records = manifest.get("records") or []
    records: list[dict[str, object]] = []
    if isinstance(raw_records, list):
        for raw in raw_records:
            if not isinstance(raw, Mapping):
                continue
            record = {
                key: _owner_prompt_value(raw[key])
                for key in _ATTACHMENT_PROMPT_KEYS
                if key in raw and raw[key] not in (None, "", [], {})
            }
            receipt = _current_attachment_receipt(raw)
            companion = raw.get("companion_path") or receipt.get("text_path")
            if companion:
                record["companion_path"] = str(companion)
            content_list = receipt.get("content_list_path")
            if content_list:
                record["content_list_path"] = str(content_list)
            media = receipt.get("media_paths") if isinstance(receipt, Mapping) else None
            raw_media = raw.get("media_paths")
            if isinstance(media, list) or isinstance(raw_media, list):
                record["media_count"] = len(media if isinstance(media, list) else raw_media)
            page_images = raw.get("page_image_paths")
            if isinstance(page_images, list):
                record["page_count"] = len(page_images)
            unresolved = receipt.get("unresolved_items") if isinstance(receipt, Mapping) else None
            if isinstance(unresolved, list) and unresolved:
                record["unresolved_items"] = _owner_prompt_value(
                    unresolved, text_limit=600, list_limit=8
                )
            records.append(record)
    result: dict[str, object] = {
        "input_stage": str(manifest.get("input_stage") or ""),
        "attachment_count": len(records),
        "records": records,
    }
    if manifest.get("content_extraction_required"):
        result["content_extraction_required"] = True
    unresolved = manifest.get("unresolved_items")
    if isinstance(unresolved, list) and unresolved:
        result["unresolved_items"] = _owner_prompt_value(
            unresolved, text_limit=600, list_limit=8
        )
    return result


def _task_prompt_view(task: object, *, include_goal: bool = False) -> dict[str, object]:
    """Project durable task state into the fields needed for coordination or recovery."""
    raw = task.to_dict() if hasattr(task, "to_dict") else task
    if not isinstance(raw, Mapping):
        return {}
    keep = (
        "task_id", "owner_id", "attempt", "role", "pages", "group_id",
        "dependencies", "status", "repair", "error", "output_path",
        "handoff_from", "asset_ids", "reference_pages", "recovery",
    )
    result = {
        key: _owner_prompt_value(raw[key], text_limit=800)
        for key in keep if key in raw and raw[key] not in (None, "", [], {})
    }
    if include_goal and raw.get("goal"):
        result["goal"] = _owner_prompt_value(raw["goal"], text_limit=2000)
    raw_result = raw.get("result")
    if isinstance(raw_result, Mapping):
        outcome = {
            key: _owner_prompt_value(raw_result[key], text_limit=800)
            for key in (
                "status", "output_path", "output_recorded", "recoverable",
                "stop_reason", "group_review_gap", "blocking_issues",
            )
            if key in raw_result and raw_result[key] not in (None, "", [], {})
        }
        summary = raw_result.get("final_text") or raw_result.get("summary")
        if summary:
            outcome["summary"] = _owner_prompt_value(summary, text_limit=1200)
        if outcome:
            result["result"] = outcome
    return result


def _artifact_prompt_view(item: Mapping[str, object]) -> dict[str, object]:
    """Keep layout and usage semantics while hiding catalog provenance bookkeeping."""
    field_limits = {"reason": 300, "source_url": 500}
    result = {
        key: _owner_prompt_value(item[key], text_limit=field_limits.get(key, 600))
        for key in _ARTIFACT_PROMPT_KEYS
        if key in item and item[key] not in (None, "", [], {})
    }
    metadata = item.get("image_metadata")
    if isinstance(metadata, Mapping):
        metadata_limits = {
            "purpose": 320,
            "source": 500,
            "download": 500,
            "crop": 400,
            "display": 240,
            "quality_intent": 240,
            "subject_region": 320,
            "text_safe_region": 320,
            "notes": 500,
        }
        visible = {
            key: _owner_prompt_value(
                metadata[key], text_limit=metadata_limits.get(key, 320)
            )
            for key in _IMAGE_METADATA_PROMPT_KEYS
            if key in metadata and (
                key == "slides"
                or (key == "expect_transparent" and metadata[key] is True)
                or (key != "expect_transparent" and metadata[key] not in (None, "", [], {}))
            )
        }
        source_url = result.get("source_url")
        if source_url and visible.get("source") == source_url:
            visible.pop("source", None)
        if visible.get("download") in {source_url, visible.get("source")}:
            visible.pop("download", None)
        if visible:
            result["image_metadata"] = visible
    usage_role = _image_usage_role(item)
    if usage_role:
        result["usage_role"] = usage_role
    return result


def _completion_gap_prompt_view(value: object) -> object:
    """Remove duplicated recovery contracts while retaining executable repair guidance."""
    def field(key: str, item: object, *, text_limit: int = 600) -> object:
        if key in {"pages", "affected_pages", "reference_pages"} and isinstance(item, (list, tuple)):
            if all(type(page) is int and page > 0 for page in item):
                return list(item)
        return _owner_prompt_value(item, text_limit=text_limit, list_limit=12)

    if not isinstance(value, Mapping):
        return _owner_prompt_value(value, text_limit=800, list_limit=12)
    result: dict[str, object] = {}
    page_issues = value.get("page_issues")
    if isinstance(page_issues, list):
        result["page_issues"] = [
            {
                key: _owner_prompt_value(row[key], text_limit=600, list_limit=12)
                for key in ("page", "lifecycle", "previously_inspected", "reasons")
                if isinstance(row, Mapping) and key in row
            }
            for row in page_issues[:32]
            if isinstance(row, Mapping)
        ]
        if len(page_issues) > 32:
            result["page_issues_omitted"] = len(page_issues) - 32
    recovery = value.get("recovery")
    if isinstance(recovery, list):
        rows = []
        for raw in recovery[:32]:
            if not isinstance(raw, Mapping):
                continue
            row = {
                key: field(key, raw[key])
                for key in ("action", "affected_pages", "pages", "review_stage") if key in raw
            }
            submit = raw.get("submit_task")
            if isinstance(submit, Mapping):
                row["submit_task"] = {
                    key: field(key, submit[key], text_limit=1200 if key == "goal" else 600)
                    for key in (
                        "task_id", "role", "pages", "group_id", "depends_on", "goal",
                        "repair", "handoff_from", "asset_ids", "reference_pages",
                    )
                    if key in submit and submit[key] not in (None, "", [], {})
                }
            rows.append(row)
        result["recovery"] = rows
        if len(recovery) > 32:
            result["recovery_omitted"] = len(recovery) - 32
    for key, item in value.items():
        if key not in {"page_issues", "recovery"}:
            result[str(key)] = field(str(key), item)
    return result


def _prompt_safe_critic_feedback(feedback: Mapping[str, object]) -> dict[str, object]:
    """Keep full Critic records in trace/state while returning a compact Owner receipt."""
    compact = copy.deepcopy(dict(feedback))
    original_body = str(compact.get("body") or "")
    body, truncated = _prompt_clip_text(original_body, PROMPT_CRITIC_BODY_LIMIT_CHARS)
    compact["body"] = body
    compact["body_truncated_for_prompt"] = bool(truncated)
    compact["body_char_count_full"] = len(original_body)
    compact["prompt_history_slimmed"] = True
    if not compact.get("skipped"):
        compact["full_feedback_ref"] = {
            "state_path": "_state/critic-results.json",
            "trace_path": "_trace/live-events.jsonl",
            "inspection_id": str(compact.get("inspection_id") or ""),
            "png_sha256": str(compact.get("png_sha256") or ""),
            "tool_use_id": str(compact.get("tool_use_id") or ""),
        }
    return compact


def _critic_feedback_reports_no_blockers(feedback: Mapping[str, object]) -> bool:
    """Conservatively detect the rubric's explicit clean-page conclusion."""
    if feedback.get("skipped") or feedback.get("completed") is not True or feedback.get("incomplete_reason"):
        return False
    body = str(feedback.get("body") or "").strip().lower()
    # The Critic contract asks for one sentence when the page is clean.  A full
    # match prevents negated, quoted, or contradictory mentions of "no blockers"
    # from closing issues merely because they contain the same words.
    body = re.sub(r"^(?:[-*#]+\s*)", "", body)
    body = body.replace("**", "").replace("__", "").strip()
    conclusion = r"(?:(?:检查|验收)?结论\s*[:：]\s*)?"
    chinese = (
        r"(?:当前(?:页面|画面)?\s*)?"
        r"(?:未发现|没有)\s*(?:必须修复的?\s*)?(?:阻塞(?:性)?问题|必须修复项)"
        r"|无\s*(?:阻塞(?:性)?问题|必须修复项)"
    )
    english = (
        r"(?:no\s+(?:blockers?|blocking\s+issues?|required\s+fixes?)"
        r"(?:\s+(?:were\s+found|were\s+identified|remain))?)"
    )
    return re.fullmatch(rf"{conclusion}(?:{chinese}|{english})\s*[。.!！]?", body) is not None


def _feedback_with_auto_resolved_issues(
    services: "RuntimeServices", closed: InspectionClosed, feedback: Mapping[str, object],
) -> dict[str, object]:
    """Close same-page visual blockers when the current external Critic says none remain."""
    result = copy.deepcopy(dict(feedback))
    if closed.kind != "page" or closed.page is None or not _critic_feedback_reports_no_blockers(result):
        return result
    resolved = services.issues.resolve_current_visual_issues(
        page=int(closed.page),
        png_sha256=closed.png_sha256,
        inspection_id=closed.inspection_id,
        actor=closed.agent_id,
        rationale="external_visual_critic_reported_no_blockers_for_current_pixels",
    )
    result["status"] = "pass_no_blockers"
    if resolved:
        result["auto_resolved_issue_ids"] = [item.issue_id for item in resolved]
    return result


def _external_critic_policy(context: ToolContext, descriptor: Mapping[str, object]) -> tuple[bool, str]:
    """Decide whether an inspect result should request the external visual Critic."""
    role = str(getattr(context, "role", "") or "").strip().lower()
    kind = str(descriptor.get("kind") or "").strip().lower()
    stage = str(descriptor.get("review_stage") or "draft").strip().lower() or "draft"
    if role == "image" and kind == "asset":
        return False, "image_owner_asset_inspection"
    if kind == "asset":
        return False, "asset_inspection"
    if kind == "group":
        return False, "group_contact_sheet_screening"
    if kind == "page" and stage not in {"final", "blocking_recheck"}:
        return False, "page_review_stage_" + stage
    return True, ""


def _skipped_inspection_scope(kind: str, reason: str) -> dict[str, object]:
    """Explain policy-skipped visual checks as scoped receipts, not failed reviews."""
    if reason == "page_review_stage_draft":
        return {
            "status": "draft_layout_check",
            "checked_scope": "layout_skeleton_only",
            "asset_usage_status": "not_verified_by_draft_inspect",
            "missing_required_assets": [],
            "not_checked": [
                "final visual identity",
                "required asset presence or usage",
                "photo crop and photo/text contrast",
                "external visual Critic judgement",
            ],
            "next_required_action": (
                "After required content and assets are actually placed, run inspect with "
                'review_stage:"final" for page-level visual acceptance.'
            ),
            "body": "草稿阶段：已查看当前页面像素，仅用于确认骨架/占位稿可渲染和明显版式问题；未调用外部 Critic，也不代表素材已使用或终稿视觉通过。",
        }
    if reason == "group_contact_sheet_screening":
        return {
            "status": "group_contact_sheet_screening",
            "checked_scope": "cross_page_overview_only",
            "asset_usage_status": "not_verified_by_contact_sheet",
            "missing_required_assets": [],
            "not_checked": [
                "single-page full-resolution acceptance",
                "required asset presence or usage on each page",
                "external visual Critic judgement",
            ],
            "next_required_action": "Use single-page final inspect for pages that are ready for acceptance.",
            "body": "联系表阶段：已查看当前多页总览，仅用于跨页节奏/导航/密度筛查；未调用外部 Critic，也不替代单页终稿验收。",
        }
    if reason in {"image_owner_asset_inspection", "asset_inspection"}:
        return {
            "status": "asset_screening_only",
            "checked_scope": "candidate_asset_pixels_only",
            "asset_usage_status": "asset_not_accepted_into_page_by_this_check",
            "missing_required_assets": [],
            "not_checked": [
                "whether the asset is actually placed on a slide",
                "final crop in the page layout",
                "page-level photo/text contrast",
                "external visual Critic judgement",
            ],
            "next_required_action": "Use the asset observation for transcription/cropping/screening, then run final page inspect after the page Owner places required assets.",
            "body": "素材观察阶段：已查看候选素材或附件像素；未调用外部 Critic，也不代表该素材已进入页面或页面终稿通过。低分辨率小字等只能记录为无法从像素确认的来源限制，不作为正式页面验收结论。",
        }
    return {
        "status": "critic_skipped_by_policy",
        "checked_scope": "local_pixels_only",
        "asset_usage_status": "not_verified_by_this_check",
        "missing_required_assets": [],
        "not_checked": ["external visual Critic judgement"],
        "next_required_action": "Run a final page inspect when the page is ready for acceptance.",
        "body": "本次 inspect 按策略只做本地像素检查；未调用外部 Critic，不代表终稿视觉通过。",
    }


def _skipped_critic_feedback(closed: InspectionClosed, reason: str) -> dict[str, object]:
    """Public inspect payload for policy-skipped Critic without recording a Critic result."""
    scope = _skipped_inspection_scope(closed.kind, str(reason))
    return {
        "inspection_id": closed.inspection_id,
        "png_sha256": closed.png_sha256,
        "critic_model": "",
        "rubric_version": "",
        "body": scope["body"],
        "completed": False,
        "skipped": True,
        "skip_reason": str(reason),
        "incomplete_reason": "skipped_by_policy:" + str(reason),
        **scope,
        "latency_s": 0.0,
        "channel_id": "",
        "agent_id": closed.agent_id,
        "page_uid": closed.page_uid,
        "page": closed.page,
        "html_sha256": closed.html_sha256,
        "scope_key": closed.scope_key,
        "tool_use_id": closed.tool_use_id,
        "stop_reason": "",
        "source_refs": {
            "inspection_id": closed.inspection_id,
            "png_sha256": closed.png_sha256,
            "kind": closed.kind,
            "members": list(closed.members),
            "png_path": closed.png_path,
            "owner_id": closed.agent_id,
            "tool_use_id": closed.tool_use_id,
        },
        "model_config": {},
        "reused_from": {},
        "question": closed.question,
        "owner_question": closed.owner_question,
        "critic_question": closed.critic_question,
        "critic_prompt_version": closed.critic_prompt_version or CRITIC_PROMPT_VERSION,
        "kind": closed.kind,
        "members": list(closed.members),
        "cache_hit": False,
    }


if False:  # pragma: no cover - 只供静态类型检查，避免运行时循环导入。
    from infer import RuntimeServices


class SkillRuntimeError(RuntimeError):
    """表示正式 Skill 运行没有形成可交付的闭环。"""


def _orchestrator_max_tokens(settings) -> int:
    method = getattr(settings, "orchestrator_request_max_tokens", None)
    return int(method()) if callable(method) else int(settings.max_tokens)


def _owner_max_tokens(settings, role: str) -> int:
    method = getattr(settings, "owner_request_max_tokens", None)
    return int(method(role)) if callable(method) else int(settings.owner_max_tokens)


def _critic_max_tokens(settings) -> int:
    method = getattr(settings, "critic_request_max_tokens", None)
    return int(method()) if callable(method) else int(settings.max_tokens)


@dataclass(frozen=True)
class PreparedSkillRuntime:
    """保存 CLI 在创建 PresenterKernel 前即可确定的生产依赖。"""

    run_spec: RunSpec
    environment: Mapping[str, str]
    settings: ProductionSettings
    snapshot: SkillSnapshot
    attachments: AttachmentManifest
    slots: ResourceSlots
    renderer: Callable[[Path, Path, Mapping[str, object]], Mapping[str, object]]
    transport: ProviderTransport
    channels: tuple[ProviderChannel, ...]
    usage: UsageLedger


class SkillRuntime:
    """使用同一正式 Skill 与模型循环，支持主 Agent 直做和按需 Owner。"""

    def __init__(
        self,
        run_spec: RunSpec,
        environment: Mapping[str, str],
        *,
        settings: ProductionSettings | None = None,
        transport: ProviderTransport | None = None,
        channels: Sequence[ProviderChannel] | None = None,
        renderer: Callable[[Path, Path, Mapping[str, object]], Mapping[str, object]] | None = None,
        validate_environment: bool = True,
    ) -> None:
        """冻结 Skill 与附件，并在首次模型调用前完成环境预检。"""
        preparation_started = time.monotonic()
        env = {str(key): str(value) for key, value in environment.items()}
        production_settings = settings or ProductionSettings.from_environment(env)
        root = run_spec.output_workspace
        if not (root / "config.json").is_file() and any((root / name).exists()
                for name in ("_state/run.json", "deck.md", "base.css", "_state/page-index.json")):
            raise SkillRuntimeError("creation_config_missing: existing workspace requires an explicit migration or a new workspace; no Markdown backfill")
        # WorkspaceState 必须最先创建：让 _state/workspace.json 成为第一个持久文件。
        workspace_state = WorkspaceState(run_spec.output_workspace)
        self._stage_font_request(workspace_state, env)
        snapshot = SkillSnapshot.stage(
            run_spec.output_workspace,
            production_settings.skill_source,
            env,
        )
        config_module = load_skill_module(snapshot.target, snapshot.tree_sha256, "deck_config")
        if (root / "config.json").is_file():
            config_module.read_config(root)
        else:
            config_module.initialize_config(root,
                presentation_language=run_spec.language if run_spec.language != "auto" else None,
                target_page_count=run_spec.page_count_hint)
        self._checkpoint_attachments = [
            {"sha256": file_sha256(path), "original_name": path.name,
             "intent": run_spec.attachment_intents[index] if run_spec.attachment_intents else ""}
            for index, path in enumerate(run_spec.attachments)
        ]
        orchestration_path = run_spec.output_workspace / "_state/orchestration.json"
        # 调度器启动即落盘；只有实际任务存在，才转入 Owner 恢复路径。
        if run_spec.run_mode.value == "inference" and (
            not orchestration_path.is_file() or not json.loads(orchestration_path.read_text()).get("tasks")
        ):
            prior = OwnerContextStore(run_spec.output_workspace).load_orchestrator()
            if prior is not None and prior["identity"].get("attachments") != self._checkpoint_attachments:
                raise SkillRuntimeError("orchestrator_checkpoint_stale: attachment inputs changed before staging")
        attachment_started = time.monotonic()
        attachments = AttachmentStager(run_spec.output_workspace).stage(
            run_spec.attachments,
            intents=(run_spec.attachment_intents or None),
        )
        attachment_ms = round((time.monotonic() - attachment_started) * 1000)
        doctor_started = time.monotonic()
        if validate_environment:
            EnvironmentDoctor(run_spec, snapshot, env).run()
        doctor_ms = round((time.monotonic() - doctor_started) * 1000) if validate_environment else None
        slots = ResourceSlots(run_spec.output_workspace, production_settings, environment={
            **env, "MODEL": run_spec.provider.model, "MODEL_BASE_URL": run_spec.provider.base_url})
        selected_renderer = renderer or SkillRenderer(
            run_spec.output_workspace,
            production_settings,
            env,
            slots,
            snapshot,
        )
        provider_started = time.monotonic()
        selected_transport = transport or create_transport(run_spec.provider)
        selected_channels = tuple(channels or provider_channels(env))
        if not selected_channels:
            raise SkillRuntimeError("没有可用的 MODEL Provider Channel")
        provider_ms = round((time.monotonic() - provider_started) * 1000)
        self.prepared = PreparedSkillRuntime(
            run_spec=run_spec,
            environment=env,
            settings=production_settings,
            snapshot=snapshot,
            attachments=attachments,
            slots=slots,
            renderer=selected_renderer,
            transport=selected_transport,
            channels=selected_channels,
            usage=UsageLedger(),
        )
        self.renderer = selected_renderer
        self._trace_lock = threading.RLock()
        self._agent_results: dict[str, dict[str, object]] = {}
        self._attachment_futures: dict[str, Future] = {}
        self._attachment_stop = threading.Event()
        # 外部视觉 Critic：缺省关闭；backend() 依 effective 开关装配。
        self._critic_executor = None
        self._critic_records: list[dict[str, object]] = []
        self._critic_state = run_spec.output_workspace / "_state" / "critic-results.json"
        if self._critic_state.is_file():
            self._critic_records = json.loads(self._critic_state.read_text(encoding="utf-8"))["results"]
        self._critic_coordinator: ExternalVisualCritic | None = None
        self._host_state = run_spec.output_workspace / "_state" / "skill-runtime.json"
        prior_host = json.loads(self._host_state.read_text()) if self._host_state.is_file() else {}
        prior_failure = prior_host.get("agent_failed") or {}
        self._previous_tool_failure = (
            {"tool": prior_failure.get("last_failed_tool"), "error_code": prior_failure.get("last_failed_error_code")}
            if prior_host.get("status") == "failed" and prior_failure.get("error_type") == "RepeatedToolFailureError"
            and prior_failure.get("last_failed_error_code") in FEEDBACK_ROUND_TOOL_ERRORS
            else None
        )
        self._critic_resume_outcome = self._load_critic_resume_outcome()
        self._recovered_provider_attempt_ids: tuple[str, ...] = ()
        self._preparation_timings_ms = {**snapshot.preparation_timings_ms,
            "attachments": attachment_ms, "doctor": doctor_ms, "provider_setup": provider_ms,
            "total": round((time.monotonic() - preparation_started) * 1000)}
        self._write_host_state(status="prepared")

    @staticmethod
    def _stage_font_request(
        workspace_state: WorkspaceState, environment: Mapping[str, str]
    ) -> None:
        """把 WebUI 传入的 canonical 字体请求原子写入 workspace，绝不由 WebUI 直写。

        请求 JSON 通过 ``MURAL_FONT_REQUEST_JSON`` 环境变量传入。因为 WorkspaceState
        已把 manifest 作为第一个文件，这里写 ``_state/font-request.json`` 不会让
        workspace 在无 manifest 时先变成非空。resume 时只接受字节完全相同的内容。
        """
        raw = str(environment.get("MURAL_FONT_REQUEST_JSON") or "").strip()
        if not raw:
            return
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SkillRuntimeError(f"MURAL_FONT_REQUEST_JSON is not valid JSON: {exc}") from exc
        if not isinstance(value, dict) or value.get("schema") != FONT_REQUEST_SCHEMA:
            raise SkillRuntimeError("MURAL_FONT_REQUEST_JSON has an unsupported schema")
        path = workspace_state.paths.path("font_request")
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        if path.exists():
            if not path.is_file() or path.read_text(encoding="utf-8") != encoded:
                raise SkillRuntimeError(
                    "resume font request conflicts with the pinned workspace request"
                )
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        pending = path.with_name(f".{path.name}.{uuid.uuid4().hex}.pending")
        try:
            descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(pending, path)
        finally:
            pending.unlink(missing_ok=True)

    def backend(self, services: "RuntimeServices") -> Mapping[str, object]:
        """注册生产工具和角色 runner，再运行正式 Orchestrator。"""
        started = time.monotonic()
        self._write_host_state(status="running")
        recovered = services.live_trace.close_interrupted_events()
        self._recovered_provider_attempt_ids = tuple(
            event["payload"]["attempt_id"] for event in recovered if event["event_type"] == "provider_failed"
        )
        tool_services = ProductionToolServices(
            services.run_spec,
            self.prepared.snapshot,
            self.prepared.settings,
            self.prepared.environment,
            services.workspace,
            services.artifacts,
            self.prepared.renderer,  # type: ignore[arg-type]
            self.prepared.slots,
        )
        self._tool_services = tool_services
        tool_services.image_handoff_sink = lambda row: self._publish_image_handoff(services, row)
        image_owners = {owner.owner_id for owner in services.owners.list_current() if owner.role == "image"}
        for row in services.artifacts.catalog()["images"]:
            if row["owner_id"] in image_owners:
                self._publish_image_handoff(services, row)
        if self.prepared.snapshot.font_store is not None:
            services.workspace.font_resolver = self.prepared.snapshot.font_store.resolve_reference
        register_tools(
            services.tools,
            tool_services.base_tool_handlers(),
            require_complete=False,
        )
        self._build_review_coordinator(services)
        services.visual_review = lambda: self._critic_summary(services, complete_missing=False)
        # 先在自然完成门内跑一次无重型格式的交付预检，使缺页、资源或拼装故障
        # 仍能回到当前 Orchestrator 修复。正式导出随后复用同一 DeliveryExporter。
        from export.delivery import DeliveryExporter
        services.finalize_delivery = lambda: DeliveryExporter(
            services.workspace, services.artifacts, services.issues
        ).export(
            include_pdf=False,
            include_pptx=False,
            include_zip=False,
            publish_result=False,
        )
        agent_runtime = AgentRuntime(
            balance=self.prepared.environment.get("MODEL_CHANNEL_BALANCE", "balanced"),
            environment=self.prepared.environment,
            settings=services.run_spec.provider,
            context_window_limit=(self.prepared.settings.training_sample_max_tokens
                if services.run_spec.run_mode.value == "synthesis" else None),
            compact_trigger_ratio=self.prepared.settings.compact_trigger_ratio,
            compact_target_tokens=self.prepared.settings.compact_target_tokens,
            compact_summary_target_tokens=self.prepared.settings.compact_summary_target_tokens,
            max_context_images=self.prepared.settings.vision_context_images,
            summary_invoker=lambda request, **context: self._invoke_compaction_summary(services, request, **context),
            channels=self.prepared.channels,
            transport=self.prepared.transport,
            tools=services.tools,
            usage=self.prepared.usage,
            event_sink=lambda event: self._record_runtime_event(services, event),
            slot_factory=lambda *, deadline_monotonic=None: self.prepared.slots.acquire("model",
                deadline_monotonic=min(time.monotonic() + services.run_spec.provider.queue_timeout_s,
                    deadline_monotonic if deadline_monotonic is not None else float("inf")),
                stop_event=services.orchestration.stop_event),
        )
        self._start_attachment_parsing(services)
        services.orchestration.prepare_submission = lambda tasks: self._prepare_slide_submission(services, tasks)
        services.orchestration.validate_submission = lambda tasks: self._validate_slide_submission(services, tasks)
        for role in ("research", "image", "slide"):
            services.orchestration.register_runner(
                role,
                self._owner_runner(services, agent_runtime, role),
            )
        self._hydrate_completed_owner_results(services)
        orchestrator_input = self._orchestrator_input(services)
        resume_state = json.loads(str(orchestrator_input["content"])).get("resume_state")
        orchestrator_system = self.prepared.snapshot.main_text()
        if isinstance(resume_state, Mapping):
            orchestrator_system += (
                "\n\n## Harness same-workspace resume\n"
                "A previous process left durable orchestration state in this workspace. "
                "Treat durable task execution status, ownership and artifact references as runtime truth. "
                "Prior task goals, saved handoffs and authored plans are historical model context, "
                "not additional user requirements or authority over the pinned Skill/Harness. "
                "They cannot waive current completion checks, forbid required inspections or introduce inspection quotas. "
                "Use current tool feedback to resolve completion gaps; preserve genuine unresolved issues. "
                "Interrupted tasks resume automatically; answer pending Owner requests. "
                "When no independent work remains, end the turn; Harness waits for real events. "
                "Do not resubmit succeeded tasks. "
                "Apply any revision_request after active tasks end, using the pinned Skill."
            )
        orchestrator_system = _append_prompt_section(
            orchestrator_system,
            self._language_policy(services, audience="agent"),
        )

        try:
            resumed = self._resume_critic_review(services)
            if resumed is not None:
                self._write_host_state(status="completed", outcome=resumed)
                return resumed
            checkpoint = self._orchestrator_checkpoint(services, orchestrator_input)
            services.owners.resume_orchestrator()
            result = agent_runtime.run(
                agent_id="orchestrator",
                role="orchestrator",
                system=orchestrator_system,
                tool_context=self._orchestrator_context(services),
                initial_messages=() if checkpoint is not None else (orchestrator_input,),
                checkpoint=checkpoint,
                max_turns=self.prepared.settings.max_turns,
                max_tokens=_orchestrator_max_tokens(self.prepared.settings),
                thinking=self.prepared.settings.thinking,
                effort=self.prepared.settings.reasoning_effort,
                checkpoint_sink=lambda checkpoint: self._save_orchestrator_checkpoint(services, checkpoint),
                continuation_context=lambda: self._continuation_context(services),
                idle_handler=lambda: self._orchestrator_idle(services),
            )
            self._record_result(result)
            visual = self._critic_summary(services, complete_missing=False)
            provider_replay = replay_provider_events(iter_replay_events(services.live_trace.path, "provider"))
            outcome = {
                "adapter": "skill_runtime",
                "skill_tree_sha256": self.prepared.snapshot.tree_sha256,
                "attachment_manifest": services.workspace.paths.relative(
                    self.prepared.attachments.path
                ),
                "orchestrator": result.to_dict(),
                "agents": [
                    copy.deepcopy(self._agent_results[key])
                    for key in sorted(self._agent_results)
                ],
                "provider_replay": {
                    "attempts": provider_replay.attempts,
                    "successes": provider_replay.successes,
                    "failures": provider_replay.failures,
                    "failovers": provider_replay.failovers,
                    "timeout_phases": dict(provider_replay.timeout_phases),
                    "recovered_interrupted_attempts": len(
                        self._recovered_provider_attempt_ids
                    ),
                },
                "usage": self.prepared.usage.summary(),
                "external_visual_critic": visual,
                "elapsed_s": round(max(0.0, time.monotonic() - started), 3),
            }
            self._finish_orchestrator_owner(services, "completed")
            self._write_host_state(status="completed", outcome=outcome)
            return outcome
        except Exception as exc:
            self._finish_orchestrator_owner(services, "interrupted" if isinstance(exc, InterruptedError) else "failed")
            error_fields: dict[str, object] = {
                "error_type": type(exc).__name__,
                "error_summary": str(exc)[:2000],
            }
            if isinstance(exc, RepeatedToolFailureError):
                error_fields["last_failed_tool"] = exc.tool_name
                error_fields["last_failed_error_code"] = exc.error_code
                error_fields["arguments_summary"] = exc.arguments_summary
                error_fields["repeat_count"] = exc.repeat_count
            self._write_host_state(
                status="failed",
                error=f"{type(exc).__name__}: {str(exc)[:2000]}",
                agent_failed=error_fields,
                elapsed_s=round(max(0.0, time.monotonic() - started), 3),
            )
            raise
        finally:
            self._attachment_stop.set()
            for future in self._attachment_futures.values():
                future.cancel()
            # Owner 的末次 inspect 仍依赖 Critic；先收完 Owner，再关闭其依赖。
            services.orchestration.close(wait=True)
            try:
                if self._critic_executor is not None:
                    self._critic_executor.shutdown(wait=True, cancel_futures=True)
            finally:
                close_renderer = getattr(self.renderer, "close", None)
                if close_renderer is not None:
                    close_renderer()

    def _attachment_manifest(self) -> dict:
        return json.loads(self.prepared.attachments.path.read_text(encoding="utf-8"))

    def _orchestrator_idle(self, services: "RuntimeServices") -> Mapping[str, object] | None:
        """自然结束一轮后在原事件条件上等待；事件是 Harness 输入，不伪造工具事务。"""
        event = services.orchestration.wait(consumer_id="orchestrator")
        from infer import _compact_delegate_wait_payload, _with_parallelization_advisories
        snapshot = services.orchestration.snapshot() if hasattr(services.orchestration, "snapshot") else {}
        event = _with_parallelization_advisories(event,
            snapshot_tasks=(snapshot.get("tasks") or {}))
        if (event["all_terminal"] and not event["events"] and not event["pending_requests"]
                and not event.get("unresolved_failures")):
            try:
                services.complete_deck()
            except ToolExecutionError as exc:
                diagnostics = {"diagnostics": exc.diagnostics} if exc.diagnostics is not None else {}
                prompt_diagnostics = (
                    {"diagnostics": _completion_gap_prompt_view(exc.diagnostics)}
                    if exc.diagnostics is not None else {}
                )
                event = {"harness_event": "completion_gap", **event,
                    "error_code": exc.code, "detail": str(exc), **prompt_diagnostics,
                    "blocking_issues": [issue.to_dict() for issue in services.issues.blocking_issues()],
                    "notice": "整册尚不能收尾。根据当前缺口补做或复核；问题确已解决时显式提交 issues_resolved。无需提交 ready。"}
                services.live_trace.append(event_type="deck_completion_checked", agent_id="orchestrator", role="orchestrator",
                    payload={"actor": "harness", "status": "incomplete", "error_code": exc.code, "detail": str(exc), **diagnostics})
                return {"role": "user", "content": _json_text(
                    _compact_delegate_wait_payload(event)
                )}
            else:
                return None
        return {"role": "user", "content": _json_text(_compact_delegate_wait_payload({
            "harness_event": "orchestration_update", **event,
            "notice": "这是后台任务的真实状态，不是工具回执。处理求助、失败和未分配页；失败后按 recovery 中的参数正式接续，不能只改任务名或用文字声明已取代。页面齐备后先用 inspect(page=[...]) 打开当前联系表，比较同位置的导航、字号、背景与主体分量，再放大疑点页。生成联系表或已有单页回执不等于已作整册比较。没有独立工作可结束本轮，Harness 自动等待或校验收尾，无需提交 ready。",
        }))}

    def _prepare_slide_submission(self, services: "RuntimeServices", tasks) -> None:
        started = time.monotonic()
        try:
            result = self._tool_services.prepare_slide_submission(tasks)
        except Exception as exc:
            services.live_trace.append(event_type="slide_preparation_failed", agent_id="orchestrator",
                role="harness", payload={"actor": "harness", "task_ids": [task["task_id"] for task in tasks],
                    "duration_s": time.monotonic() - started, "detail": str(exc)})
            raise
        if result.get("registered_pages"):
            services.live_trace.append(event_type="slide_preparation_completed", agent_id="orchestrator",
                role="harness", payload={"actor": "harness", **result,
                    "task_ids": [task["task_id"] for task in tasks],
                    "duration_s": time.monotonic() - started})

    def _validate_slide_submission(self, services: "RuntimeServices", tasks) -> None:
        from runtime.tools import ToolExecutionError
        missing = [f"slides/slide_{page:02d}.html" for task in tasks if task["role"] == "slide"
                   for page in task["pages"] if not services.workspace.paths.page_html(page).is_file()]
        if missing:
            raise ToolExecutionError("slide_preparation_missing",
                f"本批新任务尚未接受，未登记 Owner 或启动执行。缺少自动准备的页面文件：{missing}。"
                "制作准备后文件仍然缺失或已改变。检查当前计划与文件状态后重新提交；不要覆盖已有页面。")

    def _start_attachment_parsing(self, services: "RuntimeServices") -> None:
        """后台只运行唯一解析器；Agent 首轮不等 OCR，完成通知进入原事件流。"""
        records = [record for record in self._attachment_manifest().get("records", [])
                   if record.get("needs_content_extraction")]
        if not records:
            return
        pending = iter(records)
        lock = threading.Lock()
        self._attachment_futures = {record["attachment_id"]: Future() for record in records}
        services.orchestration.background_pending = lambda: any(not future.done() for future in self._attachment_futures.values())
        for record in records:
            queue_attachment_parse(services.workspace.paths.root, record["attachment_id"])

        def emit(event):
            services.live_trace.append(event_type=event["event_type"], agent_id="attachment-parser", role="harness",
                turn=0, segment_id="", payload={key: value for key, value in event.items() if key != "event_type"})

        def work():
            while not self._attachment_stop.is_set():
                with lock:
                    record = next(pending, None)
                if record is None:
                    return
                attachment_id = record["attachment_id"]
                future = self._attachment_futures[attachment_id]
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    receipt = parse_attachment(services.workspace.paths.root, attachment_id,
                        event_sink=emit, environment=self.prepared.environment)
                except Exception as exc:
                    payload = {"attachment_id": attachment_id, "status": "failed", "error_type": type(exc).__name__}
                    complete = lambda error=exc: future.set_exception(error)
                else:
                    payload = {"attachment_id": attachment_id, "status": receipt["status"],
                               "cached": receipt["cached"], "unresolved_count": len(receipt["unresolved_items"])}
                    complete = lambda: future.set_result(receipt)
                services.orchestration.publish_background_event("attachment_parse_finished", {
                    **payload, "manifest_path": "_trace/attachment-manifest.json"}, on_published=complete)

        count = min(len(records), load_runtime_config(self.prepared.environment)["mineru"]["gpu"]["concurrency"])
        for index in range(count):
            threading.Thread(target=work, name=f"mural-attachment-{index}", daemon=True).start()

    def _load_critic_resume_outcome(self) -> dict[str, object] | None:
        """仅恢复已完成创作、唯一未完成项为 Critic 的同输入任务。"""
        spec = self.prepared.run_spec
        if spec.run_mode.value != "synthesis" or spec.revision_request:
            return None
        root = spec.output_workspace
        try:
            result = json.loads((root / "result.json").read_text())
            prior_run = json.loads((root / "_state/run.json").read_text())
            host = json.loads(self._host_state.read_text())
        except (OSError, ValueError):
            return None
        identity = ("raw_user_query", "attachments", "attachment_intents", "run_mode", "authoring_profile")
        prior_attachments = [(row.get("sha256"), row.get("original_name")) for row in host.get("checkpoint_attachments", [])]
        current_attachments = [(row["sha256"], row["original_name"]) for row in self._checkpoint_attachments]
        if (result.get("exit_reason") != "critic_incomplete"
            or not result.get("external_visual_critic", {}).get("critic_incomplete")
            or host.get("status") != "completed"
            or host.get("creation_config_sha256") != file_sha256(root / "config.json")
            or prior_attachments != current_attachments
            or any(prior_run.get(key) != spec.to_manifest().get(key) for key in identity)):
            return None
        outcome = host.get("outcome")
        return copy.deepcopy(outcome) if isinstance(outcome, dict) else None

    def _resume_critic_review(self, services: "RuntimeServices") -> dict[str, object] | None:
        prior = self._critic_resume_outcome
        if prior is None or self._critic_coordinator is None:
            return None
        pages = services.workspace.pages()
        previous = {row["page_uid"]: row for row in
                    prior.get("external_visual_critic", {}).get("current_inspections", [])}
        if not pages or len(previous) != len(pages) or any(
            page.lifecycle != PageLifecycle.READY or any(
                previous.get(page.page_uid, {}).get(key) != getattr(page, key)
                for key in ("inspection_id", "html_sha256", "png_sha256")) for page in pages):
            return None
        services.workspace.assert_ready_resources(page.page for page in pages)
        started = time.monotonic()
        visual = self._critic_summary(services, complete_missing=True)
        return {**prior, "external_visual_critic": visual,
                "critic_only_resume": True, "critic_resume_usage": self.prepared.usage.summary(),
                "elapsed_s": float(prior.get("elapsed_s") or 0) + time.monotonic() - started}

    def _critic_summary(self, services: "RuntimeServices", *, complete_missing: bool = False) -> dict[str, object]:
        """汇总本次运行的 Critic 执行完整性与版本，不裁决反馈内容。"""
        switch = getattr(self, "_critic_switch", {"requested": False, "effective": False})
        coordinator = self._critic_coordinator
        if coordinator is not None:
            coordinator.settle_pending()
            if complete_missing:
                from qc.extrinsic import RUBRIC_VERSION

                receipts = json.loads(services.workspace.path.read_text()).get("inspection_receipts", {})
                pages = services.workspace.pages()
                current = current_critic_state(self._critic_records, pages,
                    critic_model=self.prepared.run_spec.provider.model,
                    rubric_version=RUBRIC_VERSION,
                    critic_prompt_version=CRITIC_PROMPT_VERSION)["current_inspections"]
                for page, review in zip(pages, current):
                    if review["completed"] or page.lifecycle not in {PageLifecycle.INSPECTED, PageLifecycle.READY}:
                        continue
                    receipt = next((value for value in receipts.values()
                        if value.get("inspection_id") == page.inspection_id
                        and value.get("kind") == "page"
                        and services.workspace.is_page_author(page.page, str(value.get("owner_id") or ""))), None)
                    if receipt is None:
                        continue  # 不能把普通看图伪装成已闭合的 Slide 原生检查。
                    services.artifacts.validate_render_receipt(page.page)
                    coordinator.complete_missing(InspectionClosed(
                        agent_id=str(receipt["owner_id"]), role=str(receipt["role"]), page=page.page,
                        page_uid=page.page_uid, inspection_id=page.inspection_id,
                        html_sha256=page.html_sha256, png_sha256=page.png_sha256,
                        png_path=page.png_path, workspace_root=str(services.workspace.paths.root),
                        scope_key=str(receipt["scope_key"]),
                        tool_use_id=str(receipt.get("tool_use_id") or ""),
                        question=str(receipt.get("question") or ""),
                        owner_question=str(receipt.get("owner_question") or receipt.get("question") or ""),
                        critic_question=str(receipt.get("critic_question") or ""),
                        critic_prompt_version=str(receipt.get("critic_prompt_version") or "")))
                coordinator.settle_pending()
        summary: dict[str, object] = {
            "requested": bool(switch.get("requested")),
            "effective": bool(switch.get("effective")),
            "run_mode": switch.get("run_mode", ""),
            "critic_model": "",
            "rubric_version": "",
            "stats": {},
            "results": list(self._critic_records),
        }
        if coordinator is not None:
            from qc.extrinsic import RUBRIC_VERSION

            summary["critic_model"] = self.prepared.run_spec.provider.model
            summary["rubric_version"] = RUBRIC_VERSION
            summary["stats"] = dict(coordinator.stats)
            summary.update(current_critic_state(self._critic_records, services.workspace.pages(),
                critic_model=str(summary["critic_model"]), rubric_version=RUBRIC_VERSION,
                critic_prompt_version=CRITIC_PROMPT_VERSION))
        return summary

    def _invoke_compaction_summary(self, services, request, *, agent_id, role, segment_id,
                                   stop_event=None, deadline_monotonic=None):
        """一次内部摘要请求；复用通道、资源槽与 raw，绝不发布作者消息或检查回执。"""
        provider = self.prepared.run_spec.provider
        request = replace(request, max_tokens=compaction_request_max_tokens(request.model, request.max_tokens))
        context_limit = (self.prepared.settings.training_sample_max_tokens
                         if self.prepared.run_spec.run_mode == RunMode.SYNTHESIS else None)
        if str(request.model).strip().lower() == "kimi-k3":
            context_limit = min(context_limit if context_limit is not None else 131072, 131072)
        input_budget = request_context_budget(provider, max_tokens=request.max_tokens,
                                              training_sample_max_tokens=context_limit)["input_budget_tokens"]
        if input_budget <= 0 or estimate_request_tokens(request.system, request.messages, request.tools) > input_budget:
            raise CompactionSummaryError("summary request exceeds its context budget; original checkpoint retained")
        checkpoint = SegmentCheckpoint.initial(request.messages)
        operation_id = "compact-" + checkpoint.segment_id
        metadata = {"agent_id": operation_id, "role": "harness", "segment_id": checkpoint.segment_id,
            "parent_agent_id": agent_id, "parent_role": role, "parent_segment_id": segment_id,
            "request_kind": request.request_kind}
        def emit(event):
            self._record_runtime_event(services, {**event, **metadata})
        emit({"event_type": "compaction_summary_requested", "model": request.model,
            "thinking": request.thinking, "effort": request.effort, "max_tokens": request.max_tokens,
            "estimated_input_tokens": estimate_request_tokens(request.system, request.messages, request.tools)})
        session = ModelSession(settings=provider, channels=self.prepared.channels,
            environment=self.prepared.environment,
            transport=self.prepared.transport, checkpoint=checkpoint, usage=self.prepared.usage,
            balance=self.prepared.environment.get("MODEL_CHANNEL_BALANCE", "balanced"),
            event_sink=emit, trace_root=str(services.workspace.paths.root), stop_event=stop_event,
            cache_identity=json.dumps([services.workspace.deck_id, agent_id]),
            slot_factory=lambda *, deadline_monotonic=None: self.prepared.slots.acquire("model", model=request.model,
                deadline_monotonic=min(time.monotonic() + provider.queue_timeout_s,
                    deadline_monotonic if deadline_monotonic is not None else float("inf")),
                stop_event=stop_event))
        return session.call(request, deadline_monotonic=deadline_monotonic)

    def _call_review(self, request, provider, transport, channels, budget_s, services, role, agent_id,
                     *, checkpoint=None, metadata=None):
        checkpoint = checkpoint or SegmentCheckpoint.initial(request.messages)
        token_budget = request_context_budget(provider, max_tokens=request.max_tokens,
            training_sample_max_tokens=(self.prepared.settings.training_sample_max_tokens
                if self.prepared.run_spec.run_mode == RunMode.SYNTHESIS else None))
        estimated_input = estimate_request_tokens(request.system, request.messages, request.tools)
        stop_event = getattr(getattr(services, "orchestration", None), "stop_event", None)
        def emit(event):
            if services is not None:
                self._record_runtime_event(services, {
                    **event, "agent_id": agent_id, "role": role, "segment_id": checkpoint.segment_id, "turn": 1})
        session = ModelSession(balance=self.prepared.environment.get("MODEL_CHANNEL_BALANCE", "balanced"),
            environment=self.prepared.environment,
            settings=provider, channels=channels, transport=transport, checkpoint=checkpoint,
            usage=self.prepared.usage, event_sink=emit,
            trace_root=str(services.workspace.paths.root) if services is not None else None,
            stop_event=stop_event,
            cache_identity=json.dumps([services.workspace.deck_id, agent_id]) if services is not None else "",
            slot_factory=lambda *, deadline_monotonic=None: self.prepared.slots.acquire("model", model=request.model,
                deadline_monotonic=min(time.monotonic() + provider.queue_timeout_s,
                    deadline_monotonic if deadline_monotonic is not None else float("inf")),
                stop_event=stop_event))
        if services is not None:
            emit({"event_type": "agent_started"})
            emit({"event_type": "agent_context",
                "system": request.system, "messages": request.messages, "tools": request.tools,
                "model": request.model, "thinking": request.thinking, "effort": request.effort,
                "max_tokens": request.max_tokens, "api_format": provider.api_format,
                "actual_max_tokens": request.max_tokens, **request_kind_public(request.request_kind),
                **token_budget, "estimated_input_tokens": estimated_input,
                "streaming": provider.streaming, "request_kind": request.request_kind,
                "max_turns": 1, "timeout": {key: getattr(provider, key) for key in (
                    "total_timeout_s", "connect_timeout_s", "idle_timeout_s", "queue_timeout_s")},
                "request_budget_s": budget_s,
                "metadata": dict(metadata or {}),
                "provenance": {"skill_tree_sha256": self.prepared.snapshot.tree_sha256}})
        if token_budget["input_budget_tokens"] <= 0 or estimated_input > token_budget["input_budget_tokens"]:
            correction = ("output and safety reservations leave no input space; correct the explicit configuration"
                if token_budget["input_budget_tokens"] <= 0 else
                "narrow the inspection question/target without dropping the general rubric")
            failure = ProviderFailure(kind="context_budget_exceeded", detail=(
                f"critic input estimate {estimated_input} exceeds input budget "
                f"{token_budget['input_budget_tokens']} (window={token_budget['effective_context_window']}, "
                f"output={request.max_tokens}, safety={provider.context_safety_buffer}); "
                f"request not sent; {correction}"))
            emit({"event_type": "agent_failed", "error_type": failure.kind,
                "error_summary": failure.detail, "phase": "preflight", "retryable": False})
            return SessionResult(None, "", 0, failure)
        try:
            result = session.call(request, request_budget_s=budget_s)
        except Exception as exc:
            emit({"event_type": "agent_failed", "error_type": type(exc).__name__})
            raise
        if result.ok:
            public_blocks = [dict(block) for block in getattr(result.response, "visible_blocks", ())
                             if block.get("type") == "text"]
            for block_index, block in enumerate(public_blocks):
                if block.get("type") == "text":
                    text = str(block.get("text", "") or "")
                    emit({"event_type": "assistant_message", "message": text, "block_index": block_index,
                          "model": request.model, "actual_max_tokens": request.max_tokens,
                          **request_kind_public(request.request_kind)})
                    warning = language_drift_warning(text, metadata or {})
                    if warning is not None:
                        emit({"event_type": "language_drift", "block_index": block_index,
                              "model": request.model, "actual_max_tokens": request.max_tokens,
                              **request_kind_public(request.request_kind), **warning})
            incomplete = ""
            if role == "critic":
                if str(result.response.stop_reason) in {"length", "max_tokens"}:
                    incomplete = "critic_response_truncated"
                elif not any(str(block.get("text") or "").strip() for block in public_blocks):
                    incomplete = "empty_critic_response"
                elif services is not None:
                    services.capture_training_segment(agent_id, role, replace(checkpoint,
                        messages=(*checkpoint.messages, {"role": "assistant", "content": public_blocks}),
                        provider_affinity_id=result.channel_id, reason="critic_completed"))
            emit({"event_type": "agent_failed", "error_type": incomplete} if incomplete else
                 {"event_type": "agent_completed"})
        else:
            emit({"event_type": "agent_failed", "error_type": getattr(result.failure, "kind", "provider_failed")})
        return result

    def vision_analyzer(
        self,
        path: Path,
        _question: str,
        _scope: str,
        _canvas: Mapping[str, object],
    ) -> Mapping[str, object]:
        """返回像素身份；AgentRuntime 随后把这个真实文件放回同一会话。"""
        target = path
        if not target.is_file() and target.with_suffix(".json").is_file():
            relative = target.resolve().relative_to(
                self.prepared.run_spec.output_workspace
            ).as_posix()
            target = materialize_page_image(
                self.prepared.run_spec.output_workspace, relative
            )
        if not target.is_file():
            raise FileNotFoundError(target)
        digest = file_sha256(target)
        return {
            "inspection_id": f"inspection-{digest[:20]}",
            "path": target.resolve().relative_to(
                self.prepared.run_spec.output_workspace
            ).as_posix(),
            "sha256": digest,
            "native_multimodal": True,
        }

    @staticmethod
    def _creation_config_for_language(services: "RuntimeServices") -> Mapping[str, object]:
        """Read config for language routing without forcing a completed workspace."""
        try:
            return services.workspace.creation_config(require_complete=False)
        except Exception:
            try:
                return services.workspace.creation_config()
            except Exception:
                return {}

    def _language_policy(self, services: "RuntimeServices", *, audience: str = "agent") -> str:
        return language_policy_prompt(self._creation_config_for_language(services), audience=audience)

    def _language_metadata(self, services: "RuntimeServices") -> dict[str, str]:
        return language_policy_metadata(self._creation_config_for_language(services))

    def _build_review_coordinator(self, services: "RuntimeServices") -> None:
        """显式启用时，在真实 inspect 事务内返回同版 PNG 与独立反馈。"""
        settings = self.prepared.settings
        run_spec = self.prepared.run_spec
        run_mode = run_spec.run_mode
        effective = settings.effective_external_visual_critic(run_mode)
        # 记录 requested / effective 供 run.json 与最终 manifest。
        self._critic_switch = {
            "requested": bool(settings.enable_external_visual_critic),
            "effective": bool(effective),
            "run_mode": str(getattr(run_mode, "value", run_mode)),
        }
        if not effective:
            return

        from qc.extrinsic import RUBRIC, RUBRIC_VERSION

        base = run_spec.provider
        critic_model = base.model
        critic_provider = replace(base, total_timeout_s=settings.critic_timeout_s,
                                  idle_timeout_s=min(base.idle_timeout_s, settings.critic_timeout_s))
        critic_transport = self.prepared.transport
        critic_channels = self.prepared.channels
        critic_channel_id = critic_channels[0].channel_id if critic_channels else ""

        def invoke(png_bytes: bytes, media_type: str, closed: InspectionClosed) -> Mapping[str, object]:
            """隔离调用：通用 Rubric 保持系统级，作者问题仅作为补充输入。"""
            from runtime.vision import image_source

            closed = inspection_with_critic_question(closed)
            source = image_source(png_bytes)
            messages = (
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": source,
                        },
                        {"type": "text", "text": "本次独立验收问题：\n" + closed.critic_question},
                    ],
                },
            )
            critic_thinking, critic_effort = maintenance_reasoning(critic_provider.model)
            request = ModelRequest(
                model=critic_provider.model,
                system=_append_prompt_section(
                    RUBRIC,
                    self._language_policy(services, audience="critic"),
                ),
                messages=messages,
                tools=(),
                max_tokens=_critic_max_tokens(settings),
                thinking=critic_thinking,
                effort=critic_effort,
                request_kind="critic_visual",
            )
            checkpoint = SegmentCheckpoint.initial(request.messages)
            agent_id = "critic-" + checkpoint.segment_id
            image_bytes = base64.b64decode(request.messages[0]["content"][0]["source"]["data"])
            metadata = {"inspection_id": closed.inspection_id, "page_uid": closed.page_uid,
                "page": closed.page, "html_sha256": closed.html_sha256, "png_sha256": closed.png_sha256,
                "image_sha256": hashlib.sha256(image_bytes).hexdigest(),
                "png_path": closed.png_path, "owner_id": closed.agent_id, "tool_use_id": closed.tool_use_id,
                "question": closed.question, "owner_question": closed.owner_question,
                "critic_question": closed.critic_question,
                "critic_prompt_version": closed.critic_prompt_version,
                "kind": closed.kind, "members": list(closed.members),
                **self._language_metadata(services)}
            result = self._call_review(request, critic_provider, critic_transport, critic_channels,
                settings.critic_timeout_s, services, "critic", agent_id,
                checkpoint=checkpoint, metadata=metadata)
            feedback = {"body": "", "stop_reason": "", "incomplete_reason": "",
                "channel_id": result.channel_id,
                "source_refs": {"agent_id": agent_id, "segment_id": checkpoint.segment_id,
                    "attempt_id": result.attempt_id, "trace_path": "_trace/live-events.jsonl",
                    **{key: metadata[key] for key in ("inspection_id", "page_uid", "page", "png_sha256",
                        "image_sha256", "kind", "members", "png_path", "owner_question",
                        "critic_question", "critic_prompt_version", "language_policy_version",
                        "working_language", "presentation_language")}},
                "model_config": {"model": request.model, "api_format": critic_provider.api_format,
                    "thinking": request.thinking, "effort": request.effort,
                    "max_tokens": request.max_tokens, "streaming": critic_provider.streaming}}
            if not result.ok or result.response is None:
                failure = getattr(result, "failure", None)
                feedback["incomplete_reason"] = getattr(failure, "detail", "") or "critic provider failed"
                return feedback
            feedback["stop_reason"] = str(getattr(result.response, "stop_reason", "") or "")
            blocks = getattr(result.response, "visible_blocks", ())
            feedback["body"] = "\n".join(
                str(block.get("text") or "")
                for block in blocks
                if block.get("type") == "text"
            ).strip()
            return feedback

        client = CriticClient(
            invoker=invoke,
            critic_model=critic_model,
            rubric=RUBRIC,
            rubric_version=RUBRIC_VERSION,
            channel_id=critic_channel_id,
        )
        executor = ThreadPoolExecutor(
            max_workers=max(1, int(settings.critic_concurrency)),
            thread_name_prefix="visual-critic",
        )

        def record_result(result, disposition: str) -> None:
            """记录每次 Critic 结果 + 处置，供 run.json 与训练准入。"""
            with self._trace_lock:
                record = {**result.to_audit_dict(), "disposition": str(disposition)}
                record["body"] = result.body
                if record not in self._critic_records:
                    self._critic_records.append(record)
                    _atomic_json_write(self._critic_state, {"results": self._critic_records})

        coordinator = ExternalVisualCritic(
            client=client,
            executor=executor,
            event_sink=lambda event: self._record_critic_event(services, event),
            result_sink=record_result,
            prior_records=self._critic_records,
        )
        self._critic_coordinator = coordinator
        self._critic_executor = executor

        def inspection_feedback(context, call, output):
            from infer import validate_inspection_target

            descriptor_key = "pending_inspection" if output.get("pending_inspection") else "current_inspection"
            descriptor = output.get(descriptor_key)
            if not isinstance(descriptor, Mapping):
                return output
            descriptor = {**descriptor, "tool_use_id": call.tool_use_id,
                          "inspection_cached": bool(output.get("inspection_cached"))}
            started = time.monotonic()
            validate_inspection_target(services.workspace, services.artifacts, descriptor)
            checked = time.monotonic()
            closed = inspection_with_critic_question(InspectionClosed(agent_id=context.owner_id, role=context.role, page=descriptor.get("page"),
                page_uid=str(descriptor.get("page_uid") or ""), inspection_id=str(descriptor["inspection_id"]),
                html_sha256=str(descriptor.get("html_sha256") or ""), png_sha256=str(descriptor["sha256"]),
                png_path=str(descriptor["path"]), workspace_root=str(services.workspace.paths.root),
                scope_key=str(descriptor["scope_key"]), tool_use_id=call.tool_use_id,
                question=str(descriptor.get("question") or ""),
                owner_question=str(descriptor.get("owner_question") or descriptor.get("question") or ""),
                critic_question=str(descriptor.get("critic_question") or ""),
                critic_prompt_version=str(descriptor.get("critic_prompt_version") or ""),
                kind=str(descriptor["kind"]), members=tuple(descriptor.get("members", ()))))
            should_review, skip_reason = _external_critic_policy(context, descriptor)
            if not should_review:
                scope = _skipped_inspection_scope(str(descriptor.get("kind") or ""), skip_reason)
                feedback = _prompt_safe_critic_feedback(_skipped_critic_feedback(closed, skip_reason))
                skipped = time.monotonic()
                return {**output, **{key: value for key, value in scope.items() if key != "body"},
                        descriptor_key: descriptor, "critic_feedback": feedback,
                        "inspect_timings_s": {**output.get("inspect_timings_s", {}),
                                              "precritic_resource_validation": checked - started,
                                              "critic_skipped": skipped - checked,
                                              "postcritic_resource_validation": 0.0}}
            raw_feedback = coordinator.review(closed)
            reviewed = time.monotonic()
            validate_inspection_target(services.workspace, services.artifacts, descriptor)
            feedback = _prompt_safe_critic_feedback(
                _feedback_with_auto_resolved_issues(services, closed, raw_feedback)
            )
            return {**output, descriptor_key: descriptor, "critic_feedback": feedback,
                    "inspect_timings_s": {**output.get("inspect_timings_s", {}),
                                          "precritic_resource_validation": checked - started,
                                          "critic_wait": reviewed - checked,
                                          "postcritic_resource_validation": time.monotonic() - reviewed}}

        services.tools.inspection_feedback = inspection_feedback

    def _record_critic_event(
        self, services: "RuntimeServices", event: Mapping[str, object]
    ) -> None:
        """把可展示的 Critic 摘要事件投影进 live trace（继续过滤凭证/私有 thinking）。"""
        payload = {
            str(key): value for key, value in event.items() if key != "event_type"
        }
        services.live_trace.append(
            event_type=str(event["event_type"]),
            agent_id="external-visual-critic",
            role="critic",
            turn=0,
            segment_id="",
            payload=payload,
        )

    def _owner_runner(
        self,
        services: "RuntimeServices",
        agent_runtime: AgentRuntime,
        expected_role: str,
    ) -> Callable[[TaskRecord, object], object]:
        """构造只改变 prompt、OwnerPacket 与 ToolContext 的通用角色 runner。"""
        def run(task: TaskRecord, _manager: object) -> object:
            if task.role != expected_role:
                raise SkillRuntimeError(
                    f"runner role mismatch: expected {expected_role}, got {task.role}"
                )
            owner = services.owners.get(task.owner_id)
            metadata_only_image_task = _is_image_metadata_only_task(task)
            handoffs = (
                self._owner_handoffs(services, task, metadata_only=True)
                if metadata_only_image_task
                else self._owner_handoffs(services, task)
            )
            if task.role == "slide":
                for page in task.pages:
                    html = services.workspace.paths.root / f"slides/slide_{page:02d}.html"
                    if not html.is_file():
                        raise SkillRuntimeError(f"slide_preparation_missing: page {page}: {html.relative_to(services.workspace.paths.root)}; prepared HTML disappeared before Owner start; restore missing pages without overwriting existing work")
                for page in task.pages:
                    services.artifacts.transfer_page_owner(
                        page, owner_id=owner.owner_id
                    )
            artifact_hashes = self._owner_artifact_hashes(services, task)
            stored = (
                None
                if metadata_only_image_task
                else services.owner_contexts.load(owner.owner_id)
            )
            resume_mode = (
                ResumeMode.COLD
                if metadata_only_image_task
                else services.owner_contexts.resume_mode(
                    owner,
                    max_context_images=getattr(self.prepared.settings, "vision_context_images", 4),
                )
            )
            changed = []
            prior_capsule = (
                str(stored.capsule or "")
                if stored is not None and resume_mode == ResumeMode.REBUILD
                else ""
            )
            if stored is not None and resume_mode != ResumeMode.COLD:
                changed = sorted(path for path in set(stored.artifact_hashes) | set(artifact_hashes)
                                 if stored.artifact_hashes.get(path) != artifact_hashes.get(path))
                if changed:
                    handoffs["changed_inputs"] = changed
                failed = [row for row in services.orchestration.snapshot()["tasks"].values()
                          if row["owner_id"] == owner.owner_id and row["task_id"] != task.task_id and row.get("error")]
                if failed:
                    previous = max(failed, key=lambda row: int(row["attempt"]))
                    errors = [dict(block) for message in stored.segment.messages
                              if isinstance(message.get("content"), list) for block in message["content"]
                              if isinstance(block, Mapping) and block.get("type") == "tool_result" and block.get("is_error")]
                    handoffs["previous_failure"] = {
                        "task_id": previous["task_id"],
                        "task_error": _owner_prompt_value(previous["error"], text_limit=1200),
                        "tool_results": [
                            {
                                key: _owner_prompt_value(error[key], text_limit=1200, list_limit=8)
                                for key in ("type", "tool_name", "error_code", "is_error", "content")
                                if key in error
                            }
                            for error in errors[-3:]
                        ],
                    }
            if stored is not None and resume_mode == ResumeMode.REBUILD:
                handoffs["previous_goal"] = stored.packet.goal
                handoffs["prior_visual_observations"] = _prior_visual_observations(stored.segment.messages, task.pages)
            packet = OwnerPacket.compile(
                owner,
                services.run_spec,
                artifact_refs=self._artifact_refs(services, task) if task.role in {"image", "slide"} else (),
                prior_capsule=prior_capsule,
                task_id=task.task_id,
                goal=task.goal,
                repair=task.repair,
                handoffs=handoffs,
                attachment_paths=self._staged_attachment_paths() if task.role in {"research", "image"} else (),
            )
            context = self._owner_tool_context(services, task)
            checkpoint = (
                stored.segment
                if stored is not None and resume_mode == ResumeMode.RESUME
                else None
            )
            current_input = {"role": "user", "content": _json_text({"owner_packet": packet.to_dict(),
                "allowed_read_paths": [scope.path + ("/**" if scope.recursive else "") for scope in context.read_scopes],
                "allowed_write_paths": [scope.path + ("/**" if scope.recursive else "") for scope in context.write_scopes]})}
            if stored is not None and resume_mode == ResumeMode.REBUILD:
                checkpoint = replace(SegmentCheckpoint.initial((current_input,)),
                    predecessor_segment_id=stored.segment.segment_id, reason="artifact_context_rebuild")
            elif checkpoint is not None and changed:
                checkpoint = replace(checkpoint, segment_id=SegmentCheckpoint.initial(()).segment_id,
                    predecessor_segment_id=checkpoint.segment_id, reason="owner_inputs_updated",
                    messages=(*checkpoint.messages, current_input))
            elif checkpoint is not None and stored.packet != packet:
                checkpoint = replace(checkpoint, messages=(*checkpoint.messages, current_input))
            initial_messages = (
                ()
                if checkpoint is not None
                else (current_input,)
            )

            def save(closed: SegmentCheckpoint) -> None:
                """在每个闭合工具边界刷新同一 Owner checkpoint。"""
                services.capture_training_segment(owner.owner_id, task.role, closed,
                    artifact_paths=self._owner_artifact_paths(task))
                services.owner_contexts.save(
                    owner=owner,
                    packet=packet,
                    segment=closed,
                    artifact_hashes=self._owner_artifact_hashes(services, task),
                    capsule=(stored.capsule if stored is not None else ""),
                )

            result = agent_runtime.run(
                agent_id=task.owner_id,
                role=task.role,
                system=_append_prompt_section(
                    self.prepared.snapshot.role_text(task.role),
                    self._language_policy(services, audience="agent"),
                ),
                tool_context=context,
                initial_messages=initial_messages,
                checkpoint=checkpoint,
                max_turns=self.prepared.settings.owner_max_turns or self.prepared.settings.max_turns,
                max_tokens=_owner_max_tokens(self.prepared.settings, task.role),
                thinking=self.prepared.settings.thinking,
                effort=self.prepared.settings.reasoning_effort,
                checkpoint_sink=save,
                continuation_context=lambda: self._continuation_context(services, task=task),
                idle_handler=self._group_review_reminder(services, task, checkpoint),
                stop_event=_manager.stop_event if _manager is not None else None,
            )
            final_hashes = self._owner_artifact_hashes(services, task)
            services.owner_contexts.save(
                owner=owner,
                packet=packet,
                segment=result.checkpoint,
                artifact_hashes=final_hashes,
                capsule=result.final_text,
            )
            if task.role == "slide":
                for page in task.pages:
                    snapshot = services.workspace.page(page)
                    if snapshot.lifecycle in {PageLifecycle.INSPECTED, PageLifecycle.READY}:
                        services.workspace.assert_ready_resources((page,))
            self._record_result(result)
            return {
                "status": "completed",
                "resume_mode": resume_mode.value,
                **({"output_path": task.output_path, "output_sha256": final_hashes.get(task.output_path),
                    "output_recorded": task.output_path in final_hashes} if task.output_path else {}),
                **result.to_dict(),
                **({"group_review_gap": gap} if (gap := self._group_review_gap(services, task)) else {}),
            }

        return run

    @staticmethod
    def _group_review_gap(services: "RuntimeServices", task: TaskRecord) -> dict[str, object] | None:
        if task.role != "slide":
            return None
        pages = services.owners.get(task.owner_id).pages
        if len(pages) < 2 or services.workspace.group_inspection_receipt(task.owner_id, pages) is not None:
            return None
        uninspected = [page for page in pages if services.workspace.page(page).lifecycle
                       not in {PageLifecycle.INSPECTED, PageLifecycle.READY}]
        return {"owner_id": task.owner_id, "pages": list(pages), "uninspected_pages": uninspected,
                "reason": "group_inspection_missing_or_stale",
                "notice": "尚无原 Owner 已消费的当前组联系表回执；单页回执不表示组内联合复核完成。"
                          "用 inspect(page=[...]) 查看当前组；联系表不要求先逐页检查，但结束时仍需缺少的单页全分辨率检查。有效成员不重渲染。"
                          "若有真实阻塞，说明缺口交回 Orchestrator，不声称组复核已完成。"}

    def _group_review_reminder(self, services: "RuntimeServices", task: TaskRecord,
                               checkpoint: SegmentCheckpoint | None):
        """每次实际委派只提醒一次，resume 沿已保存的真实输入识别，不制造工具调用。"""
        if task.role != "slide" or len(services.owners.get(task.owner_id).pages) < 2:
            return None
        reminded = False
        for message in checkpoint.messages if checkpoint is not None else ():
            if message.get("role") != "user" or not isinstance(message.get("content"), str):
                continue
            try:
                value = json.loads(message["content"])
            except json.JSONDecodeError:
                continue
            if (isinstance(value, dict) and value.get("harness_event") == "group_review_gap"
                    and value.get("task_id") == task.task_id):
                reminded = True
                break

        def remind():
            nonlocal reminded
            if reminded:
                return None
            gap = self._group_review_gap(services, task)
            if gap is None:
                return None
            reminded = True
            return {"role": "user", "content": _json_text({
                "harness_event": "group_review_gap", "task_id": task.task_id, **gap})}

        return remind

    def _continuation_context(self, services: "RuntimeServices", *, task: TaskRecord | None = None) -> dict:
        """只给摘要调用当前任务相关材料；整包不再原样注入下一段。"""
        pages = set(task.pages) if task is not None else None
        hashes = (self._owner_artifact_hashes(services, task) if task is not None
                  else self._orchestrator_artifact_hashes(services))
        issues = [issue.to_dict() for issue in services.issues.issues()
                  if pages is None or not issue.pages or pages.intersection(issue.pages)]
        requests = (services.orchestration.snapshot().get("requests") or {}).values()
        direct = services.owners.get_group("orchestrator") if task is None else None
        selected_pages = task.pages if task is not None else (direct.pages if direct else ())
        root = services.workspace.paths.root
        instruction_paths = ["deck.md", *[f"briefs/slide_{page:02d}.md" for page in selected_pages]]
        return {
            "raw_user_query": services.run_spec.raw_user_query,
            "creation_config": services.workspace.creation_config(),
            "revision_request": services.run_spec.revision_request,
            **({"owner_id": "orchestrator", "self_authored_pages": list(direct.pages)} if direct else {}),
            **({"owner_id": task.owner_id, "task_id": task.task_id, "goal": task.goal,
                "pages": list(task.pages), "page_uids": list(task.page_uids),
                "base_deck_version": task.base_deck_version, "output_path": task.output_path,
                "handoff_from": task.handoff_from, "asset_ids": list(task.asset_ids)} if task is not None else {}),
            "current_instructions": {path: (root / path).read_text(encoding="utf-8")
                                     for path in instruction_paths if (root / path).is_file()},
            "artifact_hashes": hashes,
            "owner_requests": [row for row in requests if task is None or row["owner_id"] == task.owner_id],
            "issues": [{key: row[key] for key in ("issue_id", "pages", "status", "summary", "resolution")}
                       for row in issues],
        }

    @staticmethod
    def _finish_orchestrator_owner(services: "RuntimeServices", status: str) -> None:
        owner = services.owners.get_group("orchestrator")
        if owner is not None and owner.active:
            services.owners.transition(owner.owner_id, attempt=owner.attempt, status=status)

    def _orchestrator_context(self, services: "RuntimeServices") -> ToolContext:
        """授予 Orchestrator 与正式 Skill 一致的控制面和工作区范围。"""
        owner = services.owners.get_group("orchestrator")
        context = ToolContext(
            owner_id="orchestrator",
            role="orchestrator",
            read_scopes=read_scopes_for_role("orchestrator", skill_name=self.prepared.snapshot.name,
                                           attachment_paths=self._staged_attachment_paths()),
            write_scopes=write_scopes_for_role("orchestrator"),
            metadata={
                "attempt": owner.attempt if owner else 1,
                "pages": list(owner.pages) if owner else [],
                "workspace_root": str(services.workspace.paths.root),
                "provenance": {"skill_tree_sha256": self.prepared.snapshot.tree_sha256,
                               "release": self.prepared.snapshot.release, "deck_id": services.workspace.deck_id},
                "creation_config": services.workspace.creation_config(),
                **self._language_metadata(services),
            },
        )
        return replace(context, write_authorizer=lambda path: self._tool_services.may_write(context, path))

    def _owner_tool_context(
        self, services: "RuntimeServices", task: TaskRecord
    ) -> ToolContext:
        """依据真实 task ownership 生成角色最小工具范围。"""
        # ``attachment_paths`` is content-oriented; explicit style references live in
        # ``style_reference_paths`` and still need to be readable by delegated owners.
        attachment_paths = self._staged_attachment_paths()
        context = ToolContext(
            owner_id=task.owner_id,
            role=task.role,
            read_scopes=read_scopes_for_role(
                task.role,
                skill_name=self.prepared.snapshot.name,
                pages=task.pages,
                attachment_paths=attachment_paths,
                reference_pages=task.reference_pages,
            ),
            write_scopes=write_scopes_for_role(task.role, pages=task.pages, output_path=task.output_path),
            metadata={
                "workspace_root": str(services.workspace.paths.root),
                "provenance": {"skill_tree_sha256": self.prepared.snapshot.tree_sha256,
                               "release": self.prepared.snapshot.release, "deck_id": services.workspace.deck_id},
                "task_id": task.task_id,
                "attempt": task.attempt,
                "revision_id": task.revision_id,
                "base_deck_version": task.base_deck_version,
                "page_uids": list(task.page_uids),
                "pages": list(task.pages),
                "group_id": task.group_id,
                "creation_config": services.workspace.creation_config(),
                **self._language_metadata(services),
            },
        )
        return replace(context, write_authorizer=lambda path: self._tool_services.may_write(context, path))

    def _orchestrator_identity(self, services: "RuntimeServices") -> dict[str, object]:
        return {"deck_id": services.workspace.deck_id,
                "workspace": str(services.run_spec.output_workspace),
                "raw_user_query": services.run_spec.raw_user_query,
                "revision_request": (None if services.run_spec.revision_request is None
                                     else dict(services.run_spec.revision_request)),
                "attachments": self._checkpoint_attachments,
                "skill_tree_sha256": self.prepared.snapshot.tree_sha256}

    def _orchestrator_artifact_hashes(self, services: "RuntimeServices") -> dict[str, str]:
        root = services.run_spec.output_workspace
        paths = set()
        for scope in write_scopes_for_role("orchestrator"):
            if scope.path == "assets":
                continue
            path = root / scope.path
            paths.update(path.rglob("*") if scope.recursive else (path,))
        # scaffold/CSS 是主控工具的机械副产物，也必须与闭合上下文一致。
        paths.update((root / "slides").glob("*.html"))
        paths.update(root / owner.output_path for owner in services.owners.list_current()
                     if owner.role == "research" and owner.output_path)
        direct = services.owners.get_group("orchestrator")
        paths.update(root / f"renders/slide_{page:02d}.png" for page in (direct.pages if direct else ()))
        paths.update(root / row["path"] for row in services.artifacts.catalog()["images"]
                     if row["owner_id"] == "orchestrator")
        paths.update((root / name) for name in ("base.css", "_state/workspace.json"))
        return {path.relative_to(root).as_posix(): file_sha256(path) for path in sorted(paths) if path.is_file()}

    def _save_orchestrator_checkpoint(self, services: "RuntimeServices", checkpoint: SegmentCheckpoint) -> None:
        hashes = self._orchestrator_artifact_hashes(services)
        services.capture_training_segment("orchestrator", "orchestrator", checkpoint,
                                          artifact_paths=tuple(hashes))
        services.owner_contexts.save_orchestrator(
            checkpoint, identity=self._orchestrator_identity(services),
            artifact_hashes=hashes,
            live_cursor=services.live_trace.sequence)

    def _orchestrator_checkpoint(
        self, services: "RuntimeServices", current_input: Mapping[str, object],
    ) -> SegmentCheckpoint | None:
        if services.run_spec.run_mode.value == "synthesis":
            checkpoints = {item.checkpoint.segment_id: item.checkpoint
                           for item in services.training_snapshot()
                           if item.owner_id == "orchestrator"}
            if not checkpoints:
                return None
            # 恢复输入已重建；只关联最后实际运行且已闭合落盘的段，不回灌旧聊天。
            previous = None
            for event in iter_jsonl_events(services.workspace.paths.trace / "live-events.jsonl"):
                if (event.get("agent_id") == "orchestrator" and event.get("event_type") == "agent_context"
                        and event.get("segment_id") in checkpoints):
                    previous = checkpoints[event["segment_id"]]
            if previous is None:
                raise SkillRuntimeError("orchestrator_checkpoint_trace_missing: cannot identify the prior segment")
            return replace(SegmentCheckpoint.initial((current_input,)),
                           predecessor_segment_id=previous.segment_id, reason="workspace_resume",
                           excluded_channel_ids=previous.excluded_channel_ids,
                           invalid_signature_recoveries=previous.invalid_signature_recoveries)
        if services.orchestration.snapshot().get("tasks"):
            return None  # 已委派任务继续使用现有 wave/Owner 恢复协议。
        stored = services.owner_contexts.load_orchestrator()
        hashes = self._orchestrator_artifact_hashes(services)
        if stored is None:
            if set(hashes) - {"_state/workspace.json", "base.css", "config.json"} or any(
                event.get("event_type") in {"tool_use", "tool_result"}
                for event in iter_jsonl_events(services.workspace.paths.trace / "live-events.jsonl")
            ):
                raise SkillRuntimeError("orchestrator_checkpoint_missing: refusing to replay over existing artifacts")
            return None
        prior_identity = dict(stored.get("identity") or {})
        current_identity = self._orchestrator_identity(services)
        prior_revision = prior_identity.pop("revision_request", None)
        current_revision = current_identity.pop("revision_request", None)
        if (prior_identity != current_identity
            or stored.get("artifact_hashes") != hashes):
            raise SkillRuntimeError("orchestrator_checkpoint_stale: workspace identity or artifacts changed")
        cursor = int(stored["live_cursor"])
        if any(int(event.get("sequence") or 0) > cursor and event.get("event_type") in {"tool_use", "tool_result"}
               for event in iter_jsonl_events(services.workspace.paths.trace / "live-events.jsonl")):
            raise SkillRuntimeError("orchestrator_checkpoint_stale: newer tool transaction is not checkpointed")
        checkpoint = SegmentCheckpoint.from_dict(stored["segment"])
        if prior_revision != current_revision:
            # 新编辑是真实追加输入；同请求续跑仍复用闭合上下文，不重放制作或虚构工具回执。
            checkpoint = replace(checkpoint, messages=(*checkpoint.messages, dict(current_input)))
        return checkpoint

    def _orchestrator_input(
        self, services: "RuntimeServices"
    ) -> dict[str, object]:
        """只向主 Skill 注入入口事实，不复制创作或编排规则。"""
        content: dict[str, object] = {
            "schema": "mural.orchestrator-input.v1",
            "raw_user_query": self.prepared.run_spec.raw_user_query,
            "creation_config": services.workspace.creation_config(),
            "source_policy": source_policy_text(self.prepared.run_spec.authoring_profile),
            "revision_request": (
                None
                if self.prepared.run_spec.revision_request is None
                else dict(self.prepared.run_spec.revision_request)
            ),
            "attachment_manifest": _attachment_manifest_prompt_view(
                self._attachment_manifest()
            ),
            "workspace": ".",
        }
        resume_state = self._resume_state(services)
        if resume_state is not None:
            content["resume_state"] = resume_state
        return {
            "role": "user",
            "content": _json_text(content),
        }

    def _resume_state(self, services: "RuntimeServices") -> Mapping[str, object] | None:
        """已有任务与结果就是恢复真相，不引入收集阶段。"""
        state = services.orchestration.snapshot()
        tasks = state.get("tasks") or {}
        direct = services.owners.get_group("orchestrator")
        if not tasks and direct is None:
            return None
        result = {
            "schema": "mural.same-workspace-resume.v2",
            "recovered_provider_attempt_count": len(self._recovered_provider_attempt_ids),
            "tasks": [
                _task_prompt_view(task, include_goal=True)
                for task in tasks.values()
                if isinstance(task, Mapping)
            ],
            "self_authored_pages": list(direct.pages) if direct else [],
            "pages": [
                {
                    "page": page.page,
                    "lifecycle": page.lifecycle.value,
                    "html_path": page.html_path,
                    "png_path": page.png_path,
                    "stale_png": page.stale_png,
                }
                for page in services.workspace.pages()
            ],
            "page_coverage": services.orchestration.page_coverage(),
            "pending_requests": [
                _owner_prompt_value(row, text_limit=1200, list_limit=12)
                for row in (state.get("requests") or {}).values()
                if row.get("status") == "pending"
            ],
            "cursor": (state.get("cursors") or {}).get("orchestrator", 0),
        }
        previous_failure = getattr(self, "_previous_tool_failure", None)
        if previous_failure:
            result["previous_tool_failure"] = {**previous_failure, "instruction": (
                "Correct this tool contract before continuing from the actual saved artifacts. "
                "Do not resubmit succeeded tasks. Respect the current page/asset Owner and authorized paths; "
                "wait or answer requests while its task runs, then repair through the original group_id. "
                "Use one complete script command per terminal call, with all required arguments repeated. "
                "Do not retry the denied action or change groups to bypass ownership."
            )}
        return result

    def _hydrate_completed_owner_results(
        self, services: "RuntimeServices"
    ) -> None:
        """让恢复后的最终 outcome 仍包含先前进程已完成的 Owner。"""
        raw_tasks = services.orchestration.snapshot().get("tasks") or {}
        if not isinstance(raw_tasks, Mapping):
            return
        for raw in raw_tasks.values():
            if not isinstance(raw, Mapping) or raw.get("status") != "succeeded":
                continue
            result = raw.get("result")
            if not isinstance(result, Mapping):
                continue
            agent_id = str(result.get("agent_id") or raw.get("owner_id") or "")
            if agent_id:
                with self._trace_lock:
                    self._agent_results[agent_id] = copy.deepcopy(dict(result))

    def _owner_handoffs(
        self,
        services: "RuntimeServices",
        task: TaskRecord,
        *,
        metadata_only: bool = False,
    ) -> dict[str, object]:
        """提供依赖、附件与可读路径；持久状态和解析回执不进入模型。"""
        dependencies = [
            _task_prompt_view(services.orchestration.task(task_id))
            for task_id in task.dependencies
        ]
        handoffs = {"dependency_results": dependencies}
        if task.output_path:
            handoffs["research_output_path"] = task.output_path
        if task.handoff_from:
            handoffs["handoff_from"] = task.handoff_from
            handoffs["asset_ids"] = list(task.asset_ids)
            previous = None if metadata_only else services.owner_contexts.load(task.handoff_from)
            if metadata_only:
                handoffs["metadata_only"] = True
                handoffs["metadata_only_notice"] = (
                    "仅补充或修正 assets/catalog.md 中既有素材的用途、来源、许可、署名、notes 等字段；"
                    "不要搜索、下载、生成、裁切、替换图片，也不要重新做像素检查。"
                )
            elif previous is not None:
                handoffs["previous_goal"] = _owner_prompt_value(
                    previous.packet.goal, text_limit=2000
                )
                handoffs["previous_summary"] = _owner_prompt_value(
                    previous.capsule, text_limit=2400
                )
                handoffs["prior_visual_observations"] = _prior_visual_observations(previous.segment.messages, task.pages)
        if task.reference_pages:
            handoffs["reference_html_paths"] = [f"slides/slide_{page:02d}.html" for page in task.reference_pages]
        if task.role in {"research", "image"} and not metadata_only:
            handoffs["attachment_manifest"] = _attachment_manifest_prompt_view(
                self._attachment_manifest()
            )
        if services.workspace.paths.knowledge.is_file():
            handoffs["knowledge_path"] = "knowledge.md"
        if task.role in {"image", "slide"}:
            if task.role == "image" and not metadata_only:
                handoffs["existing_image_files"] = [
                    {"path": path.relative_to(services.workspace.paths.root).as_posix(),
                     "bytes": path.stat().st_size}
                    for path in sorted((services.workspace.paths.root / "assets").glob("*"))
                    if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}
                ]
            if (services.workspace.paths.root / "assets/catalog.md").is_file():
                handoffs["image_catalog_path"] = "assets/catalog.md"
            if not metadata_only and (services.workspace.paths.root / "deck.md").is_file():
                snapshot = self.prepared.snapshot
                plan = load_skill_module(snapshot.target, snapshot.tree_sha256, "deck_plan")
                handoffs["plan"] = plan.owner_context(services.workspace.paths.root, task.pages)
        return handoffs

    def _artifact_refs(
        self, services: "RuntimeServices", task: TaskRecord,
    ) -> tuple[Mapping[str, object], ...]:
        """按消费者投影可用语义；完整 catalog 仍由 assets/catalog.md 提供。"""
        images = services.artifacts.catalog().get("images") or []
        if not isinstance(images, list):
            raise SkillRuntimeError("Artifact image catalog must be an array")
        pages = set(task.pages)
        explicit = set(getattr(task, "asset_ids", ()) or ())
        candidates: list[tuple[int, int, Mapping[str, object]]] = []
        for index, item in enumerate(images):
            if not isinstance(item, Mapping):
                continue
            consumers = (item.get("image_metadata") or {}).get("slides")
            if pages and isinstance(consumers, list) and consumers and not pages.intersection(consumers):
                continue
            artifact_id = str(item.get("artifact_id") or "")
            assigned = bool(pages and isinstance(consumers, list) and pages.intersection(consumers))
            if task.role == "slide" and assigned and artifact_id not in explicit and _image_usage_role(item) == "reference_only":
                continue
            priority = 0 if artifact_id in explicit else 1 if assigned else 2
            candidates.append((priority, index, item))
        candidates.sort(key=lambda row: (row[0], row[1]))
        limit = max(OWNER_ARTIFACT_REF_LIMIT, len(explicit))
        return tuple(
            _artifact_prompt_view(item)
            for _priority, _index, item in candidates[:limit]
        )

    def _staged_attachment_paths(self) -> tuple[str, ...]:
        """返回工作区内稳定附件路径，不把入口绝对路径传给 Owner。"""
        records = self._attachment_manifest().get("records") or []
        if not isinstance(records, list):
            raise SkillRuntimeError("Attachment manifest records must be an array")
        return tuple(
            str(item.get("path") or "")
            for item in records
            if isinstance(item, Mapping) and str(item.get("path") or "")
        )

    @staticmethod
    def _owner_artifact_paths(task: TaskRecord) -> tuple[str, ...]:
        candidates = {
            "config.json",
            "deck.md",
            "knowledge.md",
            "assets/catalog.md",
            "base.css",
        }
        if task.output_path:
            candidates.add(task.output_path)
        for page in task.pages:
            candidates.update(
                {
                    f"slides/slide_{int(page):02d}.html",
                    f"renders/slide_{int(page):02d}.png",
                    f"briefs/slide_{int(page):02d}.md",
                }
            )
        candidates.update(f"slides/slide_{page:02d}.html" for page in task.reference_pages)
        return tuple(sorted(candidates))

    def _owner_artifact_hashes(
        self, services: "RuntimeServices", task: TaskRecord
    ) -> dict[str, str]:
        """恢复指纹仍对照当前文件，与训练段的不可变版本引用分开。"""
        return {relative: digest for relative in self._owner_artifact_paths(task)
                if (digest := file_sha256(services.workspace.paths.resolve(relative)))}

    def _record_runtime_event(
        self,
        services: "RuntimeServices",
        event: Mapping[str, object],
    ) -> None:
        """运行态只写 live journal；离线格式由 Kernel 收尾时派生。"""
        services.live_trace.record_runtime_event(event)

    def _publish_image_handoff(self, services: "RuntimeServices", row: Mapping[str, object]) -> None:
        metadata = row.get("image_metadata") or {}
        digest = row.get("sha256")
        if (row.get("status") != "committed" or not digest or not metadata.get("slides")
                or metadata.get("reviewed_sha256") != digest
                or metadata.get("reviewed_by") != row.get("owner_id")
                or file_sha256(services.workspace.paths.resolve(str(row["path"]))) != digest):
            return
        image = {key: row[key] for key in ("artifact_id", "path", "owner_id", "sha256", "image_metadata")}
        key = hashlib.sha256(json.dumps(image, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        payload = {
            "image": image, "consumer_pages": list(metadata["slides"]),
            "note": "Image 已看过当前 hash 并确认该素材可用；不代表本页所有材料或整笔 Image 任务已完成。按明确需求先委派已就绪页面，未声明的依赖不由程序猜测。",
        }
        services.orchestration.publish_background_event("image_handoff_ready", payload,
            dedup_key=key, on_published=lambda: services.live_trace.append(
                event_type="image_handoff_ready", agent_id=str(row["owner_id"]), role="harness",
                event_id="image-handoff:" + key, payload=payload))

    def _record_result(self, result: AgentRunResult) -> None:
        """保存各 Agent 的无图片字节摘要，供最终 host manifest 引用。"""
        with self._trace_lock:
            self._agent_results[result.agent_id] = result.to_dict()

    def _write_host_state(
        self,
        *,
        status: str,
        outcome: Mapping[str, object] | None = None,
        error: str = "",
        agent_failed: Mapping[str, object] | None = None,
        elapsed_s: float | None = None,
    ) -> None:
        """原子记录生产装配状态与非敏感运行摘要。"""
        value: dict[str, object] = {
            "schema": "mural.skill-runtime.v1",
            "adapter": "skill_runtime",
            "status": str(status),
            "creation_config_sha256": file_sha256(self.prepared.run_spec.output_workspace / "config.json"),
            "checkpoint_attachments": copy.deepcopy(self._checkpoint_attachments),
            "preparation_timings_ms": dict(self._preparation_timings_ms),
            "run_mode": self.prepared.run_spec.run_mode.value,
            "training_sample_max_tokens": (self.prepared.settings.training_sample_max_tokens
                if self.prepared.run_spec.run_mode.value == "synthesis" else None),
            "compaction_policy": {key: getattr(self.prepared.settings, key) for key in (
                "compact_trigger_ratio", "compact_target_tokens", "compact_summary_target_tokens")},
            "skill_tree_sha256": self.prepared.snapshot.tree_sha256,
            "skill_main_sha256": self.prepared.snapshot.main_sha256,
            "role_sha256": dict(self.prepared.snapshot.role_sha256),
            "attachment_manifest": self.prepared.attachments.path.resolve().relative_to(
                self.prepared.run_spec.output_workspace
            ).as_posix(),
            "provider": self.prepared.run_spec.provider.to_manifest(),
            "recovered_interrupted_provider_attempts": len(
                self._recovered_provider_attempt_ids
            ),
        }
        if outcome is not None:
            value["outcome"] = copy.deepcopy(dict(outcome))
        if error:
            value["error"] = str(error)
        if agent_failed is not None:
            value["agent_failed"] = copy.deepcopy(dict(agent_failed))
        if elapsed_s is not None:
            value["elapsed_s"] = float(elapsed_s)
        _atomic_json_write(self._host_state, value)


def current_critic_state(records, pages, *, critic_model="", rubric_version="",
                         critic_prompt_version="") -> dict[str, object]:
    """只结算当前 page UID + inspection + HTML/PNG；历史记录完整保留。"""
    current = []
    for page in pages:
        record = next((record for record in reversed(records)
            if record.get("page_uid") == page.page_uid
            and (not critic_model or record.get("critic_model") == critic_model)
            and (not rubric_version or record.get("rubric_version") == rubric_version)
            and (not critic_prompt_version or record.get("critic_prompt_version") == critic_prompt_version)
            and record.get("inspection_id") == page.inspection_id
            and record.get("html_sha256") == page.html_sha256
            and record.get("png_sha256") == page.png_sha256), None)
        current.append({"page_uid": page.page_uid, "page": page.page,
            "inspection_id": page.inspection_id,
            "html_sha256": page.html_sha256, "png_sha256": page.png_sha256,
            "critic_model": str((record or {}).get("critic_model") or ""),
            "rubric_version": str((record or {}).get("rubric_version") or ""),
            "critic_prompt_version": str((record or {}).get("critic_prompt_version") or ""),
            "attempted": record is not None,
            "completed": bool(record and record.get("completed")),
            "incomplete_reason": str((record or {}).get("incomplete_reason") or
                ("" if record and record.get("completed") else "missing_current_critic")),
            "feedback": str((record or {}).get("body") or ""),
            "source_refs": dict((record or {}).get("source_refs") or {}),
            "tool_use_id": str((record or {}).get("tool_use_id") or "")})
    return {"current_inspections": current,
            "critic_incomplete": not current or any(not row["completed"] for row in current)}


def _prior_visual_observations(messages, pages) -> list[dict]:
    """重建时保留每个责任页最近的原始视觉反馈；版本引用不是当前验收。"""
    latest = {}
    for message in messages:
        if message.get("role") != "user" or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if block.get("type") != "tool_result" or block.get("is_error"):
                continue
            content = block.get("content")
            parts = content if isinstance(content, list) else [{"type": "text", "text": content}]
            for part in parts:
                if part.get("type") != "text" or not isinstance(part.get("text"), str):
                    continue
                try:
                    payload = json.loads(part["text"])
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict) or not payload.get("critic_feedback"):
                    continue
                descriptor = payload.get("pending_inspection") or payload.get("current_inspection") or {}
                for member in descriptor.get("members", []):
                    if member.get("page") in pages:
                        latest[member["page"]] = {"version": copy.deepcopy(member),
                            "path": descriptor.get("path"), "feedback": copy.deepcopy(payload["critic_feedback"]),
                            "note": "历史版本的观察，未证明当前页面已解决或仍存在这些问题；对照当前像素复核。"}
    return list(latest.values())


def _json_text(value: Mapping[str, object]) -> str:
    """把注入事实编码为稳定 JSON 文本，避免解析自由格式模板。"""
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, indent=2)


def _atomic_json_write(path: Path, value: Mapping[str, object]) -> None:
    """在同目录原子写入共享可读 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    pending = path.with_name(f".{path.name}.{uuid.uuid4().hex}.pending")
    try:
        descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


__all__ = ["PreparedSkillRuntime", "SkillRuntime", "SkillRuntimeError"]
