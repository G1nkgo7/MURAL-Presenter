#!/usr/bin/env python3
"""Regression check for convert_chapters native-TeX/prose handling.

Proves three things after the PROTECTED-regex change that lets LaTeX ``table``
blocks pass through conversion untouched:

1. Two distinct ``\\begin{table}...\\end{table}`` blocks survive verbatim,
   including their ``\\label{...}`` and ``&`` / ``\\\\`` cell separators.
2. Ordinary prose escaping still works: a literal ``&`` / ``%`` / ``_`` in
   running text is still escaped, so the table fix did not disable escaping.
3. Reviewed TeX prose idioms (non-breaking spaces, thousands separators,
   explicit approximation marks, and control spaces) remain semantic.
4. Native ``figure`` environments survive verbatim instead of becoming prose.
5. Chinese ``摘要`` front matter becomes an abstract environment rather than
   a numbered section, preserving bilingual section-number parity.

Run:  python scripts/test_convert_tables.py   (exit 0 = pass)
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import convert_chapters as cc


SAMPLE = r"""# 6. Experiments

Prose before with a literal ampersand A & B, a percent 50%, and an under_score.
Figure~\ref{fig:alpha} reports 17{,}171 tasks, \textasciitilde7.31B tokens, and Avg.\ Partial Score.

\begin{table}[t]
\centering
\caption{First table caption.}
\label{tab:alpha}
\begin{tabular}{lc}
\toprule
System & Score \\
\midrule
Foo & \texttt{[TBD:X\_A]} \\
\bottomrule
\end{tabular}
\end{table}

Prose between the two tables, still with A & B.

\begin{table}[t]
\centering
\caption{Second table caption.}
\label{tab:beta}
\begin{tabular}{lc}
\toprule
System & Score \\
\midrule
Bar & \texttt{[TBD:X\_B]} \\
\bottomrule
\end{tabular}
\end{table}
"""

FRONT_ZH = """# 论文标题

## 摘要

摘要正文。

## 1 Introduction

引言正文。
"""

FIGURE = r"""# 4. Data

\begin{figure}[H]
\centering
\includegraphics[width=\linewidth]{figures/pipeline.png}
\caption{Pipeline overview.}
\label{fig:pipeline}
\end{figure}
"""

RESULT_SLOT = r"""# 6. Results

The model obtains \resulttbd{[TBD:G\_M9\_PB]} overall.
"""


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "sample.md"
        src.write_text(SAMPLE, encoding="utf-8")
        out = cc.convert(src)
        front = Path(tmp) / "01_introduction_related.md"
        front.write_text(FRONT_ZH, encoding="utf-8")
        out_zh = cc.convert(front)
        figure = Path(tmp) / "02_method.md"
        figure.write_text(FIGURE, encoding="utf-8")
        out_figure = cc.convert(figure)
        slot = Path(tmp) / "03_results.md"
        slot.write_text(RESULT_SLOT, encoding="utf-8")
        out_slot = cc.convert(slot)

    failures = []

    # 1) Both table blocks survive verbatim (env + labels + separators intact).
    for label in ("tab:alpha", "tab:beta"):
        if f"\\label{{{label}}}" not in out:
            failures.append(f"missing verbatim \\label{{{label}}}")
    if out.count(r"\begin{table}") != 2 or out.count(r"\end{table}") != 2:
        failures.append("table environments not passed through exactly twice")
    if r"\begin{tabular}{lc}" not in out:
        failures.append("tabular preamble was mangled")
    if r"System & Score \\" not in out:
        failures.append("cell separators (& / \\\\) inside table were escaped")
    # Table content must NOT be escaped into \textbackslash form.
    if r"\textbackslash{}begin\{table\}" in out:
        failures.append("table block was escaped instead of passed through")

    # 2) Prose escaping still works outside tables.
    if r"A \& B" not in out:
        failures.append("literal & in prose was not escaped")
    if r"50\%" not in out:
        failures.append("literal % in prose was not escaped")
    if r"under\_score" not in out:
        failures.append("literal _ in prose was not escaped")

    # 3) Reviewed TeX prose idioms remain semantic rather than becoming
    # visible escape sequences in the rendered PDF.
    if r"Figure~\ref{fig:alpha}" not in out:
        failures.append("TeX non-breaking space before a reference became visible")
    if r"17{,}171" not in out:
        failures.append("TeX thousands separator was escaped into visible braces")
    if r"\textasciitilde7.31B" not in out:
        failures.append("explicit approximate marker was escaped as prose")
    if r"Avg.\ Partial Score" not in out:
        failures.append("TeX control space was escaped as prose")

    # 4) Native figure blocks must survive verbatim.
    if r"\begin{figure}[H]" not in out_figure or r"\label{fig:pipeline}" not in out_figure:
        failures.append("figure environment or label was not passed through")
    if r"\textbackslash{}begin\{figure\}" in out_figure:
        failures.append("figure block was escaped instead of passed through")

    # 5) Result placeholders keep their unique source ID while rendering via
    # the compact review macro rather than as visible escaped LaTeX.
    if r"\resulttbd{[TBD:G\_M9\_PB]}" not in out_slot:
        failures.append("result placeholder macro was escaped in prose")
    if r"\textbackslash{}resulttbd" in out_slot:
        failures.append("result placeholder macro became visible prose")

    # 6) Chinese front matter must not consume a numbered section.
    if r"\begin{abstract}" not in out_zh or r"\end{abstract}" not in out_zh:
        failures.append("Chinese 摘要 was not converted to an abstract environment")
    if r"\section{摘要}" in out_zh:
        failures.append("Chinese 摘要 incorrectly consumed a numbered section")
    if r"\section{Introduction}" not in out_zh:
        failures.append("Chinese front-matter level shift broke the Introduction section")

    if failures:
        print("FAIL:")
        for f in failures:
            print("  -", f)
        return 1
    print("PASS: tables/figures/prose preserved; Chinese abstract numbering intact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
