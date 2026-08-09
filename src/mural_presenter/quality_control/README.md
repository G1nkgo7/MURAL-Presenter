# Quality control

Reserved for deterministic checks and Judge-backed review used by both data construction and product
runs.

QC layers should remain explicit: schema validity, required artifacts, factual/content checks,
single-slide visual checks, slide-group relations, whole-deck requirements, trajectory integrity, and
export fidelity. Reports should include check version, evidence references, severity, disposition,
and machine-readable failure categories.

Acceptance is append-only: a failed record is retained with its reason, and a repaired deck becomes a
new attempt linked to the finding that triggered it.
