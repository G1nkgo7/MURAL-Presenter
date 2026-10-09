"""单一作者计划、内存交接与可选讲稿；不以文档关键词代替行为验收。"""
from pathlib import Path
import json
import re
import pytest

SKILL = Path(__file__).resolve().parents[3] / 'skills/mural-presenter-v1'


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(SKILL / 'scripts'))
    from _internal import deck_core, deck_plan
    return deck_core, deck_plan


def plan(root, count=3):
    shared = '# Test\n## Task and audience\nExplain the evidence.\n## Overall design\nA visible experimental object.\n'
    (root / 'config.json').write_text(json.dumps({
        'working_language': 'zh', 'presentation_language': 'en', 'target_page_count': count,
    }))
    (root / 'deck.md').write_text(shared)
    (root / 'briefs').mkdir(exist_ok=True)
    for number in range(1, count + 1):
        (root / f'briefs/slide_{number:02d}.md').write_text(
            f'# Topic {number}\n## 本页任务\nExplain this result.\n'
            f'## 页面内容与证据\nSource M{number}; exact 12.5%\n'
            f'## 视觉表达与约束\nUse a native diagram.\n## Speaker notes\nSay only note {number}.\n')
    return shared


def test_minimal_plan_is_not_rewritten_and_packets_are_page_scoped(tmp_path, modules):
    core, parser = modules
    source = plan(tmp_path)
    before = {p.name: p.read_bytes() for p in (tmp_path / 'briefs').iterdir()}
    deck, pages = core._load_plans(tmp_path, 3)
    assert deck['language'] == 'en'
    assert deck['creation_config'] == {
        'working_language': 'zh', 'presentation_language': 'en', 'target_page_count': 3,
    }
    assert pages[0]['title'] == 'Topic 1'
    assert 'Explain this result.' in pages[0]['text']
    assert 'Source M1; exact 12.5%' in pages[0]['text']
    assert not {'page_type', 'role', 'evidence', 'composition', 'primary_visual_medium'} & pages[0].keys()
    packet = parser.owner_context(tmp_path, (2,))
    assert 'experimental object' in packet['shared']
    assert [row['number'] for row in packet['pages']] == [2]
    assert 'Source M1' not in str(packet) and 'Source M3' not in str(packet)
    assert len(parser.owner_context(tmp_path, None)['pages']) == 3
    assert (tmp_path / 'deck.md').read_text() == source
    assert {p.name: p.read_bytes() for p in (tmp_path / 'briefs').iterdir()} == before
    assert sorted(path.name for path in tmp_path.iterdir()) == ['briefs', 'config.json', 'deck.md']


def test_speech_contains_only_optional_notes_not_evidence(tmp_path, modules):
    core, _ = modules
    from state.workspace import WorkspaceState
    WorkspaceState(tmp_path)
    plan(tmp_path)
    assert not (tmp_path / 'speech.md').exists()
    core.sync_speech(tmp_path, 3)
    speech = (tmp_path / 'speech.md').read_text()
    assert 'Say only note 1.' in speech
    assert 'Source M' not in speech and '12.5%' not in speech and 'Evidence' not in speech
    assert len(list((tmp_path / 'briefs').glob('*.md'))) == 3
    assert not (tmp_path / 'design-brief.md').exists()


@pytest.mark.parametrize('body', [
    r'<p>Unicode notation: \u0041, \U0001F30A, \ud83c\udf0a</p>',
    r'<code>\u003cstrong\u003eexample\u003c/strong\u003e</code>',
    r'<p>Literal slash: \\u0041</p>',
    '<p>真实文字与海洋 🌊 &amp; HTML entities</p>',
    json.loads(r'{"body": "<p>\u6d77\u6d0b</p>"}')['body'],
])
def test_author_fragment_text_is_preserved_without_transport_guessing(tmp_path, modules, body):
    core, _ = modules
    plan(tmp_path, count=1)
    (tmp_path / 'slides').mkdir()
    source = core._slide_skeleton(1).replace(
        '<!-- SLIDE_AGENT_FILL_END -->', body + '\n<!-- SLIDE_AGENT_FILL_END -->')
    path = tmp_path / 'slides/slide_01.html'
    path.write_text(source)
    _, _, fragments = core._fragments(tmp_path, expected=1)
    assert fragments == [(1, source.strip())]
    assert path.read_text() == source


@pytest.mark.parametrize('valid_count', [0, 1, 3])
def test_speech_reports_real_coverage_without_guessing_other_sections(tmp_path, modules, capsys, valid_count):
    from state.workspace import WorkspaceState
    WorkspaceState(tmp_path)
    plan(tmp_path)
    (tmp_path / 'speech.md').write_text('stale notes from an earlier version')
    for number in range(valid_count + 1, 4):
        path = tmp_path / f'briefs/slide_{number:02d}.md'
        path.write_text(path.read_text().replace('## Speaker notes', '## Background notes'))
    report = modules[0].sync_speech(tmp_path, 3)
    assert report['notes_count'] == valid_count
    assert report['page_count'] == 3
    assert report['missing_pages'] == list(range(valid_count + 1, 4))
    assert report['status'] == ('complete' if valid_count == 3 else 'partial' if valid_count else 'omitted')
    assert report['format_warnings'] == []
    assert report['omission_reason'] == ('' if valid_count else 'no_valid_speaker_notes')
    assert (tmp_path / 'speech.md').exists() == bool(valid_count)
    assert '[speech] ' in capsys.readouterr().out


@pytest.mark.parametrize('width,height,rgba', [(800, 600, (255, 0, 0, 255)), (600, 800, (255, 0, 0, 0))])
def test_contact_sheet_keeps_pixels_and_uses_real_page_labels(tmp_path, modules, monkeypatch, width, height, rgba):
    from PIL import Image
    from _internal import render_deck
    core, _ = modules
    monkeypatch.setattr(core, 'CANVAS_W', width, raising=False)
    monkeypatch.setattr(core, 'CANVAS_H', height, raising=False)
    paths = [tmp_path / 'slide_02.png', tmp_path / 'slide_05.png']
    for path in paths:
        Image.new('RGBA', (width, height), rgba).save(path)
    core._contact_sheet(paths, tmp_path / 'making.png', labels=[2, 5])
    render_deck.write_contact_sheet(paths, tmp_path / 'delivery.png', width=width, height=height, labels=[2, 5])
    assert (tmp_path / 'making.png').read_bytes() == (tmp_path / 'delivery.png').read_bytes()
    with Image.open(tmp_path / 'making.png') as sheet:
        thumb_w, thumb_h = round(400 * width / max(width, height)), round(400 * height / max(width, height))
        assert sheet.size == (thumb_w * 2, thumb_h + 22)
        expected = ((255, 255), (0, 0), (0, 0)) if rgba[3] else ((24, 24),) * 3
        assert sheet.crop((0, 22, thumb_w, thumb_h + 22)).getextrema() == expected


def test_prepare_creates_no_placeholder_author_documents(tmp_path, modules):
    from state.workspace import WorkspaceState
    WorkspaceState(tmp_path)
    modules[0].prepare(tmp_path)
    assert all(not (tmp_path / name).exists() for name in (
        'design-brief.md', 'briefs', 'speech.md', 'knowledge.md', 'material.md', 'research.md', 'assets/catalog.md'))


def test_runtime_chart_dependency_comes_from_html_not_medium_prose(tmp_path, modules):
    from state.workspace import WorkspaceState
    WorkspaceState(tmp_path)
    modules[0].prepare(tmp_path)
    rows = [{'number': 1, 'text': '中文自由描述的图表'}]
    (tmp_path / 'slides/slide_01.html').write_text('<section>Discuss charts without using ECharts.</section>')
    modules[0]._ensure_authoring_runtime_dependencies(tmp_path, rows)
    assert not (tmp_path / 'assets/vendor').exists()
    (tmp_path / 'slides/slide_01.html').write_text('<script src="assets/vendor/echarts.min.js"></script>')
    modules[0]._ensure_authoring_runtime_dependencies(tmp_path, rows)
    assert (tmp_path / 'assets/vendor/echarts.min.js').is_file()


@pytest.mark.parametrize('existing_library', [False, True])
def test_missing_frozen_chart_library_is_explicit_even_with_old_workspace_library(tmp_path, modules, monkeypatch, existing_library):
    from state.workspace import WorkspaceState
    core = modules[0]
    WorkspaceState(tmp_path)
    fake_skill = tmp_path / 'absent-skill'
    monkeypatch.setattr(core, '__file__', str(fake_skill / 'scripts/_internal/deck_core.py'))
    (tmp_path / 'slides/slide_01.html').write_text('<script src="assets/vendor/echarts.min.js"></script>')
    if existing_library:
        (tmp_path / 'assets/vendor').mkdir()
        (tmp_path / 'assets/vendor/echarts.min.js').write_text('previous content')
    with pytest.raises(FileNotFoundError, match='frozen ECharts authoring dependency is missing'):
        core._ensure_authoring_runtime_dependencies(tmp_path, [{'number': 1}])


def test_shared_pages_heading_does_not_create_a_second_page_plan(tmp_path, modules):
    plan(tmp_path, 1)
    source = '# Deck\n## Pages\n### P99 — Outline only\nExplain the intended sequence.\n'
    (tmp_path / 'deck.md').write_text(source)
    packet = modules[1].owner_context(tmp_path, (1,))
    assert packet['shared'] == source.strip()
    assert [page['number'] for page in packet['pages']] == [1]
    assert packet['pages'][0]['section'] == 'Topic 1'
    assert 'P99' not in packet['pages'][0]['text']
    assert len(modules[1].read_deck(tmp_path)[1]) == 1
    assert (tmp_path / 'deck.md').read_text() == source


def test_missing_briefs_are_not_guessed_or_materialized(tmp_path, modules):
    (tmp_path / 'deck.md').write_text('# Deck\n## Pages\n### P01 — Only an outline\n')
    with pytest.raises(ValueError, match='needs separate briefs'):
        modules[1].read_deck(tmp_path)
    assert not (tmp_path / 'briefs').exists()


def test_code_fences_and_nested_headings_do_not_create_pages(tmp_path, modules):
    source = plan(tmp_path, 1) + '\n```markdown\n## Pages\n### P99 — fake\n```\n'
    (tmp_path / 'deck.md').write_text(source)
    brief = tmp_path / 'briefs/slide_01.md'
    brief.write_text(brief.read_text() + '\n## Source details\n```md\n# P02 — not a page\n```\n')
    _, pages = modules[1].read_deck(tmp_path, 1)
    assert pages[0]['number'] == 1
    assert 'P02 — not a page' in pages[0]['text']


def test_page_hash_changes_only_for_edited_section(tmp_path, modules):
    from _internal.deck_state import _plan_hash
    core, _ = modules
    plan(tmp_path)
    _, old = core._load_plans(tmp_path)
    brief = tmp_path / 'briefs/slide_01.md'
    brief.write_text(brief.read_text().replace('exact 12.5%', 'exact 12.6%'))
    _, new = core._load_plans(tmp_path)
    assert _plan_hash(old[0]) != _plan_hash(new[0])
    assert _plan_hash(old[1]) == _plan_hash(new[1])


def test_documented_examples_parse_with_one_config_and_natural_notes(tmp_path, modules):
    core, parser = modules
    text = (SKILL / 'references/planning/plan-contract.md').read_text()
    examples = re.findall(r'```markdown\n(.*?)\n```', text, re.S)
    shared = next(value for value in examples if value.startswith('# 面向听众的整册标题'))
    brief = next(value for value in examples if value.startswith('# 六次测量'))
    plan(tmp_path, 1)
    (tmp_path / 'deck.md').write_text(shared)
    (tmp_path / 'briefs/slide_01.md').write_text(brief)
    _, pages = parser.read_deck(tmp_path, 1)
    assert len(pages) == 1 and pages[0]['title'] == '六次测量，让一条关系显形'
    assert pages[0]['speech'].startswith('先请学生描述')
    assert '- 先观察' in pages[0]['speech']
    assert core._load_plans(tmp_path)[0]['language'] == 'en'


@pytest.mark.parametrize('heading', ['讲稿', 'Speaker notes'])
def test_owner_missing_page_is_explicit_and_notes_markup_remains_intact(tmp_path, modules, heading):
    from state.workspace import WorkspaceState
    WorkspaceState(tmp_path)
    plan(tmp_path, 1)
    brief = tmp_path / 'briefs/slide_01.md'
    note = ('First `quoted` line.\n\n- First item.\n  Continued item.\n\n'
            '```markdown\n## Speaker notes\nA literal code sample, not another section.\n```\n\n'
            '### Speaking cue\nKeep **this** emphasis and the paragraph spacing.')
    brief.write_text(brief.read_text().replace('## Speaker notes\nSay only note 1.',
        f'## {heading}\n{note}\n\n## Evidence appendix\nDo not include this source prose.'))
    with pytest.raises(ValueError, match='no assigned page 2'):
        modules[1].owner_context(tmp_path, (2,))
    assert modules[0]._load_plans(tmp_path)[1][0]['speech'] == note
    assert modules[0].sync_speech(tmp_path, 1)['notes_count'] == 1
    assert note in (tmp_path / 'speech.md').read_text()
    assert 'Do not include this source prose.' not in (tmp_path / 'speech.md').read_text()


@pytest.mark.parametrize('note', [
    '```markdown\nFirst paragraph.\n\n- Item stays inside this code sample.\n```',
    'Speaker notes\n\nThis is authored narration, not a label to strip.',
])
def test_narration_is_not_cleaned_as_a_guessed_wrapper(tmp_path, modules, note):
    from state.workspace import WorkspaceState
    WorkspaceState(tmp_path)
    plan(tmp_path, 1)
    (tmp_path / 'briefs/slide_01.md').write_text(f'# Topic\n## Speaker notes\n{note}\n')
    modules[0].sync_speech(tmp_path, 1)
    assert modules[0]._load_plans(tmp_path)[1][0]['speech'] == note
    assert note in (tmp_path / 'speech.md').read_text()


def test_legacy_speaker_note_field_is_not_a_second_speech_source(tmp_path, modules):
    plan(tmp_path, 1)
    brief = tmp_path / 'briefs/slide_01.md'
    brief.write_text('# Topic\n- speaker_note: Old field is only prose.\n## 本页任务\nExplain.\n')
    assert modules[1].read_deck(tmp_path)[1][0]['speech'] == ''


@pytest.mark.parametrize('page_type', ['cover', 'section-divider', 'content', 'closing', 'poster'])
def test_page_type_words_remain_prose_without_controlling_layout(tmp_path, modules, page_type):
    plan(tmp_path, 1)
    brief = tmp_path / 'briefs/slide_01.md'
    text = (f'# 任意标题，不猜页型\n- page_type: {page_type}\n'
            '- composition: not-an-enumerated-layout\n- special_layout: author-defined\n'
            '- job: ordinary prose\n- must_show: related source\n- medium: an authored visual\n'
            '- show_footer: yes\n## 本页任务\nKeep the whole explanation.\n')
    brief.write_text(text)
    deck, pages = modules[0]._load_plans(tmp_path)
    page = pages[0]
    assert page['text'] == text.strip()
    assert not {'page_type', 'composition', 'special_layout', 'show_footer', 'evidence', 'role'} & page.keys()
    html = modules[0]._slide_skeleton(page['number'])
    assert 'data-page-type=' not in html
    assert 'data-composition=' not in html and 'data-special-layout=' not in html
    assert 'page-footer' not in html and 'special-footer' not in html


def test_page_intent_and_title_do_not_change_the_shell(tmp_path, modules):
    plan(tmp_path, 1)
    brief = tmp_path / 'briefs/slide_01.md'
    brief.write_text('# Cover\n- page_type: poster\n## 本页任务\nExplain.\n')
    _, before = modules[0]._load_plans(tmp_path)
    brief.write_text('# Cover\n## 本页任务\nExplain.\n')
    _, after = modules[0]._load_plans(tmp_path)
    assert 'page_type' not in after[0]
    assert modules[0]._slide_skeleton(before[0]['number']) == modules[0]._slide_skeleton(after[0]['number'])


def test_markdown_cannot_apply_theme_or_font_routes_to_author_css(tmp_path, modules, monkeypatch):
    from state.workspace import WorkspaceState
    WorkspaceState(tmp_path)
    core, _ = modules
    plan(tmp_path, 1)
    core.prepare(tmp_path)
    css = (':root { --canvas-w: 1600px; --canvas-h: 900px; --color-accent: #135790; '
           '--font-title: "Noto Sans SC", sans-serif; --font-display-weight: 700; }\n'
           '.deck-custom { padding: 17px; }\n')
    (tmp_path / 'base.css').write_text(css)
    shared = ((tmp_path / 'deck.md').read_text() +
        '\n## Brief\n- language: zh\n- page_count: 99\n'
        '\n## Visual Direction\n- typography_route: not-a-known-route\n'
        '## Theme Tokens\n```css\n:root { --color-accent: #ff0000; --font-title: "Bogus Font"; }\n```\n'
        '## Shared Visual Primitives\n```css\n.deck-injected { color: red; }\n```\n')
    (tmp_path / 'deck.md').write_text(shared)
    font_inputs = []
    monkeypatch.setattr(core, '_read_author_geometry', lambda _root: {'width_px': 1600, 'height_px': 900})
    monkeypatch.setattr(core, '_ensure_authoring_font_bundle',
                        lambda root, **_kw: font_inputs.append((root / 'base.css').read_text()) or {})
    assert core.scaffold_from_plans(tmp_path, 1) == [1]
    assert core._load_plans(tmp_path)[0]['language'] == 'en'
    assert font_inputs == [css]
    assert (tmp_path / 'base.css').read_text() == css
    assert (tmp_path / 'deck.md').read_text() == shared
    assert not (tmp_path / '_trace/typography-route.json').exists()
    catalog = json.loads((SKILL / 'references/typography-routes.json').read_text())
    choices = catalog['routes']['tech-product']['tokens']
    assert choices['--font-title'] and choices['--font-body']
    assert not any('deck-injected' in path.read_text() for path in (tmp_path / 'slides').glob('*.html'))
