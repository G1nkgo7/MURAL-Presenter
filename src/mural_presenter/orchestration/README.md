# Orchestration

Reserved for MURAL's lifecycle controller: material/research handoff, shared deck blueprint,
slide-group compilation, group delegation, whole-deck closure, and impact-scoped revision routing.

The Orchestrator should operate on versioned state records and emit auditable transitions. It chooses
responsibility boundaries and resume points; it should not embed provider SDK calls, browser-specific
rendering code, or QC heuristics directly.

Group plans must cover every slide exactly once while allowing contiguous, non-contiguous, and
singleton groups. Revision decisions should record why page, group, evidence, or deck scope was
selected.
