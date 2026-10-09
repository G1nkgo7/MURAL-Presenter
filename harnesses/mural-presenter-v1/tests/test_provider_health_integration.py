from contextlib import contextmanager, nullcontext
import json
from types import SimpleNamespace

import pytest

from runtime.agent import AgentRuntime
from runtime.config import RunMode, load_runtime_config
from runtime.production import ProductionSettings
from runtime.provider_health import ProviderHealthStore
from runtime.recovery import ProviderFailure
from runtime.session import ModelRequest, ModelSession, ProviderChannel, TransportResponse, UsageLedger
from runtime.skill_runtime import SkillRuntime
from runtime.tools import ToolContext, ToolRuntime
from test_agent_runtime import ScriptedTransport, _response, _settings, _text


class _FakeClock:
    def __init__(self):
        self.value = 0.0

    def now(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class _QueueGate:
    def __init__(self, clock, *, first_wait_s, on_first_release=None):
        self.clock = clock
        self.first_wait_s = first_wait_s
        self.on_first_release = on_first_release
        self.entries = 0

    @contextmanager
    def acquire(self):
        self.entries += 1
        if self.entries == 1:
            self.clock.advance(self.first_wait_s)
            if self.on_first_release is not None:
                self.on_first_release()
        yield


class _CapturingTransport:
    def __init__(self):
        self.calls = []

    def complete(self, channel, request, *, total_timeout_s):
        self.calls.append((channel.channel_id, total_timeout_s))
        return TransportResponse(_response(_text("Complete.")), first_byte_s=0)


def _budget_request(provider):
    return ModelRequest(model=provider.model, system="fixture",
        messages=({"role": "user", "content": "Continue."},), max_tokens=128)


@pytest.mark.parametrize("health_change", ["none", "quarantine", "cooldown"])
def test_request_budget_excludes_queue_once_when_shared_health_changes(tmp_path, health_change):
    clock = _FakeClock()
    provider = _settings()
    store = ProviderHealthStore(tmp_path, wall_clock=clock.now)
    primary = ProviderChannel("credential-a", "primary")
    backup = ProviderChannel("credential-b", "backup")

    def change_health():
        if health_change == "quarantine":
            failure = ProviderFailure("authentication", "fixture", status_code=403)
        elif health_change == "cooldown":
            failure = ProviderFailure("rate_limit", "fixture", retryable=True, retry_after_s=20)
        else:
            return
        store.record_failure(store.identity(provider, primary.key), failure)

    gate = _QueueGate(clock, first_wait_s=10, on_first_release=change_health)
    transport = _CapturingTransport()
    events = []
    session = ModelSession(settings=provider, channels=(primary, backup), transport=transport,
        provider_health=store, balance="primary-first", slot_factory=gate.acquire,
        monotonic=clock.now, sleeper=clock.advance, event_sink=events.append)

    result = session.call(_budget_request(provider), request_budget_s=5)

    expected_channel = "primary" if health_change == "none" else "backup"
    assert result.ok and result.attempts == 1
    assert transport.calls == [(expected_channel, 5)]
    assert gate.entries == (1 if health_change == "none" else 2)
    assert clock.now() == 10
    skipped = [event for event in events if event.get("skipped")]
    assert len(skipped) == int(health_change != "none")
    if skipped:
        assert skipped[0]["queue_wait_s"] == 10


def test_post_queue_shared_cooldown_consumes_budget_without_requeue_loop(tmp_path):
    clock = _FakeClock()
    provider = _settings()
    store = ProviderHealthStore(tmp_path, wall_clock=clock.now)
    channel = ProviderChannel("credential-a", "primary")
    sleeps = []

    def apply_cooldown():
        store.record_failure(store.identity(provider, channel.key),
            ProviderFailure("rate_limit", "fixture", retryable=True, retry_after_s=2))

    def sleep(seconds):
        sleeps.append(seconds)
        clock.advance(seconds)

    gate = _QueueGate(clock, first_wait_s=10, on_first_release=apply_cooldown)
    transport = _CapturingTransport()
    session = ModelSession(settings=provider, channels=(channel,), transport=transport,
        provider_health=store, balance="primary-first", slot_factory=gate.acquire,
        monotonic=clock.now, sleeper=sleep)

    result = session.call(_budget_request(provider), request_budget_s=5)

    assert result.ok and result.attempts == 1
    assert transport.calls == [("primary", 3)]
    assert sleeps == [2]
    assert gate.entries == 2
    assert clock.now() == 12


@pytest.mark.parametrize("kind", ["main", "context_compaction", "critic_visual"])
def test_every_request_family_uses_prepared_health_directory(tmp_path, kind):
    config = load_runtime_config()
    config["resources"]["directory"] = str(tmp_path / "shared-pool")
    config_path = tmp_path / "runtime.json"
    config_path.write_text(json.dumps(config))
    environment = {"MURAL_RUNTIME_CONFIG": str(config_path), "MODEL_CHANNEL_BALANCE": "primary-first"}
    provider = _settings()
    channels = (ProviderChannel("rejected-fixture", "primary"), ProviderChannel("usable-fixture", "backup"))
    store = ProviderHealthStore(config["resources"]["directory"])
    store.record_failure(store.identity(provider, channels[0].key),
                         ProviderFailure("authentication", "fixture", status_code=403))
    transport = ScriptedTransport([_response(_text("Complete."))])
    if kind == "main":
        runtime = AgentRuntime(settings=provider, channels=channels, transport=transport,
            tools=ToolRuntime(), environment=environment, balance="primary-first")
        result = runtime.run(agent_id="fixture", role="research", system="fixture",
            tool_context=ToolContext("fixture", "research"),
            initial_messages=({"role": "user", "content": "Continue."},),
            max_turns=1, max_tokens=128, thinking=False, effort="medium")
        assert result.final_text == "Complete."
    else:
        host = SkillRuntime.__new__(SkillRuntime)
        host.prepared = SimpleNamespace(environment=environment,
            run_spec=SimpleNamespace(provider=provider, run_mode=RunMode.SYNTHESIS),
            settings=ProductionSettings(skill_source=tmp_path), channels=channels,
            transport=transport, usage=UsageLedger(),
            slots=SimpleNamespace(acquire=lambda *args, **kwargs: nullcontext()))
        request = ModelRequest(model=provider.model, system="fixture",
            messages=({"role": "user", "content": "Continue."},),
            max_tokens=128, request_kind=kind)
        if kind == "context_compaction":
            services = SimpleNamespace(workspace=SimpleNamespace(paths=SimpleNamespace(root=tmp_path)),
                live_trace=SimpleNamespace(record_runtime_event=lambda event: None))
            result = host._invoke_compaction_summary(services, request,
                agent_id="fixture", role="research", segment_id="fixture-segment")
        else:
            result = host._call_review(request, provider, transport, channels, 30, None,
                                       "critic", "fixture")
        assert result.ok
    assert [channel for channel, _ in transport.calls] == ["backup"]
