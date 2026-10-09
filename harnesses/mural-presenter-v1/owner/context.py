"""编译短 OwnerPacket，并在闭合事务边界保存可恢复上下文。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import copy
import hashlib
import json
import os
from pathlib import Path
import threading
from typing import Any, Mapping, Sequence
import uuid

from runtime.config import RunSpec
from runtime.recovery import (
    SegmentCheckpoint,
    replayable_prefix,
    tool_boundary,
)
from runtime.vision import image_blocks
from runtime.raw_trace import checkpoint_images

from .registry import OwnerRecord


OWNER_CONTEXT_SCHEMA = "mural.owner-context.v1"


def source_policy_text(authoring_profile: object) -> str:
    """Return model-facing source guidance without exposing internal run modes."""
    return (
        "按用户请求、附件和来源边界选择材料；未明确封闭时，可为必要事实核验和素材获取使用外部来源。"
    )


class ResumeMode(str, Enum):
    """区分有效会话续接、不可重放上下文的重建和全新 Owner 启动。"""

    RESUME = "resume"
    REBUILD = "checkpoint_rebuild"
    COLD = "cold"


@dataclass(frozen=True)
class OwnerPacket:
    """保存 Owner 开始工作所需的短输入合同。"""

    owner_id: str
    role: str
    pages: tuple[int, ...]
    group_id: str
    attempt: int
    dependencies: tuple[str, ...]
    raw_user_query: str
    attachment_paths: tuple[str, ...]
    artifact_refs: tuple[Mapping[str, object], ...] = ()
    prior_capsule: str = ""
    task_id: str = ""
    goal: str = ""
    authoring_profile: str = "general"
    creation_config: Mapping[str, object] = field(default_factory=dict)
    repair: bool = False
    handoffs: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """深复制嵌套字段，防止调用方在投递后修改合同。"""
        object.__setattr__(self, "pages", tuple(int(page) for page in self.pages))
        object.__setattr__(
            self,
            "dependencies",
            tuple(str(item) for item in self.dependencies),
        )
        object.__setattr__(
            self,
            "attachment_paths",
            tuple(str(path) for path in self.attachment_paths),
        )
        object.__setattr__(
            self,
            "artifact_refs",
            tuple(copy.deepcopy(dict(item)) for item in self.artifact_refs),
        )
        object.__setattr__(
            self,
            "handoffs",
            copy.deepcopy(dict(self.handoffs or {})),
        )
        object.__setattr__(self, "creation_config", copy.deepcopy(dict(self.creation_config)))
        if not self.owner_id or not self.group_id or not self.raw_user_query.strip():
            raise ValueError("OwnerPacket identity and raw query must not be empty")

    @classmethod
    def compile(
        cls,
        record: OwnerRecord,
        run_spec: RunSpec,
        *,
        artifact_refs: Sequence[Mapping[str, object]] = (),
        prior_capsule: str = "",
        task_id: str = "",
        goal: str = "",
        repair: bool = False,
        handoffs: Mapping[str, object] | None = None,
        attachment_paths: Sequence[str] | None = None,
    ) -> "OwnerPacket":
        """由显式运行输入和产物引用编译 OwnerPacket。"""
        from state.workspace import WorkspaceState

        return cls(
            owner_id=record.owner_id,
            role=record.role,
            pages=record.pages,
            group_id=record.group_id,
            attempt=record.attempt,
            dependencies=record.dependencies,
            raw_user_query=run_spec.raw_user_query,
            attachment_paths=(
                tuple(str(path) for path in attachment_paths)
                if attachment_paths is not None
                else tuple(str(path) for path in run_spec.attachments)
            ),
            artifact_refs=tuple(artifact_refs),
            prior_capsule=str(prior_capsule),
            task_id=str(task_id),
            goal=str(goal),
            authoring_profile=run_spec.authoring_profile,
            creation_config=WorkspaceState(run_spec.output_workspace).creation_config(),
            repair=bool(repair),
            handoffs=dict(handoffs or {}),
        )

    def to_dict(self) -> dict[str, object]:
        """把短输入合同转换为 JSON 对象。"""
        return {
            "owner_id": self.owner_id,
            "role": self.role,
            "pages": list(self.pages),
            "group_id": self.group_id,
            "attempt": self.attempt,
            "dependencies": list(self.dependencies),
            "raw_user_query": self.raw_user_query,
            "attachment_paths": list(self.attachment_paths),
            "artifact_refs": [
                copy.deepcopy(dict(item)) for item in self.artifact_refs
            ],
            "prior_capsule": self.prior_capsule,
            "task_id": self.task_id,
            "goal": self.goal,
            "source_policy": source_policy_text(self.authoring_profile),
            "creation_config": copy.deepcopy(dict(self.creation_config)),
            "repair": self.repair,
            "handoffs": copy.deepcopy(dict(self.handoffs)),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "OwnerPacket":
        """从持久 JSON 恢复 OwnerPacket。"""
        pages = value.get("pages") or []
        dependencies = value.get("dependencies") or []
        attachments = value.get("attachment_paths") or []
        artifacts = value.get("artifact_refs") or []
        handoffs = value.get("handoffs") or {}
        if not all(isinstance(item, list) for item in (pages, dependencies, attachments)):
            raise ValueError("OwnerPacket array fields are invalid")
        if (
            not isinstance(artifacts, list)
            or not isinstance(handoffs, dict)
        ):
            raise ValueError("OwnerPacket artifact_refs or handoffs are invalid")
        if not all(isinstance(item, dict) for item in artifacts):
            raise ValueError("OwnerPacket artifact_refs must contain objects")
        return cls(
            owner_id=str(value.get("owner_id") or ""),
            role=str(value.get("role") or ""),
            pages=tuple(int(page) for page in pages),
            group_id=str(value.get("group_id") or ""),
            attempt=int(value.get("attempt") or 0),
            dependencies=tuple(str(item) for item in dependencies),
            raw_user_query=str(value.get("raw_user_query") or ""),
            attachment_paths=tuple(str(item) for item in attachments),
            artifact_refs=tuple(artifacts),
            prior_capsule=str(value.get("prior_capsule") or ""),
            task_id=str(value.get("task_id") or ""),
            goal=str(value.get("goal") or ""),
            authoring_profile=str(value.get("authoring_profile") or "general"),
            creation_config=value["creation_config"],
            repair=bool(value.get("repair")),
            handoffs=handoffs,
        )


@dataclass(frozen=True)
class StoredOwnerContext:
    """表示从磁盘恢复出的完整 Owner checkpoint。"""

    owner: OwnerRecord
    packet: OwnerPacket
    segment: SegmentCheckpoint
    artifact_hashes: Mapping[str, str]
    capsule: object

    def __post_init__(self) -> None:
        """复制持久映射和消息，隔离后续修改。"""
        object.__setattr__(
            self,
            "artifact_hashes",
            dict(sorted((str(key), str(value)) for key, value in self.artifact_hashes.items())),
        )
        object.__setattr__(self, "capsule", copy.deepcopy(self.capsule))

    @property
    def messages(self) -> tuple[dict[str, Any], ...]:
        return self.segment.messages


class OwnerContextStore:
    """按 owner_id 原样保存闭合上下文；压缩统一由请求预算触发。"""

    def __init__(
        self,
        workspace: str | Path,
        *,
        max_string_chars: int = 32_000,
    ) -> None:
        """绑定私有上下文目录；字符串上限仅用于 capsule。"""
        self.workspace = Path(workspace).expanduser().resolve()
        self.directory = self.workspace / "_state" / "owner-context"
        self.max_string_chars = int(max_string_chars)
        if self.max_string_chars < 256:
            raise ValueError("owner context limits are too small")
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def save(
        self,
        *,
        owner: OwnerRecord,
        packet: OwnerPacket,
        segment: SegmentCheckpoint,
        artifact_hashes: Mapping[str, str],
        capsule: object = "",
        messages: Sequence[Mapping[str, Any]] | None = None,
    ) -> StoredOwnerContext:
        """不按消息条数或单段长度另行压缩；字符串上限只限制非输入 capsule。"""
        if packet.owner_id != owner.owner_id or packet.group_id != owner.group_id:
            raise ValueError("OwnerPacket identity does not match OwnerRecord")
        source_messages = segment.messages if messages is None else messages
        closed = replayable_prefix(source_messages)
        safe_segment = SegmentCheckpoint(
            segment_id=segment.segment_id,
            predecessor_segment_id=segment.predecessor_segment_id,
            messages=tuple(closed),
            provider_affinity_id=segment.provider_affinity_id,
            excluded_channel_ids=segment.excluded_channel_ids,
            reason=segment.reason,
            invalid_signature_recoveries=segment.invalid_signature_recoveries,
        )
        safe_capsule = _strip_large_payloads(
            capsule, max_string_chars=self.max_string_chars
        )
        hashes = dict(
            sorted((str(key), str(value)) for key, value in artifact_hashes.items())
        )
        payload = {
            "schema": OWNER_CONTEXT_SCHEMA,
            "owner": owner.to_dict(),
            "packet": packet.to_dict(),
            "segment": {**safe_segment.to_dict(), "messages": checkpoint_images(self.workspace, closed)},
            "artifact_hashes": hashes,
            "capsule": safe_capsule,
        }
        path = self.path_for(owner.owner_id)
        with self._lock:
            _atomic_json_write(path, payload, private=True)
        return StoredOwnerContext(
            owner=owner,
            packet=packet,
            segment=safe_segment,
            artifact_hashes=hashes,
            capsule=safe_capsule,
        )

    def load(self, owner_id: str) -> StoredOwnerContext | None:
        """读取指定 Owner 的 checkpoint，尚未保存时返回空值。"""
        path = self.path_for(owner_id)
        with self._lock:
            if not path.is_file():
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema") != OWNER_CONTEXT_SCHEMA:
            raise ValueError(f"unsupported owner context schema in {path}")
        owner_value = value.get("owner")
        packet_value = value.get("packet")
        segment_value = value.get("segment")
        hashes = value.get("artifact_hashes") or {}
        if not all(isinstance(item, dict) for item in (owner_value, packet_value, segment_value)):
            raise ValueError(f"owner context identity fields are invalid in {path}")
        messages = checkpoint_images(self.workspace, segment_value.get("messages", []), restore=True)
        segment_value = {**segment_value, "messages": messages}
        if not isinstance(hashes, dict) or not isinstance(messages, list):
            raise ValueError(f"owner context hashes or messages are invalid in {path}")
        if not all(isinstance(item, dict) for item in messages):
            raise ValueError(f"owner context messages must contain objects in {path}")
        return StoredOwnerContext(
            owner=OwnerRecord.from_dict(owner_value),
            packet=OwnerPacket.from_dict(packet_value),
            segment=SegmentCheckpoint.from_dict(segment_value),
            artifact_hashes={str(key): str(item) for key, item in hashes.items()},
            capsule=copy.deepcopy(value.get("capsule")),
        )

    def resume_mode(
        self,
        owner: OwnerRecord,
        *,
        max_context_images: int | None = None,
    ) -> ResumeMode:
        """输入版本变化不使有效会话失效；实际资源仍由制作和检查边界校验。"""
        stored = self.load(owner.owner_id)
        if stored is None:
            return ResumeMode.COLD
        if (
            stored.owner.owner_id != owner.owner_id
            or stored.owner.role != owner.role
            or stored.owner.pages != owner.pages
            or stored.owner.group_id != owner.group_id
        ):
            return ResumeMode.COLD
        if any(block.get("source", {}).get("type") == "omitted_base64"
               for message in stored.segment.messages for block in image_blocks(message.get("content"))):
            return ResumeMode.REBUILD
        if max_context_images is not None and sum(
            1 for message in stored.segment.messages
            for _ in image_blocks(message.get("content"))
        ) > max_context_images:
            return ResumeMode.REBUILD
        return ResumeMode.RESUME

    def path_for(self, owner_id: str) -> Path:
        """把稳定 owner_id 映射为不含可控路径片段的文件名。"""
        value = str(owner_id).strip()
        if not value:
            raise ValueError("owner_id must not be empty")
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
        return self.directory / f"{digest}.json"

    def save_orchestrator(
        self, segment: SegmentCheckpoint, *, identity: Mapping[str, object],
        artifact_hashes: Mapping[str, str], live_cursor: int,
    ) -> None:
        """保存主 Agent 的闭合上下文；私有签名不进入 host/公开轨迹。"""
        if not tool_boundary(segment.messages).is_closed:
            raise ValueError("orchestrator checkpoint has an open tool transaction")
        value = segment.to_dict()
        value["messages"] = checkpoint_images(self.workspace, value["messages"])
        payload = {
            "schema": "mural.orchestrator-context.v1", "identity": dict(identity),
            "segment": value, "segment_sha256": hashlib.sha256(
                json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            "artifact_hashes": dict(artifact_hashes), "live_cursor": int(live_cursor),
        }
        with self._lock:
            _atomic_json_write(self.path_for("orchestrator"), payload, private=True)

    def load_orchestrator(self) -> Mapping[str, object] | None:
        """读取同一主 Agent 的上下文；页面责任仍只由 Owner Registry 确认。"""
        path = self.path_for("orchestrator")
        with self._lock:
            if not path.exists():
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema") != "mural.orchestrator-context.v1":
            raise ValueError("unsupported orchestrator checkpoint")
        segment = value.get("segment")
        if not isinstance(segment, dict) or hashlib.sha256(
            json.dumps(segment, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest() != value.get("segment_sha256"):
            raise ValueError("orchestrator checkpoint checksum mismatch")
        if not tool_boundary(SegmentCheckpoint.from_dict(segment).messages).is_closed:
            raise ValueError("orchestrator checkpoint has an open tool transaction")
        value["segment"] = {**segment, "messages": checkpoint_images(self.workspace, segment["messages"], restore=True)}
        return value


def _strip_large_payloads(value: object, *, max_string_chars: int) -> Any:
    """仅缩短 capsule，不处理用于恢复模型输入的 messages。"""
    if isinstance(value, bytes):
        return {
            "omitted_binary_sha256": hashlib.sha256(value).hexdigest(),
            "byte_length": len(value),
        }
    if isinstance(value, str):
        if len(value) <= max_string_chars:
            return value
        return value[:max_string_chars] + "\n…[checkpoint 已截断]"
    if isinstance(value, list):
        return [
            _strip_large_payloads(item, max_string_chars=max_string_chars)
            for item in value
        ]
    if isinstance(value, tuple):
        return [
            _strip_large_payloads(item, max_string_chars=max_string_chars)
            for item in value
        ]
    if isinstance(value, Mapping):
        if value.get("type") in {"thinking", "redacted_thinking"}:
            return copy.deepcopy(dict(value))
        result = {
            str(key): _strip_large_payloads(item, max_string_chars=max_string_chars)
            for key, item in value.items()
        }
        block_type = str(value.get("type") or "")
        source = value.get("source")
        if block_type in {"image", "input_image"} and isinstance(source, Mapping):
            if source.get("type") == "base64" and isinstance(source.get("data"), str):
                raw = str(source["data"])
                result["source"] = {
                    "type": "omitted_base64",
                    "media_type": str(source.get("media_type") or ""),
                    "sha256": hashlib.sha256(raw.encode("ascii", "ignore")).hexdigest(),
                    "encoded_chars": len(raw),
                }
        return result
    return copy.deepcopy(value)


def _atomic_json_write(path: Path, value: Mapping[str, object], *, private: bool = False) -> None:
    """在目标目录内完整落盘后原子替换上下文文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    pending = path.with_name(f".{path.name}.{uuid.uuid4().hex}.pending")
    try:
        descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if private else 0o666)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)
