"""把作者字体固定到共享对象，并维护 workspace 的精确字体引用。

范围严格限定为：按 hash 固定原始字体、public 与 user-private 边界、workspace manifest
的精确引用、runtime subset 回执，以及加载时的 hash/owner 校验。不做 audit、GC 或历史扫描。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import threading
from types import ModuleType
from typing import Callable, Mapping
import uuid

from runtime.shared_objects import SharedObjectError, SharedSkillStore, _sha256, load_skill_module, studio_data_root
from runtime.workspace_paths import WorkspacePaths


FONT_OBJECT_SCHEMA = "mural.shared-font-object.v1"
FONT_MANIFEST_SCHEMA = "mural.font-manifest.v2"
FONT_REQUEST_SCHEMA = "mural.font-request.v1"


class SharedFontError(SharedObjectError):
    """表示字体对象、用户隔离或精确引用校验失败。"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, value: Mapping[str, object], *, immutable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if immutable and path.is_file():
        if path.read_text(encoding="utf-8") != payload:
            raise SharedFontError(f"immutable font metadata conflicts: {path}")
        return
    pending = path.with_name(f".{path.name}.{uuid.uuid4().hex}.pending")
    try:
        descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if immutable:
            try:
                os.link(pending, path)
            except FileExistsError:
                if path.read_text(encoding="utf-8") != payload:
                    raise SharedFontError(f"immutable font metadata conflicts: {path}")
            else:
                path.chmod(0o444)
        else:
            os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def _font_module(skill_root: Path, tree_hash: str) -> ModuleType:
    """从固定 Skill object 加载 font bundle 模块，只用于读取 face/license 定义。"""
    source = skill_root / "scripts" / "_internal" / "font_bundle.py"
    if not source.is_file():
        raise SharedFontError(f"pinned Skill has no font bundle module: {source}")
    return load_skill_module(skill_root, tree_hash, "font_bundle")


def font_reference_resolver(workspace: Path) -> Callable[[str], tuple[Path, str]]:
    """验证并加载一次固定程序；返回的解析器仍实时校验字体授权和内容。"""
    try:
        paths = WorkspacePaths.from_root(workspace)
        manifest = json.loads(paths.font_manifest.read_text(encoding="utf-8"))
        store = SharedSkillStore(manifest["studio_data_root"], create=False)
        ref = store.load_ref(paths)
        module = _font_module(ref.object_root, ref.tree_sha256)
    except (OSError, KeyError, TypeError, ValueError, SharedObjectError) as exc:
        raise SharedFontError(f"invalid pinned font reference: {exc}") from exc

    def resolve(reference: str) -> tuple[Path, str]:
        try:
            current = json.loads(paths.font_manifest.read_text(encoding="utf-8"))
            if Path(current["studio_data_root"]).resolve() != store.studio_data:
                raise SharedFontError("font manifest belongs to a different studio-data root")
            return module.resolve_shared_font_reference(workspace, reference)
        except (OSError, KeyError, TypeError, ValueError, SharedObjectError) as exc:
            raise SharedFontError(f"invalid pinned font reference: {exc}") from exc

    return resolve


def resolve_font_reference(workspace: Path, reference: str) -> tuple[Path, str]:
    return font_reference_resolver(workspace)(reference)


@dataclass(frozen=True)
class FontPin:
    manifest_path: Path
    manifest: Mapping[str, object]


class SharedFontStore:
    """管理 public/private 字体对象，并使 workspace 只引用精确 hash。"""

    def __init__(
        self,
        workspace: str | Path,
        environment: Mapping[str, str] | None = None,
        *,
        create: bool = True,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.environment = dict(environment or os.environ)
        self.studio_data = studio_data_root(self.workspace, self.environment)
        self._reference_resolver = None
        self._font_name_resolver = None
        self._reference_lock = threading.Lock()
        self.public_root = self.studio_data / "shared" / "fonts"
        self.public_objects = self.public_root / "objects"
        self.public_metadata = self.public_root / "metadata"
        if create:
            self.public_objects.mkdir(parents=True, exist_ok=True)
            self.public_metadata.mkdir(parents=True, exist_ok=True)

    def pin_workspace(
        self,
        paths: WorkspacePaths,
        *,
        skill_root: Path,
        skill_tree_sha256: str,
    ) -> FontPin:
        """发布并固定 Skill 声明的字体面；resume 只验证既有 manifest。"""
        if paths.font_manifest.is_file():
            value = self.verify_manifest(paths.font_manifest)
            return FontPin(paths.font_manifest, value)

        module = _font_module(skill_root, skill_tree_sha256)
        source_dirs = self._font_source_dirs(skill_root)
        sources: list[dict[str, object]] = []
        for family in sorted(module.FAMILY_FACES):
            license_value = dict(module.FONT_LICENSES[family])
            for face in module.FAMILY_FACES[family]:
                try:
                    source = module._find_source(face, source_dirs)
                except FileNotFoundError:
                    continue
                reference = self.publish_public_font(
                    source,
                    license_name=str(license_value.get("license") or ""),
                    license_source=str(license_value.get("source") or ""),
                )
                sources.append(
                    {
                        "family": family,
                        "weight": str(face.weight),
                        "style": str(face.style),
                        "source_name": source.name,
                        "sha256": reference["sha256"],
                        "bytes": reference["bytes"],
                        "extension": reference["extension"],
                        "object_ref": reference["object_ref"],
                        "metadata_ref": reference["metadata_ref"],
                        "license": str(license_value.get("license") or ""),
                        "license_source": str(license_value.get("source") or ""),
                        "scope": "public",
                    }
                )

        request, custom_fonts, roles = self._load_user_request(paths)
        value: dict[str, object] = {
            "schema": FONT_MANIFEST_SCHEMA,
            "layout_version": paths.layout_version,
            "created_at": _utc_now(),
            "skill_tree_sha256": skill_tree_sha256,
            "studio_data_root": str(self.studio_data),
            "source_faces": sources,
            "custom_fonts": custom_fonts,
            "roles": roles,
            "request_sha256": _sha256(paths.path("font_request")) if request else "",
            "selected_faces": [],
            "runtime_bundle": None,
        }
        _atomic_json(paths.font_manifest, value)
        return FontPin(paths.font_manifest, self.verify_manifest(paths.font_manifest))

    def publish_public_font(
        self,
        source: Path,
        *,
        license_name: str,
        license_source: str,
    ) -> dict[str, object]:
        source = source.expanduser().resolve()
        if not source.is_file() or not source.stat().st_size:
            raise SharedFontError(f"font source is missing or empty: {source}")
        digest = _sha256(source)
        suffix = source.suffix.lower()
        if suffix not in {".ttf", ".otf", ".woff", ".woff2"}:
            raise SharedFontError(f"unsupported font object extension: {suffix}")
        target = self.public_objects / f"{digest}{suffix}"
        self._publish_file(source, target, digest)
        metadata = self.public_metadata / f"{digest}.json"
        _atomic_json(
            metadata,
            {
                "schema": FONT_OBJECT_SCHEMA,
                "sha256": digest,
                "bytes": source.stat().st_size,
                "extension": suffix,
                "scope": "public",
                "license": license_name,
                "license_source": license_source,
            },
            immutable=True,
        )
        return {
            "sha256": digest,
            "bytes": source.stat().st_size,
            "extension": suffix,
            "object_ref": target.relative_to(self.studio_data).as_posix(),
            "metadata_ref": metadata.relative_to(self.studio_data).as_posix(),
        }

    def _read_manifest(self, path: Path) -> dict[str, object]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SharedFontError(f"font manifest is missing or corrupt: {path}") from exc
        if not isinstance(value, dict) or value.get("schema") != FONT_MANIFEST_SCHEMA:
            raise SharedFontError("font manifest has an unsupported schema")
        manifest_root = Path(str(value.get("studio_data_root") or "")).expanduser().resolve()
        if manifest_root != self.studio_data:
            raise SharedFontError("font manifest belongs to a different studio-data root")
        source_rows = value.get("source_faces") or []
        custom_rows = value.get("custom_fonts") or []
        if not isinstance(source_rows, list) or not isinstance(custom_rows, list):
            raise SharedFontError("font manifest source inventories must be arrays")
        return value

    def verify_manifest(
        self, path: Path, *, enforce_active_user: bool = True
    ) -> dict[str, object]:
        value = self._read_manifest(path)
        source_rows = value.get("source_faces") or []
        custom_rows = value.get("custom_fonts") or []
        identities: set[tuple[str, str, str]] = set()
        for row in [*source_rows, *custom_rows]:
            if not isinstance(row, dict):
                raise SharedFontError("font manifest contains a non-object source")
            object_path = self._verify_manifest_object(row)
            if row.get("scope") == "public":
                try:
                    object_path.relative_to(self.public_objects.resolve())
                except ValueError as exc:
                    raise SharedFontError("public font object is outside the public store") from exc
                identity = (
                    str(row.get("family") or ""),
                    str(row.get("weight") or ""),
                    str(row.get("style") or ""),
                )
                if identity in identities:
                    raise SharedFontError(f"font manifest has a duplicate face: {identity}")
                identities.add(identity)
            elif row.get("scope") == "user":
                owner_user_id = str(row.get("owner_user_id") or "")
                if not owner_user_id:
                    raise SharedFontError("private font object has no owner_user_id")
                if enforce_active_user and owner_user_id != str(
                    self.environment.get("MURAL_USER_ID") or ""
                ):
                    raise SharedFontError("private font object belongs to a different WebUI user")
                private_root = (
                    self.studio_data / "users" / owner_user_id / "_shared" / "fonts" / "objects"
                ).resolve()
                try:
                    object_path.relative_to(private_root)
                except ValueError as exc:
                    raise SharedFontError("private font object escapes its owner store") from exc
            else:
                raise SharedFontError("font manifest contains an unsupported object scope")
        return value

    def resolve_reference(self, reference: str) -> tuple[Path, str]:
        """本次运行复用固定解析程序，不缓存授权结果或字体文件 hash。"""
        with self._reference_lock:
            if self._reference_resolver is None:
                self._reference_resolver = font_reference_resolver(self.workspace)
            resolve = self._reference_resolver
        return resolve(reference)

    def capture_runtime_bundle(self, paths: WorkspacePaths) -> None:
        """把 Skill 渲染时产出的 runtime subset 回执并入固定 manifest。"""
        value = self._read_manifest(paths.font_manifest)
        bundle_path = paths.runtime_fonts / "manifest.json"
        css_path = paths.path("base_css")
        css = css_path.read_text(encoding="utf-8") if css_path.is_file() else ""
        shared_block = re.search(r"/\* SHARED_AUTHORING_FONTS_START \*/(.*?)/\* SHARED_AUTHORING_FONTS_END \*/", css, re.S)
        if shared_block:
            selected = []
            resolve = self.resolve_reference
            resolved_references = {}
            available = [*(value.get("source_faces") or []),
                         *({**row, "family": f"User::{row['id']}", "weight": str(row.get("weight") or 400),
                            "style": ("italic" if "italic" in str(row.get("subfamily") or "").lower() else
                                      "oblique" if "oblique" in str(row.get("subfamily") or "").lower() else "normal")}
                           for row in value.get("custom_fonts") or [])]
            for rule in re.findall(r"@font-face\s*\{([^}]+)\}", shared_block.group(1)):
                reference = re.search(r'url\("([^\"]+)"\)', rule).group(1)
                if reference not in resolved_references:
                    resolved_references[reference] = resolve(reference)
                source, digest = resolved_references[reference]
                row = next((row for row in available
                            if row.get("sha256") == digest
                            and any(f'font-family: {json.dumps(name, ensure_ascii=False)};' in rule
                                    for name in self._authoring_font_names(paths, row["family"]))
                            and f'font-weight: {row["weight"]};' in rule
                            and f'font-style: {row["style"]};' in rule
                            and self._resolve_ref(row["object_ref"]) == source), None)
                if row is None:
                    raise SharedFontError("authoring font face does not match its pinned identity")
                if row.get("scope") == "user" and str(row.get("owner_user_id") or "") != str(self.environment.get("MURAL_USER_ID") or ""):
                    raise SharedFontError("private font object belongs to a different WebUI user")
                if row.get("bytes") and source.stat().st_size != int(row["bytes"]):
                    raise SharedFontError("pinned font object size mismatch")
                face = {"source_family": row["family"], "weight": row["weight"],
                        "style": row["style"], "source_object_ref": row["object_ref"],
                        "source_object_sha256": digest}
                if face not in selected:
                    selected.append(face)
            if value.get("runtime_bundle") is not None or value.get("selected_faces") != selected:
                _atomic_json(paths.font_manifest, {**value, "selected_faces": selected, "runtime_bundle": None})
            return
        if not bundle_path.is_file():
            if value.get("runtime_bundle") is not None:
                _atomic_json(paths.font_manifest, {**value, "selected_faces": [], "runtime_bundle": None})
            return
        value = self.verify_manifest(paths.font_manifest)
        try:
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SharedFontError(f"runtime font bundle manifest is corrupt: {exc}") from exc
        faces = bundle.get("faces") if isinstance(bundle, dict) else None
        if not isinstance(faces, list):
            raise SharedFontError("runtime font bundle does not contain face records")
        pinned_hashes = {
            str(row.get("sha256") or "")
            for row in [
                *(value.get("source_faces") or []),
                *(value.get("custom_fonts") or []),
            ]
            if isinstance(row, dict)
        }
        selected: list[dict[str, object]] = []
        for row in faces:
            if not isinstance(row, dict):
                raise SharedFontError("runtime font bundle contains an invalid face")
            source_hash = str(row.get("source_object_sha256") or "")
            if source_hash not in pinned_hashes:
                raise SharedFontError("runtime font bundle used an unpinned source object")
            relative = str(row.get("path") or "")
            subset = paths.resolve(relative)
            if not subset.is_file() or _sha256(subset) != str(row.get("sha256") or ""):
                raise SharedFontError(f"runtime font subset hash mismatch: {relative}")
            selected.append(dict(row))
        updated = dict(value)
        updated["selected_faces"] = selected
        updated["runtime_bundle"] = {
            "path": paths.relative(bundle_path),
            "sha256": _sha256(bundle_path),
            "face_count": len(selected),
            "updated_at": _utc_now(),
        }
        _atomic_json(paths.font_manifest, updated)

    def _authoring_font_names(self, paths: WorkspacePaths, family: str) -> list[str]:
        """名称只来自本次固定 Skill 的确切别名表；不改 CSS 或放宽字体身份。"""
        with self._reference_lock:
            if self._font_name_resolver is None:
                ref = SharedSkillStore(self.studio_data, create=False).load_ref(paths)
                self._font_name_resolver = _font_module(ref.object_root, ref.tree_sha256)._font_face_names
            resolve = self._font_name_resolver
        return resolve(family)

    def _load_user_request(
        self, paths: WorkspacePaths
    ) -> tuple[dict[str, object] | None, list[dict[str, object]], dict[str, object]]:
        request_path = paths.path("font_request")
        if not request_path.is_file():
            return None, [], {}
        try:
            value = json.loads(request_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SharedFontError(f"font request is unreadable: {exc}") from exc
        if not isinstance(value, dict) or value.get("schema") != FONT_REQUEST_SCHEMA:
            raise SharedFontError("font request has an unsupported schema")
        user_id = str(value.get("user_id") or "")
        expected_user_id = str(self.environment.get("MURAL_USER_ID") or "")
        if not user_id or user_id != expected_user_id:
            raise SharedFontError("font request does not belong to the active WebUI user")
        if value.get("fonts") and not value.get("license_acknowledged"):
            raise SharedFontError("font request lacks user embedding authorization")
        private_root = (self.studio_data / "users" / user_id / "_shared" / "fonts").resolve()
        custom: list[dict[str, object]] = []
        for raw in value.get("fonts") or []:
            if not isinstance(raw, dict):
                raise SharedFontError("font request contains an invalid custom font")
            row = dict(raw)
            row["scope"] = "user"
            row["owner_user_id"] = user_id
            object_path = self._resolve_ref(str(row.get("object_ref") or ""))
            try:
                object_path.relative_to(private_root)
            except ValueError as exc:
                raise SharedFontError("custom font object escapes the active user's store") from exc
            self._verify_manifest_object(row)
            custom.append(row)
        roles = value.get("roles") or {}
        if not isinstance(roles, dict):
            raise SharedFontError("font request roles must be an object")
        return value, custom, dict(roles)

    def _verify_manifest_object(self, row: Mapping[str, object]) -> Path:
        digest = str(row.get("sha256") or "")
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise SharedFontError("font manifest contains an invalid object hash")
        path = self._resolve_ref(str(row.get("object_ref") or ""))
        if not path.is_file() or path.is_symlink():
            raise SharedFontError(f"pinned font object is missing: {row.get('object_ref')}")
        if _sha256(path) != digest:
            raise SharedFontError(f"pinned font object hash mismatch: {row.get('object_ref')}")
        expected_bytes = int(row.get("bytes") or path.stat().st_size)
        if path.stat().st_size != expected_bytes:
            raise SharedFontError(f"pinned font object size mismatch: {row.get('object_ref')}")
        return path

    def _resolve_ref(self, relative: str) -> Path:
        raw = Path(relative)
        if raw.is_absolute() or not raw.parts or ".." in raw.parts:
            raise SharedFontError(f"invalid font object reference: {relative!r}")
        path = (self.studio_data / raw).resolve()
        try:
            path.relative_to(self.studio_data)
        except ValueError as exc:
            raise SharedFontError("font object reference escapes studio-data") from exc
        return path

    def _font_source_dirs(self, skill_root: Path) -> list[Path]:
        values = [
            Path(item).expanduser()
            for item in str(self.environment.get("PPT_FONT_SOURCE_DIRS") or "").split(os.pathsep)
            if item
        ]
        for parent in (skill_root / "scripts" / "_internal" / "font_bundle.py").parents:
            candidate = parent / "fonts"
            if candidate.is_dir():
                values.append(candidate)
        values.extend(
            (
                Path.home() / ".fonts",
                Path.home() / ".local/share/fonts",
                Path("/usr/share/fonts/opentype/noto"),
                Path("/usr/share/fonts/truetype/noto"),
            )
        )
        result: list[Path] = []
        for value in values:
            resolved = value.resolve()
            if resolved not in result:
                result.append(resolved)
        return result

    def _publish_file(self, source: Path, target: Path, digest: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if not target.is_file() or target.is_symlink() or _sha256(target) != digest:
                raise SharedFontError(f"existing font object is corrupt: {target}")
            return
        pending = target.with_name(f".{target.name}.{uuid.uuid4().hex}.pending")
        try:
            with source.open("rb") as reader:
                descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
                with os.fdopen(descriptor, "wb") as writer:
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)
                    writer.flush()
                    os.fsync(writer.fileno())
            try:
                os.link(pending, target)
            except FileExistsError:
                if _sha256(target) != digest:
                    raise SharedFontError(f"concurrent font object conflicts: {target}")
            else:
                target.chmod(0o444)
        finally:
            pending.unlink(missing_ok=True)


__all__ = [
    "FONT_MANIFEST_SCHEMA",
    "FONT_OBJECT_SCHEMA",
    "FONT_REQUEST_SCHEMA",
    "FontPin",
    "SharedFontError",
    "SharedFontStore",
    "resolve_font_reference",
]
