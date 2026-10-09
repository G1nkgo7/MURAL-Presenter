"""读取共享 deck.md 与独立分页作者计划；只编译意图，不生成同步镜像。"""
from __future__ import annotations

from pathlib import Path
import re


def headings(text: str) -> list[tuple[int, int, int, str]]:
    """忽略 fenced code 中的标题，保留结构编辑所需的原始字符位置。"""
    result = []
    offset = 0
    fence = ""
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if not fence:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = ""
        elif not fence:
            match = re.match(r"^(#{1,6})[ \t]+(.+?)[ \t]*\r?\n?$", line)
            if match:
                result.append((offset, offset + len(line), len(match.group(1)), match.group(2)))
        offset += len(line)
    return result


def speech_section(text: str) -> str:
    rows = headings(text)
    matches = [(index, end) for index, (_, end, level, title) in enumerate(rows)
               if level == 2 and title.casefold() in {"讲稿", "speaker notes"}]
    if len(matches) > 1:
        raise ValueError("brief must contain at most one ## 讲稿 or ## Speaker notes section")
    if not matches:
        return ""
    index, end = matches[0]
    stop = next((start for start, _, level, _ in rows[index + 1:] if level <= 2), len(text))
    return text[end:stop].strip()


def _read_shared(root: Path) -> str:
    return (root / "deck.md").read_text(encoding="utf-8").rstrip()


def _read_brief(root: Path, path: Path) -> dict:
    filename = re.fullmatch(r"slide_(\d{2,})\.md", path.name)
    if not filename or int(filename.group(1)) < 1 or path.name != f"slide_{int(filename.group(1)):02d}.md":
        raise ValueError(f"{path.name}: expected a canonical slide_NN.md filename")
    number = int(filename.group(1))
    source = path.read_text(encoding="utf-8")
    rows = headings(source)
    title_row = rows[0] if rows else None
    if not title_row or title_row[2] != 1 or any(row[2] == 1 for row in rows[1:]):
        raise ValueError(f"{path.name}: expected one # Page title heading")
    if source[:title_row[0]].strip():
        raise ValueError(f"{path.name}: page heading must precede its content")
    body = source[title_row[1]:].strip()
    return {"number": number, "title": title_row[3], "text": source.strip(),
            "body": body, "speech": speech_section(source),
            "path": path.relative_to(root).as_posix(), "section": title_row[3]}


def read_deck(root: Path, expected: int | None = None, *, pages: tuple[int, ...] | None = None) -> tuple[str, list[dict]]:
    shared = _read_shared(root)
    if pages is not None:
        if not pages or any(type(number) is not int or number < 1 for number in pages):
            raise ValueError("selected pages must be positive page numbers")
        if expected is not None and any(number > expected for number in pages):
            raise ValueError(f"selected pages exceed expected page count {expected}")
        return shared, [_read_brief(root, root / "briefs" / f"slide_{number:02d}.md")
                        for number in sorted(set(pages))]
    briefs = [path for path in sorted((root / "briefs").glob("*.md"))
              if re.fullmatch(r"slide_\d{2,}\.md", path.name)
              or path.read_text(encoding="utf-8").strip()]
    if not briefs:
        raise ValueError("deck.md needs separate briefs/slide_NN.md files")
    pages = [_read_brief(root, path) for path in briefs]
    pages.sort(key=lambda page: page["number"])
    numbers = [page["number"] for page in pages]
    if numbers != list(range(1, len(numbers) + 1)):
        raise ValueError(f"briefs: filenames must be unique and contiguous from slide_01.md; found {numbers}")
    if expected is not None and len(pages) != expected:
        raise ValueError(f"deck.md: expected {expected} pages, found {len(pages)}")
    return shared, pages


def owner_context(root: Path, pages: tuple[int, ...] | None) -> dict:
    """None 用于整册诊断；明确页集只读其 brief，空集只读共享方向。"""
    if pages is None:
        shared, selected = read_deck(root)
    else:
        shared = _read_shared(root)
        selected = []
        for number in sorted(set(pages)):
            path = root / "briefs" / f"slide_{number:02d}.md"
            if not path.is_file():
                raise ValueError(f"deck.md has no assigned page {number}: {path.name}")
            selected.append(_read_brief(root, path))
    return {"source": "deck.md", "shared": shared,
            "pages": [{"number": row["number"], "source": row["path"], "section": row["section"],
                       "text": row["text"]} for row in selected]}
