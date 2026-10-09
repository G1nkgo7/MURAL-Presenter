"""验证生产入口使用冻结的 MURAL v1 Skill、Harness 与 WebUI 集成。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Iterable


_IGNORED_PARTS = {
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
_TRUE_VALUES = {"1", "true", "yes", "on"}
_PRODUCTION_POLICIES = {"prod", "production"}
_DEVELOPMENT_POLICIES = {"dev", "development"}
_RUNTIME_MODES = {"inference", "synthesis", "webui"}
_SKILL_KEY = "mural-presenter-v1"
_HARNESS_KEY = "mural-presenter-harness-v1"


def _ignored_name(name: str) -> bool:
    return (
        name in _IGNORED_PARTS
        or name.startswith(".venv-")
        or name.startswith("_archived-")
    )


def _sha256(path: Path) -> str:
    state = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            state.update(chunk)
    return state.hexdigest()


def source_tree_files(root: Path) -> Iterable[Path]:
    """只在固定资源位置解引用同仓 Skill 源；发布对象与 release 摘要共用此清单。"""
    shared = {"scripts", "assets", "references/typography-routes.json", "references/font-substitutions.json"}
    for directory, child_dirs, child_files in os.walk(root, followlinks=True):
        child_dirs[:] = [name for name in child_dirs if not _ignored_name(name)]
        base = Path(directory)
        for name in [*child_dirs, *child_files]:
            path = base / name
            if _ignored_name(name) or path.suffix == ".pyc":
                continue
            if path.is_symlink():
                relative = path.relative_to(root)
                try:
                    target = path.resolve(strict=True).relative_to(root.parent.resolve())
                except (OSError, RuntimeError, ValueError) as exc:
                    raise RuntimeError(f"Skill shared source escapes its skills directory or is invalid: {path}") from exc
                if (root.parent.name != "skills" or relative.as_posix() not in shared
                        or Path(os.readlink(path)).is_absolute() or len(target.parts) < 2
                        or target.parts[0] == root.name or target.parts[1:] != relative.parts):
                    raise RuntimeError(f"unsupported Skill source symlink: {path}")
            if path.is_file():
                yield path


def release_tree_digest(roots: Iterable[Path], *, relative_to: Path) -> str:
    """计算 release manifest 使用的 sha256sum-of-sha256s 树摘要。"""
    rows: list[tuple[str, Path]] = []
    for root in roots:
        for path in source_tree_files(root):
            relative = path.relative_to(relative_to).as_posix()
            if relative_to == root:
                relative = f"./{relative}"
            rows.append((relative, path))
    state = hashlib.sha256()
    for relative, path in sorted(rows, key=lambda item: item[0].encode("utf-8")):
        state.update(f"{_sha256(path)}  {relative}\n".encode("utf-8"))
    return state.hexdigest()


def integration_digest(repository_root: Path, files: Iterable[str]) -> str:
    """计算 manifest 明确列出的 WebUI 集成面摘要。"""
    rows: list[tuple[str, Path]] = []
    for raw in files:
        relative = Path(str(raw))
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError(f"release integration path is not repository-relative: {raw}")
        path = (repository_root / relative).resolve()
        try:
            path.relative_to(repository_root.resolve())
        except ValueError as exc:
            raise RuntimeError(f"release integration path escapes repository: {raw}") from exc
        if not path.is_file():
            raise RuntimeError(f"release integration file is missing: {raw}")
        rows.append((relative.as_posix(), path))
    state = hashlib.sha256()
    for relative, path in sorted(rows, key=lambda item: item[0].encode("utf-8")):
        state.update(f"{_sha256(path)}  {relative}\n".encode("utf-8"))
    return state.hexdigest()


def _repository_root(start: Path | None = None) -> Path:
    origin = (start or Path(__file__)).resolve()
    for candidate in [origin, *origin.parents]:
        root = candidate if candidate.is_dir() else candidate.parent
        if (root / "configs/releases/mural-paper-v1.json").is_file():
            return root
    raise RuntimeError("cannot locate configs/releases/mural-paper-v1.json")


def _manifest_path(repository_root: Path, explicit: str | Path | None) -> Path:
    raw = explicit or os.environ.get("MURAL_RELEASE_MANIFEST")
    if raw is None or not str(raw).strip():
        return repository_root / "configs/releases/mural-paper-v1.json"
    candidate = Path(raw).expanduser()
    path = candidate if candidate.is_absolute() else repository_root / candidate
    path = path.resolve()
    try:
        path.relative_to(repository_root.resolve())
    except ValueError as exc:
        raise RuntimeError("MURAL release manifest escapes repository") from exc
    if not path.is_file():
        raise RuntimeError(f"MURAL release manifest is missing: {path}")
    return path


def _bounded_root(repository_root: Path, raw: object, label: str) -> Path:
    relative = Path(str(raw or ""))
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError(f"release {label} path must be repository-relative")
    path = (repository_root / relative).resolve()
    try:
        path.relative_to(repository_root.resolve())
    except ValueError as exc:
        raise RuntimeError(f"release {label} path escapes repository") from exc
    if not path.is_dir():
        raise RuntimeError(f"release {label} path is missing: {relative.as_posix()}")
    return path


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def verify_runtime_release(
    run_mode: str,
    *,
    repository_root: str | Path | None = None,
    manifest_path: str | Path | None = None,
) -> dict[str, object]:
    """生产策略下对源码漂移 fail-closed；开发策略须显式允许 dirty。"""
    mode = str(run_mode or "").strip().lower()
    if mode not in _RUNTIME_MODES:
        raise RuntimeError(f"unsupported MURAL runtime mode: {run_mode!r}")
    root = (
        Path(repository_root).expanduser().resolve()
        if repository_root is not None
        else _repository_root()
    )
    selected_manifest = _manifest_path(root, manifest_path)
    try:
        release = json.loads(selected_manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot read frozen MURAL release manifest: {exc}") from exc

    if release.get("status") != "frozen":
        raise RuntimeError("MURAL release manifest is not frozen")
    skill = release.get("skill") or {}
    harness = release.get("harness") or {}
    if skill.get("key") != _SKILL_KEY:
        raise RuntimeError(f"MURAL release does not select {_SKILL_KEY}")
    if harness.get("key") != _HARNESS_KEY:
        raise RuntimeError(f"MURAL release does not select {_HARNESS_KEY}")

    declared_modes = set((release.get("lineage") or {}).get("runtime_modes") or [])
    required_modes = {mode} if mode != "webui" else {"inference", "synthesis"}
    if not required_modes.issubset(declared_modes):
        raise RuntimeError(
            "MURAL release does not authorize runtime mode(s): "
            + ", ".join(sorted(required_modes - declared_modes))
        )

    skill_root = _bounded_root(root, skill.get("path"), "Skill")
    harness_root = _bounded_root(root, harness.get("path"), "Harness")
    integration = release.get("integration") or {}
    integration_files = integration.get("files") or []
    if not isinstance(integration_files, list) or not integration_files:
        raise RuntimeError("MURAL release is missing its WebUI integration file set")

    actual = {
        "skill": release_tree_digest((skill_root,), relative_to=skill_root),
        "harness": release_tree_digest((harness_root,), relative_to=harness_root),
        "pair": release_tree_digest((skill_root, harness_root), relative_to=root),
        "integration": integration_digest(root, integration_files),
    }
    expected = {
        "skill": str(skill.get("tree_sha256") or ""),
        "harness": str(harness.get("tree_sha256") or ""),
        "pair": str(release.get("pair_tree_sha256") or ""),
        "integration": str(integration.get("tree_sha256") or ""),
    }
    mismatches = {
        key: {"expected": expected[key], "actual": actual[key]}
        for key in expected
        if expected[key] != actual[key]
    }
    policy = os.environ.get("MURAL_RELEASE_POLICY", "production").strip().lower()
    if policy not in _PRODUCTION_POLICIES | _DEVELOPMENT_POLICIES:
        raise RuntimeError(f"unsupported MURAL_RELEASE_POLICY: {policy!r}")
    dirty_override = policy in _DEVELOPMENT_POLICIES and _enabled(
        "MURAL_ALLOW_DIRTY_RELEASE"
    )
    if mismatches and not dirty_override:
        details = "; ".join(
            f"{key} expected={value['expected']} actual={value['actual']}"
            for key, value in mismatches.items()
        )
        if policy in _DEVELOPMENT_POLICIES:
            details += (
                "; set MURAL_ALLOW_DIRTY_RELEASE=1 only for an intentional "
                "development run"
            )
        raise RuntimeError(f"frozen MURAL release hash mismatch: {details}")

    return {
        "release": str(release.get("release") or ""),
        "manifest": selected_manifest.relative_to(root).as_posix(),
        "source_commit": str((release.get("git") or {}).get("source_commit") or ""),
        "run_mode": mode,
        "policy": policy,
        "dirty_override": bool(mismatches and dirty_override),
        "expected": expected,
        "actual": actual,
        "mismatches": mismatches,
    }
