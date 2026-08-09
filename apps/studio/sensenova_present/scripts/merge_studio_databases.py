#!/usr/bin/env python3
"""Merge one stopped Studio SQLite database into the authoritative database.

The merge is intentionally offline for the source: queued/running/waiting Decks
make the command refuse to apply. IDs are remapped transactionally, local users
win username/password conflicts, and both databases are backed up before the
target is changed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any


ACTIVE_STATUSES = ("waiting", "queued", "running")
EXPECTED_TABLES = {
    "users",
    "sessions",
    "conversations",
    "messages",
    "batches",
    "decks",
    "deck_usage",
    "custom_models",
}


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def tables(con: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def table_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in con.execute(f'PRAGMA table_info("{table}")')]


def table_count(con: sqlite3.Connection, table: str) -> int:
    return int(con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def allocate_ids(
    target: sqlite3.Connection,
    table: str,
    source_ids: list[int],
) -> dict[int, int]:
    maximum = int(target.execute(f'SELECT COALESCE(MAX(id), 0) FROM "{table}"').fetchone()[0])
    return {source_id: maximum + offset for offset, source_id in enumerate(source_ids, 1)}


def insert_row(
    target: sqlite3.Connection,
    table: str,
    source_row: sqlite3.Row,
    overrides: dict[str, Any] | None = None,
) -> None:
    values = dict(source_row)
    values.update(overrides or {})
    allowed = set(table_columns(target, table))
    values = {key: value for key, value in values.items() if key in allowed}
    columns = list(values)
    placeholders = ", ".join("?" for _ in columns)
    quoted = ", ".join(f'"{column}"' for column in columns)
    target.execute(
        f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})',
        [values[column] for column in columns],
    )


def remap_seed(seed_json: str | None, deck_map: dict[int, int]) -> str | None:
    if not seed_json:
        return seed_json
    try:
        payload = json.loads(seed_json)
    except (TypeError, ValueError):
        return seed_json

    def visit(value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            return {item_key: visit(item, item_key) for item_key, item in value.items()}
        if isinstance(value, list):
            return [visit(item, key) for item in value]
        if key in {"deck_id", "parent_deck_id", "root_deck_id"}:
            try:
                return deck_map.get(int(value), value)
            except (TypeError, ValueError):
                return value
        return value

    return json.dumps(visit(payload), ensure_ascii=False)


def backup_database(con: sqlite3.Connection, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = sqlite3.connect(destination)
    try:
        con.backup(backup)
    finally:
        backup.close()


def foreign_key_violations(con: sqlite3.Connection) -> set[tuple[Any, ...]]:
    return {tuple(row) for row in con.execute("PRAGMA foreign_key_check")}


def build_plan(target: sqlite3.Connection, source: sqlite3.Connection) -> dict[str, Any]:
    target_tables = tables(target)
    source_tables = tables(source)
    missing_target = sorted(EXPECTED_TABLES - target_tables)
    missing_source = sorted(EXPECTED_TABLES - source_tables)
    if missing_target or missing_source:
        raise RuntimeError(
            f"schema mismatch: target missing={missing_target}, source missing={missing_source}"
        )
    for table in EXPECTED_TABLES:
        target_columns = set(table_columns(target, table))
        source_columns = set(table_columns(source, table))
        if target_columns != source_columns:
            raise RuntimeError(
                f"column mismatch for table {table}: "
                f"target-only={sorted(target_columns - source_columns)}, "
                f"source-only={sorted(source_columns - target_columns)}"
            )

    active = [
        dict(row)
        for row in source.execute(
            "SELECT id, status, title, run_dir FROM decks "
            "WHERE status IN (?, ?, ?) ORDER BY id",
            ACTIVE_STATUSES,
        )
    ]
    source_runs = {
        row[0]
        for row in source.execute("SELECT run_dir FROM decks WHERE run_dir IS NOT NULL")
        if row[0]
    }
    duplicate_runs = sorted(
        row[0]
        for row in target.execute("SELECT run_dir FROM decks WHERE run_dir IS NOT NULL")
        if row[0] in source_runs
    )
    target_users = {
        row["username"]: dict(row)
        for row in target.execute("SELECT * FROM users ORDER BY id")
    }
    user_conflicts = []
    new_users = []
    for row in source.execute("SELECT * FROM users ORDER BY id"):
        existing = target_users.get(row["username"])
        if existing:
            user_conflicts.append(
                {
                    "username": row["username"],
                    "target_user_id": existing["id"],
                    "source_user_id": row["id"],
                    "same_password_hash": existing["password_hash"] == row["password_hash"],
                    "resolution": "keep target account and attach source history",
                }
            )
        else:
            new_users.append(row["username"])

    return {
        "target_counts": {table: table_count(target, table) for table in sorted(EXPECTED_TABLES)},
        "source_counts": {table: table_count(source, table) for table in sorted(EXPECTED_TABLES)},
        "active_source_decks": active,
        "duplicate_run_dirs": duplicate_runs,
        "new_users": new_users,
        "user_conflicts": user_conflicts,
    }


def merge(target: sqlite3.Connection, source: sqlite3.Connection) -> dict[str, Any]:
    source_custom_models = table_count(source, "custom_models")
    if source_custom_models:
        raise RuntimeError(
            "source has custom models encrypted with a different key; merge them separately"
        )

    baseline_fk = foreign_key_violations(target)
    target.execute("BEGIN IMMEDIATE")
    try:
        target_users = {
            row["username"]: row["id"]
            for row in target.execute("SELECT id, username FROM users")
        }
        source_users = list(source.execute("SELECT * FROM users ORDER BY id"))
        new_source_user_ids = [row["id"] for row in source_users if row["username"] not in target_users]
        new_user_map = allocate_ids(target, "users", new_source_user_ids)
        user_map: dict[int, int] = {}
        for row in source_users:
            existing = target_users.get(row["username"])
            if existing is not None:
                user_map[row["id"]] = existing
                continue
            target_id = new_user_map[row["id"]]
            insert_row(target, "users", row, {"id": target_id})
            user_map[row["id"]] = target_id

        batches = list(source.execute("SELECT * FROM batches ORDER BY id"))
        batch_map = allocate_ids(target, "batches", [row["id"] for row in batches])
        for row in batches:
            insert_row(
                target,
                "batches",
                row,
                {"id": batch_map[row["id"]], "user_id": user_map[row["user_id"]]},
            )

        conversations = list(source.execute("SELECT * FROM conversations ORDER BY id"))
        conversation_map = allocate_ids(
            target, "conversations", [row["id"] for row in conversations]
        )
        for row in conversations:
            insert_row(
                target,
                "conversations",
                row,
                {
                    "id": conversation_map[row["id"]],
                    "user_id": user_map[row["user_id"]],
                },
            )

        decks = list(source.execute("SELECT * FROM decks ORDER BY id"))
        deck_map = allocate_ids(target, "decks", [row["id"] for row in decks])
        for row in decks:
            old_conversation = row["conversation_id"]
            old_batch = row["batch_id"]
            old_parent = row["parent_deck_id"]
            insert_row(
                target,
                "decks",
                row,
                {
                    "id": deck_map[row["id"]],
                    "user_id": user_map[row["user_id"]],
                    "conversation_id": (
                        conversation_map.get(old_conversation) if old_conversation is not None else None
                    ),
                    "batch_id": batch_map.get(old_batch) if old_batch is not None else None,
                    "parent_deck_id": deck_map.get(old_parent) if old_parent is not None else None,
                    "seed_json": remap_seed(row["seed_json"], deck_map),
                },
            )

        messages = list(source.execute("SELECT * FROM messages ORDER BY id"))
        message_map = allocate_ids(target, "messages", [row["id"] for row in messages])
        for row in messages:
            old_deck = row["deck_id"]
            insert_row(
                target,
                "messages",
                row,
                {
                    "id": message_map[row["id"]],
                    "conversation_id": conversation_map[row["conversation_id"]],
                    "deck_id": deck_map.get(old_deck) if old_deck is not None else None,
                },
            )

        session_tokens = {row[0] for row in target.execute("SELECT token FROM sessions")}
        imported_sessions = 0
        for row in source.execute("SELECT * FROM sessions ORDER BY created_at, token"):
            if row["token"] in session_tokens:
                continue
            insert_row(target, "sessions", row, {"user_id": user_map[row["user_id"]]})
            imported_sessions += 1

        for row in source.execute("SELECT * FROM deck_usage ORDER BY deck_id"):
            insert_row(
                target,
                "deck_usage",
                row,
                {
                    "deck_id": deck_map[row["deck_id"]],
                    "user_id": user_map[row["user_id"]],
                },
            )

        new_fk = foreign_key_violations(target)
        introduced_fk = new_fk - baseline_fk
        if introduced_fk:
            raise RuntimeError(f"merge introduced foreign-key violations: {sorted(introduced_fk)}")
        target.commit()
    except Exception:
        target.rollback()
        raise

    return {
        "users": user_map,
        "batches": batch_map,
        "conversations": conversation_map,
        "decks": deck_map,
        "messages": message_map,
        "sessions_imported": imported_sessions,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    target_path = args.target.expanduser().resolve()
    source_path = args.source.expanduser().resolve()
    if target_path == source_path:
        raise SystemExit("target and source must be different databases")

    target = connect(target_path)
    source = connect(source_path, readonly=True)
    try:
        plan = build_plan(target, source)
        print(json.dumps({"plan": plan}, ensure_ascii=False, indent=2))
        if not args.apply:
            return
        if plan["active_source_decks"]:
            raise RuntimeError("source still has active Decks; stop and settle them before merging")
        if plan["duplicate_run_dirs"]:
            raise RuntimeError("source appears to have already been imported")

        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_dir = (args.backup_dir or target_path.parent / "merge-backups").resolve()
        target_backup = backup_dir / f"{target_path.stem}.target-before-{stamp}.db"
        source_backup = backup_dir / f"{source_path.stem}.source-before-{stamp}.db"
        backup_database(target, target_backup)
        backup_database(source, source_backup)
        result = merge(target, source)
        print(
            json.dumps(
                {
                    "result": result,
                    "target_backup": str(target_backup),
                    "source_backup": str(source_backup),
                    "target_counts": {
                        table: table_count(target, table) for table in sorted(EXPECTED_TABLES)
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        source.close()
        target.close()


if __name__ == "__main__":
    main()
