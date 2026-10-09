"""Layout-v2 acceptance: path contract, shared Skill/Font pinning, portable ZIP."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from runtime.shared_objects import SharedObjectError, SharedSkillStore
from runtime.workspace_paths import (
    WORKSPACE_LAYOUT_VERSION,
    WorkspacePathError,
    WorkspacePaths,
)
from state.workspace import WorkspaceState


REPOSITORY = Path(__file__).resolve().parents[3]
SKILL_SOURCE = REPOSITORY / "skills" / "mural-presenter-v1"
SKILL_NAME = "mural-presenter-v1"


# ---------------------------------------------------------------- path contract


def test_manifest_is_the_first_persistent_file(tmp_path: Path) -> None:
    WorkspaceState(tmp_path)
    manifest = tmp_path / "_state" / "workspace.json"
    assert manifest.is_file()
    value = json.loads(manifest.read_text(encoding="utf-8"))
    assert value["layout_version"] == WORKSPACE_LAYOUT_VERSION
    assert value["paths_schema"] == "mural.workspace-paths.v2"
    assert "canvas" not in value["paths"]
    assert not {"design_brief", "slide_briefs", "slide_brief_pattern", "plan_batch"}.intersection(value["paths"])
    assert not (tmp_path / "briefs").exists()
    assert "runtime_fonts" not in value["paths"]
    assert not (tmp_path / "delivery").exists()
    assert all(not (tmp_path / name).exists() for name in ("material.md", "research.md", "speech.md"))


@pytest.mark.parametrize("change", [
    {"paths_schema": "invalid"}, {"paths_schema": None}, {"paths": {}},
    {"layout_version": True}, {"layout_version": 2.5}, {"layout_version": "2"},
])
def test_invalid_manifest_contract_is_rejected_without_rewrite(tmp_path, change):
    WorkspaceState(tmp_path)
    path = tmp_path / "_state/workspace.json"
    state = json.loads(path.read_text()) | change
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    with pytest.raises(WorkspacePathError, match="unsupported_workspace_layout"):
        WorkspaceState(tmp_path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("relative", ["stray.txt", "_state/stale.json", "_trace/events.jsonl", "inputs/source.pdf", "_runtime/tmp/stale"])
def test_non_empty_workspace_without_manifest_is_rejected(tmp_path: Path, relative: str) -> None:
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x", encoding="utf-8")
    with pytest.raises(WorkspacePathError, match="unsupported_workspace_layout"):
        WorkspacePaths.from_root(tmp_path)


def test_manifest_layout_other_than_two_is_rejected(tmp_path: Path) -> None:
    WorkspaceState(tmp_path)
    manifest = tmp_path / "_state" / "workspace.json"
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value["layout_version"] = 1
    manifest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(WorkspacePathError, match="unsupported workspace layout_version"):
        WorkspacePaths.from_root(tmp_path)


def test_path_map_drift_is_rejected(tmp_path: Path) -> None:
    WorkspaceState(tmp_path)
    manifest = tmp_path / "_state" / "workspace.json"
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value["paths"]["base_css"] = "plan/base.css"
    manifest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(WorkspacePathError, match="path mapping"):
        WorkspaceState(tmp_path)


@pytest.mark.parametrize(
    "marker", ["plan", "skills", "tmp", "materials", "assets/fonts", "_runtime/tmp/compat"]
)
def test_forbidden_directories_are_rejected(tmp_path: Path, marker: str) -> None:
    WorkspaceState(tmp_path)
    (tmp_path / marker).mkdir(parents=True)
    with pytest.raises(WorkspacePathError, match="forbidden legacy paths"):
        WorkspacePaths.from_root(tmp_path)


@pytest.mark.parametrize(
    "logical",
    ["plan/deck.md", "tmp/x", "assets/fonts/x", "skills/x", "materials/y"],
)
def test_resolve_rejects_legacy_aliases(tmp_path: Path, logical: str) -> None:
    WorkspaceState(tmp_path)
    paths = WorkspacePaths.from_root(tmp_path)
    with pytest.raises(WorkspacePathError):
        paths.resolve(logical)


def test_resolve_accepts_direct_v2_paths(tmp_path: Path) -> None:
    WorkspaceState(tmp_path)
    paths = WorkspacePaths.from_root(tmp_path)
    assert paths.resolve("base.css") == tmp_path / "base.css"
    assert paths.resolve("deck.md") == tmp_path / "deck.md"
    assert paths.resolve("slides/slide_01.html") == tmp_path / "slides" / "slide_01.html"


def test_resolve_rejects_workspace_escape(tmp_path: Path) -> None:
    WorkspaceState(tmp_path)
    paths = WorkspacePaths.from_root(tmp_path)
    with pytest.raises(WorkspacePathError):
        paths.resolve("../escape")
    with pytest.raises(WorkspacePathError):
        paths.resolve(tmp_path.parent / "outside.txt")


# ---------------------------------------------------------- shared Skill object


def test_two_decks_pin_the_same_skill_object(tmp_path: Path) -> None:
    studio = tmp_path / "studio"
    env = {"MURAL_STUDIO_DATA_DIR": str(studio)}
    store = SharedSkillStore(studio)
    refs = []
    for name in ("deck-a", "deck-b"):
        root = tmp_path / name
        WorkspaceState(root)
        paths = WorkspacePaths.from_root(root)
        refs.append(store.pin(paths, SKILL_SOURCE, skill_name=SKILL_NAME, environment=env))
    assert refs[0].tree_sha256 == refs[1].tree_sha256
    assert refs[0].object_root == refs[1].object_root
    objects = list((studio / "shared" / "skills" / "objects").iterdir())
    assert len([p for p in objects if p.is_dir()]) == 1


def test_publish_object_is_idempotent_under_concurrent_publish(tmp_path: Path) -> None:
    # BUG-L 回归:两个 deck 并发 fresh 启动时都会 stage 同一 tree_hash 并各自
    # ``pending.rename(target)``。内容寻址下 target 已被对方原子落好(非空目录),
    # 第二个 rename 在 Linux 抛 ENOTEMPTY(39) —— 不是 FileExistsError(17)。修前
    # 它逃出 except 直接崩;修后应视为幂等成功,object 树保持完好可校验。
    studio = tmp_path / "studio"
    store = SharedSkillStore(studio)
    from runtime.release_guard import release_tree_digest

    tree_hash = release_tree_digest((SKILL_SOURCE,), relative_to=SKILL_SOURCE)
    # 先由“胜出的 worker”把对象原子落盘。
    store._publish_object(SKILL_SOURCE, tree_hash)
    target = store.objects / tree_hash
    assert target.is_dir()

    # “落败的 worker”对同一 hash 再次发布 —— 必须幂等成功,不得抛 ENOTEMPTY。
    store._publish_object(SKILL_SOURCE, tree_hash)

    # 对象仍唯一且通过完整 hash/size/inventory 校验。
    assert store.verify_object(tree_hash).is_file()
    objects = [p for p in store.objects.iterdir() if p.is_dir()]
    assert objects == [target]



@pytest.mark.parametrize("conflict", [False, True])
def test_release_publish_race_checks_identity_not_timestamp(tmp_path, monkeypatch, conflict):
    from runtime import shared_objects

    store = SharedSkillStore(tmp_path / "studio")
    path = tmp_path / "release.json"
    atomic_json = shared_objects._atomic_json

    def raced_publish(target, value, **kwargs):
        winner = {**value, "created_at": "2000-01-01T00:00:00Z"}
        if conflict:
            winner["tree_sha256"] = "different-tree"
        atomic_json(target, winner, **kwargs)
        return atomic_json(target, value, **kwargs)

    monkeypatch.setattr(shared_objects, "_atomic_json", raced_publish)
    args = dict(skill_name=SKILL_NAME, release="test-race", tree_hash="tree",
                object_manifest_sha256="manifest")
    if conflict:
        with pytest.raises(SharedObjectError, match="conflicts at tree_sha256"):
            store._publish_release_record(path, **args)
    else:
        result = store._publish_release_record(path, **args)
        assert result["created_at"] == "2000-01-01T00:00:00Z"
        assert result["tree_sha256"] == "tree"
    assert json.loads(path.read_text())["created_at"] == "2000-01-01T00:00:00Z"


def test_pinned_object_survives_mutation_as_explicit_failure(tmp_path: Path) -> None:
    studio = tmp_path / "studio"
    env = {"MURAL_STUDIO_DATA_DIR": str(studio)}
    store = SharedSkillStore(studio)
    root = tmp_path / "deck"
    WorkspaceState(root)
    paths = WorkspacePaths.from_root(root)
    ref = store.pin(paths, SKILL_SOURCE, skill_name=SKILL_NAME, environment=env)
    # corrupt a pinned object file → verify must fail, never fall back to current.
    target = ref.object_root / "SKILL.md"
    target.chmod(0o644)
    target.write_text("mutated", encoding="utf-8")
    with pytest.raises(SharedObjectError):
        store.load_ref(paths, expected_skill_name=SKILL_NAME)


def test_pinned_object_missing_is_explicit_failure(tmp_path: Path) -> None:
    studio = tmp_path / "studio"
    env = {"MURAL_STUDIO_DATA_DIR": str(studio)}
    store = SharedSkillStore(studio)
    root = tmp_path / "deck"
    WorkspaceState(root)
    paths = WorkspacePaths.from_root(root)
    store.pin(paths, SKILL_SOURCE, skill_name=SKILL_NAME, environment=env)
    # point the pinned ref at a non-existent object hash.
    ref_value = json.loads(paths.skill_ref.read_text(encoding="utf-8"))
    ref_value["tree_sha256"] = "0" * 64
    paths.skill_ref.write_text(json.dumps(ref_value), encoding="utf-8")
    with pytest.raises(SharedObjectError):
        store.load_ref(paths, expected_skill_name=SKILL_NAME)


def test_author_workspace_has_no_skills_directory(tmp_path: Path) -> None:
    studio = tmp_path / "studio"
    env = {"MURAL_STUDIO_DATA_DIR": str(studio)}
    store = SharedSkillStore(studio)
    root = tmp_path / "deck"
    WorkspaceState(root)
    paths = WorkspacePaths.from_root(root)
    store.pin(paths, SKILL_SOURCE, skill_name=SKILL_NAME, environment=env)
    assert not (root / "skills").exists()
    assert paths.skill_ref.is_file()


@pytest.mark.parametrize("relative", ["_state", "_trace", "_state/workspace.json"])
def test_private_path_symlinks_are_rejected(tmp_path, relative):
    workspace = tmp_path / "deck"
    WorkspaceState(workspace)
    path = workspace / relative
    saved = tmp_path / path.name
    path.rename(saved)
    path.symlink_to(saved, target_is_directory=saved.is_dir())
    with pytest.raises(WorkspacePathError, match="unsupported_workspace_layout"):
        WorkspacePaths.from_root(workspace)


def test_production_does_not_reintroduce_legacy_product_paths():
    import ast
    roots = [REPOSITORY / "harnesses/mural-presenter-v1", SKILL_SOURCE / "scripts",
             REPOSITORY / "webui/studio/app"]
    forbidden = ("plan/", "materials/", "tmp/", "_runtime/tmp/compat/")
    violations = []
    for source_root in roots:
        for path in source_root.rglob("*.py"):
            if "tests" in path.parts or path.name in {"workspace_paths.py", "workspace_layout.py"}:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docstrings = {id(node.value) for node in ast.walk(tree)
                          if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)}
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and id(node) not in docstrings and node.value.startswith(forbidden)):
                    violations.append(f"{path.relative_to(REPOSITORY)}:{node.lineno}: {node.value}")
                if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
                        and isinstance(node.left, ast.Name) and node.left.id in {"root", "workspace", "workspace_root"}
                        and isinstance(node.right, ast.Constant) and node.right.value in {"plan", "materials", "tmp", "assets/fonts"}):
                    violations.append(f"{path.relative_to(REPOSITORY)}:{node.lineno}: forbidden root path")
    assert not violations, "\n".join(violations)


def test_cleanup_preserves_unknown_and_running_system_scratch(tmp_path):
    import runpy
    WorkspaceState(tmp_path)
    policy = runpy.run_path(str(SKILL_SOURCE / "scripts/_internal/workspace_policy.py"))
    scratch = tmp_path / "_runtime/tmp"
    system = scratch / "system"
    system.mkdir()
    (system / "active.bin").write_bytes(b"still in use")
    (scratch / "unknown.bin").write_bytes(b"unknown evidence")
    stale = scratch / "preview_slide_01.abcdefghi.html"
    stale.write_text("interrupted renderer")
    moved = policy["clean_workspace"](tmp_path)
    assert moved == ["_runtime/tmp/" + stale.name]
    assert (system / "active.bin").read_bytes() == b"still in use"
    assert (scratch / "unknown.bin").read_bytes() == b"unknown evidence"
    assert not stale.exists()
    assert any("unknown.bin" in issue for issue in policy["audit_workspace"](tmp_path))
