"""单页隔离回执的局部页号不能丢失整册后续页的内容证据。"""
from contextlib import nullcontext
import json
import os
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest

from geometry_fixture import geometry, write_geometry
from infer import build_inspect_handlers
from owner.registry import OwnerRegistry
from qc.content import ContentQC
from qc.content_review import run_content_review
from runtime.config import RunSpec
from runtime import production
from runtime.tools import ToolContext
from state.artifacts import ArtifactStateError, ArtifactStore
from state.workspace import PageLifecycle, WorkspaceState, file_sha256
from test_workspace_artifacts import _register_image


def _workspace(root, page):
    workspace = WorkspaceState(root)
    write_geometry(root, geometry(width_px=800, height_px=600))
    (root / "base.css").write_text((root / "base.css").read_text()
        + "body{margin:0}.deck{position:absolute;width:var(--canvas-w);height:var(--canvas-h)}")
    workspace.register_pages([page])
    owner = OwnerRegistry(root, deck_id=workspace.deck_id).accept(role="slide", pages=[page])
    asset = workspace.paths.assets / "concept.png"
    Image.new("RGB", (80, 60), "green").save(asset)
    html = workspace.paths.page_html(page)
    html.write_text(f'<section class="slide" data-slide="{page:02d}">'
                    '<h1>AI concept illustration</h1><img src="../assets/concept.png" '
                    'style="width:80px;height:60px"></section>')
    (root / "deck.md").write_text("# Evidence\n## Brief\n- language: en\n")
    (root / "briefs").mkdir()
    for number in range(1, page + 1):
        (root / f"briefs/slide_{number:02d}.md").write_text(f"# P{number:02d} — Evidence\n- job: explain\n")
    artifacts = ArtifactStore(root, workspace_state=workspace)
    _register_image(artifacts, asset)
    workspace.mark_authored(page, html_sha256=file_sha256(html))
    artifact_id = f"page-html-{page:02d}"
    artifacts.declare(artifact_id=artifact_id, kind="page_html", path=html,
                      owner_id=owner.owner_id, page=page)
    artifacts.commit(owner_id=owner.owner_id, artifact_ids=[artifact_id], source_hashes={})
    return workspace, artifacts


def _run_spec(root):
    return RunSpec.from_inputs(
        raw_user_query="Show the image and mark it as an AI concept illustration.",
        output_workspace=root, environment={"MODEL": "test-model"})


def _packet(workspace, artifacts, page):
    return ContentQC.build_packet(_run_spec(workspace.paths.root),
        workspace, pages=[page], render_receipts={page: artifacts.render_receipt(page)})


def _context(workspace, page):
    owner = OwnerRegistry(workspace.paths.root, deck_id=workspace.deck_id).page_owner(page)
    assert owner is not None
    return ToolContext(owner.owner_id, owner.role, metadata={"pages": [page]})


@pytest.mark.parametrize("page", [1, 2, 3])
def test_local_page_one_evidence_survives_artifacts_cache_and_resume(tmp_path, monkeypatch, page):
    workspace, artifacts = _workspace(tmp_path, page)
    cache = tmp_path / f"_trace/render-cache/page_{page:02d}.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"canvas": workspace.require_geometry(), "pages": [
        {"page": 1, "geometry": {"images": [{"src": "assets/concept.png"}],
                                  "text_boxes": [{"text": "AI concept illustration"}]}}]}))
    calls = []
    def command(*args, **kwargs):
        calls.append(args[0])
        Image.new("RGB", (800, 600), "white").save(workspace.paths.page_png(page))
        metadata = json.loads(cache.read_text())
        metadata["pages"][0].update(
            presentation_language=workspace.creation_config()["presentation_language"],
            loaded_resources=["assets/concept.png"], failed_resources=[],
            input_hashes={f"slides/slide_{page:02d}.html": file_sha256(workspace.paths.page_html(page)),
                          "base.css": file_sha256(tmp_path / "base.css"),
                          "assets/concept.png": file_sha256(workspace.paths.assets / "concept.png")},
            png_sha256=file_sha256(workspace.paths.page_png(page)))
        cache.write_text(json.dumps(metadata))
        return SimpleNamespace(stdout="render:cached unchanged HTML/CSS", stderr="", diagnostics={})
    monkeypatch.setattr(production, "_run_command", command)
    snapshot = SimpleNamespace(name="mural-presenter-v1", target=tmp_path / "pinned-skill",
        resolve_skill_path=lambda _: tmp_path / "approved.py", capture_runtime_fonts=lambda: None)
    renderer = production.SkillRenderer(tmp_path, production.ProductionSettings(skill_source=snapshot.target), {},
        SimpleNamespace(acquire=lambda *a, **kw: nullcontext()), snapshot)
    artifacts.render_pages([page], renderer)
    before = _packet(workspace, artifacts, page)
    assert before.pages[0].page == page
    assert before.pages[0].rendered_text == ("AI concept illustration",)
    assert before.pages[0].used_images == ({"path": "assets/concept.png",
        "sha256": file_sha256(workspace.paths.assets / "concept.png")},)
    resumed = WorkspaceState(tmp_path)
    resumed_artifacts = ArtifactStore(tmp_path, workspace_state=resumed)
    resumed_artifacts.render_pages([page], renderer)
    assert len(calls) == 1
    assert _packet(resumed, resumed_artifacts, page) == before
    inspections = []
    def analyzer(*args):
        inspections.append(args)
        return {}
    context = _context(workspace, page)
    inspect, commit = build_inspect_handlers(resumed, resumed_artifacts, renderer, analyzer)
    commit(inspect(context, {"page": page})["pending_inspection"])
    legacy = tmp_path / "_state/artifacts.json"
    state = json.loads(legacy.read_text())
    state["render_receipts"][str(page)]["diagnostics"].pop("visible_images")
    state["render_receipts"][str(page)]["diagnostics"].pop("rendered_text")
    legacy.write_text(json.dumps(state))
    resumed_artifacts = ArtifactStore(tmp_path, workspace_state=resumed)
    assert _packet(resumed, resumed_artifacts, page).pages[0].used_images is None
    reviews = []
    def review(packet):
        reviews.append(packet)
        if packet.pages[0].used_images is None:
            return json.dumps([{"kind": "citation_gap", "severity": "major", "pages": [page],
                                "summary": "No rendered image evidence", "owner_hint": "slide"}])
        return "[]"
    assert not run_content_review(_packet(resumed, resumed_artifacts, page), review,
                                  reviewer_model="offline-reviewer").passed
    assert len(reviews) == 1
    inspect, _ = build_inspect_handlers(resumed, resumed_artifacts, renderer, analyzer)
    repaired = inspect(context, {"page": page})
    assert repaired["inspection_cached"] and not repaired["native_multimodal"]
    assert len(calls) == 2
    assert _packet(resumed, resumed_artifacts, page) == before
    assert inspect(context, {"page": page})["inspection_cached"]
    assert len(inspections) == 1
    resumed_artifacts.render_pages([page], renderer)
    assert len(calls) == 2
    assert run_content_review(_packet(resumed, resumed_artifacts, page), review,
                              reviewer_model="offline-reviewer").passed
    assert len(reviews) == 2


@pytest.mark.parametrize("value", [None, "", {}, 1])
def test_production_cache_requires_array_evidence(value):
    good = {"visible_images": [], "rendered_text": []}
    proof = {"input_hashes": {"base.css": "test-source"}}
    assert production.SkillRenderer.cache_receipt_valid({**proof, "diagnostics": good})
    assert not production.SkillRenderer.cache_receipt_valid({"diagnostics": good})
    for key in good:
        assert not production.SkillRenderer.cache_receipt_valid({**proof, "diagnostics": {**good, key: value}})


@pytest.mark.parametrize("captured_language", [None, "zh", "en"])
def test_skill_renderer_checks_actual_captured_language(tmp_path, monkeypatch, captured_language):
    workspace, _ = _workspace(tmp_path, 1)
    png = workspace.paths.page_png(1)
    Image.new("RGB", (800, 600), "white").save(png)
    cache = tmp_path / "_trace/render-cache/page_01.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    evidence = {"page": 1, "geometry": {}, "loaded_resources": [], "failed_resources": [],
        "input_hashes": {"slides/slide_01.html": file_sha256(workspace.paths.page_html(1)),
                         "base.css": file_sha256(tmp_path / "base.css")},
        "png_sha256": file_sha256(png)}
    if captured_language is not None:
        evidence["presentation_language"] = captured_language
    cache.write_text(json.dumps({"canvas": workspace.require_geometry(), "pages": [evidence]}))
    monkeypatch.setattr(production, "_run_command", lambda *a, **kw:
        SimpleNamespace(stdout="", stderr="", diagnostics={}))
    snapshot = SimpleNamespace(name="mural-presenter-v1", target=tmp_path / "pinned-skill",
        resolve_skill_path=lambda _: tmp_path / "approved.py", capture_runtime_fonts=lambda: None)
    renderer = production.SkillRenderer(tmp_path, production.ProductionSettings(skill_source=snapshot.target), {},
        SimpleNamespace(acquire=lambda *a, **kw: nullcontext()), snapshot)
    if captured_language == "en":
        receipt = renderer(workspace.paths.page_html(1), png, workspace.require_geometry())
        assert receipt["presentation_config"] == {"presentation_language": "en"}
    else:
        with pytest.raises(ArtifactStateError, match="render presentation language is missing or changed"):
            renderer(workspace.paths.page_html(1), png, workspace.require_geometry())


@pytest.mark.parametrize("rows", [[], [{"page": 3}], [{"page": 1}, {"page": 2}]])
def test_ambiguous_single_page_receipt_does_not_silently_drop_evidence(tmp_path, monkeypatch, rows):
    workspace, _ = _workspace(tmp_path, 3)
    cache = tmp_path / "_trace/render-cache/page_03.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"canvas": workspace.require_geometry(), "pages": rows}))
    monkeypatch.setattr(production, "_run_command", lambda *a, **kw:
        SimpleNamespace(stdout="", stderr="", diagnostics={}))
    snapshot = SimpleNamespace(name="mural-presenter-v1", target=tmp_path / "pinned-skill",
        resolve_skill_path=lambda _: tmp_path / "approved.py", capture_runtime_fonts=lambda: None)
    renderer = production.SkillRenderer(tmp_path, production.ProductionSettings(skill_source=snapshot.target), {},
        SimpleNamespace(acquire=lambda *a, **kw: nullcontext()), snapshot)
    with pytest.raises(ArtifactStateError, match="single-page render evidence"):
        renderer(workspace.paths.page_html(3), workspace.paths.page_png(3), workspace.require_geometry())


@pytest.mark.parametrize("family", [None, "Inter", "InterVariable"])
def test_real_third_page_render_reaches_content_packet(tmp_path, family):
    workspace, artifacts = _workspace(tmp_path, 3)
    if family:
        css_path = tmp_path / "base.css"
        css_path.write_text(css_path.read_text() + f'\n:root {{ --font-body: "{family}"; }}\n'
                            'h1 {font-family:var(--font-body); font-weight:700}')
    source = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1"
    settings = production.ProductionSettings(skill_source=source, command_timeout_s=120)
    environment = {**os.environ, "TMPDIR": str(tmp_path)}
    snapshot = production.SkillSnapshot.stage(tmp_path, source, environment)
    renderer = production.SkillRenderer(tmp_path, settings, environment,
        production.ResourceSlots(tmp_path, settings), snapshot)
    try:
        artifacts.render_pages([3], renderer)
        cache = json.loads((tmp_path / "_trace/render-cache/page_03.json").read_text())
        assert [row["page"] for row in cache["pages"]] == [1]
        packet = _packet(workspace, artifacts, 3)
        assert packet.pages[0].rendered_text == ("AI concept illustration",)
        assert packet.pages[0].used_images[0]["path"] == "assets/concept.png"
        assert packet.pages[0].used_images[0]["sha256"] == file_sha256(workspace.paths.assets / "concept.png")
        inspect, commit = build_inspect_handlers(workspace, artifacts, renderer, lambda *args: {})
        context = _context(workspace, 3)
        result = inspect(context, {"page": 3})
        assert result["render_reused"] and result["native_multimodal"]
        assert commit(result["pending_inspection"])
        workspace.mark_ready(3)
        assert workspace.page(3).lifecycle == PageLifecycle.READY
        artifacts.validate_render_receipt(3)
        if family:
            selected = json.loads(workspace.paths.font_manifest.read_text())["selected_faces"]
            assert len([row for row in selected if row["source_family"] == "Inter"]) == 1
    finally:
        renderer.close()
