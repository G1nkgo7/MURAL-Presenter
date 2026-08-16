#!/usr/bin/env python3
"""Prepare stable PNG asset slots and atomically install generated/fetched images."""

from __future__ import annotations

import argparse
import binascii
import json
import os
import re
import shutil
import struct
import sys
import zlib
from pathlib import Path


ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
PLACEHOLDER_MARKER = b"ppt-placeholder"


def _chunk(kind: bytes, data: bytes) -> bytes:
    payload = kind + data
    return struct.pack(">I", len(data)) + payload + struct.pack(">I", binascii.crc32(payload) & 0xFFFFFFFF)


def _placeholder_png(width: int, height: int) -> bytes:
    if not (16 <= width <= 4096 and 16 <= height <= 4096):
        raise ValueError(f"invalid placeholder dimensions: {width}x{height}")
    # Neutral solid canvas. The marker makes accidental final delivery machine-detectable.
    row = b"\x00" + bytes((229, 231, 235)) * width
    raw = row * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"tEXt", b"Comment\x00" + PLACEHOLDER_MARKER)
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )


def _load_manifest(path: Path) -> list[dict[str, object]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("assets"), list):
        raise ValueError("manifest must contain schema_version=1 and assets[]")
    seen: set[str] = set()
    result: list[dict[str, object]] = []
    for raw in data["assets"]:
        if not isinstance(raw, dict):
            raise ValueError("each asset spec must be an object")
        asset_id = str(raw.get("id", ""))
        if not ID_RE.fullmatch(asset_id) or asset_id in seen:
            raise ValueError(f"invalid or duplicate asset id: {asset_id!r}")
        seen.add(asset_id)
        spec = dict(raw)
        spec["id"] = asset_id
        spec["width"] = int(raw.get("width", 1600))
        spec["height"] = int(raw.get("height", 900))
        slides_norm: list[int] = []
        for v in raw.get("slides", []):
            if isinstance(v, bool):
                raise ValueError(f"invalid slide reference for asset {asset_id}: {v!r}")
            if isinstance(v, int):
                slides_norm.append(v)
                continue
            s = str(v).strip()
            m = re.fullmatch(r"(?:slide[_-]?)?0*(\d+)(?:\.html)?", s, flags=re.IGNORECASE)
            if not m:
                raise ValueError(f"invalid slide reference for asset {asset_id}: {v!r}")
            slides_norm.append(int(m.group(1)))
        spec["slides"] = slides_norm
        expected = f"assets/by-id/{asset_id}.png"
        if raw.get("path") not in (None, expected):
            raise ValueError(f"asset path must be stable: {expected}")
        spec["path"] = expected
        result.append(spec)
    return result


def _is_placeholder(path: Path) -> bool:
    return path.is_file() and PLACEHOLDER_MARKER in path.read_bytes()


def _image_magic_ok(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 16:
        return False
    head = path.read_bytes()[:16]
    return (
        head.startswith(b"\x89PNG\r\n\x1a\n")
        or head.startswith(b"\xff\xd8\xff")
        or head.startswith((b"GIF87a", b"GIF89a"))
        or (head.startswith(b"RIFF") and b"WEBP" in head)
    )


def prepare(manifest: Path, assets_root: Path) -> dict[str, object]:
    specs = _load_manifest(manifest)
    by_id = assets_root / "by-id"
    pending = assets_root / "pending"
    by_id.mkdir(parents=True, exist_ok=True)
    pending.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    preserved: list[str] = []
    for spec in specs:
        asset_id = str(spec["id"])
        target = by_id / f"{asset_id}.png"
        marker = pending / f"{asset_id}.json"
        if target.exists() and not _is_placeholder(target) and not marker.exists():
            preserved.append(asset_id)
            continue
        if not target.exists() or not _is_placeholder(target):
            target.write_bytes(_placeholder_png(int(spec["width"]), int(spec["height"])))
        marker.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
        created.append(asset_id)
    return {"status": "ok", "prepared": created, "preserved_final": preserved}


def install(asset_id: str, source: Path, assets_root: Path, cleanup_source: bool) -> dict[str, object]:
    if not ID_RE.fullmatch(asset_id):
        raise ValueError(f"invalid asset id: {asset_id!r}")
    source = source.expanduser().resolve()
    if not _image_magic_ok(source):
        raise ValueError(f"source is missing, empty, or not a supported image: {source}")
    by_id = assets_root / "by-id"
    pending = assets_root / "pending"
    target = by_id / f"{asset_id}.png"
    marker = pending / f"{asset_id}.json"
    if not marker.is_file():
        raise ValueError(f"asset is not pending or unknown: {asset_id}")
    spec = json.loads(marker.read_text(encoding="utf-8"))
    by_id.mkdir(parents=True, exist_ok=True)
    tmp = by_id / f".{asset_id}.{os.getpid()}.tmp"
    shutil.copy2(source, tmp)
    with tmp.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(tmp, target)
    marker.unlink()
    if cleanup_source and source != target and assets_root.resolve() in source.parents:
        source.unlink(missing_ok=True)
    return {"status": "installed", "id": asset_id, "target": str(target), "slides": spec.get("slides", [])}


def cancel(asset_id: str, assets_root: Path, reason: str) -> dict[str, object]:
    if not ID_RE.fullmatch(asset_id):
        raise ValueError(f"invalid asset id: {asset_id!r}")
    target = assets_root / "by-id" / f"{asset_id}.png"
    marker = assets_root / "pending" / f"{asset_id}.json"
    if marker.exists():
        marker.unlink()
    if _is_placeholder(target):
        target.unlink()
    waived = assets_root / "waived"
    waived.mkdir(parents=True, exist_ok=True)
    (waived / f"{asset_id}.json").write_text(
        json.dumps({"id": asset_id, "reason": reason}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"status": "waived", "id": asset_id, "reason": reason}


def status(assets_root: Path) -> dict[str, object]:
    pending_dir = assets_root / "pending"
    ids = sorted(path.stem for path in pending_dir.glob("*.json")) if pending_dir.is_dir() else []
    placeholders = sorted(
        path.stem for path in (assets_root / "by-id").glob("*.png") if _is_placeholder(path)
    ) if (assets_root / "by-id").is_dir() else []
    return {"status": "ok" if not ids and not placeholders else "pending", "pending": ids, "placeholders": placeholders}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    prep = sub.add_parser("prepare")
    prep.add_argument("manifest", type=Path)
    prep.add_argument("assets_root", type=Path)

    ins = sub.add_parser("install")
    ins.add_argument("asset_id")
    ins.add_argument("source", type=Path)
    ins.add_argument("assets_root", type=Path)
    ins.add_argument("--cleanup-source", action="store_true")

    waive = sub.add_parser("cancel")
    waive.add_argument("asset_id")
    waive.add_argument("assets_root", type=Path)
    waive.add_argument("--reason", required=True)

    stat = sub.add_parser("status")
    stat.add_argument("assets_root", type=Path)

    args = parser.parse_args()
    try:
        if args.command == "prepare":
            result = prepare(args.manifest.resolve(), args.assets_root.resolve())
        elif args.command == "install":
            result = install(args.asset_id, args.source, args.assets_root.resolve(), args.cleanup_source)
        elif args.command == "cancel":
            result = cancel(args.asset_id, args.assets_root.resolve(), args.reason)
        else:
            result = status(args.assets_root.resolve())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
