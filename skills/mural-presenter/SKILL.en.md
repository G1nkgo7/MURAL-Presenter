---
name: mural-presenter
description: Create or edit a complete HTML presentation deck from a topic, outline, document, or multiple attachments. Produces one 1600x900 HTML file and render per slide, a page-aligned speaker script, and a portable player. Routes Research, Material, Image, Slide, and Review agents for creation, revision, restyling, continuation, and quality review.
---

# MURAL Presenter — English workflow

Treat this file as a route map, not a document to memorize. The runtime selected this entry because the user query is primarily English. It selects `SKILL.md` for Chinese queries. Use the English role cards at `subagents/<role>.en.md`; scripts, references, assets, paths, field names, status values, and final contracts are shared and must never be translated.

`response_language` and `deliverable_language` are independent. Use English for visible work and handoffs because this entry was selected; the slide copy, planning artifacts, and speech follow the user's requested deliverable language. A Chinese deck requested in English therefore still uses this English workflow while delivering Chinese slides.

## 0. Select the task mode

| User goal | Mode | Route |
| --- | --- | --- |
| Create a deck from a topic, brief, or attachments | create | Follow section 2 |
| Make a bounded local edit without changing narrative, facts, assets, or global style | simple edit | Follow section 3.2 |
| Change narrative, facts, assets, global style, or multiple page structures | complex edit | Follow section 3.3 |

If uncertain, perform a read-only impact analysis first. Page count alone does not decide the mode.

## 1. Shared contracts

### 1.1 Orchestrator boundary

The Orchestrator only selects the mode, plans, delegates, merges, validates, and performs deterministic closeout.

- It may write `plan/`, `base.css`, grounded summaries, and build artifacts.
- It must not write `slides/slide_NN.html`; Slide or Review owns page HTML.
- Every subagent has an explicit label and exact, non-overlapping scope.
- A timeout, crash, `blocked`, malformed contract, or non-natural stop is not a completed artifact.
- Parent truth is the structured returned contract, artifact paths, and `handoff_path`; do not poll child trace internals.
- Research is a task-level singleton. Review has one initial pass and at most two controlled post-fix rechecks.

### 1.2 Roles and routing

| Role | When | Sole responsibility |
| --- | --- | --- |
| Research | At most one, only for consequential external facts | Verify evidence and write `research/research.md` |
| Material | Parallel by non-overlapping attachment shards | Fully consume attachments and write `research/materials/material_NN.md` |
| Image | Parallel by coherent asset groups | Acquire/generate, inspect, catalog, and return real bitmap paths |
| Slide | Parallel by design-related production groups | Build only its assigned pages and close the pixel loop |
| Review | One full review; up to two post-fix rechecks | Diagnose all pages, batch repair, re-render, verify, and close speech/build |

Each child reads exactly its English card in `subagents/` and only the references routed by that card. Continue any truncated read until EOF. Do not scan unrelated references or role cards.

### 1.3 Language and capability lock

At the start, lock:

- `response_language` for visible reasoning, progress, tool preambles, and handoffs;
- `deliverable_language` for slide copy, plans, and speech;
- attachment, web, image acquisition/generation, rendering, Vision, and file capabilities.

Default each to the main query language unless the user explicitly requests a different deliverable language. `vision_analyze` is direct pixel inspection by the current role's model. Discuss visual findings in `response_language`; preserve original quotations and proper nouns when needed.

### 1.4 Workspace sources of truth

```text
research/research.md
research/materials/material_NN.md
plan/grounded-knowledge.md
plan/design-brief.md
plan/deck.md
plan/slide_NN.md
base.css
assets/
slides/slide_NN.html
renders/slide_NN.png
speech.md
present.html
```

`grounded-knowledge.md` owns facts; `design-brief.md` owns art direction; `slide_NN.md` owns page content; `base.css` owns the global design system. Final judgments always use the latest PNG, not imagined HTML output.

### 1.5 Progressive reference routing

The shared references use stable machine paths and may contain Chinese prose. Read only the routed sections and preserve all schema names and commands exactly.

| Need | Read |
| --- | --- |
| Scene and art direction | `references/design-rules.md` T1–T3 and sections 1–3 plus the matching topic section; `references/design-styles.md` index and one style family |
| Global/page planning | `references/planning-contract.md`; only the relevant page archetype in `references/layout-patterns.md` |
| Existing-deck edits | `references/editing-contract.md` |
| Slide implementation | Assigned page plans, `base.css`, the single-page section of `references/quality-checklist.md`, and routed page sections |
| Review | All of `references/quality-checklist.md` |

Read `references/fonts.md` only when the default font roles are insufficient or culturally sensitive. Route typography by occasion: formal work uses one or two families, normal presentations two or three stable roles, and explicitly expressive presentations may use three or four, with the fourth limited to a deliberate accent role. The default short-title voice is Smiley Sans with Noto Sans SC fallback; formal or rigorous work must override it to Noto Sans/Serif SC. CJK body copy never uses a Latin monospace role.

### 1.6 Media hierarchy

Choose media for meaning:

1. Real people, places, products, events, or evidence: real photographs.
2. Atmosphere, metaphor, story scenes, or hero art: generated or high-quality bitmap imagery.
3. Data: ECharts.
4. Large processes, architectures, mechanisms, and relationships: Canvas geometry plus HTML labels, or a text-free bitmap plus HTML labels.
5. SVG: only icons, logos, arrows, markers, and small decoration.

Do not replace visible people, products, works, activities, or environments with generic cards, tiny icons, decorative SVG, or abstract wireframes. A normal content slide needs a meaningful primary visual carrier: bitmap, chart, explanatory Canvas, or deliberate typographic composition.

## 2. Create a new deck

### Stage 0 — Parse

Lock language, capabilities, attachments, and delivery scope. Create an internal scene card: Speaker, Audience, Occasion, Objective, Duration, Page count, Screen vs speech, Core takeaway, Assumptions. Do not ask for non-blocking preferences; state tasteful assumptions in planning artifacts.

### Stage 1 — Ground only what matters

#### Material

When attachments exist, delegate non-overlapping Material shards, normally one file each. Give every goal exact paths, `assignment_id`, an isolated `materials/_work/material_NN/`, and `research/materials/material_NN.md`.

All formats go through:

```bash
python ${SKILL_DIR:-skills/mural-presenter}/scripts/stage_materials.py materials/_work/<assignment_id> \
  --input materials/_raw/<file>
```

Every attachment needs one `coverage_id`, `status: ok`, `coverage: complete`, continuous text chunks or complete scanned-page coverage, and a matching ledger entry. `semantic_coverage: incomplete`, `truncated`, `unsupported`, `failed`, or `missing` blocks downstream work. File metadata, archive member names, or representative frames are not semantic coverage.

Page PNGs are reading context, not automatic display figures. Named paper figures must become traceable `material_figure_crop` derivatives. A full-page facsimile is allowed only when page appearance itself is evidence and requires an explicit justification.

#### Research

Delegate the singleton Research only when an external fact can change a conclusion. Its goal must include the verbatim full user query, unresolved terms, evidence needed, response language, and deliverable language. Parent interpretations are hypotheses, never user claims. Batch independent searches in one tool round, batch best-source extraction next, and allow at most one focused gap search.

#### Grounding gate

After Material and Research return, the next action is to write and then read back `plan/grounded-knowledge.md`. Separate user facts, attachment evidence, externally verified facts, assumptions, illustrative values, conflicts, and unresolved items. A `partial` Research result must propagate its `unresolved` list and usage boundary verbatim. No Style Lock or page planning begins before this gate.

### Stage 2 — Scene direction and Style Lock

Read only the routed design references. Select one scene register and one primary style, optionally one supporting craft. Write `plan/design-brief.md#Style Lock` with:

- `scene`, `primary_style`, and at most one `supporting_craft`;
- `visual_thesis`, `signature_visual`, palette, and an explicit typography contract with `title_voice`, `title_scale`, `title_treatment`, `body_voice`, `numeric_voice`, and `font_roles`;
- image language and an implementation-independent `image_opportunity_map`;
- `base_canvas_family`, allowed background states, spatial rhythm, and special-page system;
- motif roles and explicit avoid rules;
- attachment `material_visual_mode` and `attachment_visual_map` when applicable;
- lightweight `crop_contract` for identity-, evidence-, or hero-bearing images: focus, protected parts, disposable background, and recommended fit.

Style Lock fixes a visual language, not a page template. Stability comes from type roles, color semantics, spacing, image treatment, and composition grammar. Controlled variation comes from focus, direction, medium, density, whitespace, and chapter state.

Unless the occasion is formal or rigorous, the title voice must visibly separate itself from body copy in family, scale, or treatment. Do not silently collapse an ordinary presentation into Noto Sans SC at document-like sizes.

Before declaring `image_opportunity: none`, scan for visible people, places, products, works, activities, experiences, fictional characters, and emotional scenes. Search named real identities; never generate a fake likeness. Generated imagery is valid for fictional, conceptual, future, atmospheric, or metaphorical scenes. If image tools are available, a deck with no bitmaps or only a cover bitmap is an exception that must be justified, not the safe default.

### Stage 3 — Global and per-page planning

Write `plan/deck.md` and every `plan/slide_NN.md` before page production. Follow `references/planning-contract.md` exactly. Each page contract freezes page purpose, exact audience-facing copy, evidence, medium, primary visual carrier, assets and `asset_id`, crop contract, layout archetype, spatial budget, background treatment, visual acceptance criteria, speech intent, and production group.

Group pages by design kinship, not only adjacency. Use stable groups such as `bookends`, `dividers`, and coherent content groups. The Orchestrator then writes `base.css` and runs:

```bash
python ${SKILL_DIR:-skills/mural-presenter}/scripts/deck.py prepare . --expected <slide_count>
```

### Stage 4 — Acquire imagery

Delegate Image groups only after page briefs and asset IDs are stable. Image owns real/generated bitmap acquisition, material figure crops, cutouts, catalog assignment, contact sheets, Vision review, and final `ready` status. Slide may use only ready catalog assets whose paths are frozen into its page plans.

### Stage 5 — Produce slide groups

Delegate independent design-related Slide groups in parallel. Each group reads only its scope, creates one page at a time, renders that page, performs open-ended Vision inspection, applies at most one consolidated refinement round, re-renders and rechecks, then performs a final group contact-sheet check.

Do not place mechanical source footnotes, internal paths, evidence IDs, assumptions, production labels, or fake metadata on slides. Put traceability in planning and speech. Covers, section dividers, and closing pages do not inherit normal page footers or page numbers.

### Stage 6 — Final Review and delivery

Run deterministic contact preparation, then delegate one `mode=final_review` Review. Review must inspect every latest PNG, freeze one issue ledger before editing, batch compatible fixes, re-render all changed pages, recheck pixels, sync speech, build the player, and inspect post-build output.

```bash
python ${SKILL_DIR:-skills/mural-presenter}/scripts/deck.py contact . --expected <slide_count>
python ${SKILL_DIR:-skills/mural-presenter}/scripts/deck.py build . --expected <slide_count>
```

Deliver `ready` when all hard gates pass. If every slide, render, speech entry, and player is usable but known hard issues remain after the bounded review budget, deliver `needs_improvement` with the issue ledger. Advisory warnings alone never discard a usable deck.

## 3. Edit an existing deck

Read `references/editing-contract.md` in full. Perform a read-only inventory of plans, HTML, assets, speech, and renders before choosing a path.

### 3.1 Simple versus complex

An edit is simple only when it preserves the core claim, page order, page roles, cross-page narrative, facts, assets, Style Lock, font system, and global architecture, and has a small explicit impact boundary. Otherwise it is complex.

### 3.2 Simple Review repair

Delegate exactly one Review with `mode=simple_edit`, the verbatim request, target pages, and invariants. It inspects current pixels, diagnoses once, patches target pages in one batch, updates affected plans/speech, batch re-renders, inspects changed pixels, runs `deck.py build`, and verifies post-build pixels. Do not delegate Slide, Image, Research, or a second Review.

### 3.3 Complex Orchestrator revision

Write an impact graph for facts, narrative, order, Style Lock, CSS, assets, pages, and speech. Reuse unaffected artifacts. Delegate only the missing Research, Material, Image, and affected Slide groups; update plans, run `prepare`, rebuild only affected pages unless global tokens changed, then run one full `final_review`.

### 3.4 Edit delivery gate

Every user request maps to a visible change; unaffected pages/assets remain unchanged; old and new pages share style, numbering, speech, and player behavior; every changed page has fresh post-change pixel evidence; fonts, render freshness, `speech.md`, and `present.html` pass.

## 4. Hard rules

- Data charts use ECharts; generated images never fake data graphics.
- Accurate text lives in HTML, not AI-generated images.
- SVG is not a hero image or large structural diagram.
- `slides/` contains only canonical `slide_NN.html` files; backups live under `_trace/`.
- Audience body copy is at least 20px; captions/secondary notes are at least 18px, or larger if tokens require it.
- Fix overflow by reducing repetition, restructuring, reprioritizing, or splitting pages, never by hiding content or shrinking below the floor.
- Internal planning labels, source paths, production states, and valueless pseudo-metadata never appear on screen.
- Final truth is the latest PNG. Never claim a change is complete without rendering and inspecting its new pixels.
- Review has at most three controlled instances total, and recheck is allowed only after pages were actually changed and re-rendered.

## 5. Deterministic tools

| Tool | Purpose |
| --- | --- |
| `stage_materials.py` | Parse text, PDF, Office/ODF, images, media, archives, and unknown formats; produce visual derivatives and coverage catalogs |
| `font_bundle.py` | License-aware, distributable font bundling and render freshness |
| `render.py` | Single-page and shared-browser batch rendering plus diagnostics |
| `image_cutout.py` | Inspect alpha and produce validated subject cutouts without overwriting sources |
| `deck.py` | `prepare`, material figure crop, asset registration/assignment/contact/review, page contact, speech sync, and final build |
| `install.sh` | Install cross-environment runtime, font, and Chromium dependencies |

Use the actual Skill root when mounted elsewhere. Do not read tool implementations to reverse-engineer around their public commands, stdout, manifests, or validation results.
