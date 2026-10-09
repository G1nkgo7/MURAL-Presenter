"""Provider 中立、可观测 Channel failover 的模型会话。

``ModelSession`` 拥有 messages、usage、Provider affinity 与 context segment，
但不了解页面、Owner、Wave、QC 或导出。Provider 调用通过 ``ProviderTransport``
注入，因此同一状态机可在不请求模型的情况下离线 replay。
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
import asyncio
import copy
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import TYPE_CHECKING, Any, AsyncIterable, Callable, ContextManager, Iterable, Mapping, Protocol, Sequence
from types import SimpleNamespace
from urllib.parse import urlsplit
import uuid

import httpx

if TYPE_CHECKING:
    import anthropic

from .config import EFFORT_LEVELS, ProviderSettings, load_runtime_config, model_effort_capability, provider_request_budget_s, service_url, validate_reasoning_effort
from .live_trace import PublicResponseStream
from .provider_health import ProviderHealth, ProviderHealthStore
from .prompt_visibility import model_visible_messages
from .raw_trace import RawAttempt, capture, request_hook, response_hook, stream_lines
from .request_labels import request_kind_public
from .vision import MAX_IMAGE_EDGE, check_image_count, check_request_bytes, normalize_images
from .recovery import (
    ProviderFailure,
    ProviderRequestTimeout,
    ResourceSlotTimeout,
    RecoveryAction,
    SegmentCheckpoint,
    classify_provider_exception,
    contains_signed_thinking,
    decide_recovery,
    roll_over_segment,
    timeout_phase,
)


@dataclass(frozen=True)
class ProviderChannel:
    """保存一条 Provider 凭证及其匿名稳定 ID。"""

    key: str = field(repr=False)
    channel_id: str = ""
    model: str = ""


@dataclass(frozen=True)
class ModelRequest:
    """保存两种 API 格式共同使用的不可变模型请求。"""

    model: str
    system: str
    messages: tuple[dict[str, Any], ...]
    tools: tuple[dict[str, Any], ...] = ()
    max_tokens: int = 40960
    thinking: bool = False
    effort: str = "medium"
    request_kind: str = "hermes_main"
    prompt_cache_key: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        """校验模型与 token 上限，并深复制可变消息。"""
        if not self.model.strip():
            raise ValueError("model must not be empty")
        if self.max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        messages = copy.deepcopy(list(self.messages))
        normalize_images(messages)
        check_image_count(messages)
        object.__setattr__(self, "messages", tuple(messages))
        object.__setattr__(self, "tools", tuple(copy.deepcopy(list(self.tools))))

    def anthropic_kwargs(self, settings: ProviderSettings) -> dict[str, object]:
        """把共享请求转换成 Anthropic Messages API 参数。"""
        messages: list[dict[str, Any]] = model_visible_messages(self.messages)
        _strip_internal_tool_result_fields(messages)
        _strip_empty_assistant_blocks(messages)
        if (
            settings.prompt_cache
            and settings.prompt_cache_messages
            and self.request_kind == "hermes_main"
        ):
            messages = _messages_with_cache_breakpoint(messages)
        system: str | list[dict[str, object]] = self.system
        if settings.prompt_cache and self.system:
            system = [{
                "type": "text",
                "text": self.system,
                "cache_control": {"type": "ephemeral"},
            }]
        kwargs: dict[str, object] = {
            "model": self.model,
            "system": system,
            "messages": messages,
            "max_tokens": self.max_tokens,
        }
        if self.tools:
            kwargs["tools"] = copy.deepcopy(list(self.tools))
        kwargs.update(_reasoning_parameters(self, "anthropic"))
        check_request_bytes(kwargs)
        return kwargs


@dataclass(frozen=True)
class TransportResponse:
    """统一包装 Provider 消息和首包耗时。"""

    message: Any
    first_byte_s: float | None


# 字符与图片启发式只用于提前压缩；不同 tokenizer/视觉处理器下不保证是 token 上界。
# 训练准入必须用目标模板完整编码验证，不能把这里的估算当作精确长度。
_CHARS_PER_TOKEN_RATIO = 0.6
# 用传输画布的 28px patch 数估算，而不是把 Base64 字符当正文 token。
_IMAGE_TOKEN_ESTIMATE = ((MAX_IMAGE_EDGE + 27) // 28) ** 2


def _estimate_text_tokens(text: str) -> int:
    """按字符系数估算文本 token，不代表目标 tokenizer 的真实计数。"""
    chars = len(text)
    if chars <= 0:
        return 0
    return int(chars * _CHARS_PER_TOKEN_RATIO) + 1


def _estimate_block_tokens(block: Mapping[str, Any]) -> int:
    """估算单个内容分块的 token:图片按固定常量,其余按其文本字段字符数。"""
    block_type = str(block.get("type") or "")
    if block_type in {"image", "input_image"}:
        return _IMAGE_TOKEN_ESTIMATE
    total = 0
    for key in ("text", "thinking", "data"):
        value = block.get(key)
        if isinstance(value, str) and value:
            total += _estimate_text_tokens(value)
    # tool_use 的参数、tool_result 的嵌套内容都可能很大;统一序列化后按字符估。
    for key in ("input", "content"):
        value = block.get(key)
        if value is None or isinstance(value, str):
            if isinstance(value, str) and key == "content":
                total += _estimate_text_tokens(value)
            continue
        if isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    total += _estimate_block_tokens(item)
                elif isinstance(item, str):
                    total += _estimate_text_tokens(item)
        elif isinstance(value, Mapping):
            try:
                total += _estimate_text_tokens(
                    json.dumps(value, ensure_ascii=False, default=str)
                )
            except (TypeError, ValueError):
                total += _estimate_text_tokens(str(value))
    return total


def _estimate_message_tokens(message: Mapping[str, Any]) -> int:
    """估算单条消息的 token,兼容字符串内容与结构化分块列表。"""
    content = message.get("content")
    if isinstance(content, str):
        return _estimate_text_tokens(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, Mapping):
                total += _estimate_block_tokens(block)
            elif isinstance(block, str):
                total += _estimate_text_tokens(block)
        return total
    return 0


def estimate_request_tokens(
    system: str | Sequence[Mapping[str, Any]],
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] = (),
) -> int:
    """估算一次请求发给 Provider 的输入 token 总量(prompt 侧,不含待生成输出)。

    只作 pre-send 的「是否接近上下文窗口」判断，不替代训练长度验收。
    图片按固定常量而非 base64 长度计，实际视觉 token 由目标 processor 决定。
    """
    total = 0
    if isinstance(system, str):
        total += _estimate_text_tokens(system)
    elif isinstance(system, Sequence):
        for block in system:
            if isinstance(block, Mapping):
                total += _estimate_block_tokens(block)
    for message in model_visible_messages(messages):
        if isinstance(message, Mapping):
            total += _estimate_message_tokens(message)
    for tool in tools or ():
        if isinstance(tool, Mapping):
            try:
                total += _estimate_text_tokens(
                    json.dumps(tool, ensure_ascii=False, default=str)
                )
            except (TypeError, ValueError):
                total += _estimate_text_tokens(str(tool))
    return total


def request_context_budget(
    settings: ProviderSettings, *, max_tokens: int, training_sample_max_tokens: int | None = None,
) -> dict[str, int | None]:
    """制作与 Critic 共用输入预算；估算预检不能替代目标训练模板的精确编码。"""
    window = settings.context_window
    if training_sample_max_tokens is not None:
        if training_sample_max_tokens <= 0:
            raise ValueError("training_sample_max_tokens must be positive")
        window = min(window, training_sample_max_tokens)
    return {
        "context_window": settings.context_window,
        "effective_context_window": window,
        "training_sample_max_tokens": training_sample_max_tokens,
        "max_tokens": max_tokens,
        "context_safety_buffer": settings.context_safety_buffer,
        "input_budget_tokens": window - max_tokens - settings.context_safety_buffer,
    }


_PUBLIC_DELTA_LOCAL = threading.local()


def _emit_public_delta(kind: str, block: str, text: str) -> None:
    sink = getattr(_PUBLIC_DELTA_LOCAL, "sink", None)
    if sink is not None:
        sink(kind, block, text)


@contextmanager
def _public_delta_sink(sink, stop_event=None):
    """把预览和停止信号绑定到请求线程，不改变可持久化请求。"""
    previous = getattr(_PUBLIC_DELTA_LOCAL, "sink", None)
    previous_stop = getattr(_PUBLIC_DELTA_LOCAL, "stop_event", None)
    _PUBLIC_DELTA_LOCAL.sink = sink
    _PUBLIC_DELTA_LOCAL.stop_event = stop_event
    try:
        yield
    finally:
        _PUBLIC_DELTA_LOCAL.sink = previous
        _PUBLIC_DELTA_LOCAL.stop_event = previous_stop


async def _request_with_deadline(request, timeout_s):
    """取消当前 HTTP 协程并等连接关闭；不停止别的 Owner 或改变工具事务。"""
    stop = getattr(_PUBLIC_DELTA_LOCAL, "stop_event", None)
    if stop is None:
        return await asyncio.wait_for(request, timeout=timeout_s)
    task = asyncio.create_task(request)
    deadline = asyncio.get_running_loop().time() + timeout_s if timeout_s is not None else None
    try:
        while True:
            if stop.is_set():
                raise InterruptedError("owner_interrupted: provider request cancelled for workspace resume")
            remaining = deadline - asyncio.get_running_loop().time() if deadline is not None else None
            if remaining is not None and remaining <= 0:
                raise asyncio.TimeoutError()
            done, _ = await asyncio.wait((task,), timeout=min(0.1, remaining) if remaining is not None else 0.1)
            if done:
                return task.result()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@dataclass(frozen=True)
class NormalizedAssistantResponse:
    """Provider 中立的助手响应,把各家 wire 结构归一为同一契约。

    - ``visible_blocks``:公开可见的 text/tool_use 分块(不含任何思考内容)。
    - ``analysis_summaries``:**可公开**的分析摘要(Anthropic summarized thinking、
      OpenAI Responses ``reasoning.summary[]``)。自托管 Chat Completions 的 raw
      reasoning 绝不进这里。
    - ``tool_calls``:结构化工具调用分块(``type == "tool_use"``)。
    - ``stop_reason`` / ``stop_details`` / ``usage``:终止原因、可公开分类字段与用量对象。
    - ``content``:供 ``agent.py`` / signed-thinking 检测消费的持久分块列表。对
      Anthropic 保留带 signature 的原始 thinking 分块;对 OpenAI 只含可见分块。
      raw reasoning / encrypted content 永不出现在这里。
    """

    content: tuple[dict[str, Any], ...]
    analysis_summaries: tuple[str, ...]
    stop_reason: str
    usage: Any
    stop_details: Mapping[str, str] = field(default_factory=dict)

    @property
    def visible_blocks(self) -> tuple[dict[str, Any], ...]:
        """返回除思考外的公开分块(text 与 tool_use)。"""
        return tuple(
            block
            for block in self.content
            if str(block.get("type") or "")
            not in {"thinking", "redacted_thinking"}
        )

    @property
    def tool_calls(self) -> tuple[dict[str, Any], ...]:
        """返回结构化工具调用分块。"""
        return tuple(
            block for block in self.content if block.get("type") == "tool_use"
        )


def _dedup_summaries(summaries: Iterable[str]) -> tuple[str, ...]:
    """按出现顺序去重并丢弃空白摘要,供分析摘要拼接使用。"""
    seen: dict[str, None] = {}
    for summary in summaries:
        text = str(summary or "").strip()
        if text and text not in seen:
            seen[text] = None
    return tuple(seen)


def _strip_internal_tool_result_fields(
    messages: list[dict[str, Any]],
) -> None:
    """就地移除只供 OpenAI 转换使用的内部键，保持 Anthropic tool_result schema 合规。"""
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
            ):
                block.pop("error_code", None)
                block.pop("vision_keep", None)


def _strip_empty_assistant_blocks(messages: list[dict[str, Any]]) -> None:
    """Anthropic wire projection rejects empty text/thinking blocks; raw history stays intact.

    Keep tool calls, their arguments, real thinking and redacted data unchanged.
    """
    for message in messages:
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        kept = [
            block
            for block in content
            if not (
                isinstance(block, dict)
                and (
                    (block.get("type") == "text" and block.get("text") == "")
                    or (str(block.get("type") or "") in {"thinking", "redacted_thinking"}
                        and not str(block.get("thinking") or "").strip()
                        and not str(block.get("data") or "").strip())
                )
            )
        ]
        if len(kept) != len(content):
            message["content"] = kept
    messages[:] = [message for message in messages
                   if not (message.get("role") == "assistant" and message.get("content") == [])]


class ProviderTransport(Protocol):
    """定义所有 Provider 适配器必须实现的同步接口。"""

    def complete(
        self,
        channel: ProviderChannel,
        request: ModelRequest,
        *,
        total_timeout_s: float | None,
    ) -> TransportResponse:
        """在给定 Channel 与总时限内完成一次请求。"""
        ...


@dataclass(frozen=True)
class SessionResult:
    """描述一次会话调用的成功结果或可恢复失败。"""

    response: Any | None
    channel_id: str
    attempts: int
    failure: ProviderFailure | None = None
    segment_rollover_required: bool = False
    next_channel_id: str = ""
    attempt_id: str = ""

    @property
    def ok(self) -> bool:
        """判断调用是否得到响应且没有失败记录。"""
        return self.response is not None and self.failure is None


@dataclass(frozen=True)
class ProviderReplaySummary:
    """保存 Provider Trace 重放后的计数摘要。"""

    attempts: int
    successes: int
    failures: int
    failovers: int
    timeout_phases: Mapping[str, int]
    channel_attempts: Mapping[str, int]
    open_attempt_ids: tuple[str, ...] = ()


class UsageLedger:
    """线程安全地累计 token、调用、失败和 Channel 指标。"""

    def __init__(self) -> None:
        """建立空的 usage 与 Provider 尝试计数器。"""
        self._lock = threading.Lock()
        self._recorded_attempts: set[str] = set()
        self._settled_attempts: set[str] = set()
        self._values: dict[str, object] = {
            "input_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 0,
            "calls": 0,
            "attempts": 0,
            "failed_attempts": 0,
            "provider_failovers": 0,
            "provider_channel_attempts": {},
            "provider_channel_successes": {},
            "provider_channel_failures": {},
            "provider_timeout_phases": {},
        }

    def record_attempt(self, channel_id: str, *, attempt_id: str = "") -> None:
        """记录某个 Channel 开始了一次请求。"""
        with self._lock:
            if not self._claim_attempt(self._recorded_attempts, attempt_id):
                return
            self._values["attempts"] = int(self._values["attempts"]) + 1
            self._increment("provider_channel_attempts", channel_id)

    def record_failure(self, channel_id: str, timeout: str = "", *, attempt_id: str = "") -> None:
        """记录请求失败以及可选的超时阶段。"""
        with self._lock:
            if not self._claim_attempt(self._settled_attempts, attempt_id):
                return
            self._values["failed_attempts"] = (
                int(self._values["failed_attempts"]) + 1
            )
            self._increment("provider_channel_failures", channel_id)
            if timeout:
                self._increment("provider_timeout_phases", timeout)

    def record_rejected_response(self, channel_id: str, response: Any, *, attempt_id: str = "") -> None:
        """Account a billed HTTP-200 response that is semantically unusable."""
        usage = getattr(response, "usage", None)
        with self._lock:
            if not self._claim_attempt(self._settled_attempts, attempt_id):
                return
            self._values["failed_attempts"] = int(self._values["failed_attempts"]) + 1
            self._increment("provider_channel_failures", channel_id)
            for name in (
                "input_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
                "output_tokens",
            ):
                self._values[name] = int(self._values[name]) + int(
                    getattr(usage, name, 0) or 0
                )

    def record_failover(self) -> None:
        """记录一次实际跨 Channel 重试。"""
        with self._lock:
            self._values["provider_failovers"] = (
                int(self._values["provider_failovers"]) + 1
            )

    def record_success(self, channel_id: str, response: Any, *, attempt_id: str = "") -> None:
        """累计成功响应的 token 与 Channel 成功次数。"""
        usage = getattr(response, "usage", None)
        with self._lock:
            if not self._claim_attempt(self._settled_attempts, attempt_id):
                return
            self._values["calls"] = int(self._values["calls"]) + 1
            self._increment("provider_channel_successes", channel_id)
            for name in (
                "input_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
                "output_tokens",
            ):
                self._values[name] = int(self._values[name]) + int(
                    getattr(usage, name, 0) or 0
                )

    def summary(self) -> dict[str, object]:
        """返回调用方不可修改内部状态的统计副本。"""
        with self._lock:
            return copy.deepcopy(self._values)

    def _increment(self, name: str, key: str) -> None:
        """递增一个按字符串键分组的内部计数器。"""
        values = self._values[name]
        if not isinstance(values, dict):
            raise TypeError(f"usage field {name!r} is not a counter map")
        values[key] = int(values.get(key, 0) or 0) + 1

    @staticmethod
    def _claim_attempt(recorded: set[str], attempt_id: str) -> bool:
        if not attempt_id:
            return True
        if attempt_id in recorded:
            return False
        recorded.add(attempt_id)
        return True


class ChannelPool:
    """管理 Channel 顺序、冷却、亲和性和替代选择。"""

    def __init__(
        self,
        channels: Sequence[ProviderChannel],
        *,
        cooldown_s: int = 120,
        balance: str = "balanced",
        initial_segment_id: str = "",
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """校验 Channel ID 并初始化轮转及冷却状态。"""
        if not channels:
            raise ValueError("at least one provider channel is required")
        ids = [channel.channel_id for channel in channels]
        if len(ids) != len(set(ids)) or any(not value for value in ids):
            raise ValueError("provider channel IDs must be non-empty and unique")
        self.channels = tuple(channels)
        self.cooldown_s = max(0, int(cooldown_s))
        self.balance = balance.strip().lower()
        self.monotonic = monotonic
        # 新 Owner/Deck 各建一个池；从已有段身份分散起点，避免全部先用账号 1 再被签名锁定。
        self._cursor = (int(hashlib.sha256(initial_segment_id.encode()).hexdigest(), 16)
                        if initial_segment_id else 0)
        self._cooldowns: dict[str, float] = {}
        self._lock = threading.Lock()

    def request_order(
        self,
        *,
        affinity_id: str = "",
        soft_affinity_id: str = "",
        excluded_ids: Sequence[str] = (),
        signed_history: bool = False,
    ) -> tuple[tuple[ProviderChannel, ...], bool]:
        """为当前 segment 生成候选顺序并标记是否锁定。"""
        excluded = {str(value) for value in excluded_ids if value}
        if affinity_id:
            return (
                tuple(
                    channel
                    for channel in self.channels
                    if channel.channel_id == affinity_id and channel.channel_id not in excluded
                ),
                True,
            )
        available = tuple(
            channel for channel in self.channels if channel.channel_id not in excluded
        )
        ordered = self._balanced_order(available)
        if signed_history:
            return ordered[:1], True
        if soft_affinity_id:
            ordered = tuple(channel for channel in ordered if channel.channel_id == soft_affinity_id) + tuple(
                channel for channel in ordered if channel.channel_id != soft_affinity_id
            )
        return ordered, False

    def select(
        self,
        ordered: Sequence[ProviderChannel],
        attempted_ids: set[str],
        attempt: int,
        *,
        forced_channel_id: str = "",
    ) -> ProviderChannel:
        """优先选择未尝试且未冷却的候选 Channel。"""
        with self._lock:
            now = self.monotonic()
            delays = {channel.channel_id: max(0.0, self._cooldowns.get(channel.channel_id, 0.0) - now)
                      for channel in ordered}
        earliest = min(delays.values())
        candidates = [channel for channel in ordered if delays[channel.channel_id] == earliest]
        if forced_channel_id:
            for channel in candidates:
                if channel.channel_id == forced_channel_id:
                    return channel
        for channel in candidates:
            if channel.channel_id not in attempted_ids:
                return channel
        return candidates[(max(1, attempt) - 1) % len(candidates)]

    def alternative(
        self, current: ProviderChannel, excluded_ids: Sequence[str] = (),
        *, attempted_ids: set[str] | None = None,
    ) -> ProviderChannel | None:
        """查找不同于当前且未被排除的替代 Channel。"""
        excluded = {str(value) for value in excluded_ids if value}
        candidates = [
            channel
            for channel in self.channels
            if channel.channel_id != current.channel_id
            and channel.channel_id not in excluded
        ]
        return self.select(candidates, attempted_ids or set(), 1) if candidates else None

    def cool_down(self, channel: ProviderChannel, retry_after_s: float | None = None) -> None:
        """在可重试失败后暂时冷却指定 Channel。"""
        delay = self.cooldown_s if retry_after_s is None else retry_after_s
        if delay <= 0:
            return
        with self._lock:
            self._cooldowns[channel.channel_id] = max(
                self._cooldowns.get(channel.channel_id, 0.0), self.monotonic() + delay
            )

    def cooldown_remaining(self, channel: ProviderChannel) -> float:
        with self._lock:
            return max(0.0, self._cooldowns.get(channel.channel_id, 0.0) - self.monotonic())

    def on_cooldown(self, channel: ProviderChannel) -> bool:
        """检查冷却是否仍有效，并清理已到期记录。"""
        now = self.monotonic()
        with self._lock:
            until = float(self._cooldowns.get(channel.channel_id, 0.0) or 0.0)
            if until and until <= now:
                self._cooldowns.pop(channel.channel_id, None)
                return False
            return until > now

    def _balanced_order(
        self, channels: tuple[ProviderChannel, ...]
    ) -> tuple[ProviderChannel, ...]:
        """为新的 unsigned segment 轮转首选 Channel。"""
        if len(channels) < 2 or self.balance not in {
            "balanced",
            "round_robin",
            "round-robin",
        }:
            return channels
        with self._lock:
            start = self._cursor % len(channels)
            self._cursor += 1
        return channels[start:] + channels[:start]


class AnthropicTransport:
    """同步入口、可取消的单次 Anthropic 请求；空动作恢复由 Agent 负责。"""

    def __init__(self, settings: ProviderSettings) -> None:
        """绑定 Anthropic API 格式的非敏感配置。"""
        if settings.api_format != "anthropic":
            raise ValueError("AnthropicTransport requires the anthropic API format")
        # 只加载本次选中的 SDK，且在资源排队与请求 deadline 开始前完成导入。
        import anthropic

        self._client_type = anthropic.AsyncAnthropic
        self.settings = settings

    def complete(
        self,
        channel: ProviderChannel,
        request: ModelRequest,
        *,
        total_timeout_s: float | None,
    ) -> TransportResponse:
        """取消响应 IO 并关闭客户端后返回；DNS/系统清理仍可能增加返回尾延迟。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("AnthropicTransport.complete requires a synchronous caller")
        started = time.monotonic()
        try:
            return asyncio.run(_request_with_deadline(
                self._request_once(channel, request, total_timeout_s), total_timeout_s
            ))
        except ProviderRequestTimeout:
            raise
        except asyncio.TimeoutError as exc:
            raise ProviderRequestTimeout("total_request", time.monotonic() - started) from exc

    async def _request_once(
        self,
        channel: ProviderChannel,
        request: ModelRequest,
        total_timeout_s: float | None,
    ) -> TransportResponse:
        """总 deadline 覆盖连接与完整响应；socket idle timeout 不代替总时限。"""
        kwargs = request.anthropic_kwargs(self.settings)
        kwargs["timeout"] = _request_timeout(
            self.settings, total_timeout_s, streaming=self.settings.streaming
        )
        started = time.monotonic()
        response_started = False
        first_byte_s = 0.0
        try:
            async with self._client(channel) as client:
                if not self.settings.streaming:
                    response = await client.messages.create(**kwargs)
                else:
                    async with client.messages.stream(**kwargs) as stream:
                        async for _event in stream:
                            capture("stream", _event.model_dump(mode="json") if hasattr(_event, "model_dump") else vars(_event))
                            if not response_started:
                                response_started = True
                                first_byte_s = time.monotonic() - started
                            if getattr(_PUBLIC_DELTA_LOCAL, "sink", None) is None:
                                continue
                            event_type = getattr(_event, "type", "")
                            block_id = str(getattr(_event, "index", 0))
                            if event_type == "content_block_start":
                                block = getattr(_event, "content_block", None)
                                if getattr(block, "type", "") == "text":
                                    _emit_public_delta("message", block_id, getattr(block, "text", ""))
                                elif getattr(block, "type", "") == "thinking" and request.thinking:
                                    _emit_public_delta("summary", block_id, getattr(block, "thinking", ""))
                            elif event_type == "content_block_delta":
                                delta = getattr(_event, "delta", None)
                                if getattr(delta, "type", "") == "text_delta":
                                    _emit_public_delta("message", block_id, delta.text)
                                elif getattr(delta, "type", "") == "thinking_delta" and request.thinking:
                                    _emit_public_delta("summary", block_id, delta.thinking)
                        response = await stream.get_final_message()
            capture("response", response.model_dump(mode="json") if hasattr(response, "model_dump") else {
                "content": [_anthropic_block_to_dict(block) for block in getattr(response, "content", [])],
                "stop_reason": getattr(response, "stop_reason", None)})
            elapsed = time.monotonic() - started
            if total_timeout_s is not None and elapsed > total_timeout_s:
                raise ProviderRequestTimeout("total_request", elapsed)
            return TransportResponse(
                _normalize_anthropic_response(response),
                first_byte_s if response_started else None,
            )
        except Exception as exc:
            phase = timeout_phase(exc, response_started=response_started)
            if phase and not isinstance(exc, ProviderRequestTimeout):
                raise ProviderRequestTimeout(
                    phase, time.monotonic() - started, type(exc).__name__
                ) from exc
            raise

    def _client(self, channel: ProviderChannel) -> anthropic.AsyncAnthropic:
        """单请求独占客户端，取消时只关闭本次连接，不影响其他 Owner。"""
        return self._client_type(
            api_key=channel.key,
            base_url=self.settings.base_url,
            http_client=httpx.AsyncClient(trust_env=False, event_hooks={"request": [request_hook], "response": [response_hook]}),
            max_retries=0,
        )


class OpenAIChatCompatibleTransport:
    """OpenAI Chat Completions 兼容适配器(自托管 Qwen/DeepSeek/27B)。

    在 transport 边界转换共享的 Anthropic 风格 message/tool envelope。DeepSeek、
    Qwen/vLLM 与 OpenAI Chat reasoning 字段互斥。自托管端点无法切到官方 Responses
    API,继续使用现有 reasoning service / vendor-native 接口;官方 Responses 由
    ``OpenAIResponsesTransport`` 单独处理。
    """

    def __init__(
        self,
        settings: ProviderSettings,
        *,
        client_factory: Callable[[], Any] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """绑定自托管 Chat Completions 兼容格式以及可替换的 HTTP 调用器。"""
        if settings.api_format != "openai_chat_compatible":
            raise ValueError(
                "OpenAIChatCompatibleTransport requires the "
                "openai_chat_compatible API format"
            )
        self.settings = settings
        self.client_factory = client_factory or (lambda: httpx.AsyncClient(follow_redirects=True))
        self.monotonic = monotonic

    def complete(
        self,
        channel: ProviderChannel,
        request: ModelRequest,
        *,
        total_timeout_s: float | None,
    ) -> TransportResponse:
        """执行流式或非流式 Chat Completions 并返回统一消息结构。"""
        body = openai_chat_body(
            request,
            temperature=self.settings.temperature,
            reasoning_transport=self.settings.reasoning_transport,
        )
        if self.settings.streaming:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
            if self.settings.return_token_ids:
                body["return_token_ids"] = True
        return _complete_openai_http(
            self.settings, channel, body,
            endpoint=normalize_openai_chat_url(self.settings.base_url),
            parse_stream=_read_openai_sse,
            normalize=_normalize_openai_response,
            client_factory=self.client_factory,
            monotonic=self.monotonic,
            total_timeout_s=total_timeout_s,
        )


class OpenAIResponsesTransport:
    """OpenAI 官方 Responses API 适配器(官方 GPT reasoning 模型)。

    Responses API 提供**可公开**的独立 reasoning summary(``reasoning.summary[]``
    与流式 ``response.reasoning_summary_text.delta`` 事件),归一层安全映射到
    ``analysis_summaries``。原始 reasoning / encrypted content 绝不进公开事件。
    仅用于官方端点;自托管兼容模型走 ``OpenAIChatCompatibleTransport``。
    """

    def __init__(
        self,
        settings: ProviderSettings,
        *,
        client_factory: Callable[[], Any] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """绑定官方 Responses API 格式以及可替换的 HTTP 调用器。"""
        if settings.api_format != "openai_responses":
            raise ValueError(
                "OpenAIResponsesTransport requires the openai_responses API format"
            )
        self.settings = settings
        self.client_factory = client_factory or (lambda: httpx.AsyncClient(follow_redirects=True))
        self.monotonic = monotonic

    def complete(
        self,
        channel: ProviderChannel,
        request: ModelRequest,
        *,
        total_timeout_s: float | None,
    ) -> TransportResponse:
        """执行流式或非流式 Responses 请求并返回归一响应。"""
        body = openai_responses_body(request)
        if self.settings.streaming:
            body["stream"] = True
        return _complete_openai_http(
            self.settings, channel, body,
            endpoint=normalize_openai_responses_url(self.settings.base_url),
            parse_stream=_read_openai_responses_sse,
            normalize=_normalize_openai_responses_response,
            client_factory=self.client_factory,
            monotonic=self.monotonic,
            total_timeout_s=total_timeout_s,
        )


def _complete_openai_http(
    settings: ProviderSettings,
    channel: ProviderChannel,
    body: dict[str, Any],
    *,
    endpoint: str,
    parse_stream: Callable[..., Any],
    normalize: Callable[..., NormalizedAssistantResponse],
    client_factory: Callable[[], Any],
    monotonic: Callable[[], float],
    total_timeout_s: float,
) -> TransportResponse:
    """两种格式共用可取消的 HTTP 生命周期；关闭连接后才返回并释放上层槽位。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("OpenAI transport.complete requires a synchronous caller")
    started = monotonic()
    check_request_bytes(body)

    async def request_once() -> TransportResponse:
        response_started = False
        first_byte_s = 0.0
        try:
            request_timeout = _request_timeout(settings, total_timeout_s, streaming=settings.streaming)
            capture("request", body, {"Content-Type": "application/json", "Authorization": f"Bearer {channel.key}"},
                    timeout={"total": total_timeout_s, "read": request_timeout.read,
                             "connect": request_timeout.connect})
            async with client_factory() as client:
                async with client.stream(
                    "POST", endpoint,
                    content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {channel.key}"},
                    timeout=request_timeout,
                ) as response:
                    capture("response_headers", response.status_code, getattr(response, "headers", {}))
                    if response.status_code >= 400:
                        await response.aread()
                        response.raise_for_status()
                    if settings.streaming:
                        decoded, first_byte_s = await parse_stream(
                            stream_lines(response.aiter_lines()), started=started,
                            total_timeout_s=total_timeout_s, monotonic=monotonic,
                            on_public_delta=_emit_public_delta,
                        )
                        response_started = first_byte_s > 0
                    else:
                        decoded = json.loads(await response.aread())
                        first_byte_s = None
                        response_started = True
            capture("response", decoded)
            elapsed = monotonic() - started
            if total_timeout_s is not None and elapsed > total_timeout_s:
                raise ProviderRequestTimeout("total_request", elapsed)
            return TransportResponse(
                normalize(decoded),
                first_byte_s=first_byte_s,
            )
        except Exception as exc:
            phase = timeout_phase(exc, response_started=response_started)
            if phase and not isinstance(exc, ProviderRequestTimeout):
                raise ProviderRequestTimeout(
                    phase, monotonic() - started, type(exc).__name__
                ) from exc
            raise

    try:
        return asyncio.run(_request_with_deadline(request_once(), total_timeout_s))
    except ProviderRequestTimeout:
        raise
    except asyncio.TimeoutError as exc:
        raise ProviderRequestTimeout("total_request", monotonic() - started) from exc


async def _read_openai_sse(
    response: AsyncIterable[bytes | str],
    *,
    started: float,
    total_timeout_s: float | None,
    monotonic: Callable[[], float],
    on_public_delta: Callable[[str, str, str], None] | None = None,
) -> tuple[dict[str, object], float]:
    """读取 OpenAI SSE 增量并还原成普通 Chat Completions 响应。"""
    first_byte_s = 0.0
    response_started = False
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    finish_reason = ""
    stream_done = False
    usage: dict[str, object] = {}
    try:
        async for raw_line in response:
            now = monotonic()
            if total_timeout_s is not None and now - started > total_timeout_s:
                raise ProviderRequestTimeout("total_request", now - started)
            line = (
                raw_line.decode("utf-8")
                if isinstance(raw_line, (bytes, bytearray))
                else str(raw_line)
            ).strip()
            if not line or line.startswith(":") or not line.startswith("data:"):
                continue
            if not response_started:
                response_started = True
                first_byte_s = max(0.0, now - started)
            payload = line[5:].strip()
            if payload == "[DONE]":
                stream_done = True
                break
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                raise ProviderProtocolError(
                    "Chat stream event contains invalid JSON; partial output is not executable"
                ) from None
            if not isinstance(event, dict):
                raise ValueError("OpenAI stream event must be an object")
            error = event.get("error")
            if error:
                raise ValueError(f"OpenAI stream returned an error: {error}")
            usage_value = event.get("usage")
            if isinstance(usage_value, dict):
                usage = dict(usage_value)
            choices = event.get("choices")
            if not isinstance(choices, list):
                continue
            for choice in choices:
                if not isinstance(choice, Mapping):
                    continue
                terminal = choice.get("finish_reason")
                if terminal:
                    finish_reason = str(terminal)
                delta = choice.get("delta")
                if not isinstance(delta, Mapping):
                    continue
                public_text = _openai_delta_text(delta.get("content"))
                content_parts.extend(public_text)
                if on_public_delta is not None:
                    on_public_delta("message", "0", "".join(public_text))
                reasoning_parts.extend(
                    _openai_delta_text(
                        delta.get("reasoning_content") or delta.get("reasoning")
                    )
                )
                raw_calls = delta.get("tool_calls")
                if not isinstance(raw_calls, list):
                    continue
                for fallback_index, raw_call in enumerate(raw_calls):
                    if not isinstance(raw_call, Mapping):
                        continue
                    index = int(raw_call.get("index", fallback_index))
                    accumulated = tool_calls.setdefault(
                        index,
                        {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        },
                    )
                    if raw_call.get("id"):
                        accumulated["id"] = str(accumulated["id"]) + str(
                            raw_call["id"]
                        )
                    function = raw_call.get("function")
                    if isinstance(function, Mapping):
                        target = accumulated["function"]
                        if not isinstance(target, dict):
                            raise TypeError("invalid accumulated OpenAI tool call")
                        if function.get("name"):
                            target["name"] = str(target["name"]) + str(
                                function["name"]
                            )
                        if function.get("arguments"):
                            if not isinstance(function["arguments"], str):
                                raise ProviderProtocolError("Chat streamed tool arguments must be JSON text deltas")
                            target["arguments"] = str(target["arguments"]) + str(
                                function["arguments"]
                            )
    except Exception as exc:
        phase = timeout_phase(exc, response_started=response_started)
        if phase and not isinstance(exc, ProviderRequestTimeout):
            raise ProviderRequestTimeout(
                phase, monotonic() - started, type(exc).__name__
            ) from exc
        raise
    if not response_started:
        raise ValueError("OpenAI stream ended before the first data event")
    if not stream_done or not finish_reason:
        raise ProviderProtocolError("Chat stream ended without finish_reason and [DONE]; partial output is not executable")
    message: dict[str, object] = {
        "role": "assistant",
        "content": "".join(content_parts) or None,
    }
    reasoning = "".join(reasoning_parts)
    if reasoning:
        message["reasoning_content"] = reasoning
    if tool_calls:
        message["tool_calls"] = [tool_calls[index] for index in sorted(tool_calls)]
    return (
        {
            "choices": [{"message": message, "finish_reason": finish_reason}],
            "usage": usage,
        },
        first_byte_s,
    )


def _openai_delta_text(value: object) -> list[str]:
    """把字符串或内容分块统一提取为文本增量。"""
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        return []
    parts: list[str] = []
    for item in value:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, Mapping) and item.get("text") is not None:
            parts.append(str(item.get("text") or ""))
    return parts


def _anthropic_block_to_dict(item: Any) -> dict[str, Any]:
    """把 Anthropic SDK 内容分块转成普通 dict,保留 signature 等字段。"""
    if isinstance(item, Mapping):
        return copy.deepcopy(dict(item))
    if callable(getattr(item, "model_dump", None)):
        return copy.deepcopy(item.model_dump(exclude_none=True))
    block_type = str(getattr(item, "type", "") or "")
    value: dict[str, Any] = {"type": block_type}
    for target in ("text", "thinking", "signature", "data", "id", "name", "input"):
        candidate = getattr(item, target, None)
        if candidate is not None:
            value[target] = copy.deepcopy(candidate)
    return value


def _normalized_stop_details(response: Any) -> dict[str, str]:
    """Keep only non-sensitive provider termination classifiers."""
    raw = getattr(response, "stop_details", None)
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump(mode="json")
    elif raw is not None and not isinstance(raw, Mapping):
        raw = {
            key: getattr(raw, key, None)
            for key in ("type", "category")
        }
    if not isinstance(raw, Mapping):
        return {}
    return {
        key: str(raw[key])[:200]
        for key in ("type", "category")
        if isinstance(raw.get(key), (str, int)) and str(raw[key]).strip()
    }


def _provider_refusal_failure(response: Any) -> ProviderFailure | None:
    """Classify an HTTP-200 provider refusal before Agent empty-response recovery."""
    details = _normalized_stop_details(response)
    stop_reason = str(getattr(response, "stop_reason", "") or "").strip().lower()
    detail_type = str(details.get("type") or "").strip().lower()
    if stop_reason != "refusal" and detail_type != "refusal":
        return None
    category = str(details.get("category") or "unspecified")
    return ProviderFailure(
        kind="provider_policy_refusal",
        detail=f"provider returned a refusal response (category={category})",
        retryable=False,
        channel_specific=True,
    )


def _response_diagnostics(response: Any) -> dict[str, Any]:
    """把一次 Provider 响应压成**结构化诊断**,供 provider-events 落盘排障用。

    只记形状/元数据,不落敏感正文:stop_reason、输出 token、每个内容分块的类型;
    对 tool_use 额外记工具名、参数键、参数 JSON 字节数、以及 ``empty_args``/
    ``arg_keys`` —— 让「opus-5 发出空参/残缺工具调用」这类问题在 trace 里可直接看到
    (BUG-P/BUG-Q 排障)。text/thinking 只记长度不记内容。
    """
    raw = getattr(response, "content", None)
    # Anthropic SDK 返回 list;Provider 中立的 NormalizedAssistantResponse 用 tuple。
    # 两者都是合法分块序列 —— 只认 list 会把归一响应(tuple)误判成空 blocks,
    # 使 has_empty_tool_call 永远为 False、诊断全程漏报(BUG-Q 排障失效的真因)。
    blocks = list(raw) if isinstance(raw, (list, tuple)) else []
    usage = getattr(response, "usage", None)
    block_diags: list[dict[str, Any]] = []
    for item in blocks:
        block = item if isinstance(item, Mapping) else _anthropic_block_to_dict(item)
        btype = str(block.get("type") or "")
        entry: dict[str, Any] = {"type": btype}
        if btype == "tool_use":
            args = block.get("input")
            arg_keys = sorted(args.keys()) if isinstance(args, Mapping) else []
            entry["tool_name"] = str(block.get("name") or "")
            entry["arg_keys"] = arg_keys
            entry["arg_bytes"] = len(
                json.dumps(args, ensure_ascii=False) if args is not None else ""
            )
            entry["empty_args"] = not bool(args)
        elif btype in {"text", "thinking", "redacted_thinking"}:
            entry["text_len"] = len(
                str(block.get("text") or block.get("thinking") or block.get("data") or "")
            )
        block_diags.append(entry)
    details = _normalized_stop_details(response)
    return {
        "stop_reason": str(getattr(response, "stop_reason", "") or ""),
        **({"stop_details": details} if details else {}),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0)
        if usage is not None
        else 0,
        "block_count": len(block_diags),
        "blocks": block_diags,
        "has_empty_tool_call": any(
            b.get("type") == "tool_use" and b.get("empty_args") for b in block_diags
        ),
    }


def _provider_error_fields(exc: Exception) -> Mapping[str, Any]:
    body = getattr(exc, "body", None)
    response = getattr(exc, "response", None)
    if not isinstance(body, Mapping) and isinstance(response, httpx.Response) and response.is_stream_consumed:
        if len(response.content) <= 16384:
            try:
                body = response.json()
            except ValueError:
                pass
    error = body.get("error", body) if isinstance(body, Mapping) else None
    return error if isinstance(error, Mapping) else {}


def _masked_k3_gateway_error(exc: Exception, request: ModelRequest, settings: ProviderSettings) -> bool:
    if (request.model.strip().lower() != "kimi-k3"
            or urlsplit(settings.base_url).hostname != "tokenhub.sensetime.com"):
        return False
    error = _provider_error_fields(exc)
    return (error.get("type") == "invalid_request_error"
            and error.get("code") == "invalid_request"
            and error.get("param") in (None, "")
            and str(error.get("message") or "").startswith("请求参数有误，请检查请求格式与参数"))


def _provider_error_summary(exc: Exception) -> str:
    """只展示供应商 error 的公开字段，不序列化异常携带的请求或完整响应。"""
    if isinstance(exc, ProviderProtocolError):
        return str(exc)
    error = _provider_error_fields(exc)
    fields = {}
    for key in ("type", "code", "message"):
        value = error.get(key)
        if isinstance(value, (str, int)):
            text = str(value)
            fields[key] = text[:1000]
    return "; ".join(f"{key}: {value}" for key, value in fields.items()) or type(exc).__name__


def _normalize_anthropic_response(response: Any) -> NormalizedAssistantResponse:
    """把 Anthropic 消息归一;summarized thinking → 公开分析摘要。

    ``content`` 保留原始分块(含带 signature 的 thinking),以便下游做 signed-thinking
    亲和检测与持久化。公开的 summarized thinking 文本另抽进 ``analysis_summaries``。
    """
    raw = getattr(response, "content", None)
    blocks = (
        [_anthropic_block_to_dict(item) for item in raw]
        if isinstance(raw, list)
        else []
    )
    if any(block.get("type") == "tool_use" and not isinstance(block.get("input"), dict) for block in blocks):
        raise ProviderProtocolError("Anthropic tool_use.input must be a JSON object")
    summaries = _dedup_summaries(
        str(block.get("thinking") or "")
        for block in blocks
        if str(block.get("type") or "") == "thinking"
    )
    return NormalizedAssistantResponse(
        content=tuple(blocks),
        analysis_summaries=summaries,
        stop_reason=str(getattr(response, "stop_reason", "") or "end_turn"),
        usage=getattr(response, "usage", None),
        stop_details=_normalized_stop_details(response),
    )


def _normalize_openai_response(decoded: object) -> NormalizedAssistantResponse:
    """把 OpenAI Chat Completions 响应归一为 Provider 中立结构。

    自托管兼容端点没有可安全公开的分析摘要,因此 ``analysis_summaries`` 恒为空;
    raw reasoning 只作为内部 ``thinking`` 分块留在 ``content`` 里,不进公开事件。
    """
    if not isinstance(decoded, dict):
        raise ValueError("OpenAI Chat response must be an object")
    choices = decoded.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("OpenAI Chat response is missing choices")
    choice = choices[0]
    if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
        raise ValueError("OpenAI Chat response is missing a message")
    message = choice["message"]
    finish_reason = str(choice.get("finish_reason") or "stop")
    usage_value = decoded.get("usage")
    usage = usage_value if isinstance(usage_value, dict) else {}
    details_value = usage.get("prompt_tokens_details")
    details = details_value if isinstance(details_value, dict) else {}
    return NormalizedAssistantResponse(
        content=tuple(_openai_response_blocks(message)),
        analysis_summaries=(),
        stop_reason={
            "length": "max_tokens",
            "tool_calls": "tool_use",
            "stop": "end_turn",
        }.get(finish_reason, "end_turn"),
        usage=SimpleNamespace(
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            cache_read_input_tokens=int(details.get("cached_tokens") or 0),
            cache_creation_input_tokens=0,
        ),
    )


class _SharedProviderHealthChanged(RuntimeError):
    """A different session changed eligibility while this session queued locally."""


class ModelSession:
    """执行带 Channel failover、usage 和 segment 边界的模型调用。"""

    def __init__(
        self,
        *,
        settings: ProviderSettings,
        channels: Sequence[ProviderChannel],
        transport: ProviderTransport,
        checkpoint: SegmentCheckpoint | None = None,
        event_sink: Callable[[Mapping[str, object]], None] | None = None,
        usage: UsageLedger | None = None,
        balance: str = "balanced",
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        slot_factory: Callable[..., ContextManager[object]] | None = None,
        trace_root: str | None = None,
        stop_event: threading.Event | None = None,
        provider_health: ProviderHealthStore | None = None,
        environment: Mapping[str, str] | None = None,
        cache_identity: str = "",
    ) -> None:
        """装配 Provider 配置、传输层、Checkpoint 与观测回调。"""
        self.settings = settings
        # Cache preference never locks channels or becomes part of durable model messages.
        self._cache_enabled = (settings.prompt_cache and settings.api_format == "openai_responses"
                               and settings.model.startswith("gpt-6-astra/"))
        self._cache_identity = str(cache_identity) if self._cache_enabled else ""
        self._cache_affinity_id = ""
        self.trace_root = trace_root
        self._operator_metrics_path = (environment if environment is not None else os.environ).get("MODEL_OPERATOR_METRICS_PATH")
        self.transport = transport
        if any(channel.model for channel in channels):
            if (settings.api_format != "openai_responses"
                    or not settings.model.startswith("gpt-6-astra/")
                    or any(not channel.model.startswith("gpt-6-astra/") for channel in channels)):
                raise ValueError("routed channels require an explicit GPT-6 Responses pool")
        self.checkpoint = checkpoint or SegmentCheckpoint.initial()
        self.event_sink = event_sink
        self.usage = usage or UsageLedger()
        self.monotonic = monotonic
        self.sleeper = sleeper
        self.slot_factory = slot_factory or nullcontext
        self.provider_health = provider_health or ProviderHealthStore(
            load_runtime_config(environment)["resources"]["directory"]
        )
        self.pool = ChannelPool(
            channels,
            cooldown_s=settings.channel_cooldown_s,
            balance=balance,
            initial_segment_id=self._cache_identity or self.checkpoint.segment_id,
            monotonic=monotonic,
        )
        # A wall-clock Retry-After can outlive an injected monotonic test clock.
        # Remember the observed deadline so one fully honored cooldown is not
        # re-applied until another process extends it.
        self._shared_cooldown_deadlines: dict[str, tuple[float, float]] = {}
        self._health_by_channel: dict[str, ProviderHealth] = {}
        self.last_failure: ProviderFailure | None = None
        self.stop_event = stop_event

    @property
    def provider_affinity_id(self) -> str:
        """返回 signed segment 当前绑定的 Channel ID。"""
        return self.checkpoint.provider_affinity_id

    def call(
        self,
        request: ModelRequest,
        *,
        max_attempts: int | None = None,
        deadline_monotonic: float | None = None,
        request_budget_s: float | None = None,
        preferred_channel_id: str = "",
    ) -> SessionResult:
        """在预算内完成请求，并按失败类型 retry、failover 或 rollover。"""
        logical_request = request
        if self._cache_identity and not request.prompt_cache_key:
            identity = json.dumps([self._cache_identity, request.request_kind], ensure_ascii=True)
            logical_request = replace(request, prompt_cache_key=(
                "mural-" + hashlib.sha256(identity.encode()).hexdigest()[:56]))
        if any(channel.model for channel in self.pool.channels) and request.model != self.settings.model:
            raise ValueError("routed request does not match the configured logical model")
        reasoning_parameters = _reasoning_parameters(request, self.settings.api_format, self.settings.reasoning_transport)
        sent_effort = (reasoning_parameters.get("output_config", {}).get("effort")
            or reasoning_parameters.get("reasoning_effort")
            or reasoning_parameters.get("chat_template_kwargs", {}).get("reasoning_effort")
            or reasoning_parameters.get("reasoning", {}).get("effort") or "")
        sent_parameters = {}
        for name, value in reasoning_parameters.items():
            if isinstance(value, dict):
                sent_parameters.update({f"{name}.{key}": setting for key, setting in value.items()})
            else:
                sent_parameters[name] = value
        if request_budget_s is None and deadline_monotonic is None:
            request_budget_s = provider_request_budget_s(self.settings)
        if request_budget_s is not None:
            if request_budget_s <= 0 or deadline_monotonic is not None:
                raise ValueError("request budget must be positive and cannot accompany an absolute deadline")
            deadline_monotonic = self.monotonic() + request_budget_s
        actual_request_budget_s = max(0.0, deadline_monotonic - self.monotonic())
        attempts_limit = max_attempts or self.settings.max_attempts
        if attempts_limit <= 0:
            raise ValueError("max_attempts must be positive")
        original_messages = copy.deepcopy(list(request.messages))
        signed_history = contains_signed_thinking(request.messages)

        attempted_ids: set[str] = set()
        protocol_regenerations = 0
        masked_bad_request_retries = 0
        forced_channel_id = preferred_channel_id
        previous_attempt_id = ""
        previous_channel_id = ""
        request_failure: ProviderFailure | None = None
        provider_id = provider_identifier(self.settings.base_url)
        attempt = 0
        while attempt < attempts_limit:
            if self.stop_event is not None and self.stop_event.is_set():
                raise InterruptedError("owner_interrupted: no further provider attempts")
            remaining_s = (
                deadline_monotonic - self.monotonic()
                if deadline_monotonic is not None
                else None
            )
            if remaining_s is not None and remaining_s <= 0:
                failure = request_failure or ProviderFailure(
                    kind="deadline",
                    detail="session deadline was reached before the provider call",
                )
                self.last_failure = failure
                return SessionResult(None, "", attempt, failure)

            self._refresh_health_snapshot()
            ordered, segment_locked = self._request_order(signed_history)
            if not ordered:
                return self._unavailable_result(attempts=attempt)
            self._refresh_shared_cooldowns(ordered)

            channel = self.pool.select(
                ordered,
                attempted_ids,
                attempt + 1,
                forced_channel_id=forced_channel_id,
            )
            request = replace(logical_request, model=channel.model) if channel.model else logical_request
            route_metadata = {"requested_model": logical_request.model} if channel.model else {}
            forced_channel_id = ""
            attempt_id = f"attempt-{attempt + 1:03d}-{uuid.uuid4().hex}"
            cooldown = self.pool.cooldown_remaining(channel)
            if cooldown:
                wait_s = min(cooldown, max(0.0, remaining_s)) if remaining_s is not None else cooldown
                self._emit("resource_wait_started", attempt_id=attempt_id,
                           resource="provider_cooldown", model=request.model,
                           channel_id=channel.channel_id, wait_s=wait_s)
                wait_started = self.monotonic()
                cancelled = self.stop_event.wait(wait_s) if self.stop_event is not None else self.sleeper(wait_s)
                self._emit("resource_wait_completed", attempt_id=attempt_id,
                           resource="provider_cooldown", model=request.model,
                           channel_id=channel.channel_id, cancelled=bool(cancelled),
                           queue_wait_s=round(max(0.0, self.monotonic() - wait_started), 3))
                if self.stop_event is not None and self.stop_event.is_set():
                    raise InterruptedError("owner_interrupted: provider cooldown cancelled before request")
                if deadline_monotonic is not None:
                    remaining_s = deadline_monotonic - self.monotonic()
                    if remaining_s <= 0:
                        failure = request_failure or ProviderFailure(kind="deadline", detail="session deadline reached during provider cooldown")
                        self.last_failure = failure
                        return SessionResult(None, "", attempt, failure)
            attempted_ids.add(channel.channel_id)
            total_timeout_s = float(self.settings.total_timeout_s) or None
            if remaining_s is not None:
                total_timeout_s = min(total_timeout_s, remaining_s) if total_timeout_s is not None else remaining_s
            queued = self.monotonic()
            provider_started = False
            shared_quarantined = False
            shared_cooldown = False
            if self.slot_factory is not nullcontext:
                self._emit("resource_wait_started", attempt_id=attempt_id,
                           resource="model", model=request.model,
                           queue_timeout_s=self.settings.queue_timeout_s)
            try:
                preview = PublicResponseStream(
                    self._emit, attempt_id, monotonic=self.monotonic
                )
                with _public_delta_sink(
                    preview.add if self.settings.streaming else None, self.stop_event
                ):
                    slot_parameters = inspect.signature(self.slot_factory).parameters
                    slot = (self.slot_factory(deadline_monotonic=deadline_monotonic)
                            if "deadline_monotonic" in slot_parameters else self.slot_factory())
                    with slot:
                        if self.stop_event is not None and self.stop_event.is_set():
                            raise InterruptedError("owner_interrupted: resource slot released before request")
                        slot_acquired = self.monotonic()
                        queue_wait_s = max(0.0, slot_acquired - queued)
                        health = self.provider_health.status(self._health_identity(channel))
                        self._health_by_channel[channel.channel_id] = health
                        if health.quarantined:
                            shared_quarantined = True
                        elif self._apply_shared_cooldown(channel, health):
                            shared_cooldown = True
                        if shared_quarantined or shared_cooldown:
                            raise _SharedProviderHealthChanged()
                        started = self.monotonic()
                        if deadline_monotonic is not None:
                            remaining_s = deadline_monotonic - started
                            if remaining_s <= 0:
                                raise ResourceSlotTimeout("session deadline reached while waiting for a resource slot")
                            total_timeout_s = min(total_timeout_s, remaining_s) if total_timeout_s is not None else remaining_s
                        queue_wait_s = round(queue_wait_s, 3)
                        if self.slot_factory is not nullcontext:
                            self._emit("resource_wait_completed", attempt_id=attempt_id,
                                       resource="model", model=request.model, queue_wait_s=queue_wait_s)
                        phase_timeout = _request_timeout(self.settings, total_timeout_s,
                            streaming=self.settings.streaming)
                        self._emit(
                            "provider_attempt_started",
                            attempt_id=attempt_id,
                            provider_id=provider_id,
                            channel_id=channel.channel_id,
                            attempt=attempt + 1,
                            max_attempts=attempts_limit,
                            api_format=self.settings.api_format,
                            **request_kind_public(request.request_kind),
                            model=request.model,
                            **route_metadata,
                            actual_max_tokens=request.max_tokens,
                            effective_thinking=bool(request.thinking),
                            configured_effort=request.effort,
                            effective_effort=sent_effort,
                            sent_reasoning_parameters=sent_parameters,
                            server_confirmed_effort=None,
                            streaming=self.settings.streaming,
                            total_timeout_s=total_timeout_s,
                            request_budget_s=actual_request_budget_s,
                            deadline_monotonic=deadline_monotonic,
                            connect_timeout_s=phase_timeout.connect,
                            idle_timeout_s=(min(total_timeout_s, self.settings.idle_timeout_s)
                                            if total_timeout_s is not None else None),
                            read_timeout_s=phase_timeout.read,
                            channel_affinity_id=self.provider_affinity_id,
                            channel_affinity_enforced=segment_locked,
                            cache_key_configured=bool(request.prompt_cache_key),
                            cache_channel_affinity_id=self._cache_affinity_id,
                            cache_channel_affinity_enforced=False,
                            queue_wait_s=queue_wait_s,
                            failover_from_channel_id=(previous_channel_id
                                if previous_channel_id != channel.channel_id else ""),
                            previous_attempt_id=previous_attempt_id,
                        )
                        if previous_channel_id and previous_channel_id != channel.channel_id:
                            self.usage.record_failover()
                        previous_channel_id, previous_attempt_id = channel.channel_id, attempt_id
                        attempt += 1
                        self.usage.record_attempt(channel.channel_id, attempt_id=attempt_id)
                        provider_started = True
                        raw = RawAttempt(self.trace_root, attempt_id, emit=self._emit,
                            metadata={"provider_id": provider_id, "channel_id": channel.channel_id,
                                      "credential_ref": channel.channel_id, "model": request.model,
                                      **route_metadata,
                                      **request_kind_public(request.request_kind),
                                      "request_kind": request.request_kind, "actual_max_tokens": request.max_tokens,
                                      "api_format": self.settings.api_format, "streaming": self.settings.streaming,
                                      "endpoint": f"{provider_id}/{channel.channel_id}/{self.settings.api_format}"}) if self.trace_root else nullcontext()
                        with raw:
                            transport_response = self.transport.complete(
                                channel, request, total_timeout_s=total_timeout_s,
                            )
                if list(request.messages) != original_messages:
                    raise RuntimeError("provider transport mutated durable messages")
                preview.finish()
                response = transport_response.message
                refusal = _provider_refusal_failure(response)
                if refusal is not None:
                    # A policy refusal is a completed HTTP response, not an empty model turn.
                    # Do not pin signed affinity, fail over, or feed it into Agent empty retries.
                    self.usage.record_rejected_response(channel.channel_id, response, attempt_id=attempt_id)
                    self.last_failure = refusal
                    self._emit(
                        "provider_attempt_failed",
                        attempt_id=attempt_id,
                        provider_id=provider_id,
                        channel_id=channel.channel_id,
                        attempt=attempt,
                        **request_kind_public(request.request_kind),
                        model=request.model,
                        **route_metadata,
                        actual_max_tokens=request.max_tokens,
                        elapsed_s=round(max(0.0, self.monotonic() - started), 3),
                        error_kind=refusal.kind,
                        error_summary=refusal.detail,
                        retryable=False,
                        semantic_response_failure=True,
                        response_diagnostics=_response_diagnostics(response),
                    )
                    return SessionResult(
                        None,
                        channel.channel_id,
                        attempt,
                        refusal,
                        attempt_id=attempt_id,
                    )
                self.usage.record_success(channel.channel_id, response, attempt_id=attempt_id)
                if self._cache_enabled:
                    self._cache_affinity_id = channel.channel_id
                response_signed = _response_has_signed_thinking(response)
                if signed_history or response_signed:
                    self.checkpoint = SegmentCheckpoint(
                        segment_id=self.checkpoint.segment_id,
                        predecessor_segment_id=self.checkpoint.predecessor_segment_id,
                        messages=self.checkpoint.messages,
                        provider_affinity_id=channel.channel_id,
                        excluded_channel_ids=self.checkpoint.excluded_channel_ids,
                        reason=self.checkpoint.reason,
                        invalid_signature_recoveries=(
                            self.checkpoint.invalid_signature_recoveries
                        ),
                    )
                self._emit(
                    "provider_attempt_succeeded",
                    attempt_id=attempt_id,
                    provider_id=provider_id,
                    channel_id=channel.channel_id,
                    attempt=attempt,
                    **request_kind_public(request.request_kind),
                    model=request.model,
                    **route_metadata,
                    actual_max_tokens=request.max_tokens,
                    elapsed_s=round(max(0.0, self.monotonic() - started), 3),
                    first_byte_s=(round(max(0.0, transport_response.first_byte_s), 3)
                                  if transport_response.first_byte_s is not None else None),
                    usage={name: getattr(response.usage, name) for name in (
                        "input_tokens", "output_tokens", "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                    ) if getattr(getattr(response, "usage", None), name, None) is not None},
                    response_diagnostics=_response_diagnostics(response),
                )
                self.last_failure = None
                return SessionResult(response, channel.channel_id, attempt, attempt_id=attempt_id)
            except _SharedProviderHealthChanged:
                if self.slot_factory is not nullcontext:
                    self._emit("resource_wait_completed", attempt_id=attempt_id,
                               resource="model", model=request.model,
                               queue_wait_s=round(max(0.0, self.monotonic() - queued), 3),
                               cancelled=False, skipped=True,
                               skip_reason=("provider_auth_quarantined" if shared_quarantined
                                            else "provider_cooldown"))
                if shared_quarantined:
                    self._exclude_channel(channel)
                    ordered, segment_locked = self._request_order(signed_history)
                    if not ordered:
                        return self._unavailable_result(attempts=attempt)
                    forced_channel_id = ""
                continue
            except InterruptedError:
                if provider_started:
                    self.usage.record_failure(channel.channel_id, attempt_id=attempt_id)
                    self._emit("provider_attempt_failed", attempt_id=attempt_id, provider_id=provider_id,
                        channel_id=channel.channel_id, attempt=attempt, model=request.model,
                        **request_kind_public(request.request_kind),
                        actual_max_tokens=request.max_tokens, error_kind="owner_interrupted",
                        error_summary="Provider request cancelled for workspace resume", retryable=False,
                        elapsed_s=round(max(0.0, self.monotonic() - started), 3))
                elif self.slot_factory is not nullcontext:
                    self._emit("resource_wait_failed", attempt_id=attempt_id, resource="model",
                               model=request.model, error_kind="owner_interrupted")
                raise
            except ResourceSlotTimeout:
                if provider_started:
                    raise
                failure = ProviderFailure(
                    kind="resource_queue_timeout",
                    detail="local model resource queue reached its deadline; no provider request was sent",
                    timeout_phase="queue_wait",
                )
                self.last_failure = failure
                self._emit("resource_wait_failed", attempt_id=attempt_id, resource="model",
                           model=request.model, error_kind=failure.kind,
                           error_summary=failure.detail, timeout_phase=failure.timeout_phase,
                           queue_wait_s=round(max(0.0, self.monotonic() - queued), 3))
                return SessionResult(None, "", attempt, failure)
            except Exception as exc:
                if not provider_started:
                    raise
                failure = (ProviderFailure(
                    kind="provider_protocol_error",
                    detail=str(exc),
                    retryable=protocol_regenerations < 1 and attempt < attempts_limit,
                )
                           if isinstance(exc, ProviderProtocolError) else classify_provider_exception(exc))
                alternative = self._alternative(channel, attempted_ids)
                # This masked gateway rejection succeeded unchanged on another K3 channel.
                if (failure.kind == "http_error" and failure.status_code == 400
                        and masked_bad_request_retries == 0 and attempt < attempts_limit
                        and alternative is not None and not segment_locked
                        and _masked_k3_gateway_error(exc, request, self.settings)):
                    failure = replace(failure, kind="upstream_ambiguous_400",
                                      retryable=True, channel_specific=True)
                    masked_bad_request_retries += 1
                request_failure = failure
                self.last_failure = failure
                self.usage.record_failure(channel.channel_id, failure.timeout_phase, attempt_id=attempt_id)
                if self._cache_affinity_id == channel.channel_id:
                    self._cache_affinity_id = ""
                health = self.provider_health.record_failure(
                    self._health_identity(channel), failure,
                )
                self._health_by_channel[channel.channel_id] = health
                if failure.retryable and failure.kind != "provider_protocol_error":
                    self.pool.cool_down(channel, failure.retry_after_s)
                if health.quarantined:
                    # 明确拒绝的凭证/额度不随冷却或上下文压缩恢复；沿现有 checkpoint 保留。
                    self.checkpoint = replace(self.checkpoint, excluded_channel_ids=tuple(dict.fromkeys(
                        (*self.checkpoint.excluded_channel_ids, channel.channel_id)
                    )))
                    ordered = tuple(item for item in ordered if item.channel_id != channel.channel_id)
                alternative = self._alternative(channel, attempted_ids)
                decision = decide_recovery(
                    failure,
                    current_channel_id=channel.channel_id,
                    alternative_channel_id=(
                        alternative.channel_id if alternative is not None else ""
                    ),
                    segment_locked=segment_locked,
                    attempts_remaining=attempt < attempts_limit,
                )
                will_continue = decision.action in {
                    RecoveryAction.FAILOVER_CHANNEL,
                    RecoveryAction.RETRY_SAME_CHANNEL,
                }
                self._emit(
                    "provider_attempt_failed",
                    attempt_id=attempt_id,
                    provider_id=provider_id,
                    channel_id=channel.channel_id,
                    attempt=attempt,
                    **request_kind_public(request.request_kind),
                    model=request.model,
                    **route_metadata,
                    actual_max_tokens=request.max_tokens,
                    elapsed_s=round(max(0.0, self.monotonic() - started), 3),
                    error_kind=failure.kind,
                    excluded_channel_ids=list(self.checkpoint.excluded_channel_ids),
                    status_code=failure.status_code,
                    error_summary=_provider_error_summary(exc),
                    timeout_phase=failure.timeout_phase,
                    retry_after_s=failure.retry_after_s,
                    retryable=will_continue
                    or decision.action == RecoveryAction.ROLL_OVER_SEGMENT,
                    next_channel_id=decision.next_channel_id,
                    recovery_boundary=decision.recovery_boundary,
                    partial_response_discarded=decision.partial_response_discarded,
                    segment_rollover_required=(
                        decision.action == RecoveryAction.ROLL_OVER_SEGMENT
                    ),
                )
                if decision.action == RecoveryAction.ROLL_OVER_SEGMENT:
                    return SessionResult(
                        response=None,
                        channel_id=channel.channel_id,
                        attempts=attempt,
                        failure=failure,
                        segment_rollover_required=True,
                        next_channel_id=decision.next_channel_id,
                    )
                if decision.action == RecoveryAction.FAILOVER_CHANNEL:
                    forced_channel_id = decision.next_channel_id
                    continue
                if decision.action == RecoveryAction.RETRY_SAME_CHANNEL:
                    if failure.kind == "provider_protocol_error":
                        protocol_regenerations += 1
                    forced_channel_id = channel.channel_id
                    backoff = (
                        0
                        if failure.kind == "provider_protocol_error"
                        else min(2 ** attempt, 45)
                    )
                    if deadline_monotonic is not None:
                        backoff = min(backoff, max(0.0, deadline_monotonic - self.monotonic()))
                    if backoff and not self.pool.cooldown_remaining(channel):
                        if self.stop_event is not None:
                            self.stop_event.wait(backoff)
                        else:
                            self.sleeper(backoff)
                    continue
                return SessionResult(
                    None, channel.channel_id, attempt, failure
                )
        return SessionResult(None, "", attempt, self.last_failure)

    def _health_identity(self, channel: ProviderChannel) -> str:
        settings = replace(self.settings, model=channel.model) if channel.model else self.settings
        return self.provider_health.identity(settings, channel.key)

    def _shared_quarantined_ids(self) -> tuple[str, ...]:
        return tuple(channel.channel_id for channel in self.pool.channels
                     if self._health_by_channel.get(channel.channel_id, ProviderHealth()).quarantined)

    def _refresh_health_snapshot(self) -> None:
        identities = {channel.channel_id: self._health_identity(channel) for channel in self.pool.channels}
        snapshot = self.provider_health.snapshot(identities.values())
        self._health_by_channel = {
            channel_id: snapshot[identity] for channel_id, identity in identities.items()
        }

    def _request_order(self, signed_history: bool) -> tuple[tuple[ProviderChannel, ...], bool]:
        excluded = tuple(dict.fromkeys((*self.checkpoint.excluded_channel_ids,
                                        *self._shared_quarantined_ids())))
        return self.pool.request_order(affinity_id=self.provider_affinity_id,
                                       soft_affinity_id=self._cache_affinity_id,
                                       excluded_ids=excluded, signed_history=signed_history)

    def _refresh_shared_cooldowns(self, channels: Sequence[ProviderChannel]) -> None:
        for channel in channels:
            self._apply_shared_cooldown(
                channel, self._health_by_channel.get(channel.channel_id, ProviderHealth())
            )

    def _apply_shared_cooldown(self, channel: ProviderChannel, health: ProviderHealth) -> bool:
        """Mirror a shared Retry-After once per observed wall-clock deadline."""
        if health.cooldown_remaining_s <= 0:
            return False
        observed = self._shared_cooldown_deadlines.get(channel.channel_id)
        now = self.monotonic()
        if observed is not None and observed[0] >= health.cooldown_until:
            return observed[1] > now
        local_until = now + health.cooldown_remaining_s
        self._shared_cooldown_deadlines[channel.channel_id] = (
            health.cooldown_until, local_until,
        )
        self.pool.cool_down(channel, health.cooldown_remaining_s)
        return True

    def _exclude_channel(self, channel: ProviderChannel) -> None:
        self.checkpoint = replace(self.checkpoint, excluded_channel_ids=tuple(dict.fromkeys(
            (*self.checkpoint.excluded_channel_ids, channel.channel_id)
        )))

    def _alternative(self, current: ProviderChannel, attempted_ids: set[str]) -> ProviderChannel | None:
        excluded = (*self.checkpoint.excluded_channel_ids, *self._shared_quarantined_ids())
        return self.pool.alternative(current, excluded, attempted_ids=attempted_ids)

    def _unavailable_result(self, *, attempts: int) -> SessionResult:
        available = [channel for channel in self.pool.channels
                     if channel.channel_id not in self.checkpoint.excluded_channel_ids
                     and not self._health_by_channel.get(channel.channel_id, ProviderHealth()).quarantined]
        can_roll_over = bool(self.provider_affinity_id) and bool(available)
        failure = ProviderFailure(
            kind="channel_affinity_unavailable" if can_roll_over else "channel_pool_exhausted",
            detail=("the Channel pinned to this signed segment is unavailable; start a fresh unsigned segment"
                    if can_roll_over else "all configured channels are quarantined or excluded; restore provider access before resuming"),
            retryable=can_roll_over,
            channel_specific=True,
        )
        self.last_failure = failure
        return SessionResult(None, self.provider_affinity_id, attempts, failure,
                             segment_rollover_required=can_roll_over)

    def roll_over_after_failure(
        self,
        *,
        failed_channel_id: str,
        reason: str,
        invalid_signature: bool = False,
        new_segment_id: str | None = None,
    ) -> SegmentCheckpoint:
        """从最后闭合事务创建不含签名的新 segment。"""
        self.checkpoint = roll_over_segment(
            self.checkpoint,
            failed_channel_id=failed_channel_id,
            reason=reason,
            invalid_signature=invalid_signature,
            new_segment_id=new_segment_id,
        )
        return self.checkpoint

    def _emit(self, event_type: str, **fields: object) -> None:
        """向可选 sink 发送一份不可变更内部状态的事件副本。"""
        if self._operator_metrics_path:
            from .operator_metrics import append_operator_metrics
            append_operator_metrics(self._operator_metrics_path, event_type, fields,
                                    trace_root=self.trace_root)
        if self.event_sink is None:
            return
        event = {"event_type": event_type, **fields}
        self.event_sink(copy.deepcopy(event))


class ProviderProtocolError(ValueError):
    """原生工具协议无效；错误只含协议字段名，不回显参数或隐藏推理。"""


def _decode_tool_arguments(raw: object, protocol: str) -> dict[str, object]:
    if not isinstance(raw, str):
        raise ProviderProtocolError(f"{protocol} tool arguments must be JSON text")
    def reject_constant(_value):
        raise ValueError("non-JSON numeric constant")
    try:
        value = json.loads(raw, parse_constant=reject_constant)
    except ValueError:
        raise ProviderProtocolError(f"{protocol} tool arguments contain invalid JSON") from None
    if not isinstance(value, dict):
        raise ProviderProtocolError(f"{protocol} tool arguments must decode to an object")
    return value

# OpenAI 的 role:tool 消息没有原生 is_error 字段。用一个稳定的机读前缀把
# Anthropic 侧的 is_error/error_code 语义显式带过去，供下游解析与调试。
OPENAI_TOOL_ERROR_MARKER = "[tool_error]"


def _openai_error_prefix(error_code: object) -> str:
    """返回稳定的机读错误前缀，可选带上确定性 error_code。"""
    code = str(error_code or "").strip()
    if code:
        return f"{OPENAI_TOOL_ERROR_MARKER} error_code={code}\n"
    return f"{OPENAI_TOOL_ERROR_MARKER}\n"


def to_openai_messages(
    system: str | list[dict[str, object]],
    messages: Sequence[Mapping[str, Any]],
    *,
    preserve_structured: bool = False,
) -> list[dict[str, Any]]:
    """把共享消息分块转换为 OpenAI Chat 的角色消息。"""
    output: list[dict[str, Any]] = []
    open_tool_call_ids: set[str] = set()
    if isinstance(system, list):
        system = "\n".join(
            str(block.get("text") or "")
            for block in system
            if isinstance(block, Mapping) and block.get("type") == "text"
        )
    if system:
        output.append({"role": "system", "content": system})
    for raw_message in model_visible_messages(messages):
        role = str(raw_message.get("role") or "")
        content = raw_message.get("content")
        if preserve_structured and role == "assistant" and (isinstance(content, str) or content is None) and (
                "reasoning_content" in raw_message or "tool_calls" in raw_message):
            message = copy.deepcopy(dict(raw_message))
            output.append(message)
            open_tool_call_ids = {str(call["id"]) for call in (message.get("tool_calls") or [])
                                  if isinstance(call, Mapping) and call.get("id") is not None}
            continue
        if preserve_structured and role == "tool" and "tool_call_id" in raw_message:
            output.append(copy.deepcopy(dict(raw_message)))
            open_tool_call_ids.discard(str(raw_message["tool_call_id"]))
            continue
        if isinstance(content, str):
            output.append({"role": role, "content": content})
            open_tool_call_ids.clear()
            continue
        if not isinstance(content, list):
            continue
        if role == "assistant":
            text_parts: list[str] = []
            reasoning_parts: list[str] = []
            calls: list[dict[str, Any]] = []
            for block in content:
                if not isinstance(block, Mapping):
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    text_parts.append(str(block.get("text") or ""))
                elif block_type == "thinking" and block.get("thinking"):
                    reasoning_parts.append(str(block.get("thinking")))
                elif block_type == "tool_use":
                    calls.append({
                        "id": block.get("id"),
                        "type": "function",
                        "function": {
                            "name": block.get("name"),
                            "arguments": json.dumps(
                                block.get("input") or {}, ensure_ascii=False
                            ),
                        },
                    })
            message: dict[str, Any] = {
                "role": "assistant",
                "content": "\n".join(value for value in text_parts if value) or None,
            }
            if reasoning_parts:
                message["reasoning_content"] = "\n".join(reasoning_parts)
            if calls:
                message["tool_calls"] = calls
            output.append(message)
            open_tool_call_ids = {
                str(call.get("id")) for call in calls if call.get("id") is not None
            }
            continue

        extra: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, Mapping):
                continue
            block_type = block.get("type")
            if block_type == "tool_result":
                tool_use_id = str(block.get("tool_use_id") or "")
                converted = _openai_tool_result_content(block.get("content"))
                if block.get("is_error"):
                    # OpenAI 无原生 is_error；显式注入稳定错误标记 + error_code。
                    prefix = _openai_error_prefix(block.get("error_code"))
                    if isinstance(converted, str):
                        converted = prefix + converted
                    elif isinstance(converted, list):
                        converted = [
                            {"type": "text", "text": prefix.rstrip("\n")},
                            *converted,
                        ]
                    else:
                        converted = prefix.rstrip("\n")
                if tool_use_id and tool_use_id in open_tool_call_ids:
                    output.append({
                        "role": "tool",
                        "tool_call_id": tool_use_id,
                        "content": converted,
                    })
                    open_tool_call_ids.discard(tool_use_id)
                else:
                    extra.append({
                        "type": "text",
                        "text": (
                            "[Recovered result from a compacted tool turn"
                            + (f" {tool_use_id}" if tool_use_id else "")
                            + "]"
                        ),
                    })
                    if isinstance(converted, str) and converted:
                        extra.append({"type": "text", "text": converted})
                    elif isinstance(converted, list):
                        extra.extend(converted)
            elif block_type == "text":
                extra.append({"type": "text", "text": str(block.get("text") or "")})
            elif block_type == "image":
                extra.append(_image_to_openai(block))
        if extra:
            output.append({"role": "user", "content": extra})
        open_tool_call_ids.clear()
    return output


def openai_chat_body(
    request: ModelRequest,
    *,
    temperature: float = 0.3,
    reasoning_transport: str = "",
) -> dict[str, object]:
    """构造 Chat Completions 请求体，按具体模型传递开关与强度。"""
    body: dict[str, object] = {
        "model": request.model,
        "max_tokens": request.max_tokens,
        "messages": to_openai_messages(request.system, request.messages,
                                       preserve_structured=request.model.strip().lower() == "kimi-k3"
                                       or model_effort_capability(request.model)["transport"] == "glm"),
        "temperature": float(temperature),
    }
    if request.model.strip().lower() == "kimi-k3":
        body["max_completion_tokens"] = body.pop("max_tokens")
        body.pop("temperature")
    if request.tools:
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.get("name"),
                    "description": tool.get("description", ""),
                    "parameters": tool.get(
                        "input_schema", {"type": "object", "properties": {}}
                    ),
                },
            }
            for tool in request.tools
        ]
    body.update(_reasoning_parameters(request, "openai_chat_compatible", reasoning_transport))
    return body


def normalize_openai_chat_url(base_url: str) -> str:
    """补全 Chat Completions 路径，并拒绝未实现的 Responses API。"""
    base = str(base_url or "").strip().rstrip("/")
    if base.endswith("/responses"):
        raise ValueError(
            "OpenAI Responses API is not implemented; configure a Chat "
            "Completions base URL instead"
        )
    if base.endswith("/chat/completions"):
        return base
    if not base.endswith("/v1"):
        base += "/v1"
    return base + "/chat/completions"


def to_openai_responses_input(
    system: str | list[dict[str, object]],
    messages: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """把共享消息分块转换为 OpenAI Responses API 的 ``input`` 数组。

    Responses 用 ``input_text`` / ``output_text`` / ``input_image`` 分块和独立的
    ``function_call`` / ``function_call_output`` 项;system 走顶层 ``instructions``
    而非 input,故此处不含 system。
    """
    output: list[dict[str, Any]] = []
    for raw_message in model_visible_messages(messages):
        role = str(raw_message.get("role") or "")
        content = raw_message.get("content")
        if isinstance(content, str):
            part_type = "output_text" if role == "assistant" else "input_text"
            output.append(
                {"role": role, "content": [{"type": part_type, "text": content}]}
            )
            continue
        if not isinstance(content, list):
            continue
        if role == "assistant":
            text_parts: list[str] = []
            calls: list[dict[str, Any]] = []
            for block in content:
                if not isinstance(block, Mapping):
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    text_parts.append(str(block.get("text") or ""))
                elif block_type == "tool_use":
                    calls.append({
                        "type": "function_call",
                        "call_id": str(block.get("id") or ""),
                        "name": str(block.get("name") or ""),
                        "arguments": json.dumps(
                            block.get("input") or {}, ensure_ascii=False
                        ),
                    })
            joined = "\n".join(value for value in text_parts if value)
            if joined:
                output.append(
                    {
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": joined}],
                    }
                )
            output.extend(calls)
            continue

        extra: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, Mapping):
                continue
            block_type = block.get("type")
            if block_type == "tool_result":
                tool_use_id = str(block.get("tool_use_id") or "")
                converted = _openai_responses_tool_output(block.get("content"))
                if block.get("is_error"):
                    prefix = _openai_error_prefix(block.get("error_code"))
                    converted = prefix + converted if isinstance(converted, str) else [
                        {"type": "input_text", "text": prefix}, *converted]
                output.append({
                    "type": "function_call_output",
                    "call_id": tool_use_id,
                    "output": converted,
                })
            elif block_type == "text":
                extra.append(
                    {"type": "input_text", "text": str(block.get("text") or "")}
                )
            elif block_type == "image":
                extra.append(_image_to_openai_responses(block))
        if extra:
            output.append({"role": "user", "content": extra})
    return output


def _openai_responses_tool_output(value: object) -> str | list[dict]:
    """工具回执有图时保留 Responses 原生多模态 output，不能用占位文字冒充看图。"""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return "" if value is None else str(value)
    parts: list[dict] = []
    for item in value:
        if isinstance(item, Mapping) and item.get("type") == "text":
            parts.append({"type": "input_text", "text": str(item.get("text") or "")})
        elif isinstance(item, Mapping) and item.get("type") == "image":
            parts.append(_image_to_openai_responses(item))
        elif not isinstance(item, Mapping):
            parts.append({"type": "input_text", "text": str(item)})
    if any(part["type"] == "input_image" for part in parts):
        return parts
    return "\n".join(part["text"] for part in parts if part.get("text"))


def _image_to_openai_responses(block: Mapping[str, Any]) -> dict[str, object]:
    """把共享 base64 图片分块转换成 Responses ``input_image``。"""
    source = block.get("source")
    if isinstance(source, Mapping) and source.get("type") == "base64":
        media_type = str(source.get("media_type") or "image/png")
        data = str(source.get("data") or "")
        return {
            "type": "input_image",
            "image_url": f"data:{media_type};base64,{data}",
        }
    return {"type": "input_text", "text": "<image>"}


def openai_responses_body(request: ModelRequest) -> dict[str, object]:
    """构造官方 Responses API 请求体,含可公开的 reasoning summary。"""
    system = request.system
    if isinstance(system, list):
        system = "\n".join(
            str(block.get("text") or "")
            for block in system
            if isinstance(block, Mapping) and block.get("type") == "text"
        )
    body: dict[str, object] = {
        "model": request.model,
        "input": to_openai_responses_input(request.system, request.messages),
        "max_output_tokens": request.max_tokens,
    }
    if system:
        body["instructions"] = system
    if request.prompt_cache_key:
        body["prompt_cache_key"] = request.prompt_cache_key
    if request.tools:
        # Responses otherwise promotes optional fields to required, breaking exclusive targets.
        body["tools"] = [
            {
                "type": "function",
                "strict": False,
                "name": tool.get("name"),
                "description": tool.get("description", ""),
                "parameters": tool.get(
                    "input_schema", {"type": "object", "properties": {}}
                ),
            }
            for tool in request.tools
        ]
    body.update(_reasoning_parameters(request, "openai_responses"))
    return body


def normalize_openai_responses_url(base_url: str) -> str:
    """补全官方 Responses API 路径。"""
    base = str(base_url or "").strip().rstrip("/")
    if base.endswith("/responses"):
        return base
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    if not base.endswith("/v1"):
        base += "/v1"
    return base + "/responses"


async def _read_openai_responses_sse(
    response: AsyncIterable[bytes | str],
    *,
    started: float,
    total_timeout_s: float | None,
    monotonic: Callable[[], float],
    on_public_delta: Callable[[str, str, str], None] | None = None,
) -> tuple[dict[str, object], float]:
    """读取官方 Responses SSE 增量并还原成完整 Responses 响应对象。

    只累积**可公开**的 reasoning summary delta(``response.reasoning_summary_text
    .delta``)、``output_text`` delta、function_call 参数 delta;raw reasoning
    delta 不进公开摘要。``response.completed`` 的最终对象优先作为权威结果。
    """
    first_byte_s = 0.0
    response_started = False
    text_parts: list[str] = []
    summary_parts: list[str] = []
    tool_calls: dict[str, dict[str, Any]] = {}
    tool_order: list[str] = []
    status = "completed"
    usage: dict[str, object] = {}
    final_response: dict[str, object] | None = None
    try:
        async for raw_line in response:
            now = monotonic()
            if total_timeout_s is not None and now - started > total_timeout_s:
                raise ProviderRequestTimeout("total_request", now - started)
            line = (
                raw_line.decode("utf-8")
                if isinstance(raw_line, (bytes, bytearray))
                else str(raw_line)
            ).strip()
            if not line or line.startswith(":") or not line.startswith("data:"):
                continue
            if not response_started:
                response_started = True
                first_byte_s = max(0.0, now - started)
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            event = json.loads(payload)
            if not isinstance(event, dict):
                raise ValueError("OpenAI Responses stream event must be an object")
            event_type = str(event.get("type") or "")
            if event_type == "error":
                raise ValueError(
                    f"OpenAI Responses stream returned an error: {event.get('message') or event}"
                )
            if event_type == "response.output_text.delta":
                text_parts.append(str(event.get("delta") or ""))
                if on_public_delta is not None:
                    on_public_delta("message", str(event.get("output_index", 0)), str(event.get("delta") or ""))
            elif event_type == "response.reasoning_summary_text.delta":
                summary_parts.append(str(event.get("delta") or ""))
                if on_public_delta is not None:
                    on_public_delta("summary", str((event.get("output_index", 0), event.get("summary_index", 0))), str(event.get("delta") or ""))
            elif event_type == "response.output_item.added":
                item = event.get("item")
                if isinstance(item, Mapping) and item.get("type") == "function_call":
                    key = str(event.get("output_index") if event.get("output_index") is not None else item.get("id") or "")
                    entry = tool_calls.setdefault(
                        key,
                        {
                            "call_id": str(item.get("call_id") or ""),
                            "name": str(item.get("name") or ""),
                            "arguments": str(item.get("arguments") or ""),
                        },
                    )
                    if key not in tool_order:
                        tool_order.append(key)
                    if item.get("call_id"):
                        entry["call_id"] = str(item.get("call_id"))
                    if item.get("name"):
                        entry["name"] = str(item.get("name"))
            elif event_type == "response.function_call_arguments.delta":
                key = str(event.get("output_index") if event.get("output_index") is not None else event.get("item_id") or "")
                entry = tool_calls.setdefault(
                    key, {"call_id": "", "name": "", "arguments": ""}
                )
                if key not in tool_order:
                    tool_order.append(key)
                entry["arguments"] = str(entry["arguments"]) + str(
                    event.get("delta") or ""
                )
            elif event_type in {"response.completed", "response.incomplete"}:
                candidate = event.get("response")
                if isinstance(candidate, Mapping):
                    final_response = dict(candidate)
    except Exception as exc:
        phase = timeout_phase(exc, response_started=response_started)
        if phase and not isinstance(exc, ProviderRequestTimeout):
            raise ProviderRequestTimeout(
                phase, monotonic() - started, type(exc).__name__
            ) from exc
        raise
    if not response_started:
        raise ValueError("OpenAI Responses stream ended before the first data event")
    if final_response is not None:
        return final_response, first_byte_s
    # 没有终局 response 对象时,用累积的 delta 组一个等价结构。
    reconstructed_output: list[dict[str, Any]] = []
    summary_text = "".join(summary_parts).strip()
    if summary_text:
        reconstructed_output.append(
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": summary_text}],
            }
        )
    text = "".join(text_parts)
    if text:
        reconstructed_output.append(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        )
    for key in tool_order:
        entry = tool_calls[key]
        reconstructed_output.append(
            {
                "type": "function_call",
                "call_id": entry["call_id"],
                "name": entry["name"],
                "arguments": entry["arguments"],
            }
        )
    return (
        {"status": status, "output": reconstructed_output, "usage": usage},
        first_byte_s,
    )


def _normalize_openai_responses_response(
    decoded: object,
) -> NormalizedAssistantResponse:
    """把官方 Responses 响应归一为 Provider 中立结构。

    ``reasoning.summary[]`` → 公开 ``analysis_summaries``;``output_text`` → 可见
    文本分块;``function_call`` → tool_use。raw reasoning / encrypted content 不
    进 ``content`` 与摘要。
    """
    if not isinstance(decoded, dict):
        raise ValueError("OpenAI Responses response must be an object")
    output_items = decoded.get("output")
    if not isinstance(output_items, list):
        raise ValueError("OpenAI Responses response is missing an output array")
    blocks: list[dict[str, Any]] = []
    summaries: list[str] = []
    for item in output_items:
        if not isinstance(item, Mapping):
            continue
        item_type = str(item.get("type") or "")
        if item_type == "reasoning":
            for part in item.get("summary") or ():
                if isinstance(part, Mapping) and part.get("text"):
                    summaries.append(str(part.get("text") or ""))
        elif item_type == "message":
            for part in item.get("content") or ():
                if isinstance(part, Mapping) and part.get("type") == "output_text":
                    text = str(part.get("text") or "")
                    if text.strip():
                        blocks.append({"type": "text", "text": text})
        elif item_type == "function_call":
            arguments = _decode_tool_arguments(item.get("arguments"), "Responses")
            call_id = str(item.get("call_id") or "")
            if not call_id:
                raise ProviderProtocolError("Responses function_call is missing call_id")
            if not isinstance(item.get("name"), str) or not item["name"].strip():
                raise ProviderProtocolError("Responses function_call is missing name")
            blocks.append({
                "type": "tool_use",
                "id": call_id,
                "name": str(item.get("name") or ""),
                "input": arguments,
            })
    status = str(decoded.get("status") or "completed")
    if blocks and any(block.get("type") == "tool_use" for block in blocks):
        stop_reason = "tool_use"
    elif status == "incomplete":
        detail = decoded.get("incomplete_details")
        reason = (
            str(detail.get("reason") or "")
            if isinstance(detail, Mapping)
            else ""
        )
        stop_reason = "max_tokens" if reason == "max_output_tokens" else "end_turn"
    else:
        stop_reason = "end_turn"
    usage_value = decoded.get("usage")
    usage = usage_value if isinstance(usage_value, dict) else {}
    details_value = usage.get("input_tokens_details")
    details = details_value if isinstance(details_value, dict) else {}
    return NormalizedAssistantResponse(
        content=tuple(blocks),
        analysis_summaries=_dedup_summaries(summaries),
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_read_input_tokens=int(details.get("cached_tokens") or 0),
            cache_creation_input_tokens=int(details.get("cache_write_tokens") or 0),
        ),
    )


def _reasoning_parameters(request: ModelRequest, api_format: str, transport: str = "") -> dict:
    """实际请求和事件记录复用同一份无凭证参数；发送不等于服务端已确认生效。"""
    capability = model_effort_capability(request.model)
    if capability["transport"] == "glm":
        if api_format != "openai_chat_compatible":
            raise ValueError("GLM-5.3 integration currently requires Chat Completions")
        effort = validate_reasoning_effort(request.model, request.effort, thinking=request.thinking)
        thinking = {"type": "enabled"}
        if request.model.strip().lower() != "glm-5.3":
            thinking["clear_thinking"] = False
        return {"thinking": thinking, "reasoning_effort": effort or capability["default"]}
    if request.model.strip().lower() == "kimi-k3":
        effort = validate_reasoning_effort(request.model, request.effort, thinking=request.thinking)
        return {"reasoning_effort": effort or capability["default"]}
    # Opus 的 thinking/effort 组合限制属于原生 Messages 协议。
    effort = validate_reasoning_effort(request.model if api_format == "anthropic"
        or capability["transport"] == "chat_template_kwargs" else "",
        request.effort, thinking=request.thinking)
    if api_format == "anthropic":
        fields = {}
        if request.thinking:
            fields["thinking"] = {"type": "adaptive", "display": "summarized"}
        elif "deepseek" in request.model.lower() or re.match(r"^claude-(?:opus|sonnet|haiku)-", request.model.lower()):
            fields["thinking"] = {"type": "disabled"}
        if effort and (request.thinking or capability["without_thinking"]):
            fields["output_config"] = {"effort": effort}
        return fields
    if api_format == "openai_responses":
        if not request.thinking:
            return {}
        supported_efforts = set(capability["levels"] or EFFORT_LEVELS)
        if effort and effort not in supported_efforts:
            raise ValueError(f"unsupported Responses reasoning effort: {effort}")
        return {"reasoning": {"summary": "auto", **({"effort": effort} if effort else {})}}
    selected = _reasoning_transport(request.model, transport)
    if capability["transport"] == "chat_template_kwargs" and selected != "chat_template_kwargs":
        raise ValueError(f"{request.model} requires chat_template_kwargs plus reasoning_effort, not {selected!r}")
    if selected == "deepseek":
        return {"thinking": {"type": "enabled" if request.thinking else "disabled"}}
    if selected == "chat_template_kwargs":
        fields = {"chat_template_kwargs": {"enable_thinking": bool(request.thinking)}}
        if request.thinking and capability["transport"] == selected:
            template_effort = effort or capability["default"]
            fields["chat_template_kwargs"]["reasoning_effort"] = template_effort
            # Qwen 的 xhigh 由模板接收；LightLLM 的顶层枚举不接受该值。
            if template_effort in {"low", "medium"}:
                fields["reasoning_effort"] = template_effort
        return fields
    if selected == "reasoning_effort" and request.thinking:
        levels = capability["levels"] or ("low", "medium", "high")
        if effort and effort not in levels:
            raise ValueError(f"unsupported Chat Completions reasoning effort: {effort}")
        return {"reasoning_effort": effort} if effort else {}
    return {}


def _reasoning_transport(model: str, transport: str) -> str:
    """根据显式配置或模型名选择一种 reasoning 传输字段。"""
    if str(model or "").strip().lower() == "kimi-k3":
        return "reasoning_effort"
    if model_effort_capability(model)["transport"] == "glm":
        return "glm"
    selected = str(transport or "").strip().lower().replace("-", "_")
    selected = {
        "openai": "chat_template_kwargs",
        "qwen": "chat_template_kwargs",
        "vllm": "chat_template_kwargs",
        "openai_chat": "reasoning_effort",
    }.get(selected, selected)
    if selected in {"deepseek", "chat_template_kwargs", "reasoning_effort"}:
        return selected
    if selected in {"responses", "openai_responses"}:
        # 官方 Responses API 走 OpenAIResponsesTransport,不是 Chat 兼容端点的
        # reasoning 字段;在兼容 transport 里配这个值是误用。
        raise ValueError(
            "openai_responses reasoning transport belongs to the "
            "openai_responses API format, not chat-compatible reasoning fields"
        )
    lowered = str(model or "").strip().lower()
    if "deepseek-v4" in lowered:
        return "deepseek"
    if "qwen" in lowered or "qwq" in lowered:
        return "chat_template_kwargs"
    if lowered.startswith(("gpt-5", "o1", "o3", "o4")):
        return "reasoning_effort"
    return ""


def _image_to_openai(block: Mapping[str, Any]) -> dict[str, object]:
    """把共享 base64 图片分块转换成 OpenAI image_url。"""
    source = block.get("source")
    if isinstance(source, Mapping) and source.get("type") == "base64":
        media_type = str(source.get("media_type") or "image/png")
        data = str(source.get("data") or "")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{data}"},
        }
    return {"type": "text", "text": "<image>"}


def _openai_tool_result_content(value: object) -> str | list[dict[str, object]]:
    """把工具结果文本或图片转换为 OpenAI tool content。"""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return "" if value is None else str(value)
    parts: list[dict[str, object]] = []
    has_image = False
    for item in value:
        if not isinstance(item, Mapping):
            parts.append({"type": "text", "text": str(item)})
        elif item.get("type") == "text":
            parts.append({"type": "text", "text": str(item.get("text") or "")})
        elif item.get("type") == "image":
            parts.append(_image_to_openai(item))
            has_image = True
    if not has_image:
        return "\n".join(
            str(part.get("text") or "") for part in parts if part.get("text")
        )
    return parts


def _openai_response_blocks(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    """把 OpenAI Chat 兼容 assistant 消息还原为共享内容分块。

    自托管 Chat Completions 的 raw reasoning 被保留为内部 ``thinking`` 分块用于历史
    重建,但它**不是**可公开的分析摘要;归一层不会把它抽进 ``analysis_summaries``。
    """
    blocks: list[dict[str, Any]] = []
    reasoning = str(
        message.get("reasoning") or message.get("reasoning_content") or ""
    )
    text = str(message.get("content") or "")
    structured = message.get("tool_calls")
    if structured is not None and not isinstance(structured, list):
        raise ProviderProtocolError("Chat tool_calls must be an array")
    tool_calls = structured if isinstance(structured, list) else []
    if reasoning.strip():
        blocks.append(
            {"type": "thinking", "thinking": reasoning, "signature": None}
        )
    if text:
        blocks.append({"type": "text", "text": text})
    for tool_call in tool_calls:
        if not isinstance(tool_call, Mapping):
            raise ProviderProtocolError("Chat tool_calls entries must be objects")
        function = tool_call.get("function")
        if not isinstance(function, Mapping):
            raise ProviderProtocolError("Chat tool_call is missing its function object")
        if not isinstance(tool_call.get("id"), str) or not tool_call["id"].strip():
            raise ProviderProtocolError("Chat tool_call is missing id")
        if not isinstance(function.get("name"), str) or not function["name"].strip():
            raise ProviderProtocolError("Chat tool_call is missing function.name")
        arguments = _decode_tool_arguments(function.get("arguments"), "Chat")
        blocks.append(
            {
                "type": "tool_use",
                "id": tool_call.get("id"),
                "name": function.get("name"),
                "input": arguments,
            }
        )
    return blocks


def create_transport(
    settings: ProviderSettings,
    *,
    client_factory: Callable[[], Any] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> ProviderTransport:
    """按三种 API 格式创建适配器,三者共用同一个会话状态机。"""
    if settings.api_format == "anthropic":
        return AnthropicTransport(settings)
    if settings.api_format == "openai_responses":
        return OpenAIResponsesTransport(
            settings, client_factory=client_factory, monotonic=monotonic
        )
    return OpenAIChatCompatibleTransport(
        settings, client_factory=client_factory, monotonic=monotonic
    )


def provider_channels(
    environment: Mapping[str, str] | None = None,
) -> tuple[ProviderChannel, ...]:
    """从统一 ``MODEL_*`` 凭证池生成稳定且匿名的 Channel。"""
    env = environment if environment is not None else os.environ
    route_pool = str(env.get("MODEL_ROUTE_POOL_PATH") or "").strip()
    if route_pool:
        return _routed_provider_channels(Path(route_pool).expanduser(), env)
    source: Mapping[str, str] = env
    configured_pool = str(env.get("MODEL_POOL_ENV_PATH") or "").strip()
    if configured_pool:
        pool_path = Path(configured_pool).expanduser()
        values = _read_secret_environment(pool_path)
        if not values:
            raise ValueError(f"MODEL_POOL_ENV_PATH is empty or unreadable: {pool_path}")
        source = values
    raw_pool = str(source.get("MODEL_API_KEYS") or "")
    keys = [item.strip() for item in raw_pool.replace("\n", ",").split(",") if item.strip()]
    primary = str(source.get("MODEL_API_KEY") or "").strip()
    if primary:
        keys.insert(0, primary)
    raw_fallbacks = str(source.get("MODEL_API_KEY_FALLBACKS") or "")
    keys.extend(
        item.strip()
        for item in raw_fallbacks.replace("\n", ",").split(",")
        if item.strip()
    )
    unique_keys = tuple(dict.fromkeys(keys))
    if not unique_keys:
        raise KeyError("MODEL_API_KEY")
    labels = [
        item.strip()
        for item in str(source.get("MODEL_CHANNEL_LABELS") or "")
        .replace("\n", ",")
        .split(",")
    ]
    channels: list[ProviderChannel] = []
    for index, key in enumerate(unique_keys):
        raw_label = labels[index] if index < len(labels) else f"{index + 1}"
        label = "".join(
            char.lower() if char.isalnum() else "-" for char in raw_label
        ).strip("-") or f"{index + 1}"
        fingerprint = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
        channels.append(ProviderChannel(key, f"ch-{label}-{fingerprint}"))
    return tuple(channels)


def _routed_provider_channels(path: Path, environment: Mapping[str, str]) -> tuple[ProviderChannel, ...]:
    """Load opt-in GPT-6 routes without changing the legacy credential pool."""
    if environment.get("MODEL_POOL_ENV_PATH"):
        raise ValueError("MODEL_ROUTE_POOL_PATH cannot accompany MODEL_POOL_ENV_PATH")
    try:
        pool = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("GPT-6 route pool is unreadable or invalid JSON") from None
    if not isinstance(pool, dict) or pool.get("schema") != "mural.gpt6-route-pool.v1":
        raise ValueError("unsupported GPT-6 route pool schema")
    settings = ProviderSettings.from_environment(environment)
    if (settings.api_format != "openai_responses" or pool.get("api_format") != settings.api_format
            or not settings.model.startswith("gpt-6-astra/") or pool.get("model") != settings.model
            or service_url(pool.get("base_url")) != service_url(settings.base_url)):
        raise ValueError("GPT-6 route pool does not match the selected model, endpoint or API format")
    records = pool.get("channels")
    if not isinstance(records, list) or not records:
        raise ValueError("GPT-6 route pool must contain channels")
    channels: list[ProviderChannel] = []
    labels: set[str] = set()
    identities: set[tuple[str, str]] = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("invalid GPT-6 channel record")
        label, model = record.get("id"), record.get("model")
        key_file, key_env = record.get("key_env_file"), record.get("key_env")
        if (not isinstance(label, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", label)
                or label in labels or not isinstance(model, str) or not model.startswith("gpt-6-astra/")
                or any(char.isspace() for char in model)
                or not isinstance(key_file, str) or not key_file.strip()
                or not isinstance(key_env, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key_env)):
            raise ValueError("invalid or duplicate GPT-6 channel configuration")
        source = Path(key_file).expanduser()
        if not source.is_absolute():
            source = path.parent / source
        key = _read_secret_environment(source).get(key_env, "").strip()
        if not key or (model, key) in identities:
            raise ValueError("GPT-6 channel credential is missing or duplicated")
        labels.add(label)
        identities.add((model, key))
        fingerprint = hashlib.sha256((model + "\0" + key).encode()).hexdigest()[:8]
        channels.append(ProviderChannel(key, f"ch-{label}-{fingerprint}", model=model))
    return tuple(channels)


def provider_identifier(base_url: str) -> str:
    """把 Provider 地址散列成不泄露完整地址的观测 ID。"""
    normalized = str(base_url or "").strip().rstrip("/").lower()
    return "provider-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:10]


def replay_provider_events(
    source: str | Path | Iterable[Mapping[str, object]],
    *,
    allow_open: bool = False,
) -> ProviderReplaySummary:
    """配对真实 Trace 中的 attempt 事件并汇总失败与 failover。"""
    if isinstance(source, (str, Path)):
        events: list[dict[str, object]] = []
        with Path(source).open("r", encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle, start=1):
                if not raw.strip():
                    continue
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError(f"event line {line_number} is not an object")
                events.append(value)
    else:
        events = [dict(event) for event in source]

    open_attempts: dict[str, dict[str, object]] = {}
    terminal_ids: set[str] = set()
    successes = 0
    failures = 0
    failovers = 0
    timeout_phases: dict[str, int] = {}
    channel_attempts: dict[str, int] = {}
    pending_switches: dict[tuple[str, str], str] = {}
    for event in events:
        event_type = str(event.get("event_type") or event.get("event") or "")
        if event_type not in {
            "provider_attempt_started",
            "provider_attempt_succeeded",
            "provider_attempt_failed",
        }:
            continue
        attempt_id = str(event.get("attempt_id") or "")
        session_key = (str(event.get("agent_id") or ""), str(event.get("segment_id") or ""))
        if not attempt_id:
            raise ValueError(f"{event_type} is missing attempt_id")
        if event_type == "provider_attempt_started":
            if attempt_id in open_attempts or attempt_id in terminal_ids:
                raise ValueError(f"duplicate provider attempt {attempt_id!r}")
            open_attempts[attempt_id] = event
            channel_id = str(event.get("channel_id") or "")
            channel_attempts[channel_id] = channel_attempts.get(channel_id, 0) + 1
            previous_channel = pending_switches.pop(session_key, "")
            if previous_channel and previous_channel != channel_id:
                failovers += 1
            continue
        started = open_attempts.pop(attempt_id, None)
        if started is None:
            raise ValueError(f"terminal provider event has no start: {attempt_id!r}")
        terminal_ids.add(attempt_id)
        if event_type == "provider_attempt_succeeded":
            successes += 1
        else:
            failures += 1
            phase = str(event.get("timeout_phase") or "")
            if phase:
                timeout_phases[phase] = timeout_phases.get(phase, 0) + 1
            if event.get("next_channel_id") and not event.get("segment_rollover_required"):
                pending_switches[session_key] = str(event.get("channel_id") or "")
    if open_attempts and not allow_open:
        raise ValueError(
            f"unclosed provider attempts: {sorted(open_attempts)}"
        )
    return ProviderReplaySummary(
        attempts=successes + failures + len(open_attempts),
        successes=successes,
        failures=failures,
        failovers=failovers,
        timeout_phases=dict(sorted(timeout_phases.items())),
        channel_attempts=dict(sorted(channel_attempts.items())),
        open_attempt_ids=tuple(sorted(open_attempts)),
    )


def _request_timeout(
    settings: ProviderSettings, total_s: float | None, *, streaming: bool
) -> httpx.Timeout:
    """生成各 IO 阶段的超时；Anthropic 总 deadline 由外层异步取消实现。

    ``read`` 语义按模式区分:流式下 = chunk 间最大间隔(idle);非流式下整个生成期间不发
    任何字节、直到最终响应一次性返回,故 read 必须给到整个 total(否则长生成会 ReadTimeout
    误断)。
    """
    if total_s is None or total_s == 0:
        return httpx.Timeout(None, connect=settings.connect_timeout_s,
                             write=settings.connect_timeout_s, pool=settings.connect_timeout_s)
    total = float(total_s)
    connect = min(total, max(1.0, float(settings.connect_timeout_s)))
    idle = min(total, max(1.0, float(settings.idle_timeout_s)))
    read = idle if streaming else total
    return httpx.Timeout(total, connect=connect, read=read, write=read, pool=connect)


def _messages_with_cache_breakpoint(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """给最后一条消息加缓存断点，同时保持原消息不变。"""
    if not messages:
        return messages
    output = copy.deepcopy(messages)
    last = output[-1]
    content = last.get("content")
    if isinstance(content, str):
        last["content"] = [{
            "type": "text",
            "text": content,
            "cache_control": {"type": "ephemeral"},
        }]
    elif isinstance(content, list):
        for block in reversed(content):
            if isinstance(block, dict):
                block["cache_control"] = {"type": "ephemeral"}
                break
    return output


def _response_has_signed_thinking(response: Any) -> bool:
    """判断响应是否包含绑定当前 Provider Channel 的思考签名。"""
    content = getattr(response, "content", None)
    if not isinstance(content, (list, tuple)):
        return False
    for block in content:
        block_type = (
            block.get("type") if isinstance(block, Mapping) else getattr(block, "type", "")
        )
        signature = (
            block.get("signature")
            if isinstance(block, Mapping)
            else getattr(block, "signature", "")
        )
        if block_type == "redacted_thinking":
            return True
        if block_type == "thinking" and signature:
            return True
    return False


def _read_secret_environment(path: Path) -> dict[str, str]:
    """读取凭证池文件；内容只返回给调用方，不写入全局环境。"""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            values[name.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        return {}
    return values
