"""固定 MinerU 3.4.5 的多格式协议；只把明确基础设施故障标为可切换。"""
from __future__ import annotations

import base64
import binascii
import hashlib
from io import BytesIO
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import re
import posixpath
import zipfile
from xml.etree import ElementTree as ET

import requests
from PIL import Image, UnidentifiedImageError


ADAPTER_VERSION = "mineru-file-parse-json-v3"
SERVICE_VERSION = "3.4.5"
PROTOCOL_VERSION = 2
BACKENDS = ("hybrid-auto-engine", "pipeline")
REQUEST_TIMEOUTS = (1800, 7200)
COMMAND_TIMEOUT = sum(REQUEST_TIMEOUTS) + 120
PARAMETERS = {
    "parse_method": "auto", "lang_list": "ch",
    "return_md": "true", "return_middle_json": "true",
    "return_content_list": "true", "return_images": "true",
    "return_model_output": "false", "response_format_zip": "false",
    "start_page_id": "0", "end_page_id": "99999",
}
MAX_RESPONSE_BYTES = 256 * 1024 * 1024
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000


class MinerUError(RuntimeError):
    """仅公开错误码；不回显远端正文、URL 或请求凭证。"""

    def __init__(self, code: str, *, infrastructure: bool = False):
        super().__init__(code)
        self.infrastructure = infrastructure


def _request(method: str, url: str, **kwargs) -> dict:
    try:
        with requests.Session() as session:
            session.trust_env = False
            with session.request(method, url, allow_redirects=False, stream=True,
                                 timeout=kwargs.pop("timeout", (5, 1800)), **kwargs) as response:
                if response.status_code != 200:
                    raise MinerUError(f"mineru_http_{response.status_code}",
                        infrastructure=response.status_code in {429, 502, 503, 504})
                payload = bytearray()
                for chunk in response.iter_content(64 * 1024):
                    payload.extend(chunk)
                    if len(payload) > MAX_RESPONSE_BYTES:
                        raise MinerUError("mineru_response_too_large")
                value = json.loads(payload)
    except requests.exceptions.SSLError:
        raise MinerUError("mineru_tls_failed") from None
    except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError) as exc:
        raise MinerUError(f"mineru_transport_{type(exc).__name__}", infrastructure=True) from None
    except requests.RequestException as exc:
        raise MinerUError(f"mineru_transport_{type(exc).__name__}") from None
    except (ValueError, UnicodeError):
        raise MinerUError("mineru_invalid_json") from None
    if not isinstance(value, dict):
        raise MinerUError("mineru_invalid_response")
    return value


def _endpoint(url: str) -> str:
    from .config import service_url
    try:
        return service_url(url)
    except ValueError:
        raise MinerUError("mineru_invalid_service_url") from None


def service_info(url: str) -> dict:
    value = _request("GET", _endpoint(url) + "/health", timeout=(3, 5))
    if value.get("version") not in (None, SERVICE_VERSION):
        raise MinerUError("mineru_server_version_changed")
    if value.get("protocol_version") not in (None, PROTOCOL_VERSION):
        raise MinerUError("mineru_protocol_version_changed")
    if value.get("status") == "unhealthy":
        raise MinerUError("mineru_unhealthy", infrastructure=True)
    if (value.get("status") != "healthy" or value.get("version") != SERVICE_VERSION
            or type(value.get("protocol_version")) is not int or value["protocol_version"] != PROTOCOL_VERSION):
        raise MinerUError("mineru_invalid_health")
    return {key: value[key] for key in ("version", "protocol_version")}


def source_info(source: Path) -> dict:
    """仅核对输入身份和位置，不提取另一份正文或推断材料用途。"""
    suffix = source.suffix.lower()
    if any(ord(c) < 32 or c in "/\\" for c in source.name):
        raise MinerUError("mineru_unsafe_source_name")
    info = {"requested_format": suffix[1:], "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "unresolved_items": [], "embedded_images": 0, "tables": 0}
    if suffix == ".pdf":
        import pymupdf
        try:
            with pymupdf.open(source) as document:
                if not document.is_pdf or document.needs_pass:
                    raise MinerUError("mineru_invalid_pdf")
                info.update(media_type="application/pdf", granularity="page", units=list(range(1, len(document) + 1)))
        except (RuntimeError, ValueError):
            raise MinerUError("mineru_invalid_pdf") from None
    elif suffix in {".png", ".jpg", ".jpeg"}:
        try:
            with Image.open(source) as image:
                if Image.registered_extensions().get(suffix) != image.format:
                    raise MinerUError("mineru_source_mime_extension_mismatch")
                info.update(media_type=Image.MIME[image.format], granularity="image", units=[1])
                image.verify()
        except (OSError, ValueError, Image.DecompressionBombError):
            raise MinerUError("mineru_invalid_source_image") from None
    elif suffix in {".docx", ".pptx", ".xlsx"}:
        types = {".docx": ("word/document.xml", "wordprocessingml.document", "document_block"),
                 ".pptx": ("ppt/presentation.xml", "presentationml.presentation", "slide"),
                 ".xlsx": ("xl/workbook.xml", "spreadsheetml.sheet", "worksheet")}
        main, mime, granularity = types[suffix]
        info.update(media_type=f"application/vnd.openxmlformats-officedocument.{mime}", granularity=granularity)
        try:
            with zipfile.ZipFile(source) as archive:
                names = archive.namelist()
                if main not in names or "[Content_Types].xml" not in names or len(names) != len(set(names)):
                    raise MinerUError("mineru_source_mime_extension_mismatch")
                def xml(path):
                    if archive.getinfo(path).file_size > 64 * 1024 * 1024:
                        raise MinerUError("mineru_source_xml_too_large")
                    return ET.fromstring(archive.read(path))
                tree = xml(main)
                info["embedded_images"] = len([name for name in names if name.startswith(main.split("/")[0] + "/media/")])
                info["native_charts"] = len([name for name in names if re.fullmatch(r"(?:word|ppt|xl)/charts/chart[0-9]+\.xml", name)])
                info["formulas"] = []
                if suffix == ".docx":
                    info["units"] = ["document"]
                    info["tables"] = len(tree.findall(".//{*}tbl"))
                else:
                    elements = tree.findall(".//{*}sldId") if suffix == ".pptx" else tree.findall(".//{*}sheet")
                    info["units"] = (list(range(1, len(elements) + 1)) if suffix == ".pptx"
                                     else [item.attrib["name"] for item in elements])
                    prefix, filename = main.rsplit("/", 1)
                    relationships = {row.attrib["Id"]: row.attrib["Target"] for row in xml(f"{prefix}/_rels/{filename}.rels")}
                    info["table_units"] = []
                    for index, item in enumerate(elements):
                        target = relationships[item.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]]
                        part = target.lstrip("/") if target.startswith("/") else posixpath.normpath(prefix + "/" + target)
                        unit = xml(part)
                        if suffix == ".pptx":
                            if unit.findall(".//{*}tbl"):
                                info["table_units"].append(index)
                        else:
                            cells = unit.findall(".//{*}c")
                            if any(list(cell) for cell in cells):
                                info["table_units"].append(index)
                            shared = {formula.get("si"): (cell.get("r"), formula.text)
                                      for cell in cells if (formula := cell.find("{*}f")) is not None
                                      and formula.get("t") == "shared" and formula.text}
                            for cell in cells:
                                formula = cell.find("{*}f")
                                if formula is not None:
                                    expression = "=" + formula.text if formula.text else None
                                    shared_source = None
                                    if expression is None and formula.get("t") == "shared" and formula.get("si") in shared:
                                        from openpyxl.formula.translate import Translator, TranslatorError
                                        from openpyxl.formula.tokenizer import TokenizerError
                                        origin, text = shared[formula.get("si")]
                                        shared_source = {"cell": origin, "expression": "=" + text}
                                        try:
                                            if origin and cell.get("r"):
                                                expression = Translator("=" + text, origin=origin).translate_formula(cell.get("r"))
                                        except (TranslatorError, TokenizerError, ValueError, TypeError):
                                            pass
                                    item_formula = {"worksheet": item.attrib["name"], "cell": cell.attrib.get("r"),
                                        "expression": expression, "formula_attributes": dict(formula.attrib),
                                        "cached_value": cell.findtext("{*}v"), "has_cached_value": bool(cell.findtext("{*}v"))}
                                    if shared_source is not None:
                                        item_formula["shared_formula_source"] = shared_source
                                    if expression is None:
                                        info["unresolved_items"].append({"reason": "formula_expression_unresolved",
                                            "worksheet": item.attrib["name"], "cell": cell.attrib.get("r")})
                                    info["formulas"].append(item_formula)
                                if formula is not None and not cell.findtext("{*}v"):
                                    info["unresolved_items"].append({"reason": "formula_has_no_cached_value",
                                        "worksheet": item.attrib["name"], "cell": cell.attrib.get("r")})
        except (OSError, ValueError, KeyError, zipfile.BadZipFile, ET.ParseError):
            raise MinerUError("mineru_invalid_office_source") from None
    else:
        raise MinerUError("unsupported_format")
    return info


def _image(name: str, value: str) -> bytes:
    if (not name or Path(name).name != name or "\\" in name or name in {".", ".."}
            or "\x00" in name):
        raise MinerUError("mineru_unsafe_image_path")
    if not isinstance(value, str) or ";base64," not in value:
        raise MinerUError("mineru_invalid_image_encoding")
    header, encoded = value.split(";base64,", 1)
    if len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
        raise MinerUError("mineru_image_too_large")
    try:
        raw = base64.b64decode(encoded, validate=True)
        with Image.open(BytesIO(raw)) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise MinerUError("mineru_image_dimensions_exceeded")
            expected = Image.MIME.get(image.format)
            if header != "data:" + str(expected):
                raise MinerUError("mineru_image_mime_mismatch")
            if Image.registered_extensions().get(Path(name).suffix.lower()) != image.format:
                raise MinerUError("mineru_image_extension_mismatch")
            image.verify()
    except (binascii.Error, UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise MinerUError("mineru_invalid_image_bytes") from None
    return raw


class _ImageSources(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            self.paths.extend(value for name, value in attrs if name == "src" and value)


def image_references(value):
    """Yield normalized MinerU image references from structured rows or Markdown."""
    if isinstance(value, list):
        for item in value:
            yield from image_references(item)
    elif isinstance(value, dict):
        for key, child in value.items():
            if key in {"img_path", "image_path"} and child:
                yield child if key == "img_path" or str(child).startswith("images/") else "images/" + str(child)
            else:
                yield from image_references(child)
    elif isinstance(value, str):
        if "<img" in value.lower():
            parser = _ImageSources()
            parser.feed(value)
            yield from parser.paths
        yield from (match[1].strip("<>") for match in re.finditer(r"!\[[^\]]*\]\(\s*([^\s)]+)(?:\s+[^)]*)?\)", value))


def parse_document(endpoint: str, version: str, source: Path, attachment_id: str,
                   media_type: str, kind: str) -> dict:
    info = source_info(source)
    if (version != SERVICE_VERSION or media_type != info["media_type"]
            or attachment_id != "att-" + info["source_sha256"][:12] or kind not in {"gpu", "cpu"}):
        raise MinerUError("mineru_invalid_source_binding")
    endpoint_index = 0 if kind == "gpu" else 1
    with source.open("rb") as handle:
        value = _request("POST", _endpoint(endpoint) + "/file_parse",
            data={**PARAMETERS, "backend": BACKENDS[endpoint_index]},
            files={"files": (source.name, handle, media_type)}, timeout=(5, REQUEST_TIMEOUTS[endpoint_index]))
    if hashlib.sha256(source.read_bytes()).hexdigest() != info["source_sha256"]:
        raise MinerUError("mineru_source_changed_during_parse")
    if value.get("version") != version:
        raise MinerUError("mineru_server_version_changed")
    if value.get("status", "completed") not in ("completed", "partial"):
        raise MinerUError("mineru_task_not_completed")
    backend = value.get("backend")
    results = value.get("results")
    if backend != ("hybrid-engine" if kind == "gpu" else "pipeline"):
        raise MinerUError("mineru_backend_changed")
    if (not isinstance(results, dict) or set(results) != {source.stem}
            or not isinstance(results[source.stem], dict)):
        raise MinerUError("mineru_invalid_result_binding")
    result = results[source.stem]
    files: dict[str, bytes] = {}
    unresolved = list(info["unresolved_items"])
    if value.get("status") == "partial":
        unresolved.append({"reason": "mineru_reported_partial"})
    decoded: dict[str, object] = {}
    for field, filename, expected in (
        ("md_content", "document.md", str), ("middle_json", "middle.json", dict),
        ("content_list", "content-list.json", list),
    ):
        raw = result.get(field)
        if raw is None:
            unresolved.append({"reason": f"mineru_missing_{field}"})
            continue
        if not isinstance(raw, str):
            raise MinerUError(f"mineru_invalid_{field}")
        try:
            data = raw if expected is str else json.loads(raw)
        except ValueError:
            raise MinerUError(f"mineru_invalid_{field}") from None
        if not isinstance(data, expected):
            raise MinerUError(f"mineru_invalid_{field}")
        decoded[field] = data
        files[filename] = raw.encode("utf-8")
        if field == "md_content" and not raw.strip():
            unresolved.append({"reason": "mineru_empty_markdown"})
    if str(decoded.get("md_content", "")).strip() and decoded.get("content_list") == []:
        unresolved.append({"reason": "mineru_empty_content_list"})
    images = result.get("images")
    if images is None:
        unresolved.append({"reason": "mineru_missing_images"})
    elif not isinstance(images, dict):
        raise MinerUError("mineru_invalid_images")
    else:
        for name, payload in images.items():
            files["images/" + name] = _image(name, payload)
    unit_indices = []
    middle = decoded.get("middle_json", {})
    office = info["requested_format"] in {"docx", "pptx", "xlsx"}
    if middle and (middle.get("_version_name") != version or middle.get("_backend") !=
                   ("office" if office else "hybrid" if kind == "gpu" else "pipeline")):
        raise MinerUError("mineru_invalid_structure_backend")
    # 3.4.5 实测 Office 也复用 pdf_info 这个名字；单位必须由真实源格式确定，不能冒充 PDF 页。
    containers = middle.get("pdf_info", [])
    if not isinstance(containers, list):
        raise MinerUError("mineru_invalid_structure")
    units = info["units"]
    for container in containers:
        index = container.get("page_idx") if isinstance(container, dict) else None
        if type(index) is not int or not 0 <= index < len(units) or index in unit_indices:
            raise MinerUError("mineru_invalid_location_index")
        if not isinstance(container.get("para_blocks"), list):
            raise MinerUError("mineru_invalid_structure_blocks")
        if not office:
            size = container.get("page_size")
            if (not isinstance(size, list) or len(size) != 2 or
                    any(type(value) not in {int, float} or not math.isfinite(value) or value <= 0 for value in size)):
                raise MinerUError("mineru_invalid_page_geometry")
        unit_indices.append(index)
    for index, unit in enumerate(units):
        if index not in unit_indices:
            unresolved.append({"reason": "mineru_source_unit_missing", "granularity": info["granularity"], "unit": unit})
    content = decoded.get("content_list", [])
    for item in content:
        if not isinstance(item, dict) or type(item.get("page_idx")) is not int or item["page_idx"] not in unit_indices:
            raise MinerUError("mineru_invalid_content_location_binding")
    if info["requested_format"] == "xlsx":
        for index in unit_indices:
            titles = [row.get("text") for row in content if row["page_idx"] == index and row.get("text_level") == 1]
            if units[index] not in titles:
                raise MinerUError("mineru_invalid_worksheet_binding")
    if info["embedded_images"] and not images:
        unresolved.append({"reason": "mineru_embedded_images_missing", "source_count": info["embedded_images"]})
    if info["tables"] and not any(row.get("type") == "table" for row in content):
        unresolved.append({"reason": "mineru_tables_missing", "source_count": info["tables"]})
    for index in info.get("table_units", []):
        if not any(row.get("type") == "table" and row["page_idx"] == index for row in content):
            unresolved.append({"reason": "mineru_source_unit_table_missing",
                "granularity": info["granularity"], "unit": units[index]})
    for item in content:
        if item.get("type") == "chart" and not (item.get("img_path") or item.get("image_path")):
            unresolved.append({"reason": "mineru_chart_visual_missing", "granularity": info["granularity"],
                "unit": units[item["page_idx"]]})
    references = list(image_references([middle, decoded.get("content_list", []), decoded.get("md_content", "")]))
    if any(not isinstance(path, str) for path in references):
        raise MinerUError("mineru_invalid_image_reference")
    for image_path in sorted(set(references)):
        if image_path in {"", "images/"}:
            continue
        if (not isinstance(image_path, str) or not image_path.startswith("images/")
                or Path(image_path).parts != ("images", Path(image_path).name) or "\\" in image_path):
            raise MinerUError("mineru_unsafe_image_reference")
        if image_path not in files:
            unresolved.append({"reason": "mineru_referenced_image_missing", "path": image_path})
    media = []
    for name, raw in files.items():
        if name.startswith("images/"):
            with Image.open(BytesIO(raw)) as image:
                media.append({"path": name, "sha256": hashlib.sha256(raw).hexdigest(),
                    "mime": Image.MIME[image.format], "width": image.width, "height": image.height})
    coverage = {"granularity": info["granularity"], "expected_units": units,
                "returned_units": [units[index] for index in sorted(unit_indices)],
                "content_items": len(content), "visual_completeness": "not_proven"}
    if not office:
        coverage["content_bbox_units"] = "normalized_0_1000"
    if info["requested_format"] == "docx":
        coverage["locations"] = [{"block": index, "content_pointer": f"/{index}"} for index in range(len(content))]
    else:
        coverage["locations"] = [{info["granularity"]: units[index], "middle_pointer": f"/pdf_info/{position}"}
                                 for position, index in enumerate(unit_indices)]
    return {"backend": backend, "structure_backend": middle.get("_backend"),
            "version": version, "files": files, "media": media, "coverage": coverage,
            "pages": [index + 1 for index in sorted(unit_indices)] if not office else [],
            "unresolved_items": unresolved}
