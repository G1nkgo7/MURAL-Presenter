# SenseNova Present WebUI bundle

SenseNova Present is the interactive authoring application associated with MURAL-Presenter. The
bundle exposes project creation, material upload, model and service configuration, generation
status, rendered-deck inspection, revision, trajectory monitoring, and delivery views.

For a clean-machine walkthrough covering macOS, Linux, and Windows, see the
[English local deployment guide](../../../docs/local-deployment.md) or
[中文本地部署说明](../../../docs/local-deployment_zh-CN.md).

## Directory map

| Path | Responsibility |
| --- | --- |
| [`studio/`](studio/) | FastAPI application, templates, static assets, SQLite state, and tests |
| [`distillation/`](distillation/) | Generation-process adapter and model-provider shims |
| [`dynamic/`](dynamic/) | Optional dynamic-deck adapter and rendering utilities |
| [`scripts/`](scripts/) | Cross-platform launcher and runtime bootstrap helpers |
| [`docs/`](docs/) | Deployment guidance |
| [`../../../skills/mural-presenter/`](../../../skills/mural-presenter/) | Bundled MURAL-Presenter Skill |
| [`../../../harnesses/mural-presenter/`](../../../harnesses/mural-presenter/) | Paired generation Harness |

## Start the public UI preview

Requirements: Python 3.12 and [`uv`](https://docs.astral.sh/uv/).

```bash
cp .env.example .env
./start.sh --ui-only --port 8001
```

Windows PowerShell:

```powershell
Copy-Item .env.example .env
./start.ps1 -UiOnly -Port 8001
```

The launcher creates only the Studio environment in UI-only mode. `--check` prints the effective
non-secret configuration without starting a server.

## Enable generation

The repository bundles the current MURAL-Presenter Skill and its paired Harness. It does not bundle
model weights or service credentials. Configure a compatible endpoint in `.env`, keep
`SENSENOVA_UI_ONLY=0`, and start without `--ui-only`.

At minimum, MURAL generation needs:

- one configured OpenAI- or Anthropic-compatible model endpoint and its credential, if required;
- writable `STUDIO_DATA_DIR`; and
- a Playwright Chromium installation available to the generation environment.

The launcher automatically creates the Harness environment and installs Playwright Chromium on the
first run. `PPTAGENT_MURAL_PRESENTER_SKILL_ROOT` and
`PPTAGENT_MURAL_PRESENTER_HARNESS_ROOT` are optional overrides for testing another frozen snapshot.

The complete variable list and safe examples are in [`.env.example`](.env.example). Secrets belong
in `.env` or a deployment secret store and must never be committed.

## Data and security

SQLite databases, uploads, generated decks, logs, and runtime caches live below
`STUDIO_DATA_DIR` and are ignored by Git. A single Studio process should own a SQLite database.
Place authentication, TLS, upload limits, and access control at the deployment boundary.

## Development checks

```bash
uv sync --project studio --frozen
uv run --project studio --with pytest pytest -q -p no:cacheprovider studio/tests
```

The public-release check at the repository root verifies the bundled Skill/Harness snapshot and that
the bundle contains no credentials or generated data.

See [migration provenance](MIGRATION.md) and [deployment guidance](docs/DEPLOYMENT.md).
