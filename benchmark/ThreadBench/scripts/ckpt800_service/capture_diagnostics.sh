#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
RUNTIME_DIR="${CKPT800_RUNTIME_DIR:-$REPO_ROOT/.supervision/ckpt800-service}"
REASON="${1:-manual}"
SAFE_REASON="$(printf '%s' "$REASON" | tr -cs 'A-Za-z0-9._-' '_')"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DIAG_DIR="$RUNTIME_DIR/diagnostics/${TIMESTAMP}_${SAFE_REASON}"

mkdir -p "$DIAG_DIR"

{
  printf 'captured_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'hostname=%s\n' "$(hostname)"
  printf 'reason=%s\n' "$REASON"
  printf 'kernel=%s\n' "$(uname -a)"
  printf 'uptime='
  cat /proc/uptime 2>/dev/null || true
} >"$DIAG_DIR/summary.txt" 2>&1

ps -eLo pid,ppid,lwp,psr,stat,etime,pcpu,pmem,wchan:32,comm,args \
  >"$DIAG_DIR/processes-and-threads.txt" 2>&1 || true

if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi -q >"$DIAG_DIR/nvidia-smi-q.txt" 2>&1 || true
  nvidia-smi \
    --query-gpu=index,uuid,name,memory.total,memory.used,utilization.gpu,utilization.memory,temperature.gpu,pstate \
    --format=csv >"$DIAG_DIR/nvidia-gpus.csv" 2>&1 || true
  nvidia-smi \
    --query-compute-apps=gpu_uuid,pid,process_name,used_memory \
    --format=csv >"$DIAG_DIR/nvidia-processes.csv" 2>&1 || true
  nvidia-smi pmon -c 1 >"$DIAG_DIR/nvidia-pmon.txt" 2>&1 || true
  nvidia-smi dmon -c 1 >"$DIAG_DIR/nvidia-dmon.txt" 2>&1 || true
  nvidia-smi nvlink --status >"$DIAG_DIR/nvidia-nvlink-status.txt" 2>&1 || true
fi

curl -sS --max-time 5 \
  "${CKPT800_METRICS_URL:-http://127.0.0.1:8000/metrics}" \
  >"$DIAG_DIR/metrics.prom" 2>"$DIAG_DIR/metrics.err" || true

mapfile -t service_pids < <(
  ps -eo pid=,args= |
    awk '/VLLM::EngineCore|vllm serve/ && $0 !~ /awk/ {print $1}' |
    sort -n -u
)

for pid in "${service_pids[@]}"; do
  proc_dir="$DIAG_DIR/proc-$pid"
  mkdir -p "$proc_dir"
  for name in status limits sched wchan stack; do
    timeout 2s cat "/proc/$pid/$name" >"$proc_dir/$name.txt" 2>&1 || true
  done
  timeout 2s ls -l "/proc/$pid/fd" >"$proc_dir/fds.txt" 2>&1 || true

  if command -v py-spy >/dev/null 2>&1; then
    timeout 15s py-spy dump --pid "$pid" \
      >"$proc_dir/py-spy.txt" 2>&1 || true
  fi
  if command -v pstack >/dev/null 2>&1; then
    timeout 20s pstack "$pid" >"$proc_dir/pstack.txt" 2>&1 || true
  elif command -v gdb >/dev/null 2>&1; then
    timeout 20s gdb --batch --quiet -p "$pid" \
      -ex 'set pagination off' \
      -ex 'thread apply all backtrace' \
      -ex detach \
      -ex quit >"$proc_dir/gdb-backtrace.txt" 2>&1 || true
  fi

  thread_count=0
  for task_dir in /proc/"$pid"/task/*; do
    [[ -d "$task_dir" ]] || continue
    tid="${task_dir##*/}"
    timeout 2s cat "$task_dir/wchan" \
      >"$proc_dir/thread-${tid}-wchan.txt" 2>&1 || true
    timeout 2s cat "$task_dir/stack" \
      >"$proc_dir/thread-${tid}-stack.txt" 2>&1 || true
    thread_count=$((thread_count + 1))
    [[ "$thread_count" -lt 256 ]] || break
  done
done

printf '%s\n' "$DIAG_DIR"
