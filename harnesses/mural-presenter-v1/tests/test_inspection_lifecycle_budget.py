"""Lifetime counts are advisory; unchanged visual evidence remains bounded."""
from dataclasses import replace

from PIL import Image
import pytest

from infer import build_inspect_handlers
from runtime.tools import ToolExecutionError, WriteScope
from state.artifacts import ArtifactStore
from state.workspace import WorkspaceState
from test_native_inspect import (
    _analyzer, _author_page, _group_workspace, _make_workspace, _renderer, _slide_context,
)


def changing_renderer(html, png, canvas):
    png.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', (canvas['width_px'], canvas['height_px']),
              (len(html.read_text()) % 255, 100, 100)).save(png)
    return {'geometry': canvas, 'loaded_resources': [], 'failed_resources': []}


def consume(inspect, commit, context, **args):
    payload = inspect(context, args)
    assert commit(payload['pending_inspection']) is True
    return payload


def three_checks(tmp_path):
    workspace, artifacts = _make_workspace(tmp_path)
    inspect, commit = build_inspect_handlers(workspace, artifacts, changing_renderer, _analyzer)
    context = _slide_context(workspace)
    for i in range(3):
        _author_page(workspace, artifacts, 1, '<html><body>' + 'x' * (i + 1) + '</body></html>')
        consume(inspect, commit, context, page=1, review_stage='final', question='whole page')
    return workspace, artifacts, inspect, commit, context


def test_changed_pixels_can_be_reviewed_after_three_lifetime_checks(tmp_path):
    workspace, artifacts, inspect, commit, context = three_checks(tmp_path)
    for i in range(3):
        _author_page(workspace, artifacts, 1, '<html><body>repaired' + 'x' * i + '</body></html>')
        payload = inspect(context, {'page': 1, 'review_stage': 'final', 'question': 'repair'})
        assert payload['review_convergence']['target_page_checks'] == 1
        assert workspace.page(1).inspected_png_sha256 != workspace.page(1).png_sha256
        assert commit(payload['pending_inspection']) is True
        assert workspace.page(1).inspected_png_sha256 == workspace.page(1).png_sha256
    assert workspace.inspection_attempt_count(kind='page', page=1) == 6


def test_unchanged_pixels_cannot_evade_limit_by_question_or_stage(tmp_path):
    workspace, artifacts = _make_workspace(tmp_path)
    inspect, commit = build_inspect_handlers(workspace, artifacts, _renderer, _analyzer)
    context = _slide_context(workspace)
    consume(inspect, commit, context, page=1, review_stage='final', question='question 0')
    for stage in ['draft', 'final', 'blocking_recheck']:
        with pytest.raises(ToolExecutionError) as error:
            inspect(context, {'page': 1, 'review_stage': stage, 'question': 'new wording'})
        assert error.value.code == 'inspection_limit_reached'
    replay = inspect(context, {'page': 1, 'review_stage': 'final', 'question': 'reopen pixels', 'retain': True})
    assert replay['checked_scope'] == 'cached_pixels_replay_only'
    assert 'pending_inspection' not in replay
    assert workspace.inspection_attempt_count(kind='page', page=1) == 1


def test_unchanged_pixel_limit_survives_workspace_recovery(tmp_path):
    workspace, _ = _make_workspace(tmp_path)
    artifacts = ArtifactStore(workspace.paths.root, workspace_state=workspace)
    inspect, commit = build_inspect_handlers(workspace, artifacts, _renderer, _analyzer)
    context = _slide_context(workspace)
    consume(inspect, commit, context, page=1, review_stage='final', question='question 0')
    restored = WorkspaceState(workspace.paths.root)
    inspect, _ = build_inspect_handlers(restored, ArtifactStore(restored.paths.root, workspace_state=restored),
                                       _renderer, _analyzer)
    with pytest.raises(ToolExecutionError):
        inspect(context, {'page': 1, 'review_stage': 'final', 'question': 'after restart'})


def test_cached_review_and_pixel_replay_do_not_create_more_checks(tmp_path):
    workspace, _, inspect, _, context = three_checks(tmp_path)
    cached = inspect(context, {'page': 1, 'review_stage': 'final', 'question': 'whole page'})
    assert cached['inspection_cached']
    replay = inspect(context, {'page': 1, 'review_stage': 'final', 'question': 'whole page', 'retain': True})
    assert replay['inspection_cached'] and 'pending_inspection' not in replay
    assert workspace.inspection_attempt_count(kind='page', page=1) == 3


def test_required_first_final_is_allowed_once_after_draft(tmp_path):
    workspace, artifacts = _make_workspace(tmp_path)
    inspect, commit = build_inspect_handlers(workspace, artifacts, _renderer, _analyzer)
    context = _slide_context(workspace)
    consume(inspect, commit, context, page=1, review_stage='draft', question='draft 0')
    consume(inspect, commit, context, page=1, review_stage='final', question='final')
    with pytest.raises(ToolExecutionError):
        inspect(context, {'page': 1, 'review_stage': 'final', 'question': 'extra final'})


def test_contact_sheet_counts_are_advisory_for_member_reviews(tmp_path, monkeypatch):
    workspace, _, (inspect, commit) = _group_workspace(tmp_path, monkeypatch, inspected=False)
    context = _slide_context(workspace, (1, 2))
    consume(inspect, commit, context, page=[1, 2], review_stage='draft', question='rhythm')
    for page in (1, 2):
        consume(inspect, commit, context, page=page, review_stage='final')
        assert workspace.inspection_attempt_count(kind='page', page=page, include_groups=True) == 2
    with pytest.raises(ToolExecutionError):
        inspect(context, {'page': [1, 2], 'review_stage': 'draft', 'question': 'one more unchanged sheet'})


def test_orchestrator_can_refresh_current_deck_sheet_after_author_repair(tmp_path, monkeypatch):
    workspace, artifacts, _ = _group_workspace(tmp_path, monkeypatch, inspected=False)
    inspect, commit = build_inspect_handlers(workspace, artifacts, changing_renderer, _analyzer)
    author = _slide_context(workspace, (1, 2))
    orchestrator = replace(author, owner_id='orchestrator', role='orchestrator',
                           read_scopes=(WriteScope('renders'),))
    for page in (1, 2):
        consume(inspect, commit, author, page=page, review_stage='final')
    consume(inspect, commit, orchestrator, page=[1, 2], review_stage='draft', question='deck')
    for i in range(3):
        _author_page(workspace, artifacts, 1, '<html><body>repair' + 'x' * i + '</body></html>')
        consume(inspect, commit, author, page=1, review_stage='final', question='repair')
        sheet = inspect(orchestrator, {'page': [1, 2], 'review_stage': 'draft', 'question': 'deck'})
        assert workspace.group_inspection_receipt('orchestrator', (1, 2)) is None
        assert commit(sheet['pending_inspection']) is True
        assert workspace.group_inspection_receipt('orchestrator', (1, 2)) is not None


def test_html_only_cached_rebind_does_not_consume_extra_checks(tmp_path):
    workspace, artifacts = _make_workspace(tmp_path)
    inspect, commit = build_inspect_handlers(workspace, artifacts, _renderer, _analyzer)
    context = _slide_context(workspace)
    consume(inspect, commit, context, page=1, review_stage='final')
    _author_page(workspace, artifacts, 1, '<html><body>v1</body><!-- no pixel change --></html>')
    assert inspect(context, {'page': 1, 'review_stage': 'final'})['inspection_cached']
    assert workspace.inspection_attempt_count(kind='page', page=1) == 1
