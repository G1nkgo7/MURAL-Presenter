"""Model-facing views of runtime receipts; durable messages remain unchanged."""
from __future__ import annotations

import copy
import json
from typing import Any, Mapping, Sequence


_INSPECTION_INTERNAL_KEYS = frozenset({
    "agent_id", "attempt_id", "cache_hit", "critic_agent_id", "critic_prompt_version",
    "critic_question", "event_id", "full_feedback_ref", "geometry", "html_sha256", "image_sha256",
    "inspection_id", "inspect_timings_s", "language_policy_version", "members",
    "model_config", "opened_not_verified", "opened_visual_sources", "owner_id", "owner_question", "page_uid",
    "evidence_key", "inspection_cached", "native_multimodal", "transport_media_type",
    "png_sha256", "presentation_config", "question", "render_committed",
    "render_diagnostics", "reused_from", "reused_from_inspection_id", "role",
    "rubric_version", "scope_key", "segment_id", "sha256", "source_refs", "tool_use_id", "trace_path",
})
INSPECTION_VISIBLE_DESCRIPTOR_KEYS = ("kind", "page", "pages", "path", "review_stage")
INSPECTION_VISIBLE_VISUAL_SOURCE_KEYS = ("attachment_id", "media_type", "page", "path")
INSPECTION_VISIBLE_CRITIC_FEEDBACK_KEYS = frozenset({
    "asset_usage_status", "auto_resolved_issue_ids", "body", "body_char_count_full",
    "body_truncated_for_prompt", "checked_scope", "completed", "incomplete_reason",
    "missing_required_assets", "next_required_action", "not_checked", "skip_reason", "skipped", "status",
})
_OPENED_SOURCES_PREFIX = "[Tool-opened visual sources]\n"
_LEGACY_CONSUMED_VISUAL_STATUS = "consumed_in_previous_model_turn"
_DELIVERED_VISUAL_STATUS = "pixels_delivered_in_previous_model_turn"
_WORKFLOW_INTERNAL_KEYS = frozenset({
    "activation_fingerprint", "last_activation_fingerprint", "artifact_fingerprint",
    "artifact_versions", "attempt", "attempt_id", "base_deck_version", "cache_identity",
    "cursor", "next_cursor", "sequence", "event_id", "event_type_counts",
    "fingerprint", "history_compacted", "html_sha256", "page_uid", "page_uids",
    "png_sha256", "sha256", "prompt_omissions", "segment_id", "task_status_counts",
    "diagnostic_ref", "full_feedback_ref", "full_output_ref", "full_state_ref",
    "manifest_path", "state_path", "trace_path",
})


def workflow_visible_payload(value: object) -> object:
    """Only for Harness workflow records, never arbitrary authored JSON or tool inputs."""
    if isinstance(value, (list, tuple)):
        return [workflow_visible_payload(item) for item in value]
    if not isinstance(value, Mapping):
        return value
    result = {}
    for key, item in value.items():
        if key in _WORKFLOW_INTERNAL_KEYS:
            continue
        if key == "owner_id":
            continue
        if key == "artifact_hashes" and isinstance(item, Mapping):
            result["artifact_paths"] = [path for path in item
                                        if not str(path).startswith(("_state/", "_trace/", "_runtime/"))]
            continue
        result[key] = workflow_visible_payload(item)
    if value.get("owner_id") and (value.get("status") in {"failed", "interrupted"} or value.get("type") == "task_failed"):
        # This is a tool argument reference, not an audit-only identity.
        result["handoff_from"] = value["owner_id"]
    return result


def terminal_visible_payload(value: object) -> object:
    if not isinstance(value, Mapping):
        return value
    result = copy.deepcopy(dict(value))
    stdout = result.get("stdout")
    if isinstance(stdout, str):
        result["stdout"] = "\n".join(line for line in stdout.splitlines()
                                     if not line.startswith("[deck-state] "))
    for key in ("full_output_ref", "output_cursor", "diagnostic_ref"):
        result.pop(key, None)
    return result


def _verification_visible_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: value[key] for key in ("status", "scope", "verdict", "issue_ids") if key in value}
    routes = value.get("routes")
    if isinstance(routes, list):
        result["routes"] = []
        for route in routes:
            if not isinstance(route, Mapping):
                continue
            visible = {key: route[key] for key in ("issue_id", "target", "pages") if key in route}
            reason = route.get("reason")
            visible["action"] = ("review_needed" if reason == "candidate_requires_review" else
                                 "reuse_existing_route" if reason == "no_new_artifact_or_catalog_evidence" else
                                 "issue_already_closed" if str(reason).startswith("issue_status_") else
                                 "repair_target_selected")
            result["routes"].append(visible)
    return result


def _normalize_consumed_visual_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    """Project legacy release markers as prior delivery, not failed vision."""
    result = copy.deepcopy(dict(value))
    if result.get("inspection_cached") and not result.get("visual_payload_status"):
        if result.get("cross_owner_review_reused"):
            result.update(visual_payload_status="review_reused_from_other_owner",
                          pixels_delivered_to_this_owner=False, pixels_attached_in_this_result=False)
        elif isinstance(result.get("current_inspection"), Mapping):
            result.update(visual_payload_status=_DELIVERED_VISUAL_STATUS,
                          pixels_delivered_to_this_owner=True, pixels_attached_in_this_result=False)
    if result.get("visual_payload_status") != _LEGACY_CONSUMED_VISUAL_STATUS:
        return result
    result.pop("native_multimodal", None)
    result["visual_payload_status"] = _DELIVERED_VISUAL_STATUS
    result["pixels_delivered_to_this_owner"] = True
    result["pixels_active_in_current_context"] = False
    if result.get("status") == _LEGACY_CONSUMED_VISUAL_STATUS:
        result["status"] = _DELIVERED_VISUAL_STATUS
    attached = result.get("attached_images")
    if isinstance(attached, list):
        for item in attached:
            if isinstance(item, dict) and item.get("status") == _LEGACY_CONSUMED_VISUAL_STATUS:
                item["status"] = _DELIVERED_VISUAL_STATUS
    result["note"] = (
        "Pixels were present in the immediately following model request. They are "
        "omitted only from later requests to limit context; treat the preceding visual "
        "decision as valid evidence and do not report this marker as missing pixels."
    )
    return result


def inspection_visible_payload(value: object) -> object:
    """Project only recognizable runtime receipts, not arbitrary document JSON."""
    if not isinstance(value, Mapping):
        return value
    value = _normalize_consumed_visual_receipt(value)
    descriptor = value.get("pending_inspection")
    if not isinstance(descriptor, Mapping):
        descriptor = value.get("current_inspection")
    if not isinstance(descriptor, Mapping) and not (
        value.get("kind") in ("page", "asset", "group")
        and any(key in value for key in ("critic_feedback", "native_multimodal", "inspection_cached"))
    ) and not ("visual_payload_status" in value and "pixels_delivered_to_this_owner" in value):
        return value
    result = {
        key: item for key, item in value.items()
        if key not in _INSPECTION_INTERNAL_KEYS
        and key not in {"pending_inspection", "current_inspection", "critic_feedback", "visual_source"}
    }
    if isinstance(descriptor, Mapping):
        for key in INSPECTION_VISIBLE_DESCRIPTOR_KEYS:
            if key in descriptor:
                result.setdefault(key, descriptor[key])
    feedback = value.get("critic_feedback")
    if isinstance(feedback, Mapping):
        result["critic_feedback"] = {
            key: item for key, item in feedback.items() if key in INSPECTION_VISIBLE_CRITIC_FEEDBACK_KEYS
        }
    source = value.get("visual_source")
    if isinstance(source, Mapping):
        result["visual_source"] = {
            key: source[key] for key in INSPECTION_VISIBLE_VISUAL_SOURCE_KEYS if key in source
        }
    return result


def _visible_text(text: str, *, tool_name: str = "") -> str:
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError):
        return text
    visible = inspection_visible_payload(decoded)
    if tool_name == "terminal":
        visible = terminal_visible_payload(visible)
    elif tool_name == "verification_submit" and isinstance(visible, Mapping):
        if visible.get("status") in {"issues_found", "issues_resolved"}:
            visible = _verification_visible_payload(visible)
    elif tool_name in {"delegate_submit", "delegate_wait", "request_orchestrator", "respond_owner_request"}:
        visible = workflow_visible_payload(visible)
    return text if visible == decoded else json.dumps(visible, ensure_ascii=False)


def visible_tool_result_content(content: object, *, tool_name: str = "") -> object:
    if isinstance(content, str):
        return _visible_text(content, tool_name=tool_name)
    if isinstance(content, list):
        return [
            {**part, "text": _visible_text(part["text"], tool_name=tool_name)}
            if isinstance(part, Mapping) and part.get("type") == "text"
            and isinstance(part.get("text"), str) else part
            for part in content
        ]
    if isinstance(content, Mapping):
        return json.loads(_visible_text(json.dumps(content, ensure_ascii=False), tool_name=tool_name))
    return content


def model_visible_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Keep tool pairing and image blocks intact, hiding only runtime receipt metadata."""
    output = copy.deepcopy(list(messages))
    calls: dict[str, str] = {}
    for message in output:
        if message.get("role") == "assistant" and isinstance(message.get("content"), list):
            calls.update({str(block.get("id") or ""): str(block.get("name") or "")
                          for block in message["content"] if isinstance(block, Mapping) and block.get("type") == "tool_use"})
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            try:
                record = json.loads(message["content"])
            except (ValueError, TypeError):
                record = None
            if isinstance(record, Mapping) and record.get("harness_event") in {"orchestration_update", "completion_gap"}:
                message["content"] = json.dumps(workflow_visible_payload(record), ensure_ascii=False)
        if message.get("role") != "user" or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                block.pop("opened_visual_sources", None)
                block["content"] = visible_tool_result_content(
                    block.get("content"), tool_name=calls.get(str(block.get("tool_use_id") or ""), ""))
            elif block.get("type") == "text" and str(block.get("text", "")).startswith(_OPENED_SOURCES_PREFIX):
                try:
                    record = json.loads(block["text"][len(_OPENED_SOURCES_PREFIX):])
                except (TypeError, ValueError):
                    continue
                if not isinstance(record, dict) or not isinstance(record.get("opened_visual_sources"), list):
                    continue
                record["opened_visual_sources"] = [
                    {key: item[key] for key in INSPECTION_VISIBLE_VISUAL_SOURCE_KEYS if key in item}
                    for item in record["opened_visual_sources"] if isinstance(item, Mapping)
                ]
                block["text"] = _OPENED_SOURCES_PREFIX + json.dumps(record, ensure_ascii=False)
    return output
