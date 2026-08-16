#!/usr/bin/env python3
"""Create the canonical deck directories and copy immutable skill assets once."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


DIRS = (
    "materials",
    "research",
    "plan",
    "memory",
    "assets/by-id",
    "assets/pending",
    "slides",
    "renders",
    "checks",
    "reviews",
    "tmp/slide_backups",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace_root", type=Path)
    args = parser.parse_args()

    supplied = args.workspace_root.expanduser()
    if not supplied.is_absolute():
        parser.error("workspace_root must be absolute")
    root = supplied.resolve()
    root.mkdir(parents=True, exist_ok=True)
    for rel in DIRS:
        (root / rel).mkdir(parents=True, exist_ok=True)

    skill_root = Path(__file__).resolve().parents[1]
    template = skill_root / "references/designer/base-template.css"
    base_css = root / "base.css"
    if not template.is_file():
        raise SystemExit(f"missing base template: {template}")
    base_action = "preserved"
    if not base_css.exists():
        shutil.copy2(template, base_css)
        base_action = "copied"

    vendor_src = skill_root / "assets/vendor/echarts.min.js"
    vendor_dst = root / "assets/vendor/echarts.min.js"
    vendor_action = "absent"
    if vendor_src.is_file():
        vendor_dst.parent.mkdir(parents=True, exist_ok=True)
        if not vendor_dst.exists():
            shutil.copy2(vendor_src, vendor_dst)
            vendor_action = "copied"
        else:
            vendor_action = "preserved"

    print(json.dumps({
        "status": "ok",
        "workspace_root": str(root),
        "base_css": base_action,
        "vendor": vendor_action,
        "dirs": list(DIRS),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
