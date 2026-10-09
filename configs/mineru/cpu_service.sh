#!/usr/bin/env bash
set -euo pipefail
: "${MURAL_MINERU_SERVICE_ROOT:?Set an absolute directory for your MinerU CPU service}"
SERVICE_ROOT="$MURAL_MINERU_SERVICE_ROOT"
SERVICE_SCRIPT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/cpu_service.sh"
TMUX="${MURAL_TMUX_BIN:-tmux}"
mkdir -p "$SERVICE_ROOT"/{tmp,logs,cache,output,evidence}
export TMPDIR="$SERVICE_ROOT/tmp"
export PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
export LD_LIBRARY_PATH="$SERVICE_ROOT/system/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export HF_HOME="$SERVICE_ROOT/cache/huggingface"
export MODELSCOPE_CACHE="$SERVICE_ROOT/cache/modelscope"
export XDG_CACHE_HOME="$SERVICE_ROOT/cache"
export MPLCONFIGDIR="$SERVICE_ROOT/cache/matplotlib"
export MINERU_TOOLS_CONFIG_JSON="$SERVICE_ROOT/mineru.json"
export MINERU_MODEL_SOURCE=local MINERU_DEVICE_MODE=cpu CUDA_VISIBLE_DEVICES=""
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1
export MINERU_API_OUTPUT_ROOT="$SERVICE_ROOT/output"
export MINERU_API_ENABLE_FASTAPI_DOCS=0
export MINERU_API_TASK_RETENTION_SECONDS=3600
SOCKET="$SERVICE_ROOT/tmp/manager.sock"

case "${1:-status}" in
  start)
    test -x "$SERVICE_ROOT/env/bin/mineru-api"
    test -f "$SERVICE_ROOT/evidence/installation.json"
    if "$TMUX" -S "$SOCKET" has-session -t mineru-cpu 2>/dev/null; then
      echo 'CPU manager already running'
      exit 0
    fi
    "$TMUX" -S "$SOCKET" new-session -d -s mineru-cpu "bash $SERVICE_SCRIPT supervise"
    echo 'CPU manager started; verify health before use'
    ;;
  status)
    "$TMUX" -S "$SOCKET" list-sessions
    "$SERVICE_ROOT/env/bin/python" -c 'import requests; s=requests.Session(); s.trust_env=False; r=s.get("http://127.0.0.1:8201/health",timeout=5); r.raise_for_status(); print(r.text)'
    ;;
  stop)
    "$TMUX" -S "$SOCKET" send-keys -t mineru-cpu C-c
    ;;
  supervise)
    child=''
    stop_child() {
      trap - INT TERM HUP
      if [ -n "$child" ]; then kill -TERM "$child" 2>/dev/null || true; wait "$child" || true; fi
      exit 0
    }
    trap stop_child INT TERM HUP
    while true; do
      bash "$SERVICE_SCRIPT" serve >> "$SERVICE_ROOT/logs/service.log" 2>&1 &
      child=$!
      result=0
      wait "$child" || result=$?
      child=''
      printf 'CPU service exited %s; restarting in 5s\n' "$result" >> "$SERVICE_ROOT/logs/service.log"
      sleep 5
    done
    ;;
  serve)
    cd "$SERVICE_ROOT"
    exec "$SERVICE_ROOT/env/bin/python" "$(dirname "$SERVICE_SCRIPT")/service_runtime.py" cpu \
      --host 127.0.0.1 --port 8201 --enable-vlm-preload false
    ;;
  *) echo 'Usage: bash configs/mineru/cpu_service.sh start|status|stop' >&2; exit 2 ;;
esac
