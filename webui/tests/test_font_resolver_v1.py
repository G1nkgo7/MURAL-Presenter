"""Unit coverage for the v1 deterministic font resolver + substitution catalog.

These tests exercise the pure resolution layer and the catalog invariants
without touching the filesystem font sources: ``source_available`` is injected.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FONT_BUNDLE = ROOT.parent / "skills/mural-presenter-v1/scripts/_internal/font_bundle.py"


def _load():
    spec = importlib.util.spec_from_file_location("fb_resolver_under_test", FONT_BUNDLE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fb = _load()
CAT = fb.substitution_catalog()


def _avail(family: str) -> bool:
    return family in fb.FAMILY_FACES


def _resolve(stack, weight="400", style="normal", route=None, custom=None):
    return fb.resolve_font_request(
        role="body",
        token="--font-body",
        requested_stack=stack,
        weight=weight,
        style=style,
        custom_registry=custom or {},
        route_default_stack=route or ["Inter", "Noto Sans SC"],
        source_available=_avail,
        catalog=CAT,
    )


class CatalogInvariantTests(unittest.TestCase):
    def test_catalog_loads_and_validates(self):
        self.assertEqual(CAT["schema"], "mural.font-substitutions.v1")
        self.assertTrue(CAT["aliases"])
        self.assertTrue(CAT["mappings"])

    def test_all_mappings_proposed_not_audited(self):
        # Nothing may claim audited without specimen sign-off (none exists here).
        for mapping in CAT["mappings"]:
            self.assertEqual(mapping["status"], "proposed", mapping["id"])
            self.assertIsNone(mapping.get("specimen"))

    def test_commercial_entries_carry_no_file_paths(self):
        forbidden = {"source_path", "path", "file", "url", "src"}
        for mapping in CAT["mappings"]:
            self.assertFalse(forbidden & set(mapping.keys()), mapping["id"])

    def test_canonical_family_is_never_an_alias_source(self):
        for source in CAT["aliases"]:
            self.assertNotIn(source, fb.FAMILY_FACES)

    def test_alias_cycle_is_rejected(self):
        payload = {
            "schema": "mural.font-substitutions.v1",
            "aliases": {"A": "B", "B": "A"},
            "mappings": [],
        }
        with self.assertRaises(ValueError):
            fb._validate_substitution_catalog(payload)

    def test_audited_without_specimen_is_rejected(self):
        payload = {
            "schema": "mural.font-substitutions.v1",
            "aliases": {},
            "mappings": [
                {
                    "id": "x",
                    "requested_family": "Whatever",
                    "status": "audited",
                    "candidates": ["Inter"],
                    "specimen": None,
                }
            ],
        }
        with self.assertRaises(ValueError):
            fb._validate_substitution_catalog(payload)

    def test_audited_candidate_must_exist_in_allowlist(self):
        payload = {
            "schema": "mural.font-substitutions.v1",
            "aliases": {},
            "mappings": [
                {
                    "id": "x",
                    "requested_family": "Whatever",
                    "status": "audited",
                    "candidates": ["Not A Real Family"],
                    "specimen": "canary/x.png",
                }
            ],
        }
        with self.assertRaises(ValueError):
            fb._validate_substitution_catalog(payload)


class ResolverTests(unittest.TestCase):
    def test_exact_builtin(self):
        r = _resolve(["Inter"])
        self.assertEqual(r.resolution_kind, "exact_builtin")
        self.assertEqual(r.resolved_family, "Inter")

    def test_exact_alias_spelling_variant(self):
        r = _resolve(["InterVariable"])
        self.assertEqual(r.resolution_kind, "exact_alias")
        self.assertEqual(r.resolved_family, "Inter")

    def test_proposed_commercial_maps_to_portable_fallback_not_audited(self):
        r = _resolve(["SF Pro Text", "Inter"])
        self.assertEqual(r.resolution_kind, "portable_fallback")
        self.assertEqual(r.resolved_family, "Inter")
        self.assertEqual(r.mapping_id, "sf-pro-text")

    def test_alias_to_commercial_name_still_substitutes(self):
        r = _resolve(["PPNeueMontreal"])
        self.assertEqual(r.mapping_id, "pp-neue-montreal")
        self.assertIn(r.resolution_kind, {"portable_fallback", "audited_substitute"})

    def test_weight_out_of_range_downgrades_from_exact(self):
        # Bebas Neue ships only weight 400; a 700 request must not claim exact.
        r = _resolve(["Bebas Neue"], weight="700")
        self.assertNotEqual(r.resolution_kind, "exact_builtin")

    def test_user_upload_wins(self):
        registry = {"User::font-01": {"weight": "500", "style": "normal"}}
        r = _resolve(["User::font-01"], custom=registry)
        self.assertEqual(r.resolution_kind, "exact_user_upload")

    def test_unknown_name_never_unresolved_or_system_fallback(self):
        r = _resolve(["Totally Fake Font"])
        self.assertIn(r.resolution_kind, {"route_default", "portable_fallback"})
        self.assertNotIn(r.resolution_kind, {"unresolved", "system_fallback"})

    def test_audited_substitute_requires_mapping_id(self):
        # Force an audited mapping through a synthetic catalog and confirm the
        # resolver emits audited_substitute with the mapping id.
        catalog = {
            "schema": "mural.font-substitutions.v1",
            "aliases": {},
            "mappings": [
                {
                    "id": "demo",
                    "requested_family": "Demo Commercial",
                    "status": "audited",
                    "candidates": ["Inter"],
                    "specimen": "canary/demo.png",
                }
            ],
            "unmapped_fallback": {"default": "Inter"},
        }
        r = fb.resolve_font_request(
            role="body", token="--font-body",
            requested_stack=["Demo Commercial"], weight="400", style="normal",
            custom_registry={}, route_default_stack=["Inter"],
            source_available=_avail, catalog=catalog,
        )
        self.assertEqual(r.resolution_kind, "audited_substitute")
        self.assertEqual(r.mapping_id, "demo")


if __name__ == "__main__":
    unittest.main()
