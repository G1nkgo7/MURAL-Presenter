#!/usr/bin/env python3
"""Deterministic operations visible only to Slide and Slide Group Roles."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.dont_write_bytecode = True
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _internal.renderer import render, render_group  # noqa: E402
from _internal.cli import run_expected  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="MuralPresenter Slide operations")
    sub = parser.add_subparsers(dest="command", required=True)
    page = sub.add_parser("render")
    page.add_argument("root")
    page.add_argument("--page", type=int, required=True)
    page.add_argument("--expected", type=int)
    group = sub.add_parser("render-group")
    group.add_argument("root")
    group.add_argument("--group", required=True)
    group.add_argument("--pages", required=True)
    group.add_argument("--expected", type=int)
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()
    if args.command == "render":
        return run_expected(lambda: render(root, args.page, args.expected))
    else:
        return run_expected(
            lambda: render_group(root, args.group, args.pages, args.expected)
        )


if __name__ == "__main__":
    raise SystemExit(main())
