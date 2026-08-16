#!/usr/bin/env bash
set -euo pipefail

[[ $# -eq 1 && "$1" =~ ^[1-9][0-9]*$ ]] || {
  printf 'usage: %s <vllm-process-group-id>\n' "$0" >&2
  exit 2
}

SERVER_PGID="$1"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROBE="$SCRIPT_DIR/probe_chat_completion.sh"
CAPTURE="$SCRIPT_DIR/capture_diagnostics.sh"

INTERVAL_SECONDS="${CKPT800_WATCHDOG_INTERVAL_SECONDS:-60}"
FAILURE_THRESHOLD="${CKPT800_WATCHDOG_FAILURE_THRESHOLD:-3}"
STARTUP_TIMEOUT_SECONDS="${CKPT800_STARTUP_TIMEOUT_SECONDS:-1800}"
SHUTDOWN_GRACE_SECONDS="${CKPT800_SHUTDOWN_GRACE_SECONDS:-30}"

for value_name in \
  INTERVAL_SECONDS FAILURE_THRESHOLD STARTUP_TIMEOUT_SECONDS SHUTDOWN_GRACE_SECONDS; do
  value="${!value_name}"
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || {
    printf 'invalid %s: %s\n' "$value_name" "$value" >&2
    exit 2
  }
done

log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

terminate_server() {
  local reason="$1"
  local diag_dir

  diag_dir="$("$CAPTURE" "$reason" 2>&1 || true)"
  log "diagnostics captured: $diag_dir"
  log "terminating vLLM process group $SERVER_PGID"
  kill -TERM -- "-$SERVER_PGID" 2>/dev/null || true

  for ((i = 0; i < SHUTDOWN_GRACE_SECONDS; i++)); do
    kill -0 "$SERVER_PGID" 2>/dev/null || return 0
    sleep 1
  done

  log "vLLM did not stop within ${SHUTDOWN_GRACE_SECONDS}s; sending SIGKILL"
  kill -KILL -- "-$SERVER_PGID" 2>/dev/null || true
}

startup_deadline=$(( $(date +%s) + STARTUP_TIMEOUT_SECONDS ))
while true; do
  if ! kill -0 "$SERVER_PGID" 2>/dev/null; then
    log "vLLM exited before the first successful inference probe"
    exit 1
  fi
  if "$PROBE" >/dev/null 2>&1; then
    log "initial chat-completions probe passed"
    break
  fi
  if (( $(date +%s) >= startup_deadline )); then
    log "initial inference did not become healthy within ${STARTUP_TIMEOUT_SECONDS}s"
    terminate_server "startup_probe_timeout"
    exit 1
  fi
  sleep 10
done

consecutive_failures=0
while kill -0 "$SERVER_PGID" 2>/dev/null; do
  sleep "$INTERVAL_SECONDS"
  if "$PROBE" >/dev/null 2>&1; then
    if (( consecutive_failures > 0 )); then
      log "chat-completions probe recovered after $consecutive_failures failure(s)"
    fi
    consecutive_failures=0
    continue
  fi

  consecutive_failures=$((consecutive_failures + 1))
  log "chat-completions probe failed ($consecutive_failures/$FAILURE_THRESHOLD)"
  if (( consecutive_failures >= FAILURE_THRESHOLD )); then
    terminate_server "chat_probe_failed_${consecutive_failures}_times"
    exit 1
  fi
done

log "vLLM process group exited; watchdog stopping"
