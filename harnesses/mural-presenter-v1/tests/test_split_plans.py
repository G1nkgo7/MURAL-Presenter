"""独立分页计划不是镜像；身份、修订恢复和讲稿均使用实际作者文件。"""
from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path

import pytest

from test_revision_state import deck, command, prepare, ready
from test_v1_font_pipeline import pinned_font_sources, education_font_sources, _author_font_workspace


@pytest.fixture
def parser(monkeypatch):
    skill = Path(__file__).resolve().parents[3] / 'skills/mural-presenter-v1'
    monkeypatch.syspath_prepend(str(skill / 'scripts'))
    return importlib.import_module('_internal.deck_plan')


def _split(root, count=3):
    shared = f'# Whole deck\n## Brief\n- language: zh\n- page_count: {count}\n## Visual Direction\nA focused shared visual argument.\n'
    (root / 'deck.md').write_text(shared)
    (root / 'briefs').mkdir(exist_ok=True)
    for number in range(1, count + 1):
        (root / 'briefs' / f'slide_{number:02d}.md').write_text(
            f'# P{number:02d} — Topic {number}\n- job: explain\n- must_show: Evidence {number}\n'
            f'- medium: chart\n- speaker_note: Note {number}.\n  Second line {number}.\n\n'
            f'## Evidence\nComplete source paragraph for page {number}, not just a short field.\n'
            '## Constraints\nDo not invent a value.\n')
    return shared


def test_split_plan_owner_packet_preserves_full_evidence_and_actual_locator(tmp_path, parser):
    shared = _split(tmp_path)
    text, pages = parser.read_deck(tmp_path, 3)
    assert text == shared.rstrip() and len(pages) == 3
    packet = parser.owner_context(tmp_path, (2,))
    assert packet['source'] == 'deck.md'
    assert packet['pages'][0]['source'] == 'briefs/slide_02.md'
    assert packet['pages'][0]['section'] == 'P02'
    assert 'Complete source paragraph for page 2' in packet['pages'][0]['text']
    assert 'Do not invent a value.' in packet['pages'][0]['text']
    assert 'page 1' not in repr(packet) and 'page 3' not in repr(packet)
    assert pages[1]['fields']['speaker_note'] == 'Note 2.\nSecond line 2.'


def test_shared_owner_context_needs_no_page_briefs(tmp_path, parser, monkeypatch):
    (tmp_path / 'deck.md').write_text('# Direction\n## Brief\n- page_count: 3\n')
    def reject_glob(*_args, **_kwargs):
        raise AssertionError('shared context must not enumerate briefs')
    monkeypatch.setattr(Path, 'glob', reject_glob)
    packet = parser.owner_context(tmp_path, ())
    assert packet['pages'] == [] and 'Direction' in packet['shared']
    assert not (tmp_path / 'briefs').exists()


def test_blank_noncanonical_brief_residue_does_not_block_real_pages(tmp_path, parser):
    _split(tmp_path)
    for name in ('slide_1.md', 'slide_17.md again.md', 'discarded-note.md'):
        (tmp_path / 'briefs' / name).write_text(' \n')
    _, pages = parser.read_deck(tmp_path, 3)
    assert [page['number'] for page in pages] == [1, 2, 3]
    assert (tmp_path / 'briefs/slide_1.md').read_text() == ' \n'


def test_nonempty_noncanonical_brief_is_not_silently_discarded(tmp_path, parser):
    _split(tmp_path)
    (tmp_path / 'briefs/slide_1.md').write_text('# Actual required page\nKeep this evidence.\n')
    with pytest.raises(ValueError, match='canonical slide_NN.md'):
        parser.read_deck(tmp_path, 3)


def test_empty_canonical_brief_still_fails(tmp_path, parser):
    _split(tmp_path)
    (tmp_path / 'briefs/slide_02.md').write_text(' \n')
    with pytest.raises(ValueError):
        parser.read_deck(tmp_path, 3)


def test_blank_noncanonical_brief_does_not_replace_a_missing_page(tmp_path, parser):
    _split(tmp_path)
    (tmp_path / 'briefs/slide_02.md').unlink()
    (tmp_path / 'briefs/slide_2.md').write_text(' \n')
    with pytest.raises(ValueError, match='contiguous'):
        parser.read_deck(tmp_path, 3)


@pytest.mark.parametrize('unrelated', ['missing', 'invalid'])
def test_assigned_context_reads_only_its_own_brief(tmp_path, parser, monkeypatch, unrelated):
    _split(tmp_path)
    path = tmp_path / 'briefs/slide_02.md'
    if unrelated == 'missing':
        path.unlink()
    else:
        path.write_text('not a valid page brief')
    def reject_glob(*_args, **_kwargs):
        raise AssertionError('assigned context must not enumerate briefs')
    monkeypatch.setattr(Path, 'glob', reject_glob)
    packet = parser.owner_context(tmp_path, (1,))
    assert [page['number'] for page in packet['pages']] == [1]
    assert packet['pages'][0]['source'] == 'briefs/slide_01.md'


def test_owner_context_still_requires_its_assigned_brief(tmp_path, parser):
    _split(tmp_path)
    (tmp_path / 'briefs/slide_02.md').unlink()
    with pytest.raises(ValueError, match='assigned page 2'):
        parser.owner_context(tmp_path, (2,))
    with pytest.raises(ValueError, match='contiguous'):
        parser.owner_context(tmp_path, None)


@pytest.mark.parametrize('change, message', [
    ('mixed', 'unsupported_plan_layout'), ('missing', 'contiguous'), ('heading', 'disagree'),
    ('extra-heading', 'one # PNN'), ('page-count', 'disagrees'), ('filename', 'disagree'),
])
def test_split_plan_does_not_guess_or_merge_authoring_representations(tmp_path, parser, change, message):
    _split(tmp_path)
    path = tmp_path / 'briefs/slide_02.md'
    if change == 'mixed':
        with (tmp_path / 'deck.md').open('a') as handle:
            handle.write('\n## Pages\n### P01 — Inline duplicate\n')
    elif change == 'missing':
        path.unlink()
    elif change == 'heading':
        path.write_text(path.read_text().replace('# P02', '# P09', 1))
    elif change == 'extra-heading':
        path.write_text(path.read_text() + '\n# Another page\n')
    elif change == 'page-count':
        deck_path = tmp_path / 'deck.md'
        deck_path.write_text(deck_path.read_text().replace('page_count: 3', 'page_count: 4'))
    else:
        path.rename(tmp_path / 'briefs/slide_002.md')
    with pytest.raises(ValueError, match=message):
        parser.read_deck(tmp_path)


def test_split_scaffold_has_one_plan_source_tracks_page_hashes_and_defers_speech(
    tmp_path, education_font_sources, parser,
):
    from state.workspace import WorkspaceState
    core = _author_font_workspace(tmp_path, education_font_sources)
    shared = _split(tmp_path)
    before = {p.name: p.read_bytes() for p in (tmp_path / 'briefs').glob('*.md')}
    state = WorkspaceState(tmp_path)
    state.register_pages((1, 2, 3))
    core.scaffold_from_plans(tmp_path, 3)
    assert not (tmp_path / 'speech.md').exists()
    assert (tmp_path / 'deck.md').read_text() == shared
    assert {p.name: p.read_bytes() for p in (tmp_path / 'briefs').glob('*.md')} == before
    deck_state = importlib.import_module(core.__package__ + '.deck_state')
    deck_state.compile_deck_state(tmp_path, *core._load_plans(tmp_path, 3))
    index_path = tmp_path / '_state/page-index.json'
    old = json.loads(index_path.read_text())
    assert [row['plan_path'] for row in old['pages']] == [f'briefs/slide_{p:02d}.md' for p in (1, 2, 3)]
    for row in old['pages']:
        assert row['source_plan_sha256'] == hashlib.sha256((tmp_path / row['plan_path']).read_text().strip().encode()).hexdigest()
    second = tmp_path / 'briefs/slide_02.md'
    second.write_text(second.read_text().replace('Evidence 2', 'Updated evidence 2'))
    deck_state.compile_deck_state(tmp_path, *core._load_plans(tmp_path, 3))
    new = json.loads(index_path.read_text())
    assert new['plan_epoch'] > old['plan_epoch']
    assert new['pages'][0]['source_plan_sha256'] == old['pages'][0]['source_plan_sha256']
    assert new['pages'][1]['source_plan_sha256'] != old['pages'][1]['source_plan_sha256']
    assert new['pages'][2]['source_plan_sha256'] == old['pages'][2]['source_plan_sha256']


@pytest.fixture
def split_deck(deck):
    root = deck[0].paths.root
    _split(root)
    from _internal.deck_core import _load_plans
    from _internal.deck_state import compile_deck_state
    compile_deck_state(root, *_load_plans(root, 3))
    return deck


@pytest.mark.parametrize('kind', ['delete_page', 'insert_page', 'move_page'])
@pytest.mark.parametrize('heading_separator', [' ', '  ', '\t'], ids=['space', 'spaces', 'tab'])
def test_split_structure_edits_only_real_briefs_and_preserve_identity(split_deck, parser, kind, heading_separator):
    state = split_deck[0]
    for brief in (state.paths.root / 'briefs').glob('slide_*.md'):
        brief.write_text(brief.read_text().replace('# ', '#' + heading_separator, 1))
    old = [page.page_uid for page in state.pages()]
    operation = {'kind': kind}
    if kind == 'delete_page':
        operation['targets'] = [old[1]]
        flags, expected = f'--uid {old[1]}', [old[0], old[2]]
    elif kind == 'insert_page':
        operation.update(after_page_uid=old[0], new_page_uid='pg_inserted')
        flags, expected = f'--after {old[0]} --new-uid pg_inserted', [old[0], 'pg_inserted', old[1], old[2]]
    else:
        operation.update(targets=[old[2]], before_page_uid=old[0])
        flags, expected = f'--uid {old[2]} --before {old[0]}', [old[2], old[0], old[1]]
    prepare(split_deck, operation)
    command(split_deck, kind.replace('_', '-'), '--revision-id revision-1 --operation-id op-1 ' + flags)
    assert [page.page_uid for page in state.pages()] == expected
    _, plans = parser.read_deck(state.paths.root, len(expected))
    assert '## Pages' not in state.paths.path('deck').read_text()
    assert len(list((state.paths.root / 'briefs').glob('slide_*.md'))) == len(expected)
    for number, uid in enumerate(expected, 1):
        separator = heading_separator if uid in old else ' '
        assert plans[number - 1]['text'].startswith(f'#{separator}P{number:02d} —')
        if uid in old:
            assert f'Complete source paragraph for page {old.index(uid) + 1}' in plans[number - 1]['text']
    index = json.loads((state.paths.state / 'page-index.json').read_text())
    assert [row['plan_path'] for row in index['pages']] == [f'briefs/slide_{p:02d}.md' for p in range(1, len(expected) + 1)]
    marker = json.loads((state.paths.state / 'structural-transaction.json').read_text())
    assert 'briefs/slide_02.md' in marker['files']


def test_failed_structure_restores_briefs_without_mirror(split_deck):
    from state.revisions import RevisionTransaction
    state, artifacts, owners, _ = split_deck
    prepare(split_deck, {'kind': 'delete_page', 'targets': [state.page(2).page_uid]})
    before = {p.name: p.read_bytes() for p in (state.paths.root / 'briefs').glob('*.md')}
    def interrupt(*_args):
        (state.paths.root / 'briefs/slide_02.md').unlink()
        (state.paths.root / 'briefs/slide_04.md').write_text('# P04 — New')
        raise RuntimeError('simulated structure interruption')
    with pytest.raises(RuntimeError, match='simulated'):
        RevisionTransaction(state, artifacts, owners).run(interrupt)
    assert {p.name: p.read_bytes() for p in (state.paths.root / 'briefs').glob('*.md')} == before
    assert '## Pages' not in state.paths.path('deck').read_text()


def test_final_export_derives_notes_and_staging_does_not_modify_author_files(split_deck, tmp_path, capsys):
    from export.delivery import DeliveryExporter, _validated_export_snapshot
    from qc.routing import IssueLedger
    state, artifacts, *_ = split_deck
    root = state.paths.root
    ready(split_deck)
    sources = [root / 'base.css', *sorted((root / 'slides').glob('*.html')), *sorted((root / 'renders').glob('*.png'))]
    before = {p: p.read_bytes() for p in sources}
    exporter = DeliveryExporter(state, artifacts, IssueLedger(root))
    manifest = exporter.export(include_pdf=False, include_pptx=False, include_zip=False)
    assert manifest.speech['status'] == 'complete' and manifest.speech['notes_count'] == 3
    assert manifest.outputs['speech']['path'] == 'speech.md'
    speech = root / 'speech.md'
    assert 'Note 2.' in speech.read_text() and 'Complete source paragraph' not in speech.read_text()
    brief = root / 'briefs/slide_02.md'
    brief.write_text(brief.read_text().replace('Note 2.', 'Revised note 2.'))
    old_speech = speech.read_bytes()
    staging = tmp_path / '_runtime/tmp/export-fixture'
    _validated_export_snapshot(state.paths, staging)
    assert 'Revised note 2.' in (staging / 'speech.md').read_text()
    assert speech.read_bytes() == old_speech
    exporter.export(include_pdf=False, include_pptx=False, include_zip=False)
    assert 'Revised note 2.' in speech.read_text()
    current = speech.read_bytes()
    exporter.export(include_pdf=False, include_pptx=False, include_zip=False)
    assert speech.read_bytes() == current
    for path in (root / 'briefs').glob('slide_*.md'):
        path.write_text('\n'.join(line for line in path.read_text().splitlines()
                                  if not line.startswith('- speaker_note:')) + '\n')
    manifest = exporter.export(include_pdf=False, include_pptx=False, include_zip=False)
    assert not speech.exists() and 'speech' not in manifest.outputs
    assert manifest.speech['status'] == 'omitted' and manifest.speech['missing_pages'] == [1, 2, 3]
    assert manifest.speech['omission_reason'] == 'no_valid_speaker_notes'
    assert {p: p.read_bytes() for p in sources} == before
    assert capsys.readouterr().out == ''


def test_speech_export_uses_pinned_cli_and_its_coverage_receipt(tmp_path, capsys, monkeypatch):
    from export.delivery import _sync_speech
    from runtime.shared_objects import SharedSkillStore
    from state.workspace import WorkspaceState
    source = tmp_path / 'skills/previous'
    (source / 'scripts/_internal').mkdir(parents=True)
    (source / 'SKILL.md').write_text('# Previous fixed Skill\n')
    (source / 'scripts/_internal/__init__.py').write_text('')
    core_path = source / 'scripts/_internal/deck_core.py'
    core_path.write_text('def sync_speech(root, expected=None):\n'
        '    (root / "speech.md").write_text("fixed note")\n    print("status:PASS")\n'
        '    print(\'[speech] {"status":"complete","notes_count":1,"page_count":1,"missing_pages":[]}\')\n')
    (source / 'scripts/orchestrator.py').write_text('from pathlib import Path\nimport sys\n'
        'from _internal.deck_core import sync_speech\nsync_speech(Path(sys.argv[2]))\n')
    monkeypatch.setenv('MURAL_STUDIO_DATA_DIR', str(tmp_path / 'store'))
    state = WorkspaceState(tmp_path / 'deck')
    state.paths.path('deck').write_text('# Deck\n')
    SharedSkillStore.for_workspace(state.paths.root).pin(state.paths, source, skill_name='previous')
    core_path.write_text('raise RuntimeError("mutable source must not be used")\n')
    receipt = _sync_speech(state.paths)
    assert receipt['status'] == 'complete' and receipt['notes_count'] == 1
    assert state.paths.path('speech').read_text() == 'fixed note'
    assert capsys.readouterr().out == ''
