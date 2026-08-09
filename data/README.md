# Data layout

This directory reserves public data conventions; it does not contain the unreleased training data.

Local generated directories are ignored:

- `raw/`: immutable source drops or source manifests;
- `interim/`: normalized cases and unfinished rollouts;
- `processed/`: QC-joined, split-assigned records; and
- `cache/`: reproducible local acceleration artifacts.

A future `public/` subtree may contain only versioned, redistributable query pools, schemas, manifests,
and small examples. Every public record must state provenance, license/redistribution status, schema
version, and checksums for referenced artifacts.
