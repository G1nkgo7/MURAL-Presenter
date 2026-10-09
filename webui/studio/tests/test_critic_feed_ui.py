"""真实角色归属、完整公开 Trace 与 Orchestrator 原生看图展示。"""
import json

import pytest

from app import trace
from test_startup_observability import _journal, _js


def entries(feed):
    return _js(["orchestrationFlatFeed", "orchestrationEntries"],
        "const feed=" + json.dumps(feed) + ";const before=JSON.stringify(feed);"
        "const result=orchestrationEntries(feed,{agents:{}});"
        "console.log(JSON.stringify({...result,unchanged:before===JSON.stringify(feed)}));",
        setup="""
const ed={}, canonicalOrchestrationAgentKey=key=>key,
  orchestrationAgentMeta=key=>({key,role:key,label:key}),
  orchestrationEventTime=()=>null, liveAgentText=event=>event.s||'';
""")


@pytest.mark.parametrize("kind", ["critic_started", "critic_reused", "critic_completed", "critic_failed",
    "agent_started", "agent_completed", "agent_failed", "agent_context", "resource_wait_started",
    "provider_started", "provider_failed", "provider_failover", "assistant_message", "assistant_message_delta"])
def test_page_critic_has_own_entry_without_impersonating_orchestrator(tmp_path, kind):
    path = _journal(tmp_path, [{"event_type": kind, "role": "critic", "agent_id": "independent-critic",
        "payload": {"inspection_id": "review-p6", "owner_id": "owner-6", "page": 6,
                    "message": "P6 检查反馈", "body": "P6 检查反馈", "completed": True}},
        {"event_type": "assistant_message", "payload": {"message": "整册复核：P6 交回原 Owner 修改。"}}])
    before = path.read_bytes()
    feed = trace.live_events(str(tmp_path))["agents"]
    result = entries(feed)
    assert result["unchanged"] and path.read_bytes() == before
    assert len(feed["critic"]) == 1
    assert len(result["entries"]) == 2
    assert result["entries"][0]["type"] == "critic-review"
    assert result["entries"][0]["meta"]["role"] == "critic"
    assert result["entries"][1]["text"] == "整册复核：P6 交回原 Owner 修改。"


def test_critic_only_feed_does_not_create_empty_orchestrator_bubbles():
    result = entries({"critic": [{"k": "status"}, {"k": "text", "s": "公开反馈"}]})
    assert len(result["entries"]) == 1
    assert result["entries"][0]["messages"] == ["公开反馈"]
    assert result["entries"][0]["type"] == "critic-review"
    assert len(result["flat"]) == 2 and result["unchanged"]


def test_paginated_public_trace_retains_identity_without_private_context(tmp_path):
    identity = {"inspection_id": "review-p6", "owner_id": "owner-6", "page": 6, "png_sha256": "a" * 64}
    common = {"role": "critic", "agent_id": "critic-6", "segment_id": "segment-6"}
    path = _journal(tmp_path, [
        {**common, "event_type": "agent_context", "payload": {"metadata": identity, "system": "private-system"}},
        {**common, "event_type": "assistant_message", "payload": {"message": "P6 标题重叠。"}},
    ])
    before = path.read_bytes()
    event = trace.live_events(str(tmp_path), cursor=1)["events"][0]
    assert event["critic"] == identity and event["s"] == "P6 标题重叠。"
    assert "private-system" not in json.dumps(event)
    assert path.read_bytes() == before


def test_review_lifecycle_is_one_card_with_exact_critic_trace(tmp_path):
    rows = []
    for page in (6, 9):
        identity = {"inspection_id": f"inspection-{page}", "owner_id": f"owner-{page}",
                    "page": page, "png_sha256": str(page) * 64}
        reviewer = {"role": "critic", "agent_id": f"critic-{page}", "segment_id": f"segment-{page}"}
        rows.extend([
            {"event_type": "critic_started", "role": "critic", "agent_id": "external-visual-critic",
             "segment_id": "", "payload": identity},
            {**reviewer, "event_type": "agent_context", "payload": {"metadata": identity}},
            {**reviewer, "event_type": "provider_started"},
            {**reviewer, "event_type": "assistant_message", "payload": {"message": f"仅 P{page} 的反馈"}},
            {**reviewer, "event_type": "provider_succeeded"},
            {"event_type": "critic_completed", "role": "critic", "agent_id": "external-visual-critic",
             "segment_id": "", "payload": {**identity, "body": f"仅 P{page} 的反馈", "completed": True,
                 "source_refs": {"agent_id": f"critic-{page}", "segment_id": f"segment-{page}", "api_key": "private-key"}}},
        ])
    path = _journal(tmp_path, rows)
    before = path.read_bytes()
    feed = trace.live_events(str(tmp_path))["agents"]
    result = entries(feed)
    assert len(result["entries"]) == 2
    for entry, page in zip(result["entries"], (6, 9)):
        assert entry["meta"]["key"] == f"critic-{page}"
        assert entry["segment"] == f"segment-{page}"
        assert entry["info"]["owner_id"] == f"owner-{page}"
        assert entry["state"] == "complete"
        assert entry["messages"] == [f"仅 P{page} 的反馈"]
    # 摘要卡只投影具名的 source_refs，不复制整个来源对象。
    assert "private-key" not in json.dumps(feed)
    assert path.read_bytes() == before


def test_identical_inspection_does_not_merge_different_owners_or_pixels():
    result = entries({"critic": [{"k": "status", "event_type": "critic_started", "seq": n,
        "critic": {"inspection_id": "same", "owner_id": owner, "png_sha256": digest}}
        for n, (owner, digest) in enumerate((("owner-a", "a"), ("owner-b", "a"), ("owner-a", "b")), 1)]})
    assert len(result["entries"]) == 3


def test_each_critic_has_own_complete_trace_and_not_another_reviewers_context(tmp_path):
    rows = [{"event_type": "assistant_message", "payload": {"message": "Orchestrator 自己的判断"}}]
    for page in (6, 9):
        reviewer = {"role": "critic", "agent_id": f"critic-{page}", "segment_id": f"segment-{page}"}
        rows.extend([
            {**reviewer, "event_type": "agent_context", "payload": {
                "metadata": {"page": page, "owner_id": f"owner-{page}"},
                "messages": [{"role": "user", "content": f"P{page} 输入"}]}},
            {**reviewer, "event_type": "assistant_message", "payload": {"message": f"P{page} 完整反馈"}},
        ])
    path = _journal(tmp_path, rows)
    before = path.read_bytes()
    payload = trace.agent_trace_payload(str(tmp_path), "critic-6")
    assert payload["agent"]["key"] == "critic-6"
    assert {item["key"] for item in payload["agents"]} == {"orch", "critic-6", "critic-9"}
    assert "P6 输入" in payload["content"] and "P6 完整反馈" in payload["content"]
    assert "P9" not in payload["content"] and "Orchestrator 自己的判断" not in payload["content"]
    assert len(payload["message_times"]) == len(json.loads(payload["content"])) == 2
    assert payload["message_times"][0] is None
    events = trace.agent_trace_payload(str(tmp_path), "critic-6", source="events")
    assert all(event["agent_id"] == "critic-6" for event in json.loads(events["content"]))
    pooled = trace.agent_trace_payload(str(tmp_path), "critic")
    assert all(segment["owner_action"] == "create" for segment in pooled["segments"])
    assert path.read_bytes() == before


TRACE_RENDER_FUNCTIONS = ["workflowTraceClip", "workflowTraceSafeJson", "workflowTraceResultParts",
    "liveReadFileSummary", "liveTerminalLabel", "liveToolTransactionMarkup"]
TRACE_RENDER_SETUP = """
const TOOL_LABEL={}, escapeHtml=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
  compactTraceValue=s=>String(s||'');
"""


@pytest.mark.parametrize("scope,label", [("deck", "整册视觉复核"), ("page", "页面视觉复核")])
def test_orchestrator_visual_tools_are_labelled_but_do_not_claim_pass(scope, label):
    result = _js(TRACE_RENDER_FUNCTIONS, """
const tx={id:'view',agent:'orch',tool:'vision_analyze',use:{args:{scope:SCOPE,path:'renders/contact-sheet.png',question:'检查整册一致性'}},result:{ok:true,result:{native_multimodal:true}}};
console.log(JSON.stringify({orch:liveToolTransactionMarkup(tx),image:liveToolTransactionMarkup({...tx,agent:'image'})}));
""".replace("SCOPE", json.dumps(scope)), setup=TRACE_RENDER_SETUP)
    assert f"Orchestrator · {label}" in result["orch"]
    assert "判断见本 Agent 后续消息" in result["orch"]
    assert "不是独立 Critic" in result["orch"]
    assert "复核通过" not in result["orch"]
    assert "Orchestrator" not in result["image"]


@pytest.mark.parametrize("args,label", [
    ({"page": 2}, "单页视觉检查"),
    ({"page": [2]}, "单页视觉检查"),
    ({"page": [1, 2]}, "多页联系表检查"),
    ({"path": "assets/figure.png"}, "图片检查"),
])
def test_unified_inspect_displays_actual_target_without_claiming_quality_pass(args, label):
    result = _js(TRACE_RENDER_FUNCTIONS, """
const tx={id:'view',agent:'orch',tool:'inspect',use:{args:ARGS},result:{ok:true,result:{native_multimodal:true}}};
console.log(JSON.stringify({orch:liveToolTransactionMarkup(tx),image:liveToolTransactionMarkup({...tx,agent:'image'})}));
""".replace("ARGS", json.dumps(args)), setup=TRACE_RENDER_SETUP)
    assert f"Orchestrator · {label}" in result["orch"]
    assert "执行成功不等于质量通过" in result["orch"]
    assert "只读复核不取得修改权" in result["orch"]
    assert "不是独立 Critic" not in result["orch"]
    assert "复核通过" not in result["orch"]
    assert "Orchestrator" not in result["image"]


def test_full_text_arguments_and_results_are_not_silently_clipped():
    result = _js(TRACE_RENDER_FUNCTIONS + ["workflowTraceBlockMarkup"], """
const long='x'.repeat(20000)+'末尾不能丢', tool={id:'write',tool:'write_file',use:{args:{path:'slides/slide_01.html',content:long}},result:{ok:true,result:{text:long}}};
console.log(JSON.stringify({tool:liveToolTransactionMarkup(tool),text:workflowTraceBlockMarkup({type:'text',text:long},new Map()),
  result:workflowTraceResultParts({text:long}),json:workflowTraceSafeJson({content:long,signature:'secret-signature'})}));
""", setup=TRACE_RENDER_SETUP + "const agentMarkdownHtml=s=>escapeHtml(s);\n")
    assert result["tool"].count("末尾不能丢") == 2
    assert "末尾不能丢" in result["text"] and "末尾不能丢" in result["json"]
    assert "末尾不能丢" in result["result"]["text"]
    assert "secret-signature" in result["json"]
    assert "截断" not in result["tool"] + result["text"]


def test_overview_recognizes_orchestrator_vision_before_verification_without_inventing_pass():
    result = _js(["workflowOverview", "liveToolTransactions"], """
const use={k:'tool',tool:'vision_analyze',tool_use_id:'view',args:{scope:'deck',path:'renders/contact-sheet.png'}};
const done={k:'tool_result',tool:'vision_analyze',tool_use_id:'view',ok:true};
const overview=events=>workflowOverview({nodes:[]},{status:'running',feed:{orch:events}}).phases.find(p=>p.id==='review');
console.log(JSON.stringify({before:overview([]),running:overview([use]),returned:overview([use,done]),
  imageOnly:overview([{...use,args:{scope:'asset',path:'assets/hero.png'}},done])}));
""", setup="const workflowExecutionStatus=s=>s||'unknown',workflowCurrentActivity=()=>null,workflowStatusLabel=s=>s;\n")
    assert result["before"]["status"] == result["imageOnly"]["status"] == "unknown"
    assert result["running"]["status"] == "running"
    assert result["returned"]["status"] == "observed"
    assert "Orchestrator 整册视觉复核" in result["returned"]["detail"]
    assert "尚无整册验收回执" in result["returned"]["detail"]


@pytest.mark.parametrize("pages,label", [(1, "页面"), ([1], "页面"), ([1, 2], "多页联系表")])
def test_overview_reports_actual_inspect_scope_for_single_and_multiple_targets(pages, label):
    result = _js(["workflowOverview", "liveToolTransactions"], """
const events=[{k:'tool',tool:'inspect',tool_use_id:'view',args:{page:PAGES}},
  {k:'tool_result',tool:'inspect',tool_use_id:'view',ok:true}];
console.log(JSON.stringify(workflowOverview({nodes:[]},{status:'running',feed:{orch:events}}).phases.find(p=>p.id==='review')));
""".replace("PAGES", json.dumps(pages)), setup="const workflowExecutionStatus=s=>s||'unknown',workflowCurrentActivity=()=>null,workflowStatusLabel=s=>s;\n")
    assert result["status"] == "observed"
    assert f"Orchestrator {label}视觉复核" in result["detail"]
    assert "尚无整册验收回执" in result["detail"]


def test_trace_selector_and_complete_expansion_browser(tmp_path):
    import os
    from app import engine
    from playwright.sync_api import sync_playwright
    from test_startup_observability import _js_source, JS_PATH

    env = engine.render_env({**os.environ, "TMPDIR": str(tmp_path)})
    executable = env.get("PPT_SKILL_BROWSER_EXE")
    if not executable:
        pytest.skip("deployed Chromium is not available")
    functions = TRACE_RENDER_FUNCTIONS + ["workflowTraceBlockMarkup", "workflowTraceByteLabel",
        "workflowTraceSegmentLabel", "renderWorkflowAgentTrace", "bindWorkflowTraceControls"]
    script = TRACE_RENDER_SETUP + _js_source(functions) + """
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)],agentMarkdownHtml=s=>escapeHtml(s);
const renderWorkflowImageOutputs=()=>{},renderWorkflowProviderAttempts=()=>{},renderWorkflowTraceContent=()=>{},
  openWorkflowAgentTrace=key=>window.selectedAgent=key;
const ed={workflowTraceAgent:'orch',workflowTracePayload:{agent:{key:'orch',label:'orchestrator',role:'orchestrator'},
  agents:[{key:'orch',label:'orchestrator',role:'orchestrator'},{key:'critic-6',label:'critic-6',role:'critic',page:6}],
  segments:[{id:'segment-1',segment_id:'segment-1',sources:[{id:'messages'}]}],selected_segment:'segment-1',source:'messages'}};
renderWorkflowAgentTrace();bindWorkflowTraceControls();
document.querySelector('#content').innerHTML=liveToolTransactionMarkup({id:'view',agent:'orch',tool:'vision_analyze',
  use:{args:{scope:'deck',path:'renders/contact-sheet.png',question:'检查问题 '+ 'x'.repeat(20000)+'问题末尾'}},
  result:{ok:true,result:{native_multimodal:true}}})+workflowTraceBlockMarkup({type:'text',text:'y'.repeat(5000)+'判断末尾'},new Map());
"""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=executable, env=env, headless=True, args=["--no-sandbox"])
        try:
            page = browser.new_page(viewport={"width": 960, "height": 700})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/*", lambda route: route.abort())
            page.set_content('<div class="workflow-trace-controls"><label class="workflow-trace-agent-select"><select id="workflow-trace-agent"></select></label><label><select id="workflow-trace-segment"></select></label></div><main id="content"></main>')
            page.add_style_tag(content=JS_PATH.with_suffix('.css').read_text())
            page.add_script_tag(content=script)
            assert page.locator('#workflow-trace-agent option').count() == 2
            assert page.locator('#workflow-trace-segment option').first.inner_text() == '完整轨迹（跨压缩 / 续接段）'
            page.select_option('#workflow-trace-agent', 'critic-6')
            assert page.evaluate('window.selectedAgent') == 'critic-6'
            assert 'Orchestrator · 整册视觉复核' in page.locator('.live-transaction summary').inner_text()
            page.locator('.live-transaction summary').click()
            assert '问题末尾' in page.locator('.live-transaction-detail').inner_text()
            assert '判断末尾' in page.locator('.workflow-trace-message-text').inner_text()
            assert not errors
        finally:
            browser.close()
