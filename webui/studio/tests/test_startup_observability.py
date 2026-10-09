"""启动可观测性只投影真实事件，不补造模型消息或角色执行。"""
import json
import os
from pathlib import Path
import re
import subprocess

import pytest

from app import trace
from workspace_fixture import write_workspace_manifest


JS_PATH = Path(__file__).resolve().parents[1] / "static/app.js"


def _journal(root, rows):
    write_workspace_manifest(root)
    path = root / "_trace/live-events.jsonl"
    path.parent.mkdir(exist_ok=True)
    events = [{
        "schema": "mural.live-event.v1", "sequence": i, "event_id": f"event-{i}",
        "timestamp": f"2026-09-08T04:29:{i:02d}Z", "agent_id": "orchestrator",
        "role": "orchestrator", "turn": 0, "segment_id": "first", "payload": {}, **row,
    } for i, row in enumerate(rows, 1)]
    path.write_text("".join(json.dumps(row) + "\n" for row in events))
    return path


def _js_source(names):
    source = JS_PATH.read_text()
    functions = []
    for name in names:
        start = re.search(rf"^function {name}\(", source, re.M).start()
        end = re.search(r"^(?:async )?function ", source[start + 1:], re.M)
        functions.append(source[start:start + 1 + end.start()])
    return "\n".join(functions)


def _js(names, body, setup=""):
    output = subprocess.run(["node", "-e", setup + _js_source(names) + body],
                            check=True, capture_output=True, text=True)
    return json.loads(output.stdout)


def test_context_is_retained_in_trace_without_prompt_or_phantom_roles(tmp_path):
    path = _journal(tmp_path, [
        {"event_type": "agent_started"},
        {"event_type": "agent_context", "payload": {
            "system": "private-system-text", "thinking_signature": "private-signature",
            "provenance": {"skill_tree_sha256": "a" * 64}, "api_key": "private-key",
        }},
        {"event_type": "provider_started", "turn": 1, "payload": {
            "model": "27b", "streaming": False, "attempt_id": "first-request",
        }},
    ])
    original = path.read_bytes()
    first = trace.live_events(str(tmp_path), limit=2)
    context = first["events"][1]
    assert context["k"] == "context" and context["s"] == "主 Skill 已注入"
    assert context["skill_version"] == "a" * 12
    assert context["skill_path"] == "skills/mural-presenter-v1/SKILL.md"
    assert context["source"] == "_trace/live-events.jsonl · agent_context"
    assert {row["role"] for row in first["agent_statuses"].values()} == {"orchestrator"}
    assert not any(row["k"] in {"tool", "text", "analysis"} for row in first["events"])
    for secret in ("private-system-text", "private-signature", "private-key"):
        assert secret not in json.dumps(first)
    second = trace.live_events(str(tmp_path), cursor=first["next_cursor"])
    assert len(second["events"]) == 1
    assert second["events"][0]["model"] == "27b"
    assert second["events"][0]["streaming"] is False
    assert path.read_bytes() == original


def test_compaction_lifecycle_is_projected_with_public_metrics(tmp_path):
    _journal(tmp_path, [
        {"event_type": "provider_started", "payload": {
            "model": "teacher", "request_kind": "context_compaction",
            "actual_max_tokens": 16384, "attempt_id": "compact-1",
        }},
        {"event_type": "agent_context_compacted", "segment_id": "after-compact", "payload": {
            "estimated_tokens": 112000, "tokens_before": 112000,
            "tokens_after": 24000, "input_budget": 106496,
            "compact_target_tokens": 32768, "target_met": True,
            "pressure": ["tokens"], "reason": "context_compaction",
        }},
        {"event_type": "agent_context_compaction_failed", "payload": {
            "error_type": "CompactionSummaryError", "error_summary": "summary exceeded budget",
            "recoverable": True, "checkpoint_retained": True,
        }},
    ])

    events = trace.live_events(str(tmp_path))["events"]

    assert events[0]["request_kind"] == "context_compaction"
    assert events[0]["request_kind_label"] == "上下文压缩摘要"
    assert events[1]["s"] == "上下文压缩完成"
    assert events[1]["tokens_before"] == 112000 and events[1]["tokens_after"] == 24000
    assert events[2]["s"] == "上下文压缩未完成"
    assert events[2]["recoverable"] is True and events[2]["checkpoint_retained"] is True


def test_pinned_manifest_does_not_claim_injection_or_research_start(tmp_path):
    _journal(tmp_path, [])
    (tmp_path / "_trace/skill-provenance.json").write_text(json.dumps({
        "main_sha256": "a" * 64, "role_sha256": {"research": "b" * 64},
    }))
    result = trace.live_events(str(tmp_path))
    assert result["events"] == [] and result["agents"] == {}


@pytest.mark.parametrize("system,version", [(None, "a" * 64), ("", "a" * 64), ("prompt", "invalid")])
def test_injection_requires_actual_context_and_skill_identity(tmp_path, system, version):
    _journal(tmp_path, [{"event_type": "agent_context", "payload": {
        "system": system, "provenance": {"skill_tree_sha256": version},
    }}])
    event = trace.live_events(str(tmp_path))["events"][0]
    assert event["k"] == "status" and "skill_path" not in event


def test_owner_context_identifies_its_role_card(tmp_path):
    _journal(tmp_path, [{"event_type": "agent_context", "role": "slide", "agent_id": "slide-a",
                         "payload": {"system": "role text", "provenance": {"skill_tree_sha256": "b" * 64}}}])
    event = trace.live_events(str(tmp_path))["events"][0]
    assert event["skill_path"] == "skills/mural-presenter-v1/roles/slide.md"
    assert event["s"] == "角色 Skill 已注入"


def test_chinese_live_events_hide_default_english_process_text(tmp_path):
    (tmp_path / "deck.md").write_text("# 中文演示\n", encoding="utf-8")
    _journal(tmp_path, [
        {"event_type": "assistant_message", "payload": {
            "message": "Let me inspect the workspace before continuing.",
        }},
        {"event_type": "tool_use", "payload": {
            "name": "read_file",
            "tool_use_id": "call-1",
            "arguments": {"path": "config.json"},
        }},
    ])

    payload = trace.live_events(str(tmp_path))
    assert not any(row.get("k") == "text" for row in payload["events"])
    assert any(row.get("k") == "tool" for row in payload["events"])
    assert payload["next_cursor"] == 2

    detail = trace.agent_trace_payload(str(tmp_path), "orch")
    messages = json.loads(detail["content"])
    assert "Let me inspect" not in detail["content"]
    assert any(
        block.get("type") == "tool_use"
        for message in messages
        for block in message.get("content", [])
    )


def test_resource_acquisition_is_silent_but_wait_and_failure_remain_visible():
    result = _js(["runtimeEventState"], """
console.log(JSON.stringify(['resource_wait_started', 'resource_wait_completed', 'resource_wait_failed']
  .map(event_type => runtimeEventState({event_type}))));
""")
    assert result[0][0] == "等待模型资源"
    assert result[1] is None
    assert result[2][0] == "本地资源等待失败"


def test_current_state_distinguishes_startup_queue_provider_and_tools():
    result = _js(["runtimeEventState", "agentCurrentState"], """
const rows = [
  {k:'status',event_type:'agent_started'},
  {k:'context',event_type:'agent_context',s:'主 Skill 已注入',skill_path:'SKILL.md',skill_version:'aaaa'},
  {k:'status',event_type:'resource_wait_started'},
  {k:'provider',event_type:'provider_started',model:'27b',streaming:false},
  {k:'provider',event_type:'provider_succeeded'},
  {k:'tool',tool:'write_file',hint:'deck.md'},
  {k:'provider',event_type:'provider_raw_request'},
  {k:'tool_result',tool:'write_file',ok:true,duration_ms:8},
];
console.log(JSON.stringify(Array.from({length:rows.length+1}, (_,n) =>
  agentCurrentState(rows.slice(0,n), {running:true,rendered:false,n:0}))));
""", setup="""
const cleanAgentText = s => s || '';
const TOOL_LABEL = {write_file:'写入文件'};
const toolProgress = ev => ['写入文件',ev.hint];
""")
    assert result[0][0] == "等待 Agent 启动记录"
    assert result[1][0] == "Agent 已启动"
    assert result[2] == result[1]
    assert result[3][0] == "等待模型资源" and "尚未发送" in result[3][1]
    assert result[4][0] == "等待模型响应" and "未开启流式" in result[4][1]
    assert result[5][0] == "模型已响应"
    assert result[6] == result[7] == ["写入文件", "deck.md"]
    assert result[8][0] == "写入文件已完成"


def test_context_survives_long_feed_without_duplicates_and_resets():
    result = _js(["mergeIncrementalLiveFeed"], """
const context = {k:'context',seq:1,event_id:'context'};
const rows = [context,...Array.from({length:650},(_,i)=>({k:'tool',seq:i+2,event_id:'t'+i}))];
let feed = mergeIncrementalLiveFeed({}, {orch:rows});
feed = mergeIncrementalLiveFeed(feed, {orch:[context]});
console.log(JSON.stringify({count:feed.orch.length,first:feed.orch[0],
  contexts:feed.orch.filter(e=>e.k==='context').length,
  reset:mergeIncrementalLiveFeed(feed,{orch:[]},{reset:true})}));
""", setup="const evKey=ev=>ev.event_id;\n")
    assert result["count"] == 601 and result["contexts"] == 1
    assert result["first"]["event_id"] == "context"
    assert result["reset"] == {"orch": []}


def test_fixed_version_card_does_not_label_all_roles_as_loaded():
    html = _js(["skillProvenanceMarkup"], """
console.log(JSON.stringify(skillProvenanceMarkup({main_sha256:'aaaa',
  roles:[{role:'research',sha256:'bbbb'}],source:'trace/skill-provenance.json'})));
""", setup="const escapeHtml=s=>String(s);\n")
    assert "已固定主 Skill 版本" in html
    assert "可用角色卡 · 不代表已启动" in html
    assert "已加载" not in html


def test_context_is_hidden_without_removing_messages_tools_or_trace():
    result = _js(["orchestrationFlatFeed", "orchestrationEntries", "runtimeEventState",
                  "agentCurrentState", "workflowCurrentActivity", "progressHistoryItems"], """
const context={k:'context',event_type:'agent_context',s:'角色 Skill 已注入',seq:1};
const feed=Object.fromEntries(['orch','research','image','slide','critic'].map(role=>[role,[context]]));
const hidden=orchestrationEntries(feed,{agents:{}});
const states=Object.fromEntries(Object.entries(feed).map(([role,rows])=>[role,{
  current:agentCurrentState(rows,{running:true,rendered:false,n:0}),
  activity:workflowCurrentActivity(rows)}]));
feed.orch.push({k:'text',event_id:'message',seq:2,s:'我先读取任务文件。'},
  {k:'tool',seq:3,segment_id:'one',tool_use_id:'read-1',tool:'read_file',args:{path:'SKILL.md'}},
  {...context,seq:4},
  {k:'tool_result',seq:5,segment_id:'one',tool_use_id:'read-1',tool:'read_file',ok:true,result:'file contents'});
const visible=orchestrationEntries(feed,{agents:{}});
console.log(JSON.stringify({hidden,states,visible,history:progressHistoryItems(feed.orch),
  retained:feed.orch.filter(e=>e.k==='context').length}));
""", setup="""
const ed={status:'running'}, canonicalOrchestrationAgentKey=key=>key,
  orchestrationAgentMeta=key=>({key,role:key,label:key}), agentTimingFor=()=>null,
  orchestrationEventTime=()=>null, cleanAgentText=s=>s||'', liveAgentText=e=>e.s,
  toolProgress=()=>['读取文件','SKILL.md'], friendlyToolHint=()=>'', workflowArtifactRelsFromEvents=()=>[];
""")
    assert result["hidden"] == {"flat": [], "entries": []}
    assert all(state["current"][0] == "等待 Agent 启动记录" and state["activity"] is None
               for state in result["states"].values())
    assert result["retained"] == 2
    entries = result["visible"]["entries"]
    assert [entry["type"] for entry in entries] == ["message", "orchestrator-action"]
    assert entries[0]["text"] == "我先读取任务文件。"
    assert entries[1]["transaction"]["use"]["args"]["path"] == "SKILL.md"
    assert entries[1]["transaction"]["result"]["result"] == "file contents"
    assert "已注入" not in json.dumps(result["visible"], ensure_ascii=False)
    assert [item["type"] for item in result["history"]] == ["message", "tool"]


def test_startup_progress_browser(tmp_path):
    from app import engine
    from playwright.sync_api import sync_playwright

    _journal(tmp_path, [
        {"event_type": "agent_started"},
        {"event_type": "agent_context", "payload": {
            "system": "synthetic Skill", "provenance": {"skill_tree_sha256": "a" * 64},
        }},
        {"event_type": "provider_started", "payload": {"model": "27b", "streaming": False}},
    ])
    feed = trace.live_events(str(tmp_path))["agents"]
    source = JS_PATH.read_text()
    start = source.index("const ORCHESTRATION_ROLE_LABELS =")
    labels = source[start:source.index("\n};", start) + 3]
    script = """
const ed={status:'running',kind:'static',specialistArtifacts:{agents:{}},pageAgents:{}};
const $$=(s,root=document)=>[...root.querySelectorAll(s)];
const escapeHtml=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const cleanAgentText=s=>s||'', agentPages=()=>[], agentTimingFor=()=>null, orchestrationEventTime=()=>null;
const editorPresentationKind=()=> 'static', orchestrationAgentGlyph=()=>'', orchestrationTimingMarkup=()=>'';
const summarizeToolEvents=()=>[], liveToolTransactions=()=>[];
const setScrollBottomImmediately=node=>{node.scrollTop=node.scrollHeight};
""" + labels + _js_source([
        "canonicalOrchestrationAgentKey", "orchestrationAgentMeta", "orchestrationFlatFeed",
        "runtimeEventState", "orchestrationEntries", "orchestrationEntryContent",
        "reconcileOrchestrationEntries", "orchestrationMainView", "renderOrchestrationProgress",
        "workflowTaskGraph",
    ]) + "\nwindow.draw=feed=>renderOrchestrationProgress(document.querySelector('#progress'),feed,{running:true});"
    browser_env = engine.render_env({**os.environ, "TMPDIR": str(tmp_path)})
    executable = browser_env.get("PPT_SKILL_BROWSER_EXE")
    if not executable:
        pytest.skip("deployed Chromium is not available")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=executable, env=browser_env,
                                     headless=True, args=["--no-sandbox"])
        try:
            page = browser.new_page(viewport={"width": 1024, "height": 740})
            page.route("**/*", lambda route: route.abort())
            page.set_content('<main style="max-width:900px;width:100%;margin:20px auto;padding:16px;box-sizing:border-box"><h2>WebUI 启动展示 · 模拟事件验收</h2><div id="progress" class="outline-agent-progress"></div></main>')
            page.add_style_tag(content=JS_PATH.with_suffix(".css").read_text())
            page.add_script_tag(content=script)
            page.evaluate("window.draw({})")
            assert page.locator(".agent-now strong").inner_text() == "任务启动准备中"
            for role in ("orch", "research", "image", "slide"):
                assert page.locator(f"[data-{role}-count]").inner_text() == "未启动"
            assert page.locator(".orchestration-overview .material").is_hidden()
            page.evaluate("window.draw({material:[]})")
            assert page.locator(".orchestration-overview .material").is_hidden()
            page.evaluate("window.draw", feed)
            page.evaluate("window.draw", feed)
            assert "等待模型响应" in page.locator(".agent-now strong").inner_text()
            assert "未开启流式" in page.locator(".agent-now p").inner_text()
            assert page.locator("[data-orch-count]").inner_text() == "1"
            assert page.locator("[data-research-count]").inner_text() == "未启动"
            assert page.locator(".orch-entry").count() == 0
            assert "已注入" not in page.locator("#progress").inner_text()
            page.screenshot(path=str(tmp_path / "startup-context-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 420, "height": 740})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(tmp_path / "startup-context-narrow.png"), full_page=True)
        finally:
            browser.close()
