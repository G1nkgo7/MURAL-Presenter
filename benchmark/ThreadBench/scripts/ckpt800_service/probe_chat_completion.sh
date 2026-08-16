#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${CKPT800_BASE_URL:-http://127.0.0.1:8000/v1}"
MODEL="${CKPT800_MODEL:-ckpt800}"
TIMEOUT_SECONDS="${CKPT800_PROBE_TIMEOUT_SECONDS:-20}"
API_KEY="${CKPT800_API_KEY:-EMPTY}"

[[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || {
  printf 'invalid CKPT800_PROBE_TIMEOUT_SECONDS: %s\n' "$TIMEOUT_SECONDS" >&2
  exit 2
}

response_file="$(mktemp "${TMPDIR:-/tmp}/ckpt800-probe.XXXXXX.json")"
cleanup() {
  rm -f -- "$response_file"
}
trap cleanup EXIT

http_code="$(
  curl -sS \
    --connect-timeout 5 \
    --max-time "$TIMEOUT_SECONDS" \
    -o "$response_file" \
    -w '%{http_code}' \
    "${BASE_URL%/}/chat/completions" \
    -H "Authorization: Bearer $API_KEY" \
    -H 'Content-Type: application/json' \
    --data-binary "$(
      printf '{"model":"%s","messages":[{"role":"user","content":"Reply with exactly PONG."}],"temperature":0,"max_tokens":16,"chat_template_kwargs":{"enable_thinking":false}}' \
        "$MODEL"
    )"
)"

if [[ "$http_code" != "200" ]]; then
  printf 'probe HTTP %s: ' "$http_code" >&2
  head -c 500 "$response_file" >&2 || true
  printf '\n' >&2
  exit 1
fi

/usr/bin/python3 - "$response_file" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
    choice = payload["choices"][0]
    content = choice["message"]["content"]
except (OSError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
    raise SystemExit(f"invalid chat-completions response: {exc}")

if not isinstance(content, str) or not content.strip():
    raise SystemExit(f"empty chat-completions content: {content!r}")

print(content.strip())
PY
