from __future__ import annotations

from pathlib import Path

import pytest

from runtime.config import (
    RUN_PROFILES,
    ProviderSettings,
    RunMode,
    RunSpec,
    merge_environment,
    read_environment_file,
    resolve_authoring_profile,
    resolve_run_mode,
)


@pytest.mark.parametrize("model,efforts,default", [
    ("gpt-6-astra/azure/sfa", ("low", "medium", "high", "xhigh", "max"), "xhigh"),
    ("gpt-6-astra/azure_L/qwb", ("low", "medium", "high", "xhigh", "max"), "xhigh"),
    ("gpt-6-astra/mix_L/sfj", ("low", "medium", "high", "xhigh", "max"), "xhigh"),
    ("gpt-6-astra/azure/sfh", ("low", "medium", "high", "xhigh", "max"), "xhigh"),
    ("claude-opus-4-7-thinking", ("low", "medium", "high", "xhigh", "max"), "high"),
    ("claude-opus-5", ("low", "medium", "high", "xhigh", "max"), "high"),
    ("Qwen3.8-27B", ("low", "medium", "xhigh"), "xhigh"),
])
def test_effort_environment_preserves_model_specific_choices(model, efforts, default):
    from runtime.production import ProductionSettings
    for effort in efforts:
        settings = ProductionSettings.from_environment({"MODEL": model, "MODEL_THINKING": "1", "MODEL_REASONING_EFFORT": effort})
        assert settings.reasoning_effort == effort
    assert ProductionSettings.from_environment({"MODEL": model}).reasoning_effort == default


def test_kimi_native_effort_and_maintenance_contract():
    from runtime.config import maintenance_reasoning, model_effort_capability
    from runtime.production import ProductionSettings

    capability = model_effort_capability("kimi-k3")
    assert capability == {
        "levels": ("low", "high", "max"),
        "default": "max",
        "without_thinking": (),
        "transport": "reasoning_effort",
    }
    settings = ProductionSettings.from_environment({"MODEL": "kimi-k3"})
    assert settings.thinking is True
    assert settings.reasoning_effort == "max"
    for effort in capability["levels"]:
        assert ProductionSettings.from_environment({"MODEL": "kimi-k3", "MODEL_REASONING_EFFORT": effort}).reasoning_effort == effort
    for effort in ("medium", "xhigh"):
        with pytest.raises(ValueError, match="low, high, max"):
            ProductionSettings.from_environment({"MODEL": "kimi-k3", "MODEL_REASONING_EFFORT": effort})
    with pytest.raises(ValueError, match="always uses thinking"):
        ProductionSettings.from_environment({"MODEL": "kimi-k3", "MODEL_THINKING": "0"})
    assert maintenance_reasoning("KIMI-K3") == (True, "low")
    for model in ("gpt-6-astra/azure/sfa", "Qwen3.8-27B", "claude-opus-5"):
        assert maintenance_reasoning(model) == (False, "")


@pytest.mark.parametrize("model", ["glm-5.3", "glm-5.3-flash", "glm-5.3-flash/zai_L/sfc"])
def test_glm53_native_effort_and_maintenance_contract(model):
    from runtime.config import maintenance_reasoning, model_effort_capability
    from runtime.production import ProductionSettings
    from runtime.session import ModelRequest, openai_chat_body

    assert model_effort_capability(model)["levels"] == ("low", "high", "max")
    assert maintenance_reasoning(model.upper()) == (True, "low")
    assert ProviderSettings(api_format="openai", model=model, base_url="https://provider.test").reasoning_transport == "glm"
    for effort in ("low", "high", "max"):
        settings = ProductionSettings.from_environment({"MODEL": model, "MODEL_REASONING_EFFORT": effort})
        request = ModelRequest(model=model, system="", messages=(), thinking=settings.thinking, effort=effort)
        body = openai_chat_body(request)
        assert body["thinking"] == ({"type": "enabled"} if model == "glm-5.3"
                                    else {"type": "enabled", "clear_thinking": False})
        assert body["reasoning_effort"] == effort
    for effort in ("medium", "xhigh"):
        with pytest.raises(ValueError, match="low, high, max"):
            ProductionSettings.from_environment({"MODEL": model, "MODEL_REASONING_EFFORT": effort})
    with pytest.raises(ValueError, match="always uses thinking"):
        ProductionSettings.from_environment({"MODEL": model, "MODEL_THINKING": "0"})
    with pytest.raises(ValueError, match="Chat Completions"):
        ModelRequest(model=model, system="", messages=(), thinking=True, effort="high").anthropic_kwargs(
            ProviderSettings(api_format="anthropic", model=model, base_url="https://provider.test"))


def test_glm53flash_preserves_native_images_and_reasoning_in_tool_history():
    from runtime.session import ModelRequest, openai_chat_body
    from io import BytesIO
    from PIL import Image
    import base64

    pixels = BytesIO()
    Image.new("RGB", (16, 16), "red").save(pixels, format="PNG")
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png",
              "data": base64.b64encode(pixels.getvalue()).decode()}}
    messages = ({"role": "assistant", "content": None, "reasoning_content": "actual returned reasoning",
                 "tool_calls": [{"id": "call_01", "type": "function", "function": {
                    "name": "inspect", "arguments": '{"page": 1}'}}]},
                {"role": "tool", "tool_call_id": "call_01", "content": "rendered"},
                {"role": "user", "content": [image]})
    body = openai_chat_body(ModelRequest(model="glm-5.3-flash/zai_L/sfc", system="", messages=messages,
                                       thinking=True, effort="high"))
    assert body["messages"][0]["reasoning_content"] == "actual returned reasoning"
    assert body["messages"][0]["tool_calls"][0]["id"] == body["messages"][1]["tool_call_id"]
    assert body["messages"][2]["content"][0]["type"] == "image_url"


@pytest.mark.parametrize("timeout,expected", [(0, 600.0), (75, 75.0)])
def test_provider_request_budget_and_legacy_kimi_transport(timeout, expected):
    from runtime.config import provider_request_budget_s
    settings = ProviderSettings(api_format="openai", model="kimi-k3", base_url="https://provider.test",
                                total_timeout_s=timeout, reasoning_transport="chat_template_kwargs")
    assert settings.reasoning_transport == "reasoning_effort"
    assert provider_request_budget_s(settings) == expected


@pytest.mark.parametrize("model,thinking,effort", [
    ("Qwen3.8-27B", "1", "high"), ("Qwen3.8-27B", "1", "max"),
    ("claude-opus-5", "0", "xhigh"), ("claude-opus-5", "0", "max"),
    ("claude-opus-4-7-thinking", "1", "unsupported"),
])
def test_effort_environment_rejects_unsupported_settings(model, thinking, effort):
    from runtime.production import ProductionSettings
    with pytest.raises(ValueError, match="effort"):
        ProductionSettings.from_environment({"MODEL": model, "MODEL_THINKING": thinking, "MODEL_REASONING_EFFORT": effort})


def test_inference_and_synthesis_share_live_generation_policy() -> None:
    inference = RUN_PROFILES[RunMode.INFERENCE]
    synthesis = RUN_PROFILES[RunMode.SYNTHESIS]
    assert inference.compact_active_history == synthesis.compact_active_history
    assert inference.require_complete_trace is False
    assert synthesis.require_complete_trace is True


def test_training_sample_budget_is_separate_from_provider_window():
    from runtime.production import ProductionSettings
    assert ProductionSettings.from_environment({}).training_sample_max_tokens == 131072
    environment = {"TRAINING_SAMPLE_MAX_TOKENS": "65536", "MODEL_CONTEXT_WINDOW": "262144"}
    assert ProductionSettings.from_environment(environment).training_sample_max_tokens == 65536
    assert ProviderSettings.from_environment(environment).context_window == 262144
    with pytest.raises(ValueError, match="TRAINING_SAMPLE_MAX_TOKENS"):
        ProductionSettings.from_environment({"TRAINING_SAMPLE_MAX_TOKENS": "0"})


def test_compaction_policy_has_one_explicit_configuration_with_role_output_budgets():
    from runtime.production import ProductionSettings
    settings = ProductionSettings.from_environment({})
    assert (settings.compact_trigger_ratio, settings.compact_target_tokens,
            settings.compact_summary_target_tokens) == (.9, 131072, 8192)
    assert (settings.max_tokens, settings.owner_max_tokens) == (40960, 32768)
    assert settings.orchestrator_request_max_tokens() == 16384
    assert settings.owner_request_max_tokens("slide") == 20000
    assert settings.owner_request_max_tokens("research") == 12000
    assert settings.owner_request_max_tokens("image") == 8192
    assert settings.critic_request_max_tokens() == 8192
    changed = ProductionSettings.from_environment({"COMPACT_TRIGGER_RATIO": ".85",
        "COMPACT_TARGET_TOKENS": "40000", "COMPACT_SUMMARY_TARGET_TOKENS": "6000"})
    assert (changed.compact_trigger_ratio, changed.compact_target_tokens, changed.compact_summary_target_tokens) == (.85, 40000, 6000)
    explicit = ProductionSettings.from_environment({"AGENT_MAX_TOKENS": "40912",
        "OWNER_MAX_TOKENS": "32768", "ORCHESTRATOR_MAX_TOKENS": "12000",
        "SLIDE_OWNER_MAX_TOKENS": "16000", "RESEARCH_OWNER_MAX_TOKENS": "9000",
        "IMAGE_OWNER_MAX_TOKENS": "6000", "CRITIC_MAX_TOKENS": "7000"})
    assert explicit.orchestrator_request_max_tokens() == 12000
    assert explicit.owner_request_max_tokens("slide") == 16000
    assert explicit.owner_request_max_tokens("research") == 9000
    assert explicit.owner_request_max_tokens("image") == 6000
    assert explicit.critic_request_max_tokens() == 7000


@pytest.mark.parametrize("environment", [
    {"COMPACT_TRIGGER_RATIO": "nan"}, {"COMPACT_TRIGGER_RATIO": "inf"},
    {"COMPACT_TRIGGER_RATIO": "1"}, {"COMPACT_TRIGGER_RATIO": "0"},
    {"COMPACT_TARGET_TOKENS": "0"}, {"COMPACT_SUMMARY_TARGET_TOKENS": "-1"},
    {"COMPACT_TARGET_TOKENS": "8192"},
])
def test_invalid_compaction_policy_is_rejected(environment):
    from runtime.production import ProductionSettings
    with pytest.raises(ValueError, match="COMPACT"):
        ProductionSettings.from_environment(environment)


def test_environment_file_is_side_effect_free_and_launch_values_win(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "runtime.env"
    env_file.write_text(
        "MODEL=file-model\nMODEL_TIMEOUT_S=300\n",
        encoding="utf-8",
    )
    file_values = read_environment_file(env_file)
    merged = merge_environment(
        file_values,
        {"MODEL": "launch-model", "MODEL_API_FORMAT": "anthropic"},
    )
    assert file_values["MODEL"] == "file-model"
    assert merged["MODEL"] == "launch-model"
    assert merged["MODEL_TIMEOUT_S"] == "300"


@pytest.mark.parametrize(
    "api_format",
    ["anthropic", "openai_responses", "openai_chat_compatible"],
)
def test_provider_formats_use_the_same_model_configuration_surface(
    api_format: str,
) -> None:
    settings = ProviderSettings.from_environment(
        {
            "MODEL_API_FORMAT": api_format,
            "MODEL": "shared-model",
            "MODEL_BASE_URL": "https://provider.example/v1/",
            "MODEL_STREAMING": "1",
            "MODEL_TIMEOUT_S": "321",
            "MODEL_QUEUE_TIMEOUT_S": "789",
        }
    )
    assert settings.api_format == api_format
    assert settings.model == "shared-model"
    assert settings.base_url == "https://provider.example/v1"
    assert settings.streaming is True
    assert settings.total_timeout_s == 321
    assert settings.queue_timeout_s == settings.to_manifest()["queue_timeout_s"] == 789


def test_queue_timeout_is_independent_and_must_be_positive():
    settings = ProviderSettings.from_environment({"MODEL_TIMEOUT_S": "2"})
    assert settings.queue_timeout_s == 1800
    with pytest.raises(ValueError):
        ProviderSettings.from_environment({"MODEL_QUEUE_TIMEOUT_S": "0"})


@pytest.mark.parametrize("value,expected", [(None, False), ("0", False), ("false", False), ("1", True), ("true", True)])
def test_return_token_ids_is_explicit_and_recorded(value, expected):
    environment = {} if value is None else {"MODEL_RETURN_TOKEN_IDS": value}
    settings = ProviderSettings.from_environment(environment)
    assert settings.return_token_ids is expected
    assert settings.to_manifest()["return_token_ids"] is expected


def test_invalid_return_token_ids_is_rejected():
    with pytest.raises(ValueError, match="MODEL_RETURN_TOKEN_IDS must be a boolean"):
        ProviderSettings.from_environment({"MODEL_RETURN_TOKEN_IDS": "auto"})


@pytest.mark.parametrize("api_format", ["anthropic", "openai_responses", "openai_chat_compatible"])
def test_deployment_slow_model_budgets_reach_provider_and_review_settings(api_format):
    from runtime.production import ProductionSettings
    from runtime.session import _request_timeout

    environment = read_environment_file(Path(__file__).resolve().parents[1] / "runtime.env")
    environment["MODEL_API_FORMAT"] = api_format
    provider = ProviderSettings.from_environment(environment)
    production = ProductionSettings.from_environment(environment)

    assert provider.total_timeout_s == 0
    assert provider.queue_timeout_s == 7200
    assert provider.connect_timeout_s == 60
    assert provider.idle_timeout_s == 3600
    assert production.critic_timeout_s == 1800
    assert provider.max_attempts == 6
    assert provider.streaming is False
    for streaming in (False, True):
        timeout = _request_timeout(provider, provider.total_timeout_s, streaming=streaming)
        assert timeout.read is None
        assert timeout.connect == 60


def test_legacy_openai_api_format_alias_still_resolves() -> None:
    # 旧部署的 ``openai`` 值必须继续可用,映射到自托管 Chat 兼容格式(刻意保留的别名)。
    settings = ProviderSettings.from_environment(
        {
            "MODEL_API_FORMAT": "openai",
            "MODEL": "shared-model",
            "MODEL_BASE_URL": "https://provider.example/v1/",
        }
    )
    assert settings.api_format == "openai_chat_compatible"


def test_run_spec_preserves_raw_query_without_canvas_configuration(
    tmp_path: Path,
) -> None:
    attachment = tmp_path / "source.txt"
    attachment.write_text("source", encoding="utf-8")
    raw_query = " Build a concise 4:3 deck "
    spec = RunSpec.from_inputs(
        raw_user_query=raw_query,
        output_workspace=tmp_path / "deck",
        attachments=[attachment],
        run_mode="synthesis",
        authoring_profile="general",
        page_count_hint=7,
        revision_request={"revision_no": 2, "instruction": "收紧第 3 页"},
        environment={
            "MODEL_API_FORMAT": "anthropic",
            "MODEL": "claude-test",
            "MODEL_BASE_URL": "https://provider.example/",
            "MODEL_API_KEY": "must-not-enter-manifest",
        },
    )
    manifest = spec.to_manifest()
    assert spec.run_mode is RunMode.SYNTHESIS
    assert spec.raw_user_query == raw_query
    assert manifest["page_count_hint"] == 7
    assert manifest["revision_request"] == {
        "revision_no": 2,
        "instruction": "收紧第 3 页",
    }
    assert "canvas" not in manifest
    assert manifest["provider"]["base_url"] == "https://provider.example"
    assert "must-not-enter-manifest" not in repr(manifest)
    assert not (spec.output_workspace / "canvas.json").exists()


def test_missing_attachment_fails_before_a_model_session(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="attachment files do not exist"):
        RunSpec.from_inputs(
            raw_user_query="deck",
            output_workspace=tmp_path / "deck",
            attachments=[tmp_path / "missing.pdf"],
            environment={
                "MODEL_API_FORMAT": "anthropic",
                "MODEL": "claude-test",
            },
        )


def test_invalid_modes_and_timeouts_are_rejected() -> None:
    with pytest.raises(ValueError, match="expected inference or synthesis"):
        resolve_run_mode("preview")
    with pytest.raises(ValueError, match="must be non-negative"):
        ProviderSettings.from_environment(
            {
                "MODEL_API_FORMAT": "anthropic",
                "MODEL": "claude-test",
                "MODEL_TIMEOUT_S": "-1",
            }
        )


def test_zero_provider_timeout_explicitly_disables_response_deadline():
    settings = ProviderSettings.from_environment({"MODEL_TIMEOUT_S": "0"})
    assert settings.total_timeout_s == settings.to_manifest()["total_timeout_s"] == 0


@pytest.mark.parametrize("query", ["中文字体用宋体；制作年度总结", "Create a deck about 李白"])
def test_unspecified_language_is_left_for_planning(query, tmp_path):
    spec = RunSpec.from_inputs(raw_user_query=query, output_workspace=tmp_path, environment={})
    assert spec.language == spec.to_manifest()["language"] == "auto"


@pytest.mark.parametrize("requested,environment,expected", [
    ("zh", {}, "zh"), ("en", {}, "en"),
    ("auto", {"MURAL_DELIVERY_LANGUAGE": "zh"}, "zh"),
    ("en", {"MURAL_DELIVERY_LANGUAGE": "zh"}, "en"),
])
def test_language_uses_explicit_configuration_not_query(tmp_path, requested, environment, expected):
    spec = RunSpec.from_inputs(raw_user_query="制作年度总结，使用英文输出", output_workspace=tmp_path,
                               language=requested, environment=environment)
    assert spec.language == expected


@pytest.mark.parametrize(("requested", "expected"), [
    ("auto", "general"), ("general", "general"),
    ("material_grounded", "general"), ("closed-material", "general"),
])
def test_authoring_profile_only_normalizes_caller_configuration(requested, expected):
    assert resolve_authoring_profile(requested) == expected


@pytest.mark.parametrize("query", [
    "请严格依据所给材料制作，不得使用外部信息和图片",
    "只用附件中的事实，允许搜索配图",
    "不要引入外部案例",
    "Create a factual deck about OPD.",
    "Only use the attached materials; do not search external sources.",
])
def test_query_is_preserved_for_model_without_mechanical_profile_selection(query, tmp_path):
    automatic = RunSpec.from_inputs(raw_user_query=query, output_workspace=tmp_path, environment={})
    assert automatic.authoring_profile == "general"
    assert automatic.raw_user_query == query
    explicit = RunSpec.from_inputs(raw_user_query=query, output_workspace=tmp_path,
                                   authoring_profile="material-grounded", environment={})
    assert explicit.authoring_profile == "general"
    assert explicit.raw_user_query == query


def test_authoring_profile_rejects_unknown_caller_configuration():
    with pytest.raises(ValueError, match="authoring_profile"):
        resolve_authoring_profile("unknown")


@pytest.mark.parametrize(("arguments", "environment", "expected"), [
    ([], {}, "general"),
    ([], {"MURAL_AUTHORING_PROFILE": "material-grounded"}, "general"),
    (["--authoring-profile", "auto"], {"MURAL_AUTHORING_PROFILE": "material-grounded"}, "general"),
    (["--authoring-profile", "general"], {"MURAL_AUTHORING_PROFILE": "material-grounded"}, "general"),
])
def test_cli_profile_priority_matches_single_and_batch_inputs(tmp_path, arguments, environment, expected):
    from infer import _argument_parser
    from runtime.batch import BatchArgs

    cli = _argument_parser().parse_args(["--query", "Use only the information in the material.", *arguments])
    spec = RunSpec.from_inputs(raw_user_query=cli.query, output_workspace=tmp_path,
        authoring_profile=cli.authoring_profile, environment=environment)
    assert spec.authoring_profile == expected
    assert BatchArgs(queries=tmp_path / "queries.jsonl", batch="test").authoring_profile is None
