# MURAL Presenter v0.2 Harness

This Harness exposes two ownership variants, each with behaviorally equivalent
Chinese and English instruction editions:

- Single: `mural-presenter-v0-2-zh` / `mural-presenter-v0-2-en`
- Grouped: `mural-presenter-v0-2-grouped-zh` / `mural-presenter-v0-2-grouped-en`

By default `CLEAN_MODEL_SELECT_SKILL=1`: both paths are exposed to the
Orchestrator, which selects one by its first `SKILL.md` read and then locks that
edition for the Deck. The selected instruction language does not determine the
presentation's delivery language.

Both variants use the same v0.2 Research, planning, Image, Review, revision,
tool, and delivery contracts. The only intentional production difference is
ownership topology: Single delegates one `Slide NN:` per page, while Grouped
delegates adjacent 2–4-page `SlideGroup GROUP [NN,NN]:` responsibility units.
This keeps inference prompts close to the v0.2 trajectory distribution while
making grouping a controlled variable. v0.2 also adds deterministic PDF
text/page extraction, optional OCR for scanned pages, correct image-service
routing, and a recoverable visual-repair ceiling. Document pixels are evidence
only: Image cannot inspect/crop `inputs/`, `material-figure` is blocked, and the
delivery catalog accepts only reacquired `real` or newly `generated` raster
assets. Verified data and relationships are reconstructed as code visuals.
The Harness does not introduce additional model-authored ledgers or contracts.

## Install

The standalone Harness uses an Anthropic Messages-compatible main-model endpoint.
Create a local environment and install Chromium once:

```bash
cd harnesses/mural-presenter-v0.2
cp .env.example .env
uv venv
uv pip install -r requirements.txt
uv run --no-project playwright install chromium
```

At minimum, set these values in `.env`:

```dotenv
ANTHROPIC_API_KEY=...
ANTHROPIC_BASE_URL=https://your-anthropic-compatible-endpoint
ANTHROPIC_MODEL=your-deployment-model
```

This Harness intentionally loads its `.env` over inherited shell variables so a
global model setting cannot silently change a run. Set `CLEAN_DOTENV_PATH` to use
another environment file. Never commit credentials.

## Run one deck

Run a standalone job with:

```bash
uv run --no-project python infer.py \
  --query "制作一份 8 页演示" \
  --batch demo-v02 --workers 1 --mode inference
```

`--batch` names the output namespace. `--workers 1` runs one top-level Deck at a
time; the page-level child wave still uses `CLEAN_CHILD_CONCURRENCY` (default 12).

## Run JSONL batches and attachments

Each non-empty JSONL line must contain `query`; `qid` is recommended because it
becomes part of the stable sample ID:

```json
{"qid":"rave","query":"Create a 16-slide conference talk","lang":"en","slide_count":16,"materials":["/absolute/path/to/rave.pdf"]}
{"qid":"policy","query":"制作一份政策解读","lang":"zh","attachments":[{"path":"/absolute/path/to/policy.pdf"}]}
```

Run it with:

```bash
uv run --no-project python infer.py \
  --queries /absolute/path/to/briefs.jsonl \
  --batch bench-v02 --workers 4
```

`materials` and `attachments` are aliases. Paths must exist on the worker and
may be strings or `{ "path": "..." }` objects. The Harness copies inputs into
the sample workspace, extracts complete PDF text and page derivatives, and uses
OCR when the optional OCR runtime is available. v0.2 does not place attachment
pixels in the deck: OCR/text grounds facts and visual replacement briefs, while
final imagery is reacquired or generated and verified data/relationships are
redrawn as code visuals.

Useful batch flags:

- `--limit N`: run only the first N rows;
- `--resume`: skip completed samples and retry unfinished samples without
  replacing complete recovered workspaces;
- `--max-attempts N`: cap total attempts recorded for one sample (default 3);
- `--overwrite`: delete this batch's mutable run directory and manifest before
  starting. This is destructive and does not combine with `--resume`.

## Inference and synthesis modes

v0.2 uses the same explicit dual-mode contract as `../mural-presenter`:

- `--mode inference` is the serving default. It releases image bytes after the
  model has consumed them once and compacts sufficiently old active history.
  Immutable image snapshots and the full text trace remain on disk.
- `--mode synthesis` disables both forms of lossy active-context maintenance.
  Every Orchestrator and child trace gets a hash-addressed
  `multimodal-manifest.json`; the sample is rejected if any referenced snapshot
  is missing.

Both modes run the selected Single or Grouped Skill with the same prompts,
tools, model settings, and delivery acceptance gates. They differ only in runtime
context retention and trajectory completeness. In particular, synthesis does
not use shorter prompts, fewer turns, smaller token limits, or compressed model
responses.

The CLI flag takes precedence over `MURAL_RUN_MODE`; the default is
`inference`. Use distinct batch names because a batch namespace has one fixed
trace policy:

```bash
# Fast delivery
uv run --no-project python infer.py \
  --query "制作一份 8 页演示" \
  --batch demo-v02-infer --workers 1 --mode inference

# Lossless training trajectory synthesis
uv run --no-project python infer.py \
  --queries /absolute/path/to/briefs.jsonl \
  --batch train-v02-synthesis --workers 4 --mode synthesis
```

## Skill and language routing

Set `CLEAN_FORCE_SKILL_LANGUAGE=zh` or `en` only for an explicit fixed-language
Harness path. WebUI jobs leave it unset so the Agent routes itself.

With the default `CLEAN_MODEL_SELECT_SKILL=1`, the Orchestrator sees both v0.2
instruction editions and locks whichever `SKILL.md` it reads first. This choice
does not set the deck language. `lang` wins when supplied; otherwise the Harness
infers delivery language from the query.

The default paths are:

```text
skills/mural-presenter-v0.2/mural-presenter-v0-2-zh
skills/mural-presenter-v0.2/mural-presenter-v0-2-en
harnesses/mural-presenter-v0.2/infer.py
```

For grouped ownership, set:

```dotenv
CLEAN_SKILL_NAME_ZH=mural-presenter-v0-2-grouped-zh
CLEAN_SKILL_NAME_EN=mural-presenter-v0-2-grouped-en
```

Override packaged deployments with `CLEAN_SKILLS_DIR`,
`CLEAN_SKILL_NAME_ZH`, and `CLEAN_SKILL_NAME_EN`. Do not mix one language edition
from Single with the other language edition from Grouped in the same runtime.

## Images, search, and Vision

Image generation is enabled only when credentials are present:

```dotenv
ENABLE_IMAGE_GEN=1
IMAGE_API_KEY=...
IMAGE_BASE_URL=https://your-openai-compatible-image-endpoint/v1
IMAGE_MODEL=gpt-image-2
SERPER_API_KEY=...                       # optional web/image search
```

Before the first model request, the Harness writes
`_trace/runtime-capabilities.json` and injects the same immutable capability
overlay into the Orchestrator. Optional stages are removed rather than merely
described as unavailable:

- no attachments: omit Material;
- no `SERPER_API_KEY`: omit Research and both web tools (there is no hidden
  Wikipedia/Wikimedia fallback);
- no image-generation credential: omit `image_generate`;
- neither search nor image generation: omit Image and require `code_only/none`
  visual plans.

The profile also checks the renderer, portable-font sources and real font
weight range, Python dependencies, Office/OCR helpers, and model configuration.
Missing optional services produce a degraded profile; missing final-delivery
prerequisites stop before model tokens are spent. The trace records booleans and
paths only, never credential values.

`OPENAI_API_KEY`/`OPENAI_BASE_URL` are accepted as image-service fallbacks.
When the main model natively accepts image inputs, keep
`VISION_BACKEND=native`. For a text-only main model, route screenshot inspection
to a separate OpenAI-compatible vision endpoint:

```dotenv
VISION_BACKEND=gemini
GEMINI_API_KEY=...
VISION_GEMINI_BASE_URL=https://your-vision-endpoint/v1
VISION_GEMINI_MODEL=gemini-3.5-flash
VISION_GEMINI_TIMEOUT=120
VISION_GEMINI_MAX_TOKENS=8000
```

Nova exact-raw capture is an advanced dual-proxy mode; follow the commented
`CLEAN_NOVA_RAW_V2` block in `.env.example`. It rejects startup unless the main
and auxiliary Vision proxy capabilities are separated correctly.

## Thinking and runtime limits

Default runtime limits are 12 parallel child Agents, 240/80 main/child turns,
40,960 output tokens per request, 600 seconds per model request, and 2,400
seconds of child wall time. All remain environment-overridable.

The main controls are:

```dotenv
CLEAN_THINKING=1
CLEAN_EFFORT=high
CLEAN_MAX_TOKENS=40960
CLEAN_MAX_TURNS=240
CLEAN_CHILD_MAX_TURNS=80
CLEAN_CHILD_CONCURRENCY=12
CLEAN_REMOTE_TOOL_CONCURRENCY=4
CLEAN_MODEL_TIMEOUT=600
CLEAN_CHILD_WALL_TIMEOUT=2400
```

`--workers` controls top-level Deck concurrency. `CLEAN_CHILD_CONCURRENCY`
controls simultaneous child Agents inside one delegation wave, and
`CLEAN_REMOTE_TOOL_CONCURRENCY` controls independent image/search/fetch tool
calls. They are separate limits.

## Outputs

By default the Harness writes:

```text
runs/<batch>/<sample_id>/
├── research/                 # material and knowledge briefs
├── plan/                     # deck and per-slide plans
├── slides/                   # editable page HTML
├── assets/                   # verified local imagery and catalog
├── renders/                  # page PNGs and contact sheets
├── _trace/                   # orchestrator/child messages and tool events
│   └── **/multimodal-manifest.json  # synthesis mode only
├── present.html
├── speech.md
└── result.json

logs/<batch>.manifest.jsonl   # append-only batch outcomes
```

Set `CLEAN_ARTIFACT_ROOT` to move both `runs/` and `logs/`, or set
`CLEAN_RUNS_DIR` and `CLEAN_LOGS_DIR` independently. A batch name with existing
outputs requires `--resume`, `--overwrite`, or a new name; the Harness refuses
an ambiguous accidental overwrite.
