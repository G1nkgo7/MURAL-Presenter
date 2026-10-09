#!/usr/bin/env python3
"""Deterministic operations visible only to Slide and Slide Group Roles."""

from __future__ import annotations

from pathlib import Path
import sys

sys.dont_write_bytecode = True
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _internal.cli import parse_args, run_expected  # noqa: E402


def main() -> int:
    args = parse_args("slide")
    from _internal.renderer import render, render_group

    root = Path(args.root).expanduser().resolve()
    if args.command == "render":
        return run_expected(lambda: render(root, args.page, args.expected))
    else:
        return run_expected(
            lambda: render_group(root, args.group, args.pages, args.expected)
        )


if __name__ == "__main__":
    raise SystemExit(main())
