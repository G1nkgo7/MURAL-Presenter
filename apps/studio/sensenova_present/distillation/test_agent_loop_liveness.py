import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import agent_loop


class _ToolUse:
    def __init__(self, name="read", payload=None, tool_id="tool-1"):
        self.type = "tool_use"
        self.name = name
        self.input = payload or {"path": "plan/deck.md"}
        self.id = tool_id


class AgentLoopLivenessTest(unittest.TestCase):
    def test_standard_profile_restores_role_names_and_required_tools(self):
        cases = (
            ("[image_02] read subagents/image.md", "image_02", {"file", "terminal", "web", "image_gen", "vision"}),
            ("[slide_07] finish page 7", "slide_07", {"file", "terminal", "vision"}),
            ("[review] inspect the deck", "review", {"vision"}),
        )
        for goal, label, required in cases:
            with self.subTest(label=label):
                task = agent_loop._normalize_task(
                    {"goal": goal, "toolsets": ["file"]},
                    "sense-present-standard",
                )
                self.assertEqual(task["label"], label)
                self.assertTrue(required.issubset(set(task["toolsets"])))

    def test_standard_review_requires_visible_per_page_vision_results(self):
        task = agent_loop._normalize_task(
            {"goal": "[review] inspect the deck", "toolsets": ["vision"]},
            "sense-present-standard",
        )
        self.assertIn("slide_NN: <检查结果>", task["goal"])
        self.assertIn("不得连续发起下一批 Vision", task["goal"])

    def test_progress_guard_stops_identical_no_progress_turns(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            (root / "plan" / "deck.md").write_text("stable", encoding="utf-8")
            guard = agent_loop._ProgressGuard(temporary)
            use = _ToolUse()
            result = {
                "type": "tool_result",
                "tool_use_id": use.id,
                "content": "same result",
            }
            action = "ok"
            for _ in range(agent_loop.STALL_IDENTICAL_TURNS):
                action, _reason = guard.observe([use], [result])
            self.assertEqual(action, "stop")

    def test_delegate_retries_failed_child_once(self):
        parent = SimpleNamespace(
            _delegate_depth=0,
            _spawn_lock=threading.Lock(),
            worker_recs=[],
            log=lambda _message: None,
        )
        attempts = []

        def fake_run_child(parent_obj, task, ticket):
            attempt = len(attempts) + 1
            attempts.append(task)
            label = "demo" if attempt == 1 else "demo_r2"
            clean = attempt == 2
            parent_obj.worker_recs.append({"label": label, "clean": clean})
            return {
                "label": label,
                "status": "ok" if clean else "issues",
                "renders": 0,
                "shot": None,
                "summary": "done" if clean else "stalled",
                "summary_path": None,
                "summary_chars": 4 if clean else 7,
                "summary_truncated": False,
                "artifacts": [],
            }

        with mock.patch.object(agent_loop, "_run_child", side_effect=fake_run_child):
            payload = json.loads(
                agent_loop.delegate_task(
                    parent,
                    tasks=[{
                        "goal": "complete one task",
                        "label": "demo",
                        "toolsets": ["file"],
                    }],
                )
            )

        self.assertEqual(len(attempts), 2)
        self.assertEqual(payload["results"][0]["status"], "ok")
        self.assertEqual(payload["results"][0]["attempt"], 2)
        self.assertTrue(parent.worker_recs[0]["superseded"])
        self.assertFalse(parent.worker_recs[1].get("superseded", False))


if __name__ == "__main__":
    unittest.main()
