"""同轮工具并发的顺序、事务、恢复与本机 HTTP 耗时证据；不请求模型服务。"""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from types import SimpleNamespace

import pytest

from runtime.agent import AgentInterruptedError
from runtime.production import ProductionSettings, ProductionToolServices, ResourceSlots
from runtime.recovery import ResourceSlotTimeout, tool_boundary
from runtime.tool_batch import execute_tool_batch
from runtime.tools import ToolCall, ToolContext, ToolExecutionError, ToolRuntime, register_tools
from state.workspace import WorkspaceState
from test_agent_runtime import ScriptedTransport, _definition, _response, _run, _text, _tool


def _runtime_tools(names, handler, *, role="research", event_sink=None):
    tools = ToolRuntime(event_sink=event_sink)
    for name in set(names):
        tools.register(_definition(name, handler, role=role))
    return tools


def test_real_event_completion_order_differs_from_model_result_order():
    first_started, second_finished = threading.Event(), threading.Event()
    main_thread = threading.get_ident()
    def event_sink(event):
        if event["event_type"] == "tool_result" and event["tool_use_id"] == "second":
            second_finished.set()
    def read(context, arguments):
        assert threading.get_ident() != main_thread
        assert context.metadata["turn"] == 1 and context.metadata["segment_id"]
        if arguments["path"] == "first.md":
            first_started.set()
            assert second_finished.wait(5)
        else:
            assert first_started.wait(5)
        return {"path": arguments["path"]}
    tools = _runtime_tools(["read_file"], read, event_sink=event_sink)
    transport = ScriptedTransport([
        _response(_tool("first", "read_file", {"path": "first.md"}),
                  _tool("second", "read_file", {"path": "second.md"}), stop_reason="tool_use"),
        _response(_text("done")),
    ])
    checkpoints = []
    result = _run(transport, tools, ToolContext("research", "research"), checkpoint_sink=checkpoints.append)
    ends = [event for event in tools.events if event["event_type"] == "tool_result"]
    assert [event["tool_use_id"] for event in ends] == ["second", "first"]
    assert all(event["duration_ms"] >= 0 for event in ends)
    assert [block["tool_use_id"] for block in transport.calls[1][1].messages[-1]["content"]] == ["first", "second"]
    assert result.tool_calls == 2 and all(tool_boundary(item.messages).is_closed for item in checkpoints)
    tools.assert_closed()


def test_worker_count_is_bounded_and_all_batches_are_joined():
    active, peak = 0, 0
    lock = threading.Lock()
    first_wave = threading.Barrier(8, timeout=5)
    def read(_context, arguments):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        if int(arguments["path"]) < 8:
            first_wave.wait()
        time.sleep(0.02)
        with lock:
            active -= 1
        return {}
    tools = _runtime_tools(["read_file"], read)
    calls = [ToolCall(str(index), "read_file", {"path": str(index)}) for index in range(9)]
    results = list(execute_tool_batch(tools, ToolContext("r", "research"), calls))
    assert len(results) == 9 and all(result.ok for _, result in results)
    assert peak == 8 and active == 0
    in_flight, trace_peak, completed = set(), 0, set()
    for event in tools.events:
        tool_id = event["tool_use_id"]
        assert event["timestamp"]
        if event["event_type"] == "tool_use":
            if tool_id == "8":
                assert completed == {str(index) for index in range(8)}
            in_flight.add(tool_id)
            trace_peak = max(trace_peak, len(in_flight))
        elif event["event_type"] == "tool_result":
            assert tool_id in in_flight and event["duration_ms"] >= 0
            in_flight.remove(tool_id)
            completed.add(tool_id)
    assert trace_peak == 8 and not in_flight and len(completed) == 9
    tools.assert_closed()


@pytest.mark.parametrize("name", [
    "write_file", "patch", "inspect", "render", "vision_analyze", "image_generate",
    "delegate_submit", "delegate_wait", "verification_submit", "compact", "unknown", "terminal",
])
def test_risky_tools_are_main_thread_barriers(name):
    order = []
    main_thread = threading.get_ident()
    def handler(_context, arguments):
        assert threading.get_ident() == main_thread
        order.append(arguments["path"])
        return {}
    tools = _runtime_tools(["read_file", name], handler)
    calls = [ToolCall("read-before", "read_file", {"path": "before"}),
             ToolCall("risk", name, {"path": "risk"}),
             ToolCall("read-after", "read_file", {"path": "after"})]
    assert all(result.ok for _, result in execute_tool_batch(tools, ToolContext("r", "research"), calls))
    assert order == ["before", "risk", "after"]


@pytest.mark.parametrize("first,second,role", [
    (("write_file", {"path": "same.md"}), ("write_file", {"path": "same.md"}), "slide"),
    (("write_file", {"path": "slides/slide_01.html"}), ("inspect", {"page": 1}), "slide"),
    (("web_search", {"query": "same  query"}), ("web_search", {"query": "same query"}), "research"),
    (("web_extract", {"url": "https://example.test/a#first"}),
     ("web_extract", {"url": "https://example.test/a#second", "render_js": True}), "research"),
    (("web_extract", {"url": "https://example.test/a"}),
     ("read_file", {"path": "_trace/web-extract/source.md"}), "research"),
    (("read_file", {"path": "_trace/web-extract/source.md"}),
     ("web_extract", {"url": "https://example.test/a"}), "research"),
    (("terminal", {"command": "python skills/example/scripts/image.py fetch ."}),
     ("terminal", {"command": "python skills/example/scripts/image.py finalize ."}), "image"),
    (("terminal", {"command": "python skills/example/scripts/image.py finalize ."}),
     ("read_file", {"path": "assets/catalog.md"}), "image"),
    (("read_file", {"path": "assets/catalog.md"}),
     ("terminal", {"command": "python skills/example/scripts/image.py finalize ."}), "image"),
])
def test_conflicts_preserve_declared_sequence(first, second, role):
    main_thread = threading.get_ident()
    order = []
    def handler(_context, arguments):
        assert threading.get_ident() == main_thread
        order.append(arguments)
        return {}
    calls = [ToolCall(str(index), name, arguments) for index, (name, arguments) in enumerate((first, second))]
    tools = _runtime_tools([call.name for call in calls], handler, role=role)
    assert all(result.ok for _, result in execute_tool_batch(tools, ToolContext("owner", role), calls))
    assert order == [first[1], second[1]]


@pytest.mark.parametrize("target", ["_trace/web-extract/source.md", "alias.md"])
def test_read_alias_to_a_cache_path_is_a_barrier(tmp_path, target):
    (tmp_path / "alias.md").symlink_to(tmp_path / target)
    main_thread = threading.get_ident()
    def handler(_context, _arguments):
        assert threading.get_ident() == main_thread
        return {}
    tools = _runtime_tools(["web_extract", "read_file"], handler)
    calls = [ToolCall("extract", "web_extract", {"url": "https://example.test/a"}),
             ToolCall("read", "read_file", {"path": "alias.md"})]
    context = ToolContext("r", "research", metadata={"workspace_root": str(tmp_path)})
    assert all(result.ok for _, result in execute_tool_batch(tools, context, calls))


@pytest.mark.parametrize("attachments", [
    ["att-0123456789ab", "att-abcdef012345"],
    ["att-abcdef012345", "att-0123456789ab"],
])
def test_research_reads_of_distinct_parsed_attachments_overlap(attachments):
    started = threading.Barrier(2, timeout=5)
    def handler(_context, _arguments):
        started.wait()
        return {}
    tools = _runtime_tools(["read_file"], handler, role="research")
    calls = [ToolCall(str(index), "read_file", {"path": f"inputs/extracted/{attachment}/document.md"})
             for index, attachment in enumerate(attachments)]
    assert all(result.ok for _, result in execute_tool_batch(tools, ToolContext("r", "research"), calls))


@pytest.mark.parametrize("role", ["material", "research"])
def test_removed_material_terminal_is_denied_in_tool_batches(tmp_path, monkeypatch, role):
    from test_explicit_asset_commands import services_for

    backend = services_for(tmp_path)
    monkeypatch.setattr("runtime.production._run_command", lambda *_args, **_kwargs: pytest.fail("foreign Skill must not run"))
    tools = _runtime_tools(["terminal"], backend.terminal, role=role)
    calls = [ToolCall(str(index), "terminal", {
        "command": f"python skills/foreign-skill/scripts/material.py parse . --attachment {attachment}"})
        for index, attachment in enumerate(("att-0123456789ab", "att-abcdef012345"))]
    results = list(execute_tool_batch(tools, ToolContext("removed", role), calls))
    assert all(not result.ok and result.error_code == "terminal_role_denied" for _, result in results)
    tools.assert_closed()


@pytest.mark.parametrize("command", [
    "python skills/example/scripts/image.py register . --id=hero --path assets/hero.png --kind generated",
    "python skills/example/scripts/image.py fetch . --replace --asset hero",
    "python skills/example/scripts/image.py finalize elsewhere",
    "python skills/example/scripts/image.py fetch . --unknown flag",
    "python skills/example/scripts/image.py fetch .",
    "python skills/example/scripts/material.py --help",
    "unterminated 'quote",
])
def test_all_terminal_argv_remains_a_barrier(command):
    main_thread = threading.get_ident()
    def handler(_context, _arguments):
        assert threading.get_ident() == main_thread
        return {}
    tools = _runtime_tools(["terminal", "read_file"], handler, role="image")
    calls = [ToolCall("command", "terminal", {"command": command}), ToolCall("read", "read_file", {"path": "other.md"})]
    assert all(result.ok for _, result in execute_tool_batch(tools, ToolContext("i", "image"), calls))


@pytest.mark.parametrize("error_code,expected", [
    ("handler_error", None), ("owner_interrupted", AgentInterruptedError),
    ("font_integrity_failed", AgentInterruptedError),
    ("resource_queue_timeout", ResourceSlotTimeout),
])
def test_parallel_failure_closes_checkpoint_and_resume_does_not_repeat(error_code, expected):
    started = threading.Barrier(8, timeout=5)
    executed, checkpoints = [], []
    def handler(_context, arguments):
        started.wait()
        executed.append(arguments["path"])
        if arguments["path"] == "fails.md":
            if error_code == "handler_error":
                raise RuntimeError("fixture failure")
            raise ToolExecutionError(error_code, "fixture interrupted")
        return {}
    tools = _runtime_tools(["read_file"], handler)
    labels = ["fails", *[f"works-{index}" for index in range(7)]]
    transport = ScriptedTransport([
        _response(*[_tool(label, "read_file", {"path": f"{label}.md"}) for label in labels], stop_reason="tool_use"),
        _response(_text("done")),
    ])
    kwargs = {"checkpoint_sink": checkpoints.append}
    if expected:
        with pytest.raises(expected):
            _run(transport, tools, ToolContext("r", "research"), **kwargs)
        assert len(transport.calls) == 1
    else:
        _run(transport, tools, ToolContext("r", "research"), **kwargs)
    assert sorted(executed) == [f"{label}.md" for label in labels]
    assert all(tool_boundary(checkpoint.messages).is_closed for checkpoint in checkpoints)
    assert len([event for event in tools.events if event["event_type"] == "tool_result"]) == 8
    assert checkpoints[0].messages == ({"role": "user", "content": "开始"},)
    closed = next(checkpoint for checkpoint in checkpoints
        if checkpoint.messages[-1]["role"] == "user"
        and isinstance(checkpoint.messages[-1]["content"], list)
        and [block.get("tool_use_id") for block in checkpoint.messages[-1]["content"]] == labels)
    blocks = closed.messages[-1]["content"]
    assert [block["tool_use_id"] for block in blocks] == labels
    assert blocks[0]["error_code"] == error_code and all(not block.get("is_error") for block in blocks[1:])
    fresh_tools = _runtime_tools(["read_file"], lambda *_: pytest.fail("resume must not replay tools"))
    resumed = _run(ScriptedTransport([_response(_text("resumed"))]), fresh_tools,
                   ToolContext("r", "research"), initial_messages=(), checkpoint=closed)
    assert resumed.final_text == "resumed" and not fresh_tools.events
    tools.assert_closed()


def test_parallel_searches_keep_existing_resource_slots_and_cache(tmp_path, monkeypatch):
    settings = ProductionSettings(skill_source=tmp_path, web_search_concurrency=2)
    services = object.__new__(ProductionToolServices)
    services.run_spec = SimpleNamespace(authoring_profile="general")
    services.environment = {"SERPER_API_KEY": "fixture-only"}
    services.slots = ResourceSlots(tmp_path, settings, environment={})
    services._search_lock = threading.Lock()
    services._web_results = {}
    active, peak, requests = 0, 0, []
    lock, pair = threading.Lock(), threading.Barrier(2, timeout=5)
    def post(_url, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            requests.append(kwargs["json"]["q"])
        pair.wait()
        with lock:
            active -= 1
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"organic": []})
    monkeypatch.setattr("requests.post", post)
    tools = ToolRuntime()
    register_tools(tools, {"web_search": services.web_search}, require_complete=False)
    calls = [ToolCall(str(index), "web_search", {"query": f"query {index}"}) for index in range(4)]
    calls.append(ToolCall("repeat", "web_search", {"query": "query 0"}))
    results = list(execute_tool_batch(tools, ToolContext("r", "research"), calls))
    assert all(result.ok for _, result in results)
    assert peak == 2 and active == 0 and len(requests) == 4
    assert json.loads(results[-1][1].content)["cached"]
    tools.assert_closed()


def test_two_deck_batches_share_eight_actual_search_requests(tmp_path, monkeypatch, record_property):
    settings = ProductionSettings.from_environment({})
    assert settings.web_search_concurrency == 8
    services, runtimes = [], []
    for index in range(2):
        service = object.__new__(ProductionToolServices)
        service.run_spec = SimpleNamespace(authoring_profile="general")
        service.environment = {"SERPER_API_KEY": "fixture-only", "SERPER_BASE_URL": "https://search.test"}
        service.slots = ResourceSlots(tmp_path / f"deck-{index}", settings, environment=service.environment)
        service._search_lock = threading.Lock()
        service._web_results = {}
        services.append(service)
        tools = ToolRuntime()
        register_tools(tools, {"web_search": service.web_search}, require_complete=False)
        runtimes.append(tools)
    assert services[0].slots.root == services[1].slots.root
    previous_leases = set(services[0].slots.root.glob("web_search-*.lease"))
    active, peak, queries = 0, 0, []
    lock, wave = threading.Lock(), threading.Barrier(8, timeout=10)

    def post(_url, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            queries.append(kwargs["json"]["q"])
        try:
            wave.wait()
            time.sleep(.02)
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"organic": []})
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr("requests.post", post)

    def run_deck(index):
        calls = [ToolCall(str(item), "web_search", {"query": f"deck {index} question {item}"}) for item in range(8)]
        return list(execute_tool_batch(runtimes[index], ToolContext(f"research-{index}", "research"), calls))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run_deck, range(2)))
    assert all(result.ok for batch in results for _, result in batch)
    assert peak == 8 and active == 0 and len(set(queries)) == 16
    assert set(services[0].slots.root.glob("web_search-*.lease")) == previous_leases
    for tools, batch in zip(runtimes, results):
        assert [call.tool_use_id for call, _ in batch] == [str(index) for index in range(8)]
        tools.assert_closed()
    record_property("cross_deck_search_requests", len(queries))
    record_property("cross_deck_search_peak", peak)


def test_parallel_command_scopes_keep_tool_ids_and_output_events_isolated():
    from runtime.diagnostics import _scope
    pair = threading.Barrier(2, timeout=5)
    events = []
    def handler(_context, arguments):
        scope = _scope.get()
        pair.wait()
        assert scope["tool_use_id"] == arguments["path"]
        scope["emit"]({"diagnostic_ref": arguments["path"]})
        return {}
    tools = _runtime_tools(["read_file"], handler, event_sink=events.append)
    calls = [ToolCall(label, "read_file", {"path": label}) for label in ("first", "second")]
    assert all(result.ok for _, result in execute_tool_batch(tools, ToolContext("r", "research"), calls))
    outputs = [event for event in events if event["event_type"] == "tool_output"]
    assert len(outputs) == 2 and all(event["tool_use_id"] == event["diagnostic_ref"] for event in outputs)
    assert _scope.get() is None


def test_local_http_extraction_overlap_is_faster_without_provider(tmp_path, monkeypatch, record_property):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            time.sleep(0.2)
            body = ("<html><title>Local source</title><h1>Evidence</h1><p>" + self.path + "</p></html>").encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    timings = {}
    try:
        for mode, limit in (("sequential", 1), ("parallel", 4)):
            services = object.__new__(ProductionToolServices)
            services.run_spec = SimpleNamespace(authoring_profile="general")
            services.settings = SimpleNamespace(web_extract_timeout_s=5, web_extract_max_bytes=4 * 1024 * 1024)
            services.workspace = WorkspaceState(tmp_path / mode)
            tools = ToolRuntime()
            register_tools(tools, {"web_extract": services.web_extract}, require_complete=False)
            calls = [ToolCall(str(index), "web_extract", {"url": f"http://127.0.0.1:{server.server_port}/{index}"})
                     for index in range(4)]
            monkeypatch.setattr("runtime.tool_batch.MAX_INDEPENDENT_TOOLS", limit)
            started = time.monotonic()
            results = list(execute_tool_batch(tools, ToolContext("r", "research"), calls))
            timings[mode] = time.monotonic() - started
            assert all(result.ok and not json.loads(result.content)["cached"] for _, result in results)
            assert len(list((tmp_path / mode / "_trace/web-extract").glob("*.md"))) == 4
            tools.assert_closed()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
    record_property("local_http_seconds", json.dumps(timings))
    assert timings["parallel"] < timings["sequential"] * 0.8, timings
