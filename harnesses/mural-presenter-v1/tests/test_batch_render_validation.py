import copy
import json

import pytest

from export.delivery import DeliveryError, DeliveryExporter
from state.artifacts import ArtifactStateError
from state.workspace import WorkspaceStateError
from test_delivery_training import _ready_workspace


def measured_geometry(workspace, monkeypatch):
    canvas = workspace.require_geometry()
    calls = []

    def read(root):
        calls.append(root)
        return copy.deepcopy(canvas)

    monkeypatch.setattr(workspace, '_geometry_reader', read)
    return canvas, calls


def test_ready_batch_measures_shared_canvas_once(tmp_path, monkeypatch):
    workspace, _, _ = _ready_workspace(tmp_path, pages=3)
    _, calls = measured_geometry(workspace, monkeypatch)
    workspace.assert_ready_resources([1, 2, 3])
    assert len(calls) == 1
    workspace.assert_ready_resources([1, 2, 3])
    assert len(calls) == 2


def test_delivery_batch_measures_shared_canvas_once(tmp_path, monkeypatch):
    workspace, store, issues = _ready_workspace(tmp_path, pages=3)
    _, calls = measured_geometry(workspace, monkeypatch)
    exporter = DeliveryExporter(workspace, store, issues)
    assert len(exporter._validate_pages()) == 3
    assert len(calls) == 1


def test_delivery_publication_remeasures_canvas_at_each_boundary(tmp_path, monkeypatch):
    workspace, store, issues = _ready_workspace(tmp_path, pages=3)
    _, calls = measured_geometry(workspace, monkeypatch)
    exporter = DeliveryExporter(workspace, store, issues)
    manifest = exporter.export(include_pdf=False, include_pptx=False, include_zip=False)
    assert len(calls) == 2
    exporter.write_result(manifest)
    assert len(calls) == 3


def test_shared_canvas_still_checks_each_page_canvas(tmp_path, monkeypatch):
    workspace, store, issues = _ready_workspace(tmp_path, pages=3)
    _, calls = measured_geometry(workspace, monkeypatch)
    state = json.loads(store.path.read_text())
    state['render_receipts']['2']['canvas']['width_px'] += 1
    store.path.write_text(json.dumps(state))
    with pytest.raises(DeliveryError, match='page 2 canvas mismatch'):
        DeliveryExporter(workspace, store, issues)._validate_pages()
    assert len(calls) == 1


def test_canvas_measurement_failure_reports_all_affected_pages(tmp_path, monkeypatch):
    workspace, _, _ = _ready_workspace(tmp_path, pages=3)
    calls = []

    def failed(root):
        calls.append(root)
        raise ValueError('css_geometry_dependency_failed')

    monkeypatch.setattr(workspace, '_geometry_reader', failed)
    with pytest.raises(WorkspaceStateError) as exc:
        workspace.assert_ready_resources([1, 2, 3])
    assert len(calls) == 1
    assert [row['page'] for row in exc.value.diagnostics['page_issues']] == [1, 2, 3]


def test_shared_canvas_does_not_mask_css_change_after_measurement(tmp_path, monkeypatch):
    workspace, store, issues = _ready_workspace(tmp_path, pages=3)
    _, calls = measured_geometry(workspace, monkeypatch)
    validate = store.validate_render_receipt

    def changed(page, **kwargs):
        if page == 1:
            css = workspace.paths.path('base_css')
            css.write_text(css.read_text() + '\n:root { --canvas-w: 1800px; }')
        return validate(page, **kwargs)

    monkeypatch.setattr(store, 'validate_render_receipt', changed)
    with pytest.raises(DeliveryError, match='visual resources changed after render'):
        DeliveryExporter(workspace, store, issues)._validate_pages()
    assert len(calls) == 1


def test_single_page_validation_still_reads_current_canvas(tmp_path, monkeypatch):
    workspace, store, _ = _ready_workspace(tmp_path, pages=1)
    canvas, calls = measured_geometry(workspace, monkeypatch)
    store.validate_render_receipt(1)
    canvas['width_px'] += 1
    with pytest.raises(ArtifactStateError, match='canvas mismatch'):
        store.validate_render_receipt(1)
    assert len(calls) == 2
