<!-- Reference owner: Slide. Allowed consumers: Slide only. -->

# Slide implementation rules

## Inputs and authority

Implement one page from its Presenter plan and the distilled Designer contract:

- `plan/slide_NN.md` and `plan/deck.md` define exact content, page role, speech beat, sources, and asset paths.
- `plan/design-brief.md`, `plan/art-direction.md`, `memory/design-memory.md`, and `base.css` define the approved visual system.
- Do not read the raw Designer reference library. The art direction and memory are the implementation contract.

Never change facts, wording, source scope, global tokens, or the fixed skeleton. Adapt only the internal `.slide-body` composition when needed for clarity and fit.

## Required skeleton

The root must be `<section class="slide">` with exactly one direct `.slide-title`, `.slide-body`, and `.slide-footer`. Use `.slide--bleed` only for an approved full-bleed page. Keep page furniture, safe margins, and title anchors in `base.css`.

Use grid, flex, and approved arch classes for content flow. Absolute/fixed positioning is only for declared decoration, scrim, watermark, or bleed layers; never assemble body content with coordinates.

## Content implementation

- Copy Presenter on-screen text exactly; do not rewrite or add decorative labels.
- Keep sources inside the body safe area or the standard footer, never in a second overlapping footer.
- Use only stable local paths `../assets/by-id/<asset_id>.png`. A pending slot is a valid layout placeholder and never blocks HTML authoring; it remains advisory until atomically replaced.
- Keep every text size at or above `--fs-min`; solve density by layout or content-owner escalation, not illegible type.
- Use design tokens for all colors. Never add raw hex values in page markup.
- Include an explicit CJK-capable font stack for Chinese text.

## Media

- Real photo: preserve the subject; use `contain` when `cover` would destroy it, and apply only the treatment specified by art direction.
- Generated image: never depend on embedded generated text for factual copy.
- SVG: fill the intended visual region, use a responsive viewBox, readable labels, aligned nodes, and leader lines where needed.
- ECharts: load the pinned library version, give the container explicit dimensions, read series/text/grid colors from CSS tokens, and never use the default palette.
- Data labels and callouts must be anchored to the represented mark; remove floating annotations.

## Local adaptation

The plan's layout description is an intent, not a rigid raw-reference template. Within `.slide-body`, adjust column ratios, order, chart height, SVG viewBox, legend placement, and card grouping to remove clipping, dead space, or an undersized visual. Preserve the page's content and `D-xx` design constraints.

## Render loop

Run the goal's absolute `check_slide.py` with `--mode visual`; it owns render + lint + `checks/slide_NN.json` and skips Chromium when the HTML/CSS/asset hash is unchanged. Read the JSON business result; process exit status only indicates execution success/failure.

Mandatory early stop uses `effective_hard = check.hard + screenshot_confirmed_hard`. Stop after R1 when it is empty; run R2 only for named issues; run R3 only when R2 strictly reduces the count. Unconfirmed advisory, taste, uncertainty, and `ASSET_PENDING` never consume a new round. After an asset is atomically replaced, recheck only pages that reference it.

For a rework, back up the current page under `WORKSPACE_ROOT/tmp/slide_backups/`, compare the result, and restore the previous page if the revision is not better. Delete the backup and empty temporary directory before returning. Leave only official `slide_NN.html` files in `slides/`.
