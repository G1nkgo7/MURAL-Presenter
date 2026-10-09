"""Fresh MinerU image admission across closed tool batches."""
import base64
import copy
import hashlib
import json
from types import SimpleNamespace

from PIL import Image
import pytest

from runtime.agent import AgentRuntime, _attachment_read_result_block
from runtime.recovery import tool_boundary
from runtime.session import ModelRequest, ProviderChannel
from runtime.tools import ToolContext, ToolRuntime, WriteScope, register_tools
from runtime.vision import MAX_CONTEXT_IMAGES, image_blocks, image_source, opened_visual_sources
from test_agent_runtime import ScriptedTransport, _definition, _response, _settings, _summary_invoker, _text, _tool
from test_explicit_asset_commands import services_for


@pytest.fixture
def mineru_reads(tmp_path):
    services = services_for(tmp_path)
    base = "inputs/extracted/att-123456789abc/mineru/fixed"
    document = base + "/document.md"
    (tmp_path / base / "images").mkdir(parents=True)
    paths, lines = [], []
    for group in range(8):
        references = []
        for index in range(4):
            relative = f"{base}/images/g{group}-f{index}.png"
            Image.new("RGB", (24, 16), (group * 25, index * 40, 170)).save(tmp_path / relative)
            paths.append(relative)
            references.append(f"![Figure {index}](images/g{group}-f{index}.png)")
        lines.append(" ".join(references))
    (tmp_path / document).write_text("\n".join(lines))
    outputs = {
        relative: hashlib.sha256((tmp_path / relative).read_bytes()).hexdigest()
        for relative in [document, *paths]
    }
    # Only the fixed parse receipt/output map is needed by the Markdown media reader.
    manifest = {"records": [{"parse": {"results": {"fixed": {
        "receipt": {"text_path": document, "media_paths": paths}, "outputs": outputs,
    }}}}]}
    (tmp_path / "_trace" / "attachment-manifest.json").write_text(json.dumps(manifest))
    tools = ToolRuntime()
    register_tools(tools, {"read_file": services.read_file}, require_complete=False)
    context = ToolContext(
        "research-owner", "research",
        read_scopes=(WriteScope("inputs/extracted", recursive=True),),
        metadata={"workspace_root": str(tmp_path)},
    )
    return SimpleNamespace(root=tmp_path, document=document, paths=paths, tools=tools, context=context)


def _read(fixture, call_id, group):
    return _tool(call_id, "read_file", {"path": fixture.document, "offset": group + 1, "limit": 1})


def _run(fixture, outcomes, *, initial_messages=None, max_context_images=MAX_CONTEXT_IMAGES):
    transport = ScriptedTransport(outcomes)
    events, checkpoints = [], []
    runtime = AgentRuntime(
        settings=_settings(), channels=(ProviderChannel("fixture-secret", "a"),),
        transport=transport, tools=fixture.tools, summary_invoker=_summary_invoker,
        event_sink=events.append, max_context_images=max_context_images,
    )
    result = runtime.run(
        agent_id=fixture.context.owner_id, role=fixture.context.role,
        system="Read the supplied source evidence.", tool_context=fixture.context,
        initial_messages=initial_messages or ({"role": "user", "content": "Read the selected sections."},),
        max_turns=6, max_tokens=128, thinking=False, effort="low",
        checkpoint_sink=checkpoints.append,
    )
    assert result.final_text == "done"
    assert all(tool_boundary(checkpoint.messages).is_closed for checkpoint in checkpoints)
    assert all(tool_boundary(request.messages).is_closed for _, request in transport.calls)
    assert not any(event["event_type"] == "agent_context_compaction_failed" for event in events)
    fixture.tools.assert_closed()
    return SimpleNamespace(result=result, requests=[request for _, request in transport.calls],
                           events=events, checkpoints=checkpoints)


def _results(messages):
    return {
        block["tool_use_id"]: block
        for message in messages
        for block in (message["content"] if isinstance(message.get("content"), list) else [])
        if block.get("type") == "tool_result"
    }


def _payload(block):
    content = block["content"]
    return json.loads(content[0]["text"] if isinstance(content, list) else content)


def _image_count(messages):
    return sum(1 for message in messages for _ in image_blocks(message.get("content")))


@pytest.mark.parametrize("groups", [
    list(range(5)), list(range(6)), list(range(8)), [0, 0, 1, 2, 3, 4, 5, 6],
])
def test_production_reads_share_twenty_fresh_images_and_preserve_receipts(mineru_reads, groups):
    fixture = mineru_reads
    run = _run(fixture, [
        _response(*[_read(fixture, f"read-{index}", group) for index, group in enumerate(groups)],
                  stop_reason="tool_use"),
        _response(_text("done")),
    ])
    sent = run.requests[1].messages
    results = _results(sent)
    assert list(results) == [f"read-{index}" for index in range(len(groups))]
    assert _image_count(sent) == MAX_CONTEXT_IMAGES == 20
    assert not any(event["event_type"] == "agent_context_compacted" for event in run.events)
    raw_results = {
        event["tool_use_id"]: json.loads(event["content"]) for event in fixture.tools.events
        if event["event_type"] == "tool_result"
    }
    seen = set()
    for index, group in enumerate(groups):
        block = results[f"read-{index}"]
        payload = _payload(block)
        raw = raw_results[f"read-{index}"]
        assert not block.get("is_error")
        assert {key: value for key, value in payload.items()
                if key not in {"attached_images", "multimodal_next_action"}} == {
            key: value for key, value in raw.items() if key != "_multimodal_images"
        }
        assert len(raw["_multimodal_images"]) == 4
        entries = payload["attached_images"]
        assert [entry["path"] for entry in entries] == fixture.paths[group * 4:group * 4 + 4]
        attached = []
        for entry in entries:
            if entry["path"] in seen:
                assert entry["status"] == "already_in_context"
            elif len(seen) < MAX_CONTEXT_IMAGES:
                assert entry["status"] == "attached" and "reason" not in entry
                seen.add(entry["path"])
                attached.append(entry)
            else:
                assert entry == {
                    "path": entry["path"], "status": "deferred", "reason": "batch_image_limit",
                }
        images = list(image_blocks(block["content"]))
        assert len(images) == len(attached)
        if any(entry["status"] == "deferred" for entry in entries):
            assert "read_file" in payload["multimodal_next_action"]
            assert "query" in payload["multimodal_next_action"]
            assert "offset/limit" in payload["multimodal_next_action"]
        else:
            assert "multimodal_next_action" not in payload
        for entry, image in zip(attached, images):
            assert base64.b64decode(image["source"]["data"]) == (fixture.root / entry["path"]).read_bytes()
    for attached_only in (False, True):
        assert {item["path"] for item in opened_visual_sources(
            sent, owner_id=fixture.context.owner_id, attached_only=attached_only,
        )} == seen
    assert set(run.result.vision_paths) == seen


def test_configured_active_image_budget_limits_parallel_mineru_reads(mineru_reads):
    fixture = mineru_reads
    run = _run(fixture, [
        _response(*[_read(fixture, f"read-{index}", index) for index in range(8)],
                  stop_reason="tool_use"),
        _response(_text("done")),
    ], max_context_images=4)
    sent = run.requests[1].messages
    entries = [entry for index in range(8)
               for entry in _payload(_results(sent)[f"read-{index}"])["attached_images"]]
    assert _image_count(sent) == 4
    assert sum(entry["status"] == "attached" for entry in entries) == 4
    assert sum(entry["status"] == "deferred" for entry in entries) == 28


def test_configured_active_image_budget_is_shared_by_parallel_inspections(mineru_reads):
    fixture = mineru_reads
    fixture.tools.register(_definition("inspect", lambda _context, arguments: {
        "path": arguments["path"], "native_multimodal": True,
    }, role="research"))
    calls = [_tool(f"inspect-{index}", "inspect", {"path": fixture.paths[index]})
             for index in range(8)]
    run = _run(fixture, [
        _response(*calls, stop_reason="tool_use"), _response(_text("done")),
    ], max_context_images=4)
    sent = run.requests[1].messages
    results = _results(sent)
    assert _image_count(sent) == 4
    assert sum(bool(list(image_blocks(results[f"inspect-{index}"]["content"])))
               for index in range(8)) == 4
    assert all(_payload(results[f"inspect-{index}"])["status"] == "visual_payload_deferred"
               for index in range(4, 8))


def test_deferred_inspection_can_load_cached_pixels_on_the_next_turn(mineru_reads):
    fixture = mineru_reads
    fixture.tools.register(_definition("inspect", lambda _context, arguments: {
        "path": arguments["path"],
        "native_multimodal": True,
        "inspection_cached": bool(arguments.get("cached")),
    }, role="research"))
    first = [_tool(f"inspect-{index}", "inspect", {"path": fixture.paths[index]})
             for index in range(5)]
    retry = _tool("retry", "inspect", {"path": fixture.paths[4], "cached": True})
    run = _run(fixture, [
        _response(*first, stop_reason="tool_use"),
        _response(retry, stop_reason="tool_use"),
        _response(_text("done")),
    ], max_context_images=4)
    first_results = _results(run.requests[1].messages)
    retry_result = _results(run.requests[2].messages)["retry"]
    assert _payload(first_results["inspect-4"])["status"] == "visual_payload_deferred"
    assert not list(image_blocks(first_results["inspect-4"]["content"]))
    assert len(list(image_blocks(retry_result["content"]))) == 1
    assert _payload(retry_result)["visual_source"]["path"] == fixture.paths[4]


@pytest.mark.parametrize("name", ["inspect", "inspect_group", "vision_analyze"])
@pytest.mark.parametrize("cache_mode", ["fresh", "cached", "cached-retained"])
@pytest.mark.parametrize("position", ["before", "after"])
def test_mixed_native_inspections_reserve_slots_regardless_of_order_or_cache(
    mineru_reads, name, cache_mode, position,
):
    fixture = mineru_reads
    fixture.tools.register(_definition(name, lambda _context, arguments: {
        "path": arguments["path"], "native_multimodal": True, "inspection_cached": cache_mode != "fresh",
    }, role="research"))
    inspections = [_tool(f"inspect-{index}", name, {
        "path": fixture.paths[-index - 1], "retain": cache_mode == "cached-retained",
    }) for index in range(2)]
    reads = [_read(fixture, f"read-{index}", index) for index in range(8)]
    batch = inspections + reads if position == "before" else reads + inspections
    run = _run(fixture, [_response(*batch, stop_reason="tool_use"), _response(_text("done"))])
    sent = run.requests[1].messages
    results = _results(sent)
    assert list(results) == [call.id for call in batch]
    entries = [entry for index in range(8) for entry in _payload(results[f"read-{index}"])["attached_images"]]
    assert sum(entry["status"] == "attached" for entry in entries) == 18
    assert sum(entry["status"] == "deferred" for entry in entries) == 14
    assert _image_count(sent) == (18 if cache_mode == "cached" else 20)
    assert all(len(list(image_blocks(results[f"inspect-{index}"]["content"]))) == (
        0 if cache_mode == "cached" else 1
    ) for index in range(2))


@pytest.mark.parametrize("image_query", [False, True])
def test_deferred_read_attaches_on_next_turn_without_false_seen_status(mineru_reads, image_query):
    fixture = mineru_reads
    reread = _read(fixture, "reread", 5)
    if image_query:
        reread.input["query"] = "g5-f0.png"
    run = _run(fixture, [
        _response(*[_read(fixture, f"read-{index}", index) for index in range(8)], stop_reason="tool_use"),
        _response(reread, stop_reason="tool_use"),
        _response(_text("done")),
    ])
    deferred = _payload(_results(run.requests[1].messages)["read-5"])["attached_images"]
    attached = _payload(_results(run.requests[2].messages)["reread"])["attached_images"]
    assert {entry["path"] for entry in deferred} == {entry["path"] for entry in attached}
    assert all(entry["status"] == "deferred" and entry["reason"] == "batch_image_limit" for entry in deferred)
    assert all(entry["status"] == "attached" and "reason" not in entry for entry in attached)
    reread_payload = _payload(_results(run.requests[2].messages)["reread"])
    assert "multimodal_next_action" not in reread_payload
    assert reread_payload["returned_line_count"] == 1
    assert [entry["path"] for entry in attached] == fixture.paths[20:24]
    if image_query:
        assert reread_payload["mode"] == "search_snippets"
        assert "g5-f0.png" in reread_payload["content"]
        assert "g4-f0.png" not in reread_payload["content"]
    assert len(list(image_blocks(_results(run.requests[2].messages)["reread"]["content"]))) == 4
    assert {item["path"] for item in opened_visual_sources(
        run.requests[2].messages, owner_id=fixture.context.owner_id, attached_only=True,
    )} >= {entry["path"] for entry in attached}


def test_consumed_old_images_do_not_spend_fresh_allowance_and_compact_normally(mineru_reads):
    fixture = mineru_reads
    run = _run(fixture, [
        _response(_read(fixture, "old-read", 7), stop_reason="tool_use"),
        _response(*[_read(fixture, f"read-{index}", index) for index in range(8)], stop_reason="tool_use"),
        _response(_text("done")),
    ])
    before = next(checkpoint for checkpoint in run.checkpoints if _image_count(checkpoint.messages) == 24)
    before_results = _results(before.messages)
    assert _payload(before_results["read-4"])["attached_images"] == [
        {"path": path, "status": "attached"} for path in fixture.paths[16:20]
    ]
    assert all(entry["status"] == "already_in_context"
               for entry in _payload(before_results["read-7"])["attached_images"])
    assert before_results["old-read"] == _results(run.requests[1].messages)["old-read"]
    assert _image_count(run.requests[2].messages) == 20
    released_old = _results(run.requests[2].messages)["old-read"]
    assert not list(image_blocks(released_old["content"]))
    assert "pixels_delivered_in_previous_model_turn" in str(released_old["content"])
    after_results = _results(run.requests[2].messages)
    assert {key: value for key, value in before_results.items() if key != "old-read"} == {
        key: value for key, value in after_results.items() if key != "old-read"
    }
    assert not [event for event in run.events if event["event_type"] == "agent_context_compacted"]
    released = [event for event in run.events if event["event_type"] == "agent_visual_context_released"]
    assert len(released) == 1 and released[0]["images_released"] == 4


@pytest.mark.parametrize("history_kind,protected_count", [
    ("input", 1), ("input", 20), ("retained", 1), ("transaction", 2),
])
def test_attachment_budget_reserves_protected_history(mineru_reads, history_kind, protected_count):
    fixture = mineru_reads
    image = {"type": "image", "source": image_source((fixture.root / fixture.paths[-1]).read_bytes())}
    if history_kind == "input":
        initial = [{"role": "user", "content": [copy.deepcopy(image) for _ in range(protected_count)]}]
    else:
        initial = [
            {"role": "user", "content": "Keep the reference."},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": f"old-{i}", "name": "inspect", "input": {}}
                for i in range(protected_count)
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"old-{i}", "vision_keep": i == 0,
                 "content": [copy.deepcopy(image)]}
                for i in range(protected_count)
            ]},
        ]
    original = copy.deepcopy(initial)
    run = _run(fixture, [
        _response(*[_read(fixture, f"read-{i}", i) for i in range(5)], stop_reason="tool_use"),
        _response(_text("done")),
    ], initial_messages=initial)
    sent = run.requests[1].messages
    assert initial == original
    effective_protected = 1 if history_kind == "transaction" else protected_count
    if history_kind in {"retained", "transaction"}:
        sent_initial = list(sent[:len(initial)])
        assert sent_initial[:2] == initial[:2]
        retained = copy.deepcopy(initial[2]["content"][0])
        retained.pop("vision_keep")
        assert sent_initial[2]["content"][0] == retained
        if history_kind == "transaction":
            assert not list(image_blocks(sent_initial[2]["content"][1]["content"]))
            assert "pixels_delivered_in_previous_model_turn" in str(sent_initial[2]["content"][1])
    else:
        assert list(sent[:len(initial)]) == initial
    assert _image_count(sent) == MAX_CONTEXT_IMAGES
    entries = [item for i in range(5) for item in _payload(_results(sent)[f"read-{i}"])["attached_images"]]
    assert sum(item["status"] == "attached" for item in entries) == MAX_CONTEXT_IMAGES - effective_protected
    assert sum(item["status"] == "deferred" for item in entries) == effective_protected
    assert not any(event["event_type"] == "agent_context_compacted" for event in run.events)


@pytest.mark.parametrize("position", ["before", "after"])
def test_protected_history_and_native_inspect_share_attachment_budget(mineru_reads, position):
    fixture = mineru_reads
    fixture.tools.register(_definition("inspect", lambda _context, arguments: {
        "path": arguments["path"], "native_multimodal": True,
    }, role="research"))
    image = {"type": "image", "source": image_source((fixture.root / fixture.paths[-1]).read_bytes())}
    initial = [{"role": "user", "content": [copy.deepcopy(image) for _ in range(19)]}]
    inspect = _tool("inspect", "inspect", {"path": fixture.paths[-1]})
    read = _read(fixture, "read", 0)
    batch = [inspect, read] if position == "before" else [read, inspect]
    run = _run(fixture, [_response(*batch, stop_reason="tool_use"), _response(_text("done"))],
               initial_messages=initial)
    sent = run.requests[1].messages
    assert _image_count(sent) == MAX_CONTEXT_IMAGES
    assert list(sent[:len(initial)]) == initial
    assert all(item["status"] == "deferred" for item in _payload(_results(sent)["read"])["attached_images"])
    assert len(list(image_blocks(_results(sent)["inspect"]["content"]))) == 1


def test_helper_preserves_page_and_defers_without_reading_image_bytes(mineru_reads, monkeypatch):
    fixture = mineru_reads
    images = [{"path": path, "page": 3} for path in fixture.paths[:4]]
    block = {"type": "tool_result", "tool_use_id": "read", "content": json.dumps({
        "path": fixture.document, "content": "Source text.", "_multimodal_images": [*images, images[1]],
    })}
    original = copy.deepcopy(block)
    read_bytes = type(fixture.root).read_bytes
    def allowed_read(path):
        assert path == fixture.root / images[1]["path"]
        return read_bytes(path)
    monkeypatch.setattr(type(fixture.root), "read_bytes", allowed_read)
    selected, paths = _attachment_read_result_block(
        block, fixture.context, image_allowance=1, visible_paths=frozenset({images[0]["path"]}),
    )
    assert block == original
    assert paths == (images[1]["path"],)
    assert _payload(selected)["attached_images"] == [
        {**images[0], "status": "already_in_context"},
        {**images[1], "status": "attached"},
        *[{**entry, "status": "deferred", "reason": "batch_image_limit"} for entry in images[2:]],
    ]
    assert _payload(selected)["content"] == "Source text."


@pytest.mark.parametrize("size", [(24, 16), (2400, 1600)])
def test_attachment_identity_uses_original_bytes_without_exposing_metadata(mineru_reads, size):
    from export.training import _scrub_ephemeral_training_text
    from runtime.raw_trace import externalize_binary
    from runtime.session import to_openai_messages, to_openai_responses_input

    fixture = mineru_reads
    relative = fixture.paths[0]
    Image.new("RGB", size, "red").save(fixture.root / relative)
    original = (fixture.root / relative).read_bytes()
    original_hash = hashlib.sha256(original).hexdigest()
    block = {"type": "tool_result", "tool_use_id": "read", "content": json.dumps({
        "path": fixture.document, "content": "Figure evidence.",
        "_multimodal_images": [{"path": relative, "page": 3}],
    })}
    selected, paths = _attachment_read_result_block(block, fixture.context, image_allowance=1)
    assert paths == (relative,)
    image, = image_blocks(selected["content"])
    transport_hash = hashlib.sha256(base64.b64decode(image["source"]["data"])).hexdigest()
    assert (original_hash == transport_hash) == (size[0] <= 2000)
    messages = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "read", "name": "read_file", "input": {"path": fixture.document}}
        ]},
        {"role": "user", "content": [selected]},
    ]
    expected = [{"owner_id": fixture.context.owner_id, "path": relative,
                 "sha256": original_hash, "page": 3}]
    stored = externalize_binary(fixture.root, messages)
    stored_image, = image_blocks(stored[-1]["content"])
    assert stored_image["source"]["sha256"] == transport_hash
    for history in (messages, stored):
        for attached_only in (False, True):
            for owner in (None, fixture.context.owner_id):
                assert opened_visual_sources(history, owner_id=owner, attached_only=attached_only) == expected
        assert opened_visual_sources(history, owner_id="different-owner") == []
    request = ModelRequest(model="fixture", system="read", messages=tuple(messages))
    for visible in (
        request.anthropic_kwargs(_settings()),
        to_openai_messages("read", request.messages),
        to_openai_responses_input("read", request.messages),
        _scrub_ephemeral_training_text(messages),
    ):
        serialized = json.dumps(visible)
        assert "opened_visual_sources" not in serialized
        assert original_hash not in serialized
        assert "Figure evidence." in serialized and relative in serialized
    assert opened_visual_sources(request.messages) == expected
    assert (fixture.root / relative).read_bytes() == original


def test_cached_native_reservations_can_exhaust_attachment_allowance(mineru_reads):
    fixture = mineru_reads
    fixture.tools.register(_definition("inspect", lambda *_: {
        "native_multimodal": True, "inspection_cached": True, "path": fixture.paths[0],
    }, role="research"))
    reads = [_read(fixture, "read", 0)]
    inspections = [_tool(f"inspect-{index}", "inspect", {"path": fixture.paths[0]}) for index in range(20)]
    run = _run(fixture, [_response(*reads, *inspections, stop_reason="tool_use"), _response(_text("done"))])
    sent = run.requests[1].messages
    assert _image_count(sent) == 0
    assert len(_results(sent)) == 21
    assert all(entry["status"] == "deferred" and entry["reason"] == "batch_image_limit"
               for entry in _payload(_results(sent)["read"])["attached_images"])


def test_captured_attachment_batch_externalizes_only_actually_attached_images(mineru_reads):
    from export.training import TrainingExporter
    from qc.routing import IssueLedger
    from state.artifacts import ArtifactStore
    from state.workspace import WorkspaceState

    fixture = mineru_reads
    run = _run(fixture, [
        _response(*[_read(fixture, f"read-{index}", index) for index in range(8)],
                  stop_reason="tool_use"),
        _response(_text("done")),
    ])
    workspace = WorkspaceState(fixture.root)
    exporter = TrainingExporter(workspace, ArtifactStore(fixture.root, workspace_state=workspace),
                                IssueLedger(fixture.root), run_mode="synthesis")
    exporter.capture(fixture.context.owner_id, fixture.context.role, run.result.checkpoint)
    checkpoint_path, = (fixture.root / "_trace/raw/segments").glob("segment-*.json")
    stored = json.loads(checkpoint_path.read_text())
    assert tool_boundary(stored["messages"]).is_closed
    assert _image_count(stored["messages"]) == 20
    assert "base64" not in json.dumps(stored["messages"])
    assert "_multimodal_images" not in json.dumps(stored["messages"])
    for message in stored["messages"]:
        for image in image_blocks(message.get("content")):
            source = image["source"]
            assert source["type"] == "blob_ref" and "data" not in source
            assert hashlib.sha256((fixture.root / source["path"]).read_bytes()).hexdigest() == source["sha256"]
    results = _results(stored["messages"])
    assert sum(entry["status"] == "deferred" for result in results.values()
               for entry in _payload(result)["attached_images"]) == 12
    assert not exporter.manifest_path.exists()
