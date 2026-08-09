#!/usr/bin/env python3
"""Validate per-Image-Agent manifests and atomically publish one asset map."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import tempfile


ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*$")
SOURCE_TYPES = {"real", "generated", "material"}


def _load_fragment(root: Path, agent: str) -> tuple[Path, dict]:
    path = root / "assets" / "image-manifests" / f"{agent}.json"
    if not path.is_file():
        raise ValueError(
            f"missing {path.relative_to(root)}; read that child's summary_path and "
            "tool log to recover its exact mapping before considering new Vision"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.relative_to(root)} is invalid JSON: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError(f"{path.relative_to(root)} must be a version 1 object")
    if payload.get("agent") != agent:
        raise ValueError(f"{path.relative_to(root)} agent must equal {agent!r}")
    if not isinstance(payload.get("items"), list):
        raise ValueError(f"{path.relative_to(root)} items must be an array")
    if not isinstance(payload.get("missing", []), list):
        raise ValueError(f"{path.relative_to(root)} missing must be an array")
    return path, payload


def _validate_id(value: object, where: str) -> str:
    logical_id = str(value or "").strip()
    if not ID_RE.fullmatch(logical_id):
        raise ValueError(f"{where} has invalid logical_id {logical_id!r}")
    return logical_id


def _validate_item(root: Path, agent: str, item: object, index: int) -> dict:
    where = f"{agent}.items[{index}]"
    if not isinstance(item, dict):
        raise ValueError(f"{where} must be an object")
    logical_id = _validate_id(item.get("logical_id"), where)
    relative = str(item.get("path") or "").strip()
    if not relative.startswith("assets/") or Path(relative).is_absolute():
        raise ValueError(f"{where}.path must be a workspace-relative assets/ path")
    target = (root / relative).resolve()
    assets = (root / "assets").resolve()
    if target != assets and assets not in target.parents:
        raise ValueError(f"{where}.path escapes assets/")
    if not target.is_file() or target.stat().st_size < 1:
        raise ValueError(f"{where}.path does not resolve to a non-empty file: {relative}")
    purpose = str(item.get("purpose") or "").strip()
    if not purpose:
        raise ValueError(f"{where}.purpose is required")
    page_refs = item.get("page_refs")
    if not isinstance(page_refs, list) or not page_refs or not all(
        isinstance(page, int) and page > 0 for page in page_refs
    ):
        raise ValueError(f"{where}.page_refs must contain positive page numbers")
    source_type = str(item.get("source_type") or "").strip()
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"{where}.source_type must be one of {sorted(SOURCE_TYPES)}")
    if item.get("status") != "verified":
        raise ValueError(f"{where}.status must be 'verified'")
    normalized = dict(item)
    normalized.update(
        {
            "logical_id": logical_id,
            "path": relative,
            "purpose": purpose,
            "page_refs": sorted(set(page_refs)),
            "source_type": source_type,
            "status": "verified",
            "agent": agent,
        }
    )
    return normalized


def merge(root: Path, agents: list[str]) -> dict:
    if not agents or len(set(agents)) != len(agents):
        raise ValueError("--expected must list unique Image Agent labels")
    if not all(ID_RE.fullmatch(agent) for agent in agents):
        raise ValueError("--expected contains an invalid Image Agent label")
    items: list[dict] = []
    missing: list[dict] = []
    seen: dict[str, str] = {}
    sources: list[str] = []
    for agent in agents:
        path, payload = _load_fragment(root, agent)
        sources.append(path.relative_to(root).as_posix())
        for index, raw in enumerate(payload["items"]):
            item = _validate_item(root, agent, raw, index)
            logical_id = item["logical_id"]
            if logical_id in seen:
                raise ValueError(
                    f"duplicate logical_id {logical_id!r} in {seen[logical_id]} and {agent}"
                )
            seen[logical_id] = agent
            items.append(item)
        for index, raw in enumerate(payload.get("missing", [])):
            where = f"{agent}.missing[{index}]"
            if not isinstance(raw, dict):
                raise ValueError(f"{where} must be an object")
            logical_id = _validate_id(raw.get("logical_id"), where)
            reason = str(raw.get("reason") or "").strip()
            if not reason:
                raise ValueError(f"{where}.reason is required")
            if logical_id in seen:
                raise ValueError(f"{logical_id!r} cannot be both verified and missing")
            seen[logical_id] = agent
            missing.append({"logical_id": logical_id, "reason": reason, "agent": agent})
    return {
        "version": 1,
        "sources": sources,
        "items": sorted(items, key=lambda item: item["logical_id"]),
        "missing": sorted(missing, key=lambda item: item["logical_id"]),
    }


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument(
        "--expected",
        required=True,
        help="Comma-separated Image Agent labels, for example image_01,image_02",
    )
    args = parser.parse_args()
    root = Path(args.root).resolve()
    agents = [value.strip() for value in args.expected.split(",") if value.strip()]
    try:
        payload = merge(root, agents)
        target = root / "assets" / "image-manifest.json"
        _atomic_write(target, payload)
    except (OSError, ValueError) as exc:
        print(f"status:FAIL\n{exc}")
        return 2
    print(
        "status:PASS "
        f"manifests:{len(agents)} items:{len(payload['items'])} "
        f"missing:{len(payload['missing'])} path:assets/image-manifest.json"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
