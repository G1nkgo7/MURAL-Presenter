"""委派消息复用现有协作卡片，状态刷新不依赖主 Agent 继续发言。"""
import json
import os

import pytest

from test_startup_observability import JS_PATH, _js, _js_source


FUNCTIONS = [
    "workflowDelegationNodes", "workflowDelegationMarkup", "workflowDelegationStateKey",
    "workflowTaskGraph", "workflowExecutionStatus", "workflowCurrentActivity",
    "runtimeEventState", "workflowNodeMarkup", "workflowStatusLabel", "escapeHtml",
    "workflowOwnerProgressEntry", "slideProgressSteps", "agentPages", "agentTimingFor",
    "canonicalOrchestrationAgentKey", "orchestrationAgentMeta", "orchestrationEntryContent",
    "liveToolTransactions", "specialistStep", "specialistProgressEntry", "specialistToolCount", "specialistToolStep",
    "specialistGalleryMarkup", "orchestrationAgentGlyph",
]


def task(task_id="pair", status="queued", **values):
    return {"task_id": task_id, "role": "slide", "pages": [1, 2], "status": status,
            "agent_key": "slide_group_pair", "owner_id": "owner-pair", **values}


def run_cards(tasks=(), *, receipt=None, ok=True, requested=None, feed=None, timing=None, body=None, extra=()):
    transaction = {"id": "submit-1", "tool": "delegate_submit", "agent": "orch",
                   "use": {"args": {"tasks": requested or [{"task_id": "pair", "role": "slide"}]}},
                   "result": {"ok": ok, "duration_ms": 1154, "result": receipt}}
    state = {"status": "running", "id": 42, "muralRuntime": {"tasks": tasks},
             "agentTimings": timing or {}, "feed": feed or {}, "workflowTraceAgent": "orch",
             "workflowTracePayload": {"agent": {"key": "orch"}, "content": "unchanged trace"}}
    setup = "const ed=" + json.dumps(state) + ";ed.rendered=new Set();\n"
    setup += "const tx=" + json.dumps(transaction) + ";\n"
    setup += "const pad2=n=>String(n).padStart(2,'0'),fileUrl=n=>'/page/'+n;\n"
    source = JS_PATH.read_text()
    start = source.index("const ORCHESTRATION_ROLE_LABELS =")
    setup += source[start:source.index("\n};", start) + 3] + "\n"
    setup += """
const editorPresentationKind=()=> 'static';
const agentTimingBadge=()=>'',workflowArtifactRelsFromEvents=()=>[];
const cleanAgentText=s=>s||'',agentMarkdownHtml=escapeHtml;
"""
    setup += "const before=JSON.stringify([ed,tx]);\n"
    return _js(list(dict.fromkeys(FUNCTIONS + list(extra))), body or """
const nodes=workflowDelegationNodes(tx),html=workflowDelegationMarkup(tx);
console.log(JSON.stringify({nodes,html,unchanged:before===JSON.stringify([ed,tx])}));
""", setup=setup)


def test_accepted_batch_shows_each_original_agent_before_first_message():
    tasks = [task(f"group-{i}", pages=[i * 2 + 1, i * 2 + 2],
                  agent_key=f"slide_group_{i}", owner_id=f"owner-{i}") for i in range(7)]
    result = run_cards(tasks, requested=[{"task_id": row["task_id"]} for row in tasks])
    assert result["unchanged"] and len(result["nodes"]) == 7
    assert result["html"].count('<article class="orch-entry specialist role-slide"') == 7
    assert 'class="workflow-node ' not in result["html"]
    assert result["html"].count("等待调度") == 7
    assert "NaN" not in result["html"] and "left:" not in result["html"]
    assert [node["agentKey"] for node in result["nodes"]] == [row["agent_key"] for row in tasks]
    assert all(node["status"] != "complete" for node in result["nodes"])


@pytest.mark.parametrize("status,label", [
    ("queued", "等待调度"), ("running", "正在执行"), ("waiting", "等待输入"),
    ("succeeded", "已完成"), ("failed", "执行失败"), ("cancelled", "已取消"),
    ("start_failed", "启动失败"), ("skipped", "已跳过"),
])
def test_card_uses_execution_state_not_submission_success(status, label):
    result = run_cards([task(status=status)])
    assert label in result["html"]
    assert result["nodes"][0]["taskId"] == "pair"
    assert result["nodes"][0]["identity"] == "pair · owner-pair"


@pytest.mark.parametrize("encoded", [False, True])
def test_receipt_fills_poll_gap_but_current_runtime_replaces_old_status(encoded):
    receipt = {"tasks": [task(status="queued")]}
    if encoded:
        receipt = json.dumps(receipt)
    result = run_cards(receipt=receipt)
    assert len(result["nodes"]) == 1 and result["nodes"][0]["status"] == "queued"
    result = run_cards([task(status="succeeded")], receipt=receipt)
    assert result["nodes"][0]["status"] == "complete"


def test_args_alone_and_rejected_submission_never_invent_an_agent():
    assert run_cards()["nodes"] == []
    assert run_cards([task()], ok=False)["nodes"] == []
    assert run_cards(receipt="invalid json")["nodes"] == []


@pytest.mark.parametrize("error_in_block", [True, False])
def test_readonly_trace_shows_delegation_format_error_without_phantom_cards(error_in_block):
    result = run_cards([task("image-1", role="image", status="failed", pages=[])],
        feed={"orch": [{"k": "tool_result", "segment_id": "one", "tool_use_id": "invalid-submit",
                        "error_code": "invalid_arguments"}]},
        extra=["workflowTraceConversationRows", "workflowTraceContentBlocks", "workflowTraceResultParts",
               "compactTraceValue", "liveReadFileSummary", "liveTerminalLabel", "liveToolTransactionMarkup"],
        body="""
const TOOL_LABEL={},workflowTraceSafeJson=JSON.stringify,isStaticActiveStatus=()=>false;
ed.workflowTracePayload.selected_segment='one';
const records=[{role:'assistant',content:[{type:'tool_use',id:'invalid-submit',name:'delegate_submit',
  input:{tasks:'[{"task_id":"image-1","role":"image"}]'}}]},
  {role:'user',content:[{type:'tool_result',tool_use_id:'invalid-submit',is_error:true,
    content:'$.tasks must be an array; received string',
    error_code:""" + ("'invalid_arguments'" if error_in_block else "undefined") + """}]}];
const recordsBefore=JSON.stringify(records),rows=workflowTraceConversationRows(records);
console.log(JSON.stringify({html:rows.map(row=>row.html).join(''),unchanged:recordsBefore===JSON.stringify(records)}));
""")
    assert result["unchanged"]
    assert "委派参数格式错误，任务未启动" in result["html"]
    assert "invalid_arguments" in result["html"] and "received string" in result["html"]
    assert "Role 未记录" not in result["html"]
    assert "orch-entry specialist" not in result["html"]


def test_dependencies_and_resource_wait_reuse_graph_status():
    tasks = [task(), task("research", role="research", pages=[], status="running", agent_key="research")]
    tasks[0]["dependencies"] = ["research"]
    assert run_cards(tasks)["nodes"][0]["status"] == "waiting_dependencies"
    feed = {"slide_group_pair": [{"event_type": "resource_wait_started", "task_id": "pair"}]}
    assert run_cards([task(status="running")], feed=feed)["nodes"][0]["status"] == "resource_wait"


def test_current_activity_is_scoped_to_exact_task_not_another_owner_segment():
    feed = {"slide_group_pair": [
        {"event_type": "agent_started", "task_id": "pair", "segment_id": "current", "role": "slide"},
        {"k": "tool", "tool": "inspect", "task_id": "pair", "turn": 4, "seq": 3},
        {"k": "tool", "tool": "wrong_task_tool", "task_id": "old-pair", "seq": 4},
    ]}
    result = run_cards([task(status="running")], feed=feed)
    assert result["nodes"][0]["activity"]["title"] == "执行工具 · inspect"
    assert result["nodes"][0]["segment"] == "current"
    assert "wrong_task_tool" not in result["html"] and "第 4 回合" in result["html"]


def test_child_changes_update_refresh_key_without_orchestrator_changes():
    result = run_cards([task()], body="""
const keys=[workflowDelegationStateKey()];
ed.muralRuntime.tasks[0].status='running';keys.push(workflowDelegationStateKey());
ed.feed.slide_group_pair=[{k:'tool',tool:'inspect',seq:9,task_id:'pair'}];keys.push(workflowDelegationStateKey());
ed.agentTimings.slide_group_pair={task_id:'pair',execution_state:'waiting',status_detail:'等待主 Agent 决定'};
keys.push(workflowDelegationStateKey());
console.log(JSON.stringify({keys,html:workflowDelegationMarkup(tx),trace:ed.workflowTracePayload.content}));
""")
    assert len(set(result["keys"])) == 4
    assert "等待输入" in result["html"] and "等待主 Agent 决定" in result["html"]
    assert result["trace"] == "unchanged trace"


def test_original_card_follows_its_owner_on_repair_but_not_overlapping_pages():
    tasks = [task(status="succeeded"), task("repair", status="running", pages=[2])]
    timing = {"slide_group_pair": {"task_id": "repair", "execution_state": "running"}}
    result = run_cards(tasks, timing=timing)
    assert len(result["nodes"]) == 1
    assert result["nodes"][0]["id"] == "slide-owner:owner-pair"
    assert result["nodes"][0]["taskId"] == "repair"
    assert result["nodes"][0]["taskIds"] == ["pair", "repair"]
    assert result["nodes"][0]["repairs"] == 1
    assert result["nodes"][0]["agentKey"] == "slide_group_pair"
    assert "正在执行" in result["html"]
    assert "修订回流 ×1" in result["html"]
    tasks[1]["owner_id"] = "unrelated-owner"
    assert run_cards(tasks, timing=timing)["nodes"][0]["taskId"] == "pair"


def test_trace_keeps_tool_pairing_and_embeds_cards_outside_closed_receipt():
    result = run_cards([task()], extra=["workflowTraceConversationRows", "workflowTraceContentBlocks"], body="""
const isStaticActiveStatus=()=>true,workflowTraceSafeJson=JSON.stringify;
const liveToolTransactionMarkup=()=>'<details class="receipt"><summary>提交回执</summary></details>';
const workflowTraceResultParts=()=>({images:0}),workflowArtifactLinkMarkup=()=>'',workflowToolOutputMarkup=()=>'';
const workflowTraceBlockMarkup=b=>b.text||'';
const records=[{role:'assistant',content:[{type:'text',text:'开始并行。'},
  {type:'tool_use',id:tx.id,name:tx.tool,input:tx.use.args}]},
  {role:'user',content:[{type:'tool_result',tool_use_id:tx.id,content:{tasks:[ed.muralRuntime.tasks[0]]}}]}];
const beforeRecords=JSON.stringify(records);
console.log(JSON.stringify({rows:workflowTraceConversationRows(records),unchanged:JSON.stringify(records)===beforeRecords}));
""")
    assert result["unchanged"] and len(result["rows"]) == 1
    html = result["rows"][0]["html"]
    assert html.count('class="receipt"') == 1
    assert html.index("</details>") < html.index('class="workflow-delegation-cards"')
    assert "开始并行。" in html and 'data-workflow-task-id="pair"' in html


@pytest.mark.parametrize("accepted", [False, True])
def test_history_keeps_submission_receipt_without_a_second_owner_card(accepted):
    tasks = [task(), task("single", pages=[3], agent_key="slide_03", owner_id="owner-single")]
    result = run_cards(tasks, ok=accepted, requested=[{"task_id": row["task_id"]} for row in tasks],
                       extra=["orchestrationEntryContent"], body="""
const isStaticActiveStatus=()=>true;
const liveToolTransactionMarkup=()=>'<details class="receipt"><summary>拉起同事 · Slide × 2</summary></details>';
const entry={type:'orchestrator-action',meta:{key:'orch',role:'orch'},transaction:tx};
const html=orchestrationEntryContent(entry,{artifactMode:'links'});
console.log(JSON.stringify({html,unchanged:before===JSON.stringify([ed,tx])}));
""")
    assert result["unchanged"] and "拉起同事 · Slide × 2" in result["html"]
    assert 'data-embedded-agent="true"' not in result["html"]
    assert 'workflow-delegation-cards' not in result["html"]


def test_history_cards_refresh_when_owner_changes_without_new_orchestrator_messages():
    result = run_cards([task()], extra=["orchestrationEntryContent", "reconcileOrchestrationEntries"], body="""
const isStaticActiveStatus=()=>true;
const liveToolTransactionMarkup=()=>'<details><summary>原提交回执</summary></details>';
const entry=()=>workflowOwnerProgressEntry(workflowDelegationNodes(tx)[0]);
const list={children:[],insertBefore(card){this.children.push(card)}};
const $$=(_selector,root)=>root.children;
const document={createElement:()=>({dataset:{},setAttribute(){},querySelectorAll:()=>[]})};
reconcileOrchestrationEntries(list,[entry()],{artifactMode:'links'});
const first=list.children[0],html=[first.innerHTML];
ed.muralRuntime.tasks[0].status='running';
ed.feed.slide_group_pair=[{k:'tool',tool:'inspect',seq:9,task_id:'pair'}];
reconcileOrchestrationEntries(list,[entry()],{artifactMode:'links'});html.push(first.innerHTML);
ed.muralRuntime.tasks[0].status='succeeded';
reconcileOrchestrationEntries(list,[entry()],{artifactMode:'links'});html.push(first.innerHTML);
console.log(JSON.stringify({html,same:list.children[0]===first,count:list.children.length,trace:ed.workflowTracePayload.content}));
""")
    assert result["same"] and result["count"] == 1
    assert "等待调度" in result["html"][0]
    assert "正在执行" in result["html"][1] and "执行工具 · inspect" in result["html"][1]
    assert "已完成" in result["html"][2]
    assert result["trace"] == "unchanged trace"


def test_message_dom_refreshes_when_only_child_state_changes():
    result = run_cards([task()], extra=["workflowTraceConversationRows", "workflowTraceContentBlocks",
                                       "renderWorkflowTraceMessages"], body="""
const isStaticActiveStatus=()=>true,workflowTraceSafeJson=JSON.stringify;
const liveToolTransactionMarkup=()=>'<details><summary>原提交回执</summary></details>';
const workflowTraceResultParts=()=>({images:0}),workflowArtifactLinkMarkup=()=>'',workflowToolOutputMarkup=()=>'';
const workflowTraceBlockMarkup=b=>b.text||'';
const records=[{role:'assistant',content:[{type:'tool_use',id:tx.id,name:tx.tool,input:tx.use.args}]}];
const workflowTraceMessageRecords=()=>({records}),workflowTraceFailureMarkup=()=>'';
const conversationTime=()=>null,workflowTraceHighlightMatches=()=>0;
const notice={},count={},summary={};
const lane={children:[],insertBefore(card){this.children.push(card)}};
const list={dataset:{},classList:{remove(){}},querySelector:selector=>({
  '.workflow-trace-conversation':lane,'.workflow-trace-notice':notice,
  '.workflow-trace-message-count > span':count,'[data-trace-search-summary]':summary})[selector]||null};
const $=selector=>selector==='#workflow-trace-messages'?list:null;
const document={createElement:()=>({dataset:{},querySelectorAll:()=>[]})};
renderWorkflowTraceMessages();const first=lane.children[0],html=[first.innerHTML];
ed.muralRuntime.tasks[0].status='running';
ed.feed.slide_group_pair=[{k:'tool',tool:'inspect',seq:9,task_id:'pair'}];
renderWorkflowTraceMessages();html.push(first.innerHTML);
ed.muralRuntime.tasks[0].status='succeeded';
renderWorkflowTraceMessages();html.push(first.innerHTML);
console.log(JSON.stringify({html,same:lane.children[0]===first,count:lane.children.length,
  trace:ed.workflowTracePayload.content}));
""")
    assert result["same"] and result["count"] == 1
    assert "等待调度" in result["html"][0]
    assert "正在执行" in result["html"][1] and "执行工具 · inspect" in result["html"][1]
    assert "已完成" in result["html"][2]
    assert result["trace"] == "unchanged trace"


@pytest.mark.parametrize("event_type", ["click", "keydown"])
@pytest.mark.parametrize("host_id", ["#workflow-trace-messages", "#workflow-message-list", "#agent-progress-console"])
def test_card_navigation_reuses_exact_owner_and_task(event_type, host_id):
    result = run_cards([task()], extra=["bindWorkflowTraceControls"], body="""
const handlers={},opened=[],panes=[];
const host={dataset:{},addEventListener:(name,fn)=>(handlers[name]??=[]).push(fn)};
const $=selector=>selector===HOST_ID?host:null;
const window={getSelection:()=>null};
const setWorkflowPane=p=>panes.push(p),openWorkflowOwnerGraph=(agent,options)=>opened.push({agent,options});
bindWorkflowTraceControls();bindWorkflowTraceControls();
const card={dataset:{nodeId:'task:pair',agentKey:'slide_group_pair',workflowTaskId:'pair'}};
const target={closest:selector=>selector==='[data-embedded-agent="true"]'?card:null};
for(const handler of handlers[EVENT_TYPE])handler({type:EVENT_TYPE,key:'Enter',target,preventDefault(){}});
console.log(JSON.stringify({opened,panes,selected:ed.workflowSelectedNodeId}));
""".replace("EVENT_TYPE", json.dumps(event_type)).replace("HOST_ID", json.dumps(host_id)))
    assert result["opened"] == [{"agent": "slide_group_pair", "options": {"taskId": "pair"}}]
    assert result["panes"] == ["graph"] and result["selected"] == "task:pair"


def test_main_history_keeps_lower_image_card_and_adds_pending_owner_once():
    tasks = [task("images", role="image", pages=[], agent_key="image", owner_id="image-owner", status="succeeded"), task()]
    result = run_cards(tasks, extra=["orchestrationMainView"], body="""
ed.specialistArtifacts={agents:{image:{count:2,completed:true,images:[{path:'assets/example.png',name:'插图',origin:'generated'}]}}};
const image=workflowOwnerProgressEntry(workflowTaskGraph(ed.muralRuntime.tasks).nodes[0]);
const entry={type:'orchestrator-action',key:'submit',order:1,meta:{key:'orch',role:'orch'},transaction:tx};
const orchestrationEntries=()=>({flat:[],entries:[entry,{...image,order:3}]});
const isStaticActiveStatus=()=>true,liveToolTransactionMarkup=()=>'<details>提交回执</details>';
const original=JSON.stringify([ed,tx]);
const view=orchestrationMainView(ed.feed);
const html=view.entries.map(e=>orchestrationEntryContent(e)).join('');
console.log(JSON.stringify({entries:view.entries,html,unchanged:original===JSON.stringify([ed,tx])}));
""")
    assert result["unchanged"]
    assert len(result["entries"]) == 3
    assert result["html"].count("Image Agent") == 1
    assert result["html"].count("图片登记与预览") == 1
    assert result["html"].count("查看迭代") == 2
    assert "assets%2Fexample.png" in result["html"]
    assert 'class="workflow-node ' not in result["html"]
    assert "Group Owner · P1 / P2" in result["html"]
    assert "等待调度" in result["html"]


def test_completed_group_does_not_invent_missing_group_inspection():
    feed = {"slide_group_pair": [
        {"k": "tool", "tool": "inspect", "tool_use_id": "p1", "task_id": "pair", "seq": 1},
        {"k": "tool_result", "tool": "inspect", "tool_use_id": "p1", "task_id": "pair", "seq": 2, "ok": True},
        {"k": "status", "event_type": "agent_completed", "task_id": "pair", "seq": 3},
    ]}
    result = run_cards([task(status="succeeded")], feed=feed, body="""
const entry=workflowOwnerProgressEntry(workflowDelegationNodes(tx)[0]);
console.log(JSON.stringify({entry,html:orchestrationEntryContent(entry)}));
""")
    assert result["entry"]["completed"]
    assert result["entry"]["steps"][2]["state"] == "recorded"
    assert len(result["entry"]["steps"]) == 3
    assert "1 次成功回执" in result["entry"]["steps"][2]["detail"]
    assert "视觉检查记录（单页 / 联系表 / 素材）" in result["html"]
    assert "执行结束不代表内容与视觉质量通过" in result["html"]


def test_group_repair_keeps_all_owned_pages_but_counts_current_task_only():
    tasks = [task(status="succeeded"), task("repair", pages=[2], status="running")]
    result = run_cards(tasks, timing={"slide_group_pair": {"task_id": "repair", "execution_state": "running"}},
        feed={"slide_group_pair": [
            {"k": "tool_result", "tool": "inspect_group", "tool_use_id": "old", "task_id": "pair", "ok": True},
            {"k": "tool_result", "tool": "inspect", "tool_use_id": "new", "task_id": "repair", "ok": False},
        ]}, body="""
console.log(JSON.stringify(workflowOwnerProgressEntry(workflowDelegationNodes(tx)[0])));
""")
    assert result["meta"]["pages"] == [1, 2] and result["taskId"] == "repair"
    assert result["steps"][2]["state"] == "recorded" and len(result["steps"]) == 3
    assert "1 次失败回执" in result["steps"][2]["detail"]


def test_single_owner_uses_stage_style_without_group_step_and_keeps_previews():
    result = run_cards([task(pages=[1])], body="""
ed.rendered.add(1);
console.log(JSON.stringify({html:workflowDelegationMarkup(tx)}));
""")
    assert "Slide Owner · P1" in result["html"]
    assert 'class="specialist-steps"' in result["html"]
    assert "组联系表复核" not in result["html"]
    assert 'data-workflow-page="1"' in result["html"]


def test_owner_keeps_recorded_segment_when_start_event_is_outside_feed():
    result = run_cards([task()], feed={"slide_group_pair": [
        {"k": "text", "s": "本次制作说明", "task_id": "pair", "segment_id": "slide-first"},
    ]}, body="""
console.log(JSON.stringify(workflowOwnerProgressEntry(workflowDelegationNodes(tx)[0])));
""")
    assert result["segment"] == "slide-first"


@pytest.mark.parametrize("tag", ["a", "summary", "details"])
def test_expanding_owner_details_or_opening_image_does_not_navigate_owner(tag):
    result = run_cards([task()], extra=["bindWorkflowTraceControls"], body="""
const handlers={},opened=[];
const host={dataset:{},addEventListener:(name,fn)=>(handlers[name]??=[]).push(fn)};
const $=selector=>selector==='#workflow-trace-messages'?host:null;
const window={getSelection:()=>null};
const openWorkflowOwnerGraph=()=>opened.push('owner'),setWorkflowPane=()=>{};
bindWorkflowTraceControls();
const target={closest:selector=>selector==='[data-embedded-agent="true"]'?{dataset:{}}:
  selector.split(', ').includes(TAG)?{}:null};
for(const fn of handlers.click)fn({type:'click',target,preventDefault(){throw Error('must keep native action')}});
console.log(JSON.stringify({opened}));
""".replace("TAG", json.dumps(tag)))
    assert not result["opened"]


@pytest.mark.parametrize("text", [
    "Slide 2 is not ready. The layout still needs work.",
    "Review is not complete.",
    "研究尚未完成，仍有事实待核验。",
    "Let me reconsider the claim. It is unsupported.",
    "I will inspect the page; I have not checked it yet.",
    "The write_file didn't actually overwrite the file.",
])
def test_agent_words_are_not_translated_into_a_completion_claim(text):
    result = _js(["cleanAgentText", "compactPageAgentResult", "compactOrchestrationCurrentDetail"], """
const text=INPUT;
console.log(JSON.stringify({
  raw:cleanAgentText(text),page:compactPageAgentResult(text),
  current:compactOrchestrationCurrentDetail({role:'image'},text)
}));
""".replace("INPUT", json.dumps(text)))
    assert result == {"raw": text, "page": {"summary": text, "detail": ""}, "current": text}


def test_long_agent_reply_keeps_the_complete_original_in_details():
    text = "Let me explain: " + "This is not complete. " * 20
    result = _js(["cleanAgentText", "compactPageAgentResult"],
                 "console.log(JSON.stringify(compactPageAgentResult(" + json.dumps(text) + ")));")
    assert result["summary"] == text[:160].rstrip() + "…"
    assert result["detail"] == text.strip()


@pytest.mark.parametrize("role,text,artifacts", [
    ("research", "研究尚未完成，仍有事实待核验", {"brief_ready": True}),
    ("image", "计划生成 10 张素材，目前尚未完成", {}),
    ("review", "Review is not complete.", {}),
    ("material", "附件尚未完成解析", {"files": [{"status": "complete"}]}),
])
def test_text_intent_or_artifact_presence_does_not_complete_a_specialist(role, text, artifacts):
    result = _js(["specialistProgressEntry", "specialistToolStep", "specialistToolCount",
                  "specialistStep", "liveToolTransactions", "cleanAgentText", "compactPageAgentResult"], """
const entry=specialistProgressEntry({role:ROLE,key:ROLE},[
  {k:'status',event_type:'agent_started',seq:1},
  {k:'text',s:TEXT,seq:2},
  {k:'analysis',s:'全部完成，素材已就绪',seq:3}
],ARTIFACTS);
console.log(JSON.stringify(entry));
""".replace("ROLE", json.dumps(role)).replace("TEXT", json.dumps(text)).replace("ARTIFACTS", json.dumps(artifacts)))
    assert not result["completed"] and not result["failed"]
    assert result["result"] == text
    assert all(step["state"] not in ("done", "failed", "active") for step in result["steps"])
    if role == "image":
        assert result["assetCount"] is None and result["contactSheet"] is None
        assert result["steps"][0]["state"] == "unknown"
        assert "已读取" not in result["steps"][0]["detail"]


def test_failed_then_successful_check_stays_history_not_current_failure():
    result = run_cards([task(status="running")], feed={"slide_group_pair": [
        {"k": "tool_result", "tool": "inspect", "tool_use_id": "old", "task_id": "pair", "seq": 1, "ok": False},
        {"k": "tool_result", "tool": "inspect", "tool_use_id": "new", "task_id": "pair", "seq": 2, "ok": True},
    ]}, body="""
console.log(JSON.stringify(workflowOwnerProgressEntry(workflowDelegationNodes(tx)[0])));
""")
    assert not result["failed"] and not result["completed"]
    assert result["steps"][2]["state"] == "recorded"
    assert "1 次成功回执" in result["steps"][2]["detail"] and "1 次失败回执" in result["steps"][2]["detail"]


def test_feed_window_does_not_claim_missing_historical_work():
    result = run_cards([task(status="succeeded")], extra=["mergeIncrementalLiveFeed", "evKey"], body="""
const events=[{k:'tool_result',tool:'inspect',tool_use_id:'first',seq:1,ok:true,task_id:'pair'},
  ...Array.from({length:601},(_,i)=>({k:'text',s:String(i),seq:i+2,task_id:'pair'}))];
ed.feed=mergeIncrementalLiveFeed({}, {slide_group_pair:events});
const entry=workflowOwnerProgressEntry(workflowDelegationNodes(tx)[0]);
console.log(JSON.stringify({entry,html:orchestrationEntryContent(entry),size:ed.feed.slide_group_pair.length}));
""")
    assert result["size"] == 600 and result["entry"]["completed"]
    assert result["entry"]["steps"][2]["state"] == "unknown"
    assert "当前已载入记录未包含" in result["entry"]["steps"][2]["detail"]
    assert "不代表完整历史或阶段完成" in result["html"]


def test_legacy_progress_does_not_merge_negations_or_drop_followup_text():
    first = "The layout is ready for review."
    second = first + " However, the evidence is not verified."
    result = _js(["appendOrchestratorProgress", "cleanAgentText"], """
const updates=[];
appendOrchestratorProgress(updates,FIRST,1);
appendOrchestratorProgress(updates,SECOND,2);
console.log(JSON.stringify(updates));
""".replace("FIRST", json.dumps(first)).replace("SECOND", json.dumps(second)))
    assert [row["text"] for row in result] == [first, second]


def test_browser_owner_stage_cards_layout_and_navigation(tmp_path, monkeypatch):
    from playwright.sync_api import sync_playwright
    from app import engine, preview_fonts

    for name in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(name, str(tmp_path))
    env = engine.render_env(dict(os.environ))
    tasks = [task("images", role="image", pages=[], agent_key="image", owner_id="image-owner", status="succeeded"),
             task("single", pages=[1], agent_key="slide_01", owner_id="single-owner"),
             task(pages=[2, 3], status="running")]
    markup = run_cards(tasks, requested=[{"task_id": row["task_id"]} for row in tasks])["html"]
    html = '<meta charset="utf-8"><link rel="stylesheet" href="/static/app.css"><div class="workflow-conversation-lane" id="workflow-trace-messages">' + markup + '</div>'
    studio = JS_PATH.parents[1]
    fonts = {"/api/preview-fonts/NotoSansSC.ttf": preview_fonts.catalog()["NotoSansSC.ttf"]}
    errors = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=env["PPT_SKILL_BROWSER_EXE"], env=env,
                                     headless=True, args=["--no-sandbox"], timeout=8000)
        try:
            page = browser.new_page(viewport={"width": 520, "height": 1100})
            page.set_default_timeout(5000)
            page.on("pageerror", lambda error: errors.append(str(error)))

            def route(request):
                path = request.request.url.split("owner-cards.test")[-1]
                if path == "/":
                    request.fulfill(body=html, content_type="text/html")
                elif path in fonts:
                    request.fulfill(path=str(fonts[path]))
                elif path.startswith("/static/"):
                    request.fulfill(path=str(studio / path.lstrip("/")))
                else:
                    request.abort()

            page.route("**/*", route)
            page.goto("http://owner-cards.test/", wait_until="domcontentloaded")
            page.add_style_tag(content="body{display:block;height:auto;overflow:auto;padding:12px} .workflow-conversation-lane{width:100%;padding:0}")
            page.evaluate("() => {" + _js_source(["bindWorkflowTraceControls"]) + """
const $=s=>document.querySelector(s), ed={}; window.opened=[];
const setWorkflowPane=()=>{},openWorkflowOwnerGraph=(agent,options)=>opened.push({agent,options});
bindWorkflowTraceControls();
}""")
            assert page.locator(".workflow-node").count() == 0
            assert page.locator("article.orch-entry").count() == 3
            for width, theme in ((520, "light"), (360, "dark")):
                page.set_viewport_size({"width": width, "height": 1100})
                page.evaluate("theme=>document.documentElement.dataset.theme=theme", theme)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert page.locator(".specialist-step").count() == 10
                page.screenshot(path=str(tmp_path / f"owner-cards-{theme}.png"), full_page=True)
            group = page.locator("article.role-slide").last
            group.locator("summary").first.click()
            assert group.locator("details").first.get_attribute("open") is not None
            assert page.evaluate("opened") == []
            group.get_by_role("button", name="查看迭代").click()
            assert page.evaluate("opened") == [{"agent": "slide_group_pair", "options": {"taskId": "pair"}}]
            assert errors == []
        finally:
            browser.close()
