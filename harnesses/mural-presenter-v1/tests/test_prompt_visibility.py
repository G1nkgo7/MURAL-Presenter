from __future__ import annotations

import copy
from io import BytesIO
import json

from PIL import Image
import pytest

from export.training import _assistant_internal_path_markers, _scrub_ephemeral_training_text
from runtime.agent import _deduplicate_read_payload, _image_receipts, _message_history_breakdown, _visible_read_fingerprints
from runtime.config import ProviderSettings
from runtime.language_policy import append_language_reminder, visible_output_language_reminder
from runtime.prompt_visibility import model_visible_messages
from runtime.recovery import context_compaction_summary
from runtime.session import ModelRequest, estimate_request_tokens, to_openai_messages, to_openai_responses_input
from runtime.vision import image_source, opened_visual_sources


def _inspection_messages(form="multimodal"):
    receipt = {
        "kind": "page", "native_multimodal": True, "path": "renders/slide_02.png",
        "status": "critic_feedback_available",
        "pending_inspection": {
            "kind": "page", "page": 2, "path": "renders/slide_02.png", "review_stage": "final",
            "scope_key": "private-scope", "inspection_id": "inspection-" + "1" * 20,
            "page_uid": "pg_02_" + "2" * 16, "owner_id": "slide-2", "png_sha256": "b" * 64,
            "members": [{"page": 2, "page_uid": "pg_02_" + "2" * 16, "png_sha256": "b" * 64}],
        },
        "critic_feedback": {
            "completed": True, "body": "Move the bottom label inward.",
            "critic_question": "private rubric " * 500,
            "full_feedback_ref": "_state/inspections/private.json",
            "model_config": {"max_tokens": 8192}, "auto_resolved_issue_ids": ["issue-actionable"],
        },
        "visual_source": {"owner_id": "slide-2", "path": "renders/slide_02.png", "sha256": "b" * 64},
    }
    buffer = BytesIO()
    Image.new("RGB", (32, 32), "white").save(buffer, format="PNG")
    content = json.dumps(receipt)
    if form == "dict":
        content = receipt
    elif form == "multimodal":
        content = [{"type": "text", "text": content}, {"type": "image", "source": image_source(buffer.getvalue())}]
    return [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "inspect-call", "name": "inspect",
                                             "input": {"page": 2, "review_stage": "final"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "inspect-call", "content": content}]},
    ]


def _receipt(messages):
    content = messages[-1]["content"][0]["content"]
    if isinstance(content, list):
        content = content[0]["text"]
    return json.loads(content) if isinstance(content, str) else content


@pytest.mark.parametrize("form", ["text", "dict", "multimodal"])
def test_runtime_and_training_share_visible_inspection_receipt(form):
    messages = _inspection_messages(form)
    original = copy.deepcopy(messages)
    visible = model_visible_messages(messages)
    exported = _scrub_ephemeral_training_text(messages)
    expected = _receipt(visible)
    assert expected == _receipt(exported)
    assert expected["page"] == 2 and expected["review_stage"] == "final"
    assert expected["critic_feedback"]["body"] == "Move the bottom label inward."
    assert expected["critic_feedback"]["auto_resolved_issue_ids"] == ["issue-actionable"]
    text = json.dumps(expected)
    for field in ("pending_inspection", "members", "page_uid", "owner_id", "scope_key", "sha256", "critic_question", "model_config", "full_feedback_ref"):
        assert field not in text
    assert visible[0] == messages[0]
    assert visible[-1]["content"][0]["tool_use_id"] == "inspect-call"
    assert messages == original
    if form == "multimodal":
        assert visible[-1]["content"][0]["content"][1] == messages[-1]["content"][0]["content"][1]
        assert exported[-1]["content"][0]["content"][1] == messages[-1]["content"][0]["content"][1]


def test_legacy_consumed_visual_marker_is_presented_as_confirmed_delivery():
    messages = [{"role": "user", "content": [{
        "type": "tool_result",
        "tool_use_id": "inspect-old",
        "content": json.dumps({
            "kind": "page",
            "page": 2,
            "native_multimodal": False,
            "visual_payload_status": "consumed_in_previous_model_turn",
            "status": "consumed_in_previous_model_turn",
            "note": "old ambiguous wording",
        }),
    }]}]

    visible = _receipt(model_visible_messages(messages))
    assert "native_multimodal" not in visible
    assert visible["visual_payload_status"] == "pixels_delivered_in_previous_model_turn"
    assert visible["status"] == "pixels_delivered_in_previous_model_turn"
    assert visible["pixels_delivered_to_this_owner"] is True
    assert visible["pixels_active_in_current_context"] is False
    assert "do not report this marker as missing pixels" in visible["note"]


def test_all_provider_wires_hide_receipts_without_breaking_checkpoint_replay():
    messages = _inspection_messages()
    request = ModelRequest(model="test-model", system="system", messages=tuple(messages))
    settings = ProviderSettings(api_format="anthropic", model="test-model", base_url="https://unused.example",
                                prompt_cache=False, prompt_cache_messages=False)
    wires = [request.anthropic_kwargs(settings), to_openai_messages("system", request.messages),
             to_openai_responses_input("system", request.messages)]
    for wire in wires:
        serialized = json.dumps(wire)
        assert "private-scope" not in serialized and "private rubric" not in serialized
        assert "inspection-" + "1" * 20 not in serialized
        assert "Move the bottom label inward." in serialized
        assert "inspect-call" in serialized
    assert request.messages == tuple(messages)
    assert _image_receipts(request.messages)[0][1]["inspection_id"] == "inspection-" + "1" * 20
    assert opened_visual_sources(request.messages, owner_id="slide-2", attached_only=True)[0]["sha256"] == "b" * 64
    assert estimate_request_tokens("", messages) == estimate_request_tokens("", model_visible_messages(messages))
    assert _message_history_breakdown(messages) == _message_history_breakdown(model_visible_messages(messages))


def test_compaction_reads_visible_receipts_not_private_metadata():
    from runtime.session import NormalizedAssistantResponse, SessionResult

    messages = _inspection_messages()
    original = copy.deepcopy(messages)
    requests = []

    def invoke(request):
        requests.append(request)
        return SessionResult(NormalizedAssistantResponse(
            content=({"type": "text", "text": "Repair P2 bottom label, then finish."},),
            analysis_summaries=(), stop_reason="end_turn", usage={}), "fixture", 1)

    settings = ProviderSettings(api_format="anthropic", model="test-model", base_url="https://unused.example")
    context_compaction_summary(messages, agent_id="slide-2", role="slide", current_task={}, settings=settings,
                               max_tokens=4096, thinking=False, effort="", target_tokens=1024, invoke=invoke)
    text = json.dumps([request.messages for request in requests])
    assert "private-scope" not in text and "private rubric" not in text
    assert "Move the bottom label inward." in text
    assert messages == original


def test_compacted_visual_source_notice_retains_paths_not_audit_ids():
    notice = {"opened_visual_sources": [{"owner_id": "private-owner", "sha256": "a" * 64,
                                        "path": "assets/chart.png", "page": 2}],
              "opened_not_verified": "Opening an image does not prove it passed review."}
    messages = [{"role": "user", "content": [{"type": "text", "text":
        "[Tool-opened visual sources]\n" + json.dumps(notice)}]}]
    visible = model_visible_messages(messages)
    text = visible[0]["content"][0]["text"]
    assert "private-owner" not in text and "a" * 64 not in text
    assert "assets/chart.png" in text and notice["opened_not_verified"] in text
    assert _scrub_ephemeral_training_text(messages) == visible
    assert opened_visual_sources(messages)[0]["owner_id"] == "private-owner"


def test_training_multimodal_text_scrub_preserves_image_blobs_and_exact_tool_arguments():
    arguments = {"path": "example.json", "content": json.dumps({
        "owner_id": "user-data", "page_uid": "pg_02_aaaaaaaaaaaaaaaa", "hash": "a" * 64,
        "values": [{"text": "</parameter>\n</invoke>"}, [1, 2]],
    })}
    messages = [{"role": "assistant", "content": [
        {"type": "tool_use", "id": "write-id", "name": "write_file", "input": arguments},
    ]}, {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "write-id", "content": [
        {"type": "text", "text": json.dumps({"inspection_id": "inspection-" + "1" * 20,
                                               "page_uid": "pg_02_aaaaaaaaaaaaaaaa", "keep": "useful"})},
        {"type": "image", "source": {"type": "blob_ref", "path": "blobs/image.png", "sha256": "a" * 64}},
    ]}]}]
    exported = _scrub_ephemeral_training_text(messages)
    assert exported[0] == messages[0]
    content = exported[1]["content"][0]["content"]
    assert json.loads(content[0]["text"]) == {"keep": "useful"}
    assert content[1] == messages[1]["content"][0]["content"][1]
    assert exported[1]["content"][0]["tool_use_id"] == "write-id"


def test_training_rejects_assistant_internal_paths_but_not_transport_blob_refs():
    messages = [{"role": "assistant", "content": [
        {"type": "tool_use", "id": "read-private", "name": "read_file",
         "input": {"path": "_state/private.json"}},
    ]}, {"role": "user", "content": [
        {"type": "image", "source": {"type": "blob_ref",
         "path": "_trace/raw/blobs/image.png", "sha256": "a" * 64}},
    ]}]
    markers = _assistant_internal_path_markers(messages)
    assert markers == [{"message_index": 0, "marker": "_state/", "field": "tool_arguments"}]
    assert _assistant_internal_path_markers([{"role": "assistant", "content": [
        {"type": "tool_use", "id": "revision", "name": "write_file",
         "input": {"path": "revision-operations.json", "content": "{}"}},
    ]}]) == []


def test_visibility_does_not_strip_user_documents_or_unknown_json():
    document = json.dumps({"kind": {"nested": [1]}, "owner_id": "user-owner", "state": {"token": 123}})
    messages = [{"role": "user", "content": [{"type": "text", "text": document}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "read",
                    "content": json.dumps({"path": "example.json", "content": document})}]}]
    assert model_visible_messages(messages) == messages
    assert _scrub_ephemeral_training_text(messages) == messages


def _read_history(payload):
    return [{"role": "assistant", "content": [{"type": "tool_use", "id": "read-1", "name": "read_file", "input": {"path": payload["path"]}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "read-1", "content": json.dumps(payload)}]}]


def test_duplicate_reads_require_exact_excerpt_still_in_context():
    payload = {"path": "research.md", "offset": 1, "mode": "lines", "content": "Evidence.\n" * 1000,
               "next_offset": 1001, "truncated": True}
    history = _read_history(payload)
    original = copy.deepcopy(history)
    duplicate = _deduplicate_read_payload(payload, _visible_read_fingerprints(history))
    assert "content" not in duplicate
    assert duplicate["content_status"] == "unchanged_excerpt_already_in_context"
    assert duplicate["next_offset"] == 1001 and duplicate["truncated"] is True
    assert history == original
    for changed in ({**payload, "content": payload["content"] + "new"}, {**payload, "offset": 2},
                    {**payload, "path": "other.md"}, {**payload, "mode": "query"}):
        assert _deduplicate_read_payload(changed, _visible_read_fingerprints(history)) == changed
    compacted = [{"role": "user", "content": "Summary only; old file text is no longer here."}]
    assert _deduplicate_read_payload(payload, _visible_read_fingerprints(compacted)) == payload
    assert _deduplicate_read_payload(payload, _visible_read_fingerprints(_read_history(duplicate))) == payload
    failed = copy.deepcopy(history)
    failed[1]["content"][0]["is_error"] = True
    assert _deduplicate_read_payload(payload, _visible_read_fingerprints(failed)) == payload


def test_same_batch_dedup_preserves_pending_attachment_images():
    payload = {"path": "document.md", "content": "figure description " * 500,
               "_multimodal_images": [{"path": "images/figure.png"}]}
    seen = set()
    assert _deduplicate_read_payload(payload, seen) == payload
    duplicate = _deduplicate_read_payload(payload, seen)
    assert duplicate["_multimodal_images"] == payload["_multimodal_images"]
    assert "content" not in duplicate


def test_language_reminder_dedup_reissues_after_compaction_or_policy_change():
    zh = visible_output_language_reminder({"working_language": "zh"})
    en = visible_output_language_reminder({"working_language": "en"})
    messages = []
    append_language_reminder(messages, zh)
    for _ in range(10):
        append_language_reminder(messages, zh)
    assert len(messages) == 1
    append_language_reminder(messages, en)
    append_language_reminder(messages, zh)
    assert len(messages) == 3
    append_language_reminder(messages, zh, force=True)
    assert len(messages) == 4
    compacted = [{"role": "user", "content": "Summary only"}]
    append_language_reminder(compacted, zh)
    assert len(compacted) == 2
    compacted_with_policy = [{"role": "user", "content": "Continue.\n" + zh}]
    append_language_reminder(compacted_with_policy, zh)
    assert len(compacted_with_policy) == 1
