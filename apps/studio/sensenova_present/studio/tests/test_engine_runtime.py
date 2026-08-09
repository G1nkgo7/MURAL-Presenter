import unittest
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch
from jinja2 import Environment, FileSystemLoader

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import engine, main as studio_main


class EngineRuntimeTests(unittest.TestCase):
    def _require_all_external_runtime(self):
        missing = [
            key for key, skill in engine.SKILLS.items()
            if not skill.get("ready", False)
        ]
        missing.extend(
            key for key, pipeline in engine.PIPELINES.items()
            if not pipeline.get("ready", False)
        )
        if missing:
            self.skipTest("external SenseNova Skills/Harnesses are not mounted: " + ", ".join(missing))

    def test_deck_submission_has_no_per_user_active_task_ceiling(self):
        source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("STUDIO_USER_MAX_ACTIVE", source)
        self.assertNotIn("USER_MAX_ACTIVE", source)
        self.assertNotIn("_check_user_quota", source)
        self.assertNotIn("同时最多生成", source)

    def test_static_skills_are_registered(self):
        self._require_all_external_runtime()
        self.assertEqual(
            list(engine.SKILLS),
            [
                "sense-present-standard",
                "sense-present-dazzle",
                "auto",
                "zh",
                "en",
                "long-horizon",
                "long-horizon-grouped",
                "long-horizon-grouped-inline-image",
                "visual-craft",
                "long-horizon-presenter",
            ],
        )
        auto = engine.SKILLS["auto"]
        self.assertEqual(Path(auto["path"]), engine.CLEAN_SKILLS_ROOT)
        self.assertIsNone(auto["name"])
        self.assertEqual(auto["language"], "auto")
        expected = {
            "zh": engine.CLEAN_SKILLS_ROOT / "ppt-skill-html-clean-zh",
            "en": engine.CLEAN_SKILLS_ROOT / "ppt-skill-html-clean-en",
        }
        for key, expected_path in expected.items():
            with self.subTest(skill=key):
                skill = engine.SKILLS[key]
                self.assertEqual(Path(skill["path"]), expected_path)
                self.assertTrue((expected_path / "SKILL.md").is_file())
                self.assertEqual(skill["language"], key)
                self.assertEqual(skill["pipeline"], "infer")
                self.assertEqual(skill["harness_entry"], "infer.py")

        long_horizon = engine.SKILLS["long-horizon"]
        expected_long_horizon = engine.LONG_HORIZON_SKILLS_ROOT / "long-horizon-html-ppt"
        self.assertEqual(Path(long_horizon["path"]), expected_long_horizon)
        self.assertTrue((expected_long_horizon / "SKILL.md").is_file())
        self.assertTrue((expected_long_horizon / "scripts" / "deck.py").is_file())
        self.assertEqual(long_horizon["name"], "long-horizon-html-ppt")
        self.assertEqual(long_horizon["language"], "zh")
        self.assertEqual(long_horizon["deck_language"], "auto")
        self.assertEqual(long_horizon["pipeline"], "infer")
        self.assertTrue(long_horizon["ready"])
        self.assertEqual(len(long_horizon["required_files"]), 21)
        self.assertGreaterEqual(long_horizon["source_file_count"], 20)
        self.assertEqual(long_horizon["source_file_count"], len(long_horizon["source_files"]))
        self.assertIn("roles/orchestrator.md", long_horizon["required_files"])
        self.assertIn("roles/review.md", long_horizon["required_files"])
        self.assertIn("references/visual-direction.md", long_horizon["required_files"])
        self.assertIn("scripts/workspace_policy.py", long_horizon["required_files"])
        self.assertIn("scripts/font_bundle.py", long_horizon["required_files"])
        self.assertRegex(long_horizon["source_revision"], r"^[0-9a-f]{12}$")
        for relative in long_horizon["required_files"]:
            with self.subTest(long_horizon_file=relative):
                self.assertTrue((expected_long_horizon / relative).is_file())
        for relative in long_horizon["source_files"]:
            with self.subTest(long_horizon_source_file=relative):
                self.assertTrue((expected_long_horizon / relative).is_file())

        grouped = engine.SKILLS["long-horizon-grouped"]
        expected_grouped = (
            engine.CLEAN_SKILLS_ROOT
            / "long-horizon-html-ppt-grouped"
        )
        self.assertEqual(Path(grouped["path"]), expected_grouped)
        self.assertEqual(Path(grouped["skills_root"]), engine.CLEAN_SKILLS_ROOT)
        self.assertEqual(grouped["name"], "long-horizon-html-ppt-grouped")
        self.assertEqual(grouped["label"], "Long-horizon HTML PPT Grouped")
        self.assertEqual(grouped["pairing"], "long-horizon-grouped-current")
        self.assertTrue(grouped["ready"])
        self.assertEqual(len(grouped["required_files"]), 20)
        self.assertNotIn("roles/orchestrator.md", grouped["required_files"])
        self.assertIn("roles/material.md", grouped["required_files"])
        self.assertIn("roles/review.md", grouped["required_files"])
        self.assertRegex(grouped["source_revision"], r"^[0-9a-f]{12}$")

        grouped_inline = engine.SKILLS["long-horizon-grouped-inline-image"]
        expected_grouped_inline = (
            engine.CLEAN_SKILLS_ROOT
            / "long-horizon-html-ppt-grouped-inline-image"
        )
        self.assertEqual(Path(grouped_inline["path"]), expected_grouped_inline)
        self.assertEqual(
            grouped_inline["name"],
            "long-horizon-html-ppt-grouped-inline-image",
        )
        self.assertEqual(
            grouped_inline["pairing"],
            "long-horizon-grouped-inline-image-ab",
        )
        self.assertTrue(grouped_inline["ready"], grouped_inline["unavailable_reason"])
        self.assertIn(
            "references/slide-image-routing.md",
            grouped_inline["required_files"],
        )
        self.assertNotIn("roles/image.md", grouped_inline["required_files"])
        self.assertRegex(grouped_inline["source_revision"], r"^[0-9a-f]{12}$")

        visual_craft = engine.SKILLS["visual-craft"]
        self.assertEqual(visual_craft["label"], "Visual Craft HTML PPT")
        self.assertEqual(Path(visual_craft["path"]), engine.VISUAL_CRAFT_SKILL_ROOT)
        self.assertEqual(Path(visual_craft["harness_path"]), engine.VISUAL_CRAFT_HARNESS_ROOT)
        self.assertEqual(visual_craft["harness_entry"], "distill_ppt.py")
        self.assertEqual(visual_craft["pipeline"], "visual-craft-harness")
        self.assertEqual(visual_craft["pairing"], "visual-craft-paired")
        self.assertIn("revision", visual_craft["caps"])
        self.assertRegex(visual_craft["source_revision"], r"^[0-9a-f]{12}$")
        self.assertTrue(visual_craft["ready"], visual_craft["unavailable_reason"])
        self.assertIn("scripts/font_bundle.py", visual_craft["required_files"])
        self.assertNotIn("scripts/ai_slop_lint.py", visual_craft["required_files"])
        mount = Path(visual_craft["skills_root"]) / "ppt-skill-html"
        self.assertTrue(mount.is_symlink())
        self.assertEqual(mount.resolve(), engine.VISUAL_CRAFT_SKILL_ROOT.resolve())

        presenter = engine.SKILLS["long-horizon-presenter"]
        self.assertEqual(presenter["label"], "Long-Horizon Presenter")
        self.assertEqual(
            Path(presenter["path"]), engine.LONG_HORIZON_PRESENTER_SKILL_ROOT
        )
        self.assertEqual(
            Path(presenter["harness_path"]),
            engine.LONG_HORIZON_PRESENTER_HARNESS_ROOT,
        )
        self.assertEqual(presenter["pipeline"], "long-horizon-presenter-harness")
        self.assertEqual(presenter["pairing"], "long-horizon-presenter-paired")
        self.assertEqual(presenter["name"], "long-horizon-presenter")
        self.assertIn("static_html", presenter["caps"])
        self.assertTrue(presenter["ready"], presenter["unavailable_reason"])
        self.assertIn("scripts/deck.py", presenter["required_files"])
        self.assertIn("references/editing-contract.md", presenter["required_files"])
        self.assertRegex(presenter["source_revision"], r"^[0-9a-f]{12}$")

    def test_long_horizon_presenter_is_registered_as_static_pair(self):
        self._require_all_external_runtime()
        presenter = engine.SKILLS["long-horizon-presenter"]
        pipeline = engine.PIPELINES["long-horizon-presenter-harness"]
        self.assertTrue(presenter["ready"], presenter["unavailable_reason"])
        self.assertEqual(
            Path(presenter["path"]), engine.LONG_HORIZON_PRESENTER_SKILL_ROOT
        )
        self.assertEqual(
            Path(pipeline["path"]), engine.LONG_HORIZON_PRESENTER_HARNESS_ROOT
        )
        self.assertEqual(presenter["pipeline"], "long-horizon-presenter-harness")
        self.assertEqual(pipeline["skill_mode"], "long-horizon-presenter")
        self.assertIn("scripts/image_cutout.py", presenter["required_files"])
        self.assertIn("static_html", presenter["caps"])
        self.assertIn("long-horizon-presenter", engine.PUBLIC_SKILL_KEYS)

    def test_long_horizon_catalog_refreshes_without_service_restart(self):
        previous = engine.SKILLS["long-horizon"]
        previous_grouped = engine.SKILLS["long-horizon-grouped"]
        previous_grouped_inline = engine.SKILLS[
            "long-horizon-grouped-inline-image"
        ]
        previous_presenter = engine.SKILLS["long-horizon-presenter"]
        refreshed = engine.refresh_external_skills()
        self.assertIs(engine.SKILLS["long-horizon"], refreshed)
        self.assertEqual(refreshed["source_revision"], previous["source_revision"])
        self.assertEqual(refreshed["source_file_count"], len(refreshed["source_files"]))
        self.assertEqual(
            engine.SKILLS["long-horizon-grouped"]["source_revision"],
            previous_grouped["source_revision"],
        )
        self.assertEqual(
            engine.SKILLS["long-horizon-grouped-inline-image"]["source_revision"],
            previous_grouped_inline["source_revision"],
        )
        self.assertEqual(
            engine.SKILLS["long-horizon-presenter"]["source_revision"],
            previous_presenter["source_revision"],
        )

    def test_zh_and_en_share_the_clean_infer_harness(self):
        self._require_all_external_runtime()
        self.assertEqual(
            list(engine.PIPELINES),
            [
                "sense-present-standard-harness",
                "sense-present-dazzle-harness",
                "infer",
                "visual-craft-harness",
                "long-horizon-presenter-harness",
            ],
        )
        pipeline = engine.PIPELINES["infer"]
        self.assertEqual(Path(pipeline["path"]), engine.CLEAN_PIPELINE_ROOT)
        self.assertEqual(pipeline["entry"], "infer.py")
        self.assertEqual(pipeline["skill_mode"], "clean-bilingual")
        self.assertEqual(pipeline["pairing"], "clean-current")
        self.assertTrue((Path(pipeline["path"]) / pipeline["entry"]).is_file())
        for key in (
            "auto",
            "zh",
            "en",
            "long-horizon",
            "long-horizon-grouped",
            "long-horizon-grouped-inline-image",
        ):
            with self.subTest(skill=key):
                self.assertTrue(engine.SKILLS[key]["ready"])
                self.assertEqual(engine.pipeline_for_skill(key), "infer")
                self.assertIsNone(engine.validate_selection("opus-4.7-thinking", "infer", key))

        visual = engine.PIPELINES["visual-craft-harness"]
        self.assertEqual(Path(visual["path"]), engine.VISUAL_CRAFT_HARNESS_ROOT)
        self.assertEqual(visual["entry"], "distill_ppt.py")
        self.assertEqual(visual["skill_mode"], "ppt-skill-html")
        self.assertEqual(visual["pairing"], "visual-craft-paired")
        self.assertIn("revision", visual["caps"])
        self.assertTrue(visual["ready"], visual["unavailable_reason"])
        self.assertEqual(engine.pipeline_for_skill("visual-craft"), "visual-craft-harness")
        self.assertIsNone(
            engine.validate_selection(
                "sensenova-flash-lite-v39",
                "visual-craft-harness",
                "visual-craft",
            )
        )

        presenter = engine.PIPELINES["long-horizon-presenter-harness"]
        self.assertEqual(
            Path(presenter["path"]), engine.LONG_HORIZON_PRESENTER_HARNESS_ROOT
        )
        self.assertEqual(presenter["entry"], "distill_ppt.py")
        self.assertEqual(presenter["skill_mode"], "long-horizon-presenter")
        self.assertEqual(presenter["pairing"], "long-horizon-presenter-paired")
        self.assertIn("static_html", presenter["caps"])
        self.assertTrue(presenter["ready"], presenter["unavailable_reason"])
        self.assertEqual(
            engine.pipeline_for_skill("long-horizon-presenter"),
            "long-horizon-presenter-harness",
        )
        self.assertIsNone(
            engine.validate_selection(
                "sensenova-flash-lite-v39",
                "long-horizon-presenter-harness",
                "long-horizon-presenter",
            )
        )

    def test_visual_craft_system_locks_ppt_task_and_preserves_raw_query(self):
        self._require_all_external_runtime()
        harness = engine.VISUAL_CRAFT_HARNESS_ROOT / engine.VISUAL_CRAFT_HARNESS_ENTRY
        source = harness.read_text(encoding="utf-8")
        self.assertIn("静态 HTML 演示文稿生产任务", source)
        self.assertIn("禁止回复“连接正常 / 我可以帮你 / 想做什么”", source)
        self.assertIn('return seed["query"] if isinstance(seed, dict) else str(seed)', source)
        self.assertNotIn('config["_trace_namespace"]', source)

    def test_auto_leaves_language_selection_to_clean_infer(self):
        env = engine.selection_env("ckpt800", "infer", "auto")
        self.assertEqual(env["CLEAN_FORCE_SKILL_LANGUAGE"], "")
        job = engine.build_job(
            "auto-sample",
            {"query": "Create a two-slide test deck", "lang": "zh"},
            engine.deck_run_dir(1, 997),
            model_key="ckpt800",
            skill_key="auto",
        )
        self.assertEqual(job["skill_version"], "auto")
        self.assertNotIn("lang", job["seed"])
        self.assertEqual(job["skill_mount_dir"], str(engine.CLEAN_SKILLS_ROOT))
        auto_seed = studio_main._seed_for_query("Create a deck", "auto")
        zh_seed = studio_main._seed_for_query("做一份演示", "zh")
        self.assertNotIn("lang", auto_seed)
        self.assertEqual(zh_seed["lang"], "zh")

    def test_long_horizon_uses_external_skill_root_and_auto_deck_language(self):
        self._require_all_external_runtime()
        env = engine.selection_env("ckpt800", "infer", "long-horizon")
        self.assertEqual(env["CLEAN_SKILLS_DIR"], str(engine.LONG_HORIZON_SKILLS_ROOT))
        self.assertEqual(env["CLEAN_FORCE_SKILL_LANGUAGE"], "")

        job = engine.build_job(
            "long-horizon-sample",
            {"query": "Create a research-heavy product strategy deck", "lang": "zh"},
            engine.deck_run_dir(1, 996),
            model_key="ckpt800",
            skill_key="long-horizon",
        )
        self.assertEqual(job["skill_version"], "long-horizon")
        self.assertEqual(job["skill"]["name"], "long-horizon-html-ppt")
        self.assertEqual(job["skill"]["source_revision"], engine.SKILLS["long-horizon"]["source_revision"])
        self.assertEqual(job["skill_mount_dir"], str(engine.LONG_HORIZON_SKILLS_ROOT))
        self.assertNotIn("lang", job["seed"])
        self.assertNotIn(
            "lang",
            studio_main._seed_for_query("Create a research-heavy deck", "long-horizon"),
        )

    def test_legacy_skill_keys_are_hidden_compatibility_aliases(self):
        self.assertNotIn("current", engine.SKILLS)
        self.assertNotIn("v1", engine.SKILLS)
        self.assertEqual(engine.canon_skill("current"), "zh")
        self.assertEqual(engine.canon_skill("v1"), "zh")
        env = engine.selection_env("ckpt800", "v1", "v1")
        self.assertEqual(env["CLEAN_FORCE_SKILL_LANGUAGE"], "zh")
        self.assertEqual(env["CLEAN_SKILLS_DIR"], str(engine.CLEAN_SKILLS_ROOT))
        job = engine.build_job(
            "legacy-sample", {"query": "做一页测试 PPT"}, engine.deck_run_dir(1, 998),
            model_key="ckpt800", pipeline_key="current", skill_key="v1",
        )
        self.assertEqual(job["pipeline_version"], "infer")
        self.assertEqual(job["skill_version"], "zh")
        self.assertEqual(job["pipeline"]["entry"], "infer.py")

    def test_all_mapped_harnesses_dry_run_via_serve_one(self):
        self._require_all_external_runtime()
        with tempfile.TemporaryDirectory(prefix="studio-harness-dry-") as tmp:
            tmp_path = Path(tmp)
            for version in engine.SKILLS:
                with self.subTest(skill=version):
                    suffix = 0
                    while True:
                        sample_id = f"dry-{version}-{suffix}"
                        if int(hashlib.sha1(sample_id.encode()).hexdigest(), 16) % 10 >= 2:
                            break
                        suffix += 1
                    run_dir = tmp_path / f"run-{version}"
                    job = engine.build_job(
                        sample_id, {"query": "dry run", "slide_count": 2}, run_dir,
                        dry=True, model_key="ckpt800", skill_key=version,
                    )
                    job_path = tmp_path / f"{version}.json"
                    job_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
                    env = engine.render_env(os.environ)
                    env.update(engine.selection_env(
                        "ckpt800", "infer", version
                    ))
                    proc = subprocess.run(
                        engine.runner_cmd(job_path), env=env, text=True,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
                    )
                    self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
                    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
                    self.assertEqual(result["status"], "completed", proc.stderr[-2000:])
                    self.assertEqual(result["pipeline_version"], engine.pipeline_for_skill(version))
                    self.assertEqual(result["skill_version"], version)

    def test_runner_cmd_starts_with_existing_executable(self):
        cmd = engine.runner_cmd(engine.JOBS_DIR / "dummy.json")
        self.assertTrue(cmd, "runner command must not be empty")
        self.assertTrue(
            Path(cmd[0]).exists(),
            f"runner executable does not exist: {cmd[0]!r}; command={cmd}",
        )
        self.assertIn(str(engine.SERVE_ONE), cmd)
        local_engine_python = engine.DISTILL_DIR / ".venv" / "bin" / "python"
        if local_engine_python.is_file():
            self.assertEqual(cmd[0], str(local_engine_python))

    def test_render_env_adds_local_chromium_deps(self):
        with tempfile.TemporaryDirectory(prefix="studio-playwright-") as tmp:
            cache = Path(tmp) / "ms-playwright"
            revisions = engine._playwright_revisions()
            revision = revisions.get("chromium-headless-shell", "test")
            browser = (
                cache
                / f"chromium_headless_shell-{revision}"
                / "chrome-headless-shell-linux64"
                / "chrome-headless-shell"
            )
            browser.parent.mkdir(parents=True)
            browser.write_bytes(b"browser")
            browser.chmod(0o755)
            env = engine.render_env({
                "LD_LIBRARY_PATH": "/existing/lib",
                "PLAYWRIGHT_BROWSERS_PATH": str(cache),
            })
            self.assertIn("PPTAGENT_CHROMIUM_DEPS", env)
            deps = Path(env["PPTAGENT_CHROMIUM_DEPS"])
            self.assertEqual(deps.name, "chromium-deps")
            self.assertTrue(env["LD_LIBRARY_PATH"].endswith("/existing/lib"))
            self.assertEqual(env["PLAYWRIGHT_BROWSERS_PATH"], str(cache.resolve()))
            self.assertEqual(env["PPT_SKILL_BROWSER_EXE"], str(browser))
            self.assertEqual(
                env["PPTAGENT_ENGINE_SITE_PACKAGES"],
                str(engine.DATA_DIR / "engine-runtime" / "site-packages"),
            )
            self.assertTrue(env["PYTHONPATH"].startswith(str(engine.ENGINE_SITE_PACKAGES)))

    def test_default_model_is_sensenova_flash_lite_v39(self):
        self.assertEqual(engine.DEFAULT_MODEL, "sensenova-flash-lite-v39")
        model = engine.MODELS[engine.DEFAULT_MODEL]
        self.assertEqual(model["label"], "SenseNova Flash Lite v39 (1)")
        self.assertEqual(model["engine_model"], "nova-27b-v39-step4k-dpov2")
        self.assertEqual(
            model["base_url"], os.environ.get("SENSENOVA_FLASH_LITE_V39_BASE_URL", "")
        )

    def test_model_option_label_uses_display_alias(self):
        expected = {
            "opus-4.7-thinking": "Opus 4.7 Thinking",
            "ckpt800": "ckpt800",
            "pptagent-qwen35-27b-ckpt1764": "PPTAgent Qwen3.5 27B (ckpt1764)",
            "sensenova-flash-lite-v39": "SenseNova Flash Lite v39 (1)",
            "sensenova-flash-lite-v39-2": "SenseNova Flash Lite v39 (2)",
            "sensenova-flash-lite-v39-3": "SenseNova Flash Lite v39 (3)",
            "sensenova-flash-lite-v39-4": "SenseNova Flash Lite v39 (4)",
            "gpt-5.6-sol": "GPT-5.6 SOL (tokenhub)",
            "gpt-5.6-luna": "GPT-5.6 LUNA (tokenhub)",
            "gpt-5.6-terra": "GPT-5.6 TERRA (tokenhub)",
            "kimi-k3": "Kimi K3 (DashScope)",
        }
        for key, label in expected.items():
            with self.subTest(model=key):
                self.assertEqual(engine.model_option_label(engine.MODELS[key]), label)

    def test_frontend_model_options_include_active_models(self):
        visible = dict(engine.user_selectable_models())
        self.assertEqual(set(visible), {
            "sensenova-flash-lite-v39",
            "sensenova-flash-lite-v39-2",
            "sensenova-flash-lite-v39-4",
            "pptagent-qwen35-27b-ckpt1764",
        })
        self.assertNotIn("sensenova-flash-lite-v39-3", visible)
        self.assertIn("sensenova-flash-lite-v39-3", engine.MODELS)
        self.assertNotIn("qwen3_9b_agentic_v1", engine.MODELS)
        self.assertNotIn("official_Qwen3_6_27B_agentic_v3_6", engine.MODELS)
        self.assertIn("opus-4.7-thinking", engine.MODELS)
        self.assertIn("gpt-5.6-sol", engine.MODELS)
        self.assertIn("sensenova-flash-lite-v39", engine.MODELS)
        self.assertIn("gpt-5.6-luna", engine.MODELS)
        self.assertIn("gpt-5.6-terra", engine.MODELS)
        self.assertIn("kimi-k3", engine.MODELS)
        self.assertIn("gpt-5.5", engine.MODELS)

    def test_ckpt800_model_is_registered(self):
        model = engine.MODELS["ckpt800"]
        self.assertEqual(model["label"], "ckpt800")
        self.assertEqual(model["backend"], "openai")
        self.assertEqual(model["engine_model"], "ckpt800")
        self.assertEqual(model["base_url"], os.environ.get("SENSENOVA_CKPT800_BASE_URL", ""))

    def test_pptagent_qwen35_ckpt1764_model_is_registered(self):
        model = engine.MODELS["pptagent-qwen35-27b-ckpt1764"]
        self.assertEqual(model["label"], "PPTAgent Qwen3.5 27B (ckpt1764)")
        self.assertEqual(model["backend"], "openai")
        self.assertEqual(
            model["engine_model"],
            "pptagent-qwen35-27b-6batch-qcv2-128k-mv2weighted-ckpt1764",
        )
        self.assertEqual(model["base_url"], os.environ.get("SENSENOVA_QWEN35_BASE_URL", ""))
        self.assertFalse(model.get("ui_hidden", False))

    def test_sensenova_flash_lite_v39_is_registered(self):
        model = engine.MODELS["sensenova-flash-lite-v39"]
        self.assertEqual(model["label"], "SenseNova Flash Lite v39 (1)")
        self.assertEqual(model["backend"], "openai")
        self.assertEqual(model["engine_model"], "nova-27b-v39-step4k-dpov2")
        self.assertEqual(
            model["base_url"], os.environ.get("SENSENOVA_FLASH_LITE_V39_BASE_URL", "")
        )
        self.assertFalse(model.get("ui_hidden", False))

        for key, label, base_url in (
            ("sensenova-flash-lite-v39-2", "SenseNova Flash Lite v39 (2)", os.environ.get("SENSENOVA_FLASH_LITE_V39_2_BASE_URL", "")),
            ("sensenova-flash-lite-v39-3", "SenseNova Flash Lite v39 (3)", os.environ.get("SENSENOVA_FLASH_LITE_V39_3_BASE_URL", "")),
            ("sensenova-flash-lite-v39-4", "SenseNova Flash Lite v39 (4)", os.environ.get("SENSENOVA_FLASH_LITE_V39_4_BASE_URL", "")),
        ):
            with self.subTest(model=key):
                candidate = engine.MODELS[key]
                self.assertEqual(candidate["label"], label)
                self.assertEqual(candidate["backend"], "openai")
                expected_model = (
                    "nova-27b-v39-step4k-dpov2"
                    if key == "sensenova-flash-lite-v39-4"
                    else "SenseNova-Flash-Lite-20260727-v39-fp-step4k"
                )
                self.assertEqual(candidate["engine_model"], expected_model)
                self.assertEqual(candidate["base_url"], base_url)
                self.assertFalse(candidate.get("ui_hidden", False))

    def test_legacy_opus_keys_canon_to_47_thinking(self):
        self.assertEqual(engine.canon("opus-4.8"), "opus-4.7-thinking")
        self.assertEqual(engine.canon("opus-4.8-thinking"), "opus-4.7-thinking")
        self.assertEqual(engine.canon("opus-4.7"), "opus-4.7-thinking")

    def test_build_job_passes_47_thinking_to_engine(self):
        job = engine.build_job(
            "sample",
            {"query": "做一页测试 PPT"},
            engine.deck_run_dir(1, 999),
            model_key="opus-4.8",
            pipeline_key="infer",
            skill_key="zh",
        )
        self.assertEqual(job["model"], "claude-opus-4-7-thinking")

    def test_default_generation_stack_uses_long_horizon_presenter_pair(self):
        stack = engine.default_generation_stack("opus-4.7-thinking")
        self.assertEqual(stack["pipeline"], "long-horizon-presenter-harness")
        self.assertEqual(stack["skill"], "long-horizon-presenter")
        self.assertEqual(
            stack["pipeline_label"],
            engine.PIPELINES["long-horizon-presenter-harness"]["label"],
        )
        self.assertEqual(
            stack["skill_label"], engine.SKILLS["long-horizon-presenter"]["label"]
        )
        self.assertEqual(
            engine.PUBLIC_SKILL_KEYS,
            ("sense-present-standard", "visual-craft", "long-horizon-presenter"),
        )

    def test_default_generation_stack_keeps_skill_choice_for_openai_models(self):
        stack = engine.default_generation_stack("ckpt800")
        self.assertEqual(stack["pipeline"], "long-horizon-presenter-harness")
        self.assertEqual(stack["skill"], "long-horizon-presenter")
        error = engine.validate_selection(
            "ckpt800", "long-horizon-presenter-harness", "long-horizon-presenter"
        )
        if engine.resolve_base_url(engine.MODELS["ckpt800"]) and engine.SKILLS["long-horizon-presenter"]["ready"]:
            self.assertIsNone(error)
        else:
            self.assertTrue(error)

    def test_empty_pipeline_uses_default_presenter_harness_for_all_models(self):
        for model in (
            "opus-4.7-thinking", "ckpt800", "gpt-5.6-sol",
            "gpt-5.6-luna", "gpt-5.6-terra", "kimi-k3",
        ):
            self.assertEqual(
                engine.canon_pipeline("", model), "long-horizon-presenter-harness"
            )

    def test_default_build_job_uses_long_horizon_presenter_pair(self):
        self._require_all_external_runtime()
        job = engine.build_job(
            "sample",
            {"query": "做一页测试 PPT"},
            engine.deck_run_dir(1, 1000),
            model_key="opus-4.7-thinking",
            pipeline_key="",
            skill_key="",
        )
        self.assertEqual(job["pipeline_version"], "long-horizon-presenter-harness")
        self.assertEqual(job["skill_version"], "long-horizon-presenter")
        self.assertEqual(
            job["pipeline"]["path"],
            engine.PIPELINES["long-horizon-presenter-harness"]["path"],
        )
        self.assertNotIn("lang", job["seed"])
        self.assertEqual(
            job["skill_mount_dir"],
            str(engine.LONG_HORIZON_PRESENTER_SKILL_ROOT.parent),
        )

    def test_build_job_uses_long_horizon_presenter_pair(self):
        self._require_all_external_runtime()
        job = engine.build_job(
            "presenter-sample",
            {"query": "制作一套高设计感静态 PPT"},
            engine.deck_run_dir(1, 1005),
            model_key="sensenova-flash-lite-v39",
            skill_key="long-horizon-presenter",
        )
        self.assertEqual(
            job["pipeline_version"], "long-horizon-presenter-harness"
        )
        self.assertEqual(job["skill_version"], "long-horizon-presenter")
        self.assertEqual(
            job["skill_mount_dir"],
            str(engine.LONG_HORIZON_PRESENTER_SKILL_ROOT.parent),
        )
        self.assertEqual(
            Path(job["pipeline"]["path"]),
            engine.LONG_HORIZON_PRESENTER_HARNESS_ROOT,
        )

    def test_build_job_selects_english_skill_with_same_infer_harness(self):
        job = engine.build_job(
            "sample",
            {"query": "Create a two-slide test deck", "slide_count": 2},
            engine.deck_run_dir(1, 1001),
            model_key="opus-4.7-thinking",
            pipeline_key="current",  # stale client value is ignored
            skill_key="en",
        )
        self.assertEqual(job["pipeline_version"], "infer")
        self.assertEqual(job["skill_version"], "en")
        self.assertEqual(job["pipeline"]["path"], engine.SKILLS["en"]["harness_path"])
        self.assertEqual(job["seed"]["lang"], "en")
        self.assertEqual(job["seed"]["pages_hint"], 2)

    def test_external_openai_models_are_registered_and_visible(self):
        expected = {
            "gpt-5.6-sol": (
                "gpt-5.6-sol",
                "https://tokenhub.sensetime.com/v1",
                "GPT56_API_KEY",
            ),
            "gpt-5.6-luna": (
                "gpt-5.6-luna",
                "https://tokenhub.sensetime.com/v1",
                "GPT56_API_KEY",
            ),
            "gpt-5.6-terra": (
                "gpt-5.6-terra",
                "https://tokenhub.sensetime.com/v1",
                "GPT56_API_KEY",
            ),
            "kimi-k3": (
                "kimi/kimi-k3",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "KIMI_K3_API_KEY",
            ),
        }
        for key, (engine_model, base_url, api_key_env) in expected.items():
            with self.subTest(model=key):
                model = engine.MODELS[key]
                self.assertEqual(model["backend"], "openai")
                self.assertEqual(model["engine_model"], engine_model)
                self.assertEqual(model["base_url"], base_url)
                self.assertEqual(model["api_key_env"], api_key_env)
                self.assertFalse(model.get("ui_hidden", False))
                stack = engine.default_generation_stack(key)
                self.assertEqual(stack["pipeline"], "long-horizon-presenter-harness")
                self.assertEqual(stack["skill"], "long-horizon-presenter")

    def test_runnable_catalog_and_historical_pairs_accept_all_models(self):
        for model_key in engine.MODELS:
            for skill_key in engine.SKILLS:
                with self.subTest(model=model_key, skill=skill_key):
                    result = engine.validate_selection(
                        model_key,
                        engine.pipeline_for_skill(skill_key),
                        skill_key,
                    )
                    model = engine.MODELS[model_key]
                    model_ready = model["backend"] != "openai" or bool(engine.resolve_base_url(model))
                    skill = engine.SKILLS[skill_key]
                    pipeline = engine.PIPELINES.get(engine.pipeline_for_skill(skill_key) or "", {})
                    expected_ready = (
                        model_ready
                        and skill.get("ready", False)
                        and pipeline.get("ready", False)
                        and model["backend"] in set(pipeline.get("supports", []))
                    )
                    self.assertEqual(result is None, expected_ready)

    def test_runner_has_clean_infer_adapter(self):
        serve_one = Path(__file__).resolve().parents[2] / "distillation" / "serve_one.py"
        src = serve_one.read_text(encoding="utf-8")
        self.assertIn("def _run_clean_infer(job):", src)
        self.assertIn('entry = pipe.get("entry") or "infer.py"', src)
        self.assertIn('from core import model_call, run_batch', src)
        self.assertIn('run_batch.select_skill(seed)', src)
        self.assertIn('infer._start_render_broker()', src)
        self.assertIn('rec.get("skill_language")', src)

    def test_progress_seed_payload_exposes_full_query(self):
        payload = studio_main._progress_seed_payload({
            "query": "生成主题\n\n【附件解析结果】\nnotes.txt: 参考内容",
            "slide_count": 8,
            "theme": "技术分享",
        })
        self.assertEqual(payload["query"], "生成主题\n\n【附件解析结果】\nnotes.txt: 参考内容")
        self.assertEqual(payload["user_query"], "生成主题\n\n【附件解析结果】\nnotes.txt: 参考内容")
        self.assertEqual(payload["attachments"], [])
        self.assertEqual(payload["slide_count"], 8)
        self.assertEqual(payload["theme"], "技术分享")

    def test_progress_seed_payload_exposes_safe_brief_metadata(self):
        payload = studio_main._progress_seed_payload({
            "query": "生成主题\n\n【附件资料】\n1. 文件名: brief.pdf",
            "attachments": [{
                "name": "brief.pdf", "type": "pdf", "size": 2048,
                "path": "/private/upload/brief.pdf", "text": "private body",
            }],
        })
        self.assertEqual(payload["user_query"], "生成主题")
        self.assertEqual(payload["attachments"], [{
            "index": 0, "name": "brief.pdf", "stored_name": "", "type": "pdf",
            "size": 2048, "preview_rel": "",
        }])
        self.assertNotIn("path", payload["attachments"][0])
        self.assertNotIn("text", payload["attachments"][0])

    def test_attachment_tray_is_inside_brief_above_textarea(self):
        template = Path(__file__).resolve().parents[1] / "templates" / "app.html"
        html = template.read_text(encoding="utf-8")
        brief_start = html.index('<div class="brief-card">')
        tray = html.index('id="attach-list"', brief_start)
        textarea = html.index('id="q"', brief_start)
        actions = html.index('<div class="composer-actions">', brief_start)
        self.assertLess(tray, textarea)
        self.assertLess(textarea, actions)
        self.assertEqual(html.count('id="attach-list"'), 1)
        self.assertIn('id="attachment-viewer"', html)
        script = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function deckAttachmentPreviewUrls", script)
        self.assertIn('data-attachment-fallback-url=', script)
        self.assertIn('img[data-attachment-image-fallback]', script)

    def test_deck_attachment_preview_stays_inside_upload_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            upload_root = Path(tmp) / "uploads" / "7"
            upload_root.mkdir(parents=True)
            source = upload_root / "brief.txt"
            source.write_text("brief", encoding="utf-8")
            row = {"seed_json": json.dumps({"attachments": [{
                "name": "brief.txt", "path": str(source), "type": "txt", "size": 5,
            }]})}
            with patch.object(studio_main, "_own_deck", return_value=row), \
                 patch.object(engine, "deck_uploads_dir", return_value=upload_root):
                response = studio_main.deck_attachment(7, 0, user={"id": 4}, con=object())
            self.assertEqual(Path(response.path), source)
            self.assertEqual(response.media_type, "text/plain")
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_attachment_preview_supports_pdf_text_and_office_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            upload_root = Path(tmp) / "uploads" / "7"
            upload_root.mkdir(parents=True)
            pdf = upload_root / "brief.pdf"
            pdf.write_bytes(b"%PDF-1.4\n% test fixture\n")
            page = upload_root / "brief_pptx_slide_001.png"
            page.write_bytes(b"\x89PNG\r\n\x1a\n")
            row = {"seed_json": json.dumps({"attachments": [
                {"name": "brief.pdf", "path": str(pdf), "type": "pdf", "size": pdf.stat().st_size},
                {
                    "name": "plan.pptx", "stored_name": "plan.pptx", "type": "pptx",
                    "size": 1024, "text": "[Slide 1]\n项目计划", "truncated": False,
                    "notes": ["已生成页面预览。"],
                    "images": [{"source_path": str(page), "page": 1, "kind": "pptx_slide"}],
                },
            ]})}
            with patch.object(studio_main, "_own_deck", return_value=row), \
                 patch.object(engine, "deck_uploads_dir", return_value=upload_root):
                pdf_response = studio_main.deck_attachment(7, 0, user={"id": 4}, con=object())
                preview = studio_main.deck_attachment_preview(7, 1, user={"id": 4}, con=object())
                asset_response = studio_main.deck_attachment_preview_asset(7, 1, 0, user={"id": 4}, con=object())
            self.assertEqual(pdf_response.media_type, "application/pdf")
            self.assertNotIn("content-security-policy", pdf_response.headers)
            self.assertEqual(preview["text"], "[Slide 1]\n项目计划")
            self.assertEqual(preview["assets"], [{
                "url": "/api/decks/7/attachments/1/assets/0",
                "label": "第 1 页",
                "kind": "pptx_slide",
            }])
            self.assertEqual(Path(asset_response.path), page)
            self.assertEqual(asset_response.media_type, "image/png")

    def test_attachment_viewer_routes_each_supported_type_to_its_own_renderer(self):
        script = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        preview_urls = script[script.index("function deckAttachmentPreviewUrls"):script.index("function attachmentObjectUrl")]
        self.assertNotIn("attachments/raw/${safeName}", preview_urls)
        self.assertIn('kind === "pdf"', script)
        self.assertIn('kind === "text"', script)
        self.assertIn('kind === "audio" || kind === "video"', script)
        self.assertIn('kind === "office" && previewUrl', script)
        self.assertNotIn('frame.setAttribute("sandbox", "")', script)
        self.assertIn('data-attachment-preview-url=', script)

    def test_pipeline_and_native_skill_selectors_use_custom_controls(self):
        template = Path(__file__).resolve().parents[1] / "templates" / "app.html"
        html = template.read_text(encoding="utf-8")
        self.assertIn('id="pipeline-sel" hidden', html)
        self.assertIn('id="pipeline-display"', html)
        self.assertIn('id="model-trigger"', html)
        self.assertIn('id="model-menu" hidden', html)
        self.assertIn('id="model-sel" hidden', html)
        self.assertIn('id="model-add-trigger" data-user-action="model"', html)
        self.assertNotIn('class="model-add-trigger"', html)
        self.assertIn('id="skill-trigger"', html)
        self.assertIn('id="skill-menu" hidden', html)
        self.assertIn('<select id="skill-sel" hidden>', html)
        self.assertNotIn('id="dynamic-skill-display"', html)

    def test_workspace_uses_sidebar_controls_and_personal_settings(self):
        template = Path(__file__).resolve().parents[1] / "templates" / "app.html"
        html = template.read_text(encoding="utf-8")
        script = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn('class="workspace-topbar"', html)
        self.assertIn('class="sidebar-brand-row"', html)
        self.assertIn('class="sidebar-toggle" id="sidebar-toggle"', html)
        self.assertIn('class="nova-art-logo nova-doodle" id="nova-doodle"', html)
        self.assertIn('id="nova-doodle-canvas"', html)
        self.assertNotIn('class="nova-signature"', html)
        self.assertNotIn('nova-doodle-fallback', html)
        self.assertIn('data-i18n="hero_title">今天，你想展示什么？</h1>', html)
        self.assertNotIn('data-user-action="settings"', html)
        self.assertNotIn('id="settings-modal"', html)
        self.assertIn('class="user-menu-preferences"', html)
        self.assertIn('data-theme-choice="light"', html)
        self.assertIn('data-theme-choice="dark"', html)
        self.assertIn('id="settings-language"', html)
        self.assertIn('option value="en"', html)
        self.assertIn("function setLanguage(language", script)
        self.assertIn("dock.dataset.mode = creationMode", script)
        self.assertNotIn("function setSettingsDialog", script)
        self.assertNotIn('$("#theme-toggle").addEventListener', script)

    def test_home_logo_uses_theme_aware_paused_canvas_doodle(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "static" / "app.js").read_text(encoding="utf-8")
        css = (root / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn("function initNovaDoodle()", script)
        self.assertIn('const brandLetters = "SENSENOVA"', script)
        self.assertIn('const letters = "PRESENT"', script)
        self.assertIn("const centers = [53, 102, 151, 200, 249, 298, 347]", script)
        self.assertIn("function abstractStrength(tile, row, col, letterOn)", script)
        self.assertIn("const accentMap = [", script)
        self.assertIn("function abstractAmount(elapsed)", script)
        self.assertIn("function brandState(elapsed)", script)
        self.assertIn("function brandAmount(elapsed)", script)
        self.assertIn("function brandPointColor(colors, position, alpha = 1)", script)
        self.assertIn("colorMix(colors.brandPurple, colors.brandGreen, blend, alpha)", script)
        self.assertIn("brandPurple: [179, 145, 255], brandGreen: [29, 226, 171]", script)
        self.assertNotIn("colorMix(colors.yellow, colors.purple", script)
        self.assertIn("const isLetterDot = dot.letterStrength > .8", script)
        self.assertIn("function dotSwitchState(dot, transition, isBrand)", script)
        self.assertIn("function scatterVector(dot, state, direction)", script)
        self.assertIn("initNovaDoodle();", script)
        self.assertNotIn("requestIdleCallback(initNovaDoodle", script)
        self.assertIn("new IntersectionObserver", script)
        self.assertIn("new ResizeObserver", script)
        self.assertIn('prefers-reduced-motion: reduce', script)
        self.assertIn('attributeFilter: ["data-theme"]', script)
        self.assertIn("devicePixelRatio", script)
        self.assertIn("canvas.width = width", script)
        self.assertIn(".nova-doodle canvas", css)
        self.assertIn(".nova-doodle.doodle-ready canvas", css)
        self.assertIn("width: min(440px, 90vw); height: 169px", css)
        self.assertIn("width: min(260px, 80vw); height: 100px", css)
        self.assertNotIn("dock-pointer-glow", css)
        self.assertNotIn("dockGlowResetTimer", script)
        self.assertNotIn(".brief-card::after", css)
        self.assertNotIn("nova-input-aurora", css)

    def test_collapsed_sidebar_keeps_expand_control_above_workspace(self):
        css = (Path(__file__).resolve().parents[1] / "static" / "app.css").read_text(encoding="utf-8")
        html = (Path(__file__).resolve().parents[1] / "templates" / "app.html").read_text(encoding="utf-8")
        script = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn(".layout.sidebar-collapsed .sidebar {", css)
        self.assertIn('class="sidebar-expand" id="sidebar-expand"', html)
        self.assertIn('class="sidebar-glyph-chevron"', html)
        self.assertIn("url(#sidebar-glyph-accent)", css)
        self.assertIn("width: 0; overflow: hidden", css)
        self.assertIn("pointer-events: none", css)
        self.assertIn(".layout.sidebar-collapsed .sidebar-expand {", css)
        self.assertIn("z-index: 221", css)
        self.assertIn("pointer-events: auto", css)
        self.assertNotIn('.sidebar > :not(.sidebar-brand-row) { display: none; }', css)
        self.assertIn("visibility 0s .32s", css)
        self.assertIn("box-sizing: border-box", css)
        self.assertIn("border-right-width: 0", css)
        self.assertNotIn("sidebar-transitioning", css)
        self.assertNotIn("__studioSidebarTransitionId", script)
        self.assertIn('$("#sidebar-expand").addEventListener', script)
        self.assertIn('sidebar-transition-suppressed', script)

    def test_workspace_has_structural_responsive_breakpoints(self):
        root = Path(__file__).resolve().parents[1]
        css = (root / "static" / "app.css").read_text(encoding="utf-8")
        html = (root / "templates" / "app.html").read_text(encoding="utf-8")
        script = (root / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('class="sidebar-scrim" id="sidebar-scrim"', html)
        self.assertIn('@media (max-width: 1180px)', css)
        self.assertIn('grid-template: minmax(0,1fr) 108px / minmax(0,1fr)', css)
        self.assertIn('flex-direction: row', css)
        self.assertIn('.editor.preview-layout .ed-body', css)
        self.assertIn('compactWorkspaceMedia = matchMedia("(max-width: 1180px)")', script)
        self.assertIn('$("#sidebar-scrim")?.addEventListener', script)
        self.assertIn('class="proc-toggle btn-ghost" id="proc-toggle"', html)
        self.assertIn('.editor.proc-open .proc-rail', css)
        self.assertIn('function setProcRailOpen(open)', script)

    def test_desktop_detail_rail_can_collapse_and_restore(self):
        root = Path(__file__).resolve().parents[1]
        css = (root / "static" / "app.css").read_text(encoding="utf-8")
        html = (root / "templates" / "app.html").read_text(encoding="utf-8")
        script = (root / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="proc-rail-collapse"', html)
        self.assertIn('id="proc-rail-expand"', html)
        self.assertIn('@media (min-width: 1400px)', css)
        self.assertIn('.editor.proc-rail-collapsed .proc-rail {', css)
        self.assertIn('.editor.proc-rail-collapsed .proc-rail-expand {', css)
        self.assertIn('function setProcRailCollapsed(collapsed', script)
        self.assertIn('studio_proc_rail_collapsed', script)
        self.assertIn('procRailDrawerMedia = matchMedia("(max-width: 1799px)")', script)

    def test_editor_exposes_share_links_and_owned_output_locations(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "static" / "app.js").read_text(encoding="utf-8")
        main_source = (root / "app" / "main.py").read_text(encoding="utf-8")
        dynamic_source = (root / "app" / "dynamic.py").read_text(encoding="utf-8")
        self.assertIn('id="share-btn"', script)
        self.assertIn('id="copy-output-btn"', script)
        self.assertIn('async function shareCurrentDeck()', script)
        self.assertIn('async function copyOutputLocation()', script)
        self.assertIn('function openDeckFromLocationHash()', script)
        self.assertIn('loadDecks().then(openDeckFromLocationHash)', script)
        self.assertIn('@app.get("/api/decks/{deck_id}/output-location")', main_source)
        self.assertIn('@router.get("/api/dynamic/output-location")', dynamic_source)

    def test_history_uses_icon_only_mode_markers(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "static" / "app.js").read_text(encoding="utf-8")
        css = (root / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn('class="kind-mark ${visualKind} s-${d.status}"', script)
        self.assertIn('role="img" aria-label="${escapeHtml(markerLabel)}"', script)
        self.assertNotIn('class="dot s-${d.status}"', script)
        self.assertNotIn('d.kind === "dynamic" ? "动" : "静"', script)
        self.assertIn('.kind-mark.static::before', css)
        self.assertIn('.kind-mark.dynamic::before', css)
        self.assertIn('.kind-mark.s-completed', css)
        self.assertIn('.kind-mark.s-running', css)
        self.assertIn('.kind-mark.s-failed', css)
        self.assertIn('@keyframes kind-status-breathe', css)
        self.assertIn('@keyframes kind-status-ring', css)
        self.assertIn('@keyframes kind-status-core', css)
        self.assertIn('animation: kind-status-breathe 3s', css)
        self.assertIn('scale(.56)', css)
        self.assertIn('--kind-core-offset', css)

    def test_new_chat_uses_branded_plus_icon(self):
        root = Path(__file__).resolve().parents[1]
        template = (root / "templates" / "app.html").read_text(encoding="utf-8")
        css = (root / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn('class="new-chat-plus"', template)
        self.assertIn('id="new-chat-plus-gradient"', template)
        self.assertIn('.new-btn:hover .new-chat-plus', css)
        self.assertNotIn('.new-btn-icon::after', css)

    def test_first_visit_defaults_to_dark_theme(self):
        root = Path(__file__).resolve().parents[1]
        base = (root / "templates" / "base.html").read_text(encoding="utf-8")
        script = (root / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('const savedTheme = localStorage.getItem("studio_theme")', base)
        self.assertIn(': "dark";', base)
        self.assertNotIn('prefers-color-scheme: dark', base)
        self.assertIn('setTheme(document.documentElement.dataset.theme || "dark"', script)

    def test_frontend_keeps_skill_select_user_controlled(self):
        script = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('localStorage.getItem("studio_skill")', script)
        self.assertIn('localStorage.getItem("studio_skill_default")', script)
        self.assertIn('localStorage.setItem("studio_skill_default", currentDefaultSkill)', script)
        self.assertIn('localStorage.setItem("studio_skill", $("#skill-sel").value)', script)
        self.assertIn('$("#skill-sel").addEventListener("change", () => normalizeVersionSelection())', script)
        self.assertIn("function renderSkillMenu()", script)
        self.assertIn('$("#skill-trigger").addEventListener("click"', script)
        self.assertIn('$("#skill-sel").value = item.dataset.skill', script)
        self.assertNotIn("dataset.defaultSkill", script)

    def test_dynamic_deck_navigation_tracks_the_filmstrip(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "static" / "app.js").read_text(encoding="utf-8")
        css = (root / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn("function dynamicDeckActiveSlide(frame)", script)
        self.assertIn("function bindDynamicDeckNavigation()", script)
        self.assertIn("function playDynamicSlide(frame, n)", script)
        self.assertNotIn("function keepDynamicPng(n)", script)
        self.assertIn("Do not pass through a rendered PNG between live states", script)
        self.assertIn("function revealDynamicDeckSlide(frame, n)", script)
        self.assertIn('frame.dataset.deckReady = "revealing"', script)
        self.assertIn('frame.dataset.revealToken === token', script)
        self.assertIn('dynamicDeckActiveSlide(frame) !== target', script)
        self.assertIn('(frame.dataset.pendingSlide || frame.dataset.deckReady !== "1")', script)
        self.assertIn('deckWindow.__deckGo(Math.max(0, Number(n) - 1))', script)
        self.assertIn('frame.dataset.deckReady === "1" && playDynamicSlide(frame, n)', script)
        self.assertIn('frame.dataset.deckReady = "fallback"', script)
        self.assertIn("function stabilizeDynamicDeckPlayer(frame)", script)
        self.assertIn("studio-dynamic-player-viewport-guard", script)
        self.assertIn('frame.dataset.deckKind === "dynamic"', script)
        self.assertIn('deckWindow.addEventListener("slidechange"', script)
        self.assertIn('$("#canvas-deck").addEventListener("load", bindDynamicDeckNavigation)', script)
        self.assertIn("markSel();", script)
        self.assertIn('.editor[data-kind="dynamic"].workspace-ppt .canvas-wrap', css)
        self.assertIn("padding-inline: 0", css)
        self.assertIn('.editor[data-kind="dynamic"].workspace-ppt #canvas-deck', css)
        self.assertIn("aspect-ratio: 16 / 9", css)

    def test_static_and_dynamic_html_stages_are_edge_to_edge(self):
        css = (Path(__file__).resolve().parents[1] / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn(".editor.workspace-ppt .canvas-wrap", css)
        self.assertIn(".editor.workspace-ppt .canvas {", css)
        self.assertIn(".editor.workspace-ppt #canvas-deck", css)
        self.assertIn("padding-left: 0", css)
        self.assertIn("padding-right: 0", css)
        self.assertIn("width: 100%", css)
        self.assertIn("aspect-ratio: 16 / 9", css)

    def test_static_completed_deck_uses_canonical_html_player_with_transitions(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "templates" / "app.html").read_text(encoding="utf-8")
        script = (root / "static" / "app.js").read_text(encoding="utf-8")
        css = (root / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn('title="演示文稿 HTML 播放预览"', html)
        self.assertIn('scrolling="no"', html)
        self.assertIn("function showStaticDeck(n)", script)
        self.assertIn("function keepStaticPng(n)", script)
        self.assertIn("async function revealStaticDeckWhenFontsReady", script)
        self.assertIn('const dynamicHtml = editorPresentationKind() === "dynamic"', script)
        self.assertIn('(!dynamicHtml && failed)', script)
        self.assertIn('typeof deckWindow.__deckGo === "function"', script)
        self.assertIn('if (hasNativePlayer)', script)
        self.assertIn("frame.contentDocument?.fonts?.ready", script)
        self.assertIn('frame.dataset.fontsReady = "1"', script)
        self.assertIn(".canvas iframe.font-pending", css)
        self.assertIn(".canvas iframe.font-ready", css)
        self.assertIn('`/api/decks/${id}/files/present.html`', script)
        self.assertIn("player.go(n)", script)
        self.assertIn("player.provisional && Number(player.count) < 1", script)
        self.assertIn("empty shell replace the stable rendered PNG", script)
        self.assertIn("playStaticSlide(frame, n)", script)
        self.assertIn('frame.dataset.deckKind = "static"', script)
        self.assertIn('deckWindow.addEventListener("slidechange"', script)
        self.assertIn("function guardSlideDocument(slideFrame)", script)
        self.assertIn("function stabilizeStaticDeckPlayer(frame)", script)
        self.assertIn("function scheduleDeckViewportFit", script)
        self.assertIn('document.addEventListener("fullscreenchange"', script)
        self.assertIn("studio-slide-viewport-guard", script)
        self.assertIn("studio-static-player-viewport-guard", script)
        self.assertIn("flex-shrink: 0 !important", script)
        self.assertIn("scrollbar-width: none", css)
        self.assertIn("function syncPresentationFullscreenScale()", script)
        self.assertIn("presentation-fullscreen", script)
        self.assertIn("html.presentation-fullscreen { --ui-scale: 1 !important; }", css)
        self.assertIn('deckWindow.location.href === "about:blank"', script)
        self.assertIn("Legacy/incomplete output without a player", script)

    def test_dynamic_progress_uses_dynamic_labels_and_event_duration(self):
        template = Path(__file__).resolve().parents[1] / "templates" / "app.html"
        html = template.read_text(encoding="utf-8")
        script = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="pr-countlabel"', html)
        self.assertIn('"页面已完成"', script)
        self.assertIn("dynamicEndedAt - ed.dynamicStartedAt", script)
        self.assertIn('setRailLabels("dynamic")', script)
        self.assertIn("第 ${n} 页渲染完成", script)
        self.assertIn('id="pr-runtime"', html)
        self.assertIn("function pageAgentDisplayName", script)
        self.assertIn('return "动态创作 Agent"', script)
        self.assertIn("ed.dynamicError ||", script)

    def test_static_composer_continues_the_current_artifact_in_place(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "static" / "app.js").read_text(encoding="utf-8")
        main_source = (root / "app" / "main.py").read_text(encoding="utf-8")
        serve_source = (root.parent / "distillation" / "serve_one.py").read_text(encoding="utf-8")
        self.assertIn('waiting: "等待上一版"', script)
        self.assertIn('`/api/decks/${ed.id}/continue`', script)
        self.assertIn("每轮修改都会直接更新当前演示", script)
        self.assertNotIn("静态演示的连续编辑能力正在接入", script)
        self.assertIn('@app.post("/api/decks/{deck_id}/continue")', main_source)
        self.assertIn('"in_place": True', main_source)
        self.assertIn("run_batch.revision_worker", serve_source)
        self.assertIn('getattr(pipe, "revision_worker", None)', serve_source)

    def test_dynamic_renderer_keeps_one_python_runtime(self):
        root = Path(__file__).resolve().parents[2]
        renderer = (root / "dynamic" / "skills" / "dazzle-deck" / "scripts" / "render_deck.py").read_text(encoding="utf-8")
        tools = (root / "dynamic" / "agentic" / "tools.py").read_text(encoding="utf-8")
        self.assertNotIn("/usr/lib/python3.10", renderer)
        self.assertNotIn(".local/lib/python3.10", renderer)
        self.assertNotIn("_GREENLET_SO", renderer)
        self.assertNotIn("spec_from_file_location(\"greenlet", renderer)
        self.assertNotIn("sys.modules.pop", renderer)
        self.assertNotIn("ppt-html-prod/lib/python3.11/site-packages", renderer)
        self.assertNotIn('"--disable-gpu"', renderer)
        self.assertNotIn('"--disable-software-rasterizer"', renderer)
        self.assertNotIn("UseGLDrawing", renderer)
        self.assertIn('"--use-gl=egl"', renderer)
        self.assertIn("DYNAMIC_RENDER_PYTHON", tools)

    def test_frontend_labels_only_clean_bilingual_skills(self):
        script = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('infer: "Clean infer harness"', script)
        self.assertIn('auto: "Auto（自动选择）"', script)
        self.assertIn('zh: "中文 Skill"', script)
        self.assertIn('en: "English Skill"', script)
        self.assertIn('"long-horizon": "Long-horizon HTML PPT"', script)
        self.assertIn('"long-horizon-grouped": "Long-horizon HTML PPT Grouped"', script)
        self.assertIn(
            '"long-horizon-grouped-inline-image": "Long-horizon Grouped · Inline Image"',
            script,
        )
        self.assertIn('"visual-craft": "Visual Craft HTML PPT"', script)
        self.assertIn('"visual-craft-harness": "Visual Craft Harness"', script)
        self.assertIn('"sense-present-standard": "SenseNova Static HTML"', script)
        self.assertIn('"sense-present-dazzle": "SenseNova Dynamic HTML"', script)
        self.assertIn('"sense-present-standard-harness": "SenseNova Static HTML Harness"', script)
        self.assertIn('"sense-present-dazzle-harness": "SenseNova Dynamic HTML Harness"', script)
        self.assertNotIn('current: "Current harness', script)
        self.assertNotIn('v1: "v1', script)
        self.assertIn("const SKILL_LABEL =", script)
        self.assertNotIn("mdl + pipe + skill", script)

    def test_long_horizon_presenter_frontend_labels(self):
        script = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn('"long-horizon-presenter": "Long-Horizon Presenter"', script)
        self.assertIn(
            '"long-horizon-presenter-harness": "Long-Horizon Presenter Harness"',
            script,
        )

    def test_unified_generation_workspace_uses_product_activity_language(self):
        root = Path(__file__).resolve().parents[1]
        template = (root / "templates" / "app.html").read_text(encoding="utf-8")
        script = (root / "static" / "app.js").read_text(encoding="utf-8")
        css = (root / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn('title="切换当前页：页面预览 / 消息"', template)
        self.assertIn('data-view="ppt" class="on">页面预览</button>', template)
        self.assertIn('data-view="console">消息</button>', template)
        self.assertIn("function workstreamMarkup", script)
        self.assertIn("function outlineConversationMarkup", script)
        self.assertIn("function outlineBriefData", script)
        self.assertIn('class="outline-user-turn${initial ? " initial" : " followup"}"', script)
        self.assertIn('id="outline-user-query"', script)
        self.assertIn("function outlineConversationTurns", script)
        self.assertIn("function outlineConversationLaneMarkup", script)
        self.assertIn("outlineHistoricalAgentMarkup(turn, kind)", script)
        self.assertIn("outlineCurrentAgentMarkup(kind, running)", script)
        self.assertIn('id="outline-chat-lane"', script)
        self.assertIn('id="agent-progress-console"', script)
        self.assertIn("profileAvatar.textContent = initial", script)
        self.assertNotIn("profileAvatar.innerHTML", script)
        self.assertIn('$("#agent-progress-console") || $("#gen-console")', script)
        self.assertIn('setRailLabels("dynamic")', script)
        self.assertIn('setRailLabels("static")', script)
        self.assertIn('const showDeck = n > 0 && rendered', script)
        self.assertIn('const hasPreview = !isOutline && ed.rendered.has(ed.sel)', script)
        self.assertIn('classList.toggle("outline-layout", isOutline)', script)
        self.assertIn('b.disabled = !isConsole && !hasPreview', script)
        self.assertIn('function prepareStaticDeckMotion(frame)', script)
        self.assertIn('function playStaticSlide(frame, n)', script)
        self.assertIn('--motion-page-duration: 540ms !important', script)
        self.assertIn('playStaticSlide(frame, pending)', script)
        self.assertIn('event.ok ? "当前步骤已完成" : "发现问题，正在自动修正"', script)
        self.assertNotIn('event.args?.command || event.args?.prompt', script)
        self.assertIn("function renderAgentProgress", script)
        self.assertIn("function resizeGrowingTextarea", script)
        self.assertIn("function resizePrimaryComposerInput", script)
        self.assertIn("function renderOrchestrationProgress", script)
        self.assertIn("function orchestrationEntries", script)
        self.assertIn("function compactPageAgentResult", script)
        self.assertIn('class="orch-slide-detail"', script)
        self.assertIn("function legacyImageArtifactScopes", script)
        self.assertIn('specialistArtifacts.agents?.[meta.key]', script)
        self.assertIn('class="specialist-phase material-phase"', script)
        self.assertIn('class="specialist-phase review-phase specialist-review-details"', script)
        self.assertIn('entry.contactSheet ? [entry.contactSheet] : []', script)
        self.assertIn("function agentPages(key)", script)
        self.assertIn("function pageAgentLabel(key, n)", script)
        self.assertIn("ed.pageAgents = reconcilePageAgentOwners", script)
        self.assertNotIn('if (!evs.length) { key = "orch"', script)
        self.assertIn("function reconcileAgentStories", script)
        self.assertIn("body.dataset.progressShell", script)
        self.assertIn("storyList.dataset.renderKey", script)
        self.assertIn("actionList.dataset.renderKey", script)
        self.assertIn("function currentElapsedSeconds", script)
        self.assertIn("status.dataset.dynamicChromeKey", script)
        self.assertIn("actions.dataset.dynamicRenderKey", script)
        self.assertIn('const statusText = `${STATUS_LABEL[ed.status] || ed.status}` +', script)
        self.assertNotIn('${STATUS_LABEL[p.status] || p.status}${elapsed}${counts}', script)
        self.assertIn('class="agent-section-head"', script)
        self.assertIn('"本页制作记录"', script)
        self.assertIn('class="agent-actions"', script)
        self.assertIn('patch: "调整页面", terminal: "运行制作工具"', script)
        self.assertIn("const actions = summarizeToolEvents(evs)", script)
        self.assertIn("Productized activity stream", css)
        self.assertIn(".term-bar > i { display: none; }", css)
        self.assertIn('.term[data-kind="dynamic"] .term-bar .workstream-status i', css)
        self.assertIn(".agent-now", css)
        self.assertIn(".agent-message.latest", css)
        self.assertIn(".agent-actions > summary", css)
        self.assertIn(".outline-chat-body", css)

    def test_frontend_merges_repetitive_orchestrator_progress(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "static" / "app.js").read_text(encoding="utf-8")
        css = (root / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn("function orchestratorProgressStage", script)
        self.assertIn("function appendOrchestratorProgress", script)
        self.assertIn("function canonicalOrchestrationAgentKey", script)
        self.assertIn("lastTextByAgent.get(agentKey)", script)
        self.assertIn('type: "orchestrator-progress"', script)
        self.assertIn('class="orch-progress-history"', script)
        self.assertIn("查看已合并的 ${history.length} 条阶段进展", script)
        self.assertIn("if (orchestratorProgress && orchestratorProgress.stage !== stage)", script)
        self.assertIn(".orch-progress-current", css)
        self.assertIn(".orch-progress-history", css)
        self.assertIn(".editor.outline-layout .canvas", css)
        self.assertIn(".outline-chat-lane", css)
        self.assertNotIn(".outline-session-bar", css)
        self.assertIn(".orchestration-entry-list", css)
        self.assertIn(".outline-user-bubble", css)
        self.assertIn(".outline-agent-turn", css)
        self.assertIn('html[data-theme="dark"] .pr-steps li.done::before', css)
        self.assertIn('html[data-theme="dark"] .pr-steps li.now::before', css)
        self.assertIn('.pr-steps li.done:not(:last-child)::after', css)
        self.assertIn(".outline-agent-progress .agent-message:nth-last-child(n+3)", css)
        outline_agent_rule = css.split(".outline-agent-turn {", 1)[1].split("}", 1)[0]
        self.assertIn("margin-top: 0", outline_agent_rule)
        self.assertNotIn("margin-top: auto", outline_agent_rule)
        self.assertIn('class="outline-chat-lane"', script)
        self.assertNotIn('class="term-bar outline-session-bar"', script)
        self.assertIn('html[data-theme="dark"] .editor .badge', css)
        self.assertIn('html[data-theme="dark"] .editor[data-kind="dynamic"] .badge', css)

    def test_completed_history_items_offer_regeneration(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "static" / "app.js").read_text(encoding="utf-8")
        css = (root / "static" / "app.css").read_text(encoding="utf-8")
        main = (root / "app" / "main.py").read_text(encoding="utf-8")
        dynamic = (root / "app" / "dynamic.py").read_text(encoding="utf-8")
        self.assertIn('data-history-action="regenerate"', script)
        self.assertIn('historyContextTarget.status !== "completed"', script)
        self.assertIn("async function regenerateHistoryItem", script)
        self.assertIn(".history-menu-icon.regenerate", css)
        self.assertIn('@app.post("/api/decks/{deck_id}/regenerate")', main)
        self.assertIn("def regenerate_deck", main)
        self.assertIn('@router.post("/api/dynamic/regenerate")', dynamic)

    def test_app_template_removes_legacy_full_query_modal(self):
        template = Path(__file__).resolve().parents[1] / "templates" / "app.html"
        html = template.read_text(encoding="utf-8")
        self.assertNotIn('id="query-modal"', html)
        self.assertNotIn('id="query-text"', html)
        self.assertNotIn('id="query-copy"', html)

    def test_app_template_has_deck_overview_modal(self):
        template = Path(__file__).resolve().parents[1] / "templates" / "app.html"
        html = template.read_text(encoding="utf-8")
        self.assertIn('id="overview-modal"', html)
        self.assertIn('id="overview-grid"', html)
        self.assertIn('id="overview-close"', html)

    def test_frontend_copies_query_from_the_user_message(self):
        script = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn('id="query-btn"', script)
        self.assertNotIn("openQueryModal", script)
        self.assertIn('id="outline-query-copy"', script)
        self.assertIn("function copyOutlineQuery(button)", script)
        self.assertIn('$("#outline-user-query")?.textContent?.trim()', script)
        self.assertNotIn('class="outline-specs"', script)
        self.assertIn("function legacyCopyText(text)", script)
        self.assertIn('document.execCommand("copy")', script)
        self.assertIn("window.isSecureContext && navigator.clipboard?.writeText", script)

    def test_frontend_exposes_primary_attachment_action(self):
        template = Path(__file__).resolve().parents[1] / "templates" / "app.html"
        html = template.read_text(encoding="utf-8")
        script = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        css = (Path(__file__).resolve().parents[1] / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn('id="attach-btn"', html)
        self.assertIn('aria-controls="attach-menu"', html)
        self.assertIn('class="attach-plus"', html)
        self.assertIn('id="attach-menu" hidden', html)
        self.assertIn('id="attach-upload-action"', html)
        self.assertIn('id="attach-count"', html)
        self.assertIn("上传文件或图片", html)
        self.assertIn('id="attachment-mode"', html)
        self.assertIn('value="web_parse"', html)
        self.assertNotIn("更多设置", html)
        self.assertNotIn('class="static-more"', html)
        self.assertIn('body.set("attachment_mode", attachmentMode())', script)
        self.assertIn("attachFiles.forEach", script)
        self.assertIn("function setAttachMenu(open)", script)
        self.assertIn(".attach-btn", css)
        self.assertIn(".attach-menu", css)

    def test_frontend_exposes_deck_overview_action(self):
        script = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="overview-btn"', script)
        self.assertIn("openOverviewModal", script)
        self.assertIn("renderOverviewGrid", script)
        self.assertIn("closeOverviewModal", script)
        self.assertIn("overview-card", script)
        self.assertIn("fileUrl(n)", script)
        self.assertIn("syncViewToggle()", script)
        self.assertNotIn("updateViewToggle", script)
        self.assertNotIn("VIEW_MODE_KEY", script)

    def test_overview_modal_styles_large_scrollable_grid(self):
        css = (Path(__file__).resolve().parents[1] / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn(".overview-modal", css)
        self.assertIn(".overview-grid", css)
        self.assertIn("grid-template-columns: repeat(auto-fill, minmax(300px, 1fr))", css)
        self.assertIn("grid-auto-rows: max-content", css)
        self.assertIn("align-content: start", css)
        self.assertIn("aspect-ratio: 16 / 9", css)
        self.assertIn("object-fit: contain", css)
        self.assertIn("overflow: auto", css)

    def test_page_brief_defaults_to_collapsed(self):
        script = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("ppCollapsed: true", script)
        self.assertIn('panel.classList.toggle("collapsed", !!ed.ppCollapsed)', script)
        self.assertIn('$("#pp-toggle").textContent = ed.ppCollapsed ? "展开" : "收起"', script)
        self.assertNotIn('panel.classList.remove("collapsed")', script)

    def test_deployment_uses_isolated_session_cookie(self):
        root = Path(__file__).resolve().parents[2]
        auth_src = (root / "studio" / "app" / "auth.py").read_text(encoding="utf-8")
        launch_src = (root / "scripts" / "launch.py").read_text(encoding="utf-8")
        bootstrap_src = (root / "scripts" / "bootstrap_chromium_deps.sh").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("STUDIO_SESSION_COOKIE", "studio_session")', auth_src)
        self.assertIn('"STUDIO_SESSION_COOKIE": "sense_nova_present_session"', launch_src)
        self.assertIn('"--ui-only"', launch_src)
        self.assertIn('"PPTAGENT_ENGINE_PYTHON"', launch_src)
        self.assertNotIn("/mnt/" + "afs/", bootstrap_src)

    def test_editor_layout_prioritizes_slide_preview_and_scrollable_assets(self):
        css = (Path(__file__).resolve().parents[1] / "static" / "app.css").read_text(encoding="utf-8")
        script = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("padding: 18px 12px 14px 28px", css)
        self.assertIn("border-left: 0", css)
        self.assertIn("background: rgba(248,248,251,.92); box-shadow: none", css)
        self.assertIn("max-height: clamp(210px, 34vh, 360px)", css)
        self.assertIn("overflow: hidden", css)
        self.assertIn("grid-template-columns: minmax(0, 1fr);", css)
        self.assertIn(".plan-panel.has-assets .pp-body { grid-template-columns: minmax(0, 1fr) 172px; }", css)
        self.assertIn("max-height: clamp(150px, 24vh, 260px)", css)
        self.assertIn(".pp-assets-grid { flex: 1; min-height: 0; overflow-y: auto", css)
        self.assertIn("calc((100vh - 180px) * 16 / 9)", css)
        self.assertIn('panel.classList.toggle("has-assets", !!assets.length)', script)
        self.assertIn('panel.classList.remove("has-assets")', script)

    def test_app_template_accepts_legacy_three_field_model_rows(self):
        template_dir = Path(__file__).resolve().parents[1] / "templates"
        env = Environment(loader=FileSystemLoader(template_dir))
        html = env.get_template("app.html").render(
            asset_ver=1,
            user={"display_name": "", "username": "tester", "role": "user"},
            conversations=[],
            models=[("opus-4.7-thinking", "Opus 4.7 Thinking", "anthropic")],
            default_model="opus-4.7-thinking",
            pipelines=[("infer", "Clean infer harness", "anthropic,openai", "clean-bilingual", "attachments", 1)],
            default_pipeline="infer",
            skills=[
                ("auto", "Auto（自动选择）", "infer", 1, ""),
                ("zh", "中文 Skill", "infer", 1, ""),
                ("en", "English Skill", "infer", 1, ""),
            ],
            default_skill="auto",
        )
        self.assertIn("pipeline-display", html)

    def test_guest_workspace_defers_authentication_until_generation(self):
        root = Path(__file__).resolve().parents[1]
        template = (root / "templates" / "app.html").read_text(encoding="utf-8")
        script = (root / "static" / "app.js").read_text(encoding="utf-8")
        main_src = (root / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn('data-authenticated="{{ 1 if authenticated else 0 }}"', template)
        self.assertIn('class="auth-gate" id="auth-gate" hidden', template)
        self.assertIn('id="auth-login-form"', template)
        self.assertIn('id="auth-register-form"', template)
        self.assertIn('if (!requireAuthentication({ resume: true })) return;', script)
        self.assertIn('if (shouldResume) await send();', script)
        self.assertIn('return RedirectResponse("/" if user else "/?auth=login"', main_src)
        self.assertIn('"authenticated": authenticated', main_src)

    def test_home_ambient_and_auth_card_honor_motion_and_dark_theme(self):
        root = Path(__file__).resolve().parents[1]
        template = (root / "templates" / "app.html").read_text(encoding="utf-8")
        script = (root / "static" / "app.js").read_text(encoding="utf-8")
        css = (root / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn('class="nova-orb orb-one"', template)
        self.assertIn('class="nova-sweep sweep-one"', template)
        self.assertIn('creationWorkspace?.addEventListener("pointermove"', script)
        self.assertIn("function initNovaDoodle()", script)
        self.assertIn('id="nova-doodle-canvas"', template)
        self.assertIn('@keyframes nova-sweep', css)
        self.assertIn('@media (prefers-reduced-motion: reduce)', css)
        self.assertIn('html[data-theme="dark"] .auth-gate-dialog', css)

    def test_static_page_history_is_shown_in_the_process_rail(self):
        root = Path(__file__).resolve().parents[1]
        template = (root / "templates" / "app.html").read_text(encoding="utf-8")
        script = (root / "static" / "app.js").read_text(encoding="utf-8")
        css = (root / "static" / "app.css").read_text(encoding="utf-8")
        main_src = (root / "app" / "main.py").read_text(encoding="utf-8")
        dynamic_src = (root / "app" / "dynamic.py").read_text(encoding="utf-8")
        self.assertIn('id="pr-page-history"', template)
        self.assertIn('id="pr-check-section"', template)
        self.assertIn('id="pr-speech-section"', template)
        self.assertIn('id="page-speech-toggle"', template)
        self.assertIn('class="pr-rail-body page-speech-body"', template)
        self.assertIn('id="pr-speech-content"', template)
        self.assertIn('class="pr-rail-body"', template)
        self.assertIn('function loadPageHistory', script)
        self.assertIn('function renderPageSpeech', script)
        self.assertIn('function loadDynamicPageSpeech', script)
        self.assertIn('function syncPageSpeechDrawer', script)
        self.assertIn('if (ed.historyScopePage === n) return;', script)
        self.assertIn('if (ed.historyRenderKey === renderKey) return;', script)
        self.assertIn('if (ed.speechRenderKey === renderKey) return;', script)
        self.assertIn('scroll.scrollTop = previousTop', script)
        self.assertIn('function runErrorText', script)
        self.assertIn('模型连续返回空响应，自愈重试后仍未恢复', script)
        self.assertIn('function speechMarkdownBlocks', script)
        self.assertIn('function setRailDisclosureOpen', script)
        self.assertIn('function initRailDisclosureAnimations', script)
        self.assertIn('shellAnimation.finished', script)
        self.assertIn('if (!open) details.open = false', script)
        self.assertIn('`/api/decks/${ed.id}/page-history`}?n=${page}', script)
        self.assertIn('Vision 判断', script)
        self.assertIn('整册 Review · 未完成', script)
        self.assertIn('修改记录 · ${changed} 次', script)
        self.assertIn('.proc-rail.page-history-mode', css)
        self.assertIn('.pr-rail-section[open]', css)
        self.assertIn('.pr-rail-section.is-animating > summary', css)
        self.assertIn('.pr-rail-body', css)
        self.assertIn('.pr-speech-content', css)
        self.assertIn('.page-speech-drawer.collapsed', css)
        self.assertIn('@app.get("/api/decks/{deck_id}/page-history")', main_src)
        self.assertIn('data["speech"] = trace.page_speech', main_src)
        self.assertIn('@router.get("/api/dynamic/page-speech")', dynamic_src)

    def test_clean_harness_recovers_from_consecutive_empty_responses(self):
        self._require_all_external_runtime()
        root = Path(__file__).resolve().parents[2] / "vendor" / "static_ppt-clean-current" / "core"
        loop_src = (root / "agent_loop.py").read_text(encoding="utf-8")
        config_src = (root / "config.py").read_text(encoding="utf-8")
        self.assertIn("EMPTY_RECOVERY_PROMPTS", loop_src)
        self.assertIn("if heals >= 2:", loop_src)
        self.assertIn("_compact_live_history(", loop_src)
        self.assertIn("heals = 0\n        tool_results = _tool_results", loop_src)
        self.assertIn('CLEAN_MAX_HEALS", "4"', config_src)

    def test_editor_topbar_has_no_inset_highlight_seam(self):
        css = (Path(__file__).resolve().parents[1] / "static" / "app.css").read_text(encoding="utf-8")
        product_styles = css.split("SenseNova 统一生成工作台", 1)[1]
        ed_top_rule = product_styles.split(".ed-top {", 1)[1].split("}", 1)[0]
        self.assertIn("box-shadow: none", ed_top_rule)
        self.assertNotIn("inset", ed_top_rule)

    def test_desktop_ui_uses_a_slight_global_scale_without_overflow(self):
        css = (Path(__file__).resolve().parents[1] / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn("--ui-scale: 1.08", css)
        self.assertIn("zoom: var(--ui-scale)", css)
        self.assertIn("height: calc(100vh / var(--ui-scale))", css)
        self.assertIn("@media (max-width: 1199px)", css)

    def test_dynamic_mode_uses_mint_setting_icons(self):
        css = (Path(__file__).resolve().parents[1] / "static" / "app.css").read_text(encoding="utf-8")
        script = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('.composer-dock[data-mode="dynamic"] .length-trigger-icon', css)
        self.assertIn('.composer-dock[data-mode="dynamic"] .skill-trigger-icon', css)
        self.assertIn('.composer-dock[data-mode="dynamic"] .composer-actions #send', css)
        self.assertIn("background-color: #00a979", css)
        self.assertIn("mode-button-settle", css)
        self.assertIn("background-color .52s cubic-bezier", css)
        self.assertIn('setTimeout(() => dock?.classList.remove("mode-switching"), 680)', script)
        self.assertIn("color: #00966d", css)
        self.assertIn("color: #64d9b7", css)


if __name__ == "__main__":
    unittest.main()
