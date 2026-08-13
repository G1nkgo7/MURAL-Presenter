import subprocess
import sys
import tempfile
import unittest
import zipfile
import hashlib
import json
import ast
import types
from importlib import util
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "skills/mural-presenter/scripts"
SKILL_ROOT = REPO / "skills/mural-presenter"


def _load_deck_module():
    sys.path.insert(0, str(SCRIPTS))
    spec = util.spec_from_file_location("visual_craft_v3_deck", SCRIPTS / "deck.py")
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_font_bundle_module():
    sys.path.insert(0, str(SCRIPTS))
    spec = util.spec_from_file_location("visual_craft_v3_font_bundle", SCRIPTS / "font_bundle.py")
    module = util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_stage_materials_module():
    spec = util.spec_from_file_location("mural_presenter_stage", SCRIPTS / "stage_materials.py")
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConsolidatedScriptTest(unittest.TestCase):
    def test_bilingual_skill_surface_keeps_shared_machine_contracts(self):
        english_skill = SKILL_ROOT / "SKILL.en.md"
        self.assertTrue(english_skill.is_file())
        skill_text = english_skill.read_text(encoding="utf-8")
        self.assertIn("response_language", skill_text)
        self.assertIn("deliverable_language", skill_text)
        self.assertIn("SKILL.md", skill_text)
        self.assertIn("subagents/<role>.en.md", skill_text)

        required_contracts = {
            "research": ("status: ready | partial | blocked", "output: research/research.md"),
            "material": ("status: ready | blocked", "coverage: complete | incomplete"),
            "image": ("status: ready | blocked", "crop_contract:"),
            "slide": ("group: <group_id>", "refine_rounds:"),
            "review": ("mode: simple_edit | final_review", "final_pixels_inspected: yes | no"),
        }
        for role, markers in required_contracts.items():
            card = SKILL_ROOT / f"subagents/{role}.en.md"
            self.assertTrue(card.is_file(), role)
            text = card.read_text(encoding="utf-8")
            self.assertIn("response_language", text, role)
            self.assertIn("deliverable_language", text, role)
            for marker in markers:
                self.assertIn(marker, text, role)

    def test_boxoverflow_is_diagnostic_not_a_render_process_gate(self):
        tree = ast.parse((SCRIPTS / "render.py").read_text(encoding="utf-8"))
        hard_keys = None
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if any(isinstance(target, ast.Name) and target.id == "_HARD_RENDER_KEYS"
                   for target in node.targets):
                hard_keys = ast.literal_eval(node.value)
                break
        self.assertIsNotNone(hard_keys)
        self.assertNotIn("boxoverflow", hard_keys)
        self.assertIn("overflow", hard_keys)

    def test_screen_copy_and_html_reject_emoji_or_dingbat_icons(self):
        deck = _load_deck_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            (root / "slides").mkdir()
            plan = root / "plan/slide_01.md"
            plan.write_text(
                "# Slide 01 — Demo\n## 最终屏显文案\n- 标题：看一看 👀\n"
                "## 口语讲稿\n讲解。\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Unicode 图标"):
                deck._validate_no_pictographs(root, 1, include_html=False)

            plan.write_text(
                "# Slide 01 — Demo\n## 最终屏显文案\n- 标题：看一看\n"
                "## 口语讲稿\n讲解。\n",
                encoding="utf-8",
            )
            (root / "slides/slide_01.html").write_text(
                "<section class='slide'>✦</section>", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "Unicode 图标"):
                deck._validate_no_pictographs(root, 1, include_html=True)

    def test_raster_plan_requires_explicit_presentation_contract(self):
        deck = _load_deck_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            plan = root / "plan/slide_01.md"
            plan.write_text(
                "# Slide 01 — Demo\n## 视觉实现\n"
                "- medium: generated image\n- image: mascot\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "presentation"):
                deck._validate_image_presentations(root, 1)
            plan.write_text(
                "# Slide 01 — Demo\n## 视觉实现\n"
                "- medium: generated image\n- presentation: subject-only\n"
                "- subject_only: true\n- image: mascot\n",
                encoding="utf-8",
            )
            deck._validate_image_presentations(root, 1)

    def test_no_bitmap_medium_is_not_a_false_positive_raster(self):
        # static-453 slide_06: image_opportunity none + medium "无位图（chart+timeline）"
        # must NOT be treated as a raster page (the 位图 substring false-positive).
        deck = _load_deck_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            (root / "plan/slide_01.md").write_text(
                "# Slide 01\n## 视觉实现\n"
                "- medium: 无位图（chart + timeline）\n"
                "- image_opportunity: none\n- presentation: 无\n",
                encoding="utf-8",
            )
            # Must not raise (no presentation contract required for a no-bitmap page).
            deck._validate_image_presentations(root, 1)
            (root / "plan/slide_01.md").write_text(
                "# Slide 01\n## 视觉实现\n"
                "- medium: no bitmap (chart + timeline)\n"
                "- image_opportunity: none\n",
                encoding="utf-8",
            )
            deck._validate_image_presentations(root, 1)

    def test_bitmap_opportunity_with_layout_presentation_is_rejected(self):
        # real_required + presentation split-media (a layout term) must be rejected.
        deck = _load_deck_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            (root / "plan/slide_01.md").write_text(
                "# Slide 01\n## 视觉实现\n"
                "- medium: real photo\n- image_opportunity: real_required\n"
                "- presentation: split-media\n- asset_id: x\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "presentation"):
                deck._validate_image_presentations(root, 1)
        deck = _load_deck_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "slides").mkdir()
            (root / "renders").mkdir()
            source = root / "slides/slide_01.html"
            png = root / "renders/slide_01.png"
            source.write_text("<section class='slide'>x</section>", encoding="utf-8")
            png.write_bytes(b"pixels")
            record = {
                "page": 1,
                "source": "slides/slide_01.html",
                "png": "renders/slide_01.png",
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "png_sha256": hashlib.sha256(png.read_bytes()).hexdigest(),
                "hard_issues": [],
            }
            (root / "renders/render.json").write_text(
                json.dumps({"schema_version": 2, "pages": {"01": record}}),
                encoding="utf-8",
            )
            deck._validate_render_quality(root, 1)
            record["hard_issues"] = [{"type": "boxoverflow", "count": 1}]
            (root / "renders/render.json").write_text(
                json.dumps({"schema_version": 2, "pages": {"01": record}}),
                encoding="utf-8",
            )
            deck._validate_render_quality(root, 1)
            record["hard_issues"] = [{"type": "overflow", "count": 1}]
            (root / "renders/render.json").write_text(
                json.dumps({"schema_version": 2, "pages": {"01": record}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "overflow"):
                deck._validate_render_quality(root, 1)

    def test_asset_contact_sheet_and_ready_status_contract(self):
        deck = _load_deck_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "assets").mkdir()
            from PIL import Image

            for index, asset_id in enumerate(("cover-hero", "chapter-hero"), 1):
                relative = f"assets/candidate-{index}.png"
                Image.new("RGB", (320 + index * 20, 180), (20 * index, 40, 80)).save(
                    root / relative
                )
                deck._register_asset(
                    root, relative, "generated", generator_model="test-image-model"
                )
                deck._assign_asset(root, relative, asset_id, "cinematic")

            deck._build_asset_contact(root, "cinematic")
            self.assertTrue((root / "assets/contact-sheet-cinematic.png").is_file())
            manifest = json.loads(
                (root / "assets/contact-sheet-cinematic.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                [item["asset_id"] for item in manifest["assets"]],
                ["chapter-hero", "cover-hero"],
            )

            deck._review_assets(
                root,
                "cinematic",
                ready="cover-hero",
                needs_review="chapter-hero",
                rejected=None,
            )
            catalog = json.loads(
                (root / "assets/catalog.json").read_text(encoding="utf-8")
            )
            statuses = {item["asset_id"]: item["status"] for item in catalog["assets"]}
            self.assertEqual(statuses, {
                "cover-hero": "ready",
                "chapter-hero": "needs_review",
            })

    def test_review_contact_sheets_keep_long_decks_readable(self):
        deck = _load_deck_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            renders = root / "renders"
            renders.mkdir()
            from PIL import Image
            for page in range(1, 81):
                Image.new("RGB", (160, 90), (page % 255, 20, 30)).save(
                    renders / f"slide_{page:02d}.png"
                )
            deck._build_contact(root, expected=80)
            manifest = json.loads(
                (renders / "review-contact.json").read_text(encoding="utf-8")
            )["full"]
            self.assertEqual(len(manifest["groups"]), 10)
            self.assertTrue(all(len(group["pages"]) <= 8 for group in manifest["groups"]))

    def test_grouped_slide_keeps_per_page_pixel_loop(self):
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        slide = (SKILL_ROOT / "subagents/slide.md").read_text(encoding="utf-8")
        plan = (SKILL_ROOT / "references/planning-contract.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("原子页优先", skill)
        self.assertIn("普通多页组不得超过 3 页", skill)
        self.assertIn("当前页达到 ready 后才进入下一页", skill)
        self.assertIn("1 轮 hard/semantic repair", skill)
        self.assertIn("1 轮 aesthetic completion", skill)
        self.assertIn("每页总 refine 最多 2 轮", slide)
        self.assertIn("aesthetic_completion_target", slide)
        review = (SKILL_ROOT / "subagents/review.md").read_text(encoding="utf-8")
        self.assertIn("最多只做 1 轮 refine", review)
        self.assertIn("plan/design-brief.md#Style Lock", slide)
        self.assertIn("不能用组末总览替代此前的单页验收", slide)
        self.assertIn("只放工作区内 `_trace/`", slide)
        self.assertNotIn("调试截图放系统临时目录", slide)
        self.assertIn("Style Lock 不是固定页面模板", plan)
        self.assertNotIn("不边写一页边渲一页", slide)

    def test_visual_prior_restoration_is_routed_and_executable(self):
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        skill_en = (SKILL_ROOT / "SKILL.en.md").read_text(encoding="utf-8")
        styles = (SKILL_ROOT / "references/design-styles.md").read_text(encoding="utf-8")
        recipes = (SKILL_ROOT / "references/aesthetic-recipes.md").read_text(encoding="utf-8")
        plan = (SKILL_ROOT / "references/planning-contract.md").read_text(encoding="utf-8")
        checklist = (SKILL_ROOT / "references/quality-checklist.md").read_text(encoding="utf-8")

        for content in (skill, skill_en):
            self.assertIn("aesthetic-recipes.md", content)
            self.assertIn("resolved_system", content)
        self.assertIn("一套完整 `S1–S13 resolved system`", skill)
        self.assertIn("从系统标题一直读到下一系统标题", styles)
        self.assertIn("13 套整机配方", styles)
        self.assertIn("## 1. 编辑级排版配方", recipes)
        self.assertIn("## 2. 四层调色板配方", recipes)
        self.assertIn("design_ambition", recipes)
        self.assertIn("typography_recipe / palette_recipe", plan)
        self.assertIn("学术题材不长成文档截图", checklist)

    def test_attachment_priorities_cannot_be_satisfied_by_speech(self):
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        material = (SKILL_ROOT / "subagents/material.md").read_text(encoding="utf-8")
        material_en = (SKILL_ROOT / "subagents/material.en.md").read_text(encoding="utf-8")
        plan = (SKILL_ROOT / "references/planning-contract.md").read_text(encoding="utf-8")
        slide = (SKILL_ROOT / "subagents/slide.md").read_text(encoding="utf-8")
        review = (SKILL_ROOT / "subagents/review.md").read_text(encoding="utf-8")

        for content in (material, material_en):
            self.assertIn("priority_ledger: complete", content)
            self.assertIn("screen_priority: must_present | supporting | speech_only", content)
        self.assertIn("attachment_priority_ids", plan)
        self.assertIn("讲稿只能解释和展开，不能作为映射终点", skill)
        self.assertIn("讲稿已经解释不能成为删减理由", slide)
        self.assertIn("observed_carrier", review)

    def test_large_svg_is_available_without_disabling_quality_lint(self):
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        layout = (SKILL_ROOT / "references/layout-patterns.md").read_text(encoding="utf-8")
        design = (SKILL_ROOT / "references/design-rules.md").read_text(encoding="utf-8")
        renderer = (SKILL_ROOT / "scripts/render.py").read_text(encoding="utf-8")

        self.assertIn("不禁用大型 SVG", skill)
        for archetype in ("three-layer", "radial", "funnel", "cycle", "pyramid"):
            self.assertIn(f"`{archetype}`", layout)
        self.assertIn("不是 SVG 白名单", layout)
        self.assertIn("SVG 可直接承担大型静态结构主视觉", design)
        self.assertNotIn("SVG-LARGE", renderer)
        self.assertNotIn("CONTROLLED_SVG", renderer)
        self.assertIn("SVG-SMALL", renderer)
        self.assertIn("SVG-LABEL-OVERLAP", renderer)

    def test_paper_figure_contract_is_resolution_preserving_and_subject_only(self):
        material = (SKILL_ROOT / "subagents/material.md").read_text(encoding="utf-8")
        image = (SKILL_ROOT / "subagents/image.md").read_text(encoding="utf-8")
        review = (SKILL_ROOT / "subagents/review.md").read_text(encoding="utf-8")
        deck = (SKILL_ROOT / "scripts/deck.py").read_text(encoding="utf-8")
        staging = (SKILL_ROOT / "scripts/stage_materials.py").read_text(encoding="utf-8")

        self.assertIn("visual_subject_box", material)
        self.assertIn("--source-pdf", image)
        self.assertIn("source_pdf_clip", review)
        self.assertIn("--min-long-edge", deck)
        self.assertIn("--max-body-text-fraction", deck)
        self.assertIn('"body_text_fraction"', deck)
        self.assertIn("_validate_figure_crop_usage", deck)
        self.assertIn("derotation_matrix", deck)
        self.assertIn("ocr_scaled_to_page_raster", deck)
        self.assertIn('"image_size": image_size', staging)

    def test_data_fidelity_checks_source_structure_and_final_chart(self):
        material = (SKILL_ROOT / "subagents/material.md").read_text(encoding="utf-8")
        plan = (SKILL_ROOT / "references/planning-contract.md").read_text(
            encoding="utf-8"
        )
        slide = (SKILL_ROOT / "subagents/slide.md").read_text(encoding="utf-8")
        review = (SKILL_ROOT / "subagents/review.md").read_text(encoding="utf-8")

        self.assertIn("按可见表头重建 `行 × 列`", material)
        self.assertIn("类别 × 系列 × 值", plan)
        self.assertIn("不把一维值数组随意切成多个 series", slide)
        self.assertIn("不能用同一份下游摘要自证", review)
        self.assertIn("计划或 JS 有 7 项、像素只有 4 项", review)

    def test_closing_pages_default_to_optical_center_not_summary_layout(self):
        plan = (SKILL_ROOT / "references/planning-contract.md").read_text(
            encoding="utf-8"
        )
        slide = (SKILL_ROOT / "subagents/slide.md").read_text(encoding="utf-8")
        review = (SKILL_ROOT / "subagents/review.md").read_text(encoding="utf-8")
        css = (SKILL_ROOT / "references/base-template.css").read_text(
            encoding="utf-8"
        )

        self.assertIn("结尾不是“最后一张总结内容页”", plan)
        self.assertIn("水平、垂直光学居中", plan)
        self.assertIn(".slide--cover.slide--closing", slide)
        self.assertIn("三栏总结页", slide)
        self.assertIn("水平、垂直光学居中", review)
        self.assertIn(".slide--closing > .closing-stage", css)
        self.assertIn(".slide--closing .closing-core", css)

    def test_special_page_refinement_requires_visual_regression_check(self):
        plan = (SKILL_ROOT / "references/planning-contract.md").read_text(
            encoding="utf-8"
        )
        slide = (SKILL_ROOT / "subagents/slide.md").read_text(encoding="utf-8")
        review = (SKILL_ROOT / "subagents/review.md").read_text(encoding="utf-8")
        checklist = (SKILL_ROOT / "references/quality-checklist.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("修改前后比较", plan)
        self.assertIn("修改前后", slide)
        self.assertIn("突兀白块/色带", review)
        self.assertIn("特殊页修改不退化", checklist)
        self.assertIn("不按元素种类一刀切", review)
        self.assertIn("单页 Vision", checklist)

    def test_inline_collision_uses_line_fragment_rects(self):
        renderer = (SCRIPTS / "render.py").read_text(encoding="utf-8")
        self.assertIn("range.getClientRects()", renderer)
        self.assertIn("A.el === B.el", renderer)
        self.assertIn("normal line wrap", renderer)

    def test_bbox_overlap_is_visual_signal_not_hard_render_failure(self):
        renderer = (SCRIPTS / "render.py").read_text(encoding="utf-8")
        tree = ast.parse(renderer)
        hard_keys = None
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "_HARD_RENDER_KEYS"
                for target in node.targets
            ):
                hard_keys = ast.literal_eval(node.value)
                break
        self.assertIsNotNone(hard_keys)
        self.assertNotIn("overlap", hard_keys)
        self.assertIn("overflow", hard_keys)
        self.assertNotIn("crowded", hard_keys)
        self.assertNotIn("cjkTypography", hard_keys)
        self.assertIn("ORNAMENTAL_PUNCT", renderer)

        slide = (SKILL_ROOT / "subagents/slide.md").read_text(encoding="utf-8")
        self.assertIn("不得同回合读取 `render-issues.json`", slide)
        self.assertIn("记为 checker mismatch 并保留原页", slide)
        review = (SKILL_ROOT / "subagents/review.md").read_text(encoding="utf-8")
        self.assertIn("首次 Vision 返回前", review)
        self.assertIn("再用 DOM/bbox 诊断补充定位", review)

    def test_background_system_keeps_cross_group_continuity(self):
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        plan = (SKILL_ROOT / "references/planning-contract.md").read_text(
            encoding="utf-8"
        )
        slide = (SKILL_ROOT / "subagents/slide.md").read_text(encoding="utf-8")
        review = (SKILL_ROOT / "subagents/review.md").read_text(encoding="utf-8")
        checklist = (SKILL_ROOT / "references/quality-checklist.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("base_canvas_family", skill)
        self.assertIn("克制秩序", skill)
        self.assertIn("氛围表达", skill)
        self.assertIn("局部光晕", skill)
        self.assertIn("由 Image 统一生成的背景", skill)
        self.assertIn("boundary_handoff", skill)
        self.assertIn("enter_from / exit_to", plan)
        self.assertIn("表达型场景不能无理由", plan)
        self.assertIn("普通内容页默认停留", slide)
        self.assertIn("不能在每个标题后复制通用圆形 glow", slide)
        self.assertIn("上一章末页 → divider → 新章首两页", review)
        self.assertIn("不要因此机械给每页补 glow", review)
        self.assertIn("画布家族连续", checklist)

    def test_orchestrator_preflights_group_affinity_and_deck_rhythm(self):
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        plan = (SKILL_ROOT / "references/planning-contract.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("普通同构页按小组", skill)
        self.assertIn("相邻、同章、同白底", plan)
        self.assertIn("不构成合组理由", plan)
        self.assertIn("Repetition & rhythm preflight", skill)
        self.assertIn("motif_role", plan)
        self.assertIn("母题有主次与缺席", plan)
        self.assertIn("参考文献与结尾必须分开", plan)
        self.assertIn("不创建新脚本或额外计划文件", plan)

    def test_plan_contract_declares_image_presentation_and_no_emoji_copy(self):
        plan = (SKILL_ROOT / "references/planning-contract.md").read_text(
            encoding="utf-8"
        )
        slide = (SKILL_ROOT / "subagents/slide.md").read_text(encoding="utf-8")
        review = (SKILL_ROOT / "subagents/review.md").read_text(encoding="utf-8")
        self.assertIn("presentation", plan)
        self.assertIn("`subject-only`", plan)
        self.assertIn("emoji 或 Unicode 图标", plan)
        self.assertIn("dense` 页若主信息只在半张画布", slide)
        self.assertIn("带白底/奶油底的 RGB 矩形", review)

    def test_removed_entrypoints_stay_consolidated(self):
        removed = {
            "build_player.py",
            "build_review_contact.py",
            "sync_speech.py",
            "render_batch.py",
            "parse_materials.py",
            "parse_one_file.py",
            "rasterize_pdf.py",
        }
        self.assertFalse(removed.intersection(path.name for path in SCRIPTS.iterdir()))
        self.assertEqual(
            {
                "deck.py",
                "font_bundle.py",
                "image_cutout.py",
                "render.py",
                "stage_materials.py",
            },
            {path.name for path in SCRIPTS.glob("*.py")},
        )

    def test_material_asset_registration_is_explicit_and_build_visible(self):
        deck = _load_deck_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "assets").mkdir()
            (root / "slides").mkdir()
            (root / "materials/_raw").mkdir(parents=True)
            source = root / "materials/_raw/brief.png"
            source.write_bytes(b"test-image")
            deck._register_asset(
                root,
                "assets/attachment-photo.png",
                "material",
                source_path="materials/_raw/brief.png",
            )
            self.assertEqual(
                (root / "assets/attachment-photo.png").read_bytes(), b"test-image"
            )
            (root / "slides/slide_01.html").write_text(
                '<img src="assets/attachment-photo.png">', encoding="utf-8"
            )
            deck._validate_referenced_assets(root)
            catalog = __import__("json").loads(
                (root / "assets/catalog.json").read_text(encoding="utf-8")
            )
            self.assertEqual(catalog["assets"][0]["origin"], "material")
            self.assertEqual(
                catalog["assets"][0]["source_path"], "materials/_raw/brief.png"
            )

    def test_page_facsimile_requires_auditable_justification(self):
        deck = _load_deck_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "materials/_work/material_01/_raw/paper.pdf_pages/p001.png"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"page pixels")
            with self.assertRaisesRegex(ValueError, "facsimile-justification"):
                deck._register_asset(
                    root,
                    "assets/page.png",
                    "material",
                    source_path=source.relative_to(root).as_posix(),
                    material_asset_type="page-facsimile",
                )

            justification = "该页是监管机构签发的原始扫描公文，页章、签名与完整页面关系本身构成证据。"
            deck._register_asset(
                root,
                "assets/page.png",
                "material",
                source_path=source.relative_to(root).as_posix(),
                material_asset_type="page-facsimile",
                facsimile_justification=justification,
            )
            catalog = json.loads((root / "assets/catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(catalog["assets"][0]["facsimile_justification"], justification)

    def test_referenced_facsimile_is_rechecked_at_delivery(self):
        deck = _load_deck_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "assets").mkdir()
            (root / "slides").mkdir()
            (root / "assets/page.png").write_bytes(b"page pixels")
            (root / "slides/slide_01.html").write_text(
                '<img src="assets/page.png">', encoding="utf-8"
            )
            (root / "assets/catalog.json").write_text(json.dumps({
                "schema_version": 2,
                "assets": [{
                    "path": "assets/page.png",
                    "origin": "material",
                    "material_asset_type": "page-facsimile",
                    "source_path": "materials/_raw/paper.pdf_pages/p001.png",
                }],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "lack a >=20 character justification"):
                deck._validate_referenced_assets(root)

    def test_referenced_paper_figure_is_rechecked_at_delivery(self):
        deck = _load_deck_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "assets").mkdir()
            (root / "slides").mkdir()
            (root / "materials/_raw").mkdir(parents=True)
            (root / "assets/figure.png").write_bytes(b"figure pixels")
            (root / "materials/_raw/paper.pdf").write_bytes(b"pdf")
            (root / "slides/slide_01.html").write_text(
                '<img src="assets/figure.png">', encoding="utf-8"
            )
            entry = {
                "path": "assets/figure.png",
                "origin": "derived",
                "material_asset_type": "figure_crop",
                "derivative_kind": "material_figure_crop",
                "source_pdf": "materials/_raw/paper.pdf",
                "render_source": "page_raster",
                "pixel_size": [1800, 900],
                "body_text_fraction": 0.02,
                "page_fraction": 0.40,
                "status": "ready",
            }
            (root / "assets/catalog.json").write_text(json.dumps({
                "schema_version": 2, "assets": [entry],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "render_source"):
                deck._validate_referenced_assets(root)

            entry["render_source"] = "source_pdf_clip"
            (root / "assets/catalog.json").write_text(json.dumps({
                "schema_version": 2, "assets": [entry],
            }), encoding="utf-8")
            deck._validate_referenced_assets(root)

    def test_bitmap_exception_with_attachment_visual_is_a_delivery_warning(self):
        deck = _load_deck_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            (root / "materials/_work/material_01").mkdir(parents=True)
            (root / "plan/image-strategy.json").write_text(json.dumps({
                "status": "bitmap_exception",
                "visible_subject_scan_complete": True,
            }), encoding="utf-8")
            (root / "materials/_work/material_01/catalog.json").write_text(json.dumps([{
                "name": "paper.pdf · p1",
                "kind": "image",
                "status": "ok",
                "material_asset_type": "page_context",
            }]), encoding="utf-8")
            warnings = deck._validate_bitmap_exception_attachment_bias(root)
            self.assertEqual(warnings[0]["code"], "bitmap_exception_with_attachment_visuals")
            deck._write_delivery_warnings(root, warnings)
            report = json.loads(
                (root / "_trace/delivery-warnings.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(report["warnings"]), 1)

    def test_unregistered_raster_asset_is_rejected(self):
        deck = _load_deck_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "assets").mkdir()
            (root / "slides").mkdir()
            (root / "assets/untracked.png").write_bytes(b"test-image")
            (root / "slides/slide_01.html").write_text(
                '<img src="assets/untracked.png">', encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "lack provenance"):
                deck._validate_referenced_assets(root)

    def test_font_allowlist_covers_distinct_handwriting_roles(self):
        bundle = _load_font_bundle_module()
        bundle._validate_font_allowlist()
        for family in (
            "Xiaolai",
            "LXGW WenKai",
            "Ma Shan Zheng",
            "Zhi Mang Xing",
            "Dancing Script",
            "Kalam",
            "Shadows Into Light",
            "Sacramento",
        ):
            self.assertIn(family, bundle.FAMILY_FACES)
            self.assertEqual(bundle.FONT_LICENSES[family]["license"], "OFL-1.1")
        self.assertEqual(bundle.TOKEN_FALLBACKS["--font-kai"], "Xiaolai")
        self.assertEqual(bundle.TOKEN_FALLBACKS["--font-write"], "Xiaolai")
        self.assertEqual(bundle.TOKEN_FALLBACKS["--font-jotter"], "Xiaolai")

    def test_material_text_is_losslessly_chunked(self):
        stage = _load_stage_materials_module()
        text = "甲" * 25001
        with tempfile.TemporaryDirectory() as temporary:
            record = stage._write_text_outputs(text, temporary, "lecture.txt")
            chunks = record["text_chunks"]
            rebuilt = "".join(
                Path(item["path"]).read_text(encoding="utf-8") for item in chunks
            )
        self.assertEqual(len(chunks), 3)
        self.assertEqual(sum(item["chars"] for item in chunks), len(text))
        self.assertEqual(rebuilt, text)

    def test_scanned_pdf_ocr_persists_every_page_and_full_text(self):
        stage = _load_stage_materials_module()

        class FakeRapidOCR:
            def __call__(self, page_path):
                page = int(Path(page_path).stem[-3:])
                return ([
                    [[[100, 40], [180, 40], [180, 60], [100, 60]], f"第二列-{page}", 0.98],
                    [[[10, 10], [90, 10], [90, 30], [10, 30]], f"标题-{page}", 0.99],
                ], {"elapsed": 0.01})

        fake_module = types.SimpleNamespace(RapidOCR=FakeRapidOCR)
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            sys.modules, {"rapidocr_onnxruntime": fake_module}
        ):
            root = Path(temporary)
            pages = []
            for page in (1, 2):
                path = root / f"p{page:03d}.png"
                path.write_bytes(b"pixels")
                pages.append(str(path))
            result = stage._ocr_pages_worker(pages, str(root / "ocr"), "paper.pdf")
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["covered"], 2)
            fulltext = Path(result["fulltext"]).read_text(encoding="utf-8")
            self.assertIn("标题-1", fulltext)
            self.assertIn("第二列-2", fulltext)
            coverage = json.loads(
                Path(result["coverage_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(coverage["coverage"], {
                "status": "complete", "unit": "pages", "covered": 2, "total": 2,
            })
            self.assertTrue(all(Path(item["json_path"]).is_file() for item in result["pages"]))

    def test_mixed_pdf_native_text_coverage_exposes_pages_needing_ocr(self):
        import fitz

        stage = _load_stage_materials_module()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "mixed.pdf"
            document = fitz.open()
            text_page = document.new_page()
            text_page.insert_text((72, 72), "This page contains a native searchable text layer.")
            document.new_page()
            document.save(path)
            document.close()
            coverage = stage._pdf_text_coverage(str(path))
            self.assertEqual(coverage["status"], "incomplete")
            self.assertEqual(coverage["covered"], 1)
            self.assertEqual(coverage["total"], 2)
            self.assertEqual(coverage["pages"][1]["status"], "needs_ocr")

    def test_material_native_text_matrix_does_not_need_markitdown(self):
        stage = _load_stage_materials_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, content in {
                "brief.markdown": "# Brief\n\n完整内容",
                "metrics.tsv": "name\tvalue\nreach\t1200000\n",
                "data.json": '{"audience":"Gen Z"}',
                "config.yaml": "theme: y2k\n",
            }.items():
                path = root / name
                path.write_text(content, encoding="utf-8")
                record = stage._parse_one(str(path))
                self.assertEqual(record["status"], "ok", name)
                self.assertIn(content.strip().splitlines()[-1], record["content"])

    def test_material_pptx_fallback_extracts_text_and_embedded_media(self):
        stage = _load_stage_materials_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "brief.pptx"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr(
                    "ppt/slides/slide1.xml",
                    '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                    '<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>招商主张</a:t>'
                    '</a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>',
                )
                archive.writestr("ppt/media/image1.png", b"not-empty-image-bytes")
            with mock.patch.object(stage, "_normalize_worker_init", return_value=False):
                self.assertIn("招商主张", stage._pptx_fallback(str(source)))
            media = stage._embedded_media(str(source), str(root / "media"), "brief.pptx")
            self.assertEqual(len(media), 1)
            self.assertEqual(media[0]["from_document"], "brief.pptx")

    def test_material_unknown_and_archive_do_not_claim_complete_semantics(self):
        stage = _load_stage_materials_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unknown = root / "material.bin"
            unknown.write_bytes(b"binary")
            record = stage._parse_one(str(unknown))
            self.assertEqual(record["status"], "unsupported")
            self.assertTrue(record["suggested_actions"])

            archive_path = root / "bundle.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("notes/readme.md", "content")
            record = stage._parse_one(str(archive_path))
            self.assertEqual(record["status"], "ok")
            self.assertEqual(record["semantic_coverage"], "incomplete")

    def test_deck_sync_subcommand(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            (root / "plan/deck.md").write_text("# Deck\n- language: zh\n", encoding="utf-8")
            for number, title in ((1, "开场"), (2, "结尾")):
                (root / f"plan/slide_{number:02d}.md").write_text(
                    f"# Slide {number:02d}\n- 标题：{title}\n\n"
                    f"## 口语讲稿\n\n第 {number} 页讲稿。\n\n## 来源\n\n- none\n",
                    encoding="utf-8",
                )
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "deck.py"), "sync", str(root), "--expected", "2"],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            speech = (root / "speech.md").read_text(encoding="utf-8")
            self.assertIn("# 第 01 页｜开场", speech)
            self.assertIn("# 第 02 页｜结尾", speech)

    def test_deck_prepare_subcommand_is_exposed(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "deck.py"), "--help"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("prepare", result.stdout)

    def test_deck_prepare_syncs_then_bundles_from_plans(self):
        deck = _load_deck_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            (root / "plan/deck.md").write_text("# Deck\n- language: zh\n", encoding="utf-8")
            (root / "plan/slide_01.md").write_text(
                "# Slide 01 — 开场\n- 标题：开场\n\n## 口语讲稿\n\n开场讲稿。\n\n## 来源\n\n- none\n",
                encoding="utf-8",
            )
            with mock.patch.object(deck, "bundle_workspace", return_value={"faces": [{"family": "Deck Sans"}]}) as bundle:
                deck._prepare_workspace(root, 1)
            self.assertTrue((root / "speech.md").is_file())
            self.assertTrue((root / "assets/vendor/echarts.min.js").is_file())
            css = (root / "base.css").read_text(encoding="utf-8")
            self.assertIn("deck-runtime-canvas-reset", css)
            self.assertIn("html, body { margin: 0", css)
            bundle.assert_called_once_with(root, from_plans=True)

    def test_canvas_reset_is_idempotent_for_custom_base_css(self):
        deck = _load_deck_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "base.css").write_text(
                ":root{--bg:#111}.slide{width:1600px;height:900px}", encoding="utf-8"
            )
            deck._ensure_canvas_reset(root)
            deck._ensure_canvas_reset(root)
            css = (root / "base.css").read_text(encoding="utf-8")
            self.assertEqual(css.count("deck-runtime-canvas-reset"), 1)
            self.assertTrue(css.startswith("/* deck-runtime-canvas-reset */"))
            self.assertIn("--canvas-w: 1600px", css)
            self.assertIn("--canvas-h: 900px", css)

    def test_canvas_detection_prefers_explicit_canvas_variables(self):
        deck = _load_deck_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "base.css").write_text(
                ":root{--canvas-w:900px;--canvas-h:1600px;--w:1600px;--h:900px}",
                encoding="utf-8",
            )
            self.assertEqual(deck._detect_canvas(root), (900, 1600))

    def test_canvas_reset_migrates_old_marker_without_canvas_variables(self):
        deck = _load_deck_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "base.css").write_text(
                "/* deck-runtime-canvas-reset */\n"
                ".slide{width:var(--canvas-w);height:var(--canvas-h)}",
                encoding="utf-8",
            )
            deck._ensure_canvas_reset(root)
            css = (root / "base.css").read_text(encoding="utf-8")
            self.assertEqual(css.count("deck-runtime-canvas-reset"), 1)
            self.assertIn(":root { --canvas-w: 1600px; --canvas-h: 900px; }", css)

    def test_echarts_is_normalized_to_portable_deck_asset(self):
        deck = _load_deck_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "slides").mkdir()
            slide = root / "slides/slide_01.html"
            slide.write_text(
                '<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>',
                encoding="utf-8",
            )
            deck._ensure_runtime_assets(root)
            deck._normalize_runtime_references(root)
            deck._validate_runtime_dependencies(root, expected=1)
            self.assertIn(
                '../assets/vendor/echarts.min.js', slide.read_text(encoding="utf-8")
            )

    def test_missing_local_script_is_a_delivery_error(self):
        deck = _load_deck_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "slides").mkdir()
            (root / "slides/slide_01.html").write_text(
                '<script src="../assets/vendor/missing.js"></script>', encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "portable runtime dependency audit failed"):
                deck._validate_runtime_dependencies(root, expected=1)

    def test_percent_encoded_svg_fragment_is_not_a_local_file_dependency(self):
        deck = _load_deck_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "slides").mkdir()
            (root / "slides/slide_01.html").write_text(
                '<svg><defs><filter id="n"></filter></defs>'
                '<rect style="filter:url(%23n)"></rect></svg>',
                encoding="utf-8",
            )
            (root / "base.css").write_text(
                '.texture{filter:url(%23n)}', encoding="utf-8"
            )
            deck._validate_runtime_dependencies(root, expected=1)

    def test_speech_sync_removes_markdown_fences_and_internal_sources(self):
        deck = _load_deck_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            (root / "plan/deck.md").write_text(
                "# Deck\n- language: zh\n", encoding="utf-8"
            )
            (root / "plan/slide_01.md").write_text(
                "# Slide 01 — 开场\n\n## 口语讲稿\n\n"
                "```text\n讲稿内容\n这是一段可以直接朗读的讲稿。\n```\n\n"
                "## 来源\n\n- plan/grounded-knowledge.md（编排器假设）\n"
                "- https://example.com/source\n",
                encoding="utf-8",
            )
            deck._sync_speech(root, 1)
            speech = (root / "speech.md").read_text(encoding="utf-8")
            self.assertIn("这是一段可以直接朗读的讲稿。", speech)
            self.assertIn("https://example.com/source", speech)
            self.assertNotIn("```", speech)
            self.assertNotIn("讲稿内容", speech)
            self.assertNotIn("grounded-knowledge", speech)
            self.assertNotIn("编排器假设", speech)

    def test_renderer_must_not_fallback_to_skill_echarts_for_missing_deck_asset(self):
        render = (SCRIPTS / "render.py").read_text(encoding="utf-8")
        self.assertNotIn(
            'os.path.dirname(os.path.abspath(__file__)), "..", "assets", "vendor", "echarts.min.js"',
            render,
        )
        self.assertIn('"charts_missing"', render)
        self.assertIn("机检结论: 不过", render)
        self.assertIn("--audit-player", render)
        self.assertIn("canonical player runtime audit failed", render)

    def test_skill_sources_do_not_leak_runtime_framework_terms(self):
        skill = REPO / "skills/mural-presenter"
        leaked = []
        for path in skill.rglob("*"):
            if path.is_file() and path.suffix in {".md", ".py", ".sh", ".yaml", ".css"}:
                if "harness" in path.read_text(encoding="utf-8", errors="ignore").lower():
                    leaked.append(path.relative_to(skill).as_posix())
        self.assertEqual([], leaked)

    def test_font_delivery_allowlist_requires_official_ofl_sources(self):
        fonts = _load_font_bundle_module()
        fonts._validate_font_allowlist()
        self.assertEqual(set(fonts.FAMILY_FACES), set(fonts.FONT_LICENSES))
        self.assertTrue(all(item["license"] == "OFL-1.1" for item in fonts.FONT_LICENSES.values()))
        self.assertTrue(all(item["source"].startswith("https://github.com/") for item in fonts.FONT_LICENSES.values()))
        self.assertTrue(fonts.OFL_TEMPLATE.is_file())

    def test_noto_sans_bundle_keeps_real_variable_weight_axis(self):
        fonts = _load_font_bundle_module()
        variable = REPO / "fonts/NotoSansSC.ttf"
        static = REPO / "fonts/IBMPlexMono-Regular.ttf"
        face = fonts.FAMILY_FACES["Noto Sans SC"][0]
        self.assertEqual("100 900", face.weight)
        self.assertTrue(fonts._font_supports_face(variable, face))
        self.assertFalse(fonts._font_supports_face(static, face))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            (root / "base.css").write_text(
                ':root{--font-sans:"Noto Sans SC",sans-serif;'
                '--font-body:var(--font-sans);--font-title:var(--font-sans);'
                '--font-display:var(--font-sans);--font-number:var(--font-sans);'
                '--font-mono:var(--font-sans);}',
                encoding="utf-8",
            )
            (root / "plan/slide_01.md").write_text(
                "# Slide 01\n## 最终屏显文案\n中文标题 Font 2026\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                __import__("os").environ,
                {"PPT_FONT_SOURCE_DIRS": str(REPO / "fonts")},
            ):
                manifest = fonts.bundle_workspace(root, from_plans=True)

            noto_faces = [
                item for item in manifest["faces"]
                if item["source_family"] == "Noto Sans SC"
            ]
            self.assertEqual(["100 900"], [item["weight"] for item in noto_faces])
            from fontTools.ttLib import TTFont

            bundled = TTFont(root / noto_faces[0]["path"], lazy=False)
            try:
                axes = {axis.axisTag: axis for axis in bundled["fvar"].axes}
                self.assertEqual((100.0, 900.0), (axes["wght"].minValue, axes["wght"].maxValue))
            finally:
                bundled.close()

    def test_smiley_sans_static_face_is_accepted_and_bundled(self):
        fonts = _load_font_bundle_module()
        source = REPO / "fonts/SmileySans-Oblique.ttf"
        face = fonts.FAMILY_FACES["Smiley Sans"][0]
        self.assertEqual("400", face.weight)
        self.assertEqual("oblique", face.style)
        self.assertTrue(fonts._font_supports_face(source, face))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            (root / "base.css").write_text(
                ':root{--font-sans:"Noto Sans SC",sans-serif;'
                '--font-body:var(--font-sans);'
                '--font-title:"Smiley Sans","Noto Sans SC",sans-serif;'
                '--font-display:var(--font-title);--font-number:var(--font-title);'
                '--font-mono:"IBM Plex Mono","Noto Sans SC",monospace;}',
                encoding="utf-8",
            )
            (root / "plan/slide_01.md").write_text(
                "# Slide 01\n## 最终屏显文案\n得意黑 Smiley 2026\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                __import__("os").environ,
                {"PPT_FONT_SOURCE_DIRS": str(REPO / "fonts")},
            ):
                manifest = fonts.bundle_workspace(root, from_plans=True)

            smiley_faces = [
                item for item in manifest["faces"]
                if item["source_family"] == "Smiley Sans"
            ]
            self.assertEqual(1, len(smiley_faces))
            self.assertEqual("400", smiley_faces[0]["weight"])
            self.assertEqual("oblique", smiley_faces[0]["style"])
            self.assertTrue((root / smiley_faces[0]["path"]).is_file())

    def test_default_presentation_title_route_prefers_smiley_and_keeps_body_noto(self):
        template = (REPO / "skills/mural-presenter/references/base-template.css").read_text(encoding="utf-8")
        fonts = (REPO / "skills/mural-presenter/references/fonts.md").read_text(encoding="utf-8")
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        skill_en = (SKILL_ROOT / "SKILL.en.md").read_text(encoding="utf-8")
        render = (REPO / "skills/mural-presenter/scripts/render.py").read_text(encoding="utf-8")

        self.assertIn('--font-title: "Smiley Sans", "Noto Sans SC", sans-serif;', template)
        self.assertIn('--font-hei-heavy:     "Smiley Sans", "Noto Sans SC", sans-serif;', template)
        self.assertIn('--font-body:  var(--font-sans);', template)
        self.assertIn('--fs-display: 104px;', template)
        self.assertIn('--fs-title:   60px;', template)
        for content in (fonts, skill, skill_en):
            self.assertIn("Smiley Sans", content)
        for field in ("title_voice", "title_scale", "title_treatment", "body_voice", "numeric_voice", "font_roles"):
            self.assertIn(field, skill)
        self.assertIn("approvedPresentationTitle", render)
        self.assertIn("fs>=40", render)

    def test_user_font_config_is_hash_checked_and_mapped_to_roles(self):
        fonts = _load_font_bundle_module()
        candidates = list((Path.home() / ".fonts").glob("NotoSansSC*.ttf"))
        if not candidates:
            candidates = list((Path.home() / ".fonts").glob("Archivo*.ttf"))
        if not candidates:
            self.skipTest("no readable test font installed")
        source = candidates[0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "materials/_fonts/UserFont.ttf"
            target.parent.mkdir(parents=True)
            target.write_bytes(source.read_bytes())
            digest = __import__("hashlib").sha256(target.read_bytes()).hexdigest()
            (root / "materials/font-config.json").write_text(
                __import__("json").dumps({
                    "license_acknowledged": True,
                    "roles": {"title": {"kind": "custom", "font_id": "font-01"}},
                    "fonts": [{
                        "id": "font-01", "source_path": "materials/_fonts/UserFont.ttf",
                        "sha256": digest, "weight": 400, "subfamily": "Regular",
                    }],
                }), encoding="utf-8",
            )
            registry, roles = fonts._load_custom_config(root)
        self.assertIn("User::font-01", registry)
        self.assertEqual(roles["--font-title"], "User::font-01")
        self.assertEqual(roles["--font-display"], "User::font-01")

    def test_user_font_is_subset_and_keeps_portable_fallback(self):
        fonts = _load_font_bundle_module()
        candidates = list((Path.home() / ".fonts").glob("Archivo*.ttf"))
        if not candidates:
            self.skipTest("no readable custom test font installed")
        source = candidates[0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "materials/_fonts").mkdir(parents=True)
            (root / "plan").mkdir()
            target = root / "materials/_fonts/Brand.ttf"
            target.write_bytes(source.read_bytes())
            digest = __import__("hashlib").sha256(target.read_bytes()).hexdigest()
            (root / "materials/font-config.json").write_text(
                __import__("json").dumps({
                    "license_acknowledged": True,
                    "roles": {"title": {"kind": "custom", "font_id": "font-01"}},
                    "fonts": [{
                        "id": "font-01", "source_path": "materials/_fonts/Brand.ttf",
                        "sha256": digest, "weight": 400, "subfamily": "Regular",
                    }],
                }), encoding="utf-8",
            )
            (root / "base.css").write_text(
                ':root{--font-title:"Noto Serif SC",serif;--font-body:"Noto Sans SC",sans-serif;}',
                encoding="utf-8",
            )
            (root / "plan/slide_01.md").write_text(
                "# Slide 01\n## 最终屏显文案\n中文标题 Brand 2026\n", encoding="utf-8"
            )
            manifest = fonts.bundle_workspace(root, from_plans=True)
            css = (root / "base.css").read_text(encoding="utf-8")
        custom = [item for item in manifest["faces"] if item["license"] == "user-provided"]
        self.assertTrue(custom)
        self.assertIn("USER-FONTS.txt", custom[0]["license_file"])
        self.assertRegex(css, r'--font-title: "Deck-[^"]+", "Deck-[^"]+", sans-serif;')

    def test_font_template_excludes_unapproved_legacy_names(self):
        content = (REPO / "skills/mural-presenter/references/base-template.css").read_text(encoding="utf-8")
        content += (REPO / "skills/mural-presenter/references/fonts.md").read_text(encoding="utf-8")
        for name in ("段宁硬笔楷", "叶根友钢笔行书", "迷你简硬笔行书", "Lovely Little Jelly", "今年也要加油鸭"):
            self.assertNotIn(name, content)
        for name in ("Liu Jian Mao Cao", "Long Cang", "Zhi Mang Xing", "ZCOOL QingKe HuangYou"):
            self.assertIn(name, content)

    def test_screen_copy_firewall_and_special_page_metadata_are_consistent(self):
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        plan = (SKILL_ROOT / "references/planning-contract.md").read_text(encoding="utf-8")
        design = (SKILL_ROOT / "references/design-rules.md").read_text(encoding="utf-8")
        layout = (SKILL_ROOT / "references/layout-patterns.md").read_text(encoding="utf-8")
        checklist = (SKILL_ROOT / "references/quality-checklist.md").read_text(encoding="utf-8")
        slide = (SKILL_ROOT / "subagents/slide.md").read_text(encoding="utf-8")
        review = (SKILL_ROOT / "subagents/review.md").read_text(encoding="utf-8")

        self.assertIn("screen-copy firewall", skill)
        self.assertIn("观众价值检查", plan)
        self.assertIn("内部生产信息", slide)
        self.assertIn("观众价值审计", review)
        self.assertIn("封面不素，但不设固定层数", design)
        self.assertIn("辅助文字有观众价值且不重复", checklist)
        self.assertIn("不得为了显得高级而编造", layout)
        self.assertNotIn("再叠 **2–3 个设计层**", design)
        self.assertNotIn("封面在主视觉 + 巨标题外有 2–3 个设计层", checklist)

    def test_content_sufficiency_and_visible_copy_uniqueness_are_consistent(self):
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        plan = (SKILL_ROOT / "references/planning-contract.md").read_text(encoding="utf-8")
        design = (SKILL_ROOT / "references/design-rules.md").read_text(encoding="utf-8")
        checklist = (SKILL_ROOT / "references/quality-checklist.md").read_text(encoding="utf-8")
        slide = (SKILL_ROOT / "subagents/slide.md").read_text(encoding="utf-8")
        review = (SKILL_ROOT / "subagents/review.md").read_text(encoding="utf-8")

        self.assertIn("内容充分性与屏显语义去重", skill)
        self.assertIn("内容充分性检查", plan)
        self.assertIn("图片角标、badge、callout", plan)
        self.assertIn("有效密度先补语义", design)
        self.assertIn("可见文案清点", slide)
        self.assertIn("内容充分性与屏显语义去重", review)
        self.assertIn("内容充分且屏显语义唯一", checklist)
        self.assertIn("章节脚本不机械复制", checklist)

    def test_skill_contracts_consistent_zh_and_en(self):
        # Item D: guard the skill-contract fixes against silent regression, in
        # BOTH the canonical zh skill and its en counterpart.
        variants = {
            "zh": REPO / "skills/mural-presenter",
            "en": REPO / "skills/mural-presenter",
        }
        for lang, root in variants.items():
            slide = (root / "subagents/slide.md").read_text(encoding="utf-8")
            plan = (root / "references/planning-contract.md").read_text(encoding="utf-8")
            image = (root / "subagents/image.md").read_text(encoding="utf-8")
            material = (root / "subagents/material.md").read_text(encoding="utf-8")
            base_css = (root / "references/base-template.css").read_text(encoding="utf-8")
            # D1: transition/section pages use standard .slide--section; no .slide--divider.
            self.assertIn(".slide--section", slide, lang)
            self.assertIn(".slide--divider", slide, lang)   # named as forbidden
            self.assertIn(".slide--divider", base_css, lang)  # forbidden in css comment too
            # D2: image_opportunity enum and reason are two separate fields.
            self.assertIn("image_opportunity_reason", plan, lang)
            # D3: no unconditional transparent_assets placeholder in the image template.
            self.assertNotIn("transparent_assets: assets/<name>-cutout.png | not-required", image)
            self.assertNotIn("transparent_assets: assets/<name>-cutout.png, assets/<name>-cutout.png | not-required", image)
            # D4: Material terminal is ready|blocked only (no partial in the contract line).
            self.assertIn("status: ready | blocked", material, lang)
            self.assertNotIn("status: ready | partial | blocked", material)
            # round-8: presentation is the bitmap render contract, four enums only;
            # layout terms (split-media) are called out as NOT presentation, and a
            # no-bitmap page omits presentation.
            skill = (root / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("split-media", plan, lang)
            self.assertIn("split-media", skill, lang)
            for enum in ("subject-only", "framed-scene", "full-bleed", "evidence-crop"):
                self.assertIn(enum, plan, f"{lang}:{enum}")


if __name__ == "__main__":
    unittest.main()
