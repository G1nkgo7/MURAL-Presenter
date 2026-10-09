#!/usr/bin/env python3
"""只读加载 Harness 写入的 workspace 路径映射（纯 layout-v2）。

这是一个薄读取器：它只读取并校验 Harness 写入的 ``_state/workspace.json``，
不自行定义第二套布局表，也不识别、推断或映射任何 legacy 布局。
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Mapping


LAYOUT_V2 = 2
PATHS_SCHEMA = "mural.workspace-paths.v2"

# canonical v2 映射必须至少包含这些键；具体路径值只来自 manifest，不在此硬编码。
_REQUIRED_KEYS = frozenset(
    {
        "deck",
        "knowledge",
        "speech",
        "base_css",
        "present",
        "result",
        "assets",
        "asset_catalog",
        "slides",
        "slide_pattern",
        "renders",
        "render_pattern",
        "delivery",
        "delivery_manifest",
        "portable_zip",
        "state",
        "trace",
        "runtime",
        "runtime_tmp",
        "edit_operations",
        "revision_impact",
        "skill_ref",
        "font_manifest",
        "font_request",
    }
)

_LEGACY_PREFIXES = ("plan/", "skills/", "tmp/", "materials/", "assets/fonts")
_LEGACY_NAMES = {"plan", "skills", "tmp", "materials", "assets/fonts", "_runtime/tmp/compat"}


def _safe_relative(value: str) -> str:
    raw = str(value or "").replace("\\", "/").strip()
    path = PurePosixPath(raw)
    if (
        not raw
        or path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"invalid workspace path mapping: {value!r}")
    return path.as_posix()


@dataclass(frozen=True)
class WorkspacePaths:
    root: Path
    layout_version: int
    mapping: Mapping[str, str]

    @classmethod
    def load(cls, workspace: str | Path) -> "WorkspacePaths":
        """读取并校验 Harness 写入的 ``_state/workspace.json``。"""
        root = Path(workspace).expanduser().resolve()
        if any((root / name).is_symlink() for name in ("_state", "_trace", "_state/workspace.json")):
            raise ValueError("unsupported_workspace_layout: private state/trace paths must not be symlinks")
        state = root / "_state" / "workspace.json"
        if not state.is_file():
            raise ValueError(
                f"workspace manifest is missing: {state}; the Harness must write it first"
            )
        try:
            value = json.loads(state.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"workspace path state is unreadable: {state}: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError("workspace state must contain one JSON object")
        version = value.get("layout_version")
        if type(version) is not int or version != LAYOUT_V2:
            raise ValueError(f"unsupported workspace layout_version: {version}")
        if value.get("paths_schema") != PATHS_SCHEMA:
            raise ValueError("layout-v2 workspace has no supported paths schema")
        mapping = value.get("paths")
        if not isinstance(mapping, dict) or not _REQUIRED_KEYS.issubset(mapping):
            missing = sorted(_REQUIRED_KEYS - set(mapping or {}))
            raise ValueError(f"workspace path mapping is incomplete: {missing}")
        normalized = {str(key): _safe_relative(str(item)) for key, item in mapping.items()}
        invalid = [
            f"{key}={relative}"
            for key, relative in normalized.items()
            if relative in _LEGACY_NAMES or relative.startswith(_LEGACY_PREFIXES)
        ]
        if invalid:
            raise ValueError("layout-v2 mapping contains legacy paths: " + ", ".join(invalid))
        for marker in _LEGACY_NAMES:
            target = root / marker
            if target.exists() or target.is_symlink():
                raise ValueError(f"unsupported_workspace_layout: forbidden directory {marker}")
        for relative in normalized.values():
            if not (root / relative.format(page=1)).resolve().is_relative_to(root):
                raise ValueError("unsupported_workspace_layout: path mapping escapes workspace")
        return cls(root, version, MappingProxyType(normalized))

    def path(self, key: str, *, page: int | None = None) -> Path:
        if key not in self.mapping:
            raise ValueError(f"unknown workspace path key: {key}")
        relative = self.mapping[key]
        if "{page" in relative:
            if page is None or isinstance(page, bool) or int(page) < 1:
                raise ValueError(f"workspace path {key} requires a positive page")
            relative = relative.format(page=int(page))
        target = self.root / _safe_relative(relative)
        if not target.resolve().is_relative_to(self.root):
            raise ValueError(f"workspace path escapes root: {key}")
        return target

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
    def runtime_tmp(self) -> Path:
        return self.path("runtime_tmp")

    @property
    def runtime_fonts(self) -> Path:
        return self.runtime_tmp / "fonts"

    @property
    def font_manifest(self) -> Path:
        return self.path("font_manifest")

    def page_html(self, page: int) -> Path:
        return self.path("slide_pattern", page=page)

    def page_png(self, page: int) -> Path:
        return self.path("render_pattern", page=page)

    def relative(self, path: str | Path) -> str:
        absolute = Path(path).expanduser().resolve()
        try:
            return absolute.relative_to(self.root).as_posix()
        except ValueError as exc:
            raise ValueError(f"path leaves workspace: {path}") from exc

    def resolve(self, value: str | Path) -> Path:
        """解析 canonical v2 相对路径；拒绝逃逸与一切 legacy 逻辑别名。"""
        path = Path(value)
        if path.is_absolute():
            self.relative(path)
            return path.resolve()
        logical = _safe_relative(str(value))
        head = logical.split("/", 1)[0]
        if head == "skills":
            raise ValueError(
                "logical Skill paths must be resolved through _state/skill-ref.json"
            )
        if logical in _LEGACY_NAMES or logical.startswith(_LEGACY_PREFIXES):
            raise ValueError(f"unsupported legacy logical path: {logical}")
        return self.root / logical

    def root_allowlist(self) -> set[str]:
        names = {
            PurePosixPath(relative).parts[0]
            for relative in self.mapping.values()
            if "{page" not in relative
        }
        names.add(PurePosixPath(self.mapping["slide_pattern"]).parts[0])
        names.add(PurePosixPath(self.mapping["render_pattern"]).parts[0])
        return names


__all__ = ["LAYOUT_V2", "PATHS_SCHEMA", "WorkspacePaths"]
