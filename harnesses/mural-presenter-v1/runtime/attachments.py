"""确定性暂存附件为 originals/extracted 双区布局，并生成路由与解析事实。

Harness 只负责复制、提取和记录文件事实，不替 Agent 摘要内容或决定页面用途。原件区
``inputs/originals/att-<hash>/`` 不可变；机器解析产物（文本、逻辑页图、内嵌媒体）落在
``inputs/extracted/att-<hash>/``。附件身份由完整 SHA-256 决定：同内容→同 id、同目录、resume
不重复解析；同名不同内容→不同 id、不覆盖旧件。上传顺序只进入 ``ordinal``，绝不参与身份。
"""
from __future__ import annotations

from dataclasses import dataclass
import datetime as _dt
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import time
from typing import Mapping, Sequence
import uuid
from functools import wraps
import threading


def _path_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


ATTACHMENT_SCHEMA = "mural.attachments.v1"
ATTACHMENT_SCHEMA_VERSION = 2
ATTACHMENT_LAYOUT_VERSION = "originals-extracted-v1"
_HASH_PREFIX_LEN = 12

DIRECT_TEXT_SUFFIXES = frozenset(
    {".md", ".markdown", ".txt", ".csv", ".tsv", ".json", ".jsonl"}
)
IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
)
OFFICE_SUFFIXES = frozenset({".docx", ".pptx", ".xlsx"})

# 用户可显式声明的附件用途。只保留少量确定语义,绝不恢复 v1.0 的内容猜测规则:
# ``style_reference`` = 仅作视觉/风格参照(不抽正文),``content_source`` = 仅作事实内容
# (不作视觉复用),``mixed`` = 既是内容也可视觉复用(等同没给 intent 时的保守默认)。
EXPLICIT_INTENTS = frozenset({"style_reference", "content_source", "mixed"})


class AttachmentError(RuntimeError):
    """表示附件复制、提取或页图物化无法满足机械合同。"""


@dataclass(frozen=True)
class AttachmentManifest:
    """保存附件暂存后的唯一结构化路由结果。"""

    path: Path
    value: Mapping[str, object]

    def to_dict(self) -> dict[str, object]:
        """返回调用方不可修改的 JSON 副本。"""
        return json.loads(json.dumps(dict(self.value), ensure_ascii=False))


@dataclass(frozen=True)
class AttachmentRecord:
    """附件的逻辑解析视图：调用方只用它与 manifest，不字符串拼接派生路径。"""

    root: Path
    attachment_id: str
    sha256: str
    ordinal: int
    original_name: str
    original_path: str
    extracted_root: str
    kind: str
    text_path: str | None
    page_image_paths: tuple[str, ...]
    media_paths: tuple[str, ...]

    def to_manifest_entry(self) -> dict[str, object]:
        """返回写入 manifest ``attachments[]`` 的稳定条目（仅工作区相对路径）。"""
        return {
            "attachment_id": self.attachment_id,
            "sha256": self.sha256,
            "ordinal": self.ordinal,
            "original_name": self.original_name,
            "original_path": self.original_path,
            "extracted_root": self.extracted_root,
        }


def _now_iso() -> str:
    """返回 UTC 秒级 ISO 时间戳。"""
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


class AttachmentStager:
    """把入口附件转成 originals/extracted 双区文件、提取稿和 manifest。"""

    def __init__(self, workspace: str | Path) -> None:
        """绑定工作区并创建 originals/extracted 与 trace 目录。"""
        self.root = Path(workspace).expanduser().resolve()
        self.inputs = self.root / "inputs"
        self.originals = self.inputs / "originals"
        self.extracted = self.inputs / "extracted"
        self.trace = self.root / "_trace"
        for directory in (self.originals, self.extracted, self.trace):
            directory.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.trace / "attachment-manifest.json"

    def _load_prior(self) -> dict[str, dict[str, object]]:
        """读取既有 manifest 的 sha256→attachment 条目，供 resume 去重。"""
        if not self.manifest_path.is_file():
            return {}
        try:
            value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        prior: dict[str, dict[str, object]] = {}
        details = {item.get("attachment_id"): item for item in value.get("records", []) if isinstance(item, Mapping)}
        for item in (value.get("attachments") or []):
            if isinstance(item, Mapping) and item.get("sha256"):
                prior[str(item["sha256"])] = dict(item)
                detail = details.get(item.get("attachment_id"), {})
                if detail.get("extraction_error"):
                    prior[str(item["sha256"])]["extraction_error"] = detail["extraction_error"]
                if detail.get("parse"):
                    prior[str(item["sha256"])].update(
                        parse=detail["parse"], companion_path=detail.get("companion_path"),
                        media_paths=detail.get("media_paths", []))
        return prior

    def stage(
        self,
        attachments: Sequence[str | Path],
        *,
        intents: Sequence[str | None] | None = None,
    ) -> AttachmentManifest:
        """复制全部入口文件到 originals，提取衍生物到 extracted，并原子写出 manifest。

        ``intents`` 与 ``attachments`` 顺序一一对应，取值须落在 ``EXPLICIT_INTENTS`` 内；
        不给（None 或空串）时该件保持保守默认（图片=mixed），绝不猜测语义。
        """
        prior = self._load_prior()
        normalized_intents = self._normalize_intents(attachments, intents)
        records: list[dict[str, object]] = []
        resolver_entries: list[dict[str, object]] = []
        failures: list[dict[str, str]] = []
        by_id: dict[str, dict[str, object]] = {}
        collisions: dict[str, str] = {}
        active_ids: list[str] = []

        for ordinal, raw_path in enumerate(attachments, start=1):
            intent = normalized_intents[ordinal - 1]
            source = Path(raw_path).expanduser().resolve()
            if not source.is_file():
                raise AttachmentError(f"附件不存在：{source}")
            payload = source.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            attachment_id = f"att-{digest[:_HASH_PREFIX_LEN]}"
            prev_hash = collisions.get(attachment_id)
            if prev_hash is not None and prev_hash != digest:
                raise AttachmentError(
                    f"attachment id 前缀碰撞：{attachment_id} 同时映射到不同内容"
                )
            collisions[attachment_id] = digest

            original_dir = self.originals / attachment_id
            extracted_dir = self.extracted / attachment_id
            canonical = by_id.get(attachment_id)
            prior_entry = prior.get(digest)
            target = (self.root / str(canonical["path"]) if canonical is not None else
                      self.root / str(prior_entry["original_path"]) if prior_entry is not None else
                      original_dir / source.name)
            if not target.resolve().is_relative_to(original_dir.resolve()):
                raise AttachmentError(f"附件 canonical 原件路径不属于当前内容身份：{attachment_id}")
            # 同名不同内容会落在不同 hash 目录，永不覆盖；同 hash 目录内同名即同件。
            original_dir.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(source, target)
            elif target.stat().st_size != len(payload):
                if canonical is not None or prior_entry is not None:
                    raise AttachmentError(f"附件 canonical 原件与已暂存身份不一致：{attachment_id}")
                # 同 id 目录内出现同名但字节不同：极端 hash 前缀内碰撞，另存全名后缀。
                target = original_dir / f"{digest[:_HASH_PREFIX_LEN]}_{source.name}"
                if not target.exists():
                    shutil.copy2(source, target)

            record = self._record(target, digest, attachment_id, extracted_dir, intent)
            if prior_entry and prior_entry.get("parse"):
                record["parse"] = prior_entry["parse"]
                previous = record["parse"]["results"][record["parse"]["current"]]["receipt"]
                record.update(parse_status=previous["status"], companion_path=previous["text_path"],
                              media_paths=previous["media_paths"])
            reuse = bool(prior_entry and not prior_entry.get("extraction_error")) or canonical is not None
            if record["kind"] == "document":
                if reuse:
                    self._reattach_document(record, extracted_dir)
                    if prior_entry and prior_entry.get("parse") and prior_entry.get("companion_path"):
                        record["companion_path"] = prior_entry["companion_path"]
                        record["media_paths"] = list(dict.fromkeys(
                            [*record.get("media_paths", []), *prior_entry.get("media_paths", [])]))
                else:
                    try:
                        record["page_image_paths"] = self._index_document(target, extracted_dir)
                        record["media_paths"] = []
                        record["page_preview_mode"] = "lazy_document_raster"
                    except Exception as exc:  # 已知失败进 manifest 供 Research 收口。
                        detail = "unsupported_format" if target.suffix.lower() not in {".pdf", *OFFICE_SUFFIXES} else "attachment_index_failed"
                        record["extraction_error"] = detail
                        failures.append(
                            {"path": str(record["path"]), "error": detail}
                        )
            records.append(record)
            resolver_entries.append(
                {
                    "attachment_id": attachment_id,
                    "sha256": digest,
                    "ordinal": ordinal,
                    "original_name": source.name,
                    "original_path": self._relative(target),
                    "extracted_root": self._relative(extracted_dir)
                    if extracted_dir.is_dir()
                    else self.inputs.name + "/extracted/" + attachment_id,
                }
            )
            if attachment_id not in by_id:
                by_id[attachment_id] = record
                active_ids.append(attachment_id)

        value = self._build_manifest(
            records, resolver_entries, active_ids, failures
        )
        _atomic_json_write(self.manifest_path, value)
        return AttachmentManifest(path=self.manifest_path, value=value)

    def _normalize_intents(
        self,
        attachments: Sequence[str | Path],
        intents: Sequence[str | None] | None,
    ) -> list[str | None]:
        """把可选 intents 对齐到附件序列，并只接受显式受支持取值。"""
        if intents is None:
            return [None] * len(attachments)
        if len(intents) != len(attachments):
            raise AttachmentError(
                "attachment intents must align one-to-one with attachments"
            )
        resolved: list[str | None] = []
        for value in intents:
            token = str(value or "").strip().lower().replace("-", "_")
            if not token:
                resolved.append(None)
                continue
            if token not in EXPLICIT_INTENTS:
                raise AttachmentError(f"unsupported attachment intent: {value!r}")
            resolved.append(token)
        return resolved

    def _input_set_id(self, resolver_entries: Sequence[Mapping[str, object]]) -> str:
        """由附件集合（sha256 + 原始名）派生 canonical 集合哈希；集合变则变。"""
        digest = hashlib.sha256()
        for entry in sorted(
            resolver_entries, key=lambda item: (str(item["sha256"]), str(item["original_name"]))
        ):
            digest.update(str(entry["sha256"]).encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(entry["original_name"]).encode("utf-8"))
            digest.update(b"\0")
        return "attset-" + digest.hexdigest()[:16]

    def _build_manifest(
        self,
        records: list[dict[str, object]],
        resolver_entries: list[dict[str, object]],
        active_ids: list[str],
        failures: list[dict[str, str]],
    ) -> dict[str, object]:
        """把逐件记录汇成 manifest：v2 身份 + 既有派生路由（新布局路径）。"""
        content_records = [
            item for item in records if bool(item.get("needs_content_extraction"))
        ]
        direct_records = [item for item in records if item["kind"] == "direct_text"]
        if content_records:
            input_stage = (
                "mixed"
                if any(item.get("needs_visual_reuse") for item in records)
                else "parse_pending"
            )
            attachment_paths: list[str] = []
            for item in records:
                if item.get("needs_content_extraction") or item["kind"] == "direct_text":
                    attachment_paths.append(str(item["path"]))
                    companion = str(item.get("companion_path") or "")
                    if companion:
                        attachment_paths.append(companion)
        elif records:
            input_stage = "direct_text" if direct_records else "direct_manifest"
            attachment_paths = [str(item["path"]) for item in direct_records]
        else:
            input_stage = "omitted"
            attachment_paths = []

        visual_paths = [
            str(item["path"])
            for item in records
            if item["kind"] == "image" and item.get("needs_visual_reuse")
        ]
        style_reference_paths = [
            str(item["path"])
            for item in records
            if item.get("needs_style_reference")
        ]
        now = _now_iso()
        created_at = now
        if self.manifest_path.is_file():
            try:
                prior_value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                created_at = str(prior_value.get("created_at") or now)
            except (OSError, json.JSONDecodeError):
                created_at = now
        return {
            "schema": ATTACHMENT_SCHEMA,
            "schema_version": ATTACHMENT_SCHEMA_VERSION,
            "layout_version": ATTACHMENT_LAYOUT_VERSION,
            "input_set_id": self._input_set_id(resolver_entries),
            "attachments": resolver_entries,
            "active_attachment_ids": active_ids,
            "created_at": created_at,
            "updated_at": now,
            "attachment_count": len(records),
            "input_stage": input_stage,
            "content_extraction_required": bool(content_records),
            "attachment_paths": attachment_paths,
            "direct_text_paths": [str(item["path"]) for item in direct_records],
            "visual_asset_paths": visual_paths,
            "style_reference_paths": style_reference_paths,
            "unresolved_items": failures,
            "records": records,
        }

    def _record(
        self,
        target: Path,
        digest: str,
        attachment_id: str,
        extracted_dir: Path,
        explicit_intent: str | None = None,
    ) -> dict[str, object]:
        """根据文件本身记录类型、哈希、MIME 和路由；显式 intent 覆盖保守默认。"""
        suffix = target.suffix.lower()
        if suffix in DIRECT_TEXT_SUFFIXES:
            kind = intent = "direct_text"
            content = False
            visual = False
        elif suffix in IMAGE_SUFFIXES:
            # 没有显式 UI intent 时保守记为 mixed：既不丢像素，也不让 Harness 猜其语义。
            kind = "image"
            intent = "mixed"
            content = True
            visual = True
        else:
            kind = "document"
            intent = "evidence"
            content = True
            visual = False
        needs_style = False
        # 只在用户显式声明时改写路由，绝不从 query/内容反推；direct_text 由 Research 读原件。
        if kind != "direct_text":
            if explicit_intent == "style_reference":
                intent = "style_reference"
                needs_style = True
                visual = True
                content = False
            elif explicit_intent == "content_source":
                intent = "content_source"
                content = True
                visual = False
            elif explicit_intent == "mixed":
                intent = "mixed"
                content = True
                visual = kind == "image"
        record: dict[str, object] = {
            "attachment_id": attachment_id,
            "path": self._relative(target),
            "extracted_root": self.inputs.name + "/extracted/" + attachment_id,
            "name": target.name,
            "kind": kind,
            "intent": intent,
            "bytes": target.stat().st_size,
            "sha256": digest,
            "mime": mimetypes.guess_type(target.name)[0] or "application/octet-stream",
            "needs_content_extraction": content,
            "needs_visual_reuse": visual,
            "needs_style_reference": needs_style,
        }
        if kind == "document":
            record["parse_status"] = "not_requested"
            record["page_image_pattern"] = f"inputs/extracted/{attachment_id}/pages/page_NNN.png"
        if kind == "image":
            try:
                from PIL import Image

                with Image.open(target) as image:
                    record["pixels"] = [int(image.width), int(image.height)]
                    record["mode"] = str(image.mode)
            except Exception as exc:
                record["inspection_error"] = f"{type(exc).__name__}: {exc}"
        return record

    def _reattach_document(
        self, record: dict[str, object], extracted_dir: Path
    ) -> None:
        """resume：不重新解析，从既有 extracted 目录恢复文本/页图/媒体引用。"""
        pages_dir = extracted_dir / "pages"
        # 页图的 canonical 来源是每页的 ``.json`` 描述文件（物化与否都稳定、按页排序），
        # 不依赖某些 PNG 是否已被 vision 请求物化。
        record["page_image_paths"] = (
            [
                self._relative(item.with_suffix(".png"))
                for item in sorted(pages_dir.glob("page_*.json"))
            ]
            if pages_dir.is_dir()
            else []
        )
        media_dir = extracted_dir / "media"
        record["media_paths"] = (
            [self._relative(item) for item in sorted(media_dir.iterdir()) if item.is_file()]
            if media_dir.is_dir()
            else []
        )
        record["page_preview_mode"] = "lazy_document_raster"
        record["resumed_extraction"] = True

    def _index_document(
        self, source: Path, extracted_dir: Path
    ) -> list[str]:
        """暂存只做 PDF 页索引；Office 的全文解析和预览转换都不阻塞启动。"""
        suffix = source.suffix.lower()
        extracted_dir.mkdir(parents=True, exist_ok=True)
        if suffix == ".pdf":
            return self._index_pdf(source, extracted_dir)
        if suffix in OFFICE_SUFFIXES:
            return []
        raise AttachmentError("unsupported_format")

    def _pages_dir(self, extracted_dir: Path) -> Path:
        """返回逻辑页图目录并确保存在。"""
        pages = extracted_dir / "pages"
        pages.mkdir(parents=True, exist_ok=True)
        return pages

    def _index_pdf(
        self, source: Path, extracted_dir: Path
    ) -> list[str]:
        """记录原件页数、几何与非空文本层事实；不保存全文、不做 OCR 或生成 PNG。"""
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz  # type: ignore[no-redef]

        document = fitz.open(source)
        page_dir = self._pages_dir(extracted_dir)
        page_paths: list[str] = []
        try:
            for index, page in enumerate(document, start=1):
                logical = page_dir / f"page_{index:03d}.png"
                descriptor = {"source_path": self._relative(source), "page": index,
                    "page_points": [float(page.rect.width), float(page.rect.height)],
                    "page_image": self._relative(logical), "page_preview_mode": "lazy_pdf_raster",
                    "text_mode": "unparsed", "text_blocks": [], "visual_regions": [],
                    "native_text_present": bool(page.get_text("text").strip())}
                _atomic_json_write(logical.with_suffix(".json"), descriptor)
                logical.unlink(missing_ok=True)
                page_paths.append(self._relative(logical))
        finally:
            document.close()
        return page_paths

    def _pdf_page_descriptor(self, page, source: Path, logical: Path) -> tuple[dict, str]:
        """PDF 与 Office 共用真实布局证据；旋转后的页框与实际栅格坐标对齐。"""
        import pymupdf as fitz

        def bbox(rect):
            return list(fitz.Rect(rect) * page.rotation_matrix)

        native = page.get_text("text").strip()
        text_blocks = [
            {"bbox_pdf": bbox(row[:4]), "text": row[4], "chars": len(row[4])}
            for row in page.get_text("words")
        ]
        images = [{"bbox_pdf": bbox(row["bbox"])} for row in page.get_image_info()]
        descriptor = {
            "source_path": self._relative(source), "page": page.number + 1,
            "page_points": [float(page.rect.width), float(page.rect.height)],
            "page_image": self._relative(logical), "page_preview_mode": "lazy_pdf_raster",
            "text_mode": "native" if native else "missing",
            "native_text_present": bool(native),
            "native_text": native,
            "text_blocks": text_blocks, "image_blocks": images,
            "raster_page": bool(images) and (not native or all(
                fitz.Rect(row["bbox_pdf"]).width >= page.rect.width * 0.98 for row in images)),
            "drawing_blocks": [{"bbox_pdf": bbox(row["rect"])} for row in page.get_drawings()],
        }
        return descriptor, native


    def _office_page_descriptors(
        self, source: Path, extracted_dir: Path
    ) -> list[str]:
        """禁宏、禁外链的原件只读预览；转换页不冒充 Office 的逻辑位置。"""
        from . import mineru, office

        result = office.derive(source, self.root / "_runtime/tmp/office-preview",
                               mineru.source_info(source), preview=True)
        raw = result["files"].get("office/preview.pdf")
        if raw is None:
            return []
        page_dir = self._pages_dir(extracted_dir)
        backing = page_dir / "source.pdf"
        _atomic_bytes_write(backing, raw)
        return self._pdf_descriptors_from_backing(source, backing, page_dir)

    def _pdf_descriptors_from_backing(
        self, source: Path, backing: Path, page_dir: Path
    ) -> list[str]:
        """为 Office 转换 PDF 的每页建立可复现逻辑路径。"""
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz  # type: ignore[no-redef]

        document = fitz.open(backing)
        paths: list[str] = []
        try:
            for index, page in enumerate(document, start=1):
                logical = page_dir / f"page_{index:03d}.png"
                descriptor = {"source_path": self._relative(source), "page": index,
                    "page_points": [float(page.rect.width), float(page.rect.height)],
                    "page_image": self._relative(logical), "backing_pdf": self._relative(backing),
                    "page_preview_mode": "lazy_office_pdf_raster", "location_kind": "converted_page"}
                _atomic_json_write(
                    logical.with_suffix(".json"),
                    descriptor,
                )
                logical.unlink(missing_ok=True)
                paths.append(self._relative(logical))
        finally:
            document.close()
        return paths

    def _relative(self, path: Path) -> str:
        """把工作区内路径转换为 POSIX 相对路径并拒绝逃逸。"""
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError as exc:
            raise AttachmentError(f"附件衍生路径逃逸工作区：{path}") from exc


# --- Unified resolver: callers use only these + manifest, never string-concat. ---


def _load_manifest(workspace: str | Path) -> tuple[Path, dict[str, object]]:
    """读取工作区 ``_trace/attachment-manifest.json``。"""
    root = Path(workspace).expanduser().resolve()
    path = root / "_trace" / "attachment-manifest.json"
    if not path.is_file():
        return root, {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AttachmentError(f"附件 manifest 根必须是对象：{path}")
    return root, value


def resolve_attachment_records(workspace: str | Path) -> list[AttachmentRecord]:
    """从 manifest 解析全部逻辑附件记录（按 ordinal 排序）。"""
    root, manifest = _load_manifest(workspace)
    entries = manifest.get("attachments") or []
    detail_by_id = {
        str(item.get("attachment_id")): item
        for item in (manifest.get("records") or [])
        if isinstance(item, Mapping)
    }
    records: list[AttachmentRecord] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        attachment_id = str(entry.get("attachment_id") or "")
        detail = detail_by_id.get(attachment_id, {})
        text_path = detail.get("companion_path")
        records.append(
            AttachmentRecord(
                root=root,
                attachment_id=attachment_id,
                sha256=str(entry.get("sha256") or ""),
                ordinal=int(entry.get("ordinal") or 0),
                original_name=str(entry.get("original_name") or ""),
                original_path=str(entry.get("original_path") or ""),
                extracted_root=str(entry.get("extracted_root") or ""),
                kind=str(detail.get("kind") or "document"),
                text_path=str(text_path) if text_path else None,
                page_image_paths=tuple(
                    str(item) for item in (detail.get("page_image_paths") or [])
                ),
                media_paths=tuple(
                    str(item) for item in (detail.get("media_paths") or [])
                ),
            )
        )
    records.sort(key=lambda record: record.ordinal)
    return records


def resolve_original_path(record: AttachmentRecord) -> Path:
    """返回原始输入文件的绝对路径。"""
    if not record.original_path:
        raise AttachmentError(f"附件 {record.attachment_id} 缺少 original_path")
    return record.root / record.original_path


def resolve_extracted_text(record: AttachmentRecord) -> Path | None:
    """返回机器提取文本 ``text.md`` 的绝对路径；无则返回 None。"""
    if not record.text_path:
        return None
    return record.root / record.text_path


def resolve_page_assets(record: AttachmentRecord) -> list[Path]:
    """返回逻辑页图路径（按需物化）。task F 用它触发 PDF/Office/图片页物化。

    返回的路径可能尚未物化：调用方对每条调用 ``materialize_page_image`` 即得真实 PNG。
    图片类附件没有分页，回退到原始文件本身。
    """
    if record.page_image_paths:
        return [record.root / rel for rel in record.page_image_paths]
    if record.kind == "image":
        return [resolve_original_path(record)]
    return []




def _attachment_path(record: AttachmentRecord, relative: str, *, derived: bool = False) -> Path:
    path = record.root / relative
    extracted = record.root / "inputs/extracted" / record.attachment_id
    originals = record.root / "inputs/originals" / record.attachment_id
    if (not re.fullmatch(r"att-[0-9a-f]{12}", record.attachment_id)
            or record.root / record.extracted_root != extracted
            or not (path.resolve().is_relative_to(extracted)
                    or (not derived and path.resolve().is_relative_to(originals)))):
        raise AttachmentError("附件解析路径逃逸当前附件")
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != record.root):
        raise AttachmentError("附件解析路径不得使用符号链接")
    return path


def _set_attachment_parse(record: AttachmentRecord, parse: dict) -> None:
    root, manifest = _load_manifest(record.root)
    receipt = parse["results"][parse["current"]]["receipt"]
    for detail in manifest.get("records", []):
        if detail.get("attachment_id") == record.attachment_id:
            detail.update(parse=parse, parse_status=receipt["status"],
                          companion_path=receipt["text_path"], media_paths=receipt["media_paths"])
    if receipt["text_path"]:
        manifest["attachment_paths"] = list(dict.fromkeys(
            [*manifest.get("attachment_paths", []), receipt["text_path"]]))
    _atomic_json_write(root / "_trace/attachment-manifest.json", manifest)


def _pdf_text_layer_evidence(record: AttachmentRecord | None, receipt: Mapping[str, object]) -> dict:
    """只读复用同原件的页索引；缺探测证据不把解析器文字或全局 OCR 标志当成原生。"""
    if (record is None or Path(record.original_path).suffix.lower() != ".pdf"
            or receipt.get("original_path") != record.original_path
            or receipt.get("source_sha256") != record.sha256):
        return {}
    evidence = {}
    for relative in record.page_image_paths:
        descriptor_relative = Path(relative).with_suffix(".json").as_posix()
        path = _attachment_path(record, descriptor_relative, derived=True)
        try:
            descriptor = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if (not isinstance(descriptor, dict) or descriptor.get("source_path") != record.original_path
                or type(descriptor.get("page")) is not int or descriptor["page"] < 1
                or relative != f"{record.extracted_root}/pages/page_{descriptor['page']:03d}.png"
                or descriptor.get("page_image") != relative or descriptor.get("backing_pdf")):
            continue
        present = descriptor.get("native_text_present")
        if type(present) is not bool:
            native = descriptor.get("native_text")
            if (isinstance(native, str)
                    and descriptor.get("text_mode") == ("native" if native.strip() else "missing")):
                present = bool(native.strip())
            else:
                continue
        evidence[descriptor["page"]] = {"native_text_present": present,
                                      "native_text_evidence_path": descriptor_relative}
    return evidence


def _source_location(receipt: Mapping[str, object], index: int, *, content_index: int | None = None,
                     page_evidence: Mapping[int, dict] | None = None) -> dict:
    coverage = receipt["coverage"]
    unit = coverage["granularity"]
    location = {"original_path": receipt["original_path"]}
    if unit == "page":
        location.update(page=index + 1, page_image_path=receipt["page_image_pattern"].replace("NNN", f"{index + 1:03d}"))
        location.update(native_text_present=None, native_text_evidence_path=None)
        location.update((page_evidence or {}).get(index + 1, {}))
    elif unit == "image":
        location["page_image_path"] = receipt["original_path"]
    elif unit == "document_block":
        if content_index is not None:
            location[unit] = content_index + 1
    else:
        location[unit] = coverage["expected_units"][index]
    return location


def attachment_receipt_view(receipt: dict, *, record: AttachmentRecord | None = None) -> dict:
    """展示事实从已固定解析结果派生；不生成第二份清单或修改原解析文件。"""
    receipt = {**receipt, "media_count": len(receipt["media_paths"])}
    if not receipt.get("content_list_path"):
        return receipt
    coverage = receipt["coverage"]
    page_evidence = _pdf_text_layer_evidence(record, receipt) if coverage["granularity"] == "page" else {}
    locations = []
    for item in coverage.get("locations", []):
        if coverage["granularity"] == "document_block":
            location = _source_location(receipt, 0, content_index=item["block"])
        else:
            index = coverage["expected_units"].index(item[coverage["granularity"]])
            location = _source_location(receipt, index, page_evidence=page_evidence)
        locations.append({**item, **location})
    return {**receipt,
        "coverage": {**coverage, "locations": locations},
        "content_list_view": {"path": receipt["content_list_path"], "format": "source_locations_jsonl",
            "read_with": "read_file", "offset_unit": "content_item",
            "note": "One JSON item per line; source locations and full workspace-relative paths are ready to use. "
                    "crop_box applies to crop_source_path, never the extracted img_path; it is a parser proposal in 0–1, "
                    "not visual verification. Null means Image must locate it on the source. "
                    "native_text_present is a page-level check for nonblank extractable text in the original PDF, without OCR. "
                    "True does not prove any parsed block is native; false means parsed words on that page are not native PDF text; "
                    "null means no page evidence. MinerU type=text and document-level OCR flags do not establish block provenance. "
                    "Oversized item bodies are explicitly excerpted; use full_text_path for the complete text."}}


def attachment_content_view(workspace: str | Path, relative: str) -> dict | None:
    """read_file 的只读内容视图：页码、路径与坐标由程序换算，原始 JSON 保持不变。"""
    if not relative.startswith("inputs/extracted/") or not relative.endswith("/content-list.json"):
        return None
    root, manifest = _load_manifest(workspace)
    for detail in manifest.get("records", []):
        for entry in detail.get("parse", {}).get("results", {}).values():
            receipt = entry["receipt"]
            if receipt.get("content_list_path") != relative:
                continue
            record = next(record for record in resolve_attachment_records(root)
                          if record.attachment_id == detail["attachment_id"])
            raw = _attachment_path(record, relative, derived=True).read_bytes()
            if hashlib.sha256(raw).hexdigest() != entry["outputs"].get(relative):
                raise AttachmentError("attachment_content_changed: fixed content list hash mismatch")
            rows = json.loads(raw)
            base = Path(relative).parent
            media = set(receipt["media_paths"])
            page_evidence = _pdf_text_layer_evidence(record, receipt) if receipt["coverage"]["granularity"] == "page" else {}
            lines, search_lines = [], []
            for index, row in enumerate(rows):
                location = _source_location(receipt, row["page_idx"], content_index=index, page_evidence=page_evidence)
                item = {key: value for key, value in row.items() if key not in {"page_idx", "bbox"}}
                for key, value in tuple(item.items()):
                    if key.endswith("img_path") or key == "image_path":
                        value = value if isinstance(value, str) else None
                        if key == "image_path" and value and not value.startswith("images/"):
                            value = "images/" + value
                        path = (base / value).as_posix() if value else None
                        item[key] = path if path in media and _attachment_path(record, path, derived=True).is_file() else None
                bbox = row.get("bbox")
                valid_box = (receipt["coverage"].get("content_bbox_units") == "normalized_0_1000"
                    and isinstance(bbox, list) and len(bbox) == 4
                    and all(type(value) in {int, float} and 0 <= value <= 1000 for value in bbox)
                    and 0 <= bbox[0] < bbox[2] <= 1000 and 0 <= bbox[1] < bbox[3] <= 1000)
                item.update(source={**location, "content_pointer": f"/{index}"},
                            crop_box=[value / 1000 for value in bbox] if valid_box else None,
                            crop_source_path=location.get("page_image_path") if valid_box else None)
                line = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                search_lines.append(line)
                if len(line.encode("utf-8")) > 16 * 1024:
                    # 只缩略内容预览，不截断引用；完整正文和原始 JSON 仍留在解析结果中。
                    item = {key: value for key, value in item.items()
                            if key in {"type", "source", "crop_box", "crop_source_path", "img_path", "image_path"}}
                    item.update(content_excerpt=json.dumps(row, ensure_ascii=False)[:2000],
                                content_truncated=True, full_text_path=receipt["text_path"])
                    line = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                lines.append(line)
            return {"content": "\n".join(lines), "_search_lines": search_lines, "format": "source_locations_jsonl",
                "offset_unit": "content_item", "source_sha256": receipt["source_sha256"],
                "note": attachment_receipt_view(receipt)["content_list_view"]["note"]}
    return None


def attachment_excerpt_media(
    workspace: str | Path,
    relative: str,
    *,
    selected_indices: Sequence[int],
    excerpt: str,
) -> list[dict[str, object]]:
    """Resolve MinerU media directly related to a returned text/content-list excerpt.

    Content-list reads include visual rows on the same source pages as selected text so a
    section query can return its figure without a second page-level vision search. Markdown
    reads include only image references present in the returned excerpt.
    """
    if not relative.startswith("inputs/extracted/"):
        return []
    root, manifest = _load_manifest(workspace)
    from .mineru import image_references

    for detail in manifest.get("records", []):
        for entry in detail.get("parse", {}).get("results", {}).values():
            receipt = entry.get("receipt", {})
            if relative not in {receipt.get("content_list_path"), receipt.get("text_path")}:
                continue
            outputs = entry.get("outputs", {})
            expected = outputs.get(relative)
            source = root / relative
            if not expected or not source.is_file() or _path_sha256(source) != expected:
                raise AttachmentError("attachment_content_changed: fixed MinerU text hash mismatch")
            allowed = set(receipt.get("media_paths") or [])
            candidates: list[tuple[str, int | None]] = []
            if relative == receipt.get("content_list_path"):
                rows = json.loads(source.read_bytes())
                selected = [rows[index] for index in selected_indices if 0 <= index < len(rows)]
                pages = {row.get("page_idx") for row in selected if type(row.get("page_idx")) is int}
                related = [*selected, *[
                    row for row in rows
                    if row not in selected and row.get("page_idx") in pages
                    and row.get("type") in {"image", "chart"}
                ]]
                for row in related:
                    page = row.get("page_idx")
                    for reference in image_references(row):
                        candidates.append((str(reference), int(page) + 1 if type(page) is int else None))
            else:
                candidates.extend((str(reference), None) for reference in image_references(excerpt))

            base = Path(relative).parent
            resolved: list[dict[str, object]] = []
            seen = set()
            for reference, page in candidates:
                path = reference if reference in allowed else (base / reference).as_posix()
                if path in seen or path not in allowed:
                    continue
                target = root / path
                digest = outputs.get(path)
                if not digest or not target.is_file() or _path_sha256(target) != digest:
                    raise AttachmentError("attachment_media_changed: fixed MinerU image hash mismatch")
                seen.add(path)
                resolved.append({"path": path, **({"page": page} if page is not None else {})})
            return resolved
    return []


_PARSE_LOCKS: dict[tuple[str, str], object] = {}
_PARSE_GUARD = threading.Lock()


def _set_attachment_parse_status(root: Path, attachment_id: str, value: str, receipt=None) -> None:
    from state.locking import workspace_lock

    with workspace_lock(root):
        _, manifest = _load_manifest(root)
        detail = next((item for item in manifest.get("records", [])
                       if item["attachment_id"] == attachment_id), None)
        if detail is None:
            raise AttachmentError(f"附件不存在：{attachment_id}")
        if value == "queued" and detail.get("parse_status") == "processing":
            return
        detail["parse_status"] = value
        if receipt is not None and not detail.get("parse", {}).get("current"):
            key = ("failed-" if value == "failed" else "direct-") + detail["sha256"]
            detail["parse"] = {"current": key, "results": {key: {"receipt": receipt, "outputs": {}}}}
        elif receipt is not None and value == "failed":
            prior = detail.setdefault("parse", {})
            key = "failed-" + detail["sha256"]
            prior.setdefault("results", {})[key] = {"receipt": receipt, "outputs": {}}
            prior["current"] = key
        _atomic_json_write(root / "_trace/attachment-manifest.json", manifest)


def queue_attachment_parse(workspace: str | Path, attachment_id: str) -> None:
    """后台排队只更新既有 manifest；解析、事件与回执仍由唯一入口负责。"""
    _set_attachment_parse_status(Path(workspace).expanduser().resolve(), attachment_id, "queued")


def _serialized_attachment_parse(function):
    @wraps(function)
    def parse(workspace, attachment_id, *, event_sink=None, environment=None):
        from state.locking import WorkspaceLock

        root = Path(workspace).expanduser().resolve()
        record = next((item for item in resolve_attachment_records(root)
                       if item.attachment_id == attachment_id), None)
        if record is None or not re.fullmatch(r"att-[0-9a-f]{12}", attachment_id):
            raise AttachmentError(f"附件不存在：{attachment_id}")

        def status(value, receipt=None):
            _set_attachment_parse_status(root, attachment_id, value, receipt)

        def emit(name, value, **extra):
            if event_sink:
                event_sink({"event_type": name, "attachment_id": attachment_id,
                            "source_sha256": record.sha256, "status": value, **extra})

        status("queued")
        emit("attachment_parse_queued", "queued")
        with _PARSE_GUARD:
            lock = _PARSE_LOCKS.setdefault((str(root), attachment_id),
                WorkspaceLock(root / "_state/attachments" / (attachment_id + ".lock")))
        with lock:
            status("processing")
            emit("attachment_parse_started", "processing")
            try:
                receipt = function(root, attachment_id, event_sink=event_sink, environment=environment)
            except (AttachmentError, OSError, ValueError):
                status("failed")
                emit("attachment_parse_finished", "failed", reason="attachment_parse_invalid_input")
                raise
            status(receipt["status"], receipt)
            emit("attachment_parse_finished", receipt["status"], cached=receipt["cached"],
                 unresolved_count=len(receipt["unresolved_items"]))
            return receipt
    return parse


@_serialized_attachment_parse
def parse_attachment(workspace: str | Path, attachment_id: str, *, event_sink=None,
                     environment: Mapping[str, str] | None = None) -> dict[str, object]:
    """唯一全文解析入口：先复用本次任务结果，仅基础设施故障允许 GPU→CPU 一次。"""
    from state.locking import workspace_lock
    from . import mineru, office
    from .config import load_runtime_config

    started = time.monotonic()
    record = next((item for item in resolve_attachment_records(workspace)
                   if item.attachment_id == attachment_id), None)
    if record is None:
        raise AttachmentError(f"附件不存在：{attachment_id}")
    source = _attachment_path(record, record.original_path)
    if _path_sha256(source) != record.sha256:
        raise AttachmentError("附件原件与已暂存身份不一致")
    receipt = {"attachment_id": attachment_id, "source_sha256": record.sha256,
        "requested_format": source.suffix.lower()[1:], "backend": "mineru",
        "selected_endpoint_index": None, "service_kind": None, "service_version": None,
        "parser_backend": None, "failover": False, "failover_reason": None, "attempts": [],
        "duration_ms": 0, "cached": False, "text_path": None, "middle_json_path": None,
        "content_list_path": None, "media_paths": [], "media_count": 0, "coverage": {}, "unresolved_items": [],
        "original_path": record.original_path, "page_image_paths": list(record.page_image_paths),
        "page_image_pattern": f"{record.extracted_root}/pages/page_NNN.png"
        if record.kind == "document" else None}
    if source.suffix.lower() in DIRECT_TEXT_SUFFIXES:
        return {**receipt, "backend": "direct_text", "status": "completed", "cached": True,
                "text_path": record.original_path, "coverage": {"granularity": "original_bytes"}}
    try:
        info = mineru.source_info(source)
        selected = load_runtime_config(environment)["mineru"]["endpoints"]
    except mineru.MinerUError as exc:
        return {**receipt, "status": "failed", "unresolved_items": [{"reason": str(exc)}]}

    identities = [
        {"source_sha256": record.sha256, "adapter_version": mineru.ADAPTER_VERSION,
         "office_derivation_version": office.DERIVATION_VERSION if source.suffix.lower() in OFFICE_SUFFIXES else None,
         "service_version": mineru.SERVICE_VERSION, "protocol_version": mineru.PROTOCOL_VERSION,
         "parser_backend": mineru.BACKENDS[index], "parameters": mineru.PARAMETERS,
         "requested_format": info["requested_format"], "service_kind": kind,
         "service_sha256": hashlib.sha256(endpoint.encode()).hexdigest()}
        for index, (endpoint, kind) in enumerate(zip(selected, ("gpu", "cpu")))
    ]
    keys = [hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            for identity in identities]

    def prior_results():
        _, manifest = _load_manifest(record.root)
        return next((item.get("parse", {}) for item in manifest.get("records", [])
                     if item.get("attachment_id") == attachment_id), {})

    def cached(prior, index):
        entry = prior.get("results", {}).get(keys[index], {})
        saved, outputs = entry.get("receipt", {}), entry.get("outputs", {})
        required = [saved.get(key) for key in ("text_path", "middle_json_path", "content_list_path")]
        required.extend(saved.get("media_paths", []))
        if (entry.get("identity") != identities[index] or not outputs
                or saved.get("status") not in {"completed", "partial"}
                or saved.get("service_kind") != identities[index]["service_kind"]
                or any(path not in outputs for path in required if path)
                or (saved.get("status") == "completed" and not all(required))):
            return None
        for relative, digest in outputs.items():
            path = _attachment_path(record, relative, derived=True)
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                return None
        return attachment_receipt_view({**saved, "cached": True}, record=record)

    with workspace_lock(record.root):
        prior = prior_results()
        current = prior.get("current")
        # 同一次任务成功切到 CPU 后 resume 不回头重跑；新 workspace 没有此固定结果。
        if current in keys:
            reused = cached(prior, keys.index(current))
            if reused:
                _set_attachment_parse(record, prior)
                return reused

    def emit(event_type, index, **payload):
        if event_sink:
            event_sink({"event_type": event_type, "attachment_id": attachment_id,
                "source_sha256": record.sha256, "requested_format": info["requested_format"],
                "backend": "mineru", "endpoint_index": index,
                "kind": identities[index]["service_kind"],
                "service_version": mineru.SERVICE_VERSION,
                "service_sha256": identities[index]["service_sha256"], **payload})

    for index, endpoint in enumerate(selected):
        kind = identities[index]["service_kind"]
        attempt_start = time.monotonic()
        metadata = {"endpoint_index": index, "kind": kind,
                    "service_version": mineru.SERVICE_VERSION,
                    "service_sha256": identities[index]["service_sha256"]}
        receipt.update(selected_endpoint_index=index, service_kind=kind,
                       service_version=mineru.SERVICE_VERSION, parser_backend=mineru.BACKENDS[index])
        emit("mineru_started", index)
        try:
            with workspace_lock(record.root):
                prior = prior_results()
                reused = cached(prior, index)
                if reused:
                    prior["current"] = keys[index]
                    _set_attachment_parse(record, prior)
                    emit("mineru_succeeded", index, status=reused["status"], cached=True)
                    return reused
            mineru.service_info(endpoint)
            result = mineru.parse_document(endpoint, mineru.SERVICE_VERSION, source, attachment_id,
                                           info["media_type"], kind)
            derived = None
            if source.suffix.lower() in OFFICE_SUFFIXES:
                derived = office.derive(source, record.root / "_runtime/tmp/office-preview", info,
                                        environment=environment)
                result["files"].update(derived["files"])
                result["unresolved_items"].extend(derived["unresolved_items"])
            elapsed = round((time.monotonic() - attempt_start) * 1000)
            status = "partial" if result["unresolved_items"] else "completed"
            receipt["attempts"].append({**metadata, "status": status, "duration_ms": elapsed})
            output_root = f"{record.extracted_root}/mineru/{keys[index]}/{uuid.uuid4().hex}"
            files = {f"{output_root}/{name}": raw for name, raw in result["files"].items()}
            receipt.update(status=status, parser_backend=result["backend"],
                structure_backend=result["structure_backend"],
                text_path=f"{output_root}/document.md" if "document.md" in result["files"] else None,
                middle_json_path=f"{output_root}/middle.json" if "middle.json" in result["files"] else None,
                content_list_path=f"{output_root}/content-list.json" if "content-list.json" in result["files"] else None,
                media_paths=[f"{output_root}/{item['path']}" for item in result["media"]],
                coverage=result["coverage"], unresolved_items=result["unresolved_items"],
                duration_ms=round((time.monotonic() - started) * 1000))
            if derived is not None:
                receipt["office"] = {key: value for key, value in derived.items() if key != "files"}
                receipt["office"].update(source_sha256=record.sha256, original_path=record.original_path,
                    charts=[{**chart, "img_path": f"{output_root}/{chart['img_path']}",
                             "original_path": record.original_path} for chart in derived["charts"]])
                receipt["media_paths"].extend(f"{output_root}/{chart['img_path']}" for chart in derived["charts"])
            receipt = attachment_receipt_view(receipt, record=record)
            with workspace_lock(record.root):
                for relative, raw in files.items():
                    _atomic_bytes_write(_attachment_path(record, relative, derived=True), raw)
                prior = prior_results()
                prior.setdefault("results", {})[keys[index]] = {
                    "identity": identities[index], "receipt": receipt,
                    "outputs": {path: hashlib.sha256(raw).hexdigest() for path, raw in files.items()}}
                prior["current"] = keys[index]
                _set_attachment_parse(record, prior)
            emit("mineru_succeeded", index, status=status, cached=False, duration_ms=elapsed,
                 parser_backend=result["backend"], unresolved_count=len(result["unresolved_items"]))
            return receipt
        except mineru.MinerUError as exc:
            elapsed = round((time.monotonic() - attempt_start) * 1000)
            receipt["attempts"].append({**metadata, "status": "failed", "reason": str(exc), "duration_ms": elapsed})
            emit("mineru_failed", index, reason=str(exc), infrastructure=exc.infrastructure, duration_ms=elapsed)
            if index == 0 and exc.infrastructure:
                receipt.update(failover=True, failover_reason=str(exc))
                emit("mineru_failover", index, next_endpoint_index=1, next_kind="cpu", reason=str(exc))
                continue
            receipt.update(status="failed", duration_ms=round((time.monotonic() - started) * 1000),
                           unresolved_items=[{"reason": str(exc)}])
            return receipt




def materialize_page_image(workspace: str | Path, relative_path: str) -> Path:
    """在第一次 Vision/裁图请求时把逻辑 PDF/Office 页图物化。"""
    root = Path(workspace).expanduser().resolve()
    target = (root / relative_path).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise AttachmentError("页图路径逃逸工作区") from exc
    if target.is_file():
        return target
    descriptor_path = target.with_suffix(".json")
    if not descriptor_path.is_file():
        record = next((item for item in resolve_attachment_records(root)
            if re.fullmatch(re.escape(item.extracted_root) + r"/pages/page_\d{3,}\.png", relative_path)), None)
        if record is None or Path(record.original_path).suffix.lower() not in OFFICE_SUFFIXES:
            raise FileNotFoundError(target)
        from state.locking import workspace_lock
        with workspace_lock(root):
            if not descriptor_path.is_file():
                original = _attachment_path(record, record.original_path)
                if hashlib.sha256(original.read_bytes()).hexdigest() != record.sha256:
                    raise AttachmentError("附件原件与已暂存身份不一致")
                paths = AttachmentStager(root)._office_page_descriptors(original,
                    _attachment_path(record, record.extracted_root, derived=True))
                if not paths:
                    raise AttachmentError("office_page_preview_unavailable")
                _, manifest = _load_manifest(root)
                for detail in manifest.get("records", []):
                    if detail.get("attachment_id") == record.attachment_id:
                        detail.update(page_image_paths=paths, page_preview_mode="lazy_office_pdf_raster")
                _atomic_json_write(root / "_trace/attachment-manifest.json", manifest)
    descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    if not isinstance(descriptor, dict):
        raise AttachmentError(f"页图描述不是对象：{descriptor_path}")
    backing = str(descriptor.get("backing_pdf") or descriptor.get("source_path") or "")
    page_number = int(descriptor.get("page") or 0)
    if not backing or page_number < 1:
        raise AttachmentError(f"页图描述缺少来源或页码：{descriptor_path}")
    source = (root / backing).resolve()
    try:
        source.relative_to(root)
    except ValueError as exc:
        raise AttachmentError("页图来源逃逸工作区") from exc
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz  # type: ignore[no-redef]

    document = fitz.open(source)
    try:
        if page_number > len(document):
            raise AttachmentError(f"页码超出来源范围：{page_number}")
        page = document[page_number - 1]
        if descriptor.get("text_mode") == "unparsed" or "text_mode" not in descriptor:
            geometry, _ = AttachmentStager(root)._pdf_page_descriptor(page, root / descriptor["source_path"], target)
            descriptor.update({key: value for key, value in geometry.items() if key not in {"page_preview_mode"}})
            _atomic_json_write(descriptor_path, descriptor)
        pixmap = page.get_pixmap(
            matrix=fitz.Matrix(2.5, 2.5), alpha=False
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        # PyMuPDF 依据扩展名选择编码器，因此临时文件也必须保留 PNG 后缀。
        pending = target.with_name(f".{target.stem}.{uuid.uuid4().hex}.pending.png")
        try:
            pixmap.save(pending)
            os.replace(pending, target)
        finally:
            pending.unlink(missing_ok=True)
    finally:
        document.close()
    return target




def _atomic_json_write(path: Path, value: Mapping[str, object]) -> None:
    """在目标目录完整写入 JSON 后原子替换。"""
    payload = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    _atomic_text_write(path, payload)


def _atomic_text_write(path: Path, value: str) -> None:
    """在目标目录原子写入 UTF-8 文本并保持共享可读。"""
    _atomic_bytes_write(path, str(value).encode("utf-8"))


def _atomic_bytes_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.{uuid.uuid4().hex}.pending")
    try:
        descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


__all__ = [
    "ATTACHMENT_SCHEMA",
    "ATTACHMENT_SCHEMA_VERSION",
    "ATTACHMENT_LAYOUT_VERSION",
    "AttachmentError",
    "AttachmentManifest",
    "AttachmentRecord",
    "AttachmentStager",
    "attachment_excerpt_media",
    "materialize_page_image",
    "parse_attachment",
    "queue_attachment_parse",
    "resolve_attachment_records",
    "resolve_original_path",
    "resolve_extracted_text",
    "resolve_page_assets",
]
