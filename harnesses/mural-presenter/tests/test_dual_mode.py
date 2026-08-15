import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


HARNESS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARNESS))

from core.run_profiles import resolve_run_profile
from core.trace import Trace
from core import agent as agent_core


class RunProfileTests(unittest.TestCase):
    def test_inference_is_fast_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            profile = resolve_run_profile()
        self.assertEqual("inference", profile.name)
        self.assertTrue(profile.release_consumed_images)
        self.assertTrue(profile.compact_active_history)
        self.assertFalse(profile.require_complete_trace)

    def test_synthesis_disables_lossy_context_maintenance(self):
        profile = resolve_run_profile("synthesis")
        self.assertFalse(profile.release_consumed_images)
        self.assertFalse(profile.compact_active_history)
        self.assertTrue(profile.require_complete_trace)

    def test_environment_selects_profile(self):
        with mock.patch.dict(os.environ, {"MURAL_RUN_MODE": "synthesis"}, clear=True):
            self.assertEqual("synthesis", resolve_run_profile().name)

    def test_invalid_profile_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "inference or synthesis"):
            resolve_run_profile("fastish")


class SynthesisTraceTests(unittest.TestCase):
    def test_unsigned_reasoning_is_preserved_in_messages_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace = Trace(tmp)
            block = SimpleNamespace(
                type="thinking",
                thinking="DeepSeek returned this reasoning_content",
                signature=None,
            )
            messages = [{
                "role": "assistant",
                "content": agent_core.blocks_to_dicts([block]),
            }]
            trace.write(messages, [], "synthesis")
            saved = json.loads(
                (Path(tmp) / "messages.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                saved[0]["content"],
                [{
                    "type": "thinking",
                    "thinking": "DeepSeek returned this reasoning_content",
                }],
            )

    def test_replay_drops_only_unsigned_thinking_without_mutating_trace(self):
        messages = [{
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "unsigned deepseek"},
                {
                    "type": "thinking",
                    "thinking": "signed anthropic",
                    "signature": "sig-1",
                },
                {"type": "text", "text": "progress"},
                {
                    "type": "tool_use",
                    "id": "tool-1",
                    "name": "read_file",
                    "input": {"path": "brief.md"},
                },
            ],
        }]
        replay = agent_core._messages_for_model(messages)
        self.assertEqual(
            [block["type"] for block in replay[0]["content"]],
            ["thinking", "text", "tool_use"],
        )
        self.assertEqual(replay[0]["content"][0]["signature"], "sig-1")
        self.assertEqual(messages[0]["content"][0]["thinking"], "unsigned deepseek")
        self.assertEqual(len(messages[0]["content"]), 4)

    def test_synthesis_writes_hash_verified_image_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace = Trace(tmp)
            shot = trace.snapshot_image("vision-1", b"exact-image-bytes")
            messages = [{
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "vision-1",
                    "content": [{"type": "image", "shot": shot}],
                }],
            }]
            status = trace.write(messages, [], "synthesis")
            self.assertTrue(status["complete"])
            self.assertEqual(1, status["image_count"])
            manifest = json.loads(
                (Path(tmp) / "multimodal-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual("mural.multimodal-trace.v1", manifest["schema"])
            self.assertEqual(64, len(manifest["images"][0]["sha256"]))

    def test_synthesis_fails_manifest_when_snapshot_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace = Trace(tmp)
            trace.shot_by_tcid["vision-1"] = "images/missing.png"
            status = trace.write([], [], "synthesis")
            self.assertFalse(status["complete"])
            self.assertEqual(["images/missing.png"], status["missing"])

    def test_inference_skips_training_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace = Trace(tmp)
            status = trace.write([], [], "inference")
            self.assertTrue(status["complete"])
            self.assertFalse((Path(tmp) / "multimodal-manifest.json").exists())


class RuntimeGateTests(unittest.TestCase):
    class _Trace:
        def __init__(self, root):
            self.sub_dir = root

        def snapshot_inputs(self, *_args):
            return None

        def clean(self, value):
            return value

        def write(self, *_args):
            return {"mode": "test", "complete": True, "image_count": 0}

    class _Agent:
        def __init__(self, root, mode):
            self.role = "orchestrator"
            self.label = "orch"
            self.initial_user = "build"
            self.system = "system"
            self.tools = []
            self.max_turns = 2
            self.prompt_language = "zh"
            self.final_text = ""
            self.exit_reason = None
            self.n_renders = 0
            self.trace = RuntimeGateTests._Trace(root)
            self.profile = resolve_run_profile(mode)
            self.run_mode = mode
            self._usage_acc = {
                "sum_input": 0, "sum_cache_read": 0, "sum_cache_create": 0,
                "sum_output": 0, "n_turns": 0,
            }

        def config_snapshot(self):
            return {}

        def log(self, _value):
            return None

    @staticmethod
    def _responses():
        return iter((
            SimpleNamespace(
                content=[SimpleNamespace(
                    type="tool_use", id="tool-1", name="read_file", input={}
                )],
                stop_reason="tool_use",
            ),
            SimpleNamespace(
                content=[SimpleNamespace(type="text", text="done")],
                stop_reason="end_turn",
            ),
        ))

    def _run_and_count(self, mode):
        with tempfile.TemporaryDirectory() as tmp:
            replies = self._responses()
            with (
                mock.patch.object(agent_core, "_model_call", side_effect=lambda *_: next(replies)),
                mock.patch.object(agent_core, "_run_tools", return_value=[{
                    "type": "tool_result", "tool_use_id": "tool-1", "content": "ok"
                }]),
                mock.patch.object(agent_core, "_release_consumed_vision_images", return_value=0) as release,
                mock.patch.object(agent_core, "_compact_active_history", return_value=0) as compact,
                mock.patch.object(agent_core.nova_bridge, "finalize_agent", return_value=None),
            ):
                self.assertTrue(agent_core.run_loop(self._Agent(tmp, mode)))
            return release.call_count, compact.call_count

    def test_inference_executes_context_maintenance(self):
        release, compact = self._run_and_count("inference")
        self.assertGreater(release, 0)
        self.assertGreater(compact, 0)

    def test_synthesis_bypasses_context_maintenance(self):
        release, compact = self._run_and_count("synthesis")
        self.assertEqual(0, release)
        self.assertEqual(0, compact)


if __name__ == "__main__":
    unittest.main()
