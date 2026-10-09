"""定义 MURAL workspace 的唯一布局版本与路径解析合同。

新 v1 只支持 ``layout_version: 2``。旧 workspace 由冻结的 mural-presenter-v1.0
处理，本模块不识别、不推断、不读取、不写入任何 legacy 布局，也不提供 ``plan/``、
根 ``tmp/``、``materials/``、workspace-local ``skills/`` 或
``assets/fonts/`` 的别名。
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Mapping


WORKSPACE_LAYOUT_VERSION = 2
WORKSPACE_PATHS_SCHEMA = "mural.workspace-paths.v2"

# 出现即代表这是 legacy 或被禁布局；纯 v2 workspace 一律拒绝这些目录/前缀。
_FORBIDDEN_MARKERS = (
    "plan",
    "skills",
    "tmp",
    "materials",
    "assets/fonts",
    "_runtime/tmp/compat",
)


class WorkspacePathError(RuntimeError):
    """表示 workspace 布局或路径别名不满足纯 layout-v2 合同。"""


def _v2_paths() -> dict[str, str]:
    """返回唯一 canonical layout-v2 路径映射。"""
    return {
        "deck": "deck.md",
        "creation_config": "config.json",
        "knowledge": "knowledge.md",
        "speech": "speech.md",
        "base_css": "base.css",
        "present": "present.html",
        "result": "result.json",
        "assets": "assets",
        "asset_catalog": "assets/catalog.md",
        "slides": "slides",
        "slide_pattern": "slides/slide_{page:02d}.html",
        "renders": "renders",
        "render_pattern": "renders/slide_{page:02d}.png",
        "inputs": "inputs",
        "delivery": "delivery",
        "delivery_manifest": "delivery/manifest.json",
        "portable_zip": "delivery/presentation.zip",
        "state": "_state",
        "trace": "_trace",
        "runtime": "_runtime",
        "runtime_tmp": "_runtime/tmp",
        "edit_operations": "_state/edit-operations.json",
        "revision_impact": "_state/revision-impact.md",
        "skill_ref": "_state/skill-ref.json",
        "font_manifest": "_state/font-manifest.json",
        "font_request": "_state/font-request.json",
        "workspace_manifest": "_state/workspace.json",
    }


def _safe_relative(value: str) -> str:
    """把工作区相对路径规范化为 POSIX，并拒绝逃逸与空段。"""
    normalized = str(value or "").replace("\\", "/").strip()
    candidate = PurePosixPath(normalized)
    if (
        not normalized
        or candidate.is_absolute()
        or not candidate.parts
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise WorkspacePathError(f"invalid workspace-relative path: {value!r}")
    return candidate.as_posix()


def _forbidden_markers(root: Path) -> list[str]:
    """列出会让 workspace 违反纯 v2 合同的 legacy/禁用目录。"""
    return [marker for marker in _FORBIDDEN_MARKERS
            if (root / marker).exists() or (root / marker).is_symlink()]


def _manifest_layout_version(root: Path) -> int | None:
    """读取已有 workspace manifest 的 layout_version；缺失返回 None。

    纯 v2 合同：缺失的 layout_version 绝不当作 legacy(1)；它只表示尚未初始化。
    """
    state_path = root / "_state" / "workspace.json"
    if not state_path.is_file():
        return None
    try:
        value = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspacePathError(f"invalid workspace state {state_path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkspacePathError(f"workspace state must contain one object: {state_path}")
    raw = value.get("layout_version")
    if raw is None:
        raise WorkspacePathError(
            f"workspace state has no layout_version: {state_path}"
        )
    if type(raw) is not int:
        raise WorkspacePathError("unsupported_workspace_layout: layout_version must be an integer")
    version = raw
    if version != WORKSPACE_LAYOUT_VERSION:
        raise WorkspacePathError(
            f"unsupported workspace layout_version: {version}; new v1 only serves "
            f"layout_version {WORKSPACE_LAYOUT_VERSION}"
        )
    if value.get("paths_schema") != WORKSPACE_PATHS_SCHEMA:
        raise WorkspacePathError("unsupported_workspace_layout: invalid paths_schema")
    mapping = value.get("paths")
    if not isinstance(mapping, dict) or any(mapping.get(key) != relative for key, relative in _v2_paths().items()):
        raise WorkspacePathError("unsupported_workspace_layout: path mapping differs from canonical v2")
    for relative in mapping.values():
        relative = _safe_relative(relative)
        if any(relative == marker or relative.startswith(marker + "/") for marker in _FORBIDDEN_MARKERS):
            raise WorkspacePathError("unsupported_workspace_layout: forbidden path mapping")
        target = root / relative.format(page=1)
        if not target.resolve().is_relative_to(root):
            raise WorkspacePathError("unsupported_workspace_layout: path mapping escapes workspace")
    return version


def _is_empty_enough_for_v2(root: Path) -> bool:
    """仅允许空目录初始化；已有运行数据必须拥有 manifest。"""
    return not root.exists() or (root.is_dir() and not any(root.iterdir()))


@dataclass(frozen=True)
class WorkspacePaths:
    """唯一 layout-v2 路径解析器；拒绝一切 legacy 布局与逻辑别名。"""

    root: Path
    layout_version: int
    paths: Mapping[str, str]

    @classmethod
    def from_root(cls, workspace: str | Path) -> "WorkspacePaths":
        """从 workspace 根目录构造纯 v2 解析器。

        - 有 manifest：必须是 layout 2，否则拒绝。
        - 无 manifest 且目录为空：作为新 v2 workspace。
        - 无 manifest 但目录非空：``unsupported_workspace_layout``。
        - 出现任何 legacy/禁用目录：拒绝。
        """
        root = Path(workspace).expanduser().resolve()
        if any((root / name).is_symlink() for name in ("_state", "_trace", "_state/workspace.json")):
            raise WorkspacePathError("unsupported_workspace_layout: private state/trace paths must not be symlinks")
        forbidden = _forbidden_markers(root)
        if forbidden:
            raise WorkspacePathError(
                "unsupported_workspace_layout: workspace contains forbidden "
                f"legacy paths: {forbidden}"
            )
        declared = _manifest_layout_version(root)
        if declared is None and not _is_empty_enough_for_v2(root):
            raise WorkspacePathError(
                "unsupported_workspace_layout: non-empty workspace has no "
                "_state/workspace.json manifest"
            )
        return cls(
            root=root,
            layout_version=WORKSPACE_LAYOUT_VERSION,
            paths=MappingProxyType(_v2_paths()),
        )

    def path(self, key: str, *, page: int | None = None) -> Path:
        """按 canonical 键返回工作区内绝对路径。"""
        try:
            relative = self.paths[str(key)]
        except KeyError as exc:
            raise WorkspacePathError(f"unknown workspace path key: {key}") from exc
        if "{page" in relative:
            if page is None or isinstance(page, bool) or int(page) < 1:
                raise WorkspacePathError(f"path {key!r} requires a positive page")
            relative = relative.format(page=int(page))
        return self.root / _safe_relative(relative)

    @property
    def deck(self) -> Path:
        return self.path("deck")

    @property
    def knowledge(self) -> Path:
        return self.path("knowledge")

    @property
    def slides(self) -> Path:
        return self.path("slides")

    @property
    def renders(self) -> Path:
        return self.path("renders")

    @property
    def assets(self) -> Path:
        return self.path("assets")

    @property
    def inputs(self) -> Path:
        return self.path("inputs")

    @property
    def state(self) -> Path:
        return self.path("state")

    @property
    def trace(self) -> Path:
        return self.path("trace")

    @property
    def delivery(self) -> Path:
        return self.path("delivery")

    @property
    def runtime(self) -> Path:
        return self.path("runtime")

    @property
    def runtime_tmp(self) -> Path:
        return self.path("runtime_tmp")

    @property
    def runtime_fonts(self) -> Path:
        return self.runtime_tmp / "fonts"

    @property
    def skill_ref(self) -> Path:
        return self.path("skill_ref")

    @property
    def font_manifest(self) -> Path:
        return self.path("font_manifest")

    def page_html(self, page: int) -> Path:
        return self.path("slide_pattern", page=page)

    def page_png(self, page: int) -> Path:
        return self.path("render_pattern", page=page)

    def initialize_directories(self) -> None:
        """创建 canonical v2 目录；绝不创建 legacy 目录。"""
        keys = (
            "slides",
            "renders",
            "assets",
            "inputs",
            "state",
            "trace",
            "runtime_tmp",
        )
        for key in keys:
            self.path(key).mkdir(parents=True, exist_ok=True)

    def relative(self, path: str | Path) -> str:
        """把工作区内绝对/相对路径规范化为 canonical POSIX 相对路径。"""
        candidate = Path(path)
        absolute = (
            candidate.resolve()
            if candidate.is_absolute()
            else (self.root / candidate).resolve()
        )
        try:
            return absolute.relative_to(self.root).as_posix()
        except ValueError as exc:
            raise WorkspacePathError(f"path escapes workspace: {path}") from exc

    def resolve(self, path: str | Path) -> Path:
        """解析 canonical v2 相对路径；拒绝逃逸与一切 legacy 逻辑别名。"""
        candidate = Path(path)
        if candidate.is_absolute():
            relative = self.relative(candidate)
            return self.root / relative
        logical = _safe_relative(str(path))
        head = logical.split("/", 1)[0]
        if logical == "skills" or head == "skills":
            raise WorkspacePathError(
                "logical Skill paths must be resolved through _state/skill-ref.json"
            )
        if head in {"plan", "materials", "tmp"}:
            raise WorkspacePathError(f"unsupported legacy logical path: {logical}")
        if logical == "assets/fonts" or logical.startswith("assets/fonts/"):
            raise WorkspacePathError(
                f"unsupported legacy logical path: {logical}"
            )
        self.relative(self.root / logical)
        return self.root / logical

    def manifest(self) -> dict[str, object]:
        """返回可写入 ``_state/workspace.json`` 的路径合同投影。"""
        return {
            "schema": WORKSPACE_PATHS_SCHEMA,
            "layout_version": self.layout_version,
            "paths": dict(self.paths),
        }

    def assert_no_forbidden_markers(self) -> None:
        """确认 workspace 未出现任何 legacy/禁用目录。"""
        forbidden = _forbidden_markers(self.root)
        if forbidden:
            raise WorkspacePathError(
                f"workspace contains forbidden legacy paths: {forbidden}"
            )


__all__ = [
    "WORKSPACE_LAYOUT_VERSION",
    "WORKSPACE_PATHS_SCHEMA",
    "WorkspacePathError",
    "WorkspacePaths",
]
