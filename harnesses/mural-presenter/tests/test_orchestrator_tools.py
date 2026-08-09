import unittest
from pathlib import Path
import hashlib
import json
import os
import re
import tempfile
import threading
from types import SimpleNamespace
from unittest import mock

import distill_ppt
from core import _final_contract, tools
from core import agent as agent_core
from core import nova_bridge
from core.trace import Trace as DiskTrace


class OrchestratorToolSurfaceTest(unittest.TestCase):
    def test_web_extract_processes_every_requested_url(self):
        urls = [f"https://example.test/{index}" for index in range(8)]
        with mock.patch.object(tools, "_fetch_one", side_effect=lambda url: f"body:{url}") as fetch:
            result = tools.web_extract(SimpleNamespace(), urls)
        self.assertEqual(fetch.call_count, len(urls))
        self.assertIn(urls[-1], result)

    def test_selected_file_must_be_continued_to_eof_without_losing_tail(self):
        class Agent:
            def __init__(self, root):
                self.ws = str(root)
                self.prompt_language = "zh"
                self.extra_tools = {}

            def read_path(self, path):
                return str(Path(self.ws) / path)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "long.md").write_text(
                "one\ntwo\nthree\nfour\nfive\n", encoding="utf-8"
            )
            agent = Agent(root)
            first = tools.dispatch(
                agent, "read_file", {"path": "long.md", "offset": 1, "limit": 2}
            )
            self.assertIn("续读 offset=3", first)
            blocked = tools.dispatch(
                agent, "terminal", {"command": "rg -n one ."}
            )
            self.assertIn("尚未读到末尾", blocked)
            second = tools.dispatch(
                agent, "read_file", {"path": "long.md", "offset": 3, "limit": 2}
            )
            self.assertIn("续读 offset=5", second)
            final = tools.dispatch(
                agent, "read_file", {"path": "long.md", "offset": 5, "limit": 2}
            )
            self.assertIn("5|five", final)
            self.assertNotIn("续读 offset", final)
            self.assertNotIn(
                "尚未读到末尾",
                tools.dispatch(agent, "terminal", {"command": "rg -n one ."}),
            )
            self.assertIn("long.md", agent._completed_read_paths)
            self.assertFalse(agent._pending_read_continuations)

    def test_child_must_read_its_role_card_before_other_files(self):
        class Agent:
            role = "subagent"
            prompt_language = "zh"
            extra_tools = {}

            def __init__(self, root, skill_root):
                self.ws = str(root)
                self.skill_root = skill_root
                self._required_role_card_path = (
                    "skills/mural-presenter/roles/research.md"
                )

            def read_path(self, path):
                normalized = str(path).replace("\\", "/")
                if normalized.startswith("skills/mural-presenter/"):
                    return str(
                        self.skill_root
                        / normalized.removeprefix("skills/mural-presenter/")
                    )
                return str(Path(self.ws) / normalized)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "other.md").write_text("other\n", encoding="utf-8")
            skill_root = Path(__file__).resolve().parents[3] / "skills/mural-presenter"
            agent = Agent(root, skill_root)
            blocked = tools.dispatch(
                agent, "read_file", {"path": "other.md"}
            )
            self.assertIn("角色卡读到末尾", blocked)
            role = tools.dispatch(agent, "read_file", {
                "path": "skills/mural-presenter/roles/research.md",
                "offset": 1,
                "limit": 500,
            })
            self.assertNotIn("续读 offset", role)
            self.assertIn(
                "1|# Research",
                role,
            )
            self.assertIn(
                "1|other",
                tools.dispatch(agent, "read_file", {"path": "other.md"}),
            )

    def test_child_cannot_reread_root_skill_after_role_card(self):
        agent = SimpleNamespace(role="subagent", prompt_language="zh")
        result = tools.read_file(agent, "skills/mural-presenter/SKILL.md")
        self.assertIn("根工作流只由编排器读取", result)

    def test_implementation_assets_are_not_model_readable_references(self):
        agent = SimpleNamespace(role="orchestrator", prompt_language="zh")
        for path in (
            "skills/mural-presenter/assets/base-template.css",
            "skills/mural-presenter/assets/vendor/echarts.min.js",
        ):
            result = tools.read_file(agent, path)
            self.assertIn("确定性运行资产", result)

    def test_static_skill_reference_is_not_streamed_twice(self):
        class Agent:
            role = "orchestrator"
            prompt_language = "zh"

            def __init__(self, skill_root):
                self.skills_root = str(skill_root.parent)

            def read_path(self, path):
                return str(
                    Path(self.skills_root)
                    / str(path).removeprefix("skills/")
                )

        skill = Path(__file__).resolve().parents[3] / "skills/mural-presenter"
        agent = Agent(skill)
        path = "skills/mural-presenter/references/scenario-routing.md"
        first = tools.read_file(agent, path, limit=2000)
        self.assertNotIn("已完整读取到 EOF", first)
        second = tools.read_file(agent, path, limit=2000)
        self.assertIn("已完整读取到 EOF", second)

    def test_orchestrator_selects_only_one_reference_per_routing_family(self):
        class Agent:
            role = "orchestrator"
            prompt_language = "zh"

            def __init__(self, skill_root):
                self.skills_root = str(skill_root.parent)

            def read_path(self, path):
                return str(
                    Path(self.skills_root)
                    / str(path).removeprefix("skills/")
                )

        skill = Path(__file__).resolve().parents[3] / "skills/mural-presenter"
        agent = Agent(skill)
        first = tools.read_file(
            agent,
            "skills/mural-presenter/references/slide-categories/brand-creative.md",
            limit=2000,
        )
        self.assertNotIn("同一任务只选一份", first)
        second = tools.read_file(
            agent,
            "skills/mural-presenter/references/slide-categories/education-training.md",
            limit=2000,
        )
        self.assertIn("同一任务只选一份", second)

        family_agent = Agent(skill)
        first_family = tools.read_file(
            family_agent,
            "skills/mural-presenter/references/style-families/business-technology.md",
            limit=2000,
        )
        self.assertNotIn("同一任务只选一份", first_family)
        second_family = tools.read_file(
            family_agent,
            "skills/mural-presenter/references/style-families/organic-cultural.md",
            limit=2000,
        )
        self.assertIn("同一任务只选一份", second_family)

    def test_python_m_pip_install_is_rejected(self):
        agent = SimpleNamespace(prompt_language="zh")
        result = tools.terminal(agent, "python -mpip install playwright")
        self.assertIn("禁止安装", result)

    def test_oversized_tool_result_is_spooled_losslessly(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace_dir = root / "_trace/orchestrator"
            trace_dir.mkdir(parents=True)
            agent = SimpleNamespace(
                ws=str(root),
                prompt_language="zh",
                trace=SimpleNamespace(sub_dir=str(trace_dir)),
            )
            tool_use = SimpleNamespace(id="toolu_big")
            original = "header\n" + "x" * 20000 + "\ntail-marker"
            preview = agent_core._lossless_tool_text(agent, tool_use, original, 1200)
            self.assertIn("工具结果尚未结束", preview)
            match = re.search(r"已保存到 ([^\s。]+)", preview)
            self.assertIsNotNone(match)
            stored = root / match.group(1)
            self.assertEqual(stored.read_text(encoding="utf-8"), original)

    def test_role_card_is_returned_in_one_read_without_hidden_truncation(self):
        skill_root = Path(__file__).resolve().parents[3] / "skills/mural-presenter"

        class Agent:
            role = "slide"

            def read_path(self, path):
                relative = str(path).replace("skills/mural-presenter/", "", 1)
                return str(skill_root / relative)

        result = tools.read_file(
            Agent(), "skills/mural-presenter/roles/slide.md"
        )
        expected_lines = (skill_root / "roles/slide.md").read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertTrue(result.startswith(f"1|{expected_lines[0]}\n"))
        self.assertTrue(result.endswith(f"{len(expected_lines)}|{expected_lines[-1]}"))
        self.assertNotIn("[… 截断:", result)
        self.assertIn("status:", result)

    def test_orchestrator_system_routes_to_the_canonical_skill(self):
        system = distill_ppt.BASE_SYSTEM
        self.assertIn("skills/mural-presenter/SKILL.md", system)
        self.assertIn("Harness 已在模型调用前完成", system)
        self.assertIn("不要再次运行 `deck.py preflight`", system)
        self.assertNotIn("## 4. 新建演示文稿", system)
        self.assertNotIn("### 步骤 1：理解新建需求", system)

    def test_model_timing_accumulates_without_changing_response(self):
        response = object()
        fake = SimpleNamespace(
            _usage_acc={},
            logs=[],
            log=lambda value: fake.logs.append(value),
        )
        with mock.patch.object(agent_core.time, "monotonic", return_value=12.5):
            returned = agent_core._finish_model_call(fake, 10.0, response, "ok")
        self.assertIs(returned, response)
        self.assertEqual(fake._usage_acc["sum_model_wall_seconds"], 2.5)
        self.assertEqual(fake._usage_acc["max_model_wall_seconds"], 2.5)
        self.assertTrue(any("model_timing" in line for line in fake.logs))

    def test_optional_upstream_timeout_can_be_disabled(self):
        self.assertIsNone(tools.parse_optional_timeout("none", 180))
        self.assertIsNone(tools.parse_optional_timeout("0", 180))
        self.assertIsNone(tools.parse_optional_timeout("off", 180))
        self.assertEqual(tools.parse_optional_timeout("420", 180), 420.0)
        self.assertEqual(tools.parse_optional_timeout(None, 180), 180.0)
        self.assertIsNone(nova_bridge.parse_optional_request_timeout("none"))
        self.assertEqual(
            nova_bridge.parse_optional_request_timeout("1800"), 1800.0
        )

    def test_max_tokens_stop_continues_instead_of_becoming_completion(self):
        class Trace:
            def __init__(self, root):
                self.sub_dir = str(root)

            def snapshot_inputs(self, *_args):
                return None

            def write(self, *_args):
                return None

        class Orchestrator:
            def __init__(self, root):
                self.role = "orchestrator"
                self.label = "orch"
                self.initial_user = "build"
                self.system = "system"
                self.tools = []
                self.max_turns = 0
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

        responses = iter((
            SimpleNamespace(
                content=[SimpleNamespace(type="text", text="unfinished")],
                stop_reason="length",
            ),
            SimpleNamespace(
                content=[SimpleNamespace(type="text", text="completed")],
                stop_reason="end_turn",
            ),
        ))
        with tempfile.TemporaryDirectory() as temporary:
            agent = Orchestrator(Path(temporary))
            with mock.patch.object(
                agent_core, "_model_call", side_effect=lambda *_: next(responses)
            ):
                self.assertTrue(agent_core.run_loop(agent))
            self.assertEqual(agent.final_text, "completed")
            self.assertTrue(any("max_tokens" in line for line in agent.logs))

    def test_orchestrator_stops_after_third_identical_read_file_call(self):
        class Trace:
            def __init__(self, root):
                self.sub_dir = str(root)

            def snapshot_inputs(self, *_args):
                return None

            def clean(self, value):
                return value

            def write(self, *_args):
                return None

        class Orchestrator:
            def __init__(self, root):
                self.role = "orchestrator"
                self.label = "orch"
                self.initial_user = "build a deck"
                self.system = "system"
                self.tools = []
                self.max_turns = 8
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

        def repeated_read(index):
            return SimpleNamespace(
                content=[SimpleNamespace(
                    type="tool_use",
                    id=f"toolu_read_{index}",
                    name="read_file",
                    input={"path": "skills/mural-presenter/references/design-rules.md"},
                )],
                stop_reason="tool_use",
            )

        with tempfile.TemporaryDirectory() as temporary:
            orchestrator = Orchestrator(Path(temporary))
            replies = [repeated_read(index) for index in range(1, 5)]
            tool_result = [{"type": "tool_result", "tool_use_id": "toolu", "content": "rules"}]
            with mock.patch.object(agent_core, "_model_call", side_effect=replies) as model_call, \
                    mock.patch.object(agent_core, "_run_tools", return_value=tool_result), \
                    mock.patch.object(nova_bridge, "finalize_agent", return_value=None):
                self.assertFalse(agent_core.run_loop(orchestrator))

        self.assertEqual(orchestrator.exit_reason, "stalled_repetition")
        self.assertEqual(model_call.call_count, 3)
        self.assertTrue(any("停滞保护触发" in line for line in orchestrator.logs))

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

    def test_terminal_rejects_recursive_host_font_search(self):
        class Agent:
            def __init__(self, root):
                self.ws = str(root)
                self.label = "orchestrator"

            def safe(self, relative):
                return str(Path(self.ws) / relative)

        with tempfile.TemporaryDirectory() as temporary:
            result = tools.terminal(
                Agent(temporary),
                'fc-list :lang=zh | head -20; find / -iname "*NotoSansSC*"',
            )
        self.assertIn("禁止从 /", result)
        self.assertIn("模型调用前报告", result)

    def test_terminal_rejects_model_repeating_harness_preflight(self):
        class Agent:
            def __init__(self, root):
                self.ws = str(root)
                self.label = "orchestrator"

            def safe(self, relative):
                return str(Path(self.ws) / relative)

        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            tools.os.environ,
            {"MURAL_PREFLIGHT_DONE": "1"},
        ):
            result = tools.terminal(
                Agent(temporary),
                "python skills/mural-presenter/scripts/deck.py preflight .",
            )
        self.assertIn("模型调用前完成", result)
        self.assertIn("直接开始任务解析", result)

    def test_terminal_allows_bounded_workspace_search(self):
        class Agent:
            def __init__(self, root):
                self.ws = str(root)
                self.label = "orchestrator"

            def safe(self, relative):
                return str(Path(self.ws) / relative)

        completed = mock.Mock(returncode=0, stdout="ok\n", stderr="")
        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "core.tools.subprocess.run", return_value=completed
        ) as run:
            result = tools.terminal(
                Agent(temporary),
                "find . -maxdepth 2 -type f",
            )
        self.assertEqual("ok", result)
        run.assert_called_once()

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

    def test_review_refine_round_count_is_not_a_hard_cutoff(self):
        from PIL import Image

        class Agent:
            def __init__(self, root):
                self.ws = str(root)
                self.label = "review"
                self._review_refine_rounds = 2
                self._review_modified_pages = set()
                self._review_global_visual_change = False
                self.trace = SimpleNamespace(
                    sub_dir=str(Path(root) / "_trace/subagents/review")
                )

            def safe(self, relative):
                return str(Path(self.ws) / relative)

            def writable(self, _relative):
                return True

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "slides").mkdir()
            (root / "renders").mkdir()
            page = root / "slides/slide_02.html"
            page.write_text("old", encoding="utf-8")
            (root / "base.css").write_text(":root {}", encoding="utf-8")
            Image.new("RGB", (16, 9), "white").save(root / "renders/slide_02.png")
            result = tools.patch(
                Agent(root), path="slides/slide_02.html",
                old_string="old", new_string="new",
            )
            self.assertIn("已编辑", result)
            self.assertEqual(page.read_text(encoding="utf-8"), "new")

    def test_review_edit_freezes_baseline_and_requires_before_after_pixels(self):
        from PIL import Image
        import io
        import base64

        class Agent:
            def __init__(self, root):
                self.ws = str(root)
                self.label = "review"
                self.prompt_language = "zh"
                self.cfg = {}
                self._review_refine_rounds = 0
                self._review_modified_pages = set()
                self._review_comparison_pages = set()
                self._review_global_visual_change = False
                self._review_global_comparison = False
                self.trace = SimpleNamespace(
                    sub_dir=str(root / "_trace/subagents/review")
                )

            def safe(self, relative):
                return str(Path(self.ws) / relative)

            def read_path(self, relative):
                return str(Path(self.ws) / relative)

            def writable(self, _relative):
                return True

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "slides").mkdir()
            (root / "renders").mkdir()
            (root / "_trace/subagents/review").mkdir(parents=True)
            source = root / "slides/slide_03.html"
            css = root / "base.css"
            png = root / "renders/slide_03.png"
            source.write_text("<section>before</section>", encoding="utf-8")
            css.write_text(".slide{}", encoding="utf-8")
            Image.new("RGB", (160, 90), "#224466").save(png)
            os.utime(source, ns=(1_000_000_000, 1_000_000_000))
            os.utime(css, ns=(1_000_000_000, 1_000_000_000))
            os.utime(png, ns=(2_000_000_000, 2_000_000_000))

            agent = Agent(root)
            edited = tools.patch(
                agent, path="slides/slide_03.html",
                old_string="before", new_string="after",
            )
            self.assertIn("已编辑", edited)
            baseline = Path(agent.trace.sub_dir) / "review-baselines/slide_03.png"
            self.assertTrue(baseline.is_file())
            self.assertEqual(agent._review_modified_pages, {3})

            Image.new("RGB", (160, 90), "#cc8844").save(png)
            result = tools.vision_analyze(agent, "renders/slide_03.png")
            self.assertEqual(result["comparison_mode"], "before_after")
            self.assertEqual(agent._review_comparison_pages, {3})
            composite = Image.open(io.BytesIO(base64.b64decode(result["image_b64"])))
            self.assertGreater(composite.height, 180)
            self.assertIn("旧版优点", result["summary"])

    def test_review_ready_rejects_modified_page_without_comparison(self):
        workers = [{
            "label": "review", "kind": "review", "clean": True,
            "vision_calls": 1, "machine_refine_rounds": 1,
            "review_modified_pages": [3], "review_comparison_pages": [],
            "contract": {
                "status": "ready", "mode": "simple_edit",
                "content_fidelity": "not-applicable",
                "final_pixels_inspected": "yes", "regression_checked": "yes",
                "regressed_pages": "none", "speech_aligned": "yes",
                "remaining": "none", "refine_rounds": "1",
            },
        }]
        ok, reason = distill_ppt._pixel_review_acceptance(
            workers, allow_review_only=True
        )
        self.assertFalse(ok)
        self.assertIn("BEFORE | AFTER", reason)

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
            for schema in tools.resolve_toolsets(distill_ppt.ORCHESTRATOR_TOOLSETS)
        }

        self.assertIn("write_file", names)
        self.assertIn("patch", names)
        self.assertIn("terminal", names)
        self.assertIn("delegate_task", names)
        self.assertNotIn("sync_speech", names)
        self.assertNotIn("build_player", names)

    def test_model_visible_tool_surface_is_hermes_clean(self):
        names = {
            schema["name"]
            for toolsets in (
                distill_ppt.ORCHESTRATOR_TOOLSETS,
                ["file", "terminal", "vision", "web", "image_gen"],
            )
            for schema in tools.resolve_toolsets(toolsets)
        }
        self.assertFalse({
            "search_files", "fetch_image", "delegate_tasks", "delegate_slides",
            "submit_plan_full", "submit_plan_chunk", "patch_plan",
        } & names)

        delegate = tools.DELEGATE_TASK_SCHEMA["parameters"]
        self.assertEqual(delegate["required"], ["tasks"])
        self.assertFalse(delegate["additionalProperties"])
        item = delegate["properties"]["tasks"]["items"]
        self.assertEqual(item["required"], ["goal"])
        self.assertEqual(set(item["properties"]), {"goal"})
        self.assertFalse(item["additionalProperties"])

        patch_schema = tools.PATCH_SCHEMA["parameters"]
        self.assertEqual(patch_schema["properties"]["mode"]["enum"], ["replace"])
        self.assertNotIn("patch", patch_schema["properties"])
        terminal = tools.TERMINAL_SCHEMA["parameters"]["properties"]
        self.assertEqual(set(terminal), {"command", "timeout"})

        rejected = json.loads(agent_core.delegate_task(
            SimpleNamespace(_delegate_depth=0),
            tasks=[{"goal": "Research: verify facts", "toolsets": ["web"]}],
        ))
        self.assertEqual(rejected["code"], "invalid_delegate_shape")

    def test_each_role_receives_only_its_expected_hermes_tools(self):
        orchestrator = {
            schema["name"]
            for schema in tools.resolve_toolsets(distill_ppt.ORCHESTRATOR_TOOLSETS)
        }
        self.assertEqual(
            orchestrator,
            {"read_file", "write_file", "patch", "terminal", "delegate_task"},
        )

        cases = {
            "Material 01: read one attachment shard": {
                "read_file", "write_file", "patch", "terminal", "vision_analyze",
            },
            "Research: verify facts": {
                "read_file", "write_file", "patch", "web_search", "web_extract",
            },
            "Image hero: prepare assets": {
                "read_file", "write_file", "patch", "terminal", "vision_analyze",
                "web_search", "web_extract", "image_generate",
            },
            "Slide Group bookends [01,10]: build pages": {
                "read_file", "write_file", "patch", "terminal", "vision_analyze",
            },
            "Review: mode=final_review": {
                "read_file", "write_file", "patch", "terminal", "vision_analyze",
            },
        }
        for goal, expected in cases.items():
            task = agent_core._normalize_task({"goal": goal})
            actual = {
                schema["name"] for schema in tools.resolve_toolsets(task["toolsets"])
            }
            self.assertEqual(actual, expected, goal)

    def test_child_final_contract_is_structured_for_acceptance(self):
        contract = _final_contract(
            "status: ready\ncoverage: complete\ncontent_fidelity: pass\n"
        )
        self.assertEqual(contract["status"], "ready")
        self.assertEqual(contract["coverage"], "complete")
        self.assertEqual(contract["content_fidelity"], "pass")

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

        # Structured parsing must not turn a prose mention into a verdict.
        self.assertEqual(_final_contract("我将返回 **status: ready** 作为最终结论。"), {})

    def test_child_gets_progress_bounded_closeout_when_contract_is_missing(self):
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
            self.assertTrue(any("继续同角色收口" in line for line in child.logs))

    def test_slide_gets_progress_bounded_closeout_when_pixels_are_stale(self):
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
            seen_prompts = []

            def model_call(_agent, messages):
                seen_prompts.append(json.dumps(messages, ensure_ascii=False, default=str))
                return next(replies)

            with mock.patch.object(agent_core, "_model_call", side_effect=model_call):
                self.assertTrue(agent_core.run_loop(child))
            self.assertTrue(any("最终像素证据未闭环" in line for line in child.logs))
            self.assertTrue(any("focus 联系表" in prompt for prompt in seen_prompts[1:]))
            self.assertFalse(any(
                "逐页对最终 renders/slide_NN.png" in prompt for prompt in seen_prompts
            ))
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

    def test_review_pixel_state_uses_bytes_and_flags_only_changed_pages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "slides").mkdir()
            (root / "renders").mkdir()
            (root / "plan").mkdir()
            (root / "base.css").write_text(".slide{}", encoding="utf-8")
            (root / "plan/theme.css").write_text(":root{}", encoding="utf-8")
            for page in (1, 2):
                (root / f"slides/slide_{page:02d}.html").write_text(
                    f"<section>{page}</section>", encoding="utf-8"
                )
            (root / "renders/slide_01.png").write_bytes(b"same pixels")
            (root / "renders/slide_02.png").write_bytes(b"new pixels")
            for source in (
                root / "base.css", root / "plan/theme.css",
                root / "slides/slide_01.html", root / "slides/slide_02.html",
            ):
                os.utime(source, ns=(1_000_000_000, 1_000_000_000))
            for page in (1, 2):
                os.utime(
                    root / f"renders/slide_{page:02d}.png",
                    ns=(3_000_000_000, 3_000_000_000),
                )
            (root / "renders/review-contact.json").write_text(json.dumps({
                "full": {"pages": [1, 2], "groups": []},
            }), encoding="utf-8")
            child = SimpleNamespace(
                ws=str(root),
                vision_paths=["renders/slide_01.png", "renders/slide_02.png"],
                vision_evidence={
                    # Old mtime is harmless because the current bytes are identical.
                    "renders/slide_01.png": {
                        "sha256": hashlib.sha256(b"same pixels").hexdigest(),
                        "mtime_ns": 2_000_000_000,
                    },
                    "renders/slide_02.png": {
                        "sha256": hashlib.sha256(b"old pixels").hexdigest(),
                        "mtime_ns": 2_000_000_000,
                    },
                },
            )
            self.assertEqual(agent_core._review_pixel_state(child), {
                "inspected_pages": [1, 2],
                "missing_pages": [],
                "stale_pages": [2],
                "dirty_sources": [],
            })

    def test_contact_sheet_is_rejected_after_any_included_page_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "renders").mkdir()
            (root / "slides").mkdir()
            (root / "plan").mkdir()
            (root / "base.css").write_text(".slide{}", encoding="utf-8")
            (root / "plan/theme.css").write_text(":root{}", encoding="utf-8")
            html = root / "slides/slide_01.html"
            png = root / "renders/slide_01.png"
            sheet = root / "renders/contact-sheet-focus-core.png"
            html.write_text("<section>one</section>", encoding="utf-8")
            png.write_bytes(b"pixels-v1")
            sheet.write_bytes(b"sheet-v1")
            for source in (root / "base.css", root / "plan/theme.css", html):
                os.utime(source, ns=(1_000_000_000, 1_000_000_000))
            os.utime(png, ns=(2_000_000_000, 2_000_000_000))
            (root / "renders/contact-sheet-focus-core.json").write_text(
                json.dumps({
                    "mode": "focus", "pages": [1],
                    "evidence": [{
                        "page": 1, "path": "renders/slide_01.png",
                        "sha256": hashlib.sha256(b"pixels-v1").hexdigest(),
                        "mtime_ns": 2_000_000_000,
                    }],
                }),
                encoding="utf-8",
            )
            agent = SimpleNamespace(ws=str(root))
            self.assertIsNone(tools._contact_sheet_freshness_error(agent, str(sheet)))
            png.write_bytes(b"pixels-v2")
            os.utime(png, ns=(3_000_000_000, 3_000_000_000))
            error = tools._contact_sheet_freshness_error(agent, str(sheet))
            self.assertIn("旧联系表", error)
            self.assertIn("01", error)

    def test_synthesis_trace_keeps_hash_verified_image_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            trace = DiskTrace(temporary)
            shot = trace.snapshot_image("tool-1", b"pixel bytes")
            messages = [{
                "role": "user",
                "content": [{
                    "type": "tool_result", "tool_use_id": "tool-1",
                    "content": [{"type": "image", "shot": shot}],
                }],
            }]
            status = trace.write(messages, [], "synthesis")
            self.assertTrue(status["complete"])
            self.assertEqual(status["image_count"], 1)
            manifest = json.loads(
                (Path(temporary) / "multimodal-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["images"][0]["shot"], "images/view_01.png")
            self.assertEqual(
                manifest["images"][0]["sha256"],
                hashlib.sha256(b"pixel bytes").hexdigest(),
            )

    def test_review_gets_same_role_closeout_after_post_build_pixels_change(self):
        class Trace:
            def __init__(self, root):
                self.sub_dir = str(root / "_trace")

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
                self.ws = str(root)
                self.vision_paths = ["renders/slide_01.png"]
                self.vision_evidence = {
                    "renders/slide_01.png": {
                        "sha256": hashlib.sha256(b"before build").hexdigest(),
                        "mtime_ns": 2_000_000_000,
                    },
                }
                self._usage_acc = {
                    "sum_input": 0, "sum_cache_read": 0,
                    "sum_cache_create": 0, "sum_output": 0, "n_turns": 0,
                }
                self.logs = []

            def config_snapshot(self):
                return {}

            def log(self, value):
                self.logs.append(value)

        def response():
            return SimpleNamespace(content=[SimpleNamespace(
                type="text",
                text=(
                    "status: ready\nmode: final_review\n"
                    "content_fidelity: not-applicable\ndiagnosed_pages: all\n"
                    "fixed_pages: none\nrender_mode: none\nrefine_rounds: 0\n"
                    "final_pixels_inspected: yes\nregression_checked: yes\n"
                    "regressed_pages: none\nspeech_aligned: yes\nremaining: none"
                ),
            )], stop_reason="end_turn")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "slides").mkdir()
            (root / "renders").mkdir()
            (root / "plan").mkdir()
            (root / "_trace").mkdir()
            (root / "base.css").write_text(".slide{}", encoding="utf-8")
            (root / "plan/theme.css").write_text(":root{}", encoding="utf-8")
            (root / "slides/slide_01.html").write_text("<section/>", encoding="utf-8")
            (root / "renders/slide_01.png").write_bytes(b"after build")
            for source in (
                root / "base.css", root / "plan/theme.css",
                root / "slides/slide_01.html",
            ):
                os.utime(source, ns=(1_000_000_000, 1_000_000_000))
            os.utime(root / "renders/slide_01.png", ns=(3_000_000_000, 3_000_000_000))
            (root / "renders/review-contact.json").write_text(json.dumps({
                "full": {"pages": [1], "groups": []},
            }), encoding="utf-8")
            child = Child(root)
            seen_prompts = []

            def model_call(_agent, messages):
                seen_prompts.append(json.dumps(messages, ensure_ascii=False, default=str))
                return response()

            with mock.patch.object(agent_core, "_model_call", side_effect=model_call):
                self.assertTrue(agent_core.run_loop(child))
            self.assertEqual(len(seen_prompts), 2)
            self.assertTrue(any("最终像素证据未闭环" in line for line in child.logs))
            self.assertIn("不得再修改、渲染或 build", seen_prompts[1])

    def test_slide_focus_contact_sheet_covers_fresh_group_pages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "slides").mkdir()
            (root / "renders").mkdir()
            (root / "base.css").write_text(".slide{}", encoding="utf-8")
            snapshots = []
            for page in (1, 2):
                html = root / f"slides/slide_{page:02d}.html"
                png = root / f"renders/slide_{page:02d}.png"
                html.write_text(f"<section>{page}</section>", encoding="utf-8")
                png.write_bytes(f"pixels-{page}".encode())
                os.utime(html, ns=(1_000_000_000, 1_000_000_000))
                os.utime(png, ns=(2_000_000_000, 2_000_000_000))
                snapshots.append({
                    "page": page,
                    "path": f"renders/slide_{page:02d}.png",
                    "sha256": hashlib.sha256(f"pixels-{page}".encode()).hexdigest(),
                    "mtime_ns": 2_000_000_000,
                })
            os.utime(root / "base.css", ns=(1_000_000_000, 1_000_000_000))
            sheet = root / "renders/contact-sheet-focus-group-a.png"
            sheet.write_bytes(b"group pixels")
            os.utime(sheet, ns=(3_000_000_000, 3_000_000_000))
            sheet.with_suffix(".json").write_text(json.dumps({
                "mode": "focus", "label": "group-a", "pages": [1, 2],
                "focus": "renders/contact-sheet-focus-group-a.png",
                "evidence": snapshots,
            }), encoding="utf-8")
            child = SimpleNamespace(
                ws=str(root),
                vision_paths=["renders/contact-sheet-focus-group-a.png"],
                vision_evidence={
                    "renders/contact-sheet-focus-group-a.png": {
                        "sha256": hashlib.sha256(b"group pixels").hexdigest(),
                        "mtime_ns": 3_000_000_000,
                    },
                },
                _dirty_visual_sources=set(),
            )
            self.assertEqual(agent_core._slide_pixel_state(child, [1, 2]), {
                "inspected_pages": [1, 2], "missing_pages": [],
                "stale_pages": [], "dirty_sources": [],
            })
            (root / "slides/slide_02.html").write_text("changed", encoding="utf-8")
            os.utime(root / "slides/slide_02.html", ns=(4_000_000_000, 4_000_000_000))
            self.assertEqual(
                agent_core._slide_pixel_state(child, [1, 2])["stale_pages"], [2]
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
                    "regression_checked: yes\nregressed_pages: none\n"
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
        self.assertIn("skills/mural-presenter/roles/review.md", review)
        self.assertIn("read_file", review)
        self.assertNotIn("<role-card", review)
        self.assertNotIn("final_pixels_inspected", review)
        english = agent_core._role_card_context("/tmp/skills", "image", "en")
        self.assertIn("Your only role-card path", english)
        self.assertNotIn("你的唯一角色卡", english)
        self.assertNotIn("读取 `skills/mural-presenter/SKILL.md`", distill_ppt.SUBAGENT_SYSTEM)
        self.assertIn("不通读根 Skill", distill_ppt.SUBAGENT_SYSTEM)
        self.assertIn("goal 只定义本次处理范围", distill_ppt.SUBAGENT_SYSTEM)
        self.assertIn("不能改写角色卡", distill_ppt.SUBAGENT_SYSTEM)
        self.assertIn("Do not scan the root SKILL.md", distill_ppt.SUBAGENT_SYSTEM_EN)
        self.assertIn("cannot override the role card", distill_ppt.SUBAGENT_SYSTEM_EN)

    def test_child_language_contract_is_explicit_and_plan_owned(self):
        chinese = agent_core._child_language_contract("zh")
        self.assertIn("回复语言：中文", chinese)
        self.assertIn("可见思考", chinese)
        self.assertIn("交付语言", chinese)
        self.assertIn("goal 中的 deliverable_language", chinese)
        self.assertIn("plan/deck.md", chinese)

        english = agent_core._child_language_contract("en")
        self.assertIn("Response language: English", english)
        self.assertIn("reasoning/thinking", english)
        self.assertIn("this task's goal", english)
        self.assertIn("plan/deck.md", english)

    def test_ui_runtime_inputs_are_resolved_only_by_orchestrator(self):
        preferences = {"page_count": 12, "attachment_count": 1}
        chinese = agent_core._generation_preferences_context(
            preferences, "orchestrator", "zh"
        )
        self.assertIn("用户最新指令", chinese)
        self.assertIn("Material 阶段", chinese)
        self.assertEqual(
            agent_core._generation_preferences_context(
                preferences, "subagent", "zh"
            ),
            "",
        )

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
            "goal": "Review: inspect the completed deck",
        })
        self.assertEqual(review["label"], "review")
        self.assertIn("vision", review["toolsets"])

        image = agent_core._normalize_task({
            "goal": "Image 02: prepare the planned photographic assets",
        })
        self.assertEqual(image["label"], "image_02")
        self.assertTrue(
            {"file", "terminal", "web", "image_gen", "vision"}.issubset(
                image["toolsets"]
            )
        )

        named_image = agent_core._normalize_task({
            "goal": "Image applications: prepare four real photographs",
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
            task = {"goal": "Image cover: subject_only: true", "context": ""}
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
            task = {"goal": "Image cover: 需要透明背景", "context": ""}
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
            "goal": "Slide Group bookends [01,12]: build the cover and closing",
        })
        self.assertEqual(named_group["label"], "slide_group_bookends")
        self.assertIn("vision", named_group["toolsets"])

        named_review = agent_core._normalize_task({
            "goal": "Review: inspect and finish the deck",
        })
        self.assertEqual(named_review["label"], "review")
        self.assertIn("vision", named_review["toolsets"])

        generic_label = agent_core._normalize_task({
            "goal": "Image motif: generate the cover motif",
        })
        self.assertEqual(generic_label["label"], "image_motif")

    def test_pixel_acceptance_requires_real_vision_calls(self):
        workers = [
            {"label": "slide-bookends", "clean": True, "vision_calls": 1},
            {"label": "review", "clean": True, "vision_calls": 1,
             "contract": {
                 "status": "ready", "mode": "final_review",
                 "content_fidelity": "not-applicable", "diagnosed_pages": "all",
                 "final_pixels_inspected": "yes", "regression_checked": "yes",
                 "regressed_pages": "none", "speech_aligned": "yes",
                 "remaining": "none",
             }},
        ]
        self.assertEqual(distill_ppt._pixel_review_acceptance(workers), (True, "ok"))
        workers[0]["vision_calls"] = 0
        ok, reason = distill_ppt._pixel_review_acceptance(workers)
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
                 "final_pixels_inspected": "yes", "regression_checked": "yes",
                 "regressed_pages": "none", "speech_aligned": "yes",
                 "remaining": "none",
             }},
        ]
        ok, reason = distill_ppt._pixel_review_acceptance(workers)
        self.assertFalse(ok)
        self.assertIn("slide-dividers:09", reason)

    def test_pixel_acceptance_requires_complete_final_review_contract(self):
        workers = [
            {"label": "slide-bookends", "clean": True, "vision_calls": 1},
            {"label": "review", "clean": True, "vision_calls": 8,
             "contract": {
                 "status": "ready", "mode": "final_review",
                 "content_fidelity": "not-applicable", "diagnosed_pages": "all",
                 "final_pixels_inspected": "yes", "regression_checked": "yes",
                 "regressed_pages": "none", "speech_aligned": "yes",
                 "remaining": "none",
             }},
        ]
        self.assertEqual(distill_ppt._pixel_review_acceptance(workers), (True, "ok"))
        workers[-1]["contract"].pop("final_pixels_inspected")
        ok, reason = distill_ppt._pixel_review_acceptance(workers)
        self.assertFalse(ok)
        self.assertIn("final_pixels_inspected", reason)

    def test_simple_edit_uses_machine_refine_count_without_false_rejection(self):
        workers = [{
            "label": "review", "kind": "review", "clean": True,
            "vision_calls": 1, "machine_refine_rounds": 1,
            "contract": {
                "status": "ready", "mode": "simple_edit",
                "content_fidelity": "not-applicable",
                "final_pixels_inspected": "yes", "regression_checked": "yes",
                "regressed_pages": "none", "speech_aligned": "yes",
                "remaining": "none", "refine_rounds": "0",
            },
        }]
        self.assertEqual(
            distill_ppt._pixel_review_acceptance(
                workers, allow_review_only=True
            ),
            (True, "ok"),
        )

    def test_second_review_cannot_replace_failed_final_review(self):
        contract = {
            "status": "ready", "mode": "final_review",
            "content_fidelity": "not-applicable", "diagnosed_pages": "all",
            "final_pixels_inspected": "yes", "regression_checked": "yes",
            "regressed_pages": "none", "speech_aligned": "yes",
            "remaining": "none",
        }
        workers = [
            {"label": "slide-bookends", "clean": True, "vision_calls": 1},
            {"label": "review", "kind": "review", "clean": False,
             "vision_calls": 1, "contract": {"status": "blocked"}},
            {"label": "review_r2", "kind": "review", "clean": True,
             "vision_calls": 1, "contract": contract},
        ]
        ok, reason = distill_ppt._pixel_review_acceptance(workers)
        self.assertFalse(ok)
        self.assertIn("单例", reason)

    def test_final_review_must_actually_view_every_contact_group(self):
        contract = {
            "status": "ready", "mode": "final_review",
            "content_fidelity": "not-applicable", "diagnosed_pages": "all",
            "final_pixels_inspected": "yes", "regression_checked": "yes",
            "regressed_pages": "none", "speech_aligned": "yes",
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
            ok, reason = distill_ppt._pixel_review_acceptance(workers, ws=str(root))
            self.assertFalse(ok)
            self.assertIn("缺页", reason)
            workers[-1]["vision_paths"].append("renders/contact-sheet-review-02.png")
            workers[-1]["vision_evidence"]["renders/contact-sheet-review-02.png"] = {
                "sha256": hashlib.sha256(b"contact-2").hexdigest(),
                "mtime_ns": (root / "renders/contact-sheet-review-02.png").stat().st_mtime_ns,
            }
            self.assertEqual(
                distill_ppt._pixel_review_acceptance(workers, ws=str(root)),
                (True, "ok"),
            )
            # Rewriting deterministic sheets with identical bytes changes only
            # mtimes and must not invalidate a visual judgment.
            for group in (1, 2):
                path = root / f"renders/contact-sheet-review-{group:02d}.png"
                os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000))
            self.assertEqual(
                distill_ppt._pixel_review_acceptance(workers, ws=str(root)),
                (True, "ok"),
            )

    def test_research_and_review_are_runtime_singletons(self):
        class Parent:
            def __init__(self):
                import threading
                self._spawn_lock = threading.Lock()
                self._role_spawn_count = {}

        parent = Parent()
        research = [agent_core._normalize_task({
            "goal": "Research: verify the named product", "toolsets": ["file", "web"]
        })]
        self.assertIsNone(agent_core._reserve_singleton_roles(parent, research))
        self.assertIn("已执行过", agent_core._reserve_singleton_roles(parent, research))

        review_batch = [
            agent_core._normalize_task({"goal": "Review: inspect all pages"}),
            agent_core._normalize_task({"goal": "Review: mode=simple_edit"}),
        ]
        self.assertIn("同一批次", agent_core._reserve_singleton_roles(parent, review_batch))

    def test_research_task_cannot_override_canonical_output_path(self):
        conflict = [agent_core._normalize_task({
            "label": "research",
            "goal": (
                "Research: 核验事实。\n"
                "你只需产出 plan/research-brief.md。\n"
                "output: plan/research-brief.md"
            ),
        })]
        self.assertIn(
            "正式产物固定为 research/research.md",
            agent_core._role_output_contract_error(conflict),
        )
        delegated = json.loads(agent_core.delegate_task(
            SimpleNamespace(_delegate_depth=0),
            tasks=[{"goal": conflict[0]["goal"]}],
        ))
        self.assertEqual(delegated["code"], "invalid_role_output_contract")

        canonical = [agent_core._normalize_task({
            "label": "research",
            "goal": "Research: 核验事实。\noutput: research/research.md",
        })]
        self.assertIsNone(agent_core._role_output_contract_error(canonical))

        negative_mention = [agent_core._normalize_task({
            "label": "research",
            "goal": (
                "Research: 核验事实。\n"
                "不得写入 plan/research-brief.md。\n"
                "output: research/research.md"
            ),
        })]
        self.assertIsNone(agent_core._role_output_contract_error(negative_mention))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            (root / "plan/research-brief.md").write_text(
                "# wrong path\n", encoding="utf-8"
            )
            worker = {
                "label": "research",
                "kind": "research",
                "clean": True,
                "contract": {
                    "status": "ready",
                    "output": "plan/research-brief.md",
                },
            }
            ok, reason = distill_ppt._research_acceptance(str(root), [worker])
            self.assertFalse(ok)
            self.assertIn("research/research.md", reason)

    def test_material_output_is_canonical_and_write_boundary_matches_role_card(self):
        conflict = [agent_core._normalize_task({
            "label": "material_a1",
            "goal": (
                "Material A1: 处理附件。\n"
                "assignment_id: A1\n"
                "output: plan/materials-A1.md"
            ),
        })]
        self.assertIn(
            "materials/summaries/<assignment_id>.md",
            agent_core._role_output_contract_error(conflict),
        )
        canonical = [agent_core._normalize_task({
            "label": "material_a1",
            "goal": (
                "Material A1: 处理附件。\n"
                "assignment_id: A1\n"
                "output: materials/summaries/A1.md"
            ),
        })]
        self.assertIsNone(agent_core._role_output_contract_error(canonical))
        self.assertIn("research", agent_core._ROLE_WRITE_BOUNDARIES["material"])

        with tempfile.TemporaryDirectory() as temporary:
            parent = SimpleNamespace(ws=temporary)
            child = SimpleNamespace()
            wrong = {
                "status": "ready", "coverage": "complete",
                "output": "materials/_work/A1/material_A1.md",
            }
            self.assertIn(
                "materials/summaries/<assignment_id>.md",
                agent_core._child_contract_error(parent, child, "material", wrong),
            )
            output = Path(temporary) / "materials/summaries/A1.md"
            output.parent.mkdir(parents=True)
            output.write_text("# complete\n", encoding="utf-8")
            ready = {
                "status": "ready", "coverage": "complete",
                "output": "materials/summaries/A1.md",
            }
            self.assertEqual(
                agent_core._child_contract_error(parent, child, "material", ready), ""
            )

    def test_attachments_must_be_understood_before_research(self):
        research = agent_core._normalize_task({
            "goal": "Research: 核验附件仍未解决的事实",
        })
        material = agent_core._normalize_task({
            "goal": (
                "Material A1: 处理附件。\n"
                "assignment_id: A1\n"
                "output: materials/summaries/A1.md"
            ),
        })
        no_attachments = SimpleNamespace(generation_preferences={})
        self.assertIsNone(
            agent_core._material_before_research_error(no_attachments, [research])
        )

        same_wave = SimpleNamespace(generation_preferences={"attachment_count": 1})
        self.assertIn(
            "不能同批",
            agent_core._material_before_research_error(same_wave, [material, research]),
        )
        self.assertIn(
            "尚未完成",
            agent_core._material_before_research_error(same_wave, [research]),
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "materials/summaries").mkdir(parents=True)
            (root / "materials/_work/A1").mkdir(parents=True)
            (root / "materials/attachments.json").write_text(
                json.dumps({"attachments": [{"name": "brief.md"}]}),
                encoding="utf-8",
            )
            (root / "materials/_work/A1/catalog.json").write_text(
                json.dumps([{
                    "name": "brief.md",
                    "status": "ok",
                    "coverage": {"status": "complete"},
                }]),
                encoding="utf-8",
            )
            summary = "materials/summaries/A1.md"
            (root / summary).write_text("# 完整附件摘要\n", encoding="utf-8")
            parent = SimpleNamespace(
                ws=str(root),
                generation_preferences={"attachment_count": 1},
                worker_recs=[{
                    "label": "material_a1",
                    "kind": "material",
                    "clean": True,
                    "contract": {
                        "status": "ready",
                        "coverage": "complete",
                        "output": summary,
                    },
                }],
                _completed_read_paths=set(),
            )
            self.assertIn(
                "读到末尾",
                agent_core._material_before_research_error(parent, [research]),
            )
            parent._completed_read_paths.add(tools._read_key(summary))

            incomplete_goal = agent_core._normalize_task({
                "goal": "Research: 根据附件继续检索",
            })
            self.assertIn(
                "原始 query",
                agent_core._material_before_research_error(parent, [incomplete_goal]),
            )
            complete_goal = agent_core._normalize_task({
                "goal": (
                    "Research: 根据附件后的真实缺口核验。\n"
                    "原始 query：请基于附件制作演示文稿。\n"
                    f"Material summaries: {summary}\n"
                    "证据缺口：核验附件未覆盖的公开时间线。"
                ),
            })
            self.assertIsNone(
                agent_core._material_before_research_error(parent, [complete_goal])
            )

    def test_image_asset_review_cannot_consume_review_singleton(self):
        import threading

        class Parent:
            def __init__(self):
                self._spawn_lock = threading.Lock()
                self._role_spawn_count = {}

        image = agent_core._normalize_task({
            "goal": "Image science: generate assets, then run deck.py asset-review",
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

    def test_orchestrator_cannot_bypass_material_with_raw_attachment_reads(self):
        class Agent:
            role = "orchestrator"
            ws = "/tmp/does-not-matter"

            def read_path(self, path):
                raise AssertionError("raw attachment must be blocked before filesystem access")

        direct = tools.read_file(Agent(), "materials/_raw/brief.md")
        self.assertIn("阻止编排器直接读取附件", direct)
        intermediate = tools.read_file(Agent(), "materials/_work/A1/catalog.json")
        self.assertIn("materials/summaries", intermediate)
        shell = tools.terminal(Agent(), "cat materials/_raw/brief.md | head -100")
        self.assertIn("编排器不得通过 shell", shell)

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

    def test_grouped_slide_progress_is_not_turn_or_wall_clock_bounded_by_default(self):
        single = {"goal": "Slide 7: finish one page", "context": ""}
        four_pages = {
            "goal": "Slide Group content-02 [10, 11, 12, 13]: build the group",
            "context": "",
        }
        six_pages = {
            "goal": "Slide Group content [03,04,05,06,07,08]: build the assigned pages",
            "context": "",
        }

        self.assertEqual(agent_core._slide_group_page_count(single), 1)
        self.assertEqual(agent_core._slide_group_pages(single), [7])
        self.assertEqual(agent_core._slide_group_pages(four_pages), [10, 11, 12, 13])
        self.assertEqual(agent_core._slide_group_page_count(four_pages), 4)
        self.assertEqual(agent_core._slide_group_page_count(six_pages), 6)
        self.assertFalse(hasattr(agent_core, "_slide_turn_budget"))
        self.assertIsNone(agent_core._slide_timeout_budget(single))
        self.assertIsNone(agent_core._slide_timeout_budget(six_pages))

    def test_oversized_group_still_has_no_hidden_default_deadline(self):
        task = {"goal": "Slide Group oversized [01-20]: build pages", "context": ""}
        self.assertEqual(agent_core._slide_group_page_count(task), 20)
        self.assertIsNone(agent_core._slide_timeout_budget(task))

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

    def test_first_dispatch_cannot_serialize_one_frozen_group_per_call(self):
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

            self.assertEqual(len(canonical), 1)
            self.assertIn("首次 Slide 派发", error)
            self.assertIn("content", error)

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
                "goal": "Slide Group a [01,02]: build"
            })]
            self.assertIsNone(agent_core._reserve_singleton_roles(parent, first_group))
            self.assertEqual(
                parent._slide_page_owners,
                {1: "slide_group_a", 2: "slide_group_a"},
            )

            second_group = [agent_core._normalize_task({
                "goal": "Slide Group b [03]: build"
            })]
            self.assertIsNone(agent_core._reserve_singleton_roles(parent, second_group))
            self.assertEqual(
                parent._slide_page_owners,
                {1: "slide_group_a", 2: "slide_group_a", 3: "slide_group_b"},
            )

            duplicate = [agent_core._normalize_task({
                "goal": "Slide Group c [03]: build"
            })]
            self.assertIn("已归属", agent_core._reserve_singleton_roles(parent, duplicate))

            gap_root = root / "gap"
            (gap_root / "plan").mkdir(parents=True)
            (gap_root / "plan/slide_01.md").write_text("# plan", encoding="utf-8")
            (gap_root / "plan/slide_03.md").write_text("# plan", encoding="utf-8")
            gap_parent = Parent(gap_root)
            gap_tasks = [agent_core._normalize_task({
                "goal": "Slide Group gap [01,03]: build"
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
                distill_ppt._slide_assignment_acceptance(root, records),
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
                distill_ppt._slide_assignment_acceptance(root, records),
                (True, "ok"),
            )

    def test_four_page_new_deck_requires_multiple_production_groups(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            for relative in ("design-brief.md", "deck.md", "theme.css"):
                (root / "plan" / relative).write_text("x\n", encoding="utf-8")
            for page in range(1, 5):
                (root / f"plan/slide_{page:02d}.md").write_text(
                    f"# Slide {page:02d}\n- production_group：all\n",
                    encoding="utf-8",
                )
            (root / "base.css").write_text(":root{}\n", encoding="utf-8")
            (root / "speech.md").write_text("# speech\n", encoding="utf-8")
            parent = SimpleNamespace(
                ws=str(root), cfg={}, generation_preferences={"page_count": 4},
            )
            error = agent_core._planning_before_slide_error(
                parent,
                [{"label": "slide_group_all", "goal": "Slide Group all [01,02,03,04]: go"}],
            )
            self.assertIn("至少需要两个", error)

    def test_first_slide_wave_contains_all_frozen_groups(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            for page, group in ((1, "bookends"), (2, "content"), (3, "content"), (4, "bookends")):
                (root / f"plan/slide_{page:02d}.md").write_text(
                    f"# Slide {page:02d}\n- production_group：{group}\n",
                    encoding="utf-8",
                )
            parent = SimpleNamespace(
                ws=str(root), cfg={}, worker_recs=[], _spawn_lock=threading.Lock(),
            )
            one, error = agent_core._canonicalize_slide_tasks(parent, [
                agent_core._normalize_task({
                    "goal": "Slide Group bookends [01,04]: build",
                })
            ])
            self.assertTrue(one)
            self.assertIn("首次 Slide 派发", error)
            both, error = agent_core._canonicalize_slide_tasks(parent, [
                agent_core._normalize_task({
                    "goal": "Slide Group bookends [01,04]: build",
                }),
                agent_core._normalize_task({
                    "goal": "Slide Group content [02,03]: build",
                }),
            ])
            self.assertIsNone(error)
            self.assertEqual(len(both), 2)

    def test_child_role_write_boundaries_protect_truth_sources(self):
        self.assertIn("plan", agent_core._ROLE_WRITE_BOUNDARIES["slide"])
        self.assertIn("assets", agent_core._ROLE_WRITE_BOUNDARIES["slide"])
        self.assertNotIn("slides", agent_core._ROLE_WRITE_BOUNDARIES["slide"])
        self.assertIn("slides", agent_core._ROLE_WRITE_BOUNDARIES["image"])
        self.assertNotIn("assets", agent_core._ROLE_WRITE_BOUNDARIES["image"])
        self.assertNotIn("speech.md", agent_core._ROLE_WRITE_BOUNDARIES["review"])

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

    def test_research_partial_requires_unresolved_and_grounded_propagation(self):
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
            ok, reason = distill_ppt._research_acceptance(str(root), [worker])
            self.assertFalse(ok)
            self.assertIn("未传播", reason)
            (root / "plan/grounded-knowledge.md").write_text(
                "## 未解决与使用边界\n票房口径仍未核验\n", encoding="utf-8"
            )
            self.assertEqual(
                distill_ppt._research_acceptance(str(root), [worker]), (True, "ok")
            )
            worker["contract"]["status"] = "blocked"
            worker["clean"] = False
            self.assertFalse(distill_ppt._research_acceptance(str(root), [worker])[0])

    def test_grounding_is_required_only_after_an_evidence_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            self.assertEqual(
                distill_ppt._grounded_acceptance(
                    str(root), require_materials=False, research_workers=[]
                ),
                (True, "ok"),
            )
            self.assertFalse(
                distill_ppt._grounded_acceptance(
                    str(root), require_materials=False,
                    research_workers=[{"label": "research"}],
                )[0]
            )
            (root / "plan/grounded-knowledge.md").write_text(
                "# Grounded knowledge\n\n## Verified\nA sufficiently explicit handoff.\n",
                encoding="utf-8",
            )
            self.assertEqual(
                distill_ppt._grounded_acceptance(
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
            self.assertFalse(distill_ppt._image_acceptance(str(root), [])[0])
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
                distill_ppt._image_acceptance(
                    str(root), [], allow_existing_assets=True
                ),
                (True, "ok"),
            )
            self.assertEqual(
                distill_ppt._image_acceptance(str(root), [worker]), (True, "ok")
            )
            worker["contract"]["status"] = "blocked"
            worker["clean"] = False
            self.assertFalse(distill_ppt._image_acceptance(str(root), [worker])[0])

    def test_slide_waits_for_ready_image_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            (root / "assets").mkdir()
            (root / "plan/slide_01.md").write_text(
                "image_opportunity: real_required\nasset_id: hero-city\n",
                encoding="utf-8",
            )
            parent = SimpleNamespace(
                ws=str(root), cfg={}, worker_recs=[], _spawn_lock=threading.Lock()
            )
            slide = {"label": "slide_group_hero", "goal": "Slide Group hero [01]: 制作页面"}
            image = {"label": "image_hero", "goal": "Image hero: 准备 hero-city"}
            self.assertIn(
                "Image 尚未完成",
                agent_core._image_before_slide_error(parent, [slide]),
            )
            self.assertIn(
                "不能同批委派",
                agent_core._image_before_slide_error(parent, [image, slide]),
            )

            (root / "assets/hero.jpg").write_bytes(b"image")
            (root / "assets/catalog.json").write_text(json.dumps({
                "schema_version": 2,
                "assets": [{
                    "asset_id": "hero-city",
                    "path": "assets/hero.jpg",
                    "status": "ready",
                }],
            }), encoding="utf-8")
            parent.worker_recs.append({
                "label": "image_hero", "kind": "image", "clean": True,
                "contract": {"status": "ready"},
            })
            self.assertIsNone(agent_core._image_before_slide_error(parent, [slide]))

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
                    {"type": "text", "text": "y" * 5000},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "a", "content": "x" * 9000},
                    {"type": "tool_result", "tool_use_id": "b", "content": "status: ready\noutput: research/research.md"},
                ]},
                {"role": "assistant", "content": [{"type": "text", "text": "recent"}]},
            ]
            with tempfile.TemporaryDirectory() as temporary:
                trace_dir = Path(temporary) / "_trace/orchestrator"
                trace_dir.mkdir(parents=True)
                agent = SimpleNamespace(
                    ws=temporary,
                    trace=SimpleNamespace(sub_dir=str(trace_dir)),
                )
                saved = agent_core._compact_active_history(messages, agent=agent)
                self.assertGreater(saved, 0)
                compact = messages[2]["content"][0]["content"]
                self.assertIn("历史工具结果已压缩", compact)
                self.assertIn("exact_result:", compact)
                archive = trace_dir / "history-results/a.txt"
                self.assertEqual(archive.read_text(encoding="utf-8"), "x" * 9000)
                self.assertEqual(
                    messages[2]["content"][1]["content"],
                    "status: ready\noutput: research/research.md",
                )
                self.assertEqual(messages[1]["content"][0]["id"], "a")
                compact_text = messages[1]["content"][2]["text"]
                self.assertIn("早期消息文本已压缩", compact_text)
                text_archive = trace_dir / "history-results/assistant_1_2.txt"
                self.assertEqual(text_archive.read_text(encoding="utf-8"), "y" * 5000)
        finally:
            agent_core.HISTORY_COMPACT_AFTER_CHARS = prior_threshold
            agent_core.HISTORY_KEEP_RECENT_MESSAGES = prior_keep

    def test_stage_observer_research_progress_lease_requires_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace_dir = root / "_trace/subagents/research"
            trace_dir.mkdir(parents=True)
            agent = SimpleNamespace(
                ws=str(root),
                trace=SimpleNamespace(sub_dir=str(trace_dir)),
                label="research",
                role="subagent",
                initial_user="research this topic",
                prompt_language="zh",
                worker_recs=[],
            )
            observer = agent_core.stage_memory.StageObserver(agent, "research")
            observer.observe(0, [SimpleNamespace(
                name="web_search", input={"query": "independent query 0"},
            )])
            observer.observe(1, [SimpleNamespace(
                name="web_extract", input={"urls": ["https://example.test/source"]},
            )])
            blocked = observer.preflight_tool(
                "web_search", {"query": "another evidence route"}
            )
            self.assertIn("阶段进展门已阻止", blocked)
            self.assertIn("research/research.md", blocked)

            (root / "research").mkdir()
            (root / "research/research.md").write_text(
                "# 已核验\n- claim\n## 未解决\n- another route\n",
                encoding="utf-8",
            )
            observer.observe(2, [SimpleNamespace(
                name="write_file", input={"path": "research/research.md"},
            )])
            self.assertEqual(
                observer.preflight_tool(
                    "web_search", {"query": "focused unresolved claim"}
                ),
                "",
            )
            state = json.loads(
                (trace_dir / "stage-state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["activity"]["tools"]["web_search"], 1)
            self.assertEqual(state["activity"]["tools"]["web_extract"], 1)
            self.assertEqual(state["convergence"]["gate"], "open")

    def test_image_progress_lease_blocks_expansion_until_catalog_decision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = root / "assets"
            trace_dir = root / "_trace/subagents/image_hero"
            assets.mkdir(parents=True)
            trace_dir.mkdir(parents=True)
            candidate = assets / "candidate.png"
            candidate.write_bytes(b"candidate")
            catalog_path = assets / "catalog.json"
            catalog_path.write_text(json.dumps({
                "schema_version": 2,
                "assets": [{
                    "path": "assets/candidate.png",
                    "origin": "downloaded",
                    "status": "unassigned",
                }],
            }), encoding="utf-8")
            agent = SimpleNamespace(
                ws=str(root),
                trace=SimpleNamespace(sub_dir=str(trace_dir)),
                label="image_hero",
                role="subagent",
                initial_user="Image hero:\ngroup_id: hero",
                prompt_language="zh",
                worker_recs=[],
                vision_paths=[],
                _created_asset_paths=set(),
            )
            observer = agent_core.stage_memory.StageObserver(agent, "image")

            agent._created_asset_paths.add("assets/candidate.png")
            observer.observe(0, [SimpleNamespace(
                name="terminal", input={"command": "python deck.py asset-download . --url 'https://example.test/candidate.png'"},
            )])
            blocked = observer.preflight_tool(
                "web_search", {"query": "another candidate"}
            )
            self.assertIn("阶段进展门已阻止", blocked)
            self.assertEqual(
                observer.preflight_tool(
                    "vision_analyze", {"image_url": "assets/candidate.png"}
                ),
                "",
            )

            agent.vision_paths.append("assets/candidate.png")
            observer.observe(1, [SimpleNamespace(
                name="vision_analyze", input={"image_url": "assets/candidate.png"},
            )])
            blocked = observer.preflight_tool(
                "terminal", {"command": "python deck.py asset-download . --url 'https://example.test/more.png'"}
            )
            self.assertIn("先把已检查候选明确写成", blocked)

            catalog_path.write_text(json.dumps({
                "schema_version": 2,
                "assets": [{
                    "path": "assets/candidate.png",
                    "origin": "downloaded",
                    "asset_id": "hero-cover",
                    "group_id": "hero",
                    "status": "rejected",
                }],
            }), encoding="utf-8")
            observer.observe(2, [SimpleNamespace(
                name="terminal",
                input={"command": "python deck.py asset-review . --group-id hero --rejected hero-cover"},
            )])
            self.assertEqual(
                observer.preflight_tool(
                    "terminal", {"command": "python deck.py asset-download . --url 'https://example.test/more.png'"}
                ),
                "",
            )
            state = json.loads(
                (trace_dir / "stage-state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["convergence"]["mode"], "progress_lease")
            self.assertEqual(state["convergence"]["gate"], "open")

    def test_image_output_contract_rejects_parallel_manifest(self):
        tasks = [agent_core._normalize_task({
            "goal": (
                "Image hero: group_id: hero\n"
                "Produce assets/manifest.md after downloading the images."
            ),
        })]
        error = agent_core._role_output_contract_error(tasks)
        self.assertIn("assets/catalog.json", error)
        self.assertIn("assets/manifest.md", error)

    def test_image_contact_hint_survives_fetch_vision_interleaving(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace_dir = root / "_trace/subagents/image"
            trace_dir.mkdir(parents=True)
            agent = SimpleNamespace(
                ws=str(root),
                trace=SimpleNamespace(sub_dir=str(trace_dir)),
                label="image",
                role="subagent",
                initial_user="Image gallery: group_id: gallery",
                prompt_language="zh",
                worker_recs=[],
                vision_paths=[],
                _created_asset_paths=set(),
            )
            observer = agent_core.stage_memory.StageObserver(agent, "image")
            hints = []
            for index in range(3):
                agent._created_asset_paths.add(f"assets/candidate-{index}.png")
                observer.observe(index * 2, [SimpleNamespace(
                    name="terminal", input={
                        "command": f"python deck.py asset-download . --url 'https://example.test/{index}.png'"
                    },
                )])
                agent.vision_paths.append(f"assets/candidate-{index}.png")
                _changed, hints = observer.observe(index * 2 + 1, [SimpleNamespace(
                    name="vision_analyze",
                    input={"image_url": f"assets/candidate-{index}.png"},
                )])
            self.assertTrue(hints)
            self.assertIn("素材联系表", hints[0])

    def test_stage_checkpoint_archives_exact_old_pairs_and_keeps_raw_query(self):
        previous_threshold = agent_core.HISTORY_CHECKPOINT_AFTER_CHARS
        previous_keep = agent_core.HISTORY_CHECKPOINT_KEEP_MESSAGES
        try:
            agent_core.HISTORY_CHECKPOINT_AFTER_CHARS = 10
            agent_core.HISTORY_CHECKPOINT_KEEP_MESSAGES = 4
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                trace_dir = root / "_trace/subagents/research"
                trace_dir.mkdir(parents=True)
                (root / "research").mkdir()
                (root / "research/research.md").write_text(
                    "# canonical evidence\n", encoding="utf-8"
                )
                agent = SimpleNamespace(
                    ws=str(root),
                    trace=SimpleNamespace(sub_dir=str(trace_dir)),
                    label="research",
                    role="subagent",
                    initial_user="RAW QUERY MUST STAY EXACT",
                    prompt_language="en",
                    worker_recs=[],
                    _context_checkpoint_index=0,
                    _stage_memory_context="",
                )
                observer = agent_core.stage_memory.StageObserver(agent, "research")
                messages = [{"role": "user", "content": agent.initial_user}]
                for index in range(4):
                    messages.extend((
                        {"role": "assistant", "content": [{
                            "type": "tool_use", "id": f"t{index}",
                            "name": "web_search", "input": {"query": str(index)},
                        }]},
                        {"role": "user", "content": [{
                            "type": "tool_result", "tool_use_id": f"t{index}",
                            "content": "evidence " + "x" * 80,
                        }]},
                    ))
                original = json.loads(json.dumps(messages))
                count, relative = agent_core._checkpoint_active_history(
                    messages, agent, observer
                )
                self.assertGreater(count, 0)
                self.assertEqual(messages[0]["content"], "RAW QUERY MUST STAY EXACT")
                self.assertEqual(messages[1]["role"], "assistant")
                archive = json.loads((root / relative).read_text(encoding="utf-8"))
                self.assertEqual(
                    archive["removed_messages"], original[1:1 + count]
                )
                self.assertIn(relative, agent._stage_memory_context)
        finally:
            agent_core.HISTORY_CHECKPOINT_AFTER_CHARS = previous_threshold
            agent_core.HISTORY_CHECKPOINT_KEEP_MESSAGES = previous_keep

    def test_stage_checkpoint_waits_for_pending_read_continuation(self):
        previous_threshold = agent_core.HISTORY_CHECKPOINT_AFTER_CHARS
        try:
            agent_core.HISTORY_CHECKPOINT_AFTER_CHARS = 10
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                trace_dir = root / "_trace/orchestrator"
                trace_dir.mkdir(parents=True)
                (root / "plan").mkdir()
                (root / "plan/design-brief.md").write_text("# direction\n", encoding="utf-8")
                agent = SimpleNamespace(
                    ws=str(root), trace=SimpleNamespace(sub_dir=str(trace_dir)),
                    label="orch", role="orchestrator", initial_user="raw",
                    prompt_language="zh", worker_recs=[],
                    _context_checkpoint_index=0, _stage_memory_context="",
                    _pending_read_continuations={"skills/mural-presenter/SKILL.md": 201},
                )
                observer = agent_core.stage_memory.StageObserver(agent, "other")
                messages = [
                    {"role": "user", "content": "raw"},
                    {"role": "assistant", "content": [{
                        "type": "tool_use", "id": "t1", "name": "read_file",
                        "input": {"path": "skills/mural-presenter/SKILL.md"},
                    }]},
                    {"role": "user", "content": [{
                        "type": "tool_result", "tool_use_id": "t1",
                        "content": "x" * 200,
                    }]},
                ]
                count, relative = agent_core._checkpoint_active_history(messages, agent, observer)
                self.assertEqual((count, relative), (0, ""))
        finally:
            agent_core.HISTORY_CHECKPOINT_AFTER_CHARS = previous_threshold

    def test_orchestrator_canonical_file_change_renews_progress_lease(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace_dir = root / "_trace/orchestrator"
            trace_dir.mkdir(parents=True)
            agent = SimpleNamespace(
                ws=str(root), trace=SimpleNamespace(sub_dir=str(trace_dir)),
                label="orch", role="orchestrator", initial_user="raw",
                prompt_language="zh", worker_recs=[],
            )
            observer = agent_core.stage_memory.StageObserver(agent, "other")
            (root / "plan").mkdir()
            (root / "plan/slide_01.md").write_text("# Slide 01\n", encoding="utf-8")
            observer.observe(1, [SimpleNamespace(
                name="write_file", input={"path": "plan/slide_01.md"},
            )])
            self.assertTrue(observer.progress_renewed)

    def test_slide_dispatch_requires_every_plan_and_prepare_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            for relative in ("design-brief.md", "deck.md", "theme.css"):
                (root / "plan" / relative).write_text("x\n", encoding="utf-8")
            parent = SimpleNamespace(
                ws=str(root), generation_preferences={"page_count": 2}, cfg={},
            )
            tasks = [{"label": "slide_group_g1", "goal": "Slide Group G1 [01,02]: go"}]
            error = agent_core._planning_before_slide_error(parent, tasks)
            self.assertIn("missing_pages=01,02", error)
            for page in (1, 2):
                (root / f"plan/slide_{page:02d}.md").write_text("# plan\n", encoding="utf-8")
            (root / "base.css").write_text(":root{}\n", encoding="utf-8")
            (root / "speech.md").write_text("# speech\n", encoding="utf-8")
            self.assertIsNone(agent_core._planning_before_slide_error(parent, tasks))

    def test_orchestrator_stage_waits_for_slide_plans_and_prepare(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace_dir = root / "_trace/orchestrator"
            trace_dir.mkdir(parents=True)
            (root / "plan").mkdir()
            for relative in ("design-brief.md", "deck.md", "theme.css"):
                (root / "plan" / relative).write_text("x\n", encoding="utf-8")
            agent = SimpleNamespace(
                ws=str(root), trace=SimpleNamespace(sub_dir=str(trace_dir)),
                label="orch", role="orchestrator", initial_user="raw",
                prompt_language="zh", worker_recs=[], cfg={},
                generation_preferences={"page_count": 2},
            )
            observer = agent_core.stage_memory.StageObserver(agent, "other")
            self.assertEqual(observer.state["stage"], "deck_planning")
            for page in (1, 2):
                (root / f"plan/slide_{page:02d}.md").write_text("# plan\n", encoding="utf-8")
            (root / "base.css").write_text(":root{}\n", encoding="utf-8")
            (root / "speech.md").write_text("# speech\n", encoding="utf-8")
            observer.refresh(2)
            self.assertEqual(observer.state["stage"], "production_dispatch")

    def test_workspace_preflight_runs_only_for_sample_specific_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertFalse(distill_ppt._needs_workspace_input_check(str(root), []))
            self.assertTrue(distill_ppt._needs_workspace_input_check(
                str(root), [{"name": "brief.pdf"}]
            ))
            (root / "materials").mkdir()
            (root / "materials/font-config.json").write_text("{}\n", encoding="utf-8")
            self.assertTrue(distill_ppt._needs_workspace_input_check(str(root), []))

    def test_nova_delivery_status_follows_final_acceptance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "task_result.json").write_text(json.dumps({
                "status": "completed", "sample_id": "sample",
            }), encoding="utf-8")
            agent = SimpleNamespace(nova_raw=SimpleNamespace(root=root))
            payload = nova_bridge.sync_acceptance_status(
                agent, False, "Review pixels rejected"
            )
            self.assertEqual(payload["status"], "rejected")
            self.assertFalse(payload["delivery_acceptance"]["accepted"])
            saved = json.loads((root / "task_result.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "rejected")

            saved["status"] = "quarantine"
            (root / "task_result.json").write_text(json.dumps(saved), encoding="utf-8")
            payload = nova_bridge.sync_acceptance_status(agent, True, "ok")
            self.assertEqual(payload["status"], "quarantine")

    def test_skill_snapshot_is_real_immutable_tree_with_hash(self):
        previous = distill_ppt.SKILLS_DIR
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source_root = root / "source"
                skill = source_root / "mural-presenter"
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text("version one", encoding="utf-8")
                run = root / "run"
                run.mkdir()
                distill_ppt.SKILLS_DIR = str(source_root)
                skills_root = Path(distill_ppt._snapshot_skill(str(run)))
                snapshot = skills_root / "mural-presenter/SKILL.md"
                self.assertFalse((run / "skills").is_symlink())
                self.assertEqual(snapshot.read_text(encoding="utf-8"), "version one")
                manifest = json.loads(
                    (run / "_trace/skill-snapshot.json").read_text(encoding="utf-8")
                )
                first_hash = manifest["tree_sha256"]
                (skill / "SKILL.md").write_text("version two", encoding="utf-8")
                distill_ppt._snapshot_skill(str(run))
                self.assertEqual(snapshot.read_text(encoding="utf-8"), "version one")
                second = json.loads(
                    (run / "_trace/skill-snapshot.json").read_text(encoding="utf-8")
                )
                self.assertEqual(second["tree_sha256"], first_hash)
        finally:
            distill_ppt.SKILLS_DIR = previous


if __name__ == "__main__":
    unittest.main()
