"""Deterministic edit-impact compilation for MURAL v1.0 revisions.

The Orchestrator describes *what* should change in ``edit-operations.json``.
This module decides *which current pages* form the minimal changed scope and
whether each temporary Revision Group may reuse a historical logical Owner.
Only explicit targets and recorded structural relations determine the scope.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import time

EDIT_OPERATIONS_SCHEMA = "mural.edit-operations.v1"
REVISION_IMPACT_SCHEMA = "mural.revision-impact.v1"
ACTIVE_REVISION_SCHEMA = "mural.active-revision.v1"
REVISION_CONTEXT_SCHEMA = "mural.revision-context.v1"
PAGE_INDEX_SCHEMA = "mural.page-index.v1"
DECK_GRAPH_SCHEMA = "mural.deck-graph.v1"
ISSUE_LEDGER_SCHEMA = "mural.issue-ledger.v1"

LOCAL_KINDS = {
    "content", "copy", "layout", "image", "terminology", "evidence", "data",
    "visual_encoding", "section",
}
STRUCTURAL_KINDS = {"delete_page", "insert_page", "move_page"}
GLOBAL_KINDS = {"shared_system", "replan"}
SUPPORTED_KINDS = LOCAL_KINDS | STRUCTURAL_KINDS | GLOBAL_KINDS


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _tokens(value: object, *, field: str) -> list[str]:
    raw = value or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError(f"{field} must be an array of stable IDs")
    return list(
        dict.fromkeys(_safe_token(item, field=field) for item in raw)
    )


def _safe_token(value: object, *, field: str) -> str:
    token = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", token):
        raise ValueError(
            f"{field} must use 1–128 letters, digits, dot, underscore, or hyphen"
        )
    return token


def _operation_targets(operation: dict, by_position: dict[int, str]) -> list[str]:
    raw = (
        operation.get("target_page_uids")
        or operation.get("targets")
        or operation.get("page_uids")
        or []
    )
    if isinstance(raw, str):
        raw = [raw]
    targets = [str(uid).strip() for uid in raw if str(uid).strip()]
    positions = operation.get("target_positions") or operation.get("pages") or []
    if isinstance(positions, (int, str)):
        positions = [positions]
    for value in positions:
        try:
            position = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid target position {value!r}") from exc
        if position not in by_position:
            raise ValueError(f"target position {position} is not in the current Deck")
        targets.append(by_position[position])
    return list(dict.fromkeys(targets))


def _edge_members(edge: dict) -> tuple[str, ...]:
    members: list[str] = []
    if isinstance(edge.get("pages"), list):
        members.extend(str(uid) for uid in edge["pages"] if str(uid))
    for key in ("from", "to"):
        if str(edge.get(key) or ""):
            members.append(str(edge[key]))
    return tuple(dict.fromkeys(members))


def _narrative_neighbors(targets: set[str], edges: list[dict]) -> set[str]:
    """Structural edits affect one-hop transitions, not the entire deck chain."""
    affected = set(targets)
    for edge in edges:
        if str(edge.get("type") or "") != "narrative":
            continue
        members = set(_edge_members(edge))
        if members & targets:
            affected.update(members)
    return affected


def _owner_rows(root: Path) -> list[dict]:
    state = _read_json(root / "_state" / "owners.json")
    index = _read_json(root / "_state/page-index.json")
    by_position = {int(row["position"]): row["page_uid"] for row in index.get("pages", [])}
    rows: list[dict] = []
    for group_id, raw in dict(state.get("owners") or {}).items():
        if not isinstance(raw, dict) or str(raw.get("status") or "") == "archived":
            continue
        row = dict(raw)
        row.setdefault("group_id", str(group_id))
        row["page_uids"] = [by_position[page] for page in row["pages"] if page in by_position]
        rows.append(row)
    return rows


def _owner_for_uid(owners: list[dict], page_uid: str) -> dict | None:
    candidates = [
        row
        for row in owners
        if page_uid in {str(uid) for uid in row.get("page_uids", [])}
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda row: (
            int(row.get("ownership_epoch") or 0),
            float(row.get("execution_started_at") or 0),
        ),
    )


def _assignment_id(revision_id: str, page_uids: list[str], kinds: list[str]) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {"revision": revision_id, "pages": page_uids, "kinds": kinds},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:10]
    safe_revision = re.sub(r"[^a-z0-9-]+", "-", revision_id.lower()).strip("-")
    return f"rg-{safe_revision or 'revision'}-{digest}"


def _verification_scope(
    page_uids: list[str],
    kinds: set[str],
    owner_count: int,
) -> str:
    if kinds & GLOBAL_KINDS:
        return "whole_deck"
    if kinds & STRUCTURAL_KINDS or owner_count > 1:
        return "affected_montage+whole_deck_invariants"
    if len(page_uids) > 1:
        return "revision_group_montage"
    return "target_page"


def _human_impact_markdown(impact: dict, *, skill_name: str) -> str:
    applied = {str(value) for value in impact.get("applied_operation_ids", [])}
    lines = [
        f"# Revision Impact — {impact['revision_id']}",
        "",
        f"- base_deck_version: {impact['base_deck_version']}",
        f"- plan_epoch: {impact['plan_epoch']}",
        f"- route: {impact['route']}",
        f"- source_issue_ids: {', '.join(impact.get('source_issue_ids', [])) or 'none'}",
        f"- direct_targets: {', '.join(impact['direct_targets']) or 'none'}",
        f"- affected_pages: {', '.join(impact['affected_pages']) or 'none'}",
        f"- structural_pending: {str(impact['structural_pending']).lower()}",
        "",
        "## Operations",
        "",
    ]
    for operation in impact.get("operations", []):
        if not isinstance(operation, dict):
            continue
        op_id = str(operation.get("operation_id") or "")
        kind = str(operation.get("kind") or "")
        targets = [str(uid) for uid in operation.get("target_page_uids", [])]
        lines.extend(
            [
                f"### {op_id}",
                f"- kind: {kind}",
                f"- targets: {', '.join(targets) or 'none'}",
                f"- status: {'applied' if op_id in applied else 'pending'}",
            ]
        )
        if operation.get("key"):
            lines.append(f"- relation_key: {operation['key']}")
        after_uid = str(operation.get("after_page_uid") or "")
        before_uid = str(operation.get("before_page_uid") or "")
        anchor_flag = f"--after {after_uid}" if after_uid else f"--before {before_uid}"
        if kind == "delete_page" and targets:
            lines.append(
                f"- command: `python skills/{skill_name}/scripts/"
                "orchestrator.py delete-page . "
                f"--revision-id {impact['revision_id']} --operation-id {op_id} "
                f"--uid {targets[0]}`"
            )
        elif kind == "insert_page" and (after_uid or before_uid):
            lines.append(
                f"- command: `python skills/{skill_name}/scripts/"
                "orchestrator.py insert-page . "
                f"--revision-id {impact['revision_id']} --operation-id {op_id} "
                f"{anchor_flag} --new-uid {operation['new_page_uid']}`"
            )
        elif kind == "move_page" and targets and (after_uid or before_uid):
            lines.append(
                f"- command: `python skills/{skill_name}/scripts/"
                "orchestrator.py move-page . "
                f"--revision-id {impact['revision_id']} --operation-id {op_id} "
                f"--uid {targets[0]} {anchor_flag}`"
            )
        lines.append("")
    lines.extend(
        [
        "## Dynamic Revision Groups",
        "",
        ]
    )
    for assignment in impact["assignments"]:
        lines.extend(
            [
                f"### {assignment['revision_group_id']}",
                f"- pages: {', '.join(assignment['page_uids']) or 'none'}",
                f"- positions: {', '.join(str(p) for p in assignment['positions']) or 'none'}",
                f"- owner_policy: {assignment['owner_policy']}",
                f"- owner_group_id: {assignment.get('owner_group_id') or 'new'}",
                f"- execution_role: {assignment.get('execution_role') or 'slide'}",
                f"- operation_kinds: {', '.join(assignment['operation_kinds'])}",
                f"- verification: {assignment['verification_scope']}",
                f"- issue_ids: {', '.join(assignment.get('issue_ids', [])) or 'none'}",
                "",
            ]
        )
    lines.extend(
        [
            "## Contract",
            "",
            "This file is a deterministic rendering of edit-operations.json and the current Deck Graph.",
            "Do not hand-edit it; rerun `prepare-revision` after changing operations.",
            "",
        ]
    )
    return "\n".join(lines)


def _operation_prompt_view(operation: dict, impact: dict, *, skill_name: str) -> dict:
    """Return the executable business fields without exposing state paths."""
    op_id = str(operation.get("operation_id") or "")
    kind = str(operation.get("kind") or "")
    targets = [str(uid) for uid in operation.get("target_page_uids", [])]
    applied = {str(value) for value in impact.get("applied_operation_ids", [])}
    view = {
        key: operation[key]
        for key in (
            "operation_id", "kind", "target_page_uids", "after_page_uid",
            "before_page_uid", "new_page_uid", "key", "source_issue_ids",
            "affected_page_uids",
        )
        if operation.get(key) not in (None, "", [], {})
    }
    view["status"] = "applied" if op_id in applied else "pending"
    after_uid = str(operation.get("after_page_uid") or "")
    before_uid = str(operation.get("before_page_uid") or "")
    anchor_flag = f"--after {after_uid}" if after_uid else f"--before {before_uid}"
    prefix = f"python skills/{skill_name}/scripts/orchestrator.py"
    if kind == "delete_page" and targets:
        view["command"] = (
            f"{prefix} delete-page . --revision-id {impact['revision_id']} "
            f"--operation-id {op_id} --uid {targets[0]}"
        )
    elif kind == "insert_page" and (after_uid or before_uid):
        view["command"] = (
            f"{prefix} insert-page . --revision-id {impact['revision_id']} "
            f"--operation-id {op_id} {anchor_flag} --new-uid {operation['new_page_uid']}"
        )
    elif kind == "move_page" and targets and (after_uid or before_uid):
        view["command"] = (
            f"{prefix} move-page . --revision-id {impact['revision_id']} "
            f"--operation-id {op_id} --uid {targets[0]} {anchor_flag}"
        )
    return view


def _active_revision_prompt_view(impact: dict, pointer: dict, *, skill_name: str) -> dict:
    """Project a recoverable revision receipt without filesystem provenance."""
    assignment_keys = (
        "revision_group_id", "page_uids", "positions", "operation_kinds",
        "owner_policy", "owner_group_id", "execution_role", "verification_scope",
        "issue_ids", "status",
    )
    return {
        "revision_id": str(impact.get("revision_id") or pointer.get("revision_id") or ""),
        "base_deck_version": int(impact.get("base_deck_version") or pointer.get("base_deck_version") or 0),
        "status": str(pointer.get("status") or impact.get("status") or ""),
        "route": str(impact.get("route") or ""),
        "structural_pending": bool(impact.get("structural_pending")),
        "operations": [
            _operation_prompt_view(operation, impact, skill_name=skill_name)
            for operation in impact.get("operations", [])
            if isinstance(operation, dict)
        ],
        "assignments": [
            {
                key: assignment[key]
                for key in assignment_keys
                if assignment.get(key) not in (None, "", [], {})
            }
            for assignment in impact.get("assignments", [])
            if isinstance(assignment, dict)
        ],
    }


def revision_context(root: Path) -> dict:
    """Expose current revision facts while keeping ``_state`` Harness-private."""
    root = Path(root).resolve()
    skill_name = _read_json(root / "_state/skill-ref.json").get("skill_name") or "mural-presenter-v1"
    index = _read_json(root / "_state/page-index.json")
    graph = _read_json(root / "_state/deck-graph.json")
    if index.get("schema") != PAGE_INDEX_SCHEMA or graph.get("schema") != DECK_GRAPH_SCHEMA:
        raise ValueError("run validate-plans before requesting revision context")
    if str(index.get("deck_id") or "") != str(graph.get("deck_id") or ""):
        raise ValueError("page-index and deck-graph refer to different decks")
    result = {
        "schema": REVISION_CONTEXT_SCHEMA,
        "deck_version": int(graph.get("deck_version") or 0),
        "pages": [
            {
                key: row[key]
                for key in ("position", "page_uid", "title")
                if row.get(key) not in (None, "")
            }
            for row in index.get("pages", [])
            if isinstance(row, dict)
        ],
        "active_revision": None,
    }
    pointer = _read_json(root / "_state/active-revision.json")
    if pointer.get("schema") != ACTIVE_REVISION_SCHEMA:
        return result
    raw_path = str(pointer.get("impact_path") or "")
    impact_path = (root / raw_path).resolve()
    try:
        impact_path.relative_to(root)
    except ValueError as exc:
        raise ValueError("active revision impact path escapes workspace") from exc
    impact = _read_json(impact_path)
    if impact.get("schema") != REVISION_IMPACT_SCHEMA:
        raise ValueError("active revision impact is missing or invalid")
    result["active_revision"] = _active_revision_prompt_view(
        impact, pointer, skill_name=str(skill_name)
    )
    return result


def compile_revision_impact(
    root: Path,
    operations_path: Path | None = None,
    *,
    revision_id: str = "",
) -> dict:
    """Compile one active revision and persist executable assignments."""
    root = Path(root).resolve()
    skill_name = _read_json(root / "_state/skill-ref.json")["skill_name"]
    index = _read_json(root / "_state" / "page-index.json")
    graph = _read_json(root / "_state" / "deck-graph.json")
    if index.get("schema") != PAGE_INDEX_SCHEMA or graph.get("schema") != DECK_GRAPH_SCHEMA:
        raise ValueError("run validate-plans before prepare-revision")
    if str(index.get("deck_id") or "") != str(graph.get("deck_id") or ""):
        raise ValueError("page-index and deck-graph refer to different decks")
    current_version = int(graph.get("deck_version") or 0)
    if current_version < 1 or int(index.get("deck_version") or 0) != current_version:
        raise ValueError("page-index and deck-graph versions disagree")

    source_path = Path(operations_path) if operations_path else root / "revision-operations.json"
    if not source_path.is_absolute():
        source_path = root / source_path
    source_path = source_path.resolve()
    try:
        source_path.relative_to(root)
    except ValueError as exc:
        raise ValueError("edit operations path must stay inside the workspace") from exc
    if not source_path.is_file():
        raise ValueError("edit operations JSON file not found; use write_file to create "
            f"revision-operations.json, then run: python skills/{skill_name}/scripts/"
            "orchestrator.py prepare-revision . (--operations accepts a file path, not JSON or @file)")
    source = _read_json(source_path)
    if source.get("schema") != EDIT_OPERATIONS_SCHEMA:
        raise ValueError(
            f"{source_path.relative_to(root)} must use schema {EDIT_OPERATIONS_SCHEMA}"
        )
    revision = _safe_token(
        revision_id or source.get("revision_id"), field="revision_id"
    )
    active_pointer = _read_json(root / "_state" / "active-revision.json")
    if active_pointer.get("schema") == ACTIVE_REVISION_SCHEMA:
        active_revision = str(active_pointer.get("revision_id") or "")
        active_status = str(active_pointer.get("status") or "")
        if active_revision and active_status not in {"committed", "aborted"}:
            active_path = (root / str(active_pointer.get("impact_path") or "")).resolve()
            try:
                active_path.relative_to(root)
            except ValueError as exc:
                raise ValueError("active revision impact path escapes workspace") from exc
            active_impact = _read_json(active_path)
            active_started = bool(active_impact.get("applied_operation_ids")) or any(
                isinstance(item, dict)
                and str(item.get("status") or "")
                not in {"", "ready", "blocked_on_structure"}
                for item in active_impact.get("assignments", [])
            )
            if active_revision != revision:
                raise ValueError(
                    f"revision {active_revision!r} is still active ({active_status}); "
                    "commit or explicitly abort it before preparing another revision"
                )
            if active_started:
                raise ValueError(
                    f"revision {revision!r} has already started execution; "
                    "do not recompile its impact after structure or assignment work begins"
                )
    base_version = int(source.get("base_deck_version") or 0)
    if base_version != current_version:
        raise ValueError(
            f"revision base version mismatch: operations={base_version}, current={current_version}"
        )
    raw_operations = source.get("operations")
    if not isinstance(raw_operations, list) or not raw_operations:
        raise ValueError("edit-operations.json requires a non-empty operations array")

    issue_ledger = _read_json(root / "_state" / "issues.json")
    issue_rows = {
        str(item.get("issue_id") or ""): item
        for item in issue_ledger.get("issues", {}).values()
        if issue_ledger.get("schema") == ISSUE_LEDGER_SCHEMA
        and isinstance(item, dict)
        and str(item.get("issue_id") or "")
    }
    issue_page_uids = {int(row["position"]): row["page_uid"] for row in index.get("pages", [])}
    for item in issue_rows.values():
        item["page_uids"] = [issue_page_uids[page] for page in item["pages"] if page in issue_page_uids]
    source_issue_ids = set(
        _tokens(source.get("source_issue_ids"), field="source_issue_ids")
    )

    rows = [dict(row) for row in index.get("pages", []) if isinstance(row, dict)]
    by_uid = {str(row.get("page_uid") or ""): row for row in rows}
    by_position = {
        int(row["position"]): str(row["page_uid"])
        for row in rows
        if str(row.get("page_uid") or "")
    }
    current_pages = [str(uid) for uid in graph.get("pages", []) if str(uid)]
    edges = [dict(edge) for edge in graph.get("edges", []) if isinstance(edge, dict)]
    normalized_ops: list[dict] = []
    direct_targets: set[str] = set()
    affected_pages: set[str] = set()
    deleted_pages: set[str] = set()
    created_pages: set[str] = set()
    structural_pending = False
    global_route = False

    for index_no, raw in enumerate(raw_operations, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"operation {index_no} must be an object")
        kind = str(raw.get("kind") or raw.get("operation") or "").strip().lower().replace("-", "_")
        if kind not in SUPPORTED_KINDS:
            raise ValueError(
                f"operation {index_no} kind {kind!r} is unsupported; expected one of "
                + ", ".join(sorted(SUPPORTED_KINDS))
            )
        op_id = _safe_token(
            raw.get("operation_id") or raw.get("op_id") or f"op-{index_no:02d}",
            field=f"operation {index_no} id",
        )
        operation_issue_ids = set(
            _tokens(
                raw.get("source_issue_ids") or raw.get("issue_ids"),
                field=f"operation {index_no} source_issue_ids",
            )
        )
        source_issue_ids.update(operation_issue_ids)
        targets = _operation_targets(raw, by_position)
        after_uid = str(raw.get("after_page_uid") or raw.get("after_uid") or "").strip()
        before_uid = str(raw.get("before_page_uid") or raw.get("before_uid") or "").strip()
        if kind in {"insert_page", "move_page"} and bool(after_uid) == bool(before_uid):
            raise ValueError(f"{kind} requires exactly one after_page_uid or before_page_uid")
        if kind == "insert_page":
            anchor = after_uid or before_uid
            if not anchor or anchor not in by_uid:
                raise ValueError("insert_page requires an existing after_page_uid or before_page_uid")
            if targets and set(targets) != {anchor}:
                raise ValueError(
                    "insert_page may only target its insertion anchor; use "
                    "new_page_uid for the new page"
                )
            targets = list(dict.fromkeys([*targets, anchor]))
            new_uid = _safe_token(raw.get("new_page_uid"), field="new_page_uid")
            if new_uid in by_uid:
                raise ValueError(f"new_page_uid {new_uid!r} already exists")
            created_pages.add(new_uid)
        else:
            new_uid = ""
        if kind == "move_page":
            anchor = after_uid or before_uid
            if not anchor or anchor not in by_uid:
                raise ValueError(
                    "move_page requires an existing after_page_uid or before_page_uid"
                )
            if anchor in targets:
                raise ValueError("move_page anchor must differ from its target page")
        if kind in {"delete_page", "move_page"} and len(targets) != 1:
            raise ValueError(f"{kind} requires exactly one target page_uid")
        missing = [uid for uid in targets if uid not in by_uid]
        if missing:
            raise ValueError(
                f"operation {op_id} targets unknown current page_uid(s): {missing}"
            )
        if not targets and kind not in GLOBAL_KINDS:
            raise ValueError(f"operation {op_id} requires at least one target page")

        direct = set(targets)
        impacted = set(direct)
        if kind in STRUCTURAL_KINDS:
            structural_pending = True
            impacted = _narrative_neighbors(direct, edges)
            structural_anchor = after_uid or before_uid
            if structural_anchor:
                impacted.update(_narrative_neighbors({structural_anchor}, edges))
            if kind == "delete_page":
                deleted_pages.update(direct)
            if kind == "insert_page":
                impacted.add(new_uid)
        elif kind in GLOBAL_KINDS:
            global_route = True
            impacted = set(current_pages)
        direct_targets.update(direct)
        affected_pages.update(impacted)
        normalized_ops.append(
            {
                "operation_id": op_id,
                "kind": kind,
                "target_page_uids": sorted(direct),
                "after_page_uid": after_uid or None,
                "before_page_uid": before_uid or None,
                "new_page_uid": new_uid or None,
                "key": str(raw.get("key") or "").strip() or None,
                "joint": bool(raw.get("joint", False)),
                "source_issue_ids": sorted(operation_issue_ids),
                "affected_page_uids": sorted(impacted),
            }
        )

    unknown_issue_ids = sorted(source_issue_ids - set(issue_rows))
    if unknown_issue_ids:
        raise ValueError(
            "edit operations reference unknown Review issue ID(s): "
            + ", ".join(unknown_issue_ids)
        )
    inactive_issue_ids = sorted(
        issue_id
        for issue_id in source_issue_ids
        if str(issue_rows[issue_id].get("status") or "")
        not in {"open", "unresolved"}
    )
    if inactive_issue_ids:
        raise ValueError(
            "Review issue ID(s) are already verified or inactive: "
            + ", ".join(inactive_issue_ids)
        )

    active_targets = sorted((affected_pages - deleted_pages) | created_pages)
    kinds = sorted({str(operation["kind"]) for operation in normalized_ops})
    owners = _owner_rows(root)
    owner_by_uid = {uid: _owner_for_uid(owners, uid) for uid in active_targets}
    force_joint = global_route or structural_pending or any(
        bool(operation.get("joint")) for operation in normalized_ops
    )
    buckets: list[list[str]] = []
    if global_route:
        buckets = [active_targets]
    elif force_joint:
        buckets = [active_targets] if active_targets else []
    else:
        grouped: dict[str, list[str]] = defaultdict(list)
        for uid in active_targets:
            owner = owner_by_uid.get(uid)
            key = (
                f"owner:{owner.get('owner_id')}"
                if isinstance(owner, dict) and str(owner.get("owner_id") or "")
                else f"page:{uid}"
            )
            grouped[key].append(uid)
        buckets = [sorted(values) for _key, values in sorted(grouped.items())]

    assignments: list[dict] = []
    for page_uids in buckets:
        bucket_owners = {
            str(owner_by_uid[uid].get("owner_id"))
            for uid in page_uids
            if isinstance(owner_by_uid.get(uid), dict)
            and str(owner_by_uid[uid].get("owner_id") or "")
        }
        reusable_owner = None
        if len(bucket_owners) == 1 and not (set(kinds) & (STRUCTURAL_KINDS | GLOBAL_KINDS)):
            reusable_owner = next(
                (
                    owner_by_uid[uid]
                    for uid in page_uids
                    if isinstance(owner_by_uid.get(uid), dict)
                ),
                None,
            )
        owner_policy = "reuse_owner" if reusable_owner is not None else (
            "replan" if "replan" in kinds else "new_revision_group"
        )
        revision_group_id = _assignment_id(revision, page_uids, kinds)
        bucket_page_uids = set(page_uids)
        assignment_issue_ids: set[str] = set()
        for operation in normalized_ops:
            operation_ids = {
                str(value)
                for value in operation.get("source_issue_ids", [])
                if str(value)
            }
            affected = {
                str(value)
                for value in operation.get("affected_page_uids", [])
                if str(value)
            }
            if operation_ids and (not affected or affected & bucket_page_uids):
                assignment_issue_ids.update(operation_ids)
        for issue_id in source_issue_ids:
            issue_pages = {
                str(value)
                for value in issue_rows[issue_id].get("page_uids", [])
                if str(value)
            }
            if not issue_pages or issue_pages & bucket_page_uids:
                assignment_issue_ids.add(issue_id)
        assignments.append(
            {
                "assignment_id": revision_group_id,
                "revision_group_id": revision_group_id,
                "page_uids": page_uids,
                "positions": sorted(
                    int(by_uid[uid]["position"])
                    for uid in page_uids
                    if uid in by_uid
                ),
                "operation_kinds": kinds,
                "owner_policy": owner_policy,
                "owner_id": (
                    str(reusable_owner.get("owner_id") or "")
                    if reusable_owner is not None
                    else None
                ),
                "owner_group_id": (
                    str(reusable_owner.get("group_id") or "")
                    if reusable_owner is not None
                    else revision_group_id
                ),
                "owner_contract_page_uids": (
                    list(reusable_owner.get("page_uids") or [])
                    if reusable_owner is not None
                    else page_uids
                ),
                "verification_scope": _verification_scope(
                    page_uids, set(kinds), len(bucket_owners)
                ),
                "issue_ids": sorted(assignment_issue_ids),
                "status": "blocked_on_structure" if structural_pending else "ready",
            }
        )

    # A pure shared-system repair is executed by the persistent Orchestrator
    # through the versioned base.css transaction. It deliberately has no Slide
    # child: finalize re-renders the whole Deck and root native vision checks
    # the resulting contact sheet/pages. Keep an assignment so issue lifecycle
    # and revision commit remain transactional, but make its executor explicit.
    if set(kinds) == {"shared_system"}:
        for assignment in assignments:
            assignment["owner_policy"] = "orchestrator_shared_system"
            assignment["owner_id"] = "orchestrator"
            assignment["owner_group_id"] = "__shared__"
            assignment["execution_role"] = "orchestrator"
            assignment["verification_scope"] = "whole_deck"

    unbound = sorted(
        issue_id
        for issue_id in source_issue_ids
        if not any(
            issue_id in {
                str(value)
                for value in assignment.get("issue_ids", [])
                if str(value)
            }
            for assignment in assignments
        )
    )
    if unbound:
        raise ValueError(
            "Review issue(s) have no executable revision assignment: "
            + ", ".join(unbound)
        )

    route = "replan" if "replan" in kinds else (
        "structural_edit" if structural_pending else "dynamic_revision"
    )
    impact = {
        "schema": REVISION_IMPACT_SCHEMA,
        "revision_id": revision,
        "deck_id": str(graph.get("deck_id") or ""),
        "base_deck_version": current_version,
        "plan_epoch": int(graph.get("plan_epoch") or 0),
        "operations_sha256": hashlib.sha256(
            json.dumps(normalized_ops, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "route": route,
        "operations": normalized_ops,
        "source_issue_ids": sorted(source_issue_ids),
        "direct_targets": sorted(direct_targets),
        "affected_pages": sorted(affected_pages | created_pages),
        "retired_page_uids": sorted(deleted_pages),
        "created_page_uids": sorted(created_pages),
        "structural_pending": structural_pending,
        "assignments": assignments,
        "status": "prepared",
        "created_at": time.time(),
    }
    revision_root = root / "_state" / "revisions" / revision
    impact_path = revision_root / "impact.json"
    _atomic_write_json(impact_path, impact)
    _atomic_write_json(
        root / "_state" / "active-revision.json",
        {
            "schema": ACTIVE_REVISION_SCHEMA,
            "revision_id": revision,
            "base_deck_version": current_version,
            "impact_path": str(impact_path.relative_to(root)),
            "operations_path": str(source_path.relative_to(root)),
            "status": "prepared",
        },
    )
    human_path = root / "_state" / "revision-impact.md"
    human_path.parent.mkdir(parents=True, exist_ok=True)
    human_path.write_text(_human_impact_markdown(impact, skill_name=skill_name), encoding="utf-8")
    return impact


__all__ = [
    "ACTIVE_REVISION_SCHEMA",
    "EDIT_OPERATIONS_SCHEMA",
    "REVISION_IMPACT_SCHEMA",
    "REVISION_CONTEXT_SCHEMA",
    "compile_revision_impact",
    "revision_context",
]
