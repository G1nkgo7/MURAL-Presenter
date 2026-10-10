# MURAL Presenter

Paper implementation for **Multi-Agent Unified Revision-Aware Authoring for Long-Horizon Presentations**.

This repository publishes the 0919 authoring Skill, its paired Harness and the WebUI.
The 254 core source files are byte-identical to
`mural-presentbench-qwen35-9b-20260919T163556Z`. Deployment paths are resolved at launch.

[简体中文](README_zh-CN.md) · [WebUI guide](webui/README.md) · [Contributing](CONTRIBUTING.md)

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

## Repository layout

```text
MURAL-Presenter/
├── README.md / README_zh-CN.md
├── skills/mural-presenter-v1/       # Frozen authoring Skill and tools
├── harnesses/mural-presenter-v1/    # Frozen inference and delivery runtime
├── webui/                          # UI, server, dependency locks and launchers
├── configs/                        # Runtime template, MinerU and release provenance
├── fonts/                          # Bundled rendering fonts
├── scripts/                        # Portable CLI and configuration adapters
├── tools/                          # Source verification and integration freezing
├── tests/                          # Public integration regression tests
└── .github/                        # CI and contribution templates
```

The core directory names and internal assets, references, docs and tests are part of its frozen
identity. Keep them intact. Root-level `benchmark/`, `docs/`, `examples/`, branding `assets/`,
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
