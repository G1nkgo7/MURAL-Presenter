import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from runtime.operator_metrics import append_operator_metrics
from runtime.session import ModelSession


def test_allowlist_append_and_actual_success_usage(tmp_path):
    path = tmp_path / "metrics.jsonl"
    fields = dict(attempt_id="attempt-1", channel_id="anonymous-1", model="model",
                  request_kind="main", effective_effort="high", status_code=200,
                  retryable=False, elapsed_s=1.0, first_byte_s=0.1, queue_wait_s=0.2,
                  resource="model", secret="SECRET", raw_response="SECRET",
                  content="SECRET", reasoning="SECRET", toolargs="SECRET",
                  response_diagnostics={"output_tokens": 12, "blocks": ["SECRET"]})
    append_operator_metrics(path, "provider_attempt_started", fields)
    append_operator_metrics(path, "provider_attempt_succeeded", fields)
    raw = path.read_text()
    records = [json.loads(line) for line in raw.splitlines()]
    assert len(records) == 2
    assert "SECRET" not in raw
    assert records[1]["usage"] == {"output_tokens": 12}
    assert records[0]["effort"] == "high"
    assert type(records[0]["ts"]) is float
    assert set(records[1]) == {"ts", "event", "attempt_id", "channel_id", "model",
        "request_kind", "effort", "status_code", "retryable", "elapsed_s",
        "first_byte_s", "queue_wait_s", "resource", "usage"}


def test_normalized_usage_is_whitelisted(tmp_path):
    path = tmp_path / "metrics.jsonl"
    append_operator_metrics(path, "provider_attempt_succeeded", {"usage": {
        "input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 3,
        "cache_creation_input_tokens": 4, "raw": "SECRET"}})
    assert json.loads(path.read_text())["usage"] == {
        "input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 3,
        "cache_creation_input_tokens": 4}


@pytest.mark.parametrize("event", ["content_delta", "reasoning_delta", "raw_response"])
def test_uninterested_events_no_output(tmp_path, event):
    path = tmp_path / "metrics.jsonl"
    append_operator_metrics(path, event, {"content": "SECRET"})
    assert not path.exists()


def test_no_setting_and_unsafe_paths_no_output(tmp_path):
    with patch("runtime.operator_metrics.os.open") as opened:
        append_operator_metrics(None, "provider_attempt_started", {})
        append_operator_metrics("relative.jsonl", "provider_attempt_started", {})
        append_operator_metrics(tmp_path / "trace" / "metrics.jsonl",
                                "provider_attempt_started", {}, trace_root=tmp_path / "trace")
        opened.assert_not_called()


@pytest.mark.parametrize("failure", ["open", "write", "close"])
def test_io_failure_never_raises(tmp_path, failure):
    with patch("runtime.operator_metrics.os.open", return_value=17) as opened, \
         patch("runtime.operator_metrics.os.write") as written, \
         patch("runtime.operator_metrics.os.close") as closed:
        {"open": opened, "write": written, "close": closed}[failure].side_effect = OSError("fixture")
        append_operator_metrics(tmp_path / "metrics.jsonl", "provider_attempt_failed", {})
        if failure != "open":
            closed.assert_called_once_with(17)


def test_single_bounded_append_write(tmp_path):
    with patch("runtime.operator_metrics.os.open", return_value=17) as opened, \
         patch("runtime.operator_metrics.os.write") as written, \
         patch("runtime.operator_metrics.os.close") as closed:
        append_operator_metrics(tmp_path / "metrics.jsonl", "provider_attempt_started", {})
        import os
        assert opened.call_args.args[1] & os.O_APPEND
        written.assert_called_once()
        assert len(written.call_args.args[1]) <= 2048
        closed.assert_called_once_with(17)
        written.reset_mock()
        append_operator_metrics(tmp_path / "metrics.jsonl", "provider_attempt_started",
                                {name: "\\" * 256 for name in
                                 ("attempt_id", "channel_id", "model", "request_kind", "error_kind", "resource")})
        written.assert_not_called()


def test_emit_without_sink_and_disabled(tmp_path):
    path = tmp_path / "metrics.jsonl"
    session = SimpleNamespace(_operator_metrics_path=str(path), trace_root=None, event_sink=None)
    ModelSession._emit(session, "provider_attempt_succeeded",
                       response_diagnostics={"output_tokens": 7, "blocks": ["SECRET"]})
    assert json.loads(path.read_text())["usage"] == {"output_tokens": 7}
    session._operator_metrics_path = None
    ModelSession._emit(session, "provider_attempt_started", secret="SECRET")
    assert len(path.read_text().splitlines()) == 1


def test_constructor_retains_only_path_from_explicit_environment(tmp_path, monkeypatch):
    path = tmp_path / "metrics.jsonl"
    monkeypatch.setenv("MODEL_OPERATOR_METRICS_PATH", str(tmp_path / "wrong.jsonl"))
    environment = {"MODEL_OPERATOR_METRICS_PATH": str(path), "SECRET": "SECRET"}
    settings = SimpleNamespace(prompt_cache=False, channel_cooldown_s=0)
    with patch("runtime.session.ChannelPool"):
        session = ModelSession(settings=settings, channels=(), transport=object(),
                               provider_health=object(), environment=environment)
        session._emit("provider_attempt_started", attempt_id="a")
        assert session._operator_metrics_path == str(path)
        assert not any(value is environment for value in vars(session).values())
        disabled = ModelSession(settings=settings, channels=(), transport=object(),
                                provider_health=object(), environment={})
        disabled._emit("provider_attempt_started")
        assert disabled._operator_metrics_path is None
    assert path.exists()
    assert not (tmp_path / "wrong.jsonl").exists()


def test_mock_provider_success_records_all_actual_counters(tmp_path):
    from runtime.provider_health import ProviderHealthStore
    from runtime.session import ModelRequest, ProviderChannel
    from test_agent_runtime import ScriptedTransport, _response, _settings, _text

    path = tmp_path / "metrics.jsonl"
    response = _response(_text("SECRET CONTENT"))
    response.usage.cache_read_input_tokens = 5
    response.usage.cache_creation_input_tokens = 7
    transport = ScriptedTransport([response])
    events = []
    session = ModelSession(settings=_settings(),
        channels=(ProviderChannel("SECRET KEY", "anonymous"),), transport=transport,
        provider_health=ProviderHealthStore(tmp_path / "health"), event_sink=events.append,
        environment={"MODEL_OPERATOR_METRICS_PATH": str(path)})
    result = session.call(ModelRequest(model="model-test", system="SECRET SYSTEM",
        messages=({"role": "user", "content": "SECRET PROMPT"},), max_tokens=128))
    assert result.ok and result.response is response
    records = [json.loads(line) for line in path.read_text().splitlines()]
    success = next(record for record in records if record["event"] == "provider_attempt_succeeded")
    assert success["usage"] == {"input_tokens": 3, "output_tokens": 2,
        "cache_read_input_tokens": 5, "cache_creation_input_tokens": 7}
    assert next(event for event in events if event["event_type"] ==
                "provider_attempt_succeeded")["usage"] == success["usage"]
    assert "SECRET" not in path.read_text()
    assert "MODEL_OPERATOR_METRICS_PATH" not in transport.calls[0][1].anthropic_kwargs(_settings())
