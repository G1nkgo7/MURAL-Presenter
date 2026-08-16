<!-- Reference owner: Shared contract. Allowed consumers: Research, Presenter. -->

# Provenance and constraint protocol

Use this protocol only to preserve facts and stable constraints across the Research → Presenter boundary. It does not contain visual, layout, implementation, or audience-taste guidance.

## Evidence identifiers

- `M-xx`: claim copied from user material. Preserve the original value, unit, date, scope, and attachment name.
- `K-xx`: externally supported claim. Preserve source, publication date, URL, scope, and confidence.
- `V-xx`: factual visual-world reference: real subject, scene, material, medium, or era context. It is evidence, not an art direction.
- `C-xx`: Presenter-owned content, narrative, speech, or asset-binding constraint.
- `D-xx`: Designer-owned visual-system constraint. Presenter may consume it at Gate but must not create or rewrite it.

Do not silently convert one identifier family into another. When a Presenter plan uses a factual claim, cite the supporting `M-xx` or `K-xx` in the plan and preserve its wording and scope.

## Source and uncertainty rules

- Prefer the user attachment for what the user or supplied document says; use external evidence to verify or contextualize it.
- Record conflicting values side by side. Do not average, merge, or choose a winner without an explicit basis.
- Mark unsupported required content as `示意`, `待核`, or `unknown`; never turn it into a precise fact.
- Keep dates and measurement scope with every time-sensitive number.
- Use short quotations only when exact wording matters; otherwise paraphrase and retain the source.

## Public copy boundary

Internal paths and IDs are production metadata. Never show `research_NN`, `materials_NN`, `knowledge-brief.md`, `plan/`, `slide_NN`, `K-xx`, or `M-xx` on a rendered slide. On-screen attribution must be a real external source, organization and date, attachment title when appropriate, or an explicit `示意/待核` label.

## Handoff contract

- Research produces evidence and a grounded domain model; it does not decide narrative, style, or layout.
- Presenter decides thesis, sequence, exact on-screen copy, speech, and semantic visual need; it does not alter evidence or invent a visual style.
- If the knowledge package cannot support a required claim or page, return the gap rather than hiding it inside prose.

