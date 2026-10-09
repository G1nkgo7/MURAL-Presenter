FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    STUDIO_DATA_DIR=/data \
    SENSE_NOVA_LOCAL_HOST=0.0.0.0 \
    SENSE_NOVA_LOCAL_PORT=8001

WORKDIR /workspace
COPY webui /workspace/webui
COPY skills/mural-presenter-v1 /workspace/skills/mural-presenter-v1
COPY harnesses/mural-presenter-v1 /workspace/harnesses/mural-presenter-v1
COPY configs /workspace/configs
COPY scripts /workspace/scripts
COPY tools /workspace/tools
COPY benchmark/PresentBench /workspace/benchmark/PresentBench
COPY fonts /workspace/fonts

RUN set -eu; \
    engine=/workspace/webui/inference; \
    uv sync --project /workspace/webui/studio --frozen; \
    uv sync --project "$engine" --frozen; \
    "$engine/.venv/bin/python" -m playwright install --with-deps chromium; \
    python /workspace/webui/scripts/launch.py --check --no-install --no-browser-install

WORKDIR /workspace/webui

EXPOSE 8001
VOLUME ["/data"]

CMD ["python", "scripts/launch.py", "--host", "0.0.0.0", "--port", "8001", "--no-install", "--no-browser-install"]
