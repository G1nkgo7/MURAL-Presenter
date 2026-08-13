from app import engine


def test_v02_registers_the_single_slide_runtime(tmp_path):
    skill = engine.SKILLS["mural-presenter-v0.2"]
    pipeline = engine.PIPELINES["mural-presenter-v0.2-harness"]

    assert skill["ready"], skill["unavailable_reason"]
    assert skill["pairing"] == pipeline["pairing"] == "mural-presenter-v0.2-single-slide"
    assert skill["runtime_env"]["CLEAN_CHILD_CONCURRENCY"] == "4"
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
