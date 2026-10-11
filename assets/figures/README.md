# Manuscript figures

Current exports from **MuralPresenter: Multi-Agent Units of Responsibility for Authoring
Long-Horizon Presentations**, synchronized on **2026-10-11** from the manuscript working tree.
The inventory follows the figures actually included by `main_en.tex` and its chapters, including
uncommitted author edits. Draft plots and unused figure variants are not selected by modification time.

## Figure index

| Manuscript figure | Public image | Active source PDF |
| --- | --- | --- |
| 1 · Cross-page dependencies | [Preview](cross-page-dependencies.png) | `figures/fig1_cross_page_dependencies.pdf` |
| 2 · Agent roles and tools | [Preview](roles-and-tools.png) | `figures/fig2_framework.pdf` |
| 3 · Authoring and revision | [Preview](ownership-workflow.png) | `figures/fig3_workflow_preparation_group.pdf` |
| 4 · Responsibility workload | [Preview](responsibility-workload.png) | `figures/fig4_responsibility_workload.pdf` |
| 5 · Selective supervision | [Preview](supervision-ablation.png) | `figures/fig5_supervision_ablation.pdf` |
| Appendix · Revision case | [Six before/after pairs](revision-case/README.md) | `figures/revision_case.pdf`, pages 1–12 |

Figure 3 is included through `figures/fig3_authoring_process.tex`; that wrapper selects
`fig3_workflow_preparation_group.pdf`, not the similarly named standalone PDF.
The appendix shows pages 1–6 of a twelve-slide deck before and after revision. It does not show
all twelve slides in either state. The qualitative case is excluded from benchmark means.

## Running example and README icons

The homepage presents a short animal-compass request, selected actual outputs and an expandable
method explanation with Figure 3.
Its sources are `chapters_en/01_introduction.tex`, `chapters_en/02_muralpresenter.tex` and the
animal-compass requirements in `chapters_en/appendix.tex`. This illustrates the task and
responsibility assignment; it is not a trace-backed demonstration of realized grouping or repair.
The [animal-compass gallery](animal-compass/README.md) adds three original output pages from a
completed 40-slide run using the 27B Global Selective two-epoch checkpoint. Its separate manifest
records the experiment runtime, which differs from the frozen 0919 source release. These pages
show generated outputs, not the illustrative edit sequence.
The paper-to-presentation revision gallery remains a separate qualitative execution case.

Three [capability icons](icons/README.md) reuse original manuscript PNGs alongside the overview's
text labels. Their separate [manifest](icons/manifest.json) records byte-identical copies.

## Export quality and provenance

- The five main figures are complete PDF pages rendered at 2,400 pixels wide with PyMuPDF.
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

中文：本次同步正文实际引用的五张图和附录六组修改前后对照，共 17 张 PNG。只转换文件格式，
不改图中科学内容；未被正文引用的试绘版本留在论文工作区。旧品牌素材与 Git 历史保留。
