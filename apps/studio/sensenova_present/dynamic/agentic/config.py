"""Self-contained configuration for the vendored dynamic Studio runtime."""
from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

SKILLS_DIR = Path(os.environ.get("AGENTIC_SKILLS_DIR", ROOT / "skills"))
RENDER_SCRIPT_REL = "skills/dazzle-deck/scripts/render_deck.py"
BASH_TIMEOUT_S = int(os.environ.get("AGENTIC_BASH_TIMEOUT", "180"))
MAX_VISION_EDGE = int(os.environ.get("MAX_VISION_EDGE", "1280"))
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://tokenhub.sensetime.com/v1")
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "gpt-image-2")
