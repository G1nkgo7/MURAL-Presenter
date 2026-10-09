"""制作品牌页与折叠消息仅投影真实页数、任务及工具回执。"""
import json

import pytest

from test_startup_observability import _js, _js_source


def making(status="running", total=4):
    return _js(["escapeHtml", "workflowExecutionStatus", "workflowStatusLabel", "muralMakingView", "muralMakingMarkup"],
        "const view=muralMakingView();console.log(JSON.stringify({view,html:muralMakingMarkup(view,true)}));",
        setup="const ed={status:" + json.dumps(status) + ",total:" + str(total) + """,rendered:new Set([1,3]),muralRuntime:{tasks:[
          {role:'slide',pages:[1],status:'succeeded'}, {role:'slide',pages:[2],status:'running'},
          {role:'slide',pages:[3],status:'failed'}, {role:'slide',pages:[4],status:'waiting_dependencies'}]}};
          const isStaticActiveStatus=status=>status==='running';
""")


def tool(result=True, active=True):
    return _js(["escapeHtml", "compactTraceValue", "liveReadFileSummary", "liveTerminalLabel", "liveToolTransactionMarkup"],
        "console.log(JSON.stringify(liveToolTransactionMarkup({id:'call-1',tool:'web_search',use:{args:{query:'唯一参数'}},result:"
        + ("{ok:true,duration_ms:120,result:'唯一结果'}" if result else "null")
        + "},{compact:true,active:" + json.dumps(active) + "})));",
        setup="const TOOL_LABEL={web_search:'网络搜索 / Web search'}, workflowTraceSafeJson=JSON.stringify;")


def test_making_progress_counts_previews_not_inspections_or_task_completions():
    result = making()
    assert result["view"]["previews"] == 2 and result["view"]["total"] == 4
    assert [row["state"] for row in result["view"]["rows"]] == ["complete", "running", "failed", "waiting_dependencies"]
    assert "2 / 4" in result["html"] and "50%" in result["html"]
    assert "不代表视觉检查或整册验收通过" in result["html"]
    assert "MURAL" in result["html"] and "/static/logo.svg" in result["html"]
    assert "Cheso" not in result["html"]


def test_unknown_page_count_and_stopped_run_have_no_invented_progress():
    result = making(total=0)
    assert "<progress" not in result["html"] and "%" not in result["html"]
    assert "页数尚未确定" in result["html"]
    result = making(status="interrupted")
    assert "mural-making-hero active" not in result["html"]
    assert "state-running" not in result["html"]
    assert result["view"]["rows"][1]["state"] == "interrupted"
    assert "当前未继续生成" in result["html"]


def test_preview_loading_reuses_brand_without_duplicate_page_checklist():
    result = _js(["escapeHtml", "muralMakingMarkup"], """
const html=muralMakingMarkup({active:true,status:'制作中',total:10,previews:0,rows:[]},true,{checklist:false});
console.log(JSON.stringify(html));
""")
    assert 'mural-making-hero active' in result and 'MURAL Logo' in result
    assert '<progress value="0" max="10"' in result
    assert 'mural-page-checklist' not in result


def test_compact_tool_does_not_duplicate_params_and_keeps_missing_receipt_unknown():
    html = tool()
    assert html.count("唯一参数") == 1 and html.count("唯一结果") == 1
    assert "data-detail-key=\"tool-call-1\"" in html
    html = tool(result=False, active=False)
    assert "未收到结束回执" in html and "succeeded" not in html and "active running" not in html


def test_tool_detail_pretty_prints_json_strings_without_double_escaping():
    result = _js(["workflowTraceClip", "workflowTraceSafeJson"], """
const encoded=JSON.stringify({directories:[{path:'.',returncode:0,stdout:'slides\\n_state'}],truncated:false});
console.log(JSON.stringify({json:workflowTraceSafeJson(encoded),text:workflowTraceSafeJson('plain output')}));
""")
    assert result["json"].startswith("{\n  \"directories\": [")
    assert '\\\"directories\\\"' not in result["json"]
    assert result["text"] == "plain output"


def test_compaction_runtime_state_explains_trigger_and_completion():
    result = _js(["runtimeEventState"], """
console.log(JSON.stringify({
  started:runtimeEventState({event_type:'provider_started',request_kind:'context_compaction',model:'teacher',actual_max_tokens:16384}),
  completed:runtimeEventState({event_type:'agent_context_compacted',tokens_before:112000,tokens_after:24000,input_budget:106496}),
  failed:runtimeEventState({event_type:'agent_context_compaction_failed',error_summary:'summary exceeded budget',recoverable:true})
    }));
""")
    assert result["started"][0] == "正在压缩上下文"
    assert "不是任务正文" in result["started"][1]
    assert result["completed"][0] == "上下文压缩完成"
    assert "112,000 tokens" in result["completed"][1] and "24,000 tokens" in result["completed"][1]
    assert result["failed"][0] == "上下文压缩未完成" and "可恢复" in result["failed"][1]


def read_summary(receipt, *, args=None, ok=True, active=True, pending=False):
    transaction = {"id": "read-1", "tool": "read_file", "use": {"args": args or {"path": "references/plan-contract.md"}},
                   "result": None if pending else {"ok": ok, "duration_ms": 411, "result": receipt}}
    return _js(["escapeHtml", "compactTraceValue", "liveReadFileSummary", "liveTerminalLabel", "liveToolTransactionMarkup"],
        "const transaction=" + json.dumps(transaction) + ";const active=" + json.dumps(active) + ";"
        "console.log(JSON.stringify({summary:liveReadFileSummary(transaction,active),"
        "html:liveToolTransactionMarkup(transaction,{compact:true,active})}));",
        setup="const TOOL_LABEL={read_file:'研读资料'}, workflowTraceSafeJson=JSON.stringify;")


@pytest.mark.parametrize("encoded", [False, True])
def test_read_file_shows_filename_and_full_receipt_without_inferring_understanding(encoded):
    receipt = {"mode": "line_range", "offset": 1, "line_count": 120, "returned_line_count": 120, "next_offset": None}
    result = read_summary(json.dumps(receipt) if encoded else receipt)
    assert result["summary"]["progress"] == "全文已返回 · 120 / 120 行"
    summary = result["html"].split("</summary>")[0]
    assert "读取文件 · plan-contract.md" in summary
    assert 'title="references/plan-contract.md"' in summary
    assert "全文已返回 · 120 / 120 行" in summary and "411 ms" in summary
    assert "不代表模型已理解或落实" in summary


@pytest.mark.parametrize("receipt,args,expected", [
    ({"mode": "line_range", "offset": 1, "line_count": 350, "returned_line_count": 100, "next_offset": 101}, None,
     "本次部分读取 · 第 1–100 行 / 共 350 行 · 可从第 101 行继续"),
    ({"mode": "line_range", "offset": 251, "line_count": 350, "returned_line_count": 100, "next_offset": None}, None,
     "本次部分读取 · 第 251–350 行 / 共 350 行"),
    ({"mode": "search_snippets", "offset": 1, "line_count": 120, "returned_line_count": 5, "next_offset": None}, None,
     "搜索片段 · 返回 5 / 120 行 · 不代表全文"),
    ({"mode": "line_range", "offset": 1, "line_count": 5, "returned_line_count": 5, "next_offset": None},
     {"path": "research.md", "query": "evidence"}, "搜索片段 · 返回 5 / 5 行 · 不代表全文"),
    ({"mode": "line_range", "offset": 1, "line_count": 0, "returned_line_count": 0, "next_offset": None}, None,
     "全文已返回 · 空文件（0 行）"),
    ({"mode": "line_range", "offset": 200, "line_count": 120, "returned_line_count": 0, "next_offset": None}, None,
     "未返回正文 · 文件共 120 行"),
])
def test_read_ranges_do_not_confuse_eof_or_search_with_a_full_read(receipt, args, expected):
    assert read_summary(receipt, args=args)["summary"]["progress"] == expected


@pytest.mark.parametrize("options,expected", [
    ({"pending": True}, "读取中 · 等待回执"),
    ({"pending": True, "active": False}, "读取中断 · 范围未确认"),
    ({"ok": False}, "读取失败 · 范围未确认"),
    ({}, "已返回 · 读取范围未提供"),
])
def test_read_failure_and_missing_metadata_never_claim_a_complete_read(options, expected):
    result = read_summary('truncated {"content":', **options)
    assert result["summary"]["progress"] == expected
    assert "全文已返回" not in result["html"]


def test_read_file_path_is_escaped_and_truncated_receipt_is_not_complete():
    result = read_summary({"mode": "line_range", "offset": 1, "line_count": 10,
                           "returned_line_count": 10, "next_offset": None, "truncated": True},
                          args={"path": 'references/<img src=x onerror="alert(1)">.md'})
    assert "全文已返回" not in result["html"] and "<img" not in result["html"]
    assert "&lt;img" in result["html"]


@pytest.mark.parametrize("tool,label", [("patch", "调整页面"), ("write_file", "写入文件")])
@pytest.mark.parametrize("phase,status", [("completed", "完成 · 224 ms"), ("failed", "失败"),
                                         ("running", "运行中"), ("interrupted", "未收到结束回执")])
def test_edit_cards_show_filename_and_preserve_result_state(tool, label, phase, status):
    transaction = {"id": "edit-1", "tool": tool, "use": {"args": {
        "path": "slides/slide_04.html", "new_text": "updated title"}},
        "result": {"ok": phase == "completed", "duration_ms": 224,
                   "result": {"path": "slides/slide_04.html"}}
        if phase in {"completed", "failed"} else None}
    html = _js(["escapeHtml", "compactTraceValue", "liveReadFileSummary", "liveTerminalLabel", "liveToolTransactionMarkup"],
        "console.log(JSON.stringify(liveToolTransactionMarkup(" + json.dumps(transaction)
        + ",{compact:true,active:" + json.dumps(phase != "interrupted") + "})));",
        setup="const TOOL_LABEL={patch:'调整页面',write_file:'写入文件'}, workflowTraceSafeJson=JSON.stringify;")
    summary = html.split("</summary>")[0]
    assert f"{label} · slide_04.html" in summary
    assert 'title="slides/slide_04.html"' in summary
    assert status in summary
    assert html.count("updated title") == 1 and "调用参数" in html
    assert "updated title" not in summary


def test_edit_card_paths_are_not_guessed_and_are_escaped():
    html = _js(["escapeHtml", "compactTraceValue", "liveReadFileSummary", "liveTerminalLabel", "liveToolTransactionMarkup"], """
const draw=t=>liveToolTransactionMarkup({id:'edit',tool:'patch',...t},{compact:true});
console.log(JSON.stringify([
  draw({use:{args:{}},result:{ok:false,error_code:'missing_path'}}),
  draw({result:{ok:true,result:{path:'styles/base.css'}}}),
  draw({use:{args:{path:'slides/<img onerror="alert(1)">.html'}}})]));
""", setup="const TOOL_LABEL={patch:'调整页面'}, workflowTraceSafeJson=JSON.stringify;")
    assert "调整页面 · 文件名未记录" in html[0] and "失败" in html[0]
    assert 'title="styles/base.css"' in html[1] and "调整页面 · base.css" in html[1]
    assert "<img" not in html[2] and "&lt;img" in html[2]


@pytest.mark.parametrize("command,label", [
    ("python skills/mural-presenter-v1/scripts/slide.py render . --pages 2", "slide.py render"),
    ("python3 skills/mural-presenter-v1/scripts/slide.py render-group . --pages 1,2", "slide.py render-group"),
    ('python skills/mural-presenter-v1/scripts/image.py crop . --notes "render; inspect"', "image.py crop"),
    ("python skills/mural-presenter-v1/scripts/orchestrator.py scaffold .", "orchestrator.py scaffold"),
    ('"/runtime env/bin/python3.11" "skills/test skill/scripts/image.py" register-user .', "image.py register-user"),
    ("python 'skills/test skill/scripts/orchestrator.py' --help", "orchestrator.py --help"),
    ("ls -la assets", "查看目录 · assets"),
    ('ls -a -l "references/style cards" assets/vendor', "查看目录 · references/style cards · assets/vendor"),
    ("ls", "查看目录 · ."),
    ('ls -- "-archived pages"', "查看目录 · -archived pages"),
    (None, "命令未记录"),
    (" ", "命令未记录"),
    ("python 'unclosed path", "命令见详情"),
    ("python -c 'print(1)'", "命令见详情"),
    ("bash -lc 'python slide.py render'", "命令见详情"),
])
def test_terminal_cards_show_script_and_action_from_call_arguments(command, label):
    transaction = {"id": "terminal-1", "tool": "terminal", "use": {"args": {"command": command}},
                   "result": {"ok": True, "duration_ms": 835, "result": "render completed"}}
    html = _js(["escapeHtml", "compactTraceValue", "liveReadFileSummary", "liveTerminalLabel", "liveToolTransactionMarkup"],
        "console.log(JSON.stringify(liveToolTransactionMarkup(" + json.dumps(transaction) + ",{compact:true})));",
        setup="const TOOL_LABEL={terminal:'运行制作工具'}, workflowTraceSafeJson=JSON.stringify;")
    summary = html.split("</summary>")[0]
    assert label in summary and "完成 · 835 ms" in summary
    assert "render completed" not in summary and "调用参数" in html
    assert "--notes" not in summary and "skills/" not in summary


@pytest.mark.parametrize("phase,status", [("completed", "完成 · 835 ms"), ("failed", "失败 · terminal_parse_failed"),
                                         ("running", "运行中"), ("interrupted", "未收到结束回执")])
def test_terminal_command_label_keeps_result_state_and_escapes_markup(phase, status):
    transaction = {"id": "terminal-2", "agent": "slide", "tool": "terminal", "use": {"args": {
        "command": 'python "skills/<img onerror=alert(1)>.py" render . --notes "private long argument"'}},
        "result": {"ok": phase == "completed", "duration_ms": 835, "error_code": "terminal_parse_failed"}
        if phase in {"completed", "failed"} else None}
    html = _js(["escapeHtml", "compactTraceValue", "liveReadFileSummary", "liveTerminalLabel", "liveToolTransactionMarkup"],
        "console.log(JSON.stringify(liveToolTransactionMarkup(" + json.dumps(transaction)
        + ",{compact:true,showAgent:true,active:" + json.dumps(phase != "interrupted") + "})));",
        setup="const TOOL_LABEL={terminal:'运行制作工具'}, workflowTraceSafeJson=JSON.stringify,"
              "orchestrationAgentMeta=()=>({label:'P2 制作'});")
    summary = html.split("</summary>")[0]
    assert "P2 制作 · 运行制作工具 · &lt;img" in summary and ".py render" in summary
    assert status in summary and "<img" not in html
    assert "private long argument" not in summary and html.count("private long argument") == 1


@pytest.mark.parametrize(("tasks", "label"), [
    ([{"task_id": "research", "role": "research"}], "拉起同事 · Research"),
    ([{"task_id": "image", "role": "image"}, {"task_id": "p1", "role": "slide"},
      {"task_id": "p2", "role": "slide"}], "拉起同事 · Image · Slide × 2"),
    ([{"task_id": "repair", "role": "slide", "repair": True}], "拉起同事 · Slide"),
])
def test_delegate_submit_card_lists_structured_task_roles(tasks, label):
    transaction = {"id": "delegate-1", "agent": "orch", "tool": "delegate_submit",
                   "use": {"args": {"tasks": tasks}},
                   "result": {"ok": True, "duration_ms": 42, "result": {"accepted": len(tasks)}}}
    html = _js(["escapeHtml", "compactTraceValue", "liveReadFileSummary", "liveTerminalLabel",
                "liveToolTransactionMarkup"],
        "console.log(JSON.stringify(liveToolTransactionMarkup(" + json.dumps(transaction) + ",{compact:true})));",
        setup="const TOOL_LABEL={}, workflowTraceSafeJson=JSON.stringify;")
    summary = html.split("</summary>")[0]
    assert label in summary and "提交已处理 · 42 ms" in summary and "Orchestrator" not in summary


@pytest.mark.parametrize("active", [True, False])
@pytest.mark.parametrize("args,code", [
    ({"tasks": '[{"task_id":"image-1","role":"image"}]'}, "invalid_arguments"),
    ({"tasks": '[{"goal":"<img src=x> "bad quotes""}]'}, "invalid_arguments"),
    ({"tasks": [None]}, "invalid_arguments"),
    ({}, "missing_arguments"),
    ({"tasks": [{"task_id": "image-1", "role": "image", "unexpected": True}]}, "unknown_arguments"),
])
def test_delegate_submit_format_failure_does_not_claim_a_role_or_started_task(args, code, active):
    transaction = {"id": "delegate-invalid", "agent": "orch", "tool": "delegate_submit",
                   "use": {"args": args}, "result": {"ok": False, "error_code": code,
                   "result": "$.tasks must be an array; received string"}}
    result = _js(["escapeHtml", "compactTraceValue", "liveReadFileSummary", "liveTerminalLabel",
                  "liveToolTransactionMarkup"],
        "const tx=" + json.dumps(transaction) + ";const before=JSON.stringify(tx);"
        "console.log(JSON.stringify({html:liveToolTransactionMarkup(tx,{compact:true,active:"
        + json.dumps(active) + "}),unchanged:before===JSON.stringify(tx)}));",
        setup="const TOOL_LABEL={}, workflowTraceSafeJson=JSON.stringify, ed={muralRuntime:{tasks:["
              "{task_id:'image-1',role:'image',status:'failed',error:'old failure'}]}};")
    html = result["html"]
    summary = html.split("</summary>")[0]
    assert result["unchanged"]
    assert "委派参数格式错误，任务未启动" in summary
    assert f"失败 · {code}" in summary
    assert "Role 未记录" not in html and "拉起同事" not in summary
    assert "同事任务失败" not in html and "old failure" not in html
    assert "received string" in html and "<img" not in html


@pytest.mark.parametrize("code", ["tool_permission_denied", "delegation_rejected", "handler_error"])
def test_other_delegation_failures_preserve_their_error_kind(code):
    transaction = {"id": "delegate-error", "agent": "orch", "tool": "delegate_submit",
                   "use": {"args": {"tasks": [{"task_id": "image-1", "role": "image"}]}},
                   "result": {"ok": False, "error_code": code, "result": "actual failure"}}
    html = _js(["escapeHtml", "compactTraceValue", "liveReadFileSummary", "liveTerminalLabel",
                "liveToolTransactionMarkup"],
        "console.log(JSON.stringify(liveToolTransactionMarkup(" + json.dumps(transaction) + ",{compact:true})));",
        setup="const TOOL_LABEL={}, workflowTraceSafeJson=JSON.stringify;")
    assert f"失败 · {code}" in html and "actual failure" in html
    assert "委派参数格式错误" not in html and "任务未启动" not in html


def test_delegate_submit_card_tracks_later_task_failure():
    transaction = {"id": "delegate-failed", "agent": "orch", "tool": "delegate_submit",
                   "use": {"args": {"tasks": [{"task_id": "slide-1", "role": "slide"}]}},
                   "result": {"ok": True, "duration_ms": 1601, "result": {"accepted": 1}}}
    html = _js(["escapeHtml", "compactTraceValue", "liveReadFileSummary", "liveTerminalLabel",
                "liveToolTransactionMarkup"],
        "console.log(JSON.stringify(liveToolTransactionMarkup(" + json.dumps(transaction) + ",{compact:true})));",
        setup="const TOOL_LABEL={}, workflowTraceSafeJson=JSON.stringify, ed={muralRuntime:{tasks:["
              "{task_id:'slide-1',role:'slide',status:'failed',"
              "error:'SkillRuntimeError: slide_scaffold_missing: page 1'}]}};")
    summary = html.split("</summary>")[0]
    assert "拉起同事 · Slide" in summary
    assert "同事任务失败 · slide_scaffold_missing · 1,601 ms" in summary
    assert "class=\"agent-action live-transaction failed" in html and "<summary><span>!</span>" in html
    assert "同事任务失败：SkillRuntimeError: slide_scaffold_missing: page 1" in html


def test_inline_tool_pairing_does_not_cross_agent_or_segment():
    result = _js(["escapeHtml", "orchestrationFlatFeed", "orchestrationEntries", "orchestrationEntryContent",
                  "compactTraceValue", "liveReadFileSummary", "liveTerminalLabel", "liveToolTransactionMarkup"], """
const feed={orch:[{k:'tool',seq:1,segment_id:'one',tool_use_id:'same-id',tool:'web_search',args:{query:'来源'}},
  {k:'tool_result',seq:3,segment_id:'one',tool_use_id:'same-id',tool:'web_search',ok:true,result:'matched'},
  {k:'tool_result',seq:4,segment_id:'two',tool_use_id:'same-id',tool:'web_search',ok:false,result:'wrong-segment'}],
other:[{k:'tool_result',seq:2,segment_id:'one',tool_use_id:'same-id',tool:'web_search',ok:false,result:'wrong-agent'}]};
const entries=orchestrationEntries(feed,{agents:{}}).entries;
console.log(JSON.stringify({entries,html:orchestrationEntryContent(entries[0])}));
""", setup="""
const ed={status:'running'}, canonicalOrchestrationAgentKey=key=>key,
orchestrationAgentMeta=key=>({key,role:key==='orch'?'orch':'other'}),
orchestrationEventTime=()=>null, agentTimingFor=()=>null, orchestrationAgentGlyph=()=>'',
toolProgress=()=>['网络搜索','检索来源'], friendlyToolHint=()=>'', workflowArtifactRelsFromEvents=()=>[],
editorPresentationKind=()=> 'static', isStaticActiveStatus=s=>s==='running',
workflowTraceSafeJson=JSON.stringify, TOOL_LABEL={web_search:'网络搜索 / Web search'};
""")
    assert len(result["entries"]) == 1
    assert result["entries"][0]["transaction"]["result"]["result"] == "matched"
    assert "compact-tool" in result["html"]
    assert "wrong-agent" not in result["html"] and "wrong-segment" not in result["html"]


def test_message_minutes_use_real_dates_and_do_not_invent_missing_times():
    result = _js(["conversationDate", "conversationDayLabel", "conversationTime", "orchestrationEventTime"], """
console.log(JSON.stringify([
  orchestrationEventTime({ts:'2026-09-09T11:26:01Z'}),
  orchestrationEventTime({ts:'2026-09-09T11:26:59Z'}),
  orchestrationEventTime({ts:'2026-09-09T11:27:00Z'}),
  orchestrationEventTime({ts:'2026-09-09T11:30:00Z'}),
  orchestrationEventTime({ts:'2026-09-10T11:26:01Z'}),
  orchestrationEventTime({}), orchestrationEventTime({ts:'invalid'})]));
""", setup="const fmtDur=s=>String(s);")
    assert result[0]["minute"] == result[1]["minute"] == result[2]["minute"]
    assert result[0]["groupLabel"].endswith("11:25")
    assert result[3]["groupLabel"].endswith("11:30")
    assert result[0]["minute"] != result[3]["minute"] != result[4]["minute"]
    assert result[0]["minute"] != result[4]["minute"]
    assert result[5:] == [None, None]


def test_thinking_fold_uses_public_summary_and_real_completion_state():
    result = _js(["escapeHtml", "livePreviewLabel", "workflowTraceBlockMarkup", "orchestrationEntryContent"], """
const summary={type:'thinking',summary:'先核对公开来源。',thinking:'private-thinking',signature:'private-signature'};
const complete=workflowTraceBlockMarkup(summary,new Map());
const stream=workflowTraceBlockMarkup({...summary,partial:true},new Map());
const ended=['failed','stopped','paused_recoverable','completed','canceled'].map(status=>{
  ed.status=status;
  return workflowTraceBlockMarkup({...summary,partial:true},new Map());
});
ed.status='running';
const interrupted=workflowTraceBlockMarkup({...summary,partial:true,interrupted:true},new Map());
console.log(JSON.stringify({complete,stream,ended,interrupted,
  hidden:workflowTraceBlockMarkup({type:'thinking',thinking:'private-thinking',signature:'private-signature'},new Map()),
  text:orchestrationEntryContent({type:'message',meta:{role:'orch',label:'整体策划'},text:'我先规划任务，然后进行联网检索。'}),
  other:orchestrationEntryContent({type:'message',meta:{role:'critic',label:'独立 Critic'},text:'发现可读性问题。'})}));
""", setup="""
const ed={status:'running'}, workflowTraceClip=s=>String(s||''), agentMarkdownHtml=s=>escapeHtml(s),
  orchestrationAgentGlyph=()=>'', editorPresentationKind=()=> 'static';
""")
    assert "思考已完成" in result["complete"]
    assert "思考中" in result["stream"] and "思考已完成" not in result["stream"]
    assert all("思考已中断" in html for html in [result["interrupted"], *result["ended"]])
    assert result["hidden"] == ""
    for html in [result["complete"], result["stream"], result["interrupted"], *result["ended"]]:
        assert " open" not in html and html.count("先核对公开来源。") == 1
        assert "private-thinking" not in html and "private-signature" not in html
    assert "orch-entry-head" not in result["text"] and "orch-avatar" not in result["text"]
    assert "我先规划任务，然后进行联网检索。" in result["text"]
    assert "独立 Critic" in result["other"]


def test_live_projection_distinguishes_thinking_from_response_without_inventing_either():
    result = _js(["escapeHtml", "orchestrationFlatFeed", "orchestrationEntries", "orchestrationEntryContent",
                  "liveAgentText", "livePreviewLabel"], """
const feed={orch:[
  {k:'analysis',partial:true,stream_id:'thinking-one',event_id:'one',seq:1,s:'只核对需要的资料。'},
  {k:'text',partial:true,stream_id:'text-one',event_id:'two',seq:2,s:'正在整理资料。'},
  {k:'analysis',partial:true,phase:'interrupted',stream_id:'thinking-two',event_id:'three',seq:3,s:'中断前的摘要。'}]};
const entries=orchestrationEntries(feed,{agents:{}}).entries;
console.log(JSON.stringify({entries,html:entries.map(e=>orchestrationEntryContent(e)),
  empty:orchestrationEntries({}, {agents:{}}).entries}));
""", setup="""
const ed={status:'running'}, canonicalOrchestrationAgentKey=key=>key,
  orchestrationAgentMeta=key=>({key,role:'orch',label:'整体策划'}), orchestrationEventTime=()=>null,
  cleanAgentText=s=>s||'', orchestrationAgentGlyph=()=>'', editorPresentationKind=()=> 'static',
  agentMarkdownHtml=s=>escapeHtml(s);
""")
    assert len(result["entries"]) == 3 and result["empty"] == []
    assert [entry["analysis"] for entry in result["entries"]] == [True, False, True]
    assert "思考中" in result["html"][0]
    assert "生成中 · 非最终消息" in result["html"][1] and "orch-thinking-fold" not in result["html"][1]
    assert "思考已中断" in result["html"][2]


def test_chromium_message_minutes_without_timeline_lines(tmp_path, monkeypatch):
    import os
    from pathlib import Path
    from playwright.sync_api import sync_playwright
    from app import engine

    studio = Path(__file__).resolve().parents[1]
    for name in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(name, str(tmp_path))
    env = engine.render_env(dict(os.environ))
    script = _js_source(["escapeHtml", "conversationDate", "conversationDayLabel", "conversationTime",
        "orchestrationEventTime", "compactTraceValue", "liveReadFileSummary", "liveTerminalLabel", "liveToolTransactionMarkup",
        "livePreviewLabel", "orchestrationEntryContent", "reconcileOrchestrationEntries"]) + """
const $$=(s,root=document)=>[...root.querySelectorAll(s)];
const ed={status:'running'}, fmtDur=s=>String(s), isStaticActiveStatus=s=>s==='running',
  editorPresentationKind=()=> 'static', orchestrationAgentGlyph=()=> '▦',
  agentMarkdownHtml=escapeHtml, workflowTraceSafeJson=JSON.stringify,
  TOOL_LABEL={web_search:'研究资料'}, agentTimingBadge=()=> '<time class="orch-stage-time">耗时 12 秒</time>';
window.rows=Array.from({length:6},(_,i)=>({key:'row-'+i, type:i===0?'message':i%2?'analysis':'orchestrator-action',
  meta:{role:'orch',label:'整体策划',note:''},text:i===0?'我先阅读任务，再进行联网检索。':'先核对来源，避免把推测写成事实。',
  eventTime:orchestrationEventTime({ts:`2026-09-09T11:26:${String(i*8).padStart(2,'0')}Z`}),
  transaction:{id:'call-'+i,tool:'web_search',use:{args:{query:'核对资料'}},result:{ok:true,result:'已返回来源',duration_ms:120}}}));
window.draw=()=>reconcileOrchestrationEntries(document.querySelector('.orchestration-entry-list'),rows);
draw();
"""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=env["PPT_SKILL_BROWSER_EXE"], env=env,
                                    headless=True, args=["--no-sandbox"])
        try:
            page = browser.new_page(viewport={"width": 460, "height": 820}, timezone_id="UTC")
            page.route("**/*", lambda route: route.abort())
            page.set_content('<html data-theme="dark"><body><main class="workflow-conversation-lane" style="max-width:390px;margin:20px auto;padding:16px"><div class="orchestration-entry-list"></div></main></body></html>')
            page.add_style_tag(path=str(studio / "static/app.css"))
            page.add_script_tag(content=script)
            assert page.locator(".orch-entry").count() == 6
            assert page.locator('[data-entry-key="row-0"] .orch-avatar, [data-entry-key="row-0"] .orch-entry-head').count() == 0
            assert page.locator('[data-detail-key="analysis-row-1"] > summary').inner_text() == "思考已完成"
            assert page.locator(".orch-thinking-fold[open]").count() == 0
            assert page.locator(".orch-time-group").count() == 1
            assert "11:26" in page.locator(".orch-time-group").inner_text()
            assert page.locator(".orch-output-time, .orch-entry-foot").count() == 0
            page.locator('[data-detail-key="analysis-row-1"] > summary').click()
            page.evaluate("draw()")
            assert page.locator('[data-detail-key="analysis-row-1"]').get_attribute("open") is not None
            page.evaluate("rows.push({...rows[2],key:'next-minute',eventTime:orchestrationEventTime({ts:'2026-09-09T11:31:01Z'})});draw()")
            assert page.locator(".orch-time-group").count() == 2
            assert "120 ms" in page.locator("main").inner_text()
            assert page.locator('.orch-entry').first.get_attribute("title")
            for theme in ("light", "dark"):
                page.evaluate("theme=>document.documentElement.dataset.theme=theme", theme)
                assert page.locator(".orchestration-entry-list").evaluate("n=>getComputedStyle(n,'::before').content") == "none"
                assert page.locator("main").evaluate("n=>n.scrollWidth<=n.clientWidth")
            page.emulate_media(reduced_motion="reduce")
            page.wait_for_timeout(300)
            page.screenshot(path=str(tmp_path / "message-minute-groups.png"), full_page=True)
            page.evaluate("rows.shift();draw()")
            assert page.locator(".orch-time-group").count() == 2
            assert page.locator('[data-detail-key="analysis-row-1"]').get_attribute("open") is not None
            page.evaluate("rows.push({...rows[0],key:'missing-time',eventTime:null});draw()")
            assert page.locator(".orch-time-group").count() == 2
            page.evaluate("rows.push({...rows[0],key:'next-day',eventTime:orchestrationEventTime({ts:'2026-09-10T11:27:01Z'})});draw()")
            assert page.locator(".orch-time-group").count() == 3
            page.evaluate("rows.push({key:'owner',type:'slide',meta:{role:'slide',label:'Slide P1'},completed:true,action:'页面制作完成'});draw()")
            assert "耗时 12 秒" in page.locator(".orch-entry-foot").inner_text()
            assert page.locator(".orch-entry-foot").evaluate("n=>getComputedStyle(n).borderTopWidth") == "0px"
            page.evaluate("rows=[];draw()")
            assert page.locator(".orch-time-group, .orch-entry").count() == 0
            page.evaluate("""rows=[{key:'live',type:'stream-preview',analysis:true,interrupted:false,
              meta:{role:'orch',label:'整体策划'},text:'正在核对资料。'}];draw()""")
            assert page.locator('.orch-thinking-fold > summary').inner_text() == "思考中"
            assert page.locator('.orch-thinking-fold').get_attribute("open") is None
            page.locator('.orch-thinking-fold > summary').click()
            page.evaluate("rows[0].text+='保留相关证据。';draw()")
            assert page.locator('.orch-thinking-fold').get_attribute("open") is not None
            assert page.locator('.orch-thinking-fold .agent-markdown').inner_text() == "正在核对资料。保留相关证据。"
            page.evaluate("ed.status='paused_recoverable';draw()")
            assert page.locator('.orch-thinking-fold > summary').inner_text() == "思考已中断"
            page.evaluate("""ed.status='running';rows[0].type='analysis';draw();
              rows.push({key:'reply',type:'message',meta:{role:'orch',label:'整体策划'},text:'我先规划任务，然后进行联网检索。'},
                {key:'search',type:'orchestrator-action',meta:{role:'orch'},transaction:{id:'search',tool:'web_search',
                use:{args:{query:'任务相关的公开资料'}},result:{ok:true,result:'已返回来源',duration_ms:120}}});draw()""")
            assert page.locator('.orch-thinking-fold > summary').inner_text() == "思考已完成"
            assert page.locator('.orch-thinking-fold').get_attribute("open") is not None
            assert page.locator('[data-entry-key="reply"] .orch-entry-head').count() == 0
            page.locator('.orch-thinking-fold > summary').click()
            assert page.locator('main').evaluate("n=>n.scrollWidth<=n.clientWidth")
            page.screenshot(path=str(tmp_path / "compact-thinking-messages.png"), full_page=True)
        finally:
            browser.close()


def test_chromium_mural_brand_and_tool_folds(tmp_path, monkeypatch):
    import os
    from pathlib import Path
    from playwright.sync_api import sync_playwright
    from app import engine

    studio = Path(__file__).resolve().parents[1]
    for name in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(name, str(tmp_path))
    env = engine.render_env(dict(os.environ))
    html = making()["html"]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=env["PPT_SKILL_BROWSER_EXE"], env=env,
                                    headless=True, args=["--no-sandbox"])
        try:
            page = browser.new_page(viewport={"width": 1000, "height": 1100})
            page.route("**/*", lambda route: route.fulfill(content_type="image/svg+xml", body=(studio / "static/logo.svg").read_text())
                       if route.request.url.endswith("/static/logo.svg") else route.abort())
            page.set_content('<html data-theme="dark"><head><base href="http://making.test/"></head><body><main style="max-width:820px;margin:24px auto;padding:16px">' + html
                             + '<p>我先核对资料，再安排页面制作。</p><div class="orch-tool-message">' + tool() + '</div></main></body></html>')
            page.add_style_tag(path=str(studio / "static/app.css"))
            page.wait_for_function("document.querySelector('.mural-making-brand img').naturalWidth>0")
            assert page.locator(".mural-page-checklist li").count() == 4
            assert page.locator(".live-transaction").get_attribute("open") is None
            assert "唯一参数" not in page.locator(".live-transaction summary").inner_text()
            page.locator(".live-transaction summary").click()
            assert "唯一参数" in page.locator(".live-transaction-detail").inner_text()
            page.locator(".live-transaction summary").click()
            page.screenshot(path=str(tmp_path / "mural-making-messages.png"), full_page=True)
            for width in (1000, 420):
                page.set_viewport_size({"width": width, "height": 1100})
                assert page.locator("main").evaluate("n=>n.scrollWidth<=n.clientWidth")
            page.emulate_media(reduced_motion="reduce")
            assert page.locator(".mural-making-brand strong").evaluate("n=>getComputedStyle(n).animationName") == "none"
            assert page.locator(".state-running > span").evaluate("n=>getComputedStyle(n).animationName") == "none"
        finally:
            browser.close()
