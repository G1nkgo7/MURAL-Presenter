# Data pipeline

Reserved for the offline path from query specifications to versioned training/evaluation records.

Expected stages include ingestion, material normalization, case compilation, rollout scheduling,
artifact indexing, deduplication, QC joins, acceptance filtering, split assignment, and manifest
export. Every transformation should be restartable and write a new manifest rather than silently
editing upstream records.

The pipeline may invoke the provider-neutral `inference` runner and consume `quality_control`
reports, but it must preserve the configuration, code revision, schema version, seed, and lineage of
every retained or rejected record.
