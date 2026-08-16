#!/usr/bin/env bash
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${RUN_ROOT:?set RUN_ROOT to the completed run directory}"
runtime="$(RUN_ROOT="${RUN_ROOT}" GATE_REPO="${GATE_REPO:-}" "${HERE}/prepare_gate_runtime.sh")"
report_dir="${RUN_ROOT}/gate-reports"
mkdir -p "${report_dir}"

python3 - "${RUN_ROOT}/raw" <<'PY'
import json
import sys
from pathlib import Path

raw = Path(sys.argv[1])
tasks = sorted((raw / "tasks").glob("*/precheck.json"))
if not tasks:
    raise SystemExit("no finalized raw task precheck files found")
failed = []
for path in tasks:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        failed.append(f"{path}: unreadable ({exc})")
        continue
    if report.get("ok") is not True:
        failed.append(f"{path}: {report.get('errors')}")
quarantine = list((raw / "quarantine").glob("*")) if (raw / "quarantine").is_dir() else []
if quarantine:
    failed.append(f"quarantine contains {len(quarantine)} trajectories")
if failed:
    raise SystemExit("raw task Gate failed:\n" + "\n".join(failed))
print(f"raw_precheck_ok tasks={len(tasks)} quarantine=0")
PY

python3 "${HERE}/validate_nova_proxy_run.py" "${RUN_ROOT}"

python3 "${runtime}/scripts/scan_tool_trajectory_integrity.py" \
  "${RUN_ROOT}/proxy_traces/user_side_requests.jsonl" \
  --output-json "${report_dir}/tool-trajectory-integrity.json" \
  --findings-jsonl "${report_dir}/tool-trajectory-findings.jsonl" \
  --fail-on fabrication_suspect,protocol_leak,infra_error
