# SenseNova Present deployment guide

## Modes

UI preview mode runs the product surface without a local generation runtime:

```bash
./start.sh --ui-only --host 127.0.0.1 --port 8001
```

Full mode requires mounted Skills and Harnesses, a model endpoint, and a Chromium-capable generation
environment. Copy `.env.example` to `.env`, configure those paths, set `SENSENOVA_UI_ONLY=0`, then
run `./start.sh`.

Validate non-secret configuration before starting:

```bash
./start.sh --check --ui-only --no-install --no-browser-install
```

## Service topology

One WebUI process should own its SQLite database and dispatch queue. Multiple public entry points
should reverse proxy to that process instead of starting independent writers against one database.
Put TLS, authentication policy, request-size limits, and access logging at the proxy boundary.

## Containers

```bash
cp .env.example .env
docker compose up --build -d
docker compose logs -f sensenova-present
```

Compose defaults to UI-only mode and persists `/data` in a named volume. Full generation images must
also mount approved external runtimes and set the corresponding environment variables.

## Production checklist

1. `--check` succeeds without printing a secret.
2. `/healthz` returns a successful response.
3. Chinese and English UI modes render correctly.
4. `STUDIO_DATA_DIR` is persistent, access-controlled, and backed up.
5. Required model, image, search, Skill, and Harness dependencies pass explicit smoke tests.
6. A small deck completes render, review, revision, and export.
7. `.env`, databases, uploads, logs, and generated artifacts are absent from the release bundle.
