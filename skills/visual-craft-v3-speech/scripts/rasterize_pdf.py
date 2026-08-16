#!/usr/bin/env python3
"""rasterize_pdf.py —— 图片式/扫描 PDF(抽不出文本层)→ 前 N 页渲成 PNG,交给 material 子代理 vision_analyze。

否则这类材料内容彻底丢失。用法:
    <pymupdf-python> rasterize_pdf.py <pdf> <out_dir>

⚠️ 依赖 PyMuPDF(fitz)——**不在解析 venv 里**,是第三套解释器(见同目录 install.sh 的 pymupdf 分装)。
上限/DPI 走环境变量,免长文档炸 vision 图上限:
    MATERIAL_RASTER_MAX_PAGES (默认 12) · MATERIAL_RASTER_DPI (150) · MATERIAL_RASTER_MAXPX (2600,单页最长边像素上限)

末行恒输出一行 JSON:{pages:[相对/绝对路径...], total:N, note?}。异常也兜成一行 JSON(pages 为空),
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


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(json.dumps({"pages": [], "total": 0, "note": "usage: rasterize_pdf.py <pdf> <out_dir>"}))
        sys.exit(0)
    try:
        rec = rasterize(sys.argv[1], sys.argv[2])
    except Exception as e:
        rec = {"pages": [], "total": 0, "note": f"{type(e).__name__}: {str(e)[:150]}"}
    print(json.dumps(rec, ensure_ascii=False))
