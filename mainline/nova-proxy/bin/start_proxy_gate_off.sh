#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
: "${RUN_ROOT:?set RUN_ROOT to a dedicated absolute run directory}"
ACTION="${1:-start}"
NOVA_PORT="${NOVA_PORT:-18018}"
NOVA_AGENT_MODEL="${NOVA_AGENT_MODEL:-claude-opus-4-7-thinking}"

runtime="$(RUN_ROOT="${RUN_ROOT}" GATE_REPO="${GATE_REPO:-}" "${HERE}/prepare_gate_runtime.sh")"
overlay="${ROOT}/overlays/gate-off-v1.1.patch"
marker="${RUN_ROOT}/local-gate-off-overlay.json"

if [[ ! -f "${marker}" ]]; then
  patch -d "${runtime}" -p1 --forward --silent <"${overlay}"
  python3 - "${marker}" "${runtime}" "${overlay}" <<'PY'
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

output, runtime, overlay = map(Path, sys.argv[1:])
files = [
    runtime / "outputs/agentloop/openai_chat_completion_entry.py",
    runtime / "outputs/agentloop/proxy_server.py",
    runtime / "outputs/agentloop/test_engine_terminal_contracts.py",
    runtime / "scripts/start_tianjin_proxy.sh",
]
record = {
    "schema_version": "nova.gate_off.local_overlay.v2",
    "upstream_tag": "sensenova-harness-vision-v1.1-20260815",
    "upstream_commit": "4a067c75e4db7ea5ba81c264a1dc0ddac24f411e",
    "profile": "opus47_gate_off_brushing",
    "thinking_policy": "force_on",
    "anthropic_agent_thinking_gate": "off",
    "signature_policy": "preserve provider blocks verbatim; never synthesize",
    "overlay_sha256": hashlib.sha256(overlay.read_bytes()).hexdigest(),
    "patched_file_sha256": {
        str(path.relative_to(runtime)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in files
    },
    "applied_at": datetime.now(timezone.utc).isoformat(),
}
output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
fi

mkdir -p "${RUN_ROOT}/proxy_traces" "${RUN_ROOT}/proxy_artifacts" "${RUN_ROOT}/logs"
if [[ "${ACTION}" =~ ^(start|restart)$ ]]; then
  : "${API_KEYS_FILE:?set API_KEYS_FILE to a controlled YAML file}"
  [[ -r "${API_KEYS_FILE}" ]] || { echo "API_KEYS_FILE is not readable" >&2; exit 2; }
fi

if [[ -z "${POPPLER_ROOT:-}" && -d "${RUN_ROOT}/runtime/poppler-env/bin" ]]; then
  POPPLER_ROOT="${RUN_ROOT}/runtime/poppler-env"
else
  POPPLER_ROOT="${POPPLER_ROOT:-${RUN_ROOT}/runtime/poppler}"
fi
if [[ -d "${POPPLER_ROOT}/usr/bin" ]]; then
  export POPPLER_ROOT
  export PATH="${ROOT}/poppler-bin:${PATH}"
elif [[ -d "${POPPLER_ROOT}/bin" ]]; then
  export POPPLER_ROOT
  export PATH="${POPPLER_ROOT}/bin:${PATH}"
  export LD_LIBRARY_PATH="${POPPLER_ROOT}/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
fi

INSTALL_DIR="${runtime}" \
RUN_ID="${NOVA_RUN_ID:-$(basename "${RUN_ROOT}")}" \
RUN_ROOT="${RUN_ROOT}" \
HOST=127.0.0.1 \
PORT="${NOVA_PORT}" \
DEPLOY_PROFILE=opus47_gate_off_brushing \
API_KEYS_FILE="${API_KEYS_FILE:-/dev/null}" \
ANTHROPIC_CONFIG_PREFIX=CLAUDE_A_F \
AGENT_PAYLOAD_FORMAT=anthropic \
AGENT_PROMPT_PROTOCOL=anthropic_native \
AGENT_MODEL="${NOVA_AGENT_MODEL}" \
AGENT_MAX_TOKENS=65536 \
AGENT_HARD_MAX_TOKENS=65536 \
AGENT_SEND_IMAGES_FIELD='' \
VISION_BACKEND=agent \
VISION_MODEL="${NOVA_AGENT_MODEL}" \
VISION_MAX_TOKENS=8192 \
VISION_HARD_MAX_TOKENS=8192 \
VISION_RETRIES=1 \
VISION_TEMPERATURE=0.0 \
VISION_EXTRA_JSON='{}' \
THINKING_POLICY=force_on \
ANTHROPIC_AGENT_THINKING_GATE=off \
ANTHROPIC_AGENT_THINKING_RETRIES=0 \
DISABLE_AGENT_VISION_INPUT=1 \
DISABLE_RENDER_VISION_PLACEHOLDERS=0 \
BUILTIN_TRACE_FORMAT=natural \
ALLOW_INTERNAL_TRACE=1 \
TRACE_MODE=full \
TRACE_MAX_TOTAL_BYTES=536870912000 \
TRACE_MAX_FILE_BYTES=214748364800 \
TRACE_OVERFLOW=error \
NON_STRICT_TOOL_OUTPUT=0 \
MAX_STEPS=24 \
EMPTY_OUTPUT_RETRIES=2 \
TIMEOUT=1200 \
SSE_HEARTBEAT_SECONDS=5 \
LIGHTLLM_ANTHROPIC_ENABLE_PDF_PARSING=1 \
TEMPLATE='' \
TRACE_DIR="${RUN_ROOT}/proxy_traces" \
ARTIFACT_DIR="${RUN_ROOT}/proxy_artifacts" \
LOG_DIR="${RUN_ROOT}/logs" \
PID_FILE="${RUN_ROOT}/logs/nova_vision_proxy.pid" \
LOG_FILE="${RUN_ROOT}/logs/nova_vision_proxy.log" \
bash "${runtime}/scripts/start_tianjin_proxy.sh" "${ACTION}"

if [[ "${ACTION}" == "start" || "${ACTION}" == "restart" ]]; then
  python3 - "${NOVA_PORT}" "${NOVA_AGENT_MODEL}" "${RUN_ROOT}/health.start.json" <<'PY'
import json
import sys
import urllib.request
from pathlib import Path

port, model, output = sys.argv[1:]
with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=10) as response:
    health = json.load(response)
expected = {
    "ok": True,
    "agent_model": model,
    "vision_model": model,
    "thinking_policy": "force_on",
    "anthropic_agent_thinking_gate": "off",
    "anthropic_agent_thinking_retries": 0,
    "internal_trace_enabled": True,
}
failures = [f"{key}={health.get(key)!r}, expected {value!r}" for key, value in expected.items() if health.get(key) != value]
if failures:
    raise SystemExit("gate-off health mismatch: " + "; ".join(failures))
Path(output).write_text(json.dumps(health, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"ok": True, "gate": "off", "health": output}, ensure_ascii=False))
PY
fi
