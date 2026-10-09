"""Tool 层单一真源验收：角色工具集、唯一 Schema/Adapter、vision 物化与原子边界。

这些测试锁定 task F 的合同：runtime.tools 是全部公共工具契约的唯一真源，
inspect 按需物化附件页图且仅实际作者的单页回执推进 INSPECTED，write/patch 与
artifact-commit 的原子边界在失败时不留下未登记的 canonical 文件改动。
"""
from __future__ import annotations

from geometry_fixture import geometry, write_geometry

from pathlib import Path

import pytest
from PIL import Image

from infer import build_inspect_handlers
from owner.registry import OwnerRegistry
from runtime.attachments import (
    AttachmentStager,
    resolve_attachment_records,
    resolve_page_assets,
)

from runtime.tools import (
    STABLE_TOOL_SCHEMAS,
    ToolCall,
    ToolContext,
    ToolContractError,
    ToolRuntime,
    WriteScope,
    register_tools,
    tool_specs,
)
from state.artifacts import ArtifactStore
from state.workspace import PageLifecycle, WorkspaceState


# 四个当前角色收到的确切工具集（唯一真源投影）。
EXPECTED_ROLE_TOOLS = {
    "orchestrator": {
        "delegate_submit",
        "respond_owner_request",
        "verification_submit",
        "inspect",
        "image_generate",
        "web_search",
        "web_extract",
        "web_screenshot",
        "read_file",
        "write_file",
        "patch",
        "terminal",
    },
    "research": {"read_file", "write_file", "patch", "web_search", "web_extract", "inspect"},
    "image": {
        "image_generate",
        "web_screenshot",
        "read_file",
        "write_file",
        "patch",
        "terminal",
        "web_search",
        "inspect",
    },
    "slide": {
        "request_orchestrator",
        "read_file",
        "write_file",
        "patch",
        "terminal",
        "inspect",
    },
}


def _full_runtime() -> ToolRuntime:
    """用占位 handler 注册全部工具契约，仅用于校验目录形状。"""
    runtime = ToolRuntime()
    handlers = {name: (lambda _c, _a: {"ok": True}) for name in tool_specs()}
    register_tools(runtime, handlers)
    return runtime


def test_each_role_receives_exactly_its_declared_tool_set() -> None:
    runtime = _full_runtime()
    for role, expected in EXPECTED_ROLE_TOOLS.items():
        advertised = {tool["name"] for tool in runtime.advertised_tools(role)}
        assert advertised == expected, role
    assert runtime.advertised_tools("material") == []


def test_visual_entry_is_unified_without_executable_aliases() -> None:
    specs = tool_specs()
    assert "inspect" in specs
    assert not {"vision_analyze", "inspect_group"} & specs.keys()


@pytest.mark.parametrize("arguments,valid", [
    ({"page": [1]}, True), ({"page": [1, 3], "question": "检查导航"}, True),
    ({"path": "assets/photo.png"}, True),
    ({"page": []}, False), ({"page": [1, "2"]}, False),
    ({"page": True}, False), ({"page": -1}, False), ({"page": 1}, False),
    ({"page": "[1,2]"}, False), ({"path": ""}, False),
    ({"pages": [1, 2]}, False), ({"page": 1, "scope": "page"}, False),
])
def test_inspect_explicit_target_schema(arguments, valid):
    result = _full_runtime().execute(ToolContext("orchestrator", "orchestrator"),
        ToolCall("shape", "inspect", arguments))
    assert result.ok is valid


def test_inspect_native_schema_has_explicit_array_type_without_union():
    schema = next(tool for tool in _full_runtime().advertised_tools("slide")
                  if tool["name"] == "inspect")["input_schema"]["properties"]["page"]
    assert schema["type"] == "array"
    assert schema["items"]["type"] == "integer"
    assert not {"oneOf", "anyOf"} & schema.keys()
    runtime = _full_runtime()
    result = runtime.execute(ToolContext("orchestrator", "orchestrator"),
        ToolCall("string-array", "inspect", {"page": "[1,2]"}))
    assert not result.ok and result.error_code == "invalid_arguments"
    assert "must be an array; received string" in result.content
    assert len(runtime.events) == 2


@pytest.mark.parametrize("arguments", [{}, {"page": [1], "path": "assets/photo.png"}])
def test_inspect_target_exclusivity_is_checked_before_any_image_work(tmp_path, arguments):
    workspace = WorkspaceState(tmp_path)
    artifacts = ArtifactStore(tmp_path, workspace_state=workspace)

    def unexpected_io(*_args):
        pytest.fail("invalid inspection targets must not render or analyze images")

    inspect, _commit = build_inspect_handlers(workspace, artifacts, unexpected_io, unexpected_io)
    runtime = ToolRuntime()
    register_tools(runtime, {"inspect": inspect}, require_complete=False)
    result = runtime.execute(ToolContext("orchestrator", "orchestrator"),
        ToolCall("invalid-target", "inspect", arguments))
    assert not result.ok and result.error_code == "invalid_arguments"
    assert "page 与 path 必须且只能提供一个" in result.content
    assert len(runtime.events) == 2


def test_artifact_commit_contract_explains_automatic_authoring_commits() -> None:
    runtime = _full_runtime()
    spec = tool_specs()["artifact_commit"]
    assert spec.model_visible is False
    for role in ("slide", "image"):
        assert "artifact_commit" not in {
            item["name"] for item in runtime.advertised_tools(role)
        }
        assert "write_file/patch" in spec.description
        assert "image.py finalize" in spec.description
        assert "无需额外调用" in spec.description
        ids = spec.input_schema["properties"]["artifact_ids"]
        assert "page-html-01" in ids["description"]
        assert "不是文件路径" in ids["description"]


def test_internal_compatibility_tools_are_not_model_visible() -> None:
    runtime = _full_runtime()
    description = tool_specs()["delegate_submit"].description
    assert "自动等待真实结果" in description and "自然结束本轮" in description
    for name in ("artifact_commit", "delegate_wait", "render"):
        assert tool_specs()[name].model_visible is False
    for role in ("orchestrator", "slide"):
        assert "render" not in {item["name"] for item in runtime.advertised_tools(role)}
    assert "delegate_wait" not in {
        item["name"] for item in runtime.advertised_tools("orchestrator")
    }
    result = runtime.execute(
        ToolContext("orchestrator", "orchestrator"),
        ToolCall("legacy-render", "render", {"pages": [1]}),
    )
    assert result.ok
    waited = runtime.execute(
        ToolContext("orchestrator", "orchestrator"),
        ToolCall("legacy-wait", "delegate_wait", {}),
    )
    assert waited.ok


def test_inspect_and_terminal_describe_the_automatic_lifecycle() -> None:
    runtime = _full_runtime()
    tools = {item["name"]: item for item in runtime.advertised_tools("slide")}
    assert "自动渲染" in tools["inspect"]["description"]
    assert "不要求先调用其他渲染工具" in tools["inspect"]["description"]
    assert "不需要生命周期命令" in tools["terminal"]["description"]


def test_every_public_tool_has_exactly_one_schema_and_one_adapter() -> None:
    specs = tool_specs()
    # 单一真源：每个工具名恰好出现一次，且携带唯一 Schema。
    assert len(specs) == len({spec.name for spec in specs.values()})
    runtime = ToolRuntime()
    handlers = {name: (lambda _c, _a: "ok") for name in specs}
    register_tools(runtime, handlers)
    # 再次注册同名工具会被运行时拒绝，证明没有第二处装配点。
    with pytest.raises(ValueError):
        register_tools(runtime, {"render": (lambda _c, _a: "ok")}, require_complete=False)


def test_render_and_patch_have_single_canonical_names() -> None:
    specs = tool_specs()
    assert "render" in specs and "patch" in specs
    # patch_file 漂移已消除：只有 patch。
    assert "patch_file" not in specs
    # terminal 的 slide render 与独立 render 各自唯一，Trace 名称不会重复定义。
    assert specs["terminal"].name == "terminal"
    assert specs["render"].name == "render"


def _render_page(tmp_path: Path) -> tuple[WorkspaceState, ArtifactStore, dict]:
    """冻结画布、登记并渲染第 1 页，返回可供 vision 检查的真实 PNG 状态。"""
    workspace = WorkspaceState(tmp_path)
    canvas = write_geometry(
        tmp_path,
        geometry(
            width_px=1600,
            height_px=900,
        ),
    )
    artifacts = ArtifactStore(tmp_path, workspace_state=workspace)
    workspace.register_pages([1])
    workspace.paths.page_html(1).write_text(
        "<html><body>page</body></html>", encoding="utf-8"
    )
    workspace.register_scaffold(1)
    artifacts.declare(
        artifact_id="page-html-01",
        kind="page_html",
        path=workspace.paths.page_html(1),
        owner_id="owner-slide",
        page=1,
    )
    artifacts.commit(owner_id="owner-slide", artifact_ids=["page-html-01"], source_hashes={})

    def renderer(_html: Path, png: Path, received: dict):
        png.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (received["width_px"], received["height_px"]), (244, 241, 234)).save(png)
        return {"geometry": received, "loaded_resources": [], "failed_resources": []}

    artifacts.render_pages([1], renderer)
    return workspace, artifacts, canvas


@pytest.mark.parametrize("is_author", [True, False])
def test_deck_render_inspection_advances_only_for_actual_author(tmp_path: Path, is_author) -> None:
    workspace, artifacts, _ = _render_page(tmp_path)
    owners = OwnerRegistry(tmp_path, deck_id=workspace.deck_id)
    if is_author:
        owners.claim_orchestrator_page(1)
    else:
        owners.accept(role="slide", pages=[1], group_id="page-1")

    def analyzer(_path, _q, _s, _canvas):
        return {"summary": "clean"}

    handler = build_inspect_handlers(workspace, artifacts, None, analyzer)[0]
    result = handler(
        ToolContext(owner_id="orchestrator", role="orchestrator", read_scopes=(WriteScope("renders"),)),
        {"path": "renders/slide_01.png", "question": "check"},
    )
    assert result["deck_page_inspection"] is True
    assert result["inspection_id"].startswith("inspection-")
    assert workspace.page(1).lifecycle == PageLifecycle.RENDERED
    _, commit = build_inspect_handlers(workspace, artifacts, None, analyzer)
    commit(result["pending_inspection"])
    assert workspace.page(1).lifecycle == (PageLifecycle.INSPECTED if is_author else PageLifecycle.RENDERED)


def test_attachment_page_view_materializes_and_does_not_advance_inspected(
    tmp_path: Path,
) -> None:
    workspace, artifacts, _ = _render_page(tmp_path)
    # 一个尚未物化的附件 PDF 页图（首次只存在描述，不存在真实 PNG）。
    src = tmp_path / "src" / "doc.pdf"
    src.parent.mkdir(parents=True, exist_ok=True)
    try:
        import pymupdf as fitz
    except ImportError:  # pragma: no cover - env guard
        import fitz  # type: ignore[no-redef]
    document = fitz.open()
    document.new_page().insert_text((72, 72), "attachment page one")
    document.save(src)
    document.close()
    AttachmentStager(tmp_path).stage([src])
    record = resolve_attachment_records(tmp_path)[0]
    page_assets = resolve_page_assets(record)
    assert page_assets and not page_assets[0].is_file()  # 首次未物化
    relative = workspace.paths.relative(page_assets[0])

    seen: list[Path] = []

    def analyzer(path, _q, _s, _canvas):
        seen.append(path)
        return {"summary": "attachment pixels"}

    handler = build_inspect_handlers(workspace, artifacts, None, analyzer)[0]
    result = handler(
        ToolContext(owner_id="deck:research:e1", role="research", read_scopes=(WriteScope("inputs"),)),
        {"path": relative, "question": "read source"},
    )
    # 首次查看即触发按需物化，真实像素喂给 analyzer（不伪造 summary）。
    assert seen and seen[0].is_file() and seen[0].stat().st_size > 0
    assert result["deck_page_inspection"] is False
    # 附件查看绝不推进 Deck 页面 INSPECTED。
    assert workspace.page(1).lifecycle != PageLifecycle.INSPECTED


def test_scope_page_on_non_deck_target_does_not_fabricate_inspection(
    tmp_path: Path,
) -> None:
    workspace, artifacts, _ = _render_page(tmp_path)
    # 一张普通资源图（非登记 Deck 渲染 PNG）。
    asset = workspace.paths.resolve("assets/logo.png")
    asset.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 64), (0, 0, 0)).save(asset)

    handler, _ = build_inspect_handlers(
        workspace, artifacts, None, lambda _p, _q, _s, _c: {"summary": "asset"}
    )
    result = handler(
        ToolContext(owner_id="deck:image:e1", role="image", read_scopes=(WriteScope("assets"),)),
        {"path": "assets/logo.png", "question": "look"},
    )
    assert result["deck_page_inspection"] is False
    assert workspace.page(1).lifecycle != PageLifecycle.INSPECTED


def test_write_scope_denied_leaves_no_canonical_change(tmp_path: Path) -> None:
    """write_file 越权时事务闭合为错误，磁盘上不产生任何 canonical 改动。"""
    workspace = WorkspaceState(tmp_path)
    write_geometry(
        tmp_path,
        geometry(
            width_px=1600,
            height_px=900,
        ),
    )
    artifacts = ArtifactStore(tmp_path, workspace_state=workspace)

    committed: list[str] = []

    def write_handler(context: ToolContext, arguments):
        # 权限校验发生在运行时 mutating 分支，越权时本 handler 不会被调用。
        committed.append(str(arguments["path"]))
        return {"ok": True}

    runtime = ToolRuntime()
    register_tools(runtime, {"write_file": write_handler}, require_complete=False)
    context = ToolContext(
        owner_id="deck:slide:e1",
        role="slide",
        write_scopes=(WriteScope("slides/slide_01.html", recursive=False),),
    )
    denied = runtime.execute(
        context,
        ToolCall(
            "w-denied",
            "write_file",
            {"path": "slides/slide_02.html", "content": "x"},
        ),
    )
    assert denied.error_code == "write_scope_denied"
    assert committed == []  # handler 从未执行，磁盘无改动
    assert not (tmp_path / "slides" / "slide_02.html").exists()


def test_verification_tool_name_and_control_subset_unchanged() -> None:
    # 控制工具子集保持稳定（不含基础工具），供 WebUI/Trace 语义一致投影。
    assert set(STABLE_TOOL_SCHEMAS) == {
        "delegate_submit",
        "delegate_wait",
        "request_orchestrator",
        "respond_owner_request",
        "artifact_commit",
        "render",
        "inspect",
        "verification_submit",
    }


def _bare_tool_services(workspace: WorkspaceState, artifacts: ArtifactStore):
    """装配写入与实时归属检查的域对象，跳过重量级 Skill/Renderer。"""
    from runtime.production import ProductionToolServices

    services = object.__new__(ProductionToolServices)
    services.workspace = workspace
    services.artifacts = artifacts
    services.owners = OwnerRegistry(workspace.paths.root, deck_id=workspace.deck_id)
    return services


def test_write_commit_atomic_boundary_rolls_back_on_commit_failure(
    tmp_path: Path, monkeypatch,
) -> None:
    """越权在写入前拒绝；合法写入的提交中断则回滚 canonical 文件。"""
    workspace = WorkspaceState(tmp_path)
    write_geometry(
        tmp_path,
        geometry(
            width_px=1600,
            height_px=900,
        ),
    )
    artifacts = ArtifactStore(tmp_path, workspace_state=workspace)
    services = _bare_tool_services(workspace, artifacts)
    owner = services.owners.accept(role="slide", pages=[1], group_id="page-1")

    # 当前归属与写域只允许第 1 页。
    context = ToolContext(
        owner_id=owner.owner_id,
        role="slide",
        metadata={"pages": [1]},
        write_scopes=(WriteScope("slides/slide_01.html", recursive=False),),
    )
    target = workspace.paths.resolve("slides/slide_02.html")
    assert not target.exists()
    with pytest.raises(ToolContractError, match="不能写入"):
        services.write_file(context, {"path": "slides/slide_02.html", "content": "<x/>"})
    # 拒绝后没有孤儿 canonical 改动。
    assert not target.exists()

    # 已存在文件被 patch 但提交失败时，内容回滚到写入前。
    owned = workspace.paths.resolve("slides/slide_01.html")
    owned.parent.mkdir(parents=True, exist_ok=True)
    owned.write_text("<original/>", encoding="utf-8")
    def fail_commit(*_args, **_kwargs):
        raise RuntimeError("commit interrupted")
    monkeypatch.setattr(services, "_commit_slide_if_needed", fail_commit)
    with pytest.raises(RuntimeError, match="commit interrupted"):
        services.write_file(
            context, {"path": "slides/slide_01.html", "content": "<mutated/>"}
        )
    assert owned.read_text(encoding="utf-8") == "<original/>"


def test_write_commit_succeeds_and_registers_when_owner_owns_page(
    tmp_path: Path,
) -> None:
    """归属正确时，写入既落盘又登记为当前 page_html 产物。"""
    workspace = WorkspaceState(tmp_path)
    write_geometry(
        tmp_path,
        geometry(
            width_px=1600,
            height_px=900,
        ),
    )
    artifacts = ArtifactStore(tmp_path, workspace_state=workspace)
    services = _bare_tool_services(workspace, artifacts)
    owner = services.owners.accept(role="slide", pages=[1], group_id="page-1")
    context = ToolContext(
        owner_id=owner.owner_id, role="slide", metadata={"pages": [1]},
        write_scopes=(WriteScope("slides/slide_01.html", recursive=False),),
    )
    result = services.write_file(
        context, {"path": "slides/slide_01.html", "content": "<section/>"}
    )
    assert result["logical_path"] == "slides/slide_01.html"
    assert workspace.paths.resolve("slides/slide_01.html").is_file()
    # 已登记为产物：页面进入 AUTHORED（已提交 HTML），不再是 MISSING。
    assert workspace.page(1).lifecycle == PageLifecycle.AUTHORED


def test_research_rewrite_is_idempotent_across_interruption(tmp_path: Path) -> None:
    """Research 重写指定任务稿：同 tool_use_id 重放幂等、可从中断恢复。"""
    workspace = WorkspaceState(tmp_path)
    write_geometry(
        tmp_path,
        geometry(
            width_px=1600,
            height_px=900,
        ),
    )
    artifacts = ArtifactStore(tmp_path, workspace_state=workspace)
    services = _bare_tool_services(workspace, artifacts)
    owner = services.owners.accept(role="research", group_id="research")

    runtime = ToolRuntime()
    register_tools(
        runtime,
        {"write_file": services.write_file, "patch": services.patch},
        require_complete=False,
    )
    context = ToolContext(
        owner_id=owner.owner_id,
        role="research",
        write_scopes=(WriteScope(owner.output_path, recursive=False),),
    )
    call = ToolCall("r-1", "write_file", {"path": owner.output_path, "content": "v1"})
    first = runtime.execute(context, call)
    # 中断后同 id 重放：闭合事务幂等返回同一 result（不重复副作用）。
    replay = runtime.execute(context, call)
    assert first.ok and replay == first
    # 恢复后以新 tool_use_id 重写，成功推进内容。
    revised = runtime.execute(
        context,
        ToolCall("r-2", "write_file", {"path": owner.output_path, "content": "v2"}),
    )
    assert revised.ok
    patched = runtime.execute(
        context,
        ToolCall(
            "r-3",
            "patch",
            {"path": owner.output_path, "old_text": "v2", "new_text": "v2\nsection"},
        ),
    )
    assert patched.ok
    assert workspace.paths.resolve(owner.output_path).read_text(encoding="utf-8") == "v2\nsection"
