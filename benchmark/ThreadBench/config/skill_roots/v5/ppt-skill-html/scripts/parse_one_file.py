#!/usr/bin/env python3
"""把单个附件解析成结构化文本（供 harness 的 _stage_materials 预解析调用）。

⚠️ 必须用装了 markitdown/pdfminer.six/openpyxl 的解释器跑（见同目录 install.sh 建的 venv）：
    <normalize-venv>/bin/python  parse_one_file.py  <file>
末行恒输出一行 JSON：{status, content, content_chars?, truncated?, note?}。
异常也兜成一行 JSON，保证上游 json.loads 不会吃到 traceback。

本脚本 + parse_materials.py 是 skill 自带的解析通道（skill 自包含解析逻辑）；
第三方库不随 skill 分发，由 install.sh 装到独立 venv、宿主用 NORMALIZE_PY 注入。
"""
import sys
import json
import os

# 相对自身定位同目录的 parse_materials（skill 自包含，不再依赖 data/material-query）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def parse(path):
    import parse_materials as pm
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    pm._worker_init()
    if ext == "pdf":
        rec = pm._finish(pm._pdf(path))
        if rec.get("content_chars", 0) < 20:
            rec["status"] = "failed"
            rec["note"] = "near-empty (likely scanned PDF, needs OCR / vision)"
        return rec
    if ext in pm.MARKIT_EXT:
        return pm._finish(pm._markitdown(path))
    if ext == "doc":
        return {"status": "failed", "content": "",
                "note": "legacy .doc needs LibreOffice (unavailable)"}
    return {"status": "skipped", "content": "",
            "note": f"not a document ({ext}) — image? route to vision"}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"status": "failed", "content": "", "note": "no file arg"}))
        sys.exit(0)
    try:
        rec = parse(sys.argv[1])
    except Exception as e:
        rec = {"status": "failed", "content": "", "note": f"{type(e).__name__}: {str(e)[:150]}"}
    print(json.dumps(rec, ensure_ascii=False))
