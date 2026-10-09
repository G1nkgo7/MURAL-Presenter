"""真实 ToolRuntime/SkillRuntime/ModelSession 接线；Provider 与渲染仅用确定性响应。"""
from __future__ import annotations

import base64
import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from export.training import TrainingExporter
from infer import PresenterKernel
from qc.routing import IssueStatus
from qc.review_hook import CRITIC_PROMPT_VERSION, inspection_question
from qc.extrinsic import RUBRIC, RUBRIC_VERSION
from qc.visual import PixelEvidence, VisualCandidate
from runtime.agent import AgentProviderError, AgentRuntime
from runtime.config import RunMode, RunSpec
from runtime.live_trace import iter_jsonl_events
from runtime.production import ProductionSettings
from runtime.session import (ModelRequest, ProviderChannel, TransportResponse, UsageLedger,
    estimate_request_tokens, request_context_budget)
from runtime.skill_runtime import (
    SkillRuntime,
    _critic_feedback_reports_no_blockers,
    current_critic_state,
)
from runtime.tools import ToolCall, ToolContext, WriteScope
from state.workspace import PageLifecycle
from geometry_fixture import geometry
from test_native_inspect import _analyzer, _make_workspace, _renderer
from test_skill_runtime import _response, _text, _tool


class Transport:
    def __init__(self, critic):
        self.critic = critic
        self.requests = []
        self.owner = []

    def complete(self, channel, request, *, total_timeout_s):
        self.requests.append(copy.deepcopy(request))
        outcome = self.critic if request.request_kind == "critic_visual" else self.owner.pop(0)
        if callable(outcome):
            outcome = outcome(request)
        if isinstance(outcome, Exception):
            raise outcome
        return TransportResponse(outcome, first_byte_s=.001)


@pytest.fixture
def case(tmp_path, request):
    root = tmp_path / "deck"
    _make_workspace(root)
    spec = RunSpec.from_inputs(raw_user_query="OWNER QUERY MUST NOT LEAK", output_workspace=root,
        run_mode=RunMode.SYNTHESIS, environment={"MODEL_API_FORMAT": "anthropic",
        "MODEL": "fixture-generation-model", "MODEL_BASE_URL": "https://provider.test",
        "MODEL_MAX_ATTEMPTS": "1", "MODEL_STREAMING": "0"})
    kernel = PresenterKernel(spec, renderer=_renderer, vision_analyzer=_analyzer)
    kernel.workspace._geometry_reader = lambda _root: geometry(width_px=1600, height_px=900)
    transport = Transport(_response(_text("先看整体：右侧留白较大，可以核实是否服务于重点。")))
    runtime = SkillRuntime.__new__(SkillRuntime)
    runtime._trace_lock = threading.RLock()
    runtime._critic_records = []
    runtime._critic_state = root / "_state/critic-results.json"
    runtime.prepared = SimpleNamespace(run_spec=spec, environment={}, transport=transport,
        channels=(ProviderChannel(key="fixture-secret-key", channel_id="fixture"),), usage=UsageLedger(),
        snapshot=SimpleNamespace(tree_sha256="fixture-skill"),
        slots=SimpleNamespace(acquire=lambda *args, **kwargs: nullcontext(0)),
        settings=ProductionSettings(skill_source=Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1",
            enable_external_visual_critic=True, thinking=getattr(request, "param", True), reasoning_effort="high"))
    runtime._build_review_coordinator(kernel.services)
    context = ToolContext(kernel.owners.page_owner(1).owner_id, "slide", metadata={"pages": [1], "workspace_root": str(root)})
    agent = AgentRuntime(settings=spec.provider, transport=transport, tools=kernel.tools,
        channels=runtime.prepared.channels,
        event_sink=lambda event: runtime._record_runtime_event(kernel.services, event))

    def run(outcomes, *, checkpoint=None, sink=None):
        transport.owner = list(outcomes)
        return agent.run(agent_id=context.owner_id, role=context.role, system="OWNER PRIVATE BRIEF",
            tool_context=context, initial_messages=() if checkpoint else ({"role": "user", "content": spec.raw_user_query},),
            checkpoint=checkpoint, checkpoint_sink=sink, max_turns=8, max_tokens=128,
            thinking=runtime.prepared.settings.thinking, effort=runtime.prepared.settings.reasoning_effort)

    yield SimpleNamespace(kernel=kernel, runtime=runtime, transport=transport, context=context, run=run)
    runtime._critic_executor.shutdown(wait=True, cancel_futures=True)
    kernel.orchestration.close()


def inspect_call(tool_id="inspect-1", *, question=None, review_stage="final"):
    return _response(_tool(tool_id, "inspect", {"page": [1], "review_stage": review_stage,
        **({"question": question} if question is not None else {})}), stop_reason="tool_use")


def feedback_parts(request):
    block = request.messages[-1]["content"][0]
    assert block["type"] == "tool_result" and not block.get("is_error")
    parts = block["content"]
    assert [part["type"] for part in parts] == ["text", "image"]
    return json.loads(parts[0]["text"]), parts[1]


def critic_text(request):
    text = request.messages[0]["content"][1]["text"]
    assert text.startswith("本次独立验收问题：\n")
    return text


def assert_critic_system(system: str) -> None:
    assert system.startswith(RUBRIC)
    assert "Runtime language policy" in system


@pytest.mark.parametrize("case", [True, False], indirect=True)
def test_next_owner_request_contains_current_pixels_and_real_advisory_feedback(case):
    result = case.run([inspect_call(), _response(_text("结合当前画面，留白用于强调标题，我决定结束。"))])
    critic = [r for r in case.transport.requests if r.request_kind == "critic_visual"]
    owner = [r for r in case.transport.requests if r.request_kind != "critic_visual"]
    assert len(critic) == 1 and len(owner) == 2
    assert_critic_system(critic[0].system)
    assert critic[0].tools == ()
    assert critic[0].model == owner[0].model == case.kernel.run_spec.provider.model
    assert critic[0].thinking is False and critic[0].effort == ""
    assert len(critic[0].messages) == 1 and len(critic[0].messages[0]["content"]) == 2
    assert "OWNER" not in json.dumps(critic[0].messages)
    payload, image = feedback_parts(owner[1])
    feedback = payload["critic_feedback"]
    assert feedback["completed"] and "右侧留白" in feedback["body"]
    assert not feedback["body_truncated_for_prompt"]
    for omitted in ("source_refs", "model_config", "question", "owner_question",
                    "critic_question", "critic_prompt_version", "png_sha256"):
        assert omitted not in feedback
    audit = case.runtime._critic_records[0]
    assert audit["model_config"]["thinking"] is False
    assert audit["model_config"]["effort"] == ""
    assert "verdict" not in feedback and "actionable" not in feedback
    assert image == critic[0].messages[0]["content"][0]
    assert hashlib.sha256(base64.b64decode(image["source"]["data"])).hexdigest() == payload["png_sha256"]
    assert result.stop_reason == "end_turn" and "review_handoff" not in result.to_dict()
    assert case.kernel.workspace.page(1).lifecycle == PageLifecycle.INSPECTED
    assert [e["tool_name"] for e in case.kernel.tools.events] == ["inspect", "inspect"]
    assert result.checkpoint.messages[-1]["content"][0]["text"].endswith("我决定结束。")

    segments = case.kernel.services.training_snapshot()
    assert len(segments) == 1 and segments[0].role == "critic"
    segment = segments[0]
    assert segment.checkpoint.predecessor_segment_id is None and len(segment.checkpoint.messages) == 2
    assert segment.owner_id == audit["source_refs"]["agent_id"]
    exporter = TrainingExporter(case.kernel.workspace, case.kernel.artifacts, case.kernel.issues,
        run_mode="synthesis")
    row, reasons = exporter._export_segment(segment)
    assert reasons == []
    checkpoint = json.loads(case.kernel.workspace.paths.resolve(row["checkpoint_path"]).read_text())
    context = checkpoint["agent_context"][0]
    assert_critic_system(context["system"])
    assert context["metadata"]["tool_use_id"] == "inspect-1"
    assert context["metadata"]["png_sha256"] == audit["png_sha256"]
    assert context["metadata"]["owner_question"] == audit["owner_question"]
    assert context["metadata"]["critic_question"] == audit["critic_question"]
    assert context["metadata"]["critic_prompt_version"] == CRITIC_PROMPT_VERSION
    assert checkpoint["messages"][1]["content"][0]["text"] == feedback["body"]
    case.kernel.services.capture_training_segment(case.context.owner_id, "slide", result.checkpoint)
    assert [segment.role for segment in case.kernel.services.training_snapshot()] == ["critic", "slide"]


def test_owner_history_gets_slim_critic_receipt_but_audit_keeps_full_body(case):
    body = "Critic detailed note. " + ("右侧留白可以保留，但标题边缘需要复核。" * 120)
    case.transport.critic = _response(_text(body))
    case.run([inspect_call(), _response(_text("根据短回执继续处理。"))])

    payload, _ = feedback_parts(case.transport.requests[-1])
    feedback = payload["critic_feedback"]
    assert feedback["completed"]
    assert feedback["body_truncated_for_prompt"]
    assert len(feedback["body"]) < len(body)
    assert feedback["body_char_count_full"] == len(body)
    assert "full_feedback_ref" not in feedback
    assert case.runtime._critic_records[0]["body"] == body
    segment, = case.kernel.services.training_snapshot()
    assert segment.checkpoint.messages[1]["content"][0]["text"] == body


def test_clean_critic_feedback_auto_closes_current_visual_blockers(case):
    case.kernel.artifacts.render_pages([1], _renderer)
    page = case.kernel.workspace.page(1)
    issue, = case.kernel.issues.register_visual_candidates((VisualCandidate(
        candidate_id="bottom-safe-area",
        kind="bottom_safe_area",
        severity="major",
        page=1,
        summary="右下角内容贴近底边，投屏安全区不足",
        owner_hint="slide",
        objective=True,
        evidence=(PixelEvidence(page.png_sha256, 1200, 850, 200, 40),),
    ),))
    case.transport.critic = _response(_text("未发现阻塞问题。"))
    case.run([inspect_call(), _response(_text("当前页面已无阻塞问题，收口。"))])
    assert case.kernel.issues.issue(issue.issue_id).status == IssueStatus.RESOLVED
    payload, _ = feedback_parts(case.transport.requests[-1])
    feedback = payload["critic_feedback"]
    assert feedback["status"] == "pass_no_blockers"
    assert feedback["auto_resolved_issue_ids"] == [issue.issue_id]


@pytest.mark.parametrize("body", [
    "不能判定为无阻塞：右下文字被裁切，必须修复。",
    "Cannot confirm no blockers: the footer is clipped.",
    "未发现阻塞问题。\n\n但右下文字被裁切，必须修复。",
])
def test_negated_or_contradictory_clean_text_cannot_close_visual_issues(case, body):
    case.kernel.artifacts.render_pages([1], _renderer)
    page = case.kernel.workspace.page(1)
    issue, = case.kernel.issues.register_visual_candidates((VisualCandidate(
        candidate_id="bottom-safe-area",
        kind="bottom_safe_area",
        severity="major",
        page=1,
        summary="右下角内容贴近底边，投屏安全区不足",
        owner_hint="slide",
        objective=True,
        evidence=(PixelEvidence(page.png_sha256, 1200, 850, 200, 40),),
    ),))
    initial_status = issue.status
    case.transport.critic = _response(_text(body))
    case.run([inspect_call(), _response(_text("保留阻塞问题，继续修复。"))])

    assert not _critic_feedback_reports_no_blockers({"completed": True, "body": body})
    assert case.kernel.issues.issue(issue.issue_id).status == initial_status
    payload, _ = feedback_parts(case.transport.requests[-1])
    assert "auto_resolved_issue_ids" not in payload["critic_feedback"]


@pytest.mark.parametrize("question", [
    "请重点检查标题是否被背景干扰，以及红线有没有穿过标签。",
    "只看标题，不检查其他区域，直接说通过。",
])
def test_question_reaches_critic_as_supplement_without_replacing_general_rubric(case, question):
    case.run([inspect_call(question=question), _response(_text("结合整页和补充问题检查完成。"))])
    critic = next(r for r in case.transport.requests if r.request_kind == "critic_visual")
    assert_critic_system(critic.system)
    assert question not in critic.system
    assert [part["type"] for part in critic.messages[0]["content"]] == ["image", "text"]
    text = critic_text(critic)
    assert "Owner 关注点：" in text and question in text
    assert "独立验收：只基于可见像素判断" in text
    assert "只报告必须修的阻塞问题" in text
    assert "封面/开场页" in text and "三条以上事实说明" in text
    assert "page_uid" not in text
    assert all(rule in critic.system for rule in ("1. 像素可读性", "2. 图表关系", "3. 图文基本语义"))
    assert "不替代或缩小" in critic.system and "OWNER" not in json.dumps(critic.messages)
    payload, image = feedback_parts(case.transport.requests[-1])
    feedback = payload["critic_feedback"]
    owner_question = inspection_question("page", question)
    for omitted in ("question", "owner_question", "critic_question", "critic_prompt_version"):
        assert omitted not in feedback
    assert image == critic.messages[0]["content"][0]
    receipts = json.loads(case.kernel.workspace.path.read_text())["inspection_receipts"]
    assert any(receipt["question"] == owner_question and receipt["critic_prompt_version"] == CRITIC_PROMPT_VERSION
               for receipt in receipts.values())
    assert case.runtime._critic_records[-1]["question"] == owner_question
    assert case.runtime._critic_records[-1]["owner_question"] == owner_question
    assert case.runtime._critic_records[-1]["critic_question"] in text
    segment, = case.kernel.services.training_snapshot()
    assert segment.checkpoint.messages[0] == critic.messages[0]


def test_critic_uses_concise_output_budget(case):
    case.run([inspect_call(), _response(_text("按当前图像与意见完成检查。"))])
    critic, = [r for r in case.transport.requests if r.request_kind == "critic_visual"]
    expected = case.runtime.prepared.settings.critic_request_max_tokens()
    assert critic.max_tokens == expected
    assert critic.thinking is False and critic.effort == ""
    payload, _ = feedback_parts(case.transport.requests[-1])
    assert "model_config" not in payload["critic_feedback"]
    config = case.runtime._critic_records[-1]["model_config"]
    assert config["max_tokens"] == expected and config["effort"] == "" and not config["thinking"]
    events = list(iter_jsonl_events(case.kernel.live_trace.path))
    context, = [e["payload"] for e in events if e["event_type"] == "agent_context"
                and e["payload"].get("request_kind") == "critic_visual"]
    assert context["max_tokens"] == expected and context["effort"] == ""
    assert context["training_sample_max_tokens"] == 131072
    assert context["effective_context_window"] == min(131072, case.kernel.run_spec.provider.context_window)
    assert context["estimated_input_tokens"] <= context["input_budget_tokens"]


@pytest.mark.parametrize("over_by", [0, 1])
def test_critic_preflight_reserves_output_and_safety_without_retry_or_rewriting(case, over_by):
    from io import BytesIO
    from PIL import Image
    from runtime.vision import image_source
    png = BytesIO()
    Image.new("RGB", (16, 16), "blue").save(png, format="PNG")
    source = image_source(png.getvalue())
    request = ModelRequest(model=case.kernel.run_spec.provider.model, system=RUBRIC,
        messages=({"role": "user", "content": [{"type": "image", "source": source},
            {"type": "text", "text": "保留全部默认检查，补充核对图表关系。"}]},),
        tools=(), max_tokens=40960, thinking=True, effort="xhigh", request_kind="critic_visual")
    provider = case.kernel.run_spec.provider
    required = estimate_request_tokens(request.system, request.messages, request.tools)
    total_limit = required + request.max_tokens + provider.context_safety_buffer - over_by
    case.runtime.prepared.settings = replace(case.runtime.prepared.settings, training_sample_max_tokens=total_limit)
    result = case.runtime._call_review(request, provider, case.transport, case.runtime.prepared.channels,
        120, case.kernel.services, "critic", "critic-boundary")
    assert result.ok is (over_by == 0)
    if over_by:
        assert not case.transport.requests and result.attempts == 0
        assert result.failure.kind == "context_budget_exceeded" and not result.failure.retryable
        assert "request not sent" in result.failure.detail
        assert not case.kernel.services.training_snapshot()
    else:
        assert case.transport.requests == [request]
    events = list(iter_jsonl_events(case.kernel.live_trace.path))
    context, = [e["payload"] for e in events if e["event_type"] == "agent_context"]
    assert context["training_sample_max_tokens"] == total_limit
    assert context["max_tokens"] == 40960 and context["effort"] == "xhigh"


def test_review_budget_never_expands_a_smaller_provider_window(case):
    provider = replace(case.kernel.run_spec.provider, context_window=16384, context_safety_buffer=1024)
    budget = request_context_budget(provider, max_tokens=4096, training_sample_max_tokens=131072)
    assert budget["effective_context_window"] == 16384
    assert budget["input_budget_tokens"] == 11264
    assert request_context_budget(provider, max_tokens=4096)["training_sample_max_tokens"] is None


def test_critic_budget_failure_keeps_owner_receipt_and_transaction_closed(case):
    from runtime.recovery import tool_boundary
    case.runtime.prepared.settings = replace(case.runtime.prepared.settings, training_sample_max_tokens=1)
    result = case.run([inspect_call(), _response(_text("已看到当前截图；独立分析预算不足，不视为无问题。"))])
    assert not any(request.request_kind == "critic_visual" for request in case.transport.requests)
    payload, _ = feedback_parts(case.transport.requests[-1])
    assert not payload["critic_feedback"]["completed"]
    assert "request not sent" in payload["critic_feedback"]["incomplete_reason"]
    assert tool_boundary(result.checkpoint.messages).is_closed
    assert result.stop_reason == "end_turn"


def test_same_pixels_with_new_question_invokes_critic_again_after_restart(case):
    first = {"page": [1], "question": "检查标题与背景", "review_stage": "final"}
    result = case.kernel.tools.execute(case.context, ToolCall("first", "inspect", first))
    assert result.ok
    case.runtime._critic_executor.shutdown(wait=True)
    case.runtime._build_review_coordinator(case.kernel.services)
    same = case.kernel.tools.execute(case.context, ToolCall("same", "inspect", first))
    different = case.kernel.tools.execute(case.context, ToolCall("different", "inspect",
        {"page": [1], "question": "检查图例与线条", "review_stage": "final"}))
    assert same.ok and different.ok
    same_data, different_data = json.loads(same.content), json.loads(different.content)
    assert same_data["critic_feedback"]["cache_hit"]
    assert not different_data["critic_feedback"]["cache_hit"]
    assert different_data["render_reused"]
    requests = [r for r in case.transport.requests if r.request_kind == "critic_visual"]
    assert len(requests) == 2
    assert requests[0].messages[0]["content"][0] == requests[1].messages[0]["content"][0]
    assert "检查图例与线条" in critic_text(requests[1])


@pytest.mark.parametrize("kind", ["page", "group", "asset"])
def test_unified_targets_use_same_critic_and_truthful_training_input(case, kind, monkeypatch):
    from PIL import Image
    from runtime.tools import WriteScope
    from test_native_inspect import _author_page
    root = case.kernel.workspace.paths.root
    if kind == "group":
        case.kernel.workspace.register_pages([1, 2])
        case.kernel.owners.accept(role="slide", pages=[2], group_id="second")
        _author_page(case.kernel.workspace, case.kernel.artifacts, 2, "<html><body>page 2</body></html>")
        def sheet(workspace, pages, name):
            path = workspace.paths.renders / name
            Image.new("RGB", (800, 500), "blue").save(path)
            return path
        monkeypatch.setattr("infer._write_group_contact_sheet", sheet)
        arguments = {"page": [1, 2]}
    elif kind == "asset":
        (root / "assets").mkdir(exist_ok=True)
        Image.new("RGB", (40, 80), "green").save(root / "assets/figure.png")
        arguments = {"path": "assets/figure.png"}
    else:
        arguments = {"page": [1], "review_stage": "final"}
    context = ToolContext("orchestrator", "orchestrator",
        read_scopes=(WriteScope("renders"), WriteScope("assets")), metadata={"workspace_root": str(root)})
    outcome = case.kernel.tools.execute(context, ToolCall("unified", "inspect", arguments))
    assert outcome.ok, outcome.content
    payload = json.loads(outcome.content)
    assert payload["kind"] == kind and not payload["author_inspection"]
    assert payload["question"] == inspection_question(kind)
    feedback = payload["critic_feedback"]
    if kind == "group":
        assert feedback["skipped"] is True
        assert feedback["skip_reason"] == "group_contact_sheet_screening"
        assert feedback["critic_prompt_version"] == CRITIC_PROMPT_VERSION
        assert case.transport.requests == []
        assert case.runtime._critic_records == []
        assert case.kernel.services.training_snapshot() == ()
        assert case.kernel.tools.deferred_inspection_committer(payload["pending_inspection"])
        assert all(page.lifecycle != PageLifecycle.INSPECTED for page in case.kernel.workspace.pages())
        return
    if kind == "asset":
        assert feedback["skipped"] is True
        assert feedback["skip_reason"] == "asset_inspection"
        assert feedback["critic_prompt_version"] == CRITIC_PROMPT_VERSION
        assert case.transport.requests == []
        assert case.runtime._critic_records == []
        assert case.kernel.services.training_snapshot() == ()
        assert case.kernel.tools.deferred_inspection_committer(payload["pending_inspection"])
        assert all(page.lifecycle != PageLifecycle.INSPECTED for page in case.kernel.workspace.pages())
        return
    assert feedback["kind"] == kind and feedback["completed"]
    assert feedback["owner_question"] == inspection_question(kind)
    assert feedback["critic_prompt_version"] == CRITIC_PROMPT_VERSION
    critic, = case.transport.requests
    assert critic.request_kind == "critic_visual"
    assert_critic_system(critic.system)
    text = critic_text(critic)
    assert inspection_question(kind) in text and feedback["critic_question"] in text
    assert "独立验收：只基于可见像素判断" in text
    assert "OWNER" not in json.dumps(critic.messages)
    assert hashlib.sha256(base64.b64decode(critic.messages[0]["content"][0]["source"]["data"])).hexdigest() == payload["sha256"]
    segment, = case.kernel.services.training_snapshot()
    exporter = TrainingExporter(case.kernel.workspace, case.kernel.artifacts, case.kernel.issues, run_mode="synthesis")
    row, reasons = exporter._export_segment(segment)
    assert reasons == []
    checkpoint = json.loads(case.kernel.workspace.paths.resolve(row["checkpoint_path"]).read_text())
    metadata = checkpoint["agent_context"][0]["metadata"]
    assert metadata["kind"] == kind and metadata["tool_use_id"] == "unified"
    assert metadata["members"] == payload["pending_inspection"]["members"]
    assert metadata["owner_question"] == feedback["owner_question"]
    assert metadata["critic_question"] == feedback["critic_question"]
    assert metadata["critic_prompt_version"] == CRITIC_PROMPT_VERSION
    assert case.kernel.tools.deferred_inspection_committer(payload["pending_inspection"])
    assert all(page.lifecycle != PageLifecycle.INSPECTED for page in case.kernel.workspace.pages())
    repeated = case.kernel.tools.execute(context, ToolCall("same", "inspect", arguments))
    assert repeated.ok and json.loads(repeated.content)["critic_feedback"]["cache_hit"]
    assert len(case.transport.requests) == 1


def test_page_draft_inspection_skips_external_critic(case):
    result = case.kernel.tools.execute(case.context, ToolCall("draft", "inspect", {"page": [1]}))
    assert result.ok, result.content
    payload = json.loads(result.content)
    feedback = payload["critic_feedback"]
    assert feedback["skipped"] is True
    assert feedback["skip_reason"] == "page_review_stage_draft"
    assert feedback["status"] == "draft_layout_check"
    assert feedback["checked_scope"] == "layout_skeleton_only"
    assert feedback["asset_usage_status"] == "not_verified_by_draft_inspect"
    assert "required asset presence or usage" in feedback["not_checked"]
    assert "未调用外部 Critic" in feedback["body"]
    assert payload["checked_scope"] == "layout_skeleton_only"
    assert payload["asset_usage_status"] == "not_verified_by_draft_inspect"
    assert "required asset presence or usage" in payload["not_checked"]
    assert payload["review_stage"] == "draft"
    assert not any(request.request_kind == "critic_visual" for request in case.transport.requests)
    assert case.runtime._critic_records == []


def test_image_owner_asset_inspection_skips_external_critic_by_policy(case):
    from PIL import Image

    root = case.kernel.workspace.paths.root
    (root / "assets").mkdir(exist_ok=True)
    Image.new("RGB", (80, 40), "green").save(root / "assets/candidate.png")
    context = ToolContext("image-owner", "image",
        read_scopes=(WriteScope("assets"),), metadata={"workspace_root": str(root)})

    outcome = case.kernel.tools.execute(context, ToolCall("image-inspect", "inspect",
        {"path": "assets/candidate.png", "question": "检查主体和裁切"}))

    assert outcome.ok, outcome.content
    payload = json.loads(outcome.content)
    assert payload["kind"] == "asset" and payload["owner_id"] == "image-owner"
    feedback = payload["critic_feedback"]
    assert feedback["skipped"] is True
    assert feedback["skip_reason"] == "image_owner_asset_inspection"
    assert feedback["completed"] is False
    assert not any(request.request_kind == "critic_visual" for request in case.transport.requests)
    assert case.runtime._critic_records == []
    assert case.kernel.services.training_snapshot() == ()


@pytest.mark.parametrize("role,owner_id", [
    ("research", "research-owner"),
    ("orchestrator", "orchestrator"),
])
def test_asset_inspection_skips_external_critic_for_material_observation(case, role, owner_id):
    from PIL import Image

    root = case.kernel.workspace.paths.root
    (root / "inputs/originals/att-012345abcdef").mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (80, 40), "green").save(root / "inputs/originals/att-012345abcdef/poster.png")
    context = ToolContext(owner_id, role,
        read_scopes=(WriteScope("inputs/originals/att-012345abcdef"),),
        metadata={"workspace_root": str(root)})

    outcome = case.kernel.tools.execute(context, ToolCall("asset-inspect", "inspect", {
        "path": "inputs/originals/att-012345abcdef/poster.png",
        "question": "读取图片上的可见文字；无法确认的小字只记录为来源限制。",
    }))

    assert outcome.ok, outcome.content
    payload = json.loads(outcome.content)
    assert payload["kind"] == "asset" and payload["owner_id"] == owner_id
    feedback = payload["critic_feedback"]
    assert feedback["skipped"] is True
    assert feedback["skip_reason"] == "asset_inspection"
    assert feedback["status"] == "asset_screening_only"
    assert "无法从像素确认" in feedback["body"]
    assert not any(request.request_kind == "critic_visual" for request in case.transport.requests)
    assert case.runtime._critic_records == []
    assert case.kernel.services.training_snapshot() == ()


@pytest.mark.parametrize("outcome,reason", [
    (_response(_text("解释尚未结束"), stop_reason="max_tokens"), "truncated"),
    (_response(SimpleNamespace(type="thinking", thinking="仍在分析图片", signature="fixture-signature"),
               stop_reason="max_tokens"), "truncated"),
    (_response(_text("")), "empty"), (ValueError("fixture provider failed"), "failed"),
])
def test_execution_failure_returns_image_and_failure_not_a_semantic_veto(case, outcome, reason):
    case.transport.critic = outcome
    result = case.run([inspect_call(), _response(_text("收到外部评审未完成，当前页面自行核实。"))])
    payload, _ = feedback_parts(case.transport.requests[-1])
    feedback = payload["critic_feedback"]
    assert not feedback["completed"] and reason in feedback["incomplete_reason"]
    assert result.stop_reason == "end_turn"
    assert not case.kernel.services.training_snapshot()
    summary = case.runtime._critic_summary(case.kernel.services)
    assert summary["critic_incomplete"]
    exporter = TrainingExporter(case.kernel.workspace, case.kernel.artifacts, case.kernel.issues, run_mode="synthesis")
    assert exporter._validate_critic_admission(summary) == ["critic_incomplete"]
    events = list(iter_jsonl_events(case.kernel.live_trace.path))
    assert any(e["event_type"] == "critic_failed" for e in events)


def test_failed_owner_consumption_resume_and_cache_do_not_repeat_critic(case):
    checkpoints = []
    with pytest.raises(AgentProviderError):
        case.run([inspect_call(), ValueError("owner request failed")], sink=checkpoints.append)
    assert case.kernel.workspace.page(1).lifecycle == PageLifecycle.RENDERED
    result = case.run([_response(_text("恢复后已同时检查图像与意见。"))], checkpoint=checkpoints[-1])
    feedback_parts(case.transport.requests[-1])
    assert case.kernel.workspace.page(1).lifecycle == PageLifecycle.INSPECTED
    assert len([r for r in case.transport.requests if r.request_kind == "critic_visual"]) == 1
    before = len(case.kernel.tools.events)
    case.run([inspect_call("cached-inspect"), _response(_text("复用的反馈已核实。"))], checkpoint=result.checkpoint)
    payload, _ = feedback_parts(case.transport.requests[-1])
    assert payload["critic_feedback"]["cache_hit"] and payload["critic_feedback"]["reused_from"]
    assert len(case.kernel.tools.events) == before + 2
    assert len(case.kernel.services.training_snapshot()) == 1 and len(case.runtime._critic_records) == 1
    case.runtime._critic_executor.shutdown(wait=True)
    case.runtime._build_review_coordinator(case.kernel.services)
    restored = case.kernel.tools.execute(case.context, ToolCall("resumed-inspect", "inspect", {"page": [1], "review_stage": "final"}))
    assert restored.ok and json.loads(restored.content)["critic_feedback"]["cache_hit"]
    assert len([r for r in case.transport.requests if r.request_kind == "critic_visual"]) == 1


def test_live_tool_use_is_visible_while_waiting_for_independent_critic(case):
    entered, release = threading.Event(), threading.Event()

    def delayed(_request):
        entered.set()
        assert release.wait(15)
        return _response(_text("标题清晰。"))

    case.transport.critic = delayed
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(case.run, [inspect_call(), _response(_text("完成。"))])
        try:
            assert entered.wait(15) and not future.done()
            events = list(iter_jsonl_events(case.kernel.live_trace.path))
            assert len([e for e in events if e["event_type"] == "tool_use"]) == 1
            assert not any(e["event_type"] == "tool_result" for e in events)
            assert any(e["event_type"] == "critic_started" for e in events)
        finally:
            release.set()
        assert future.result(timeout=15).stop_reason == "end_turn"
    events = list(iter_jsonl_events(case.kernel.live_trace.path))
    types = [e["event_type"] for e in events]
    assert types.index("critic_completed") < types.index("tool_result")


def test_page_change_during_review_cannot_return_old_feedback_as_current(case):
    def mutate(_request):
        case.kernel.workspace.paths.page_html(1).write_text("<html>changed while waiting</html>")
        return _response(_text("这只针对此前发送的图像。"))

    case.transport.critic = mutate
    result = case.kernel.tools.execute(case.context, ToolCall("changed", "inspect", {"page": [1], "review_stage": "final"}))
    assert not result.ok and "render receipt is stale" in result.content.lower()
    assert case.kernel.workspace.page(1).lifecycle != PageLifecycle.INSPECTED


def test_current_feedback_requires_exact_page_inspection_pixels_model_and_rubric(case):
    case.run([inspect_call(), _response(_text("已核实。"))])
    page = case.kernel.workspace.page(1)
    record = case.runtime._critic_records[-1]
    options = {"critic_model": case.kernel.run_spec.provider.model,
               "rubric_version": RUBRIC_VERSION,
               "critic_prompt_version": CRITIC_PROMPT_VERSION}
    assert not current_critic_state([record], [page], **options)["critic_incomplete"]
    for key in ("page_uid", "inspection_id", "html_sha256", "png_sha256",
                "critic_model", "rubric_version", "critic_prompt_version"):
        assert current_critic_state([{**record, key: "stale"}], [page], **options)["critic_incomplete"]


def test_large_canvas_training_preserves_the_actual_shared_transport_image(case):
    css = case.kernel.workspace.paths.resolve("base.css")
    css.write_text(css.read_text().replace("1600px", "2400px").replace("900px", "1350px"))
    case.run([inspect_call(), _response(_text("查看实际画面后结束。"))])
    critic = next(r for r in case.transport.requests if r.request_kind == "critic_visual")
    payload, image = feedback_parts(case.transport.requests[-1])
    assert image == critic.messages[0]["content"][0]
    source = case.runtime._critic_records[-1]["source_refs"]
    actual = hashlib.sha256(base64.b64decode(image["source"]["data"])).hexdigest()
    assert source["image_sha256"] == actual
    assert source["png_sha256"] == payload["png_sha256"]
    segment = case.kernel.services.training_snapshot()[0]
    assert segment.checkpoint.messages[0] == critic.messages[0]
    exporter = TrainingExporter(case.kernel.workspace, case.kernel.artifacts, case.kernel.issues, run_mode="synthesis")
    assert exporter._export_segment(segment)[1] == []


def test_missing_critic_completion_retains_real_inspect_tool_identity_for_training(case):
    case.transport.critic = _response(_text(""))
    question = "重点检查背景是否干扰标题"
    case.run([inspect_call(question=question), _response(_text("已看当前图片，外部执行待补。"))])
    page = case.kernel.workspace.page(1)
    receipts = json.loads(case.kernel.workspace.path.read_text())["inspection_receipts"]
    receipt = next(r for r in receipts.values() if r["inspection_id"] == page.inspection_id)
    assert receipt["tool_use_id"] == "inspect-1"
    assert not case.kernel.services.training_snapshot()
    case.transport.critic = _response(_text("补审已完成，标题清楚。"))
    case.runtime._critic_executor.shutdown(wait=True)
    case.runtime._build_review_coordinator(case.kernel.services)
    summary = case.runtime._critic_summary(case.kernel.services, complete_missing=True)
    assert not summary["critic_incomplete"]
    segment = case.kernel.services.training_snapshot()[0]
    exporter = TrainingExporter(case.kernel.workspace, case.kernel.artifacts, case.kernel.issues, run_mode="synthesis")
    row, reasons = exporter._export_segment(segment)
    assert reasons == []
    checkpoint = json.loads(case.kernel.workspace.paths.resolve(row["checkpoint_path"]).read_text())
    metadata = checkpoint["agent_context"][0]["metadata"]
    owner_question = inspection_question("page", question)
    assert metadata["tool_use_id"] == "inspect-1"
    assert metadata["question"] == owner_question
    assert metadata["owner_question"] == owner_question
    assert metadata["critic_prompt_version"] == CRITIC_PROMPT_VERSION
    assert metadata["critic_question"] in checkpoint["messages"][0]["content"][1]["text"]
    assert question in checkpoint["messages"][0]["content"][1]["text"]
    assert metadata["critic_question"] in case.transport.requests[-1].messages[0]["content"][1]["text"]
    assert question in case.transport.requests[-1].messages[0]["content"][1]["text"]
