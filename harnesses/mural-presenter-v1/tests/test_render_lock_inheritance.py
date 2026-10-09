"""Borrow a parent-owned render lock without re-locking it on process-bound AFS."""
import fcntl
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def policy():
    source = (Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1"
              / "scripts/_internal/workspace_policy.py")
    spec = importlib.util.spec_from_file_location("render_lock_policy_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_inherited_render_lock_does_not_reacquire_or_unlock(policy, tmp_path, monkeypatch):
    path = tmp_path / "_state/render-page-01.lock"
    path.parent.mkdir()
    with path.open("a+b") as parent:
        fcntl.flock(parent.fileno(), fcntl.LOCK_EX)
        monkeypatch.setenv("MURAL_PAGE_RENDER_LOCK_FD", str(parent.fileno()))

        def forbidden(*args):
            raise AssertionError("a borrowed parent lock must not be re-locked or unlocked")

        monkeypatch.setattr(policy.fcntl, "flock", forbidden)
        with policy.page_render_lock(tmp_path, 1) as descriptor:
            assert descriptor == parent.fileno()
        assert not parent.closed


def test_inherited_render_lock_still_rejects_wrong_page(policy, tmp_path, monkeypatch):
    state = tmp_path / "_state"
    state.mkdir()
    (state / "render-page-02.lock").touch()
    with (state / "render-page-01.lock").open("a+b") as parent:
        monkeypatch.setenv("MURAL_PAGE_RENDER_LOCK_FD", str(parent.fileno()))
        with pytest.raises(ValueError, match="invalid inherited"):
            with policy.page_render_lock(tmp_path, 2):
                pytest.fail("wrong-page descriptor was accepted")
