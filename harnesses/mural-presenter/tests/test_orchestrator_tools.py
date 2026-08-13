import unittest
from pathlib import Path
import glob
import hashlib
import json
import os
import re
import tempfile
from types import SimpleNamespace
from unittest import mock

import infer
from core import _final_contract, tools
from core import agent as agent_core
from core import nova_bridge, one_shot_vision
from core.contracts import CONTENT_FIDELITY_PATH, REVIEW_CLOSEOUT_ARTIFACTS, REVIEW_ISSUES_PATH


class OrchestratorToolSurfaceTest(unittest.TestCase):
    def test_exact_tool_repeat_guard_is_consecutive_and_resets_on_progress(self):
        agent = SimpleNamespace()
        repeated = [SimpleNamespace(
            name="terminal",
            input={"command": "grep -n missing scripts/deck.py"},
        )]
        different = [SimpleNamespace(
            name="search_files",
            input={"pattern": "missing", "path": "scripts/deck.py"},
        )]

        self.assertEqual(agent_core._exact_tool_repeat_count(agent, repeated), 1)
        self.assertEqual(agent_core._exact_tool_repeat_count(agent, repeated), 2)
        self.assertEqual(agent_core._exact_tool_repeat_count(agent, repeated), 3)
        self.assertEqual(agent_core._exact_tool_repeat_count(agent, different), 1)
        self.assertEqual(agent_core._exact_tool_repeat_count(agent, repeated), 1)

        # A write is substantive progress and deliberately leaves this guard.
        write = [SimpleNamespace(name="write_file", input={"path": "plan/a.md"})]
        self.assertEqual(agent_core._exact_tool_repeat_count(agent, write), 0)
        self.assertIsNone(agent._exact_repeat_signature)

    def test_slide_vision_allows_idempotent_rerender_but_rejects_noop_edit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            slides = root / "slides"
            renders = root / "renders"
            slides.mkdir()
            renders.mkdir()
            html = slides / "slide_01.html"
            css = root / "base.css"
            png = renders / "slide_01.png"
            html.write_text("<main>stable</main>", encoding="utf-8")
            css.write_text("main { color: black; }", encoding="utf-8")
            png.write_bytes(b"same-pixels")
            now = 1_800_000_000_000_000_000
            os.utime(html, ns=(now, now))
            os.utime(css, ns=(now, now))
            os.utime(png, ns=(now + 10, now + 10))
            agent = SimpleNamespace(ws=str(root))

            self.assertIsNone(tools._slide_vision_freshness_error(agent, str(png)))

            # A deterministic final render can replace the PNG without changing
            # either source or pixels.  It remains valid evidence and must not
            # force a cosmetic source edit.
            os.utime(png, ns=(now + 20, now + 20))
            self.assertIsNone(tools._slide_vision_freshness_error(agent, str(png)))

            # If a source really changed but the pixels did not, the guard still
            # catches the no-op repair.
            os.utime(html, ns=(now + 15, now + 15))
            os.utime(png, ns=(now + 30, now + 30))
            error = tools._slide_vision_freshness_error(agent, str(png))
            self.assertIn("无效修复", error or "")

    def test_review_closeout_contract_covers_final_acceptance_artifacts(self):
        self.assertIn(REVIEW_ISSUES_PATH, REVIEW_CLOSEOUT_ARTIFACTS)
        self.assertIn(CONTENT_FIDELITY_PATH, REVIEW_CLOSEOUT_ARTIFACTS)
        self.assertEqual(infer.REVIEW_ISSUES_PATH, REVIEW_ISSUES_PATH)
        self.assertEqual(infer.CONTENT_FIDELITY_PATH, CONTENT_FIDELITY_PATH)

    def test_slide_group_pages_do_not_consume_next_numbered_line(self):
        task = {
            "goal": "Slide Group general",
            "context": "--pages 02,03,04\n12. 输出厨房组任务",
        }

        self.assertEqual(agent_core._slide_group_pages(task), [2, 3, 4])

    def test_slide_group_pages_prefer_structured_assignment(self):
        task = {
            "goal": "Slide Group wardrobe pages 05,06,07,08,09,10,15",
            "assigned_pages": [5, 6, 7, 8, 9, 10],
        }

        self.assertEqual(agent_core._slide_group_pages(task), [5, 6, 7, 8, 9, 10])

    def test_normalized_slide_task_preserves_structured_assignment(self):
        task = agent_core._normalize_task({
            "goal": "Slide Group general --pages 02,03,04\n12. 输出厨房组任务",
            "label": "slide_group_general",
            "assigned_pages": [4, "02", 3, 4, 0, "bad"],
        })

        self.assertEqual(task["assigned_pages"], [2, 3, 4])
        self.assertEqual(agent_core._slide_group_pages(task), [2, 3, 4])

    def test_subprocess_env_prepends_engine_python_directory(self):
        executable = os.path.join(os.sep, "runtime", "venv", "bin", "python")
        with mock.patch.dict(os.environ, {
            "PPTAGENT_ENGINE_PYTHON": executable,
            "PATH": os.pathsep.join([os.path.join(os.sep, "usr", "bin"), os.path.dirname(executable)]),
        }, clear=False):
            env = tools._subprocess_env()

        self.assertEqual(env["PATH"].split(os.pathsep)[0], os.path.dirname(executable))
        self.assertEqual(env["PATH"].split(os.pathsep).count(os.path.dirname(executable)), 1)

    def test_oversized_vision_result_is_spooled_losslessly(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace_dir = root / "_trace/orchestrator"
            trace_dir.mkdir(parents=True)
            agent = SimpleNamespace(
                ws=str(root),
                prompt_language="zh",
                trace=SimpleNamespace(sub_dir=str(trace_dir)),
            )
            tool_use = SimpleNamespace(id="toolu_vision_big")
            original = "vision-start\n" + "像素证据" * 5000 + "\nvision-tail"
            preview = agent_core._lossless_tool_text(
                agent, tool_use, original, 16000
            )
            self.assertIn("工具结果尚未结束", preview)
            match = re.search(r"已保存到 ([^\s。]+)", preview)
            self.assertIsNotNone(match)
            stored = root / match.group(1)
            self.assertEqual(stored.read_text(encoding="utf-8"), original)

    def test_restore_vision_evidence_from_immutable_trace_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace_dir = root / "_trace" / "subagents" / "review"
            image_dir = trace_dir / "images"
            render_dir = root / "renders"
            image_dir.mkdir(parents=True)
            render_dir.mkdir()
            viewed = b"pixel bytes inspected by vision"
            current = render_dir / "contact-sheet-review-01.png"
            current.write_bytes(viewed)
            (image_dir / "view_01.png").write_bytes(viewed)
            (trace_dir / "tool_log.json").write_text(json.dumps([
                {
                    "turn": 3,
                    "name": "vision_analyze",
                    "args": {"image_url": "/tmp/rejected-preview.png"},
                },
                {
                    "turn": 4,
                    "name": "vision_analyze",
                    "args": {"image_url": "renders/contact-sheet-review-01.png"},
                },
            ]), encoding="utf-8")

            def read_path(relative):
                candidate = Path(relative)
                if candidate.is_absolute() or ".." in candidate.parts:
                    raise ValueError(f"outside workspace: {relative}")
                return str(root / candidate)

            agent = SimpleNamespace(
                ws=str(root),
                trace=SimpleNamespace(sub_dir=str(trace_dir)),
                read_path=read_path,
                vision_paths=[],
                vision_evidence={},
                n_vision_calls=0,
                log=lambda _message: None,
            )

            restored = agent_core._restore_vision_evidence(agent)

            rel = "renders/contact-sheet-review-01.png"
            self.assertEqual(restored["vision_paths"], [rel])
            self.assertEqual(
                restored["vision_evidence"][rel]["sha256"],
                hashlib.sha256(viewed).hexdigest(),
            )
            self.assertTrue((trace_dir / "vision-evidence.json").is_file())

    def test_post_run_evidence_recovery_failure_keeps_valid_handoff(self):
        import threading

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace_dir = root / "_trace" / "subagents" / "research"
            trace_dir.mkdir(parents=True)
            (root / "research").mkdir()
            (root / "research" / "research.md").write_text(
                "# Verified research\n", encoding="utf-8"
            )
            child = SimpleNamespace(
                run=lambda: True,
                final_text=(
                    "status: ready\n"
                    "output: research/research.md\n"
                    "unresolved: none\n"
                ),
                exit_reason="text_response",
                trace=SimpleNamespace(sub_dir=str(trace_dir)),
                nova_precheck=None,
                n_renders=0,
                n_vision_calls=0,
                vision_paths=[],
                vision_evidence={},
                last_shot=None,
                log=lambda _message: None,
            )
            parent = SimpleNamespace(
                ws=str(root),
                _child_sem=threading.Semaphore(1),
                _spawn_lock=threading.Lock(),
                worker_recs=[],
            )
            task = {"label": "research", "goal": "Research: verify facts"}
            ticket = {"abandoned": False, "label": "research"}

            with mock.patch.object(
                agent_core, "_build_child", return_value=(child, "research")
            ), mock.patch.object(
                agent_core,
                "_restore_vision_evidence",
                side_effect=ValueError("outside workspace: /tmp/debug.png"),
            ):
                result = agent_core._run_child(parent, task, ticket)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["contract"]["status"], "ready")
            handoff = root / result["handoff_path"]
            self.assertTrue(handoff.is_file())
            persisted = json.loads(handoff.read_text(encoding="utf-8"))
            self.assertTrue(persisted["clean"])
            self.assertEqual(persisted["contract"]["status"], "ready")

    def test_acceptance_merges_review_evidence_sidecar(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace_dir = root / "_trace" / "subagents" / "review"
            trace_dir.mkdir(parents=True)
            evidence = {
                "renders/contact-sheet-review-01.png": {
                    "sha256": "abc",
                    "mtime_ns": 123,
                }
            }
            (trace_dir / "vision-evidence.json").write_text(json.dumps({
                "version": 1,
                "vision_calls": 3,
                "vision_paths": list(evidence),
                "vision_evidence": evidence,
            }), encoding="utf-8")

            recovered = infer._review_with_persisted_evidence(str(root), {
                "label": "review",
                "trace_dir": "_trace/subagents/review",
                "vision_calls": 0,
            })

            self.assertEqual(recovered["vision_calls"], 3)
            self.assertEqual(recovered["vision_paths"], list(evidence))
            self.assertEqual(recovered["vision_evidence"], evidence)

    def test_acceptance_recovers_pre_sidecar_review_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace_dir = root / "_trace" / "subagents" / "review"
            image_dir = trace_dir / "images"
            render_dir = root / "renders"
            image_dir.mkdir(parents=True)
            render_dir.mkdir()
            source = render_dir / "contact-sheet-review-01.png"
            source.write_bytes(b"original high resolution pixels")
            snapshot = image_dir / "view_01.png"
            snapshot.write_bytes(b"resized pixels returned to the model")
            source_time = source.stat().st_mtime_ns
            os.utime(snapshot, ns=(source_time + 1_000_000, source_time + 1_000_000))
            (trace_dir / "messages.json").write_text(json.dumps([
                {"role": "assistant", "content": [{
                    "type": "tool_use",
                    "id": "vision-1",
                    "name": "vision_analyze",
                    "input": {"image_url": "renders/contact-sheet-review-01.png"},
                }]},
                {"role": "user", "content": [{
                    "type": "tool_result",
                    "tool_use_id": "vision-1",
                    "content": [{"type": "image", "shot": "images/view_01.png"}],
                }]},
            ]), encoding="utf-8")

            recovered = infer._review_with_persisted_evidence(str(root), {
                "label": "review",
                "trace_dir": "_trace/subagents/review",
            })

            rel = "renders/contact-sheet-review-01.png"
            self.assertEqual(recovered["vision_paths"], [rel])
            self.assertEqual(recovered["vision_calls"], 1)
            self.assertEqual(
                recovered["vision_evidence"][rel]["sha256"],
                hashlib.sha256(source.read_bytes()).hexdigest(),
            )

    def test_initial_generation_without_deliverable_still_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            orch = SimpleNamespace(ws=temporary, exit_reason="max_turns")
            ok, reason = infer._accept(orch)
        self.assertFalse(ok)
        self.assertIn("没有产出任何 slide", reason)

    def test_revision_non_text_finish_still_runs_delivery_gates(self):
        with tempfile.TemporaryDirectory() as temporary:
            orch = SimpleNamespace(ws=temporary, exit_reason="max_turns")
            ok, reason = infer._accept(
                orch,
                allow_review_only=True,
                allow_non_text_exit=True,
            )
        self.assertFalse(ok)
        self.assertEqual(reason, "没有产出任何 slide")

    def test_openai_backend_does_not_require_anthropic_key(self):
        with mock.patch.dict(
            os.environ,
            {"MODEL_BACKEND": " OpenAI "},
            clear=True,
        ):
            self.assertFalse(infer._requires_anthropic_api_key())

    def test_default_backend_still_requires_anthropic_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(infer._requires_anthropic_api_key())

    def test_image_generate_without_key_fails_before_network_request(self):
        agent = SimpleNamespace(img_key="")
        with mock.patch.object(tools.requests, "post") as post:
            result = tools.image_generate(agent, "a visual for a presentation")

        self.assertIn("未配置生图 API Key", result)
        self.assertIn("未发起网络请求", result)
        self.assertIn("不要重试", result)
        post.assert_not_called()

    def test_nova_vision_returns_text_only_to_main_agent(self):
        from PIL import Image

        class Agent:
            def __init__(self, root):
                self.ws = str(root)
                self.label = "slide_bookends"
                self.prompt_language = "zh"

            def read_path(self, relative):
                return str(Path(self.ws) / relative)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "renders/slide_01.png"
            image.parent.mkdir()
            Image.new("RGB", (64, 64), "#223344").save(image)
            with mock.patch.dict(os.environ, {"NOVA_RAW_V2": "1"}), mock.patch.object(
                nova_bridge,
                "call_vision_auxiliary",
                return_value="像素证据正常",
            ) as call:
                result = tools.vision_analyze(
                    Agent(root),
                    "renders/slide_01.png",
                    question="检查裁切",
                    _parent_tool_use_id="toolu_vision_1",
                )
        self.assertEqual(result["vision_backend"], "nova_auxiliary_model")
        self.assertIn("像素证据正常", result["vision_analysis"])
        self.assertNotIn("image_b64", result)
        self.assertEqual(call.call_args.kwargs["parent_tool_use_id"], "toolu_vision_1")

    def test_nova_main_call_persists_exact_request_response_and_trace(self):
        trace = {
            "schema_version": "nova_agent.internal_model_calls.v1",
            "status": {"ok": True},
            "runtime": {"agent_send_images_field": None},
            "images": [],
            "tools": {"user": []},
            "summary": {"tool_sequence": [], "final_answer": "done"},
            "agent_model_calls": [
                {
                    "index": 1,
                    "model_input": {"rendered_prompt": "prompt"},
                    "request": {},
                    "response": {"ok": True},
                    "model_reply": {"raw_text": "done"},
                    "builtin_tool_executions": [],
                }
            ],
            "vision_model_calls": [],
        }
        payload = {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5",
            "content": [{"type": "text", "text": "done"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "nova_internal_trace": trace,
        }

        class Raw:
            content = json.dumps(payload).encode()
            http_response = SimpleNamespace(status_code=200)

            @staticmethod
            def parse():
                return SimpleNamespace(
                    content=[SimpleNamespace(type="text", text="done")],
                    usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                )

        create = mock.Mock(return_value=Raw())
        client = SimpleNamespace(
            messages=SimpleNamespace(
                with_raw_response=SimpleNamespace(create=create)
            )
        )
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ,
            {
                "NOVA_RAW_V2": "1",
                "NOVA_RAW_ROOT": temporary,
                "NOVA_RUN_ID": "smoke",
                "NOVA_AGENT_STOP_SEQUENCES_JSON": json.dumps(
                    ["\nuser\n<tool_response>"]
                ),
            },
        ):
            agent = SimpleNamespace(
                sid="sample-1",
                role="orchestrator",
                label="orch",
                tools=[],
                initial_user="make a deck",
                client=client,
                log=lambda *_: None,
            )
            agent.nova_raw = nova_bridge.create_recorder(agent)
            response = nova_bridge.call_main(
                agent,
                {
                    "model": "claude-opus-5",
                    "system": "system",
                    "messages": [{"role": "user", "content": "make a deck"}],
                    "max_tokens": 1024,
                    "tools": [],
                },
            )
            request_files = list(Path(temporary).glob("tasks/*/attempts/main/**/request.json"))
            response_files = list(Path(temporary).glob("tasks/*/attempts/main/**/response.json"))
            trace_files = list(Path(temporary).glob("tasks/*/attempts/main/**/trace.json"))
            request = json.loads(request_files[0].read_text())
            saved_response = json.loads(response_files[0].read_text())
            saved_trace = json.loads(trace_files[0].read_text())

        self.assertEqual(response.content[0].text, "done")
        self.assertFalse(request["stream"])
        self.assertTrue(request["nova_include_trace"])
        self.assertEqual(request["nova_trace_context"]["request_kind"], "hermes_main")
        self.assertEqual(request["stop_sequences"], ["\nuser\n<tool_response>"])
        self.assertEqual(saved_response["nova_internal_trace"], trace)
        self.assertEqual(saved_trace, trace)

    def test_orchestrator_public_label_is_always_orch(self):
        self.assertEqual(
            agent_core._canonical_agent_label("orchestrator", "orchestrator"),
            "orch",
        )
        self.assertEqual(
            agent_core._canonical_agent_label("orchestrator", "orch"),
            "orch",
        )
        self.assertEqual(
            agent_core._canonical_agent_label("leaf", "slide_01"),
            "slide_01",
        )

    def test_quality_commands_cannot_hide_diagnostics_with_tail(self):
        class Agent:
            def __init__(self, root):
                self.ws = str(root)
                self.label = "review"

            def safe(self, relative):
                return str(Path(self.ws) / relative)

        with tempfile.TemporaryDirectory() as temporary:
            result = tools.terminal(
                Agent(temporary),
                "python skills/mural-presenter/scripts/deck.py build . --expected 36 | tail -30",
            )
            self.assertIn("禁止用 tail/head", result)

    def test_terminal_rejects_recursive_root_scan(self):
        class Agent:
            def __init__(self, root):
                self.ws = str(root)
                self.label = "orch"

            def safe(self, relative):
                return str(Path(self.ws) / relative)

        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "core.tools.subprocess.run"
        ) as run:
            result = tools.terminal(
                Agent(temporary),
                'find / -name "NotoSans-SC*" -type f',
            )
        self.assertIn("禁止递归扫描文件系统根目录", result)
        run.assert_not_called()

    def test_blocked_review_is_terminal_for_orchestrator(self):
        parent = SimpleNamespace(
            role="orchestrator",
            worker_recs=[{
                "label": "review",
                "kind": "review",
                "clean": False,
                "exit_reason": "text_response",
                "contract": {
                    "status": "blocked",
                    "remaining": "slide_02 NOTE 换行",
                    "validation_error": "review 必须返回 status: ready，得到 blocked",
                },
            }],
            _spawn_lock=None,
        )
        failure = agent_core._blocking_review_failure(parent)
        self.assertEqual(failure["status"], "blocked")
        self.assertIn("NOTE 换行", failure["detail"])

    def test_review_blocked_acceptance_reports_real_reason(self):
        import threading

        orch = SimpleNamespace(
            ws="/unused",
            exit_reason="review_blocked",
            _spawn_lock=threading.Lock(),
            worker_recs=[],
            _terminal_contract_failure={
                "status": "blocked",
                "detail": "slide_02 NOTE 换行",
            },
        )
        with mock.patch.object(infer, "_slide_htmls", return_value=["/unused/slides/slide_01.html"]), \
                mock.patch.object(infer.os.path, "exists", return_value=True), \
                mock.patch.object(infer, "_render_ok", return_value=True), \
                mock.patch.object(infer, "_ensure_present_html", return_value=(True, "ok")), \
                mock.patch.object(infer, "_delivery_audit", return_value=(True, "ok")), \
                mock.patch.object(infer, "_grounded_acceptance", return_value=(True, "ok")), \
                mock.patch.object(infer, "_research_acceptance", return_value=(True, "ok")), \
                mock.patch.object(infer, "_image_acceptance", return_value=(True, "ok")), \
                mock.patch.object(infer, "_slide_assignment_acceptance", return_value=(True, "ok")), \
                mock.patch.object(infer, "_pixel_review_acceptance", return_value=(False, "Review blocked")), \
                mock.patch.object(infer, "_v_pass", return_value=(True, "ok")):
            ok, reason = infer._accept(orch)
        self.assertTrue(ok)
        self.assertIn("completed_with_issues", reason)
        self.assertIn("NOTE 换行", reason)

    def test_successful_render_labels_bbox_signals_as_diagnostic(self):
        class Agent:
            def __init__(self, root):
                self.ws = str(root)
                self.label = "slide_group_a"

            def safe(self, relative):
                return str(Path(self.ws) / relative)

        completed = mock.Mock(
            returncode=0,
            stdout="renders/slide_07.png boxoverflow=2 overlap=1\n",
            stderr="",
        )
        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "core.tools.subprocess.run", return_value=completed
        ):
            result = tools.terminal(
                Agent(temporary),
                "python skills/mural-presenter/scripts/render.py --batch . --pages 07",
            )
        self.assertIn("诊断提示", result)
        self.assertIn("不是进程失败", result)

    def test_review_cannot_open_a_second_visual_refine_round(self):
        class Agent:
            def __init__(self, root):
                self.ws = str(root)
                self.label = "review"
                self._review_refine_rounds = 1

            def safe(self, relative):
                return str(Path(self.ws) / relative)

            def writable(self, _relative):
                return True

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "slides").mkdir()
            page = root / "slides/slide_02.html"
            page.write_text("old", encoding="utf-8")
            result = tools.patch(
                Agent(root), path="slides/slide_02.html",
                old_string="old", new_string="new",
            )
            self.assertIn("不得开启第二轮", result)

    def test_image_agent_cannot_reinspect_unchanged_candidate(self):
        from PIL import Image

        class Agent:
            def __init__(self, root):
                self.ws = str(root)
                self.label = "image_a"

            def read_path(self, relative):
                return str(Path(self.ws) / relative)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "assets").mkdir()
            candidate = root / "assets/candidate.png"
            Image.new("RGB", (64, 64), "#c9a86a").save(candidate)
            agent = Agent(root)

            first = tools.vision_analyze(agent, "assets/candidate.png")
            self.assertIsInstance(first, dict)
            repeated = tools.vision_analyze(
                agent, "assets/candidate.png", question="ask again differently"
            )
            self.assertIn("已阻止重复素材检查", repeated)

            Image.new("RGB", (64, 64), "#0e0d0b").save(candidate)
            changed = tools.vision_analyze(agent, "assets/candidate.png")
            self.assertIsInstance(changed, dict)

    def test_vision_uses_current_agent_model_and_query_language(self):
        from PIL import Image

        class Agent:
            def __init__(self, root, language):
                self.ws = str(root)
                self.label = "slide_bookends"
                self.prompt_language = language

            def read_path(self, relative):
                return str(Path(self.ws) / relative)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "assets").mkdir()
            candidate = root / "assets/candidate.png"
            Image.new("RGB", (64, 64), "#c9a86a").save(candidate)

            english = tools.vision_analyze(
                Agent(root, "en"), "assets/candidate.png"
            )
            self.assertIsInstance(english, dict)
            self.assertNotIn("b_vision", english)
            self.assertIn("in English", english["summary"])

            chinese = tools.vision_analyze(
                Agent(root, "zh"), "assets/candidate.png"
            )
            self.assertIsInstance(chinese, dict)
            self.assertNotIn("b_vision", chinese)
            self.assertIn("用中文", chinese["summary"])

    def test_slide_vision_rejects_stale_pixels_and_repeated_identical_views(self):
        class Agent:
            def __init__(self, root):
                self.ws = str(root)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "slides").mkdir()
            (root / "renders").mkdir()
            source = root / "slides/slide_04.html"
            png = root / "renders/slide_04.png"
            source.write_text("<section>old</section>", encoding="utf-8")
            png.write_bytes(b"old pixels")
            os.utime(source, ns=(1_000_000_000, 1_000_000_000))
            os.utime(png, ns=(2_000_000_000, 2_000_000_000))
            agent = Agent(root)

            self.assertIsNone(tools._slide_vision_freshness_error(agent, str(png)))
            self.assertIsNone(tools._slide_vision_freshness_error(agent, str(png)))
            repeated = tools._slide_vision_freshness_error(agent, str(png))
            self.assertIn("无进展复看", repeated)

            source.write_text("<section>changed</section>", encoding="utf-8")
            os.utime(source, ns=(3_000_000_000, 3_000_000_000))
            stale = tools._slide_vision_freshness_error(agent, str(png))
            self.assertIn("旧像素", stale)

            os.utime(png, ns=(4_000_000_000, 4_000_000_000))
            unchanged = tools._slide_vision_freshness_error(agent, str(png))
            self.assertIn("无效修复", unchanged)

            png.write_bytes(b"new pixels")
            os.utime(png, ns=(5_000_000_000, 5_000_000_000))
            self.assertIsNone(tools._slide_vision_freshness_error(agent, str(png)))

    def test_review_cannot_reinspect_unchanged_pixels(self):
        class Agent:
            def __init__(self, root):
                self.ws = str(root)
                self.label = "review"

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "renders/contact-sheet-review-01.png"
            image.parent.mkdir()
            image.write_bytes(b"first pixels")
            agent = Agent(root)

            self.assertIsNone(tools._review_vision_repeat_error(agent, str(image)))
            repeated = tools._review_vision_repeat_error(agent, str(image))
            self.assertIn("Review 重复查看未变化像素", repeated)

            image.write_bytes(b"changed pixels")
            self.assertIsNone(tools._review_vision_repeat_error(agent, str(image)))

    def test_failed_one_shot_vision_does_not_consume_repeat_allowance(self):
        from PIL import Image

        class Agent:
            def __init__(self, root):
                self.ws = str(root)
                self.label = "image"
                self.prompt_language = "zh"

            def read_path(self, relative):
                return str(Path(self.ws) / relative)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "assets/candidate.png"
            image.parent.mkdir()
            Image.new("RGB", (64, 64), "#223344").save(image)
            agent = Agent(root)
            with mock.patch.dict(os.environ, {"VISION_BACKEND": "one_shot"}), mock.patch.object(
                one_shot_vision,
                "call",
                side_effect=RuntimeError("truncated by max tokens"),
            ) as call:
                first = tools.vision_analyze(
                    agent, "assets/candidate.png", _parent_tool_use_id="vision-1"
                )
                second = tools.vision_analyze(
                    agent, "assets/candidate.png", _parent_tool_use_id="vision-2"
                )

        self.assertIn("独立 Vision 模型不可用", first)
        self.assertIn("独立 Vision 模型不可用", second)
        self.assertNotIn("已阻止重复素材检查", second)
        self.assertEqual(call.call_count, 2)

    def test_tool_generated_asset_provenance_is_atomic_and_explicit(self):
        class Agent:
            def __init__(self, root):
                self.ws = str(root)

            def safe(self, relative):
                return str(Path(self.ws) / relative)

        with tempfile.TemporaryDirectory() as temporary:
            agent = Agent(temporary)
            tools._record_asset_provenance(
                agent,
                "assets/generated.png",
                origin="generated",
                generator_model="gpt-image-test",
                prompt="editorial hero",
                aspect_ratio="landscape",
            )
            tools._record_asset_provenance(
                agent,
                "assets/downloaded.jpg",
                origin="downloaded",
                source_url="https://example.com/photo.jpg",
            )
            catalog = json.loads(
                (Path(temporary) / "assets/catalog.json").read_text(encoding="utf-8")
            )
            by_path = {item["path"]: item for item in catalog["assets"]}
            self.assertEqual(by_path["assets/generated.png"]["origin"], "generated")
            self.assertEqual(
                by_path["assets/generated.png"]["generator_model"],
                "gpt-image-test",
            )
            self.assertEqual(
                by_path["assets/downloaded.jpg"]["source_url"],
                "https://example.com/photo.jpg",
            )

    def test_uses_standard_terminal_without_custom_sync_tools(self):
        names = {
            schema["name"]
            for schema in tools.resolve_toolsets(infer.ORCHESTRATOR_TOOLSETS)
        }

        self.assertIn("write_file", names)
        self.assertIn("patch", names)
        self.assertIn("terminal", names)
        self.assertIn("delegate_task", names)
        self.assertNotIn("sync_speech", names)
        self.assertNotIn("build_player", names)

    def test_child_final_contract_is_structured_for_acceptance(self):
        contract = _final_contract(
            "status: ready\ncoverage: complete\ncontent_fidelity: pass\n"
        )
        self.assertEqual(contract["status"], "ready")
        self.assertEqual(contract["coverage"], "complete")
        self.assertEqual(contract["content_fidelity"], "pass")

    def test_review_ready_ledger_recovers_missing_final_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "_trace").mkdir()
            (root / "slides").mkdir()
            (root / "renders").mkdir()
            (root / "speech.md").write_text("# Speech\n", encoding="utf-8")
            (root / "base.css").write_text(".slide{}\n", encoding="utf-8")
            (root / "slides/slide_01.html").write_text(
                "<section class='slide'>one</section>", encoding="utf-8"
            )
            (root / "renders/slide_01.png").write_bytes(b"final-pixels")
            render_mtime = max(
                (root / "base.css").stat().st_mtime_ns,
                (root / "slides/slide_01.html").stat().st_mtime_ns,
            ) + 10
            os.utime(root / "renders/slide_01.png", ns=(render_mtime, render_mtime))
            (root / "renders/review-contact.json").write_text(
                json.dumps({"full": {"pages": [1], "groups": []}}),
                encoding="utf-8",
            )
            ledger = root / "_trace/review-issues.md"
            ledger.write_text(
                "status: ready\n"
                "Hard: 无\n"
                "hard_issues: []\n"
                "remaining: none\n"
                "content_fidelity: pass\n",
                encoding="utf-8",
            )
            agent = SimpleNamespace(
                ws=str(root), label="review", initial_user="final review",
                _review_ledger_initial_sha256="old", _expected_review_mode="final_review",
                _review_refine_rounds=1, n_vision_calls=4,
                vision_evidence={
                    "renders/slide_01.png": {
                        "sha256": hashlib.sha256(b"final-pixels").hexdigest(),
                        "mtime_ns": render_mtime,
                    }
                },
                _dirty_visual_sources=set(),
            )
            contract = agent_core._review_ledger_contract(agent)

        self.assertEqual(contract["status"], "ready")
        self.assertEqual(contract["mode"], "final_review")
        self.assertEqual(contract["diagnosed_pages"], "all")
        self.assertEqual(contract["final_pixels_inspected"], "yes")
        self.assertEqual(contract["speech_aligned"], "yes")
        self.assertEqual(contract["remaining"], "none")
        self.assertEqual(
            contract["contract_recovered_from"], "_trace/review-issues.md"
        )

    def test_review_ledger_recovery_rejects_old_or_hard_issue_ledger(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "_trace").mkdir()
            ledger = root / "_trace/review-issues.md"
            ledger.write_text(
                "status: ready\nHard: slide_03 clipping\nremaining: none\n",
                encoding="utf-8",
            )
            agent = SimpleNamespace(
                ws=str(root), label="review", initial_user="final review",
                _review_ledger_initial_sha256="old", _expected_review_mode="final_review",
                _review_ledger_initial_mtime_ns=0,
                _review_refine_rounds=0, n_vision_calls=2,
                vision_evidence={}, _dirty_visual_sources=set(),
            )
            self.assertEqual(agent_core._review_ledger_contract(agent), {})
            ledger.write_text(
                "status: ready\nHard: 无\nhard_issues: []\nremaining: none\n",
                encoding="utf-8",
            )
            agent._review_ledger_initial_sha256 = hashlib.sha256(
                ledger.read_bytes()
            ).hexdigest()
            agent._review_ledger_initial_mtime_ns = ledger.stat().st_mtime_ns
            self.assertEqual(agent_core._review_ledger_contract(agent), {})

            # Rewriting the same ready content is not enough without current
            # post-build final-pixel evidence.
            previous_mtime = agent._review_ledger_initial_mtime_ns
            os.utime(ledger, ns=(previous_mtime + 1, previous_mtime + 1))
            self.assertEqual(agent_core._review_ledger_contract(agent), {})

    def test_harness_builds_missing_present_html_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "slides").mkdir()
            (root / "renders").mkdir()
            (root / "skills/test/scripts").mkdir(parents=True)
            (root / "slides/slide_01.html").write_text(
                "<section class='slide'>one</section>", encoding="utf-8"
            )
            (root / "renders/slide_01.png").write_bytes(b"png")
            (root / "skills/test/scripts/deck.py").write_text(
                "# fake deck builder\n", encoding="utf-8"
            )
            calls = []

            def fake_run(command, **kwargs):
                calls.append(command)
                (root / "present.html").write_text("x" * 200, encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="built", stderr="")

            with mock.patch.object(
                infer, "_workspace_skills_root", return_value=str(root / "skills")
            ), mock.patch.object(
                infer, "_workspace_skill_name", return_value="test"
            ), mock.patch.object(
                infer, "_run_noninteractive", side_effect=fake_run
            ):
                ok, reason = infer._ensure_present_html(str(root), 1)

        self.assertTrue(ok)
        self.assertEqual(reason, "built_by_harness")
        self.assertEqual(len(calls), 1)
        self.assertIn("build", calls[-1])

    def test_delivery_failure_is_fed_to_agent_then_rechecked(self):
        orch = SimpleNamespace(
            ws="/workspace/deck", prompt_language="zh", log=mock.Mock()
        )
        with mock.patch.object(
            infer,
            "_delivery_gate",
            side_effect=[(False, "slide_01.md: missing spoken script section"), (True, "ok")],
        ) as gate, mock.patch.object(
            agent_core, "delegate_task", return_value='{"results":[{"status":"ok"}]}'
        ) as delegate:
            ok, reason = infer._ensure_delivery_with_agent(
                orch, 1, allow_agent_repair=True
            )

        self.assertTrue(ok)
        self.assertEqual(reason, "ok")
        self.assertEqual(gate.call_count, 2)
        delegate.assert_called_once()
        kwargs = delegate.call_args.kwargs
        self.assertEqual(kwargs["label"], "delivery_fix_1")
        self.assertIn("missing spoken script section", kwargs["goal"])
        self.assertIn("plan/slide_NN.md", kwargs["goal"])

    def test_child_final_contract_accepts_safe_markdown_and_json_variants(self):
        bold = _final_contract(
            "**产出合同**\n\n- **status**: `ready`\n- **coverage: complete**\n"
        )
        self.assertEqual(bold["status"], "ready")
        self.assertEqual(bold["coverage"], "complete")
        self.assertEqual(
            _final_contract("**status: ready**，8/8 素材已落盘。")["status"],
            "ready",
        )
        self.assertEqual(
            _final_contract("**状态:ready** — 12/12 素材可用。")["status"],
            "ready",
        )
        self.assertEqual(
            _final_contract(
                "`status: ready`，路径 `assets/cover.png`。"
            )["status"],
            "ready",
        )

        fenced = _final_contract(
            "产物已落盘。\n```json\n"
            '{"status":"ready","mode":"final_review","remaining":"none"}'
            "\n```\n"
        )
        self.assertEqual(fenced["status"], "ready")
        self.assertEqual(fenced["mode"], "final_review")
        self.assertEqual(fenced["remaining"], "none")

        empty_remaining = _final_contract(
            "```json\n"
            '{"status":"ready","mode":"simple_edit","remaining":[]}'
            "\n```\n"
        )
        self.assertEqual(empty_remaining["remaining"], "none")
        self.assertEqual(_final_contract("remaining: []")["remaining"], "none")

        unresolved = _final_contract(
            "```json\n"
            '{"status":"ready","mode":"simple_edit",'
            '"remaining":["slide_01 still clips"]}'
            "\n```\n"
        )
        self.assertNotEqual(unresolved["remaining"], "none")

        # Structured parsing must not turn a prose mention into a verdict.
        self.assertEqual(_final_contract("我将返回 **status: ready** 作为最终结论。"), {})

    def test_research_contract_accepts_bold_label_colons_and_real_output(self):
        contract = _final_contract(
            "**status:** ready\n"
            "**output:** research/Research.md\n"
            "**unresolved:** none\n"
        )
        self.assertEqual(contract["status"], "ready")
        self.assertEqual(contract["output"], "research/Research.md")

        class Parent:
            pass

        class Child:
            pass

        with tempfile.TemporaryDirectory() as temporary:
            parent = Parent()
            parent.ws = temporary
            output = Path(temporary) / contract["output"]
            output.parent.mkdir(parents=True)
            output.write_text("# Research\n", encoding="utf-8")
            self.assertEqual(
                "",
                agent_core._child_contract_error(
                    parent, Child(), "research", contract
                ),
            )
            contract["status"] = "partial"
            contract.pop("unresolved", None)
            self.assertEqual(
                "",
                agent_core._child_contract_error(
                    parent, Child(), "research", contract
                ),
            )
            self.assertIn("已按正式产物继续", contract["validation_warning"])
            child = Child()
            child.ws = temporary
            self.assertEqual(
                agent_core._role_contract_closeout_issues(child, "research", contract),
                [],
            )

    def test_child_gets_one_bounded_closeout_when_contract_is_missing(self):
        class Trace:
            def __init__(self, root):
                self.sub_dir = str(root)

            def snapshot_inputs(self, *_args):
                return None

            def write(self, *_args):
                return None

        class Child:
            def __init__(self, root):
                self.role = "leaf"
                self.label = "image_demo"
                self.initial_user = "Image: prepare one asset"
                self.system = "system"
                self.tools = []
                self.max_turns = 3
                self.prompt_language = "zh"
                self.final_text = ""
                self.exit_reason = None
                self.n_renders = 0
                self.trace = Trace(root)
                self._usage_acc = {
                    "sum_input": 0, "sum_cache_read": 0,
                    "sum_cache_create": 0, "sum_output": 0, "n_turns": 0,
                }
                self.logs = []

            def config_snapshot(self):
                return {}

            def log(self, value):
                self.logs.append(value)

        def response(text):
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=text)],
                stop_reason="end_turn",
            )

        with tempfile.TemporaryDirectory() as temporary:
            child = Child(Path(temporary))
            replies = iter((
                response("我下一步会整理素材目录。"),
                response("status: ready\nassets: complete"),
            ))
            with mock.patch.object(agent_core, "_model_call", side_effect=lambda *_: next(replies)):
                self.assertTrue(agent_core.run_loop(child))
            self.assertEqual(child.exit_reason, "text_response")
            self.assertEqual(_final_contract(child.final_text)["status"], "ready")
            self.assertTrue(any("有限收口提醒" in line for line in child.logs))

    def test_slide_gets_bounded_closeout_when_pixels_are_stale(self):
        class Trace:
            def __init__(self, root):
                self.sub_dir = str(root)

            def snapshot_inputs(self, *_args):
                return None

            def write(self, *_args):
                return None

        class Child:
            def __init__(self, root):
                self.role = "leaf"
                self.label = "slide_group_a"
                self.initial_user = "Slide Group a [01]: build"
                self.system = "system"
                self.tools = []
                self.max_turns = 3
                self.prompt_language = "zh"
                self.final_text = ""
                self.exit_reason = None
                self.n_renders = 1
                self.trace = Trace(root / "_trace")
                self.ws = str(root)
                self._assigned_pages = [1]
                self.vision_paths = ["renders/slide_01.png"]
                self._dirty_visual_sources = set()
                self._usage_acc = {
                    "sum_input": 0, "sum_cache_read": 0,
                    "sum_cache_create": 0, "sum_output": 0, "n_turns": 0,
                }
                self.logs = []

            def config_snapshot(self):
                return {}

            def log(self, value):
                self.logs.append(value)

        def response(text):
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=text)],
                stop_reason="end_turn",
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "slides").mkdir()
            (root / "renders").mkdir()
            html = root / "slides/slide_01.html"
            png = root / "renders/slide_01.png"
            html.write_text("changed after inspection", encoding="utf-8")
            png.write_bytes(b"old pixels")
            os.utime(png, ns=(2_000_000_000, 2_000_000_000))
            os.utime(html, ns=(3_000_000_000, 3_000_000_000))
            child = Child(root)
            child.vision_evidence = {
                "renders/slide_01.png": {
                    "sha256": hashlib.sha256(b"old pixels").hexdigest(),
                    "mtime_ns": 2_000_000_000,
                }
            }
            replies = iter((
                response("status: ready\npages: 01"),
                response("status: ready\npages: 01"),
            ))
            with mock.patch.object(agent_core, "_model_call", side_effect=lambda *_: next(replies)):
                self.assertTrue(agent_core.run_loop(child))
            self.assertTrue(any("最终像素证据未闭环" in line for line in child.logs))
            self.assertEqual(
                agent_core._slide_pixel_state(child, [1])["stale_pages"], [1]
            )

            png.write_bytes(b"final pixels")
            os.utime(png, ns=(4_000_000_000, 4_000_000_000))
            child.vision_evidence["renders/slide_01.png"] = {
                "sha256": hashlib.sha256(b"final pixels").hexdigest(),
                "mtime_ns": 4_000_000_000,
            }
            self.assertEqual(
                agent_core._slide_pixel_state(child, [1]),
                {
                    "inspected_pages": [1], "missing_pages": [],
                    "stale_pages": [], "dirty_sources": [],
                },
            )

    def test_review_gets_closeout_for_incomplete_ready_contract(self):
        class Trace:
            def __init__(self, root):
                self.sub_dir = str(root)

            def snapshot_inputs(self, *_args):
                return None

            def write(self, *_args):
                return None

        class Child:
            def __init__(self, root):
                self.role = "leaf"
                self.label = "review"
                self.initial_user = "Review: mode=final_review"
                self._expected_review_mode = "final_review"
                self.system = "system"
                self.tools = []
                self.max_turns = 3
                self.prompt_language = "zh"
                self.final_text = ""
                self.exit_reason = None
                self.n_renders = 0
                self.trace = Trace(root)
                self._usage_acc = {
                    "sum_input": 0, "sum_cache_read": 0,
                    "sum_cache_create": 0, "sum_output": 0, "n_turns": 0,
                }
                self.logs = []

            def config_snapshot(self):
                return {}

            def log(self, value):
                self.logs.append(value)

        def response(text):
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=text)],
                stop_reason="end_turn",
            )

        with tempfile.TemporaryDirectory() as temporary:
            child = Child(Path(temporary))
            replies = iter((
                response("status: ready\nfinal_pixels_inspected: yes\nremaining: none"),
                response(
                    "status: ready\nmode: final_review\n"
                    "content_fidelity: not-applicable\ndiagnosed_pages: all\n"
                    "refine_rounds: 0\nfinal_pixels_inspected: yes\n"
                    "speech_aligned: yes\nremaining: none"
                ),
            ))
            with mock.patch.object(agent_core, "_model_call", side_effect=lambda *_: next(replies)):
                self.assertTrue(agent_core.run_loop(child))
            contract = _final_contract(child.final_text)
            self.assertEqual(contract["mode"], "final_review")
            self.assertEqual(contract["diagnosed_pages"], "all")
            self.assertTrue(any("mode: final_review" in line for line in child.logs))

    def test_localized_standalone_review_status_is_safe_fallback(self):
        contract = _final_contract(
            "所有交付门通过。\n\n**状态:ready**\n"
        )
        self.assertEqual(contract["status"], "ready")
        self.assertEqual(_final_contract("Review 已返回 ready"), {})
        self.assertEqual(_final_contract("当前还 not ready"), {})

    def test_child_receives_role_card_path_not_injected_card_body(self):
        review = agent_core._role_card_context(
            str(Path(__file__).resolve().parents[3] / "skills"), "review"
        )
        self.assertIn("skills/mural-presenter/subagents/review.md", review)
        self.assertIn("read_file", review)
        self.assertNotIn("<role-card", review)
        self.assertNotIn("final_pixels_inspected", review)
        english = agent_core._role_card_context("/tmp/skills", "image", "en")
        self.assertIn("Your only role-card path", english)
        self.assertIn("skills/mural-presenter/subagents/image.md", english)
        self.assertNotIn("你的唯一角色卡", english)
        self.assertNotIn("必须读取 `skills/mural-presenter/SKILL.md`", infer.SUBAGENT_SYSTEM)
        self.assertIn("不要通读根 SKILL.md", infer.SUBAGENT_SYSTEM)
        self.assertIn("Do not scan the root SKILL.md", infer.SUBAGENT_SYSTEM_EN)

    def test_query_language_selects_frozen_skill_package(self):
        self.assertEqual(
            infer.SKILL_BY_LANGUAGE[agent_core._infer_prompt_language("请制作一份五页演示")],
            "mural-presenter",
        )
        self.assertEqual(
            infer.SKILL_BY_LANGUAGE[
                agent_core._infer_prompt_language("Create a five-slide presentation")
            ],
            "mural-presenter",
        )

    def test_child_language_contract_is_explicit_and_plan_owned(self):
        chinese = agent_core._child_language_contract("zh")
        self.assertIn("Response language: 中文", chinese)
        self.assertIn("reasoning/thinking", chinese)
        self.assertIn("Deliverable language", chinese)
        self.assertIn("plan/deck.md", chinese)

        english = agent_core._child_language_contract("en")
        self.assertIn("Response language: English", english)
        self.assertIn("reasoning/thinking", english)
        self.assertIn("plan/deck.md", english)

    def test_slide_and_review_always_receive_vision(self):
        for label, goal in (
            ("slide-bookends", "Slide Group bookends [01,12]: build pages"),
            ("review", "Review: mode=final_review"),
        ):
            task = agent_core._normalize_task({
                "label": label,
                "goal": goal,
                "toolsets": ["file"],
            })
            self.assertIn("vision", task["toolsets"])
            self.assertIn("terminal", task["toolsets"])

    def test_slide_cannot_inherit_image_acquisition_toolsets(self):
        task = agent_core._normalize_task({
            "label": "slide_group_hero",
            "goal": "Slide Group hero [01,02]: build pages",
            "toolsets": ["file", "terminal", "vision", "web", "image_gen"],
        })
        self.assertNotIn("image_gen", task["toolsets"])
        self.assertNotIn("web", task["toolsets"])
        self.assertTrue({"file", "terminal", "vision"}.issubset(task["toolsets"]))

    def test_role_label_is_derived_when_model_omits_it(self):
        task = agent_core._normalize_task({
            "goal": "Slide Group dividers [03,09]: build transitions",
            "toolsets": ["file", "terminal"],
        })
        self.assertEqual(task["label"], "slide_group_dividers")
        self.assertIn("vision", task["toolsets"])

        single = agent_core._normalize_task({
            "goal": "Slide 7: finish the process page",
            "toolsets": ["file", "terminal"],
        })
        self.assertEqual(single["label"], "slide_07")
        self.assertIn("vision", single["toolsets"])

        review = agent_core._normalize_task({
            "goal": "Final Review: inspect the completed deck",
            "toolsets": ["file", "terminal"],
        })
        self.assertEqual(review["label"], "review")
        self.assertIn("vision", review["toolsets"])

        image = agent_core._normalize_task({
            "goal": "[image_02] prepare the planned photographic assets",
            "toolsets": ["file"],
        })
        self.assertEqual(image["label"], "image_02")
        self.assertTrue(
            {"file", "terminal", "web", "image_gen", "vision"}.issubset(
                image["toolsets"]
            )
        )

        named_image = agent_core._normalize_task({
            "goal": "[image-applications] prepare four real photographs",
            "toolsets": ["file"],
        })
        self.assertEqual(named_image["label"], "image_applications")

    def test_transparent_image_ready_requires_alpha_cutout_and_exact_vision(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "assets").mkdir()
            cutout = root / "assets/hero-cutout.png"
            Image.new("RGBA", (32, 32), (30, 80, 120, 0)).save(cutout)
            with Image.open(cutout) as image:
                pixels = image.load()
                for y in range(8, 24):
                    for x in range(8, 24):
                        pixels[x, y] = (30, 80, 120, 255)
                image.save(cutout)
            task = {"goal": "[image-cover] subject_only: true", "context": ""}
            contract = {"status": "ready"}
            final = (
                "status: ready\n"
                "transparent_assets: assets/hero-cutout.png\n"
            )
            self.assertIsNone(
                agent_core._image_transparency_error(
                    root,
                    task,
                    "image_cover",
                    contract,
                    final,
                    ["assets/hero-cutout.png"],
                )
            )
            self.assertIn(
                "未用 vision_analyze",
                agent_core._image_transparency_error(
                    root, task, "image_cover", contract, final, []
                ),
            )
            inferred = (
                "status: ready\n"
                "已交付并检查透明素材 `assets/hero-cutout.png`。\n"
            )
            self.assertIsNone(
                agent_core._image_transparency_error(
                    root,
                    task,
                    "image_cover",
                    contract,
                    inferred,
                    ["assets/hero-cutout.png"],
                )
            )

    def test_transparent_image_ready_rejects_rgb_or_unlisted_cutout(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "assets").mkdir()
            Image.new("RGB", (32, 32), "white").save(root / "assets/hero.png")
            task = {"goal": "[image-cover] subject_only: true", "context": ""}
            contract = {"status": "ready"}
            error = agent_core._image_transparency_error(
                root,
                task,
                "image_cover",
                contract,
                "status: ready\npath: assets/hero.png\n",
                ["assets/hero.png"],
            )
            self.assertIn("缺少 transparent_assets", error)

        named_group = agent_core._normalize_task({
            "goal": "[slide-bookends] build the cover and closing",
            "toolsets": ["file", "terminal"],
        })
        self.assertEqual(named_group["label"], "slide_group_bookends")
        self.assertIn("vision", named_group["toolsets"])

        named_review = agent_core._normalize_task({
            "goal": "[review-final] inspect and finish the deck",
            "toolsets": ["file", "terminal"],
        })
        self.assertEqual(named_review["label"], "review")
        self.assertIn("vision", named_review["toolsets"])

        generic_label = agent_core._normalize_task({
            "label": "child_01",
            "goal": "[image-motif] generate the cover motif",
            "toolsets": ["file"],
        })
        self.assertEqual(generic_label["label"], "image_motif")

    def test_pixel_acceptance_requires_real_vision_calls(self):
        workers = [
            {"label": "slide-bookends", "clean": True, "vision_calls": 1},
            {"label": "review", "clean": True, "vision_calls": 1,
             "contract": {
                 "status": "ready", "mode": "final_review",
                 "content_fidelity": "not-applicable", "diagnosed_pages": "all",
                 "final_pixels_inspected": "yes", "speech_aligned": "yes",
                 "remaining": "none",
             }},
        ]
        self.assertEqual(infer._pixel_review_acceptance(workers), (True, "ok"))
        workers[0]["vision_calls"] = 0
        ok, reason = infer._pixel_review_acceptance(workers)
        self.assertFalse(ok)
        self.assertIn("Slide", reason)

    def test_pixel_acceptance_requires_every_assigned_slide_page(self):
        workers = [
            {"label": "slide-dividers", "clean": True, "vision_calls": 2,
             "assigned_pages": [4, 9, 14], "inspected_pages": [4, 14]},
            {"label": "review", "clean": True, "vision_calls": 4,
             "contract": {
                 "status": "ready", "mode": "final_review",
                 "content_fidelity": "not-applicable", "diagnosed_pages": "all",
                 "final_pixels_inspected": "yes", "speech_aligned": "yes",
                 "remaining": "none",
             }},
        ]
        ok, reason = infer._pixel_review_acceptance(workers)
        self.assertFalse(ok)
        self.assertIn("slide-dividers:09", reason)

    def test_pixel_acceptance_requires_complete_final_review_contract(self):
        workers = [
            {"label": "slide-bookends", "clean": True, "vision_calls": 1},
            {"label": "review", "clean": True, "vision_calls": 8,
             "contract": {
                 "status": "ready", "mode": "final_review",
                 "content_fidelity": "not-applicable", "diagnosed_pages": "all",
                 "final_pixels_inspected": "yes", "speech_aligned": "yes",
                 "remaining": "none",
             }},
        ]
        self.assertEqual(infer._pixel_review_acceptance(workers), (True, "ok"))
        workers[-1]["contract"].pop("final_pixels_inspected")
        ok, reason = infer._pixel_review_acceptance(workers)
        self.assertFalse(ok)
        self.assertIn("final_pixels_inspected", reason)

    def test_simple_edit_uses_machine_refine_count_without_false_rejection(self):
        workers = [{
            "label": "review", "kind": "review", "clean": True,
            "vision_calls": 1, "machine_refine_rounds": 1,
            "contract": {
                "status": "ready", "mode": "simple_edit",
                "content_fidelity": "not-applicable",
                "final_pixels_inspected": "yes", "speech_aligned": "yes",
                "remaining": "none", "refine_rounds": "0",
            },
        }]
        self.assertEqual(
            infer._pixel_review_acceptance(
                workers, allow_review_only=True
            ),
            (True, "ok"),
        )

    def test_latest_bounded_review_can_accept_repaired_pixels(self):
        contract = {
            "status": "ready", "mode": "final_review",
            "content_fidelity": "not-applicable", "diagnosed_pages": "all",
            "final_pixels_inspected": "yes", "speech_aligned": "yes",
            "remaining": "none",
        }
        workers = [
            {"label": "slide-bookends", "clean": True, "vision_calls": 1},
            {"label": "review", "kind": "review", "clean": False,
             "vision_calls": 1, "contract": {"status": "blocked"}},
            {"label": "review_r2", "kind": "review", "clean": True,
             "vision_calls": 1, "contract": contract},
        ]
        self.assertEqual(infer._pixel_review_acceptance(workers), (True, "ok"))

    def test_final_review_must_actually_view_every_contact_group(self):
        contract = {
            "status": "ready", "mode": "final_review",
            "content_fidelity": "not-applicable", "diagnosed_pages": "all",
            "final_pixels_inspected": "yes", "speech_aligned": "yes",
            "remaining": "none",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "renders").mkdir()
            (root / "slides").mkdir()
            (root / "_trace").mkdir()
            (root / "base.css").write_text(".slide{}", encoding="utf-8")
            for page in range(1, 5):
                (root / f"slides/slide_{page:02d}.html").write_text(
                    f"<section class='slide'>{page}</section>", encoding="utf-8"
                )
                (root / f"renders/slide_{page:02d}.png").write_bytes(f"png-{page}".encode())
            for group in (1, 2):
                (root / f"renders/contact-sheet-review-{group:02d}.png").write_bytes(
                    f"contact-{group}".encode()
                )
            (root / "renders/review-contact.json").write_text(json.dumps({
                "full": {
                    "pages": [1, 2, 3, 4],
                    "groups": [
                        {"path": "renders/contact-sheet-review-01.png", "pages": [1, 2]},
                        {"path": "renders/contact-sheet-review-02.png", "pages": [3, 4]},
                    ],
                },
            }), encoding="utf-8")
            (root / "_trace/review-issues.md").write_text(
                "diagnosed_pages: all\nremaining: none\n", encoding="utf-8"
            )
            workers = [
                {"label": "slide-bookends", "clean": True, "vision_calls": 1},
                {"label": "review", "kind": "review", "clean": True,
                 "vision_calls": 1,
                 "vision_paths": ["renders/contact-sheet-review-01.png"],
                 "vision_evidence": {
                     "renders/contact-sheet-review-01.png": {
                         "sha256": hashlib.sha256(b"contact-1").hexdigest(),
                         "mtime_ns": (root / "renders/contact-sheet-review-01.png").stat().st_mtime_ns,
                     },
                 },
                 "contract": contract},
            ]
            ok, reason = infer._pixel_review_acceptance(workers, ws=str(root))
            self.assertFalse(ok)
            self.assertIn("缺页", reason)
            workers[-1]["vision_paths"].append("renders/contact-sheet-review-02.png")
            workers[-1]["vision_evidence"]["renders/contact-sheet-review-02.png"] = {
                "sha256": hashlib.sha256(b"contact-2").hexdigest(),
                "mtime_ns": (root / "renders/contact-sheet-review-02.png").stat().st_mtime_ns,
            }
            self.assertEqual(
                infer._pixel_review_acceptance(workers, ws=str(root)),
                (True, "ok"),
            )
            # An intermediate page-worker advisory must not override fresh
            # artifacts plus a task-level final Review that returned ready.
            workers[0].update({
                "clean": False,
                "assigned_pages": [1, 2, 3, 4],
                "inspected_pages": [1, 2, 3, 4],
                "vision_calls": 4,
            })
            self.assertEqual(
                infer._pixel_review_acceptance(workers, ws=str(root)),
                (True, "ok"),
            )

    def test_research_is_singleton_and_review_has_bounded_retries(self):
        class Parent:
            def __init__(self):
                import threading
                self._spawn_lock = threading.Lock()
                self._role_spawn_count = {}
                self.worker_recs = []

        parent = Parent()
        research = [agent_core._normalize_task({
            "goal": "Research: verify the named product", "toolsets": ["file", "web"]
        })]
        # First Research dispatch is allowed.
        self.assertIsNone(agent_core._reserve_singleton_roles(parent, research))
        # A first attempt that FAILED (blocked, not clean) may be recovered once.
        parent.worker_recs.append({
            "label": "research", "kind": "research", "clean": False,
            "contract": {"status": "blocked"},
        })
        self.assertIsNone(agent_core._reserve_singleton_roles(parent, research))
        # Bounded: after MAX_RESEARCH_ATTEMPTS dispatches the slot is exhausted.
        self.assertIn("上限", agent_core._reserve_singleton_roles(parent, research))
        # A succeeded Research blocks a redundant second active instance.
        success_parent = Parent()
        self.assertIsNone(agent_core._reserve_singleton_roles(success_parent, research))
        success_parent.worker_recs.append({
            "label": "research", "kind": "research", "clean": True,
            "contract": {"status": "ready"},
        })
        self.assertIn("单例", agent_core._reserve_singleton_roles(success_parent, research))

        review_batch = [
            agent_core._normalize_task({"goal": "Final Review: inspect all pages"}),
            agent_core._normalize_task({"goal": "Review: mode=simple_edit"}),
        ]
        self.assertIn("同一批次", agent_core._reserve_singleton_roles(parent, review_batch))

        one_review = [agent_core._normalize_task({
            "label": "review", "goal": "Final Review: inspect all pages"
        })]
        self.assertIsNone(agent_core._reserve_singleton_roles(parent, one_review))
        parent.worker_recs.append({
            "label": "review", "kind": "review", "clean": False,
            "contract": {"status": "blocked"},
        })
        self.assertIsNone(agent_core._reserve_singleton_roles(parent, one_review))
        parent.worker_recs.append({
            "label": "review_r2", "kind": "review", "clean": False,
            "contract": {"status": "blocked"},
        })
        self.assertIsNone(agent_core._reserve_singleton_roles(parent, one_review))
        self.assertIn("上限", agent_core._reserve_singleton_roles(parent, one_review))

    def test_blocked_slide_repair_restores_last_viewed_baseline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "slides").mkdir()
            (root / "renders").mkdir()
            source = root / "slides/slide_15.html"
            render = root / "renders/slide_15.png"
            source.write_text("verified", encoding="utf-8")
            render.write_bytes(b"verified-pixels")
            worker = SimpleNamespace(
                ws=str(root),
                label="slide_group_comparison",
                vision_paths=["renders/slide_15.png"],
            )
            tools._snapshot_verified_slide(worker, "slides/slide_15.html")
            source.write_text("regressed", encoding="utf-8")
            render.write_bytes(b"regressed-pixels")

            restored = tools.restore_verified_slides(worker)

            self.assertEqual(restored, [15])
            self.assertEqual(source.read_text(encoding="utf-8"), "verified")
            self.assertEqual(render.read_bytes(), b"verified-pixels")

    def test_image_asset_review_cannot_consume_review_singleton(self):
        import threading

        class Parent:
            def __init__(self):
                self._spawn_lock = threading.Lock()
                self._role_spawn_count = {}

        image = agent_core._normalize_task({
            "label": "image_science",
            "goal": "[Image] generate assets, then run deck.py asset-review",
        })
        self.assertEqual(agent_core._task_kind(image["label"], image["goal"]), "image")

        parent = Parent()
        self.assertIsNone(agent_core._reserve_singleton_roles(parent, [image]))
        self.assertNotIn("review", parent._role_spawn_count)

        final_review = agent_core._normalize_task({
            "label": "review",
            "goal": "Review: mode=final_review; inspect all pages",
        })
        self.assertIsNone(agent_core._reserve_singleton_roles(parent, [final_review]))
        self.assertEqual(parent._role_spawn_count.get("review"), 1)

    def test_stall_finalization_blocks_more_evidence_tools(self):
        class Agent:
            def __init__(self):
                self._finalization_only = True
                self.prompt_language = "zh"
                self.extra_tools = {}

        result = tools.dispatch(
            Agent(), "vision_analyze", {"image_url": "unchanged.png"}
        )
        self.assertIn("停滞收口阶段", result)
        self.assertIn("收口产物", result)

    def test_review_stall_closeout_allows_both_canonical_artifacts_only(self):
        class Agent:
            def __init__(self, root):
                self.ws = str(root)
                self._finalization_only = True
                self._finalization_role = "review"
                self.prompt_language = "zh"
                self.extra_tools = {}
                self.forbid_write_prefixes = []
                self.protected_runtime_paths = []

            def safe(self, path):
                candidate = os.path.normpath(os.path.join(self.ws, path))
                root = os.path.normpath(self.ws)
                if candidate != root and not candidate.startswith(root + os.sep):
                    raise ValueError("outside workspace")
                return candidate

            def writable(self, _path):
                return True

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            agent = Agent(root)

            issues = tools.dispatch(agent, "write_file", {
                "path": "_trace/review-issues.md",
                "content": "status: ready\nremaining: none\n",
            })
            fidelity_path = root / "_trace" / "content-fidelity.md"
            fidelity = tools.dispatch(agent, "write_file", {
                # Absolute paths inside the workspace are valid tool input and
                # must canonicalize to the same closeout artifact.
                "path": str(fidelity_path),
                "content": "# Content fidelity\n\ncontent_fidelity: pass\n",
            })
            blocked_slide = tools.dispatch(agent, "write_file", {
                "path": "slides/slide_01.html",
                "content": "must not be written",
            })
            escape_path = root.parent / f"escape-{root.name}.md"
            blocked_traversal = tools.dispatch(agent, "write_file", {
                "path": f"../{escape_path.name}",
                "content": "must not escape",
            })

            self.assertIn("已写入", issues)
            self.assertIn("已写入", fidelity)
            self.assertTrue((root / "_trace" / "review-issues.md").is_file())
            self.assertTrue(fidelity_path.is_file())
            self.assertIn("停滞收口阶段", blocked_slide)
            self.assertIn("停滞收口阶段", blocked_traversal)
            self.assertFalse((root / "slides" / "slide_01.html").exists())
            self.assertFalse(escape_path.exists())

    def test_review_stall_closeout_can_patch_content_fidelity_report(self):
        class Agent:
            def __init__(self, root):
                self.ws = str(root)
                self._finalization_only = True
                self._finalization_role = "review"
                self.prompt_language = "en"
                self.extra_tools = {}
                self.forbid_write_prefixes = []
                self.protected_runtime_paths = []

            def safe(self, path):
                candidate = os.path.normpath(os.path.join(self.ws, path))
                root = os.path.normpath(self.ws)
                if candidate != root and not candidate.startswith(root + os.sep):
                    raise ValueError("outside workspace")
                return candidate

            def writable(self, _path):
                return True

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "_trace" / "content-fidelity.md"
            report.parent.mkdir(parents=True)
            report.write_text("content_fidelity: pending\n", encoding="utf-8")
            result = tools.dispatch(Agent(root), "patch", {
                "path": "_trace/content-fidelity.md",
                "old_string": "pending",
                "new_string": "pass",
            })

            self.assertIn("已编辑", result)
            self.assertEqual(
                report.read_text(encoding="utf-8"),
                "content_fidelity: pass\n",
            )

    def test_stall_closeout_does_not_force_clean_review_to_blocked(self):
        class Parent:
            pass

        class Child:
            # Legacy runs may still carry this attribute in memory.  Runtime
            # stall protection must not override a truthful evidence-backed
            # contract returned during closeout.
            _stall_forced_status = "blocked"

        self.assertEqual("", agent_core._child_contract_error(
            Parent(), Child(), "review", {"status": "ready"}
        ))

    def test_child_handoff_is_structured_and_not_240_char_truncated(self):
        class Parent:
            pass

        class Child:
            pass

        with tempfile.TemporaryDirectory() as temporary:
            parent = Parent()
            parent.ws = temporary
            child = Child()
            child.final_text = (
                "status: ready\noutput: research/research.md\nkey_findings: "
                + "关键发现" * 100
            )
            child.exit_reason = "text_response"
            child.n_renders = 0
            child.n_vision_calls = 0
            child.last_shot = None
            child.trace = type("Trace", (), {
                "sub_dir": str(Path(temporary) / "_trace/subagents/research")
            })()
            Path(child.trace.sub_dir).mkdir(parents=True)

            result = agent_core._child_handoff(
                parent, child, "research", True, _final_contract(child.final_text)
            )
            self.assertNotIn("summary", result)
            self.assertEqual(result["artifacts"], ["research/research.md"])
            handoff = json.loads(
                (Path(temporary) / result["handoff_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(handoff["final_response"], child.final_text)
            self.assertGreater(len(handoff["final_response"]), 240)

    def test_orchestrator_cannot_reload_full_child_messages(self):
        class Agent:
            role = "orchestrator"

            def read_path(self, path):
                raise AssertionError("blocked path must not reach filesystem")

        response = tools.read_file(
            Agent(), "_trace/subagents/material/messages.json"
        )
        self.assertIn("完整轨迹", response)
        self.assertIn("handoff.json", response)

    def test_consumed_vision_images_leave_active_context_without_lifetime_quota(self):
        messages = [{
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "vision-1",
                "content": [
                    {"type": "text", "text": "renders/slide_01.png"},
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/png", "data": "AAAA"
                    }},
                ],
            }],
        }]
        self.assertEqual(agent_core._release_consumed_vision_images(messages), 1)
        result = messages[0]["content"][0]["content"]
        self.assertFalse(any(item.get("type") == "image" for item in result))
        self.assertIn("已从活跃上下文释放", result[-1]["text"])

        # 释放是幂等的；后续批次仍可继续注入新图。
        self.assertEqual(agent_core._release_consumed_vision_images(messages), 0)

    def test_grouped_slide_budget_scales_with_owned_pages(self):
        single = {"goal": "Slide 7: finish one page", "context": ""}
        four_pages = {
            "goal": "Slide Group content-02 [10, 11, 12, 13]: build the group",
            "context": "",
        }
        six_pages = {
            "goal": "[slide-content] build the assigned pages",
            "context": "read plan/slide_03.md through plan/slide_08.md: "
                       "plan/slide_03.md plan/slide_04.md plan/slide_05.md "
                       "plan/slide_06.md plan/slide_07.md plan/slide_08.md",
        }

        self.assertEqual(agent_core._slide_group_page_count(single), 1)
        self.assertEqual(agent_core._slide_group_pages(single), [7])
        self.assertEqual(agent_core._slide_group_pages(four_pages), [10, 11, 12, 13])
        self.assertEqual(agent_core._slide_group_page_count(four_pages), 4)
        self.assertEqual(agent_core._slide_group_page_count(six_pages), 6)
        self.assertEqual(agent_core._slide_turn_budget(single), 36)
        self.assertEqual(agent_core._slide_turn_budget(four_pages), 72)
        self.assertEqual(agent_core._slide_turn_budget(six_pages), 96)
        self.assertEqual(agent_core._slide_timeout_budget(single), 900)
        self.assertEqual(agent_core._slide_timeout_budget(six_pages), 1800)

    def test_grouped_slide_budget_has_an_anomaly_backstop(self):
        task = {"goal": "Slide Group oversized [01-20]: build pages", "context": ""}
        self.assertEqual(agent_core._slide_group_page_count(task), 20)
        self.assertEqual(agent_core._slide_turn_budget(task), 120)
        self.assertEqual(agent_core._slide_timeout_budget(task), 2400)

    def test_slide_page_parser_accepts_common_legacy_prose(self):
        chinese = {
            "goal": "Slide Group dividers：负责第 03、09、15 页，并保持章节页呼应",
            "context": "",
        }
        english = {
            "goal": "Slide Group evidence: build pages 01-03,05",
            "context": "",
        }
        self.assertEqual(agent_core._slide_group_pages(chinese), [3, 9, 15])
        self.assertEqual(agent_core._slide_group_pages(english), [1, 2, 3, 5])

    def test_frozen_plan_canonicalizes_per_page_tasks_back_into_groups(self):
        class Parent:
            def __init__(self, root):
                self.ws = str(root)
                self.cfg = {}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            for page, group in ((1, "bookends"), (2, "content"), (3, "bookends")):
                (root / f"plan/slide_{page:02d}.md").write_text(
                    f"# Slide {page:02d}\n- production_group: {group}\n",
                    encoding="utf-8",
                )
            tasks = [
                agent_core._normalize_task({
                    "goal": f"Slide {page}: complete the page",
                    "label": f"slide_{page:02d}",
                })
                for page in (1, 2, 3)
            ]
            canonical, error = agent_core._canonicalize_slide_tasks(Parent(root), tasks)

            self.assertIsNone(error)
            self.assertEqual(len(canonical), 2)
            by_label = {task["label"]: task for task in canonical}
            self.assertEqual(
                agent_core._slide_group_pages(by_label["slide_group_bookends"]),
                [1, 3],
            )
            self.assertEqual(
                agent_core._slide_group_pages(by_label["slide_group_content"]),
                [2],
            )
            self.assertIn("Original directives", by_label["slide_group_bookends"]["goal"])

    def test_frozen_group_id_can_restore_pages_when_prose_omits_them(self):
        class Parent:
            def __init__(self, root):
                self.ws = str(root)
                self.cfg = {}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            for page in (1, 3):
                (root / f"plan/slide_{page:02d}.md").write_text(
                    f"# Slide {page:02d}\n- production_group: bookends\n",
                    encoding="utf-8",
                )
            task = agent_core._normalize_task({
                "goal": "Slide Group bookends: 完成封面与结尾",
                "label": "slide_group_bookends",
            })
            canonical, error = agent_core._canonicalize_slide_tasks(Parent(root), [task])

            self.assertIsNone(error)
            self.assertEqual(len(canonical), 1)
            self.assertEqual(agent_core._slide_group_pages(canonical[0]), [1, 3])

    def test_one_frozen_group_can_be_dispatched_per_call(self):
        class Parent:
            def __init__(self, root):
                self.ws = str(root)
                self.cfg = {}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            for page, group in ((1, "bookends"), (2, "content"), (3, "bookends")):
                (root / f"plan/slide_{page:02d}.md").write_text(
                    f"# Slide {page:02d}\n- production_group: {group}\n",
                    encoding="utf-8",
                )
            task = agent_core._normalize_task({
                "goal": "Slide Group bookends：负责封面与结尾",
                "label": "slide_group_bookends",
            })
            canonical, error = agent_core._canonicalize_slide_tasks(Parent(root), [task])

            self.assertIsNone(error)
            self.assertEqual(len(canonical), 1)
            self.assertEqual(canonical[0]["label"], "slide_group_bookends")
            self.assertEqual(agent_core._slide_group_pages(canonical[0]), [1, 3])

    def test_slide_page_ownership_is_complete_unique_and_reserved_before_dispatch(self):
        import threading

        class Parent:
            def __init__(self, root):
                self.ws = str(root)
                self.cfg = {}
                self._spawn_lock = threading.Lock()
                self._role_spawn_count = {}
                self._slide_page_owners = {}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            for page in range(1, 4):
                (root / f"plan/slide_{page:02d}.md").write_text("# plan", encoding="utf-8")
            parent = Parent(root)
            first_group = [agent_core._normalize_task({
                "goal": "Slide Group a [01,02]: build", "label": "slide_a"
            })]
            self.assertIsNone(agent_core._reserve_singleton_roles(parent, first_group))
            self.assertEqual(parent._slide_page_owners, {1: "slide_a", 2: "slide_a"})

            second_group = [agent_core._normalize_task({
                "goal": "Slide Group b [03]: build", "label": "slide_b"
            })]
            self.assertIsNone(agent_core._reserve_singleton_roles(parent, second_group))
            self.assertEqual(parent._slide_page_owners, {1: "slide_a", 2: "slide_a", 3: "slide_b"})

            duplicate = [agent_core._normalize_task({
                "goal": "Slide Group c [03]: build", "label": "slide_c"
            })]
            self.assertIn("已归属", agent_core._reserve_singleton_roles(parent, duplicate))

            gap_root = root / "gap"
            (gap_root / "plan").mkdir(parents=True)
            (gap_root / "plan/slide_01.md").write_text("# plan", encoding="utf-8")
            (gap_root / "plan/slide_03.md").write_text("# plan", encoding="utf-8")
            gap_parent = Parent(gap_root)
            gap_tasks = [agent_core._normalize_task({
                "goal": "Slide Group gap [01,03]: build", "label": "slide_gap"
            })]
            self.assertIn("连续", agent_core._reserve_singleton_roles(gap_parent, gap_tasks))

    def test_failed_slide_group_can_continue_only_under_same_owner(self):
        import threading

        class Parent:
            def __init__(self, root):
                self.ws = str(root)
                self.cfg = {}
                self._spawn_lock = threading.Lock()
                self._role_spawn_count = {}
                self._slide_page_owners = {}
                self.worker_recs = []

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            for page in (1, 2):
                (root / f"plan/slide_{page:02d}.md").write_text(
                    f"# Slide {page:02d}\n- production_group：chapter-a\n",
                    encoding="utf-8",
                )
            parent = Parent(root)
            first = [agent_core._normalize_task({
                "label": "slide_group_chapter-a",
                "goal": "Slide Group chapter-a [01,02]: build",
            })]
            self.assertIsNone(agent_core._reserve_singleton_roles(parent, first))
            parent.worker_recs.append({
                "label": "slide_group_chapter-a", "kind": "slide",
                "clean": False, "assigned_pages": [1, 2],
            })

            continuation = [agent_core._normalize_task({
                "label": "slide_group_chapter-a",
                "goal": "Slide Group chapter-a [01,02]: continue missing pixel closure",
            })]
            self.assertIsNone(agent_core._reserve_singleton_roles(parent, continuation))
            self.assertEqual(
                continuation[0].get("_resume_of"), "slide_group_chapter-a"
            )

            intruder = [agent_core._normalize_task({
                "label": "slide_group_other",
                "goal": "Slide Group other [01,02]: overwrite",
            })]
            self.assertIn("已归属", agent_core._reserve_singleton_roles(parent, intruder))

            records = [
                {
                    "label": "slide_group_chapter-a", "kind": "slide",
                    "clean": False, "assigned_pages": [1, 2],
                    "superseded_by": "slide_group_chapter-a_r2",
                },
                {
                    "label": "slide_group_chapter-a_r2", "kind": "slide",
                    "clean": True, "assigned_pages": [1, 2],
                },
            ]
            self.assertEqual(
                infer._slide_assignment_acceptance(root, records),
                (True, "ok"),
            )

    def test_frozen_production_groups_cannot_be_split_into_per_page_workers(self):
        import threading

        class Parent:
            def __init__(self, root):
                self.ws = str(root)
                self.cfg = {}
                self._spawn_lock = threading.Lock()
                self._role_spawn_count = {}
                self._slide_page_owners = {}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            for page, group in ((1, "bookends"), (2, "content"), (3, "bookends")):
                (root / f"plan/slide_{page:02d}.md").write_text(
                    f"# Slide {page:02d}\n- production_group：{group}\n",
                    encoding="utf-8",
                )
            split_parent = Parent(root)
            split = [
                agent_core._normalize_task({"goal": f"Slide {page}: build", "label": f"slide_{page:02d}"})
                for page in (1, 2, 3)
            ]
            self.assertIn(
                "production_group",
                agent_core._reserve_singleton_roles(split_parent, split),
            )
            self.assertEqual(split_parent._slide_page_owners, {})

            grouped_parent = Parent(root)
            grouped = [
                agent_core._normalize_task({
                    "goal": "Slide Group bookends [01,03]: build", "label": "slide_bookends"
                }),
                agent_core._normalize_task({
                    "goal": "Slide Group content [02]: build", "label": "slide_content"
                }),
            ]
            self.assertIsNone(agent_core._reserve_singleton_roles(grouped_parent, grouped))

            records = [
                {"label": "slide_bookends", "kind": "slide", "assigned_pages": [1, 3]},
                {"label": "slide_content", "kind": "slide", "assigned_pages": [2]},
            ]
            self.assertEqual(
                infer._slide_assignment_acceptance(root, records),
                (True, "ok"),
            )

    def test_worker_crash_is_persisted_as_blocked_record(self):
        import threading

        class Parent:
            def __init__(self, root):
                self.ws = str(root)
                self._spawn_lock = threading.Lock()
                self.worker_recs = []
                trace_dir = root / "_trace/orchestrator"
                trace_dir.mkdir(parents=True)
                self.trace = type("Trace", (), {"sub_dir": str(trace_dir)})()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = Parent(root)
            ticket = {"label": "slide_a", "recorded": False}
            task = {"label": "slide_a", "goal": "Slide Group a [01,02]: build"}
            record = agent_core._record_worker_failure(
                parent, task, ticket, "crashed", "RuntimeError: boom"
            )
            self.assertFalse(record["clean"])
            self.assertEqual(record["contract"]["status"], "blocked")
            self.assertEqual(record["assigned_pages"], [1, 2])
            self.assertEqual(len(parent.worker_recs), 1)
            failure = root / "_trace/worker-failures/slide_a.json"
            self.assertTrue(failure.is_file())

    def test_research_acceptance_prefers_artifact_over_optional_contract_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "research").mkdir()
            (root / "plan").mkdir()
            (root / "research/research.md").write_text("# evidence\nverified", encoding="utf-8")
            worker = {
                "label": "research", "kind": "research", "clean": True,
                "contract": {
                    "status": "partial", "output": "research/research.md",
                    "unresolved": "票房口径仍未核验",
                },
            }
            self.assertEqual(
                infer._research_acceptance(str(root), [worker]), (True, "ok")
            )
            worker["contract"].pop("unresolved")
            ok, reason = infer._research_acceptance(str(root), [worker])
            self.assertTrue(ok)
            self.assertIn("已按正式产物继续", reason)
            (root / "plan/grounded-knowledge.md").write_text(
                "## 未解决与使用边界\n票房口径仍未核验\n", encoding="utf-8"
            )
            worker["contract"]["status"] = "blocked"
            worker["clean"] = False
            ok, reason = infer._research_acceptance(str(root), [worker])
            self.assertTrue(ok)
            self.assertIn("已按正式产物继续", reason)
            (root / "research/research.md").unlink()
            self.assertFalse(infer._research_acceptance(str(root), [worker])[0])

    def test_grounding_is_required_only_after_an_evidence_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            self.assertEqual(
                infer._grounded_acceptance(
                    str(root), require_materials=False, research_workers=[]
                ),
                (True, "ok"),
            )
            self.assertFalse(
                infer._grounded_acceptance(
                    str(root), require_materials=False,
                    research_workers=[{"label": "research"}],
                )[0]
            )
            (root / "plan/grounded-knowledge.md").write_text(
                "# Grounded knowledge\n\n## Verified\nA sufficiently explicit handoff.\n",
                encoding="utf-8",
            )
            self.assertEqual(
                infer._grounded_acceptance(
                    str(root), require_materials=True, research_workers=[]
                ),
                (True, "ok"),
            )

    def test_downstream_dispatch_waits_for_grounding_handoff(self):
        import threading

        class Parent:
            def __init__(self, root):
                self.ws = str(root)
                self._spawn_lock = threading.Lock()
                self.worker_recs = [{
                    "label": "research", "kind": "research", "clean": True,
                    "contract": {"status": "ready"},
                }]
                self.generation_preferences = {}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            parent = Parent(root)
            tasks = [{"label": "slide_group_a", "goal": "Slide Group a [01]: build"}]
            error = agent_core._grounding_before_downstream_error(parent, tasks)
            self.assertIn("grounded-knowledge.md", error)
            (root / "plan/grounded-knowledge.md").write_text(
                "# Grounded knowledge\n\n## Verified\nA sufficiently explicit handoff.\n",
                encoding="utf-8",
            )
            self.assertIsNone(
                agent_core._grounding_before_downstream_error(parent, tasks)
            )

    def test_downstream_dispatch_uses_grounding_even_when_contract_is_dirty(self):
        import threading

        class Parent:
            def __init__(self, root):
                self.ws = str(root)
                self._spawn_lock = threading.Lock()
                self.worker_recs = [{
                    "label": "research", "kind": "research", "clean": False,
                    "contract": {"status": "partial", "output": "research/research.md"},
                }]
                self.generation_preferences = {}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "research").mkdir()
            (root / "plan").mkdir()
            (root / "research/research.md").write_text(
                "# Research\n\nUseful verified evidence.\n", encoding="utf-8"
            )
            (root / "plan/grounded-knowledge.md").write_text(
                "# Grounded knowledge\n\n## Verified\nUseful verified evidence.\n",
                encoding="utf-8",
            )
            tasks = [{"label": "image", "goal": "Image: prepare visual assets"}]
            self.assertIsNone(
                agent_core._grounding_before_downstream_error(Parent(root), tasks)
            )

    def test_revision_reuses_existing_attachment_grounding(self):
        import threading

        class Parent:
            def __init__(self, root):
                self.ws = str(root)
                self.cfg = {"_revision_mode": True}
                self._spawn_lock = threading.Lock()
                self.worker_recs = []
                self.generation_preferences = {"attachment_count": 1}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            parent = Parent(root)
            tasks = [{"label": "review", "goal": "Review: mode=simple_edit"}]
            error = agent_core._grounding_before_downstream_error(parent, tasks)
            self.assertIn("grounded-knowledge.md", error)
            (root / "plan/grounded-knowledge.md").write_text(
                "# Grounded knowledge\n\n## Verified\nExisting attachment evidence remains valid.\n",
                encoding="utf-8",
            )
            self.assertIsNone(
                agent_core._grounding_before_downstream_error(parent, tasks)
            )

    def test_planned_image_requires_ready_worker_and_resolvable_asset(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            (root / "assets").mkdir()
            (root / "plan/slide_01.md").write_text(
                "image_opportunity: real_required\nasset_id: hero-city\n", encoding="utf-8"
            )
            self.assertFalse(infer._image_acceptance(str(root), [])[0])
            worker = {
                "label": "image_city", "kind": "image", "clean": True,
                "contract": {"status": "ready"},
            }
            (root / "assets/hero.jpg").write_bytes(b"image")
            (root / "assets/catalog.json").write_text(json.dumps({
                "schema_version": 2,
                "assets": [{
                    "asset_id": "hero-city", "path": "assets/hero.jpg", "status": "ready"
                }],
            }), encoding="utf-8")
            self.assertEqual(
                infer._image_acceptance(
                    str(root), [], allow_existing_assets=True
                ),
                (True, "ok"),
            )
            self.assertEqual(
                infer._image_acceptance(str(root), [worker]), (True, "ok")
            )
            worker["contract"]["status"] = "blocked"
            worker["clean"] = False
            self.assertFalse(infer._image_acceptance(str(root), [worker])[0])
            retry = {
                "label": "image_city_r2", "kind": "image", "clean": True,
                "contract": {"status": "ready"},
            }
            self.assertEqual(
                infer._image_acceptance(str(root), [worker, retry]),
                (True, "ok"),
            )

    def test_slide_dispatch_waits_for_ready_image_manifest(self):
        import threading

        class Parent:
            def __init__(self, root):
                self.ws = str(root)
                self.cfg = {}
                self._spawn_lock = threading.Lock()
                self.worker_recs = []

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            (root / "assets").mkdir()
            (root / "plan/slide_01.md").write_text(
                "image_opportunity: real_required\nasset_id: hero-city\n",
                encoding="utf-8",
            )
            (root / "plan/slide_02.md").write_text(
                "image_opportunity：none（数据页，图表本身就是主视觉）\n",
                encoding="utf-8",
            )
            parent = Parent(root)
            slide = [{"label": "slide_group_a", "goal": "Slide Group a [01,02]: build"}]
            self.assertIn("Image Agent", agent_core._image_before_slide_error(parent, slide))
            self.assertIn(
                "不能同批委派",
                agent_core._image_before_slide_error(parent, [
                    {"label": "image_a", "goal": "Image: create hero-city"},
                    slide[0],
                ]),
            )
            (root / "assets/hero.jpg").write_bytes(b"image")
            (root / "assets/catalog.json").write_text(json.dumps({
                "assets": [{
                    "asset_id": "hero-city", "path": "assets/hero.jpg", "status": "ready"
                }],
            }), encoding="utf-8")
            parent.worker_recs.append({
                "label": "image_a", "kind": "image", "clean": True,
                "contract": {"status": "ready"},
            })
            self.assertIsNone(agent_core._image_before_slide_error(parent, slide))

    def test_fullwidth_asset_id_delimiter_passes_dispatch_and_acceptance(self):
        import threading

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            (root / "assets").mkdir()
            (root / "plan/slide_01.md").write_text(
                "# t\n\n## 视觉实现\n"
                "- medium：real photo\n"
                "- image_opportunity：real_required\n"
                "- presentation：framed-scene\n"
                "- asset_id：hero-city, supporting-city\n"
                "- asset_path：assets/hero.jpg\n",
                encoding="utf-8",
            )
            (root / "assets/hero.jpg").write_bytes(b"image")
            (root / "assets/supporting.jpg").write_bytes(b"image")
            (root / "assets/catalog.json").write_text(json.dumps({
                "assets": [
                    {
                        "asset_id": "hero-city",
                        "path": "assets/hero.jpg",
                        "status": "ready",
                    },
                    {
                        "asset_id": "supporting-city",
                        "path": "assets/supporting.jpg",
                        "status": "ready",
                    },
                ],
            }), encoding="utf-8")
            parent = SimpleNamespace(
                ws=str(root),
                cfg={},
                _spawn_lock=threading.Lock(),
                worker_recs=[{
                    "label": "image_a",
                    "kind": "image",
                    "clean": True,
                    "contract": {"status": "ready"},
                }],
            )
            slide = [{
                "label": "slide_group_a",
                "goal": "Slide Group a [01]: build",
            }]

            self.assertEqual(
                agent_core._slide_image_plan(str(root))[0]["asset_ids"],
                ["hero-city", "supporting-city"],
            )
            self.assertIsNone(
                agent_core._image_before_slide_error(parent, slide)
            )
            self.assertEqual(
                infer._planned_asset_ids(str(root)),
                ({"hero-city", "supporting-city"}, True),
            )

    def test_all_no_bitmap_plan_requires_reviewed_exception(self):
        import threading

        class Parent:
            def __init__(self, root):
                self.ws = str(root)
                self.cfg = {}
                self._spawn_lock = threading.Lock()
                self.worker_recs = []

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            for page in (1, 2):
                (root / f"plan/slide_{page:02d}.md").write_text(
                    "image_opportunity: none\n", encoding="utf-8"
                )
            slide = [{"label": "slide_group_a", "goal": "Slide Group a [01,02]: build"}]
            parent = Parent(root)
            self.assertIn(
                "image-strategy.json",
                agent_core._image_before_slide_error(parent, slide),
            )
            (root / "plan/image-strategy.json").write_text(json.dumps({
                "status": "bitmap_exception",
                "visible_subject_scan_complete": True,
                "exception_basis": "pure_chart",
                "exception_reason": "两页均为精确数值图表，额外位图会降低数据识别与比较准确性。",
                "reviewed_pages": [1, 2],
            }, ensure_ascii=False), encoding="utf-8")
            self.assertIsNone(agent_core._image_before_slide_error(parent, slide))
            self.assertEqual(infer._image_acceptance(str(root), []), (True, "ok"))

    def test_catalog_asset_id_active_vs_rejected_order_independent(self):
        # Same asset_id may appear as a rejected historical entry + an active
        # ready one; resolution must be order-independent and ignore rejected.
        import itertools
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "assets").mkdir()
            (root / "assets/hero.png").write_bytes(b"img")
            ready = {"asset_id": "hero", "path": "assets/hero.png", "status": "ready"}
            rejected = {"asset_id": "hero", "path": "assets/old.png", "status": "rejected"}
            for order in itertools.permutations([ready, rejected]):
                (root / "assets/catalog.json").write_text(
                    json.dumps({"assets": list(order)}), encoding="utf-8")
                self.assertIsNone(
                    agent_core._catalog_asset_error(str(root), {"hero"}),
                    f"order {[e['status'] for e in order]} should resolve ready",
                )
            # Only rejected → missing/not-ready.
            (root / "assets/catalog.json").write_text(
                json.dumps({"assets": [rejected]}), encoding="utf-8")
            self.assertIsNotNone(agent_core._catalog_asset_error(str(root), {"hero"}))
            # Two active ready entries for the same id → explicit ambiguity.
            dup = {"asset_id": "hero", "path": "assets/hero.png", "status": "ready"}
            (root / "assets/catalog.json").write_text(
                json.dumps({"assets": [ready, dup]}), encoding="utf-8")
            err = agent_core._catalog_asset_error(str(root), {"hero"})
            self.assertIsNotNone(err)
            self.assertIn("ambiguous", err)

    def test_image_opportunity_parser_shared_by_gate_and_synthesis(self):
        # Item #2: the pre-Slide dispatch gate (agent) and the synthesis final
        # acceptance must use the SAME no-bitmap decision, so `none` and the CJK
        # `无位图（说明）` read identically on both sides.
        needs = agent_core._image_opportunity_needs_bitmap
        for value, expected in (
            ("real_required（具名嘉宾）", True),
            ("generated_ok - stylized", True),
            ("none（数据页，图表即主视觉）", False),
            ("chart_only", False),
            ("无位图（纯数据表）", False),
            ("无需配图", False),
        ):
            self.assertEqual(needs(value), expected, value)
        # synthesis entry imports the same helper (no divergent copy).
        from core.agent import _image_opportunity_needs_bitmap as synthesis_side
        self.assertIs(synthesis_side, needs)

    def test_catalog_resolution_consistent_agent_and_synthesis(self):
        # The agent dispatch gate and the synthesis acceptance must agree on the
        # same catalog via the shared _catalog_active_by_id helper.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "assets").mkdir()
            (root / "plan").mkdir()
            (root / "assets/h.png").write_bytes(b"img")
            (root / "plan/slide_01.md").write_text(
                "image_opportunity: real_required\nasset_id: hero\n", encoding="utf-8")
            (root / "assets/catalog.json").write_text(json.dumps({"assets": [
                {"asset_id": "hero", "path": "assets/old.png", "status": "rejected"},
                {"asset_id": "hero", "path": "assets/h.png", "status": "ready"},
            ]}), encoding="utf-8")
            self.assertIsNone(agent_core._catalog_asset_error(str(root), {"hero"}))
            ok, _ = infer._image_acceptance(str(root), [{
                "label": "image_a", "kind": "image", "clean": True,
                "contract": {"status": "ready"},
            }])
            self.assertTrue(ok)

    def test_task_kind_explicit_tag_wins_over_incidental_prose(self):
        # An Image task whose goal prose incidentally contains "Slides:" or
        # "Review:" must stay image (and keep image_gen) — explicit tag/label wins.
        cases = [
            ("", "[image-hero] hero art; slides: 5 pages", "image"),
            ("", "[image] cover; Review: the palette first", "image"),
            ("image-concept", "concept; the review: notes say X", "image"),
        ]
        for label, goal, expected in cases:
            self.assertEqual(agent_core._task_kind(label, goal), expected, goal)
            norm = agent_core._normalize_task({"label": label, "goal": goal})
            self.assertIn("image_gen", norm["toolsets"], f"image_gen stripped for: {goal}")
            self.assertNotIn("image_gen",
                             agent_core._normalize_task(
                                 {"goal": "Slides: build cover"}).get("toolsets", []),
                             "a genuine unlabeled slide task keeps no image_gen")

    def test_task_kind_incidental_midtext_role_words_do_not_classify(self):
        # No label/tag; the role words appear mid-prose, not as a first-line
        # header → must resolve to "other" and NOT strip image_gen/web tools.
        for goal in (
            "Prepare visual assets for Slides: 1-3; include Review: notes",
            "Generate imagery.\nUse it for Slides: 1-3.",
            "Create the hero; the Review: team will check later",
        ):
            self.assertEqual(agent_core._task_kind("", goal), "other", goal)
            norm = agent_core._normalize_task({
                "goal": goal,
                "toolsets": ["file", "terminal", "web", "image_gen", "vision"],
            })
            self.assertIn("image_gen", norm["toolsets"], goal)
            self.assertIn("web", norm["toolsets"], goal)
        # But a first-line explicit role header still classifies.
        self.assertEqual(agent_core._task_kind("", "Slides: build the cover"), "slide")
        self.assertEqual(agent_core._task_kind("", "Review: inspect pages"), "review")
        self.assertEqual(agent_core._task_kind("", "Research: verify facts"), "research")

    def test_transparency_gate_ignores_material_words_and_field_name(self):
        # A physical "transparent" material description or the echoed contract
        # field name transparent_assets must NOT be read as a cutout requirement
        # (deck 449: "transparent acrylic water guides" wrongly blocked Slide).
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "assets").mkdir()
            for goal in (
                "[image-concept] render transparent acrylic water guides",
                "[image-hero] 生成 6 张 hero 图片，返回 transparent_assets 字段",
                "[image-concept] 材料需要透明感，玻璃质感",
                "[image-concept] 要透明的玻璃材质",
            ):
                task = {"goal": goal, "context": ""}
                self.assertIsNone(
                    agent_core._image_transparency_error(
                        root, task, "image_concept",
                        {"status": "ready"},
                        "status: ready\ntransparent_assets: not-required\n",
                        [],
                    ),
                    f"material/field text should not trigger: {goal}",
                )

    def test_transparency_gate_fires_on_genuine_requirements(self):
        # Explicit requirements — including the bare CJK "需要透明背景" and the
        # cut-out verbs — must still demand a verified cutout.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "assets").mkdir()
            (root / "assets/hero.png").write_bytes(b"rgb")
            for goal in (
                "[image-cover] subject_only: true",
                "[image-cover] presentation: subject-only",
                "[image-cover] expect_transparent: true",
                "[image-cover] 该素材需要透明背景",
                "[image-cover] 背景透明的主体",
                "[image-cover] 主体透明，悬浮在版面上",
                "[image-cover] 透明主体拼贴",
                "[image-cover] 需要抠图去背",
            ):
                err = agent_core._image_transparency_error(
                    root, {"goal": goal, "context": ""}, "image_cover",
                    {"status": "ready"},
                    "status: ready\npath: assets/hero.png\n",
                    ["assets/hero.png"],
                )
                self.assertIsNotNone(err, f"requirement not detected: {goal}")

    def test_slide_gate_recovers_from_disk_handoff_when_memory_stale(self):
        import threading

        class Parent:
            def __init__(self, root):
                self.ws = str(root)
                self.cfg = {}
                self._spawn_lock = threading.Lock()
                self.worker_recs = []

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            (root / "assets").mkdir()
            (root / "plan/slide_01.md").write_text(
                "image_opportunity: real_required\nasset_id: hero-city\n",
                encoding="utf-8",
            )
            (root / "assets/hero.jpg").write_bytes(b"image")
            (root / "assets/catalog.json").write_text(json.dumps({
                "assets": [{
                    "asset_id": "hero-city", "path": "assets/hero.jpg", "status": "ready"
                }],
            }), encoding="utf-8")
            parent = Parent(root)
            # In-memory record is stale (clean=False from a false-positive), and
            # the orchestrator did NOT hand-edit memory.
            parent.worker_recs.append({
                "label": "image_a", "kind": "image", "clean": False,
                "contract": {"status": "blocked"}, "attempt": 1, "ts": 1.0,
            })
            slide = [{"label": "slide_group_a", "goal": "Slide Group a [01]: build"}]
            self.assertIn(
                "未完成的 active Image 分片",
                agent_core._image_before_slide_error(parent, slide),
            )
            # A SAME-BASE higher-attempt handoff recovers it — normal delegate_task
            # reconciles disk truth into memory first, which supersedes the blocked
            # attempt.  (A different base could NEVER cross-heal it; see the
            # dedicated negative test below.)
            handoff_dir = root / "_trace/subagents/image_a_r2"
            handoff_dir.mkdir(parents=True)
            (handoff_dir / "handoff.json").write_text(json.dumps({
                "label": "image_a_r2", "clean": True,
                "contract": {"status": "ready"}, "kind": "image",
                "attempt": 2, "ts": 9.0,
            }), encoding="utf-8")
            agent_core._reconcile_worker_recs(parent)
            self.assertIsNone(agent_core._image_before_slide_error(parent, slide))

    def test_slide_gate_unrelated_disk_ready_does_not_heal_memory_blocked(self):
        # Cross-base leak: memory image_a active blocked + an unrelated disk
        # image-finalize clean+ready must NOT clear the gate, regardless of the
        # in-memory list order.
        import threading
        import itertools

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            (root / "assets").mkdir()
            (root / "plan/slide_01.md").write_text(
                "image_opportunity: real_required\nasset_id: hero\n", encoding="utf-8")
            (root / "assets/catalog.json").write_text(
                json.dumps({"assets": []}), encoding="utf-8")
            finalize = root / "_trace/subagents/image-finalize"
            finalize.mkdir(parents=True)
            (finalize / "handoff.json").write_text(json.dumps({
                "label": "image-finalize", "clean": True,
                "contract": {"status": "ready"}, "kind": "image",
                "attempt": 1, "ts": 9.0,
            }), encoding="utf-8")
            mem = [
                {"label": "image_a", "kind": "image", "clean": False,
                 "contract": {"status": "blocked"}, "attempt": 1, "ts": 1.0},
                {"label": "image_b", "kind": "image", "clean": True,
                 "contract": {"status": "ready"}, "attempt": 1, "ts": 2.0},
            ]
            slide = [{"label": "slide_group_a", "goal": "Slide Group a [01]: build"}]
            for order in itertools.permutations(mem):
                parent = SimpleNamespace(
                    ws=str(root), cfg={}, _spawn_lock=threading.Lock(),
                    worker_recs=list(order))
                err = agent_core._image_before_slide_error(parent, slide)
                self.assertIsNotNone(err, order)
                self.assertIn("未完成的 active Image 分片", err)

    def test_material_acceptance_recovers_from_disk_handoff_when_memory_stale(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "materials").mkdir()
            (root / "materials/attachments.json").write_text(json.dumps({
                "attachments": [{"name": "spec.pdf"}]
            }), encoding="utf-8")
            work = root / "materials/_work/material_a"
            work.mkdir(parents=True)
            (work / "catalog.json").write_text(json.dumps({
                "assets": [{"name": "spec.pdf", "status": "ready"}]
            }), encoding="utf-8")
            # Stale in-memory material worker (clean=False) but a corrected handoff.
            stale = [{
                "label": "material_a", "kind": "material", "clean": False,
                "contract": {"status": "blocked", "coverage": "partial"},
            }]
            handoff_dir = root / "_trace/subagents/material_a"
            handoff_dir.mkdir(parents=True)
            (handoff_dir / "handoff.json").write_text(json.dumps({
                "label": "material_a", "clean": True,
                "contract": {"status": "ready", "coverage": "complete"},
            }), encoding="utf-8")
            self.assertTrue(
                infer._disk_material_worker_ready(str(root), "material_a")
            )

    # ---- durable worker-state layer (restart / late-completion / supersede) ----

    def _write_handoff(self, root, label, *, clean, status, kind=None,
                       pages=None, coverage=None, attempt=None, ts=0.0):
        import re as _re
        sub = Path(root) / "_trace/subagents" / label
        sub.mkdir(parents=True, exist_ok=True)
        contract = {"status": status}
        if coverage is not None:
            contract["coverage"] = coverage
        payload = {
            "label": label, "clean": clean, "contract": contract,
            "kind": kind or agent_core._task_kind(label, None),
            "assigned_pages": pages or [],
            "attempt": attempt if attempt is not None
            else (int(_re.search(r"_r(\d+)$", label).group(1))
                  if _re.search(r"_r(\d+)$", label) else 1),
            "base_label": _re.sub(r"_r\d+$", "", label),
            "ts": ts, "exit_reason": "text_response",
            "vision_paths": [], "vision_evidence": {},
        }
        (sub / "handoff.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_late_completion_handoff_outranks_failure_record(self):
        # A worker timed out (failure record clean=False) but later wrote a clean
        # handoff; the durable rebuild must surface the clean completion.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fail_dir = root / "_trace/worker-failures"
            fail_dir.mkdir(parents=True)
            (fail_dir / "slide_group_a.json").write_text(json.dumps({
                "label": "slide_group_a", "clean": False,
                "contract": {"status": "blocked"}, "kind": "slide",
                "assigned_pages": [1, 2], "attempt": 1, "ts": 10.0,
            }), encoding="utf-8")
            self._write_handoff(root, "slide_group_a", clean=True, status="ready",
                                kind="slide", pages=[1, 2], ts=20.0)
            state = agent_core._rebuild_worker_state(str(root))
            recs = [r for r in state["worker_recs"] if r["label"] == "slide_group_a"]
            self.assertEqual(len(recs), 1)
            self.assertTrue(recs[0]["clean"])
            self.assertEqual(state["slide_page_owners"], {1: "slide_group_a", 2: "slide_group_a"})

    def test_repair_rn_supersedes_earlier_failed_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_handoff(root, "image_x", clean=False, status="blocked",
                                kind="image", ts=10.0)
            # Only the failed first attempt exists → gate must NOT clear.
            self.assertFalse(agent_core._disk_image_stage_ready(str(root)))
            # A repair retry finishes clean → active attempt clears the gate.
            self._write_handoff(root, "image_x_r2", clean=True, status="ready",
                                kind="image", ts=20.0)
            self.assertTrue(agent_core._disk_image_stage_ready(str(root)))
            state = agent_core._rebuild_worker_state(str(root))
            first = next(r for r in state["worker_recs"] if r["label"] == "image_x")
            self.assertEqual(first.get("superseded_by"), "image_x_r2")
            self.assertEqual(state["spawn_count"]["image_x"], 2)

    def test_restart_hydration_rebuilds_worker_state_and_gates(self):
        import threading

        class Orch:
            def __init__(self, root):
                self.role = "orchestrator"
                self.ws = str(root)
                self.cfg = {}
                self._spawn_lock = threading.Lock()
                self.worker_recs = []
                self._spawn_count = {}
                self._role_spawn_count = {}
                self._slide_page_owners = {}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_handoff(root, "research", clean=True, status="ready",
                                kind="research", ts=1.0)
            self._write_handoff(root, "image_x_r2", clean=True, status="ready",
                                kind="image", ts=2.0)
            self._write_handoff(root, "slide_group_a", clean=True, status="ready",
                                kind="slide", pages=[1, 2], ts=3.0)
            orch = Orch(root)
            agent_core._hydrate_orchestrator_state(orch)
            self.assertTrue(orch.worker_recs)
            self.assertEqual(orch._role_spawn_count.get("research"), 1)
            self.assertEqual(orch._slide_page_owners, {1: "slide_group_a", 2: "slide_group_a"})
            self.assertEqual(orch._spawn_count.get("image_x"), 2)
            # Hydrated singleton count must gate a second Research dispatch.
            research_task = {"label": "research", "goal": "[research] gather facts"}
            self.assertIsNotNone(agent_core._reserve_singleton_roles(orch, [research_task]))

    def test_hydration_preserves_older_trace_via_next_rn(self):
        import threading

        class Orch:
            def __init__(self, root):
                self.role = "orchestrator"
                self.ws = str(root)
                self.cfg = {}
                self._spawn_lock = threading.Lock()
                self.worker_recs = []
                self._spawn_count = {}
                self._role_spawn_count = {}
                self._slide_page_owners = {}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_handoff(root, "slide_group_a", clean=False, status="blocked",
                                kind="slide", pages=[1], ts=1.0)
            self._write_handoff(root, "slide_group_a_r2", clean=False, status="blocked",
                                kind="slide", pages=[1], ts=2.0)
            sentinel = (root / "_trace/subagents/slide_group_a/handoff.json").read_text()
            orch = Orch(root)
            agent_core._hydrate_orchestrator_state(orch)
            # Next attempt for this base must be _r3, and old traces untouched.
            self.assertEqual(orch._spawn_count.get("slide_group_a"), 2)
            self.assertEqual(
                (root / "_trace/subagents/slide_group_a/handoff.json").read_text(),
                sentinel,
            )

    def test_hydration_noop_on_fresh_and_live(self):
        import threading

        class Orch:
            def __init__(self, root, recs):
                self.role = "orchestrator"
                self.ws = str(root)
                self.cfg = {}
                self._spawn_lock = threading.Lock()
                self.worker_recs = recs
                self._spawn_count = {}
                self._role_spawn_count = {}
                self._slide_page_owners = {}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fresh = Orch(root, [])
            agent_core._hydrate_orchestrator_state(fresh)
            self.assertEqual(fresh.worker_recs, [])
            # Live run with an existing rec is never clobbered.
            self._write_handoff(root, "image_x", clean=True, status="ready", kind="image")
            live_recs = [{"label": "live", "kind": "image", "clean": True,
                          "contract": {"status": "ready"}}]
            live = Orch(root, live_recs)
            agent_core._hydrate_orchestrator_state(live)
            self.assertEqual(live.worker_recs, live_recs)

    def test_worker_ledger_tolerates_torn_last_line(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "_trace").mkdir(parents=True)
            path = root / "_trace/worker-ledger.jsonl"
            path.write_text(
                json.dumps({"label": "a", "ts": 1.0}) + "\n"
                + json.dumps({"label": "b", "ts": 2.0}) + "\n"
                + '{"label": "c", "ts": 3.0',  # torn, no newline / close
                encoding="utf-8",
            )
            events = agent_core._read_worker_ledger(str(root))
            self.assertEqual([e["label"] for e in events], ["a", "b"])

    # ---- B: Research singleton with in-flight / failed-retry / success / restart ----

    def _research_parent(self):
        import threading

        class P:
            def __init__(self):
                self._spawn_lock = threading.Lock()
                self._role_spawn_count = {}
                self._slide_page_owners = {}
                self._spawn_count = {}
                self.worker_recs = []
        return P()

    def _research_task(self):
        return agent_core._normalize_task(
            {"goal": "Research: verify the named product", "toolsets": ["file", "web"]})

    def test_research_inflight_second_dispatch_rejected(self):
        # First dispatch increments the count but leaves NO terminal record; a
        # second dispatch while in-flight must be refused (no concurrent Research).
        p = self._research_parent()
        self.assertIsNone(agent_core._reserve_singleton_roles(p, [self._research_task()]))
        p._role_spawn_count["research"] = 1  # dispatch increment, still running
        err = agent_core._reserve_singleton_roles(p, [self._research_task()])
        self.assertIsNotNone(err)
        self.assertIn("进行中", err)

    def test_research_failed_then_one_controlled_retry(self):
        p = self._research_parent()
        agent_core._reserve_singleton_roles(p, [self._research_task()])
        p._role_spawn_count["research"] = 1
        p.worker_recs.append({"label": "research", "kind": "research", "clean": False,
                              "contract": {"status": "blocked"}})
        self.assertIsNone(agent_core._reserve_singleton_roles(p, [self._research_task()]))

    def test_research_success_blocks_second_active(self):
        p = self._research_parent()
        p._role_spawn_count["research"] = 1
        p.worker_recs.append({"label": "research", "kind": "research", "clean": True,
                              "contract": {"status": "ready"}})
        err = agent_core._reserve_singleton_roles(p, [self._research_task()])
        self.assertIsNotNone(err)
        self.assertIn("单例", err)

    def test_research_two_failures_exhaust_after_restart(self):
        # Two failed research attempts on disk → restart hydrate recovers count 2,
        # so a third dispatch hits the MAX_RESEARCH_ATTEMPTS ceiling.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_handoff(root, "research", clean=False, status="blocked",
                                kind="research", ts=1.0)
            self._write_handoff(root, "research_r2", clean=False, status="blocked",
                                kind="research", ts=2.0)
            p = self._research_parent()
            p.role = "orchestrator"
            p.ws = str(root)
            p.cfg = {}
            agent_core._hydrate_orchestrator_state(p)
            self.assertEqual(p._role_spawn_count.get("research"), 2)
            err = agent_core._reserve_singleton_roles(p, [self._research_task()])
            self.assertIsNotNone(err)
            self.assertIn("上限", err)

    def test_research_acceptance_ignores_superseded_failure(self):
        # A failed first attempt + a clean recovered retry must ACCEPT (not be
        # rejected as "not a singleton"); the active attempt is validated.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "research").mkdir()
            (root / "research/research.md").write_text("findings", encoding="utf-8")
            recs = [
                {"label": "research", "kind": "research", "clean": False,
                 "contract": {"status": "blocked"}, "superseded_by": "research_r2"},
                {"label": "research_r2", "kind": "research", "clean": True,
                 "contract": {"status": "ready", "output": "research/research.md"}},
            ]
            ok, _ = infer._research_acceptance(str(root), recs)
            self.assertTrue(ok)

    def test_research_acceptance_flags_multiple_active_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "research").mkdir()
            (root / "research/research.md").write_text("findings", encoding="utf-8")
            recs = [
                {"label": "research", "kind": "research", "clean": True,
                 "contract": {"status": "ready", "output": "research/research.md"}},
                {"label": "research_alt", "kind": "research", "clean": True,
                 "contract": {"status": "ready", "output": "research/research.md"}},
            ]
            ok, reason = infer._research_acceptance(str(root), recs)
            self.assertFalse(ok)
            self.assertIn("歧义", reason)

    # ---- C: reconcile disk truth into acceptance + orphan-start restart ----

    def test_reconcile_recovers_late_clean_slide_with_full_record(self):
        import threading

        class Orch:
            def __init__(self, root):
                self.role = "orchestrator"
                self.ws = str(root)
                self.cfg = {}
                self._spawn_lock = threading.Lock()
                self._spawn_count = {}
                self._role_spawn_count = {}
                self._slide_page_owners = {}
                self.worker_recs = [{
                    "label": "slide_group_a", "kind": "slide", "clean": False,
                    "contract": {"status": "blocked"}, "assigned_pages": [1],
                    "inspected_pages": [], "stale_pixel_pages": [1], "renders": 0,
                }]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_handoff(root, "slide_group_a", clean=True, status="ready",
                                kind="slide", pages=[1], ts=99.0)
            # augment the handoff with the full machine-acceptance record
            hp = root / "_trace/subagents/slide_group_a/handoff.json"
            payload = json.loads(hp.read_text())
            payload.update({"renders": 2, "vision_calls": 3, "inspected_pages": [1],
                            "stale_pixel_pages": [], "dirty_visual_sources": [],
                            "machine_refine_rounds": 1})
            hp.write_text(json.dumps(payload))
            orch = Orch(root)
            agent_core._reconcile_worker_recs(orch)
            rec = next(r for r in orch.worker_recs if r["label"] == "slide_group_a")
            self.assertTrue(rec["clean"])
            self.assertEqual(rec["inspected_pages"], [1])
            self.assertEqual(rec["stale_pixel_pages"], [])
            self.assertEqual(rec["renders"], 2)

    def test_orphan_start_restart_counts_interrupted_attempt(self):
        # A start marker with no terminal handoff/failure is an interrupted
        # attempt: counted (so the next dispatch is _r2), not clean, never reused.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sub = root / "_trace/subagents/research"
            sub.mkdir(parents=True)
            (sub / "start.json").write_text(json.dumps({
                "label": "research", "base_label": "research", "attempt": 1,
                "kind": "research", "assigned_pages": [], "ts": 5.0, "event": "start",
            }), encoding="utf-8")
            state = agent_core._rebuild_worker_state(str(root))
            self.assertEqual(state["spawn_count"].get("research"), 1)
            rec = next(r for r in state["worker_recs"] if r["label"] == "research")
            self.assertFalse(rec["clean"])
            self.assertTrue(rec.get("interrupted"))
            # A terminal handoff for the same label overrides the orphan marker.
            self._write_handoff(root, "research", clean=True, status="ready",
                                kind="research", ts=9.0)
            state2 = agent_core._rebuild_worker_state(str(root))
            rec2 = next(r for r in state2["worker_recs"] if r["label"] == "research")
            self.assertTrue(rec2["clean"])

    # ---- round-4 negatives: deterministic active-selection + trust boundaries ----

    def test_effective_active_is_order_independent(self):
        import itertools
        recs = [
            {"label": "slide_a", "kind": "slide", "clean": True,
             "contract": {"status": "ready"}, "attempt": 1, "ts": 1.0},
            {"label": "slide_a_r2", "kind": "slide", "clean": False,
             "contract": {"status": "blocked"}, "attempt": 2, "ts": 2.0},
        ]
        for order in itertools.permutations(recs):
            active = agent_core._effective_active_recs(list(order), kind="slide")
            self.assertEqual([r["label"] for r in active], ["slide_a_r2"], order)

    def test_image_acceptance_stale_ready_does_not_mask_active_blocked(self):
        import itertools
        recs = [
            {"label": "image_a", "kind": "image", "clean": True,
             "contract": {"status": "ready"}, "attempt": 1, "ts": 1.0},
            {"label": "image_a_r2", "kind": "image", "clean": False,
             "contract": {"status": "blocked"}, "attempt": 2, "ts": 2.0},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            (root / "assets").mkdir()
            (root / "plan/slide_01.md").write_text(
                "image_opportunity: real_required\nasset_id: hero\n", encoding="utf-8")
            (root / "assets/catalog.json").write_text(
                json.dumps({"assets": []}), encoding="utf-8")
            for order in itertools.permutations(recs):
                ok, reason = infer._image_acceptance(str(root), list(order))
                self.assertFalse(ok, order)
                self.assertIn("未完成的 active 分片", reason)

    def test_research_multiple_active_bases_are_ambiguous(self):
        # Any two active bases are ambiguous regardless of success/failure mix —
        # we must not "pick a successful one".
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "research").mkdir()
            (root / "research/research.md").write_text("x", encoding="utf-8")
            for combo in (
                [("research", True, "ready"), ("research_alt", False, "blocked")],
                [("research", True, "ready"), ("research_alt", True, "ready")],
                [("research", False, "blocked"), ("research_alt", False, "blocked")],
            ):
                recs = [{"label": lbl, "kind": "research", "clean": cl,
                         "contract": {"status": st, "output": "research/research.md"}}
                        for lbl, cl, st in combo]
                ok, reason = infer._research_acceptance(str(root), recs)
                self.assertFalse(ok, combo)
                self.assertIn("歧义", reason)

    def test_pixel_review_selection_is_order_independent(self):
        import itertools
        base_slide = {"kind": "slide", "assigned_pages": [1], "inspected_pages": [1],
                      "vision_calls": 1, "clean": True, "contract": {"status": "ready"}}
        reviews = [
            {"label": "review", "kind": "review", "clean": False,
             "contract": {"status": "blocked"}, "attempt": 1, "ts": 1.0,
             "vision_calls": 1},
            {"label": "review_r2", "kind": "review", "clean": False,
             "contract": {"status": "blocked", "remaining": "still broken"},
             "attempt": 2, "ts": 2.0, "vision_calls": 1},
        ]
        slide = dict(base_slide, label="slide_group_a")
        results = set()
        for order in itertools.permutations(reviews):
            ok, reason = infer._pixel_review_acceptance(
                [slide] + list(order), ws=None, allow_review_only=True)
            results.add((ok, reason))
        # Same verdict regardless of input order (latest active review == review_r2).
        self.assertEqual(len(results), 1)

    def test_reconcile_trusts_handoff_not_ledger_only_clean(self):
        import threading

        class Orch:
            def __init__(self, root, recs):
                self.role = "orchestrator"
                self.ws = str(root)
                self.cfg = {}
                self._spawn_lock = threading.Lock()
                self._spawn_count = {}
                self._role_spawn_count = {}
                self._slide_page_owners = {}
                self.worker_recs = recs

        # (a) ledger-only "clean" with NO handoff must NOT promote.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "_trace").mkdir(parents=True)
            (root / "_trace/worker-ledger.jsonl").write_text(
                json.dumps({"label": "research", "clean": True,
                            "contract_status": "ready", "kind": "research",
                            "attempt": 1, "ts": 5.0}) + "\n", encoding="utf-8")
            orch = Orch(root, [{"label": "research", "kind": "research",
                                "clean": False, "contract": {"status": "blocked"}}])
            agent_core._reconcile_worker_recs(orch)
            rec = next(r for r in orch.worker_recs if r["label"] == "research")
            self.assertFalse(rec["clean"])

        # (b) a real handoff clean+partial (Research) DOES promote (not ready-only).
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_handoff(root, "research", clean=True, status="partial",
                                kind="research", ts=9.0)
            orch = Orch(root, [{"label": "research", "kind": "research",
                                "clean": False, "contract": {"status": "blocked"}}])
            agent_core._reconcile_worker_recs(orch)
            rec = next(r for r in orch.worker_recs if r["label"] == "research")
            self.assertTrue(rec["clean"])

    def test_start_only_restart_hydrates_and_next_is_r2(self):
        import threading

        class Orch:
            def __init__(self, root):
                self.role = "orchestrator"
                self.ws = str(root)
                self.cfg = {}
                self._spawn_lock = threading.Lock()
                self._spawn_count = {}
                self._role_spawn_count = {}
                self._slide_page_owners = {}
                self.worker_recs = []

        # start.json only — no ledger, no handoff, no failure.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sub = root / "_trace/subagents/slide_group_a"
            sub.mkdir(parents=True)
            (sub / "start.json").write_text(json.dumps({
                "label": "slide_group_a", "base_label": "slide_group_a",
                "attempt": 1, "kind": "slide", "assigned_pages": [1], "ts": 3.0,
            }), encoding="utf-8")
            orch = Orch(root)
            agent_core._hydrate_orchestrator_state(orch)
            self.assertEqual(orch._spawn_count.get("slide_group_a"), 1)

    def test_role_spawn_count_uses_max_attempt_not_record_count(self):
        # Sparse history: review + review_r3 (missing review_r2) must charge 3.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_handoff(root, "review", clean=False, status="blocked",
                                kind="review", ts=1.0)
            self._write_handoff(root, "review_r3", clean=False, status="blocked",
                                kind="review", attempt=3, ts=3.0)
            state = agent_core._rebuild_worker_state(str(root))
            self.assertEqual(state["role_spawn_count"].get("review"), 3)

    def test_accept_fails_loudly_when_reconcile_raises(self):
        # _accept must NOT swallow a reconcile failure (no broad except/pass);
        # a durable-state error must produce a diagnostic rejection.
        def _boom(_orch):
            raise RuntimeError("disk gone")

        saved = {
            "reconcile": agent_core._reconcile_worker_recs,
            "present": infer._ensure_present_html,
            "delivery": infer._delivery_audit,
            "render_ok": infer._render_ok,
        }
        agent_core._reconcile_worker_recs = _boom
        infer._ensure_present_html = lambda *a, **k: (True, "ok")
        infer._delivery_audit = lambda *a, **k: (True, "ok")
        infer._render_ok = lambda *a, **k: True
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "slides").mkdir()
                (root / "renders").mkdir()
                (root / "slides/slide_01.html").write_text("<html></html>", encoding="utf-8")
                (root / "renders/slide_01.png").write_bytes(b"x")
                import threading as _t

                orch = SimpleNamespace(
                    ws=str(root), exit_reason="text_response",
                    _spawn_lock=_t.Lock(), worker_recs=[], cfg={},
                )
                ok, reason = infer._accept(orch, require_materials=False)
                self.assertFalse(ok)
                self.assertIn("reconcile", reason)
        finally:
            agent_core._reconcile_worker_recs = saved["reconcile"]
            infer._ensure_present_html = saved["present"]
            infer._delivery_audit = saved["delivery"]
            infer._render_ok = saved["render_ok"]

    # ---- round-5 negatives: gate/acceptance stale-ready, trust boundary, budgets ----

    def _image_gate_parent(self, root, recs):
        import threading

        class Parent:
            def __init__(self):
                self.ws = str(root)
                self.cfg = {}
                self._spawn_lock = threading.Lock()
                self.worker_recs = recs
        return Parent()

    def test_image_dispatch_gate_stale_ready_does_not_mask_active_blocked(self):
        import itertools
        recs = [
            {"label": "image_a", "kind": "image", "clean": True,
             "contract": {"status": "ready"}, "attempt": 1, "ts": 1.0},
            {"label": "image_a_r2", "kind": "image", "clean": False,
             "contract": {"status": "blocked"}, "attempt": 2, "ts": 2.0},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            (root / "assets").mkdir()
            (root / "plan/slide_01.md").write_text(
                "image_opportunity: real_required\nasset_id: hero\n", encoding="utf-8")
            (root / "assets/catalog.json").write_text(
                json.dumps({"assets": []}), encoding="utf-8")
            slide = [{"label": "slide_group_a", "goal": "Slide Group a [01]: build"}]
            for order in itertools.permutations(recs):
                err = agent_core._image_before_slide_error(
                    self._image_gate_parent(root, list(order)), slide)
                self.assertIsNotNone(err, order)
                self.assertIn("未完成的 active Image 分片", err)

    def test_image_acceptance_multiple_bases_requires_all_ready(self):
        import itertools
        recs = [
            {"label": "image_a", "kind": "image", "clean": True,
             "contract": {"status": "ready"}, "ts": 1.0},
            {"label": "image_b", "kind": "image", "clean": False,
             "contract": {"status": "blocked"}, "ts": 2.0},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            (root / "assets").mkdir()
            (root / "plan/slide_01.md").write_text(
                "image_opportunity: real_required\nasset_id: hero\n", encoding="utf-8")
            (root / "assets/catalog.json").write_text(
                json.dumps({"assets": []}), encoding="utf-8")
            for order in itertools.permutations(recs):
                ok, reason = infer._image_acceptance(str(root), list(order))
                self.assertFalse(ok, order)
                self.assertIn("未完成的 active 分片", reason)

    def test_material_acceptance_order_independent(self):
        import itertools
        recs = [
            {"label": "material_a", "kind": "material", "clean": True,
             "contract": {"status": "ready", "coverage": "complete"},
             "attempt": 1, "ts": 1.0},
            {"label": "material_a_r2", "kind": "material", "clean": False,
             "contract": {"status": "blocked", "coverage": "partial"},
             "attempt": 2, "ts": 2.0},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "materials").mkdir()
            (root / "materials/attachments.json").write_text(
                json.dumps({"attachments": [{"name": "a.pdf"}]}), encoding="utf-8")
            work = root / "materials/_work/material_a"
            work.mkdir(parents=True)
            (work / "catalog.json").write_text(
                json.dumps({"assets": [{"name": "a.pdf", "status": "ready"}]}),
                encoding="utf-8")
            results = set()
            for order in itertools.permutations(recs):
                ok, _ = infer._material_acceptance(str(root), list(order))
                results.add(ok)
            # Active r2 is blocked → reject on every input order.
            self.assertEqual(results, {False})

    def test_disk_image_stage_ready_trusts_handoff_not_ledger_only(self):
        # (a) ledger-only clean, no handoff, no memory → must NOT be ready.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "_trace").mkdir(parents=True)
            (root / "_trace/worker-ledger.jsonl").write_text(
                json.dumps({"label": "image_a", "clean": True,
                            "contract_status": "ready", "kind": "image",
                            "attempt": 1, "ts": 5.0}) + "\n", encoding="utf-8")
            self.assertFalse(agent_core._disk_image_stage_ready(str(root)))
        # (b) ledger-only clean WITH a stale blocked memory rec must not promote
        # via the dispatch gate either.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            (root / "assets").mkdir()
            (root / "plan/slide_01.md").write_text(
                "image_opportunity: real_required\nasset_id: hero\n", encoding="utf-8")
            (root / "assets/catalog.json").write_text(
                json.dumps({"assets": []}), encoding="utf-8")
            (root / "_trace").mkdir(parents=True)
            (root / "_trace/worker-ledger.jsonl").write_text(
                json.dumps({"label": "image_a", "clean": True,
                            "contract_status": "ready", "kind": "image",
                            "attempt": 1, "ts": 5.0}) + "\n", encoding="utf-8")
            parent = self._image_gate_parent(root, [
                {"label": "image_a", "kind": "image", "clean": False,
                 "contract": {"status": "blocked"}}])
            err = agent_core._image_before_slide_error(
                parent, [{"label": "slide_group_a", "goal": "Slide Group a [01]: build"}])
            self.assertIsNotNone(err)
        # (c) a real handoff clean+ready DOES clear it.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_handoff(root, "image_a", clean=True, status="ready", kind="image")
            self.assertTrue(agent_core._disk_image_stage_ready(str(root)))
        # (d) two active bases, one blocked → not ready (all must be ready).
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_handoff(root, "image_a", clean=True, status="ready", kind="image")
            self._write_handoff(root, "image_b", clean=False, status="blocked", kind="image")
            self.assertFalse(agent_core._disk_image_stage_ready(str(root)))

    def test_blocking_review_failure_budget_uses_max_attempt(self):
        import threading

        class Orch:
            role = "orchestrator"

            def __init__(self, recs, role_count):
                self._spawn_lock = threading.Lock()
                self.worker_recs = recs
                self._role_spawn_count = {"review": role_count}
                self._review_recovery_notified = ""

        # review + review_r2 + review_r3 exhaust the budget: must say budget-used,
        # not "re-delegate Review".
        recs = [
            {"label": f"review{sfx}", "kind": "review", "clean": False,
             "contract": {"status": "blocked"}, "attempt": att, "ts": float(att)}
            for sfx, att in (("", 1), ("_r2", 2), ("_r3", 3))
        ]
        result = agent_core._blocking_review_failure(Orch(recs, 3))
        self.assertIsNotNone(result)
        self.assertIn("预算已用完", result["instruction"])
        self.assertNotIn("再委派 Review", result["instruction"])

    # ---- round-8: deterministic plan image-contract gate (static-453) ----

    def _write_plan_page(self, root, page, visual_body):
        plan = root / "plan"
        plan.mkdir(exist_ok=True)
        (plan / f"slide_{page:02d}.md").write_text(
            "# t\n\n## 视觉实现\n" + visual_body + "\n\n## 口语讲稿\nx\n",
            encoding="utf-8")

    def test_plan_gate_no_bitmap_medium_not_false_positive(self):
        # static-453 slide_06: image_opportunity none + medium "无位图（chart+timeline）"
        # must NOT be judged a raster page (the 位图 substring false-positive).
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_plan_page(
                root, 6,
                "- medium: 无位图（chart + timeline）\n"
                "- image_opportunity: none\n- presentation: 无\n")
            self.assertIsNone(agent_core._plan_image_contract_error(str(root)))

    def test_plan_gate_english_no_bitmap_not_false_positive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_plan_page(
                root, 6,
                "- medium: no bitmap (chart + timeline)\n"
                "- image_opportunity: none\n")
            self.assertIsNone(agent_core._plan_image_contract_error(str(root)))

    def test_plan_gate_rejects_split_media_as_presentation(self):
        # static-453 slide_03: real_required + presentation split-media (a layout
        # term) must be rejected by the early gate.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_plan_page(
                root, 3,
                "- medium: real photo\n- image_opportunity: real_required\n"
                "- presentation: split-media\n- asset_id: x\n")
            err = agent_core._plan_image_contract_error(str(root))
            self.assertIsNotNone(err)
            self.assertIn("presentation", err)

    def test_plan_gate_accepts_framed_scene(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_plan_page(
                root, 3,
                "- medium: real photo\n- image_opportunity: real_required\n"
                "- presentation: framed-scene\n- asset_id: x\n")
            self.assertIsNone(agent_core._plan_image_contract_error(str(root)))

    def test_plan_gate_subject_only_requires_flag(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_plan_page(
                root, 3,
                "- medium: generated image\n- image_opportunity: generated_ok\n"
                "- presentation: subject-only\n- asset_id: x\n")
            err = agent_core._plan_image_contract_error(str(root))
            self.assertIsNotNone(err)
            self.assertIn("subject_only", err)
            # With the flag present it passes.
            self._write_plan_page(
                root, 3,
                "- medium: generated image\n- image_opportunity: generated_ok\n"
                "- presentation: subject-only\n- subject_only: true\n- asset_id: x\n")
            self.assertIsNone(agent_core._plan_image_contract_error(str(root)))

    def test_plan_gate_raster_path_is_authoritative_even_when_opportunity_none(self):
        # static-453-style: image_opportunity none BUT a real raster asset path in
        # the visual section + missing/illegal presentation → early reject (deck.py
        # would also reject at build; the gate must not let it through).
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_plan_page(
                root, 1,
                "- medium: 无位图\n- image_opportunity: none\n"
                "- image: assets/foo.png\n")
            err = agent_core._plan_image_contract_error(str(root))
            self.assertIsNotNone(err)
            self.assertIn("presentation", err)
            # Illegal presentation on the same raster-path page also rejects.
            self._write_plan_page(
                root, 1,
                "- medium: 无位图\n- image_opportunity: none\n"
                "- presentation: split-media\n- image: assets/foo.jpg\n")
            self.assertIsNotNone(agent_core._plan_image_contract_error(str(root)))

    def test_plan_gate_none_without_raster_path_still_passes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_plan_page(
                root, 1,
                "- medium: 无位图（chart + timeline）\n- image_opportunity: none\n"
                "- presentation: 无\n")
            self.assertIsNone(agent_core._plan_image_contract_error(str(root)))
            # A valid four-enum on a raster-path no-bitmap page also passes.
            self._write_plan_page(
                root, 1,
                "- medium: 无位图\n- image_opportunity: none\n"
                "- presentation: framed-scene\n- image: assets/foo.png\n")
            self.assertIsNone(agent_core._plan_image_contract_error(str(root)))

    def test_plan_gate_missing_opportunity_with_raster_medium_is_rejected(self):
        # Skipped-prepare / legacy plan: no image_opportunity line at all, but the
        # visual section declares a raster medium with an illegal presentation.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_plan_page(
                root, 1,
                "- medium: real photo\n- presentation: split-media\n- image: mascot\n")
            self.assertIsNotNone(agent_core._plan_image_contract_error(str(root)))
            # A valid presentation cannot compensate for a missing machine field;
            # Image dispatch must force the plan to be frozen first.
            self._write_plan_page(
                root, 1,
                "- medium: real photo\n- presentation: framed-scene\n- asset_id: x\n")
            err = agent_core._plan_image_contract_error(str(root))
            self.assertIsNotNone(err)
            self.assertIn("image_opportunity", err)

    def test_plan_gate_undeclared_no_bitmap_page_is_rejected_before_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_plan_page(root, 1, "- medium: Canvas diagram（无位图）\n")
            err = agent_core._plan_image_contract_error(str(root))
            self.assertIsNotNone(err)
            self.assertIn("image_opportunity", err)

    def test_plan_gate_raster_path_detection_scoped_to_visual_section(self):
        # A path mentioned only in the speech/source section must NOT be read as a
        # raster asset (would false-trigger the contract on a no-bitmap page).
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = root / "plan"
            plan.mkdir()
            (plan / "slide_01.md").write_text(
                "# t\n\n## 视觉实现\n- medium: 无位图\n- image_opportunity: none\n"
                "- presentation: 无\n\n## 口语讲稿\n讲稿里提到 assets/misleading.png\n",
                encoding="utf-8")
            self.assertIsNone(agent_core._plan_image_contract_error(str(root)))

    def test_plan_gate_speech_presentation_cannot_satisfy_visual_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = root / "plan"
            plan.mkdir()
            (plan / "slide_01.md").write_text(
                "# t\n\n## 视觉实现\n- medium: real photo\n"
                "- image_opportunity: real_required\n- image: assets/foo.png\n\n"
                "## 口语讲稿\n- presentation: framed-scene\n",
                encoding="utf-8",
            )
            err = agent_core._plan_image_contract_error(str(root))
            self.assertIsNotNone(err)
            self.assertIn("presentation", err)

    def test_plan_gate_speech_subject_only_flag_cannot_satisfy_visual_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = root / "plan"
            plan.mkdir()
            (plan / "slide_01.md").write_text(
                "# t\n\n## 视觉实现\n- medium: generated image\n"
                "- image_opportunity: generated_ok\n- presentation: subject-only\n\n"
                "## 口语讲稿\n- subject_only: true\n",
                encoding="utf-8",
            )
            err = agent_core._plan_image_contract_error(str(root))
            self.assertIsNotNone(err)
            self.assertIn("subject_only", err)

    def test_plan_gate_missing_visual_section_matches_deck_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = root / "plan"
            plan.mkdir()
            path = plan / "slide_01.md"
            path.write_text(
                "# t\n\n## 口语讲稿\nassets/foo.png\n"
                "- presentation: framed-scene\n- image_opportunity: none\n",
                encoding="utf-8",
            )
            self.assertIsNone(agent_core._plan_image_contract_error(str(root)))

            path.write_text(
                "# t\n\n## 口语讲稿\nassets/foo.png\n"
                "- presentation: framed-scene\n- image_opportunity: real_required\n",
                encoding="utf-8",
            )
            err = agent_core._plan_image_contract_error(str(root))
            self.assertIsNotNone(err)
            self.assertIn("presentation", err)

    def test_plan_gate_blocks_image_dispatch_before_prepare_no_spawn(self):
        # Orchestrator that never ran prepare still cannot dispatch Image/Slide on
        # a bad presentation contract; the gate returns an image_presentation_contract
        # error and no start.json / worker record is created.
        import threading
        import unittest.mock as mock
        from core.agent import Agent

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_plan_page(
                root, 1,
                "- medium: real photo\n- image_opportunity: real_required\n"
                "- presentation: split-media\n- asset_id: x\n")
            (root / "plan/grounded-knowledge.md").write_text("x" * 60, encoding="utf-8")

            # Minimal orchestrator-like parent; never build a real Agent (hermetic).
            built = {"n": 0}

            def fake_agent(*a, **k):
                built["n"] += 1
                return SimpleNamespace(trace=SimpleNamespace(sub_dir=""))

            parent = SimpleNamespace(
                role="orchestrator", ws=str(root), sid="s", cfg={},
                base_system="sys", child_system="sys",
                skills_root=str(root / "skills"), prompt_language="zh",
                _spawn_lock=threading.Lock(), _spawn_count={},
                _role_spawn_count={}, _slide_page_owners={}, worker_recs=[],
                _delegate_depth=0, generation_preferences={},
            )
            with mock.patch.object(agent_core, "Agent", fake_agent):
                out = agent_core.delegate_task(parent, tasks=[{
                    "label": "image-hero", "goal": "Image: create hero",
                    "toolsets": ["file", "image_gen"]}])
            payload = json.loads(out)
            self.assertEqual(payload.get("code"), "image_presentation_contract")
            self.assertEqual(built["n"], 0)
            self.assertFalse(glob.glob(str(root / "_trace/subagents/*/start.json")))
            self.assertEqual(parent.worker_recs, [])

    def test_plan_gate_blocks_dispatch_on_none_with_raster_path_no_spawn(self):
        # Reviewer counter-example at the DELEGATE level: image_opportunity none +
        # a real raster asset path + missing presentation must reject before any
        # Image/Slide spawn, not run the whole pipeline and fail at build.
        import threading
        import unittest.mock as mock

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_plan_page(
                root, 1,
                "- medium: 无位图\n- image_opportunity: none\n"
                "- image: assets/foo.png\n")
            (root / "plan/grounded-knowledge.md").write_text("x" * 60, encoding="utf-8")
            built = {"n": 0}

            def fake_agent(*a, **k):
                built["n"] += 1
                return SimpleNamespace(trace=SimpleNamespace(sub_dir=""))

            parent = SimpleNamespace(
                role="orchestrator", ws=str(root), sid="s", cfg={},
                base_system="sys", child_system="sys",
                skills_root=str(root / "skills"), prompt_language="zh",
                _spawn_lock=threading.Lock(), _spawn_count={},
                _role_spawn_count={}, _slide_page_owners={}, worker_recs=[],
                _delegate_depth=0, generation_preferences={},
            )
            with mock.patch.object(agent_core, "Agent", fake_agent):
                out = agent_core.delegate_task(parent, tasks=[{
                    "label": "image-hero", "goal": "Image: create hero",
                    "toolsets": ["file", "image_gen"]}])
            payload = json.loads(out)
            self.assertEqual(payload.get("code"), "image_presentation_contract")
            self.assertEqual(built["n"], 0)
            self.assertFalse(glob.glob(str(root / "_trace/subagents/*/start.json")))
            self.assertEqual(parent.worker_recs, [])

    def test_plan_gate_blocks_image_dispatch_when_opportunity_undeclared(self):
        import threading
        import unittest.mock as mock

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_plan_page(
                root, 1,
                "- medium: real photo\n- presentation: framed-scene\n- asset_id: x\n",
            )
            (root / "plan/grounded-knowledge.md").write_text("x" * 60, encoding="utf-8")
            built = {"n": 0}

            def fake_agent(*a, **k):
                built["n"] += 1
                return SimpleNamespace(trace=SimpleNamespace(sub_dir=""))

            parent = SimpleNamespace(
                role="orchestrator", ws=str(root), sid="s", cfg={},
                base_system="sys", child_system="sys",
                skills_root=str(root / "skills"), prompt_language="zh",
                _spawn_lock=threading.Lock(), _spawn_count={},
                _role_spawn_count={}, _slide_page_owners={}, worker_recs=[],
                _delegate_depth=0, generation_preferences={},
            )
            with mock.patch.object(agent_core, "Agent", fake_agent):
                out = agent_core.delegate_task(parent, tasks=[{
                    "label": "image-hero", "goal": "Image: create hero",
                    "toolsets": ["file", "image_gen"],
                }])
            payload = json.loads(out)
            self.assertEqual(payload.get("code"), "image_presentation_contract")
            self.assertIn("image_opportunity", payload.get("error", ""))
            self.assertIn("同级独立行", payload.get("retry", ""))
            self.assertEqual(built["n"], 0)
            self.assertFalse(glob.glob(str(root / "_trace/subagents/*/start.json")))
            self.assertEqual(parent.worker_recs, [])

    def test_start_reservation_fail_closed_no_agent(self):
        # Hermetic: never build a real orchestrator Agent (that reads API env);
        # a minimal parent carries exactly the fields _build_child touches before
        # the start reservation, and Agent construction is fully mocked so the
        # test cannot reach any client/env/network.
        import threading
        import unittest.mock as mock

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = SimpleNamespace(
                ws=str(root),
                sid="s",
                cfg={},
                base_system="sys",
                child_system="sys",
                skills_root=str(root / "skills"),
                prompt_language="zh",
                _spawn_lock=threading.Lock(),
                _spawn_count={},
                _delegate_depth=0,
            )
            norm = agent_core._normalize_task({
                "goal": "Slide Group a [01]: build", "label": "slide_group_a",
                "toolsets": ["file"], "role": "leaf", "assigned_pages": [1],
            })
            built = {"n": 0}

            def fake_agent(*a, **k):
                built["n"] += 1
                return SimpleNamespace(trace=SimpleNamespace(sub_dir=""))

            # os.replace fails during the start reservation → _write_attempt_start
            # raises → _build_child must fail closed before constructing any Agent.
            with mock.patch("os.replace", side_effect=OSError("disk full")), \
                    mock.patch.object(agent_core, "Agent", fake_agent):
                with self.assertRaises(OSError):
                    agent_core._build_child(parent, norm)
            self.assertEqual(built["n"], 0)

    def test_active_history_compaction_keeps_delegate_contract_and_tool_pairing(self):
        prior_threshold = agent_core.HISTORY_COMPACT_AFTER_CHARS
        prior_keep = agent_core.HISTORY_KEEP_RECENT_MESSAGES
        try:
            agent_core.HISTORY_COMPACT_AFTER_CHARS = 10
            agent_core.HISTORY_KEEP_RECENT_MESSAGES = 1
            messages = [
                {"role": "user", "content": "task"},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "a", "name": "web_search", "input": {}},
                    {"type": "tool_use", "id": "b", "name": "delegate_task", "input": {}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "a", "content": "x" * 9000},
                    {"type": "tool_result", "tool_use_id": "b", "content": "status: ready\noutput: research/research.md"},
                ]},
                {"role": "assistant", "content": [{"type": "text", "text": "recent"}]},
            ]
            saved = agent_core._compact_active_history(messages)
            self.assertGreater(saved, 0)
            self.assertIn("历史工具结果已压缩", messages[2]["content"][0]["content"])
            self.assertEqual(
                messages[2]["content"][1]["content"],
                "status: ready\noutput: research/research.md",
            )
            self.assertEqual(messages[1]["content"][0]["id"], "a")
        finally:
            agent_core.HISTORY_COMPACT_AFTER_CHARS = prior_threshold
            agent_core.HISTORY_KEEP_RECENT_MESSAGES = prior_keep

    def test_skill_snapshot_is_real_immutable_tree_with_hash(self):
        previous = infer.SKILLS_DIR
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source_root = root / "source"
                skill = source_root / "mural-presenter"
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text("version one", encoding="utf-8")
                run = root / "run"
                run.mkdir()
                infer.SKILLS_DIR = str(source_root)
                skills_root = Path(infer._snapshot_skill(str(run)))
                snapshot = skills_root / "mural-presenter/SKILL.md"
                self.assertFalse((run / "skills").is_symlink())
                self.assertEqual(snapshot.read_text(encoding="utf-8"), "version one")
                manifest = json.loads(
                    (run / "_trace/skill-snapshot.json").read_text(encoding="utf-8")
                )
                self.assertEqual(manifest["skill"], "mural-presenter")
                self.assertEqual(manifest["language"], "zh")
                first_hash = manifest["tree_sha256"]
                (skill / "SKILL.md").write_text("version two", encoding="utf-8")
                infer._snapshot_skill(str(run))
                self.assertEqual(snapshot.read_text(encoding="utf-8"), "version one")
                second = json.loads(
                    (run / "_trace/skill-snapshot.json").read_text(encoding="utf-8")
                )
                self.assertEqual(second["tree_sha256"], first_hash)
        finally:
            infer.SKILLS_DIR = previous

    def test_english_skill_snapshot_records_language(self):
        previous = infer.SKILLS_DIR
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "source/mural-presenter"
                source.mkdir(parents=True)
                (source / "SKILL.md").write_text("English contract", encoding="utf-8")
                run = root / "run"
                run.mkdir()
                infer.SKILLS_DIR = str(root / "source")
                skills_root = Path(
                    infer._snapshot_skill(
                        str(run), "mural-presenter", language="en"
                    )
                )
                self.assertTrue((skills_root / "mural-presenter/SKILL.md").is_file())
                manifest = json.loads(
                    (run / "_trace/skill-snapshot.json").read_text(encoding="utf-8")
                )
                self.assertEqual(manifest["skill"], "mural-presenter")
                self.assertEqual(manifest["language"], "en")
        finally:
            infer.SKILLS_DIR = previous


if __name__ == "__main__":
    unittest.main()
