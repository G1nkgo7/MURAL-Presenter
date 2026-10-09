# Repository layout

| Path | Responsibility |
| --- | --- |
| `skills/mural-presenter-v1/` | Exact 0919 Skill source, including role prompts and deterministic tools |
| `harnesses/mural-presenter-v1/` | Exact 0919 Harness source and its regression tests |
| `webui/` | Matching Studio frontend/backend, inference bridge and locked dependencies |
| `configs/runtime.json` | Portable deployment template; no credentials |
| `configs/runtime.local.json` | Ignored deployment-specific resolved configuration |
| `configs/releases/` | Active integration manifest and original experimental inventory |
| `configs/experiments/` | Non-secret experimental settings |
| `scripts/` | Small portable launch adapters; no alternative generation implementation |
| `tools/` | Release verification and provenance utilities |
| `tests/` | Integration tests for the portable release |
| `benchmark/` | Evaluation code; original third-party notices retained |
| `fonts/` | Existing distributable font bundle and license records |
| `assets/` | Project assets |
| `docs/` | Installation, reproduction, provenance and maintenance |

`site/` is an independent local website repository and is not tracked by this repository.
`.snapshots/` contains local recovery archives, never release inputs.
`artifacts/`, `data/` and `webui/studio/data/` hold local outputs, datasets and runtime state.
Historical `archive/`, `paper/`, `train/`, `research/`, `variants/`, `mainline/`, `references/`,
`src/` and `services/` remain local where present; they are not alternate public entrypoints.
Old Skill/Harness/configuration directories were moved intact into the recorded local backup.

The `v1` names are retained deliberately: they are part of frozen source and state contracts.
The outer source package's historical `v1.0` directory name is not the name of this runtime.
