import importlib.util
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from studio.app import custom_models


ROOT = Path(__file__).resolve().parents[1]
BACKENDS = [ROOT.parent / "harnesses/mural-presenter/core/openai_backend.py"]
EXPERIMENTAL_BACKEND = (
    ROOT.parent / "research/mural-next/harnesses/mural-presenter/core/openai_backend.py"
)
if EXPERIMENTAL_BACKEND.is_file():
    BACKENDS.append(EXPERIMENTAL_BACKEND)


def load_backend(path: Path, suffix: str):
    spec = importlib.util.spec_from_file_location(f"deepseek_backend_{suffix}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Response:
    def read(self):
        return json.dumps({
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {},
        }).encode()


class DeepSeekThinkingTests(unittest.TestCase):
    def test_official_v4_custom_models_expose_toggle_high(self):
        row = {
            "model_id": "deepseek-v4-pro",
            "base_url": "https://api.deepseek.com",
            "vision_enabled": 1,
        }
        self.assertEqual(custom_models.thinking_capability(row), {
            "mode": "toggle",
            "transport": "deepseek",
            "toggle": True,
            "effort": "high",
        })
        self.assertFalse(custom_models.vision_capability(row))
        self.assertFalse(custom_models.public_payload({
            **row,
            "id": 1,
            "display_name": "DeepSeek V4 Pro",
            "api_key_enc": "encrypted",
        })["multimodal"])

    def test_available_harnesses_send_official_deepseek_thinking_fields(self):
        for index, path in enumerate(BACKENDS):
            with self.subTest(path=path):
                module = load_backend(path, str(index))
                captured = {}

                def fake_urlopen(request, timeout=None):
                    captured.update(json.loads(request.data))
                    return _Response()

                env = {
                    "STUDIO_THINKING_TRANSPORT": "deepseek",
                    "STUDIO_EFFECTIVE_THINKING": "1",
                    "THINK_EFFORT": "high",
                    "STUDENT_TEMPERATURE": "0.3",
                }
                with (
                    mock.patch.dict(os.environ, env, clear=False),
                    mock.patch.object(module.urllib.request, "urlopen", fake_urlopen),
                ):
                    module.OpenAIShim(
                        "https://api.deepseek.com", "deepseek-v4-flash", "secret"
                    ).messages.create(messages=[], max_tokens=40960)

                self.assertEqual(captured["thinking"], {"type": "enabled"})
                self.assertEqual(captured["reasoning_effort"], "high")
                self.assertEqual(captured["max_tokens"], 40960)
                self.assertEqual(captured["temperature"], 0.3)

    def test_available_harnesses_send_disabled_without_effort(self):
        for index, path in enumerate(BACKENDS):
            with self.subTest(path=path):
                module = load_backend(path, f"disabled_{index}")
                captured = {}

                def fake_urlopen(request, timeout=None):
                    captured.update(json.loads(request.data))
                    return _Response()

                env = {
                    "STUDIO_THINKING_TRANSPORT": "deepseek",
                    "STUDIO_EFFECTIVE_THINKING": "0",
                    "THINK_EFFORT": "high",
                    "STUDENT_TEMPERATURE": "0.3",
                }
                with (
                    mock.patch.dict(os.environ, env, clear=False),
                    mock.patch.object(module.urllib.request, "urlopen", fake_urlopen),
                ):
                    module.OpenAIShim(
                        "https://api.deepseek.com", "deepseek-v4-pro", "secret"
                    ).messages.create(messages=[], max_tokens=40960)

                self.assertEqual(captured["thinking"], {"type": "disabled"})
                self.assertNotIn("reasoning_effort", captured)


if __name__ == "__main__":
    unittest.main()
