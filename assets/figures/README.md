# Manuscript figures

Exports for **MuralPresenter: Dependency-Aware Multi-Agent Authoring for Long-Horizon Presentations**,
synchronized on **2026-10-13** from the manuscript working tree, including uncommitted author edits.
The inventory distinguishes current manuscript figures from the archived revision example retained
by this repository. Draft variants are not selected by modification time.

## Figure index

| Manuscript figure | Public image | Active source PDF |
| --- | --- | --- |
| Main text · Cross-page dependencies | [Preview](cross-page-dependencies.png) | `figures/fig1_cross_page_dependencies.pdf` |
| Appendix · Agent roles and tools | [Preview](roles-and-tools.png) | `figures/fig2_framework.pdf` |
| Main text · Authoring and revision | [Preview](ownership-workflow.png) | `figures/fig3_workflow.pdf` |
| Main text · Responsibility workload | [Preview](responsibility-workload.png) | `figures/fig4_responsibility_workload.pdf` |
| Main text · Selective supervision | [Preview](supervision-ablation.png) | `figures/fig5_supervision_ablation.pdf` |
| Archived example · Revision case | [Six before/after pairs](revision-case/README.md) | `figures/revision_case.pdf`, pages 1–12 |

The workflow is included through `figures/fig3_authoring_process.tex`, which now selects
`fig3_workflow.pdf`. The revision example shows pages 1–6 of a twelve-slide deck before and
after revision, not all twelve slides. Its appendix display is disabled in the current manuscript;
the repository retains it as an independent example excluded from benchmark means.

## Running example and README icons

The homepage presents a short animal-compass request, selected actual outputs and an expandable
method explanation with the current workflow figure.
Its sources are `chapters_en/01_introduction.tex`, `chapters_en/02_preliminaries.tex` and the
animal-compass requirements in `chapters_en/appendix.tex`. This illustrates the task and
responsibility assignment; it is not a trace-backed demonstration of realized grouping or repair.
The [animal-compass gallery](animal-compass/README.md) adds three original output pages from a
completed 40-slide run using the 27B Global Selective two-epoch checkpoint. Its separate manifest
records the experiment runtime, which differs from the frozen 0919 source release. These pages
show generated outputs, not the illustrative edit sequence.
The paper-to-presentation revision gallery remains a separate qualitative execution case.

Three [capability icons](icons/README.md) remain available as manuscript artwork; they are no longer
used in the homepage overview. The compact header uses standard pictograms beside its
primary navigation links. The icons' [manifest](icons/manifest.json) records byte-identical copies.

## Export quality and provenance

- The five method and analysis figures are complete PDF pages rendered at 2,400 pixels wide with PyMuPDF.
- The twelve case previews are complete PDF pages rendered at 1,200 × 675 pixels.
- All exports use an opaque background and lossless PNG. No labels, data, colors or diagrams are redrawn.
- [manifest.json](manifest.json) records source PDF hashes, page mappings, output hashes, dimensions,
  renderer version and the manuscript Git HEAD. The per-file hashes identify working-tree sources
  more precisely than the Git HEAD when the manuscript has uncommitted changes.
- Source PDFs and the manuscript workspace remain outside this runtime release.

To reproduce an export, use the manifest's source and page with PyMuPDF:

```python
import fitz

with fitz.open(source_pdf) as document:
    page = document[source_page - 1]
    scale = output_width / page.rect.width
    page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).save(output_png)
```

## Historical artwork

`execution-topologies.{png,pdf}` and `authoring-lifecycle.{png,pdf}` remain historical illustrations.
They use the earlier group-centric framing and are not the current README figures. Earlier versions
of the three replaced PNGs remain in Git history.

The README header retains the approved [street-mural identity](../logo/exports/street-v2/README.md).
Earlier `mural-paper-lockup-{light,dark}.svg`, PNG lockups and mascots remain historical exports.
Manuscript figures retain their scientific content independently of the promotional brand artwork.

中文：当前保留正文／附录方法与分析图五张，以及独立修改示例的六组前后对照，共 17 张 PNG。
本次更新流程图和角色图；只转换文件格式，不改科学内容。修改示例虽暂不在论文展示，仍在仓库保留。
