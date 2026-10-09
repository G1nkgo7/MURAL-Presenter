"""后台解析状态与任务 DAG 均只投影已有运行证据。"""
import json
from pathlib import Path

import pytest

from app import engine, trace
from app.workspace_layout import WorkspaceLayout
from test_startup_observability import _journal, _js, _js_source
from workspace_fixture import write_workspace_manifest


@pytest.mark.parametrize("status", ["queued", "processing", "completed", "partial", "failed"])
def test_attachment_manifest_reports_exact_source_and_status_without_writes(tmp_path, status):
    write_workspace_manifest(tmp_path)
    (tmp_path / "_trace").mkdir()
    receipt = {"source_sha256": "a" * 64, "status": status, "cached": True,
               "unresolved_items": [{"page": 2}], "text": "private document text"}
    record = {"attachment_id": "att-1", "name": "report.pdf", "sha256": "a" * 64,
              "parse_status": status, "parse": {"current": "result-1", "results": {
                  "result-1": {"receipt": receipt}}}}
    manifest = tmp_path / "_trace/attachment-manifest.json"
    manifest.write_text(json.dumps({"records": [record], "active_attachment_ids": ["att-1"]}))
    before = manifest.read_bytes()
    result = trace.snapshot(str(tmp_path))["attachment_parse"]
    assert result == [{"attachment_id": "att-1", "source_sha256": "a" * 64, "name": "report.pdf",
                       "status": status, "cached": True, "unresolved_count": 1}]
    assert "private document text" not in json.dumps(result)
    assert manifest.read_bytes() == before
    record["sha256"] = "b" * 64
    manifest.write_text(json.dumps({"records": [record]}))
    result = trace._attachment_parse_payload(str(tmp_path))
    assert result[0]["source_sha256"] == "b" * 64
    assert "cached" not in result[0] and "unresolved_count" not in result[0]


def test_parse_journal_exposes_runtime_status_not_model_message(tmp_path):
    path = _journal(tmp_path, [{"event_type": name, "agent_id": "attachment-parser", "role": "harness",
        "payload": {"attachment_id": "att-1", "source_sha256": "a" * 64, "status": status,
                    "cached": name.endswith("finished"), "unresolved_count": 2, "text": "not public"}}
        for name, status in [("attachment_parse_queued", "queued"),
                             ("attachment_parse_started", "processing"),
                             ("attachment_parse_finished", "partial")]])
    before = path.read_bytes()
    payload = trace.live_events(str(tmp_path))
    assert [row["status"] for row in payload["events"]] == ["queued", "processing", "partial"]
    assert all(row["k"] == "status" and row["role"] == "harness" for row in payload["events"])
    assert payload["agent_timings"] == {} and payload["agent_statuses"] == {}
    assert "not public" not in json.dumps(payload)
    assert path.read_bytes() == before


def test_frontend_parse_identity_prevents_stale_same_path_status():
    result = _js(["outlineBriefData"], """
console.log(JSON.stringify(outlineBriefData().attachments.map(row => row.parse)));
""", setup="""
const ed = {attachments: [{name:'report.pdf',stored_name:'report.pdf',source_sha256:'new'},
                         {name:'renamed.pdf',stored_name:'renamed.pdf',source_sha256:'old'}],
  attachmentParse:[{name:'report.pdf',source_sha256:'old',status:'completed'}]};
const deckAttachmentPreviewUrls = () => ({});
""")
    assert result[0] is None and result[1]["status"] == "completed"


def test_parse_labels_and_notifications_do_not_claim_semantic_verification():
    result = _js(["attachmentParseLabel", "runtimeEventState"], """
console.log(JSON.stringify(['queued','processing','completed','partial','failed'].map(status =>
  runtimeEventState({event_type:'attachment_parse_finished',attachment_id:'att-1',s:'附件状态',status,cached:true,unresolved_count:2}))));
""")
    assert all("不代表 Research 已阅读或语义核验" in detail for _, detail in result)
    assert "解析完成" in result[2][1] and "2 项未解决" in result[2][1]


def test_parse_timeline_entry_is_visible_as_runtime_not_agent_message():
    result = _js(["attachmentParseLabel", "runtimeEventState", "orchestrationEntries", "orchestrationEntryContent"], """
const entry = orchestrationEntries({harness: [{agent:'harness',k:'status',event_type:'attachment_parse_finished',
  event_id:'parse-1',s:'附件部分解析完成',status:'partial',attachment_id:'att-1'}]}, {agents:{}}).entries[0];
console.log(JSON.stringify({type:entry.type,html:orchestrationEntryContent(entry)}));
""", setup="""
const ed = {}, orchestrationFlatFeed = feed => Object.values(feed).flat(),
  canonicalOrchestrationAgentKey = key => key, orchestrationAgentMeta = key => ({key,role:'harness',label:'后台解析'}),
  orchestrationEventTime = () => null, escapeHtml = text => String(text),
  orchestrationAgentGlyph = () => '', editorPresentationKind = () => 'static';
""")
    assert result["type"] == "runtime"
    assert "后台状态 · 非模型消息" in result["html"]
    assert "附件部分解析完成" in result["html"]
    assert "agent-markdown" not in result["html"]


def test_dag_only_uses_explicit_dependencies_and_keeps_image_batches_distinct():
    result = _js(["workflowExecutionStatus", "workflowCurrentActivity", "runtimeEventState", "attachmentParseLabel", "workflowTaskGraph"], """
const tasks = [
 {task_id:'research',role:'research',agent_key:'research',owner_id:'r',status:'running',dependencies:[]},
 {task_id:'image-a',role:'image',agent_key:'image',owner_id:'i',status:'succeeded',dependencies:[],result:{segment_ids:['first']}},
 {task_id:'image-b',role:'image',agent_key:'image',owner_id:'i',status:'failed',dependencies:['image-a']},
 {task_id:'slide-free',role:'slide',status:'running',dependencies:[]},
 {task_id:'slide-dependent',role:'slide',status:'skipped',dependencies:['image-b']},
];
console.log(JSON.stringify(workflowTaskGraph(tasks,{image:{task_id:'image-b',execution_state:'start_failed',status_detail:'本批次未启动'}})));
""")
    assert result["edges"] == [{"from": "task:image-a", "to": "task:image-b"},
                               {"from": "task:image-b", "to": "task:slide-dependent"}]
    nodes = {row["taskId"]: row for row in result["nodes"]}
    assert nodes["image-a"]["segment"] == "first" and nodes["image-a"]["status"] == "complete"
    assert nodes["image-b"]["status"] == "start_failed"
    assert nodes["slide-dependent"]["status"] == "dependency_failed"
    assert "image-b" in nodes["slide-dependent"]["detail"]
    assert not any(edge["to"] == "task:slide-free" for edge in result["edges"])


def test_new_workspace_browses_only_knowledge_path(tmp_path):
    write_workspace_manifest(tmp_path)
    (tmp_path / "knowledge.md").write_text("authorized evidence")
    layout = WorkspaceLayout.load(tmp_path)
    assert "knowledge" in layout.paths
    assert "material" not in layout.paths and "research" not in layout.paths
    group = next(files for key, _, files in layout.group_files() if key == "knowledge")
    assert group == [tmp_path / "knowledge.md"]
    with pytest.raises(KeyError):
        layout.path("research")


def test_parallel_research_drafts_are_discoverable_and_not_deck_wide_completion(tmp_path):
    write_workspace_manifest(tmp_path)
    drafts = tmp_path / "research"
    drafts.mkdir()
    (drafts / "material.md").write_text("Attachment evidence")
    (tmp_path / "knowledge.md").write_text("Adopted evidence only")
    (tmp_path / "_state/owners.json").write_text(json.dumps({"owners": {
        group: {"owner_id": owner, "role": "research", "group_id": group, "status": "running", "pages": []}
        for group, owner in (("material", "r1"), ("public", "r2"))}}))
    (tmp_path / "_state/orchestration.json").write_text(json.dumps({"tasks": {
        group: {"owner_id": owner, "role": "research", "output_path": f"research/{group}.md"}
        for group, owner in (("material", "r1"), ("public", "r2"))}}))
    artifacts = trace.specialist_artifacts(str(tmp_path))
    assert artifacts["agents"]["research_r1"]["output_recorded"]
    assert not artifacts["agents"]["research_r2"]["output_recorded"]
    assert "brief_ready" not in artifacts["agents"]["research_r2"]
    files = next(files for key, _, files in WorkspaceLayout.load(tmp_path).group_files() if key == "knowledge")
    assert set(files) == {tmp_path / "knowledge.md", drafts / "material.md"}


def test_specialist_keys_stay_stable_when_another_owner_starts_or_is_handed_off(tmp_path):
    write_workspace_manifest(tmp_path)
    path = tmp_path / "_state/owners.json"
    first = {"owner_id": "r1", "role": "research", "group_id": "material", "pages": [], "status": "running"}
    path.write_text(json.dumps({"owners": {"material": first}}))
    key = trace._shared_v1_live_owners(str(tmp_path))[0]["r1"]["key"]
    second = {**first, "owner_id": "r2", "group_id": "public"}
    path.write_text(json.dumps({"owners": {"material": first, "public": second}}))
    owners, _ = trace._shared_v1_live_owners(str(tmp_path))
    assert owners["r1"]["key"] == key != owners["r2"]["key"]
    path.write_text(json.dumps({"owners": {"public": second}, "archives": [{**first, "status": "archived"}]}))
    assert trace._shared_v1_live_owners(str(tmp_path))[0]["r1"]["key"] == key
    assert trace._trace_agent_aliases(str(tmp_path))["r1"] == key


def test_image_cards_use_registered_ownership_even_for_identical_pixels(tmp_path):
    from PIL import Image
    write_workspace_manifest(tmp_path)
    assets = tmp_path / "assets"
    assets.mkdir()
    records, owners = {}, {}
    for name in ("cover", "figure"):
        Image.new("RGB", (100, 100), "navy").save(assets / f"{name}.png")
        owners[name] = {"owner_id": name, "group_id": name, "role": "image", "pages": [], "status": "completed"}
        records[name] = {"artifact_id": name, "owner_id": name, "kind": "image", "status": "committed", "path": f"assets/{name}.png"}
    (tmp_path / "_state/owners.json").write_text(json.dumps({"owners": owners}))
    (tmp_path / "_state/artifacts.json").write_text(json.dumps({"artifacts": records}))
    result = trace.specialist_artifacts(str(tmp_path))
    for name in owners:
        images = result["agents"][f"image_{name}"]["images"]
        assert len(images) == 1 and images[0]["path"] == f"assets/{name}.png"


def test_plan_tree_lists_only_existing_page_briefs_in_page_order(tmp_path):
    write_workspace_manifest(tmp_path)
    assert not trace._workflow_artifacts(str(tmp_path), "running")["groups"]
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"working_language": "zh", "presentation_language": "en", "target_page_count": None}))
    before = config.read_bytes()
    (tmp_path / "deck.md").write_text("# 整册方向")
    briefs = tmp_path / "briefs"
    briefs.mkdir()
    for name in ("slide_100.md", "slide_02.md", "slide_01.md", "notes.md", "slide_draft.md"):
        (briefs / name).write_text(f"# {name}")
    (briefs / "slide_03.md").symlink_to(tmp_path / "deck.md")
    def plans():
        return next(group["files"] for group in trace._workflow_artifacts(str(tmp_path), "running")["groups"] if group["id"] == "plan")
    files = plans()
    assert [row["rel"] for row in files] == ["config.json", "deck.md", "briefs/slide_01.md", "briefs/slide_02.md", "briefs/slide_100.md"]
    assert files[0]["kind"] == "data" and all(row["kind"] == "markdown" for row in files[1:])
    assert WorkspaceLayout.load(tmp_path).creation_config()["presentation_language"] == "en"
    assert config.read_bytes() == before
    (briefs / "slide_10.md").write_text("# New page")
    assert [row["rel"] for row in plans()][-2:] == ["briefs/slide_10.md", "briefs/slide_100.md"]
    (briefs / "slide_02.md").unlink()
    assert "briefs/slide_02.md" not in [row["rel"] for row in plans()]
    config.unlink()
    assert "config.json" not in [row["rel"] for row in plans()]
    assert not config.exists()


def test_research_progress_uses_its_own_output_without_inventing_verification():
    result = _js(["workflowArtifactRelsFromEvents", "specialistProgressEntry",
                  "specialistToolCount", "specialistToolStep", "specialistStep", "liveToolTransactions"], """
const events = [{k:'tool',tool:'read_file',hint:'attachments/report.md'},
                {k:'tool',tool:'write_file',hint:'knowledge.md'}];
console.log(JSON.stringify({rels:workflowArtifactRelsFromEvents(events),
  progress:specialistProgressEntry({role:'research',key:'research'}, events,
    {output_recorded:true,output_path:'research/material.md'})}));
""", setup="""
const workflowArtifactFiles = () => [], cleanAgentText = text => text || '';
""")
    assert result["rels"] == ["knowledge.md"]
    steps = result["progress"]["steps"]
    assert steps[0]["state"] == "unknown" and "当前已载入记录" in steps[0]["detail"]
    assert steps[1]["detail"] == "1 次发起记录 · 0 次成功回执 · 1 次未见结束回执"
    assert steps[2]["detail"] == "research/material.md 已落盘；不代表研究完成"
    assert not result["progress"]["completed"]
    assert "交叉核验" not in json.dumps(result, ensure_ascii=False)


def test_material_overview_is_hidden_until_historical_events_exist():
    source = _js_source(["renderOrchestrationProgress"])
    assert '<span class="material" hidden><b data-material-count>' in source
    counters = source[source.index("  const keys = Object.keys(feed || {});"):source.index('  const list = body.querySelector(".orchestration-entry-list");')]
    result = _js([], """
const rows = [{}, {material:[]}, {material:[{k:'status',event_type:'agent_started'}]}, {}].map(feed => {
  const nodes = {}, body = {querySelector: key => nodes[key] ||= {span:{},closest(){return this.span}}};
""" + counters + """
  const node=nodes['[data-material-count]'];
  return {count:node.textContent,hidden:node.span.hidden};
});
console.log(JSON.stringify(rows));
""", setup="const orchestrationAgentMeta = key => ({role:key});")
    assert result == [{"count": 0, "hidden": True}, {"count": 0, "hidden": True},
                      {"count": 1, "hidden": False}, {"count": 0, "hidden": True}]


def test_only_current_v1_registration_is_public_and_does_not_require_material():
    assert set(engine.SKILLS) == {"mural-presenter-v1"}
    assert set(engine.PIPELINES) == {"mural-presenter-v1-harness"}
    required = engine.SKILLS["mural-presenter-v1"]["required_files"]
    assert "roles/material.md" not in required
    assert {"roles/research.md", "roles/image.md", "roles/slide.md"} <= set(required)
