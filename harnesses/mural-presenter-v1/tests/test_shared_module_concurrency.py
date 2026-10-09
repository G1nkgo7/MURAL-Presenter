"""Regression: concurrent load_skill_module must not expose a half-initialized module.

Batch mode runs many deck workers as threads in one process, all sharing
``sys.modules``. Before the load lock, a second thread could observe a pinned
Skill module that had been inserted into ``sys.modules`` but whose
``exec_module`` had not finished — surfacing as e.g.
``font_bundle has no attribute FAMILY_FACES``. This test reproduces the race
by loading the same module from many threads at once and asserting every caller
gets the same, fully-initialized object.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SKILL = REPO / "skills/mural-presenter-v1"

from runtime.shared_objects import load_skill_module  # noqa: E402


def _clear_pinned(tree_hash: str) -> None:
    prefix = f"_mural_skill_{tree_hash}"
    for name in [n for n in sys.modules if n == prefix or n.startswith(prefix + ".")]:
        sys.modules.pop(name, None)


def test_concurrent_load_returns_fully_initialized_module():
    tree_hash = "0" * 64  # synthetic identity; only used as a sys.modules namespace key
    _clear_pinned(tree_hash)
    try:
        results: list[object] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(16)

        def worker() -> None:
            try:
                barrier.wait()
                module = load_skill_module(SKILL, tree_hash, "font_bundle")
                # The attribute that was missing under the race.
                assert hasattr(module, "FAMILY_FACES"), "module observed half-initialized"
                results.append(module)
            except BaseException as exc:  # noqa: BLE001 - surface any race failure
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not errors, f"concurrent load raised: {errors[0]!r}"
        assert len(results) == 16
        # All callers must get the identical cached object, not distinct reloads.
        assert all(module is results[0] for module in results)
        assert getattr(results[0], "__mural_loaded__", False) is True
    finally:
        _clear_pinned(tree_hash)
