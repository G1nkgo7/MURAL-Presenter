"""Persist replayable multimodal traces for v0.2 synthesis runs."""
from __future__ import annotations

import hashlib
import json
import mimetypes
from pathlib import Path


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def _referenced_shots(messages: list[dict], image_by_tool: dict[str, str]) -> dict[str, str]:
    shots: dict[str, str] = {}
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and isinstance(block.get("content"), list)
            ):
                continue
            tool_use_id = str(block.get("tool_use_id") or "")
            for item in block["content"]:
                if not (isinstance(item, dict) and item.get("type") == "image"):
                    continue
                shot = str(item.get("shot") or "")
                if shot:
                    shots.setdefault(shot, tool_use_id)
    for tool_use_id, shot in image_by_tool.items():
        if shot:
            shots.setdefault(str(shot), str(tool_use_id or ""))
    return shots


def _media_types(tool_log: list[dict]) -> dict[str, str]:
    return {
        str(item.get("snapshot")): str(item.get("media_type"))
        for item in tool_log
        if isinstance(item, dict) and item.get("snapshot") and item.get("media_type")
    }


def write_trace(
    trace_dir: Path,
    messages: list[dict],
    tool_log: list[dict],
    image_by_tool: dict[str, str],
    run_mode: str,
) -> dict:
    """Write the normal trace and, in synthesis mode, its replay manifest."""
    _write_json(trace_dir / "messages.json", messages)
    _write_json(trace_dir / "tool_log.json", tool_log)
    status = {"mode": run_mode, "complete": True, "image_count": 0}
    if run_mode != "synthesis":
        return status

    media_types = _media_types(tool_log)
    images: list[dict] = []
    missing: list[str] = []
    for shot, tool_use_id in sorted(_referenced_shots(messages, image_by_tool).items()):
        path = trace_dir / shot
        if not path.is_file():
            missing.append(shot)
            continue
        raw = path.read_bytes()
        guessed_media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        images.append({
            "shot": Path(shot).as_posix(),
            "tool_use_id": tool_use_id,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "media_type": media_types.get(shot, guessed_media),
        })
    manifest = {
        "schema": "mural.multimodal-trace.v1",
        "complete": not missing,
        "image_count": len(images),
        "images": images,
        "missing": missing,
        "replay_contract": (
            "messages.json plus the hash-addressed images reconstruct the exact "
            "model-visible multimodal turns"
        ),
    }
    _write_json(trace_dir / "multimodal-manifest.json", manifest)
    status.update({
        "complete": bool(manifest["complete"]),
        "image_count": int(manifest["image_count"]),
        "missing": list(manifest["missing"]),
    })
    return status
