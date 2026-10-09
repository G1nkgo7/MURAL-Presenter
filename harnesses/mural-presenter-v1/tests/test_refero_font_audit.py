"""Unit tests for the read-only Refero font audit classifier + coverage math.

These exercise the classification/normalization machinery on representative
dirty input (CSS stacks, icon fonts, noise, commercial families we do NOT
vendor) so the coverage denominator is trustworthy even though the committed
snapshot is a placeholder.  Network is never touched.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


AUDIT_PATH = (
    Path(__file__).resolve().parents[1]
    / "canary/refero_font_audit.py"
)


def _load_audit():
    name = "_refero_audit_under_test"
    spec = importlib.util.spec_from_file_location(name, AUDIT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


AUDIT = _load_audit()


class ClassifyTests(unittest.TestCase):
    def _bucket(self, raw: str) -> str:
        records = AUDIT.classify(raw)
        # For a single (non-stack) atom, the first record is the classification.
        return records[0]["bucket"]

    def test_generic_families_are_excluded(self):
        for value in ("serif", "sans-serif", "monospace", "system-ui", "UI-Monospace"):
            self.assertEqual(self._bucket(value), "generic_family", value)

    def test_icon_fonts_are_flagged(self):
        for value in ("Font Awesome 6 Free", "Material Icons", "material symbols outlined"):
            self.assertEqual(self._bucket(value), "icon_font", value)

    def test_noise_is_flagged(self):
        for value in ("16px", "Bold", "unknown", "font", "  "):
            self.assertEqual(self._bucket(value), "description_noise", value)

    def test_concrete_families_are_font_family(self):
        for value in ("Inter", "SF Pro Display", "Graphik", "PP Neue Montreal"):
            self.assertEqual(self._bucket(value), "font_family", value)

    def test_css_stack_splits_into_members(self):
        records = AUDIT.classify('"Inter", "Helvetica Neue", sans-serif')
        self.assertEqual(records[0]["bucket"], "css_stack")
        members = {r["canonical"] or r["bucket"] for r in records[1:]}
        self.assertIn("Inter", members)
        self.assertIn("Helvetica Neue", members)
        self.assertIn("generic_family", members)

    def test_alias_folding_is_canonical(self):
        for raw, canonical in (
            ("PPNeueMontreal", "PP Neue Montreal"),
            ("InterVariable", "Inter Variable"),
            ("GeistMono", "Geist Mono"),
            ("SuisseIntl", "Suisse Intl"),
        ):
            records = AUDIT.classify(raw)
            self.assertEqual(records[0]["canonical"], canonical, raw)

    def test_nfkc_and_case_fold_dedupe(self):
        # Full-width + case variants must normalize to one canonical key.
        self.assertEqual(AUDIT._canonical_key("Ｉｎｔｅｒ"), AUDIT._canonical_key("inter"))
        self.assertEqual(AUDIT._canonical_key('"Open Sans"'), AUDIT._canonical_key("open sans"))


class CoverageMathTests(unittest.TestCase):
    def test_denominator_is_distinct_families_not_raw_strings(self):
        snapshot = {
            "provenance": "test",
            "font_strings": [
                "Inter", "inter", '"Inter"',       # 3 raw -> 1 canonical, covered
                "SF Pro Display",                    # commercial, NOT vendored
                "Graphik",                           # commercial, NOT vendored
                "sans-serif", "Font Awesome", "14px",  # excluded buckets
            ],
        }
        whitelist = {"Inter", "Roboto"}
        _baseline, summary = AUDIT.build_baseline(snapshot, whitelist)
        self.assertEqual(summary["raw_string_count"], 8)
        # distinct canonical families: Inter, SF Pro Display, Graphik = 3
        self.assertEqual(summary["distinct_canonical_families"], 3)
        self.assertEqual(summary["covered_by_whitelist"], 1)
        self.assertEqual(summary["uncovered_by_whitelist"], 2)
        self.assertGreaterEqual(summary["excluded_non_family_count"], 3)
        uncovered = {row["family"] for row in summary["uncovered_families"]}
        self.assertEqual(uncovered, {"SF Pro Display", "Graphik"})


if __name__ == "__main__":
    unittest.main()
