"""协作概览读取执行证据，不把阶段展示变成另一套调度状态。"""
import json

import pytest

from test_startup_observability import _js


FUNCTIONS = ["workflowExecutionStatus", "workflowCurrentActivity", "workflowTaskGraph", "workflowOverview",
             "workflowStatusLabel", "runtimeEventState", "liveToolTransactions", "attachmentParseLabel"]


def overview(state):
    return _js(FUNCTIONS, """
const graph = workflowTaskGraph(state.muralRuntime?.tasks || [], state.agentTimings || {}, state.feed || {}, state.status);
console.log(JSON.stringify({graph, ...workflowOverview(graph, state)}));
""", setup=f"const state = {json.dumps(state)};\n")


def test_no_delegation_still_has_real_coordinator_and_no_phantom_activity():
    result = overview({"status": "running", "fullQuery": "用户的真实任务", "muralRuntime": {"tasks": []}})
    assert result["query"] == "用户的真实任务"
    assert result["coordinator"]["status"] == "unknown"
    assert result["active"] == result["delegated"] == result["graph"]["edges"] == []
    assert "尚无启动记录" in result["coordinator"]["detail"]


def test_direct_author_uses_main_card_not_a_synthetic_slide_task():
    result = overview({"status": "running", "pageAgents": {"1": "orch"},
        "feed": {"orch": [{"event_type": "agent_started"}]}, "muralRuntime": {"tasks": []}})
    assert result["graph"]["nodes"] == result["graph"]["edges"] == result["delegated"] == []
    assert result["coordinator"]["role"] == "orchestrator"
    assert result["coordinator"]["agentKey"] == "orch" and result["coordinator"]["pages"] == [1]
    assert result["coordinator"]["title"] == "主 Agent 制作 · 整册统筹"
    assert result["coordinator"]["progress"] == "自制 1 页 · 无子任务"
    page_phase = next(row for row in result["phases"] if row["id"] == "slide")
    assert page_phase["status"] == "observed"
    assert "主 Agent 制作：P1" in page_phase["detail"] and "不代表检查完成" in page_phase["detail"]


def test_mixed_ownership_does_not_turn_reviewed_pages_into_main_authorship():
    result = overview({"status": "running", "pageAgents": {"1": "orch", "2": "slide_group_pair", "3": "slide_group_pair"},
        "muralRuntime": {"tasks": [{"task_id": "pair", "role": "slide", "pages": [2, 3], "status": "completed"}]},
        "feed": {"orch": [{"event_type": "agent_started"}, {"k": "tool", "tool": "inspect_group",
            "tool_use_id": "review", "args": {"pages": [2, 3]}}]}})
    assert result["coordinator"]["pages"] == [1]
    assert result["coordinator"]["progress"] == "自制 1 页 · 委派任务完成 1 / 1"
    page_phase = next(row for row in result["phases"] if row["id"] == "slide")
    assert page_phase["status"] == "observed" and "主 Agent 制作：P1；" in page_phase["detail"]
    assert result["graph"]["nodes"][0]["pages"] == [2, 3]


def test_review_events_alone_do_not_imply_direct_authorship():
    result = overview({"status": "running", "feed": {"orch": [{"k": "tool", "tool": "inspect",
        "tool_use_id": "review", "args": {"page": 1}}, {"k": "text", "s": "我来制作 slide_01.html"}]}})
    assert result["coordinator"]["pages"] == []
    assert "主 Agent 制作" not in result["coordinator"]["title"]
    assert next(row for row in result["phases"] if row["id"] == "slide")["status"] == "unknown"


def test_direct_author_making_rows_use_registry_not_main_lifecycle():
    result = _js(["workflowExecutionStatus", "workflowStatusLabel", "muralMakingView"], """
console.log(JSON.stringify(muralMakingView()));
""", setup="""
const ed={status:'running',total:3,rendered:new Set([1]),pageAgents:{'1':'orch','2':'slide_group_pair'},
  feed:{orch:[{event_type:'agent_completed'}]},muralRuntime:{tasks:[{role:'slide',pages:[2],status:'running'}]}};
const isStaticActiveStatus=status=>status==='running';
""")
    first, delegated, unassigned = result["rows"]
    assert first == {"page": 1, "state": "observed", "preview": True, "label": "主 Agent 制作 · 已有预览"}
    assert delegated["state"] == "running" and unassigned["label"] == "等待页面任务"
    assert result["previews"] == 1


@pytest.mark.parametrize("artifact_status,expected", [("complete", "complete"), ("", "observed")])
def test_direct_author_completion_requires_verified_delivery(artifact_status, expected):
    state = {"status": "completed", "artifactStatus": artifact_status, "pageAgents": {"1": "orch"}}
    result = overview(state)
    phase = next(row for row in result["phases"] if row["id"] == "slide")
    assert phase["status"] == expected
    result = _js(["workflowExecutionStatus", "workflowStatusLabel", "muralMakingView"], """
console.log(JSON.stringify(muralMakingView()));
""", setup="const ed=" + json.dumps({**state, "total": 1}) + ";const isStaticActiveStatus=()=>false;")
    assert result["rows"][0]["state"] == expected
    assert ("交付已验证" in result["rows"][0]["label"]) == (expected == "complete")


def test_live_feed_does_not_guess_or_override_page_ownership():
    from test_startup_observability import JS_PATH

    source = JS_PATH.read_text()
    assert "reconcilePageAgentOwners" not in source
    assert 'ed.pageAgents = { ...(payload.page_agents || {}) };' in source


@pytest.mark.parametrize("event,status", [("agent_started", "running"),
    ("agent_completed", "complete"), ("agent_failed", "failed"),
    ("resource_wait_started", "resource_wait"), ("resource_wait_failed", "failed")])
def test_coordinator_uses_lifecycle_and_resource_evidence(event, status):
    result = overview({"status": "running", "feed": {"orch": [
        {"event_type": "agent_started"}, {"event_type": event}]}})
    assert result["coordinator"]["status"] == status


def test_stopped_run_does_not_leave_old_started_event_running():
    result = overview({"status": "interrupted", "feed": {"orch": [{"event_type": "agent_started"}]},
                       "muralRuntime": {"tasks": [{"task_id": "p", "role": "slide", "status": "running"}]}})
    assert result["coordinator"]["status"] == "interrupted"
    assert "尚无 Orchestrator 完成记录" in result["coordinator"]["detail"]
    assert result["graph"]["nodes"][0]["status"] == "unknown"
    assert result["active"] == []


def test_parallel_stages_and_explicit_dependencies_are_independent_of_delegation():
    state = {"status": "running", "muralRuntime": {"tasks": [
        {"task_id": "r", "role": "research", "status": "running"},
        {"task_id": "i", "role": "image", "status": "running"},
        {"task_id": "p", "role": "slide", "pages": [1], "status": "running"},
        {"task_id": "wait", "role": "slide", "status": "pending", "dependencies": ["i"]}],
        "deck_timeline": [{"type": "task_submitted", "task_id": "r"}]}}
    result = overview(state)
    assert result["active"] == ["研究与规划", "素材准备", "页面制作"]
    assert result["delegated"] == ["task:r"]
    assert result["graph"]["edges"] == [{"from": "task:i", "to": "task:wait"}]
    assert result["graph"]["nodes"][3]["status"] == "waiting_dependencies"


def test_delegation_requires_accepted_record_not_just_attempted_tool_call():
    state = {"muralRuntime": {"tasks": [{"task_id": "p", "role": "slide"}]}, "feed": {"orch": [
        {"k": "tool", "tool": "delegate_submit", "tool_use_id": "d", "args": {"tasks": [{"task_id": "p"}]}}]}}
    assert overview(state)["delegated"] == []
    state["feed"]["orch"].append({"k": "tool_result", "tool": "delegate_submit", "tool_use_id": "d", "ok": True})
    assert overview(state)["delegated"] == ["task:p"]


def test_slide_owner_repair_batches_render_as_one_workflow_node():
    state = {"status": "running", "muralRuntime": {"tasks": [
        {"task_id": "slide-01", "role": "slide", "pages": [1], "owner_id": "owner-p1",
         "agent_key": "slide_01", "status": "succeeded"},
        {"task_id": "slide-01-repair", "role": "slide", "pages": [1], "owner_id": "owner-p1",
         "agent_key": "slide_01", "owner_action": "reuse_owner", "status": "running"},
    ], "deck_timeline": [{"type": "task_submitted", "task_id": "slide-01"}]},
        "agentTimings": {"slide_01": {"task_id": "slide-01-repair", "execution_state": "running"}}}
    result = overview(state)
    nodes = result["graph"]["nodes"]
    assert len(nodes) == 1
    node = nodes[0]
    assert node["id"] == "slide-owner:owner-p1"
    assert node["taskId"] == "slide-01-repair"
    assert node["taskIds"] == ["slide-01", "slide-01-repair"]
    assert node["pages"] == [1] and node["status"] == "running"
    assert node["repairs"] == 1 and node["code"] == "同一 Owner · 2 批次"
    assert result["delegated"] == ["slide-owner:owner-p1"]


def test_batches_keep_owner_identity_and_resource_wait_does_not_leak_to_previous_batch():
    result = overview({"muralRuntime": {"tasks": [
        {"task_id": "a", "role": "image", "owner_id": "same", "agent_key": "image", "status": "succeeded"},
        {"task_id": "b", "role": "image", "owner_id": "same", "agent_key": "image", "status": "running"}]},
        "feed": {"image": [{"task_id": "b", "event_type": "resource_wait_started"}]}})
    first, second = result["graph"]["nodes"]
    assert first["status"] == "complete" and second["status"] == "resource_wait"
    assert "同一 Owner · 批次 2/2" == second["code"]
    assert second["identity"] == "b · same" and second["title"] == "图片素材"


def test_scaffold_png_plan_and_parse_do_not_prove_review_or_delivery():
    result = overview({"status": "completed", "rendered": [1, 2], "authored": [1, 2],
        "staticDeckFinal": True, "briefMeta": {"verified_delivery_download": True},
        "workflow": {"artifacts": {"groups": [{"files": [{"rel": "deck.md"}, {"rel": "delivery/deck.pptx"}]}]}},
        "attachmentParse": [{"status": "completed"}]})
    phases = {row["id"]: row for row in result["phases"]}
    assert phases["review"]["status"] == phases["delivery"]["status"] == "unknown"
    assert phases["research"]["status"] == "observed"
    assert "不代表 Research 已阅读" in phases["attachments"]["detail"]


def test_verified_receipt_not_model_message_marks_review_and_delivery():
    state = {"status": "completed", "artifactStatus": "complete", "feed": {"orch": [
        {"k": "text", "s": "全部验收通过，已经完成！"}]}}
    assert next(row for row in overview(state)["phases"] if row["id"] == "review")["status"] == "unknown"
    state["feed"]["orch"] += [
        {"k": "tool", "tool": "verification_submit", "tool_use_id": "v", "args": {"status": "ready"}},
        {"k": "tool_result", "tool": "verification_submit", "tool_use_id": "v", "ok": True}]
    assert all(row["status"] == "complete" for row in overview(state)["phases"] if row["id"] in {"review", "delivery"})
    state["status"] = "running"
    state["muralRuntime"] = {"tasks": [{"task_id": "revision", "role": "slide", "status": "running"}]}
    phases = {row["id"]: row for row in overview(state)["phases"]}
    assert phases["review"]["status"] == "observed" and phases["delivery"]["status"] == "unknown"


def test_current_action_and_receipts_are_scoped_to_task_not_reused_owner():
    result = overview({"status": "running", "muralRuntime": {"tasks": [
        {"task_id": "a", "role": "image", "owner_id": "same", "agent_key": "image", "status": "succeeded", "result": {"segment_ids": ["s1"]}},
        {"task_id": "b", "role": "image", "owner_id": "same", "agent_key": "image", "status": "running", "result": {"segment_ids": ["s2"]}}]},
        "feed": {"image": [
            {"task_id": "a", "segment_id": "s1", "k": "tool_result", "tool": "web_search", "tool_use_id": "old", "turn": 9, "ok": True},
            {"task_id": "b", "segment_id": "s2", "k": "tool_result", "tool": "read_file", "tool_use_id": "new", "turn": 1, "ok": True},
            {"task_id": "b", "segment_id": "s2", "k": "tool_result", "tool": "read_file", "tool_use_id": "new", "turn": 1, "ok": True},
            {"task_id": "b", "segment_id": "s2", "k": "tool", "tool": "image_generate", "tool_use_id": "generate", "hint": "封面主图", "turn": 2}]}})
    first, current = result["graph"]["nodes"]
    assert first["activity"]["title"] == "工具已返回 · web_search"
    assert first["progress"] == "第 9 回合 · 1 次工具回执"
    assert current["activity"] == {"title": "执行工具 · image_generate", "detail": "封面主图"}
    assert current["progress"] == "第 2 回合 · 1 次工具回执"
    assert result["coordinator"]["progress"] == "委派任务完成 1 / 2"


@pytest.mark.parametrize("status,expected", [("ready", "complete"), ("incomplete", "waiting")])
def test_automatic_completion_uses_real_harness_event_without_ready_tool(status, expected):
    state = {"status": "completed", "artifactStatus": "complete", "feed": {"orch": [
        {"k": "status", "event_type": "deck_completion_checked", "status": status, "seq": 5},
        {"k": "status", "event_type": "agent_completed", "seq": 6}]}}
    before = json.dumps(state)
    phase = next(row for row in overview(state)["phases"] if row["id"] == "review")
    assert phase["status"] == expected and "Harness 自动" in phase["detail"]
    assert "尚无整册验收回执" not in phase["detail"]
    assert json.dumps(state) == before
    if status == "ready":
        assert "不代表离线内容 QC 或 clean 准入" in phase["detail"]


@pytest.mark.parametrize("pending_task", [False, True])
def test_previous_automatic_completion_does_not_finish_an_active_revision(pending_task):
    state = {"status": "running", "artifactStatus": "complete", "feed": {"orch": [
        {"k": "status", "event_type": "deck_completion_checked", "status": "ready"}]}}
    if pending_task:
        state["muralRuntime"] = {"tasks": [{"task_id": "repair", "role": "slide", "status": "running"}]}
    phase = next(row for row in overview(state)["phases"] if row["id"] == "review")
    assert phase["status"] == "observed"
    assert ("仍有未闭合任务" if pending_task else "当前制作尚未收尾") in phase["detail"]


@pytest.mark.parametrize("latest", ["automatic_failure", "automatic_success", "new_issue"])
def test_latest_completion_evidence_wins_over_earlier_pass_or_failure(latest):
    passed = {"k": "status", "event_type": "deck_completion_checked", "status": "ready"}
    failed = {"k": "status", "event_type": "deck_completion_checked", "status": "incomplete"}
    issue = [{"k": "tool", "tool": "verification_submit", "tool_use_id": "issue", "args": {"status": "issues_found"}},
             {"k": "tool_result", "tool": "verification_submit", "tool_use_id": "issue", "ok": True}]
    events = {"automatic_failure": [passed, failed], "automatic_success": [*issue, failed, passed],
              "new_issue": [passed, *issue]}[latest]
    state = {"status": "completed", "artifactStatus": "complete", "feed": {"orch": events}}
    phase = next(row for row in overview(state)["phases"] if row["id"] == "review")
    assert phase["status"] == ("complete" if latest == "automatic_success" else "waiting")


@pytest.mark.parametrize("count", [0, 1, 2, 5, 9, 16])
@pytest.mark.parametrize("width", [320, 620, 1100])
def test_original_flow_has_shared_plan_closure_and_one_slide_band(count, width):
    result = _js([*FUNCTIONS, "workflowFlowLayout"], f"""
const nodes=Array.from({{length:{count}}},(_,i)=>({{task_id:'t'+i,role:'slide',pages:[i+1]}}));
nodes.push({{task_id:'research',role:'research',pages:[]}},{{task_id:'image',role:'image',pages:[]}});
const state={{fullQuery:'Actual task',rendered:new Set()}}, graph=workflowTaskGraph(nodes);
console.log(JSON.stringify(workflowFlowLayout(graph.nodes,workflowOverview(graph,state),{width},state)));
""")
    assert result["width"] >= width
    rows = result["rows"]
    assert rows[1]["nodes"][0]["x"] + 140 == result["width"] / 2
    assert [n["id"] for row in rows for n in row["nodes"]] == ["query", "orch", "task:research", "blueprint", "task:image"] + [f"task:t{i}" for i in range(count)] + ["assemble", "review", "delivery"]
    assert len([row for row in rows if row["nodes"][0]["role"] == "slide"]) == bool(count)
    assert all(node["status"] == "unknown" for row in rows for node in row["nodes"] if node["id"] in {"blueprint", "assemble", "review", "delivery"})
    assert result["edges"] == [{"from": source["id"], "to": target["id"], "axis": "vertical"}
        for previous, current in zip(rows, rows[1:]) for source in previous["nodes"] for target in current["nodes"]]
    for row in rows:
        for i, node in enumerate(row["nodes"]):
            card_width = 232 if node.get("taskId") else {"query": 180, "orch": 280, "blueprint": 194, "assemble": 188, "review": 192, "delivery": 174}[node["id"]]
            assert node["x"] >= 0 and node["x"] + card_width <= result["width"]
            if i:
                assert row["nodes"][i - 1]["x"] + 232 < node["x"]


def test_all_tasks_keep_drilldown_identity_without_needing_a_current_segment():
    result = overview({"muralRuntime": {"tasks": [
        {"task_id": "known", "role": "image", "agent_key": "image", "status": "pending"},
        {"task_id": "timed", "role": "slide", "status": "running"},
        {"task_id": "logged", "role": "research", "status": "succeeded"},
        {"task_id": "unassigned", "role": "slide", "status": "pending"}]},
        "agentTimings": {"slide_group_t": {"task_id": "timed"}},
        "feed": {"orch": [{"task_id": "unassigned", "event_type": "tool_result", "role": "orchestrator"}],
                 "research": [{"task_id": "logged", "event_type": "agent_completed", "role": "research"}]}})
    nodes = result["graph"]["nodes"]
    assert [node["agentKey"] for node in nodes] == ["image", "slide_group_t", "research", ""]
    assert nodes[-1]["taskId"] == "unassigned"
