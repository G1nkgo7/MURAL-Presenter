# Studio API service

Reserved for the authenticated server boundary between MURAL Studio and reusable runtime modules.

The service should own project/run authorization, input validation, artifact access, cancellation,
resume, streamed lifecycle events, revision requests, and export delivery. It delegates planning,
inference, rendering, and QC to `src/mural_presenter/` rather than duplicating those implementations.

Initial resource families are expected to cover projects, materials, runs, decks, revisions, review
findings, and exports. The concrete HTTP/event protocol will be versioned before implementation. Model
credentials and private infrastructure details remain server-side and outside checked-in config.
