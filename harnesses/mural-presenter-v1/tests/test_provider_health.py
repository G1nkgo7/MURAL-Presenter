from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
import multiprocessing
from pathlib import Path

import httpx

from runtime.config import ProviderSettings, load_runtime_config
from runtime.provider_health import ProviderHealthStore
from runtime.recovery import ProviderFailure, SegmentCheckpoint
from runtime.session import ModelRequest, ModelSession, ProviderChannel, TransportResponse


def _settings(**changes) -> ProviderSettings:
    return replace(ProviderSettings(
        api_format="anthropic", model="health-model", base_url="https://provider.example/v1",
        channel_cooldown_s=30, max_attempts=3, prompt_cache=False, prompt_cache_messages=False,
    ), **changes)


def _request(messages=None) -> ModelRequest:
    return ModelRequest(model="health-model", system="system", messages=(
        {"role": "user", "content": "continue"},
    ) if messages is None else tuple(messages), max_tokens=32)


def _response():
    from types import SimpleNamespace
    return SimpleNamespace(content=[], usage=SimpleNamespace(
        input_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0, output_tokens=1,
    ))


class _Transport:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def complete(self, channel, request, *, total_timeout_s):
        self.calls.append(channel.channel_id)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return TransportResponse(outcome, first_byte_s=0)


def _quarantine_process(directory: str, identity: str) -> None:
    ProviderHealthStore(directory).record_failure(
        identity, ProviderFailure("authentication", "ignored", status_code=403, retryable=True),
    )


def _cooldown_process(directory: str, identity: str, retry_after_s: float) -> None:
    ProviderHealthStore(directory).record_failure(
        identity, ProviderFailure("rate_limit", "ignored", retryable=True,
                                  retry_after_s=retry_after_s),
    )


def test_identity_is_contract_and_credential_stable_not_channel_alias(tmp_path: Path) -> None:
    store = ProviderHealthStore(tmp_path)
    settings = _settings()
    baseline = store.identity(settings, "credential-a")
    assert baseline == store.identity(settings, "credential-a")
    assert baseline != store.identity(settings, "credential-b")
    assert baseline != store.identity(replace(settings, model="other-model"), "credential-a")
    assert baseline != store.identity(replace(settings, base_url="https://other.example/v1"), "credential-a")
    assert baseline != store.identity(replace(settings, api_format="openai_chat_compatible"), "credential-a")


def test_health_reads_do_not_create_per_credential_directories(tmp_path: Path) -> None:
    store = ProviderHealthStore(tmp_path)
    identity = store.identity(_settings(), "credential-a")
    assert not store.status(identity).quarantined
    assert store.snapshot([identity])[identity].cooldown_remaining_s == 0
    assert not (store.root / identity).exists()
    store.record_failure(identity, ProviderFailure("server_error", "fixture", retryable=True))
    assert not (store.root / identity).exists()


def test_session_environment_selects_its_configured_shared_directory(tmp_path: Path) -> None:
    config = load_runtime_config()
    configured_root = tmp_path / "configured-resources"
    config["resources"]["directory"] = str(configured_root)
    config_path = tmp_path / "runtime.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    session = ModelSession(settings=_settings(), channels=(ProviderChannel("credential-a", "a"),),
        transport=_Transport([_response()]), environment={"MURAL_RUNTIME_CONFIG": str(config_path)})
    assert session.provider_health.root == configured_root / "provider-health-v1"


def test_shared_auth_quarantine_is_sticky_across_sessions_and_redacted_on_disk(tmp_path: Path) -> None:
    store = ProviderHealthStore(tmp_path)
    settings = _settings()
    request = httpx.Request("POST", "https://provider.example/v1")
    rejected = httpx.HTTPStatusError("response-body-secret", request=request,
        response=httpx.Response(403, request=request, json={"error": {"message": "response-body-secret"}}))
    first = ModelSession(settings=settings, channels=(ProviderChannel("credential-a", "first-alias"),),
        transport=_Transport([rejected]), provider_health=store)
    assert first.call(_request()).failure.status_code == 403
    second_transport = _Transport([_response()])
    second = ModelSession(settings=settings, channels=(ProviderChannel("credential-a", "reordered-alias"),),
        transport=second_transport, provider_health=store)
    result = second.call(_request())
    assert result.failure.kind == "channel_pool_exhausted" and result.attempts == 0
    assert not second_transport.calls
    persisted = "\n".join(path.name for path in store.root.rglob("*"))
    assert "credential-a" not in persisted and "provider.example" not in persisted and "403" not in persisted
    # A later response on an already in-flight request never resets a quarantine.
    assert store.status(store.identity(settings, "credential-a")).quarantined


def test_shared_auth_quarantine_requires_signed_segment_rollover(tmp_path: Path) -> None:
    store = ProviderHealthStore(tmp_path)
    settings = _settings()
    locked = ProviderChannel("credential-a", "locked")
    store.record_failure(store.identity(settings, locked.key),
        ProviderFailure("authentication", "ignored", status_code=401, retryable=True))
    checkpoint = SegmentCheckpoint(segment_id="signed", predecessor_segment_id=None,
        messages=({"role": "assistant", "content": [{"type": "thinking", "thinking": "x", "signature": "s"}]},),
        provider_affinity_id=locked.channel_id)
    transport = _Transport([_response()])
    session = ModelSession(settings=settings, channels=(locked, ProviderChannel("credential-b", "backup")),
        checkpoint=checkpoint, transport=transport, provider_health=store)
    result = session.call(_request(checkpoint.messages))
    assert result.segment_rollover_required and result.failure.kind == "channel_affinity_unavailable"
    assert result.attempts == 0 and not transport.calls


def test_explicit_retry_after_is_shared_and_extended_without_slot_wait(tmp_path: Path) -> None:
    now = [100.0]
    store = ProviderHealthStore(tmp_path, wall_clock=lambda: now[0])
    settings = _settings()
    identity = store.identity(settings, "credential-a")
    store.record_failure(identity, ProviderFailure("rate_limit", "ignored", retryable=True, retry_after_s=7))
    store.record_failure(identity, ProviderFailure("rate_limit", "ignored", retryable=True, retry_after_s=11))
    assert store.status(identity).cooldown_remaining_s == 11
    now[0] += 11
    assert store.status(identity).cooldown_remaining_s == 0


def test_filesystem_store_keeps_longest_concurrent_retry_after(tmp_path: Path) -> None:
    context = multiprocessing.get_context("fork")
    store = ProviderHealthStore(tmp_path)
    identity = store.identity(_settings(), "credential-a")
    children = [context.Process(target=_cooldown_process, args=(str(tmp_path), identity, delay))
                for delay in (2, 9)]
    for child in children:
        child.start()
    for child in children:
        child.join(5)
        assert child.exitcode == 0
    assert store.status(identity).cooldown_remaining_s > 8


def test_post_queue_health_recheck_releases_slot_without_a_fictitious_attempt(tmp_path: Path) -> None:
    store = ProviderHealthStore(tmp_path)
    settings = _settings(channel_cooldown_s=0)
    entered = []
    primary = ProviderChannel("credential-a", "primary")

    @contextmanager
    def slot():
        entered.append(True)
        if len(entered) == 1:
            store.record_failure(store.identity(settings, primary.key),
                ProviderFailure("authentication", "ignored", status_code=403, retryable=True))
        yield

    transport, events = _Transport([_response()]), []
    session = ModelSession(settings=settings,
        channels=(primary, ProviderChannel("credential-b", "backup")), transport=transport,
        balance="primary-first", slot_factory=slot, event_sink=events.append, provider_health=store)
    result = session.call(_request())
    assert result.ok and result.attempts == 1
    assert transport.calls == ["backup"] and len(entered) == 2
    assert session.usage.summary()["attempts"] == 1
    queue_end = [event for event in events if event["event_type"] == "resource_wait_completed"
                 and event.get("skipped")]
    assert len(queue_end) == 1
    assert queue_end[0]["cancelled"] is False
    assert queue_end[0]["skip_reason"] == "provider_auth_quarantined"


def test_filesystem_store_serializes_process_quarantine_race(tmp_path: Path) -> None:
    context = multiprocessing.get_context("fork")
    store = ProviderHealthStore(tmp_path)
    identity = store.identity(_settings(), "credential-a")
    children = [context.Process(target=_quarantine_process, args=(str(tmp_path), identity)) for _ in range(2)]
    for child in children:
        child.start()
    for child in children:
        child.join(5)
        assert child.exitcode == 0
    assert store.status(identity).quarantined
