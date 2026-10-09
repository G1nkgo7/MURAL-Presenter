"""WebUI 依赖留在 Studio；真实 Chromium 在正式 inference Python 中运行。"""
import json
import os
from pathlib import Path
import subprocess

from app import engine


def test_task_dag_and_attachment_states_in_formal_chromium(tmp_path):
    studio = Path(__file__).resolve().parents[1]
    browser_tmp = tmp_path / "browser"
    browser_tmp.mkdir()
    env = engine.render_env(dict(os.environ))
    for key in ("TMPDIR", "TMP", "TEMP"):
        env[key] = str(browser_tmp)
    script = r'''
import json, os, re, sys
from pathlib import Path
from playwright.sync_api import sync_playwright
studio = Path(sys.argv[1])
source = (studio / "static/app.js").read_text()
names = ("workflowTaskGraph", "renderWorkflowCanvas", "workflowDrawEdges", "workflowNodeMarkup",
         "reconcileWorkflowNodes",
         "workflowExecutionStatus", "workflowCurrentActivity", "workflowFlowLayout", "workflowOverview", "renderWorkflowOverview", "liveToolTransactions",
         "runtimeEventState", "workflowStatusLabel", "attachmentParseLabel", "outlineBriefData", "outlineBriefMetaHtml")
functions = []
for name in names:
    start = re.search(rf"^function {name}\(", source, re.M).start()
    end = re.search(r"^(?:async )?function ", source[start + 1:], re.M)
    functions.append(source[start:start + 1 + end.start()])
setup = """
const $ = s => document.querySelector(s), escapeHtml = s => String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
const pad2=n=>String(n).padStart(2,'0'), fileUrl=n=>'/page/'+n;
const ed = {id:1,kind:'static',status:'running',fullQuery:'三页城市热岛微课：研究遥感证据，准备情境图片，制作图层推理页面。',workflowPane:'graph',rendered:new Set(),agentTimings:{
  image:{task_id:'image-b',execution_state:'start_failed',status_detail:'本批次启动失败'}},feed:{},muralRuntime:{tasks:[
  {task_id:'research',role:'research',status:'running',dependencies:[]},
  {task_id:'image-a',role:'image',agent_key:'image',owner_id:'image-owner',status:'succeeded',dependencies:[],result:{segment_ids:['segment-a']}},
  {task_id:'image-b',role:'image',agent_key:'image',owner_id:'image-owner',status:'failed',dependencies:['image-a']},
  {task_id:'slide-free',role:'slide',agent_key:'slide-group',pages:[1,2],status:'running',dependencies:[]},
  {task_id:'slide-wait',role:'slide',pages:[3],status:'skipped',dependencies:['image-b']}]},
  attachments:[{name:'report.pdf',stored_name:'report.pdf',source_sha256:'a',upload_status:'saved',type:'pdf'}],
  attachmentParse:[{name:'report.pdf',source_sha256:'a',status:'partial',unresolved_count:2}]};
const renderWorkflowSidePanels=()=>{}, renderWorkflowOwnerGraph=()=>{}, syncWorkflowLayoutReset=()=>{},
  scheduleWorkflowEdgeDraw=stage=>requestAnimationFrame(()=>workflowDrawEdges(stage,stage._workflowEdges)),
  deckAttachmentPreviewUrls=()=>({url:'/attachment'}), attachmentPreviewKind=()=> 'pdf',
  attachmentTypeLabel=()=> 'PDF', attachmentSizeLabel=()=> '';
renderWorkflowCanvas();
$('#attachments').innerHTML=outlineBriefMetaHtml(outlineBriefData());
"""
errors=[]
with sync_playwright() as pw:
    browser = pw.chromium.launch(executable_path=os.environ['PPT_SKILL_BROWSER_EXE'],
        env=dict(os.environ),headless=True,args=['--no-sandbox'])
    try:
        page=browser.new_page(viewport={'width':1100,'height':1000})
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.route('**/*',lambda route:route.abort())
        page.set_content('<div id="attachments"></div><div id="workflow-live-state"></div><div id="workflow-scroll" style="width:100%;height:900px;overflow:auto"><section id="workflow-overview" class="workflow-overview"></section><div id="workflow-stage" class="workflow-stage"></div></div>')
        page.add_style_tag(path=str(studio/'static/app.css'))
        page.add_script_tag(content='\n'.join(functions)+'\n'+setup)
        page.wait_for_function('document.querySelectorAll(".workflow-edge").length===13')
        assert page.locator('.workflow-task-node').count()==5
        assert page.locator('[data-node-id="orch"] strong').inner_text()=='Orchestrator · 整册统筹'
        assert 'unknown' in page.locator('[data-node-id="orch"]').get_attribute('class')
        assert page.locator('[data-node-id="orch"]').evaluate("n=>getComputedStyle(n,'::after').animationName")=='none'
        assert page.locator('[data-node-id="task:slide-free"]').evaluate("n=>getComputedStyle(n,'::after').animationName")=='none'
        assert '正在推进：研究与规划、页面制作' in page.locator('#workflow-overview').inner_text()
        assert '尚无整册验收回执' in page.locator('[data-workflow-phase="review"]').inner_text()
        assert page.locator('[data-node-id="task:image-a"]').get_attribute('data-workflow-segment')=='segment-a'
        assert 'start_failed' in page.locator('[data-node-id="task:image-b"]').get_attribute('class')
        assert 'dependency_failed' in page.locator('[data-node-id="task:slide-wait"]').get_attribute('class')
        assert 'image-b' in page.locator('[data-node-id="task:slide-wait"] p').get_attribute('title')
        assert '同一 Owner · 批次 2/2' in page.locator('[data-node-id="task:image-b"]').inner_text()
        ids=['query','orch','task:research','blueprint','task:image-a','task:slide-free','assemble','review','delivery']
        tops=[page.locator(f'[data-node-id="{key}"]').evaluate('n=>n.offsetTop') for key in ids]
        assert tops==sorted(set(tops))
        assert '分层流程示意，不表示实际等待依赖' in page.locator('.workflow-relations').inner_text()
        page.evaluate("ed.muralRuntime.deck_timeline=[{type:'task_submitted',task_id:'slide-free'}];renderWorkflowCanvas()")
        page.wait_for_function('document.querySelectorAll(".workflow-edge").length===13')
        assert page.locator('.workflow-edge.delegation').count()==0
        assert page.locator('.workflow-edge[data-from="orch"][data-to="task:slide-free"]').count()==0
        current=page.locator('.workflow-edge[data-to="task:slide-free"] + .workflow-edge-flow')
        assert current.count()==2
        assert all('active' in row for row in current.evaluate_all('nodes=>nodes.map(n=>n.getAttribute("class"))'))
        assert current.first.evaluate('n=>getComputedStyle(n).animationName')=='workflow-dash'
        # 连续动作更新必须复用节点和动画；不能靠减少轮询掩盖整图重建。
        page.evaluate("ed.rendered.add(1);renderWorkflowCanvas()")
        page.wait_for_function('document.querySelector(".workflow-edge-flow.active").getAnimations()[0]?.startTime != null')
        page.evaluate("""window.stableGraph={
          cards:new Map([...document.querySelectorAll('.workflow-node')].map(n=>[n.dataset.nodeId,n])),
          paths:[...document.querySelectorAll('.workflow-edge,.workflow-edge-flow')],
          layer:document.querySelector('.workflow-edge-layer'),
          thumb:document.querySelector('[data-node-id="task:slide-free"] img'),
          dot:document.querySelector('[data-node-id="task:slide-free"] .workflow-node-foot i'),
          animation:document.querySelector('.workflow-edge-flow.active').getAnimations()[0],
          overview:document.querySelector('.workflow-query'),
          live:document.querySelector('#workflow-live-state i')};
          stableGraph.animationStart=stableGraph.animation.startTime;
          stableGraph.button=document.querySelector('[data-node-id="task:slide-free"] .workflow-node-drilldown');
          stableGraph.button.focus({preventScroll:true});
          document.querySelector('#workflow-scroll').scrollTop=480;
          stableGraph.scrollTop=document.querySelector('#workflow-scroll').scrollTop;
        """)
        for turn in range(1, 6):
            page.evaluate("""async turn=>{
              ed.feed['slide-group']=[{k:'text',task_id:'slide-free',turn,s:'正在调整页面，第 '+turn+' 轮'}];
              renderWorkflowCanvas();
              await new Promise(requestAnimationFrame);
            }""", turn)
            assert page.locator('[data-node-id="task:slide-free"] .workflow-node-activity').inner_text().endswith(f'第 {turn} 轮')
            assert page.evaluate("""[...stableGraph.cards].every(([id,node])=>node.isConnected&&node===document.querySelector(`[data-node-id="${id}"]`))
              && stableGraph.paths.every(n=>n.isConnected)
              && stableGraph.layer===document.querySelector('.workflow-edge-layer')
              && stableGraph.thumb===document.querySelector('[data-node-id="task:slide-free"] img')
              && stableGraph.dot===document.querySelector('[data-node-id="task:slide-free"] .workflow-node-foot i')
              && stableGraph.overview===document.querySelector('.workflow-query')
              && stableGraph.live===document.querySelector('#workflow-live-state i')
              && document.activeElement===stableGraph.button
              && stableGraph.animation===document.querySelector('.workflow-edge-flow.active').getAnimations()[0]
              && stableGraph.animation.startTime===stableGraph.animationStart
              && document.querySelector('#workflow-scroll').scrollTop===stableGraph.scrollTop""")
        page.evaluate("""ed.workflowNodeOffsets={'task:slide-free':{x:18,y:7}};renderWorkflowCanvas();
          workflowDrawEdges(document.querySelector('#workflow-stage'),document.querySelector('#workflow-stage')._workflowEdges);""")
        assert page.locator('[data-node-id="task:slide-free"]').evaluate("n=>n.offsetLeft===Number(n.dataset.baseX)+18 && n.offsetTop===Number(n.dataset.baseY)+7")
        assert page.evaluate('stableGraph.paths.every(n=>n.isConnected)')
        page.evaluate("ed.workflowNodeOffsets={};renderWorkflowCanvas();document.querySelector('#workflow-scroll').scrollTop=0")
        page.emulate_media(reduced_motion='reduce')
        assert current.first.evaluate('n=>getComputedStyle(n).animationName')=='none'
        assert page.locator('[data-node-id="task:slide-free"]').evaluate("n=>getComputedStyle(n,'::after').animationName")=='none'
        page.emulate_media(reduced_motion='no-preference')
        for status in ('queued','resource_wait','waiting_dependencies','failed','succeeded'):
            page.evaluate('(status)=>{ed.muralRuntime.tasks.find(t=>t.task_id==="slide-free").status=status;renderWorkflowCanvas()}',status)
            page.wait_for_function("""document.querySelectorAll('.workflow-edge[data-to="task:slide-free"] + .workflow-edge-flow.active').length===0""")
            assert page.locator('[data-node-id="task:slide-free"]').evaluate("n=>getComputedStyle(n,'::after').animationName")=='none'
        page.evaluate('ed.muralRuntime.tasks.find(t=>t.task_id==="slide-free").status="running";renderWorkflowCanvas()')
        page.wait_for_function("""document.querySelectorAll('.workflow-edge[data-to="task:slide-free"] + .workflow-edge-flow.active').length===2""")
        page.locator('.workflow-query summary').click()
        page.evaluate("ed.feed.orch=[{event_type:'agent_started'},{event_type:'provider_started',model:'27b'}];renderWorkflowCanvas()")
        assert page.locator('.workflow-query').get_attribute('open') is not None
        assert '等待模型响应' in page.locator('[data-node-id="orch"]').inner_text()
        page.locator('.workflow-query summary').click()
        page.evaluate("""ed.muralRuntime.tasks.find(t=>t.task_id==='slide-free').agent_key='slide_group_free';
          ed.feed.slide_group_free=[{event_type:'agent_started',task_id:'slide-free',segment_id:'live'},
            {k:'tool',tool:'inspect',tool_use_id:'inspect-current',task_id:'slide-free',turn:3,hint:'P2 图层叠加示意页'}];renderWorkflowCanvas()""")
        assert '执行工具 · inspect' in page.locator('[data-node-id="task:slide-free"] .workflow-node-activity').inner_text()
        assert page.locator('[data-node-id="task:slide-free"] .workflow-node-progress').inner_text()=='第 3 回合'
        assert '委派任务完成 1 / 5' in page.locator('[data-node-id="orch"]').inner_text()
        boxes = page.locator('.workflow-node').evaluate_all('(nodes)=>nodes.map(n=>{const r=n.getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height}})')
        for i, a in enumerate(boxes):
            for b in boxes[i+1:]:
                assert a['x']+a['w']<=b['x'] or b['x']+b['w']<=a['x'] or a['y']+a['h']<=b['y'] or b['y']+b['h']<=a['y']
        assert '部分解析完成' in page.locator('#attachments').inner_text()
        assert '2 项未解决' in page.locator('#attachments').inner_text()
        assert '不代表' in page.locator('.outline-attachment').get_attribute('title')
        page.evaluate("ed.attachments[0].source_sha256='b';$('#attachments').innerHTML=outlineBriefMetaHtml(outlineBriefData())")
        assert '原件已保存' in page.locator('#attachments').inner_text()
        assert '部分解析完成' not in page.locator('#attachments').inner_text()
        page.evaluate("""ed.muralRuntime.tasks.push(...[4,5,6,7].map(page=>({
          task_id:`slide-${page}`,role:'slide',pages:[page],status:page===5?'running':'succeeded',
          dependencies:page===5?['slide-4']:[]})));renderWorkflowCanvas()""")
        page.wait_for_function('document.querySelectorAll(".workflow-edge").length===25')
        assert page.locator('.workflow-task-node[data-role="slide"]').count()==6
        assert page.locator('[data-node-id="orch"]').evaluate('n=>Math.abs(n.offsetLeft+n.offsetWidth/2-n.parentNode.offsetWidth/2)<1')
        assert page.locator('.workflow-task-node[data-role="slide"]').evaluate_all("""nodes=>{
          const specialists=[...document.querySelectorAll('.workflow-task-node:not([data-role="slide"])')];
          return Math.min(...nodes.map(n=>n.offsetTop))>Math.max(...specialists.map(n=>n.offsetTop+n.offsetHeight));
        }""")
        assert page.locator('.workflow-edge[data-to="task:slide-5"]').count()==2
        assert page.locator('.workflow-edge[data-to="task:slide-5"] + .workflow-edge-flow.active').count()==2
        page.evaluate("ed.muralRuntime.deck_timeline=ed.muralRuntime.tasks.map(t=>({type:'task_submitted',task_id:t.task_id}));renderWorkflowCanvas()")
        page.wait_for_function('document.querySelectorAll(".workflow-edge").length===25')
        assert page.locator('.workflow-edge').evaluate_all('edges=>edges.every(edge=>edge.getAttribute("d").includes(" C")&&!edge.getAttribute("d").includes(" L"))')
        assert page.locator('.workflow-edge').evaluate_all("""edges=>{
          return edges.every(edge=>{
            const unrelated=[...document.querySelectorAll('.workflow-node')].filter(n=>![edge.dataset.from,edge.dataset.to].includes(n.dataset.nodeId));
            for(let t=0;t<=1;t+=.005){const p=edge.getPointAtLength(t*edge.getTotalLength());
              if(unrelated.some(n=>p.x>n.offsetLeft&&p.x<n.offsetLeft+n.offsetWidth
                &&p.y>n.offsetTop&&p.y<n.offsetTop+n.offsetHeight)) return false;}
            return true;
          });
        }""")
        assert page.evaluate('$("#workflow-scroll").scrollWidth>$("#workflow-scroll").clientWidth')
        assert page.locator('#workflow-overview').bounding_box()['x']>=0
        if os.environ.get('MURAL_WORKFLOW_SCREENSHOT'):
            screenshot = Path(os.environ['MURAL_WORKFLOW_SCREENSHOT'])
            page.screenshot(path=str(screenshot), full_page=True)
            page.evaluate("document.body.style.height='auto';document.body.style.overflow='visible';$('#workflow-scroll').style.height='auto';$('#workflow-scroll').style.overflow='visible'")
            page.screenshot(path=str(screenshot.with_stem(screenshot.stem+'-full')), full_page=True)
        page.set_viewport_size({'width':620,'height':1000})
        page.evaluate('renderWorkflowCanvas()')
        page.wait_for_function('document.querySelectorAll(".workflow-edge").length===25')
        assert page.locator('#workflow-overview').bounding_box()['width']<=620
        assert page.locator('[data-node-id="orch"]').count()==1
        assert page.locator('.workflow-edge').count()==25
        assert page.locator('[data-node-id="orch"]').evaluate('n=>Math.abs(n.offsetLeft+n.offsetWidth/2-n.parentNode.offsetWidth/2)<1')
        assert page.evaluate('$("#workflow-scroll").scrollWidth>$("#workflow-scroll").clientWidth')
        assert page.locator('.workflow-task-node[data-role="slide"]').evaluate_all('nodes=>new Set(nodes.map(n=>n.offsetTop)).size===1')
        assert page.locator('.workflow-task-node').evaluate_all("""nodes=>nodes.every(n=>{
          const card=n.getBoundingClientRect(), foot=n.querySelector('.workflow-node-foot').getBoundingClientRect();
          return n.offsetHeight<=276 && foot.bottom<card.bottom && foot.right<=card.right;
        })""")
        page.evaluate("ed.status='interrupted';renderWorkflowCanvas()")
        page.wait_for_function('document.querySelectorAll(".workflow-edge-flow.active").length===0')
        assert page.locator('.workflow-node.running').count()==0
        assert not errors,errors
        page.evaluate("ed.muralRuntime={};ed.feed={};ed.agentTimings={};renderWorkflowCanvas()")
        assert page.locator('.workflow-task-node').count()==0
        assert '接口尚未提供任务列表' in page.locator('#workflow-stage').inner_text()
        assert page.locator('[data-node-id="orch"]').count()==1
        print(json.dumps({'python':sys.executable,'nodes':9,'flow_edges':25,'browser_errors':errors}))
    finally:
        browser.close()
'''
    result = subprocess.run([engine._engine_python(), "-B", "-c", script, str(studio)],
                            env=env, text=True, capture_output=True, timeout=90)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["python"] == engine._engine_python()
    assert report["flow_edges"] == 25 and report["browser_errors"] == []
