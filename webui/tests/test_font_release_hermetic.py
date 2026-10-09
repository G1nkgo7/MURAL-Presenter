"""Font release invariants: hermetic self-containment + declaration parity.

Three font declarations must describe one vocabulary:

* ``font_bundle.FAMILY_FACES``  — families the exporter can bundle into a deck.
* ``preview_fonts.FACE_SPECS``  — families the live Studio preview serves.
* ``custom_fonts.BUILTIN_FAMILIES`` — families a user may pick per role.

If they drift, a deck can select a family the preview cannot show, or the UI can
offer a family the exporter cannot bundle.  These tests fail on that drift and
prove the release resolves every whitelisted face from the shipped ``fonts/``
directory alone — never from ``~/.fonts`` or system fontconfig.
"""
from __future__ import annotations

import ast
import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
FONT_BUNDLE = REPO / "skills/mural-presenter-v1/scripts/_internal/font_bundle.py"
PREVIEW_FONTS = ROOT / "studio/app/preview_fonts.py"
CUSTOM_FONTS = ROOT / "studio/app/custom_fonts.py"


def _load_font_bundle():
    name = "font_bundle_v1_under_test"
    spec = importlib.util.spec_from_file_location(name, FONT_BUNDLE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _literal_assignment(source: Path, target: str):
    """Extract a top-level literal (dict/tuple/set) by name without importing.

    ``custom_fonts`` and ``preview_fonts`` pull in FastAPI/engine at import
    time; parsing the AST keeps this contract test free of that runtime.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for name in node.targets:
                if isinstance(name, ast.Name) and name.id == target:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"{target} not found in {source}")


def _preview_families() -> set[str]:
    spec = importlib.util.spec_from_file_location('preview_fonts_parity_test', PREVIEW_FONTS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {family for family, *_rest in module.FACE_SPECS}


def _builtin_families() -> set[str]:
    return set(_literal_assignment(CUSTOM_FONTS, "BUILTIN_FAMILIES"))


class FontDeclarationParityTests(unittest.TestCase):
    def test_export_and_preview_describe_the_same_families(self):
        fb = _load_font_bundle()
        export = set(fb.FAMILY_FACES)
        preview = _preview_families()
        self.assertEqual(
            export,
            preview,
            "export/preview font families drifted: "
            f"export-only={sorted(export - preview)}, "
            f"preview-only={sorted(preview - export)}",
        )

    def test_preview_face_candidates_weights_and_styles_match_export(self):
        spec = importlib.util.spec_from_file_location('preview_fonts_faces_test', PREVIEW_FONTS)
        preview = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(preview)
        bundle = _load_font_bundle()
        expected = tuple((family, face.source_names, face.weight, face.style)
                         for family, faces in bundle.FAMILY_FACES.items() for face in faces)
        self.assertEqual(preview.FACE_SPECS, expected)
        css = preview.stylesheet()
        self.assertTrue(all(f'font-family: {family!r}' in css for family in bundle.FAMILY_FACES))

    def test_export_and_licenses_stay_in_lockstep(self):
        fb = _load_font_bundle()
        self.assertEqual(set(fb.FAMILY_FACES), set(fb.FONT_LICENSES))
        # _validate_font_allowlist enforces OFL-1.1 + a source for every family.
        fb._validate_font_allowlist()

    def test_every_selectable_family_is_bundleable_and_previewable(self):
        fb = _load_font_bundle()
        builtin = _builtin_families()
        export = set(fb.FAMILY_FACES)
        preview = _preview_families()
        self.assertLessEqual(
            builtin,
            export,
            f"selectable but not bundleable: {sorted(builtin - export)}",
        )
        self.assertLessEqual(
            builtin,
            preview,
            f"selectable but not previewable: {sorted(builtin - preview)}",
        )


class HermeticReleaseTests(unittest.TestCase):
    def test_release_dir_covers_every_family_matching_the_lock(self):
        fb = _load_font_bundle()
        report = fb.audit_release_fonts()
        self.assertTrue(
            report["ready"],
            "release font audit failed: " + "; ".join(report["missing"]),
        )
        self.assertEqual(report["family_count"], len(fb.FAMILY_FACES))
        self.assertGreaterEqual(report["face_count"], report["family_count"])

    def test_audit_ignores_home_and_system_fonts(self):
        """Auditing under a scrubbed HOME must give the identical verdict.

        This is the load-bearing hermetic proof: if the audit silently borrowed
        ``~/.fonts`` the result would change when HOME is emptied.
        """
        fb = _load_font_bundle()
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"HOME": "/nonexistent-home-for-tests"}, clear=False):
            scrubbed = fb.audit_release_fonts()
        baseline = fb.audit_release_fonts()
        self.assertEqual(scrubbed["ready"], baseline["ready"])
        self.assertEqual(scrubbed["missing"], baseline["missing"])
        self.assertTrue(scrubbed["ready"])

    def test_lock_pins_the_new_eleven_families(self):
        fb = _load_font_bundle()
        import json

        lock = json.loads(Path(fb.FONT_LOCK_PATH).read_text(encoding="utf-8"))
        families = lock["families"]
        for family in (
            "Inter", "Inter Tight", "Geist", "Geist Mono", "JetBrains Mono",
            "Poppins", "Roboto", "Roboto Mono", "Open Sans", "Figtree",
            "Plus Jakarta Sans",
        ):
            self.assertIn(family, families)
            self.assertEqual(families[family]["license"], "OFL-1.1")
            self.assertTrue(families[family]["faces"])


if __name__ == "__main__":
    unittest.main()
