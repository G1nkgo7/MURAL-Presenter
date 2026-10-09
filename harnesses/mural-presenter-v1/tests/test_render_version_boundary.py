"""渲染期间修改输入，不能把旧像素登记成新资源版本。"""
import hashlib
import importlib
import json
from pathlib import Path

from PIL import Image
import pytest

from runtime.shared_objects import workspace_skill_module
from test_editable_pptx import browser_environment
from test_skill_canvas_contract import _author_workspace


@pytest.mark.parametrize("status", [200, 503])
def test_remote_font_capture_publication_and_cache(tmp_path, monkeypatch, browser_environment, status):
    from test_v1_font_pipeline import font_bundle, RELEASE_FONT_DIR

    _author_workspace(tmp_path)
    core = workspace_skill_module(tmp_path, "deck_core")
    renderer_module = workspace_skill_module(tmp_path, "render_deck")
    remote = importlib.import_module(renderer_module.__package__ + ".remote_resources")
    monkeypatch.setattr(core, "_load_plans", lambda *_, pages=None: ({"language": "en"}, [{"number": 1}]))
    monkeypatch.setattr(core, "_ensure_authoring_font_bundle", lambda *_, pages=None: {})
    url = "https://cdn.example/remote-font.ttf"
    font = font_bundle._find_source(font_bundle.FAMILY_FACES["Inter"][0], [RELEASE_FONT_DIR]).read_bytes()
    calls = []
    def fetch(reference, **_kwargs):
        assert reference == url
        calls.append(reference)
        body = font if status == 200 else b"temporarily unavailable"
        return {"status": status, "headers": {"content-type": "font/ttf", "access-control-allow-origin": "*"},
                "body": body, "sha256": hashlib.sha256(body).hexdigest()}
    monkeypatch.setattr(remote, "fetch", fetch)
    css = tmp_path / "base.css"
    css.write_text(css.read_text() + f'''
      @font-face{{font-family:"Remote Font";font-weight:100 900;src:url("{url}")}}
      html,body{{margin:0}}h1{{font-family:"Remote Font";font-weight:700;font-size:64px}}
    ''')
    (tmp_path / "slides/slide_01.html").write_text(
        '<section class="slide" data-slide="01"><h1>Remote font remains bold</h1></section>')
    def run(command):
        renderer = renderer_module.DeckRenderer(Path(command[4]), Path(command[5]), workspace=tmp_path, motion_check=False)
        renderer.start()
        try:
            renderer.render_page(1)
            if status == 200:
                assert renderer._page.evaluate('document.fonts.check(\'700 64px "Remote Font"\')')
                assert renderer._page.locator("h1").evaluate("e => getComputedStyle(e).fontWeight") == "700"
            renderer.finalize()
        finally:
            renderer.close()
    monkeypatch.setattr(core, "_run_renderer", run)
    if status != 200:
        with pytest.raises(ValueError, match="render_resource_unavailable.*Remote Font"):
            core.render(tmp_path, 1, 1)
        assert not (tmp_path / "renders/slide_01.png").exists()
        return
    assert core.render(tmp_path, 1, 1)
    evidence = core._page_render_metadata(tmp_path, 1)
    assert evidence["input_hashes"][url] == hashlib.sha256(font).hexdigest()
    assert url in evidence["loaded_resources"] and not evidence["failed_resources"]
    count = len(calls)
    assert count > 0
    monkeypatch.setattr(core, "_run_renderer", lambda *_: pytest.fail("unchanged page must use its capture"))
    assert core.render(tmp_path, 1, 1)
    assert len(calls) == count
    assert core._page_render_metadata(tmp_path, 1) == evidence


@pytest.mark.parametrize("surface", ["image", "css", "html"])
def test_real_render_rejects_inputs_changed_after_browser_loaded_them(tmp_path, monkeypatch, browser_environment, surface):
    _author_workspace(tmp_path)
    core = workspace_skill_module(tmp_path, "deck_core")
    renderer_module = workspace_skill_module(tmp_path, "render_deck")
    monkeypatch.setattr(core, "_load_plans", lambda *_, pages=None: ({"language": "en"}, [{"number": 1}]))
    monkeypatch.setattr(core, "_ensure_authoring_font_bundle", lambda *_, pages=None: {})
    css = tmp_path / "base.css"
    css.write_text(css.read_text() + '\nhtml,body{margin:0} .slide{background:white}\n')
    picture = tmp_path / "assets/picture.png"
    Image.new("RGB", (64, 48), "navy").save(picture)
    html = tmp_path / "slides/slide_01.html"
    html.write_text('''<section class="slide" data-slide="01"><h1>Current version</h1>
      <img id="photo" style="width:600px;height:450px"><script>
      document.getElementById('photo').src=['assets','picture.png'].join('/');</script></section>''')
    observed_body_hashes = []
    original_hash = hashlib.sha256(picture.read_bytes()).hexdigest()
    invocations = []
    def run(command):
        invocations.append(command)
        disturb = len(invocations) == 1
        renderer = renderer_module.DeckRenderer(Path(command[4]), Path(command[5]), workspace=tmp_path, motion_check=False)
        finished = renderer._request_finished
        def after_load(request):
            finished(request)
            if request.url.split('?', 1)[0] == picture.as_uri():
                observed_body_hashes.append(hashlib.sha256(request.response().body()).hexdigest())
                if disturb and surface == "image":
                    Image.new("RGB", (64, 48), "orange").save(picture)
        renderer._request_finished = after_load
        renderer.start()
        try:
            renderer.render_page(1)
            renderer.finalize()
            if disturb and surface == "css":
                css.write_text(css.read_text() + '\nh1{color:red}\n')
            elif disturb and surface == "html":
                html.write_text(html.read_text().replace("Current version", "Changed version"))
        finally:
            renderer.close()
    monkeypatch.setattr(core, "_run_renderer", run)
    with pytest.raises((ValueError, RuntimeError), match="render.*(changed|version|source|resources)"):
        core.render(tmp_path, 1, 1)
    assert observed_body_hashes == [original_hash]
    assert not (tmp_path / "renders/slide_01.png").exists()
    state = json.loads((tmp_path / "_trace/slide-render-states/page_01.json").read_text())
    assert not state["hashes"]
    assert core.render(tmp_path, 1, 1)
    evidence = core._page_render_metadata(tmp_path, 1)
    for path in (css, html, picture):
        assert evidence["input_hashes"][path.relative_to(tmp_path).as_posix()] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert core.render(tmp_path, 1, 1)
    assert len(invocations) == 2
    assert len(json.loads((tmp_path / "_trace/slide-render-states/page_01.json").read_text())["hashes"]) == 1


@pytest.mark.parametrize("surface", ["image", "css", "html", "png"])
def test_harness_rejects_drift_between_renderer_return_and_receipt_commit(tmp_path, surface):
    from state.artifacts import ArtifactStateError, ArtifactStore
    from state.workspace import PageLifecycle, WorkspaceState, file_sha256
    from test_workspace_artifacts import _author_css, _commit_page, _register_image, _write_png

    _author_css(tmp_path)
    workspace = WorkspaceState(tmp_path)
    store = ArtifactStore(tmp_path, workspace_state=workspace)
    workspace.register_pages([1])
    image = tmp_path / "assets/picture.png"
    Image.new("RGB", (32, 24), "navy").save(image)
    _register_image(store, image)
    html = workspace.paths.page_html(1)
    html.write_text('<h1>Current</h1><img src="assets/picture.png">')
    _commit_page(workspace, store, page=1)
    css = tmp_path / "base.css"

    def renderer(_html, png, canvas):
        _write_png(png)
        evidence = {"geometry": canvas, "loaded_resources": ["assets/picture.png"], "failed_resources": [],
                    "input_hashes": {p.relative_to(tmp_path).as_posix(): file_sha256(p) for p in (html, css, image)},
                    "png_sha256": file_sha256(png)}
        if surface == "image":
            Image.new("RGB", (32, 24), "orange").save(image)
            _register_image(store, image)
        elif surface == "css":
            css.write_text(css.read_text() + '\nh1{color:red}\n')
        elif surface == "html":
            html.write_text(html.read_text().replace("Current", "Changed"))
        else:
            _write_png(png, color=(255, 128, 0))
        return evidence

    with pytest.raises(ArtifactStateError, match="render (inputs|pixels) changed"):
        store.render_pages([1], renderer)
    assert workspace.page(1).lifecycle not in {PageLifecycle.RENDERED, PageLifecycle.INSPECTED, PageLifecycle.READY}
    assert store.render_receipt(1) == {}


@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt])
def test_interrupted_capture_does_not_consume_repair_state_and_can_retry(tmp_path, monkeypatch, error):
    from test_skill_canvas_contract import _render_fixture

    _author_workspace(tmp_path)
    core = workspace_skill_module(tmp_path, "deck_core")
    monkeypatch.setattr(core, "_load_plans", lambda *_, pages=None: ({"language": "en"}, [{"number": 1}]))
    monkeypatch.setattr(core, "_ensure_authoring_font_bundle", lambda *_, pages=None: {})
    (tmp_path / "slides/slide_01.html").write_text('<section class="slide" data-slide="01"><h1>Retry</h1></section>')
    def interrupted(_command):
        raise error("capture interrupted")
    monkeypatch.setattr(core, "_run_renderer", interrupted)
    with pytest.raises(error, match="capture interrupted"):
        core.render(tmp_path, 1, 1)
    state_path = tmp_path / "_trace/slide-render-states/page_01.json"
    assert not json.loads(state_path.read_text())["hashes"]
    assert not (tmp_path / "renders/.page_01").exists()
    monkeypatch.setattr(core, "_run_renderer", lambda command: _render_fixture(command, b"pixels"))
    assert core.render(tmp_path, 1, 1)
    assert len(json.loads(state_path.read_text())["hashes"]) == 1


@pytest.mark.parametrize("kind", ["absolute", "symlink"])
def test_dynamic_file_request_cannot_read_outside_workspace(tmp_path, monkeypatch, browser_environment, kind):
    root = tmp_path / "deck"
    _author_workspace(root)
    css = root / "base.css"
    css.write_text(css.read_text() + '\nhtml,body{margin:0}\n')
    core = workspace_skill_module(root, "deck_core")
    renderer_module = workspace_skill_module(root, "render_deck")
    monkeypatch.setattr(core, "_load_plans", lambda *_, pages=None: ({"language": "en"}, [{"number": 1}]))
    monkeypatch.setattr(core, "_ensure_authoring_font_bundle", lambda *_, pages=None: {})
    outside = tmp_path / "outside.png"
    Image.new("RGB", (32, 24), "orange").save(outside)
    reference = outside.as_uri()
    if kind == "symlink":
        (root / "assets/linked.png").symlink_to(outside)
        reference = "assets/linked.png"
    (root / "slides/slide_01.html").write_text('<section class="slide" data-slide="01"><h1>Bounded resources</h1>'
        '<img id="photo"><script>document.getElementById("photo").src=' + json.dumps(reference) + ';</script></section>')
    read_bytes = Path.read_bytes
    def bounded_read(path):
        if path.resolve() == outside.resolve():
            pytest.fail("renderer read an unapproved file before checking its scope")
        return read_bytes(path)
    monkeypatch.setattr(Path, "read_bytes", bounded_read)
    def render(command):
        renderer = renderer_module.DeckRenderer(Path(command[4]), Path(command[5]), workspace=root, motion_check=False)
        renderer.start()
        try:
            renderer.render_page(1)
            renderer.finalize()
        finally:
            renderer.close()
    monkeypatch.setattr(core, "_run_renderer", render)
    with pytest.raises(ValueError, match="render_resource_unavailable"):
        core.render(root, 1, 1)
    assert not (root / "renders/slide_01.png").exists()
    assert not json.loads((root / "_trace/slide-render-states/page_01.json").read_text())["hashes"]
