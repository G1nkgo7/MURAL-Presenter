import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


STUDIO_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = STUDIO_ROOT.parent
sys.path.insert(0, str(STUDIO_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "distillation"))

from app import engine, jobs, main, trace


def load_harness():
    path = PROJECT_ROOT / "distillation" / "sense_present_v2.py"
    spec = importlib.util.spec_from_file_location("sense_present_v2_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    previous_agent_loop = sys.modules.get("agent_loop")
    previous_tools = sys.modules.get("tools")
    sys.modules["agent_loop"] = types.SimpleNamespace()
    sys.modules["tools"] = types.SimpleNamespace()
    try:
        spec.loader.exec_module(module)
    finally:
        if previous_agent_loop is None:
            sys.modules.pop("agent_loop", None)
        else:
            sys.modules["agent_loop"] = previous_agent_loop
        if previous_tools is None:
            sys.modules.pop("tools", None)
        else:
            sys.modules["tools"] = previous_tools
    return module


class SensePresentV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.harness = load_harness()

    def _require_direct_skills(self):
        missing = [
            key for key in ("sense-present-standard", "sense-present-dazzle")
            if not engine.SKILLS[key].get("ready", False)
        ]
        if missing:
            self.skipTest("external direct Skills are not mounted: " + ", ".join(missing))

    def test_catalog_exposes_two_direct_skills_at_one_fixed_revision(self):
        self._require_direct_skills()
        expected = {
            "sense-present-standard": ("sn-ppt-standard", "sense-present-standard-harness"),
            "sense-present-dazzle": ("sn-ppt-dazzle", "sense-present-dazzle-harness"),
        }
        for key, (name, pipeline) in expected.items():
            with self.subTest(skill=key):
                skill = engine.SKILLS[key]
                self.assertTrue(skill["ready"], skill["unavailable_reason"])
                self.assertEqual(skill["source_revision"], "8481ba94")
                self.assertEqual(skill["name"], name)
                self.assertEqual(skill["pipeline"], pipeline)
                self.assertEqual(engine.pipeline_for_skill(key), pipeline)
                self.assertTrue((Path(skill["path"]) / "SKILL.md").is_file())

    def test_build_job_preserves_static_or_dynamic_output(self):
        self._require_direct_skills()
        pairs = (
            ("sense-present-standard", "static_html", "sense-present-standard-harness", "sense_present_standard.py"),
            ("sense-present-dazzle", "dynamic_html", "sense-present-dazzle-harness", "sense_present_dazzle.py"),
        )
        for skill, output, pipeline, entry in pairs:
            with self.subTest(skill=skill):
                job = engine.build_job(
                    f"v2-{output}",
                    {"query": "做两页演示", "slide_count": 2, "ppt_output": output},
                    engine.deck_run_dir(1, 9001),
                    model_key="sensenova-flash-lite-v39",
                    skill_key=skill,
                )
                self.assertEqual(job["pipeline_version"], pipeline)
                self.assertEqual(job["skill_version"], skill)
                self.assertEqual(job["seed"]["ppt_output"], output)
                self.assertEqual(job["pipeline"]["entry"], entry)

    def test_bootstrap_routes_one_pack_to_both_outputs_and_staged_files(self):
        with tempfile.TemporaryDirectory(prefix="sense-present-v2-") as tmp:
            root = Path(tmp)
            manifest = root / "attachments" / "manifest.json"
            manifest.parent.mkdir()
            manifest.write_text(json.dumps({
                "raw_attachments": [{"path": "attachments/raw/source.pdf"}],
                "images": [{"path": "attachments/image/cover.png"}],
            }), encoding="utf-8")
            task = self.harness._bootstrap(root, {
                "query": "制作一份发布会演示",
                "slide_count": 6,
                "ppt_output": "dynamic_html",
            })
            self.assertEqual(task["ppt_mode"], "dazzle")
            self.assertEqual(task["choices"]["output"], "dynamic_html")
            self.assertEqual(task["params"]["page_count"], 6)
            self.assertEqual(task["choices"]["static_postprocess"], [])
            self.assertEqual(task["request"]["source_files"], [
                "attachments/raw/source.pdf", "attachments/image/cover.png",
            ])
            self.assertTrue((root / "info_pack.json").is_file())

    def test_accepts_canonical_static_and_dynamic_deliveries(self):
        with tempfile.TemporaryDirectory(prefix="sense-present-v2-static-") as tmp:
            root = Path(tmp)
            (root / "slides").mkdir()
            (root / "renders").mkdir()
            (root / "present.html").write_text("<html></html>", encoding="utf-8")
            for n in (1, 2):
                (root / "slides" / f"slide_{n:02d}.html").write_text("<section></section>", encoding="utf-8")
                (root / "renders" / f"slide_{n:02d}.png").write_bytes(b"png")
            self.assertEqual(self.harness._accept(root, "static_html"), (True, "ok", 2))

        with tempfile.TemporaryDirectory(prefix="sense-present-v2-dynamic-") as tmp:
            root = Path(tmp)
            (root / "shots").mkdir()
            (root / "deck.html").write_text("<section class='slide active'></section>", encoding="utf-8")
            (root / "deck_manifest.json").write_text(json.dumps({"n_pages": 3}), encoding="utf-8")
            for n in (1, 2, 3):
                (root / "shots" / f"page_{n:02d}.png").write_bytes(b"png")
            (root / "shots" / "render.json").write_text(json.dumps({
                "n_pages": 3,
                "console_errors": [],
            }), encoding="utf-8")
            self.assertEqual(self.harness._accept(root, "dynamic_html"), (True, "ok", 3))
            preview = main._static_html_preview_state(str(root))
            self.assertTrue(preview["ready"])
            self.assertEqual(preview["entry"], "deck.html")
            snap = trace.snapshot(str(root), seed={"ppt_output": "dynamic_html"}, status="completed")
            self.assertEqual(snap["rendered"], [1, 2, 3])
            self.assertEqual(snap["ppt_output"], "dynamic_html")

    def test_direct_harness_pixel_contracts_are_distinct(self):
        static_orch = SimpleNamespace(worker_recs=[
            {"label": "slide_01", "clean": True, "vision_calls": 1},
            {"label": "slide_02", "clean": True, "vision_calls": 1},
            {"label": "review", "clean": True, "vision_calls": 1},
        ])
        self.assertEqual(
            self.harness._pixel_acceptance(static_orch, "static_html", 2),
            (True, "ok"),
        )
        static_orch.worker_recs[0]["vision_calls"] = 0
        ok, reason = self.harness._pixel_acceptance(static_orch, "static_html", 2)
        self.assertFalse(ok)
        self.assertIn("Slide", reason)

        dynamic_orch = SimpleNamespace(n_vision_calls=3, worker_recs=[])
        self.assertEqual(
            self.harness._pixel_acceptance(dynamic_orch, "dynamic_html", 3),
            (True, "ok"),
        )
        dynamic_orch.n_vision_calls = 2
        ok, reason = self.harness._pixel_acceptance(dynamic_orch, "dynamic_html", 3)
        self.assertFalse(ok)
        self.assertIn("Dazzle", reason)

    def test_workspace_exposes_immutable_direct_skill_paths_to_shell_commands(self):
        self._require_direct_skills()
        with tempfile.TemporaryDirectory(prefix="sense-present-skill-links-") as tmp:
            root = Path(tmp)
            self.harness._link_skill_tree(root)
            for name in (
                "sn-ppt-story", "sn-ppt-standard", "sn-ppt-dazzle", "sn-ppt-tools",
            ):
                self.assertTrue((root / "skills" / name / "SKILL.md").is_file() or (
                    root / "skills" / name / "references" / "capability-policy.md"
                ).is_file())

    def test_dynamic_delivery_and_revision_are_in_place(self):
        with tempfile.TemporaryDirectory(prefix="sense-present-v2-revision-") as tmp:
            root = Path(tmp)
            (root / "deck.html").write_text("<html></html>", encoding="utf-8")
            row = {
                "id": 77,
                "run_dir": str(root),
                "seed_json": json.dumps({"ppt_output": "dynamic_html"}),
            }
            self.assertIsNone(jobs._ensure_static_delivery(row, root))
            jobs._prepare_revision_workspace(row, {
                "parent_deck_id": 77,
                "revision_no": 2,
                "instruction": "把封面标题放大",
                "in_place": True,
            })
            request = root / "_trace" / "revisions" / "revision_002" / "request.json"
            self.assertTrue(request.is_file())
            self.assertTrue((root / "deck.html").is_file())

    def test_direct_agent_names_follow_the_existing_progress_protocol(self):
        with tempfile.TemporaryDirectory(prefix="sense-present-feed-") as tmp:
            log = Path(tmp) / "job.log"
            log.write_text(
                "[deck-1/orchestrator] [1] 💬 正在规划整套演示\n"
                "[deck-1/orchestrator] 🔧 delegate_task({\"name\":\"image_01\"})\n"
                "[deck-1/image_01] [1] 💬 正在准备视觉素材\n"
                "[deck-1/slide_01] 🔧 write({\"path\":\"slides/slide_01.html\"})\n",
                encoding="utf-8",
            )
            feed = trace.livefeed(str(log))
            self.assertEqual(set(feed["agents"]), {"orch", "image_01", "slide_01"})
            self.assertEqual(feed["agents"]["orch"][0]["k"], "text")


if __name__ == "__main__":
    unittest.main()
