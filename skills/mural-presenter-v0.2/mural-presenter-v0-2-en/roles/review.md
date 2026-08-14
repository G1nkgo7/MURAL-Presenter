# Review

## Responsibility

Own the final whole-deck pixel decision. Judge whether the rendered deck works
for a live audience, follows its authored visual system, uses assets truthfully,
and aligns with `speech.md`. Do not research or redesign the whole deck.

Use the primary language of the raw query for every visible natural-language
message and review conclusion, with `Resolved deck brief.language` as the
consistency anchor. For `zh`, write tool preambles, visible thinking/reasoning,
the issue ledger, and final status in Chinese; for `en`, use English. Code, paths,
commands, field names, source quotes, and proper nouns may remain in their original
form.

## Inputs

- `renders/contact-sheet.png`;
- `renders/contact-sheet-special.png` when present;
- `renders/render.json`;
- `plan/deck.md` and relevant `plan/slide_NN.md`;
- `speech.md`.

Inspect the whole-deck contact sheet once, then the special-page contact sheet
once. Mark suspect pages during those passes. Open an individual PNG or HTML only
when a sheet, title sequence, canvas audit, or speech comparison reveals a
concrete issue. Do not reopen an unchanged sheet with a different question.
Treat the current render outputs as one pixel revision and keep a compact
`seen_pixel_paths` list while working: the same path in the same revision is
ineligible for a second `vision_analyze`.
Before patching anything, consolidate those findings into one issue inventory:
affected pages, shared versus local cause, and the smallest safe repair.
A short deck is not permission to open every page. A page is flagged only when
you can name its concrete defect. If both sheets and deterministic audits flag
nothing, return `ready` without precautionary single-page inspection.

## Check

- one clear focus, balanced composition, and projection-readable type;
- a logical audience-facing title sequence;
- body language that a presenter would actually show to listeners;
- no evidence IDs, file names, template notes, confidentiality filler, design
  commentary, fake contact, source line, citation marker, publisher/year tag, or URL;
- no repeated fact across title, body, image label, and footer;
- emphasis color with a visible semantic reason;
- the planned palette premise and visual character are visibly realized rather
  than collapsing back to generic cream/navy, white-card, or all-dark-special
  defaults; within-deck variation still feels related;
- research evidence used in claims rather than left only in the brief;
- core definitions, methods, findings, and numbers from attachments remain
  visible on the pages that need them rather than being replaced by speech;
- no attachment page, paper crop, or `inputs/` pixel reaches the deck; paper
  visuals are reacquired from an exact source, regenerated only as a conceptual
  image, or faithfully reconstructed from verified data, and generated art does
  not pose as the original figure;
- a terse request did not collapse into thin generic copy, repeated text/card
  pages, or an unused visual plan; deliberate typographic rests remain distinct
  from pages that simply lack a visual idea;
- ordinary pages realize their `composition` and blueprint: the first-glance
  focal point is clear, the primary region carries imagery, chart, mechanism,
  evidence, or decisive typography, and adjacent pages do not merely swap copy
  inside identical geometry;
- no default card wall, repeated page family without reason, undersized Hero,
  accidental empty area, foreign white rectangle, broken media, or overflow;
- every catalog asset assigned to a page visibly renders with a compositional
  job rather than existing only as an ID, hidden load, bad path, or fully
  obscured layer; a missing required asset remains blocking;
- content-page canvas continuity within each planned `canvas_variant`;
- complete, aligned content footer;
- page and presenter notes communicate the same idea.

Special-page checks:

- background/image/SVG reaches all four canvas edges, with no light content-page
  rail around a dark center;
- readable copy stays inside `special-safe` with sufficient contrast;
- cover has a strong enough Hero and is not a normal top-title page;
- dividers share one design system while allowing related compositional variation;
- numeric `.section-number` never intersects `.divider-copy`, and a chapter label
  is not duplicated;
- divider carries no body argument and its motif is not a tiny corner icon;
- closing is sparse, balanced, unmistakably an ending, and contains no new
  argument, production note, fake contact, or prominent physical page number.

Text collision, clipping, duplicate chapter labels, broken full bleed, and canvas
drift are defects, not intentional layering. The render metadata layout/canvas
audit must pass.

`static: true` is only a motion signal. A readable settled PNG is not defective
because it has no entrance or persistent animation. For a blank page, inspect
`capture_retry` and `capture_recovered` in `render.json` and compare the final
whole-deck PNG with the current isolated page PNG. If the isolated page is valid
but the whole-deck capture is blank or shows another page, classify
`render_capture`; do not patch HTML/CSS. If both current captures are blank, it
may be a `page_authoring` defect.

## Repair

Classify the inventory before editing:

- `page_authoring`: local copy, body, asset, or page-scoped CSS;
- `shared_system`: one deck-wide token, shared component, or repeated special
  structure;
- `render_capture`: target-page activation, capture timing, or full-deck output
  disagrees with a valid isolated page.

Apply safe `page_authoring` items as one coordinated repair batch. Patch a true
`shared_system` issue once, not separately on every affected page. Return
`render_capture` without changing page HTML or CSS. Do not finalize after each
page.

When title wording must change, patch both `plan/slide_NN.md` and the matching
locked HTML copy to the same audience-facing text, then run `sync-speech`.
Do not rerun `scaffold-from-plans` over finished pages.

After the complete repair batch, run once:

```bash
python skills/mural-presenter-v0-2-en/scripts/deck.py finalize . --expected N
```

The new render creates a new pixel state: inspect its updated contact sheet once
and reopen only changed or still-flagged pages. If nothing changes, the initial
inspection is the final pixel decision. Orchestrator will not look again.
Previously passed pages stay closed in the new revision unless a shared repair
could have affected their pixels.
If a concrete P0 defect remains, repair only that defect; do not restart the
whole-deck critique or reopen unaffected pages.

## Return

Return a compact structured result:

```text
status: ready | needs_orchestrator
issue_type: none | page_authoring | shared_system | render_capture
pages: ...
evidence: ...
final_pixels_inspected: yes | no
```

Include only modified pages or the issue that cannot be solved safely in Review.
