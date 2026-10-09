# MURAL Presenter

Paper implementation for **Multi-Agent Unified Revision-Aware Authoring for Long-Horizon Presentations**.

This release uses the Skill and Harness frozen for
`mural-presentbench-qwen35-9b-20260919T163556Z`. The complete core source is byte-for-byte
identical to that experimental snapshot. The WebUI is included; deployment paths are resolved
at launch rather than pointing to the original author's machine.

[简体中文](README_zh-CN.md) · [Repository layout](docs/repository-layout.md) ·
[Reproduction](docs/reproduction.md) · [Release provenance](docs/release-provenance.md) ·
[WebUI](webui/README.md)

Current packaging checks and known upstream test failures are recorded in [validation](docs/validation.md).

## Quick start

Use Python 3.12 and `uv`. Model credentials and optional external services are not bundled.

```bash
uv sync --project webui/studio --frozen
uv sync --project webui/inference --frozen
cp webui/.env.example webui/.env
bash webui/start.sh
```

Open the address printed by the launcher (default `http://127.0.0.1:8001`). Configure your
model in the UI or deployment environment. On Windows use `webui/start.ps1`.
The first launch can install the Chromium renderer. It creates `configs/runtime.local.json`
with repository-local absolute paths; edit that ignored file to configure your MinerU services
and concurrency. Environment variable `MURAL_RUNTIME_CONFIG` can select an existing configuration.

The WebUI uses a single active pair:

- `skills/mural-presenter-v1/`: authoring instructions, roles, rendering/export tools.
- `harnesses/mural-presenter-v1/`: inference, synthesis, state, model calls and delivery.

## Command-line inference

```bash
webui/inference/.venv/bin/python scripts/run_inference.py \
  --query "Create a six-slide research presentation" \
  --workspace "$PWD/artifacts/example"
```

Use `--help` for supported arguments. The wrapper resolves deployment configuration before
calling the unchanged Harness. For a diagnostic report, use `--doctor --json artifacts/doctor.json`.
PDF/Office/image extraction requires configured MinerU services; those are external dependencies.
Text-only generation does not require an attachment parser. Model, search and image-generation
services require the operator's own configuration.

## Version and verification

```bash
python tools/check_paper_release.py
```

The active manifest is `configs/releases/mural-paper-v1.json`. Original experimental hashes and
inventory are retained under `configs/releases/upstream-0919/`; the portable integration has its
own manifest identity. No historical test count or experimental score is treated as new validation.
See [reproduction boundaries](docs/reproduction.md) for the exact run parameters and external assets.

The marketing website is maintained separately. It is not needed to install or run this release;
local `site/` files and their independent Git repository are intentionally ignored here.

## License and citation

Project-wide licensing is pending confirmation by the rights holders. Existing third-party
licenses and notices remain in effect; this repository does not grant a new license over them.
Paper metadata and the final release tag will be added when the authors freeze the publication.
