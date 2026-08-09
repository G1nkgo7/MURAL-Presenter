import ast
import json
import os
from pathlib import Path
import unittest
import urllib.request
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]


class ThinkingToggleContractTests(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_composer_exposes_optional_think_switch(self):
        template = self.read("studio/templates/app.html")
        script = self.read("studio/static/app.js")
        self.assertIn('id="thinking-toggle"', template)
        self.assertIn('role="checkbox" aria-checked="true"', template)
        self.assertIn('<span class="thinking-check"', template)
        self.assertIn('<span>深度思考</span>', template)
        self.assertNotIn('id="thinking-current"', template)
        self.assertIn('id="thinking-sel" type="checkbox" checked', template)
        self.assertIn('body.set("thinking", thinkingEnabled() ? "1" : "0")', script)
        self.assertIn("thinking: thinkingEnabled()", script)
        self.assertIn('savedThinkingPreference === null ? true', script)
        self.assertIn('data-thinking-toggle="{{ thinking_toggle }}"', template)
        self.assertIn('control.hidden = !supported', script)
        self.assertIn('return modelSupportsThinking()', script)

    def test_static_jobs_persist_and_transport_choice(self):
        main = self.read("studio/app/main.py")
        jobs = self.read("studio/app/jobs.py")
        serve = self.read("distillation/serve_one.py")
        self.assertIn('"thinking": bool(thinking)', main)
        self.assertIn('thinking: int = Form(1)', main)
        self.assertIn('STUDIO_ENABLE_THINKING', jobs)
        self.assertIn('STUDIO_REQUESTED_THINKING', jobs)
        self.assertIn('STUDIO_EFFECTIVE_THINKING', jobs)
        self.assertIn('payload["chat_template_kwargs"] = {"enable_thinking": enabled}', serve)

    def test_thinking_transport_preserves_request_class_contract(self):
        """The adapter must remain subclassable by httpx during import."""
        source = self.read("distillation/serve_one.py")
        tree = ast.parse(source)
        installer = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_install_thinking_transport"
        )
        namespace = {"json": json, "os": os, "urllib": urllib}
        exec(compile(ast.Module(body=[installer], type_ignores=[]), "serve_one.py", "exec"), namespace)

        original = urllib.request.Request
        try:
            with mock.patch.dict(os.environ, {
                "STUDIO_THINKING_TRANSPORT": "chat_template_kwargs",
                "STUDIO_ENABLE_THINKING": "0",
            }, clear=False):
                namespace["_install_thinking_transport"]()

            class HttpxCompatibleRequest(urllib.request.Request):
                pass

            request = HttpxCompatibleRequest(
                "http://example.test/v1/chat/completions",
                data=b'{"model":"demo"}',
            )
            self.assertEqual(
                json.loads(request.data)["chat_template_kwargs"],
                {"enable_thinking": False},
            )
        finally:
            urllib.request.Request = original

    def test_dynamic_jobs_send_explicit_chat_template_choice(self):
        adapter = self.read("studio/app/dynamic.py")
        runtime = self.read("dynamic/viz/agent_runtime.py")
        self.assertIn('thinking_transport=thinking_transport', adapter)
        self.assertIn('"requested_thinking": requested_thinking', adapter)
        self.assertIn('"effective_thinking": effective_thinking', adapter)
        self.assertIn('body["chat_template_kwargs"] = {"enable_thinking": bool(thinking)}', runtime)

    def test_generation_preferences_reach_every_runtime_trace(self):
        standard = self.read("distillation/sense_present_v2.py")
        standard_agent = self.read("distillation/agent_loop.py")
        dynamic = self.read("studio/app/dynamic.py")
        dynamic_runtime = self.read("dynamic/viz/agent_runtime.py")
        self.assertIn('run_config["_generation_preferences"]', standard)
        self.assertIn('"generation_preferences": self.generation_preferences', standard_agent)
        self.assertIn('generation_preferences=generation_preferences', dynamic)
        self.assertIn('"generation_preferences": generation_preferences', dynamic_runtime)
        self.assertIn('_write_runtime_config(conv_id', dynamic_runtime)

    def test_runtime_preferences_do_not_rewrite_raw_dynamic_user_message(self):
        adapter = self.read("studio/app/dynamic.py")
        runtime = self.read("dynamic/viz/agent_runtime.py")
        self.assertNotIn('message += "\\n\\n本次演示偏好', adapter)
        self.assertIn('messages = [{"role": "user", "content": message}]', runtime)
        self.assertIn('HARNESS_SYSTEM_DIRECTIVE', runtime)

    def test_attachment_contract_does_not_rewrite_static_user_query(self):
        main = self.read("studio/app/main.py")
        self.assertNotIn('seed_store["query"] = query + block', main)
        self.assertIn('seed_store["attachment_mode"] = mode', main)
        self.assertIn('seed_store["attachments"] = records', main)

    def test_process_configuration_reports_choice(self):
        template = self.read("studio/templates/app.html")
        script = self.read("studio/static/app.js")
        self.assertIn('id="pr-config-thinking"', template)
        self.assertIn('id="outline-config-thinking"', script)
        self.assertIn('深度思考：${thinking', script)

    def test_model_registry_is_the_thinking_capability_source(self):
        sys_path = str(ROOT / "studio")
        import sys
        if sys_path not in sys.path:
            sys.path.insert(0, sys_path)
        from app import engine

        supported = engine.resolve_thinking("sensenova-flash-lite-v39", True)
        self.assertTrue(supported["requested"])
        self.assertTrue(supported["effective"])
        self.assertEqual(supported["transport"], "chat_template_kwargs")
        disabled = engine.resolve_thinking("sensenova-flash-lite-v39", False)
        self.assertFalse(disabled["effective"])
        qwen_enabled = engine.resolve_thinking("pptagent-qwen35-27b-ckpt1764", True)
        self.assertTrue(qwen_enabled["requested"])
        self.assertTrue(qwen_enabled["effective"])
        self.assertTrue(qwen_enabled["toggle"])
        self.assertEqual(qwen_enabled["transport"], "chat_template_kwargs")
        qwen_disabled = engine.resolve_thinking("pptagent-qwen35-27b-ckpt1764", False)
        self.assertFalse(qwen_disabled["effective"])
        qwen_env = engine.selection_env(
            "pptagent-qwen35-27b-ckpt1764",
            "mural-presenter-harness",
            "mural-presenter",
        )
        self.assertEqual(
            qwen_env["STUDIO_THINKING_TRANSPORT"],
            "chat_template_kwargs",
        )


if __name__ == "__main__":
    unittest.main()
