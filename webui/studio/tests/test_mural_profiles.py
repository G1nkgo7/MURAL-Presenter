import json
import os
from types import SimpleNamespace
from unittest import mock

import pytest

from app import engine
from app import jobs


def test_creative_profile_uses_the_same_runtime_with_an_explicit_seed_lock(tmp_path):
    stable = engine.SKILLS["mural-presenter"]
    creative = engine.SKILLS["mural-presenter-creative"]

    assert stable["ready"], stable["unavailable_reason"]
    assert creative["ready"], creative["unavailable_reason"]
    assert stable["pipeline"] == creative["pipeline"] == "mural-presenter-harness"
    assert "authoring_profile" not in stable
    assert creative["runtime_env"] == {"MURAL_AUTHORING_PROFILE": "creative"}

    job = engine.build_job(
        "creative-profile-test",
        {"query": "Make an 8-page research talk", "slide_count": 8},
        tmp_path / "creative-profile-test",
        dry=True,
        model_key="test-model",
        model_config={"engine_model": "test-model"},
        skill_key="mural-presenter-creative",
    )

    assert job["skill_version"] == "mural-presenter-creative"
    assert job["pipeline_version"] == "mural-presenter-harness"
    assert job["seed"]["authoring_profile"] == "creative"
    assert job["seed"]["pages_hint"] == 8


def test_job_launcher_drops_ambient_authoring_profile():
    source = open(jobs.__file__, encoding="utf-8").read()
    drop_block = source[source.index("_DROP = ("):source.index("child_env =", source.index("_DROP = ("))]
    assert '"MURAL_AUTHORING_PROFILE"' in drop_block


def test_v02_registers_the_single_slide_runtime(tmp_path):
    skill = engine.SKILLS["mural-presenter-v0.2"]
    pipeline = engine.PIPELINES["mural-presenter-v0.2-harness"]

    assert skill["ready"], skill["unavailable_reason"]
    assert skill["pairing"] == pipeline["pairing"] == "mural-presenter-v0.2-single-slide"
    assert skill["runtime_env"]["CLEAN_CHILD_CONCURRENCY"] == "12"
    assert skill["runtime_env"]["CLEAN_CHILD_POOL_MAX_WORKERS"] == "12"
    assert skill["runtime_env"]["CLEAN_MAX_TOKENS"] == "40960"

    job = engine.build_job(
        "v02-profile-test",
        {"query": "制作一份 8 页论文演示", "slide_count": 8},
        tmp_path / "v02-profile-test",
        dry=True,
        model_key="test-model",
        model_config={"engine_model": "test-model"},
        skill_key="mural-presenter-v0.2",
    )

    assert job["skill_version"] == "mural-presenter-v0.2"
    assert job["pipeline_version"] == "mural-presenter-v0.2-harness"
    assert job["seed"]["pages_hint"] == 8


def test_v02_grouped_uses_the_same_harness_with_grouped_skill_names(tmp_path):
    skill = engine.SKILLS["mural-presenter-v0.2-grouped"]
    pipeline = engine.PIPELINES["mural-presenter-v0.2-grouped-harness"]

    assert skill["ready"], skill["unavailable_reason"]
    assert skill["harness_path"] == engine.SKILLS["mural-presenter-v0.2"]["harness_path"]
    assert skill["pairing"] == pipeline["pairing"] == "mural-presenter-v0.2-grouped"
    assert skill["runtime_env"]["CLEAN_SKILL_NAME_ZH"] == (
        "mural-presenter-v0-2-grouped-zh"
    )
    assert skill["runtime_env"]["CLEAN_SKILL_NAME_EN"] == (
        "mural-presenter-v0-2-grouped-en"
    )
    assert skill["runtime_env"]["CLEAN_CHILD_CONCURRENCY"] == "12"

    job = engine.build_job(
        "v02-grouped-profile-test",
        {"query": "制作一份 8 页论文演示", "slide_count": 8},
        tmp_path / "v02-grouped-profile-test",
        dry=True,
        model_key="test-model",
        model_config={"engine_model": "test-model"},
        skill_key="mural-presenter-v0.2-grouped",
    )

    assert job["skill_version"] == "mural-presenter-v0.2-grouped"
    assert job["pipeline_version"] == "mural-presenter-v0.2-grouped-harness"
    assert job["skill"]["runtime_env"]["CLEAN_SKILL_NAME_ZH"] == (
        "mural-presenter-v0-2-grouped-zh"
    )


def test_v03_registers_one_adaptive_skill_and_harness(tmp_path):
    skill = engine.SKILLS["mural-presenter-v0.3"]
    pipeline = engine.PIPELINES["mural-presenter-v0.3-harness"]

    assert skill["ready"], skill["unavailable_reason"]
    assert skill["pairing"] == pipeline["pairing"] == "mural-presenter-v0.3-adaptive"
    assert skill["name"] == "mural-presenter-v0-3"
    assert skill["instruction_paths"]["zh"] == skill["instruction_paths"]["en"]
    assert skill["runtime_env"]["CLEAN_MODEL_SELECT_SKILL"] == "0"
    assert skill["runtime_env"]["CLEAN_SKILL_NAME_ZH"] == "mural-presenter-v0-3"
    assert skill["runtime_env"]["CLEAN_SKILL_NAME_EN"] == "mural-presenter-v0-3"
    assert skill["runtime_env"]["CLEAN_CHILD_CONCURRENCY"] == "12"
    assert not any(path.endswith("/scripts/deck.py") for path in skill["required_files"])
    for relative in (
        "scripts/orchestrator.py",
        "scripts/image.py",
        "scripts/slide.py",
        "scripts/review.py",
        "scripts/_internal/bootstrap.py",
        "scripts/_internal/deck_core.py",
    ):
        assert f"mural-presenter-v0-3/{relative}" in skill["required_files"]

    job = engine.build_job(
        "v03-profile-test",
        {"query": "制作一份 8 页论文演示", "slide_count": 8},
        tmp_path / "v03-profile-test",
        dry=True,
        model_key="test-model",
        model_config={"engine_model": "test-model"},
        skill_key="mural-presenter-v0.3",
    )
    assert job["skill_version"] == "mural-presenter-v0.3"
    assert job["pipeline_version"] == "mural-presenter-v0.3-harness"


def test_v04_registers_paired_quality_iteration(tmp_path):
    skill = engine.SKILLS["mural-presenter-v0.4"]
    pipeline = engine.PIPELINES["mural-presenter-v0.4-harness"]

    assert skill["ready"], skill["unavailable_reason"]
    assert skill["pairing"] == pipeline["pairing"] == "mural-presenter-v0.4-adaptive"
    assert skill["name"] == "mural-presenter-v0-4"
    assert skill["runtime_env"]["CLEAN_CHILD_CONCURRENCY"] == "12"
    assert "mural-presenter-v0-4/assets/vendor/echarts.min.js" in skill["required_files"]
    assert "mural-presenter-v0-4/scripts/_internal/cli.py" in skill["required_files"]

    job = engine.build_job(
        "v04-profile-test",
        {"query": "制作一份 8 页论文演示", "slide_count": 8},
        tmp_path / "v04-profile-test",
        dry=True,
        model_key="test-model",
        model_config={"engine_model": "test-model"},
        skill_key="mural-presenter-v0.4",
    )
    assert job["skill_version"] == "mural-presenter-v0.4"
    assert job["pipeline_version"] == "mural-presenter-v0.4-harness"


def test_second_deployment_model_uses_its_own_endpoint_and_thinking_transport(
    monkeypatch,
):
    prefix = "SENSENOVA_MODEL2"
    monkeypatch.setenv(f"{prefix}_BASE_URL", "https://model.example.test/v1/")
    monkeypatch.setenv(f"{prefix}_NAME", "pptagent-test")
    monkeypatch.setenv(f"{prefix}_DISPLAY_NAME", "PPTAgent 2")
    monkeypatch.setenv(f"{prefix}_SLIDE_CONCURRENCY", "4")
    monkeypatch.setenv(f"{prefix}_THINKING_TRANSPORT", "chat_template_kwargs")

    model = engine._environment_model(prefix)

    assert model is not None
    assert model["label"] == "PPTAgent 2"
    assert model["engine_model"] == "pptagent-test"
    assert model["base_url"] == "https://model.example.test/v1"
    assert model["thinking_transport"] == "chat_template_kwargs"
    assert model["slide_concurrency"] == 4


def test_v04_uses_broker_owning_clean_runner(monkeypatch):
    from pathlib import Path
    import importlib.util

    serve_one = Path(__file__).resolve().parents[2] / "inference" / "serve_one.py"
    monkeypatch.syspath_prepend(str(serve_one.parent))
    spec = importlib.util.spec_from_file_location("v04_serve_one_test", serve_one)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert module._uses_clean_runner({
        "pipeline": {"skill_mode": "mural-presenter-v0.4-adaptive"},
    })


def test_sense_present_suite_uses_entry_skill_and_paired_adapter(tmp_path):
    skill = engine.SKILLS["sense-present-ppt-skill"]
    pipeline = engine.PIPELINES["sense-present-ppt-skill-harness"]

    assert skill["ready"], skill["unavailable_reason"]
    assert skill["name"] == "sn-ppt-entry"
    assert skill["pairing"] == pipeline["pairing"] == "sense-present-ppt-skill-suite"
    assert skill["skills_root"].endswith("/skills/sense-present-ppt-skill/skills")
    assert pipeline["entry"] == "sense_present_suite.py"

    job = engine.build_job(
        "sense-present-suite-test",
        {"query": "制作一页静态演示", "slide_count": 1},
        tmp_path / "sense-present-suite-test",
        dry=True,
        model_key="test-model",
        model_config={"engine_model": "test-model"},
        skill_key="sense-present-ppt-skill",
    )
    assert job["skill_version"] == "sense-present-ppt-skill"
    assert job["pipeline_version"] == "sense-present-ppt-skill-harness"
    assert job["seed"]["pages_hint"] == 1


def test_v02_rejects_text_only_main_model_before_generation():
    model = {
        "label": "DeepSeek V4 Flash",
        "backend": "openai",
        "engine_model": "deepseek-v4-flash",
        "base_url": "https://tokenhub.sensetime.com/v1",
        "api_key": "test-only",
        "multimodal": False,
        "thinking_mode": "none",
    }
    error = engine.validate_selection(
        "custom:test",
        "mural-presenter-v0.2-harness",
        "mural-presenter-v0.2",
        model_config=model,
    )
    assert "文本接口" in error
    env = engine.selection_env(
        "custom:test",
        "mural-presenter-v0.2-harness",
        "mural-presenter-v0.2",
        model_config=model,
    )
    assert env["VISION_BACKEND"] == "disabled"


def test_v03_multimodal_model_uses_isolated_same_model_vision_critic():
    model = {
        "label": "PPTAgent Multimodal",
        "backend": "openai",
        "engine_model": "pptagent-multimodal",
        "base_url": "https://tokenhub.sensetime.com/v1",
        "api_key": "test-only",
        "multimodal": True,
        "thinking_mode": "none",
    }
    env = engine.selection_env(
        "custom:test",
        "mural-presenter-v0.3-harness",
        "mural-presenter-v0.3",
        model_config=model,
    )
    assert env["VISION_BACKEND"] == "same_model_aux"


@pytest.mark.parametrize("skill_version", ["mural-presenter-v0.3", "mural-presenter-v0.4"])
def test_role_split_delivery_recovery_uses_role_entry_not_deck_py(
    tmp_path, skill_version
):
    slides = tmp_path / "slides"
    slides.mkdir()
    (slides / "slide_01.html").write_text("<section>ok</section>", encoding="utf-8")
    row = {
        "id": 1,
        "seed_json": "{}",
        "skill_version": skill_version,
    }
    calls = []

    def completed(command, **kwargs):
        calls.append(command)
        if command[2] == "finalize":
            (tmp_path / "present.html").write_text("<html>built</html>", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="status:PASS", stderr="")

    with mock.patch.object(jobs, "_run_noninteractive", side_effect=completed):
        assert jobs._ensure_static_delivery(row, tmp_path) is None

    assert [command[2] for command in calls] == ["finalize", "audit"]
    assert all(command[1].endswith("/scripts/orchestrator.py") for command in calls)
    assert all("deck.py" not in " ".join(command) for command in calls)


@pytest.mark.parametrize("skill_version", ["mural-presenter-v0.3", "mural-presenter-v0.4"])
def test_role_split_delivery_rebuilds_a_stale_formal_player(
    tmp_path, skill_version
):
    slides = tmp_path / "slides"
    slides.mkdir()
    slide = slides / "slide_01.html"
    present = tmp_path / "present.html"
    (tmp_path / "base.css").write_text(".slide { width: 1600px; }", encoding="utf-8")
    present.write_text("<html>old</html>", encoding="utf-8")
    slide.write_text('<section class="slide" data-slide="1">new</section>', encoding="utf-8")
    os.utime(present, ns=(1_000_000_000, 1_000_000_000))
    os.utime(slide, ns=(2_000_000_000, 2_000_000_000))
    row = {
        "id": 1,
        "seed_json": "{}",
        "skill_version": skill_version,
    }
    calls = []

    def completed(command, **kwargs):
        calls.append(command[2])
        if command[2] == "finalize":
            present.write_text("<html>fresh</html>", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="status:PASS", stderr="")

    with mock.patch.object(jobs, "_run_noninteractive", side_effect=completed):
        assert jobs._ensure_static_delivery(row, tmp_path) is None

    assert calls == ["finalize", "audit"]


def test_failed_engine_result_is_not_promoted_by_stale_pngs(tmp_path):
    (tmp_path / "renders").mkdir()
    (tmp_path / "renders" / "slide_01.png").write_bytes(b"stale-render")
    (tmp_path / "result.json").write_text(
        json.dumps({
            "status": "failed",
            "reason": "unresolved Review failure",
            "exit_reason": "stalled_repetition",
        }),
        encoding="utf-8",
    )
    row = {"id": 30, "seed_json": "{}", "status": "running"}
    with (
        mock.patch.object(jobs, "_load_deck", return_value=row),
        mock.patch.object(jobs, "_fail_deck") as fail,
        mock.patch.object(jobs, "_set_status") as set_status,
    ):
        jobs._finalize(30, tmp_path, rc=1)

    fail.assert_called_once()
    set_status.assert_not_called()


def test_needs_improvement_is_a_successful_usable_delivery(tmp_path):
    (tmp_path / "renders").mkdir()
    (tmp_path / "renders" / "slide_01.png").write_bytes(b"final-render")
    (tmp_path / "result.json").write_text(
        json.dumps(
            {
                "status": "needs_improvement",
                "reason": "bounded Review left a known visual issue",
            }
        ),
        encoding="utf-8",
    )
    row = {"id": 31, "seed_json": "{}", "status": "running"}
    with (
        mock.patch.object(jobs, "_load_deck", return_value=row),
        mock.patch.object(jobs, "_ensure_static_delivery", return_value=None),
        mock.patch.object(jobs, "_fail_deck") as fail,
        mock.patch.object(jobs, "_set_status") as set_status,
        mock.patch.object(jobs, "_settle_waiting_revisions"),
    ):
        jobs._finalize(31, tmp_path, rc=0)

    fail.assert_not_called()
    set_status.assert_called_once_with(
        31,
        "completed",
        slide_count=1,
        error=None,
        finished_at=mock.ANY,
    )
