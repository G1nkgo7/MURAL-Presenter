"""Agent 协作图下钻只读真实事件，检查像素版本、Critic 归属和浏览器交互。"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

from PIL import Image
import pytest

from app import main, trace
from test_delivery_download import download_case


def append(root, sequence, kind, payload=None, *, owner="owner-a", segment="s1", role="slide"):
    event = {"schema": "mural.live-event.v1", "sequence": sequence,
             "timestamp": f"2026-09-09T00:00:{sequence:02d}Z", "event_id": f"event-{sequence}",
             "agent_id": owner, "role": role, "segment_id": segment, "turn": sequence,
             "event_type": kind, "payload": payload or {}}
    with (root / "_trace/live-events.jsonl").open("a") as output:
        output.write(json.dumps(event) + "\n")
    return event


@pytest.fixture
def case(download_case):
    root = download_case["root"]
    (root / "_state/owners.json").write_text(json.dumps({"owners": {
        "a": {"owner_id": "owner-a", "role": "slide", "group_id": "a", "pages": [1, 2], "status": "running"},
        "b": {"owner_id": "owner-b", "role": "slide", "group_id": "b", "pages": [3], "status": "pending"},
    }}))
    blobs = root / "_trace/training/blobs"
    blobs.mkdir(parents=True)
    png = blobs / "source.png"
    Image.new("RGB", (160, 90), "orange").save(png)
    digest = hashlib.sha256(png.read_bytes()).hexdigest()
    png.rename(blobs / f"{digest}.png")
    append(root, 1, "agent_started")
    append(root, 2, "tool_use", {"tool_use_id": "inspect-1", "name": "inspect", "arguments": {"page": 1, "question": "标题是否清晰，主体是否完整？"}})
    feedback = {"inspection_id": "review-1", "png_sha256": digest, "page": 1, "page_uid": "page-1",
                "completed": True, "body": "标题重叠；修正父容器。", "incomplete_reason": "",
                "critic_model": "27b", "rubric_version": "rubric-test", "latency_s": 1.2,
                "source_refs": {"segment_id": "critic-segment"}, "reused_from": {}, "cache_hit": False}
    append(root, 3, "critic_started", {"inspection_id": "review-1", "png_sha256": digest,
        "page": 1, "owner_id": "owner-a", "tool_use_id": "inspect-1"}, owner="critic-1", segment="critic-segment", role="critic")
    append(root, 4, "critic_completed", {**feedback, "owner_id": "owner-a", "tool_use_id": "inspect-1"},
           owner="critic-1", segment="critic-segment", role="critic")
    append(root, 5, "tool_result", {"tool_use_id": "inspect-1", "name": "inspect", "ok": True, "duration_ms": 500,
        "result": json.dumps({"inspection_id": "review-1", "page": 1, "png_sha256": digest,
                              "native_multimodal": True, "critic_feedback": feedback})})
    append(root, 6, "tool_use", {"tool_use_id": "patch-1", "name": "patch", "arguments": {
        "path": "slides/slide_01.html", "repair_reason": "调整父容器", "new_text": "not displayed"}})
    append(root, 7, "tool_result", {"tool_use_id": "patch-1", "name": "patch", "ok": False,
        "error_code": "patch_not_found", "result": {"message": "匹配失败", "api_key": "secret-value"}})
    return {**download_case, "digest": digest}


def graph(case):
    return trace.agent_trace_payload(str(case["root"]), "slide_group_a", include_workflow=True)["workflow"]


def test_projection_pairs_transactions_and_exact_critic_images_without_writes(case):
    root = case["root"]
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    data = graph(case)
    rendered = next(n for n in data["nodes"] if n["kind"] == "render")
    critic = next(n for n in data["nodes"] if n["kind"] == "critic")
    edit = next(n for n in data["nodes"] if n["kind"] == "edit")
    assert rendered["duration_ms"] == 500
    assert rendered["images"][0]["rel"] == f'_trace/training/blobs/{case["digest"]}.png'
    assert critic["render_id"] == rendered["id"] and critic["status"] == "completed"
    assert "verdict" not in critic and "disposition" not in critic
    assert critic["tool_use_id"] == "inspect-1" and critic["critic_model"] == "27b"
    assert critic["rubric_version"] == "rubric-test"
    assert critic["source_refs"] == {"segment_id": "critic-segment"}
    assert critic["feedback"] == "标题重叠；修正父容器。"
    assert edit["status"] == "failed" and edit["error"] == "匹配失败"
    assert "成功不代表视觉通过" in rendered["observation"]
    assert data["pages"] == [1, 2]
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    serialized = json.dumps(data)
    for secret in ("secret-value", "hidden-thought", "hidden-signature", "not displayed"):
        assert secret not in serialized
    assert "workflow" not in trace.agent_trace_payload(str(root), "slide_group_a")


def test_critic_own_entry_remains_linked_to_exact_owner_inspect(case):
    from test_critic_feed_ui import entries

    feed = trace.live_events(str(case["root"]))["agents"]
    review_entry, = entries({"critic": feed["critic"]})["entries"]
    assert review_entry["type"] == "critic-review"
    assert review_entry["info"]["owner_id"] == "owner-a"
    own = graph(case)["nodes"]
    review = next(node for node in own if node["kind"] == "critic")
    render = next(node for node in own if node["id"] == review["render_id"])
    assert review["feedback"] == "标题重叠；修正父容器。"
    assert review["pages"] == [1] and review["png_sha256"] == case["digest"]
    assert review["tool_use_id"] == render["tool_use_id"] == "inspect-1"
    other = trace.agent_trace_payload(str(case["root"]), "slide_group_b", include_workflow=True)["workflow"]
    assert not any(node["kind"] == "critic" for node in other["nodes"])


def test_live_resume_dedup_and_failover_keep_order_and_running_tools(case):
    root = case["root"]
    append(root, 8, "agent_failed")
    append(root, 9, "agent_started", segment="s2")
    append(root, 10, "provider_failover", {"reason": "channel unavailable"}, segment="s2")
    event = append(root, 11, "tool_use", {"tool_use_id": "inspect-2", "name": "inspect", "arguments": {"page": 2}}, segment="s2")
    with (root / "_trace/live-events.jsonl").open("a") as output:
        output.write(json.dumps(event) + "\n" + '{"schema":')
    nodes = graph(case)["nodes"]
    assert [n["sequence"] for n in nodes] == sorted({n["sequence"] for n in nodes})
    assert nodes[-1]["status"] == "running" and nodes[-1]["segment_id"] == "s2"
    assert any(n["title"] == "Provider 切换通道" for n in nodes)
    assert any(n["segment_id"] == "s1" for n in nodes)


@pytest.mark.parametrize("defect", ["missing", "tampered", "symlink"])
def test_never_substitutes_current_image_or_follows_external_blob(case, defect, tmp_path):
    root, digest = case["root"], case["digest"]
    blob = root / f"_trace/training/blobs/{digest}.png"
    original = blob.read_bytes()
    blob.unlink()
    if defect == "tampered":
        blob.write_bytes(b"changed")
    elif defect == "symlink":
        outside = tmp_path.parent / (tmp_path.name + "-outside.png")
        outside.write_bytes(original)
        blob.symlink_to(outside)
    (root / "renders").mkdir()
    Image.new("RGB", (160, 90), "blue").save(root / "renders/slide_01.png")
    assert next(n for n in graph(case)["nodes"] if n["kind"] == "render")["images"][0]["rel"] == ""


def test_critic_does_not_cross_owner_or_pixel_and_feedback_is_one_node(case):
    root = case["root"]
    for seq, payload in [(8, {"owner_id": "owner-b", "png_sha256": case["digest"]}),
                         (9, {"owner_id": "owner-a", "png_sha256": "0" * 64}),
                         (10, {"owner_id": "owner-a", "png_sha256": case["digest"]})]:
        append(root, seq, "critic_completed", {"inspection_id": "review-1", "completed": True, **payload}, owner="critic-1", role="critic")
    reviews = [n for n in graph(case)["nodes"] if n["kind"] == "critic"]
    assert len(reviews) == 1 and reviews[0]["updates"] == 3
    other = trace.agent_trace_payload(str(root), "slide_group_b", include_workflow=True)
    assert other["workflow"]["nodes"] == [] and other["empty_state"]["kind"] == "not_started"


@pytest.mark.parametrize("event_type", ["critic_started", "critic_reused"])
def test_critic_waiting_binds_exact_owner_and_inspect_call_before_result(case, event_type):
    root = case["root"]
    journal = root / "_trace/live-events.jsonl"
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    rows[2]["event_type"] = event_type
    if event_type == "critic_reused":
        rows[2]["payload"].update(pending=True, reused_from_inspection_id="source-review")
    journal.write_text("\n".join(json.dumps(row) for row in rows[:3]) + "\n")
    append(root, 4, "critic_started", {"inspection_id": "foreign", "owner_id": "owner-b",
        "tool_use_id": "inspect-1", "page": 1, "png_sha256": case["digest"]}, owner="critic-2", role="critic")
    nodes = graph(case)["nodes"]
    reviews = [row for row in nodes if row["kind"] == "critic"]
    assert len(reviews) == 1 and reviews[0]["status"] == "running"
    render = next(row for row in nodes if row["kind"] == "render")
    assert reviews[0]["render_id"] == render["id"]
    assert render["images"][0]["sha256"] == case["digest"]
    assert reviews[0].get("finished_at") is None
    assert reviews[0]["cache_hit"] is (event_type == "critic_reused")
    assert "observation" not in render and not any(row["kind"] == "message" for row in nodes)


@pytest.mark.parametrize("completed,reused", [(False, False), (True, True)])
def test_inspect_feedback_alone_preserves_failure_and_reuse_without_owner_message(case, completed, reused):
    journal = case["root"] / "_trace/live-events.jsonl"
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    rows = [row for row in rows if not row["event_type"].startswith("critic_")]
    result = json.loads(rows[2]["payload"]["result"])
    result["critic_feedback"].update(completed=completed, body="没有明显问题" if completed else "",
        incomplete_reason="Provider timeout" if not completed else "",
        reused_from={"inspection_id": "earlier"} if reused else {}, cache_hit=reused)
    rows[2]["payload"]["result"] = json.dumps(result)
    journal.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    nodes = graph(case)["nodes"]
    review = next(row for row in nodes if row["kind"] == "critic")
    assert review["status"] == ("completed" if completed else "failed")
    assert review["cache_hit"] is reused
    assert review["error"] == ("" if completed else "Provider timeout")
    assert not any(row["kind"] == "message" for row in nodes)


def test_draft_inspect_feedback_is_scoped_not_failed_external_critic(case):
    journal = case["root"] / "_trace/live-events.jsonl"
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    rows = [row for row in rows if not row["event_type"].startswith("critic_")]
    result = json.loads(rows[2]["payload"]["result"])
    result["review_stage"] = "draft"
    result["critic_feedback"].update(
        completed=False,
        skipped=True,
        skip_reason="page_review_stage_draft",
        incomplete_reason="skipped_by_policy:page_review_stage_draft",
        status="draft_layout_check",
        checked_scope="layout_skeleton_only",
        asset_usage_status="not_verified_by_draft_inspect",
        not_checked=["required asset presence or usage", "external visual Critic judgement"],
        next_required_action='After assets are placed, run inspect with review_stage:"final".',
        body="草稿阶段：已查看当前页面像素；未调用外部 Critic，也不代表素材已使用或终稿视觉通过。",
    )
    rows[2]["payload"]["result"] = json.dumps(result)
    journal.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    review = next(row for row in graph(case)["nodes"] if row["kind"] == "critic")
    assert review["status"] == "skipped"
    assert review["title"] == "草稿 / 筛查检查（未调用外部 Critic）"
    assert review["error"] == ""
    assert "素材已使用" in review["feedback"]
    assert review["checked_scope"] == "layout_skeleton_only"


def test_tool_feedback_never_substitutes_another_pixel_version(case):
    journal = case["root"] / "_trace/live-events.jsonl"
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    rows = [row for row in rows if not row["event_type"].startswith("critic_")]
    result = json.loads(rows[2]["payload"]["result"])
    result["critic_feedback"]["png_sha256"] = "0" * 64
    rows[2]["payload"]["result"] = json.dumps(result)
    journal.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    assert not any(row["kind"] == "critic" for row in graph(case)["nodes"])


def test_critic_failed_event_is_visible_before_inspect_result(case):
    journal = case["root"] / "_trace/live-events.jsonl"
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    journal.write_text("\n".join(json.dumps(row) for row in rows[:3]) + "\n")
    append(case["root"], 4, "critic_failed", {"inspection_id": "review-1", "owner_id": "owner-a",
        "png_sha256": case["digest"], "completed": False, "incomplete_reason": "Provider timeout"}, owner="critic-1", role="critic")
    review = next(row for row in graph(case)["nodes"] if row["kind"] == "critic")
    assert review["status"] == "failed"
    assert review["error"] == "Provider timeout" and review["finished_at"]
    assert not any(row["kind"] == "message" for row in graph(case)["nodes"])


def test_only_real_owner_message_is_shown_despite_critical_feedback(case):
    body = "我核对了当前截图，决定保留这一版并结束。"
    append(case["root"], 8, "assistant_message", {"message": body})
    nodes = graph(case)["nodes"]
    messages = [row for row in nodes if row["kind"] == "message"]
    assert len(messages) == 1 and messages[0]["detail"] == body
    assert next(row for row in nodes if row["kind"] == "critic")["status"] == "completed"


def test_owned_api_and_image_authentication(case):
    result = main.deck_agent_trace(1, "slide_group_a", include_workflow=True, user={"id": 7}, con=case["con"])
    assert result["workflow"]["nodes"]
    image = next(n for n in result["workflow"]["nodes"] if n["kind"] == "render")["images"][0]
    response = main.deck_file_path(1, image["rel"], user={"id": 7}, con=case["con"])
    assert Path(response.path).read_bytes() == (case["root"] / image["rel"]).read_bytes()
    for handler, arg in ((main.deck_agent_trace, "slide_group_a"), (main.deck_file_path, image["rel"])):
        with pytest.raises(main.HTTPException) as error:
            handler(1, arg, user={"id": 8}, con=case["con"])
        assert error.value.status_code == 404
    with pytest.raises(main.HTTPException) as error:
        main.require_user(None)
    assert error.value.status_code == 401


def test_chromium_drilldown_filter_refresh_and_back(case, monkeypatch):
    from playwright.sync_api import sync_playwright
    studio = Path(__file__).resolve().parents[1]
    source = (studio / "static/app.js").read_text()
    names = ("escapeHtml", "workflowAgentTraceUrl", "workflowArtifactUrl", "openWorkflowOwnerGraph",
             "closeWorkflowOwnerGraph", "renderWorkflowOwnerGraph", "bindWorkflowOwnerGraph",
             "loadWorkflowAgentTrace", "cancelWorkflowAgentTraceRequest", "openWorkflowAgentTrace",
             "bindWorkflowCenterControls", "syncWorkflowLayoutReset", "workflowNodeMarkup",
             "workflowStatusLabel", "syncWorkflowConversationMode", "workflowDefaultTraceAgentKey",
             "workflowOwnerNodeMarkup", "workflowOwnerReadState", "workflowOwnerPageNodes", "setWorkflowConversationMode",
             "workflowTaskGraph", "workflowExecutionStatus", "workflowCurrentActivity", "runtimeEventState", "attachmentParseLabel")
    functions = []
    for name in names:
        start = re.search(rf"^(?:async )?function {name}\(", source, re.M).start()
        end = re.search(r"^(?:async )?function ", source[start + 1:], re.M)
        functions.append(source[start:start + 1 + end.start()])
    template = (studio / "templates/app.html").read_text()
    panel = re.search(r'<section class="workflow-owner-graph".*?</section>', template, re.S).group()
    html = '<div id="workflow-scroll" style="height:700px;overflow:auto"><section id="workflow-overview" style="height:80px">整册概览</section>' + panel + '<div id="workflow-stage" class="workflow-stage"></div></div><div id="workflow-trace-messages"></div><input id="workflow-trace-search">'
    setup = """
const $ = s => document.querySelector(s), $$ = s => [...document.querySelectorAll(s)];
const ed = {id:1, status:'running', rendered:new Set(), workflowPane:'graph', workflowTraceRequest:0, workflowTraceAgent:'', workflowPanelCollapsed:{}};
const pad2=n=>String(n).padStart(2,'0');
const trajectoryMode=false, WORKFLOW_TRACE_REFRESH_MS=0, WORKFLOW_TRACE_REQUEST_TIMEOUT_MS=2000;
const isStaticActiveStatus=s=>s==='running', refreshWorkflowToolOutputs=()=>{};
const syncWorkflowPanelLayout=()=>{}, bindWorkflowNodeDragging=()=>{}, bindWorkflowSplitter=()=>{};
const renderWorkflowCanvas=()=>renderWorkflowOwnerGraph(), renderWorkflowAgentTrace=()=>{}, renderWorkflowConversation=()=>{};
const setWorkflowPane=()=>{}, renderWorkflowPagePane=()=>{};
$('#workflow-stage').innerHTML=workflowNodeMarkup({id:'owner-a',agentKey:'slide_group_a',role:'slide',pages:[1,2],title:'Owner A',status:'running',kicker:'Slide Owner',detail:'制作页面',x:40,y:40});
bindWorkflowCenterControls();
"""
    env = main.engine.render_env(dict(os.environ))
    executable = env.get("PPT_SKILL_BROWSER_EXE")
    assert executable
    browser_tmp = case["root"].parent / (case["root"].name + "-browser")
    browser_tmp.mkdir()
    for key in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(key, str(browser_tmp)); env[key] = str(browser_tmp)
    payload = trace.agent_trace_payload(str(case["root"]), "slide_group_a", include_workflow=True)
    ui_fonts = {
        "/static/fonts/InstrumentSans.ttf": studio / "static/fonts/InstrumentSans.ttf",
        "/static/fonts/SchibstedGrotesk.ttf": studio / "static/fonts/SchibstedGrotesk.ttf",
        "/api/preview-fonts/NotoSansSC.ttf": main.preview_fonts.catalog()["NotoSansSC.ttf"],
    }
    response_status = 200
    requests = []
    errors = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=executable, env=env, headless=True, args=["--no-sandbox"])
        try:
            page = browser.new_page(viewport={"width": 1100, "height": 800})
            page.set_default_timeout(10000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            def serve(route):
                url = route.request.url
                path = urlsplit(url).path
                requests.append(url)
                if "/agent-trace/" in url:
                    response = payload if "include_workflow=true" in url else {k: v for k, v in payload.items() if k != "workflow"}
                    route.fulfill(status=response_status, content_type="application/json", body=json.dumps(response))
                elif "/files/" in url:
                    route.fulfill(content_type="image/png", body=(case["root"] / url.split("/files/", 1)[1].split("?", 1)[0]).read_bytes())
                elif path in ui_fonts:
                    route.fulfill(content_type="font/ttf", body=ui_fonts[path].read_bytes())
                else:
                    route.fulfill(content_type="text/html; charset=utf-8", body=html)
            page.route("**/*", serve)
            page.goto("http://workflow.test/")
            page.add_style_tag(path=str(studio / "static/app.css"))
            page.add_script_tag(content="\n".join(functions) + setup)
            node = page.locator('.workflow-node[data-agent-key="slide_group_a"]')
            assert node.locator("[data-workflow-drilldown]").is_visible()
            node.locator("strong").click()
            page.wait_for_function("!ed.workflowTraceLoading && !!ed.workflowTracePayload")
            assert page.locator("#workflow-stage").is_visible()
            assert page.locator("#workflow-owner-graph").is_hidden()
            assert "trace-selected" in node.get_attribute("class")
            assert node.locator("[data-workflow-drilldown]").inner_text() == "查看迭代"
            page.evaluate("document.fonts.ready")
            page.wait_for_function("getComputedStyle(document.querySelector('.workflow-node-drilldown')).transform === 'none'")
            for role in ("slide", "orchestrator", "material", "research", "image"):
                node.evaluate("(card, role) => card.dataset.role = role", role)
                card_box = node.bounding_box()
                button_box = node.locator("[data-workflow-drilldown]").bounding_box()
                assert button_box["height"] <= 22 and button_box["width"] <= 60
                assert card_box["y"] - 3 <= button_box["y"] + button_box["height"] / 2 <= card_box["y"]
                assert button_box["y"] + button_box["height"] <= node.locator(".workflow-node-kicker").bounding_box()["y"]
            node.evaluate("card => card.dataset.role = 'slide'")
            page.screenshot(path=str(browser_tmp / "selected-card.png"), full_page=True)
            assert not any("include_workflow=true" in url for url in requests)
            node.locator("[data-workflow-drilldown]").click()
            page.wait_for_selector(".workflow-owner-node.kind-render img")
            page.evaluate("document.fonts.ready")
            cdp = page.context.new_cdp_session(page)
            cdp.send("DOM.enable")
            cdp.send("CSS.enable")
            document = cdp.send("DOM.getDocument")["root"]["nodeId"]
            note = cdp.send("DOM.querySelector", {"nodeId": document, "selector": "#workflow-owner-note"})["nodeId"]
            fonts = cdp.send("CSS.getPlatformFontsForNode", {"nodeId": note})["fonts"]
            assert any(font["isCustomFont"] and font["familyName"].startswith("Noto Sans SC") for font in fonts)
            cdp.detach()
            assert any("include_workflow=true" in url for url in requests)
            assert page.locator("#workflow-stage").is_hidden()
            assert page.locator("#workflow-overview").is_hidden()
            assert page.locator(".kind-render img").evaluate("img=>img.naturalWidth") == 160
            assert "反馈已返回" in page.locator(".kind-critic").inner_text()
            assert "标题重叠" in page.locator(".kind-critic").inner_text()
            assert "匹配失败" in page.locator(".kind-edit").inner_text()
            assert page.locator(".workflow-owner-review .kind-critic").count() == 1
            assert page.locator(".workflow-owner-changes .kind-edit").count() == 1
            canvas, checks, changes = [page.locator(selector).bounding_box() for selector in (
                ".workflow-owner-canvas", ".workflow-owner-checks", ".workflow-owner-changes")]
            owner, critic = [page.locator(selector).bounding_box() for selector in (
                ".workflow-owner-self-check", ".workflow-owner-review")]
            assert checks["y"] >= canvas["y"] + canvas["height"]
            assert changes["y"] >= checks["y"] + checks["height"]
            assert abs(owner["y"] - critic["y"]) < 2
            assert owner["x"] + owner["width"] < critic["x"]
            assert canvas["width"] > owner["width"] * 1.9
            assert page.locator(".workflow-owner-frame img").bounding_box()["width"] > 900
            assert "标题是否清晰" in page.locator(".workflow-owner-self-check").inner_text()
            assert "工具成功不代表视觉通过" in page.locator(".workflow-owner-self-check").inner_text()
            assert page.locator(".workflow-owner-self-check .verdict-READY").count() == 0
            assert "顺序不表示" in page.locator(".workflow-owner-changes").inner_text()
            assert page.locator(".workflow-owner-changes .workflow-owner-timeline .kind-edit").count() == 1
            assert page.locator('[data-owner-page="1"]').get_attribute("aria-pressed") == "true"
            assert page.locator(".workflow-owner-round").count() == 1
            assert page.locator(".workflow-owner-round b").inner_text() == "P1"
            assert "P1 · 画面 01 · V1" in page.locator(".workflow-owner-canvas").inner_text()
            page.screenshot(path=str(browser_tmp / "parallel-inspections.png"), full_page=True)
            page.locator(".kind-render").evaluate("node=>node.keptOnRefresh=true")
            page.evaluate("loadWorkflowAgentTrace({force:true})")
            assert page.locator(".kind-render").evaluate("node=>node.keptOnRefresh")
            page.locator('[data-owner-page="2"]').click()
            assert page.locator(".kind-render").count() == 0
            assert "P2" in page.locator("#workflow-owner-note").inner_text()
            page.locator('[data-owner-page="0"]').click()
            assert "执行段开始" in page.locator("#workflow-owner-nodes").inner_text()
            assert page.locator(".kind-render").count() == 0
            page.locator('[data-owner-page="2"]').click()
            append(case["root"], 8, "tool_use", {"tool_use_id": "inspect-2", "name": "inspect", "arguments": {"page": 2}})
            payload = trace.agent_trace_payload(str(case["root"]), "slide_group_a", include_workflow=True)
            page.locator("#workflow-owner-all-tools").check()
            page.evaluate("loadWorkflowAgentTrace({force:true})")
            assert page.locator(".workflow-owner-round").count() == 1
            page.locator('[data-owner-round="event-8"]').first.click()
            assert page.locator(".workflow-owner-frame.status-running").count() == 1
            digests = []
            for color in ("blue", "green"):
                image_path = case["root"] / f"_trace/training/blobs/{color}.png"
                Image.new("RGB", (160, 90), color).save(image_path)
                digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
                image_path.rename(image_path.with_name(f"{digest}.png"))
                digests.append(digest)
            append(case["root"], 9, "tool_result", {"tool_use_id": "inspect-2", "name": "inspect", "ok": True,
                "result": {"page": 2, "png_sha256": digests[0]}})
            append(case["root"], 10, "tool_use", {"tool_use_id": "inspect-3", "name": "inspect", "arguments": {"page": 1}})
            append(case["root"], 11, "tool_result", {"tool_use_id": "inspect-3", "name": "inspect", "ok": True,
                "result": {"page": 1, "png_sha256": digests[1]}})
            append(case["root"], 12, "tool_use", {"tool_use_id": "patch-2", "name": "patch", "arguments": {"path": "slides/slide_02.html"}})
            append(case["root"], 13, "tool_result", {"tool_use_id": "patch-2", "name": "patch", "ok": False,
                "result": {"message": "P2 修改失败"}})
            append(case["root"], 14, "assistant_message", {"message": "组级说明，不猜测归属单页。"})
            append(case["root"], 15, "tool_use", {"tool_use_id": "inspect-group", "name": "inspect_group", "arguments": {"pages": [1, 2]}})
            append(case["root"], 16, "tool_result", {"tool_use_id": "inspect-group", "name": "inspect_group", "ok": True,
                "result": {"pending_inspection": {"members": [{"page": 1, "png_sha256": digests[1]}, {"page": 2, "png_sha256": digests[0]}]}}})
            append(case["root"], 17, "critic_completed", {"inspection_id": "group-review-p2", "owner_id": "owner-a",
                "tool_use_id": "inspect-group", "page": 2, "png_sha256": digests[0], "completed": True,
                "body": "仅 P2 的像素反馈"}, owner="critic-2", role="critic")
            payload = trace.agent_trace_payload(str(case["root"]), "slide_group_a", include_workflow=True)
            page.evaluate("loadWorkflowAgentTrace({force:true})")
            assert page.locator('[data-owner-page="2"]').get_attribute("aria-pressed") == "true"
            assert page.locator(".workflow-owner-round").count() == 2
            assert page.locator(".workflow-owner-round b").all_text_contents() == ["P2", "P2"]
            assert "P2 修改失败" in page.locator(".workflow-owner-changes").inner_text()
            assert "组级说明" not in page.locator("#workflow-owner-nodes").inner_text()
            page.locator('[data-owner-round="event-15"]').first.click()
            assert page.locator(".workflow-owner-frame img").count() == 1
            assert digests[0] in page.locator(".workflow-owner-frame img").get_attribute("src")
            assert "仅 P2 的像素反馈" in page.locator(".workflow-owner-review").inner_text()
            page.screenshot(path=str(browser_tmp / "group-page-2.png"), full_page=True)
            page.locator('[data-owner-page="1"]').click()
            assert page.locator(".workflow-owner-round").count() == 3
            assert page.locator(".workflow-owner-round b").all_text_contents() == ["P1"] * 3
            assert "P2 修改失败" not in page.locator("#workflow-owner-nodes").inner_text()
            page.locator('[aria-label="下一个画面"]').click()
            assert page.locator(".workflow-owner-frame").get_attribute("data-owner-node") == "event-10"
            assert "P1 · 画面 02 · V2" in page.locator(".workflow-owner-canvas").inner_text()
            page.locator('[aria-label="下一个画面"]').click()
            assert page.locator(".workflow-owner-frame img").count() == 1
            assert digests[1] in page.locator(".workflow-owner-frame img").get_attribute("src")
            assert "仅 P2 的像素反馈" not in page.locator(".workflow-owner-review").inner_text()
            page.screenshot(path=str(browser_tmp / "group-page-1.png"), full_page=True)
            page.locator('[data-owner-page="0"]').click()
            assert "组级说明" in page.locator("#workflow-owner-nodes").inner_text()
            assert page.locator(".workflow-owner-round").count() == 0
            page.locator('[data-owner-page="1"]').click()
            page.locator("#workflow-owner-back").click()
            assert page.locator("#workflow-owner-graph").is_hidden()
            assert page.locator("#workflow-stage").is_visible()
            assert page.locator("#workflow-overview").is_visible()
            assert page.evaluate("document.querySelector('#workflow-scroll').scrollTop === ed.workflowOverviewScroll.top")
            node.press("Enter")
            assert page.locator("#workflow-owner-graph").is_hidden()
            node.press(" ")
            assert page.locator("#workflow-owner-graph").is_hidden()
            node.locator('[data-workflow-page="1"]').press("Enter")
            assert page.evaluate("ed.workflowPage") == 1
            assert page.locator("#workflow-owner-graph").is_hidden()
            node.locator("[data-workflow-drilldown]").press("Enter")
            page.wait_for_selector(".kind-render img")
            page.screenshot(path=str(browser_tmp / "owner-iteration.png"), full_page=True)

            original = payload
            page.locator("#workflow-owner-back").click()
            for role, key in (("orchestrator", "orch"), ("research", "research"), ("image", "image"), ("slide", "slide_group_a")):
                payload = {**original, "agent": {"role": role, "label": key},
                           "workflow": {"pages": [], "nodes": [original["workflow"]["nodes"][0]]}}
                page.evaluate("""([role,key])=>{
                  ed.workflowSelectedNodeId='';
                  $('#workflow-stage').innerHTML=workflowNodeMarkup({id:key,taskId:key,agentKey:key,
                    role,pages:[],title:key,status:'running',kicker:role,detail:'实际执行记录',x:40,y:40});
                }""", [role, key])
                card = page.locator(".workflow-node")
                card.locator("strong").click()
                page.wait_for_function("!ed.workflowTraceLoading")
                assert "trace-selected" in card.get_attribute("class")
                assert page.locator("#workflow-owner-graph").is_hidden()
                before = len(requests)
                card.locator("[data-workflow-drilldown]").click()
                page.wait_for_selector(".workflow-owner-node")
                assert any(f"/agent-trace/{key}?" in url and "include_workflow=true" in url for url in requests[before:])
                assert role.lower() in page.locator("#workflow-owner-title").inner_text().lower()
                page.locator("#workflow-owner-back").click()

            page.evaluate("""ed.muralRuntime={tasks:[
                {task_id:'upstream',role:'image',status:'failed'},
                {task_id:'pending-slide',role:'slide',pages:[3],status:'skipped',dependencies:['upstream']}]};
              $('#workflow-stage').innerHTML=workflowTaskGraph(ed.muralRuntime.tasks).nodes.map(n=>workflowNodeMarkup({...n,x:40,y:n.role==='image'?40:340})).join('');""")
            pending = page.locator('[data-workflow-task-id="pending-slide"]')
            before = len([url for url in requests if "/agent-trace/" in url])
            pending.locator("strong").click()
            assert "trace-selected" in pending.get_attribute("class")
            assert page.locator(".workflow-node.trace-selected").count() == 1
            assert page.locator("#workflow-owner-graph").is_hidden()
            pending.locator("[data-workflow-drilldown]").click()
            assert "依赖失败" in page.locator(".workflow-owner-empty").inner_text()
            assert page.locator("#workflow-stage").is_hidden()
            assert len([url for url in requests if "/agent-trace/" in url]) == before
            assert page.evaluate("ed.workflowTraceAgent") == ""
            page.screenshot(path=str(browser_tmp / "unstarted-task.png"), full_page=True)
            payload = original
            page.evaluate("""ed.muralRuntime.tasks[1].agent_key='slide_group_a';
              renderWorkflowOwnerGraph();""")
            page.wait_for_selector(".kind-render img")
            assert page.evaluate("ed.workflowOwnerAgent") == "slide_group_a"
            page.locator("#workflow-owner-back").click()
            assert page.evaluate("ed.workflowOwnerTask") == ""
            page.evaluate("openWorkflowOwnerGraph('slide_group_a')")
            page.wait_for_selector(".kind-render img")

            payload = {key: value for key, value in original.items() if key != "workflow"}
            page.evaluate("loadWorkflowAgentTrace({force:true})")
            assert "服务尚未更新" in page.locator(".workflow-owner-empty").inner_text()
            assert "该 Agent 尚无可展示的执行事件" not in page.locator("#workflow-owner-nodes").inner_text()
            assert "正在读取" not in page.locator("#workflow-owner-note").inner_text()
            page.screenshot(path=str(browser_tmp / "backend-not-updated.png"), full_page=True)
            page.locator("[data-owner-retry]").click()
            page.wait_for_function("!ed.workflowTraceLoading")
            assert page.locator(".state-unsupported").count() == 1

            payload = {**original, "workflow": {}}
            page.evaluate("loadWorkflowAgentTrace({force:true})")
            assert "数据格式异常" in page.locator(".workflow-owner-empty").inner_text()
            payload = {**original, "workflow": {"pages": [1, 2], "nodes": []},
                       "empty_state": {"kind": "not_started", "message": "尚未启动，等待上游任务。"}}
            page.evaluate("loadWorkflowAgentTrace({force:true})")
            assert page.locator(".state-not_started").count() == 1

            response_status = 503
            page.evaluate("ed.workflowTracePayload=null; loadWorkflowAgentTrace({force:true})")
            assert "读取失败" in page.locator(".workflow-owner-empty").inner_text()
            response_status = 200
            payload = original
            page.evaluate("loadWorkflowAgentTrace({force:true})")
            response_status = 503
            page.evaluate("loadWorkflowAgentTrace({force:true})")
            assert page.locator(".workflow-owner-refresh-error").count() == 1
            assert page.locator(".workflow-owner-frame img").count() == 1
            response_status = 200
            page.evaluate("loadWorkflowAgentTrace({force:true})")
            assert page.locator(".workflow-owner-refresh-error").count() == 0
            page.set_viewport_size({"width": 800, "height": 800})
            owner_box = page.locator(".workflow-owner-self-check").bounding_box()
            critic_box = page.locator(".workflow-owner-review").bounding_box()
            assert abs(owner_box["y"] - critic_box["y"]) < 2
            page.set_viewport_size({"width": 620, "height": 800})
            canvas_box = page.locator(".workflow-owner-canvas").bounding_box()
            review_box = page.locator(".workflow-owner-review").bounding_box()
            changes_box = page.locator(".workflow-owner-changes").bounding_box()
            assert review_box["y"] >= canvas_box["y"] + canvas_box["height"]
            assert changes_box["y"] >= review_box["y"] + review_box["height"]
            assert page.locator(".workflow-owner-checks").evaluate("n=>n.scrollWidth<=n.clientWidth")
            assert not errors
        finally:
            browser.close()


def test_owner_page_projection_keeps_versions_feedback_and_shared_events_separate():
    source = (Path(__file__).resolve().parents[1] / "static/app.js").read_text()
    start = source.index("function workflowOwnerPageNodes(")
    function = source[start:source.index("\nfunction ", start + 1)]
    script = function + """
const graph={nodes:[
  {id:'start',kind:'lifecycle',pages:[]},
  {id:'p1-v1',kind:'render',pages:[1],images:[{page:1,sha256:'a',version:1}]},
  {id:'p2-failed',kind:'render',pages:[2],status:'failed'},
  {id:'p2-edit',kind:'edit',pages:[2]},
  {id:'shared-edit',kind:'edit',pages:[1,2]},
  {id:'message',kind:'message',pages:[]},
  {id:'group',kind:'render',pages:[1,2],images:[{page:1,sha256:'b',version:2},{page:2,sha256:'c',version:1}]},
  {id:'critic-p2',kind:'critic',pages:[1,2],render_id:'group',png_sha256:'c'},
  {id:'shared-critic',kind:'critic',pages:[1,2],render_id:'group',png_sha256:'unknown'},
  {id:'unknown-render',kind:'render',pages:[],status:'failed'}
]};
const before=JSON.stringify(graph);
console.log(JSON.stringify({p1:workflowOwnerPageNodes(graph,1),p2:workflowOwnerPageNodes(graph,2),
  shared:workflowOwnerPageNodes(graph,0),all:workflowOwnerPageNodes(graph,null),
  empty:workflowOwnerPageNodes(graph,3),unchanged:JSON.stringify(graph)===before}));
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    projected = json.loads(result.stdout)
    assert [row["id"] for row in projected["p1"]] == ["p1-v1", "group"]
    assert [row["id"] for row in projected["p2"]] == ["p2-failed", "p2-edit", "group", "critic-p2"]
    assert [row["id"] for row in projected["shared"]] == ["start", "shared-edit", "message", "shared-critic", "unknown-render"]
    assert projected["p1"][-1]["images"] == [{"page": 1, "sha256": "b", "version": 2}]
    assert projected["p2"][2]["images"] == [{"page": 2, "sha256": "c", "version": 1}]
    assert projected["empty"] == [] and len(projected["all"]) == 10 and projected["unchanged"]


def test_owner_cards_deduplicate_public_feedback_without_semantic_verdict():
    source = (Path(__file__).resolve().parents[1] / "static/app.js").read_text()
    functions = []
    for name in ("escapeHtml", "workflowOwnerNodeMarkup"):
        start = source.index(f"function {name}(")
        functions.append(source[start:source.index("\nfunction ", start + 1)])
    script = "\n".join(functions) + """
const node={id:'review',kind:'critic',status:'completed',sequence:10,critic_model:'27b',
  detail:'建议核对标题重叠。'};
console.log(JSON.stringify({audit:workflowOwnerNodeMarkup(node),
  failed:workflowOwnerNodeMarkup({...node,status:'failed',detail:'',error:'Provider timeout'}),
  reused:workflowOwnerNodeMarkup({...node,cache_hit:true}),
  short:workflowOwnerNodeMarkup({...node,detail:'同一段反馈',feedback:' 同一段反馈 ',error:'同一段反馈',segment_id:'segment-1',turn:3,png_sha256:'pixel-hash'}),
  long:workflowOwnerNodeMarkup({...node,detail:'甲'.repeat(180)+'尾部证据<script>'}),
  empty:workflowOwnerNodeMarkup({...node,detail:''})}));
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    markup = json.loads(result.stdout)
    assert "建议核对标题重叠。" in markup["audit"] and "反馈已返回" in markup["audit"]
    assert "通过" not in markup["audit"] and "需修订" not in markup["audit"]
    assert "27b" in markup["audit"]
    assert "评审执行未完成" in markup["failed"] and "Provider timeout" in markup["failed"]
    assert "复用反馈 · 无新请求" in markup["reused"]
    assert markup["short"].count("同一段反馈") == 1
    assert "<pre>" not in markup["short"]
    assert "<summary>执行信息</summary>" in markup["short"]
    for value in ("segment-1", "回合 3", "pixel-hash"):
        assert value in markup["short"]
    assert markup["long"].count("甲") == 180
    assert "<pre>尾部证据&lt;script&gt;</pre>" in markup["long"]
    assert "展开剩余内容与执行信息" in markup["long"]
    assert "<pre>" not in markup["empty"]


def test_owner_read_states_do_not_conflate_missing_projection_and_empty_trace():
    source = (Path(__file__).resolve().parents[1] / "static/app.js").read_text()
    start = source.index("function workflowOwnerReadState(")
    function = source[start:source.index("\nfunction ", start + 1)]
    script = "const ed={workflowTraceLoading:true};" + function + """
const cases=[
  [null,null,[]],
  [{content:'existing Trace'},undefined,[]],
  [{load_error:true,content:'failed'},undefined,[]],
  [{workflow:{}},{},[]],
  [{workflow:{nodes:[]},empty_state:{kind:'dependency_failed',message:'upstream failed'}},{nodes:[]},[]],
  [{workflow:{nodes:[{}]}},{nodes:[{}]},[]],
  [{workflow:{nodes:[{}]}},{nodes:[{}]},[{}]],
];
console.log(JSON.stringify(cases.map(args=>workflowOwnerReadState(...args)?.kind||'ready')));
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == ["loading", "unsupported", "error", "error", "dependency_failed", "filtered", "ready"]
