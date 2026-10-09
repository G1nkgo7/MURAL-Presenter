"""独立 Critic 的公开反馈、冻结像素、同步等待与既有持久恢复合同。"""
from __future__ import annotations

import base64
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace

from PIL import Image
import pytest

from qc.extrinsic import CriticClient, CriticResult, ExternalVisualCritic, RUBRIC, RUBRIC_VERSION
from qc.review_hook import CRITIC_PROMPT_VERSION, InspectionClosed, inspection_with_critic_question
from runtime.config import ProviderSettings
from runtime.recovery import tool_boundary
from runtime.session import ProviderChannel, TransportResponse
from runtime.tools import ToolContext, ToolDefinition, ToolRuntime


def _settings():
    return ProviderSettings(api_format="anthropic", model="model-test", base_url="https://provider.test",
        total_timeout_s=30, connect_timeout_s=5, idle_timeout_s=10, channel_cooldown_s=0,
        max_attempts=2, streaming=False, prompt_cache=False, prompt_cache_messages=False)


def _response(*blocks, stop_reason="end_turn"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason, analysis_summaries=(),
        usage=SimpleNamespace(input_tokens=3, cache_read_input_tokens=0, cache_creation_input_tokens=0, output_tokens=2))


def _text(value):
    return SimpleNamespace(type="text", text=value)


def _tool(tool_id, name, arguments):
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=arguments)


class ScriptedTransport:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def complete(self, channel, request, *, total_timeout_s):
        self.calls.append((channel.channel_id, request))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return TransportResponse(outcome, first_byte_s=0.01)


def _write_png(workspace, relative, color=(200, 30, 30)):
    target = workspace / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 48), color).save(target)
    return hashlib.sha256(target.read_bytes()).hexdigest()


def _inspect_tool(workspace, png_relative, png_sha, *, page=1):
    def handler(context, arguments):
        return {"page": page, "path": png_relative, "html_sha256": "html-" + png_sha[:8],
            "png_sha256": png_sha, "inspection_id": f"inspection-{png_sha[:20]}",
            "native_multimodal": True, "pending_inspection": {
                "page": page, "page_uid": f"p{page}", "owner_id": context.owner_id,
                "inspection_id": f"inspection-{png_sha[:20]}",
                "html_sha256": "html-" + png_sha[:8], "png_sha256": png_sha}}
    return ToolDefinition(name="inspect", description="inspect",
        input_schema={"type": "object", "properties": {}, "additionalProperties": True},
        handler=handler, roles=frozenset({"slide"}))


def _run_owner(workspace, transport, tools, coordinator, *, max_turns=8):
    from runtime.agent import AgentRuntime

    if coordinator is not None:
        def feedback(context, call, payload):
            fact = payload["pending_inspection"]
            closed = InspectionClosed(context.owner_id, context.role, fact["inspection_id"],
                fact["html_sha256"], fact["png_sha256"], payload["path"], str(workspace),
                page=fact["page"], page_uid=fact["page_uid"], tool_use_id=call.tool_use_id)
            return {**payload, "critic_feedback": coordinator.review(closed)}
        tools.inspection_feedback = feedback
    runtime = AgentRuntime(settings=_settings(), channels=(ProviderChannel("secret-a", "ch-a"),),
        transport=transport, tools=tools)
    return runtime.run(agent_id="owner-slide", role="slide", system="正式角色卡",
        tool_context=ToolContext(owner_id="owner-slide", role="slide",
            metadata={"workspace_root": str(workspace), "pages": [1]}),
        initial_messages=({"role": "user", "content": "开始"},), max_turns=max_turns,
        max_tokens=128, thinking=False, effort="medium")


def _closed(root, *, page=1, page_uid="page-1", inspection_id="inspection-1", scope="scope-1", color=(200, 30, 30)):
    path = f"renders/{inspection_id}.png"
    png = _write_png(root, path, color)
    return InspectionClosed("owner-slide", "slide", inspection_id, "html-" + inspection_id, png,
        path, str(root), page=page, page_uid=page_uid, scope_key=scope, tool_use_id="tool-" + inspection_id)


def _reply(body="当前画面未见明显问题。", **extra):
    return {"body": body, "stop_reason": "end_turn", **extra}


def _critic(executor, invoke, *, records=None, events=None, prior=(), model="test", rubric=RUBRIC_VERSION):
    records = records if records is not None else []
    return ExternalVisualCritic(client=CriticClient(invoker=invoke, critic_model=model, rubric_version=rubric),
        executor=executor, prior_records=prior, event_sink=events.append if events is not None else None,
        result_sink=lambda result, disposition: records.append({**result.to_audit_dict(), "disposition": disposition}))


@pytest.mark.parametrize("body", ["READY", "REVISE\n问题｜标题截断\n定位｜页首\n改法｜换行",
    "REVISE\n整体可以更精致一点", "先逐项观察图表，随后给出结论。\n\n没有明显问题。",
    "先核对刻度。\n\nREVISE\n请核实右上标签与图例是否对应。", "  没有明显问题。\n"])
def test_natural_public_response_is_complete_without_verdict_parsing(tmp_path, body):
    closed = _closed(tmp_path)
    result = CriticClient(invoker=lambda *_: _reply(body), critic_model="test").review(closed)
    assert result.completed and result.body == body and result.incomplete_reason == ""
    audit = result.to_audit_dict()
    assert not {"verdict", "actionable", "raw_text", "inject", "handoff", "messages"} & audit.keys()
    assert audit["inspection_id"] == closed.inspection_id and audit["png_sha256"] == closed.png_sha256
    assert audit["owner_question"] == closed.question
    assert audit["critic_prompt_version"] == CRITIC_PROMPT_VERSION
    assert "独立验收：只基于可见像素判断" in audit["critic_question"]
    assert "固定整页检查" in audit["critic_question"]
    assert "底部余量" in audit["critic_question"]
    assert "封面/开场页" in audit["critic_question"]
    assert "标题与主视觉是否形成一张完整画面" in audit["critic_question"]
    assert "图片没有圆角本身不是缺陷" in audit["critic_question"]
    assert "局部修改验收不得跳过整页基础版式检查" in audit["critic_question"]
    assert "page_uid" not in audit["critic_question"]


def test_rubric_keeps_three_pixel_checks_without_owner_verdict_or_case_hints():
    assert RUBRIC_VERSION == "visual-critic-rubric.v12"
    assert all(label in RUBRIC for label in ("1. 像素可读性", "2. 图表关系", "3. 图文基本语义"))
    assert "并不豁免对象或状态标注错误" in RUBRIC
    assert "四边安全区" in RUBRIC and "底部余量" in RUBRIC and "局部修改" in RUBRIC
    assert "用于阻塞验收" in RUBRIC and "页面 Owner 判断" in RUBRIC
    assert "可见依据" in RUBRIC and "留白" in RUBRIC
    assert "相同语义角色应稳定" in RUBRIC and "不同模板" in RUBRIC
    assert "ground_map" in RUBRIC and "变化本身当作合理证据" in RUBRIC
    assert not any(word in RUBRIC for word in ("READY", "REVISE", "269", "P6", "P7", "白雾", "水蒸气"))
    from qc import extrinsic, review_hook
    assert not hasattr(extrinsic, "parse_verdict")
    assert not any(hasattr(review_hook, name) for name in ("OwnerIntent", "ReconcileDirective", "classify_owner_intent"))
    assert not (Path(extrinsic.__file__).parent / "reconcile.py").exists()


def test_invoker_receives_exact_pixels_and_explicit_closed_with_public_source_refs(tmp_path):
    closed = _closed(tmp_path)
    seen = []
    refs = {"agent_id": "critic-real", "segment_id": "segment-real", "attempt_id": "attempt-real",
        "request_kind": "critic_visual", "inspection_id": closed.inspection_id,
        "png_sha256": closed.png_sha256, "page_uid": closed.page_uid, "page": closed.page}
    config = {"model": "test", "thinking": True, "effort": "high", "api_format": "anthropic", "streaming": True}
    def invoke(pixels, media, identity):
        seen.append((pixels, media, identity))
        return _reply("完整公开反馈", source_refs=refs, model_config=config, channel_id="real-channel",
                      signature="not-public", hidden_thinking="not-public")
    result = CriticClient(invoker=invoke, critic_model="test").review(closed)
    prepared = inspection_with_critic_question(closed)
    assert seen == [((tmp_path / closed.png_path).read_bytes(), "image/png", prepared)]
    assert result.source_refs == refs and result.model_config == config
    assert result.agent_id == "owner-slide" != result.source_refs["agent_id"]
    assert result.channel_id == "real-channel" and result.tool_use_id == closed.tool_use_id
    assert result.owner_question == prepared.owner_question
    assert result.critic_question == prepared.critic_question
    shown = result.to_display_dict()
    assert shown["owner_id"] == closed.agent_id
    assert "signature" not in shown and "hidden_thinking" not in shown
    shown["source_refs"]["segment_id"] = "changed"
    assert result.source_refs["segment_id"] == "segment-real"


def test_current_rubric_does_not_reuse_legacy_review_after_resume(tmp_path):
    closed = _closed(tmp_path)
    records, calls = [], []

    def invoke(_pixels, _media, _closed):
        calls.append(1)
        return _reply("保留真实的公开反馈，不自动改成裁决。")

    with ThreadPoolExecutor(1) as executor:
        legacy = _critic(executor, invoke, records=records, rubric="visual-critic-rubric.v7")
        legacy.review(closed)
        previous = json.loads(json.dumps(records))
        current = _critic(executor, invoke, prior=previous, records=records)
        refreshed = current.review(closed)
        repeated = current.review(closed)

    assert len(calls) == 2
    assert not refreshed["cache_hit"] and refreshed["rubric_version"] == RUBRIC_VERSION
    assert repeated["cache_hit"] and repeated["body"] == refreshed["body"]
    assert previous[0]["rubric_version"] == "visual-critic-rubric.v7"
    assert refreshed["png_sha256"] == closed.png_sha256
    assert "verdict" not in refreshed


@pytest.mark.parametrize("response,reason", [
    ({}, "invalid_critic_response"), ({"body": None}, "invalid_critic_response"),
    (_reply(" \n "), "empty_critic_response"),
    (_reply("未完成正文", stop_reason="max_tokens"), "critic_response_truncated"),
    (_reply("未完成正文", stop_reason="length"), "critic_response_truncated"),
    (_reply("部分正文", incomplete_reason="provider_error"), "provider_error"),
])
def test_empty_truncated_or_failed_response_stays_incomplete(tmp_path, response, reason):
    result = CriticClient(invoker=lambda *_: response, critic_model="test").review(_closed(tmp_path))
    assert not result.completed and reason in result.incomplete_reason
    if isinstance(response.get("body"), str):
        assert result.body == response["body"]


@pytest.mark.parametrize("failure", [TimeoutError("provider deadline"), RuntimeError("provider unavailable")])
def test_provider_failure_is_returned_without_another_call_budget(tmp_path, failure):
    calls = []
    def invoke(*_):
        calls.append(1)
        raise failure
    result = CriticClient(invoker=invoke, critic_model="test").review(_closed(tmp_path))
    assert not result.completed and type(failure).__name__ in result.incomplete_reason
    assert result.body == "" and calls == [1]


@pytest.mark.parametrize("changed", ["missing", "hash", "escape", "workspace"])
def test_image_path_and_hash_failure_never_invokes_critic(tmp_path, changed):
    closed = _closed(tmp_path / "workspace")
    if changed == "missing":
        closed = replace(closed, png_path="missing.png")
    elif changed == "hash":
        closed = replace(closed, png_sha256="0" * 64)
    elif changed == "escape":
        closed = replace(closed, png_path="../outside.png")
    else:
        closed = replace(closed, workspace_root="")
    result = CriticClient(invoker=lambda *_: pytest.fail("invalid pixels must not reach provider"),
                          critic_model="test").review(closed)
    assert not result.completed and result.incomplete_reason.startswith("png_unavailable:")


def test_same_inspection_resume_reuses_feedback_without_new_calls_records_or_events(tmp_path):
    closed = _closed(tmp_path)
    calls, records, events = [], [], []
    refs = {"agent_id": "critic-1", "segment_id": "segment-1", "inspection_id": closed.inspection_id}
    def invoke(*_):
        calls.append(1)
        return _reply("有位置、有问题的批评仍然只是反馈。", source_refs=refs)
    with ThreadPoolExecutor(1) as executor:
        critic = _critic(executor, invoke, records=records, events=events)
        first = critic.review(closed)
        count = len(events)
        for _ in range(3):
            cached = critic.review(replace(closed, tool_use_id="current-tool"))
            assert cached["cache_hit"] and cached["tool_use_id"] == "current-tool"
            assert cached["reused_from"]["inspection_id"] == closed.inspection_id
            assert cached["source_refs"] == refs
        resumed = _critic(executor, invoke, records=records, events=events, prior=json.loads(json.dumps(records)))
        again = resumed.review(closed)
        assert again["cache_hit"] and resumed.stats["requested"] == 0
    assert not first["cache_hit"] and first["completed"]
    assert calls == [1] and len(records) == 1 and len(events) == count == 2
    assert records[0]["disposition"] == "completed"
    assert set(critic.stats) == {"requested", "completed", "cache_hits", "failures"}


@pytest.mark.parametrize("changed", ["none", "scope", "question", "owner", "page", "model", "rubric", "pixels"])
@pytest.mark.parametrize("resume", [False, True])
def test_reuse_requires_matching_pixels_question_model_and_rubric(tmp_path, changed, resume):
    old = replace(_closed(tmp_path), question="检查标题与背景的关系")
    current = replace(old, inspection_id="inspection-2", html_sha256="html-2", tool_use_id="tool-2",
        scope_key="different-scope-hash" if changed == "scope" else old.scope_key,
        question="补看底部图例与数据" if changed == "question" else old.question,
        agent_id="owner-2" if changed == "owner" else old.agent_id,
        page=2 if changed == "page" else old.page,
        page_uid="page-2" if changed == "page" else old.page_uid)
    if changed == "pixels":
        current = replace(_closed(tmp_path, inspection_id="inspection-2", color=(2, 70, 30)), question=old.question)
    calls, records = [], []
    def invoke(_pixels, _media, closed):
        calls.append(closed)
        return _reply("前言与检查意见", source_refs={"agent_id": f"critic-{len(calls)}",
            "inspection_id": closed.inspection_id, "segment_id": f"segment-{len(calls)}"})
    with ThreadPoolExecutor(2) as executor:
        critic = _critic(executor, invoke, records=records)
        critic.review(old)
        if resume or changed in {"model", "rubric"}:
            critic = _critic(executor, invoke, records=records, prior=records,
                model="different" if changed == "model" else "test",
                rubric="different" if changed == "rubric" else RUBRIC_VERSION)
        assert len(records) == 1
        result = critic.review(current)
        repeated = critic.review(current)
    reused = changed not in {"model", "rubric", "pixels", "question", "page"}
    assert len(calls) == (1 if reused else 2)
    assert result["html_sha256"] == current.html_sha256
    assert result["inspection_id"] == current.inspection_id and result["png_sha256"] == current.png_sha256
    assert result["question"] == current.question
    assert (result["agent_id"], result["page_uid"], result["page"], result["scope_key"]) == (
        current.agent_id, current.page_uid, current.page, current.scope_key)
    assert repeated["completed"] and repeated["cache_hit"]
    assert len(records) == 2
    if reused:
        assert result["reused_from"]["inspection_id"] == old.inspection_id
        assert result["reused_from"]["page_uid"] == old.page_uid
        assert result["reused_from"]["agent_id"] == old.agent_id
        assert result["source_refs"]["inspection_id"] == old.inspection_id
        assert result["source_refs"]["agent_id"] == "critic-1"
        assert records[-1]["disposition"] == "cache_reused"
    else:
        assert not result["reused_from"]


@pytest.mark.parametrize("changed", ["none", "scope", "question", "owner", "page", "pixels"])
def test_concurrent_pending_reviews_share_only_matching_pixels_and_question(tmp_path, changed):
    old = replace(_closed(tmp_path), question="检查标题与背景的关系")
    current = replace(old, inspection_id="inspection-2", html_sha256="html-2", tool_use_id="tool-2",
        scope_key="different-scope-hash" if changed == "scope" else old.scope_key,
        question="补看底部图例与数据" if changed == "question" else old.question,
        agent_id="owner-2" if changed == "owner" else old.agent_id,
        page=2 if changed == "page" else old.page,
        page_uid="page-2" if changed == "page" else old.page_uid)
    if changed == "pixels":
        current = replace(_closed(tmp_path, inspection_id="inspection-2", color=(1, 2, 3)), question=old.question)
    entered, queued, release = threading.Event(), threading.Event(), threading.Event()
    calls, records, events = [], [], []
    def invoke(_pixels, _media, closed):
        calls.append(closed)
        entered.set()
        assert release.wait(5)
        return _reply("检查完成", source_refs={"inspection_id": closed.inspection_id})
    def event(value):
        events.append(value)
        if value.get("inspection_id") == current.inspection_id:
            queued.set()
    with ThreadPoolExecutor(2) as executor, ThreadPoolExecutor(2) as owner:
        critic = _critic(executor, invoke, records=records)
        critic._event_sink = event
        first = owner.submit(critic.review, old)
        assert entered.wait(2)
        second = owner.submit(critic.review, current)
        try:
            assert queued.wait(2)
            with pytest.raises(FutureTimeout):
                second.result(timeout=.03)
            assert not records
        finally:
            release.set()
        result = second.result(timeout=3)
        assert first.result(timeout=3)["completed"] and result["completed"]
        critic.settle_pending()
        critic.settle_pending()
    assert len(calls) == critic.stats["requested"] == critic.stats["completed"] == (
        2 if changed in {"pixels", "question", "page"} else 1)
    assert len(records) == 2 and {r["inspection_id"] for r in records} == {old.inspection_id, current.inspection_id}
    assert len([e for e in events if e["event_type"] == "critic_started"]) == len(calls)
    for record in records:
        assert record["body"] == "检查完成"
        assert record["tool_use_id"] == (current.tool_use_id if record["inspection_id"] == current.inspection_id else old.tool_use_id)
    assert (result["agent_id"], result["page_uid"], result["page"], result["scope_key"]) == (
        current.agent_id, current.page_uid, current.page, current.scope_key)
    if changed not in {"pixels", "question", "page"}:
        assert result["reused_from"]["inspection_id"] == old.inspection_id
        assert result["source_refs"]["inspection_id"] == old.inspection_id


def test_question_is_part_of_cache_identity_even_if_other_ids_are_unchanged(tmp_path):
    old = replace(_closed(tmp_path), question="标题是否受背景干扰")
    calls = []
    with ThreadPoolExecutor(1) as executor:
        critic = _critic(executor, lambda _pixels, _media, closed: calls.append(closed.question) or _reply())
        critic.review(old)
        current = replace(old, question="检查红线端点")
        assert not critic.review(current)["cache_hit"]
        assert critic.review(current)["cache_hit"]
    assert calls == [old.question, current.question]


def test_target_kind_is_part_of_critic_cache_identity(tmp_path):
    old = _closed(tmp_path)
    calls = []
    with ThreadPoolExecutor(1) as executor:
        critic = _critic(executor, lambda _pixels, _media, closed: calls.append(closed.kind) or _reply())
        for kind in ("page", "group", "asset"):
            current = replace(old, kind=kind)
            assert not critic.review(current)["cache_hit"]
            assert critic.review(current)["cache_hit"]
    assert calls == ["page", "group", "asset"]


def test_same_transaction_concurrency_does_not_duplicate_publication(tmp_path):
    closed = _closed(tmp_path)
    entered, release = threading.Event(), threading.Event()
    calls, records = [], []
    def invoke(*_):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return _reply()
    with ThreadPoolExecutor(1) as executor, ThreadPoolExecutor(4) as owner:
        critic = _critic(executor, invoke, records=records)
        futures = [owner.submit(critic.review, closed) for _ in range(4)]
        assert entered.wait(2)
        release.set()
        assert all(future.result(timeout=3)["completed"] for future in futures)
    assert calls == [1] and len(records) == 1


def test_executor_queue_waits_for_real_result_without_a_second_timeout(tmp_path):
    closed = _closed(tmp_path)
    release, waiting = threading.Event(), threading.Event()
    records = []
    with ThreadPoolExecutor(1) as executor, ThreadPoolExecutor(1) as owner:
        blocker = executor.submit(release.wait, 5)
        critic = _critic(executor, lambda *_: _reply(), records=records)
        future = owner.submit(critic.review, closed)
        try:
            with pytest.raises(FutureTimeout):
                future.result(timeout=.03)
            assert not records
        finally:
            release.set()
        assert future.result(timeout=3)["completed"]
        assert blocker.result(timeout=1)


@pytest.mark.parametrize("provider_limit", [None, 2])
def test_independent_reviews_overlap_without_bypassing_shared_provider_pool(tmp_path, provider_limit):
    from contextlib import ExitStack, nullcontext
    from runtime.production import ProductionSettings, ResourceSlots

    width = 4
    count = 1 if provider_limit is None else 2
    skill = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1"
    slots = [ResourceSlots(tmp_path / f"deck-{index}",
        ProductionSettings(skill_source=skill, model_concurrency=provider_limit or width)) for index in range(count)]
    release, saturated = threading.Event(), threading.Event()
    lock = threading.Lock()
    active = peak = 0
    entered, records = [], []

    def invoke(index, pixels, media, closed):
        nonlocal active, peak
        with slots[index].acquire("critic") if provider_limit else nullcontext():
            with lock:
                active += 1
                peak = max(peak, active)
                entered.append(closed.inspection_id)
                if active == (provider_limit or width):
                    saturated.set()
            try:
                assert release.wait(5)
                return _reply(source_refs={"inspection_id": closed.inspection_id})
            finally:
                with lock:
                    active -= 1

    with ExitStack() as stack:
        owner = stack.enter_context(ThreadPoolExecutor(count * (width + 1)))
        futures = []
        for index in range(count):
            executor = stack.enter_context(ThreadPoolExecutor(width))
            critic = _critic(executor, lambda *args, index=index: invoke(index, *args), records=records)
            for page in range(width + 1):
                closed = _closed(tmp_path, page=page + 1, page_uid=f"{index}-{page}",
                    inspection_id=f"{index}-{page}", color=(index, page, 90))
                futures.append(owner.submit(critic.review, closed))
        try:
            assert saturated.wait(3), [record.get("incomplete_reason") for record in records]
            assert len(entered) == (provider_limit or width)
            assert not any(future.done() for future in futures)
        finally:
            release.set()
        results = [future.result(timeout=5) for future in futures]
    assert peak == (provider_limit or width)
    assert len(entered) == len(records) == len(results) == count * (width + 1)
    assert all(result["completed"] and result["source_refs"]["inspection_id"] == result["inspection_id"] for result in results)


@pytest.mark.parametrize("cancelled", [False, True])
def test_cancelled_or_failed_coalesced_future_keeps_both_real_identities_incomplete(tmp_path, cancelled):
    old = _closed(tmp_path)
    current = replace(old, inspection_id="inspection-2", html_sha256="html-2", tool_use_id="tool-2",
        scope_key="different-question-hash", agent_id="owner-2")
    pending, submitted, records = Future(), [], []
    executor = SimpleNamespace(submit=lambda _fn, closed: submitted.append(closed) or pending)
    critic = _critic(executor, lambda *_: pytest.fail("future supplies execution failure"), records=records)
    critic.complete_missing(old)
    critic.complete_missing(current)
    if cancelled:
        pending.cancel()
    else:
        pending.set_exception(RuntimeError("executor failed"))
    result = critic.review(current)
    critic.settle_pending()
    assert submitted == [inspection_with_critic_question(old)] and not result["completed"]
    assert len(records) == 2 and all(not row["completed"] for row in records)
    assert result["reused_from"]["inspection_id"] == old.inspection_id
    assert critic.stats["failures"] == 1 and critic.stats["completed"] == 0
    assert all("CancelledError" in row["incomplete_reason"] for row in records) if cancelled else True


@pytest.mark.parametrize("failure", ["empty", "truncated", "provider", "cancelled"])
@pytest.mark.parametrize("resume", [False, True])
def test_incomplete_pixels_are_not_reused_as_completed_for_another_inspection(tmp_path, failure, resume):
    old = _closed(tmp_path)
    current = replace(old, inspection_id="inspection-2", html_sha256="html-2", tool_use_id="tool-2",
        scope_key="different-question-hash", agent_id="owner-2", page=2, page_uid="page-2")
    calls, records = [], []
    def invoke(*_):
        calls.append(1)
        if len(calls) == 1 and failure != "cancelled":
            if failure == "empty":
                return _reply("")
            if failure == "truncated":
                return _reply("实际已截断", stop_reason="max_tokens")
            raise RuntimeError("provider request failed")
        return _reply("新的真实检查完成", source_refs={"inspection_id": current.inspection_id})
    with ThreadPoolExecutor(1) as executor:
        critic = _critic(executor, invoke, records=records)
        if failure == "cancelled":
            cancelled = Future()
            cancelled.cancel()
            critic._executor = SimpleNamespace(submit=lambda *_: cancelled)
        first = critic.review(old)
        assert not first["completed"] and first["incomplete_reason"]
        assert records[-1]["disposition"] == "critic_incomplete"
        if failure == "cancelled":
            critic._executor = executor
        if resume:
            critic = _critic(executor, invoke, records=records, prior=records)
        result = critic.review(current)
    assert len(calls) == (1 if failure == "cancelled" else 2)
    assert result["completed"] and not result["cache_hit"]
    assert not result["reused_from"] and result["source_refs"]["inspection_id"] == current.inspection_id
    assert records[-1]["disposition"] == "completed" and len(records) == 2


def test_completed_future_survives_result_sink_failure_without_second_request(tmp_path):
    closed = _closed(tmp_path)
    calls, records = [], []
    fail = [True]
    def sink(result, disposition):
        if fail.pop() if fail else False:
            raise OSError("audit write interrupted")
        records.append(result.to_audit_dict())
    with ThreadPoolExecutor(1) as executor:
        critic = _critic(executor, lambda *_: calls.append(1) or _reply())
        critic._result_sink = sink
        with pytest.raises(OSError, match="audit write interrupted"):
            critic.review(closed)
        assert critic.review(closed)["completed"]
        critic.settle_pending()
    assert calls == [1] and len(records) == 1


def test_closed_executor_records_real_failure_without_inventing_a_request(tmp_path):
    closed = _closed(tmp_path)
    records, events = [], []
    executor = ThreadPoolExecutor(1)
    executor.shutdown(wait=True)
    critic = _critic(executor, lambda *_: pytest.fail("closed executor must not call provider"),
                     records=records, events=events)
    critic.complete_missing(closed)
    critic.settle_pending()
    assert len(records) == 1 and not records[0]["completed"]
    assert "critic_executor_unavailable" in records[0]["incomplete_reason"]
    assert critic.stats["requested"] == 0 and critic.stats["failures"] == 1
    assert [event["event_type"] for event in events] == ["critic_failed"]


def test_completion_retries_only_missing_execution_once_per_run_and_resume(tmp_path):
    closed = _closed(tmp_path)
    replies = iter([_reply(""), _reply(""), _reply("有问题，请核实标题。"), _reply("新版本检查完成")])
    calls, records = [], []
    with ThreadPoolExecutor(1) as executor:
        invoke = lambda *_: calls.append(1) or next(replies)
        critic = _critic(executor, invoke, records=records)
        assert not critic.review(closed)["completed"]
        for _ in range(4):
            critic.complete_missing(closed)
            critic.settle_pending()
        assert len(calls) == 2 and not records[-1]["completed"]
        resumed = _critic(executor, invoke, records=records, prior=records)
        resumed.complete_missing(closed)
        resumed.settle_pending()
        assert len(calls) == 3 and records[-1]["completed"]
        assert records[-1]["body"] == "有问题，请核实标题。"
        resumed = _critic(executor, invoke, records=records, prior=records)
        resumed.complete_missing(closed)
        resumed.settle_pending()
        assert len(calls) == 3
        current = _closed(tmp_path, inspection_id="inspection-2", color=(1, 2, 3))
        resumed.complete_missing(current)
        resumed.settle_pending()
        assert len(calls) == 4 and records[-1]["png_sha256"] == current.png_sha256


def test_execution_events_identify_owner_tool_page_and_pixels_without_guessing(tmp_path):
    closed = _closed(tmp_path)
    events = []
    with ThreadPoolExecutor(1) as executor:
        critic = _critic(executor, lambda *_: _reply(), events=events)
        critic.review(closed)
        current = replace(closed, inspection_id="inspection-2", html_sha256="html-2", tool_use_id="tool-2")
        critic.review(current)
    assert [event["event_type"] for event in events] == ["critic_started", "critic_completed", "critic_completed"]
    for event in events:
        identity = closed if event["inspection_id"] == closed.inspection_id else current
        assert event["owner_id"] == identity.agent_id and event["tool_use_id"] == identity.tool_use_id
        assert event["page_uid"] == identity.page_uid and event["page"] == identity.page
        assert event["png_sha256"] == identity.png_sha256 and event["html_sha256"] == identity.html_sha256
    assert events[-1]["disposition"] == "cache_reused"


@pytest.mark.parametrize("feedback", ["未见明显问题。", "标题截断，请核实页首；建议换行。", "我认为应该换一种风格。"])
def test_owner_next_turn_receives_png_and_feedback_then_may_finish_without_harness_messages(tmp_path, feedback):
    sha = _write_png(tmp_path, "renders/slide_01.png")
    tools = ToolRuntime()
    tools.register(_inspect_tool(tmp_path, "renders/slide_01.png", sha))
    transport = ScriptedTransport([_response(_tool("inspect-1", "inspect", {"page": 1})),
        _response(_text("我已自行看图，保留当前版本并结束。"))])
    with ThreadPoolExecutor(1) as executor:
        critic = _critic(executor, lambda *_: _reply(feedback))
        result = _run_owner(tmp_path, transport, tools, critic)
    assert result.turns == 2 and result.final_text == "我已自行看图，保留当前版本并结束。"
    assert len(transport.calls) == 2 and critic.stats["requested"] == 1
    messages = transport.calls[1][1].messages
    image_sources = []
    feedback_seen = False
    for message in messages:
        for block in message.get("content", []) if isinstance(message.get("content"), (list, tuple)) else []:
            if block.get("type") == "image":
                image_sources.append(block["source"])
            if block.get("type") == "tool_result":
                content = block.get("content")
                if isinstance(content, str) and feedback in content:
                    feedback_seen = True
                elif isinstance(content, (list, tuple)):
                    feedback_seen |= any(b.get("type") == "text" and feedback in b.get("text", "") for b in content)
                    image_sources.extend(b["source"] for b in content if b.get("type") == "image")
            assert not (block.get("type") == "tool_use" and block.get("name") == "think")
    assert feedback_seen and any(hashlib.sha256(base64.b64decode(source["data"])).hexdigest() == sha for source in image_sources)
    assert tool_boundary(result.checkpoint.messages).is_closed
    assert not any(message.get("_critic_provenance") for message in result.checkpoint.messages)


def test_switch_off_keeps_original_owner_image_path_without_critic(tmp_path):
    sha = _write_png(tmp_path, "renders/slide_01.png")
    tools = ToolRuntime()
    tools.register(_inspect_tool(tmp_path, "renders/slide_01.png", sha))
    transport = ScriptedTransport([_response(_tool("inspect-1", "inspect", {"page": 1})), _response(_text("完成"))])
    result = _run_owner(tmp_path, transport, tools, None)
    assert result.final_text == "完成" and result.vision_paths == ("renders/slide_01.png",)


@pytest.mark.parametrize("case", json.loads((Path(__file__).parent / "fixtures/critic_deck269_public.json").read_text()))
def test_deck_269_p6_p7_public_prefaces_are_completed_offline_protocol_responses(tmp_path, case):
    closed = _closed(tmp_path)
    result = CriticClient(invoker=lambda *_: _reply(case["body"], stop_reason=case["stop_reason"]),
                          critic_model=case["model"]).review(closed)
    assert result.completed and result.body == case["body"]
    assert result.body.splitlines()[0] not in {"READY", "REVISE"}
    assert result.incomplete_reason == "" and case["page"] in {6, 7}
