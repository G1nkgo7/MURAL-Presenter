"""内容审计只改变展示，不推断修复，也不混合不同审计轮次。"""
import json

import pytest

from test_startup_observability import _js


def render(events):
    return _js(["escapeHtml", "orchestrationFlatFeed", "orchestrationEntries", "orchestrationEntryContent"],
        "const entries=orchestrationEntries({content:" + json.dumps(events) + "},{agents:{}}).entries;"
        "console.log(JSON.stringify({entries,html:entries.map(e=>orchestrationEntryContent(e)).join('')}));",
        setup="""
const ed={}, canonicalOrchestrationAgentKey=key=>key,
  orchestrationAgentMeta=key=>({key,role:'content',label:'内容审计'}),
  orchestrationEventTime=()=>null, orchestrationAgentGlyph=()=>'', editorPresentationKind=()=> 'static';
""")


def status(kind, seq, payload, segment="audit-1"):
    return dict(event_type="content_qc_" + kind, event_id=f"e-{seq}", seq=seq,
                segment_id=segment, k="status", s="内容审计 · " + json.dumps(payload))


def message(findings, seq=2):
    return dict(event_type="assistant_message", event_id=f"e-{seq}", seq=seq,
                segment_id="independent-provider-call", k="text", s=json.dumps(findings))


FINDINGS = [dict(kind="factual_conflict", severity=level, pages=[page], summary=summary)
            for level, page, summary in [("minor", 6, "来源数字不一致"),
                                         ("advisory", 6, "图示倍数待核验"),
                                         ("advisory", 8, "术语需要更新<script>")]]


def test_summary_and_findings_are_one_card_without_repeated_json():
    events = [status("started", 1, {"reviewer_model": "27b"}), message(FINDINGS),
              status("completed", 3, {"status": "completed", "passed": True, "finding_count": 3,
                                     "elapsed_s": 79.791, "reviewer_model": "27b"})]
    result = render(events + [events[1], events[2]])
    assert len(result["entries"]) == 1
    html = result["html"]
    for value in ("通过，但有提示", "1 项轻微", "2 项建议", "79.8 秒", "P6", "P8", "通过不代表提示已修复"):
        assert value in html
    assert html.count("来源数字不一致") == 1
    assert "finding_count" not in html and "factual_conflict" not in html
    assert "&lt;script&gt;" in html and "<script>" not in html


@pytest.mark.parametrize("kind,summary,findings,expected", [
    ("completed", {"status": "completed", "passed": False, "finding_count": 1},
     [dict(FINDINGS[0], severity="major")], "未通过"),
    ("failed", {"status": "incomplete", "incomplete_reason": "模型超时"}, None, "审计未完成"),
    ("completed", {"status": "completed", "passed": True, "finding_count": 0}, [], "未发现内容问题"),
    ("completed", {"status": "completed", "passed": True, "finding_count": 3}, None, "明细尚未完整获取"),
    ("completed", {}, None, "审计结果待确认"),
])
def test_outcome_uses_recorded_result_not_tool_success(kind, summary, findings, expected):
    events = [status("started", 1, {})]
    if findings is not None:
        events.append(message(findings))
    events.append(status(kind, 3, summary))
    result = render(events)
    assert len(result["entries"]) == 1 and expected in result["html"]
    if findings is None:
        assert "未发现内容问题" not in result["html"]


def test_new_round_and_mismatched_terminal_do_not_reuse_previous_findings():
    events = [status("started", 1, {}), message(FINDINGS),
              status("completed", 3, {"status": "completed", "passed": True, "finding_count": 3}),
              status("started", 4, {}, "audit-2"), message([], 5),
              status("completed", 6, {"status": "completed", "passed": True, "finding_count": 0}, "audit-2")]
    result = render(events)
    assert len(result["entries"]) == 2
    assert result["entries"][0]["findings"] == FINDINGS
    assert result["entries"][1]["findings"] == []
    events[-1]["segment_id"] = "unmatched-audit"
    result = render(events)
    assert len(result["entries"]) == 3
    assert not result["entries"][-1]["findingsReceived"]


def test_malformed_or_partial_model_text_does_not_become_zero_findings():
    malformed = {**message([]), "s": "<script>not an array</script>"}
    result = render([status("started", 1, {}), malformed,
                     status("failed", 3, {"status": "incomplete"})])
    assert "未解析的审计文本" in result["html"]
    assert "&lt;script&gt;" in result["html"] and "<script>" not in result["html"]
    result = render([status("started", 1, {}), {**malformed, "partial": True}])
    assert len(result["entries"]) == 1
    assert not result["entries"][0]["findingsReceived"]
    assert "审计进行中" in result["html"]


def test_chromium_content_audit_card_wraps_without_duplicate_findings(tmp_path, monkeypatch):
    import os
    from pathlib import Path
    from playwright.sync_api import sync_playwright
    from app import engine

    for name in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(name, str(tmp_path))
    events = [status("started", 1, {}), message(FINDINGS), status("completed", 3, {
        "status": "completed", "passed": True, "finding_count": 3,
        "elapsed_s": 79.791, "reviewer_model": "27b"})]
    html = render(events)["html"]
    env = engine.render_env(dict(os.environ))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=env["PPT_SKILL_BROWSER_EXE"],
                                    env=env, headless=True, args=["--no-sandbox"])
        try:
            page = browser.new_page(viewport={"width": 1000, "height": 900})
            page.route("**/*", lambda route: route.abort())
            page.set_content('<article class="orch-entry content-audit role-content">' + html + '</article>')
            page.add_style_tag(path=str(Path(__file__).resolve().parents[1] / "static/app.css"))
            assert page.locator(".content-audit-card").count() == 1
            assert page.locator(".content-audit-finding").count() == 3
            assert "通过，但有提示" in page.locator(".orch-entry-head").inner_text()
            assert page.locator(".content-audit-card").inner_text().count("来源数字不一致") == 1
            page.wait_for_function("getComputedStyle(document.querySelector('.orch-entry')).opacity === '1'")
            page.screenshot(path=str(tmp_path / "content-audit-card.png"), full_page=True)
            for width in (1000, 420):
                page.set_viewport_size({"width": width, "height": 900})
                for theme in ("light", "dark"):
                    page.evaluate("theme=>document.documentElement.dataset.theme=theme", theme)
                    assert page.locator(".content-audit-card").evaluate("n=>n.scrollWidth<=n.clientWidth")
                    assert all(page.locator(".content-audit-finding").evaluate_all("nodes=>nodes.map(n=>n.scrollWidth<=n.clientWidth)"))
        finally:
            browser.close()
