import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT.parent
    / "harnesses/mural-presenter/core/one_shot_vision.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("lhp_one_shot_vision_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Response:
    status_code = 200
    text = ""

    @staticmethod
    def json():
        return {
            "choices": [{"message": {"content": "像素证据：标题清晰，无裁切。"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8},
        }


class _TruncatedResponse(_Response):
    @staticmethod
    def json():
        return {
            "choices": [{
                "message": {"content": "未完成的视觉结论"},
                "finish_reason": "length",
            }],
            "usage": {},
        }


class _Trace:
    def __init__(self, root):
        self.sub_dir = str(root)

    def snapshot_image(self, tool_id, raw):
        target = Path(self.sub_dir) / "images" / "view_01.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        return "images/view_01.png"


class LhpVisionProxyTests(unittest.TestCase):
    def test_gemini_receives_image_and_main_model_only_gets_text(self):
        module = load_module()
        captured = {}

        def fake_post(url, **kwargs):
            captured.update({"url": url, **kwargs})
            return _Response()

        env = {
            "VISION_BACKEND": "one_shot",
            "VISION_ONESHOT_API_KEY": "secret",
            "VISION_ONESHOT_BASE_URL": "https://example.test/v1",
            "VISION_ONESHOT_MODEL": "gemini-3.5-flash",
            "VISION_ONESHOT_TIMEOUT": "600",
        }
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, env, clear=False
        ), mock.patch.object(module.requests, "post", fake_post):
            agent = SimpleNamespace(
                trace=_Trace(Path(temporary)), prompt_language="zh"
            )
            result = module.call(
                agent,
                image_bytes=b"stable-image-bytes",
                media_type="image/png",
                source_path="renders/slide_01.png",
                question="检查裁切",
                parent_tool_use_id="vision-1",
            )
            ledger = Path(temporary) / "aux_calls" / "vision-one-shot.jsonl"
            ledger_text = ledger.read_text()
            record = json.loads(ledger_text.splitlines()[0])

        self.assertEqual(result, "像素证据：标题清晰，无裁切。")
        self.assertEqual(captured["url"], "https://example.test/v1/chat/completions")
        self.assertEqual(captured["json"]["model"], "gemini-3.5-flash")
        self.assertEqual(captured["timeout"], 600.0)
        self.assertEqual(captured["json"]["messages"][1]["content"][1]["type"], "image_url")
        self.assertNotIn("tools", captured["json"])
        self.assertEqual(record["status"], "completed")
        self.assertNotIn("secret", ledger_text)

    def test_upstream_length_is_not_accepted_as_complete(self):
        module = load_module()
        env = {
            "VISION_BACKEND": "one_shot",
            "VISION_ONESHOT_API_KEY": "secret",
            "VISION_ONESHOT_BASE_URL": "https://example.test/v1",
            "VISION_ONESHOT_MODEL": "gemini-3.5-flash",
            "VISION_ONESHOT_TIMEOUT": "600",
        }
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, env, clear=False
        ), mock.patch.object(module.requests, "post", return_value=_TruncatedResponse()):
            agent = SimpleNamespace(trace=_Trace(Path(temporary)), prompt_language="zh")
            with self.assertRaisesRegex(RuntimeError, "truncated"):
                module.call(
                    agent,
                    image_bytes=b"stable-image-bytes",
                    media_type="image/png",
                    source_path="renders/slide_01.png",
                    question="检查裁切",
                    parent_tool_use_id="vision-truncated",
                )
            ledger = Path(temporary) / "aux_calls" / "vision-one-shot.jsonl"
            record = json.loads(ledger.read_text().splitlines()[0])
        self.assertEqual(record["status"], "failed")


if __name__ == "__main__":
    unittest.main()
