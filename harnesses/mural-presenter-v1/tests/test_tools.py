from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from runtime.tools import (
    STABLE_TOOL_SCHEMAS,
    STABLE_TOOL_ROLES,
    ToolCall,
    ToolContext,
    ToolDefinition,
    ToolExecutionError,
    ToolRuntime,
    WriteScope,
    register_tools,
    replay_tool_transactions,
    validate_json_value,
)


def _write_definition(handler) -> ToolDefinition:
    return ToolDefinition(
        name="write_file",
        description="write one owned file",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        handler=handler,
        roles=frozenset({"slide"}),
        mutates_workspace=True,
        path_argument="path",
    )


def test_successful_tool_call_is_closed_and_idempotent() -> None:
    invocations: list[dict] = []
    runtime = ToolRuntime()
    runtime.register(
        _write_definition(
            lambda _context, arguments: invocations.append(dict(arguments)) or "ok"
        )
    )
    context = ToolContext(
        owner_id="deck:group-a:e1",
        role="slide",
        write_scopes=(WriteScope("slides/slide_02.html", recursive=False),),
    )
    call = ToolCall(
        "tool-1",
        "write_file",
        {"path": "slides/slide_02.html", "content": "<section/>"},
    )
    first = runtime.execute(context, call)
    second = runtime.execute(context, call)
    assert first.ok and second == first
    assert len(invocations) == 1
    assert replay_tool_transactions(runtime.events).open_ids == ()
    assert [event["event_type"] for event in runtime.events] == [
        "tool_use",
        "tool_result",
    ]


def test_validation_permission_and_handler_failures_still_close_transactions() -> None:
    runtime = ToolRuntime()
    runtime.register(_write_definition(lambda _context, _arguments: 1 / 0))
    denied = ToolContext(
        owner_id="deck:group-a:e1",
        role="slide",
        write_scopes=(WriteScope("slides/slide_02.html", recursive=False),),
    )
    bad_path = runtime.execute(
        denied,
        ToolCall(
            "tool-path",
            "write_file",
            {"path": "slides/slide_03.html", "content": "x"},
        ),
    )
    unknown_arg = runtime.execute(
        denied,
        ToolCall(
            "tool-arg",
            "write_file",
            {"path": "slides/slide_02.html", "content": "x", "ready": True},
        ),
    )
    crashed = runtime.execute(
        denied,
        ToolCall(
            "tool-crash",
            "write_file",
            {"path": "slides/slide_02.html", "content": "x"},
        ),
    )
    assert bad_path.error_code == "write_scope_denied"
    assert unknown_arg.error_code == "unknown_arguments"
    assert crashed.error_code == "handler_error"
    summary = replay_tool_transactions(runtime.events)
    assert summary.uses == summary.results == 3
    assert summary.open_ids == ()


@pytest.mark.parametrize('other', [
    ToolContext('owner-b', 'research'), ToolContext('owner-b', 'slide'), ToolContext('owner-a', 'slide'),
])
def test_cached_result_cannot_cross_owner_or_role(other):
    runtime = ToolRuntime()
    runtime.register(ToolDefinition('owned_fact', 'read owned evidence',
        {'type': 'object', 'properties': {}, 'additionalProperties': False},
        lambda context, _: {'owner': context.owner_id, 'fact': 'synthetic evidence'},
        roles=frozenset({'research'})))
    owner = ToolContext('owner-a', 'research')
    call = ToolCall('provider-id-1', 'owned_fact', {})
    original = runtime.execute(owner, call)
    assert original.ok
    denied = runtime.execute(other, call)
    assert not denied.ok and denied.error_code == 'tool_use_id_conflict'
    assert 'synthetic evidence' not in denied.content
    assert runtime.execute(owner, call) == original
    assert len(runtime.events) == 2


def test_inflight_id_is_bound_to_original_identity_and_arguments():
    started, release = threading.Event(), threading.Event()
    runtime = ToolRuntime()
    def handler(_context, _arguments):
        started.set()
        assert release.wait(10)
        return 'done'
    runtime.register(ToolDefinition('wait', 'hold a synthetic call',
        {'type': 'object', 'properties': {'value': {'type': 'integer'}}, 'required': ['value']}, handler))
    owner = ToolContext('owner-a', 'research')
    call = ToolCall('provider-id-2', 'wait', {'value': 1})
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runtime.execute, owner, call)
        try:
            assert started.wait(10)
            assert runtime.execute(owner, call).error_code == 'tool_use_inflight'
            assert runtime.execute(ToolContext('owner-b', 'research'), call).error_code == 'tool_use_id_conflict'
            assert runtime.execute(owner, ToolCall(call.tool_use_id, 'wait', {'value': 2})).error_code == 'tool_use_id_conflict'
        finally:
            release.set()
        assert future.result(timeout=10).ok
    assert replay_tool_transactions(runtime.events).open_ids == ()
    assert len(runtime.events) == 2


def test_workspace_path_cannot_escape_owner_scope() -> None:
    runtime = ToolRuntime()
    runtime.register(_write_definition(lambda _context, _arguments: "ok"))
    context = ToolContext(
        owner_id="deck:group-a:e1",
        role="slide",
        write_scopes=(WriteScope("slides", recursive=True),),
    )
    result = runtime.execute(
        context,
        ToolCall(
            "tool-escape",
            "write_file",
            {"path": "../plan/deck.md", "content": "overwrite"},
        ),
    )
    assert not result.ok
    assert result.error_code == "invalid_path"


def test_stable_actions_have_no_polling_or_magic_prose_fields() -> None:
    assert set(STABLE_TOOL_SCHEMAS) == {
        "delegate_submit",
        "delegate_wait",
        "request_orchestrator",
        "respond_owner_request",
        "artifact_commit",
        "render",
        "inspect",
        "verification_submit",
    }
    event_properties = STABLE_TOOL_SCHEMAS["delegate_wait"]["properties"]
    assert set(event_properties) == {"after_sequence"}
    assert "timeout" not in event_properties
    assert STABLE_TOOL_SCHEMAS["delegate_submit"]["required"] == ["tasks"]
    assert all("review" not in roles for roles in STABLE_TOOL_ROLES.values())
    assert STABLE_TOOL_ROLES["request_orchestrator"] == frozenset({"slide"})
    assert STABLE_TOOL_ROLES["respond_owner_request"] == frozenset(
        {"orchestrator"}
    )
    serialized = repr(STABLE_TOOL_SCHEMAS).lower()
    assert "page decision" not in serialized
    assert "delivery issue" not in serialized
    assert "ownership_topology" not in serialized
    assert "canvas_select" not in STABLE_TOOL_SCHEMAS


def test_delegate_submit_advertises_a_valid_native_array_example() -> None:
    runtime = ToolRuntime()
    register_tools(runtime, {"delegate_submit": lambda _c, _a: "ok"}, require_complete=False)
    tool = runtime.advertised_tools("orchestrator")[0]
    example = json.loads(tool["description"].split("最小格式示例：", 1)[1].splitlines()[0])
    assert isinstance(example["tasks"], list)
    assert example["tasks"][0]["role"] == "image"
    assert "不要编码成字符串" in tool["description"]
    assert "不要编码成字符串" in tool["input_schema"]["properties"]["tasks"]["description"]
    validate_json_value(example, tool["input_schema"])


@pytest.mark.parametrize("arguments,code,detail", [
    ({"tasks": '[{"task_id":"image-01","role":"image"}]'},
     "invalid_arguments", "$.tasks must be an array; received string"),
    ({"tasks": '[{"goal":"two "unlabelled" images"}]'},
     "invalid_arguments", "$.tasks must be an array; received string"),
    ({"tasks": {"role": "image"}}, "invalid_arguments", "received object"),
    ({"tasks": None}, "invalid_arguments", "received null"),
    ({"tasks": []}, "invalid_arguments", "must contain at least 1 items"),
    ({"tasks": [None]}, "invalid_arguments", "$.tasks[0] must be an object; received null"),
    ({"tasks": ['{"role":"image"}']}, "invalid_arguments", "$.tasks[0] must be an object; received string"),
    ({"tasks": [{"task_id": "image-01"}]}, "missing_arguments", "['role']"),
    ({}, "missing_arguments", "['tasks']"),
    ({"tasks": [{"task_id": "image-01", "role": "image", "unexpected": True}]},
     "unknown_arguments", "$.tasks[0] contains unknown fields"),
])
def test_invalid_delegation_returns_correction_without_starting_or_reparsing(arguments, code, detail):
    invocations = []
    runtime = ToolRuntime()
    register_tools(runtime, {
        "delegate_submit": lambda _c, args: invocations.append(args) or {"accepted": len(args["tasks"])},
    }, require_complete=False)
    context = ToolContext("orchestrator", "orchestrator")
    original = json.dumps(arguments, ensure_ascii=False)
    bad_call = ToolCall("bad-delegation", "delegate_submit", arguments)
    rejected = runtime.execute(context, bad_call)
    assert not rejected.ok and rejected.error_code == code
    assert detail in rejected.content
    assert "本批任务未接受或启动" in rejected.content
    assert "tasks 直接传 JSON 数组" in rejected.content
    assert invocations == []
    assert runtime.execute(context, bad_call) == rejected
    assert json.dumps(arguments, ensure_ascii=False) == original
    assert runtime.events[0]["arguments"] == arguments
    runtime.assert_closed()

    corrected = json.loads(rejected.content.split("格式示例：", 1)[1])
    corrected_call = ToolCall("corrected-delegation", "delegate_submit", corrected)
    accepted = runtime.execute(context, corrected_call)
    assert accepted.ok and json.loads(accepted.content) == {"accepted": 1}
    assert invocations == [corrected]
    assert runtime.execute(context, corrected_call) == accepted
    assert invocations == [corrected]
    summary = replay_tool_transactions(runtime.events)
    assert summary.uses == summary.results == 2 and summary.open_ids == ()


def test_delegation_permission_and_execution_errors_are_not_mislabeled_as_format_errors():
    def reject(_context, _arguments):
        raise ToolExecutionError("delegation_rejected", "page already has an Owner")

    runtime = ToolRuntime()
    register_tools(runtime, {"delegate_submit": reject}, require_complete=False)
    denied = runtime.execute(ToolContext("slide", "slide"), ToolCall(
        "denied", "delegate_submit", {"tasks": "[]"},
    ))
    assert denied.error_code == "tool_permission_denied"
    rejected = runtime.execute(ToolContext("orchestrator", "orchestrator"), ToolCall(
        "conflict", "delegate_submit", {"tasks": [{"task_id": "p1", "role": "slide", "pages": [1]}]},
    ))
    assert rejected.error_code == "delegation_rejected"
    assert rejected.content == "page already has an Owner"
    for result in (denied, rejected):
        assert "格式示例" not in result.content and "未接受或启动" not in result.content
    runtime.assert_closed()
