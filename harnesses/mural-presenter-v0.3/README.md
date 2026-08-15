# MURAL Presenter v0.3 Harness

v0.3 exposes one bilingual Skill: `mural-presenter-v0-3`. The presentation
language follows the user query. After evidence collection, the Orchestrator
chooses exactly one page-ownership topology in `plan/deck.md`:

- `single`: one structured `{role: slide, pages: [NN]}` task per independent page;
- `grouped`: one structured `{role: slide, group_id: GROUP, pages: [NN,NN]}` task per contiguous 2–4-page
  dependency group.

The choice is based on cross-page responsibility dependencies, not deck length
or available concurrency. A deck cannot mix both forms. The Harness validates
the choice before delegation and preserves it during revision.

## Install and run

```bash
cd harnesses/mural-presenter-v0.3
cp .env.example .env
uv venv
uv pip install -r requirements.txt
uv run --no-project playwright install chromium

uv run --no-project python infer.py \
  --query "制作一份 8 页演示" \
  --batch demo-v03 --workers 1 --mode inference
```

At minimum configure the Anthropic Messages-compatible main endpoint:

```dotenv
ANTHROPIC_API_KEY=...
ANTHROPIC_BASE_URL=https://your-endpoint
ANTHROPIC_MODEL=your-multimodal-model
```

The only packaged Skill path is:

```text
skills/mural-presenter-v0.3/mural-presenter-v0-3
```

`CLEAN_MODEL_SELECT_SKILL=0` is the default. `CLEAN_SKILL_NAME`,
`CLEAN_SKILL_NAME_ZH`, and `CLEAN_SKILL_NAME_EN` all resolve to
`mural-presenter-v0-3`; the aliases exist only for shared Harness compatibility.

## Runtime capability contract

Before model work, the Harness checks the renderer, fonts, model endpoint,
attachment parsers and optional services. It writes
`_trace/runtime-capabilities.json` and injects a key-free capability contract:

- no attachments: omit Material;
- no search key: omit Research and web tools;
- no image credential: omit image generation;
- neither search nor generation: omit Image and plan code-only/typographic
  visuals;
- missing required renderer/font/model dependencies: stop before spending
  model tokens.

Optional services:

```dotenv
SERPER_API_KEY=...
ENABLE_IMAGE_GEN=1
IMAGE_API_KEY=...
IMAGE_BASE_URL=https://your-image-endpoint/v1
IMAGE_MODEL=gpt-image-2
```

Vision uses a fresh, history-free request to the selected multimodal main model
by default. The acting Agent receives structured text, never image tokens:

```dotenv
VISION_BACKEND=same_model_aux
```

Set `VISION_BACKEND=external_model` plus `VISION_CRITIC_MODEL`,
`VISION_CRITIC_BASE_URL`, and `VISION_CRITIC_API_KEY` to use a separate visual
critic. The legacy `gemini` value maps to this route. `nova` remains the exact-raw
auxiliary route for synthesis runs.

## Attachments

`materials` and `attachments` accept strings or `{ "path": "...", "intent":
"..." }` objects. Harness writes `_trace/attachment-manifest.json`. Direct text
is handed off without Material; user photos/illustrations go directly to Image;
style references are inspected once by Orchestrator; screenshots, scans and
content-extraction images go to Material. `mixed` requests content extraction
and visual reuse in parallel. PDF and supported Office evidence remains owned by
Material, and Research consumes the canonical material brief instead of reopening it.

An independent Figure, photo or illustration may be reused through the audited
`material-figure` command. It copies a bounded region into `assets/` and rejects
page facsimiles, edge-heavy crops, low resolution and text-heavy regions. The
result is catalogued as `kind: material`. Slides never reference `inputs/**`
directly. Unsupported or unsafe visuals are reacquired, regenerated as a clearly
conceptual image, or reconstructed from verified data in HTML/CSS/SVG.

## Inference and synthesis

- `--mode inference` releases consumed image bytes and compacts stale active
  context while keeping durable traces and image snapshots.
- `--mode synthesis` disables lossy active-context maintenance and requires a
  complete hash-addressed multimodal manifest.

Both modes run the same Skill, models, tools and quality gates. Use different
batch names because a batch namespace has one trace policy.

```bash
uv run --no-project python infer.py \
  --queries /absolute/path/to/briefs.jsonl \
  --batch train-v03 --workers 4 --mode synthesis
```

## Concurrency and quality limits

Defaults are 12 child Agents, 4 concurrent remote tool calls, 240/80 main/child
turns, 40,960 output tokens, 600 seconds per model request and 2,400 seconds per
child. `--workers` controls top-level decks; it is independent of child and tool
parallelism.

```dotenv
CLEAN_CHILD_CONCURRENCY=12
CLEAN_CHILD_POOL_MAX_WORKERS=12
CLEAN_REMOTE_TOOL_CONCURRENCY=4
CLEAN_MAX_TURNS=240
CLEAN_CHILD_MAX_TURNS=80
CLEAN_MAX_TOKENS=40960
CLEAN_MODEL_TIMEOUT=600
CLEAN_CHILD_WALL_TIMEOUT=2400
```

Slide performs a first-pixel inspection and one merged repair inspection. A
remaining known defect returns `repair_required`; Review must open that page and
attempt a bounded repair. A remaining, pixel-inspected visual imperfection can
end as `needs_improvement` without converting an otherwise usable deck into a
hard failure; its original blocking severity remains in the Review trace.

## Outputs

```text
runs/<batch>/<sample_id>/
├── research/
├── plan/
├── slides/
├── assets/
├── renders/
├── _trace/
├── present.html
├── speech.md
└── result.json
```

Use `--resume` for unfinished samples, `--max-attempts N` for a bounded retry
count, or `--overwrite` only when intentionally replacing that batch's mutable
run directory.
