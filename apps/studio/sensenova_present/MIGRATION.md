# Migration provenance

This compatibility bundle was imported from the SenseNova Present WebUI working tree at source
revision `45c5ec6` on branch `feat/trajectory-monitor`.

The source snapshot contained ongoing uncommitted work. The import therefore records provenance
without claiming a clean upstream release. It copied the application code required to preserve the
current product architecture:

- `studio/` application code, templates, static assets, and tests;
- `distillation/` adapters and its reproducible Python lockfile;
- `dynamic/` adapters and bundled, code-sized Skill material;
- cross-platform launch, bootstrap, migration, and deployment files.

The import intentionally excluded:

- `vendor/` runtimes, font collections, examples, and model-facing Harness snapshots;
- virtual environments, browser caches, generated runs, SQLite databases, uploads, logs, and JSONL
  production data;
- `.env` files and credentials;
- private gateway and machine-specific deployment scripts;
- batch-specific background launchers and fixed-account browser smoke scripts; and
- working-tree-only documentation that referenced unavailable private artifacts.

A later repository-owned release adds `skills/mural-presenter/` and
`harnesses/mural-presenter/` as a separate frozen pair with its own source provenance. It does not
reintroduce the excluded vendor trees, historical runs, private services, or credentials.

Before import, the focused source suite reported 161 passing tests, 25 failing tests, and 146 passing
subtests. Several failures depended on unavailable external Skills or stale assertions in the active
working tree. The repository records that baseline so migration validation is not confused with an
upstream clean-test claim.

The product brand remains **SenseNova Present**. Repository and paper branding remains
**MURAL-Presenter**.
