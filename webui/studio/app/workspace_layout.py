"""WebUI 只读取 Harness 写入的 layout-v2 路径合同，不推断或回退旧布局。"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Iterable, Mapping


PATHS_SCHEMA = "mural.workspace-paths.v2"
LAYOUT_V2 = 2

_plan_spec = importlib.util.spec_from_file_location(
    "mural_studio_deck_plan",
    Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1/scripts/_internal/deck_plan.py",
)
_deck_plan = importlib.util.module_from_spec(_plan_spec)
_plan_spec.loader.exec_module(_deck_plan)

_config_spec = importlib.util.spec_from_file_location(
    "mural_studio_deck_config",
    Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1/scripts/_internal/deck_config.py",
)
_deck_config = importlib.util.module_from_spec(_config_spec)
_config_spec.loader.exec_module(_deck_config)


_REQUIRED = frozenset({
    "deck",
    "knowledge", "speech", "base_css", "present", "result", "assets",
    "asset_catalog", "slides", "slide_pattern", "renders", "render_pattern",
    "delivery", "delivery_manifest", "portable_zip", "runtime", "runtime_tmp",
})
_FORBIDDEN = ("plan", "materials", "skills", "tmp", "assets/fonts", "_runtime/tmp/compat")


def _safe_relative(value: object) -> str:
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
class WorkspaceLayout:
    root: Path
    layout_version: int
    paths: Mapping[str, str]

    @classmethod
    def load(cls, workspace: str | Path) -> "WorkspaceLayout":
        """严格读取既有 v2 manifest，旧数据保留但不由当前 WebUI 解释。"""
        root = Path(workspace).expanduser().resolve()
        if any((root / name).is_symlink() for name in ("_state", "_trace", "_state/workspace.json")):
            raise ValueError("unsupported_workspace_layout: private state/trace paths must not be symlinks")
        state = root / "_state" / "workspace.json"
        if not state.is_file():
            raise ValueError("unsupported_workspace_layout: workspace manifest is missing")
        try:
            value = json.loads(state.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"workspace state is unreadable: {state}: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError("workspace state must contain one JSON object")
        version = value.get("layout_version")
        if type(version) is not int or version != LAYOUT_V2:
            raise ValueError(f"unsupported_workspace_layout: layout_version={version!r}")
        if value.get("paths_schema") != PATHS_SCHEMA:
            raise ValueError("unsupported_workspace_layout: invalid paths_schema")
        mapping = value.get("paths")
        if not isinstance(mapping, dict) or not _REQUIRED.issubset(mapping):
            raise ValueError("workspace path mapping is incomplete")
        normalized = {
            str(key): _safe_relative(relative) for key, relative in mapping.items()
        }
        for marker in _FORBIDDEN:
            if (root / marker).exists() or (root / marker).is_symlink():
                raise ValueError(f"unsupported_workspace_layout: forbidden directory {marker}")
        for relative in normalized.values():
            if any(relative == prefix or relative.startswith(prefix + "/") for prefix in _FORBIDDEN):
                raise ValueError("unsupported_workspace_layout: forbidden path mapping")
            if not (root / relative.format(page=1)).resolve().is_relative_to(root):
                raise ValueError("unsupported_workspace_layout: path mapping escapes workspace")
        return cls(root, version, MappingProxyType(normalized))

    def path(self, key: str, *, page: int | None = None) -> Path:
        relative = self.paths[key]
        if "{page" in relative:
            if page is None or int(page) < 1:
                raise ValueError(f"workspace path {key} requires a positive page")
            relative = relative.format(page=int(page))
        target = (self.root / _safe_relative(relative)).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"workspace path escapes root: {key}") from exc
        return target

    def relative(self, path: str | Path) -> str:
        target = Path(path).expanduser().resolve()
        return target.relative_to(self.root).as_posix()

    def geometry(self) -> dict | None:
        from PIL import Image
        pages = sorted(self.path("renders").glob("slide_*.png"))
        if not pages:
            return None
        with Image.open(pages[0]) as image:
            width, height = image.size
        return {"width_px": width, "height_px": height,
                "aspect_ratio": width / height, "source": self.relative(pages[0])}

    def plan_pages(self) -> list[dict]:
        if not self.path("deck").is_file():
            return []
        return _deck_plan.read_deck(self.root)[1]

    def creation_config(self) -> dict:
        if "creation_config" not in self.paths:
            raise ValueError("workspace manifest has no creation_config path mapping")
        value = json.loads(self.path("creation_config").read_text(encoding="utf-8"))
        return _deck_config.validate_config(value, require_complete=True)

    def plan_text(self, page: int = 0) -> str:
        if not self.path("deck").is_file():
            return ""
        if page == 0:
            return self.path("deck").read_text(encoding="utf-8")
        return next((row["text"] for row in self.plan_pages() if row["number"] == page), "")

    def portable_download(self) -> Path:
        """只交付 Harness 已验收的原始 ZIP；下载不触发重新构建。"""
        manifest = json.loads(self.path("delivery_manifest").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema") != "mural.delivery.v1":
            raise ValueError("v1 交付 manifest 格式无效")
        if manifest.get("status") != "complete":
            raise ValueError("v1 交付尚未验收完成")
        outputs = manifest.get("outputs")
        record = outputs.get("portable_zip") if isinstance(outputs, dict) else None
        if not isinstance(record, dict):
            raise ValueError("v1 交付 manifest 缺少 portable_zip")
        archive = self.path("portable_zip")
        if _safe_relative(record.get("path")) != self.relative(archive):
            raise ValueError("v1 交付 ZIP 路径与 workspace manifest 不一致")
        if not archive.is_file() or archive.stat().st_size == 0:
            raise ValueError("v1 交付缺少 presentation.zip")
        with archive.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if record.get("bytes") != archive.stat().st_size or record.get("sha256") != digest:
            raise ValueError("v1 交付 ZIP 与已验收 manifest 不一致")
        return archive

    def group_files(self) -> Iterable[tuple[str, str, list[Path]]]:
        specs = (
            ("knowledge", "Knowledge", [*self._existing("knowledge"),
                *sorted(path for path in (self.root / "research").rglob("*.md")
                        if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(self.root))]),
            (
                "plan",
                "Plan",
                [
                    *self._existing("creation_config"),
                    *self._existing("deck"),
                    *sorted((path for path in (self.root / "briefs").glob("slide_*.md")
                             if path.stem[6:].isdigit() and not path.is_symlink()
                             and path.resolve().is_relative_to(self.root)),
                            key=lambda path: int(path.stem[6:])),
                ],
            ),
            ("assets", "Assets", self._asset_files()),
            (
                "slides",
                "Slides",
                [
                    *self._existing("base_css"),
                    *sorted(self.path("slides").glob("slide_*.html")),
                ],
            ),
            ("renders", "Renders", self._render_files()),
            ("delivery", "Presentation", [*self._existing("present", "speech", "result"), *self._delivery_files()]),
            ("attachments", "Attachments", self._attachment_files()),
        )
        for group_id, label, files in specs:
            unique = list(dict.fromkeys(path for path in files if path.is_file()))
            if group_id != "plan":
                unique.sort()
            if unique:
                yield group_id, label, unique

    def attachment_records(self) -> list[dict[str, object]]:
        """Read the v2 attachment manifest for WebUI list/download/preview views.

        Returns one entry per logical attachment with workspace-relative paths for
        the immutable original, the extracted ``text.md``, and per-page assets. Old
        legacy decks have no such manifest and yield an empty list.
        """
        manifest_path = self.root / "_trace" / "attachment-manifest.json"
        if not manifest_path.is_file():
            return []
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(manifest, dict):
            return []
        detail_by_id = {
            str(item.get("attachment_id")): item
            for item in (manifest.get("records") or [])
            if isinstance(item, dict)
        }
        entries: list[dict[str, object]] = []
        for item in manifest.get("attachments") or []:
            if not isinstance(item, dict):
                continue
            attachment_id = str(item.get("attachment_id") or "")
            detail = detail_by_id.get(attachment_id, {})
            text_rel = detail.get("companion_path")
            pages = [str(page) for page in (detail.get("page_image_paths") or [])]
            entries.append(
                {
                    "attachment_id": attachment_id,
                    "sha256": str(item.get("sha256") or ""),
                    "ordinal": int(item.get("ordinal") or 0),
                    "original_name": str(item.get("original_name") or ""),
                    "original_path": str(item.get("original_path") or ""),
                    "extracted_root": str(item.get("extracted_root") or ""),
                    "kind": str(detail.get("kind") or "document"),
                    "mime": str(detail.get("mime") or ""),
                    "text_path": str(text_rel) if text_rel else "",
                    "page_image_paths": pages,
                    "media_paths": [
                        str(media) for media in (detail.get("media_paths") or [])
                    ],
                }
            )
        entries.sort(key=lambda entry: entry["ordinal"])
        return entries

    def _attachment_files(self) -> list[Path]:
        """Original files plus extracted ``text.md`` for the Attachments group."""
        files: list[Path] = []
        for entry in self.attachment_records():
            for rel in (entry["original_path"], entry["text_path"]):
                if rel:
                    files.append((self.root / _safe_relative(rel)).resolve())
        return files

    def _existing(self, *keys: str) -> list[Path]:
        return [self.path(key) for key in keys if key in self.paths and self.path(key).is_file()]

    def _asset_files(self) -> list[Path]:
        root = self.path("assets")
        if not root.is_dir():
            return []
        result = []
        for path in root.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(root)
            if relative.parts and relative.parts[0] in {"fonts", "vendor"}:
                continue
            result.append(path)
        return result

    def _render_files(self) -> list[Path]:
        root = self.path("renders")
        if not root.is_dir():
            return []
        return [
            path
            for path in root.iterdir()
            if path.is_file()
            and (path.name in {"contact-sheet.png", "render.json"} or path.name.startswith("slide_"))
        ]

    def _delivery_files(self) -> list[Path]:
        root = self.path("delivery")
        if not root.is_dir():
            return []
        return [
            root / "presentation.html",
            root / "presentation.pdf",
            root / "presentation.pptx",
            self.path("portable_zip"),
        ]


__all__ = ["LAYOUT_V2", "PATHS_SCHEMA", "WorkspaceLayout"]
