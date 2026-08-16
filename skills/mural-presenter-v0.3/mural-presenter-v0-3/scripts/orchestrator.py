#!/usr/bin/env python3
"""Deterministic operations visible only to the Orchestrator Role."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.dont_write_bytecode = True
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _internal.delivery import audit, finalize  # noqa: E402
from _internal.planning import scaffold_from_plans, sync_speech, validate_plans  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="MuralPresenter Orchestrator operations")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("validate-plans", "scaffold", "sync-speech", "finalize"):
        item = sub.add_parser(command)
        item.add_argument("root")
        item.add_argument("--expected", type=int)
        if command == "scaffold":
            item.add_argument("--force", action="store_true")
    audit_parser = sub.add_parser("audit")
    audit_parser.add_argument("root")
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()
    if args.command == "validate-plans":
        validate_plans(root, args.expected)
    elif args.command == "scaffold":
        scaffold_from_plans(root, args.expected, force=args.force)
    elif args.command == "sync-speech":
        sync_speech(root, args.expected)
    elif args.command == "finalize":
        finalize(root, args.expected)
    else:
        audit(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

