#!/usr/bin/env python3
"""rasterize_pdf.py —— PDF → PNG 页图,交给 material 子代理 vision_analyze。两种模式:

  1) 全渲(默认):图片式/扫描 PDF(抽不出文本层)→ 前 N 页渲成 PNG,否则内容彻底丢失。
        <pymupdf-python> rasterize_pdf.py <pdf> <out_dir>
  2) 只渲含图页(--images-only):**有文字层但内嵌图/图表**的混合 PDF——文本已由 pdfminer 抽走进 md,
     但嵌入的图表 pdfminer 看不见、会整块丢失。本模式只把「含显著位图的页」渲成 PNG 补给 vision,别的页跳过。
        <pymupdf-python> rasterize_pdf.py <pdf> <out_dir> --images-only

⚠️ 依赖 PyMuPDF(fitz)——**不在解析 venv 里**,是第三套解释器(见同目录 install.sh 的 pymupdf 分装)。
上限/DPI 走环境变量,免长文档炸 vision 图上限:
    MATERIAL_RASTER_MAX_PAGES (默认 12) · MATERIAL_RASTER_DPI (150) · MATERIAL_RASTER_MAXPX (2600,单页最长边像素上限)
  --images-only 专属上限:MATERIAL_IMG_PAGE_FRAC(默认 0.10,一页位图面积/页面积 ≥ 此值才算含图页)·
    MATERIAL_IMG_MAX_PAGES(默认 8,补图页数上限,免混合长文档炸 vision)

末行恒输出一行 JSON:{pages:[相对/绝对路径...], total:N, image_pages?:[页码...], note?}。异常也兜成一行 JSON(pages 为空),
保证上游(stage_materials.py / material 子代理)json.loads 不吃 traceback。
"""
import sys
import os
import json


def rasterize(pdf_path, out_dir):
    max_pages = int(os.environ.get("MATERIAL_RASTER_MAX_PAGES", "12"))
    dpi = int(os.environ.get("MATERIAL_RASTER_DPI", "150"))
    maxpx = int(os.environ.get("MATERIAL_RASTER_MAXPX", "2600"))
    try:
        import fitz  # PyMuPDF,自包含不需系统 poppler
    except Exception as e:
        return {"pages": [], "total": 0, "note": f"PyMuPDF 不可用: {type(e).__name__}: {e}"}
    pages, total = [], 0
    try:
        doc = fitz.open(pdf_path)
        total = doc.page_count
        os.makedirs(out_dir, exist_ok=True)
        for i in range(min(total, max_pages)):
            page = doc[i]
            zoom = dpi / 72.0                                   # 72pt = 1inch
            longest = max(page.rect.width, page.rect.height) or 1
            if longest * zoom > maxpx:                          # 超大页(海报/图纸)按上限缩放,避免 GB 级 pixmap
                zoom = maxpx / longest
            p = os.path.join(out_dir, f"p{i + 1:03d}.png")
            page.get_pixmap(matrix=fitz.Matrix(zoom, zoom)).save(p)
            pages.append(p)
        doc.close()
    except Exception as e:
        return {"pages": pages, "total": total, "note": f"{type(e).__name__}: {str(e)[:150]}"}
    return {"pages": pages, "total": total}


def rasterize_image_pages(pdf_path, out_dir):
    """混合 PDF 补图:文本层已由 pdfminer 抽走,这里只把「含显著内嵌位图的页」渲成 PNG,补给 vision 读图表。
    判定:一页里所有嵌入位图的 bbox 面积之和 / 页面积 ≥ MATERIAL_IMG_PAGE_FRAC(默认 0.10)→ 该页含图。
    只渲被判定的页(别的页跳过,免和文本 md 大面积冗余、免炸 vision),上限 MATERIAL_IMG_MAX_PAGES(默认 8)。
    返回 {pages:[...], total:N, image_pages:[1-based 页码...], note?}(pages 与 image_pages 一一对应)。"""
    frac_thr = float(os.environ.get("MATERIAL_IMG_PAGE_FRAC", "0.10"))
    max_pages = int(os.environ.get("MATERIAL_IMG_MAX_PAGES", "8"))
    dpi = int(os.environ.get("MATERIAL_RASTER_DPI", "150"))
    maxpx = int(os.environ.get("MATERIAL_RASTER_MAXPX", "2600"))
    try:
        import fitz  # PyMuPDF
    except Exception as e:
        return {"pages": [], "total": 0, "image_pages": [], "note": f"PyMuPDF 不可用: {type(e).__name__}: {e}"}
    pages, image_pages, total = [], [], 0
    try:
        doc = fitz.open(pdf_path)
        total = doc.page_count
        flagged = []
        for i in range(total):
            page = doc[i]
            parea = abs(page.rect.width * page.rect.height) or 1.0
            imgarea, seen = 0.0, set()
            for img in page.get_images(full=True):
                xref = img[0]
                if xref in seen:                     # 同一图在页上多次引用只算一次面积
                    continue
                seen.add(xref)
                try:                                 # 位图在页面上的实际渲染 bbox(可能被缩放/多处放置)
                    for r in page.get_image_rects(xref):
                        imgarea += abs(r.width * r.height)
                except Exception:
                    pass
            if imgarea / parea >= frac_thr:
                flagged.append(i)
            if len(flagged) >= max_pages:
                break
        if flagged:
            os.makedirs(out_dir, exist_ok=True)
        for i in flagged:
            page = doc[i]
            zoom = dpi / 72.0
            longest = max(page.rect.width, page.rect.height) or 1
            if longest * zoom > maxpx:
                zoom = maxpx / longest
            p = os.path.join(out_dir, f"p{i + 1:03d}.png")
            page.get_pixmap(matrix=fitz.Matrix(zoom, zoom)).save(p)
            pages.append(p)
            image_pages.append(i + 1)
        doc.close()
    except Exception as e:
        return {"pages": pages, "total": total, "image_pages": image_pages,
                "note": f"{type(e).__name__}: {str(e)[:150]}"}
    return {"pages": pages, "total": total, "image_pages": image_pages}


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(json.dumps({"pages": [], "total": 0, "note": "usage: rasterize_pdf.py <pdf> <out_dir> [--images-only]"}))
        sys.exit(0)
    _images_only = "--images-only" in sys.argv[3:]
    try:
        rec = (rasterize_image_pages if _images_only else rasterize)(sys.argv[1], sys.argv[2])
    except Exception as e:
        rec = {"pages": [], "total": 0, "note": f"{type(e).__name__}: {str(e)[:150]}"}
    print(json.dumps(rec, ensure_ascii=False))
