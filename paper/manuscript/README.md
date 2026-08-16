# MuralPresenter bilingual review drafts

This directory reuses the previous ICLR 2026 style/template assets for paired Chinese and English,
release-grounded MuralPresenter manuscripts.

The Method and experiments are grounded in the frozen MuralPresenter implementation, benchmark,
renderer, Judge, and run ledgers described in the appendix. Exact versions, hashes, budgets, seeds,
and failure rules must travel with any released result bundle.

Compile from this directory with:

```bash
tectonic main_zh.tex --keep-intermediates --keep-logs
tectonic main_en.tex --keep-intermediates --keep-logs
```

Both entrypoints require XeLaTeX because they use `fontspec` and bundled fonts. The root-level
`latexmkrc` redirects Overleaf's default pdfLaTeX command to XeLaTeX, and both entrypoints also carry
the `% !TeX program = xelatex` directive. When switching languages in Overleaf, select `main_en.tex`
or `main_zh.tex` as the project's Main document; no compiler change is required for a fresh upload.

The canonical Chinese prose sources live in `../chapters/`; their English peers live in
`../chapters_en/`. Regenerate the LaTeX projections with
`python ../scripts/convert_chapters.py --lang zh` and
`python ../scripts/convert_chapters.py --lang en` before compiling.

The Chinese PDF is for argument review; the English PDF is the submission-facing edition. The paper
is written as a completed study. Unknown numerical values and metric-dependent interpretations retain
unique `[TBD:*]` IDs in source but render as compact orange `TBD` markers in the PDFs. The author-only
replacement ledger is `../plan/result-placeholder-ledger-20260813.md` and is excluded from the
Overleaf package.

The English build ends the main body and starts References on page 12, with 17 total pages.
The Chinese review build ends the main body on page 10, starts References on page 11, and has 15 total
pages including the appendix. Both entrypoints use `\raggedbottom`, and main result tables use
`[!htbp]`, preventing the template from stretching internal whitespace around non-breakable floats.
Figure 3 uses the raster overview at
`figures/fig3_long_horizon_agentic_data_synthesis.png` with a LaTeX overlay that replaces the former
planning-status label with `MuralPresenter-9B corpus`. Figure 4 uses the deterministic contract diagram
at `figures/fig4_threadbench_anatomy.pdf`; its criterion-level details remain in Appendix A. Run `python ../scripts/audit_manuscript.py`,
`python ../scripts/audit_bilingual.py`, and `python ../scripts/audit_english_manuscript.py` after
material changes.
