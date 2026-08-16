#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  CASE_DIR=/path/to/case ./generate_ppt.sh <model-tag>
  CASE_DIR=/path/to/case ./generate_ppt.sh --all-envs

This benchmark wrapper uses one frozen MURAL Presenter bundle selected by
PRESENTER_ROOT.

The bundle supplies both the Skill and Harness. The Harness infers prompt and
delivery language from the exact case instruction; there is no separate Skill
edition selector.

Required environment variables:
  CASE_DIR          Input directory containing Instruction.md. Existing
                    lowercase instruction.md is also accepted. Other regular
                    files below the directory are passed as attachments;
                    benchmark control and runtime directories are excluded.

Optional environment variables:
  ENV_FILE          Model environment file.
                    Default: <this repository>/model_envs/<model-tag>.env
  MODEL_ENVS_DIR    Model environment directory used by --all-envs.
                    Default: <this repository>/model_envs
  PYTHON_BIN        Python environment used by the Harness and rendering.
                    Default: python3 from PATH
  PRESENTER_ROOT    Frozen MURAL Presenter bundle containing the Skill,
                    Harness, FROZEN.json, and FILES.sha256. Required.
  PRESENTER_RUNTIME_ROOT
                    Harness scheduler root. Live Deck paths are exposed at
                    <OUTPUT_ROOT>/<model-tag>/<RUN_ID> from task start.
                    Default: <OUTPUT_ROOT>/.presenter-runtime
  OUTPUT_ROOT       Destination root. Default: <CASE_DIR>/outputs
  RUN_ID            Output directory name. Default: UTC timestamp plus PID
  PRESENTER_MAX_ATTEMPTS
                    Whole-deck attempts through --max-attempts/--resume.
                    Range: 1..5. Default: 1
  PRESENTER_ATTEMPT_TIMEOUT_SECONDS
                    Wall-clock limit for one Harness invocation. Default: 10800
  GENERATION_TIMEOUT_SECONDS
                    Total wall-clock budget across all attempts.
                    Default: PRESENTER_ATTEMPT_TIMEOUT_SECONDS *
                    PRESENTER_MAX_ATTEMPTS + 300
  MAX_TURNS, MAX_TOKENS, SUBAGENT_MAX_TOKENS, THINKING, THINK_EFFORT,
  MAX_CONCURRENT_CHILDREN, SLIDE_CONCURRENCY, TURN_TOOL_PARALLEL,
  SLIDE_MAX_TURNS_BASE, SLIDE_MAX_TURNS_CAP, MAX_VISION_EDGE,
  RENDER_GLOBAL_LIMIT
                    Public MURAL Presenter runtime settings.
  ALL_ENVS_MAX_CONCURRENT
                    Maximum parallel model jobs in --all-envs mode.
                    Default: 6
  DRY_RUN           1 validates the frozen bundle and Harness CLI without a
                    model call.

Benchmark-side page retries, Skill/Harness patches, canvas repair, page
truncation, and page filling are unsupported. Limited export adaptation is
recorded separately from native generation success.
EOF
}

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PRESENTER_ROOT="${PRESENTER_ROOT:?set PRESENTER_ROOT to the frozen MURAL Presenter bundle}"
HARNESS_ROOT="$PRESENTER_ROOT/harnesses/long-horizon-presenter"
SKILLS_ROOT="$PRESENTER_ROOT/skills"
SKILL_ROOT="$SKILLS_ROOT/long-horizon-presenter"
PIPELINE_SCRIPT="$HARNESS_ROOT/distill_ppt.py"
PRESENTER_MANIFEST="$PRESENTER_ROOT/FROZEN.json"
PRESENTER_CHECKSUMS="$PRESENTER_ROOT/FILES.sha256"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
BENCHMARK_PYTHON="${BENCHMARK_PYTHON:-$PYTHON_BIN}"
MODEL_ENVS_DIR="${MODEL_ENVS_DIR:-$SCRIPT_DIR/model_envs}"
ARTIFACT_ADAPTER="${ARTIFACT_ADAPTER:-$SCRIPT_DIR/scripts/adapt_generation_artifacts.py}"
ARTIFACT_RENDERER="$SKILL_ROOT/scripts/render.py"
CASE_DIR="${CASE_DIR:-}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)_$$}"
DRY_RUN="${DRY_RUN:-0}"
PRESENTER_MAX_ATTEMPTS="${PRESENTER_MAX_ATTEMPTS:-1}"
PRESENTER_ATTEMPT_TIMEOUT_SECONDS="${PRESENTER_ATTEMPT_TIMEOUT_SECONDS:-10800}"
POLICY_MAX_TURNS="${MAX_TURNS:-200}"
POLICY_MAX_TOKENS="${MAX_TOKENS:-40960}"
POLICY_SUBAGENT_MAX_TOKENS="${SUBAGENT_MAX_TOKENS:-32768}"
POLICY_THINKING="${THINKING:-0}"
POLICY_THINK_EFFORT="${THINK_EFFORT:-high}"
POLICY_MAX_CONCURRENT_CHILDREN="${MAX_CONCURRENT_CHILDREN:-8}"
POLICY_SLIDE_CONCURRENCY="${SLIDE_CONCURRENCY:-$POLICY_MAX_CONCURRENT_CHILDREN}"
POLICY_TURN_TOOL_PARALLEL="${TURN_TOOL_PARALLEL:-8}"
POLICY_SLIDE_MAX_TURNS_BASE="${SLIDE_MAX_TURNS_BASE:-$POLICY_MAX_TURNS}"
POLICY_SLIDE_MAX_TURNS_CAP="${SLIDE_MAX_TURNS_CAP:-$POLICY_MAX_TURNS}"
POLICY_MAX_VISION_EDGE="${MAX_VISION_EDGE:-1600}"
POLICY_RENDER_GLOBAL_LIMIT="${RENDER_GLOBAL_LIMIT:-2}"

[[ $# -eq 1 ]] || {
  usage >&2
  exit 2
}
[[ -n "$CASE_DIR" ]] || fail "CASE_DIR must be set"
[[ "$DRY_RUN" == "0" || "$DRY_RUN" == "1" ]] || fail "DRY_RUN must be 0 or 1"
[[ "$PRESENTER_MAX_ATTEMPTS" =~ ^[1-5]$ ]] || \
  fail "PRESENTER_MAX_ATTEMPTS must be an integer from 1 through 5"
for value_name in \
    PRESENTER_ATTEMPT_TIMEOUT_SECONDS \
    POLICY_MAX_TURNS \
    POLICY_MAX_TOKENS \
    POLICY_SUBAGENT_MAX_TOKENS \
    POLICY_MAX_CONCURRENT_CHILDREN \
    POLICY_SLIDE_CONCURRENCY \
    POLICY_TURN_TOOL_PARALLEL \
    POLICY_SLIDE_MAX_TURNS_BASE \
    POLICY_SLIDE_MAX_TURNS_CAP \
    POLICY_MAX_VISION_EDGE; do
  value="${!value_name}"
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || \
    fail "${value_name#POLICY_} must be a positive integer"
done
[[ "$POLICY_THINKING" == "0" || "$POLICY_THINKING" == "1" ]] || \
  fail "THINKING must be 0 or 1"
[[ "$POLICY_RENDER_GLOBAL_LIMIT" =~ ^[0-9]+$ ]] || \
  fail "RENDER_GLOBAL_LIMIT must be a non-negative integer"
[[ -n "$POLICY_THINK_EFFORT" && "$POLICY_THINK_EFFORT" != *$'\n'* ]] || \
  fail "THINK_EFFORT must be a non-empty single-line value"
GENERATION_TIMEOUT_SECONDS="${GENERATION_TIMEOUT_SECONDS:-$((PRESENTER_ATTEMPT_TIMEOUT_SECONDS * PRESENTER_MAX_ATTEMPTS + 300))}"
[[ "$GENERATION_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || \
  fail "GENERATION_TIMEOUT_SECONDS must be a positive integer"
[[ "$GENERATION_TIMEOUT_SECONDS" -gt "$PRESENTER_ATTEMPT_TIMEOUT_SECONDS" ]] || \
  fail "GENERATION_TIMEOUT_SECONDS must exceed PRESENTER_ATTEMPT_TIMEOUT_SECONDS"
[[ "$RUN_ID" =~ ^[A-Za-z0-9._-]+$ ]] || \
  fail "RUN_ID may contain only letters, numbers, dot, underscore, and hyphen: $RUN_ID"

verify_presenter_bundle() {
  [[ -d "$PRESENTER_ROOT" ]] || fail "Long-Horizon Presenter bundle not found: $PRESENTER_ROOT"
  [[ -r "$PRESENTER_MANIFEST" ]] || fail "frozen manifest not readable: $PRESENTER_MANIFEST"
  [[ -r "$PRESENTER_CHECKSUMS" ]] || fail "checksum manifest not readable: $PRESENTER_CHECKSUMS"
  [[ -r "$PIPELINE_SCRIPT" ]] || fail "Harness entry not readable: $PIPELINE_SCRIPT"
  [[ -r "$SKILL_ROOT/SKILL.md" ]] || fail "Skill entry not readable: $SKILL_ROOT/SKILL.md"
  [[ -r "$ARTIFACT_RENDERER" ]] || fail "Skill renderer not readable: $ARTIFACT_RENDERER"
  (
    cd "$PRESENTER_ROOT"
    sha256sum -c --status FILES.sha256
  ) || fail "Long-Horizon Presenter bundle checksum verification failed"
}

verify_presenter_bundle
readarray -t PRESENTER_VERSION_VALUES < <(
  "$BENCHMARK_PYTHON" - "$PRESENTER_MANIFEST" <<'PY'
import json
import os
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
expected = {
    "name": "long-horizon-presenter",
    "skill": "skills/long-horizon-presenter",
    "harness": "harnesses/long-horizon-presenter",
    "entry": "harnesses/long-horizon-presenter/distill_ppt.py",
}
for key, value in expected.items():
    if manifest.get(key) != value:
        raise SystemExit(f"unexpected frozen manifest {key}: {manifest.get(key)!r}")
for key in ("version", "local_version", "skill_tree_sha256", "harness_tree_sha256"):
    value = manifest.get(key)
    if not isinstance(value, str) or not value:
        raise SystemExit(f"missing frozen manifest field: {key}")
    print(value)
PY
)
[[ ${#PRESENTER_VERSION_VALUES[@]} -eq 4 ]] || fail "invalid frozen presenter manifest"
PRESENTER_VERSION="${PRESENTER_VERSION_VALUES[0]}"
PRESENTER_LOCAL_VERSION="${PRESENTER_VERSION_VALUES[1]}"
SKILL_SOURCE_SHA256="${PRESENTER_VERSION_VALUES[2]}"
HARNESS_SOURCE_SHA256="${PRESENTER_VERSION_VALUES[3]}"
PRESENTER_SOURCE_SHA256="$(sha256sum "$PRESENTER_CHECKSUMS" | awk '{print $1}')"

CASE_DIR="$(realpath -m -- "$CASE_DIR")"
OUTPUT_ROOT="${OUTPUT_ROOT:-$CASE_DIR/outputs}"
if [[ -z "${PRESENTER_RUNTIME_ROOT+x}" ]]; then
  PRESENTER_RUNTIME_ROOT="$OUTPUT_ROOT/.presenter-runtime"
  for EXISTING_LEGACY_RUN in "$SCRIPT_DIR/.presenter-runtime-legacy"/*/"$RUN_ID"; do
    if [[ -d "$EXISTING_LEGACY_RUN" ]]; then
      PRESENTER_RUNTIME_ROOT="$SCRIPT_DIR/.presenter-runtime-legacy"
      break
    fi
  done
fi
GENERATION_WORK_ROOT="${GENERATION_WORK_ROOT:-$PRESENTER_RUNTIME_ROOT/work}"
ATTEMPT_ROOT="${ATTEMPT_ROOT:-$OUTPUT_ROOT/.generation-attempts}"
ATTEMPT_WRITER="${ATTEMPT_WRITER:-$SCRIPT_DIR/scripts/write_generation_attempt.py}"
METADATA_WRITER="${METADATA_WRITER:-$SCRIPT_DIR/scripts/write_generation_metadata.py}"

run_all_envs() {
  local log_dir="${ALL_ENVS_LOG_DIR:-$OUTPUT_ROOT/.all-envs/$RUN_ID}"
  local status_file="$log_dir/status.tsv"
  local max_concurrent="${ALL_ENVS_MAX_CONCURRENT:-6}"
  local env_file env_name model_tag safe_tag pid rc index
  local failed=0
  local -a env_files=()
  local -a model_tags=()
  local -a pids=()
  local -a active_indices=()
  local -A safe_tags=()

  [[ "$max_concurrent" =~ ^[0-9]+$ ]] || \
    fail "ALL_ENVS_MAX_CONCURRENT must be a non-negative integer"
  [[ -d "$MODEL_ENVS_DIR" ]] || fail "model env directory not found: $MODEL_ENVS_DIR"
  while IFS= read -r -d '' env_file; do
    env_files+=("$env_file")
  done < <(
    find "$MODEL_ENVS_DIR" -mindepth 1 -maxdepth 1 \
      \( -type f -o -type l \) -name '*.env' -print0 | sort -z
  )
  [[ ${#env_files[@]} -gt 0 ]] || fail "no model env files found under: $MODEL_ENVS_DIR"

  for env_file in "${env_files[@]}"; do
    [[ -r "$env_file" ]] || fail "model env not readable: $env_file"
    env_name="$(basename -- "$env_file")"
    model_tag="${env_name%.env}"
    [[ "$model_tag" =~ ^[A-Za-z0-9._-]+$ ]] || fail "invalid model env filename: $env_name"
    safe_tag="${model_tag//./_}"
    safe_tag="${safe_tag//-/_}"
    if [[ -n "${safe_tags[$safe_tag]+x}" ]]; then
      fail "model tags collide after normalization: ${safe_tags[$safe_tag]} and $model_tag"
    fi
    safe_tags[$safe_tag]="$model_tag"
    model_tags+=("$model_tag")
  done

  [[ ! -e "$log_dir" ]] || fail "all-envs log directory already exists: $log_dir"
  mkdir -p "$log_dir"
  printf 'model_tag\texit_code\tstate\tlog\n' >"$status_file"

  terminate_all_envs() {
    trap - INT TERM
    printf '\ninterrupt received; terminating %d model jobs\n' "${#pids[@]}" >&2
    for pid in "${pids[@]}"; do
      kill -TERM "$pid" 2>/dev/null || true
    done
    for pid in "${pids[@]}"; do
      wait "$pid" 2>/dev/null || true
    done
    exit 130
  }
  trap terminate_all_envs INT TERM

  record_model_result() {
    local wait_index="$1"
    local wait_rc="$2"
    local wait_model_tag="${model_tags[$wait_index]}"
    if [[ "$wait_rc" -eq 0 ]]; then
      printf '%s\t%d\tcompleted\t%s/%s.log\n' \
        "$wait_model_tag" "$wait_rc" "$log_dir" "$wait_model_tag" >>"$status_file"
    else
      failed=$((failed + 1))
      printf '%s\t%d\tfailed\t%s/%s.log\n' \
        "$wait_model_tag" "$wait_rc" "$log_dir" "$wait_model_tag" >>"$status_file"
    fi
  }

  wait_for_any_model() {
    local wait_pid="" wait_rc wait_index="" active_index
    local -a wait_pids=()
    local -a remaining_indices=()

    for active_index in "${active_indices[@]}"; do
      wait_pids+=("${pids[$active_index]}")
    done
    if wait -n -p wait_pid "${wait_pids[@]}"; then
      wait_rc=0
    else
      wait_rc=$?
    fi
    for active_index in "${active_indices[@]}"; do
      if [[ "${pids[$active_index]}" == "$wait_pid" ]]; then
        wait_index="$active_index"
      else
        remaining_indices+=("$active_index")
      fi
    done
    [[ -n "$wait_index" ]] || fail "completed model job was not tracked: pid=$wait_pid"
    active_indices=("${remaining_indices[@]}")
    record_model_result "$wait_index" "$wait_rc"
  }

  for index in "${!model_tags[@]}"; do
    model_tag="${model_tags[$index]}"
    env_file="${env_files[$index]}"
    (
      ENV_FILE="$env_file" RUN_ID="$RUN_ID" \
        PRESENTER_MAX_ATTEMPTS="$PRESENTER_MAX_ATTEMPTS" \
        "$SCRIPT_DIR/generate_ppt.sh" "$model_tag"
    ) >"$log_dir/$model_tag.log" 2>&1 &
    pid=$!
    pids+=("$pid")
    active_indices+=("$index")
    printf 'launched: model_tag=%s pid=%s log=%s/%s.log\n' \
      "$model_tag" "$pid" "$log_dir" "$model_tag"
    if [[ "$max_concurrent" -gt 0 && ${#active_indices[@]} -ge "$max_concurrent" ]]; then
      wait_for_any_model
    fi
  done
  while [[ ${#active_indices[@]} -gt 0 ]]; do
    wait_for_any_model
  done
  trap - INT TERM
  printf '\nall-envs finished: total=%d completed=%d failed=%d\n' \
    "${#model_tags[@]}" "$(( ${#model_tags[@]} - failed ))" "$failed"
  printf 'status: %s\n' "$status_file"
  [[ "$failed" -eq 0 ]]
}

if [[ "$1" == "--all-envs" ]]; then
  run_all_envs
  exit $?
fi

MODEL_TAG="$1"
[[ "$MODEL_TAG" =~ ^[A-Za-z0-9._-]+$ ]] || \
  fail "model-tag may contain only letters, numbers, dot, underscore, and hyphen: $MODEL_TAG"

ENV_FILE="${ENV_FILE:-$MODEL_ENVS_DIR/$MODEL_TAG.env}"
PIPELINE_ROOT="$PRESENTER_RUNTIME_ROOT/$MODEL_TAG/$RUN_ID"
PIPELINE_RUNS_DIR="$PIPELINE_ROOT/runs"
PIPELINE_LOGS_DIR="$PIPELINE_ROOT/logs"
DEST_DIR="$OUTPUT_ROOT/$MODEL_TAG/$RUN_ID"
ATTEMPT_STAGE="preflight"
ATTEMPT_STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
ATTEMPT_STARTED_EPOCH="$(date +%s)"
TMP_FILES=()

cleanup() {
  if [[ ${#TMP_FILES[@]} -gt 0 ]]; then
    rm -f -- "${TMP_FILES[@]}"
  fi
  if [[ "${LIVE_OUTPUT_LINK:-0}" == "1" && -L "$DEST_DIR" && ! -e "$DEST_DIR" ]]; then
    rm -f -- "$DEST_DIR"
  fi
}

record_attempt() {
  local rc="$?"
  local finished_at finished_epoch record_path
  trap - EXIT
  cleanup
  finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  finished_epoch="$(date +%s)"
  if record_path="$("$BENCHMARK_PYTHON" "$ATTEMPT_WRITER" \
      --attempt-root "$ATTEMPT_ROOT" \
      --case-dir "$CASE_DIR" \
      --model-tag "$MODEL_TAG" \
      --run-id "$RUN_ID" \
      --benchmark-attempt-limit 1 \
      --presenter-max-attempts "$PRESENTER_MAX_ATTEMPTS" \
      --stage "$ATTEMPT_STAGE" \
      --exit-code "$rc" \
      --started-at "$ATTEMPT_STARTED_AT" \
      --started-epoch "$ATTEMPT_STARTED_EPOCH" \
      --finished-at "$finished_at" \
      --finished-epoch "$finished_epoch" \
      --output-dir "$DEST_DIR" \
      --dry-run "$DRY_RUN")"; then
    printf 'attempt_record: %s\n' "$record_path"
  else
    printf 'warning: failed to write generation attempt record under %s\n' "$ATTEMPT_ROOT" >&2
  fi
  exit "$rc"
}
trap record_attempt EXIT

[[ -d "$CASE_DIR" ]] || fail "case directory not found: $CASE_DIR"
INSTRUCTION="$CASE_DIR/Instruction.md"
if [[ ! -r "$INSTRUCTION" && -r "$CASE_DIR/instruction.md" ]]; then
  INSTRUCTION="$CASE_DIR/instruction.md"
fi
[[ -r "$INSTRUCTION" ]] || \
  fail "Instruction.md not found or unreadable under: $CASE_DIR"
CASE_ID="$(basename -- "$CASE_DIR")"
[[ -n "$CASE_ID" && "$CASE_ID" != "." && "$CASE_ID" != "/" ]] || \
  fail "cannot derive case id from CASE_DIR: $CASE_DIR"
[[ -r "$ENV_FILE" ]] || fail "model env not found or unreadable: $ENV_FILE"
[[ -x "$PYTHON_BIN" ]] || fail "Presenter Python is not executable: $PYTHON_BIN"
if ! "$PYTHON_BIN" -c \
    'import anthropic, requests, PIL, playwright, numpy, pptx, fitz, fontTools, brotli' \
    >/dev/null 2>&1; then
  fail "Long-Horizon Presenter Python dependencies are incomplete: $PYTHON_BIN"
fi
[[ -r "$ATTEMPT_WRITER" ]] || fail "attempt writer not found: $ATTEMPT_WRITER"
[[ -r "$METADATA_WRITER" ]] || fail "metadata writer not found: $METADATA_WRITER"
[[ -r "$ARTIFACT_ADAPTER" ]] || fail "artifact adapter not found: $ARTIFACT_ADAPTER"
command -v timeout >/dev/null 2>&1 || fail "GNU timeout is required"
[[ ! -e "$PIPELINE_ROOT" ]] || fail "Presenter runtime already exists: $PIPELINE_ROOT"
[[ ! -e "$DEST_DIR" && ! -L "$DEST_DIR" ]] || fail "destination already exists: $DEST_DIR"

ATTEMPT_STAGE="env_validation"
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
EFFECTIVE_MODEL="${MODEL:-${STUDENT_MODEL:-unknown}}"
EFFECTIVE_BACKEND="${MODEL_BACKEND:-anthropic}"
EFFECTIVE_BACKEND="${EFFECTIVE_BACKEND,,}"
case "$EFFECTIVE_BACKEND" in
  claude|anthropic)
    EFFECTIVE_BACKEND="anthropic"
    [[ -n "${ANTHROPIC_API_KEY:-}" ]] || fail "ANTHROPIC_API_KEY must be set by $ENV_FILE"
    ;;
  openai|student)
    EFFECTIVE_BACKEND="openai"
    [[ -n "${STUDENT_API_KEY:-}" ]] || fail "STUDENT_API_KEY must be set by $ENV_FILE"
    [[ -n "${STUDENT_BASE_URL:-}" ]] || fail "STUDENT_BASE_URL must be set by $ENV_FILE"
    ;;
  *)
    fail "unsupported MODEL_BACKEND=$EFFECTIVE_BACKEND in $ENV_FILE"
    ;;
esac
export MODEL_BACKEND="$EFFECTIVE_BACKEND"

if [[ "$DRY_RUN" == "1" ]]; then
  ATTEMPT_STAGE="dry_run"
  HELP_OUTPUT="$("$PYTHON_BIN" "$PIPELINE_SCRIPT" --help)"
  [[ "$HELP_OUTPUT" == *"--max-attempts"* ]] || fail "Presenter CLI does not expose --max-attempts"
  [[ "$HELP_OUTPUT" == *"--resume"* ]] || fail "Presenter CLI does not expose --resume"
  [[ "$HELP_OUTPUT" == *"--input"* ]] || fail "Presenter CLI does not expose --input"
  printf 'dry-run validation completed; no generation artifacts were created\n'
  ATTEMPT_STAGE="completed"
  exit 0
fi

mkdir -p "$PIPELINE_RUNS_DIR" "$PIPELINE_LOGS_DIR" "$GENERATION_WORK_ROOT"
export TMPDIR="$GENERATION_WORK_ROOT"
SEED_FILE="$(mktemp "$TMPDIR/thread-bench-seed.XXXXXX.jsonl")"
TMP_FILES+=("$SEED_FILE")
"$BENCHMARK_PYTHON" - "$CASE_DIR" "$INSTRUCTION" "$SEED_FILE" <<'PY'
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

case_dir = Path(sys.argv[1]).resolve()
instruction_file = Path(sys.argv[2]).resolve()
seed_file = Path(sys.argv[3])
excluded_names = {
    "case.yaml",
    "case_metadata.json",
    "dashboard_analysis.json",
    "instruction.md",
    "ref.json",
}
excluded_roots = {
    ".generation-attempts",
    "evaluations",
    "outputs",
    "rubric_versions",
}
attachments = []
for root, directories, filenames in os.walk(case_dir):
    directories[:] = sorted(
        name
        for name in directories
        if name not in excluded_roots and not name.startswith(".")
    )
    for filename in sorted(filenames):
        if filename.startswith(".") or filename.lower() in excluded_names:
            continue
        path = Path(root, filename)
        if path.is_file() and path.resolve() != instruction_file:
            attachments.append(str(path.resolve()))
seed = {
    "qid": case_dir.name,
    "query": instruction_file.read_text(encoding="utf-8"),
}
if attachments:
    seed["attachments"] = attachments
seed_file.write_text(
    json.dumps(seed, ensure_ascii=False, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
print(f"attachments: {len(attachments)}")
for attachment in attachments:
    print(f"  - {attachment}")
PY

SAFE_TAG="${MODEL_TAG//./_}"
SAFE_TAG="${SAFE_TAG//-/_}"
SAFE_CASE_TAG="$(basename -- "$CASE_DIR")"
SAFE_CASE_TAG="${SAFE_CASE_TAG//./_}"
SAFE_CASE_TAG="${SAFE_CASE_TAG//-/_}"
BATCH="benchmark_${SAFE_CASE_TAG}_${SAFE_TAG}_${RUN_ID}"
BATCH_DIR="$PIPELINE_RUNS_DIR/$BATCH"
MANIFEST="$PIPELINE_LOGS_DIR/$BATCH.manifest.jsonl"
SAMPLE_HASH="$("$BENCHMARK_PYTHON" - "$SEED_FILE" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

seed = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
attachments = seed.get("attachments")
if os.environ.get("SID_ATTACH_BASENAME", "1") == "1" and isinstance(attachments, list):
    normalized = dict(seed)
    normalized["attachments"] = [
        ({**item, "path": os.path.basename(str(item["path"]))}
         if isinstance(item, dict) and "path" in item else item)
        for item in attachments
    ]
    seed = normalized
canonical = json.dumps(seed, sort_keys=True, ensure_ascii=False)
print(hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12])
PY
)"
EXPECTED_RUN_DIR="$BATCH_DIR/${BATCH}_${SAMPLE_HASH}"
mkdir -p "$(dirname -- "$DEST_DIR")"
ln -s -- "$EXPECTED_RUN_DIR" "$DEST_DIR"
LIVE_OUTPUT_LINK=1
PRESENTER_ENV=(
  "PATH=$(dirname -- "$PYTHON_BIN"):$PATH"
  "NOVA_RAW_V2=0"
  "PPT_RUNS_ROOT=$PIPELINE_RUNS_DIR"
  "PPT_LOGS_ROOT=$PIPELINE_LOGS_DIR"
  "PPT_SKILLS_ROOT=$SKILLS_ROOT"
  "MAX_TURNS=$POLICY_MAX_TURNS"
  "MAX_TOKENS=$POLICY_MAX_TOKENS"
  "SUBAGENT_MAX_TOKENS=$POLICY_SUBAGENT_MAX_TOKENS"
  "THINKING=$POLICY_THINKING"
  "THINK_EFFORT=$POLICY_THINK_EFFORT"
  "MAX_CONCURRENT_CHILDREN=$POLICY_MAX_CONCURRENT_CHILDREN"
  "SLIDE_CONCURRENCY=$POLICY_SLIDE_CONCURRENCY"
  "TURN_TOOL_PARALLEL=$POLICY_TURN_TOOL_PARALLEL"
  "SLIDE_MAX_TURNS_BASE=$POLICY_SLIDE_MAX_TURNS_BASE"
  "SLIDE_MAX_TURNS_CAP=$POLICY_SLIDE_MAX_TURNS_CAP"
  "MAX_VISION_EDGE=$POLICY_MAX_VISION_EDGE"
  "POOL_MAX_WORKERS=1"
  "CONCURRENCY_FILE=$PIPELINE_ROOT/CONCURRENCY"
  "RENDER_GLOBAL_LIMIT=$POLICY_RENDER_GLOBAL_LIMIT"
  "RENDER_LOCK_DIR=$PRESENTER_RUNTIME_ROOT/render-slots"
)

printf 'model_tag: %s\n' "$MODEL_TAG"
printf 'model: %s\n' "$EFFECTIVE_MODEL"
printf 'backend: %s\n' "$EFFECTIVE_BACKEND"
printf 'presenter_root: %s\n' "$PRESENTER_ROOT"
printf 'presenter_version: %s\n' "$PRESENTER_LOCAL_VERSION"
printf 'harness: %s\n' "$PIPELINE_SCRIPT"
printf 'skill: %s\n' "$SKILL_ROOT"
printf 'pipeline_runtime: %s\n' "$PIPELINE_ROOT"
printf 'case_id: %s\n' "$CASE_ID"
printf 'instruction: %s (exact query passthrough)\n' "$INSTRUCTION"
printf 'presenter_max_attempts: %s\n' "$PRESENTER_MAX_ATTEMPTS"
printf 'generation_timeout_seconds: %s\n' "$GENERATION_TIMEOUT_SECONDS"
printf 'destination: %s\n' "$DEST_DIR"

GENERATION_STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
GENERATION_STARTED_EPOCH="$(date +%s)"
GENERATION_DEADLINE_EPOCH="$((GENERATION_STARTED_EPOCH + GENERATION_TIMEOUT_SECONDS))"
PIPELINE_RC=0
PRESENTER_INVOCATIONS=0
LAST_UPSTREAM_STATUS=""
RUN_DIR=""

while [[ "$PRESENTER_INVOCATIONS" -lt "$PRESENTER_MAX_ATTEMPTS" ]]; do
  CURRENT_EPOCH="$(date +%s)"
  REMAINING_SECONDS="$((GENERATION_DEADLINE_EPOCH - CURRENT_EPOCH))"
  [[ "$REMAINING_SECONDS" -gt 0 ]] || {
    PIPELINE_RC=124
    break
  }
  INVOCATION_TIMEOUT="$PRESENTER_ATTEMPT_TIMEOUT_SECONDS"
  if [[ "$REMAINING_SECONDS" -lt "$INVOCATION_TIMEOUT" ]]; then
    INVOCATION_TIMEOUT="$REMAINING_SECONDS"
  fi
  PRESENTER_INVOCATIONS="$((PRESENTER_INVOCATIONS + 1))"
  ATTEMPT_STAGE="pipeline_attempt_${PRESENTER_INVOCATIONS}"
  RESUME_ARGS=()
  if [[ "$PRESENTER_INVOCATIONS" -gt 1 ]]; then
    RESUME_ARGS+=(--resume)
  fi
  printf '\nLong-Horizon Presenter invocation %s/%s (timeout: %ss)\n' \
    "$PRESENTER_INVOCATIONS" "$PRESENTER_MAX_ATTEMPTS" "$INVOCATION_TIMEOUT"

  set +e
  env "${PRESENTER_ENV[@]}" \
    timeout --signal=TERM --kill-after=30s "$INVOCATION_TIMEOUT" \
    "$PYTHON_BIN" "$PIPELINE_SCRIPT" \
      --input "$SEED_FILE" \
      --batch "$BATCH" \
      --workers 1 \
      --limit 1 \
      --max-attempts "$PRESENTER_MAX_ATTEMPTS" \
      "${RESUME_ARGS[@]}"
  PIPELINE_RC=$?
  set -e
  [[ "$PIPELINE_RC" -eq 0 ]] || break
  [[ -r "$MANIFEST" ]] || fail "Presenter manifest not found: $MANIFEST"

  MANIFEST_STATE="$("$BENCHMARK_PYTHON" - "$MANIFEST" "$BATCH_DIR" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

manifest = Path(sys.argv[1])
batch_dir = Path(sys.argv[2]).resolve()
records = [
    json.loads(line)
    for line in manifest.read_text(encoding="utf-8").splitlines()
    if line.strip()
]
if not records:
    raise SystemExit("Presenter manifest is empty")
record = records[-1]
run_dir = Path(str(record.get("run_dir") or "")).resolve()
if run_dir.parent != batch_dir:
    raise SystemExit(f"Presenter run escaped batch directory: {run_dir}")
print(len(records))
print(record.get("status") or "")
print(run_dir)
PY
)" || fail "cannot read Presenter manifest state"
  readarray -t MANIFEST_VALUES < <(printf '%s\n' "$MANIFEST_STATE")
  [[ ${#MANIFEST_VALUES[@]} -eq 3 ]] || fail "invalid Presenter manifest state"
  OBSERVED_ATTEMPTS="${MANIFEST_VALUES[0]}"
  LAST_UPSTREAM_STATUS="${MANIFEST_VALUES[1]}"
  RUN_DIR="${MANIFEST_VALUES[2]}"
  [[ "$(realpath -m -- "$RUN_DIR")" == "$(realpath -m -- "$EXPECTED_RUN_DIR")" ]] || \
    fail "Presenter run directory does not match live output target: $RUN_DIR"
  [[ "$OBSERVED_ATTEMPTS" -le "$PRESENTER_INVOCATIONS" ]] || \
    fail "Presenter manifest contains more attempts than wrapper invocations"
  printf 'Presenter status after invocation %s: %s\n' \
    "$PRESENTER_INVOCATIONS" "$LAST_UPSTREAM_STATUS"
  [[ "$LAST_UPSTREAM_STATUS" != "completed" ]] || break
done

rm -f -- "$SEED_FILE"
TMP_FILES=()
GENERATION_FINISHED_EPOCH="$(date +%s)"
GENERATION_FINISHED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
GENERATION_DURATION_SECONDS="$((GENERATION_FINISHED_EPOCH - GENERATION_STARTED_EPOCH))"

if [[ "$PIPELINE_RC" -eq 124 || "$PIPELINE_RC" -eq 137 ]]; then
  fail "generation exceeded its configured wall-clock budget"
fi
[[ "$PIPELINE_RC" -eq 0 ]] || fail "Long-Horizon Presenter exited with code $PIPELINE_RC"
[[ -n "$RUN_DIR" && -d "$RUN_DIR" ]] || fail "Long-Horizon Presenter run directory is unavailable"
[[ -r "$MANIFEST" ]] || fail "Long-Horizon Presenter manifest not found: $MANIFEST"

ATTEMPT_STAGE="artifact_adaptation"
[[ ! -e "$RUN_DIR/generation_metadata.json" ]] || \
  fail "native run contains reserved benchmark sidecar: $RUN_DIR/generation_metadata.json"
"$BENCHMARK_PYTHON" "$ARTIFACT_ADAPTER" \
  --run-dir "$RUN_DIR" \
  --output-dir "$DEST_DIR" \
  --renderer "$ARTIFACT_RENDERER" \
  --python-bin "$PYTHON_BIN"
ADAPTATION_RECORD="$DEST_DIR/artifact_adaptation.json"
[[ -r "$ADAPTATION_RECORD" ]] || fail "artifact adapter did not write: $ADAPTATION_RECORD"
ARTIFACT_MODE="$("$BENCHMARK_PYTHON" - "$ADAPTATION_RECORD" <<'PY'
import json
import sys
from pathlib import Path

value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
mode = value.get("mode") if isinstance(value, dict) else None
if mode not in {"read_only_native_output", "limited_artifact_adaptation"}:
    raise SystemExit(f"invalid artifact adaptation mode: {mode!r}")
print(mode)
PY
)" || fail "cannot read artifact adaptation mode"

ATTEMPT_STAGE="metadata"
METADATA_ARGS=(
  --run-dir "$RUN_DIR"
  --manifest "$MANIFEST"
  --output-dir "$DEST_DIR"
  --case-id "$CASE_ID"
  --instruction "$INSTRUCTION"
  --model-tag "$MODEL_TAG"
  --model "$EFFECTIVE_MODEL"
  --backend "$EFFECTIVE_BACKEND"
  --batch "$BATCH"
  --run-id "$RUN_ID"
  --artifact-mode "$ARTIFACT_MODE"
  --adaptation-record "$ADAPTATION_RECORD"
  --pipeline-name "long-horizon-presenter"
  --pipeline-root "$HARNESS_ROOT"
  --pipeline-source-root "$PRESENTER_ROOT"
  --pipeline-version "$PRESENTER_LOCAL_VERSION"
  --skill-name "long-horizon-presenter"
  --skill-root "$SKILL_ROOT"
  --skill-version "$PRESENTER_VERSION"
  --presenter-source-sha256 "$PRESENTER_SOURCE_SHA256"
  --skill-source-sha256 "$SKILL_SOURCE_SHA256"
  --harness-source-sha256 "$HARNESS_SOURCE_SHA256"
  --started-at "$GENERATION_STARTED_AT"
  --finished-at "$GENERATION_FINISHED_AT"
  --duration-seconds "$GENERATION_DURATION_SECONDS"
  --generation-timeout-seconds "$GENERATION_TIMEOUT_SECONDS"
  --presenter-max-attempts "$PRESENTER_MAX_ATTEMPTS"
  --presenter-invocations "$PRESENTER_INVOCATIONS"
  --runtime-setting "MAX_TURNS=$POLICY_MAX_TURNS"
  --runtime-setting "MAX_TOKENS=$POLICY_MAX_TOKENS"
  --runtime-setting "SUBAGENT_MAX_TOKENS=$POLICY_SUBAGENT_MAX_TOKENS"
  --runtime-setting "THINKING=$POLICY_THINKING"
  --runtime-setting "THINK_EFFORT=$POLICY_THINK_EFFORT"
  --runtime-setting "MAX_CONCURRENT_CHILDREN=$POLICY_MAX_CONCURRENT_CHILDREN"
  --runtime-setting "SLIDE_CONCURRENCY=$POLICY_SLIDE_CONCURRENCY"
  --runtime-setting "TURN_TOOL_PARALLEL=$POLICY_TURN_TOOL_PARALLEL"
  --runtime-setting "SLIDE_MAX_TURNS_BASE=$POLICY_SLIDE_MAX_TURNS_BASE"
  --runtime-setting "SLIDE_MAX_TURNS_CAP=$POLICY_SLIDE_MAX_TURNS_CAP"
  --runtime-setting "MAX_VISION_EDGE=$POLICY_MAX_VISION_EDGE"
  --runtime-setting "POOL_MAX_WORKERS=1"
  --runtime-setting "RENDER_GLOBAL_LIMIT=$POLICY_RENDER_GLOBAL_LIMIT"
  --runtime-setting "RENDER_LOCK_DIR=$PRESENTER_RUNTIME_ROOT/render-slots"
)
if ! "$BENCHMARK_PYTHON" "$METADATA_WRITER" "${METADATA_ARGS[@]}"; then
  fail "Long-Horizon Presenter run did not satisfy the export artifact contract"
fi

if [[ -L "$DEST_DIR" ]]; then
  rm -f -- "$DEST_DIR"
  mv -- "$RUN_DIR" "$DEST_DIR"
  ln -s -- "$DEST_DIR" "$RUN_DIR"
  LIVE_OUTPUT_LINK=0
fi

printf '\nexported Long-Horizon Presenter run\n'
printf 'run_dir: %s\n' "$RUN_DIR"
printf 'output_dir: %s\n' "$DEST_DIR"
printf 'artifact_mode: %s\n' "$ARTIFACT_MODE"
printf 'presenter_attempts_observed: %s\n' "$PRESENTER_INVOCATIONS"
printf 'generation_metadata: %s/generation_metadata.json\n' "$DEST_DIR"
printf 'generation_duration_seconds: %s\n' "$GENERATION_DURATION_SECONDS"
ATTEMPT_STAGE="completed"
