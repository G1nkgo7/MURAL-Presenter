#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""附件解析通道（供 ppt-skill-html 的附件预解析用；裁剪版，无磁盘副作用）。

⚠️ 运行环境要求：markitdown / pdfminer.six / openpyxl 必须可 import。
   这些库不随 skill 分发，由 `scripts/install.sh` 安装到一个独立 venv，
   宿主通过 NORMALIZE_PY 指向那个 venv 的 python 来跑本脚本（见 install.sh / SKILL.md）。

通道（channel）：
  docx/pptx/xlsx/xls/md/txt/csv/ppt -> MarkItDown
  pdf                               -> pdfminer.six（direct；markitdown 的 pdf wrapper 不稳）
  doc（legacy binary）              -> 需 LibreOffice（通常不可用）-> failed
  图片 / 抽不出文本的扫描 PDF        -> 交给 material 子代理的 vision_analyze 兜底（见 material.md）

本文件只导出「通道函数 + 常量」，不建缓存目录、不读全局 jsonl（那些属于批量管线，不在 skill 内）。
"""
import signal
import logging
import os

MAX_CHARS = int(os.environ.get('MATERIALS_MAX_CHARS', '12000'))          # ~7.2k tokens；单文档内容上限
PDF_TIMEOUT = 90           # 每个 pdf 秒数上限
MARKIT_EXT = {'docx', 'pptx', 'xlsx', 'xls', 'md', 'txt', 'csv', 'ppt'}

_MD = None  # per-process MarkItDown 单例


def _worker_init():
    """装配 MarkItDown 单例 + 压掉 pdfminer 的逐字形警告（否则刷屏 stderr）。"""
    global _MD
    for name in ('pdfminer', 'pdfminer.pdffont', 'pdfminer.pdfinterp', 'pdfminer.layout'):
        logging.getLogger(name).setLevel(logging.ERROR)
    from markitdown import MarkItDown
    _MD = MarkItDown()


def _markitdown(path):
    global _MD
    if _MD is None:
        _worker_init()
    # docx 优先 MarkItDown;失败/空(常见坏 docx:内部 word/../NULL 坏关系引用)→ 降级 python-docx / 直解 document.xml
    ext = os.path.splitext(path)[1].lstrip('.').lower()
    if ext == 'docx':
        try:
            txt = _MD.convert(path).text_content or ''
            if txt.strip():
                return txt
        except Exception:
            pass
        return _docx_fallback(path)   # MarkItDown 失败/空 → 兜底
    if ext == 'xlsx':
        try:
            txt = _MD.convert(path).text_content or ''
            if txt.strip():
                return txt
        except Exception:
            pass
        return _xlsx_fallback(path)   # MarkItDown FileConversion/超时 → openpyxl 直读兜底
    return _MD.convert(path).text_content or ''


def _xlsx_fallback(path):
    """坏/超时 xlsx(MarkItDown FileConversionException / TimeoutExpired)的兜底:openpyxl 只读模式直读,
    逐 sheet 抽单元格文本(read_only + values_only,大表也快;跳过空行)。"""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        out = []
        for ws in wb.worksheets:
            out.append(f'## {ws.title}')
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None and str(c).strip()]
                if cells:
                    out.append(' | '.join(cells))
        wb.close()
        return '\n'.join(out).strip()
    except Exception:
        return ''


def _docx_fallback(path):
    """坏 docx(MarkItDown/pdfminer 都因坏关系引用失败)的兜底:先试 python-docx,
    再退到 unzip + 直解 word/document.xml 抽全部段落文本(纯正文,忽略图片/表格截图)。
    这是把 material 子代理踩坑后手搓的兜底固化进脚本,免每次重新发明。"""
    # ① python-docx(能读大多数 docx,即使 MarkItDown 的 relationship 解析挂了)
    try:
        from docx import Document
        doc = Document(path)
        paras = [p.text for p in doc.paragraphs]
        txt = '\n'.join(paras).strip()
        if txt:
            return txt
    except Exception:
        pass
    # ② 最后兜底:unzip 取 word/document.xml,ET 直解所有 <w:t> 文本(不依赖 relationship)
    try:
        import zipfile
        import xml.etree.ElementTree as ET
        W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
        with zipfile.ZipFile(path) as z:
            xml = z.read('word/document.xml')
        root = ET.fromstring(xml)
        lines = []
        for p in root.iter(W + 'p'):
            parts = [t.text for t in p.iter(W + 't') if t.text]
            lines.append(''.join(parts))
        return '\n'.join(lines).strip()
    except Exception:
        return ''


def _pdf(path):
    from pdfminer.high_level import extract_text

    def _to(*a):
        raise TimeoutError()
    signal.signal(signal.SIGALRM, _to)
    signal.alarm(PDF_TIMEOUT)
    try:
        return extract_text(path) or ''
    finally:
        signal.alarm(0)


def _finish(txt):
    txt = (txt or '').strip()
    n = len(txt)
    if n == 0:
        return {'status': 'failed', 'content_chars': 0, 'truncated': False,
                'content': '', 'note': 'empty parse'}
    trunc = n > MAX_CHARS
    return {'status': 'truncated' if trunc else 'ok', 'content_chars': n,
            'truncated': trunc, 'content': txt[:MAX_CHARS]}
