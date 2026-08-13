# Markdown planning contract

The canonical planning source is readable Markdown:

```text
plan/deck.md
plan/slide_01.md ... plan/slide_NN.md
```

There is no aggregate page JSON/YAML. Keep global decisions in `deck.md` and
page-specific evidence in the corresponding slide plan.

## `plan/deck.md`

````markdown
# Deck Plan
- title: Audience-facing deck title
- footer: Optional short footer

## Resolved deck brief
- language: zh
- page_count: 24
- audience: Primary listeners and decision makers
- image_mode: Mixed; authentic search for real subjects, code visuals for mechanisms
- rationale: One line explaining the choices inferred from the request.

## Audience and objective
Who is listening, who is speaking, what should change after the talk.

## Narrative and page map
The argument, sections, page roles/titles, handoffs, rhythm, peak pages, and the
primary evidence mode or visual event for each narrative page.

## Visual storyboard
For each page or consecutive page run, name the primary visual event: sourced or
generated imagery, data chart, mechanism, document evidence, decisive
typography, or an intentional pause. Check that the opening, chapter turns, and
content peaks each have an executable anchor so page-level planning does not
collapse into text panels.

## Visual contract
Palette premise and source cues; dominant visual character; subject-specific
color roles, type roles, grid, image treatment, content canvas, page-family
grammar, and defaults to avoid.

## Special pages
Cover Hero strategy; divider numbering/motif/type/spatial system; closing strategy.

## Theme Tokens
The values below demonstrate one chromatic route, not a reusable default.
Replace them from this deck's palette premise.

```css
:root {
  --content-canvas: #d9c7f3;
  --surface: #f3a6b8;
  --ink: #241532;
  --muted: #66536f;
  --accent: #e94f37;
  --accent-2: #007f73;
  --special-bg: #492a7a;
  --special-ink: #fff2a8;
  --font-display: var(--font-heavy);
  --font-body: var(--font-sans);
}
```
````

`Resolved deck brief` is the internal task resolution. Follow explicit user
requirements first. Infer only missing values from the request's primary
language, audience, complexity, and delivery setting. `image_mode` is a concise
strategy phrase, not a fixed enum; page-level visual needs remain more specific.
The Skill edition never determines `language`.

The Theme Tokens block contains custom properties only. The script merges it into
the prepared `base.css`; Orchestrator does not write structural CSS. Set the
actual deck colors deliberately; do not copy the illustrative values or leave
the prepared neutral fallback merely because it is available.

## `plan/slide_NN.md`

Use fixed English field names even when the values and narrative are Chinese:

```markdown
# slide_18
- role: Explain the accessible hardware system
- page_type: process
- page_family: arch-diagram
- composition: data-focus
- visual_evidence: code_only
- canvas_variant: base
- show_footer: true

## Narrative
What this page must make the audience understand and how it connects.

## Evidence
- Claim or fact, with boundary/assumption.
- Full source URL and date when applicable.

## On-screen copy (exact)
- eyebrow: SYSTEM
- title: One logical audience-facing title
- subtitle: Optional clarifying subtitle
- section_index:

## Semantic visual need
The relationship or object that should be seen and why. State authenticity and
title-safe-area needs, not a component tree or exact coordinates.

## Composition blueprint
- focal: The evidence, subject, number, or conclusion seen first.
- reading_path: The audience path from focal evidence to conclusion and support.
- primary: What belongs in the scaffold's primary region.
- secondary: What belongs in its supporting region without repeating the title.

## Render anchors
Only self-contained anchors that matter to continuity. State the shared
semantics or token; omit decorative recipes and never ask Slide to inspect or
“reuse Pxx.”

## Constraints
Only page-specific hard requirements or prohibited claims.

## Speech beat
Presenter explanation, caveat, and transition.
```

Required preamble fields:

- `role`;
- `page_type` as a lowercase kebab token;
- `page_family` as a lowercase kebab token;
- `visual_evidence`: `required`, `preferred`, `code_only`, or `none`.

Ordinary content pages should also state a `composition` and Composition
blueprint. If either is omitted, validation warns and the scaffold infers a
safe starting geometry from `page_type / page_family` rather than blocking the
deck. Explicit planning remains preferable because it preserves the intended
focal point and reading path. Choose from:

- `visual-split`: visual/evidence and explanation; add `.is-reversed` to flip;
- `data-focus`: dominant chart, map, or mechanism plus a narrow annotation rail;
- `comparison`: two genuinely comparable peer regions;
- `sequence`: timeline, process, or path plus a supporting conclusion;
- `matrix`: table, matrix, or checklist plus an explanatory takeaway;
- `editorial`: a strong claim or narrative text in asymmetric relation to evidence;
- `freeform`: only when the plan states a clear composition action not served by
  the other geometries.

The script supplies only regions and useful default proportions, never component
styling. Slide may reverse, overlap, resize, and restyle them. The purpose is a
first-draft reading path, not a repeated template.

Use `special_layout` only on special pages, where it is required:

- cover: `split`, `centered`, `lower-third`, or `poster`;
- section-divider: `number-copy`, `centered`, `visual-split`,
  `vertical-rail`, or `banded`;
- closing: `centered`, `split`, or `lower-third`.

`special_layout` selects only the stable structural header grid. `page_family`
remains a free subject-specific art direction, so a divider may combine
`special_layout: visual-split` with `page_family: archive-collision`.

`canvas_variant` defaults to `base`; use another token only for a purposeful
full-canvas change. `show_footer` defaults to true on content pages and false on
special pages.

`title` is required. `eyebrow` and `subtitle` are optional. For
`section-divider`, `section_index` is numeric only (`02`), never `CHAPTER 02`.
Cover, divider, and closing use page types `cover`, `section-divider`, and
`closing`; every other page type is an ordinary content page.

Substantive content pages need an Evidence section. Keep the decisive facts,
boundaries, and source URLs needed by `speech.md`, but do not paste long source
extracts or repeat the global visual contract. Slide never puts raw sources on
the canvas. The plan fixes the conclusion and evidence, not the complete final
body copy or layout. When the original request is sparse, enrich each page with
the concrete fact, example, contrast, mechanism, or implication that earns its
place; do not use generic bullets merely to make the page plan look complete.

## Commands

```bash
python skills/mural-presenter-v0-2-en/scripts/deck.py validate-plans . --expected N
python skills/mural-presenter-v0-2-en/scripts/deck.py scaffold-from-plans . --expected N
python skills/mural-presenter-v0-2-en/scripts/deck.py sync-speech . --expected N
```

`scaffold-from-plans` writes only missing HTML by default. It never overwrites
finished pages unless `--force` is explicitly supplied. Do not use `--force`
after Slide work has begun.
