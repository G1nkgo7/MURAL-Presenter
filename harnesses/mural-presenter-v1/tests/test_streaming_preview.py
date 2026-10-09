from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from runtime.agent import AgentRuntime
from runtime.config import ProviderSettings
from runtime.live_trace import LiveEventJournal, PublicResponseStream, read_live_events
from runtime.recovery import ProviderRequestTimeout
from runtime.session import (
    AnthropicTransport, ModelRequest, ProviderChannel, TransportResponse,
    _normalize_anthropic_response, _read_openai_sse, _read_openai_responses_sse,
    _public_delta_sink,
)
from runtime.tools import ToolContext, ToolRuntime
from state.workspace import WorkspaceState

REPOSITORY = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY / "webui" / "studio"))
from app import trace


def settings(**kwargs):
    # 本文件测流式预览路径,前提即流式;生产默认已改非流式,故这里显式开流式。
    # 个别用例(test_nonstreaming_has_only_canonical_messages)显式传 streaming=False 覆盖。
    kwargs.setdefault("streaming", True)
    return ProviderSettings(api_format="anthropic", model="fixture", base_url="https://fixture.invalid", **kwargs)


def response(text="正式回复"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )


class _AsyncClient(SimpleNamespace):
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def run_agent(journal, transport, *, role="orchestrator", config=None, channels=None):
    runtime = AgentRuntime(
        settings=config or settings(),
        channels=channels or (ProviderChannel("fixture-key", "ch-a"),),
        transport=transport, tools=ToolRuntime(), event_sink=journal.record_runtime_event,
    )
    return runtime.run(
        agent_id=role, role=role, system="fixture", tool_context=ToolContext(role, role),
        initial_messages=({"role": "user", "content": "fixture"},),
        max_turns=2, max_tokens=128, thinking=True, effort="medium",
    )


def node_merge(frames):
    script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(process.argv[1], 'utf8');
function extract(name) {
  const start = source.indexOf(`function ${name}(`);
  assert(start >= 0, name);
  const tail = source.slice(start + 1);
  const next = tail.search(/^(?:async )?function /m);
  return source.slice(start, next < 0 ? undefined : start + 1 + next);
}
const context = {};
vm.runInNewContext(extract('evKey') + extract('mergeIncrementalLiveFeed'), context);
let feed = {};
const snapshots = JSON.parse(fs.readFileSync(0, 'utf8')).map(frame => {
  feed = context.mergeIncrementalLiveFeed(feed, frame);
  return JSON.parse(JSON.stringify(feed));
});
process.stdout.write(JSON.stringify(snapshots));
"""
    proc = subprocess.run(
        ["node", "-e", script, str(REPOSITORY / "webui/studio/static/app.js")],
        input=json.dumps(frames), text=True, capture_output=True, check=True, timeout=10,
    )
    return json.loads(proc.stdout)


@pytest.mark.parametrize("role", ["orchestrator", "slide", "research", "image", "material"])
def test_webui_sees_preview_before_provider_finishes_then_replaces_it(tmp_path, role):
    WorkspaceState(tmp_path)
    journal = LiveEventJournal(tmp_path / "_trace/live-events.jsonl")
    preview_ready, release = threading.Event(), threading.Event()
    errors, results = [], []

    class Stream:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def __aiter__(self):
            yield SimpleNamespace(type="content_block_delta", index=0, delta=SimpleNamespace(type="text_delta", text="正在整理内容。"))
            preview_ready.set()
            assert await asyncio.to_thread(release.wait, 10)
            yield SimpleNamespace(type="content_block_delta", index=0, delta=SimpleNamespace(type="text_delta", text="整理完成。"))
            yield SimpleNamespace(type="message_stop")
        async def get_final_message(self): return response("正在整理内容。整理完成。")

    transport = AnthropicTransport(settings())
    transport._client = lambda _channel: _AsyncClient(messages=SimpleNamespace(stream=lambda **kw: Stream()))
    def worker():
        try: results.append(run_agent(journal, transport, role=role))
        except BaseException as exc: errors.append(exc)
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert preview_ready.wait(10)
        during = trace.livefeed("", run_dir=str(tmp_path), deck_status="running")
        assert thread.is_alive()
        partials = [e for es in during["agents"].values() for e in es if e.get("partial")]
        assert len(partials) == 1 and partials[0]["s"] == "正在整理内容。"
        assert partials[0]["role"] == role
        raw_during = read_live_events(journal.path)["events"]
        assert not any(e["event_type"] == "assistant_message" for e in raw_during)
    finally:
        release.set()
        thread.join(10)
    assert not errors and not thread.is_alive()
    after = trace.livefeed("", run_dir=str(tmp_path), live_cursor=during["live_next_cursor"])
    merged = node_merge([during["agents"], after["agents"], after["agents"]])
    visible = [e for es in merged[-1].values() for e in es if e.get("k") == "text"]
    assert len(visible) == 1 and not visible[0].get("partial")
    assert visible[0]["s"] == "正在整理内容。整理完成。"
    assert all("_delta" not in json.dumps(m) for m in results[0].checkpoint.messages)
    rows = read_live_events(journal.path)["events"]
    messages, _ = trace._live_events_messages(rows)
    texts = [b for m in messages for b in m["content"] if b["type"] == "text"]
    assert len(texts) == 1 and not texts[0].get("partial")


def test_preview_snapshots_preserve_split_text_but_do_not_guess_reasoning():
    clock = [0.0]
    events = []
    secret = "opaque-fixture-credential-12345"
    preview = PublicResponseStream(lambda kind, **fields: events.append({"event_type": kind, **fields}), "attempt-test", monotonic=lambda: clock[0])
    text = f"公开消息。api_key={secret} thinking_signature=EXAMPLE 完毕。"
    for char in text:
        clock[0] += 0.3
        preview.add("message", "0", char)
    preview.add("reasoning", "0", "PRIVATE_REASONING")
    preview.finish()
    encoded = json.dumps(events)
    assert "PRIVATE_REASONING" not in encoded
    assert events[-1]["message"] == text
    assert all(text.startswith(e["message"]) for e in events)
    assert len({e["revision"] for e in events}) == len(events)
    events.clear()
    preview = PublicResponseStream(lambda *args, **kw: events.append(kw), "throttle", monotonic=lambda: 0)
    for _ in range(100): preview.add("message", "0", "文字")
    assert len(events) == 1
    preview.finish()
    assert len(events) == 2


def test_anthropic_stream_exposes_only_requested_public_summary():
    observed = []
    deltas = [
        SimpleNamespace(type="thinking_delta", thinking="公开摘要。"),
        SimpleNamespace(type="signature_delta", signature="PRIVATE_SIGNATURE"),
        SimpleNamespace(type="input_json_delta", partial_json='{"content":"PRIVATE_TOOL_BODY"}'),
    ]
    class Stream:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def __aiter__(self):
            for delta in deltas:
                yield SimpleNamespace(type="content_block_delta", index=0, delta=delta)
        async def get_final_message(self): return response()
    transport = AnthropicTransport(settings())
    transport._client = lambda _channel: _AsyncClient(messages=SimpleNamespace(stream=lambda **kw: Stream()))
    request = ModelRequest(model="fixture", system="", messages=(), thinking=True)
    with _public_delta_sink(lambda *args: observed.append(args)):
        transport.complete(ProviderChannel("fixture", "ch-a"), request, total_timeout_s=10)
    assert observed == [("summary", "0", "公开摘要。")]
    observed.clear()
    request = ModelRequest(model="fixture", system="", messages=(), thinking=False)
    with _public_delta_sink(lambda *args: observed.append(args)):
        transport.complete(ProviderChannel("fixture", "ch-a"), request, total_timeout_s=10)
    assert observed == []


@pytest.mark.parametrize("reader,events,expected", [
    (_read_openai_sse, [{"choices": [{"delta": {"content": "可见。", "reasoning_content": "PRIVATE_REASONING"}, "finish_reason": "stop"}]}], [("message", "0", "可见。")]),
    (_read_openai_responses_sse, [{"type": "response.output_text.delta", "delta": "可见。"}, {"type": "response.reasoning_summary_text.delta", "delta": "公开摘要。"}, {"type": "response.reasoning.delta", "delta": "PRIVATE_REASONING"}], [("message", "0", "可见。"), ("summary", "(0, 0)", "公开摘要。")]),
])
def test_other_transports_keep_private_reasoning_out_of_previews(reader, events, expected):
    observed = []
    lines = ["data: " + json.dumps(e) for e in events] + ["data: [DONE]"]
    async def response_lines():
        for line in lines:
            yield line
    asyncio.run(reader(response_lines(), started=0, total_timeout_s=100, monotonic=lambda: 1,
                       on_public_delta=lambda *args: observed.append(args)))
    assert observed == expected


def test_nonstreaming_has_only_canonical_messages(tmp_path):
    config = settings(streaming=False)
    WorkspaceState(tmp_path)
    journal = LiveEventJournal(tmp_path / "_trace/live-events.jsonl")
    transport = AnthropicTransport(config)
    async def create(**kwargs):
        return response()
    transport._client = lambda _channel: _AsyncClient(messages=SimpleNamespace(create=create))
    run_agent(journal, transport, config=config)
    rows = read_live_events(journal.path)["events"]
    assert not any(e["event_type"].endswith("_delta") for e in rows)
    assert any(e["event_type"] == "assistant_message" for e in rows)


def test_failover_and_resume_keep_preview_identity_and_order(tmp_path):
    WorkspaceState(tmp_path)
    journal = LiveEventJournal(tmp_path / "_trace/live-events.jsonl")
    class Transport:
        count = 0
        def complete(self, channel, request, **kwargs):
            self.count += 1
            from runtime.session import _emit_public_delta
            _emit_public_delta("message", "0", "中断前的消息。" if self.count == 1 else "恢复后的消息。")
            if self.count == 1: raise ProviderRequestTimeout("stream_idle", 1)
            return TransportResponse(_normalize_anthropic_response(response("恢复后的正式消息。")), 0.1)
    run_agent(journal, Transport(), config=settings(max_attempts=2), channels=(ProviderChannel("fixture-a", "ch-a"), ProviderChannel("fixture-b", "ch-b")))
    rows = read_live_events(journal.path)["events"]
    assert any(e["event_type"] == "provider_failover" for e in rows)
    projected = trace.live_events(str(tmp_path))
    feed = node_merge([projected["agents"], projected["agents"]])[-1]
    messages = [e for es in feed.values() for e in es if e["k"] == "text"]
    assert len(messages) == 2
    assert messages[0]["partial"] and messages[0]["phase"] == "interrupted"
    assert not messages[1].get("partial")
    reopened = LiveEventJournal(journal.path)
    preview = next(e for e in rows if e["event_type"].endswith("_delta"))
    assert reopened.append(**{k:preview[k] for k in ("event_type", "agent_id", "role", "turn", "segment_id", "payload", "event_id")}) is None
    cursor = reopened.sequence
    reopened.record_runtime_event({"event_type": "agent_started", "agent_id": "slide", "role": "slide", "segment_id": "resumed", "turn": 0})
    reopened.record_runtime_event({"event_type": "assistant_message_delta", "agent_id": "slide", "role": "slide", "segment_id": "resumed", "turn": 1, "attempt_id": "resume-attempt", "revision": 1, "message": "恢复预览。"})
    reopened.close_interrupted_events()
    after = read_live_events(journal.path)["events"]
    assert [e["sequence"] for e in after] == list(range(1, len(after) + 1))
    assert len({e["event_id"] for e in after}) == len(after)
    update = trace.live_events(str(tmp_path), cursor=cursor)
    merged = node_merge([projected["agents"], update["agents"]])[-1]
    slide = [e for es in merged.values() for e in es if e.get("role") == "slide" and e.get("partial")]
    assert len(slide) == 1 and slide[0]["phase"] == "interrupted"
    messages, _ = trace._live_events_messages(after)
    resumed = [b for m in messages for b in m["content"] if b.get("text") == "恢复预览。"]
    assert len(resumed) == 1 and resumed[0]["interrupted"]


def test_concurrent_owners_do_not_share_preview_sinks(tmp_path):
    WorkspaceState(tmp_path)
    journal = LiveEventJournal(tmp_path / "_trace/live-events.jsonl")
    barrier = threading.Barrier(2, timeout=10)
    errors = []
    class Transport:
        def complete(self, channel, request, **kwargs):
            from runtime.session import _emit_public_delta
            label = threading.current_thread().name
            barrier.wait()
            _emit_public_delta("message", "0", label + " 公开预览。")
            barrier.wait()
            return TransportResponse(_normalize_anthropic_response(response(label)), 0.1)
    transport = Transport()
    def worker(role):
        try: run_agent(journal, transport, role=role)
        except BaseException as exc: errors.append(exc)
    threads = [threading.Thread(name=role, target=worker, args=(role,)) for role in ("orchestrator", "slide")]
    for thread in threads: thread.start()
    for thread in threads: thread.join(15)
    assert not errors and not any(t.is_alive() for t in threads)
    previews = [e for e in read_live_events(journal.path)["events"] if e["event_type"] == "assistant_message_delta"]
    assert len(previews) == 2
    assert all(e["payload"]["message"].startswith(e["agent_id"] + " ") for e in previews)


def test_previews_do_not_enter_compatibility_logs(tmp_path):
    from runtime.skill_runtime import SkillRuntime
    from runtime.live_trace import iter_replay_events
    runtime = SkillRuntime.__new__(SkillRuntime)
    WorkspaceState(tmp_path)
    journal = LiveEventJournal(tmp_path / "_trace/live-events.jsonl")
    common = {"agent_id": "orch", "role": "orchestrator", "segment_id": "segment", "turn": 1, "attempt_id": "attempt"}
    for kind in ("assistant_message_delta", "assistant_message"):
        runtime._record_runtime_event(SimpleNamespace(live_trace=journal), {
            **common, "event_type": kind, "revision": 1, "message": "公开消息。",
        })
    assert [e["event_type"] for e in iter_replay_events(journal.path, "agent")] == ["assistant_message"]
    assert not (tmp_path / "_trace/agent-events.jsonl").exists()
    assert journal.sequence == 2


def test_browser_updates_existing_role_cards_and_replaces_preview(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    from app.engine import render_env
    browser_env = render_env(os.environ.copy())
    executable = browser_env.get("PPT_SKILL_BROWSER_EXE")
    if not executable:
        pytest.skip("deployed Chromium is not available")
    browser_env["TMPDIR"] = str(tmp_path)
    source = (REPOSITORY / "webui/studio/static/app.js").read_text()
    names = (
        "escapeHtml", "evKey", "mergeIncrementalLiveFeed", "livePreviewLabel", "liveAgentText",
        "cleanAgentText", "agentMarkdownHtml", "canonicalOrchestrationAgentKey", "agentPages",
        "orchestrationAgentMeta", "orchestrationAgentGlyph", "orchestrationFlatFeed", "agentTimingFor",
        "orchestrationEntries", "orchestrationEntryContent", "reconcileOrchestrationEntries",
        "pageAgentDisplayName", "pageAgentProgressItems", "reconcileProgressHistory", "agentIdentityGlyph",
    )
    functions = []
    for name in names:
        start = source.index(f"function {name}(")
        end = re.search(r"^(?:async )?function ", source[start + 1:], re.M)
        functions.append(source[start:start + 1 + end.start()] if end else source[start:])
    roles = re.search(r"const ORCHESTRATION_ROLE_LABELS = \{.*?^\};", source, re.M | re.S).group()
    setup = """
const ed = {status:'running', kind:'static', feed:{}, pageAgents:{'1':'slide_01'}, rendered:new Set()};
const $$ = (s, root=document) => [...root.querySelectorAll(s)];
const pad2 = n => String(n).padStart(2, '0');
const editorPresentationKind = () => 'static';
const orchestrationEventTime = () => null;
window.renderFrame = frame => {
  ed.feed = mergeIncrementalLiveFeed(ed.feed, frame);
  const {entries} = orchestrationEntries(ed.feed, {agents:{}});
  reconcileOrchestrationEntries(document.querySelector('#roles'), entries);
};
window.renderPage = events => reconcileProgressHistory(document.querySelector('#page'), pageAgentProgressItems(events, 'slide_01', 1));
"""
    WorkspaceState(tmp_path)
    journal = LiveEventJournal(tmp_path / "_trace/live-events.jsonl")
    def emit(agent, role, kind, text, revision=1):
        journal.record_runtime_event({
            "event_type": kind, "agent_id": agent, "role": role, "segment_id": agent + "-segment",
            "turn": 1, "attempt_id": agent + "-attempt", "revision": revision,
            "summary" if "summary" in kind else "message": text,
        })
    for agent, role, text in (
        ("orchestrator", "orchestrator", "Let me check the sources. 正在整理演示结构。"),
        ("material", "material", "正在整理附件内容。"),
        ("research", "research", "正在核对检索来源。"),
        ("image", "image", "正在整理配图要求。"),
        ("slide_01", "slide", "正在编排第一页。"),
    ):
        emit(agent, role, "assistant_message_delta", text)
    emit("orchestrator", "orchestrator", "assistant_analysis_summary_delta", "公开摘要：先核对资料，再组织页面。")
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=executable, env=browser_env, headless=True, args=["--no-sandbox"])
        try:
            page = browser.new_page(viewport={"width": 1120, "height": 1000})
            page.set_content('<main style="margin:32px auto;max-width:880px"><h2>MURAL v1 流式展示</h2><p>脚本化模拟事件 · 非真实模型 canary</p><section class="is-orchestration"><div id="roles" class="orchestration-entry-list"></div></section><h3>页面角色消息</h3><div id="page"></div></main>')
            page.add_style_tag(path=str(REPOSITORY / "webui/studio/static/app.css"))
            page.add_script_tag(content=roles + "\n" + "\n".join(functions) + setup)
            frame = trace.live_events(str(tmp_path))
            page.evaluate("renderFrame", frame["agents"])
            assert page.locator(".stream-preview").count() == 6
            assert "Let me check the sources." in page.locator("#roles").inner_text()
            assert "生成中 · 非最终消息" in page.locator("#roles").inner_text()
            page.evaluate("window.firstPreview = document.querySelector('.stream-preview.role-orch')")
            slide = next(e for es in frame["agents"].values() for e in es if e["role"] == "slide")
            page.evaluate("renderPage", [slide])
            page.screenshot(path=str(tmp_path / "streaming-preview.png"), full_page=True, animations="disabled")
            emit("orchestrator", "orchestrator", "assistant_message_delta", "Let me check the sources. 正在整理演示结构。资料已核对。", revision=2)
            update = trace.live_events(str(tmp_path), cursor=frame["next_cursor"])
            page.evaluate("renderFrame", update["agents"])
            assert page.evaluate("firstPreview === document.querySelector('.stream-preview.role-orch')")
            assert "资料已核对。" in page.locator(".stream-preview.role-orch").first.inner_text()
            emit("orchestrator", "orchestrator", "assistant_message", "Let me check the sources. 正式回复：资料核对完成。")
            emit("orchestrator", "orchestrator", "assistant_analysis_summary", "公开摘要：资料已核对，页面结构已确定。")
            update = trace.live_events(str(tmp_path), cursor=update["next_cursor"])
            page.evaluate("renderFrame", update["agents"])
            page.evaluate("renderFrame", update["agents"])
            assert page.locator(".stream-preview.role-orch").count() == 0
            assert page.locator(".message.role-orch").count() == 1
            assert "Let me check the sources." in page.locator(".message.role-orch").inner_text()
            assert page.locator(".analysis.role-orch").count() == 1
            page.evaluate("renderPage", [{**slide, "partial": False, "stream_id": "", "s": "第一页正式消息。"}])
            assert page.locator("#page .agent-message").count() == 1
            assert "非最终消息" not in page.locator("#page").inner_text()
            page.evaluate("ed.status = 'canceled'; renderFrame({})")
            assert "已中断 · 非最终消息" in page.locator(".stream-preview").first.inner_text()
            page.screenshot(path=str(tmp_path / "streaming-final.png"), full_page=True, animations="disabled")
        finally:
            browser.close()
