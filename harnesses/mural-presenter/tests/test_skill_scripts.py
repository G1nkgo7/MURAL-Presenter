import subprocess
import sys
import tempfile
import unittest
import zipfile
import hashlib
import json
import ast
from importlib import util
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "skills/mural-presenter/scripts"


def _load_deck_module():
    sys.path.insert(0, str(SCRIPTS))
    spec = util.spec_from_file_location("mural_presenter_deck", SCRIPTS / "deck.py")
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_bundle_fonts_module():
    sys.path.insert(0, str(SCRIPTS))
    spec = util.spec_from_file_location("mural_presenter_bundle_fonts", SCRIPTS / "bundle_fonts.py")
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
    def test_asset_download_is_batched_and_records_provenance(self):
        from io import BytesIO
        from PIL import Image

        deck = _load_deck_module()
        image = BytesIO()
        Image.new("RGB", (24, 16), "#c96a45").save(image, format="PNG")

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return image.getvalue()

        urls = [
            "https://images.example.test/hero-a.png",
            "https://images.example.test/hero-b.png",
        ]
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            deck, "urlopen", side_effect=lambda *_args, **_kwargs: Response()
        ):
            root = Path(temporary)
            deck._download_assets(root, urls)
            catalog = json.loads(
                (root / "assets/catalog.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(catalog["assets"]), 2)
            self.assertEqual(
                {item["source_url"] for item in catalog["assets"]}, set(urls)
            )
            self.assertTrue(all(
                item["origin"] == "downloaded"
                and (root / item["path"]).is_file()
                for item in catalog["assets"]
            ))

    def test_preflight_checks_required_runtime_before_production(self):
        deck = _load_deck_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(
                deck, "_preflight_fonts",
                return_value={"available": 8, "declared": 12, "custom": 1},
            ), mock.patch.object(deck, "_probe_browser_runtime", return_value="cached"):
                summary = deck._preflight_workspace(root)
        self.assertEqual("cached", summary["browser"])
        self.assertEqual(1, summary["fonts"]["custom"])
        self.assertEqual(0, summary["attachments"])
        self.assertEqual("all", summary["scope"])

    def test_workspace_preflight_does_not_repeat_environment_probe(self):
        deck = _load_deck_module()
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            deck, "_preflight_fonts", side_effect=AssertionError("must not run")
        ), mock.patch.object(
            deck, "_probe_browser_runtime", side_effect=AssertionError("must not run")
        ):
            summary = deck._preflight_workspace(Path(temporary), "workspace")
        self.assertEqual("workspace", summary["scope"])
        self.assertEqual("skipped", summary["browser"])

    def test_environment_preflight_does_not_validate_sample_attachments(self):
        deck = _load_deck_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "materials").mkdir()
            (root / "materials/attachments.json").write_text(
                json.dumps({"attachments": [{"name": "missing.pdf", "status": "missing"}]}),
                encoding="utf-8",
            )
            with mock.patch.object(
                deck, "_preflight_fonts",
                return_value={"available": 8, "declared": 12, "custom": 0},
            ), mock.patch.object(deck, "_probe_browser_runtime", return_value="cached"):
                summary = deck._preflight_workspace(root, "environment")
        self.assertEqual("environment", summary["scope"])
        self.assertEqual(0, summary["attachments"])

    def test_font_source_resolution_skips_unreadable_earlier_candidate(self):
        fonts = _load_bundle_fonts_module()
        face = fonts.Face(("NotoSansSC.ttf",), "400")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            (first / "NotoSansSC.ttf").write_bytes(b"bad")
            expected = second / "NotoSansSC.ttf"
            expected.write_bytes(b"good")
            with mock.patch.object(
                fonts,
                "_font_source_readable",
                side_effect=lambda path: Path(path) == expected,
            ):
                selected = fonts._find_source(face, [first, second])
        self.assertEqual(expected, selected)

    def test_font_source_dirs_include_one_bounded_family_level(self):
        fonts = _load_bundle_fonts_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "noto-sc"
            nested.mkdir()
            with mock.patch.dict(
                fonts.os.environ,
                {"PPT_FONT_SOURCE_DIRS": str(root)},
            ):
                directories = fonts._font_source_dirs()
        self.assertIn(root.resolve(), directories)
        self.assertIn(nested.resolve(), directories)

    def test_preflight_rejects_missing_staged_attachment(self):
        deck = _load_deck_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "materials").mkdir()
            (root / "materials/attachments.json").write_text(
                json.dumps({"attachments": [{"name": "brief.pdf", "status": "missing"}]}),
                encoding="utf-8",
            )
            count, errors, _warnings = deck._preflight_attachment_inventory(root)
        self.assertEqual(1, count)
        self.assertTrue(any("brief.pdf" in item for item in errors))

    def test_preflight_reuses_recent_browser_failure(self):
        deck = _load_deck_module()
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "browser-probe.json"
            cache.write_text(
                json.dumps(
                    {
                        "status": "failed",
                        "checked_at": deck.time.time(),
                        "error": "missing browser library",
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(
                deck.os.environ,
                {"MURAL_PREFLIGHT_FAILURE_TTL": "300"},
            ):
                payload = deck._cached_browser_probe(cache)
        self.assertIsNotNone(payload)
        self.assertEqual("failed", payload["status"])
        self.assertIn("missing browser", payload["error"])

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

    def test_build_quality_gate_uses_structured_render_evidence(self):
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
            self.assertEqual(len(manifest["evidence"]), 80)
            self.assertTrue(all(item["sha256"] for item in manifest["evidence"]))
            self.assertTrue(all(group["evidence"] for group in manifest["groups"]))

    def test_focus_contact_sheet_is_unique_and_has_page_evidence(self):
        deck = _load_deck_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            renders = root / "renders"
            renders.mkdir()
            from PIL import Image
            for page in (1, 2):
                Image.new("RGB", (160, 90), (page * 10, 20, 30)).save(
                    renders / f"slide_{page:02d}.png"
                )
            deck._build_contact(root, focus="1,2", label="bookends")
            sheet = renders / "contact-sheet-focus-bookends.png"
            sidecar = renders / "contact-sheet-focus-bookends.json"
            self.assertTrue(sheet.is_file())
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertEqual(payload["pages"], [1, 2])
            self.assertEqual([item["page"] for item in payload["evidence"]], [1, 2])
            self.assertTrue(all(item["sha256"] for item in payload["evidence"]))

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
        self.assertIn("crowded", hard_keys)
        self.assertIn("ORNAMENTAL_PUNCT", renderer)

    def test_final_player_adds_lightweight_motion_without_mutating_slide_sources(self):
        deck = _load_deck_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "slides").mkdir()
            source = root / "slides/slide_01.html"
            original = (
                "<!doctype html><html><head></head><body>"
                "<section class='slide'><header class='slide-title'>标题</header>"
                "<main class='slide-body'><div>正文</div></main>"
                "<footer class='slide-footer'>页脚</footer></section>"
                "</body></html>"
            )
            source.write_text(original, encoding="utf-8")
            with mock.patch.object(
                deck, "_validate_prepared_runtime", return_value={"faces": []}
            ) as validate_runtime, mock.patch.object(
                deck, "validate_render_freshness", return_value=[]
            ), mock.patch.object(deck, "bundle_workspace") as bundle:
                self.assertEqual(0, deck._build_player(root, expected=1))

            validate_runtime.assert_called_once_with(root)
            bundle.assert_not_called()

            player = (root / "present.html").read_text(encoding="utf-8")
            self.assertEqual(original, source.read_text(encoding="utf-8"))
            self.assertIn("@keyframes mural-enter", player)
            self.assertIn("prefers-reduced-motion:reduce", player)
            self.assertIn("data-mural-reveal-step", player)
            self.assertIn("prepareMotion(e)", player)
            self.assertIn(".slide-title,.page-header,header", player)
            self.assertIn(".slide-body,.page-body", player)

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
                "bundle_fonts.py",
                "cutout_image.py",
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
        bundle = _load_bundle_fonts_module()
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

    def test_deck_prepare_materializes_theme_without_model_copying_template(self):
        deck = _load_deck_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plan").mkdir()
            (root / "plan/deck.md").write_text("# Deck\n- language: zh\n", encoding="utf-8")
            (root / "plan/slide_01.md").write_text(
                "# Slide 01 — 开场\n- 标题：开场\n\n## 口语讲稿\n\n开场。\n\n## 来源\n\n- none\n",
                encoding="utf-8",
            )
            (root / "plan/theme.css").write_text(
                ":root{--bg:#06070d;--accent:#38f2d6}\n.pixel-frame{border-radius:0}",
                encoding="utf-8",
            )
            with mock.patch.object(deck, "bundle_workspace", return_value={"faces": []}):
                deck._prepare_workspace(root, 1)
            css = (root / "base.css").read_text(encoding="utf-8")
            self.assertIn("deck-theme-overrides", css)
            self.assertIn("--accent:#38f2d6", css)
            self.assertIn(".slide{", css)

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

    def test_build_rejects_unprepared_echarts_without_rewriting_slide(self):
        deck = _load_deck_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "slides").mkdir()
            (root / "assets/fonts").mkdir(parents=True)
            slide = root / "slides/slide_01.html"
            original = (
                '<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/'
                'echarts.min.js"></script>'
            )
            slide.write_text(original, encoding="utf-8")
            (root / "base.css").write_text(
                "/* deck-runtime-canvas-reset */\nhtml,body{margin:0}", encoding="utf-8"
            )
            deck._ensure_runtime_assets(root)
            (root / "assets/fonts/manifest.json").write_text(
                '{"faces":[]}', encoding="utf-8"
            )
            with mock.patch.object(deck, "validate_font_bundle", return_value=[]):
                with self.assertRaisesRegex(ValueError, "必须引用"):
                    deck._validate_prepared_runtime(root)
            self.assertEqual(original, slide.read_text(encoding="utf-8"))

    def test_build_requires_current_review_contact_without_regenerating_it(self):
        deck = _load_deck_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            renders = root / "renders"
            renders.mkdir()
            (renders / "slide_01.png").write_bytes(b"reviewed pixels")
            (renders / "contact-sheet.png").write_bytes(b"overview")
            (renders / "contact-sheet-review-01.png").write_bytes(b"group")
            payload = {
                "full": {
                    "pages": [1],
                    "overview": "renders/contact-sheet.png",
                    "groups": [{
                        "path": "renders/contact-sheet-review-01.png",
                        "pages": [1],
                    }],
                    "evidence": [{
                        "page": 1,
                        "path": "renders/slide_01.png",
                        "sha256": hashlib.sha256(b"reviewed pixels").hexdigest(),
                    }],
                },
            }
            manifest = renders / "review-contact.json"
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            before = {path: path.read_bytes() for path in renders.iterdir()}
            deck._validate_review_contact(root, expected=1)
            self.assertEqual(before, {path: path.read_bytes() for path in renders.iterdir()})

            (renders / "slide_01.png").write_bytes(b"changed after review")
            with self.assertRaisesRegex(ValueError, "早于当前逐页 PNG"):
                deck._validate_review_contact(root, expected=1)

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
        fonts = _load_bundle_fonts_module()
        fonts._validate_font_allowlist()
        self.assertEqual(set(fonts.FAMILY_FACES), set(fonts.FONT_LICENSES))
        self.assertTrue(all(item["license"] == "OFL-1.1" for item in fonts.FONT_LICENSES.values()))
        self.assertTrue(all(item["source"].startswith("https://github.com/") for item in fonts.FONT_LICENSES.values()))
        self.assertTrue(fonts.OFL_TEMPLATE.is_file())

    def test_user_font_config_is_hash_checked_and_mapped_to_roles(self):
        fonts = _load_bundle_fonts_module()
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
        fonts = _load_bundle_fonts_module()
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
        content = (REPO / "skills/mural-presenter/assets/base-template.css").read_text(encoding="utf-8")
        content += (REPO / "skills/mural-presenter/references/fonts.md").read_text(encoding="utf-8")
        for name in ("段宁硬笔楷", "叶根友钢笔行书", "迷你简硬笔行书", "Lovely Little Jelly", "今年也要加油鸭"):
            self.assertNotIn(name, content)
        for name in ("Liu Jian Mao Cao", "Long Cang", "Zhi Mang Xing", "ZCOOL QingKe HuangYou"):
            self.assertIn(name, content)

if __name__ == "__main__":
    unittest.main()
