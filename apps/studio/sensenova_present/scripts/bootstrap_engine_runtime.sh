#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PPTAGENT_ENGINE_PYTHON:-/usr/bin/python3}"
SITE_PACKAGES="${PPTAGENT_ENGINE_SITE_PACKAGES:-$ROOT/studio/data/engine-runtime/site-packages}"
REQUIREMENTS="$ROOT/scripts/engine-runtime-requirements.txt"

mkdir -p "$SITE_PACKAGES"

if PYTHONPATH="$SITE_PACKAGES${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON" - <<'PY'
import importlib.metadata

expected = {
    "anthropic": "0.120.0",
    "requests": "2.31.0",
    "playwright": "1.61.0",
    "Pillow": "12.3.0",
    "python-dotenv": "1.2.2",
    "PyYAML": "6.0.1",
    "beautifulsoup4": "4.12.3",
}
for name, version in expected.items():
    if importlib.metadata.version(name) != version:
        raise SystemExit(1)
import anthropic
import requests
import playwright
from PIL import Image
PY
then
  echo "engine runtime already present: $SITE_PACKAGES"
  exit 0
fi

echo "installing engine runtime into $SITE_PACKAGES"
"$PYTHON" -m pip install \
  --disable-pip-version-check \
  --upgrade \
  --target "$SITE_PACKAGES" \
  -r "$REQUIREMENTS"

PYTHONPATH="$SITE_PACKAGES${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON" -c \
  'import anthropic, requests, playwright; from PIL import Image; print("engine runtime ready")'
