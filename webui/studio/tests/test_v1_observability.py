"""V1 的端到端投影：实际日志、实际前端函数，不调用真实 Provider。"""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess

import pytest

from app import engine, trace
from workspace_fixture import write_workspace_manifest

ROOT = Path(__file__).resolve().parents[3]

# inference/runtime.py 与 Harness 的 runtime 包同名，测试显式装载被测文件以隔离 import 顺序。
_spec = importlib.util.spec_from_file_location("v1_live_trace_test", ROOT / "harnesses/mural-presenter-v1/runtime/live_trace.py")
_live = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_live)
LiveEventJournal = _live.LiveEventJournal


def test_worker_discovery_is_scoped_to_exact_studio_job(tmp_path, monkeypatch):
    from app import jobs
    job = tmp_path / "studio-a/jobs/1.json"
    proc = tmp_path / "proc"
    rows = {
        101: ["python", "/source/serve_one.py", "--job", str(job)],
        102: ["python", "/source/serve_one.py", "--job", str(tmp_path / "studio-b/jobs/1.json")],
        103: ["python", "-c", f"serve_one.py --job {job}"],
        104: ["python", "/source/serve_one.py", "--job", str(job) + ".backup"],
    }
    for pid, args in rows.items():
        directory = proc / str(pid)
        directory.mkdir(parents=True)
        (directory / "cmdline").write_bytes(("\0".join(args) + "\0").encode())
    monkeypatch.setattr(engine, "JOBS_DIR", job.parent)
    monkeypatch.setattr(jobs, "Path", lambda value: proc if str(value) == "/proc" else Path(value))
    assert jobs._matching_engine_pids(1) == {101}


@pytest.mark.parametrize("prefix", ["", "plan/"])
def test_task_documents_fetch_and_link_the_enumerated_artifacts(tmp_path, prefix):
    write_workspace_manifest(tmp_path)
    expected = [prefix + "deck.md"]
    for rel in expected:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Document")
    if prefix:
        with pytest.raises(ValueError, match="unsupported_workspace_layout"):
            trace._workflow_artifacts(str(tmp_path), "running")
        return
    artifacts = trace._workflow_artifacts(str(tmp_path), "running")
    source = (ROOT / "webui/studio/static/app.js").read_text()
    functions = []
    for name in ("workflowDocumentSpecs", "workflowDocumentArtifactSignature", "loadWorkflowDocuments", "renderWorkflowTaskPane"):
        start = re.search(rf"^(?:async )?function {name}\(", source, re.M).start()
        end = re.search(r"^(?:async )?function ", source[start + 1:], re.M)
        functions.append(source[start:start + 1 + end.start()])
    setup = """
const ed = {id:1, fullQuery:'Request', workflow:{artifacts:ARTIFACTS}};
const host = {dataset:{}, innerHTML:''};
const $ = () => host, escapeHtml = s => s, agentMarkdownHtml = s => s;
const outlineBriefData = () => ({}), workflowArtifactUrl = file => '/files/' + file.rel;
const fetched = [];
const fetch = async url => { fetched.push(url.split('?')[0]); return {ok:true, text:async ()=>'# Document'}; };
""".replace("ARTIFACTS", json.dumps(artifacts))
    script = setup + "\n".join(functions) + "\nloadWorkflowDocuments(workflowDocumentArtifactSignature()).then(()=>console.log(JSON.stringify({fetched, html:host.innerHTML})));"
    result = subprocess.run(["node", "-e", script], check=True, text=True, capture_output=True)
    value = json.loads(result.stdout)
    assert set(value["fetched"]) == {"/files/" + rel for rel in expected}
    for rel in expected:
        assert f'href="/files/{rel}"' in value["html"]


def test_single_plan_drives_progress_and_page_context_without_sidecars(tmp_path, monkeypatch):
    from app import main
    from app.workspace_layout import WorkspaceLayout
    write_workspace_manifest(tmp_path)
    layout = WorkspaceLayout.load(tmp_path)
    assert layout.plan_pages() == [] and layout.plan_text(1) == ""
    assert trace.snapshot(str(tmp_path))["slides_total"] is None
    (tmp_path / "deck.md").write_text("# Topic\n## Visual Direction\nOne shared object\n")
    (tmp_path / "briefs").mkdir()
    (tmp_path / "briefs/slide_01.md").write_text("# P01 — First\n- job: first story\n")
    (tmp_path / "briefs/slide_02.md").write_text("# P02 — Second\n- must_show: second evidence\n")
    assert trace.snapshot(str(tmp_path))["slides_total"] == 2
    assert "second evidence" in layout.plan_text(2) and "first story" not in layout.plan_text(2)
    monkeypatch.setattr(main, "_own_deck", lambda *_: {"run_dir": str(tmp_path)})
    result = main.deck_slideinfo(1, 2, user={"id": 1}, con=None)
    assert result["exists"] and "second evidence" in result["html"]
    assert "first story" not in result["html"]
    assert main.deck_slideinfo(1, 3, user={"id": 1}, con=None) == {"exists": False}
    assert len(list((tmp_path / "briefs").glob("*.md"))) == 2
    assert not (tmp_path / "design-brief.md").exists()


def test_scaffold_is_not_authored_and_image_stage_is_not_slide_rendering(tmp_path):
    write_workspace_manifest(tmp_path)
    (tmp_path / "_trace").mkdir()
    state = tmp_path / "_state/workspace.json"
    value = json.loads(state.read_text())
    value["pages"] = {"1": {"authored_html_sha256": ""}, "2": {
        "authored_html_sha256": hashlib.sha256(b"<section>Authored content</section>").hexdigest()}}
    state.write_text(json.dumps(value))
    (tmp_path / "slides").mkdir()
    for number in (1, 2):
        (tmp_path / f"slides/slide_{number:02d}.html").write_text(
            "<section></section>" if number == 1 else "<section>Authored content</section>")
    (tmp_path / "_state/owners.json").write_text(json.dumps({"schema": "mural.owner-registry.v2", "owners": {
        "image": {"role": "image", "status": "running"},
        "slide": {"role": "slide", "status": "waiting"},
    }}))
    (tmp_path / "_state/orchestration.json").write_text(json.dumps({"schema": "mural.orchestration.v2", "tasks": {
        "images": {"role": "image", "status": "running"},
        "page": {"role": "slide", "status": "waiting_dependency"},
    }}))
    progress = trace.snapshot(str(tmp_path), status="running")
    assert progress["authored"] == [2]
    assert progress["slides_authored"] == progress["slides_scaffolded"] == 1
    assert progress["task_stage"] == progress["phase"] == "designing"
    assert progress["workers"] == 1


def test_runtime_card_displays_task_states_without_wave(tmp_path):
    source = (ROOT / "webui/studio/static/app.js").read_text()
    start = source.index("function syncMuralRuntimeCard(")
    end = source.index("\nfunction ", start + 1)
    code = "const card={dataset:{},title:'',b:{},i:{},querySelector(s){return this[s]}};const $=()=>card;\n"
    code += source[start:end] + '\nsyncMuralRuntimeCard({mural_runtime:{schema:"mural.webui-runtime-projection.v1",state_counts:{active:2,waiting:1,blocked:1,terminal:3}}});console.log(JSON.stringify(card));'
    result = subprocess.run(["node", "-e", code], check=True, capture_output=True, text=True)
    card = json.loads(result.stdout)
    assert card["b"]["textContent"] == "MURAL 任务状态"
    assert card["i"]["textContent"] == "2 运行 · 1 等待 · 1 阻塞 · 3 已闭合"
    assert "Wave" not in result.stdout


@pytest.mark.parametrize("own_pages", [[], [1], [1, 3]])
def test_direct_author_owner_is_projected_without_fake_slide_tasks(tmp_path, own_pages):
    write_workspace_manifest(tmp_path)
    (tmp_path / "_state/owners.json").write_text(json.dumps({"schema": "mural.owner-registry.v2", "owners": {
        "orchestrator": {"owner_id": "orchestrator", "role": "orchestrator", "pages": own_pages, "status": "running"},
        "group": {"owner_id": "owner-group", "role": "slide", "group_id": "group", "pages": [2], "status": "running"},
    }}))
    (tmp_path / "_state/orchestration.json").write_text(json.dumps({"schema": "mural.orchestration.v2", "tasks": {
        "p2": {"task_id": "p2", "owner_id": "owner-group", "role": "slide", "pages": [2], "status": "running"},
    }}))
    journal = LiveEventJournal(tmp_path / "_trace/live-events.jsonl")
    common = dict(agent_id="orchestrator", role="orchestrator", turn=1, segment_id="main")
    journal.append(event_type="agent_started", payload={}, **common)
    journal.append(event_type="tool_use", payload={"name": "inspect", "tool_use_id": "review-p2",
        "arguments": {"page": 2}}, **common)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    live = trace.live_events(str(tmp_path))
    assert live["page_agents"] == {**{str(p): "orch" for p in own_pages}, "2": "slide_group_group"}
    assert live["agent_timings"]["orch"]["pages"] == own_pages
    assert live["agent_timings"]["orch"]["role"] == "orchestrator"
    assert "orchestrator" not in live["agent_keys"]
    assert set(live["agents"]) == {"orch"}
    runtime = trace._shared_v1_runtime(str(tmp_path))
    assert [row["task_id"] for row in runtime["tasks"]] == ["p2"]
    assert runtime["task_stage"] == "rendering"
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_direct_author_pages_do_not_come_from_planning_config(tmp_path):
    write_workspace_manifest(tmp_path)
    (tmp_path / "_state/page-index.json").write_text(json.dumps({"pages": [
        {"page_uid": "p1", "position": 1}, {"page_uid": "p2", "position": 2}]}))
    directory = tmp_path / "_trace/orchestrator"
    directory.mkdir(parents=True)
    (directory / "config.json").write_text(json.dumps({"role": "orchestrator", "owner_id": "orchestrator",
        "assigned_page_uids": ["p1", "p2"]}))
    value = trace._workflow_payload(str(tmp_path), {"1": "orch", "2": "slide_group_other"}, {}, {}, "running")
    run, = value["runs"]
    assert run["key"] == "orch" and run["role"] == "orchestrator"
    assert run["owner_id"] == "orchestrator" and run["pages"] == [1]


def test_direct_author_runtime_has_no_delegated_tasks(tmp_path):
    write_workspace_manifest(tmp_path)
    (tmp_path / "_state/owners.json").write_text(json.dumps({"schema": "mural.owner-registry.v2", "owners": {
        "orchestrator": {"owner_id": "orchestrator", "role": "orchestrator", "pages": [1], "status": "running"}}}))
    (tmp_path / "_state/orchestration.json").write_text(json.dumps({"schema": "mural.orchestration.v2", "tasks": {}}))
    assert trace._shared_v1_runtime(str(tmp_path))["tasks"] == []
    live = trace.live_events(str(tmp_path))
    assert live["page_agents"] == {"1": "orch"} and live["agent_keys"] == ["orch"]


def test_direct_author_page_name_requires_actual_page_ownership():
    from test_startup_observability import _js

    result = _js(["agentPages", "pageAgentLabel", "pageAgentDisplayName"], """
console.log(JSON.stringify([1,2].map(page=>({name:pageAgentDisplayName('orch',page),note:pageAgentLabel('orch',page)}))));
""", setup="const ed={pageAgents:{'1':'orch','2':'slide_group_other'}};")
    assert result[0] == {"name": "主 Agent 制作", "note": "由主 Agent 直接负责本页制作与修订"}
    assert result[1] == {"name": "MURAL Presenter", "note": "整册统筹与页面复核"}


@pytest.mark.parametrize("pages,label", [([1], "第 1 页制作"), ([2, 3], "页面组协作"), ([], "页面制作")])
def test_single_and_group_labels_follow_actual_pages_not_internal_owner_key(pages, label):
    source = (ROOT / "webui/studio/static/app.js").read_text()
    functions = []
    for name in ("canonicalOrchestrationAgentKey", "agentPages", "orchestrationAgentMeta",
                 "actText", "pageAgentLabel", "pageAgentDisplayName"):
        start = re.search(rf"^function {name}\(", source, re.M).start()
        end = re.search(r"^function ", source[start + 1:], re.M)
        functions.append(source[start:start + 1 + end.start()])
    setup = "const ed={pageAgents:" + json.dumps({str(n): "slide_group_owner" for n in pages}) + "};"
    setup += 'const pad2=n=>String(n).padStart(2,"0");'
    script = setup + "\n".join(functions) + """
console.log(JSON.stringify({meta:orchestrationAgentMeta('slide_group_owner'),
  activity:actText({activity:{agent:'slide_group_owner',tool:'write_file'}}),
  note:pageAgentLabel('slide_group_owner',1),name:pageAgentDisplayName('slide_group_owner',1)}));
"""
    output = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    value = json.loads(output.stdout)
    assert value["meta"]["key"] == "slide_group_owner"
    assert value["meta"]["pages"] == pages and value["meta"]["label"] == label
    assert value["activity"].startswith(label)
    assert ("页面组" in value["name"]) == (len(pages) > 1)
    assert ("统一设计" in value["note"]) == (len(pages) > 1)


def test_real_content_journal_visible_before_completion_and_across_reviews(tmp_path, monkeypatch):
    write_workspace_manifest(tmp_path)
    journal = LiveEventJournal(tmp_path / "_trace/live-events.jsonl")
    for index in (1, 2):
        common = dict(agent_id="content-qc", role="content-audit", turn=0, segment_id=f"review-{index}")
        journal.append(event_type="content_qc_started", payload={"reviewer_model":"27b"}, **common)
        running = trace.live_events(str(tmp_path))
        assert running["agent_timings"]["content-audit"]["status"] == "running"
        assert running["agent_timings"]["content-audit"]["finished_at"] is None
        assert running["events"][-1]["s"].startswith("内容审计开始")
        journal.append(event_type="content_qc_completed", payload={"status":"completed", "passed":True, "elapsed_s":.1}, **common)
        completed = trace.live_events(str(tmp_path), cursor=running["next_cursor"])
        assert len(completed["events"]) == 1
        assert completed["agent_timings"]["content-audit"]["status"] == "complete"
    segments = trace.agent_trace_payload(str(tmp_path), "content-audit")["segments"]
    assert len(segments) == 2 and all(row["finished_at"] for row in segments)
    assert journal.sequence == 4


def test_new_runtime_does_not_take_resource_capacity_from_model_or_legacy_environment(tmp_path, monkeypatch):
    shared = str(tmp_path / "service-slots")
    monkeypatch.setenv("MURAL_RESOURCE_SLOTS_DIR", shared)
    env = engine.selection_env("custom:test", "mural-presenter-v1-harness", "mural-presenter-v1",
        model_config={"label":"test", "backend":"openai", "engine_model":"27b",
                      "base_url":"https://provider.test/v1", "api_key":"test-only", "multimodal":True,
                      "slide_concurrency": 99})
    assert "MURAL_RESOURCE_SLOTS_DIR" not in env
    assert "SLIDE_CONCURRENCY" not in env


def test_parallel_critic_results_are_visible_and_do_not_finish_other_reviews(tmp_path, monkeypatch):
    write_workspace_manifest(tmp_path)
    journal = LiveEventJournal(tmp_path / "_trace/live-events.jsonl")
    common = dict(agent_id="external-visual-critic", role="critic", turn=0, segment_id="")
    for review in ("a", "b"):
        journal.append(event_type="critic_started", payload={"inspection_id": review}, **common)
    for review, status in (("a", "running"), ("b", "complete")):
        journal.append(event_type="critic_completed",
            payload={"inspection_id": review, "completed": True, "body": "文字可以读清。"}, **common)
        live = trace.live_events(str(tmp_path))
        assert live["agent_timings"]["critic"]["status"] == status
        assert bool(live["agent_timings"]["critic"]["finished_at"]) == (status == "complete")
        assert live["events"][-1]["critic"]["body"] == "文字可以读清。"


def test_cached_critic_binding_is_not_displayed_as_another_model_call(tmp_path):
    write_workspace_manifest(tmp_path)
    journal = LiveEventJournal(tmp_path / "_trace/live-events.jsonl")
    journal.append(event_type="critic_completed", agent_id="external-visual-critic", role="critic",
        turn=0, segment_id="", payload={"inspection_id": "current", "completed": True,
                                      "reused_from": {"inspection_id": "earlier"}, "cache_hit": True})
    event = trace.live_events(str(tmp_path))["events"][0]
    assert "同版像素，未发起新请求" in event["s"]
    assert "未进行 Owner 处置" not in event["s"]


def test_critic_execution_failure_remains_failed_without_a_semantic_verdict(tmp_path):
    write_workspace_manifest(tmp_path)
    journal = LiveEventJournal(tmp_path / "_trace/live-events.jsonl")
    common = dict(agent_id="critic-independent", role="critic", turn=0, segment_id="critic-only")
    journal.append(event_type="critic_started", payload={"inspection_id": "failed"}, **common)
    journal.append(event_type="critic_failed", payload={"inspection_id": "failed", "completed": False,
        "body": "", "incomplete_reason": "Provider timeout"}, **common)
    live = trace.live_events(str(tmp_path))
    assert live["agent_timings"]["critic"]["status"] == "failed"
    assert "执行未完成" in live["events"][-1]["s"]
    assert live["events"][-1]["critic"]["incomplete_reason"] == "Provider timeout"


def test_inflight_critic_reuse_waits_without_starting_another_request(tmp_path):
    write_workspace_manifest(tmp_path)
    journal = LiveEventJournal(tmp_path / "_trace/live-events.jsonl")
    common = dict(agent_id="external-visual-critic", role="critic", turn=0, segment_id="")
    journal.append(event_type="critic_started", payload={"inspection_id": "source"}, **common)
    journal.append(event_type="critic_reused", payload={"inspection_id": "reused", "pending": True,
        "reused_from_inspection_id": "source"}, **common)
    pending = trace.live_events(str(tmp_path))
    assert [event["event_type"] for event in pending["events"]] == ["critic_started", "critic_reused"]
    assert "等待公开反馈" in pending["events"][-1]["s"]
    assert "未发起新请求" in pending["events"][-1]["s"]
    for review, status in (("source", "running"), ("reused", "complete")):
        journal.append(event_type="critic_completed", payload={"inspection_id": review, "completed": True,
            "body": "文字可以读清。", "reused_from": {"inspection_id": "source"} if review == "reused" else {}}, **common)
        live = trace.live_events(str(tmp_path))
        assert live["agent_timings"]["critic"]["status"] == status
        assert bool(live["agent_timings"]["critic"]["finished_at"]) == (status == "complete")
    assert sum(event["event_type"] == "critic_started" for event in live["events"]) == 1
    assert {"critic_stale", "critic_reconciled"}.isdisjoint(trace._LIVE_EVENT_TYPES)


def test_workspace_resume_closes_critic_waiting_in_the_ui_without_fake_feedback(tmp_path):
    write_workspace_manifest(tmp_path)
    journal = LiveEventJournal(tmp_path / "_trace/live-events.jsonl")
    common = dict(agent_id="external-visual-critic", role="critic", turn=0, segment_id="")
    journal.append(event_type="critic_started", payload={"inspection_id": "source", "owner_id": "owner-1",
        "tool_use_id": "inspect-1", "png_sha256": "f" * 64}, **common)
    journal.append(event_type="critic_reused", payload={"inspection_id": "reused", "owner_id": "owner-2",
        "tool_use_id": "inspect-2", "png_sha256": "f" * 64, "pending": True,
        "reused_from_inspection_id": "source"}, **common)
    running = trace.live_events(str(tmp_path))
    assert running["agent_timings"]["critic"]["status"] == "running"
    resumed = LiveEventJournal(journal.path)
    assert len(resumed.close_interrupted_events()) == 2
    failed = trace.live_events(str(tmp_path), cursor=running["next_cursor"])
    assert failed["agent_timings"]["critic"]["status"] == "failed"
    assert failed["agent_timings"]["critic"]["finished_at"]
    assert [event["event_type"] for event in failed["events"]] == ["critic_failed", "critic_failed"]
    assert all("process_interrupted" in event["critic"]["incomplete_reason"] for event in failed["events"])
    assert resumed.close_interrupted_events() == ()
    assert trace.live_events(str(tmp_path), cursor=failed["next_cursor"])["events"] == []


def test_runtime_hash_ignores_broken_environment_link(tmp_path):
    (tmp_path / "main.py").write_text("pass\n")
    baseline = hashlib.sha256()
    engine._hash_runtime_tree(baseline, tmp_path, prefix="harness/")
    (tmp_path / ".venv").symlink_to(tmp_path / "missing-environment")
    actual = hashlib.sha256()
    engine._hash_runtime_tree(actual, tmp_path, prefix="harness/")
    assert actual.hexdigest() == baseline.hexdigest()


def test_process_view_distinguishes_content_audit_and_page_critic(tmp_path, monkeypatch):
    write_workspace_manifest(tmp_path)
    journal = LiveEventJournal(tmp_path / "_trace/live-events.jsonl")
    for event_type, role, payload in [
        ("critic_started", "critic", {"inspection_id": "a"}),
        ("critic_completed", "critic", {"inspection_id": "a", "completed": True, "body": "标题重叠，建议核对。"}),
        ("content_qc_completed", "content-audit", {"status": "completed", "passed": True}),
    ]:
        journal.append(event_type=event_type, role=role, agent_id=role, turn=0, segment_id="", payload=payload)
    projected = trace.live_events(str(tmp_path))["events"]
    feed = {}
    for event in projected:
        feed.setdefault(event["agent"], []).append(event)
    source = (ROOT / "webui/studio/static/app.js").read_text()
    functions = []
    for name in ("canonicalOrchestrationAgentKey", "orchestrationAgentMeta", "orchestrationFlatFeed", "orchestrationEntries"):
        start = re.search(rf"^function {name}\(", source, re.M).start()
        end = re.search(r"^function ", source[start + 1:], re.M)
        functions.append(source[start:start + 1 + end.start()])
    start = source.index("const ORCHESTRATION_ROLE_LABELS =")
    labels = source[start:source.index("\n};", start) + 3]
    setup = "const ed={specialistArtifacts:{agents:{}}}; const agentTimingFor=()=>null, orchestrationEventTime=()=>null, cleanAgentText=s=>s, agentPages=()=>[];"
    script = setup + labels + "\n".join(functions) + "\nconsole.log(JSON.stringify(orchestrationEntries(" + json.dumps(feed) + ").entries));"
    output = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    entries = json.loads(output.stdout)
    assert len(entries) == 2
    assert entries[0]["type"] == "critic-review"
    assert entries[1]["meta"]["label"] == "内容审计"
    assert entries[1]["type"] == "content-audit"
    assert feed["critic"][-1]["critic"]["body"] == "标题重叠，建议核对。"
