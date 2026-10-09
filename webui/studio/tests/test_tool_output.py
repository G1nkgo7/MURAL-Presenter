import hashlib
import json
import asyncio
from types import SimpleNamespace
from urllib.parse import urlsplit

from app import main, trace
from workspace_fixture import write_workspace_manifest


def request(path):
    async def invoke():
        parsed = urlsplit(path)
        messages = []
        finished = asyncio.Event()
        received = False
        async def receive():
            nonlocal received
            if not received:
                received = True
                return {"type":"http.request", "body":b"", "more_body":False}
            await finished.wait()
            return {"type":"http.disconnect"}
        async def send(message):
            messages.append(message)
            if message["type"] == "http.response.body" and not message.get("more_body"):
                finished.set()
        await asyncio.wait_for(main.app({"type":"http", "asgi":{"version":"3.0"}, "http_version":"1.1", "method":"GET",
                        "path":parsed.path, "raw_path":parsed.path.encode(), "query_string":parsed.query.encode(),
                        "scheme":"http", "headers":[], "server":("test",80), "client":("test",1), "root_path":""}, receive, send), timeout=5)
        status = next(message["status"] for message in messages if message["type"] == "http.response.start")
        text = b"".join(message.get("body",b"") for message in messages).decode()
        return SimpleNamespace(status_code=status,text=text,json=lambda:json.loads(text))
    return asyncio.run(invoke())


def test_tool_output_api_requires_deck_ownership_and_preserves_partial_tail(tmp_path, monkeypatch):
    write_workspace_manifest(tmp_path)
    trace_dir = tmp_path / "_trace"
    output_dir = trace_dir / "tool-output"
    output_dir.mkdir(parents=True)
    use = {"schema":"mural.live-event.v1", "sequence":1, "event_id":"tool:known:tool_use", "event_type":"tool_use",
           "agent_id":"image-owner", "role":"image", "turn":1, "segment_id":"image-segment", "payload":{"name":"terminal","tool_use_id":"known","task_id":"task-a","attempt":1}}
    (trace_dir / "live-events.jsonl").write_text(json.dumps(use) + "\n")
    log = output_dir / (hashlib.sha256(b"known").hexdigest() + ".jsonl")
    first = json.dumps({"stream":"stdout","text":"HTTP 403"}) + "\n"
    log.write_text(first + '{"stream":')
    monkeypatch.setattr(main, "_own_deck", lambda con, deck_id, uid: {"run_dir":str(tmp_path)} if deck_id == 1 and uid == 7 else None)
    previous = main.app.dependency_overrides.copy()
    try:
        main.app.dependency_overrides[main.get_db] = lambda: None
        main.app.dependency_overrides[main.current_user] = lambda: None
        assert request("/api/decks/1/tool-output/known").status_code == 401
        main.app.dependency_overrides[main.current_user] = lambda: {"id":7}
        assert request("/api/decks/2/tool-output/known").status_code == 404
        assert request("/api/decks/1/tool-output/unknown").status_code == 404
        assert request("/api/decks/1/tool-output/known?cursor=1").status_code == 400
        response = request("/api/decks/1/tool-output/known")
        assert response.status_code == 200
        payload = response.json()
        assert payload["rows"] == [{"stream":"stdout","text":"HTTP 403"}]
        assert payload["incomplete_tail"] and not payload["has_more"]
        assert payload["next_cursor"] == len(first.encode())
        hidden = tmp_path / "private.txt"
        hidden.write_text('private-fixture')
        log.rename(log.with_suffix(".saved"))
        log.symlink_to(hidden)
        response = request("/api/decks/1/tool-output/known")
        assert response.status_code == 400 and "private-fixture" not in response.text
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(previous)
