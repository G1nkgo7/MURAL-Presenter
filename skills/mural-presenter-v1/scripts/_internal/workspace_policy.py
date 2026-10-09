#!/usr/bin/env python3
"""Controlled temporary files, atomic promotion, and delivery-surface auditing."""
from __future__ import annotations

import os
import fcntl
import re
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DELIVERY_ROOTS = {
    "config.json",
    "knowledge.md",
    "research",
    "deck.md",
    "briefs",
    "slides",
    "assets",
    "renders",
    "checks",
    "delivery",
    "present.html",
    "speech.md",
    "base.css",
    "_trace",
}
# Runtime control-plane state is Harness-owned metadata. It is deliberately
# separate from ``DELIVERY_ROOTS`` because no model-facing artifact or exported
# deck should depend on it, but it must survive process restarts alongside the
# trace so owners, waves, leases, and checkpoints remain recoverable. The pinned
# Skill object lives outside the workspace (referenced via _state/skill-ref.json),
# so no workspace-local ``skills/`` directory exists under layout v2.
HARNESS_ROOTS = {"inputs", "result.json", "font-intent.json", "_state", "_runtime"}
# Agent-authored control input. It is intentionally outside ``_state`` so the
# model never needs access to Harness state, and outside the delivery surface.
AGENT_CONTROL_ROOTS = {"revision-operations.json"}
SCRATCH_SUFFIXES = (".new", ".tmp", ".bak", ".orig", ".rej")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
ISOLATED_RENDER_RE = re.compile(r"\.page_\d{2}$")
STALE_RENDER_PREVIEW_RE = re.compile(
    r"preview_slide_\d{2}\.[A-Za-z0-9_-]{6,}\.html$"
)


def _inside(root: Path, path: Path) -> bool:
    root_real = root.resolve()
    path_real = path.resolve()
    return path_real == root_real or root_real in path_real.parents


def _check_target(root: Path, path: Path) -> None:
    if not _inside(root, path):
        raise ValueError(f"path leaves workspace: {path}")


def _tmp_dir(root: Path) -> Path:
    path = root / "_runtime" / "tmp"
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def page_render_lock(root: Path, page: int) -> Iterator[int]:
    """The Harness may pass its held lock to the child; never unlock that borrow."""
    path = root.resolve() / "_state" / f"render-page-{int(page):02d}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    inherited = os.environ.get("MURAL_PAGE_RENDER_LOCK_FD")
    if inherited is not None:
        try:
            descriptor = int(inherited)
            actual, expected = os.fstat(descriptor), path.stat()
            if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                raise ValueError("inherited page render lock does not match this page")
        except (OSError, ValueError) as exc:
            raise ValueError("invalid inherited page render transaction lock") from exc
        # AFS locks are process-bound: the parent owns this borrow throughout capture.
        yield descriptor
        return
    deadline = time.monotonic() + 1800
    with path.open("a+b") as handle:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("page render transaction lock timed out")
                time.sleep(0.02)
        yield handle.fileno()


def _controlled_mkstemp(root: Path, **kwargs) -> tuple[int, str]:
    """Create a scratch file safely while concurrent writers clean empty tmp/."""
    for attempt in range(16):
        try:
            return tempfile.mkstemp(dir=_tmp_dir(root), **kwargs)
        except FileNotFoundError:
            if attempt == 15:
                raise


def _remove_empty_tmp(root: Path) -> None:
    """Keep the shared scratch directory stable throughout a child wave.

    The directory itself is Harness metadata and ``audit_workspace`` already
    permits it when empty.  Removing it after every atomic promotion creates a
    cross-process TOCTOU race for concurrent Slide/Image Agents.  Final deck
    audit still rejects any retained files inside it.
    """
    (root / "_runtime" / "tmp").mkdir(parents=True, exist_ok=True)


def atomic_write_text(root: Path, target: Path, content: str, *, overwrite: bool = True) -> bool:
    """Write in controlled ``tmp/`` and atomically promote with ``os.replace``."""
    root = root.resolve()
    if not overwrite and target.absolute() != target.resolve():
        raise ValueError(f"scaffold path must not traverse symlinks: {target}")
    target = target.resolve()
    _check_target(root, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = _controlled_mkstemp(
        root,
        prefix=f"{target.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            if not overwrite:
                os.fchmod(handle.fileno(), 0o644)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(temporary, target)
        else:
            # 原子创建，检查后出现的作者文件也不能被覆盖。
            try:
                os.link(temporary, target)
            except FileExistsError:
                if target.is_symlink() or not target.is_file():
                    raise ValueError(f"scaffold target is not a regular file: {target}")
                return False
        return True
    finally:
        temporary.unlink(missing_ok=True)
        _remove_empty_tmp(root)


def atomic_write_bytes(root: Path, target: Path, content: bytes) -> None:
    """Write bytes in controlled ``tmp/`` and atomically promote them."""
    root = root.resolve()
    target = target.resolve()
    _check_target(root, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = _controlled_mkstemp(
        root,
        prefix=f"{target.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
        _remove_empty_tmp(root)


def atomic_copy(root: Path, source: Path, target: Path) -> None:
    """Copy bytes to controlled ``tmp/`` and atomically promote the complete file."""
    root = root.resolve()
    source = source.resolve()
    target = target.resolve()
    _check_target(root, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = _controlled_mkstemp(
        root,
        prefix=f"{target.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
        _remove_empty_tmp(root)


@contextmanager
def temporary_text(
    root: Path,
    *,
    prefix: str,
    suffix: str,
    content: str,
) -> Iterator[Path]:
    """Create one controlled temporary text file and remove it in ``finally``."""
    root = root.resolve()
    descriptor, temporary_name = _controlled_mkstemp(
        root,
        prefix=prefix,
        suffix=suffix,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        yield temporary
    finally:
        temporary.unlink(missing_ok=True)
        _remove_empty_tmp(root)


def _is_scratch_name(name: str) -> bool:
    lowered = name.lower()
    return (
        lowered.startswith("base.css.")
        or lowered.endswith(SCRATCH_SUFFIXES)
        or lowered.endswith("~")
        or lowered.startswith(".preview_slide_")
    )


def _inside_isolated_render(relative: Path) -> bool:
    return (
        len(relative.parts) >= 2
        and relative.parts[0] == "renders"
        and bool(ISOLATED_RENDER_RE.fullmatch(relative.parts[1]))
    )


def audit_workspace(root: Path) -> list[str]:
    """Return delivery-policy violations without changing the workspace."""
    root = root.resolve()
    violations: list[str] = []
    if not root.is_dir():
        return [f"workspace does not exist: {root}"]

    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if (
            child.name in DELIVERY_ROOTS
            or child.name in HARNESS_ROOTS
            or child.name in AGENT_CONTROL_ROOTS
        ):
            continue
        violations.append(f"root output is outside the allowlist: {child.name}")

    # 普通 authoring 不再持有字体子集；tmp 中的任何文件都需要明确清理或归档。
    scratch = root / "_runtime" / "tmp"
    if scratch.is_dir():
        residue = sorted(
            str(path.relative_to(root))
            for path in scratch.rglob("*")
            if (path.is_file() or path.is_symlink())
        )
        if residue:
            violations.append(
                "controlled temporary directory retained files after the command: "
                + ", ".join(residue)
            )

    for top_name in sorted(DELIVERY_ROOTS - {"_trace"}):
        top = root / top_name
        if not top.is_dir():
            continue
        for path in top.rglob("*"):
            if not path.is_file() and not path.is_symlink():
                continue
            relative = path.relative_to(root)
            if _inside_isolated_render(relative):
                continue
            if _is_scratch_name(path.name):
                violations.append(f"scratch sidecar on delivery surface: {relative}")
            if (
                relative.parts[0] == "assets"
                and path.suffix.lower() == ".md"
                and path.name != "catalog.md"
                and path.name.startswith("catalog_")
            ):
                violations.append(f"unmerged asset catalog fragment: {relative}")
    return sorted(set(violations))


def assert_workspace_clean(root: Path) -> None:
    violations = audit_workspace(root)
    if violations:
        detail = "\n- ".join(violations)
        raise ValueError(f"workspace output audit failed:\n- {detail}")


def _referenced_asset_text(root: Path) -> str:
    candidates = [
        root / "base.css",
        root / "deck.md",
        root / "assets" / "catalog.md",
        *sorted((root / "briefs").glob("slide_[0-9][0-9].md")),
        *sorted((root / "slides").glob("slide_[0-9][0-9].html")),
    ]
    chunks: list[str] = []
    for path in candidates:
        if path.is_file():
            chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(chunks)


def _quarantine_target(root: Path, relative: Path) -> Path:
    quarantine = root / "_trace" / "noncanonical-artifacts"
    quarantine.mkdir(parents=True, exist_ok=True)
    flattened = "__".join(relative.parts)
    target = quarantine / flattened
    counter = 1
    while target.exists():
        target = quarantine / f"{flattened}.{counter}"
        counter += 1
    return target


def recover_stale_render_previews(root: Path) -> list[str]:
    """Quarantine only renderer-owned previews left by an interrupted process.

    A normal ``temporary_text`` context removes these files in ``finally``.
    Process termination can bypass that cleanup, while final delivery correctly
    rejects any remaining ``tmp/`` content.  Finalize runs after child waves
    have joined, so it can recover this exact renderer-owned filename family
    without granting model roles a general delete capability.
    """
    root = root.resolve()
    temporary = root / "_runtime" / "tmp"
    if not temporary.is_dir():
        return []
    moved: list[str] = []
    for source in sorted(temporary.iterdir(), key=lambda item: item.name):
        if (
            source.is_dir()
            or not STALE_RENDER_PREVIEW_RE.fullmatch(source.name)
        ):
            continue
        relative = source.relative_to(root)
        source.replace(_quarantine_target(root, relative))
        moved.append(relative.as_posix())
    _remove_empty_tmp(root)
    return moved


def clean_workspace(root: Path, *, unused_assets: bool = False) -> list[str]:
    """Quarantine known intermediates; never remove an artifact irrecoverably."""
    root = root.resolve()
    candidates: set[Path] = set()
    for path in root.iterdir():
        if path.is_file() and _is_scratch_name(path.name):
            candidates.add(path)
    for top_name in DELIVERY_ROOTS - {"_trace"}:
        top = root / top_name
        if not top.is_dir():
            continue
        for path in top.rglob("*"):
            if not path.is_file() and not path.is_symlink():
                continue
            relative = path.relative_to(root)
            if _inside_isolated_render(relative):
                continue
            if _is_scratch_name(path.name):
                candidates.add(path)
            if (
                relative.parts[0] == "assets"
                and path.suffix.lower() == ".md"
                and path.name != "catalog.md"
                and path.name.startswith("catalog_")
            ):
                candidates.add(path)

    for path in root.glob(".preview_slide_*.html"):
        candidates.add(path)

    if unused_assets:
        references = _referenced_asset_text(root)
        assets = root / "assets"
        if assets.is_dir():
            for path in assets.rglob("*"):
                if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
                    continue
                relative = path.relative_to(root).as_posix()
                if relative not in references and path.name not in references:
                    candidates.add(path)

    moved: list[str] = []
    for source in sorted(candidates):
        if not source.exists():
            continue
        relative = source.relative_to(root)
        source.replace(_quarantine_target(root, relative))
        moved.append(str(relative))

    moved.extend(recover_stale_render_previews(root))
    return moved
