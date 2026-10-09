#!/usr/bin/env python3
"""Image 与 Orchestrator 共用的素材入口；执行范围由 Harness 按实际归属校验。"""

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
    args = parse_args("image")
    from _internal.assets import (
        fetch_images,
        finalize_assets,
        inspect_image,
        material_figure,
        print_image_report,
        register_image,
        register_user_image,
        remove_checkerboard,
    )

    root = Path(args.root).expanduser().resolve()
    input_error = ("invalid_arguments" if args.command == "fetch" and args.replace and not args.asset
                   else "unsupported_format" if args.command in {"inspect", "remove-checkerboard"} and Path(args.asset).suffix.lower() not in {".png", ".webp"} else "")
    if input_error:
        print("MURAL_DIAGNOSTIC " + json.dumps({"type": "image_command", "operation": args.command,
              "status": "failed", "error_kind": input_error, "asset": args.asset}, ensure_ascii=False), flush=True)
    if args.command == "register":
        return run_expected(lambda: register_image(root, args.id, args.path, status=args.status, reason=args.reason, reviewed=args.reviewed,
            **{name: getattr(args, name) for name in ("kind", "slides", "source", "download", "purpose", "crop",
                "display", "quality_intent", "subject_region", "text_safe_region", "notes", "expect_transparent")}))
    elif args.command == "register-user":
        return run_expected(
            lambda: register_user_image(root, args.source, args.output)
        )
    elif args.command in {"crop", "crop-material"}:
        return run_expected(
            lambda: material_figure(
                root,
                args.source,
                args.output,
                args.box,
                crop_policy=args.crop_policy,
                preserve_internal_labels=args.preserve_internal_labels,
                auto_tighten=args.auto_tighten,
            )
        )
    elif args.command == "extract-subject":
        return run_expected(
            lambda: material_figure(
                root,
                args.source,
                args.output,
                args.box,
                crop_policy="subject-only",
                auto_tighten=True,
                alpha_cutout=True,
                exclude_boxes=args.exclude_box,
            )
        )
    elif args.command == "fetch":
        return run_expected(
            lambda: fetch_images(
                root,
                replace=args.replace,
                asset_ids=args.asset,
            )
        )
    elif args.command == "finalize":
        return run_expected(lambda: finalize_assets(root, owner_id=args.owner_id))
    elif args.command == "inspect":
        return run_expected(
            lambda: print_image_report(
                inspect_image(root, args.asset, args.expect_transparent)
            )
        )
    else:
        return run_expected(
            lambda: print_image_report(remove_checkerboard(root, args.asset))
        )


if __name__ == "__main__":
    raise SystemExit(main())
