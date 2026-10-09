"""静态任务只保留 Workflow 主入口，右侧复用会话并展示真实 Owner 素材。"""
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from test_startup_observability import _js


def test_sidebar_image_gallery_uses_registered_paths_and_keeps_sources():
    result = _js(["escapeHtml", "specialistGalleryMarkup"], """
console.log(JSON.stringify([
  specialistGalleryMarkup([]),
  specialistGalleryMarkup([{name:'<script>bad</script>.png',path:'assets/hero & image.png',mtime:7,origin:'generated',generator_model:'image-model'}])
]));
""", setup="const ed={id:269};")
    assert result[0] == ""
    assert "assets%2Fhero%20%26%20image.png" in result[1]
    assert "asset-thumbnail" in result[1] and "loading=\"lazy\"" in result[1]
    assert "&lt;script&gt;" in result[1] and "<script>" not in result[1]
    assert "AI 生成" in result[1] and "image-model" in result[1]
    assert "preview-unavailable" in result[1] and "this.closest('a').hidden=true" not in result[1]


def test_static_workspace_has_no_separate_process_entry():
    studio = Path(__file__).resolve().parents[1]
    template = (studio / "templates/app.html").read_text()
    source = (studio / "static/app.js").read_text()
    assert 'data-workspace-view="process"' not in template
    assert template.count('data-workspace-view="workflow"') == 1
    assert template.count('data-workspace-view="ppt"') == 1
    open_deck = source.split("async function openDeck(")[1].split("async function openDynamic(")[0]
    assert 'workspaceView: "workflow"' in open_deck
    assert 'workspaceView: "process"' not in open_deck
    assert 'workflowPane: "page"' in open_deck
    assert 'workflowPanelCollapsed: { files: false, conversation: false }' in open_deck
    assert 'setSidebarCollapsed(true' not in open_deck


def test_artifact_tab_can_open_before_a_file_is_selected():
    result = _js(["syncWorkflowCenterPane"], """
syncWorkflowCenterPane();
const artifact={pane:ed.workflowPane,panels:panels.map(p=>p.hidden),rendered:rendered.slice()};
ed.workflowPane='page';syncWorkflowCenterPane();
console.log(JSON.stringify({artifact,page:{pane:ed.workflowPane,panels:panels.map(p=>p.hidden)},rendered}));
""", setup="""
const ed={workflowPane:'artifact',workflowArtifactRel:''}, rendered=[];
const panels=['graph','task','page','artifact'].map(p=>({dataset:{workflowPanePanel:p}}));
const $=()=>null, $$=()=>panels, syncWorkflowLayoutReset=()=>{};
const renderWorkflowTaskPane=()=>rendered.push('task'),renderWorkflowPagePane=()=>rendered.push('page'),
  renderWorkflowArtifactPane=()=>rendered.push('artifact');
""")
    assert result["artifact"] == {"pane": "artifact", "panels": [True, True, True, False], "rendered": ["artifact"]}
    assert result["page"] == {"pane": "page", "panels": [True, True, False, True]}
    assert result["rendered"] == ["artifact", "page"]


def test_page_preview_has_no_progress_footer_and_keeps_navigation_updates():
    result = _js(["workflowReadyPages", "renderWorkflowPagePane"], """
renderWorkflowPagePane();
const first=nodes['#workflow-page-stage'].innerHTML;
ed.rendered.add(2);renderWorkflowPagePane();
const updated={html:nodes['#workflow-page-stage'].innerHTML,position:nodes['#workflow-page-position'].textContent,
  strip:nodes['#workflow-page-strip'].innerHTML,nextDisabled:nodes['#workflow-page-next'].disabled};
ed.workflowPage=2;renderWorkflowPagePane();
const selected={html:nodes['#workflow-page-stage'].innerHTML,position:nodes['#workflow-page-position'].textContent,
  previousDisabled:nodes['#workflow-page-prev'].disabled,nextDisabled:nodes['#workflow-page-next'].disabled};
ed.rendered.clear();renderWorkflowPagePane();
console.log(JSON.stringify({first,updated,selected,empty:nodes['#workflow-page-stage'].innerHTML}));
""", setup="""
const nodes=Object.fromEntries(['stage','strip','position','title','prev','next'].map(name=>
  ['#workflow-page-'+name,{dataset:{},innerHTML:'',querySelector:()=>null}]));
const $=selector=>nodes[selector],pad2=n=>String(n).padStart(2,'0'),fileUrl=n=>'/preview/'+n;
const ed={rendered:new Set([1]),total:12,workflowPage:1};
const muralMakingView=()=>({previews:ed.rendered.size,total:ed.total,status:'演示文稿制作中'});
const muralMakingMarkup=()=>'<section>等待页面预览</section>';
""")
    assert result["first"] == '<img src="/preview/1" alt="第 1 页渲染预览">'
    assert result["updated"]["html"] == result["first"]
    assert result["updated"]["position"] == "1 / 2" and result["updated"]["nextDisabled"] is False
    assert result["updated"]["strip"].count(' ready"') == 2
    assert result["selected"]["html"] == '<img src="/preview/2" alt="第 2 页渲染预览">'
    assert result["selected"]["position"] == "2 / 2"
    assert result["selected"]["previousDisabled"] is False and result["selected"]["nextDisabled"] is True
    assert result["empty"] == "<section>等待页面预览</section>"
    css = (Path(__file__).resolve().parents[1] / "static/app.css").read_text()
    assert ".workflow-page-progress" not in css


def test_split_ratio_defaults_and_bounds():
    result = _js(["workflowSplitRatio"], """
console.log(JSON.stringify([null,undefined,'',NaN,'bad',.4,'0.55',0,-1,1].map(workflowSplitRatio)));
""")
    assert result == [.4, .4, .4, .4, .4, .4, .55, .25, .25, .65]


def test_split_remembers_user_preference_but_not_temporary_trace_expansion():
    result = _js(["workflowSplitRatio", "setWorkflowSplit"], """
setWorkflowSplit(.5);
setWorkflowSplit(.6,{remember:false});
console.log(JSON.stringify({ed,stored,styles,attributes}));
""", setup="""
const stored={},styles={},attributes={},ed={workflowTraceExpanded:true};
const localStorage={setItem:(k,v)=>stored[k]=v};
const shell={style:{setProperty:(k,v)=>styles[k]=v},classList:{remove:()=>{}}};
const splitter={setAttribute:(k,v)=>attributes[k]=v};
const $=selector=>selector==='.workflow-studio-shell'?shell:selector==='#workflow-splitter'?splitter:null;
""")
    assert result["ed"]["workflowSplitRatio"] == .5
    assert result["ed"]["workflowTraceExpanded"] is False
    assert result["stored"] == {"studio_workflow_split_ratio": "0.5"}
    assert result["styles"] == {"--workflow-center-fr": "0.4fr", "--workflow-message-fr": "0.6fr"}
    assert result["attributes"]["aria-valuenow"] == "60"


def test_agent_conversation_pairs_tools_and_keeps_context_and_failures():
    result = _js(["workflowTraceConversationRows", "workflowTraceContentBlocks", "escapeHtml"], """
const rows=workflowTraceConversationRows([
 {role:'user',content:JSON.stringify({owner_packet:{task:'秘密不是正文'},allowed_read_paths:['deck.md']})},
 {role:'assistant',content:[{type:'text',text:'先写本页文案。'},
   {type:'tool_use',id:'write',name:'write_file',input:{path:'briefs/slide_01.md'}},
   {type:'tool_use',id:'failed',name:'patch',input:{path:'slides/slide_01.html'}},
   {type:'tool_use',id:'pending',name:'inspect',input:{}}]},
 {role:'user',content:[{type:'tool_result',tool_use_id:'write',content:{ok:true}},
   {type:'tool_result',tool_use_id:'failed',is_error:true,content:'not found'}]},
 {role:'user',content:'请保留讲稿。'},
 {role:'user',content:[{type:'tool_result',tool_use_id:'orphan',content:'独立回执'}]}
]);
console.log(JSON.stringify({rows,transactions}));
""", setup="""
const ed={status:'paused_recoverable',workflowTraceAgent:'slide',workflowTracePayload:{selected_segment:'current'},
  feed:{slide:[{k:'tool_result',tool_use_id:'write',segment_id:'old',duration_ms:999},
    {k:'tool_result',tool_use_id:'write',segment_id:'current',duration_ms:411}]}};
const transactions=[],isStaticActiveStatus=s=>s==='running',workflowTraceSafeJson=JSON.stringify;
const liveToolTransactionMarkup=(t,options)=>{transactions.push({...t,options});return '<details>tool</details>';};
const workflowArtifactLinkMarkup=p=>'<a>'+p+'</a>',workflowTraceResultParts=()=>({images:0});
const workflowTraceBlockMarkup=b=>b.text||b.content||'',workflowToolOutputMarkup=()=>'';
""")
    assert len(result["rows"]) == 4
    assert '<details class="workflow-trace-context' in result["rows"][0]["html"]
    assert " open" not in result["rows"][0]["html"]
    assert "briefs/slide_01.md" in result["rows"][1]["html"]
    assert result["rows"][1]["html"].count('class="orch-entry-copy orch-tool-message"') == 3
    assert "slides/slide_01.html" not in result["rows"][1]["html"]  # failed writes have no artifact claim
    write, failed, pending = result["transactions"]
    assert write["result"]["duration_ms"] == 411
    assert failed["result"]["ok"] is False
    assert pending["result"] is None and pending["options"]["active"] is False
    assert result["rows"][2]["html"] == "请保留讲稿。"
    assert result["rows"][3]["html"] == "独立回执"


def test_message_navigation_selects_the_exact_owner_segment():
    result = _js(["openWorkflowAgentTrace"], """
openWorkflowAgentTrace('owner',{segment:'first',taskId:'one'});
openWorkflowAgentTrace('owner',{segment:'second',taskId:'two'});
openWorkflowAgentTrace('owner',{segment:'missing'});
console.log(JSON.stringify({highlights,scrolled,source:ed.workflowTraceSource}));
""", setup="""
const ed={workflowPane:'graph',workflowTraceAgent:'owner',workflowTraceSource:'events',workflowPanelCollapsed:{}},
  highlights=[],scrolled=[];
const nodes=['first','second'].map((segment,i)=>({dataset:{agentKey:'owner',workflowSegment:segment,workflowTaskId:['one','two'][i],nodeId:'node-'+i},
  scrollIntoView:()=>scrolled.push(segment)}));
const document={querySelectorAll:()=>nodes},$=()=>null,cancelWorkflowAgentTraceRequest=()=>{},
  syncWorkflowPanelLayout=()=>{},syncWorkflowConversationMode=()=>highlights.push(ed.workflowSelectedNodeId),
  renderWorkflowAgentTrace=()=>{},loadWorkflowAgentTrace=()=>{};
""")
    assert result == {"highlights": ["node-0", "node-1", ""], "scrolled": ["first", "second"], "source": "messages"}


def test_switching_agent_defaults_to_complete_cross_segment_trace():
    result = _js(["workflowAgentTraceUrl", "openWorkflowAgentTrace"], """
openWorkflowAgentTrace('image-owner');
console.log(JSON.stringify({
  segment:ed.workflowTraceSegment,
  pinned:ed.workflowTraceSegmentPinned,
  source:ed.workflowTraceSource,
  url:workflowAgentTraceUrl(ed.workflowTraceAgent)
}));
""", setup="""
const ed={id:42,workflowPane:'graph',workflowTraceAgent:'orch',workflowTraceSource:'events',workflowPanelCollapsed:{}},
  trajectoryMode=false;
const document={querySelectorAll:()=>[]},$=()=>null,cancelWorkflowAgentTraceRequest=()=>{},
  syncWorkflowPanelLayout=()=>{},syncWorkflowConversationMode=()=>{},renderWorkflowAgentTrace=()=>{},
  loadWorkflowAgentTrace=()=>{};
""")
    assert result["segment"] == "__all__" and result["pinned"] is True
    assert result["source"] == "messages"
    assert "segment=__all__" in result["url"]


def test_compaction_block_is_visible_in_readable_agent_trace():
    html = _js(["escapeHtml", "workflowTraceBlockMarkup"], """
console.log(JSON.stringify(workflowTraceBlockMarkup({type:'context_compaction',phase:'completed',
  tokens_before:112000,tokens_after:24000,input_budget:106496},new Map())));
""")
    assert "上下文压缩完成" in html
    assert "压缩前 112,000" in html and "压缩后 24,000" in html
    assert "workflow-trace-compaction" in html


def test_browser_workflow_sidebar_full_app(tmp_path, monkeypatch):
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    from playwright.sync_api import sync_playwright
    from app import engine

    studio = Path(__file__).resolve().parents[1]
    template = Environment(loader=FileSystemLoader(studio / "templates"), autoescape=select_autoescape()).get_template("app.html")
    html = template.render(authenticated=True, auth_enabled=False, dynamic_enabled=True,
                           edition="test", asset_ver="test", user={"display_name": "测试用户", "username": "test", "role": "user"})
    for name in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(name, str(tmp_path))
    env = engine.render_env(dict(os.environ))
    images = [{"path": "assets/hero-one.png", "name": "素材一.png", "mtime": 1, "origin": "generated"},
              {"path": "assets/hero-two.png", "name": "素材二.png", "mtime": 1, "origin": "downloaded"}]
    progress = {"id": 901, "title": "右侧栏收敛验收", "query": "三页演示：核验资料、准备配图并完成页面。", "status": "paused_recoverable",
        "phase": "rendering", "slides_total": 3, "rendered": [], "authored": [], "slides_rendered": 0,
        "model": "27b", "model_label": "27B", "skill_version": "mural-presenter-v1", "thinking": False,
        "resume_eligible": True, "elapsed_s": 42,
        "mural_runtime": {"tasks": [
            {"task_id": "image-one", "role": "image", "agent_key": "image_one", "owner_id": "owner-one", "status": "succeeded", "dependencies": []},
            {"task_id": "slide-one", "role": "slide", "agent_key": "slide_group_one", "pages": [1, 2, 3], "status": "queued", "dependencies": ["image-one"]}]}}
    feed = {"orch": [
        {"k": "analysis", "event_id": "summary-1", "s": "需要先核验关键事实，再安排配图。", "seq": 1, "ts": "2026-09-09T11:26:00Z"},
        {"k": "text", "event_id": "message-1", "s": "我先规划任务，然后进行联网检索。", "seq": 2, "ts": "2026-09-09T11:26:01Z"},
        {"k": "tool", "tool": "web_search", "tool_use_id": "search-1", "segment_id": "first", "args": {"query": "公开资料"}, "seq": 3},
        {"k": "tool_result", "tool": "web_search", "tool_use_id": "search-1", "segment_id": "first", "ok": True, "result": "来源已返回。", "seq": 4},
        {"k": "tool", "tool": "read_file", "tool_use_id": "read-1", "segment_id": "first", "seq": 6,
         "args": {"path": "skills/mural-presenter-v1/references/planning/plan-contract.md"}},
        {"k": "tool_result", "tool": "read_file", "tool_use_id": "read-1", "segment_id": "first", "seq": 7,
         "ok": True, "duration_ms": 411, "result": json.dumps({"mode": "line_range", "offset": 1,
         "line_count": 120, "returned_line_count": 120, "next_offset": None})}],
        "image_one": [{"k": "text", "s": "两张素材已经落盘。", "seq": 5, "segment_id": "image-first", "task_id": "image-one", "ts": "2026-09-09T11:26:05Z"}],
        "slide_group_one": [{"k": "text", "s": "本页先说明资料来源。", "seq": 8,
                             "segment_id": "slide-first", "task_id": "slide-one"}]}
    artifacts = {"agents": {"image_one": {"images": images, "count": 2, "completed": True}}}
    file_groups = [{"id": "plan", "label": "Plan", "files": [{"rel": "briefs/slide_01.md",
        "name": "slide_01.md", "kind": "markdown", "size": 120}]}]
    requests = []
    trace_messages = [
        {"role": "user", "content": json.dumps({"owner_packet": {"task": "制作本页"}, "allowed_read_paths": ["deck.md"]})},
        {"role": "assistant", "content": [{"type": "text", "text": "我先写本页计划，再检查画面。"},
            {"type": "tool_use", "id": "plan-write", "name": "write_file", "input": {"path": "briefs/slide_01.md", "content": "第一页分页计划"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "plan-write", "content": {"path": "briefs/slide_01.md"}}]},
    ]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=env["PPT_SKILL_BROWSER_EXE"], env=env, headless=True, args=["--no-sandbox"])
        try:
            page = browser.new_page(viewport={"width": 1600, "height": 1000})
            page.set_default_timeout(12000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def serve(route):
                request = route.request
                path = urlparse(request.url).path
                requests.append((request.method, path))
                if path == "/":
                    route.fulfill(content_type="text/html; charset=utf-8", body=html)
                elif path in {"/static/app.js", "/static/app.css", "/static/logo.svg"}:
                    mime = {".js": "application/javascript", ".css": "text/css", ".svg": "image/svg+xml"}[Path(path).suffix]
                    route.fulfill(content_type=mime, body=(studio / path.lstrip("/")).read_text())
                elif path == "/api/decks":
                    route.fulfill(json={"decks": [progress]})
                elif path == "/api/decks/901/progress":
                    route.fulfill(json=progress)
                elif path == "/api/decks/901/livefeed":
                    route.fulfill(json={"agents": feed, "specialist_artifacts": artifacts, "workflow": {"artifacts": {"groups": file_groups}}})
                elif path.startswith("/api/decks/901/agent-trace/"):
                    agent = path.rsplit("/", 1)[-1]
                    params = parse_qs(urlparse(request.url).query)
                    segment = params.get("segment", ["latest"])[0]
                    source = params.get("source", ["messages"])[0]
                    route.fulfill(json={"agent": {"label": agent, "role": "slide"}, "source": source,
                        "segments": [{"id": segment, "segment_id": segment, "sources": [{"id": "messages"}, {"id": "events"}]}],
                        "selected_segment": segment, "content": json.dumps(trace_messages),
                        "updated_at": "2026-09-09T11:27:00Z", "message_times": ["2026-09-09T11:26:00Z"]*3})
                elif path.endswith("/asset-thumbnail") or (path == "/api/decks/901/file" and "renders/slide_" in request.url):
                    route.fulfill(content_type="image/svg+xml", body=(studio / "static/logo.svg").read_text())
                elif path.startswith("/api/decks/901/files/briefs/") or path.startswith("/api/decks/901/files/assets/"):
                    if path.endswith("briefs/slide_01.md"):
                        route.fulfill(content_type="text/plain; charset=utf-8", body="# 第一页分页计划\n真实文件内容预览。")
                    else:
                        route.fulfill(content_type="image/svg+xml", body=(studio / "static/logo.svg").read_text())
                elif path.startswith("/api/"):
                    route.fulfill(json={})
                else:
                    route.abort()

            page.route("**/*", serve)
            page.goto("http://sidebar.test/#static-901", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_selector("#editor.workspace-workflow", state="visible", timeout=30000)
            assert not page.locator('.layout').evaluate("n=>n.classList.contains('sidebar-collapsed')")
            assert page.locator('.workflow-files').is_hidden()
            assert page.locator('.workflow-files').evaluate("n=>n.closest('#workflow-artifact-pane')!==null")
            assert abs(page.locator('.workflow-canvas-toolbar').bounding_box()['x']
                       - page.locator('.workflow-studio-shell').bounding_box()['x']) < 1
            assert page.locator('#workflow-pane-switch').get_attribute('data-active') == 'page'
            assert page.locator('#workflow-page-pane').is_visible()
            assert page.locator('#workflow-scroll').is_hidden()

            def assert_slider():
                page.wait_for_function("""() => {
                  const n=document.querySelector('#workflow-pane-switch'), a=n.querySelector('button.on');
                  const p=getComputedStyle(n,'::before'), s=getComputedStyle(n);
                  const left=parseFloat(s.borderLeftWidth)+parseFloat(p.left)+new DOMMatrix(p.transform).m41;
                  return Math.abs(left-a.offsetLeft)<2 && Math.abs(parseFloat(p.width)-a.offsetWidth)<2;
                }""")
                assert page.locator('#workflow-pane-switch button.on').evaluate("n=>getComputedStyle(n).backgroundColor") == 'rgba(0, 0, 0, 0)'

            assert page.locator('#workflow-pane-switch button:visible').count() == 4
            page.locator('[data-workflow-pane="artifact"]').click()
            assert page.locator('.workflow-file-tree').is_visible()
            assert "选择一个产物" in page.locator('#workflow-artifact-stage').inner_text()
            assert page.locator('#workflow-artifact-open').is_hidden()
            toolbar = page.locator('.workflow-canvas-toolbar').bounding_box()
            assert page.locator('.workflow-files').bounding_box()['y'] >= toolbar['y'] + toolbar['height'] - 1
            empty_artifact_width = page.locator('.workflow-canvas-panel').bounding_box()['width']
            page.locator('#workflow-file-tree [data-workflow-artifact-rel="briefs/slide_01.md"]').click()
            page.wait_for_function("document.querySelector('#workflow-artifact-stage').textContent.includes('第一页分页计划')")
            assert page.locator('#workflow-artifact-path').inner_text() == 'briefs/slide_01.md'
            page.screenshot(path=str(tmp_path/'workflow-artifact-plan.png'), full_page=True, animations='disabled')
            for name in ('graph', 'task', 'page'):
                page.locator(f'[data-workflow-pane="{name}"]').click()
                assert_slider()
                assert page.locator('.workflow-files').is_hidden()
                assert abs(page.locator('.workflow-canvas-panel').bounding_box()['width'] - empty_artifact_width) < 1
            page.evaluate("selectWorkflowArtifact('assets/hero-one.png',{rel:'assets/hero-one.png',name:'hero-one.png',kind:'image'})")
            assert page.locator('#workflow-pane-switch button:visible').count() == 4
            for theme in ('light','dark'):
                page.evaluate('theme=>document.documentElement.dataset.theme=theme', theme)
                for name in ('graph', 'task', 'page', 'artifact'):
                    page.locator(f'[data-workflow-pane="{name}"]').click()
                    assert_slider()
                    assert page.locator('.workflow-files').is_visible() == (name == 'artifact')
                    assert abs(page.locator('.workflow-canvas-toolbar').bounding_box()['x']
                               - page.locator('.workflow-studio-shell').bounding_box()['x']) < 1
            page.screenshot(path=str(tmp_path/'workflow-artifact-only-files.png'), full_page=True, animations='disabled')
            page.locator('[data-workflow-pane="page"]').click()

            divider = page.locator('#workflow-splitter')
            old_width = page.locator('.workflow-conversation').bounding_box()['width']
            grip = divider.bounding_box()
            page.mouse.move(grip['x'] + grip['width']/2, grip['y'] + 100)
            page.mouse.down()
            page.mouse.move(grip['x'] - 140, grip['y'] + 100, steps=8)
            page.mouse.up()
            assert page.locator('.workflow-conversation').bounding_box()['width'] > old_width + 100
            assert page.locator('.workflow-canvas-panel').bounding_box()['width'] >= 320
            divider.focus()
            divider.press('Home')
            assert divider.get_attribute('aria-valuenow') == '25'
            divider.press('End')
            assert divider.get_attribute('aria-valuenow') == '65'
            divider.dispatch_event('dblclick')
            assert divider.get_attribute('aria-valuenow') == '40'
            assert page.evaluate("localStorage.getItem('studio_workflow_split_ratio')") == '0.4'
            page.locator('[data-workflow-pane="artifact"]').click()
            assert page.locator('.workflow-file-tree').is_visible()
            page.locator('#workflow-files-toggle').click()
            assert page.locator('.workflow-file-tree').is_hidden()
            page.locator('#workflow-files-toggle').click()
            page.locator('[data-workflow-pane="page"]').click()
            assert page.locator('.workflow-files').is_hidden()

            def wait_for_split_animation():
                page.wait_for_function("""() => document.querySelector('.workflow-studio-shell')
                  .getAnimations().every(animation => animation.playState === 'finished' || animation.playState === 'idle')""")

            for width, height in ((1600, 1000), (1200, 740), (600, 1000)):
                page.set_viewport_size({"width": width, "height": height})
                for ratio in (.25, .4, .65):
                    page.evaluate("ratio => setWorkflowSplit(ratio)", ratio)
                    wait_for_split_animation()
                    expanded_share = (page.locator('.workflow-conversation').bounding_box()['width']
                                      / page.locator('.workflow-studio-shell').bounding_box()['width'])
                    page.locator('#workflow-conversation-toggle').click()
                    wait_for_split_animation()
                    bounds = page.evaluate("""() => {
                      const shell=document.querySelector('.workflow-studio-shell');
                      const center=document.querySelector('.workflow-canvas-panel').getBoundingClientRect();
                      const messages=document.querySelector('.workflow-conversation').getBoundingClientRect();
                      const frame=shell.getBoundingClientRect(), style=getComputedStyle(shell);
                      const scale=frame.width/shell.offsetWidth;
                      return {layout:style.display, frame:frame.width, center:center.width,
                        messages:messages.width, height:messages.height/scale,
                        unused:frame.right-messages.right, gap:messages.left-center.right,
                        rail:parseFloat(style.getPropertyValue('--workflow-message-track'))*scale};
                    }""")
                    assert divider.is_hidden()
                    assert page.locator('#workflow-conversation-toggle').get_attribute('aria-expanded') == 'false'
                    if bounds['layout'] == 'grid':
                        assert abs(bounds['unused']) < 1, bounds
                        assert abs(bounds['gap']) < 1, bounds
                        assert abs(bounds['messages'] - bounds['rail']) < 1, bounds
                        assert abs(bounds['frame'] - bounds['center'] - bounds['messages']) < 1, bounds
                    else:
                        assert bounds['height'] <= 50, bounds
                        assert abs(bounds['frame'] - bounds['messages']) < 1, bounds
                    if ratio == .4:
                        page.screenshot(path=str(tmp_path/f'workflow-collapsed-{width}x{height}.png'))
                    page.locator('#workflow-conversation-toggle').click()
                    page.wait_for_function("document.querySelector('#workflow-conversation-toggle').getAttribute('aria-expanded')==='true'")
                    wait_for_split_animation()
                    restored_share = (page.locator('.workflow-conversation').bounding_box()['width']
                                      / page.locator('.workflow-studio-shell').bounding_box()['width'])
                    assert abs(restored_share - expanded_share) < .005, (width, height, ratio, expanded_share, restored_share)
                    assert page.evaluate("localStorage.getItem('studio_workflow_split_ratio')") == str(ratio)
            page.set_viewport_size({"width": 1600, "height": 1000})
            page.evaluate("setWorkflowSplit(.4)")
            divider.wait_for(state='visible')
            page.screenshot(path=str(tmp_path/'workflow-preview-default-split.png'), full_page=True, animations='disabled')
            assert page.locator('[data-workspace-view="process"]').count() == 0
            assert page.locator("#filmstrip .frame-outline").count() == 0
            assert page.locator("#agent-progress-console, #outline-chat-lane").count() == 0
            page.wait_for_selector("#workflow-message-list .specialist-gallery img", state="visible")
            assert page.locator("#workflow-message-list .specialist-gallery img").count() == 2
            assert page.locator("#workflow-message-list #outline-task-config").count() == 1
            assert "27B" in page.locator("#outline-task-config summary").inner_text()
            assert page.locator("#workflow-message-list .mural-page-checklist li").count() == 3
            assert page.locator("#resume-btn").is_visible()
            assert page.locator("#workflow-chat-form").is_visible()
            assert page.locator("#workflow-chat-send").is_disabled()
            assert "图片登记与预览" in page.locator("#workflow-message-list").inner_text()
            assert "AI 生成" in page.locator("#workflow-message-list").inner_text()
            assert "真实图片" in page.locator("#workflow-message-list").inner_text()
            page.locator('[data-workflow-pane="graph"]').click()
            slide_message = page.locator('#workflow-message-list [data-message-agent="slide_group_one"]')
            slide_message.click()
            page.wait_for_selector('#workflow-trace-messages .live-transaction')
            assert page.evaluate('ed.workflowTraceSegment') == 'slide-first'
            assert page.locator('.workflow-node.trace-selected[data-agent-key="slide_group_one"]').count() == 1
            assert page.locator('#workflow-trace-messages .workflow-trace-context').get_attribute('open') is None
            assert page.locator('#workflow-trace-messages .live-transaction').count() == 1
            assert '我先写本页计划' in page.locator('#workflow-trace-messages').inner_text()
            assert 'owner_packet' not in page.locator('#workflow-trace-messages').inner_text()
            assert 'write_file' not in page.locator('#workflow-trace-messages').inner_text()
            tool = page.locator('#workflow-trace-messages .live-transaction')
            tool.locator('summary').click()
            assert tool.get_attribute('open') is not None
            assert page.evaluate('ed.workflowTraceAgent') == 'slide_group_one'
            page.evaluate("window.keptTool=document.querySelector('#workflow-trace-messages .live-transaction');ed.workflowTracePayload.updated_at='new';renderWorkflowTraceMessages()")
            assert page.evaluate("keptTool===document.querySelector('#workflow-trace-messages .live-transaction') && keptTool.open")
            page.locator('#workflow-trace-messages [data-workflow-artifact-rel="briefs/slide_01.md"]').click()
            page.wait_for_function("document.querySelector('#workflow-artifact-stage').textContent.includes('第一页分页计划')")
            page.locator('#workflow-trace-search').fill('owner_packet')
            page.wait_for_selector('#workflow-trace-messages mark[data-search-match]', state='attached')
            assert page.locator('#workflow-trace-messages mark[data-search-match]').count() == 1
            page.locator('#workflow-trace-search').fill('')
            page.wait_for_function("document.querySelector('#workflow-trace-messages [data-trace-search-summary]').textContent==='按执行顺序排列'")
            page.locator('#workflow-trace-messages .workflow-trace-context > summary').click()
            tool.locator('summary').click()
            page.locator('[data-workflow-pane="graph"]').click()
            page.screenshot(path=str(tmp_path/'workflow-agent-conversation.png'), full_page=True, animations='disabled')
            page.locator('#workflow-trace-back').click()
            image_message = page.locator('#workflow-message-list [data-message-agent="image_one"]')
            image_message.focus()
            image_message.press('Enter')
            page.wait_for_function("ed.workflowTraceAgent==='image_one' && !ed.workflowTraceLoading")
            assert page.locator('.workflow-node.trace-selected[data-agent-key="image_one"]').count() == 1
            assert page.locator('.workflow-node.trace-selected[data-agent-key="slide_group_one"]').count() == 0
            assert page.evaluate('ed.workflowTraceSegment') == 'image-first'
            page.locator('.workflow-trace-technical > summary').click()
            page.locator('[data-trace-source="events"]').click()
            page.wait_for_selector('#workflow-trace-json', state='visible')
            page.wait_for_function("!ed.workflowTraceLoading && ed.workflowTracePayload?.source==='events'")
            assert 'owner_packet' in page.locator('#workflow-trace-json').inner_text()
            page.locator('#workflow-trace-back').click()
            page.locator('.workflow-node[data-agent-key="slide_group_one"]').click()
            page.wait_for_selector('#workflow-trace-messages .live-transaction')
            assert page.locator('.workflow-node.trace-selected[data-agent-key="slide_group_one"]').count() == 1
            page.locator('#workflow-trace-back').click()
            assert page.locator('#workflow-message-list .orch-thinking-fold > summary').inner_text() == "思考已完成"
            assert page.locator('#workflow-message-list .orch-thinking-fold').get_attribute("open") is None
            assert page.locator('#workflow-message-list .orch-entry.message .orch-entry-head').count() == 0
            assert page.locator('#workflow-message-list .compact-tool').count() == 2
            read_card = page.locator('#workflow-message-list .compact-tool[data-tool-use-id="read-1"]')
            assert read_card.get_attribute("open") is None
            assert "读取文件 · plan-contract.md" in read_card.locator("summary").inner_text()
            assert "全文已返回 · 120 / 120 行" in read_card.locator("summary").inner_text()
            read_card.locator("summary").click()
            assert "references/planning/plan-contract.md" in read_card.locator(".live-transaction-detail").inner_text()
            read_card.locator("summary").click()
            page.locator('#workflow-message-list .orch-thinking-fold').scroll_into_view_if_needed()
            page.wait_for_function("[...document.querySelectorAll('#workflow-message-list .orch-entry')].every(n=>getComputedStyle(n).opacity==='1')")
            page.screenshot(path=str(tmp_path / "workflow-compact-messages.png"), full_page=True)
            first_image = page.locator("#workflow-message-list .specialist-gallery img").first
            first_image.scroll_into_view_if_needed()
            page.wait_for_function("[...document.querySelectorAll('#workflow-message-list .specialist-gallery img')].every(n=>n.naturalWidth>0)")
            assert first_image.evaluate("n=>getComputedStyle(n).objectFit") == "contain"
            assert "assets%2Fhero-one.png" in page.locator("#workflow-message-list .specialist-gallery a").first.get_attribute("href")
            page.screenshot(path=str(tmp_path / "workflow-sidebar-images.png"), full_page=True)
            page.locator('[data-workflow-pane="graph"]').click()
            page.locator('[data-node-id="task:image-one"]').click()
            page.wait_for_selector(".workflow-conversation.trace-mode")
            assert page.locator("#workflow-image-outputs").is_visible()
            assert page.locator("#workflow-image-outputs img").count() == 2
            page.screenshot(path=str(tmp_path / "workflow-image-owner-trace.png"), full_page=True)
            # 按 Owner 精确取素材，不把其他 Agent 或全局素材混入。
            page.evaluate("ed.workflowTracePayload=null;ed.workflowTraceAgent='image_one';ed.workflowTraceSource='messages';renderWorkflowImageOutputs()")
            assert page.locator("#workflow-image-outputs img").count() == 2
            assert "跨执行段" in page.locator("#workflow-image-outputs").text_content()
            page.evaluate("ed.workflowTraceAgent='image_other';renderWorkflowImageOutputs()")
            assert page.locator("#workflow-image-outputs").is_hidden()
            page.evaluate("ed.workflowTraceAgent='image_one';ed.workflowTraceSource='tools';renderWorkflowImageOutputs()")
            assert page.locator("#workflow-image-outputs").is_hidden()
            page.locator('[data-workflow-conversation-mode="history"]').click()
            # 任务结束后修改入口仍复用原有权限与状态判断。
            page.evaluate("ed.status='completed';syncWorkflowComposer()")
            page.locator("#workflow-chat-input").fill("精简第二页")
            assert page.locator("#workflow-chat-send").is_enabled()
            page.evaluate("ed.staticDeckReady=true;ed.authored=new Set([1]);setWorkspaceView('ppt')")
            assert "workspace-ppt" in page.locator("#editor").get_attribute("class")
            page.locator('[data-workspace-view="workflow"]').click()
            assert "workspace-workflow" in page.locator("#editor").get_attribute("class")
            assert page.locator("#workflow-chat-input").input_value() == "精简第二页"
            assert page.locator("#agent-progress-console, #outline-chat-lane").count() == 0
            page.evaluate("ed.status='running';syncWorkflowComposer()")
            assert page.locator("#workflow-chat-send").is_disabled()
            assert page.locator("#workflow-chat-input").input_value() == "精简第二页"
            page.evaluate("ed.total=0;ed.rendered=new Set();setWorkflowPane('page');renderWorkflowPagePane()")
            stage = page.locator('#workflow-page-stage')
            assert stage.locator('.mural-making-hero.active').count() == 1
            assert "页数尚未确定" in stage.inner_text()
            assert stage.locator('progress, .mural-page-checklist').count() == 0
            page.evaluate("ed.total=12;renderWorkflowPagePane()")
            assert stage.locator('progress').get_attribute('value') == '0'
            assert stage.locator('progress').get_attribute('max') == '12'
            assert "预览覆盖率 0%" in stage.inner_text()
            assert page.locator('#workflow-page-strip button').count() == 12
            for width, height in ((1600, 1000), (1100, 740), (420, 850)):
                page.set_viewport_size({"width": width, "height": height})
                stage.scroll_into_view_if_needed()
                assert stage.evaluate("n=>n.scrollWidth<=n.clientWidth")
                assert stage.locator('.mural-making-hero').bounding_box()['height'] >= stage.bounding_box()['height'] - 28
            page.set_viewport_size({"width": 1600, "height": 1000})
            page.wait_for_function("document.querySelector('#workflow-page-stage .mural-making-brand img').naturalWidth>0")
            page.screenshot(path=str(tmp_path / 'workflow-page-loading.png'), full_page=True)
            page.evaluate("ed.status='paused_recoverable';renderWorkflowPagePane()")
            assert stage.locator('.mural-making-hero.active').count() == 0
            assert "当前未继续生成" in stage.inner_text()
            assert stage.locator('.mural-making-brand strong').evaluate("n=>getComputedStyle(n).animationName") == "none"
            page.evaluate("ed.status='running';ed.rendered=new Set([1]);renderWorkflowPagePane();window.previewNode=document.querySelector('#workflow-page-stage > img')")
            assert stage.locator('.mural-making-hero').count() == 0
            assert stage.locator('progress, .workflow-page-progress').count() == 0
            page.evaluate("ed.rendered.add(2);renderWorkflowPagePane()")
            assert stage.locator('progress, .workflow-page-progress').count() == 0
            assert page.locator('#workflow-page-strip button.ready').count() == 2
            assert page.evaluate("previewNode===document.querySelector('#workflow-page-stage > img')")
            page.wait_for_function("document.querySelector('#workflow-page-stage > img').naturalWidth>0")
            page.screenshot(path=str(tmp_path / 'workflow-page-preview.png'), full_page=True)
            page.evaluate("ed.total=12;ed.rendered=new Set(Array.from({length:12},(_,i)=>i+1));renderWorkflowPagePane()")
            strip = page.locator('#workflow-page-strip')
            for width, height in ((1600, 1000), (1100, 740), (420, 850)):
                page.set_viewport_size({"width": width, "height": height})
                assert strip.evaluate("n=>getComputedStyle(n).overflowY") == "hidden"
                assert strip.evaluate("n=>getComputedStyle(n).overflowX") == "auto"
                assert strip.evaluate("n=>n.scrollWidth>n.clientWidth")
                strip.locator('button').last.click()
                assert strip.evaluate("n=>n.scrollLeft>0")
                assert page.locator('#workflow-page-position').inner_text() == "12 / 12"
                assert strip.evaluate("""n=>{const b=n.getBoundingClientRect();return [...n.children].every(c=>{
                  const r=c.getBoundingClientRect();return r.top>=b.top && r.bottom<=b.top+n.clientHeight;});}""")
            page.evaluate("setWorkflowPane('graph')")
            page.set_viewport_size({"width": 420, "height": 850})
            page.locator("#workflow-chat-input").scroll_into_view_if_needed()
            assert page.locator(".workflow-conversation").is_visible()
            assert page.locator(".workflow-conversation").bounding_box()["width"] <= 420
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            page.screenshot(path=str(tmp_path / "workflow-sidebar-mobile.png"), full_page=True)
            page.evaluate("ed.kind='dynamic';ed.workspaceView='process';syncViewToggle()")
            assert "workspace-process" in page.locator("#editor").get_attribute("class")
            assert page.locator("#workspace-primary-label").inner_text() == "过程"
            assert not errors, errors
            assert all(method == "GET" for method, _ in requests), requests
        finally:
            browser.close()
