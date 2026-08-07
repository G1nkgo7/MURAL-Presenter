# THREAD-Bench overview

**THREAD-Bench** stands for **Tracking Holistic Requirements and End-to-End Alignment in Decks**.
It is a planned 50-case diagnostic benchmark for long-horizon presentation authoring.

## Evaluation layers

THREAD-Bench separates evidence that is often collapsed into one final score:

1. **Process evidence** from Research, Planning, Image, and Group Authoring handoffs.
2. **Knowledge checks** for factual, numerical, and definition-level correctness.
3. **Deck-level presentation checks** for narrative, design language, terminology, and task requirements.
4. **Page-level presentation checks** for role fulfillment, layout, legibility, and visual communication.
5. **Long-range probes** that connect an anchor decision to one or more distant target slides or revisions.

## Dependency records

Each long-range probe should identify:

- the anchor stage or slide where a decision is established;
- all target stages or slides that must consume, preserve, or answer it;
- an observable relation predicate;
- the dependency span and relation type;
- the evidence source used by the judge.

A relation is closed only when every required target satisfies its predicate. This prevents an average
per-slide score from hiding a broken cross-slide requirement.

## Case construction

Cases vary domain, audience, speaker perspective, purpose, language, target length, attachment setting,
style direction, and dependency structure. A case may include bookends, distant definition reuse,
comparison alignment, a visual encoding that must remain stable, or a later edit that should preserve
untouched slides.

## Release gates

The benchmark is not yet canonical. Public release requires:

- exactly 50 frozen case identifiers and redistributable inputs;
- a versioned schema and validation script;
- frozen judge prompts and score aggregation;
- judge calibration and disagreement analysis;
- contamination and overlap checks;
- example traces and a documented missing-evidence policy.

Until these gates are complete, THREAD-Bench should be described in future or present-progress tense,
and no final comparative score should be reported.

