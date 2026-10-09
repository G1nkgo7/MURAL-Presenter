# Validation — 2026-10-09

## Portable integration v2: shared-port Studio deployment

The original dashboard remains the gateway's root service. Studio uses `/studio/`, while
the paper website keeps `/mural-presenter/`. The gateway strips only Studio's prefix and
the launcher passes the same prefix to Uvicorn; this is required for nested static-file mounts.

- Portable HTTP and gateway tests: 9 passed, including root deployment, `/studio`, a nested
  prefix, real Uvicorn static routing, redirects, cookie scope, request bodies and event streams.
- Existing WebUI service/profile/retirement regression tests: 139 passed, 1 browser test skipped.
- Real-browser checks exercised the deployed composer and trajectory navigation, with all
  same-origin requests kept under `/studio/` and no HTTP or JavaScript errors during those checks.
- The external root page matched the original dashboard byte-for-byte; the paper website remained
  available. Both the frozen 254-file core and the refreshed v2 integration digest verified.

No model generation or edits to historical presentation data were used for this routing change.

## Initial portable integration validation

This validates the portable 0919 source package, not newly reproduced paper scores.
Tests used Python 3.12.13 and separate environments installed from both WebUI `uv.lock` files.
Test-only `pytest` and `httpx` were installed separately. No model requests were made.

## Passing checks

- All 254 frozen Skill/Harness files match the original experimental inventory; the combined
  tree hash remains `378c3ac7fa26d06b91f5408c7a25e7350b9b37e712611a322e387ae13a2d64e3`.
- Active integration manifest verification passes without a dirty-release override. It covers
  backend code, frontend templates/assets, dependency locks and portable launch configuration.
- Portable configuration and HTTP tests: 5 passed. The tests exercise configuration preservation,
  runtime selection, FastAPI startup, the main page, health endpoint and four CSS/JS assets.
- Frozen release/configuration contracts: 95 passed (`test_release_guard.py`, `test_config.py`).
- WebUI service configuration, runtime retirement and profile tests: 139 passed, 1 skipped
  because the suite's default deployed Chromium was unavailable.
- The launcher's `--check --no-install --no-browser-install` passes with the published `.env.example`.
- CLI inference `--help`, JavaScript syntax checks and deployment shell syntax checks pass.
- Real Chromium CSS/DOM preflight passes with a compatible existing browser and shared-library bundle.
- A Git archive of source commit `adeac82c7332c3d6212bf252b17d44bbef7ef1fc`, with the final manifest
  pointing to that source commit, passes core/integration verification, the example launcher check
  and all 5 portable/HTTP tests. It contains no local historical trees or site. The export reused
  the separate locked-dependency test environments through local environment links.

## Known limits and failures

The frozen suite could not initially collect two tests because its publication inventory excludes
the required canary modules (`test_real_webui_canary.py`, `test_refero_font_audit.py`). A broader run
excluding those files stopped at the browser preflight. After correcting the local browser/library
environment, both observed failing assertions were rerun and still failed:

1. `test_agent_runtime.py::test_resume_preserves_inspect_feedback_as_tool_context_without_reconciliation`:
   the resumed tool content has a string where the test expects text/image content parts.
2. `test_creation_config_runtime.py::test_presentation_change_requires_new_render_and_real_visual_consumption`:
   the second inspection returns `inspection_limit_reached` after changing the presentation language.

The frozen core remains byte-identical; these results were recorded instead of patching the
experimental baseline. The full suite has not passed. Packaging CI deliberately tests its stated
integration scope and does not hide these results behind expected-failure markers.

WebUI archive-retirement tests were adapted to this package's layout: historical archives and
internal launchers are local-only, while the active runtime and retired-task rejection remain tested.
Other legacy WebUI tests are retained as upstream references and may require unpublished historical
trees. They are not all part of the public packaging CI.

FastAPI's thread-based HTTP test required execution outside the restricted filesystem sandbox;
the same test passed with isolated test data. Starlette reported a deprecation warning for its
`httpx` test client. No application dependency locks were upgraded to suppress it.

Docker builds, Windows/macOS startup, live model generation and the 238-case experimental run were
not tested. The launcher reports that its default managed browser directory still needs installation;
the successful browser preflight used an explicitly configured existing installation.

## Repeatable checks

The exact automated commands are in `.github/workflows/public-surface.yml`. Generate the local
configuration first through the documented launcher, then run `python tools/check_paper_release.py`.
Use fresh test data and a new `--basetemp` directory. Do not run regression tests against real Studio
databases or historical experimental workspaces.
