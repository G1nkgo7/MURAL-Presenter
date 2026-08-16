#!/usr/bin/env python3
"""Convert the controlled Markdown chapter drafts into review-draft LaTeX."""

from __future__ import annotations

import argparse
import html
import re
from pathlib import Path
from typing import Any

import mistune


ROOT = Path(__file__).resolve().parents[1]

CHAPTERS = [
    "01_introduction_related",
    "02_method",
    "03_benchmark_experiments",
    "04_discussion_conclusion",
]

PROTECTED = re.compile(
    r"(\\begin\{table\}.*?\\end\{table\}|"
    r"\\begin\{figure\}.*?\\end\{figure\}|"
    r"\\\[.*?\\\]|\\\(.*?\\\)|\\cite[pt]?\{[^}]+\}|"
    r"\\(?:ref|autoref|input|url|href)\{[^}]+\}|\\textsc\{[^}]+\}|\\textbf\{[^}]+\}|\\texttt\{[^}]+\}|\\emph\{[^}]+\}|\\resulttbd\{[^}]+\}|"
    r"\\textasciitilde(?:\{\})?|\\ |\d{1,3}(?:\{,\}\d{3})+)",
    flags=re.DOTALL,
)


def protect_tex(text: str) -> tuple[str, dict[str, str]]:
    """Hide TeX spans from Mistune and restore them after rendering.

    Without this preprocessing, Mistune consumes the backslashes in math
    delimiters and treats underscores inside formulas as Markdown emphasis.
    Alphanumeric sentinels survive both the block and inline parsers unchanged.
    """
    protected: dict[str, str] = {}

    def stash(match: re.Match[str]) -> str:
        token = f"TEXPROTECTEDTOKEN{len(protected):04d}END"
        protected[token] = match.group(0)
        return token

    return PROTECTED.sub(stash, text), protected


def escape_plain(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        # Reviewed chapter sources use bare ``~`` as TeX's non-breaking
        # interword space (e.g. ``Figure~\ref{...}``). A literal tilde must be
        # written explicitly as ``\textasciitilde{}``, which is protected
        # above. Escaping every bare tilde made the glyph visible in the PDF.
        "~": "~",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(char, char) for char in text)


def escape_with_math(text: str) -> str:
    pieces: list[str] = []
    cursor = 0
    for match in PROTECTED.finditer(text):
        pieces.append(escape_plain(text[cursor : match.start()]))
        pieces.append(match.group(0))
        cursor = match.end()
    pieces.append(escape_plain(text[cursor:]))
    return "".join(pieces)


def render_inline(nodes: list[dict[str, Any]]) -> str:
    output: list[str] = []
    for node in nodes:
        kind = node["type"]
        if kind == "text":
            output.append(escape_with_math(node.get("raw", "")))
        elif kind == "codespan":
            raw = html.unescape(node.get("raw", ""))
            output.append(r"\texttt{" + escape_plain(raw) + "}")
        elif kind in {"strong", "emphasis"}:
            command = "textbf" if kind == "strong" else "emph"
            output.append(
                rf"\{command}" + "{" + render_inline(node.get("children", [])) + "}"
            )
        elif kind == "link":
            label = render_inline(node.get("children", []))
            url = node.get("attrs", {}).get("url", "")
            output.append(r"\href{" + url + "}{" + label + "}")
        elif kind in {"softbreak", "linebreak"}:
            output.append(" ")
        elif "children" in node:
            output.append(render_inline(node["children"]))
        else:
            output.append(escape_with_math(node.get("raw", "")))
    return "".join(output).strip()


def heading_title(node: dict[str, Any]) -> str:
    title = render_inline(node.get("children", []))
    title = re.sub(r"^\d+(?:\.\d+)*\.?\s*", "", title)
    return title.strip()


def render_table(node: dict[str, Any]) -> str:
    head = next(child for child in node["children"] if child["type"] == "table_head")
    body = next(child for child in node["children"] if child["type"] == "table_body")
    header_cells = head["children"]
    columns = len(header_cells)

    def row(cells: list[dict[str, Any]]) -> str:
        return " & ".join(render_inline(cell.get("children", [])) for cell in cells) + r" \\"

    if columns == 2:
        begin = (
            "\\begin{center}\n\\small\n"
            "\\begin{tabularx}{\\linewidth}{>{\\raggedright\\arraybackslash}"
            "p{0.24\\linewidth}X}\n"
        )
        end = "\\end{tabularx}\n\\end{center}\n"
    else:
        begin = (
            "\\begin{center}\n\\scriptsize\n\\resizebox{\\linewidth}{!}{%\n"
            "\\begin{tabular}{" + "l" * columns + "}\n"
        )
        end = "\\end{tabular}}\n\\end{center}\n"

    lines = [begin, "\\toprule", row(header_cells), "\\midrule"]
    for table_row in body.get("children", []):
        lines.append(row(table_row.get("children", [])))
    lines.extend(["\\bottomrule", end])
    return "\n".join(lines)


def render_list(node: dict[str, Any]) -> str:
    ordered = bool(node.get("attrs", {}).get("ordered"))
    environment = "enumerate" if ordered else "itemize"
    lines = [rf"\begin{{{environment}}}"]
    for item in node.get("children", []):
        chunks: list[str] = []
        for child in item.get("children", []):
            if child["type"] in {"block_text", "paragraph"}:
                chunks.append(render_inline(child.get("children", [])))
            elif child["type"] == "list":
                chunks.append(render_list(child))
        lines.append(r"\item " + "\n".join(chunks))
    lines.append(rf"\end{{{environment}}}")
    return "\n".join(lines)


def convert(source: Path) -> str:
    markdown = mistune.create_markdown(renderer="ast", plugins=["table"])
    source_text, protected = protect_tex(source.read_text(encoding="utf-8"))
    ast = markdown(source_text)
    output: list[str] = [
        "% Generated mechanically from the reviewed Markdown source.",
        "% Controller edits should be made in both source and LaTeX when material.",
        "",
    ]
    abstract_open = False

    for node in ast:
        kind = node["type"]
        if kind == "blank_line":
            continue
        if kind == "heading":
            level = int(node.get("attrs", {}).get("level", 1))
            title = heading_title(node)
            if source.stem == "01_introduction_related" and level == 1:
                # The front-matter Markdown keeps the paper title as its first
                # heading.  LaTeX already receives that title from main_zh.tex.
                continue
            if abstract_open:
                output.extend([r"\end{abstract}", ""])
                abstract_open = False
            if title.lower() == "abstract" or title == "摘要":
                output.extend([r"\begin{abstract}", ""])
                abstract_open = True
                continue
            if source.stem == "01_introduction_related":
                level -= 1
            command = {1: "section", 2: "subsection", 3: "subsubsection"}.get(
                level, "paragraph"
            )
            output.extend([rf"\{command}{{{title}}}", ""])
        elif kind == "paragraph":
            output.extend([render_inline(node.get("children", [])), ""])
        elif kind == "table":
            output.extend([render_table(node), ""])
        elif kind == "list":
            output.extend([render_list(node), ""])
        elif kind == "block_quote":
            quote = "".join(
                render_inline(child.get("children", []))
                for child in node.get("children", [])
            )
            output.extend([r"\begin{quote}", quote, r"\end{quote}", ""])
        elif kind == "thematic_break":
            output.extend([r"\medskip\hrule\medskip", ""])
        else:
            raw = node.get("raw", "")
            if raw:
                output.extend([escape_with_math(raw), ""])

    if abstract_open:
        output.extend([r"\end{abstract}", ""])
    rendered = "\n".join(output).rstrip() + "\n"
    rendered = re.sub(
        r"(?<=[\u3400-\u9fff，。；：、！？）】》]) +"
        r"(?=[\u3400-\u9fff，。；：、！？（【《])",
        "",
        rendered,
    )
    for token, original in protected.items():
        rendered = rendered.replace(token, original)
    return rendered


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Project the reviewed Markdown chapters into LaTeX."
    )
    parser.add_argument(
        "--lang",
        choices=("zh", "en"),
        default="zh",
        help="chapter edition to convert (default: zh)",
    )
    args = parser.parse_args()
    source_dir = ROOT / ("chapters_en" if args.lang == "en" else "chapters")
    output_dir = ROOT / "manuscript" / (
        "chapters_en" if args.lang == "en" else "chapters"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    for name in CHAPTERS:
        source = source_dir / f"{name}.md"
        if not source.is_file():
            raise SystemExit(f"missing source chapter: {source}")
        output = output_dir / f"{name}.tex"
        rendered = convert(source)
        if args.lang == "en":
            rendered = rendered.replace(
                r"\input{figures/page_topologies}",
                r"\input{figures/page_topologies_en}",
            ).replace(
                r"\input{figures/release_workflow}",
                r"\input{figures/release_workflow_en}",
            )
        output.write_text(rendered, encoding="utf-8")
        print(f"{source.name} -> {output.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
