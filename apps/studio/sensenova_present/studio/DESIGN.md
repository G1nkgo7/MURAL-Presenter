# SenseNova Present Studio architecture

## Current compatibility boundary

The imported application is a server-rendered FastAPI product with a small vanilla-JavaScript
client. FastAPI owns authentication/session state, SQLite persistence, uploads, job dispatch,
revision requests, artifact access, and runtime configuration. Generation runs in a separate Python
process through the `distillation` adapter, keeping browser and model execution state out of the
request process.

```text
Browser
  -> FastAPI routes and templates
      -> SQLite + workspace artifacts
      -> job dispatcher
          -> distillation adapter
              -> configured model service
              -> mounted Skill and Harness
              -> browser renderer
```

This topology reflects the existing working product. The public repository's intended architecture
extracts the server responsibilities behind `services/api` and moves reusable logic into
`src/mural_presenter` without forcing a risky one-shot rewrite.

## State ownership

- `studio/app/db.py` defines persisted product state.
- `STUDIO_DATA_DIR` owns databases, uploads, workspaces, logs, caches, and runtime artifacts.
- `studio/app/jobs.py` dispatches and observes generation processes.
- `studio/app/engine.py` maps product profiles to externally configured models, Skills, and Harnesses.
- `studio/app/trajectory_monitor.py` provides read-only views over explicitly configured evaluation
  roots.

## Configuration boundary

No deployment endpoint or filesystem identity is a source-code default. Model URLs, external Skill
roots, Harness roots, browser libraries, evaluation roots, and credentials are supplied through the
environment. The public default is UI-only mode.

## Security boundary

Secrets remain server-side. Uploaded and generated artifacts are user data and must not be served
outside authenticated routes in a production deployment. SQLite expects a single owning Studio
process; horizontal scaling requires an external database and job queue before multiple writers are
introduced.
