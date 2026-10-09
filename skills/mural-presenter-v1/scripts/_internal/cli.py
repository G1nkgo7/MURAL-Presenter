"""Role CLI 的唯一纯语法定义；解析和 --help 不加载领域实现。"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence


def _pages(value: str) -> list[int]:
    if not value.strip():
        raise argparse.ArgumentTypeError(
            "--slides cannot be empty; omit --slides for no consumers, or pass pages like 1,3"
        )
    parts = value.split(",")
    if not parts or any(not part.strip().isdigit() or int(part) < 1 for part in parts):
        raise argparse.ArgumentTypeError("--slides requires comma-separated positive page numbers")
    return sorted({int(part) for part in parts})


def build_parser(role: str, *, authoring_only: bool = False) -> argparse.ArgumentParser:
    """供脚本与 Harness 复用语法；创作模式隐藏后台 Deck 生命周期入口。"""
    if role not in {"orchestrator", "image", "slide"}:
        raise ValueError(f"unsupported script role: {role}")
    parser = argparse.ArgumentParser(description=f"MuralPresenter {role.title()} operations")
    sub = parser.add_subparsers(dest="command", required=True)
    if role == "orchestrator":
        commands = ("inspect-plans", "validate-plans")
        if not authoring_only:
            commands += ("scaffold", "sync-speech", "finalize")
        for command in commands:
            item = sub.add_parser(command)
            item.add_argument("root")
            item.add_argument("--expected", type=int)
            if command == "scaffold":
                item.add_argument("--force", action="store_true")
        if not authoring_only:
            audit = sub.add_parser("audit")
            audit.add_argument("root")
        revision_context = sub.add_parser("revision-context")
        revision_context.add_argument("root")
        revision = sub.add_parser("commit-revision")
        revision.add_argument("root")
        revision.add_argument("--revision-id", required=True)
        revision.add_argument("--expected-base-version", type=int)
        prepare = sub.add_parser("prepare-revision")
        prepare.add_argument("root")
        prepare.add_argument(
            "--operations", default="revision-operations.json", metavar="PATH",
            help="workspace-relative JSON FILE path (default: revision-operations.json); not inline JSON or @file",
        )
        prepare.add_argument("--revision-id", default="")
        for command in ("delete-page", "insert-page", "move-page"):
            item = sub.add_parser(command)
            item.add_argument("root")
            item.add_argument("--revision-id", required=True)
            item.add_argument("--operation-id", required=True)
            if command != "insert-page":
                item.add_argument("--uid", required=True)
            if command != "delete-page":
                anchor = item.add_mutually_exclusive_group(required=True)
                anchor.add_argument("--after", default="")
                anchor.add_argument("--before", default="")
            if command == "insert-page":
                item.add_argument("--new-uid", required=True)
    elif role == "image":
        record = sub.add_parser("register", help="Validate an explicit asset declaration; Harness commits the receipt")
        record.add_argument("root")
        record.add_argument("--id", required=True)
        record.add_argument("--path", required=True, help="Explicit raster path directly under assets/; available requires an existing image, pending may name the intended download path")
        record.add_argument("--kind", required=True, choices=("generated", "real", "material", "user"))
        record.add_argument("--status", choices=("available", "pending", "unavailable"), default="available")
        record.add_argument("--slides", type=_pages, default=[],
                            help="Explicit consumer page numbers, e.g. 1,3; omit this flag for no consumers")
        record.add_argument("--source", default="", help="Source URL or attachment reference")
        record.add_argument("--download", default="", help="Direct URL for a pending real image; fetch downloads it")
        for name in ("purpose", "crop", "display", "quality-intent", "subject-region", "text-safe-region", "notes"):
            record.add_argument("--" + name, default="", help="Placement guidance; quoted multiline text is preserved, not parsed")
        record.add_argument("--expect-transparent", action="store_true")
        record.add_argument("--reviewed", action="store_true", help="After viewing this exact image, confirm usability for --slides and publish an incremental handoff; not automatic visual verification")
        record.add_argument("--reason", default="", help="Required explanation for unavailable status")
        register = sub.add_parser("register-user")
        register.add_argument("root")
        register.add_argument("--source", required=True, help="Actual image file under inputs/, using its workspace-relative path from the attachment manifest; not the manifest JSON itself")
        register.add_argument("--output", required=True)
        crop = sub.add_parser("crop", aliases=["crop-material"], help="Crop an inputs/ or assets/ image using explicit normalized coordinates; the author judges visual usability")
        crop.add_argument("root")
        crop.add_argument("--source", required=True)
        crop.add_argument("--output", required=True)
        crop.add_argument("--box", required=True, help="Normalized x0,y0,x1,y1 in [0,1], e.g. 0.1,0.2,0.9,0.8; pixel coordinates are never inferred")
        crop.add_argument("--crop-policy", choices=("subject-only", "preserve-complete-figure", "document-facsimile"), default="preserve-complete-figure",
                          help="Author-selected crop intent; area and pixel dimensions do not determine image purpose or usability")
        crop.add_argument("--preserve-internal-labels", action="store_true")
        crop.add_argument("--auto-tighten", action="store_true")
        extract = sub.add_parser("extract-subject")
        extract.add_argument("root")
        extract.add_argument("--source", required=True)
        extract.add_argument("--output", required=True)
        extract.add_argument("--box", required=True, help="Normalized x0,y0,x1,y1 in [0,1]; pixel coordinates are never inferred")
        extract.add_argument("--exclude-box", action="append", default=[], help="从抠图 Alpha 中排除相邻正文；使用原页归一化坐标 x0,y0,x1,y1，可重复")
        fetch = sub.add_parser("fetch")
        fetch.add_argument("root")
        fetch.add_argument("--replace", action="store_true")
        fetch.add_argument("--asset", action="append", default=[], help="fetch only this registered asset id; repeat for multiple assets; --replace refreshes an existing file")
        finalize = sub.add_parser("finalize")
        finalize.add_argument("root")
        finalize.add_argument("--owner-id", default="", help="Harness supplies the current material author; only finalize that author's assets")
        inspect = sub.add_parser("inspect")
        inspect.add_argument("root")
        inspect.add_argument("--asset", required=True)
        inspect.add_argument("--expect-transparent", action="store_true")
        checker = sub.add_parser("remove-checkerboard")
        checker.add_argument("root")
        checker.add_argument("--asset", required=True)
    else:
        page = sub.add_parser("render")
        page.add_argument("root")
        page.add_argument("--page", type=int, required=True)
        page.add_argument("--expected", type=int)
        group = sub.add_parser("render-group")
        group.add_argument("root")
        group.add_argument("--group", required=True)
        group.add_argument("--pages", required=True)
        group.add_argument("--expected", type=int)
    return parser


def parse_args(role: str, argv: Sequence[str] | None = None) -> argparse.Namespace:
    return build_parser(role).parse_args(argv)


def run_expected(action: Callable[[], object]) -> int:
    """Run one deterministic action with concise contract-failure feedback.

    Missing inputs, malformed text and validation failures are normal Agent
    repair signals. Unexpected implementation/runtime exceptions intentionally
    propagate with a traceback so engineering failures are not disguised as a
    fixable deck contract.
    """
    from PIL import UnidentifiedImageError

    try:
        action()
    except (FileNotFoundError, UnicodeError, ValueError, UnidentifiedImageError) as exc:
        print("status:FAIL")
        print(str(exc))
        return 2
    return 0


__all__ = ["build_parser", "parse_args", "run_expected"]
