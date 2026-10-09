"""真实浏览器动态资源：登记、缓存、ready 与离线导出共用渲染依赖。"""
import json
import os
from pathlib import Path
import zipfile

from PIL import Image
import pytest

from export import delivery
from qc.routing import IssueLedger
from runtime.production import ProductionSettings, ResourceSlots, SkillRenderer, SkillSnapshot
from state.artifacts import ArtifactStateError, _html_resource_hashes
from state.workspace import WorkspaceStateError
from test_editable_pptx import browser_environment
from test_revision_state import SKILL, deck, ready, register_image


def test_dynamic_image_freshness_cache_and_portable_exports(deck, browser_environment):
    workspace, store, *_ = deck
    root = workspace.paths.root
    asset = register_image(deck, "dynamic", slides=[1])
    css = root / "base.css"
    css.write_text(css.read_text() + "\nhtml,body{margin:0;padding:0;width:var(--canvas-w);height:var(--canvas-h);overflow:hidden}\n"
                   ".slide.ground-primary-field{background:var(--content-canvas)}\n")
    html = workspace.paths.page_html(1)
    html.write_text('''<section class="slide ground-primary-field" id="slide-01" data-slide="01">
      <h1 style="font-size:48px;color:#142d46">Dynamic asset</h1>
      <img id="photo" alt="Visible local asset" style="display:block;width:1000px;height:700px;object-fit:contain">
      <script>document.getElementById('photo').src = ['assets','dynamic.png'].join('/');</script>
    </section>''')
    page = store.artifact("page-html-01")
    store.commit(owner_id=page.owner_id, artifact_ids=[page.artifact_id], source_hashes={})
    snapshot = SkillSnapshot.stage(root, SKILL, dict(os.environ))
    settings = ProductionSettings(skill_source=SKILL, command_timeout_s=120)
    renderer = SkillRenderer(root, settings, dict(os.environ), ResourceSlots(root, settings, environment=os.environ), snapshot)
    try:
        store.render_pages([1], renderer)
        receipt = store.render_receipt(1)
        assert asset.path in receipt["loaded_resources"]
        assert receipt["resource_hashes"][asset.path] == asset.sha256
        ready(deck)
        before = delivery.file_sha256(workspace.paths.page_png(1))
        Image.new("RGB", (32, 24), "orange").save(root / asset.path)
        with pytest.raises(ArtifactStateError, match="resources changed"):
            store.validate_render_receipt(1)
        with pytest.raises(WorkspaceStateError, match="resources changed"):
            workspace.mark_ready(1)
        store.record_image_result(artifact_id=asset.artifact_id, owner_id=asset.owner_id,
                                  path=root / asset.path, status="available", image_metadata={"slides": [1]})
        store.render_pages([1], renderer)
        assert delivery.file_sha256(workspace.paths.page_png(1)) != before
        current = store.render_receipt(1)
        # 第二次相同输入确实命中，不再次进入 Chromium/CLI。
        old_call = renderer.__class__.__call__
        renderer.__class__.__call__ = lambda *_: pytest.fail("unchanged render missed its cache")
        try:
            store.render_pages([1], renderer)
        finally:
            renderer.__class__.__call__ = old_call
        assert store.render_receipt(1)["resource_hashes"] == current["resource_hashes"]
        ready(deck)
        exporter = delivery.DeliveryExporter(workspace, store, IssueLedger(root))
        exporter.export(include_pdf=False, include_pptx=False, include_zip=False)
        with pytest.raises(ValueError, match="single-file HTML.*runtime resources"):
            delivery.export_on_demand(root, "html")
        assert not (root / "delivery/presentation.html").exists()
        package = delivery.export_on_demand(root, "zip")
        portable = root / "_runtime/tmp/portable-check"
        with zipfile.ZipFile(root / package["path"]) as archive:
            assert archive.read(asset.path) == (root / asset.path).read_bytes()
            # ZIP 的闭包仍不得包含原始输入、内部状态或未引用素材。
            assert not any(name.startswith(("inputs/", "_state/", "_trace/")) for name in archive.namelist())
            assert all(not Path(name).is_absolute() and ".." not in Path(name).parts for name in archive.namelist())
            archive.extractall(portable)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=browser_environment["PPT_SKILL_BROWSER_EXE"],
                headless=True, args=["--no-sandbox"], env={**os.environ, "TMPDIR": str(root / "_runtime/tmp")})
            try:
                preview = browser.new_page()
                preview.goto((portable / "present.html").as_uri())
                preview.wait_for_function("document.getElementById('photo')?.naturalWidth === 32")
                assert preview.locator("#photo").evaluate("e => e.naturalHeight") == 24
            finally:
                browser.close()
        pdf = delivery.export_on_demand(root, "pdf")
        import pymupdf
        with pymupdf.open(root / pdf["path"]) as document:
            assert len(document) == 3 and document[0].get_images()
        pptx = delivery.export_on_demand(root, "pptx-editable")
        from pptx import Presentation
        assert len(Presentation(root / pptx["path"]).slides) == 3
        # 移除动态引用后，旧依赖不应永久粘在渲染缓存中。
        html.write_text('<section class="slide" id="slide-01" data-slide="01"><h1>Text only now</h1></section>')
        store.commit(owner_id=page.owner_id, artifact_ids=[page.artifact_id], source_hashes={})
        store.render_pages([1], renderer)
        assert asset.path not in store.render_receipt(1)["resource_hashes"]
    finally:
        renderer.close()


def test_observed_resource_hashes_decode_urls_and_reject_escape(tmp_path):
    (tmp_path / "assets").mkdir()
    image = tmp_path / "assets/a #1.png"
    Image.new("RGB", (2, 3)).save(image)
    html = tmp_path / "page.html"
    html.write_text("<h1>Dynamic</h1>")
    assert _html_resource_hashes(tmp_path, html, loaded_resources=["assets/a%20%231.png?v=2"]) == {
        "assets/a #1.png": delivery.file_sha256(image)}
    for reference in ("../outside.png", "assets/missing.png"):
        with pytest.raises(ArtifactStateError):
            _html_resource_hashes(tmp_path, html, loaded_resources=[reference])
    assert _html_resource_hashes(tmp_path, html, loaded_resources=["https://cdn.example/live.png"]) == {}
