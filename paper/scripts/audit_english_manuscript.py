#!/usr/bin/env python3
"""Check the compiled English author-review manuscript and its page budget."""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANUSCRIPT = ROOT / "manuscript"
CHAPTERS = ROOT / "chapters_en"
EXPECTED_TBDS = Counter({f"TBD-E{i}": 1 for i in range(1, 8)})


def check(condition: bool, label: str, failures: list[str]) -> None:
    if condition:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}")
        failures.append(label)


def main() -> int:
    failures: list[str] = []
    paths = {
        "main": MANUSCRIPT / "main_en.tex",
        "pdf": MANUSCRIPT / "main_en.pdf",
        "log": MANUSCRIPT / "main_en.log",
        "aux": MANUSCRIPT / "main_en.aux",
        "bib": MANUSCRIPT / "references.bib",
    }
    for label, path in paths.items():
        check(path.is_file(), f"{label} exists", failures)
    if failures:
        return 1

    main_tex = paths["main"].read_text(encoding="utf-8")
    log = paths["log"].read_text(encoding="utf-8", errors="replace")
    aux = paths["aux"].read_text(encoding="utf-8", errors="replace")
    chapter_text = "\n".join(
        (CHAPTERS / f"0{i}_{name}.md").read_text(encoding="utf-8")
        for i, name in (
            (1, "introduction_related"),
            (2, "method"),
            (3, "benchmark_experiments"),
            (4, "discussion_conclusion"),
        )
    )

    check(
        r"\title{\fullsys:" in main_tex
        and r"\newcommand{\fullsys}{\MuralPresenterLogo}" in main_tex
        and r"\newcommand{\sys}{MuralPresenter}" in main_tex
        and "Multi-Agent Unified" in main_tex
        and "Reasoning and Authoring" in main_tex
        and "Long-Horizon Presentations" in main_tex,
        "locked title is present",
        failures,
    )
    check("xeCJK" not in main_tex and "fontspec" not in main_tex,
          "English build has no CJK dependency", failures)
    check(
        all(
            f"\\input{{chapters_en/0{i}_{name}}}" in main_tex
            for i, name in (
                (1, "introduction_related"),
                (2, "method"),
                (3, "benchmark_experiments"),
                (4, "discussion_conclusion"),
            )
        ),
        "all English chapters are included",
        failures,
    )
    check(
        "\\input{chapters_en/05_appendix}" in main_tex
        and "\\input{figures/page_topologies_en}" in (
            MANUSCRIPT / "chapters_en" / "01_introduction_related.tex"
        ).read_text(encoding="utf-8")
        and "\\input{figures/release_workflow_en}" in (
            MANUSCRIPT / "chapters_en" / "02_method.tex"
        ).read_text(encoding="utf-8"),
        "English appendix and captions are included",
        failures,
    )

    tbd_counts = Counter(re.findall(r"TBD-E[1-7]", chapter_text))
    check(tbd_counts == EXPECTED_TBDS, "TBD-E1--E7 occur exactly once", failures)

    cited: set[str] = set()
    for group in re.findall(r"\\cite[pt]?\{([^}]+)\}", chapter_text):
        cited.update(key.strip() for key in group.split(","))
    bib_keys = set(re.findall(r"@\w+\{\s*([^,\s]+)", paths["bib"].read_text(encoding="utf-8")))
    check(cited <= bib_keys, "all English citation keys exist in the bibliography", failures)

    bad_log = re.search(
        r"(?:LaTeX Error|Undefined control sequence|Citation .* undefined|"
        r"Reference .* undefined|There were undefined references)",
        log,
        flags=re.IGNORECASE,
    )
    check(bad_log is None, "compile log has no undefined citations/references or TeX errors", failures)

    total_match = re.search(r"Output written on main_en\.xdv \((\d+) pages", log)
    check(total_match is not None, "total page count is recorded", failures)
    if total_match:
        print(f"INFO  total pages: {total_match.group(1)}")

    main_match = re.search(
        r"\\newlabel\{main:end\}\{\{[^}]*\}\{(\d+)\}", aux
    )
    check(main_match is not None, "main-body end page is recorded", failures)
    if main_match:
        main_pages = int(main_match.group(1))
        print(f"INFO  main-body pages: {main_pages}")
        check(main_pages <= 9, "main body fits the 9-page target", failures)

    check(paths["pdf"].read_bytes().startswith(b"%PDF"), "compiled artifact is a PDF", failures)
    print(f"English manuscript audit: {'PASS' if not failures else 'FAIL'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
