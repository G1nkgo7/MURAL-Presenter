"""Studio 完成判定只读已固定 generation，不从 source Skill 补建。"""
import hashlib
import json

import pytest

from app import jobs


@pytest.fixture
def generation(tmp_path, monkeypatch):
    def write_json(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
    def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()
    paths = {
        "deck": "deck.md",
        "knowledge": "knowledge.md",
        "speech": "speech.md", "base_css": "base.css", "present": "present.html",
        "result": "result.json", "assets": "assets", "asset_catalog": "assets/catalog.md",
        "slides": "slides", "slide_pattern": "slides/slide_{page:02d}.html",
        "renders": "renders", "render_pattern": "renders/slide_{page:02d}.png",
        "delivery": "delivery", "delivery_manifest": "delivery/manifest.json",
        "portable_zip": "delivery/presentation.zip",
        "runtime": "_runtime", "runtime_tmp": "_runtime/tmp",
    }
    for relative in ("slides/slide_01.html", "renders/slide_01.png", "present.html"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative)
    pair = {"html_sha256": digest(tmp_path / "slides/slide_01.html"), "png_sha256": digest(tmp_path / "renders/slide_01.png")}
    page = {"page":1, "page_uid":"stable-page", "lifecycle":"ready", "inspection_id":"inspection-current", **pair}
    state = {"layout_version":2, "paths_schema":"mural.workspace-paths.v2", "paths":paths,
        "pages":{"1":{"page_uid":"stable-page", "ready_pair":pair, "last_inspection":{"inspection_id":"inspection-current", **pair}}}}
    manifest = {"schema":"mural.generation.v1", "status":"complete", "pages":[page], "outputs":{"present_html":{
        "path":"present.html", "sha256":digest(tmp_path/"present.html"), "bytes":(tmp_path/"present.html").stat().st_size}}}
    write_json(tmp_path/"_state/workspace.json", state)
    result = {"status":"completed", "generation": manifest}
    write_json(tmp_path/"result.json", result)
    monkeypatch.setattr(jobs, "_run_noninteractive", lambda *a, **kw: pytest.fail("read-only validation must not start a worker"))
    return {"root":tmp_path,"row":{"skill_version":"mural-presenter-v1","seed_json":"{}"}, "write":write_json,"state":state,"manifest":manifest,"result":result}


def test_current_generation_passes_without_writes_or_subprocess(generation):
    root = generation["root"]
    before = {p.relative_to(root):p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert jobs._ensure_static_delivery(generation["row"],root) is None
    assert not (root/"delivery").exists()
    assert before == {p.relative_to(root):p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("defect",["missing_present","present_changed","html_changed","png_changed","manifest_changed","rejected","page_uid","unready","inspection","new_page"])
def test_stale_or_rejected_generation_never_gets_rebuilt(generation,defect):
    root = generation["root"]
    if defect == "missing_present": (root/"present.html").unlink()
    elif defect in {"present_changed","html_changed","png_changed"}:
        path={"present_changed":"present.html","html_changed":"slides/slide_01.html","png_changed":"renders/slide_01.png"}[defect]
        (root/path).write_text("changed")
    elif defect == "manifest_changed":
        generation["manifest"]["status"]="failed"
        generation["write"](root/"result.json",generation["result"])
    elif defect == "rejected":
        generation["result"]["status"]="rejected"
        generation["write"](root/"result.json",generation["result"])
    else:
        page = generation["state"]["pages"]["1"]
        if defect == "page_uid": page["page_uid"]="replacement"
        if defect == "unready": page["ready_pair"]=None
        if defect == "inspection": page["last_inspection"]["inspection_id"]="new-inspection"
        if defect == "new_page": generation["state"]["pages"]["2"]={**page,"page_uid":"added"}
        generation["write"](root/"_state/workspace.json",generation["state"])
    assert "只读验收" in jobs._ensure_static_delivery(generation["row"],root)
