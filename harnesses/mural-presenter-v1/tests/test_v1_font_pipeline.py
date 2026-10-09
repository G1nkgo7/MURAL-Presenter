"""v1 font-pipeline tests: mixture-v2 parity + normalize-does-not-eat-fonts.

These exercise ``font_bundle`` directly (the module that owns the allowlist,
the release/route audits, and ``normalize_font_family_declarations``). The
frozen-dataclass ``Face`` requires the module to be imported as a real package
member, so it is loaded the same way the v1.0 harness does.

Coverage: exact aliases, unchanged authored fallback stacks, real font
availability, authorized user fonts and portable glyph/weight parity.
"""
from __future__ import annotations

import hashlib
import errno
import fcntl
import importlib
import importlib.util
import json
import os
import re
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[3]
SKILL = REPO / "skills/mural-presenter-v1"
INTERNAL = SKILL / "scripts/_internal"
RELEASE_FONT_DIR = REPO / "fonts"


def _load_font_bundle():
    package_name = "_mural_presenter_v1_font_tests"
    module_name = f"{package_name}.font_bundle"
    if module_name in sys.modules:
        return sys.modules[module_name]
    package = types.ModuleType(package_name)
    package.__path__ = [str(INTERNAL)]
    package.__package__ = package_name
    sys.modules[package_name] = package
    spec = importlib.util.spec_from_file_location(module_name, INTERNAL / "font_bundle.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


font_bundle = _load_font_bundle()


def test_font_bundle_lock_leaves_no_runtime_residue(tmp_path):
    with font_bundle._workspace_bundle_lock(tmp_path):
        lock_files = list((tmp_path / "_state/locks").glob("font-bundle.lock"))
        assert len(lock_files) == 1
        identity = lock_files[0].stat().st_ino
        assert not (tmp_path / "_runtime").exists()
    with font_bundle._workspace_bundle_lock(tmp_path):
        assert lock_files[0].stat().st_ino == identity
    assert lock_files[0].is_file()


def test_font_bundle_lock_serializes_workspace_but_not_export_staging(tmp_path):
    workspace = tmp_path / "workspace"
    staging = tmp_path / "export-staging"
    attempting = threading.Event()
    entered = threading.Event()

    def acquire(root, signal=False):
        if signal:
            attempting.set()
        with font_bundle._workspace_bundle_lock(root):
            if signal:
                entered.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        with font_bundle._workspace_bundle_lock(workspace):
            same_workspace = pool.submit(acquire, workspace, True)
            assert attempting.wait(2)
            assert not entered.wait(0.1)
            pool.submit(acquire, staging).result(timeout=2)
            assert not entered.is_set()
        same_workspace.result(timeout=2)
    assert entered.is_set()
    for root in (workspace, staging):
        assert (root / "_state/locks/font-bundle.lock").is_file()
        assert not (root / "_runtime").exists()


def _font_lock_clock(monkeypatch):
    clock, sleeps = [0.0], []
    def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay
    monkeypatch.setattr(font_bundle, "time", types.SimpleNamespace(
        monotonic=lambda: clock[0], sleep=sleep), raising=False)
    return clock, sleeps


def test_font_bundle_lock_serializes_processes(tmp_path):
    import multiprocessing
    context = multiprocessing.get_context("fork")
    received, sent = context.Pipe(duplex=False)
    def acquire():
        with sent:
            sent.send("waiting")
            with font_bundle._workspace_bundle_lock(tmp_path, timeout_s=5):
                sent.send("acquired")
    process = context.Process(target=acquire)
    try:
        with font_bundle._workspace_bundle_lock(tmp_path):
            process.start()
            sent.close()
            assert received.poll(3) and received.recv() == "waiting"
            assert not received.poll(0.15)
        assert received.poll(3) and received.recv() == "acquired"
        process.join(timeout=3)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=3)
        received.close()
        sent.close()


def test_font_bundle_lock_retries_eagain_then_releases_on_body_failure(tmp_path, monkeypatch):
    clock, sleeps = _font_lock_clock(monkeypatch)
    operations = []
    def flock(fd, operation):
        operations.append(operation)
        if len(operations) <= 3:
            raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")
    monkeypatch.setattr(fcntl, "flock", flock)
    with pytest.raises(ValueError, match="body failed"):
        with font_bundle._workspace_bundle_lock(tmp_path):
            assert clock[0] == pytest.approx(0.15)
            raise ValueError("body failed")
    assert operations == [fcntl.LOCK_EX | fcntl.LOCK_NB] * 4 + [fcntl.LOCK_UN]
    assert sleeps == [0.05] * 3


def test_font_bundle_lock_timeout_is_bounded_and_does_not_unlock_unowned_lock(tmp_path, monkeypatch):
    clock, sleeps = _font_lock_clock(monkeypatch)
    calls = []
    def flock(fd, operation):
        calls.append((fd, operation))
        raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")
    monkeypatch.setattr(fcntl, "flock", flock)
    with pytest.raises(TimeoutError, match="font_bundle_lock_timeout") as error:
        with font_bundle._workspace_bundle_lock(tmp_path, timeout_s=0.12):
            pytest.fail("the lock was never acquired")
    assert clock[0] == pytest.approx(0.12)
    assert all(0 < delay <= 0.05 for delay in sleeps)
    assert all(operation == fcntl.LOCK_EX | fcntl.LOCK_NB for _, operation in calls)
    assert "waited_s=0.120" in str(error.value)
    assert str(tmp_path / "_state/locks/font-bundle.lock") in str(error.value)
    with pytest.raises(OSError) as closed:
        os.fstat(calls[0][0])
    assert closed.value.errno == errno.EBADF


def test_font_bundle_lock_does_not_retry_other_os_errors(tmp_path, monkeypatch):
    _, sleeps = _font_lock_clock(monkeypatch)
    operations = []
    def flock(fd, operation):
        operations.append(operation)
        raise PermissionError(errno.EACCES, "permission denied")
    monkeypatch.setattr(fcntl, "flock", flock)
    with pytest.raises(PermissionError):
        with font_bundle._workspace_bundle_lock(tmp_path):
            pytest.fail("the lock was never acquired")
    assert sleeps == [] and operations == [fcntl.LOCK_EX | fcntl.LOCK_NB]


@pytest.fixture(scope="module")
def pinned_font_sources(tmp_path_factory):
    from runtime.shared_fonts import SharedFontStore
    data = tmp_path_factory.mktemp("pinned-font-data")
    store = SharedFontStore(data, {"MURAL_STUDIO_DATA_DIR": str(data)})
    rows = []
    for family in ("Noto Sans SC", "Noto Serif SC", "IBM Plex Mono", "Inter"):
        for face in font_bundle.FAMILY_FACES[family]:
            source = font_bundle._find_source(face, [RELEASE_FONT_DIR])
            rows.append({**store.publish_public_font(source, license_name="OFL-1.1", license_source="test"),
                         "family": family, "weight": face.weight, "style": face.style, "scope": "public"})
    return {"schema": "mural.font-manifest.v2", "studio_data_root": str(data),
            "source_faces": rows, "custom_fonts": [], "roles": {}, "runtime_bundle": None}


def _pin_font_manifest(root, manifest):
    (root / "_state").mkdir(exist_ok=True)
    (root / "_state/font-manifest.json").write_text(json.dumps(manifest))


@pytest.mark.parametrize("mode", ["authoring", "portable"])
def test_chinese_font_family_cssom_and_real_loading_in_authoring_and_portable(
    tmp_path, monkeypatch, pinned_font_sources, mode,
):
    from urllib.parse import urljoin
    from playwright.sync_api import sync_playwright
    from runtime.shared_fonts import SharedFontStore
    from runtime.shared_objects import SharedSkillStore
    from state.workspace import WorkspaceState

    state = WorkspaceState(tmp_path)
    monkeypatch.setenv("PATH", f'{Path(sys.executable).parent}:{os.environ.get("PATH", "")}')
    store = SharedFontStore(tmp_path, {"MURAL_STUDIO_DATA_DIR": pinned_font_sources["studio_data_root"]})
    rows = list(pinned_font_sources["source_faces"])
    families = ("今年也要加油鸭", "段宁硬笔楷书粗体")
    for family in families:
        for face in font_bundle.FAMILY_FACES[family]:
            source = font_bundle._find_source(face, [RELEASE_FONT_DIR])
            rows.append({**store.publish_public_font(source,
                license_name=font_bundle.FONT_LICENSES[family]["license"], license_source="font-name-browser-test"),
                "family": family, "weight": face.weight, "style": face.style, "scope": "public"})
    _pin_font_manifest(tmp_path, {**pinned_font_sources, "source_faces": rows})
    SharedSkillStore(pinned_font_sources["studio_data_root"]).pin(
        state.paths, SKILL, skill_name="mural-presenter-v1")
    (tmp_path / "base.css").write_text(
        f':root {{ --font-body:"{families[1]}"; --font-display:"{families[0]}"; }}', encoding="utf-8")
    (tmp_path / "slides/slide_01.html").write_text('<h1>中文字体</h1>', encoding="utf-8")
    font_bundle.prepare_authoring_fonts(tmp_path)
    store.capture_runtime_bundle(state.paths)
    captured = json.loads(state.paths.font_manifest.read_text(encoding="utf-8"))
    for family in families:
        selected = next(face for face in captured["selected_faces"] if face["source_family"] == family)
        original = next(face for face in rows if face["family"] == family)
        assert selected["source_object_sha256"] == original["sha256"]
        assert (selected["weight"], selected["style"]) == (original["weight"], original["style"])
    assert captured["runtime_bundle"] is None
    assert not state.paths.runtime_fonts.exists()

    sys.path.insert(0, str(REPO / "webui/studio"))
    from app.engine import render_env
    environment = render_env({**os.environ, "TMPDIR": str(tmp_path / "_runtime/tmp")})
    (tmp_path / "_runtime/tmp").mkdir(parents=True, exist_ok=True)
    observations = {"mode": mode, "faces": {}}
    selected_families = dict.fromkeys(families)
    expected_fonts = {}
    if mode == "portable":
        manifest = font_bundle.bundle_workspace(tmp_path)
        assert not font_bundle.validate_font_bundle(tmp_path)
        selected_families = {family: manifest["delivery_families"][family] for family in families}
        assert len(set(selected_families.values())) == len(families), selected_families
        for family in families:
            face = next(row for row in manifest["faces"] if row["source_family"] == family)
            original = next(row for row in rows if row["family"] == family)
            assert face["source_object_sha256"] == original["sha256"]
            expected_fonts[family] = ((tmp_path / face["path"]).as_uri(), face["sha256"])
    else:
        for family in families:
            selected_families[family] = family
            face = next(row for row in rows if row["family"] == family)
            expected_fonts[family] = ((store.studio_data / face["object_ref"]).as_uri(), face["sha256"])
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=environment["PPT_SKILL_BROWSER_EXE"],
            env=environment, headless=True, args=["--no-sandbox", "--allow-file-access-from-files"])
        try:
            for family, selected in selected_families.items():
                css = (tmp_path / "base.css").read_text(encoding="utf-8")
                token = '--font-display' if family == families[0] else '--font-body'
                document = ('<!doctype html><meta charset="utf-8"><style>' + css + '</style>'
                    '<h1 id="direct" style=\'font-family:"' + selected + '"\'>中文字体</h1>'
                    '<h1 id="token" style="font-family:var(' + token + ')">中文字体</h1>')
                preview = tmp_path / "font-browser.html"
                preview.write_text(document, encoding="utf-8")
                page = browser.new_page()
                responses = []
                page.on("response", lambda response: responses.append(response))
                try:
                    page.goto(preview.as_uri(), wait_until="load")
                    observed = page.evaluate("""async family => {
                        await document.fonts.ready;
                        const loaded = await document.fonts.load(`48px "${family}"`, '中文字体');
                        return {
                            rules: [...document.styleSheets[0].cssRules]
                                .filter(rule=>rule.type===CSSRule.FONT_FACE_RULE)
                                .map(rule=>({family:rule.style.fontFamily,src:rule.style.src})),
                            loaded: loaded.map(face=>({family:face.family,status:face.status})),
                            direct: getComputedStyle(document.querySelector('#direct')).fontFamily,
                            token: getComputedStyle(document.querySelector('#token')).fontFamily,
                        };
                    }""", selected)
                    observed["font_bytes"] = {response.url: hashlib.sha256(response.body()).hexdigest()
                        for response in responses if response.request.resource_type == "font"}
                    observations["faces"][family] = observed
                    expected_url, expected_hash = expected_fonts[family]
                    assert any(rule["family"].strip('"') == selected
                               and urljoin(preview.as_uri(), rule["src"].split('"')[1]) == expected_url
                               for rule in observed["rules"]), observed
                    assert observed["loaded"] and all(face["status"] == "loaded" for face in observed["loaded"]), observed
                    assert all(face["family"].strip('"') == selected for face in observed["loaded"]), observed
                    assert observed["direct"].split(",")[0].strip('" ') == selected
                    assert observed["token"].split(",")[0].strip('" ') == selected
                    assert observed["font_bytes"].get(expected_url) == expected_hash, observed
                finally:
                    page.close()
        finally:
            browser.close()
            (tmp_path / "font-browser-observations.json").write_text(
                json.dumps(observations, ensure_ascii=False, indent=2), encoding="utf-8")


def _font_plan_workspace(root, *, presentation='zh', working='zh'):
    from state.workspace import WorkspaceState
    WorkspaceState(root)
    (root / 'config.json').write_text(json.dumps({'working_language': working,
        'presentation_language': presentation, 'target_page_count': 1}))
    (root / 'briefs').mkdir(exist_ok=True)
    (root / 'briefs/slide_01.md').write_text(
        '# Hello 中文\n## Page task\nExplain the example.\n'
        '## Content and evidence\nKeep the actual data and its boundary.\n'
        '## Visual expression\nUse the authored font roles.\n'
        '## Speaker notes\nOne paragraph.\n\n- Keep this list.\n')
    (root / 'deck.md').write_text('# Font choices\n## Overall design\nTypography serves the content.\n')
    return importlib.import_module(font_bundle.__package__ + '.deck_core')


@pytest.mark.parametrize("language,prose", [('en', '内部规划注释'), ('zh', 'English planning note')])
def test_current_config_language_precedes_planning_prose(tmp_path, language, prose):
    core = _font_plan_workspace(tmp_path, presentation=language)
    (tmp_path/'deck.md').write_text(f'# Deck\n## Task and audience\n{prose}\n')
    deck, pages = core._load_plans(tmp_path, 1)
    assert deck['language'] == language
    assert pages[0]['speech'] == 'One paragraph.\n\n- Keep this list.'


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("route", sorted(json.loads((SKILL/'references/typography-routes.json').read_text())["routes"]))
def test_font_catalog_tokens_are_authored_explicitly_without_language_rewriting(tmp_path, route, language):
    core = _font_plan_workspace(tmp_path, presentation=language)
    catalog = json.loads((SKILL/'references/typography-routes.json').read_text())
    tokens = catalog['routes'][route]['tokens']
    css = ':root {\n' + '\n'.join(f'  {token}: {value};' for token, value in tokens.items()) + '\n}\n'
    (tmp_path/'base.css').write_text(css)
    (tmp_path/'deck.md').write_text(f'# Deck\n## Overall design\nConsulted {route}; actual font roles are in base.css.\n')
    assert core._load_plans(tmp_path, 1)[0]['language'] == language
    assert font_bundle._token_definitions((tmp_path/'base.css').read_text()) == tokens
    assert (tmp_path/'base.css').read_text() == css
    assert not (tmp_path/'_trace/typography-route.json').exists()


def test_explicit_font_intent_precedes_author_css_default(tmp_path, monkeypatch):
    _font_plan_workspace(tmp_path, presentation='en')
    monkeypatch.setattr(font_bundle, '_font_source_dirs', lambda: [RELEASE_FONT_DIR])
    css = ':root { --font-title: "Manrope", "Noto Sans SC", sans-serif; --font-body: "Inter", "Noto Sans SC", sans-serif; }'
    (tmp_path/'font-intent.json').write_text(json.dumps({'schema':'mural.font-intent.v1',
        'roles':{'body':{'requested_stack':['Noto Sans SC'], 'weight':'400'}}}))
    identity = font_bundle._bundle_identity(tmp_path, css, ['<p>Hello 中文</p>'])
    assert identity[2]['--font-body'][0] == 'Noto Sans SC'
    assert identity[2]['--font-title'][0] == 'Manrope'


@pytest.fixture(scope="module")
def education_font_sources(pinned_font_sources):
    from runtime.shared_fonts import SharedFontStore
    data = Path(pinned_font_sources["studio_data_root"])
    store = SharedFontStore(data, {"MURAL_STUDIO_DATA_DIR": str(data)})
    rows = list(pinned_font_sources["source_faces"])
    for family in ("Sora", "Xiaolai"):
        for face in font_bundle.FAMILY_FACES[family]:
            source = font_bundle._find_source(face, [RELEASE_FONT_DIR])
            rows.append({**store.publish_public_font(source, license_name="OFL-1.1", license_source="test"),
                         "family": family, "weight": face.weight, "style": face.style, "scope": "public"})
    return {**pinned_font_sources, "source_faces": rows}


def _education_plan(description):
    return '# Font choices\n## Overall design\n' + description + '\n'


def _author_font_workspace(root, manifest):
    core = _font_plan_workspace(root)
    _pin_font_manifest(root, manifest)
    (root / 'base.css').write_text(':root {\n  --canvas-w: 1600px;\n  --canvas-h: 900px;\n'
                                  '  --font-display: "Inter", "Noto Sans SC", sans-serif;\n}\n')
    return core


@pytest.mark.parametrize('declaration, selected', [
    ('- typography_route: education-friendly', False),
    ('字体角色：typography_route: education-friendly。', False),
    ('Use the explicitly authored education font roles.', True),
])
def test_scaffold_reports_prepared_author_css_not_planning_route(
    tmp_path, education_font_sources, declaration, selected, capsys, monkeypatch,
):
    core = _author_font_workspace(tmp_path, education_font_sources)
    monkeypatch.setattr(core, '_read_author_geometry', lambda _root: {'width_px': 1600, 'height_px': 900})
    source = _education_plan(declaration)
    (tmp_path / 'deck.md').write_text(source)
    if selected:
        tokens = json.loads((SKILL/'references/typography-routes.json').read_text())['routes']['education-friendly']['tokens']
        css = tmp_path / 'base.css'
        css.write_text(css.read_text() + ':root {\n' + '\n'.join(f'{key}: {value};' for key, value in tokens.items()) + '\n}\n')
    assert core.scaffold_from_plans(tmp_path, 1) == [1]
    output = capsys.readouterr().out
    receipt = json.loads(next(line.removeprefix('typography:') for line in output.splitlines()
                              if line.startswith('typography:')))
    assert set(receipt) == {'effective_token_stacks', 'source'}
    assert receipt['source'] == 'base.css and user font settings after font preparation'
    assert not (tmp_path / '_trace/typography-route.json').exists()
    assert not (tmp_path / '_trace/design-compile.json').exists()
    effective = receipt['effective_token_stacks']
    final_css = (tmp_path / 'base.css').read_text()
    definitions = font_bundle._token_definitions(final_css)
    for token, stack in effective.items():
        assert font_bundle._family_candidates(definitions[token]) == stack
    resolution = json.loads((tmp_path / '_trace/font-resolution.json').read_text())
    assert resolution['token_stacks'] == effective
    if selected:
        # 作者明确选择的家族顺序不变；generic fallback 仍按现有字体解析处理。
        assert effective == {
            '--font-display': ['Sora', 'Noto Sans SC', 'Inter'],
            '--font-title': ['Noto Sans SC', 'Inter'],
            '--font-body': ['Noto Sans SC', 'Inter'],
            '--font-number': ['Sora', 'Noto Sans SC', 'Inter'],
            '--font-annotation': ['Xiaolai', 'Noto Sans SC'],
            '--font-mono': ['IBM Plex Mono', 'Noto Sans SC'],
        }
    else:
        assert effective['--font-display'][0] == 'Inter'
    assert (tmp_path / 'deck.md').read_text() == source
    assert not (tmp_path / '_runtime/tmp/fonts').exists()


@pytest.mark.parametrize('override, expected', [
    ('author-css', 'Inter'), ('font-intent', 'Noto Serif SC'), ('user-font', 'Inter'),
])
def test_font_preparation_keeps_explicit_overrides_and_reports_effective_stack(
    tmp_path, education_font_sources, override, expected, capsys, monkeypatch,
):
    manifest = education_font_sources
    if override == 'user-font':
        manifest = {**manifest, 'roles': {'display': {'kind': 'builtin', 'family': 'Inter'}}}
    core = _author_font_workspace(tmp_path, manifest)
    monkeypatch.setattr(core, '_read_author_geometry', lambda _root: {'width_px': 1600, 'height_px': 900})
    css_path = tmp_path / 'base.css'
    if override != 'author-css':
        css_path.write_text(css_path.read_text() + '\n:root { --font-display: "Sora", "Noto Sans SC", sans-serif; }\n')
    (tmp_path / 'deck.md').write_text(_education_plan('Font roles are explicitly authored in CSS.'))
    if override in {'font-intent', 'user-font'}:
        (tmp_path / 'font-intent.json').write_text(json.dumps({'schema': 'mural.font-intent.v1',
            'roles': {'display': {'requested_stack': ['Noto Serif SC']}}}))
    core.scaffold_from_plans(tmp_path, 1)
    receipt = json.loads(next(line.removeprefix('typography:') for line in capsys.readouterr().out.splitlines()
                              if line.startswith('typography:')))
    stack = receipt['effective_token_stacks']['--font-display']
    assert set(receipt) == {'effective_token_stacks', 'source'}
    assert stack[0] == expected
    assert any(family in {'Noto Sans SC', 'Noto Serif SC'} for family in stack)
    final_css = (tmp_path / 'base.css').read_text()
    assert font_bundle._family_candidates(font_bundle._token_definitions(final_css)['--font-display']) == stack
    assert font_bundle.prepare_authoring_fonts(tmp_path, from_plans=True)['token_stacks']['--font-display'] == stack
    assert (tmp_path / 'base.css').read_text() == final_css


def test_planning_edit_does_not_change_authored_font_selection(
    tmp_path, education_font_sources, capsys, monkeypatch,
):
    core = _author_font_workspace(tmp_path, education_font_sources)
    monkeypatch.setattr(core, '_read_author_geometry', lambda _root: {'width_px': 1600, 'height_px': 900})
    plan = tmp_path / 'deck.md'
    plan.write_text(_education_plan('- typography_route: education-friendly'))
    core.scaffold_from_plans(tmp_path, 1)
    capsys.readouterr()
    css = (tmp_path / 'base.css').read_bytes()
    plan.write_text(_education_plan('Fonts remain optional.'))
    core.scaffold_from_plans(tmp_path, 1)
    receipt = json.loads(next(line.removeprefix('typography:') for line in capsys.readouterr().out.splitlines()
                              if line.startswith('typography:')))
    assert set(receipt) == {'effective_token_stacks', 'source'}
    assert receipt['effective_token_stacks']['--font-display'][0] == 'Inter'
    assert not (tmp_path / '_trace/typography-route.json').exists()
    assert not (tmp_path / '_trace/design-compile.json').exists()
    assert (tmp_path / 'base.css').read_bytes() == css


def test_automatic_font_preflight_writes_shared_css_even_when_update_flag_is_false(
    tmp_path, education_font_sources, monkeypatch,
):
    core = _author_font_workspace(tmp_path, education_font_sources)
    monkeypatch.setattr(core, '_read_author_geometry', lambda _: {'width_px': 1600, 'height_px': 900})
    (tmp_path / 'deck.md').write_text(_education_plan('Keep explicit font choices.'))
    before = (tmp_path / 'base.css').read_bytes()
    core.scaffold_from_plans(tmp_path, pages=[1], update_shared_css=False)
    prepared = (tmp_path / 'base.css').read_bytes()
    assert prepared != before
    assert font_bundle.AUTHORING_START.encode() in prepared
    assert (tmp_path / 'slides/slide_01.html').is_file()
    html = (tmp_path / 'slides/slide_01.html').read_bytes()
    core.scaffold_from_plans(tmp_path, pages=[1], update_shared_css=False)
    assert (tmp_path / 'base.css').read_bytes() == prepared
    assert (tmp_path / 'slides/slide_01.html').read_bytes() == html


def test_existing_pages_receive_missing_authoring_font_block(
    tmp_path, education_font_sources, monkeypatch,
):
    core = _author_font_workspace(tmp_path, education_font_sources)
    monkeypatch.setattr(core, '_read_author_geometry', lambda _: {'width_px': 1600, 'height_px': 900})
    (tmp_path / 'deck.md').write_text(_education_plan('Keep explicit font choices.'))
    core.scaffold_from_plans(tmp_path, pages=[1])
    html = (tmp_path / 'slides/slide_01.html').read_bytes()
    css = (tmp_path / 'base.css').read_text()
    stripped, count = re.subn(
        re.escape(font_bundle.AUTHORING_START) + r'.*?' + re.escape(font_bundle.AUTHORING_END),
        '',
        css,
        count=1,
        flags=re.S,
    )
    assert count == 1
    (tmp_path / 'base.css').write_text(stripped)
    core.scaffold_from_plans(tmp_path, pages=[1], update_shared_css=False)
    restored = (tmp_path / 'base.css').read_text()
    assert font_bundle.AUTHORING_START in restored
    assert (tmp_path / 'slides/slide_01.html').read_bytes() == html


def test_partial_plan_prepares_real_fonts_and_renders_without_unrelated_briefs(
    tmp_path, education_font_sources,
):
    from PIL import Image
    from geometry_fixture import require_browser
    from runtime.shared_objects import SharedSkillStore, load_skill_module
    from state.workspace import WorkspaceState

    require_browser(tmp_path)
    _author_font_workspace(tmp_path, education_font_sources)
    reference = SharedSkillStore(education_font_sources['studio_data_root']).pin(
        WorkspaceState(tmp_path).paths, SKILL, skill_name='mural-presenter-v1')
    core = load_skill_module(reference.object_root, reference.tree_sha256, 'deck_core')
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({**json.loads(config.read_text()), 'target_page_count': 10}))
    (tmp_path / 'briefs/slide_01.md').rename(tmp_path / 'briefs/slide_05.md')
    (tmp_path / 'briefs/slide_03.md').write_text('Unfinished independent plan')
    css = tmp_path / 'base.css'
    css.write_text(css.read_text() + '\nbody{margin:0}.slide{width:1600px;height:900px;background:white}'
        'h1{font-family:var(--font-display);font-size:64px;font-weight:700;margin:40px}\n')

    assert core.scaffold_from_plans(tmp_path, expected=10, pages=[5]) == [5]
    prepared_css = css.read_bytes()
    assert font_bundle.AUTHORING_START.encode() in prepared_css
    assert not (tmp_path / 'slides/slide_01.html').exists()
    html = tmp_path / 'slides/slide_05.html'
    html.write_text(html.read_text().replace('<!-- SLIDE_AGENT_FILL_START -->',
        '<!-- SLIDE_AGENT_FILL_START --><h1>Hello 中文</h1>'))
    source = html.read_bytes()
    assert core.render(tmp_path, 5, expected=10)
    with Image.open(tmp_path / 'renders/slide_05.png') as image:
        assert image.size == (1600, 900)
        assert any(low < 200 for low, _high in image.convert('RGB').getextrema())
    metadata = core._page_render_metadata(tmp_path, 5)
    assert any(path.endswith(('.ttf', '.otf')) for path in metadata['loaded_resources'])
    assert not metadata['failed_resources']
    assert css.read_bytes() == prepared_css and html.read_bytes() == source
    with pytest.raises(ValueError):
        core.validate_plans(tmp_path, expected=10)


def test_route_audit_includes_language_default_fonts(tmp_path, monkeypatch):
    monkeypatch.setattr(font_bundle, '_font_source_dirs', lambda: [RELEASE_FONT_DIR])
    path = tmp_path/'routes.json'
    path.write_text(json.dumps({'routes': {'example': {'tokens': {'--font-body': '"Manrope"'}}},
                               'language_defaults': {'en': {'Noto Sans SC': 'unavailable-language-face'}}}))
    audit = font_bundle.audit_typography_routes(path)
    assert not audit['ready']
    assert any('unavailable-language-face' in problem for problem in audit['missing'])


@pytest.mark.parametrize("requested,expected", [("Manrope", "Manrope"), ("GeistMono", "Geist Mono"), ("SF Pro Text", "Noto Sans SC")])
def test_font_intent_selects_actual_bundle_stack_and_changes_identity(tmp_path, monkeypatch, requested, expected):
    monkeypatch.setattr(font_bundle, "_font_source_dirs", lambda: [RELEASE_FONT_DIR])
    css = ':root { --font-body: "Noto Sans SC", sans-serif; }'
    fragments = ['<p style="font-family:var(--font-body)">You’ll read “this”. 中文。</p>']
    before = font_bundle._bundle_identity(tmp_path, css, fragments)
    (tmp_path / 'font-intent.json').write_text(json.dumps({
        'schema': 'mural.font-intent.v1',
        'roles': {'body': {'requested_stack': [requested, 'Noto Sans SC'], 'weight': '400', 'style': 'normal'}},
    }))
    after = font_bundle._bundle_identity(tmp_path, css, fragments)
    assert after[2]['--font-body'][0] == expected
    assert expected in after[5]
    assert 'Noto Sans SC' in after[2]['--font-body']
    assert after[6] != before[6]
    row = next(r for r in after[7] if r['role'] == 'body')
    assert row['resolved_family'] == after[2]['--font-body'][0]


def test_explicit_custom_font_role_precedes_lower_priority_intent(tmp_path, monkeypatch):
    monkeypatch.setattr(font_bundle, "_font_source_dirs", lambda: [RELEASE_FONT_DIR])
    monkeypatch.setattr(font_bundle, "_load_custom_config", lambda _root: (
        {'Authorized Brand': {'weight': '400'}}, {'--font-body': 'Authorized Brand'},
    ))
    (tmp_path / 'font-intent.json').write_text(json.dumps({'schema': 'mural.font-intent.v1',
        'roles': {'body': {'requested_stack': ['Manrope'], 'weight': '700'}}}))
    identity = font_bundle._bundle_identity(tmp_path, ':root { --font-body: "Noto Sans SC"; }', ['<p>Hello</p>'])
    row = next(r for r in identity[7] if r['role'] == 'body')
    assert identity[2]['--font-body'][0] == row['resolved_family'] == 'Authorized Brand'
    assert row['resolution_kind'] == 'exact_user_upload' and row['user_authorized']
    assert row['weight_resolved'] == '400'


def test_real_font_bundle_and_cache_keep_intent_and_delivered_css_in_sync(tmp_path, monkeypatch):
    monkeypatch.setattr(font_bundle, "_font_source_dirs", lambda: [RELEASE_FONT_DIR])
    monkeypatch.setenv('PATH', f'{Path(sys.executable).parent}:{os.environ.get("PATH", "")}')
    (tmp_path / 'slides').mkdir()
    (tmp_path / 'slides/slide_01.html').write_text('<p style="font-family:var(--font-body)">You’ll read “this”. 中文。</p>')
    css_path = tmp_path / 'base.css'
    css_path.write_text(':root { --font-body: "Noto Sans SC", sans-serif; }')
    intent_path = tmp_path / 'font-intent.json'
    intent = {'schema': 'mural.font-intent.v1', 'roles': {'body': {'requested_stack': ['Inter', 'Noto Sans SC']}}}
    intent_path.write_text(json.dumps(intent))
    first = font_bundle.bundle_workspace(tmp_path)
    css = css_path.read_text()
    assert first['token_stacks']['--font-body'][0] == 'Inter'
    assert f'--font-body: "{first["delivery_families"]["Inter"]}"' in css
    assert any(face['source_family'] == 'Inter' for face in first['faces'])
    assert not font_bundle.validate_font_bundle(tmp_path)
    assert font_bundle.bundle_workspace(tmp_path) == first
    assert css_path.read_text() == css
    intent['roles']['body']['requested_stack'][0] = 'InterVariable'
    intent_path.write_text(json.dumps(intent))
    second = font_bundle.bundle_workspace(tmp_path)
    assert second['deck_id'] == first['deck_id']
    row = next(r for r in second['resolutions'] if r['role'] == 'body')
    assert row['requested_family'] == 'InterVariable' and row['resolved_family'] == 'Inter'
    assert row['resolution_kind'] == 'exact_alias'
    assert second['resolution_fingerprint'] != first['resolution_fingerprint']
    assert not font_bundle.validate_font_bundle(tmp_path)


def test_authoring_fonts_use_shared_release_sources_without_workspace_copies(tmp_path, monkeypatch, pinned_font_sources):
    _pin_font_manifest(tmp_path, pinned_font_sources)
    monkeypatch.setattr(font_bundle, "_font_source_dirs", lambda: [RELEASE_FONT_DIR])
    (tmp_path / "slides").mkdir()
    (tmp_path / "slides/slide_01.html").write_text(
        '<p style="font-family:var(--font-body)">共享字体 中文</p>', encoding="utf-8"
    )
    (tmp_path / "base.css").write_text(
        ':root { --font-body: "Noto Sans SC", sans-serif; }', encoding="utf-8"
    )

    manifest = font_bundle.prepare_authoring_fonts(tmp_path)

    css = (tmp_path / "base.css").read_text(encoding="utf-8")
    assert manifest["mode"] == "shared-authoring"
    trace = json.loads((tmp_path/'_trace/font-resolution.json').read_text())
    assert trace['resolutions'] == manifest['resolutions']
    assert trace['resolution_fingerprint'] == manifest['resolution_fingerprint']
    assert font_bundle.AUTHORING_START in css
    pinned = next(row for row in pinned_font_sources['source_faces'] if row['family'] == 'Noto Sans SC')
    source = Path(pinned_font_sources['studio_data_root']) / pinned['object_ref']
    assert source.as_uri() in css
    assert RELEASE_FONT_DIR.as_uri() not in css
    assert not (tmp_path / "_runtime/tmp/fonts").exists()


def test_portable_bundle_replaces_shared_authoring_sources(tmp_path, monkeypatch, pinned_font_sources):
    _pin_font_manifest(tmp_path, pinned_font_sources)
    monkeypatch.setattr(font_bundle, "_font_source_dirs", lambda: [RELEASE_FONT_DIR])
    monkeypatch.setenv('PATH', f'{Path(sys.executable).parent}:{os.environ.get("PATH", "")}')
    (tmp_path / "slides").mkdir()
    (tmp_path / "slides/slide_01.html").write_text('<p>Portable 中文</p>', encoding="utf-8")
    (tmp_path / "base.css").write_text(
        ':root { --font-body: "Noto Sans SC", sans-serif; }', encoding="utf-8"
    )
    font_bundle.prepare_authoring_fonts(tmp_path)

    font_bundle.bundle_workspace(tmp_path)

    css = (tmp_path / "base.css").read_text(encoding="utf-8")
    assert font_bundle.AUTHORING_START not in css
    assert font_bundle.BUNDLE_START in css
    assert "_runtime/tmp/fonts/" in css
    assert not font_bundle.validate_font_bundle(tmp_path)


def test_shared_font_digest_verifies_pinned_bytes_and_rejects_unlisted_uri(tmp_path, pinned_font_sources):
    (tmp_path / "config.json").write_text(json.dumps({"working_language": "en", "presentation_language": "en", "target_page_count": 1}))
    _pin_font_manifest(tmp_path, pinned_font_sources)
    core = importlib.import_module(font_bundle.__package__ + '.deck_core')
    row = pinned_font_sources['source_faces'][0]
    source = Path(pinned_font_sources['studio_data_root']) / row['object_ref']
    uri = source.as_uri()
    css = f'@font-face {{src: url("{uri}")}}'
    plain = hashlib.sha256(css.encode() + b'\0<p>Font</p>').hexdigest()
    assert core._render_input_digest(tmp_path, css, '<p>Font</p>') != plain
    assert font_bundle.resolve_shared_font_reference(tmp_path, uri) == (source, row['sha256'])
    with pytest.raises(ValueError, match='not pinned'):
        font_bundle.resolve_shared_font_reference(tmp_path, (RELEASE_FONT_DIR/'NotoSansSC.ttf').as_uri())
    bad = {**pinned_font_sources, 'source_faces': [{**row, 'sha256': '0' * 64}]}
    _pin_font_manifest(tmp_path, bad)
    with pytest.raises(ValueError, match='hash|identity'):
        core._render_input_digest(tmp_path, css, '<p>Font</p>')


def test_authoring_resume_keeps_old_subset_files_but_clears_obsolete_receipt(tmp_path, pinned_font_sources, monkeypatch):
    from runtime.shared_fonts import SharedFontStore
    from runtime.shared_objects import SharedSkillStore
    from state.workspace import WorkspaceState
    state = WorkspaceState(tmp_path)
    old = {**pinned_font_sources, 'runtime_bundle': {'sha256': 'old'}, 'selected_faces': [{'path': 'old'}]}
    _pin_font_manifest(tmp_path, old)
    SharedSkillStore(pinned_font_sources['studio_data_root']).pin(state.paths, SKILL, skill_name='mural-presenter-v1')
    fonts = tmp_path / '_runtime/tmp/fonts'
    fonts.mkdir(parents=True)
    retained = fonts / 'historical.woff2'
    retained.write_bytes(b'historical subset')
    (fonts / 'manifest.json').write_text('{"faces": []}')
    (tmp_path / 'base.css').write_text(':root { --font-body: "Noto Sans SC"; --canvas-w: 1600px; --canvas-h: 900px; }')
    (tmp_path / 'slides/slide_01.html').write_text('<p>Hello</p>')
    monkeypatch.setattr(font_bundle, '_font_source_dirs', lambda: [])
    first = font_bundle.prepare_authoring_fonts(tmp_path)
    css = (tmp_path/'base.css').read_bytes()
    assert font_bundle.prepare_authoring_fonts(tmp_path) == first
    assert (tmp_path/'base.css').read_bytes() == css
    store = SharedFontStore(tmp_path, {'MURAL_STUDIO_DATA_DIR': pinned_font_sources['studio_data_root']})
    store.capture_runtime_bundle(state.paths)
    saved = json.loads((tmp_path/'_state/font-manifest.json').read_text())
    assert saved['runtime_bundle'] is None
    assert saved['selected_faces'] and all(row['source_object_sha256'] for row in saved['selected_faces'])
    assert retained.read_bytes() == b'historical subset'
    core = importlib.import_module(font_bundle.__package__ + '.deck_core')
    monkeypatch.setattr(core, 'audit_workspace', lambda _root: [])
    core.audit(tmp_path)


def test_shared_face_receipt_keeps_distinct_weights_for_same_object(tmp_path, pinned_font_sources):
    from runtime.shared_fonts import SharedFontStore
    from runtime.shared_objects import SharedSkillStore
    from state.workspace import WorkspaceState
    state = WorkspaceState(tmp_path)
    row = pinned_font_sources['source_faces'][0]
    faces = [{**row, 'weight': weight} for weight in ('400', '700')]
    _pin_font_manifest(tmp_path, {**pinned_font_sources, 'source_faces': faces})
    SharedSkillStore(pinned_font_sources['studio_data_root']).pin(state.paths, SKILL, skill_name='mural-presenter-v1')
    uri = (Path(pinned_font_sources['studio_data_root'])/row['object_ref']).as_uri()
    rules = [f'@font-face {{font-family: {json.dumps(row["family"])}; font-weight: {weight}; font-style: normal; src: url("{uri}");}}'
             for weight in ('400', '700')]
    (tmp_path/'base.css').write_text(font_bundle.AUTHORING_START+'\n'.join(rules)+font_bundle.AUTHORING_END)
    store = SharedFontStore(tmp_path, {'MURAL_STUDIO_DATA_DIR': pinned_font_sources['studio_data_root']})
    store.capture_runtime_bundle(state.paths)
    selected = json.loads((tmp_path/'_state/font-manifest.json').read_text())['selected_faces']
    assert [row['weight'] for row in selected] == ['400', '700']


@pytest.mark.parametrize("family", ["Inter", "InterVariable", "Inter Variable"])
def test_authoring_aliases_capture_one_pinned_face(tmp_path, pinned_font_sources, family):
    from runtime.shared_fonts import SharedFontStore
    from runtime.shared_objects import SharedSkillStore, load_skill_module
    from state.workspace import WorkspaceState
    state = WorkspaceState(tmp_path)
    _pin_font_manifest(tmp_path, pinned_font_sources)
    ref = SharedSkillStore(pinned_font_sources['studio_data_root']).pin(
        state.paths, SKILL, skill_name='mural-presenter-v1')
    module = load_skill_module(ref.object_root, ref.tree_sha256, 'font_bundle')
    css_path = state.paths.path('base_css')
    css_path.write_text(f':root {{ --font-body: "{family}"; }}')
    state.paths.page_html(1).write_text('<p style="font-family:var(--font-body)">Inter aliases</p>')
    module.prepare_authoring_fonts(tmp_path)
    css = css_path.read_bytes()
    for name in module._font_face_names('Inter'):
        assert f'font-family: "{name}";'.encode() in css
    store = SharedFontStore(tmp_path, {'MURAL_STUDIO_DATA_DIR': pinned_font_sources['studio_data_root']})
    store.capture_runtime_bundle(state.paths)
    captured = state.paths.font_manifest.read_bytes()
    faces = [row for row in json.loads(captured)['selected_faces'] if row['source_family'] == 'Inter']
    assert len(faces) == 1
    expected = next(row for row in pinned_font_sources['source_faces'] if row['family'] == 'Inter')
    assert faces[0]['source_object_sha256'] == expected['sha256']
    assert faces[0]['weight'] == expected['weight']
    store.capture_runtime_bundle(state.paths)
    assert state.paths.font_manifest.read_bytes() == captured
    assert css_path.read_bytes() == css


@pytest.mark.parametrize('change', ['unknown_alias', 'other_family_alias', 'weight', 'style', 'hash', 'path'])
def test_authoring_alias_does_not_relax_pinned_identity(tmp_path, pinned_font_sources, change):
    from runtime.shared_fonts import SharedFontError, SharedFontStore
    from runtime.shared_objects import SharedSkillStore
    from state.workspace import WorkspaceState
    state = WorkspaceState(tmp_path)
    row = next(row for row in pinned_font_sources['source_faces'] if row['family'] == 'Inter')
    manifest = {**pinned_font_sources, 'source_faces': [dict(row)], 'selected_faces': []}
    uri = (Path(manifest['studio_data_root']) / row['object_ref']).as_uri()
    family, weight, style = 'InterVariable', row['weight'], row['style']
    if change == 'unknown_alias':
        family = 'InterInvented'
    elif change == 'other_family_alias':
        family = 'NotoSansSC'
    elif change == 'weight':
        weight = '700'
    elif change == 'style':
        style = 'italic'
    elif change == 'hash':
        manifest['source_faces'][0]['sha256'] = '0' * 64
    elif change == 'path':
        other = tmp_path / 'same-bytes.ttf'
        other.write_bytes((Path(manifest['studio_data_root']) / row['object_ref']).read_bytes())
        uri = other.as_uri()
    _pin_font_manifest(tmp_path, manifest)
    SharedSkillStore(manifest['studio_data_root']).pin(state.paths, SKILL, skill_name='mural-presenter-v1')
    state.paths.path('base_css').write_text(font_bundle.AUTHORING_START +
        f'@font-face {{font-family: "{family}"; font-weight: {weight}; font-style: {style}; src: url("{uri}");}}' +
        font_bundle.AUTHORING_END)
    store = SharedFontStore(tmp_path, {'MURAL_STUDIO_DATA_DIR': manifest['studio_data_root']})
    before = state.paths.font_manifest.read_bytes()
    with pytest.raises(SharedFontError):
        store.capture_runtime_bundle(state.paths)
    assert state.paths.font_manifest.read_bytes() == before


@pytest.mark.parametrize('integrity_error', [True, False])
def test_font_capture_classifies_integrity_but_not_lock_contention(integrity_error):
    from types import SimpleNamespace
    from runtime.production import SkillSnapshot
    from runtime.shared_fonts import SharedFontError
    from runtime.tools import ToolExecutionError

    failure = (SharedFontError('authoring font face does not match its pinned identity')
               if integrity_error else BlockingIOError(errno.EAGAIN, 'lock contention'))
    def capture(_paths):
        raise failure
    snapshot = SimpleNamespace(font_store=SimpleNamespace(capture_runtime_bundle=capture), paths=None)
    with pytest.raises(ToolExecutionError if integrity_error else BlockingIOError) as caught:
        SkillSnapshot.capture_runtime_fonts(snapshot)
    if integrity_error:
        assert caught.value.code == 'font_integrity_failed'
        assert str(failure) in str(caught.value)
        assert 'resume' in str(caught.value)
    else:
        assert caught.value is failure


def test_artifact_dependencies_include_authorized_font_and_reject_byte_tamper(tmp_path, pinned_font_sources):
    from runtime.shared_objects import SharedSkillStore
    from state.artifacts import _html_resource_hashes, ArtifactStateError
    from state.workspace import WorkspaceState
    state = WorkspaceState(tmp_path)
    data = Path(pinned_font_sources['studio_data_root'])
    payload = b'font dependency fixture'
    digest = hashlib.sha256(payload).hexdigest()
    relative = f'shared/fonts/objects/{digest}.ttf'
    source = data / relative
    source.write_bytes(payload)
    manifest = {**pinned_font_sources, 'source_faces': [
        {'family': 'fixture', 'weight': '400', 'style': 'normal', 'scope': 'public',
         'object_ref': relative, 'sha256': digest}]}
    _pin_font_manifest(tmp_path, manifest)
    SharedSkillStore(data).pin(state.paths, SKILL, skill_name='mural-presenter-v1')
    (tmp_path/'assets/canvas.css').write_text(':root{}')
    (tmp_path/'base.css').write_text(f'@font-face {{src:url("{source.as_uri()}")}}')
    html = tmp_path/'slides/slide_01.html'
    html.write_text('<section>Text</section>')
    resources = _html_resource_hashes(tmp_path, html, loaded_resources=[])
    assert resources[source.as_uri()] == digest
    assert 'base.css' in resources
    source.write_bytes(b'changed font bytes')
    with pytest.raises(ArtifactStateError, match='hash mismatch'):
        _html_resource_hashes(tmp_path, html, loaded_resources=[])


def test_pinned_custom_font_stays_deck_scoped_and_is_portable(tmp_path, pinned_font_sources, monkeypatch):
    import shutil
    _pin_font_manifest(tmp_path, pinned_font_sources)
    row = next(row for row in pinned_font_sources['source_faces'] if row['family'] == 'Inter')
    data = Path(pinned_font_sources['studio_data_root'])
    relative = f'users/1/_shared/fonts/objects/{row["sha256"]}.ttf'
    private = data/relative
    private.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(data/row['object_ref'], private)
    custom = {**row, 'id': 'brand', 'object_ref': relative, 'scope': 'user', 'owner_user_id': '1', 'weight': '400'}
    _pin_font_manifest(tmp_path, {**pinned_font_sources, 'custom_fonts': [custom],
                                'roles': {'body': {'kind': 'custom', 'font_id': 'brand'}}})
    (tmp_path/'slides').mkdir()
    (tmp_path/'slides/slide_01.html').write_text('<p style="font-family:var(--font-body)">Private 中文</p>')
    (tmp_path/'base.css').write_text(':root {--font-body: "Noto Sans SC";}')
    monkeypatch.setattr(font_bundle, '_font_source_dirs', lambda: [])
    monkeypatch.setenv('PATH', f'{Path(sys.executable).parent}:{os.environ.get("PATH", "")}')
    bundle = font_bundle.prepare_authoring_fonts(tmp_path)
    assert bundle['token_stacks']['--font-body'][0] == 'User::brand'
    assert any(face['user_authorized'] for face in bundle['faces'])
    assert not (tmp_path/'_runtime/tmp/fonts').exists()
    css = (tmp_path/'base.css').read_text()
    assert private.as_uri() in css and font_bundle.AUTHORING_START in css
    assert font_bundle.resolve_shared_font_reference(tmp_path, private.as_uri())[0] == private
    monkeypatch.setenv("MURAL_USER_ID", "other")
    with pytest.raises(ValueError, match="different WebUI user"):
        font_bundle.resolve_shared_font_reference(tmp_path, private.as_uri())
    monkeypatch.setenv("MURAL_USER_ID", "1")
    # 便携打包仍明确生成有授权回执的子集；这里是隔离测试 workspace。
    bundle = font_bundle.bundle_workspace(tmp_path)
    assert any(face["user_authorized"] for face in bundle["faces"])
    assert not font_bundle.validate_font_bundle(tmp_path)
    assert "file:" not in (tmp_path/"base.css").read_text()


@pytest.mark.parametrize('failure', ['missing', 'corrupt', 'unreadable'])
def test_pinned_custom_font_failure_keeps_object_provenance(tmp_path, failure):
    data = tmp_path/'data'
    payload = b'invalid-font-bytes'
    digest = hashlib.sha256(payload).hexdigest()
    relative = f'users/1/_shared/fonts/objects/{digest}.ttf'
    source = data/relative
    source.parent.mkdir(parents=True)
    if failure != 'missing':
        source.write_bytes(b'changed bytes' if failure == 'corrupt' else payload)
    _pin_font_manifest(tmp_path, {'schema': 'mural.font-manifest.v2', 'studio_data_root': str(data),
                                'custom_fonts': [{'id': 'broken', 'object_ref': relative, 'sha256': digest,
                                                  'scope': 'user', 'owner_user_id': '1'}]})
    expected = FileNotFoundError if failure == 'missing' else ValueError
    with pytest.raises(expected, match='missing|hash mismatch|unreadable') as exc:
        font_bundle._load_custom_config(tmp_path)
    assert relative in str(exc.value)


def test_read_only_font_store_does_not_create_directories(tmp_path):
    from runtime.shared_fonts import SharedFontStore
    data = tmp_path/'absent-data'
    SharedFontStore(tmp_path, {'MURAL_STUDIO_DATA_DIR': str(data)}, create=False)
    assert not data.exists()


@pytest.mark.parametrize('kind', ['css_import', 'css_image', 'svg_image', 'img_srcset'])
def test_skill_render_digest_tracks_transitive_visual_resources(tmp_path, kind):
    (tmp_path / "config.json").write_text(json.dumps({"working_language": "en", "presentation_language": "en", "target_page_count": 1}))
    core = importlib.import_module(font_bundle.__package__ + '.deck_core')
    assets = tmp_path/'assets'
    (assets/'nested').mkdir(parents=True)
    leaf = assets/'leaf.svg'
    leaf.write_text('<svg xmlns="http://www.w3.org/2000/svg"><rect fill="red"/></svg>')
    css, fragment = '', '<section>Example</section>'
    if kind == 'css_import':
        css = '@import "assets/nested/theme.css";'
        (assets/'nested/theme.css').write_text('@import "../leaf.css";')
        leaf = assets/'leaf.css'
        leaf.write_text('section {color: red;}')
    elif kind == 'css_image':
        css = '@import url("assets/nested/theme.css");'
        (assets/'nested/theme.css').write_text('section {background: url("../leaf.svg")}')
    elif kind == 'svg_image':
        (assets/'nested/plot.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg"><image href="../leaf.svg"/></svg>')
        fragment = '<section><img src="assets/nested/plot.svg"></section>'
    else:
        fragment = '<section><img srcset="assets/leaf.svg 2x"></section>'
    before = core._render_input_digest(tmp_path, css, fragment)
    leaf.write_text(leaf.read_text().replace('red', 'blue'))
    assert core._render_input_digest(tmp_path, css, fragment) != before


# The 12 mixture-v2 public families that were missing from the v1 allowlist
# (handoff §3) plus the P1 case-local design fonts we vendored (handoff §4).
MIXTURE_V2_P0_MISSING = [
    "Chakra Petch", "Cinzel", "Comic Neue", "Cormorant", "EB Garamond",
    "Marcellus", "Zilla Slab", "Lovely Little Jelly", "今年也要加油鸭",
    "叶根友钢笔行书简体", "段宁硬笔楷书粗体", "迷你简硬笔行书",
]
P1_VENDORED = [
    "Archivo Black", "IBM Plex Serif", "Press Start 2P", "Orbitron",
    "DM Serif Display", "Roboto Condensed", "VT323", "Baloo 2", "Bodoni Moda",
    "Ark Pixel", "Fusion Pixel", "Alimama FangYuanTi VF", "Alibaba PuHuiTi 3.0",
    "DingTalk JinBuTi", "Sarasa Mono SC", "Alimama ShuHeiTi", "Zpix",
]


def _ttfont(path: Path):
    from fontTools.ttLib import TTFont

    return TTFont(str(path), lazy=True)


def _internal_families(font) -> set[str]:
    """Family (nameID 1) + typographic family (nameID 16), non-empty."""
    names = font["name"]
    return {n for n in (names.getDebugName(1), names.getDebugName(16)) if n}


# ---------------------------------------------------------------------------
# §5.2 — normalize must not eat exact families.
# ---------------------------------------------------------------------------


def test_exact_builtin_first_family_is_preserved_not_collapsed() -> None:
    source = """
.a { font-family: "EB Garamond", serif; }
.b { font-family: "段宁硬笔楷书粗体", sans-serif; }
.c { font-family: "Comic Neue", cursive; }
.d { font-family: "Chakra Petch", sans-serif; }
.e { font-family: Cormorant, serif; }
"""
    normalized, count = font_bundle.normalize_font_family_declarations(source)

    # Every literal above is a shipped family: kept, never rewritten to a role.
    assert "var(--font-" not in normalized
    assert '"EB Garamond"' in normalized
    assert '"段宁硬笔楷书粗体"' in normalized
    assert '"Comic Neue"' in normalized
    assert '"Chakra Petch"' in normalized
    assert normalized == source
    assert count == 0


def test_pixel_and_cjk_parity_faces_are_not_rewritten_to_roles() -> None:
    source = """
.px { font-family: "Press Start 2P", monospace; }
.ark { font-family: "Ark Pixel", sans-serif; }
.puhui { font-family: "Alibaba PuHuiTi 3.0", sans-serif; }
.ding { font-family: "DingTalk JinBuTi", sans-serif; }
"""
    normalized, _count = font_bundle.normalize_font_family_declarations(source)

    assert "var(--font-" not in normalized
    for family in ("Press Start 2P", "Ark Pixel", "Alibaba PuHuiTi 3.0", "DingTalk JinBuTi"):
        assert f'"{family}"' in normalized


def test_alias_first_family_normalizes_to_official_family() -> None:
    # A known alias (not itself in FAMILY_FACES) folds onto its canonical family.
    source = '.t { font-family: GeistMono, monospace; }'
    normalized, count = font_bundle.normalize_font_family_declarations(source)
    assert '"Geist Mono"' in normalized
    assert "GeistMono," not in normalized  # the alias spelling is dropped
    assert count == 1


def test_platform_and_unshipped_stacks_are_not_replaced_by_roles() -> None:
    source = """
.sys { font-family: -apple-system, "Helvetica Neue", Arial, sans-serif; }
.unshipped { font-family: "Source Han Serif SC", "EB Garamond", serif; }
.code { font-family: "Menlo", "Consolas", monospace; }
"""
    normalized, count = font_bundle.normalize_font_family_declarations(source)
    assert normalized == source
    assert count == 0


def test_explicit_tokens_and_inheritance_are_preserved() -> None:
    source = """
.a { font-family: var(--font-display); }
.b { font-family: inherit; }
.c { font-family: "Inter", monospace !important; }
"""
    normalized, count = font_bundle.normalize_font_family_declarations(source)
    assert ".a { font-family: var(--font-display); }" in normalized
    assert ".b { font-family: inherit; }" in normalized
    assert normalized == source
    assert count == 0


@pytest.mark.parametrize('requested', [
    ['Unknown Brush', 'EB Garamond'], ['Unknown Serif', 'Inter'], ['Missing Cursive', 'GeistMono'],
])
def test_font_resolver_uses_declared_fallback_not_words_in_missing_name(requested):
    resolution = font_bundle.resolve_font_request(role='display', token='--font-display',
        requested_stack=requested, weight='400', style='normal', custom_registry={},
        route_default_stack=['Noto Sans SC'], source_available=lambda _family: True,
        catalog=font_bundle.substitution_catalog())
    assert resolution.resolved_family == {'GeistMono': 'Geist Mono'}.get(requested[-1], requested[-1])
    assert resolution.requested_family == requested[0]


def test_unknown_font_has_no_implicit_role_substitution():
    resolution = font_bundle.resolve_font_request(role='display', token='--font-display',
        requested_stack=['Unknown Brush Serif Cursive'], weight='400', style='normal',
        custom_registry={}, route_default_stack=['Inter'], source_available=lambda _family: True,
        catalog=font_bundle.substitution_catalog())
    assert resolution.resolution_kind == 'unresolved' and not resolution.resolved_family


def test_font_stack_expands_explicit_nested_variable_fallback_only():
    definitions = {'--font-display': 'var(--font-absent, var(--font-other, "EB Garamond", "Inter"))'}
    assert font_bundle._resolve_token_stack('--font-display', definitions) == ['EB Garamond', 'Inter']
    assert font_bundle._resolve_token_stack('--font-display', {}) == []


def test_undefined_optional_font_variable_keeps_its_declared_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(font_bundle, '_font_source_dirs', lambda: [RELEASE_FONT_DIR])
    css = ':root{--font-display:var(--font-absent, "Inter");}h1{font-family:var(--font-display);}'
    identity = font_bundle._bundle_identity(tmp_path, css, ['<h1>Example</h1>'])
    assert identity[2]['--font-display'][0] == 'Inter'
    assert '--font-absent' not in identity[2]


def test_missing_literal_font_reports_available_choices(tmp_path, monkeypatch):
    monkeypatch.setattr(font_bundle, '_font_source_dirs', lambda: [RELEASE_FONT_DIR])
    with pytest.raises(ValueError, match='font_stack_unavailable.*Unknown Brush.*choose available'):
        font_bundle._bundle_identity(tmp_path, ':root{--font-body:"Inter";}',
            ['<p style="font-family:Unknown Brush">Hello</p>'])


def test_font_name_in_prose_is_not_a_font_selection(tmp_path, monkeypatch):
    monkeypatch.setattr(font_bundle, '_font_source_dirs', lambda: [RELEASE_FONT_DIR])
    identity = font_bundle._bundle_identity(tmp_path, ':root{--font-body:"Inter";}',
        ['<p>The source mentions EB Garamond, but this paragraph uses Inter.</p>'])
    assert 'EB Garamond' not in identity[5]
    brief = tmp_path / 'deck.md'
    brief.write_text('Do not choose EB Garamond or --font-write-cursive.')
    assert font_bundle._plan_font_fragment(brief) == ''


@pytest.mark.parametrize('language', [None, '', 'auto', 'unknown'])
def test_planning_requires_model_to_choose_delivery_language_in_config(tmp_path, language):
    core = _font_plan_workspace(tmp_path, presentation=language)
    with pytest.raises(ValueError, match='language'):
        core._load_plans(tmp_path, 1)


@pytest.mark.parametrize('started,current', [('auto', 'zh'), ('auto', 'en'), ('zh', 'zh'), ('en', 'zh')])
def test_plan_compilation_uses_current_config_without_reapplying_start_language(tmp_path, started, current):
    core = _font_plan_workspace(tmp_path, presentation=current)
    startup = json.dumps({'language': started})
    (tmp_path / '_state/run.json').write_text(startup)
    (tmp_path / 'deck.md').write_text('# Deck\n## Overall design\n- language: en\n')
    assert core._load_plans(tmp_path, 1)[0]['language'] == current
    assert (tmp_path / '_state/run.json').read_text() == startup


# ---------------------------------------------------------------------------
# Regression 1 — mixture-v2 public families are a subset of the manifest.
# ---------------------------------------------------------------------------


def test_mixture_v2_families_are_subset_of_official_manifest() -> None:
    allow = set(font_bundle.FAMILY_FACES)
    import json

    lock = json.loads((INTERNAL / "FONT-MANIFEST.lock.json").read_text(encoding="utf-8"))
    locked = set(lock["families"])
    for family in MIXTURE_V2_P0_MISSING + P1_VENDORED:
        assert family in allow, f"{family} missing from FAMILY_FACES"
        assert family in locked, f"{family} missing from the SHA lock"
    # Cormorant and Cormorant Garamond are DISTINCT families, both present.
    assert "Cormorant" in allow and "Cormorant Garamond" in allow


# ---------------------------------------------------------------------------
# Regression 2 — every new file opens; internal family matches the CSS family.
# ---------------------------------------------------------------------------


def test_new_faces_open_and_internal_family_matches_css_family() -> None:
    # A handful of families have honestly-garbled or Chinese internal names; the
    # lock records the true internal_family and we compare against THAT, not a
    # naive equality with the CSS key.
    import json

    lock = json.loads((INTERNAL / "FONT-MANIFEST.lock.json").read_text(encoding="utf-8"))
    # The 叶根友 file has a corrupt internal name table ("∂∏˘"); we accept the
    # honest recorded value rather than forcing a match.
    corrupt_internal_name = {"叶根友钢笔行书简体"}
    for family in MIXTURE_V2_P0_MISSING + P1_VENDORED:
        for face in font_bundle.FAMILY_FACES[family]:
            source = None
            for name in face.source_names:
                cand = RELEASE_FONT_DIR / name
                if cand.is_file():
                    source = cand
                    break
            assert source is not None, f"{family}: no file on disk"
            font = _ttfont(source)
            try:
                internal = _internal_families(font)
            finally:
                font.close()
            locked_face = next(
                f for entry in [lock["families"][family]] for f in entry["faces"]
                if f["file"] == source.name
            )
            assert locked_face["internal_family"] in internal or family in corrupt_internal_name
            if family not in corrupt_internal_name:
                # The CSS family or its recorded internal family must be a real
                # name inside the font's name table.
                assert internal, f"{family}: empty internal name table"


# ---------------------------------------------------------------------------
# Regression 3 — Comic Neue is no longer corrupt.
# ---------------------------------------------------------------------------


def test_comic_neue_is_a_valid_font_now() -> None:
    path = RELEASE_FONT_DIR / "ComicNeue-Regular.ttf"
    assert path.is_file()
    # The old file was 87724 bytes and raised "bad sfntVersion"; the replacement
    # opens cleanly and reports the right family.
    font = _ttfont(path)
    try:
        assert font["name"].getDebugName(1) == "Comic Neue"
        covered: set[int] = set()
        for table in font["cmap"].tables:
            covered.update(table.cmap)
    finally:
        font.close()
    assert all(ord(ch) in covered for ch in "AaGg09")
    assert font_bundle._font_source_readable(str(path)) is True


# ---------------------------------------------------------------------------
# Regression 4 — exact builtin is NOT normalized to a generic role.
# (This is the direct anti-regression for the old behaviour the handoff calls
# out: EB Garamond used to become --font-serif.)
# ---------------------------------------------------------------------------


def test_eb_garamond_is_not_rewritten_to_a_role() -> None:
    normalized, _count = font_bundle.normalize_font_family_declarations(
        '.title { font-family: "EB Garamond"; }'
    )
    assert "var(--font-serif)" not in normalized
    assert '"EB Garamond"' in normalized


# ---------------------------------------------------------------------------
# Regression 5 — a new font survives subsetting with family/weight/style/glyphs.
# ---------------------------------------------------------------------------


def test_new_font_bundles_end_to_end_with_correct_family_and_glyphs(
    tmp_path: Path,
) -> None:
    import shutil

    if not shutil.which("pyftsubset"):
        pytest.skip("pyftsubset is required for the subset regression")

    base_css = (SKILL / "assets/base.css").read_text(encoding="utf-8")
    # Force EB Garamond (a new variable serif) onto the display role literally.
    css = base_css + "\n:root { --font-display: \"EB Garamond\", \"Noto Serif SC\", serif; }\n"
    (tmp_path / "base.css").write_text(css, encoding="utf-8")
    (tmp_path / "slides").mkdir()
    (tmp_path / "slides/slide_01.html").write_text(
        '<section class="slide" data-slide="01">'
        '<h1 style="font-family: var(--font-display)">EB Garamond 衬线标题 Aa Gg 09</h1>'
        "</section>",
        encoding="utf-8",
    )

    manifest = font_bundle.bundle_workspace(tmp_path)

    assert "EB Garamond" in manifest["delivery_families"]
    assert not font_bundle.validate_font_bundle(tmp_path)
    eb_faces = [f for f in manifest["faces"] if f["source_family"] == "EB Garamond"]
    assert eb_faces, "EB Garamond was not bundled"
    for face in eb_faces:
        assert face["weight"] == "400 800"  # real wght axis, preserved
        assert face["style"] == "normal"
        out = tmp_path / face["path"]
        assert out.is_file() and out.stat().st_size
        assert hashlib.sha256(out.read_bytes()).hexdigest() == face["sha256"]
        # The renamed subset keeps a Latin range and reports the deck family.
        subset = _ttfont(out)
        try:
            assert face["delivery_family"] in _internal_families(subset)
            covered: set[int] = set()
            for table in subset["cmap"].tables:
                covered.update(table.cmap)
        finally:
            subset.close()
        assert all(ord(ch) in covered for ch in "AaGg09")


# ---------------------------------------------------------------------------
# Regression 6 — a CJK display face auto-appends Noto, primary family unchanged.
# ---------------------------------------------------------------------------


def test_cjk_display_face_appends_noto_without_changing_primary(
    tmp_path: Path,
) -> None:
    import shutil

    if not shutil.which("pyftsubset"):
        pytest.skip("pyftsubset is required for the CJK fallback regression")

    base_css = (SKILL / "assets/base.css").read_text(encoding="utf-8")
    # DingTalk JinBuTi covers Han but is NOT a complete CJK fallback.
    css = base_css + "\n:root { --font-display: \"DingTalk JinBuTi\", \"Noto Sans SC\", sans-serif; }\n"
    (tmp_path / "base.css").write_text(css, encoding="utf-8")
    (tmp_path / "slides").mkdir()
    (tmp_path / "slides/slide_01.html").write_text(
        '<section class="slide" data-slide="01">'
        '<h1 style="font-family: var(--font-display)">钉钉进步体 罕见字 fallback</h1>'
        "</section>",
        encoding="utf-8",
    )

    manifest = font_bundle.bundle_workspace(tmp_path)

    display_stack = manifest["token_stacks"]["--font-display"]
    assert display_stack[0] == "DingTalk JinBuTi"  # primary unchanged
    assert any(f in font_bundle._COMPLETE_CJK_FALLBACKS for f in display_stack)
    assert "Noto Sans SC" in manifest["delivery_families"]
    assert not font_bundle.validate_font_bundle(tmp_path)


# ---------------------------------------------------------------------------
# Regression 7 — WebUI/infer/synthesis share one font source fingerprint.
# ---------------------------------------------------------------------------


def test_release_and_route_audit_expose_a_stable_source_fingerprint() -> None:
    release = font_bundle.audit_release_fonts()
    routes = font_bundle.audit_typography_routes()
    assert release["ready"] is True, release["missing"]
    assert routes["ready"] is True, routes["missing"]
    assert routes["source_fingerprint"], "route audit must expose a fingerprint"
    # The fingerprint is deterministic across calls (same worker, same bytes).
    again = font_bundle.audit_typography_routes()
    assert again["source_fingerprint"] == routes["source_fingerprint"]


# ---------------------------------------------------------------------------
# Gate — the allowlist now accepts non-OFL parity fonts with honest metadata,
# but still rejects a family with missing source / usage-scope.
# ---------------------------------------------------------------------------


def test_allowlist_gate_is_source_plus_scope_not_ofl_only() -> None:
    # Passes as-is with the mixture-v2 parity fonts present.
    font_bundle._validate_font_allowlist()
    # Non-OFL families are allowed and record their true license, never "OFL-1.1".
    for family in ("Zpix", "Alibaba PuHuiTi 3.0", "叶根友钢笔行书简体"):
        meta = font_bundle.FONT_LICENSES[family]
        assert meta["license"] and meta["license"] != "OFL-1.1"
        assert meta["source"]
        assert font_bundle._font_usage_scope(family)


def test_allowlist_gate_rejects_missing_scope_or_source(monkeypatch) -> None:
    broken = dict(font_bundle.FONT_LICENSES)
    broken["EB Garamond"] = {"license": "OFL-1.1", "source": ""}
    monkeypatch.setattr(font_bundle, "FONT_LICENSES", broken)
    font_bundle._font_usage_scope.cache_clear() if hasattr(
        font_bundle._font_usage_scope, "cache_clear"
    ) else None
    with pytest.raises(RuntimeError):
        font_bundle._validate_font_allowlist()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
