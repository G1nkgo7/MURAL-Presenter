#!/usr/bin/env bash
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MURAL_ROOT="$(cd "${HERE}/../../.." && pwd)"
PIPELINE_ROOT="${MURAL_PIPELINE_ROOT:-$(cd "${MURAL_ROOT}/../ppt-html-pipeline" && pwd)}"

# A deployment may reuse a frozen Python site through CLEAN_SHARED_PYTHON_SITE
# instead of installing packages at every pod start.
SHARED_PYTHON_SITE="${CLEAN_SHARED_PYTHON_SITE:-}"
if [[ -d "${SHARED_PYTHON_SITE}" ]]; then
  export PYTHONPATH="${SHARED_PYTHON_SITE}${PYTHONPATH:+:${PYTHONPATH}}"
fi
CLEAN_PYTHON="${CLEAN_PYTHON:-$(command -v python3)}"
[[ -x "${CLEAN_PYTHON}" ]] || CLEAN_PYTHON="$(command -v python)"
"${CLEAN_PYTHON}" -c 'import anthropic, distro, pydantic, PIL, playwright' >/dev/null || {
  echo "Clean Harness dependency preflight failed: required Python packages are not importable" >&2
  exit 4
}

# CCI runs as root and would otherwise search /root/.cache/ms-playwright.  Use
# the shared, prevalidated Chromium installation used by local smoke runs.
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}"
export PPT_SKILL_BROWSER_EXE="${PPT_SKILL_BROWSER_EXE:-${PLAYWRIGHT_BROWSERS_PATH}/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell}"
export PPT_SKILL_BROWSER_LIB_DIRS="${PPT_SKILL_BROWSER_LIB_DIRS:-}"
export FONTCONFIG_FILE="${FONTCONFIG_FILE:-${PIPELINE_ROOT}/config/fontconfig-acp.conf}"
export PPT_FONT_SOURCE_DIRS="${PPT_FONT_SOURCE_DIRS:-$HOME/.fonts:$HOME/.local/share/fonts:${PIPELINE_ROOT}/fonts}"
mkdir -p /tmp/fontconfig-cache
[[ -x "${PPT_SKILL_BROWSER_EXE}" ]] || {
  echo "Clean Harness render preflight failed: browser is not executable: ${PPT_SKILL_BROWSER_EXE}" >&2
  exit 5
}
if [[ -n "${POPPLER_ROOT:-}" && -d "${POPPLER_ROOT}/bin" ]]; then
  export PATH="${POPPLER_ROOT}/bin:${PATH}"
  if [[ -r "${POPPLER_ROOT}/etc/fonts/fonts.conf" ]]; then
    export FONTCONFIG_FILE="${FONTCONFIG_FILE:-${POPPLER_ROOT}/etc/fonts/fonts.conf}"
  fi
fi

: "${RUN_ROOT:?set RUN_ROOT to the immutable Nova Gate run root}"
: "${QUERIES:?set QUERIES to a JSONL file}"

NOVA_PORT="${NOVA_PORT:-18017}"
NOVA_AGENT_MODEL="${NOVA_AGENT_MODEL:-claude-opus-4-7-thinking}"
: "${MODEL_KEYS_ENV:?set MODEL_KEYS_ENV to the model credential env file}"
[[ -r "${MODEL_KEYS_ENV}" ]] || { echo "MODEL_KEYS_ENV is not readable" >&2; exit 2; }

set -a
# shellcheck disable=SC1090
source "${MODEL_KEYS_ENV}"
set +a

# Search credentials are intentionally loaded separately from model/image
# credentials.  Only the two Serper variables are imported, so values such as
# IMAGE_MODEL in the pipeline .env cannot silently override the frozen rollout
# configuration.  A bulk rollout requires real Serper search; it must never
# degrade unnoticed to the public Wikipedia/Wikimedia fallback.
SEARCH_KEYS_ENV="${SEARCH_KEYS_ENV:-${PIPELINE_ROOT}/.env}"
[[ -r "${SEARCH_KEYS_ENV}" ]] || {
  echo "SEARCH_KEYS_ENV is not readable: ${SEARCH_KEYS_ENV}" >&2
  exit 2
}
while IFS= read -r -d '' env_name; do
  IFS= read -r -d '' env_value || {
    echo "Malformed search credential stream from ${SEARCH_KEYS_ENV}" >&2
    exit 2
  }
  if [[ -z "${!env_name:-}" ]]; then
    printf -v "${env_name}" '%s' "${env_value}"
    export "${env_name}"
  fi
done < <("${CLEAN_PYTHON}" - "${SEARCH_KEYS_ENV}" <<'PY'
import sys
from pathlib import Path

values = {}
for raw in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    name, value = line.split("=", 1)
    values[name.strip()] = value.strip().strip('"').strip("'")
for name in ("SERPER_API_KEY", "SERPER_BASE_URL"):
    value = values.get(name, "")
    if value:
        sys.stdout.write(name + "\0" + value + "\0")
PY
)
: "${SERPER_API_KEY:?SERPER_API_KEY is required for this rollout}"
export SERPER_BASE_URL="${SERPER_BASE_URL:-https://google.serper.dev}"
export CLEAN_REQUIRE_SEARCH=1

proxy_origin="http://127.0.0.1:${NOVA_PORT}"
curl -fsS "${proxy_origin}/health" >/dev/null || {
  echo "Nova Gate proxy is not healthy at ${proxy_origin}" >&2
  exit 3
}

export CLEAN_DOTENV_PATH=/dev/null
export CLEAN_NOVA_RAW_V2=1
export CLEAN_NOVA_GATE_V1=1
export CLEAN_NOVA_GATE_AGENT_MODEL="${NOVA_AGENT_MODEL}"
export CLEAN_NOVA_GATE_THINKING_GATE="${NOVA_THINKING_GATE:-off}"
export CLEAN_NOVA_GATE_VISION_MAX_TOKENS="${NOVA_VISION_MAX_TOKENS:-8192}"
export CLEAN_NOVA_GATE_VISION_HARD_MAX_TOKENS="${NOVA_VISION_HARD_MAX_TOKENS:-8192}"
export CLEAN_NOVA_RAW_ROOT="${RUN_ROOT}/raw"
export NOVA_RUN_ID="${NOVA_RUN_ID:-$(basename "${RUN_ROOT}")}" 
export NOVA_PROXY_BASE_URL="${proxy_origin}"
export NOVA_VISION_PROXY_BASE_URL="${proxy_origin}"
export ANTHROPIC_BASE_URL="${proxy_origin}"
export VISION_BACKEND=nova
export ANTHROPIC_API_KEY=nova-local-no-inbound-auth
export ANTHROPIC_MODEL="${NOVA_AGENT_MODEL}"
export CLEAN_THINKING=1
export CLEAN_EFFORT=high
export CLEAN_MAX_TOKENS=65536
export CLEAN_MODEL_TIMEOUT=86400
export CLEAN_FIRST_RESPONSE_TIMEOUT=86400
export CLEAN_ACTIVE_RESPONSE_TIMEOUT=86400
export CLEAN_PROMPT_CACHE="${CLEAN_PROMPT_CACHE:-1}"
export CLEAN_PROMPT_CACHE_MESSAGES="${CLEAN_PROMPT_CACHE_MESSAGES:-1}"
export CLEAN_CHILD_CONCURRENCY="${CLEAN_CHILD_CONCURRENCY:-4}"
export CLEAN_CHILD_POOL_MAX_WORKERS="${CLEAN_CHILD_POOL_MAX_WORKERS:-4}"
export CLEAN_REMOTE_TOOL_CONCURRENCY="${CLEAN_REMOTE_TOOL_CONCURRENCY:-4}"
export VISION_NOVA_TIMEOUT=86400
export VISION_NOVA_MAX_TOKENS=65536
export CLEAN_ARTIFACT_ROOT="${RUN_ROOT}/clean"
export CLEAN_RUNS_DIR="${RUN_ROOT}/clean/runs"
export CLEAN_LOGS_DIR="${RUN_ROOT}/clean/logs"
export CLEAN_SKILLS_DIR="${PIPELINE_ROOT}/skills"
export CLEAN_SKILL_NAME=ppt-skill-html-clean
export CLEAN_SKILL_NAME_ZH=ppt-skill-html-clean-zh
export CLEAN_SKILL_NAME_EN=ppt-skill-html-clean-en
export OPENAI_BASE_URL="${TOKENHUB_BASE_URL%/}/v1"
export OPENAI_API_KEY="${TOKENHUB_IMAGE_KEY}"
export IMAGE_MODEL="${TOKENHUB_IMAGE_MODEL:-gpt-image-2-adobe}"
export ENABLE_IMAGE_GEN=1
export IMAGE_GENERATE_TIMEOUT="${IMAGE_GENERATE_TIMEOUT:-900}"
export IMAGE_DOWNLOAD_TIMEOUT="${IMAGE_DOWNLOAD_TIMEOUT:-300}"

if [[ "${CLEAN_ENV_PREFLIGHT_ONLY:-0}" == "1" ]]; then
  report="${CLEAN_ENV_PREFLIGHT_REPORT:-${RUN_ROOT}/runtime/clean-environment-preflight.json}"
  exec "${CLEAN_PYTHON}" "${PIPELINE_ROOT}/scripts/clean_environment_preflight.py" \
    --output "${report}"
fi

infer_args=(
  --queries "${QUERIES}"
  --batch "${BATCH:-clean-smoke-3-opus47t-v1}"
  --workers "${WORKERS:-4}"
  --max-attempts "${CLEAN_MAX_ATTEMPTS:-3}"
)
if [[ "${CLEAN_RESUME:-0}" == "1" ]]; then
  infer_args+=(--resume)
fi

exec "${CLEAN_PYTHON}" "${PIPELINE_ROOT}/infer.py" "${infer_args[@]}"
