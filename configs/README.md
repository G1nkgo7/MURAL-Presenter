# Configuration

Checked-in configuration belongs here only when it is non-secret, reviewable, and tied to an explicit
schema version. Environment-specific hosts, access tokens, credentials, and private storage paths must
remain outside Git.

- `query_synthesis/`: pool versions, sampling quotas, languages, deck ranges, seeds, and constraints.
- `data_pipeline/`: stage graph, input/output manifests, split policy, and resumability settings.
- `inference/`: logical model roles, adapter names, decoding settings, limits, retry, and concurrency.
- `quality_control/`: enabled checks, thresholds, Judge protocol references, and acceptance policy.

Named configurations should be immutable after a recorded run. Changes create a new name or version
and are referenced from `run.json`.
