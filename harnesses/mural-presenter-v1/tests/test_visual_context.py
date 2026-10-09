import base64
import copy
from io import BytesIO
import json

from PIL import Image
import pytest

from runtime.agent import AgentProtocolError, AgentProviderError, AgentRuntime, _vision_result_block
from runtime.recovery import CompactionSummaryError, ProviderRequestTimeout, SegmentCheckpoint, tool_boundary
from runtime.session import ModelRequest, ProviderChannel, estimate_request_tokens, openai_chat_body, openai_responses_body
from runtime.tools import ToolContext, ToolExecutionError, ToolRuntime
from runtime.vision import (VisionBudgetError, check_request_bytes,
    image_blocks, image_source, opened_visual_sources, release_consumed_visual_context,
    visual_compaction_tail)
from test_agent_runtime import ScriptedTransport, _definition, _response, _settings, _text, _tool, _summary_invoker


def _source(size=(24, 16), format="PNG"):
    buffer = BytesIO()
    Image.new("RGB", size, "navy").save(buffer, format=format)
    return image_source(buffer.getvalue())


def _history(count, *, keep=()):
    source = _source()
    messages = [{"role": "user", "content": "Compare the sequence"}]
    for index in range(count):
        messages += [
            {"role": "assistant", "content": [
                {"type": "text", "text": f"Observation before page {index}"},
                {"type": "tool_use", "id": str(index), "name": "vision_analyze", "input": {"path": f"p{index}.png"}},
            ]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": str(index),
                "vision_keep": index in keep, "content": [
                    {"type": "text", "text": json.dumps({"path": f"p{index}.png", "sha256": f"hash-{index}"})},
                    {"type": "image", "source": copy.deepcopy(source)},
                ]}]},
        ]
    return messages


def _count(messages):
    return sum(1 for message in messages for _ in image_blocks(message.get("content")))


def _compact(messages):
    runtime = AgentRuntime(settings=_settings(), channels=(ProviderChannel("test", "a"),),
        transport=ScriptedTransport([]), tools=ToolRuntime(), summary_invoker=_summary_invoker)
    return runtime._compact_messages(messages, system="fixture", tools=(), max_tokens=128,
        agent_id="material", role="material")


def _opened_history(count, *, owner_id="material"):
    messages = _history(count)
    for index in range(count):
        part = messages[index * 2 + 2]["content"][0]["content"][0]
        payload = json.loads(part["text"])
        payload["visual_source"] = {"owner_id": owner_id, **payload}
        part["text"] = json.dumps(payload)
    return messages


def _payload(block):
    raw = block["content"]
    return json.loads(raw[0]["text"] if isinstance(raw, list) else raw)


def test_consumed_tool_images_release_but_user_and_explicit_references_remain():
    messages = [
        {"role": "user", "content": [{"type": "image", "source": _source()}]},
        *_history(2, keep=(0,)),
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "write", "name": "write_file", "input": {"path": "x", "content": "done"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "write", "content": "{\"status\":\"ok\"}"},
        ]},
    ]
    released, metrics = release_consumed_visual_context(messages)
    assert metrics == {
        "images_released": 1,
        "images_retained_one_extra_turn": 1,
        "retention_requests_expired": 0,
        "inspection_receipts_superseded": 0,
    }
    assert _count(released) == 2
    consumed = released[5]["content"][0]
    payload = json.loads(consumed["content"][0]["text"])
    assert payload["visual_payload_status"] == "pixels_delivered_in_previous_model_turn"
    assert payload["pixels_delivered_to_this_owner"] is True
    assert payload["pixels_active_in_current_context"] is False
    assert "native_multimodal" not in payload
    assert "do not report this marker as missing pixels" in payload["note"]
    assert not any(part["type"] == "image" for part in consumed["content"])
    assert _count(messages) == 3


def test_older_inspection_for_changed_pixels_becomes_a_small_superseded_marker():
    messages = _history(2)
    for index, message_index in enumerate((2, 4), start=1):
        block = messages[message_index]["content"][0]
        payload = json.loads(block["content"][0]["text"])
        payload.update(kind="page", page=3, png_sha256=f"pixels-{index}")
        block["content"][0]["text"] = json.dumps(payload)
    messages.extend([
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "next", "name": "write_file", "input": {"path": "x", "content": "done"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "next", "content": "{}"},
        ]},
    ])
    released, metrics = release_consumed_visual_context(messages)
    assert metrics["images_released"] == 2
    assert metrics["inspection_receipts_superseded"] == 1
    old = json.loads(released[2]["content"][0]["content"])
    current = json.loads(released[4]["content"][0]["content"][0]["text"])
    assert old["status"] == "superseded_by_newer_pixels"
    assert current["png_sha256"] == "pixels-2"


def test_explicit_retention_is_capped_and_expires_after_one_extra_decision():
    messages = _history(4, keep=(0, 1, 2, 3))
    messages.extend([
        {"role": "assistant", "content": [{"type": "text", "text": "compared"}]},
        {"role": "user", "content": "continue"},
    ])
    retained, first = release_consumed_visual_context(messages, max_retained_images=2)
    assert first["images_released"] == 2
    assert first["images_retained_one_extra_turn"] == 2
    assert first["retention_requests_expired"] == 2
    assert _count(retained) == 2
    assert "hash-2" in str(retained) and "hash-3" in str(retained)
    retained.extend([
        {"role": "assistant", "content": [{"type": "text", "text": "decision made"}]},
        {"role": "user", "content": "continue"},
    ])
    released, second = release_consumed_visual_context(retained, max_retained_images=2)
    assert second["images_released"] == 2
    assert second["images_retained_one_extra_turn"] == 0
    assert _count(released) == 0


def test_mislabeled_jpeg_is_sent_with_actual_mime_without_modifying_file(tmp_path):
    target = tmp_path / "generated.png"
    Image.new("RGB", (24, 16), "red").save(target, format="JPEG")
    original = target.read_bytes()
    block = _vision_result_block({"type": "tool_result", "tool_use_id": "v", "content": "{}"},
        target.name, ToolContext("i", "image", metadata={"workspace_root": str(tmp_path)}))
    source = block["content"][-1]["source"]
    assert source["media_type"] == "image/jpeg"
    assert base64.b64decode(source["data"]) == original == target.read_bytes()
    receipt = json.loads(block["content"][0]["text"])
    assert receipt["visual_source"]["path"] == target.name
    assert len(receipt["visual_source"]["sha256"]) == 64


def test_opened_sources_keep_owner_and_file_version_without_claiming_verification(tmp_path):
    relative = "inputs/extracted/att-0123456789ab/pages/page_002.png"
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    Image.new("RGB", (24, 16), "red").save(target)
    context = ToolContext("material-a", "material", metadata={"workspace_root": str(tmp_path)})
    block = {"type": "tool_result", "tool_use_id": "v", "content": "{}"}
    first = _payload(_vision_result_block(block, relative, context))
    source = first["visual_source"]
    assert source["owner_id"] == "material-a"
    assert source["path"] == relative
    assert source["attachment_id"] == "att-0123456789ab"
    assert source["page"] == 2
    assert "opened_visual_sources" not in first
    assert "opened_not_verified" not in first
    sources = [{"owner_id": "material-a", "path": relative,
        "sha256": source["sha256"], "attachment_id": "att-0123456789ab", "page": 2}]
    reopened = _payload(_vision_result_block(block, relative, context, opened_sources=sources))
    assert reopened["visual_source"]["sha256"] == source["sha256"]
    assert "opened_visual_sources" not in reopened
    Image.new("RGB", (24, 16), "blue").save(target)
    changed = _payload(_vision_result_block(block, relative, context, opened_sources=sources))
    assert changed["visual_source"]["sha256"] != source["sha256"]
    assert "opened_visual_sources" not in changed
    other = _payload(_vision_result_block(block, relative,
        ToolContext("material-b", "material", metadata=context.metadata),
        opened_sources=(changed["visual_source"],)))
    assert other["visual_source"]["owner_id"] == "material-b"
    assert "opened_visual_sources" not in other


def test_opened_source_recovery_ignores_failed_receipts_unattributed_history_and_other_owners():
    messages = _opened_history(3, owner_id="material-a")
    messages[2]["content"][0]["is_error"] = True
    legacy = messages[4]["content"][0]["content"][0]
    payload = json.loads(legacy["text"])
    del payload["visual_source"]["owner_id"]
    legacy["text"] = json.dumps(payload)
    assert opened_visual_sources(messages, owner_id="material-b") == []
    assert opened_visual_sources(messages, owner_id="material-a") == [
        {"owner_id": "material-a", "path": "p2.png", "sha256": "hash-2"}]


def test_opened_sources_survive_visual_and_repeated_token_compaction():
    messages = _opened_history(21)
    expected = opened_visual_sources(messages, owner_id="material")
    tail = visual_compaction_tail(messages, owner_id="material", target_tokens=20000, input_budget_tokens=20000)[0]
    assert _count(tail) == 3 and tool_boundary(tail).is_closed
    assert opened_visual_sources(tail, owner_id="material") == expected
    assert visual_compaction_tail(tail, owner_id="material", target_tokens=20000, input_budget_tokens=20000)[0] == tail
    for message in tail:
        for block in message["content"] if isinstance(message["content"], list) else []:
            if block.get("type") == "tool_result":
                block["content"] = [part for part in block["content"] if part["type"] != "image"]
    text_only = visual_compaction_tail(tail, owner_id="material", target_tokens=20000, input_budget_tokens=20000)[0]
    assert _count(text_only) == 0 and tool_boundary(text_only).is_closed
    assert opened_visual_sources(text_only, owner_id="material") == expected


def test_material_later_vision_receipts_and_resume_keep_only_successful_openings(tmp_path):
    from test_agent_runtime import _run
    for name in ("first.png", "second.png"):
        Image.new("RGB", (24, 16), "navy").save(tmp_path / name)

    def handler(_context, arguments):
        if arguments["path"] == "missing.png":
            raise ToolExecutionError("missing_image", "fixture image is unavailable")
        return {"path": arguments["path"], "native_multimodal": not arguments.get("cached"),
                "inspection_cached": bool(arguments.get("cached"))}

    tools = ToolRuntime()
    tools.register(_definition("vision_analyze", handler, role="material"))
    context = ToolContext("material", "material", metadata={"workspace_root": str(tmp_path)})
    failed = ScriptedTransport([_response(
        _tool("first", "vision_analyze", {"path": "first.png"}),
        _tool("missing", "vision_analyze", {"path": "missing.png"}),
        _tool("second", "vision_analyze", {"path": "second.png"}), stop_reason="tool_use"),
        ValueError("fixture provider did not accept the images")])
    saved = []
    with pytest.raises(AgentProviderError):
        _run(failed, tools, context, checkpoint_sink=saved.append)
    blocks = saved[-1].messages[-1]["content"]
    assert [_payload(blocks[index])["visual_source"]["path"] for index in (0, 2)] == ["first.png", "second.png"]
    assert blocks[1]["is_error"] and "opened_visual_sources" not in blocks[1]["content"]
    expected = opened_visual_sources(saved[-1].messages, owner_id="material")
    assert [source["path"] for source in expected] == ["first.png", "second.png"]
    checkpoint = SegmentCheckpoint.from_dict(json.loads(json.dumps(saved[-1].to_dict())))
    resumed = ScriptedTransport([
        _response(_tool("cached", "vision_analyze", {"path": "second.png", "cached": True}), stop_reason="tool_use"),
        _response(_tool("reopen", "vision_analyze", {"path": "second.png"}), stop_reason="tool_use"),
        _response(_text("Opened sources remain available; verification is still separate")),
    ])
    runtime = AgentRuntime(summary_invoker=_summary_invoker, settings=_settings(), channels=(ProviderChannel("secret", "a"),),
        transport=resumed, tools=tools)
    result = runtime.run(agent_id="material", role="material", system="fixture", tool_context=context,
        checkpoint=checkpoint, max_turns=4, max_tokens=128, thinking=False, effort="low")
    assert resumed.calls[0][1].messages == checkpoint.messages
    cached = resumed.calls[1][1].messages[-1]["content"][0]
    assert isinstance(cached["content"], str) and "opened_visual_sources" not in _payload(cached)
    reopened = _payload(resumed.calls[2][1].messages[-1]["content"][0])
    assert reopened["visual_source"]["path"] == "second.png"
    assert "opened_visual_sources" not in reopened
    assert opened_visual_sources(result.checkpoint.messages, owner_id="material") == expected
    assert len(tools.events) == 10 and tool_boundary(result.checkpoint.messages).is_closed


def test_material_opened_sources_remain_readable_after_runtime_token_compaction_and_resume(tmp_path):
    from dataclasses import replace
    messages = _opened_history(6)
    messages[0]["content"] = "x" * 60000
    expected = opened_visual_sources(messages, owner_id="material")
    context = ToolContext("material", "material", metadata={"workspace_root": str(tmp_path)})
    transport = ScriptedTransport([_response(_text("compacted"))])
    runtime = AgentRuntime(summary_invoker=_summary_invoker, settings=replace(_settings(), context_window=20000, context_safety_buffer=1000),
        channels=(ProviderChannel("secret", "a"),), transport=transport, tools=ToolRuntime())
    result = runtime.run(agent_id="material", role="material", system="fixture", tool_context=context,
        checkpoint=SegmentCheckpoint.initial(messages), max_turns=2, max_tokens=128, thinking=False, effort="low")
    assert opened_visual_sources(transport.calls[0][1].messages, owner_id="material") == expected
    assert _count(transport.calls[0][1].messages) == 3
    checkpoint = SegmentCheckpoint.from_dict(json.loads(json.dumps(result.checkpoint.to_dict())))
    Image.new("RGB", (24, 16), "blue").save(tmp_path / "new.png")
    tools = ToolRuntime()
    tools.register(_definition("vision_analyze", lambda *_: {"native_multimodal": True}, role="material"))
    resumed = ScriptedTransport([
        _response(_tool("new", "vision_analyze", {"path": "new.png"}), stop_reason="tool_use"),
        _response(_text("done")),
    ])
    runtime = AgentRuntime(summary_invoker=_summary_invoker, settings=_settings(), channels=(ProviderChannel("secret", "a"),),
        transport=resumed, tools=tools)
    result = runtime.run(agent_id="material", role="material", system="fixture", tool_context=context,
        checkpoint=checkpoint, max_turns=3, max_tokens=128, thinking=False, effort="low")
    payload = _payload(resumed.calls[1][1].messages[-1]["content"][0])
    assert payload["visual_source"]["path"] == "new.png"
    sources = opened_visual_sources(result.checkpoint.messages, owner_id="material")
    assert sources[:-1] == expected and sources[-1]["path"] == "new.png"
    assert tool_boundary(result.checkpoint.messages).is_closed


def test_runtime_does_not_merge_another_owners_opened_sources(tmp_path):
    Image.new("RGB", (24, 16), "blue").save(tmp_path / "new.png")
    tools = ToolRuntime()
    tools.register(_definition("vision_analyze", lambda *_: {"native_multimodal": True}, role="material"))
    transport = ScriptedTransport([
        _response(_tool("new", "vision_analyze", {"path": "new.png"}), stop_reason="tool_use"),
        _response(_text("done")),
    ])
    runtime = AgentRuntime(summary_invoker=_summary_invoker, settings=_settings(), channels=(ProviderChannel("secret", "a"),),
        transport=transport, tools=tools)
    runtime.run(agent_id="material-b", role="material", system="fixture",
        tool_context=ToolContext("material-b", "material", metadata={"workspace_root": str(tmp_path)}),
        checkpoint=SegmentCheckpoint.initial(_opened_history(2, owner_id="material-a")),
        max_turns=3, max_tokens=128, thinking=False, effort="low")
    source = _payload(transport.calls[1][1].messages[-1]["content"][0])["visual_source"]
    assert source["owner_id"] == "material-b"
    assert source["path"] == "new.png"


def test_text_only_native_result_keeps_original_response_without_claiming_opening():
    from test_agent_runtime import _run
    tools = ToolRuntime()
    tools.register(_definition("vision_analyze", lambda *_: "fixture returned no image", role="material"))
    transport = ScriptedTransport([
        _response(_tool("text", "vision_analyze", {}), stop_reason="tool_use"),
        _response(_text("done")),
    ])
    _run(transport, tools, ToolContext("material", "material"))
    payload = _payload(transport.calls[1][1].messages[-1]["content"][0])
    assert payload["result"] == "fixture returned no image"
    assert "opened_visual_sources" not in payload and "visual_source" not in payload


@pytest.mark.parametrize("invalid_image", [False, True])
def test_failed_image_attachment_does_not_record_an_opening(tmp_path, invalid_image):
    from PIL import UnidentifiedImageError
    from test_agent_runtime import _run
    if invalid_image:
        (tmp_path / "bad.png").write_bytes(b"not an image")
    Image.new("RGB", (24, 16), "blue").save(tmp_path / "good.png")
    tools = ToolRuntime()
    tools.register(_definition("vision_analyze", lambda *_: {"native_multimodal": True}, role="material"))
    transport = ScriptedTransport([
        _response(_tool("good", "vision_analyze", {"path": "good.png"}), stop_reason="tool_use"),
        _response(_tool("bad", "vision_analyze", {"path": "bad.png"}), stop_reason="tool_use"),
    ])
    saved = []
    with pytest.raises(UnidentifiedImageError if invalid_image else AgentProtocolError):
        _run(transport, tools, ToolContext("material", "material", metadata={"workspace_root": str(tmp_path)}),
            checkpoint_sink=saved.append)
    assert saved
    assert [source["path"] for source in opened_visual_sources(saved[-1].messages, owner_id="material")] == ["good.png"]
    assert all(source["path"] != "bad.png" for checkpoint in saved
        for source in opened_visual_sources(checkpoint.messages, owner_id="material"))


def test_budget_batches_old_images_preserving_recent_references_and_receipts():
    messages = _opened_history(21)
    messages[2]["content"][0]["vision_keep"] = True
    before = copy.deepcopy(messages)
    selected, result = _compact(messages)
    assert result["images_before"] == 21 and 1 < result["images_after"] < 21
    assert messages == before
    assert all(message in selected for message in before[1:3])
    assert selected[-6:] == before[-6:]
    assert tool_boundary(selected).is_closed
    for index in range(21):
        assert f"hash-{index}" in str(selected)
        assert f"p{index}.png" in str(selected)
    stable = copy.deepcopy(selected)
    assert _compact(selected) is None and selected == stable


def test_many_old_images_in_one_tool_batch_are_released_together():
    messages = _opened_history(4)
    old = messages[2]["content"][0]
    old["content"] += [{"type": "image", "source": _source()} for _ in range(20)]
    selected, _ = _compact(messages)
    assert _count(selected) == 3
    assert "hash-0" in str(selected) and tool_boundary(selected).is_closed


def test_pending_results_split_across_messages_are_never_removed():
    history = _history(21)
    messages = [history[0], {"role": "assistant", "content": [
        message["content"][-1] for message in history if message["role"] == "assistant"]},
        *[message for message in history[1:] if message["role"] == "user"]]
    before = copy.deepcopy(messages)
    with pytest.raises(CompactionSummaryError, match="protected context"):
        _compact(messages)
    assert messages == before
    assert tool_boundary(messages).is_closed
    with pytest.raises(VisionBudgetError, match="vision_context_limit"):
        ModelRequest(model="test", system="test", messages=tuple(messages))


def test_request_byte_pressure_also_compacts_and_protected_overflow_is_explicit(monkeypatch):
    import runtime.vision as vision
    messages = _history(10)
    monkeypatch.setattr(vision, "MAX_REQUEST_BYTES", 70000)
    selected, result = _compact(messages)
    assert result["images_released"] > 0
    assert _count(selected) >= 1
    assert result["bytes_after"] <= 70000 - 65536
    with pytest.raises(VisionBudgetError, match="request_bytes_exceeded"):
        check_request_bytes({"text": "x" * 70001})
    protected = _history(21, keep=tuple(range(21)))
    with pytest.raises(CompactionSummaryError, match="protected context"):
        _compact(protected)
    with pytest.raises(VisionBudgetError, match="vision_context_limit"):
        ModelRequest(model="test", system="test", messages=tuple(protected))


def test_token_compaction_keeps_recent_comparisons_and_older_key_reference():
    messages = _history(6, keep=(0,))
    messages[0]["content"] = [{"type": "text", "text": "Original reference"}, {"type": "image", "source": _source()}]
    tail = visual_compaction_tail(messages, target_tokens=28000, input_budget_tokens=28000)[0]
    assert _count(tail) == 5  # 当前预算容纳近期截图、显式参考和用户原图，不是固定轮数上限。
    assert tail[-6:] == messages[-6:]
    assert "hash-0" in str(tail)
    assert tool_boundary(tail).is_closed


def test_request_normalizes_mime_once_and_strips_internal_retention_fields():
    source = _source(format="JPEG")
    source["media_type"] = "image/png"
    messages = _history(1, keep=(0,))
    messages[-1]["content"][0]["content"][-1]["source"] = source
    request = ModelRequest(model="test", system="test", messages=tuple(messages))
    assert request.messages[-1]["content"][0]["content"][-1]["source"]["media_type"] == "image/jpeg"
    wire = request.anthropic_kwargs(_settings())
    assert "vision_keep" not in str(wire)
    assert "data:image/jpeg;base64," in str(openai_chat_body(request))
    assert "data:image/jpeg;base64," in str(openai_responses_body(request))


def test_visual_release_survives_failure_resume_and_failover_without_reexecuting_tools(tmp_path):
    Image.new("RGB", (24, 16), "blue").save(tmp_path / "page.png")
    tools = ToolRuntime()
    tools.register(_definition("vision_analyze", lambda *_: {"native_multimodal": True}, role="material"))
    responses = [_response(_text(f"Observation {i}"), _tool(f"v{i}", "vision_analyze", {
        "path": "page.png", "retain": i == 0}), stop_reason="tool_use") for i in range(21)]
    transport = ScriptedTransport([*responses, ProviderRequestTimeout("first_byte", 2)])
    checkpoints, events = [], []
    context = ToolContext("material", "material", metadata={"workspace_root": str(tmp_path)})
    runtime = AgentRuntime(summary_invoker=_summary_invoker, settings=_settings(max_attempts=1), channels=(ProviderChannel("secret-a", "a"),),
        transport=transport, tools=tools, event_sink=events.append)
    from runtime.agent import AgentProviderError
    with pytest.raises(AgentProviderError):
        runtime.run(agent_id="material", role="material", system="fixture", tool_context=context,
            initial_messages=({"role": "user", "content": "review"},), max_turns=40,
            max_tokens=128, thinking=False, effort="low", checkpoint_sink=checkpoints.append)
    counts = [_count(request.messages) for _, request in transport.calls]
    assert counts[:21] == [0, 1, 2, *([1] * 18)]
    retained = counts[-1]
    assert retained == 1
    assert estimate_request_tokens(transport.calls[-1][1].system, transport.calls[-1][1].messages,
        transport.calls[-1][1].tools) <= runtime.compact_target_tokens
    assert not [e for e in events if e["event_type"] == "agent_context_compacted"]
    assert len([e for e in events if e["event_type"] == "agent_visual_context_released"]) == 19
    assert len(tools.events) == 42
    segments = {checkpoint.segment_id: checkpoint for checkpoint in checkpoints}
    assert [_count(checkpoint.messages) for checkpoint in segments.values()] == [3, *([2] * 18), retained]
    saved = SegmentCheckpoint.from_dict(json.loads(json.dumps(checkpoints[-1].to_dict())))
    resumed_transport = ScriptedTransport([ProviderRequestTimeout("first_byte", 2), _response(_text("Compared recent pages; done"))])
    resumed = AgentRuntime(summary_invoker=_summary_invoker, settings=_settings(), channels=(ProviderChannel("secret-a", "a"), ProviderChannel("secret-b", "b")),
        transport=resumed_transport, tools=tools, balance="primary-first")
    result = resumed.run(agent_id="material", role="material", system="fixture", tool_context=context,
        checkpoint=saved, max_turns=3, max_tokens=128, thinking=False, effort="low")
    assert [channel for channel, _ in resumed_transport.calls] == ["a", "b"]
    assert resumed_transport.calls[0][1].messages == resumed_transport.calls[1][1].messages == saved.messages
    assert len(tools.events) == 42 and tool_boundary(result.checkpoint.messages).is_closed
    assert _count(result.checkpoint.messages) == retained


def test_signed_visual_compaction_preserves_prior_segment_and_opens_unsigned_boundary():
    messages = _history(21)
    messages[1]["content"].insert(0, {"type": "thinking", "thinking": "private", "signature": "private-signature"})
    checkpoint = SegmentCheckpoint.initial(messages)
    transport = ScriptedTransport([_response(_text("done"))])
    runtime = AgentRuntime(summary_invoker=_summary_invoker, settings=_settings(), channels=(ProviderChannel("secret-a", "a"),),
        transport=transport, tools=ToolRuntime())
    result = runtime.run(agent_id="slide", role="slide", system="fixture", tool_context=ToolContext("slide", "slide"),
        checkpoint=checkpoint, max_turns=2, max_tokens=128, thinking=True, effort="low")
    assert len(result.segments) == 2 and _count(result.segments[0].messages) == 21
    assert result.checkpoint.predecessor_segment_id == checkpoint.segment_id
    assert result.checkpoint.reason == "visual_budget"
    assert "private-signature" not in str(transport.calls[0][1].messages)
    assert 1 < _count(transport.calls[0][1].messages) < 21
    assert estimate_request_tokens("fixture", transport.calls[0][1].messages) <= runtime.compact_target_tokens
    assert tool_boundary(result.checkpoint.messages).is_closed


def test_token_compaction_preserves_recent_consumed_images_not_only_pending():
    from dataclasses import replace
    messages = _history(6)
    messages[0]["content"] = "x" * 60000
    transport = ScriptedTransport([_response(_text("compared; done"))])
    runtime = AgentRuntime(summary_invoker=_summary_invoker, settings=replace(_settings(), context_window=20000, context_safety_buffer=1000),
        channels=(ProviderChannel("secret", "a"),), transport=transport, tools=ToolRuntime())
    result = runtime.run(agent_id="slide", role="slide", system="fixture", tool_context=ToolContext("slide", "slide"),
        checkpoint=SegmentCheckpoint.initial(messages), max_turns=2, max_tokens=128, thinking=False, effort="low")
    assert _count(transport.calls[0][1].messages) == 3
    assert list(transport.calls[0][1].messages)[-6:] == messages[-6:]
    assert tool_boundary(result.checkpoint.messages).is_closed


def test_failure_after_signed_compaction_is_attributed_to_current_segment():
    from runtime.agent import AgentProviderError
    messages = _history(21)
    messages[1]["content"].insert(0, {"type": "thinking", "thinking": "private", "signature": "private-signature"})
    checkpoint = SegmentCheckpoint.initial(messages)
    events, saved = [], []
    runtime = AgentRuntime(summary_invoker=_summary_invoker, settings=_settings(max_attempts=1), channels=(ProviderChannel("secret", "a"),),
        transport=ScriptedTransport([ValueError("fixture failure")]), tools=ToolRuntime(), event_sink=events.append)
    with pytest.raises(AgentProviderError):
        runtime.run(agent_id="slide", role="slide", system="fixture", tool_context=ToolContext("slide", "slide"),
            checkpoint=checkpoint, max_turns=2, max_tokens=128, thinking=True, effort="low", checkpoint_sink=saved.append)
    failed = next(event for event in events if event["event_type"] == "agent_failed")
    assert failed["segment_id"] == saved[-1].segment_id != checkpoint.segment_id


@pytest.mark.parametrize("scope", ["inspect", "group", "path"])
def test_explicit_reference_reopens_cached_pixels_without_recommitting_inspection(tmp_path, monkeypatch, scope):
    from test_inspection_transactions import _fixture, _run, _call, _response as response, _payload_and_parts, ScriptedTransport as Transport
    _, _, context, arguments, tools, committed = _fixture(tmp_path, monkeypatch, scope)
    _run(Transport([_call("inspect", arguments), response()]), tools, context)
    transport = Transport([_call("inspect", {**arguments, "retain": True}, "reopen"), response()])
    _run(transport, tools, context)
    payload, parts = _payload_and_parts(transport.requests[1])
    assert payload["inspection_cached"] and any(part["type"] == "image" for part in parts)
    assert len(committed) == 1
    assert transport.requests[1].messages[-1]["content"][0]["vision_keep"] is True


def test_image_token_estimate_covers_maximum_transport_dimensions():
    from runtime.session import estimate_request_tokens
    count = estimate_request_tokens("", [{"role": "user", "content": [{"type": "image", "source": _source((2000, 2000))}]}])
    assert 5184 <= count < 10000


def test_large_page_transport_is_bounded_and_byte_stable_without_changing_original():
    from runtime.vision import TARGET_IMAGE_BYTES, normalize_images, MAX_REQUEST_BYTES, request_bytes
    page = Image.effect_noise((1600, 1100), 30).convert("RGB")
    payload = BytesIO()
    page.save(payload, format="PNG")
    original = payload.getvalue()
    assert len(base64.b64encode(original)) > TARGET_IMAGE_BYTES
    source = image_source(original)
    assert len(source["data"]) <= TARGET_IMAGE_BYTES
    assert image_source(base64.b64decode(source["data"])) == source
    assert payload.getvalue() == original
    messages = [{"role": "user", "content": [{"type": "image", "source": source} for _ in range(20)]}]
    before = copy.deepcopy(messages)
    normalize_images(messages)
    assert messages == before and request_bytes({"messages": messages}) < MAX_REQUEST_BYTES
    assert _compact(messages) is None


def test_recent_consumed_batch_is_soft_priority_but_pending_images_remain():
    from runtime.vision import MAX_CONTEXT_IMAGES
    messages = _opened_history(2)
    first = messages[2]["content"][0]
    first["content"] += [{"type": "image", "source": _source()} for _ in range(MAX_CONTEXT_IMAGES)]
    pending = copy.deepcopy(messages[-1])
    selected, result = _compact(messages)
    assert result["images_before"] > MAX_CONTEXT_IMAGES
    assert result["images_after"] == 1
    assert selected[-1] == pending and tool_boundary(selected).is_closed
    assert "hash-0" in str(selected)
