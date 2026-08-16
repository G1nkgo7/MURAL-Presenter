#!/usr/bin/env python3
"""stage_materials.py —— skill 自带的「附件编排入口」:遍历全部附件 → 拷进 _raw/ →
分类型解析(文本/图片/扫描PDF光栅化)→ 写 catalog.json。material 子代理一条命令跑完解析。

用法:
    python stage_materials.py <materials_dir> [attachments.json]

  <materials_dir>   目标目录(通常就是工作区的 `materials/`);附件原件已由 harness 拷进
                    `<materials_dir>/_raw/`,或由本脚本按 attachments.json 拷入。
  attachments.json  附件清单(默认读 `<materials_dir>/attachments.json`);每项:
                    {"path": <源路径>, "name": <可选文件名>}  或  直接字符串路径。
                    若不存在,则改为扫描 `<materials_dir>/_raw/` 下已有文件当附件。

跨解释器(本脚本用哪个 python 跑都行,它自己 subprocess 拉起下面两套):
  - 文本解析:`$NORMALIZE_PY parse_one_file.py <file>`(解析 venv:markitdown/pdfminer/openpyxl)
  - 扫描PDF光栅化:`$RASTERIZE_PY rasterize_pdf.py <pdf> <out_dir>`(PyMuPDF venv;默认回退到本解释器)

产出:`<materials_dir>/catalog.json` + 各 `<name>.md` + `_raw/<name>_pages/pNNN.png`。
catalog schema 与旧 harness _stage_materials 一致(material.md 依赖这些字段):
    name / raw / ext / kind(doc|image) / text / chars / status / rasterized_pages / note
    扫描PDF 的每页另出一条 image 条目:{name:"<pdf> · pN", raw, ext:png, kind:image, from_scanned_pdf}
末行输出一行 JSON 汇总:{status, catalog, entries, usable, failed};没有附件或 usable=0 时非 0 退出。
"""
import sys
import os
import json
import shutil
import subprocess

_HERE = os.path.dirname(os.path.abspath(__file__))
_IMG_EXT = {"jpg", "jpeg", "png", "webp", "gif", "bmp"}

# 解析用解释器(第三方库不在 skill 树,靠外部 venv;见 install.sh / SKILL 环境依赖段)
NORMALIZE_PY = os.environ.get("NORMALIZE_PY", sys.executable)
# 光栅化用解释器(需 PyMuPDF;默认回退当前解释器,装了 pymupdf 就能用)
RASTERIZE_PY = os.environ.get("RASTERIZE_PY", os.environ.get("PYMUPDF_PY", sys.executable))
PARSE_ONE = os.path.join(_HERE, "parse_one_file.py")
RASTERIZE = os.path.join(_HERE, "rasterize_pdf.py")


def _load_attachments(mdir):
    """确定附件清单:优先 attachments.json;否则扫 _raw/ 下已有文件。"""
    aj = os.path.join(mdir, "attachments.json")
    if os.path.exists(aj):
        try:
            data = json.load(open(aj, encoding="utf-8"))
            if isinstance(data, dict):
                data = data.get("attachments") or data.get("files") or []
            return data or []
        except Exception:
            pass
    raw = os.path.join(mdir, "_raw")
    if os.path.isdir(raw):
        return [{"path": os.path.join(raw, n), "name": n}
                for n in sorted(os.listdir(raw))
                if os.path.isfile(os.path.join(raw, n)) and not n.endswith("_pages")]
    return []


def _catalog_root(mdir):
    """Return a workspace-relative root when possible, otherwise an absolute path."""
    absolute = os.path.abspath(mdir)
    relative = os.path.relpath(absolute, os.getcwd())
    if relative == ".":
        return "."
    if relative == ".." or relative.startswith(".." + os.sep):
        return absolute.replace(os.sep, "/")
    return relative.replace(os.sep, "/")


def stage(mdir, attachments):
    raw = os.path.join(mdir, "_raw")
    catalog_root = _catalog_root(mdir).rstrip("/")
    os.makedirs(raw, exist_ok=True)
    catalog = []
    for a in attachments:
        # attachments.json 成功项的路径字段可能叫 raw(harness 当前写法)或 path(旧写法)——都接,
        # 别只认 path(否则 raw 项 → src=None → 误判 missing、白丢已拷进 _raw 的原件)。
        src = (a.get("raw") or a.get("path")) if isinstance(a, dict) else a
        name = (a.get("name") if isinstance(a, dict) else None) or (os.path.basename(src) if src else "unknown")
        if not src or not os.path.exists(src):
            catalog.append({"name": name, "status": "missing"})
            continue
        dst = os.path.join(raw, name)
        try:
            if os.path.abspath(src) != os.path.abspath(dst):
                shutil.copy2(src, dst)
        except Exception as e:
            catalog.append({"name": name, "status": "failed", "note": f"copy: {e}"})
            continue
        ext = os.path.splitext(name)[1].lstrip(".").lower()
        entry = {"name": name, "raw": f"{catalog_root}/_raw/{name}", "ext": ext}
        raster_pages = []
        if ext in _IMG_EXT:
            entry["kind"] = "image"                 # 图片不预解析,交 material 子代理 vision_analyze
        else:
            try:
                out = subprocess.run([NORMALIZE_PY, PARSE_ONE, dst],
                                     capture_output=True, text=True, timeout=180)
                rec = json.loads(out.stdout.strip().splitlines()[-1])
                if rec.get("content"):
                    with open(os.path.join(mdir, name + ".md"), "w", encoding="utf-8") as f:
                        f.write(rec["content"])
                    entry.update(kind="doc", text=f"{catalog_root}/{name}.md",
                                 chars=rec.get("content_chars"), status=rec.get("status"))
                    # 混合 PDF:pdfminer 只抽文本层、看不见嵌入图/图表 → 图内容会整块丢失。
                    # 额外跑一遍 PyMuPDF 逐页图检测,把「含显著内嵌位图的页」渲成图补给 vision(别的页不渲,免和文本冗余)。
                    if ext == "pdf":
                        _ipgs, _ipnos = _rasterize_images(dst, os.path.join(raw, name + "_pages"))
                        if _ipgs:
                            entry["embedded_image_pages"] = len(_ipgs)
                            entry["note"] = ("文本已抽入 md;该 PDF 另有 " + str(len(_ipgs))
                                             + " 页含内嵌图/图表(pdfminer 抽不到图),已 rasterize,"
                                             "内容见下方同名 image 条目,请 vision_analyze 逐页读回图表。")
                            raster_pages = [{"name": f"{name} · p{pno}(内嵌图)",
                                             "raw": f"{catalog_root}/_raw/{name}_pages/{os.path.basename(pg)}",
                                             "ext": "png", "kind": "image", "from_text_pdf_images": name}
                                            for pg, pno in zip(_ipgs, _ipnos)]
                else:
                    entry.update(kind="doc", status=rec.get("status", "failed"), note=rec.get("note"))
                    # 图片式/扫描 PDF 抽不出文本 → 光栅化页图,当图片交 vision_analyze(否则内容彻底丢失)
                    if ext == "pdf":
                        _pgs, _total = _rasterize(dst, os.path.join(raw, name + "_pages"))
                        if _pgs:
                            entry["rasterized_pages"] = len(_pgs)
                            entry["note"] = ("图片式/扫描 PDF、无文本层;已 rasterize 前 "
                                             + str(len(_pgs)) + (f"/{_total}" if _total > len(_pgs) else "")
                                             + " 页成图,内容见下方同名 image 条目,请用 vision_analyze 逐页读。")
                            raster_pages = [{"name": f"{name} · p{i}",
                                             "raw": f"{catalog_root}/_raw/{name}_pages/{os.path.basename(pg)}",
                                             "ext": "png", "kind": "image", "from_scanned_pdf": name}
                                            for i, pg in enumerate(_pgs, 1)]
            except Exception as e:
                entry.update(kind="doc", status="failed", note=f"{type(e).__name__}: {e}")
        catalog.append(entry)
        catalog.extend(raster_pages)
    with open(os.path.join(mdir, "catalog.json"), "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2)
    return catalog


def _rasterize(pdf_path, out_dir):
    """subprocess 调 rasterize_pdf.py(PyMuPDF venv);返回 (页图路径列表, 总页数)。"""
    try:
        out = subprocess.run([RASTERIZE_PY, RASTERIZE, pdf_path, out_dir],
                             capture_output=True, text=True, timeout=300)
        rec = json.loads(out.stdout.strip().splitlines()[-1])
        return rec.get("pages") or [], rec.get("total") or 0
    except Exception:
        return [], 0


def _rasterize_images(pdf_path, out_dir):
    """混合 PDF 补图:文本已抽走进 md,这里只渲「含显著内嵌位图的页」补给 vision(--images-only)。
    返回 (页图路径列表, [1-based 图页码...])。PyMuPDF 不可用/无图页 → 返回空,静默不影响文本。"""
    try:
        out = subprocess.run([RASTERIZE_PY, RASTERIZE, pdf_path, out_dir, "--images-only"],
                             capture_output=True, text=True, timeout=300)
        rec = json.loads(out.stdout.strip().splitlines()[-1])
        return rec.get("pages") or [], rec.get("image_pages") or []
    except Exception:
        return [], []


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: stage_materials.py <materials_dir> [attachments.json]"}))
        sys.exit(2)
    mdir = sys.argv[1]
    os.makedirs(mdir, exist_ok=True)
    atts = _load_attachments(mdir)
    if len(sys.argv) > 2 and os.path.exists(sys.argv[2]):
        try:
            data = json.load(open(sys.argv[2], encoding="utf-8"))
            atts = (data.get("attachments") if isinstance(data, dict) else data) or atts
        except Exception:
            pass
    if not atts:
        print(json.dumps({
            "status": "failed",
            "catalog": os.path.join(mdir, "catalog.json"),
            "entries": 0,
            "usable": 0,
            "failed": 0,
            "error": "no attachments found in attachments.json or materials/_raw",
        }, ensure_ascii=False))
        sys.exit(1)
    cat = stage(mdir, atts)
    n_ok = sum(1 for e in cat if e.get("status") in ("ok", "truncated") or e.get("kind") == "image")
    n_failed = sum(1 for e in cat if e.get("status") in ("failed", "missing"))
    status = "failed" if n_ok == 0 else ("partial" if n_failed else "ok")
    print(json.dumps({"status": status,
                      "catalog": os.path.join(mdir, "catalog.json"),
                      "entries": len(cat), "usable": n_ok, "failed": n_failed}, ensure_ascii=False))
    sys.exit(1 if n_ok == 0 else 0)
