"""Critic 故障只补真实缺失评审，不重新制作或伪造训练准入。"""
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PIL import Image

from qc.extrinsic import CriticClient, ExternalVisualCritic
from qc.review_hook import InspectionClosed
from runtime.config import RunMode
from runtime.session import TransportResponse
from runtime.skill_runtime import SkillRuntime, SkillRuntimeError
from state.workspace import PageLifecycle, WorkspaceState, file_sha256
from test_skill_runtime import ScriptedDeckTransport, _execute, _response, _run_spec, _text


def test_completion_is_bounded_and_respects_current_hash_and_prior_success(tmp_path):
    image = tmp_path / "page.png"
    Image.new("RGB", (32, 18), "navy").save(image)
    closed = InspectionClosed(agent_id="slide-owner", role="slide", page=1, page_uid="p1",
        inspection_id="i1", html_sha256="h1", png_sha256=file_sha256(image),
        png_path=image.name, workspace_root=str(tmp_path), scope_key="scope1")
    replies = iter(["", "", "未见明显问题", "问题｜遮挡\n定位｜标题\n改法｜调整"])
    calls, records = [], []
    client = CriticClient(invoker=lambda *_: calls.append(1) or {"body": next(replies)}, critic_model="test")
    with ThreadPoolExecutor(max_workers=1) as executor:
        def coordinator():
            return ExternalVisualCritic(client=client, executor=executor, prior_records=records,
                result_sink=lambda result, _disposition: records.append(result.to_audit_dict()))
        critic = coordinator()
        assert not critic.review(closed)["completed"]
        for _ in range(4):
            critic.complete_missing(closed)
            critic.settle_pending()
        assert len(calls) == 2 and not records[-1]["completed"]
        resumed = coordinator()
        resumed.complete_missing(closed)
        resumed.settle_pending()
        assert len(calls) == 3 and records[-1]["completed"] and records[-1]["body"] == "未见明显问题"
        resumed = coordinator()
        resumed.complete_missing(closed)
        resumed.settle_pending()
        assert len(calls) == 3
        Image.new("RGB", (32, 18), "orange").save(image)
        current = replace(closed, inspection_id="i2", html_sha256="h2", png_sha256=file_sha256(image))
        resumed.complete_missing(current)
        resumed.settle_pending()
        assert len(calls) == 4 and records[-1]["completed"]
        assert "问题｜遮挡" in records[-1]["body"] and records[-1]["inspection_id"] == "i2"


class EmptyCriticTransport(ScriptedDeckTransport):
    def complete(self, channel, request, **kwargs):
        if request.request_kind == "critic_visual":
            self.requests.append(request)
            return TransportResponse(_response(_text("")), first_byte_s=0.01)
        return super().complete(channel, request, **kwargs)


@pytest.mark.parametrize("problem", ["stale_pixels", "stale_resources"])
def test_critic_only_resume_does_not_accept_changed_or_rejected_artifacts(problem):
    page = SimpleNamespace(page=3, page_uid="p3", lifecycle=PageLifecycle.READY,
        inspection_id="i3", html_sha256="h3", png_sha256="png3")
    runtime = SkillRuntime.__new__(SkillRuntime)
    runtime._critic_coordinator = object()
    runtime._critic_resume_outcome = {"external_visual_critic": {"current_inspections": [vars(page).copy()]}}
    runtime._critic_summary = lambda *a, **kw: {"critic_incomplete": False}
    if problem == "stale_pixels":
        page.png_sha256 = "changed"
    def validate(_pages):
        if problem == "stale_resources":
            raise ValueError("visual resources changed after render")
    services = SimpleNamespace(workspace=SimpleNamespace(pages=lambda: [page], assert_ready_resources=validate))
    if problem == "stale_resources":
        with pytest.raises(ValueError, match="visual resources changed"):
            runtime._resume_critic_review(services)
    else:
        assert runtime._resume_critic_review(services) is None


@pytest.mark.parametrize("changed_input", ["attachment", "creation_config"])
def test_critic_only_resume_rejects_input_overwritten_at_same_path(tmp_path, changed_input):
    root, source = tmp_path / "workspace", tmp_path / "source.txt"
    source.write_text("原始证据")
    spec = _run_spec(root, RunMode.SYNTHESIS, source)
    runtime = SkillRuntime.__new__(SkillRuntime)
    runtime.prepared = SimpleNamespace(run_spec=spec)
    runtime._host_state = root / "_state/skill-runtime.json"
    runtime._host_state.parent.mkdir(parents=True)
    (root / "_state/run.json").write_text(json.dumps(spec.to_manifest()))
    config = root / "config.json"
    config.write_text(json.dumps({"working_language": "zh", "presentation_language": "zh", "target_page_count": 1}))
    original_hash = file_sha256(source)
    runtime._host_state.write_text(json.dumps({"status": "completed", "outcome": {"original": True},
        "creation_config_sha256": file_sha256(config),
        "checkpoint_attachments": [{"sha256": original_hash, "original_name": source.name}]}))
    (root / "result.json").write_text(json.dumps({"exit_reason": "critic_incomplete", "external_visual_critic": {"critic_incomplete": True}}))
    runtime._checkpoint_attachments = [{"sha256": original_hash, "original_name": source.name, "intent": ""}]
    assert runtime._load_critic_resume_outcome() == {"original": True}
    if changed_input == "attachment":
        source.write_text("同路径不同证据")
        runtime._checkpoint_attachments[0]["sha256"] = file_sha256(source)
    else:
        config.write_text(json.dumps({"working_language": "zh", "presentation_language": "en", "target_page_count": 1}))
    assert runtime._load_critic_resume_outcome() is None


@pytest.mark.parametrize("recovery_succeeds", [False, True])
def test_synthesis_critic_only_resume_preserves_pages_and_training_lineage(tmp_path, recovery_succeeds):
    root, attachment = tmp_path / "workspace", tmp_path / "source.txt"
    attachment.write_text("测试原始事实。")
    first = EmptyCriticTransport()
    with pytest.raises(SkillRuntimeError, match="P1: empty_critic_response"):
        _execute(root, RunMode.SYNTHESIS, attachment, transport=first,
                 enable_visual_critic=True)
    result = json.loads((root / "result.json").read_text())
    assert result["exit_reason"] == "critic_incomplete" and result["status"] == "error"
    assert "training_manifest" not in result
    assert "P1: empty_critic_response" in result["error"]
    # 草稿和总览检查不调用外部 Critic；只补当前页面缺失的正式评审。
    assert len([request for request in first.requests if request.request_kind == "critic_visual"]) == 1
    page_before = WorkspaceState(root).page(1).to_dict()
    author_events_before = (root / "_state/workspace.json").read_bytes()
    checkpoints = {path.name: file_sha256(path) for path in (root / "_trace/raw/segments").glob("segment-*.json")}
    assert checkpoints
    second = ScriptedDeckTransport() if recovery_succeeds else EmptyCriticTransport()
    if recovery_succeeds:
        outcome, _, _ = _execute(root, RunMode.SYNTHESIS, attachment, transport=second,
                                 enable_visual_critic=True)
        assert outcome.backend_result["critic_only_resume"] is True
    else:
        with pytest.raises(SkillRuntimeError, match="empty_critic_response"):
            _execute(root, RunMode.SYNTHESIS, attachment, transport=second,
                     enable_visual_critic=True)
    assert second.requests and all(request.request_kind == "critic_visual" for request in second.requests)
    assert len([request for request in second.requests if request.request_kind == "critic_visual"]) == 1
    assert WorkspaceState(root).page(1).to_dict() == page_before
    assert (root / "_state/workspace.json").read_bytes() == author_events_before
    assert all(file_sha256(root / "_trace/raw/segments" / name) == digest for name, digest in checkpoints.items())
    assert not (root / "_trace/training").exists()
    result = json.loads((root / "result.json").read_text())
    assert result["status"] == ("completed" if recovery_succeeds else "error")
