#!/usr/bin/env python3
"""Inference 入口与 inference/synthesis 共用的生成内核。

CLI 默认装配正式 Skill Runtime；测试仍可注入 backend、Renderer 与 Provider 替身。两种运行
模式执行完全相同的生成路径；synthesis 保留 raw 分段证据，训练导出另行显式执行。
"""
from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import threading
from typing import Any, Callable, Mapping, Sequence
import uuid

from export.delivery import DeliveryExporter, DeliveryManifest
from export.readable import export_readable_by_role
from export.training import TrainingExporter, TrainingSegmentInput
from orchestration.manager import (
    assert_tasks_terminal,
    OrchestrationError,
    OrchestrationManager,
    OwnerRequestInterrupted,
    TaskRunner,
    TaskStatus,
)
from owner.context import OwnerContextStore
from owner.registry import OwnerRegistry
from qc.routing import IssueLedger, IssueLedgerError, RevisionRouter
from qc.visual import VisionAnalyzer
from qc.review_hook import InspectionClosed, inspection_question, inspection_with_critic_question
from runtime.attachments import (
    AttachmentError,
    materialize_page_image,
    resolve_attachment_records,
    resolve_page_assets,
)
from runtime.config import (
    RunMode,
    RunSpec,
    load_runtime_config,
    merge_environment,
    read_environment_file,
)
from runtime.live_trace import (
    LiveEventJournal,
    iter_jsonl_events,
    iter_replay_events,
)
from runtime.recovery import ResourceSlotTimeout, SegmentCheckpoint
from runtime.vision import MAX_CONTEXT_IMAGES
from runtime.workspace_paths import WorkspacePathError
from runtime.shared_objects import SharedSkillStore, load_skill_module
from runtime.tools import (
    ToolContext,
    normalize_workspace_path,
    ToolExecutionError,
    ToolHandler,
    ToolRuntime,
    register_tools,
)
from state.artifacts import ArtifactStateError, ArtifactStore, RenderBackend, UncommittedPageSourceError
from state.workspace import PageLifecycle, WorkspaceState, WorkspaceStateError, file_sha256


MAX_PAGE_INSPECTIONS_PER_PIXEL = 1
PAGE_INSPECTION_REVIEW_TARGET = 1
GenerationBackend = Callable[["RuntimeServices"], Mapping[str, object] | None]

DELEGATE_WAIT_TEXT_LIMIT = 4000
DELEGATE_WAIT_RESULT_TEXT_LIMIT = 2000
DELEGATE_WAIT_SUMMARY_TEXT_LIMIT = 600
DELEGATE_WAIT_PROMPT_CHAR_LIMIT = 16 * 1024


def _prompt_excerpt(value: object, limit: int) -> object:
    """Keep orchestration state complete on disk while bounding prompt history."""
    if not isinstance(value, str) or len(value) <= limit:
        return copy.deepcopy(value)
    head = max(1, limit // 2)
    tail = max(1, limit - head)
    omitted = len(value) - head - tail
    return value[:head] + f"\n...[omitted {omitted} chars from model prompt; full wait state is in trace]...\n" + value[-tail:]


def _compact_prompt_value(value: object, *, text_limit: int = DELEGATE_WAIT_TEXT_LIMIT,
                          depth: int = 0) -> object:
    """Bound nested wait payloads without changing their top-level contract."""
    if isinstance(value, str):
        return _prompt_excerpt(value, text_limit)
    if isinstance(value, list):
        limit = 12 if depth > 0 else 48
        items = [_compact_prompt_value(item, text_limit=text_limit, depth=depth + 1)
                 for item in value[:limit]]
        if len(value) > limit:
            items.append({"omitted_items": len(value) - limit,
                          "omitted_notice": "additional wait items omitted from model prompt"})
        return items
    if isinstance(value, Mapping):
        return {str(key): copy.deepcopy(item) if key in {
                    "continuation_params", "handoff_params", "research_task_link_params"
                } and isinstance(item, Mapping) else
                _compact_prompt_value(item, text_limit=text_limit, depth=depth + 1)
                for key, item in value.items()}
    return copy.deepcopy(value)


def _compact_delegate_task(task: Mapping[str, object]) -> dict[str, object]:
    keep = (
        "task_id", "owner_id", "attempt", "role", "pages", "group_id", "dependencies",
        "status", "repair", "error", "output_path", "handoff_from", "asset_ids",
        "reference_pages", "base_deck_version", "page_uids", "recovery",
    )
    compact = {key: _compact_prompt_value(task[key]) for key in keep if key in task}
    status = str(task.get("status") or "")
    if "goal" in task and status not in {"succeeded", "completed", "skipped"}:
        compact["goal"] = _prompt_excerpt(str(task.get("goal") or ""), 800)
    raw_result = task.get("result")
    if isinstance(raw_result, Mapping):
        result_keep = (
            "status", "role", "stop_reason", "output_path", "output_recorded",
            "recoverable", "group_review_gap", "repair_handoff", "review_handoff",
            "blocking_issues", "recovery",
        )
        result = {
            key: _compact_prompt_value(raw_result[key], text_limit=800)
            for key in result_keep if key in raw_result
        }
        summary = raw_result.get("final_text") or raw_result.get("summary")
        if summary:
            result["summary"] = _prompt_excerpt(
                str(summary), DELEGATE_WAIT_SUMMARY_TEXT_LIMIT
            )
        if result:
            compact["result"] = result
    elif raw_result not in (None, ""):
        compact["result"] = {
            "summary": _prompt_excerpt(
                str(raw_result), DELEGATE_WAIT_SUMMARY_TEXT_LIMIT
            )
        }
    return compact


def _compact_delegate_event(event: Mapping[str, object]) -> dict[str, object]:
    """Project persisted orchestration events into the small control-plane view."""
    internal_keys = {
        "dedup_key", "diagnostic_ref", "full_feedback_ref", "full_output_ref",
        "full_state_ref", "manifest_path", "state_path", "trace_path",
    }
    keep = ("sequence", "type", "task_id", "owner_id", "actionable")
    compact = {key: copy.deepcopy(event[key]) for key in keep if key in event}
    if event.get("type") == "image_handoff_ready":
        image = event.get("image")
        if isinstance(image, Mapping):
            compact["image"] = {
                key: copy.deepcopy(image[key])
                for key in ("artifact_id", "path", "sha256") if key in image
            }
        compact["consumer_pages"] = _compact_prompt_value(
            event.get("consumer_pages") or []
        )
        compact["catalog_ref"] = "assets/catalog.md"
        return compact
    if event.get("type") == "task_completed":
        return compact
    for key, value in event.items():
        if key not in compact and key not in internal_keys:
            compact[str(key)] = _compact_prompt_value(value, text_limit=800, depth=1)
    return compact


def _coalesce_delegate_events(events: Sequence[object]) -> list[object]:
    """Keep only the latest notification for the same image artifact."""
    latest_image: dict[str, int] = {}
    for index, event in enumerate(events):
        if not isinstance(event, Mapping) or event.get("type") != "image_handoff_ready":
            continue
        image = event.get("image")
        artifact_id = str(image.get("artifact_id") or "") if isinstance(image, Mapping) else ""
        if artifact_id:
            latest_image[artifact_id] = index
    return [
        event for index, event in enumerate(events)
        if not (
            isinstance(event, Mapping)
            and event.get("type") == "image_handoff_ready"
            and isinstance(event.get("image"), Mapping)
            and (artifact_id := str(event["image"].get("artifact_id") or ""))
            and latest_image.get(artifact_id) != index
        )
    ]


def _delegate_wait_payload_chars(payload: Mapping[str, object]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _fit_delegate_wait_payload(result: dict[str, object]) -> dict[str, object]:
    """Apply an aggregate prompt budget after per-record projection."""
    if _delegate_wait_payload_chars(result) <= DELEGATE_WAIT_PROMPT_CHAR_LIMIT:
        return result

    omissions: dict[str, int | str] = {}
    tasks = result.get("tasks")
    if isinstance(tasks, list):
        removed = 0
        for task in tasks:
            if (isinstance(task, dict) and task.get("status") in {"succeeded", "completed", "skipped"}
                    and task.pop("result", None) is not None):
                removed += 1
        if removed:
            omissions["completed_task_summaries"] = removed

    events = result.get("events")
    if isinstance(events, list):
        completed = [event for event in events if isinstance(event, Mapping)
                     and event.get("type") == "task_completed"]
        if completed:
            result["events"] = [event for event in events if event not in completed]
            omissions["redundant_task_completed_events"] = len(completed)

    result["prompt_omissions"] = omissions
    if _delegate_wait_payload_chars(result) <= DELEGATE_WAIT_PROMPT_CHAR_LIMIT:
        return result

    events = result.get("events")
    if isinstance(events, list):
        image_events = [event for event in events if isinstance(event, Mapping)
                        and event.get("type") == "image_handoff_ready"]
        while image_events and _delegate_wait_payload_chars(result) > DELEGATE_WAIT_PROMPT_CHAR_LIMIT:
            removed = image_events.pop(0)
            events.remove(removed)
            omissions["image_handoffs"] = int(omissions.get("image_handoffs") or 0) + 1
        if omissions.get("image_handoffs"):
            omissions["image_handoff_source"] = "assets/catalog.md"

    if _delegate_wait_payload_chars(result) <= DELEGATE_WAIT_PROMPT_CHAR_LIMIT:
        return result

    tasks = result.get("tasks")
    if isinstance(tasks, list):
        removable = [task for task in tasks if isinstance(task, Mapping)
                     and task.get("status") in {"succeeded", "completed", "skipped"}]
        while removable and _delegate_wait_payload_chars(result) > DELEGATE_WAIT_PROMPT_CHAR_LIMIT:
            removed = removable.pop(0)
            tasks.remove(removed)
            omissions["completed_tasks"] = int(omissions.get("completed_tasks") or 0) + 1

    if _delegate_wait_payload_chars(result) > DELEGATE_WAIT_PROMPT_CHAR_LIMIT:
        # Critical requests and failures remain visible, but their prose is bounded harder.
        for key in ("events", "pending_requests", "unresolved_failures"):
            if isinstance(result.get(key), list):
                original_count = len(result[key])
                result[key] = [
                    _compact_prompt_value(item, text_limit=320)
                    for item in result[key][:24]
                ]
                if original_count > 24:
                    omissions[key] = int(omissions.get(key) or 0) + original_count - 24
        result["prompt_omissions"] = omissions
    if _delegate_wait_payload_chars(result) > DELEGATE_WAIT_PROMPT_CHAR_LIMIT:
        # A pathological backlog must still not become a giant model message. Pending
        # requests and unresolved failures remain durable and will be returned again.
        remaining_events = result.get("events") if isinstance(result.get("events"), list) else []
        remaining_tasks = result.get("tasks") if isinstance(result.get("tasks"), list) else []
        event_type_counts: dict[str, int] = {}
        task_status_counts: dict[str, int] = {}
        for event in remaining_events:
            if isinstance(event, Mapping):
                event_type = str(event.get("type") or "unknown")
                event_type_counts[event_type] = event_type_counts.get(event_type, 0) + 1
        for task in remaining_tasks:
            if isinstance(task, Mapping):
                task_status = str(task.get("status") or "unknown")
                task_status_counts[task_status] = task_status_counts.get(task_status, 0) + 1
        if remaining_events:
            omissions["events"] = int(omissions.get("events") or 0) + len(remaining_events)
        if remaining_tasks:
            omissions["tasks"] = int(omissions.get("tasks") or 0) + len(remaining_tasks)
        preserved_keys = (
            "harness_event", "status", "all_terminal", "cursor", "error_code", "detail",
            "notice", "blocking_issues", "delegated_pages", "unassigned_pages",
            "undelegated_pages", "self_authored_pages", "planned_page_count",
            "target_page_count", "page_coverage_source", "recovery",
        )
        fallback = {
            key: _compact_prompt_value(result[key], text_limit=160)
            for key in preserved_keys if key in result
        }
        for key in ("pending_requests", "unresolved_failures"):
            values = result.get(key)
            if isinstance(values, list):
                fallback[key] = [
                    _compact_prompt_value(item, text_limit=160)
                    for item in values[:8]
                ]
                if len(values) > 8:
                    omissions[key] = int(omissions.get(key) or 0) + len(values) - 8
        fallback.update({
            "events": [],
            "tasks": [],
            "history_compacted": True,
            "event_type_counts": event_type_counts,
            "task_status_counts": task_status_counts,
            "prompt_omissions": {
                **omissions,
                "payload_budget_fallback": True,
                "image_handoff_source": "assets/catalog.md",
                "pending_state": "durable",
            },
        })
        return fallback
    return result


def _compact_delegate_wait_payload(payload: object) -> object:
    """Return a prompt-safe delegate_wait result; the full state remains persisted."""
    if not isinstance(payload, Mapping):
        return payload
    original = copy.deepcopy(dict(payload))
    result = copy.deepcopy(original)
    if isinstance(result.get("tasks"), list):
        result["tasks"] = [_compact_delegate_task(task) if isinstance(task, Mapping) else task
                           for task in result["tasks"]]
    if isinstance(result.get("events"), list):
        result["events"] = [
            _compact_delegate_event(event) if isinstance(event, Mapping) else event
            for event in _coalesce_delegate_events(result["events"])
        ]
    if isinstance(result.get("pending_requests"), list):
        result["pending_requests"] = [
            _compact_prompt_value(request, text_limit=DELEGATE_WAIT_RESULT_TEXT_LIMIT)
            for request in result["pending_requests"]
        ]
    if isinstance(result.get("unresolved_failures"), list):
        result["unresolved_failures"] = [
            _compact_prompt_value(failure, text_limit=DELEGATE_WAIT_RESULT_TEXT_LIMIT)
            for failure in result["unresolved_failures"]
        ]
    handled = {"tasks", "events", "pending_requests", "unresolved_failures"}
    for key, value in tuple(result.items()):
        if key not in handled:
            result[key] = _compact_prompt_value(value, text_limit=DELEGATE_WAIT_RESULT_TEXT_LIMIT)
    if result != original:
        result["history_compacted"] = True
    return _fit_delegate_wait_payload(result)


def _goal_page_mention_count(goal: object, planned_page_count: object) -> int:
    text = str(goal or "").lower()
    try:
        planned = int(planned_page_count or 0)
    except (TypeError, ValueError):
        planned = 0
    if planned <= 0:
        planned = 12
    hits = set()
    for page in range(1, planned + 1):
        markers = (f"p{page}", f"p{page:02d}", f"第{page}页", f"页{page}", f"page {page}", f"page-{page}")
        if any(marker in text for marker in markers):
            hits.add(page)
    return len(hits)


def _parallelization_advisories(
    payload: Mapping[str, object],
    *,
    submitted_tasks: Sequence[Mapping[str, object]] = (),
    snapshot_tasks: Mapping[str, object] | Sequence[Mapping[str, object]] = (),
) -> list[dict[str, object]]:
    """Small runtime nudges that keep Orchestrator from serializing independent work."""
    try:
        planned = int(payload.get("planned_page_count") or payload.get("target_page_count") or 0)
    except (TypeError, ValueError):
        planned = 0
    unassigned_raw = payload.get("unassigned_pages") or payload.get("undelegated_pages") or []
    unassigned = [int(page) for page in unassigned_raw if type(page) is int]
    if isinstance(snapshot_tasks, Mapping):
        task_rows = [row for row in snapshot_tasks.values() if isinstance(row, Mapping)]
    else:
        task_rows = [row for row in snapshot_tasks if isinstance(row, Mapping)]
    submitted_rows = [row for row in submitted_tasks if isinstance(row, Mapping)]
    all_rows = [*task_rows, *submitted_rows, *(payload.get("tasks") or [])]
    slide_known = any(row.get("role") == "slide" for row in all_rows if isinstance(row, Mapping))
    image_active = any(row.get("role") == "image" and row.get("status") not in {
        "completed", "succeeded", "failed", "canceled", "interrupted"
    } for row in all_rows if isinstance(row, Mapping))
    advisories: list[dict[str, object]] = []
    if unassigned and not slide_known:
        advisories.append({
            "code": "slide_parallelism_available",
            "unassigned_pages": unassigned,
            "message": (
                "仍有未分配页面。只要 config.json、deck.md、base.css 和对应 "
                "briefs/slide_NN.md 已就绪，就先批量提交独立 Slide Owner；缺最终素材的页也可以先做结构、"
                "文字和占位，待单项 image_handoff_ready 后续做，不靠页数硬阈值，也不必等待整批 Image 任务结束。"
            ),
        })
    image_rows = [row for row in submitted_rows if row.get("role") == "image"]
    if len(image_rows) == 1 and planned >= 3 and _goal_page_mention_count(image_rows[0].get("goal", ""), planned) >= 2:
        advisories.append({
            "code": "image_task_split_preferred",
            "task_id": image_rows[0].get("task_id", ""),
            "message": (
                "这个 Image 任务覆盖多页素材需求，容易把搜索、裁切、inspect 和登记串行化。后续优先按页面、"
                "主视觉/机制图/生态图等独立素材包拆成多个 Image 任务；每个任务可单项 finalize/register，"
                "让 Slide Owner 先消费已完成素材。"
            ),
        })
    if image_active and unassigned and not slide_known:
        advisories.append({
            "code": "do_not_wait_for_full_image_batch_before_slides",
            "message": (
                "当前仍有 Image 工作在跑，但页面制作不需要等整批素材全部结束。已确定文本、机制结构或占位方案的页面"
                "应先委派 Slide Owner；只有真正依赖某张图片身份/裁切的局部再等对应 handoff。"
            ),
        })
    return advisories


def _knowledge_integration_advisories(payload: Mapping[str, object]) -> list[dict[str, object]]:
    """Nudge Orchestrator to adopt completed Research into the shared deck facts."""
    outputs: list[dict[str, object]] = []
    for row in payload.get("tasks") or []:
        if not isinstance(row, Mapping):
            continue
        if row.get("role") != "research" or row.get("status") not in {
            "succeeded", "completed",
        }:
            continue
        output_path = str(row.get("output_path") or "").strip()
        item: dict[str, object] = {"task_id": str(row.get("task_id") or "")}
        if output_path:
            item["output_path"] = output_path
        outputs.append(item)
    if not outputs:
        return []
    return [{
        "code": "research_ready_for_knowledge_adoption",
        "research_outputs": outputs,
        "message": (
            "Research 已完成。Orchestrator 应先读取对应 research/*.md，判断哪些结论被本册采用，"
            "把采用后的解释、依据、冲突处理和边界写入 knowledge.md；再把相关结论同步到 "
            "briefs/slide_NN.md 后委派或续做 Slide。Slide 不应直接把研究任务稿当作当前统一口径。"
        ),
    }]


def _with_parallelization_advisories(
    payload: object,
    *,
    submitted_tasks: Sequence[Mapping[str, object]] = (),
    snapshot_tasks: Mapping[str, object] | Sequence[Mapping[str, object]] = (),
) -> object:
    if not isinstance(payload, Mapping):
        return payload
    result = copy.deepcopy(dict(payload))
    if "unassigned_pages" in result and "undelegated_pages" not in result:
        result["undelegated_pages"] = copy.deepcopy(result["unassigned_pages"])
    advisories = _parallelization_advisories(result,
        submitted_tasks=submitted_tasks, snapshot_tasks=snapshot_tasks)
    if advisories:
        existing = result.get("parallelization_advisories")
        result["parallelization_advisories"] = [
            *(existing if isinstance(existing, list) else []),
            *advisories,
        ]
    knowledge_advisories = _knowledge_integration_advisories(result)
    if knowledge_advisories:
        existing = result.get("knowledge_integration_advisories")
        result["knowledge_integration_advisories"] = [
            *(existing if isinstance(existing, list) else []),
            *knowledge_advisories,
        ]
    return result


@dataclass
class RuntimeServices:
    """集中暴露一次共享生成运行所需的状态与工具。"""

    run_spec: RunSpec
    workspace: WorkspaceState
    owners: OwnerRegistry
    owner_contexts: OwnerContextStore
    orchestration: OrchestrationManager
    artifacts: ArtifactStore
    issues: IssueLedger
    router: RevisionRouter
    tools: ToolRuntime
    live_trace: LiveEventJournal
    visual_review: Callable[[], Mapping[str, object]] | None = None
    finalize_delivery: Callable[[], object] | None = None
    training_segments: list[TrainingSegmentInput] = field(default_factory=list)
    _training_lock: threading.RLock = field(
        default_factory=threading.RLock, repr=False
    )

    def orchestrator_tool_context(self) -> ToolContext:
        """创建只能以 Orchestrator 身份调用控制工具的上下文。"""
        return ToolContext(owner_id="orchestrator", role="orchestrator")

    def _page_completion_error(
        self, code: str, detail: str, page_issues: Sequence[Mapping[str, object]],
    ) -> ToolExecutionError:
        """从唯一归属表投影续做参数；只给建议，不创建任务或改变责任组。"""
        owners = {page: owner for owner in self.owners.list_current()
                  if owner.role in {"slide", "orchestrator"} for page in owner.pages}
        grouped = {}
        for issue in page_issues:
            owner = owners.get(issue["page"])
            grouped.setdefault(owner.owner_id if owner else "", []).append(issue)
        recovery = []
        for rows in grouped.values():
            owner = owners.get(rows[0]["page"])
            affected = [row["page"] for row in rows]
            goal = "\n".join(
                f"P{row['page']} ({row['html_path']}): " + "; ".join(reason["detail"] for reason in row["reasons"])
                for row in rows)
            goal = (f"仅处理以下当前完成缺口：\n{goal}\n"
                "沿用当前 config.json 与分页计划；如需改动，只修正导致回执失效的内容。"
                "原作者使用 inspect(page=受影响页码, review_stage=\"final\") 生成并实际查看当前 PNG。"
                "正常修稿后的必要复核不受页面累计检查次数阻断；不要只看旧 PNG 或联系表代替单页验收。"
                "保留组内其他页面及有效成果；不要因本次续做重做全组。语义问题仍须明确复核，不因渲染成功自动关闭。")
            item = {"action": "author_or_delegate", "pages": affected, "affected_pages": affected, "goal": goal}
            if owner is not None:
                item.update(owner_id=owner.owner_id, group_id=owner.group_id, pages=list(owner.pages),
                    action="continue_self" if owner.role == "orchestrator" else "delegate_submit")
                if owner.role == "slide":
                    item["submit_task"] = {"task_id": f"completion-recheck-{uuid.uuid4().hex[:16]}",
                        "role": "slide", "group_id": owner.group_id, "pages": list(owner.pages),
                        "repair": True, "depends_on": [], "goal": goal}
                    missing = [self.workspace.paths.relative(self.workspace.paths.page_html(page))
                               for page in owner.pages if not self.workspace.paths.page_html(page).is_file()]
                    if missing:
                        item.update(prerequisite={
                            "code": "slide_preparation_missing", "missing_paths": missing,
                            "detail": "使用 submit_task 交回原 Owner；Harness 在接受前按有效计划补齐缺失 HTML，"
                                      "不覆盖已有页面、不改变归属。若计划或共享字体准备失败，先修正回执指出的问题再提交。"})
            recovery.append(item)
        return ToolExecutionError(code, detail, diagnostics={"page_issues": list(page_issues), "recovery": recovery})

    def _completion_pages(self) -> tuple[int, ...]:
        """先校验便宜的当前状态；不以历史 ready 或模型结束文案作为完成证据。"""
        state = self.orchestration.snapshot()
        try:
            assert_tasks_terminal(state)
        except OrchestrationError as exc:
            raise ToolExecutionError("unfinished_tasks", str(exc)) from exc
        requests = [key for key, row in state.get("requests", {}).items() if row.get("status") == "pending"]
        if requests or self.orchestration.background_pending():
            raise ToolExecutionError("unfinished_tasks", f"Background work or Owner requests remain: {requests}")
        self.tools.assert_closed()
        with self.workspace._lock:
            leases = [key for key, row in self.artifacts._read_state().get("transactions", {}).items()
                      if row.get("status") == "open"]
            if leases:
                raise ToolExecutionError("unfinished_artifact_transactions", f"Artifact writes remain open: {leases}")
            revision_path = self.workspace.paths.state / "active-revision.json"
            if revision_path.is_file():
                revision = json.loads(revision_path.read_text(encoding="utf-8"))
                if revision.get("status") not in {"committed", "aborted"}:
                    raise ToolExecutionError("revision_incomplete",
                        f"Finish the active revision using its recorded impact and commit-revision: {revision.get('revision_id')}")
        pages = self.workspace.pages()
        if not pages:
            raise ToolExecutionError("verification_pages_missing", "Create and inspect the planned pages before completion.")
        try:
            config = self.workspace.creation_config(require_complete=True)
        except (ValueError, OSError) as exc:
            raise ToolExecutionError("creation_config_invalid", str(exc)) from exc
        if config["target_page_count"] is not None and len(pages) != config["target_page_count"]:
            raise ToolExecutionError("verification_page_coverage",
                f"config.json target_page_count={config['target_page_count']}, current pages={len(pages)}; finish the requested structural revision")
        expected = self.orchestration.page_identity_snapshot().get("pages", [])
        actual = {page.page: page.page_uid for page in pages}
        planned = {int(row["position"]): row["page_uid"] for row in expected}
        if planned != actual:
            raise ToolExecutionError("verification_page_coverage", f"Planned pages and current pages differ: planned={planned}, current={actual}")
        incomplete = [page for page in pages if page.lifecycle not in {PageLifecycle.INSPECTED, PageLifecycle.READY}]
        if incomplete:
            raise self._page_completion_error("verification_pages_not_inspected",
                f"Current page inspection evidence is missing or stale: {[page.page for page in incomplete]}. "
                "Use page_issues for the cause and recovery for the original authors; prior checks are not necessarily absent.",
                self.workspace.inspection_diagnostics(incomplete))
        page_numbers = tuple(page.page for page in pages)
        if len(page_numbers) > 1 and self.workspace.group_inspection_receipt("orchestrator", page_numbers) is None:
            raise ToolExecutionError(
                "verification_group_inspection_missing_or_stale",
                "The Orchestrator has not consumed a contact sheet covering every current page version. "
                "Use inspect(page=[...]) with all current page numbers to compare the final deck's title chain, "
                "visual hierarchy, semantic color roles, backgrounds, navigation, image treatment, and rhythm. "
                "A partial or stale contact sheet cannot complete the deck.",
                diagnostics={
                    "pages": list(page_numbers),
                    "recovery": [{
                        "action": "inspect_current_deck_contact_sheet",
                        "pages": list(page_numbers),
                        "review_stage": "draft",
                        "notice": (
                            "联系表只做整册当前版本的跨页比较，不替代各页终稿检查；"
                            "颜色无需逐页相同，但相同语义角色应稳定，背景与强调关系的变化不能使页面像不同模板拼接。"
                        ),
                    }],
                },
            )
        try:
            self.issues.verification(scope="deck", verdict="pass", issue_ids=(), actor="harness", dry_run=True)
        except IssueLedgerError as exc:
            raise ToolExecutionError("verification_rejected", str(exc)) from exc
        return tuple(actual)

    def complete_deck(self) -> dict[str, object]:
        """闭合回合后校验并预生成交付；不生成模型工具动作或关闭语义问题。"""
        self._completion_pages()
        if self.visual_review is not None:
            visual = self.visual_review()
            missing = [row["page"] for row in visual.get("current_inspections", []) if not row.get("attempted")]
            if visual.get("effective") and missing:
                raise ToolExecutionError("visual_review_not_started",
                    f"Current pages lack a Critic attempt: {missing}. Their original authors must inspect the current pages.")
        # 与委派共用锁序；慢评审在锁外，成功写入前重验当前任务、问题和全部资源。
        with self.orchestration._condition, self.workspace._lock:
            pages = self._completion_pages()
            try:
                snapshots = self.workspace.mark_ready_pages(pages)
            except WorkspaceStateError as exc:
                if exc.diagnostics and exc.diagnostics.get("page_issues"):
                    raise self._page_completion_error("verification_resources_stale", str(exc),
                        exc.diagnostics["page_issues"]) from exc
                raise ToolExecutionError("verification_resources_stale", str(exc)) from exc
        finalized = False
        if self.finalize_delivery is not None:
            try:
                self.finalize_delivery()
            except Exception as exc:
                detail = f"后台交付预检失败：{type(exc).__name__}: {str(exc)[:1600]}"
                raise ToolExecutionError(
                    "automatic_finalization_failed",
                    detail,
                    diagnostics={
                        "stage": "delivery_preflight",
                        "detail": detail,
                        "recovery": [{
                            "action": "repair_current_artifacts",
                            "detail": (
                                "保留当前已检查成果；按错误指出的缺页、资源、画布或派生产物问题做最小修复，"
                                "再自然结束以触发一次后台复验。无需调用生命周期命令。"
                            ),
                        }],
                    },
                ) from exc
            finalized = True
        result = self.issues.verification(scope="deck", verdict="pass", issue_ids=(), actor="harness",
            rationale="automatic_completion_current_artifacts")
        result = {"status": "ready", **result}
        if finalized:
            result["automatic_finalization"] = "complete"
        self.live_trace.append(event_type="deck_completion_checked", agent_id="orchestrator", role="orchestrator",
            payload={"actor": "harness", **result, "pages": [
                {"page": page.page, "page_uid": page.page_uid, "html_sha256": page.html_sha256,
                 "png_sha256": page.png_sha256} for page in snapshots]})
        return result

    def add_training_segment(self, segment: TrainingSegmentInput) -> None:
        """按 Agent 与 segment 身份登记或刷新一个真实闭合 checkpoint。"""
        key = (segment.owner_id, segment.checkpoint.segment_id)
        with self._training_lock:
            for index, current in enumerate(self.training_segments):
                if (current.owner_id, current.checkpoint.segment_id) == key:
                    self.training_segments[index] = segment
                    break
            else:
                self.training_segments.append(segment)

    def training_snapshot(self) -> tuple[TrainingSegmentInput, ...]:
        """返回线程安全且顺序稳定的训练 segment 快照。"""
        with self._training_lock:
            return tuple(self.training_segments)

    def capture_training_segment(
        self, owner_id: str, role: str, checkpoint: SegmentCheckpoint,
        *, artifact_paths: Sequence[str] = (),
    ) -> None:
        if self.run_spec.run_mode != RunMode.SYNTHESIS:
            return
        with self._training_lock:
            segment = TrainingExporter(self.workspace, self.artifacts, self.issues,
                run_mode=self.run_spec.run_mode).capture(owner_id, role, checkpoint,
                    artifact_paths=artifact_paths)
            self.add_training_segment(segment)


@dataclass(frozen=True)
class RunOutcome:
    """保存共享生成、交付和可选训练导出的最终结果。"""

    run_mode: RunMode
    workspace: Path
    backend_result: Mapping[str, object]
    delivery: DeliveryManifest
    training_manifest: Mapping[str, object] | None

    def to_dict(self) -> dict[str, object]:
        """把运行结果转换为 CLI 可打印对象。"""
        return {
            "run_mode": self.run_mode.value,
            "workspace": str(self.workspace),
            "backend_result": dict(self.backend_result),
            "delivery": self.delivery.to_dict(),
            "training_manifest": (
                None
                if self.training_manifest is None
                else dict(self.training_manifest)
            ),
        }


class PresenterKernel:
    """装配一次共享控制面，并在交付前执行注入的生成 backend。"""

    def __init__(
        self,
        run_spec: RunSpec,
        *,
        role_runners: Mapping[str, TaskRunner] | None = None,
        renderer: RenderBackend | None = None,
        vision_analyzer: VisionAnalyzer | None = None,
        max_workers: int | None = None,
        environment: Mapping[str, str] | None = None,
        visual_context_image_limit: int | None = None,
    ) -> None:
        """创建状态真源和稳定工具，但不调用模型或选择画布。"""
        self.run_spec = run_spec
        configured_image_limit = (
            int(visual_context_image_limit)
            if visual_context_image_limit is not None
            else int((environment or {}).get("VISION_CONTEXT_IMAGES", 4))
        )
        if not 0 < configured_image_limit <= MAX_CONTEXT_IMAGES:
            raise ValueError(
                f"VISION_CONTEXT_IMAGES must be between 1 and {MAX_CONTEXT_IMAGES}"
            )
        self.visual_context_image_limit = configured_image_limit
        if max_workers is None:
            max_workers = load_runtime_config(environment)["decks"]["owners"]
        self.workspace = WorkspaceState(run_spec.output_workspace)
        self.owners = OwnerRegistry(
            self.workspace.paths.root, deck_id=self.workspace.deck_id
        )
        self.owner_contexts = OwnerContextStore(self.workspace.paths.root)
        self.orchestration = OrchestrationManager(
            self.workspace.paths.root,
            deck_id=self.workspace.deck_id,
            max_workers=max_workers,
            registry=self.owners,
            request_key=hashlib.sha256(json.dumps([run_spec.raw_user_query,
                None if run_spec.revision_request is None else dict(run_spec.revision_request)],
                ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
        )
        self.artifacts = ArtifactStore(
            self.workspace.paths.root, workspace_state=self.workspace
        )
        self.issues = IssueLedger(self.workspace.paths.root)
        self.router = RevisionRouter(
            self.issues, self.owners, artifacts=self.artifacts, request_key=self.orchestration.request_key
        )
        self.live_trace = LiveEventJournal(
            self.workspace.paths.trace / "live-events.jsonl",
            full_payload=True,
        )
        self.tools = ToolRuntime(
            event_sink=self.live_trace.record_tool_event,
        )
        selected_renderer = renderer or _missing_renderer
        bind_live_trace = getattr(selected_renderer, "bind_live_trace", None)
        if callable(bind_live_trace):
            bind_live_trace(self.live_trace)
        selected_analyzer = vision_analyzer or _missing_vision_analyzer
        # 各目标共用渲染与分析；图片被调用者实际消费后再提交回执，
        # 只有真实作者的单页消费推进 INSPECTED。
        inspect_handler, commit_inspection = (
            build_inspect_handlers(
                self.workspace,
                self.artifacts,
                selected_renderer,
                selected_analyzer,
            )
        )
        # tools.py 是工具契约（名称/Schema/角色/权限）的唯一真源；这里只提供
        # 每个控制工具的 Handler Adapter，适配器调用领域对象的普通方法。
        handlers: dict[str, ToolHandler] = {
            "delegate_submit": self._delegate_submit_handler(),
            "delegate_wait": self._delegate_wait_handler(),
            "request_orchestrator": self._request_orchestrator_handler(),
            "respond_owner_request": self._respond_owner_request_handler(),
            "artifact_commit": self._artifact_commit_handler(),
            "render": self._render_handler(selected_renderer),
            "inspect": inspect_handler,
            "verification_submit": self._verification_handler(),
        }
        register_tools(self.tools, handlers, require_complete=False)
        self.tools.deferred_inspection_committer = commit_inspection
        self.services = RuntimeServices(
            run_spec=run_spec,
            workspace=self.workspace,
            owners=self.owners,
            owner_contexts=self.owner_contexts,
            orchestration=self.orchestration,
            artifacts=self.artifacts,
            issues=self.issues,
            router=self.router,
            tools=self.tools,
            live_trace=self.live_trace,
        )
        if run_spec.run_mode == RunMode.SYNTHESIS:
            for segment in TrainingExporter(self.workspace, self.artifacts, self.issues,
                    run_mode=run_spec.run_mode).load_segments():
                self.services.add_training_segment(segment)
        for role, runner in (role_runners or {}).items():
            self.orchestration.register_runner(role, self._wrap_runner(runner))
        self._write_run_manifest()

    def execute(
        self,
        backend: GenerationBackend,
        *,
        include_pdf: bool = False,
        include_pptx: bool = False,
        include_zip: bool = False,
        title: str = "MURAL Presentation",
    ) -> RunOutcome:
        """运行共享 backend、验证任务闭合并完成 raw；训练导出独立执行。"""
        try:
            raw_result = backend(self.services)
            if raw_result is not None and not isinstance(raw_result, Mapping):
                raise TypeError("generation backend must return an object or None")
            backend_result = dict(raw_result or {})
            self.tools.assert_closed()
            self._assert_tasks_terminal()
            delivery_exporter = DeliveryExporter(
                self.workspace, self.artifacts, self.issues
            )
            delivery = delivery_exporter.export(
                include_pdf=include_pdf,
                include_pptx=include_pptx,
                include_zip=include_zip,
                title=title,
                publish_result=False,
            )
            visual = backend_result.get("external_visual_critic")
            if self.run_spec.run_mode == RunMode.SYNTHESIS:
                if (isinstance(visual, Mapping) and visual.get("effective")
                        and visual.get("critic_incomplete")):
                    delivery_exporter.write_result(delivery, visual_review=visual)
                    missing = [row for row in visual.get("current_inspections", [])
                               if not row.get("completed")]
                    detail = "; ".join(f"P{row['page']}: {row.get('incomplete_reason') or 'missing_current_critic'}"
                                       for row in missing)
                    from runtime.skill_runtime import SkillRuntimeError
                    raise SkillRuntimeError(
                        "synthesis visual Critic incomplete: " + detail
                        + "; resume this workspace to complete missing reviews",
                    )
            delivery_exporter.write_result(delivery, visual_review=visual if
                self.run_spec.run_mode == RunMode.SYNTHESIS and isinstance(visual, Mapping) else None)
            return RunOutcome(
                run_mode=self.run_spec.run_mode,
                workspace=self.workspace.paths.root,
                backend_result=backend_result,
                delivery=delivery,
                training_manifest=None,
            )
        finally:
            try:
                self.orchestration.close(wait=True)
            finally:
                self._write_replay_traces()

    def _delegate_submit_handler(self) -> ToolHandler:
        """Adapter：向持久任务图提交责任与依赖。"""
        def handle(_context: ToolContext, arguments: Mapping[str, object]) -> object:
            try:
                tasks = arguments.get("tasks") or []
                if not isinstance(tasks, list):
                    raise OrchestrationError("delegate_submit tasks must be an array")
                result = self.orchestration.submit_tasks(tasks)
                result.update(self.orchestration.page_coverage())
                return _with_parallelization_advisories(result,
                    submitted_tasks=tasks,
                    snapshot_tasks=(self.orchestration.snapshot().get("tasks") or {}))
            except OrchestrationError as exc:
                raise ToolExecutionError("delegation_rejected", str(exc),
                    diagnostics=exc.diagnostics) from exc

        return handle

    def _delegate_wait_handler(self) -> ToolHandler:
        """Adapter：等待任务事件，直接带回当前结果和待回复请求。"""
        def handle(context: ToolContext, arguments: Mapping[str, object]) -> object:
            try:
                event = self.orchestration.wait(
                    consumer_id=context.owner_id,
                    after_sequence=arguments.get("after_sequence"),
                )
                event = _with_parallelization_advisories(event,
                    snapshot_tasks=(self.orchestration.snapshot().get("tasks") or {}))
                return _compact_delegate_wait_payload(event)
            except ResourceSlotTimeout as exc:
                raise ToolExecutionError("resource_queue_timeout", str(exc)) from exc
            except InterruptedError as exc:
                raise ToolExecutionError("owner_interrupted", str(exc)) from exc
            except OrchestrationError as exc:
                raise ToolExecutionError("delegation_wait_failed", str(exc)) from exc
        return handle


    def _request_orchestrator_handler(self) -> ToolHandler:
        """Adapter：把 Slide Owner 求助绑定到当前持久 task_id。"""
        def handle(context: ToolContext, arguments: Mapping[str, object]) -> object:
            task_id = str(context.metadata.get("task_id") or "")
            if not task_id:
                raise ToolExecutionError(
                    "owner_task_missing",
                    "request_orchestrator requires task_id in ToolContext metadata",
                )
            try:
                return self.orchestration.request_owner_assistance(
                    task_id,
                    request_type=str(arguments["issue_type"]),
                    detail=str(arguments["question"]),
                    pages=tuple(int(page) for page in arguments.get("pages", [])),
                    evidence=str(arguments.get("evidence") or ""),
                )
            except OwnerRequestInterrupted as exc:
                raise ToolExecutionError("owner_interrupted", str(exc)) from exc
            except OrchestrationError as exc:
                raise ToolExecutionError("owner_request_failed", str(exc)) from exc

        return handle

    def _respond_owner_request_handler(self) -> ToolHandler:
        """Adapter：把 Orchestrator 的结构化决定送回等待中的 Owner。"""
        def handle(_context: ToolContext, arguments: Mapping[str, object]) -> object:
            response = {
                "status": str(arguments["status"]),
                "resolution": str(arguments["resolution"]),
                "references": [str(item) for item in arguments.get("references", [])],
            }
            try:
                return self.orchestration.respond_owner_request(
                    str(arguments["request_id"]),
                    response,
                )
            except OrchestrationError as exc:
                raise ToolExecutionError("owner_response_rejected", str(exc)) from exc

        return handle

    def _artifact_commit_handler(self) -> ToolHandler:
        """Adapter：用 ToolContext Owner 和可选 lease 提交产物哈希。"""
        def handle(context: ToolContext, arguments: Mapping[str, object]) -> object:
            try:
                records = self.artifacts.commit(
                    owner_id=context.owner_id,
                    artifact_ids=tuple(str(item) for item in arguments["artifact_ids"]),
                    source_hashes=dict(arguments["source_hashes"]),
                    transaction_id=str(
                        context.metadata.get("artifact_transaction_id") or ""
                    ),
                )
                return {"artifacts": [record.to_dict() for record in records]}
            except (ArtifactStateError, WorkspaceStateError) as exc:
                raise ToolExecutionError("artifact_commit_failed", str(exc)) from exc

        return handle

    def _render_handler(self, renderer: RenderBackend) -> ToolHandler:
        """Adapter：渲染显式页码并返回每页同源快照。"""
        def handle(context: ToolContext, arguments: Mapping[str, object]) -> object:
            pages = tuple(int(item) for item in arguments["pages"])
            if context.role == "slide" and any(
                not self.workspace.is_page_author(page, context.owner_id) for page in pages
            ):
                raise ToolExecutionError("render_scope_denied", "Slide Owner 只能渲染自己负责的页面；跨页参考不授予写入渲染产物的权限")
            try:
                snapshots = self.artifacts.render_pages(
                    pages, renderer
                )
                return {"pages": [{**snapshot.to_dict(), "geometry":
                    self.artifacts.render_receipt(snapshot.page)["canvas"], "render_diagnostics":
                    self.artifacts.render_receipt(snapshot.page).get("diagnostics", {})}
                    for snapshot in snapshots]}
            except (ArtifactStateError, WorkspaceStateError) as exc:
                raise ToolExecutionError("render_failed", str(exc)) from exc

        return handle

    def _verification_handler(self) -> ToolHandler:
        """只接收模型的问题反馈；整册完成由闭合回合后的 Harness 负责。"""
        return self._submit_verification

    def _submit_verification(
        self, context: ToolContext, arguments: Mapping[str, object],
    ) -> dict[str, object]:
        """登记新问题或显式确认已复核的修复，不声明整册完成。"""
        try:
            status = str(arguments["status"])
            raw_issues = arguments.get("issues") or []
            if not isinstance(raw_issues, list) or not all(
                isinstance(item, Mapping) for item in raw_issues
            ):
                raise IssueLedgerError("verification issues must be an array of objects")
            if status == "issues_found":
                if not raw_issues:
                    raise IssueLedgerError("issues_found requires at least one issue")
                if arguments.get("resolved_issue_ids"):
                    raise IssueLedgerError(
                        "issues_found cannot resolve earlier issues in the same call"
                    )
                pages = {page.page: page for page in self.workspace.pages()}
                unknown = sorted({n for issue in raw_issues for n in issue["positions"]} - pages.keys())
                if unknown:
                    raise IssueLedgerError(f"verification issues reference unknown pages: {unknown}")
                bound_issues = []
                for issue in raw_issues:
                    positions = sorted(set(issue["positions"]))
                    bound_issues.append({**issue, "positions": positions,
                        "page_uids": [pages[n].page_uid for n in positions],
                        "artifact_versions": [[page.page_uid, page.html_sha256, page.png_sha256]
                                              for n, page in pages.items() if not positions or n in positions]})
                records = self.issues.record_orchestrator_issues(bound_issues)
                result = self.issues.verification(
                    scope="deck",
                    verdict="repair_required",
                    issue_ids=[record.issue_id for record in records],
                    actor=context.owner_id,
                )
                return {"status": status, **result,
                        "routes": [self.router.route_issue(record.issue_id).to_dict() for record in records]}
            if status != "issues_resolved":
                raise IssueLedgerError("status must be issues_found or issues_resolved; completion is automatic")
            if raw_issues:
                raise IssueLedgerError("issues_resolved must not include new issues")
            resolved = arguments.get("resolved_issue_ids") or []
            if not isinstance(resolved, list) or not resolved:
                raise IssueLedgerError("issues_resolved requires nonempty resolved_issue_ids")
            with self.workspace._lock:
                records = {record.issue_id: record for record in self.issues.issues()}
                unknown = sorted(set(resolved) - records.keys())
                if unknown:
                    raise IssueLedgerError(f"unknown issue ids: {unknown}")
                affected = sorted({page for issue_id in resolved for page in
                    (records[issue_id].pages or tuple(page.page for page in self.workspace.pages()))})
                missing = sorted(set(affected) - {page.page for page in self.workspace.pages()})
                if missing:
                    raise IssueLedgerError(f"resolved issues reference missing pages: {missing}")
                snapshots = [self.workspace.page(page) for page in affected]
                incomplete = [page for page in snapshots if page.lifecycle not in {PageLifecycle.INSPECTED, PageLifecycle.READY}]
                if not affected or incomplete:
                    raise self.services._page_completion_error("verification_pages_not_inspected",
                        f"Current affected pages need valid inspection evidence before confirming repairs: {affected}",
                        self.workspace.inspection_diagnostics(incomplete))
                try:
                    self.workspace.assert_ready_resources(affected)
                except WorkspaceStateError as exc:
                    if exc.diagnostics and exc.diagnostics.get("page_issues"):
                        raise self.services._page_completion_error("verification_resources_stale", str(exc),
                            exc.diagnostics["page_issues"]) from exc
                    raise ToolExecutionError("verification_resources_stale", str(exc)) from exc
                return {"status": status, **self.issues.resolve_issues(resolved, actor=context.owner_id,
                    rationale="orchestrator_reverified_current_artifacts")}
        except IssueLedgerError as exc:
            raise ToolExecutionError("verification_rejected", str(exc)) from exc

    def _wrap_runner(self, runner: TaskRunner) -> TaskRunner:
        """任务完成只校验当前产物；整页 ready 由 Harness 整册收尾提交。"""
        def wrapped(task, manager):
            """阶段完成不等于内容完整，不解释自由文本来推断 ready。"""
            result = runner(task, manager)
            repair_required = isinstance(result, Mapping) and (
                result.get("status") in {"repair_required", "repair_handoff"}
            )
            if task.role == "slide" and not repair_required:
                for page in task.pages:
                    snapshot = self.workspace.page(page)
                    if snapshot.lifecycle in {PageLifecycle.INSPECTED, PageLifecycle.READY}:
                        self.workspace.assert_ready_resources((page,))
            return result

        return wrapped

    def _assert_tasks_terminal(self) -> None:
        """拒绝在 Owner 仍运行、等待或中断时进入导出。"""
        assert_tasks_terminal(self.orchestration.snapshot())

    def _write_run_manifest(self) -> None:
        """保存不含凭证的原始运行输入和 Profile。"""
        if (self.workspace.paths.state / "run.json").exists():
            return
        value = {
            "schema": "mural.run.v1",
            **self.run_spec.to_manifest(),
            "profile": {
                "visual_context_image_limit": self.visual_context_image_limit,
                "visual_context_selection": "closed_interactions_within_token_budget",
                "compact_active_history": self.run_spec.profile.compact_active_history,
                "require_complete_trace": self.run_spec.profile.require_complete_trace,
            },
        }
        _atomic_json_write(self.workspace.paths.state / "run.json", value)

    def _write_replay_traces(self) -> None:
        """离线接口只由 journal 重建，不合并旧投影或进程内第二份事件。"""
        for kind in ("agent", "provider", "tool"):
            payload = "".join(json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                              for event in iter_replay_events(self.live_trace.path, kind))
            path = self.workspace.paths.trace / f"{kind}-events.jsonl"
            if kind == "tool" or payload or path.exists():
                _atomic_text_write(path, payload)


def _inspection_id(
    relative: str, sha256: str, question: str, payload: Mapping[str, object]
) -> str:
    """由分析目标的机械内容生成稳定 inspection id。"""
    body = json.dumps(
        {
            "path": relative,
            "sha256": sha256,
            "question": question,
            "result": dict(payload),
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return f"inspection-{hashlib.sha256(body.encode('utf-8')).hexdigest()[:20]}"


def _materialize_vision_target(
    workspace: WorkspaceState, target: Path, relative: str
) -> Path:
    """真实文件不存在但存在页图描述时，用 A 的 resolver 触发按需物化。"""
    if target.is_file():
        return target
    root = workspace.paths.root
    if target.with_suffix(".json").is_file():
        return materialize_page_image(root, relative)
    # 逻辑附件页图（page_image_paths）在 manifest 中登记但尚未光栅化时也按需物化。
    for record in resolve_attachment_records(root):
        for asset in resolve_page_assets(record):
            if asset.resolve() == target.resolve() and not asset.is_file():
                return materialize_page_image(root, workspace.paths.relative(asset))
    return target


def _registered_deck_page(artifacts: ArtifactStore, target: Path) -> int | None:
    """当且仅当目标恰好是某个已登记 Deck 渲染 PNG 时返回其页码。"""
    resolved = target.resolve()
    matches = [
        page.page
        for page in artifacts.workspace.pages()
        if artifacts.paths.resolve(page.png_path) == resolved
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _reviewed_asset_handoff(
    context: ToolContext,
    artifacts: ArtifactStore,
    target: Path,
    relative: str,
    *,
    force_pixels: bool = False,
) -> dict[str, object] | None:
    """Reuse a current Image Owner review instead of reopening raw pixels.

    This is deliberately limited to the Orchestrator and to an exact committed
    asset hash.  It does not claim that the Orchestrator saw the image and it
    does not replace inspection of the composed slide.
    """
    if context.role != "orchestrator" or force_pixels:
        return None
    digest = file_sha256(target)
    for row in artifacts.catalog().get("images", ()):
        if not isinstance(row, Mapping) or row.get("path") != relative:
            continue
        metadata = row.get("image_metadata") or {}
        if (
            row.get("status") != "committed"
            or row.get("sha256") != digest
            or not isinstance(metadata, Mapping)
            or metadata.get("reviewed_sha256") != digest
            or not metadata.get("reviewed_by")
            or metadata.get("reviewed_by") == context.owner_id
        ):
            return None
        slides = metadata.get("slides")
        consumer_pages = list(slides) if isinstance(slides, list) else []
        own_pixels_seen = artifacts.workspace.inspection_attempt_count(
            kind="asset", owner_id=context.owner_id, png_sha256=digest,
        ) > 0
        return {
            "kind": "asset",
            "path": relative,
            "sha256": digest,
            "inspection_cached": True,
            "native_multimodal": False,
            "cross_owner_review_reused": True,
            "visual_payload_status": ("pixels_delivered_in_previous_model_turn" if own_pixels_seen
                                      else "review_reused_from_other_owner"),
            "pixels_delivered_to_this_owner": own_pixels_seen,
            "pixels_attached_in_this_result": False,
            "asset_usage_status": "reviewed_asset_available",
            "checked_scope": "image_owner_asset_usability_handoff",
            "consumer_pages": consumer_pages,
            "not_checked": ["composed page crop, contrast, hierarchy, and surrounding layout"],
            "next_required_action": (
                "Use the reviewed asset through assets/catalog.md or the image handoff, "
                "then inspect the composed page. Reopen raw pixels only for a specific unresolved "
                "asset question by setting retain=true."
            ),
            "note": (
                "The current file hash was already consumed and reviewed by its Image Owner. "
                "This reuses that handoff; it does not claim new pixel inspection by the Orchestrator."
            ),
        }
    return None


def _inspection_member(page) -> dict[str, object]:
    return {key: getattr(page, key) for key in ("page", "page_uid", "html_sha256", "png_sha256")}


def _inspection_descriptor(
    context: ToolContext, *, kind: str, scope: str, question: str,
    members: Sequence[Mapping[str, object]], path: str, sha256: str,
    presentation_config: Mapping[str, object], review_stage: str = "draft",
) -> dict[str, object]:
    """检查目的和消费角色独立于像素版本；同像素换目的仍需真实消费。"""
    stage = str(review_stage or "draft").strip().lower() or "draft"
    if stage not in {"draft", "final", "blocking_recheck"}:
        raise ToolExecutionError("invalid_arguments", "review_stage 只能是 draft、final 或 blocking_recheck。")
    page = int(members[0]["page"]) if kind == "page" and members else None
    page_uid = str(members[0].get("page_uid") or "") if kind == "page" and members else ""
    html_sha256 = str(members[0].get("html_sha256") or "") if kind == "page" and members else ""
    prepared = inspection_with_critic_question(InspectionClosed(
        agent_id=context.owner_id, role=context.role, inspection_id="",
        html_sha256=html_sha256, png_sha256=sha256, png_path=path, workspace_root="",
        page=page, page_uid=page_uid, question=question.strip(), kind=kind,
        members=tuple(members)))
    identity = {"kind": kind, "scope": scope, "actor": context.owner_id,
                "role": context.role, "question": prepared.question,
                "review_stage": stage,
                "owner_question": prepared.owner_question,
                "critic_question": prepared.critic_question,
                "critic_prompt_version": prepared.critic_prompt_version,
                "page_uids": [member["page_uid"] for member in members],
                **({"path": path} if kind == "asset" else {}),
                "presentation_config": dict(presentation_config)}
    scope_key = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    # Purpose remains in the cache identity; evidence progress must not depend on wording.
    evidence = {"kind": kind, "actor": context.owner_id,
                "review_stage": stage, "sha256": sha256,
                "page_uids": identity["page_uids"],
                "presentation_config": dict(presentation_config)}
    evidence_key = hashlib.sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    receipt = {"kind": kind, "scope_key": scope_key, "owner_id": context.owner_id, "role": context.role,
               "evidence_key": evidence_key,
               "question": identity["question"],
               "review_stage": stage,
               "owner_question": identity["owner_question"],
               "critic_question": identity["critic_question"],
               "critic_prompt_version": identity["critic_prompt_version"],
               "members": list(members), "path": path, "sha256": sha256,
               "presentation_config": dict(presentation_config)}
    receipt["inspection_id"] = _inspection_id(path, sha256, scope_key, {"members": list(members)})
    if kind == "page":
        receipt.update(members[0])
    return receipt


def _inspection_response(workspace: WorkspaceState, descriptor: Mapping[str, object]) -> dict[str, object]:
    cached = workspace.inspection_receipt(str(descriptor["scope_key"]), descriptor["members"])
    prior_pixel_delivery = cached is not None or workspace.inspection_attempt_count(
        kind=str(descriptor["kind"]), owner_id=str(descriptor["owner_id"]),
        png_sha256=str(descriptor["sha256"]),
        page=descriptor.get("page"), page_uid=str(descriptor.get("page_uid") or ""),
    ) > 0
    current = dict(descriptor)
    if cached is not None:
        rebind = cached["members"] != descriptor["members"] or (
            descriptor["kind"] == "page" and workspace.page(int(descriptor["page"])).lifecycle == PageLifecycle.RENDERED
        )
        if rebind:
            current["reused_from_inspection_id"] = cached.get("reused_from_inspection_id") or cached["inspection_id"]
            if descriptor["kind"] == "group":
                workspace.record_group_inspection(current)
            elif descriptor["kind"] == "asset":
                workspace.record_asset_inspection(current)
            else:
                workspace.mark_inspected(int(descriptor["page"]), inspection_id=str(descriptor["inspection_id"]),
                    expected=descriptor, receipt=current)
        elif cached.get("reused_from_inspection_id"):
            current["reused_from_inspection_id"] = cached["reused_from_inspection_id"]
    payload = {"path": descriptor["path"], "sha256": descriptor["sha256"],
               "inspection_id": descriptor["inspection_id"], "scope_key": descriptor["scope_key"],
               "evidence_key": descriptor["evidence_key"],
               "inspection_cached": cached is not None, "native_multimodal": cached is None,
               "visual_payload_status": ("pixels_delivered_in_previous_model_turn" if cached is not None
                                         else "awaiting_pixel_delivery_for_new_purpose" if prior_pixel_delivery
                                         else "awaiting_pixel_delivery"),
               "pixels_delivered_to_this_owner": prior_pixel_delivery,
               "pixels_attached_in_this_result": False}
    if cached is None:
        payload["pending_inspection"] = dict(descriptor)
    else:
        payload["current_inspection"] = current
    return payload


def _write_group_contact_sheet(workspace: WorkspaceState, pages: Sequence[int], name: str) -> Path:
    """复用固定 Skill 的无浏览器拼图路径；不触发页面渲染或字体重建。"""
    paths = workspace.paths
    ref = SharedSkillStore.for_workspace(paths.root).load_ref(paths)
    module = load_skill_module(ref.object_root, ref.tree_sha256, "render_deck")
    renderer = module.DeckRenderer(paths.root / "present.html", paths.renders, workspace=paths.root, motion_check=False)
    target = renderer._contact_sheet([paths.page_png(page) for page in pages], name=name, labels=list(pages))
    if target is None:
        raise WorkspaceStateError("group contact sheet generation failed")
    return target


def _page_render_is_current(
    workspace: WorkspaceState, artifacts: ArtifactStore, page: int,
    renderer: RenderBackend | None = None,
) -> bool:
    """判断某页是否已有与当前 HTML 同源、可直接复用的渲染。"""
    snapshot = workspace.page(page)
    if snapshot.lifecycle not in {
        PageLifecycle.RENDERED,
        PageLifecycle.INSPECTED,
        PageLifecycle.READY,
    }:
        return False
    if snapshot.stale_png:
        return False
    try:
        artifacts.validate_render_receipt(page)
    except (ArtifactStateError, WorkspaceStateError):
        return False
    cache_check = getattr(renderer, "cache_receipt_valid", None)
    return cache_check is None or bool(cache_check(artifacts.render_receipt(page)))


def validate_inspection_target(
    workspace: WorkspaceState, artifacts: ArtifactStore, descriptor: Mapping[str, object],
) -> None:
    """检查分析前后与消费时的同一版本；联系表绝不代表逐页检查。"""
    with workspace._lock:
        for member in descriptor.get("members", ()):
            page = workspace.page(int(member["page"]))
            if page.page_uid != member.get("page_uid"):
                raise WorkspaceStateError("inspection page identity changed",
                    diagnostics={"code": "page_identity_changed"})
            artifacts.validate_render_receipt(page.page)
            if any(member.get(key) != getattr(page, key)
                   for key in ("html_sha256", "png_sha256")):
                raise WorkspaceStateError("inspection page version changed",
                    diagnostics={"code": "inspection_version_changed"})
        if descriptor.get("kind") != "asset" and descriptor.get("presentation_config") != workspace.presentation_config():
            raise WorkspaceStateError("presentation language changed during inspection",
                diagnostics={"code": "presentation_language_changed"})
        if file_sha256(workspace.paths.resolve(str(descriptor["path"]))) != descriptor.get("sha256"):
            raise WorkspaceStateError("inspection image changed",
                diagnostics={"code": "inspection_version_changed"})


def _inspect_timings(
    started: float,
    rendered: float,
    *,
    receipts: Sequence[Mapping[str, object]] = (),
    rendered_now: bool = False,
) -> dict[str, float]:
    """Inspect wall clock plus, when this call actually rendered, slot/CLI breakdown."""
    timings = {
        "render_and_commit": rendered - started,
        "inspection_prepare": time.monotonic() - rendered,
    }
    if not rendered_now:
        return timings
    for receipt in receipts:
        diagnostics = receipt.get("diagnostics") if isinstance(receipt, Mapping) else None
        raw = diagnostics.get("timings_s") if isinstance(diagnostics, Mapping) else None
        if not isinstance(raw, Mapping):
            continue
        for key in ("slot_wait", "browser_host_ready", "skill_command", "font_receipt",
                    "receipt_decode", "total"):
            value = raw.get(key)
            if isinstance(value, (int, float)):
                timings[f"render_{key}"] = float(value)
        break
    return timings


def build_inspect_handlers(
    workspace: WorkspaceState,
    artifacts: ArtifactStore,
    renderer: RenderBackend | None,
    analyzer: VisionAnalyzer,
) -> tuple[ToolHandler, Callable[[Mapping[str, object]], bool | Mapping[str, object]]]:
    """唯一检查入口；按目标准备像素，共用分析、缓存与延迟消费提交。"""
    paths = workspace.paths

    def _page_access(context: ToolContext, page: int) -> None:
        if not workspace.is_page_author(page, context.owner_id) and not context.may_read(workspace.page(page).png_path):
            raise ToolExecutionError("read_scope_denied", f"Owner 无权查看 P{page}；检查不授予修改或作者身份。")

    def _render_page(page: int) -> bool:
        current = _page_render_is_current(workspace, artifacts, page, renderer)
        if not current:
            try:
                artifacts.render_pages([page], renderer)
            except UncommittedPageSourceError as exc:
                raise ToolExecutionError("render_source_uncommitted", str(exc)) from exc
        return current

    def inspect(context: ToolContext, arguments: Mapping[str, object]) -> object:
        # Handler 也供内部调用；目标分支只按显式参数，不从正文或文件名猜用途。
        if ("page" in arguments) == ("path" in arguments):
            raise ToolExecutionError("invalid_arguments", "page 与 path 必须且只能提供一个。")
        pages = []
        supplemental = str(arguments.get("question") or "")
        review_stage = str(arguments.get("review_stage") or "draft").strip().lower() or "draft"
        started = time.monotonic()
        try:
            target = None
            if "path" in arguments:
                relative = normalize_workspace_path(str(arguments["path"]))
                if not context.may_read(relative):
                    raise ToolExecutionError("read_scope_denied", f"Owner 无权查看图片 {relative!r}")
                try:
                    target = paths.resolve(relative)
                except WorkspacePathError as exc:
                    raise ToolExecutionError("read_scope_denied", str(exc)) from exc
                if not context.may_read(paths.relative(target)):
                    raise ToolExecutionError("read_scope_denied", "图片软链接目标不在读取范围内。")
                target = _materialize_vision_target(workspace, target, relative)
                if not target.is_file():
                    raise ToolExecutionError("path_not_found", f"图片不存在：{relative}")
                registered = _registered_deck_page(artifacts, target)
                if registered is not None:
                    pages = [registered]
                else:
                    handoff = _reviewed_asset_handoff(
                        context,
                        artifacts,
                        target,
                        paths.relative(target),
                        force_pixels=bool(arguments.get("retain")),
                    )
                    if handoff is not None:
                        return handoff
            else:
                raw = arguments["page"]
                pages = raw if isinstance(raw, list) else [raw]
                if (not pages or any(isinstance(page, bool) or not isinstance(page, int) or page < 1 for page in pages)
                        or len(set(pages)) != len(pages)):
                    raise ToolExecutionError("invalid_arguments", "page 必须是正整数或不重复的正整数数组。")
            for page in pages:
                _page_access(context, page)
            kind = "group" if len(pages) > 1 else "page" if pages else "asset"
            question = inspection_question(kind, supplemental)
            reused = {page: _render_page(page) for page in pages}
            rendered = time.monotonic()
            members = [_inspection_member(workspace.page(page)) for page in pages]
            if kind == "group":
                # 版本进入文件名，其他检查不能覆盖仍在模型请求中的旧联系表。
                pixel_members = [{key: member[key] for key in ("page", "page_uid", "png_sha256")} for member in members]
                digest = hashlib.sha256(json.dumps(pixel_members, sort_keys=True).encode()).hexdigest()[:20]
                name = f"group-{digest}.png"
                target = paths.renders / name
                candidate = _inspection_descriptor(context, kind=kind, scope=kind, question=question,
                    presentation_config=workspace.presentation_config(), members=members,
                    path=paths.relative(target), sha256=file_sha256(target), review_stage=review_stage)
                if workspace.inspection_receipt(candidate["scope_key"], members) is None:
                    target = _write_group_contact_sheet(workspace, pages, name)
            elif kind == "page":
                target = paths.resolve(workspace.page(pages[0]).png_path)
            else:
                from PIL import Image

                with Image.open(target) as image:
                    image.verify()
            descriptor = _inspection_descriptor(context, kind=kind, scope=kind, question=question,
                presentation_config=workspace.presentation_config() if pages else {},
                members=members, path=paths.relative(target), sha256=file_sha256(target),
                review_stage=review_stage)
            cached = workspace.inspection_receipt(str(descriptor["scope_key"]), descriptor["members"])
            pixel_replay = None
            if cached is None:
                target_identity = {"kind": kind, "owner_id": context.owner_id,
                                   "png_sha256": str(descriptor["sha256"])}
                if kind == "page":
                    target_identity.update(page=int(descriptor["page"]),
                                           page_uid=str(descriptor["page_uid"]))
                prior_attempts = workspace.inspection_attempt_count(**target_identity)
                first_final = review_stage == "final" and not workspace.inspection_attempt_count(
                    **target_identity, review_stages=("final", "blocking_recheck"),
                )
                if prior_attempts >= MAX_PAGE_INSPECTIONS_PER_PIXEL and not first_final:
                    if arguments.get("retain") is True:
                        pixel_replay = workspace.cached_pixel_replay(descriptor)
                    if pixel_replay is None:
                        raise ToolExecutionError(
                            "inspection_limit_reached",
                            (
                                f"{descriptor['path']} 当前像素版本已完成 {prior_attempts} 次 {kind} inspect。"
                                "不要继续用新 question 反复重查同一画面；请基于现有回执修复或收口。"
                                "先修复真实阻塞问题，再复查变化后的画面；切换到 blocking_recheck 也不能反复检查未变化的像素。"
                                "如需重新加载自己已消费的当前缓存像素，使用 retain=true；这不会创建新验收。"
                            ),
                            diagnostics={
                                "kind": kind,
                                "page": descriptor.get("page"),
                                "path": descriptor["path"],
                                "png_sha256": descriptor["sha256"],
                                "max_page_inspections_per_pixel": MAX_PAGE_INSPECTIONS_PER_PIXEL,
                                "prior_attempts": prior_attempts,
                                "allowed_extra_stage": "blocking_recheck",
                            },
                        )
                    else:
                        validate_inspection_target(workspace, artifacts, pixel_replay)
                        descriptor = {**pixel_replay, "evidence_key": descriptor["evidence_key"]}
            native = _inspection_response(workspace, descriptor)
            payload = {}
            if not native["inspection_cached"]:
                result = analyzer(target, question, kind, workspace.require_geometry() if pages else {})
                if not isinstance(result, Mapping):
                    raise ValueError("visual analyzer result must be an object")
                payload = copy.deepcopy(dict(result))
            payload.update(native)
            if pixel_replay is not None:
                payload.update(checked_scope="cached_pixels_replay_only", note=(
                    "Reattached this Owner's already-consumed current pixels, without creating a new inspection "
                    "or Critic judgement. The cached review retains its original purpose and stage. "
                    "Use the actual attached pixels for the present question; do not infer a new pass from the cache."
                ))
            payload.update(kind=kind, pages=pages, question=question,
                owner_question=descriptor["owner_question"],
                critic_question=descriptor["critic_question"],
                critic_prompt_version=descriptor["critic_prompt_version"],
                supplemental_question=supplemental,
                review_stage=descriptor["review_stage"],
                owner_id=context.owner_id, deck_page_inspection=kind == "page",
                author_inspection=kind == "page" and workspace.is_page_author(pages[0], context.owner_id),
                render_reused=all(reused.values()) if pages else None,
                member_renders=[workspace.page(page).png_path for page in pages],
                rendered_pages=[page for page in pages if not reused[page]],
                inspect_timings_s=_inspect_timings(
                    started, rendered,
                    receipts=[artifacts.render_receipt(page) for page in pages] if pages else (),
                    rendered_now=bool(pages) and any(not reused[page] for page in pages),
                ))
            if kind == "page":
                snapshot = workspace.page(pages[0])
                page_checks = workspace.inspection_attempt_count(
                    kind="page", page=snapshot.page, page_uid=snapshot.page_uid, include_groups=True,
                )
                if page_checks >= PAGE_INSPECTION_REVIEW_TARGET:
                    blockers = [issue.summary for issue in IssueLedger(workspace.paths.root).issues()
                                if snapshot.page in issue.pages and issue.blocks_positive_admission]
                    payload["review_convergence"] = {
                        "completed_page_checks": page_checks,
                        "target_page_checks": PAGE_INSPECTION_REVIEW_TARGET,
                        "blocking_issues": blockers[:8],
                        "next_action": (
                            "Repair the remaining blockers together, then recheck the changed page."
                            if blockers else
                            "Use this review to close the page if no blockers remain; avoid cosmetic recheck loops."
                        ),
                    }
                receipt = artifacts.render_receipt(snapshot.page)
                payload.update(page=snapshot.page, page_uid=snapshot.page_uid,
                    html_sha256=snapshot.html_sha256, png_sha256=snapshot.png_sha256,
                    render_committed=snapshot.to_dict(), geometry=receipt["canvas"],
                    render_diagnostics=receipt.get("diagnostics", {}),
                    pixel_unchanged=receipt.get("pixel_unchanged", False))
                if payload["pixel_unchanged"]:
                    payload["repair_hint"] = (
                        "HTML changed but rendered PNG is identical. This is not evidence of a successful repair. "
                        "Check current selectors and media parent geometry/margins/height/overflow in render_diagnostics "
                        "before changing object-position again; preserve any unresolved issue.")
            elif kind == "group":
                payload["note"] = str(payload.get("note") or "") + "联系表仅用于跨页比较，不替代逐页全分辨率检查；细节不清时按页号放大。"
            return payload
        except (ArtifactStateError, WorkspaceStateError) as exc:
            return {"pages": pages, "render_ok": False, "native_multimodal": False,
                    "visual_payload_status": "not_delivered_render_failed",
                    "pixels_delivered_to_this_owner": False, "pixels_attached_in_this_result": False,
                    "diagnostics": str(exc)}
        except (OSError, ValueError, AttachmentError) as exc:
            raise ToolExecutionError("inspect_failed", str(exc)) from exc

    def commit_inspection(descriptor: Mapping[str, object]) -> bool | Mapping[str, object]:
        try:
            with workspace._lock:
                validate_inspection_target(workspace, artifacts, descriptor)
                members = descriptor.get("members", ())
                cached = workspace.inspection_receipt(str(descriptor.get("scope_key") or ""), members)
                if cached is not None and cached["inspection_id"] == descriptor["inspection_id"] and cached["members"] == list(members):
                    return False
                kind = descriptor.get("kind")
                if kind == "asset":
                    workspace.record_asset_inspection(descriptor)
                elif kind == "group":
                    workspace.record_group_inspection(descriptor)
                else:
                    artifacts.record_inspection(int(descriptor["page"]),
                        inspection_id=str(descriptor["inspection_id"]), expected=descriptor,
                        receipt=descriptor)
                return True
        except (ArtifactStateError, WorkspaceStateError) as exc:
            diagnostics = dict(exc.diagnostics or {})
            code = str(diagnostics.get("code") or "")
            if code == "image_registration_invalid":
                return {
                    "status": "blocked",
                    "error_code": code,
                    "inspection_id": descriptor["inspection_id"],
                    "pages": [int(member["page"]) for member in descriptor.get("members", ())],
                    "diagnostics": diagnostics,
                    "detail": str(exc),
                }
            if code not in {"resource_changed", "render_receipt_stale",
                    "inspection_version_changed", "page_identity_changed", "presentation_language_changed", "canvas_changed"}:
                raise
            return {"status": "stale", "inspection_id": descriptor["inspection_id"],
                    "pages": [int(member["page"]) for member in descriptor.get("members", ())],
                    "diagnostics": diagnostics, "detail": str(exc)}

    return inspect, commit_inspection


def run_presenter(
    run_spec: RunSpec,
    backend: GenerationBackend,
    *,
    role_runners: Mapping[str, TaskRunner] | None = None,
    renderer: RenderBackend | None = None,
    vision_analyzer: VisionAnalyzer | None = None,
    environment: Mapping[str, str] | None = None,
    include_pdf: bool = False,
    include_pptx: bool = False,
    include_zip: bool = False,
    title: str = "MURAL Presentation",
    visual_context_image_limit: int | None = None,
) -> RunOutcome:
    """以同一 Kernel 执行 inference 或 synthesis。"""
    kernel = PresenterKernel(
        run_spec,
        role_runners=role_runners,
        renderer=renderer,
        vision_analyzer=vision_analyzer,
        environment=environment,
        visual_context_image_limit=visual_context_image_limit,
    )
    return kernel.execute(
        backend,
        include_pdf=include_pdf,
        include_pptx=include_pptx,
        include_zip=include_zip,
        title=title,
    )


def _run_single_deck(
    run_spec: RunSpec,
    environment: Mapping[str, str],
    *,
    mode: RunMode,
    backend: GenerationBackend | None = None,
    role_runners: Mapping[str, TaskRunner] | None = None,
    renderer: RenderBackend | None = None,
    vision_analyzer: VisionAnalyzer | None = None,
    export_existing: bool = False,
    include_pdf: bool = False,
    include_pptx: bool = False,
    include_zip: bool = False,
    title: str = "MURAL Presentation",
    release_verification: Mapping[str, object] | None = None,
) -> RunOutcome:
    """装配并执行一次单 deck 生成。批量层与单 deck CLI 都走这里,绝不各自重写。"""
    WorkspaceState(run_spec.output_workspace)
    if release_verification is not None:
        _atomic_json_write(
            run_spec.output_workspace / "_state" / "release.json",
            release_verification,
        )
    selected_backend = backend
    selected_renderer = renderer
    selected_vision_analyzer = vision_analyzer
    visual_context_image_limit = None
    if selected_backend is None and export_existing:
        selected_backend = _existing_workspace_backend
    if selected_backend is None:
        from runtime.skill_runtime import SkillRuntime

        production_runtime = SkillRuntime(
            run_spec,
            environment,
            renderer=selected_renderer,
        )
        selected_backend = production_runtime.backend
        selected_renderer = production_runtime.renderer
        if selected_vision_analyzer is None:
            selected_vision_analyzer = production_runtime.vision_analyzer
        visual_context_image_limit = production_runtime.prepared.settings.vision_context_images
    return run_presenter(
        run_spec,
        selected_backend,
        role_runners=role_runners,
        renderer=selected_renderer,
        vision_analyzer=selected_vision_analyzer,
        environment=environment,
        visual_context_image_limit=visual_context_image_limit,
        include_pdf=include_pdf,
        include_pptx=include_pptx,
        include_zip=include_zip,
        title=title,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    backend: GenerationBackend | None = None,
    role_runners: Mapping[str, TaskRunner] | None = None,
    renderer: RenderBackend | None = None,
    vision_analyzer: VisionAnalyzer | None = None,
    forced_mode: RunMode | None = None,
) -> int:
    """解析共享 CLI,并默认执行正式 Skill-backed 生成 runtime。"""
    selected_args = list(argv) if argv is not None else sys.argv[1:]
    if selected_args and selected_args[0] == "rebuild-readable-trace":
        rebuild_parser = argparse.ArgumentParser(description="Rebuild Agent projections from recorded live events, without models")
        rebuild_parser.add_argument("workspace")
        target = rebuild_parser.parse_args(selected_args[1:])
        from runtime.workspace_paths import WorkspacePaths
        from types import SimpleNamespace
        paths = WorkspacePaths.from_root(target.workspace)
        if not paths.path("workspace_manifest").is_file():
            raise ValueError("unsupported_workspace_layout: workspace manifest is missing")
        print(json.dumps(export_readable_by_role(SimpleNamespace(paths=paths)), ensure_ascii=False))
        return 0
    parser = _argument_parser()
    arguments = parser.parse_args(list(argv) if argv is not None else None)
    mode = forced_mode or RunMode(arguments.mode)
    environment = merge_environment(
        read_environment_file(arguments.runtime_env), os.environ
    )
    if arguments.doctor:
        if not arguments.json:
            parser.error("--doctor 需要 --json 指定报告路径")
        from runtime.production import EnvironmentDoctor
        report_path = Path(arguments.json).expanduser().absolute()
        if report_path.exists() and not arguments.force:
            parser.error("报告已存在；使用 --force 重新检查并替换")
        spec = RunSpec.from_inputs(raw_user_query="Environment Doctor", output_workspace=report_path.parent,
                                   attachments=arguments.attachment, environment=environment)
        report = EnvironmentDoctor(spec, None, environment).run(raise_on_error=False, report_path=report_path, full=True)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["status"] == "pass" else 1
    if arguments.json or arguments.force:
        parser.error("--json / --force 仅用于 --doctor")
    # 批量入口:--queries 给出时,复用单 deck 路径逐行跑,不复制 agent loop。
    if arguments.queries:
        if not arguments.batch:
            parser.error("--queries 需要同时给出 --batch")
        from runtime.batch import BatchArgs, run_batch

        batch_args = BatchArgs(
            queries=Path(arguments.queries),
            batch=str(arguments.batch),
            workers=int(arguments.workers),
            max_attempts=int(arguments.max_attempts),
            resume=bool(arguments.resume),
            limit=int(arguments.limit or 0),
            run_mode=mode,
            authoring_profile=arguments.authoring_profile,
            include_pdf=arguments.export_existing and not arguments.no_pdf,
            include_pptx=arguments.export_existing and not arguments.no_pptx,
            include_zip=arguments.export_existing and not arguments.no_zip,
            title=arguments.title,
        )

        def _run_deck(run_spec: RunSpec, deck_env: Mapping[str, str]) -> RunOutcome:
            release_verification = None
            if backend is None:
                from runtime.release_guard import verify_runtime_release

                release_verification = verify_runtime_release(mode.value)
            return _run_single_deck(
                run_spec,
                deck_env,
                mode=mode,
                backend=backend,
                role_runners=role_runners,
                renderer=renderer,
                vision_analyzer=vision_analyzer,
                export_existing=arguments.export_existing,
                include_pdf=arguments.export_existing and not arguments.no_pdf,
                include_pptx=arguments.export_existing and not arguments.no_pptx,
                include_zip=arguments.export_existing and not arguments.no_zip,
                title=arguments.title,
                release_verification=release_verification,
            )

        return run_batch(batch_args, run_deck=_run_deck, environment=environment)

    # 单 deck 入口。
    if not arguments.workspace:
        parser.error("--query 需要同时给出 --workspace")
    release_verification = None
    if backend is None:
        from runtime.release_guard import verify_runtime_release

        release_verification = verify_runtime_release(mode.value)
    run_spec = RunSpec.from_inputs(
        raw_user_query=arguments.query,
        output_workspace=arguments.workspace,
        attachments=arguments.attachment,
        attachment_intents=(arguments.attachment_intent or None),
        run_mode=mode,
        authoring_profile=arguments.authoring_profile,
        page_count_hint=arguments.page_count,
        revision_request=_revision_request(arguments.revision_json),
        language=arguments.language,
        environment=environment,
    )
    outcome = _run_single_deck(
        run_spec,
        environment,
        mode=mode,
        backend=backend,
        role_runners=role_runners,
        renderer=renderer,
        vision_analyzer=vision_analyzer,
        export_existing=arguments.export_existing,
        include_pdf=arguments.export_existing and not arguments.no_pdf,
        include_pptx=arguments.export_existing and not arguments.no_pptx,
        include_zip=arguments.export_existing and not arguments.no_zip,
        title=arguments.title,
        release_verification=release_verification,
    )
    print(json.dumps(outcome.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0


def _argument_parser() -> argparse.ArgumentParser:
    """构造 inference/synthesis 共用 CLI 参数(单 deck 或批量二选一)。"""
    parser = argparse.ArgumentParser(description="MURAL Presenter v1")
    # 单 deck(--query --workspace)与批量(--queries --batch)二选一;两者都复用同一
    # 单 deck 执行路径,批量层只在其外面做读 jsonl / 并发 / 重试 / summary。
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--query", help="单条 inline query(单 deck)")
    source.add_argument("--doctor", action="store_true", help="仅运行当前 EnvironmentDoctor，不创建 Deck 或调用生成模型")
    source.add_argument(
        "--queries", help="query jsonl 文件,每行一个 deck(批量合成入口)"
    )
    parser.add_argument("--workspace", help="单 deck 输出工作区(与 --query 搭配)")
    parser.add_argument("--json", help="Doctor JSON 报告路径")
    parser.add_argument("--force", action="store_true", help="重新运行 Doctor 并替换已有报告，不复用缓存")
    parser.add_argument("--attachment", action="append", default=[])
    parser.add_argument(
        "--attachment-intent",
        action="append",
        default=[],
        help=(
            "每个附件的显式用途,顺序与 --attachment 一一对应"
            "(style_reference/content_source/mixed);不给即保守默认"
        ),
    )
    parser.add_argument(
        "--mode", choices=[mode.value for mode in RunMode], default="inference"
    )
    parser.add_argument(
        "--authoring-profile",
        choices=["auto", "general"],
        default=None,
        help="未传时沿用 MURAL_AUTHORING_PROFILE；显式 auto 使用 general；材料约束只来自 prompt",
    )
    parser.add_argument("--language", default="auto")
    parser.add_argument("--page-count", type=int)
    parser.add_argument("--revision-json", default="")
    parser.add_argument("--title", default="MURAL Presentation")
    parser.add_argument(
        "--runtime-env",
        default=str(Path(__file__).resolve().with_name("runtime.env")),
    )
    parser.add_argument("--no-pdf", action="store_true")
    parser.add_argument("--no-pptx", action="store_true")
    parser.add_argument("--no-zip", action="store_true")
    parser.add_argument("--export-existing", action="store_true")
    # 批量层参数(仅在 --queries 时生效)。
    parser.add_argument("--batch", help="批次名,决定 runs/<batch>/ 与 <batch>.summary.json")
    parser.add_argument("--workers", type=int, default=8, help="deck 级并发数")
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="单 sample 最多尝试次数(仅对运维/瞬态失败重试;含历史 manifest)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="断点续跑:跳过已到终态(completed/内容排除)的样本",
    )
    parser.add_argument("--limit", type=int, default=0, help="只取前 N 条(0=全部,用于 smoke)")
    return parser


def _revision_request(raw: str) -> Mapping[str, object] | None:
    """解析 WebUI 传入的结构化修订请求，不把它拼接进原始 query。"""
    if not str(raw or "").strip():
        return None
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("revision_json must encode an object")
    return value


def _missing_renderer(
    _html_path: Path, _png_path: Path, _canvas: Mapping[str, object]
) -> Mapping[str, object]:
    """在宿主未注入 Renderer 时给出明确可恢复错误。"""
    raise ArtifactStateError("renderer backend is not configured")


def _missing_vision_analyzer(
    _path: Path, _question: str, _scope: str, _canvas: Mapping[str, object]
) -> Mapping[str, object]:
    """在宿主未注入视觉分析器时给出明确错误。"""
    raise ValueError("vision analyzer backend is not configured")


def _existing_workspace_backend(_services: RuntimeServices) -> Mapping[str, object]:
    """只导出已经 ready 的 Shadow 工作区，不执行生成。"""
    return {"mode": "export_existing"}


def _atomic_json_write(path: Path, value: Mapping[str, object]) -> None:
    """原子写入排序后的运行 JSON。"""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    _atomic_text_write(path, payload)


def _atomic_text_write(path: Path, value: str) -> None:
    """原子写入 UTF-8 运行文本。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.{uuid.uuid4().hex}.pending")
    try:
        with pending.open("x", encoding="utf-8") as handle:
            handle.write(str(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
