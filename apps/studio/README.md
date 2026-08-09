# SenseNova Present Studio

This directory contains the product WebUI shipped with the MURAL-Presenter research repository.
The product-facing brand remains **SenseNova Present**; **MURAL-Presenter** names the repository,
method, and paper.

The runnable compatibility bundle lives in [`sensenova_present/`](sensenova_present/). It includes
the FastAPI/Jinja Studio, its SQLite-backed task service, revision and trajectory views, the
distillation adapter, and the dynamic-deck adapter. The repository bundles the default
MURAL-Presenter Skill and Harness; model endpoints, credentials, and user data remain external.

This product is separate from [`../../site/`](../../site/), the public MURAL-Presenter project
website, blog, and paper preview.

## Quick preview

```bash
cd apps/studio/sensenova_present
cp .env.example .env
./start.sh --ui-only --port 8001
```

Open `http://127.0.0.1:8001`. UI-only mode exercises only the product surface. To enable MURAL
generation, start without `--ui-only` and configure the model endpoint described in the
[bundle README](sensenova_present/README.md); no separate Skill mount is required.

## Integration boundary

The current import deliberately preserves the working application's internal module layout. It is
therefore a **compatibility bundle**, not yet the final separation between browser UI,
[`../../services/api/`](../../services/api/), and reusable MURAL runtime modules. This keeps the
verified SenseNova product behavior intact while the public API boundary is extracted incrementally.

See [migration provenance](sensenova_present/MIGRATION.md) for copied and excluded components.
