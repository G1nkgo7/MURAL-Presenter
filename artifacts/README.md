# Runtime artifacts

Generated runs, caches, and exports are ignored by Git. Runtime code should write each attempt under
`artifacts/runs/<run_id>/` using the canonical layout documented in
[`../docs/repository-layout.md`](../docs/repository-layout.md).

Do not treat this directory as a database. Persist identity, status, provenance, and checksums in
manifests so a run can later be copied to object storage or packaged as an independently auditable
release artifact.
