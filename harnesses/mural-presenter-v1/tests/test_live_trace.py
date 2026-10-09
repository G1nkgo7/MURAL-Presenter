from __future__ import annotations

import json
from pathlib import Path
import threading

from runtime.live_trace import LiveEventJournal, read_live_events
from runtime.tools import ToolCall, ToolContext, ToolDefinition, ToolRuntime


def test_same_turn_diagnostics_keep_distinct_progress_but_deduplicate_replay(tmp_path):
    journal = LiveEventJournal(tmp_path / "_trace/live-events.jsonl")
    event = {"event_type": "agent_context_compacted", "agent_id": "orch", "role": "orchestrator",
             "segment_id": "segment", "turn": 2}
    for count in (1, 2, 1):
        journal.record_runtime_event({**event, "compaction_count": count})
    rows = read_live_events(journal.path)["events"]
    assert [row["payload"]["compaction_count"] for row in rows] == [1, 2]
    assert [row["sequence"] for row in rows] == [1, 2]


def test_recoverable_compaction_failure_is_not_dropped_from_live_trace(tmp_path):
    journal = LiveEventJournal(tmp_path / "_trace/live-events.jsonl")

    written = journal.record_runtime_event({
        "event_type": "agent_context_compaction_failed",
        "agent_id": "owner-slide",
        "role": "slide",
        "segment_id": "before-compact",
        "error_type": "CompactionSummaryError",
        "error_summary": "summary exceeded budget",
        "recoverable": True,
        "checkpoint_retained": True,
    })

    assert len(written) == 1
    event = read_live_events(journal.path)["events"][0]
    assert event["event_type"] == "agent_context_compaction_failed"
    assert event["payload"]["recoverable"] is True
    assert event["payload"]["checkpoint_retained"] is True


def test_no_action_observability_preserves_compact_audit_fields_without_large_content(tmp_path):
    journal = LiveEventJournal(tmp_path / "_trace/live-events.jsonl")
    written = journal.record_runtime_event({
        "event_type": "agent_no_action_response",
        "agent_id": "owner-research",
        "role": "research",
        "segment_id": "segment-7",
        "turn": 14,
        "reason": "output_budget_exhausted_no_action",
        "stop_reason": "max_tokens",
        "retry_allowed": True,
        "recovery_action": "retry_without_thinking_reduced_budget",
        "thinking": False,
        "max_tokens": 4096,
        "configuration_changed": True,
        "recovery_configuration": {"thinking": False, "effort": "", "max_tokens": 4096},
        "raw_response": "x" * 20000,
    })

    assert len(written) == 1
    event = read_live_events(journal.path)["events"][0]
    assert event["event_type"] == "agent_no_action_response"
    assert event["payload"] == {
        "reason": "output_budget_exhausted_no_action",
        "stop_reason": "max_tokens",
        "retry_allowed": True,
        "recovery_action": "retry_without_thinking_reduced_budget",
        "thinking": False,
        "max_tokens": 4096,
        "configuration_changed": True,
        "recovery_configuration": {"thinking": False, "effort": "", "max_tokens": 4096},
    }


def test_replay_projections_rebuild_only_from_journal_after_interruption(tmp_path):
    from infer import PresenterKernel
    from runtime.config import RunSpec
    from runtime.live_trace import iter_jsonl_events, iter_replay_events
    from runtime.session import replay_provider_events
    from runtime.tools import replay_tool_transactions

    kernel = PresenterKernel(RunSpec.from_inputs(raw_user_query="trace fixture", output_workspace=tmp_path,
        environment={"MODEL_API_FORMAT": "openai", "MODEL": "fixture", "MODEL_BASE_URL": "https://example.test"}))
    journal = kernel.live_trace
    common = {"agent_id": "owner-slide", "role": "slide", "segment_id": "segment", "turn": 1}
    try:
        journal.record_runtime_event({**common, "event_type": "agent_started"})
        journal.record_runtime_event({**common, "event_type": "agent_turn_completed"})
        journal.record_runtime_event({**common, "event_type": "provider_attempt_started", "attempt_id": "request"})
        journal.record_tool_event({**common, "event_type": "tool_use", "owner_id": "owner-slide",
            "tool_use_id": "tool", "tool_name": "inspect", "arguments": {"page": 1}})
        # SIGKILL 不会执行 finally：没有旧投影，完整 journal 仍可恢复。
        assert not list((tmp_path / "_trace").glob("provider-events.jsonl"))
        with journal.path.open("ab") as handle:
            handle.write(b'{"sequence":999')
        reopened = LiveEventJournal(journal.path)
        recovered = reopened.close_interrupted_events()
        assert [row["event_type"] for row in recovered] == ["provider_failed", "tool_result", "agent_failed"]
        assert reopened.close_interrupted_events() == ()
        assert replay_provider_events(iter_replay_events(journal.path, "provider")).failures == 1
        provider_path = tmp_path / "_trace/provider-events.jsonl"
        provider_path.write_text('{"event_type":"provider_attempt_started","attempt_id":"fake-stale-projection"}\n')
        saved = journal.path.read_bytes()
        kernel._write_replay_traces()
        first = {kind: (tmp_path / f"_trace/{kind}-events.jsonl").read_bytes() for kind in ("agent", "provider", "tool")}
        kernel._write_replay_traces()
        assert journal.path.read_bytes() == saved
        assert all((tmp_path / f"_trace/{kind}-events.jsonl").read_bytes() == value for kind, value in first.items())
        assert b"fake-stale-projection" not in provider_path.read_bytes()
        assert replay_provider_events(provider_path).failures == 1
        tool_replay = replay_tool_transactions(iter_jsonl_events(tmp_path / "_trace/tool-events.jsonl"))
        assert tool_replay.uses == tool_replay.results == 1 and not tool_replay.open_ids
        assert "agent_turn_completed" in first["agent"].decode()
        rows = read_live_events(journal.path)["events"]
        assert [row["sequence"] for row in rows] == list(range(1, len(rows) + 1))
        assert len({row["event_id"] for row in rows}) == len(rows)
    finally:
        kernel.orchestration.close(wait=True)


def _tool_definition(handler) -> ToolDefinition:
    return ToolDefinition(
        name="inspect",
        description="inspect",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=handler,
        roles=frozenset({"orchestrator", "slide"}),
    )


def test_tool_use_and_result_are_fsynced_while_task_is_running(
    tmp_path: Path,
) -> None:
    path = tmp_path / "_trace" / "live-events.jsonl"
    journal = LiveEventJournal(path)
    use_visible = threading.Event()
    release_handler = threading.Event()

    def sink(event):
        journal.record_tool_event(event)
        if event["event_type"] == "tool_use":
            use_visible.set()

    def handler(_context, arguments):
        assert arguments["path"] == "plan/deck.md"
        assert release_handler.wait(5)
        return {"status": "ready"}

    tools = ToolRuntime(event_sink=sink)
    tools.register(_tool_definition(handler))
    context = ToolContext(
        "orchestrator",
        "orchestrator",
        metadata={"turn": 2, "segment_id": "segment-live"},
    )
    result_holder = []
    worker = threading.Thread(
        target=lambda: result_holder.append(
            tools.execute(
                context,
                ToolCall("tool-live-1", "inspect", {"path": "plan/deck.md"}),
            )
        )
    )
    worker.start()
    assert use_visible.wait(5)

    during = read_live_events(path)
    assert worker.is_alive()
    assert [event["event_type"] for event in during["events"]] == ["tool_use"]
    assert during["events"][0]["payload"]["tool_use_id"] == "tool-live-1"

    release_handler.set()
    worker.join(5)
    assert not worker.is_alive()
    assert result_holder[0].ok
    after = read_live_events(path)
    assert [event["event_type"] for event in after["events"]] == [
        "tool_use",
        "tool_result",
    ]
    assert after["events"][1]["payload"]["duration_ms"] >= 0


def test_orchestrator_and_slide_events_keep_role_and_transaction_identity(
    tmp_path: Path,
) -> None:
    journal = LiveEventJournal(tmp_path / "_trace" / "live-events.jsonl")
    tools = ToolRuntime(event_sink=journal.record_tool_event)
    tools.register(_tool_definition(lambda _context, args: {"path": args["path"]}))

    journal.record_runtime_event(
        {
            "event_type": "agent_started",
            "agent_id": "orchestrator",
            "role": "orchestrator",
            "turn": 0,
            "segment_id": "segment-orch",
        }
    )
    tools.execute(
        ToolContext(
            "orchestrator",
            "orchestrator",
            metadata={"turn": 1, "segment_id": "segment-orch"},
        ),
        ToolCall("tool-orch", "inspect", {"path": "plan/deck.md"}),
    )
    journal.record_runtime_event(
        {
            "event_type": "agent_started",
            "agent_id": "owner-slide-e1",
            "role": "slide",
            "turn": 0,
            "segment_id": "segment-slide",
            "group_id": "deck-3pages",
            "pages": [1, 2, 3],
        }
    )
    tools.execute(
        ToolContext(
            "owner-slide-e1",
            "slide",
            metadata={"turn": 1, "segment_id": "segment-slide"},
        ),
        ToolCall("tool-slide", "inspect", {"path": "slides/slide_01.html"}),
    )

    events = read_live_events(journal.path)["events"]
    assert {event["role"] for event in events} == {"orchestrator", "slide"}
    by_id = {}
    for event in events:
        tool_use_id = event["payload"].get("tool_use_id")
        if tool_use_id:
            by_id.setdefault(tool_use_id, []).append(event["event_type"])
    assert by_id == {
        "tool-orch": ["tool_use", "tool_result"],
        "tool-slide": ["tool_use", "tool_result"],
    }


def test_resume_and_failover_continue_sequence_without_duplicates(
    tmp_path: Path,
) -> None:
    path = tmp_path / "_trace" / "live-events.jsonl"
    journal = LiveEventJournal(path)
    started = {
        "event_type": "provider_attempt_started",
        "attempt_id": "attempt-a",
        "agent_id": "orchestrator",
        "role": "orchestrator",
        "turn": 3,
        "segment_id": "segment-a",
        "channel_id": "ch-a",
    }
    failed = {
        "event_type": "provider_attempt_failed",
        "attempt_id": "attempt-a",
        "agent_id": "orchestrator",
        "role": "orchestrator",
        "turn": 3,
        "segment_id": "segment-a",
        "channel_id": "ch-a",
        "next_channel_id": "ch-b",
        "error_kind": "timeout",
        "segment_rollover_required": False,
    }
    journal.record_runtime_event(started)
    journal.record_runtime_event(failed)
    journal.record_runtime_event(started)
    journal.record_runtime_event(failed)

    resumed = LiveEventJournal(path)
    resumed.record_runtime_event(
        {
            "event_type": "provider_attempt_started",
            "attempt_id": "attempt-b",
            "previous_attempt_id": "attempt-a",
            "failover_from_channel_id": "ch-a",
            "agent_id": "orchestrator",
            "role": "orchestrator",
            "turn": 3,
            "segment_id": "segment-a",
            "channel_id": "ch-b",
        }
    )
    resumed.record_runtime_event(
        {
            "event_type": "provider_attempt_succeeded",
            "attempt_id": "attempt-b",
            "agent_id": "orchestrator",
            "role": "orchestrator",
            "turn": 3,
            "segment_id": "segment-a",
            "channel_id": "ch-b",
        }
    )

    events = read_live_events(path)["events"]
    assert [event["sequence"] for event in events] == list(range(1, 6))
    assert len({event["event_id"] for event in events}) == 5
    assert [event["event_type"] for event in events] == [
        "provider_started",
        "provider_failed",
        "provider_started",
        "provider_failover",
        "provider_succeeded",
    ]


def test_incomplete_tail_is_recoverable_and_trace_is_public_only(
    tmp_path: Path,
) -> None:
    path = tmp_path / "_trace" / "live-events.jsonl"
    journal = LiveEventJournal(path)
    journal.record_runtime_event(
        {
            "event_type": "assistant_message",
            "agent_id": "owner-slide",
            "role": "slide",
            "turn": 1,
            "segment_id": "segment-1",
            "message": (
                "已读取 Bearer very-secret-token-value；"
                'reasoning_signature = "embedded-signature-value"'
            ),
            "signature": "provider-thinking-signature",
            "reasoning_content": "hidden chain of thought",
            "api_key": "sk-live-secret-value",
        }
    )
    with path.open("ab") as handle:
        handle.write(b'{"schema":"mural.live-event.v1","sequence":')

    recovered = LiveEventJournal(path)
    recovered.record_tool_event(
        {
            "event_type": "tool_use",
            "tool_use_id": "tool-redacted",
            "tool_name": "inspect",
            "owner_id": "owner-slide",
            "role": "slide",
            "turn": 2,
            "segment_id": "segment-1",
            "arguments": {
                "path": "plan/deck.md",
                "authorization": "Bearer another-secret-value",
                "content": "full private document",
            },
        }
    )

    raw = path.read_text(encoding="utf-8")
    assert "very-secret-token-value" in raw
    assert "another-secret-value" in raw
    assert "sk-live-secret-value" not in raw
    assert "provider-thinking-signature" not in raw
    assert "embedded-signature-value" in raw
    assert "reasoning_signature" in raw
    assert "hidden chain of thought" not in raw
    events = read_live_events(path)["events"]
    assert [event["sequence"] for event in events] == [1, 2]
    assert events[1]["payload"]["arguments"]["content"].startswith("[omitted")


def test_cursor_returns_only_new_complete_events(tmp_path: Path) -> None:
    journal = LiveEventJournal(tmp_path / "_trace" / "live-events.jsonl")
    for turn in range(1, 4):
        journal.record_runtime_event(
            {
                "event_type": "assistant_message",
                "agent_id": "orchestrator",
                "role": "orchestrator",
                "turn": turn,
                "segment_id": "segment-a",
                "message": f"message-{turn}",
            }
        )
    page = read_live_events(journal.path, cursor=1, limit=1)
    assert [event["sequence"] for event in page["events"]] == [2]
    assert page["next_cursor"] == 2
    assert page["has_more"] is True
    final = read_live_events(journal.path, cursor=page["next_cursor"])
    assert [event["sequence"] for event in final["events"]] == [3]


def test_workspace_resume_closes_interrupted_agent_and_tool_once(
    tmp_path: Path,
) -> None:
    path = tmp_path / "_trace" / "live-events.jsonl"
    journal = LiveEventJournal(path)
    journal.record_runtime_event(
        {
            "event_type": "agent_started",
            "agent_id": "owner-slide",
            "role": "slide",
            "turn": 0,
            "segment_id": "segment-a",
        }
    )
    journal.record_tool_event(
        {
            "event_type": "tool_use",
            "tool_use_id": "tool-interrupted",
            "tool_name": "inspect",
            "owner_id": "owner-slide",
            "role": "slide",
            "turn": 1,
            "segment_id": "segment-a",
            "arguments": {"path": "renders/slide_01.png"},
        }
    )

    resumed = LiveEventJournal(path)
    closed = resumed.close_interrupted_events()
    assert [event["event_type"] for event in closed] == [
        "tool_result",
        "agent_failed",
    ]
    assert closed[0]["payload"]["error_code"] == "process_interrupted"
    assert closed[1]["turn"] == 1
    assert resumed.close_interrupted_events() == ()
    events = read_live_events(path)["events"]
    assert [event["sequence"] for event in events] == [1, 2, 3, 4]


def test_live_jsonl_is_valid_one_object_per_complete_line(tmp_path: Path) -> None:
    journal = LiveEventJournal(tmp_path / "_trace" / "live-events.jsonl")
    journal.record_runtime_event(
        {
            "event_type": "agent_completed",
            "agent_id": "orchestrator",
            "role": "orchestrator",
            "turn": 1,
            "segment_id": "segment-a",
        }
    )
    rows = [json.loads(line) for line in journal.path.read_text().splitlines()]
    assert rows[0]["schema"] == "mural.live-event.v1"


def test_resume_closes_interrupted_critic_and_pending_reuse_once(tmp_path):
    journal = LiveEventJournal(tmp_path / "_trace/live-events.jsonl")
    common = {"agent_id": "external-visual-critic", "role": "critic", "segment_id": "", "turn": 0}
    original = {"inspection_id": "inspection-1", "owner_id": "owner-1", "tool_use_id": "inspect-1",
        "page_uid": "page-1", "page": 1, "html_sha256": "h1", "png_sha256": "f" * 64,
        "scope_key": "scope-1", "critic_model": "same-model", "rubric_version": "current-rubric"}
    reused = {**original, "inspection_id": "inspection-2", "owner_id": "owner-2", "tool_use_id": "inspect-2",
        "page_uid": "page-2", "page": 2, "scope_key": "scope-2", "pending": True,
        "reused_from_inspection_id": original["inspection_id"]}
    journal.append(event_type="critic_started", payload=original, **common)
    journal.append(event_type="critic_reused", payload=reused, **common)
    for terminal in ("critic_completed", "critic_failed"):
        closed = {**original, "inspection_id": terminal, "tool_use_id": terminal}
        journal.append(event_type="critic_started", payload=closed, **common)
        journal.append(event_type=terminal, payload={**closed, "completed": terminal == "critic_completed"}, **common)
    journal.append(event_type="critic_completed", payload={**original, "owner_id": "foreign-owner",
        "tool_use_id": "foreign-tool", "completed": True}, **common)
    before = journal.path.read_bytes()
    resumed = LiveEventJournal(journal.path)
    closed = resumed.close_interrupted_events()
    assert [event["event_type"] for event in closed] == ["critic_failed", "critic_failed"]
    for event, source in zip(closed, (original, reused)):
        payload = event["payload"]
        assert all(payload[key] == source[key] for key in (
            "inspection_id", "owner_id", "tool_use_id", "page_uid", "page", "png_sha256", "html_sha256", "scope_key"))
        assert payload["completed"] is False and payload["pending"] is False
        assert payload["body"] == "" and "process_interrupted" in payload["incomplete_reason"]
        assert payload["closed_during_workspace_resume"] is True
    assert closed[0]["payload"]["cache_hit"] is False and closed[1]["payload"]["cache_hit"] is True
    assert closed[1]["payload"]["reused_from_inspection_id"] == original["inspection_id"]
    assert journal.path.read_bytes().startswith(before)
    after = journal.path.read_bytes()
    assert resumed.close_interrupted_events() == ()
    assert LiveEventJournal(journal.path).close_interrupted_events() == ()
    assert journal.path.read_bytes() == after
    later = {**original, "inspection_id": "inspection-3", "tool_use_id": "inspect-3", "png_sha256": "a" * 64}
    resumed.append(event_type="critic_started", payload=later, **common)
    closed = resumed.close_interrupted_events()
    assert len(closed) == 1 and closed[0]["payload"]["inspection_id"] == later["inspection_id"]
    assert resumed.close_interrupted_events() == ()
    rows = read_live_events(journal.path)["events"]
    assert len([row for row in rows if row["event_type"] == "critic_started"]) == 4
    assert not any(row["event_type"] in {"assistant_message", "provider_started"} for row in rows)
