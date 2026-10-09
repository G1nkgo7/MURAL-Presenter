"""持久化异步委派，并用条件事件唤醒 Orchestrator。

本模块不调用模型，也不读取 Plan。它只执行结构化 ``delegate_*`` 与
``owner_request`` 动作，维护依赖、Owner 状态和可重启的事件游标。
"""
from __future__ import annotations

from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass
from enum import Enum
import copy
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Callable, Mapping, Sequence
import uuid

from owner.registry import (
    OWNER_ROLES,
    OwnerRecord,
    OwnerRegistry,
    OwnerRegistryError,
    OwnerStatus,
)
from runtime.recovery import ResourceSlotTimeout
from state.locking import workspace_lock


ORCHESTRATION_SCHEMA = "mural.orchestration.v2"
TaskRunner = Callable[["TaskRecord", "OrchestrationManager"], object]


class OrchestrationError(RuntimeError):
    """表示委派、依赖或事件状态不满足机械合同。"""

    def __init__(self, detail: str, *, diagnostics: Mapping[str, object] | None = None) -> None:
        self.diagnostics = diagnostics
        super().__init__(detail)


class OwnerRequestInterrupted(OrchestrationError):
    """求助中的 Owner 已关闭或取消，闭合工具结果后必须终止执行。"""


class TaskStatus(str, Enum):
    """列出异步任务的持久执行状态。"""

    PENDING = "queued"
    WAITING_DEPENDENCY = "waiting_dependency"
    RUNNING = "running"
    WAITING = "blocked"
    COMPLETED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


TERMINAL_TASK_STATUSES = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.SKIPPED}
)


def assert_tasks_terminal(state: Mapping[str, object]) -> None:
    tasks = state.get("tasks") or {}
    unfinished = {key: row.get("status") for key, row in tasks.items()
                  if row.get("status") not in TERMINAL_TASK_STATUSES}
    if unfinished:
        raise OrchestrationError(f"unfinished tasks: {unfinished}; resolve pending requests before completion")


@dataclass(frozen=True)
class TaskRecord:
    """保存一次 Owner attempt 对应的调度任务。"""

    task_id: str
    owner_id: str
    attempt: int
    role: str
    pages: tuple[int, ...]
    group_id: str
    dependencies: tuple[str, ...]
    status: TaskStatus
    goal: str = ""
    repair: bool = False
    result: object = None
    error: str = ""
    request_key: str = ""
    base_deck_version: int = 0
    page_uids: tuple[str, ...] = ()
    revision_id: str = ""
    reference_pages: tuple[int, ...] = ()
    output_path: str = ""
    handoff_from: str = ""
    asset_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """规范枚举与集合字段并复制结果。"""
        object.__setattr__(self, "status", TaskStatus(self.status))
        object.__setattr__(self, "pages", tuple(int(page) for page in self.pages))
        object.__setattr__(self, "page_uids", tuple(self.page_uids))
        object.__setattr__(self, "reference_pages", tuple(self.reference_pages))
        object.__setattr__(self, "asset_ids", tuple(self.asset_ids))
        object.__setattr__(
            self,
            "dependencies",
            tuple(str(item) for item in self.dependencies),
        )
        object.__setattr__(self, "result", copy.deepcopy(self.result))

    @property
    def terminal(self) -> bool:
        """判断任务是否已经得到不可继续推进的结果。"""
        return self.status in TERMINAL_TASK_STATUSES

    def to_dict(self) -> dict[str, object]:
        """把任务记录转换为持久 JSON 对象。"""
        return {
            "task_id": self.task_id,
            "owner_id": self.owner_id,
            "attempt": self.attempt,
            "role": self.role,
            "pages": list(self.pages),
            "group_id": self.group_id,
            "dependencies": list(self.dependencies),
            "status": self.status.value,
            "goal": self.goal,
            "repair": self.repair,
            "result": copy.deepcopy(self.result),
            "error": self.error,
            "request_key": self.request_key,
            "base_deck_version": self.base_deck_version,
            "page_uids": list(self.page_uids),
            "revision_id": self.revision_id,
            "reference_pages": list(self.reference_pages),
            "output_path": self.output_path,
            "handoff_from": self.handoff_from,
            "asset_ids": list(self.asset_ids),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "TaskRecord":
        """从持久 JSON 恢复任务记录。"""
        pages = value.get("pages") or []
        dependencies = value.get("dependencies") or []
        if not isinstance(pages, list) or not isinstance(dependencies, list):
            raise ValueError("task pages and dependencies must be arrays")
        return cls(
            task_id=str(value.get("task_id") or ""),
            owner_id=str(value.get("owner_id") or ""),
            attempt=int(value.get("attempt") or 0),
            role=str(value.get("role") or ""),
            pages=tuple(int(page) for page in pages),
            group_id=str(value.get("group_id") or ""),
            dependencies=tuple(str(item) for item in dependencies),
            status=TaskStatus(str(value.get("status") or "")),
            goal=str(value.get("goal") or ""),
            repair=bool(value.get("repair")),
            result=copy.deepcopy(value.get("result")),
            error=str(value.get("error") or ""),
            request_key=str(value.get("request_key") or ""),
            base_deck_version=int(value.get("base_deck_version") or 0),
            page_uids=tuple(value.get("page_uids") or []),
            revision_id=str(value.get("revision_id") or ""),
            reference_pages=tuple(value.get("reference_pages") or ()),
            output_path=str(value.get("output_path") or ""),
            handoff_from=str(value.get("handoff_from") or ""),
            asset_ids=tuple(value.get("asset_ids") or ()),
        )


def _unfinished_owner_error(detail: str, tasks: Sequence[TaskRecord]) -> OrchestrationError:
    return OrchestrationError(detail, diagnostics={
        "status": "rejected",
        "detail": detail,
        "submission_accepted": False,
        "current_tasks": [{
            "task_id": task.task_id, "role": task.role, "group_id": task.group_id,
            "pages": list(task.pages), "status": task.status.value,
            "continuation_after_task_finishes": {
                "role": task.role, "group_id": task.group_id, "repair": True,
                **({"pages": list(task.pages)} if task.role == "slide" else {}),
                **({"output_path": task.output_path} if task.output_path else {}),
            },
        } for task in tasks],
        "next_action": (
            "The current tasks are unfinished. Answer pending Owner requests or continue independent work. "
            "If there is no independent work, naturally end this turn; the Harness waits for real task events "
            "and resumes this conversation automatically. Do not poll directories or submit another task for the same Owner scope. "
            "After the current task finishes, use its original group_id and a new task_id for repair, "
            "keeping its complete scope. The continuation parameters above are not valid now."
        ),
    })


@dataclass(frozen=True)
class EventRecord:
    """表示一个带持久递增序号的调度事件。"""

    sequence: int
    event_type: str
    task_id: str
    owner_id: str
    actionable: bool
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        """复制事件载荷，避免等待方改变历史。"""
        object.__setattr__(self, "payload", copy.deepcopy(dict(self.payload)))

    def to_dict(self) -> dict[str, object]:
        """把事件转换为工具可返回的 JSON 对象。"""
        return {
            "sequence": self.sequence,
            "type": self.event_type,
            "task_id": self.task_id,
            "owner_id": self.owner_id,
            "actionable": self.actionable,
            **copy.deepcopy(dict(self.payload)),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "EventRecord":
        """从持久对象恢复事件并保留未知载荷字段。"""
        reserved = {
            "sequence",
            "type",
            "task_id",
            "owner_id",
            "actionable",
        }
        return cls(
            sequence=int(value.get("sequence") or 0),
            event_type=str(value.get("type") or ""),
            task_id=str(value.get("task_id") or ""),
            owner_id=str(value.get("owner_id") or ""),
            actionable=bool(value.get("actionable")),
            payload={key: copy.deepcopy(item) for key, item in value.items() if key not in reserved},
        )


class OrchestrationManager:
    """协调 Owner 线程、依赖、求助握手和无轮询事件等待。"""

    def __init__(
        self,
        workspace: str | Path,
        *,
        deck_id: str,
        max_workers: int = 4,
        registry: OwnerRegistry | None = None,
        request_key: str = "",
    ) -> None:
        """加载持久任务并把上次进程遗留的运行任务标为中断。"""
        if int(max_workers) < 1:
            raise ValueError("max_workers must be positive")
        self.workspace = Path(workspace).expanduser().resolve()
        self.state_dir = self.workspace / "_state"
        self.path = self.state_dir / "orchestration.json"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.registry = registry or OwnerRegistry(self.workspace, deck_id=deck_id)
        self.request_key = str(request_key)
        self._condition = threading.Condition(threading.RLock())
        self._max_workers = int(max_workers)
        self._ready_tasks: deque[str] = deque()
        self._threads: dict[str, threading.Thread] = {}
        # 已获执行槽但尚未发布 RUNNING 的线程也占槽；求助等待只保留调用栈。
        self._execution_tasks: set[str] = set()
        self._runners: dict[str, TaskRunner] = {}
        self._futures: dict[str, Future[object]] = {}
        self._closed = False
        self.stop_event = threading.Event()
        self.background_pending = lambda: False
        self._state = self._load_state()
        with self._condition:
            if self._state.get("submission"):
                self._commit_submission_locked()
            if self._recover_interrupted_locked():
                self._persist_locked()

    def register_runner(self, role: str, runner: TaskRunner) -> None:
        """注册角色执行器，并恢复该角色上次中断的原任务。"""
        normalized = str(role).strip().lower()
        if normalized not in OWNER_ROLES:
            raise ValueError(f"unsupported runner role {normalized!r}")
        if not callable(runner):
            raise TypeError("runner must be callable")
        with self._condition:
            self._ensure_open_locked()
            self._runners[normalized] = runner
            changed = self._resume_role_locked(normalized)
            changed = self._schedule_ready_locked() or changed
            if changed:
                self._persist_locked()
                self._condition.notify_all()


    def submit_tasks(self, task_specs: Sequence[Mapping[str, object]]) -> dict[str, object]:
        """向同一个持久任务图追加任务；稳定 ID 重放只返回原结果。"""
        prepare = getattr(self, "prepare_submission", None)
        if prepare is not None:
            additions = self._submit_tasks(task_specs, prepare_only=True)
            if additions:
                # 耗时准备不持调度锁；接受时重新检查归属和依赖，旧 ID 重放不重复准备。
                prepare(additions)
        return self._submit_tasks(task_specs)

    def _submit_tasks(self, task_specs, *, prepare_only: bool = False):
        normalized = []
        for raw in task_specs:
            if not isinstance(raw, Mapping):
                raise OrchestrationError("each task must be an object")
            task_id = raw.get("task_id")
            if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", task_id):
                raise OrchestrationError("task_id must be a stable 1–96 character identifier")
            deps = raw.get("depends_on", [])
            if not isinstance(deps, list) or any(not isinstance(key, str) or not key for key in deps):
                raise OrchestrationError("depends_on must contain task IDs")
            try:
                contract = self.registry.resolve_submission_contract({**raw, "dependencies": deps})
            except OwnerRegistryError as exc:
                raise OrchestrationError(str(exc)) from exc
            if contract["role"] not in self._runners:
                raise OrchestrationError(f"no runner for {contract['role']}")
            if contract["role"] == "slide" and not contract["pages"]:
                raise OrchestrationError("slide tasks require pages")
            if not isinstance(raw.get("goal", ""), str) or not isinstance(raw.get("repair", False), bool):
                raise OrchestrationError("goal must be text and repair must be boolean")
            references = raw.get("reference_pages", [])
            if (not isinstance(references, list) or any(type(page) is not int or page < 1 for page in references)
                    or len(references) != len(set(references))):
                raise OrchestrationError("reference_pages must contain distinct positive page numbers")
            if references and contract["role"] != "slide":
                raise OrchestrationError("reference_pages is only available to Slide Owners")
            for page in references:
                path = self.state_dir.parent / f"slides/slide_{page:02d}.html"
                if not path.is_file() or path.is_symlink() or path.resolve().parent != self.state_dir.parent / "slides":
                    raise OrchestrationError(f"reference page source is missing or outside slides/: {page}")
            asset_ids = raw.get("asset_ids", [])
            if not isinstance(asset_ids, list) or any(not isinstance(item, str) or not item for item in asset_ids):
                raise OrchestrationError("asset_ids must contain explicit image artifact IDs")
            if len(asset_ids) != len(set(asset_ids)):
                raise OrchestrationError("asset_ids must not contain duplicates")
            if (asset_ids and (contract["role"] != "image" or not contract["handoff_from"])) or (
                    contract["role"] == "image" and contract["handoff_from"] and not asset_ids):
                raise OrchestrationError("Image handoff requires both handoff_from and the specific asset_ids")
            normalized.append({**contract, "task_id": task_id,
                               "goal": raw.get("goal", "").strip(), "repair": raw.get("repair", False),
                "reference_pages": tuple(sorted(references)), "asset_ids": tuple(asset_ids)})
        ids = [row["task_id"] for row in normalized]
        if not ids or len(ids) != len(set(ids)):
            raise OrchestrationError("tasks must have distinct non-empty IDs")
        with self._condition:
            self._ensure_open_locked()
            if self._state.get("submission"):
                self._commit_submission_locked()
            tasks = self._task_map_locked()
            additions = []
            for row in normalized:
                prior = tasks.get(row["task_id"])
                if prior is not None:
                    if any(getattr(prior, key) != row[key] for key in
                           ("role", "pages", "group_id", "dependencies", "goal", "repair", "reference_pages", "output_path", "handoff_from", "asset_ids")):
                        raise OrchestrationError(
                            f"task_id {prior.task_id!r} already has a different contract; use a new revision/repair ID",
                            diagnostics={"recovery": self._failure_recovery(prior)}
                            if prior.status == TaskStatus.FAILED else None,
                        )
                else:
                    additions.append(row)
            dependencies = {key: task.dependencies for key, task in tasks.items()}
            dependencies.update({row["task_id"]: row["dependencies"] for row in additions})
            missing = sorted({dep for values in dependencies.values() for dep in values} - set(dependencies))
            if missing:
                raise OrchestrationError(f"unknown dependency task IDs: {missing}")
            visiting, visited = set(), set()

            def visit(key):
                if key in visiting:
                    raise OrchestrationError(f"task dependency cycle at {key!r}")
                if key not in visited:
                    visiting.add(key)
                    for dep in dependencies[key]:
                        visit(dep)
                    visiting.remove(key)
                    visited.add(key)

            for key in ids:
                visit(key)
            validator = getattr(self, "validate_submission", None)
            if validator is not None and not prepare_only:
                validator(additions)
            active = [task for task in tasks.values() if not task.terminal]
            claimed = {page for task in active if task.role == "slide" for page in task.pages}
            groups = {task.group_id for task in active}
            batch_groups = {}
            for row in additions:
                if row["group_id"] in groups:
                    if row["group_id"] in batch_groups:
                        raise OrchestrationError(
                            f"current submission repeats owner group {row['group_id']!r} "
                            f"in tasks {batch_groups[row['group_id']]!r} and {row['task_id']!r}; "
                            "no tasks from this submission were accepted"
                        )
                    raise _unfinished_owner_error(
                        f"owner group {row['group_id']!r} already has an unfinished task",
                        [task for task in active if task.group_id == row["group_id"]],
                    )
                groups.add(row["group_id"])
                batch_groups[row["group_id"]] = row["task_id"]
                if row["role"] == "slide":
                    overlap = claimed.intersection(row["pages"])
                    if overlap:
                        detail = f"overlapping page ownership: {sorted(overlap)}"
                        conflicts = [task for task in active if task.role == "slide"
                                     and set(task.pages).intersection(overlap)]
                        if conflicts:
                            raise _unfinished_owner_error(detail, conflicts)
                        raise OrchestrationError(detail + "; overlap is within this submission; "
                            "give each page one task only; no tasks from this submission were accepted")
                    claimed.update(row["pages"])
            if additions:
                # 主 Agent 的首次写入与委派共用此锁，预检到接受之间不能插入新的页面归属。
                with workspace_lock(self.workspace):
                    try:
                        self.registry.validate_page_ownership(additions)
                    except OwnerRegistryError as exc:
                        raise OrchestrationError(str(exc)) from exc
                    self._validate_output_paths_locked(additions)
                    self._validate_asset_handoffs_locked(additions)
                    if prepare_only:
                        return additions
                    index = self.page_identity_snapshot()
                    pointer_path = self.state_dir / "active-revision.json"
                    pointer = json.loads(pointer_path.read_text()) if pointer_path.is_file() else {}
                    page_uids = {int(row["position"]): row["page_uid"] for row in index.get("pages", [])}
                    for row in additions:
                        row.update(base_deck_version=int(index.get("deck_version") or 0),
                                   page_uids=[page_uids[page] for page in row["pages"] if page in page_uids],
                                   revision_id=str(pointer.get("revision_id") or ""), request_key=self.request_key)
                    # 同一状态文件中的提交意图先落盘，覆盖 Owner Registry 写入后的崩溃窗口。
                    self._state["submission"] = _json_value(additions)
                    self._persist_locked()
                    self._commit_submission_locked()
            if prepare_only:
                return []
            self._schedule_ready_locked()
            self._persist_locked()
            current = self._task_map_locked()
            reused = [key for key in ids if key in tasks]
            failed = [key for key in reused if current[key].terminal and current[key].status != TaskStatus.COMPLETED]
            return {"tasks": [current[key].to_dict() for key in ids],
                    "new_task_ids": [row["task_id"] for row in additions],
                    "reused_task_ids": reused,
                    "reused_failed_task_ids": failed,
                    "recovery": [self._failure_recovery(current[key]) for key in failed],
                    "replay_notice": (
                        "本次没有重新执行这些旧失败任务。补齐条件后使用新的 task_id 提交新尝试，页面仍交回原 Owner。"
                        if failed else "同 task_id 仅返回已有记录，不创建新执行。" if reused else "")}

    def _commit_submission_locked(self) -> None:
        with workspace_lock(self.workspace):
            self._commit_submission_artifacts_locked()

    def _commit_submission_artifacts_locked(self) -> None:
        additions = self._state["submission"]
        try:
            owners = self.registry.accept_many(additions)
        except OwnerRegistryError as exc:
            # 注册表原子拒绝时没有任何 Owner 被接受，不留下可重放的失败提交。
            self._state.pop("submission")
            self._persist_locked()
            raise OrchestrationError(str(exc)) from exc
        tasks = self._task_map_locked()
        for row, owner in zip(additions, owners):
            task = TaskRecord(
                task_id=row["task_id"], owner_id=owner.owner_id, attempt=owner.attempt,
                role=owner.role, pages=owner.pages, group_id=owner.group_id,
                dependencies=owner.dependencies,
                status=TaskStatus.WAITING_DEPENDENCY if owner.dependencies else TaskStatus.PENDING,
                goal=row["goal"], repair=row["repair"], request_key=row["request_key"],
                base_deck_version=row["base_deck_version"], page_uids=tuple(row["page_uids"]),
                revision_id=row["revision_id"],
                reference_pages=tuple(row["reference_pages"]),
                output_path=owner.output_path, handoff_from=row["handoff_from"],
                asset_ids=tuple(row["asset_ids"]),
            )
            if task.role == "image" and task.handoff_from:
                from state.artifacts import ArtifactStore
                ArtifactStore(self.workspace).transfer_image_owners(owner_id=owner.owner_id,
                    previous_owner_ids=[task.handoff_from], artifact_ids=task.asset_ids)
            elif task.role == "slide" and task.handoff_from:
                from state.artifacts import ArtifactStore
                store = ArtifactStore(self.workspace)
                for page in task.pages:
                    store.transfer_page_owner(page, owner_id=owner.owner_id)
                    store.workspace.invalidate_previous_author_review(page, owner.owner_id)
            tasks[task.task_id] = task
            self._emit_locked("task_submitted", task, actionable=False,
                              payload={"dependencies": list(task.dependencies)})
        self._set_task_map_locked(tasks)
        self._state.pop("submission")
        self._persist_locked()
        self._schedule_ready_locked()
        self._persist_locked()
        self._condition.notify_all()

    def wait(self, *, consumer_id: str = "orchestrator", after_sequence: int | None = None,
             timeout_s: float | None = None) -> dict[str, object] | None:
        """普通 Slide 完成合并交回，决策事件立即交回；不改变执行依赖。"""
        deadline = None if timeout_s is None else time.monotonic() + max(0, timeout_s)
        with self._condition:
            cursor = int(after_sequence if after_sequence is not None else
                         self._state.get("cursors", {}).get(consumer_id, 0))
            while True:
                tasks = self._task_map_locked()
                self._raise_for_interruption_locked(tasks, tuple(tasks))
                unseen = [event for event in self._event_list_locked() if event.sequence > cursor]
                coverage = self.page_coverage()
                events = [event for event in unseen if event.actionable or event.event_type == "task_completed"]
                requests = [row for row in self._request_map_locked().values() if row["status"] == "pending"]
                terminal = all(task.terminal for task in tasks.values()) and not self.background_pending()
                if any(event.actionable for event in events) or requests or terminal:
                    # 覆盖已由提交回执报告；缺页不把普通提交/完成升级为唤醒。
                    # 仅实际交回事件时推进游标，不能因提交过任务而吞掉并发到达的决策或求助。
                    cursor = max([cursor, *(event.sequence for event in unseen)])
                    self._state.setdefault("cursors", {})[consumer_id] = cursor
                    self._persist_locked()
                    changed = {event.task_id for event in events} | {row["task_id"] for row in requests}
                    latest = {}
                    for task in tasks.values():
                        if task.owner_id not in latest or task.attempt > latest[task.owner_id].attempt:
                            latest[task.owner_id] = task
                    handed_off = self._resolved_handoff_owners_locked(tasks)
                    failures = [{**{key: task.to_dict()[key] for key in
                                 ("task_id", "owner_id", "attempt", "role", "group_id", "pages", "error")},
                                 "recovery": self._failure_recovery(task)}
                                for task in latest.values() if task.status == TaskStatus.FAILED and task.owner_id not in handed_off]
                    return {"status": "idle" if terminal else "running", "all_terminal": terminal,
                            "cursor": cursor, "events": [event.to_dict() for event in events],
                            "tasks": [task.to_dict() for key, task in tasks.items() if key in changed],
                            "pending_requests": copy.deepcopy(requests), "unresolved_failures": failures, **coverage}
                if self._closed:
                    raise OrchestrationError("orchestration manager is closed")
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return None
                self._condition.wait(remaining)

    @staticmethod
    def _failure_recovery(task: TaskRecord) -> dict[str, object]:
        # Parameters deliberately omit task_id: replaying a failed ID never runs it.
        continuation = {"role": task.role, "group_id": task.group_id,
                        "depends_on": []}
        handoff = {"role": task.role, "handoff_from": task.owner_id,
                   "depends_on": []}
        if task.role == "slide":
            continuation.update(pages=list(task.pages), repair=True)
            handoff.update(pages=list(task.pages), repair=True)
        elif task.role == "research":
            continuation["output_path"] = task.output_path
            handoff["output_path"] = task.output_path
        recovery = {
            "failed_task_id": task.task_id, "failed_owner_id": task.owner_id,
            "action": "delegate_submit", "continuation_params": continuation,
            "handoff_params": handoff,
            "instruction": (
                "Use a fresh task_id and a goal addressing the failure. Continue with "
                "continuation_params, or hand off with handoff_params and a new group_id. "
                "Do not depend on the failed task. Only successful structurally linked "
                "recovery resolves this failure; prose or similar task names do not."
            ),
        }
        if task.role == "image":
            recovery["instruction"] += (
                " Image handoff also requires explicit asset_ids from the artifact catalog; "
                "all unfinished predecessor assets must be resolved."
            )
        if task.role == "research":
            recovery["research_task_link_params"] = {
                "role": "research", "group_id": task.task_id, "depends_on": [],
            }
            recovery["instruction"] += (
                " Alternatively use research_task_link_params with a fresh task_id "
                "to explicitly link a separate Research continuation by exact predecessor task_id."
            )
        return recovery

    def _resolved_handoff_owners_locked(
        self, tasks: Mapping[str, TaskRecord]
    ) -> set[str]:
        """Only successful, structurally linked handoff chains supersede failures."""
        completed = [
            task for task in tasks.values()
            if task.status == TaskStatus.COMPLETED and task.handoff_from
        ]
        resolved = {
            task.handoff_from for task in completed
            if task.role in {"research", "slide"}
        }
        # A Research continuation may be submitted before the failed owner's
        # identity is known. In that narrow contract its group_id is exactly the
        # predecessor task_id; generic shared group names never imply recovery.
        failed_research = {
            task.task_id: task
            for task in tasks.values()
            if task.role == "research" and task.status == TaskStatus.FAILED
        }
        resolved.update(
            failed_research[task.group_id].owner_id
            for task in tasks.values()
            if (
                task.role == "research"
                and task.status == TaskStatus.COMPLETED
                and task.group_id in failed_research
                and task.task_id != task.group_id
            )
        )
        image_predecessors = {
            task.handoff_from for task in completed if task.role == "image"
        }
        unfinished_owners = set()
        if image_predecessors:
            from state.artifacts import ArtifactStore

            images = ArtifactStore(self.workspace, create=False).catalog()["images"]
            committed_stems = {
                str(Path(str(row.get("path") or "")).with_suffix(""))
                for row in images
                if row.get("status") == "committed"
                and row.get("path")
                and row.get("sha256")
            }
            for row in images:
                if row.get("status") not in {"declared", "broken"}:
                    continue
                path = str(row.get("path") or "")
                if path and str(Path(path).with_suffix("")) in committed_stems:
                    continue
                unfinished_owners.add(str(row.get("owner_id") or ""))
            resolved.update(image_predecessors - unfinished_owners)
        # A successful replacement also supersedes explicitly linked ancestors.
        # Keep Image's unfinished-asset gate on each link, not just the last one.
        predecessors_by_owner: dict[str, set[str]] = {}
        for task in tasks.values():
            predecessors = set()
            if task.handoff_from and task.role in {"research", "slide", "image"}:
                if task.role != "image" or task.handoff_from not in unfinished_owners:
                    predecessors.add(task.handoff_from)
            if (
                task.role == "research"
                and task.group_id in failed_research
                and task.task_id != task.group_id
            ):
                predecessors.add(failed_research[task.group_id].owner_id)
            if predecessors:
                predecessors_by_owner.setdefault(task.owner_id, set()).update(predecessors)
        pending = deque(resolved)
        while pending:
            for predecessor in predecessors_by_owner.get(pending.popleft(), ()):
                if predecessor not in resolved:
                    resolved.add(predecessor)
                    pending.append(predecessor)
        return resolved

    def publish_background_event(self, event_type: str, payload: Mapping[str, object],
                                 *, on_published: Callable[[], None], dedup_key: str = "") -> None:
        """后台机械工作复用既有事件游标，不建立虚构 Owner 或独立任务图。"""
        with self._condition:
            repeated = dedup_key and any(
                event.event_type == event_type and event.payload.get("dedup_key") == dedup_key
                for event in self._event_list_locked())
            if not self._closed and not repeated:
                self._emit_locked(event_type, None, actionable=True,
                                  payload={**payload, **({"dedup_key": dedup_key} if dedup_key else {})})
                self._persist_locked()
            # 完成标记与可恢复通知同处等待锁内，不能先观察到 idle 再丢失完成事件。
            on_published()
            self._condition.notify_all()

    def request_owner_assistance(
        self,
        task_id: str,
        *,
        request_type: str,
        detail: str,
        pages: Sequence[int] = (),
        evidence: str = "",
        timeout_s: float | None = None,
    ) -> dict[str, object]:
        """暂停当前 Owner，通知 Orchestrator，并在同一调用中等待答复。"""
        normalized_task = str(task_id).strip()
        if not str(request_type).strip() or not str(detail).strip():
            raise OrchestrationError("owner request type and detail must not be empty")
        deadline = None if timeout_s is None else time.monotonic() + max(0.0, timeout_s)
        with self._condition:
            tasks = self._task_map_locked()
            task = tasks.get(normalized_task)
            if task is None:
                raise OrchestrationError(f"unknown task_id {normalized_task!r}")
            if task.status != TaskStatus.RUNNING:
                raise OrchestrationError(
                    f"task {normalized_task!r} cannot request help from {task.status.value!r}"
                )
            request_id = f"request-{uuid.uuid4().hex}"
            waiting = _replace_task(task, status=TaskStatus.WAITING)
            tasks[task.task_id] = waiting
            self._set_task_map_locked(tasks)
            self._transition_owner_locked(waiting, OwnerStatus.WAITING)
            requests = self._request_map_locked()
            requests[request_id] = {
                "request_id": request_id,
                "task_id": task.task_id,
                "owner_id": task.owner_id,
                "status": "pending",
                "request_type": str(request_type).strip(),
                "detail": str(detail).strip(),
                "pages": list(dict.fromkeys(int(page) for page in pages)),
                "evidence": str(evidence).strip(),
                "response": None,
            }
            self._state["requests"] = requests
            self._emit_locked(
                "owner_request",
                waiting,
                actionable=True,
                payload={
                    "request_id": request_id,
                    "request_type": str(request_type).strip(),
                    "detail": str(detail).strip(),
                    "pages": list(dict.fromkeys(int(page) for page in pages)),
                    "evidence": str(evidence).strip(),
                },
            )
            self._execution_tasks.remove(task.task_id)
            self._schedule_ready_locked()
            self._persist_locked()
            self._condition.notify_all()
            while True:
                request = self._request_map_locked()[request_id]
                current = self._task_locked(task.task_id)
                if self._closed or current.terminal:
                    request["status"] = "cancelled" if current.terminal else "interrupted"
                    requests = self._request_map_locked()
                    requests[request_id] = request
                    self._state["requests"] = requests
                    if not current.terminal:
                        interrupted = _replace_task(current, status=TaskStatus.INTERRUPTED,
                                                    error="manager closed during owner request")
                        current_tasks = self._task_map_locked()
                        current_tasks[task.task_id] = interrupted
                        self._set_task_map_locked(current_tasks)
                        self._transition_owner_locked(interrupted, OwnerStatus.INTERRUPTED)
                        self._emit_locked("task_interrupted", interrupted, actionable=True,
                                          payload={"reason": "manager_closed", "recoverable": True})
                    self._persist_locked()
                    self._condition.notify_all()
                    raise OwnerRequestInterrupted("owner request interrupted by manager close or task cancellation")
                if request.get("status") in {"responded", "timed_out"} and task.task_id in self._execution_tasks:
                    running = _replace_task(
                        current, status=TaskStatus.RUNNING
                    )
                    current_tasks = self._task_map_locked()
                    current_tasks[task.task_id] = running
                    self._set_task_map_locked(current_tasks)
                    self._transition_owner_locked(running, OwnerStatus.RUNNING)
                    self._persist_locked()
                    self._condition.notify_all()
                    if request["status"] == "timed_out":
                        return {"status": "timed_out", "request_id": request_id}
                    response = request.get("response")
                    return copy.deepcopy(response) if isinstance(response, dict) else {
                        "status": "resolved",
                        "value": copy.deepcopy(response),
                    }
                if request["status"] != "pending" or deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    self._condition.wait(remaining)
                    continue
                request["status"] = "timed_out"
                requests = self._request_map_locked()
                requests[request_id] = request
                self._state["requests"] = requests
                self._emit_locked(
                    "owner_request_timeout",
                    current,
                    actionable=True,
                    payload={"request_id": request_id},
                )
                self._ready_tasks.append(task.task_id)
                self._dispatch_ready_locked()
                self._persist_locked()
                self._condition.notify_all()

    def respond_owner_request(
        self,
        request_id: str,
        response: Mapping[str, object],
    ) -> dict[str, object]:
        """按全局唯一 request_id 答复求助，并唤醒原 Owner 工具调用。"""
        normalized = str(request_id).strip()
        safe_response = _json_value(dict(response))
        with self._condition:
            self._ensure_open_locked()
            requests = self._request_map_locked()
            request = requests.get(normalized)
            if request is None:
                raise OrchestrationError(f"unknown request_id {normalized!r}")
            if request.get("status") != "pending":
                raise OrchestrationError(
                    f"owner request {normalized!r} is already {request.get('status')!r}"
                )
            if self._task_locked(str(request["task_id"])).status != TaskStatus.WAITING:
                raise OrchestrationError(f"owner request {normalized!r} no longer has a waiting task")
            request["status"] = "responded"
            request["response"] = safe_response
            requests[normalized] = request
            self._state["requests"] = requests
            self._ready_tasks.append(str(request["task_id"]))
            self._dispatch_ready_locked()
            self._persist_locked()
            self._condition.notify_all()
            return {
                "request_id": normalized,
                "owner_resumed": True,
                "task_id": str(request.get("task_id") or ""),
            }

    def task(self, task_id: str) -> TaskRecord:
        """读取一个任务的不可变快照。"""
        with self._condition:
            return self._task_locked(str(task_id).strip())

    def page_identity_snapshot(self) -> dict[str, object]:
        """读取已有页身份；未编译页表时只取已登记页，不创建索引或计算资源哈希。"""
        index_path = self.state_dir / "page-index.json"
        if index_path.is_file():
            return {**json.loads(index_path.read_text()), "coverage_source": "plan_index"}
        workspace_path = self.state_dir / "workspace.json"
        if not workspace_path.is_file():
            return {}
        state = json.loads(workspace_path.read_text())
        return {
            "deck_version": int(state.get("deck_version") or 0),
            "coverage_source": "registered_pages",
            "pages": [{"position": int(row["page"]), "page_uid": row["page_uid"]}
                      for row in state.get("pages", {}).values() if not row.get("retired")],
        }

    def snapshot(self) -> dict[str, object]:
        """返回完整调度状态的深复制快照。"""
        with self._condition:
            return copy.deepcopy(self._state)

    def page_coverage(self) -> dict[str, object]:
        """以当前归属、页身份和任务事实区分自制、委派与尚未分配页。"""
        with self._condition, workspace_lock(self.workspace):
            index = self.page_identity_snapshot()
            pages = index.get("pages", [])
            config_path = self.workspace / "config.json"
            target = json.loads(config_path.read_text()).get("target_page_count") if config_path.is_file() else None
            if target is not None and (type(target) is not int or target < 1):
                raise OrchestrationError("config.json target_page_count must be a positive integer or null")
            if not pages and target is None:
                return {}
            owners = self.registry.list_current()
            direct = {page for owner in owners if owner.role == "orchestrator" for page in owner.pages}
            delegated_owners = {owner.owner_id for owner in owners if owner.role == "slide"}
            tasks = [task for task in self._task_map_locked().values()
                     if task.role == "slide" and task.owner_id in delegated_owners]
            delegated = {uid for task in tasks for uid in task.page_uids}
            # 初始登记前的任务仍可按页号报告；编译页身份后不把旧位置当成新页。
            positional = {page for task in tasks if not task.page_uids for page in task.pages}
            if index.get("coverage_source", "registered_pages") != "registered_pages":
                positional = set()
            assigned = {int(page["position"]) for page in pages
                        if page["page_uid"] in delegated or int(page["position"]) in positional}
            self_authored = {int(page["position"]) for page in pages if int(page["position"]) in direct}
            unassigned = sorted(({int(page["position"]) for page in pages}
                                 | set(range(1, (target or 0) + 1))) - assigned - self_authored)
            return {
                "planned_page_count": target if target is not None else len(pages),
                **({"target_page_count": target} if config_path.is_file() else {}),
                "page_coverage_source": index.get("coverage_source", "registered_pages"),
                "self_authored_pages": sorted(self_authored),
                "delegated_pages": sorted(assigned),
                "unassigned_pages": unassigned,
                "undelegated_pages": unassigned,
            }

    def close(self, *, wait: bool = True, timeout_s: float = 60.0) -> None:
        """请求在闭合工具边界暂停；超时保留真实运行状态，不硬杀或伪造完成。"""
        deadline = time.monotonic() + max(0.0, timeout_s)
        with self._condition:
            if not self._closed:
                self._closed = True
                self.stop_event.set()
                tasks = self._task_map_locked()
                if any(not task.terminal for task in tasks.values()):
                    self._emit_locked("shutdown_requested", None, actionable=False,
                                      payload={"reason": "manager_closed", "recoverable": True})
                for task_id, future in self._futures.items():
                    future.cancel()
                    task = tasks[task_id]
                    if task.status == TaskStatus.PENDING:
                        self._emit_locked("task_deferred", task, actionable=True,
                                          payload={"reason": "manager_closed", "recoverable": True})
                self._persist_locked()
            self._ready_tasks.clear()
            self._condition.notify_all()
            threads = tuple(self._threads.values())
        if wait:
            for thread in threads:
                thread.join(max(0.0, deadline - time.monotonic()))
            pending = [key for key, thread in self._threads.items() if thread.is_alive()]
            if pending:
                with self._condition:
                    self._emit_locked("shutdown_pending", None, actionable=True,
                                      payload={"task_ids": pending, "recoverable": True,
                                               "reason": "in_flight_tool_boundary", "timeout_s": timeout_s})
                    self._persist_locked()
                raise OrchestrationError(f"owner_shutdown_pending: {pending}; preserving in-flight tools and checkpoints")


    def _validate_output_paths_locked(self, additions) -> None:
        for row in additions:
            if not row["output_path"]:
                continue
            target = self.workspace / row["output_path"]
            if not target.resolve().is_relative_to(self.workspace) or any(
                    path.is_symlink() for path in (target, *target.parents) if path != self.workspace and self.workspace in path.parents):
                raise OrchestrationError("research_output_path_invalid: symlinks or paths outside the workspace are not allowed")
            if target.exists() and not target.is_file():
                raise OrchestrationError("research_output_path_invalid: output must be a Markdown file")
            if any(path.is_file() for path in target.parents if self.workspace in path.parents):
                raise OrchestrationError("research_output_path_invalid: parent path is a file")
            known = {owner.output_path for owner in self.registry.list_current() if owner.role == "research"}
            if target.exists() and row["output_path"] not in known:
                raise OrchestrationError("research_output_conflict: existing unassigned file cannot be overwritten by a new task")

    def _validate_asset_handoffs_locked(self, additions) -> None:
        from state.artifacts import ArtifactStateError, ArtifactStore
        claimed = set()
        for row in additions:
            if not row["handoff_from"]:
                continue
            if any(not task.terminal and task.owner_id == row["handoff_from"] for task in self._task_map_locked().values()):
                raise OrchestrationError("handoff requires all previous Owner tasks to finish")
            if row["role"] == "slide":
                try:
                    for page in row["pages"]:
                        ArtifactStore(self.workspace).transfer_page_owner(page, owner_id=f"pending:{row['task_id']}", validate_only=True)
                except ArtifactStateError as exc:
                    raise OrchestrationError(str(exc)) from exc
                continue
            if row["role"] != "image":
                continue
            if claimed.intersection(row["asset_ids"]):
                raise OrchestrationError("image handoff contains overlapping asset_ids")
            claimed.update(row["asset_ids"])
            try:
                ArtifactStore(self.workspace).transfer_image_owners(owner_id=f"pending:{row['task_id']}",
                    previous_owner_ids=[row["handoff_from"]], artifact_ids=row["asset_ids"], validate_only=True)
            except ArtifactStateError as exc:
                raise OrchestrationError(str(exc)) from exc

    def _run_task(self, task_id: str) -> object:
        """在线程真正执行时发布启动，再记录结果或可恢复中断。"""
        with self._condition:
            tasks = self._task_map_locked()
            task = tasks[task_id]
            if self._closed or task.status != TaskStatus.PENDING:
                return None
            task = _replace_task(task, status=TaskStatus.RUNNING, error="")
            tasks[task_id] = task
            self._set_task_map_locked(tasks)
            self._transition_owner_locked(task, OwnerStatus.RUNNING)
            if task.dependencies:
                self._emit_locked(
                    "dependency_unblocked", task, actionable=task.role != "slide",
                    payload={"dependencies": list(task.dependencies)},
                )
            self._emit_locked("task_started", task, actionable=False, payload={})
            self._persist_locked()
            self._condition.notify_all()
            runner = self._runners[task.role]
        try:
            result = _json_value(runner(task, self))
        except Exception as exc:
            self._finish_failed(task_id, exc)
            return None
        self._finish_completed(task_id, result)
        return result

    def _finish_completed(self, task_id: str, result: object) -> None:
        """原子记录成功结果并启动新解除依赖的任务。"""
        with self._condition:
            tasks = self._task_map_locked()
            task = tasks[task_id]
            if task.status not in {TaskStatus.RUNNING, TaskStatus.WAITING}:
                return
            completed = _replace_task(
                task, status=TaskStatus.COMPLETED, result=result, error=""
            )
            tasks[task_id] = completed
            self._set_task_map_locked(tasks)
            self._transition_owner_locked(completed, OwnerStatus.COMPLETED)
            self._emit_locked(
                "task_completed", completed,
                actionable=(completed.role != "slide" or (isinstance(result, Mapping)
                            and (result.get("status") in {"repair_required", "repair_handoff"}
                                 or bool(result.get("group_review_gap"))))),
                payload={},
            )
            self._schedule_ready_locked()
            self._persist_locked()
            self._condition.notify_all()

    def _finish_failed(self, task_id: str, exc: Exception) -> None:
        """区分资源中断与真实执行失败，并通知等待方。"""
        with self._condition:
            tasks = self._task_map_locked()
            task = tasks[task_id]
            if task.terminal or task.status == TaskStatus.INTERRUPTED:
                return
            detail = f"{type(exc).__name__}: {str(exc)[:1000]}"
            compact_failed = (
                isinstance(exc, InterruptedError)
                and str(exc).startswith(
                    "owner_interrupted: context_compaction_recoverable; checkpoint retained;"
                )
            )
            if compact_failed:
                # Compaction only ended this Owner attempt.  Treat it as a
                # recoverable terminal failure so the Orchestrator can receive
                # the event and submit a fresh, narrower attempt in-process.
                failed = _replace_task(task, status=TaskStatus.FAILED, error=detail)
                tasks[task_id] = failed
                self._set_task_map_locked(tasks)
                self._transition_owner_locked(failed, OwnerStatus.FAILED)
                self._emit_locked(
                    "task_failed", failed, actionable=True,
                    payload={"error": detail, "reason": "context_compaction_recoverable",
                             "recoverable": True},
                )
            elif isinstance(exc, (ResourceSlotTimeout, InterruptedError)):
                interrupted = _replace_task(task, status=TaskStatus.INTERRUPTED, error=detail)
                tasks[task_id] = interrupted
                self._set_task_map_locked(tasks)
                self._transition_owner_locked(interrupted, OwnerStatus.INTERRUPTED)
                self._emit_locked(
                    "task_interrupted", interrupted, actionable=True,
                    payload={"reason": "resource_queue_timeout" if isinstance(exc, ResourceSlotTimeout) else "owner_interrupted",
                             "error": detail, "recoverable": True},
                )
                for dependent in tasks.values():
                    if dependent.status == TaskStatus.WAITING_DEPENDENCY and task_id in self._dependency_closure(tasks, (dependent.task_id,)):
                        self._emit_locked(
                            "dependency_interrupted", dependent, actionable=True,
                            payload={"interrupted_dependencies": [task_id], "recoverable": True},
                        )
            else:
                failed = _replace_task(task, status=TaskStatus.FAILED, error=detail)
                tasks[task_id] = failed
                self._set_task_map_locked(tasks)
                self._transition_owner_locked(failed, OwnerStatus.FAILED)
                self._emit_locked(
                    "task_failed", failed, actionable=True, payload={"error": detail}
                )
            self._schedule_ready_locked()
            self._persist_locked()
            self._condition.notify_all()

    @staticmethod
    def _dependency_closure(tasks: Mapping[str, TaskRecord], task_ids: Sequence[str]) -> set[str]:
        """覆盖间接依赖，避免等待下游时遗漏上游队列中断。"""
        pending = list(task_ids)
        found: set[str] = set()
        while pending:
            task_id = pending.pop()
            if task_id not in found:
                found.add(task_id)
                pending.extend(tasks[task_id].dependencies)
        return found

    def _raise_for_interruption_locked(self, tasks: Mapping[str, TaskRecord], task_ids: Sequence[str]) -> None:
        interrupted = [
            task_id for task_id in self._dependency_closure(tasks, task_ids)
            if tasks[task_id].status == TaskStatus.INTERRUPTED and task_id in self._futures
        ]
        if interrupted:
            reasons = {event.task_id: event.payload.get("reason") for event in self._event_list_locked()
                       if event.event_type == "task_interrupted"}
            if any(reasons.get(task_id) == "owner_interrupted" for task_id in interrupted):
                details = {task_id: tasks[task_id].error for task_id in sorted(interrupted)}
                raise InterruptedError(f"owner_interrupted: resume the same workspace after resolving the cause: {details}")
            raise ResourceSlotTimeout(
                "resource_queue_timeout: Owner tasks were interrupted while waiting for resources; "
                f"resume the same workspace after capacity is available: {sorted(interrupted)}"
            )

    def _schedule_ready_locked(self) -> bool:
        """提交依赖已就绪的任务；未获执行槽者保持 pending。"""
        if self._closed:
            return False
        changed = False
        while True:
            progress = False
            tasks = self._task_map_locked()
            for task_id in sorted(tasks):
                task = tasks[task_id]
                if task.status not in {TaskStatus.PENDING, TaskStatus.WAITING_DEPENDENCY} or task_id in self._futures:
                    continue
                dependencies = [tasks[item] for item in task.dependencies]
                failed_dependencies = [
                    item.task_id
                    for item in dependencies
                    if item.status in {TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.SKIPPED}
                ]
                if failed_dependencies:
                    failed = _replace_task(
                        task,
                        status=TaskStatus.SKIPPED,
                        error=f"dependency failed: {failed_dependencies}",
                    )
                    tasks[task_id] = failed
                    self._set_task_map_locked(tasks)
                    self._transition_owner_locked(failed, OwnerStatus.FAILED)
                    self._emit_locked(
                        "task_skipped",
                        failed,
                        actionable=True,
                        payload={"failed_dependencies": failed_dependencies},
                    )
                    progress = changed = True
                    continue
                if not all(item.status == TaskStatus.COMPLETED for item in dependencies):
                    continue
                if task.role not in self._runners:
                    continue
                tasks[task_id] = _replace_task(task, status=TaskStatus.PENDING)
                self._set_task_map_locked(tasks)
                self._futures[task_id] = Future()
                self._ready_tasks.append(task_id)
                progress = changed = True
            if not progress:
                self._dispatch_ready_locked()
                return changed

    def _dispatch_ready_locked(self) -> None:
        """新任务与原调用恢复共用 FIFO；仅获得执行槽的新任务创建线程。"""
        while not self._closed and self._ready_tasks and len(self._execution_tasks) < self._max_workers:
            task_id = self._ready_tasks.popleft()
            task = self._task_locked(task_id)
            future = self._futures[task_id]
            if task.terminal:
                if not future.running() and not future.done():
                    future.set_result(None)
                continue
            if task.status == TaskStatus.PENDING:
                if not future.set_running_or_notify_cancel():
                    continue
                self._execution_tasks.add(task_id)
                thread = threading.Thread(target=self._run_task_thread, args=(task_id,), name=f"mural-owner-{task_id}")
                self._threads[task_id] = thread
                try:
                    thread.start()
                except RuntimeError as exc:
                    self._threads.pop(task_id)
                    self._execution_tasks.remove(task_id)
                    detail = f"owner_thread_start_failed: {type(exc).__name__}: {str(exc)[:1000]}"
                    interrupted = _replace_task(task, status=TaskStatus.INTERRUPTED, error=detail)
                    tasks = self._task_map_locked()
                    tasks[task_id] = interrupted
                    self._set_task_map_locked(tasks)
                    self._transition_owner_locked(interrupted, OwnerStatus.INTERRUPTED)
                    self._emit_locked("task_interrupted", interrupted, actionable=True,
                                      payload={"reason": "owner_thread_start_failed", "error": detail,
                                               "recoverable": True})
                    self._persist_locked()
                    future.set_result(None)
                    self._condition.notify_all()
                    return
            elif task.status == TaskStatus.WAITING:
                self._execution_tasks.add(task_id)
                self._condition.notify_all()

    def _run_task_thread(self, task_id: str) -> None:
        """保持同步 Owner 调用栈，并在退出时归还唯一执行槽。"""
        future = self._futures[task_id]
        try:
            result = self._run_task(task_id)
        except BaseException as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)
        finally:
            with self._condition:
                self._execution_tasks.discard(task_id)
                self._dispatch_ready_locked()
                self._condition.notify_all()


    def _emit_locked(
        self,
        event_type: str,
        task: TaskRecord | None,
        *,
        actionable: bool,
        payload: Mapping[str, object],
    ) -> EventRecord:
        """追加一条序号严格递增的持久事件。"""
        sequence = int(self._state.get("sequence") or 0) + 1
        event = EventRecord(
            sequence=sequence,
            event_type=str(event_type),
            task_id=task.task_id if task else "",
            owner_id=task.owner_id if task else "",
            actionable=bool(actionable),
            payload=_json_value({**dict(payload), **(
                {"recovery": self._failure_recovery(task)}
                if event_type == "task_failed" and task is not None else {}
            )}),
        )
        events = [item.to_dict() for item in self._event_list_locked()]
        events.append(event.to_dict())
        self._state["events"] = events
        self._state["sequence"] = sequence
        return event

    def _transition_owner_locked(
        self, task: TaskRecord, status: OwnerStatus
    ) -> bool:
        """同步 Owner 状态；过期 attempt 会留下显式冲突事件。"""
        try:
            self.registry.transition(
                task.owner_id, attempt=task.attempt, status=status
            )
            return True
        except OwnerRegistryError as exc:
            self._emit_locked(
                "owner_registry_conflict",
                task,
                actionable=True,
                payload={"error": str(exc), "target_status": status.value},
            )
            return False

    def _recover_interrupted_locked(self) -> bool:
        """把进程重启时遗留的运行/等待任务标为可恢复中断。"""
        tasks = self._task_map_locked()
        changed = False
        for task_id, task in list(tasks.items()):
            if task.status not in {TaskStatus.RUNNING, TaskStatus.WAITING}:
                continue
            interrupted = _replace_task(task, status=TaskStatus.INTERRUPTED)
            tasks[task_id] = interrupted
            self._transition_owner_locked(interrupted, OwnerStatus.INTERRUPTED)
            self._emit_locked(
                "task_interrupted",
                interrupted,
                actionable=True,
                payload={"reason": "manager_restart"},
            )
            changed = True
        if changed:
            requests = self._request_map_locked()
            for request in requests.values():
                if request.get("status") == "pending":
                    request["status"] = "interrupted"
            self._state["requests"] = requests
            self._set_task_map_locked(tasks)
        return changed

    def _resume_role_locked(self, role: str) -> bool:
        """把指定角色的中断任务转回 pending，保留原 task_id。"""
        tasks = self._task_map_locked()
        changed = False
        for task_id, task in list(tasks.items()):
            if task.role != role or task.status != TaskStatus.INTERRUPTED or task_id in self._futures:
                continue
            pending = _replace_task(task, status=TaskStatus.PENDING)
            tasks[task_id] = pending
            self._transition_owner_locked(pending, OwnerStatus.PENDING)
            self._emit_locked(
                "task_resumed",
                pending,
                actionable=False,
                payload={"same_task_id": True},
            )
            changed = True
        if changed:
            self._set_task_map_locked(tasks)
        return changed


    def _task_locked(self, task_id: str) -> TaskRecord:
        """等待者只解码自身状态，避免一次广播唤醒反复扫描全册任务。"""
        raw = self._state.get("tasks") or {}
        if not isinstance(raw, dict):
            raise OrchestrationError("orchestration tasks must be an object")
        value = raw[task_id]
        if not isinstance(value, dict):
            raise OrchestrationError("orchestration task entries must be objects")
        task = TaskRecord.from_dict(value)
        if task.task_id != str(task_id):
            raise OrchestrationError("task key does not match task_id")
        return task

    def _task_map_locked(self) -> dict[str, TaskRecord]:
        """解码完整任务映射；单任务读取不经过此遍历。"""
        raw = self._state.get("tasks") or {}
        if not isinstance(raw, dict):
            raise OrchestrationError("orchestration tasks must be an object")
        return {str(task_id): self._task_locked(task_id) for task_id in raw}

    def _set_task_map_locked(self, tasks: Mapping[str, TaskRecord]) -> None:
        """编码并替换持久任务映射。"""
        self._state["tasks"] = {
            key: value.to_dict() for key, value in sorted(tasks.items())
        }


    def _request_map_locked(self) -> dict[str, dict[str, object]]:
        """校验并返回可更新的 Owner 求助映射。"""
        raw = self._state.get("requests") or {}
        if not isinstance(raw, dict) or not all(isinstance(item, dict) for item in raw.values()):
            raise OrchestrationError("orchestration requests must be an object of objects")
        return raw


    def _event_list_locked(self) -> list[EventRecord]:
        """解码并校验事件序号严格递增。"""
        raw = self._state.get("events") or []
        if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
            raise OrchestrationError("orchestration events must be an array of objects")
        events = [EventRecord.from_dict(item) for item in raw]
        sequences = [event.sequence for event in events]
        if sequences != sorted(set(sequences)):
            raise OrchestrationError("orchestration event sequences are invalid")
        return events

    def _load_state(self) -> dict[str, object]:
        """读取调度状态，缺失时创建空状态。"""
        if not self.path.is_file():
            return {
                "schema": ORCHESTRATION_SCHEMA,
                "sequence": 0,
                "tasks": {},
                "cursors": {},
                "requests": {},
                "events": [],
            }
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema") != ORCHESTRATION_SCHEMA:
            raise OrchestrationError(
                f"unsupported orchestration state schema in {self.path}"
            )
        return value

    def _persist_locked(self) -> None:
        """在同目录完整落盘后原子替换调度状态。"""
        payload = json.dumps(
            self._state, ensure_ascii=False, sort_keys=True, indent=2
        ) + "\n"
        pending = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.pending")
        try:
            with pending.open("x", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(pending, self.path)
        finally:
            pending.unlink(missing_ok=True)

    def _ensure_open_locked(self) -> None:
        """拒绝在 Manager 关闭后注册或提交工作。"""
        if self._closed:
            raise OrchestrationError("orchestration manager is closed")


def _replace_task(
    task: TaskRecord,
    *,
    status: TaskStatus,
    result: object = None,
    error: str = "",
) -> TaskRecord:
    """复制任务并替换状态、结果与错误。"""
    return TaskRecord(
        task_id=task.task_id,
        owner_id=task.owner_id,
        attempt=task.attempt,
        role=task.role,
        pages=task.pages,
        group_id=task.group_id,
        dependencies=task.dependencies,
        status=status,
        goal=task.goal,
        repair=task.repair,
        result=result,
        error=str(error),
        request_key=task.request_key,
        base_deck_version=task.base_deck_version,
        page_uids=task.page_uids,
        revision_id=task.revision_id,
        reference_pages=task.reference_pages,
        output_path=task.output_path,
        handoff_from=task.handoff_from,
        asset_ids=task.asset_ids,
    )


def _json_value(value: object) -> object:
    """深复制 JSON 值，并拒绝不可持久化的线程局部对象。"""
    return json.loads(json.dumps(value, ensure_ascii=False))
