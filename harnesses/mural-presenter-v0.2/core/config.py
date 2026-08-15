"""Environment-driven configuration for the standalone Clean harness."""
from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _load_early_dotenv() -> None:
    """Load project configuration before module constants are evaluated."""
    path = Path(os.environ.get("CLEAN_DOTENV_PATH", ROOT / ".env"))
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


_load_early_dotenv()

ARTIFACT_ROOT = Path(os.environ.get("CLEAN_ARTIFACT_ROOT", ROOT))
RUNS_DIR = Path(os.environ.get("CLEAN_RUNS_DIR", ARTIFACT_ROOT / "runs"))
LOGS_DIR = Path(os.environ.get("CLEAN_LOGS_DIR", ARTIFACT_ROOT / "logs"))
# This frozen bilingual Harness lives in ``harnesses/mural-presenter-v0.2``;
# its two instruction editions live in ``skills/mural-presenter-v0.2``.
# Keep CLEAN_SKILLS_DIR as an explicit deployment override for packaged builds.
DEFAULT_SKILLS_DIR = ROOT.parents[1] / "skills" / "mural-presenter-v0.2"
SKILLS_DIR = Path(os.environ.get("CLEAN_SKILLS_DIR", DEFAULT_SKILLS_DIR))
SKILL_NAME = os.environ.get("CLEAN_SKILL_NAME", "mural-presenter-v0-2")
SKILL_NAME_ZH = os.environ.get("CLEAN_SKILL_NAME_ZH", f"{SKILL_NAME}-zh")
SKILL_NAME_EN = os.environ.get("CLEAN_SKILL_NAME_EN", f"{SKILL_NAME}-en")

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-7-thinking")
NOVA_RAW_V2 = os.environ.get("CLEAN_NOVA_RAW_V2", "0") == "1"
# Frozen Gate compatibility profile from nova_vision_demo tag
# ``sensenova-harness-vision-v1.1-20260815``.  The legacy exact-raw profile
# remains the default; only the mainline launcher enables this explicitly.
NOVA_GATE_V1 = os.environ.get("CLEAN_NOVA_GATE_V1", "0") == "1"
NOVA_GATE_AGENT_MODEL = os.environ.get(
    "CLEAN_NOVA_GATE_AGENT_MODEL", "claude-opus-5"
).strip() or "claude-opus-5"
NOVA_PROXY_BASE_URL = os.environ.get("NOVA_PROXY_BASE_URL", "").strip().rstrip("/")
NOVA_VISION_PROXY_BASE_URL = os.environ.get(
    "NOVA_VISION_PROXY_BASE_URL", NOVA_PROXY_BASE_URL
).strip().rstrip("/")
ANTHROPIC_BASE_URL = (
    NOVA_PROXY_BASE_URL
    if NOVA_RAW_V2 and NOVA_PROXY_BASE_URL
    else os.environ.get("ANTHROPIC_BASE_URL", "https://tokenhub.sensetime.com")
)
NOVA_RAW_ROOT = Path(
    os.environ.get("CLEAN_NOVA_RAW_ROOT", ARTIFACT_ROOT / "nova_raw")
)
NOVA_HEALTH_TIMEOUT_S = int(os.environ.get("NOVA_HEALTH_TIMEOUT", "10"))
MODEL_TIMEOUT_S = int(os.environ.get("CLEAN_MODEL_TIMEOUT", "600"))
# Workspace preparation/audit touches the mounted delivery filesystem before
# and after model work.  AFS metadata/atomic-rename latency can occasionally
# exceed one minute even for small files, so this guard must tolerate storage
# stalls instead of rejecting an otherwise healthy Deck before its first turn.
WORKSPACE_IO_TIMEOUT_S = max(
    60, int(os.environ.get("CLEAN_WORKSPACE_IO_TIMEOUT", "300"))
)
# Response timing is a Harness liveness concern, not an Agent quality budget.
# Default every turn to the complete model timeout; deployments may override it
# for transport operations, but a timeout only retries the same request.
FIRST_RESPONSE_TIMEOUT_S = int(
    os.environ.get("CLEAN_FIRST_RESPONSE_TIMEOUT", str(MODEL_TIMEOUT_S))
)
ACTIVE_RESPONSE_TIMEOUT_S = int(
    os.environ.get("CLEAN_ACTIVE_RESPONSE_TIMEOUT", str(MODEL_TIMEOUT_S))
)

# Deliberately high: the shared deck liveness boundary, not an arbitrary turn
# budget, should be the practical stop condition for healthy work.
MAX_TURNS = int(os.environ.get("CLEAN_MAX_TURNS", "240"))
CHILD_MAX_TURNS = int(os.environ.get("CLEAN_CHILD_MAX_TURNS", "80"))
PER_TURN_MAX_TOKENS = int(os.environ.get("CLEAN_MAX_TOKENS", "40960"))
THINKING = os.environ.get("CLEAN_THINKING", "0") == "1"
# CLEAN_EFFORT is canonical. THINK_EFFORT remains a compatibility alias for
# the shared inference .env used by earlier Harnesses.
THINK_EFFORT = (
    os.environ.get("CLEAN_EFFORT")
    or os.environ.get("THINK_EFFORT")
    or "medium"
)
MAX_HEALS = max(1, int(os.environ.get("CLEAN_MAX_HEALS", "4")))
# Harness-only last-resort liveness boundary. It does not alter the workflow,
# reduce quality, skip a role, or ask an Agent to rush. Long production decks
# get a full day by default; capacity tuning must not reject otherwise healthy
# work. There is no separate Slide deadline.
DECK_TIMEOUT_S = int(os.environ.get("CLEAN_DECK_TIMEOUT", "86400"))
CHILD_WALL_TIMEOUT_S = int(os.environ.get("CLEAN_CHILD_WALL_TIMEOUT", "2400"))
REVIEW_MAX_ATTEMPTS = max(
    1, int(os.environ.get("CLEAN_REVIEW_MAX_ATTEMPTS", "2"))
)

# Generic liveness guards. These do not constrain PPT content or design; they
# only stop a role that is demonstrably repeating the same action/result or the
# same unrecoverable error without making workspace progress.
STALL_IDENTICAL_TURNS = max(
    2, int(os.environ.get("CLEAN_STALL_IDENTICAL_TURNS", "4"))
)
STALL_ACTION_REPEATS = max(
    STALL_IDENTICAL_TURNS,
    int(os.environ.get("CLEAN_STALL_ACTION_REPEATS", "12")),
)
STALL_ERROR_TURNS = max(
    2, int(os.environ.get("CLEAN_STALL_ERROR_TURNS", "4"))
)
STALL_NO_PROGRESS_TURNS = max(
    4, int(os.environ.get("CLEAN_STALL_NO_PROGRESS_TURNS", "5"))
)

# Keep full text in the on-disk trace, but replace old live request history
# with a deterministic capsule once the provider-reported input grows large.
HISTORY_COMPACT_INPUT_TOKENS = max(
    16000,
    int(os.environ.get("CLEAN_HISTORY_COMPACT_INPUT_TOKENS", "80000")),
)
HISTORY_COMPACT_CHARS = max(
    64000,
    int(os.environ.get("CLEAN_HISTORY_COMPACT_CHARS", "320000")),
)
HISTORY_KEEP_RECENT_MESSAGES = max(
    8,
    int(os.environ.get("CLEAN_HISTORY_KEEP_RECENT_MESSAGES", "24")),
)

# A child wave is one synchronous ``delegate_task`` call. The control file is
# reread before each new wave; already-submitted children keep their original
# executor and are never cancelled or resized.
CHILD_CONCURRENCY = max(
    1,
    int(os.environ.get("CLEAN_CHILD_CONCURRENCY", "12")),
)
CHILD_POOL_MAX_WORKERS = max(
    CHILD_CONCURRENCY,
    int(
        os.environ.get(
            "CLEAN_CHILD_POOL_MAX_WORKERS",
            str(CHILD_CONCURRENCY),
        )
    ),
)
CHILD_CONCURRENCY_FILE = os.environ.get(
    "CLEAN_CHILD_CONCURRENCY_FILE",
    "",
).strip()

# Parallel remote calls emitted by one Image/Research response are not child
# agents and therefore do not follow the child-wave control file.
REMOTE_TOOL_CONCURRENCY = max(
    1,
    int(os.environ.get("CLEAN_REMOTE_TOOL_CONCURRENCY", "4")),
)

ENABLE_IMAGE_GEN = os.environ.get("ENABLE_IMAGE_GEN", "1") != "0"
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://tokenhub.sensetime.com/v1")
IMAGE_BASE_URL = os.environ.get("IMAGE_BASE_URL", OPENAI_BASE_URL)
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "gpt-image-2-pro-all")
BASH_TIMEOUT_S = int(os.environ.get("CLEAN_BASH_TIMEOUT", "300"))
MAX_VISION_EDGE = int(os.environ.get("MAX_VISION_EDGE", "1600"))
# v0.2 deliberately keeps pixel inspection on the same model as the acting
# Agent.  The shared WebUI environment historically uses ``one_shot`` for the
# older MURAL harness, but this clean harness must not silently imply that an
# auxiliary model is active.  Treat those shared-environment aliases as an
# explicit same-model/native request and expose both values in the trace.
VISION_BACKEND_REQUESTED = os.environ.get("VISION_BACKEND", "native").strip().lower()
VISION_BACKEND = (
    "native"
    if VISION_BACKEND_REQUESTED in {
        "native",
        "same_model",
        "same-model",
        "one_shot",
        "oneshot",
        "internal_one_shot",
    }
    else VISION_BACKEND_REQUESTED
)
VISION_GEMINI_MODEL = os.environ.get(
    "VISION_GEMINI_MODEL",
    "gemini-3.5-flash",
)
VISION_GEMINI_BASE_URL = os.environ.get(
    "VISION_GEMINI_BASE_URL",
    "https://tokenhub.sensetime.com/v1",
).rstrip("/")
VISION_GEMINI_TIMEOUT_S = int(os.environ.get("VISION_GEMINI_TIMEOUT", "120"))
VISION_GEMINI_MAX_TOKENS = int(
    os.environ.get("VISION_GEMINI_MAX_TOKENS", "8000")
)
VISION_NOVA_TIMEOUT_S = int(os.environ.get("VISION_NOVA_TIMEOUT", "600"))
VISION_NOVA_MAX_TOKENS = int(os.environ.get("VISION_NOVA_MAX_TOKENS", "16000"))

DECK_W, DECK_H = 1600, 900
