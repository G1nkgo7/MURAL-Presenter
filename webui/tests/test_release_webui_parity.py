import ast
import json
import os
import unittest
import urllib.request
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class ReleaseWebUIParityTests(unittest.TestCase):
    def test_provisional_player_routes_fragments_and_standalone_documents(self):
        main = (ROOT / "studio/app/main.py").read_text(encoding="utf-8")
        self.assertIn(
            "if not any(standalone_documents):",
            main,
        )
        self.assertIn(
            "return _inline_fragment_present_html(",
            main,
        )
        self.assertIn(
            "return _hybrid_fragment_present_html(",
            main,
        )
        self.assertIn(
            'data-kind="{kind}"',
            main,
        )
        self.assertIn(
            "fragment?.classList.toggle('active', active)",
            main,
        )
        self.assertIn(
            'slide_paths = [f"slides/{path.name}"',
            main,
        )
        self.assertIn(
            "shell.className = 'slide-shell'",
            main,
        )
        self.assertIn(
            "frame.src = path",
            main,
        )
        self.assertIn(
            "const fontsReady = Promise.all(loads)",
            main,
        )

    def test_final_png_mtime_does_not_downgrade_formal_player(self):
        main = (ROOT / "studio/app/main.py").read_text(encoding="utf-8")
        start = main.index("def _static_html_preview_state(")
        end = main.index("\ndef _provisional_present_html(", start)
        preview = main[start:end]
        self.assertIn("stamp_sources = fragments + [css_path]", preview)
        self.assertNotIn('glob("slide_*.png")', preview)

    def test_studio_preserves_static_deck_motion_tokens(self):
        app = (ROOT / "studio/static/app.js").read_text(encoding="utf-8")
        start = app.index("function prepareStaticDeckMotion(")
        end = app.index("\nfunction playStaticSlide(", start)
        motion = app[start:end]
        self.assertNotIn("--motion-page-duration", motion)
        self.assertNotIn("--motion-enter-duration", motion)

    def test_studio_does_not_promote_html_newer_than_its_render_checkpoint(self):
        app = (ROOT / "studio/static/app.js").read_text(encoding="utf-8")
        start = app.index("function staticHtmlCheckpointReady(")
        end = app.index("\nfunction showStaticDeck(", start)
        gate = app[start:end]
        self.assertIn("renderedStamp >= authoredStamp", gate)
        self.assertIn("if (!staticHtmlCheckpointReady(n))", app)
        self.assertIn("if (!staticHtmlCheckpointReady(pending))", app)

    def test_static_preview_never_leaves_iframe_and_png_both_hidden(self):
        app = (ROOT / "studio/static/app.js").read_text(encoding="utf-8")
        keep_start = app.index("function keepStaticPng(")
        keep_end = app.index("\nlet staticPreviewFallbackToken", keep_start)
        keep = app[keep_start:keep_end]
        self.assertIn("if (!currentIsUsable)", keep)
        self.assertIn("img.src = url", keep)
        start = app.index("function scheduleStaticPreviewFallback(")
        end = app.index("\nasync function revealStaticDeckWhenFontsReady(", start)
        fallback = app[start:end]
        self.assertIn('frame.classList.contains("font-pending")', fallback)
        self.assertIn("keepStaticPng(n)", fallback)
        show_start = app.index("function showStaticDeck(")
        show_end = app.index("\nfunction outlineBriefData(", show_start)
        show = app[show_start:show_end]
        self.assertIn("scheduleStaticPreviewFallback(frame, n, deckId, requestedStamp)", show)
        self.assertIn('frame.contentDocument?.readyState === "complete"', show)

    def test_deployment_think_off_reaches_openai_compatible_chat_api(self):
        engine = (ROOT / "studio/app/engine.py").read_text(encoding="utf-8")
        self.assertIn('"chat_template_kwargs" if transport == "openai"', engine)
        # The deployment registry now supports both SENSENOVA_MODEL and
        # SENSENOVA_MODEL2 through one prefix-aware helper. Keep this assertion
        # on the semantic default rather than the old first-model-only literal.
        self.assertIn('f"{prefix}_THINKING_TRANSPORT"', engine)
        self.assertIn('"chat_template_kwargs",', engine)

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

    def test_process_feed_normalizes_internal_recovery_narration(self):
        app = (ROOT / "studio/static/app.js").read_text(encoding="utf-8")
        self.assertIn(
            "规划文件未通过批量写入约束，正在使用受控批量恢复方式更新。",
            app,
        )
        self.assertIn(
            "页面责任分组格式未通过校验，正在按既定所有权拓扑修正单页组写法。",
            app,
        )

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
        self.assertIn('"mural-presenter-v0.2",', engine)
        self.assertIn('"mural-presenter-v0.2-grouped",', engine)
        self.assertIn('"mural-presenter": "MURAL Presenter"', app)
        self.assertIn(
            '"mural-presenter-creative": "MURAL Presenter · Creative"', app
        )
        self.assertIn(
            '"mural-presenter-v0.2": "MURAL Presenter v0.2 · Single"', app
        )
        self.assertIn(
            '"mural-presenter-v0.2-grouped": "MURAL Presenter v0.2 · Grouped"', app
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

    def test_history_search_and_filters_use_list_metadata(self):
        template = (ROOT / "studio/templates/app.html").read_text(encoding="utf-8")
        app = (ROOT / "studio/static/app.js").read_text(encoding="utf-8")
        main = (ROOT / "studio/app/main.py").read_text(encoding="utf-8")
        dynamic = (ROOT / "studio/app/dynamic.py").read_text(encoding="utf-8")

        for element_id in (
            "history-search-input",
            "history-skill-filter",
            "history-model-filter",
        ):
            self.assertIn(f'id="{element_id}"', template)
        self.assertIn("function filteredHistoryRows()", app)
        self.assertIn("row.display_title || row.title", app)
        self.assertIn("row.skill_version", app)
        self.assertIn("row.model || row.model_key", app)
        self.assertIn("model,skill_version,seed_json", main)
        self.assertIn('item["model_label"]', main)
        self.assertIn('meta.get("model_key")', dynamic)
        self.assertIn('meta.get("skill_version")', dynamic)


if __name__ == "__main__":
    unittest.main()
