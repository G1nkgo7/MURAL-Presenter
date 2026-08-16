# Local PresentBench runbook

This checkout tracks the official repository at commit
`e70ff01da962274e1e3cc03f77ec435ad66c5eb6`.

The benchmark dataset is pinned to Hugging Face revision
`31ec40084405c5f2e6b8b5adedf2999b6060e7e1` and lives in `data/`.

## Runtime

Use a Python 3.11 environment with the benchmark requirements installed:

```text
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Set it once for a shell session:

```bash
PB_PY="$PWD/.venv/bin/python"
```

## Smoke tests

Run the official unit tests:

```bash
"$PB_PY" -m unittest discover -s tests -v
```

Run the offline full-path smoke. This uses a real benchmark case and exercises
PDF parsing, checklist assembly, checkpoint/result output and weighted scoring;
only the remote model response is replaced by a deterministic local adapter:

```bash
"$PB_PY" local/offline_smoke.py
```

## Real judge smoke

Configure the judge without committing credentials:

```bash
export GENAI_API_KEY='...'
export GENAI_BASE_URL='...'  # omit when using Google's default endpoint
```

Place generated decks beneath a result root that mirrors `data/`:

```text
<RESULT_ROOT>/<domain>/<subcategory>/<case>/generation_task/results/slides.pdf
```

Then run one stratified case with one worker:

```bash
"$PB_PY" judge_all.py \
  --agent_name MURAL \
  --data_root "$PWD/data" \
  --result_root /absolute/path/to/mural-presentbench-results \
  --api_type gemini_inline \
  --model gemini-3-flash-preview \
  --limit 1 \
  --max_workers 1 \
  --retry 2 \
  --temperature 0
```

Remove `--limit 1` only after the real-judge smoke succeeds. Start the full run
with conservative concurrency, then raise it after confirming API quota and PDF
upload behavior.
