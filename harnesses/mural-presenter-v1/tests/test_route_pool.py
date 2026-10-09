import copy
from dataclasses import replace
import hashlib
import json

import httpx
import pytest

from runtime.config import ProviderSettings
from runtime.provider_health import ProviderHealthStore
from runtime.recovery import ProviderFailure, SegmentCheckpoint
from runtime.session import ModelRequest, ModelSession, ProviderChannel, create_transport, provider_channels


SFA = "gpt-6-astra/azure/sfa"
QWB = "gpt-6-astra/azure_L/qwb"


@pytest.fixture
def route_env(tmp_path):
    (tmp_path / "keys.env").write_text("SFA_KEY=fixture-sfa\nQWB_KEY=fixture-qwb\n")
    pool = {"schema": "mural.gpt6-route-pool.v1", "api_format": "openai_responses",
            "model": SFA, "base_url": "https://provider.example/v1", "channels": [
                {"id": "sfa", "model": SFA, "key_env_file": "keys.env", "key_env": "SFA_KEY"},
                {"id": "qwb", "model": QWB, "key_env_file": "keys.env", "key_env": "QWB_KEY"}]}
    path = tmp_path / "pool.json"
    path.write_text(json.dumps(pool))
    return {"MODEL_API_FORMAT": "openai_responses", "MODEL": SFA,
            "MODEL_BASE_URL": pool["base_url"], "MODEL_ROUTE_POOL_PATH": str(path)}


def test_route_pool_binds_keys_to_models_with_order_independent_ids(route_env):
    channels = provider_channels(route_env)
    assert [(c.model, c.key) for c in channels] == [(SFA, "fixture-sfa"), (QWB, "fixture-qwb")]
    assert "fixture-" not in repr(channels)
    from pathlib import Path
    path = Path(route_env["MODEL_ROUTE_POOL_PATH"])
    pool = json.loads(path.read_text())
    pool["channels"].reverse()
    path.write_text(json.dumps(pool))
    assert provider_channels(route_env) == tuple(reversed(channels))


@pytest.mark.parametrize("override", [
    {"MODEL_API_FORMAT": "anthropic"}, {"MODEL": "Qwen3.8-27B"},
    {"MODEL_BASE_URL": "https://wrong.example/v1"}, {"MODEL_POOL_ENV_PATH": "/old-pool.env"},
])
def test_route_pool_rejects_wrong_dispatch_contract(route_env, override):
    with pytest.raises(ValueError):
        provider_channels({**route_env, **override})


@pytest.mark.parametrize("damage", ["empty", "duplicate_id", "duplicate_pair", "bad_model", "missing_key", "invalid_json"])
def test_route_pool_rejects_invalid_records_without_disclosing_credentials(route_env, damage):
    from pathlib import Path
    path = Path(route_env["MODEL_ROUTE_POOL_PATH"])
    pool = json.loads(path.read_text())
    if damage == "empty": pool["channels"] = []
    elif damage == "duplicate_id": pool["channels"][1]["id"] = "sfa"
    elif damage == "duplicate_pair": pool["channels"][1].update(model=SFA, key_env="SFA_KEY")
    elif damage == "bad_model": pool["channels"][1]["model"] = "claude-opus-5"
    elif damage == "missing_key": pool["channels"][1]["key_env"] = "MISSING"
    path.write_text("{fixture-private-invalid-json" if damage == "invalid_json" else json.dumps(pool))
    with pytest.raises(ValueError) as exc:
        provider_channels(route_env)
    assert "fixture-" not in str(exc.value)


@pytest.mark.parametrize("model,api_format", [("claude-opus-5", "anthropic"), ("Qwen3.8-27B", "openai_chat_compatible")])
def test_legacy_pool_keeps_keys_ids_and_no_model_override(model, api_format):
    channels = provider_channels({"MODEL": model, "MODEL_API_FORMAT": api_format,
        "MODEL_API_KEY": "fixture-a", "MODEL_API_KEY_FALLBACKS": "fixture-b"})
    assert [c.key for c in channels] == ["fixture-a", "fixture-b"]
    assert all(c.model == "" for c in channels)
    assert channels[0].channel_id == "ch-1-" + hashlib.sha256(b"fixture-a").hexdigest()[:8]


@pytest.mark.parametrize("status", [403, 429, 502])
def test_failover_switches_model_and_key_and_records_actual_route(tmp_path, route_env, status):
    settings = replace(ProviderSettings.from_environment(route_env), streaming=False, max_attempts=2)
    channels = provider_channels(route_env)
    seen, events = [], []

    def handler(request):
        body = json.loads(request.content)
        seen.append((request.headers["authorization"], body["model"]))
        if body["model"] == SFA:
            return httpx.Response(status, headers={"Retry-After": "60"} if status == 429 else {},
                                  json={"error": {"message": "fixture failure"}})
        return httpx.Response(200, json={"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]})

    transport = create_transport(settings, client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    health = ProviderHealthStore(tmp_path / "health")
    session = ModelSession(settings=settings, channels=channels, transport=transport,
        provider_health=health, event_sink=events.append, trace_root=str(tmp_path / "run"), balance="primary-first")
    request = ModelRequest(model=SFA, system="test", messages=({"role": "user", "content": "hello"},),
                           max_tokens=256, thinking=True, effort="high")
    original = copy.deepcopy(request)
    result = session.call(request)
    assert result.ok and result.attempts == 2
    assert seen == [("Bearer fixture-sfa", SFA), ("Bearer fixture-qwb", QWB)]
    assert request == original
    starts = [e for e in events if e["event_type"] == "provider_attempt_started"]
    assert [e["model"] for e in starts] == [SFA, QWB]
    assert all(e["requested_model"] == SFA and e["effective_effort"] == "high" for e in starts)
    assert starts[1]["failover_from_channel_id"] == channels[0].channel_id
    for path in (tmp_path / "run" / "_trace" / "raw" / "provider").glob("*.request.json"):
        raw = json.loads(path.read_text())
        assert raw["model"] == raw["body"]["model"]
        assert raw["requested_model"] == SFA
        assert "fixture-sfa" not in path.read_text() and "fixture-qwb" not in path.read_text()
    expected_identity = health.identity(replace(settings, model=QWB), channels[1].key)
    assert session._health_identity(channels[1]) == expected_identity
    if status == 403:
        assert health.status(session._health_identity(channels[0])).quarantined
        assert not health.status(expected_identity).quarantined
        next_session = ModelSession(settings=settings, channels=channels, transport=transport,
                                   provider_health=health, balance="primary-first")
        assert next_session.call(request).ok
        assert seen[-1] == ("Bearer fixture-qwb", QWB) and len(seen) == 3


def test_signed_affinity_never_silently_switches_route(tmp_path, route_env):
    settings = replace(ProviderSettings.from_environment(route_env), streaming=False, max_attempts=2)
    channels = provider_channels(route_env)
    health = ProviderHealthStore(tmp_path / "health")
    health.record_failure(health.identity(settings, channels[0].key),
                          ProviderFailure("authentication", "fixture", status_code=403))
    checkpoint = replace(SegmentCheckpoint.initial(), provider_affinity_id=channels[0].channel_id)
    session = ModelSession(settings=settings, channels=channels, transport=None,
                           provider_health=health, checkpoint=checkpoint)
    result = session.call(ModelRequest(model=SFA, system="test", messages=()))
    assert not result.ok and result.segment_rollover_required
    assert result.attempts == 0


def test_same_key_on_different_routes_has_distinct_health_identity(tmp_path, route_env):
    settings = ProviderSettings.from_environment(route_env)
    a = ProviderChannel("fixture-key", "sfa", model=SFA)
    b = ProviderChannel("fixture-key", "qwb", model=QWB)
    session = ModelSession(settings=settings, channels=(a, b), transport=None,
                           provider_health=ProviderHealthStore(tmp_path))
    assert session._health_identity(a) != session._health_identity(b)


@pytest.mark.parametrize("api_format,model", [("anthropic", "claude-opus-5"), ("openai_chat_compatible", "Qwen3.8-27B")])
def test_non_responses_session_rejects_routed_channel(tmp_path, api_format, model):
    with pytest.raises(ValueError, match="GPT-6 Responses"):
        ModelSession(settings=ProviderSettings(api_format=api_format, model=model, base_url="https://provider.example"),
                     channels=(ProviderChannel("fixture", "route", model=QWB),), transport=None)
