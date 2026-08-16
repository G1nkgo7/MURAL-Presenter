# Author-Approved Main-Figure Specification

## Source of truth

The two PNG files under `architecture/approved/` are the author-approved visual
masters. They supersede earlier deterministic redraws and concept variants.

- Figure 1: `approved/fig1_page_topologies.png` (1872×840).
- Figure 2: `approved/fig2_release_workflow.png` (1881×836).

The approved pixels must not be redrawn, recolored, cropped, sharpened, or otherwise
altered. `build_paper_figures.py` copies each PNG byte-for-byte and only creates PDF
and SVG raster wrappers required by the paper toolchain.

## Figure 1 — Execution topologies

The selected figure compares:

1. sequential generation with a growing history and shared evolving state;
2. full-context per-slide parallel generation with replicated deck context and
   independent slide ownership; and
3. MURAL with one shared deck blueprint, group briefs, Group Agents, multi-slide
   outputs, deck assembly, and whole-deck review.

Figure 1 motivates group ownership and deck closure. It does not contain the later
human-edit router; revision routing belongs to Figure 2.

## Figure 2 — Full authoring and revision lifecycle

The selected figure contains four stages:

1. Material & Research;
2. Plan & Compile, including group briefs and semantic grouping;
3. Group Authoring with group-local write–render–inspect–revise;
4. Review, Revise & Deliver, including whole-deck review, user edit, the revision
   router, local patch, group reactivation, replan, and HTML/PPTX/PDF/image delivery.

Captions may explain the scope semantics but must use labels consistent with the
figure, especially `revision router`.

## Distribution

The same approved PNG bytes must appear at all PNG consumer locations:

- `figures/architecture/fig{1,2}_*.png`;
- `manuscript/figures/fig{1,2}_*.png`;
- `MURAL/assets/figures/execution-topologies.png` and
  `MURAL/assets/figures/authoring-lifecycle.png`;
- matching files under `MURAL/site/public/`.

The paper includes the generated PDF wrappers. SVG wrappers embed the same approved
PNG and are provided for compatibility; they are not editable vector redraws.
