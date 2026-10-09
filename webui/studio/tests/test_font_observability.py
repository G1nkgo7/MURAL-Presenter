"""E26 font-observability contract: role expansion + resolution labels."""
from app import custom_fonts, main


def test_font_roles_include_display_and_mono() -> None:
    # v2 makes display/title independent and adds the mono role.
    assert {"display", "title", "body", "number", "annotation", "mono"} <= custom_fonts.FONT_ROLES


def test_resolution_labels_are_text_and_cover_every_kind() -> None:
    labels = main._FONT_RESOLUTION_LABELS
    for kind in (
        "exact_builtin", "exact_alias", "exact_user_upload", "audited_substitute",
        "route_default", "portable_fallback", "system_fallback", "unresolved",
    ):
        assert labels.get(kind), kind
    # Error kinds carry an explicit textual error marker (not colour-only).
    assert "错误" in labels["system_fallback"]
    assert "错误" in labels["unresolved"]


def test_builtin_display_and_mono_roles_offer_the_new_families(tmp_path=None) -> None:
    # A builtin family a user may pick for a role must be an approved builtin.
    for family in ("Inter", "Inter Tight", "Geist", "Geist Mono", "JetBrains Mono"):
        assert family in custom_fonts.BUILTIN_FAMILIES
