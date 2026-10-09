from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest
from pptx import Presentation

from export import delivery
from test_delivery_training import _ready_workspace as _scaffold_workspace
from test_v1_font_pipeline import pinned_font_sources, _pin_font_manifest
from test_editable_pptx import browser_environment

_font_sources = None

@pytest.fixture(autouse=True)
def export_font_sources(monkeypatch, pinned_font_sources, browser_environment, tmp_path):
    monkeypatch.setenv("MURAL_STUDIO_DATA_DIR", pinned_font_sources["studio_data_root"])
    monkeypatch.setenv("MURAL_RESOURCE_SLOTS_DIR", str(tmp_path/'_runtime/tmp/render-slots'))
    monkeypatch.setitem(globals(), "_font_sources", pinned_font_sources)


def _ready_workspace(root, *, pages=2):
    state, artifacts, issues = _scaffold_workspace(root, pages=pages)
    _pin_font_manifest(root, _font_sources)
    delivery.DeliveryExporter(state, artifacts, issues).export(
        include_pdf=False, include_pptx=False, include_zip=False)
    return state, artifacts, issues


def _author_bytes(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*')
            if p.is_file() and not p.relative_to(root).as_posix().startswith(('delivery/', '_runtime/tmp/'))}


@pytest.mark.parametrize('format', ['html', 'pdf', 'pptx', 'zip'])
def test_on_demand_formats_are_cached_without_author_mutation(tmp_path, format):
    _ready_workspace(tmp_path)
    (tmp_path / '_state/private-context.json').write_text('{"secret":"PRIVATE-CONTEXT-SENTINEL"}')
    before = _author_bytes(tmp_path)
    first = delivery.export_on_demand(tmp_path, format)
    assert first['cached'] is False
    assert first['path'] == f'delivery/presentation.{format}'
    manifest = json.loads((tmp_path / 'delivery/manifest.json').read_text())
    assert manifest['schema'] == 'mural.delivery.v1'
    assert manifest['outputs']['portable_zip' if format == 'zip' else format]['sha256'] == first['sha256']
    target = tmp_path / first['path']
    assert target.is_file() and target.stat().st_mode & 0o004
    assert first['sha256'] == delivery.file_sha256(target)
    stat = target.stat()
    second = delivery.export_on_demand(tmp_path, format)
    assert second == {**first, 'cached': True}
    assert target.stat().st_mtime_ns == stat.st_mtime_ns
    assert _author_bytes(tmp_path) == before
    assert not list((tmp_path / '_runtime/tmp').glob('on-demand-*'))
    if format == 'html':
        text = target.read_text()
        assert 'P01' in text
        assert 'href="assets/' not in text and 'src="slides/' not in text
    elif format == 'pdf':
        assert target.read_bytes().startswith(b'%PDF-')
    elif format == 'pptx':
        assert len(Presentation(target).slides) == 2
        with zipfile.ZipFile(target) as archive:
            assert {archive.read(name) for name in archive.namelist() if name.startswith('ppt/media/')} == {
                (tmp_path / f'renders/slide_{page:02d}.png').read_bytes() for page in (1, 2)}
    else:
        with zipfile.ZipFile(target) as archive:
            assert 'present.html' in archive.namelist()
            assert not any(name.startswith(('_state/', '_trace/', '_runtime/')) for name in archive.namelist())
            assert all(b'PRIVATE-CONTEXT-SENTINEL' not in archive.read(name) for name in archive.namelist())


def test_on_demand_manifest_uses_current_speech_receipt_not_generation_memory(tmp_path, monkeypatch):
    _ready_workspace(tmp_path)
    before = _author_bytes(tmp_path)
    speech = {'status': 'partial', 'page_count': 2, 'notes_count': 1,
              'missing_pages': [2], 'omission_reason': ''}
    def sync(paths):
        assert paths.root != tmp_path
        paths.path('speech').write_text('# Speaker notes\n\n## Slide 01\n\nCurrent note.\n')
        return speech
    monkeypatch.setattr(delivery, '_sync_speech', sync)
    delivery.export_on_demand(tmp_path, 'pptx')
    manifest = json.loads((tmp_path / 'delivery/manifest.json').read_text())
    assert manifest['speech'] == speech
    assert _author_bytes(tmp_path) == before


def test_on_demand_concurrent_requests_publish_once(tmp_path, monkeypatch):
    _ready_workspace(tmp_path, pages=1)
    original = delivery.DeliveryExporter._write_pptx
    calls = []
    def write(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(delivery.DeliveryExporter, '_write_pptx', write)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: delivery.export_on_demand(tmp_path, 'pptx'), range(4)))
    assert len(calls) == 1
    assert len({row['path'] for row in results}) == 1
    assert sorted(row['cached'] for row in results) == [False, True, True, True]


def test_on_demand_refuses_stale_source_and_damaged_cache(tmp_path):
    _ready_workspace(tmp_path, pages=1)
    first = delivery.export_on_demand(tmp_path, 'pdf')
    (tmp_path / first['path']).write_bytes(b'corrupted')
    with pytest.raises(delivery.DeliveryError, match='cache'):
        delivery.export_on_demand(tmp_path, 'pdf')
    (tmp_path / 'slides/slide_01.html').write_text('<p>uninspected edit</p>')
    before = _author_bytes(tmp_path)
    with pytest.raises(delivery.DeliveryError, match='not ready|no longer ready|stale'):
        delivery.export_on_demand(tmp_path, 'zip')
    assert _author_bytes(tmp_path) == before


@pytest.mark.parametrize('relative', ['delivery', '_runtime/tmp', 'slides'])
def test_on_demand_rejects_symlink_directories(tmp_path, relative):
    root = tmp_path / 'deck'
    _ready_workspace(root, pages=1)
    path = root / relative
    moved = tmp_path / 'outside'
    if path.exists():
        path.rename(moved)
    else:
        moved.mkdir()
    path.symlink_to(moved, target_is_directory=True)
    with pytest.raises(RuntimeError, match='symlink|escapes'):
        delivery.export_on_demand(root, 'pdf')


def test_on_demand_cli_emits_one_public_result(tmp_path):
    _ready_workspace(tmp_path, pages=1)
    result = subprocess.run([sys.executable, '-m', 'export.delivery', '--workspace', str(tmp_path),
                             '--format', 'pdf'], cwd=Path(delivery.__file__).parents[1],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value['filename'] == 'presentation.pdf' and value['path'].startswith('delivery/')


def test_deferred_generation_keeps_verified_manifest_identity(tmp_path):
    workspace, artifacts, issues = _ready_workspace(tmp_path, pages=1)
    manifest = delivery.DeliveryExporter(workspace, artifacts, issues).export(
        include_pdf=False, include_pptx=False, include_zip=False)
    result = json.loads((tmp_path / 'result.json').read_text())
    assert manifest.path == ''
    assert 'delivery_manifest' not in result
    assert result['generation']['schema'] == 'mural.generation.v1'
    assert result['generation']['pages'] == list(manifest.pages)
    assert not (tmp_path / 'delivery').exists()
    assert set(manifest.outputs) == {'present_html', 'contact_sheet'}


@pytest.mark.parametrize('format', ['html', 'zip', 'eager_zip'])
def test_pinned_shared_fonts_export_offline_without_source_subset(tmp_path, monkeypatch, pinned_font_sources, format):
    from runtime.shared_objects import SharedSkillStore, load_skill_module
    from playwright.sync_api import sync_playwright
    from test_v1_font_pipeline import REPO, SKILL
    monkeypatch.setenv('MURAL_STUDIO_DATA_DIR', pinned_font_sources['studio_data_root'])
    monkeypatch.setenv('PATH', f'{Path(sys.executable).parent}:{os.environ.get("PATH", "")}')
    workspace, artifacts, issues = _ready_workspace(tmp_path, pages=1)
    ref = SharedSkillStore.for_workspace(tmp_path).pin(workspace.paths, SKILL, skill_name='mural-presenter-v1')
    fonts = load_skill_module(ref.object_root, ref.tree_sha256, 'font_bundle')
    workspace.paths.font_manifest.write_text(json.dumps(pinned_font_sources))
    (tmp_path / 'base.css').write_text(':root{--canvas-w:1600px;--canvas-h:1200px;--font-body:"Noto Sans SC";}'
        '.deck{position:absolute;width:1600px;height:1200px}.slide{width:1600px;height:1200px;}'
        'h1{font:48px var(--font-body)}')
    workspace.paths.page_html(1).write_text('<section class="slide" data-slide="1"><h1>Hello 中文。</h1></section>')
    artifacts.commit(owner_id='owner-slide', artifact_ids=['page-html-01'], source_hashes={})
    fonts.prepare_authoring_fonts(tmp_path)
    assert 'file://' in (tmp_path / 'base.css').read_text()
    assert not workspace.paths.runtime_fonts.exists()
    def renderer(_html, png, _canvas):
        from PIL import Image
        Image.new('RGB', (1600, 1200), 'white').save(png)
        return {'geometry': _canvas, 'loaded_resources': [], 'failed_resources': []}
    artifacts.render_pages([1], renderer)
    artifacts.record_inspection(1, inspection_id='font-source-inspection')
    workspace.mark_ready(1)
    # 转为共享作者字体后保留的旧 subset 历史不是本次导出依赖。
    workspace.paths.runtime_fonts.mkdir(parents=True)
    old_manifest = workspace.paths.runtime_fonts / 'manifest.json'
    old_manifest.write_text('{"license_files":["_runtime/tmp/fonts/missing-old-license.txt"]}')
    old_font_bytes = old_manifest.read_bytes()
    exporter = delivery.DeliveryExporter(workspace, artifacts, issues)
    exporter.export(include_pdf=False, include_pptx=False, include_zip=False,
                    title='Friendly & offline')
    before = _author_bytes(tmp_path)
    if format == 'eager_zip':
        manifest = exporter.export(include_pdf=False, include_pptx=False, include_zip=True,
                                   title='Friendly & offline')
        result = manifest.outputs['portable_zip']
        before.pop('result.json')
        after = _author_bytes(tmp_path)
        after.pop('result.json')
        assert after == before
        format = 'zip'
    else:
        result = delivery.export_on_demand(tmp_path, format)
        assert _author_bytes(tmp_path) == before
    assert old_manifest.read_bytes() == old_font_bytes
    assert list(workspace.paths.runtime_fonts.iterdir()) == [old_manifest]
    output = tmp_path / result['path']
    if format == 'zip':
        browser_root = tmp_path / '_runtime/tmp/offline-browser'
        with zipfile.ZipFile(output) as archive:
            names = archive.namelist()
            assert any(name.startswith('assets/fonts/') and name.endswith('.woff2') for name in names)
            assert all(b'file://' not in archive.read(name) for name in names if name.endswith(('.html', '.css')))
            archive.extractall(browser_root)
        output = browser_root / 'present.html'
    else:
        text = output.read_text()
        assert 'file://' not in text and 'data:font/woff2;base64,' in text
        import re
        licenses = json.loads(re.search(r'id="mural-font-licenses">(.*?)</script>', text, re.S).group(1))
        assert licenses and all(name.startswith('assets/fonts/') for name in licenses)
        assert 'OPEN FONT LICENSE' in ''.join(licenses.values())
    sys.path.insert(0, str(REPO / 'webui/studio'))
    from app.engine import render_env
    env = render_env({**os.environ, 'TMPDIR': str(tmp_path / '_runtime/tmp')})
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=env['PPT_SKILL_BROWSER_EXE'], env=env,
                                     headless=True, args=['--no-sandbox'])
        try:
            page = browser.new_page()
            failures = []
            page.route('https://**/*', lambda route: route.abort())
            page.route('http://**/*', lambda route: route.abort())
            page.on('requestfailed', lambda request: failures.append(request.url))
            page.goto(output.as_uri(), wait_until='load')
            page.evaluate('document.fonts.ready')
            assert page.title() == 'Friendly & offline'
            assert page.evaluate('document.documentElement.lang') == 'en'
            assert page.locator('h1').inner_text() == 'Hello 中文。'
            assert page.evaluate("Array.from(document.fonts).some(f=>f.status==='loaded' && f.family.startsWith('Deck-'))")
            assert not failures
        finally:
            browser.close()


def test_portable_css_and_svg_dependencies_resolve_relative_to_their_file(tmp_path):
    _ready_workspace(tmp_path, pages=1)
    css = tmp_path / 'assets/theme.css'
    svg = tmp_path / 'assets/icon.svg'
    expected = tmp_path / 'assets/picture.png'
    assert delivery._local_reference(css, '../assets/picture.png', tmp_path) == expected
    assert delivery._local_reference(css, 'picture.png', tmp_path) == expected
    assert delivery._local_reference(svg, 'picture.png', tmp_path) == expected
    assert delivery._local_reference(tmp_path / 'slides/slide_01.html', '../assets/picture.png', tmp_path) == expected
    with pytest.raises(delivery.DeliveryError, match='escapes'):
        delivery._local_reference(css, '../../outside.png', tmp_path)


def test_local_reference_inlines_pinned_shared_font_file_uri(tmp_path, monkeypatch):
    from runtime import shared_fonts
    from runtime.shared_fonts import SharedFontError

    font = tmp_path / 'Inter.ttf'
    font.write_bytes(b'font-bytes')
    monkeypatch.setattr(
        shared_fonts, 'font_reference_resolver',
        lambda root: lambda reference: (font, '0' * 64),
    )
    assert delivery._local_reference(
        tmp_path / 'base.css', font.as_uri(), tmp_path) == font.resolve()
    monkeypatch.setattr(
        shared_fonts, 'font_reference_resolver',
        lambda root: lambda reference: (_ for _ in ()).throw(SharedFontError('not pinned')),
    )
    with pytest.raises(delivery.DeliveryError, match='external runtime resource'):
        delivery._local_reference(tmp_path / 'base.css', 'file:///etc/passwd', tmp_path)


def test_private_font_owner_is_checked_even_when_export_is_cached(tmp_path, monkeypatch, pinned_font_sources):
    _ready_workspace(tmp_path, pages=1)
    data = Path(pinned_font_sources['studio_data_root'])
    monkeypatch.setenv('MURAL_STUDIO_DATA_DIR', str(data))
    monkeypatch.setenv('MURAL_USER_ID', 'alice')
    row = pinned_font_sources['source_faces'][0]
    private = data / 'users/alice/_shared/fonts/objects' / Path(row['object_ref']).name
    private.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(data / row['object_ref'], private)
    custom = {**row, 'id': 'user-custom', 'scope': 'user', 'owner_user_id': 'alice',
              'object_ref': private.relative_to(data).as_posix(), 'weight': '400'}
    (tmp_path / '_state/font-manifest.json').write_text(json.dumps({**pinned_font_sources, 'custom_fonts': [custom]}))
    first = delivery.export_on_demand(tmp_path, 'pdf')
    before = _author_bytes(tmp_path)
    monkeypatch.setenv('MURAL_USER_ID', 'bob')
    with pytest.raises(RuntimeError, match='different WebUI user'):
        delivery.export_on_demand(tmp_path, 'pdf')
    assert _author_bytes(tmp_path) == before
    assert (tmp_path / first['path']).is_file()
