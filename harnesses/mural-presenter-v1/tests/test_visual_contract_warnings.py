from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def deck_core(monkeypatch):
    scripts = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1/scripts"
    monkeypatch.syspath_prepend(str(scripts))
    return importlib.import_module("_internal.deck_core")


def _rendered(*pages: tuple[int, dict]) -> dict:
    return {
        "w": 1600,
        "h": 900,
        "pages": [
            {"page": number, "geometry": geometry}
            for number, geometry in pages
        ],
    }


def test_latin_display_face_falling_back_to_body_han_face_is_advisory(
    tmp_path, deck_core,
):
    trace = tmp_path / "_trace"
    trace.mkdir(parents=True)
    (trace / "font-resolution.json").write_text(
        json.dumps({
            "token_stacks": {
                "--font-display": ["Sora", "Noto Sans SC"],
                "--font-body": ["Noto Sans SC"],
            }
        }),
        encoding="utf-8",
    )
    first = {
        "images": [],
        "media": [],
        "text_boxes": [{
            "text": "刚接好线，一上电就报警？",
            "font_size": 72,
            "font_family": 'Sora, "Noto Sans SC", sans-serif',
        }],
    }

    warnings = deck_core._visual_contract_warnings(
        tmp_path,
        [{"number": 1}],
        _rendered((1, first)),
    )

    assert any("resolves to the body Han family" in warning for warning in warnings)


def test_separate_display_route_not_used_by_chinese_anchor_is_advisory(
    tmp_path, deck_core,
):
    trace = tmp_path / "_trace"
    trace.mkdir(parents=True)
    (trace / "font-resolution.json").write_text(
        json.dumps({
            "token_stacks": {
                "--font-display": ["Sora", "Noto Sans SC"],
                "--font-body": ["Noto Sans SC"],
            }
        }),
        encoding="utf-8",
    )
    first = {
        "images": [],
        "media": [],
        "text_boxes": [{
            "text": "一上电就报警？",
            "font_size": 72,
            "font_family": '"Noto Sans SC", sans-serif',
        }],
    }

    warnings = deck_core._visual_contract_warnings(
        tmp_path,
        [{"number": 1}],
        _rendered((1, first)),
    )

    assert any("separate display stack is not visibly applied" in warning for warning in warnings)


def test_distinct_han_display_face_does_not_warn(tmp_path, deck_core):
    trace = tmp_path / "_trace"
    trace.mkdir(parents=True)
    (trace / "font-resolution.json").write_text(
        json.dumps({
            "token_stacks": {
                "--font-display": ["Noto Serif SC", "Noto Sans SC"],
                "--font-body": ["Noto Sans SC"],
            }
        }),
        encoding="utf-8",
    )
    first = {
        "images": [],
        "media": [],
        "text_boxes": [{
            "text": "有温度的安全实训",
            "font_size": 64,
            "font_family": '"Noto Serif SC", "Noto Sans SC", serif',
        }],
    }

    warnings = deck_core._visual_contract_warnings(
        tmp_path,
        [{"number": 1}],
        _rendered((1, first)),
    )

    assert not any("resolves to the body Han family" in warning for warning in warnings)
