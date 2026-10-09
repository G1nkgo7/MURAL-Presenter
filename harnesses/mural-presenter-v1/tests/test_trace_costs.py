"""只读统计真实记录，缺失、不完整和不同口径不能被伪造为精确成本。"""
from __future__ import annotations

import hashlib
import json

from export.readable import main, summarize_trace_costs


def _write(root, relative, value):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')
    return relative


def _events(root, rows, tail=b''):
    path = root / '_trace/live-events.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b''.join((json.dumps(row) + '\n').encode() for row in rows) + tail)


def _event(identity, kind, *, role='slide', agent='owner', second=0, **payload):
    return {'event_id': identity, 'sequence': second + 1, 'timestamp': f'2026-09-08T00:00:{second:02d}Z',
            'event_type': kind, 'agent_id': agent, 'role': role, 'turn': 1, 'segment_id': 's0', 'payload': payload}


def _bytes(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob('*') if path.is_file()}


def test_provider_attempt_dedup_tokens_keep_api_semantics_and_concurrency(tmp_path):
    raw1 = _write(tmp_path, '_trace/raw/p1.json', {'attempt_id': 'p1', 'status': 'completed',
        'api_format': 'anthropic', 'body': {'thinking_signature': 'PRIVATE', 'usage': {
            'input_tokens': 1, 'cache_read_input_tokens': 100, 'cache_creation_input_tokens': 20, 'output_tokens': 11}}})
    raw2 = _write(tmp_path, '_trace/raw/p2.json', {'attempt_id': 'p2', 'status': 'completed',
        'api_format': 'openai_chat_compatible', 'body': {'usage': {
            'prompt_tokens': 100, 'completion_tokens': 2, 'prompt_tokens_details': {'cached_tokens': 60}}}})
    rows = [
        _event('a', 'provider_started', attempt_id='p1', api_format='anthropic', queue_wait_s=2),
        _event('b', 'provider_raw_response', attempt_id='p1', raw_ref=raw1),
        _event('c', 'provider_raw_response', attempt_id='p1', raw_ref=raw1),
        _event('d', 'provider_succeeded', attempt_id='p1', elapsed_s=10, response_diagnostics={'output_tokens': 11}, second=10),
        _event('e', 'provider_started', role='content-audit', agent='audit', attempt_id='p2', api_format='openai_chat_compatible', queue_wait_s=1),
        _event('f', 'provider_raw_response', role='content-audit', agent='audit', attempt_id='p2', raw_ref=raw2),
        _event('g', 'provider_succeeded', role='content-audit', agent='audit', attempt_id='p2', elapsed_s=5, second=5),
        _event('h', 'provider_started', attempt_id='p3', api_format='anthropic'),
        _event('i', 'provider_failed', attempt_id='p3', elapsed_s=7, error_summary='Bearer credential-do-not-display', second=7),
        _event('j', 'provider_failover', attempt_id='p3'),
        _event('k', 'provider_started', attempt_id='p4', api_format='anthropic', second=20),
        _event('l', 'resource_wait_failed', attempt_id='no-request', queue_wait_s=8),
    ]
    _events(tmp_path, [*rows, rows[3]], tail=b'not-json\n{"partial"')
    _write(tmp_path, '_state/skill-runtime.json', {'status': 'completed', 'outcome': {'usage': {'input_tokens': 9999}}})
    _write(tmp_path, '_trace/training/training-manifest.json', {'training_admission': 'pending'})
    before = _bytes(tmp_path)
    report = summarize_trace_costs([tmp_path])
    assert _bytes(tmp_path) == before
    row = report['workspaces'][0]
    assert row['duplicate_event_ids_ignored'] == 1 and row['trace_tail_complete'] is False
    assert row['trace_observed_span_s'] == 20
    provider = row['provider']
    assert provider['attempts'] == provider['started'] == 4
    assert provider['states'] == {'succeeded': 2, 'failed': 1, 'open': 1}
    assert provider['elapsed_s'] == {'sum': 22, 'recorded': 3, 'missing': 1}
    assert provider['local_queue_wait_s'] == {'sum': 3, 'recorded': 2, 'missing': 2}
    assert row['queue_only'] == {'attempts': 1, 'wait_s': {'sum': 8, 'recorded': 1, 'missing': 0}}
    assert row['provider_failover_events'] == 1
    assert row['roles']['content-audit']['provider']['attempts'] == 1
    assert row['roles']['slide']['provider']['attempts'] == 3
    tokens = provider['tokens_by_api_format']
    assert tokens['anthropic']['input_tokens'] == {'sum': 1, 'recorded': 1, 'missing': 2}
    assert tokens['anthropic']['cache_read_input_tokens']['sum'] == 100
    assert tokens['anthropic']['cache_creation_input_tokens']['sum'] == 20
    assert tokens['anthropic']['output_tokens']['sum'] == 11
    assert tokens['openai_chat_compatible']['input_tokens']['sum'] == 100
    assert tokens['openai_chat_compatible']['cache_read_input_tokens']['sum'] == 60
    assert 'excludes' in tokens['anthropic']['input_semantics']
    assert 'includes' in tokens['openai_chat_compatible']['input_semantics']
    assert row['terminal_outcome_usage']['input_tokens'] == 9999
    assert row['terminal_outcome_usage']['not_added_to_attempt_totals'] is True
    assert report['denominator']['pending'] == 1
    # 成本报表只取用量，不复制 error_summary 或原始响应正文。
    assert 'credential-do-not-display' not in json.dumps(report) and 'PRIVATE' not in json.dumps(report)


def test_page_calls_receipts_and_owner_repair_binding_are_not_quality_inference(tmp_path):
    receipt = {'page': 1, 'html_sha256': 'html1', 'png_sha256': 'png1'}
    packet = {'owner_id': 'owner', 'task_id': 'repair-page', 'attempt': 2, 'repair': True}
    rows = [
        _event('a', 'agent_started', task_id='repair-page', attempt=2, pages=[1]),
        _event('b', 'agent_context', task_id='repair-page', attempt=2, pages=[1], page_uids=['page-uid'],
               revision_id='revision', messages=[{'role': 'user', 'content': json.dumps({'owner_packet': packet})}]),
        _event('c', 'agent_started', agent='different', task_id='attempt-not-repair-proof', attempt=3, pages=[2]),
        _event('d', 'tool_use', tool_use_id='g', name='terminal', arguments={'command': 'python skills/alpha/scripts/slide.py render-group . --pages 1,2'}),
        _event('e', 'tool_result', tool_use_id='g', name='terminal', ok=True, duration_ms=50,
               result={'pages': [receipt, {'page': 2, 'html_sha256': 'html2', 'png_sha256': 'png2'}]}),
        _event('f', 'tool_use', tool_use_id='r', name='terminal', arguments={'command': 'python skills/alpha/scripts/slide.py render . --page=1'}),
        _event('g', 'tool_result', tool_use_id='r', name='terminal', ok=True, duration_ms=10, result={'pages': [receipt]}),
        _event('h', 'tool_use', tool_use_id='i', name='inspect', arguments={'page': 1}),
        _event('i', 'tool_result', tool_use_id='i', name='inspect', ok=True, duration_ms=20,
               result={'page': 1, 'inspection_cached': True, 'render_committed': receipt}),
        _event('j', 'tool_use', tool_use_id='fail', name='inspect', arguments={'page': 1}),
        _event('k', 'tool_result', tool_use_id='fail', name='inspect', ok=False, duration_ms=1),
        _event('l', 'tool_use', tool_use_id='open', name='inspect', arguments={'page': 2}),
        _event('m', 'tool_result', tool_use_id='orphan', name='inspect', ok=True, result={'page': 3}),
    ]
    _events(tmp_path, rows)
    row = summarize_trace_costs([tmp_path])['workspaces'][0]
    counts = row['roles']['slide']['tools']
    assert counts == {'calls': 5, 'succeeded': 3, 'failed': 1, 'open': 1, 'orphan_results': 1,
                      'render_calls': 2, 'render_successful_calls': 2, 'inspect_calls': 3, 'inspect_successful_calls': 1}
    page = row['pages']['1']
    assert page['render_calls'] == 2 and page['render_successful_calls'] == 2
    assert page['inspect_calls'] == 2 and page['inspect_successful_calls'] == 1
    assert page['inspection_cache_hits'] == 1 and page['distinct_html_png_pairs'] == 1
    assert page['render_receipt_observations'] == 3 and page['rasterizations'] is None
    assert '3' not in row['pages']
    assert row['owner_attempt_bindings'][0]['repair'] is True
    assert row['owner_attempt_bindings'][0]['revision_id'] == 'revision'
    assert row['owner_attempt_bindings'][1]['attempt'] == 3 and row['owner_attempt_bindings'][1]['repair'] is None


def test_native_group_inspection_counts_member_participation_not_rasterization(tmp_path, monkeypatch):
    from test_native_inspect import _group_workspace, _slide_context

    workspace, _, (inspect, commit) = _group_workspace(tmp_path, monkeypatch)
    context = _slide_context(workspace, (1, 2))
    first = inspect(context, {'page': [1, 2]})
    assert first['pending_inspection']['kind'] == 'group'
    commit(first['pending_inspection'])
    cached = inspect(context, {'page': [1, 2]})
    assert cached['inspection_cached'] is True and 'current_inspection' in cached
    rows = []
    for index, result in enumerate((first, cached)):
        rows.extend([
            _event(f'{index}-use', 'tool_use', tool_use_id=f'g{index}', name='inspect', arguments={'page': [1, 2]}),
            _event(f'{index}-result', 'tool_result', tool_use_id=f'g{index}', name='inspect', ok=True, result=result),
        ])
    _events(workspace.paths.root, rows)
    before = _bytes(workspace.paths.root)
    row = summarize_trace_costs([workspace.paths.root])['workspaces'][0]
    assert _bytes(workspace.paths.root) == before
    assert row['roles']['slide']['tools']['inspect_calls'] == 2
    for number in ('1', '2'):
        page = row['pages'][number]
        assert page['inspect_calls'] == page['inspect_successful_calls'] == 2
        assert page['inspection_cache_hits'] == 1
        assert page['render_receipt_observations'] == 2 and page['distinct_html_png_pairs'] == 1
        assert page['render_calls'] == 0 and page['rasterizations'] is None


def test_batch_denominators_do_not_duplicate_paths_or_drop_missing_workspaces(tmp_path, capsys):
    roots = []
    for state in ('accepted', 'rejected', 'pending', 'not_applicable'):
        root = tmp_path / state
        _write(root, 'result.json', {'status': 'completed', 'training_admission': state})
        roots.append(root)
    roots.extend(tmp_path / f'not-started-{index}' for index in range(101))
    before = _bytes(tmp_path)
    main(['--stats', *map(str, roots), str(roots[0]), str(roots[0] / '../accepted')])
    report = json.loads(capsys.readouterr().out)
    assert report['denominator'] == {'workspaces': 105, 'accepted': 1, 'rejected': 1,
                                    'pending': 1, 'not_applicable': 1, 'unknown': 101}
    assert len(report['workspaces']) == 105
    assert report['workspaces'][-1]['trace_present'] is False
    assert report['workspaces'][-1]['provider']['elapsed_s']['sum'] is None
    assert _bytes(tmp_path) == before
    assert not any(path.exists() for path in roots[4:])


def test_response_usage_missing_partial_mismatched_or_unsafe_is_not_invented(tmp_path):
    root = tmp_path / 'deck'
    raw = _write(root, '_trace/raw/wrong.json', {'attempt_id': 'other', 'status': 'completed', 'body': {'usage': {'input_tokens': 999}}})
    partial = _write(root, '_trace/raw/partial.json', {'attempt_id': 'partial', 'status': 'interrupted', 'partial': True,
                                                   'body': {'usage': {'input_tokens': 888}}})
    rows = []
    for index, (identity, ref) in enumerate((('mismatch', raw), ('partial', partial), ('unsafe', '_trace/../../secret.json'))):
        rows.extend([_event(f'{index}-start', 'provider_started', attempt_id=identity, api_format='anthropic'),
                     _event(f'{index}-raw', 'provider_raw_response', attempt_id=identity, raw_ref=ref),
                     _event(f'{index}-done', 'provider_succeeded', attempt_id=identity, response_diagnostics={'output_tokens': 2})])
    response = _write(root, '_trace/raw/responses.json', {'attempt_id': 'responses', 'status': 'completed', 'body': {
        'response': {'usage': {'input_tokens': 100, 'output_tokens': 7, 'input_tokens_details': {'cached_tokens': 30}}}}})
    rows.extend([_event('response-start', 'provider_started', attempt_id='responses', api_format='openai_responses'),
                 _event('response-raw', 'provider_raw_response', attempt_id='responses', raw_ref=response)])
    _events(root, rows)
    row = summarize_trace_costs([root])['workspaces'][0]
    tokens = row['provider']['tokens_by_api_format']
    assert tokens['anthropic']['input_tokens'] == {'sum': None, 'recorded': 0, 'missing': 3}
    assert tokens['anthropic']['output_tokens']['sum'] == 6
    assert tokens['openai_responses']['input_tokens']['sum'] == 100
    assert tokens['openai_responses']['cache_read_input_tokens']['sum'] == 30
    assert row['gaps']['unsafe_raw_reference'] == 1
    assert row['gaps']['raw_attempt_identity_missing_or_mismatched'] == 1


def test_responses_cost_buckets_include_cache_writes_and_deduplicate_mirrored_attempts(tmp_path):
    raw = _write(tmp_path, '_trace/raw/responses.json', {'attempt_id': 'one', 'status': 'completed',
        'api_format': 'openai_responses', 'body': {'usage': {'input_tokens': 100, 'output_tokens': 7,
            'input_tokens_details': {'cached_tokens': 60, 'cache_write_tokens': 30}}}})
    _events(tmp_path, [
        _event('start', 'provider_started', attempt_id='one', api_format='openai_responses'),
        _event('raw', 'provider_raw_response', attempt_id='one', raw_ref=raw),
        _event('mirror', 'provider_raw_response', agent='parent-mirror', attempt_id='one', raw_ref=raw),
        _event('done', 'provider_succeeded', attempt_id='one'),
    ])
    row = summarize_trace_costs([tmp_path])['workspaces'][0]
    assert row['provider']['attempts'] == 1
    tokens = row['provider']['tokens_by_api_format']['openai_responses']
    assert tokens['input_tokens']['sum'] == 100
    assert tokens['cache_read_input_tokens']['sum'] == 60
    assert tokens['cache_creation_input_tokens']['sum'] == 30
    assert tokens['ordinary_input_tokens']['sum'] == 10


def test_responses_cost_does_not_assume_missing_cache_write_usage_is_zero(tmp_path):
    raw = _write(tmp_path, '_trace/raw/responses.json', {'attempt_id': 'one', 'status': 'completed',
        'api_format': 'openai_responses', 'body': {'usage': {'input_tokens': 100, 'output_tokens': 7,
            'input_tokens_details': {'cached_tokens': 60}}}})
    _events(tmp_path, [_event('raw', 'provider_raw_response', attempt_id='one', raw_ref=raw)])
    tokens = summarize_trace_costs([tmp_path])['workspaces'][0]['provider']['tokens_by_api_format']['openai_responses']
    assert tokens['cache_creation_input_tokens'] == {'sum': None, 'recorded': 0, 'missing': 1}
    assert tokens['ordinary_input_tokens'] == {'sum': None, 'recorded': 0, 'missing': 1}
