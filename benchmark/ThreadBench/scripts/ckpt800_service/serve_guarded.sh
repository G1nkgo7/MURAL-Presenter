#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
RUNTIME_DIR="${CKPT800_RUNTIME_DIR:-$REPO_ROOT/.supervision/ckpt800-service}"
CHECKPOINT="${CKPT800_CHECKPOINT:?set CKPT800_CHECKPOINT to the model checkpoint directory}"
PORT="${CKPT800_PORT:-8000}"
MAX_NUM_SEQS="${CKPT800_MAX_NUM_SEQS:-64}"
MAX_MODEL_LEN="${CKPT800_MAX_MODEL_LEN:-131072}"
GPU_MEMORY_UTILIZATION="${CKPT800_GPU_MEMORY_UTILIZATION:-0.92}"
RUN_ID="${SCO_JOB_ID:-manual}_$(date -u +%Y%m%dT%H%M%SZ)"
SERVE_LOG="$RUNTIME_DIR/serve-${RUN_ID}.log"
WATCHDOG_LOG="$RUNTIME_DIR/watchdog-${RUN_ID}.log"

mkdir -p "$RUNTIME_DIR"

for value_name in PORT MAX_NUM_SEQS MAX_MODEL_LEN; do
  value="${!value_name}"
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || {
    printf 'invalid %s: %s\n' "$value_name" "$value" >&2
    exit 2
  }
done

[[ -d "$CHECKPOINT" ]] || {
  printf 'checkpoint directory not found: %s\n' "$CHECKPOINT" >&2
  exit 1
}
command -v vllm >/dev/null 2>&1 || {
  printf 'vllm command not found\n' >&2
  exit 1
}

export CKPT800_BASE_URL="http://127.0.0.1:${PORT}/v1"
export CKPT800_METRICS_URL="http://127.0.0.1:${PORT}/metrics"
export CKPT800_RUNTIME_DIR="$RUNTIME_DIR"

printf 'starting guarded ckpt800 service\n'
printf 'checkpoint=%s\n' "$CHECKPOINT"
printf 'port=%s max_model_len=%s max_num_seqs=%s gpu_memory_utilization=%s\n' \
  "$PORT" "$MAX_MODEL_LEN" "$MAX_NUM_SEQS" "$GPU_MEMORY_UTILIZATION"
printf 'serve_log=%s\nwatchdog_log=%s\n' "$SERVE_LOG" "$WATCHDOG_LOG"

setsid vllm serve "$CHECKPOINT" \
  --served-model-name ckpt800 \
  --port "$PORT" \
  --tensor-parallel-size 2 \
  --max-model-len "$MAX_MODEL_LEN" \
  --gpu-memory-utilization "$GPU_MEMORY_UTILIZATION" \
  --enable-prefix-caching \
  --max-num-seqs "$MAX_NUM_SEQS" \
  --reasoning-parser qwen3 \
  --trust-remote-code \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder \
  >>"$SERVE_LOG" 2>&1 &
server_pid=$!

"$SCRIPT_DIR/inference_watchdog.sh" "$server_pid" \
  >>"$WATCHDOG_LOG" 2>&1 &
watchdog_pid=$!

shutdown() {
  trap - INT TERM
  kill -TERM "$watchdog_pid" 2>/dev/null || true
  kill -TERM -- "-$server_pid" 2>/dev/null || true
  wait "$server_pid" 2>/dev/null || true
  wait "$watchdog_pid" 2>/dev/null || true
  exit 143
}
trap shutdown INT TERM

wait "$server_pid"
server_rc=$?

kill -TERM "$watchdog_pid" 2>/dev/null || true
wait "$watchdog_pid" 2>/dev/null || true

if (( server_rc != 0 )); then
  "$SCRIPT_DIR/capture_diagnostics.sh" "vllm_exit_${server_rc}" \
    >>"$WATCHDOG_LOG" 2>&1 || true
fi

printf 'vLLM exited with code %s\n' "$server_rc" >&2
exit "$server_rc"
