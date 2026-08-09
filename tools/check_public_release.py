#!/usr/bin/env python3
"""Check the repository's public-facing, release-independent invariants."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IGNORED_PARTS = {
    ".git",
    ".next",
    ".pytest_cache",
    ".venv",
    ".vinext",
    ".wrangler",
    "__pycache__",
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
    ".ps1",
    ".py",
    ".sh",
    ".toml",
    ".ts",
    ".tsx",
    ".yml",
    ".yaml",
}
FORBIDDEN_PUBLIC_PATTERNS = {
    "private RFC1918 host": re.compile(
        r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"
    ),
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
            or name in {"Dockerfile", "start.bat", ".env.example"}
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


def tree_digest(root: Path, *, excluded: set[str] | None = None) -> tuple[int, str]:
    excluded = excluded or set()
    digest_state = hashlib.sha256()
    files = []
    for path in sorted(root.rglob("*")):
        if (
            not path.is_file()
            or any(part in IGNORED_PARTS for part in path.parts)
            or path.suffix == ".pyc"
        ):
            continue
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        data = path.read_bytes()
        digest_state.update(relative.encode("utf-8"))
        digest_state.update(b"\0")
        digest_state.update(hashlib.sha256(data).digest())
        digest_state.update(b"\0")
        files.append(relative)
    return len(files), digest_state.hexdigest()


def check_mural_snapshot() -> list[str]:
    errors: list[str] = []
    skill_root = ROOT / "skills/mural-presenter"
    harness_root = ROOT / "harnesses/mural-presenter"
    provenance_path = harness_root / "source-provenance.json"
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"MURAL snapshot provenance is unreadable: {exc}"]
    skill_count, skill_sha = tree_digest(skill_root)
    harness_count, harness_sha = tree_digest(
        harness_root, excluded={"source-provenance.json"}
    )
    expected = (
        ("skill_file_count", skill_count),
        ("skill_tree_sha256", skill_sha),
        ("harness_file_count", harness_count),
        ("harness_tree_sha256", harness_sha),
    )
    for key, actual in expected:
        if provenance.get(key) != actual:
            errors.append(
                f"MURAL snapshot provenance mismatch for {key}: "
                f"expected {provenance.get(key)!r}, found {actual!r}"
            )
    return errors


def check_repository_scaffold() -> list[str]:
    errors: list[str] = []
    required = (
        "docs/local-deployment.md",
        "docs/local-deployment_zh-CN.md",
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
        "apps/studio/sensenova_present/README.md",
        "apps/studio/sensenova_present/MIGRATION.md",
        "apps/studio/sensenova_present/studio/app/main.py",
        "apps/studio/sensenova_present/scripts/launch.py",
        "services/api/README.md",
        "scripts/README.md",
        "tests/README.md",
        "data/README.md",
        "artifacts/README.md",
        "skills/mural_authoring/README.md",
        "skills/mural-presenter/SKILL.md",
        "skills/mural-presenter/roles/slide.md",
        "skills/mural-presenter/scripts/deck.py",
        "harnesses/mural-presenter/distill_ppt.py",
        "harnesses/mural-presenter/core/nova_raw.py",
        "harnesses/mural-presenter/pyproject.toml",
        "harnesses/mural-presenter/source-provenance.json",
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
        *check_mural_snapshot(),
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
