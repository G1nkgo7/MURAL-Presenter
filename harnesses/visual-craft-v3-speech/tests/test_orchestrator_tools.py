import os
import unittest
from unittest import mock

import distill_ppt
from core import tools


class OrchestratorToolSurfaceTest(unittest.TestCase):
    def test_uses_standard_terminal_without_custom_sync_tools(self):
        names = {
            schema["name"]
            for schema in tools.resolve_toolsets(distill_ppt.ORCHESTRATOR_TOOLSETS)
        }

        self.assertIn("write_file", names)
        self.assertIn("patch", names)
        self.assertIn("terminal", names)
        self.assertIn("delegate_task", names)
        self.assertNotIn("sync_speech", names)
        self.assertNotIn("build_player", names)

    def test_anthropic_key_gate_is_backend_specific(self):
        with mock.patch.dict(os.environ, {"MODEL_BACKEND": "openai"}, clear=True):
            self.assertFalse(distill_ppt._requires_anthropic_key())
        with mock.patch.dict(os.environ, {"MODEL_BACKEND": "anthropic"}, clear=True):
            self.assertTrue(distill_ppt._requires_anthropic_key())
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(distill_ppt._requires_anthropic_key())


if __name__ == "__main__":
    unittest.main()
