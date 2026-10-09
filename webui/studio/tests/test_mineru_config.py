"""MinerU 只读统一 runtime JSON，WebUI 不从 Provider dotenv 注入旧参数。"""
import asyncio
import json
import sys
from unittest import mock

import pytest

from app import service_config


def test_old_mineru_endpoints_are_not_forwarded_from_provider_environment(tmp_path, monkeypatch):
    source = tmp_path / "deployment.env"
    endpoints = "http://gpu.example:8200,http://127.0.0.1:8201"
    source.write_text(f"MURAL_MINERU_ENDPOINTS={endpoints}\n"
                      "MURAL_MINERU_URL=http://retired.example:8000/\nUNRELATED=private\n")
    monkeypatch.setattr(service_config, "PROVIDER_ENV_PATH", source)
    monkeypatch.setenv("MURAL_MINERU_ENDPOINTS", "http://ambient.example:8200,http://ambient.example:8201")
    monkeypatch.setenv("MURAL_MINERU_URL", "http://ambient.example:8000")
    assert service_config.system_runtime_env() == {}


@pytest.mark.parametrize("configured", ["", "MURAL_MINERU_ENDPOINTS=\n"])
def test_unconfigured_mineru_does_not_fall_back_to_old_or_ambient_values(tmp_path, monkeypatch, configured):
    source = tmp_path / "deployment.env"
    source.write_text(configured + "MURAL_MINERU_URL=http://retired.example:8000/\n")
    monkeypatch.setattr(service_config, "PROVIDER_ENV_PATH", source)
    monkeypatch.setenv("MURAL_MINERU_ENDPOINTS", "http://ambient.example:8200,http://ambient.example:8201")
    monkeypatch.setenv("MURAL_MINERU_URL", "http://ambient.example:8000")
    assert service_config.system_runtime_env() == {}


@pytest.mark.parametrize("entrypoint", ["job", "doctor"])
@pytest.mark.parametrize("configured", [None, "", "http://gpu.example:8200,http://127.0.0.1:8201"])
def test_launch_preserves_only_runtime_pointer_and_reads_provider_configuration_once(tmp_path, monkeypatch, entrypoint, configured):
    from app import engine, environment_checks, jobs

    source = tmp_path / "deployment.env"
    source.write_text("" if configured is None else f"MURAL_MINERU_ENDPOINTS={configured}\n")
    monkeypatch.setattr(service_config, "PROVIDER_ENV_PATH", source)
    monkeypatch.setattr(service_config, "CONFIG_DIR", tmp_path / "user-configs")
    monkeypatch.setenv("MURAL_MINERU_ENDPOINTS", "http://ambient.example:8200,http://ambient.example:8201")
    monkeypatch.setenv("MURAL_MINERU_URL", "http://retired.example:8000")
    pointer = tmp_path / "runtime.json"
    pointer.write_text(json.dumps(service_config.load_runtime_config({})))
    monkeypatch.setenv("MURAL_RUNTIME_CONFIG", str(pointer))
    retired_capacity = ("MURAL_RESOURCE_SLOTS_DIR", "MODEL_CONCURRENCY", "SLIDE_CONCURRENCY",
                        "RENDER_CONCURRENCY", "PPT_RENDER_CONCURRENCY", "STUDIO_MAX_PER_MODEL", "STUDIO_MAX_CONCURRENT")
    for key in retired_capacity:
        monkeypatch.setenv(key, "999")
    monkeypatch.setattr(engine, "selection_env", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(engine, "external_visual_critic_env", lambda *_args: {})
    monkeypatch.setattr(engine, "render_env", lambda env: {
        **env, "PPTAGENT_ENGINE_PYTHON": sys.executable, "NORMALIZE_PY": sys.executable,
    })
    dispatched = []

    async def capture_launch(*_args, **kwargs):
        dispatched.append(kwargs["env"])
        raise OSError("test stopped before process launch")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_launch)
    with mock.patch.object(service_config, "read_env_file", wraps=service_config.read_env_file) as read_shared:
        if entrypoint == "job":
            model = "mineru-config-test"
            monkeypatch.setitem(engine.MODELS, model, {"backend": "openai", "engine_model": model})
            row = {"status": "queued", "run_dir": str(tmp_path), "user_id": 1,
                   "seed_json": json.dumps({}), "model": model,
                   "pipeline": "mural-presenter-v1-harness", "skill_version": "mural-presenter-v1"}
            monkeypatch.setattr(jobs, "_load_deck", lambda _: row)
            monkeypatch.setattr(jobs, "_set_status", lambda *_args, **_kwargs: None)
            failures = []
            monkeypatch.setattr(jobs, "_fail_deck", lambda *args: failures.append(args))
            monkeypatch.setattr(engine, "build_job", lambda *_args, **_kwargs: {"skill": {}})
            monkeypatch.setattr(engine, "write_job_file", lambda *_args: tmp_path / "job.json")
            monkeypatch.setattr(engine, "log_path", lambda _: tmp_path / "job.log")
            monkeypatch.setattr(engine, "runner_cmd", lambda _: ["unused-test-command"])
            asyncio.run(jobs._process(1))
            assert len(failures) == 1 and "test stopped before process launch" in failures[0][1]
        else:
            monkeypatch.setattr(environment_checks, "REPORT_DIR", tmp_path / "reports")
            monkeypatch.setattr(engine, "_engine_python", lambda: sys.executable)
            with pytest.raises(OSError, match="test stopped before process launch"):
                asyncio.run(environment_checks.run(user_id=1, model_key="test", model_label="Test",
                    pipeline_key="mural-presenter-v1-harness", skill_key="mural-presenter-v1"))
    read_shared.assert_called_once_with(source)
    assert len(dispatched) == 1
    mineru_env = {key: value for key, value in dispatched[0].items() if key.startswith("MURAL_MINERU_")}
    assert mineru_env == {}
    assert dispatched[0]["MURAL_RUNTIME_CONFIG"] == str(pointer)
    assert not any(key in dispatched[0] for key in retired_capacity)


def test_generation_settings_do_not_read_deployment_services(tmp_path, monkeypatch):
    monkeypatch.setattr(service_config, "CONFIG_DIR", tmp_path)
    with mock.patch.object(service_config, "read_env_file") as read_shared:
        assert service_config.generation_limits(1)["max_tokens"] == service_config.DEFAULT_MAX_TOKENS
        assert service_config.runtime_env(1)["MAX_TOKENS"] == str(service_config.DEFAULT_MAX_TOKENS)
        assert service_config.title_task_model_key(1) == service_config.DEFAULT_TITLE_TASK_MODEL
    read_shared.assert_not_called()


@pytest.mark.parametrize("mode,live_probes", [("local", False), ("local_and_services", True)])
def test_mineru_probe_metadata_survives_public_report(mode, live_probes):
    from app import environment_checks

    report = environment_checks._public_report({
        "checked": True, "status": "pass", "mode": mode, "live_probes": live_probes,
        "checks": [],
    }, model_key="test", model_label="Test", runtime_python=sys.executable)
    assert report["ready"] is True
    assert report["mode"] == mode and report["live_probes"] is live_probes
