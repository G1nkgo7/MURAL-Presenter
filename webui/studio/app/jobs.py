"""Async job dispatcher: queued decks -> engine subprocesses.

The shared Harness runtime configuration bounds total and per-model Decks.
Deck status is queued -> running -> completed | failed, persisted in SQLite.

Restart safety: engine subprocesses survive a studio restart (they are separate
OS processes). On startup, a deck stuck in 'running' is ADOPTED if its engine
process is still alive (a watcher polls until it finishes, then finalizes) —
NEVER re-enqueued while alive, which would spawn a second engine fighting over
the same run_dir. Only decks with no live process and no result get requeued.
"""
import asyncio
from collections import Counter, deque
import errno
import glob
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

from . import custom_models, dynamic, engine, service_config, titles, trace
from .db import connect
from .workspace_layout import WorkspaceLayout

_limits: dict = {}
_queues: dict = {}         # model_key -> 待运行 Deck
_running: dict = {}        # deck_id -> asyncio subprocess
_workers: list = []
_scheduled: set = set()    # 排队和执行共用去重记录
_active: dict = {}         # deck_id -> model_key，包含领养的存活进程
_loop = None               # sync FastAPI endpoint 通过它安全投递到 ASGI loop
_user_canceled: set = set()   # 用户主动取消的 deck:收尾时显示「已取消」而非报错

_BROKEN_STDIO_MARKERS = (
    "bad file descriptor",
    "init_sys_streams",
    "can't initialize sys standard streams",
)

_RECOVERABLE_EXIT_REASONS = {
    "api_failed",
    "resource_queue_timeout",
    "runtime_timeout",
    "stalled_repetition",
    "process_interrupted",
    "agent_turn_limit",
    "critic_incomplete",
    "release_verification_failed",
}
_NONRECOVERABLE_PROVIDER_MARKERS = (
    "[api auth]",
    "[api 400]",
    "authenticationerror",
    "permissiondeniederror",
    "invalid beta flag",
)
_TRANSIENT_PROVIDER_AUTH_MARKERS = (
    "request signature we calculated does not match",
    "request signature mismatch",
)
_RECOVERABLE_PROVIDER_CONTEXT_MARKERS = (
    "invalid `signature` in `thinking` block",
    "invalid signature in thinking block",
)


def _latest_provider_failure(log_tail: str) -> str:
    """Return only the latest provider failure episode from a job log tail.

    A long Deck may contain an earlier child-side 400 followed by a later root
    503. Recovery must classify the terminal episode selected by ``resume_info``
    rather than letting an unrelated historical error poison the checkpoint.
    """
    markers = (
        "[api auth]",
        "[api 400]",
        "[api failed]",
    )
    starts = [log_tail.rfind(marker) for marker in markers]
    latest = max(starts, default=-1)
    return log_tail[latest:] if latest >= 0 else log_tail


def _run_noninteractive(command, **kwargs):
    """Run a non-interactive child with isolated stdin and one startup retry."""
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    for attempt in range(2):
        try:
            result = subprocess.run(command, **kwargs)
        except OSError as exc:
            if attempt == 0 and exc.errno == errno.EBADF:
                continue
            raise
        output = f"{getattr(result, 'stdout', '') or ''}\n{getattr(result, 'stderr', '') or ''}".lower()
        if attempt == 0 and result.returncode and any(marker in output for marker in _BROKEN_STDIO_MARKERS):
            continue
        return result
    raise RuntimeError("non-interactive subprocess retry exhausted")


DELIVERY_MEDIA_TYPES = {
    "html": "text/html", "pdf": "application/pdf", "zip": "application/zip",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "pptx-editable": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


class DeliveryExportError(RuntimeError):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def _offline_runtime(root: Path) -> tuple[str, dict]:
    python = engine._engine_python()
    if not python or not (Path(python).parent.parent / "pyvenv.cfg").is_file():
        raise DeliveryExportError("正式 inference 虚拟环境不可用，未使用其他 Python 回退", 503)
    environment = engine.render_env({
        **{key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "SYSTEMROOT", "MURAL_RUNTIME_CONFIG") if key in os.environ},
        "PPTAGENT_ENGINE_PYTHON": python, "TMPDIR": str(root / "_runtime/tmp"),
        "PYTHONDONTWRITEBYTECODE": "1", "MURAL_STUDIO_DATA_DIR": str(engine.DATA_DIR),
        "SKILL_COMMAND_TIMEOUT_S": "600",
    })
    return python, environment


def resolve_shared_font(workspace: Path, reference: str) -> tuple[Path, str]:
    """正式环境只读校验固定字体引用，不运行 doctor、模型或字体生成。"""
    python, environment = _offline_runtime(workspace)
    bootstrap = (
        "import json,sys;from pathlib import Path;"
        "from runtime.shared_fonts import resolve_font_reference;"
        "p,h=resolve_font_reference(Path(sys.argv[1]),sys.argv[2]);"
        "print(json.dumps({'path':str(p),'sha256':h}))"
    )
    try:
        result = _run_noninteractive([python, "-c", bootstrap, str(workspace), reference],
            cwd=engine.MURAL_V1_HARNESS_ROOT, env=environment,
            capture_output=True, text=True, timeout=30, check=False)
        if result.returncode:
            raise ValueError("font verification failed")
        value = json.loads(result.stdout)
        return Path(value["path"]).resolve(strict=True), str(value["sha256"])
    except subprocess.TimeoutExpired as exc:
        raise DeliveryExportError("固定字体验证超时", 504) from exc
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise DeliveryExportError("固定字体不可用或未通过 workspace 授权校验") from exc


def export_delivery(row, output_format: str) -> dict:
    """按需调用唯一离线导出入口；格式缓存、generation 与 pin 验收属于 Harness。"""
    if output_format not in DELIVERY_MEDIA_TYPES:
        raise DeliveryExportError("导出格式仅支持 html、pdf、pptx、pptx-editable、zip", 422)
    if row["skill_version"] != "mural-presenter-v1" or row["status"] != "completed":
        raise DeliveryExportError("仅支持已完成的 mural-presenter-v1 演示")
    try:
        if not row["run_dir"]:
            raise ValueError("missing workspace")
        layout = WorkspaceLayout.load(row["run_dir"])
        root = layout.root
        if not root.is_dir():
            raise ValueError("unsupported workspace")
        delivery = layout.path("delivery")
        delivery.relative_to(root)
        scratch = layout.path("runtime_tmp")
        scratch.relative_to(root)
    except (OSError, TypeError, ValueError) as exc:
        raise DeliveryExportError("导出工作区不存在或路径映射无效") from exc
    python, environment = _offline_runtime(root)
    scratch.mkdir(parents=True, exist_ok=True)
    environment["TMPDIR"] = str(scratch)
    environment["MURAL_USER_ID"] = str(row["user_id"])
    try:
        result = _run_noninteractive(
            [python, "-m", "export.delivery", "--workspace", str(root), "--format", output_format],
            cwd=engine.MURAL_V1_HARNESS_ROOT, env=environment,
            text=True, capture_output=True, timeout=600, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DeliveryExportError("导出超时，请稍后重试", 504) from exc
    except OSError as exc:
        raise DeliveryExportError("离线导出进程无法启动", 503) from exc
    if result.returncode:
        detail = "导出未通过工作区验收"
        try:
            error = json.loads((result.stderr or "").rstrip().rsplit("\n", 1)[-1])
            if error.get("error") == "delivery_export_failed" and isinstance(error.get("message"), str):
                detail += "：" + error["message"][:600]
        except (TypeError, ValueError, AttributeError):
            pass
        raise DeliveryExportError(detail)
    try:
        record = json.loads(result.stdout)
        extension = "pptx" if output_format == "pptx-editable" else output_format
        if record["format"] != output_format:
            raise ValueError("invalid export receipt")

        def verified_file(receipt, suffix):
            relative = PurePosixPath(receipt["path"])
            if (relative.is_absolute() or ".." in relative.parts
                    or relative.parts[0] != "delivery" or relative.suffix != suffix):
                raise ValueError("invalid export receipt")
            target = (root / relative).resolve(strict=True)
            target.relative_to(delivery)
            target.relative_to(root)
            with target.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            size = target.stat().st_size
            if not size or receipt["bytes"] != size or receipt["sha256"] != digest:
                raise ValueError("export changed after verification")
            return target

        target = verified_file(record, "." + extension)
        if output_format == "pptx-editable":
            verified_file(record["editability_report"], ".json")
            for field in ("native_text", "native_images", "native_shapes", "native_tables",
                          "local_raster_fallbacks", "pages_with_warnings"):
                count = record["summary"][field]
                if type(count) is not int or count < 0:
                    raise ValueError("invalid editability summary")
    except (OSError, TypeError, ValueError, KeyError, IndexError) as exc:
        raise DeliveryExportError("导出回执或产物校验失败，未提供下载") from exc
    return {**record, "path": target, "media_type": DELIVERY_MEDIA_TYPES[output_format],
            "filename": f"presentation_{int(row['id'])}{'_editable' if output_format == 'pptx-editable' else ''}.{extension}"}


def _set_status(deck_id, status, **fields):
    con = connect()
    try:
        cols = ["status = ?"]
        vals = [status]
        for k, v in fields.items():
            cols.append(f"{k} = ?")
            vals.append(v)
        vals.append(deck_id)
        con.execute(f"UPDATE decks SET {', '.join(cols)} WHERE id = ?", vals)
        con.commit()
    finally:
        con.close()


def _load_deck(deck_id):
    con = connect()
    try:
        return con.execute("SELECT * FROM decks WHERE id = ?", (deck_id,)).fetchone()
    finally:
        con.close()


def _store_plan_title(deck_id: int, title: str, *, expected=None) -> bool:
    """只更新本次请求所见的自动标题，保护人工改名及较新的运行结果。"""
    con = connect()
    try:
        row = con.execute(
            "SELECT conversation_id,title,title_source,title_locked,title_updated_at,"
            "started_at,seed_json,status FROM decks WHERE id = ?",
            (deck_id,),
        ).fetchone()
        if (
            not row
            or not title
            or row["title_locked"]
            or row["status"] not in {"running", "completed"}
            or row["title_source"] not in {"fallback", "task_model", "plan"}
        ):
            return False
        compared = ("title", "title_source", "title_updated_at", "started_at", "seed_json")
        if expected is not None and any(row[key] != expected[key] for key in compared):
            return False
        if row["title_source"] == "plan" and row["title"] == title:
            return True
        previous = row["title"]
        previous_source = row["title_source"]
        now = int(time.time())
        changed = con.execute(
            "UPDATE decks SET title = ?, title_source = 'plan', title_updated_at = ? "
            "WHERE id = ? AND title_locked = 0 AND title_source = ? AND title = ? "
            "AND title_updated_at IS ? AND started_at IS ? AND seed_json IS ? "
            "AND status IN ('running', 'completed')",
            (title, now, deck_id, previous_source, previous,
             row["title_updated_at"], row["started_at"], row["seed_json"]),
        ).rowcount
        if changed and row["conversation_id"]:
            con.execute(
                "UPDATE conversations SET title = ?, updated_at = ? "
                "WHERE id = ? AND title = ?",
                (title, now, row["conversation_id"], previous),
            )
        con.commit()
        return bool(changed)
    finally:
        con.close()


def _title_plan(run_dir: Path) -> str:
    try:
        layout = WorkspaceLayout.load(run_dir)
        config = layout.creation_config()
        pages = layout.plan_pages()
        if config["target_page_count"] is not None and len(pages) != config["target_page_count"]:
            return ""
        return titles.plan_title_input(layout.plan_text(), pages) if pages else ""
    except (OSError, ValueError):
        # 计划尚未写齐时不猜标题；沿用既有解析器，不影响制作继续。
        return ""


async def _refresh_title_after_plan(
    deck_id: int,
    row,
    run_dir: Path,
    stopped: asyncio.Event,
    *,
    initial_plan: str | None = None,
) -> None:
    """首份完整计划更新一次；结束时仅复核变化，不随每次写文件调用模型。"""
    row = dict(row)
    try:
        seed = json.loads(row["seed_json"] or "{}")
    except (TypeError, ValueError):
        seed = {}
    if row["title_locked"] or row["title_source"] == "manual":
        return
    query = str(seed.get("user_query") or seed.get("query") or "").strip()
    if not query:
        return

    async def refine(plan: str) -> bool:
        try:
            current = _load_deck(deck_id)
            if (not current or current["title_locked"] or current["title_source"] == "manual"
                    or current["seed_json"] != row["seed_json"]
                    or current["status"] not in {"running", "completed"}):
                return False
            registry = dynamic._model_registry({"id": row["user_id"]})
            model = titles.select_task_model(registry, service_config.title_task_model_key(row["user_id"]))
            title = await asyncio.to_thread(
                titles.summarize_after_plan, query, plan, model,
                previous_title=current["title"] if current["title_source"] == "plan" else "",
            )
            if _title_plan(run_dir) != plan:
                return False
            return _store_plan_title(deck_id, title, expected=current)
        except Exception as exc:
            # 标题模型、数据库等旁路故障不能导致 Deck 失败。
            print(f"[jobs] deck {deck_id}: Plan 标题更新跳过：{type(exc).__name__}: {exc}", flush=True)
            return False

    last_plan = initial_plan
    pending = ""
    initial_checked = row["title_source"] == "plan"
    while not stopped.is_set():
        if not initial_checked:
            plan = _title_plan(run_dir)
            if plan and plan == pending:
                if await refine(plan):
                    last_plan = plan
                initial_checked = True
            pending = plan
        try:
            await asyncio.wait_for(stopped.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass
    current = _load_deck(deck_id)
    if current and current["status"] == "completed":
        plan = _title_plan(run_dir)
        if plan and plan != last_plan:
            await refine(plan)


def _schedule_title_refresh(deck_id, row, run_dir, stopped, *, initial_plan=None):
    task = asyncio.create_task(_refresh_title_after_plan(
        deck_id, row, run_dir, stopped, initial_plan=initial_plan,
    ), name=f"deck-title-{deck_id}")
    _workers.append(task)
    task.add_done_callback(lambda done: _workers.remove(done) if done in _workers else None)


def title_refresh_pending(deck_id: int) -> bool:
    return any(task.get_name() == f"deck-title-{deck_id}" and not task.done()
               for task in tuple(_workers))


def _matching_engine_pids(deck_id) -> set[int]:
    """Return matching inference-worker PIDs without assuming Linux ``/proc``."""
    needle = str((engine.JOBS_DIR / f"{deck_id}.json").resolve())
    matches: set[int] = set()
    proc_root = Path("/proc")
    if proc_root.is_dir():
        try:
            process_entries = tuple(proc_root.iterdir())
        except OSError:
            process_entries = ()
        for entry in process_entries:
            if not entry.name.isdigit():
                continue
            try:
                args = (entry / "cmdline").read_bytes().decode("utf-8", "ignore").split("\0")
            except OSError:
                continue
            if (any(Path(arg).name == "serve_one.py" for arg in args)
                    and any(args[index + 1] == needle for index, arg in enumerate(args[:-1])
                            if arg == "--job")):
                matches.add(int(entry.name))
        return matches

    command: list[str] | None = None
    if os.name == "nt":
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if powershell:
            command = [
                powershell, "-NoProfile", "-NonInteractive", "-Command",
                "Get-CimInstance Win32_Process | ForEach-Object { "
                "\"$($_.ProcessId)`t$($_.CommandLine)\" }",
            ]
    else:
        ps = shutil.which("ps")
        if ps:
            command = [ps, "-axo", "pid=,command="]

    if command:
        try:
            result = _run_noninteractive(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return matches
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "serve_one.py" not in line or needle not in line:
                    continue
                found = re.match(r"\s*(\d+)(?:\s+|\t)", line)
                if found:
                    matches.add(int(found.group(1)))
    return matches


def _engine_alive(deck_id) -> bool:
    """Return whether this deck's inference worker is alive."""
    return bool(_matching_engine_pids(deck_id))


def _revision_seed(row) -> dict | None:
    try:
        seed = json.loads(row["seed_json"] or "{}")
    except (TypeError, ValueError):
        return None
    revision = seed.get("_revision")
    return revision if isinstance(revision, dict) else None


def _prepare_revision_workspace(row, revision: dict) -> None:
    """Prepare either an in-place revision or a legacy immutable workspace."""
    parent_id = int(revision.get("parent_deck_id") or 0)
    target = Path(row["run_dir"] or "")
    if revision.get("in_place"):
        if parent_id != int(row["id"]):
            raise RuntimeError("原地修订的 deck 来源与目标不一致")
        try:
            seed = json.loads(row["seed_json"] or "{}")
        except (TypeError, ValueError):
            seed = {}
        entry = "deck.html" if seed.get("ppt_output") == "dynamic_html" else "present.html"
        if not (target / entry).is_file():
            raise RuntimeError(f"当前成稿缺少 {entry}，不能原地续编")

        # Keep only lightweight run metadata for diagnosis.  The presentation,
        # renders and output path stay exactly where they are and are edited by
        # revision_worker directly; no second Deck/workspace is created.
        revision_no = int(revision.get("revision_no") or 1)
        archive = target / "_trace" / "revisions" / f"revision_{revision_no:03d}"
        archive.mkdir(parents=True, exist_ok=True)
        result = target / "result.json"
        if result.is_file():
            candidates = [archive / "result-before.json"]
            candidates.extend(archive / f"result-attempt-{n:02d}.json" for n in range(1, 100))
            destination = next(path for path in candidates if not path.exists())
            result.replace(destination)
        previous_log = engine.log_path(int(row["id"]))
        if previous_log.is_file():
            artifact_snapshot = archive / "specialist-artifacts.json"
            if not artifact_snapshot.exists():
                try:
                    previous_feed = trace.livefeed(str(previous_log), run_dir=str(target))
                    artifact_payload = trace.scoped_specialist_artifacts(
                        str(target), previous_feed.get("agents", {}).keys()
                    )
                    temporary = artifact_snapshot.with_suffix(".json.tmp")
                    temporary.write_text(
                        json.dumps(artifact_payload, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    temporary.replace(artifact_snapshot)
                except (OSError, TypeError, ValueError):
                    # Archiving diagnostics must never prevent an otherwise
                    # valid in-place revision from starting.
                    pass
            log_candidates = [archive / "job-before.log"]
            log_candidates.extend(archive / f"job-attempt-{n:02d}.log" for n in range(1, 100))
            destination = next(path for path in log_candidates if not path.exists())
            shutil.copy2(previous_log, destination)
        (archive / "request.json").write_text(json.dumps({
            "deck_id": int(row["id"]),
            "revision_no": revision_no,
            "instruction": str(revision.get("instruction") or ""),
            "in_place": True,
            "created_at": int(time.time()),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return

    parent = _load_deck(parent_id)
    if not parent or parent["status"] != "completed":
        raise RuntimeError("父版本尚未完成，不能创建修订工作区")
    source = Path(parent["run_dir"] or "")
    if not (source / "present.html").is_file():
        raise RuntimeError("父版本缺少 present.html，不能继续修改")

    marker = target / "_trace" / "revision-parent.json"
    if target.exists():
        try:
            previous = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            raise RuntimeError("修订工作区已存在但来源标记无效")
        if int(previous.get("parent_deck_id") or 0) != parent_id:
            raise RuntimeError("修订工作区与父版本不匹配")
        return

    def ignore(_directory, names):
        skipped = {"_trace", "result.json"}
        skipped.update(name for name in names if name.startswith("deck_") and name.endswith(".pptx"))
        return skipped.intersection(names)

    target.parent.mkdir(parents=True, exist_ok=True)
    # Revisions are immutable snapshots: materialize any parent symlink instead
    # of coupling the child to a path that may later change or disappear.
    shutil.copytree(source, target, symlinks=False, ignore=ignore)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({
        "parent_deck_id": parent_id,
        "revision_no": int(revision.get("revision_no") or 1),
        "instruction": str(revision.get("instruction") or ""),
        "source_run_dir": str(source),
        "created_at": int(time.time()),
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def _settle_waiting_revisions(parent_deck_id: int, succeeded: bool) -> None:
    """Release waiting children after a parent completes, or fail them with it."""
    con = connect()
    try:
        rows = con.execute(
            "SELECT id FROM decks WHERE parent_deck_id = ? AND status = 'waiting' "
            "ORDER BY id",
            (parent_deck_id,),
        ).fetchall()
        if not rows:
            return
        if succeeded:
            con.executemany(
                "UPDATE decks SET status='queued', error=NULL WHERE id = ?",
                [(row["id"],) for row in rows],
            )
        else:
            con.executemany(
                "UPDATE decks SET status='failed', error=?, finished_at=? WHERE id = ?",
                [("父版本生成失败，修订未执行", int(time.time()), row["id"]) for row in rows],
            )
        con.commit()
    finally:
        con.close()
    if succeeded:
        for row in rows:
            enqueue(row["id"])
    else:
        for row in rows:
            _settle_waiting_revisions(row["id"], False)


def _fail_deck(deck_id: int, error: str) -> None:
    """Fail one Deck and deterministically settle any dependent revision."""
    _user_canceled.discard(deck_id)
    _set_status(
        deck_id,
        "failed",
        error=error,
        finished_at=int(time.time()),
    )
    _settle_waiting_revisions(deck_id, False)


def _runtime_events(root):
    """恢复只读取已提交的 live journal；离线投影缺失不影响进程中断判定。"""
    path = root / "_trace/live-events.jsonl"
    if path.is_file():
        with path.open("rb") as handle:
            for line in handle:
                if not line.endswith(b"\n"):
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("schema") == "mural.live-event.v1":
                    yield {**event.get("payload", {}), **{key: value for key, value in event.items() if key != "payload"}}


def _unresolved_child_exit(root, tasks):
    failed = {str(task.get("owner_id")) for task in (tasks or {}).values()
              if isinstance(task, dict) and task.get("status") in {"failed", "interrupted"}}
    reasons = {"AgentProviderError": "api_failed", "ResourceSlotTimeout": "resource_queue_timeout",
               "AgentTurnLimitError": "agent_turn_limit", "ProcessInterrupted": "process_interrupted"}
    latest = {}
    for event in _runtime_events(root):
        owner = event.get("agent_id")
        if owner not in failed:
            continue
        if event.get("event_type") == "agent_failed":
            latest[owner] = (event["sequence"], reasons.get(event.get("error_type"), ""))
        elif event.get("event_type") == "agent_completed":
            latest.pop(owner, None)
    return max((item for item in latest.values() if item[1]), default=(0, ""))[1]


def _orchestrator_checkpoint_path(root: Path) -> Path:
    return root / "_state/owner-context" / (hashlib.sha256(b"orchestrator").hexdigest()[:24] + ".json")


def _orchestrator_checkpoint_is_durable(root: Path) -> bool:
    """只认可 Harness 的私有闭合上下文；已有规划也不能退回原 query 冷启动。"""
    try:
        run = json.loads((root / "_state/run.json").read_text(encoding="utf-8"))
        workspace = json.loads((root / "_state/workspace.json").read_text(encoding="utf-8"))
        host = json.loads((root / "_state/skill-runtime.json").read_text(encoding="utf-8"))
        checkpoint = json.loads(_orchestrator_checkpoint_path(root).read_text(encoding="utf-8"))
        attachment_manifest = json.loads((root / "_trace/attachment-manifest.json").read_text(encoding="utf-8"))
        attachments = [{"sha256": item["sha256"], "original_name": item["original_name"],
                        "intent": run["attachment_intents"][index] if run.get("attachment_intents") else ""}
                       for index, item in enumerate(attachment_manifest.get("attachments", []))]
        if not all(isinstance(value, dict) for value in (run, workspace, host, checkpoint)):
            return False
        failure = host.get("agent_failed")
        if not isinstance(failure, dict):
            return False
        if (run.get("schema") != "mural.run.v1" or not str(run.get("raw_user_query") or "").strip()
            or Path(str(run.get("output_workspace") or "")).resolve() != root.resolve()
            or workspace.get("schema") != "mural.workspace-state.v1" or workspace.get("layout_version") != 2
            or not workspace.get("deck_id")
            or host.get("schema") != "mural.skill-runtime.v1" or host.get("status") != "failed"
            or failure.get("error_type") not in {"ResourceSlotTimeout", "AgentInterruptedError"}
            or checkpoint.get("schema") != "mural.orchestrator-context.v1"
            or checkpoint.get("identity") != {"deck_id": workspace["deck_id"], "workspace": str(root.resolve()),
                "raw_user_query": run["raw_user_query"], "attachments": attachments,
                "skill_tree_sha256": host.get("skill_tree_sha256")}):
            return False
        if (root / "_state/owners.json").exists():
            return False
        orchestration_path = root / "_state/orchestration.json"
        if orchestration_path.exists():
            orchestration = json.loads(orchestration_path.read_text(encoding="utf-8"))
            if (not isinstance(orchestration, dict) or orchestration.get("schema") != "mural.orchestration.v2"
                    or orchestration.get("tasks") != {}):
                return False
        segment = checkpoint.get("segment")
        if (not isinstance(segment, dict) or segment.get("schema") != "mural.runtime-segment.v1"
            or not segment.get("messages") or hashlib.sha256(json.dumps(
                segment, sort_keys=True, ensure_ascii=False).encode()).hexdigest() != checkpoint.get("segment_sha256")):
            return False
        hashes = checkpoint.get("artifact_hashes")
        if not isinstance(hashes, dict) or "_state/workspace.json" not in hashes:
            return False
        for relative, digest in hashes.items():
            path = (root / relative).resolve()
            if not path.is_relative_to(root.resolve()) or not path.is_file():
                return False
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                return False
        cursor = int(checkpoint["live_cursor"])
        live = list(_runtime_events(root))
        if not live or any(int(event.get("sequence") or 0) > cursor and event.get("event_type") in {"tool_use", "tool_result"}
                           for event in live):
            return False
        events = [event for event in live
                  if event.get("event_type") in {"agent_started", "agent_failed", "agent_completed"}]
        if not events or not all(isinstance(event, dict) and event.get("agent_id") == "orchestrator"
                                 and event.get("role") == "orchestrator"
                                 for event in events):
            return False
        terminal = events[-1]
        return bool(terminal.get("event_type") == "agent_failed"
                    and terminal.get("error_type") == failure.get("error_type")
                    and terminal.get("segment_id") == segment.get("segment_id")
                    and any(event.get("event_type") == "agent_started" for event in events))
    except (OSError, IndexError, KeyError, TypeError, ValueError):
        return False


def resume_info(deck_id: int, run_dir: str | os.PathLike[str]) -> dict:
    """Return the thin, read-only task-level recovery decision for one Deck."""
    root = Path(run_dir)
    shared_orchestration, shared_owners, shared_runtime = {}, {}, {}
    try:
        WorkspaceLayout.load(root)
    except (ValueError, OSError):
        return {"eligible": False, "durable": False, "exit_reason": "unsupported_workspace_layout",
                "root_exit_reason": "unsupported_workspace_layout", "child_exit_reason": None, "trace_source": None}
    try:
        shared_orchestration = json.loads(
            (root / "_state" / "orchestration.json").read_text(encoding="utf-8")
        )
        shared_owners = json.loads(
            (root / "_state" / "owners.json").read_text(encoding="utf-8")
        )
        shared_runtime = json.loads(
            (root / "_state" / "skill-runtime.json").read_text(encoding="utf-8")
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    shared_tasks = (
        shared_orchestration.get("tasks")
        if isinstance(shared_orchestration, dict)
        else None
    )
    shared_durable = bool(
        root.is_dir()
        and isinstance(shared_orchestration, dict)
        and shared_orchestration.get("schema") == "mural.orchestration.v2"
        and isinstance(shared_tasks, dict)
        and shared_tasks
        and isinstance(shared_owners, dict)
        and shared_owners.get("schema") == "mural.owner-registry.v2"
        and isinstance(shared_runtime, dict)
        and shared_runtime.get("schema") == "mural.skill-runtime.v1"
        and (root / "_trace" / "live-events.jsonl").is_file()
    )
    orchestrator_durable = not shared_durable and _orchestrator_checkpoint_is_durable(root)
    durable = shared_durable or orchestrator_durable
    result = {}
    try:
        result = json.loads((root / "result.json").read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    root_exit_reason = str(
        (result.get("exit_reason") if isinstance(result, dict) else "")
        or ""
    )
    child_exit_reason = _unresolved_child_exit(root, shared_tasks) if shared_durable else ""
    if shared_durable and not root_exit_reason:
        root_exit_reason = "process_interrupted"
    exit_reason = (
        root_exit_reason
        if root_exit_reason in _RECOVERABLE_EXIT_REASONS
        else child_exit_reason or root_exit_reason
    )
    log_tail = ""
    try:
        data = engine.log_path(deck_id).read_bytes()
        log_tail = data[-131072:].decode("utf-8", "replace").lower()
    except OSError:
        pass
    # 发布前置检查未改动既有检查点；只识别本次恢复的最后一个异常，不把旧故障套到新失败上。
    latest_traceback = log_tail.rsplit("traceback (most recent call last):", 1)[-1]
    release_resume_blocked = bool(
        durable and root_exit_reason == "runtime_error" and result.get("resume_workspace")
        and any(line.startswith("runtimeerror: frozen mural release hash mismatch:")
                for line in latest_traceback.splitlines())
    )
    if release_resume_blocked:
        exit_reason = "release_verification_failed"
    terminal_provider_failure = _latest_provider_failure(log_tail)
    transient_provider_auth_error = any(
        marker in terminal_provider_failure
        for marker in _TRANSIENT_PROVIDER_AUTH_MARKERS
    )
    recoverable_provider_context_error = any(
        marker in terminal_provider_failure
        for marker in _RECOVERABLE_PROVIDER_CONTEXT_MARKERS
    )
    provider_configuration_error = (
        any(
            marker in terminal_provider_failure
            for marker in _NONRECOVERABLE_PROVIDER_MARKERS
        )
        and not transient_provider_auth_error
        and not recoverable_provider_context_error
    )
    terminal_error = str(result.get("error") or "").lower() if isinstance(result, dict) else ""
    provider_access_disabled = (
        root_exit_reason == "api_failed"
        and "access has been disabled" in terminal_error
        and "usage policy" in terminal_error
    )
    failed_channel = ""
    if shared_durable:
        last_provider_terminal = ""
        try:
            for event in _runtime_events(root):
                event_type = str(event.get("event_type") or "")
                if event_type == "provider_failed":
                    last_provider_terminal = str(event.get("error_kind") or "")
                    failed_channel = str(event.get("channel_id") or "")
                elif event_type == "provider_succeeded":
                    last_provider_terminal = ""
                    failed_channel = ""
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
        provider_configuration_error = (
            provider_configuration_error or last_provider_terminal == "authentication"
        )
        provider_access_disabled = provider_access_disabled or (
            root_exit_reason == "api_failed" and last_provider_terminal == "access_disabled"
        )
    provider_channel_retired = False
    if provider_access_disabled and failed_channel:
        row = _load_deck(deck_id)
        provider_channel_retired = bool(row) and engine.shared_provider_channel_retired(
            str(row["model"]), failed_channel
        )
    provider_configuration_error = (
        provider_configuration_error or provider_access_disabled
    ) and not provider_channel_retired
    eligible = bool(
        durable
        and exit_reason in _RECOVERABLE_EXIT_REASONS
        and not provider_configuration_error
    )
    return {
        "eligible": eligible,
        "durable": durable,
        "exit_reason": exit_reason or None,
        "root_exit_reason": root_exit_reason or None,
        "child_exit_reason": child_exit_reason or None,
        "error_summary": str(result.get("error") or "")[:2000] if isinstance(result, dict) else "",
        "trace_source": (
            "_state/orchestration.json"
            if shared_durable
            else str(_orchestrator_checkpoint_path(root).relative_to(root)) if orchestrator_durable
            else None
        ),
        "provider_configuration_error": provider_configuration_error,
        "provider_access_disabled": provider_access_disabled,
        "provider_channel_retired": provider_channel_retired,
        "recoverable_provider_context_error": recoverable_provider_context_error,
    }


def _pause_recoverable_deck(deck_id: int, info: dict) -> None:
    _user_canceled.discard(deck_id)
    exit_reason = str(info.get("exit_reason") or "transient failure")
    if exit_reason == "stalled_repetition":
        explanation = "执行轨迹停滞；已保留最近的安全检查点，可由同一逻辑任务继续生成。"
    elif exit_reason == "process_interrupted":
        explanation = "执行进程已中断；工作区和安全检查点仍在，可在同一任务中继续生成。"
    elif exit_reason == "agent_turn_limit":
        explanation = "已达到本次回合上限；保留当前工作区，手动继续将使用新一轮预算，不会自动无限重试。"
    elif exit_reason == "resource_queue_timeout":
        explanation = "本地模型资源排队已超时，请求尚未发出；保留当前工作区，可手动继续。"
    elif exit_reason == "critic_incomplete":
        explanation = "独立视觉评审未完成，训练尚未准入；当前页面与轨迹已保留，继续将补齐缺失评审。"
        if info.get("error_summary"):
            explanation += " " + str(info["error_summary"])
    elif exit_reason == "release_verification_failed":
        explanation = "恢复启动被发布校验拒绝；已有检查点保留。完成发布冻结后可继续，启动仍需通过严格校验。"
    else:
        explanation = "上游模型通道暂时不可用；已保留最近的安全检查点，可继续生成。"
    _set_status(
        deck_id,
        "paused_recoverable",
        error=f"{explanation}（{exit_reason}）",
        finished_at=int(time.time()),
    )


def archive_for_fresh_retry(deck_id: int, run_dir: str | os.PathLike[str]) -> str:
    """Preserve a failed workspace, then free its canonical path for a clean run."""
    source = Path(run_dir)
    if not source.exists():
        return ""
    archive_root = source.parent / "_archived_attempts" / str(deck_id)
    archive_root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    destination = archive_root / f"fresh-retry-{stamp}-{time.time_ns()}"
    os.replace(source, destination)
    return str(destination)


def _ensure_static_delivery(row, run_dir: Path) -> str | None:
    """只读验收 Harness 的生成回执；无需用户提前导出，也不从 Skill 补建。"""
    try:
        seed = json.loads(row["seed_json"] or "{}") if row else {}
    except (TypeError, ValueError):
        seed = {}
    if seed.get("ppt_output") == "dynamic_html":
        deck = run_dir / "deck.html"
        return None if deck.is_file() and deck.stat().st_size else "最终交付物缺少 deck.html"
    if row and row["skill_version"] == "mural-presenter-v1":
        # v1 的组装与验收只属于 Harness；Studio 不从可变源码补建失败结果。
        try:
            layout = WorkspaceLayout.load(run_dir)
            result = json.loads(layout.path("result").read_text(encoding="utf-8"))
            manifest = result["generation"]
            if (result["status"] not in {"completed", "needs_improvement"}
                    or manifest["schema"] != "mural.generation.v1" or manifest["status"] != "complete"):
                raise ValueError("generation")
            present = layout.path("present")
            record = manifest["outputs"]["present_html"]
            if (record["path"] != layout.relative(present) or not present.stat().st_size
                    or record["bytes"] != present.stat().st_size
                    or record["sha256"] != hashlib.sha256(present.read_bytes()).hexdigest()):
                raise ValueError("present")
            state = json.loads((layout.root / "_state/workspace.json").read_text(encoding="utf-8"))
            current = {int(page): item for page, item in state["pages"].items() if not item.get("retired")}
            if not current or {int(page["page"]) for page in manifest["pages"]} != set(current):
                raise ValueError("pages")
            for page in manifest["pages"]:
                number = int(page["page"])
                now = current[number]
                pair = {key: page[key] for key in ("html_sha256", "png_sha256")}
                if (page["lifecycle"] != "ready" or page["page_uid"] != now["page_uid"]
                        or pair != now["ready_pair"]
                        or page["inspection_id"] != now["last_inspection"]["inspection_id"]
                        or pair["html_sha256"] != hashlib.sha256(layout.path("slide_pattern", page=number).read_bytes()).hexdigest()
                        or pair["png_sha256"] != hashlib.sha256(layout.path("render_pattern", page=number).read_bytes()).hexdigest()):
                    raise ValueError("current page")
            return None
        except (OSError, KeyError, TypeError, ValueError, AttributeError):
            return "生成产物未通过当前版本只读验收；请在 Harness 中完成当前页面检查，WebUI 不会重新构建"
    return "unsupported_workspace_layout: 历史 Harness/Skill 入口已停用"


def _finalize(deck_id, run_dir: Path, rc=None):
    """Decide final status from result.json + what actually landed on disk.

    Some engine rejections only describe trace/SFT quality and a rendered deck
    can still be useful.  Pixel-review and delivery-integrity rejections are
    different: existing PNGs are not a valid delivery when the final Review did
    not finish or inspect fresh pixels.
    """
    res_path = run_dir / "result.json"
    engine_status, reason, err = None, None, None
    res: dict = {}
    if res_path.exists():
        try:
            res = json.loads(res_path.read_text(encoding="utf-8"))
            engine_status = res.get("status")
            reason = res.get("reason")
            err = res.get("error")
        except Exception as e:
            err = f"bad result.json: {e}"
    else:
        err = f"engine produced no result.json (exit={rc}); see {engine.log_path(deck_id)}"

    row = _load_deck(deck_id)
    try:
        seed = json.loads(row["seed_json"] or "{}") if row else {}
    except (TypeError, ValueError):
        seed = {}
    dynamic_v2 = seed.get("ppt_output") == "dynamic_html"
    rendered = len(glob.glob(str(run_dir / "renders" / "slide_*.png")))
    if dynamic_v2:
        rendered = max(
            len(glob.glob(str(run_dir / "shots" / "page_*.png"))),
            len(glob.glob(str(run_dir / "shots" / "slide_*.png"))),
        )
    is_revision = bool(row and _revision_seed(row))
    # A user stop is authoritative even when the engine managed to leave a
    # render/result behind before the termination signal was observed.  The
    # old order treated "rendered > 0" as success first and therefore turned a
    # stopped task back into "completed" during finalization.
    if deck_id in _user_canceled:
        _user_canceled.discard(deck_id)
        _set_status(
            deck_id,
            "interrupted",
            error="用户已停止",
            finished_at=int(time.time()),
        )
        _settle_waiting_revisions(deck_id, False)
        return

    recovery = resume_info(deck_id, run_dir)
    if (
        engine_status not in {"completed", "needs_improvement", "rejected"}
        and recovery["eligible"]
    ):
        _pause_recoverable_deck(deck_id, recovery)
        return

    rejection_text = " ".join(str(value or "") for value in (reason, err)).lower()
    delivery_blocking_markers = (
        "最终 review", "没有执行最终 review", "review", "vision_analyze",
        "final_pixels_inspected", "交付依赖审计", "没有成功渲染",
        "渲染疑似空白", "render freshness",
    )
    delivery_blocked = (
        engine_status == "rejected"
        and any(marker in rejection_text for marker in delivery_blocking_markers)
    )
    # A hard engine failure is not a completed Deck merely because an earlier
    # render exists. Keep the artifact-only fallback solely for non-delivery
    # ``rejected`` results (for example trace/SFT eligibility rejection).
    artifact_fallback = (
        engine_status == "rejected"
        and not is_revision
        and rendered > 0
        and not delivery_blocked
    )
    succeeded = engine_status in {"completed", "needs_improvement"} or artifact_fallback
    if succeeded:
        delivery_error = _ensure_static_delivery(row, run_dir)
        if delivery_error:
            _fail_deck(deck_id, delivery_error)
            return
    if succeeded:
        _user_canceled.discard(deck_id)
        _set_status(deck_id, "completed", slide_count=rendered or None,
                    error=None, finished_at=int(time.time()))
    else:
        _fail_deck(deck_id, err or reason or "生成失败(无成稿)")
        return
    _settle_waiting_revisions(deck_id, succeeded)


async def _process(deck_id):
    row = _load_deck(deck_id)
    # A queued item can still be present in the dispatch queue after the user
    # stops it.  Do not let that stale queue entry revive an interrupted task.
    if not row or row["status"] != "queued":
        return
    run_dir = Path(row["run_dir"])
    seed = json.loads(row["seed_json"])
    resume_workspace = bool(seed.pop("_resume_workspace", False))
    revision = seed.get("_revision") if isinstance(seed.get("_revision"), dict) else None
    if revision:
        _prepare_revision_workspace(row, revision)
    dry = bool(seed.pop("_dry", False))     # _dry is studio-only, not part of the engine seed
    sample_id = f"u{row['user_id']}_d{deck_id}"

    model_key = row["model"] if "model" in row.keys() and row["model"] else None
    pipeline_key = row["pipeline"] if "pipeline" in row.keys() and row["pipeline"] else None
    skill_key = row["skill_version"] if "skill_version" in row.keys() and row["skill_version"] else None
    model_config = None
    if custom_models.id_from_key(model_key) is not None:
        con = connect()
        try:
            custom_row = custom_models.get_owned(
                con, row["user_id"], model_key, active_only=False
            )
        finally:
            con.close()
        if not custom_row:
            raise ValueError("自定义模型不存在或不属于当前用户")
        model_config = custom_models.runtime_config(custom_row)
    selected_model = model_config or engine.MODELS[
        engine.canon(model_key) or engine.DEFAULT_MODEL
    ]
    if (
        resume_workspace
        and selected_model.get("backend") == "anthropic"
        and not selected_model.get("api_key_env")
    ):
        # The last selected shared-pool channel may be the one that just went
        # unavailable.  A deliberate resume is a new channel-selection boundary.
        engine.invalidate_anthropic_key(selected_model["engine_model"])
    job = engine.build_job(sample_id, seed, run_dir, dry=dry, model_key=model_key,
                           pipeline_key=pipeline_key, skill_key=skill_key,
                           model_config=model_config, resume_workspace=resume_workspace)
    job_path = engine.write_job_file(deck_id, job)
    log_path = engine.log_path(deck_id)
    logf = open(log_path, "ab" if resume_workspace else "wb")
    if resume_workspace:
        logf.write(
            (
                "\n\n=== MURAL task resume "
                + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                + " ===\n"
            ).encode("utf-8")
        )
        logf.flush()

    _set_status(
        deck_id,
        "running",
        started_at=int(time.time()),
        finished_at=None,
        error=None,
    )
    rc = None
    # Drop studio's venv vars so `uv run --project inference` uses the engine's
    # own venv cleanly (avoids the harmless VIRTUAL_ENV-mismatch warning).
    # Also drop inherited ANTHROPIC_*/OPENAI_* so the engine's own inference/.env
    # (loaded via setdefault in the runtime loader) is authoritative. Without this,
    # an ambient ANTHROPIC_BASE_URL=https://api.anthropic.com (e.g. from a parent
    # Claude harness) shadows the tokenhub endpoint and the tokenhub key 401s.
    # (teammate-B deploy 2026-06-29)
    _DROP = ("VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT",
             "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
             "OPENAI_API_KEY", "OPENAI_BASE_URL", "IMAGE_API_KEY", "IMAGE_BASE_URL",
             "IMAGE_MODEL", "IMAGE_PROVIDER",
             "SERPER_API_KEY", "SERPER_BASE_URL",
             "MODEL_API_FORMAT", "MODEL", "MODEL_BASE_URL", "MODEL_API_KEY",
             "MODEL_API_KEYS", "MODEL_API_KEY_FALLBACKS", "MODEL_CHANNEL_LABELS",
             "MODEL_POOL_ENV_PATH", "MODEL_ROUTE_POOL_PATH", "MODEL_CHANNEL_BALANCE", "MURAL_SKILL_DIR",
             "STUDIO_MAX_PER_MODEL", "STUDIO_MAX_CONCURRENT", "MURAL_RESOURCE_SLOTS_DIR",
             "MODEL_CONCURRENCY", "SLIDE_CONCURRENCY", "RENDER_CONCURRENCY", "PPT_RENDER_CONCURRENCY",
             # Limits are account-scoped.  Never inherit another launcher or
             # user's process-level values when 0 means Harness default.
             "MAX_TURNS", "CLEAN_MAX_TURNS", "CLEAN_CHILD_MAX_TURNS",
             "SUBAGENT_MAX_TURNS", "STUDIO_MAX_TURNS", "AGENT_MAX_TURNS", "OWNER_MAX_TURNS",
             "MAX_TOKENS", "SUBAGENT_MAX_TOKENS", "CLEAN_MAX_TOKENS",
             "STUDIO_MAX_TOKENS", "AGENT_MAX_TOKENS", "OWNER_MAX_TOKENS",
             "ORCHESTRATOR_MAX_TOKENS", "SLIDE_OWNER_MAX_TOKENS",
             "RESEARCH_OWNER_MAX_TOKENS", "IMAGE_OWNER_MAX_TOKENS",
             "CRITIC_MAX_TOKENS")
    _DROP = _DROP + (
        "STUDIO_THINKING_TRANSPORT",
        "STUDIO_REQUESTED_THINKING",
        "STUDIO_EFFECTIVE_THINKING",
        "STUDIO_REQUESTED_EFFORT",
        "STUDIO_EFFECTIVE_EFFORT",
        "STUDIO_ENABLE_THINKING",
        "THINKING",
        # Think switch is per-job.  Drop ambient canonical thinking vars so a
        # launcher/shell value cannot shadow the studio choice; they are set
        # explicitly below (CLEAN_THINKING) and via selection_env (THINK_EFFORT).
        "CLEAN_THINKING",
        "CLEAN_EFFORT",
        "THINK_EFFORT",
        "MODEL_THINKING",
        "MODEL_REASONING_EFFORT",
        # Authoring profile is a per-job Skill choice.  Never let an ambient
        # launcher value turn the stable public profile into creative.
        "MURAL_AUTHORING_PROFILE",
    )
    child_env = {k: v for k, v in os.environ.items()
                 if k not in _DROP and not k.startswith("MURAL_MINERU_")}
    child_env.update(engine.selection_env(
        model_key, pipeline_key, skill_key, model_config=model_config, skill_config=job["skill"]
    ))
    # Deployment-owned defaults come from namespaced environment variables;
    # user-owned WebUI settings override them for this job only.
    # API keys never enter job.json/result.json and are not process-global.
    child_env.update(service_config.system_runtime_env())
    child_env.update(service_config.runtime_env(row["user_id"]))
    # A Studio Deck performs only the cheap local preflight. Full Chromium,
    # LibreOffice and provider probes are available from 用户 → 环境检验 and
    # must not delay every generation request.
    child_env["CLEAN_DOCTOR_MODE"] = "lightweight"
    # Run profile is a per-task property.  Selection defaults remain
    # inference-safe, while an explicit Studio/API synthesis request must win
    # after deployment/user environment overlays so the trace cannot be
    # mislabeled as synthesis while actually running inference mechanics.
    requested_run_mode = str(seed.get("run_mode") or "inference").strip().lower()
    if requested_run_mode not in {"inference", "synthesis"}:
        raise ValueError(
            f"unsupported deck run_mode {requested_run_mode!r}; "
            "expected inference or synthesis"
        )
    child_env["MURAL_RUN_MODE"] = requested_run_mode
    # --- external visual critic switch ---
    # Independent visual critic (v1 harness). Every run mode honors the explicit
    # opt-in stored in the deck run-config.
    requested_visual_critic = bool(seed.get("enable_visual_critic"))
    critic_env = engine.external_visual_critic_env(
        requested_visual_critic, requested_run_mode
    )
    child_env.update(critic_env)
    # All WebDemo static Harnesses use the same per-turn completion budget.
    # Their historical defaults differ (VisualCraft 16K, Clean 32K), so pass
    # every compatibility variable explicitly instead of relying on defaults.
    limits = seed.get("runtime_limits")
    if not isinstance(limits, dict):
        limits = service_config.generation_limits(row["user_id"])
    token_budget = str(limits.get("max_tokens") or 40960)
    for token_env in (
        "MAX_TOKENS",
        "SUBAGENT_MAX_TOKENS",
        "CLEAN_MAX_TOKENS",
        "STUDIO_MAX_TOKENS",
        "AGENT_MAX_TOKENS",
    ):
        child_env[token_env] = token_budget
    child_env["OWNER_MAX_TOKENS"] = token_budget
    child_env.update(service_config.role_token_env(token_budget))
    static_max_turns = int(limits.get("static_max_turns") or 0)
    if static_max_turns:
        turn_budget = str(static_max_turns)
        child_env.update({
            "MAX_TURNS": turn_budget,
            "CLEAN_MAX_TURNS": turn_budget,
            "AGENT_MAX_TURNS": turn_budget,
        })
    static_subagent_max_turns = int(limits.get("static_subagent_max_turns") or 0)
    if static_subagent_max_turns:
        child_turn_budget = str(static_subagent_max_turns)
        child_env.update({
            "SUBAGENT_MAX_TURNS": child_turn_budget,
            "CLEAN_CHILD_MAX_TURNS": child_turn_budget,
            "OWNER_MAX_TURNS": child_turn_budget,
        })
    # Studio owns the user-facing Think switch.  Always pass an explicit value
    # so self-hosted OpenAI-compatible endpoints cannot silently fall back to
    # their server-side default (which may be thinking-on).
    requested_thinking = bool(seed.get("requested_thinking", seed.get("thinking")))
    effective_thinking = bool(seed.get("effective_thinking", seed.get("thinking")))
    child_env["STUDIO_REQUESTED_THINKING"] = "1" if requested_thinking else "0"
    child_env["STUDIO_EFFECTIVE_THINKING"] = "1" if effective_thinking else "0"
    child_env["STUDIO_ENABLE_THINKING"] = child_env["STUDIO_EFFECTIVE_THINKING"]
    child_env["THINKING"] = child_env["STUDIO_EFFECTIVE_THINKING"]
    # CLEAN_THINKING is the canonical switch the v0.4 harness reads
    # (core/config.py: THINKING = CLEAN_THINKING == "1"). STUDIO_*/THINKING are
    # not read there, so the studio Think toggle only reaches mural-v0.4 via this.
    child_env["CLEAN_THINKING"] = child_env["STUDIO_EFFECTIVE_THINKING"]
    child_env["MODEL_THINKING"] = child_env["STUDIO_EFFECTIVE_THINKING"]
    requested_effort = str(seed.get("requested_effort") or "").strip().lower()
    effective_effort = str(
        seed.get("effective_effort", seed.get("thinking_effort") or "")
    ).strip().lower()
    child_env["STUDIO_REQUESTED_EFFORT"] = requested_effort
    child_env["STUDIO_EFFECTIVE_EFFORT"] = effective_effort
    if effective_effort:
        child_env["CLEAN_EFFORT"] = effective_effort
        child_env["THINK_EFFORT"] = effective_effort
    else:
        child_env.pop("CLEAN_EFFORT", None)
        child_env.pop("THINK_EFFORT", None)
    child_env["MODEL_REASONING_EFFORT"] = service_config.validate_reasoning_effort(
        child_env.get("MODEL", ""), effective_effort, thinking=effective_thinking)
    child_env = engine.render_env(child_env)
    try:
        for spawn_attempt in range(2):
            try:
                proc = await asyncio.create_subprocess_exec(
                    *engine.runner_cmd(job_path),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=logf, stderr=asyncio.subprocess.STDOUT,
                    cwd=str(engine.INFERENCE_DIR), env=child_env,
                    start_new_session=True,   # 独立会话:studio 重启/被杀不会连带 TERM 引擎(曾致 exit=143 秒败)
                )
                break
            except OSError as exc:
                if spawn_attempt == 0 and exc.errno == errno.EBADF:
                    continue
                raise
        _running[deck_id] = proc
        spawn_ts = time.time()
        waiter = asyncio.ensure_future(proc.wait())
        res_path = run_dir / "result.json"

        def _result_fresh():
            # retry 复用 run_dir、不清旧产物 → 只认本次 spawn 之后写的 result.json,
            # 否则上一轮失败残留的 result.json 会让早收尾误杀刚起的新引擎。
            try:
                return res_path.stat().st_mtime >= spawn_ts - 1
            except OSError:
                return False

        # 早收尾:result.json 是引擎业务上的最后一笔;之后进程若迟迟不退,只可能是
        # 被超时遗弃的僵尸子线程拖住(非 daemon 线程阻塞解释器退出,期间还在白烧
        # 模型 API/GPU)。deck36 实测:内容 76min 做完,进程 230min 才退。
        # → 见到新 result.json 后给 10s 自然退出,仍滞留则终止进程止损。
        while True:
            done, _ = await asyncio.wait({waiter}, timeout=3)
            if done:
                rc = waiter.result()
                break
            if _result_fresh():
                done, _ = await asyncio.wait({waiter}, timeout=10)
                if done:
                    rc = waiter.result()
                    break
                print(f"[jobs] deck {deck_id}: result.json 已落盘但引擎滞留(僵尸子线程),终止止损",
                      flush=True)
                proc.terminate()
                done, _ = await asyncio.wait({waiter}, timeout=8)
                if not done:
                    proc.kill()
                    await waiter
                rc = waiter.result() if waiter.done() else None
                break
    except Exception as e:
        _fail_deck(deck_id, f"launch error: {type(e).__name__}: {e}")
        return
    finally:
        _running.pop(deck_id, None)
        logf.close()

    # studio 关停时 asyncio 会 TERM 它的直接子进程(uv 包装层,rc=143),
    # 但引擎(孙进程,独立会话)往往还活着 —— 此时转入领养观察,绝不能误标 failed。
    if _engine_alive(deck_id):
        await _adopt(deck_id, run_dir)
        return
    _finalize(deck_id, run_dir, rc)


def _kill_engine_procs(deck_id) -> bool:
    """TERM this deck's inference worker on Linux, macOS, or Windows."""
    killed = False
    for pid in _matching_engine_pids(deck_id):
        try:
            os.kill(pid, signal.SIGTERM)
            killed = True
        except OSError:
            continue
    return killed


async def _adopt(deck_id, run_dir: Path):
    """Watch an engine process that survived a studio restart; finalize when done."""
    res_path = run_dir / "result.json"
    row = _load_deck(deck_id)
    started_at = (row["started_at"] or 0) if row else 0

    def _result_fresh():
        # 领养的 deck 也可能是 retry 复用的 run_dir → 只认本次 started_at 之后的 result.json
        try:
            return res_path.stat().st_mtime >= started_at - 1
        except OSError:
            return False

    lingered = 0
    while _engine_alive(deck_id):
        if _result_fresh():
            lingered += 1
            if lingered == 3:      # 终止请求不等于进程退出，仍由 watcher 持有 Deck 槽。
                print(f"[jobs] deck {deck_id}(adopted): result.json 已落盘但引擎滞留,终止止损",
                      flush=True)
                _kill_engine_procs(deck_id)
        await asyncio.sleep(5)
    # small grace period for result.json to land after process exit
    for _ in range(4):
        if res_path.exists():
            break
        await asyncio.sleep(1)
    _finalize(deck_id, run_dir)


async def _run_scheduled(deck_id, adopted_run_dir=None):
    """进程确实结束后释放同一份 Deck 占用；取消请求本身不提前腾出资源。"""
    title_stop = asyncio.Event()
    try:
        row = _load_deck(deck_id)
        if row and row["run_dir"]:
            run_dir = Path(row["run_dir"])
            initial_plan = (_title_plan(run_dir)
                            if row["title_source"] == "plan" and adopted_run_dir is None else None)
            _schedule_title_refresh(deck_id, row, run_dir, title_stop, initial_plan=initial_plan)
        if adopted_run_dir is not None:
            await _adopt(deck_id, adopted_run_dir)
        else:
            row = _load_deck(deck_id)
            if row and row["status"] == "queued":
                await _process(deck_id)
    except Exception as e:
        _fail_deck(deck_id, f"dispatcher: {type(e).__name__}: {e}")
    finally:
        title_stop.set()
        _active.pop(deck_id, None)
        _scheduled.discard(deck_id)
        _drain_queues()


def _dispatch(deck_id, key, adopted_run_dir=None):
    if deck_id in _active:
        return
    _scheduled.add(deck_id)
    _active[deck_id] = key
    task = asyncio.create_task(_run_scheduled(deck_id, adopted_run_dir))
    _workers.append(task)
    task.add_done_callback(lambda done: _workers.remove(done) if done in _workers else None)


def _drain_queues():
    counts = Counter(_active.values())
    for key, queue in _queues.items():
        while queue and len(_active) < _limits["total"] and counts[key] < _limits["per_model"]:
            deck_id = queue.popleft()
            row = _load_deck(deck_id)
            if deck_id in _active:
                continue
            if not row or row["status"] != "queued":
                _scheduled.discard(deck_id)
                continue
            _dispatch(deck_id, key)
            counts[key] += 1


def _model_key(deck_id) -> str:
    row = _load_deck(deck_id)
    if row and custom_models.id_from_key(row["model"]) is not None:
        # Keep each custom model in its own bounded pool. Flash and Pro must
        # not accidentally share one global "deployment-model" queue.
        return str(row["model"])
    ck = engine.canon(row["model"]) if row and "model" in row.keys() else None
    return ck or engine.DEFAULT_MODEL   # 老 deck 的旧 key 经 alias 归一


def enqueue(deck_id):
    if _loop is None:
        return
    _loop.call_soon_threadsafe(_enqueue_bounded, deck_id)


def _enqueue_bounded(deck_id):
    if deck_id in _scheduled or deck_id in _running:
        return
    key = _model_key(deck_id)
    if _engine_alive(deck_id):
        row = _load_deck(deck_id)
        if row:
            _dispatch(deck_id, key, Path(row["run_dir"] or ""))
        return
    _scheduled.add(deck_id)
    _queues.setdefault(key, deque()).append(deck_id)
    _drain_queues()


async def cancel(deck_id) -> bool:
    _user_canceled.add(deck_id)
    row = _load_deck(deck_id)
    if row and row["status"] in ("waiting", "queued"):
        _set_status(
            deck_id,
            "interrupted",
            error="用户已停止",
            finished_at=int(time.time()),
        )
        _user_canceled.discard(deck_id)
        if deck_id not in _active:
            for queue in _queues.values():
                while deck_id in queue:
                    queue.remove(deck_id)
            _scheduled.discard(deck_id)
            _drain_queues()
        return True
    if row and row["status"] == "running":
        # Make the interruption visible immediately.  _finalize() keeps this
        # state authoritative after the subprocess exits.
        _set_status(
            deck_id,
            "interrupted",
            error="用户已停止",
            finished_at=int(time.time()),
        )
    proc = _running.get(deck_id)
    if proc is not None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        return True
    # adopted run (no handle): kill by scanning /proc
    return _kill_engine_procs(deck_id)


async def start():
    """Init dispatch state, recover/adopt orphaned runs, and start queued Decks."""
    global _limits, _loop
    _loop = asyncio.get_running_loop()
    _limits = service_config.load_runtime_config()["decks"]

    con = connect()
    try:
        running = [dict(r) for r in con.execute(
            "SELECT id, run_dir, status FROM decks WHERE status IN ('running', 'interrupted')")]
        queued = [r["id"] for r in con.execute(
            "SELECT id FROM decks WHERE status = 'queued' ORDER BY id")]
        waiting = [dict(r) for r in con.execute(
            "SELECT child.id, parent.status AS parent_status "
            "FROM decks child LEFT JOIN decks parent ON parent.id = child.parent_deck_id "
            "WHERE child.status = 'waiting' ORDER BY child.id"
        )]
    finally:
        con.close()

    for r in running:
        run_dir = Path(r["run_dir"] or "")
        if _engine_alive(r["id"]):
            # engine survived the restart — watch it, do NOT spawn a second one
            _dispatch(r["id"], _model_key(r["id"]), run_dir)
        elif r["status"] == "interrupted":
            continue
        elif (run_dir / "result.json").exists():
            _finalize(r["id"], run_dir)
            title_stop = asyncio.Event()
            title_stop.set()
            _schedule_title_refresh(r["id"], _load_deck(r["id"]), run_dir, title_stop)
        else:
            _set_status(r["id"], "queued")
            queued.append(r["id"])

    for item in waiting:
        if item["parent_status"] == "completed":
            _set_status(item["id"], "queued", error=None)
            queued.append(item["id"])
        elif item["parent_status"] in ("failed", "rejected", None):
            _fail_deck(item["id"], "父版本未成功完成，修订未执行")

    for did in sorted(set(queued)):
        enqueue(did)
