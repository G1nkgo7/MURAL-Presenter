# Inference

Reserved for provider-neutral single-run and batch execution.

This module will own model/runtime adapters, request normalization, tool/event capture, retry and
timeout policy, concurrency limits, checkpointing, resume, usage accounting, and terminal status.
It should accept a run configuration plus a versioned input bundle and produce append-only events and
artifacts under one `run_id`.

Provider credentials remain outside checked-in configuration. Offline rollout generation and online
authoring should share adapters where possible while retaining distinct run profiles and manifests.
