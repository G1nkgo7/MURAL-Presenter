"""Studio 只保存上传原件；正文、页面与媒体由 Harness 后台解析。"""
import hashlib
import os
import re
from pathlib import Path

from fastapi import UploadFile

from . import engine

MAX_FILES = 8
MAX_FILE_BYTES = 20 * 1024 * 1024


def safe_name(name: str) -> str:
    base = os.path.basename(name or "").replace("\\", "_").replace("/", "_").strip()
    base = re.sub(r"[^A-Za-z0-9._()\- \u4e00-\u9fff]+", "_", base)
    return base[:120] or "file"


def _unique_path(directory: Path, filename: str) -> Path:
    base = safe_name(filename)
    stem = Path(base).stem or "file"
    suffix = Path(base).suffix
    out = directory / base
    i = 2
    while out.exists():
        out = directory / f"{stem}_{i}{suffix}"
        i += 1
    return out


def store_attachments(deck_id: int, files: list[UploadFile]) -> list[dict]:
    out = []
    updir = engine.deck_uploads_dir(deck_id)
    updir.mkdir(parents=True, exist_ok=True)
    for upload in files or []:
        if len(out) >= MAX_FILES:
            break
        if not getattr(upload, "filename", ""):
            continue
        data = upload.file.read()
        if not data:
            continue
        record = {
            "name": upload.filename,
            "stored_name": "",
            "path": "",
            "size": len(data),
            "type": Path(upload.filename).suffix.lower().lstrip(".") or "unknown",
            "source_sha256": hashlib.sha256(data).hexdigest(),
            "notes": [],
        }
        if len(data) > MAX_FILE_BYTES:
            record["notes"] = [f"文件超过 {MAX_FILE_BYTES // 1024 // 1024}MB，未保存。"]
            out.append(record)
            continue
        path = _unique_path(updir, upload.filename)
        path.write_bytes(data)
        record.update(stored_name=path.name, path=str(path))
        out.append(record)
    return out


def raw_attachments(records: list[dict]) -> list[dict]:
    raw = []
    for record in records or []:
        if not record.get("path"):
            continue
        stored = safe_name(record.get("stored_name") or record.get("name") or "file")
        raw.append({
            "source_path": record["path"],
            "workspace_rel": f"attachments/raw/{stored}",
            "name": record.get("name", ""),
            "stored_name": stored,
            "type": record.get("type", "unknown"),
            "size": record.get("size", 0),
        })
    return raw
