"""人工编辑只回写选中的源码节点；渲染复用固定 Skill，不制造模型检查回执。"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
from html import escape
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid


MAX_REQUEST_BYTES = 18 * 1024 * 1024
VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
NON_EDITABLE = frozenset("html head body script style link meta base title source iframe object embed video".split())
STYLE_KEYS = frozenset({"transform", "width", "height", "font-size", "color",
                        "-webkit-text-fill-color", "opacity", "white-space"})
_busy: set[int] = set()
_guard = threading.Lock()


class EditError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def require_idle(deck_id: int) -> None:
    with _guard:
        if deck_id in _busy:
            raise EditError("该演示正在保存、渲染或导出，请完成后再操作。", 409)


@contextmanager
def saving(deck_id: int):
    with _guard:
        if deck_id in _busy:
            raise EditError("该演示已有写入或导出操作，请稍后重试。", 409)
        _busy.add(deck_id)
    try:
        yield
    finally:
        with _guard:
            _busy.discard(deck_id)


def version(source: str, css: str) -> str:
    return hashlib.sha256(source.encode() + b"\0" + css.encode()).hexdigest()


class SourceDocument(HTMLParser):
    """记录原文位置，只替换发生变化的标签或文字，保留其余脚本和格式。"""
    def __init__(self, source: str):
        super().__init__(convert_charrefs=False)
        self.source = source
        self.lines = [0]
        self.lines.extend(match.end() for match in re.finditer("\n", source))
        self.nodes: list[dict] = []
        self.stack: list[dict] = []
        self.feed(source)
        self.close()

    def source_offset(self):
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def handle_starttag(self, tag, attrs):
        start = self.source_offset()
        node = {"index": len(self.nodes), "tag": tag, "attrs": attrs,
                "start": start, "open_end": start + len(self.get_starttag_text()),
                "close_start": None, "end": None, "parent": self.stack[-1] if self.stack else None}
        self.nodes.append(node)
        if tag in VOID:
            node["close_start"] = node["end"] = node["open_end"]
        else:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        node = self.nodes[-1]
        if self.stack and self.stack[-1] is node:
            self.stack.pop()
        node["close_start"] = node["end"] = node["open_end"]

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                node = self.stack[index]
                node["close_start"] = self.source_offset()
                node["end"] = self.source.index(">", self.source_offset()) + 1
                del self.stack[index:]
                return

    @staticmethod
    def editable(node):
        attrs = dict(node["attrs"])
        return (node["tag"] not in NON_EDITABLE and node["end"] is not None
                and "slide" not in str(attrs.get("class") or "").split())

    def annotated(self):
        source = self.source
        for node in reversed(self.nodes):
            if self.editable(node):
                raw = source[node["start"]:node["open_end"]]
                end = raw.rfind("/>") if raw.rstrip().endswith("/>") else raw.rfind(">")
                raw = raw[:end] + f' data-mural-source="{node["index"]}" data-mural-source-tag="{node["tag"]}"' + raw[end:]
                source = source[:node["start"]] + raw + source[node["open_end"]:]
        return source


def _style_value(key, value):
    if key not in STYLE_KEYS or not isinstance(value, str) or len(value) > 500:
        raise EditError("不支持的页面样式修改。")
    if key in {"width", "height", "font-size"}:
        valid = re.fullmatch(r"\d+(?:\.\d+)?px", value) and 0 < float(value[:-2]) <= 20000
    elif key == "opacity":
        valid = re.fullmatch(r"\d+(?:\.\d+)?", value) and 0 <= float(value) <= 1
    elif key == "white-space":
        valid = value == "pre-wrap"
    elif key == "transform":
        valid = bool(re.fullmatch(r"(?:(?:translate(?:X|Y)?|rotate|scale(?:X|Y)?|matrix)\([-+\d.,\s%a-z]+\)\s*)+", value))
        valid = valid or value == "none"
    else:
        valid = bool(re.fullmatch(r"#[\da-fA-F]{6}|rgba?\([\d.,\s%]+\)", value))
    if not valid:
        raise EditError(f"无效样式值：{key}")
    return value


def _styles(existing: str, changes: dict) -> str:
    # 不拆开 url、引号或括号内的分号；只删除被本次修改替代的属性。
    pieces, start, quote, depth = [], 0, "", 0
    escaped = False
    for index, character in enumerate(existing + ";"):
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
        elif quote:
            if character == quote:
                quote = ""
        elif character in "\"'":
            quote = character
        elif character == "(":
            depth += 1
        elif character == ")":
            depth = max(0, depth - 1)
        elif character == ";" and not depth:
            piece = existing[start:index].strip()
            if piece and piece.partition(":")[0].strip().lower() not in changes:
                pieces.append(piece)
            start = index + 1
    pieces.extend(f"{key}: {_style_value(key, value)} !important" for key, value in changes.items())
    return "; ".join(pieces) + ";"


def _image(value):
    from PIL import Image
    if not isinstance(value, str) or len(value) > 17 * 1024 * 1024:
        raise EditError("替换图片过大。")
    match = re.fullmatch(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=\r\n]+)", value)
    if not match:
        raise EditError("替换图片必须是 PNG、JPEG 或 WebP 文件。")
    try:
        data = base64.b64decode(match[2], validate=True)
        if len(data) > 12 * 1024 * 1024:
            raise EditError("替换图片不能超过 12 MB。")
        with Image.open(io.BytesIO(data)) as image:
            if image.format.lower() != match[1] or image.width * image.height > 40_000_000:
                raise EditError("图片编码或尺寸不符合要求。")
            image.verify()
    except (ValueError, OSError, Image.DecompressionBombError) as exc:
        raise EditError("替换图片无法正确解码。") from exc
    return value


def apply_changes(source: str, changes: list) -> str:
    if not isinstance(changes, list) or len(changes) > 500:
        raise EditError("每次保存最多接受 500 项对象修改。")
    document = SourceDocument(source)
    replacements, seen = [], set()
    for change in changes:
        if not isinstance(change, dict) or set(change) - {"node", "styles", "text", "src", "delete"}:
            raise EditError("无效的页面修改参数。")
        number = change.get("node")
        if type(number) is not int or not 0 <= number < len(document.nodes) or number in seen:
            raise EditError("页面对象标识无效或重复。")
        seen.add(number)
        node = document.nodes[number]
        if not document.editable(node):
            raise EditError("不能修改脚本、页面容器或不完整的 HTML 节点。")
        if "delete" in change:
            if change["delete"] is not True or set(change) != {"node", "delete"}:
                raise EditError("删除对象不能同时附带其他修改。")
            replacements.append((node["start"], node["end"], ""))
            continue
        attrs = dict(node["attrs"])
        if "styles" in change:
            if not isinstance(change["styles"], dict):
                raise EditError("样式修改必须为对象。")
            attrs["style"] = _styles(attrs.get("style") or "", change["styles"])
        if "src" in change:
            if node["tag"] != "img":
                raise EditError("只能替换原 HTML 中的图片，不能替换图表快照。")
            attrs["src"] = _image(change["src"])
            attrs.pop("srcset", None)
            attrs.pop("sizes", None)
        if "styles" in change or "src" in change:
            rendered_attrs = "".join(f" {name}" if value is None else f' {name}="{escape(value, quote=True)}"' for name, value in attrs.items())
            closing = "/>" if source[node["start"]:node["open_end"]].rstrip().endswith("/>") else ">"
            replacements.append((node["start"], node["open_end"], f'<{node["tag"]}{rendered_attrs}{closing}'))
        if "text" in change:
            if (not isinstance(change["text"], str) or len(change["text"]) > 20000
                    or node["tag"] in VOID or any(item["parent"] is node and item["tag"] != "br" for item in document.nodes)):
                raise EditError("文字编辑只适用于纯文本对象；组合内请选中具体文字。")
            replacements.append((node["open_end"], node["close_start"], escape(change["text"])))
    replacements.sort()
    if any(a[1] > b[0] for a, b in zip(replacements, replacements[1:])):
        raise EditError("不能同时修改被删除对象及其子对象。")
    result = source
    for start, end, replacement in reversed(replacements):
        result = result[:start] + replacement + result[end:]
    return result


def source_payload(row, page: int):
    from .workspace_layout import WorkspaceLayout
    if row["status"] != "completed" or row["skill_version"] != "mural-presenter-v1":
        raise EditError("只能直接编辑已经制作完成的当前 v1 演示。", 409)
    if page < 1:
        raise EditError("页码必须大于零。")
    layout = WorkspaceLayout.load(row["run_dir"])
    source_path = layout.root / layout.paths["slide_pattern"].format(page=page)
    for path in (source_path, layout.root / layout.paths["base_css"]):
        relative = path.relative_to(layout.root)
        if any((layout.root / Path(*relative.parts[:index])).is_symlink() for index in range(1, len(relative.parts) + 1)):
            raise EditError("页面编辑不支持软链接源码。", 409)
    if not source_path.is_file():
        raise EditError("页面 HTML 不存在。", 404)
    source = source_path.read_text(encoding="utf-8")
    css = layout.path("base_css").read_text(encoding="utf-8")
    return layout, source, css


def run_save(row, page: int, payload: dict, user_id: int):
    from . import jobs
    layout, source, css = source_payload(row, page)
    if set(payload) != {"version", "changes"} or payload.get("version") != version(source, css):
        raise EditError("原页面或共享样式已改变，请重新打开编辑，避免覆盖较新的修改。", 409)
    apply_changes(source, payload["changes"])
    python, environment = jobs._offline_runtime(layout.root)
    environment["SKILL_COMMAND_TIMEOUT_S"] = "180"
    layout.path("runtime_tmp").mkdir(parents=True, exist_ok=True)
    request = {**payload, "workspace": str(layout.root), "page": page, "user_id": user_id}
    command = [python, str(Path(__file__).resolve()), "--save-page"]
    try:
        completed = subprocess.run(command, input=json.dumps(request, ensure_ascii=False),
                                   text=True, capture_output=True, env=environment, timeout=240)
    except subprocess.TimeoutExpired as exc:
        raise EditError("页面保存或渲染超时。请重新打开页面核对源码；未把旧截图标为更新成功。", 504) from exc
    try:
        result = json.loads(completed.stdout)
    except ValueError as exc:
        raise EditError("页面保存进程未返回完整结果。请重新打开页面核对已保存内容。", 502) from exc
    if not isinstance(result, dict):
        raise EditError("页面保存进程返回了无效结果。", 502)
    if not result.get("saved"):
        raise EditError(result.get("error") or "页面未保存。", result.get("status_code", 500))
    return result


def _write(path: Path, content: bytes):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".page-edit-", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            os.chmod(temporary, 0o644)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def save_and_render(request: dict):
    from runtime.production import ProductionSettings, ResourceSlots, SkillRenderer, SkillSnapshot
    from state.artifacts import ArtifactStore
    from state.workspace import WorkspaceState, file_sha256

    root, page = Path(request["workspace"]).resolve(), int(request["page"])
    workspace = WorkspaceState(root)
    artifacts = ArtifactStore(root, workspace_state=workspace, create=False)
    settings = ProductionSettings.from_environment()
    # 必须已有固定引用；人工编辑不能悄悄为旧工作区重新选择 Skill。
    if not workspace.paths.skill_ref.is_file():
        raise EditError("工作区缺少固定 Skill 引用，尚未写入修改。", 409)
    snapshot = SkillSnapshot.stage(root, settings.skill_source, os.environ)
    renderer = SkillRenderer(root, settings, os.environ, ResourceSlots(root, settings), snapshot)
    saved = False
    try:
        with workspace._lock:
            html_path, css_path = workspace.paths.page_html(page), workspace.paths.path("base_css")
            source, css = html_path.read_text(), css_path.read_text()
            if request["version"] != version(source, css):
                raise EditError("原页面或共享样式已改变，请重新打开编辑。", 409)
            updated = apply_changes(source, request["changes"])
            png_path = workspace.paths.page_png(page)
            if updated != source:
                record = artifacts.artifact(f"page-html-{page:02d}")
                revision = "manual-" + uuid.uuid4().hex
                archive = root / "_trace/revisions" / revision
                archive.mkdir(parents=True)
                _write(archive / html_path.name, source.encode())
                if png_path.is_file():
                    _write(archive / png_path.name, png_path.read_bytes())
                tx = artifacts.begin_transaction(record.owner_id, [record.artifact_id])
                try:
                    _write(html_path, updated.encode())
                    artifacts.commit(owner_id=record.owner_id, artifact_ids=[record.artifact_id],
                        source_hashes={record.path: file_sha256(html_path),
                                       **{path: file_sha256(root / path) for path in record.source_paths}}, transaction_id=tx)
                except Exception:
                    _write(html_path, source.encode())
                    artifacts.abort_transaction(tx, owner_id=record.owner_id)
                    raise
                saved = True
                state = workspace._read_state()
                workspace._append_event(state, "page_manually_edited", page,
                    user_id=request["user_id"], revision=revision,
                    before_html_sha256=hashlib.sha256(source.encode()).hexdigest(),
                    html_sha256=file_sha256(html_path))
                workspace._write_state(state)
            saved = True
        artifacts.render_pages([page], renderer)
        artifacts.validate_render_receipt(page)
        return {"saved": True, "rendered": True, "page": page,
                "html_sha256": file_sha256(html_path), "png_sha256": file_sha256(png_path),
                "stamp": time.time_ns() // 1_000_000, "review_required": True}
    except Exception as exc:
        return {"saved": saved, "rendered": False, "page": page,
                "error": f"{type(exc).__name__}: {exc}",
                "status_code": getattr(exc, "status_code", 500)}
    finally:
        renderer.close()


if __name__ == "__main__":
    from contextlib import redirect_stdout
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "harnesses/mural-presenter-v1"))
    try:
        request = json.loads(sys.stdin.read(MAX_REQUEST_BYTES + 1))
        with redirect_stdout(sys.stderr):
            result = save_and_render(request)
    except Exception as exc:
        result = {"saved": False, "rendered": False, "error": f"{type(exc).__name__}: {exc}",
                  "status_code": getattr(exc, "status_code", 500)}
    print(json.dumps(result, ensure_ascii=False))
