#!/usr/bin/env python3
"""Harness-only workspace bootstrap, invoked through the pinned Skill."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.dont_write_bytecode = True
_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _internal.deck_core import prepare, restore_base  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Harness-only MuralPresenter bootstrap")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "restore-base"):
        item = sub.add_parser(command)
        item.add_argument("root")
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()
    if args.command == "prepare":
        prepare(root)
    elif args.command == "restore-base":
        restore_base(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
