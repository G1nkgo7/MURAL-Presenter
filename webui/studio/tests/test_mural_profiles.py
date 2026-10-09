import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace
from unittest import mock

import pytest
from workspace_fixture import write_workspace_manifest

from app import engine
from app import environment_checks
from app import jobs
from app import trace, service_config


def test_livefeed_prefers_structured_slide_group_identity(tmp_path):
    role_dir = tmp_path / "_trace" / "subagents" / "slide_group_deck-uhi"
    role_dir.mkdir(parents=True)
    (role_dir / "config.json").write_text(
        json.dumps({
            "label": "slide_group_deck-uhi",
            "role": "slide",
            "owner_id": "134:deck-uhi:e1",
            "owner_group_id": "deck-uhi",
            "owner_scope_kind": "production_group",
            "task": "[Image fallback evidence] Image 已结束。",
        }),
        encoding="utf-8",
    )

    assert trace._trace_agent_aliases(str(tmp_path)) == {
        "slide_group_deck-uhi": "slide_group_deck-uhi"
    }


def test_registry_page_agents_normalizes_group_separator(tmp_path):
    write_workspace_manifest(tmp_path)
    (tmp_path / '_state/owners.json').write_text(json.dumps({
        'schema': 'mural.owner-registry.v2', 'owners': {
            'deck_uhi': {'owner_id': 'owner', 'group_id': 'deck_uhi', 'role': 'slide', 'pages': [1, 2]}}}))
    assert trace._registry_page_agents(str(tmp_path)) == {'1': 'slide_group_deck-uhi', '2': 'slide_group_deck-uhi'}


def test_workflow_payload_exposes_group_creation_and_owner_reuse(tmp_path):
    write_workspace_manifest(tmp_path)
    state = tmp_path / "_state"
    state.mkdir(exist_ok=True)
    page_uids = ["pg-a", "pg-b", "pg-c"]
    (state / "page-index.json").write_text(
        json.dumps({
            "pages": [
                {"page_uid": uid, "position": index}
                for index, uid in enumerate(page_uids, 1)
            ]
        }),
        encoding="utf-8",
    )
    trace_root = tmp_path / "_trace"
    for name, segment, action, assigned in (
        ("slide_group_deck-uhi", 0, "create", page_uids),
        ("slide_group_deck-uhi_verify2", 1, "reuse_owner", page_uids[1:]),
    ):
        role_dir = trace_root / "subagents" / name
        role_dir.mkdir(parents=True)
        (role_dir / "config.json").write_text(
            json.dumps({
                "label": "slide_group_deck-uhi",
                "role": "slide",
                "owner_id": "134:deck-uhi:e1",
                "owner_group_id": "deck-uhi",
                "owner_scope_kind": "production_group",
                "owner_action": action,
                "segment_id": segment,
                "resume_of_segment_id": segment - 1 if segment else None,
                "assigned_page_uids": assigned,
                "started_at": f"2026-08-29 11:{segment:02d}:00",
                "finished_at": f"2026-08-29 11:{segment:02d}:30",
            }),
            encoding="utf-8",
        )
    (trace_root / "runtime-summary.json").write_text(
        json.dumps({
            "issues": {"records": [{
                "issue_id": "issue-1",
                "issue_type": "layout",
                "severity": "major",
                "status": "verified",
                "page_uids": page_uids[1:],
                "transition_count": 4,
            }]}
        }),
        encoding="utf-8",
    )
    (tmp_path / "deck.md").write_text("# Deck", encoding="utf-8")
    slides = tmp_path / "slides"
    slides.mkdir()
    (slides / "slide_01.html").write_text("<main></main>", encoding="utf-8")
    renders = tmp_path / "renders"
    renders.mkdir()
    (renders / "slide_01.png").write_bytes(b"png")
    (tmp_path / "present.html").write_text("<main></main>", encoding="utf-8")
    aliases = trace._trace_agent_aliases(str(tmp_path))
    page_agents = {str(page): "slide_group_deck-uhi" for page in range(1, 4)}

    payload = trace._workflow_payload(
        str(tmp_path), page_agents, aliases,
        {"slide_group_deck-uhi": {"status": "complete"}},
        "completed",
    )

    slide_runs = [run for run in payload["runs"] if run["role"] == "slide"]
    assert [run["owner_action"] for run in slide_runs] == ["create", "reuse_owner"]
    assert slide_runs[0]["pages"] == [1, 2, 3]
    assert slide_runs[1]["pages"] == [2, 3]
    assert payload["issues"][0]["pages"] == [2, 3]
    assert payload["artifacts"]["locked"] is False
    assert payload["artifacts"]["file_count"] == 4
    assert [group["id"] for group in payload["artifacts"]["groups"]] == [
        "plan", "slides", "renders", "delivery",
    ]


def test_agent_trace_payload_groups_owner_segments_without_rewriting_fields(tmp_path):
    _write_shared_v1_workspace(tmp_path)
    for sequence, segment in ((1, "first"), (3, "second")):
        _append_live_event(tmp_path, sequence, "agent_context", agent_id="owner-group-evidence",
            role="slide", segment_id=segment, payload={"system": "Actual system",
            "api_key": "sk-example-secret-value", "messages": [{"role": "user", "content": "start"}],
            "tools": [], "group_id": "evidence", "attempt": 1})
        _append_live_event(tmp_path, sequence + 1, "assistant_message", agent_id="owner-group-evidence",
            role="slide", segment_id=segment, payload={"message": segment})
    by_agent, _, _ = trace._live_events_by_agent(str(tmp_path))
    key = next(key for key, rows in by_agent.items() if rows[0]["agent_id"] == "owner-group-evidence")
    payload = trace.agent_trace_payload(str(tmp_path), key, source="config")
    assert len(payload["segments"]) == 2
    assert payload["selected_segment"] == "second"
    assert payload["source"] == "config"
    assert "sk-example-secret-value" in payload["content"]
    assert "[REDACTED]" not in payload["content"]
    first = trace.agent_trace_payload(str(tmp_path), key, source="messages", segment="first")
    assert "first" in first["content"] and "second" not in first["content"]


def test_agent_trace_complete_scope_spans_compaction_and_resume_segments(tmp_path):
    _write_shared_v1_workspace(tmp_path)
    common = {"agent_id": "owner-complete-trace", "role": "slide"}
    _append_live_event(tmp_path, 1, "agent_context", **common, segment_id="before", payload={
        "system": "Actual system", "messages": [{"role": "user", "content": "original task"}],
        "tools": [], "group_id": "complete-trace",
    })
    _append_live_event(tmp_path, 2, "assistant_message", **common, segment_id="before",
        payload={"message": "before compaction"})
    _append_live_event(tmp_path, 3, "provider_started", **common, segment_id="before", payload={
        "request_kind": "context_compaction", "model": "teacher", "actual_max_tokens": 16384,
        "attempt_id": "compact-1",
    })
    _append_live_event(tmp_path, 4, "agent_context_compacted", **common, segment_id="after", payload={
        "estimated_tokens": 112000, "tokens_before": 112000, "tokens_after": 24000,
        "input_budget": 106496, "target_met": True,
    })
    _append_live_event(tmp_path, 5, "agent_context", **common, segment_id="after", payload={
        "system": "Actual system", "messages": [{"role": "user", "content": "compacted summary"}],
        "tools": [], "group_id": "complete-trace",
    })
    _append_live_event(tmp_path, 6, "assistant_message", **common, segment_id="after",
        payload={"message": "after compaction"})
    by_agent, _, _ = trace._live_events_by_agent(str(tmp_path))
    key = next(key for key, rows in by_agent.items() if rows[0]["agent_id"] == common["agent_id"])

    complete = trace.agent_trace_payload(str(tmp_path), key, segment="__all__")
    records = json.loads(complete["content"])
    blocks = [block for record in records
              for block in (record.get("content") if isinstance(record.get("content"), list) else [])]

    assert complete["selected_segment"] == "__all__"
    assert complete["trace_scope"] == "all_segments"
    assert [segment["id"] for segment in complete["segments"]] == ["before", "after"]
    assert "original task" in complete["content"]
    assert "before compaction" in complete["content"] and "after compaction" in complete["content"]
    assert "compacted summary" not in complete["content"]  # later cumulative context is not replayed twice
    compactions = [block for block in blocks if block.get("type") == "context_compaction"]
    assert [block["phase"] for block in compactions] == ["started", "completed"]
    assert compactions[-1]["tokens_before"] == 112000 and compactions[-1]["tokens_after"] == 24000

    raw = trace.agent_trace_payload(str(tmp_path), key, segment="__all__", source="events")
    assert [event["event_type"] for event in json.loads(raw["content"])] == [
        "agent_context", "assistant_message", "provider_started", "agent_context_compacted",
        "agent_context", "assistant_message",
    ]
    latest = trace.agent_trace_payload(str(tmp_path), key)
    assert latest["selected_segment"] == "after"
    assert "after compaction" in latest["content"] and "before compaction" not in latest["content"]



def test_progress_snapshot_exposes_mural_runtime_projection(tmp_path):
    write_workspace_manifest(tmp_path)
    runtime = {
        "schema": "mural.runtime-summary.v1",
        "deck_timeline": {"deck_version": 2},
        "tasks": [{"task_id": "page", "status": "running"}],
        "state_counts": {"active": 1, "waiting": 2},
        "issues": {"count": 1},
        "artifacts": {"verified_checkpoint_count": 3},
        "provider_queue_observability": "unavailable",
    }
    target = tmp_path / "_trace/runtime-summary.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(runtime), encoding="utf-8")
    (target.parent / "independent-visual-qc.json").write_text(
        json.dumps({
            "latest_deck_qc": {
                "qc_status": "pass",
                "candidate_positions": [],
            }
        }),
        encoding="utf-8",
    )

    progress = trace.snapshot(str(tmp_path), status="running")
    assert progress["mural_runtime"]["schema"] == "mural.runtime-summary.v1"
    assert progress["mural_runtime"]["tasks"][0]["task_id"] == "page"
    assert progress["mural_runtime"]["state_counts"] == {"active": 1, "waiting": 2}
    assert progress["mural_runtime"]["provider_queue_observability"] == "unavailable"
    assert progress["mural_runtime"]["visual_qc"]["qc_status"] == "pass"
    assert progress["qc_status"] == "pass"


def _write_shared_v1_workspace(root, *, provider_error_kind=""):
    write_workspace_manifest(root)
    state = root / "_state"
    trace_root = root / "_trace"
    state.mkdir(parents=True, exist_ok=True)
    trace_root.mkdir(parents=True)
    task_id = "task-shared-v1"
    (state / "orchestration.json").write_text(
        json.dumps({
            "schema": "mural.orchestration.v2",
            "events": [],
            "tasks": {
                task_id: {
                    "task_id": task_id,
                    "owner_id": "owner-slide-e1",
                    "role": "slide",
                    "status": "running",
                }
            },
        }),
        encoding="utf-8",
    )
    (state / "owners.json").write_text(
        json.dumps({
            "schema": "mural.owner-registry.v2",
            "owners": {
                "deck": {
                    "owner_id": "owner-slide-e1",
                    "role": "slide",
                    "status": "running",
                }
            },
        }),
        encoding="utf-8",
    )
    (state / "skill-runtime.json").write_text(
        json.dumps({"schema": "mural.skill-runtime.v1", "status": "running"}),
        encoding="utf-8",
    )
    (trace_root / "live-events.jsonl").touch()
    if provider_error_kind:
        _append_live_event(root, 1, "provider_started", payload={"attempt_id": "attempt-1"})
        _append_live_event(root, 2, "provider_failed",
                           payload={"attempt_id": "attempt-1", "error_kind": provider_error_kind})


def test_progress_snapshot_projects_shared_v1_owner_and_task_state(tmp_path):
    _write_shared_v1_workspace(tmp_path)

    progress = trace.snapshot(str(tmp_path), status="running")

    assert progress["workers"] == 1
    assert progress["task_stage"] == "rendering"
    assert progress["mural_runtime"]["schema"] == "mural.webui-runtime-projection.v1"
    assert progress["mural_runtime"]["tasks"][0]["role"] == "slide"


def _append_live_event(
    root,
    sequence,
    event_type,
    *,
    agent_id="orchestrator",
    role="orchestrator",
    payload=None,
    turn=1,
    segment_id="segment-live",
):
    target = root / "_trace" / "live-events.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    if event_type == "agent_started" and agent_id == "owner-slide-e1":
        payload = {"task_id": "task-shared-v1", **(payload or {})}
    event = {
        "schema": "mural.live-event.v1",
        "sequence": sequence,
        "timestamp": f"2026-09-03T00:00:{sequence:02d}Z",
        "event_id": f"event-{sequence}",
        "agent_id": agent_id,
        "role": role,
        "turn": turn,
        "segment_id": segment_id,
        "event_type": event_type,
        "payload": payload or {},
    }
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


@pytest.mark.parametrize("task_status,dependency_status,expected", [
    ("pending", None, "not_started"),
    ("running", None, "not_started"),
    ("pending", "running", "waiting_dependencies"),
    ("pending", "completed", "not_started"),
    ("failed", "failed", "dependency_failed"),
    ("failed", "cancelled", "dependency_failed"),
    ("failed", None, "start_failed"),
    ("cancelled", None, "cancelled"),
])
def test_known_owner_without_events_has_read_only_trace_empty_state(tmp_path, task_status, dependency_status, expected):
    _write_shared_v1_workspace(tmp_path)
    state_path = tmp_path / "_state/orchestration.json"
    state = json.loads(state_path.read_text())
    task = state["tasks"]["task-shared-v1"]
    task.update(status=task_status, attempt=1, error="StartupError: api_key=opaque-secret thinking_signature=private-signature")
    if dependency_status:
        task["dependencies"] = ["upstream"]
        state["tasks"]["upstream"] = {"task_id": "upstream", "owner_id": "image-owner", "role": "image", "group_id": "pictures", "status": dependency_status}
    state_path.write_text(json.dumps(state))
    write_workspace_manifest(tmp_path)
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    payload = trace.agent_trace_payload(str(tmp_path), "slide_group_deck")
    timing = trace.live_events(str(tmp_path))["agent_timings"]["slide_group_deck"]
    assert payload["empty_state"]["kind"] == timing["execution_state"] == expected
    assert payload["segments"] == [] and payload["selected_segment"] == ""
    assert json.loads(payload["content"]) == []
    assert not payload["task_state"]["trace_available"]
    assert timing["started_at"] is None and timing["finished_at"] is None
    assert "load_error" not in payload
    assert "opaque-secret" not in json.dumps(payload) and "private-signature" not in json.dumps(payload)
    if expected == "dependency_failed":
        assert "Image（pictures）" in payload["empty_state"]["message"]
    with pytest.raises(FileNotFoundError):
        trace.agent_trace_payload(str(tmp_path), "unknown-owner")
    assert before == {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}


def test_dependency_failure_is_visible_for_both_unstarted_groups(tmp_path):
    _write_shared_v1_workspace(tmp_path)
    state_path = tmp_path / "_state/orchestration.json"
    owners_path = tmp_path / "_state/owners.json"
    state = json.loads(state_path.read_text())
    owners = json.loads(owners_path.read_text())
    state["tasks"] = {"image-first": {"task_id": "image-first", "owner_id": "image-owner", "role": "image", "status": "failed", "group_id": "pictures"}}
    owners["owners"] = {}
    for group, pages in (("bookends", [1, 3]), ("page-02", [2])):
        owner_id = f"owner-{group}"
        owners["owners"][group] = {"owner_id": owner_id, "group_id": group, "role": "slide", "status": "failed", "pages": pages}
        state["tasks"][group] = {"task_id": group, "owner_id": owner_id, "role": "slide", "status": "skipped", "attempt": 1, "dependencies": ["image-first"]}
    owners_path.write_text(json.dumps(owners))
    state_path.write_text(json.dumps(state))
    _append_live_event(tmp_path, 1, "agent_started")
    before = (tmp_path / "_trace/live-events.jsonl").read_bytes()
    feed = trace.livefeed("", run_dir=str(tmp_path), deck_status="running")
    for group in ("bookends", "page-02"):
        key = f"slide_group_{group}"
        assert feed["agent_timings"][key]["execution_state"] == "dependency_failed"
        assert key not in feed["agents"]
        assert trace.agent_trace_payload(str(tmp_path), key)["content"] == "[]"
    assert (tmp_path / "_trace/live-events.jsonl").read_bytes() == before


def test_retried_owner_keeps_history_but_waits_for_its_own_start(tmp_path):
    _write_shared_v1_workspace(tmp_path)
    state_path = tmp_path / "_state/orchestration.json"
    state = json.loads(state_path.read_text())
    state["tasks"]["task-shared-v1"].update(status="failed", attempt=1)
    state_path.write_text(json.dumps(state))
    for seq, kind, payload in (
        (1, "agent_started", {"task_id": "task-shared-v1"}),
        (2, "assistant_message", {"message": "第一轮已保存的消息"}),
        (3, "agent_failed", {"error_type": "RuntimeError"}),
    ):
        _append_live_event(tmp_path, seq, kind, agent_id="owner-slide-e1", role="slide", segment_id="old-segment", payload=payload)
    assert trace.live_events(str(tmp_path))["agent_timings"]["slide_group_deck"]["execution_state"] == "failed"
    state["tasks"]["retry"] = {**state["tasks"]["task-shared-v1"], "task_id": "retry", "attempt": 2, "status": "running"}
    state_path.write_text(json.dumps(state))
    pending = trace.agent_trace_payload(str(tmp_path), "slide_group_deck")
    assert pending["empty_state"]["kind"] == "not_started"
    assert pending["selected_segment"] == "" and pending["content"] == "[]"
    assert [row["id"] for row in pending["segments"]] == ["old-segment"]
    history = trace.agent_trace_payload(str(tmp_path), "slide_group_deck", segment="old-segment")
    assert "第一轮已保存的消息" in history["content"] and "empty_state" not in history
    timing = trace.live_events(str(tmp_path))["agent_timings"]["slide_group_deck"]
    assert timing["execution_state"] == "not_started" and timing["status"] == "waiting"
    assert timing["started_at"] is None and timing["finished_at"] is None

    _append_live_event(tmp_path, 4, "agent_started", agent_id="owner-slide-e1", role="slide", segment_id="new-segment", payload={"task_id": "retry"})
    no_messages = trace.agent_trace_payload(str(tmp_path), "slide_group_deck")
    assert no_messages["empty_state"]["kind"] == "no_messages"
    _append_live_event(tmp_path, 5, "assistant_message", agent_id="owner-slide-e1", role="slide", segment_id="new-segment", payload={"message": "重试后的新消息"})
    current = trace.agent_trace_payload(str(tmp_path), "slide_group_deck")
    assert "empty_state" not in current and current["selected_segment"] == "new-segment"
    assert "重试后的新消息" in current["content"] and "第一轮已保存的消息" not in current["content"]
    timing = trace.live_events(str(tmp_path))["agent_timings"]["slide_group_deck"]
    assert timing["execution_state"] == "running" and timing["finished_at"] is None
    _append_live_event(tmp_path, 6, "agent_completed", agent_id="owner-slide-e1", role="slide", segment_id="new-segment")
    assert trace.live_events(str(tmp_path))["agent_timings"]["slide_group_deck"]["execution_state"] == "complete"


def test_started_owner_retry_after_startup_failure_uses_task_id(tmp_path):
    _write_shared_v1_workspace(tmp_path)
    state_path = tmp_path / "_state/orchestration.json"
    state = json.loads(state_path.read_text())
    state["tasks"]["task-shared-v1"].update(status="failed", attempt=1)
    state["tasks"]["retry"] = {**state["tasks"]["task-shared-v1"], "task_id": "retry", "attempt": 2, "status": "running"}
    state["events"] = [
        {"type": "task_started", "owner_id": "owner-slide-e1", "task_id": task_id, "sequence": n}
        for n, task_id in enumerate(("task-shared-v1", "retry"), 1)
    ]
    state_path.write_text(json.dumps(state))
    _append_live_event(tmp_path, 1, "agent_started", agent_id="owner-slide-e1", role="slide", payload={"task_id": "retry"})
    timing = trace.live_events(str(tmp_path))["agent_timings"]["slide_group_deck"]
    assert timing["execution_state"] == "running" and timing["trace_available"]


def test_resumed_task_uses_latest_real_start_and_preserves_history(tmp_path):
    _write_shared_v1_workspace(tmp_path)
    for sequence, kind, segment_id in (
        (1, "agent_started", "before-resume"),
        (2, "agent_failed", "before-resume"),
        (3, "agent_started", "after-resume"),
    ):
        _append_live_event(tmp_path, sequence, kind, agent_id="owner-slide-e1", role="slide", segment_id=segment_id)
    timing = trace.live_events(str(tmp_path))["agent_timings"]["slide_group_deck"]
    assert timing["execution_state"] == "running"
    assert timing["started_at"] == "2026-09-03T00:00:03Z" and timing["finished_at"] is None
    payload = trace.agent_trace_payload(str(tmp_path), "slide_group_deck")
    assert payload["selected_segment"] == "after-resume"
    assert [item["id"] for item in payload["segments"]] == ["before-resume", "after-resume"]


def test_workflow_browser_shows_empty_state_then_real_messages(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    _write_shared_v1_workspace(tmp_path)
    _append_live_event(tmp_path, 1, "agent_started")
    state_path = tmp_path / "_state/orchestration.json"
    state = json.loads(state_path.read_text())
    state["tasks"]["task-shared-v1"].update(status="failed", attempt=1, dependencies=["image-first"])
    state["tasks"]["image-first"] = {"task_id": "image-first", "owner_id": "image-owner", "role": "image", "group_id": "pictures <img src=x onerror=alert(1)>", "status": "failed"}
    state_path.write_text(json.dumps(state))
    studio = Path(__file__).resolve().parents[1]
    source = (studio / "static/app.js").read_text()
    names = (
        "escapeHtml", "workflowRunStatus", "workflowStatusLabel", "workflowNodeMarkup",
        "workflowAgentTraceUrl", "renderWorkflowTraceMessages", "renderWorkflowAgentTrace", "renderWorkflowImageOutputs",
        "renderWorkflowTraceContent", "workflowTraceByteLabel", "workflowTraceSegmentLabel",
        "workflowTraceMessageRecords", "workflowTraceConversationRows", "workflowTraceContentBlocks", "workflowTraceMessageKind",
        "workflowTraceMessageLabel", "workflowTraceMessageMeta", "workflowTraceBlockMarkup", "livePreviewLabel",
        "workflowTraceClip", "workflowTraceResultParts", "workflowTraceSafeJson", "workflowTraceToolSummary",
        "workflowToolOutputMarkup", "agentMarkdownHtml", "loadWorkflowAgentTrace", "cancelWorkflowAgentTraceRequest",
        "workflowTraceFailureMarkup",
        "refreshWorkflowToolOutputs", "workflowToolOutputState", "loadWorkflowToolOutput", "workflowToolOutputBody",
        "compactTraceValue", "liveReadFileSummary", "liveTerminalLabel", "liveToolTransactionMarkup", "renderWorkflowProviderAttempts",
    )
    functions = []
    for name in names:
        match = re.search(r"^(?:async )?function " + name + r"\(", source, re.M)
        end = re.search(r"^(?:async )?function ", source[match.start() + 1:], re.M)
        functions.append(source[match.start():match.start() + 1 + end.start()] if end else source[match.start():])
    setup = """
const $ = s => document.querySelector(s), $$ = s => [...document.querySelectorAll(s)];
const ed = {id:217, status:'running', workflowTraceAgent:'slide_group_deck', workflowTraceRequest:0, rendered:new Set()};
const trajectoryMode = false, WORKFLOW_TRACE_REFRESH_MS = 0, WORKFLOW_TRACE_REQUEST_TIMEOUT_MS = 1000;
const TOOL_LABEL = {patch:'调整页面细节', inspect:'检查画面质量'};
const isStaticActiveStatus = s => s === 'running', pad2 = n => String(n).padStart(2,'0');
const conversationTime = () => null, workflowTraceMessageTime = () => '', workflowTraceHighlightMatches = () => 0;
const workflowArtifactLinkMarkup = () => '';
window.drawNode = () => { $('#node').innerHTML = workflowNodeMarkup({id:'owner', agentKey:'slide_group_deck', role:'slide', status:workflowRunStatus('slide_group_deck'), x:10,y:10,kicker:'Group owner',title:'页面组',detail:'制作页面',pages:[]}); };
"""
    browser_env = engine.render_env({**os.environ, "PPTAGENT_ENGINE_PYTHON": str(studio.parent / "inference/.venv/bin/python")})
    executable = browser_env.get("PPT_SKILL_BROWSER_EXE")
    if not executable:
        pytest.skip("deployed Chromium is not available")
    browser_env["TMPDIR"] = str(tmp_path)
    def response():
        return trace.agent_trace_payload(str(tmp_path), "slide_group_deck")
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=executable, env=browser_env, headless=True, args=["--no-sandbox"])
        try:
            page = browser.new_page(viewport={"width": 1080, "height": 680})
            page.route("**/api/decks/217/agent-trace/**", lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(response())))
            page.route("http://workflow.test/", lambda route: route.fulfill(content_type="text/html; charset=utf-8", body='<h2>Workflow 状态与 Trace · 模拟事件验收</h2><div id="node" style="height:230px;position:relative"></div><h3 id="workflow-trace-title"></h3><span id="workflow-trace-meta"></span><select id="workflow-trace-segment"></select><input id="workflow-trace-search"><button id="workflow-trace-copy">复制</button><div id="workflow-trace-messages"></div><small id="workflow-trace-footer-note"></small>'))
            page.goto("http://workflow.test/")
            page.add_style_tag(path=str(studio / "static/app.css"))
            page.add_script_tag(content="\n".join(functions) + setup)
            page.evaluate("value => {ed.agentTimings=value; drawNode();}", trace.live_events(str(tmp_path))["agent_timings"])
            page.evaluate("loadWorkflowAgentTrace({force:true})")
            assert page.locator(".workflow-node.dependency_failed").count() == 1
            assert "依赖失败 · 未启动" in page.locator("#node").inner_text()
            assert "Image（pictures" in page.locator("#workflow-trace-messages").inner_text(), page.evaluate("ed.workflowTracePayload")
            assert page.locator(".task-state").count() == 1 and page.locator(".workflow-trace-message").count() == 0
            assert page.locator("img").count() == 0
            assert page.locator("#workflow-trace-search").is_disabled()
            page.screenshot(path=str(tmp_path / "workflow-dependency-failed.png"), full_page=True, animations="disabled")

            state["tasks"]["retry"] = {**state["tasks"]["task-shared-v1"], "task_id": "retry", "attempt": 2, "status": "running", "dependencies": []}
            state_path.write_text(json.dumps(state))
            page.evaluate("loadWorkflowAgentTrace({force:true})")
            assert "尚未启动" in page.locator("#workflow-trace-messages").inner_text()
            _append_live_event(tmp_path, 2, "agent_started", agent_id="owner-slide-e1", role="slide", payload={"task_id": "retry"}, segment_id="retry-segment")
            _append_live_event(tmp_path, 3, "assistant_message", agent_id="owner-slide-e1", role="slide", payload={"message": "已启动，正在制作页面。"}, segment_id="retry-segment")
            _append_live_event(tmp_path, 4, "tool_use", agent_id="owner-slide-e1", role="slide", payload={
                "tool_use_id": "patch-visible", "name": "patch",
                "arguments": {"path": "slides/slide_01.html", "repair_reason": "修正标题间距"},
            }, segment_id="retry-segment")
            _append_live_event(tmp_path, 5, "tool_result", agent_id="owner-slide-e1", role="slide", payload={
                "tool_use_id": "patch-visible", "name": "patch", "ok": True,
                "result": {"logical_path": "slides/slide_01.html", "replacements": 1, "sha256": "patch-sha"},
            }, segment_id="retry-segment")
            _append_live_event(tmp_path, 6, "tool_use", agent_id="owner-slide-e1", role="slide", payload={
                "tool_use_id": "inspect-visible", "name": "inspect", "arguments": {"page": 1},
            }, segment_id="retry-segment")
            _append_live_event(tmp_path, 7, "tool_result", agent_id="owner-slide-e1", role="slide", payload={
                "tool_use_id": "inspect-visible", "name": "inspect", "ok": True,
                "result": {"inspection_id": "inspection-visible", "html_sha256": "html-sha", "png_sha256": "png-sha"},
            }, segment_id="retry-segment")
            page.evaluate("value => {ed.agentTimings=value; drawNode();}", trace.live_events(str(tmp_path))["agent_timings"])
            page.evaluate("loadWorkflowAgentTrace({force:true})")
            assert page.locator(".task-state").count() == 0
            assert page.locator(".workflow-trace-message").count() == 1
            assert not page.locator("#workflow-trace-search").is_disabled()
            assert page.locator(".workflow-node.running").count() == 1
            assert "已启动，正在制作页面。" in page.locator("#workflow-trace-messages").inner_text()
            results = page.locator("#workflow-trace-messages .live-transaction")
            assert results.count() == 2
            results.nth(0).locator("summary").click()
            assert '"replacements": 1' in results.nth(0).locator("pre").last.inner_text()
            results.nth(1).locator("summary").click()
            assert '"png_sha256": "png-sha"' in results.nth(1).locator("pre").last.inner_text()
            page.evaluate("""() => {
              const host = document.createElement('div');
              host.id = 'transaction-test';
              host.innerHTML = liveToolTransactionMarkup({
                id:'inspect-live', tool:'inspect', agent:'slide_group_deck',
                use:{args:{page:1, question:'检查当前页面'}},
                result:{ok:true, duration_ms:321, result:{inspection_id:'inspection-live', png_sha256:'live-png'}}
              });
              document.body.appendChild(host);
            }""")
            transaction = page.locator("#transaction-test .live-transaction")
            transaction.locator("summary").click()
            assert '"inspection_id": "inspection-live"' in transaction.locator(".live-transaction-detail").inner_text()
            assert "segment=" not in page.evaluate("workflowAgentTraceUrl(ed.workflowTraceAgent)")
            assert "segment=retry-segment" in page.evaluate("ed.workflowTraceSegmentPinned=true; workflowAgentTraceUrl(ed.workflowTraceAgent)")
            page.screenshot(path=str(tmp_path / "workflow-started.png"), full_page=True, animations="disabled")
        finally:
            browser.close()


def test_process_timeline_splits_repair_into_a_later_owner_segment():
    source = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text()
    start = source.index("function slideOwnerSegments(")
    end = source.index("\nfunction orchestrationEntries(", start)
    helper = source[start:end]
    events = [
        {"seq": 10, "ts": "2026-09-07T00:00:00Z", "event_type": "agent_started", "segment_id": "initial", "k": "status"},
        {"seq": 20, "ts": "2026-09-07T00:01:00Z", "event_type": "agent_completed", "segment_id": "initial", "k": "status"},
        {"seq": 40, "ts": "2026-09-07T00:02:00Z", "event_type": "agent_started", "segment_id": "repair", "k": "status"},
        {"seq": 41, "ts": "2026-09-07T00:02:01Z", "event_type": "tool_use", "segment_id": "repair", "k": "tool", "tool": "patch", "args": {"repair_reason": "修正曲线与标注重叠"}},
        {"seq": 50, "ts": "2026-09-07T00:03:00Z", "event_type": "agent_completed", "segment_id": "repair", "k": "status"},
    ]
    script = helper + "\nprocess.stdout.write(JSON.stringify(slideOwnerSegments(" + json.dumps(events, ensure_ascii=False) + ")));"
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True,
    )
    segments = json.loads(completed.stdout)
    assert [segment["segmentId"] for segment in segments] == ["initial", "repair"]
    assert segments[0]["reused"] is False
    assert segments[1]["reused"] is True
    assert segments[1]["repairReason"] == "修正曲线与标注重叠"
    assert segments[1]["completed"] is True
    assert "发现问题后重新拉起原 Owner" in source


def test_shared_v1_livefeed_exposes_tool_use_before_task_finishes(tmp_path):
    _write_shared_v1_workspace(tmp_path)
    _append_live_event(tmp_path, 1, "agent_started")
    _append_live_event(
        tmp_path,
        2,
        "tool_use",
        payload={
            "tool_use_id": "tool-running",
            "name": "delegate_submit",
            "arguments": {"pages": [1, 2, 3]},
        },
    )

    during = trace.livefeed(
        str(tmp_path / "missing-job.log"),
        run_dir=str(tmp_path),
        deck_status="running",
    )

    assert during["live_event_schema"] == "mural.live-event.v1"
    assert during["live_next_cursor"] == 2
    assert [event["k"] for event in during["agents"]["orch"]] == [
        "status",
        "tool",
    ]
    assert during["agents"]["orch"][1]["phase"] == "running"

    _append_live_event(
        tmp_path,
        3,
        "tool_result",
        payload={
            "tool_use_id": "tool-running",
            "name": "delegate_submit",
            "ok": True,
            "result": {"wave_id": "wave-1"},
            "duration_ms": 418,
            "error_code": "",
        },
    )
    after = trace.livefeed(
        str(tmp_path / "missing-job.log"),
        run_dir=str(tmp_path),
        deck_status="running",
        live_cursor=during["live_next_cursor"],
    )
    result = after["agents"]["orch"][0]
    assert result["k"] == "tool_result"
    assert result["tool_use_id"] == "tool-running"
    assert result["duration_ms"] == 418


def test_shared_v1_live_events_group_real_orchestrator_and_slide_owner(tmp_path):
    _write_shared_v1_workspace(tmp_path)
    owners = json.loads((tmp_path / "_state" / "owners.json").read_text())
    owners["owners"]["deck"].update({"group_id": "deck-3pages", "pages": [1, 2, 3]})
    (tmp_path / "_state" / "owners.json").write_text(
        json.dumps(owners), encoding="utf-8"
    )
    _append_live_event(tmp_path, 1, "assistant_message", payload={"message": "开始编排"})
    _append_live_event(
        tmp_path,
        2,
        "agent_started",
        agent_id="owner-slide-e1",
        role="slide",
        payload={"group_id": "deck-3pages", "pages": [1, 2, 3]},
        turn=0,
        segment_id="segment-slide",
    )
    _append_live_event(
        tmp_path,
        3,
        "tool_use",
        agent_id="owner-slide-e1",
        role="slide",
        payload={
            "tool_use_id": "tool-slide",
            "name": "render",
            "arguments": {"pages": [1, 2, 3]},
        },
        segment_id="segment-slide",
    )
    _append_live_event(
        tmp_path,
        4,
        "tool_result",
        agent_id="owner-slide-e1",
        role="slide",
        payload={
            "tool_use_id": "tool-slide",
            "name": "render",
            "ok": False,
            "result": "render failed",
            "duration_ms": 900,
            "error_code": "render_error",
        },
        segment_id="segment-slide",
    )

    payload = trace.live_events(str(tmp_path), cursor=1)

    assert list(payload["agents"]) == ["slide_group_deck-3pages"]
    assert payload["page_agents"] == {
        "1": "slide_group_deck-3pages",
        "2": "slide_group_deck-3pages",
        "3": "slide_group_deck-3pages",
    }
    events = payload["agents"]["slide_group_deck-3pages"]
    assert [event["k"] for event in events] == ["status", "tool", "tool_result"]
    assert events[-1]["error_code"] == "render_error"


def test_agent_trace_payload_projects_per_agent_view_from_live_events(tmp_path):
    """New v1 workspaces only persist the unified journal; project it per Agent."""
    _write_shared_v1_workspace(tmp_path)
    owners = json.loads((tmp_path / "_state" / "owners.json").read_text())
    owners["owners"]["deck"].update({"group_id": "deck-3pages", "pages": [1, 2, 3]})
    (tmp_path / "_state" / "owners.json").write_text(
        json.dumps(owners), encoding="utf-8"
    )
    # Orchestrator turn: one visible message + a delegate tool call/result.
    _append_live_event(tmp_path, 1, "agent_started")
    _append_live_event(
        tmp_path, 2, "assistant_message", payload={"message": "开始编排整册"}
    )
    _append_live_event(
        tmp_path,
        3,
        "tool_use",
        payload={
            "tool_use_id": "call-orch",
            "name": "delegate_submit",
            "arguments": {"pages": [1, 2, 3]},
        },
    )
    _append_live_event(
        tmp_path,
        4,
        "tool_result",
        payload={
            "tool_use_id": "call-orch",
            "name": "delegate_submit",
            "ok": True,
            "result": {"wave_id": "wave-1"},
        },
    )
    # A separate Slide Owner runs its own turns and must not bleed into orch.
    _append_live_event(
        tmp_path,
        5,
        "agent_started",
        agent_id="owner-slide-e1",
        role="slide",
        payload={"group_id": "deck-3pages", "pages": [1, 2, 3]},
        turn=0,
        segment_id="segment-slide",
    )
    _append_live_event(
        tmp_path,
        6,
        "tool_use",
        agent_id="owner-slide-e1",
        role="slide",
        payload={
            "tool_use_id": "call-slide",
            "name": "render",
            "arguments": {"pages": [1, 2, 3]},
        },
        turn=1,
        segment_id="segment-slide",
    )
    _append_live_event(
        tmp_path,
        7,
        "tool_result",
        agent_id="owner-slide-e1",
        role="slide",
        payload={
            "tool_use_id": "call-slide",
            "name": "render",
            "ok": True,
            "result": "ok",
        },
        turn=1,
        segment_id="segment-slide",
    )

    orch = trace.agent_trace_payload(str(tmp_path), "orch")
    assert orch["projected_from"] == "live-events.jsonl"
    assert orch["agent"]["key"] == "orch"
    assert orch["source"] == "messages"
    orch_records = json.loads(orch["content"])
    # One assistant turn (message + tool_use) plus its tool_result user message.
    assert [record["role"] for record in orch_records] == ["assistant", "user"]
    assistant_blocks = [block["type"] for block in orch_records[0]["content"]]
    assert assistant_blocks == ["text", "tool_use"]
    assert orch_records[0]["content"][1]["id"] == "call-orch"
    assert orch_records[1]["content"][0]["tool_use_id"] == "call-orch"
    # The Slide Owner's render call must not appear in the orchestrator view.
    assert "call-slide" not in orch["content"]

    slide = trace.agent_trace_payload(str(tmp_path), "slide_group_deck-3pages")
    assert slide["projected_from"] == "live-events.jsonl"
    assert slide["agent"]["role"] == "slide"
    assert "call-slide" in slide["content"]
    assert "call-orch" not in slide["content"]

    # An unknown Agent key is a real 404, not the old per-format 404 for all.
    with pytest.raises(FileNotFoundError):
        trace.agent_trace_payload(str(tmp_path), "does-not-exist")


def test_agent_trace_projection_preserves_message_without_inventing_reasoning(tmp_path):
    """正文原样显示；未声明的旁支字段不冒充消息或可观测 reasoning。"""
    _write_shared_v1_workspace(tmp_path)
    _append_live_event(tmp_path, 1, "agent_started")
    _append_live_event(
        tmp_path,
        2,
        "assistant_message",
        payload={
            "message": "Bearer live-secret-token-value",
            "signature": "provider-thinking-signature",
            "reasoning_content": "hidden reasoning",
            "api_key": "sk-example-secret-value",
        },
    )
    _append_live_event(
        tmp_path,
        3,
        "assistant_analysis_summary",
        payload={"summary": "已确认画布尺寸，接下来写设计简报。"},
    )

    payload = trace.agent_trace_payload(str(tmp_path), "orch")
    content = payload["content"]

    assert "live-secret-token-value" in content
    assert "provider-thinking-signature" not in content
    assert "hidden reasoning" not in content
    assert "sk-example-secret-value" not in content
    # The sanctioned analysis summary is allowed and maps to a thinking summary.
    records = json.loads(content)
    thinking = [
        block for record in records for block in record.get("content", [])
        if isinstance(block, dict) and block.get("type") == "thinking"
    ]
    assert thinking and thinking[0]["summary"].startswith("已确认画布尺寸")
    assert "thinking" not in thinking[0]  # only the summary, never raw thinking


def test_agent_trace_payload_rejects_old_per_agent_directories(tmp_path):
    role_dir = tmp_path / "_trace/orchestrator"
    role_dir.mkdir(parents=True)
    original = '{"role":"orchestrator","label":"orch"}'
    (role_dir / "config.json").write_text(original)
    with pytest.raises(ValueError, match="unsupported_workspace_layout"):
        trace.agent_trace_payload(str(tmp_path), "orch")
    assert (role_dir / "config.json").read_text() == original



def test_shared_v1_live_events_preserve_failover_order_and_original_message(tmp_path):
    _write_shared_v1_workspace(tmp_path)
    for sequence, event_type, payload in (
        (1, "provider_started", {"attempt_id": "attempt-a", "channel_id": "ch-a"}),
        (2, "provider_failed", {"attempt_id": "attempt-a", "error_kind": "http_error",
            "status_code": 429, "error_summary": "rate limit exceeded"}),
        (3, "provider_failover", {"attempt_id": "attempt-a", "from_channel_id": "ch-a", "to_channel_id": "ch-b"}),
        (4, "provider_started", {"attempt_id": "attempt-b", "channel_id": "ch-b"}),
        (5, "provider_succeeded", {"attempt_id": "attempt-b", "channel_id": "ch-b"}),
    ):
        _append_live_event(tmp_path, sequence, event_type, payload=payload)
    _append_live_event(
        tmp_path,
        6,
        "assistant_message",
        payload={
            "message": "Bearer live-secret-token-value",
            "signature": "provider-thinking-signature",
            "reasoning_content": "hidden reasoning",
            "api_key": "sk-example-secret-value",
        },
    )
    with (tmp_path / "_trace" / "live-events.jsonl").open("ab") as handle:
        handle.write(b'{"schema":"mural.live-event.v1"')

    payload = trace.live_events(str(tmp_path))
    serialized = json.dumps(payload, ensure_ascii=False)

    assert [event["seq"] for event in payload["events"]] == [1, 2, 3, 4, 5, 6]
    assert [event["phase"] for event in payload["events"][:5]] == [
        "started", "failed", "failover", "started", "succeeded",
    ]
    assert payload['events'][1]['status_code'] == 429
    assert payload['events'][1]['error_summary'] == 'rate limit exceeded'
    assert "live-secret-token-value" in serialized
    assert "provider-thinking-signature" not in serialized
    assert "hidden reasoning" not in serialized
    assert "sk-example-secret-value" not in serialized


def test_resume_info_accepts_interrupted_shared_v1_workspace(tmp_path, monkeypatch):
    _write_shared_v1_workspace(tmp_path)
    log = tmp_path / "job.log"
    log.write_text("process ended before delivery\n", encoding="utf-8")
    monkeypatch.setattr(engine, "log_path", lambda _deck_id: log)

    info = jobs.resume_info(101, tmp_path)

    assert info["eligible"] is True
    assert info["durable"] is True
    assert info["exit_reason"] == "process_interrupted"
    assert info["trace_source"] == "_state/orchestration.json"


@pytest.mark.parametrize("durable,authentication", [(True, False), (False, False), (True, True)])
def test_turn_limit_resume_requires_durable_non_authentication_failure(tmp_path, monkeypatch, durable, authentication):
    if durable:
        _write_shared_v1_workspace(tmp_path, provider_error_kind="authentication" if authentication else "")
    (tmp_path / "result.json").write_text(json.dumps({"status": "error", "exit_reason": "agent_turn_limit"}))
    monkeypatch.setattr(engine, "log_path", lambda _: tmp_path / "unused.log")
    info = jobs.resume_info(228, tmp_path)
    assert info["eligible"] is (durable and not authentication)
    assert info["exit_reason"] == ("agent_turn_limit" if durable else "unsupported_workspace_layout")


def test_turn_limit_pause_is_explicit_and_never_requeues():
    with mock.patch.object(jobs, "_set_status") as set_status:
        jobs._pause_recoverable_deck(228, {"exit_reason": "agent_turn_limit"})
    assert set_status.call_args.args == (228, "paused_recoverable")
    assert "手动继续" in set_status.call_args.kwargs["error"]
    assert "上游模型通道暂时不可用" not in set_status.call_args.kwargs["error"]


def test_resume_info_does_not_offer_provider_resume_for_training_rejection(tmp_path, monkeypatch):
    _write_shared_v1_workspace(tmp_path)
    (tmp_path / "result.json").write_text(json.dumps({"status": "completed", "exit_reason": "training_rejected",
        "training_admission": "rejected", "artifact_status": "complete"}))
    monkeypatch.setattr(engine, "log_path", lambda _: tmp_path / "unused.log")
    info = jobs.resume_info(101, tmp_path)
    assert info["durable"] and not info["eligible"]
    assert info["exit_reason"] == "training_rejected"


def test_resume_info_rejects_shared_v1_terminal_authentication(tmp_path, monkeypatch):
    _write_shared_v1_workspace(tmp_path, provider_error_kind="authentication")
    log = tmp_path / "job.log"
    log.write_text("provider authentication failed\n", encoding="utf-8")
    monkeypatch.setattr(engine, "log_path", lambda _deck_id: log)

    info = jobs.resume_info(102, tmp_path)

    assert info["eligible"] is False
    assert info["provider_configuration_error"] is True


@pytest.mark.parametrize("error_kind", ["http_error", "access_disabled"])
def test_resume_info_does_not_retry_policy_disabled_account(tmp_path, monkeypatch, error_kind):
    _write_shared_v1_workspace(tmp_path, provider_error_kind=error_kind)
    (tmp_path / "result.json").write_text(json.dumps({
        "status": "error", "exit_reason": "api_failed",
        "error": "Your access has been disabled for a suspected violation of our Usage Policy.",
    }))
    monkeypatch.setattr(engine, "log_path", lambda _: tmp_path / "missing.log")
    info = jobs.resume_info(236, tmp_path)
    assert not info["eligible"]
    assert info["provider_access_disabled"]
    assert info["provider_configuration_error"]


def test_resume_info_does_not_confuse_later_outage_with_earlier_account_failure(tmp_path, monkeypatch):
    _write_shared_v1_workspace(tmp_path, provider_error_kind="http_error")
    log = tmp_path / "job.log"
    log.write_text("Your access has been disabled for a suspected violation of our Usage Policy.\n")
    (tmp_path / "result.json").write_text(json.dumps({
        "status": "error", "exit_reason": "api_failed", "error": "HTTP 503 upstream unavailable",
    }))
    monkeypatch.setattr(engine, "log_path", lambda _: log)
    info = jobs.resume_info(236, tmp_path)
    assert info["eligible"]
    assert not info["provider_access_disabled"]


def test_job_launcher_drops_ambient_authoring_profile():
    source = open(jobs.__file__, encoding="utf-8").read()
    drop_block = source[source.index("_DROP = ("):source.index("child_env =", source.index("_DROP = ("))]
    assert '"MURAL_AUTHORING_PROFILE"' in drop_block


def test_v06_is_archived_instead_of_silently_aliasing_to_v1():
    assert engine.canon_skill("mural-presenter-v0.6") is None
    assert engine.canon_pipeline("mural-presenter-v0.6-harness") is None


@pytest.mark.parametrize("skill_name", ["mural-presenter-v1", "mural-presenter-v1"])
def test_shared_v1_maps_the_selected_model_to_the_native_runtime_contract(monkeypatch, skill_name):
    selected_root = engine._MURAL_REPOSITORY_ROOT / "skills" / skill_name
    monkeypatch.setenv("MURAL_SKILL_DIR", str(selected_root))
    monkeypatch.setattr(engine, "SKILLS", dict(engine.SKILLS))
    monkeypatch.setattr(engine, "PIPELINES", dict(engine.PIPELINES))
    engine.SKILLS["mural-presenter-v1"] = engine._mural_v1_skill()
    model = {
        "label": "PPTAgent Qwen 3.5 27B",
        "backend": "openai",
        "engine_model": "pptagent-qwen35-27b-ckpt1764",
        "base_url": "https://model.example.test/v1/",
        "api_key": "test-only",
        "multimodal": True,
        "thinking_mode": "none",
        "slide_concurrency": 4,
    }

    env = engine.selection_env(
        "custom:qwen27b",
        "mural-presenter-v1-harness",
        "mural-presenter-v1",
        model_config=model,
    )

    assert engine.SKILLS["mural-presenter-v1"]["status"] == "current"
    assert "mural-presenter-v1.0" not in engine.SKILLS
    assert env["MODEL_API_FORMAT"] == "openai"
    assert env["MODEL"] == "pptagent-qwen35-27b-ckpt1764"
    assert env["MODEL_BASE_URL"] == "https://model.example.test/v1"
    assert env["MODEL_API_KEY"] == "test-only"
    assert "MODEL_CONCURRENCY" not in env and "SLIDE_CONCURRENCY" not in env
    assert "MODEL_CONCURRENCY" not in engine.SKILLS["mural-presenter-v1"]["runtime_env"]
    assert service_config.load_runtime_config(env)["resources"]["provider"] == 64
    assert env["MURAL_SKILL_DIR"] == engine.SKILLS["mural-presenter-v1"]["path"]
    assert Path(env["MURAL_SKILL_DIR"]).resolve() == selected_root.resolve()
    assert (Path(env["MURAL_SKILL_DIR"]) / "SKILL.md").is_file()
    assert "MURAL_SKILLS_DIR" not in env
    assert "CLEAN_SKILLS_DIR" not in env


@pytest.fixture
def selected_skill_root(tmp_path, monkeypatch):
    selected = tmp_path / "independent-skill-name"
    for relative in engine.SKILLS["mural-presenter-v1"]["required_files"]:
        path = selected / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"selected {relative}\n", encoding="utf-8")
    reference = selected / "references" / "chosen-reference.md"
    reference.parent.mkdir()
    reference.write_text("selected reference\n", encoding="utf-8")
    monkeypatch.setenv("MURAL_SKILL_DIR", str(selected))
    monkeypatch.setattr(engine, "SKILLS", dict(engine.SKILLS))
    monkeypatch.setattr(engine, "PIPELINES", dict(engine.PIPELINES))
    return selected


def test_shared_v1_defaults_ignore_retired_parent_directory_variables(tmp_path, monkeypatch):
    monkeypatch.delenv("MURAL_SKILL_DIR", raising=False)
    monkeypatch.setenv("MURAL_SKILLS_DIR", str(tmp_path / "plural-parent"))
    monkeypatch.setenv("PPTAGENT_MURAL_V10_SKILLS_ROOT", str(tmp_path / "v10-parent"))
    monkeypatch.setenv("PPTAGENT_MURAL_V06_SKILLS_ROOT", str(tmp_path / "v06-parent"))
    monkeypatch.setattr(engine, "MURAL_V10_SKILLS_ROOT", tmp_path / "retired-root")
    skill = engine._mural_v1_skill()
    expected = engine._MURAL_REPOSITORY_ROOT / "skills" / "mural-presenter-v1"
    assert skill["ready"], skill["unavailable_reason"]
    assert skill["path"] == str(expected.resolve())
    assert skill["runtime_env"]["MURAL_SKILL_DIR"] == skill["path"]


def test_shared_v1_selected_root_drives_descriptor_job_and_environment(selected_skill_root):
    model = {"backend": "openai", "engine_model": "test-model", "api_key": "test-only",
             "base_url": "https://model.example.test/v1", "multimodal": True,
             "thinking_mode": "none"}
    job = engine.build_job("selection-test", {"query": "test"}, selected_skill_root.parent / "unused-run",
                           skill_key="mural-presenter-v1", model_config=model)
    skill = job["skill"]
    assert skill["ready"], skill["unavailable_reason"]
    assert skill["path"] == str(selected_skill_root)
    assert skill["name"] == selected_skill_root.name
    assert set(skill["instruction_paths"].values()) == {str(selected_skill_root)}
    assert (Path(skill["path"]) / "SKILL.md").read_text() == "selected SKILL.md\n"
    assert job["skill_mount_dir"] == str(selected_skill_root)
    env = engine.selection_env(None, "mural-presenter-v1-harness", "mural-presenter-v1", model_config=model)
    assert env["MURAL_SKILL_DIR"] == skill["path"]
    assert env["MURAL_RELEASE_MANIFEST"] == str(engine.MURAL_V1_RELEASE_MANIFEST_PATH)
    assert "CLEAN_SKILLS_DIR" not in env and "MURAL_SKILLS_DIR" not in env


def test_shared_v1_auto_language_preserves_studio_hint(selected_skill_root):
    model = {"backend": "openai", "engine_model": "test-model", "api_key": "test-only",
             "base_url": "https://model.example.test/v1", "multimodal": True,
             "thinking_mode": "none"}
    job = engine.build_job(
        "language-test",
        {"query": "制作中文演示", "lang": "zh"},
        selected_skill_root.parent / "language-run",
        skill_key="mural-presenter-v1",
        model_config=model,
    )
    assert job["seed"]["lang"] == "zh"


@pytest.fixture
def pinned_skill_workspace(selected_skill_root, monkeypatch):
    workspace = selected_skill_root.parent / "pinned-workspace"
    monkeypatch.setenv("MURAL_STUDIO_DATA_DIR", str(selected_skill_root.parent / "studio-data"))
    script = (
        "import json, sys\n"
        "from runtime.shared_objects import SharedSkillStore\n"
        "from state.workspace import WorkspaceState\n"
        "from pathlib import Path\n"
        "paths = WorkspaceState(sys.argv[1]).paths\n"
        "source = Path(sys.argv[2])\n"
        "ref = SharedSkillStore.for_workspace(paths.root).pin(paths, source, skill_name=source.name)\n"
        "print(json.dumps({'object_root': str(ref.object_root), 'release_record': str(ref.release_record)}))\n"
    )
    result = subprocess.run(
        [str(engine.INFERENCE_DIR / ".venv/bin/python"), "-B", "-c", script,
         str(workspace), str(selected_skill_root)],
        cwd=str(engine.MURAL_V1_HARNESS_ROOT), capture_output=True, text=True, check=True, timeout=30,
    )
    return SimpleNamespace(workspace=workspace, source=selected_skill_root, **json.loads(result.stdout))


def test_resume_uses_verified_pin_when_new_source_is_missing(pinned_skill_workspace, monkeypatch):
    case = pinned_skill_workspace
    missing = case.source.parent / "missing-new-selection"
    monkeypatch.setenv("MURAL_SKILL_DIR", str(missing))
    before = {str(path): path.read_bytes() for path in case.workspace.rglob("*") if path.is_file()}
    model = {"backend": "openai", "engine_model": "test-model", "api_key": "test-only",
             "base_url": "https://model.example.test/v1", "thinking_mode": "none"}
    job = engine.build_job("resume-pin", {}, case.workspace, model_config=model, resume_workspace=True)
    assert job["resume_workspace"] and job["pipeline"]["ready"]
    assert job["skill"]["path"] == case.object_root
    assert job["skill"]["name"] == case.source.name
    assert job["skill_mount_dir"] == case.object_root
    assert job["selected_skill_name"] == missing.name
    assert (Path(job["skill"]["path"]) / "SKILL.md").read_bytes() == (case.source / "SKILL.md").read_bytes()
    env = engine.selection_env(None, job["pipeline_version"], job["skill_version"],
                               model_config=model, skill_config=job["skill"])
    assert env["MURAL_SKILL_DIR"] == case.object_root
    assert env["MURAL_RELEASE_MANIFEST"] == str(engine.MURAL_V1_RELEASE_MANIFEST_PATH)
    assert "MURAL_RELEASE_POLICY" not in env and "MURAL_ALLOW_DIRTY_RELEASE" not in env
    assert {str(path): path.read_bytes() for path in case.workspace.rglob("*") if path.is_file()} == before
    with pytest.raises(ValueError, match="SKILL.md"):
        engine.build_job("not-resume", {}, case.workspace, model_config=model)


@pytest.mark.parametrize("damage", ["missing-ref", "invalid-ref", "object-bytes", "release-record"])
def test_resume_requires_a_valid_existing_pin_even_when_current_source_is_ready(pinned_skill_workspace, damage):
    case = pinned_skill_workspace
    reference = case.workspace / "_state/skill-ref.json"
    if damage == "missing-ref":
        reference.unlink()
    elif damage == "invalid-ref":
        reference.write_text("{}")
    elif damage == "object-bytes":
        target = Path(case.object_root) / "SKILL.md"
        target.chmod(0o644)
        target.write_text("corrupt immutable test object")
    else:
        target = Path(case.release_record)
        target.chmod(0o644)
        target.write_text("{}")
    with pytest.raises(ValueError, match="无法恢复固定 Skill"):
        engine.build_job("bad-pin", {}, case.workspace, model_config={"engine_model": "test"},
                         resume_workspace=True)


def test_resume_pin_does_not_bypass_harness_readiness(pinned_skill_workspace, monkeypatch):
    monkeypatch.setattr(engine, "_mural_v1_runtime_defaults", lambda: {})
    with pytest.raises(ValueError, match="Harness 缺少"):
        engine.build_job("missing-runtime", {}, pinned_skill_workspace.workspace,
                         model_config={"engine_model": "test"}, resume_workspace=True)


@pytest.mark.parametrize("relative", ["SKILL.md", "roles/slide.md", "assets/base.css", "scripts/orchestrator.py"])
def test_shared_v1_preflight_checks_only_selected_root(selected_skill_root, relative):
    (selected_skill_root / relative).unlink()
    skill = engine._mural_v1_skill()
    assert not skill["ready"] and not skill["source_revision"]
    assert relative in skill["unavailable_reason"]
    with pytest.raises(ValueError, match=re.escape(relative)):
        engine.build_job("missing-selection", {}, selected_skill_root.parent / "unused-run",
                         skill_key="mural-presenter-v1")


@pytest.mark.parametrize("relative", ["SKILL.md", "references/chosen-reference.md", "scripts/orchestrator.py", "assets/base.css"])
def test_shared_v1_revision_hashes_selected_content_not_sibling_skills(selected_skill_root, relative):
    before = engine._mural_v1_skill()["source_revision"]
    (selected_skill_root.parent / "unrelated-skill.md").write_text("unrelated change")
    assert engine._mural_v1_skill()["source_revision"] == before
    (selected_skill_root / relative).write_text("selected change")
    assert engine._mural_v1_skill()["source_revision"] != before


@pytest.mark.parametrize("directory,relative", [("scripts", "orchestrator.py"), ("assets", "base.css")])
def test_shared_v1_revision_includes_shared_resource_directory(selected_skill_root, directory, relative):
    shared = selected_skill_root.parent / f"shared-{directory}"
    (selected_skill_root / directory).rename(shared)
    (selected_skill_root / directory).symlink_to(shared, target_is_directory=True)
    before = engine._mural_v1_skill()["source_revision"]
    (shared / relative).write_text("shared resource change")
    assert engine._mural_v1_skill()["source_revision"] != before


def test_doctor_receives_selected_skill_root(selected_skill_root, monkeypatch):
    monkeypatch.setitem(engine.SKILLS, "mural-presenter-v1", engine._mural_v1_skill())
    monkeypatch.setattr(environment_checks, "REPORT_DIR", selected_skill_root.parent / "reports")
    monkeypatch.setattr(engine, "_engine_python", lambda: str(engine.INFERENCE_DIR / ".venv/bin/python"))
    monkeypatch.setattr(engine, "render_env", lambda env: env)
    monkeypatch.setattr(service_config, "system_runtime_env", lambda: {})
    monkeypatch.setattr(service_config, "runtime_env", lambda _: {})
    model = {"backend": "openai", "engine_model": "test-model", "api_key": "test-only",
             "base_url": "https://model.example.test/v1", "multimodal": True,
             "thinking_mode": "none"}
    dispatched = []

    class Process:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def capture_doctor(*command, **kwargs):
        dispatched.append(kwargs["env"])
        assert Path(command[1]) == engine.MURAL_V1_HARNESS_ROOT / "infer.py"
        report = Path(command[command.index("--json") + 1])
        report.write_text(json.dumps({"checked": True, "status": "pass", "checks": []}))
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_doctor)
    asyncio.run(environment_checks.run(user_id=95, model_key="test", model_label="Test",
        pipeline_key="mural-presenter-v1-harness", skill_key="mural-presenter-v1", model_config=model))
    assert len(dispatched) == 1
    assert dispatched[0]["MURAL_SKILL_DIR"] == str(selected_skill_root)


@pytest.mark.parametrize("selection_present", [False, True])
@pytest.mark.parametrize("resume_workspace", [False, True])
@pytest.mark.parametrize("effort", ["", "xhigh", "max"])
def test_job_launcher_uses_selected_skill_root_without_ambient_override(tmp_path, selected_skill_root, monkeypatch, selection_present, resume_workspace, effort):
    selected = str(selected_skill_root)
    selected_skill = engine._mural_v1_skill()
    monkeypatch.setitem(engine.SKILLS, "mural-presenter-v1", selected_skill)
    monkeypatch.setenv("MURAL_SKILL_DIR", str(tmp_path / "ambient-skill"))
    monkeypatch.setenv("MODEL_ROUTE_POOL_PATH", str(tmp_path / "ambient-gpt6-pool.json"))
    model_key = "single-skill-test"
    monkeypatch.setitem(engine.MODELS, model_key, {
        "label": "Test model", "backend": "openai", "engine_model": "test-model",
        "base_url": "https://model.example.test/v1", "api_key": "test-only",
        "multimodal": True, "thinking_mode": "none",
    })
    row = {"status": "queued", "run_dir": str(tmp_path), "user_id": 1,
           "seed_json": json.dumps({"runtime_limits": {}, "_resume_workspace": resume_workspace,
               "effective_thinking": bool(effort), "effective_effort": effort, "requested_effort": effort}), "model": model_key,
           "pipeline": "mural-presenter-v1-harness", "skill_version": "mural-presenter-v1"}
    monkeypatch.setattr(jobs, "_load_deck", lambda _: row)
    monkeypatch.setattr(jobs, "_set_status", lambda *_args, **_kwargs: None)
    failures = []
    monkeypatch.setattr(jobs, "_fail_deck", lambda *args: failures.append(args))
    built = []

    def build_job(*_args, **kwargs):
        built.append(kwargs)
        monkeypatch.setitem(engine.SKILLS, "mural-presenter-v1", engine._mural_v1_skill())
        return {"skill": selected_skill}

    monkeypatch.setattr(engine, "build_job", build_job)
    monkeypatch.setattr(engine, "write_job_file", lambda *_args: tmp_path / "job.json")
    monkeypatch.setattr(engine, "log_path", lambda _: tmp_path / "job.log")
    monkeypatch.setattr(engine, "runner_cmd", lambda _: ["unused-test-command"])
    monkeypatch.setattr(engine, "render_env", lambda env: env)
    monkeypatch.setattr(service_config, "system_runtime_env", lambda: {})
    monkeypatch.setattr(service_config, "runtime_env", lambda _: {})
    if not selection_present:
        monkeypatch.setattr(engine, "selection_env", lambda *_args, **_kwargs: {})
    dispatched = []

    async def capture_launch(*_args, **kwargs):
        dispatched.append(kwargs["env"])
        raise OSError("test stopped before process launch")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_launch)
    asyncio.run(jobs._process(1))
    assert len(dispatched) == 1 and len(failures) == 1
    assert "MODEL_ROUTE_POOL_PATH" not in dispatched[0]
    assert dispatched[0]["MODEL_REASONING_EFFORT"] == effort
    assert dispatched[0]["STUDIO_REQUESTED_EFFORT"] == dispatched[0]["STUDIO_EFFECTIVE_EFFORT"] == effort
    assert built[0]["resume_workspace"] is resume_workspace
    if selection_present:
        assert dispatched[0]["MURAL_SKILL_DIR"] == selected
    else:
        assert "MURAL_SKILL_DIR" not in dispatched[0]


def test_anthropic_override_keeps_lifecycle_tool_credentials(tmp_path, monkeypatch):
    shared = tmp_path / "shared.env"
    shared.write_text(
        "\n".join(
            [
                "ANTHROPIC_API_KEY=shared-key",
                "ANTHROPIC_API_KEY_FALLBACKS=shared-fallback",
                "ANTHROPIC_BASE_URL=https://shared.example.test",
                "ENABLE_IMAGE_GEN=1",
                "OPENAI_API_KEY=image-key",
                "SERPER_API_KEY=search-key",
                "GEMINI_API_KEY=judge-key",
                "VISION_BACKEND=gemini",
                "VISION_CRITIC_API_KEY=legacy-vision-key",
                "TOKENHUB_VISION_MODEL=legacy-vision-model",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(service_config, "PROVIDER_ENV_PATH", shared)
    monkeypatch.setattr(engine, "DATA_DIR", tmp_path / "studio-data")

    override = engine._write_anthropic_override_dotenv(
        "TOKENHUB_CLAUDE_KEY_A",
        "teacher-key",
        "https://teacher.example.test",
        "claude-opus-test",
    )
    values = engine._read_env_file(override)

    assert values["ANTHROPIC_API_KEY"] == "teacher-key"
    assert values["ANTHROPIC_BASE_URL"] == "https://teacher.example.test"
    assert values["ANTHROPIC_MODEL"] == "claude-opus-test"
    assert "ANTHROPIC_API_KEY_FALLBACKS" not in values
    assert values["ENABLE_IMAGE_GEN"] == "1"
    assert values["OPENAI_API_KEY"] == "image-key"
    assert values["SERPER_API_KEY"] == "search-key"
    assert values["GEMINI_API_KEY"] == "judge-key"
    assert not any(
        key == "VISION_BACKEND"
        or key.startswith("VISION_")
        or key.startswith("TOKENHUB_VISION")
        for key in values
    )
    assert os.stat(override).st_mode & 0o777 == 0o600


def test_second_deployment_model_uses_its_own_endpoint_and_thinking_transport(
    monkeypatch,
):
    prefix = "SENSENOVA_MODEL2"
    monkeypatch.setenv(f"{prefix}_BASE_URL", "http://10.119.18.133:8000/v1/")
    monkeypatch.setenv(f"{prefix}_NAME", "pptagent-test")
    monkeypatch.setenv(f"{prefix}_DISPLAY_NAME", "PPTAgent 2")
    monkeypatch.setenv(f"{prefix}_SLIDE_CONCURRENCY", "4")
    monkeypatch.setenv(f"{prefix}_THINKING_TRANSPORT", "chat_template_kwargs")

    model = engine._environment_model(prefix)

    assert model is not None
    assert model["label"] == "PPTAgent 2"
    assert model["engine_model"] == "pptagent-test"
    assert model["base_url"] == "http://10.119.18.133:8000/v1"
    assert model["thinking_transport"] == "chat_template_kwargs"
    assert model["slide_concurrency"] == 4


def test_deployment_model_can_use_openai_responses_api_format(
    monkeypatch,
):
    prefix = "SENSENOVA_MODEL5"
    monkeypatch.setenv(f"{prefix}_BASE_URL", "https://tokenhub.sensetime.com/v1/")
    monkeypatch.setenv(f"{prefix}_NAME", "gpt-6-astra/azure/sfa")
    monkeypatch.setenv(f"{prefix}_DISPLAY_NAME", "GPT-6 Astra")
    monkeypatch.setenv(f"{prefix}_API_FORMAT", "openai_responses")
    monkeypatch.setenv(f"{prefix}_API_KEY_ENV", "GPT6_ASTRA_API_KEY")
    monkeypatch.setenv(f"{prefix}_THINKING_EFFORT", "xhigh")
    monkeypatch.setenv(f"{prefix}_MULTIMODAL", "1")

    model = engine._environment_model(prefix)
    assert model is not None
    assert model["api_format"] == "openai_responses"
    assert model["api_key_env"] == "GPT6_ASTRA_API_KEY"
    assert model["thinking_transport"] == "reasoning_effort"
    assert model["thinking_effort"] == "xhigh"
    assert model["thinking_effort_levels"] == ("low", "medium", "high", "xhigh", "max")

    selected_root = engine._MURAL_REPOSITORY_ROOT / "skills" / "mural-presenter-v1"
    monkeypatch.setenv("MURAL_SKILL_DIR", str(selected_root))
    monkeypatch.setattr(engine, "SKILLS", dict(engine.SKILLS))
    monkeypatch.setattr(engine, "PIPELINES", dict(engine.PIPELINES))
    monkeypatch.setattr(engine, "_deployment_secret", lambda name: "secret" if name == "GPT6_ASTRA_API_KEY" else "")
    engine.SKILLS["mural-presenter-v1"] = engine._mural_v1_skill()

    env = engine.selection_env(
        "deployment-model-5",
        "mural-presenter-v1-harness",
        "mural-presenter-v1",
        model_config=model,
    )

    assert env["MODEL_API_FORMAT"] == "openai_responses"
    assert env["MODEL"] == "gpt-6-astra/azure/sfa"
    assert env["MODEL_BASE_URL"] == "https://tokenhub.sensetime.com/v1"
    assert env["MODEL_API_KEY"] == "secret"
    assert env["THINK_EFFORT"] == "xhigh"


def test_gpt6_route_pool_is_selected_only_for_configured_model(monkeypatch, tmp_path):
    prefix = "SENSENOVA_MODEL5"
    pool = tmp_path / "routes.json"
    monkeypatch.setenv(f"{prefix}_BASE_URL", "https://provider.example/v1")
    monkeypatch.setenv(f"{prefix}_NAME", "gpt-6-astra/azure/sfa")
    monkeypatch.setenv(f"{prefix}_API_FORMAT", "openai_responses")
    monkeypatch.setenv(f"{prefix}_ROUTE_POOL_PATH", str(pool))
    model = engine._environment_model(prefix)
    env = engine.selection_env("deployment-model-5", "mural-presenter-v1-harness",
                               "mural-presenter-v1", model_config=model)
    assert env["MODEL_ROUTE_POOL_PATH"] == str(pool)
    assert env["MODEL"] == "gpt-6-astra/azure/sfa"
    without_pool = dict(model)
    without_pool.pop("route_pool_path")
    env = engine.selection_env("deployment-model-5", "mural-presenter-v1-harness",
                               "mural-presenter-v1", model_config=without_pool)
    assert "MODEL_ROUTE_POOL_PATH" not in env
    monkeypatch.setenv(f"{prefix}_NAME", "Qwen3.8-27B")
    with pytest.raises(ValueError, match="only supported for GPT-6"):
        engine._environment_model(prefix)


def test_v04_uses_broker_owning_clean_runner(monkeypatch):
    from pathlib import Path
    import importlib.util

    serve_one = Path(__file__).resolve().parents[2] / "inference" / "serve_one.py"
    monkeypatch.syspath_prepend(str(serve_one.parent))
    spec = importlib.util.spec_from_file_location("v04_serve_one_test", serve_one)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert module._uses_clean_runner({
        "pipeline": {"skill_mode": "mural-presenter-v0.4-adaptive"},
    })


@pytest.mark.parametrize(
    "skill_version",
    ["mural-presenter-v0.3", "mural-presenter-v0.4", "mural-presenter-v1"],
)
def test_role_split_delivery_recovery_uses_role_entry_not_deck_py(
    tmp_path, skill_version, monkeypatch
):
    from app import jobs
    monkeypatch.setattr(jobs, "_run_noninteractive",
                        lambda *a, **kw: pytest.fail("retired entry must not rebuild"))
    expected = "只读验收" if skill_version == "mural-presenter-v1" else "历史 Harness/Skill 入口已停用"
    assert expected in jobs._ensure_static_delivery(
        {"skill_version": skill_version, "seed_json": "{}"}, tmp_path)


@pytest.mark.parametrize(
    "skill_version",
    ["mural-presenter-v0.3", "mural-presenter-v0.4", "mural-presenter-v1"],
)
def test_role_split_delivery_rebuilds_a_stale_formal_player(
    tmp_path, skill_version, monkeypatch
):
    from app import jobs
    monkeypatch.setattr(jobs, "_run_noninteractive",
                        lambda *a, **kw: pytest.fail("retired entry must not rebuild"))
    expected = "只读验收" if skill_version == "mural-presenter-v1" else "历史 Harness/Skill 入口已停用"
    assert expected in jobs._ensure_static_delivery(
        {"skill_version": skill_version, "seed_json": "{}"}, tmp_path)


def test_v1_delivery_does_not_recover_from_workspace_or_mutable_source_entry(tmp_path):
    slides = tmp_path / "slides"
    slides.mkdir()
    (slides / "slide_01.html").write_text("<section>ok</section>", encoding="utf-8")
    local_entry = tmp_path / "skills" / "mural-presenter-v1" / "scripts" / "orchestrator.py"
    local_entry.parent.mkdir(parents=True)
    local_entry.write_text("# stale workspace snapshot\n", encoding="utf-8")
    row = {"id": 1, "seed_json": "{}", "skill_version": "mural-presenter-v1"}
    calls = []

    def completed(command, **kwargs):
        calls.append(command)
        if command[2] == "finalize":
            (tmp_path / "present.html").write_text("<html>built</html>", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="status:PASS", stderr="")

    with mock.patch.object(jobs, "_run_noninteractive", side_effect=completed):
        assert "只读验收" in jobs._ensure_static_delivery(row, tmp_path)
    assert calls == [] and not (tmp_path / "present.html").exists()


def test_retired_mutable_skill_export_helper_is_removed():
    assert not hasattr(jobs, "_prepare_portable_static_export")


def test_failed_engine_result_is_not_promoted_by_stale_pngs(tmp_path):
    (tmp_path / "renders").mkdir()
    (tmp_path / "renders" / "slide_01.png").write_bytes(b"stale-render")
    (tmp_path / "result.json").write_text(
        json.dumps({
            "status": "failed",
            "reason": "unresolved Review failure",
            "exit_reason": "stalled_repetition",
        }),
        encoding="utf-8",
    )
    row = {"id": 30, "seed_json": "{}", "status": "running"}
    with (
        mock.patch.object(jobs, "_load_deck", return_value=row),
        mock.patch.object(jobs, "_fail_deck") as fail,
        mock.patch.object(jobs, "_set_status") as set_status,
    ):
        jobs._finalize(30, tmp_path, rc=1)

    fail.assert_called_once()
    set_status.assert_not_called()


def test_needs_improvement_is_a_successful_usable_delivery(tmp_path):
    (tmp_path / "renders").mkdir()
    (tmp_path / "renders" / "slide_01.png").write_bytes(b"final-render")
    (tmp_path / "result.json").write_text(
        json.dumps(
            {
                "status": "needs_improvement",
                "reason": "bounded Review left a known visual issue",
            }
        ),
        encoding="utf-8",
    )
    row = {"id": 31, "seed_json": "{}", "status": "running"}
    with (
        mock.patch.object(jobs, "_load_deck", return_value=row),
        mock.patch.object(jobs, "_ensure_static_delivery", return_value=None),
        mock.patch.object(jobs, "_fail_deck") as fail,
        mock.patch.object(jobs, "_set_status") as set_status,
        mock.patch.object(jobs, "_settle_waiting_revisions"),
    ):
        jobs._finalize(31, tmp_path, rc=0)

    fail.assert_not_called()
    set_status.assert_called_once_with(
        31,
        "completed",
        slide_count=1,
        error=None,
        finished_at=mock.ANY,
    )


def _write_recoverable_workspace(root, *, exit_reason="api_failed"):
    _write_shared_v1_workspace(root)
    (root / "result.json").write_text(json.dumps({"status": "error", "exit_reason": exit_reason}))


def test_resume_info_accepts_transient_failure_with_empty_wave_map(
    tmp_path,
    monkeypatch,
):
    _write_recoverable_workspace(tmp_path)
    log = tmp_path / "job.log"
    log.write_text("provider returned 503 no channel\n", encoding="utf-8")
    monkeypatch.setattr(engine, "log_path", lambda _deck_id: log)

    info = jobs.resume_info(32, tmp_path)

    assert info["eligible"] is True
    assert info["durable"] is True
    assert info["exit_reason"] == "api_failed"


def test_resume_info_accepts_durable_stalled_orchestrator_checkpoint(
    tmp_path,
    monkeypatch,
):
    _write_recoverable_workspace(tmp_path, exit_reason="stalled_repetition")
    log = tmp_path / "job.log"
    log.write_text(
        "[stalled] repeated a recoverable contract action without progress\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(engine, "log_path", lambda _deck_id: log)

    info = jobs.resume_info(39, tmp_path)

    assert info["eligible"] is True
    assert info["durable"] is True
    assert info["exit_reason"] == "stalled_repetition"


def test_stalled_checkpoint_pause_message_does_not_claim_provider_outage():
    with mock.patch.object(jobs, "_set_status") as set_status:
        jobs._pause_recoverable_deck(
            39,
            {"exit_reason": "stalled_repetition"},
        )

    kwargs = set_status.call_args.kwargs
    assert "执行轨迹停滞" in kwargs["error"]
    assert "模型通道暂时不可用" not in kwargs["error"]


def test_resume_info_recovers_child_outage_hidden_by_incomplete_closure(
    tmp_path,
    monkeypatch,
):
    _write_recoverable_workspace(tmp_path, exit_reason="incomplete_closure")
    path = tmp_path / "_state/orchestration.json"
    state = json.loads(path.read_text())
    task = state["tasks"]["task-shared-v1"]
    task["status"] = "failed"
    path.write_text(json.dumps(state))
    _append_live_event(tmp_path, 1, "agent_failed", agent_id=task["owner_id"], role="slide",
                       payload={"error_type": "AgentProviderError"})
    log = tmp_path / "job.log"
    log.write_text("root closed after child transport interruption\n", encoding="utf-8")
    monkeypatch.setattr(engine, "log_path", lambda _deck_id: log)

    info = jobs.resume_info(35, tmp_path)

    assert info["eligible"] is True
    assert info["exit_reason"] == "api_failed"
    assert info["root_exit_reason"] == "incomplete_closure"
    assert info["child_exit_reason"] == "api_failed"


def test_resume_info_rejects_configuration_or_authentication_failure(
    tmp_path,
    monkeypatch,
):
    _write_recoverable_workspace(tmp_path)
    log = tmp_path / "job.log"
    log.write_text("[API auth] invalid credential\n", encoding="utf-8")
    monkeypatch.setattr(engine, "log_path", lambda _deck_id: log)

    info = jobs.resume_info(33, tmp_path)

    assert info["eligible"] is False
    assert info["provider_configuration_error"] is True


def test_resume_info_accepts_transient_gateway_signature_mismatch(
    tmp_path,
    monkeypatch,
):
    _write_recoverable_workspace(tmp_path)
    log = tmp_path / "job.log"
    log.write_text(
        "[API auth] PermissionDeniedError: request signature we calculated "
        "does not match the signature you provided\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(engine, "log_path", lambda _deck_id: log)

    info = jobs.resume_info(36, tmp_path)

    assert info["eligible"] is True
    assert info["provider_configuration_error"] is False


def test_resume_info_accepts_invalid_thinking_signature(
    tmp_path,
    monkeypatch,
):
    _write_recoverable_workspace(tmp_path)
    log = tmp_path / "job.log"
    log.write_text(
        "[api 400] ValidationException: Invalid `signature` in `thinking` block\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(engine, "log_path", lambda _deck_id: log)

    info = jobs.resume_info(38, tmp_path)

    assert info["eligible"] is True
    assert info["provider_configuration_error"] is False
    assert info["recoverable_provider_context_error"] is True


def test_resume_info_uses_latest_provider_failure_episode(
    tmp_path,
    monkeypatch,
):
    _write_recoverable_workspace(tmp_path)
    log = tmp_path / "job.log"
    log.write_text(
        "[api 400] ValidationException: Operation not allowed\n"
        "child returned to the root\n"
        "[api failed] InternalServerError: HTTP 503 no available channel\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(engine, "log_path", lambda _deck_id: log)

    info = jobs.resume_info(37, tmp_path)

    assert info["eligible"] is True
    assert info["provider_configuration_error"] is False


def test_static_frontend_offers_resume_for_failed_safe_checkpoint():
    source = (
        Path(__file__).resolve().parents[1] / "static" / "app.js"
    ).read_text(encoding="utf-8")

    assert "ed.resumeEligible = p.resume_eligible === true" in source
    assert '(ed.resumeEligible && ["failed", "interrupted"].includes(status))' in source
    assert 'id="resume-btn">↻ 从检查点继续' in source
    assert 'id="retry-btn">从头重新生成' in source


def test_finalize_pauses_recoverable_provider_failure(tmp_path):
    (tmp_path / "result.json").write_text(
        json.dumps({"status": "failed", "exit_reason": "api_failed"}),
        encoding="utf-8",
    )
    row = {"id": 34, "seed_json": "{}", "status": "running"}
    recovery = {
        "eligible": True,
        "durable": True,
        "exit_reason": "api_failed",
    }
    with (
        mock.patch.object(jobs, "_load_deck", return_value=row),
        mock.patch.object(jobs, "resume_info", return_value=recovery),
        mock.patch.object(jobs, "_pause_recoverable_deck") as pause,
        mock.patch.object(jobs, "_fail_deck") as fail,
    ):
        jobs._finalize(34, tmp_path, rc=1)

    pause.assert_called_once_with(34, recovery)
    fail.assert_not_called()


def test_model_specific_pool_reaches_harness_without_shared_account_mix(tmp_path, monkeypatch):
    shared = tmp_path / "provider.env"
    shared.write_text("OPUS47_API_KEYS=first,second,third\nANTHROPIC_API_KEY=unrelated-shared\n")
    monkeypatch.setattr(service_config, "PROVIDER_ENV_PATH", shared)
    monkeypatch.delenv("SENSENOVA_OPUS47_API_KEYS", raising=False)
    model = {"label": "Opus 4.7", "backend": "anthropic", "engine_model": "claude-opus-4-7-thinking",
             "multimodal": True, "thinking_mode": "toggle", "thinking_transport": "anthropic",
             "thinking_effort": "high", "thinking_effort_levels": ("low", "medium", "high"),
             "api_key_env": "OPUS47_API_KEYS", "base_url": "https://provider.test"}
    env = engine.selection_env("opus-4-7", "mural-presenter-v1-harness", "mural-presenter-v1", model_config=model)
    assert env["MODEL_API_KEY"] == "first"
    assert env["MODEL_API_KEYS"] == "first,second,third"
    assert env["MODEL_API_KEY_FALLBACKS"] == "second,third"
    assert env["MODEL_CHANNEL_LABELS"] == "1,2,3"
    assert env["MODEL_BASE_URL"] == "https://provider.test"
    assert env["MODEL"] == "claude-opus-4-7-thinking"
    assert env["THINK_EFFORT"] == "high" and env["MODEL_TIMEOUT_S"] == "0"
    assert "unrelated-shared" not in repr(env)


def test_shared_anthropic_fallback_survives_harness_dotenv_override(
    tmp_path,
    monkeypatch,
):
    shared = tmp_path / "shared.env"
    shared.write_text(
        "\n".join(
            [
                "ANTHROPIC_API_KEY=primary",
                "ANTHROPIC_API_KEYS=primary,fallback",
                "ANTHROPIC_API_KEY_FALLBACKS=fallback",
                "ANTHROPIC_API_CHANNEL_LABELS=k,a",
                "ANTHROPIC_BASE_URL=https://gateway.example.test",
                "ENABLE_IMAGE_GEN=1",
                "OPENAI_API_KEY=image-key",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(service_config, "PROVIDER_ENV_PATH", shared)
    monkeypatch.setattr(engine, "DATA_DIR", tmp_path / "studio-data")
    monkeypatch.setattr(
        engine,
        "_anthropic_pool",
        lambda: ("https://gateway.example.test", ["primary", "fallback"]),
    )
    monkeypatch.setattr(
        engine,
        "_probe_key",
        lambda *_args: pytest.fail("v1 must not probe or reorder the pool during configuration"),
    )
    engine._KEY_CACHE.clear()

    model = {
        "label": "Teacher",
        "backend": "anthropic",
        "engine_model": "claude-opus-test",
        "multimodal": True,
        "thinking_mode": "toggle",
        "thinking_transport": "anthropic",
        "thinking_effort": "high",
        "deployment_managed": True,
        "api_key_env": "",
        "base_url": "",
    }
    env = engine.selection_env(
        "custom:test",
        "mural-presenter-v1-harness",
        "mural-presenter-v1",
        model_config=model,
    )
    persisted = engine._read_env_file(env["CLEAN_DOTENV_PATH"])

    assert env["ANTHROPIC_API_KEY"] == "primary"
    assert env["ANTHROPIC_API_KEYS"] == "primary,fallback"
    assert env["ANTHROPIC_API_KEY_FALLBACKS"] == "fallback"
    assert env["ANTHROPIC_API_CHANNEL_LABELS"] == "k,a"
    assert persisted["ANTHROPIC_API_KEY"] == "primary"
    assert persisted["ANTHROPIC_API_KEYS"] == "primary,fallback"
    assert persisted["ANTHROPIC_API_KEY_FALLBACKS"] == "fallback"
    assert persisted["ANTHROPIC_API_CHANNEL_LABELS"] == "k,a"
    assert env["MODEL_CHANNEL_BALANCE"] == "primary-first"
    assert env["CLEAN_DOTENV_PATH"] == str(shared)
    assert env["MODEL"] == "claude-opus-test"
    assert persisted["ENABLE_IMAGE_GEN"] == "1"


def test_builtin_opus_models_use_shared_teacher_pool():
    for model_key in (engine.ANTHROPIC_MODEL2_KEY, engine.ANTHROPIC_MODEL3_KEY):
        model = engine.MODELS[model_key]
        assert model["backend"] == "anthropic"
        assert model["api_key_env"] == ""
        assert model["base_url"] == ""


def test_v1_dispatch_uses_one_pool_without_probe_or_generated_dotenv(tmp_path, monkeypatch):
    shared = tmp_path / "shared.env"
    shared.write_text(
        "ANTHROPIC_BASE_URL=https://gateway.test\n"
        "ANTHROPIC_API_KEY=key-i\nANTHROPIC_API_KEY_FALLBACKS=key-l\n"
        "ANTHROPIC_API_CHANNEL_LABELS=i,l\nANTHROPIC_CHANNEL_BALANCE=primary-first\n"
    )
    monkeypatch.setattr(service_config, "PROVIDER_ENV_PATH", shared, raising=False)
    monkeypatch.setenv("MURAL_OPUS_POOL_ENV_PATH", str(tmp_path / "retired.env"))
    monkeypatch.setenv("SENSENOVA_ANTHROPIC_API_KEY_FALLBACKS", "key-j")
    monkeypatch.setattr(engine, "_KEY_CACHE", {"opus-test": {"key": "key-j", "ts": 1e20}})
    model = {"backend": "anthropic", "engine_model": "opus-test", "multimodal": True}
    with mock.patch.object(engine, "_probe_key") as probe, mock.patch.object(
        engine, "_write_anthropic_override_dotenv"
    ) as persist:
        env = engine.selection_env("custom:test", "mural-presenter-v1-harness",
                                   "mural-presenter-v1", model_config=model)
    assert env["MODEL_API_KEY"] == "key-i"
    assert env["MODEL_API_KEYS"] == "key-i,key-l"
    assert env["MODEL_API_KEY_FALLBACKS"] == "key-l"
    assert env["MODEL_CHANNEL_LABELS"] == "i,l"
    assert env["MODEL_CHANNEL_BALANCE"] == "primary-first"
    assert env["CLEAN_DOTENV_PATH"] == str(shared)
    probe.assert_not_called()
    persist.assert_not_called()


def test_removed_channel_is_never_returned_from_key_cache(monkeypatch):
    monkeypatch.setattr(engine, "_KEY_CACHE", {"opus-test": {"key": "retired-j", "ts": 1e20}})
    monkeypatch.setattr(engine, "_anthropic_pool", lambda: ("https://gateway.test", ["key-i", "key-l"]))
    with mock.patch.object(engine, "_probe_key", return_value=True) as probe:
        assert engine.resolve_anthropic_key("opus-test") == "key-i"
    assert probe.call_args.args[1] == "key-i"


@pytest.mark.parametrize("keys,private,eligible", [
    (["key-i", "key-l"], False, True),
    (["key-i", "retired-j"], False, False),
    ([], False, False),
    (["key-i", "key-l"], True, False),
])
def test_disabled_account_resume_requires_explicit_shared_pool_removal(tmp_path, monkeypatch, keys, private, eligible):
    import hashlib
    channel = "ch-j-" + hashlib.sha256(b"retired-j").hexdigest()[:8]
    _write_shared_v1_workspace(tmp_path, provider_error_kind="access_disabled")
    path = tmp_path / "_trace/live-events.jsonl"
    events = [json.loads(raw) for raw in path.read_text().splitlines()]
    events[-1]["payload"]["channel_id"] = channel
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    (tmp_path / "result.json").write_text(json.dumps({"status": "error", "exit_reason": "api_failed"}))
    monkeypatch.setattr(engine, "log_path", lambda _: tmp_path / "missing.log")
    model = "custom:private" if private else engine.ANTHROPIC_MODEL3_KEY
    monkeypatch.setattr(jobs, "_load_deck", lambda _: {"model": model})
    monkeypatch.setattr(engine, "_anthropic_pool", lambda: ("https://gateway.test", keys))
    info = jobs.resume_info(236, tmp_path)
    assert info["eligible"] is eligible
    assert info["provider_channel_retired"] is eligible
    assert info["provider_access_disabled"]


@pytest.mark.parametrize("retired", [
    "mural-presenter", "mural-presenter-creative", "mural-presenter-v0.2",
    "mural-presenter-v0.2-grouped", "mural-presenter-v0.3", "mural-presenter-v0.4",
    "mural-presenter-v0.5", "mural-presenter-v1.0", "visual-craft", "visual-craft-v3-speech",
    "sense-present-ppt-skill", "sense-present-standard", "zh", "en", "current",
])
def test_only_current_pair_can_be_selected_or_refreshed(retired):
    engine.refresh_external_skills(force=True)
    assert set(engine.SKILLS) == {"mural-presenter-v1"}
    assert set(engine.PIPELINES) == {"mural-presenter-v1-harness"}
    assert engine.canon_skill(retired) is None
    assert engine.canon_pipeline(retired + "-harness") is None
    assert engine.validate_selection("custom:test", retired + "-harness", retired,
        model_config={"backend": "openai", "engine_model": "27b", "multimodal": True}) == "未知 skill"
