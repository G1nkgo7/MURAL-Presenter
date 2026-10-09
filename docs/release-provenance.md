# Release provenance

The reference runtime is `mural-presentbench-qwen35-9b-20260919T163556Z`, frozen on 2026-09-19.
It records working-tree bytes, not a claim that the source was a clean Git commit.

Original artifacts are stored in `configs/releases/upstream-0919/`:

- `publication.json`: original 319-file inventory.
- `pin.json`: publication/inventory hashes and runtime identity.
- `release.json`: original Skill, Harness, pair and integration digests.

All 55 Skill files and all 199 Harness files are imported unchanged. Their original combined
SHA-256 is `378c3ac7fa26d06b91f5408c7a25e7350b9b37e712611a322e387ae13a2d64e3`.
The core retains upstream documentation and historical deployment defaults as part of that identity;
use the root launch instructions instead of historical machine-specific examples.

The matching upstream WebUI supplied templates, static assets and supporting modules. Before import,
all 30 WebUI files covered by the experimental inventory matched the frozen version byte-for-byte.
Presentation-only files were not part of that original inventory. Two upstream test files were
unreadable and were not copied; no source permissions were changed.

Portable integration changes are limited to repository-local runtime configuration, WebUI dispatch
of that configuration, launch adapters, packaging, documentation and validation. The active manifest
has a distinct release identity and freshly calculated integration digest. Generation prompts,
model-call logic, authoring state and rendering/export implementations remain unchanged.

The experimental inventory also records internal data-pipeline launch configurations. Those are
not published as runnable deployment instructions here; the original inventory retains their hashes.
No credentials, local environments, generated datasets or copied Git object databases are published.

Use `python tools/check_paper_release.py` to verify the source pair and current integration.
Changes to frozen source require a new source baseline and explicit reproduction validation;
do not silently recalculate hashes to conceal source changes.
