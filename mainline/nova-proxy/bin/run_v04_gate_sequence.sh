#!/usr/bin/env bash
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MURAL_ROOT="$(cd "${HERE}/../../.." && pwd)"

: "${API_KEYS_FILE:?set API_KEYS_FILE to the controlled Nova YAML file}"
: "${IMAGE_API_KEY:?set IMAGE_API_KEY}"
: "${IMAGE_BASE_URL:?set IMAGE_BASE_URL}"
: "${IMAGE_MODEL:?set IMAGE_MODEL}"

NOVA_AGENT_MODEL="${NOVA_AGENT_MODEL:-claude-opus-4-7-thinking}"
: "${POPPLER_ROOT:?set POPPLER_ROOT to a real run-local Poppler installation}"
ACTIVE_RUN_ROOT=""
ACTIVE_PORT=""

cleanup_active_proxy() {
  if [[ -n "${ACTIVE_RUN_ROOT}" && -n "${ACTIVE_PORT}" ]]; then
    RUN_ROOT="${ACTIVE_RUN_ROOT}" NOVA_PORT="${ACTIVE_PORT}" \
      "${HERE}/start_proxy.sh" stop >/dev/null 2>&1 || true
  fi
}
trap cleanup_active_proxy EXIT

wait_for_proxy() {
  local port="$1"
  local attempt
  for attempt in $(seq 1 60); do
    if CLEAN_NOVA_GATE_AGENT_MODEL="${NOVA_AGENT_MODEL}" \
      NOVA_PROXY_BASE_URL="http://127.0.0.1:${port}/v1" \
      "${HERE}/preflight.py" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "Nova Proxy on port ${port} did not pass health preflight" >&2
  return 1
}

run_v04_case() {
  local run_root="$1"
  local port="$2"
  local batch="$3"
  local query="$4"

  RUN_ROOT="${run_root}" "${HERE}/prepare_gate_runtime.sh" >/dev/null
  RUN_ROOT="${run_root}" \
  NOVA_PORT="${port}" \
  NOVA_AGENT_MODEL="${NOVA_AGENT_MODEL}" \
  API_KEYS_FILE="${API_KEYS_FILE}" \
  POPPLER_ROOT="${POPPLER_ROOT}" \
    "${HERE}/start_proxy.sh" start
  ACTIVE_RUN_ROOT="${run_root}"
  ACTIVE_PORT="${port}"
  wait_for_proxy "${port}"

  RUN_ROOT="${run_root}" \
  NOVA_PORT="${port}" \
  NOVA_AGENT_MODEL="${NOVA_AGENT_MODEL}" \
  MURAL_VERSION=0.4 \
  ENABLE_IMAGE_GEN=1 \
  IMAGE_API_KEY="${IMAGE_API_KEY}" \
  IMAGE_BASE_URL="${IMAGE_BASE_URL}" \
  IMAGE_MODEL="${IMAGE_MODEL}" \
  SMOKE_BATCH="${batch}" \
  SMOKE_QUERY="${query}" \
    "${HERE}/smoke.sh"

  RUN_ROOT="${run_root}" "${HERE}/validate_run.sh"
  cleanup_active_proxy
  ACTIVE_RUN_ROOT=""
  ACTIVE_PORT=""
}

: "${GATE_OUTPUT_ROOT:?set GATE_OUTPUT_ROOT to a writable smoke-output directory}"
cover_root="${GATE_OUTPUT_ROOT}/v04-cover"
cover_query="为高校开源社团制作一张中文活动招新 PPT 封面：像素 RPG Game Start 启动页，包含真实开源项目氛围和明确报名行动。必须生成至少一张像素风图片作为真实页面素材，并在交付前通过 vision_analyze 检查最终渲染的可读性与版式。只做 1 页。"

echo "Starting MURAL v0.4 one-page cover Gate smoke"
run_v04_case \
  "${cover_root}" 18017 \
  "nova-gate-v11-v04-cover-opus47t-20260815" \
  "${cover_query}"

deck_root="${GATE_OUTPUT_ROOT}/v04-8p"
deck_query="为高校学生制作一份 8 页中文开源软件讲座 PPT。面向不喜欢说教、但每天都在使用开源软件的本科生：用真实常见应用开场，重点讲清 MIT、Apache-2.0 与 GPL 的差别，结合真实商业冲突或合规案例，最后给出新手使用、修改、分发和商业化的防坑清单。整体采用 8-bit 复古像素 RPG 风格，深色 CRT 扫描线背景与高亮青、电光黄撞色；封面做成 Game Start，许可证核心页使用 RPG 技能树或阵营矩阵，避免默认图表和满屏文字。必须使用真实图片素材，并在交付前逐页进行 vision_analyze 和最终 Review。"

echo "Starting MURAL v0.4 eight-page Gate smoke"
run_v04_case \
  "${deck_root}" 18018 \
  "nova-gate-v11-v04-8p-opus47t-20260815" \
  "${deck_query}"

echo "All Nova Gate comparison stages passed"
