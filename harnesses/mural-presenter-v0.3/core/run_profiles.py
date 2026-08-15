"""Authoritative execution profiles for delivery and training-data synthesis."""
from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class RunProfile:
    name: str
    release_consumed_images: bool
    compact_active_history: bool
    require_complete_trace: bool


PROFILES = {
    "inference": RunProfile(
        name="inference",
        release_consumed_images=True,
        compact_active_history=True,
        require_complete_trace=False,
    ),
    "synthesis": RunProfile(
        name="synthesis",
        release_consumed_images=False,
        compact_active_history=False,
        require_complete_trace=True,
    ),
}


def resolve_run_profile(value: str | None = None) -> RunProfile:
    """Resolve one explicit profile; inference is the serving-safe default."""
    name = str(value or os.environ.get("MURAL_RUN_MODE") or "inference").strip().lower()
    try:
        return PROFILES[name]
    except KeyError as exc:
        raise ValueError(
            f"unsupported MURAL run mode {name!r}; expected inference or synthesis"
        ) from exc
