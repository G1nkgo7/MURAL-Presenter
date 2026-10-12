"""轨迹增量读取、工具详情按需访问及缓存失效的回归。"""
import json
from pathlib import Path
import re
import subprocess

from app import main, trace
from test_delivery_download import download_case, request
from test_startup_observability import _journal


def append(path, sequence, kind="assistant_message", payload=None):
    row = {"schema": "mural.live-event.v1", "sequence": sequence,
           "event_id": f"event-{sequence}", "event_type": kind,
           "agent_id": "orchestrator", "role": "orchestrator",
           "timestamp": "2026-10-10T00:00:00Z", "segment_id": "first",
           "payload": payload or {"message": "继续制作页面"}}
    with path.open("ab") as output:
        output.write(json.dumps(row).encode() + b"\n")


def test_warm_cursor_parses_only_appended_rows_and_preserves_paging(tmp_path, monkeypatch):
    path = _journal(tmp_path, [{"event_type": "agent_started"},
        {"event_type": "assistant_message", "payload": {"message": "第一条"}}])
    first = trace.live_events(str(tmp_path), limit=1, compact=True)
    assert first["next_cursor"] == 1 and first["has_more"]
    loads, original = [], trace.json.loads
    def tracked(value, *args, **kwargs):
        if isinstance(value, bytes):
            loads.append(value)
        return original(value, *args, **kwargs)
    monkeypatch.setattr(trace.json, "loads", tracked)
    second = trace.live_events(str(tmp_path), cursor=1, compact=True)
    assert [event["seq"] for event in second["events"]] == [2]
    assert not second["has_more"] and not loads
    append(path, 3)
    third = trace.live_events(str(tmp_path), cursor=2, compact=True)
    assert [event["seq"] for event in third["events"]] == [3]
    assert len(loads) == 1
    loads.clear()
    assert trace.live_events(str(tmp_path), cursor=3, compact=True)["events"] == []
    assert not loads


def test_index_handles_partial_tail_duplicates_rotation_and_rewrite(tmp_path):
    path = _journal(tmp_path, [{"event_type": "agent_started"}])
    trace.live_events(str(tmp_path), compact=True)
    append(path, 2)
    append(path, 2)
    with path.open("ab") as output:
        output.write(b'{"schema":')
    result = trace.live_events(str(tmp_path), cursor=1, compact=True)
    assert [event["seq"] for event in result["events"]] == [2]
    with path.open("ab") as output:
        output.write(b'"invalid"}\n')
    append(path, 3)
    assert trace.live_events(str(tmp_path), cursor=2, compact=True)["next_cursor"] == 3
    replacement = path.with_suffix(".replacement")
    replacement.write_text("")
    append(replacement, 1, payload={"message": "替换后的新轨迹"})
    replacement.replace(path)
    result = trace.live_events(str(tmp_path), cursor=3, compact=True)
    assert result["cursor_reset"] and result["events"][0]["s"] == "替换后的新轨迹"
    path.write_text("")
    append(path, 1, payload={"message": "原路径重新写入" * 100})
    result = trace.live_events(str(tmp_path), compact=True)
    assert result["events"][0]["s"].startswith("原路径重新写入")
    assert len(result["events"]) == 1


def test_compact_feed_retains_receipts_and_fetches_original_tool_details(tmp_path):
    content = "<html>完整页面内容</html>" * 5000
    _journal(tmp_path, [
        {"event_type": "tool_use", "payload": {"name": "write_file", "tool_use_id": "write",
            "arguments": {"path": "slides/slide_01.html", "content": content}}},
        {"event_type": "tool_result", "payload": {"name": "read_file", "tool_use_id": "read",
            "ok": True, "result": json.dumps({"content": content, "mode": "line_range",
                "line_count": 10, "returned_line_count": 10, "next_offset": None})}},
    ])
    feed = trace.live_events(str(tmp_path), compact=True)
    assert content not in json.dumps(feed)
    assert feed["events"][0]["args"] == {"path": "slides/slide_01.html"}
    result = json.loads(feed["events"][1]["result"])
    assert result["mode"] == "line_range" and result["next_offset"] is None
    assert all(event["details_available"] for event in feed["events"])
    assert trace.live_event_detail(str(tmp_path), 1)["args"]["content"] == content
    assert json.loads(trace.live_event_detail(str(tmp_path), 2)["result"])["content"] == content
    assert trace.live_event_detail(str(tmp_path), 99) is None
    assert trace.live_events(str(tmp_path))["events"][0]["args"]["content"] == content


def test_detail_endpoint_keeps_owner_authorization_and_missing_event_status(download_case, monkeypatch):
    path = download_case["root"] / "_trace/live-events.jsonl"
    append(path, 1, "tool_use", {"name": "write_file", "arguments": {"content": "原始内容"}})
    status, _, body = request("/api/decks/1/live-events/1")
    assert status == 200 and json.loads(body)["args"]["content"] == "原始内容"
    assert request("/api/decks/1/live-events/99")[0] == 404
    main.app.dependency_overrides[main.current_user] = lambda: {"id": 8}
    assert request("/api/decks/1/live-events/1")[0] == 404
    main.app.dependency_overrides[main.current_user] = lambda: None
    assert request("/api/decks/1/live-events/1")[0] == 401


def test_ui_font_shards_cover_source_and_preserve_weight_axis():
    from fontTools.ttLib import TTFont
    root = Path(__file__).resolve().parents[3]
    fonts = root / "webui/studio/static/fonts"
    manifest = json.loads((fonts / "ui-cjk.json").read_text())
    with TTFont(root / manifest["source"]) as source:
        expected = set(source.getBestCmap())
    coverage = set()
    for part in manifest["parts"]:
        with TTFont(fonts / part["file"]) as font:
            coverage.update(font.getBestCmap())
            axis, = font["fvar"].axes
            assert (axis.axisTag, axis.minValue, axis.maxValue) == ("wght", 100, 900)
            assert "Open Font License" in font["name"].getDebugName(13)
    assert coverage == expected and len(coverage) == manifest["characters"]
    assert manifest["parts"][0]["bytes"] < 500_000


def test_livefeed_does_not_overlap_or_apply_previous_deck_response():
    source = (Path(__file__).resolve().parents[1] / "static/app.js").read_text()
    start = re.search(r"^async function fetchLive\(", source, re.M).start()
    end = re.search(r"^(?:async )?function ", source[start + 1:], re.M)
    function = source[start:start + 1 + end.start()]
    script = """
const ed={id:27,kind:'static',sel:1,feed:{},liveEventCursor:0,workspaceView:'ppt'};
const trajectoryMode=false, pending=[], calls=[], rendered=[], history=[];
const jget=url=>{calls.push(url);return new Promise(resolve=>pending.push(resolve))};
const mergeIncrementalLiveFeed=(old, next)=>next;
const renderLive=()=>rendered.push(ed.id), renderWorkflowCanvas=()=>{};
const loadPageHistory=n=>history.push(n);
""" + function + """
(async()=>{
  const first=fetchLive();
  await fetchLive();
  const initialCalls=calls.length;
  ed.id=31;ed.liveFeedRequest=null;
  const second=fetchLive(), currentRequest=ed.liveFeedRequest;
  pending[0]({agents:{old:[]}});await first;
  const staleKeptCurrent=ed.liveFeedRequest===currentRequest && !ed.liveFeedLoaded;
  pending[1]({live_event_schema:'mural.live-event.v1',agents:{current:[]},live_next_cursor:500});
  await second;
  console.log(JSON.stringify({initialCalls,totalCalls:calls.length,staleKeptCurrent,
    rendered,history,cursor:ed.liveEventCursor,feed:ed.feed,loaded:ed.liveFeedLoaded}));
})().catch(error=>{console.error(error);process.exit(1)});
"""
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    state = json.loads(result.stdout)
    assert state == {"initialCalls": 1, "totalCalls": 2, "staleKeptCurrent": True,
                     "rendered": [31], "history": [1], "cursor": 500,
                     "feed": {"current": []}, "loaded": True}
