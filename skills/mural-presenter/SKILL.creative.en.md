---
name: mural-presenter-creative
description: High-agency MURAL Presenter authoring profile that keeps deterministic attachment, font, asset, render, and delivery checks while returning narrative, copy, and composition authority to strong models.
---

# MURAL Presenter · Creative / Teacher Profile

This profile is for strong authoring models. The runtime owns facts, provenance, files, pixel freshness, and delivery. Agents own narrative, art direction, audience copy, and composition.

## 1. Engineering foundation

Deliver `plan/grounded-knowledge.md`, `plan/design-brief.md`, `plan/deck.md`, every `plan/slide_NN.md`, `base.css`, per-page HTML/PNG, `speech.md`, and `present.html`.

- With attachments, delegate Material per attachment. It reads `subagents/material.en.md`, runs `stage_materials.py`, and preserves full text/OCR coverage, source locators, and the priority ledger.
- Delegate at most one Research when external facts can change the conclusion.
- Bitmaps enter `assets/catalog.json` through Image. Slide never searches, generates, or invents asset paths.
- Use ECharts for data. Keep accurate words in HTML. Extract paper Figures at high resolution from the source PDF; never present a full paper page as the Figure.
- Final pages pass the existing font, render, player, provenance, attachment, and `deck.py build/audit` checks.
- Existing-deck changes still follow `references/editing-contract.md` and preserve unaffected pages.

## 2. Creative authority

The Orchestrator is the deck's Art Director. Read all of `references/creative-direction.en.md`, then only the references needed for this case: `planning-contract.md`; design-rules §4–5 and layout-patterns §9 for charts/mechanisms/SVG; `fonts.md` when font verification matters; relevant quality-checklist sections at review time.

Resolved systems and the style catalog are optional vocabulary, not forms. Do not fill a system ID for compliance or let a style number replace a theme-specific visual idea.

Derive one `visual_thesis` from the topic, evidence, audience, occasion, and real imagery. Establish a sourced `palette_anchor`, two to four page families, type voices for title/body/numbers/notes, one adaptable signature motif, and a rhythm across imagery, data, mechanisms, typography, and breathing pages. Style Lock freezes this design DNA and its exclusions, not per-page geometry.

## 3. Evidence and attachments

After Material/Research, immediately write `plan/grounded-knowledge.md`. Preserve every attachment `priority_id / screen_priority / source_locator / fidelity_form`. User-named content, main conclusions, key numbers/relationships, and decision evidence are `must_present`.

Every `must_present` item maps to at least one page plan and appears in final pixels as readable copy, chart, Figure, or diagram. Speech may expand it but cannot fulfill it.

Attachments define evidence and candidate visuals, not visual quality or an image quota. Reuse clear irreplaceable Figures, people, products, works, or experimental outputs. Omit poor or text-heavy images while retaining their facts. When attachment visuals are weak, use better real/generated imagery or rebuild relationships with SVG/Canvas/ECharts. Academic and teaching decks remain designed for live presentation rather than imitating the paper.

## 4. Plan facts; do not pre-render the page in prose

The Orchestrator freezes the title chain, evidence assignment, visual storyboard, and page plans. Each page plan includes all machine fields required by the current runtime plus a light creative packet: page job and audience takeaway; fixed title; must-show facts, numbers, boundaries, and `attachment_priority_ids`; optional support; one semantic visual goal with ready assets and crop protection; suggested first focus/reading action; speaker beat; and one unique `production_group`.

Freeze content invariants, not complete sentences, card counts, component trees, or pixel instructions. Slide may compress, merge, split, and rewrite body copy without changing the title, facts, numbers, qualifiers, provenance boundaries, or must-show meaning.

**Every page has its own production group and its own Slide Agent.** A delegate call may launch many pages in parallel, but each task owns exactly one page.

```bash
python ${SKILL_DIR:-skills/mural-presenter}/scripts/deck.py prepare . --expected <N>
```

## 5. Media and page production

Choose the most truthful medium: real subject→real image; concept/atmosphere→generated bitmap; data→ECharts; static mechanism/relationship→large SVG; auto-layout or long labels→Canvas + HTML; typography-only only when language itself forms the visual event.

Resolve Image and catalog before starting pages that consume bitmaps. Do not add images by quota, but do not silently reduce named people, products, works, scenes, or principal paper Figures to icons and white cards.

Each Slide reads `subagents/slide.creative.en.md`. It owns body copy and body composition for one page and never reads sibling HTML. Workflow: complete draft → render → Vision → one consolidated repair; if technically clean but visibly under-directed, allow one named aesthetic-completion pass. Keep the better verified pixel state.

## 6. Review and delivery

After all pages, keep one Review owner using `subagents/review.creative.en.md`. If the first Review returns blocked, the runtime may let that same owner re-verify within its bounded recovery budget; never run multiple Review owners in parallel. Review judges whether the visual thesis is visible; pages share a world without copying geometry; cover/dividers/peaks/closing create live rhythm; primary visuals have weight; attachment priorities are visible; and the deck has not collapsed into white cards, document screenshots, or purposeless whitespace.

Review maintains `_trace/review-issues.md` and, for grounded work, `_trace/content-fidelity.md`, then performs at most one consolidated repair. A need for new evidence or assets returns blocked to the original single-page agent.

```bash
python ${SKILL_DIR:-skills/mural-presenter}/scripts/deck.py build . --expected <N>
```

Inspect the post-build final contact sheets, then return a concise delivery summary.

## 7. Red lines

Never invent facts, numbers, quotes, identities, paper results, or sources. Never remove attachment conclusions because speech covers them. Never display internal IDs, paths, group names, design notes, or build status. Never use generated images as data charts or accurate text evidence. Never damage a strong composition merely to clear a non-visible lint warning. “Professional,” “academic,” and “minimal” never mean low design completion.
