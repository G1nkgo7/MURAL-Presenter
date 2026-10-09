"""管理产物声明、Owner lease、提交哈希和 HTML/PNG 同源证明。

图片可用性来自实际文件与解码结果；页面完成度来自 artifact transaction、render 和
inspection 回执，不解析 handoff 文案中的 ready、failed 或 unavailable 说法。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import base64
import copy
import hashlib
from html.parser import HTMLParser
from io import BytesIO
import json
import os
from pathlib import Path
import re
import time
from typing import Callable, Mapping, Sequence
from urllib.parse import unquote, urlsplit
import uuid
import xml.etree.ElementTree as ET

from PIL import Image, UnidentifiedImageError

from runtime.shared_objects import workspace_skill_module

from .locking import page_render_lock
from .workspace import (
    PageSnapshot,
    WorkspaceState,
    WorkspaceStateError,
    file_sha256,
)


ARTIFACT_STATE_SCHEMA = "mural.artifact-state.v1"
ARTIFACT_KINDS = frozenset({"page_html", "image", "shared", "data"})


def _process_start_id(pid: int) -> str | None:
    """区分进程退出、同 PID 复用与无法确认；无法确认时不回收 lease。"""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()
        return fields[19]
    except FileNotFoundError:
        return ""
    except (OSError, IndexError):
        return None


class ArtifactStateError(RuntimeError):
    """表示产物声明、lease、哈希或资源加载合同不成立。"""

    def __init__(self, detail: str, *, diagnostics: Mapping[str, object] | None = None) -> None:
        self.diagnostics = diagnostics
        super().__init__(detail)


class UncommittedPageSourceError(ArtifactStateError):
    """源码未经过 Owner 写入事务，不能把骨架或未登记改动当作已制作页面。"""


class ArtifactStatus(str, Enum):
    """列出 Artifact Catalog 中的机械状态。"""

    DECLARED = "declared"
    COMMITTED = "committed"
    UNAVAILABLE = "unavailable"
    BROKEN = "broken"


@dataclass(frozen=True)
class ArtifactRecord:
    """保存一个 canonical 产物及其当前已提交版本。"""

    artifact_id: str
    kind: str
    path: str
    owner_id: str
    page: int | None
    status: ArtifactStatus
    revision: int = 0
    sha256: str = ""
    source_paths: tuple[str, ...] = ()
    source_hashes: Mapping[str, str] = field(default_factory=dict)
    source_url: str = ""
    reason: str = ""
    pixel_width: int | None = None
    pixel_height: int | None = None
    image_metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """规范 Artifact 字段并复制来源哈希。"""
        kind = str(self.kind).strip().lower()
        if kind not in ARTIFACT_KINDS:
            raise ValueError(f"unsupported artifact kind {kind!r}")
        if not str(self.artifact_id).strip() or not str(self.owner_id).strip():
            raise ValueError("artifact identity and owner must not be empty")
        if int(self.revision) < 0:
            raise ValueError("artifact revision must be non-negative")
        sources = tuple(
            dict.fromkeys(str(path).strip() for path in self.source_paths if str(path).strip())
        )
        hashes = self.source_hashes or {}
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "status", ArtifactStatus(self.status))
        object.__setattr__(self, "revision", int(self.revision))
        object.__setattr__(self, "source_paths", sources)
        if not isinstance(self.image_metadata, Mapping):
            raise ValueError("image_metadata must be an object")
        object.__setattr__(self, "image_metadata", copy.deepcopy(dict(self.image_metadata)))
        object.__setattr__(
            self,
            "source_hashes",
            dict(sorted((str(key), str(value)) for key, value in hashes.items())),
        )

    def to_dict(self) -> dict[str, object]:
        """把产物记录转换为持久 JSON 对象。"""
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "path": self.path,
            "owner_id": self.owner_id,
            "page": self.page,
            "status": self.status.value,
            "revision": self.revision,
            "sha256": self.sha256,
            "source_paths": list(self.source_paths),
            "source_hashes": dict(self.source_hashes),
            "source_url": self.source_url,
            "reason": self.reason,
            "pixel_width": self.pixel_width,
            "pixel_height": self.pixel_height,
            "image_metadata": copy.deepcopy(dict(self.image_metadata)),
            "aspect_ratio": self.pixel_width / self.pixel_height if self.pixel_width and self.pixel_height else None,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ArtifactRecord":
        """从 Catalog JSON 恢复一个产物记录。"""
        paths = value.get("source_paths") or []
        hashes = value.get("source_hashes") or {}
        if not isinstance(paths, list) or not isinstance(hashes, dict):
            raise ValueError("artifact source paths or hashes are invalid")
        raw_page = value.get("page")
        return cls(
            artifact_id=str(value.get("artifact_id") or ""),
            kind=str(value.get("kind") or ""),
            path=str(value.get("path") or ""),
            owner_id=str(value.get("owner_id") or ""),
            page=(int(raw_page) if raw_page is not None else None),
            status=ArtifactStatus(str(value.get("status") or "")),
            revision=int(value.get("revision") or 0),
            sha256=str(value.get("sha256") or ""),
            source_paths=tuple(str(item) for item in paths),
            source_hashes={str(key): str(item) for key, item in hashes.items()},
            source_url=str(value.get("source_url") or ""),
            reason=str(value.get("reason") or ""),
            pixel_width=value.get("pixel_width"),
            pixel_height=value.get("pixel_height"),
            image_metadata=value.get("image_metadata") or {},
        )


@dataclass(frozen=True)
class AssetValidation:
    """保存实际图片依赖的加载、登记与提交证据。"""

    page: int
    references: tuple[str, ...]
    missing: tuple[str, ...]
    broken: tuple[str, ...]
    external_unverified: tuple[str, ...]
    unregistered: tuple[str, ...] = ()
    uncommitted: tuple[str, ...] = ()
    stale: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        """判断页面是否没有确定性的图片加载问题。"""
        return not any((self.missing, self.broken, self.external_unverified,
                        self.unregistered, self.uncommitted, self.stale))

    def to_dict(self) -> dict[str, object]:
        """把资源验证结果转换为短 JSON 证据。"""
        return {
            "page": self.page,
            "references": list(self.references),
            "missing": list(self.missing),
            "broken": list(self.broken),
            "external_unverified": list(self.external_unverified),
            "unregistered": list(self.unregistered),
            "uncommitted": list(self.uncommitted),
            "stale": list(self.stale),
            "passed": self.passed,
        }


RenderBackend = Callable[[Path, Path, Mapping[str, object]], Mapping[str, object] | None]


class ArtifactStore:
    """协调 Artifact Catalog、Owner lease 和页面渲染回执。"""

    def __init__(
        self,
        workspace: str | Path,
        *,
        workspace_state: WorkspaceState | None = None,
        create: bool = True,
    ) -> None:
        """绑定唯一工作区状态，不创建默认画布。"""
        self.workspace = workspace_state or WorkspaceState(workspace)
        self.paths = self.workspace.paths
        self.path = self.paths.state / "artifacts.json"
        self._lock = self.workspace._lock
        with self._lock:
            state = self._read_state()
            if create and not self.path.exists():
                self._write_state(state)

    def _shared_font(self, reference: str) -> tuple[Path, str]:
        from runtime.shared_fonts import font_reference_resolver
        with self._lock:
            if self.workspace.font_resolver is None:
                self.workspace.font_resolver = font_reference_resolver(self.paths.root)
            resolve = self.workspace.font_resolver
        return resolve(reference)

    def declare(
        self,
        *,
        artifact_id: str,
        kind: str,
        path: str | Path,
        owner_id: str,
        page: int | None = None,
        source_paths: Sequence[str | Path] = (),
        replace_unavailable: bool = False,
    ) -> ArtifactRecord:
        """声明 canonical 产物、Owner 和已知来源，不写入完成状态。"""
        normalized_id = str(artifact_id).strip()
        normalized_kind = str(kind).strip().lower()
        normalized_path = self.paths.relative(path)
        normalized_owner = str(owner_id).strip()
        normalized_sources = tuple(self.paths.relative(item) for item in source_paths)
        if not normalized_id or not normalized_owner:
            raise ArtifactStateError("artifact_id and owner_id must not be empty")
        if normalized_kind == "page_html":
            if page is None:
                raise ArtifactStateError("page_html artifact requires page")
            canonical = self.paths.relative(self.paths.page_html(int(page)))
            if normalized_path != canonical:
                raise ArtifactStateError(
                    f"page_html must use canonical path {canonical!r}"
                )
        if normalized_kind == "image":
            asset_root = self.paths.assets.resolve()
            candidate = self.paths.resolve(normalized_path).resolve()
            if candidate != asset_root and asset_root not in candidate.parents:
                raise ArtifactStateError("image artifacts must live under assets/")
        record = ArtifactRecord(
            artifact_id=normalized_id,
            kind=normalized_kind,
            path=normalized_path,
            owner_id=normalized_owner,
            page=(int(page) if page is not None else None),
            status=ArtifactStatus.DECLARED,
            source_paths=normalized_sources,
            source_hashes={},
        )
        with self._lock:
            state = self._read_state()
            records = _artifact_records(state)
            if normalized_kind == "image":
                occupied = next((item for item in records.values()
                                 if item.kind == "image" and item.path == normalized_path
                                 and item.artifact_id != normalized_id), None)
                if occupied is not None:
                    raise ArtifactStateError(
                        f"image path {normalized_path!r} is already declared as {occupied.artifact_id!r} "
                        f"by owner {occupied.owner_id!r}; reuse that artifact_id"
                    )
            existing = records.get(normalized_id)
            if existing is not None:
                same_contract = (
                    existing.kind == record.kind
                    and existing.path == record.path
                    and existing.owner_id == record.owner_id
                    and existing.page == record.page
                    and existing.source_paths == record.source_paths
                )
                if not same_contract:
                    recoverable = (replace_unavailable and existing.kind == record.kind == "image"
                                   and existing.owner_id == record.owner_id
                                   and existing.status in {ArtifactStatus.DECLARED, ArtifactStatus.UNAVAILABLE}
                                   and existing.page == record.page and existing.source_paths == record.source_paths)
                    if not recoverable:
                        raise ArtifactStateError(
                            f"artifact {normalized_id!r} is already declared differently"
                        )
                    self._validate_transaction(state, normalized_owner, (normalized_id,), "")
                    _verify_image_file(self.paths.resolve(record.path))
                    record = replace(record, revision=existing.revision)
                    self._append_event(state, "image_path_rebound", artifact_id=normalized_id,
                                       previous=existing.to_dict(), path=record.path)
                else:
                    return existing
            records[normalized_id] = record
            state["artifacts"] = {
                key: item.to_dict() for key, item in sorted(records.items())
            }
            self._append_event(state, "artifact_declared", artifact_id=normalized_id)
            self._write_state(state)
            return record

    def begin_transaction(
        self, owner_id: str, artifact_ids: Sequence[str], *, external_process: bool = False
    ) -> str:
        """为一组产物取得互斥 lease，并返回交易 ID。"""
        owner = str(owner_id).strip()
        ids = tuple(dict.fromkeys(str(item).strip() for item in artifact_ids if str(item).strip()))
        if not ids:
            raise ArtifactStateError("artifact transaction must contain artifacts")
        with self._lock:
            state = self._read_state()
            records = _artifact_records(state)
            missing = sorted(set(ids) - set(records))
            if missing:
                raise ArtifactStateError(f"unknown artifact ids: {missing}")
            wrong_owner = [item for item in ids if records[item].owner_id != owner]
            if wrong_owner:
                raise ArtifactStateError(f"owner does not control artifacts: {wrong_owner}")
            self._release_exited_process_leases(state, ids)
            transactions = _transaction_records(state)
            occupied = {
                artifact_id
                for transaction in transactions.values()
                if transaction.get("status") == "open"
                for artifact_id in transaction.get("artifact_ids", [])
            }
            conflicts = sorted(set(ids) & occupied)
            if conflicts:
                external = any(tx.get("status") == "open" and set(conflicts).intersection(tx.get("artifact_ids", []))
                               and (tx.get("process") or {}).get("external_process") for tx in transactions.values())
                detail = ("; external image.py may still be writing. Wait for the active operation; after a forced stop, "
                          "confirm its subprocess has exited before an operator releases the recorded lease. "
                          "Do not use another artifact_id to bypass it." if external else "")
                raise ArtifactStateError(f"artifacts already leased: {conflicts}{detail}")
            transaction_id = f"artifact-tx-{uuid.uuid4().hex}"
            transactions[transaction_id] = {
                "transaction_id": transaction_id,
                "owner_id": owner,
                "artifact_ids": list(ids),
                "status": "open",
                "process": {"host": os.uname().nodename, "pid": os.getpid(), "start": _process_start_id(os.getpid()),
                            "external_process": external_process},
            }
            state["transactions"] = transactions
            self._append_event(
                state,
                "artifact_transaction_started",
                transaction_id=transaction_id,
                artifact_ids=list(ids),
            )
            self._write_state(state)
            return transaction_id

    def transfer_page_owner(
        self, page: int, *, owner_id: str, validate_only: bool = False
    ) -> ArtifactRecord | None:
        """把已声明页面产物转交给 Orchestration 已授权的新 Owner。"""
        number = int(page)
        target_owner = str(owner_id).strip()
        if number < 1 or not target_owner:
            raise ArtifactStateError(
                "page and owner_id are required for ownership transfer"
            )
        artifact_id = f"page-html-{number:02d}"
        with self._lock:
            state = self._read_state()
            records = _artifact_records(state)
            current = records.get(artifact_id)
            if current is None:
                return None
            if current.kind != "page_html" or current.page != number:
                raise ArtifactStateError(
                    f"artifact {artifact_id!r} is not the canonical page artifact"
                )
            if current.owner_id == target_owner:
                return current
            occupied = {
                item
                for transaction in _transaction_records(state).values()
                if transaction.get("status") == "open"
                for item in transaction.get("artifact_ids", [])
            }
            if artifact_id in occupied:
                raise ArtifactStateError(
                    f"cannot transfer leased artifact {artifact_id!r}"
                )
            if validate_only:
                return current
            updated = replace(current, owner_id=target_owner)
            records[artifact_id] = updated
            state["artifacts"] = {
                key: item.to_dict() for key, item in sorted(records.items())
            }
            self._append_event(
                state,
                "artifact_owner_transferred",
                artifact_id=artifact_id,
                previous_owner_id=current.owner_id,
                owner_id=target_owner,
            )
            self._write_state(state)
            return updated

    def transfer_image_owners(
        self, *, owner_id: str, previous_owner_ids: Sequence[str], artifact_ids: Sequence[str],
        validate_only: bool = False,
    ) -> None:
        """只交接已明确选定的素材；同角色的其他产物不转移。"""
        target_owner = str(owner_id).strip()
        if not target_owner:
            raise ArtifactStateError("image ownership transfer requires owner_id")
        predecessors = set(previous_owner_ids)
        with self._lock:
            state = self._read_state()
            records = _artifact_records(state)
            missing = set(artifact_ids) - set(records)
            if missing:
                raise ArtifactStateError(f"unknown image artifact_ids: {sorted(missing)}")
            if any(records[key].kind != "image" for key in artifact_ids):
                raise ArtifactStateError("image handoff only accepts image artifact_ids")
            changes = [records[key] for key in artifact_ids if records[key].owner_id != target_owner]
            occupied = {item for tx in _transaction_records(state).values()
                        if tx.get("status") == "open" for item in tx.get("artifact_ids", [])}
            for record in changes:
                if record.owner_id not in predecessors:
                    raise ArtifactStateError(
                        f"image artifact {record.artifact_id!r} has no terminal predecessor owner"
                    )
                if record.artifact_id in occupied:
                    raise ArtifactStateError(f"cannot transfer leased artifact {record.artifact_id!r}")
            if validate_only or not changes:
                return
            for record in changes:
                records[record.artifact_id] = replace(record, owner_id=target_owner)
                self._append_event(state, "artifact_owner_transferred",
                                   artifact_id=record.artifact_id,
                                   previous_owner_id=record.owner_id, owner_id=target_owner)
            state["artifacts"] = {key: item.to_dict() for key, item in sorted(records.items())}
            self._refresh_catalog_hash(state)
            self._write_state(state)

    def commit(
        self,
        *,
        owner_id: str,
        artifact_ids: Sequence[str],
        source_hashes: Mapping[str, object],
        transaction_id: str = "",
    ) -> tuple[ArtifactRecord, ...]:
        """校验 Owner、lease、文件和来源哈希后提交产物版本。"""
        owner = str(owner_id).strip()
        ids = tuple(dict.fromkeys(str(item).strip() for item in artifact_ids if str(item).strip()))
        expected = {str(key): str(value) for key, value in source_hashes.items()}
        with self._lock:
            state = self._read_state()
            records = _artifact_records(state)
            missing = sorted(set(ids) - set(records))
            if missing:
                raise ArtifactStateError(f"unknown artifact ids: {missing}")
            self._validate_transaction(state, owner, ids, transaction_id)
            updates: dict[str, ArtifactRecord] = {}
            for artifact_id in ids:
                record = records[artifact_id]
                if record.owner_id != owner:
                    raise ArtifactStateError(
                        f"owner {owner!r} may not commit {artifact_id!r}"
                    )
                path = self.paths.resolve(record.path)
                digest = file_sha256(path)
                if not digest:
                    raise ArtifactStateError(f"artifact file is missing: {record.path}")
                dimensions = _verify_image_file(path) if record.kind == "image" else None
                if artifact_id in expected and expected[artifact_id] != digest:
                    raise ArtifactStateError(
                        f"artifact hash changed before commit: {artifact_id}"
                    )
                if record.path in expected and expected[record.path] != digest:
                    raise ArtifactStateError(
                        f"artifact path hash changed before commit: {record.path}"
                    )
                actual_sources: dict[str, str] = {}
                for source_path in record.source_paths:
                    source_digest = file_sha256(self.paths.resolve(source_path))
                    if not source_digest:
                        raise ArtifactStateError(
                            f"artifact source is missing: {source_path}"
                        )
                    if expected.get(source_path) != source_digest:
                        raise ArtifactStateError(
                            f"artifact source hash mismatch: {source_path}"
                        )
                    actual_sources[source_path] = source_digest
                updates[artifact_id] = ArtifactRecord(
                    artifact_id=record.artifact_id,
                    kind=record.kind,
                    path=record.path,
                    owner_id=record.owner_id,
                    page=record.page,
                    status=ArtifactStatus.COMMITTED,
                    revision=record.revision + 1,
                    sha256=digest,
                    source_paths=record.source_paths,
                    source_hashes=actual_sources,
                    source_url=record.source_url,
                    pixel_width=dimensions[0] if dimensions else None,
                    pixel_height=dimensions[1] if dimensions else None,
                    image_metadata=record.image_metadata,
                )

            for record in updates.values():
                if record.kind == "page_html" and record.page is not None:
                    self.workspace.mark_authored(
                        record.page, html_sha256=record.sha256
                    )
            records.update(updates)
            state["artifacts"] = {
                key: item.to_dict() for key, item in sorted(records.items())
            }
            if transaction_id:
                transactions = _transaction_records(state)
                transaction = transactions[transaction_id]
                transaction["status"] = "committed"
                transactions[transaction_id] = transaction
                state["transactions"] = transactions
            self._refresh_catalog_hash(state)
            self._append_event(
                state,
                "artifacts_committed",
                artifact_ids=list(ids),
                transaction_id=str(transaction_id),
            )
            self._write_state(state)
            return tuple(updates[item] for item in ids)

    def abort_transaction(self, transaction_id: str, *, owner_id: str) -> None:
        """释放未提交 lease，并保留现有 canonical 文件供 Owner 决定后续处理。"""
        normalized = str(transaction_id).strip()
        with self._lock:
            state = self._read_state()
            transactions = _transaction_records(state)
            transaction = transactions.get(normalized)
            if transaction is None:
                raise ArtifactStateError(f"unknown transaction {normalized!r}")
            if transaction.get("owner_id") != str(owner_id).strip():
                raise ArtifactStateError("only the transaction owner may abort it")
            if transaction.get("status") != "open":
                raise ArtifactStateError("only an open transaction may be aborted")
            transaction["status"] = "aborted"
            transactions[normalized] = transaction
            state["transactions"] = transactions
            self._append_event(
                state, "artifact_transaction_aborted", transaction_id=normalized
            )
            self._write_state(state)

    def record_image_result(
        self,
        *,
        artifact_id: str,
        owner_id: str,
        path: str | Path,
        status: str,
        source_url: str = "",
        reason: str = "",
        image_metadata: Mapping[str, object] | None = None,
        transaction_id: str = "",
        reviewed_sha256: str = "",
    ) -> ArtifactRecord:
        """依据真实获取结果更新图片 Catalog，不读取自由文本 handoff。"""
        normalized_status = str(status).strip().lower()
        if normalized_status not in {"available", "unavailable", "pending"}:
            raise ArtifactStateError("image status must be available, unavailable or pending")
        if image_metadata is not None and not isinstance(image_metadata, Mapping):
            raise ArtifactStateError("image_metadata must be an object")
        if normalized_status == "unavailable" and not str(reason).strip():
            raise ArtifactStateError("unavailable image requires a mechanical reason")
        if reviewed_sha256 and normalized_status != "available":
            raise ArtifactStateError("only available images may carry a reviewed hash")
        record = self.declare(
            artifact_id=artifact_id,
            kind="image",
            path=path,
            owner_id=owner_id,
            replace_unavailable=normalized_status == "available",
        )
        if normalized_status == "available":
            with self._lock:
                self.commit(
                    owner_id=owner_id,
                    artifact_ids=[artifact_id],
                    source_hashes={record.path: reviewed_sha256} if reviewed_sha256 else {},
                    transaction_id=transaction_id,
                )
                state = self._read_state()
                records = _artifact_records(state)
                current = records[artifact_id]
                metadata = dict((current.image_metadata or {}) if image_metadata is None else image_metadata)
                metadata.pop("reviewed_sha256", None)
                metadata.pop("reviewed_by", None)
                if reviewed_sha256:
                    metadata["reviewed_sha256"] = reviewed_sha256
                    metadata["reviewed_by"] = owner_id
                updated = replace(current, source_url=str(source_url), reason="", image_metadata=metadata)
                records[artifact_id] = updated
                state["artifacts"] = {
                    key: item.to_dict() for key, item in sorted(records.items())
                }
                self._refresh_catalog_hash(state)
                self._write_state(state)
                return updated
        unavailable_reason = str(reason).strip()
        with self._lock:
            state = self._read_state()
            self._validate_transaction(state, owner_id, (artifact_id,), transaction_id)
            records = _artifact_records(state)
            current = records[record.artifact_id]
            updated = ArtifactRecord(
                artifact_id=current.artifact_id,
                kind=current.kind,
                path=current.path,
                owner_id=current.owner_id,
                page=current.page,
                status=ArtifactStatus.DECLARED if normalized_status == "pending" else ArtifactStatus.UNAVAILABLE,
                revision=current.revision + 1,
                source_paths=current.source_paths,
                source_hashes={},
                source_url=str(source_url),
                reason=unavailable_reason,
                image_metadata=current.image_metadata if image_metadata is None else image_metadata,
            )
            records[record.artifact_id] = updated
            state["artifacts"] = {
                key: item.to_dict() for key, item in sorted(records.items())
            }
            if transaction_id:
                state["transactions"][transaction_id]["status"] = "committed"
            self._refresh_catalog_hash(state)
            self._append_event(
                state,
                "image_pending" if normalized_status == "pending" else "image_unavailable",
                artifact_id=record.artifact_id,
                reason=unavailable_reason,
            )
            self._write_state(state)
            return updated

    def validate_html_assets(
        self,
        page: int,
        *,
        loaded_resources: Sequence[str] = (),
    ) -> AssetValidation:
        """复用渲染依赖闭包检查实际位图；临时、未引用文件不参与。"""
        number = int(page)
        html_path = self.paths.page_html(number)
        if not html_path.is_file():
            raise ArtifactStateError(f"page HTML is missing: {html_path}")
        parser = _ImageReferenceParser()
        parser.feed(html_path.read_text(encoding="utf-8"))
        references = tuple(dict.fromkeys(parser.references))
        missing: list[str] = []
        broken: list[str] = []
        external: list[str] = []
        image_paths: list[str] = []
        for reference in references:
            if reference.startswith("data:image/"):
                try:
                    _verify_data_image(reference)
                except ArtifactStateError:
                    broken.append(reference[:80])
                continue
            split = urlsplit(reference)
            if split.scheme in {"http", "https"}:
                if reference.split("#", 1)[0] not in loaded_resources:
                    external.append(reference)
                continue
            candidate = _resolve_html_resource(self.paths.root, html_path, reference)
            if candidate is None or not candidate.is_file():
                missing.append(reference)
                continue
            try:
                _verify_image_file(candidate)
            except ArtifactStateError:
                broken.append(reference)
            if candidate.suffix.lower() != ".svg":
                image_paths.append(self.paths.relative(candidate))
        registration = {key: () for key in ("unregistered", "uncommitted", "stale")}
        if not missing and not broken and not external:
            hashes = _html_resource_hashes(self.paths.root, html_path, loaded_resources=loaded_resources,
                                          resolve_shared_font=self._shared_font)
            registration = self._validate_registered_images(hashes, image_paths=image_paths)
            for relative in hashes:
                if Path(relative).suffix.lower() in _RASTER_SUFFIXES:
                    try:
                        _verify_image_file(self.paths.resolve(relative))
                    except ArtifactStateError:
                        broken.append(relative)
        return AssetValidation(
            page=number,
            references=references,
            missing=tuple(missing),
            broken=tuple(broken),
            external_unverified=tuple(external),
            **registration,
        )

    def _validate_registered_images(
        self, resource_hashes: Mapping[str, str], *, image_paths: Sequence[str] = (),
    ) -> dict[str, tuple[str, ...]]:
        with self._lock:
            images: dict[str, list[ArtifactRecord]] = {}
            for record in _artifact_records(self._read_state()).values():
                if record.kind == "image":
                    images.setdefault(record.path, []).append(record)
        invalid: dict[str, list[str]] = {key: [] for key in ("unregistered", "uncommitted", "stale")}
        for relative, digest in resource_hashes.items():
            if Path(relative).suffix.lower() not in _RASTER_SUFFIXES and relative not in image_paths and relative not in images:
                continue
            records = images.get(relative, [])
            committed = [record for record in records if record.status == ArtifactStatus.COMMITTED]
            if not records:
                invalid["unregistered"].append(relative)
            elif not committed:
                invalid["uncommitted"].append(relative)
            elif not any(record.sha256 == digest for record in committed):
                invalid["stale"].append(relative)
        return {key: tuple(sorted(values)) for key, values in invalid.items()}

    def render_pages(
        self,
        pages: Sequence[int],
        renderer: RenderBackend,
    ) -> tuple[PageSnapshot, ...]:
        """Serialize each page from cache lookup through capture and receipt commit."""
        pages = tuple(dict.fromkeys(int(item) for item in pages))
        for page in pages:
            self._authored_source_hash(page)
        snapshots: list[PageSnapshot] = []
        for page in pages:
            try:
                with page_render_lock(self.paths.root, page):
                    snapshots.extend(self._render_pages_locked((page,), renderer))
            except TimeoutError as exc:
                raise ArtifactStateError(
                    f"page {page} render transaction timed out; retry this page later",
                    diagnostics={"code": "render_transaction_timeout", "page": page,
                                 "recoverable": True},
                ) from exc
        return tuple(snapshots)

    def _render_pages_locked(
        self,
        pages: Sequence[int],
        renderer: RenderBackend,
    ) -> tuple[PageSnapshot, ...]:
        """把唯一 Mapping[str, object] 传给 Renderer，并记录每页同源哈希。"""
        pages = tuple(dict.fromkeys(int(item) for item in pages))
        # 整批预检先于任何渲染；每页执行前再核对，防止等待期间源码发生变化。
        for page in pages:
            self._authored_source_hash(page)
        canvas = self.workspace.require_geometry()
        snapshots: list[PageSnapshot] = []
        for page in pages:
            html_path = self.paths.page_html(page)
            png_path = self.paths.page_png(page)
            before_sha256 = self._authored_source_hash(page)
            presentation_config = self.workspace.presentation_config()
            with self._lock:
                try:
                    self.validate_render_receipt(page)
                    cache_check = getattr(renderer, "cache_receipt_valid", None)
                    if cache_check is not None and not cache_check(self.render_receipt(page)):
                        raise ArtifactStateError(f"page {page} cached renderer evidence is incomplete")
                except ArtifactStateError:
                    pass
                else:
                    current = self.workspace.page(page)
                    snapshots.append(self.workspace.mark_rendered(page,
                        html_sha256=current.html_sha256, png_sha256=current.png_sha256))
                    continue
            receipt = renderer(html_path, png_path, canvas) or {}
            if not isinstance(receipt, Mapping):
                raise ArtifactStateError("renderer receipt must be an object")
            if receipt.get("geometry") != canvas:
                raise ArtifactStateError(f"page {page} DOM canvas mismatch: expected {canvas}, actual {receipt.get('geometry')}")
            after_sha256 = file_sha256(html_path)
            normalization = receipt.get("canonical_html_normalization")
            if after_sha256 == before_sha256 and normalization is not None:
                raise ArtifactStateError(
                    f"page {page} renderer reported a normalization without changing HTML"
                )
            failed = receipt.get("failed_resources") or []
            loaded = receipt.get("loaded_resources") or []
            if not isinstance(failed, list) or not isinstance(loaded, list):
                raise ArtifactStateError("renderer resource receipts must be arrays")
            if failed:
                raise ArtifactStateError(
                    f"renderer failed resources for page {page}: {failed}"
                )
            resource_started = time.monotonic()
            validation = self.validate_html_assets(
                page, loaded_resources=tuple(str(item) for item in loaded)
            )
            if not validation.passed:
                raise ArtifactStateError(
                    f"page {page} contains broken image resources: {validation.to_dict()}"
                )
            resource_hashes = _html_resource_hashes(
                self.paths.root,
                html_path,
                loaded_resources=tuple(str(item) for item in loaded),
                resolve_shared_font=self._shared_font,
            )
            resource_checked = time.monotonic()
            input_hashes = receipt.get("input_hashes", {})
            current_inputs = {**resource_hashes, self.paths.relative(html_path): after_sha256}
            if not isinstance(input_hashes, Mapping) or any(
                not digest or (urlsplit(path).scheme not in {"http", "https"}
                               and current_inputs.get(path) != digest) for path, digest in input_hashes.items()
            ):
                raise ArtifactStateError(f"page {page} render inputs changed after capture; inspect the current version")
            png_sha256 = file_sha256(png_path)
            if "png_sha256" in receipt and receipt["png_sha256"] != png_sha256:
                raise ArtifactStateError(f"page {page} render pixels changed after capture")
            _verify_image_file(png_path)
            try:
                workspace_skill_module(self.paths.root, "geometry").validate_png(png_path, canvas)
            except ValueError as exc:
                raise ArtifactStateError(f"page {page}: {exc}") from exc
            if after_sha256 != before_sha256:
                self._commit_renderer_normalization(
                    page, normalization, before_sha256=before_sha256, after_sha256=after_sha256)
            commit_started = time.monotonic()
            with self._lock:
                previous_receipt = copy.deepcopy(
                    _render_receipts(self._read_state()).get(str(page), {})
                )
            render_inputs_unchanged = bool(previous_receipt) and (
                previous_receipt.get("html_sha256") == after_sha256
                and previous_receipt.get("presentation_config") == presentation_config
                and previous_receipt.get("canvas") == canvas
                and previous_receipt.get("input_hashes") == dict(input_hashes)
                and previous_receipt.get("resource_hashes") == resource_hashes
            )
            equivalent_rerender = bool(
                render_inputs_unchanged
                and previous_receipt.get("png_sha256")
                and previous_receipt.get("png_sha256") != png_sha256
            )
            snapshots.append(
                self.workspace.mark_rendered(
                    page,
                    html_sha256=after_sha256,
                    png_sha256=png_sha256,
                    presentation_config=presentation_config,
                    preserve_equivalent_inspection=equivalent_rerender,
                )
            )
            committed = time.monotonic()
            with self._lock:
                state = self._read_state()
                receipts = _render_receipts(state)
                receipts[str(page)] = {
                    "page": page,
                    "html_sha256": after_sha256,
                    "png_sha256": png_sha256,
                    "input_hashes": dict(input_hashes),
                    "loaded_resources": [str(item) for item in loaded],
                    "failed_resources": [],
                    "resource_hashes": resource_hashes,
                    "diagnostics": {**copy.deepcopy(receipt.get("diagnostics", {})),
                                    "artifact_timings_s": {"resource_validation": resource_checked - resource_started,
                                                           "png_validation": commit_started - resource_checked,
                                                           "workspace_commit": committed - commit_started}},
                    "canonical_html_normalization": (
                        copy.deepcopy(dict(normalization))
                        if isinstance(normalization, Mapping)
                        else None
                    ),
                    "canvas": canvas,
                    "presentation_config": presentation_config,
                    "render_inputs_unchanged": render_inputs_unchanged,
                    "pixel_unchanged": bool(previous_receipt.get("png_sha256")) and (
                        previous_receipt["png_sha256"] == png_sha256
                    ),
                    "pixel_hash_changed_with_same_inputs": equivalent_rerender,
                    "preserved_inspection_on_equivalent_rerender": equivalent_rerender,
                }
                state["render_receipts"] = receipts
                self._append_event(state, "page_render_receipt", page=page)
                self._write_state(state)
        return tuple(snapshots)

    def _authored_source_hash(self, page: int) -> str:
        path = self.paths.page_html(page)
        digest = file_sha256(path)
        if not digest or self.workspace.page(page).authored_html_sha256 != digest:
            raise UncommittedPageSourceError(
                f"page {page}: {path.relative_to(self.paths.root)} has no registered current authored source. "
                "Use write_file or patch to author this HTML before inspect; "
                "a contact sheet cannot author an automatically prepared empty page. No Git commit is required."
            )
        return digest

    def _commit_renderer_normalization(
        self,
        page: int,
        normalization: object,
        *,
        before_sha256: str,
        after_sha256: str,
    ) -> None:
        """仅接受 Renderer 显式证明的 canonical HTML 确定性归一化。"""
        if not isinstance(normalization, Mapping):
            raise ArtifactStateError(
                f"page {page} renderer changed committed HTML without a normalization receipt"
            )
        if (
            str(normalization.get("before_sha256") or "") != before_sha256
            or str(normalization.get("after_sha256") or "") != after_sha256
        ):
            raise ArtifactStateError(
                f"page {page} renderer normalization hashes do not match canonical HTML"
            )
        artifact_id = f"page-html-{page:02d}"
        try:
            record = self.artifact(artifact_id)
        except KeyError as exc:
            raise ArtifactStateError(
                f"page {page} normalization requires a declared page artifact"
            ) from exc
        if (
            record.kind != "page_html"
            or record.page != page
            or record.status != ArtifactStatus.COMMITTED
            or record.sha256 != before_sha256
        ):
            raise ArtifactStateError(
                f"page {page} normalization source does not match its committed artifact"
            )
        self.commit(
            owner_id=record.owner_id,
            artifact_ids=[artifact_id],
            source_hashes=record.source_hashes,
        )

    def record_inspection(
        self, page: int, *, inspection_id: str,
        expected: Mapping[str, object] | None = None,
        receipt: Mapping[str, object] | None = None,
    ) -> PageSnapshot:
        """把视觉分析回执绑定到当前页面哈希对。"""
        return self.workspace.mark_inspected(page, inspection_id=inspection_id,
                                             expected=expected, receipt=receipt)

    def artifact(self, artifact_id: str) -> ArtifactRecord:
        """读取一个 Artifact Catalog 记录。"""
        with self._lock:
            record = _artifact_records(self._read_state()).get(str(artifact_id))
            if record is None:
                raise KeyError(artifact_id)
            return record

    def render_receipt(self, page: int) -> dict[str, object]:
        """读取指定页最后一次成功 Renderer 资源回执。"""
        with self._lock:
            receipt = _render_receipts(self._read_state()).get(str(int(page)))
            return copy.deepcopy(receipt) if receipt is not None else {}

    def validate_render_receipt(
        self, page: int, *, _canvas: Mapping[str, object] | None = None,
    ) -> None:
        """确认页面、PNG、Canvas 与全部本地视觉资源仍和 render 同源。"""
        receipt = self.render_receipt(page)
        if not receipt:
            raise ArtifactStateError(f"page {page} has no successful render receipt",
                diagnostics={"code": "render_receipt_missing"})
        config = self.workspace.presentation_config()
        if receipt.get("presentation_config") != config:
            raise ArtifactStateError(f"page {page} presentation language changed after render; inspect the current page",
                diagnostics={"code": "presentation_language_changed",
                    "before": (receipt.get("presentation_config") or {}).get("presentation_language"),
                    "after": config["presentation_language"]})
        snapshot = self.workspace.page(page)
        if (
            receipt.get("html_sha256") != snapshot.html_sha256
            or receipt.get("png_sha256") != snapshot.png_sha256
        ):
            raise ArtifactStateError(f"page {page} render receipt is stale",
                diagnostics={"code": "render_receipt_stale"})
        expected = self.workspace.require_geometry() if _canvas is None else _canvas
        if receipt.get("canvas") != expected:
            raise ArtifactStateError(f"page {page} canvas mismatch: expected {expected}, actual {receipt.get('canvas')}",
                diagnostics={"code": "canvas_changed", "before": receipt.get("canvas"), "after": expected})
        hashes = receipt.get("resource_hashes") or {}
        if not isinstance(hashes, dict):
            raise ArtifactStateError(f"page {page} render resource hashes are invalid",
                diagnostics={"code": "render_resources_invalid"})
        current = _html_resource_hashes(
            self.paths.root, self.paths.page_html(page),
            loaded_resources=tuple(str(item) for item in receipt.get("loaded_resources", [])),
            resolve_shared_font=self._shared_font,
        )
        changed = [key for key in hashes.keys() | current.keys()
                   if hashes.get(key) != current.get(key)]
        if changed:
            raise ArtifactStateError(
                f"page {page} visual resources changed after render: {sorted(changed)}",
                diagnostics={"code": "resource_changed", "changed_resources": sorted(changed)},
            )
        registration = self._validate_registered_images(current)
        if any(registration.values()):
            raise ArtifactStateError(f"page {page} image registrations invalid: {registration}",
                diagnostics={"code": "image_registration_invalid", "registration": registration})

    def catalog(self) -> dict[str, object]:
        """返回图片 Catalog 及其稳定内容哈希。"""
        with self._lock:
            state = self._read_state()
            records = _artifact_records(state)
            images = [
                records[key].to_dict()
                for key in sorted(records)
                if records[key].kind == "image"
            ]
            return {
                "schema": "mural.image-catalog.v1",
                "sha256": str(state.get("catalog_sha256") or ""),
                "images": images,
            }

    def _validate_transaction(
        self,
        state: Mapping[str, object],
        owner_id: str,
        artifact_ids: Sequence[str],
        transaction_id: str,
    ) -> None:
        """校验显式交易，或确认这些产物当前没有被 lease。"""
        self._release_exited_process_leases(state, artifact_ids)
        transactions = _transaction_records(state)
        open_transactions = {
            key: value
            for key, value in transactions.items()
            if value.get("status") == "open"
        }
        if transaction_id:
            transaction = open_transactions.get(str(transaction_id))
            if transaction is None:
                raise ArtifactStateError("artifact transaction is missing or not open")
            if transaction.get("owner_id") != owner_id:
                raise ArtifactStateError("artifact transaction owner does not match")
            if set(transaction.get("artifact_ids") or []) != set(artifact_ids):
                raise ArtifactStateError("artifact transaction membership does not match")
            return
        occupied = {
            artifact_id
            for transaction in open_transactions.values()
            for artifact_id in transaction.get("artifact_ids", [])
        }
        conflicts = sorted(set(artifact_ids) & occupied)
        if conflicts:
            raise ArtifactStateError(
                f"leased artifacts require their transaction_id: {conflicts}"
            )

    def _release_exited_process_leases(self, state: dict[str, object], artifact_ids: Sequence[str]) -> None:
        changed = False
        for transaction_id, transaction in _transaction_records(state).items():
            process = transaction.get("process") or {}
            if (transaction.get("status") != "open" or not set(artifact_ids).intersection(transaction.get("artifact_ids", []))
                    or process.get("host") != os.uname().nodename or type(process.get("pid")) is not int
                    or process["pid"] < 1 or not process.get("start") or process.get("external_process")):
                continue
            actual = _process_start_id(process["pid"])
            if actual is not None and actual != process["start"]:
                transaction["status"] = "aborted"
                self._append_event(state, "artifact_transaction_aborted", transaction_id=transaction_id,
                    reason="lease_process_exited_or_replaced")
                changed = True
        if changed:
            self._write_state(state)

    def _refresh_catalog_hash(self, state: dict[str, object]) -> None:
        """根据图片记录的机械字段重算 Catalog 内容哈希。"""
        records = _artifact_records(state)
        rows = [
            records[key].to_dict()
            for key in sorted(records)
            if records[key].kind == "image"
        ]
        payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        state["catalog_sha256"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _append_event(
        self, state: dict[str, object], event_type: str, **fields: object
    ) -> None:
        """追加不依赖自由文本解释的 Artifact 事件。"""
        sequence = int(state.get("sequence") or 0) + 1
        events = state.get("events") or []
        if not isinstance(events, list):
            raise ArtifactStateError("artifact events must be an array")
        events.append(
            {"sequence": sequence, "type": str(event_type), **copy.deepcopy(fields)}
        )
        state["sequence"] = sequence
        state["events"] = events

    def _read_state(self) -> dict[str, object]:
        """读取并校验 Artifact Catalog 状态。"""
        if not self.path.is_file():
            return {
                "schema": ARTIFACT_STATE_SCHEMA,
                "sequence": 0,
                "catalog_sha256": hashlib.sha256(b"[]").hexdigest(),
                "artifacts": {},
                "transactions": {},
                "render_receipts": {},
                "events": [],
            }
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema") != ARTIFACT_STATE_SCHEMA:
            raise ArtifactStateError(f"unsupported artifact state in {self.path}")
        _artifact_records(value)
        _transaction_records(value)
        _render_receipts(value)
        return value

    def _write_state(self, state: Mapping[str, object]) -> None:
        """在同目录完整写入后原子替换 Artifact 状态。"""
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


class _ImageReferenceParser(HTMLParser):
    """收集 HTML 中会产生图片像素的资源引用。"""

    def __init__(self) -> None:
        """初始化引用列表并保留 HTMLParser 默认行为。"""
        super().__init__(convert_charrefs=True)
        self.references: list[str] = []
        self.resources: list[str] = []
        self._in_style = False

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        """从 img、source、object 与 SVG image 标签提取资源地址。"""
        values = {str(key).lower(): value for key, value in attrs}
        normalized_tag = str(tag).lower()
        if normalized_tag == "style":
            self._in_style = True
        if normalized_tag == "script" and values.get("src"):
            self.resources.append(str(values["src"]).strip())
        if normalized_tag == "use":
            reference = str(values.get("href") or values.get("xlink:href") or "").strip()
            if reference and not reference.startswith("#"):
                self.resources.append(reference)
        if normalized_tag in {"img", "image", "object"}:
            for key in ("src", "href", "xlink:href", "data"):
                value = values.get(key)
                if value:
                    reference = value.strip()
                    self.references.append(reference)
                    self.resources.append(reference)
                    break
        if normalized_tag in {"img", "source"} and values.get("srcset"):
            references = _srcset_references(str(values["srcset"]))
            self.references.extend(references)
            self.resources.extend(references)
        if normalized_tag == "link" and "stylesheet" in str(values.get("rel") or "").lower():
            href = str(values.get("href") or "").strip()
            if href:
                self.resources.append(href)
        style = values.get("style") or ""
        style_references = _css_url_references(style)
        self.references.extend(style_references)
        self.resources.extend(style_references)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "style":
            self._in_style = False

    def handle_data(self, data: str) -> None:
        if self._in_style:
            self.resources.extend(_css_url_references(data))


def _artifact_records(state: Mapping[str, object]) -> dict[str, ArtifactRecord]:
    """校验并解码 Artifact 映射。"""
    raw = state.get("artifacts") or {}
    if not isinstance(raw, dict):
        raise ArtifactStateError("artifacts must be an object")
    result: dict[str, ArtifactRecord] = {}
    for artifact_id, value in raw.items():
        if not isinstance(value, dict):
            raise ArtifactStateError("artifact records must be objects")
        record = ArtifactRecord.from_dict(value)
        if record.artifact_id != str(artifact_id):
            raise ArtifactStateError("artifact key does not match artifact_id")
        result[str(artifact_id)] = record
    return result


def _transaction_records(
    state: Mapping[str, object]
) -> dict[str, dict[str, object]]:
    """校验并返回 Artifact transaction 映射。"""
    raw = state.get("transactions") or {}
    if not isinstance(raw, dict) or not all(isinstance(item, dict) for item in raw.values()):
        raise ArtifactStateError("artifact transactions must be an object of objects")
    return raw


def _render_receipts(state: Mapping[str, object]) -> dict[str, dict[str, object]]:
    """校验并返回 Renderer 资源回执映射。"""
    raw = state.get("render_receipts") or {}
    if not isinstance(raw, dict) or not all(isinstance(item, dict) for item in raw.values()):
        raise ArtifactStateError("render receipts must be an object of objects")
    return raw


def _verify_image_file(path: Path) -> tuple[int, int] | None:
    """确认本地图片存在且能被对应解码器完整解析。"""
    if not path.is_file() or path.stat().st_size == 0:
        raise ArtifactStateError(f"image file is missing or empty: {path}")
    if path.suffix.lower() == ".svg":
        try:
            ET.parse(path)
        except (ET.ParseError, OSError) as exc:
            raise ArtifactStateError(f"invalid SVG image: {path}") from exc
        return
    try:
        with Image.open(path) as image:
            image.verify()
            return image.size
    except (OSError, UnidentifiedImageError) as exc:
        raise ArtifactStateError(f"image cannot be decoded: {path}") from exc


def _verify_data_image(reference: str) -> None:
    """解码并验证 ``data:image`` 资源。"""
    header, separator, payload = reference.partition(",")
    if not separator or ";base64" not in header:
        raise ArtifactStateError("image data URL must use base64")
    try:
        raw = base64.b64decode(payload, validate=True)
        with Image.open(BytesIO(raw)) as image:
            image.verify()
    except (ValueError, OSError, UnidentifiedImageError) as exc:
        raise ArtifactStateError("image data URL cannot be decoded") from exc


def _resolve_html_resource(
    workspace: Path, html_path: Path, reference: str
) -> Path | None:
    """按实际渲染 base 解析本地资源引用，并拒绝工作区逃逸。

    真实渲染加载的是 workspace 根的 ``present.html``(base=workspace 根),而非
    ``slides/slide_NN.html`` 片段本身;``deck_core._normalize_fragment_resource_paths``
    也把片段里的 ``../assets/`` 归一为根相对的 ``assets/``。因此校验器必须与真实 render
    base 一致:``assets/x`` 与 ``../assets/x``(片段自然写法)都按 workspace 根解析,否则
    会把根 ``assets/`` 误判为 missing,让每个带图页面 render 失败。leading ``../`` 前缀
    对 ``assets/`` 与 ``fonts/`` 视作根相对(与 normalize 一致);其余越界路径仍拒绝。
    """
    split = urlsplit(reference)
    if split.scheme or split.netloc:
        return None
    raw_path = unquote(split.path)
    if not raw_path:
        return None
    # 收敛片段自然写法:开头一个或多个 ../ 后接 assets/ 或 fonts/ = 根相对。
    file_relative = html_path.suffix.lower() in {".css", ".svg"} and not raw_path.startswith("/")
    normalized = raw_path if file_relative else re.sub(
        r"^(?:\.\./)+(assets|fonts)/", r"\1/", raw_path.lstrip("/"))
    if not file_relative and normalized == "../base.css":
        normalized = "base.css"
    base = html_path.parent if file_relative else workspace
    candidate = (base / normalized).resolve()
    root = workspace.resolve()
    if candidate != root and root not in candidate.parents:
        return None
    return candidate


def _srcset_references(value: str) -> list[str]:
    """从 srcset 的每个候选中提取 URL。"""
    return [
        candidate.strip().split()[0]
        for candidate in str(value).split(",")
        if candidate.strip()
    ]


def _css_url_references(value: str) -> list[str]:
    """提取 CSS 的资源和 stylesheet import。"""
    references = [
        match.strip().strip("\"'")
        for match in re.findall(r"url\(([^)]+)\)", str(value), flags=re.IGNORECASE)
        if match.strip()
    ]
    references.extend(re.findall(r"@import\s+['\"]([^'\"]+)['\"]", str(value), re.IGNORECASE))
    return [value for value in references if not value.startswith("#")]


_RASTER_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif", ".ico"})


def _html_resource_hashes(
    workspace: Path,
    html_path: Path,
    *,
    loaded_resources: Sequence[str],
    resolve_shared_font: Callable[[str], tuple[Path, str]] | None = None,
) -> dict[str, str]:
    """递归记录 HTML 与 CSS 引用的本地视觉资源哈希。"""
    parser = _ImageReferenceParser()
    parser.feed(html_path.read_text(encoding="utf-8"))
    loaded = {str(item) for item in loaded_resources}
    queue: list[tuple[Path, str]] = [
        (html_path, reference) for reference in {*parser.resources, *loaded}
    ]
    if (workspace / "base.css").is_file():
        queue.append((html_path, "/base.css"))
    hashes: dict[str, str] = {}
    visited: set[Path] = set()
    while queue:
        base_path, reference = queue.pop(0)
        if reference.startswith(("data:", "blob:")):
            continue
        split = urlsplit(reference)
        if split.scheme in {"http", "https"}:
            if not split.hostname or split.username is not None or split.password is not None:
                raise ArtifactStateError(f"invalid remote resource URL: {reference}")
            continue  # 实际加载状态及可取得的响应 hash 来自 Renderer；不为复核额外发网络请求。
        if split.scheme == "file":
            if reference in hashes:
                continue
            from runtime.shared_fonts import SharedFontError, font_reference_resolver

            try:
                if resolve_shared_font is None:
                    resolve_shared_font = font_reference_resolver(workspace)
                _, expected = resolve_shared_font(reference)
            except SharedFontError as exc:
                raise ArtifactStateError(f"invalid shared font reference: {exc}") from exc
            hashes[reference] = expected
            continue
        candidate = _resolve_html_resource(workspace, base_path, reference)
        if candidate is None or not candidate.is_file():
            raise ArtifactStateError(f"visual resource is missing: {reference}")
        resolved = candidate.resolve()
        if resolved in visited:
            continue
        visited.add(resolved)
        relative = resolved.relative_to(workspace.resolve()).as_posix()
        hashes[relative] = file_sha256(resolved)
        if resolved.suffix.lower() == ".css":
            for nested in _css_url_references(resolved.read_text(encoding="utf-8")):
                queue.append((resolved, nested))
        elif resolved.suffix.lower() == ".svg":
            nested_parser = _ImageReferenceParser()
            nested_parser.feed(resolved.read_text(encoding="utf-8"))
            queue.extend((resolved, nested) for nested in nested_parser.resources)
    return dict(sorted(hashes.items()))
