from __future__ import annotations

from geometry_fixture import geometry, write_geometry

from pathlib import Path

from PIL import Image
import pytest


from state.artifacts import ArtifactStateError, ArtifactStatus, ArtifactStore
from state.workspace import PageLifecycle, WorkspaceState, file_sha256


def _author_css(tmp_path: Path) -> dict:
    # layout v2: the workspace manifest must be the first persistent file, so
    # initialize the WorkspaceState before writing any other artifact.
    WorkspaceState(tmp_path)
    spec = geometry(
        width_px=1600,
        height_px=1200,
    )
    return write_geometry(tmp_path, spec)


def _write_png(path: Path, color: tuple[int, int, int] = (20, 80, 160)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1600, 1200), color).save(path)


def _register_image(store: ArtifactStore, path: Path) -> None:
    relative = store.paths.relative(path)
    store.record_image_result(artifact_id=relative, owner_id="owner-image", path=path, status="available")


def _commit_page(
    workspace: WorkspaceState,
    store: ArtifactStore,
    *,
    page: int,
    owner_id: str = "owner-slide",
) -> None:
    artifact_id = f"page-html-{page:02d}"
    store.declare(
        artifact_id=artifact_id,
        kind="page_html",
        path=workspace.paths.page_html(page),
        owner_id=owner_id,
        page=page,
    )
    store.commit(
        owner_id=owner_id,
        artifact_ids=[artifact_id],
        source_hashes={artifact_id: file_sha256(workspace.paths.page_html(page))},
    )


def test_workspace_creation_does_not_choose_default_canvas(tmp_path: Path) -> None:
    workspace = WorkspaceState(tmp_path)

    # layout v2 scaffolds canonical directories, but never fabricates a canvas.
    assert not (tmp_path / "canvas.json").exists()
    assert "canvas" not in workspace.paths.paths


def _write_page_html(workspace: WorkspaceState, page: int) -> None:
    path = workspace.paths.page_html(page)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"<html><body>page {page}</body></html>", encoding="utf-8")


def test_reconcile_retires_pages_absent_from_disk(tmp_path: Path) -> None:
    # FEAT-2.1:结构修订(delete/move-page)会让页号从磁盘消失,但 register_pages 只增不减。
    # reconcile_pages 以磁盘现存页集为准 retire 掉遗留记录,pages() 不再返回它们。
    _author_css(tmp_path)
    workspace = WorkspaceState(tmp_path)
    for page in (1, 2, 3):
        _write_page_html(workspace, page)
    workspace.register_pages([1, 2, 3])
    assert [p.page for p in workspace.pages()] == [1, 2, 3]

    # 模拟 delete-page 2:磁盘只剩 1、3。
    workspace.paths.page_html(2).unlink()
    newly_retired = workspace.reconcile_pages([1, 3])

    assert newly_retired == (2,)
    assert [p.page for p in workspace.pages()] == [1, 3]
    # manifest 也不带幽灵页。
    assert [row["page"] for row in workspace.manifest()["pages"]] == [1, 3]


def test_reconcile_reinstates_page_that_reappears(tmp_path: Path) -> None:
    # retired 页号若重新出现(insert 回填同号 / 再次 reconcile 时在场),清除 retired 标记。
    _author_css(tmp_path)
    workspace = WorkspaceState(tmp_path)
    for page in (1, 2):
        _write_page_html(workspace, page)
    workspace.register_pages([1, 2])
    workspace.reconcile_pages([1])  # retire 2
    assert [p.page for p in workspace.pages()] == [1]

    # 页 2 回填:reconcile 带上它 → 重新活跃。
    workspace.reconcile_pages([1, 2])
    assert [p.page for p in workspace.pages()] == [1, 2]


def test_register_pages_reinstates_a_retired_record(tmp_path: Path) -> None:
    # 直接 register 一个之前被 retire 的页号也应清除 retired 标记。
    _author_css(tmp_path)
    workspace = WorkspaceState(tmp_path)
    _write_page_html(workspace, 1)
    _write_page_html(workspace, 2)
    workspace.register_pages([1, 2])
    workspace.reconcile_pages([1])
    assert [p.page for p in workspace.pages()] == [1]

    workspace.register_pages([2])
    assert [p.page for p in workspace.pages()] == [1, 2]


def test_reconcile_is_noop_and_preserves_lifecycle_when_all_present(tmp_path: Path) -> None:
    # 全部在场时 reconcile 不改动任何页,也不误伤其它页的生命周期。
    _author_css(tmp_path)
    workspace = WorkspaceState(tmp_path)
    for page in (1, 2):
        _write_page_html(workspace, page)
    workspace.register_pages([1, 2])
    before = {p.page: p.lifecycle for p in workspace.pages()}

    assert workspace.reconcile_pages([1, 2]) == ()
    after = {p.page: p.lifecycle for p in workspace.pages()}
    assert before == after


def test_scaffold_is_not_authored_and_hashes_drive_lifecycle(tmp_path: Path) -> None:
    _author_css(tmp_path)
    workspace = WorkspaceState(tmp_path)
    store = ArtifactStore(tmp_path, workspace_state=workspace)
    workspace.register_pages([1])
    html_path = workspace.paths.page_html(1)
    html_path.write_text("<html><body>scaffold</body></html>", encoding="utf-8")

    scaffold = workspace.register_scaffold(1)

    assert scaffold.lifecycle == PageLifecycle.SCAFFOLDED
    _commit_page(workspace, store, page=1)
    assert workspace.page(1).lifecycle == PageLifecycle.AUTHORED

    seen_canvas: list[tuple[int, int]] = []

    def renderer(_html: Path, png: Path, canvas: dict):
        seen_canvas.append((canvas["width_px"], canvas["height_px"]))
        _write_png(png)
        return {"geometry": canvas, "loaded_resources": [], "failed_resources": []}

    rendered = store.render_pages([1], renderer)[0]
    assert rendered.lifecycle == PageLifecycle.RENDERED
    assert seen_canvas == [(1600, 1200)]
    inspected = store.record_inspection(1, inspection_id="vision-001")
    assert inspected.lifecycle == PageLifecycle.INSPECTED
    assert workspace.mark_ready(1).lifecycle == PageLifecycle.READY
    repeated = store.record_inspection(1, inspection_id="vision-final-review")
    assert repeated.lifecycle == PageLifecycle.READY
    state = workspace.path.read_text(encoding="utf-8")
    assert '"ready_preserved": true' in state

    workspace.invalidate_previous_author_review(1, "new-author")
    assert workspace.page(1).lifecycle == PageLifecycle.RENDERED
    assert workspace.page(1).png_sha256 == repeated.png_sha256
    store.record_inspection(1, inspection_id="new-author-consumed-pixels")
    assert workspace.mark_ready(1).lifecycle == PageLifecycle.READY

    html_path.write_text("<html><body>changed</body></html>", encoding="utf-8")
    stale = workspace.page(1)
    assert stale.lifecycle == PageLifecycle.SCAFFOLDED
    assert stale.stale_png is True


def test_render_rejects_missing_broken_and_unverified_external_images(
    tmp_path: Path,
) -> None:
    _author_css(tmp_path)
    workspace = WorkspaceState(tmp_path)
    store = ArtifactStore(tmp_path, workspace_state=workspace)
    workspace.register_pages([2])
    html_path = workspace.paths.page_html(2)
    html_path.write_text(
        '<html><body><img src="../assets/evidence.png"></body></html>',
        encoding="utf-8",
    )
    workspace.register_scaffold(2)
    _commit_page(workspace, store, page=2)

    missing = store.validate_html_assets(2)
    assert missing.missing == ("../assets/evidence.png",)

    asset_path = workspace.paths.assets / "evidence.png"
    asset_path.write_bytes(b"not-an-image")
    broken = store.validate_html_assets(2)
    assert broken.broken == ("../assets/evidence.png",)

    _write_png(asset_path)
    assert store.validate_html_assets(2).unregistered == ("assets/evidence.png",)
    _register_image(store, asset_path)
    assert store.validate_html_assets(2).passed is True

    # BUG-I 回归:slide 片段常写根相对的 ``assets/x``(27b 的自然写法),真实渲染
    # 加载根 present.html(base=根),该引用指向根 assets/。校验器必须按根解析,
    # 不能相对 slides/ 片段目录解析成 slides/assets/ 而误判 missing。
    html_path.write_text(
        '<html><body><img src="assets/evidence.png"></body></html>',
        encoding="utf-8",
    )
    _commit_page(workspace, store, page=2)
    assert store.validate_html_assets(2).passed is True

    html_path.write_text(
        '<html><body><img src="https://cdn.example/evidence.png"></body></html>',
        encoding="utf-8",
    )
    _commit_page(workspace, store, page=2)
    external = store.validate_html_assets(2)
    assert external.external_unverified == ("https://cdn.example/evidence.png",)
    assert store.validate_html_assets(
        2, loaded_resources=["https://cdn.example/evidence.png"]
    ).passed


def test_renderer_failure_receipt_cannot_be_reported_as_pass(tmp_path: Path) -> None:
    _author_css(tmp_path)
    workspace = WorkspaceState(tmp_path)
    store = ArtifactStore(tmp_path, workspace_state=workspace)
    workspace.register_pages([3])
    workspace.paths.page_html(3).write_text(
        "<html><body>page</body></html>", encoding="utf-8"
    )
    workspace.register_scaffold(3)
    _commit_page(workspace, store, page=3)

    def renderer(_html: Path, png: Path, _canvas: dict):
        _write_png(png)
        return {"geometry": _canvas, "loaded_resources": [], "failed_resources": ["assets/lost.png"]}

    with pytest.raises(ArtifactStateError, match="failed resources"):
        store.render_pages([3], renderer)

    assert workspace.page(3).lifecycle == PageLifecycle.AUTHORED


def test_renderer_can_explicitly_commit_deterministic_html_normalization(
    tmp_path: Path,
) -> None:
    _author_css(tmp_path)
    workspace = WorkspaceState(tmp_path)
    store = ArtifactStore(tmp_path, workspace_state=workspace)
    workspace.register_pages([4])
    html_path = workspace.paths.page_html(4)
    html_path.write_text(
        "<html><body style=\"font-family: literal\">page</body></html>",
        encoding="utf-8",
    )
    workspace.register_scaffold(4)
    _commit_page(workspace, store, page=4)
    before_sha256 = file_sha256(html_path)

    def renderer(source: Path, png: Path, _canvas: dict):
        source.write_text(
            "<html><body style=\"font-family: var(--font-body)\">page</body></html>",
            encoding="utf-8",
        )
        after_sha256 = file_sha256(source)
        _write_png(png)
        return {
            "geometry": _canvas, "loaded_resources": [],
            "failed_resources": [],
            "canonical_html_normalization": {
                "producer": "frozen-skill",
                "before_sha256": before_sha256,
                "after_sha256": after_sha256,
            },
        }

    rendered = store.render_pages([4], renderer)[0]

    assert rendered.lifecycle == PageLifecycle.RENDERED
    assert rendered.authored_html_sha256 == file_sha256(html_path)
    assert store.artifact("page-html-04").sha256 == file_sha256(html_path)
    receipt = store.render_receipt(4)
    assert receipt["canonical_html_normalization"]["producer"] == "frozen-skill"


def test_renderer_cannot_mutate_committed_html_without_receipt(tmp_path: Path) -> None:
    _author_css(tmp_path)
    workspace = WorkspaceState(tmp_path)
    store = ArtifactStore(tmp_path, workspace_state=workspace)
    workspace.register_pages([5])
    html_path = workspace.paths.page_html(5)
    html_path.write_text("<html><body>page</body></html>", encoding="utf-8")
    workspace.register_scaffold(5)
    _commit_page(workspace, store, page=5)

    def renderer(source: Path, png: Path, _canvas: dict):
        source.write_text("<html><body>mutated</body></html>", encoding="utf-8")
        _write_png(png)
        return {"geometry": _canvas, "loaded_resources": [], "failed_resources": []}

    with pytest.raises(ArtifactStateError, match="without a normalization receipt"):
        store.render_pages([5], renderer)


def test_page_artifact_can_transfer_to_authorized_repair_owner(tmp_path: Path) -> None:
    _author_css(tmp_path)
    workspace = WorkspaceState(tmp_path)
    store = ArtifactStore(tmp_path, workspace_state=workspace)
    workspace.register_pages([6])
    html_path = workspace.paths.page_html(6)
    html_path.write_text("<html><body>first owner</body></html>", encoding="utf-8")
    workspace.register_scaffold(6)
    _commit_page(workspace, store, page=6, owner_id="owner-epoch-1")

    transferred = store.transfer_page_owner(6, owner_id="owner-epoch-2")

    assert transferred is not None
    assert transferred.owner_id == "owner-epoch-2"
    html_path.write_text("<html><body>repair owner</body></html>", encoding="utf-8")
    with pytest.raises(ArtifactStateError, match="may not commit"):
        store.commit(
            owner_id="owner-epoch-1",
            artifact_ids=["page-html-06"],
            source_hashes={},
        )
    repaired = store.commit(
        owner_id="owner-epoch-2",
        artifact_ids=["page-html-06"],
        source_hashes={},
    )[0]
    assert repaired.owner_id == "owner-epoch-2"
    assert workspace.page(6).lifecycle == PageLifecycle.AUTHORED


def test_image_catalog_changes_only_with_actual_result_or_repair(tmp_path: Path) -> None:
    workspace = WorkspaceState(tmp_path)
    store = ArtifactStore(tmp_path, workspace_state=workspace)
    image_path = workspace.paths.assets / "hero.png"
    _write_png(image_path, (10, 20, 30))

    first = store.record_image_result(
        artifact_id="image-hero",
        owner_id="owner-image",
        path=image_path,
        status="available",
        source_url="https://source.example/hero-v1.png",
    )
    first_catalog = store.catalog()["sha256"]
    assert (first.pixel_width, first.pixel_height) == (1600, 1200)
    saved = ArtifactStore(tmp_path).catalog()["images"][0]
    assert (saved["pixel_width"], saved["pixel_height"], saved["aspect_ratio"]) == (1600, 1200, 4 / 3)
    _write_png(image_path, (200, 40, 20))
    repaired = store.record_image_result(
        artifact_id="image-hero",
        owner_id="owner-image",
        path=image_path,
        status="available",
        source_url="https://source.example/hero-v2.png",
    )

    assert first.status == ArtifactStatus.COMMITTED
    assert repaired.revision == first.revision + 1
    assert repaired.sha256 != first.sha256
    assert store.catalog()["sha256"] != first_catalog
    assert repaired.source_url.endswith("hero-v2.png")

    unavailable = store.record_image_result(
        artifact_id="image-secondary",
        owner_id="owner-image",
        path=workspace.paths.assets / "secondary.png",
        status="unavailable",
        source_url="https://source.example/missing.png",
        reason="fetch_http_404",
    )
    assert unavailable.status == ArtifactStatus.UNAVAILABLE
    assert unavailable.sha256 == ""


def test_unavailable_image_path_can_be_rebound_with_auditable_revision(tmp_path):
    import json
    workspace = WorkspaceState(tmp_path)
    store = ArtifactStore(tmp_path, workspace_state=workspace)
    previous = store.record_image_result(artifact_id="hero", owner_id="image", path="assets/missing.jpg",
        status="unavailable", reason="file_missing")
    _write_png(tmp_path / "assets/generated.png")
    repaired = store.record_image_result(artifact_id="hero", owner_id="image", path="assets/generated.png", status="available")
    assert repaired.status == ArtifactStatus.COMMITTED and repaired.revision == previous.revision + 1
    saved = ArtifactStore(tmp_path).catalog()["images"][0]
    assert saved["path"] == "assets/generated.png" and saved["sha256"] == repaired.sha256
    events = json.loads((tmp_path / "_state/artifacts.json").read_text())["events"]
    rebound = next(event for event in events if event["type"] == "image_path_rebound")
    assert rebound["previous"] == previous.to_dict()


@pytest.mark.parametrize("failure", ["owner", "committed", "leased", "missing", "outside_assets"])
def test_image_rebinding_does_not_bypass_existing_artifact_contract(tmp_path, failure):
    workspace = WorkspaceState(tmp_path)
    store = ArtifactStore(tmp_path, workspace_state=workspace)
    _write_png(tmp_path / "assets/original.png")
    store.record_image_result(artifact_id="hero", owner_id="image", path="assets/original.png",
        status="available" if failure == "committed" else "unavailable", reason="missing")
    path = "renders/new.png" if failure == "outside_assets" else "assets/new.png"
    if failure != "missing":
        _write_png(tmp_path / path)
    if failure == "leased":
        store.begin_transaction("image", ["hero"])
    before = (tmp_path / "_state/artifacts.json").read_bytes()
    with pytest.raises(ArtifactStateError):
        store.record_image_result(artifact_id="hero", owner_id="other" if failure == "owner" else "image",
            path=path, status="available")
    assert (tmp_path / "_state/artifacts.json").read_bytes() == before


def test_artifact_lease_blocks_implicit_or_wrong_owner_commit(tmp_path: Path) -> None:
    workspace = WorkspaceState(tmp_path)
    store = ArtifactStore(tmp_path, workspace_state=workspace)
    shared = workspace.paths.assets / "theme.css"
    shared.write_text(":root{--ink:#123;}", encoding="utf-8")
    store.declare(
        artifact_id="shared-theme",
        kind="shared",
        path=shared,
        owner_id="owner-slide",
    )
    transaction_id = store.begin_transaction("owner-slide", ["shared-theme"])

    with pytest.raises(ArtifactStateError, match="require their transaction_id"):
        store.commit(
            owner_id="owner-slide",
            artifact_ids=["shared-theme"],
            source_hashes={},
        )
    with pytest.raises(ArtifactStateError, match="owner does not match"):
        store.commit(
            owner_id="owner-other",
            artifact_ids=["shared-theme"],
            source_hashes={},
            transaction_id=transaction_id,
        )

    committed = store.commit(
        owner_id="owner-slide",
        artifact_ids=["shared-theme"],
        source_hashes={"shared-theme": file_sha256(shared)},
        transaction_id=transaction_id,
    )[0]
    assert committed.status == ArtifactStatus.COMMITTED


def test_workspace_manifest_propagates_one_canvas_contract(tmp_path: Path) -> None:
    canvas = _author_css(tmp_path)
    workspace = WorkspaceState(tmp_path)

    manifest = workspace.manifest()

    assert manifest["canvas"] == canvas
    assert "css_variables" not in manifest


def _inspected_render_fixture(tmp_path):
    _author_css(tmp_path)
    workspace = WorkspaceState(tmp_path)
    store = ArtifactStore(tmp_path, workspace_state=workspace)
    workspace.register_pages([1])
    workspace.paths.page_html(1).write_text(
        '<link rel="stylesheet" href="../assets/theme.css">'
        '<img src="../assets/picture.png"><main>Page</main>', encoding="utf-8")
    (workspace.paths.assets / "theme.css").write_text("main {color: navy}", encoding="utf-8")
    (tmp_path / "base.css").write_text((tmp_path / "base.css").read_text() + "\nmain {font-size: 30px}", encoding="utf-8")
    _write_png(workspace.paths.assets / "picture.png")
    _register_image(store, workspace.paths.assets / "picture.png")
    _commit_page(workspace, store, page=1)
    calls = []
    def renderer(_html, png, _canvas):
        calls.append(1)
        _write_png(png, (20 + len(calls), 80, 160))
        return {"geometry": _canvas, "loaded_resources": [], "failed_resources": []}
    store.render_pages([1], renderer)
    store.record_inspection(1, inspection_id="author-inspection")
    return workspace, store, renderer, calls


@pytest.mark.parametrize("ready", [False, True])
def test_current_render_receipt_reuses_page_without_renderer_or_inspection_loss(tmp_path, ready):
    workspace, store, renderer, calls = _inspected_render_fixture(tmp_path)
    if ready:
        workspace.mark_ready(1)
    before = workspace.page(1).to_dict()
    png_mtime = workspace.paths.page_png(1).stat().st_mtime_ns
    receipt = store.render_receipt(1)
    assert store.render_pages([1], renderer)[0].to_dict() == before
    assert len(calls) == 1
    assert store.render_receipt(1) == receipt
    assert workspace.paths.page_png(1).stat().st_mtime_ns == png_mtime


@pytest.mark.parametrize("changed", ["html", "png", "css", "base_css", "image", "missing_png"])
def test_render_receipt_cache_invalidates_changed_bytes_and_dependencies(tmp_path, changed):
    workspace, store, renderer, calls = _inspected_render_fixture(tmp_path)
    if changed == "html":
        path = workspace.paths.page_html(1)
        path.write_text(path.read_text() + "<p>New content</p>", encoding="utf-8")
        _commit_page(workspace, store, page=1)
    elif changed == "png":
        _write_png(workspace.paths.page_png(1), (100, 20, 40))
    elif changed == "missing_png":
        workspace.paths.page_png(1).unlink()
    elif changed == "css":
        (workspace.paths.assets / "theme.css").write_text("main {color: red}", encoding="utf-8")
    elif changed == "base_css":
        (tmp_path / "base.css").write_text((tmp_path / "base.css").read_text() + "\nmain {font-size: 50px}", encoding="utf-8")
    else:
        _write_png(workspace.paths.assets / "picture.png", (100, 20, 40))
        _register_image(store, workspace.paths.assets / "picture.png")
    after = store.render_pages([1], renderer)[0]
    assert len(calls) == 2
    assert after.lifecycle == PageLifecycle.RENDERED
    assert after.inspection_id == ""
    store.validate_render_receipt(1)


def test_render_receipt_recomputes_dependency_closure(tmp_path):
    import json

    workspace, store, renderer, calls = _inspected_render_fixture(tmp_path)
    state_path = tmp_path / "_state" / "artifacts.json"
    state = json.loads(state_path.read_text())
    assert state["render_receipts"]["1"]["resource_hashes"].pop("base.css")
    state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ArtifactStateError, match="visual resources changed"):
        store.validate_render_receipt(1)
    store.render_pages([1], renderer)
    assert len(calls) == 2
    assert store.render_receipt(1)["resource_hashes"]["base.css"] == file_sha256(tmp_path / "base.css")


def test_render_dependencies_follow_stylesheet_relative_imports(tmp_path):
    workspace, store, renderer, calls = _inspected_render_fixture(tmp_path)
    (workspace.paths.assets / "theme.css").write_text(
        '@import "nested.css"; main {background: url("picture.png")}', encoding="utf-8")
    nested = workspace.paths.assets / "nested.css"
    nested.write_text('main {color: blue; background: url("../assets/picture.png")}', encoding="utf-8")
    store.render_pages([1], renderer)
    hashes = store.render_receipt(1)["resource_hashes"]
    assert hashes["assets/nested.css"] == file_sha256(nested)
    assert "assets/picture.png" in hashes
    nested.write_text("main {color: red}", encoding="utf-8")
    store.render_pages([1], renderer)
    assert len(calls) == 3


@pytest.mark.parametrize("element", [
    '<style>main {background: url("assets/extra.png"); filter: url(#shadow)}</style>',
    '<img src="assets/picture.png" srcset="assets/extra.png 2x">',
    '<script src="assets/chart.js"></script>',
])
def test_render_dependencies_include_inline_css_srcset_and_script(tmp_path, element):
    workspace, store, renderer, calls = _inspected_render_fixture(tmp_path)
    html = workspace.paths.page_html(1)
    html.write_text(html.read_text() + element, encoding="utf-8")
    dependency = workspace.paths.assets / ("chart.js" if "<script" in element else "extra.png")
    if dependency.suffix == ".js":
        dependency.write_text("window.chartVersion=1", encoding="utf-8")
    else:
        _write_png(dependency)
        _register_image(store, dependency)
    _commit_page(workspace, store, page=1)
    store.render_pages([1], renderer)
    assert store.render_receipt(1)["resource_hashes"][f"assets/{dependency.name}"] == file_sha256(dependency)
    if dependency.suffix == ".js":
        dependency.write_text("window.chartVersion=2", encoding="utf-8")
    else:
        _write_png(dependency, (150, 100, 50))
        _register_image(store, dependency)
    store.render_pages([1], renderer)
    assert len(calls) == 3


def test_render_dependencies_follow_external_svg_base_and_nested_svg(tmp_path):
    workspace, store, renderer, calls = _inspected_render_fixture(tmp_path)
    html = workspace.paths.page_html(1)
    html.write_text(html.read_text() + '<img src="assets/icons/chart.svg">', encoding="utf-8")
    bitmap = workspace.paths.assets / "icons/backdrop.png"
    _write_png(bitmap)
    _register_image(store, bitmap)
    (bitmap.parent / "chart.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><style>image{filter:url(#blur)}</style>'
        '<use href="nested.svg#picture"/></svg>', encoding="utf-8")
    (bitmap.parent / "nested.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><image id="picture" href="backdrop.png"/></svg>',
        encoding="utf-8")
    _commit_page(workspace, store, page=1)
    store.render_pages([1], renderer)
    assert store.render_receipt(1)["resource_hashes"]["assets/icons/backdrop.png"] == file_sha256(bitmap)
    _write_png(bitmap, (150, 100, 50))
    _register_image(store, bitmap)
    store.render_pages([1], renderer)
    assert len(calls) == 3


@pytest.mark.parametrize("ready", [False, True])
def test_direct_identical_render_preserves_author_inspection_and_ready(tmp_path, ready):
    workspace, store, _renderer, _calls = _inspected_render_fixture(tmp_path)
    if ready:
        workspace.mark_ready(1)
    before = workspace.page(1).to_dict()
    _write_png(workspace.paths.page_png(1), (21, 80, 160))
    after = workspace.mark_rendered(1,
        html_sha256=file_sha256(workspace.paths.page_html(1)),
        png_sha256=file_sha256(workspace.paths.page_png(1)))
    assert after.to_dict() == before
    assert after.inspection_id == "author-inspection"


def test_cached_render_rebinds_returned_html_after_unrendered_intermediate_edit(tmp_path):
    workspace, store, renderer, calls = _inspected_render_fixture(tmp_path)
    html = workspace.paths.page_html(1)
    original = html.read_text()
    png_hash = file_sha256(workspace.paths.page_png(1))
    html.write_text(original + "<p>Unrendered edit</p>", encoding="utf-8")
    _commit_page(workspace, store, page=1)
    html.write_text(original, encoding="utf-8")
    _commit_page(workspace, store, page=1)
    assert workspace.page(1).lifecycle == PageLifecycle.AUTHORED
    store.validate_render_receipt(1)
    restored = store.render_pages([1], renderer)[0]
    assert len(calls) == 1
    assert restored.lifecycle == PageLifecycle.RENDERED
    assert restored.rendered_html_sha256 == file_sha256(html)
    assert restored.rendered_png_sha256 == png_hash
    store.record_inspection(1, inspection_id="restored-pixels-consumed")
    assert workspace.page(1).lifecycle == PageLifecycle.INSPECTED


@pytest.mark.parametrize("reference", ["img", "inline-css", "stylesheet", "nested-svg"])
def test_delivery_image_registration_uses_actual_recursive_dependencies(tmp_path, reference):
    _author_css(tmp_path)
    workspace = WorkspaceState(tmp_path)
    store = ArtifactStore(tmp_path, workspace_state=workspace)
    workspace.register_pages([1])
    picture = workspace.paths.assets / "evidence.png"
    _write_png(picture)
    _write_png(workspace.paths.assets / "unused-experiment.png")
    if reference == "img":
        html = '<img src="assets/evidence.png">'
    elif reference == "inline-css":
        html = '<style>main{background:url("assets/evidence.png")}</style><main>Page</main>'
    elif reference == "stylesheet":
        (workspace.paths.assets / "theme.css").write_text('@import "nested.css";')
        (workspace.paths.assets / "nested.css").write_text('main{background:url("evidence.png")}')
        html = '<link rel="stylesheet" href="assets/theme.css"><main>Page</main>'
    else:
        (workspace.paths.assets / "chart.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg"><use href="nested.svg#figure"/></svg>')
        (workspace.paths.assets / "nested.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg"><image id="figure" href="evidence.png"/></svg>')
        html = '<img src="assets/chart.svg">'
    workspace.paths.page_html(1).write_text(html)
    validation = store.validate_html_assets(1)
    assert not validation.passed and validation.unregistered == ("assets/evidence.png",)
    assert "unused-experiment" not in repr(validation.to_dict())
    _register_image(store, picture)
    assert store.validate_html_assets(1).passed
    assert len(store.catalog()["images"]) == 1


@pytest.mark.parametrize("status", ["pending", "unavailable"])
def test_existing_bitmap_requires_a_committed_registration(tmp_path, status):
    _author_css(tmp_path)
    workspace = WorkspaceState(tmp_path)
    store = ArtifactStore(tmp_path, workspace_state=workspace)
    workspace.register_pages([1])
    workspace.paths.page_html(1).write_text('<img src="assets/evidence.png">')
    picture = workspace.paths.assets / "evidence.png"
    _write_png(picture)
    store.record_image_result(artifact_id="evidence", owner_id="owner-image", path=picture,
                              status=status, reason="not approved")
    validation = store.validate_html_assets(1)
    assert not validation.passed and validation.uncommitted == ("assets/evidence.png",)
    store.record_image_result(artifact_id="evidence", owner_id="owner-image", path=picture, status="available")
    assert store.validate_html_assets(1).passed
    _write_png(picture, (200, 40, 10))
    validation = store.validate_html_assets(1)
    assert not validation.passed and validation.stale == ("assets/evidence.png",)


def test_pictureless_page_ignores_unregistered_and_unavailable_candidates(tmp_path):
    _author_css(tmp_path)
    workspace = WorkspaceState(tmp_path)
    store = ArtifactStore(tmp_path, workspace_state=workspace)
    workspace.register_pages([1])
    workspace.paths.page_html(1).write_text('<main>Text and native layout only</main>')
    (workspace.paths.assets / "broken-experiment.png").write_bytes(b"not an image")
    store.record_image_result(artifact_id="optional", owner_id="owner-image", path="assets/optional.png",
                              status="unavailable", reason="not selected")
    before = store.path.read_bytes()
    assert store.validate_html_assets(1).passed
    assert store.path.read_bytes() == before


def test_current_render_receipt_rechecks_image_registration_without_pixel_drift(tmp_path):
    workspace, store, _renderer, _calls = _inspected_render_fixture(tmp_path)
    receipt = store.render_receipt(1)
    store.record_image_result(artifact_id="assets/picture.png", owner_id="owner-image",
                              path="assets/picture.png", status="unavailable", reason="withdrawn")
    assert store.render_receipt(1) == receipt
    with pytest.raises(ArtifactStateError, match="image registrations invalid.*uncommitted"):
        store.validate_render_receipt(1)
