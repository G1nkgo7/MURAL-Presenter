#!/usr/bin/env python3
"""Clean harness batch driver: one workspace and one parallel role team per query.

    runs/<batch>/<sid>/      # deck workspace and trace
    logs/<batch>.manifest.jsonl

输入 JSONL 每行 {qid, query, lang, ...}；也可 --query 临时跑一条。
该入口负责读取推理 query、构造运行配置，并为 worker 注入渲染环境。
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import hashlib
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from core import config as acfg  # noqa: E402
from core.language import infer_deck_language, normalize_language  # noqa: E402
from core.run_profiles import resolve_run_profile  # noqa: E402

RUNS = str(acfg.RUNS_DIR)
LOGS = str(acfg.LOGS_DIR)
TERMINAL = {"completed"}


def cgroup_cpus():
    try:
        with open("/sys/fs/cgroup/cpu.max") as f:
            q, p = f.read().split()
        if q != "max":
            return max(1, int(int(q) / int(p)))
    except Exception:
        pass
    try:
        q = int(open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read())
        p = int(open("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read())
        if q > 0:
            return max(1, int(q / p))
    except Exception:
        pass
    return os.cpu_count() or 1


def load_dotenv():
    """加载项目 .env，让它**覆盖**已有环境变量（含 ~/.bashrc 的全局 export 与命令行前缀）。

    .env 是本项目专属配置，语义上应压过全局默认——尤其 ANTHROPIC_MODEL：
    ~/.bashrc 里 export 的全局模型（供别处用）不应污染本管线的生成模型。
    故用直接赋值而非 setdefault（旧实现只在变量不存在时才写，导致 .bashrc 永远赢、.env 形同虚设）。
    优先级：.env > 命令行前缀 env > ~/.bashrc。要临时换模型，改 .env 一处即可。
    """
    path = os.environ.get("CLEAN_DOTENV_PATH") or os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip().strip('"').strip("'")


def load_queries(path):
    """读 jsonl，每行一个 {qid?, query, ...}。容错跳空行、坏行报行号。"""
    out = []
    with open(path, encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise SystemExit(f"query 文件第 {ln} 行不是合法 JSON: {e}")
            if not isinstance(obj, dict) or not obj.get("query"):
                raise SystemExit(f"query 文件第 {ln} 行缺少 query 字段: {line[:120]}")
            out.append(obj)
    return out


def make_sample_id(batch, q, seen):
    """优先用 query 自带 qid；否则对 query 内容做稳定哈希（与文件位置无关）。"""
    qid = q.get("qid")
    base = f"{batch}_{qid}" if qid else f"{batch}_" + hashlib.sha1(
        json.dumps(q, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
    seen[base] = seen.get(base, 0) + 1
    return base if seen[base] == 1 else f"{base}_{seen[base]}"


def load_manifest(mpath):
    done, attempts = {}, {}
    if os.path.exists(mpath):
        with open(mpath, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = r.get("sample_id")
                if not sid:
                    continue
                attempts[sid] = attempts.get(sid, 0) + 1
                done[sid] = r
    for sid, r in done.items():
        r["_attempts"] = attempts[sid]
    return done


def append_manifest(mpath, rec):
    with open(mpath, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()


def run_dir_complete(run_dir):
    """run_dir 是否已是一份完整产出（present.html + 自然收尾的轨迹）。
    用于 resume：完整但未入 manifest 的 deck（worker 收尾被中断）别 rmtree 重跑——
    保住它交给 rescue.py 救回，避免一份贵 deck 付两遍钱。"""
    presentation = os.path.join(run_dir, "present.html")
    if not (
        os.path.exists(presentation)
        and os.path.getsize(presentation) >= 500
    ):
        return False
    mp = os.path.join(run_dir, "_trace", "orchestrator", "messages.json")
    try:
        with open(mp, encoding="utf-8") as f:
            msgs = json.load(f)
    except Exception:
        return False
    if not msgs or msgs[-1].get("role") != "assistant":
        return False
    last = msgs[-1].get("content")
    return isinstance(last, list) and any(
        isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip() for b in last)


def build_config(args):
    run_profile = resolve_run_profile(getattr(args, "mode", None))
    explicit_nova_root = os.environ.get("CLEAN_NOVA_RAW_ROOT", "").strip()
    nova_root = (
        Path(explicit_nova_root)
        if explicit_nova_root
        else acfg.NOVA_RAW_ROOT / args.batch
    )
    return {
        "batch": args.batch,
        "run_mode": run_profile.name,
        "batch_workers": int(getattr(args, "workers", 4)),
        "max_attempts": int(getattr(args, "max_attempts", 3)),
        # model 取值链：MODEL > ANTHROPIC_MODEL(此时已被 .env 覆盖) > acfg 默认。
        # 不能只 fallback 到 acfg.ANTHROPIC_MODEL——它在 import 时就被 ~/.bashrc 定死了，
        # load_dotenv() 是 import 之后才跑的，改不动那个已求值的模块变量；必须现读 os.environ。
        "model": os.environ.get("MODEL") or os.environ.get("ANTHROPIC_MODEL") or acfg.ANTHROPIC_MODEL,
        "model_base_url": acfg.ANTHROPIC_BASE_URL,
        "openai_base_url": os.environ.get("IMAGE_BASE_URL", acfg.IMAGE_BASE_URL),
        "image_model": os.environ.get("IMAGE_MODEL", acfg.IMAGE_MODEL),
        "enable_image_gen": acfg.ENABLE_IMAGE_GEN and bool(
            os.environ.get("IMAGE_API_KEY") or os.environ.get("OPENAI_API_KEY")
        ),
        "max_turns": int(os.environ.get("CLEAN_MAX_TURNS", str(acfg.MAX_TURNS))),
        "child_max_turns": int(os.environ.get("CLEAN_CHILD_MAX_TURNS", str(acfg.CHILD_MAX_TURNS))),
        "max_tokens": int(os.environ.get("CLEAN_MAX_TOKENS", str(acfg.PER_TURN_MAX_TOKENS))),
        "first_response_timeout_s": int(
            os.environ.get(
                "CLEAN_FIRST_RESPONSE_TIMEOUT",
                str(acfg.FIRST_RESPONSE_TIMEOUT_S),
            )
        ),
        "active_response_timeout_s": int(
            os.environ.get(
                "CLEAN_ACTIVE_RESPONSE_TIMEOUT",
                str(acfg.ACTIVE_RESPONSE_TIMEOUT_S),
            )
        ),
        "deck_timeout_s": int(
            os.environ.get("CLEAN_DECK_TIMEOUT", str(acfg.DECK_TIMEOUT_S))
        ),
        "child_wall_timeout_s": int(
            os.environ.get(
                "CLEAN_CHILD_WALL_TIMEOUT",
                str(acfg.CHILD_WALL_TIMEOUT_S),
            )
        ),
        "model_timeout_s": acfg.MODEL_TIMEOUT_S,
        "bash_timeout_s": acfg.BASH_TIMEOUT_S,
        "child_concurrency": acfg.CHILD_CONCURRENCY,
        "child_pool_max_workers": acfg.CHILD_POOL_MAX_WORKERS,
        "child_concurrency_file": acfg.CHILD_CONCURRENCY_FILE,
        "remote_tool_concurrency": acfg.REMOTE_TOOL_CONCURRENCY,
        "max_heals": acfg.MAX_HEALS,
        "max_vision_edge": acfg.MAX_VISION_EDGE,
        "deck_width": acfg.DECK_W,
        "deck_height": acfg.DECK_H,
        "thinking": os.environ.get("CLEAN_THINKING", "1" if acfg.THINKING else "0") == "1",
        "effort": (
            os.environ.get("CLEAN_EFFORT")
            or os.environ.get("THINK_EFFORT")
            or acfg.THINK_EFFORT
        ),
        "nova_raw_v2": acfg.NOVA_RAW_V2,
        "nova_run_id": os.environ.get("NOVA_RUN_ID", args.batch).strip() or args.batch,
        "nova_raw_root": str(nova_root),
    }


def select_skill(seed: dict) -> tuple[str, str]:
    """Expose the one bilingual v0.3 Skill; output language stays query-driven."""
    return acfg.SKILL_NAME, "zh"


def read_concurrency_target(path: str | None, default: int, pool_max: int) -> int:
    """Read the live deck target without interrupting already-running decks."""
    if not path:
        return max(1, min(pool_max, default))
    try:
        value = int(Path(path).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        value = default
    return max(0, min(pool_max, value))


def ensure_control_file(path: Path, initial: int) -> bool:
    """Create one control file without overwriting an operator's existing value."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(f"{int(initial)}\n")
        handle.flush()
        os.fsync(handle.fileno())
    return True


def _extract_package_media(
    source: Path,
    output_dir: Path,
    prefixes: tuple[str, ...],
) -> list[Path]:
    """Expose embedded Office media as deterministic Material-only derivatives."""
    output_dir.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    with zipfile.ZipFile(source) as archive:
        names = sorted(
            name for name in archive.namelist()
            if any(name.startswith(prefix) for prefix in prefixes)
            and not name.endswith("/")
        )
        for index, name in enumerate(names, 1):
            suffix = Path(name).suffix.lower() or ".bin"
            destination = output_dir / f"embedded_{index:03d}{suffix}"
            destination.write_bytes(archive.read(name))
            extracted.append(destination)
    return extracted


def _render_office_pages(source: Path, output_dir: Path) -> list[Path]:
    """Best-effort LibreOffice rendering; embedded media remains the fallback."""
    executable = shutil.which("libreoffice") or shutil.which("soffice")
    if not executable:
        return []
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mural-office-") as temporary:
        converted = subprocess.run(
            [
                executable,
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                temporary,
                str(source),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        pdf = Path(temporary) / f"{source.stem}.pdf"
        if converted.returncode != 0 or not pdf.is_file():
            return []
        import pymupdf as fitz

        document = fitz.open(pdf)
        rendered: list[Path] = []
        try:
            for index, page in enumerate(document, 1):
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
                destination = output_dir / f"page_{index:03d}.png"
                pixmap.save(destination)
                rendered.append(destination)
        finally:
            document.close()
        return rendered


def _relative_derivative(path: Path, source: Path) -> str:
    return str(path.relative_to(source.parent.parent)).replace(os.sep, "/")


def _append_visual_derivatives(
    blocks: list[str],
    source: Path,
    media: list[Path],
    pages: list[Path],
) -> None:
    blocks.extend(["", "## Visual derivatives", ""])
    if pages:
        blocks.append(
            "> Complete Office page previews are available for Material visual inspection."
        )
        blocks.extend(f"- page_preview: {_relative_derivative(path, source)}" for path in pages)
    else:
        blocks.append(
            "> Complete page rendering unavailable in this runtime; embedded media and structured text follow."
        )
    image_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
    for path in media:
        blocks.append(f"- embedded_media: {_relative_derivative(path, source)}")
        if path.suffix.lower() not in image_suffixes:
            continue
        ocr_text, _, ocr_note = _rapidocr_page(path)
        if ocr_text:
            blocks.extend([
                "  embedded_media_ocr: |",
                *[f"    {line}" for line in ocr_text.splitlines()],
            ])
        else:
            blocks.append(f"  embedded_media_ocr: unavailable ({ocr_note or 'no text'})")
    if not media:
        blocks.append("- embedded_media: none")


def _append_chart_xml(blocks: list[str], source: Path, prefix: str) -> None:
    """Preserve cached chart labels/values even when Office rendering is unavailable."""
    from xml.etree import ElementTree as ET

    with zipfile.ZipFile(source) as archive:
        chart_names = sorted(
            name for name in archive.namelist()
            if name.startswith(prefix) and name.endswith(".xml")
        )
        for index, name in enumerate(chart_names, 1):
            root = ET.fromstring(archive.read(name))
            labels = [
                (node.text or "").strip()
                for node in root.iter()
                if node.tag.rsplit("}", 1)[-1] in {"v", "f"}
                and (node.text or "").strip()
            ]
            blocks.extend(["", f"### Chart {index}", ""])
            blocks.append(" | ".join(labels) if labels else "(chart XML has no cached labels or values)")


def _extract_material(source: Path, target: Path) -> bool:
    """Create a readable Markdown companion for common office files.

    This is deterministic ingestion in the Harness, not a model role. Material
    still decides what matters and writes the evidence note.
    """
    suffix = source.suffix.lower()
    blocks: list[str] = [f"# Extracted material: {source.name}", ""]
    try:
        if suffix == ".xlsx":
            try:
                from openpyxl import load_workbook

                book = load_workbook(source, read_only=False, data_only=False)
                for sheet in book.worksheets:
                    title = sheet.title
                    blocks.extend([f"## Sheet: {title}", ""])
                    if sheet.merged_cells.ranges:
                        blocks.append(
                            "> merged_ranges: "
                            + ", ".join(str(item) for item in sheet.merged_cells.ranges)
                        )
                    for row in sheet.iter_rows():
                        values = [
                            "" if cell.value is None else str(cell.value).replace("\n", " ")
                            for cell in row
                        ]
                        if any(values):
                            blocks.append(" | ".join(values))
                    if getattr(sheet, "_charts", None):
                        blocks.append(f"> charts_on_sheet: {len(sheet._charts)}")
                    if getattr(sheet, "_images", None):
                        blocks.append(f"> embedded_images_on_sheet: {len(sheet._images)}")
                    blocks.append("")
                book.close()
            except ImportError:
                # The production model environment is intentionally small. XLSX is
                # a ZIP of XML files, so retain a dependency-free readable fallback.
                import zipfile
                from xml.etree import ElementTree as ET

                ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
                with zipfile.ZipFile(source) as archive:
                    shared: list[str] = []
                    if "xl/sharedStrings.xml" in archive.namelist():
                        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                        shared = [
                            "".join(node.text or "" for node in item.findall(".//m:t", ns))
                            for item in root.findall("m:si", ns)
                        ]
                    sheet_names = sorted(
                        name for name in archive.namelist()
                        if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
                    )
                    for index, name in enumerate(sheet_names, 1):
                        blocks.extend([f"## Sheet {index}", ""])
                        root = ET.fromstring(archive.read(name))
                        for row in root.findall(".//m:row", ns):
                            values: list[str] = []
                            for cell in row.findall("m:c", ns):
                                value = cell.find("m:v", ns)
                                inline = cell.find("m:is", ns)
                                formula = cell.find("m:f", ns)
                                text = "" if value is None else (value.text or "")
                                if inline is not None:
                                    text = "".join(
                                        node.text or "" for node in inline.findall(".//m:t", ns)
                                    )
                                if cell.get("t") == "s" and text.isdigit():
                                    text = shared[int(text)]
                                if formula is not None and (formula.text or "").strip():
                                    text = f"={formula.text}" + (f" [cached: {text}]" if text else "")
                                values.append(text.replace("\n", " "))
                            if any(values):
                                blocks.append(" | ".join(values))
                        blocks.append("")
            derivative_dir = source.with_suffix(source.suffix + ".pages")
            media = _extract_package_media(source, derivative_dir, ("xl/media/",))
            pages = _render_office_pages(source, derivative_dir)
            _append_visual_derivatives(blocks, source, media, pages)
            _append_chart_xml(blocks, source, "xl/charts/")
        elif suffix == ".docx":
            from docx import Document

            doc = Document(source)
            blocks.extend(paragraph.text for paragraph in doc.paragraphs if paragraph.text.strip())
            for table_index, table in enumerate(doc.tables, 1):
                blocks.extend(["", f"## Table {table_index}", ""])
                for row in table.rows:
                    blocks.append(" | ".join(cell.text.replace("\n", " ") for cell in row.cells))
            derivative_dir = source.with_suffix(source.suffix + ".pages")
            media = _extract_package_media(source, derivative_dir, ("word/media/",))
            pages = _render_office_pages(source, derivative_dir)
            _append_visual_derivatives(blocks, source, media, pages)
        elif suffix == ".pptx":
            from pptx import Presentation

            deck = Presentation(source)
            for index, slide in enumerate(deck.slides, 1):
                blocks.extend(["", f"## Slide {index}", ""])
                for shape in slide.shapes:
                    text = getattr(shape, "text", "").strip()
                    if text:
                        blocks.append(text)
                    if getattr(shape, "has_table", False):
                        for row in shape.table.rows:
                            blocks.append(
                                " | ".join(cell.text.replace("\n", " ") for cell in row.cells)
                            )
                    if getattr(shape, "has_chart", False):
                        blocks.append("> chart_present: yes")
                    description = str(getattr(shape, "alternative_text", "") or "").strip()
                    if description:
                        blocks.append(f"> alt_text: {description}")
            derivative_dir = source.with_suffix(source.suffix + ".pages")
            media = _extract_package_media(source, derivative_dir, ("ppt/media/",))
            pages = _render_office_pages(source, derivative_dir)
            _append_visual_derivatives(blocks, source, media, pages)
            _append_chart_xml(blocks, source, "ppt/charts/")
        elif suffix == ".pdf":
            _extract_pdf(source, target, blocks)
        elif suffix in {".txt", ".md", ".csv", ".tsv", ".json"}:
            blocks.append(source.read_text(encoding="utf-8", errors="replace"))
        else:
            return False
    except Exception as exc:  # noqa: BLE001
        blocks.extend(["", f"> Extraction failed: {type(exc).__name__}: {exc}"])
        target.write_text("\n".join(blocks).strip() + "\n", encoding="utf-8")
        return False
    target.write_text("\n".join(blocks).strip() + "\n", encoding="utf-8")
    return True


def _rapidocr_page(path: Path) -> tuple[str, list[dict], str]:
    """Best-effort OCR for scanned pages; native PDF text remains preferred."""
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return "", [], "RapidOCR unavailable"
    try:
        engine = RapidOCR()
        result, _ = engine(str(path))
    except Exception as exc:  # noqa: BLE001
        return "", [], f"RapidOCR failed: {type(exc).__name__}: {exc}"
    rows: list[dict] = []
    texts: list[str] = []
    for item in result or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        polygon, value = item[0], item[1]
        text = str(value or "").strip()
        if not text:
            continue
        texts.append(text)
        rows.append({
            "polygon": [
                [round(float(point[0]), 3), round(float(point[1]), 3)]
                for point in polygon
            ],
            "text": text,
            "chars": len(text),
        })
    return "\n".join(texts), rows, "" if texts else "RapidOCR returned no text"


def _extract_pdf(source: Path, target: Path, blocks: list[str]) -> None:
    """Extract complete native text and deterministic high-resolution page views.

    The readable companion remains the only extra document. Page PNG/JSON files
    live beside it as machine derivatives so Material and Image can inspect and
    crop paper figures without inventing another model-authored contract.
    """
    import pymupdf as fitz

    page_dir = source.with_suffix(source.suffix + ".pages")
    page_dir.mkdir(parents=True, exist_ok=True)
    document = fitz.open(source)
    page_summaries: list[str] = []
    try:
        for index, page in enumerate(document, 1):
            native_text = page.get_text("text", sort=True).strip()
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5), alpha=False)
            image_path = page_dir / f"page_{index:03d}.png"
            pixmap.save(image_path)

            text_rows: list[dict] = []
            for row in page.get_text("blocks", sort=True):
                if len(row) < 5:
                    continue
                text = str(row[4] or "").strip()
                if not text:
                    continue
                text_rows.append({
                    "bbox_pdf": [round(float(value), 3) for value in row[:4]],
                    "text": text,
                    "chars": len(text),
                })

            ocr_text = ""
            ocr_rows: list[dict] = []
            ocr_note = ""
            if len(native_text) < 20:
                ocr_text, ocr_rows, ocr_note = _rapidocr_page(image_path)
            resolved_text = native_text or ocr_text
            mode = "native" if native_text else "ocr" if ocr_text else "missing"
            layout = {
                "source": source.name,
                "page": index,
                "page_points": [round(page.rect.width, 3), round(page.rect.height, 3)],
                "image_pixels": [pixmap.width, pixmap.height],
                "text_mode": mode,
                "native_chars": len(native_text),
                "ocr_chars": len(ocr_text),
                "ocr_note": ocr_note,
                "text_blocks": text_rows,
                "ocr_blocks": ocr_rows,
            }
            image_path.with_suffix(".json").write_text(
                json.dumps(layout, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            blocks.extend([
                "",
                f"## Page {index}",
                "",
                f"> page_image: {image_path.relative_to(source.parent.parent)}",
                f"> text_mode: {mode}",
                "",
                resolved_text,
            ])
            page_summaries.append(
                f"P{index}: {mode}, {len(resolved_text)} chars, "
                f"{pixmap.width}x{pixmap.height} PNG"
            )
    finally:
        document.close()
    blocks[2:2] = [
        "> PDF ingestion: complete page-by-page native text plus 200+ DPI page PNGs.",
        "> Scanned pages use RapidOCR when available; `text_mode: missing` must be reported, never guessed.",
        "",
        "## Ingestion coverage",
        "",
        *[f"- {summary}" for summary in page_summaries],
        "",
    ]


_DIRECT_TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".csv", ".tsv", ".json", ".jsonl"}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


def _image_attachment_intent(query: str, item: object, filename: str) -> dict[str, object]:
    explicit = ""
    if isinstance(item, dict):
        explicit = str(
            item.get("intent") or item.get("attachment_intent") or item.get("purpose") or ""
        ).strip().lower().replace("-", "_")
    aliases = {
        "asset": "visual_asset",
        "image": "visual_asset",
        "visual": "visual_asset",
        "visual_asset": "visual_asset",
        "style": "style_reference",
        "reference": "style_reference",
        "style_reference": "style_reference",
        "evidence": "evidence",
        "content": "evidence",
        "ocr": "evidence",
        "mixed": "mixed",
        "both": "mixed",
    }
    intent = aliases.get(explicit, "")
    text = f"{query} {filename}".lower()
    style = bool(re.search(
        r"参考.{0,10}(?:风格|设计|版式|配色)|风格参考|照着.{0,10}(?:风格|设计)|"
        r"根据.{0,10}(?:图片|图像|照片).{0,8}(?:风格|设计|版式|配色)|"
        r"mood\s*board|style\s*reference|match.{0,12}(?:style|design)",
        text,
        re.I,
    ))
    visual = bool(re.search(
        r"(?:作为|用作|放到|放进|放在|用于).{0,10}(?:配图|封面|背景|logo|照片|插图|素材)|"
        r"直接使用.{0,6}(?:图片|图像|照片)|use.{0,12}(?:cover|image|photo|logo|asset)",
        text,
        re.I,
    ))
    evidence = bool(re.search(
        r"(?:读取|识别|提取|分析|总结).{0,10}(?:图片|图像|截图|扫描|图表|数据|内容)|"
        r"根据.{0,10}(?:图片|图像).{0,6}(?:内容|数据|文字)|"
        r"根据.{0,10}(?:截图|扫描|图表|数据|内容)|"
        r"(?:截图|扫描件|报表|表格|图表).{0,8}(?:内容|数据|文字)|"
        r"(?:screenshot|scan|chart|table|report).{0,16}(?:read|extract|analy[sz]e|data|content)",
        text,
        re.I,
    ))
    if not intent:
        if evidence and visual:
            intent = "mixed"
        elif evidence:
            intent = "evidence"
        elif style and visual:
            intent = "mixed"
        elif style:
            intent = "style_reference"
        else:
            # A standalone user image in a presentation request is most often
            # a supplied asset. Screenshot/scan filenames remain evidence-like.
            filename_evidence = bool(re.search(
                r"screenshot|screen[_ -]?shot|scan|截图|扫描|报表|chart|table",
                filename,
                re.I,
            ))
            intent = "evidence" if filename_evidence else "visual_asset"
    return {
        "intent": intent,
        "needs_content_extraction": intent in {"evidence", "mixed"},
        "needs_visual_reuse": intent in {"visual_asset", "mixed"},
        "needs_style_reference": intent == "style_reference" or (intent == "mixed" and style),
    }


def _attachment_record(target: Path, run_root: Path, item: object, query: str) -> dict[str, object]:
    suffix = target.suffix.lower()
    relative = str(target.relative_to(run_root))
    payload = target.read_bytes()
    record: dict[str, object] = {
        "path": relative,
        "name": target.name,
        "kind": "direct_text" if suffix in _DIRECT_TEXT_SUFFIXES else "image" if suffix in _IMAGE_SUFFIXES else "document",
        "intent": "direct_text" if suffix in _DIRECT_TEXT_SUFFIXES else "evidence",
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "mime": mimetypes.guess_type(target.name)[0] or "application/octet-stream",
        "needs_content_extraction": suffix not in _DIRECT_TEXT_SUFFIXES,
        "needs_visual_reuse": False,
        "needs_style_reference": False,
    }
    if suffix in _IMAGE_SUFFIXES:
        record.update(_image_attachment_intent(query, item, target.name))
        try:
            from PIL import Image

            with Image.open(target) as opened:
                record["pixels"] = [int(opened.width), int(opened.height)]
                record["mode"] = str(opened.mode)
        except Exception as exc:  # noqa: BLE001
            record["inspection_error"] = f"{type(exc).__name__}: {exc}"
    return record


def _write_direct_text_handoff(run_root: Path, records: list[dict[str, object]]) -> None:
    research = run_root / "research"
    research.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Direct attachment handoff",
        "",
        "- status: ready",
        "- material_stage: direct_text",
        "- evidence_scope: runtime_contract",
        "- unresolved_items: []",
        "",
        "Harness generated this deterministic text handoff without starting a Material Agent.",
    ]
    for index, record in enumerate(records, 1):
        path = run_root / str(record["path"])
        text = path.read_text(encoding="utf-8", errors="replace")
        lines.extend([
            "",
            f"## Attachment {index}: {record['name']}",
            f"- source_path: {record['path']}",
            f"- sha256: {record['sha256']}",
            f"- bytes: {record['bytes']}",
            "",
            "### Verbatim content",
            "",
            text,
        ])
    (research / "material.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def stage_materials(run_dir, seed):
    """Copy attachments, classify their intent, and expose deterministic handoffs."""
    raw = seed.get("materials") or seed.get("attachments") or []
    if isinstance(raw, (str, dict)):
        raw = [raw]
    staged = []
    failures: list[dict[str, str]] = []
    records: list[dict[str, object]] = []
    query = str(seed.get("user_query") or seed.get("query") or "")
    run_root = Path(run_dir)
    inputs = Path(run_dir) / "inputs"
    for index, item in enumerate(raw, 1):
        source_value = item.get("path") if isinstance(item, dict) else item
        if not source_value:
            raise ValueError(f"materials[{index}] 缺少 path")
        source = Path(str(source_value)).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"material 不存在: {source}")
        inputs.mkdir(parents=True, exist_ok=True)
        target = inputs / f"{index:02d}_{source.name}"
        shutil.copy2(source, target)
        staged.append(str(target.relative_to(run_dir)))
        record = _attachment_record(target, run_root, item, query)
        records.append(record)
        if target.suffix.lower() in (_DIRECT_TEXT_SUFFIXES | _IMAGE_SUFFIXES):
            continue
        companion = target.with_suffix(target.suffix + ".md")
        if _extract_material(target, companion):
            staged.append(str(companion.relative_to(run_dir)))
            record["companion_path"] = str(companion.relative_to(run_dir))
            if target.suffix.lower() == ".pdf" and acfg.VISION_BACKEND == "disabled":
                companion_text = companion.read_text(
                    encoding="utf-8", errors="replace"
                )
                if re.search(r"(?mi)^>\s*text_mode:\s*missing\s*$", companion_text):
                    failures.append({
                        "path": str(target.relative_to(run_dir)),
                        "error": (
                            "scanned page has no native/OCR text and the selected "
                            "main model is text-only (VISION_BACKEND=disabled)"
                        ),
                    })
        else:
            if companion.is_file():
                staged.append(str(companion.relative_to(run_dir)))
                record["companion_path"] = str(companion.relative_to(run_dir))
                detail = next(
                    (
                        line.removeprefix("> Extraction failed: ").strip()
                        for line in companion.read_text(
                            encoding="utf-8", errors="replace"
                        ).splitlines()
                        if line.startswith("> Extraction failed:")
                    ),
                    "unsupported or incomplete extraction",
                )
            else:
                detail = "unsupported attachment type"
            failures.append({"path": str(target.relative_to(run_dir)), "error": detail})
    material_records = [
        record for record in records if bool(record.get("needs_content_extraction"))
    ]
    direct_text_records = [record for record in records if record.get("kind") == "direct_text"]
    visual_asset_paths = [
        str(record["path"]) for record in records if bool(record.get("needs_visual_reuse"))
    ]
    style_reference_paths = [
        str(record["path"]) for record in records if bool(record.get("needs_style_reference"))
    ]
    material_agent_required = bool(material_records)
    if material_agent_required:
        material_stage = "mixed" if visual_asset_paths or style_reference_paths else "agent_required"
        material_agent_paths = []
        for record in records:
            if not bool(record.get("needs_content_extraction")) and record.get("kind") != "direct_text":
                continue
            material_agent_paths.append(str(record["path"]))
            if record.get("companion_path"):
                material_agent_paths.append(str(record["companion_path"]))
    elif direct_text_records:
        material_stage = "direct_text"
        material_agent_paths = []
        _write_direct_text_handoff(run_root, direct_text_records)
    else:
        material_stage = "omitted"
        material_agent_paths = []
    profile = {
        "schema": "mural.attachments.v1",
        "attachment_count": len(records),
        "material_stage": material_stage,
        "material_agent_required": material_agent_required,
        "material_agent_paths": material_agent_paths,
        "direct_text_paths": [str(record["path"]) for record in direct_text_records],
        "unresolved_items": [],
        "visual_asset_paths": visual_asset_paths,
        "style_reference_paths": style_reference_paths,
        "records": records,
    }
    trace = run_root / "_trace"
    trace.mkdir(parents=True, exist_ok=True)
    (trace / "attachment-manifest.json").write_text(
        json.dumps(profile, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    seed["_attachment_profile"] = profile
    seed["_material_ingestion_failures"] = failures
    return staged


def _material_blocked(run_dir: str, failures: list[dict[str, str]]) -> None:
    research = Path(run_dir) / "research"
    research.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Material ingestion",
        "",
        "- status: material_blocked",
        "- blocking: yes",
        "",
        "## Failed attachments",
        "",
    ]
    for item in failures:
        lines.append(f"- `{item['path']}`: {item['error']}")
    lines.extend([
        "",
        "The Harness stopped before Research and planning. Fix the deterministic parser "
        "or replace the unreadable attachment; external search must not guess its contents.",
    ])
    (research / "material.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def worker(task):
    load_dotenv()
    sid, run_dir = task["sample_id"], task["run_dir"]
    config, seed = task["config"], dict(task["seed"])
    if (
        os.environ.get("MODEL_BACKEND", "").strip().lower() != "openai"
        and "ANTHROPIC_API_KEY" not in os.environ
    ):
        return {"sample_id": sid, "status": "error", "run_dir": run_dir,
                "error": "子进程环境里没有 ANTHROPIC_API_KEY"}
    try:
        from core.agent_loop import run_sample
    except Exception as e:  # noqa: BLE001
        return {"sample_id": sid, "status": "error", "run_dir": run_dir,
                "error": f"无法导入 run_sample: {type(e).__name__}: {e}"}
    try:
        os.makedirs(run_dir, exist_ok=False)
    except FileExistsError:
        shutil.rmtree(run_dir, ignore_errors=True)
        try:
            os.makedirs(run_dir, exist_ok=False)
        except FileExistsError:
            return {"sample_id": sid, "status": "error", "run_dir": run_dir,
                    "error": "run_dir 反复无法创建，跳过"}
    try:
        seed["_staged_materials"] = stage_materials(run_dir, seed)
        ingestion_failures = list(seed.get("_material_ingestion_failures") or [])
        if ingestion_failures:
            _material_blocked(run_dir, ingestion_failures)
            return {
                "sample_id": sid,
                "run_dir": run_dir,
                "status": "material_blocked",
                "query": seed.get("query"),
                "skill_name": config.get("skill_name"),
                "skill_language": config.get("skill_language"),
                "material_ingestion_failures": ingestion_failures,
            }
        res = run_sample(sid, seed, run_dir, config)
        status = res.get("status", "completed") if isinstance(res, dict) else "completed"
        out = {"sample_id": sid, "run_dir": run_dir, "status": status}
        # query 与其元数据并入 manifest（accept/recorder/to_openai 需要）
        out["query"] = seed.get("query")
        out["skill_name"] = config.get("skill_name")
        out["skill_language"] = config.get("skill_language")
        out["query_language_hint"] = config.get("deck_language")
        for k in (
            "qid",
            "id",
            "task_type",
            "class",
            "lang",
            "slide_count",
            "category",
            "gravity",
            "domain",
            "domain_code",
            "domain_l1",
            "domain_l2",
            "style",
            "style_code",
            "style_group",
            "style_profile",
            "style_hint",
            "pages_hint",
            "length_level",
            "complexity",
            "detail_form",
            "image_requirement",
            "tone",
            "scene",
            "source",
            "model",
            "model_provider",
            "have_template",
            "_longpage",
            "_noattach_trace",
        ):
            if seed.get(k) is not None:
                out[k] = seed.get(k)
        if isinstance(res, dict):
            out.update({k: v for k, v in res.items() if k not in out})
        return out
    except Exception as e:  # noqa: BLE001
        return {"sample_id": sid, "run_dir": run_dir, "status": "error",
                "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:]}


def revision_worker(task):
    """Run a follow-up revision in an already populated, copied workspace."""
    load_dotenv()
    sid, run_dir = task["sample_id"], task["run_dir"]
    config, seed = task["config"], dict(task["seed"])
    revision = seed.get("_revision")
    if not isinstance(revision, dict):
        return {"sample_id": sid, "status": "error", "run_dir": run_dir,
                "error": "revision_worker 缺少 _revision 契约"}
    if (
        os.environ.get("MODEL_BACKEND", "").strip().lower() != "openai"
        and "ANTHROPIC_API_KEY" not in os.environ
    ):
        return {"sample_id": sid, "status": "error", "run_dir": run_dir,
                "error": "子进程环境里没有 ANTHROPIC_API_KEY"}
    if not os.path.isdir(run_dir):
        return {"sample_id": sid, "status": "error", "run_dir": run_dir,
                "error": "revision workspace 不存在"}
    try:
        from core.agent_loop import run_sample
        res = run_sample(sid, seed, run_dir, config)
        status = res.get("status", "completed") if isinstance(res, dict) else "completed"
        out = {
            "sample_id": sid,
            "run_dir": run_dir,
            "status": status,
            "query": seed.get("query"),
            "revision_no": revision.get("revision_no"),
            "parent_deck_id": revision.get("parent_deck_id"),
        }
        if isinstance(res, dict):
            out.update({key: value for key, value in res.items() if key not in out})
        return out
    except Exception as exc:  # noqa: BLE001
        return {"sample_id": sid, "run_dir": run_dir, "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc()[-1500:]}


class Progress:
    def __init__(self, total, run_mode="inference"):
        self.total = total
        self.run_mode = run_mode
        self.ok = self.rej = self.err = 0
        self.start = time.time()

    def update(self, rec):
        st = rec.get("status")
        if st == "completed":
            self.ok += 1
        elif st == "rejected":
            self.rej += 1
        else:
            self.err += 1
        done = self.ok + self.rej + self.err
        extra = ""
        if rec.get("reason") and st != "completed":
            extra = f"  [{rec['reason']}]"
        elif rec.get("error"):
            extra = f"  {str(rec['error'])[:80]}"
        print(f"  [{done}/{self.total}] {rec['sample_id']}: {st}{extra}", flush=True)

    def close(self):
        dur = time.time() - self.start
        activity = "合成" if self.run_mode == "synthesis" else "推理"
        print(f"\n{activity}完成。✓{self.ok} 通过  ⊘{self.rej} 丢弃  ✗{self.err} 失败  用时 {dur:.0f}s", flush=True)


def main():
    load_dotenv()
    ap = argparse.ArgumentParser(description="Clean parallel-role presentation rollout")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--queries", help="query jsonl 文件（每行 {qid?, query, ...}）")
    g.add_argument("--query", help="单条 inline query，临时跑一条")
    ap.add_argument("--batch", required=True, help="批次名，决定 runs/<batch>/ 与 manifest")
    ap.add_argument("--workers", type=int, default=4, help="并行子进程数")
    ap.add_argument("--limit", type=int, default=0, help="只取前 N 条(0=全部)")
    ap.add_argument("--resume", action="store_true", help="断点续跑：跳过 completed，重跑未完成")
    ap.add_argument("--overwrite", action="store_true", help="从头重来（删旧产物，危险）")
    ap.add_argument("--max-attempts", type=int, default=3, help="单 sample 最多尝试次数（含历史）")
    ap.add_argument(
        "--mode",
        choices=("inference", "synthesis"),
        default=os.environ.get("MURAL_RUN_MODE", "inference"),
        help=("inference=交付优先，释放已消费图片并压缩旧上下文；"
              "synthesis=训练数据优先，禁用有损上下文维护并强制完整轨迹"),
    )
    args = ap.parse_args()

    cores = cgroup_cpus()
    if args.workers > 2 * cores:
        print(f"⚠️  --workers={args.workers} 远超真实核数 {cores}；渲染会争抢/OOM。建议 ≈ {cores}~{2 * cores}。",
              flush=True)
    if args.resume and args.overwrite:
        raise SystemExit("--resume 与 --overwrite 互斥。")

    if args.query:
        # Inline and JSONL queries share the same deterministic language
        # selection unless the JSONL row explicitly supplies ``lang``.
        queries = [{"qid": "adhoc", "query": args.query}]
    else:
        queries = load_queries(args.queries)
    if args.limit:
        queries = queries[:args.limit]

    batch_dir = os.path.join(RUNS, args.batch)
    os.makedirs(LOGS, exist_ok=True)
    mpath = os.path.join(LOGS, f"{args.batch}.manifest.jsonl")
    has_runs = os.path.isdir(batch_dir) and os.listdir(batch_dir)
    if (os.path.exists(mpath) or has_runs) and not args.resume and not args.overwrite:
        raise SystemExit(f"批次 {args.batch} 已有产物。续跑加 --resume，从头来加 --overwrite，或换 --batch。")
    if args.overwrite:
        shutil.rmtree(batch_dir, ignore_errors=True)
        if os.path.exists(mpath):
            os.remove(mpath)
    os.makedirs(batch_dir, exist_ok=True)

    done = load_manifest(mpath) if args.resume else {}
    config = build_config(args)
    if config["nova_raw_v2"]:
        if not os.environ.get("CLEAN_NOVA_RAW_ROOT", "").strip():
            raise SystemExit(
                "CLEAN_NOVA_RAW_V2=1 时必须显式设置 CLEAN_NOVA_RAW_ROOT；"
                "禁止把 exact raw 默认写进可被 --overwrite 清理的工作目录"
            )
        if not acfg.NOVA_PROXY_BASE_URL:
            raise SystemExit("CLEAN_NOVA_RAW_V2=1 时必须设置 NOVA_PROXY_BASE_URL")
        if not acfg.NOVA_VISION_PROXY_BASE_URL:
            raise SystemExit(
                "CLEAN_NOVA_RAW_V2=1 时必须设置 NOVA_VISION_PROXY_BASE_URL"
            )
        if acfg.NOVA_VISION_PROXY_BASE_URL == acfg.NOVA_PROXY_BASE_URL:
            raise SystemExit(
                "Nova exact-raw 必须使用主/辅双 Proxy：主 Proxy 禁用 builtin "
                "vision_reader，NOVA_VISION_PROXY_BASE_URL 指向独立辅助 Proxy"
            )
        if acfg.VISION_BACKEND != "nova":
            raise SystemExit(
                "CLEAN_NOVA_RAW_V2=1 时必须设置 VISION_BACKEND=nova；"
                "主 Agent 禁止接收 native image"
            )
        from core.nova_raw import validate_proxy_health

        try:
            main_health = validate_proxy_health(
                acfg.NOVA_PROXY_BASE_URL,
                timeout_s=acfg.NOVA_HEALTH_TIMEOUT_S,
                expected_builtin_vision_reader=False,
            )
            vision_health = validate_proxy_health(
                acfg.NOVA_VISION_PROXY_BASE_URL,
                timeout_s=acfg.NOVA_HEALTH_TIMEOUT_S,
                expected_builtin_vision_reader=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(
                f"Nova Proxy preflight 失败，拒绝开始刷数：{type(exc).__name__}: {exc}"
            ) from exc
        raw_root = Path(config["nova_raw_root"]).resolve()
        mutable_batch_root = Path(batch_dir).resolve()
        if raw_root == mutable_batch_root or raw_root.is_relative_to(mutable_batch_root):
            raise SystemExit(
                "CLEAN_NOVA_RAW_ROOT 不能位于当前 runs/<batch> 内；"
                "exact raw 必须与可覆盖的 Deck workspace 隔离"
            )
        raw_root.mkdir(parents=True, exist_ok=True)
        print(
            "✓ Nova raw V2 preflight: health contract PASS; "
            f"raw_root={config['nova_raw_root']} "
            f"agent_upstream={main_health.get('agent_request_url')} "
            f"vision_upstream={vision_health.get('vision_request_url')} "
            "strict_dual_proxy=yes",
            flush=True,
        )
    # 实际生效的模型（经 .env 覆盖后的最终值，与 agent 落盘 config.json / 发给 API 的 model 同源）。
    # 显式打印，避免误以为用了 ~/.bashrc 的全局 ANTHROPIC_MODEL——环境变量到底哪个赢，一眼可查。
    print(f"🤖 model={config['model']}  base_url={acfg.ANTHROPIC_BASE_URL}  "
          f"mode={config['run_mode']}  "
          f"thinking={'on' if config['thinking'] else 'off'}  effort={config['effort']}  "
          f"max_turns={config['max_turns']}  max_tokens={config['max_tokens']}  "
          f"child_turns={config['child_max_turns']}  "
          f"child_parallel_initial={config['child_concurrency']}  "
          f"child_parallel_max={config['child_pool_max_workers']}  "
          f"remote_tool_parallel={config['remote_tool_concurrency']}  "
          f"workers={config['batch_workers']}  deck_timeout={config['deck_timeout_s']}s  "
          f"生图={'开' if config['enable_image_gen'] else '关'}",
          flush=True)
    if not config["enable_image_gen"]:
        print("ℹ️  生图未启用（ENABLE_IMAGE_GEN=0 或缺 OPENAI_API_KEY）：一切视觉将代码绘制。", flush=True)

    existing = set(os.listdir(batch_dir)) if os.path.isdir(batch_dir) else set()
    tasks, skipped, exhausted, salvageable, seen = [], 0, 0, 0, {}
    for q in queries:
        sid = make_sample_id(args.batch, q, seen)
        run_dir = os.path.join(batch_dir, sid)
        prev = done.get(sid, {})
        if prev.get("status") in TERMINAL:
            skipped += 1
            continue
        if prev.get("_attempts", 0) >= args.max_attempts:
            exhausted += 1
            continue
        if sid in existing:
            # 完整但未入 manifest（worker 收尾被中断）→ 别毁、别重跑，留给 rescue.py 救回（省钱）
            if run_dir_complete(run_dir):
                salvageable += 1
                continue
            shutil.rmtree(run_dir, ignore_errors=True)
        sample_config = dict(config)
        sample_config["skill_name"], sample_config["skill_language"] = select_skill(q)
        sample_config["deck_language"] = infer_deck_language(q)
        required_skills = (
            [acfg.SKILL_NAME_ZH, acfg.SKILL_NAME_EN]
            if sample_config["skill_language"] == "auto"
            else [sample_config["skill_name"]]
        )
        for required_skill in required_skills:
            skill_root = acfg.SKILLS_DIR / required_skill
            if not (skill_root / "SKILL.md").is_file():
                raise SystemExit(
                    f"候选 Skill 不完整：{skill_root}；请先安装对应语言版本"
                )
        tasks.append(
            {
                "sample_id": sid,
                "seed": q,
                "run_dir": run_dir,
                "config": sample_config,
            }
        )

    print(f"batch={args.batch}  queries={len(queries)}  待跑={len(tasks)}  "
          f"已完成跳过={skipped}  达上限={exhausted}  可救回(完整未入manifest)={salvageable}  workers={args.workers}",
          flush=True)
    if salvageable:
        print(f"ℹ️  {salvageable} 个 deck 已完整产出但未入 manifest，已保留未重跑。", flush=True)
    if not tasks:
        print("没有要跑的。")
        return

    prog = Progress(len(tasks), config["run_mode"])

    def finish_record(rec, task):
        rec["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        append_manifest(mpath, rec)
        prog.update(rec)

    child_concurrency_file = config["child_concurrency_file"]
    if child_concurrency_file:
        child_control = Path(child_concurrency_file)
        try:
            ensure_control_file(child_control, config["child_concurrency"])
        except OSError as exc:
            print(
                f"⚠️  child 并发控制文件暂不可创建: {child_control} "
                f"({type(exc).__name__}: {exc})；"
                "每波委派将保留上一次有效值。",
                flush=True,
            )
        print(
            f"[live] child 并发控制文件: {child_control} "
            f"(初始={config['child_concurrency']}，"
            f"池上限={config['child_pool_max_workers']})\n"
            "[live] 新值只影响之后发起的 delegate_task；"
            "已经提交的子任务不取消、不缩容。",
            flush=True,
        )

    concurrency_file = os.environ.get("CLEAN_CONCURRENCY_FILE")
    pool_max = max(
        args.workers,
        int(os.environ.get("CLEAN_POOL_MAX_WORKERS", str(args.workers))),
    )
    concurrency_poll = max(
        0.2,
        float(os.environ.get("CLEAN_CONCURRENCY_POLL_SEC", "5")),
    )
    if concurrency_file:
        control = Path(concurrency_file)
        ensure_control_file(control, args.workers)
        print(
            f"[live] deck 并发控制文件: {control} "
            f"(当前目标={read_concurrency_target(str(control), args.workers, pool_max)}, "
            f"池上限={pool_max})\n"
            f"[live] 在线调整: echo N > {control}；0=优雅暂停补发，"
            "缩容和暂停都不打断在飞 deck。",
            flush=True,
        )

    # A one-sample interactive run gains no isolation or throughput from wrapping
    # the Orchestrator in another long-lived process. Page workers are still
    # parallel through the singular Hermes delegate_task adapter.
    if args.workers == 1 and pool_max == 1 and not concurrency_file:
        for task in tasks:
            try:
                rec = worker(task)
            except Exception as e:  # noqa: BLE001
                rec = {"sample_id": task["sample_id"], "run_dir": task["run_dir"],
                       "status": "error", "error": f"子进程崩溃: {type(e).__name__}: {e}"}
            finish_record(rec, task)
    else:
        pool_kw = {"max_workers": pool_max}
        if sys.version_info >= (3, 11):
            pool_kw["max_tasks_per_child"] = 1
        with cf.ProcessPoolExecutor(**pool_kw) as ex:
            next_task = 0
            in_flight: dict[cf.Future, dict] = {}
            while next_task < len(tasks) or in_flight:
                target = read_concurrency_target(
                    concurrency_file,
                    args.workers,
                    pool_max,
                )
                while next_task < len(tasks) and len(in_flight) < target:
                    task = tasks[next_task]
                    next_task += 1
                    in_flight[ex.submit(worker, task)] = task
                if not in_flight:
                    if next_task < len(tasks) and concurrency_file:
                        time.sleep(concurrency_poll)
                        continue
                    break
                completed, _ = cf.wait(
                    tuple(in_flight),
                    timeout=concurrency_poll,
                    return_when=cf.FIRST_COMPLETED,
                )
                for fut in completed:
                    task = in_flight.pop(fut)
                    try:
                        rec = fut.result()
                    except Exception as e:  # noqa: BLE001
                        rec = {"sample_id": task["sample_id"], "run_dir": task["run_dir"],
                               "status": "error", "error": f"子进程崩溃: {type(e).__name__}: {e}"}
                    finish_record(rec, task)
    prog.close()
    print(f"manifest: {mpath}")


if __name__ == "__main__":
    main()
