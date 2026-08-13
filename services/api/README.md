# Studio API service

This is the target extraction boundary between SenseNova Present Studio and reusable MURAL runtime
modules. The product currently keeps these routes inside the canonical WebUI FastAPI application at
[`../../webui/studio/app/`](../../webui/studio/app/).

Extraction will be incremental: first stabilize project, run, artifact, revision, and event schemas;
then move server responsibilities here without changing product behavior. This service will own
authorization, validation, lifecycle events, cancellation/resume, revision routing, and export
delivery. Planning, inference, rendering, and QC should ultimately delegate to
[`../../src/mural_presenter/`](../../src/mural_presenter/).

The browser must never receive provider credentials or deployment-local runtime paths.
