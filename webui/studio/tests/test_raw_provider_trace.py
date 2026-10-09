"""API 可观测内容分流与受鉴权的原件读取，不依赖远程模型。"""
import json

import pytest

from app import main, trace
from workspace_fixture import write_workspace_manifest
from test_delivery_download import download_case, request
from test_mural_profiles import _append_live_event


def test_trace_sources_are_distinct_and_missing_reasoning_is_explicit(tmp_path):
    write_workspace_manifest(tmp_path)
    (tmp_path / '_trace').mkdir()
    _append_live_event(tmp_path, 1, 'agent_context', payload={
        'system': 'Actual system prompt', 'messages': [{'role': 'user', 'content': 'Initial input'}],
        'tools': [{'name': 'write_file', 'input_schema': {'required': ['path', 'content']}}],
        'model': '27b', 'thinking': True})
    _append_live_event(tmp_path, 2, 'assistant_reasoning', payload={
        'mode': 'summary', 'content': 'API summary', 'source': 'provider_response', 'attempt_id': 'attempt-a'})
    _append_live_event(tmp_path, 3, 'assistant_reasoning', payload={
        'mode': 'full', 'content': 'API-observable full reasoning', 'source': 'provider_response',
        'attempt_id': 'attempt-b', 'partial': True, 'interrupted': True})
    _append_live_event(tmp_path, 4, 'tool_use', payload={'tool_use_id': 'call-1', 'name': 'write_file', 'arguments': {'path': 'deck.md'}})
    tools = json.loads(trace.agent_trace_payload(str(tmp_path), 'orch', source='tools')['content'])
    assert tools[0]['status'] == 'running'
    _append_live_event(tmp_path, 5, 'tool_result', payload={'tool_use_id': 'call-1', 'ok': True, 'result': 'done', 'duration_ms': 15})
    sources = {name: trace.agent_trace_payload(str(tmp_path), 'orch', source=name)
               for name in trace._AGENT_TRACE_SOURCES}
    assert sources['system_prompt']['content'] == 'Actual system prompt'
    assert json.loads(sources['config']['content'])['thinking'] is True
    assert json.loads(sources['config']['content'])['tool_schemas'][0]['name'] == 'write_file'
    assert 'Initial input' in sources['messages']['content']
    assert 'API summary' in sources['summary']['content'] and 'full reasoning' not in sources['summary']['content']
    assert 'API-observable full reasoning' in sources['full_reasoning']['content']
    assert json.loads(sources['full_reasoning']['content'])[0]['interrupted'] is True
    assert json.loads(sources['tools']['content'])[0]['status'] == 'completed'
    assert len(json.loads(sources['events']['content'])) == 5
    _append_live_event(tmp_path, 6, 'agent_started', segment_id='new-segment')
    missing = trace.agent_trace_payload(str(tmp_path), 'orch', source='full_reasoning')
    assert missing['empty_state']['kind'] == 'unavailable_in_source_trace'


def test_raw_http_owned_only_signature_metadata_and_stable_stream_cursor(download_case):
    root = download_case['root']
    raw = root / '_trace/raw/provider'
    raw.mkdir(parents=True)
    payload = {'body': {'content': [
                   {'type': 'thinking', 'thinking': 'observable reasoning', 'signature': 'private-signature'},
                   {'type': 'text', 'text': 'signature=author; api_key=example; EMPTY'},
                   {'type': 'tool_use', 'id': 't', 'name': 'example', 'input': {'signature': 'author signature'}}]},
               'headers': {'Authorization': 'Bearer secret', 'Set-Cookie': 'private-cookie'}}
    (raw / 'attempt-a.response.json').write_text(json.dumps(payload))
    status, _, body = request('/api/decks/1/provider-trace/attempt-a/response')
    assert status == 200
    value = json.loads(body)
    assert 'observable reasoning' in value['content']
    assert 'private-signature' not in value['content'] and 'private-cookie' not in value['content']
    blocks = json.loads(value['content'])['body']['content']
    assert blocks[0]['signature_metadata']['present'] is True
    assert blocks[1:] == payload['body']['content'][1:]
    assert json.loads(value['content'])['headers'] == {}
    assert request('/api/decks/2/provider-trace/attempt-a/response')[0] == 404
    assert request('/api/decks/1/provider-trace/attempt-a/unknown')[0] == 400
    with pytest.raises(main.HTTPException) as error:
        main._safe_target({'run_dir': str(root)}, '_trace/raw/provider/attempt-a.response.json')
    assert error.value.status_code == 403
    stream = raw / 'attempt-a.stream.jsonl'
    stream.write_text('{"sequence":1,"event":{"text":"first"}}\n')
    first = trace.provider_trace_payload(str(root), 'attempt-a', 'stream')
    with stream.open('a') as handle:
        handle.write('{"sequence":2,"event":{"text":"second"}}\n{"unfinished":')
    second = trace.provider_trace_payload(str(root), 'attempt-a', 'stream', cursor=first['next_cursor'])
    assert 'second' in second['content'] and 'first' not in second['content']
    with pytest.raises(ValueError):
        trace.provider_trace_payload(str(root), '../escape', 'request')


def test_critic_completion_does_not_finish_another_running_segment(tmp_path):
    write_workspace_manifest(tmp_path)
    for sequence, kind, segment in [(1, "agent_started", "a"), (2, "agent_started", "b"),
                                    (3, "agent_completed", "a")]:
        _append_live_event(tmp_path, sequence, kind, agent_id="critic-a", role="critic", segment_id=segment)
    assert trace.live_events(str(tmp_path))["agent_statuses"]["critic"]["status"] == "running"
    _append_live_event(tmp_path, 4, "agent_completed", agent_id="critic-a", role="critic", segment_id="b")
    assert trace.live_events(str(tmp_path))["agent_statuses"]["critic"]["status"] == "complete"


def test_role_binding_survives_events_without_group_metadata(tmp_path):
    write_workspace_manifest(tmp_path)
    _append_live_event(tmp_path, 1, "agent_context", agent_id="slide-owner-a", role="slide", payload={"group_id": "facts"})
    _append_live_event(tmp_path, 2, "assistant_message", agent_id="slide-owner-a", role="slide", payload={"message": "Actual message"})
    live = trace.live_events(str(tmp_path))
    assert len(live["agents"]) == 1
    assert len(next(iter(live["agents"].values()))) == 2
