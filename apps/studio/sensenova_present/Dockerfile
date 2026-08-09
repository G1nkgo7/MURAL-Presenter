FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    STUDIO_DATA_DIR=/data \
    SENSE_NOVA_LOCAL_HOST=0.0.0.0 \
    SENSE_NOVA_LOCAL_PORT=8001

WORKDIR /workspace
COPY apps/studio/sensenova_present /workspace/apps/studio/sensenova_present
COPY skills/mural-presenter /workspace/skills/mural-presenter
COPY harnesses/mural-presenter /workspace/harnesses/mural-presenter

RUN uv sync --project /workspace/apps/studio/sensenova_present/studio --frozen \
    && uv sync --project /workspace/harnesses/mural-presenter --frozen \
    && /workspace/harnesses/mural-presenter/.venv/bin/python -m playwright install --with-deps chromium

WORKDIR /workspace/apps/studio/sensenova_present

EXPOSE 8001
VOLUME ["/data"]

CMD ["python", "scripts/launch.py", "--host", "0.0.0.0", "--port", "8001", "--no-install", "--no-browser-install"]
