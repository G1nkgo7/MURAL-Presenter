---
title: "A presentation is not a stack of slides: introducing MURAL"
description: "A full-lifecycle, revision-aware approach to long-horizon presentation authoring."
date: 2026-08-07
language: en
status: research-preview
---

# A presentation is not a stack of slides

## Introducing MURAL, a full-lifecycle approach to long-horizon presentation authoring

Making a presentation rarely starts with a blank slide and ends when the last page appears. A real
authoring process begins earlier: deciding what the talk is for, who will hear it, and what they should
remember. It moves through source material, fact checking, image collection, narrative planning, and
art direction. Slides are then written, composed, rendered, and inspected. When the deck finally looks
complete, someone reads it from beginning to end and notices that a definition has changed, a section
arrives too abruptly, or a visual convention has drifted. The deck is revised, sometimes once and
sometimes over several rounds.

Current presentation agents are surprisingly good at individual moments in this process. They can
draft an outline, retrieve information, write a slide, generate code, or respond to a local edit. The
harder problem is keeping decisions alive across the whole chain. A term introduced near the opening
may return much later. A color can acquire a meaning that every subsequent chart must preserve. The
closing slide may need to answer a question posed twenty pages earlier. A post-generation request can
look local while quietly changing several related pages.

We call this a long-horizon authoring problem. The horizon is not simply the number of slides or the
time required to generate them. It is the distance over which a fact, narrative promise, design rule,
or user requirement must remain valid across stages, pages, and revisions.

MURAL — Multi-Agent Unified Revision-Aware Authoring for Long-Horizon Presentations — is our attempt to make that lifecycle explicit.
It is a Skill-driven multi-agent framework for editable HTML presentations, built around a shared deck
blueprint, slide-group ownership, rendered inspection at two levels, and revision routing by impact
scope.

![The MURAL authoring lifecycle](../assets/figures/authoring-lifecycle.png)

## The missing unit between a deck and a slide

There are two intuitive ways to organize an agentic presentation system. One agent can carry the deck
from beginning to end. That gives the work one owner and one continuous context, but the trajectory
accumulates research notes, planning decisions, tool observations, slide code, render feedback, and
later edits. The alternative is to split the work across functional roles or independent slide agents.
This makes specialization and parallel execution easier, but relationships between slides now depend
on state being transferred and verified across separate contexts.

Neither topology is wrong. They place responsibility in different locations.

MURAL introduces a middle unit: the slide group. Slides belong to the same group when they share a
narrative responsibility, a visual family, an asset series, or an explicit dependency. A cover and its
closing response can form a group even though they are not adjacent. Several steps in one argument can
share a producer. Section transitions can be treated as one visual system. Every slide belongs to one
complete, non-overlapping group, and different groups can still run in parallel.

The Group Agent does more than produce several files. It writes all pages in its assignment, renders
them together, examines a group contact sheet, consolidates the defects it can observe, and revises the
affected pages. The group shares design DNA and narrative responsibility, not a rigid geometric
template. Its pages should feel related without becoming copies of one another.

![Three execution topologies](../assets/figures/execution-topologies.png)

This distinction matters because giving every page worker a copy of the complete deck context does not
create a common owner for a cross-slide relationship. Assembly is also not the differentiator; every
parallel system eventually needs to merge its pages. MURAL changes where related work is jointly
produced and where it is verified after merging.

## Turning the lifecycle into an executable Skill

The MURAL Authoring Skill describes stages, roles, persisted artifacts, quality gates, and continuation
paths. It begins with the user brief and optional materials. A Material role turns attachments into a
location-grounded account that later agents can use without reopening the original files. Research is
invoked only when an external fact or unresolved term can change the deck’s conclusion. This makes
grounding a deliberate decision, not a ritual applied to every prompt.

The Orchestrator then fixes the audience, communicative goal, narrative arc, terminology, visual
direction, page map, asset strategy, and production groups. These decisions are written into a shared
deck blueprint and compiled into group and page briefs. Downstream agents do not need to reconstruct
the whole talk from a growing dialogue; they receive the decisions relevant to their responsibility.

When the deck needs bitmap assets, one centralized Image stage resolves them before group authoring
begins. That choice is intentionally simple. If every group searches or generates images independently,
the deck can acquire competing asset catalogs, unrelated visual series, and duplicated work. A shared
stage gives the entire deck one resolved set of local assets, while Group Agents remain responsible for
how those assets participate in composition.

Group authoring closes the first visual loop. Each group produces a complete first draft, renders the
group, inspects the latest pixels, combines the visible problems, revises, and renders again. Once all
groups finish, deterministic assembly creates the authoritative deck and a whole-deck contact sheet.
Whole-deck Review then checks relationships no group can judge alone: numerical conventions,
terminology, setup and response, design semantics, section rhythm, special pages, and alignment between
the slides and their speaker notes.

This is why MURAL is more than a set of prompts. The durable object is the authoring procedure: which
role owns which decision, what becomes a file, when pixels become evidence, and how the system resumes
after a failure or an edit.

## Revision is part of authoring, not an afterthought

A generated presentation remains useful only if people can change it. MURAL routes later requests by
their actual impact. A precise, local request can patch one page and rerender it. A request that changes
the relationship or visual language of several related pages reactivates the corresponding slide
group. A change that needs new evidence or imagery adds only those stages and the affected groups. The
system replans the deck when the request alters sections, title structure, slide count, shared design
rules, or several groups.

The point is not to perform the smallest possible edit at any cost. It is to select the smallest scope
that can preserve the deck’s decisions reliably. When the impact is uncertain, the router escalates.
This turns revision into a continuation of the original authoring process instead of an unrelated patch
mechanism.

Our training hypothesis follows the same idea. Generation trajectories already contain repeated
inspect, diagnose, patch, and rerender behavior. Those trajectories may support post-generation
revision even without a separate edit-only training set. That possibility is useful, but it remains a
hypothesis until controlled experiments separate the effect of refinement traces from the base model,
data, and runtime.

## Why the source is HTML

An image-only slide can be visually rich, but it collapses text, layout, and graphics into pixels.
Changing a number, moving one annotation, preserving selectable text, or adding an interaction often
requires regeneration or reconstruction. MURAL keeps HTML, CSS, SVG, text, and media as structured
source, while treating browser rendering as the visual truth used for inspection.

That representation creates a practical bridge between generation and editing. An agent can patch a
specific element, render the page, and inspect the actual result. It also leaves room for future visual
editors and web-native interactions. HTML is not declared universally superior: PPTX export can lose
CSS behavior, fonts, animation, or editable structure. PDF and image exports have different trade-offs.
MURAL treats those formats as delivery adapters whose fidelity must be measured.

## Evaluating the relationships that matter

Per-slide quality remains necessary, but it cannot tell us whether a deck kept its promises. A set of
attractive pages can still use one metric in two incompatible ways, abandon an opening question, or
change the meaning of a color halfway through.

We are developing THREAD-Bench — Tracking Holistic Requirements and End-to-End Alignment in Decks —
to make those relationships observable. The planned 50-case benchmark separates process evidence,
knowledge checks, deck-level presentation requirements, page-level quality, and case-specific
long-range probes. Each long-range probe records where a decision is established, which distant targets
must consume or preserve it, and what observable predicate closes the relation. A dependency is
considered closed only when every required target satisfies it.

THREAD-Bench will sit alongside existing evaluations rather than replace them. PresentBench and
SlidesGen-Bench provide complementary measures of grounded generation, content, aesthetics, and
artifact structure. DECKBench supplies multi-turn editing tasks. Together they let us ask whether MURAL
improves long-range consistency without sacrificing the quality of individual slides or the ability to
carry out later edits.

## Where the project stands

Today’s release is a research preview. The public repository contains the project narrative, the system
figures, the MURAL brand system, and bilingual documentation. The grouped workflow, centralized asset
stage, group-level inspection, whole-deck review, and scope-aware revision routes have been implemented
in the working system, but the experimental code and Skill still need a clean version freeze.

The training set, canonical THREAD-Bench cases, judge calibration, model checkpoints, and effectiveness
results are not public yet. We are deliberately leaving those claims open instead of filling the launch
page with provisional numbers. Their release requires reproducible configurations, traceable run
ledgers, redistribution review, and licenses appropriate to each artifact.

MURAL starts from a simple premise: a presentation is not a pile of independently acceptable slides.
It is a designed argument that persists through evidence, planning, production, review, and change. The
system should be organized around that lifecycle too.
