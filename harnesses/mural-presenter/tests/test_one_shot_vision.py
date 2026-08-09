import json
from pathlib import Path
from types import SimpleNamespace

from core import one_shot_vision
from core.trace import Trace


class _Response:
    status_code = 200
    text = ""

    @staticmethod
    def json():
        return {
            "choices": [{"message": {"content": "像素证据：标题清晰，无裁切。"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8},
        }


def test_one_shot_vision_is_exactly_one_request(monkeypatch, tmp_path):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _Response()

    monkeypatch.setenv("VISION_BACKEND", "one_shot")
    monkeypatch.setenv("VISION_ONESHOT_API_KEY", "test-key")
    monkeypatch.setenv("VISION_ONESHOT_BASE_URL", "https://example.test")
    monkeypatch.setenv("VISION_ONESHOT_MODEL", "gemini-3.5-flash")
    monkeypatch.setattr(one_shot_vision.requests, "post", fake_post)
    trace = Trace(str(tmp_path / "_trace" / "slide_01"))
    agent = SimpleNamespace(trace=trace, prompt_language="zh")

    result = one_shot_vision.call(
        agent,
        image_bytes=b"not-a-real-png-but-stable-test-bytes",
        media_type="image/png",
        source_path="renders/slide_01.png",
        question="检查裁切",
        parent_tool_use_id="tool-vision-1",
    )

    assert result.startswith("像素证据")
    assert len(calls) == 1
    url, kwargs = calls[0]
    assert url == "https://example.test/v1/chat/completions"
    assert kwargs["json"]["model"] == "gemini-3.5-flash"
    assert "tools" not in kwargs["json"]
    assert "thinking" not in kwargs["json"]
    assert kwargs["timeout"] is None
    ledger = Path(trace.sub_dir) / "aux_calls" / "vision-one-shot.jsonl"
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["request_count"] == 1
    assert rows[0]["status"] == "completed"
    assert rows[0]["shot"] == "images/view_01.png"
    assert "test-key" not in ledger.read_text(encoding="utf-8")
    status = trace.write([], [], "synthesis")
    assert status["image_count"] == 1
    manifest = json.loads(
        (Path(trace.sub_dir) / "multimodal-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["images"][0]["placement"] == "auxiliary_vision_request"
    assert manifest["images"][0]["tool_use_id"] == "tool-vision-1"


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("VISION_BACKEND", raising=False)
    assert one_shot_vision.enabled() is False


def test_aliases_enable_the_same_transport(monkeypatch):
    for value in ("one_shot", "oneshot", "internal_one_shot"):
        monkeypatch.setenv("VISION_BACKEND", value)
        assert one_shot_vision.enabled() is True
