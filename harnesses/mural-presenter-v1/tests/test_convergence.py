from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import json
import ast
import os
from pathlib import Path
import subprocess
import threading
import tempfile
from types import SimpleNamespace

import pytest

from test_orchestration_manager import drain
from orchestration.manager import OrchestrationError, OrchestrationManager
from qc.extrinsic import CriticClient, ExternalVisualCritic
from qc.review_hook import InspectionClosed
from runtime.production import ProductionToolServices
from runtime.tools import ToolContext, ToolExecutionError, ToolContractError, WriteScope
from test_extrinsic_critic import (
    _write_png, _inspect_tool, _response, _tool, _text, _run_owner, ScriptedTransport,
)
from runtime.tools import ToolRuntime
from runtime.recovery import tool_boundary
from state.artifacts import ArtifactStore
from state.workspace import WorkspaceState


def test_same_pixel_feedback_is_reused_without_escalation_and_survives_resume(tmp_path):
    png_sha = _write_png(tmp_path, 'renders/slide_01.png')
    closed = InspectionClosed('owner', 'slide', 'i-1', 'h', png_sha,
        'renders/slide_01.png', str(tmp_path), page=1, page_uid='uid-1')
    records, calls = [], []
    def invoke(*_):
        calls.append(True)
        return {'body': '问题｜标题截断\n定位｜右上角\n改法｜调整宽度', 'stop_reason': 'end_turn'}
    client = CriticClient(invoker=invoke, critic_model='test')
    with ThreadPoolExecutor(1) as executor:
        critic = ExternalVisualCritic(client=client, executor=executor,
            result_sink=lambda result, disposition: records.append({**result.to_audit_dict(), 'disposition': disposition}))
        assert critic.review(closed)['completed']
        repeated = critic.review(closed)
        assert repeated['cache_hit'] and '标题截断' in repeated['body']
        assert not {'inject', 'handoff', 'verdict', 'actionable'} & repeated.keys()
        resumed = ExternalVisualCritic(client=client, executor=executor, prior_records=json.loads(json.dumps(records)))
        assert resumed.review(closed)['cache_hit']
        assert len(calls) == 1
        # 同页同用途可复用；不同页的用途和检查问题不同，不能仅凭像素相同复用。
        again = InspectionClosed('owner', 'slide', 'i-2', 'h2', png_sha,
            'renders/slide_01.png', str(tmp_path), page=1, page_uid='uid-1')
        assert resumed.review(again)['reused_from']['inspection_id'] == 'i-1'
        other = InspectionClosed('owner-2', 'slide', 'i-3', 'h', png_sha,
            'renders/slide_01.png', str(tmp_path), page=2, page_uid='uid-2')
        reused = resumed.review(other)
        assert not reused['cache_hit'] and reused['page_uid'] == 'uid-2'
        assert len(calls) == 2


def test_owner_may_finish_after_inspect_feedback_without_automatic_handoff(tmp_path):
    sha = _write_png(tmp_path, 'renders/slide_01.png')
    tools = ToolRuntime()
    tools.register(_inspect_tool(tmp_path, 'renders/slide_01.png', sha))
    transport = ScriptedTransport([_response(_tool('inspect-1', 'inspect', {'page': 1})),
        _response(_text('ready')), _response(_text('I disagree; ready'))])
    client = CriticClient(invoker=lambda *_: {'body': '问题｜标题截断\n定位｜页首'},
        critic_model='test')
    with ThreadPoolExecutor(1) as executor:
        critic = ExternalVisualCritic(client=client, executor=executor)
        result = _run_owner(tmp_path, transport, tools, critic)
    assert result.final_text == 'ready' and result.stop_reason == 'end_turn'
    assert result.turns == 2
    assert 'review_handoff' not in result.to_dict()
    assert tool_boundary(result.checkpoint.messages).is_closed


def test_new_critic_model_on_same_inspection_does_not_reuse_response(tmp_path):
    sha = _write_png(tmp_path, 'renders/slide_01.png')
    closed = InspectionClosed('owner', 'slide', 'inspection', 'html', sha,
        'renders/slide_01.png', str(tmp_path), page=1, page_uid='p1')
    records, models = [], []
    with ThreadPoolExecutor(1) as executor:
        for model in ('critic-a', 'critic-b'):
            client = CriticClient(invoker=lambda *_: {'body': '问题｜截断\n定位｜标题'}, critic_model=model)
            critic = ExternalVisualCritic(client=client, executor=executor, prior_records=records,
                result_sink=lambda result, disposition: records.append({**result.to_audit_dict(), 'disposition': disposition}))
            feedback = critic.review(closed)
            assert not feedback['cache_hit'] and feedback['completed']
            models.append(feedback['critic_model'])
    assert models == ['critic-a', 'critic-b'] and len(records) == 2


@pytest.mark.parametrize('outcomes', [('failed', 'failed'), ('failed', 'succeeded', 'failed')])
def test_explicit_research_followup_is_not_blocked_by_historical_failures(tmp_path, outcomes):
    calls, owners = [], []
    def run(task, _):
        calls.append(task.task_id)
        owners.append(task.owner_id)
        if task.task_id != 'recovered' and outcomes[int(task.task_id.rsplit('-', 1)[-1])] == 'failed':
            raise RuntimeError('external source unavailable')
        return {'status': 'completed', 'evidence': 'current source'}
    for attempt, outcome in enumerate(outcomes):
        manager = OrchestrationManager(tmp_path, deck_id='d', request_key='request-1')
        manager.register_runner('research', run)
        try:
            wave = manager.submit_tasks([{'task_id': f'research-{attempt}', 'role': 'research', 'group_id': 'facts', 'output_path': 'research/facts.md', 'goal': f'new wording {attempt}'}])
            drain(manager)
            assert wave['tasks'][0]['task_id'] in manager.snapshot()['tasks']
            assert manager.task(f'research-{attempt}').status.value == outcome
        finally:
            manager.close()
    manager = OrchestrationManager(tmp_path, deck_id='d', request_key='request-1')
    manager.register_runner('research', run)
    try:
        spec = {'task_id': 'recovered', 'role': 'research', 'group_id': 'facts', 'output_path': 'research/facts.md', 'goal': 'Source restored; continue from existing evidence and verify the remaining question'}
        receipt = manager.submit_tasks([spec])
        drain(manager)
        task = manager.task('recovered')
        assert receipt['new_task_ids'] == ['recovered']
        assert task.status.value == 'succeeded' and task.output_path == 'research/facts.md'
        assert task.attempt == len(outcomes) + 1 and len(set(owners)) == 1
        assert manager.submit_tasks([spec])['reused_task_ids'] == ['recovered']
        assert calls == [*(f'research-{n}' for n in range(len(outcomes))), 'recovered']
        manager.register_runner('research', lambda *_: {})
        manager.submit_tasks([{'task_id': 'independent', 'role': 'research', 'output_path': 'research/other.md'}])
        drain(manager)
        assert manager.task('independent').status.value == 'succeeded'
    finally:
        manager.close()
    revised = OrchestrationManager(tmp_path, deck_id='d', request_key='actual-new-user-request')
    revised.register_runner('research', lambda *_: {'status': 'completed'})
    try:
        wave = revised.submit_tasks([{'task_id': 'new-request', 'role': 'research'}])
        drain(revised)
        assert revised.task('new-request').status.value == 'succeeded'
    finally:
        revised.close()


def _web_backend():
    backend = object.__new__(ProductionToolServices)
    backend.run_spec = SimpleNamespace(authoring_profile='general')
    backend.settings = SimpleNamespace(web_extract_timeout_s=5, web_extract_max_bytes=4*1024*1024)
    backend.environment = {'SERPER_API_KEY': 'test-only'}
    backend.slots = SimpleNamespace(acquire=lambda *_a, **_kw: nullcontext())
    backend._search_lock = threading.Lock()
    backend._web_results = {}
    return backend


def test_denied_read_returns_real_scope_without_expanding_permission():
    context = ToolContext(owner_id='slide-owner', role='slide', read_scopes=(
        WriteScope('base.css', recursive=False), WriteScope('slides/slide_01.html', recursive=False)))
    with pytest.raises(ToolContractError) as failure:
        _web_backend().read_file(context, {'path': 'deck.md'})
    assert 'base.css' in str(failure.value) and 'slides/slide_01.html' in str(failure.value)
    assert not context.may_read('deck.md')


def test_search_reuses_real_result_and_distinguishes_image_query(monkeypatch):
    calls = []
    def post(*args, **kw):
        calls.append(args[0])
        return SimpleNamespace(raise_for_status=lambda: None,
            json=lambda: {'organic': [{'title': 'source', 'link': 'https://example.test'}], 'images': []})
    monkeypatch.setattr('requests.post', post)
    backend, context = _web_backend(), ToolContext(owner_id='r', role='research')
    first = backend.web_search(context, {'query': 'exact query'})
    again = backend.web_search(context, {'query': 'exact query'})
    assert again['cached'] and again['results'] == first['results']
    backend.web_search(context, {'query': 'exact query', 'search_type': 'images'})
    assert len(calls) == 2


@pytest.mark.parametrize("locale,expected", [
    ({}, {}), ({"language": "auto", "region": "auto"}, {}),
    ({"language": "fr", "region": "ca"}, {"hl": "fr", "gl": "ca"}),
    ({"language": "en"}, {"hl": "en"}),
])
def test_search_locale_is_explicit_or_left_to_service(monkeypatch, locale, expected):
    bodies = []
    def post(_url, **kwargs):
        bodies.append(kwargs['json'])
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'organic': []})
    monkeypatch.setattr('requests.post', post)
    backend = _web_backend()
    backend.web_search(ToolContext(owner_id='r', role='research'), {'query': '中文问题 Canada', **locale})
    assert bodies == [{'q': '中文问题 Canada', 'num': 5, **expected}]


def test_extract_pdf_and_retry_failed_request_without_poisoning_cache(tmp_path, monkeypatch):
    import pymupdf
    from state.workspace import WorkspaceState
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), 'Rain garden evidence')
        data = pdf.tobytes()
    calls = []
    def run(*args, **kw):
        calls.append(args[0])
        return subprocess.CompletedProcess(args[0], 28 if len(calls)==1 else 0,
            stdout=b'' if len(calls)==1 else data,
            stderr=b'curl: (28) operation timed out' if len(calls)==1 else b'\nhttps://example.test/source.pdf')
    monkeypatch.setattr('runtime.production.subprocess.run', run)
    backend, context = _web_backend(), ToolContext(
        owner_id='r', role='research',
        read_scopes=(WriteScope('sources/web', recursive=True),),
    )
    backend.workspace = WorkspaceState(tmp_path)
    with pytest.raises(ToolExecutionError, match='timed out') as failed:
        backend.web_extract(context, {'url': 'https://example.test/source.pdf'})
    assert failed.value.code == 'web_extract_timeout'
    assert failed.value.diagnostics['retry_recommended'] == 'once'
    assert not list((tmp_path / '_trace/web-extract').glob('*'))
    result = backend.web_extract(context, {'url': 'https://example.test/source.pdf'})
    assert result['format'] == 'pdf' and 'Rain garden evidence' in result['content']
    continued = backend.read_file(context, {'path': result['source_ref']})
    assert 'Rain garden evidence' in continued['content']
    assert continued['path'] == result['source_ref'] and '_trace/' not in str(result)
    resumed = _web_backend()
    resumed.workspace = WorkspaceState(tmp_path)
    assert resumed.web_extract(context, {'url': 'https://example.test/source.pdf'})['cached']
    assert len(calls) == 2
    assert calls[0][-2] == '--'


def test_image_http_error_keeps_service_message_and_excludes_private_fields(tmp_path, monkeypatch):
    import requests
    backend = _web_backend()
    backend._image_failures = {}
    backend.settings.image_generation_max_failure_streak = 2
    backend.settings.image_generation_timeout_s = 10
    backend.workspace = WorkspaceState(tmp_path)
    backend.artifacts = ArtifactStore(tmp_path, workspace_state=backend.workspace)
    key = 'image-private-key-fixture'
    backend.environment.update(IMAGE_API_KEY=key, IMAGE_BASE_URL='https://image.test/v1', IMAGE_MODEL='configured-image')
    response = requests.Response()
    response.status_code = 400
    response.url = 'https://image.test/v1/images/generations'
    response._content = json.dumps({'error': {'message': '许可证不存在 '+key, 'type': 'invalid_request_error',
        'thinking_signature': 'hidden-signature-fixture', 'request': 'hidden-request-fixture'}}).encode()
    monkeypatch.setattr('requests.post', lambda *_a, **_kw: response)
    with pytest.raises(ToolExecutionError) as error:
        backend.image_generate(ToolContext(owner_id='i', role='image', write_scopes=(WriteScope('assets'),)), {'prompt': 'test'})
    message = str(error.value)
    assert '许可证不存在' in message and '400' in message
    assert key in message
    assert 'hidden-signature-fixture' not in message and 'hidden-request-fixture' not in message
    assert not list(tmp_path.rglob('*.png'))
    assert {row["status"] for row in json.loads(backend.artifacts.path.read_text())["transactions"].values()} == {"aborted"}


@pytest.mark.parametrize("mode", ["RGB", "CMYK"])
def test_generated_jpeg_is_stored_as_true_png_and_retry_reuses_bytes(tmp_path, monkeypatch, mode):
    import base64
    from io import BytesIO
    from PIL import Image
    from state.workspace import WorkspaceState
    encoded = BytesIO()
    Image.new(mode, (40, 30)).save(encoded, format="JPEG")
    calls = []
    def post(*args, **kwargs):
        calls.append(True)
        reserved = backend.artifacts.catalog()["images"][0]
        assert reserved["owner_id"] == "image" and reserved["status"] == "declared"
        assert not (tmp_path / reserved["path"]).exists()
        assert {row["status"] for row in json.loads(backend.artifacts.path.read_text())["transactions"].values()} == {"open"}
        return SimpleNamespace(raise_for_status=lambda: None,
            json=lambda: {"data": [{"b64_json": base64.b64encode(encoded.getvalue()).decode()}]})
    monkeypatch.setattr("requests.post", post)
    backend = _web_backend()
    backend._image_failures = {}
    backend.settings.image_generation_max_failure_streak = 2
    backend.settings.image_generation_timeout_s = 10
    backend.workspace = WorkspaceState(tmp_path)
    backend.artifacts = ArtifactStore(tmp_path, workspace_state=backend.workspace)
    backend.environment.update(IMAGE_API_KEY="fixture", IMAGE_BASE_URL="https://image.test/v1")
    context = ToolContext(owner_id="image", role="image", read_scopes=(WriteScope("assets"),), write_scopes=(WriteScope("assets"),))
    first = backend.image_generate(context, {"prompt": "fixture"})
    target = tmp_path / first["path"]
    assert target.suffix == ".png" and target.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(target) as image:
        assert image.format == "PNG" and image.size == (40, 30)
    second = backend.image_generate(context, {"prompt": "fixture"})
    assert second["reused"] and second["sha256"] == first["sha256"]
    assert second["artifact_id"] == first["artifact_id"]
    assert backend.artifacts.artifact(first["artifact_id"]).owner_id == "image"
    other_context = ToolContext(owner_id="orchestrator", role="orchestrator", read_scopes=(WriteScope("assets"),),
        write_scopes=(WriteScope("assets"),))
    other = backend.image_generate(other_context, {"prompt": "fixture"})
    assert other["reused"] and other["owner_id"] == "image" and other["artifact_id"] == first["artifact_id"]
    Image.new("RGB", (40, 30), "red").save(target)
    with pytest.raises(ToolExecutionError) as failure:
        backend.image_generate(other_context, {"prompt": "fixture"})
    assert failure.value.code == "asset_content_changed"
    assert calls == [True]


def test_pdf_full_text_is_cached_while_display_is_truncated(tmp_path, monkeypatch):
    import pymupdf
    from state.workspace import WorkspaceState
    with pymupdf.open() as pdf:
        for index in range(51):
            pdf.new_page().insert_text((72, 72), f'Source text page {index + 1}')
        data = pdf.tobytes()
    calls = []
    def run(*args, **kw):
        calls.append(args[0])
        return subprocess.CompletedProcess(args[0], 0, stdout=data, stderr=b'\nhttps://example.test/long.pdf')
    monkeypatch.setattr('runtime.production.subprocess.run', run)
    backend, context = _web_backend(), ToolContext(
        owner_id='r', role='research',
        read_scopes=(WriteScope('sources/web', recursive=True),),
    )
    backend.workspace = WorkspaceState(tmp_path)
    result = backend.web_extract(context, {'url': 'https://example.test/long.pdf', 'max_chars': 1000})
    assert result['truncated'] and len(result['content']) == 1000
    assert '[page 51]' not in result['content']
    tail = backend.read_file(context, {
        'path': result['source_ref'], 'query': 'Source text page 51'
    })
    assert '[page 51]' in tail['content'] and 'Source text page 51' in tail['content']
    metadata = json.loads(next((tmp_path / '_trace/web-extract').glob('*.json')).read_text())
    assert result['page_count'] == 51 and metadata['characters'] > len(result['content'])
    complete = backend.web_extract(context,
        {'url': 'https://example.test/long.pdf', 'max_chars': 30000})
    assert complete['cached']
    assert complete['requested_max_chars'] == 30000 and complete['prompt_window_clamped']
    assert (not complete['truncated']) or (
        complete['truncated'] and complete['prompt_window_clamped']
        and len(complete['content']) == 12000)
    assert len(calls) == 1


def test_browser_scratch_is_private_and_cleaned_even_if_chromium_leaves_residue(tmp_path, monkeypatch):
    import playwright.sync_api
    from contextlib import contextmanager
    import time
    source = Path(__file__).resolve().parents[3]/'skills/mural-presenter-v1/scripts/_internal/render_deck.py'
    tree = ast.parse(source.read_text())
    renderer = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'DeckRenderer')
    renderer.body = [node for node in renderer.body if isinstance(node, ast.FunctionDef) and node.name in ('start', 'close', '_timed')]
    namespace = {'tempfile': tempfile, 'os': os, 'ensure_browser_libs': lambda: None,
        'DEVICE_SCALE': 1, 'GOTO_TIMEOUT_MS': 100, 'contextmanager': contextmanager, 'time': time}
    exec(compile(ast.Module(body=[renderer], type_ignores=[]), str(source), 'exec'), namespace)
    monkeypatch.setenv('TMPDIR', str(tmp_path))
    monkeypatch.delenv('MURAL_RENDER_BROWSER_ENDPOINT', raising=False)
    roots = []
    page = SimpleNamespace(set_default_timeout=lambda *_: None, on=lambda *_: None)
    def launch(**kw):
        parent = Path(kw['env']['TMPDIR'])
        roots.append(parent)
        (parent/'.org.chromium.Chromium.fixture').write_bytes(b'chromium scratch')
        return SimpleNamespace(close=lambda: None, new_context=lambda **_: SimpleNamespace(
            new_page=lambda: page, route=lambda *_: None, route_web_socket=lambda *_: None))
    pw = SimpleNamespace(chromium=SimpleNamespace(launch=launch), stop=lambda: None)
    monkeypatch.setattr(playwright.sync_api, 'sync_playwright', lambda: SimpleNamespace(start=lambda: pw))
    instances = []
    for _ in range(2):
        r = namespace['DeckRenderer']()
        r.meta = {'timings_s': {}}
        r.deck_w, r.deck_h = 1600, 900
        r._on_console = r._on_pageerror = lambda *_: None
        r._request_finished = lambda *_: None
        r._serve_file = lambda *_: None
        r._serve_remote = lambda *_: None
        r.start()
        instances.append(r)
    assert roots[0] != roots[1]
    instances[0].close()
    assert not roots[0].exists() and roots[1].exists()
    instances[1].close()
    assert not list(tmp_path.iterdir())
