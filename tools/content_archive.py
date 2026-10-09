#!/usr/bin/env python3
"""Deterministic, content-verified tar.zst helpers for release artifacts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tarfile
import time
from typing import Iterable

import zstandard


def sha256_file(path: Path) -> str:
    state = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            state.update(block)
    return state.hexdigest()


def atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    path.chmod(0o644)


def safe_relative(value: str | Path) -> str:
    text = Path(value).as_posix()
    while text.startswith("./"):
        text = text[2:]
    relative = PurePosixPath(text)
    if not text or text == "." or relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe archive member path: {value}")
    return relative.as_posix()


def _source(root: Path, relative: str) -> Path:
    source = root / relative
    if not source.is_file():
        raise FileNotFoundError(f"archive source is missing: {source}")
    resolved = source.resolve()
    workspace = root.resolve()
    if resolved != workspace and workspace not in resolved.parents:
        raise ValueError(f"archive source escapes root: {source} -> {resolved}")
    return source


def member_records(root: Path, members: Iterable[str | Path]) -> list[dict]:
    records: list[dict] = []
    for relative in sorted({safe_relative(value) for value in members}):
        source = _source(root, relative)
        records.append(
            {
                "path": relative,
                "size": source.stat().st_size,
                "sha256": sha256_file(source),
                "mode": 0o755 if os.access(source, os.X_OK) else 0o644,
            }
        )
    if not records:
        raise ValueError("refusing to create an empty archive")
    return records


def write_tar_zst(
    root: Path,
    members: Iterable[str | Path],
    target: Path,
    *,
    compression_level: int = 10,
) -> dict:
    root = root.resolve()
    records = member_records(root, members)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{time.time_ns()}.tmp")
    compressor = zstandard.ZstdCompressor(
        level=compression_level,
        write_checksum=True,
        write_content_size=False,
        threads=0,
    )
    try:
        with temporary.open("wb") as raw:
            with compressor.stream_writer(raw, closefd=False) as compressed:
                with tarfile.open(
                    fileobj=compressed,
                    mode="w|",
                    format=tarfile.PAX_FORMAT,
                ) as archive:
                    for record in records:
                        source = _source(root, record["path"])
                        info = tarfile.TarInfo(record["path"])
                        info.size = int(record["size"])
                        info.mode = int(record["mode"])
                        info.mtime = 0
                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""
                        with source.open("rb") as stream:
                            archive.addfile(info, stream)
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    target.chmod(0o644)
    return {
        "path": str(target.resolve()),
        "sha256": sha256_file(target),
        "compressed_bytes": target.stat().st_size,
        "uncompressed_bytes": sum(int(record["size"]) for record in records),
        "member_count": len(records),
        "members": records,
    }


def verify_tar_zst(path: Path, expected_records: Iterable[dict]) -> dict:
    expected = {
        safe_relative(str(record["path"])): {
            "size": int(record["size"]),
            "sha256": str(record["sha256"]),
        }
        for record in expected_records
    }
    observed: dict[str, dict] = {}
    decompressor = zstandard.ZstdDecompressor()
    with path.open("rb") as raw:
        with decompressor.stream_reader(raw, closefd=False) as expanded:
            with tarfile.open(fileobj=expanded, mode="r|") as archive:
                for member in archive:
                    relative = safe_relative(member.name)
                    if relative in observed:
                        raise ValueError(f"duplicate archive member: {relative}")
                    if not member.isfile():
                        raise ValueError(f"non-regular archive member: {relative}")
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ValueError(f"unreadable archive member: {relative}")
                    state = hashlib.sha256()
                    size = 0
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        state.update(block)
                        size += len(block)
                    observed[relative] = {"size": size, "sha256": state.hexdigest()}
    if observed != expected:
        missing = sorted(set(expected) - set(observed))
        extra = sorted(set(observed) - set(expected))
        changed = sorted(
            key
            for key in set(expected) & set(observed)
            if expected[key] != observed[key]
        )
        raise ValueError(
            "archive member verification failed: "
            f"missing={missing}, extra={extra}, changed={changed}"
        )
    return {
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "compressed_bytes": path.stat().st_size,
        "uncompressed_bytes": sum(value["size"] for value in observed.values()),
        "member_count": len(observed),
        "verified": True,
    }
