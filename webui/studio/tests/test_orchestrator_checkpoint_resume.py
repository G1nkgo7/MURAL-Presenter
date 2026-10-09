"""无 Owner 的 Orchestrator 只能从私有闭合 checkpoint 继续，不能重做已写文件。"""
import asyncio
import hashlib
import json
import stat

import pytest

from app import jobs, main
from test_resource_queue_resume import _run_entry, queue_case
from test_delivery_download import download_case


def _checkpoint(root):
    digest = hashlib.sha256(b"orchestrator").hexdigest()[:24]
    return root / "_state/owner-context" / f"{digest}.json"


@pytest.mark.parametrize("interruption", [False, True])
def test_queue_after_planning_resumes_real_context_without_duplicate_writes(queue_case, interruption):
    root = queue_case.root
    failed = _run_entry(queue_case, allowed_calls=2, interruption=interruption)
    assert failed["result"]["exit_reason"] == ("process_interrupted" if interruption else "resource_queue_timeout")
    assert json.loads((root / "_state/orchestration.json").read_text())["tasks"] == {}
    info = jobs.resume_info(1, root)
    assert info["eligible"] and info["durable"]
    assert info["trace_source"] == str(_checkpoint(root).relative_to(root))
    checkpoint = json.loads(_checkpoint(root).read_text())
    assert checkpoint["schema"] == "mural.orchestrator-context.v1"
    assert stat.S_IMODE(_checkpoint(root).stat().st_mode) == 0o600
    assert "private-test-signature" in _checkpoint(root).read_text()
    before = (root / "_trace/live-events.jsonl").read_bytes()
    original = (root / "deck.md").read_bytes()
    original_mtime = (root / "deck.md").stat().st_mtime_ns
    resumed = _run_entry(queue_case, allowed_calls=1, resume=True)
    assert resumed["observed"] == ["ResourceSlotTimeout"]
    assert resumed["provider_calls"] == 1
    assert (root / "speech.md").read_text() == "# 从闭合上下文继续"
    assert (root / "deck.md").read_bytes() == original
    assert (root / "deck.md").stat().st_mtime_ns == original_mtime
    assert (root / "_trace/live-events.jsonl").read_bytes().startswith(before)
    events = [json.loads(row) for row in (root / "_trace/tool-events.jsonl").read_text().splitlines()]
    for name in ("read_file", "write_file"):
        uses = [row for row in events if row["event_type"] == "tool_use" and row["tool_name"] == name]
        assert len(uses) == (1 if name == "read_file" else 2)
    public = "".join(path.read_text() for path in (root / "_trace").glob("*.jsonl"))
    agents = [json.loads(line) for line in (root / "_trace/agent-events.jsonl").read_text().splitlines()]
    assert [event["turn"] for event in agents if event["event_type"] == "agent_turn_completed"] == [1, 2, 3]
    providers = [json.loads(line) for line in (root / "_trace/provider-events.jsonl").read_text().splitlines()]
    successes = [event for event in providers if event["event_type"] == "provider_attempt_succeeded"]
    assert len(successes) == len({event["attempt_id"] for event in successes}) == 3
    assert "private-test-signature" not in public
    live_events = [json.loads(line) for line in (root / "_trace/live-events.jsonl").read_text().splitlines()]
    assert any("private test thinking" in json.dumps(event["payload"]) for event in live_events
               if event["event_type"] == "agent_context")
    assert all("private test thinking" not in json.dumps(event["payload"]) for event in live_events
               if event["event_type"] == "assistant_message")
    assert "private-test-signature" not in (root / "_state/skill-runtime.json").read_text()


@pytest.mark.parametrize("defect", ["missing", "foreign_deck", "query", "artifact", "open_tool", "late_tool"])
def test_planning_resume_rejects_missing_stale_or_open_checkpoint(queue_case, defect):
    root = queue_case.root
    _run_entry(queue_case, allowed_calls=2)
    path = _checkpoint(root)
    value = json.loads(path.read_text())
    if defect == "missing":
        path.unlink()
    elif defect == "artifact":
        (root / "deck.md").write_text("人为改写，不应被冷启动覆盖")
    elif defect == "late_tool":
        with (root / "_trace/live-events.jsonl").open("a") as stream:
            stream.write(json.dumps({"schema": "mural.live-event.v1", "event_type": "tool_use",
                "sequence": value["live_cursor"] + 100, "event_id": "not-checkpointed",
                "timestamp": "2026-09-07T00:00:00Z", "agent_id": "orchestrator", "role": "orchestrator",
                "turn": 3, "segment_id": value["segment"]["segment_id"],
                "payload": {"tool_use_id": "not-checkpointed", "name": "write_file", "arguments": {}}}) + "\n")
    else:
        if defect == "foreign_deck":
            value["identity"]["deck_id"] = "another-deck"
        elif defect == "query":
            value["identity"]["raw_user_query"] = "another query"
        else:
            value["segment"]["messages"].pop()
        path.write_text(json.dumps(value))
    assert not jobs.resume_info(1, root)["eligible"]
    if defect in {"missing", "artifact"}:
        existing = (root / "deck.md").read_bytes()
        refused = _run_entry(queue_case, allowed_calls=1, resume=True)
        assert refused["provider_calls"] == 0
        assert (root / "deck.md").read_bytes() == existing


def test_changed_attachment_is_rejected_before_staging_or_any_model_call(queue_case):
    source = queue_case.root.parent / "source.txt"
    source.write_text("原始材料，必须保留")
    first = _run_entry(queue_case, allowed_calls=2)
    assert first["result"]["exit_reason"] == "resource_queue_timeout"
    manifest = (queue_case.root / "_trace/attachment-manifest.json").read_bytes()
    original = queue_case.root / json.loads(manifest)["attachments"][0]["original_path"]
    original_bytes = original.read_bytes()
    assert not (queue_case.root / "material.md").exists()
    source.write_text("上游文件已替换，不能混入旧模型上下文")
    refused = _run_entry(queue_case, allowed_calls=1, resume=True)
    assert refused["provider_calls"] == 0
    assert refused["observed"] == ["SkillRuntimeError"]
    assert refused["result"]["exit_reason"] == "runtime_error"
    assert original.read_bytes() == original_bytes
    assert (queue_case.root / "_trace/attachment-manifest.json").read_bytes() == manifest


@pytest.mark.parametrize("orchestration", [
    {"schema": "unsupported", "tasks": {}},
    {"schema": "mural.orchestration.v2", "tasks": {"already-submitted": {}}},
    {"schema": "mural.orchestration.v2", "tasks": []},
])
def test_early_checkpoint_does_not_hide_invalid_or_delegated_state(queue_case, orchestration):
    _run_entry(queue_case)
    (queue_case.root / "_state/orchestration.json").write_text(json.dumps(orchestration))
    assert not jobs.resume_info(1, queue_case.root)["eligible"]


@pytest.mark.parametrize("route", ["file?rel=_state/owner-context/private.json",
                                  "files/_state/owner-context/private.json",
                                  "file?rel=assets/checkpoint-alias.json",
                                  "files/assets/checkpoint-alias.json"])
def test_private_checkpoint_is_not_a_webui_download(download_case, route):
    root = download_case["root"]
    path = root / "_state/owner-context/private.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"signature":"private-test-signature"}')
    (root / "assets").mkdir(exist_ok=True)
    (root / "assets/checkpoint-alias.json").symlink_to(path)
    async def request():
        messages = []
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(message):
            messages.append(message)
        path, _, query = f"/api/decks/1/{route}".partition("?")
        await main.app({"type": "http", "asgi": {"version": "3.0"}, "method": "GET",
                        "path": path, "raw_path": path.encode(), "root_path": "", "scheme": "http",
                        "query_string": query.encode(), "headers": [], "client": ("test", 1),
                        "server": ("test", 80), "http_version": "1.1"}, receive, send)
        return next(row["status"] for row in messages if row["type"] == "http.response.start"), b"".join(
            row.get("body", b"") for row in messages if row["type"] == "http.response.body")
    status, body = asyncio.run(request())
    assert status == 403
    assert b"private-test-signature" not in body
