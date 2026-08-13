import ast
import json
import os
import unittest
import urllib.request
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class ReleaseWebUIParityTests(unittest.TestCase):
    def test_provisional_player_wraps_complete_slide_documents(self):
        main = (ROOT / "studio/app/main.py").read_text(encoding="utf-8")
        self.assertIn(
            "iframe.provisional-slide[data-slide]",
            main,
        )
        self.assertIn(
            'src="slides/{filename}"',
            main,
        )
        self.assertIn(
            "const fontsReady = Promise.all(slides.map(waitForFrame));",
            main,
        )
        self.assertNotIn(
            "document.querySelectorAll('.slide[data-slide]')",
            main,
        )

    def test_deployment_think_off_reaches_openai_compatible_chat_api(self):
        engine = (ROOT / "studio/app/engine.py").read_text(encoding="utf-8")
        self.assertIn('"chat_template_kwargs" if transport == "openai"', engine)
        self.assertIn('"SENSENOVA_MODEL_THINKING_TRANSPORT",\n                "chat_template_kwargs"', engine)

        source = (ROOT / "inference/serve_one.py").read_text(encoding="utf-8")
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
                "STUDIO_THINKING_TRANSPORT": "openai",
                "STUDIO_EFFECTIVE_THINKING": "0",
            }, clear=False):
                namespace["_install_thinking_transport"]()
            request = urllib.request.Request(
                "http://model.test/v1/chat/completions",
                data=b'{"model":"demo"}',
            )
            self.assertEqual(
                json.loads(request.data)["chat_template_kwargs"],
                {"enable_thinking": False},
            )
        finally:
            urllib.request.Request = original

    def test_preview_image_reveal_keeps_the_canvas_full_bleed(self):
        css = (ROOT / "studio/static/app.css").read_text(encoding="utf-8")
        self.assertIn(
            "from { opacity: 0; filter: saturate(.4) brightness(1.15); }",
            css,
        )
        self.assertNotIn(
            "from { opacity: 0; transform: scale(.96);",
            css,
        )

    def test_process_feed_keeps_local_deployment_autofollow_and_times(self):
        app = (ROOT / "studio/static/app.js").read_text(encoding="utf-8")
        for marker in (
            "function orchestrationEventTime(event)",
            "function orchestrationOutputTimeMarkup(value, label = \"记录\")",
            "function orchestrationTimingMarkup()",
            "const wasNearBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 120",
            "const shouldFollow = wasNearBottom || (firstFlowRender && running)",
            "top: shouldFollow ? scroller.scrollHeight : previousTop",
        ):
            self.assertIn(marker, app)

    def test_process_feed_keeps_terminal_states_and_revision_scoping(self):
        app = (ROOT / "studio/static/app.js").read_text(encoding="utf-8")
        trace = (ROOT / "studio/app/trace.py").read_text(encoding="utf-8")
        main = (ROOT / "studio/app/main.py").read_text(encoding="utf-8")
        css = (ROOT / "studio/static/app.css").read_text(encoding="utf-8")

        self.assertIn(
            '["completed", "failed", "rejected", "interrupted", "not_started"]',
            app,
        )
        self.assertIn("function renderStatus(p, phase)", app)
        self.assertIn("def _inherit_live_event_positions", trace)
        self.assertIn("def _attach_absolute_event_times", trace)
        self.assertIn('event["ts"] = _trace_iso(started_at + elapsed_s)', trace)
        self.assertGreaterEqual(main.count("trace.scoped_specialist_artifacts("), 3)
        self.assertIn(".orchestration-timing-card", css)
        self.assertIn(".orch-output-time", css)

    def test_public_release_exposes_stable_and_creative_mural_profiles(self):
        engine = (ROOT / "studio/app/engine.py").read_text(encoding="utf-8")
        app = (ROOT / "studio/static/app.js").read_text(encoding="utf-8")
        self.assertIn(
            '_DEFAULT_PUBLIC_SKILL_KEYS = ("mural-presenter", "mural-presenter-creative")',
            engine,
        )
        self.assertIn('"mural-presenter": "MURAL Presenter"', app)
        self.assertIn(
            '"mural-presenter-creative": "MURAL Presenter · Creative"', app
        )
        self.assertNotIn('"long-horizon-presenter": "Long-Horizon Presenter"', app)

    def test_mural_presenter_registry_requires_bilingual_auto_routing(self):
        engine = (ROOT / "studio/app/engine.py").read_text(encoding="utf-8")
        start = engine.index("def _mural_presenter_skill(")
        end = engine.index("\ndef _mural_next_skill(", start)
        mural = engine[start:end]
        for required in (
            '"SKILL.en.md"',
            '"subagents/research.en.md"',
            '"subagents/material.en.md"',
            '"subagents/image.en.md"',
            '"subagents/slide.en.md"',
            '"subagents/review.en.md"',
            '"supported_languages": ["zh", "en"]',
            '"agent_language_routing": "query-auto"',
        ):
            self.assertIn(required, mural)

    def test_mural_v01_registry_pairs_two_skill_paths_with_its_harness(self):
        engine = (ROOT / "studio/app/engine.py").read_text(encoding="utf-8")
        start = engine.index("def _mural_v01_skill(")
        end = engine.index("\ndef _mural_next_skill(", start)
        mural = engine[start:end]
        for required in (
            '"zh": "mural-presenter-v0-1-zh"',
            '"en": "mural-presenter-v0-1-en"',
            '"agent_language_routing": "first-skill-read-lock"',
            '"CLEAN_MODEL_SELECT_SKILL": "1"',
            '"pipeline": "mural-presenter-v0.1-harness"',
        ):
            self.assertIn(required, mural)

        app = (ROOT / "studio/static/app.js").read_text(encoding="utf-8")
        self.assertIn(
            '"mural-presenter-v0.1": "MURAL Presenter v0.1"',
            app,
        )

    def test_mural_v01_openai_backend_does_not_require_anthropic_key(self):
        run_batch = (
            ROOT.parents[0]
            / "harnesses/mural-presenter-v0.1/core/run_batch.py"
        ).read_text(encoding="utf-8")
        self.assertGreaterEqual(
            run_batch.count(
                'os.environ.get("MODEL_BACKEND", "").strip().lower() != "openai"'
            ),
            2,
        )

        serve_one = (ROOT / "inference/serve_one.py").read_text(encoding="utf-8")
        self.assertIn(
            "model_call._client = lambda _base_url=None: shim",
            serve_one,
        )


if __name__ == "__main__":
    unittest.main()
