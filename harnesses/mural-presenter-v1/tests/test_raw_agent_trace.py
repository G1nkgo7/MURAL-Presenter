"""真实 transport 边界的合成 HTTP 响应；不调用远程模型。"""
import base64
import json
from pathlib import Path
import shutil
import threading

import httpx
import pytest

from export.readable import AgentTraceProjection, export_readable_by_role
from runtime.agent import AgentRuntime
from runtime.config import ProviderSettings
from runtime.live_trace import LiveEventJournal, iter_jsonl_events
from runtime.raw_trace import RawAttempt, capture, externalize_binary
from runtime.session import ModelRequest, ModelSession, OpenAIChatCompatibleTransport, ProviderChannel
from runtime.tools import ToolContext, ToolRuntime
from state.workspace import WorkspaceState
from test_agent_runtime import ScriptedTransport, _response, _text, _tool, _definition


def journal(root):
    WorkspaceState(root)
    return LiveEventJournal(root / '_trace/live-events.jsonl', projector=AgentTraceProjection(root).apply)


@pytest.mark.parametrize("rollover", [False, True])
def test_failed_segment_is_captured_with_context_and_closed_lineage(tmp_path, rollover):
    from export.training import TrainingExporter
    from qc.routing import IssueLedger
    from state.artifacts import ArtifactStore
    from runtime.agent import AgentProviderError
    from runtime.recovery import SegmentCheckpoint, tool_boundary

    live = journal(tmp_path)
    workspace = WorkspaceState(tmp_path)
    exporter = TrainingExporter(workspace, ArtifactStore(tmp_path, workspace_state=workspace), IssueLedger(tmp_path), run_mode="synthesis")
    messages = ({"role": "user", "content": "continue this owner's work"},)
    checkpoint = SegmentCheckpoint(segment_id="original", predecessor_segment_id=None,
        messages=messages, provider_affinity_id="G" if rollover else "")
    error = httpx.HTTPStatusError("quota exhausted", request=httpx.Request("POST", "https://provider.test"),
        response=httpx.Response(403))
    transport = ScriptedTransport([error, ValueError("explicit provider failure")] if rollover else [ValueError("explicit provider failure")])
    settings = ProviderSettings(api_format="anthropic", model="test", base_url="https://provider.test", max_attempts=2)
    agent = AgentRuntime(settings=settings,
        channels=(ProviderChannel("secret-g", "G"), ProviderChannel("secret-k", "K")),
        transport=transport, tools=ToolRuntime(), event_sink=live.record_runtime_event, balance="primary-first")
    with pytest.raises(AgentProviderError):
        agent.run(agent_id="same-owner", role="research", system="actual instruction",
            tool_context=ToolContext("same-owner", "research", metadata={"workspace_root": str(tmp_path)}),
            checkpoint=checkpoint, max_turns=3, max_tokens=128, thinking=False, effort="low",
            checkpoint_sink=lambda value: exporter.capture("same-owner", "research", value))
    segments = exporter.load_segments()
    contexts = [e for e in iter_jsonl_events(live.path) if e["event_type"] == "agent_context"]
    assert len(segments) == len(contexts) == (2 if rollover else 1)
    assert {segment.checkpoint.segment_id for segment in segments} == {e["segment_id"] for e in contexts}
    assert all(segment.owner_id == "same-owner" and tool_boundary(segment.checkpoint.messages).is_closed for segment in segments)
    for path in (tmp_path / "_trace/raw/segments").glob("*.json"):
        raw = json.loads(path.read_text())
        assert raw["agent_context"] and raw["agent_context"][0]["system"] == "actual instruction"
        assert not any(message["role"] == "assistant" for message in raw["messages"])
    if rollover:
        assert segments[-1].checkpoint.predecessor_segment_id == "original"
        assert segments[-1].checkpoint.excluded_channel_ids == ("G",)
    assert not exporter.manifest_path.exists()


def test_canonical_full_payload_is_replayable_without_eager_disk_projections(tmp_path):
    workspace = WorkspaceState(tmp_path)
    live = LiveEventJournal(tmp_path / '_trace/live-events.jsonl', full_payload=True)
    context = dict(owner_id='orchestrator', role='orchestrator', segment_id='s1', turn=1)
    live.append(agent_id='orchestrator', role='orchestrator', segment_id='s1', event_type='agent_context',
                payload={'system': 'actual instruction', 'messages': [], 'tools': []})
    payload = 'actual-content-' * 1000
    call = dict(tool_use_id='fixture-full', tool_name='write_file', **context)
    live.record_tool_event(dict(call, event_type='tool_use', arguments={'path': 'deck.md', 'content': payload,
        'api_key': 'must-not-leak', 'thinking_signature': 'private-signature'}))
    (tmp_path / 'deck.md').write_text(payload)
    live.record_tool_event(dict(call, event_type='tool_result', ok=True, content=payload))
    rows = list(iter_jsonl_events(live.path))
    assert rows[-2]['payload']['arguments']['content'] == payload
    assert rows[-1]['payload']['result'] == payload
    assert rows[-1]['payload']['artifacts'][0]['path'] == 'deck.md'
    assert rows[-2]['payload']['arguments']['api_key'] == 'must-not-leak'
    assert rows[-2]['payload']['arguments']['thinking_signature'] == 'private-signature'
    assert not (tmp_path / '_trace/agent-index.json').exists()
    assert not (tmp_path / '_trace/orchestrator').exists()
    original = live.path.read_bytes()
    export_readable_by_role(workspace)
    assert live.path.read_bytes() == original
    transaction = json.loads((tmp_path / '_trace/orchestrator/tool_log.json').read_text())['transactions'][0]
    assert transaction['args']['content'] == payload and transaction['result_summary'] == payload


@pytest.mark.parametrize("kind", ["agent_turn_completed", "resource_wait_started",
    "resource_wait_completed", "provider_started", "provider_succeeded", "provider_raw_request",
    "provider_raw_response", "critic_started", "critic_completed"])
def test_progress_diagnostics_do_not_rewrite_unchanged_message_snapshots(tmp_path, kind):
    live = journal(tmp_path)
    common = {'agent_id': 'orchestrator', 'role': 'orchestrator', 'segment_id': 'segment', 'turn': 1}
    live.record_runtime_event({**common, 'event_type': 'agent_started'})
    directory = tmp_path / '_trace/orchestrator'
    snapshots = {name: (directory / name).read_bytes() for name in (
        'messages.json', 'tool_log.json', 'tools.json', 'artifacts.json')}
    live.record_runtime_event({**common, 'event_type': kind, 'attempt_id': 'fixture', 'stop_reason': 'tool_use'})
    assert all((directory / name).read_bytes() == content for name, content in snapshots.items())
    assert list(iter_jsonl_events(directory / 'events.jsonl'))[-1]['event_type'] == kind
    export_readable_by_role(WorkspaceState(tmp_path))
    assert all((directory / name).read_bytes() == content for name, content in snapshots.items())
    assert len(list(iter_jsonl_events(directory / 'events.jsonl'))) == 2


def test_tool_transactions_leave_unchanged_tools_and_artifacts_snapshots_intact(tmp_path):
    live = journal(tmp_path)
    common = dict(agent_id='orchestrator', role='orchestrator', segment_id='s1')
    live.append(**common, event_type='agent_context', payload={'system': 'fixture', 'messages': [], 'tools': []})
    directory = tmp_path / '_trace/orchestrator'
    unchanged = {name: (directory / name).read_bytes() for name in ('tools.json', 'artifacts.json')}
    payload = {'tool_use_id': 'fixture', 'name': 'read_file'}
    live.append(**common, event_type='tool_use', payload={**payload, 'arguments': {'path': 'deck.md'}})
    live.append(**common, event_type='tool_result', payload={**payload, 'ok': True, 'result': 'actual text'})
    assert all((directory / name).read_bytes() == content for name, content in unchanged.items())
    transaction = json.loads((directory / 'tool_log.json').read_text())['transactions'][0]
    assert transaction['status'] == 'completed' and transaction['result_summary'] == 'actual text'
    def contents():
        return {path.name: (json.loads(path.read_text()) if path.suffix == '.json' else
            [json.loads(line) for line in path.read_text().splitlines()] if path.suffix == '.jsonl' else
            path.read_bytes()) for path in directory.iterdir()}
    before = contents()
    export_readable_by_role(WorkspaceState(tmp_path))
    assert before == contents()


def test_context_and_tool_transaction_are_on_disk_before_agent_finishes(tmp_path):
    live = journal(tmp_path)
    tools = ToolRuntime(event_sink=live.record_tool_event)
    entered, release = threading.Event(), threading.Event()
    def handler(context, arguments):
        entered.set()
        assert release.wait(10)
        return {'ok': True, 'text': 'written'}
    tools.register(_definition('write_file', handler))
    transport = ScriptedTransport([_response(_tool('call-1', 'write_file', {'path': 'slides/slide_01.html'}), stop_reason='tool_use'), _response(_text('done'))])
    settings = ProviderSettings(api_format='anthropic', model='test', base_url='https://synthetic.test')
    agent = AgentRuntime(settings=settings, channels=[ProviderChannel('opaque-fixture-key', 'channel')],
        transport=transport, tools=tools, event_sink=live.record_runtime_event)
    errors = []
    def run():
        try:
            agent.run(agent_id='slide-a', role='slide', system='Effective system',
                tool_context=ToolContext('slide-a', 'slide', metadata={'workspace_root': str(tmp_path), 'group_id': 'facts', 'pages': [1, 2], 'attempt': 1}),
                initial_messages=[{'role': 'user', 'content': 'Effective input'}],
                max_turns=3, max_tokens=128, thinking=False, effort='low')
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(10)
        folder = tmp_path / '_trace/subagents/slide_group_facts'
        records = json.loads((folder / 'tool_log.json').read_text())['transactions']
        assert records[0]['status'] == 'running' and records[0]['finished_at'] is None
        assert 'Effective system' in (folder / 'system_prompt.md').read_text()
        assert json.loads((folder / 'tools.json').read_text())['tools'] == tools.advertised_tools('slide')
        assert not any(row['event_type'] == 'agent_completed' for row in iter_jsonl_events(live.path))
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive() and not errors
    records = json.loads((folder / 'tool_log.json').read_text())['transactions']
    assert len(records) == 1 and records[0]['tool_use_id'] == 'call-1' and records[0]['status'] == 'completed'
    assert not (tmp_path / '_trace/subagents/material').exists()
    assert not (tmp_path / '_trace/by-role').exists()


def test_raw_signature_is_private_summary_is_exact_and_credentials_are_removed(tmp_path):
    live = journal(tmp_path)
    def emit(kind, **payload):
        live.record_runtime_event({'event_type': kind, 'agent_id': 'orch', 'role': 'orchestrator', 'turn': 1, 'segment_id': 's1', **payload})
    secret = 'opaque-fixture-secret'
    with RawAttempt(tmp_path, 'attempt-test', emit=emit, metadata={'api_format': 'anthropic'}):
        capture('request', {'system': 'actual', 'messages': [{'role': 'user', 'content': secret}], 'thinking': {'display': 'summarized'}},
                {'Authorization': 'Bearer header-only-secret', 'Cookie': 'cookie-secret', 'Content-Type': 'application/json'})
        capture('response_headers', 200, {'set-cookie': 'cookie-secret'})
        capture('response', {'content': [{'type': 'thinking', 'thinking': 'provider summary', 'signature': 'protocol-signature'}], 'usage': {'output_tokens': 7}})
    rows = list(iter_jsonl_events(live.path))
    reasoning = next(row for row in rows if row['event_type'] == 'assistant_reasoning')['payload']
    assert reasoning['mode'] == 'summary' and reasoning['content'] == 'provider summary'
    assert reasoning['signature_metadata']['present']
    raw = tmp_path / '_trace/raw/provider/attempt-test.response.json'
    assert 'protocol-signature' in raw.read_text() and raw.stat().st_mode & 0o077 == 0
    for path in (tmp_path / '_trace').rglob('*'):
        if path.is_file():
            text = path.read_text()
            assert 'header-only-secret' not in text and 'cookie-secret' not in text
            if not path.is_relative_to(tmp_path / '_trace/raw'):
                assert 'protocol-signature' not in text
    request = json.loads((tmp_path / '_trace/raw/provider/attempt-test.request.json').read_text())
    assert request['body']['messages'][0]['content'] == secret
    assert request['headers'] == {'Content-Type': 'application/json'}


def test_raw_capture_preserves_binary_even_when_base64_contains_credential_text(tmp_path):
    import base64
    encoded = 'EMPTYAAA'
    payload = base64.b64decode(encoded, validate=True)
    source = {'type': 'base64', 'media_type': 'application/octet-stream', 'data': encoded}
    uri = 'data:application/octet-stream;base64,' + encoded
    attempt = RawAttempt(tmp_path, 'attempt-binary', emit=lambda *_a, **_k: None, metadata={})
    cleaned = attempt.clean({'source': source, 'image_url': uri, 'text': 'credential EMPTY'})
    assert cleaned['source'] == cleaned['image_url']
    assert (tmp_path / cleaned['source']['path']).read_bytes() == payload
    assert cleaned['text'] == 'credential EMPTY'
    assert source['data'] == encoded


def test_raw_capture_scrubs_inline_data_urls_in_text(tmp_path):
    encoded = base64.b64encode(b'inline text payload').decode()
    attempt = RawAttempt(tmp_path, 'attempt-inline-binary', emit=lambda *_a, **_k: None, metadata={})
    cleaned = attempt.clean({'text': f'before data:image/png;base64,{encoded} after'})
    text = cleaned['text']
    assert text.startswith('before [inline-binary-omitted;mime=image/png;sha256=')
    assert text.endswith(';bytes=19] after')
    assert 'data:image' not in text
    assert encoded not in text


def test_raw_capture_scrubs_long_bare_base64_in_text(tmp_path):
    encoded = base64.b64encode(b'x' * 4096).decode()
    attempt = RawAttempt(tmp_path, 'attempt-long-binary', emit=lambda *_a, **_k: None, metadata={})
    cleaned = attempt.clean({'text': f'prefix {encoded} suffix'})
    text = cleaned['text']
    assert text.startswith('prefix [inline-binary-omitted;sha256=')
    assert text.endswith(';bytes=4096] suffix')
    assert encoded not in text


def test_raw_capture_keeps_long_plain_alphabetic_text(tmp_path):
    tail = "End of intact evidence"
    plain = "a" * 70000 + tail
    attempt = RawAttempt(tmp_path, 'attempt-long-plain-text', emit=lambda *_a, **_k: None, metadata={})
    cleaned = attempt.clean({'text': plain})
    assert cleaned["text"] == plain
    assert "inline-binary-omitted" not in cleaned["text"]


@pytest.mark.parametrize('field', ['api_key', 'Authorization', 'X-Api-Key', 'Proxy-Authorization', 'Cookie', 'Set-Cookie'])
def test_raw_capture_does_not_persist_credentials_that_look_like_binary(field, tmp_path):
    uri = 'data:application/octet-stream;base64,c2VjcmV0'
    attempt = RawAttempt(tmp_path, 'attempt-auth-binary', emit=lambda *_a, **_k: None, metadata={})
    with attempt:
        attempt.request({'messages': []}, {field: uri})
        attempt.response_headers(200, {field: uri})
    assert json.loads(attempt.path('request').read_text())['headers'] == {}
    assert json.loads(attempt.path('response').read_text())['headers'] == {}
    assert not (tmp_path / '_trace/raw/blobs').exists()


def test_streaming_failover_keeps_partial_full_reasoning_in_its_own_attempt(tmp_path):
    live = journal(tmp_path)
    class Stream(httpx.AsyncByteStream):
        def __init__(self, broken): self.broken = broken
        async def __aiter__(self):
            delta = {'reasoning_content': 'partial evidence' if self.broken else 'complete evidence'}
            yield ('data: ' + json.dumps({'choices': [{'index': 0, 'delta': delta}]}) + '\n\n').encode()
            if self.broken:
                raise httpx.ReadTimeout('synthetic interruption')
            yield b'data: {"choices":[{"index":0,"delta":{"content":"done"},"finish_reason":"stop"}]}\n\n'
            yield b'data: [DONE]\n\n'
    calls = []
    def http(request):
        calls.append(request)
        return httpx.Response(200, stream=Stream(len(calls) == 1), headers={'content-type': 'text/event-stream'})
    settings = ProviderSettings(api_format='openai_chat_compatible', model='27b', base_url='https://synthetic.test/v1',
        streaming=True, max_attempts=2, channel_cooldown_s=0)
    transport = OpenAIChatCompatibleTransport(settings, client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(http)))
    session = ModelSession(settings=settings, channels=[ProviderChannel('fixture-a', 'A'), ProviderChannel('fixture-b', 'B')],
        transport=transport, trace_root=str(tmp_path), event_sink=lambda event: live.record_runtime_event({
            'agent_id': 'research-a', 'role': 'research', 'turn': 1, 'segment_id': 's1', **event}))
    result = session.call(ModelRequest(model='27b', system='actual', messages=({'role': 'user', 'content': 'search'},)))
    assert result.ok and len(calls) == 2
    rows = list(iter_jsonl_events(live.path))
    reasoning = [row['payload'] for row in rows if row['event_type'] == 'assistant_reasoning']
    assert [(item['content'], item['mode'], item['interrupted']) for item in reasoning] == [
        ('partial evidence', 'full', True), ('complete evidence', 'full', False)]
    assert len({item['attempt_id'] for item in reasoning}) == 2
    assert any(row['event_type'] == 'provider_failover' for row in rows)
    assert len(list((tmp_path / '_trace/raw/provider').glob('*.request.json'))) == 2
    for path in (tmp_path / '_trace/raw/provider').glob('*.stream.jsonl'):
        stream = list(iter_jsonl_events(path))
        assert [item['sequence'] for item in stream] == list(range(1, len(stream) + 1))
        assert stream[-1]['status'] in {'completed', 'interrupted'}


def test_projection_rebuild_resume_and_half_tail_preserve_global_identity(tmp_path):
    live = journal(tmp_path)
    event = dict(agent_id='slide-a', role='slide', segment_id='s1', event_type='agent_context',
                 payload={'group_id': 'facts', 'pages': [2, 3], 'attempt': 2, 'system': 'actual', 'messages': [], 'tools': []})
    live.append(**event, event_id='context-1')
    folder = tmp_path / '_trace/subagents/slide_group_facts_attempt_02'
    original = live.path.read_bytes()
    shutil.rmtree(folder)
    export_readable_by_role(WorkspaceState(tmp_path))
    assert live.path.read_bytes() == original and (folder / 'config.json').is_file()
    before = {path.name: path.read_bytes() for path in folder.iterdir()}
    export_readable_by_role(WorkspaceState(tmp_path))
    assert before == {path.name: path.read_bytes() for path in folder.iterdir()}
    with live.path.open('ab') as handle: handle.write(b'{"partial":')
    resumed = LiveEventJournal(live.path, projector=AgentTraceProjection(tmp_path).apply)
    assert resumed.append(**event, event_id='context-1') is None
    resumed.append(agent_id='slide-a', role='slide', segment_id='s2', event_type='agent_completed')
    events = list(iter_jsonl_events(live.path))
    assert [row['sequence'] for row in events] == [1, 2]
    assert len({row['event_id'] for row in events}) == 2


def test_projection_failure_is_durable_and_does_not_block_execution(tmp_path):
    def broken(event): raise OSError('synthetic projection failure')
    live = LiveEventJournal(tmp_path / '_trace/live-events.jsonl', projector=broken)
    live.append(agent_id='a', role='slide', event_type='agent_started')
    events = list(iter_jsonl_events(live.path))
    assert [row['event_type'] for row in events] == ['agent_started', 'trace_projection_failed']
    assert events[-1]['payload']['last_sequence'] == 1 and live.sequence == 2


def test_binary_inputs_are_content_addressed_once(tmp_path):
    data = base64.b64encode(b'synthetic binary input').decode()
    first = externalize_binary(tmp_path, {'type': 'base64', 'media_type': 'image/png', 'data': data})
    second = externalize_binary(tmp_path, 'data:image/png;base64,' + data)
    assert first == second
    assert len(list((tmp_path / '_trace/raw/blobs').iterdir())) == 1
    assert (tmp_path / first['path']).read_bytes() == b'synthetic binary input'


def test_killed_process_raw_stream_is_recovered_without_rewriting_evidence(tmp_path):
    import multiprocessing
    import os
    import signal
    live = journal(tmp_path)
    context = multiprocessing.get_context('fork')
    parent, child = context.Pipe()
    def run():
        writer = LiveEventJournal(live.path, projector=AgentTraceProjection(tmp_path).apply)
        def emit(kind, **payload):
            writer.record_runtime_event({'event_type': kind, 'agent_id': 'research-a', 'role': 'research',
                'segment_id': 's1', 'turn': 1, **payload})
        emit('agent_started')
        with RawAttempt(tmp_path, 'attempt-killed', emit=emit, metadata={'api_format': 'openai_chat_compatible'}):
            capture('request', {'messages': [{'role': 'user', 'content': 'actual request'}]}, {})
            capture('stream', {'choices': [{'index': 0, 'delta': {'reasoning_content': 'recorded partial evidence'}}]})
            child.send('flushed')
            child.recv()
    process = context.Process(target=run)
    process.start()
    try:
        assert parent.poll(10) and parent.recv() == 'flushed'
        stream = tmp_path / '_trace/raw/provider/attempt-killed.stream.jsonl'
        original = stream.read_bytes()
        os.kill(process.pid, signal.SIGKILL)
        process.join(10)
        resumed = LiveEventJournal(live.path, projector=AgentTraceProjection(tmp_path).apply)
        resumed.close_interrupted_events()
        rows = list(iter_jsonl_events(live.path))
        reasoning = next(row['payload'] for row in rows if row['event_type'] == 'assistant_reasoning')
        assert reasoning['content'] == 'recorded partial evidence'
        assert reasoning['partial'] and reasoning['interrupted']
        assert stream.read_bytes() == original
        saved = live.path.read_bytes()
        resumed.close_interrupted_events()
        assert live.path.read_bytes() == saved
        assert len({row['event_id'] for row in rows}) == len(rows)
    finally:
        if process.is_alive():
            process.terminate()
            process.join(10)
        parent.close()
        child.close()


def test_artifact_hash_is_recorded_at_execution_and_not_recomputed_on_rebuild(tmp_path):
    live = journal(tmp_path)
    artifact = tmp_path / 'deck.md'
    artifact.write_text('original')
    identity = {'owner_id': 'orch', 'role': 'orchestrator', 'segment_id': 's1',
                'turn': 1, 'tool_use_id': 'read-1', 'name': 'read_file'}
    live.record_tool_event({**identity, 'event_type': 'tool_use', 'arguments': {'path': 'deck.md'}})
    live.record_tool_event({**identity, 'event_type': 'tool_result', 'content': 'original', 'ok': True})
    path = tmp_path / '_trace/orchestrator/artifacts.json'
    before = json.loads(path.read_text())['artifacts']
    assert len(before) == 1
    artifact.write_text('changed since execution')
    export_readable_by_role(WorkspaceState(tmp_path))
    assert json.loads(path.read_text())['artifacts'] == before


def test_text_split_across_stream_chunks_is_persisted_without_rewriting(tmp_path):
    secret = 'opaque-fixture-secret'
    with RawAttempt(tmp_path, 'attempt-split', emit=lambda *args, **kwargs: None,
                    metadata={}):
        capture('request', {'messages': []}, {})
        capture('stream', {'choices': [{'delta': {'content': 'safe ' + secret[:10]}}]})
        assert (tmp_path / '_trace/raw/provider/attempt-split.stream.jsonl').exists()
        capture('stream', {'choices': [{'delta': {'content': secret[10:] + ' finished.'}}]})
    rows = list(iter_jsonl_events(tmp_path / '_trace/raw/provider/attempt-split.stream.jsonl'))
    text = ''.join(row['event']['choices'][0]['delta']['content'] for row in rows if 'event' in row)
    assert text == 'safe ' + secret + ' finished.'


def test_reasoning_delta_is_projected_while_provider_is_still_running(tmp_path):
    live = journal(tmp_path)
    def emit(kind, **payload):
        live.record_runtime_event({'event_type': kind, 'agent_id': 'orch', 'role': 'orchestrator',
                                   'segment_id': 's1', 'turn': 1, **payload})
    with RawAttempt(tmp_path, 'attempt-running', emit=emit, metadata={}):
        capture('request', {'messages': []}, {})
        capture('stream', {'choices': [{'delta': {'reasoning_content': 'API-visible progress.'}}]})
        events = list(iter_jsonl_events(live.path))
        assert events[-1]['event_type'] == 'assistant_reasoning_delta'
        assert not any(row['event_type'] == 'provider_raw_response' for row in events)
        reasoning = list(iter_jsonl_events(tmp_path / '_trace/orchestrator/reasoning.jsonl'))
        assert reasoning[-1]['content'] == 'API-visible progress.' and reasoning[-1]['partial']


def test_alternating_projectors_keep_all_agents_and_global_order(tmp_path):
    first = journal(tmp_path)
    second = LiveEventJournal(first.path, projector=AgentTraceProjection(tmp_path).apply)
    first.append(agent_id="orch", role="orchestrator", segment_id="s1", event_type="agent_started")
    second.append(agent_id="research-a", role="research", segment_id="s2", event_type="agent_started")
    first.append(agent_id="orch", role="orchestrator", segment_id="s1", event_type="agent_completed")
    index = json.loads((tmp_path / "_trace/agent-index.json").read_text())
    assert {row["agent_id"]: row["last_sequence"] for row in index["agents"]} == {"orch": 3, "research-a": 2}


@pytest.mark.parametrize("text", [
    'Authorization: Bearer opaque-inline-fixture',
    '{"authorization": "Bearer opaque-inline-fixture"}',
    'api_key="opaque-inline-fixture"',
])
def test_credential_examples_are_not_rewritten_as_connection_config(text, tmp_path):
    with RawAttempt(tmp_path, 'attempt-example', emit=lambda *_a, **_k: None, metadata={}) as attempt:
        attempt.request({'messages': [{'role': 'user', 'content': text}]}, {})
        attempt.response({'choices': [{'message': {'content': text}}]})
    assert json.loads(attempt.path('request').read_text())['body']['messages'][0]['content'] == text
    assert json.loads(attempt.path('response').read_text())['body']['choices'][0]['message']['content'] == text


def test_context_keeps_observable_text_and_tool_fields_without_protocol_signature(tmp_path):
    live = journal(tmp_path)
    messages = [{'role': 'assistant', 'content': [
        {'type': 'thinking', 'thinking': 'Provider observable reasoning EMPTY', 'signature': 'native-private'},
        {'type': 'tool_use', 'id': 'tool', 'name': 'example', 'input': {'signature': 'author', 'api_key': 'example'}}]}]
    event = live.append(agent_id='orch', role='orchestrator', event_type='agent_context',
                       payload={'system': 'token = EMPTY', 'messages': messages, 'tools': []})
    blocks = event['payload']['messages'][0]['content']
    assert blocks[0] == {'type': 'thinking', 'thinking': 'Provider observable reasoning EMPTY'}
    assert blocks[1] == messages[0]['content'][1]
    assert messages[0]['content'][0]['signature'] == 'native-private'
