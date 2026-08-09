#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Optional direct shell configuration.  Export values here, export them in
# your current shell, or copy .env.example to .env. Never commit real keys.
# export SENSENOVA_IMAGE_BASE_URL="https://example.com/v1"
# export SENSENOVA_IMAGE_API_KEY="sk-..."
# export SENSENOVA_SEARCH_BASE_URL="https://google.serper.dev"
# export SENSENOVA_SEARCH_API_KEY="..."

PYTHON_BIN="${SENSENOVA_BOOTSTRAP_PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3.12 || true)"
fi
if [[ -z "$PYTHON_BIN" ]] && command -v uv >/dev/null 2>&1; then
  PYTHON_BIN="$(uv python find 3.12 2>/dev/null || true)"
fi
if [[ -z "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3 || command -v python || true)"
fi
if [[ -z "$PYTHON_BIN" ]]; then
  echo "Python 3.12+ is required. Install it directly or run: uv python install 3.12" >&2
  exit 1
fi

if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
  echo "Python 3.12+ is required; found $("$PYTHON_BIN" --version 2>&1)." >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "[SenseNova Present] Installing uv for the current user..."
  "$PYTHON_BIN" -m pip install --user uv
  USER_BASE="$($PYTHON_BIN -m site --user-base)"
  export PATH="$USER_BASE/bin:$PATH"
fi

exec "$PYTHON_BIN" "$PROJECT_ROOT/scripts/launch.py" "$@"
