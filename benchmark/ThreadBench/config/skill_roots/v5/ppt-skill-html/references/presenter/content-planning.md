<!-- Reference owner: Presenter. Allowed consumers: Presenter only. -->

# Content planning rules

## Ownership

Presenter owns what the deck says, the order in which it says it, the exact on-screen copy, the semantic visual need, and the first speech draft. It does not choose palette, typography, visual style, or HTML implementation.

## Planning sequence

1. Read the user brief and the complete `research/knowledge-brief.md`.
2. State one audience-specific thesis: what should this audience remember, believe, decide, or do?
3. For decks of eight or more pages, organize the argument into two to four acts with a reason for each transition.
4. Assign each page one job and one semantic page role.
5. Write `plan/narrative.md`, then write every self-contained `plan/slide_NN.md` and the first `speech.md` together.
6. Freeze the resulting content contract in `memory/content-memory.md` using stable `C-xx` IDs.

## Page contract

Every `plan/slide_NN.md` must contain:

- semantic role and one-sentence job;
- previous-page handoff, current claim, and takeaway;
- speech beat and transition intent;
- exact title, body, labels, values, and source line;
- supporting `M-xx`/`K-xx` provenance for factual content;
- semantic visual medium, subject, purpose, focal information, ratio, logical asset ID, and binding status;
- content that must not be dropped, duplicated, or revealed early.

Do not put internal provenance IDs or file paths in exact on-screen copy. Apply `references/shared/provenance-and-constraints.md`.

## Semantic page roles

Use the language of the deck, but preserve these distinct jobs:

- Cover: establish topic, occasion, and promise.
- Section Divider: open a new act with one promise.
- Opening/Context: establish the situation or question.
- Agenda: expose structure when the audience benefits from it.
- Problem: define the tension without prematurely giving the answer.
- Point/Argument: establish one explicit claim.
- Analysis: explain mechanism, causality, or structure.
- Data: make one evidence-backed finding visible.
- Case Study: make an argument concrete through a real example.
- Solution: specify the intervention.
- Plan/Timeline: specify sequence, owner, milestone, or output.
- Comparison: compare the same dimensions and reach a judgment.
- Summary: compress established conclusions.
- Call to Action: state who should do what and when.
- Closing: return to the thesis as a low-density final frame; it is not another summary.

Avoid adjacent pages with the same job unless the argument truly requires a sequence. The final page must be Closing and must not use generic thanks-for-listening language.

## Content density

- Keep one primary idea per page and usually no more than three supporting information groups.
- Convert real quantitative comparison into ECharts or a conclusion-led data visual.
- Use SVG for structure, mechanism, flow, or relation.
- Use real imagery for identity-sensitive subjects; use generated imagery only for conceptual or stylistic needs.
- Do not solve density by shrinking copy. Split or simplify the argument before implementation.

## Designer boundary

Describe what a visual must communicate, not how it should look. Do not specify hex values, typefaces, shadow recipes, decorative motifs, or a layout template. At Phase-1 Gate, consume Designer's contract and revise only Presenter-owned files when the content cannot fit the approved visual grammar.

