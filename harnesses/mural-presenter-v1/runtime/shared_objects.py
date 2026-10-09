"""维护内容寻址、不可变的共享 Skill object。

范围严格限定为 publish -> pin -> load/verify，以及固定引用所需的最小 release 身份。
不做全 Studio 扫描、历史/交付/训练审计、unreferenced 报告或 GC。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import threading
from types import ModuleType
from typing import Mapping
import uuid

from runtime.release_guard import release_tree_digest, source_tree_files
from runtime.workspace_paths import WorkspacePaths


SKILL_OBJECT_SCHEMA = "mural.shared-skill-object.v1"
SKILL_RELEASE_SCHEMA = "mural.shared-skill-release.v1"
SKILL_REF_SCHEMA = "mural.skill-ref.v1"


class SharedObjectError(RuntimeError):
    """表示共享对象缺失、损坏或不可变记录冲突。"""


# 批量模式下多个 worker 线程共享同一进程的 sys.modules；固定 Skill 模块的
# 加载必须串行化，否则第二个线程会拿到 exec_module 尚未跑完的半初始化模块
# (症状:font_bundle 缺 FAMILY_FACES 等属性)。按模块全名加锁,粒度最小。
_MODULE_LOAD_LOCK = threading.Lock()


def load_skill_module(skill_root: Path, tree_hash: str, module_name: str) -> ModuleType:
    """按固定对象身份隔离加载 Skill 模块，供字体与交付共用。"""
    source = skill_root / "scripts" / "_internal" / f"{module_name}.py"
    package = f"_mural_skill_{tree_hash}"
    name = f"{package}.scripts._internal.{module_name}"
    existing = sys.modules.get(name)
    if isinstance(existing, ModuleType) and getattr(existing, "__mural_loaded__", False):
        return existing
    with _MODULE_LOAD_LOCK:
        # 双检:可能在等锁期间别的线程已完成加载。
        existing = sys.modules.get(name)
        if isinstance(existing, ModuleType) and getattr(existing, "__mural_loaded__", False):
            return existing
        for pkg_name, path in {
            package: skill_root,
            f"{package}.scripts": skill_root / "scripts",
            f"{package}.scripts._internal": source.parent,
        }.items():
            if pkg_name not in sys.modules:
                namespace = ModuleType(pkg_name)
                namespace.__path__ = [str(path)]
                namespace.__package__ = pkg_name
                sys.modules[pkg_name] = namespace
        spec = importlib.util.spec_from_file_location(name, source)
        if spec is None or spec.loader is None:
            raise SharedObjectError(f"cannot load pinned Skill module: {source}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(name, None)
            raise
        # 只有 exec_module 完整跑完才打标记;别的线程凭此标记判断是否可复用,
        # 避免读到半初始化模块。
        module.__mural_loaded__ = True
        return module


def workspace_skill_module(workspace: Path, module_name: str) -> ModuleType:
    paths = WorkspacePaths.from_root(workspace)
    ref = SharedSkillStore(studio_data_root(workspace), create=False).load_ref(paths)
    return load_skill_module(ref.object_root, ref.tree_sha256, module_name)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    state = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            state.update(chunk)
    return state.hexdigest()


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def studio_data_root(
    workspace: str | Path,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """确定共享对象所在的 Studio data 根目录。"""
    env = environment if environment is not None else os.environ
    raw = str(
        env.get("MURAL_STUDIO_DATA_DIR")
        or env.get("STUDIO_DATA_DIR")
        or ""
    ).strip()
    if raw:
        return Path(raw).expanduser().resolve()
    repository = _repository_root()
    candidate = repository / "webui" / "studio" / "data"
    if (repository / "webui" / "studio").is_dir():
        return candidate.resolve()
    return (Path(workspace).expanduser().resolve().parent / ".mural-studio-data").resolve()


def _source_files(root: Path) -> list[Path]:
    return sorted(source_tree_files(root), key=lambda item: item.relative_to(root).as_posix().encode("utf-8"))


def _atomic_json(path: Path, value: Mapping[str, object], *, immutable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
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
                existing = path.read_text(encoding="utf-8")
                if existing != payload:
                    raise SharedObjectError(f"immutable record conflicts with existing file: {path}")
            else:
                path.chmod(0o444)
        else:
            os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def _make_tree_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            raise SharedObjectError(f"shared object unexpectedly contains a symlink: {path}")
        path.chmod(0o555 if path.is_dir() else 0o444)
    root.chmod(0o555)


def re_full_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


@dataclass(frozen=True)
class SkillObjectRef:
    skill_name: str
    tree_sha256: str
    object_root: Path
    object_manifest: Path
    release: str
    release_record: Path
    ref_path: Path


class SharedSkillStore:
    """创建、固定并验证 `<studio-data>/shared/skills`。"""

    def __init__(self, studio_data: str | Path, *, create: bool = True) -> None:
        self.studio_data = Path(studio_data).expanduser().resolve()
        self.root = self.studio_data / "shared" / "skills"
        self.objects = self.root / "objects"
        self.releases = self.root / "releases"
        if create:
            self.objects.mkdir(parents=True, exist_ok=True)
            self.releases.mkdir(parents=True, exist_ok=True)

    @classmethod
    def for_workspace(
        cls,
        workspace: str | Path,
        environment: Mapping[str, str] | None = None,
    ) -> "SharedSkillStore":
        return cls(studio_data_root(workspace, environment))

    def pin(
        self,
        paths: WorkspacePaths,
        source: str | Path,
        *,
        skill_name: str,
        environment: Mapping[str, str] | None = None,
        expected_tree_sha256: str | None = None,
    ) -> SkillObjectRef:
        """新 workspace 发布对象并写入固定引用；resume 只验证既有引用。"""
        if paths.skill_ref.is_file():
            return self.load_ref(paths)

        source_root = Path(source).expanduser().resolve()
        if not (source_root / "SKILL.md").is_file():
            raise SharedObjectError(f"Skill source is incomplete: {source_root}")
        tree_hash = release_tree_digest((source_root,), relative_to=source_root)
        if expected_tree_sha256 is not None and tree_hash != expected_tree_sha256:
            raise SharedObjectError("selected Skill object is not authorized by the production release")
        object_root = self.objects / tree_hash
        object_manifest = object_root / "object-manifest.json"
        if object_root.exists():
            self.verify_object(tree_hash)
        else:
            self._publish_object(source_root, tree_hash)
        object_identity = self._release_identity(tree_hash)
        release = self._workspace_release_identity(paths, tree_hash) or object_identity
        release_record = self.releases / skill_name / f"{release}.json"
        object_manifest_sha256 = _sha256(object_manifest)
        self._publish_release_record(
            release_record,
            skill_name=skill_name,
            release=release,
            tree_hash=tree_hash,
            object_manifest_sha256=object_manifest_sha256,
        )
        reference = {
            "schema": SKILL_REF_SCHEMA,
            "skill_name": skill_name,
            "tree_sha256": tree_hash,
            "object_manifest_sha256": object_manifest_sha256,
            "release": release,
            "release_id": release,
            "object_identity": object_identity,
            "release_record": release_record.relative_to(self.studio_data).as_posix(),
            "source_revision": self._source_revision(),
            "created_at": _utc_now(),
        }
        _atomic_json(paths.skill_ref, reference)
        return self.load_ref(paths, expected_skill_name=skill_name)

    def load_ref(
        self, paths: WorkspacePaths, *, expected_skill_name: str | None = None
    ) -> SkillObjectRef:
        """校验并加载 workspace 固定的 Skill 引用；损坏时明确失败，绝不回退 current。"""
        try:
            value = json.loads(paths.skill_ref.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SharedObjectError(f"cannot read pinned Skill ref: {exc}") from exc
        if not isinstance(value, dict) or value.get("schema") != SKILL_REF_SCHEMA:
            raise SharedObjectError("pinned Skill ref has an unsupported schema")
        skill_name = value.get("skill_name")
        if not isinstance(skill_name, str) or not skill_name or Path(skill_name).name != skill_name or skill_name in {".", ".."}:
            raise SharedObjectError("pinned Skill ref has an invalid Skill name")
        if expected_skill_name is not None and skill_name != expected_skill_name:
            raise SharedObjectError("pinned Skill ref selects a different Skill")
        tree_hash = str(value.get("tree_sha256") or "")
        if not re_full_sha256(tree_hash):
            raise SharedObjectError("pinned Skill ref has an invalid tree hash")
        object_manifest = self.verify_object(tree_hash)
        expected_manifest_hash = str(value.get("object_manifest_sha256") or "")
        if _sha256(object_manifest) != expected_manifest_hash:
            raise SharedObjectError("pinned Skill object manifest hash mismatch")
        release_relative = str(value.get("release_record") or "")
        release_record = self._bounded_store_path(release_relative)
        if not release_record.is_file():
            raise SharedObjectError(f"pinned Skill release record is missing: {release_relative}")
        release_value = json.loads(release_record.read_text(encoding="utf-8"))
        if (
            not isinstance(release_value, dict)
            or release_value.get("schema") != SKILL_RELEASE_SCHEMA
            or release_value.get("tree_sha256") != tree_hash
            or release_value.get("skill_name") != skill_name
            or str(release_value.get("release_id") or release_value.get("release") or "")
            != str(value.get("release_id") or value.get("release") or "")
            or release_value.get("object_manifest_sha256") != expected_manifest_hash
        ):
            raise SharedObjectError("pinned Skill release record does not match its object")
        return SkillObjectRef(
            skill_name=skill_name,
            tree_sha256=tree_hash,
            object_root=self.objects / tree_hash,
            object_manifest=object_manifest,
            release=str(value.get("release") or ""),
            release_record=release_record,
            ref_path=paths.skill_ref,
        )

    def verify_object(self, tree_hash: str) -> Path:
        """按不可变对象清单逐文件校验 hash/size/inventory。"""
        if not re_full_sha256(tree_hash):
            raise SharedObjectError(f"invalid Skill object hash: {tree_hash!r}")
        root = self.objects / tree_hash
        manifest_path = root / "object-manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SharedObjectError(f"Skill object manifest is missing or corrupt: {tree_hash}") from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema") != SKILL_OBJECT_SCHEMA
            or manifest.get("tree_sha256") != tree_hash
        ):
            raise SharedObjectError(f"Skill object manifest identity mismatch: {tree_hash}")
        rows = manifest.get("files")
        if not isinstance(rows, list) or not rows:
            raise SharedObjectError(f"Skill object manifest has no files: {tree_hash}")
        declared: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise SharedObjectError(f"Skill object contains an invalid file record: {tree_hash}")
            relative = str(row.get("path") or "")
            path = self._object_file(root, relative)
            declared.add(relative)
            if not path.is_file() or path.is_symlink():
                raise SharedObjectError(f"Skill object file is missing: {tree_hash}/{relative}")
            if path.stat().st_size != int(row.get("bytes", -1)):
                raise SharedObjectError(f"Skill object file size mismatch: {tree_hash}/{relative}")
            if _sha256(path) != str(row.get("sha256") or ""):
                raise SharedObjectError(f"Skill object file hash mismatch: {tree_hash}/{relative}")
        actual = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file() and path.name != "object-manifest.json"
        }
        if actual != declared:
            raise SharedObjectError(
                f"Skill object file inventory mismatch: missing={sorted(declared-actual)}, "
                f"extra={sorted(actual-declared)}"
            )
        return manifest_path

    def _publish_object(self, source: Path, tree_hash: str) -> None:
        target = self.objects / tree_hash
        pending = self.objects / f".{tree_hash}.{uuid.uuid4().hex}.pending"
        pending.mkdir(mode=0o755)
        try:
            rows: list[dict[str, object]] = []
            for path in _source_files(source):
                relative = path.relative_to(source)
                destination = pending / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
                destination.chmod(0o444)
                rows.append(
                    {
                        "path": relative.as_posix(),
                        "sha256": _sha256(destination),
                        "bytes": destination.stat().st_size,
                    }
                )
            copied_hash = hashlib.sha256("".join(
                f"{row['sha256']}  ./{row['path']}\n" for row in rows
            ).encode("utf-8")).hexdigest()
            if copied_hash != tree_hash:
                raise SharedObjectError("Skill source changed while its object bytes were copied")
            manifest = {
                "schema": SKILL_OBJECT_SCHEMA,
                "tree_sha256": tree_hash,
                "created_at": _utc_now(),
                "source_revision": self._source_revision(),
                "files": rows,
            }
            _atomic_json(pending / "object-manifest.json", manifest)
            if release_tree_digest((source,), relative_to=source) != tree_hash:
                raise SharedObjectError("Skill source changed while its object was being staged")
            try:
                pending.rename(target)
            except OSError as exc:
                # 内容寻址下 target 名即 tree_hash，另一并发 worker 已原子落好同哈希
                # 同内容的树。rename 到已存在的非空目录在 Linux 抛 ENOTEMPTY(39)，
                # 空目录抛 EEXIST/ENOTEMPTY——都视为幂等成功；其它错误(磁盘/权限)照抛。
                if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY) or not target.is_dir():
                    raise
            if target.is_dir():
                _make_tree_read_only(target)
                self.verify_object(tree_hash)
        finally:
            if pending.exists():
                shutil.rmtree(pending)

    def _release_identity(self, tree_hash: str) -> str:
        """从对象 hash 派生固定引用所需的最小 release id。"""
        return f"unreleased-{tree_hash[:16]}"

    def _workspace_release_identity(self, paths: WorkspacePaths, tree_hash: str) -> str:
        """Prefer the verified production release name over the content-only fallback."""
        release_path = paths.state / "release.json"
        try:
            value = json.loads(release_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        if not isinstance(value, dict):
            return ""
        if str(value.get("policy") or "").strip().lower() not in {"prod", "production"}:
            return ""
        if str((value.get("actual") or {}).get("skill") or "") != tree_hash:
            return ""
        release = str(value.get("release") or "").strip()
        return release if release else ""

    def _publish_release_record(
        self,
        path: Path,
        *,
        skill_name: str,
        release: str,
        tree_hash: str,
        object_manifest_sha256: str,
    ) -> dict[str, object]:
        identity = {
            "schema": SKILL_RELEASE_SCHEMA,
            "skill_name": skill_name,
            "release": release,
            "release_id": release,
            "tree_sha256": tree_hash,
            "object_manifest_sha256": object_manifest_sha256,
            "source_revision": self._source_revision(),
        }
        def existing_record() -> dict[str, object]:
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise SharedObjectError(f"immutable Skill release record is corrupt: {path}") from exc
            if not isinstance(existing, dict):
                raise SharedObjectError(f"immutable Skill release record is invalid: {path}")
            for key, expected in identity.items():
                if existing.get(key) != expected:
                    raise SharedObjectError(
                        f"immutable Skill release record conflicts at {key}: {path}"
                    )
            return existing

        if path.is_file():
            return existing_record()
        value = {**identity, "created_at": _utc_now()}
        try:
            _atomic_json(path, value, immutable=True)
        except SharedObjectError:
            if not path.is_file():
                raise
            # Concurrent publishers may differ only in their creation timestamp.
            return existing_record()
        return value

    def _source_revision(self) -> str:
        head = _repository_root() / ".git" / "HEAD"
        try:
            return head.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _object_file(self, root: Path, relative: str) -> Path:
        raw = Path(relative)
        if raw.is_absolute() or not raw.parts or ".." in raw.parts:
            raise SharedObjectError(f"invalid Skill object relative path: {relative!r}")
        path = (root / raw).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError as exc:
            raise SharedObjectError(f"Skill object path escapes its object: {relative}") from exc
        return path

    def _bounded_store_path(self, relative: str) -> Path:
        raw = Path(relative)
        if raw.is_absolute() or not raw.parts or ".." in raw.parts:
            raise SharedObjectError("shared store reference path is invalid")
        path = (self.studio_data / raw).resolve()
        try:
            path.relative_to(self.studio_data)
        except ValueError as exc:
            raise SharedObjectError("shared store reference escapes studio-data") from exc
        return path


__all__ = [
    "SKILL_OBJECT_SCHEMA",
    "SKILL_REF_SCHEMA",
    "SKILL_RELEASE_SCHEMA",
    "SharedObjectError",
    "SharedSkillStore",
    "SkillObjectRef",
    "re_full_sha256",
    "studio_data_root",
]
