"""Narrow coverage for rendering, inspection evidence, and durable web budgets."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

from PIL import Image
import pytest

from infer import build_inspect_handlers
from runtime.agent import _repeatable_evidence
from runtime.prompt_visibility import model_visible_messages, workflow_visible_payload
from runtime.tools import ToolCall, ToolContext, ToolExecutionError, WriteScope
from state.artifacts import ArtifactStore
from state.locking import page_render_lock
from state.workspace import WorkspaceState
from test_native_inspect import (
    _analyzer, _author_page, _group_workspace, _make_workspace, _renderer, _slide_context,
)
from test_web_extract_integration import _services


def test_same_page_render_serializes_independent_artifact_stores(tmp_path):
    workspace, first_store = _make_workspace(tmp_path)
    second_store = ArtifactStore(tmp_path, workspace_state=WorkspaceState(tmp_path))
    entered, release, second_started, second_capture = (threading.Event() for _ in range(4))

    def first_renderer(html, png, canvas):
        entered.set()
        assert release.wait(5)
        return _renderer(html, png, canvas)

    def second_renderer(html, png, canvas):
        second_capture.set()
        return _renderer(html, png, canvas)

    def second_render():
        second_started.set()
        return second_store.render_pages([1], second_renderer)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(first_store.render_pages, [1], first_renderer)
        try:
            assert entered.wait(5)
            second = pool.submit(second_render)
            assert second_started.wait(5)
            assert not second_capture.wait(.05)
        finally:
            release.set()
        assert first.result(timeout=5)[0] == second.result(timeout=5)[0]
    assert not second_capture.is_set()
    first_store.validate_render_receipt(workspace.page(1).page)


def test_different_pages_still_render_in_parallel(tmp_path):
    workspace, artifacts = _make_workspace(tmp_path, pages=(1, 2))
    _author_page(workspace, artifacts, 2, "<html><body>Second page</body></html>")
    captures = threading.Barrier(2)

    def renderer(html, png, canvas):
        captures.wait(timeout=5)
        return _renderer(html, png, canvas)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(artifacts.render_pages, [page], renderer) for page in (1, 2)]
        assert [future.result(timeout=5)[0].page for future in futures] == [1, 2]


def test_skill_child_borrows_parent_page_lock_without_releasing_it(tmp_path):
    scripts = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1/scripts"
    code = (
        "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
        "from _internal.workspace_policy import page_render_lock\n"
        "with page_render_lock(Path(sys.argv[2]), 1):\n"
        "    print('borrowed')\n"
    )
    with page_render_lock(tmp_path, 1) as lock:
        descriptor = lock.handle.fileno()
        environment = {**os.environ, "MURAL_PAGE_RENDER_LOCK_FD": str(descriptor)}
        result = subprocess.run([sys.executable, "-c", code, str(scripts), str(tmp_path)],
                                env=environment, pass_fds=(descriptor,), capture_output=True,
                                text=True, timeout=5, check=True)
        assert result.stdout.strip() == "borrowed"
        with lock.path.open("a+b") as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    with lock.path.open("a+b") as contender:
        fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_skill_rejects_inherited_lock_for_a_different_page(tmp_path, monkeypatch):
    scripts = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1/scripts"
    monkeypatch.syspath_prepend(str(scripts))
    from _internal.workspace_policy import page_render_lock as skill_lock

    with page_render_lock(tmp_path, 1) as lock:
        monkeypatch.setenv("MURAL_PAGE_RENDER_LOCK_FD", str(lock.handle.fileno()))
        with pytest.raises(ValueError, match="invalid inherited"):
            with skill_lock(tmp_path, 2):
                pytest.fail("a different page must not reuse the inherited lock")


@pytest.mark.parametrize("kind", ["asset", "group", "page"])
def test_new_questions_keep_distinct_purposes_but_share_pixel_budget(tmp_path, monkeypatch, kind):
    workspace, _artifacts, (inspect, commit) = _group_workspace(tmp_path, monkeypatch, inspected=False)
    context = replace(_slide_context(workspace, (1, 2)), read_scopes=(WriteScope("assets"),))
    asset = workspace.paths.root / "assets/photo.png"
    asset.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (80, 60), "white").save(asset)
    arguments = {"path": "assets/photo.png"} if kind == "asset" else {"page": [1, 2] if kind == "group" else 1}
    receipts = []
    payload = inspect(context, {**arguments, "review_stage": "final", "question": "Check the subject"})
    assert not payload["inspection_cached"]
    assert payload["pixels_delivered_to_this_owner"] is False
    assert commit(payload["pending_inspection"]) is True
    receipts.append(payload)
    assert len({receipt["scope_key"] for receipt in receipts}) == 1
    assert len({receipt["evidence_key"] for receipt in receipts}) == 1
    with pytest.raises(ToolExecutionError) as error:
        inspect(context, {**arguments, "review_stage": "final", "question": "One more rephrased review"})
    assert error.value.code == "inspection_limit_reached"
    cached = inspect(context, {**arguments, "review_stage": "final", "question": "Check the subject"})
    assert cached["inspection_cached"] and cached["pixels_delivered_to_this_owner"]
    assert not cached["pixels_attached_in_this_result"]
    replay = inspect(context, {**arguments, "review_stage": "final",
                               "question": "Reload pixels for the recovery question", "retain": True})
    assert replay["inspection_cached"] and replay["pixels_delivered_to_this_owner"]
    assert replay["checked_scope"] == "cached_pixels_replay_only"
    assert replay["review_stage"] == "final"
    assert "pending_inspection" not in replay
    assert replay["inspection_id"] == receipts[-1]["inspection_id"]
    assert commit(replay["current_inspection"]) is False
    assert workspace.inspection_attempt_count(kind=kind, owner_id=context.owner_id) == 1


def test_changed_asset_pixels_receive_a_fresh_inspection_budget(tmp_path):
    workspace, artifacts = _make_workspace(tmp_path)
    inspect, commit = build_inspect_handlers(workspace, artifacts, _renderer, _analyzer)
    context = ToolContext("image-owner", "image", read_scopes=(WriteScope("assets"),))
    asset = tmp_path / "assets/photo.png"
    asset.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (80, 60), "white").save(asset)
    result = inspect(context, {"path": "assets/photo.png", "question": "Question 0"})
    assert commit(result["pending_inspection"])
    Image.new("RGB", (80, 60), "blue").save(asset)
    current = inspect(context, {"path": "assets/photo.png", "question": "Check the replacement"})
    assert current["native_multimodal"] and not current["inspection_cached"]


def test_inspection_progress_identity_does_not_use_question_or_cache_scope():
    call = ToolCall("one", "inspect", {"page": 1, "question": "First purpose"})
    first = {"kind": "page", "pages": [1], "sha256": "pixels", "scope_key": "first-purpose"}
    second = {**first, "scope_key": "second-purpose", "inspection_cached": False}
    assert _repeatable_evidence(call, json.dumps(first)) == _repeatable_evidence(call, json.dumps(second))
    assert _repeatable_evidence(call, json.dumps(first))[1] is True
    assert _repeatable_evidence(call, json.dumps({**first, "sha256": "changed"}))[0] != _repeatable_evidence(call, json.dumps(first))[0]


def test_cached_owner_pixels_are_not_reattached_just_because_feedback_exists(tmp_path, monkeypatch):
    from test_inspection_transactions import (
        ScriptedTransport, _call, _fixture, _payload_and_parts, _response, _run,
    )

    _workspace, _artifacts, context, arguments, tools, _committed = _fixture(tmp_path, monkeypatch, "inspect")
    tools.inspection_feedback = lambda _context, _call, payload: {
        **payload, "critic_feedback": {"completed": True, "status": "pass_no_blockers", "body": "No blockers"}}
    transport = ScriptedTransport([_call("inspect", arguments), _call("inspect", arguments, "cached"), _response()])
    _run(transport, tools, context)
    payload, parts = _payload_and_parts(transport.requests[-1])
    assert payload["pixels_delivered_to_this_owner"] is True
    assert payload["pixels_attached_in_this_result"] is False
    assert not isinstance(parts, list) or not any(part.get("type") == "image" for part in parts)


def test_research_budget_and_source_progress_survive_runtime_recreation(tmp_path, monkeypatch):
    monkeypatch.setattr("runtime.production.RESEARCH_WEB_MAX_CALLS", 2)
    monkeypatch.setattr("runtime.production.RESEARCH_SUCCESSFUL_EXTRACT_TARGET", 1)
    owner = ToolContext("research-owner", "research")
    original = _services(tmp_path)
    original._research_web_begin(owner, "web_extract")
    original._research_web_success(owner, "web_extract", {"url": "https://source.example/evidence"})
    resumed = _services(tmp_path)
    resumed._research_web_begin(owner, "web_search")
    cue = resumed._research_web_success(owner, "web_search", {"results": []})
    assert cue["research_convergence"]["status"] == "sufficient_for_handoff"
    with pytest.raises(ToolExecutionError, match="Research") as error:
        _services(tmp_path)._research_web_begin(owner, "web_search")
    assert error.value.code == "research_web_budget_reached"
    _services(tmp_path)._research_web_begin(ToolContext("another-owner", "research"), "web_search")


def test_research_reserves_budget_even_if_process_stops_before_result(tmp_path, monkeypatch):
    monkeypatch.setattr("runtime.production.RESEARCH_WEB_MAX_CALLS", 1)
    owner = ToolContext("research-owner", "research")
    _services(tmp_path)._research_web_begin(owner, "web_search")
    with pytest.raises(ToolExecutionError) as error:
        _services(tmp_path)._research_web_begin(owner, "web_search")
    assert error.value.code == "research_web_budget_reached"


def test_legacy_research_budget_rebuild_counts_unique_results_once(tmp_path, monkeypatch):
    monkeypatch.setattr("runtime.production.RESEARCH_WEB_MAX_CALLS", 2)
    services = _services(tmp_path)
    def event(tool_id, *, ok=True, error=""):
        return {"event_type": "tool_result", "role": "research", "agent_id": "research-owner",
                "payload": {"name": "web_extract", "tool_use_id": tool_id, "ok": ok,
                            "error_code": error, "result": json.dumps({"url": "https://source.example/"})}}
    rows = [event("one"), event("one"), event("two", ok=False, error="web_extract_http_403"),
            event("budget", ok=False, error="research_web_budget_reached")]
    (services.workspace.paths.trace / "live-events.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    with pytest.raises(ToolExecutionError):
        services._research_web_begin(ToolContext("research-owner", "research"), "web_search")
    progress = services.workspace._read_state()["research_web_progress"]["research-owner"]
    assert progress["calls"] == 2 and progress["failures"] == 1


@pytest.mark.parametrize("cross_owner", [False, True])
def test_cached_receipt_explains_delivery_without_exposing_transport(cross_owner):
    payload = {"kind": "asset", "inspection_cached": True, "native_multimodal": False,
               "path": "assets/photo.png", "visual_source": {"path": "assets/photo.png", "transport_media_type": "image/png"}}
    if cross_owner:
        payload["cross_owner_review_reused"] = True
    else:
        payload["current_inspection"] = {"kind": "asset", "path": "assets/photo.png", "owner_id": "owner"}
    messages = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "review",
                                               "content": json.dumps(payload)}]}]
    visible = json.loads(model_visible_messages(messages)[0]["content"][0]["content"])
    assert visible["pixels_delivered_to_this_owner"] is (not cross_owner)
    assert not visible["pixels_attached_in_this_result"]
    assert visible["visual_payload_status"] == ("review_reused_from_other_owner" if cross_owner
                                                else "pixels_delivered_in_previous_model_turn")
    assert not {"inspection_cached", "native_multimodal"} & visible.keys()
    assert "transport_media_type" not in json.dumps(visible)


def test_terminal_and_verification_views_hide_internal_records_and_preserve_pairing():
    from export.training import _scrub_ephemeral_training_text

    outputs = {
        "terminal": {"returncode": 0, "stdout": 'status:PASS\n[deck-state] {"deck_graph":"_state/deck-graph.json"}\nplans:3'},
        "verification_submit": {"status": "issues_found", "scope": "deck", "verdict": "repair_required",
                                "issue_ids": ["issue-1"], "routes": [{"issue_id": "issue-1", "target": "slide",
                                "pages": [2], "owner_id": "private-owner", "activation_fingerprint": "f" * 64,
                                "reason": "structured_issue_route"}]},
    }
    messages = []
    for name, payload in outputs.items():
        messages.extend([{"role": "assistant", "content": [{"type": "tool_use", "id": name, "name": name, "input": {}}]},
                         {"role": "user", "content": [{"type": "tool_result", "tool_use_id": name, "content": json.dumps(payload)}]}])
    visible = model_visible_messages(messages)
    assert visible == _scrub_ephemeral_training_text(messages)
    text = json.dumps(visible)
    for internal in ("deck-state", "_state/deck-graph.json", "activation_fingerprint", "private-owner", "structured_issue_route"):
        assert internal not in text
    assert "status:PASS" in text and "plans:3" in text and "issue-1" in text
    assert visible[0] == messages[0] and visible[1]["content"][0]["tool_use_id"] == "terminal"


def test_workflow_view_keeps_actionable_recovery_refs_not_hashes_or_cursors():
    state = {"cursor": 42, "owner_id": "current-private-owner", "artifact_hashes": {
             "slides/slide_01.html": "a" * 64, "_state/workspace.json": "b" * 64},
             "tasks": [{"task_id": "task-1", "group_id": "body", "status": "failed", "owner_id": "failed-owner", "attempt": 2}],
             "pending_requests": [{"request_id": "request-1", "owner_id": "request-private-owner", "question": "Choose a source"}]}
    visible = workflow_visible_payload(state)
    assert visible["artifact_paths"] == ["slides/slide_01.html"]
    assert visible["tasks"][0]["handoff_from"] == "failed-owner"
    assert visible["pending_requests"][0]["request_id"] == "request-1"
    for internal in ("cursor", "attempt", "current-private-owner", "request-private-owner", "a" * 64, "b" * 64):
        assert internal not in json.dumps(visible)
    messages = [{"role": "user", "content": json.dumps({"harness_event": "orchestration_update", **state})}]
    assert "cursor" not in json.dumps(model_visible_messages(messages))
    from export.training import _scrub_ephemeral_training_text
    assert "failed-owner" in json.dumps(_scrub_ephemeral_training_text(messages))
