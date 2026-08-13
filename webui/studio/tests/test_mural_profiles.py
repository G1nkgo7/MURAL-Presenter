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
