<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo/exports/mural-logo-lockup-dark.png">
    <img src="assets/logo/exports/mural-logo-lockup-light.png" width="1000" alt="MURAL Presenter — Multi-Agent Unified Revision-Aware Authoring for Long-Horizon Presentations">
  </picture>
</p>

<p align="center"><strong>Lifecycle authoring for editable, long-horizon presentations.</strong></p>

# MURAL Presenter

Paper implementation for **Multi-Agent Unified Revision-Aware Authoring for Long-Horizon Presentations**.

This repository publishes the 0919 authoring Skill, its paired Harness and the WebUI.
The 254 core source files are byte-identical to
`mural-presentbench-qwen35-9b-20260919T163556Z`. Deployment paths are resolved at launch.

[简体中文](README_zh-CN.md) · [Quick start](#quick-start) · [WebUI guide](webui/README_EN.md) · [Release scope](#experiment-scope) · [Contributing](CONTRIBUTING.md)

## The name is the method

| Letter | Stands for | Method implication |
| :---: | --- | --- |
| **M** | **Multi-Agent** | Specialist roles can run in parallel without making every slide an isolated task. |
| **U** | **Unified** | A shared deck blueprint carries audience, evidence, terminology, narrative, and design decisions. |
| **R** | **Revision-Aware** | Work resumes at page, group, or deck scope according to the edit's actual impact. |
| **A** | **Authoring** | The reusable object is an executable lifecycle, not a one-shot image-generation prompt. |
| **L** | **Long-Horizon Presentations** | Decisions must survive across stages, distant slides, and later revision turns. |

> **MURAL does not merely split a long workflow. It aligns agent ownership with the deck's
> dependency structure, then restores whole-deck closure after parallel production.**

## At a glance

**MURAL-Presenter** (MURAL for short) is a Skill-driven multi-agent framework for the full lifecycle of editable HTML
presentations. It treats presentation creation as an authoring process rather than a one-shot
rendering problem: understand the request, ground materials and facts, plan the deck, prepare
assets, author related slides, inspect rendered pixels, review the assembled deck, revise it,
and export the result.

Here, **long horizon** refers to the distance over which a decision must remain valid across
stages, slides, and revision turns. An audience assumption set at the beginning may shape an
entire deck; a definition introduced on slide 3 may be consumed on slide 18; a later edit may
need to update several related slides without disturbing the rest.

- **Shared state, scoped views.** Global decisions are externalized once and projected into
  group and page briefs instead of being reconstructed from a growing conversation.
- **Slide-group ownership.** Narratively or visually dependent pages have one accountable
  producer and one joint render–inspect–revise loop, even when the pages are non-contiguous.
- **Revision-aware continuation.** Later edits reuse the same lifecycle and restart only the
  smallest scope that can reliably preserve deck-level decisions.

## The missing middle: deck → slide group → page

Single-agent authoring preserves one continuous context, but its execution history grows with
the deck and later edits. Per-slide parallelism shortens each trajectory, but moves cross-slide
relationships into state handoffs between independent workers. This is a responsibility problem,
not merely a scheduling problem. MURAL inserts a middle unit: the **slide group**.

Slides that share a narrative responsibility, visual system, asset series, or explicit
dependency are assigned to one Group Agent for joint authoring and inspection. Different groups
can still run in parallel. After assembly, a whole-deck review restores the global view.

<p align="center">
  <img src="assets/figures/execution-topologies.png" width="100%" alt="Sequential, full-context parallel, and MURAL execution topologies">
</p>

<p align="center"><em>Figure 1. MURAL assigns dependency-related slides to shared Group Agents and restores whole-deck closure after parallel authoring.</em></p>

## Full-lifecycle authoring

MURAL externalizes the lifecycle as the reusable **MURAL Authoring Skill**:

1. **Material and research** organize optional attachments and close decision-relevant evidence gaps.
2. **Plan and compile** establish a shared deck blueprint, design system, page map, and complete slide groups.
3. **Group authoring** uses one centralized image stage, then lets Group Agents jointly author and inspect related slides.
4. **Review, revise, and deliver** assemble the deck, run whole-deck review, route later edits by impact scope, and export deliverables.

<p align="center">
  <img src="assets/figures/authoring-lifecycle.png" width="100%" alt="MURAL full authoring and revision lifecycle">
</p>

<p align="center"><em>Figure 2. The MURAL Authoring Skill spans material grounding, deck planning, group authoring, whole-deck review, impact-aware revision, and multi-format delivery.</em></p>

The revision router selects the smallest reliable scope:

- **Page patch** for a precisely local change.
- **Group replay** when several related slides or their shared visual language must change.
- **Deck replan** only when the request changes deck-level decisions or structure.

## Quick start

Use Python 3.12 and `uv`. Provide your own model endpoint and credentials.

```bash
uv sync --project webui/studio --frozen
uv sync --project webui/inference --frozen
cp webui/.env.example webui/.env
bash webui/start.sh
```

Copy the example only on first setup; preserve an existing `.env`.
Open the address printed by the launcher, normally `http://127.0.0.1:8001`.
Configure the model in the UI or deployment environment. On Windows use `webui/start.ps1`.
The first launch can install Chromium. It creates ignored `configs/runtime.local.json`
without overwriting an existing configuration. Edit that file for concurrency and ordered
GPU/CPU MinerU endpoints, or select an existing file with `MURAL_RUNTIME_CONFIG`.
PDF/Office/image attachments require MinerU; text-only tasks do not require an attachment parser.
Search and image generation require separately configured providers.

## Command-line inference

```bash
webui/inference/.venv/bin/python scripts/run_inference.py \
  --query "Create a six-slide research presentation" \
  --workspace "$PWD/artifacts/example"
```

Use `--help` for arguments or `--doctor --json artifacts/doctor.json` for diagnostics.
Use a new workspace; preserve historical workspaces and their pinned runtime.

## Why HTML?

MURAL uses HTML/CSS/SVG as its authoring source so that text, layout, graphics, media, and
interactions remain separately addressable. Browser rendering provides the pixel-level ground
truth for inspection, while the structured source supports targeted revision. PPTX, PDF, and
images are delivery adapters; export fidelity is evaluated rather than assumed.

## Repository layout

```text
MURAL-Presenter/
├── README.md / README_zh-CN.md
├── skills/mural-presenter-v1/       # Frozen authoring Skill and tools
├── harnesses/mural-presenter-v1/    # Frozen inference and delivery runtime
├── webui/                          # UI, server, dependency locks and launchers
├── assets/                         # Public logos, icons and method diagrams
├── configs/                        # Runtime template, MinerU and release provenance
├── fonts/                          # Bundled rendering fonts
├── scripts/                        # Portable CLI and configuration adapters
├── tools/                          # Source verification and integration freezing
├── tests/                          # Public integration regression tests
└── .github/                        # CI and contribution templates
```

The core directory names and internal assets, references, docs and tests are part of its frozen
identity. Keep them intact. Public logos and diagrams live in `assets/logo/exports/` and
`assets/figures/`; design drafts remain local. Root-level `benchmark/`, `docs/`, `examples/`,
`paper/`, training material and the independent `site/` are not included in this release.
Deployment credentials, environments, databases and generated cases remain local and ignored.
Necessary usage, provenance and validation information is consolidated in these READMEs.

## Source provenance

The active manifest is `configs/releases/mural-paper-v1.json`.
`configs/releases/upstream-0919/` retains the original 319-file publication inventory,
its pin and its release hashes. Historical inventory entries record provenance; they do not
require publishing the original internal pipeline configurations or benchmark scripts.

All 55 Skill and 199 Harness files retain the original combined SHA-256:
`378c3ac7fa26d06b91f5408c7a25e7350b9b37e712611a322e387ae13a2d64e3`.
Historical examples and links inside that frozen source may refer to the original environment;
use this README's portable launch instructions. Two unreadable upstream WebUI tests were not
imported. The portable integration has a separate release identity and digest.

```bash
python tools/check_paper_release.py
```

This verifies the frozen core, active integration and tracked release boundaries.
Do not refresh core hashes to conceal source changes. Integration updates require their own
review, tests and manifest; see [Contributing](CONTRIBUTING.md).

## Experiment scope

This is the runtime source release, not the complete experiment artifact package.
The reference 0919 experiment used Qwen3.5-9B, thinking enabled, Critic/search/image generation
disabled, 238 PresentBench cases and at most three generation attempts per case.
These settings do not define every WebUI model's defaults.

Score reproduction additionally requires the exact inputs and checksums, model weights and
chat template, evaluation adapter, font shim, Judge settings and result coverage records.
Benchmark suites, training pipelines, checkpoints and paper result tables are not bundled here.
The original evaluation font inventory was larger than this repository's font bundle.
Running the application or passing source checks does not reproduce the paper's scores.

## Validation and known limits

CI commands are in [.github/workflows/public-surface.yml](.github/workflows/public-surface.yml).
They cover portable configuration, HTTP and `/studio` routing, release/configuration contracts,
and active WebUI configuration. Use isolated test data, not an existing Studio database.

The compact public tree was checked on Linux on 2026-10-10: 10 portable/source-digest tests,
95 release/configuration tests and 139 WebUI tests passed; one browser test was skipped.
The example launcher preflight passed. These checks used an isolated Git worktree and existing
locked-dependency test environments, without model requests or production data.

The complete historical suite has not passed. The frozen source retains these known failures:

- `test_agent_runtime.py::test_resume_preserves_inspect_feedback_as_tool_context_without_reconciliation`:
  resumed tool content is a string where the assertion expects text/image parts.
- `test_creation_config_runtime.py::test_presentation_change_requires_new_render_and_real_visual_consumption`:
  the second inspection returns `inspection_limit_reached` after a language change.

Two historical tests cannot collect without unpublished canary modules
(`test_real_webui_canary.py`, `test_refero_font_audit.py`); some legacy WebUI tests require archived
runtimes. Packaging CI deliberately selects its documented scope. Docker builds, Windows/macOS
startup and the complete 238-case experiment have not been validated by these packaging checks.
Local deployment smoke tests are not paper-score reproduction.

## License and citation

Project-wide licensing awaits confirmation by the rights holders. Third-party terms remain
applicable; see [third-party notices](webui/THIRD_PARTY_NOTICES.md) and [font records](fonts/FONTS.md).
Final authors, paper URL/DOI, citation metadata and the immutable paper tag remain to be confirmed.
