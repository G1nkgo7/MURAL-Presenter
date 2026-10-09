"""Office 原生图表与公式的隔离派生；不修改原件或替换 MinerU 的正文。"""
from __future__ import annotations

from io import BytesIO
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from xml.etree import ElementTree as ET

from PIL import Image


DERIVATION_VERSION = "libreoffice-native-evidence-v1"
_FUNCTIONS = {"SUM", "AVERAGE", "MIN", "MAX", "COUNT", "COUNTA", "IF", "AND", "OR", "NOT",
              "ROUND", "ROUNDUP", "ROUNDDOWN", "ABS", "INT", "MOD", "SQRT", "POWER"}
_CELL_RANGE = re.compile(r"(?:(?:'[^']*(?:''[^']*)*'|[\w ]+)!)?\$?[A-Z]{1,3}\$?[1-9][0-9]{0,6}"
                         r"(?::\$?[A-Z]{1,3}\$?[1-9][0-9]{0,6})?", re.I)


class OfficeError(RuntimeError):
    """只向解析回执暴露固定错误码。"""


def _formula_allowed(expression: str) -> bool:
    from openpyxl.formula import Tokenizer
    from openpyxl.formula.tokenizer import TokenizerError

    if not expression or len(expression) > 4096:
        return False
    try:
        tokens = Tokenizer("=" + expression.lstrip("=")).items
    except TokenizerError:
        return False
    if len(tokens) > 1024:
        return False
    for token in tokens:
        if token.type == "FUNC" and token.subtype == "OPEN" and token.value[:-1].upper() not in _FUNCTIONS:
            return False
        if token.type == "OPERAND" and token.subtype == "RANGE" and not _CELL_RANGE.fullmatch(token.value):
            return False
        if token.type not in {"FUNC", "OPERAND", "SEP", "PAREN", "OPERATOR-PREFIX", "OPERATOR-INFIX", "OPERATOR-POSTFIX", "WHITE-SPACE"}:
            return False
    return bool(tokens)


def _safe_package(payload: bytes, *, nested: bool = False) -> None:
    """拒绝主动内容与外部依赖；即使转换器配置变化，也不向其加载这些来源。"""
    try:
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            entries = archive.infolist()
            if sum(item.file_size for item in entries) > 256 * 1024 * 1024:
                raise OfficeError("office_preview_package_too_large")
            for entry in entries:
                name = entry.filename
                lower = name.lower()
                if any(marker in lower for marker in ("vbaproject", "activex/", "externallinks/", "macrosheets/")):
                    raise OfficeError("office_active_content_disabled")
                if "/embeddings/" in lower:
                    if nested or not lower.endswith(".xlsx"):
                        raise OfficeError("office_embedded_active_content_disabled")
                    _safe_package(archive.read(entry), nested=True)
                if not lower.endswith((".xml", ".rels")):
                    continue
                if entry.file_size > 64 * 1024 * 1024:
                    raise OfficeError("office_preview_xml_too_large")
                root = ET.fromstring(archive.read(entry))
                for node in root.iter():
                    tag = node.tag.rsplit("}", 1)[-1]
                    active_type = node.get("Type", "") + " " + node.get("ContentType", "")
                    if any(marker in active_type.lower() for marker in ("vbaproject", "macroenabled", "activex", "macrosheet")):
                        raise OfficeError("office_active_content_disabled")
                    if tag == "Relationship" and node.get("TargetMode", "").lower() == "external":
                        raise OfficeError("office_external_links_disabled")
                    if tag in {"f", "formula"} and node.text and not _formula_allowed(node.text):
                        raise OfficeError("office_formula_outside_controlled_subset")
                    if tag == "instrText" and re.search(r"\b(DDE(?:AUTO)?|INCLUDETEXT|INCLUDEPICTURE|DATABASE|LINK)\b", node.text or "", re.I):
                        raise OfficeError("office_external_fields_disabled")
    except (zipfile.BadZipFile, ET.ParseError, KeyError, OSError):
        raise OfficeError("office_invalid_preview_package") from None


def _uno_python(process: subprocess.Popen) -> tuple[str, dict[str, str]]:
    # 沿本次实际启动进程找同一安装的 UNO Python，避免另配一套 Office 路径。
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise OfficeError("office_preview_start_failed")
        executable = Path(f"/proc/{process.pid}/exe").resolve()
        if executable.name in {"oosplash", "soffice.bin"}:
            program = executable.parent
            child_environment = dict(item.split(b"=", 1) for item in
                Path(f"/proc/{process.pid}/environ").read_bytes().split(b"\0") if b"=" in item)
            selected = program / "python"
            environment = {"LD_LIBRARY_PATH": child_environment.get(b"LD_LIBRARY_PATH", b"").decode(),
                           "PYTHONPATH": str(program)}
            return (str(selected) if selected.is_file() else sys.executable), environment
        time.sleep(0.1)
    raise OfficeError("office_uno_runtime_unavailable")


def derive(source: Path, temporary_root: Path, info: dict, *, preview: bool = False, environment=None) -> dict:
    result = {"derivation_version": DERIVATION_VERSION, "files": {}, "charts": [],
              "formulas": list(info.get("formulas", [])), "unresolved_items": []}
    if not preview and not info.get("native_charts") and not any(
            not item["has_cached_value"] for item in result["formulas"]):
        return result
    executable = shutil.which("libreoffice") or shutil.which("soffice")
    if not executable:
        result["unresolved_items"] = [{"reason": "office_preview_unavailable"}]
        return result
    try:
        _safe_package(source.read_bytes())
    except OfficeError as exc:
        result["unresolved_items"] = [{"reason": str(exc)}]
        return result
    temporary_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="office-", dir=temporary_root) as directory:
        work = Path(directory)
        profile = work / "profile"
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        environment = {**os.environ, **(environment or {}), "TMPDIR": str(work), "PYTHONDONTWRITEBYTECODE": "1"}
        if "FONTCONFIG_FILE" not in environment and not Path("/etc/fonts/fonts.conf").is_file():
            existing = Path.home() / ".local/chromedeps/etc/fonts/fonts.conf"
            if existing.is_file():
                environment["FONTCONFIG_FILE"] = str(existing)
        try:
            process = subprocess.Popen([executable, f"-env:UserInstallation={profile.as_uri()}",
                "--headless", "--nologo", "--nodefault", "--nofirststartwizard", "--norestore",
                f"--accept=socket,host=127.0.0.1,port={port};urp;StarOffice.ComponentContext"],
                env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            result["unresolved_items"] = [{"reason": "office_preview_start_failed"}]
            return result
        try:
            interpreter, uno_environment = _uno_python(process)
            request = {"source": str(source.resolve()), "directory": str(work), "port": port,
                       "format": info["requested_format"], "formulas": result["formulas"], "preview": preview}
            completed = subprocess.run([interpreter, str(Path(__file__).with_name("office_worker.py"))],
                input=json.dumps(request), text=True, capture_output=True, timeout=180,
                env={**environment, **uno_environment})
            if completed.returncode:
                raise OfficeError("office_native_derivation_failed")
            derived = json.loads(completed.stdout)
            for chart in derived["charts"]:
                name = chart.pop("filename")
                if Path(name).name != name:
                    raise OfficeError("office_invalid_derived_path")
                raw = (work / name).read_bytes()
                with Image.open(BytesIO(raw)) as image:
                    if image.format != "PNG" or image.width * image.height > 40_000_000:
                        raise OfficeError("office_invalid_chart_image")
                    image.verify()
                chart["img_path"] = "office/" + name
                result["files"][chart["img_path"]] = raw
            if preview:
                result["files"]["office/preview.pdf"] = (work / "preview.pdf").read_bytes()
            result.update(charts=derived["charts"], formulas=derived["formulas"],
                          engine_version=derived["engine_version"])
            for item in result["formulas"]:
                if item.get("calculation_error"):
                    result["unresolved_items"].append({"reason": "office_formula_calculation_error",
                        "worksheet": item["worksheet"], "cell": item["cell"], "error": item["calculation_error"]})
            if len(result["charts"]) < info.get("native_charts", 0):
                result["unresolved_items"].append({"reason": "office_native_chart_missing",
                    "expected": info["native_charts"], "derived": len(result["charts"])})
        except (OfficeError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
            reason = str(exc) if isinstance(exc, OfficeError) else "office_native_derivation_failed"
            result["unresolved_items"].append({"reason": reason})
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
    return result
