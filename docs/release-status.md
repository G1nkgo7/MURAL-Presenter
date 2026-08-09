# Release status and evidence boundary

Last reviewed: 2026-08-08

MURAL is a research project under active development. This repository intentionally separates
what has been observed in the working system from what has been experimentally demonstrated or
cleared for public release.

## Available now

- The locked public name and research framing.
- The MURAL mascot, compact mark, lockups, and deterministic export builder.
- Publication diagrams for the execution topology and full lifecycle.
- Bilingual method, benchmark, and launch documentation.
- A deployable project site.
- A documented repository scaffold for query synthesis, data preparation, inference, QC, the
  product Web UI, and its server boundary. The scaffold contains interface documentation only.
- Bilingual working-paper PDFs whose main bodies fit the nine-page target; all effectiveness
  result slots remain explicitly pending.

## Implemented in the working system, not yet released here

- A grouped authoring workflow with complete, non-overlapping slide groups.
- A centralized image stage before group production.
- Group-level rendering, contact-sheet inspection, and revision.
- Whole-deck review after deterministic finalization.
- Revision routing across local, group, evidence, and global scopes.

These statements describe inspected mechanisms, not measured quality improvements.

## Planned or awaiting a freeze

- A clean public implementation commit and versioned MURAL Authoring Skill.
- The functional MURAL Studio Web UI and API service; their current directories are placeholders.
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
