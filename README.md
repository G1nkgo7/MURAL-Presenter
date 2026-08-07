<p align="center">
  <img src="assets/logo/exports/mural-logo-lockup-light.png" width="780" alt="MURAL logo">
</p>

<p align="center">
  <strong>Multi-Agent Unified Revision-Aware Authoring for Long-Horizon Presentations</strong>
</p>

<p align="center">
  <a href="README.md">English</a> · <a href="README_zh-CN.md">简体中文</a>
</p>

> [!NOTE]
> This repository is currently a **research preview**. It publishes the project narrative,
> system diagrams, and brand assets first. The implementation, MURAL Authoring Skill,
> THREAD-Bench cases, training data, checkpoints, and measured results will be released only
> after their versions and public-use boundaries are frozen.

## What is MURAL?

MURAL is a Skill-driven multi-agent framework for the full lifecycle of editable HTML
presentations. It treats presentation creation as an authoring process rather than a one-shot
rendering problem: understand the request, ground materials and facts, plan the deck, prepare
assets, author related slides, inspect rendered pixels, review the assembled deck, revise it,
and export the result.

Here, **long horizon** refers to the distance over which a decision must remain valid across
stages, slides, and revision turns. An audience assumption set at the beginning may shape an
entire deck; a definition introduced on slide 3 may be consumed on slide 18; a later edit may
need to update several related slides without disturbing the rest.

## Why another execution unit?

Single-agent authoring preserves one continuous context, but its execution history grows with
the deck and later edits. Per-slide parallelism shortens each trajectory, but moves cross-slide
relationships into state handoffs between independent workers. MURAL inserts a middle unit:
the **slide group**.

Slides that share a narrative responsibility, visual system, asset series, or explicit
dependency are assigned to one Group Agent for joint authoring and inspection. Different groups
can still run in parallel. After assembly, a whole-deck review restores the global view.

<p align="center">
  <img src="assets/figures/execution-topologies.png" width="100%" alt="Sequential, full-context parallel, and MURAL execution topologies">
</p>

## Full-lifecycle authoring

MURAL externalizes the lifecycle as the reusable **MURAL Authoring Skill**:

1. **Material and research** organize optional attachments and close decision-relevant evidence gaps.
2. **Plan and compile** establish a shared deck blueprint, design system, page map, and complete slide groups.
3. **Group authoring** uses one centralized image stage, then lets Group Agents jointly author and inspect related slides.
4. **Review, revise, and deliver** assemble the deck, run whole-deck review, route later edits by impact scope, and export deliverables.

<p align="center">
  <img src="assets/figures/authoring-lifecycle.png" width="100%" alt="MURAL full authoring and revision lifecycle">
</p>

The revision router selects the smallest reliable scope:

- **Page patch** for a precisely local change.
- **Group replay** when several related slides or their shared visual language must change.
- **Deck replan** only when the request changes deck-level decisions or structure.

## Why HTML?

MURAL uses HTML/CSS/SVG as its authoring source so that text, layout, graphics, media, and
interactions remain separately addressable. Browser rendering provides the pixel-level ground
truth for inspection, while the structured source supports targeted revision. PPTX, PDF, and
images are delivery adapters; export fidelity is evaluated rather than assumed.

## Evaluation

We are developing **THREAD-Bench** — *Tracking Holistic Requirements and End-to-End Alignment
in Decks* — to evaluate both process evidence and the final artifact. Its intended scope includes
knowledge accuracy, deck- and page-level presentation quality, and case-specific long-range
dependencies. PresentBench and SlidesGen-Bench cover complementary generation quality, while
DECKBench supplies multi-turn revision tasks.

No MURAL effectiveness number is public yet. See [Release status](docs/release-status.md) for the
evidence boundary and [THREAD-Bench overview](docs/thread-bench.md) for the planned protocol.

## Repository map

| Path | Contents |
| --- | --- |
| [`assets/logo/`](assets/logo/) | Primary mascot, compact mark, lockups, and reproducible exports |
| [`assets/figures/`](assets/figures/) | Publication figures in PNG and PDF |
| [`docs/`](docs/) | Method, benchmark, branding, and release notes |
| [`blog/`](blog/) | Platform-neutral English and Chinese launch articles |
| [`site/`](site/) | Deployable bilingual project site and blog |
| [`tools/`](tools/) | Deterministic builders for public visual assets |

## Project status

| Component | Status |
| --- | --- |
| Public narrative and system diagrams | Available |
| Brand system | Available |
| Paper manuscript | In preparation |
| MURAL Authoring Skill | Pending version freeze |
| THREAD-Bench | Pending schema and judge calibration |
| Training data and checkpoints | Pending reproducibility and release review |
| Experimental results | Not yet public |

## Contributing

The most useful contributions during the preview phase are corrections to the public description,
reproducible failure cases, benchmark-case proposals, accessibility feedback, and export-fidelity
reports. Please read [CONTRIBUTING.md](CONTRIBUTING.md) before opening an issue or pull request.

## Citation and license

Citation metadata, authorship, the paper URL, and licenses will be added when the corresponding
artifacts are made public. Until then, the absence of a license does **not** grant permission to
redistribute or reuse unreleased implementation or data.

