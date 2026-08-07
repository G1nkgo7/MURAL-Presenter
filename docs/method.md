# Method overview

MURAL — **Multi-Agent Unified Revision-Aware Authoring** — models presentation creation as a
full lifecycle with persistent, externalized state.

## Long-horizon state

The relevant horizon is not runtime alone. It is the distance over which a fact, term, narrative
setup, visual encoding, audience assumption, or task requirement remains binding across stages,
slides, and revisions. MURAL therefore asks three questions:

1. Which decisions must be deck-wide?
2. Which related slides need one accountable producer?
3. Which failures can only be detected after the deck is assembled?

## Shared deck blueprint

The Orchestrator resolves audience, purpose, language, length, evidence requirements, narrative
structure, art direction, page map, asset board, and complete production groups. These decisions are
persisted as a shared deck blueprint and projected into concise group and page briefs rather than
being repeatedly inferred from a growing conversation.

## Slide groups

A slide group is an execution and responsibility unit between one page and the complete deck.
Grouping may follow a narrative sequence, bookends, section transitions, a shared visual family,
an asset series, or an explicit cross-slide dependency. Groups are complete and non-overlapping;
different groups may run in parallel.

Each Group Agent writes all pages in its assignment, renders them together, inspects a group contact
sheet, consolidates defects, revises the affected pages, and verifies the latest pixels. Grouping
shares design DNA and narrative responsibility, not a fixed geometric template.

## Two-level visual closure

Group-level inspection can close continuity and rhythm within a related sequence. It cannot verify
every deck-wide relationship. After deterministic assembly, whole-deck Review checks factual
conventions, terminology, setup and response, visual semantics, special pages, and global rhythm.

## Revision routing

Later requests are classified by impact scope:

- `local_revision`: targeted page and element changes with no shared-system impact;
- `group_revision`: relationships or visual language within one slide group;
- `evidence_revision`: new facts or assets plus the affected groups;
- `global_revision`: section, title chain, shared system, page count, or multi-group changes.

Uncertain requests escalate to the next reliable scope. The goal is not to minimize work at any cost,
but to avoid restarting the complete lifecycle for a change that can be safely localized.

## Structured HTML source

HTML/CSS/SVG keep text, layout, media, and graphics separately addressable and make browser pixels
the inspection target. Delivery adapters can export PPTX, PDF, and images, but each adapter has its
own fidelity and editability limits.

