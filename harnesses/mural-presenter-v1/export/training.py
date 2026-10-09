"""导出真实上下文 segment、lineage、checkpoint、图片 blob 与准入结论。

运行期只保存 raw 段记录；训练清单和准入仅由显式离线导出生成。
训练行引用单个 Owner segment，绝不把整册所有 Agent 历史拼成一个样本。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping, Sequence
import uuid

from qc.routing import IssueLedger, IssueStatus
from runtime.config import RunMode
from runtime.base64_sanitize import scrub_inline_base64
from runtime.prompt_visibility import (
    INSPECTION_VISIBLE_CRITIC_FEEDBACK_KEYS as TRAINING_VISIBLE_CRITIC_FEEDBACK_KEYS,
    INSPECTION_VISIBLE_DESCRIPTOR_KEYS as TRAINING_VISIBLE_INSPECTION_DESCRIPTOR_KEYS,
    INSPECTION_VISIBLE_VISUAL_SOURCE_KEYS as TRAINING_VISIBLE_VISUAL_SOURCE_KEYS,
    model_visible_messages,
)
from runtime.recovery import SegmentCheckpoint, strip_signed_thinking, tool_boundary, validate_segment_lineage
from runtime.live_trace import iter_jsonl_events
from runtime.raw_trace import store_blob, without_context_signatures
from runtime.session import estimate_request_tokens
from state.artifacts import ArtifactStore
from state.workspace import WorkspaceState, file_sha256

from .delivery import DeliveryManifest


TRAINING_SEGMENT_SCHEMA = "mural.training-segment.v2"
TRAINING_MANIFEST_SCHEMA = "mural.training-export.v2"

EPHEMERAL_TRAINING_TEXT_KEYS = frozenset({
    "agent_id",
    "attempt_id",
    "critic_agent_id",
    "event_id",
    "html_sha256",
    "image_sha256",
    "inspection_id",
    "owner_id",
    "page_uid",
    "png_sha256",
    "segment_id",
    "tool_use_id",
    "trace_path",
})
PROMPT_INVISIBLE_TOOL_RESULT_KEYS = EPHEMERAL_TRAINING_TEXT_KEYS | frozenset({
    "cache_hit",
    "critic_prompt_version",
    "critic_question",
    "content_path",
    "full_feedback_ref",
    "full_body_path",
    "geometry",
    "html_sha256",
    "inspect_timings_s",
    "language_policy_version",
    "members",
    "metadata_path",
    "model_config",
    "opened_not_verified",
    "opened_visual_sources",
    "owner_question",
    "question",
    "render_committed",
    "render_diagnostics",
    "reused_from",
    "rubric_version",
    "source_refs",
})
LEGACY_TOOLCALL_TEXT_RE = re.compile(
    r"</?invoke\b|</?tool_call\b|<function=|</function\s*>|</?parameter(?:\b|=)",
    re.IGNORECASE,
)
INTERNAL_MODEL_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])_(?:state|trace)[/\\]", re.IGNORECASE
)


class TrainingExportError(RuntimeError):
    """表示训练轨迹无法机械重建或导出。"""

    def __init__(self, message: str, *, manifest: Mapping[str, object] | None = None):
        super().__init__(message)
        self.manifest = manifest


@dataclass(frozen=True)
class TrainingSegmentInput:
    """保存一个 Owner 的单个上下文 checkpoint 及产物引用。"""

    owner_id: str
    role: str
    checkpoint: SegmentCheckpoint
    artifact_hashes: Mapping[str, str] = field(default_factory=dict)
    image_paths: tuple[str, ...] = ()
    artifact_blobs: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    image_blobs: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        """规范身份、哈希与图片引用。"""
        if not str(self.owner_id).strip() or not str(self.role).strip():
            raise ValueError("training segment owner and role must not be empty")
        object.__setattr__(
            self,
            "artifact_hashes",
            dict(sorted((str(key), str(value)) for key, value in self.artifact_hashes.items())),
        )
        object.__setattr__(
            self,
            "image_paths",
            tuple(dict.fromkeys(str(path) for path in self.image_paths)),
        )


class TrainingExporter:
    """把共享运行轨迹导出为可验证引用，并执行训练候选完整性准入。"""

    def __init__(
        self,
        workspace: WorkspaceState,
        artifacts: ArtifactStore,
        issues: IssueLedger,
        *,
        run_mode: RunMode | str,
    ) -> None:
        """绑定共享状态真源，沿用实际上下文边界，不按消息条数裁段。"""
        self.workspace = workspace
        self.artifacts = artifacts
        self.issues = issues
        self.run_mode = RunMode(run_mode)
        self.root = workspace.paths.root
        self.trace_root = workspace.paths.trace / "training"
        self.segment_dir = workspace.paths.trace / "raw/segments"
        self.blob_dir = workspace.paths.trace / "raw/blobs"
        self.rows_path = self.trace_root / "training-segments.jsonl"
        self.manifest_path = self.trace_root / "training-manifest.json"

    def capture(
        self, owner_id: str, role: str, checkpoint: SegmentCheckpoint,
        *, artifact_paths: Sequence[str] = (),
    ) -> TrainingSegmentInput:
        """在真实闭合边界保存文件版本；同段刷新，前驱段不再绑定后来的文件。"""
        if not tool_boundary(checkpoint.messages).is_closed:
            raise TrainingExportError("cannot capture an open tool transaction")
        self.segment_dir.mkdir(parents=True, exist_ok=True)
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        blobs = {}
        for relative in artifact_paths:
            path = self.workspace.paths.resolve(relative)
            if path.is_file():
                blobs[relative] = self._store_blob(path.read_bytes(), path.suffix)
        segment = TrainingSegmentInput(
            owner_id, role, checkpoint,
            artifact_hashes={key: str(blob["sha256"]) for key, blob in blobs.items()},
            artifact_blobs=blobs,
        )
        self._export_segment(segment)
        return segment

    def load_segments(self) -> tuple[TrainingSegmentInput, ...]:
        """恢复已落盘的闭合段，包括已完成 Owner；不依赖有界 resume capsule。"""
        if not self.segment_dir.exists() and any((self.trace_root / "segments").glob("segment-*.json")):
            raise TrainingExportError("workspace uses pre-deferred training snapshots; use its pinned runtime, not a silent fresh start")
        pending = []
        for path in sorted(self.segment_dir.glob("segment-*.json")):
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("schema") != "mural.training-checkpoint.v1":
                raise TrainingExportError(f"unsupported training checkpoint: {path.name}")
            checkpoint = SegmentCheckpoint.from_dict({**value, "schema": "mural.runtime-segment.v1"})
            segment = TrainingSegmentInput(
                str(value["owner_id"]), str(value["role"]), checkpoint,
                artifact_hashes=value.get("artifact_hashes", {}),
                artifact_blobs=value.get("artifact_blobs", {}),
                image_blobs=tuple(value.get("image_blobs", [])),
            )
            if path.stem != _sample_id(segment.owner_id, checkpoint.segment_id):
                raise TrainingExportError(f"training checkpoint identity changed: {path.name}")
            pending.append(segment)
        ordered, seen = [], set()
        while pending:
            ready = [segment for segment in pending if segment.checkpoint.predecessor_segment_id is None
                     or (segment.owner_id, segment.checkpoint.predecessor_segment_id) in seen]
            if not ready:
                raise TrainingExportError("training checkpoint lineage is incomplete or cyclic")
            for segment in ready:
                ordered.append(segment)
                seen.add((segment.owner_id, segment.checkpoint.segment_id))
                pending.remove(segment)
        return tuple(ordered)

    def export(
        self,
        segments: Sequence[TrainingSegmentInput],
        delivery: DeliveryManifest,
        *,
        critic: Mapping[str, object] | None = None,
        content_qc: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """写出 segment 引用和准入 manifest，即使拒绝也保留诊断原因。

        Owner 的 inspect 回执和独立 Critic checkpoint 保持各自真实消息边界；
        视觉评审只检查执行完整性。生成阶段不运行内容 QC；若后处理显式提供审计汇总，
        才据此限制候选段准入。accepted 只表示 raw 轨迹可供后处理筛选，不是 clean。
        """
        self.segment_dir.mkdir(parents=True, exist_ok=True)
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        # 全局门:整册产物级问题(交付失败 / blocking issue / 已启用视觉 Critic 执行未完成 / 内容 QC 高危 /
        # lineage 非法)。这些说明整册轨迹不可信,应拒收全部 segment。
        global_reasons = self._validate_global(segments, delivery)
        global_reasons.extend(self._validate_critic_admission(critic))
        global_reasons.extend(self._validate_content_qc(content_qc))
        global_reasons = sorted(set(global_reasons))
        qc_pending = bool(set(global_reasons) & {
            "critic_incomplete", "content_qc_incomplete",
            "content_qc_not_effective_in_synthesis",
        })
        rows: list[dict[str, object]] = []
        segment_reason_lists: list[list[str]] = []
        for segment in segments:
            row, reasons = self._export_segment(segment)
            rows.append(row)
            segment_reason_lists.append(sorted(set(reasons)))
        # 段级门(工具事务未闭合 / 该段图片 blob 坏)只该拒**该段**,
        # 不连累同册其它干净段。整册 rejection_reasons 仍汇总全部原因供诊断。
        rejection_reasons = sorted(
            set(global_reasons).union(*segment_reason_lists) if segment_reason_lists
            else set(global_reasons)
        )
        trainable_count = 0
        for row, seg_reasons in zip(rows, segment_reason_lists):
            if self.run_mode == RunMode.INFERENCE:
                row_admission = "not_applicable"
                trainable = False
            else:
                # 每段独立准入:无全局门且本段无段级门 → 该段可供后处理筛选。
                trainable = not global_reasons and not seg_reasons
                row_admission = "pending" if qc_pending else ("accepted" if trainable else "rejected")
            row["training_admission"] = row_admission
            row["trainable"] = trainable
            if trainable:
                trainable_count += 1
        # 整册 admission:inference 不适用;synthesis 下有全局门则整册拒,否则只要有任一
        # 可训练段即 accepted(段级问题由 per-row trainable 精确隔离,不整册一刀切)。
        if self.run_mode == RunMode.INFERENCE:
            admission = "not_applicable"
        elif qc_pending:
            admission = "pending"
        elif global_reasons:
            admission = "rejected"
        else:
            admission = "accepted" if trainable_count else "rejected"
        _atomic_jsonl_write(self.rows_path, rows)
        accepted_degradations = [
            issue.issue_id
            for issue in self.issues.issues()
            if issue.status == IssueStatus.ACCEPTED_DEGRADATION
        ]
        portable = delivery.outputs.get("portable_zip") or {}
        skill_ref = self.workspace.paths.skill_ref
        pinned_skill = json.loads(skill_ref.read_text(encoding="utf-8")) if skill_ref.is_file() else {}
        font_manifest = self.workspace.paths.font_manifest
        manifest = {
            "schema": TRAINING_MANIFEST_SCHEMA,
            "data_stage": "raw",
            "run_mode": self.run_mode.value,
            "training_admission": admission,
            "eligible_for_training": admission == "accepted",
            "segment_count": len(rows),
            "trainable_segment_count": trainable_count,
            "token_budget": {
                "captured_sample_limits": sorted({row["token_budget"]["training_sample_max_tokens"]
                    for row in rows if type(row["token_budget"]["training_sample_max_tokens"]) is int}),
                "estimated_over_budget_segments": sum(
                    row["token_budget"]["status"] == "estimated_over_budget" for row in rows),
                "unconfigured_segments": sum(
                    row["token_budget"]["training_sample_max_tokens"] is None for row in rows),
                "token_lengths_verified": False,
                "exact_validation_required": True,
            },
            "global_rejection_reasons": global_reasons,
            "segments_path": self.workspace.paths.relative(self.rows_path),
            "segments_sha256": file_sha256(self.rows_path),
            "generation_ref": "result.json#/generation",
            "delivery_manifest": delivery.path or None,
            "delivery_status": delivery.status,
            "skill_tree_sha256": str(pinned_skill.get("tree_sha256") or ""),
            "font_manifest_sha256": file_sha256(font_manifest) if font_manifest.is_file() else "",
            "portable_package_manifest_sha256": str(
                portable.get("package_manifest_sha256") or ""
            ),
            "attachments": self._attachment_provenance(),
            "rejection_reasons": rejection_reasons,
            "accepted_degradations": accepted_degradations,
            "contract": (
                "每行只引用一个 Owner 的真实上下文 segment、前驱 checkpoint、已验证产物和图片 blob；"
                "首段 predecessor 必须为 null。全局门(交付/已启用 Critic/内容 QC/lineage)拒整册；"
                "段级门(未闭合事务/坏 blob)只拒该段,同册其它干净段仍 trainable。"
                "生成阶段不调用整册内容 QC；未审 raw 可进入后处理，但不等于 clean。"
                "显式提供的 QC 未完成记 pending，保留 raw 且不作质量负样本；段准入不替代下游 QC。"
                "视觉 Critic 的完成与意见内容无关；accepted 只表示可供训练筛选，不证明语义质量。"
                "每段长度仅为估算，超长 raw 不裁剪、不拆工具事务；所有候选须经目标训练模板完整编码校验，"
                "包含系统、工具定义、图片、上下文与答案，不通过时不得作为训练就绪数据。"
            ),
            "external_visual_critic": {
                "requested": bool((critic or {}).get("requested")),
                "effective": bool((critic or {}).get("effective")),
                "critic_incomplete": bool((critic or {}).get("critic_incomplete")),
                "current_inspections": copy.deepcopy((critic or {}).get("current_inspections", [])),
            },
            "content_qc": {"stage": "postprocess", "status": "pending"} if content_qc is None else {
                "requested": bool((content_qc or {}).get("requested")),
                "effective": bool((content_qc or {}).get("effective")),
                "content_qc_incomplete": bool(
                    (content_qc or {}).get("content_qc_incomplete")
                ),
                "unresolved_high_severity": bool(
                    (content_qc or {}).get("unresolved_high_severity")
                ),
            },
        }
        _atomic_json_write(self.manifest_path, manifest)
        return manifest

    def _attachment_provenance(self) -> list[dict[str, object]]:
        """把附件 manifest 的逻辑记录投影进训练 manifest，保留证据溯源。

        只引用附件身份（sha256）、原名、顺序、提取文本存在性与逻辑页数，全部为工作区相对；
        不重新解析，不复制像素，不泄露绝对路径，因此训练样本可追溯到确切原始输入而不丢证据。
        """
        from runtime.attachments import (
            resolve_attachment_records,
            resolve_extracted_text,
            resolve_page_assets,
        )

        provenance: list[dict[str, object]] = []
        for record in resolve_attachment_records(self.root):
            text_path = resolve_extracted_text(record)
            provenance.append(
                {
                    "attachment_id": record.attachment_id,
                    "sha256": record.sha256,
                    "ordinal": record.ordinal,
                    "original_name": record.original_name,
                    "original_path": record.original_path,
                    "extracted_root": record.extracted_root,
                    "kind": record.kind,
                    "extracted_text_path": (
                        self.workspace.paths.relative(text_path)
                        if text_path is not None and text_path.is_file()
                        else ""
                    ),
                    "page_count": len(resolve_page_assets(record)),
                    "media_count": len(record.media_paths),
                }
            )
        return provenance

    def _validate_critic_admission(
        self, critic: Mapping[str, object] | None
    ) -> list[str]:
        """Critic 完整性准入：启用时要求必要评审执行完成，不裁决意见内容。

        ``critic is None`` 表示本次导出未接入 Critic 汇总（feature 未参与），不据此拒绝；
        未启用的合成任务同样不据此拒绝；这里只裁决已启用的汇总。
        """
        reasons: list[str] = []
        if self.run_mode != RunMode.SYNTHESIS or critic is None:
            return reasons
        if not bool(critic.get("effective")):
            return reasons
        if bool(critic.get("critic_incomplete")):
            reasons.append("critic_incomplete")
        return reasons

    def _validate_content_qc(
        self, content_qc: Mapping[str, object] | None
    ) -> list[str]:
        """内容 QC 准入：synthesis 下未完成 / 有未处置高危 finding 拒绝正样本。

        ``content_qc is None`` 表示 raw 尚未进行后处理内容 QC，不阻止制作完成与候选导出。
        只有后处理显式提供汇总时才使用此检查；不会触发模型调用或向创作轨迹注入反馈。
        """
        reasons: list[str] = []
        if self.run_mode != RunMode.SYNTHESIS or content_qc is None:
            return reasons
        if not bool(content_qc.get("effective")):
            # 提供了汇总却未实际审计：不能把它视作通过。
            reasons.append("content_qc_not_effective_in_synthesis")
            return reasons
        if bool(content_qc.get("content_qc_incomplete")):
            reasons.append("content_qc_incomplete")
        if bool(content_qc.get("unresolved_high_severity")):
            reasons.append("content_qc_unresolved")
        return reasons

    def _validate_global(
        self,
        segments: Sequence[TrainingSegmentInput],
        delivery: DeliveryManifest,
    ) -> list[str]:
        """验证运行模式、交付、Issue 和每个 Owner 的 lineage。"""
        reasons: list[str] = []
        if self.run_mode != RunMode.SYNTHESIS:
            reasons.append(f"run_mode={self.run_mode.value}")
        if delivery.status != "complete":
            reasons.append(f"delivery_status={delivery.status or 'missing'}")
        if not segments:
            reasons.append("no_training_segments")
        blockers = self.issues.blocking_issues()
        reasons.extend(f"blocking_issue={issue.issue_id}" for issue in blockers)
        by_owner: dict[str, list[SegmentCheckpoint]] = {}
        for segment in segments:
            by_owner.setdefault(segment.owner_id, []).append(segment.checkpoint)
        for owner_id, checkpoints in by_owner.items():
            if checkpoints and checkpoints[0].predecessor_segment_id is not None:
                reasons.append(f"lineage_first_predecessor_not_null={owner_id}")
            try:
                validate_segment_lineage(checkpoints)
            except ValueError as exc:
                reasons.append(f"lineage_invalid={owner_id}:{exc}")
        return reasons

    def _export_segment(
        self, segment: TrainingSegmentInput
    ) -> tuple[dict[str, object], list[str]]:
        """外置单个 segment 的消息图片并写入 checkpoint 文件。"""
        reasons: list[str] = []
        checkpoint = segment.checkpoint
        if any(message.get("_critic_provenance") for message in checkpoint.messages):
            reasons.append(f"non_model_transaction={checkpoint.segment_id}")
        boundary = tool_boundary(checkpoint.messages)
        if not boundary.is_closed:
            reasons.append(f"open_tool_transaction={checkpoint.segment_id}")
        messages, inline_blobs = self._externalize_images(checkpoint.messages)
        toolcall_text_markers = _assistant_legacy_toolcall_markers(messages)
        if toolcall_text_markers:
            reasons.append(f"toolcall_template_tag_in_assistant_text={checkpoint.segment_id}")
        internal_path_markers = _assistant_internal_path_markers(messages)
        if internal_path_markers:
            reasons.append(f"internal_runtime_path_in_assistant_output={checkpoint.segment_id}")
        explicit_blobs: list[dict[str, object]] = []
        for blob in segment.image_blobs:
            if self._valid_blob(blob):
                explicit_blobs.append(dict(blob))
            else:
                reasons.append(f"image_blob_invalid={blob.get('path')}")
        for relative in segment.image_paths:
            try:
                source = self.workspace.paths.resolve(relative)
            except RuntimeError:
                reasons.append(f"image_path_escape={relative}")
                continue
            if not source.is_file():
                reasons.append(f"image_blob_missing={relative}")
                continue
            explicit_blobs.append(self._store_blob(source.read_bytes(), source.suffix))
        artifact_validation: dict[str, str] = {}
        for key, expected in segment.artifact_hashes.items():
            blob = segment.artifact_blobs.get(key)
            if blob is not None:
                if str(blob.get("sha256")) == expected and self._valid_blob(blob):
                    artifact_validation[key] = "checkpoint_snapshot"
                else:
                    artifact_validation[key] = "invalid_snapshot"
                    reasons.append(f"artifact_snapshot_invalid={key}")
                continue
            actual = self._artifact_hash(key)
            if not actual:
                artifact_validation[key] = "missing"
                reasons.append(f"artifact_reference_missing={key}")
            elif actual == expected:
                artifact_validation[key] = "current"
            elif self._checkpoint_attests_artifact(
                checkpoint.messages, key=key, expected=expected
            ):
                artifact_validation[key] = "checkpoint_attested_revision"
            else:
                artifact_validation[key] = "mismatch"
                reasons.append(f"artifact_hash_mismatch={key}")
        sample_id = _sample_id(segment.owner_id, checkpoint.segment_id)
        checkpoint_path = self.segment_dir / f"{sample_id}.json"
        image_blobs = list({str(blob["path"]): blob for blob in [*inline_blobs, *explicit_blobs]}.values())
        observable = self._observable_trace(segment)
        context = observable["agent_context"][0] if observable["agent_context"] else {}
        sample_limit = context.get("training_sample_max_tokens")
        if sample_limit is not None and (type(sample_limit) is not int or sample_limit <= 0):
            reasons.append(f"training_budget_invalid={checkpoint.segment_id}")
        estimated = (estimate_request_tokens(context["system"], messages, context["tools"])
            if "system" in context and "tools" in context else None)
        valid_limit = type(sample_limit) is int and sample_limit > 0
        # raw 保留真实末尾工具回执；估算超限是离线精确验证的诊断，不是质量负样本或截断依据。
        token_budget = {
            "training_sample_max_tokens": sample_limit,
            "estimated_total_tokens": estimated,
            "estimator": "runtime_character_image_heuristic_v1",
            "status": ("unconfigured" if not valid_limit else "input_context_missing" if estimated is None
                else "estimated_over_budget" if estimated > sample_limit else "estimated_within_budget"),
            "token_length_verified": False,
            "exact_validation_required": True,
        }
        if segment.role == "critic":
            contexts = observable["agent_context"]
            context = contexts[0] if contexts else {}
            metadata = context.get("metadata") or {}
            initial = context.get("messages") or []
            user_blocks = messages[0].get("content") if messages else None
            image = user_blocks[0] if isinstance(user_blocks, list) and len(user_blocks) in {1, 2} else {}
            supplemental = user_blocks[1:] if isinstance(user_blocks, list) else []
            context_blocks = initial[0].get("content") if len(initial) == 1 else None
            source = image.get("source") or {}
            response = messages[-1].get("content") if messages else None
            public_text = response if isinstance(response, str) else "\n".join(
                str(block.get("text") or "") for block in response or [] if block.get("type") == "text")
            kind = metadata.get("kind", "page")
            members = metadata.get("members") or []
            target_valid = (
                all(metadata.get(key) for key in ("page_uid", "page", "html_sha256")) if kind == "page"
                else bool(members) and all(isinstance(member, Mapping) and all(member.get(key)
                    for key in ("page", "page_uid", "html_sha256", "png_sha256")) for member in members) if kind == "group"
                else bool(metadata.get("png_path")) and not members if kind == "asset" else False
            )
            valid = (
                bool(context.get("system")) and bool(context.get("model")) and context.get("tools") == []
                and len(initial) == 1 and initial[0].get("role") == "user"
                and all(metadata.get(key) for key in (
                    "inspection_id", "png_sha256", "image_sha256", "owner_id", "tool_use_id"))
                and target_valid
                and len(messages) == 2 and messages[0].get("role") == "user"
                and messages[1].get("role") == "assistant" and bool(public_text.strip())
                and image.get("type") == "image" and source.get("type") == "blob_ref"
                and source.get("sha256") == metadata.get("image_sha256")
                and all(block.get("type") == "text" and isinstance(block.get("text"), str)
                        for block in supplemental)
                and isinstance(context_blocks, list) and supplemental == context_blocks[1:]
            )
            if not valid:
                reasons.append(f"critic_input_incomplete={checkpoint.segment_id}")
        if any(item.get("partial") or item.get("interrupted") for key in (
                "assistant_reasoning", "assistant_analysis_summary") for item in observable[key]):
            reasons.append(f"partial_provider_reasoning={checkpoint.segment_id}")
        payload = {
            "schema": "mural.training-checkpoint.v1",
            "owner_id": segment.owner_id,
            "role": segment.role,
            "segment_id": checkpoint.segment_id,
            "predecessor_segment_id": checkpoint.predecessor_segment_id,
            "reason": checkpoint.reason,
            "provider_affinity_id": checkpoint.provider_affinity_id,
            "excluded_channel_ids": list(checkpoint.excluded_channel_ids),
            "invalid_signature_recoveries": checkpoint.invalid_signature_recoveries,
            "messages": messages,
            "toolcall_text_validation": {
                "status": "failed" if toolcall_text_markers else "passed",
                "forbidden_markers": toolcall_text_markers,
            },
            "internal_path_validation": {
                "status": "failed" if internal_path_markers else "passed",
                "forbidden_markers": internal_path_markers,
            },
            "token_budget": token_budget,
            **observable,
            "artifact_hashes": dict(segment.artifact_hashes),
            "artifact_blobs": dict(segment.artifact_blobs),
            "artifact_validation": artifact_validation,
            "image_blobs": image_blobs,
        }
        _atomic_json_write(checkpoint_path, payload)
        row = {
            "schema": TRAINING_SEGMENT_SCHEMA,
            "sample_id": sample_id,
            "sample_kind": "agent_segment",
            "owner_id": segment.owner_id,
            "role": segment.role,
            "segment_id": checkpoint.segment_id,
            "predecessor_segment_id": checkpoint.predecessor_segment_id,
            "checkpoint_path": self.workspace.paths.relative(checkpoint_path),
            "checkpoint_sha256": file_sha256(checkpoint_path),
            "message_count": len(messages),
            "token_budget": token_budget,
            "artifact_hashes": dict(segment.artifact_hashes),
            "artifact_blobs": dict(segment.artifact_blobs),
            "artifact_validation": artifact_validation,
            "image_blobs": image_blobs,
            "toolcall_text_validation": {
                "status": "failed" if toolcall_text_markers else "passed",
                "forbidden_markers": toolcall_text_markers,
            },
            "internal_path_validation": {
                "status": "failed" if internal_path_markers else "passed",
                "forbidden_markers": internal_path_markers,
            },
            "segment_rejection_reasons": sorted(set(reasons)),
        }
        return row, reasons

    def _observable_trace(self, segment):
        events = [event for event in iter_jsonl_events(self.workspace.paths.trace / "live-events.jsonl")
                  if event.get("agent_id") == segment.owner_id
                  and str(event.get("segment_id")) == str(segment.checkpoint.segment_id)]
        reasoning, summaries, refs, contexts = [], [], [], []
        for event in events:
            kind, value = event.get("event_type"), event.get("payload", {})
            recorded = {**value, "sequence": event["sequence"], "event_id": event["event_id"]}
            recorded.pop("signature", None)
            recorded.pop("signature_metadata", None)
            if kind == "assistant_reasoning":
                target = reasoning if value.get("mode") == "full" else summaries
                target.append(recorded)
            elif kind == "assistant_analysis_summary" and not any(
                    item.get("attempt_id") == value.get("attempt_id") for item in summaries):
                summaries.append({**recorded, "mode": "summary", "content": value.get("summary", ""),
                                  "source": "provider_response"})
            elif kind in {"provider_raw_request", "provider_raw_response"}:
                refs.append(recorded)
            elif kind == "agent_context":
                if "messages" in recorded:
                    recorded["messages"] = _scrub_ephemeral_training_text(
                        without_context_signatures(recorded["messages"])
                    )
                contexts.append(recorded)
        return {"assistant_reasoning": reasoning, "assistant_analysis_summary": summaries,
            "raw_provider_refs": refs, "agent_context": contexts,
            "reasoning_status": "recorded" if reasoning else "unavailable_in_source_trace",
            "trace_provenance": {"source": "_trace/live-events.jsonl",
                "agent_id": segment.owner_id, "segment_id": segment.checkpoint.segment_id}}

    def _externalize_images(
        self, messages: Sequence[Mapping[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, object]]]:
        """递归把消息中的 base64 图片替换为内容寻址 blob 引用。"""
        copied = strip_signed_thinking(messages)
        blobs: list[dict[str, object]] = []

        def visit(value: object) -> None:
            if isinstance(value, list):
                for item in value:
                    visit(item)
                return
            if not isinstance(value, dict):
                return
            source = value.get("source")
            if isinstance(source, dict) and source.get("type") == "blob_ref":
                if not self._valid_blob(source):
                    raise TrainingExportError("message image blob is missing or corrupt")
                path = self.workspace.paths.resolve(str(source["path"]))
                blobs.append({"path": source["path"], "sha256": source["sha256"], "bytes": path.stat().st_size})
            if isinstance(source, dict) and source.get("type") == "base64":
                data = source.get("data")
                if isinstance(data, str):
                    try:
                        raw = base64.b64decode(data, validate=True)
                    except ValueError as exc:
                        raise TrainingExportError(
                            "message image base64 is invalid"
                        ) from exc
                    media_type = str(source.get("media_type") or "image/png")
                    extension = {
                        "image/png": ".png",
                        "image/jpeg": ".jpg",
                        "image/webp": ".webp",
                        "image/gif": ".gif",
                    }.get(media_type, ".bin")
                    blob = self._store_blob(raw, extension)
                    blobs.append(blob)
                    value["source"] = {
                        "type": "blob_ref",
                        "media_type": media_type,
                        "sha256": blob["sha256"],
                        "path": blob["path"],
                    }
            for child in value.values():
                visit(child)

        visit(copied)
        copied = scrub_inline_base64(copied)
        copied = _scrub_ephemeral_training_text(copied)
        # 原生图片外置为 blob；普通文本中夹带的内联二进制只保留短收据。
        unique = {
            str(blob["sha256"]): blob for blob in blobs
        }
        return copied, [unique[key] for key in sorted(unique)]

    def _store_blob(self, payload: bytes, extension: str) -> dict[str, object]:
        return store_blob(self.root, payload)

    def _valid_blob(self, blob: Mapping[str, object]) -> bool:
        try:
            path = self.workspace.paths.resolve(str(blob.get("path") or ""))
            path.relative_to(self.blob_dir.resolve())
        except (RuntimeError, ValueError):
            return False
        return path.is_file() and file_sha256(path) == str(blob.get("sha256") or "")

    def _artifact_hash(self, key: str) -> str:
        """按 artifact_id 或工作区相对路径读取当前哈希。"""
        try:
            return self.artifacts.artifact(key).sha256
        except KeyError:
            pass
        try:
            return file_sha256(self.workspace.paths.resolve(key))
        except RuntimeError:
            return ""

    def _checkpoint_attests_artifact(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        key: str,
        expected: str,
    ) -> bool:
        """允许已被后续修订替代、但由本段闭合工具结果证明的历史产物。"""
        try:
            reference = self.artifacts.artifact(key).path
        except KeyError:
            reference = key
        for message in messages:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, Mapping) or block.get("type") != "tool_result":
                    continue
                evidence = json.dumps(
                    block.get("content"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if str(expected) in evidence and str(reference) in evidence:
                    return True
        return False


def _assistant_legacy_toolcall_markers(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, object]]:
    """Find XML/tag-style tool calls that leaked into assistant target text."""
    markers: list[dict[str, object]] = []

    def text_fragments(value: object) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            fragments: list[str] = []
            for item in value:
                fragments.extend(text_fragments(item))
            return fragments
        if not isinstance(value, Mapping):
            return []
        block_type = str(value.get("type") or "")
        if block_type == "tool_use":
            return []
        fragments = []
        for key in ("text", "content", "thinking", "reasoning", "reasoning_content"):
            item = value.get(key)
            if isinstance(item, str):
                fragments.append(item)
        return fragments

    for index, message in enumerate(messages):
        if message.get("role") != "assistant":
            continue
        candidates = text_fragments(message.get("content"))
        for field in ("reasoning_content", "reasoning"):
            value = message.get(field)
            if isinstance(value, str):
                candidates.append(value)
        for text in candidates:
            match = LEGACY_TOOLCALL_TEXT_RE.search(text)
            if match:
                markers.append({
                    "message_index": index,
                    "marker": match.group(0)[:32],
                    "field": "assistant_text",
                })
                break
    return markers


def _assistant_internal_path_markers(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, object]]:
    """Reject assistant targets coupled to private Harness directories."""
    markers: list[dict[str, object]] = []
    for index, message in enumerate(messages):
        if message.get("role") != "assistant":
            continue
        candidates: list[tuple[str, str]] = []
        content = message.get("content")
        if isinstance(content, str):
            candidates.append(("assistant_text", content))
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, Mapping):
                    continue
                if block.get("type") == "tool_use":
                    candidates.append((
                        "tool_arguments",
                        json.dumps(block.get("input"), ensure_ascii=False, default=str),
                    ))
                elif isinstance(block.get("text"), str):
                    candidates.append(("assistant_text", str(block["text"])))
        for field in ("reasoning_content", "reasoning"):
            if isinstance(message.get(field), str):
                candidates.append((field, str(message[field])))
        for field, text in candidates:
            match = INTERNAL_MODEL_PATH_RE.search(text)
            if match:
                markers.append({
                    "message_index": index,
                    "marker": match.group(0).replace("\\", "/"),
                    "field": field,
                })
                break
    return markers


def _sample_id(owner_id: str, segment_id: str) -> str:
    """由 Owner 和 segment 生成稳定 sample ID。"""
    digest = hashlib.sha256(f"{owner_id}\0{segment_id}".encode("utf-8")).hexdigest()
    return f"segment-{digest[:24]}"


def _scrub_ephemeral_training_text(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Redact run-specific ids from model-visible training text, not provenance refs."""
    copied = model_visible_messages(messages)

    def training_visible_json(value: object) -> object:
        if isinstance(value, list):
            return [training_visible_json(item) for item in value]
        if not isinstance(value, dict):
            return value
        output: dict[str, object] = {}
        descriptor = value.get("pending_inspection")
        if not isinstance(descriptor, Mapping):
            descriptor = value.get("current_inspection")
        if isinstance(descriptor, Mapping):
            for key in TRAINING_VISIBLE_INSPECTION_DESCRIPTOR_KEYS:
                if key in descriptor and key not in value:
                    output[key] = training_visible_json(descriptor[key])
        for raw_key, item in value.items():
            key = str(raw_key)
            if key in PROMPT_INVISIBLE_TOOL_RESULT_KEYS or key in {"pending_inspection", "current_inspection"}:
                continue
            if key == "critic_feedback" and isinstance(item, Mapping):
                visible = {
                    str(inner_key): training_visible_json(inner_value)
                    for inner_key, inner_value in item.items()
                    if str(inner_key) in TRAINING_VISIBLE_CRITIC_FEEDBACK_KEYS
                }
                if visible:
                    output[key] = visible
                continue
            if key == "visual_source" and isinstance(item, Mapping):
                visible = {
                    str(inner_key): training_visible_json(inner_value)
                    for inner_key, inner_value in item.items()
                    if str(inner_key) in TRAINING_VISIBLE_VISUAL_SOURCE_KEYS
                }
                if visible:
                    output[key] = visible
                continue
            output[key] = training_visible_json(item)
        return output

    def redact_private_runtime_paths(text: str) -> str:
        return re.sub(
            r"(?<![A-Za-z0-9_.-])_(?:state|trace)[/\\][^\s\"'<>]+",
            "<private-runtime-path>",
            text,
            flags=re.IGNORECASE,
        )

    def redact_json(value: object) -> object:
        value = training_visible_json(value)
        if isinstance(value, list):
            return [redact_json(item) for item in value]
        if isinstance(value, dict):
            output = {}
            for key, item in value.items():
                if str(key) in PROMPT_INVISIBLE_TOOL_RESULT_KEYS:
                    continue
                output[key] = redact_json(item)
            return output
        if isinstance(value, str):
            return redact_private_runtime_paths(value)
        return value

    def redact_text(text: str, *, parse_json: bool = False) -> str:
        raw = str(text or "")
        stripped = raw.strip()
        if parse_json and stripped[:1] in {"{", "["}:
            try:
                decoded = json.loads(stripped)
            except (TypeError, ValueError):
                pass
            else:
                redacted = redact_json(decoded)
                if redacted == decoded:
                    return raw
                return json.dumps(redacted, ensure_ascii=False, sort_keys=True)
        value = re.sub(r"；page_uid[:：][^。\n]*。", "。", raw)
        value = re.sub(r"\bpage_uid[:：][^\s\n,，。；;]*[。]?", "", value)
        value = re.sub(r"\binspection-[0-9a-f]{12,}\b", "inspection-<redacted>", value)
        value = re.sub(r"\battempt-\d{3}-[0-9a-f]{16,}\b", "attempt-<redacted>", value)
        value = re.sub(r"\bcritic-segment-[0-9a-f]{16,}\b", "critic-segment-<redacted>", value)
        value = re.sub(r"\bsegment-[0-9a-f]{16,}\b", "segment-<redacted>", value)
        value = re.sub(r"\bpg_[A-Za-z0-9_:-]{8,}\b", "pg_<redacted>", value)
        value = re.sub(r"\b[0-9a-f]{64}\b", "<sha256>", value)
        return redact_private_runtime_paths(value)

    def visit(value: object, key: str = "", *, role: str = "") -> None:
        if isinstance(value, list):
            for item in value:
                visit(item, role=role)
            return
        if not isinstance(value, dict):
            return
        if value.get("type") in ("tool_use", "image"):
            return
        if value.get("type") == "tool_result":
            content = value.get("content")
            if isinstance(content, str):
                value["content"] = redact_text(content, parse_json=True)
            elif isinstance(content, list):
                value["content"] = [
                    {**part, "text": redact_text(part["text"], parse_json=True)}
                    if isinstance(part, dict) and part.get("type") == "text"
                    and isinstance(part.get("text"), str) else copy.deepcopy(part)
                    for part in content
                ]
            elif isinstance(content, dict):
                value["content"] = redact_json(content)
            for child_key, child in list(value.items()):
                if child_key != "content":
                    visit(child, str(child_key), role=role)
            return
        for child_key, child in list(value.items()):
            if isinstance(child, str) and child_key in {
                "content", "message", "owner_question", "question", "summary", "text",
            }:
                if role == "assistant":
                    value[child_key] = redact_text(child)
            else:
                visit(child, str(child_key), role=role)

    for message in copied:
        visit(message, role=str(message.get("role") or ""))
    return copied


def _atomic_json_write(path: Path, value: Mapping[str, object]) -> None:
    """原子写入排序后的 JSON checkpoint。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if path.is_file() and path.read_text(encoding="utf-8") == payload:
        return
    pending = path.with_name(f".{path.name}.{uuid.uuid4().hex}.pending")
    try:
        with pending.open("x", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, path)
        _fsync_directory(path.parent)
    finally:
        pending.unlink(missing_ok=True)


def _atomic_jsonl_write(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    """原子写入一行一个 segment 的 JSONL 索引。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for row in rows
    )
    pending = path.with_name(f".{path.name}.{uuid.uuid4().hex}.pending")
    try:
        with pending.open("x", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def export_workspace(root: str | Path) -> dict[str, object]:
    """只处理已完成 raw；不调用模型，不修改页面、运行终态或恢复点。"""
    from export.delivery import DELIVERY_MANIFEST_SCHEMA, DeliveryExporter
    from orchestration.manager import assert_tasks_terminal
    from state.locking import workspace_lock
    root = Path(root).resolve()
    # 先读完成凭证，不通过构造 WorkspaceState 意外创建一个空工作区。
    result_path = root / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    run = json.loads((root / "_state/run.json").read_text(encoding="utf-8"))
    if (result.get("status") != "completed" or result.get("artifact_status") != "complete"
            or run.get("run_mode") != "synthesis"):
        raise TrainingExportError("offline training export requires completed synthesis raw")
    with workspace_lock(root):
        if (json.loads(result_path.read_text(encoding="utf-8")) != result
                or json.loads((root / "_state/run.json").read_text(encoding="utf-8")) != run):
            raise TrainingExportError("workspace changed while waiting for offline export")
        generation = result.get("generation") or {}
        if generation.get("schema") != "mural.generation.v1":
            raise TrainingExportError("completed raw is missing its generation receipt")
        state_path = root / "_state/orchestration.json"
        if state_path.is_file():
            assert_tasks_terminal(json.loads(state_path.read_text(encoding="utf-8")))
        host_path = root / "_state/skill-runtime.json"
        if host_path.is_file() and json.loads(host_path.read_text()).get("status") != "completed":
            raise TrainingExportError("workspace is active or resumable; finish generation before training export")
        workspace = WorkspaceState(root)
        artifacts, issues = ArtifactStore(root, workspace_state=workspace), IssueLedger(root)
        delivery = DeliveryManifest.from_dict({**generation, "schema": DELIVERY_MANIFEST_SCHEMA})
        DeliveryExporter(workspace, artifacts, issues).verify(delivery)
        exporter = TrainingExporter(workspace, artifacts, issues, run_mode="synthesis")
        segments = exporter.load_segments()
        if not segments:
            raise TrainingExportError("completed raw has no preserved context segments")
        return exporter.export(segments, delivery, critic=result.get("external_visual_critic"))


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="已完成 raw 的批量训练导出；不运行 QC 或模型")
    parser.add_argument("workspaces", nargs="+", type=Path)
    arguments = parser.parse_args(argv)
    results = []
    for root in arguments.workspaces:
        try:
            manifest = export_workspace(root)
        except (OSError, ValueError, RuntimeError) as exc:
            results.append({"workspace": str(root), "status": "error", "error": str(exc)})
        else:
            results.append({"workspace": str(root), "status": "exported",
                            "training_admission": manifest["training_admission"],
                            "segment_count": manifest["segment_count"]})
    print(json.dumps(results, ensure_ascii=False))
    return int(any(row["status"] == "error" for row in results))


if __name__ == "__main__":
    raise SystemExit(main())
