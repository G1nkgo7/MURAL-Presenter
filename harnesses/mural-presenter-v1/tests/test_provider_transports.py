from __future__ import annotations

import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from runtime.config import ProviderSettings, resolve_api_format
from runtime.recovery import ProviderRequestTimeout
from runtime.session import (
    AnthropicTransport,
    ModelRequest,
    ModelSession,
    NormalizedAssistantResponse,
    OpenAIChatCompatibleTransport,
    OpenAIResponsesTransport,
    ProviderChannel,
    ProviderProtocolError,
    _normalize_anthropic_response,
    _normalize_openai_response,
    _normalize_openai_responses_response,
    create_transport,
    normalize_openai_chat_url,
    normalize_openai_responses_url,
    openai_chat_body,
    openai_responses_body,
    to_openai_messages,
    to_openai_responses_input,
)


def _settings(
    api_format: str,
    *,
    streaming: bool = True,
    model: str = "shared-model",
) -> ProviderSettings:
    return ProviderSettings(
        api_format=api_format,
        model=model,
        base_url="https://provider.example",
        streaming=streaming,
        prompt_cache=False,
        prompt_cache_messages=False,
    )


def _request(model: str, *, thinking: bool, effort: str = "high") -> ModelRequest:
    return ModelRequest(
        model=model,
        system="system",
        messages=({"role": "user", "content": "continue"},),
        max_tokens=64,
        thinking=thinking,
        effort=effort,
    )


class _AsyncClient(SimpleNamespace):
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


@pytest.mark.parametrize("field", ["content", "reasoning", "reasoning_content"])
def test_inline_tool_examples_remain_text_and_never_become_actions(field):
    example = 'Example only: <function=write_file><parameter=path>deck.md</parameter></function>'
    response = _normalize_openai_response({"choices": [{"message": {field: example}, "finish_reason": "stop"}]})
    assert response.tool_calls == ()
    expected = {"type": "text", "text": example} if field == "content" else {
        "type": "thinking", "thinking": example, "signature": None}
    assert response.content == (expected,)


@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize("arguments", ['{"path":"PRIVATE_ARGUMENT",', "", "null", "[]", '"text"',
                                       '{"x":NaN}', '{"x":Infinity}', {}, None])
def test_invalid_native_tool_arguments_are_protocol_errors(protocol, arguments):
    with pytest.raises(ProviderProtocolError) as failure:
        if protocol == "chat":
            _normalize_openai_response({"choices": [{"message": {"tool_calls": [{"id": "t1", "type": "function",
                "function": {"name": "write_file", "arguments": arguments}}]}}]})
        else:
            _normalize_openai_responses_response({"output": [{"type": "function_call", "call_id": "t1",
                "name": "write_file", "arguments": arguments}]})
    assert "PRIVATE_ARGUMENT" not in str(failure.value)


@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic"])
def test_only_native_tool_protocol_produces_paired_action_identifiers(protocol):
    arguments = {"path": "deck.md", "content": "# Deck"}
    example = '<function=delete_file>not an action</function>'
    if protocol == "chat":
        response = _normalize_openai_response({"choices": [{"message": {"content": example,
            "tool_calls": [{"id": "t1", "type": "function", "function": {
                "name": "write_file", "arguments": json.dumps(arguments)}}]}}]})
    elif protocol == "responses":
        response = _normalize_openai_responses_response({"output": [
            {"type": "message", "content": [{"type": "output_text", "text": example}]},
            {"type": "function_call", "call_id": "t1", "name": "write_file", "arguments": json.dumps(arguments)}]})
    else:
        response = _normalize_anthropic_response(SimpleNamespace(content=[
            {"type": "text", "text": example},
            {"type": "tool_use", "id": "t1", "name": "write_file", "input": arguments}]))
    assert response.tool_calls == ({"type": "tool_use", "id": "t1", "name": "write_file", "input": arguments},)
    assert {"type": "text", "text": example} in response.content


@pytest.mark.parametrize("arguments", [None, [], "{broken JSON}"])
def test_anthropic_tool_input_must_already_be_a_native_object(arguments):
    with pytest.raises(ProviderProtocolError, match="tool_use.input"):
        _normalize_anthropic_response(SimpleNamespace(content=[
            {"type": "tool_use", "id": "t1", "name": "write_file", "input": arguments}]))


def test_three_api_formats_create_a_transport_for_the_same_session_contract() -> None:
    anthropic_transport = create_transport(_settings("anthropic"))
    responses_transport = create_transport(_settings("openai_responses"))
    chat_transport = create_transport(_settings("openai_chat_compatible"))
    assert isinstance(anthropic_transport, AnthropicTransport)
    assert isinstance(responses_transport, OpenAIResponsesTransport)
    assert isinstance(chat_transport, OpenAIChatCompatibleTransport)


@pytest.mark.parametrize("role", ["orchestrator", "research", "image", "slide"])
def test_actual_role_catalog_uses_supported_anthropic_schema_roots(role):
    from runtime.tools import ToolRuntime, register_tools, tool_specs

    runtime = ToolRuntime()
    register_tools(runtime, {name: lambda *_: None for name in tool_specs()})
    request = replace(_request("claude-opus-4-7-thinking", thinking=True, effort="xhigh"),
                      tools=tuple(runtime.advertised_tools(role)))

    async def create(**kwargs):
        assert kwargs["thinking"]["type"] == "adaptive"
        assert kwargs["output_config"] == {"effort": "xhigh"}
        for tool in kwargs["tools"]:
            schema = tool["input_schema"]
            assert schema["type"] == "object"
            assert not {"oneOf", "allOf", "anyOf"}.intersection(schema), tool["name"]
        inspect = next(tool for tool in kwargs["tools"] if tool["name"] == "inspect")
        assert {"page", "path", "question"} <= inspect["input_schema"]["properties"].keys()
        return SimpleNamespace(content=[{"type": "tool_use", "id": "inspect-1",
            "name": "inspect", "input": {"page": [1, 2]}}], stop_reason="tool_use")

    transport = AnthropicTransport(_settings("anthropic", streaming=False))
    transport._client = lambda _channel: _AsyncClient(messages=SimpleNamespace(create=create))
    result = transport.complete(ProviderChannel("fixture", "local"), request, total_timeout_s=30)
    assert result.message.tool_calls[0]["input"] == {"page": [1, 2]}


@pytest.mark.parametrize("api_format, payload", [
    ("openai_chat_compatible", {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}),
    ("openai_responses", {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]}),
])
def test_long_response_budget_keeps_connection_timeout_independent(api_format, payload):
    captured = {}

    def handler(request):
        captured.update(request.extensions["timeout"])
        return httpx.Response(200, json=payload)

    settings = ProviderSettings(
        api_format=api_format, model="shared-model", base_url="https://provider.example",
        total_timeout_s=3600, idle_timeout_s=3600, connect_timeout_s=60, streaming=False,
    )
    transport = create_transport(settings, client_factory=lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))
    transport.complete(ProviderChannel("test-only"), _request("shared-model", thinking=False),
                       total_timeout_s=3600)
    assert captured["read"] == 3600
    assert captured["connect"] == 60


@pytest.mark.parametrize("api_format", ["openai_chat_compatible", "openai_responses"])
@pytest.mark.parametrize("streaming", [False, True])
def test_actual_openai_http_timeout_distinguishes_nonstream_generation_from_stream_idle(api_format, streaming):
    captured = {}
    if api_format == "openai_chat_compatible":
        payload = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        events = [{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}]
    else:
        payload = {"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]}
        events = [{"type": "response.completed", "response": payload}]

    def handler(request):
        captured.update(request.extensions["timeout"])
        assert bool(json.loads(request.content).get("stream")) is streaming
        if not streaming:
            return httpx.Response(200, json=payload)
        data = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
        return httpx.Response(200, text=data, headers={"content-type": "text/event-stream"})

    settings = replace(_settings(api_format, streaming=streaming),
                       total_timeout_s=600, idle_timeout_s=120, connect_timeout_s=30)
    transport = create_transport(settings, client_factory=lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))
    result = transport.complete(ProviderChannel("fixture"), _request("shared-model", thinking=False),
                                total_timeout_s=600)
    assert captured["read"] == (120 if streaming else 600)
    assert captured["connect"] == 30
    assert result.message.content == ({"type": "text", "text": "ok"},)


def test_legacy_openai_api_format_resolves_to_chat_compatible() -> None:
    # 旧部署里的 ``openai`` 值必须继续可用,映射到自托管 Chat 兼容格式,
    # 保持既有行为不变(刻意保留的配置别名)。
    assert resolve_api_format("openai") == "openai_chat_compatible"
    assert resolve_api_format("OpenAI") == "openai_chat_compatible"
    settings = _settings("openai")
    assert settings.api_format == "openai_chat_compatible"
    assert isinstance(
        create_transport(settings), OpenAIChatCompatibleTransport
    )
    from_env = ProviderSettings.from_environment(
        {
            "MODEL_API_FORMAT": "openai",
            "MODEL": "shared-model",
            "MODEL_BASE_URL": "https://provider.example",
        }
    )
    assert from_env.api_format == "openai_chat_compatible"


def test_unknown_api_format_is_rejected() -> None:
    with pytest.raises(ValueError, match="MODEL_API_FORMAT"):
        _settings("gemini")


def test_responses_transports_cache_key_only_as_api_parameter_and_preserves_cache_writes():
    model = "gpt-6-astra/azure/sfa"
    settings = _settings("openai_responses", streaming=False, model=model)
    request = replace(_request(model, thinking=True), prompt_cache_key="transport-private-cache-key")
    seen = []

    def handler(incoming):
        body = json.loads(incoming.content)
        seen.append(body)
        return httpx.Response(200, json={"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            "usage": {"input_tokens": 45472, "output_tokens": 2,
                      "input_tokens_details": {"cached_tokens": 7462, "cache_write_tokens": 38007}}})

    transport = create_transport(settings, client_factory=lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))
    result = transport.complete(ProviderChannel("fixture", "test"), request, total_timeout_s=30)
    assert seen[0]["prompt_cache_key"] == request.prompt_cache_key
    assert request.prompt_cache_key not in json.dumps(seen[0]["input"]) + seen[0]["instructions"]
    assert result.message.usage.cache_creation_input_tokens == 38007
    assert result.message.usage.cache_read_input_tokens == 7462
    assert "prompt_cache_key" not in openai_chat_body(request)
    assert "prompt_cache_key" not in request.anthropic_kwargs(settings)


def test_reasoning_payloads_are_model_specific() -> None:
    deepseek = openai_chat_body(_request("deepseek-v4", thinking=True))
    qwen = openai_chat_body(_request("qwen3.5-27b", thinking=False))
    openai = openai_chat_body(
        _request("gpt-5.1", thinking=True), reasoning_transport="reasoning_effort"
    )
    generic = openai_chat_body(_request("generic-chat", thinking=True))

    assert deepseek["thinking"] == {"type": "enabled"}
    assert "chat_template_kwargs" not in deepseek
    assert "reasoning_effort" not in deepseek
    assert qwen["chat_template_kwargs"] == {"enable_thinking": False}
    assert "thinking" not in qwen and "reasoning_effort" not in qwen
    assert openai["reasoning_effort"] == "high"
    assert "thinking" not in openai and "chat_template_kwargs" not in openai
    assert not {
        "thinking",
        "chat_template_kwargs",
        "reasoning_effort",
    }.intersection(generic)


@pytest.mark.parametrize("thinking", [False, True])
@pytest.mark.parametrize("effort", ["low", "medium", "xhigh"])
@pytest.mark.parametrize("transport", ["", "chat_template_kwargs", "qwen"])
def test_qwen38_switch_and_effort_are_both_serialized(thinking, effort, transport):
    wire = json.loads(json.dumps(openai_chat_body(
        _request("Qwen/Qwen3.8-27B", thinking=thinking, effort=effort), reasoning_transport=transport)))
    assert wire["chat_template_kwargs"] == {
        "enable_thinking": thinking, **({"reasoning_effort": effort} if thinking else {})}
    assert wire.get("reasoning_effort") == (effort if thinking and effort != "xhigh" else None)
    assert "thinking" not in wire and "output_config" not in wire


@pytest.mark.parametrize("effort", ["high", "max", "unsupported"])
def test_qwen38_invalid_effort_fails_before_request(effort):
    with pytest.raises(ValueError, match="reasoning effort must be"):
        openai_chat_body(_request("Qwen3.8-27B", thinking=True, effort=effort))


@pytest.mark.parametrize("effort,expected", [(" MEDIUM ", "medium"), ("", "xhigh")])
def test_qwen38_template_effort_uses_the_validated_value_or_model_default(effort, expected):
    wire = openai_chat_body(_request("Qwen3.8-27B", thinking=True, effort=effort))
    assert wire["chat_template_kwargs"]["reasoning_effort"] == expected
    assert wire.get("reasoning_effort") == (expected if expected != "xhigh" else None)


def test_qwen38_transport_override_cannot_drop_the_thinking_switch():
    with pytest.raises(ValueError, match="requires chat_template_kwargs"):
        openai_chat_body(_request("Qwen3.8-27B", thinking=False, effort="medium"), reasoning_transport="reasoning_effort")


@pytest.mark.parametrize("model", ["Qwen3.5-27B", "Qwen3.6-27B", "Qwen3.8-8B", "qwq-32b"])
def test_other_qwen_models_do_not_inherit_38_effort(model):
    wire = openai_chat_body(_request(model, thinking=True, effort="medium"))
    assert wire["chat_template_kwargs"] == {"enable_thinking": True}
    assert "reasoning_effort" not in wire


def test_chat_compatible_endpoint_rejects_a_responses_base_url() -> None:
    # 自托管 Chat 兼容端点不接受 /responses 路径(官方 Responses 走独立 transport)。
    with pytest.raises(ValueError, match="Responses"):
        normalize_openai_chat_url("https://api.example/v1/responses")
    with pytest.raises(ValueError, match="openai_responses"):
        openai_chat_body(
            _request("gpt-5.1", thinking=True),
            reasoning_transport="openai_responses",
        )


@pytest.mark.parametrize("effort", ["xhigh", "max"])
def test_openai_responses_supports_gpt6_astra_reasoning(effort: str) -> None:
    wire = openai_responses_body(
        _request("gpt-6-astra/azure/sfa", thinking=True, effort=effort)
    )

    assert wire["model"] == "gpt-6-astra/azure/sfa"
    assert wire["reasoning"] == {"summary": "auto", "effort": effort}


@pytest.mark.parametrize("role", ["orchestrator", "research", "image", "slide", "critic"])
def test_responses_preserves_optional_fields_in_actual_role_catalogs(role):
    from runtime.tools import ToolRuntime, register_tools, tool_specs

    runtime = ToolRuntime()
    register_tools(runtime, {name: lambda *_: None for name in tool_specs()})
    tools = tuple(runtime.advertised_tools(role))
    original = copy.deepcopy(tools)
    opus = replace(_request("claude-opus-5", thinking=True), tools=tools)
    qwen = replace(_request("Qwen3.8-27B", thinking=True, effort="medium"), tools=tools)
    opus_before = opus.anthropic_kwargs(_settings("anthropic"))
    qwen_before = openai_chat_body(qwen)

    wire = openai_responses_body(replace(
        _request("gpt-6-astra/azure/sfa", thinking=True), tools=tools))

    assert len(wire["tools"]) == len(original) > 0
    for actual, native in zip(wire["tools"], original):
        assert actual["strict"] is False
        assert actual["parameters"] == native["input_schema"]
    inspect = next(tool for tool in wire["tools"] if tool["name"] == "inspect")
    assert inspect["parameters"]["required"] == []
    assert wire["reasoning"] == {"summary": "auto", "effort": "high"}
    assert tools == original
    assert opus.anthropic_kwargs(_settings("anthropic")) == opus_before
    assert openai_chat_body(qwen) == qwen_before
    assert all("strict" not in tool for tool in opus_before["tools"])
    assert all("strict" not in tool["function"] for tool in qwen_before["tools"])


@pytest.mark.parametrize("arguments", [{"page": [1]}, {"path": "assets/photo.png"}])
def test_responses_transport_round_trips_inspect_with_only_one_target(arguments):
    from runtime.tools import ToolRuntime, register_tools, tool_specs

    runtime = ToolRuntime()
    register_tools(runtime, {name: lambda *_: None for name in tool_specs()})
    tool = next(tool for tool in runtime.advertised_tools("orchestrator")
                if tool["name"] == "inspect")

    def handler(http_request):
        body = json.loads(http_request.content)
        advertised = body["tools"][0]
        assert advertised["strict"] is False
        assert advertised["parameters"] == tool["input_schema"]
        return httpx.Response(200, json={"status": "completed", "output": [
            {"type": "function_call", "call_id": "inspect-one-target",
             "name": "inspect", "arguments": json.dumps(arguments)}]})

    transport = create_transport(
        _settings("openai_responses", streaming=False),
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    request = replace(_request("gpt-6-astra/azure/sfa", thinking=True), tools=(tool,))
    response = transport.complete(ProviderChannel("fixture"), request, total_timeout_s=30)
    assert response.message.tool_calls == ({"type": "tool_use", "id": "inspect-one-target",
                                           "name": "inspect", "input": arguments},)


def test_orphan_compacted_tool_result_is_demoted_to_a_user_message() -> None:
    converted = to_openai_messages(
        "system",
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "old-tool",
                        "content": "durable evidence",
                    }
                ],
            }
        ],
    )
    assert [message["role"] for message in converted] == ["system", "user"]
    assert "Recovered result" in repr(converted[-1]["content"])
    assert "durable evidence" in repr(converted[-1]["content"])


def test_anthropic_stream_round_trips_a_complete_message() -> None:
    final = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="done")],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=11,
            output_tokens=7,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )

    class Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def __aiter__(self):
            yield SimpleNamespace(type="message_start")
            yield SimpleNamespace(type="content_block_delta")

        async def get_final_message(self):
            return final

    captured: dict[str, object] = {}

    def stream(**kwargs):
        captured.update(kwargs)
        return Stream()

    transport = AnthropicTransport(replace(_settings("anthropic"), return_token_ids=True))
    transport._client = lambda _channel: _AsyncClient(  # type: ignore[method-assign]
        messages=SimpleNamespace(stream=stream)
    )
    result = transport.complete(
        ProviderChannel("secret-a", "ch-a"),
        _request("shared-model", thinking=False),
        total_timeout_s=30,
    )
    assert isinstance(result.message, NormalizedAssistantResponse)
    assert result.message.stop_reason == "end_turn"
    assert [block["type"] for block in result.message.content] == ["text"]
    assert result.message.content[0]["text"] == "done"
    assert result.message.analysis_summaries == ()
    assert result.message.usage.input_tokens == 11
    assert captured["model"] == "shared-model"
    assert "return_token_ids" not in captured
    assert result.first_byte_s >= 0


def test_anthropic_nonstreaming_first_byte_is_unknown_not_total_duration():
    async def create(**kwargs):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="done")],
            stop_reason="end_turn", usage=SimpleNamespace(input_tokens=1, output_tokens=1))
    transport = AnthropicTransport(_settings("anthropic", streaming=False))
    transport._client = lambda _: _AsyncClient(messages=SimpleNamespace(create=create))
    result = transport.complete(ProviderChannel("fixture", "local"),
                                _request("shared-model", thinking=False), total_timeout_s=30)
    assert result.first_byte_s is None
    assert result.message.content[0]["text"] == "done"


def test_openai_stream_round_trips_tools_usage_and_cache_counts() -> None:
    captured: dict[str, object] = {}
    events = [
        {
            "choices": [
                {
                    "delta": {
                        "reasoning_content": "think ",
                        "content": "working",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "tool-1",
                                "function": {
                                    "name": "render",
                                    "arguments": '{"pages":',
                                },
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "reasoning_content": "then act",
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": "[2]}"}}
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "prompt_tokens_details": {"cached_tokens": 5},
            },
        },
    ]
    lines = [
        f"data: {json.dumps(event)}\n\n".encode("utf-8") for event in events
    ] + [b"data: [DONE]\n\n"]

    def stream(method, endpoint, *, content, headers, timeout):
        assert method == "POST"
        captured["url"] = endpoint
        captured["authorization"] = httpx.Headers(headers)["Authorization"]
        captured["body"] = json.loads(content)
        captured["timeout"] = timeout
        return _Body(lines=lines)

    settings = _settings("openai_chat_compatible", model="qwen3.5-27b")
    transport = OpenAIChatCompatibleTransport(settings, client_factory=lambda: _AsyncClient(stream=stream))
    request = ModelRequest(
        model=settings.model,
        system="system",
        messages=({"role": "user", "content": "render"},),
        tools=(
            {
                "name": "render",
                "description": "render pages",
                "input_schema": {"type": "object", "properties": {}},
            },
        ),
        thinking=True,
        max_tokens=64,
    )
    result = transport.complete(
        ProviderChannel("shared-secret", "ch-shared"),
        request,
        total_timeout_s=30,
    )
    assert captured["url"] == "https://provider.example/v1/chat/completions"
    assert captured["authorization"] == "Bearer shared-secret"
    assert captured["body"]["stream"] is True
    assert captured["body"]["stream_options"] == {"include_usage": True}
    assert "return_token_ids" not in captured["body"]
    assert captured["body"]["chat_template_kwargs"] == {"enable_thinking": True}
    assert result.message.stop_reason == "tool_use"
    # 自托管兼容端点没有可公开的分析摘要;raw reasoning 只是内部 thinking 分块。
    assert result.message.analysis_summaries == ()
    tool = next(
        block for block in result.message.content if block["type"] == "tool_use"
    )
    assert tool["name"] == "render"
    assert tool["input"] == {"pages": [2]}
    assert result.message.usage.cache_read_input_tokens == 5


def test_openai_non_streaming_remains_supported() -> None:
    captured: dict[str, object] = {}

    def stream(method, endpoint, *, content, headers, timeout):
        assert method == "POST"
        captured["body"] = json.loads(content)
        return _Body(body=json.dumps({
            "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        }).encode("utf-8"))

    transport = OpenAIChatCompatibleTransport(
        _settings("openai_chat_compatible", streaming=False), client_factory=lambda: _AsyncClient(stream=stream)
    )
    result = transport.complete(
        ProviderChannel("shared-secret", "ch-shared"),
        _request("shared-model", thinking=False),
        total_timeout_s=30,
    )
    assert "stream" not in captured["body"]
    assert result.message.content[0]["text"] == "done"


@pytest.mark.parametrize("repeated", [False, True])
def test_malformed_chat_stream_has_bounded_protocol_recovery(repeated):
    calls = []
    partial = {"choices": [{"delta": {"reasoning_content": "actual partial reasoning"}}]}
    complete = {"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]}

    def handler(request):
        calls.append(json.loads(request.content))
        if repeated or len(calls) == 1:
            content = b"".join(_sse([partial])[:-1]) + b'data: {"id":"TRUNCATED_PRIVATE_PAYLOAD\n\n'
        else:
            content = b"".join(_sse([complete]))
        return httpx.Response(200, content=content, headers={"content-type": "text/event-stream"})

    settings = replace(_settings("openai_chat_compatible", model="kimi-k3"),
                       max_attempts=2, total_timeout_s=600)
    transport = OpenAIChatCompatibleTransport(settings,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    session = ModelSession(settings=settings, channels=(ProviderChannel("fixture", "ch-a"),),
                           transport=transport, sleeper=lambda _delay: None)
    result = session.call(_request("kimi-k3", thinking=True), request_budget_s=600)
    assert len(calls) == result.attempts == 2
    assert all(call["reasoning_effort"] == "high" for call in calls)
    assert calls[0]["messages"] == calls[1]["messages"]
    assert result.ok is not repeated
    if repeated:
        assert result.failure.kind == "provider_protocol_error"
        assert "TRUNCATED_PRIVATE_PAYLOAD" not in result.failure.detail
        assert result.response is None
    else:
        assert list(result.response.content) == [{"type": "text", "text": "done"}]


@pytest.mark.parametrize("api_format", ["openai_chat_compatible", "openai_responses"])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_token_ids_opt_in_only_changes_streaming_chat_wire_body(api_format, streaming, enabled):
    captured = {}
    chat = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
    response = {"status": "completed", "output": [{"type": "message", "content": [
        {"type": "output_text", "text": "ok"}]}]}
    def handler(request):
        captured.update(json.loads(request.content))
        if not streaming:
            return httpx.Response(200, json=chat if api_format == "openai_chat_compatible" else response)
        events = ([{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}]
            if api_format == "openai_chat_compatible" else [{"type": "response.completed", "response": response}])
        return httpx.Response(200, content=b"".join(_sse(events)), headers={"content-type": "text/event-stream"})
    settings = replace(_settings(api_format, streaming=streaming), return_token_ids=enabled)
    transport = create_transport(settings, client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result = transport.complete(ProviderChannel("fixture"), _request("shared-model", thinking=False), total_timeout_s=30)
    assert result.message.content[0]["text"] == "ok"
    assert captured.get("return_token_ids") is (True if enabled and streaming and api_format == "openai_chat_compatible" else None)


def test_token_only_sse_preserves_long_native_tool_arguments_without_fake_actions():
    arguments = {"path": "slides/slide_01.html", "content": "<p>中文、quoted: \"value\"</p>\n" * 800}
    argument_json = json.dumps(arguments, ensure_ascii=False)
    split = argument_json.index(', "content"')
    events = [
        {"prompt_token_ids": [1001], "choices": []},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "write-1", "function": {
            "name": "write_file", "arguments": argument_json[:split]}}]}}]},
        {"choices": [{"delta": None, "token_ids": [1002]}]},
        {"choices": [{"delta": {}, "token_ids": [1003, 1004]}]},
        {"choices": [{"token_ids": [1005]}]},
        {"choices": [{"delta": {"content": None, "tool_calls": []}, "token_ids": [1006]}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {
            "arguments": argument_json[split:]}}]}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 8000}},
    ]
    captured = {}
    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, content=b"".join(_sse(events)), headers={"content-type": "text/event-stream"})
    settings = replace(_settings("openai_chat_compatible", model="Qwen3.8-27B"), return_token_ids=True)
    transport = create_transport(settings, client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result = transport.complete(ProviderChannel("fixture"), _request(settings.model, thinking=True, effort="medium"), total_timeout_s=30)
    assert captured["return_token_ids"] is True
    assert captured["reasoning_effort"] == captured["chat_template_kwargs"]["reasoning_effort"] == "medium"
    assert result.message.stop_reason == "tool_use"
    assert result.message.content == ({"type": "tool_use", "id": "write-1", "name": "write_file", "input": arguments},)
    assert result.message.usage.output_tokens == 8000


@pytest.mark.parametrize("reasoning", [None, "still thinking"])
def test_token_only_length_response_does_not_become_a_tool_success(reasoning):
    events = [{"choices": [{"delta": {"reasoning_content": reasoning}, "token_ids": [21, 22], "finish_reason": "length"}]}]
    transport = create_transport(replace(_settings("openai_chat_compatible"), return_token_ids=True),
        client_factory=lambda: _AsyncClient(stream=lambda *_a, **_kw: _Body(lines=_sse(events))))
    result = transport.complete(ProviderChannel("fixture"), _request("shared-model", thinking=False), total_timeout_s=30)
    assert result.message.stop_reason == "max_tokens"
    assert not result.message.tool_calls


@pytest.mark.parametrize("complete_arguments,finish,done", [
    (False, True, True), (True, False, True), (True, True, False), (True, False, False),
])
def test_token_heartbeat_cannot_make_an_incomplete_tool_stream_executable(complete_arguments, finish, done):
    arguments = '{"path":"deck.md","content":"complete"}' if complete_arguments else '{"path":"deck.md","content":'
    events = [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "t1", "function": {"name": "write_file", "arguments": arguments}}]}}]},
        {"choices": [{"delta": {}, "token_ids": [10], "finish_reason": "tool_calls" if finish else None}]},
    ]
    lines = _sse(events)
    if not done:
        lines.pop()
    transport = create_transport(replace(_settings("openai_chat_compatible"), return_token_ids=True),
        client_factory=lambda: _AsyncClient(stream=lambda *_a, **_kw: _Body(lines=lines)))
    with pytest.raises(ProviderProtocolError):
        transport.complete(ProviderChannel("fixture"), _request("shared-model", thinking=False), total_timeout_s=30)


@pytest.mark.parametrize(
    "api_format", ["anthropic", "openai_responses", "openai_chat_compatible"]
)
def test_streaming_can_be_enabled_for_all_api_formats(api_format: str) -> None:
    settings = ProviderSettings.from_environment(
        {
            "MODEL_API_FORMAT": api_format,
            "MODEL": "shared-model",
            "MODEL_BASE_URL": "https://provider.example",
            "MODEL_STREAMING": "1",
        }
    )
    assert settings.streaming is True


def test_anthropic_stream_timeout_is_stream_idle_after_the_first_event() -> None:
    request = httpx.Request("POST", "https://provider.example/v1/messages")

    class Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def __aiter__(self):
            yield SimpleNamespace(type="message_start")
            raise httpx.ReadTimeout("idle", request=request)

        async def get_final_message(self):
            raise AssertionError("unreachable")

    transport = AnthropicTransport(_settings("anthropic"))
    transport._client = lambda _channel: _AsyncClient(  # type: ignore[method-assign]
        messages=SimpleNamespace(stream=lambda **_kwargs: Stream())
    )
    with pytest.raises(ProviderRequestTimeout) as captured:
        transport.complete(
            ProviderChannel("secret-a", "ch-a"),
            _request("shared-model", thinking=False),
            total_timeout_s=30,
        )
    assert captured.value.phase == "stream_idle"


def test_openai_stream_timeout_is_stream_idle_after_the_first_event() -> None:
    request = httpx.Request("POST", "https://provider.example/v1/chat/completions")

    class Response(_Body):
        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"partial"}}]}'
            raise httpx.ReadTimeout("idle", request=request)

    transport = OpenAIChatCompatibleTransport(
        _settings("openai_chat_compatible"),
        client_factory=lambda: _AsyncClient(stream=lambda *_args, **_kwargs: Response()),
    )
    with pytest.raises(ProviderRequestTimeout) as captured:
        transport.complete(
            ProviderChannel("secret-a", "ch-a"),
            _request("shared-model", thinking=False),
            total_timeout_s=30,
        )
    assert captured.value.phase == "stream_idle"


# --------------------------------------------------------------------------- #
# OpenAI 官方 Responses API transport(独立 reasoning summary → analysis_summaries)
# --------------------------------------------------------------------------- #


class _Body:
    """模拟 HTTPX 异步响应，保持整段 JSON 与 SSE 两种真实读取接口。"""

    status_code = 200

    def __init__(self, *, body: bytes | None = None, lines: list[bytes] | None = None):
        self._body = body
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def raise_for_status(self):
        return self

    async def aread(self):
        assert self._body is not None
        return self._body

    async def aiter_lines(self):
        assert self._lines is not None
        for line in self._lines:
            yield line.decode("utf-8").rstrip("\r\n")


def _sse(events: list[dict[str, object]]) -> list[bytes]:
    return [
        f"data: {json.dumps(event)}\n\n".encode("utf-8") for event in events
    ] + [b"data: [DONE]\n\n"]


def test_responses_non_streaming_maps_reasoning_summary_to_analysis() -> None:
    captured: dict[str, object] = {}
    decoded = {
        "status": "completed",
        "output": [
            {
                "type": "reasoning",
                "summary": [
                    {"type": "summary_text", "text": "First I outline the plan."},
                    {"type": "summary_text", "text": "Then I draft the deck."},
                ],
                # raw reasoning / encrypted content that must NEVER leak:
                "content": [{"type": "reasoning_text", "text": "SECRET raw chain"}],
                "encrypted_content": "ENCRYPTED-BLOB",
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Here is the outline."}],
            },
        ],
        "usage": {
            "input_tokens": 20,
            "output_tokens": 8,
            "input_tokens_details": {"cached_tokens": 4},
        },
    }

    def stream(method, endpoint, *, content, headers, timeout):
        assert method == "POST"
        captured["url"] = endpoint
        captured["authorization"] = httpx.Headers(headers)["Authorization"]
        captured["body"] = json.loads(content)
        return _Body(body=json.dumps(decoded).encode("utf-8"))

    transport = OpenAIResponsesTransport(
        _settings("openai_responses", streaming=False, model="gpt-5.1"), client_factory=lambda: _AsyncClient(stream=stream)
    )
    request = ModelRequest(
        model="gpt-5.1",
        system="system",
        messages=({"role": "user", "content": "outline"},),
        max_tokens=64,
        thinking=True,
        effort="high",
    )
    result = transport.complete(
        ProviderChannel("gpt-secret", "ch-gpt"), request, total_timeout_s=30
    )
    message = result.message
    assert isinstance(message, NormalizedAssistantResponse)
    assert message.analysis_summaries == (
        "First I outline the plan.",
        "Then I draft the deck.",
    )
    assert [block["type"] for block in message.content] == ["text"]
    assert message.content[0]["text"] == "Here is the outline."
    assert message.stop_reason == "end_turn"
    assert message.usage.input_tokens == 20
    assert message.usage.cache_read_input_tokens == 4
    # official endpoint routing + reasoning summary requested
    assert captured["url"] == "https://provider.example/v1/responses"
    assert captured["authorization"] == "Bearer gpt-secret"
    assert captured["body"]["reasoning"] == {"summary": "auto", "effort": "high"}
    assert captured["body"]["instructions"] == "system"
    # raw reasoning / encrypted content never entered the public content or summaries
    blob = json.dumps(
        {"content": [dict(b) for b in message.content],
         "summaries": list(message.analysis_summaries)}
    )
    assert "SECRET raw chain" not in blob
    assert "ENCRYPTED-BLOB" not in blob


def test_responses_streaming_concats_summary_deltas() -> None:
    # 流式 reasoning summary delta 累积拼接成一条摘要;output_text delta 拼成可见文本。
    events = [
        {"type": "response.reasoning_summary_text.delta", "delta": "Plan "},
        {"type": "response.reasoning_summary_text.delta", "delta": "the "},
        {"type": "response.reasoning_summary_text.delta", "delta": "deck."},
        {"type": "response.output_text.delta", "delta": "Draft "},
        {"type": "response.output_text.delta", "delta": "ready."},
        {"type": "response.output_text.done"},  # no final object → reconstruct
    ]

    def stream(*_args, **_kwargs):
        return _Body(lines=_sse(events))

    transport = OpenAIResponsesTransport(
        _settings("openai_responses", model="gpt-5.1"), client_factory=lambda: _AsyncClient(stream=stream)
    )
    request = ModelRequest(
        model="gpt-5.1",
        system="system",
        messages=({"role": "user", "content": "outline"},),
        max_tokens=64,
        thinking=True,
    )
    result = transport.complete(
        ProviderChannel("gpt-secret", "ch-gpt"), request, total_timeout_s=30
    )
    message = result.message
    assert message.analysis_summaries == ("Plan the deck.",)
    assert message.content[0]["text"] == "Draft ready."
    assert message.stop_reason == "end_turn"


def test_responses_dedups_identical_summary_parts() -> None:
    # 终局响应对象里若出现重复摘要片段,归一层按出现顺序去重。
    decoded = {
        "status": "completed",
        "output": [
            {
                "type": "reasoning",
                "summary": [
                    {"type": "summary_text", "text": "Plan the deck."},
                    {"type": "summary_text", "text": "Plan the deck."},
                    {"type": "summary_text", "text": "Then verify."},
                ],
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Done."}],
            },
        ],
        "usage": {"input_tokens": 5, "output_tokens": 2},
    }

    def stream(*_args, **_kwargs):
        return _Body(body=json.dumps(decoded).encode("utf-8"))

    transport = OpenAIResponsesTransport(
        _settings("openai_responses", streaming=False, model="gpt-5.1"), client_factory=lambda: _AsyncClient(stream=stream)
    )
    result = transport.complete(
        ProviderChannel("gpt-secret", "ch-gpt"),
        _request("gpt-5.1", thinking=True),
        total_timeout_s=30,
    )
    assert result.message.analysis_summaries == ("Plan the deck.", "Then verify.")


def test_responses_streaming_prefers_final_response_object() -> None:
    final = {
        "status": "completed",
        "output": [
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "Concise plan."}],
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Done."}],
            },
        ],
        "usage": {"input_tokens": 5, "output_tokens": 2},
    }
    events = [
        {"type": "response.reasoning_summary_text.delta", "delta": "Conc"},
        {"type": "response.output_text.delta", "delta": "Do"},
        {"type": "response.completed", "response": final},
    ]

    def stream(*_args, **_kwargs):
        return _Body(lines=_sse(events))

    transport = OpenAIResponsesTransport(
        _settings("openai_responses", model="gpt-5.1"), client_factory=lambda: _AsyncClient(stream=stream)
    )
    result = transport.complete(
        ProviderChannel("gpt-secret", "ch-gpt"),
        _request("gpt-5.1", thinking=True),
        total_timeout_s=30,
    )
    # The authoritative final object wins over the partial deltas.
    assert result.message.analysis_summaries == ("Concise plan.",)
    assert result.message.content[0]["text"] == "Done."


def test_responses_streaming_maps_function_call_to_tool_use() -> None:
    events = [
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"type": "function_call", "call_id": "call-1", "name": "render"},
        },
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 0,
            "delta": '{"pages":',
        },
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 0,
            "delta": "[2]}",
        },
        {"type": "response.output_text.done"},
    ]

    def stream(*_args, **_kwargs):
        return _Body(lines=_sse(events))

    transport = OpenAIResponsesTransport(
        _settings("openai_responses", model="gpt-5.1"), client_factory=lambda: _AsyncClient(stream=stream)
    )
    request = ModelRequest(
        model="gpt-5.1",
        system="system",
        messages=({"role": "user", "content": "render"},),
        tools=(
            {
                "name": "render",
                "description": "render pages",
                "input_schema": {"type": "object", "properties": {}},
            },
        ),
        max_tokens=64,
        thinking=False,
    )
    result = transport.complete(
        ProviderChannel("gpt-secret", "ch-gpt"), request, total_timeout_s=30
    )
    tool = next(
        block for block in result.message.content if block["type"] == "tool_use"
    )
    assert tool["id"] == "call-1"
    assert tool["name"] == "render"
    assert tool["input"] == {"pages": [2]}
    assert result.message.stop_reason == "tool_use"


def test_responses_maps_incomplete_max_output_tokens_to_max_tokens() -> None:
    decoded = {
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "partial"}],
            }
        ],
        "usage": {"input_tokens": 3, "output_tokens": 64},
    }

    def stream(*_args, **_kwargs):
        return _Body(body=json.dumps(decoded).encode("utf-8"))

    transport = OpenAIResponsesTransport(
        _settings("openai_responses", streaming=False, model="gpt-5.1"), client_factory=lambda: _AsyncClient(stream=stream)
    )
    result = transport.complete(
        ProviderChannel("gpt-secret", "ch-gpt"),
        _request("gpt-5.1", thinking=False),
        total_timeout_s=30,
    )
    assert result.message.stop_reason == "max_tokens"


def test_responses_without_summary_yields_no_analysis() -> None:
    decoded = {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Plain answer."}],
            }
        ],
        "usage": {"input_tokens": 3, "output_tokens": 2},
    }

    def stream(*_args, **_kwargs):
        return _Body(body=json.dumps(decoded).encode("utf-8"))

    transport = OpenAIResponsesTransport(
        _settings("openai_responses", streaming=False, model="gpt-5.1"), client_factory=lambda: _AsyncClient(stream=stream)
    )
    result = transport.complete(
        ProviderChannel("gpt-secret", "ch-gpt"),
        _request("gpt-5.1", thinking=False),
        total_timeout_s=30,
    )
    assert result.message.analysis_summaries == ()


def test_responses_input_converts_tool_transaction() -> None:
    converted = to_openai_responses_input(
        "system",
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "calling render"},
                    {
                        "type": "tool_use",
                        "id": "call-1",
                        "name": "render",
                        "input": {"pages": [1]},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call-1",
                        "content": "rendered ok",
                    }
                ],
            },
        ],
    )
    assert converted[0] == {
        "role": "assistant",
        "content": [{"type": "output_text", "text": "calling render"}],
    }
    assert converted[1] == {
        "type": "function_call",
        "call_id": "call-1",
        "name": "render",
        "arguments": '{"pages": [1]}',
    }
    assert converted[2] == {
        "type": "function_call_output",
        "call_id": "call-1",
        "output": "rendered ok",
    }


def test_normalize_openai_responses_url_variants() -> None:
    assert (
        normalize_openai_responses_url("https://api.example")
        == "https://api.example/v1/responses"
    )
    assert (
        normalize_openai_responses_url("https://api.example/v1")
        == "https://api.example/v1/responses"
    )
    assert (
        normalize_openai_responses_url("https://api.example/v1/responses")
        == "https://api.example/v1/responses"
    )
