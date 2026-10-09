#!/usr/bin/env bash
set -euo pipefail
: "${MURAL_MINERU_SERVICE_ROOT:?Set an absolute directory for your MinerU GPU service}"
SERVICE_ROOT="$MURAL_MINERU_SERVICE_ROOT"
SERVICE_ENV="${MURAL_MINERU_SERVICE_ENV:-$SERVICE_ROOT/env}"
DEPLOY_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$SERVICE_ROOT"/{logs,tmp,cache,models,output,evidence}
mkdir -p "$SERVICE_ROOT/cache/pip"
exec >> "$SERVICE_ROOT/logs/service.log" 2>&1
export TMPDIR="$SERVICE_ROOT/tmp"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1
export PIP_CACHE_DIR="$SERVICE_ROOT/cache/pip"
export HF_HOME="$SERVICE_ROOT/cache/huggingface"
export MODELSCOPE_CACHE="$SERVICE_ROOT/cache/modelscope"
export XDG_CACHE_HOME="$SERVICE_ROOT/cache"
export MINERU_TOOLS_CONFIG_JSON="$SERVICE_ROOT/mineru.json"
export MINERU_API_OUTPUT_ROOT="$SERVICE_ROOT/output"
export MINERU_API_ENABLE_FASTAPI_DOCS=0
export MINERU_API_TASK_RETENTION_SECONDS=3600
export HF_HUB_DISABLE_TELEMETRY=1
export DO_NOT_TRACK=1
export CUDA_VISIBLE_DEVICES=0

date -u
nvidia-smi --query-gpu=name,uuid,memory.total --format=csv
# 服务环境只继承固定容器镜像的 CUDA 栈，不读取或修改 WebUI 环境。
if [ ! -x "$SERVICE_ENV/bin/python" ]; then
  python3 -m venv --system-site-packages "$SERVICE_ENV"
fi
if [ ! -f "$SERVICE_ENV/.installation-complete.json" ]; then
  constraints=()
  if [ -f "$SERVICE_ROOT/evidence/dependencies.local.freeze.txt" ]; then
    constraints=(-c "$SERVICE_ROOT/evidence/dependencies.local.freeze.txt")
  fi
  "$SERVICE_ENV/bin/python" -m pip install --disable-pip-version-check \
    --no-index --find-links "$SERVICE_ROOT/cache/wheels" \
    --no-compile "${constraints[@]}" 'mineru[pipeline,vlm]==3.4.5' 'vllm==0.19.1'
  "$SERVICE_ENV/bin/python" -m pip freeze --all > "$SERVICE_ROOT/evidence/dependencies.freeze.txt"
  "$SERVICE_ENV/bin/python" -m pip freeze --local > "$SERVICE_ROOT/evidence/dependencies.local.freeze.txt"
fi
"$SERVICE_ENV/bin/python" "$DEPLOY_ROOT/prepare_models.py" "$SERVICE_ROOT"
export MINERU_MODEL_SOURCE=local
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
cd "$SERVICE_ROOT"
exec "$SERVICE_ENV/bin/python" "$DEPLOY_ROOT/service_runtime.py" gpu --host 0.0.0.0 --port 8200 \
  --enable-vlm-preload true
