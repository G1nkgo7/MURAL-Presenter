#!/usr/bin/env python3
"""Deterministic operations visible only to the Orchestrator Role."""

from __future__ import annotations

import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _internal.cli import parse_args, run_expected  # noqa: E402


def main() -> int:
    args = parse_args("orchestrator")
    from _internal.delivery import audit, finalize
    from _internal.planning import (
        inspect_plans,
        scaffold_from_plans,
        sync_speech,
        validate_plans,
    )
    from _internal.deck_state import commit_deck_revision
    from _internal.revision_router import compile_revision_impact, revision_context
    from _internal.revision_structure import (
        delete_page,
        insert_page,
        move_page,
    )

    root = Path(args.root).expanduser().resolve()
    if args.command == "inspect-plans":
        return run_expected(lambda: inspect_plans(root, args.expected))
    elif args.command == "validate-plans":
        return run_expected(lambda: validate_plans(root, args.expected))
    elif args.command == "scaffold":
        return run_expected(
            lambda: scaffold_from_plans(root, args.expected, force=args.force)
        )
    elif args.command == "sync-speech":
        return run_expected(lambda: sync_speech(root, args.expected))
    elif args.command == "finalize":
        return run_expected(lambda: finalize(root, args.expected))
    elif args.command == "commit-revision":
        return run_expected(
            lambda: print(
                json.dumps(
                    commit_deck_revision(
                        root,
                        args.revision_id,
                        expected_base_version=args.expected_base_version,
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        )
    elif args.command == "revision-context":
        return run_expected(
            lambda: print(
                json.dumps(
                    revision_context(root),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        )
    elif args.command == "prepare-revision":
        def prepare_revision() -> None:
            compile_revision_impact(
                root,
                Path(args.operations),
                revision_id=args.revision_id,
            )
            print(json.dumps(revision_context(root), ensure_ascii=False, sort_keys=True))

        return run_expected(prepare_revision)
    elif args.command == "delete-page":
        return run_expected(
            lambda: print(
                json.dumps(
                    delete_page(
                        root,
                        revision_id=args.revision_id,
                        operation_id=args.operation_id,
                        page_uid=args.uid,
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        )
    elif args.command == "insert-page":
        return run_expected(
            lambda: print(
                json.dumps(
                    insert_page(
                        root,
                        revision_id=args.revision_id,
                        operation_id=args.operation_id,
                        new_uid=args.new_uid,
                        after_uid=args.after,
                        before_uid=args.before,
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        )
    elif args.command == "move-page":
        return run_expected(
            lambda: print(
                json.dumps(
                    move_page(
                        root,
                        revision_id=args.revision_id,
                        operation_id=args.operation_id,
                        page_uid=args.uid,
                        after_uid=args.after,
                        before_uid=args.before,
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        )
    else:
        return run_expected(lambda: audit(root))


if __name__ == "__main__":
    raise SystemExit(main())
