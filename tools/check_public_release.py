#!/usr/bin/env python3
"""Check the repository's public-facing, release-independent invariants."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IGNORED_PARTS = {
    ".git",
    ".next",
    ".vinext",
    ".wrangler",
    "dist",
    "node_modules",
    "tmp",
}
TEXT_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".mjs",
    ".py",
    ".ts",
    ".tsx",
    ".yml",
    ".yaml",
}
FORBIDDEN_PUBLIC_PATTERNS = {
    "private RFC1918 host": re.compile(r"\b10\.210\."),
    "private AFS path": re.compile(r"/mnt/afs/"),
    "private workspace identity": re.compile(r"hejiatong", re.I),
    "retired temporary site": re.compile(r"pau1ownia7\.chatgpt\.site", re.I),
    "superseded method name": re.compile(
        r"\b(?:LH-Present(?:er)?|LongHorizon-Presenter|SensePresenter)\b",
        re.I,
    ),
}
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


def public_text_files() -> list[Path]:
    files: list[Path] = []
    for directory, child_dirs, child_files in os.walk(ROOT):
        child_dirs[:] = [name for name in child_dirs if name not in IGNORED_PARTS]
        base = Path(directory)
        files.extend(
            base / name
            for name in child_files
            if (base / name).suffix.lower() in TEXT_SUFFIXES
        )
    return sorted(files)


def check_private_references(files: list[Path]) -> list[str]:
    errors: list[str] = []
    for path in files:
        if path.resolve() == Path(__file__).resolve():
            continue
        text = path.read_text(encoding="utf-8")
        for label, pattern in FORBIDDEN_PUBLIC_PATTERNS.items():
            if pattern.search(text):
                errors.append(f"{path.relative_to(ROOT)} contains {label}")
    return errors


def check_markdown_links(files: list[Path]) -> tuple[int, list[str]]:
    checked = 0
    errors: list[str] = []
    for path in (item for item in files if item.suffix.lower() == ".md"):
        text = path.read_text(encoding="utf-8")
        for raw_target in MARKDOWN_LINK.findall(text):
            target = raw_target.strip().strip("<>").split("#", 1)[0]
            if not target or re.match(r"^(?:https?://|mailto:)", target):
                continue
            checked += 1
            resolved = (path.parent / target).resolve()
            if not resolved.exists():
                errors.append(
                    f"{path.relative_to(ROOT)} links to missing local target: {target}"
                )
    return checked, errors


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_mirrors() -> list[str]:
    errors: list[str] = []
    pairs = (
        ("assets/figures/execution-topologies.png", "site/public/execution-topologies.png"),
        ("assets/figures/authoring-lifecycle.png", "site/public/authoring-lifecycle.png"),
        ("assets/logo/exports/mural-social-card.png", "site/public/og.png"),
    )
    for source_name, mirror_name in pairs:
        source = ROOT / source_name
        mirror = ROOT / mirror_name
        if not source.is_file() or not mirror.is_file():
            errors.append(f"missing mirrored public asset: {source_name} / {mirror_name}")
        elif digest(source) != digest(mirror):
            errors.append(f"public asset mirror is stale: {mirror_name}")
    return errors


def check_brand_and_paper() -> list[str]:
    errors: list[str] = []
    expansion = (
        "Multi-Agent",
        "Unified",
        "Revision-Aware",
        "Authoring",
        "Long-Horizon Presentations",
    )
    for filename in ("README.md", "README_zh-CN.md"):
        text = (ROOT / filename).read_text(encoding="utf-8")
        for term in expansion:
            if term not in text:
                errors.append(f"{filename} is missing acronym term: {term}")
    for filename in (
        "site/public/mural-paper.pdf",
        "site/public/mural-paper-zh.pdf",
        "site/public/mural-paper-cover-en.png",
        "site/public/mural-paper-cover-zh.png",
    ):
        if not (ROOT / filename).is_file():
            errors.append(f"missing manuscript preview asset: {filename}")
    return errors


def check_repository_scaffold() -> list[str]:
    errors: list[str] = []
    required = (
        "docs/repository-layout.md",
        "docs/repository-layout_zh-CN.md",
        "src/mural_presenter/README.md",
        "src/mural_presenter/query_synthesis/README.md",
        "src/mural_presenter/data_pipeline/README.md",
        "src/mural_presenter/orchestration/README.md",
        "src/mural_presenter/inference/README.md",
        "src/mural_presenter/rendering/README.md",
        "src/mural_presenter/quality_control/README.md",
        "src/mural_presenter/schemas/README.md",
        "configs/README.md",
        "apps/studio/README.md",
        "services/api/README.md",
        "scripts/README.md",
        "tests/README.md",
        "data/README.md",
        "artifacts/README.md",
        "skills/mural_authoring/README.md",
        "benchmarks/thread_bench/README.md",
    )
    for filename in required:
        if not (ROOT / filename).is_file():
            errors.append(f"missing repository scaffold file: {filename}")
    return errors


def main() -> None:
    files = public_text_files()
    link_count, link_errors = check_markdown_links(files)
    errors = [
        *check_private_references(files),
        *link_errors,
        *check_mirrors(),
        *check_brand_and_paper(),
        *check_repository_scaffold(),
    ]
    if errors:
        raise SystemExit("Public release check failed:\n- " + "\n- ".join(errors))
    print(
        f"Public release check passed: {len(files)} text files, "
        f"{link_count} local Markdown links, 3 mirrored assets."
    )


if __name__ == "__main__":
    main()
