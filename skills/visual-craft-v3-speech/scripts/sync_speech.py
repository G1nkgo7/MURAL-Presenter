#!/usr/bin/env python3
"""Build speech.md from the audience-facing scripts in plan/slide_NN.md."""

from __future__ import annotations

import argparse
import os
import re
import tempfile
from pathlib import Path


SPEECH_HEADINGS = {
    "口语讲稿",
    "讲述内容",
    "讲稿",
    "口播",
    "spoken script",
    "speaker script",
    "speech",
    "talk track",
}
SOURCE_HEADINGS = {
    "来源",
    "参考资料",
    "参考资料（不朗读）",
    "参考资料(不朗读)",
    "sources",
    "sources (not spoken)",
    "references",
    "provenance",
}


def normalize_heading(value: str) -> str:
    value = re.sub(r"\s+", " ", value.strip().lower())
    return value.rstrip(":：")


def sections(text: str) -> dict[str, str]:
    matches = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", text))
    result: dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        result[normalize_heading(match.group(1))] = text[start:end].strip()
    return result


def first_section(parts: dict[str, str], aliases: set[str]) -> str:
    normalized = {normalize_heading(alias) for alias in aliases}
    for heading, body in parts.items():
        if heading in normalized:
            return body.strip()
    return ""


def clean_inline(value: str) -> str:
    value = value.strip().strip("` ")
    value = re.sub(r"^[-*+]\s+", "", value)
    return value.strip()


def plan_title(text: str) -> str:
    match = re.search(
        r"(?mi)^[ \t]*[-*+][ \t]*(?:\*\*)?"
        r"(?:title|标题|主标题|章节名|幕名)"
        r"(?:\*\*)?(?:[ \t]*(?:\([^\r\n)]*\)|（[^\r\n）]*）))?"
        r"[ \t]*[:：][ \t]*([^\r\n]+?)[ \t]*$",
        text,
    )
    if match:
        candidate = clean_inline(match.group(1))
        if candidate:
            return candidate
    raise ValueError(
        "missing canonical audience title; add one non-empty "
        "`- 标题：...` or `- title: ...` line. "
        "Do not use the page summary or a production description as the title."
    )


def infer_language(deck_text: str, plan_texts: list[str]) -> str:
    match = re.search(
        r"(?mi)^\s*(?:[-*+]\s*)?(?:language|语言)\s*[:：]\s*(zh|en|中文|英文)\s*$",
        deck_text,
    )
    if match:
        return "zh" if match.group(1).lower() in {"zh", "中文"} else "en"
    sample = "\n".join(plan_texts)
    return "zh" if re.search(r"[\u3400-\u9fff]", sample) else "en"


def plan_files(root: Path, expected: int | None) -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    for path in (root / "plan").glob("slide_*.md"):
        match = re.fullmatch(r"slide_(\d+)\.md", path.name)
        if match:
            found.append((int(match.group(1)), path))
    found.sort()
    if not found:
        raise ValueError("no plan/slide_NN.md files found")
    numbers = [number for number, _ in found]
    wanted = list(range(1, (expected or len(found)) + 1))
    if numbers != wanted:
        raise ValueError(f"plan pages must be continuous: expected {wanted}, got {numbers}")
    return found


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def build(root: Path, expected: int | None) -> None:
    files = plan_files(root, expected)
    texts = [path.read_text(encoding="utf-8") for _, path in files]
    deck_path = root / "plan" / "deck.md"
    deck_text = deck_path.read_text(encoding="utf-8") if deck_path.exists() else ""
    language = infer_language(deck_text, texts)
    rows: list[str] = []
    errors: list[str] = []

    for (number, path), text in zip(files, texts):
        try:
            title = plan_title(text)
            parts = sections(text)
            speech = first_section(parts, SPEECH_HEADINGS)
            if not speech:
                raise ValueError("missing `## 口语讲稿` / `## Spoken script`")
            sources = first_section(parts, SOURCE_HEADINGS)
            if language == "zh":
                heading = f"# 第 {number:02d} 页｜{title}"
                script_heading = "## 讲述内容"
                sources_heading = "## 参考资料（不朗读）"
                sources = sources or "- 本页无外部引用。"
            else:
                heading = f"# Slide {number:02d} | {title}"
                script_heading = "## Spoken script"
                sources_heading = "## Sources (not spoken)"
                sources = sources or "- No external citations."
            rows.extend(
                [heading, "", script_heading, "", speech, "", sources_heading, "", sources, ""]
            )
        except ValueError as exc:
            errors.append(f"{path.name}: {exc}")

    if errors:
        raise ValueError("\n".join(errors))
    atomic_write(root / "speech.md", "\n".join(rows).rstrip() + "\n")
    print("status:PASS")
    print(f"speech:{len(files)} pages language:{language}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--expected", type=int)
    args = parser.parse_args()
    try:
        build(Path(args.root).resolve(), args.expected)
    except (OSError, ValueError) as exc:
        print("status:FAIL")
        print(exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
