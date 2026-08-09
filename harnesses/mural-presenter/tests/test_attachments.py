import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import distill_ppt


def _write_complete_material(root, assignment, name, text="完整材料内容。"):
    work = root / f"materials/_work/{assignment}"
    work.mkdir(parents=True, exist_ok=True)
    raw = root / f"materials/_raw/{name}"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(text, encoding="utf-8")
    full = work / f"{name}.md"
    full.write_text(text, encoding="utf-8")
    chunk = work / f"_chunks/{name}/chunk_001.md"
    chunk.parent.mkdir(parents=True, exist_ok=True)
    chunk.write_text(text, encoding="utf-8")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    coverage_id = f"coverage-{assignment}"
    catalog = [{
        "name": name,
        "raw": raw.relative_to(root).as_posix(),
        "kind": "doc",
        "status": "ok",
        "text": full.relative_to(root).as_posix(),
        "text_sha256": digest,
        "text_chunks": [{
            "path": chunk.relative_to(root).as_posix(),
            "start_char": 0,
            "end_char": len(text),
            "chars": len(text),
            "sha256": digest,
        }],
        "coverage": {
            "status": "complete", "unit": "chars",
            "covered": len(text), "total": len(text), "chunks": 1,
        },
        "coverage_id": coverage_id,
    }]
    (work / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    summary = root / f"materials/summaries/{assignment}.md"
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(
        f"# 材料分片摘要\n\n## Coverage ledger\n- {name} | coverage_id: {coverage_id} | complete\n",
        encoding="utf-8",
    )


class AttachmentContractTest(unittest.TestCase):
    def test_named_paper_figure_is_cropped_and_registered_as_derived_asset(self):
        from PIL import Image, ImageDraw

        skill_root = Path(__file__).resolve().parents[3] / "skills/mural-presenter"
        script = skill_root / "scripts/deck.py"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            page = root / "materials/_work/material_01/_raw/paper.pdf_pages/p002.png"
            page.parent.mkdir(parents=True)
            image = Image.new("RGB", (1000, 1400), "white")
            ImageDraw.Draw(image).rectangle((100, 350, 900, 850), fill="#c85b3d")
            image.save(page)

            run = subprocess.run(
                [
                    sys.executable, str(script), "material-figure", str(root),
                    "--source", page.relative_to(root).as_posix(),
                    "--path", "assets/paper-figure-01.png",
                    "--figure-id", "Figure 1", "--source-page", "2",
                    "--box", "0.1,0.25,0.9,0.607142857",
                ],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            output = root / "assets/paper-figure-01.png"
            self.assertTrue(output.is_file())
            with Image.open(output) as cropped:
                self.assertEqual(cropped.size, (800, 500))
            catalog = json.loads((root / "assets/catalog.json").read_text(encoding="utf-8"))
            entry = catalog["assets"][0]
            self.assertEqual(entry["derivative_kind"], "material_figure_crop")
            self.assertEqual(entry["material_asset_type"], "figure_crop")
            self.assertEqual(entry["figure_id"], "Figure 1")
            self.assertEqual(entry["source_page"], 2)

    def test_named_paper_figure_rejects_near_full_page_crop(self):
        from PIL import Image

        skill_root = Path(__file__).resolve().parents[3] / "skills/mural-presenter"
        script = skill_root / "scripts/deck.py"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            page = root / "materials/_work/material_01/_raw/paper.pdf_pages/p001.png"
            page.parent.mkdir(parents=True)
            Image.new("RGB", (1000, 1400), "white").save(page)
            run = subprocess.run(
                [
                    sys.executable, str(script), "material-figure", str(root),
                    "--source", page.relative_to(root).as_posix(),
                    "--path", "assets/not-a-figure.png",
                    "--figure-id", "Figure 1", "--source-page", "1",
                    "--box", "0,0,1,1",
                ],
                capture_output=True, text=True, check=False,
            )
            self.assertNotEqual(run.returncode, 0)
            self.assertIn("almost the whole paper page", run.stderr)
            self.assertFalse((root / "assets/not-a-figure.png").exists())

    def test_pdf_page_visual_cannot_be_registered_as_a_generic_material_image(self):
        from PIL import Image

        skill_root = Path(__file__).resolve().parents[3] / "skills/mural-presenter"
        script = skill_root / "scripts/deck.py"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            page = root / "materials/_work/material_01/_raw/paper.pdf_pages/p003.png"
            page.parent.mkdir(parents=True)
            Image.new("RGB", (800, 1200), "white").save(page)
            run = subprocess.run(
                [
                    sys.executable, str(script), "asset-register", str(root),
                    "--path", "assets/paper-page.png", "--origin", "material",
                    "--source-path", page.relative_to(root).as_posix(),
                ],
                capture_output=True, text=True, check=False,
            )
            self.assertNotEqual(run.returncode, 0)
            self.assertIn("context-only", run.stderr)
            self.assertFalse((root / "assets/paper-page.png").exists())

    def test_plain_markdown_stages_without_markitdown(self):
        skill_root = Path(__file__).resolve().parents[3] / "skills/mural-presenter"
        script = skill_root / "scripts/stage_materials.py"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "brief.md"
            source.write_text("# 简报\n\n- 原生 Markdown 不依赖转换器\n", encoding="utf-8")
            output = root / "materials"
            env = dict(os.environ)
            env["NORMALIZE_PY"] = sys.executable
            run = subprocess.run(
                [sys.executable, str(script), str(output), "--input", str(source)],
                capture_output=True, text=True, env=env, check=False,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            catalog = json.loads((output / "catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(catalog[0]["status"], "ok")
            self.assertEqual(catalog[0]["coverage"]["status"], "complete")
            self.assertEqual(
                (output / "brief.md.md").read_text(encoding="utf-8"),
                source.read_text(encoding="utf-8").strip(),
            )

    def test_revision_brief_routes_simple_and_complex_edits(self):
        brief = distill_ppt._revision_brief(
            {"query": "制作一套产品发布演示"},
            {"instruction": "修正第 3 页标题"},
        )

        self.assertIn("6. 编辑已有演示文稿", brief)
        self.assertIn("mode=simple_edit", brief)
        self.assertIn("mode=final_review", brief)
        self.assertIn("只选择一条路径", brief)
        self.assertIn("保留无关页面", brief)

    def test_initial_brief_preserves_raw_user_query(self):
        query = "请根据附件制作演示"
        brief = distill_ppt.seed_to_brief({
            "query": query,
            "attachments": [{"name": "source.pdf", "path": "/tmp/source.pdf"}],
        })
        self.assertEqual(brief, query)

    def test_runtime_page_count_uses_explicit_ui_value_not_pages_hint(self):
        self.assertNotIn(
            "page_count", distill_ppt._generation_preferences({"pages_hint": 24})
        )
        self.assertEqual(
            distill_ppt._generation_preferences({"slide_count": 18})["page_count"],
            18,
        )

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

    def test_custom_fonts_are_staged_outside_material_attachments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "Brand Display.ttf"
            source.write_bytes(b"font-source")
            digest = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
            run_dir = root / "run"
            run_dir.mkdir()
            config = distill_ppt._stage_custom_fonts(str(run_dir), {
                "font_config": {
                    "version": 1,
                    "license_acknowledged": True,
                    "roles": {"title": {"kind": "custom", "font_id": "font-01"}},
                    "fonts": [{
                        "id": "font-01", "name": source.name, "path": str(source),
                        "sha256": digest, "family": "Brand Display",
                    }],
                }
            })
            self.assertEqual(config["fonts"][0]["source_path"], "materials/_fonts/Brand_Display.ttf")
            self.assertTrue((run_dir / "materials/_fonts/Brand_Display.ttf").is_file())
            self.assertTrue((run_dir / "materials/font-config.json").is_file())
            self.assertFalse((run_dir / "materials/attachments.json").exists())

    def test_missing_empty_path_is_reported(self):
        missing = distill_ppt._missing_attachments({
            "attachments": [{"name": "missing.pdf", "path": ""}]
        })
        self.assertEqual(missing, ["missing.pdf"])

    def test_acceptance_requires_material_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workers = [{
                "label": "material_01", "clean": True,
                "contract": {"status": "ready", "coverage": "complete"},
            }]
            ok, reason = distill_ppt._material_acceptance(str(root), workers)
            self.assertFalse(ok)
            self.assertIn("附件清单", reason)

            (root / "materials").mkdir(parents=True)
            (root / "materials/attachments.json").write_text(
                json.dumps({"attachments": [{"name": "source.pdf"}]}), encoding="utf-8"
            )
            _write_complete_material(root, "material_01", "source.pdf")
            ok, reason = distill_ppt._material_acceptance(str(root), workers)
            self.assertTrue(ok, reason)

    def test_acceptance_allows_complete_coverage_with_human_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "materials").mkdir(parents=True)
            (root / "materials/attachments.json").write_text(
                json.dumps({"attachments": [{"name": "source.md"}]}), encoding="utf-8"
            )
            _write_complete_material(root, "material_01", "source.md")
            workers = [{
                "label": "material_01", "clean": True,
                "contract": {
                    "status": "ready",
                    "coverage": "complete（1 chunk / 299 chars）",
                },
            }]
            ok, reason = distill_ppt._material_acceptance(str(root), workers)
            self.assertTrue(ok, reason)

    def test_acceptance_rejects_direct_material_bypass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "materials").mkdir(parents=True)
            (root / "materials/attachments.json").write_text(
                json.dumps({"attachments": [{"name": "source.pdf"}]}),
                encoding="utf-8",
            )
            _write_complete_material(root, "material_01", "source.pdf")
            ok, reason = distill_ppt._material_acceptance(str(root), [])
            self.assertFalse(ok)
            self.assertIn("Material worker", reason)

    def test_acceptance_supports_parallel_material_shards(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workers = [
                {"label": "material_01", "clean": True,
                 "contract": {"status": "ready", "coverage": "complete"}},
                {"label": "material_02", "clean": True,
                 "contract": {"status": "ready", "coverage": "complete"}},
            ]
            (root / "materials").mkdir(parents=True)
            (root / "materials/attachments.json").write_text(
                json.dumps({"attachments": [
                    {"name": "source-1.pdf"}, {"name": "source-2.pdf"},
                ]}),
                encoding="utf-8",
            )
            for number in (1, 2):
                _write_complete_material(
                    root, f"material_{number:02d}", f"source-{number}.pdf"
                )
            ok, reason = distill_ppt._material_acceptance(str(root), workers)
            self.assertTrue(ok, reason)

    def test_acceptance_ignores_pdf_page_visual_derivatives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "materials").mkdir(parents=True)
            (root / "materials/attachments.json").write_text(
                json.dumps({"attachments": [{"name": "source.pdf"}]}), encoding="utf-8"
            )
            _write_complete_material(root, "material_01", "source.pdf")
            catalog_path = root / "materials/_work/material_01/catalog.json"
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            page = root / "materials/_work/material_01/_raw/source.pdf_pages/p001.png"
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_bytes(b"png")
            catalog.extend([
                {
                    "name": "source.pdf · visual p1",
                    "raw": page.relative_to(root).as_posix(),
                    "kind": "image", "status": "ok", "page": 1,
                    "is_derivative": True, "derived_from": "source.pdf",
                    "derivative_kind": "pdf_page_visual",
                    "coverage": {"status": "complete", "unit": "asset", "covered": 1, "total": 1},
                    "coverage_id": "derived-page-1",
                },
                {
                    # Legacy catalogs used this field without explicit lineage.
                    "name": "source.pdf · visual p2",
                    "raw": page.relative_to(root).as_posix(),
                    "kind": "image", "status": "ok", "page": 2,
                    "from_pdf": "source.pdf",
                    "coverage": {"status": "complete", "unit": "asset", "covered": 1, "total": 1},
                    "coverage_id": "derived-page-2",
                },
            ])
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            workers = [{
                "label": "material_01", "clean": True,
                "contract": {"status": "ready", "coverage": "complete"},
            }]
            ok, reason = distill_ppt._material_acceptance(str(root), workers)
            self.assertTrue(ok, reason)

    def test_acceptance_still_rejects_unrelated_extra_root_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "materials").mkdir(parents=True)
            (root / "materials/attachments.json").write_text(
                json.dumps({"attachments": [{"name": "source.pdf"}]}), encoding="utf-8"
            )
            _write_complete_material(root, "material_01", "source.pdf")
            catalog_path = root / "materials/_work/material_01/catalog.json"
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            extra = root / "materials/_raw/unrelated.png"
            extra.write_bytes(b"png")
            catalog.append({
                "name": "unrelated.png", "raw": extra.relative_to(root).as_posix(),
                "kind": "image", "status": "ok",
                "coverage": {"status": "complete", "unit": "asset", "covered": 1, "total": 1},
                "coverage_id": "unrelated-root",
            })
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            workers = [{
                "label": "material_01", "clean": True,
                "contract": {"status": "ready", "coverage": "complete"},
            }]
            ok, reason = distill_ppt._material_acceptance(str(root), workers)
            self.assertFalse(ok)
            self.assertIn("unrelated.png", reason)

    def test_acceptance_rejects_truncated_material(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "materials/_work/material_01"
            work.mkdir(parents=True)
            (root / "materials/attachments.json").write_text(
                json.dumps({"attachments": [{"name": "lecture.pdf"}]}), encoding="utf-8"
            )
            (work / "catalog.json").write_text(json.dumps([{
                "name": "lecture.pdf", "kind": "doc", "status": "truncated",
            }]), encoding="utf-8")
            summary = root / "materials/summaries/material_01.md"
            summary.parent.mkdir(parents=True)
            summary.write_text("# 摘要\n\n部分内容已经读取，但不是全文。", encoding="utf-8")
            workers = [{
                "label": "material_01", "clean": True,
                "contract": {"status": "ready", "coverage": "complete"},
            }]
            ok, reason = distill_ppt._material_acceptance(str(root), workers)
            self.assertFalse(ok)
            self.assertIn("status=truncated", reason)

    def test_attachment_review_requires_ready_content_fidelity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "_trace/content-fidelity.md"
            report.parent.mkdir(parents=True)
            report.write_text("# Content fidelity\n\nAll slide claims trace to attachment evidence.\n", encoding="utf-8")
            blocked = [{
                "label": "review", "clean": True,
                "contract": {"status": "ready", "content_fidelity": "fail"},
            }]
            ok, reason = distill_ppt._attachment_review_acceptance(str(root), blocked)
            self.assertFalse(ok)
            self.assertIn("content_fidelity", reason)
            ready = [{
                "label": "review", "clean": True,
                "contract": {"status": "ready", "content_fidelity": "pass"},
            }]
            ok, reason = distill_ppt._attachment_review_acceptance(str(root), ready)
            self.assertTrue(ok, reason)


if __name__ == "__main__":
    unittest.main()
