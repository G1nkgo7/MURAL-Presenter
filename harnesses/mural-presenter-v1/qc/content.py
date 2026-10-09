"""编译 raw query、来源材料、当前页面与讲稿供内容 Reviewer 核对。

Harness 只做可见文本提取和来源哈希，不通过关键词或 Query 正则替 Reviewer 判断内容是否
满足用户意图。Reviewer 的结构化 finding 才进入 Issue Ledger。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import copy
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.parse import unquote, urlsplit

from runtime.config import RunSpec
from state.workspace import WorkspaceState, file_sha256
from state.artifacts import _resolve_html_resource


CONTENT_SEVERITIES = frozenset({"advisory", "minor", "major", "critical"})
CONTENT_OWNER_HINTS = frozenset({"slide", "image", "orchestrator", "none"})


@dataclass(frozen=True)
class SourceDocument:
    """保存一份材料或研究来源的路径、哈希与文本。"""

    path: str
    sha256: str
    text: str

    def to_dict(self) -> dict[str, object]:
        """把来源文档转换为 Reviewer 输入对象。"""
        return {"path": self.path, "sha256": self.sha256, "text": self.text}


@dataclass(frozen=True)
class VisiblePageText:
    """保存一页最终 HTML 的当前哈希和可见文本。"""

    page: int
    html_sha256: str
    text: str
    rendered_text: tuple[str, ...] | None = None
    used_images: tuple[Mapping[str, object], ...] | None = None
    png_sha256: str = ""

    def to_dict(self) -> dict[str, object]:
        """把页面可见文本转换为 Reviewer 输入对象。"""
        return {
            "page": self.page,
            "html_sha256": self.html_sha256,
            "text": self.text,
            "rendered_text": list(self.rendered_text) if self.rendered_text is not None else None,
            "used_images": [dict(row) for row in self.used_images] if self.used_images is not None else None,
            "png_sha256": self.png_sha256,
        }


@dataclass(frozen=True)
class ContentPacket:
    """表示 Content Reviewer 唯一需要的短事实包。"""

    raw_user_query: str
    sources: tuple[SourceDocument, ...]
    pages: tuple[VisiblePageText, ...]
    language: str
    authored_plans: tuple[SourceDocument, ...] = ()
    derived_speech: SourceDocument | None = None
    visual_evidence: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        """冻结来源与页面序列并确认原始 Query 非空。"""
        if not self.raw_user_query.strip():
            raise ValueError("content packet raw query must not be empty")
        object.__setattr__(self, "sources", tuple(self.sources))
        object.__setattr__(self, "pages", tuple(self.pages))
        object.__setattr__(self, "authored_plans", tuple(self.authored_plans))
        object.__setattr__(self, "visual_evidence", tuple(copy.deepcopy(dict(row)) for row in self.visual_evidence))

    def to_dict(self) -> dict[str, object]:
        """把内容事实包转换为结构化 Reviewer 输入。"""
        return {
            "raw_user_query": self.raw_user_query,
            "language": self.language,
            "sources": [item.to_dict() for item in self.sources],
            "pages": [item.to_dict() for item in self.pages],
            "authored_plans": [item.to_dict() for item in self.authored_plans],
            "derived_speech": self.derived_speech.to_dict() if self.derived_speech is not None else None,
            "visual_evidence": copy.deepcopy(list(self.visual_evidence)),
        }


@dataclass(frozen=True)
class ContentFinding:
    """保存 Content Reviewer 返回的一条可路由 finding。"""

    finding_id: str
    kind: str
    severity: str
    pages: tuple[int, ...]
    summary: str
    owner_hint: str
    objective: bool = False
    evidence: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """规范 finding 的严重度、页码、路由提示与证据。"""
        severity = str(self.severity).strip().lower()
        owner_hint = str(self.owner_hint).strip().lower()
        if severity not in CONTENT_SEVERITIES:
            raise ValueError(f"unsupported content severity {severity!r}")
        if owner_hint not in CONTENT_OWNER_HINTS:
            raise ValueError(f"unsupported content owner hint {owner_hint!r}")
        if not str(self.kind).strip() or not str(self.summary).strip():
            raise ValueError("content finding kind and summary must not be empty")
        object.__setattr__(self, "severity", severity)
        object.__setattr__(self, "owner_hint", owner_hint)
        object.__setattr__(self, "pages", tuple(sorted(set(int(page) for page in self.pages))))
        object.__setattr__(self, "evidence", copy.deepcopy(dict(self.evidence or {})))

    def to_dict(self) -> dict[str, object]:
        """把 finding 转换为 Issue Ledger 输入。"""
        return {
            "finding_id": self.finding_id,
            "kind": self.kind,
            "severity": self.severity,
            "pages": list(self.pages),
            "summary": self.summary,
            "owner_hint": self.owner_hint,
            "objective": self.objective,
            "evidence": copy.deepcopy(dict(self.evidence)),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ContentFinding":
        """从 Reviewer 对象恢复并校验 finding。"""
        pages = value.get("pages") or []
        evidence = value.get("evidence") or {}
        if not isinstance(pages, list) or not isinstance(evidence, dict):
            raise ValueError("content finding pages or evidence are invalid")
        finding_id = str(value.get("finding_id") or "") or _finding_id(value)
        return cls(
            finding_id=finding_id,
            kind=str(value.get("kind") or ""),
            severity=str(value.get("severity") or ""),
            pages=tuple(int(page) for page in pages),
            summary=str(value.get("summary") or ""),
            owner_hint=str(value.get("owner_hint") or "none"),
            objective=bool(value.get("objective")),
            evidence=evidence,
        )


ContentReviewer = Callable[[ContentPacket], Sequence[Mapping[str, object]]]


class ContentQC:
    """提取机械事实，并把语义覆盖判断交给 Content Reviewer。"""

    @staticmethod
    def build_packet(
        run_spec: RunSpec,
        workspace: WorkspaceState,
        *,
        pages: Sequence[int],
        source_paths: Sequence[str | Path] = (),
        render_receipts: Mapping[int, Mapping[str, object]] | None = None,
        visual_evidence: Sequence[Mapping[str, object]] = (),
    ) -> ContentPacket:
        """由原始输入、来源文件和当前 HTML 编译事实包。"""
        def document(raw_path: str | Path) -> SourceDocument:
            path = workspace.paths.resolve(raw_path)
            if not path.is_file():
                raise FileNotFoundError(f"content QC source is missing: {path}")
            data = path.read_bytes()
            return SourceDocument(
                path=workspace.paths.relative(path),
                sha256=hashlib.sha256(data).hexdigest(),
                text=data.decode("utf-8"),
            )
        sources = [document(path) for path in source_paths]
        selected_pages = tuple(sorted(set(int(item) for item in pages)))
        # 作者原文单独送审，不充当事实来源；保留完整讲稿及自然段，不另建 Markdown 解析器。
        plan_paths = [workspace.paths.deck, *(workspace.paths.resolve(f"briefs/slide_{page:02d}.md")
                                            for page in selected_pages)]
        authored_plans = tuple(document(path) for path in plan_paths if path.is_file())
        speech_path = workspace.paths.path("speech")
        speech = document(speech_path) if speech_path.is_file() else None
        visible_pages: list[VisiblePageText] = []
        crop_path = workspace.paths.trace / "material-figures.json"
        crops = json.loads(crop_path.read_text(encoding="utf-8")).get("assets", {}) if crop_path.is_file() else {}
        for page in selected_pages:
            html_path = workspace.paths.page_html(page)
            if not html_path.is_file():
                raise FileNotFoundError(f"content QC page HTML is missing: {html_path}")
            receipt = (render_receipts or {}).get(page, {})
            if receipt and receipt.get("html_sha256") != file_sha256(html_path):
                raise ValueError(f"content QC render receipt is stale: page {page}")
            png_path = workspace.paths.page_png(page)
            png_sha256 = file_sha256(png_path) if png_path.is_file() else ""
            if receipt.get("png_sha256") and receipt["png_sha256"] != png_sha256:
                raise ValueError(f"content QC render receipt PNG is stale: page {page}")
            diagnostics = receipt.get("diagnostics", {})
            images = None
            if "visible_images" in diagnostics:
                images = {}
                for row in diagnostics["visible_images"]:
                    source = str(row.get("src") or "")
                    split = urlsplit(source)
                    if split.scheme == "file":
                        path = Path(unquote(split.path)).resolve()
                        if not path.is_relative_to(workspace.paths.root):
                            raise ValueError("rendered image escapes workspace")
                    else:
                        path = _resolve_html_resource(workspace.paths.root, html_path, source)
                    if path is None:
                        continue
                    relative = workspace.paths.relative(path)
                    digest = file_sha256(path)
                    image = {"path": relative, "sha256": digest}
                    crop = crops.get(relative, {})
                    if crop and crop.get("sha256") == digest:
                        image.update(source_document=crop.get("source_document", ""),
                                     source_page=crop.get("source_page"),
                                     source=crop.get("source", ""))
                    image = images.setdefault(relative, image)
                    instance = {key: row[key] for key in ("alt", "x", "y", "width", "height") if key in row}
                    if instance:
                        image.setdefault("rendered_instances", []).append(instance)
            visible_pages.append(
                VisiblePageText(
                    page=page,
                    html_sha256=file_sha256(html_path),
                    text=extract_visible_text(html_path.read_text(encoding="utf-8")),
                    rendered_text=tuple(diagnostics["rendered_text"]) if "rendered_text" in diagnostics else None,
                    used_images=tuple(images.values()) if images is not None else None,
                    png_sha256=png_sha256,
                )
            )
        return ContentPacket(
            raw_user_query=run_spec.raw_user_query,
            sources=tuple(sources),
            pages=tuple(visible_pages),
            language=workspace.creation_config(require_complete=True)["presentation_language"],
            authored_plans=authored_plans,
            derived_speech=speech,
            visual_evidence=tuple(visual_evidence),
        )

    @staticmethod
    def review(
        packet: ContentPacket, reviewer: ContentReviewer
    ) -> tuple[ContentFinding, ...]:
        """调用 Reviewer 一次，并校验其全部结构化 findings。"""
        raw_findings = reviewer(packet)
        if isinstance(raw_findings, (str, bytes)) or not isinstance(raw_findings, Sequence):
            raise ValueError("content reviewer must return an array of findings")
        findings: list[ContentFinding] = []
        for value in raw_findings:
            if not isinstance(value, Mapping):
                raise ValueError("content reviewer findings must be objects")
            finding = ContentFinding.from_dict(value)
            if set(finding.pages) - {page.page for page in packet.pages}:
                raise ValueError("content finding refers to pages outside its packet")
            findings.append(finding)
        return tuple(findings)


class _VisibleTextParser(HTMLParser):
    """提取静态文本并剔除明确不渲染的区域；不替代浏览器 CSS/像素判断。"""

    def __init__(self) -> None:
        """初始化隐藏标签深度和文本片段。"""
        super().__init__(convert_charrefs=True)
        self.hidden_depth = 0
        self.fragments: list[str] = []
        self.open_tags: list[tuple[str, bool]] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        """进入脚本、样式、模板或显式隐藏区域时暂停收集。"""
        attributes = {str(key).lower(): str(value or "") for key, value in attrs}
        normalized = str(tag).lower()
        hidden = self.hidden_depth > 0 or normalized in {
            "script",
            "style",
            "template",
            "noscript",
        } or (
            "hidden" in attributes or _inline_display_none(attributes.get("style", ""))
        )
        void_tags = {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }
        if normalized not in void_tags:
            self.open_tags.append((normalized, hidden))
            if hidden:
                self.hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        """离开隐藏标签时恢复文本收集。"""
        normalized = str(tag).lower()
        while self.open_tags:
            open_tag, hidden = self.open_tags.pop()
            if hidden:
                self.hidden_depth = max(0, self.hidden_depth - 1)
            if open_tag == normalized:
                break

    def handle_data(self, data: str) -> None:
        """收集非隐藏区域中的非空文本。"""
        text = " ".join(str(data).split())
        if self.hidden_depth == 0 and text:
            self.fragments.append(text)


def _inline_display_none(style: str) -> bool:
    """只处理显式 inline display；aria-hidden 是辅助技术属性，不隐藏像素。"""
    display, important = "", False
    for declaration in str(style).split(";"):
        name, separator, value = declaration.partition(":")
        if not separator or name.strip().lower() != "display":
            continue
        value, priority_separator, priority = value.lower().partition("!")
        if priority_separator and priority.strip() != "important":
            continue
        priority_set = bool(priority_separator)
        if not important or priority_set:
            display, important = value.strip(), priority_set
    return display == "none"


def extract_visible_text(html: str) -> str:
    """机械提取静态文字；不把辅助技术隐藏误当视觉隐藏，不计算外部 CSS。"""
    parser = _VisibleTextParser()
    parser.feed(str(html))
    return " ".join(parser.fragments)


def _finding_id(value: Mapping[str, object]) -> str:
    """由 finding 内容生成稳定 ID。"""
    payload = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, default=str)
    return f"finding-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:20]}"
