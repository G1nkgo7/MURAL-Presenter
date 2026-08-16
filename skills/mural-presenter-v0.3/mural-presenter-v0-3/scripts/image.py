#!/usr/bin/env python3
"""Deterministic operations visible only to the Image Role."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.dont_write_bytecode = True
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _internal.assets import (  # noqa: E402
    fetch_images,
    finalize_assets,
    inspect_image,
    material_figure,
    print_image_report,
    register_user_image,
    remove_checkerboard,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="MuralPresenter Image operations")
    sub = parser.add_subparsers(dest="command", required=True)
    register = sub.add_parser("register-user")
    register.add_argument("root")
    register.add_argument("--source", required=True)
    register.add_argument("--output", required=True)
    crop = sub.add_parser("crop-material")
    crop.add_argument("root")
    crop.add_argument("--source", required=True)
    crop.add_argument("--output", required=True)
    crop.add_argument("--box", required=True)
    fetch = sub.add_parser("fetch")
    fetch.add_argument("root")
    fetch.add_argument("--replace", action="store_true")
    finalize_parser = sub.add_parser("finalize")
    finalize_parser.add_argument("root")
    inspect_parser = sub.add_parser("inspect")
    inspect_parser.add_argument("root")
    inspect_parser.add_argument("--asset", required=True)
    inspect_parser.add_argument("--expect-transparent", action="store_true")
    checker = sub.add_parser("remove-checkerboard")
    checker.add_argument("root")
    checker.add_argument("--asset", required=True)
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()
    if args.command == "register-user":
        register_user_image(root, args.source, args.output)
    elif args.command == "crop-material":
        material_figure(root, args.source, args.output, args.box)
    elif args.command == "fetch":
        fetch_images(root, replace=args.replace)
    elif args.command == "finalize":
        finalize_assets(root)
    elif args.command == "inspect":
        print_image_report(inspect_image(root, args.asset, args.expect_transparent))
    else:
        print_image_report(remove_checkerboard(root, args.asset))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

