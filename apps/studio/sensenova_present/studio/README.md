# SenseNova Present Studio application

This is the FastAPI/Jinja product application. It combines the browser surface and the current
server-side compatibility API while the stable MURAL service boundary is extracted.

## Main capabilities

- brief entry, attachment ingestion, and bilingual project setup;
- model, image, and search-service configuration;
- static and optional dynamic presentation jobs;
- task status, logs, rendered slides, contact sheets, and artifact browsing;
- follow-up revision and in-place static editing;
- custom model profiles and read-only trajectory monitoring; and
- HTML/PPTX/PDF/image delivery surfaces exposed by the connected Harness.

## Run

Use the bundle launcher from the parent directory:

```bash
../start.sh --ui-only --port 8001
```

For direct development:

```bash
uv sync --frozen
uv run uvicorn app.main:app --host 127.0.0.1 --port 8001 --reload
```

The app reads product settings from environment variables. See the bundle
[configuration template](../.env.example). Persistent state defaults to `studio/data/`; production
deployments should set `STUDIO_DATA_DIR` explicitly.

## Runtime profiles

Generation profiles are registered in `app/engine.py`. Paths and model endpoints are deployment
configuration, not repository constants. Missing external Skills and Harnesses are represented as
unavailable profiles rather than silently substituted with a different workflow.

The public UI can start with `SENSENOVA_UI_ONLY=1`. Full generation requires the clean Harness and
the relevant external Skill/Harness roots.

## Tests

```bash
uv run --with pytest pytest -q -p no:cacheprovider tests
```

Tests that exercise optional adapters require those external fixtures to be configured. Do not
commit generated `data/`, provider keys, or deployment-local endpoint values.
