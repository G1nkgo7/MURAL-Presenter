from __future__ import annotations

import json
from pathlib import Path

import pytest

from runtime.release_guard import (
    integration_digest,
    release_tree_digest,
    verify_runtime_release,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _release_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    skill = root / "skills/mural-presenter-v1"
    harness = root / "harnesses/mural-presenter-v1"
    _write(skill / "SKILL.md", "canonical skill\n")
    _write(harness / "infer.py", "canonical harness\n")
    integration_files = [
        "webui/scripts/launch.py",
        "webui/inference/serve_one.py",
        "webui/studio/app/engine.py",
    ]
    for relative in integration_files:
        _write(root / relative, f"{relative}\n")
    release = {
        "release": "test-v1-release",
        "status": "frozen",
        "git": {"source_commit": "a" * 40},
        "skill": {
            "key": "mural-presenter-v1",
            "path": "skills/mural-presenter-v1",
            "tree_sha256": release_tree_digest((skill,), relative_to=skill),
        },
        "harness": {
            "key": "mural-presenter-harness-v1",
            "path": "harnesses/mural-presenter-v1",
            "tree_sha256": release_tree_digest((harness,), relative_to=harness),
        },
        "lineage": {"runtime_modes": ["inference", "synthesis"]},
        "integration": {
            "files": integration_files,
            "tree_sha256": integration_digest(root, integration_files),
        },
    }
    release["pair_tree_sha256"] = release_tree_digest(
        (skill, harness), relative_to=root
    )
    _write(
        root / "configs/releases/mural-paper-v1.json",
        json.dumps(release, indent=2),
    )
    return root


@pytest.mark.parametrize("mode", ["inference", "synthesis", "webui"])
def test_frozen_release_accepts_all_official_entrypoints(
    tmp_path: Path, mode: str
) -> None:
    root = _release_repo(tmp_path)
    report = verify_runtime_release(mode, repository_root=root)
    assert report["release"] == "test-v1-release"
    assert report["manifest"] == "configs/releases/mural-paper-v1.json"
    assert report["mismatches"] == {}
    assert not report["dirty_override"]


def test_production_rejects_source_drift(tmp_path: Path) -> None:
    root = _release_repo(tmp_path)
    _write(root / "skills/mural-presenter-v1/SKILL.md", "dirty skill\n")
    with pytest.raises(RuntimeError, match="hash mismatch"):
        verify_runtime_release("inference", repository_root=root)


def test_archive_contains_exact_runtime_source_set_not_public_scan_subset(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from tools import freeze_mural_release as freeze
    from runtime.release_guard import source_tree_files
    root = _release_repo(tmp_path)
    _write(root / "harnesses/mural-presenter-v1/runtime/session.py", "native session\n")
    _write(root / "harnesses/mural-presenter-v1/runtime/agent.py", "agent loop\n")
    _write(root / "harnesses/mural-presenter-v1/runtime/__pycache__/ignored.pyc", "generated\n")
    path = root / "configs/releases/mural-paper-v1.json"
    release = json.loads(path.read_text())
    monkeypatch.setattr(freeze, "ROOT", root)
    members = set(freeze.release_members(path, release))
    source_members = {item.relative_to(root).as_posix()
                      for kind in ("skill", "harness")
                      for item in source_tree_files(root / release[kind]["path"])}
    assert "harnesses/mural-presenter-v1/runtime/session.py" in members
    assert "harnesses/mural-presenter-v1/runtime/agent.py" in members
    assert not any("__pycache__" in item for item in members)
    assert members == source_members | set(release["integration"]["files"]) | {path.relative_to(root).as_posix()}


def test_development_requires_explicit_dirty_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _release_repo(tmp_path)
    _write(root / "harnesses/mural-presenter-v1/infer.py", "dirty harness\n")
    monkeypatch.setenv("MURAL_RELEASE_POLICY", "development")
    with pytest.raises(RuntimeError, match="MURAL_ALLOW_DIRTY_RELEASE"):
        verify_runtime_release("synthesis", repository_root=root)
    monkeypatch.setenv("MURAL_ALLOW_DIRTY_RELEASE", "1")
    report = verify_runtime_release("synthesis", repository_root=root)
    assert report["dirty_override"]
    assert set(report["mismatches"]) == {"harness", "pair"}


@pytest.mark.parametrize("relative", [
    "webui/inference/serve_one.py",
    "webui/studio/app/engine.py",
    "webui/scripts/launch.py",
])
def test_integration_drift_is_part_of_the_frozen_contract(tmp_path: Path, relative: str) -> None:
    root = _release_repo(tmp_path)
    _write(root / relative, "changed integration\n")
    with pytest.raises(RuntimeError, match="integration"):
        verify_runtime_release("webui", repository_root=root)


@pytest.mark.parametrize("mode", ["inference", "synthesis", "webui"])
@pytest.mark.parametrize("relative", [
    "webui/studio/static/app.js",
    "webui/studio/static/app.css",
    "webui/studio/static/logo.svg",
    "webui/studio/templates/app.html",
    "webui/RELEASE_MANIFEST.json",
])
def test_unfrozen_presentation_changes_do_not_block_generation(tmp_path: Path, mode: str, relative: str) -> None:
    root = _release_repo(tmp_path)
    _write(root / relative, "initial presentation\n")
    before = verify_runtime_release(mode, repository_root=root)
    _write(root / relative, "changed presentation\n")
    after = verify_runtime_release(mode, repository_root=root)
    assert after["actual"] == before["actual"]
    assert after["mismatches"] == {}
    assert not after["dirty_override"]


def test_generated_canary_data_does_not_change_the_runtime_digest(
    tmp_path: Path,
) -> None:
    root = _release_repo(tmp_path)
    _write(
        root / "harnesses/mural-presenter-v1/canary/runs/live/result.json",
        "first\n",
    )
    report = verify_runtime_release("inference", repository_root=root)
    assert report["mismatches"] == {}


def test_explicit_rollback_manifest_must_stay_inside_repository(
    tmp_path: Path,
) -> None:
    root = _release_repo(tmp_path)
    with pytest.raises(RuntimeError, match="escapes repository"):
        verify_runtime_release(
            "inference",
            repository_root=root,
            manifest_path=root.parent / "outside.json",
        )


def test_release_bundle_preserves_declared_webui_integration(tmp_path, monkeypatch):
    from tools import freeze_mural_release as freeze
    from tools.content_archive import write_tar_zst, verify_tar_zst
    root = _release_repo(tmp_path)
    monkeypatch.setattr(freeze, 'ROOT', root)
    path = root / 'configs/releases/mural-paper-v1.json'
    release = json.loads(path.read_text())
    members = freeze.release_members(path, release)
    assert set(release['integration']['files']).issubset(members)
    record = write_tar_zst(root, members, tmp_path / 'release.tar.zst')
    verified = verify_tar_zst(Path(record['path']), record['members'])
    assert verified['verified'] and verified['sha256'] == record['sha256']
    freeze.verify_source_members(path, release, record['members'])
    incomplete = [row for row in record['members'] if row['path'] != 'harnesses/mural-presenter-v1/infer.py']
    with pytest.raises(ValueError, match="source coverage/hash mismatch: harness"):
        freeze.verify_source_members(path, release, incomplete)
