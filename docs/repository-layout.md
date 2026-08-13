# Repository layout

This document defines the public implementation layout for MURAL-Presenter. Most core directories
remain interface contracts. `webui/` is the single runnable SenseNova Present WebUI. The default
MURAL Presenter Skill and paired Harness have exactly one canonical editable source at
`skills/mural-presenter/` and `harnesses/mural-presenter/`; the WebUI references those paths instead
of carrying another copy. Model endpoints and optional search/image services remain deployment-owned.

## Two execution paths

### Offline data construction

```text
versioned pools + synthesis config
                ↓
        query_synthesis
                ↓ query specifications
         data_pipeline
                ↓ normalized case bundles
    inference / rollout runner
                ↓ run artifacts and trajectories
        quality_control
                ↓ accepted records + rejection reasons
       released dataset subset
```

### Interactive authoring

```text
webui → services/api → orchestration → inference
                                      ↘ rendering ↔ quality_control
                                               ↓
                                      HTML / PPTX / PDF / images
```

The two paths share schemas and runtime adapters, but not lifecycle ownership. Offline data code
creates and filters reproducible records; the product path serves user projects and resumable runs.

## Directory responsibilities

| Path | Owns | Expected output |
| --- | --- | --- |
| `src/mural_presenter/query_synthesis/` | Domain/style/audience/speaker pools, constrained sampling, prompt assembly | Versioned query specifications with seeds and provenance |
| `src/mural_presenter/data_pipeline/` | Ingestion, normalization, rollout manifests, filtering, dataset assembly | Reproducible case bundles and accepted-data manifests |
| `src/mural_presenter/orchestration/` | Lifecycle state, deck planning, group compilation, delegation, revision routing | Deck/group plans and auditable state transitions |
| `src/mural_presenter/inference/` | Model adapters, run configuration, single/batch execution, retries and resume | Provider-neutral responses, traces, usage and terminal status |
| `src/mural_presenter/rendering/` | HTML rendering, screenshots, contact sheets and delivery adapters | Rendered pages, inspection surfaces and exported files |
| `src/mural_presenter/quality_control/` | Schema, content, visual, deck-level and trajectory checks | Structured QC reports and acceptance decisions |
| `src/mural_presenter/schemas/` | Versioned records shared across stages | Stable serialized contracts without provider-specific fields |
| `configs/` | Checked-in non-secret configuration | Named, reviewable experiment and pipeline settings |
| `scripts/` | Human-facing command entrypoints | Thin wrappers around importable modules |
| `webui/` | Interactive authoring Web UI | Project, run, review, revision and export views |
| `skills/mural-presenter/` | Frozen paper/release lifecycle, role contracts, design references and deterministic deck tools | Versioned Skill snapshot copied into each run |
| `harnesses/mural-presenter/` | Frozen paper/release model calls, delegation, tool execution, traces and recovery | Paired multi-agent generation process |
| `services/api/` | Authenticated server boundary for Studio | Project/run APIs plus streamed lifecycle events |
| `tests/` | Unit, integration and end-to-end verification | Deterministic checks and small redistributable fixtures |

## Boundary rules

1. `schemas` must not depend on a model provider, Web framework, or concrete storage backend.
2. `query_synthesis` creates task specifications; it does not silently run models or accept its own
   outputs.
3. `data_pipeline` records provenance and composes stages. Model execution belongs to `inference`,
   and accept/reject decisions belong to `quality_control`.
4. `quality_control` emits findings and decisions. It must not mutate a deck without recording a
   new revision attempt.
5. Browser code never stores provider credentials or calls model providers directly. During the
   compatibility phase, Studio's FastAPI server owns those adapters; the target is to extract them
   into `services/api`, which delegates reusable work to `src/mural_presenter`.
6. `scripts` contain argument parsing and wiring only; tested logic stays importable from `src/`.
7. Every persisted record carries a schema version, stable identifier, configuration reference,
   random seed where applicable, and provenance sufficient to reproduce or reject the record.

## Canonical run directory

Generated directories are ignored by Git, but every run should follow one inspectable layout:

```text
artifacts/runs/<run_id>/
├── run.json               # identity, versions, config, status, timestamps
├── input/                 # immutable query, attachments and normalized case bundle
├── state/                 # deck blueprint, group briefs and revision state
├── traces/                # event stream, model/tool calls and usage accounting
├── deck/                  # editable HTML/CSS/assets and export metadata
├── renders/               # page images, contact sheets, PDF/PPTX previews
└── qc/                    # stage reports, deck report and acceptance decision
```

Large or restricted inputs should be referenced through checksummed manifests rather than copied
into Git. A future public release may commit small redistributable examples under `data/public/` or
`examples/`, but never credentials, private endpoints, or proprietary source material.

## Web surfaces

- `site/` is the static bilingual project website, blog, and manuscript reader.
- `webui/` contains the canonical runnable SenseNova Present interactive product WebUI.
- `services/api/` is the target versioned browser-to-runtime boundary; equivalent routes currently
  live inside the WebUI's FastAPI server.

Keeping these surfaces separate lets the paper site remain dependency-light while the authoring UI
can later adopt project persistence, streamed runs, visual editing, review, and export without
coupling those concerns to publication pages.
