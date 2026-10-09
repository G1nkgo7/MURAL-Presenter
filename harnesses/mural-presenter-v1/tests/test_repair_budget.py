"""已消费页面的修复批次按稳定页面身份和真实用户修订持久计数。"""
from __future__ import annotations

from geometry_fixture import geometry, write_geometry

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


from runtime.production import ProductionToolServices
from owner.registry import OwnerRegistry
from runtime.tools import ToolCall, ToolContext, ToolExecutionError, ToolRuntime, WriteScope, register_tools, tool_specs
from state.artifacts import ArtifactStateError, ArtifactStore
from state.workspace import PageLifecycle, WorkspaceState, WorkspaceStateError, file_sha256


REASON = "页脚与底部正文重叠，遮挡阅读；增加内容底部留白，使页脚与正文分离。"


def _services(root: Path, revision=None):
    workspace = WorkspaceState(root)
    if not workspace.paths.path("base_css").exists():
        write_geometry(root, geometry(width_px=1600, height_px=900))
    services = object.__new__(ProductionToolServices)
    services.workspace = workspace
    services.artifacts = ArtifactStore(root, workspace_state=workspace)
    services.owners = OwnerRegistry(root, deck_id=workspace.deck_id)
    if services.owners.page_owner(1) is None:
        services.owners.accept(role="slide", pages=[1], group_id="repair-test")
    services.run_spec = SimpleNamespace(revision_request=revision)
    return services


def _context(services, **metadata):
    return ToolContext(owner_id=services.owners.page_owner(1).owner_id, role="slide",
        write_scopes=(WriteScope("slides/slide_01.html", recursive=False),),
        metadata={"pages": [1], **metadata})


def _write(services, content, reason=None, context=None):
    arguments = {"path": "slides/slide_01.html", "content": content}
    if reason is not None:
        arguments["repair_reason"] = reason
    return services.write_file(context or _context(services), arguments)


def _inspect(services, label):
    workspace = services.workspace
    png = workspace.paths.page_png(1)
    png.write_bytes(b"inspection-fixture-" + label.encode())
    workspace.mark_rendered(1, html_sha256=file_sha256(workspace.paths.page_html(1)),
        png_sha256=file_sha256(png))
    workspace.mark_inspected(1, inspection_id=label)


def _budgets(services):
    return json.loads(services.workspace.path.read_text())["pages"]["1"].get("repair_budgets", {})


def _checked(root):
    services = _services(root)
    _write(services, "draft")
    _inspect(services, "draft-check")
    return services


def test_first_draft_and_identical_writes_do_not_consume_budget(tmp_path):
    services = _services(tmp_path)
    _write(services, "first")
    _write(services, "draft")
    assert _budgets(services) == {}
    _inspect(services, "draft-check")
    _write(services, "draft")
    assert _budgets(services) == {}
    assert services.workspace.page(1).lifecycle == PageLifecycle.INSPECTED


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_checked_page_requires_explicit_repair_reason(tmp_path, reason):
    services = _checked(tmp_path)
    before = services.workspace.page(1).to_dict()
    with pytest.raises(ToolExecutionError) as error:
        _write(services, "changed", reason)
    assert error.value.code == "repair_reason_required"
    assert services.workspace.page(1).to_dict() == before
    assert _budgets(services) == {}


@pytest.mark.parametrize("reason", ["改", "x" * 600, "真实问题与修改依据。" * 1000], ids=["short", "600", "long"])
def test_repair_reason_has_no_prose_length_gate(tmp_path, reason):
    services = _checked(tmp_path)
    _write(services, "changed", reason)
    assert services.workspace.paths.page_html(1).read_text() == "changed"
    assert services.workspace.page(1).lifecycle == PageLifecycle.AUTHORED
    assert next(iter(_budgets(services).values()))["batches"][-1]["repair_reason"] == reason


def test_rerender_without_consumption_does_not_erase_checked_history(tmp_path):
    services = _checked(tmp_path)
    workspace = services.workspace
    workspace.mark_rendered(1, html_sha256=file_sha256(workspace.paths.page_html(1)),
        png_sha256=file_sha256(workspace.paths.page_png(1)))
    with pytest.raises(ToolExecutionError, match="repair_reason"):
        _write(services, "changed")


@pytest.mark.parametrize("tool", ["write_file", "patch"])
def test_third_fourth_fifth_batches_write_and_keep_exact_inspection_contract(tmp_path, tool):
    services = _checked(tmp_path)
    workspace = services.workspace
    page_uid = workspace.page(1).page_uid
    runtime = ToolRuntime()
    register_tools(runtime, {tool: getattr(services, tool)}, require_complete=False)
    for number in range(1, 6):
        before = workspace.page(1).to_dict()
        args = {"path": "slides/slide_01.html", "repair_reason": REASON}
        args.update({"content": f"repair-{number}"} if tool == "write_file" else
            {"old_text": workspace.paths.page_html(1).read_text(), "new_text": f"repair-{number}"})
        result = runtime.execute(_context(services, group_id="new-group", attempt=number),
            ToolCall(f"repair-{number}", tool, args))
        assert result.ok, result.content
        assert workspace.paths.page_html(1).read_text() == f"repair-{number}"
        assert workspace.page(1).page_uid == page_uid
        assert workspace.page(1).lifecycle == PageLifecycle.AUTHORED
        with pytest.raises(WorkspaceStateError):
            workspace.mark_ready(1)
        with pytest.raises(WorkspaceStateError):
            workspace.mark_inspected(1, inspection_id="old-version", expected=before)
        budget = next(iter(_budgets(services).values()))
        assert len(budget["batches"]) == number and budget["open"]
        assert budget["batches"][-1] == {
            "repair_reason": REASON,
            "base_html_sha256": before["html_sha256"],
            "base_png_sha256": before["png_sha256"],
        }
        _inspect(services, f"check-{number}")
        budget = next(iter(_budgets(services).values()))
        assert not budget["open"]
        assert budget["batches"][-1]["inspection_id"] == f"check-{number}"
        assert budget["batches"][-1]["html_sha256"] == file_sha256(workspace.paths.page_html(1))
        assert budget["batches"][-1]["png_sha256"] == file_sha256(workspace.paths.page_png(1))


def test_later_batches_still_require_reason_without_hard_maximum(tmp_path):
    services = _checked(tmp_path)
    for number in range(1, 4):
        _write(services, f"repair-{number}", REASON)
        _inspect(services, f"check-{number}")
    before = services.workspace.page(1).to_dict()
    with pytest.raises(ToolExecutionError) as error:
        _write(services, "missing-reason")
    assert error.value.code == "repair_reason_required"
    detail = error.value.diagnostics
    assert detail["batches_used"] == 3
    assert "max_batches" not in detail
    assert detail["page_uid"] == before["page_uid"]
    assert detail["html_sha256"] == before["html_sha256"]
    assert detail["png_sha256"] == before["png_sha256"]
    assert services.workspace.page(1).to_dict() == before
    assert len(next(iter(_budgets(services).values()))["batches"]) == 3


def test_pending_inspection_multiple_patches_and_resume_remain_one_batch(tmp_path):
    services = _checked(tmp_path)
    _write(services, "repair-a", REASON)
    services = _services(tmp_path)
    _write(services, "repair-a")  # Provider 重放相同产物。
    services.patch(_context(services, group_id="replacement"), {"path": "slides/slide_01.html",
        "old_text": "repair-a", "new_text": "repair-b"})
    _write(services, "repair-c")
    budget = next(iter(_budgets(services).values()))
    assert len(budget["batches"]) == 1 and budget["open"] is True
    _inspect(services, "check-1")
    _write(services, "repair-d", REASON)
    _inspect(services, "check-2")
    for number in range(3, 6):
        services = _services(tmp_path)
        _write(services, f"repair-{number}", REASON)
        assert len(next(iter(_budgets(services).values()))["batches"]) == number
        _inspect(services, f"check-{number}")


def test_process_exit_immediately_after_commit_preserves_batch(tmp_path):
    _checked(tmp_path)
    tests = Path(__file__).resolve().parent
    result = subprocess.run([sys.executable, "-c", """
import os,sys
from test_repair_budget import _services, _write, REASON
services = _services(__import__('pathlib').Path(sys.argv[1]))
commit = services._commit_slide_if_needed
def stop_after_commit(*args):
    commit(*args)
    os._exit(17)
services._commit_slide_if_needed = stop_after_commit
_write(services, 'committed-before-crash', REASON)
""", str(tmp_path)], env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join((str(tests), str(tests.parent)))}, timeout=30)
    assert result.returncode == 17
    resumed = _services(tmp_path)
    budget = next(iter(_budgets(resumed).values()))
    assert len(budget["batches"]) == 1 and budget["open"]
    assert resumed.workspace.page(1).authored_html_sha256 == file_sha256(resumed.workspace.paths.page_html(1))
    _write(resumed, "committed-before-crash")
    _inspect(resumed, "after-crash-check")
    _write(resumed, "second-batch", REASON)
    _inspect(resumed, "second-check")
    resumed = _services(tmp_path)
    _write(resumed, "third-batch", REASON)
    assert len(next(iter(_budgets(resumed).values()))["batches"]) == 3


def test_failed_inspection_does_not_close_repair_batch(tmp_path):
    services = _checked(tmp_path)
    _write(services, "repair-in-progress", REASON)
    with pytest.raises(WorkspaceStateError):
        services.workspace.mark_inspected(1, inspection_id="stale-pixel-attempt")
    _write(services, "same-batch-continued")
    budget = next(iter(_budgets(services).values()))
    assert len(budget["batches"]) == 1 and budget["open"]


def test_write_failure_does_not_charge_batch(tmp_path, monkeypatch):
    services = _checked(tmp_path)
    before = services.workspace.page(1).to_dict()
    def fail_write(*_args):
        raise OSError("injected write failure")
    with monkeypatch.context() as patcher:
        patcher.setattr("runtime.production._atomic_text_write", fail_write)
        with pytest.raises(OSError, match="injected"):
            _write(services, "failed-write", REASON)
    assert services.workspace.page(1).to_dict() == before
    assert _budgets(services) == {}
    _write(services, "successful-write", REASON)
    assert len(next(iter(_budgets(services).values()))["batches"]) == 1


def test_actual_owner_transfer_does_not_reset_budget(tmp_path):
    services = _checked(tmp_path)
    for number in (1, 2):
        _write(services, f"repair-{number}", REASON)
        _inspect(services, f"check-{number}")
    services.artifacts.transfer_page_owner(1, owner_id="new-slide-owner")
    with services.owners._locked_state() as state:
        state["owners"]["repair-test"]["owner_id"] = "new-slide-owner"
        services.owners._write_state(state)
    replacement = ToolContext(owner_id="new-slide-owner", role="slide",
        write_scopes=(WriteScope("slides/slide_01.html", recursive=False),),
        metadata={"pages": [1], "group_id": "replacement", "attempt": 999})
    _write(services, "third-batch", REASON, context=replacement)
    assert len(next(iter(_budgets(services).values()))["batches"]) == 3


def test_commit_failure_rolls_back_repair_count_and_page_state(tmp_path, monkeypatch):
    services = _checked(tmp_path)
    before = services.workspace.page(1).to_dict()
    original = services.artifacts._write_state
    failed = False

    def fail_commit(state):
        nonlocal failed
        if not failed and state["events"][-1]["type"] == "artifacts_committed":
            failed = True
            raise ArtifactStateError("injected artifact persistence failure")
        return original(state)

    with monkeypatch.context() as patcher:
        patcher.setattr(services.artifacts, "_write_state", fail_commit)
        with pytest.raises(ToolExecutionError) as error:
            _write(services, "failed-repair", REASON)
        assert error.value.code == "slide_artifact_commit_failed"
    assert services.workspace.page(1).to_dict() == before
    assert _budgets(services) == {}
    _write(services, "successful-repair", REASON)
    assert len(next(iter(_budgets(services).values()))["batches"]) == 1


def test_real_user_revision_has_own_budget_but_original_budget_is_not_reset(tmp_path):
    services = _checked(tmp_path)
    for number in (1, 2):
        _write(services, f"repair-{number}", REASON)
        _inspect(services, f"check-{number}")
    revision = {"parent_deck_id": 7, "revision_no": 1, "instruction": "更新页脚说明", "in_place": True}
    revised = _services(tmp_path, revision)
    _write(revised, "user-revision", REASON)
    _inspect(revised, "revision-check")
    budgets = _budgets(revised)
    assert sorted(len(b["batches"]) for b in budgets.values()) == [1, 2]
    resumed = _services(tmp_path, dict(revision))
    _write(resumed, "user-revision-repair-2", REASON)
    _inspect(resumed, "revision-check-2")
    original = _services(tmp_path)
    _write(original, "original-revision-continued", REASON)
    assert sorted(len(b["batches"]) for b in _budgets(original).values()) == [2, 3]


def test_repair_reason_is_optional_in_both_advertised_write_schemas():
    for tool in ("write_file", "patch"):
        schema = tool_specs()[tool].input_schema
        assert "repair_reason" not in schema["required"]
        assert schema["properties"]["repair_reason"]["type"] == "string"
        assert "maxLength" not in schema["properties"]["repair_reason"]
