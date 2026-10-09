"""独立接线回归：真实审计 invoker/ModelSession 只从请求预算扣除非排队时间。"""
from __future__ import annotations

from concurrent.futures import Future
from contextlib import contextmanager
import hashlib
from pathlib import Path
import threading
from types import SimpleNamespace

from PIL import Image
import pytest

from qc.review_hook import InspectionClosed
from runtime.config import ProviderSettings
from runtime.live_trace import LiveEventJournal
from runtime.production import ProductionSettings
from runtime.recovery import ProviderRequestTimeout, ResourceSlotTimeout
from runtime.session import ModelSession, NormalizedAssistantResponse, ProviderChannel, TransportResponse, UsageLedger
from runtime.skill_runtime import SkillRuntime
from runtime.tools import ToolRuntime
import runtime.skill_runtime as wiring


class Clock:
    def __init__(self):
        self.value = 0.0

    def now(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class QueuedSlots:
    def __init__(self, clock, *, fail=False):
        self.clock = clock
        self.fail = fail
        self.calls = []

    @contextmanager
    def acquire(self, resource, *, model, deadline_monotonic, stop_event=None):
        self.calls.append((resource, model, deadline_monotonic - self.clock.now()))
        self.clock.advance(500)
        if self.fail:
            raise ResourceSlotTimeout("synthetic local queue exhausted")
        yield 500


def _runtime(tmp_path, monkeypatch, *, channels=2, outcome="success", cooldown_s=120):
    clock = Clock()
    slots = QueuedSlots(clock, fail=outcome == "queue_failed")
    sessions, events, requests = [], [], []
    original_result = Future.result

    def await_real_result(future, timeout=None):
        assert timeout is None, "Critic future must not introduce a second request deadline"
        return original_result(future, timeout=timeout)

    monkeypatch.setattr(Future, "result", await_real_result)
    monkeypatch.setattr(wiring, "time", SimpleNamespace(monotonic=clock.now))

    def session_factory(**kwargs):
        downstream = kwargs.pop("event_sink", None)
        def record(event):
            events.append(event)
            if downstream is not None:
                downstream(event)
        session = ModelSession(**kwargs, monotonic=clock.now, sleeper=clock.advance, event_sink=record)
        sessions.append(session)
        return session

    monkeypatch.setattr(wiring, "ModelSession", session_factory)
    settings = ProductionSettings(skill_source=Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1",
        enable_external_visual_critic=True, critic_timeout_s=120)
    budget = settings.critic_timeout_s
    text = "synthetic completed review"

    class Transport:
        def complete(self, channel, request, *, total_timeout_s):
            requests.append((channel.channel_id, request, total_timeout_s))
            if outcome == "exhausted":
                clock.advance(total_timeout_s)
                raise ProviderRequestTimeout("total_request", total_timeout_s)
            if outcome == "retry" and len(requests) == 1:
                clock.advance(budget / 2)
                raise ProviderRequestTimeout("total_request", budget / 2)
            clock.advance(1)
            response = NormalizedAssistantResponse(
                content=({"type": "text", "text": text},), analysis_summaries=(), stop_reason="end_turn",
                usage=SimpleNamespace(input_tokens=3, output_tokens=2))
            return TransportResponse(response, first_byte_s=1)

    runtime = SkillRuntime.__new__(SkillRuntime)
    runtime._trace_lock = threading.RLock()
    runtime._agent_trace = tmp_path / "_trace/agent-events.jsonl"
    runtime._provider_trace = tmp_path / "_trace/provider-events.jsonl"
    runtime._critic_records = []
    runtime._critic_state = tmp_path / "critic-results.json"
    runtime.prepared = SimpleNamespace(settings=settings, snapshot=SimpleNamespace(tree_sha256="test-skill"),
        run_spec=SimpleNamespace(run_mode="inference", output_workspace=tmp_path,
            provider=ProviderSettings(api_format="anthropic", model="main-model", base_url="https://provider.test",
                total_timeout_s=600, queue_timeout_s=1800, max_attempts=6, channel_cooldown_s=cooldown_s)),
        environment={"MODEL_CHANNEL_BALANCE": "primary-first"}, transport=Transport(),
        channels=(ProviderChannel("synthetic-a", "A"), ProviderChannel("synthetic-b", "B"))[:channels],
        usage=UsageLedger(), slots=slots)
    from state.workspace import WorkspaceState
    workspace = WorkspaceState(tmp_path)
    captured = []
    services = SimpleNamespace(workspace=workspace, live_trace=LiveEventJournal(tmp_path / "_trace/live-events.jsonl"),
        tools=ToolRuntime(), capture_training_segment=lambda *args: captured.append(args))
    png = tmp_path / "page.png"
    Image.new("RGB", (8, 8), "white").save(png)
    closed = InspectionClosed("slide-owner", "slide", "inspection", "html",
        hashlib.sha256(png.read_bytes()).hexdigest(), png.name, str(tmp_path), page=1, page_uid="p1")
    runtime._build_review_coordinator(services)

    def invoke():
        try:
            runtime._critic_coordinator.review(closed)
            assert not runtime._critic_coordinator._cycles
            assert len(runtime._critic_records) == 1
            return runtime._critic_records[0]
        finally:
            runtime._critic_executor.shutdown(wait=True)

    return SimpleNamespace(invoke=invoke, clock=clock, slots=slots, sessions=sessions,
        requests=requests, events=events, budget=budget, runtime=runtime, captured=captured)


def test_review_invoker_waits_longer_than_request_budget_then_receives_full_budget(tmp_path, monkeypatch):
    case = _runtime(tmp_path, monkeypatch)
    result = case.invoke()
    assert result["completed"] and result["body"] == "synthetic completed review"
    assert len(case.captured) == 1 and case.captured[0][1] == "critic"
    assert case.captured[0][0] == result["source_refs"]["agent_id"]
    assert len(case.sessions) == len(case.requests) == 1
    assert case.requests[0][2] == case.budget
    assert case.slots.calls == [("model", "main-model", 1800)]
    assert case.clock.now() == 501
    assert [event["queue_wait_s"] for event in case.events if event["event_type"] == "resource_wait_completed"] == [500]
    assert case.runtime.prepared.usage.summary()["calls"] == 1


@pytest.mark.parametrize("channels", [1, 2])
@pytest.mark.parametrize("cooldown_s", [0, 10])
def test_review_invoker_retries_share_active_budget_but_not_queue_time(tmp_path, monkeypatch, channels, cooldown_s):
    case = _runtime(tmp_path, monkeypatch, channels=channels, outcome="retry", cooldown_s=cooldown_s)
    result = case.invoke()
    assert result["completed"]
    backoff = (cooldown_s or 2) if channels == 1 else 0
    assert [request[2] for request in case.requests] == [case.budget, case.budget / 2 - backoff]
    assert len(case.sessions) == 1
    assert case.clock.now() == 1000 + case.budget / 2 + backoff + 1
    assert all(call[2] == 1800 for call in case.slots.calls)
    usage = case.runtime.prepared.usage.summary()
    assert usage["attempts"] == 2 and usage["failed_attempts"] == 1
    assert usage["provider_failovers"] == int(channels == 2)


def test_review_cooldown_cannot_reset_or_overrun_existing_request_budget(tmp_path, monkeypatch):
    case = _runtime(tmp_path, monkeypatch, channels=1, outcome="retry", cooldown_s=120)
    assert not case.invoke()["completed"]
    assert len(case.requests) == len(case.slots.calls) == 1
    assert case.clock.now() == 500 + case.budget
    waits = [event for event in case.events if event.get("resource") == "provider_cooldown"]
    assert waits[-1]["queue_wait_s"] == case.budget / 2


def test_exhausted_review_cannot_restart_an_entire_request_budget(tmp_path, monkeypatch):
    case = _runtime(tmp_path, monkeypatch, outcome="exhausted")
    assert not case.invoke()["completed"]
    assert len(case.sessions) == len(case.requests) == len(case.slots.calls) == 1
    assert case.clock.now() == 500 + case.budget


def test_failed_review_queue_is_not_reentered_or_counted_as_provider_failure(tmp_path, monkeypatch):
    case = _runtime(tmp_path, monkeypatch, outcome="queue_failed")
    result = case.invoke()
    assert not result["completed"]
    assert result["incomplete_reason"] == "local model resource queue reached its deadline; no provider request was sent"
    assert not result["source_refs"].get("attempt_id") and not case.captured
    assert len(case.sessions) == len(case.slots.calls) == 1
    assert not case.requests
    assert [event["event_type"] for event in case.events] == ["resource_wait_started", "resource_wait_failed"]
    usage = case.runtime.prepared.usage.summary()
    assert usage["attempts"] == usage["failed_attempts"] == usage["provider_failovers"] == 0
