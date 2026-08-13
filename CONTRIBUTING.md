# Contributing to MURAL

Thank you for helping improve MURAL. The project is currently a research preview, so public
contributions should stay within the artifacts already present in this repository.

## Useful contributions now

- Correct an inaccurate or ambiguous statement in the public documentation.
- Report a reproducible presentation-authoring or export-fidelity failure.
- Propose a THREAD-Bench case with observable, checkable long-range dependencies.
- Improve accessibility, localization, or documentation structure.
- Improve a deterministic public asset builder without changing the scientific claim.
- Improve the documented repository interfaces without presenting placeholder modules as a
  runnable implementation.

## Before opening an issue

1. Search existing issues for the same behavior.
2. Remove confidential material, credentials, private URLs, and proprietary source documents.
3. For a failure report, include the input brief, expected behavior, observed behavior, affected
   slide numbers, output format, and the smallest reproducible artifact you are allowed to share.
4. For a benchmark case, distinguish atomic knowledge checks, deck-level requirements,
   page-level requirements, and explicit cross-slide dependencies.

## Pull requests

- Keep one conceptual change per pull request.
- Update both English and Chinese documentation when changing shared public claims.
- Rebuild derived logo or figure assets with the checked-in builder when applicable.
- Do not add measured results without a frozen evaluation configuration and traceable evidence.
- Do not add implementation copied from the private working system.
- Keep reusable workflow logic in `src/mural_presenter/`; `scripts/` should remain thin entrypoints,
  and `webui/` must call model/runtime functionality through `services/api/`.

Before submitting a public-surface change, run:

```bash
python tools/check_public_release.py
cd site && npm run lint && npm test
```

By contributing, you confirm that you have the right to share the submitted material. A project
license and contributor terms will be published with the first code-bearing release; until then,
maintainers may defer code contributions that would create unclear licensing obligations.
