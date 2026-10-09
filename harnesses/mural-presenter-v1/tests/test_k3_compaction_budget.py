from dataclasses import replace
from types import SimpleNamespace

import pytest

from runtime.config import RunMode
from runtime.recovery import CompactionSummaryError, compaction_request_max_tokens, context_compaction_summary
from runtime.session import ModelRequest, estimate_request_tokens
from test_agent_runtime import _settings, _summary_result


@pytest.mark.parametrize("stop_reason,text", [("stop", ""), ("end_turn", ""),
                                               ("max_tokens", "cut"), ("length", "cut")])
def test_k3_first_and_short_retry_reserve_gateway_thinking(stop_reason, text):
    calls = []
    responses = [_summary_result(text, stop_reason=stop_reason), _summary_result("Keep the source and finish page 2.")]
    settings = replace(_settings(), model="kimi-k3", context_window=131072, context_safety_buffer=1000)
    def invoke(request):
        calls.append(request)
        assert request.max_tokens > 8192
        assert estimate_request_tokens(request.system, request.messages) + request.max_tokens + 1000 <= 131072
        return responses.pop(0)
    summary, metrics = context_compaction_summary([{"role": "user", "content": "Evidence"}],
        agent_id="slide", role="slide", current_task=None, settings=settings, max_tokens=8192,
        thinking=True, effort="high", target_tokens=4096, invoke=invoke)
    assert [r.max_tokens for r in calls] == [16384, 16384]
    assert all(r.thinking and r.effort == "low" for r in calls)
    assert "at most 2048 visible tokens" in calls[1].system
    assert metrics["summary_retry_max_tokens"] == 16384
    assert "Keep the source" in summary


def test_other_models_unchanged_and_large_k3_budget_not_lowered():
    for model in ("gpt-6-astra/azure/sfa", "claude-opus-5", "glm-5.3-flash/zai_L/sfc"):
        assert compaction_request_max_tokens(model, 2048) == 2048
    assert compaction_request_max_tokens("kimi-k3", 40960) == 40960


def test_k3_no_input_room_fails_recoverably_without_provider_call():
    settings = replace(_settings(), model="kimi-k3", context_window=17000, context_safety_buffer=1000)
    calls = []
    with pytest.raises(CompactionSummaryError, match="capacity"):
        context_compaction_summary([{"role": "user", "content": "Evidence"}], agent_id="slide",
            role="slide", current_task=None, settings=settings, max_tokens=8192, thinking=True,
            effort="high", target_tokens=4096, invoke=lambda r: calls.append(r))
    assert calls == []


def test_runtime_boundary_normalizes_before_sending_and_rejects_over_128k(monkeypatch):
    import runtime.skill_runtime as module
    calls = []
    class Session:
        def __init__(self, **kwargs):
            pass
        def call(self, request, **kwargs):
            calls.append(request)
            return _summary_result("handoff")
    monkeypatch.setattr(module, "ModelSession", Session)
    provider = replace(_settings(), model="kimi-k3", context_window=262144, context_safety_buffer=1000)
    prepared = SimpleNamespace(run_spec=SimpleNamespace(provider=provider, run_mode=RunMode.SYNTHESIS),
        settings=SimpleNamespace(training_sample_max_tokens=262144), channels=(), environment={},
        transport=None, usage=None, slots=None)
    fake = SimpleNamespace(prepared=prepared, _record_runtime_event=lambda *args: None)
    services = SimpleNamespace(workspace=SimpleNamespace(paths=SimpleNamespace(root="/tmp/test"), deck_id="d"))
    request = ModelRequest(model="kimi-k3", system="", messages=({"role": "user", "content": "Evidence"},),
                           max_tokens=2048, thinking=True, effort="low", request_kind="context_compaction")
    module.SkillRuntime._invoke_compaction_summary(fake, services, request, agent_id="a", role="slide", segment_id="s")
    assert calls[0].max_tokens == 16384 and request.max_tokens == 2048
    monkeypatch.setattr(module, "estimate_request_tokens", lambda *args: 120000)
    with pytest.raises(CompactionSummaryError, match="context budget"):
        module.SkillRuntime._invoke_compaction_summary(fake, services, request, agent_id="a", role="slide", segment_id="s")
    assert len(calls) == 1
