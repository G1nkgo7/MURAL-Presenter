#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${RUN_ROOT:?set RUN_ROOT to the dedicated CCI rollout root}"
: "${QUERIES:?set QUERIES to the rollout JSONL file}"
: "${MODEL_KEYS_ENV:?set MODEL_KEYS_ENV to the model credential env file}"
: "${CLOUD3_MODEL_KEYS_ENV:?set CLOUD3_MODEL_KEYS_ENV to the Cloud3 credential env file}"
: "${SEARCH_KEYS_ENV:?set SEARCH_KEYS_ENV to the search credential env file}"
KEY_ENV="${MODEL_KEYS_ENV}"
CLOUD3_KEY_ENV="${CLOUD3_MODEL_KEYS_ENV}"
SEARCH_ENV="${SEARCH_KEYS_ENV}"
MURAL_ROOT="$(cd "${HERE}/../../.." && pwd)"
PIPELINE_ROOT="${MURAL_PIPELINE_ROOT:-$(cd "${MURAL_ROOT}/../ppt-html-pipeline" && pwd)}"
FEISHU_NOTIFY="${FEISHU_NOTIFY:-}"
MODEL_47="${NOVA_AGENT_MODEL:-claude-opus-4-7-thinking}"
MODEL_CLOUD3="${NOVA_CLOUD3_AGENT_MODEL:-claude-opus-4-7-thinking}"
INITIAL_TOTAL="${INITIAL_TOTAL_CONCURRENCY:-32}"
EXPECTED_QUERY_COUNT="${EXPECTED_QUERY_COUNT:-1000}"
ROUTE_CAP="${ROUTE_CAP_CONCURRENCY:-22}"
CHILD_INITIAL="${CLEAN_CHILD_CONCURRENCY:-2}"
CHILD_CAP="${CLEAN_CHILD_POOL_MAX_WORKERS:-4}"
POPPLER_ROOT="${POPPLER_ROOT:-${RUN_ROOT}/runtime/poppler-env}"

notify() {
  [[ -n "${FEISHU_NOTIFY}" ]] || return 0
  python3 "${FEISHU_NOTIFY}" "$@" || true
}

# A persisted operator target wins on container resume.  The CCI definition may
# still contain its original bootstrap value, but an ordinary stop/start must
# not silently raise concurrency after the operator has reduced it.
if [[ -r "${RUN_ROOT}/live/total-concurrency" ]]; then
  persisted_total="$(tr -dc '0-9' <"${RUN_ROOT}/live/total-concurrency")"
  [[ -n "${persisted_total}" ]] && INITIAL_TOTAL="${persisted_total}"
fi

[[ -r "${QUERIES}" ]] || { echo "missing queries: ${QUERIES}" >&2; exit 2; }
[[ -r "${KEY_ENV}" ]] || { echo "missing key env: ${KEY_ENV}" >&2; exit 2; }
[[ -r "${CLOUD3_KEY_ENV}" ]] || { echo "missing Cloud3 key env: ${CLOUD3_KEY_ENV}" >&2; exit 2; }
[[ -r "${SEARCH_ENV}" ]] || { echo "missing search key env: ${SEARCH_ENV}" >&2; exit 2; }
mkdir -p "${RUN_ROOT}/queries" "${RUN_ROOT}/logs" "${RUN_ROOT}/live"

# Storage is a hard precondition.  Exercise both extent allocation and metadata
# writes before starting a proxy or spending any teacher-model tokens.
python3 "${HERE}/storage_preflight.py" \
  --root "${RUN_ROOT}" \
  --report "${RUN_ROOT}/runtime/storage-preflight-start.json"

set -a
# shellcheck disable=SC1090
source "${KEY_ENV}"
# Cloud3 has a separately managed route credential.  Loading it second makes
# the override explicit without changing Cloud1/2 or the shared image key.
# shellcheck disable=SC1090
source "${CLOUD3_KEY_ENV}"
set +a
for name in TOKENHUB_CLOUD_KEY_1 TOKENHUB_CLOUD_KEY_2 TOKENHUB_CLOUD_KEY_3; do
  [[ -n "${!name:-}" ]] || { echo "missing ${name}" >&2; exit 2; }
done

python3 - "${QUERIES}" "${RUN_ROOT}/queries" "${EXPECTED_QUERY_COUNT}" <<'PY'
import json
import os
import sys
from pathlib import Path

source, output, expected = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
rows = []
for line_number, raw in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
    if not raw.strip():
        continue
    row = json.loads(raw)
    if not isinstance(row, dict) or not str(row.get("query") or "").strip():
        raise SystemExit(f"invalid query row {line_number}")
    rows.append(row)
if len(rows) != expected:
    raise SystemExit(f"expected {expected} queries, got {len(rows)}")
output.mkdir(parents=True, exist_ok=True)
for route in range(3):
    target = output / f"cloud{route + 1}.jsonl"
    temporary = target.with_name(f".{target.name}.tmp.{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        for index, row in enumerate(rows):
            if index % 3 == route:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(temporary, target)
print("query_shards=" + ",".join(
    str(sum(1 for index in range(len(rows)) if index % 3 == route))
    for route in range(3)
))
PY

python3 "${HERE}/set_clean_bulk_concurrency.py" \
  "${RUN_ROOT}" "${INITIAL_TOTAL}" --route-cap "${ROUTE_CAP}"

pids=()
cleanup() {
  for pid in "${pids[@]:-}"; do kill -TERM "${pid}" 2>/dev/null || true; done
}
trap cleanup TERM INT

for route in 1 2 3; do
  route_root="${RUN_ROOT}/routes/cloud${route}"
  mkdir -p "${route_root}/runtime" "${route_root}/logs"
  key_name="TOKENHUB_CLOUD_KEY_${route}"
  key_value="${!key_name}"
  route_model="${MODEL_47}"
  if [[ "${route}" == 3 ]]; then
    route_model="${MODEL_CLOUD3}"
  fi
  python3 - "${route_root}/teacher-model.json" "cloud${route}" "${route_model}" "${RUN_ROOT}/queries/cloud${route}.jsonl" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

path, route, model, queries = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
payload = {
    "schema": "mural.teacher-route.v1",
    "created_at": datetime.now(timezone.utc).isoformat(),
    "route": route,
    "teacher_model": model,
    "query_shard": queries,
}
temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
os.replace(temporary, path)
PY
  python3 - "${route_root}/runtime/api_keys.yaml" "${TOKENHUB_BASE_URL}" "${key_value}" <<'PY'
import os
import sys
from pathlib import Path

path, url, key = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
temporary.write_text(f"CLAUDE_A_F_URL: {url}\nCLAUDE_A_F: {key}\n", encoding="utf-8")
os.chmod(temporary, 0o600)
os.replace(temporary, path)
PY
  port=$((18030 + route))
  env RUN_ROOT="${route_root}" \
    API_KEYS_FILE="${route_root}/runtime/api_keys.yaml" \
    NOVA_PORT="${port}" NOVA_AGENT_MODEL="${route_model}" POPPLER_ROOT="${POPPLER_ROOT}" \
    bash "${HERE}/start_proxy_gate_off.sh" foreground \
    >>"${route_root}/logs/proxy-supervisor.log" 2>&1 &
  pids+=("$!")
done

for route in 1 2 3; do
  port=$((18030 + route))
  route_root="${RUN_ROOT}/routes/cloud${route}"
  ready=0
  for _ in $(seq 1 180); do
    if curl -fsS "http://127.0.0.1:${port}/health" >"${route_root}/health.bulk-start.json" 2>/dev/null; then
      ready=1
      break
    fi
    sleep 10
  done
  [[ "${ready}" == 1 ]] || { echo "cloud${route} proxy failed readiness" >&2; exit 3; }
done

# Run one fail-closed Harness environment check per route after Nova is healthy
# and before any Agent Loop starts.  This performs a real Chromium screenshot
# but no teacher/image model call, then persists a JSON report for audit.
for route in 1 2 3; do
  route_root="${RUN_ROOT}/routes/cloud${route}"
  port=$((18030 + route))
  route_model="${MODEL_47}"
  if [[ "${route}" == 3 ]]; then
    route_model="${MODEL_CLOUD3}"
  fi
  env RUN_ROOT="${route_root}" \
    QUERIES="${RUN_ROOT}/queries/cloud${route}.jsonl" \
    NOVA_PORT="${port}" NOVA_AGENT_MODEL="${route_model}" \
    MODEL_KEYS_ENV="${KEY_ENV}" SEARCH_KEYS_ENV="${SEARCH_ENV}" \
    POPPLER_ROOT="${POPPLER_ROOT}" \
    CLEAN_ENV_PREFLIGHT_ONLY=1 \
    CLEAN_ENV_PREFLIGHT_REPORT="${route_root}/runtime/clean-environment-preflight.json" \
    bash "${HERE}/run_ppt_html_clean.sh" \
    >"${route_root}/logs/environment-preflight.log" 2>&1 || {
      echo "cloud${route} Clean Harness environment preflight failed" >&2
      exit 4
    }
done

# Freeze the exact boundary after all three environment/search preflights pass
# and before any resumed Agent Loop is launched.  Outputs whose authoritative
# start time is at or after this timestamp belong to the fully configured
# segment; earlier work remains preserved but is not mislabeled as such.
python3 - "${RUN_ROOT}" "${HERE}" "${INITIAL_TOTAL}" "${PIPELINE_ROOT}" <<'PY'
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

root, script_root = map(Path, sys.argv[1:3])
initial_total = int(sys.argv[3])
pipeline_root = Path(sys.argv[4])
created_at = datetime.now(timezone.utc)
routes = {}
for route in ("cloud1", "cloud2", "cloud3"):
    route_root = root / "routes" / route
    report_path = route_root / "runtime/clean-environment-preflight.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    probes = {
        item["name"]: item["status"]
        for item in report.get("checks", [])
        if item.get("name") in {
            "live:serper-search",
            "live:serper-images",
            "runtime:nova-shared-gate-health",
            "chromium:render",
        }
    }
    if report.get("status") != "PASS" or probes.get("live:serper-search") != "PASS" or probes.get("live:serper-images") != "PASS":
        raise SystemExit(f"{route} environment/search preflight was not fully PASS")
    offsets = {}
    for relative in (
        "proxy_traces/user_side_requests.jsonl",
        "proxy_traces/agent_model_requests.jsonl",
        "proxy_traces/vision_model_requests.jsonl",
        "logs/batch.log",
    ):
        path = route_root / relative
        offsets[relative] = path.stat().st_size if path.is_file() else 0
    routes[route] = {
        "preflight_report": str(report_path),
        "probes": probes,
        "byte_offsets_before_agent_loop": offsets,
    }
files = [
    script_root / "run_clean_bulk_cci.sh",
    script_root / "run_ppt_html_clean.sh",
    pipeline_root / "core/tools.py",
    pipeline_root / "scripts/clean_environment_preflight.py",
]
payload = {
    "schema": "mural.environment-complete-boundary.v1",
    "created_at": created_at.isoformat(),
    "definition": "Deck attempts whose authoritative task start is at or after created_at use the complete Chromium/font/Nova/Serper/image configuration.",
    "total_deck_concurrency": initial_total,
    "prompt_cache_enabled": True,
    "prompt_cache_messages_enabled": True,
    "search_fallback_allowed": False,
    "routes": routes,
    "code_sha256": {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in files
    },
}
history = root / "environment-history"
history.mkdir(parents=True, exist_ok=True)
stamp = created_at.strftime("%Y%m%dT%H%M%SZ")
for output in (root / "ENVIRONMENT_COMPLETE_FROM.json", history / f"{stamp}.json"):
    temporary = output.with_name(f".{output.name}.tmp.{os.getpid()}")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, output)
formal_path = root / "FORMAL_DATA_BOUNDARY.json"
if not formal_path.exists():
    formal_payload = {
        "schema": "mural.formal-data-boundary.v1",
        "formal_data_from": payload["created_at"],
        "immutable": True,
        "authoritative_clock": "root orchestrator task_meta.created_at_epoch",
        "environment_snapshot": str(root / "ENVIRONMENT_COMPLETE_FROM.json"),
        "configuration": {
            "cloud1_model": "claude-opus-4-7-thinking",
            "cloud2_model": "claude-opus-4-7-thinking",
            "cloud3_model": "claude-opus-4-7-thinking",
            "prompt_cache_enabled": True,
            "prompt_cache_messages_enabled": True,
            "serper_search_required": True,
            "search_fallback_allowed": False,
            "chromium_preflight_required": True,
            "nova_proxy_preflight_required": True,
            "image_tool_preflight_required": True,
        },
        "direct_use_policy": {
            "root_started_at_or_after_boundary": True,
            "manifest_status": "completed",
            "nova_raw_v2": True,
            "nova_raw_precheck_ok": True,
            "review_completed": True,
            "final_view_after_review": True,
            "finalize_succeeded": True,
            "blank_pages": [],
            "console_errors": [],
            "unresolved_child_failures": [],
            "special_page_geometry_status": "PASS",
        },
        "preboundary_policy": "not_directly_usable; preserve raw and rerun",
        "raw_data_policy": "never delete or overwrite",
    }
    temporary = formal_path.with_name(f".{formal_path.name}.tmp.{os.getpid()}")
    temporary.write_text(json.dumps(formal_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, formal_path)
print(json.dumps({"environment_complete_from": payload["created_at"], "output": str(root / "ENVIRONMENT_COMPLETE_FROM.json")}, ensure_ascii=False))
PY

batch_logs=()
for route in 1 2 3; do
  batch_logs+=("${RUN_ROOT}/routes/cloud${route}/logs/batch.log")
done
enospc_cursor="${RUN_ROOT}/runtime/enospc-watch-cursor.json"
snapshot_args=(
  --run-root "${RUN_ROOT}"
  --cursor "${enospc_cursor}"
  --snapshot-only
)
for log_path in "${batch_logs[@]}"; do
  snapshot_args+=(--log "${log_path}")
done
python3 "${HERE}/watch_enospc.py" "${snapshot_args[@]}"

batch_pids=()
for route in 1 2 3; do
  route_root="${RUN_ROOT}/routes/cloud${route}"
  port=$((18030 + route))
  workers="$(tr -dc '0-9' <"${route_root}/live/deck-concurrency")"
  route_model="${MODEL_47}"
  if [[ "${route}" == 3 ]]; then
    route_model="${MODEL_CLOUD3}"
  fi
  # A separate process group lets the ENOSPC watchdog terminate the runner and
  # every Deck worker together instead of leaving model-using orphans behind.
  setsid env RUN_ROOT="${route_root}" \
    QUERIES="${RUN_ROOT}/queries/cloud${route}.jsonl" \
    BATCH="clean-1k-cloud${route}-v1" WORKERS="${workers}" \
    NOVA_PORT="${port}" NOVA_AGENT_MODEL="${route_model}" CLEAN_RESUME=1 \
    CLEAN_CONCURRENCY_FILE="${route_root}/live/deck-concurrency" \
    CLEAN_POOL_MAX_WORKERS="${ROUTE_CAP}" \
    CLEAN_CHILD_CONCURRENCY="${CHILD_INITIAL}" \
    CLEAN_CHILD_POOL_MAX_WORKERS="${CHILD_CAP}" \
    CLEAN_REMOTE_TOOL_CONCURRENCY=4 \
    CLEAN_PROMPT_CACHE=1 CLEAN_PROMPT_CACHE_MESSAGES=1 \
    MODEL_KEYS_ENV="${KEY_ENV}" SEARCH_KEYS_ENV="${SEARCH_ENV}" \
    POPPLER_ROOT="${POPPLER_ROOT}" \
    bash "${HERE}/run_ppt_html_clean.sh" \
    >>"${route_root}/logs/batch.log" 2>&1 &
  batch_pids+=("$!")
  pids+=("$!")
done

watch_args=(
  --run-root "${RUN_ROOT}"
  --cursor "${enospc_cursor}"
)
for log_path in "${batch_logs[@]}"; do
  watch_args+=(--log "${log_path}")
done
for pid in "${batch_pids[@]}"; do
  watch_args+=(--pid "${pid}")
done
python3 "${HERE}/watch_enospc.py" "${watch_args[@]}" &
enospc_watch_pid="$!"
pids+=("${enospc_watch_pid}")

# One notification per hour, independent of model calls.  It reads manifests
# only and stops as soon as the three batch processes finish.
hourly_notifier() {
  while sleep 3600; do
    alive=0
    for pid in "${batch_pids[@]}"; do
      if kill -0 "${pid}" 2>/dev/null; then alive=1; break; fi
    done
    [[ "${alive}" == 1 ]] || return 0
    message="$(python3 "${HERE}/report_clean_bulk_progress.py" "${RUN_ROOT}")"
    notify "${message}"
  done
}
hourly_notifier &
notifier_pid="$!"
pids+=("${notifier_pid}")

failed=0
for pid in "${batch_pids[@]}"; do
  wait "${pid}" || failed=1
done
kill -TERM "${notifier_pid}" 2>/dev/null || true
kill -TERM "${enospc_watch_pid}" 2>/dev/null || true
wait "${enospc_watch_pid}" 2>/dev/null || true
date -u +%FT%TZ >"${RUN_ROOT}/BATCH_FINISHED_AT"
python3 "${HERE}/refresh_clean_bulk_data_status.py" "${RUN_ROOT}" >/dev/null || true
if [[ -f "${RUN_ROOT}/NO_SPACE_STOP.json" ]]; then
  failed=1
  touch "${RUN_ROOT}/BATCH_FAILED"
  notify \
    "【MURAL 数据 agent｜空间止损】批次因 ENOSPC/配额错误停止；禁止自动恢复。请清理空间、通过 storage_preflight 后重刷失败 Query。详情：${RUN_ROOT}/NO_SPACE_STOP.json" || true
elif [[ "${failed}" == 0 ]]; then
  touch "${RUN_ROOT}/BATCH_DONE"
  notify \
    "【MURAL 数据 agent】CCI 1K Nova rollout 三分片已全部结束（三路均为 Opus4.7 Thinking，独立 Cloud Key）；请查看 ${RUN_ROOT}" || true
else
  touch "${RUN_ROOT}/BATCH_FAILED"
  notify \
    "【MURAL 数据 agent】CCI 1K Nova rollout 有分片异常；请查看 ${RUN_ROOT}/routes/*/logs/batch.log" || true
fi

# CCI is Deployment-like: keep the container alive to prevent automatic rerun.
sleep 7d
