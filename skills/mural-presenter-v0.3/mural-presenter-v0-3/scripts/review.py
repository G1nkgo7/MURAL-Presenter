#!/usr/bin/env python3
"""Deterministic operations visible only to the Review Role."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.dont_write_bytecode = True
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _internal.delivery import finalize, sync_speech  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="MuralPresenter Review operations")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("sync-speech", "finalize"):
        item = sub.add_parser(command)
        item.add_argument("root")
        item.add_argument("--expected", type=int)
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()
    if args.command == "sync-speech":
        sync_speech(root, args.expected)
    else:
        finalize(root, args.expected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
