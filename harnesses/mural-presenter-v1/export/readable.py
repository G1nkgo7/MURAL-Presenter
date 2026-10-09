"""从实时事件生成可恢复的 Agent 视图；目录和快照都不是事实源。"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import uuid
from collections import Counter, defaultdict
from datetime import datetime
import math
import shlex
import sys

READABLE_SCHEMA = "mural.agent-readable.v1"

def _json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default

def _atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.{uuid.uuid4().hex}.pending")
    payload = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    try:
        with pending.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        pending.unlink(missing_ok=True)

def _append(path, value):
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        size, start, content = handle.tell(), handle.tell(), b""
        while start and content.count(b"\n") < 2:
            length = min(65536, start)
            start -= length
            handle.seek(start)
            content = handle.read(length) + content
        end = content.rfind(b"\n") + 1
        if start + end != size:
            handle.truncate(start + end)
        rows = content[:end].splitlines()
        if rows and json.loads(rows[-1]).get("sequence", 0) >= value["sequence"]:
            return
        handle.seek(0, os.SEEK_END)
        handle.write((json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
        handle.flush()
        os.fsync(handle.fileno())

def _label(value):
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value)).strip(".-") or "unnamed"

def _assistant_block(messages, event, block):
    attempt = event["payload"].get("attempt_id")
    message = next((item for item in reversed(messages) if item.get("role") == "assistant"
        and item.get("segment_id") == event["segment_id"] and item.get("turn") == event["turn"]
        and (not attempt or item.get("attempt_id") == attempt)), None)
    if message is None:
        message = {"role": "assistant", "content": [], "segment_id": event["segment_id"],
                   "turn": event["turn"], "attempt_id": attempt, "sequence": event["sequence"]}
        messages.append(message)
    message["content"].append(block)


class AgentTraceProjection:
    def __init__(self, root, *, rebuild=False):
        self.root = Path(root).resolve()
        self.trace = self.root / "_trace"
        self.index_path = self.trace / "agent-index.json"
        self.index = {"schema": READABLE_SCHEMA, "agents": []} if rebuild else None
        self.rebuild, self.initialized = rebuild, set()

    def _identity(self, event):
        if self.index is None:
            self.index = _json(self.index_path, {"schema": READABLE_SCHEMA, "agents": []})
        agent, segment, payload = event["agent_id"], event["segment_id"], event["payload"]
        rows = self.index["agents"]
        for row in rows:
            if (row["agent_id"] == agent and segment in row["segment_ids"]
                    and (not payload.get("attempt") or row["attempt"] == int(payload["attempt"]))):
                return row
        contexts = event["event_type"] in {"agent_started", "agent_context"}
        candidates = [row for row in rows if row["agent_id"] == agent]
        if candidates and not contexts:
            row = candidates[-1]
        else:
            registry = _json(self.root / "_state/owners.json", {}).get("owners", {})
            owner = next((item for item in registry.values() if item.get("owner_id") == agent), {})
            meta = {**owner, **(payload if contexts else {})}
            role, attempt = event["role"], int(meta.get("attempt") or 1)
            row = next((item for item in candidates if item["attempt"] == attempt), None)
            if row is None:
                group = str(meta.get("group_id") or "")
                base = "orchestrator" if role == "orchestrator" else (
                    "slide_group_" + _label(group or agent) if role == "slide" else _label(role))
                if attempt > 1:
                    base += f"_attempt_{attempt:02d}"
                relative = "_trace/" + base if role == "orchestrator" else "_trace/subagents/" + base
                if any(item["path"] == relative for item in rows):
                    relative += "-" + hashlib.sha256(agent.encode()).hexdigest()[:8]
                row = {"agent_id": agent, "role": role, "group_id": group,
                       "task_id": str(meta.get("task_id") or ""), "pages": list(meta.get("pages") or []),
                       "attempt": attempt, "segment_ids": [], "path": relative, "last_sequence": 0}
                rows.append(row)
        if segment and segment not in row["segment_ids"]:
            row["segment_ids"].append(segment)
        return row

    def apply(self, event):
        if event["event_type"] == "trace_projection_failed":
            return
        if not self.rebuild:
            self.index = _json(self.index_path, {"schema": READABLE_SCHEMA, "agents": []})
        row = self._identity(event)
        sequence = event["sequence"]
        if sequence <= row["last_sequence"]:
            return
        directory = self.root / row["path"]
        if directory.is_symlink() or not directory.resolve().is_relative_to(self.trace.resolve()):
            raise ValueError("Agent projection path escapes trace")
        directory.mkdir(parents=True, exist_ok=True)
        reset = self.rebuild and row["path"] not in self.initialized
        self.initialized.add(row["path"])
        meta = {"projected_from": "live-events.jsonl", "authoritative": False, "last_sequence": sequence}
        # 进展诊断不改变消息、工具或产物，避免每个回合额外重写全部快照。
        diagnostic = event["event_type"].startswith("agent_") and event["event_type"] not in {
            "agent_started", "agent_context", "agent_completed", "agent_failed"}
        diagnostic = diagnostic or event["event_type"].startswith("resource_wait_")
        if event["event_type"].endswith("_delta") or (diagnostic and not reset):
            if event["event_type"] == "assistant_reasoning_delta":
                _append(directory / "reasoning.jsonl", {**event, **event["payload"], **meta})
            _append(directory / "events.jsonl", {**event, **meta})
            row["last_sequence"] = sequence
            self.index.update(meta)
            _atomic(self.index_path, self.index)
            return
        config = {} if reset else _json(directory / "config.json", {})
        snapshots = {}
        for filename, key in (("messages", "messages"), ("tool_log", "transactions"), ("tools", "tools"), ("artifacts", "artifacts")):
            snapshots[filename] = [] if reset else _json(directory / (filename + ".json"), {}).get(key, [])
        messages, transactions = snapshots["messages"], snapshots["tool_log"]
        if reset:
            for name in ("events.jsonl", "reasoning.jsonl"):
                _atomic(directory / name, "")
        kind, payload = event["event_type"], event["payload"]
        changed = {
            "agent_context": {"messages", "tools"},
            "assistant_message": {"messages"},
            "assistant_reasoning": {"messages"},
            "tool_use": {"messages", "tool_log"},
            "tool_result": {"messages", "tool_log"},
            "tool_output": {"tool_log"},
        }.get(kind, set())
        if kind == "tool_result" and payload.get("artifacts"):
            changed.add("artifacts")
        if kind == "agent_context":
            config.setdefault("contexts", []).append({"segment_id": event["segment_id"], "sequence": sequence, **payload})
            config.update({key: value for key, value in payload.items() if key not in {"messages", "tools", "system"}})
            config["context_status"] = "recorded"
            snapshots["tools"] = payload.get("tools", [])
            messages.extend(payload.get("messages", []))
            prompt = payload.get("system", "")
            _atomic(directory / "system_prompt.md",
                f"<!-- projected_from=live-events.jsonl authoritative=false last_sequence={sequence} "
                f"agent_id={event['agent_id']} segment_id={event['segment_id']} timestamp={event['timestamp']} "
                f"sha256={hashlib.sha256(prompt.encode()).hexdigest()} -->\n" + prompt)
        if kind == "assistant_message":
            _assistant_block(messages, event, {"type": "text", "text": payload.get("message", "")})
        if kind == "assistant_reasoning":
            config["reasoning_count"] = config.get("reasoning_count", 0) + 1
            _append(directory / "reasoning.jsonl", {**event, **payload, **meta})
            _assistant_block(messages, event, {"type": "thinking",
                "thinking" if payload["mode"] == "full" else "summary": payload["content"],
                "source": payload["source"], "partial": payload.get("partial", False),
                "interrupted": payload.get("interrupted", False)})
            config.setdefault("reasoning_attempts", []).append({"attempt_id": payload.get("attempt_id"), "mode": payload["mode"]})
        elif kind == "assistant_analysis_summary" and not any(
                item.get("attempt_id") == payload.get("attempt_id") for item in config.get("reasoning_attempts", [])):
            config["reasoning_count"] = config.get("reasoning_count", 0) + 1
            _append(directory / "reasoning.jsonl", {**event, **meta, "mode": "summary",
                "content": payload.get("summary", ""), "source": "provider_response", "partial": False, "interrupted": False})
        if kind == "tool_use":
            args = payload.get("arguments", {})
            transactions.append({"tool_use_id": payload["tool_use_id"], "turn": event["turn"],
                "segment_id": event["segment_id"], "name": payload["name"], "args": args,
                "started_at": event["timestamp"], "finished_at": None, "duration_ms": None,
                "status": "running", "ok": None, "error_code": "", "result_summary": None, "diagnostic_ref": None})
            _assistant_block(messages, event, {"type": "tool_use",
                "id": payload["tool_use_id"], "name": payload["name"], "input": args})
        if kind in {"tool_result", "tool_output"}:
            transaction = next((item for item in transactions if item["tool_use_id"] == payload["tool_use_id"]), None)
            if transaction is not None:
                if payload.get("diagnostic_ref"):
                    transaction["diagnostic_ref"] = payload["diagnostic_ref"]
                if kind == "tool_result":
                    transaction.update(finished_at=event["timestamp"], duration_ms=payload.get("duration_ms"),
                        status="completed" if payload.get("ok") else "failed", ok=payload.get("ok"),
                        error_code=payload.get("error_code", ""), result_summary=payload.get("result"))
                    messages.append({"role": "user", "content": [{"type": "tool_result",
                        "tool_use_id": payload["tool_use_id"], "content": payload.get("result"), "is_error": not payload.get("ok")}]})
                    snapshots["artifacts"].extend(payload.get("artifacts", []))
        if kind.startswith("provider_"):
            config.setdefault("provider_attempts", {}).setdefault(str(payload.get("attempt_id", "")), {}).update(
                {**payload, "event_type": kind})
        if kind in {"agent_started", "agent_context"}:
            config.setdefault("started_at", event["timestamp"])
            config["status"] = "running"
        if kind in {"agent_completed", "agent_failed"}:
            config.update(status="completed" if kind == "agent_completed" else "failed",
                          finished_at=event["timestamp"], exit_reason=payload.get("stop_reason") or payload.get("error_type"))
        if config.get("context_status") != "recorded":
            config.update(context_status="unavailable_in_source_trace", unavailable_reason="not_recorded_by_original_run")
            if not (directory / "system_prompt.md").is_file() or reset:
                _atomic(directory / "system_prompt.md", "<!-- projected_from=live-events.jsonl authoritative=false -->\n"
                        "unavailable_in_source_trace: not_recorded_by_original_run\n")
        row["last_sequence"] = sequence
        config.update({**row, **meta, "message_count": len(messages), "tool_count": len(transactions)})
        _append(directory / "events.jsonl", {**event, **meta})
        if not (directory / "reasoning.jsonl").exists():
            _atomic(directory / "reasoning.jsonl", "")
        for filename, key in (("messages", "messages"), ("tool_log", "transactions"), ("tools", "tools"), ("artifacts", "artifacts")):
            path = directory / (filename + ".json")
            if reset or filename in changed or not path.is_file():
                _atomic(path, {**meta, key: snapshots[filename]})
        _atomic(directory / "config.json", config)
        self.index.update(meta)
        _atomic(self.index_path, self.index)


def export_readable_by_role(workspace):
    from runtime.live_trace import iter_jsonl_events
    projection = AgentTraceProjection(workspace.paths.root, rebuild=True)
    for event in iter_jsonl_events(workspace.paths.trace / "live-events.jsonl"):
        if event.get("schema") == "mural.live-event.v1":
            projection.apply(event)
    return projection.index


_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
_PAGE_COUNTS = ("render_calls", "render_successful_calls", "inspect_calls", "inspect_successful_calls", "inspection_cache_hits", "render_receipt_observations")


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def _measure(values):
    recorded = [value for value in values if _number(value) is not None]
    return {"sum": round(sum(recorded), 6) if recorded else None, "recorded": len(recorded), "missing": len(values) - len(recorded)}


def _object(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def _cost_json(root, relative, gaps):
    path = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts or not path.resolve().is_relative_to(root):
        gaps["unsafe_reference"] += 1
        return {}
    try:
        return _object(_json(path, {}))
    except (OSError, UnicodeError, ValueError):
        gaps["unreadable_json"] += 1
        return {}


def _attempt_usage(root, attempt, gaps):
    """每次请求只取最终原件一次；原始正文、签名和错误均不进入报告。"""
    usage, source = {}, "unavailable"
    raw_ref = attempt.get("raw_ref", "")
    if raw_ref and (not raw_ref.startswith("_trace/") or not (root / raw_ref).resolve().is_relative_to(root / "_trace")):
        gaps["unsafe_raw_reference"] += 1
    elif raw_ref:
        record = _cost_json(root, raw_ref, gaps)
        if record.get("attempt_id") != attempt["attempt_id"]:
            gaps["raw_attempt_identity_missing_or_mismatched"] += 1
        elif record.get("status") == "completed" and not record.get("partial"):
            body = _object(record.get("body"))
            body = _object(body.get("response")) or body
            usage = _object(body.get("usage"))
            attempt.setdefault("api_format", record.get("api_format"))
            if usage:
                source = "final_raw_response"
    fmt = attempt.get("api_format") or "unknown"
    if fmt == "anthropic":
        tokens = {key: _number(usage.get(key)) for key in _TOKEN_FIELDS}
        semantics = "input_tokens excludes cache_read and cache_creation"
    elif fmt in {"openai_chat_compatible", "openai_responses"}:
        chat = fmt == "openai_chat_compatible"
        details = _object(usage.get("prompt_tokens_details" if chat else "input_tokens_details"))
        tokens = {"input_tokens": _number(usage.get("prompt_tokens" if chat else "input_tokens")),
                  "output_tokens": _number(usage.get("completion_tokens" if chat else "output_tokens")),
                  "cache_read_input_tokens": _number(details.get("cached_tokens")),
                  "cache_creation_input_tokens": None if chat else _number(details.get("cache_write_tokens"))}
        semantics = ("input_tokens includes cache_read; do not add cached tokens again" if chat else
                     "input_tokens includes cache_read and cache_creation; ordinary input is total minus both; charge each bucket once")
    else:
        tokens = {key: None for key in _TOKEN_FIELDS}
        semantics = "unknown; no cross-format input total"
    if tokens["output_tokens"] is None:
        tokens["output_tokens"] = _number(attempt.get("diagnostic_output_tokens"))
        if tokens["output_tokens"] is not None and source == "unavailable":
            source = "response_diagnostics_output_only"
    ordinary_input = tokens["input_tokens"] if fmt == "anthropic" else None
    if fmt == "openai_responses" and all(tokens[key] is not None for key in (
        "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"
    )):
        ordinary_input = tokens["input_tokens"] - tokens["cache_read_input_tokens"] - tokens["cache_creation_input_tokens"]
        if ordinary_input < 0:
            gaps["inconsistent_cache_token_buckets"] += 1
            ordinary_input = None
    return {"api_format": fmt, "input_semantics": semantics, "source": source,
            "ordinary_input_tokens": ordinary_input, **tokens}


def _provider_costs(attempts):
    groups = defaultdict(list)
    for row in attempts:
        groups[row["usage"]["api_format"]].append(row["usage"])
    return {"attempts": len(attempts), "started": sum(row.get("started", False) for row in attempts),
            "states": dict(Counter(row.get("state", "open") for row in attempts)),
            "elapsed_s": _measure([row.get("elapsed_s") for row in attempts]),
            "local_queue_wait_s": _measure([row.get("queue_wait_s") for row in attempts]),
            "tokens_by_api_format": {fmt: {"attempts": len(rows), "input_semantics": rows[0]["input_semantics"],
                "sources": dict(Counter(row["source"] for row in rows)),
                **{key: _measure([row[key] for row in rows]) for key in (*_TOKEN_FIELDS, "ordinary_input_tokens")}} for fmt, rows in sorted(groups.items())}}


def _tool_pages(name, arguments, result):
    pages = arguments.get("pages", [])
    pages = list(pages) if isinstance(pages, list) else []
    if arguments.get("page") is not None:
        pages.extend(arguments["page"] if isinstance(arguments["page"], list) else [arguments["page"]])
    action = "inspect" if name in {"inspect", "inspect_group"} else ""
    if name == "terminal":
        try:
            argv = shlex.split(str(arguments.get("command") or ""))
        except ValueError:
            argv = []
        for index, token in enumerate(argv[:-1]):
            if token.endswith(("/slide.py", "/orchestrator.py")) and argv[index + 1] in {"render", "render-group"}:
                action = "render"
                for position, flag in enumerate(argv):
                    option, separator, value = flag.partition("=")
                    if option in {"--page", "--pages"}:
                        if not separator and position + 1 < len(argv):
                            value = argv[position + 1]
                        pages.extend(value.split(","))
    descriptor = _object(result.get("pending_inspection")) or _object(result.get("current_inspection"))
    receipts = result.get("pages", []) if action == "render" else descriptor.get("members", [])
    receipts = [row for row in receipts if isinstance(row, dict)] if isinstance(receipts, list) else []
    if isinstance(result.get("render_committed"), dict):
        receipts.append(result["render_committed"])
    pages.extend(row.get("page") for row in receipts)
    if action == "inspect" and result.get("page") is not None:
        pages.append(result["page"])
    return action, sorted({int(page) for page in pages if str(page).isdigit() and int(page) > 0}), receipts


def _workspace_costs(root):
    from runtime.live_trace import iter_jsonl_events, preview_value
    gaps, seen, duplicates, timestamps = Counter(), set(), 0, []
    attempts, tools, bindings, queues = {}, {}, {}, {}
    event_counts = Counter()
    trace = root / "_trace/live-events.jsonl"
    for event in iter_jsonl_events(trace):
        identity = event.get("event_id")
        if identity and identity in seen:
            duplicates += 1
            continue
        if identity:
            seen.add(identity)
        else:
            gaps["events_without_event_id"] += 1
        kind, payload = event.get("event_type", ""), _object(event.get("payload"))
        event_counts[kind] += 1
        role = preview_value(event["role"]) if isinstance(event.get("role"), str) and event["role"] else "unknown"
        agent = str(event.get("agent_id") or "")
        try:
            timestamp = datetime.fromisoformat(str(event.get("timestamp", "")).replace("Z", "+00:00"))
            if timestamp.tzinfo is not None:
                timestamps.append(timestamp.timestamp())
        except ValueError:
            gaps["invalid_timestamps"] += 1
        if kind in {"agent_started", "agent_context"}:
            key = (agent, payload.get("task_id"), payload.get("attempt"))
            if key[1] is not None and key[2] is not None:
                binding = bindings.setdefault(key, {"agent_id": agent, "role": role, "task_id": key[1], "attempt": key[2], "repair": None})
                for field in ("pages", "page_uids", "revision_id", "base_deck_version"):
                    if field in payload:
                        binding[field] = payload[field]
                for message in payload.get("messages", []):
                    packet = _object(_object(message).get("content")).get("owner_packet", {})
                    if (isinstance(packet, dict) and packet.get("owner_id") == agent
                            and packet.get("task_id") == key[1] and packet.get("attempt") == key[2]
                            and isinstance(packet.get("repair"), bool)):
                        binding["repair"] = packet["repair"]
        attempt_id = payload.get("attempt_id")
        if kind.startswith("resource_wait_") and attempt_id:
            queues[attempt_id] = {"state": kind, "queue_wait_s": payload.get("queue_wait_s"), "role": role}
        if kind in {"provider_started", "provider_succeeded", "provider_failed", "provider_raw_response"}:
            if not attempt_id:
                gaps["provider_events_without_attempt_id"] += 1
                continue
            row = attempts.setdefault(attempt_id, {"attempt_id": attempt_id, "role": role})
            if kind == "provider_started":
                row["started"] = True
                row.update({key: payload[key] for key in ("api_format", "queue_wait_s") if key in payload})
            elif kind in {"provider_succeeded", "provider_failed"}:
                row.update(state=kind.removeprefix("provider_"), elapsed_s=payload.get("elapsed_s"))
                row["diagnostic_output_tokens"] = _object(payload.get("response_diagnostics")).get("output_tokens")
            elif payload.get("raw_ref"):
                row["raw_ref"] = str(payload["raw_ref"])
        if kind in {"tool_use", "tool_result"}:
            tool_id = payload.get("tool_use_id")
            if not tool_id:
                gaps["tools_without_tool_use_id"] += 1
                continue
            row = tools.setdefault((agent, tool_id), {"role": role, "name": payload.get("name", "unknown")})
            row["use" if kind == "tool_use" else "result"] = payload
    for key, attempt in attempts.items():
        if "queue_wait_s" not in attempt:
            attempt["queue_wait_s"] = queues.get(key, {}).get("queue_wait_s")
        attempt["usage"] = _attempt_usage(root, attempt, gaps)
    pages, role_tools = defaultdict(Counter), defaultdict(Counter)
    hashes = defaultdict(set)
    for tool in tools.values():
        use, end = tool.get("use", {}), tool.get("result", {})
        counts = role_tools[tool["role"]]
        counts["calls" if use else "orphan_results"] += 1
        if use:
            counts["succeeded" if end.get("ok") is True else "failed" if end else "open"] += 1
        result = _object(end.get("result"))
        action, numbers, receipts = _tool_pages(tool["name"], _object(use.get("arguments")), result)
        if action and use:
            counts[action + "_calls"] += 1
            if end.get("ok") is True:
                counts[action + "_successful_calls"] += 1
        if action and not numbers:
            gaps["page_calls_without_page_binding"] += 1
        for number in numbers if action else []:
            if use:
                pages[number][action + "_calls"] += 1
            if use and end.get("ok") is True:
                pages[number][action + "_successful_calls"] += 1
                if action == "inspect" and result.get("inspection_cached") is True:
                    pages[number]["inspection_cache_hits"] += 1
            pairs = {(row.get("html_sha256"), row.get("png_sha256")) for row in receipts
                     if row.get("page") == number and row.get("html_sha256") and row.get("png_sha256")}
            if pairs:
                pages[number]["render_receipt_observations"] += 1
                hashes[number].update(pairs)
    host = _cost_json(root, "_state/skill-runtime.json", gaps)
    result = _cost_json(root, "result.json", gaps)
    training = _cost_json(root, "_trace/training/training-manifest.json", gaps)
    admission = training.get("training_admission", result.get("training_admission", "unknown"))
    if admission not in {"accepted", "rejected", "pending", "not_applicable"}:
        admission = "unknown"
    terminal_usage = _object(_object(host.get("outcome")).get("usage"))
    tail_complete = None
    if trace.is_file():
        with trace.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell():
                handle.seek(-1, os.SEEK_END)
                tail_complete = handle.read(1) == b"\n"
            else:
                tail_complete = True
    roles = sorted({row["role"] for row in attempts.values()} | set(role_tools))
    return {"workspace": str(root), "generation_status": result.get("status", host.get("status", "unknown")),
        "training_admission": admission, "training_admission_source": "_trace/training/training-manifest.json" if training.get("training_admission") else "result.json" if result.get("training_admission") else None,
        "trace_present": trace.is_file(), "trace_tail_complete": tail_complete,
        "events": sum(event_counts.values()), "duplicate_event_ids_ignored": duplicates,
        "trace_observed_span_s": round(max(timestamps) - min(timestamps), 6) if timestamps else None,
        "provider": _provider_costs(list(attempts.values())), "provider_failover_events": event_counts["provider_failover"],
        "queue_only": {"attempts": sum(key not in attempts for key in queues),
            "wait_s": _measure([row.get("queue_wait_s") for key, row in queues.items() if key not in attempts])},
        "roles": {role: {"provider": _provider_costs([row for row in attempts.values() if row["role"] == role]),
            "tools": dict(role_tools[role]), "tool_duration_ms": _measure([row.get("result", {}).get("duration_ms")
                for row in tools.values() if row["role"] == role])} for role in roles},
        "pages": {str(page): {**{key: values[key] for key in _PAGE_COUNTS}, "distinct_html_png_pairs": len(hashes[page]), "rasterizations": None}
                  for page, values in sorted(pages.items())}, "owner_attempt_bindings": list(bindings.values()),
        "terminal_outcome_usage": {"source": "_state/skill-runtime.json#/outcome/usage", "not_added_to_attempt_totals": True,
            "resume_coverage": "unknown", **{key: _number(terminal_usage.get(key)) for key in _TOKEN_FIELDS}},
        "gaps": dict(gaps)}


def summarize_trace_costs(workspaces):
    """只读离线派生；输入多个工作区即构成明确分母，不扫描或初始化工作区。"""
    from runtime.live_trace import preview_value
    roots = list(dict.fromkeys(Path(path).expanduser().resolve() for path in workspaces))
    rows = [_workspace_costs(root) for root in roots]
    denominators = {state: sum(row["training_admission"] == state for row in rows)
                    for state in ("accepted", "rejected", "pending", "not_applicable", "unknown")}
    return preview_value({"workspaces": rows, "denominator": {"workspaces": len(roots), **denominators},
        "limitations": ["No monetary pricing, first-draft closure, quality improvement, or per-issue repair cost is inferred.",
            "Provider/tool sums overlap under concurrency; trace_observed_span_s is not whole-task wall time.",
            "Tokens are recorded partial sums with coverage, grouped by API input/cache semantics; missing is not zero.",
            "Page counters are tool participations: one group call may count on multiple pages. Receipts may be cached; inspect success is not inspection closure.",
            "Only observed page ordinals appear; structural revisions may renumber them. Recorded owner bindings retain page_uids where available.",
            "Admission denominators describe latest recorded snapshots, which may predate an active resume; generation status is reported separately.",
            "Only complete JSONL objects are read; malformed rows and incomplete tail lines are ignored without changing the source.",
            "Owner attempt is not assumed to be a repair; only an explicitly matching owner_packet supplies repair=true/false."]},
        max_depth=20, max_items=sys.maxsize)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Read-only cost summary from recorded MURAL traces; JSON to stdout only")
    parser.add_argument("--stats", nargs="+", required=True, metavar="WORKSPACE")
    arguments = parser.parse_args(argv)
    print(json.dumps(summarize_trace_costs(arguments.stats), ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
