from __future__ import annotations
import json
from pathlib import Path
import subprocess
from PIL import Image
from pptx import Presentation
import pytest
from app import main, pptx_export
from workspace_fixture import write_workspace_manifest

@pytest.mark.parametrize("seed", [{}, {"query": "制作 A1 横版海报"}, {"format_code": "standard_4_3"}, {"canvas_spec": {"width_px": 1920, "height_px": 1080}}])
def test_no_output_means_no_canvas_even_with_query_hints(seed):
    payload = main._canvas_spec_payload(None, seed)
    assert payload == {"status": "pending", "message": "画布尚未生成"}
    assert "width_px" not in payload

@pytest.mark.parametrize("width,height", [(1600,900), (1600,1200), (2245,3179), (3179,2245), (900,1600), (1200,1200), (1728,1000)])
def test_generated_png_drives_ui_and_pptx(tmp_path, width, height):
    write_workspace_manifest(tmp_path)
    renders = tmp_path / "renders"
    renders.mkdir()
    for page in range(1, 4):
        Image.new("RGB", (width, height), (240, 240, 240)).save(renders / f"slide_{page:02d}.png")
    payload = main._canvas_spec_payload(str(tmp_path), {"query": "错误提示 16:9"})
    assert (payload["width_px"], payload["height_px"]) == (width, height)
    assert payload["source"] == "renders/slide_01.png"
    target = tmp_path / "deck.pptx"
    assert pptx_export.build_pptx(str(tmp_path), str(target)) == 3
    deck = Presentation(target)
    assert deck.slide_width / deck.slide_height == pytest.approx(width / height)
    assert not (tmp_path / "canvas.json").exists()

def test_mixed_png_sizes_are_rejected_on_export(tmp_path):
    write_workspace_manifest(tmp_path)
    (tmp_path / "renders").mkdir()
    for page, size in enumerate([(1600, 1200), (1600, 900)], 1):
        Image.new("RGB", size).save(tmp_path / "renders" / f"slide_{page:02d}.png")
    with pytest.raises(ValueError, match="slide_02.*canvas mismatch"):
        pptx_export.build_pptx(str(tmp_path), str(tmp_path / "deck.pptx"))

def test_frontend_hides_geometry_label_without_changing_preview_dimensions():
    studio = Path(__file__).resolve().parents[1]
    js = (studio / "static/app.js").read_text()
    html = (studio / "templates/app.html").read_text()
    assert "DEFAULT_CANVAS_WIDTH" not in js and "DEFAULT_CANVAS_HEIGHT" not in js
    assert "canvas-geometry" not in js and "canvas-geometry" not in html
    source = js[js.index("function activeCanvasSize("):js.index("// 预览缩放只作用于外层 iframe")]
    result = subprocess.run(["node", "-e", """
const ed = {}, properties = {};
const $ = () => ({style: {
  setProperty: (key, value) => {properties[key] = value},
  removeProperty: key => {delete properties[key]},
}});
""" + source + """
const outputs = [[1600,900], [1600,1200], [2245,3179], [1200,1200], null].map(size => {
  setCanvasSpec(size ? {width_px:size[0],height_px:size[1]} : null);
  return {size:activeCanvasSize(),properties:{...properties}};
});
console.log(JSON.stringify(outputs));
"""], check=True, capture_output=True, text=True)
    states = json.loads(result.stdout)
    for state, (width, height) in zip(states, [(1600,900), (1600,1200), (2245,3179), (1200,1200)]):
        assert state["size"] == {"width": width, "height": height, "ratio": width / height}
        assert state["properties"]["--deck-aspect-ratio"] == f"{width} / {height}"
    assert states[-1] == {"size": None, "properties": {}}
