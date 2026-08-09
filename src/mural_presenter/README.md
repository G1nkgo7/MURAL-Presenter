# MURAL-Presenter core

This is the reserved home of reusable Python implementation. The current public repository contains
module contracts only; runnable code will be added after the implementation and license boundary are
frozen.

Modules are organized by responsibility rather than by a single end-to-end script:

- `query_synthesis/` creates reproducible task specifications.
- `data_pipeline/` turns specifications, materials, rollouts, and QC decisions into traceable data.
- `orchestration/` owns lifecycle state, slide-group responsibility, and revision routing.
- `inference/` owns provider-neutral model execution and run recovery.
- `rendering/` owns browser rendering, visual inspection surfaces, and delivery adapters.
- `quality_control/` owns checks, findings, acceptance, and failure taxonomy.
- `schemas/` owns the serialized contracts shared by every stage.

See [`../../docs/repository-layout.md`](../../docs/repository-layout.md) for dependency rules and the
canonical run-directory layout.
