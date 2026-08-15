"""Shared public-CLI error boundary for Role-specific thin entry points."""

from __future__ import annotations

from collections.abc import Callable


def run_expected(action: Callable[[], object]) -> int:
    """Run one deterministic action with concise contract-failure feedback.

    Missing inputs, malformed text and validation failures are normal Agent
    repair signals. Unexpected implementation/runtime exceptions intentionally
    propagate with a traceback so engineering failures are not disguised as a
    fixable deck contract.
    """
    try:
        action()
    except (FileNotFoundError, UnicodeError, ValueError) as exc:
        print("status:FAIL")
        print(str(exc))
        return 2
    return 0


__all__ = ["run_expected"]
