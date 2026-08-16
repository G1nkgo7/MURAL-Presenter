#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
JUDGE_SECRETS_FILE="${JUDGE_SECRETS_FILE:-$SCRIPT_DIR/.env}"
JUDGE_CONFIG_FILE="${JUDGE_CONFIG_FILE:-$SCRIPT_DIR/configs/judge.env}"

[[ -f "$JUDGE_SECRETS_FILE" ]] || {
  echo "error: Judge secrets file not found: $JUDGE_SECRETS_FILE" >&2
  exit 2
}
[[ -f "$JUDGE_CONFIG_FILE" ]] || {
  echo "error: Judge config file not found: $JUDGE_CONFIG_FILE" >&2
  exit 2
}

set -a
source "$JUDGE_SECRETS_FILE"
source "$JUDGE_CONFIG_FILE"
set +a

CASE_DIR="${CASE_DIR:-$SCRIPT_DIR/data/Education/Education-lh-Animal_navigation}"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
RUBRIC_REVISION="${RUBRIC_REVISION:-}"
EVALUATION_REVISION="${EVALUATION_REVISION:-}"
EVALUATION_SCOPE="${EVALUATION_SCOPE:-all}"
DIMENSION_MAX_CONCURRENT_JUDGE_REQUESTS="${DIMENSION_MAX_CONCURRENT_JUDGE_REQUESTS:-4}"
DIMENSION_JUDGE_MAX_TOKENS="${DIMENSION_JUDGE_MAX_TOKENS:-8000}"
JUDGE_MODEL="${JUDGE_MODEL:-}"
INTERMEDIATE_JUDGE_MODEL="${INTERMEDIATE_JUDGE_MODEL:-${JUDGE_MODEL:-gemini-3.5-flash}}"
INTERMEDIATE_JUDGE_PROVIDER="${INTERMEDIATE_JUDGE_PROVIDER:-openai}"
INTERMEDIATE_JUDGE_BASE_URL="${INTERMEDIATE_JUDGE_BASE_URL:-}"
INTERMEDIATE_JUDGE_API_KEY="${INTERMEDIATE_JUDGE_API_KEY:-}"
INTERMEDIATE_JUDGE_API_KEY_ENV="${INTERMEDIATE_JUDGE_API_KEY_ENV:-}"
FINAL_JUDGE_MODEL="${FINAL_JUDGE_MODEL:-${JUDGE_MODEL:-gemini-3.5-flash}}"
PREAUDIT_MODEL="${PREAUDIT_MODEL:-${PREAUDIT_JUDGE_MODEL:-gemini-3.1-pro-preview}}"
PREAUDIT_JUDGE_MODEL="$PREAUDIT_MODEL"
PREAUDIT_JUDGE_PROVIDER="${PREAUDIT_JUDGE_PROVIDER:-}"
PREAUDIT_JUDGE_BASE_URL="${PREAUDIT_JUDGE_BASE_URL:-}"
PREAUDIT_JUDGE_API_KEY="${PREAUDIT_JUDGE_API_KEY:-}"
PREAUDIT_JUDGE_API_KEY_ENV="${PREAUDIT_JUDGE_API_KEY_ENV:-}"
USE_FINAL_PREAUDIT="${USE_FINAL_PREAUDIT:-0}"
REUSE_FINAL_KNOWLEDGE_EVALUATION_REVISION="${REUSE_FINAL_KNOWLEDGE_EVALUATION_REVISION:-}"
REUSE_UNCHANGED_EVALUATION_REVISION="${REUSE_UNCHANGED_EVALUATION_REVISION:-}"
FORCE_REJUDGE_CRITERION_IDS="${FORCE_REJUDGE_CRITERION_IDS:-}"
GROUP_MIN_DECAY_ALPHA="${GROUP_MIN_DECAY_ALPHA:-0.3}"
FINAL_JUDGE_PROVIDER="${FINAL_JUDGE_PROVIDER:-openai}"
FINAL_JUDGE_BASE_URL="${FINAL_JUDGE_BASE_URL:-}"
FINAL_JUDGE_API_KEY="${FINAL_JUDGE_API_KEY:-}"
FINAL_JUDGE_API_KEY_ENV="${FINAL_JUDGE_API_KEY_ENV:-}"
TASK_RETRIES="${TASK_RETRIES:-3}"
OVERWRITE="${OVERWRITE:-1}"
RESUME="${RESUME:-1}"
DRY_RUN="${DRY_RUN:-0}"
MOCK_JUDGE="${MOCK_JUDGE:-0}"
ALLOW_RECOVERED_RUN_FOR_TESTING="${ALLOW_RECOVERED_RUN_FOR_TESTING:-0}"
RUN_SELECTOR="${RUN_SELECTOR:-$SCRIPT_DIR/scripts/select_valid_generation_run.py}"
BENCHMARK_PYTHON="${BENCHMARK_PYTHON:-python3}"

[[ "$OVERWRITE" == "0" || "$OVERWRITE" == "1" ]] || {
  echo "error: OVERWRITE must be 0 or 1" >&2
  exit 2
}
[[ "$RESUME" == "0" || "$RESUME" == "1" ]] || {
  echo "error: RESUME must be 0 or 1" >&2
  exit 2
}
[[ "$DRY_RUN" == "0" || "$DRY_RUN" == "1" ]] || {
  echo "error: DRY_RUN must be 0 or 1" >&2
  exit 2
}
[[ "$MOCK_JUDGE" == "0" || "$MOCK_JUDGE" == "1" ]] || {
  echo "error: MOCK_JUDGE must be 0 or 1" >&2
  exit 2
}
[[ "$USE_FINAL_PREAUDIT" == "0" || "$USE_FINAL_PREAUDIT" == "1" ]] || {
  echo "error: USE_FINAL_PREAUDIT must be 0 or 1" >&2
  exit 2
}
[[ "$ALLOW_RECOVERED_RUN_FOR_TESTING" == "0" || "$ALLOW_RECOVERED_RUN_FOR_TESTING" == "1" ]] || {
  echo "error: ALLOW_RECOVERED_RUN_FOR_TESTING must be 0 or 1" >&2
  exit 2
}
[[ "$EVALUATION_SCOPE" == "all" || "$EVALUATION_SCOPE" == "final" || "$EVALUATION_SCOPE" == "intermediate" ]] || {
  echo "error: EVALUATION_SCOPE must be all, final, or intermediate" >&2
  exit 2
}
awk -v value="$GROUP_MIN_DECAY_ALPHA" 'BEGIN { exit !(value ~ /^[0-9]+([.][0-9]+)?$/ && value >= 0 && value <= 1) }' || {
  echo "error: GROUP_MIN_DECAY_ALPHA must be a number in [0, 1]" >&2
  exit 2
}
if [[ "$EVALUATION_SCOPE" == "intermediate" && "$USE_FINAL_PREAUDIT" == "1" ]]; then
  echo "error: USE_FINAL_PREAUDIT requires EVALUATION_SCOPE=all or final" >&2
  exit 2
fi

resolve_api_key_reference() {
  local role="$1"
  local reference="$2"
  if [[ -z "$reference" ]]; then
    return 0
  fi
  if [[ ! "$reference" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    echo "error: invalid API key variable name for $role: $reference" >&2
    return 2
  fi
  if [[ -z "${!reference:-}" ]]; then
    echo "error: $reference, referenced by $role, is not set in $JUDGE_SECRETS_FILE" >&2
    return 2
  fi
  printf '%s' "${!reference}"
}

if [[ "$DRY_RUN" == "0" && "$MOCK_JUDGE" == "0" \
  && "$EVALUATION_SCOPE" != "final" && -n "$INTERMEDIATE_JUDGE_API_KEY_ENV" ]]; then
  INTERMEDIATE_JUDGE_API_KEY="$(
    resolve_api_key_reference "Intermediate Judge" "$INTERMEDIATE_JUDGE_API_KEY_ENV"
  )"
fi
if [[ "$DRY_RUN" == "0" && "$MOCK_JUDGE" == "0" \
  && "$EVALUATION_SCOPE" != "intermediate" && -n "$FINAL_JUDGE_API_KEY_ENV" ]]; then
  FINAL_JUDGE_API_KEY="$(
    resolve_api_key_reference "Final Judge" "$FINAL_JUDGE_API_KEY_ENV"
  )"
fi
if [[ "$DRY_RUN" == "0" && "$MOCK_JUDGE" == "0" \
  && "$USE_FINAL_PREAUDIT" == "1" && -n "$PREAUDIT_JUDGE_API_KEY_ENV" ]]; then
  PREAUDIT_JUDGE_API_KEY="$(
    resolve_api_key_reference "Final preaudit" "$PREAUDIT_JUDGE_API_KEY_ENV"
  )"
fi
export INTERMEDIATE_JUDGE_API_KEY FINAL_JUDGE_API_KEY PREAUDIT_JUDGE_API_KEY

if [[ -z "$RUBRIC_REVISION" ]]; then
  ACTIVE_RUBRIC_REVISION="$(
    awk '
      /^[[:space:]]*active_rubric_revision:[[:space:]]*/ {
        value = $0
        sub(/^[^:]*:[[:space:]]*/, "", value)
        sub(/[[:space:]#].*$/, "", value)
        gsub(/"/, "", value)
        print value
        exit
      }
    ' "$CASE_DIR/case.yaml"
  )"
  if [[ "$ACTIVE_RUBRIC_REVISION" =~ ^v[0-9][0-9][0-9]$ ]] \
    && [[ -d "$CASE_DIR/rubric_versions/$ACTIVE_RUBRIC_REVISION" ]]; then
    RUBRIC_REVISION="$ACTIVE_RUBRIC_REVISION"
  else
    mapfile -t RUBRIC_REVISIONS < <(
      for rubric_dir in "$CASE_DIR"/rubric_versions/v[0-9][0-9][0-9]; do
        [[ -d "$rubric_dir" ]] || continue
        if [[ -f "$rubric_dir/intermediate_case_rubric.json" \
          && -f "$rubric_dir/final_case_rubric.json" ]]; then
          basename "$rubric_dir"
        fi
      done | sort -V
    )
    if [[ "${#RUBRIC_REVISIONS[@]}" -eq 0 ]]; then
      echo "error: no supported rubric found under $CASE_DIR/rubric_versions" >&2
      exit 1
    fi
    RUBRIC_REVISION="${RUBRIC_REVISIONS[-1]}"
  fi
fi
EVALUATION_REVISION="${EVALUATION_REVISION:-$RUBRIC_REVISION}"
RUBRIC_DIR="$CASE_DIR/rubric_versions/$RUBRIC_REVISION"
INTERMEDIATE_CASE_RUBRIC="$RUBRIC_DIR/intermediate_case_rubric.json"
FINAL_CASE_RUBRIC="$RUBRIC_DIR/final_case_rubric.json"
if [[ ! -f "$INTERMEDIATE_CASE_RUBRIC" || ! -f "$FINAL_CASE_RUBRIC" ]]; then
  echo "error: split intermediate/final rubric files not found under $RUBRIC_DIR" >&2
  exit 1
fi

if [[ -n "${GEN_MODELS:-}" ]]; then
  read -r -a GEN_MODEL_LIST <<<"$GEN_MODELS"
else
  mapfile -t GEN_MODEL_LIST < <(
    find "$CASE_DIR/outputs" -mindepth 1 -maxdepth 1 -type d ! -name '.*' -printf '%f\n' | sort
  )
fi
if [[ "${#GEN_MODEL_LIST[@]}" -eq 0 ]]; then
  echo "error: no generation model directories found under $CASE_DIR/outputs" >&2
  exit 1
fi

if [[ -z "$INTERMEDIATE_JUDGE_BASE_URL" ]]; then
  if [[ "$INTERMEDIATE_JUDGE_PROVIDER" == "anthropic" ]]; then
    INTERMEDIATE_JUDGE_BASE_URL="${ANTHROPIC_BASE_URL:-https://tokenhub.sensetime.com}"
  else
    INTERMEDIATE_JUDGE_BASE_URL="${GEMINI_BASE_URL:-${OPENAI_BASE_URL:-https://tokenhub.sensetime.com/v1}}"
  fi
fi
FINAL_JUDGE_BASE_URL="${FINAL_JUDGE_BASE_URL:-${GEMINI_BASE_URL:-${OPENAI_BASE_URL:-https://tokenhub.sensetime.com/v1}}}"
PREAUDIT_JUDGE_PROVIDER="${PREAUDIT_JUDGE_PROVIDER:-$FINAL_JUDGE_PROVIDER}"
if [[ -z "$PREAUDIT_JUDGE_BASE_URL" ]]; then
  if [[ "$PREAUDIT_JUDGE_MODEL" == gpt-5.6-* ]]; then
    PREAUDIT_JUDGE_BASE_URL="${OPENAI_5_6_BASE_URL:-$FINAL_JUDGE_BASE_URL}"
  else
    PREAUDIT_JUDGE_BASE_URL="$FINAL_JUDGE_BASE_URL"
  fi
fi
if [[ -z "$PREAUDIT_JUDGE_API_KEY" && "$PREAUDIT_JUDGE_MODEL" == gpt-5.6-* ]]; then
  PREAUDIT_JUDGE_API_KEY="${OPENAI_5_6_API_KEY:-}"
fi
export PREAUDIT_JUDGE_API_KEY

export PLAYWRIGHT_BROWSERS_PATH="${JUDGE_PLAYWRIGHT_BROWSERS_PATH:-$SCRIPT_DIR/.supervision/playwright-browsers}"
PLAYWRIGHT_LIBS="${JUDGE_PLAYWRIGHT_LIBS:-}"
if [[ -n "$PLAYWRIGHT_LIBS" ]]; then
  export LD_LIBRARY_PATH="$PLAYWRIGHT_LIBS${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

failed=0
for gen_model in "${GEN_MODEL_LIST[@]}"; do
  if [[ -n "${GEN_RUN:-}" ]]; then
    gen_run="$GEN_RUN"
  else
    SELECTOR_ARGS=(
      "$RUN_SELECTOR"
      --case-dir "$CASE_DIR"
      --model "$gen_model"
    )
    if [[ "$ALLOW_RECOVERED_RUN_FOR_TESTING" == "0" ]]; then
      SELECTOR_ARGS+=(--require-benchmark-pass)
    fi
    gen_run="$("$BENCHMARK_PYTHON" "${SELECTOR_ARGS[@]}")" || {
      echo "error: failed to select generation run for $gen_model" >&2
      failed=$((failed + 1))
      continue
    }
  fi

  if [[ "$USE_FINAL_PREAUDIT" == "1" ]]; then
    preaudit_profile="$PREAUDIT_JUDGE_MODEL"
  else
    preaudit_profile="no-preaudit"
  fi
  if [[ "$EVALUATION_SCOPE" == "all" ]]; then
    judge_profile="$(printf '%s__%s__%s' "$INTERMEDIATE_JUDGE_MODEL" "$FINAL_JUDGE_MODEL" "$preaudit_profile" | sed -E 's/[^A-Za-z0-9_.-]+/_/g; s/^[._]+//; s/[._]+$//')"
  elif [[ "$EVALUATION_SCOPE" == "intermediate" ]]; then
    judge_profile="$(printf '%s__intermediate-only' "$INTERMEDIATE_JUDGE_MODEL" | sed -E 's/[^A-Za-z0-9_.-]+/_/g; s/^[._]+//; s/[._]+$//')"
  else
    judge_profile="$(printf '%s__final-only' "$FINAL_JUDGE_MODEL" | sed -E 's/[^A-Za-z0-9_.-]+/_/g; s/^[._]+//; s/[._]+$//')"
    if [[ "$USE_FINAL_PREAUDIT" == "1" ]]; then
      judge_profile="${judge_profile}__$(printf '%s' "$PREAUDIT_JUDGE_MODEL" | sed -E 's/[^A-Za-z0-9_.-]+/_/g; s/^[._]+//; s/[._]+$//')"
    fi
  fi
  if [[ -z "$judge_profile" ]]; then
    echo "error: cannot derive evaluation directory name from Judge models" >&2
    failed=$((failed + 1))
    continue
  fi
  output_dir="$CASE_DIR/evaluations/$EVALUATION_REVISION/$gen_model/$gen_run/$judge_profile"
  if [[ -s "$output_dir/score_result.json" && "$OVERWRITE" == "0" ]]; then
    if grep -q '"benchmark_eligible": true' "$output_dir/score_result.json"; then
      echo "skip: model=$gen_model run=$gen_run result=$output_dir/score_result.json"
      continue
    fi
  fi

  model_overwrite=0
  model_resume=0
  if [[ -s "$output_dir/score_result.json" ]]; then
    model_overwrite="$OVERWRITE"
  elif [[ "$RESUME" == "1" ]]; then
    model_resume=1
    if [[ -d "$output_dir" ]]; then
      echo "resume: model=$gen_model run=$gen_run output=$output_dir"
    fi
  elif [[ -d "$output_dir" ]] \
    && [[ -n "$(find "$output_dir" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
    model_overwrite=1
    echo "retry: model=$gen_model run=$gen_run overwriting incomplete output=$output_dir"
  fi

  JUDGE_ARGS=(
    "$SCRIPT_DIR/scripts/run_dimension_rubric_judge.py"
    --case-dir "$CASE_DIR"
    --gen-model "$gen_model"
    --gen-run "$gen_run"
    --rubric-revision "$RUBRIC_REVISION"
    --evaluation-revision "$EVALUATION_REVISION"
    --evaluation-scope "$EVALUATION_SCOPE"
    --intermediate-case-rubric "$INTERMEDIATE_CASE_RUBRIC"
    --final-case-rubric "$FINAL_CASE_RUBRIC"
    --judge-model "$JUDGE_MODEL"
    --intermediate-judge-provider "$INTERMEDIATE_JUDGE_PROVIDER"
    --intermediate-judge-model "$INTERMEDIATE_JUDGE_MODEL"
    --intermediate-judge-base-url "$INTERMEDIATE_JUDGE_BASE_URL"
    --final-judge-provider "$FINAL_JUDGE_PROVIDER"
    --final-judge-model "$FINAL_JUDGE_MODEL"
    --preaudit-judge-provider "$PREAUDIT_JUDGE_PROVIDER"
    --preaudit-model "$PREAUDIT_MODEL"
    --preaudit-judge-base-url "$PREAUDIT_JUDGE_BASE_URL"
    --final-judge-base-url "$FINAL_JUDGE_BASE_URL"
    --max-concurrent-judge-requests "$DIMENSION_MAX_CONCURRENT_JUDGE_REQUESTS"
    --max-tokens "$DIMENSION_JUDGE_MAX_TOKENS"
    --group-min-decay-alpha "$GROUP_MIN_DECAY_ALPHA"
    --task-retries "$TASK_RETRIES"
  )
  if [[ "$MOCK_JUDGE" == "1" ]]; then
    JUDGE_ARGS+=(--mock-judge)
  fi
  if [[ "$USE_FINAL_PREAUDIT" == "1" ]]; then
    JUDGE_ARGS+=(--use-final-preaudit)
  fi
  if [[ -n "$REUSE_FINAL_KNOWLEDGE_EVALUATION_REVISION" ]]; then
    JUDGE_ARGS+=(
      --reuse-final-knowledge-evaluation-revision
      "$REUSE_FINAL_KNOWLEDGE_EVALUATION_REVISION"
    )
  fi
  if [[ -n "$REUSE_UNCHANGED_EVALUATION_REVISION" ]]; then
    JUDGE_ARGS+=(
      --reuse-unchanged-evaluation-revision
      "$REUSE_UNCHANGED_EVALUATION_REVISION"
    )
  fi
  if [[ -n "$FORCE_REJUDGE_CRITERION_IDS" ]]; then
    read -r -a force_rejudge_ids <<< "$FORCE_REJUDGE_CRITERION_IDS"
    for criterion_id in "${force_rejudge_ids[@]}"; do
      JUDGE_ARGS+=(--force-rejudge-criterion-id "$criterion_id")
    done
  fi
  if [[ "$ALLOW_RECOVERED_RUN_FOR_TESTING" == "1" ]]; then
    JUDGE_ARGS+=(--allow-recovered-run-for-testing)
  fi
  if [[ "$model_overwrite" == "1" ]]; then
    JUDGE_ARGS+=(--overwrite)
  fi
  if [[ "$model_resume" == "1" ]]; then
    JUDGE_ARGS+=(--resume)
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    JUDGE_ARGS+=(--dry-run)
  fi

  echo "judge: model=$gen_model run=$gen_run rubric=$RUBRIC_REVISION evaluation=$EVALUATION_REVISION"
  if ! "$PYTHON_BIN" "${JUDGE_ARGS[@]}"; then
    failed=$((failed + 1))
  fi
done

[[ "$failed" -eq 0 ]] || {
  echo "failed generation models: $failed/${#GEN_MODEL_LIST[@]}" >&2
  exit 1
}
