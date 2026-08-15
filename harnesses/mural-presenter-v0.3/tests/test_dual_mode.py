from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARNESS))

from core.agent_loop import (
    Agent,
    _compact_live_history_for_profile,
    _messages_for_model,
    _release_consumed_images_for_profile,
)
from core.run_profiles import resolve_run_profile
from core.trace_mode import write_trace


def _history() -> list[dict]:
    messages = [{"role": "user", "content": "start"}]
    for index in range(8):
        messages.extend([
            {"role": "assistant", "content": f"step {index}"},
            {"role": "user", "content": f"result {index}"},
        ])
    return messages


def _image_turn() -> list[dict]:
    return [{
        "role": "user",
        "content": [{
            "type": "tool_result",
            "tool_use_id": "vision-1",
            "content": [
                {"type": "text", "text": "pixels"},
                {"type": "image", "source": {"type": "base64", "data": "AAAA"}},
            ],
        }],
    }]


def test_profiles_match_release_harness_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MURAL_RUN_MODE", raising=False)
    inference = resolve_run_profile()
    synthesis = resolve_run_profile("synthesis")
    assert inference.name == "inference"
    assert inference.release_consumed_images
    assert inference.compact_active_history
    assert not inference.require_complete_trace
    assert synthesis.name == "synthesis"
    assert not synthesis.release_consumed_images
    assert not synthesis.compact_active_history
    assert synthesis.require_complete_trace
    with pytest.raises(ValueError):
        resolve_run_profile("compressed-synthesis")


def test_unsigned_reasoning_is_durable_but_not_replayed() -> None:
    block = SimpleNamespace(
        type="thinking",
        thinking="DeepSeek returned this reasoning_content",
        signature=None,
    )
    messages = [{"role": "assistant", "content": Agent.blocks([block])}]
    replay = _messages_for_model(messages)
    assert messages[0]["content"] == [{
        "type": "thinking",
        "thinking": "DeepSeek returned this reasoning_content",
    }]
    assert replay[0]["content"] == []

    signed = [{
        "role": "assistant",
        "content": [{
            "type": "thinking",
            "thinking": "signed",
            "signature": "sig-1",
        }],
    }]
    assert _messages_for_model(signed) is signed


def test_synthesis_bypasses_both_lossy_context_operations() -> None:
    agent = SimpleNamespace(profile=resolve_run_profile("synthesis"))
    history = _history()
    before = list(history)
    assert not _compact_live_history_for_profile(agent, history, keep_recent=4)
    assert history == before

    image_turn = _image_turn()
    assert not _release_consumed_images_for_profile(agent, image_turn)
    assert image_turn[0]["content"][0]["content"][1]["type"] == "image"


def test_inference_runs_both_context_maintenance_operations() -> None:
    agent = SimpleNamespace(profile=resolve_run_profile("inference"))
    history = _history()
    assert _compact_live_history_for_profile(agent, history, keep_recent=4)
    assert len(history) < 17

    image_turn = _image_turn()
    assert _release_consumed_images_for_profile(agent, image_turn)
    parts = image_turn[0]["content"][0]["content"]
    assert all(part.get("type") != "image" for part in parts)


def test_synthesis_writes_hash_addressed_multimodal_manifest(tmp_path: Path) -> None:
    image_path = tmp_path / "images" / "view_001.png"
    image_path.parent.mkdir()
    image_path.write_bytes(b"exact-pixels")
    messages = [{
        "role": "user",
        "content": [{
            "type": "tool_result",
            "tool_use_id": "vision-1",
            "content": [{"type": "image", "shot": "images/view_001.png"}],
        }],
    }]
    tool_log = [{
        "tool_use_id": "vision-1",
        "snapshot": "images/view_001.png",
        "media_type": "image/png",
    }]
    status = write_trace(
        tmp_path,
        messages,
        tool_log,
        {"vision-1": "images/view_001.png"},
        "synthesis",
    )
    assert status == {
        "mode": "synthesis",
        "complete": True,
        "image_count": 1,
        "missing": [],
    }
    manifest = json.loads(
        (tmp_path / "multimodal-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["schema"] == "mural.multimodal-trace.v1"
    assert manifest["images"][0]["bytes"] == len(b"exact-pixels")
    assert len(manifest["images"][0]["sha256"]) == 64


def test_synthesis_marks_missing_snapshot_incomplete(tmp_path: Path) -> None:
    status = write_trace(
        tmp_path,
        [],
        [],
        {"vision-1": "images/missing.png"},
        "synthesis",
    )
    assert not status["complete"]
    assert status["missing"] == ["images/missing.png"]


def test_inference_does_not_write_synthesis_manifest(tmp_path: Path) -> None:
    status = write_trace(tmp_path, [], [], {}, "inference")
    assert status == {"mode": "inference", "complete": True, "image_count": 0}
    assert not (tmp_path / "multimodal-manifest.json").exists()
