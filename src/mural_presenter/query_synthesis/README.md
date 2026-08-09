# Query synthesis

Reserved for deterministic construction of presentation requests from versioned domain, style,
audience, language, speaker-intent, and requirement pools.

Inputs should include named pool versions, sampling constraints, a random seed, and optional source
material references. Outputs should be immutable query specifications rather than free-form prompt
text alone. A specification should preserve its sampled dimensions, user-facing brief, requirements,
expected language, target deck range, provenance, and rejection history.

This module does not run presentation generation or decide whether a generated trajectory is
accepted; those responsibilities belong to `inference` and `quality_control`.
