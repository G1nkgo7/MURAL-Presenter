import ast
import hashlib
import json
import os
import runpy
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class ReleaseWebUIParityTests(unittest.TestCase):
    def test_v1_digest_includes_shared_skill_scripts_with_runtime_path_checks(self):
        guard = runpy.run_path(str(ROOT.parent / "harnesses/mural-presenter-v1/runtime/release_guard.py"))
        with tempfile.TemporaryDirectory() as temporary:
            skills = Path(temporary) / "skills"
            shared = skills / "mural-presenter-v1/scripts"
            shared.mkdir(parents=True)
            source = shared / "slide.py"
            source.write_text("version = 1\n")
            alpha = skills / "mural-presenter-v1-alpha"
            alpha.mkdir()
            (alpha / "SKILL.md").write_text("selected Skill\n")
            (alpha / "scripts").symlink_to("../mural-presenter-v1/scripts", target_is_directory=True)
            def digest():
                return guard["release_tree_digest"]((alpha,), relative_to=alpha)
            before = digest()
            source.write_text("version = 2\n")
            self.assertNotEqual(before, digest())
            (alpha / "unapproved").symlink_to("../mural-presenter-v1/scripts", target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "unsupported Skill source symlink"):
                digest()

    def test_runtime_revision_hash_ignores_generated_harness_outputs(self):
        engine_path = ROOT / "studio/app/engine.py"
        tree = ast.parse(engine_path.read_text(encoding="utf-8"))
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_hash_runtime_tree"
        )
        namespace = {"hashlib": hashlib, "os": os, "Path": Path}
        exec(
            compile(
                ast.Module(body=[function], type_ignores=[]),
                str(engine_path),
                "exec",
            ),
            namespace,
        )
        hash_tree = namespace["_hash_runtime_tree"]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "core").mkdir()
            (root / "runs/sample/_trace").mkdir(parents=True)
            (root / "artifacts").mkdir()
            (root / "canary/runs/live").mkdir(parents=True)
            (root / "logs").mkdir()
            (root / "core/runtime.py").write_text("VERSION = 1\n", encoding="utf-8")
            (root / "runs/sample/_trace/messages.json").write_text("first", encoding="utf-8")
            (root / "artifacts/result.json").write_text("first", encoding="utf-8")
            (root / "canary/runs/live/result.json").write_text("first", encoding="utf-8")
            (root / "logs/live.log").write_text("first", encoding="utf-8")

            before = hashlib.sha256()
            hash_tree(before, root, prefix="harness/")
            (root / "runs/sample/_trace/messages.json").write_text("second", encoding="utf-8")
            (root / "artifacts/result.json").write_text("second", encoding="utf-8")
            (root / "canary/runs/live/result.json").write_text("second", encoding="utf-8")
            (root / "logs/live.log").write_text("second", encoding="utf-8")
            after_outputs = hashlib.sha256()
            hash_tree(after_outputs, root, prefix="harness/")
            self.assertEqual(before.hexdigest(), after_outputs.hexdigest())

            (root / "core/runtime.py").write_text("VERSION = 2\n", encoding="utf-8")
            after_source = hashlib.sha256()
            hash_tree(after_source, root, prefix="harness/")
            self.assertNotEqual(before.hexdigest(), after_source.hexdigest())

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

    def test_v02_formal_players_expose_font_readiness(self):
        for language in ("en", "zh"):
            for grouped in (False, True):
                suffix = "-grouped" if grouped else ""
                deck = (
                    ROOT.parents[0]
                    / "archive/mural-presenter-pre-v0.6/skills/mural-presenter-v0.2"
                    / f"mural-presenter-v0-2{suffix}-{language}/scripts/deck.py"
                ).read_text(encoding="utf-8")
                self.assertIn(
                    "const fontsReady = Promise.resolve(document.fonts?.ready)",
                    deck,
                )
                self.assertIn(
                    "window.cleanDeck = {{ go, step, count: slides.length, fontsReady }}",
                    deck,
                )

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

    def test_static_preview_uses_canonical_viewport_and_current_page_fallback(self):
        app = (ROOT / "studio/static/app.js").read_text(encoding="utf-8")
        self.assertNotIn("const DEFAULT_CANVAS_WIDTH", app)
        self.assertNotIn("const DEFAULT_CANVAS_HEIGHT", app)
        active_start = app.index("function activeCanvasSize(")
        active_end = app.index("\nfunction setCanvasSpec(", active_start)
        active = app[active_start:active_end]
        self.assertIn("ed.canvasSpec?.width_px", active)
        self.assertIn("ed.canvasSpec?.height_px", active)
        self.assertIn(": null", active)
        fit_start = app.index("function fitCanonicalStaticFrame(")
        fit_end = app.index("\nfunction resetCanonicalStaticFrame(", fit_start)
        fit = app[fit_start:fit_end]
        self.assertIn("if (!size) return", fit)
        self.assertIn("deck.offsetWidth", fit)
        self.assertIn("const { width, height } = size", fit)
        self.assertIn("frame.style.width = `${width}px`", fit)
        self.assertIn("frame.style.height = `${height}px`", fit)
        self.assertIn("frame.style.transform = `scale(${scale})`", fit)

        show_start = app.index("function showStaticDeck(")
        show_end = app.index("\nfunction outlineBriefData(", show_start)
        show = app[show_start:show_end]
        self.assertLess(show.index("primeStaticPng(n)"), show.index("const sameDeck"))

        keep_start = app.index("function keepStaticPng(")
        keep_end = app.index("\nlet staticPreviewFallbackToken", keep_start)
        self.assertIn("primeStaticPng(n)", app[keep_start:keep_end])

    def test_deployment_think_off_reaches_openai_compatible_chat_api(self):
        engine = (ROOT / "studio/app/engine.py").read_text(encoding="utf-8")
        self.assertIn('"openai": "chat_template_kwargs"', engine)
        self.assertIn("def _default_openai_thinking_transport", engine)
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
            "const firstPrimaryLoad = (firstFlowRender || firstPopulatedFlow)",
            "const shouldFollow = wasNearBottom || firstPrimaryLoad",
            "top: shouldFollow ? scroller.scrollHeight : previousTop",
        ):
            self.assertIn(marker, app)

    def test_agent_trace_refresh_cannot_leave_the_panel_permanently_loading(self):
        app = (ROOT / "studio/static/app.js").read_text(encoding="utf-8")
        start = app.index("async function loadWorkflowAgentTrace(")
        end = app.index("\nfunction openWorkflowAgentTrace(", start)
        loader = app[start:end]

        self.assertIn("const controller = new AbortController()", loader)
        self.assertIn("WORKFLOW_TRACE_REQUEST_TIMEOUT_MS", loader)
        self.assertIn("signal: controller.signal", loader)
        self.assertIn("clearTimeout(timeout)", loader)
        self.assertIn("ed.workflowTraceLoading = false", loader)
        self.assertIn("if (request === ed.workflowTraceRequest)", loader)

        messages_start = app.index("function renderWorkflowTraceMessages(")
        messages_end = app.index("\nfunction renderWorkflowTraceContent(", messages_start)
        messages = app[messages_start:messages_end]
        self.assertIn(
            "if (ed.workflowTraceLoading && !ed.workflowTracePayload)",
            messages,
        )

    def test_workflow_blank_click_keeps_the_selected_agent_trace_available(self):
        app = (ROOT / "studio/static/app.js").read_text(encoding="utf-8")
        controls_start = app.index("function bindWorkflowCenterControls(")
        controls_end = app.index("\nfunction scheduleWorkflowEdgeDraw(", controls_start)
        controls = app[controls_start:controls_end]
        blank_start = controls.index(
            'if (!event.target.closest(".workflow-node") && (ed.workflowTraceAgent || ed.workflowSelectedNodeId)) {'
        )
        blank_click = controls[blank_start:controls.index("\n      }", blank_start)]
        self.assertIn('ed.workflowSelectedNodeId = "";', blank_click)
        self.assertIn('setWorkflowConversationMode("history");', blank_click)
        self.assertNotIn('clearAgent: true', blank_click)
        self.assertNotIn('ed.workflowTraceAgent =', blank_click)
        self.assertNotIn('ed.workflowTracePayload =', blank_click)

        trace_start = app.index("function bindWorkflowTraceControls(")
        trace_end = app.index("\nfunction setScrollBottomImmediately(", trace_start)
        trace_controls = app[trace_start:trace_end]
        self.assertIn(
            'back.addEventListener("click", () => setWorkflowConversationMode("history"))',
            trace_controls,
        )

    def test_process_feed_preserves_agent_recovery_narration(self):
        import subprocess

        app = (ROOT / "studio/static/app.js").read_text(encoding="utf-8")
        start = app.index("function cleanAgentText(")
        clean = app[start:app.index("\nfunction agentStoryHtml(", start)]
        originals = [
            "Slide 2 is not ready. The layout still needs work.",
            "Review is not complete.",
            "研究尚未完成，仍有事实待核验。",
            "The planning write failed; I need to read the exact current text before retrying.",
        ]
        script = clean + "\nconsole.log(JSON.stringify(" + json.dumps(originals) + ".map(cleanAgentText)));"
        result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout), originals)

    def test_process_feed_keeps_terminal_states_and_revision_scoping(self):
        app = (ROOT / "studio/static/app.js").read_text(encoding="utf-8")
        trace = (ROOT / "studio/app/trace.py").read_text(encoding="utf-8")
        main = (ROOT / "studio/app/main.py").read_text(encoding="utf-8")
        css = (ROOT / "studio/static/app.css").read_text(encoding="utf-8")

        self.assertIn(
            '["completed", "failed", "rejected", "interrupted", "paused_recoverable", "not_started"]',
            app,
        )
        self.assertIn("function renderStatus(p, phase)", app)
        self.assertIn("def _inherit_live_event_positions", trace)
        self.assertIn("def _attach_absolute_event_times", trace)
        self.assertIn('event["ts"] = _trace_iso(started_at + elapsed_s)', trace)
        self.assertGreaterEqual(main.count("trace.scoped_specialist_artifacts("), 3)
        self.assertIn(".orchestration-timing-card", css)
        self.assertIn(".orch-output-time", css)

    def test_public_release_defaults_to_the_only_active_v1_pair(self):
        engine = (ROOT / "studio/app/engine.py").read_text(encoding="utf-8")
        from studio.app import engine as registry
        self.assertEqual(tuple(registry.SKILLS), ("mural-presenter-v1",))
        self.assertEqual(tuple(registry.PIPELINES), ("mural-presenter-v1-harness",))
        self.assertEqual(registry.PUBLIC_SKILL_KEYS, ("mural-presenter-v1",))
        self.assertEqual(registry.PUBLIC_PIPELINE_KEYS, ("mural-presenter-v1-harness",))
        self.assertNotIn("LEGACY_SKILL_ALIASES", engine)
        main = (ROOT / "studio/app/main.py").read_text(encoding="utf-8")
        self.assertIn("for k in engine.PUBLIC_PIPELINE_KEYS", main)

        launch = (ROOT / "scripts/launch.py").read_text(encoding="utf-8")
        self.assertIn(
            '"harnesses/mural-presenter-v1/runtime/release_guard.py"',
            launch.replace("\n", ""),
        )
        self.assertIn('"PPTAGENT_MURAL_V1_HARNESS_ROOT"', launch)
        self.assertNotIn('"mural-presenter-v1,mural-presenter-v1.0"', launch)
        self.assertNotIn('harnesses/mural-presenter-v1.0', launch)

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

    def test_mural_v01_is_archived_and_not_registered(self):
        engine = (ROOT / "studio/app/engine.py").read_text(encoding="utf-8")
        self.assertNotIn("def _mural_v01_skill(", engine)
        self.assertNotIn('"mural-presenter-v0.1": _mural_v01_skill()', engine)
        archive = ROOT.parents[0] / "archive/mural-presenter-pre-v0.6"
        self.assertTrue(
            (archive / "skills/mural-presenter-v0.1/mural-presenter-v0-1-zh/SKILL.md").is_file()
        )
        self.assertTrue(
            (archive / "harnesses/mural-presenter-v0.1/infer.py").is_file()
        )


if __name__ == "__main__":
    unittest.main()
