---
name: mural-presenter-v0-2-en
description: English instructions for producing audience-facing 1600×900 static HTML presentations. The query, not the instruction edition, determines delivery language.
---

# Static HTML Presentation

Produce coherent, evidence-led presentations that are readable in a room and
visually authored rather than assembled from a default card template.

## Roles and outputs

| Role | Canonical output |
|---|---|
| Orchestrator | `plan/deck.md`, every `plan/slide_NN.md`, delegation, delivery |
| Material | `research/material.md` only when attachments exist |
| Research | one `research/knowledge-brief.md` |
| Image | local imagery plus one `assets/catalog.md` and contact sheet |
| Slide | one finished `slides/slide_NN.html` and PNG-driven repair |
| Review | final whole-deck pixel and speech-alignment decision |

## Workflow

```text
Material when needed
→ one Research brief
→ deck.md + slide_NN.md
→ validate-plans
→ scaffold-from-plans + sync-speech
→ Image and code_only / none Slides in parallel
→ preferred Slides after resolve-or-skip decisions
→ required Slides after their assets resolve
→ finalize
→ Review; if changed, finalize and inspect again
→ delivery
```

For a long deck, Orchestrator may write consecutive small groups of page Markdown
files. Missing files are resumed directly by page number.
Research maintains one canonical brief: when evidence spans several themes it
creates stable sections early and fills only incomplete sections, rather than
retrying one monolithic final write or duplicating the same ledger elsewhere.
Production always assigns one Slide Agent per page. All `Slide NN:` tasks may
run in one parallel wave, but they must never be merged into a SlideGroup. v0.2
deliberately matches the single-page training distribution.
Except for purely transformative/fictional work, a user prohibition on network
research, or attachments with complete evidence, Research actually issues the
parallel first-pass searches instead of substituting model memory.

## Sparse requests

A brief or one-sentence query is an underspecified brief, not an instruction to
make a thin deck. Honor every explicit constraint, then infer the missing
audience context, presentation purpose, decision, and useful scope from the
subject and Research. Record those choices in the resolved brief. Enrich the
argument with relevant facts, examples, contrasts, mechanisms, implications, and
next actions—not with generic filler or a needlessly encyclopedic detour.

Visual richness follows the information job. Build a deck-wide rhythm from
authentic imagery for identifiable subjects, generated imagery for authored
concepts or atmosphere, and HTML/CSS/SVG for data and relationships. A
typographic pause may be deliberate; repeated text blocks or card grids are not
the default merely because the original query was short. Query length alone
must not reduce evidence depth or visual ambition.

Make the palette and visual character explicit instead of inheriting a safe
fallback. Derive color from the subject's place, material, era, audience,
imagery, or emotional temperature. The content canvas may be chromatic, dark,
softly tinted, or paper-like; white/cream with navy is only one valid answer,
not the default definition of professionalism. Choose one coherent visual
character—such as high-chroma editorial, documentary, tactile print, luminous
technical, illustrated, or typographic—and let page variety grow inside it.

## Planning boundary

`plan/deck.md` is the one global contract. It records audience, objective,
delivery language, page count, narrative map, page rhythm, visual grammar,
special-page system, image direction, anti-default choices, and a short
`Theme Tokens` block.

The instruction edition never determines the presentation language. After
Research, Orchestrator resolves explicit or implicit language, page count,
audience, and visual-medium intent from the original request and records them,
with one-line rationale, in `## Resolved deck brief`. Those decisions then remain
stable for every page and role.

Each `plan/slide_NN.md` fixes:

- page role, page type/family, title chain, and narrative handoff;
- an editable content-page `composition` starting point with focal point,
  reading path, and primary/secondary region jobs;
- a structural `special_layout` for cover, divider, or closing pages;
- the audience conclusion or decision for the page;
- usable evidence and explicit assumptions;
- whether visual evidence is `required`, `preferred`, `code_only`, or `none`;
- the semantic visual need, constraints, and speech beat.

Orchestrator locks titles because their sequence carries the argument. It does
not write a component tree, exact geometry, decorative recipe, or the complete
body copy. Slide turns the light skeleton and evidence packet into a rich page.
Every page plan and delegation is self-contained. Encode cross-page continuity
as shared semantics or a short anchor; never ask Slide to inspect or reuse a
sibling page's HTML.

When a definition, method, finding, number, or conclusion from an attachment
carries the page's argument, it must appear on screen. Do not remove essential
content merely because `speech.md` explains it. Speech adds context and sources;
it does not replace what the audience must see. Academic, course, and paper
decks retain full layout, typography, and visual ambition without inventing evidence.

Use [plan-contract.md](references/plan-contract.md) for the Markdown structure.
Use `deck.md` decisions once; never copy the global contract into every page plan.

## Audience-facing content

Every visible line should help the audience understand, compare, remember, or
act. Never display production notes, evidence IDs, file names, template labels,
confidentiality filler, design commentary, or phrases such as “echoes the cover.”
Do not repeat the same message in the title, body, image label, and footer.

Full sources, URLs, publisher/year strings, and citation markers belong in
`speech.md`, not on the slide. Natural-language attribution may remain visible
only when source identity changes the audience's interpretation.

Color emphasis needs a visible reason: current state, key decision, risk,
conclusion, or chapter identity. Ordinary content pages share the deck-level
`--content-canvas`. Local panels, collage paper, charts, images, and accents may
vary freely. A full-canvas change is deliberate only when the page plan declares
a `canvas_variant`. A shared canvas does not imply white cards: use the authored
palette's tonal fields, image crops, outlines, texture, and type contrast.

## Special pages

Cover, section-divider, and closing use an independent full-canvas structure:

```text
special-canvas
├─ special-background
├─ special-overlay
└─ special-safe
```

Background photography, texture, SVG, and color fields reach all four edges;
readable copy stays inside `special-safe`. A special page never reuses the
content-page `slide-inner → page-frame → page-body` chain.

A cover normally has a meaningful Hero image, evidence object, or authored SVG
mechanism. Condense the title when possible; when meaning requires length, split
its hierarchy and select a composition that can hold it. Do not default every
deck to a top title or every short title to an empty centered canvas.

Dividers in one deck share numbering grammar, type roles, motif treatment,
color logic, and spatial character. They may alternate a small set of related
compositions. `special_layout` selects the stable header grid while `page_family`
keeps the subject-specific art direction free. `section_index` is numeric (`02`);
a chapter label appears once, not again in both number and eyebrow. A divider
carries a chapter name, one bridge line, and one motif—not body arguments or KPIs.

Include a closing unless the user explicitly declines it. Keep it concise and
unmistakably audience-facing: conclusion, thanks, Q&A, or truthful next step.
Do not add new arguments, fake contact details, production notes, or a prominent
physical page number.

## Images

Orchestrator states visual purpose and truth requirement in each page plan.
Image decides whether to search, generate, or leave a `code_only` need to
Slide. v0.2 does not use any attachment pixel, rendered document page, or crop
as a delivery asset. Attachments ground facts, data, captions, labels, and
visual semantics only through native text/OCR:

- known people, places, products, documents, works, events, and real cases:
  search and download authentic imagery;
- non-specific atmosphere or a deliberately authored concept: generation is
  allowed;
- data, process, architecture, and relationships: HTML/CSS/SVG;
- decoration without explanatory value: omit it.

Do not crop or directly reference figures/tables from paper or course
attachments, including any `inputs/` page image. Route from the title, caption,
subject, labels, and values recorded by OCR/extraction: search for the exact
visual on an official paper, project, or author page when a credible match is
available; generate a new explanatory image only for conceptual subjects; and
rebuild data, experimental results, and mechanisms in HTML/CSS/SVG from verified
values. A generated image must never pose as the paper's original figure,
experiment screenshot, or exact data graphic. When faithful reconstruction is
not possible, use bounded copy or a code visual and preserve the uncertainty.

Treat `preferred` as a positive visual commission when a truthful raster would
materially improve identity, setting, evidence, or atmosphere. Image should
attempt the most credible route and skip only with a concrete reason; Slide then
uses the planned fallback. This keeps image work purposeful without turning
asset counts into a quota.

One Image Agent handles the deck in a batch. Record each selected source, direct
download URL, and target path in the sole `assets/catalog.md`; run `fetch-images`
once for authentic imagery, then run `assets-finalize` and inspect one contact
sheet. Native portrait/landscape orientation is not a rejection when the intended
16:9 crop remains strong. Reuse an existing local path rather than downloading
the same image under another name.
Catalog paths are exact workspace-root-relative `assets/NAME.ext` values. Every
asset assigned to a page must visibly render from that exact path in final HTML;
never prepend `../`, hide the load, or leave only an asset ID.

A `required` page cannot pass build unless a resolved raster path is both assigned
in the catalog and actually referenced by its HTML. Transparency checkerboards
are inspected and, only when safely detected, removed by the supplied script.

## Slide refinement

Slide fills the page before its first render, then uses the real PNG for one
coordinated repair. It inspects the first successful render and every PNG made
by repair. Normal work uses those two inspections; the renderer retains at most
eight distinct states only as an emergency recovery ceiling, not an aesthetic
exploration budget. Distinct preflight failures count and unchanged states reuse
their PNG. A remaining hard defect triggers simplification or reporting, leaving
a legal recovery path for a later repair Agent.
Slide uses only `render . --page NN`; whole-deck build, rendering, and renderer
diagnosis belong to the finalization and Review path. A valid isolated page that
disagrees with a whole-deck capture is reported as a capture issue, not rewritten
to match the bad frame.

Only a visible defect justifies another cycle: clipping, collision, overflow,
broken media, severe unintended emptiness, unreadable hierarchy/contrast, or an
incorrect data relationship. Once the page is readable and correct, cosmetic
exploration is complete. A special-page problem shared by several pages is
reported for one shared fix before `finalize`, not explored independently by
each Slide.
The first visual inspection produces one consolidated `must_fix` list for one
coordinated repair. Stop as soon as verification passes; repeated reading,
reference searches, or a fresh composition must not bypass the stop line.

The shared transition applies to every page. GSAP is only for an explicitly
requested isolated live-demo whose stable first frame also works as a static slide.

## Review

Review is the final pixel owner. It inspects the whole-deck contact sheet once
and the special-page contact sheet once, then opens only pages flagged by those
passes or another concrete audit finding. It checks content-canvas continuity,
title logic, audience language, asset use, footer geometry, collisions, clipping,
and speech alignment. Before editing, it forms one complete issue inventory,
applies the safe repairs as one coordinated batch, and then finalizes once.
It classifies an unresolved issue as page authoring, shared system, or render
capture before routing it. `static` metadata alone is not a visual defect, and a
capture mismatch never justifies patching valid page HTML. Orchestrator does not
inspect unchanged pixels again after Review.
Deck length does not justify opening every page; if sheets and deterministic
audits flag nothing, Review returns ready without precautionary single-page checks.

## Commands

```bash
python skills/mural-presenter-v0-2-en/scripts/deck.py validate-plans . --expected N
python skills/mural-presenter-v0-2-en/scripts/deck.py scaffold-from-plans . --expected N
python skills/mural-presenter-v0-2-en/scripts/deck.py sync-speech . --expected N
python skills/mural-presenter-v0-2-en/scripts/deck.py fetch-images .
python skills/mural-presenter-v0-2-en/scripts/deck.py assets-finalize .
python skills/mural-presenter-v0-2-en/scripts/deck.py render . --page NN
python skills/mural-presenter-v0-2-en/scripts/deck.py finalize . --expected N
python skills/mural-presenter-v0-2-en/scripts/deck.py audit .
```

Read only the references needed for the page:

- planning: [plan-contract.md](references/plan-contract.md)
- special and content-page choices: [page-patterns.md](references/page-patterns.md)
- typography: [fonts-and-type.md](references/fonts-and-type.md)
- imagery: [materials-and-images.md](references/materials-and-images.md)
- charts and diagrams: [charts-and-diagrams.md](references/charts-and-diagrams.md)
- DOM contract: [html-contract.md](references/html-contract.md)
