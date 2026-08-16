#!/usr/bin/env bash
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MURAL_ROOT="$(cd "${HERE}/../../.." && pwd)"
MURAL_VERSION="${MURAL_VERSION:-0.4}"
case "${MURAL_VERSION}" in
  0.2|0.4) ;;
  *) echo "unsupported MURAL_VERSION=${MURAL_VERSION}; expected 0.2 or 0.4" >&2; exit 2 ;;
esac
HARNESS="${MURAL_ROOT}/harnesses/mural-presenter-v${MURAL_VERSION}"
: "${RUN_ROOT:?set RUN_ROOT to a dedicated absolute run directory}"
NOVA_PORT="${NOVA_PORT:-8001}"
NOVA_AGENT_MODEL="${NOVA_AGENT_MODEL:-claude-opus-5}"
if [[ -z "${NOVA_THINKING_GATE:-}" ]]; then
  if [[ "${MURAL_VERSION}" == "0.4" ]]; then
    NOVA_THINKING_GATE=off
    NOVA_THINKING_RETRIES=0
  else
    NOVA_THINKING_GATE=visible_signed
    NOVA_THINKING_RETRIES=1
  fi
else
  NOVA_THINKING_RETRIES="${NOVA_THINKING_RETRIES:-$([[ "${NOVA_THINKING_GATE}" == "off" ]] && echo 0 || echo 1)}"
fi
proxy_url="${NOVA_PROXY_BASE_URL:-http://127.0.0.1:${NOVA_PORT}/v1}"
# Anthropic's Python client appends ``/v1/messages`` to its configured origin.
# Keep the externally documented Gate URL versioned, but pass the origin to the
# SDK so the physical request is exactly /v1/messages rather than /v1/v1/messages.
proxy_origin="${proxy_url%/}"
proxy_origin="${proxy_origin%/v1}"

CLEAN_NOVA_GATE_AGENT_MODEL="${NOVA_AGENT_MODEL}" \
CLEAN_NOVA_GATE_THINKING_GATE="${NOVA_THINKING_GATE}" \
CLEAN_NOVA_GATE_THINKING_RETRIES="${NOVA_THINKING_RETRIES}" \
MURAL_VERSION="${MURAL_VERSION}" \
NOVA_PROXY_BASE_URL="${proxy_url}" \
  "${HERE}/preflight.py" >/dev/null
mkdir -p "${RUN_ROOT}/raw" "${RUN_ROOT}/mural"

export CLEAN_DOTENV_PATH=/dev/null
export CLEAN_NOVA_RAW_V2=1
export CLEAN_NOVA_GATE_V1=1
export CLEAN_NOVA_GATE_AGENT_MODEL="${NOVA_AGENT_MODEL}"
export CLEAN_NOVA_GATE_THINKING_GATE="${NOVA_THINKING_GATE}"
export CLEAN_NOVA_GATE_THINKING_RETRIES="${NOVA_THINKING_RETRIES}"
export CLEAN_NOVA_RAW_ROOT="${RUN_ROOT}/raw"
export NOVA_RUN_ID="${NOVA_RUN_ID:-$(basename "${RUN_ROOT}")}"
export NOVA_PROXY_BASE_URL="${proxy_origin}"
export NOVA_VISION_PROXY_BASE_URL="${proxy_origin}"
export VISION_BACKEND=nova
export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-nova-local-no-inbound-auth}"
export ANTHROPIC_MODEL="${NOVA_AGENT_MODEL}"
export CLEAN_THINKING=1
export CLEAN_EFFORT=high
export CLEAN_MAX_TOKENS=65536
# The Proxy owns the frozen 1200s timeout for each internal provider call.  A
# complete Nova Agent loop can contain multiple such calls, so the outer Hermes
# request must not impose another short boundary or disconnect mid-loop.
export CLEAN_MODEL_TIMEOUT=86400
export CLEAN_FIRST_RESPONSE_TIMEOUT=86400
export CLEAN_ACTIVE_RESPONSE_TIMEOUT=86400
export CLEAN_PROMPT_CACHE="${CLEAN_PROMPT_CACHE:-1}"
export CLEAN_PROMPT_CACHE_MESSAGES="${CLEAN_PROMPT_CACHE_MESSAGES:-1}"
# A role may legitimately need several explicit 1200s attempts.  Keep the
# per-request Gate timeout frozen while preventing the role liveness boundary
# from expiring after exactly two attempts.
export CLEAN_CHILD_WALL_TIMEOUT=86400
export VISION_NOVA_TIMEOUT=86400
export VISION_NOVA_MAX_TOKENS=65536
export CLEAN_ARTIFACT_ROOT="${RUN_ROOT}/mural"
export CLEAN_SKILLS_DIR="${MURAL_ROOT}/skills/mural-presenter-v${MURAL_VERSION}"
if [[ "${MURAL_VERSION}" == "0.4" ]]; then
  export CLEAN_SKILL_NAME=mural-presenter-v0-4
  export CLEAN_SKILL_NAME_ZH=mural-presenter-v0-4
  export CLEAN_SKILL_NAME_EN=mural-presenter-v0-4
fi
export MURAL_RUN_MODE=synthesis

exec python3 "${HARNESS}/infer.py" "$@"
