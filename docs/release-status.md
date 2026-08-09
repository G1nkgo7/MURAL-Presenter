# Release status and evidence boundary

Last reviewed: 2026-08-09

MURAL is a research project under active development. This repository intentionally separates
what has been observed in the working system from what has been experimentally demonstrated or
cleared for public release.

## Available now

- The locked public name and research framing.
- The MURAL mascot, compact mark, lockups, and deterministic export builder.
- Publication diagrams for the execution topology and full lifecycle.
- Bilingual method, benchmark, and launch documentation.
- A deployable project site.
- A documented repository scaffold for query synthesis, data preparation, inference, QC, and the
  target Studio service boundary.
- A sanitized SenseNova Present WebUI compatibility bundle with the current MURAL-Presenter Skill
  and paired Harness. It can run as a UI preview or connect to a user-configured model service;
  credentials, model weights, optional service backends, and production data remain external.
- Bilingual working-paper PDFs whose main bodies fit the nine-page target; all effectiveness
  result slots remain explicitly pending.

## Implemented and represented in the bundled MURAL workflow

- A grouped authoring workflow with complete, non-overlapping slide groups.
- A centralized image stage before group production.
- Group-level rendering, contact-sheet inspection, and revision.
- Whole-deck review after deterministic finalization.
- Revision routing across local, group, evidence, and global scopes.

These statements describe inspected mechanisms, not measured quality improvements.

## Planned or awaiting a freeze

- Stable public APIs around the bundled, versioned MURAL-Presenter Skill/Harness pair.
- Extraction of the compatibility bundle's server routes into the versioned `services/api` boundary.
- Rollouts and accepted agentic trajectories derived from the current 1,000 query specifications
  (850 Chinese and 150 English); completion, acceptance, and training counts are not yet frozen.
- A canonical 50-case THREAD-Bench release and calibrated judges.
- Final model identities, training configuration, and accepted-data counts.
- Main effectiveness results, ablations, statistical analysis, and cost diagnostics.

## Claims we do not make

- Multi-agent systems, global planning, HTML slide generation, contact sheets, or final review are
  not individually claimed as new.
- Single-agent systems are not claimed to be inherently worse at editing.
- Every per-slide system is not claimed to drift.
- HTML, PPTX, PDF, and images are not assumed to preserve identical behavior or editability.
- A generation-time refinement loop is not assumed to improve later editing until evaluated.

## Publication gates

Measured claims require a frozen code and Skill version, reproducible configuration, benchmark
schema, judge protocol, raw run ledger, and traceable aggregation. Public code or data additionally
requires a license and a review for credentials, private endpoints, proprietary material, and
third-party redistribution restrictions.
