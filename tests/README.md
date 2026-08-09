# Tests

The future test suite will mirror the public module boundaries:

- unit tests for schemas, sampling, transformations, routing, and deterministic QC;
- integration tests for adapter contracts, resumable runs, rendering, and artifact manifests;
- end-to-end tests for offline data construction and Studio/API authoring flows; and
- small redistributable fixtures with explicit provenance and expected checksums.

Tests must not require private endpoints or unrestricted model calls by default. Model-backed tests
should be opt-in, budget-bounded, and record their configuration and outputs.
