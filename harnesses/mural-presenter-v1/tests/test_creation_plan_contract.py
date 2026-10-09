"""当前创作配置、自然分页文稿与共享脚本的唯一消费入口。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


SKILL = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1"


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(SKILL / "scripts"))
    from _internal import deck_config, deck_core, deck_plan
    return deck_config, deck_core, deck_plan


def write_plan(root, config=None, brief=None):
    config = config or {"working_language": "zh", "presentation_language": "en", "target_page_count": 1}
    (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
    (root / "deck.md").write_text(
        "# 创作方案\n## 任务\n中文协作，英文呈现。\n## Pages\n概要，不是第二份分页。\n"
        "## 整体设计规范\n| 项目 | 决定 |\n| 主色 | #07C160 |\n", encoding="utf-8")
    (root / "briefs").mkdir(exist_ok=True)
    (root / "briefs/slide_01.md").write_text(brief or
        "# Evidence first\n\n## 本页任务\n说明证据。\n\n## 页面内容与证据\n"
        "Evidence first.\n数据单位、时间口径、估算限定仍在。\n\n## 视觉表达与约束\n保留主体。\n"
        "\n## Speaker notes\nExplain the evidence.\n\n- State its boundary.\n", encoding="utf-8")
    return config


def test_initialization_never_guesses_query_or_replaces_current_settings(tmp_path, modules):
    cfg, _, _ = modules
    value = cfg.initialize_config(tmp_path, presentation_language="en", target_page_count=2)
    assert value == {"working_language": None, "presentation_language": "en", "target_page_count": 2}
    with pytest.raises(ValueError, match="working_language"):
        cfg.read_config(tmp_path, require_complete=True)
    value["working_language"] = "zh"
    (tmp_path / "config.json").write_text(json.dumps(value))
    before = (tmp_path / "config.json").read_bytes()
    assert cfg.initialize_config(tmp_path, presentation_language="zh", target_page_count=9) == value
    assert (tmp_path / "config.json").read_bytes() == before


def test_local_preparation_and_fragments_do_not_require_unrelated_plans(tmp_path, modules, monkeypatch):
    _, core, plan = modules
    write_plan(tmp_path, {"working_language": "en", "presentation_language": "en", "target_page_count": 10})
    (tmp_path / "briefs/slide_01.md").rename(tmp_path / "briefs/slide_05.md")
    (tmp_path / "briefs/slide_03.md").write_text("Unfinished independent draft")
    monkeypatch.setattr(core, "_read_author_geometry", lambda _: {})
    monkeypatch.setattr(core, "_ensure_authoring_font_bundle", lambda *a, **k: {})
    assert core.scaffold_from_plans(tmp_path, pages=[5]) == [5]
    with pytest.raises(ValueError):
        core.validate_plans(tmp_path)
    assert len(plan.read_deck(tmp_path, pages=(5,))[1]) == 1
    with pytest.raises(ValueError, match="target_page_count"):
        core.scaffold_from_plans(tmp_path, pages=[11])
    page = tmp_path / "slides/slide_05.html"
    page.write_text('<section class="slide" id="slide-05" data-slide="05"><div class="page-body">Evidence</div></section>')
    assert core._fragments(tmp_path, expected=10, only_page=5)[2] == [(5, page.read_text())]


@pytest.mark.parametrize("patch", [
    {"provider": "fake"}, {"target_page_count": True}, {"target_page_count": 0},
    {"target_page_count": "2"}, {"working_language": "auto"},
    {"presentation_language": "中文"}, {"speech_language": None},
])
def test_invalid_configuration_is_explicit(tmp_path, modules, patch):
    value = write_plan(tmp_path)
    with pytest.raises(ValueError):
        modules[0].validate_config(value | patch)


def test_missing_config_does_not_consume_legacy_markdown(tmp_path, modules):
    write_plan(tmp_path)
    (tmp_path / "config.json").unlink()
    (tmp_path / "deck.md").write_text("# Old\n## Brief\n- language: en\n- page_count: 1\n")
    before = (tmp_path / "deck.md").read_bytes()
    with pytest.raises(ValueError, match="config.json is missing"):
        modules[1]._load_plans(tmp_path)
    assert not (tmp_path / "config.json").exists()
    assert (tmp_path / "deck.md").read_bytes() == before


def test_markdown_is_not_a_configuration_or_css_channel(tmp_path, modules, monkeypatch):
    _, core, _ = modules
    write_plan(tmp_path)
    with (tmp_path / "deck.md").open("a") as handle:
        handle.write("\n## Brief\n- language: zh\n- page_count: 99\n- footer: internal\n"
                     "## Theme Tokens\n```css\n:root {--accent: red;}\n```\n"
                     "## Visual Direction\n- typography_route: nonexistent\n"
                     "## Shared Visual Primitives\n```css\n.deck-test {color: red;}\n```\n")
    (tmp_path / "base.css").write_text(":root {--accent: #07C160;}\n")
    (tmp_path / "slides").mkdir()
    css = (tmp_path / "base.css").read_bytes()
    monkeypatch.setattr(core, "_read_author_geometry", lambda root: {})
    monkeypatch.setattr(core, "_ensure_authoring_font_bundle", lambda root, **kw: {})
    deck, pages = core._load_plans(tmp_path)
    assert deck["language"] == "en" and deck["pages"] == 1
    assert not {"resolved", "tokens", "footer"} & deck.keys()
    assert not {"role", "evidence", "composition", "special_layout", "show_footer", "production_group"} & pages[0].keys()
    core.scaffold_from_plans(tmp_path)
    assert (tmp_path / "base.css").read_bytes() == css
    assert not (tmp_path / "_trace/design-compile.json").exists()
    assert not (tmp_path / "_trace/typography-route.json").exists()
    html = (tmp_path / "slides/slide_01.html").read_text()
    assert "Evidence first" not in html and "internal" not in html and "<footer" not in html
    assert core._skeleton_is_unfilled(html)


def test_scaffold_preserves_a_file_created_between_check_and_atomic_publish(tmp_path, modules, monkeypatch):
    _, core, _ = modules
    write_plan(tmp_path)
    (tmp_path / "base.css").write_text(":root {--canvas-w:1600px; --canvas-h:900px;}")
    monkeypatch.setattr(core, "_read_author_geometry", lambda _: {})
    monkeypatch.setattr(core, "_ensure_authoring_font_bundle", lambda *args, **kwargs: {})
    original = core.os.link
    html = tmp_path / "slides/slide_01.html"

    def concurrent_write(source, target):
        assert target == html
        html.write_text("Concurrent author's current page")
        original(source, target)

    monkeypatch.setattr(core.os, "link", concurrent_write)
    assert core.scaffold_from_plans(tmp_path, pages=[1]) == [1]
    assert html.read_text() == "Concurrent author's current page"
    assert not list((tmp_path / "_runtime/tmp").iterdir())


@pytest.mark.parametrize("target", ["page", "directory"])
def test_scaffold_rejects_symlinks_before_shared_preparation(tmp_path, modules, monkeypatch, target):
    _, core, _ = modules
    write_plan(tmp_path)
    outside = tmp_path / "unchanged"
    outside.mkdir()
    if target == "page":
        (tmp_path / "slides").mkdir()
        (tmp_path / "slides/slide_01.html").symlink_to(outside / "page.html")
    else:
        (tmp_path / "slides").symlink_to(outside, target_is_directory=True)
    def unexpected(*args, **kwargs):
        pytest.fail("invalid paths must fail before shared preparation")
    monkeypatch.setattr(core, "_read_author_geometry", unexpected)
    monkeypatch.setattr(core, "_ensure_authoring_font_bundle", unexpected)
    with pytest.raises(ValueError, match="regular workspace file"):
        core.scaffold_from_plans(tmp_path, pages=[1])
    assert not list(outside.iterdir())


def test_target_count_is_not_an_implicit_structural_edit(tmp_path, modules, monkeypatch, capsys):
    config = write_plan(tmp_path)
    config["target_page_count"] = 3
    (tmp_path / "config.json").write_text(json.dumps(config))
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    _, core, _ = modules
    deck, _ = core._load_plans(tmp_path, expected=1)
    assert deck["pages"] == 1
    inventory = core.inspect_plans(tmp_path)
    assert inventory["expected"] == 3 and inventory["actual"] == 1
    assert "REPAIR_REQUIRED" in capsys.readouterr().out
    monkeypatch.setattr(core, "_read_author_geometry", lambda root: pytest.fail("target check must precede browser"))
    with pytest.raises(ValueError, match="target_page_count=3"):
        core.scaffold_from_plans(tmp_path)
    with pytest.raises(ValueError, match="target_page_count=3"):
        core.finalize(tmp_path)
    assert before == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


@pytest.mark.parametrize("heading", ["讲稿", "Speaker notes", "SPEAKER NOTES"])
def test_speech_section_keeps_paragraphs_lists_and_fenced_headings(tmp_path, modules, heading):
    notes = "开场。\n\n- 第一项\n- 第二项\n\n### 补充\n解释。\n\n```markdown\n## Speaker notes\n- literal\n```"
    source = f"# Public title\n\n## 页面内容与证据\nNOT NOTES\n\n## {heading}\n{notes}\n\n## 附加证据\nNOT NOTES EITHER\n"
    config = write_plan(tmp_path, brief=source)
    config["speech_language"] = "zh"
    (tmp_path / "config.json").write_text(json.dumps(config))
    parser = modules[2]
    assert parser.read_deck(tmp_path)[1][0]["speech"] == notes
    assert modules[1].sync_speech(tmp_path)["notes_count"] == 1
    speech = (tmp_path / "speech.md").read_text()
    assert speech.startswith("# 演讲备注") and notes in speech and "NOT NOTES" not in speech
    assert (tmp_path / "briefs/slide_01.md").read_text() == source


def test_ambiguous_notes_fail_and_code_metadata_is_not_executed(tmp_path, modules):
    write_plan(tmp_path, brief="# Title\n```markdown\n- page_type: closing\n```\n\n## 本页任务\n- page_type: cover\n")
    assert "page_type" not in modules[2].read_deck(tmp_path)[1][0]
    (tmp_path / "briefs/slide_01.md").write_text("# Title\n## 讲稿\nA\n## Speaker notes\nB\n")
    with pytest.raises(ValueError, match="at most one"):
        modules[2].read_deck(tmp_path)


@pytest.mark.parametrize("intent", ["封面：介绍主题。", "内容页：比较证据。", "转场：进入下一章。",
    "结尾：留下下一步。", "- page_type: cover\n- page_type: invented\n- special_layout: invented"])
def test_page_intent_is_preserved_without_selecting_a_scaffold(tmp_path, modules, intent):
    source = f"# Subject\n{intent}\n\n## 本页任务\n介绍主题。\n"
    write_plan(tmp_path, brief=source)
    deck, pages = modules[1]._load_plans(tmp_path)
    assert pages[0]["text"] == source.strip()
    assert not {"page_type", "special_layout", "composition", "show_footer"} & pages[0].keys()
    html = modules[1]._slide_skeleton(pages[0]["number"])
    assert 'class="page-stage"' in html and 'data-slide="01"' in html
    assert not any(value in html for value in ("data-page-type", "data-frame", "<header", "<h1", "<footer"))
    assert modules[1]._skeleton_is_unfilled(html)


def test_creation_config_is_an_allowed_deliverable(tmp_path, modules):
    write_plan(tmp_path)
    from _internal.workspace_policy import audit_workspace
    assert audit_workspace(tmp_path) == []


@pytest.mark.parametrize("language", ["en", "zh", "zh-CN", "fr"])
def test_document_preserves_configured_language_tag(modules, language):
    assert f'<html lang="{language}">' in modules[1]._document("", [(1, "<section>hello</section>")], language)


def test_real_skill_cache_only_tracks_presentation_language(tmp_path, modules):
    config = write_plan(tmp_path)
    css = ":lang(en) .slide {background:red} :lang(zh) .slide {background:blue}"
    fragment = '<section class="slide" data-slide="01">Hello</section>'
    core = modules[1]
    old = core._render_input_digest(tmp_path, css, fragment)
    config.update(working_language="en", target_page_count=4, speech_language="zh")
    (tmp_path / "config.json").write_text(json.dumps(config))
    assert core._render_input_digest(tmp_path, css, fragment) == old
    config["presentation_language"] = "zh"
    (tmp_path / "config.json").write_text(json.dumps(config))
    assert core._render_input_digest(tmp_path, css, fragment) != old


@pytest.mark.parametrize("page", [1, 5])
def test_real_render_reuses_working_language_but_refreshes_presentation(tmp_path, monkeypatch, capsys, page):
    from PIL import Image
    from geometry_fixture import geometry, require_browser, write_geometry
    from runtime.shared_objects import workspace_skill_module

    require_browser(tmp_path)
    write_geometry(tmp_path, geometry(width_px=800, height_px=600))
    config = write_plan(tmp_path)
    if page != 1:
        config["target_page_count"] = 10
        (tmp_path / "config.json").write_text(json.dumps(config))
        (tmp_path / "briefs/slide_01.md").rename(tmp_path / f"briefs/slide_{page:02d}.md")
        (tmp_path / "briefs/slide_03.md").write_text("Unfinished independent plan")
    core = workspace_skill_module(tmp_path, "deck_core")
    monkeypatch.setattr(core, "_ensure_authoring_font_bundle", lambda *_args, **_kwargs: {})
    css = (tmp_path / "base.css").read_text() + (
        "body{margin:0}.deck,.stage{position:absolute;width:var(--canvas-w);height:var(--canvas-h)}"
        ".slide{position:absolute;visibility:hidden;color:white}.slide.active{visibility:visible}"
        ".slide:lang(en){background:rgb(220,20,30)}.slide:lang(zh-CN){background:rgb(20,30,220)}"
        "h1{font:48px sans-serif;padding:40px}"
    )
    (tmp_path / "base.css").write_text(css)
    (tmp_path / f"slides/slide_{page:02d}.html").write_text(f'<section class="slide" data-slide="{page:02d}"><h1>Language-sensitive page</h1></section>')
    assert core.render(tmp_path, page, expected=config["target_page_count"])
    png = tmp_path / f"renders/slide_{page:02d}.png"
    first = png.read_bytes()
    with Image.open(png) as image:
        assert image.convert("RGB").getpixel((790, 590)) == (220, 20, 30)
    capsys.readouterr()
    config["working_language"] = "en"
    (tmp_path / "config.json").write_text(json.dumps(config))
    assert core.render(tmp_path, page, expected=config["target_page_count"])
    assert "render:cached" in capsys.readouterr().out
    assert png.read_bytes() == first
    config["presentation_language"] = "zh-CN"
    (tmp_path / "config.json").write_text(json.dumps(config))
    assert core.render(tmp_path, page, expected=config["target_page_count"])
    assert "render:cached" not in capsys.readouterr().out
    assert png.read_bytes() != first
    with Image.open(png) as image:
        assert image.convert("RGB").getpixel((790, 590)) == (20, 30, 220)
    assert core._page_render_metadata(tmp_path, page)["presentation_language"] == "zh-CN"
