"""Recoverable deterministic page delete/insert/move operations for revisions."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time

from .deck_core import _load_plans
from .deck_plan import read_deck
from .deck_state import (
    DECK_GRAPH_SCHEMA,
    PAGE_INDEX_SCHEMA,
    _plan_hash,
    compile_deck_state,
    require_revision_transaction,
)
from .revision_router import (
    ACTIVE_REVISION_SCHEMA,
    REVISION_IMPACT_SCHEMA,
    _assignment_id,
    _atomic_write_json,
    _human_impact_markdown,
    _read_json,
)


def _atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    with temporary.open("wb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _active_impact(root: Path, revision_id: str) -> tuple[dict, Path]:
    pointer = _read_json(root / "_state" / "active-revision.json")
    if pointer.get("schema") != ACTIVE_REVISION_SCHEMA:
        raise ValueError("prepare-revision must run before a structural edit")
    active = str(pointer.get("revision_id") or "")
    if active != str(revision_id or ""):
        raise ValueError(f"active revision is {active!r}, not {revision_id!r}")
    impact_path = (root / str(pointer.get("impact_path") or "")).resolve()
    try:
        impact_path.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("active revision impact path escapes workspace") from exc
    impact = _read_json(impact_path)
    if impact.get("schema") != REVISION_IMPACT_SCHEMA:
        raise ValueError("active revision impact is missing or malformed")
    return impact, impact_path


def _current_state(root: Path, impact: dict) -> tuple[dict, dict, list[dict]]:
    index = _read_json(root / "_state" / "page-index.json")
    graph = _read_json(root / "_state" / "deck-graph.json")
    if index.get("schema") != PAGE_INDEX_SCHEMA or graph.get("schema") != DECK_GRAPH_SCHEMA:
        raise ValueError("run validate-plans before structural revision commands")
    current_version = int(graph.get("deck_version") or 0)
    if current_version != int(impact.get("base_deck_version") or 0):
        raise ValueError(
            f"structural revision is based on v{impact.get('base_deck_version')}, "
            f"current Deck is v{current_version}"
        )
    rows = sorted(
        [dict(row) for row in index.get("pages", []) if isinstance(row, dict)],
        key=lambda row: int(row.get("position") or 0),
    )
    if not rows:
        raise ValueError("page-index contains no pages")
    return index, graph, rows


def _operation(impact: dict, operation_id: str, kind: str) -> dict:
    matches = [
        dict(item)
        for item in impact.get("operations", [])
        if isinstance(item, dict)
        and str(item.get("operation_id") or "") == str(operation_id or "")
    ]
    if len(matches) != 1 or str(matches[0].get("kind") or "") != kind:
        raise ValueError(
            f"active impact has no unique {kind} operation {operation_id!r}"
        )
    applied = {str(value) for value in impact.get("applied_operation_ids", [])}
    if operation_id in applied:
        return {**matches[0], "already_applied": True}
    return matches[0]


def _renumber_html(text: str, number: int, total: int) -> str:
    value = text
    root = re.search(r'''\bid\s*=\s*(["'])(slide-\d+)\1''', text)
    if root:
        # 页号变化必须同步本页 CSS/JS/片段引用，否则样式仍指向旧根节点。
        old_id, new_id = re.escape(root.group(2)), f"slide-{number:02d}"
        value = re.sub(rf'''(["'`]){old_id}\1''',
                       lambda m: m.group(1) + new_id + m.group(1), value)
        value = re.sub(rf"#{old_id}(?![\w-])", f"#{new_id}", value)
    value = re.sub(
        r'''(\bdata-slide\s*=\s*)(["'])\d+\2''',
        lambda m: m.group(1) + m.group(2) + f"{number:02d}" + m.group(2), value, count=1
    )
    value = re.sub(
        r'(<span\b[^>]*class="[^"]*\bpage-(?:no|number)\b[^"]*"[^>]*>)'
        r'\s*\d+\s*/\s*\d+',
        rf"\g<1>{number:02d} / {total:02d}",
        value,
        count=1,
        flags=re.I,
    )
    return value


def _insert_plan() -> str:
    return """# Inserted page

## 本页任务
依据当前修订要求补齐本页意图及与前后页的衔接。

## 页面内容与证据
补齐可直接制作的内容、来源定位及必要的事实限定；没有依据的主张不要定稿。

## 视觉表达与约束
沿用整册设计规范，说明本页表达关系以及必须保留的内容。

## 讲稿
"""


def _materialize_order(
    root: Path,
    rows: list[dict],
    ordered_uids: list[str],
    *,
    inserted_uid: str = "",
) -> tuple[list[str], list[str]]:
    """Rewrite canonical numbered files and return changed/renderable UIDs."""
    by_uid = {str(row.get("page_uid") or ""): row for row in rows}
    _, pages = read_deck(root, len(rows))
    plans_by_number = {row["number"]: (root / row["path"]).read_bytes() for row in pages}
    content: dict[str, dict[str, bytes]] = {}
    for uid, row in by_uid.items():
        files: dict[str, bytes] = {}
        for kind, key in (
            ("html", "html_path"),
            ("png", "png_path"),
        ):
            path = root / str(row.get(key) or "")
            if path.is_file():
                files[kind] = path.read_bytes()
        content[uid] = files
    for directory, pattern in [
        ("briefs", "slide_*.md"),
        ("slides", "slide_*.html"),
        ("renders", "slide_*.png"),
    ]:
        for path in (root / directory).glob(pattern):
            path.unlink()

    total = len(ordered_uids)
    changed: list[str] = []
    renderable: list[str] = []
    page_sections = []
    for number, uid in enumerate(ordered_uids, start=1):
        old = by_uid.get(uid)
        if uid == inserted_uid:
            page_sections.append(_insert_plan().encode("utf-8"))
            changed.append(uid)
            continue
        if old is None:
            raise ValueError(f"missing canonical plan for page_uid {uid}")
        old_position = int(old.get("position") or 0)
        page_sections.append(plans_by_number[old_position])
        html_bytes = content[uid].get("html")
        if html_bytes is not None:
            html_text = _renumber_html(html_bytes.decode("utf-8"), number, total)
            _atomic_write_bytes(
                root / "slides" / f"slide_{number:02d}.html",
                html_text.encode("utf-8"),
            )
            renderable.append(uid)
            if html_text.encode("utf-8") != html_bytes:
                changed.append(uid)
            elif old_position != number:
                changed.append(uid)
            elif "png" in content[uid]:
                _atomic_write_bytes(root / "renders" / f"slide_{number:02d}.png", content[uid]["png"])
        if old_position != number and uid not in changed:
            changed.append(uid)
    for number, page_bytes in enumerate(page_sections, 1):
        _atomic_write_bytes(root / "briefs" / f"slide_{number:02d}.md", page_bytes)
    return list(dict.fromkeys(changed)), renderable


def _provisional_index(
    root: Path,
    old_index: dict,
    ordered_uids: list[str],
    *,
    inserted_uid: str = "",
) -> None:
    old_by_uid = {
        str(row.get("page_uid") or ""): dict(row)
        for row in old_index.get("pages", [])
        if isinstance(row, dict)
    }
    rows: list[dict] = []
    _, plans = _load_plans(root, len(ordered_uids))
    for position, uid in enumerate(ordered_uids, start=1):
        plan = plans[position - 1]
        old_row = old_by_uid.get(uid) or {}
        row = {
            "page_uid": uid,
            "position": position,
            "plan_path": Path(plan["path"]).relative_to(root).as_posix(),
            "plan_section": plan["plan_section"],
            "html_path": f"slides/slide_{position:02d}.html",
            "png_path": f"renders/slide_{position:02d}.png",
            "title": str(plan.get("title") or ""),
            "source_plan_sha256": _plan_hash(plan),
            "status": "planned" if uid == inserted_uid else str(old_row.get("status") or "planned"),
        }
        rows.append(row)
    provisional = dict(old_index)
    provisional["pages"] = rows
    provisional.pop("index_sha256", None)
    provisional["index_sha256"] = hashlib.sha256(
        json.dumps(provisional, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    _atomic_write_json(root / "_state" / "page-index.json", provisional)


def _compile_current(root: Path, ordered_uids: list[str]) -> dict:
    deck, plans = _load_plans(root, len(ordered_uids))
    return compile_deck_state(root, deck, plans, ordered_uids=ordered_uids)


def _refresh_impact(
    root: Path,
    impact: dict,
    impact_path: Path,
    *,
    operation_id: str,
    semantic_uids: set[str],
    retired_uids: set[str],
) -> dict:
    applied = list(dict.fromkeys(
        [*impact.get("applied_operation_ids", []), operation_id]
    ))
    structural_ids = {
        str(item.get("operation_id") or "")
        for item in impact.get("operations", [])
        if isinstance(item, dict)
        and str(item.get("kind") or "") in {"delete_page", "insert_page", "move_page"}
    }
    pending = structural_ids - set(applied)
    index = _read_json(root / "_state" / "page-index.json")
    by_uid = {
        str(row.get("page_uid") or ""): dict(row)
        for row in index.get("pages", [])
        if isinstance(row, dict)
    }
    current_semantic = sorted(uid for uid in semantic_uids if uid in by_uid)
    kinds = sorted({
        str(item.get("kind") or "")
        for item in impact.get("operations", [])
        if isinstance(item, dict)
    })
    assignments = []
    if current_semantic:
        group_id = _assignment_id(str(impact["revision_id"]), current_semantic, kinds)
        assignments.append(
            {
                "assignment_id": group_id,
                "revision_group_id": group_id,
                "page_uids": current_semantic,
                "positions": sorted(int(by_uid[uid]["position"]) for uid in current_semantic),
                "operation_kinds": kinds,
                "owner_policy": "new_revision_group",
                "owner_id": None,
                "owner_group_id": group_id,
                "owner_contract_page_uids": current_semantic,
                "verification_scope": "affected_montage+whole_deck_invariants",
                "status": "blocked_on_structure" if pending else "ready",
            }
        )
    impact["applied_operation_ids"] = applied
    impact["structural_pending"] = bool(pending)
    impact["retired_page_uids"] = sorted(
        set(impact.get("retired_page_uids", [])) | retired_uids
    )
    impact["affected_pages"] = sorted(
        (set(impact.get("affected_pages", [])) | semantic_uids) - retired_uids
    )
    impact["assignments"] = assignments
    impact["plan_epoch"] = int(index.get("plan_epoch") or impact.get("plan_epoch") or 0)
    impact["status"] = "structure_applied" if not pending else "structure_in_progress"
    impact["structure_updated_at"] = time.time()
    _atomic_write_json(impact_path, impact)
    pointer_path = root / "_state" / "active-revision.json"
    pointer = _read_json(pointer_path)
    pointer["status"] = impact["status"]
    _atomic_write_json(pointer_path, pointer)
    (root / "_state" / "revision-impact.md").write_text(
        _human_impact_markdown(impact, skill_name=_read_json(root / "_state/skill-ref.json")["skill_name"]), encoding="utf-8"
    )
    return impact


def _apply_order_change(
    root: Path,
    revision_id: str,
    operation_id: str,
    *,
    kind: str,
    target_uid: str,
    after_uid: str = "",
    before_uid: str = "",
    new_uid: str = "",
) -> dict:
    root = Path(root).resolve()
    require_revision_transaction(root)
    impact, impact_path = _active_impact(root, revision_id)
    operation = _operation(impact, operation_id, kind)
    if operation.get("already_applied"):
        return impact
    old_index, _graph, rows = _current_state(root, impact)
    ordered = [str(row.get("page_uid") or "") for row in rows]
    by_uid = {str(row.get("page_uid") or ""): row for row in rows}
    if target_uid not in by_uid and kind != "insert_page":
        raise ValueError(f"page_uid {target_uid!r} is not in the current Deck")
    anchor_uid = after_uid or before_uid
    if anchor_uid and anchor_uid not in by_uid:
        raise ValueError(f"anchor page_uid {anchor_uid!r} is not in the current Deck")
    operation_targets = [
        str(uid) for uid in operation.get("target_page_uids", []) if str(uid)
    ]
    if kind in {"delete_page", "move_page"} and operation_targets != [target_uid]:
        raise ValueError(
            f"{kind} arguments do not match prepared operation targets "
            f"{operation_targets}"
        )
    prepared_after = str(operation.get("after_page_uid") or "")
    prepared_before = str(operation.get("before_page_uid") or "")
    if kind in {"insert_page", "move_page"}:
        if bool(after_uid) == bool(before_uid):
            raise ValueError(f"{kind} requires exactly one --after or --before anchor")
        if (prepared_after, prepared_before) != (after_uid, before_uid):
            raise ValueError(f"{kind} arguments do not match the prepared anchor")
    if kind == "insert_page" and str(operation.get("new_page_uid") or "") != new_uid:
        raise ValueError(
            f"insert_page new UID {new_uid!r} does not match prepared operation"
        )
    retired: set[str] = set()
    inserted = ""
    old_positions = {uid: index for index, uid in enumerate(ordered)}
    if kind == "delete_page":
        ordered.remove(target_uid)
        retired.add(target_uid)
    elif kind == "move_page":
        if anchor_uid == target_uid:
            raise ValueError("move-page requires a distinct anchor page_uid")
        ordered.remove(target_uid)
        ordered.insert(ordered.index(anchor_uid) + bool(after_uid), target_uid)
    elif kind == "insert_page":
        inserted = new_uid
        if not inserted or inserted in by_uid:
            raise ValueError("insert-page requires one unused new page_uid")
        ordered.insert(ordered.index(anchor_uid) + bool(after_uid), inserted)
    if not ordered:
        raise ValueError("a Deck must retain at least one page")
    changed_uids, renderable = _materialize_order(
        root,
        rows,
        ordered,
        inserted_uid=inserted,
    )
    _provisional_index(root, old_index, ordered, inserted_uid=inserted)
    _compile_current(root, ordered)

    semantic = set(str(uid) for uid in impact.get("affected_pages", []))
    semantic.discard(target_uid if kind == "delete_page" else "")
    if inserted:
        semantic.add(inserted)
    if kind == "move_page":
        semantic.update({target_uid, anchor_uid})
        old_index_no = old_positions[target_uid]
        for index_no in (old_index_no - 1, old_index_no + 1):
            if 0 <= index_no < len(rows):
                semantic.add(str(rows[index_no].get("page_uid") or ""))
    return _refresh_impact(
        root,
        impact,
        impact_path,
        operation_id=operation_id,
        semantic_uids=semantic,
        retired_uids=retired,
    )


def delete_page(
    root: Path,
    *,
    revision_id: str,
    operation_id: str,
    page_uid: str,
) -> dict:
    return _apply_order_change(
        root,
        revision_id,
        operation_id,
        kind="delete_page",
        target_uid=str(page_uid),
    )


def move_page(
    root: Path,
    *,
    revision_id: str,
    operation_id: str,
    page_uid: str,
    after_uid: str = "",
    before_uid: str = "",
) -> dict:
    return _apply_order_change(
        root,
        revision_id,
        operation_id,
        kind="move_page",
        target_uid=str(page_uid),
        after_uid=str(after_uid),
        before_uid=str(before_uid),
    )


def insert_page(
    root: Path,
    *,
    revision_id: str,
    operation_id: str,
    new_uid: str,
    after_uid: str = "",
    before_uid: str = "",
) -> dict:
    return _apply_order_change(
        root,
        revision_id,
        operation_id,
        kind="insert_page",
        target_uid=str(after_uid or before_uid),
        after_uid=str(after_uid),
        before_uid=str(before_uid),
        new_uid=str(new_uid),
    )


__all__ = ["delete_page", "insert_page", "move_page"]
