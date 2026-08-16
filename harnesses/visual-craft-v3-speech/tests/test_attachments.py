import json
import tempfile
import unittest
from pathlib import Path

import distill_ppt


class AttachmentContractTest(unittest.TestCase):
    def test_initial_brief_injects_material_contract(self):
        brief = distill_ppt.seed_to_brief({
            "query": "请根据附件制作演示",
            "attachments": [{"name": "source.pdf", "path": "/tmp/source.pdf"}],
        })

        self.assertIn("【V3 附件材料】", brief)
        self.assertIn("materials/_raw/", brief)
        self.assertIn('["file","terminal","vision"]', brief)
        self.assertIn("material", brief)

    def test_stage_sanitizes_and_deduplicates_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.txt"
            second = root / "second.txt"
            first.write_text("one", encoding="utf-8")
            second.write_text("two", encoding="utf-8")
            run_dir = root / "run"
            run_dir.mkdir()

            manifest = distill_ppt._stage_materials(str(run_dir), {
                "attachments": [
                    {"name": "../same.txt", "path": str(first)},
                    {"name": "same.txt", "source_path": str(second)},
                ]
            })

            self.assertEqual([item["name"] for item in manifest], ["same.txt", "same_2.txt"])
            self.assertEqual((run_dir / "materials/_raw/same.txt").read_text(), "one")
            self.assertEqual((run_dir / "materials/_raw/same_2.txt").read_text(), "two")

    def test_missing_empty_path_is_reported(self):
        missing = distill_ppt._missing_attachments({
            "attachments": [{"name": "missing.pdf", "path": ""}]
        })
        self.assertEqual(missing, ["missing.pdf"])

    def test_acceptance_requires_material_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workers = [{"label": "material", "clean": True}]
            ok, reason = distill_ppt._material_acceptance(str(root), workers)
            self.assertFalse(ok)
            self.assertIn("catalog.json", reason)

            (root / "materials").mkdir()
            (root / "research").mkdir()
            (root / "materials/catalog.json").write_text(
                json.dumps([{"name": "source.pdf", "status": "ok"}]), encoding="utf-8"
            )
            (root / "research/materials.md").write_text(
                "# 材料摘要\n\n附件内容已经完成解析与归纳。", encoding="utf-8"
            )
            ok, reason = distill_ppt._material_acceptance(str(root), workers)
            self.assertTrue(ok, reason)


if __name__ == "__main__":
    unittest.main()
