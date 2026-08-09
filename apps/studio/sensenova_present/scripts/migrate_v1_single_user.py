#!/usr/bin/env python3
"""Merge Studio ownership into the anonymous V1 ``user`` workspace.

The migration is deliberately non-destructive to generated workspaces: rows
retain their deck IDs, conversation IDs and absolute run directories.  Only
ownership metadata is consolidated.  A SQLite backup and a dynamic-owner
manifest are written before an applied migration.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import time
from pathlib import Path


OWNED_TABLES = ("conversations", "decks", "batches", "deck_usage", "custom_models")


def connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=10000")
    return con


def ensure_user(con: sqlite3.Connection, username: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if row is None:
        now = int(time.time())
        con.execute(
            "INSERT INTO users(username,password_hash,display_name,role,is_active,created_at) "
            "VALUES(?,?,?,?,1,?)",
            (username, "disabled-v1", "user", "user", now),
        )
        row = con.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return row


def table_count(con: sqlite3.Connection, table: str) -> int:
    return int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def choose_config(data_dir: Path, canonical_id: int, base_user_id: int | None) -> Path | None:
    root = data_dir / "user_configs"
    preferred = [root / f"{canonical_id}.json"]
    if base_user_id is not None:
        preferred.append(root / f"{base_user_id}.json")
    for path in preferred:
        if path.is_file():
            return path
    candidates = sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def dynamic_meta_files(data_dir: Path) -> list[Path]:
    return sorted((data_dir / "dynamic_runs").glob("*/meta.json"))


def backup_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = connect(source)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def migrate(args: argparse.Namespace) -> dict:
    db_path = args.db.resolve()
    data_dir = args.data_dir.resolve()
    con = connect(db_path)
    try:
        users_before = [dict(row) for row in con.execute(
            "SELECT id,username,is_active FROM users ORDER BY id"
        )]
        counts = {table: table_count(con, table) for table in OWNED_TABLES}
        counts["history_preferences"] = table_count(con, "history_preferences")
        dynamic_files = dynamic_meta_files(data_dir)
        plan = {
            "db": str(db_path),
            "data_dir": str(data_dir),
            "canonical_username": args.username,
            "users_before": users_before,
            "records": counts,
            "dynamic_runs": len(dynamic_files),
            "apply": args.apply,
        }
        if not args.apply:
            return plan

        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        backup_dir = data_dir / "backups" / f"v1-single-user-{stamp}"
        backup_database(db_path, backup_dir / "studio.db")
        backup_dir.mkdir(parents=True, exist_ok=True)

        canonical = ensure_user(con, args.username)
        canonical_id = int(canonical["id"])
        con.commit()
        base = con.execute("SELECT id FROM users WHERE username = ?", (args.base_user,)).fetchone()
        base_user_id = int(base["id"]) if base else None

        history = {}
        for row in con.execute(
            "SELECT item_kind,item_id,pinned,updated_at FROM history_preferences "
            "ORDER BY updated_at"
        ):
            key = (row["item_kind"], row["item_id"])
            previous = history.get(key)
            history[key] = {
                "pinned": max(int(row["pinned"]), int(previous["pinned"]) if previous else 0),
                "updated_at": max(int(row["updated_at"]), int(previous["updated_at"]) if previous else 0),
            }

        con.execute("BEGIN IMMEDIATE")
        for table in OWNED_TABLES:
            con.execute(f"UPDATE {table} SET user_id = ? WHERE user_id <> ?", (canonical_id, canonical_id))
        con.execute("DELETE FROM history_preferences")
        con.executemany(
            "INSERT INTO history_preferences(user_id,item_kind,item_id,pinned,updated_at) "
            "VALUES(?,?,?,?,?)",
            [
                (canonical_id, kind, item_id, values["pinned"], values["updated_at"])
                for (kind, item_id), values in history.items()
            ],
        )
        con.execute("DELETE FROM sessions")
        con.execute(
            "UPDATE users SET is_active = CASE WHEN id = ? THEN 1 ELSE 0 END",
            (canonical_id,),
        )
        con.execute(
            "UPDATE users SET display_name = 'user', role = 'user', password_hash = 'disabled-v1' "
            "WHERE id = ?",
            (canonical_id,),
        )
        con.commit()

        owner_manifest = []
        canonical_owner = f"{canonical_id}:{args.username}"
        for meta_path in dynamic_files:
            try:
                payload = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            previous = str(payload.get("owner") or "")
            owner_manifest.append({"path": str(meta_path), "owner": previous})
            if previous != canonical_owner:
                payload["owner"] = canonical_owner
                temp = meta_path.with_suffix(".json.v1-tmp")
                temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                temp.replace(meta_path)
        (backup_dir / "dynamic-owners.json").write_text(
            json.dumps(owner_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        config_source = choose_config(data_dir, canonical_id, base_user_id)
        config_target = data_dir / "user_configs" / f"{canonical_id}.json"
        if config_source and config_source.resolve() != config_target.resolve():
            config_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(config_source, config_target)

        plan.update({
            "canonical_user_id": canonical_id,
            "canonical_owner": canonical_owner,
            "backup_dir": str(backup_dir),
            "service_config_source": str(config_source) if config_source else None,
            "service_config_target": str(config_target) if config_source else None,
            "history_after": len(history),
        })
        return plan
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    project = Path(__file__).resolve().parents[1]
    parser.add_argument("--db", type=Path, default=project / "studio/data/studio.db")
    parser.add_argument("--data-dir", type=Path, default=project / "studio/data")
    parser.add_argument("--username", default="user")
    parser.add_argument("--base-user", default="test")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(migrate(args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
