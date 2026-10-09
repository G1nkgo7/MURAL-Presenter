"""291/292 的裁切、交接、阶段完成与安全收尾回归；不调用真实 Provider。"""
import asyncio
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

from PIL import Image
import pytest

from infer import PresenterKernel
from orchestration.manager import OrchestrationError, OrchestrationManager, TaskRecord, TaskStatus
from runtime import production
from runtime.agent import AgentInterruptedError
from runtime.config import RunSpec
from runtime.recovery import tool_boundary
from runtime.shared_objects import SharedSkillStore
from runtime.session import AnthropicTransport, ModelRequest, ModelSession, ProviderChannel
from runtime.skill_runtime import SkillRuntime
from runtime.tools import ToolCall, ToolContext, ToolContractError, ToolRuntime, WriteScope
from state.artifacts import ArtifactStore
from state.workspace import PageLifecycle, WorkspaceState
from test_agent_runtime import _definition, _response, _run, _settings, _tool, ScriptedTransport
from test_orchestration_manager import drain
from test_owner_continuation import continuation
from test_workspace_artifacts import _inspected_render_fixture


@pytest.mark.parametrize("folder", ["inputs", "assets"])
def test_crop_supports_actual_attachment_generated_and_downloaded_images(tmp_path, monkeypatch, capsys, folder):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1/scripts"))
    from _internal.deck_core import material_figure
    root = WorkspaceState(tmp_path).paths.root
    source = root / folder / "source.png"
    Image.new("RGBA", (1000, 600), (50, 80, 100, 128)).save(source)
    original = source.read_bytes()
    material_figure(root, f"{folder}/source.png", "assets/crop.png", "0.1,0.2,0.9,0.8")
    assert json.loads(capsys.readouterr().out)["pixels"] == [800, 360]
    assert source.read_bytes() == original
    with Image.open(root / "assets/crop.png") as cropped:
        assert cropped.getpixel((10, 10)) == (50, 80, 100, 128)
    if folder == "assets":
        with pytest.raises(ValueError, match="differ from source"):
            material_figure(root, "assets/source.png", "assets/source.png", "0,0,1,1")
    outside = root / "_state/private.png"
    Image.new("RGB", (100, 100)).save(outside)
    (root / folder / "escape.png").symlink_to(outside)
    with pytest.raises(ValueError, match="under inputs/ or assets/"):
        material_figure(root, f"{folder}/escape.png", "assets/escape-crop.png", "0,0,1,1")


@pytest.mark.parametrize("action", ["crop", "crop-material", "extract-subject", "register-user"])
def test_harness_crop_asset_scope_does_not_open_attachment_registration(tmp_path, monkeypatch, action):
    workspace = WorkspaceState(tmp_path)
    Image.new("RGB", (100, 100)).save(tmp_path / "assets/source.png")
    backend = production.ProductionToolServices.__new__(production.ProductionToolServices)
    backend.workspace = workspace
    backend.artifacts = ArtifactStore(tmp_path, workspace_state=workspace)
    backend.settings = SimpleNamespace(command_timeout_s=5)
    backend.snapshot = SimpleNamespace(name="mural-presenter-v1", tree_sha256="crop-scope-test",
        target=Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1",
        resolve_skill_path=lambda _path: tmp_path / "image.py")
    backend._command_environment = lambda: {}
    executed = []
    def cropped_output(*args, **kwargs):
        executed.append(args)
        Image.new("RGB", (80, 80)).save(tmp_path / "assets/crop.png")
        return SimpleNamespace(returncode=0, stdout="", stderr="", diagnostics={})
    monkeypatch.setattr(production, "_run_command", cropped_output)
    monkeypatch.setattr(production, "materialize_page_image", lambda *_: pytest.fail("assets are not lazy PDF pages"))
    context = ToolContext("image", "image", read_scopes=(WriteScope("assets", recursive=True),),
        write_scopes=(WriteScope("assets", recursive=True),))
    command = f"python skills/mural-presenter-v1/scripts/image.py {action} . --source assets/source.png --output assets/crop.png"
    if action != "register-user":
        command += " --box 0.1,0.1,0.9,0.9"
        assert backend.terminal(context, {"command": command})["returncode"] == 0
        assert len(executed) == 1
    else:
        with pytest.raises(ToolContractError, match="cannot read"):
            backend.terminal(context, {"command": command})
        assert executed == []


def test_reference_page_authorization_is_exact_read_only_and_persistent(tmp_path):
    workspace = WorkspaceState(tmp_path)
    skill = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1"
    reference = SharedSkillStore.for_workspace(tmp_path).pin(
        workspace.paths, skill, skill_name=skill.name
    )
    (tmp_path / "config.json").write_text(json.dumps({
        "working_language": "en", "presentation_language": "en", "target_page_count": 1,
    }))
    workspace.paths.page_html(6).write_text("<section>Representative page</section>")
    manager = OrchestrationManager(tmp_path, deck_id="references")
    received = []
    manager.register_runner("slide", lambda task, _: received.append(task) or {})
    spec = {"task_id": "p1", "role": "slide", "pages": [1], "reference_pages": [6]}
    try:
        submitted = manager.submit_tasks([spec])["tasks"][0]
        drain(manager)
        task = manager.task("p1")
        assert task.reference_pages == received[0].reference_pages == (6,)
        assert TaskRecord.from_dict(json.loads(manager.path.read_text())["tasks"]["p1"]).reference_pages == (6,)
        assert manager.submit_tasks([spec])["reused_task_ids"] == ["p1"]
        assert manager.registry.get(submitted["owner_id"]).pages == (1,)
        runtime = object.__new__(SkillRuntime)
        runtime.prepared = SimpleNamespace(snapshot=SimpleNamespace(
            name=reference.skill_name, tree_sha256=reference.tree_sha256, release="test",
        ))
        runtime._attachment_manifest = lambda: {}
        runtime._staged_attachment_paths = lambda: ()
        runtime._tool_services = SimpleNamespace(
            may_write=lambda context, path: context.may_write(path)
        )
        services = SimpleNamespace(workspace=workspace, run_spec=RunSpec.from_inputs(raw_user_query="one", output_workspace=tmp_path),
                                   orchestration=manager)
        context = runtime._owner_tool_context(services, task)
        assert context.may_read("slides/slide_06.html") and not context.may_write("slides/slide_06.html")
        assert not context.may_read("slides/slide_05.html") and not context.may_read("briefs/slide_06.md")
        assert context.may_write("slides/slide_01.html")
        assert runtime._owner_handoffs(services, task)["reference_html_paths"] == ["slides/slide_06.html"]
        assert "slides/slide_06.html" in runtime._owner_artifact_hashes(services, task)
        with pytest.raises(OrchestrationError, match="different contract"):
            manager.submit_tasks([{**spec, "reference_pages": []}])
        with pytest.raises(OrchestrationError, match="reference page source"):
            manager.submit_tasks([{**spec, "task_id": "missing", "reference_pages": [8]}])
    finally:
        manager.close()


@pytest.mark.parametrize("entry", ["kernel", "skill"])
def test_phase_completion_does_not_promote_inspected_page(tmp_path, monkeypatch, entry):
    workspace, store, _, _ = _inspected_render_fixture(tmp_path)
    if entry == "kernel":
        PresenterKernel._wrap_runner(SimpleNamespace(workspace=workspace), lambda *_: {"status": "completed"})(
            SimpleNamespace(role="slide", pages=(1,)), None)
    else:
        runtime, services, agent, _, _, _ = continuation(tmp_path / "context", monkeypatch)
        services.workspace, services.artifacts = workspace, store
        monkeypatch.setattr(runtime, "_owner_handoffs", lambda *_: {})
        owner = services.owners.accept(role="slide", pages=[1], group_id="one")
        task = TaskRecord("layout-only", owner.owner_id, owner.attempt, "slide", (1,), "one", (), TaskStatus.RUNNING)
        runtime._owner_runner(services, agent, "slide")(task, None)
    assert workspace.page(1).lifecycle == PageLifecycle.INSPECTED
    kernel = PresenterKernel(RunSpec.from_inputs(raw_user_query="one", output_workspace=tmp_path))
    try:
        context = kernel.services.orchestrator_tool_context()
        issue = kernel.tools.execute(context, ToolCall("missing-image", "verification_submit", {
            "status": "issues_found", "issues": [{"positions": [1], "problem": "Required image is a placeholder",
                                                  "suggested_fix": "Insert the completed Image asset"}]}))
        assert issue.ok, issue
        from runtime.tools import ToolExecutionError
        with pytest.raises(ToolExecutionError) as premature:
            kernel.services.complete_deck()
        assert premature.value.code == "verification_rejected"
        assert workspace.page(1).lifecycle == PageLifecycle.INSPECTED
    finally:
        kernel.orchestration.close()


def test_placeholder_followup_uses_existing_dependency_and_same_owner(tmp_path):
    release = threading.Event()
    manager = OrchestrationManager(tmp_path, deck_id="deferred-image")
    manager.register_runner("image", lambda *_: release.wait())
    manager.register_runner("slide", lambda *_: {"status": "completed"})
    try:
        manager.submit_tasks([{"task_id": "image", "role": "image"},
                              {"task_id": "layout", "role": "slide", "pages": [6], "group_id": "sample"}])
        deadline = time.monotonic() + 30
        while manager.task("layout").status != TaskStatus.COMPLETED:
            assert time.monotonic() < deadline
            manager.wait(timeout_s=.1)
        followup = {"task_id": "insert-image", "role": "slide", "pages": [6], "group_id": "sample", "repair": True,
                    "depends_on": ["image"], "goal": "Replace the three placeholders; preserve the layout"}
        manager.submit_tasks([followup])
        assert manager.task("insert-image").status == TaskStatus.WAITING_DEPENDENCY
        assert manager.task("insert-image").owner_id == manager.task("layout").owner_id
        release.set()
        drain(manager)
        assert manager.task("insert-image").status == TaskStatus.COMPLETED
    finally:
        release.set()
        manager.close()


def test_final_ready_rejects_pending_work_before_review_or_resource_checks(tmp_path, monkeypatch):
    kernel = PresenterKernel(RunSpec.from_inputs(raw_user_query="one", output_workspace=tmp_path))
    release = threading.Event()
    kernel.orchestration.register_runner("image", lambda *_: release.wait())
    kernel.services.visual_review = lambda: pytest.fail("pending work must be rejected before review")
    monkeypatch.setattr(kernel.workspace, "assert_ready_resources",
                        lambda *_: pytest.fail("pending work must be rejected before full resource checks"))
    try:
        kernel.orchestration.submit_tasks([{"task_id": "required-images", "role": "image"}])
        from runtime.tools import ToolExecutionError
        with pytest.raises(ToolExecutionError) as result:
            kernel.services.complete_deck()
        assert result.value.code == "unfinished_tasks"
        assert "required-images" in str(result.value)
    finally:
        release.set()
        kernel.orchestration.close()


def test_agent_stops_after_complete_tool_batch_with_resumable_checkpoint(tmp_path):
    stop = threading.Event()
    tools = ToolRuntime()
    executed = []
    def action(_context, _arguments):
        executed.append("tool")
        stop.set()
        return {"written": True}
    tools.register(_definition("action", action))
    transport = ScriptedTransport([_response(_tool("first", "action", {}), _tool("second", "action", {}), stop_reason="tool_use")])
    checkpoints = []
    with pytest.raises(AgentInterruptedError):
        _run(transport, tools, ToolContext("owner", "slide"), stop_event=stop, checkpoint_sink=checkpoints.append)
    assert len(transport.calls) == 1 and executed == ["tool", "tool"]
    assert tool_boundary(checkpoints[-1].messages).is_closed
    assert {block["tool_use_id"] for block in checkpoints[-1].messages[-1]["content"]} == {"first", "second"}
    resumed = _run(ScriptedTransport([_response(SimpleNamespace(type="text", text="恢复后继续"))]),
                   tools, ToolContext("owner", "slide"), checkpoint=checkpoints[-1], initial_messages=())
    assert resumed.stop_reason == "end_turn" and tool_boundary(resumed.checkpoint.messages).is_closed
    assert executed == ["tool", "tool"]


def test_shutdown_interrupts_native_provider_closes_connection_and_does_not_failover(tmp_path, monkeypatch):
    manager = OrchestrationManager(tmp_path, deck_id="shutdown")
    started, connection_closed = threading.Event(), threading.Event()
    events = []
    transport = AnthropicTransport(_settings())
    async def pending(*_):
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            connection_closed.set()
    monkeypatch.setattr(transport, "_request_once", pending)
    def owner(*_):
        session = ModelSession(settings=_settings(), transport=transport,
            channels=(ProviderChannel("secret-a", "a"), ProviderChannel("secret-b", "b")),
            stop_event=manager.stop_event, event_sink=events.append)
        session.call(ModelRequest(model="test", system="", messages=()))
    manager.register_runner("slide", owner)
    manager.submit_tasks([{"task_id": "p1", "role": "slide", "pages": [1]}])
    assert started.wait(3)
    manager.close(timeout_s=2)
    assert connection_closed.is_set()
    assert manager.task("p1").status == TaskStatus.INTERRUPTED
    assert [row["event_type"] for row in events] == ["provider_attempt_started", "provider_attempt_failed"]
    assert events[-1]["error_kind"] == "owner_interrupted" and not events[-1]["retryable"]


def test_close_timeout_is_explicit_without_killing_running_tool(tmp_path):
    entered, release = threading.Event(), threading.Event()
    manager = OrchestrationManager(tmp_path, deck_id="slow-tool")
    def owner(*_):
        entered.set()
        assert release.wait(5)
    manager.register_runner("slide", owner)
    manager.submit_tasks([{"task_id": "p1", "role": "slide", "pages": [1]}])
    assert entered.wait(3)
    try:
        with pytest.raises(OrchestrationError, match="owner_shutdown_pending"):
            manager.close(timeout_s=.01)
        assert manager.task("p1").status == TaskStatus.RUNNING
        assert manager.snapshot()["events"][-1]["type"] == "shutdown_pending"
    finally:
        release.set()
        manager.close()


def test_font_uri_checks_only_matching_object_but_still_rejects_tampering(tmp_path, monkeypatch):
    import hashlib
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1/scripts"))
    from _internal import font_bundle
    store = tmp_path / "shared/fonts/objects"
    store.mkdir(parents=True)
    rows = []
    for index in range(90):
        data = f"font-object-{index}".encode()
        digest = hashlib.sha256(data).hexdigest()
        path = store / f"{digest}.ttf"
        path.write_bytes(data)
        rows.append({"sha256": digest, "object_ref": path.relative_to(tmp_path).as_posix(), "scope": "public"})
    (tmp_path / "_state").mkdir()
    (tmp_path / "_state/font-manifest.json").write_text(json.dumps({"schema": "mural.font-manifest.v2",
        "studio_data_root": str(tmp_path), "source_faces": rows}))
    calls = []
    verify = font_bundle._pinned_object
    def counted(*args, **kwargs):
        calls.append((args[1]["sha256"], kwargs.get("verify", True)))
        return verify(*args, **kwargs)
    monkeypatch.setattr(font_bundle, "_pinned_object", counted)
    selected = tmp_path / rows[-1]["object_ref"]
    assert font_bundle.resolve_shared_font_reference(tmp_path, selected.as_uri()) == (selected, rows[-1]["sha256"])
    assert calls == [(rows[-1]["sha256"], True)]
    selected.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        font_bundle.resolve_shared_font_reference(tmp_path, selected.as_uri())


@pytest.mark.parametrize("reuse_program", [False, True])
def test_resource_check_loads_skill_once_and_rechecks_font_hashes_each_time(tmp_path, monkeypatch, reuse_program):
    import hashlib
    from geometry_fixture import geometry, write_geometry
    from runtime import shared_fonts
    from runtime.shared_objects import SharedSkillStore
    from state.artifacts import ArtifactStateError, ArtifactStore, _html_resource_hashes
    write_geometry(tmp_path, geometry(width_px=1600, height_px=900))
    store = SharedSkillStore.for_workspace(tmp_path)
    objects = store.studio_data / "shared/fonts/objects"
    objects.mkdir(parents=True, exist_ok=True)
    rows, references = [], []
    for data in (b"font-one", b"font-two"):
        digest = hashlib.sha256(data).hexdigest()
        path = objects / f"{digest}.ttf"
        path.write_bytes(data)
        rows.append({"sha256": digest, "object_ref": path.relative_to(store.studio_data).as_posix(), "scope": "public"})
        references.append(path.as_uri())
    (tmp_path / "_state/font-manifest.json").write_text(json.dumps({"schema": "mural.font-manifest.v2",
        "studio_data_root": str(store.studio_data), "source_faces": rows}))
    css = tmp_path / "base.css"
    css.write_text(css.read_text() + "\n".join(f'@font-face {{src:url("{uri}");}}' for uri in references * 2))
    html = tmp_path / "slides/slide_01.html"
    html.write_text("<section>font references</section>")
    factory, calls, checked_references = shared_fonts.font_reference_resolver, [], []
    def counted(root):
        calls.append(root)
        resolve = factory(root)
        def current(reference):
            checked_references.append(reference)
            return resolve(reference)
        return current
    monkeypatch.setattr(shared_fonts, "font_reference_resolver", counted)
    artifacts = ArtifactStore(tmp_path)
    def resource_hashes():
        return _html_resource_hashes(tmp_path, html, loaded_resources=(),
                                    resolve_shared_font=artifacts._shared_font if reuse_program else None)
    result = resource_hashes()
    assert calls == [tmp_path]
    assert checked_references == references
    assert {uri: result[uri] for uri in references} == dict(zip(references, [row["sha256"] for row in rows]))
    assert resource_hashes() == result
    assert checked_references == references * 2
    (objects / f'{rows[-1]["sha256"]}.ttf').write_bytes(b"changed-font")
    with pytest.raises(ArtifactStateError, match="hash mismatch"):
        resource_hashes()
    assert calls == [tmp_path] * (1 if reuse_program else 3)


def test_authoring_font_capture_reuses_program_and_ignores_unused_inventory(tmp_path, monkeypatch):
    import hashlib
    from geometry_fixture import geometry, write_geometry
    from runtime import shared_fonts
    from runtime.shared_objects import SharedSkillStore
    from runtime.workspace_paths import WorkspacePaths
    write_geometry(tmp_path, geometry(width_px=1600, height_px=900))
    store_root = SharedSkillStore.for_workspace(tmp_path).studio_data
    objects = store_root / "shared/fonts/objects"
    objects.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(b"font-capture-fixture").hexdigest()
    font = objects / f"{digest}.ttf"
    font.write_bytes(b"font-capture-fixture")
    rows = [{"sha256": digest, "object_ref": font.relative_to(store_root).as_posix(), "scope": "public",
             "family": "fixture", "weight": weight, "style": "normal"} for weight in ("400", "700")]
    rows.append({"sha256": "f" * 64, "object_ref": "shared/fonts/objects/" + "f" * 64 + ".ttf", "scope": "public",
                 "family": "unused", "weight": "400", "style": "normal"})
    paths = WorkspacePaths.from_root(tmp_path)
    paths.font_manifest.write_text(json.dumps({"schema": "mural.font-manifest.v2", "studio_data_root": str(store_root),
                                               "source_faces": rows}))
    rules = [f'@font-face {{font-family: "fixture"; font-weight: {weight}; font-style: normal; src: url("{font.as_uri()}");}}'
             for weight in ("400", "700")]
    (tmp_path / "base.css").write_text("/* SHARED_AUTHORING_FONTS_START */" + "\n".join(rules)
                                      + "/* SHARED_AUTHORING_FONTS_END */")
    store = shared_fonts.SharedFontStore(tmp_path, {"MURAL_STUDIO_DATA_DIR": str(store_root)})
    factory, loads, checked = shared_fonts.font_reference_resolver, [], []
    def resolver(root):
        loads.append(root)
        resolve = factory(root)
        def current(reference):
            checked.append(reference)
            return resolve(reference)
        return current
    monkeypatch.setattr(shared_fonts, "font_reference_resolver", resolver)
    for _ in range(2):
        store.capture_runtime_bundle(paths)
    assert loads == [tmp_path] and checked == [font.as_uri()] * 2
    selected = json.loads(paths.font_manifest.read_text())["selected_faces"]
    assert [row["weight"] for row in selected] == ["400", "700"]
    # 完整装配/resume 仍检查未使用但缺失的库存；普通渲染只依赖实际字体。
    with pytest.raises(shared_fonts.SharedFontError, match="missing"):
        store.verify_manifest(paths.font_manifest)
    font.write_bytes(b"changed-used-font")
    with pytest.raises(shared_fonts.SharedFontError, match="hash mismatch"):
        store.capture_runtime_bundle(paths)


@pytest.mark.parametrize("api_format", ["openai_chat_compatible", "openai_responses"])
def test_shutdown_cancels_both_openai_http_transports(api_format):
    from dataclasses import replace
    import httpx
    from runtime.session import create_transport
    stop, started, closed = threading.Event(), threading.Event(), threading.Event()
    async def handler(_request):
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            closed.set()
    transport = create_transport(replace(_settings(), api_format=api_format),
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    session = ModelSession(settings=replace(_settings(), api_format=api_format), transport=transport,
        channels=(ProviderChannel("secret", "a"),), stop_event=stop)
    errors = []
    def run():
        try:
            session.call(ModelRequest(model="test", system="", messages=()))
        except InterruptedError as exc:
            errors.append(exc)
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert started.wait(3)
    finally:
        stop.set()
        thread.join(3)
    assert not thread.is_alive() and closed.is_set() and len(errors) == 1


def test_resource_wait_cancellation_releases_ticket_and_does_not_steal_slot(tmp_path):
    slots = production.ResourceSlots(tmp_path, production.ProductionSettings.from_environment())
    slots.root = tmp_path / "slots"
    slots.root.mkdir()
    slots.limits["model"] = 1
    stop, waiting = threading.Event(), threading.Event()
    errors = []
    def waiter():
        waiting.set()
        try:
            with slots.acquire("model", deadline_monotonic=time.monotonic() + 10, stop_event=stop):
                errors.append("unexpected admission")
        except InterruptedError:
            errors.append("interrupted")
    with slots.acquire("model"):
        thread = threading.Thread(target=waiter)
        thread.start()
        assert waiting.wait(2)
        stop.set()
        thread.join(2)
        assert not thread.is_alive()
        assert errors == ["interrupted"] and not list(slots.root.glob("*.queue/*.ticket"))
    with slots.acquire("model", deadline_monotonic=time.monotonic() + 1):
        pass
