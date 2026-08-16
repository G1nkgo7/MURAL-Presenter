# Guarded ckpt800 SCO service

This directory contains the gaozhenxi-owned startup and recovery path for the
`ckpt800` vLLM service.

- `serve_guarded.sh` starts vLLM with `max-num-seqs=64`.
- `probe_chat_completion.sh` verifies a real minimal inference, not just
  `/health` or `/v1/models`.
- `inference_watchdog.sh` treats three consecutive inference failures as an
  engine failure, captures diagnostics, and terminates vLLM so SCO fault
  tolerance can restart the job.
- `capture_diagnostics.sh` saves process/thread state, GPU state, metrics, and
  best-effort EngineCore stacks under
  `.supervision/ckpt800-service/diagnostics/`.

The SCO job must be created with fault tolerance, anomaly detection, and a
positive retry count. The watchdog intentionally exits the serving command on
an unrecoverable probe failure; SCO owns the subsequent job restart.

For the currently installed `sco-acp`, `--enable-anomaly-detection` enables the
SCO health monitor and `--enable-fault-tolerance --retry-times N` enables job
recovery. The CLI does not expose an `--enable-checker` flag, so the real
`/v1/chat/completions` watchdog in this directory is the service-level checker.
