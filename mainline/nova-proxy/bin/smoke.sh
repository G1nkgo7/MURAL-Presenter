#!/usr/bin/env bash
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${RUN_ROOT:?set RUN_ROOT to a dedicated absolute run directory}"
batch="${SMOKE_BATCH:-nova-gate-v1-smoke}"
query="${SMOKE_QUERY:-为高校开源社团制作一份 3 页中文活动预告演示：封面、真实开源项目亮点、报名行动页。使用深色像素 RPG 视觉，必须生成或检索至少一张相关图片，并在交付前通过 vision_analyze 检查最终渲染的可读性与版式。}"

exec "${HERE}/run_mural.sh" \
  --query "${query}" \
  --batch "${batch}" \
  --workers 1 \
  --mode synthesis

