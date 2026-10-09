"""持久化 Issue Ledger，并依据结构化证据路由原 Owner。

路由只消费 issue 类型、页码、Owner hint、Catalog/hash 变化和工具事件；它不解析
``PAGE DECISION``、``DELIVERY ISSUE`` 或其他自由文本协议。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Iterable, Mapping, Sequence
import uuid

from owner.registry import OwnerRecord, OwnerRegistry
from state.artifacts import ArtifactStore
from state.locking import workspace_lock
from state.workspace import file_sha256

from .content import ContentFinding
from .visual import ReviewVerdict, VisualCandidate, VisualReview


ISSUE_LEDGER_SCHEMA = "mural.issue-ledger.v1"
BLOCKING_SEVERITIES = frozenset({"major", "critical"})


class IssueLedgerError(RuntimeError):
    """表示 Issue 状态、复核或验证合同不成立。"""


class IssueStatus(str, Enum):
    """列出 Issue 从候选到闭合的持久状态。"""

    CANDIDATE = "candidate"
    OPEN = "open"
    UNRESOLVED = "unresolved"
    RESOLVED = "resolved"
    ACCEPTED_DEGRADATION = "accepted_degradation"
    REJECTED = "rejected"


class RouteTarget(str, Enum):
    """列出 Harness 可返回的修订目标。"""

    SLIDE = "slide"
    IMAGE = "image"
    ORCHESTRATOR = "orchestrator"
    NONE = "none"


class OwnerIntent(str, Enum):
    """列出由实际工具行为识别的 Owner 意图。"""

    REPAIR = "repair"
    READY = "ready"
    HELP = "help"
    ACTIVE = "active"


@dataclass(frozen=True)
class IssueRecord:
    """保存一条问题、复核结果、路由与证据指纹。"""

    issue_id: str
    fingerprint: str
    source: str
    kind: str
    severity: str
    pages: tuple[int, ...]
    summary: str
    owner_hint: str
    objective: bool
    status: IssueStatus
    review_verdict: str = ""
    evidence: object = field(default_factory=dict)
    artifact_fingerprint: str = ""
    route_target: RouteTarget = RouteTarget.NONE
    route_owner_id: str = ""
    last_activation_fingerprint: str = ""
    resolution: str = ""

    def __post_init__(self) -> None:
        """规范状态、路由、页码并深复制证据。"""
        object.__setattr__(self, "status", IssueStatus(self.status))
        object.__setattr__(self, "route_target", RouteTarget(self.route_target))
        object.__setattr__(
            self, "pages", tuple(sorted(set(int(page) for page in self.pages)))
        )
        object.__setattr__(self, "evidence", copy.deepcopy(self.evidence))

    @property
    def blocks_positive_admission(self) -> bool:
        """判断当前问题是否阻止正训练样本准入。"""
        return self.severity in BLOCKING_SEVERITIES and self.status in {
            IssueStatus.CANDIDATE,
            IssueStatus.OPEN,
            IssueStatus.UNRESOLVED,
        }

    def to_dict(self) -> dict[str, object]:
        """把 Issue 转换为持久 JSON 对象。"""
        return {
            "issue_id": self.issue_id,
            "fingerprint": self.fingerprint,
            "source": self.source,
            "kind": self.kind,
            "severity": self.severity,
            "pages": list(self.pages),
            "summary": self.summary,
            "owner_hint": self.owner_hint,
            "objective": self.objective,
            "status": self.status.value,
            "review_verdict": self.review_verdict,
            "evidence": copy.deepcopy(self.evidence),
            "artifact_fingerprint": self.artifact_fingerprint,
            "route_target": self.route_target.value,
            "route_owner_id": self.route_owner_id,
            "last_activation_fingerprint": self.last_activation_fingerprint,
            "resolution": self.resolution,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "IssueRecord":
        """从 Ledger JSON 恢复一条 Issue。"""
        pages = value.get("pages") or []
        if not isinstance(pages, list):
            raise ValueError("issue pages must be an array")
        return cls(
            issue_id=str(value.get("issue_id") or ""),
            fingerprint=str(value.get("fingerprint") or ""),
            source=str(value.get("source") or ""),
            kind=str(value.get("kind") or ""),
            severity=str(value.get("severity") or ""),
            pages=tuple(int(page) for page in pages),
            summary=str(value.get("summary") or ""),
            owner_hint=str(value.get("owner_hint") or "none"),
            objective=bool(value.get("objective")),
            status=IssueStatus(str(value.get("status") or "")),
            review_verdict=str(value.get("review_verdict") or ""),
            evidence=copy.deepcopy(value.get("evidence")),
            artifact_fingerprint=str(value.get("artifact_fingerprint") or ""),
            route_target=RouteTarget(str(value.get("route_target") or "none")),
            route_owner_id=str(value.get("route_owner_id") or ""),
            last_activation_fingerprint=str(
                value.get("last_activation_fingerprint") or ""
            ),
            resolution=str(value.get("resolution") or ""),
        )


@dataclass(frozen=True)
class RouteAction:
    """表示一次可执行修订路由或明确的不激活决定。"""

    issue_id: str
    target: RouteTarget
    owner_id: str
    pages: tuple[int, ...]
    activation_fingerprint: str
    reason: str

    def to_dict(self) -> dict[str, object]:
        """把修订动作转换为调度器可消费对象。"""
        return {
            "issue_id": self.issue_id,
            "target": self.target.value,
            "owner_id": self.owner_id,
            "pages": list(self.pages),
            "activation_fingerprint": self.activation_fingerprint,
            "reason": self.reason,
        }


class IssueLedger:
    """原子保存 QC 候选、复核、验证和 accepted degradation。"""

    def __init__(self, workspace: str | Path) -> None:
        """绑定工作区 Issue Ledger。"""
        self.workspace = Path(workspace).expanduser().resolve()
        self.directory = self.workspace / "_state"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "issues.json"
        self._lock = workspace_lock(self.workspace)
        with self._lock:
            state = self._read_state()
            if not self.path.exists():
                self._write_state(state)

    def register_visual_candidates(
        self, candidates: Sequence[VisualCandidate]
    ) -> tuple[IssueRecord, ...]:
        """先把全部 Critic 候选登记为 candidate，防止预算静默丢失。"""
        return tuple(
            self._upsert(
                source="visual",
                kind=candidate.kind,
                severity=candidate.severity,
                pages=(() if candidate.page is None else (candidate.page,)),
                summary=candidate.summary,
                owner_hint=candidate.owner_hint,
                objective=candidate.objective,
                status=IssueStatus.CANDIDATE,
                review_verdict="",
                evidence=[item.to_dict() for item in candidate.evidence],
                artifact_fingerprint=_visual_artifact_fingerprint(candidate),
                resolution="",
            )
            for candidate in candidates
        )

    def record_external_visual_candidates(
        self,
        candidates: Sequence[VisualCandidate],
        *,
        disposition: str = "",
    ) -> tuple[IssueRecord, ...]:
        """登记外部视觉 Critic 候选，source=external_visual_critic，含同哈希去重。

        candidate_id 已绑定 inspection + 像素 hash，故同一未变像素的同一问题经 _upsert
        指纹自动去重；不与 native 视觉候选混淆来源。disposition 记进 resolution 供审计。
        """
        return tuple(
            self._upsert(
                source="external_visual_critic",
                kind=candidate.kind,
                severity=candidate.severity,
                pages=(() if candidate.page is None else (candidate.page,)),
                summary=candidate.summary,
                owner_hint=candidate.owner_hint,
                objective=candidate.objective,
                status=(
                    IssueStatus.OPEN
                    if candidate.severity in {"major", "critical"}
                    else IssueStatus.CANDIDATE
                ),
                review_verdict="",
                evidence=[item.to_dict() for item in candidate.evidence],
                artifact_fingerprint=_visual_artifact_fingerprint(candidate),
                resolution=str(disposition),
            )
            for candidate in candidates
        )

    def record_visual_reviews(
        self, reviews: Sequence[VisualReview]
    ) -> tuple[IssueRecord, ...]:
        """把每条复核结果更新为 open、rejected 或 unresolved。"""
        records: list[IssueRecord] = []
        for review in reviews:
            status = {
                ReviewVerdict.CONFIRMED: IssueStatus.OPEN,
                ReviewVerdict.REJECTED: IssueStatus.REJECTED,
                ReviewVerdict.UNRESOLVED: IssueStatus.UNRESOLVED,
            }[review.verdict]
            candidate = review.candidate
            records.append(
                self._upsert(
                    source="visual",
                    kind=candidate.kind,
                    severity=candidate.severity,
                    pages=(() if candidate.page is None else (candidate.page,)),
                    summary=candidate.summary,
                    owner_hint=candidate.owner_hint,
                    objective=candidate.objective,
                    status=status,
                    review_verdict=review.verdict.value,
                    evidence=[item.to_dict() for item in candidate.evidence],
                    artifact_fingerprint=_visual_artifact_fingerprint(candidate),
                    resolution=review.rationale,
                )
            )
        return tuple(records)

    def record_content_findings(
        self, findings: Sequence[ContentFinding]
    ) -> tuple[IssueRecord, ...]:
        """把 Content Reviewer findings 登记为可路由 open issue。"""
        return tuple(
            self._upsert(
                source="content",
                kind=finding.kind,
                severity=finding.severity,
                pages=finding.pages,
                summary=finding.summary,
                owner_hint=finding.owner_hint,
                objective=finding.objective,
                status=IssueStatus.OPEN,
                review_verdict="confirmed",
                evidence=finding.evidence,
                artifact_fingerprint=_evidence_fingerprint(finding.evidence),
                resolution="",
            )
            for finding in findings
        )

    def record_orchestrator_issues(
        self, issues: Sequence[Mapping[str, object]]
    ) -> tuple[IssueRecord, ...]:
        """把 Orchestrator 的结构化整册复核问题登记为可路由 Issue。"""
        records: list[IssueRecord] = []
        for issue in issues:
            pages_value = issue.get("positions") or []
            if not isinstance(pages_value, list):
                raise IssueLedgerError("verification issue positions must be an array")
            owner_hint = str(issue.get("target") or "")
            evidence = {
                "observation": str(issue["problem"]),
                "suggested_fix": str(issue["suggested_fix"]),
                "page_uids": [str(item) for item in issue.get("page_uids") or []],
                "artifact_versions": issue.get("artifact_versions") or [],
            }
            records.append(
                self._upsert(
                    source="orchestrator",
                    kind="verification_issue",
                    severity="major",
                    pages=tuple(int(page) for page in pages_value),
                    summary=str(issue["problem"]),
                    owner_hint=owner_hint,
                    objective=False,
                    status=IssueStatus.OPEN,
                    review_verdict="confirmed_by_orchestrator",
                    evidence=evidence,
                    artifact_fingerprint=_evidence_fingerprint(evidence),
                    resolution="",
                )
            )
        return tuple(records)

    def verification(
        self,
        *,
        scope: str,
        verdict: str,
        issue_ids: Sequence[str],
        actor: str,
        rationale: str = "",
        dry_run: bool = False,
    ) -> dict[str, object]:
        """应用结构化验证，并拒绝在 major 未闭合时提交 pass。"""
        normalized_scope = str(scope).strip() or "deck"
        normalized_verdict = str(verdict).strip().lower()
        if normalized_verdict not in {
            "pass",
            "repair_required",
            "unresolved",
            "accepted_degradation",
        }:
            raise IssueLedgerError(f"unsupported verification verdict {verdict!r}")
        ids = tuple(dict.fromkeys(str(item).strip() for item in issue_ids if str(item).strip()))
        with self._lock:
            state = self._read_state()
            records = _issue_records(state)
            missing = sorted(set(ids) - set(records))
            if missing:
                raise IssueLedgerError(f"unknown issue ids: {missing}")
            scoped = [item for item in records.values() if _in_scope(item, normalized_scope)]
            if normalized_verdict == "pass":
                out_of_scope = [
                    issue_id
                    for issue_id in ids
                    if not _in_scope(records[issue_id], normalized_scope)
                ]
                if out_of_scope:
                    raise IssueLedgerError(
                        f"verification issues are outside scope: {out_of_scope}"
                    )
                for issue_id in ids:
                    records[issue_id] = replace(
                        records[issue_id],
                        status=IssueStatus.RESOLVED,
                        resolution=str(rationale),
                    )
                scoped = [
                    item for item in records.values() if _in_scope(item, normalized_scope)
                ]
                blockers = [item.issue_id for item in scoped if item.blocks_positive_admission]
                if blockers:
                    raise IssueLedgerError(
                        f"scope cannot pass with unresolved major issues: {blockers}"
                    )
            else:
                if not ids:
                    raise IssueLedgerError(
                        f"verification verdict {normalized_verdict!r} requires issue_ids"
                    )
                target_status = {
                    "repair_required": IssueStatus.OPEN,
                    "unresolved": IssueStatus.UNRESOLVED,
                    "accepted_degradation": IssueStatus.ACCEPTED_DEGRADATION,
                }[normalized_verdict]
                if target_status == IssueStatus.ACCEPTED_DEGRADATION and not str(rationale).strip():
                    raise IssueLedgerError("accepted degradation requires rationale")
                for issue_id in ids:
                    records[issue_id] = replace(
                        records[issue_id],
                        status=target_status,
                        resolution=str(rationale),
                    )
            if dry_run:
                return {"scope": normalized_scope, "verdict": normalized_verdict, "issue_ids": list(ids)}
            state["issues"] = {
                key: item.to_dict() for key, item in sorted(records.items())
            }
            self._append_event(
                state,
                "verification_submitted",
                scope=normalized_scope,
                verdict=normalized_verdict,
                issue_ids=list(ids),
                actor=str(actor),
                rationale=str(rationale),
            )
            self._write_state(state)
            return {
                "scope": normalized_scope,
                "verdict": normalized_verdict,
                "issue_ids": list(ids),
            }

    def resolve_issues(self, issue_ids: Sequence[str], *, actor: str, rationale: str) -> dict[str, object]:
        """只闭合模型明确复核的条目，允许分批解决；不产生整册 pass。"""
        ids = tuple(dict.fromkeys(str(value).strip() for value in issue_ids))
        with self._lock:
            state = self._read_state()
            records = _issue_records(state)
            if not ids or any(issue_id not in records for issue_id in ids):
                raise IssueLedgerError("resolve_issues requires existing issue ids")
            for issue_id in ids:
                records[issue_id] = replace(records[issue_id], status=IssueStatus.RESOLVED, resolution=rationale)
            state["issues"] = {key: item.to_dict() for key, item in sorted(records.items())}
            self._append_event(state, "issues_resolved", issue_ids=list(ids), actor=actor, rationale=rationale)
            self._write_state(state)
            return {"scope": "deck", "issue_ids": list(ids)}

    def resolve_current_visual_issues(
        self, *, page: int, png_sha256: str, inspection_id: str,
        actor: str, rationale: str,
    ) -> tuple[IssueRecord, ...]:
        """Close blocking visual issues proven against the same current page pixels."""
        page_number = int(page)
        pixel_hash = str(png_sha256 or "").strip()
        if not page_number or not pixel_hash:
            return ()
        with self._lock:
            state = self._read_state()
            records = _issue_records(state)
            resolved: list[IssueRecord] = []
            for issue_id, issue in records.items():
                if not issue.blocks_positive_admission:
                    continue
                if issue.pages and page_number not in issue.pages:
                    continue
                if not _is_current_visual_issue(issue, pixel_hash):
                    continue
                updated = replace(issue, status=IssueStatus.RESOLVED, resolution=str(rationale))
                records[issue_id] = updated
                resolved.append(updated)
            if not resolved:
                return ()
            state["issues"] = {key: item.to_dict() for key, item in sorted(records.items())}
            self._append_event(state, "visual_issues_resolved_by_clean_inspection",
                               issue_ids=[item.issue_id for item in resolved],
                               page=page_number, png_sha256=pixel_hash,
                               inspection_id=str(inspection_id), actor=str(actor),
                               rationale=str(rationale))
            self._write_state(state)
            return tuple(resolved)

    def record_route(self, action: RouteAction) -> IssueRecord:
        """保存一次真实激活指纹，供后续无变化抑制使用。"""
        with self._lock:
            state = self._read_state()
            records = _issue_records(state)
            issue = records.get(action.issue_id)
            if issue is None:
                raise IssueLedgerError(f"unknown issue {action.issue_id!r}")
            updated = replace(
                issue,
                route_target=action.target,
                route_owner_id=action.owner_id,
                last_activation_fingerprint=action.activation_fingerprint,
            )
            records[action.issue_id] = updated
            state["issues"] = {
                key: item.to_dict() for key, item in sorted(records.items())
            }
            self._append_event(
                state, "issue_routed", **action.to_dict()
            )
            self._write_state(state)
            return updated

    def issue(self, issue_id: str) -> IssueRecord:
        """读取一条 Issue。"""
        with self._lock:
            issue = _issue_records(self._read_state()).get(str(issue_id))
            if issue is None:
                raise KeyError(issue_id)
            return issue

    def issues(self) -> tuple[IssueRecord, ...]:
        """返回按 issue_id 排序的 Ledger 快照。"""
        with self._lock:
            records = _issue_records(self._read_state())
            return tuple(records[key] for key in sorted(records))

    def blocking_issues(self, scope: str = "deck") -> tuple[IssueRecord, ...]:
        """返回指定范围内阻止正训练准入的问题。"""
        return tuple(
            issue
            for issue in self.issues()
            if _in_scope(issue, scope) and issue.blocks_positive_admission
        )

    def _upsert(
        self,
        *,
        source: str,
        kind: str,
        severity: str,
        pages: Sequence[int],
        summary: str,
        owner_hint: str,
        objective: bool,
        status: IssueStatus,
        review_verdict: str,
        evidence: object,
        artifact_fingerprint: str,
        resolution: str,
    ) -> IssueRecord:
        """按证据指纹去重，并允许 candidate 被复核结果推进。"""
        fingerprint = _issue_fingerprint(
            source=source,
            kind=kind,
            pages=pages,
            evidence=evidence,
            artifact_fingerprint=artifact_fingerprint,
        )
        with self._lock:
            state = self._read_state()
            records = _issue_records(state)
            existing = next(
                (item for item in records.values() if item.fingerprint == fingerprint),
                None,
            )
            if existing is not None:
                promote = existing.status == IssueStatus.CANDIDATE and status != IssueStatus.CANDIDATE
                retarget = source == "orchestrator" and bool(owner_hint) and existing.owner_hint != owner_hint
                if promote or retarget:
                    existing = replace(
                        existing,
                        status=status if promote else existing.status,
                        review_verdict=str(review_verdict) if promote else existing.review_verdict,
                        resolution=str(resolution) if promote else existing.resolution,
                        owner_hint=str(owner_hint) if retarget else existing.owner_hint,
                    )
                    records[existing.issue_id] = existing
                    state["issues"] = {
                        key: item.to_dict() for key, item in sorted(records.items())
                    }
                    self._append_event(
                        state,
                        "issue_reviewed",
                        issue_id=existing.issue_id,
                        status=existing.status.value,
                        owner_hint=existing.owner_hint,
                    )
                    self._write_state(state)
                return existing
            issue_id = f"issue-{fingerprint[:20]}"
            record = IssueRecord(
                issue_id=issue_id,
                fingerprint=fingerprint,
                source=str(source),
                kind=str(kind),
                severity=str(severity).strip().lower(),
                pages=tuple(int(page) for page in pages),
                summary=str(summary),
                owner_hint=str(owner_hint).strip().lower(),
                objective=bool(objective),
                status=status,
                review_verdict=str(review_verdict),
                evidence=copy.deepcopy(evidence),
                artifact_fingerprint=str(artifact_fingerprint),
                resolution=str(resolution),
            )
            records[issue_id] = record
            state["issues"] = {
                key: item.to_dict() for key, item in sorted(records.items())
            }
            self._append_event(
                state, "issue_registered", issue_id=issue_id, status=status.value
            )
            self._write_state(state)
            return record

    def _append_event(
        self, state: dict[str, object], event_type: str, **fields: object
    ) -> None:
        """追加 Issue 状态事件。"""
        sequence = int(state.get("sequence") or 0) + 1
        events = state.get("events") or []
        if not isinstance(events, list):
            raise IssueLedgerError("issue events must be an array")
        events.append(
            {"sequence": sequence, "type": str(event_type), **copy.deepcopy(fields)}
        )
        state["sequence"] = sequence
        state["events"] = events

    def _read_state(self) -> dict[str, object]:
        """读取并校验 Issue Ledger。"""
        if not self.path.is_file():
            return {
                "schema": ISSUE_LEDGER_SCHEMA,
                "sequence": 0,
                "issues": {},
                "events": [],
            }
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema") != ISSUE_LEDGER_SCHEMA:
            raise IssueLedgerError(f"unsupported issue ledger in {self.path}")
        _issue_records(value)
        return value

    def _write_state(self, state: Mapping[str, object]) -> None:
        """在同目录完整写入后原子替换 Issue Ledger。"""
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


class RevisionRouter:
    """把 open/unresolved issue 路由到现有 Owner 或 Orchestrator。"""

    def __init__(
        self,
        ledger: IssueLedger,
        registry: OwnerRegistry,
        *,
        artifacts: ArtifactStore | None = None,
        request_key: str = "",
    ) -> None:
        """绑定唯一 Ledger、Owner Registry 与可选图片 Catalog。"""
        self.ledger = ledger
        self.registry = registry
        self.artifacts = artifacts
        self.request_key = str(request_key)

    def route_issue(self, issue_id: str) -> RouteAction:
        """依据结构化 hint 和成员关系生成一次修订动作。"""
        issue = self.ledger.issue(issue_id)
        if issue.status in {
            IssueStatus.RESOLVED,
            IssueStatus.ACCEPTED_DEGRADATION,
            IssueStatus.REJECTED,
        }:
            return RouteAction(
                issue_id=issue.issue_id,
                target=RouteTarget.NONE,
                owner_id="",
                pages=issue.pages,
                activation_fingerprint="",
                reason=f"issue_status_{issue.status.value}",
            )
        target = (
            RouteTarget.ORCHESTRATOR
            if issue.status == IssueStatus.CANDIDATE
            else _target_for_issue(issue)
        )
        owner = self._find_owner(target, issue.pages)
        if owner is not None and owner.role == "orchestrator":
            target = RouteTarget.ORCHESTRATOR
        elif target in {RouteTarget.SLIDE, RouteTarget.IMAGE} and owner is None:
            target = RouteTarget.ORCHESTRATOR
        owner_id = owner.owner_id if owner is not None else (
            "orchestrator" if target == RouteTarget.ORCHESTRATOR else ""
        )
        catalog_hash = (
            str(self.artifacts.catalog().get("sha256") or "")
            if self.artifacts is not None and _uses_catalog(issue)
            else ""
        )
        page_versions = [
            (page.page_uid, page.html_sha256, page.png_sha256)
            for page in self.artifacts.workspace.pages()
            if not issue.pages or page.page in issue.pages
        ] if self.artifacts is not None else []
        positions = issue.pages
        if not positions and self.artifacts is not None:
            positions = tuple(page.page for page in self.artifacts.workspace.pages())
        input_paths = ["deck.md", "base.css", "knowledge.md", "assets/catalog.md",
                       *(f"briefs/slide_{page:02d}.md" for page in positions)]
        inputs = {path: file_sha256(self.registry.workspace / path) for path in input_paths}
        activation = hashlib.sha256(
            f"{issue.fingerprint}\0{target.value}\0{owner_id}\0{catalog_hash}\0{json.dumps(page_versions)}\0{self.request_key}\0{json.dumps(inputs, sort_keys=True)}".encode(
                "utf-8"
            )
        ).hexdigest()
        if issue.last_activation_fingerprint == activation:
            return RouteAction(
                issue_id=issue.issue_id,
                target=RouteTarget.NONE,
                owner_id="",
                pages=issue.pages,
                activation_fingerprint=activation,
                reason="no_new_artifact_or_catalog_evidence",
            )
        action = RouteAction(
            issue_id=issue.issue_id,
            target=target,
            owner_id=owner_id,
            pages=issue.pages,
            activation_fingerprint=activation,
            reason=(
                "candidate_requires_review"
                if issue.status == IssueStatus.CANDIDATE
                else "structured_issue_route"
            ),
        )
        if target != RouteTarget.NONE:
            self.ledger.record_route(action)
        return action

    def route_pending(self) -> tuple[RouteAction, ...]:
        """路由全部仍需行动的问题，并保留 no-change 决定。"""
        return tuple(
            self.route_issue(issue.issue_id)
            for issue in self.ledger.issues()
            if issue.status in {
                IssueStatus.CANDIDATE,
                IssueStatus.OPEN,
                IssueStatus.UNRESOLVED,
            }
        )

    def _find_owner(
        self, target: RouteTarget, pages: Sequence[int]
    ) -> OwnerRecord | None:
        """在现有逻辑 Owner 中选择覆盖问题页的对应角色。"""
        role = {
            RouteTarget.SLIDE: "slide",
            RouteTarget.IMAGE: "image",
        }.get(target)
        if role is None:
            return None
        candidates = [
            owner for owner in self.registry.list_current()
            if owner.role == role or (target == RouteTarget.SLIDE and pages and owner.role == "orchestrator")
        ]
        page_set = set(int(page) for page in pages)
        covering = [
            owner for owner in candidates if not page_set or page_set <= set(owner.pages)
        ]
        return covering[0] if len(covering) == 1 else None


def infer_owner_intent(events: Iterable[Mapping[str, object]]) -> OwnerIntent:
    """仅依据实际工具和终态事件识别 repair、ready 或 help。"""
    saw_mutation = False
    saw_terminal = False
    for event in events:
        event_type = str(event.get("event_type") or event.get("type") or "")
        tool_name = str(event.get("tool_name") or event.get("name") or "")
        if event_type == "owner_request" or tool_name == "request_orchestrator":
            return OwnerIntent.HELP
        if tool_name in {"artifact_commit", "render", "write_file", "patch"}:
            saw_mutation = True
        if event_type in {"task_completed", "wave_terminal"}:
            saw_terminal = True
    if saw_mutation:
        return OwnerIntent.REPAIR
    if saw_terminal:
        return OwnerIntent.READY
    return OwnerIntent.ACTIVE


def _issue_records(state: Mapping[str, object]) -> dict[str, IssueRecord]:
    """校验并解码 Ledger Issue 映射。"""
    raw = state.get("issues") or {}
    if not isinstance(raw, dict):
        raise IssueLedgerError("issues must be an object")
    records: dict[str, IssueRecord] = {}
    for issue_id, value in raw.items():
        if not isinstance(value, dict):
            raise IssueLedgerError("issue records must be objects")
        issue = IssueRecord.from_dict(value)
        if issue.issue_id != str(issue_id):
            raise IssueLedgerError("issue key does not match issue_id")
        records[str(issue_id)] = issue
    return records


def _is_current_visual_issue(issue: IssueRecord, png_sha256: str) -> bool:
    """Only visual/layout issues can be closed by a clean visual inspection."""
    source = issue.source.lower()
    kind = issue.kind.lower()
    if source in {"visual", "external_visual_critic"} or kind in {"external_visual_critic", "stale_png"}:
        hashes = _png_hashes(issue.evidence)
        return png_sha256 in hashes or (source == "external_visual_critic" and not hashes)
    if source == "orchestrator" and kind == "verification_issue" and _looks_visual_summary(issue.summary):
        return png_sha256 in _png_hashes(issue.evidence)
    return False


def _looks_visual_summary(summary: str) -> bool:
    text = str(summary or "").lower()
    keywords = (
        "安全区", "底边", "贴边", "边缘", "裁切", "遮挡", "溢出", "字号", "可读",
        "间距", "拥挤", "留白", "图表", "标签", "坐标", "图例", "线条",
        "safe area", "margin", "edge", "crop", "overflow", "readability",
        "spacing", "layout", "label", "legend", "axis",
    )
    return any(keyword in text for keyword in keywords)


def _png_hashes(value: object) -> set[str]:
    hashes: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "png_sha256" and isinstance(item, str) and _looks_sha256(item):
                hashes.add(item)
            else:
                hashes.update(_png_hashes(item))
        return hashes
    if isinstance(value, list):
        if len(value) >= 3 and isinstance(value[2], str) and _looks_sha256(value[2]):
            hashes.add(value[2])
        for item in value:
            hashes.update(_png_hashes(item))
    return hashes


def _looks_sha256(value: str) -> bool:
    text = str(value).strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _issue_fingerprint(
    *,
    source: str,
    kind: str,
    pages: Sequence[int],
    evidence: object,
    artifact_fingerprint: str,
) -> str:
    """由问题类型、页码和当前证据生成去重指纹。"""
    value = {
        "source": str(source),
        "kind": str(kind),
        "pages": sorted(set(int(page) for page in pages)),
        "evidence": copy.deepcopy(evidence),
        "artifact_fingerprint": str(artifact_fingerprint),
    }
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _visual_artifact_fingerprint(candidate: VisualCandidate) -> str:
    """汇总候选中全部当前 PNG 哈希。"""
    hashes = sorted({item.png_sha256 for item in candidate.evidence})
    return (
        hashlib.sha256("\0".join(hashes).encode("utf-8")).hexdigest()
        if hashes
        else candidate.candidate_id
    )


def _evidence_fingerprint(evidence: object) -> str:
    """为 Content evidence 生成稳定内容哈希。"""
    payload = json.dumps(evidence, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _target_for_issue(issue: IssueRecord) -> RouteTarget:
    """优先采用 Reviewer owner_hint，再处理少量确定性机械类别。"""
    hinted = {
        "slide": RouteTarget.SLIDE,
        "image": RouteTarget.IMAGE,
        "orchestrator": RouteTarget.ORCHESTRATOR,
        "none": RouteTarget.NONE,
    }.get(issue.owner_hint, RouteTarget.NONE)
    if hinted != RouteTarget.NONE:
        return hinted
    if issue.kind.startswith("image_"):
        return RouteTarget.IMAGE
    if issue.kind in {"cross_page_consistency", "shared_style_drift", "user_requirement"}:
        return RouteTarget.ORCHESTRATOR
    return RouteTarget.SLIDE if issue.pages else RouteTarget.ORCHESTRATOR


def _in_scope(issue: IssueRecord, scope: str) -> bool:
    """判断 issue 是否属于 deck、page 或显式 issue 范围。"""
    normalized = str(scope).strip().lower()
    if normalized in {"", "deck", "all"}:
        return True
    if normalized.startswith("page:"):
        try:
            return int(normalized.split(":", 1)[1]) in issue.pages
        except ValueError:
            return False
    if normalized.startswith("issue:"):
        return normalized.split(":", 1)[1] == issue.issue_id.lower()
    return False


def _uses_catalog(issue: IssueRecord) -> bool:
    """判断修订激活是否需要图片 Catalog 变化作为新证据。"""
    kind = issue.kind.lower()
    return issue.owner_hint == "image" or "image" in kind or "asset" in kind
