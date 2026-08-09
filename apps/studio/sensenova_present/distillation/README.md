# Distillation and generation adapter

This directory contains the existing SenseNova Present process adapter used by Studio. It builds a
generation job, selects an Anthropic- or OpenAI-compatible backend, records the agent trajectory, and
hands artifacts back to Studio.

It is not a self-contained public model release. Skills, Harnesses, model services, image generation,
search providers, and their credentials are deployment inputs.

## Setup

```bash
cp .env.example .env
uv sync --frozen
```

Use the parent bundle launcher for normal product operation. For a direct student-model smoke run:

```bash
STUDENT_BASE_URL=https://model.example/v1 \
STUDENT_MODEL=my-model \
uv run python run_student.py --query "Create a concise three-slide product brief" --lang en
```

Never commit `.env`, generated runs, JSONL production data, or real service URLs. The adapter's
checked-in defaults are deliberately endpoint-free.
