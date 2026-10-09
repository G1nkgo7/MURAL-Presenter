from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from export import delivery, editable, native_pdf
from runtime.config import load_runtime_config
from runtime.production import ProductionSettings
from test_delivery_training import _ready_workspace
from test_v1_font_pipeline import pinned_font_sources, _pin_font_manifest
from test_editable_pptx import browser_environment, sample_html, websocket_server


@pytest.fixture
def completed(tmp_path, monkeypatch, pinned_font_sources):
    monkeypatch.setenv('MURAL_STUDIO_DATA_DIR',pinned_font_sources['studio_data_root'])
    state,artifacts,issues=_ready_workspace(tmp_path,pages=1)
    _pin_font_manifest(tmp_path,pinned_font_sources)
    delivery.DeliveryExporter(state,artifacts,issues).export(include_pdf=False,include_pptx=False,include_zip=False)
    return state,artifacts


@pytest.fixture
def completed_pair(tmp_path, monkeypatch, pinned_font_sources):
    monkeypatch.setenv('MURAL_STUDIO_DATA_DIR',pinned_font_sources['studio_data_root'])
    config = load_runtime_config()
    config['resources'].update(directory=str(tmp_path/'render-slots'), render=1)
    config_path = tmp_path/'runtime.json'
    config_path.write_text(json.dumps(config), encoding='utf-8')
    monkeypatch.setenv('MURAL_RUNTIME_CONFIG',str(config_path))
    monkeypatch.setenv('MURAL_RESOURCE_SLOTS_DIR',str(tmp_path/'ignored-env-slots'))
    monkeypatch.setenv('RENDER_CONCURRENCY','99')
    monkeypatch.setenv('SKILL_COMMAND_TIMEOUT_S','30')
    assert ProductionSettings.from_environment().render_concurrency == 1
    roots = [tmp_path/'deck-a',tmp_path/'deck-b']
    for root in roots:
        state,artifacts,issues = _ready_workspace(root,pages=1)
        _pin_font_manifest(root,pinned_font_sources)
        delivery.DeliveryExporter(state,artifacts,issues).export(include_pdf=False,include_pptx=False,include_zip=False)
    return roots


def _wait_for_render_ticket(directory, future):
    deadline = time.monotonic() + 30
    while not list(directory.glob('render-*.queue/*.ticket')):
        if future.done():
            future.result()
            raise AssertionError('second Deck bypassed the shared render queue')
        assert time.monotonic() < deadline, 'second Deck never reached the shared render queue'
        time.sleep(.02)


def _fake_pptx(target):
    target.write_bytes(b'fake-pptx-test-only')
    return {'schema':editable.REPORT_SCHEMA,'summary':{'native_text':1,'local_raster_fallbacks':0}}


@pytest.mark.parametrize('format',['pptx-editable','pdf'])
def test_editable_exports_share_render_capacity_across_decks(tmp_path,monkeypatch,completed_pair,format):
    first_entered, release_first = threading.Event(), threading.Event()
    calls = []
    def convert(documents,target,canvas,**options):
        root = next(root for root in completed_pair if target.is_relative_to(root))
        calls.append(root)
        if root == completed_pair[0]:
            first_entered.set()
            assert release_first.wait(45)
        return _fake_pptx(target)
    monkeypatch.setattr(editable if format=='pptx-editable' else native_pdf,
        'write_editable_pptx' if format=='pptx-editable' else 'write_native_pdf',convert)
    before = [author_bytes(root) for root in completed_pair]
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(delivery.export_on_demand,completed_pair[0],format)
        try:
            assert first_entered.wait(45)
            second = pool.submit(delivery.export_on_demand,completed_pair[1],format)
            _wait_for_render_ticket(tmp_path/'render-slots',second)
            assert calls == completed_pair[:1] and not second.done()
        finally:
            release_first.set()
        assert first.result(timeout=10)['cached'] is False
        assert second.result(timeout=10)['cached'] is False
    assert calls == completed_pair
    assert [author_bytes(root) for root in completed_pair] == before
    from runtime.production import ProductionSettings, ResourceSlots
    with ResourceSlots(completed_pair[1],ProductionSettings.from_environment()).acquire('render'):
        assert delivery.export_on_demand(completed_pair[0],format)['cached'] is True
    assert calls == completed_pair


@pytest.mark.parametrize('format',['pptx-editable','pdf'])
def test_editable_render_queue_timeout_preserves_source_and_can_retry(tmp_path,monkeypatch,completed_pair,format):
    from runtime import production
    clock_offset = [0.0]
    monkeypatch.setattr(production,'time',SimpleNamespace(
        monotonic=lambda: time.monotonic() + clock_offset[0],sleep=time.sleep))
    first_entered, release_first = threading.Event(), threading.Event()
    calls = []
    def convert(documents,target,canvas,**options):
        root = next(root for root in completed_pair if target.is_relative_to(root))
        calls.append(root)
        if root == completed_pair[0]:
            first_entered.set()
            assert release_first.wait(45)
        return _fake_pptx(target)
    monkeypatch.setattr(editable if format=='pptx-editable' else native_pdf,
        'write_editable_pptx' if format=='pptx-editable' else 'write_native_pdf',convert)
    before = author_bytes(completed_pair[1])
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(delivery.export_on_demand,completed_pair[0],format)
        try:
            assert first_entered.wait(45)
            second = pool.submit(delivery.export_on_demand,completed_pair[1],format)
            _wait_for_render_ticket(tmp_path/'render-slots',second)
            # 真实票据入队后推进槽时钟，避免把字体准备耗时当作排队超时。
            clock_offset[0] = 60
            with pytest.raises(delivery.DeliveryError,match='render queue timeout'):
                second.result(timeout=12)
            assert calls == completed_pair[:1]
            assert not (completed_pair[1]/'delivery/presentation.editable.pptx').exists()
            assert not (completed_pair[1]/'delivery/presentation.pdf').exists()
            assert not (completed_pair[1]/'delivery/presentation.editable.editability.json').exists()
            assert not list((completed_pair[1]/'_runtime/tmp').glob('on-demand-*'))
            assert not list((tmp_path/'render-slots').glob('render-*.queue/*.ticket'))
            assert author_bytes(completed_pair[1]) == before
        finally:
            release_first.set()
        assert first.result(timeout=10)['cached'] is False
    clock_offset[0] = 0
    assert delivery.export_on_demand(completed_pair[1],format)['cached'] is False
    assert calls == completed_pair and author_bytes(completed_pair[1]) == before


def author_bytes(root):
    return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()
            and not p.relative_to(root).as_posix().startswith(('delivery/','_runtime/tmp/'))}


def test_editable_snapshot_cache_and_report_integrity(tmp_path,monkeypatch,completed):
    calls=[]
    def convert(documents,target,canvas,**options):
        calls.append(documents)
        assert len(documents)==1 and 'P01' in documents[0][1]
        assert 'data:text/css;base64,' in documents[0][1]
        assert not any('PRIVATE-SENTINEL' in text for _,text in documents)
        target.write_bytes(b'fake-pptx-test-only')
        return {'schema':editable.REPORT_SCHEMA,'summary':{'native_text':1,'local_raster_fallbacks':0}}
    monkeypatch.setattr(editable,'write_editable_pptx',convert)
    (tmp_path/'_state/private-context.json').write_text('PRIVATE-SENTINEL')
    before=author_bytes(tmp_path)
    first=delivery.export_on_demand(tmp_path,'pptx-editable')
    assert first['format']=='pptx-editable' and first['filename']=='presentation.editable.pptx'
    assert first['path']=='delivery/presentation.editable.pptx'
    report=first['editability_report']
    assert report['path']=='delivery/presentation.editable.editability.json'
    assert report['sha256']==delivery.file_sha256(tmp_path/report['path'])
    assert delivery.export_on_demand(tmp_path,'pptx-editable')=={**first,'cached':True}
    assert len(calls)==1 and author_bytes(tmp_path)==before
    assert not list((tmp_path/'_runtime/tmp').glob('on-demand-*'))
    (tmp_path/report['path']).write_text('{}')
    with pytest.raises(delivery.DeliveryError,match='cache editability report'):
        delivery.export_on_demand(tmp_path,'pptx-editable')
    assert author_bytes(tmp_path)==before


@pytest.mark.parametrize('drift',['html','css','render'])
def test_editable_export_keeps_ready_resource_receipt_gate(tmp_path,monkeypatch,completed,drift):
    state,_=completed
    def unexpected(*args,**kwargs):
        raise AssertionError('converter must not run before the existing source gates')
    monkeypatch.setattr(editable,'write_editable_pptx',unexpected)
    path={'html':state.paths.page_html(1),'css':tmp_path/'base.css','render':state.paths.page_png(1)}[drift]
    path.write_bytes(path.read_bytes()+b' changed')
    with pytest.raises((RuntimeError,ValueError)):
        delivery.export_on_demand(tmp_path,'pptx-editable')
    assert not (tmp_path/'delivery/presentation.editable.pptx').exists()


def test_editability_report_symlink_is_rejected(tmp_path,monkeypatch,completed):
    (tmp_path/'delivery').mkdir(exist_ok=True)
    destination=tmp_path/'delivery/presentation.editable.editability.json'
    destination.symlink_to(tmp_path/'result.json')
    original=(tmp_path/'result.json').read_bytes()
    with pytest.raises(delivery.DeliveryError,match='symlink'):
        delivery.export_on_demand(tmp_path,'pptx-editable')
    assert (tmp_path/'result.json').read_bytes()==original


def test_real_completed_snapshot_exports_editable_and_reuses_cache(tmp_path,completed,browser_environment):
    from playwright.sync_api import sync_playwright
    from runtime.shared_objects import workspace_skill_module
    from qc.routing import IssueLedger
    state,artifacts=completed
    html=sample_html(1600,1200).replace('"DejaVu Sans",sans-serif','var(--font-body)').replace('<head>','<head><link rel="stylesheet" href="../base.css">')
    state.paths.page_html(1).write_text(html)
    with (tmp_path/'base.css').open('a') as handle:
        handle.write('\n:root { --font-body: "Noto Sans SC"; }\n')
    workspace_skill_module(tmp_path,'font_bundle').prepare_authoring_fonts(tmp_path)
    artifacts.commit(owner_id='owner-slide',artifact_ids=['page-html-01'],source_hashes={})
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path=browser_environment['PPT_SKILL_BROWSER_EXE'],env=browser_environment,headless=True,args=['--no-sandbox'])
        try:
            page=browser.new_page(viewport={'width':1600,'height':1200})
            def render(html_path,png,canvas):
                page.goto(html_path.as_uri())
                page.evaluate('document.fonts.ready')
                page.screenshot(path=str(png))
                return {'geometry':canvas,'loaded_resources':[],'failed_resources':[]}
            artifacts.render_pages([1],render)
        finally:
            browser.close()
    artifacts.record_inspection(1,inspection_id='synthetic-editable-inspection')
    state.mark_ready(1)
    delivery.DeliveryExporter(state,artifacts,IssueLedger(tmp_path)).export(include_pdf=False,include_pptx=False,include_zip=False)
    before=author_bytes(tmp_path)
    first=delivery.export_on_demand(tmp_path,'pptx-editable')
    assert first['summary']['native_tables']==1 and first['summary']['native_text']>=3
    assert editable.inspect_pptx(tmp_path/first['path'])['tables']==1
    assert delivery.export_on_demand(tmp_path,'pptx-editable')=={**first,'cached':True}
    assert author_bytes(tmp_path)==before


def test_real_export_cli_rejects_websocket_without_connecting(tmp_path,completed,browser_environment,websocket_server):
    from qc.routing import IssueLedger
    state,artifacts = completed
    endpoint, requests = websocket_server
    html_path = state.paths.page_html(1)
    html_path.write_text(html_path.read_text().replace('</body>',
        f'<script>new WebSocket({json.dumps(endpoint)})</script></body>'))
    artifacts.commit(owner_id='owner-slide',artifact_ids=['page-html-01'],source_hashes={})
    png_bytes = state.paths.page_png(1).read_bytes()
    def synthetic_render(html,png,canvas):
        png.write_bytes(png_bytes)
        return {'geometry':canvas,'loaded_resources':[],'failed_resources':[]}
    artifacts.render_pages([1],synthetic_render)
    artifacts.record_inspection(1,inspection_id='synthetic-websocket-source')
    state.mark_ready(1)
    delivery.DeliveryExporter(state,artifacts,IssueLedger(tmp_path)).export(include_pdf=False,include_pptx=False,include_zip=False)
    before = author_bytes(tmp_path)
    result = subprocess.run([sys.executable,'-m','export.delivery','--workspace',str(tmp_path),'--format','pptx-editable'],
        cwd=Path(delivery.__file__).resolve().parents[1],env={**os.environ,**browser_environment},
        capture_output=True,text=True,timeout=60)
    assert result.returncode == 1 and not result.stdout, (result.stdout,result.stderr)
    error = json.loads(result.stderr.strip().splitlines()[-1])
    assert error['error'] == 'delivery_export_failed' and error['code'] == 'export_source_invalid'
    assert requests == []
    assert not (tmp_path/'delivery/presentation.editable.pptx').exists()
    assert not (tmp_path/'delivery/presentation.editable.editability.json').exists()
    assert not list((tmp_path/'_runtime/tmp').glob('on-demand-*'))
    assert author_bytes(tmp_path) == before
