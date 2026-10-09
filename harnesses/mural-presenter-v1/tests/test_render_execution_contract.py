from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest

from geometry_fixture import geometry, write_geometry
from runtime.production import (
    ProductionToolServices, read_scopes_for_role, write_scopes_for_role,
)
from runtime.shared_objects import SharedSkillStore
from runtime.tools import ToolCall, ToolContext, ToolDefinition, ToolRuntime
from state.artifacts import ArtifactStore
from state.workspace import PageLifecycle, WorkspaceState, file_sha256


@pytest.fixture
def rendering(tmp_path):
    workspace = WorkspaceState(tmp_path)
    write_geometry(tmp_path, geometry(width_px=1600, height_px=1200))
    workspace.register_pages([1, 2])
    for page in (1, 2):
        workspace.paths.page_html(page).write_text(f'<main>Skeleton {page}</main>')
        workspace.register_scaffold(page)
    artifacts = ArtifactStore(tmp_path, workspace_state=workspace)
    reference = SharedSkillStore.for_workspace(tmp_path).load_ref(
        workspace.paths, expected_skill_name="mural-presenter-v1")

    class Renderer:
        calls = []
        last_stdout = "status:PASS"

        def __call__(self, html, png, canvas):
            self.calls.append(html.name)
            Image.new("RGB", (canvas["width_px"], canvas["height_px"]), "navy").save(png)
            return {"geometry": canvas, "loaded_resources": [], "failed_resources": []}

    renderer = Renderer()
    services = ProductionToolServices(
        SimpleNamespace(revision_request=None),
        SimpleNamespace(name=reference.skill_name, target=reference.object_root, tree_sha256=reference.tree_sha256,
                        resolve_skill_path=lambda logical: reference.object_root / logical.split("/", 2)[2]),
        SimpleNamespace(command_timeout_s=30), {}, workspace, artifacts, renderer, None)
    owner = services.owners.accept(role="slide", pages=[1, 2], group_id="body")
    services.owners.transition(owner.owner_id, attempt=owner.attempt, status="running")
    tools = ToolRuntime()
    tools.register(ToolDefinition("terminal", "render", {"type": "object"}, services.terminal,
                                  frozenset({"slide"})))
    context = ToolContext(owner.owner_id, "slide", read_scopes_for_role("slide", skill_name=reference.skill_name, pages=(1, 2)),
                          write_scopes_for_role("slide", pages=(1, 2)),
                          {"pages": [1, 2], "group_id": owner.group_id, "attempt": owner.attempt})
    return workspace, services, renderer, tools, context


def _render(tools, context, command):
    return tools.execute(context, ToolCall(command, "terminal", {
        "command": "python skills/mural-presenter-v1/scripts/slide.py " + command}))


@pytest.mark.parametrize("command", ["render . --page 1", "render-group . --group body --pages 1,2"])
def test_scaffold_is_not_renderable_via_either_terminal_entry(rendering, command):
    workspace, _services, renderer, tools, context = rendering
    result = _render(tools, context, command)
    assert not result.ok
    assert result.error_code == "render_source_uncommitted"
    assert "slides/slide_01.html" in result.content
    assert "write_file" in result.content and "patch" in result.content
    assert renderer.calls == []
    assert not workspace.paths.page_png(1).exists()


def test_group_preflights_all_sources_before_rendering_any_member(rendering):
    workspace, services, renderer, tools, context = rendering
    services.write_file(context, {"path": "slides/slide_01.html", "content": "<main>Authored first</main>"})
    result = _render(tools, context, "render-group . --group body --pages 1,2")
    assert not result.ok and result.error_code == "render_source_uncommitted"
    assert "slides/slide_02.html" in result.content
    assert renderer.calls == []
    assert not workspace.paths.page_png(1).exists()


@pytest.mark.parametrize("command,code", [
    ("render . --page 3", "slide_page_scope_denied"),
    ("render-group . --group body --pages 1,3", "slide_page_scope_denied"),
    ("render-group . --group body --pages 1,1", "terminal_argument_invalid"),
    ("render-group . --group body --pages 1,no", "terminal_argument_invalid"),
    ("render-group . --group body --pages 0,1", "terminal_argument_invalid"),
    ("render-group . --group ../escape --pages 1,2", "terminal_argument_invalid"),
])
def test_render_rejects_invalid_or_unowned_pages_before_execution(rendering, command, code):
    _workspace, _services, renderer, tools, context = rendering
    result = _render(tools, context, command)
    assert not result.ok and result.error_code == code
    assert renderer.calls == []


@pytest.mark.parametrize("denied", ["read", "write"])
def test_render_requires_page_read_and_write_scope(rendering, denied):
    from dataclasses import replace
    _workspace, _services, renderer, tools, context = rendering
    context = replace(context, **{f"{denied}_scopes": ()})
    result = _render(tools, context, "render . --page 1")
    assert not result.ok and result.error_code == f"{denied}_scope_denied"
    assert renderer.calls == []


def test_group_uses_canonical_renderer_receipts_and_contact_sheet(rendering):
    import json
    workspace, services, renderer, tools, context = rendering
    for page in (1, 2):
        services.write_file(context, {"path": f"slides/slide_{page:02d}.html",
                                      "content": f"<main>Authored {page}</main>"})
    result = _render(tools, context, "render-group . --group body --pages 1,2")
    assert result.ok, result.content
    receipt = json.loads(result.content)
    assert renderer.calls == ["slide_01.html", "slide_02.html"]
    assert [page["page"] for page in receipt["pages"]] == [1, 2]
    for page in receipt["pages"]:
        assert page["html_sha256"] == file_sha256(workspace.paths.page_html(page["page"]))
        assert page["png_sha256"] == file_sha256(workspace.paths.page_png(page["page"]))
        assert workspace.page(page["page"]).lifecycle == PageLifecycle.RENDERED
    with Image.open(workspace.paths.root / receipt["contact_sheet"]) as sheet:
        assert sheet.width == 800 and sheet.height == 322
    # Reusing the same page through the single entry does not render twice or mark it ready.
    again = _render(tools, context, "render . --page 1")
    assert again.ok and len(renderer.calls) == 2
    assert json.loads(again.content)["pages"][0] == receipt["pages"][0]


def test_external_source_change_is_not_silently_committed_by_render(rendering):
    workspace, services, renderer, tools, context = rendering
    services.write_file(context, {"path": "slides/slide_01.html", "content": "<main>Authored</main>"})
    workspace.paths.page_html(1).write_text("<main>Unregistered change</main>")
    result = _render(tools, context, "render . --page 1")
    assert not result.ok and result.error_code == "render_source_uncommitted"
    assert renderer.calls == []


def test_inspect_does_not_turn_unwritten_source_into_a_successful_diagnostic(rendering):
    from infer import build_inspect_handlers
    workspace, services, renderer, tools, context = rendering
    inspect, _commit = build_inspect_handlers(workspace, services.artifacts, renderer, lambda *_: {})
    tools.register(ToolDefinition("inspect", "inspect", {"type": "object"}, inspect, frozenset({"slide"})))
    result = tools.execute(context, ToolCall("inspect-unwritten", "inspect", {"page": [1]}))
    assert not result.ok and result.error_code == "render_source_uncommitted"
    assert "write_file" in result.content and "slides/slide_01.html" in result.content
    assert renderer.calls == []
