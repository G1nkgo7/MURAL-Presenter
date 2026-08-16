#!/usr/bin/env bash
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${RUN_ROOT:?set RUN_ROOT to a dedicated absolute run directory}"
ACTION="${1:-start}"
NOVA_PORT="${NOVA_PORT:-8001}"
NOVA_AGENT_MODEL="${NOVA_AGENT_MODEL:-claude-opus-5}"

runtime="$(RUN_ROOT="${RUN_ROOT}" GATE_REPO="${GATE_REPO:-}" "${HERE}/prepare_gate_runtime.sh")"
mkdir -p "${RUN_ROOT}/proxy_traces" "${RUN_ROOT}/proxy_artifacts" "${RUN_ROOT}/logs"

# Containers used for brushing may provide Poppler as a run-local package
# instead of a system package.  This remains a real binary dependency: the
# Gate health check still rejects missing pdftotext/pdftoppm/pdfinfo.
POPPLER_ROOT="${POPPLER_ROOT:-${RUN_ROOT}/runtime/poppler}"
if [[ -d "${POPPLER_ROOT}/usr/bin" ]]; then
  export POPPLER_ROOT
  export PATH="${HERE}/../poppler-bin:${PATH}"
fi

if [[ "${ACTION}" =~ ^(start|restart|foreground)$ ]]; then
  : "${API_KEYS_FILE:?set API_KEYS_FILE to a controlled YAML key file}"
  [[ -r "${API_KEYS_FILE}" ]] || { echo "API_KEYS_FILE is not readable" >&2; exit 2; }
fi

INSTALL_DIR="${runtime}" \
RUN_ID="${NOVA_RUN_ID:-$(basename "${RUN_ROOT}")}" \
RUN_ROOT="${RUN_ROOT}" \
HOST=127.0.0.1 \
PORT="${NOVA_PORT}" \
API_KEYS_FILE="${API_KEYS_FILE:-/dev/null}" \
ANTHROPIC_CONFIG_PREFIX=CLAUDE_A_F \
BRUSH_MODEL="${NOVA_AGENT_MODEL}" \
BRUSH_AGENT_MAX_TOKENS=65536 \
BRUSH_VISION_MAX_TOKENS=2048 \
SSE_HEARTBEAT_SECONDS=5 \
bash "${runtime}/scripts/run_opus5_signed_brushing_proxy.sh" "${ACTION}"
