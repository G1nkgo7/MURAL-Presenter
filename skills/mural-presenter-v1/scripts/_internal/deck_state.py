"""Compile stable page identity and the minimal versioned Deck Graph.

The model authors ``config.json``, ``deck.md`` and ``slide_NN.md``. This
module deterministically projects their identities into control-plane
state under ``_state/``; it never asks the model to maintain a second graph.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

PAGE_INDEX_SCHEMA = "mural.page-index.v1"
DECK_GRAPH_SCHEMA = "mural.deck-graph.v1"


def _json_hash(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        if path.read_text(encoding="utf-8") == serialized:
            return
    except (OSError, UnicodeError):
        pass
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(serialized)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _plan_hash(plan: dict) -> str:
    text = str(plan.get("text") or "")
    if text:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    return _json_hash(
        {
            key: plan.get(key)
            for key in (
                "number",
                "title",
                "speech",
            )
        }
    )


def _plan_fingerprint(deck: dict, plans: list[dict]) -> str:
    return _json_hash(
        {
            "deck": {
                "title": deck.get("title"),
                "language": deck.get("language"),
                "pages": deck.get("pages"),
                "creation_config": deck["creation_config"],
                "text_sha256": hashlib.sha256(
                    str(deck.get("text") or "").encode("utf-8")
                ).hexdigest(),
            },
            "plans": [
                {
                    "number": int(plan["number"]),
                    "source_plan_sha256": _plan_hash(plan),
                }
                for plan in plans
            ],
        }
    )


def _workspace_identity(root: Path, ordered_uids=None) -> tuple[str, dict[int, str]]:
    from .workspace_paths import WorkspacePaths
    WorkspacePaths.load(root)
    state = _read_json(root / "_state/workspace.json")
    if ordered_uids is not None:
        require_revision_transaction(root)
        pages = {number: uid for number, uid in enumerate(ordered_uids, 1)}
    else:
        pages = {int(key): row["page_uid"] for key, row in state["pages"].items()
                 if not row.get("retired")}
    return state["deck_id"], pages


def require_revision_transaction(root: Path) -> None:
    marker = _read_json(root / "_state/structural-transaction.json")
    if marker.get("status") != "prepared" or not os.environ.get("MURAL_REVISION_TRANSACTION") or marker.get("transaction_id") != os.environ["MURAL_REVISION_TRANSACTION"]:
        raise ValueError("structural/commit commands must run through the Harness transaction")


def _assign_page_rows(
    root: Path,
    deck_id: str,
    plans: list[dict],
    existing: dict,
    identities: dict[int, str],
) -> list[dict]:
    old_by_uid = {row["page_uid"]: row for row in existing.get("pages", [])}
    rows: list[dict] = []
    for plan in plans:
        number = int(plan["number"])
        if number not in identities:
            raise ValueError(f"Harness has not registered page {number}")
        uid = identities[number]
        old = old_by_uid.get(uid, {})
        source_hash = _plan_hash(plan)
        unchanged = source_hash == str(old.get("source_plan_sha256") or "")
        status = str(old.get("status") or "planned") if unchanged else "planned"
        row = {
            "page_uid": uid,
            "position": number,
            "plan_path": Path(plan["path"]).relative_to(root).as_posix(),
            "plan_section": plan["plan_section"],
            "html_path": f"slides/slide_{number:02d}.html",
            "png_path": f"renders/slide_{number:02d}.png",
            "title": str(plan.get("title") or ""),
            "source_plan_sha256": source_hash,
            "status": status,
        }
        if unchanged and isinstance(old.get("artifact_hashes"), dict):
            row["artifact_hashes"] = dict(old["artifact_hashes"])
        rows.append(row)
    return rows


def _graph_edges(rows: list[dict]) -> list[dict]:
    edges: list[dict] = []
    uids = [str(row["page_uid"]) for row in rows]
    for left, right in zip(uids, uids[1:]):
        edges.append(
            {"type": "narrative", "key": "deck-sequence", "from": left, "to": right}
        )

    if uids:
        edges.append({"type": "shared_system", "key": "deck-theme", "pages": uids})
    return edges


def compile_deck_state(root: Path, deck: dict, plans: list[dict], *, ordered_uids=None) -> dict:
    """Compile and persist stable page index + versioned Deck Graph."""
    root = Path(root)
    state_root = root / "_state"
    index_path = state_root / "page-index.json"
    graph_path = state_root / "deck-graph.json"
    old_index = _read_json(index_path)
    old_graph = _read_json(graph_path)
    deck_id, identities = _workspace_identity(root, ordered_uids)
    fingerprint = _plan_fingerprint(deck, plans)
    previous_fingerprint = str(
        old_graph.get("plan_fingerprint")
        or old_index.get("plan_fingerprint")
        or ""
    )
    deck_version = max(
        1,
        int(old_graph.get("deck_version") or old_index.get("deck_version") or 1),
    )
    previous_epoch = int(
        old_graph.get("plan_epoch") or old_index.get("plan_epoch") or 0
    )
    plan_epoch = (
        max(1, previous_epoch)
        if fingerprint == previous_fingerprint
        else previous_epoch + 1
    )
    revision_id = str(
        old_graph.get("revision_id") or old_index.get("revision_id") or ""
    )
    rows = _assign_page_rows(root, deck_id, plans, old_index, identities)
    page_index = {
        "schema": PAGE_INDEX_SCHEMA,
        "deck_id": deck_id,
        "deck_version": deck_version,
        "plan_epoch": plan_epoch,
        "revision_id": revision_id,
        "plan_fingerprint": fingerprint,
        "pages": rows,
    }
    page_index["index_sha256"] = _json_hash(page_index)

    graph = {
        "schema": DECK_GRAPH_SCHEMA,
        "deck_id": deck_id,
        "deck_version": deck_version,
        "plan_epoch": plan_epoch,
        "revision_id": revision_id,
        "plan_fingerprint": fingerprint,
        "page_index_sha256": page_index["index_sha256"],
        "pages": [str(row["page_uid"]) for row in rows],
        "nodes": {
            str(row["page_uid"]): {
                key: row[key]
                for key in (
                    "position",
                    "title",
                    "plan_path",
                    "plan_section",
                    "html_path",
                    "png_path",
                )
            }
            for row in rows
        },
        "edges": _graph_edges(rows),
        "global_contracts": {
            "title": str(deck.get("title") or ""),
            "creation_config": dict(deck["creation_config"]),
        },
        "revision_history": list(old_graph.get("revision_history") or []),
    }
    _atomic_write_json(index_path, page_index)
    _atomic_write_json(graph_path, graph)
    return {
        "deck_id": deck_id,
        "deck_version": deck_version,
        "plan_epoch": plan_epoch,
        "revision_id": revision_id,
        "pages": len(rows),
        "page_index": str(index_path.relative_to(root)),
        "deck_graph": str(graph_path.relative_to(root)),
    }


def commit_deck_revision(
    root: Path,
    revision_id: str,
    *,
    expected_base_version: int | None = None,
) -> dict:
    """Advance the logical Deck version after verified revision work.

    The command is idempotent for the current ``revision_id``. It refreshes
    derived metadata without mutating plans or artifacts; callers must render
    and inspect the changed scope before committing.
    """
    root = Path(root)
    require_revision_transaction(root)
    revision = str(revision_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", revision):
        raise ValueError(
            "revision_id must be a stable 1–128 character token using "
            "letters, digits, dot, underscore, or hyphen"
        )
    index_path = root / "_state" / "page-index.json"
    graph_path = root / "_state" / "deck-graph.json"
    index = _read_json(index_path)
    graph = _read_json(graph_path)
    if index.get("schema") != PAGE_INDEX_SCHEMA or graph.get("schema") != DECK_GRAPH_SCHEMA:
        raise ValueError("compile Deck state with validate-plans before commit-revision")
    index_version = int(index.get("deck_version") or 0)
    graph_version = int(graph.get("deck_version") or 0)
    if index_version < 1 or graph_version != index_version:
        raise ValueError(
            "page-index and deck-graph versions disagree; re-run validate-plans"
        )
    current_revision = str(graph.get("revision_id") or "")
    if current_revision == revision:
        return {
            "deck_id": str(graph.get("deck_id") or ""),
            "deck_version": graph_version,
            "base_deck_version": max(0, graph_version - 1),
            "revision_id": revision,
            "status": "already_committed",
        }
    history = [
        dict(item)
        for item in graph.get("revision_history", [])
        if isinstance(item, dict)
    ]
    if any(str(item.get("revision_id") or "") == revision for item in history):
        raise ValueError(f"revision_id {revision!r} was already used by an older version")
    if expected_base_version is not None and int(expected_base_version) != graph_version:
        raise ValueError(
            f"revision base version mismatch: expected {int(expected_base_version)}, "
            f"current {graph_version}"
        )

    active_path = root / "_state" / "active-revision.json"
    active = _read_json(active_path)
    impact: dict = {}
    impact_path: Path | None = None
    active_revision = str(active.get("revision_id") or "")
    active_status = str(active.get("status") or "")
    if (
        active.get("schema") == "mural.active-revision.v1"
        and active_revision
        and active_revision != revision
        and active_status not in {"committed", "aborted"}
    ):
        raise ValueError(
            f"cannot commit {revision!r} while revision {active_revision!r} "
            f"is active ({active_status})"
        )
    if active_revision == revision:
        raw_impact_path = str(active.get("impact_path") or "")
        candidate = (root / raw_impact_path).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError as exc:
            raise ValueError("active revision impact path escapes workspace") from exc
        impact = _read_json(candidate)
        impact_path = candidate
        if str(impact.get("schema") or "") != "mural.revision-impact.v1":
            raise ValueError("active revision impact is missing or malformed")
        if int(impact.get("base_deck_version") or 0) != graph_version:
            raise ValueError("active revision impact no longer matches current Deck version")
        if bool(impact.get("structural_pending", False)):
            raise ValueError("cannot commit revision with pending structural operations")
        assignments = [
            item
            for item in impact.get("assignments", [])
            if isinstance(item, dict)
        ]
        incomplete = [
            str(item.get("assignment_id") or item.get("revision_group_id") or "")
            for item in assignments
            if str(item.get("status") or "") not in {"completed", "verified"}
        ]
        if incomplete:
            raise ValueError(
                "cannot commit revision before Dynamic Revision Groups complete: "
                + ", ".join(incomplete)
            )

    # 插页后的 Brief 会继续编辑；提交时不能把旧 placeholder/title/hash 固定进新版本。
    from .deck_core import _load_plans
    deck, plans = _load_plans(root, len(index['pages']))
    compile_deck_state(root, deck, plans,
                       ordered_uids=[row['page_uid'] for row in index['pages']])
    index, graph = _read_json(index_path), _read_json(graph_path)
    next_version = graph_version + 1
    index["deck_version"] = next_version
    index["revision_id"] = revision
    index.pop("index_sha256", None)
    index["index_sha256"] = _json_hash(index)
    history.append(
        {
            "revision_id": revision,
            "base_deck_version": graph_version,
            "deck_version": next_version,
            "plan_epoch": int(graph.get("plan_epoch") or 0),
            "plan_fingerprint": str(graph.get("plan_fingerprint") or ""),
            "route": str(impact.get("route") or "legacy_revision"),
            "affected_pages": list(impact.get("affected_pages") or []),
            "impact_sha256": (
                _json_hash(impact) if impact else ""
            ),
        }
    )
    graph["deck_version"] = next_version
    graph["revision_id"] = revision
    graph["page_index_sha256"] = index["index_sha256"]
    graph["revision_history"] = history
    _atomic_write_json(index_path, index)
    _atomic_write_json(graph_path, graph)
    if impact and impact_path is not None:
        impact["status"] = "committed"
        impact["committed_deck_version"] = next_version
        impact["committed_at"] = time.time()
        _atomic_write_json(impact_path, impact)
        active["status"] = "committed"
        active["committed_deck_version"] = next_version
        _atomic_write_json(active_path, active)
    return {
        "deck_id": str(graph.get("deck_id") or ""),
        "deck_version": next_version,
        "base_deck_version": graph_version,
        "revision_id": revision,
        "status": "committed",
    }


__all__ = [
    "DECK_GRAPH_SCHEMA",
    "PAGE_INDEX_SCHEMA",
    "commit_deck_revision",
    "compile_deck_state",
]
