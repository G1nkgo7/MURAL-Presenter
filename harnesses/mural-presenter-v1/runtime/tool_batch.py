"""同轮独立工具的小批次执行；不替代工具权限、资源槽或事务日志。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
from typing import Iterator, Sequence
from urllib.parse import urlsplit, urlunsplit

from .tools import ToolCall, ToolContext, ToolResult, ToolRuntime, normalize_workspace_path


MAX_INDEPENDENT_TOOLS = 8


def _access(call: ToolCall, context: ToolContext) -> tuple[set[str], set[str]] | None:
    """None 是顺序屏障；仅识别已审查的固定入口，不推测未知工具的副作用。"""
    arguments = call.arguments
    if call.name == "read_file":
        try:
            path = normalize_workspace_path(arguments.get("path", ""))
        except ValueError:
            return None
        root = context.metadata.get("workspace_root")
        if root:
            target = Path(root) / path
            try:
                aliased = target.resolve() != target.absolute()
            except (OSError, RuntimeError):
                return None
            if aliased:
                return None
        # Private runtime data may change asynchronously. Public web source refs
        # resolve to that cache internally, so their reads also stay ordered.
        if path.startswith(("_trace/", "_state/", "sources/web/")):
            return None
        attachment = re.match(r"inputs/extracted/(att-[0-9a-f]{12})(?:/|$)", path)
        return ({"attachment:" + attachment[1]} if attachment else {"file:" + path}), set()
    if call.name == "web_search":
        query = " ".join(str(arguments.get("query", "")).split())
        return (set(), {"search:" + query}) if query else None
    if call.name == "web_extract":
        try:
            parts = urlsplit(str(arguments.get("url", "")).strip())
            if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
                return None
            url = urlunsplit((parts.scheme.lower(), parts.netloc, parts.path, parts.query, ""))
        except ValueError:
            return None
        return set(), {"source:" + url}
    return None


def execute_tool_batch(
    tools: ToolRuntime, context: ToolContext, calls: Sequence[ToolCall],
) -> Iterator[tuple[ToolCall, ToolResult]]:
    """连续独立调用有界重叠，屏障原线程执行；结果按声明顺序消费后才离开批次。"""
    index = 0
    while index < len(calls):
        group, reads, writes = [], set(), set()
        while index + len(group) < len(calls) and len(group) < MAX_INDEPENDENT_TOOLS:
            call = calls[index + len(group)]
            access = _access(call, context)
            if access is None:
                break
            call_reads, call_writes = access
            if writes & (call_reads | call_writes) or reads & call_writes:
                break
            group.append(call)
            reads.update(call_reads)
            writes.update(call_writes)
        if len(group) < 2:
            call = calls[index]
            yield call, tools.execute(context, call)
            index += 1
            continue
        with ThreadPoolExecutor(max_workers=len(group), thread_name_prefix="mural-tool") as pool:
            futures = [pool.submit(tools.execute, context, call) for call in group]
            for call, future in zip(group, futures):
                yield call, future.result()
        index += len(group)
