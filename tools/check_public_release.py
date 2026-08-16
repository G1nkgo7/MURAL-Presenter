#!/usr/bin/env python3
"""Check the repository's public-facing, release-independent invariants."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
IGNORED_PARTS = {
    ".git",
    ".next",
    ".pytest_cache",
    ".venv",
    ".vinext",
    ".wrangler",
    "__pycache__",
    "CONCURRENCY",
    "dist",
    "node_modules",
    "runtime",
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
        relative = Path(directory).relative_to(ROOT)
        if relative == Path("webui/studio/data"):
            child_dirs[:] = []
            continue
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
            target = unquote(raw_target.strip().strip("<>").split("#", 1)[0])
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


def release_tree_digest(roots: tuple[Path, ...], *, relative_to: Path) -> str:
    """Match the sha256sum-of-sha256s digest stored in the frozen release manifest."""
    rows: list[tuple[str, Path]] = []
    for root in roots:
        for directory, child_dirs, child_files in os.walk(root):
            child_dirs[:] = [name for name in child_dirs if name not in IGNORED_PARTS]
            base = Path(directory)
            for name in child_files:
                path = base / name
                if name in IGNORED_PARTS or path.suffix == ".pyc":
                    continue
                relative = path.relative_to(relative_to).as_posix()
                if relative_to == root:
                    relative = f"./{relative}"
                rows.append((relative, path))
    state = hashlib.sha256()
    for relative, path in sorted(rows, key=lambda item: item[0].encode("utf-8")):
        state.update(f"{digest(path)}  {relative}\n".encode("utf-8"))
    return state.hexdigest()


def check_mural_snapshot() -> list[str]:
    errors: list[str] = []
    skill_root = ROOT / "skills/mural-presenter"
    harness_root = ROOT / "harnesses/mural-presenter"
    release_path = ROOT / "configs/releases/mural-paper-v1.json"
    try:
        release = json.loads(release_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"MURAL release manifest is unreadable: {exc}"]
    expected = (
        (
            "skill.tree_sha256",
            release.get("skill", {}).get("tree_sha256"),
            release_tree_digest((skill_root,), relative_to=skill_root),
        ),
        (
            "harness.tree_sha256",
            release.get("harness", {}).get("tree_sha256"),
            release_tree_digest((harness_root,), relative_to=harness_root),
        ),
        (
            "pair_tree_sha256",
            release.get("pair_tree_sha256"),
            release_tree_digest((skill_root, harness_root), relative_to=ROOT),
        ),
    )
    for key, declared, actual in expected:
        if declared != actual:
            errors.append(
                f"MURAL release manifest mismatch for {key}: "
                f"expected {declared!r}, found {actual!r}"
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
        "webui/README.md",
        "webui/README_EN.md",
        "webui/RELEASE_MANIFEST.json",
        "webui/studio/app/main.py",
        "webui/scripts/launch.py",
        "services/api/README.md",
        "scripts/README.md",
        "tests/README.md",
        "data/README.md",
        "artifacts/README.md",
        "skills/mural_authoring/README.md",
        "skills/mural-presenter/SKILL.md",
        "skills/mural-presenter/subagents/slide.md",
        "skills/mural-presenter/scripts/deck.py",
        "harnesses/mural-presenter/infer.py",
        "harnesses/mural-presenter/core/nova_bridge.py",
        "harnesses/mural-presenter/pyproject.toml",
        "configs/releases/mural-paper-v1.json",
        "benchmark/ThreadBench/README.md",
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
