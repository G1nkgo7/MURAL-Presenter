from pathlib import Path
from unittest import mock

import pytest
from fastapi.responses import HTMLResponse
from fastapi import HTTPException

from app import main, preview_fonts
from workspace_fixture import write_workspace_manifest


def test_shared_preview_catalog_contains_v04_core_faces() -> None:
    catalog = preview_fonts.catalog()
    assert "NotoSansSC.ttf" in catalog
    assert "SmileySans-Oblique.ttf" in catalog
    css = preview_fonts.stylesheet()
    assert "font-family: 'Noto Sans SC'" in css
    assert "font-family: 'Smiley Sans'" in css
    assert "/api/preview-fonts/SmileySans-Oblique.ttf" in css


def test_preview_font_injection_is_idempotent() -> None:
    source = "<!doctype html><html><head><title>x</title></head><body></body></html>"
    injected = preview_fonts.inject(source)
    assert 'name="studio-preview-fonts"' in injected
    assert "/api/preview-fonts.css" in injected
    assert preview_fonts.inject(injected) == injected


def test_legacy_present_route_is_rejected_without_mutating_deck(
    tmp_path: Path,
) -> None:
    present = tmp_path / "present.html"
    source = "<!doctype html><html><head></head><body>deck</body></html>"
    present.write_text(source, encoding="utf-8")
    row = {
        "id": 7,
        "user_id": 1,
        "run_dir": str(tmp_path),
        "skill_version": "mural-presenter-v0.4",
    }
    with mock.patch.object(main, "_own_deck", return_value=row):
        with pytest.raises(HTTPException) as captured:
            main.deck_file_path(7, "present.html", user={"id": 1}, con=object())
    assert captured.value.status_code == 409
    assert captured.value.detail == "unsupported_workspace_layout"
    assert present.read_text(encoding="utf-8") == source


def test_failed_run_never_exposes_intermediate_html_as_final(tmp_path: Path) -> None:
    write_workspace_manifest(tmp_path)
    (tmp_path / "present.html").write_text("<html>broken draft</html>", encoding="utf-8")
    state = main._static_html_preview_for_status(str(tmp_path), "failed")
    assert state["ready"] is False
    assert state["final"] is False
    assert state["failed_intermediate"] is True

    row = {
        "id": 9,
        "user_id": 1,
        "run_dir": str(tmp_path),
        "skill_version": "mural-presenter-v1",
        "status": "failed",
    }
    with mock.patch.object(main, "_own_deck", return_value=row):
        with pytest.raises(HTTPException) as captured:
            main.deck_file_path(
                9,
                "present.html",
                user={"id": 1},
                con=object(),
            )
    assert captured.value.status_code == 409
    assert "失败中间态" in str(captured.value.detail)
