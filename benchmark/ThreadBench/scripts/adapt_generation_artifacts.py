#!/usr/bin/env python3
"""Export a generation run with narrowly bounded artifact adaptation.

Allowed changes are deliberately limited to:
- wrap page-specific HTML fragments as standalone HTML documents;
- split one self-contained multi-page HTML deck into page documents;
- normalize page HTML/PNG filenames to slide_NN.html / slide_NN.png;
- render a PNG only when the matching PNG is missing or invalid.

The source run is never modified, and its copied artifacts are retained in the
export. Repairs add or update only the canonical slide_NN.html / slide_NN.png
targets needed by downstream consumers. This adapter never fits a canvas,
changes model-authored page content, truncates pages, or invents/fills missing
pages.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
CANONICAL_HTML_RE = re.compile(r"^slide_([0-9]{2})\.html$")
CANONICAL_PNG_RE = re.compile(r"^slide_([0-9]{2})\.png$")
PAGE_PNG_RE = re.compile(r"^(?:slide|page)[_-]([0-9]+)\.png$", re.IGNORECASE)
NUMBER_RE = re.compile(r"([0-9]+)")
VOID_TAGS = {
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


class AdaptationError(RuntimeError):
    """Raised when the requested bounded adaptation cannot be performed."""


@dataclass(frozen=True)
class PageSource:
    output_index: int
    source_index: int | None
    source_path: Path
    source_relpath: str
    html: str
    operation: str


def nonempty(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def is_full_html(source: str) -> bool:
    prefix = source[:4096].lower()
    return "<html" in prefix or "<!doctype html" in prefix


def valid_png(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size <= len(PNG_SIGNATURE):
        return False
    with path.open("rb") as handle:
        return handle.read(len(PNG_SIGNATURE)) == PNG_SIGNATURE


def page_number(path: Path) -> int | None:
    matches = NUMBER_RE.findall(path.stem)
    if not matches:
        return None
    value = int(matches[-1])
    return value if value > 0 else None


def relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


class DeckParts(HTMLParser):
    """Collect the head body and top-level elements marked as slides."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.in_head = False
        self.head_parts: list[str] = []
        self.capture: list[str] | None = None
        self.capture_depth = 0
        self.slides: list[str] = []

    @staticmethod
    def _is_slide(attrs: list[tuple[str, str | None]]) -> bool:
        classes = next(
            (value for key, value in attrs if key.lower() == "class"),
            None,
        )
        has_class = bool(classes and "slide" in classes.split())
        has_data_slide = any(key.lower() == "data-slide" for key, _ in attrs)
        return has_class or has_data_slide

    def _append(self, value: str) -> None:
        if self.capture is not None:
            self.capture.append(value)
        elif self.in_head:
            self.head_parts.append(value)

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        lower = tag.lower()
        raw = self.get_starttag_text()
        if lower == "head" and self.capture is None:
            self.in_head = True
            return
        if self.capture is None and not self.in_head and self._is_slide(attrs):
            self.capture = [raw]
            self.capture_depth = 0 if lower in VOID_TAGS else 1
            if self.capture_depth == 0:
                self.slides.append("".join(self.capture))
                self.capture = None
            return
        self._append(raw)
        if self.capture is not None and lower not in VOID_TAGS:
            self.capture_depth += 1

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self._append(self.get_starttag_text())

    def handle_endtag(self, tag: str) -> None:
        lower = tag.lower()
        if lower == "head" and self.capture is None:
            self.in_head = False
            return
        self._append(f"</{tag}>")
        if self.capture is not None:
            self.capture_depth -= 1
            if self.capture_depth == 0:
                self.slides.append("".join(self.capture))
                self.capture = None

    def handle_data(self, data: str) -> None:
        self._append(data)

    def handle_entityref(self, name: str) -> None:
        self._append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self._append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self._append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        self._append(f"<!{decl}>")

    def handle_pi(self, data: str) -> None:
        self._append(f"<?{data}>")


def standalone_document(
    fragment: str,
    *,
    head: str = "",
    base_css: str = "",
    title: str,
) -> str:
    """Wrap one model-authored page fragment without changing the fragment."""
    css = f"<style>\n{base_css}\n</style>" if base_css else ""
    return (
        "<!doctype html>\n"
        '<html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<base href="../">'
        f"<title>{html.escape(title)}</title>{head}{css}</head>"
        f"<body>{fragment}</body></html>\n"
    )


def page_files(run_dir: Path) -> list[Path]:
    slides_dir = run_dir / "slides"
    if not slides_dir.is_dir():
        return []
    files = [
        path
        for path in slides_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() == ".html"
        and ".bak." not in path.name.lower()
        and not path.name.startswith(".")
        and nonempty(path)
    ]
    numbered = [(page_number(path), path) for path in files]
    if files and all(number is not None for number, _ in numbered):
        values = [int(number) for number, _ in numbered if number is not None]
        if len(values) != len(set(values)):
            raise AdaptationError("page HTML filenames contain duplicate page numbers")
        return [path for _, path in sorted(numbered, key=lambda item: int(item[0]))]
    if len(files) > 1:
        raise AdaptationError(
            "multiple page HTML files cannot be ordered deterministically: "
            + ", ".join(sorted(path.name for path in files))
        )
    return files


def deck_candidate(run_dir: Path) -> Path:
    groups = [
        [run_dir / "deck.html"],
        [run_dir / "present.html"],
        [run_dir / "slide.html"],
        sorted(run_dir.glob("*.html")),
        sorted((run_dir / "~").glob("*.html"))
        if (run_dir / "~").is_dir()
        else [],
        sorted((run_dir / "output").glob("*.html"))
        if (run_dir / "output").is_dir()
        else [],
    ]
    seen: set[Path] = set()
    for group in groups:
        candidates = []
        for path in group:
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            if nonempty(path):
                candidates.append(path)
        if not candidates:
            continue
        if len(candidates) > 1:
            raise AdaptationError(
                "ambiguous deck HTML candidates: "
                + ", ".join(relative(path, run_dir) for path in candidates)
            )
        return candidates[0]
    raise AdaptationError("no page HTML files or recoverable deck HTML found")


def discover_pages(run_dir: Path) -> list[PageSource]:
    css_path = run_dir / "base.css"
    base_css = css_path.read_text(encoding="utf-8") if css_path.is_file() else ""
    files = page_files(run_dir)
    if files:
        pages: list[PageSource] = []
        for output_index, path in enumerate(files, 1):
            source = path.read_text(encoding="utf-8", errors="replace")
            operation = "native_full_html"
            exported = source
            if not is_full_html(source):
                operation = "wrapped_page_fragment"
                exported = standalone_document(
                    source,
                    base_css=base_css,
                    title=f"Slide {output_index}",
                )
            pages.append(
                PageSource(
                    output_index=output_index,
                    source_index=page_number(path),
                    source_path=path,
                    source_relpath=relative(path, run_dir),
                    html=exported,
                    operation=operation,
                )
            )
        return pages

    deck = deck_candidate(run_dir)
    source = deck.read_text(encoding="utf-8", errors="replace")
    parser = DeckParts()
    parser.feed(source)
    parser.close()
    if parser.capture is not None:
        raise AdaptationError(f"unterminated slide element in {deck}")
    if not parser.slides:
        if not is_full_html(source):
            raise AdaptationError(f"deck candidate is not a complete HTML document: {deck}")
        return [
            PageSource(
                output_index=1,
                source_index=1,
                source_path=deck,
                source_relpath=relative(deck, run_dir),
                html=source,
                operation="renamed_single_html",
            )
        ]
    head = "".join(parser.head_parts)
    return [
        PageSource(
            output_index=index,
            source_index=index,
            source_path=deck,
            source_relpath=relative(deck, run_dir),
            html=standalone_document(
                fragment,
                head=head,
                base_css="" if head else base_css,
                title=f"Slide {index}",
            ),
            operation="split_multi_page_html",
        )
        for index, fragment in enumerate(parser.slides, 1)
    ]


def render_map(run_dir: Path) -> dict[int, Path]:
    directory = run_dir / "renders"
    if not directory.is_dir():
        return {}
    result: dict[int, Path] = {}
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix.lower() != ".png":
            continue
        match = PAGE_PNG_RE.fullmatch(path.name)
        if match is None:
            continue
        number = int(match.group(1))
        if number <= 0:
            continue
        if number in result:
            raise AdaptationError(
                f"multiple render PNG files map to page {number}: "
                f"{result[number].name}, {path.name}"
            )
        result[number] = path
    return result


def render_missing(
    *,
    python_bin: Path,
    renderer: Path,
    html_path: Path,
    output_png: Path,
    temporary_root: Path,
) -> None:
    with tempfile.TemporaryDirectory(
        prefix=".novapresent-render-",
        dir=temporary_root,
    ) as temporary:
        temporary_dir = Path(temporary)
        rendered = temporary_dir / "slide_01.png"
        command = [
            str(python_bin),
            str(renderer),
            str(html_path),
            str(rendered),
        ]
        completed = subprocess.run(command, check=False)
        if completed.returncode != 0:
            raise AdaptationError(
                f"renderer exited with {completed.returncode}: {' '.join(command)}"
            )
        if not valid_png(rendered):
            raise AdaptationError(f"renderer did not create a valid PNG: {rendered}")
        output_png.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(rendered, output_png)


def native_contract(
    run_dir: Path,
    pages: list[PageSource],
) -> bool:
    if not pages:
        return False
    for page in pages:
        expected_html = run_dir / "slides" / f"slide_{page.output_index:02d}.html"
        expected_png = run_dir / "renders" / f"slide_{page.output_index:02d}.png"
        if (
            page.source_path.resolve() != expected_html.resolve()
            or page.operation != "native_full_html"
            or not valid_png(expected_png)
        ):
            return False
    native_pngs = render_map(run_dir)
    return set(native_pngs) == {page.output_index for page in pages}


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def copytree_ignore(source_root: Path):
    """Ignore only Harness-owned transient paths while copying a finished run."""
    resolved_root = source_root.resolve()

    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored = {name for name in names if name == ".pw-browsers"}
        if Path(directory).resolve() == resolved_root and "tmp" in names:
            ignored.add("tmp")
        return ignored

    return ignore


def adapt(
    *,
    run_dir: Path,
    output_dir: Path,
    renderer: Path,
    python_bin: Path,
) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    output_dir = output_dir.resolve()
    renderer = renderer.resolve()
    python_bin = python_bin.expanduser().absolute()
    if not run_dir.is_dir():
        raise AdaptationError(f"run directory not found: {run_dir}")
    if output_dir.exists() and output_dir != run_dir:
        raise AdaptationError(f"output directory already exists: {output_dir}")
    if not nonempty(renderer):
        raise AdaptationError(f"render.py not found: {renderer}")
    if not nonempty(python_bin):
        raise AdaptationError(f"Python executable not found: {python_bin}")

    pages = discover_pages(run_dir)

    native_ok = native_contract(run_dir, pages)
    source_renders = render_map(run_dir)
    source_page_numbers = {
        page.source_index for page in pages if page.source_index is not None
    }
    extra_renders = sorted(set(source_renders) - source_page_numbers)
    if extra_renders:
        raise AdaptationError(
            "render pages have no matching model-authored HTML; refusing to truncate: "
            + ", ".join(str(value) for value in extra_renders)
        )

    if run_dir == output_dir:
        if not native_ok:
            raise AdaptationError(
                "direct live output requires canonical native slide HTML/PNG pairs"
            )
        result: dict[str, Any] = {
            "schema_version": "limited_artifact_adaptation_v1",
            "mode": "read_only_native_output",
            "source_run_dir": str(run_dir),
            "output_dir": str(output_dir),
            "page_count": len(pages),
            "native_artifact_contract_satisfied": True,
            "final_artifact_contract_satisfied": True,
            "source_run_unchanged": True,
            "normalization_performed": False,
            "html_split_performed": False,
            "fragment_wrapping_performed": False,
            "filename_normalization_performed": False,
            "render_performed": False,
            "rendered_indices": [],
            "reused_render_indices": [page.output_index for page in pages],
            "canvas_fit_performed": False,
            "canvas_enforced": False,
            "truncated_indices": [],
            "filled_or_synthesized_indices": [],
            "pages": [
                {
                    "index": page.output_index,
                    "source_html": page.source_relpath,
                    "output_html": f"slides/slide_{page.output_index:02d}.html",
                    "output_render": f"renders/slide_{page.output_index:02d}.png",
                    "operation": page.operation,
                    "html_content_preserved": True,
                    "render_reused": True,
                }
                for page in pages
            ],
        }
        write_json(run_dir / "artifact_adaptation.json", result)
        return result

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.adapt-", dir=output_dir.parent)
    )
    try:
        shutil.copytree(
            run_dir,
            staging,
            symlinks=True,
            dirs_exist_ok=True,
            ignore=copytree_ignore(run_dir),
        )
        slides_dir = staging / "slides"
        renders_dir = staging / "renders"
        slides_dir.mkdir(parents=True, exist_ok=True)
        renders_dir.mkdir(parents=True, exist_ok=True)
        # The staging tree is a complete copy of the model-authored run. Keep
        # every original HTML, PNG, contact sheet, and diagnostic sidecar.
        # Normalization below is repair-only: it creates or refreshes the
        # canonical slide_NN pair without clearing unrelated source artifacts.

        rendered_indices: list[int] = []
        reused_render_indices: list[int] = []
        page_records: list[dict[str, Any]] = []
        for page in pages:
            html_path = slides_dir / f"slide_{page.output_index:02d}.html"
            png_path = renders_dir / f"slide_{page.output_index:02d}.png"
            if page.operation in {"native_full_html", "renamed_single_html"}:
                # A complete model-authored page is copied byte-for-byte. Only
                # its exported filename may change.
                shutil.copy2(page.source_path, html_path)
            else:
                html_path.write_text(page.html, encoding="utf-8")

            source_png = (
                source_renders.get(page.source_index)
                if page.source_index is not None
                else None
            )
            if source_png is not None and valid_png(source_png):
                shutil.copy2(source_png, png_path)
                reused_render_indices.append(page.output_index)
            else:
                render_missing(
                    python_bin=python_bin,
                    renderer=renderer,
                    html_path=html_path,
                    output_png=png_path,
                    temporary_root=output_dir.parent,
                )
                rendered_indices.append(page.output_index)

            page_records.append(
                {
                    "index": page.output_index,
                    "source_html": page.source_relpath,
                    "output_html": f"slides/slide_{page.output_index:02d}.html",
                    "output_render": f"renders/slide_{page.output_index:02d}.png",
                    "operation": page.operation,
                    "html_content_preserved": page.operation
                    in {"native_full_html", "renamed_single_html"},
                    "render_reused": page.output_index in reused_render_indices,
                }
            )

        split_performed = any(
            page.operation == "split_multi_page_html" for page in pages
        )
        fragment_wrapping_performed = any(
            page.operation == "wrapped_page_fragment" for page in pages
        )
        filename_normalization_performed = any(
            page.source_relpath != f"slides/slide_{page.output_index:02d}.html"
            for page in pages
        )
        adaptation_performed = bool(
            split_performed
            or fragment_wrapping_performed
            or filename_normalization_performed
            or rendered_indices
        )
        mode = (
            "limited_artifact_adaptation"
            if adaptation_performed
            else "read_only_native_output"
        )
        result: dict[str, Any] = {
            "schema_version": "limited_artifact_adaptation_v1",
            "mode": mode,
            "source_run_dir": str(run_dir),
            "output_dir": str(output_dir),
            "page_count": len(pages),
            "native_artifact_contract_satisfied": native_ok,
            "final_artifact_contract_satisfied": True,
            "source_run_unchanged": True,
            "normalization_performed": adaptation_performed,
            "html_split_performed": split_performed,
            "fragment_wrapping_performed": fragment_wrapping_performed,
            "filename_normalization_performed": filename_normalization_performed,
            "render_performed": bool(rendered_indices),
            "rendered_indices": rendered_indices,
            "reused_render_indices": reused_render_indices,
            "canvas_fit_performed": False,
            "canvas_enforced": False,
            "truncated_indices": [],
            "filled_or_synthesized_indices": [],
            "pages": page_records,
        }
        write_json(staging / "artifact_adaptation.json", result)
        staging.rename(output_dir)
        return result
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--renderer", required=True, type=Path)
    parser.add_argument("--python-bin", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = adapt(
        run_dir=args.run_dir,
        output_dir=args.output_dir,
        renderer=args.renderer,
        python_bin=args.python_bin,
    )
    print(f"artifact_mode: {result['mode']}")
    print(f"page_count: {result['page_count']}")
    print(
        "native_artifact_contract_satisfied: "
        f"{str(result['native_artifact_contract_satisfied']).lower()}"
    )
    print(f"rendered_indices: {result['rendered_indices']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AdaptationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
