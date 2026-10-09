"""Turn the engine's on-disk run_dir into a friendly progress snapshot.

The engine writes deck.md, slides/, renders/ and each Agent's messages/tool log in
real time.  The runner subprocess also streams compact activity lines to
data/jobs/<id>.log.  Filesystem artifacts drive phase/page progress; the durable
per-Agent trace replaces the compact log preview as soon as one model response
has landed, so the UI can show complete in-progress prose without waiting for
the role to stop.
"""
import glob
import copy
import hashlib
import io
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .workspace_layout import WorkspaceLayout

# runner log lines look like:  [u2_d9/slide_03] 🔧 read({"path": ...})
_ACT_RE = re.compile(r"\[[^/\]]+/([^\]]+)\] 🔧 (\w+)\(")


def _planned_pages(run_dir: str) -> list[dict]:
    try:
        return WorkspaceLayout.load(run_dir).plan_pages()
    except (OSError, ValueError):
        return []


def _first_existing(run_dir: str, *relatives: str) -> str | None:
    """Return the first existing artifact among explicitly named candidates."""
    for relative in relatives:
        path = os.path.join(run_dir, relative)
        if os.path.isfile(path):
            return path
    return None

# Image agents sometimes copy a generated hash-named image to a readable
# semantic filename.  Both paths are useful to the deck, but the activity
# gallery should show the visual only once.  Cache content hashes by immutable
# stat identity so polling livefeed does not repeatedly read large PNG files.
_ASSET_DIGEST_CACHE = {}

_AGENT_TRACE_SOURCES = {
    "messages": "messages.json",
    "tools": "tool_log.json",
    "config": "config.json",
    "full_reasoning": "reasoning.jsonl",
    "summary": "reasoning.jsonl",
    "system_prompt": "system_prompt.md",
    "provider_attempts": "events.jsonl",
    "events": "events.jsonl",
}
_AGENT_TRACE_MAX_BYTES = 16 * 1024 * 1024
_LIVE_EVENT_SCHEMA = "mural.live-event.v1"
_LIVE_EVENT_TYPES = {
    "agent_started", "agent_completed", "agent_failed",
    "agent_context_compacted", "agent_context_compaction_failed",
    "agent_segment_rolled_over", "agent_visual_context_released", "agent_turn_completed",
    "agent_empty_response_recovered", "agent_empty_tool_call_recovered", "agent_no_action_response",
    "agent_repeated_evidence_warning",
    "language_drift",
    "assistant_analysis_summary", "assistant_message",
    "assistant_analysis_summary_delta", "assistant_message_delta",
    "tool_use", "tool_result", "tool_output",
    "provider_started", "provider_succeeded", "provider_failed",
    "provider_failover", "agent_context", "assistant_reasoning", "assistant_reasoning_delta",
    "provider_raw_request", "provider_raw_response", "trace_projection_failed",
    "resource_wait_started", "resource_wait_completed", "resource_wait_failed",
    "critic_started", "critic_reused", "critic_failed", "critic_completed",
    "content_qc_started", "content_qc_completed", "content_qc_failed",
    "attachment_parse_queued", "attachment_parse_started", "attachment_parse_finished",
    "mineru_started", "mineru_failed", "mineru_failover", "mineru_succeeded",
    "deck_completion_checked",
    "slide_preparation_completed", "slide_preparation_failed", "image_handoff_ready",
}


_REQUEST_KIND_DISPLAY = {
    "hermes_main": ("主 Agent 工作轮", "Orchestrator 或 Owner 的普通模型工作请求；hermes_main 是兼容保留的内部日志名。"),
    "critic_visual": ("独立视觉 Critic", "外部视觉复核请求；只检查对应像素和用途，不是 Owner 自述。"),
    "context_compaction": ("上下文压缩摘要", "Harness 的内部交接摘要请求；不执行创作工具，不代表任务输出。"),
    "phase6_provider_canary": ("Provider 恢复探针", "运行时恢复/故障转移的协议探针。"),
}


def _request_kind_public(payload):
    kind = str((payload or {}).get("request_kind") or "")
    label = str((payload or {}).get("request_kind_label") or "")
    note = str((payload or {}).get("request_kind_note") or "")
    if kind and (not label or not note):
        fallback_label, fallback_note = _REQUEST_KIND_DISPLAY.get(kind, (kind, "内部请求分类；按原始 request_kind 保留以便调试。"))
        label = label or fallback_label
        note = note or fallback_note
    return kind, label, note


def _asset_digest(path, stat):
    key = (path, stat.st_mtime_ns, stat.st_size)
    digest = _ASSET_DIGEST_CACHE.get(key)
    if digest:
        return digest
    sha = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            sha.update(chunk)
    digest = sha.hexdigest()
    if len(_ASSET_DIGEST_CACHE) >= 4096:
        _ASSET_DIGEST_CACHE.clear()
    _ASSET_DIGEST_CACHE[key] = digest
    return digest


def _slide_nums(pattern):
    nums = []
    for p in glob.glob(pattern):
        b = os.path.basename(p)
        try:
            nums.append(int(b.split("_")[1].split(".")[0]))
        except (IndexError, ValueError):
            pass
    return sorted(set(nums))


def _last_activity(log_path):
    """Last tool call from the runner log: {'agent': 'slide_03', 'tool': 'read'}."""
    if not log_path or not os.path.exists(log_path):
        return None
    try:
        size = os.path.getsize(log_path)
        with open(log_path, "rb") as f:
            f.seek(max(0, size - 8192))
            chunk = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return None
    m = None
    for m in _ACT_RE.finditer(chunk):
        pass
    return {"agent": _canonical_agent_key(m.group(1)), "tool": m.group(2)} if m else None


def _shared_v1_runtime(run_dir):
    """Project the shared-runtime v1 state files into the existing WebUI shape."""
    state_root = os.path.join(run_dir, "_state")
    try:
        with open(os.path.join(state_root, "orchestration.json"), encoding="utf-8") as source:
            orchestration = json.load(source)
        with open(os.path.join(state_root, "owners.json"), encoding="utf-8") as source:
            owners = json.load(source)
    except (OSError, ValueError, TypeError):
        return {}
    if (
        not isinstance(orchestration, dict)
        or orchestration.get("schema") not in {"mural.orchestration.v1", "mural.orchestration.v2"}
        or not isinstance(owners, dict)
        or owners.get("schema") != "mural.owner-registry.v2"
    ):
        return {}
    task_map = orchestration.get("tasks") or {}
    owner_map = owners.get("owners") or {}
    if not all(isinstance(item, dict) for item in (task_map, owner_map)):
        return {}
    live_owners, _ = _shared_v1_live_owners(run_dir)
    tasks = [{**task, "agent_key": live_owners.get(task.get("owner_id"), {}).get("key", "")}
             for task in task_map.values() if isinstance(task, dict)]
    states = [task.get("status") for task in tasks]
    active = [
        item for item in owner_map.values()
        if isinstance(item, dict)
        and str(item.get("status") or "") == "running"
    ]
    role_stage = {
        "material": "researching",
        "research": "researching",
        "image": "designing",
        "slide": "rendering",
    }
    stage = next(
        (role_stage[role] for item in active if (role := str(item.get("role") or "")) in role_stage),
        None,
    )
    activity = None
    tool_events = os.path.join(run_dir, "_trace", "tool-events.jsonl")
    try:
        with open(tool_events, "rb") as source:
            source.seek(max(0, os.path.getsize(tool_events) - 131072))
            lines = source.read().decode("utf-8", "replace").splitlines()
        for raw in reversed(lines):
            event = json.loads(raw)
            if isinstance(event, dict) and event.get("event_type") == "tool_use":
                activity = {
                    "agent": str(event.get("owner_id") or event.get("role") or ""),
                    "tool": str(event.get("tool_name") or ""),
                }
                break
    except (OSError, ValueError, TypeError):
        pass
    return {
        "schema": "mural.webui-runtime-projection.v1",
        "deck_timeline": orchestration.get("events") or [],
        "tasks": tasks,
        "state_counts": {
            "active": states.count("running"),
            "waiting": sum(status in {"queued", "waiting_dependency", "pending"} for status in states),
            "blocked": sum(status in {"blocked", "interrupted", "waiting"} for status in states),
            "terminal": sum(status in {"succeeded", "completed", "failed", "skipped", "cancelled"} for status in states),
        },
        "issues": {},
        "artifacts": {},
        "active_workers": len(active),
        "task_stage": stage,
        "activity": activity,
    }


def _attachment_parse_payload(run_dir):
    """只投影当前原件和解析回执；解析完成不代表 Research 阅读或语义核验。"""
    manifest = _read_trace_json(os.path.join(run_dir or "", "_trace", "attachment-manifest.json"), {})
    active = set(manifest.get("active_attachment_ids") or [])
    out = []
    for record in manifest.get("records") or []:
        if active and record.get("attachment_id") not in active:
            continue
        status = record.get("parse_status")
        if status not in {"queued", "processing", "completed", "partial", "failed"}:
            continue
        parse = record.get("parse") or {}
        receipt = (parse.get("results", {}).get(parse.get("current"), {}) or {}).get("receipt") or {}
        item = {
            "attachment_id": record.get("attachment_id"),
            "source_sha256": record.get("sha256"),
            "name": record.get("name"),
            "status": status,
        }
        if receipt.get("source_sha256") == record.get("sha256") and receipt.get("status") == status:
            item.update(cached=receipt.get("cached") is True,
                        unresolved_count=len(receipt.get("unresolved_items") or []))
        out.append(item)
    return out


def snapshot(run_dir, seed=None, status=None, started_at=None, finished_at=None, log_path=None):
    rd = run_dir
    try:
        layout = WorkspaceLayout.load(rd)
        with open(os.path.join(rd, "_state", "workspace.json"), encoding="utf-8") as source:
            page_state = json.load(source).get("pages") or {}
    except (ValueError, OSError, TypeError):
        return {"status": status, "slides_total": (seed or {}).get("slide_count"), "slides_authored": 0,
                "slides_rendered": 0, "rendered": [], "workspace_error": "unsupported_workspace_layout",
                "task_stage": "preparing", "attachment_parse": _attachment_parse_payload(rd)}
    shared_v1 = _shared_v1_runtime(rd)
    task_pack = {}
    try:
        with open(os.path.join(rd, "task_pack.json"), encoding="utf-8") as source:
            task_pack = json.load(source)
    except (OSError, ValueError, TypeError):
        pass
    output = (task_pack.get("choices") or {}).get("output") or (seed or {}).get("ppt_output")
    existing_html = _slide_nums(str(layout.path("slides") / "slide_*.html"))
    authored = []
    for number in existing_html:
        record = page_state.get(str(number), {})
        expected = record.get("authored_html_sha256")
        if not expected or record.get("retired"):
            continue
        try:
            with layout.path("slide_pattern", page=number).open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
        except (OSError, ValueError):
            continue
        if digest == expected:
            authored.append(number)
    rendered = _slide_nums(str(layout.path("renders") / "slide_*.png"))
    # 每页渲染文件的毫秒级 mtime：页面被自检快速重渲时，
    # 秒级时间戳会把同一秒内的真实修改误判为未变。
    rtimes = {}
    for n in rendered:
        try:
            image_path = str(layout.path("renders") / f"slide_{n:02d}.png")
            stat = os.stat(image_path)
            rtimes[str(n)] = stat.st_mtime_ns // 1_000_000
        except OSError:
            pass
    atimes = {}
    for n in authored:
        try:
            stat = os.stat(layout.path("slides") / f"slide_{n:02d}.html")
            atimes[str(n)] = stat.st_mtime_ns // 1_000_000
        except OSError:
            pass
    workers = max(
        len(glob.glob(os.path.join(rd, "_trace", "subagents", "slide_*"))),
        int(shared_v1.get("active_workers") or 0),
    )

    total = len(_planned_pages(rd))
    if task_pack:
        try:
            total = int((task_pack.get("params") or {}).get("page_count") or total)
        except (TypeError, ValueError):
            pass
    if not total and seed and seed.get("slide_count"):
        try:
            total = int(seed["slide_count"])
        except (TypeError, ValueError):
            total = 0

    prog = {
        "status": status,
        "slides_total": total or None,
        "slides_authored": len(authored),
        "slides_scaffolded": len(set(existing_html) - set(authored)),
        "slides_rendered": len(rendered),
        "authored": authored,
        "rendered": rendered,
        "atimes": atimes,
        "rtimes": rtimes,
        "workers": workers,
        "activity": _last_activity(log_path) or shared_v1.get("activity"),
        "elapsed_s": ((finished_at or int(time.time())) - started_at) if started_at else None,
        "error": None,
        "ppt_output": output or "static_html",
        "task_stage": (
            (task_pack.get("state") or {}).get("current_stage")
            or shared_v1.get("task_stage")
        ),
        "completed_stages": (task_pack.get("state") or {}).get("completed_stages") or [],
        "attachment_parse": _attachment_parse_payload(rd),
    }
    try:
        with open(
            os.path.join(rd, "_trace", "runtime-summary.json"),
            encoding="utf-8",
        ) as source:
            runtime_summary = json.load(source)
    except (OSError, ValueError, TypeError):
        runtime_summary = {}
    if not runtime_summary and shared_v1:
        runtime_summary = shared_v1
    try:
        with open(
            os.path.join(rd, "_trace", "independent-visual-qc.json"),
            encoding="utf-8",
        ) as source:
            visual_qc = json.load(source)
    except (OSError, ValueError, TypeError):
        visual_qc = {}
    latest_visual_qc = (
        visual_qc.get("latest_deck_qc")
        if isinstance(visual_qc.get("latest_deck_qc"), dict)
        else {}
    )
    prog["mural_runtime"] = {
        "schema": runtime_summary.get("schema"),
        "deck_timeline": runtime_summary.get("deck_timeline"),
        "tasks": shared_v1.get("tasks", runtime_summary.get("tasks") or []),
        "state_counts": runtime_summary.get("state_counts") or {},
        "issues": runtime_summary.get("issues") or {},
        "artifacts": runtime_summary.get("artifacts") or {},
        "provider_queue_observability": runtime_summary.get(
            "provider_queue_observability"
        ),
        "visual_qc": latest_visual_qc,
    }
    try:
        with open(os.path.join(rd, "result.json"), encoding="utf-8") as source:
            final_result = json.load(source)
    except (OSError, ValueError, TypeError):
        final_result = {}
    prog["artifact_status"] = final_result.get("artifact_status")
    prog["training_admission"] = final_result.get("training_admission")
    prog["training_rejection_reasons"] = final_result.get("training_rejection_reasons") or []
    prog["qc_status"] = (
        final_result.get("qc_status") or latest_visual_qc.get("qc_status")
    )
    prog["phase"] = _phase(prog, rd, status, task_pack=task_pack)
    return prog


def _phase(prog, rd, status, task_pack=None):
    if status == "completed":
        return "done"
    if status in (
        "failed", "rejected", "interrupted", "stopped", "paused_recoverable",
    ):
        return "failed"
    if status == "queued" or not os.path.isdir(os.path.join(rd, "_trace")):
        return "starting"
    stage = str(((task_pack or {}).get("state") or {}).get("current_stage") or "")
    if stage:
        if stage == "entry" or "research" in stage:
            return "researching"
        if stage == "story":
            return "planning"
        if stage.endswith(".plan"):
            return "designing"
        if any(token in stage for token in (".pages", ".build", ".content")):
            return "rendering"
        if any(token in stage for token in (".review", ".verify", ".final", ".deliver")):
            return "verifying"
    planned = len(_planned_pages(rd))
    if planned and prog["slides_rendered"] >= planned:
        return "verifying"
    if prog.get("task_stage") in {"researching", "designing", "rendering"}:
        return prog["task_stage"]
    if prog["workers"] or prog["slides_authored"] or prog["slides_rendered"]:
        return "rendering"
    if planned:
        return "delegating"
    act = prog.get("activity") or {}
    tool = act.get("tool")
    if tool in ("web_search", "web_fetch"):
        return "researching"
    if tool == "image_generate" or glob.glob(os.path.join(rd, "assets", "*")) \
       or os.path.exists(os.path.join(rd, "base.css")):
        return "designing"
    return "planning"


# ---- 实时输出流:解析 runner 日志,按 agent 分组 ----------------------------
# 旧格式:[u6_d14/slide_04_r2] 🔧 edit({"path": "slides/slide_04.html", ...
# 新格式:[09:01:28 + 706.0s] [u6_d14/orchestrator] [3] 💬 我先读取 skill 说明…
# 时间前缀由当前 clean Harness 输出；保持可选以兼容历史 deck 日志。
_TIMING_PREFIX_RE = r"(?:\[(\d{2}:\d{2}:\d{2}) \+\s*(\d+(?:\.\d+)?)s\]\s+)?"
_FEED_RE = re.compile(
    _TIMING_PREFIX_RE
    + r"\[[^/\]]+/([^\]]+)\] (?:🔧 (\w+)\((.*)|\[\d+\] 💬 (.*))"
)
_AGENT_EXIT_RE = re.compile(
    _TIMING_PREFIX_RE
    + r"\[[^/\]]+/([^\]]+)\]\s+轨迹已写\b.*?\bexit=([A-Za-z0-9_-]+)"
)
_HINT_RE = re.compile(r'"path"\s*:\s*"([^"]+)"')
_SLIDE_REF_RE = re.compile(r"(?:slide_|page_)0*(\d{1,3})(?=\D|$)", re.I)
_PAGES_ARG_RE = re.compile(r"--pages(?:=|[ \t]+)([0-9][0-9, \t]*)", re.I)


def _slide_group_agent_key(group_id):
    """Return one public key for equivalent plan/runtime group spellings."""
    suffix = re.sub(
        r"[^a-zA-Z0-9.]+",
        "-",
        str(group_id or "").strip(),
    ).strip("-").lower()
    return f"slide_group_{suffix or 'group'}"


def _agent_alias_from_task(task, fallback):
    """Recover a stable role label when a Harness trace was named child_NN."""
    text = str(task or "").strip()
    # Current grouped Skills include a durable assignment line such as
    # ``Slide Group roots [27,28]``.  Prefer it over the runner label: labels
    # have appeared as slide-group-roots, slide-roots and slide-g08 while the
    # production-group id in the plan remains stable.
    explicit_groups = re.findall(
        r"\bSlide\s+Group\s+([A-Za-z0-9._-]+)\s*\[\s*\d",
        text,
        re.I,
    )
    if explicit_groups:
        return _slide_group_agent_key(explicit_groups[-1])
    identity_group = re.search(
        r"(?:身份是|identity\s*(?:is|:))\s*slide[-_]group[-_]([A-Za-z0-9._-]+)",
        text,
        re.I,
    )
    if identity_group:
        return _slide_group_agent_key(identity_group.group(1))
    bracketed = re.search(
        r"\[\s*(research|material|image|slide|review)(?:[\s_-]+([^\]]+))?\s*\]",
        text,
        re.I,
    )
    if bracketed:
        role = bracketed.group(1).lower()
        suffix = re.sub(r"[^a-zA-Z0-9._-]+", "-", bracketed.group(2) or "").strip("-_").lower()
        if role == "slide":
            if suffix.isdigit():
                return f"slide_{int(suffix):02d}"
            return _slide_group_agent_key(suffix)
        if role == "review":
            return "review"
        return f"{role}_{suffix}" if suffix else role
    group = re.match(r"slide\s+group\s+([^\s:\[]+)", text, re.I)
    if group:
        return _slide_group_agent_key(group.group(1))
    single = re.match(r"slide\s+0*(\d{1,3})\b", text, re.I)
    if single:
        return f"slide_{int(single.group(1)):02d}"
    for role in ("research", "material", "image", "review"):
        if re.match(rf"(?:final\s+)?{role}\b", text, re.I):
            return role
    return str(fallback or "")


def _trace_agent_aliases(run_dir):
    aliases = {}
    trace_root = os.path.join(run_dir or "", "_trace", "subagents")
    for config_path in glob.glob(os.path.join(trace_root, "*", "config.json")):
        raw = os.path.basename(os.path.dirname(config_path))
        try:
            with open(config_path, encoding="utf-8", errors="replace") as source:
                config = json.load(source)
        except (OSError, ValueError, TypeError):
            continue
        role = str(config.get("role") or "").strip().lower()
        owner_group_id = str(config.get("owner_group_id") or "").strip()
        label = str(config.get("label") or raw).strip()
        if role == "slide" and owner_group_id:
            # The Harness has already resolved durable ownership.  Do not
            # reinterpret bracketed task prose such as ``[Image fallback
            # evidence]`` as an Agent identity.
            aliases[raw] = _slide_group_agent_key(owner_group_id)
        elif role == "slide" and re.match(r"slide[-_]group[-_]", label, re.I):
            suffix = re.sub(r"^slide[-_]group[-_]", "", label, flags=re.I)
            aliases[raw] = _slide_group_agent_key(suffix)
        else:
            aliases[raw] = _agent_alias_from_task(
                config.get("task") or config.get("goal") or "",
                label,
            )
    owners, _ = _shared_v1_live_owners(run_dir)
    aliases.update({owner_id: row["key"] for owner_id, row in owners.items()})
    return aliases


def _agent_trace_segments(run_dir, agent_key):
    """Resolve one public Agent key to its durable trace directories."""
    if not run_dir or not os.path.isdir(run_dir):
        return []
    aliases = _trace_agent_aliases(run_dir)
    configs = [os.path.join(run_dir, "_trace", "orchestrator", "config.json")]
    configs.extend(sorted(glob.glob(os.path.join(
        run_dir, "_trace", "subagents", "*", "config.json"
    ))))
    wanted = _canonical_agent_key(agent_key)
    segments = []
    for config_path in configs:
        if not os.path.isfile(config_path):
            continue
        config = _read_trace_json(config_path, {})
        if not isinstance(config, dict):
            continue
        role_dir = os.path.dirname(config_path)
        raw = os.path.basename(role_dir)
        role = str(config.get("role") or "").strip().lower()
        key = "orch" if role == "orchestrator" else _livefeed_agent_key(raw, aliases)
        if _canonical_agent_key(key) != wanted:
            continue
        available = []
        for source, filename in _AGENT_TRACE_SOURCES.items():
            path = os.path.join(role_dir, filename)
            if os.path.isfile(path):
                available.append({
                    "id": source,
                    "filename": filename,
                    "size": os.path.getsize(path),
                    "updated_at": _trace_iso(os.path.getmtime(path)),
                })
        started = config.get("started_at")
        segments.append({
            "id": raw,
            "key": key,
            "label": str(config.get("label") or raw),
            "role": role or key.split("_", 1)[0],
            "segment_id": config.get("segment_id"),
            "owner_action": str(config.get("owner_action") or "create"),
            "started_at": started,
            "finished_at": config.get("finished_at"),
            "exit_reason": str(config.get("exit_reason") or ""),
            "model": str(config.get("model") or ""),
            "requested_thinking": bool(
                config.get("requested_thinking", config.get("thinking", False))
            ),
            "effective_thinking": bool(
                config.get("effective_thinking", config.get("thinking", False))
            ),
            "requested_effort": str(config.get("requested_effort") or ""),
            "effective_effort": str(
                config.get("effective_effort", config.get("thinking_effort") or "")
            ),
            "thinking_effort": str(config.get("thinking_effort") or ""),
            "sources": available,
            "_role_dir": role_dir,
            "_sort": str(started or "") + f"|{os.path.getmtime(config_path):020.6f}",
        })
    segments.sort(key=lambda item: item["_sort"])
    return segments


def _agent_trace_message_times(role_dir, parsed, selected):
    """Project durable tool timestamps onto readable message cards when known."""
    records = parsed if isinstance(parsed, list) else None
    if records is None and isinstance(parsed, dict):
        records = next((parsed.get(key) for key in ("messages", "items")
                        if isinstance(parsed.get(key), list)), None)
    if records is None:
        return []

    tool_log = _read_trace_json(os.path.join(role_dir, "tool_log.json"), [])
    by_tool_id = {}
    by_turn = {}
    if isinstance(tool_log, list):
        for item in tool_log:
            if not isinstance(item, dict):
                continue
            timestamp = _trace_timestamp(item.get("time"))
            value = _trace_iso(timestamp) if timestamp else None
            if not value:
                continue
            tool_use_id = str(item.get("tool_use_id") or "").strip()
            if tool_use_id:
                by_tool_id[tool_use_id] = value
            try:
                turn = int(item.get("turn"))
            except (TypeError, ValueError):
                continue
            by_turn.setdefault(turn, value)

    started = _trace_timestamp(selected.get("started_at"))
    finished = _trace_timestamp(selected.get("finished_at"))
    started_iso = _trace_iso(started) if started else None
    finished_iso = _trace_iso(finished) if finished else None
    assistant_turn = -1
    result = []
    for index, message in enumerate(records):
        if not isinstance(message, dict):
            result.append(None)
            continue
        role = str(message.get("role") or "").lower()
        if role == "assistant":
            assistant_turn += 1
        content = message.get("content")
        blocks = content if isinstance(content, list) else []
        ids = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                ids.append(str(block.get("id") or ""))
            elif block.get("type") == "tool_result":
                ids.append(str(block.get("tool_use_id") or ""))
        candidates = [by_tool_id[item] for item in ids if item in by_tool_id]
        value = min(candidates) if candidates else None
        if value is None and role == "assistant":
            value = by_turn.get(assistant_turn)
        if value is None and index == 0:
            value = started_iso
        if value is None and index == len(records) - 1 and role == "assistant":
            value = finished_iso
        result.append(value)
    return result


def _live_events_by_agent(run_dir):
    """Read the unified v1 journal and bucket public rows by workflow Agent key.

    v1 no longer writes per-Agent ``_trace/<role>/messages.json`` directories.
    The only durable per-turn record is the append-only ``live-events.jsonl``
    stream.  Group its rows by the same stable key the workflow cards and live
    feed already use (:func:`_shared_v1_live_agent_key`) so a click on a node
    resolves to that node's events. Rows retain their recorded content; connection
    headers and native signatures are handled at their protocol boundaries.
    """
    path = os.path.join(run_dir or "", "_trace", "live-events.jsonl")
    owners, _page_agents = _shared_v1_live_owners(run_dir)
    if not os.path.isfile(path):
        return {}, {}, owners
    by_agent = {}
    agent_keys = {}
    seen_ids = set()
    try:
        with open(path, "rb") as source:
            for raw in source:
                if not raw.endswith(b"\n"):
                    continue  # skip a half-written trailing line
                try:
                    value = json.loads(raw)
                except (UnicodeDecodeError, TypeError, ValueError):
                    continue
                if not isinstance(value, dict) or value.get("schema") != _LIVE_EVENT_SCHEMA:
                    continue
                event_type = str(value.get("event_type") or "")
                if event_type not in _LIVE_EVENT_TYPES:
                    continue
                event_id = str(value.get("event_id") or "")
                if event_id and event_id in seen_ids:
                    continue
                if event_id:
                    seen_ids.add(event_id)
                public = value
                if not isinstance(public, dict):
                    continue
                agent_id = public.get("agent_id")
                if event_type in {"agent_context", "agent_started"} or agent_id not in agent_keys:
                    agent_keys[agent_id] = _shared_v1_live_agent_key(public, owners)
                key = agent_keys[agent_id]
                by_agent.setdefault(key, []).append(public)
    except OSError:
        return {}, {}, owners
    for events in by_agent.values():
        events.sort(key=lambda item: int(item.get("sequence") or 0))
    return by_agent, seen_ids, owners


def _live_events_agent_segments(events, owners, key):
    """Describe each Segment an Agent ran, matching the old segment-card shape."""
    segments = {}
    order = []
    for event in events:
        segment_id = str(event.get("segment_id") or "")
        record = segments.get(segment_id)
        if record is None:
            role = str(event.get("role") or "").strip().lower()
            record = {
                "id": segment_id,
                "key": key,
                "label": str(event.get("agent_id") or key),
                "role": role or (key.split("_", 1)[0] if key else ""),
                "segment_id": segment_id,
                "owner_action": "create",
                "started_at": None,
                "finished_at": None,
                "exit_reason": "",
                "model": "",
                "requested_thinking": None,
                "effective_thinking": None,
                "requested_effort": "",
                "effective_effort": "",
                "thinking_effort": "",
                "configured_effort": "",
                "sent_reasoning_parameters": {},
                "server_confirmed_effort": None,
                # Only the projected conversation is available for v1 journals.
                "sources": [{"id": name, "filename": filename} for name, filename in _AGENT_TRACE_SOURCES.items()],
                "_first_seq": int(event.get("sequence") or 0),
            }
            segments[segment_id] = record
            order.append(segment_id)
        timestamp = str(event.get("timestamp") or "")
        event_type = str(event.get("event_type") or "")
        if event_type == "provider_started":
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            record["model"] = str(payload.get("model") or record["model"])
            if isinstance(payload.get("effective_thinking"), bool):
                record["effective_thinking"] = payload["effective_thinking"]
                record["effective_effort"] = str(payload.get("effective_effort") or "")
                record["thinking_effort"] = record["effective_effort"]
            record["configured_effort"] = str(payload.get("configured_effort") or "")
            record["sent_reasoning_parameters"] = payload.get("sent_reasoning_parameters", {})
            record["server_confirmed_effort"] = payload.get("server_confirmed_effort")
        if timestamp and record["started_at"] is None:
            record["started_at"] = timestamp
        if event_type in {"agent_started", "content_qc_started"} and timestamp:
            record["started_at"] = timestamp
        elif event_type in {"agent_completed", "content_qc_completed"} and timestamp:
            record["finished_at"] = timestamp
        elif event_type in {"agent_failed", "content_qc_failed"}:
            record["finished_at"] = timestamp or record["finished_at"]
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            record["exit_reason"] = str(payload.get("error_type") or payload.get("incomplete_reason") or "failed")
            record["failure"] = payload
    ordered = sorted(
        (segments[segment_id] for segment_id in order),
        key=lambda item: item["_first_seq"],
    )
    # An Owner that resumes runs a second Segment; label those as continuations
    # so the Segment selector reads the same as the old per-Agent traces.
    for index, record in enumerate(ordered):
        if index and record["label"] == ordered[index - 1]["label"]:
            record["owner_action"] = "reuse_owner"
    return ordered


def _live_events_messages(events, response_language=None):
    """Project one Agent's ordered journal rows into ``messages.json`` shape.

    The frontend renders a list of ``{role, content}`` records where ``content``
    is a list of blocks (``text`` / ``thinking`` with a ``summary`` field /
    ``tool_use`` / ``tool_result``).  Build assistant turns by ``turn`` within a
    Segment and emit paired ``tool_result`` user messages in sequence order so a
    call and its result render adjacently.  Returns ``(messages, times)`` where
    ``times[i]`` is the ISO timestamp of ``messages[i]`` for the message rail.
    """
    # 预览只投影最后一份累计文本；正式消息按 attempt 取代预览，日志本身不改写。
    latest = {}
    completed = set()
    interrupted = set()
    interrupted_segments = set()
    resource_waits = {}
    for event in events:
        kind = str(event.get("event_type") or "")
        payload = event.get("payload") or {}
        attempt = str(payload.get("attempt_id") or "")
        if kind in {"assistant_message_delta", "assistant_analysis_summary_delta"}:
            latest[(attempt, kind[:-6])] = event.get("event_id")
        elif kind in {"assistant_message", "assistant_analysis_summary"}:
            completed.add((attempt, kind))
        elif kind == "provider_failed":
            interrupted.add(attempt)
        elif kind == "agent_failed":
            interrupted_segments.add(str(event.get("segment_id") or ""))
        elif kind.startswith("resource_wait_"):
            resource_waits[attempt] = event.get("event_id")
    messages = []
    times = []
    turns = {}  # (segment_id, turn) -> index into messages
    outputs = {str((event.get("payload") or {}).get("tool_use_id") or "")
               for event in events if event.get("event_type") == "tool_output"}
    for event in events:
        event_type = str(event.get("event_type") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        preview = event_type in {"assistant_message_delta", "assistant_analysis_summary_delta"}
        preview_meta = {}
        if preview:
            event_type = event_type[:-6]
            attempt = str(payload.get("attempt_id") or "")
            key = (attempt, event_type)
            if key in completed or latest.get(key) != event.get("event_id"):
                continue
            preview_meta = {
                "partial": True,
                "interrupted": attempt in interrupted or str(event.get("segment_id") or "") in interrupted_segments,
            }
        segment_id = str(event.get("segment_id") or "")
        timestamp = str(event.get("timestamp") or "") or None
        try:
            turn = int(event.get("turn") or 0)
        except (TypeError, ValueError):
            turn = 0
        turn_key = (segment_id, turn)

        def assistant():
            index = turns.get(turn_key)
            if index is None:
                index = len(messages)
                turns[turn_key] = index
                messages.append({"role": "assistant", "content": []})
                times.append(timestamp)
            elif timestamp and not times[index]:
                times[index] = timestamp
            return messages[index]

        if event_type.startswith("resource_wait_"):
            if resource_waits.get(str(payload.get("attempt_id") or "")) != event.get("event_id"):
                continue
            if event_type == "resource_wait_completed":
                continue
            status = {
                "resource_wait_started": "等待本地模型资源槽；尚未请求 Provider",
                "resource_wait_failed": "本地模型资源排队超时；未发送请求，也未切换 Provider",
            }[event_type]
            if event_type == "resource_wait_started" and str(event.get("segment_id") or "") in interrupted_segments:
                status = "本地模型资源排队期间已中断；未请求 Provider"
            duration = payload.get("queue_wait_s")
            if isinstance(duration, (int, float)):
                status += f" · 排队 {duration:g}s"
            messages.append({"role": "system", "content": [{"type": "text", "text": status}]})
            times.append(timestamp)
        elif event_type == "assistant_message":
            text = _visible_text_for_language(
                str(payload.get("message") or ""), response_language
            )
            if text:
                assistant()["content"].append({"type": "text", "text": text, **preview_meta})
        elif event_type == "assistant_analysis_summary":
            summary = str(payload.get("summary") or "")
            if summary:
                # The frontend renders ``thinking`` blocks via ``block.summary``.
                # Never carry raw thinking/reasoning/signature here.
                assistant()["content"].append({"type": "thinking", "summary": summary, **preview_meta})
        elif event_type == "tool_use":
            arguments = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
            assistant()["content"].append({
                "type": "tool_use",
                "id": str(payload.get("tool_use_id") or ""),
                "name": str(payload.get("name") or "tool"),
                "input": arguments,
                "has_diagnostics": str(payload.get("tool_use_id") or "") in outputs,
            })
        elif event_type == "tool_result":
            messages.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": str(payload.get("tool_use_id") or ""),
                "content": payload.get("result"),
                "is_error": payload.get("ok") is False,
                "duration_ms": payload.get("duration_ms"),
                "error_code": payload.get("error_code", ""),
            }]})
            times.append(timestamp)
        elif (event_type == "provider_started"
              and str(payload.get("request_kind") or "") == "context_compaction"):
            messages.append({"role": "system", "content": [{
                "type": "context_compaction",
                "phase": "started",
                "model": str(payload.get("model") or ""),
                "actual_max_tokens": payload.get("actual_max_tokens"),
                "attempt_id": str(payload.get("attempt_id") or ""),
            }]})
            times.append(timestamp)
        elif event_type == "agent_context_compacted":
            messages.append({"role": "system", "content": [{
                "type": "context_compaction",
                "phase": "completed",
                **{key: payload[key] for key in (
                    "estimated_tokens", "input_budget", "tokens_before", "tokens_after",
                    "compact_target_tokens", "target_met", "pressure", "reason",
                ) if key in payload},
            }]})
            times.append(timestamp)
        elif event_type == "agent_context_compaction_failed":
            messages.append({"role": "system", "content": [{
                "type": "context_compaction",
                "phase": "failed",
                **{key: payload[key] for key in (
                    "error_type", "error_summary", "recoverable", "checkpoint_retained",
                    "recovery_action", "estimated_tokens", "input_budget",
                ) if key in payload},
            }]})
            times.append(timestamp)
        # agent_started/completed/failed and provider_* carry no readable
        # message body; their lifecycle already drives the Segment cards.
    return messages, times


def tool_output_payload(run_dir, tool_use_id, *, cursor=0, limit=100):
    """仅按真实事务 ID 读取脱敏诊断；不接受客户端文件路径。"""
    WorkspaceLayout.load(run_dir)
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", tool_use_id):
        raise ValueError("invalid tool_use_id")
    by_agent, _seen, _owners = _live_events_by_agent(run_dir)
    events = [event for rows in by_agent.values() for event in rows
              if (event.get("payload") or {}).get("tool_use_id") == tool_use_id]
    use = next((row for row in events if row["event_type"] == "tool_use"), None)
    if use is None:
        raise FileNotFoundError("tool transaction not found")
    result = next((row["payload"] for row in events if row["event_type"] == "tool_result"), None)
    complete = result is not None
    filename = hashlib.sha256(tool_use_id.encode()).hexdigest() + ".jsonl"
    root = os.path.realpath(run_dir)
    target = os.path.join(root, "_trace", "tool-output", filename)
    if os.path.realpath(target) != target:
        raise ValueError("diagnostic path escapes workspace")
    position = max(0, int(cursor))
    rows = []
    incomplete_tail = False
    size = os.path.getsize(target) if os.path.isfile(target) else 0
    if position > size:
        raise ValueError("diagnostic cursor exceeds saved output")
    if size:
        with open(target, "rb") as handle:
            if position:
                handle.seek(position - 1)
                if handle.read(1) != b"\n":
                    raise ValueError("diagnostic cursor must be a record boundary")
            handle.seek(position)
            batch_start = position
            for _ in range(min(max(1, int(limit)), 200)):
                raw = handle.readline(512 * 1024)
                if not raw.endswith(b"\n"):
                    incomplete_tail = bool(raw)
                    break
                rows.append(json.loads(raw))
                position = handle.tell()
                if position - batch_start >= 256 * 1024:
                    break
    return {"tool_use_id": tool_use_id, "agent_id": use["agent_id"], "role": use["role"],
            "task_id": use["payload"].get("task_id", ""), "attempt": use["payload"].get("attempt", 0),
            "cursor": max(0, int(cursor)), "next_cursor": position, "size": size, "rows": rows,
            "has_more": position < size and not incomplete_tail, "incomplete_tail": incomplete_tail, "complete": complete,
            "ok": result.get("ok") if result else None, "error_code": result.get("error_code", "") if result else "",
            "empty_state": "尚无诊断输出" if not size else ""}


def _workflow_trace_image(run_dir, digest):
    if not re.fullmatch(r"[a-f0-9]{64}", str(digest or "")):
        return ""
    relatives = [f"_trace/training/blobs/{digest}.{ext}" for ext in ("png", "jpg", "jpeg", "webp")]
    relatives.append(f"_trace/raw/blobs/{digest}")
    root = os.path.realpath(run_dir)
    for relative in relatives:
        path = os.path.realpath(os.path.join(root, relative))
        if path != os.path.join(root, relative):
            continue
        try:
            if _asset_digest(path, os.stat(path)) == digest:
                return relative
        except OSError:
            continue
    return ""


def workflow_trace_image(run_dir, digest):
    """只开放公开画面回执引用的图片字节，不开放 Raw Trace 文件下载。"""
    relative = _workflow_trace_image(run_dir, digest)
    if not relative:
        raise FileNotFoundError("historical image not found")
    referenced = False
    with open(os.path.join(run_dir, "_trace/live-events.jsonl"), "rb") as journal:
        for raw in journal:
            if not raw.endswith(b"\n") or digest.encode("ascii") not in raw:
                continue
            try:
                event = json.loads(raw)
                if event.get("schema") != _LIVE_EVENT_SCHEMA:
                    continue
                payload = event.get("payload") or {}
                if event.get("event_type") == "tool_result" and payload.get("name") in {
                    "inspect", "render", "inspect_group", "render_group", "run_command",
                }:
                    result = payload.get("result") or {}
                    if isinstance(result, str):
                        result = json.loads(result)
                    members = [result, *((result.get("pending_inspection") or result.get("current_inspection") or {}).get("members") or [])]
                elif event.get("event_type") in {"critic_started", "critic_completed", "critic_reused", "critic_failed"}:
                    members = [payload]
                else:
                    continue
                referenced = any(member.get("png_sha256") == digest or member.get("sha256") == digest for member in members)
            except (ValueError, TypeError, AttributeError):
                continue
            if referenced:
                break
    if not referenced:
        raise FileNotFoundError("historical image receipt not found")
    from PIL import Image
    with open(os.path.join(run_dir, relative), "rb") as source:
        content = source.read()
    if hashlib.sha256(content).hexdigest() != digest:
        raise FileNotFoundError("historical image hash mismatch")
    try:
        with Image.open(io.BytesIO(content)) as picture:
            media_type = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}.get(picture.format)
            if not media_type:
                raise ValueError("unsupported historical image format")
            picture.verify()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise FileNotFoundError("historical image is not a valid raster image") from exc
    return content, media_type


def _owner_workflow(run_dir, events, all_events, owner):
    """只读投影真实事务；图片按回执 hash 定位，绝不拿新版截图替代旧版。"""
    def object_value(value):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return {}
        return value if isinstance(value, dict) else {}

    def node(event, kind, title):
        return {"id": event["event_id"], "sequence": event["sequence"],
                "timestamp": event["timestamp"], "segment_id": event.get("segment_id", ""),
                "turn": event.get("turn", 0), "kind": kind, "title": title,
                "pages": [], "status": "completed"}

    nodes, calls, inspections, feedbacks = [], {}, {}, {}
    pages = set(owner.get("pages") or [])
    agent_ids = {event.get("agent_id") for event in events}
    for event in events:
        payload = event["payload"]
        kind = event["event_type"]
        if kind == "tool_use":
            args = object_value(payload.get("arguments"))
            name = str(payload.get("name") or "tool")
            item = node(event, "edit" if name in {"write_file", "patch"} else "tool", name)
            if name in {"render", "inspect", "render_group", "inspect_group"}:
                item.update(kind="render", title="渲染 / Owner 看图" if name.startswith("inspect") else "渲染")
            item.update(tool_use_id=payload["tool_use_id"], tool_name=name, status="running",
                        detail=str(args.get("repair_reason") or args.get("question") or
                                   _live_event_hint(args))[:2400])
            refs = (args["page"] if isinstance(args.get("page"), list) else [args.get("page")]) + (
                args["pages"] if isinstance(args.get("pages"), list) else [])
            refs += re.findall(r"slides/slide_(\d+)\.html", str(args.get("path") or ""))
            item["pages"] = sorted({int(p) for p in refs if str(p).isdigit() and int(p) > 0})
            calls[payload["tool_use_id"]] = item
            nodes.append(item)
        elif kind == "tool_result":
            item = calls.get(payload.get("tool_use_id"))
            if item is None:
                item = node(event, "tool", str(payload.get("name") or "tool"))
                item.update(tool_use_id=payload.get("tool_use_id"), detail="缺少调用开始记录")
                nodes.append(item)
            result = object_value(payload.get("result"))
            item.update(status="completed" if payload.get("ok") else "failed",
                        finished_at=event["timestamp"], result_sequence=event["sequence"],
                        duration_ms=payload.get("duration_ms"), error_code=payload.get("error_code", ""))
            if not payload.get("ok"):
                item["error"] = str(result.get("message") or result.get("error") or payload.get("error_code") or "工具执行失败")[:2400]
            receipt = object_value(result.get("pending_inspection") or result.get("current_inspection"))
            members = receipt.get("members") or [result]
            images = []
            if payload.get("name") in {"inspect", "render", "render_group", "inspect_group", "run_command"}:
                for member in members:
                    page = member.get("page")
                    if str(page).isdigit():
                        item["pages"] = sorted(set(item["pages"]) | {int(page)})
                visual_inputs = ([{"page": result.get("page"), "png_sha256": result["sha256"]}]
                    if payload.get("name") == "inspect" and result.get("sha256") else members)
                for image in visual_inputs:
                    digest = image.get("png_sha256")
                    if digest:
                        images.append({"page": image.get("page"), "sha256": digest,
                                       "rel": _workflow_trace_image(run_dir, digest)})
                if images:
                    inspection_kind = result.get("kind") or receipt.get("kind")
                    title = {"page": "单页视觉检查", "group": "多页联系表检查", "asset": "素材 / 附件图片检查"}.get(inspection_kind)
                    item.update(kind="render", title=title or ("渲染 / Owner 看图" if payload.get("name", "").startswith("inspect") else "渲染"),
                                images=images, pixel_unchanged=bool(result.get("pixel_unchanged")),
                                inspection_cached=bool(result.get("inspection_cached")),
                                inspection_id=result.get("inspection_id", ""))
                    if result.get("native_multimodal"):
                        item["observation"] = "截图已返回 Owner；调用成功不代表视觉通过，判断见后续消息。"
            inspection_id = result.get("inspection_id")
            if inspection_id:
                inspections[inspection_id] = item
                feedback = object_value(result.get("critic_feedback"))
                if (feedback.get("inspection_id") == inspection_id
                        and feedback.get("png_sha256") in {image["sha256"] for image in item.get("images", [])}):
                    feedbacks[inspection_id] = feedback
        elif kind in {"assistant_message", "assistant_analysis_summary"}:
            item = node(event, "message", "Owner 消息" if kind == "assistant_message" else "可展示分析摘要")
            item["detail"] = str(payload.get("message") if kind == "assistant_message" else payload.get("summary") or "")[:6000]
            nodes.append(item)
        elif kind in {"agent_started", "agent_completed", "agent_failed", "provider_failed", "provider_failover"}:
            labels = {"agent_started": "执行段开始", "agent_completed": "执行段完成", "agent_failed": "执行段失败",
                      "provider_failed": "Provider 失败", "provider_failover": "Provider 切换通道"}
            item = node(event, "lifecycle", labels[kind])
            item["status"] = "failed" if kind.endswith("failed") else "completed"
            item["detail"] = str(payload.get("reason") or payload.get("error_code") or "")[:1600]
            nodes.append(item)

    # Critic 有独立 agent_id，用 inspection_id + 像素 hash 关联，不能按“最近一页”猜。
    def critic_details(item, feedback):
        if feedback.get("skipped"):
            item.update(status="skipped", completed=False,
                        title="草稿 / 筛查检查（未调用外部 Critic）",
                        feedback=str(feedback.get("body") or "")[:8000],
                        error="",
                        critic_model="", rubric_version="",
                        checked_scope=feedback.get("checked_scope", ""),
                        asset_usage_status=feedback.get("asset_usage_status", ""),
                        not_checked=feedback.get("not_checked") or [],
                        next_required_action=feedback.get("next_required_action", ""),
                        source_refs={},
                        reused_from={},
                        cache_hit=False)
            return
        completed = feedback.get("completed")
        item.update(status="completed" if completed is True else "failed" if completed is False else "unknown",
                    completed=completed, feedback=str(feedback.get("body") or "")[:8000],
                    error=str(feedback.get("incomplete_reason") or "")[:2400],
                    critic_model=feedback.get("critic_model", ""), rubric_version=feedback.get("rubric_version", ""),
                    source_refs=feedback.get("source_refs") or {},
                    reused_from=feedback.get("reused_from") or ({"inspection_id": feedback["reused_from_inspection_id"]}
                        if feedback.get("reused_from_inspection_id") else {}),
                    cache_hit=bool(feedback.get("cache_hit") or feedback.get("reused_from")
                                   or feedback.get("reused_from_inspection_id")))
        if feedback.get("latency_s") is not None:
            item["duration_ms"] = round(float(feedback["latency_s"]) * 1000)

    reviews = {}
    for event in sorted(all_events, key=lambda row: row["sequence"]):
        if not event["event_type"].startswith("critic_"):
            continue
        payload = event["payload"]
        inspection_id = payload.get("inspection_id")
        if payload.get("owner_id") and payload["owner_id"] not in agent_ids:
            continue
        render = inspections.get(inspection_id)
        if render is None and payload.get("owner_id") in agent_ids:
            render = calls.get(payload.get("tool_use_id"))
            if (not render or render.get("tool_name") not in {"inspect", "inspect_group"}
                    or not inspection_id or not re.fullmatch(r"[a-f0-9]{64}", str(payload.get("png_sha256") or ""))
                    or (payload.get("page") is not None and payload["page"] not in render["pages"])):
                continue
            if render.get("images") and payload["png_sha256"] not in {image["sha256"] for image in render["images"]}:
                continue
            if not render.get("images"):
                render.update(inspection_id=inspection_id, title="inspect 等待独立反馈",
                              images=[{"page": payload.get("page"), "sha256": payload["png_sha256"],
                                       "rel": _workflow_trace_image(run_dir, payload["png_sha256"])}])
            inspections[inspection_id] = render
        if not render or payload.get("png_sha256") not in {image["sha256"] for image in render.get("images", [])}:
            continue
        item = reviews.get(inspection_id)
        if item is None:
            item = node(event, "critic", "独立视觉 Critic")
            item.update(pages=render["pages"], inspection_id=inspection_id, png_sha256=payload.get("png_sha256"),
                        render_id=render["id"], segment_id=render["segment_id"],
                        tool_use_id=render.get("tool_use_id"), updates=0)
            reviews[inspection_id] = item
            nodes.append(item)
        item["updates"] += 1
        critic_details(item, payload)
        waiting = event["event_type"] == "critic_started" or (
            event["event_type"] == "critic_reused" and payload.get("pending") is True)
        if waiting:
            item["status"] = "running"
        else:
            item["finished_at"] = event["timestamp"]
    for inspection_id, feedback in feedbacks.items():
        render = inspections[inspection_id]
        item = reviews.get(inspection_id)
        if item is None:
            item = {key: render[key] for key in ("sequence", "timestamp", "segment_id", "turn", "pages")}
            item.update(id=render["id"] + ":critic-feedback", kind="critic", title="独立视觉 Critic",
                        inspection_id=inspection_id, png_sha256=feedback["png_sha256"],
                        render_id=render["id"], tool_use_id=render.get("tool_use_id"), updates=0)
            nodes.append(item)
        critic_details(item, feedback)
        item["finished_at"] = render.get("finished_at")
    versions = {}
    for item in nodes:
        pages.update(item["pages"])
        for image in item.get("images", []):
            hashes = versions.setdefault(image["page"], [])
            if image["sha256"] not in hashes:
                hashes.append(image["sha256"])
            image["version"] = hashes.index(image["sha256"]) + 1
    return {"nodes": sorted(nodes, key=lambda row: row["sequence"]),
                               "pages": sorted(pages), "source": "live-events.jsonl",
                               "note": "连线表示实际调用顺序，不推断因果；Critic 按检查 ID 和像素版本关联。"}


def _live_events_agent_trace_payload(run_dir, agent_key, segment, source="messages", include_workflow=False):
    """Project the unified v1 journal into the existing agent-trace envelope."""
    by_agent, _seen, owners = _live_events_by_agent(run_dir)
    if not by_agent and not owners:
        return None
    wanted = _canonical_agent_key(agent_key)
    events = by_agent.get(wanted)
    if events is None:
        events = next(
            (rows for key, rows in by_agent.items()
             if _canonical_agent_key(key) == wanted),
            None,
        )
    if events is None:
        # 独立评审共用实时角色泳道，但下钻必须按真实 Agent 身份隔离。
        events = [event for rows in by_agent.values() for event in rows
                  if event.get("agent_id") == agent_key] or None
    task_states = _shared_v1_task_states(
        run_dir, owners, [event for rows in by_agent.values() for event in rows]
    )
    task_state = task_states.get(wanted)
    if not events and task_state is None:
        raise FileNotFoundError("agent trace not found")

    agents = []
    for key, rows in by_agent.items():
        contexts = {}
        for event in rows:
            if event["event_type"] in {"agent_started", "agent_context"}:
                contexts[event["agent_id"]] = event
        independent = rows[0].get("role") in {"critic", "content-audit"}
        for identity, event in (contexts.items() if independent else [(key, rows[0])]):
            metadata = (event.get("payload") or {}).get("metadata") or {}
            agents.append({"key": identity, "label": event["agent_id"], "role": event["role"],
                           "page": metadata.get("page"), "owner_id": metadata.get("owner_id", "")})
    extra = {"agents": agents}
    if include_workflow:
        owner = next((row for row in owners.values() if row["key"] == wanted), {})
        extra["workflow"] = _owner_workflow(run_dir, events or [],
                                          [event for rows in by_agent.values() for event in rows], owner)

    segments = _live_events_agent_segments(events or [], owners, wanted)
    all_segments = segment == "__all__"
    selected = segments[-1] if all_segments and segments else next(
        (item for item in segments if segment and item["id"] == segment), None
    )
    public_segments = [
        {key: value for key, value in item.items() if not key.startswith("_")}
        for item in segments
    ]
    if selected is None and task_state and not task_state["trace_available"]:
        owner = next(row for row in owners.values() if row["key"] == wanted)
        return {
            "schema": "mural.agent-trace.v1",
            "agent": {"key": wanted, "label": owner["agent_id"], "role": owner["role"]},
            "segments": public_segments,
            "selected_segment": "__all__" if all_segments else "",
            "trace_scope": "all_segments" if all_segments else "segment",
            "source": "messages",
            "filename": "",
            "size": 0,
            "updated_at": None,
            "content": "[]",
            "redacted": False,
            "message_times": [],
            "empty_state": {
                "kind": task_state["execution_state"],
                "message": task_state["status_detail"],
            },
            "task_state": task_state,
            **extra,
        }
    if selected is None:
        selected = segments[-1]
    scoped = events if all_segments else ([
        event for event in events
        if str(event.get("segment_id") or "") == selected["id"]
    ] or events)
    messages, message_times = _live_events_messages(
        scoped, response_language=_run_response_language(run_dir)
    )
    # Redaction already ran per row on read; re-run the credential/thinking pass
    # on the assembled tree so a projected tool_result string can never leak a
    # bearer token that survived inside a nested result payload.
    contexts = [event["payload"] for event in scoped
                if event["event_type"] == "agent_context"]
    context = contexts[-1] if contexts else None
    unavailable = None
    value = messages
    if source in {"full_reasoning", "summary"}:
        mode = "full" if source == "full_reasoning" else "summary"
        value = [{**event["payload"], "sequence": event["sequence"], "timestamp": event["timestamp"]}
                 for event in scoped if event["event_type"] == "assistant_reasoning"
                 and event["payload"].get("mode") == mode]
        completed = {(item.get("attempt_id"), str(item.get("block_index"))) for item in value}
        partial = {}
        for event in scoped:
            item = event["payload"]
            key = (item.get("attempt_id"), str(item.get("block_index")))
            if event["event_type"] != "assistant_reasoning_delta" or item.get("mode") != mode or key in completed:
                continue
            record = partial.setdefault(key, {**item, "content": "", "sequence": event["sequence"]})
            record["content"] += item.get("content", "")
        value.extend(partial.values())
        if source == "summary" and not value:
            value = [{"mode": "summary", "content": event["payload"].get("summary"),
                      "sequence": event["sequence"], "source": "provider_response"}
                     for event in scoped if event["event_type"] == "assistant_analysis_summary"]
        if not value:
            unavailable = "API 未记录此类 reasoning；不会用摘要或模型消息补写完整 reasoning。"
    elif source == "system_prompt":
        value = context.get("system", "") if context else ""
    elif source == "config":
        value = {key: item for key, item in (context or {}).items()
                 if key not in {"system", "messages", "tools"}}
        value["tool_schemas"] = (context or {}).get("tools", [])
    elif source == "tools":
        uses = {event["payload"]["tool_use_id"]: {**event["payload"], "sequence": event["sequence"],
                "started_at": event["timestamp"], "status": "running"} for event in scoped
                if event["event_type"] == "tool_use"}
        for event in scoped:
            if event["event_type"] == "tool_result":
                use = uses.get(event["payload"]["tool_use_id"])
                if use is not None:
                    use.update(event["payload"], finished_at=event["timestamp"],
                               status="completed" if event["payload"].get("ok") else "failed")
        value = list(uses.values())
    elif source == "provider_attempts":
        value = [event for event in scoped if event["event_type"].startswith("provider_")]
    elif source == "events":
        value = scoped
    if source in {"system_prompt", "config"} and context is None:
        unavailable = "unavailable_in_source_trace: 原运行未记录实际 Agent 上下文。"
    if source == "messages" and context:
        # The complete Agent view carries the initial model-facing context once,
        # then follows durable journal events across compaction/resume segments.
        # Replaying every later cumulative context would duplicate old turns.
        inherited_context = contexts[0] if all_segments else context
        inherited = inherited_context.get("messages", [])
        value = inherited + value
        message_times = [None] * len(inherited) + message_times
    content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    selected_public = next(item for item in public_segments if item["id"] == selected["id"])
    updated = _trace_timestamp(
        (events[-1].get("timestamp") if all_segments and events else None)
        or selected.get("finished_at") or selected.get("started_at")
    )
    payload = {
        "schema": "mural.agent-trace.v1",
        "agent": {
            "key": selected_public["key"],
            "label": selected_public["label"],
            "role": selected_public["role"],
        },
        "segments": public_segments,
        "selected_segment": "__all__" if all_segments else selected_public["id"],
        "trace_scope": "all_segments" if all_segments else "segment",
        "source": source,
        "filename": "live-events.jsonl",
        "size": len(content.encode("utf-8")),
        "updated_at": _trace_iso(updated) if updated else None,
        "content": content,
        "redacted": False,
        "message_times": message_times,
        "projected_from": "live-events.jsonl",
        **extra,
    }
    if task_state:
        payload["task_state"] = task_state
    if unavailable:
        payload["empty_state"] = {"kind": "unavailable_in_source_trace", "message": unavailable}
    elif source == "messages" and not messages:
        payload["empty_state"] = {
            "kind": "no_messages",
            "message": "该执行段尚未写入可展示消息；已有生命周期记录仍保留在轨迹中。",
        }
    return payload


def agent_trace_payload(run_dir, agent_key, *, segment="", source="messages", include_workflow=False):
    """Return the recorded JSON trace for one workflow Agent without text rewriting."""
    source = str(source or "messages").strip().lower()
    if source not in _AGENT_TRACE_SOURCES:
        raise ValueError("unsupported trace source")
    WorkspaceLayout.load(run_dir)
    projected = _live_events_agent_trace_payload(run_dir, agent_key, segment, source, include_workflow)
    if projected is None:
        raise FileNotFoundError("agent trace not recorded")
    return projected


def _json_text_values(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _json_text_values(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _json_text_values(item)


def _registry_page_agents(run_dir):
    """页面责任只来自真实 Owner Registry，不从作者计划猜测。"""
    return _shared_v1_live_owners(run_dir)[1]


def _group_event_pages(args):
    """Best-effort page references from a grouped Agent tool call."""
    pages = {int(value) for value in _SLIDE_REF_RE.findall(args or "")}
    for match in _PAGES_ARG_RE.finditer(args or ""):
        pages.update(int(value) for value in re.findall(r"\d+", match.group(1)))
    return {page for page in pages if page > 0}


def _canonical_agent_key(value):
    """Normalize historical public aliases without renaming trace directories."""
    raw = str(value or "").strip()
    base = re.sub(r"_r\d+$", "", raw, flags=re.I)
    if base.lower() in {"orch", "orchestrator"}:
        return "orch"
    return base


def _livefeed_agent_key(raw_agent, aliases=None):
    """Normalize retry Agent labels to the stable key consumed by the UI."""
    raw_agent = str(raw_agent or "")
    if raw_agent in (aliases or {}):
        return aliases[raw_agent]
    base_agent = _canonical_agent_key(raw_agent)
    alias = (aliases or {}).get(raw_agent) or (aliases or {}).get(base_agent) or base_agent
    base_agent = _canonical_agent_key(alias)
    grouped = re.match(r"slide[-_]group[-_](.+)", base_agent, re.I)
    if grouped:
        return _slide_group_agent_key(grouped.group(1))
    slide_match = re.match(r"(slide_\d+)", base_agent)
    return slide_match.group(1) if slide_match else base_agent


def _streaming_delta_events(role_dir, messages, response_language, sequence):
    """Project only the current, not-yet-canonical model turn into the UI.

    ``live-deltas.jsonl`` is deliberately not a training source.  Once the
    matching Assistant response appears in atomic ``messages.json``, this
    projection disappears and the canonical blocks take over.
    """
    path = os.path.join(role_dir, "live-deltas.jsonl")
    if not os.path.isfile(path):
        return []
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as source:
            start = max(0, size - 2 * 1024 * 1024)
            source.seek(start)
            raw = source.read().decode("utf-8", errors="ignore")
        lines = raw.splitlines()
        if start and lines:
            lines = lines[1:]
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            item = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(item, dict) and item.get("schema") == "mural.live-delta.v1":
            records.append(item)
    starts = [item for item in records if item.get("event") == "turn_start"]
    if not starts:
        return []
    current = starts[-1]
    stream_id = str(current.get("stream_id") or "")
    canonical_assistant_count = sum(
        1 for message in messages
        if isinstance(message, dict) and message.get("role") == "assistant"
    )
    try:
        base_assistant_count = int(current.get("base_assistant_count") or 0)
    except (TypeError, ValueError):
        base_assistant_count = 0
    if canonical_assistant_count > base_assistant_count:
        return []
    stream_records = [
        item for item in records
        if str(item.get("stream_id") or "") == stream_id
        and item.get("event") == "delta"
    ]
    visible_text = "".join(
        str(item.get("text") or "")
        for item in stream_records if item.get("kind") == "text"
    )
    visible_text = _visible_text_for_language(visible_text, response_language)
    events = []
    if visible_text:
        events.append({
            "k": "text", "s": visible_text, "seq": sequence,
            "partial": True,
        })
        sequence += 1
    seen_tools = set()
    for item in stream_records:
        if item.get("kind") != "tool_start":
            continue
        name = str(item.get("tool_name") or item.get("text") or "tool")
        tool_use_id = str(item.get("tool_use_id") or "")
        identity = tool_use_id or name
        if identity in seen_tools:
            continue
        seen_tools.add(identity)
        events.append({
            "k": "tool", "tool": name, "hint": "正在接收参数…",
            "seq": sequence, "partial": True,
        })
        sequence += 1
    return events


def _same_live_event(canonical, streamed):
    """Match one durable trace event to its globally ordered runner-log row."""
    if canonical.get("k") != streamed.get("k"):
        return False
    if canonical.get("k") == "tool":
        return str(canonical.get("tool") or "") == str(streamed.get("tool") or "")
    left = re.sub(r"\s+", " ", str(canonical.get("s") or "")).strip()
    right = re.sub(r"\s+", " ", str(streamed.get("s") or "")).strip()
    if not left or not right:
        return False
    # The compact stdout row commonly contains only the first line / first 800
    # characters, while messages.json retains the complete Assistant response.
    return left == right or left.startswith(right) or right.startswith(left)


def _inherit_live_event_positions(canonical, streamed):
    """Keep global runner order/times when atomic traces replace compact rows.

    Agent-local messages.json files all begin at sequence zero.  Treating those
    values as global sequence numbers made completed workers jump to the top of
    the process timeline.  The runner log is the shared event stream, so match
    durable events back to it and retain its global position and wall-clock.
    """
    if not canonical:
        return canonical
    source = list(streamed or [])
    cursor = 0
    matched = []
    for canonical_index, event in enumerate(canonical):
        # Streaming projections carry an Agent-local sequence. It is useful
        # only inside that one trace and must never be treated as global.
        event.pop("seq", None)
        match_index = None
        for index in range(cursor, len(source)):
            if _same_live_event(event, source[index]):
                match_index = index
                break
        if match_index is None:
            continue
        original = source[match_index]
        for field in ("seq", "clock", "elapsed_s"):
            if original.get(field) is not None:
                event[field] = original[field]
        matched.append((canonical_index, float(original.get("seq", match_index))))
        cursor = match_index + 1

    # A rare trace-only block can be absent from stdout (for example a streamed
    # final delta). Place it between its nearest matched neighbours instead of
    # assigning another Agent-local zero. Fractional sequence values remain
    # stable and sort naturally next to the surrounding global events.
    anchors = {index: order for index, order in matched}
    for index, event in enumerate(canonical):
        if event.get("seq") is not None:
            continue
        previous = next(((i, anchors[i]) for i in range(index - 1, -1, -1) if i in anchors), None)
        following = next(((i, anchors[i]) for i in range(index + 1, len(canonical)) if i in anchors), None)
        if previous and following:
            span = following[0] - previous[0]
            event["seq"] = previous[1] + (following[1] - previous[1]) * ((index - previous[0]) / span)
        elif previous:
            event["seq"] = previous[1] + (index - previous[0]) / 1000
        elif following:
            event["seq"] = following[1] - (following[0] - index) / 1000
        elif source:
            # The durable text can differ from the compact runner row after
            # localization/normalization.  It still belongs at this Agent's
            # first globally observed position, never at a new local zero.
            event["seq"] = float(source[0].get("seq", 0)) + index / 1000
            for field in ("clock", "elapsed_s"):
                if source[0].get(field) is not None:
                    event[field] = source[0][field]
        else:
            event["seq"] = index
    return canonical


def _live_trace_events(run_dir, aliases, response_language, active_agents):
    """Read complete in-progress Assistant turns from atomic Harness traces.

    Only agents already present in the current runner log are eligible.  This
    prevents traces retained from an earlier static revision from appearing in
    the newest conversation turn.
    """
    if not run_dir:
        return {}
    role_dirs = [os.path.join(run_dir, "_trace", "orchestrator")]
    role_dirs.extend(sorted(glob.glob(os.path.join(run_dir, "_trace", "subagents", "*"))))
    result = {}
    for role_dir in role_dirs:
        if not os.path.isdir(role_dir):
            continue
        config = _read_trace_json(os.path.join(role_dir, "config.json"), {})
        raw_agent = str(config.get("label") or os.path.basename(role_dir))
        if str(config.get("role") or "").lower() == "orchestrator":
            raw_agent = "orch"
        key = _livefeed_agent_key(raw_agent, aliases)
        if key not in active_agents:
            continue
        messages = _read_trace_json(os.path.join(role_dir, "messages.json"), [])
        if not isinstance(messages, list):
            continue
        events = []
        sequence = 0
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            content = message.get("content")
            blocks = content if isinstance(content, list) else [
                {"type": "text", "text": content}
            ]
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                kind = str(block.get("type") or "")
                if kind == "text":
                    value = _visible_text_for_language(
                        str(block.get("text") or ""), response_language
                    )
                    if value:
                        events.append({"k": "text", "s": value})
                        sequence += 1
                elif kind == "tool_use":
                    args = block.get("input") if isinstance(block.get("input"), dict) else {}
                    hint = ""
                    for field in ("path", "query", "pattern", "command", "aspect_ratio", "url"):
                        if args.get(field):
                            hint = str(args[field])
                            break
                    events.append({
                        "k": "tool",
                        "tool": str(block.get("name") or "tool"),
                        "hint": hint,
                    })
                    sequence += 1
        events.extend(
            _streaming_delta_events(
                role_dir, messages, response_language, sequence
            )
        )
        if events:
            result[key] = _inherit_live_event_positions(events, active_agents.get(key, []))
    return result


def _shared_v1_live_owners(run_dir):
    """Map durable v1 Owner IDs to stable WebUI role/group identities."""
    value = _read_trace_json(
        os.path.join(run_dir or "", "_state", "owners.json"), {}
    )
    owner_map = value.get("owners") if isinstance(value, dict) else {}
    if not isinstance(owner_map, dict):
        owner_map = {}
    rows = []
    archived = [(str(row.get("group_id") or ""), row)
                for row in ((value.get("archives") or []) if isinstance(value, dict) else []) if isinstance(row, dict)]
    for registry_key, raw in [*archived, *owner_map.items()]:
        if not isinstance(raw, dict):
            continue
        owner_id = str(raw.get("owner_id") or "").strip()
        role = str(raw.get("role") or "").strip().lower()
        if not owner_id or not role:
            continue
        rows.append((str(registry_key), owner_id, role, raw))
    owners = {}
    page_agents = {}
    for registry_key, owner_id, role, raw in rows:
        group_id = str(raw.get("group_id") or registry_key or "").strip()
        pages = sorted({
            int(page) for page in (raw.get("pages") or [])
            if str(page).isdigit() and int(page) > 0
        })
        if role == "orchestrator":
            key = "orch"
        elif role == "slide":
            key = _slide_group_agent_key(group_id or owner_id)
        else:
            key = f"{role}_{owner_id}"
        if role in {"orchestrator", "slide"} and raw.get("status") != "archived":
            for page in pages:
                page_agents[str(page)] = key
        owners[owner_id] = {
            "key": key,
            "role": role,
            "agent_id": owner_id,
            "group_id": group_id,
            "pages": pages,
            "status": str(raw.get("status") or ""),
        }
    return owners, page_agents


def _shared_v1_task_states(run_dir, owners, events):
    """只读投影调度状态；未启动任务没有 Agent Segment，不能伪造消息或时间。"""
    orchestration = _read_trace_json(os.path.join(run_dir or "", "_state", "orchestration.json"), {})
    tasks = orchestration.get("tasks") or {}
    schedule = sorted(orchestration.get("events") or [], key=lambda row: int(row.get("sequence") or 0))
    submitted = {row.get("task_id"): row.get("sequence", 0) for row in schedule if row.get("type") == "task_submitted"}
    states = {}
    for owner_id, owner in owners.items():
        candidates = [task for task in tasks.values() if task.get("owner_id") == owner_id]
        rows = sorted((event for event in events if event.get("agent_id") == owner_id), key=lambda row: int(row.get("sequence") or 0))
        if not candidates and rows:
            continue
        task = max(candidates, key=lambda row: (int(row.get("attempt") or 0), submitted.get(row.get("task_id"), 0))) if candidates else {}
        task_id = str(task.get("task_id") or "")
        status = str(task.get("status") or owner.get("status") or "pending")
        # 调度线程启动不等于已有 Agent 消息；用 agent_started 区分同一 Owner 的续跑。
        start = next((row for row in reversed(rows) if row.get("event_type") == "agent_started"
                      and (row.get("payload") or {}).get("task_id") == task_id), None)
        current_rows = [row for row in rows if start and int(row.get("sequence") or 0) >= int(start["sequence"])]
        terminal = next((row for row in reversed(current_rows) if row.get("event_type") in {"agent_completed", "agent_failed"}), None)
        failed_dependencies = next((row.get("failed_dependencies") or [] for row in reversed(schedule) if row.get("task_id") == task_id and row.get("type") in {"task_failed", "task_skipped"}), [])
        if not failed_dependencies and status in {"failed", "skipped"} and not start:
            failed_dependencies = [key for key in task.get("dependencies") or [] if tasks.get(key, {}).get("status") in {"failed", "cancelled", "skipped"}]
        detail = ""
        dependency_causes = []
        if failed_dependencies and not start:
            labels = []
            for dependency_id in failed_dependencies:
                dependency = tasks.get(dependency_id, {})
                label = str(dependency.get("role") or "依赖任务").title()
                group = str(dependency.get("group_id") or dependency_id)
                labels.append(f"{label}（{group}）")
                dependency_owner = owners.get(dependency.get("owner_id"), {})
                dependency_start = next((event for event in reversed(events) if event.get("event_type") == "agent_started"
                                         and (event.get("payload") or {}).get("task_id") == dependency_id), {})
                dependency_causes.append({"task_id": dependency_id, "label": f"{label}（{group}）",
                                          "agent_key": dependency_owner.get("key", ""),
                                          "segment_id": dependency_start.get("segment_id", "")})
            state = "dependency_failed"
            detail = f"上游 {'、'.join(labels)} 失败或取消，本任务未启动，因此没有 Agent 消息。"
        elif status == "cancelled":
            state, detail = "cancelled", "任务已取消。"
        elif not start:
            if status == "failed":
                state = "start_failed"
                error_type = str(task.get("error") or "").partition(":")[0]
                detail = "任务在 Agent 启动前失败，尚无 Agent 消息。"
                if re.fullmatch(r"[A-Za-z_]\w{0,80}(?:Error|Exception)", error_type):
                    detail += f"错误类型：{error_type}。"
            elif status == "waiting_dependency" or (status == "pending" and any(tasks.get(key, {}).get("status") not in {"completed", "succeeded"} for key in task.get("dependencies") or [])):
                state, detail = "waiting_dependencies", "正在等待上游依赖完成，Agent 尚未启动。"
                interrupted = next((row.get("interrupted_dependencies") or [] for row in reversed(schedule)
                                    if row.get("task_id") == task_id and row.get("type") == "dependency_interrupted"), [])
                if any(tasks.get(key, {}).get("status") == "interrupted" for key in interrupted):
                    detail = "上游依赖已暂停，本任务尚未启动；从同一工作区继续后再执行，没有 Agent 消息。"
            else:
                state, detail = "not_started", "任务已登记或派发，尚未进入 Agent 执行回合；启动后会自动显示消息。"
        else:
            state = {"completed": "complete", "succeeded": "complete", "blocked": "waiting"}.get(status, status)
            if status == "interrupted" and str(task.get("error") or "").startswith("ResourceSlotTimeout:"):
                detail = "等待模型资源超时，任务已暂停；可从同一工作区继续，不是 Provider 请求失败。"
            elif terminal:
                state = "failed" if terminal["event_type"] == "agent_failed" else "complete"
        states[owner["key"]] = {
            "task_id": task_id,
            "attempt": int(task.get("attempt") or 0),
            "task_status": status,
            "execution_state": state,
            "status": "waiting" if state in {"not_started", "waiting_dependencies"} else "failed" if state in {"dependency_failed", "start_failed"} else state,
            "status_detail": str(detail),
            "dependency_causes": dependency_causes,
            "trace_available": bool(start),
            "started_at": start.get("timestamp") if start else None,
            "finished_at": terminal.get("timestamp") if terminal else None,
        }
    return states


def _shared_v1_live_agent_key(event, owners):
    """Resolve one live event to the same stable key used by workflow cards."""
    agent_id = str(event.get("agent_id") or "").strip()
    role = str(event.get("role") or "").strip().lower()
    if role == "orchestrator" or agent_id == "orchestrator":
        return "orch"
    owner = owners.get(agent_id) if isinstance(owners, dict) else None
    if isinstance(owner, dict) and owner.get("key"):
        return str(owner["key"])
    if role == "slide":
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        group_id = str(payload.get("group_id") or "").strip()
        return _slide_group_agent_key(group_id or agent_id or "unassigned")
    return role or agent_id or "agent"


def _live_event_hint(arguments):
    if not isinstance(arguments, dict):
        return ""
    for field in (
        "path", "query", "pattern", "command", "url", "wave_id",
        "group_id", "status", "pages",
    ):
        value = arguments.get(field)
        if value not in (None, "", [], {}):
            if isinstance(value, (dict, list)):
                return json.dumps(value, ensure_ascii=False, separators=(",", ":"))[:240]
            return str(value)[:240]
    return ""


def _project_shared_v1_live_event(event, agent_key, *, critic_context=None,
                                  response_language=None):
    """Project a public journal row into the compact shape consumed by app.js."""
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    event_type = str(event.get("event_type") or "")
    projected = {
        "seq": int(event.get("sequence") or 0),
        "ts": str(event.get("timestamp") or ""),
        "event_id": str(event.get("event_id") or ""),
        "event_type": event_type,
        "agent": agent_key,
        "agent_id": str(event.get("agent_id") or ""),
        "role": str(event.get("role") or ""),
        "turn": int(event.get("turn") or 0),
        "segment_id": str(event.get("segment_id") or ""),
        "attempt_id": str(payload.get("attempt_id") or ""),
        "task_id": str(payload.get("task_id") or ""),
    }
    if event.get("role") == "critic":
        details = (payload.get("metadata") if event_type == "agent_context" else
                   payload if event_type.startswith("critic_") else critic_context)
        if isinstance(details, dict) and details.get("inspection_id"):
            # 展示用身份沿用真实检查记录，不把提示词或 Provider 私有配置投给聊天卡片。
            projected["critic"] = {key: details[key] for key in (
                "inspection_id", "owner_id", "page", "page_uid", "tool_use_id",
                "html_sha256", "png_sha256", "critic_model", "rubric_version",
                "body", "completed", "pending", "cache_hit", "latency_s", "incomplete_reason",
            ) if key in details}
            refs = details.get("source_refs") or {}
            if refs.get("agent_id"):
                projected["critic"]["trace_agent_id"] = refs["agent_id"]
                projected["critic"]["trace_segment_id"] = refs.get("segment_id", "")
    if event_type in {"assistant_message_delta", "assistant_analysis_summary_delta"}:
        text = str(payload.get("message") or payload.get("summary") or "")
        if event_type == "assistant_message_delta":
            text = _visible_text_for_language(text, response_language)
        projected.update({
            "k": "text" if event_type == "assistant_message_delta" else "analysis",
            "s": text,
            "partial": True,
            "stream_id": f"{payload.get('attempt_id')}:{event_type}",
            "revision": int(payload.get("revision") or 0),
        })
    elif event_type == "agent_context":
        provenance = payload.get("provenance") or {}
        tree_hash = provenance.get("skill_tree_sha256") if isinstance(provenance, dict) else None
        role = str(event.get("role") or "")
        if (role in {"orchestrator", "material", "research", "image", "slide"}
                and isinstance(payload.get("system"), str) and payload["system"].strip()
                and isinstance(tree_hash, str) and re.fullmatch(r"[0-9a-f]{64}", tree_hash)):
            # 只投影真实上下文中的版本身份，不发送提示词，也不把固定版本误报为角色启动。
            projected.update({
                "k": "context",
                "s": "主 Skill 已注入" if role == "orchestrator" else "角色 Skill 已注入",
                "skill_path": ("skills/mural-presenter-v1/SKILL.md" if role == "orchestrator"
                               else f"skills/mural-presenter-v1/roles/{role}.md"),
                "skill_version": tree_hash[:12],
                "source": "_trace/live-events.jsonl · agent_context",
            })
        else:
            projected.update({"k": "status", "s": "运行上下文已准备"})
    elif event_type == "assistant_message":
        request_kind, request_kind_label, request_kind_note = _request_kind_public(payload)
        projected.update({
            "k": "text",
            "s": _visible_text_for_language(
                str(payload.get("message") or ""), response_language
            ),
            "request_kind": request_kind,
            "request_kind_label": request_kind_label,
            "request_kind_note": request_kind_note,
            "model": str(payload.get("model") or ""),
            "actual_max_tokens": payload.get("actual_max_tokens"),
        })
    elif event_type == "assistant_analysis_summary":
        projected.update({"k": "analysis", "s": str(payload.get("summary") or "")})
    elif event_type == "agent_context_compacted":
        projected.update({
            "k": "status",
            "status": "completed",
            "s": "上下文压缩完成",
            **{key: payload[key] for key in (
                "estimated_tokens", "input_budget", "tokens_before", "tokens_after",
                "compact_target_tokens", "target_met", "pressure", "reason",
            ) if key in payload},
        })
    elif event_type == "agent_context_compaction_failed":
        projected.update({
            "k": "status",
            "status": "failed",
            "s": "上下文压缩未完成",
            **{key: payload[key] for key in (
                "error_type", "error_summary", "recoverable", "checkpoint_retained",
                "recovery_action", "estimated_tokens", "input_budget",
            ) if key in payload},
        })
    elif event_type.startswith("attachment_parse_"):
        status = str(payload.get("status") or "")
        label = {"queued": "附件等待后台解析", "processing": "附件正在后台解析",
                 "completed": "附件解析完成", "partial": "附件部分解析完成", "failed": "附件解析失败"}
        projected.update({"k": "status", "s": label.get(status, "附件解析状态更新"),
                          "status": status, "attachment_id": str(payload.get("attachment_id") or ""),
                          "source_sha256": str(payload.get("source_sha256") or ""),
                          "cached": payload.get("cached") is True,
                          "unresolved_count": int(payload.get("unresolved_count") or 0)})
    elif event_type.startswith("critic_"):
        label = {"critic_started": "独立 Critic 请求中，等待公开反馈",
                 "critic_reused": "独立 Critic 复用等待公开反馈（同版像素，未发起新请求）",
                 "critic_completed": "独立 Critic 执行完成",
                 "critic_failed": "独立 Critic 执行未完成"}[event_type]
        if event_type == "critic_completed" and (payload.get("cache_hit") or payload.get("reused_from")):
            label = "独立 Critic 反馈复用（同版像素，未发起新请求）"
        projected.update({"k": "status", "s": label})
    elif event_type.startswith("mineru_"):
        label = {"mineru_started": "附件解析请求已开始", "mineru_failed": "附件解析请求失败",
                 "mineru_failover": "附件解析服务切换", "mineru_succeeded": "附件解析请求完成"}[event_type]
        details = {key: payload[key] for key in ("attachment_id", "endpoint_index", "kind", "backend",
            "requested_format", "status", "cached", "duration_ms", "reason", "infrastructure", "attempt") if key in payload}
        projected.update({"k": "status", "s": label + " · " + json.dumps(details, ensure_ascii=False)})
    elif event_type == "deck_completion_checked":
        status = str(payload.get("status") or "")
        label = "制作完成校验通过" if status == "ready" else "制作完成校验未通过"
        details = {key: payload[key] for key in ("status", "pages", "error_code", "reason", "error_summary") if key in payload}
        projected.update({"k": "status", "s": label + " · " + json.dumps(details, ensure_ascii=False),
                          "status": status})
    elif event_type in {"slide_preparation_completed", "slide_preparation_failed", "image_handoff_ready"}:
        label = {"slide_preparation_completed": "本批页面结构已准备",
                 "slide_preparation_failed": "本批页面准备未完成", "image_handoff_ready": "当前素材已核验交接"}[event_type]
        details = {key: payload[key] for key in ("pages", "created_pages", "consumer_pages", "note", "error_code") if key in payload}
        projected.update({"k": "status", "s": label, **details})
    elif event_type.startswith("content_qc_"):
        label = {"content_qc_started": "内容审计开始", "content_qc_completed": "内容审计完成",
                 "content_qc_failed": "内容审计未完成"}[event_type]
        projected.update({"k": "status", "s": label + " · " + json.dumps(payload, ensure_ascii=False),
                          "status": payload.get("status", "running")})
    elif event_type == "tool_use":
        arguments = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
        projected.update({
            "k": "tool",
            "phase": "running",
            "tool": str(payload.get("name") or "tool"),
            "tool_use_id": str(payload.get("tool_use_id") or ""),
            "args": arguments,
            "hint": _live_event_hint(arguments),
        })
    elif event_type == "tool_result":
        projected.update({
            "k": "tool_result",
            "phase": "succeeded" if payload.get("ok") is True else "failed",
            "tool": str(payload.get("name") or "tool"),
            "tool_use_id": str(payload.get("tool_use_id") or ""),
            "ok": payload.get("ok") is True,
            "result": payload.get("result"),
            "duration_ms": int(payload.get("duration_ms") or 0),
            "error_code": str(payload.get("error_code") or ""),
        })
    elif event_type == "tool_output":
        projected.update({"k": "tool_output", "tool_use_id": str(payload.get("tool_use_id") or ""),
                          "output_cursor": payload.get("output_cursor"), "diagnostic_ref": payload.get("diagnostic_ref"),
                          "truncated": bool(payload.get("truncated"))})
    elif event_type.startswith("provider_"):
        request_kind, request_kind_label, request_kind_note = _request_kind_public(payload)
        projected.update({
            "k": "provider",
            "phase": event_type.removeprefix("provider_"),
            "model": str(payload.get("model") or ""),
            "request_kind": request_kind,
            "request_kind_label": request_kind_label,
            "request_kind_note": request_kind_note,
            "actual_max_tokens": payload.get("actual_max_tokens"),
            "streaming": payload.get("streaming") if isinstance(payload.get("streaming"), bool) else None,
            "attempt_id": str(payload.get("attempt_id") or ""),
            "channel_id": str(
                payload.get("channel_id") or payload.get("to_channel_id") or ""
            ),
            "error_kind": str(payload.get("error_kind") or ""),
            "status_code": payload.get("status_code"),
            "error_summary": str(payload.get("error_summary") or ""),
            "duration_ms": round(float(payload.get("elapsed_s") or 0) * 1000),
        })
    elif event_type == "language_drift":
        projected.update({"k": "status", "status": "warning",
                          "s": "工作语言疑似漂移",
                          "request_kind": str(payload.get("request_kind") or ""),
                          "request_kind_label": str(payload.get("request_kind_label") or ""),
                          "detail": str(payload.get("message") or ""),
                          "sample": str(payload.get("sample") or "")})
    else:
        projected.update({
            "k": "status",
            "status": {
                "agent_started": "running",
                "agent_completed": "completed",
                "agent_failed": "failed",
            }.get(event_type, ""),
            "error_type": str(payload.get("error_type") or ""),
            "error_summary": str(payload.get("error_summary") or ""),
        })
    return projected


def live_events(run_dir, *, cursor=0, limit=500, response_language=None):
    """Read complete journal rows after ``cursor`` and return role-grouped deltas."""
    try:
        WorkspaceLayout.load(run_dir)
    except (ValueError, TypeError, OSError) as exc:
        return {"events": [], "agents": {}, "cursor": cursor, "next_cursor": cursor,
                "has_more": False, "workspace_error": str(exc)}
    try:
        requested_cursor = max(0, int(cursor or 0))
    except (TypeError, ValueError):
        requested_cursor = 0
    try:
        page_limit = min(5000, max(1, int(limit or 500)))
    except (TypeError, ValueError):
        page_limit = 500
    if response_language is None:
        response_language = _run_response_language(run_dir)
    owners, page_agents = _shared_v1_live_owners(run_dir)
    path = os.path.join(run_dir or "", "_trace", "live-events.jsonl")
    all_events = []
    seen_ids = set()
    if os.path.isfile(path):
        try:
            with open(path, "rb") as source:
                for raw in source:
                    if not raw.endswith(b"\n"):
                        continue
                    try:
                        value = json.loads(raw)
                    except (UnicodeDecodeError, TypeError, ValueError):
                        continue
                    if not isinstance(value, dict) or value.get("schema") != _LIVE_EVENT_SCHEMA:
                        continue
                    try:
                        sequence = int(value.get("sequence") or 0)
                    except (TypeError, ValueError):
                        continue
                    event_type = str(value.get("event_type") or "")
                    event_id = str(value.get("event_id") or "")
                    if sequence <= 0 or event_type not in _LIVE_EVENT_TYPES:
                        continue
                    if event_id and event_id in seen_ids:
                        continue
                    if event_id:
                        seen_ids.add(event_id)
                    public = value
                    if isinstance(public, dict):
                        all_events.append(public)
        except OSError:
            all_events = []
    all_events.sort(key=lambda item: int(item.get("sequence") or 0))
    maximum = int(all_events[-1].get("sequence") or 0) if all_events else 0
    cursor_reset = requested_cursor > maximum and maximum > 0
    effective_cursor = 0 if cursor_reset else requested_cursor
    remaining = [
        event for event in all_events
        if int(event.get("sequence") or 0) > effective_cursor
    ]
    selected = remaining[:page_limit]
    next_cursor = (
        int(selected[-1].get("sequence") or effective_cursor)
        if selected else effective_cursor
    )
    bound_keys = {}
    event_keys = {}
    critic_contexts = {}
    for event in all_events:
        agent_id = str(event.get("agent_id") or "")
        if event.get("role") == "critic" and event["event_type"] == "agent_context":
            critic_contexts[(agent_id, event.get("segment_id"))] = (event.get("payload") or {}).get("metadata")
        if event["event_type"] in {"agent_started", "agent_context"} or agent_id not in bound_keys:
            bound_keys[agent_id] = _shared_v1_live_agent_key(event, owners)
        event_keys[event["sequence"]] = bound_keys[agent_id]
    grouped = {}
    projections = []
    for event in selected:
        key = event_keys[event["sequence"]]
        projected = _project_shared_v1_live_event(event, key,
            critic_context=critic_contexts.get((event.get("agent_id"), event.get("segment_id"))),
            response_language=response_language)
        if projected.get("k") == "text" and not str(projected.get("s") or "").strip():
            continue
        projections.append(projected)
        if event["event_type"] != "tool_output":
            grouped.setdefault(key, []).append(projected)

    statuses = {}
    timing_bounds = {}
    critic_reviews = {}
    active_segments = {}
    for owner in owners.values():
        key = str(owner.get("key") or "")
        status = str(owner.get("status") or "")
        statuses[key] = {
            **owner,
            "status": "complete" if status == "completed" else status,
        }
    for event in all_events:
        key = event_keys[event["sequence"]]
        event_type = str(event.get("event_type") or "")
        if event.get("role") == "harness":
            continue
        timestamp = str(event.get("timestamp") or "")
        current = statuses.setdefault(key, {
            "key": key,
            "role": str(event.get("role") or ""),
            "agent_id": str(event.get("agent_id") or ""),
            "group_id": "",
            "pages": [],
            "status": "waiting",
        })
        bounds = timing_bounds.setdefault(key, {"started_at": None, "finished_at": None})
        if event_type.startswith("critic_"):
            payload = event.get("payload") or {}
            review_id = str(payload.get("inspection_id") or "")
            critic_reviews[review_id] = (
                "running" if event_type == "critic_started" or (
                    event_type == "critic_reused" and payload.get("pending") is True) else
                "complete" if payload.get("completed") is True else "failed")
            states = set(critic_reviews.values())
            current["status"] = "running" if "running" in states else "failed" if "failed" in states else "complete"
            bounds["started_at"] = bounds["started_at"] or timestamp
            bounds["finished_at"] = None if "running" in states else timestamp
        elif event_type in {"agent_started", "content_qc_started"}:
            active_segments.setdefault(key, set()).add(str(event.get("segment_id") or ""))
            current["status"] = "running"
            bounds["started_at"] = bounds["started_at"] or timestamp
            bounds["finished_at"] = None
        elif event_type in {"agent_completed", "content_qc_completed"}:
            current["status"] = "complete"
            bounds["finished_at"] = timestamp
        elif event_type in {"agent_failed", "content_qc_failed"}:
            current["status"] = "failed"
            bounds["finished_at"] = timestamp
        if event_type in {"agent_completed", "agent_failed", "content_qc_completed", "content_qc_failed"}:
            active_segments.setdefault(key, set()).discard(str(event.get("segment_id") or ""))
        if active_segments.get(key) or (key == "critic" and "running" in critic_reviews.values()):
            current["status"] = "running"
            bounds["finished_at"] = None
        if timestamp and not bounds["started_at"]:
            bounds["started_at"] = timestamp
    timings = {}
    for key, status in statuses.items():
        bounds = timing_bounds.get(key, {})
        started_at = bounds.get("started_at")
        finished_at = bounds.get("finished_at")
        started = _trace_timestamp(started_at)
        finished = _trace_timestamp(finished_at)
        timings[key] = {
            "status": status.get("status") or "waiting",
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_s": (
                round(max(0.0, finished - started), 1)
                if started is not None and finished is not None else None
            ),
            "role": status.get("role") or "",
            "agent_id": status.get("agent_id") or "",
            "group_id": status.get("group_id") or "",
            "pages": list(status.get("pages") or []),
        }
    for key, state in _shared_v1_task_states(run_dir, owners, all_events).items():
        started = _trace_timestamp(state["started_at"])
        finished = _trace_timestamp(state["finished_at"])
        statuses[key].update(state)
        timings[key].update(state, duration_s=(
            round(max(0.0, finished - started), 1)
            if started is not None and finished is not None else None
        ))
    return {
        "schema": _LIVE_EVENT_SCHEMA,
        "cursor": effective_cursor,
        "next_cursor": next_cursor,
        "max_sequence": maximum,
        "has_more": len(remaining) > len(selected),
        "cursor_reset": cursor_reset,
        "events": projections,
        "agents": grouped,
        "agent_keys": sorted(statuses),
        "agent_statuses": statuses,
        "agent_timings": timings,
        "page_agents": page_agents,
    }


def _trace_timestamp(value):
    """Parse Harness timestamps without inventing a time for old traces."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    normalized = text.replace(" UTC", "+00:00")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Historical Harness versions persisted the host-local wall clock
        # without an offset.  Interpret those values in the same host-local
        # timezone instead of relabelling them as UTC.  New traces always carry
        # an explicit trailing Z and do not enter this compatibility branch.
        return time.mktime(parsed.timetuple()) + parsed.microsecond / 1_000_000
    return parsed.timestamp()


def _trace_iso(timestamp):
    if not timestamp:
        return None
    return datetime.fromtimestamp(float(timestamp), timezone.utc).isoformat().replace("+00:00", "Z")


def _workflow_artifacts(run_dir, deck_status):
    """Return the small, user-facing file tree shown beside the DAG.

    The canvas is a view of production artifacts rather than a trace browser,
    so internal state, model messages, font bundles, and vendored libraries are
    deliberately excluded. Files appear as soon as they are committed on disk.
    """
    layout = WorkspaceLayout.load(run_dir)
    groups = []
    for group_id, label, paths in layout.group_files():
        files = []
        for path in paths:
            stat = path.stat()
            suffix = path.suffix.lower()
            kind = (
                "image" if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
                else "html" if suffix in {".html", ".htm"}
                else "markdown" if suffix == ".md"
                else "style" if suffix == ".css"
                else "data" if suffix == ".json"
                else "file"
            )
            files.append({"name": path.name, "rel": layout.relative(path), "kind": kind,
                          "size": stat.st_size, "updated_at": _trace_iso(stat.st_mtime)})
        groups.append({"id": group_id, "label": label, "files": files})
    return {"groups": groups, "file_count": sum(len(group["files"]) for group in groups),
            "locked": str(deck_status or "") != "completed"}


def _attach_absolute_event_times(agents, overall_timing):
    """Attach UTC timestamps to elapsed-only process events.

    Harness stdout historically carries a host-local ``HH:MM:SS`` clock.  That
    value has no timezone and must not be sent to the browser as an absolute
    time.  The persisted run start is UTC, so combine it with each monotonic
    elapsed value and expose an explicit ``Z`` timestamp instead.
    """
    started_at = _trace_timestamp((overall_timing or {}).get("started_at"))
    if started_at is None:
        return
    for events in agents.values():
        for event in events:
            try:
                elapsed_s = float(event.get("elapsed_s"))
            except (TypeError, ValueError):
                continue
            if elapsed_s < 0:
                continue
            event["ts"] = _trace_iso(started_at + elapsed_s)


def _workflow_payload(run_dir, page_agents, aliases, agent_timings, deck_status):
    """Project durable Owner executions into the visual workflow surface."""
    if not run_dir:
        return {
            "schema": "mural.workflow.v1", "runs": [], "issues": [],
            "artifacts": {"groups": [], "file_count": 0, "locked": True},
        }

    page_index = _read_trace_json(
        os.path.join(run_dir, "_state", "page-index.json"), {}
    )
    positions = {
        str(row.get("page_uid") or ""): int(row.get("position") or 0)
        for row in (page_index.get("pages") or [])
        if isinstance(row, dict) and int(row.get("position") or 0) > 0
    }
    config_paths = [
        os.path.join(run_dir, "_trace", "orchestrator", "config.json")
    ]
    config_paths.extend(sorted(glob.glob(os.path.join(
        run_dir, "_trace", "subagents", "*", "config.json"
    ))))
    runs = []
    for config_path in config_paths:
        config = _read_trace_json(config_path, {})
        if not isinstance(config, dict) or not config:
            continue
        role = str(config.get("role") or "").strip().lower()
        raw_label = str(
            config.get("label") or os.path.basename(os.path.dirname(config_path))
        )
        key = "orch" if role == "orchestrator" else _livefeed_agent_key(
            raw_label, aliases
        )
        page_uids = [
            str(uid) for uid in (config.get("assigned_page_uids") or []) if uid
        ]
        pages = sorted({positions.get(uid, 0) for uid in page_uids} - {0})
        if role == "orchestrator" or (role == "slide" and not pages):
            pages = sorted({
                int(page)
                for page, owner in (page_agents or {}).items()
                if owner == key and str(page).isdigit()
            })
        timing = (agent_timings or {}).get(key) or {}
        action = str(config.get("owner_action") or "").strip().lower()
        if not action:
            action = (
                "reuse_owner"
                if config.get("resume_of_segment_id") is not None
                else "create"
            )
        status = str(timing.get("status") or "waiting")
        if config.get("finished_at") or config.get("exit_reason"):
            status = "complete"
        runs.append({
            "key": key,
            "role": role or ("slide" if key.startswith("slide_") else key),
            "label": raw_label,
            "owner_id": str(config.get("owner_id") or ""),
            "group_id": str(config.get("owner_group_id") or ""),
            "scope_kind": str(config.get("owner_scope_kind") or ""),
            "owner_action": action,
            "pages": pages,
            "segment_id": config.get("segment_id"),
            "resume_of_segment_id": config.get("resume_of_segment_id"),
            "started_at": config.get("started_at"),
            "finished_at": config.get("finished_at"),
            "status": status,
            "exit_reason": str(config.get("exit_reason") or ""),
        })
    runs.sort(key=lambda row: (
        str(row.get("started_at") or ""),
        str(row.get("key") or ""),
        int(row.get("segment_id") or 0),
    ))

    runtime = _read_trace_json(
        os.path.join(run_dir, "_trace", "runtime-summary.json"), {}
    )
    issues = []
    for issue in ((runtime.get("issues") or {}).get("records") or []):
        if not isinstance(issue, dict):
            continue
        issue_pages = sorted({
            positions.get(str(uid), 0)
            for uid in (issue.get("page_uids") or [])
        } - {0})
        issues.append({
            "issue_id": str(issue.get("issue_id") or ""),
            "issue_type": str(issue.get("issue_type") or ""),
            "severity": str(issue.get("severity") or ""),
            "status": str(issue.get("status") or ""),
            "pages": issue_pages,
            "transition_count": int(issue.get("transition_count") or 0),
        })
    return {
        "schema": "mural.workflow.v1",
        "deck_status": str(deck_status or ""),
        "runs": runs,
        "issues": issues,
        "artifacts": _workflow_artifacts(run_dir, deck_status),
        "updated_at": runtime.get("updated_at"),
    }


def _trace_pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _agent_timing_payload(
    run_dir,
    aliases=None,
    deck_status=None,
    deck_started_at=None,
    deck_finished_at=None,
):
    """Return real per-Agent wall-clock boundaries persisted by the Harness.

    The regular WebDemo consumes runner logs through :func:`livefeed`, while
    the trajectory monitor already exposed these timestamps independently.
    Keeping the derivation here makes the primary UI receive the same timing
    data without changing the Harness or fabricating timestamps for old jobs.

    ``deck_status`` is the authoritative DB job status.  The per-Agent configs
    can NOT reliably signal "the whole run is done": the orchestrator never
    persists its own ``finished_at`` and ``present.html`` exists mid-run, so
    inferring the overall finish from ``max(child finished_at)`` freezes the
    "整体用时" the moment every currently-spawned child is momentarily complete
    (e.g. between the last Slide wave and Review).  When the DB says the deck is
    still running we therefore keep the overall clock open-ended and let the UI
    fall back to the live ``progress.elapsed_s`` job clock.
    """
    if not run_dir:
        return {}, None
    configs = [os.path.join(run_dir, "_trace", "orchestrator", "config.json")]
    configs.extend(sorted(glob.glob(os.path.join(run_dir, "_trace", "subagents", "*", "config.json"))))
    timings = {}
    for config_path in configs:
        try:
            with open(config_path, encoding="utf-8", errors="replace") as source:
                config = json.load(source)
            config_stat = os.stat(config_path)
        except (OSError, ValueError, TypeError):
            continue
        role_dir = os.path.dirname(config_path)
        raw_label = str(config.get("label") or os.path.basename(role_dir))
        key = _livefeed_agent_key(raw_label, aliases)
        started = _trace_timestamp(config.get("started_at"))
        if started is None:
            # A config mtime is reliable evidence that the Agent existed. It is
            # only used for historical Harness versions that omitted started_at.
            started = config_stat.st_mtime
        completion_paths = [
            os.path.join(role_dir, name)
            for name in ("messages.json", "tool_log.json", "summary.md", "usage.json", "handoff.json")
            if os.path.isfile(os.path.join(role_dir, name))
        ]
        config_finished = _trace_timestamp(config.get("finished_at"))
        complete = bool(config_finished) or any(
            os.path.basename(path) in {"summary.md", "usage.json", "handoff.json"}
            for path in completion_paths
        )
        running = not complete and _trace_pid_alive(config.get("pid"))
        artifact_finished = max(
            (os.path.getmtime(path) for path in completion_paths), default=0.0
        )
        finished = (
            max(config_finished or 0.0, artifact_finished) or None
            if complete
            else None
        )
        status = "complete" if complete else ("running" if running else "waiting")
        end = finished or (time.time() if running else None)
        candidate = {
            "status": status,
            "started_at": _trace_iso(started),
            "finished_at": _trace_iso(finished),
            "duration_s": round(max(0.0, end - started), 1) if end else None,
        }
        previous = timings.get(key)
        if previous:
            previous_start = _trace_timestamp(previous.get("started_at")) or started
            previous_finish = _trace_timestamp(previous.get("finished_at"))
            merged_start = min(previous_start, started)
            merged_finish = max(previous_finish or 0.0, finished or 0.0) or None
            merged_status = "running" if "running" in {previous.get("status"), status} else (
                "complete" if "complete" in {previous.get("status"), status} else "waiting"
            )
            merged_end = merged_finish or (time.time() if merged_status == "running" else None)
            candidate = {
                "status": merged_status,
                "started_at": _trace_iso(merged_start),
                "finished_at": _trace_iso(merged_finish),
                "duration_s": round(max(0.0, merged_end - merged_start), 1) if merged_end else None,
            }
        timings[key] = candidate
    known = [value for value in timings.values() if value.get("started_at")]
    if not known:
        return timings, None
    overall_start = min(_trace_timestamp(value["started_at"]) for value in known)
    # DB status is authoritative for "is the whole run done"; per-Agent configs
    # cannot be (orchestrator has no persisted finish, present.html exists early).
    status_l = str(deck_status or "").strip().lower()
    TERMINAL = {
        "completed", "failed", "rejected", "interrupted",
        "paused_recoverable", "not_started",
    }
    db_terminal = status_l in TERMINAL
    db_running = bool(status_l) and not db_terminal  # queued / running / ...
    trace_active = any(value.get("status") == "running" for value in known)
    # Still-running per DB → keep the overall clock open even when every
    # currently-spawned child is momentarily complete (Slide wave → Review gap).
    active = trace_active or db_running
    finishes = [_trace_timestamp(value.get("finished_at")) for value in known]
    complete_finishes = [value for value in finishes if value]
    if db_running:
        overall_finish = None
    else:
        overall_finish = None if active else (max(complete_finishes) if complete_finishes else None)
    # Prefer the DB's real end/start for a terminal deck so the frozen headline
    # matches the job clock instead of the last child's trace timestamp.
    if db_terminal:
        db_finish = _trace_timestamp(deck_finished_at)
        if db_finish:
            overall_finish = db_finish
        db_start = _trace_timestamp(deck_started_at)
        if db_start:
            overall_start = min(overall_start, db_start)
    overall_end = overall_finish or (time.time() if active else None)
    overall = {
        "started_at": _trace_iso(overall_start),
        "finished_at": _trace_iso(overall_finish),
        "duration_s": round(max(0.0, overall_end - overall_start), 1) if overall_end else None,
    }
    return timings, overall


def _apply_agent_exit_timings(chunk, timings, aliases=None):
    """Complete per-Agent clocks from the Harness's durable exit event.

    v0.1 writes ``messages.json`` and ``tool_log.json`` for both active and
    finished workers, so file presence cannot distinguish the two. The runner
    emits one explicit ``轨迹已写 ... exit=...`` line only after an Agent has
    stopped. Its elapsed value is Agent-local; combine it with that Agent's
    persisted start timestamp to recover an exact UTC finish boundary.
    """
    for line in str(chunk or "").splitlines():
        match = _AGENT_EXIT_RE.match(line)
        if not match:
            continue
        clock, elapsed_s, raw_agent, exit_reason = match.groups()
        key = _livefeed_agent_key(raw_agent, aliases)
        timing = timings.get(key)
        if not timing:
            continue
        started = _trace_timestamp(timing.get("started_at"))
        try:
            elapsed = float(elapsed_s)
        except (TypeError, ValueError):
            elapsed = None
        if started is None or elapsed is None or elapsed < 0:
            continue
        # The role directory is reused for a retry, so config.json describes
        # the latest attempt while the job log retains earlier exits. Prefer
        # the explicit runner wall clock to reject an exit that predates the
        # current attempt's start; elapsed alone is attempt-local and would
        # otherwise be added to the wrong start timestamp.
        finished = None
        if clock:
            try:
                hour, minute, second = (int(part) for part in clock.split(":"))
                start_dt = datetime.fromtimestamp(started, timezone.utc)
                candidates = [
                    start_dt.replace(hour=hour, minute=minute, second=second, microsecond=0).timestamp() + day * 86400
                    for day in (-1, 0, 1)
                ]
                expected = started + elapsed
                finished = min(candidates, key=lambda value: abs(value - expected))
            except (TypeError, ValueError):
                finished = None
        if finished is None:
            finished = started + elapsed
        if finished < started - 1:
            continue
        previous_finish = _trace_timestamp(timing.get("finished_at"))
        if previous_finish and previous_finish > finished:
            finished = previous_finish
        failed = bool(re.search(r"(?:fail|error|reject|incomplete|blocked)", exit_reason, re.I))
        timing.update({
            "status": "failed" if failed else "complete",
            "finished_at": _trace_iso(finished),
            "duration_s": round(max(0.0, finished - started), 1),
            "exit_reason": exit_reason,
        })
    return timings


def _promote_review_partial(timings, run_dir):
    """Mark the Review timing as partial when the run proves a durable bounded handoff.

    A Review with ``incomplete_closure`` is only partial when the overall run
    succeeded with ``needs_improvement`` quality and all durable-handoff
    conditions are met.  Genuine API/stale/missing-finalize failures stay failed.
    """
    if not run_dir:
        return
    result_path = os.path.join(run_dir, "result.json")
    try:
        with open(result_path, encoding="utf-8", errors="replace") as f:
            result = json.load(f)
    except (OSError, ValueError, TypeError):
        return
    if result.get("status") not in {"completed", "needs_improvement"}:
        return
    detail = result.get("accept_detail") or {}
    if not (
        detail.get("review_completed")
        and detail.get("finalize_succeeded")
        and detail.get("final_render_after_review")
        and detail.get("final_view_after_review")
        and detail.get("quality_status") == "needs_improvement"
        and not detail.get("unresolved_child_failures")
    ):
        return
    for key, timing in timings.items():
        if "review" in key.lower() and timing.get("status") == "failed":
            exit_reason = str(timing.get("exit_reason") or "").lower()
            if exit_reason in {"incomplete_closure", "needs_improvement"}:
                timing["status"] = "partial"


def _promote_image_terminal(timings, run_dir, aliases=None):
    """Prefer the durable Image handoff over an earlier raw runner exit line.

    Historical runs may log ``incomplete_closure`` before ``_run_child``
    recovers a finalized catalog.  The final config and image-handoff receipt
    are later and authoritative; showing the earlier line as 未完成 made #201,
    #215 and #218 look failed after successful recovery.
    """
    if not run_dir:
        return
    handoff = _read_trace_json(
        os.path.join(run_dir, "_trace", "image-handoff.json"), {}
    )
    if not isinstance(handoff, dict):
        handoff = {}
    schema = str(handoff.get("schema") or "")
    status = str(handoff.get("status") or "").strip().lower()
    execution = str(handoff.get("execution_status") or "").strip().lower()
    terminal = execution == "completed" or status in {
        "completed",
        "completed_with_fallback",
    }
    if schema == "mural.image-handoff.v1" and {
        "ready_pages", "failed_pages"
    }.issubset(handoff):
        terminal = True
        status = (
            "completed_with_fallback"
            if list(handoff.get("failed_pages") or [])
            else "completed"
        )

    final_exit = ""
    for config_path in glob.glob(
        os.path.join(run_dir, "_trace", "subagents", "*", "config.json")
    ):
        config = _read_trace_json(config_path, {})
        if not isinstance(config, dict):
            continue
        raw = os.path.basename(os.path.dirname(config_path))
        key = _livefeed_agent_key(
            str(config.get("label") or raw), aliases
        )
        if key != "image" and str(config.get("label") or "").lower() != "image":
            continue
        final_exit = str(config.get("exit_reason") or "").strip().lower()
        config_status = str(
            config.get("image_execution_status") or ""
        ).strip().lower()
        if config_status in {"completed", "completed_with_fallback"}:
            terminal = True
            status = config_status
        elif final_exit in {
            "completed",
            "completed_with_fallback",
            "partial_ready_handoff",
            "recovered_ready_handoff",
        }:
            terminal = True
            if not status:
                status = (
                    "completed_with_fallback"
                    if "partial" in final_exit
                    else "completed"
                )
    if not terminal:
        return
    status = status or "completed"
    for key, timing in timings.items():
        if key == "image" or key.startswith("image_"):
            timing["status"] = "complete"
            timing["completion_status"] = status
            timing["exit_reason"] = status


def _run_response_language(run_dir):
    """Best-effort response language for filtering user-facing process prose."""
    candidates = [
        str(WorkspaceLayout.load(run_dir).path("deck")),
        os.path.join(run_dir or "", "_trace", "orchestrator", "config.json"),
    ]
    for path in candidates:
        try:
            with open(path, encoding="utf-8", errors="replace") as source:
                text = source.read(16384)
        except OSError:
            continue
        lowered = text.lower()
        if re.search(r"response[_ -]?language\s*[:：]\s*(zh|中文|chinese)", lowered):
            return "zh"
        if re.search(r"response[_ -]?language\s*[:：]\s*(en|英文|english)", lowered):
            return "en"
        cjk = len(re.findall(r"[\u3400-\u9fff]", text))
        latin = len(re.findall(r"[A-Za-z]", text))
        if cjk or latin:
            return "zh" if cjk * 4 >= latin else "en"
    return None


def _visible_text_for_language(text, language):
    """Hide model-default-language prose from the localized process UI.

    Raw traces stay untouched.  Tool events and deterministic role/status UI still
    describe progress when a weaker model ignores the requested response language.
    """
    value = str(text or "").strip()
    if not value or language not in {"zh", "en"}:
        return value
    cjk = len(re.findall(r"[\u3400-\u9fff]", value))
    latin = len(re.findall(r"[A-Za-z]", value))
    if language == "zh" and not cjk and latin >= 8:
        return ""
    if language == "en" and cjk and cjk * 4 > max(latin, 1):
        return ""
    return value


def _append_subagent_summaries(
    agents, run_dir, next_sequence, aliases=None, response_language=None
):
    """Add each worker's durable final summary to its live activity stream.

    Runner logs intentionally keep only the first physical line of a multi-line
    final response.  The complete, user-facing summary is already persisted in
    ``summary.md``; merge that final result back into the stream without
    exposing raw model scratch messages.
    """
    trace_root = os.path.join(run_dir or "", "_trace", "subagents")
    if not os.path.isdir(trace_root):
        return
    for offset, agent_dir in enumerate(sorted(glob.glob(os.path.join(trace_root, "*")))):
        summary_path = os.path.join(agent_dir, "summary.md")
        try:
            with open(summary_path, encoding="utf-8", errors="replace") as source:
                summary = _visible_text_for_language(
                    source.read(2400), response_language
                )
        except OSError:
            continue
        if len(summary) < 12:
            continue
        key = _livefeed_agent_key(os.path.basename(agent_dir), aliases)
        # A continuation reuses the same workspace, so ``_trace/subagents`` may
        # still contain workers from earlier conversation turns.  Only enrich
        # agents that are present in this job log; otherwise an old Image Agent
        # is incorrectly moved into the newest assistant reply.
        if key not in agents:
            continue
        events = agents[key]
        text_events = [event for event in events if event.get("k") == "text"]
        last_text = text_events[-1] if text_events else None
        visible = str(last_text.get("s") or "").strip() if last_text else ""
        # The runner commonly logged only the first line. Replace that prefix
        # in-place so the final result keeps its real sequence position.
        if last_text and visible and summary.startswith(visible):
            last_text["s"] = summary
        elif not any(str(event.get("s") or "").strip() == summary for event in text_events):
            events.append({"k": "text", "s": summary, "seq": next_sequence + offset})


def _live_overall_timing(
    timings, deck_status=None, deck_started_at=None, deck_finished_at=None
):
    """Build the headline clock from live Agent lifecycle timestamps."""
    starts = [
        _trace_timestamp(item.get("started_at"))
        for item in (timings or {}).values()
        if item.get("started_at")
    ]
    starts = [value for value in starts if value is not None]
    db_start = _trace_timestamp(deck_started_at)
    if db_start is not None:
        starts.append(db_start)
    if not starts:
        return None
    started = min(starts)
    terminal = str(deck_status or "").lower() in {
        "completed", "failed", "rejected", "interrupted",
        "paused_recoverable", "not_started",
    }
    finished = _trace_timestamp(deck_finished_at) if terminal else None
    if finished is None and terminal:
        finishes = [
            _trace_timestamp(item.get("finished_at"))
            for item in (timings or {}).values()
            if item.get("finished_at")
        ]
        finishes = [value for value in finishes if value is not None]
        finished = max(finishes) if finishes else None
    end = finished if finished is not None else time.time()
    return {
        "started_at": _trace_iso(started),
        "finished_at": _trace_iso(finished),
        "duration_s": round(max(0.0, end - started), 1),
    }


def livefeed(
    log_path,
    max_bytes=8 * 1024 * 1024,
    per_agent=240,
    run_dir=None,
    deck_status=None,
    deck_started_at=None,
    deck_finished_at=None,
    live_cursor=0,
):
    """Tail the runner log -> agents plus grouped page ownership.

    ``page_agents`` 来自持久 Owner 登记；主 Agent 自制页映射到 ``orch``，
    委派页映射到对应 ``slide_group_*``。复核中的页码不改变制作归属。

    ``deck_status`` / ``deck_started_at`` / ``deck_finished_at`` come from the
    authoritative DB row so ``overall_timing`` does not falsely freeze while the
    run is still going (see :func:`_agent_timing_payload`).

    Event: {"k":"tool","tool":name,"hint":path-or-snippet} | {"k":"text","s":...}.
    Retry agents merge into their base key; specialist roles stay separate so the
    Studio can reconstruct the complete orchestration conversation."""
    agents = {}
    try:
        WorkspaceLayout.load(run_dir)
    except (ValueError, OSError, TypeError):
        return {"agents": {}, "page_agents": {}, "agent_keys": [], "agent_timings": {},
                "workspace_error": "unsupported_workspace_layout", "events": []}
    page_agents = _registry_page_agents(run_dir)
    aliases = _trace_agent_aliases(run_dir)
    agent_timings, overall_timing = _agent_timing_payload(
        run_dir,
        aliases,
        deck_status=deck_status,
        deck_started_at=deck_started_at,
        deck_finished_at=deck_finished_at,
    )
    workflow = _workflow_payload(
        run_dir, page_agents, aliases, agent_timings, deck_status
    )
    response_language = _run_response_language(run_dir)
    live_path = os.path.join(run_dir or "", "_trace", "live-events.jsonl")
    if os.path.isfile(live_path):
        live_payload = live_events(
            run_dir, cursor=live_cursor, limit=5000,
            response_language=response_language,
        )
        page_agents.update(live_payload.get("page_agents") or {})
        agent_timings.update(live_payload.get("agent_timings") or {})
        overall_timing = _live_overall_timing(
            agent_timings,
            deck_status=deck_status,
            deck_started_at=deck_started_at,
            deck_finished_at=deck_finished_at,
        ) or overall_timing
        return {
            "agents": live_payload.get("agents") or {},
            "agent_keys": live_payload.get("agent_keys") or [],
            "page_agents": page_agents,
            "agent_timings": agent_timings,
            "overall_timing": overall_timing,
            "workflow": workflow,
            "live_event_schema": _LIVE_EVENT_SCHEMA,
            "live_cursor": live_payload.get("cursor", 0),
            "live_next_cursor": live_payload.get("next_cursor", 0),
            "live_max_sequence": live_payload.get("max_sequence", 0),
            "live_has_more": live_payload.get("has_more", False),
            "live_cursor_reset": live_payload.get("cursor_reset", False),
        }
    if not log_path or not os.path.exists(log_path):
        return {
            "agents": agents,
            "page_agents": page_agents,
            "agent_timings": agent_timings,
            "overall_timing": overall_timing,
            "workflow": workflow,
        }
    try:
        size = os.path.getsize(log_path)
        with open(log_path, "rb") as f:
            f.seek(max(0, size - max_bytes))
            chunk = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return {
            "agents": agents,
            "page_agents": page_agents,
            "agent_timings": agent_timings,
            "overall_timing": overall_timing,
            "workflow": workflow,
        }
    _apply_agent_exit_timings(chunk, agent_timings, aliases)
    _promote_image_terminal(agent_timings, run_dir, aliases)
    _promote_review_partial(agent_timings, run_dir)
    for sequence, line in enumerate(chunk.split("\n")):
        m = _FEED_RE.match(line)
        if not m:
            continue
        clock, elapsed_s, raw_agent, tool, args, text = m.groups()
        key = _livefeed_agent_key(raw_agent, aliases)
        if key.startswith("slide_group_"):
            for page in _group_event_pages(args or ""):
                page_agents.setdefault(str(page), key)
        lst = agents.setdefault(key, [])
        if tool:
            hm = _HINT_RE.search(args or "")
            hint = hm.group(1) if hm else (args or "")[:48]
            event = {"k": "tool", "tool": tool, "hint": hint, "seq": sequence}
            if clock:
                event["clock"] = clock
            if elapsed_s is not None:
                event["elapsed_s"] = float(elapsed_s)
            lst.append(event)
        else:
            t = _visible_text_for_language(text, response_language)
            # Old live logs and revision logs may contain the same progress
            # under both ``orch`` and ``orchestrator``.  They now share ``key``;
            # collapse an exact adjacent duplicate before it reaches the UI.
            if t and not (
                lst and lst[-1].get("k") == "text"
                and str(lst[-1].get("s") or "").strip() == t[:800].strip()
            ):
                event = {"k": "text", "s": t[:800], "seq": sequence}
                if clock:
                    event["clock"] = clock
                if elapsed_s is not None:
                    event["elapsed_s"] = float(elapsed_s)
                lst.append(event)
    # stdout keeps the UI responsive before the first model response lands;
    # once the atomic trace exists, prefer its complete untruncated turns.
    for key, events in _live_trace_events(
        run_dir, aliases, response_language, agents
    ).items():
        agents[key] = events
    _append_subagent_summaries(
        agents, run_dir, len(chunk.split("\n")) + 1, aliases, response_language
    )
    _attach_absolute_event_times(agents, overall_timing)
    for k in agents:
        agents[k] = agents[k][-per_agent:]
    return {
        "agents": agents,
        "page_agents": page_agents,
        "agent_timings": agent_timings,
        "overall_timing": overall_timing,
        "workflow": workflow,
    }


def specialist_artifacts(run_dir):
    """Small, user-facing artifact index for the 00 orchestration view.

    The browser receives only relative paths and mtimes.  Image bytes are served
    through the authenticated thumbnail endpoint, so polling the live feed never
    transfers the multi-megabyte originals produced by the Image Agent.
    """
    result = {
        "research": {"brief_ready": False, "queries": [], "sources": []},
        "material": {"count": 0, "files": []},
        "image": {"catalog_ready": False, "count": 0, "contact_sheet": None, "images": []},
        # Per-agent indexes prevent the orchestration view from attaching the
        # deck-wide gallery to every Image Agent card.  Older frontends can
        # continue to consume the role-level ``image`` entry above.
        "agents": {},
    }
    if not run_dir:
        return result
    try:
        research = WorkspaceLayout.load(run_dir).path("knowledge")
    except (ValueError, OSError):
        result["workspace_error"] = "unsupported_workspace_layout"
        return result
    result["research"]["brief_ready"] = research.is_file() and research.stat().st_size > 0
    aliases = _trace_agent_aliases(run_dir)
    task_state = _read_trace_json(os.path.join(run_dir, "_state", "orchestration.json"), {})
    for task in (task_state.get("tasks") or {}).values():
        relative = str(task.get("output_path") or "")
        if task.get("role") != "research" or not relative.startswith("research/"):
            continue
        path = Path(run_dir) / relative
        if not path.resolve().is_relative_to(Path(run_dir).resolve()) or path.is_symlink():
            continue
        agent = _livefeed_agent_key(str(task.get("owner_id") or ""), aliases)
        result["agents"][agent] = {"output_path": relative,
            "output_recorded": path.is_file(), "queries": [], "sources": []}
    trace_root = os.path.join(run_dir, "_trace", "subagents")
    for agent_dir in sorted(glob.glob(os.path.join(trace_root, "*"))):
        if not os.path.isdir(agent_dir):
            continue
        raw_agent = os.path.basename(agent_dir)
        agent = _livefeed_agent_key(raw_agent, aliases)
        role = agent.split("_", 1)[0]
        config = {}
        try:
            with open(os.path.join(agent_dir, "config.json"), encoding="utf-8", errors="replace") as source:
                config = json.load(source)
        except (OSError, ValueError, TypeError):
            pass
        if role == "research":
            queries = []
            try:
                with open(os.path.join(agent_dir, "tool_log.json"), encoding="utf-8", errors="replace") as source:
                    tool_log = json.load(source)
                for event in tool_log if isinstance(tool_log, list) else []:
                    if event.get("name") != "web_search":
                        continue
                    query = str((event.get("args") or {}).get("query") or "").strip()
                    if query and query not in queries:
                        queries.append(query)
            except (OSError, ValueError, TypeError):
                pass
            payload = ""
            for filename in ("messages.json", "summary.md"):
                path = os.path.join(agent_dir, filename)
                try:
                    if os.path.getsize(path) <= 8 * 1024 * 1024:
                        with open(path, encoding="utf-8", errors="replace") as source:
                            if filename.endswith(".json"):
                                payload += "\n" + "\n".join(_json_text_values(json.load(source)))
                            else:
                                payload += "\n" + source.read()
                except OSError:
                    continue
                except (ValueError, TypeError):
                    continue
            sources = []
            seen_urls = set()
            titled = re.compile(r"-\s+([^\n]{2,180})\n\s*(https?://[^\s\"'<>]+)", re.I)
            for match in titled.finditer(payload):
                title = re.sub(r"\\[nrt]", " ", match.group(1)).strip(" -")
                url = match.group(2).rstrip(".,;，；")
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                sources.append({"title": title, "url": url})
            for url in re.findall(r"https?://[^\\\s\"'<>]+", payload, re.I):
                url = url.rstrip(".,;，；")
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                sources.append({"title": re.sub(r"^https?://", "", url).split("/", 1)[0], "url": url})
            artifact = {
                **result["agents"].get(agent, {}),
                "queries": queries[:24],
                "sources": sources[:24],
            }
            result["agents"][agent] = artifact
            for query in artifact["queries"]:
                if query not in result["research"]["queries"]:
                    result["research"]["queries"].append(query)
            known = {item["url"] for item in result["research"]["sources"]}
            result["research"]["sources"].extend(
                item for item in artifact["sources"] if item["url"] not in known
            )
        elif role == "material":
            task = str(config.get("task") or config.get("goal") or "")
            names = []
            for match in re.findall(
                r"[^\s\"'`<>]+?\.(?:pdf|docx?|pptx?|xlsx?|csv|txt|md|png|jpe?g|webp)",
                task,
                re.I,
            ):
                name = os.path.basename(match.rstrip(".,;，；)"))
                if name and name not in names:
                    names.append(name)
            files = [{"name": name, "status": "processing"} for name in names]
            result["agents"][agent] = {"count": len(files), "files": files}

    material_files = []
    for catalog_path in glob.glob(os.path.join(run_dir, "materials", "**", "catalog.json"), recursive=True):
        try:
            with open(catalog_path, encoding="utf-8", errors="replace") as source:
                catalog = json.load(source)
        except (OSError, ValueError, TypeError):
            continue
        entries = catalog.get("entries", []) if isinstance(catalog, dict) else catalog
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or entry.get("from_scanned_pdf"):
                continue
            coverage = entry.get("coverage") or ""
            item = {
                "name": str(entry.get("name") or os.path.basename(str(entry.get("raw") or "material"))),
                "kind": str(entry.get("kind") or "file"),
                "status": str(entry.get("status") or "unknown"),
                "coverage": str(coverage.get("status") or "") if isinstance(coverage, dict) else str(coverage),
            }
            if item not in material_files:
                material_files.append(item)
    result["material"] = {"count": len(material_files), "files": material_files}
    material_agents = [key for key in result["agents"] if key.startswith("material")]
    if material_files:
        if len(material_agents) == 1:
            result["agents"][material_agents[0]] = result["material"]
        elif not material_agents:
            result["agents"]["material"] = result["material"]
    asset_dir = os.path.join(run_dir, "assets")
    result["image"]["catalog_ready"] = any(
        os.path.isfile(os.path.join(asset_dir, name))
        for name in ("catalog.json", "catalog.md")
    )
    if not os.path.isdir(asset_dir):
        return result

    provenance = {}
    has_asset_status_contract = False
    try:
        with open(os.path.join(asset_dir, "catalog.json"), encoding="utf-8", errors="replace") as source:
            asset_catalog = json.load(source)
        entries = asset_catalog.get("assets", []) if isinstance(asset_catalog, dict) else []
        provenance = {
            str(item.get("path")): item for item in entries
            if isinstance(item, dict) and item.get("path")
        }
        has_asset_status_contract = any(
            isinstance(item, dict) and (item.get("asset_id") or "status" in item)
            for item in entries
        )
    except (OSError, ValueError, TypeError):
        pass

    raster_exts = {".png", ".jpg", ".jpeg", ".webp"}
    raw_images = []
    contact_sheets = []
    for path in glob.glob(os.path.join(asset_dir, "*")):
        if not os.path.isfile(path) or os.path.splitext(path)[1].lower() not in raster_exts:
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        item = {
            "path": "assets/" + os.path.basename(path),
            "name": os.path.basename(path),
            "mtime": int(stat.st_mtime),
        }
        meta = provenance.get(item["path"], {})
        if isinstance(meta, dict):
            for key in (
                "origin", "source_url", "source_path", "generator_model",
                "parent_asset", "source_origin", "aspect_ratio", "asset_id",
                "group_id", "status",
            ):
                if meta.get(key):
                    item[key] = meta[key]
        basename = os.path.basename(path).lower()
        if basename == "contact-sheet.png" or basename.startswith("contact-sheet-"):
            contact_sheets.append(item)
        else:
            item["_full_path"] = path
            item["_stat"] = stat
            raw_images.append(item)

    # Collapse byte-identical aliases while retaining every path in the
    # lookup below. Prefer a human-readable semantic filename over the
    # image_generate hash, and merge provenance from the catalogued original.
    by_size = {}
    for item in raw_images:
        by_size.setdefault(item["_stat"].st_size, []).append(item)
    duplicate_groups = {}
    for size, same_size in by_size.items():
        if len(same_size) == 1:
            item = same_size[0]
            duplicate_groups[(size, item["path"])] = [item]
            continue
        for item in same_size:
            try:
                digest = _asset_digest(item["_full_path"], item["_stat"])
            except OSError:
                digest = item["path"]
            duplicate_groups.setdefault((size, digest), []).append(item)

    image_aliases = {}
    images = []
    generated_hash_name = re.compile(r"^img_[0-9a-f]{8,}\.(?:png|jpe?g|webp)$", re.I)
    provenance_keys = (
        "origin", "source_url", "source_path", "generator_model",
        "parent_asset", "source_origin", "aspect_ratio", "asset_id",
        "group_id", "status",
    )
    for group in duplicate_groups.values():
        canonical = max(group, key=lambda item: (
            not bool(generated_hash_name.match(item["name"])),
            sum(bool(item.get(key)) for key in provenance_keys),
            item["mtime"],
        ))
        for item in group:
            for key in provenance_keys:
                if not canonical.get(key) and item.get(key):
                    canonical[key] = item[key]
        canonical.pop("_full_path", None)
        canonical.pop("_stat", None)
        images.append(canonical)
        for item in group:
            image_aliases[item["path"]] = canonical
    images.sort(key=lambda item: (item["mtime"], item["name"]))
    if contact_sheets:
        contact_sheets.sort(key=lambda item: (item["mtime"], item["name"]))
        result["image"]["contact_sheet"] = contact_sheets[-1]
    ready_ids = {
        str(item.get("asset_id")) for item in images
        if item.get("status") == "ready" and item.get("asset_id")
    }
    result["image"]["count"] = len(ready_ids) if has_asset_status_contract else len(images)
    # Keep the complete asset set.  The browser requests lazy thumbnails, so
    # dropping older items here only makes the material timeline misleading:
    # once a contact sheet exists the user still needs to see every source
    # image that was collected/generated before the whole-set review.
    result["image"]["images"] = images

    image_items = {item["path"]: item for item in images}
    image_items.update(image_aliases)
    for sheet in contact_sheets:
        image_items[sheet["path"]] = sheet

    image_path_pattern = re.compile(
        r"assets/[^\s\"'`<>()?#[\]{}]+?\.(?:png|jpe?g|webp)", re.I
    )

    def image_paths(value):
        """Extract workspace image paths from one structured trace value."""
        found = []
        if isinstance(value, str):
            for match in image_path_pattern.finditer(value):
                rel = match.group(0).replace("\\", "/")
                if rel not in found:
                    found.append(rel)
        elif isinstance(value, list):
            for entry in value:
                for rel in image_paths(entry):
                    if rel not in found:
                        found.append(rel)
        elif isinstance(value, dict):
            for entry in value.values():
                for rel in image_paths(entry):
                    if rel not in found:
                        found.append(rel)
        return found

    def generated_paths_from_messages(trace_path):
        """Return only image-tool outputs, excluding files merely read by an agent.

        Image agents often inspect the deck-wide ``assets/catalog.json``.  A raw
        regex over the complete conversation consequently attributes every
        catalog entry to the last agent that read it.  Tool-use/result linkage is
        the authoritative ownership signal: keep paths returned by that agent's
        image generation/search tools and ignore unrelated read-file results.
        """
        try:
            if os.path.getsize(trace_path) > 8 * 1024 * 1024:
                return [], False
            with open(trace_path, "r", encoding="utf-8", errors="replace") as source:
                messages = json.load(source)
        except (OSError, ValueError, TypeError):
            return [], False
        if not isinstance(messages, list):
            return [], False

        image_tool_ids = set()
        for message in messages:
            content = message.get("content") if isinstance(message, dict) else None
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = str(block.get("name") or "").lower()
                if name in {
                    "image_generate",
                    "image_search",
                    "image_download",
                    # The current long-horizon image role downloads selected
                    # search results with fetch_image.  Treat its linked result
                    # as first-class ownership evidence; otherwise this agent
                    # falls back to scanning messages and inherits every path
                    # from a deck-wide catalog it happened to read.
                    "fetch_image",
                }:
                    tool_id = str(block.get("id") or "")
                    if tool_id:
                        image_tool_ids.add(tool_id)

        paths = []
        if image_tool_ids:
            for message in messages:
                content = message.get("content") if isinstance(message, dict) else None
                for block in content if isinstance(content, list) else []:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    if str(block.get("tool_use_id") or "") not in image_tool_ids:
                        continue
                    for rel in image_paths(block.get("content")):
                        if rel not in paths:
                            paths.append(rel)

            # A contact sheet can be assembled after generation without another
            # image tool call.  Keep that one explicit review artifact when the
            # agent's final response names it.
            for message in messages:
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    continue
                for rel in image_paths(message.get("content")):
                    name = os.path.basename(rel).lower()
                    if (name == "contact-sheet.png" or name.startswith("contact-sheet-")) and rel not in paths:
                        paths.append(rel)
        return paths, bool(image_tool_ids)

    per_agent_paths = {}
    for agent_dir in sorted(glob.glob(os.path.join(trace_root, "*"))):
        if not os.path.isdir(agent_dir):
            continue
        raw_agent = os.path.basename(agent_dir)
        agent = _livefeed_agent_key(raw_agent, aliases)
        if not agent.startswith("image"):
            continue
        paths = per_agent_paths.setdefault(agent, [])
        message_path = os.path.join(agent_dir, "messages.json")
        generated_paths, has_image_tools = generated_paths_from_messages(message_path)
        if has_image_tools:
            for rel in generated_paths:
                if rel in image_items and rel not in paths:
                    paths.append(rel)
            continue

        # Compatibility path for older traces that did not preserve tool-use
        # IDs.  Those runs can only be reconstructed from the paths mentioned in
        # their compact logs/summaries.
        for filename in ("tool_log.json", "messages.json"):
            trace_path = os.path.join(agent_dir, filename)
            try:
                if os.path.getsize(trace_path) > 2 * 1024 * 1024:
                    continue
                with open(trace_path, "r", encoding="utf-8", errors="ignore") as stream:
                    payload = stream.read()
            except OSError:
                continue
            for match in image_path_pattern.finditer(payload):
                rel = match.group(0).replace("\\", "/")
                if rel in image_items and rel not in paths:
                    paths.append(rel)

    # Real images are often downloaded via a shell/terminal call rather than a
    # first-class image tool (image_generate/fetch_image), so tool-use linkage
    # above misses them and the per-agent card collapses to just the generated
    # asset (see deck-9: 1 generated shown, 3 fetched real images dropped).
    # image-download-receipts.json is the Image role's authoritative download
    # ledger; attribute each receipted asset to the Image agent whose trace text
    # actually names that path (single Image agent → all; multiple children →
    # disambiguated by mention). This complements, not replaces, tool linkage.
    receipts_path = os.path.join(run_dir or "", "_trace", "image-download-receipts.json")
    receipt_assets = []
    try:
        with open(receipts_path, encoding="utf-8", errors="replace") as source:
            receipts = json.load(source)
        for entry in (receipts.get("assets") or {}).values():
            rel = str(entry.get("path") or "").replace("\\", "/")
            if rel and rel in image_items:
                receipt_assets.append(rel)
    except (OSError, ValueError, TypeError):
        receipt_assets = []
    if receipt_assets and per_agent_paths:
        image_agents = list(per_agent_paths.keys())
        agent_trace_text = {}
        for agent in image_agents:
            agent_dir = next(
                (
                    path for path in glob.glob(os.path.join(trace_root, "*"))
                    if os.path.isdir(path)
                    and _livefeed_agent_key(os.path.basename(path), aliases) == agent
                ),
                "",
            )
            text = ""
            for filename in ("tool_log.json", "messages.json"):
                trace_path = os.path.join(agent_dir, filename)
                try:
                    if os.path.getsize(trace_path) > 4 * 1024 * 1024:
                        continue
                    with open(trace_path, encoding="utf-8", errors="ignore") as stream:
                        text += stream.read()
                except OSError:
                    continue
            agent_trace_text[agent] = text
        for rel in receipt_assets:
            if len(image_agents) == 1:
                owners = image_agents
            else:
                owners = [a for a in image_agents if rel in agent_trace_text.get(a, "")]
            for agent in owners:
                if rel not in per_agent_paths[agent]:
                    per_agent_paths[agent].append(rel)

    for agent, paths in per_agent_paths.items():
        scoped_images = []
        scoped_seen = set()
        for path in paths:
            item = image_items.get(path)
            if not item or item["path"] in scoped_seen:
                continue
            scoped_seen.add(item["path"])
            scoped_images.append(item)
        contact_sheet = next(
            (
                item for item in reversed(scoped_images)
                if item["name"].lower() == "contact-sheet.png"
                or item["name"].lower().startswith("contact-sheet-")
            ),
            None,
        )
        scoped_images = [
            item for item in scoped_images
            if item["name"].lower() != "contact-sheet.png"
            and not item["name"].lower().startswith("contact-sheet-")
        ]
        scoped_ready_ids = {
            str(item.get("asset_id")) for item in scoped_images
            if item.get("status") == "ready" and item.get("asset_id")
        }
        agent_trace_dir = next(
            (
                path for path in glob.glob(os.path.join(trace_root, "*"))
                if os.path.isdir(path)
                and _livefeed_agent_key(os.path.basename(path), aliases) == agent
            ),
            "",
        )
        result["agents"][agent] = {
            "catalog_ready": result["image"]["catalog_ready"],
            # summary.md is written only after that concrete Image child has
            # actually stopped.  A deck-wide catalog may already exist while
            # another Image child is still running, so it cannot mark every
            # card complete.
            "completed": bool(
                agent_trace_dir
                and os.path.isfile(os.path.join(agent_trace_dir, "summary.md"))
            ),
            "count": len(scoped_ready_ids) if has_asset_status_contract else len(scoped_images),
            "contact_sheet": contact_sheet,
            "images": scoped_images,
        }
    recorded = _read_trace_json(os.path.join(run_dir, "_state", "artifacts.json"), {})
    if isinstance(recorded.get("artifacts"), dict):
        # 当前 Harness 有真实素材归属时，不用文件名或聊天提及来猜作者。
        by_owner = {}
        for record in recorded["artifacts"].values():
            if record.get("kind") != "image":
                continue
            agent = _livefeed_agent_key(str(record.get("owner_id") or ""), aliases)
            item = image_items.get(str(record.get("path") or ""))
            if item:
                by_owner.setdefault(agent, []).append({**item, "path": record["path"],
                    "name": os.path.basename(record["path"]), "asset_id": record["artifact_id"], "status": record["status"]})
        for agent in {key for key in result["agents"] if key.startswith("image")} | set(by_owner):
            images = by_owner.get(agent, [])
            result["agents"][agent] = {**result["agents"].get(agent, {}), "images": images, "count": len(images)}
    return result


def scoped_specialist_artifacts(run_dir, agent_keys):
    """Return specialist artifacts belonging to one archived execution turn.

    In-place revisions intentionally keep generated assets and traces in one
    workspace.  The complete artifact index therefore spans several turns;
    scope it to the agents found in the corresponding immutable job log before
    exposing it in the conversation timeline.
    """
    artifacts = specialist_artifacts(run_dir)
    wanted = {str(key) for key in (agent_keys or [])}
    agents = artifacts.get("agents") if isinstance(artifacts.get("agents"), dict) else {}
    artifacts["agents"] = {
        key: value for key, value in agents.items() if key in wanted
    }
    roles = {key.split("_", 1)[0].split("-", 1)[0] for key in wanted}
    if "research" not in roles:
        artifacts["research"] = {"brief_ready": False, "queries": [], "sources": []}
    if "material" not in roles:
        artifacts["material"] = {"count": 0, "files": []}
    if "image" not in roles:
        artifacts["image"] = {
            "catalog_ready": False, "count": 0, "contact_sheet": None, "images": []
        }
    return artifacts


# ---- 页级视觉迭代历史：真实 Vision 快照 + 判断 + 后续修改 -----------------
_TRACE_VISION_RE = re.compile(r"🔧\s+vision_analyze\((.*)\)\s*$", re.S)
_TRACE_RENDER_RE = re.compile(r"deck\.py\s+render\s+\.\s+--page\s+0?(\d+)\b")
_TRACE_PAGE_IMAGE_RE = re.compile(r"(?:slide_|page_)0?(\d+)\.png\b", re.I)
_TRACE_CHAT_RE = re.compile(r"(?:\[\d+\]\s+)?💬\s+(.*)", re.S)
_TRACE_TOOL_RE = re.compile(r"🔧\s+(patch|edit|write_file)\((.*)\)\s*$", re.S)
_SPEECH_HEADING_RE = re.compile(
    r"^#{1,3}\s+(?:(?:slide|page)\s*0*(?P<western>\d+)|第\s*0*(?P<cjk>\d+)\s*页)"
    r"(?:\s*[—–:：|｜·-]\s*(?P<title>[^\n]+))?\s*$",
    re.I | re.M,
)
_SPEECH_EVIDENCE_RE = re.compile(
    r"^#{2,4}\s+(?:evidence\s+and\s+sources|证据(?:与来源)?|来源(?:与证据)?|"
    r"参考资料(?:[（(]不朗读[）)])?)\s*$",
    re.I | re.M,
)
_SPEECH_CONTENT_RE = re.compile(
    r"^#{2,4}\s+(?:讲述内容|演讲稿|speaker\s+notes?)\s*$", re.I | re.M)


def _trace_arg(payload, key):
    """Best-effort extraction from a possibly truncated JSON tool call."""
    match = re.search(rf'"{re.escape(key)}"\s*:\s*"((?:\\.|[^"\\])*)"', payload or "", re.S)
    if not match:
        # Trace messages intentionally cap long tool arguments.  Preserve the
        # visible prefix so revision summaries can still identify selectors and
        # CSS properties even when the closing JSON quote was truncated.
        prefix = re.search(rf'"{re.escape(key)}"\s*:\s*"(.*)', payload or "", re.S)
        if not prefix:
            return ""
        return prefix.group(1).replace(r"\n", "\n").replace(r'\"', '"').rstrip(")")
    try:
        return json.loads('"' + match.group(1) + '"')
    except (TypeError, ValueError, json.JSONDecodeError):
        return match.group(1).replace(r"\n", " ").replace(r'\"', '"')


def _clean_history_text(value, limit=700):
    text = str(value or "").strip()
    text = re.sub(r"\[context compacted\].*", "", text, flags=re.I | re.S)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _review_page_excerpt(text, page):
    """Pull P01's paragraph from an all-deck review summary when available."""
    pattern = re.compile(
        rf"(?:^|\n)\s*-?\s*\*\*P0?{page}\b.*?\*\*\s*[：:]?\s*(.*?)"
        rf"(?=\n\s*-?\s*\*\*P\d+\b|\Z)", re.S | re.I)
    match = pattern.search(text or "")
    if match:
        return _clean_history_text(match.group(1))
    # v3 review agents usually write ``slide_12`` rather than ``P12``. Keep
    # the matching paragraph only, instead of showing the whole deck review.
    slide_ref = re.compile(rf"\bslide[_\s-]*0?{page}\b", re.I)
    for paragraph in re.split(r"\n\s*\n", text or ""):
        if slide_ref.search(paragraph):
            return _clean_history_text(paragraph)
    return ""


def _history_image_rel(run_dir, label, view_no):
    roots = []
    canonical = _canonical_agent_key(label)
    if canonical == "orch":
        # The on-disk directory remains ``orchestrator`` for compatibility;
        # only the public identity is canonicalized to ``orch``.
        roots.extend([
            os.path.join(run_dir, "_trace", "orchestrator"),
            os.path.join(run_dir, "_trace", "orch"),
        ])
    roots.extend([
        os.path.join(run_dir, "_trace", "subagents", label),
        os.path.join(run_dir, "_trace", label),
    ])
    for root in roots:
        for name in (f"view_{view_no:03d}.png", f"view_{view_no:02d}.png", f"view_{view_no}.png"):
            path = os.path.join(root, "images", name)
            if os.path.isfile(path):
                return os.path.relpath(path, run_dir).replace(os.sep, "/")
    return ""


def _change_note(tool_name, payload):
    """Turn an observable file edit into a short, user-facing revision note.

    The harness does not persist a separate Vision prose response: the model sees
    the PNG and immediately edits the slide.  We therefore describe the actual
    edits made after that inspection instead of exposing model scratchpad text.
    """
    if tool_name == "write_file":
        return "重写本页 HTML 结构与样式"
    old = _trace_arg(payload, "old_string")
    new = _trace_arg(payload, "new_string")
    joined = f"{old}\n{new}"
    selectors = []
    for match in re.finditer(r"(?:^|\n)\s*([^\n{}]{1,90})\s*\{", joined):
        selector = re.sub(r"\s+", " ", match.group(1)).strip()
        if (selector.startswith((".", "#")) or " #" in selector or " ." in selector) \
                and selector not in selectors:
            selectors.append(selector)
    props = []
    old_props = dict(re.findall(r"(?:^|[;{]\s*|\n\s*)([-\w]+)\s*:\s*([^;\n}]+)", old))
    new_props = dict(re.findall(r"(?:^|[;{]\s*|\n\s*)([-\w]+)\s*:\s*([^;\n}]+)", new))
    for prop in dict.fromkeys([*old_props, *new_props]):
        if old_props.get(prop) != new_props.get(prop):
            props.append(prop)
    target = selectors[0] if selectors else "页面内容"
    if props:
        return f"调整 {target} 的{'、'.join(props[:4])}"
    if re.search(r"<[^>]+>", joined):
        classes = re.findall(r'class=\\?"([^"\\]+)', joined)
        if classes:
            return f"调整 {classes[-1].split()[0]} 区域的结构与内容"
        return "调整页面结构与内容"
    return "微调本页视觉细节"


def page_speech(run_dir, page):
    """Return the optional manifest-mapped speech section for one slide."""
    try:
        page = int(page)
    except (TypeError, ValueError):
        return {"exists": False, "page": 0}
    if page <= 0:
        return {"exists": False, "page": page}

    speech_path = WorkspaceLayout.load(run_dir).path("speech")
    if not speech_path.is_file():
        return {"exists": False, "page": page}
    try:
        with open(speech_path, encoding="utf-8", errors="replace") as speech_file:
            text = speech_file.read()
    except OSError:
        return {"exists": False, "page": page}

    headings = list(_SPEECH_HEADING_RE.finditer(text))
    for index, heading in enumerate(headings):
        number = int(heading.group("western") or heading.group("cjk") or 0)
        if number != page:
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        body = text[heading.end():end].strip()
        evidence_match = _SPEECH_EVIDENCE_RE.search(body)
        notes = body[:evidence_match.start()].strip() if evidence_match else body
        evidence = body[evidence_match.end():].strip() if evidence_match else ""
        # v3 emits a page heading followed by ``## 讲述内容``.  That structural
        # heading is useful in the artifact but should not be repeated in the UI.
        notes = _SPEECH_CONTENT_RE.sub("", notes, count=1).strip()
        return {
            "exists": True,
            "page": page,
            "title": (heading.group("title") or "").strip(),
            "notes": notes,
            "evidence": evidence,
            "source": os.path.relpath(speech_path, run_dir).replace(os.sep, "/"),
        }
    return {"exists": False, "page": page, "source": os.path.relpath(speech_path, run_dir).replace(os.sep, "/")}


def _read_trace_json(path, default):
    try:
        with open(path, encoding="utf-8", errors="replace") as source:
            value = json.load(source)
        return value
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return default


def _assistant_visible_text(message):
    """Return only user-visible assistant text from a persisted message.

    The message trace may also contain tool calls and private reasoning blocks.
    Page-history cards should expose the assistant's stated visual verdict, not
    implementation payloads or hidden scratchpad content.
    """
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") not in {"text", "output_text"}:
            continue
        text = str(block.get("text") or "").strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts).strip()


def _trace_result_shot(value):
    """Return the exact persisted screenshot referenced by a tool result."""
    if isinstance(value, dict):
        shot = value.get("shot")
        if isinstance(shot, str) and shot.strip():
            return shot.strip()
        for child in value.values():
            found = _trace_result_shot(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _trace_result_shot(child)
            if found:
                return found
    return ""


def _v3_message_image_rel(run_dir, agent_dir, shot):
    """Resolve a message ``shot`` safely to a Deck-relative trace path."""
    shot = str(shot or "").strip().replace("\\", "/")
    if not shot:
        return ""
    candidate = os.path.realpath(os.path.join(agent_dir, shot))
    agent_root = os.path.realpath(agent_dir)
    try:
        if os.path.commonpath([candidate, agent_root]) != agent_root:
            return ""
    except ValueError:
        return ""
    if not os.path.isfile(candidate):
        return ""
    return os.path.relpath(candidate, run_dir).replace(os.sep, "/")


def _v3_vision_judgments(agent_dir):
    """Recover the visible verdict following each v3 ``vision_analyze`` call.

    ``tool_log.json`` records the image and question, while the actual verdict
    is persisted in ``messages.json`` after the corresponding tool result. Keep
    the result ordered exactly like the tool log so callers can join the two
    immutable traces without relying on mutable render filenames.
    """
    messages = _read_trace_json(os.path.join(agent_dir, "messages.json"), [])
    if not isinstance(messages, list):
        return []

    calls = []
    by_id = {}
    waiting = []
    is_review = bool(re.search(r"(?:^|[-_])review(?:[-_]|$)", os.path.basename(agent_dir)))
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        blocks = content if isinstance(content, list) else []

        if role == "assistant":
            visible = _assistant_visible_text(message)
            if visible and waiting:
                referenced_pages = {
                    int(value) for value in re.findall(
                        r"(?:\bslide[_\s-]*|\bP0?|第\s*)(\d{1,3})(?:\s*页)?\b", visible, re.I)
                }
                for call in waiting:
                    page = call.get("page") or 0
                    excerpt = _review_page_excerpt(visible, page) if is_review and page else ""
                    if is_review and len(waiting) > 1 and referenced_pages and page not in referenced_pages:
                        call["judgment"] = "本轮批量视觉检查已完成，未单独指出本页问题。"
                    else:
                        call["judgment"] = _clean_history_text(excerpt or visible)
                waiting = []

            for block in blocks:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                if block.get("name") != "vision_analyze":
                    continue
                args = block.get("input") if isinstance(block.get("input"), dict) else {}
                image_path = str(args.get("image_url") or args.get("path") or "")
                page_match = _TRACE_PAGE_IMAGE_RE.search(image_path)
                call = {
                    "id": str(block.get("id") or ""),
                    "page": int(page_match.group(1)) if page_match else 0,
                    "judgment": "",
                    "shot": "",
                }
                calls.append(call)
                if call["id"]:
                    by_id[call["id"]] = call
            continue

        if role != "user":
            continue
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            call = by_id.get(str(block.get("tool_use_id") or ""))
            if call is not None:
                call["shot"] = _trace_result_shot(block)
                if call not in waiting:
                    waiting.append(call)
    return calls


def _v3_page_history(run_dir, page, max_events=24):
    """Read page checks from the v3 per-agent trace layout.

    v3 intentionally persists each agent independently (``tool_log.json`` plus
    ``images/view_01.png``) and does not create the older aggregate
    ``_trace/events.jsonl``.  Reconstruct the same UI contract from those
    immutable records without rewriting a completed workspace.
    """
    agents_root = os.path.join(run_dir, "_trace", "subagents")
    if not os.path.isdir(agents_root):
        return {"page": page, "items": [], "total": 0}

    agent_dirs = [path for path in glob.glob(os.path.join(agents_root, "*")) if os.path.isdir(path)]

    def agent_epoch(path):
        candidates = []
        for name in ("config.json", "tool_log.json", "messages.json", "summary.md"):
            try:
                candidates.append(os.path.getmtime(os.path.join(path, name)))
            except OSError:
                pass
        if candidates:
            return min(candidates)
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0

    revision_boundaries = []
    for request_path in glob.glob(os.path.join(
            run_dir, "_trace", "revisions", "revision_*", "request.json")):
        request = _read_trace_json(request_path, {})
        try:
            revision_no = int(request.get("revision_no") or 0)
            created_at = float(request.get("created_at") or 0)
        except (TypeError, ValueError):
            continue
        if revision_no > 0 and created_at > 0:
            revision_boundaries.append((created_at, revision_no))
    revision_boundaries.sort()

    agent_dirs.sort(key=lambda path: (agent_epoch(path), os.path.basename(path)))
    items = []
    item_order = 0
    for agent_dir in agent_dirs:
        label = os.path.basename(agent_dir)
        base_epoch = agent_epoch(agent_dir)
        revision_no = 0
        for created_at, candidate in revision_boundaries:
            if base_epoch >= created_at:
                revision_no = candidate
            else:
                break
        calls = _read_trace_json(os.path.join(agent_dir, "tool_log.json"), [])
        if not isinstance(calls, list):
            continue
        try:
            with open(os.path.join(agent_dir, "summary.md"), encoding="utf-8", errors="replace") as summary_file:
                summary = summary_file.read()
        except OSError:
            summary = ""
        is_review = bool(re.search(r"(?:^|[-_])review(?:[-_]|$)", label))
        message_judgments = _v3_vision_judgments(agent_dir)
        agent_items = []
        vision_no = 0
        render_no = 0
        active_item = None
        for call in calls:
            if not isinstance(call, dict):
                continue
            name = str(call.get("name") or "")
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            command = str(args.get("command") or "")
            if name in {"terminal", "run_command", "exec_command"} and re.search(
                    rf"(?:slides|renders)/slide_0?{page}\.(?:html|png)\b", command, re.I):
                render_no += 1
            if name == "vision_analyze":
                vision_no += 1
                image_path = str(args.get("image_url") or args.get("path") or "")
                page_match = _TRACE_PAGE_IMAGE_RE.search(image_path)
                target_page = int(page_match.group(1)) if page_match else 0
                active_item = None
                if target_page != page:
                    continue
                message_judgment = ""
                message_image_rel = ""
                has_message_record = vision_no <= len(message_judgments)
                if has_message_record:
                    message_record = message_judgments[vision_no - 1]
                    message_judgment = str(message_record.get("judgment") or "")
                    message_image_rel = _v3_message_image_rel(
                        run_dir, agent_dir, message_record.get("shot"))
                judgment = message_judgment or (
                    _review_page_excerpt(summary, page) if is_review else _clean_history_text(summary))
                item = {
                    "stage": "review" if is_review else "build",
                    "time": "",
                    "epoch": base_epoch + vision_no / 1000,
                    "revision_no": revision_no,
                    "agent": "整册 Review Agent" if is_review else f"页面 {page:02d} 子 Agent",
                    "render_no": render_no,
                    # Some failed Vision calls do not persist a screenshot, so
                    # the image ordinal can diverge from the tool-call ordinal.
                    # messages.json retains the exact call-id -> shot mapping.
                    "image_rel": (message_image_rel if has_message_record else
                                  _history_image_rel(run_dir, label, vision_no)),
                    "prompt": _clean_history_text(args.get("question") or args.get("prompt"), 500),
                    "judgment": judgment,
                    "changes": 0,
                    "change_notes": [],
                }
                item_order += 1
                item["_order"] = item_order
                items.append(item)
                agent_items.append(item)
                active_item = item
                continue
            if active_item is None or name not in {"patch", "edit", "write_file"}:
                continue
            path = str(args.get("path") or "")
            if not re.search(rf"(?:^|/)slide_0?{page}\.html\b", path, re.I):
                continue
            active_item["changes"] += 1
            payload = json.dumps(args, ensure_ascii=False)
            note = _change_note(name, payload)
            if note and note not in active_item["change_notes"] and len(active_item["change_notes"]) < 4:
                active_item["change_notes"].append(note)

        for item in agent_items:
            if item.get("judgment") or not item.get("changes"):
                continue
            notes = "；".join(item.get("change_notes") or [])
            item["judgment"] = f"检查发现页面仍需调整，已完成 {notes or '视觉细节修正'}。"

    items.sort(key=lambda item: (item.get("epoch") or 0, item.get("_order") or 0))
    for index, item in enumerate(items, 1):
        item["version"] = index
        item["image_url"] = item.pop("image_rel", "")
        item.pop("_order", None)
    return {"page": page, "items": items[-max_events:], "total": len(items)}


def page_history(run_dir, page, max_events=24):
    """Rebuild one slide's real visual iteration timeline from _trace/events.jsonl.

    Every vision_analyze input is already snapshotted by the harness as
    _trace/<agent>/images/view_NNN.png. Those immutable images are the exact
    render versions the agent judged, unlike renders/slide_N.png which is
    overwritten on every pass.
    """
    try:
        page = int(page)
    except (TypeError, ValueError):
        return {"page": 0, "items": []}
    if page <= 0:
        return {"page": page, "items": []}
    event_path = os.path.join(run_dir, "_trace", "events.jsonl")
    if not os.path.isfile(event_path):
        return _v3_page_history(run_dir, page, max_events=max_events)

    items = []
    view_counts = {}
    render_counts = {}
    active = {}
    try:
        lines = open(event_path, encoding="utf-8", errors="replace")
    except OSError:
        return {"page": page, "items": []}
    with lines:
        for line in lines:
            try:
                event = json.loads(line)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            label = str(event.get("label") or "")
            message = str(event.get("message") or "")
            if not label or not message:
                continue

            render_match = _TRACE_RENDER_RE.search(message)
            if render_match:
                rendered_page = int(render_match.group(1))
                key = (label, rendered_page)
                render_counts[key] = render_counts.get(key, 0) + 1

            vision = _TRACE_VISION_RE.search(message)
            if vision:
                view_counts[label] = view_counts.get(label, 0) + 1
                payload = vision.group(1)
                image_path = _trace_arg(payload, "image_url") or _trace_arg(payload, "path")
                page_match = _TRACE_PAGE_IMAGE_RE.search(image_path)
                target_page = int(page_match.group(1)) if page_match else 0
                # Contact sheets are intentionally not duplicated into every page.
                if target_page != page:
                    continue
                stage = "review" if re.search(r"(?:^|_)review(?:_|$)", label) else "build"
                item = {
                    "stage": stage,
                    "time": event.get("clock") or "",
                    "epoch": event.get("epoch"),
                    "agent": "整册 Review Agent" if stage == "review" else f"页面 {page:02d} 子 Agent",
                    "render_no": render_counts.get((label, page), 0),
                    "image_rel": _history_image_rel(run_dir, label, view_counts[label]),
                    "prompt": _clean_history_text(
                        _trace_arg(payload, "question") or _trace_arg(payload, "prompt"), 500),
                    "judgment": "",
                    "changes": 0,
                    "change_notes": [],
                }
                items.append(item)
                active[label] = item
                continue

            tool = _TRACE_TOOL_RE.search(message)
            if tool and label in active:
                payload = tool.group(2)
                path = _trace_arg(payload, "path")
                label_page = re.search(r"slide_0?(\d+)", label)
                owns_page = bool(label_page and int(label_page.group(1)) == page)
                if owns_page or re.search(rf"slides/slide_0?{page}\.html\b", path):
                    active[label]["changes"] += 1
                    note = _change_note(tool.group(1), payload)
                    if note and note not in active[label]["change_notes"] \
                            and len(active[label]["change_notes"]) < 4:
                        active[label]["change_notes"].append(note)
                continue

            chat = _TRACE_CHAT_RE.search(message)
            if not chat:
                continue
            text = chat.group(1).strip()
            if re.search(r"(?:^|[-_])review(?:[-_]|$)", label):
                excerpt = _review_page_excerpt(text, page)
                if excerpt:
                    for item in reversed(items):
                        if item["stage"] == "review" and not item["judgment"]:
                            item["judgment"] = excerpt
            elif label in active and not active[label]["judgment"]:
                clean = _clean_history_text(text)
                if len(clean) >= 12:
                    active[label]["judgment"] = clean

    # Stable page-local version number across build and review passes.
    for index, item in enumerate(items, 1):
        item["version"] = index
        item["image_url"] = item.pop("image_rel", "")
    return {"page": page, "items": items[-max_events:], "total": len(items)}


def provider_trace_payload(run_dir, attempt_id, source, *, cursor=0, limit=65536):
    """展示受限 Provider 原件，仅排除连接认证和原生签名；游标为字符偏移。"""
    WorkspaceLayout.load(run_dir)
    if not re.fullmatch(r"attempt-[A-Za-z0-9_-]{1,160}", attempt_id):
        raise ValueError("invalid Provider attempt")
    if source not in {"request", "response", "stream"}:
        raise ValueError("unsupported Provider trace source")
    root = os.path.realpath(run_dir)
    suffix = "jsonl" if source == "stream" else "json"
    target = os.path.join(root, "_trace", "raw", "provider", f"{attempt_id}.{source}.{suffix}")
    if os.path.realpath(target) != target:
        raise ValueError("Provider trace path escapes workspace")
    if not os.path.isfile(target):
        raise FileNotFoundError(target)
    if os.path.getsize(target) > 64 * 1024 * 1024:
        raise ValueError("Provider trace exceeds preview size limit")
    with open(target, encoding="utf-8") as handle:
        if source == "stream":
            value = [json.loads(line) for line in handle if line.endswith("\n")]
        else:
            value = json.load(handle)
    clean = _provider_trace_view(value)
    content = ("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in clean)
               if source == "stream" else json.dumps(clean, ensure_ascii=False, indent=2))
    position = int(cursor)
    if position < 0 or position > len(content):
        raise ValueError("invalid Provider trace cursor")
    end = min(len(content), position + min(max(int(limit), 1), 65536))
    return {"attempt_id": attempt_id, "source": source, "cursor": position, "next_cursor": end,
            "cursor_unit": "character", "has_more": end < len(content),
            "content": content[position:end], "redacted": clean != value, "signature_policy": "metadata_only"}


def _provider_trace_view(value):
    # 只查看 HTTP 头和原生协议块；tool input、JSON 文本及任意业务 signature 字段不解释。
    def protocol_block(block):
        if isinstance(block, dict) and block.get("type") in {"thinking", "signature_delta"} and "signature" in block:
            data = str(block.pop("signature") or "").encode()
            block["signature_metadata"] = {"present": bool(data), "bytes": len(data),
                                           "sha256": hashlib.sha256(data).hexdigest()}

    copied = copy.deepcopy(value)
    for record in copied if isinstance(copied, list) else [copied]:
        if not isinstance(record, dict):
            continue
        if isinstance(record.get("headers"), dict):
            record["headers"] = {key: item for key, item in record["headers"].items()
                if key.lower() not in {"authorization", "proxy-authorization", "api-key", "api_key", "apikey",
                                      "x-api-key", "cookie", "set-cookie"}}
        body = record.get("body") or record.get("event")
        if not isinstance(body, dict):
            continue
        blocks = body.get("content") if isinstance(body.get("content"), list) else []
        for block in [*blocks, body.get("delta"), body.get("content_block")]:
            protocol_block(block)
        for message in body.get("messages") or []:
            if isinstance(message, dict) and message.get("role") == "assistant" and isinstance(message.get("content"), list):
                for block in message["content"]:
                    protocol_block(block)
    return copied
