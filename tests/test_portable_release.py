import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from runtime_environment import prepare_environment


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    for key in ["MURAL_RUNTIME_CONFIG", "MURAL_SKILL_DIR", "MURAL_RELEASE_MANIFEST"]:
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key, raising=False)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/runtime.json").write_bytes((ROOT / "configs/runtime.json").read_bytes())
    return tmp_path


def test_local_config_is_portable_and_preserves_operator_edits(deployment):
    local = prepare_environment(deployment)
    value = json.loads(local.read_text())
    assert value["resources"]["directory"] == str(deployment / "webui/studio/data/resource-slots")
    value["decks"]["total"] = 24
    local.write_text(json.dumps(value))
    os.environ.pop("MURAL_RUNTIME_CONFIG")
    assert prepare_environment(deployment) == local
    assert json.loads(local.read_text())["decks"]["total"] == 24


def test_explicit_configuration_is_not_overwritten(deployment, monkeypatch):
    explicit = deployment / "operator.json"
    explicit.write_text('{"operator":"owned"}')
    monkeypatch.setenv("MURAL_RUNTIME_CONFIG", str(explicit))
    assert prepare_environment(deployment) == explicit
    assert explicit.read_text() == '{"operator":"owned"}'
    assert not (deployment / "configs/runtime.local.json").exists()


def test_resolved_config_satisfies_frozen_runtime(deployment):
    prepare_environment(deployment)
    spec = importlib.util.spec_from_file_location("paper_test_runtime_config", ROOT / "harnesses/mural-presenter-v1/runtime/config.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    config = module.load_runtime_config()
    assert config["decks"]["per_model"] <= config["decks"]["total"]
    assert Path(config["resources"]["directory"]).is_relative_to(deployment)


def test_webui_dispatches_current_runtime_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("STUDIO_DATA_DIR", str(tmp_path / "studio"))
    monkeypatch.setenv("MURAL_SECRETS_ENV_PATH", str(tmp_path / "no-secrets.env"))
    monkeypatch.setenv("MURAL_RUNTIME_CONFIG", str(tmp_path / "operator-runtime.json"))
    monkeypatch.setenv("MURAL_SKILL_DIR", str(ROOT / "skills/mural-presenter-v1"))
    sys.path.insert(0, str(ROOT / "webui/studio"))
    from app import engine
    skill = engine._mural_v1_skill()
    assert skill["ready"], skill.get("unavailable_reason")
    assert Path(skill["harness_path"]) == ROOT / "harnesses/mural-presenter-v1"
    assert Path(skill["path"]) == ROOT / "skills/mural-presenter-v1"
    assert skill["runtime_env"]["MURAL_RUNTIME_CONFIG"] == str(tmp_path / "operator-runtime.json")
