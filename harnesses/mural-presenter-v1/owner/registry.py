"""维护真实制作与已接受委派的持久 Owner 身份。

注册表不读取 Plan Markdown，也不推断页面应由谁负责。角色、页组和依赖全部来自
Orchestrator 的结构化动作；主 Agent 的页面归属在实际写入时登记。
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
from typing import Iterator, Mapping, Sequence
import uuid

from state.locking import workspace_lock


OWNER_REGISTRY_SCHEMA = "mural.owner-registry.v2"
OWNER_ROLES = frozenset({"research", "image", "slide"})


class OwnerRegistryError(RuntimeError):
    """表示 Owner 注册表合同或状态转换不成立。"""


class OwnerStatus(str, Enum):
    """列出持久 Owner 可以处于的机械状态。"""

    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    ARCHIVED = "archived"


_ALLOWED_TRANSITIONS: dict[OwnerStatus, frozenset[OwnerStatus]] = {
    OwnerStatus.PENDING: frozenset(
        {OwnerStatus.RUNNING, OwnerStatus.FAILED, OwnerStatus.INTERRUPTED, OwnerStatus.CANCELLED}
    ),
    OwnerStatus.RUNNING: frozenset(
        {
            OwnerStatus.WAITING,
            OwnerStatus.COMPLETED,
            OwnerStatus.FAILED,
            OwnerStatus.INTERRUPTED,
            OwnerStatus.CANCELLED,
        }
    ),
    OwnerStatus.WAITING: frozenset(
        {
            OwnerStatus.RUNNING,
            OwnerStatus.COMPLETED,
            OwnerStatus.FAILED,
            OwnerStatus.INTERRUPTED,
            OwnerStatus.CANCELLED,
        }
    ),
    OwnerStatus.INTERRUPTED: frozenset(
        {OwnerStatus.PENDING, OwnerStatus.RUNNING, OwnerStatus.FAILED, OwnerStatus.CANCELLED}
    ),
    OwnerStatus.COMPLETED: frozenset(),
    OwnerStatus.FAILED: frozenset(),
    OwnerStatus.CANCELLED: frozenset(),
    OwnerStatus.ARCHIVED: frozenset(),
}


@dataclass(frozen=True)
class OwnerRecord:
    """保存一个逻辑 Owner 的最小稳定合同。"""

    owner_id: str
    role: str
    pages: tuple[int, ...]
    group_id: str
    attempt: int
    status: OwnerStatus
    dependencies: tuple[str, ...] = ()
    output_path: str = ""
    handoff_from: str = ""

    def __post_init__(self) -> None:
        """规范角色、页码、依赖和状态字段。"""
        role = str(self.role).strip().lower()
        if role not in OWNER_ROLES and role != "orchestrator":
            raise ValueError(f"unsupported owner role {role!r}")
        pages = tuple(sorted(set(int(page) for page in self.pages)))
        if any(page < 1 for page in pages):
            raise ValueError("owner pages must contain positive integers")
        group_id = str(self.group_id).strip()
        if not group_id:
            raise ValueError("owner group_id must not be empty")
        if int(self.attempt) < 1:
            raise ValueError("owner attempt must be positive")
        dependencies = tuple(
            dict.fromkeys(
                str(item).strip() for item in self.dependencies if str(item).strip()
            )
        )
        object.__setattr__(self, "owner_id", str(self.owner_id).strip())
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "pages", pages)
        object.__setattr__(self, "group_id", group_id)
        object.__setattr__(self, "attempt", int(self.attempt))
        object.__setattr__(self, "status", OwnerStatus(self.status))
        object.__setattr__(self, "dependencies", dependencies)
        if not self.owner_id:
            raise ValueError("owner_id must not be empty")
        if role == "orchestrator":
            if self.owner_id != "orchestrator" or group_id != "orchestrator":
                raise ValueError("orchestrator owner_id and group_id must be 'orchestrator'")
        elif self.owner_id == "orchestrator" or group_id == "orchestrator":
            raise ValueError("orchestrator identity is reserved for the main Agent")

    @property
    def active(self) -> bool:
        """判断当前 attempt 是否仍在执行生命周期内。"""
        return self.status in {
            OwnerStatus.PENDING,
            OwnerStatus.RUNNING,
            OwnerStatus.WAITING,
        }

    def same_contract(self, *, role: str, pages: Sequence[int], output_path: str = "") -> bool:
        """判断角色与页面成员是否和当前逻辑 Owner 一致。"""
        normalized_pages = tuple(sorted(set(int(page) for page in pages)))
        return (self.role == str(role).strip().lower()
                and (self.role in {"research", "image"} or self.pages == normalized_pages)
                and self.output_path == output_path)

    def to_dict(self) -> dict[str, object]:
        """把记录转换为稳定 JSON 对象。"""
        return {
            "owner_id": self.owner_id,
            "role": self.role,
            "pages": list(self.pages),
            "group_id": self.group_id,
            "attempt": self.attempt,
            "status": self.status.value,
            "dependencies": list(self.dependencies),
            "output_path": self.output_path,
            "handoff_from": self.handoff_from,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "OwnerRecord":
        """从注册表 JSON 对象恢复 Owner 记录。"""
        raw_pages = value.get("pages") or []
        raw_dependencies = value.get("dependencies") or []
        if not isinstance(raw_pages, list) or not isinstance(raw_dependencies, list):
            raise ValueError("owner pages and dependencies must be arrays")
        return cls(
            owner_id=str(value.get("owner_id") or ""),
            role=str(value.get("role") or ""),
            pages=tuple(int(page) for page in raw_pages),
            group_id=str(value.get("group_id") or ""),
            attempt=int(value.get("attempt") or 0),
            status=OwnerStatus(str(value.get("status") or "")),
            dependencies=tuple(str(item) for item in raw_dependencies),
            output_path=str(value.get("output_path") or ""),
            handoff_from=str(value.get("handoff_from") or ""),
        )


class OwnerRegistry:
    """原子保存 Owner 身份，并对 attempt 状态执行比较更新。"""

    def __init__(self, workspace: str | Path, *, deck_id: str) -> None:
        """绑定工作区和 deck 身份，但不从工作区内容推断 Owner。"""
        self.workspace = Path(workspace).expanduser().resolve()
        self.state_dir = self.workspace / "_state"
        self.path = self.state_dir / "owners.json"
        self.deck_id = str(deck_id).strip()
        if not self.deck_id:
            raise ValueError("deck_id must not be empty")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self._locked_state() as state:
            stored_deck_id = str(state.get("deck_id") or "")
            if stored_deck_id and stored_deck_id != self.deck_id:
                raise OwnerRegistryError(
                    f"owner registry belongs to deck {stored_deck_id!r}, not {self.deck_id!r}"
                )
            if not stored_deck_id:
                state["deck_id"] = self.deck_id
                self._write_state(state)

    def accept(
        self,
        *,
        role: str,
        pages: Sequence[int] = (),
        group_id: str = "",
        dependencies: Sequence[str] = (),
        output_path: str = "",
    ) -> OwnerRecord:
        """接受结构化委派，并创建或复用对应逻辑 Owner。"""
        return self.accept_many(
            [
                {
                    "role": role,
                    "pages": list(pages),
                    "group_id": group_id,
                    "dependencies": list(dependencies),
                    "output_path": output_path,
                }
            ]
        )[0]

    def accept_many(
        self,
        contracts: Sequence[Mapping[str, object]],
        *,
        reject_unfinished: bool = False,
    ) -> tuple[OwnerRecord, ...]:
        """在一次文件事务中接受一个完整 wave 的 Owner 合同。"""
        normalized = [_normalize_contract(value) for value in contracts]
        if not normalized:
            raise OwnerRegistryError("owner contract batch must not be empty")
        group_ids = [str(item["group_id"]) for item in normalized]
        if len(group_ids) != len(set(group_ids)):
            raise OwnerRegistryError("owner contract batch contains duplicate group_id")

        with self._locked_state() as state:
            owners = _owner_map(state)
            retiring = _validate_handoffs(owners, normalized)
            _check_slide_ownership(tuple(owner for group, owner in owners.items() if group not in retiring), normalized)
            _check_research_outputs(tuple(owner for group, owner in owners.items() if group not in retiring), normalized)
            if reject_unfinished:
                conflicts = sorted(
                    str(item["group_id"])
                    for item in normalized
                    if (
                        (current := owners.get(str(item["group_id"]))) is not None
                        and current.status
                        not in {
                            OwnerStatus.COMPLETED,
                            OwnerStatus.FAILED,
                            OwnerStatus.CANCELLED,
                        }
                    )
                )
                if conflicts:
                    raise OwnerRegistryError(
                        f"owner groups already have unfinished attempts: {conflicts}"
                    )
            epochs = _epoch_map(state)
            archives = _archive_list(state)
            for group in retiring:
                archives.append(replace(owners.pop(group), status=OwnerStatus.ARCHIVED).to_dict())
            accepted: list[OwnerRecord] = []
            for item in normalized:
                role = str(item["role"])
                pages = tuple(item["pages"])
                group_id = str(item["group_id"])
                dependencies = tuple(item["dependencies"])
                current = owners.get(group_id)
                if current is not None and current.same_contract(
                    role=role, pages=pages, output_path=item["output_path"]
                ):
                    if current.active:
                        record = current
                    else:
                        record = replace(
                            current,
                            attempt=current.attempt + 1,
                            status=OwnerStatus.PENDING,
                            pages=pages,
                            dependencies=dependencies,
                        )
                else:
                    epoch = int(epochs.get(group_id, 0)) + 1
                    epochs[group_id] = epoch
                    if current is not None:
                        archives.append(
                            replace(current, status=OwnerStatus.ARCHIVED).to_dict()
                        )
                    record = OwnerRecord(
                        owner_id=_owner_id(self.deck_id, group_id, epoch),
                        role=role,
                        pages=pages,
                        group_id=group_id,
                        attempt=1,
                        status=OwnerStatus.PENDING,
                        dependencies=dependencies,
                        output_path=item["output_path"],
                        handoff_from=item["handoff_from"],
                    )
                owners[group_id] = record
                accepted.append(record)
            state["group_epochs"] = epochs
            state["archives"] = archives
            state["owners"] = {
                key: value.to_dict() for key, value in sorted(owners.items())
            }
            self._write_state(state)
            return tuple(accepted)

    def validate_page_ownership(self, contracts: Sequence[Mapping[str, object]]) -> None:
        """调度提交意图落盘前预检；accept_many 持锁写入时仍执行同一校验。"""
        normalized = [_normalize_contract(value) for value in contracts]
        with self._locked_state() as state:
            owners = _owner_map(state)
            retiring = _validate_handoffs(owners, normalized)
            remaining = tuple(owner for group, owner in owners.items() if group not in retiring)
            _check_slide_ownership(remaining, normalized)
            _check_research_outputs(remaining, normalized)

    def page_owner(self, page: int) -> OwnerRecord | None:
        """读取页面的实际责任方；素材消费者页码不授予页面责任。"""
        if type(page) is not int or page < 1:
            raise OwnerRegistryError("page must be a positive integer")
        with self._locked_state() as state:
            return next((owner for owner in _owner_map(state).values()
                         if owner.role in {"slide", "orchestrator"} and page in owner.pages), None)

    def claim_orchestrator_page(self, page: int) -> OwnerRecord:
        """在成功写入的工作区事务内，登记或扩展主 Agent 的页面责任。"""
        if type(page) is not int or page < 1:
            raise OwnerRegistryError("page must be a positive integer")
        with self._locked_state() as state:
            owners = _owner_map(state)
            for owner in owners.values():
                if owner.role not in {"slide", "orchestrator"} or page not in owner.pages:
                    continue
                if owner.role == "orchestrator":
                    return owner
                raise OwnerRegistryError(
                    f"page_owner_conflict: page {page} belongs to {owner.owner_id} "
                    f"(group_id={owner.group_id!r}, pages={list(owner.pages)}). "
                    "Return changes to the original Owner; the main Agent cannot take over the page."
                )
            current = owners.get("orchestrator")
            record = replace(current, pages=(*current.pages, page)) if current else OwnerRecord(
                owner_id="orchestrator", role="orchestrator", pages=(page,),
                group_id="orchestrator", attempt=1, status=OwnerStatus.RUNNING,
            )
            owners[record.group_id] = record
            state["owners"] = {key: value.to_dict() for key, value in sorted(owners.items())}
            self._write_state(state)
            return record

    def transition(
        self,
        owner_id: str,
        *,
        attempt: int,
        status: OwnerStatus | str,
    ) -> OwnerRecord:
        """以 owner_id 和 attempt 比较更新状态，拒绝过期执行回写。"""
        target = OwnerStatus(status)
        with self._locked_state() as state:
            owners = _owner_map(state)
            current = next(
                (record for record in owners.values() if record.owner_id == owner_id),
                None,
            )
            if current is None:
                raise OwnerRegistryError(f"unknown active owner {owner_id!r}")
            if current.attempt != int(attempt):
                raise OwnerRegistryError(
                    f"stale owner attempt {attempt}; current attempt is {current.attempt}"
                )
            if current.status == target:
                return current
            if target not in _ALLOWED_TRANSITIONS[current.status]:
                raise OwnerRegistryError(
                    f"invalid owner transition {current.status.value!r} -> {target.value!r}"
                )
            updated = replace(current, status=target)
            owners[current.group_id] = updated
            state["owners"] = {
                key: value.to_dict() for key, value in sorted(owners.items())
            }
            self._write_state(state)
            return updated

    def resume_orchestrator(self) -> OwnerRecord | None:
        """整进程恢复仍使用同一主身份，不把一次新执行伪装成子任务。"""
        with self._locked_state() as state:
            owners = _owner_map(state)
            current = owners.get("orchestrator")
            if current is None:
                return None
            updated = replace(current, attempt=current.attempt + 1, status=OwnerStatus.RUNNING)
            state["owners"]["orchestrator"] = updated.to_dict()
            self._write_state(state)
            return updated

    def get(self, owner_id: str) -> OwnerRecord:
        """按稳定 owner_id 读取当前 Owner。"""
        with self._locked_state() as state:
            record = next(
                (
                    item
                    for item in _owner_map(state).values()
                    if item.owner_id == str(owner_id)
                ),
                None,
            )
            if record is None:
                raise KeyError(owner_id)
            return record

    def get_group(self, group_id: str) -> OwnerRecord | None:
        """按显式 group_id 读取当前 Owner，缺失时返回空值。"""
        with self._locked_state() as state:
            return _owner_map(state).get(str(group_id).strip())

    def resolve_submission_contract(self, value: Mapping[str, object]) -> dict[str, object]:
        """仅按显式身份补齐省略的原责任范围；不猜角色、不改归属，也不放松接受时校验。"""
        if not value.get("group_id") and not value.get("handoff_from"):
            return _normalize_contract(value)
        resolved = dict(value)
        with self._locked_state() as state:
            owners = _owner_map(state)
            handoff = value.get("handoff_from")
            requested_group = str(value.get("group_id") or "").strip()
            if not requested_group and handoff:
                role = str(value.get("role") or "").strip().lower()
                task_id = str(value.get("task_id") or "")
                if role in OWNER_ROLES and task_id:
                    requested_group = f"{role}-{task_id}"
            source = owners.get(requested_group)
            if handoff:
                if source is None or source.handoff_from != handoff:
                    source = next((owner for owner in owners.values() if owner.owner_id == handoff), None)
                if source is None:
                    raise OwnerRegistryError(f"handoff_from must identify a current Owner: {handoff!r}")
            if source is not None:
                if "pages" not in resolved:
                    resolved["pages"] = list(source.pages)
                if source.role == "research" and "output_path" not in resolved:
                    resolved["output_path"] = source.output_path
        return _normalize_contract(resolved)

    def list_current(self) -> tuple[OwnerRecord, ...]:
        """返回按 group_id 排序的当前 Owner 快照。"""
        with self._locked_state() as state:
            owners = _owner_map(state)
            return tuple(owners[key] for key in sorted(owners))

    def list_archived(self) -> tuple[OwnerRecord, ...]:
        """返回因成员变化而归档的历史 Owner。"""
        with self._locked_state() as state:
            return tuple(
                OwnerRecord.from_dict(item) for item in _archive_list(state)
            )

    def snapshot(self) -> dict[str, object]:
        """返回可审计但不共享内部引用的注册表快照。"""
        with self._locked_state() as state:
            return json.loads(json.dumps(state, ensure_ascii=False))

    @contextmanager
    def _locked_state(self) -> Iterator[dict[str, object]]:
        with workspace_lock(self.workspace):
            yield self._read_state()

    def _read_state(self) -> dict[str, object]:
        """读取并校验当前注册表顶层结构。"""
        if not self.path.exists():
            return {
                "schema": OWNER_REGISTRY_SCHEMA,
                "deck_id": self.deck_id,
                "group_epochs": {},
                "owners": {},
                "archives": [],
            }
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise OwnerRegistryError("owner registry root must be an object")
        if value.get("schema") != OWNER_REGISTRY_SCHEMA:
            raise OwnerRegistryError(
                f"unsupported owner registry schema {value.get('schema')!r}"
            )
        _owner_map(value)
        _archive_list(value)
        _epoch_map(value)
        return value

    def _write_state(self, state: Mapping[str, object]) -> None:
        """用同目录原子替换持久化完整注册表。"""
        payload = json.dumps(
            dict(state), ensure_ascii=False, sort_keys=True, indent=2
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


def _validate_handoffs(owners, contracts) -> set[str]:
    retiring: set[str] = set()
    targets = {row["group_id"] for row in contracts}
    for row in contracts:
        predecessor = row["handoff_from"]
        if not predecessor:
            continue
        current = owners.get(row["group_id"])
        if current is not None:
            if (current.handoff_from == predecessor and current.active
                    and current.same_contract(role=row["role"], pages=row["pages"], output_path=row["output_path"])):
                continue  # 已持久化提交在任务记录落盘前中断，重放不再次迁移。
            raise OwnerRegistryError("handoff requires a new group_id")
        source = next((owner for owner in owners.values() if owner.owner_id == predecessor), None)
        if source is None or source.role != row["role"]:
            raise OwnerRegistryError("handoff_from must identify a current Owner of the same role")
        if source.status not in {OwnerStatus.COMPLETED, OwnerStatus.FAILED, OwnerStatus.CANCELLED}:
            raise OwnerRegistryError("handoff requires the previous Owner attempt to finish first")
        if source.group_id in targets:
            raise OwnerRegistryError("cannot resume and hand off the same Owner in one submission")
        if row["role"] == "image":
            continue  # 素材仅按显式 ID 迁移；原 Owner 仍保有其他素材。
        if source.group_id in retiring:
            raise OwnerRegistryError("cannot hand off the same Owner twice")
        if row["role"] == "slide" and source.pages != tuple(row["pages"]):
            raise OwnerRegistryError("page handoff must keep the complete previous page group")
        if row["role"] == "research" and source.output_path != row["output_path"]:
            raise OwnerRegistryError("research handoff must keep the previous output_path")
        retiring.add(source.group_id)
    return retiring


def _check_research_outputs(owners, contracts) -> None:
    claims = {owner.output_path: owner.group_id for owner in owners if owner.role == "research" and owner.output_path}
    for row in contracts:
        if row["role"] != "research":
            continue
        path, group = row["output_path"], row["group_id"]
        prior = claims.get(path)
        if prior is not None and prior != group:
            raise OwnerRegistryError(f"research_output_conflict: {path!r} belongs to group {prior!r}")
        previous = next((owner for owner in owners if owner.group_id == group), None)
        if previous and (previous.role != "research" or previous.output_path != path):
            raise OwnerRegistryError("research continuation must keep the original output_path and role")
        claims[path] = group


def _check_slide_ownership(owners: Sequence[OwnerRecord], contracts: Sequence[Mapping[str, object]]) -> None:
    # 完成一次任务不释放页面责任；结构事务会显式归档受影响的 Owner。
    claims = [(owner.role, owner.pages, owner.group_id, owner.owner_id) for owner in owners]
    for contract in contracts:
        role, pages, group = contract["role"], tuple(contract["pages"]), contract["group_id"]
        for prior_role, prior_pages, prior_group, owner_id in claims:
            same_group = group == prior_group
            shared_pages = (role in {"slide", "orchestrator"} and prior_role in {"slide", "orchestrator"}
                            and bool(set(pages).intersection(prior_pages)))
            replaces_page_owner = same_group and bool({role, prior_role} & {"slide", "orchestrator"})
            if not (shared_pages or replaces_page_owner):
                continue
            if same_group and role == prior_role and pages == prior_pages:
                continue
            if prior_role == "orchestrator":
                raise OwnerRegistryError(
                    f"page_owner_conflict: requested group_id={group!r}, pages={list(pages)} "
                    f"conflicts with orchestrator (pages={list(prior_pages)}). "
                    "These pages must continue in the main Agent context; do not delegate them to a new Owner."
                )
            raise OwnerRegistryError(
                f"page_owner_conflict: requested group_id={group!r}, pages={list(pages)} "
                f"conflicts with {owner_id} (group_id={prior_group!r}, pages={list(prior_pages)}). "
                "For repair, use a new task_id with the original group_id and complete pages; "
                "put only the targeted changes in goal. A fresh executor requires explicit handoff_from "
                "after the previous task finishes. Membership changes require a structural revision."
            )
        claims.append((role, pages, group, f"submitted group {group!r}"))


def _derived_group_id(role: str, pages: Sequence[int]) -> str:
    """为省略 group_id 的结构化委派生成确定性组名。"""
    membership = ",".join(str(page) for page in pages) or "global"
    digest = hashlib.sha256(f"{role}:{membership}".encode("utf-8")).hexdigest()[:12]
    return f"{role}-{digest}"


def _normalize_contract(value: Mapping[str, object]) -> dict[str, object]:
    """校验一个 Owner 合同，并补出稳定的默认 group_id。"""
    if not isinstance(value, Mapping):
        raise OwnerRegistryError("owner contract must be an object")
    role = str(value.get("role") or "").strip().lower()
    if role not in OWNER_ROLES:
        raise OwnerRegistryError(f"unsupported owner role {role!r}")
    raw_pages = value.get("pages") or []
    if not isinstance(raw_pages, Sequence) or isinstance(raw_pages, (str, bytes)):
        raise OwnerRegistryError("owner pages must be an array")
    if any(
        not isinstance(page, int) or isinstance(page, bool) for page in raw_pages
    ):
        raise OwnerRegistryError("owner pages must contain integers")
    pages = tuple(sorted(set(raw_pages)))
    if any(page < 1 for page in pages):
        raise OwnerRegistryError("owner pages must contain positive integers")
    handoff_from = value.get("handoff_from", "")
    if not isinstance(handoff_from, str):
        raise OwnerRegistryError("handoff_from must be an Owner ID string")
    task_id = str(value.get("task_id") or "")
    group_id = str(value.get("group_id") or "").strip() or (
        f"{role}-{task_id}" if task_id and (role != "slide" or handoff_from) else _derived_group_id(role, pages))
    if group_id == "orchestrator":
        raise OwnerRegistryError("group_id 'orchestrator' is reserved for the main Agent")
    output_path = value.get("output_path", "")
    if not isinstance(output_path, str):
        raise OwnerRegistryError("output_path must be a workspace-relative Markdown path")
    if role == "research":
        if not output_path:
            digest = hashlib.sha256(group_id.encode("utf-8")).hexdigest()[:16]
            output_path = f"research/{digest}.md"
        from runtime.tools import normalize_workspace_path
        try:
            output_path = normalize_workspace_path(output_path)
        except ValueError as exc:
            raise OwnerRegistryError(str(exc)) from exc
        if not output_path.startswith("research/") or not output_path.endswith(".md"):
            raise OwnerRegistryError("Research output_path must be a Markdown file under research/")
    elif output_path:
        raise OwnerRegistryError("output_path is only available to Research tasks")
    raw_dependencies = value.get("dependencies") or []
    if not isinstance(raw_dependencies, Sequence) or isinstance(
        raw_dependencies, (str, bytes)
    ):
        raise OwnerRegistryError("owner dependencies must be an array")
    dependencies = tuple(
        dict.fromkeys(
            str(item).strip() for item in raw_dependencies if str(item).strip()
        )
    )
    return {
        "role": role,
        "pages": pages,
        "group_id": group_id,
        "dependencies": dependencies,
        "output_path": output_path,
        "handoff_from": handoff_from,
    }


def _owner_id(deck_id: str, group_id: str, epoch: int) -> str:
    """由 deck、组与成员代次生成稳定且不泄露长文本的 Owner ID。"""
    digest = hashlib.sha256(f"{deck_id}\0{group_id}".encode("utf-8")).hexdigest()[:16]
    return f"owner-{digest}-e{epoch}"


def _owner_map(state: Mapping[str, object]) -> dict[str, OwnerRecord]:
    """校验并解码顶层当前 Owner 映射。"""
    raw = state.get("owners") or {}
    if not isinstance(raw, dict):
        raise OwnerRegistryError("owner registry owners must be an object")
    records: dict[str, OwnerRecord] = {}
    for group_id, value in raw.items():
        if not isinstance(value, dict):
            raise OwnerRegistryError("owner registry records must be objects")
        record = OwnerRecord.from_dict(value)
        if record.group_id != str(group_id):
            raise OwnerRegistryError("owner registry group key does not match record")
        records[str(group_id)] = record
    return records


def _archive_list(state: Mapping[str, object]) -> list[dict[str, object]]:
    """校验并复制历史 Owner 列表。"""
    raw = state.get("archives") or []
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise OwnerRegistryError("owner registry archives must be an array of objects")
    for item in raw:
        OwnerRecord.from_dict(item)
    return [dict(item) for item in raw]


def _epoch_map(state: Mapping[str, object]) -> dict[str, int]:
    """校验并复制每个 group 的成员代次。"""
    raw = state.get("group_epochs") or {}
    if not isinstance(raw, dict):
        raise OwnerRegistryError("owner registry group_epochs must be an object")
    result = {str(key): int(value) for key, value in raw.items()}
    if any(value < 1 for value in result.values()):
        raise OwnerRegistryError("owner registry epochs must be positive")
    return result
