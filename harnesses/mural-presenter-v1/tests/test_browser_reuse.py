"""真实 Chromium 复用回归；不以 mock 截图或 sleep 模拟提速。"""
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from PIL import Image
from playwright.sync_api import sync_playwright
import pytest

from geometry_fixture import geometry, write_geometry
from runtime.diagnostics import CommandFailed, run_command
from runtime.shared_objects import workspace_skill_module


@pytest.fixture
def browser_workspace(tmp_path):
    write_geometry(tmp_path, geometry(width_px=800, height_px=600))
    core = workspace_skill_module(tmp_path, "deck_core")
    browser = workspace_skill_module(tmp_path, "browser_host")
    css = (tmp_path / "base.css").read_text() + (
        ".deck{position:absolute;width:var(--canvas-w);height:var(--canvas-h)}"
        "body{margin:0}.slide{position:absolute;visibility:hidden;background:#123456;color:#fff}"
        ".slide.active{visibility:visible}h1{font:72px sans-serif;padding:40px}"
        ".bar{margin:50px;width:350px;height:60px;background:#f28c28}"
    )
    (tmp_path / "base.css").write_text(css)
    fragments = [(page, f'<section class="slide" data-slide="{page:02d}">'
                  f'<h1>Page {page}</h1><div class="bar"></div></section>') for page in (1, 2)]
    (tmp_path / "present.html").write_text(core._document(css, fragments, "en"))
    (tmp_path / "deck.md").write_text("# Browser reuse\n## Brief\n- language: en\n")
    (tmp_path / "briefs").mkdir()
    for page, fragment in fragments:
        (tmp_path / f"briefs/slide_{page:02d}.md").write_text(
            f"# P{page:02d} — Page {page}\n- job: explain\n- must_show: Page {page}\n")
        (tmp_path / f"slides/slide_{page:02d}.html").write_text(fragment)
    scratch = tmp_path / "_runtime/tmp/system"
    scratch.mkdir(parents=True, exist_ok=True)
    environment = {**os.environ, "TMPDIR": str(scratch), "TMP": str(scratch), "TEMP": str(scratch),
                   "PPT_RENDER_LOCK_PATH": str(tmp_path / "_state/render.lock"),
                   "PPT_RENDER_LOCK_DIR": str(tmp_path / "_state/render-slots"), "PPT_RENDER_CONCURRENCY": "2"}
    environment.pop(browser.ENDPOINT_ENV, None)
    host = browser.BrowserHost(tmp_path, environment)
    try:
        yield tmp_path, browser, host, environment
    finally:
        host.close()


def _pixels(path):
    with Image.open(path) as image:
        return image.size, image.convert("RGBA").tobytes()


def test_real_resource_readiness_does_not_wait_for_network_idle(browser_workspace, monkeypatch):
    root, _, _, environment = browser_workspace
    for key in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(key, environment[key])
    module = workspace_skill_module(root, "render_deck")
    renderer = module.DeckRenderer(root / "present.html", root / "renders", workspace=root)
    try:
        renderer.start()
        renderer._load()
        pending = [request.url for request, status in renderer._requests.items() if status == "pending"]
        report = {"timings_s": renderer.meta["timings_s"], "pending": pending,
                  "ready_state": renderer._page.evaluate("document.readyState"),
                  "resources": renderer._resource_receipt()}
        (root / "_trace/resource-readiness.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report))
        assert pending == []
        assert report["ready_state"] == "complete"
        assert report["timings_s"]["resource_wait"] < 2
    finally:
        renderer.close()


def test_real_load_readiness_preserves_assets_without_waiting_for_background_polling(browser_workspace, monkeypatch):
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from state.workspace import file_sha256
    root, _, _, environment = browser_workspace
    for key in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(key, environment[key])
    asset = root / "assets/hero.png"
    Image.new("RGB", (40, 30), "orange").save(asset)
    css = root / "assets/fixture.css"
    css.write_text("body { background: rgb(18, 52, 86); }")
    (root / "present.html").write_text('<html><head><link rel="stylesheet" href="assets/fixture.css"></head>'
        '<body><img src="assets/hero.png"><script>'
        'window.poll=setInterval(()=>fetch("https://render.fixture/pulse").catch(()=>{}),50);'
        '</script></body></html>')
    module = workspace_skill_module(root, "render_deck")
    renderer = module.DeckRenderer(root / "present.html", root / "renders", workspace=root)
    try:
        renderer.start()
        renderer._context.route("https://render.fixture/**", lambda route: route.fulfill(
            status=200, headers={"Access-Control-Allow-Origin": "*"}, body="ok"))
        renderer._load()
        assert renderer._page.evaluate("document.images[0].complete && document.images[0].naturalWidth") == 40
        assert renderer._page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(18, 52, 86)"
        assert renderer.meta["resource_wait"]["timed_out"] is False
        assert renderer.meta["timings_s"]["resource_wait"] < 2
        before = time.monotonic()
        with pytest.raises(PlaywrightTimeoutError):
            renderer._page.wait_for_load_state("networkidle", timeout=8000)
        legacy_wait_s = time.monotonic() - before
        renderer._page.evaluate("clearInterval(window.poll)")
        renderer._page.wait_for_load_state("networkidle", timeout=2000)
        receipt = renderer._resource_receipt()
        assert not receipt["failed_resources"]
        assert receipt["input_hashes"]["assets/hero.png"] == file_sha256(asset)
        assert receipt["input_hashes"]["assets/fixture.css"] == file_sha256(css)
        report = {"load_wait_s": renderer.meta["timings_s"]["resource_wait"],
                  "networkidle_wait_s": legacy_wait_s, "resources": receipt}
        (root / "_trace/resource-wait-comparison.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report))
    finally:
        renderer.close()


def test_real_load_readiness_still_records_missing_images(browser_workspace, monkeypatch):
    root, _, _, environment = browser_workspace
    for key in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(key, environment[key])
    (root / "present.html").write_text('<html><body><img src="assets/missing.png"></body></html>')
    module = workspace_skill_module(root, "render_deck")
    renderer = module.DeckRenderer(root / "present.html", root / "renders", workspace=root)
    try:
        renderer.start()
        renderer._load()
        assert "assets/missing.png" in renderer._resource_receipt()["failed_resources"]
        assert renderer._page.evaluate("document.images[0].naturalWidth") == 0
    finally:
        renderer.close()


def _render(workspace, name, *, shared=False, all_pages=False):
    started = time.monotonic()
    root, browser, host, environment = workspace
    env = dict(environment)
    host_ready = time.monotonic()
    if shared:
        env[browser.ENDPOINT_ENV] = host.acquire(time.monotonic() + 45)["endpoint"]
    host_ready_s = time.monotonic() - host_ready
    source = Path(workspace_skill_module(root, "render_deck").__file__)
    output = root / "_runtime/tmp" / name
    output.mkdir()
    command = [sys.executable, str(source), "--workspace", str(root), str(root / "present.html"),
               str(output), "--no-motion-check", *( ["--all"] if all_pages else ["--page", "1"])]
    command_started = time.monotonic()
    result = run_command(command, cwd=root, timeout_s=60, environment=env)
    command_elapsed_s = time.monotonic() - command_started
    meta = json.loads((output / "render.json").read_text())
    return {"mode": "shared" if shared else "cold", "all_pages": all_pages,
            "elapsed_s": time.monotonic() - started, "host_ready_s": host_ready_s,
            "command_elapsed_s": command_elapsed_s, "meta": meta, "output": str(output),
            "diagnostics": result.diagnostics}


def test_real_cold_and_reused_single_and_multi_page_pixels(browser_workspace):
    rows = []
    for all_pages in (False, True):
        tag = "multi" if all_pages else "single"
        cold = _render(browser_workspace, "cold-" + tag, all_pages=all_pages)
        shared = _render(browser_workspace, "shared-" + tag, shared=True, all_pages=all_pages)
        again = _render(browser_workspace, "again-" + tag, shared=True, all_pages=all_pages)
        rows.extend([cold, shared, again])
        assert cold["meta"]["browser_connection"] == "cold_launch"
        assert shared["meta"]["browser_connection"] == again["meta"]["browser_connection"] == "shared_cdp"
        for page in range(1, 3 if all_pages else 2):
            name = f"slide_{page:02d}.png"
            expected = _pixels(Path(cold["output"]) / name)
            assert expected[0] == (800, 600)
            assert expected == _pixels(Path(shared["output"]) / name) == _pixels(Path(again["output"]) / name)
        for row in (cold, shared, again):
            assert {"playwright_start", "browser_connect_or_launch", "context_create", "navigation",
                    "resource_wait", "font_ready", "settle", "screenshot", "geometry_check"} <= row["meta"]["timings_s"].keys()
    report = browser_workspace[0] / "_trace/browser-reuse-benchmark.json"
    report.write_text(json.dumps(rows, indent=2))
    print(f"BROWSER_REUSE_BENCHMARK={report}")


@pytest.mark.parametrize("early_capture_s", [.1, .8, 3.5])
@pytest.mark.parametrize("already_waited_ms", [0, 2000])
def test_motion_final_deadline_includes_early_capture_time(browser_workspace, monkeypatch,
                                                          early_capture_s, already_waited_ms):
    from types import SimpleNamespace

    root, *_ = browser_workspace
    module = workspace_skill_module(root, "render_deck")
    elapsed = [already_waited_ms / 1000]
    waits, final_frames = [], []
    renderer = module.DeckRenderer.__new__(module.DeckRenderer)
    renderer.motion_check = True

    def settle(milliseconds):
        assert milliseconds >= 0
        waits.append(milliseconds)
        elapsed[0] += milliseconds / 1000

    def screenshot():
        elapsed[0] += early_capture_s
        return b"fixture pixels"

    def final_frame():
        final_frames.append(elapsed[0])
        return b"fixture pixels", 5, (), False, False

    renderer._settle, renderer._screenshot = settle, screenshot
    renderer._grab_with_blank_retry = final_frame
    renderer._layout_geometry = renderer._resource_receipt = lambda: {}
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    monkeypatch.setattr(module, "_motion_blocks", lambda _png: ())
    renderer._sample_page(already_waited_ms)
    expected = max(module.FINAL_CAPTURE_MS / 1000,
                   max(already_waited_ms, module.EARLY_MS) / 1000 + early_capture_s)
    assert final_frames == pytest.approx([expected])
    assert waits[-1] == module.LIVE_EXTRA_MS


def test_real_font_readiness_uses_author_fonts_without_loading_unused_fallback(browser_workspace, monkeypatch):
    root, _browser, _host, environment = browser_workspace
    for key in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(key, environment[key])
    module = workspace_skill_module(root, "render_deck")
    source = Path(__file__).resolve().parents[3] / "fonts/BebasNeue-Regular.ttf"
    (root / "assets").mkdir(exist_ok=True)
    font = root / "assets/used.ttf"
    font.write_bytes(source.read_bytes())
    (root / "assets/unused.ttf").write_bytes(font.read_bytes())
    present = root / "present.html"
    present.write_text('<style>' + (root / 'base.css').read_text() + '''
      @font-face{font-family:UsedFace;src:url(assets/used.ttf)}
      @font-face{font-family:"Noto Sans SC";src:url(assets/unused.ttf);font-weight:700}
      h1{font:72px UsedFace}
      </style><section class="slide active" data-slide="01"><h1>Visible title</h1></section>''')
    renderer = module.DeckRenderer(present, root / "renders", workspace=root, motion_check=False)
    try:
        renderer.start()
        renderer._load()
        renderer._wait_fonts()
        assert renderer._page.evaluate("document.fonts.status") == "loaded"
        assert "UsedFace" in renderer.loaded_font_families
        assert '"Noto Sans SC"' not in renderer.loaded_font_families
        receipt = renderer._resource_receipt()
        assert "assets/used.ttf" in receipt["loaded_resources"]
        assert "assets/unused.ttf" not in receipt["loaded_resources"]
        assert not receipt["failed_resources"]
    finally:
        renderer.close()


def test_real_target_activation_precedes_font_readiness(browser_workspace, monkeypatch):
    root, _browser, _host, environment = browser_workspace
    for key in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(key, environment[key])
    module = workspace_skill_module(root, "render_deck")
    source = Path(__file__).resolve().parents[3] / "fonts/BebasNeue-Regular.ttf"
    target_font = root / "assets/target.ttf"
    target_font.write_bytes(source.read_bytes())
    present = root / "present.html"
    present.write_text('<style>' + (root / 'base.css').read_text() + '''
      @font-face{font-family:TargetFace;src:url(assets/target.ttf)}
      .slide{display:none}.slide.active{display:block}
      .slide[data-slide="02"] h1{font-family:TargetFace}
      </style><section class="slide active" data-slide="01"><h1>First page</h1></section>
      <section class="slide" data-slide="02"><h1>Target page</h1></section>
      <script>window.cleanDeck={go(n){document.querySelectorAll('.slide').forEach((el,i)=>
          el.classList.toggle('active',i===n-1))}}</script>''')
    renderer = module.DeckRenderer(present, root / "renders", workspace=root, motion_check=False)
    sample = renderer._sample_page

    def checked_sample(*args, **kwargs):
        assert "TargetFace" in renderer.loaded_font_families
        assert renderer._page.evaluate("document.fonts.status") == "loaded"
        return sample(*args, **kwargs)

    renderer._sample_page = checked_sample
    try:
        renderer.start()
        renderer._load("?slide=2")
        assert "assets/target.ttf" not in renderer._resource_receipt()["loaded_resources"]
        renderer.render_page(2)
        assert "assets/target.ttf" in renderer._resource_receipt()["loaded_resources"]
        assert not renderer._resource_receipt()["failed_resources"]
    finally:
        renderer.close()


def test_routed_pinned_font_hashes_only_the_bytes_it_sends(browser_workspace, monkeypatch):
    import hashlib
    from types import SimpleNamespace

    root, *_ = browser_workspace
    renderer_module = workspace_skill_module(root, "render_deck")
    font_module = workspace_skill_module(root, "font_bundle")
    store = root.parent / (root.name + "-fonts")
    data = b"pinned font fixture"
    digest = hashlib.sha256(data).hexdigest()
    reference = f"shared/fonts/objects/{digest}.ttf"
    font = store / reference
    font.parent.mkdir(parents=True)
    font.write_bytes(data)
    (root / "_state/font-manifest.json").write_text(json.dumps({
        "schema": "mural.font-manifest.v2", "studio_data_root": str(store),
        "source_faces": [{"scope": "public", "object_ref": reference, "sha256": digest}]}))
    renderer = renderer_module.DeckRenderer.__new__(renderer_module.DeckRenderer)
    renderer.workspace = root.resolve()
    renderer._response_hashes = {}
    renderer.console_errors = []
    request = type("Request", (), {"url": font.as_uri()})()
    received, reads, aborts = [], [], []
    route = SimpleNamespace(request=request, fulfill=lambda **kw: received.append(kw),
                            abort=lambda: aborts.append(True))
    read_bytes = Path.read_bytes

    def read(path):
        if path == font:
            reads.append(path)
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    renderer._serve_file(route)
    assert reads == [font]
    assert received[0]["body"] == data and renderer._response_hashes[request] == digest
    assert not aborts
    font.write_bytes(b"replaced after pinning")
    with pytest.raises(ValueError, match="hash mismatch"):
        font_module.resolve_shared_font_reference(root, font.as_uri())
    renderer._serve_file(route)
    assert aborts == [True] and len(received) == 1
    assert "pinned font changed before capture" in renderer.console_errors[-1]


def test_real_parallel_contexts_and_disconnect_do_not_share_state(browser_workspace):
    root, _module, host, _env = browser_workspace
    ready = host.acquire(time.monotonic() + 45)
    barrier = threading.Barrier(2)
    first_closed = threading.Event()
    def render_client(index):
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(ready["endpoint"])
            context = browser.new_context()
            page = context.new_page()
            page.goto((root / "present.html").as_uri())
            page.evaluate("value => localStorage.setItem('isolated', value)", str(index))
            barrier.wait(timeout=10)
            if index == 1:
                context.close()
                browser.close()
                first_closed.set()
            else:
                assert first_closed.wait(10)
                assert page.evaluate("localStorage.getItem('isolated')") == "2"
                assert page.evaluate("1 + 1") == 2
                context.close()
                browser.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(render_client, index) for index in (1, 2)]
        for future in results:
            future.result(timeout=30)
    assert host.acquire(time.monotonic() + 10)["browser_pid"] == ready["browser_pid"]


def test_real_timeout_disposes_only_its_context(browser_workspace):
    root, module, host, environment = browser_workspace
    ready = host.acquire(time.monotonic() + 45)
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(ready["endpoint"])
        context = browser.new_context()
        page = context.new_page()
        page.set_content("<h1>survives peer timeout</h1>")
        cdp = browser.new_browser_cdp_session()
        before = set(cdp.send("Target.getBrowserContexts")["browserContextIds"])
        code = ("import os;from playwright.sync_api import sync_playwright;"
                "p=sync_playwright().start();b=p.chromium.connect_over_cdp(os.environ['MURAL_RENDER_BROWSER_ENDPOINT']);"
                "c=b.new_context();s=c.new_page();print('CONTEXT_CREATED',flush=True);"
                "s.evaluate('() => {while (true) {}}')")
        started = time.monotonic()
        with pytest.raises(CommandFailed) as failed:
            run_command([sys.executable, "-c", code], cwd=root, timeout_s=6,
                        environment={**environment, module.ENDPOINT_ENV: ready["endpoint"]})
        assert time.monotonic() - started < 10
        assert failed.value.result.diagnostics["termination"] == "timeout"
        assert "CONTEXT_CREATED" in failed.value.result.stdout
        for _ in range(50):
            after = set(cdp.send("Target.getBrowserContexts")["browserContextIds"])
            if after == before:
                break
            time.sleep(.1)
        assert after == before
        assert page.locator("h1").inner_text() == "survives peer timeout"
        context.close()
        browser.close()
    assert host.acquire(time.monotonic() + 10)["browser_pid"] == ready["browser_pid"]


def _running(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def _wait_gone(*pids):
    deadline = time.monotonic() + 10
    while any(_running(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(.05)
    assert not any(_running(pid) for pid in pids), pids


def test_real_browser_crash_rebuilds_and_close_reaps_owned_processes(browser_workspace):
    _root, _module, host, _environment = browser_workspace
    first = host.acquire(time.monotonic() + 45)
    os.kill(first["browser_pid"], signal.SIGKILL)
    _wait_gone(first["browser_pid"])
    second = host.acquire(time.monotonic() + 45)
    assert not second["reused"] and first["browser_pid"] != second["browser_pid"]
    host.close()
    _wait_gone(first["host_pid"], second["browser_pid"], second["host_pid"])


def test_owned_process_cleanup_does_not_signal_a_reused_pid(browser_workspace, monkeypatch):
    _root, module, _host, _environment = browser_workspace
    killed = []
    monkeypatch.setattr(module, "_process_state", lambda _pid: ("S", 1, "new-start-time"))
    monkeypatch.setattr(module, "_kill_group", killed.append)
    module._stop_owned({12345: "old-start-time"})
    assert killed == []


def test_failed_owned_shutdown_retains_directory_after_process_reparenting(browser_workspace, monkeypatch):
    from types import SimpleNamespace

    _root, module, _host, environment = browser_workspace
    temporary = Path(environment["TMPDIR"]) / "chromium-host-retained"
    temporary.mkdir()
    cdp = SimpleNamespace(send=lambda _command: {"processInfo": [{"type": "browser", "id": 12345}]})
    context = SimpleNamespace(close=lambda: None,
                              browser=SimpleNamespace(new_browser_cdp_session=lambda: cdp))
    def launch(profile, **_kwargs):
        Path(profile).mkdir()
        (Path(profile) / "DevToolsActivePort").write_text("12345\n/devtools/browser/test\n")
        return context
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch), stop=lambda: None)
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: SimpleNamespace(start=lambda: playwright))
    monkeypatch.setitem(sys.modules, "render_deck", SimpleNamespace(ensure_browser_libs=lambda: None,
                                                                  reset_child_process_signals=lambda: None))
    monkeypatch.setattr(module.signal, "signal", lambda *_: None)
    monkeypatch.setattr(module.sys, "stdin", iter(()))
    monkeypatch.setenv("TMPDIR", environment["TMPDIR"])
    snapshots = iter([{12345: "owned-start-time"}, {}])
    monkeypatch.setattr(module, "_descendants", lambda *_: next(snapshots))
    stopped = []
    def cannot_stop(processes):
        stopped.append(dict(processes))
        if processes:
            raise TimeoutError("owned process did not exit")
    monkeypatch.setattr(module, "_stop_owned", cannot_stop)
    with pytest.raises(TimeoutError, match="owned process did not exit"):
        module._serve(str(temporary))
    assert stopped == [{12345: "owned-start-time"}, {12345: "owned-start-time"}]
    assert (temporary / "profile/DevToolsActivePort").is_file()


@pytest.mark.parametrize("shutdown", ["eof", "sigterm", "stalled_close", "stopped_host", "killed_host"])
def test_real_host_cleanup_waits_for_owned_processes_and_preserves_siblings(browser_workspace, monkeypatch, shutdown):
    root, module, host, environment = browser_workspace
    if shutdown == "stalled_close":
        launch = subprocess.Popen
        helper = str(Path(module.__file__).resolve())
        code = ("import sys,threading;"
                f"sys.path.insert(0,{str(Path(helper).parent)!r});"
                "from playwright.sync_api import BrowserContext;"
                "BrowserContext.close=lambda self: threading.Event().wait();"
                "from browser_host import _serve;_serve(sys.argv[1])")
        def launch_stalled(command, **kwargs):
            if command[1] == helper:
                command = [command[0], "-c", code, command[2]]
            return launch(command, **kwargs)
        monkeypatch.setattr(module.subprocess, "Popen", launch_stalled)
    ready = host.acquire(time.monotonic() + 45)
    temporary, process = host._temporary, host._process
    assert temporary.parent == Path(environment["TMPDIR"])
    assert (temporary / "profile").is_dir()
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(ready["endpoint"])
        context = browser.new_context()
        page = context.new_page()
        page.set_content("<h1>owned browser is live</h1>")
        assert page.locator("h1").inner_text() == "owned browser is live"
        context.close()
        browser.close()
    owned = module._descendants(ready["host_pid"], host._host_started)
    sibling = temporary.parent / "another-task"
    sibling.mkdir()
    (sibling / "keep.txt").write_text("not owned by this BrowserHost")
    if shutdown in {"sigterm", "stopped_host", "killed_host"}:
        os.kill(ready["host_pid"], {"sigterm": signal.SIGTERM, "stopped_host": signal.SIGSTOP,
                                  "killed_host": signal.SIGKILL}[shutdown])
    started = time.monotonic()
    host.close()
    assert time.monotonic() - started < 15
    assert process.poll() is not None
    _wait_gone(ready["host_pid"], ready["browser_pid"], *owned)
    assert not temporary.exists()
    assert (sibling / "keep.txt").read_text() == "not owned by this BrowserHost"
    host.close()


@pytest.mark.parametrize("termination", [signal.SIGTERM, signal.SIGKILL])
def test_real_parent_termination_closes_browser_via_stdin_eof(browser_workspace, termination):
    root, module, _host, environment = browser_workspace
    code = ("import os,sys,time,json;from pathlib import Path;"
            f"sys.path.insert(0,{str(Path(module.__file__).parent)!r});"
            "from browser_host import BrowserHost;h=BrowserHost(Path.cwd(),dict(os.environ));"
            "ready=h.acquire(time.monotonic()+45);ready['temporary_directory']=str(h._temporary);"
            "print(json.dumps(ready),flush=True);time.sleep(90)")
    parent = subprocess.Popen([sys.executable, "-c", code], cwd=root, env=environment,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ready = None
    owned = {}
    try:
        ready = json.loads(parent.stdout.readline())
        owned = {pid: module._process_state(pid)[2] for pid in (ready["host_pid"], ready["browser_pid"])}
        os.kill(parent.pid, termination)
        parent.wait(timeout=10)
        _wait_gone(ready["host_pid"], ready["browser_pid"])
        assert not Path(ready["temporary_directory"]).exists()
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=10)
        parent.stdout.close()
        parent.stderr.close()
        module._stop_owned(owned)


def test_host_executes_the_workspace_pinned_helper(browser_workspace):
    root, module, host, _environment = browser_workspace
    ready = host.acquire(time.monotonic() + 45)
    assert "shared/skills/objects" in str(Path(module.__file__))
    assert host._process.args[1] == str(Path(module.__file__).resolve())
    assert ready["reused"] is False


def test_real_harness_renderer_preserves_font_and_artifact_contracts(browser_workspace, monkeypatch):
    from types import SimpleNamespace
    from runtime.production import ProductionSettings, ProductionToolServices, ResourceSlots, SkillRenderer, SkillSnapshot
    from runtime.tools import ToolContext
    from state.artifacts import ArtifactStore
    from state.workspace import WorkspaceState, file_sha256

    root, _module, _host, environment = browser_workspace
    source = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1"
    settings = ProductionSettings(skill_source=source, render_concurrency=2, command_timeout_s=120)
    snapshot = SkillSnapshot.stage(root, source, environment)
    renderer = SkillRenderer(root, settings, environment, ResourceSlots(root, settings), snapshot)
    state = WorkspaceState(root)
    artifacts = ArtifactStore(root, workspace_state=state)
    try:
        receipts = []
        for page in (1, 2):
            state.mark_authored(page, html_sha256=file_sha256(state.paths.page_html(page)))
            def capture(html, png, canvas):
                receipt = renderer(html, png, canvas)
                receipts.append(receipt)
                return receipt
            rendered = artifacts.render_pages([page], capture)[0]
            assert rendered.png_sha256 == file_sha256(state.paths.page_png(page))
            assert rendered.html_sha256 == file_sha256(state.paths.page_html(page))
            assert _pixels(state.paths.page_png(page))[0] == (800, 600)
        diagnostics = [receipt["diagnostics"] for receipt in receipts]
        assert [row["browser_reused"] for row in diagnostics] == [False, True]
        assert diagnostics[0]["browser_pid"] == diagnostics[1]["browser_pid"]
        assert all(row["renderer_timings_s"]["screenshot"] > 0 for row in diagnostics)
        assert Path(renderer._browser_host._process.args[1]).is_relative_to(snapshot.target)
        assert not (root / "skills").exists()
        (root / "_trace/harness-browser-reuse.json").write_text(json.dumps(diagnostics, indent=2))
        services = ProductionToolServices.__new__(ProductionToolServices)
        services.workspace, services.settings, services.renderer = state, settings, renderer
        services._validated_command = lambda *_: ([], "orchestrator", "audit", Namespace())
        services._command_environment = lambda: environment
        audit = workspace_skill_module(root, "workspace_policy").audit_workspace
        def audit_after_cleanup(*_args, **_kwargs):
            assert renderer._browser_host is None
            assert not audit(root)
            return SimpleNamespace(returncode=0, stdout="", stderr="", diagnostics={})
        with monkeypatch.context() as patch:
            patch.setattr("runtime.production._run_command", audit_after_cleanup)
            services.terminal(ToolContext("orchestrator", "orchestrator"), {"command": "audit"})
        rebuilt = renderer(state.paths.page_html(1), state.paths.page_png(1), {})["diagnostics"]
        assert not rebuilt["browser_reused"]
        assert rebuilt["browser_pid"] != diagnostics[0]["browser_pid"]
    finally:
        renderer.close()
    assert not audit(root)


@pytest.mark.parametrize("action", ["audit", "finalize"])
@pytest.mark.parametrize("status", ["queued", "running", "waiting_dependency", "blocked", "interrupted", "succeeded"])
@pytest.mark.parametrize("has_close", [True, False])
def test_terminal_browser_cleanup_requires_all_owners_terminal(tmp_path, monkeypatch, action, status, has_close):
    from types import SimpleNamespace
    from runtime.production import ProductionToolServices
    from runtime.tools import ToolContext, ToolExecutionError
    from state.workspace import WorkspaceState
    service = ProductionToolServices.__new__(ProductionToolServices)
    service.workspace = WorkspaceState(tmp_path)
    service.settings = SimpleNamespace(command_timeout_s=10)
    service._validated_command = lambda *_: ([], "orchestrator", action, Namespace())
    service._command_environment = lambda: {}
    calls = []
    service.renderer = SimpleNamespace(close=lambda: calls.append("close")) if has_close else lambda *_: {}
    service.snapshot = SimpleNamespace(target=tmp_path, tree_sha256="pinned")
    monkeypatch.setattr("runtime.production.load_skill_module", lambda *_: SimpleNamespace(read_deck=lambda _: ({}, [])))
    def execute(*_args, **_kwargs):
        calls.append("execute")
        return SimpleNamespace(returncode=0, stdout="", stderr="", diagnostics={})
    monkeypatch.setattr("runtime.production._run_command", execute)
    (tmp_path / "_state/orchestration.json").write_text(json.dumps({"tasks": {"page": {"status": status}}}))
    if status == "succeeded":
        service.terminal(ToolContext("orchestrator", "orchestrator"), {"command": action})
        assert calls == (["close", "execute"] if has_close else ["execute"])
    else:
        with pytest.raises(ToolExecutionError, match="unfinished tasks"):
            service.terminal(ToolContext("orchestrator", "orchestrator"), {"command": action})
        assert not calls


@pytest.mark.parametrize("endpoint", ["ws://external.example:8000/devtools/browser/x",
                                     "http://127.0.0.1:8000", "ws://user:secret@127.0.0.1:8000/devtools/browser/x"])
def test_external_endpoint_is_rejected(browser_workspace, endpoint):
    with pytest.raises(ValueError, match="internal loopback"):
        browser_workspace[1].validate_endpoint(endpoint)
