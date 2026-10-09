#!/usr/bin/env python3
"""Check the repository's public-facing, release-independent invariants."""

from __future__ import annotations

import hashlib
import json
import os
import re
import runpy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IGNORED_PARTS = {
    ".git",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".vinext",
    ".wrangler",
    "__pycache__",
    "CONCURRENCY",
    "canary",
    "dist",
    "node_modules",
    "runtime",
    "runs",
    "tmp",
    "archive",
}
V1_RELEASE_IGNORED_PARTS = {
    ".git",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".vinext",
    ".wrangler",
    "__pycache__",
    "artifacts",
    "canary",
    "dist",
    "logs",
    "node_modules",
    "outputs",
    "runs",
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


def ignored_directory_name(name: str) -> bool:
    """Exclude generated environments and archived worktrees by convention."""
    return (
        name in IGNORED_PARTS
        or name.startswith(".venv-")
        or name.startswith("_archived-")
    )


def public_text_files() -> list[Path]:
    files: list[Path] = []
    for directory, child_dirs, child_files in os.walk(ROOT):
        relative = Path(directory).relative_to(ROOT)
        if relative == Path("webui/studio/data"):
            child_dirs[:] = []
            continue
        child_dirs[:] = [
            name for name in child_dirs if not ignored_directory_name(name)
        ]
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
        # Private-reference patterns are ASCII. Replacement decoding preserves
        # that signal while keeping one valid non-UTF-8 source file in an
        # imported tree from crashing the entire release preflight.
        text = path.read_text(encoding="utf-8", errors="replace")
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
            or any(ignored_directory_name(part) for part in path.parts)
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


def release_tree_digest(
    roots: tuple[Path, ...],
    *,
    relative_to: Path,
    ignored_parts: set[str] | None = None,
) -> str:
    """Match the sha256sum-of-sha256s digest stored in the frozen release manifest."""
    if ignored_parts == V1_RELEASE_IGNORED_PARTS:
        guard = runpy.run_path(str(ROOT / "harnesses/mural-presenter-v1/runtime/release_guard.py"))
        return guard["release_tree_digest"](roots, relative_to=relative_to)
    selected_ignored_parts = IGNORED_PARTS if ignored_parts is None else ignored_parts

    def ignored_release_name(name: str) -> bool:
        return (
            name in selected_ignored_parts
            or name.startswith(".venv-")
            or name.startswith("_archived-")
        )

    rows: list[tuple[str, Path]] = []
    for root in roots:
        for directory, child_dirs, child_files in os.walk(root):
            child_dirs[:] = [
                name for name in child_dirs if not ignored_release_name(name)
            ]
            base = Path(directory)
            for name in child_files:
                path = base / name
                if ignored_release_name(name) or path.suffix == ".pyc":
                    continue
                relative = path.relative_to(relative_to).as_posix()
                if relative_to == root:
                    relative = f"./{relative}"
                rows.append((relative, path))
    state = hashlib.sha256()
    for relative, path in sorted(rows, key=lambda item: item[0].encode("utf-8")):
        state.update(f"{digest(path)}  {relative}\n".encode("utf-8"))
    return state.hexdigest()


def release_file_digest(files: tuple[Path, ...], *, relative_to: Path) -> str:
    """Hash one explicit integration file set with the release digest format."""
    state = hashlib.sha256()
    rows = sorted(
        ((path.relative_to(relative_to).as_posix(), path) for path in files),
        key=lambda item: item[0].encode("utf-8"),
    )
    for relative, path in rows:
        state.update(f"{digest(path)}  {relative}\n".encode("utf-8"))
    return state.hexdigest()


def check_mural_snapshot(release_path: str | Path | None = None) -> list[str]:
    errors: list[str] = []
    release_path = Path(release_path) if release_path else ROOT / "configs/releases/mural-paper-v1.json"
    if not release_path.is_absolute():
        release_path = ROOT / release_path
    release_path = release_path.resolve()
    try:
        release_path.relative_to(ROOT.resolve())
    except ValueError:
        return [f"MURAL release manifest must stay inside the repository: {release_path}"]
    try:
        release = json.loads(release_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"MURAL release manifest is unreadable: {exc}"]
    try:
        skill_root = (ROOT / str(release["skill"]["path"])).resolve()
        harness_root = (ROOT / str(release["harness"]["path"])).resolve()
        skill_root.relative_to(ROOT.resolve())
        harness_root.relative_to(ROOT.resolve())
    except (KeyError, TypeError, ValueError) as exc:
        return [f"MURAL release manifest has invalid Skill/Harness paths: {exc}"]
    if not skill_root.is_dir() or not harness_root.is_dir():
        return [
            "MURAL release manifest points to a missing Skill/Harness pair: "
            f"skill={skill_root}, harness={harness_root}"
        ]
    release_ignored_parts = (
        V1_RELEASE_IGNORED_PARTS
        if release.get("harness", {}).get("key") == "mural-presenter-harness-v1"
        else None
    )
    expected = (
        (
            "skill.tree_sha256",
            release.get("skill", {}).get("tree_sha256"),
            release_tree_digest(
                (skill_root,),
                relative_to=skill_root,
                ignored_parts=release_ignored_parts,
            ),
        ),
        (
            "harness.tree_sha256",
            release.get("harness", {}).get("tree_sha256"),
            release_tree_digest(
                (harness_root,),
                relative_to=harness_root,
                ignored_parts=release_ignored_parts,
            ),
        ),
        (
            "pair_tree_sha256",
            release.get("pair_tree_sha256"),
            release_tree_digest(
                (skill_root, harness_root),
                relative_to=ROOT,
                ignored_parts=release_ignored_parts,
            ),
        ),
    )
    for key, declared, actual in expected:
        if declared != actual:
            errors.append(
                f"MURAL release manifest mismatch for {key}: "
                f"expected {declared!r}, found {actual!r}"
            )

    git_record = release.get("git") or {}
    source_commit = str(git_record.get("source_commit") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        errors.append("MURAL release manifest is missing a full git.source_commit")

    integration = release.get("integration") or {}
    integration_paths = integration.get("files") or []
    integration_files: list[Path] = []
    if not isinstance(integration_paths, list) or not integration_paths:
        errors.append("MURAL release manifest is missing integration.files")
    else:
        for raw in integration_paths:
            relative = Path(str(raw))
            if relative.is_absolute() or ".." in relative.parts:
                errors.append(
                    f"MURAL integration path must be repository-relative: {raw!r}"
                )
                continue
            path = (ROOT / relative).resolve()
            try:
                path.relative_to(ROOT.resolve())
            except ValueError:
                errors.append(f"MURAL integration path escapes repository: {raw!r}")
                continue
            if not path.is_file():
                errors.append(f"MURAL integration file is missing: {raw!r}")
                continue
            integration_files.append(path)
    if integration_files and len(integration_files) == len(integration_paths):
        actual_integration = release_file_digest(
            tuple(integration_files), relative_to=ROOT
        )
        declared_integration = integration.get("tree_sha256")
        if declared_integration != actual_integration:
            errors.append(
                "MURAL release manifest mismatch for integration.tree_sha256: "
                f"expected {declared_integration!r}, found {actual_integration!r}"
            )

    method_profiles = release.get("method_profiles")
    if isinstance(method_profiles, dict):
        resolved_profile_roots: dict[str, Path] = {}
        for profile_name in ("full", "woskill"):
            profile = method_profiles.get(profile_name)
            if not isinstance(profile, dict):
                errors.append(
                    f"MURAL release manifest is missing method_profiles.{profile_name}"
                )
                continue
            skill = profile.get("skill")
            if not isinstance(skill, dict):
                errors.append(
                    f"MURAL release manifest has invalid method_profiles.{profile_name}.skill"
                )
                continue
            try:
                profile_root = (ROOT / str(skill["path"])).resolve()
                profile_root.relative_to(ROOT.resolve())
            except (KeyError, TypeError, ValueError) as exc:
                errors.append(
                    f"MURAL {profile_name} profile has invalid Skill path: {exc}"
                )
                continue
            if not profile_root.is_dir():
                errors.append(
                    f"MURAL {profile_name} profile Skill is missing: {profile_root}"
                )
                continue
            resolved_profile_roots[profile_name] = profile_root
            declared_digest = skill.get("tree_sha256")
            actual_digest = release_tree_digest(
                (profile_root,), relative_to=profile_root
            )
            if declared_digest != actual_digest:
                errors.append(
                    "MURAL release manifest mismatch for "
                    f"method_profiles.{profile_name}.skill.tree_sha256: "
                    f"expected {declared_digest!r}, found {actual_digest!r}"
                )

        # The paper w/o-Skill arm may remove guidance only.  Mechanical scripts
        # and frozen runtime assets must remain byte-identical so the ablation
        # cannot accidentally compare two Harness/renderer implementations.
        full_root = resolved_profile_roots.get("full")
        woskill_root = resolved_profile_roots.get("woskill")
        if full_root is not None and woskill_root is not None:
            for relative in ("scripts", "assets"):
                full_surface = tree_digest(full_root / relative)
                woskill_surface = tree_digest(woskill_root / relative)
                if full_surface != woskill_surface:
                    errors.append(
                        "MURAL paper ablation mechanical surface differs for "
                        f"{relative}: full={full_surface}, woskill={woskill_surface}"
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
        "skills/mural-presenter-v1/SKILL.md",
        "skills/mural-presenter-v1/references/style-index.md",
        "skills/mural-presenter-v1/references/planning/plan-contract.md",
        "skills/mural-presenter-v1/scripts/_internal/deck_plan.py",
        "skills/mural-presenter-v1/roles/slide.md",
        "skills/mural-presenter-v1/scripts/orchestrator.py",
        "harnesses/mural-presenter-v1/infer.py",
        "harnesses/mural-presenter-v1/runtime/agent.py",
        "harnesses/mural-presenter-v1/runtime/production.py",
        "archive/harnesses-20260905/README.md",
        "archive/mural-presenter-pre-v0.6/README.md",
        "configs/releases/mural-paper-v1.json",
        "harnesses/mural-presenter-v1/tests/test_layout_v2_contract.py",
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
