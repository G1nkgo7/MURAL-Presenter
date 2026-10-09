"""成稿图 → 图片型 PPTX；页面尺寸来自真实 PNG。"""
import os

from .workspace_layout import WorkspaceLayout

from pptx import Presentation
from pptx.util import Inches

def renders_of(run_dir: str):
    return sorted(str(path) for path in WorkspaceLayout.load(run_dir).path("renders").glob("slide_*.png"))


def canvas_inches(run_dir: str) -> tuple[float, float]:
    geometry = WorkspaceLayout.load(run_dir).geometry()
    if geometry is None:
        raise ValueError("画布尚未生成")
    return geometry["width_px"] / 96, geometry["height_px"] / 96


def build_pptx(run_dir: str, out_path: str) -> int:
    pngs = renders_of(run_dir)
    if not pngs:
        raise ValueError("该 deck 没有成稿图")
    width_in, height_in = canvas_inches(run_dir)
    prs = Presentation()
    prs.slide_width = Inches(width_in)
    prs.slide_height = Inches(height_in)
    blank = prs.slide_layouts[6]
    from PIL import Image
    for p in pngs:
        with Image.open(p) as image:
            if image.size != (round(width_in * 96), round(height_in * 96)):
                raise ValueError(f"{p}: PNG canvas mismatch")
        slide = prs.slides.add_slide(blank)
        slide.shapes.add_picture(p, 0, 0, width=prs.slide_width, height=prs.slide_height)
    tmp = out_path + ".tmp"
    prs.save(tmp)
    os.replace(tmp, out_path)
    return len(pngs)
