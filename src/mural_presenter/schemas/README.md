# Shared schemas

Reserved for the versioned records exchanged across query synthesis, data preparation,
orchestration, inference, rendering, QC, Studio, and evaluation.

Likely contracts include query specification, material/evidence manifest, case bundle, run manifest,
deck blueprint, group brief, event, artifact index, QC finding, acceptance decision, revision request,
and export record. Concrete fields remain pending the public freeze.

Schemas should use stable identifiers and explicit versions, avoid provider-specific response shapes,
and support forward-compatible migration rather than in-place mutation.
