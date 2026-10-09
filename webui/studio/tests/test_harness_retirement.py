"""公开包只暴露当前 V1；历史归档和内部批量入口保留在本地。"""
from pathlib import Path
import importlib.util
import subprocess

import pytest

from app import engine


ROOT = Path(__file__).resolve().parents[3]
RETIRED = (
    "mural-presenter-v0.6", "mural-presenter-v1.0",
    "visual-craft-v3-pre-speech", "visual-craft-v3-speech",
)


def test_active_harness_directory_has_one_runtime_and_no_compat_links():
    active = ROOT / "harnesses"
    directories = {path.name for path in active.iterdir() if path.is_dir() and not path.name.startswith(".")}
    assert directories == {"mural-presenter-v1"}
    for name in RETIRED:
        assert not (active / name).exists()
        assert not (active / name).is_symlink()


def test_active_skill_directory_and_default_ui_have_one_pair():
    active = ROOT / 'skills'
    assert {p.name for p in active.iterdir() if p.is_dir() and not p.name.startswith('.')} == {
        'mural-presenter-v1'}
    assert engine.PUBLIC_SKILL_KEYS == ('mural-presenter-v1',)
    assert engine.PUBLIC_PIPELINE_KEYS == ('mural-presenter-v1-harness',)
    assert engine.DEFAULT_SKILL == 'mural-presenter-v1'
    for name in ('mural-presenter-v0.6', 'sense-present-ppt-skill',
                 'visual-craft-v3-pre-speech', 'visual-craft-v3-speech'):
        assert not (active / name).exists()
        assert not (active / name).is_symlink()


def test_repository_scaffold_check_targets_current_harness():
    for relative in ("tools/check_paper_release.py", "scripts/run_inference.py",
                     "harnesses/mural-presenter-v1/runtime/agent.py",
                     "configs/releases/upstream-0919/publication.json"):
        assert (ROOT / relative).is_file()
    tracked = subprocess.check_output(["git", "ls-files", "--", "archive/"], cwd=ROOT, text=True)
    assert not tracked


@pytest.mark.parametrize('ready', (True, False))
def test_launcher_uses_formal_environment_and_packaged_fonts_only(monkeypatch, ready):
    path = ROOT / 'webui/scripts/launch.py'
    spec = importlib.util.spec_from_file_location('mural_launcher_retirement_test', path)
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    python = ROOT / 'webui/inference/.venv/bin/python'
    fonts = ROOT / 'fonts'
    checked = []
    monkeypatch.setattr(launcher, '_presenter_runtime_ready',
                        lambda py, directory: checked.append((py, directory)) or ready)
    if ready:
        assert launcher._prepare_presenter_runtime(python, fonts) == (python, fonts)
    else:
        with pytest.raises(SystemExit, match='Incomplete inference runtime or packaged fonts'):
            launcher._prepare_presenter_runtime(python, fonts)
    assert checked == [(python, fonts)]
    source = path.read_text()
    assert 'archive/mural-presenter-pre-v0.6' not in source
    assert 'harnesses/mural-presenter-v1.0' not in source
    assert 'fonts_dir = MURAL_PRESENTER_SUITE / "fonts"' in source


@pytest.mark.parametrize("skill", RETIRED[1:])
def test_retired_harness_cannot_build_a_new_job(skill, tmp_path):
    engine.refresh_external_skills(force=True)
    assert engine.SKILLS["mural-presenter-v1"]["ready"]
    assert skill not in engine.SKILLS
    with pytest.raises(ValueError, match="未知 skill"):
        engine.build_job("retired-test", {"query": "one page"}, tmp_path / "deck",
                         dry=True, skill_key=skill, model_key="test",
                         model_config={"engine_model": "test"})
    assert not (tmp_path / "deck").exists()


@pytest.mark.parametrize("profile", (
    "_shared-paper-synthesis", "opus5thinking-native-multimodal-paper-3000",
    "opus5thinking-native-multimodal-paper-edit-500",
))
def test_legacy_batch_entry_is_not_published(profile):
    relative = Path("configs/data_pipeline") / profile / "run.sh"
    tracked = subprocess.check_output(["git", "ls-files", "--", str(relative)], cwd=ROOT, text=True)
    assert not tracked
