<!-- Reference owner: Presenter. Allowed consumers: Presenter only. -->

# Presenter content audit

Audit the rendered deck against `memory/content-memory.md`; do not perform visual-style review.

## Hard coherence checks

- Required topic, audience, purpose, language, page count, and named sections are present.
- Every rendered title, value, label, claim, and source follows the corresponding page plan and factual invariant.
- No unsupported fact, internal production name, placeholder, or silent change of scope appears on screen or in speech.
- The page order advances the thesis without duplicate claims, unexplained jumps, premature conclusions, or an unclosed opening promise.
- Speech and rendered slide share the same claim, value, emphasis, and terminology.
- The final page is a real Closing rather than another Summary or generic thanks page.

## Soft coherence checks

- Each page can be explained within its allotted time.
- Transitions are natural and create the next question.
- Terminology, example depth, and tone fit the target audience.
- Spoken context complements rather than duplicates all on-screen text.
- The whole deck has a memorable through-line and a clear desired audience response.

## Routing

- Patch `plan/*`, `speech.md`, or `memory/content-memory.md` only when Presenter-owned meaning is wrong.
- Return affected slide numbers for targeted Slide re-authoring.
- Route palette, typography, imagery treatment, and cross-slide visual drift to Designer.
- Route clipping, broken elements, and local HTML construction to Slide or deterministic checks.
- Never request a full rebuild for a local coherence defect.

Every finding must cite a `C-xx` constraint, concrete slide evidence, owner, and smallest valid patch.
