"""主时间线保留真实编排与协作卡，不混入独立评审及 Owner 内部事件。"""
import json

import pytest

from test_startup_observability import JS_PATH, _js


FUNCTIONS = ["canonicalOrchestrationAgentKey", "orchestrationAgentMeta", "orchestrationFlatFeed",
             "slideOwnerSegments", "orchestrationEntries", "orchestrationMainView",
             "workflowTaskGraph", "workflowDelegationNodes"]


def run_view(feed, body=None, extra_functions=()):
    source = JS_PATH.read_text()
    start = source.index("const ORCHESTRATION_ROLE_LABELS =")
    labels = source[start:source.index("\n};", start) + 3]
    setup = labels + """
const ed={status:'running',kind:'other',specialistArtifacts:{agents:{}}};
const agentPages=()=>[7,8], pad2=n=>String(n).padStart(2,'0'), agentTimingFor=()=>null;
const orchestrationEventTime=()=>null, cleanAgentText=s=>s||'', liveAgentText=e=>e.s||'';
const livePreviewLabel=()=> '流式片段', toolProgress=e=>[e.tool,'实际工具进展'], friendlyToolHint=()=>'';
const workflowArtifactRelsFromEvents=()=>['slides/slide_07.html','renders/slide_07.png'];
const compactPageAgentResult=text=>({summary:text,detail:text});
const specialistProgressEntry=(meta,events)=>({type:'specialist',meta,order:events[0].seq,key:meta.key});
const runtimeEventState=e=>e?.event_type?.startsWith('attachment_parse_')?['附件解析','后台事件']
  :e?.event_type==='agent_context_compacted'?['上下文压缩完成','已从新上下文段继续']
  :e?.event_type==='agent_context_compaction_failed'?['上下文压缩未完成','检查点已保留']
  :e?.k==='provider'&&e?.phase==='started'&&e?.request_kind==='context_compaction'?['正在压缩上下文','生成内部续接摘要']:null;
"""
    return _js(FUNCTIONS + list(extra_functions), "const feed=" + json.dumps(feed) + ";"
               "const before=JSON.stringify(feed);" + (body or """
const view=orchestrationMainView(feed,{agents:{}});
console.log(JSON.stringify({view,unchanged:JSON.stringify(feed)===before,raw:feed}));
"""), setup=setup)


@pytest.mark.parametrize("kind", ["critic_started", "critic_completed", "critic_failed", "critic_reused",
    "agent_context", "agent_started", "agent_failed", "assistant_message", "assistant_message_delta",
    "provider_started", "provider_failed", "provider_failover"])
def test_critic_events_stay_out_of_main_timeline_but_remain_available(kind):
    feed = {
        "orch": [{"k": "text", "event_id": "orch-message", "seq": 1, "s": "P7 交回原 Owner。"}],
        "critic-7": [{"k": "text", "event_type": kind, "seq": 2, "s": "独立检查原文",
                      "critic": {"inspection_id": "i7", "owner_id": "owner-7", "page": 7}}],
    }
    result = run_view(feed)
    assert result["unchanged"] and result["raw"] == feed
    assert [entry["text"] for entry in result["view"]["entries"]] == ["P7 交回原 Owner。"]
    assert {row["agent"] for row in result["view"]["flat"]} == {"orch"}


def test_orphan_critic_does_not_become_orchestrator_activity():
    result = run_view({"critic": [{"k": "text", "s": "缺少 Owner 的评审反馈"}]})
    assert result["view"] == {"flat": [], "entries": []} and result["unchanged"]


def test_cooperation_cards_and_artifacts_survive_without_owner_internal_bubbles():
    feed = {
        "orch": [{"k": "text", "event_id": "message", "seq": 1, "s": "提交 P7/P8 制作。"}],
        "slide_group_shared": [
            {"k": "status", "event_type": "agent_started", "seq": 2, "segment_id": "owner-segment"},
            {"k": "tool", "tool": "inspect", "tool_use_id": "inspect-7", "seq": 3, "segment_id": "owner-segment"},
            {"k": "tool_result", "tool": "inspect", "tool_use_id": "inspect-7", "ok": True,
             "result": {"critic_feedback": {"body": "核对父容器"}}, "seq": 4, "segment_id": "owner-segment"},
            {"k": "text", "partial": True, "stream_id": "preview-7", "s": "正在逐像素分析", "seq": 5},
            {"k": "provider", "phase": "failed", "error_summary": "Owner 内部故障", "seq": 6},
            {"k": "text", "s": "P7/P8 已完成。", "seq": 7, "segment_id": "owner-segment"},
            {"k": "status", "event_type": "agent_completed", "seq": 8, "segment_id": "owner-segment"},
        ],
        "research": [{"k": "text", "s": "来源已整理", "seq": 9}],
        "image": [{"k": "text", "s": "图片已登记", "seq": 10}],
    }
    result = run_view(feed)
    entries = result["view"]["entries"]
    assert result["unchanged"]
    assert [entry["type"] for entry in entries] == ["message", "slide", "specialist", "specialist"]
    slide = entries[1]
    assert slide["meta"]["label"] == "页面组协作" and slide["meta"]["pages"] == [7, 8]
    assert slide["segment"] == "owner-segment" and slide["completed"]
    assert slide["resultDetail"] == "P7/P8 已完成。"
    assert slide["artifactRels"] == ["slides/slide_07.html", "renders/slide_07.png"]
    assert {row["agent"] for row in result["view"]["flat"]} == {"orch"}
    assert result["raw"]["slide_group_shared"][2]["result"]["critic_feedback"]["body"] == "核对父容器"


def test_main_messages_and_transactions_keep_exact_body_and_segment_pairing():
    text = "相同措辞仍是不同轮次的真实消息。\n" + "正文" * 200
    feed = {"orchestrator": [
        {"k": "text", "event_id": "message-1", "seq": 1, "segment_id": "s1", "s": text},
        {"k": "text", "event_id": "message-2", "seq": 2, "segment_id": "s2", "s": text},
        {"k": "tool", "tool": "vision_analyze", "seq": 3, "segment_id": "s2", "tool_use_id": "view",
         "args": {"path": "renders/contact-sheet.png", "scope": "deck", "question": "Orch 自己看整册"}},
        {"k": "tool_result", "tool": "vision_analyze", "seq": 4, "segment_id": "s2", "tool_use_id": "view",
         "ok": False, "error_code": "actual_error", "result": "真实失败原文"},
    ]}
    result = run_view(feed)
    entries = result["view"]["entries"]
    assert result["unchanged"]
    assert [entry["text"] for entry in entries[:2]] == [text, text]
    assert entries[0]["key"] != entries[1]["key"]
    tx = entries[2]["transaction"]
    assert tx["agent"] == "orch" and tx["use"]["args"]["question"] == "Orch 自己看整册"
    assert tx["result"]["result"] == "真实失败原文" and tx["result"]["error_code"] == "actual_error"


@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.parametrize("repair_reason", ["将过渡语宽度从 660px 调整为 720px", ""])
def test_owner_repair_and_context_continuation_update_one_existing_card(completed, repair_reason):
    owner_events = [
        {"k": "status", "event_type": "agent_started", "seq": 2, "segment_id": "initial"},
        {"k": "status", "event_type": "agent_completed", "seq": 3, "segment_id": "initial"},
        {"k": "status", "event_type": "agent_started", "seq": 5, "segment_id": "continued"},
        {"k": "tool", "tool": "patch", "tool_use_id": "owner-patch", "seq": 6,
         "segment_id": "continued", "args": {"path": "slides/slide_07.html", "repair_reason": repair_reason}},
        {"k": "tool_result", "tool": "patch", "tool_use_id": "owner-patch", "seq": 7,
         "segment_id": "continued", "ok": True, "result": {"path": "slides/slide_07.html"}},
    ]
    if completed:
        owner_events.append({"k": "status", "event_type": "agent_completed", "seq": 8,
                             "segment_id": "continued"})
    feed = {"orch": [
        {"k": "text", "event_id": "delegation-note", "seq": 1, "s": "交回原 Owner 修正 P7。"},
        {"k": "tool", "tool": "delegate_submit", "tool_use_id": "delegate-repair", "seq": 4,
         "args": {"tasks": [{"task_id": "fix-p7", "role": "slide", "pages": [7]}]}},
    ], "slide_group_shared": owner_events}
    result = run_view(feed)
    entries = result["view"]["entries"]
    assert result["unchanged"] and result["raw"] == feed
    assert [entry["type"] for entry in entries] == ["message", "slide", "orchestrator-action"]
    assert entries[1]["segment"] == "continued"
    assert entries[1]["key"] == "slide-owner:slide_group_shared"
    assert entries[1]["order"] == 2 and entries[1]["completed"] is completed
    assert entries[1]["artifactRels"] == ["slides/slide_07.html", "renders/slide_07.png"]
    assert entries[2]["transaction"]["use"]["args"]["tasks"][0]["task_id"] == "fix-p7"
    assert not any(entry["type"] == "repair" for entry in entries)
    assert result["raw"]["slide_group_shared"][3]["args"]["repair_reason"] == repair_reason
    assert {event["agent"] for event in result["view"]["flat"]} == {"orch"}


@pytest.mark.parametrize("agent", ["slide_07", "slide_group_shared"])
def test_first_segment_local_repair_does_not_hide_owner_card(agent):
    feed = {agent: [
        {"k": "status", "event_type": "agent_started", "seq": 5, "segment_id": "repair-only"},
        {"k": "tool", "tool": "patch", "seq": 6, "segment_id": "repair-only",
         "args": {"repair_reason": "保持主体，只修本页文本换行"}},
        {"k": "text", "seq": 7, "segment_id": "repair-only", "s": "已局部修改。"},
    ]}
    result = run_view(feed)
    assert result["view"]["flat"] == []
    cards = result["view"]["entries"]
    assert len(cards) == 1 and cards[0]["type"] == "slide"
    assert cards[0]["key"] == f"slide-owner:{agent}"
    assert cards[0]["segment"] == "repair-only" and not cards[0]["completed"]
    assert cards[0]["meta"]["label"] == ("第 7 页制作" if agent == "slide_07" else "页面组协作")
    assert result["unchanged"] and result["raw"] == feed


@pytest.mark.parametrize("terminal", ["agent_completed", "agent_failed", None])
def test_owner_card_keeps_identity_order_and_current_state_across_segments(terminal):
    events = [
        {"k": "status", "event_type": "agent_started", "seq": 1, "segment_id": "initial", "task_id": "make"},
        {"k": "status", "event_type": "agent_completed", "seq": 2, "segment_id": "initial", "task_id": "make"},
    ]
    first = run_view({"slide_group_shared": events})["view"]["entries"][0]
    events.extend([
        {"k": "status", "event_type": "agent_started", "seq": 3, "segment_id": "repair", "task_id": "fix"},
        {"k": "tool", "tool": "patch", "seq": 4, "segment_id": "repair", "task_id": "fix",
         "args": {"repair_reason": "修正标签"}},
        {"k": "status", "event_type": "context_compacted", "seq": 5, "segment_id": "repair", "task_id": "fix"},
        {"k": "tool", "tool": "inspect", "seq": 6, "segment_id": "compacted", "task_id": "fix"},
    ])
    if terminal:
        events.append({"k": "status", "event_type": terminal, "seq": 7, "segment_id": "compacted", "task_id": "fix"})
    result = run_view({"slide_group_shared": events})
    cards = result["view"]["entries"]
    assert result["unchanged"] and len(cards) == 1
    card = cards[0]
    assert card["key"] == first["key"] and card["order"] == first["order"]
    assert card["segment"] == "compacted" and card["taskId"] == "fix"
    assert card["completed"] is (terminal == "agent_completed")
    assert card["failed"] is (terminal == "agent_failed")


def test_different_owners_are_not_merged_by_overlapping_page_numbers():
    feed = {key: [{"k": "status", "event_type": "agent_started", "seq": index,
                   "segment_id": "first"}]
            for index, key in enumerate(["slide_group_shared", "slide_group_distinct"], 1)}
    result = run_view(feed)
    assert result["unchanged"] and len(result["view"]["entries"]) == 2
    assert len({entry["key"] for entry in result["view"]["entries"]}) == 2


def test_background_and_orchestrator_recovery_are_attributed_but_offline_audit_is_not_dialogue():
    feed = {
        "orch": [{"k": "provider", "phase": "failed", "seq": 1, "error_summary": "主模型请求失败"}],
        "harness": [{"k": "status", "event_type": "attachment_parse_started", "seq": 2}],
        "content": [{"k": "status", "event_type": "content_qc_started", "seq": 3, "s": "{}"}],
    }
    result = run_view(feed)
    assert [entry["type"] for entry in result["view"]["entries"]] == ["provider", "runtime"]
    assert [entry["meta"]["role"] for entry in result["view"]["entries"]] == ["orch", "harness"]
    assert "content" in result["raw"] and result["unchanged"]


def test_slide_owner_compaction_is_visible_as_runtime_maintenance():
    feed = {"slide_group_shared": [
        {"k": "status", "event_type": "agent_started", "seq": 1, "segment_id": "before"},
        {"k": "provider", "event_type": "provider_started", "phase": "started", "seq": 2,
         "segment_id": "before", "request_kind": "context_compaction"},
        {"k": "status", "event_type": "agent_context_compacted", "seq": 3, "segment_id": "after"},
    ]}
    result = run_view(feed)
    runtime = [entry for entry in result["view"]["entries"] if entry["type"] == "runtime"]
    assert [entry["action"] for entry in runtime] == ["正在压缩上下文", "上下文压缩完成"]
    assert all(entry["runtimeKind"] == "compaction" for entry in runtime)
    assert any(entry["type"] == "slide" for entry in result["view"]["entries"])
    assert result["unchanged"]


def test_progress_renderer_uses_scoped_view_for_cards_current_activity_and_tool_list():
    feed = {"orch": [{"k": "text", "event_id": "message", "seq": 1, "s": "等待已委派页面返回。"}],
            "critic": [{"k": "text", "seq": 2, "s": "独立 Critic 最晚反馈"}]}
    result = run_view(feed, extra_functions=["renderOrchestrationProgress"], body="""
const makeNode=()=>({dataset:{},classList:{add(){},toggle(){}},children:new Map(),
  querySelector(key){if(!this.children.has(key))this.children.set(key,makeNode());return this.children.get(key)},
  closest(){return null},addEventListener(){},textContent:'',innerHTML:'',scrollHeight:0,scrollTop:0,clientHeight:100});
const body=makeNode();body.id='test-progress';body.dataset.progressShell='orchestration';
body.querySelector('[data-material-count]').closest=()=>({});
const editorPresentationKind=()=> 'static', orchestrationTimingMarkup=()=>'', compactOrchestrationCurrentDetail=(_meta,s)=>s;
const orchestrationAgentGlyph=()=>'';
const $$=()=>[], requestAnimationFrame=()=>{};
let shown=[], tools=[];
const reconcileOrchestrationEntries=(_node,rows)=>{shown=rows};
const summarizeToolEvents=()=>[],liveToolTransactions=rows=>{tools=rows;return []};
renderOrchestrationProgress(body,feed,{running:true});
console.log(JSON.stringify({shown,tools,unchanged:before===JSON.stringify(feed),
  current:body.querySelector('.agent-now').querySelector('p').textContent}));
""")
    assert result["unchanged"] and result["current"] == "等待已委派页面返回。"
    assert len(result["shown"]) == 1 and result["shown"][0]["text"] == result["current"]
    assert {row["agent"] for row in result["tools"]} == {"orch"}
