# MURAL Presenter × Nova Proxy Gate V1

This directory is the production entry for tomorrow's raw-trajectory rollout.
MURAL Presenter remains the inference and presentation-authoring harness. Nova
Proxy is only the model gateway and internal Vision-reader loop required by the
Gate contract.

## Frozen contract

- Gate repository: `agent_data/nova_vision_demo`
- Gate tag: `sensenova-harness-vision-v1.1-20260815`
- Gate commit: `4a067c75e4db7ea5ba81c264a1dc0ddac24f411e`
- MURAL harness: `harnesses/mural-presenter-v0.2`
- MURAL skills: `skills/mural-presenter-v0.2`
- Raw schema switch: `CLEAN_NOVA_RAW_V2=1`
- Gate compatibility switch: `CLEAN_NOVA_GATE_V1=1`

The Gate source is exported from the exact tag into each run root. The dirty
working tree of the local Gate clone is never executed.

## Runtime topology

```text
query
  -> MURAL Presenter Orchestrator / subagents
  -> shared Nova Proxy /v1/messages (visible-signed Anthropic model)
  -> MURAL vision_analyze tool
  -> shared Nova Proxy /v1/messages (auxiliary Agent, no Hermes tools)
  -> built-in vision_reader
  -> same Anthropic provider route and model used as Vision reader
  -> text-only tool_result back to MURAL
```

Main requests never contain image blocks. Images appear only in the auxiliary
`vision_analyze` request. Every physical request is sent with `stream=false`,
`nova_include_trace=true`, and a complete `nova_trace_context`. Raw response
bytes and `nova_internal_trace` are saved before SDK parsing.
Anthropic SDK retries are disabled; the explicit MURAL retry loop owns every
retry so each physical request receives its own attempt directory.
The per-request timeout remains the Gate's 1200 seconds. The MURAL child-role
wall boundary is 86400 seconds so it cannot expire after exactly two attempts.
The outer Hermes HTTP timeout is 86400 seconds. The Gate's 1200-second timeout
applies to each internal provider call, while one Nova Agent loop may contain
multiple calls; Hermes therefore waits for the complete Proxy response instead
of disconnecting mid-loop.

The launcher accepts the documented versioned Proxy URL (`.../v1`) for health
checks, then passes its origin to the Anthropic SDK. The SDK itself appends
`/v1/messages`; this prevents an accidental `/v1/v1/messages` request.

## Output layout

For `RUN_ROOT=/path/to/run`:

```text
RUN_ROOT/
├── gate-runtime/            # exact exported Gate tag, plus provenance
├── proxy_traces/            # Nova server-side audit logs
├── proxy_artifacts/         # Nova tool artifacts
├── logs/                    # proxy PID and log
├── mural/                   # normal MURAL deck workspaces and manifest
└── raw/
    ├── tasks/<main_trajectory_id>/
    │   ├── messages.json
    │   ├── tools.json
    │   ├── task_result.json
    │   ├── attempts.jsonl
    │   ├── attempts/main/...
    │   ├── attempts/aux/...
    │   ├── aux_calls/*.json
    │   └── assets/...
    └── quarantine/...       # fail-closed incomplete trajectories
```

The raw directory is separate from the replaceable deck workspace. MURAL
`--overwrite` cannot delete raw evidence.

## Commands

Prepare the exact Gate runtime:

```bash
RUN_ROOT=/path/to/run bin/prepare_gate_runtime.sh
```

Start the dedicated brushing proxy. The controlled YAML supplies the one
Anthropic route reused by Agent and Vision; no key is stored here. Startup is
not considered successful until the v1.1 wrapper's four live readiness probes
pass and are saved under the run root:

```bash
RUN_ROOT=/path/to/run \
API_KEYS_FILE=/secure/path/api_keys.yaml \
NOVA_PORT=8001 \
bin/start_proxy.sh start
```

The launcher accepts `POPPLER_ROOT` for a real run-local Poppler installation;
when unset it checks `RUN_ROOT/runtime/poppler` before falling back to `PATH`.
The Gate is not weakened when Poppler is absent: startup/health still fails.

Run the frozen health gate:

```bash
NOVA_PROXY_BASE_URL=http://127.0.0.1:8001/v1 bin/preflight.py
```

Run one real MURAL smoke:

```bash
RUN_ROOT=/path/to/run NOVA_PORT=8001 bin/smoke.sh
```

Run a JSONL batch:

```bash
RUN_ROOT=/path/to/run NOVA_PORT=8001 \
  bin/run_mural.sh --queries /path/to/queries.jsonl \
  --batch rollout-name --workers 1 --mode synthesis
```

Validate finalized task-level prechecks and run the frozen Gate integrity
scanner before scaling or handing off a batch:

```bash
RUN_ROOT=/path/to/run bin/validate_run.sh
```

Increase concurrency only after the one-task smoke and a 3–10 task pilot both
pass. A missing trace, image in a main request, unpaired tool call/result, or
unresolved auxiliary parent quarantines the task and blocks scale-up.

`NOVA_AGENT_MODEL` may select another Anthropic-native signed-thinking model
for a controlled comparison. It is applied to both Agent and Vision, and the
v1.1 visible-signed readiness probe must still pass. The default remains
`claude-opus-5`.
