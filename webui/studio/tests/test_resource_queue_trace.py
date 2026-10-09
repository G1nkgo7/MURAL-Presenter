"""本地等待可实时查看，但不伪装成模型消息或 Provider 故障。"""
import importlib
import json
from pathlib import Path
import sys

from app import trace
from workspace_fixture import write_workspace_manifest


def _harness_live_trace():
    harness_root = Path(__file__).resolve().parents[3] / "harnesses/mural-presenter-v1"
    sys.path.insert(0, str(harness_root))
    try:
        return importlib.import_module("runtime.live_trace")
    finally:
        sys.path.remove(str(harness_root))


def test_queue_trace_is_incremental_and_distinct_from_provider_failure(tmp_path):
    write_workspace_manifest(tmp_path)
    module = _harness_live_trace()
    journal = module.LiveEventJournal(tmp_path / "_trace/live-events.jsonl")
    base = {"agent_id": "orchestrator", "role": "orchestrator", "turn": 1,
            "segment_id": "same-segment", "attempt_id": "queue-a", "resource": "model"}
    journal.record_runtime_event({**base, "event_type": "resource_wait_started"})
    payload = trace.agent_trace_payload(str(tmp_path), "orchestrator")
    messages = json.loads(payload["content"])
    assert messages[0]["role"] == "system"
    assert "尚未请求 Provider" in messages[0]["content"][0]["text"]

    failed = {**base, "event_type": "resource_wait_failed", "queue_wait_s": 12,
              "error_kind": "resource_queue_timeout"}
    journal.record_runtime_event(failed)
    assert not journal.record_runtime_event(failed)
    payload = trace.agent_trace_payload(str(tmp_path), "orchestrator")
    messages = json.loads(payload["content"])
    assert len(messages) == 1
    assert messages[0]["role"] == "system"
    assert "未发送请求，也未切换 Provider" in messages[0]["content"][0]["text"]
    assert "12s" in messages[0]["content"][0]["text"]

    for kind in ("resource_wait_started", "resource_wait_completed"):
        journal.record_runtime_event({**base, "attempt_id": "queue-b", "event_type": kind,
                                      "queue_wait_s": 2})
    payload = trace.agent_trace_payload(str(tmp_path), "orchestrator")
    messages = json.loads(payload["content"])
    assert len(messages) == 1
    assert "排队超时" in messages[0]["content"][0]["text"]
    rows = [json.loads(line) for line in journal.path.read_text().splitlines()]
    assert [row["sequence"] for row in rows] == [1, 2, 3, 4]
    assert all(row["event_type"].startswith("resource_wait_") for row in rows)


def test_attachment_and_completion_events_keep_live_cursor_contiguous(tmp_path):
    write_workspace_manifest(tmp_path)
    module = _harness_live_trace()
    assert trace._LIVE_EVENT_TYPES == module.LIVE_EVENT_TYPES
    journal = module.LiveEventJournal(tmp_path / "_trace/live-events.jsonl")
    kinds = ["mineru_started", "mineru_failed", "mineru_failover", "mineru_succeeded", "deck_completion_checked"]
    for index, kind in enumerate(kinds):
        journal.append(event_type=kind, agent_id="orchestrator", role="orchestrator",
            payload={"actor": "harness", "status": "ready" if kind == "deck_completion_checked" else "running",
                     "api_key": "sk-do-not-expose-credential", "attempt": index})
    events, cursor = [], 0
    while True:
        page = trace.live_events(str(tmp_path), cursor=cursor, limit=2)
        events.extend(page["events"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert [event["seq"] for event in events] == [1, 2, 3, 4, 5]
    assert [event["event_type"] for event in events] == kinds
    assert all(event["k"] == "status" and "tool_use_id" not in event for event in events)
    assert "sk-do-not-expose-credential" not in json.dumps(events)
    assert "制作完成校验通过" in events[-1]["s"]


def test_successful_queue_adds_no_message_or_time():
    base = {"agent_id": "orchestrator", "role": "orchestrator", "turn": 1,
            "segment_id": "segment", "payload": {"attempt_id": "queue-a", "queue_wait_s": 0.024}}
    events = [{**base, "event_id": kind, "event_type": kind}
              for kind in ("resource_wait_started", "resource_wait_completed")]
    assert trace._live_events_messages(events) == ([], [])


def test_interrupted_queue_is_not_projected_as_still_waiting():
    base = {"agent_id": "owner-a", "role": "slide", "turn": 1,
            "segment_id": "interrupted-segment"}
    events = [
        {**base, "event_id": "wait", "event_type": "resource_wait_started",
         "payload": {"attempt_id": "queue-a"}},
        {**base, "event_id": "failure", "event_type": "agent_failed",
         "payload": {"error_type": "ProcessInterrupted"}},
    ]
    messages, _ = trace._live_events_messages(events)
    text = messages[0]["content"][0]["text"]
    assert "排队期间已中断" in text
    assert "未请求 Provider" in text
    assert "等待本地" not in text


def test_paused_owner_and_unstarted_dependencies_are_not_projected_as_failures(tmp_path):
    state = tmp_path / "_state"
    state.mkdir()
    tasks = {
        "source": {"task_id": "source", "owner_id": "research", "role": "research", "status": "interrupted",
                   "error": "ResourceSlotTimeout: local resource queue", "dependencies": []},
        "page": {"task_id": "page", "owner_id": "slide", "role": "slide", "status": "pending", "dependencies": ["source"]},
    }
    events = [
        {"sequence": 1, "agent_id": "research", "event_type": "agent_started", "payload": {"task_id": "source"}},
        {"sequence": 2, "agent_id": "research", "event_type": "agent_failed", "payload": {"error_type": "ResourceSlotTimeout"}},
    ]
    schedule = [{"sequence": 1, "type": "dependency_interrupted", "task_id": "page", "interrupted_dependencies": ["source"]}]
    path = state / "orchestration.json"
    path.write_text(json.dumps({"tasks": tasks, "events": schedule}))
    owners = {"research": {"key": "research"}, "slide": {"key": "slide"}}
    projected = trace._shared_v1_task_states(str(tmp_path), owners, events)
    assert projected["research"]["status"] == "interrupted"
    assert "不是 Provider" in projected["research"]["status_detail"]
    assert projected["slide"]["status"] == "waiting"
    assert not projected["slide"]["trace_available"]
    assert "依赖已暂停" in projected["slide"]["status_detail"]
    tasks["source"]["status"] = "running"
    path.write_text(json.dumps({"tasks": tasks, "events": schedule}))
    resumed_events = events + [{"sequence": 3, "agent_id": "research", "event_type": "agent_started", "payload": {"task_id": "source"}}]
    resumed = trace._shared_v1_task_states(str(tmp_path), owners, resumed_events)
    assert resumed["research"]["status"] == "running"
    assert "已暂停" not in resumed["slide"]["status_detail"]
