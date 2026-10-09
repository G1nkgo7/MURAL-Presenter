"""工具 Schema、权限与原子 tool-use/result 事务。

本模块刻意不理解页面审美或 Plan Markdown。它只接受显式工具动作、检查机械权限、
执行已注册 handler，并始终用恰好一个 result 闭合事务。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import copy
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath
import threading
import time
from typing import Any, Callable, Iterable, Mapping

from .diagnostics import command_scope


TOOL_EVENT_SCHEMA = "mural.tool-event.v1"
ToolHandler = Callable[["ToolContext", Mapping[str, Any]], object]
ToolEventSink = Callable[[Mapping[str, object]], None]


# 只有 inspect 对外提供；历史名称仅用于如实读取旧 checkpoint，不注册执行别名。
NATIVE_INSPECTION_TOOLS = frozenset({"vision_analyze", "inspect", "inspect_group"})


def _is_native_inspection_tool(name: str) -> bool:
    """判断工具是否会把真实像素以 Native inline image 放回同一会话。"""
    return name in NATIVE_INSPECTION_TOOLS


class ToolContractError(ValueError):
    """模型提交了机械上无效的工具调用。"""

    def __init__(self, code: str, detail: str) -> None:
        """保存稳定错误码，并把详细信息交给标准异常。"""
        self.code = str(code)
        super().__init__(detail)


class ToolExecutionError(RuntimeError):
    """已知 handler 失败；稳定错误码决定交回模型修正还是暂停后续跑。"""

    def __init__(self, code: str, detail: str, *, diagnostics: Mapping[str, object] | None = None) -> None:
        """保存执行错误的稳定错误码。"""
        self.code = str(code)
        self.diagnostics = diagnostics
        super().__init__(detail)


@dataclass(frozen=True)
class WriteScope:
    """声明某个 Owner 可以写入的工作区路径范围。"""

    path: str
    recursive: bool = True

    def __post_init__(self) -> None:
        """在保存前规范允许写入的路径。"""
        object.__setattr__(self, "path", normalize_workspace_path(self.path))

    def permits(self, candidate: str) -> bool:
        """判断候选路径是否落在当前精确或递归范围内。"""
        normalized = normalize_workspace_path(candidate)
        if not self.recursive:
            return normalized == self.path
        root = PurePosixPath(self.path)
        target = PurePosixPath(normalized)
        return target == root or root in target.parents


@dataclass(frozen=True)
class ToolContext:
    """携带一次工具调用的 Owner 身份、角色和写权限。"""

    owner_id: str
    role: str
    read_scopes: tuple[WriteScope, ...] = ()
    write_scopes: tuple[WriteScope, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)
    write_authorizer: Callable[[str], bool] | None = field(default=None, repr=False, compare=False)

    def may_read(self, path: str) -> bool:
        """判断当前 Owner 是否拥有目标路径的读取权限。"""
        return any(scope.permits(path) for scope in self.read_scopes)

    def may_write(self, path: str) -> bool:
        """判断当前 Owner 是否拥有目标路径的写权限。"""
        if self.write_authorizer is not None:
            return self.write_authorizer(normalize_workspace_path(path))
        return any(scope.permits(path) for scope in self.write_scopes)


@dataclass(frozen=True)
class ToolDefinition:
    """绑定一个工具的说明、输入 Schema、权限和执行函数。"""

    name: str
    description: str
    input_schema: Mapping[str, object]
    handler: ToolHandler
    roles: frozenset[str] = frozenset()
    mutates_workspace: bool = False
    path_argument: str = ""
    model_visible: bool = True

    def advertised_schema(self) -> dict[str, object]:
        """返回可发给模型的名称、说明与输入 Schema 副本。"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": copy.deepcopy(dict(self.input_schema)),
        }


@dataclass(frozen=True)
class ToolCall:
    """表示模型提交的一次结构化工具调用。"""

    tool_use_id: str
    name: str
    arguments: Mapping[str, object]


@dataclass(frozen=True)
class ToolResult:
    """保存工具调用的闭合结果和稳定错误码。"""

    tool_use_id: str
    name: str
    ok: bool
    content: str
    error_code: str = ""

    def as_message_block(self) -> dict[str, object]:
        """把执行结果转换成模型消息中的 ``tool_result`` 分块。"""
        block: dict[str, object] = {
            "type": "tool_result",
            "tool_use_id": self.tool_use_id,
            "content": self.content,
        }
        if not self.ok:
            block["is_error"] = True
        return block


@dataclass(frozen=True)
class ToolReplaySummary:
    """汇总离线重放后工具事务的闭合情况。"""

    uses: int
    results: int
    closed_ids: tuple[str, ...]
    open_ids: tuple[str, ...]


class ToolRuntime:
    """执行注册工具，并保留可审计的事务日志。"""

    def __init__(
        self,
        *,
        event_sink: ToolEventSink | None = None,
    ) -> None:
        """初始化工具表与事务日志。"""
        self._definitions: dict[str, ToolDefinition] = {}
        self._results: dict[str, ToolResult] = {}
        self._calls: dict[str, tuple[str, str, ToolCall]] = {}
        self._events: list[dict[str, object]] = []
        self._event_sinks: list[ToolEventSink] = (
            [event_sink] if event_sink is not None else []
        )
        self._lock = threading.RLock()
        # 宿主可在装配控制工具时挂一个延迟 inspection 提交器；AgentRuntime 在图片被
        # 下一次主模型请求成功消费后调用它推进 INSPECTED。默认无（纯工具重放场景）。
        self.deferred_inspection_committer: (
            Callable[[Mapping[str, object]], bool | Mapping[str, object] | None] | None
        ) = None
        self.inspection_feedback: Callable[[ToolContext, ToolCall, Mapping[str, object]], Mapping[str, object]] | None = None

    @property
    def events(self) -> tuple[dict[str, object], ...]:
        """返回不可由调用方修改的工具事件快照。"""
        with self._lock:
            return tuple(copy.deepcopy(self._events))

    def register(self, definition: ToolDefinition) -> None:
        """注册一个名称唯一且写入声明完整的工具。"""
        name = definition.name.strip()
        if not name:
            raise ValueError("tool name must not be empty")
        with self._lock:
            if name in self._definitions:
                raise ValueError(f"tool {name!r} is already registered")
            if definition.mutates_workspace and not definition.path_argument:
                raise ValueError(
                    f"mutating tool {name!r} must declare its path argument"
                )
            self._definitions[name] = definition

    def add_event_sink(self, sink: ToolEventSink) -> None:
        """登记一个在工具动作发生点同步调用的持久化观察器。"""
        if not callable(sink):
            raise TypeError("tool event sink must be callable")
        with self._lock:
            if sink not in self._event_sinks:
                self._event_sinks.append(sink)

    def advertised_tools(self, role: str) -> list[dict[str, object]]:
        """列出指定角色有权调用的工具 Schema。"""
        normalized_role = role.strip().lower()
        with self._lock:
            return [
                definition.advertised_schema()
                for definition in self._definitions.values()
                if definition.model_visible
                and (not definition.roles or normalized_role in definition.roles)
            ]

    def execute(self, context: ToolContext, call: ToolCall) -> ToolResult:
        """校验并执行一次调用，确保所有路径都闭合 result。"""
        tool_use_id = call.tool_use_id.strip()
        name = call.name.strip()
        if not tool_use_id:
            raise ValueError("tool_use_id must not be empty")
        role = context.role.strip().lower()
        started = time.monotonic()
        with self._lock:
            original = self._calls.get(tool_use_id)
            if original is not None:
                owner_id, original_role, original_call = original
                if (owner_id != context.owner_id or original_role != role
                        or original_call.name != name
                        or dict(original_call.arguments) != dict(call.arguments)):
                    return ToolResult(
                        tool_use_id=tool_use_id,
                        name=name,
                        ok=False,
                        content="tool_use_id was already used for a different owner, role or call",
                        error_code="tool_use_id_conflict",
                    )
                previous = self._results.get(tool_use_id)
                if previous is not None:
                    return previous
                return ToolResult(
                    tool_use_id=tool_use_id,
                    name=name,
                    ok=False,
                    content="tool_use_id is still executing",
                    error_code="tool_use_inflight",
                )
            stored_call = ToolCall(
                tool_use_id, name, copy.deepcopy(dict(call.arguments))
            )
            self._calls[tool_use_id] = (context.owner_id, role, stored_call)
            self._append_event(
                "tool_use",
                context,
                stored_call,
                arguments=copy.deepcopy(dict(call.arguments)),
            )
            definition = self._definitions.get(name)

        try:
            if definition is None:
                raise ToolContractError("unknown_tool", f"unknown tool {name!r}")
            if definition.roles and role not in definition.roles:
                raise ToolContractError(
                    "tool_permission_denied",
                    f"role {role!r} may not call {name!r}",
                )
            arguments = copy.deepcopy(dict(call.arguments))
            try:
                validate_json_value(arguments, definition.input_schema)
            except ToolContractError as exc:
                if name != "delegate_submit":
                    raise
                raise ToolContractError(
                    exc.code,
                    f"{exc}。委派参数未通过校验，本批任务未接受或启动。"
                    "请修正上述字段后重提；tasks 直接传 JSON 数组，不要编码成字符串。"
                    f"格式示例：{_DELEGATE_SUBMIT_EXAMPLE}",
                ) from exc
            if definition.mutates_workspace:
                raw_path = arguments.get(definition.path_argument)
                if not isinstance(raw_path, str):
                    raise ToolContractError(
                        "invalid_write_path",
                        f"{name!r} requires string path argument "
                        f"{definition.path_argument!r}",
                    )
                normalized_path = normalize_workspace_path(raw_path)
                if not context.may_write(normalized_path):
                    detail = f"owner {context.owner_id!r} may not write {normalized_path!r}. "
                    if role == "orchestrator" and PurePosixPath(normalized_path).parts[:1] == ("slides",):
                        detail += (
                            "A rejected write does not transfer ownership. Delegated page changes, including "
                            "page numbers and shared HTML elements, go back to the original Slide Owner. "
                            "If its task is still running, answer its requests or wait; after it finishes, "
                            "submit repair with its original group_id and complete page scope, not a new group."
                        )
                    else:
                        detail += "Use only authorized write paths; necessary out-of-scope changes go to the authorized Owner."
                    raise ToolContractError(
                        "write_scope_denied",
                        detail,
                    )
                arguments[definition.path_argument] = normalized_path
            def emit_output(payload):
                # 输出引用只进入实时流；最终 tool-events 仍只保存 use/result 事务。
                event = {"event_type": "tool_output", "tool_use_id": tool_use_id,
                         "tool_name": name, "owner_id": context.owner_id, "role": context.role,
                         **{key: context.metadata.get(key, "") for key in (
                             "turn", "segment_id", "task_id", "attempt", "wave_id",
                             "group_id", "model", "request_kind", "request_kind_label",
                             "request_kind_note", "actual_max_tokens")},
                         "pages": list(context.metadata.get("pages") or []),
                         **payload}
                with self._lock:
                    for sink in self._event_sinks:
                        sink(copy.deepcopy(event))

            with command_scope(tool_use_id, {**context.metadata, "owner_id": context.owner_id, "role": context.role}, emit_output):
                output = definition.handler(context, arguments)
                if name == "inspect" and isinstance(output, Mapping) and self.inspection_feedback is not None:
                    output = self.inspection_feedback(context, stored_call, output)
            result = ToolResult(
                tool_use_id=tool_use_id,
                name=name,
                ok=True,
                content=_result_content(output),
            )
        except (ToolContractError, ToolExecutionError) as exc:
            result = ToolResult(
                tool_use_id=tool_use_id,
                name=name,
                ok=False,
                content=_result_content(exc.diagnostics) if isinstance(exc, ToolExecutionError) and exc.diagnostics is not None else str(exc),
                error_code=exc.code,
            )
        # 每个已接受的 tool_use 都必须得到一个 result，handler 异常也不能打破事务。
        except Exception as exc:
            result = ToolResult(
                tool_use_id=tool_use_id,
                name=name,
                ok=False,
                content=f"{type(exc).__name__}: {exc}",
                error_code="handler_error",
            )

        with self._lock:
            self._results[tool_use_id] = result
            self._append_event(
                "tool_result",
                context,
                stored_call,
                ok=result.ok,
                content=result.content,
                error_code=result.error_code,
                duration_ms=round(max(0.0, time.monotonic() - started) * 1000, 3),
            )
        return result

    def assert_closed(self) -> None:
        """确认当前日志中不存在未闭合工具事务。"""
        with self._lock:
            summary = replay_tool_transactions(self._events)
        if summary.open_ids:
            raise RuntimeError(f"open tool transactions: {list(summary.open_ids)}")

    def _append_event(
        self,
        event_type: str,
        context: ToolContext,
        call: ToolCall,
        **fields: object,
    ) -> None:
        """按递增序号追加一个不含隐式状态的工具事件。"""
        metadata = context.metadata if isinstance(context.metadata, Mapping) else {}
        event = {
            "schema": TOOL_EVENT_SCHEMA,
            "sequence": len(self._events) + 1,
            "timestamp": datetime.now(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
            "event_type": event_type,
            "tool_use_id": call.tool_use_id,
            "tool_name": call.name,
            "owner_id": context.owner_id,
            "role": context.role,
            "turn": int(metadata.get("turn") or 0),
            "segment_id": str(metadata.get("segment_id") or ""),
            "task_id": str(metadata.get("task_id") or ""),
            "attempt": int(metadata.get("attempt") or 0),
            "wave_id": str(metadata.get("wave_id") or ""),
            "group_id": str(metadata.get("group_id") or ""),
            "pages": list(metadata.get("pages") or []),
            "model": str(metadata.get("model") or ""),
            "request_kind": str(metadata.get("request_kind") or ""),
            "request_kind_label": str(metadata.get("request_kind_label") or ""),
            "request_kind_note": str(metadata.get("request_kind_note") or ""),
            "actual_max_tokens": int(metadata.get("actual_max_tokens") or 0),
            **fields,
        }
        self._events.append(event)
        for sink in tuple(self._event_sinks):
            sink(copy.deepcopy(event))


def normalize_workspace_path(value: str) -> str:
    """把工具路径规范为不可逃逸工作区的 POSIX 相对路径。"""
    raw = str(value or "").strip().replace("\\", "/")
    if not raw or "\x00" in raw:
        raise ToolContractError("invalid_path", "workspace path is empty or invalid")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ToolContractError(
            "invalid_path", f"workspace path must be canonical and relative: {raw!r}"
        )
    return path.as_posix()


def validate_json_value(
    value: object, schema: Mapping[str, object], path: str = "$"
) -> None:
    """校验稳定工具 Schema 使用到的 JSON 类型与数值边界。"""
    alternatives = schema.get("oneOf")
    if isinstance(alternatives, list):
        matches = 0
        for alternative in alternatives:
            try:
                validate_json_value(value, alternative, path)
            except ToolContractError:
                continue
            matches += 1
        if matches != 1:
            raise ToolContractError("invalid_arguments", f"{path} must match exactly one permitted form")
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            raise ToolContractError(
                "invalid_arguments", f"{path} must be an object; received {_json_type_name(value)}"
            )
        properties = schema.get("properties")
        property_map = properties if isinstance(properties, Mapping) else {}
        required = schema.get("required")
        required_names = required if isinstance(required, list) else []
        missing = [name for name in required_names if name not in value]
        if missing:
            raise ToolContractError(
                "missing_arguments", f"{path} is missing required fields {missing}"
            )
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(property_map))
            if unknown:
                raise ToolContractError(
                    "unknown_arguments", f"{path} contains unknown fields {unknown}"
                )
        for name, item in value.items():
            child_schema = property_map.get(name)
            if isinstance(child_schema, Mapping):
                validate_json_value(item, child_schema, f"{path}.{name}")
    elif expected == "array":
        if not isinstance(value, list):
            raise ToolContractError(
                "invalid_arguments", f"{path} must be an array; received {_json_type_name(value)}"
            )
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(value) < min_items:
            raise ToolContractError(
                "invalid_arguments", f"{path} must contain at least {min_items} items"
            )
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                validate_json_value(item, item_schema, f"{path}[{index}]")
    elif expected == "string" and not isinstance(value, str):
        raise ToolContractError("invalid_arguments", f"{path} must be a string")
    elif expected == "integer" and (
        not isinstance(value, int) or isinstance(value, bool)
    ):
        raise ToolContractError("invalid_arguments", f"{path} must be an integer")
    elif expected == "number" and (
        not isinstance(value, (int, float)) or isinstance(value, bool)
    ):
        raise ToolContractError("invalid_arguments", f"{path} must be a number")
    elif expected == "boolean" and not isinstance(value, bool):
        raise ToolContractError("invalid_arguments", f"{path} must be a boolean")

    choices = schema.get("enum")
    if isinstance(choices, list) and value not in choices:
        raise ToolContractError(
            "invalid_arguments", f"{path} must be one of {choices}, got {value!r}"
        )
    minimum = schema.get("minimum")
    if isinstance(minimum, (int, float)) and isinstance(value, (int, float)):
        if value < minimum:
            raise ToolContractError(
                "invalid_arguments", f"{path} must be at least {minimum}"
            )
    maximum = schema.get("maximum")
    if isinstance(maximum, (int, float)) and isinstance(value, (int, float)):
        if value > maximum:
            raise ToolContractError(
                "invalid_arguments", f"{path} must be at most {maximum}"
            )
    min_length = schema.get("minLength")
    if isinstance(min_length, int) and isinstance(value, str):
        if len(value) < min_length:
            raise ToolContractError(
                "invalid_arguments", f"{path} must contain at least {min_length} characters"
            )


def _json_type_name(value: object) -> str:
    return {
        type(None): "null", dict: "object", list: "array", str: "string",
        bool: "boolean", int: "integer", float: "number",
    }.get(type(value), type(value).__name__)


def replay_tool_transactions(events: Iterable[Mapping[str, object]]) -> ToolReplaySummary:
    """重放工具事件并确认每个 use 都有且仅有一个 result。"""
    open_ids: set[str] = set()
    closed_ids: list[str] = []
    uses = 0
    results = 0
    for event in events:
        event_type = str(event.get("event_type") or event.get("event") or "")
        tool_use_id = str(event.get("tool_use_id") or event.get("id") or "")
        if event_type == "tool_use":
            uses += 1
            if not tool_use_id or tool_use_id in open_ids or tool_use_id in closed_ids:
                raise ValueError(f"invalid or duplicate tool_use_id {tool_use_id!r}")
            open_ids.add(tool_use_id)
        elif event_type == "tool_result":
            results += 1
            if tool_use_id not in open_ids:
                raise ValueError(f"tool_result has no open tool_use {tool_use_id!r}")
            open_ids.remove(tool_use_id)
            closed_ids.append(tool_use_id)
    return ToolReplaySummary(
        uses=uses,
        results=results,
        closed_ids=tuple(closed_ids),
        open_ids=tuple(sorted(open_ids)),
    )


def _result_content(value: object) -> str:
    """把 handler 返回值稳定序列化为 Agent 可见文本。"""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _object_schema(
    properties: Mapping[str, object], required: tuple[str, ...] = ()
) -> dict[str, object]:
    """构造禁止额外字段的对象型工具 Schema。"""
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


_DELEGATE_SUBMIT_EXAMPLE = '{"tasks":[{"task_id":"image-01","role":"image","goal":"为 P1、P2 制作无文字示意图"}]}'


STABLE_TOOL_SCHEMAS: dict[str, dict[str, object]] = {
    "delegate_submit": _object_schema(
        {
            "tasks": {
                "type": "array",
                "minItems": 1,
                "description": "直接传任务对象数组，不要编码成字符串。Research、Image、Slide 均可按独立任务并行，数量由现有资源配置调度；同文件或同素材不能并发抢写。",
                "items": _object_schema(
                    {
                        "task_id": {"type": "string", "minLength": 1,
                                    "description": "稳定任务 ID；相同 ID 仅重放已有记录，不重新执行。失败后再执行或新修复使用新 task_id；续做保留原 group_id，独立问题创建新身份"},
                        "role": {
                            "type": "string",
                            "enum": ["research", "image", "slide"],
                        },
                        "pages": {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 1},
                            "description": "新建 Slide 的完整责任页范围。多页只包含必须一起调整的视觉关系，在 goal 简述联动点；已定共享 CSS、素材、章节或风格不是成组理由，可独立完成的页同批分别提交。续做已指定原 group_id 或显式交接 handoff_from 时可省略，由注册表复用完整成员；局部修改范围写进 goal。若填写则必须与原责任范围一致。",
                        },
                        "group_id": {"type": "string", "description": "续做填写原回执 group_id，省略的 pages/output_path 自动复用原合同。独立 Research/Image 省略时按 task_id 创建新身份；不因同角色自动继承历史。"},
                        "output_path": {"type": "string", "description": "Research 任务稿的唯一写入路径，如 research/material-evidence.md。续做指定原 group_id 或显式交接 handoff_from 时，省略则复用原路径；新建独立任务省略时才生成稳定路径并在回执中返回。显式填写的冲突路径会被拒绝。Research 不写 knowledge.md，由 Orchestrator 整合采用后的结论。"},
                        "handoff_from": {"type": "string", "description": "可选：明确由新 Subagent 接手已终止的同角色 Owner，填写原 owner_id，使用新 group_id。省略的页组/研究稿路径从原 Owner 读取；显式改变范围仍被拒绝。正常续做只用原 group_id，不需此字段。"},
                        "asset_ids": {"type": "array", "items": {"type": "string"}, "uniqueItems": True,
                                      "description": "仅 Image 显式交接使用，配合 handoff_from 列出接手的已有素材 ID；不接管其他素材，复用图片无需转移作者。"},
                        "depends_on": {"type": "array", "items": {"type": "string"},
                                       "description": "依赖任务 ID，可引用同批或已提交任务；其余任务不等待"},
                        "goal": {"type": "string", "description": "本轮目标、修改范围和保留条件。Slide 的当前内容与来源先写回所属 brief，由 handoffs.plan 传入；goal 不另抄整份文案或来源表。事实/来源变更不能只写在 goal 中。Research/Image 的 goal 说明研究或素材需求。"},
                        "repair": {"type": "boolean"},
                        "reference_pages": {"type": "array", "items": {"type": "integer", "minimum": 1},
                                            "uniqueItems": True,
                                            "description": "可选：授权 Slide 只读这些已存在页面的 HTML 作为参考；不授予修改权限，也不改变 ownership。共享原语优先放 base.css。"},
                    },
                    ("task_id", "role"),
                ),
            }
        },
        ("tasks",),
    ),
    "delegate_wait": _object_schema(
        {
            "after_sequence": {"type": "integer", "minimum": 0},
        },
    ),
    "request_orchestrator": _object_schema(
        {
            "issue_type": {
                "type": "string",
                "enum": [
                    "asset_gap",
                    "plan_ambiguity",
                    "cross_group_conflict",
                    "shared_design_decision",
                ],
            },
            "question": {"type": "string", "minLength": 1},
            "pages": {"type": "array", "items": {"type": "integer", "minimum": 1}},
            "evidence": {"type": "string"},
        },
        ("issue_type", "question"),
    ),
    "respond_owner_request": _object_schema(
        {
            "request_id": {"type": "string"},
            "status": {"type": "string", "enum": ["resolved", "declined"]},
            "resolution": {"type": "string", "minLength": 1},
            "references": {"type": "array", "items": {"type": "string"}},
        },
        ("request_id", "status", "resolution"),
    ),
    "artifact_commit": _object_schema(
        {
            "artifact_ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": (
                    "已登记且属于当前 Owner 的 artifact_id，例如 page-html-01；"
                    "不是文件路径、page_uid、页码或 group_id，不能猜测 ID"
                ),
            },
            "source_hashes": {"type": "object"},
        },
        ("artifact_ids", "source_hashes"),
    ),
    "render": _object_schema(
        {
            "pages": {
                "type": "array",
                "items": {"type": "integer", "minimum": 1},
                "minItems": 1,
            }
        },
        ("pages",),
    ),
    "inspect": _object_schema(
        {
            "page": {"type": "array", "minItems": 1, "items": {"type": "integer", "minimum": 1},
                "description": "页码数组：单页传 [5]，多页传 [1,2,3]；不重复、不编码成字符串。多页生成并分析联系表，不要求先逐页 inspect。"},
            "path": {"type": "string", "minLength": 1,
                "description": "工作区可读图片或附件页图路径；与 page 二选一。"},
            "question": {"type": "string", "description": "可选补充用途与关注点，例如用于哪页、表达什么、重点核对什么；不要写请批准、直接通过或忽略其他缺陷。启用外部视觉复核时，页面验收会由 Harness 另行包装给独立 Critic；Image Owner 的素材筛选仍只做自身像素检查。"},
            "review_stage": {"type": "string", "enum": ["draft", "final", "blocking_recheck"],
                "description": "视觉检查阶段。draft/缺省只做本地像素检查，用于草稿/占位骨架，不证明真实素材已用上或终稿通过；final 或 blocking_recheck 在外部视觉复核已启用时才触发 Critic。"},
            "retain": {"type": "boolean", "description": "默认图片只供紧接着的一次模型决策；true 也只额外保留一轮，且同一 Owner 最多短留 2 张。它不是图片归档或逐页阅读模式。连续阅读附件页、依次修页或素材采购不要设 true；需要时用路径/短回执继续。Orchestrator 默认复用 Image Owner 交接并检查合成页，仅有具体未解像素问题时重开原图"},
        },
        (),
    ),
    "verification_submit": _object_schema(
        {
            "status": {"type": "string", "enum": ["issues_found", "issues_resolved"]},
            "issues": {
                "type": "array",
                "items": _object_schema(
                    {
                        "positions": {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 1},
                            "description": "受影响页码；空数组表示整册问题",
                        },
                        "problem": {"type": "string", "minLength": 1},
                        "suggested_fix": {"type": "string", "minLength": 1},
                        "target": {
                            "type": "string",
                            "enum": ["slide", "image", "orchestrator"],
                            "description": "通常省略，按页责任路由；需要换素材时指定 image，共享系统或重规划指定 orchestrator",
                        },
                    },
                    ("positions", "problem", "suggested_fix"),
                ),
            },
            "resolved_issue_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        ("status", "issues"),
    ),
}


STABLE_TOOL_ROLES: dict[str, frozenset[str]] = {
    "delegate_submit": frozenset({"orchestrator"}),
    "delegate_wait": frozenset({"orchestrator"}),
    "request_orchestrator": frozenset({"slide"}),
    "respond_owner_request": frozenset({"orchestrator"}),
    "artifact_commit": frozenset({"orchestrator", "image", "slide"}),
    "render": frozenset({"slide", "orchestrator"}),
    "inspect": frozenset({"orchestrator", "slide", "research", "image", "critic"}),
    "verification_submit": frozenset({"orchestrator"}),
}


# 生产宿主提供的角色基础工具（文件、脚本、网络、图片）。它们的 handler 由
# production.py 的 ProductionToolServices 域方法提供，但名称、描述、Schema、角色与
# 写入/画布属性都是本模块的唯一真源，避免第二处 ToolDefinition 装配点。
BASE_TOOL_SPECS: dict[str, "ToolSpec"] = {}


@dataclass(frozen=True)
class ToolSpec:
    """一个工具除 handler 外的全部公共契约字段（单一真源）。"""

    name: str
    description: str
    input_schema: Mapping[str, object]
    roles: frozenset[str]
    mutates_workspace: bool = False
    path_argument: str = ""
    model_visible: bool = True

    def bind(self, handler: ToolHandler) -> ToolDefinition:
        """把 Handler Adapter 绑定到本契约，产出可注册的工具定义。"""
        return ToolDefinition(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
            handler=handler,
            roles=self.roles,
            mutates_workspace=self.mutates_workspace,
            path_argument=self.path_argument,
            model_visible=self.model_visible,
        )


_STABLE_DESCRIPTIONS: dict[str, str] = {
    "delegate_submit": (
        "向持久任务图提交 Owner 任务及依赖。tasks 直接传任务对象数组，不要编码成字符串。"
        f"最小格式示例：{_DELEGATE_SUBMIT_EXAMPLE}\n"
        "Slide 接受前按有效计划补齐本批缺失结构，不覆盖已有页。回执附带 planned_page_count、self_authored_pages、delegated_pages、unassigned_pages。独立 Research/Image 可并发；Research 写回执中的 output_path，Orchestrator 汇总 knowledge.md。重放同一 task_id 不会重复执行；失败后按回执 recovery 参数使用新 task_id 正式接续，不能只换名字或声明旧失败已取代。"
        "没有独立工作时自然结束本轮，Harness 自动等待真实结果、失败或求助事件并继续同一会话；不要用 ls slides/ 或重复读取文件轮询进度。"
    ),
    "delegate_wait": "显式收割一次后台任务图事件；自然结束本轮时 Harness 也会继续等待，不要把它当计时器反复轮询。需要处理 Research/Image/Slide 的失败、求助、终止或当前批次全部结束时调用；普通 Slide 完成合并返回，即使仍有未委派页也不逐页唤醒。回执附带当前页覆盖、pending_requests 与失败事件；失败事件表示子任务状态，不表示 delegate_wait 工具失败。page_coverage_source 区分计划索引和已登记页；all_terminal 不代表整册完成，仍需核对缺页与失败。页面齐备后先打开当前联系表作整册比较，再放大疑点页。无需 collect",
    "request_orchestrator": "暂停当前 Slide Owner 并请求 Orchestrator 协助",
    "respond_owner_request": "向等待中的 Slide Owner 送回明确决定，恢复原会话；决定及必要的共享修改就绪后优先回复，不把修改 CSS 本身当作已答复",
    "artifact_commit": (
        "显式提交已登记产物。正常创作无需额外调用：Slide 的 write_file/patch 已自动提交 HTML，"
        "image.py finalize 已同步图片 catalog；接着按角色合同 inspect 或交接即可。"
        "仅在明确需要显式提交已有 artifact_id 时使用，不创建产物，也不代替像素检查"
    ),
    "render": "按实际 DOM/CSS 渲染显式页面",
    "inspect": (
        '唯一视觉检查入口：{"page":[5]} 看单页，{"page":[1,2,3]} 看带页号联系表，'
        '{"path":"assets/figure.png"} 看素材/附件图片；page 与 path 二选一。'
        "页面仅在 PNG 缺失或过期时自动渲染；这是渲染并查看的统一入口，不要求先调用其他渲染工具，也不要求先逐页检查才能看联系表。"
        "按目标使用默认问题，question 只写本次用途与关注点；不要要求批准、直接通过或忽略其他缺陷。"
        "review_stage 缺省或 draft 只做本地像素检查，用于草稿/占位骨架，不证明真实素材已用上、最终裁切成立或终稿通过；页面终稿或阻塞修复复检才传 final/blocking_recheck，且只有外部视觉复核已启用时才触发 Critic。"
        "启用后，页面验收会由 Harness 包装成独立、只看像素的 Critic 问题；Image Owner 的素材筛选不触发外部 Critic。"
        "有 Critic 时公开意见与实际图片一起进入你的上下文。"
        "单页看细节，多页比较排印、导航、图像处理、色彩角色与背景变化是否仍属于同一设计系统；不同页不必完全同色，但不能像不同模板拼接。小字看不清时用单页放大。"
        "Orchestrator 可复核委派页，但无修改权；仅真实作者的单页消费登记作者检查，联系表不替代逐页全分辨率检查。"
        "意见不是命令，执行成功不代表质量通过；相同版本和问题可复用，失败如实返回"
    ),
    "verification_submit": (
        "报告待修问题用 issues_found；复核修复后用 issues_resolved、issues:[] 和 resolved_issue_ids 明确闭合对应问题。"
        "不用于声明 ready；正常结束回合后 Harness 自动检查整册完成条件，返回具体缺口或收尾。"
        "待修问题只需 positions/problem/suggested_fix；默认阻止闭合，修复后依据当前像素复验。"
        "独立视觉 Critic 提供观察而非否决权；不要求双方意见一致。"
        "资源同源与实际作者检查必须满足；启用视觉 Critic 时还须满足其执行完整性。内容 QC 在 raw 完成后离线执行"
    ),
}


def tool_specs() -> dict[str, ToolSpec]:
    """返回全部公共工具的契约表：控制工具 + 生产基础工具（唯一真源）。"""
    specs: dict[str, ToolSpec] = {}
    for name, schema in STABLE_TOOL_SCHEMAS.items():
        specs[name] = ToolSpec(
            name=name,
            description=_STABLE_DESCRIPTIONS[name],
            input_schema=schema,
            roles=STABLE_TOOL_ROLES[name],
            # 页面写入会自动提交，inspect 会按需渲染。保留旧动作的执行能力供
            # checkpoint 恢复和工程诊断，但不再让新模型学习两套等价生命周期。
            model_visible=name not in {"artifact_commit", "delegate_wait", "render"},
        )
    for name, spec in BASE_TOOL_SPECS.items():
        if name in specs:
            raise ValueError(f"base tool {name!r} collides with a control tool")
        specs[name] = spec
    return specs


def register_base_tool_spec(spec: ToolSpec) -> None:
    """登记一个生产基础工具契约，保证名称在整个目录内唯一。"""
    if spec.name in STABLE_TOOL_SCHEMAS or spec.name in BASE_TOOL_SPECS:
        raise ValueError(f"tool {spec.name!r} is already declared")
    BASE_TOOL_SPECS[spec.name] = spec


def register_tools(
    runtime: ToolRuntime,
    handlers: Mapping[str, ToolHandler],
    *,
    require_complete: bool = True,
) -> None:
    """按唯一契约表注册工具，每个 handler 必须对应一个已声明的工具契约。

    这是 Tool 目录的唯一构建入口：调用方只提供 name→Handler Adapter 映射，
    工具的名称、Schema、角色与画布/写入属性一律来自 :func:`tool_specs`。
    ``require_complete`` 为真时（默认）要求覆盖全部契约；生产宿主分两批注册
    控制工具与基础工具时可置假，只注册本批 handler 命名的工具。
    """
    specs = tool_specs()
    extra = sorted(set(handlers) - set(specs))
    if extra:
        raise ValueError(f"handlers without a declared tool spec: {extra}")
    if require_complete:
        missing = sorted(set(specs) - set(handlers))
        if missing:
            raise ValueError(f"missing tool handlers: {missing}")
    for name in specs:
        if name in handlers:
            runtime.register(specs[name].bind(handlers[name]))


_ALL_PRODUCTION_ROLES = frozenset(
    {"orchestrator", "research", "image", "slide"}
)

for _spec in (
    ToolSpec(
        name="read_file",
        description="读取获授权文本或 web_extract 返回的 source_ref；返回会按 prompt 历史预算在行边界截断，query 定位字面关键词及邻近内容，offset/limit 续读。普通文本的 N| 前缀是展示行号，不属于文件原文，复制到 patch 时只取其后的文本；搜索片段可能不连续，完整段落用 offset/limit 读取。读取已登记的 MinerU document.md 或内容清单时，同一片段直接关联的 figure/chart 会作为图片 token 随工具结果返回，无需再 inspect 原页。内容清单返回完整来源定位的单行条目，按条目分页。不执行正则或 shell。",
        input_schema=_object_schema(
            {
                "path": {
                    "type": "string",
                    "description": "工作区相对路径，如 deck.md；也可原样使用 web_extract 返回的 sources/web/... source_ref。Skill 文件使用注入的 selected Skill logical root 下完整逻辑路径 skills/<当前 Skill>/...。Markdown 相对链接先按所在文档目录解析，消去 . / .. 并去掉 #章节锚点；不要直接传裸 references/...、内部缓存路径或源码/object 绝对路径。仍受当前读域限制。",
                },
                "offset": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                "query": {"type": "string", "description": "可选：不区分大小写的字面关键词；offset 为起始行或附件条目，limit 限制返回数量。"},
            },
            ("path",),
        ),
        roles=_ALL_PRODUCTION_ROLES,
    ),
    ToolSpec(
        name="write_file",
        description="原子写入当前 Owner 写域内的完整 UTF-8 文件",
        input_schema=_object_schema(
            {"path": {"type": "string"}, "content": {"type": "string"},
             "repair_reason": {"type": "string",
                "description": "已检查页面开始一批修复时必填：问题定位、阅读后果、修复目标。首稿无需；连续修改至成功 inspect 算一批。"}},
            ("path", "content"),
        ),
        roles=_ALL_PRODUCTION_ROLES,
        mutates_workspace=True,
        path_argument="path",
    ),
    ToolSpec(
        name="patch",
        description="以精确旧文本替换当前 Owner 写域内文件内容，不是 diff、正则或模糊匹配。old_text 从已读取的当前文件原文复制；匹配总数不符时整次不写入，先重读对应原文再修正参数，不猜标点或转义。",
        input_schema=_object_schema(
            {
                "path": {"type": "string"},
                "old_text": {"type": "string", "minLength": 1,
                    "description": "待替换的连续原文，去掉 read_file 展示的 N| 行号前缀；保留文件本身的 Markdown 标记、空格、换行、引号与反斜杠。JSON 解码后的参数值须与文件逐字符一致：按 JSON 规范转义一次，不把显示用的转义反斜杠再写进文本。只改一处时选取足够上下文使其唯一。"},
                "new_text": {"type": "string",
                    "description": "替换后的完整片段，不附 diff 的加减号或代码围栏；文件本身需要的标记照常保留。使用与 old_text 相同的 JSON 转义规则；空字符串表示删除该片段。"},
                "expected_replacements": {"type": "integer", "minimum": 1,
                    "description": "默认 1，必须等于 old_text 在当前文件中的精确匹配总数，不是最多替换次数。只有确实要替换全部这些位置时才填多处；否则扩大 old_text 上下文定位唯一位置。"},
                "repair_reason": {"type": "string",
                    "description": "已检查页面开始一批修复时必填：问题定位、阅读后果、修复目标。首稿无需；连续修改至成功 inspect 算一批。"},
            },
            ("path", "old_text", "new_text"),
        ),
        roles=_ALL_PRODUCTION_ROLES,
        mutates_workspace=True,
        path_argument="path",
    ),
    ToolSpec(
        name="terminal",
        description="执行当前角色的内容、素材或结构修订 Skill 命令，或内置只读查询：ls [-al] [相对目录 ...]；grep/rg [-rniF] [--max-results N] 固定文本 [相对路径 ...]。页面初始化、像素渲染查看、讲稿派生和整册交付由 Harness 随写入、inspect 与自然收尾自动完成，不需要生命周期命令。ls 可用 ; 连接多条，支持 2>/dev/null（保留结构化错误和失败状态）。grep 目录需 -r，rg 默认递归；检索是字面匹配而非正则，-i 忽略大小写，-n 显示行号。支持末尾 | head -40 或 | head -n 40 截断当前查询的返回项数，不执行 shell/head。路径可加引号，沿用当前读域及固定 Skill；不跟随软链接、展开变量/通配符或创建目录。整次最多返回 200 项；检索跳过二进制/超大文件并报告扫描预算、截断和部分失败。完整内容用 read_file",
        input_schema=_object_schema(
            {
                "command": {"type": "string", "minLength": 1},
                "timeout_s": {"type": "integer", "minimum": 1},
            },
            ("command",),
        ),
        roles=frozenset({"orchestrator", "image", "slide"}),
    ),
    ToolSpec(
        name="web_search",
        description="使用已配置搜索服务检索网页或图片候选并返回来源页",
        input_schema=_object_schema(
            {
                "query": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                "search_type": {"type": "string", "enum": ["search", "images"]},
                "language": {"type": "string"},
                "region": {"type": "string"},
            },
            ("query",),
        ),
        roles=frozenset({"orchestrator", "research", "image"}),
    ),
    ToolSpec(
        name="web_extract",
        description="提取公开 HTML/PDF 正文，保留结构并返回 prompt-bounded 片段；需要更多正文时把返回的 source_ref 原样交给 read_file，并用 query/offset 续读。max_chars 只限制当前片段且最多 12000；普通网页默认 HTTP，明确需要 JavaScript 加载正文时才用 render_js=true。两种结果分别缓存。浏览器不登录、不提交表单；不处理扫描 OCR、Office 或图片，403 等目标站限制应直接更换来源",
        input_schema=_object_schema(
            {
                "url": {"type": "string", "minLength": 1},
                "max_chars": {"type": "integer", "minimum": 1000, "maximum": 12000},
                "render_js": {"type": "boolean", "description": "仅用于需JavaScript加载的公开正文；默认false，不用于绕过访问限制"},
            },
            ("url",),
        ),
        roles=frozenset({"orchestrator", "research"}),
    ),
    ToolSpec(
        name="web_screenshot",
        description=(
            "获取公开网页的真实 1280x900 视口截图并自动登记到 assets/，返回 artifact_id、path 与真实来源。"
            "query 是页面中可见原文的字面短语，用于滚动定位，不执行选择器或脚本；未找到则报错。"
            "公开 PDF 返回原件页图；URL 的 #page=N 指定从 1 开始的物理页，优先于 query。否则 query 匹配原文选页，省略则读第 1 页；不做 OCR。"
            "默认复用本次任务已取得的同 URL/query 截图。不登录、不提交表单、不绕过 403，不访问内网。"
            "遵守仅限附件、禁网等来源要求。获取成功不等于验收通过，随后用 inspect(path=...) 看实际图片；"
            "resource_busy 表示本地资源尚未可用、没有访问来源，可等待渲染结束后再试同一 URL，不应当作网站拒绝。"
            "登记消费者时由返回的原素材 Owner 沿用身份，以 --kind real 和 --source source_url 登记。"
        ),
        input_schema=_object_schema({
            "url": {"type": "string", "minLength": 1},
            "query": {"type": "string", "minLength": 1, "maxLength": 500},
        }, ("url",)),
        roles=frozenset({"orchestrator", "image"}),
    ),
    ToolSpec(
        name="image_generate",
        description=(
            "按已规划的 generated 路线生成一张无文字位图到 assets/，成功后已自动登记，返回 artifact_id、path 和 owner_id。"
            "后续 image.py register 由原素材 Owner 沿用返回的 ID 与路径补充用途、消费者和区域说明，不为同一路径另起 ID。"
            "缓存复用保留原 ID 与 Owner；业务名称写入 purpose。登记成功不等于视觉验收。"
        ),
        input_schema=_object_schema(
            {
                "prompt": {"type": "string", "minLength": 1},
                "aspect_ratio": {
                    "type": "string",
                    "enum": ["landscape", "portrait", "square"],
                },
            },
            ("prompt",),
        ),
        roles=frozenset({"orchestrator", "image"}),
    ),
):
    register_base_tool_spec(_spec)
