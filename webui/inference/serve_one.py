#!/usr/bin/env python3
"""单条 live 生成入口 —— 给 studio 后端调用,跑一份 PPT。

不引入并行 / manifest / 进度条;**直接复用 `runtime.worker` 的单样本逻辑**
(load_dotenv / ANTHROPIC_API_KEY 检查 / exist_ok=False 隔离 + 脏残留清理 / 异常捕获),
跑完把结果 rec 写到 <run_dir>/result.json,供 studio dispatcher 读取判定状态。

由 studio 的 dispatcher 在 **inference 的 uv 环境**里起(这样能 import agent_loop 的
重依赖:anthropic / playwright …):

    uv run --project <inference> python <inference>/serve_one.py --job /abs/job.json

job.json:
    {"sample_id": "u1_d7", "seed": {"query": "...", "lang": "zh", "slide_count": 12},
     "run_dir": "/abs/.../decks/7", "dry_run": false, "batch": "studio"}
"""
import argparse
import hashlib
import importlib.util
import importlib
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import traceback
import types
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import runtime as runtime_driver  # noqa: E402  (reuse worker/build_config/load_dotenv)
from attachments_runtime import build_initial_user_content, stage_seed_attachments  # noqa: E402


def _install_thinking_transport():
    """Inject the Studio Think choice into versioned OpenAI shims.

    Versioned Harnesses intentionally remain immutable and each carries its
    own ``core.openai_backend`` module.  They all use ``urllib.request.Request``
    for chat completions, so this process-local adapter applies the request
    option without rewriting every frozen Harness copy.
    """
    transport = os.environ.get("STUDIO_THINKING_TRANSPORT", "").strip().lower()
    # ``openai`` was the old deployment default even though this runtime uses
    # OpenAI-compatible chat completions. Treat it as the concrete
    # chat_template_kwargs transport so old jobs also send an explicit choice.
    if transport not in {"chat_template_kwargs", "openai"}:
        return
    original = urllib.request.Request
    if getattr(original, "_studio_thinking_adapter", False):
        return
    enabled = os.environ.get(
        "STUDIO_EFFECTIVE_THINKING",
        os.environ.get("STUDIO_ENABLE_THINKING", "0"),
    ) == "1"

    class StudioRequest(original):
        """Request-compatible adapter that preserves urllib's class contract.

        httpx subclasses ``urllib.request.Request`` while it is imported.  A
        function wrapper therefore breaks unrelated Harness imports with
        ``TypeError: function() argument 'code' must be code, not str``.  Keep
        the transport hook as a real subclass so both direct construction and
        third-party subclassing continue to work.
        """

        _studio_thinking_adapter = True

        def __init__(self, url, data=None, headers=None, origin_req_host=None,
                     unverifiable=False, method=None):
            target = getattr(url, "full_url", url)
            if data and str(target).rstrip("/").endswith("/chat/completions"):
                try:
                    payload = json.loads(
                        data.decode("utf-8") if isinstance(data, bytes) else data
                    )
                    payload["chat_template_kwargs"] = {"enable_thinking": enabled}
                    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                except (TypeError, ValueError, UnicodeDecodeError):
                    pass
            super().__init__(
                url,
                data=data,
                headers=headers or {},
                origin_req_host=origin_req_host,
                unverifiable=unverifiable,
                method=method,
            )

    urllib.request.Request = StudioRequest


def _load_module(name, path, root):
    """Load a pipeline module from an explicit file path with its root on sys.path."""
    root = os.path.abspath(root)
    path = os.path.abspath(path)
    if root not in sys.path:
        sys.path.insert(0, root)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module spec: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _select_pipeline(job):
    """Return the inference module for this job and apply any version-specific wiring."""
    version = job.get("pipeline_version") or "current"
    pipe = job.get("pipeline") or {}
    root = pipe.get("path")
    entry = pipe.get("entry") or "presenter.py"
    if not root:
        raise RuntimeError(f"pipeline {version} missing path")
    skill_mode = pipe.get("skill_mode") or "ppt-skill-html"
    skill_mount = job.get("skill_mount_dir")
    # Set path contracts before importing the external Harness: both the V2
    # thin Harness and the legacy Web Demo read these values at import/runtime.
    if skill_mode == "ppt-skill":
        skill_path = (job.get("skill") or {}).get("path") or skill_mount
        if skill_path:
            os.environ["PPT_SKILL_DIR"] = os.path.abspath(skill_path)
    elif skill_mount:
        os.environ["PPT_SKILLS_ROOT"] = os.path.abspath(skill_mount)
    safe_version = "".join(ch if ch.isalnum() else "_" for ch in version)
    mod = _load_module(
        f"pptagent_pipeline_{safe_version}", os.path.join(root, entry), root
    )
    # Every catalog entry reads the logical skills/ppt-skill-html root. Point it
    # at the mount generated for the selected Skill version.
    if skill_mode == "ppt-skill-html" and skill_mount:
        mod.SKILLS_DIR = os.path.abspath(skill_mount)
    return mod


def _uses_clean_runner(job):
    """Route every Clean Harness profile through its broker-owning runner.

    Versioned profiles may append an orchestration qualifier to the base mode
    (for example ``clean-bilingual-single-slide``).  They still share the same
    infer.py/core.run_batch entry contract and must not fall through to the
    legacy module-level worker interface.
    """
    mode = str((job.get("pipeline") or {}).get("skill_mode") or "")
    return (
        mode == "clean-bilingual"
        or mode.startswith("clean-bilingual-")
        or mode in {
            "mural-presenter-v0.3-adaptive",
            "mural-presenter-v0.4-adaptive",
            # v0.5 shares the exact v0.4 harness/runner contract; it differs
            # only in the skill package (base.css, layout primitives, prompts).
            "mural-presenter-v0.5-adaptive",
            # v0.6 remains routable for historical retries. v1.0 is the
            # public paper/data-synthesis release and uses the same broker-owned
            # run_batch entry contract.
            "mural-presenter-v0.6-adaptive",
            "mural-presenter-v1.0-adaptive",
            # Ablation variants share the exact v0.4 harness/runner contract;
            # they only differ in skill knowledge (nk) and solo_mode env flag.
            "mural-presenter-v0.4-solo",
            "mural-presenter-v0.4-nk",
            "mural-presenter-v0.4-nk-solo",
        }
    )


def _uses_mural_v1_runner(job):
    """Route the shared-runtime v1 profile through its native CLI contract."""
    return str((job.get("pipeline") or {}).get("skill_mode") or "") == (
        "mural-presenter-v1"
    )


def _mural_v1_attachment_paths(seed):
    """Return original Studio uploads plus any explicit intent for native v1 staging.

    Each element is ``(path, intent)``; ``intent`` is ``None`` unless the Studio
    seed attachment record carries an explicit ``intent`` (style_reference /
    content_source / mixed). Studio's current upload UI does not yet collect a
    per-attachment intent, so this stays ``None`` for existing decks and only
    activates once a seed record supplies one — the harness then routes it
    instead of flattening every image to ``mixed``.
    """
    paths = []
    seen = set()
    for collection in (seed.get("attachments") or [], seed.get("_raw_attachments") or []):
        if not isinstance(collection, list):
            continue
        for item in collection:
            if not isinstance(item, dict):
                continue
            raw = item.get("path") or item.get("source_path")
            if not raw:
                continue
            path = Path(str(raw)).expanduser().resolve()
            marker = str(path)
            if marker in seen:
                continue
            if not path.is_file():
                raise FileNotFoundError(f"Studio attachment does not exist: {path}")
            seen.add(marker)
            intent = item.get("intent")
            paths.append((path, str(intent) if intent else None))
    return paths


def _build_mural_v1_font_request(seed):
    """Build the canonical font-request JSON WITHOUT writing into the workspace.

    Under pure layout v2 the workspace must stay empty until the Harness writes
    ``_state/workspace.json`` first. So the WebUI only resolves + validates the
    pinned Studio font objects here and returns a canonical request string; the
    Harness (SkillRuntime) writes ``_state/font-request.json`` after it creates
    the WorkspaceState manifest.
    """
    config = seed.get("font_config")
    if not isinstance(config, dict):
        return None
    user_id = str(config.get("user_id") or "")
    active_user = str(os.environ.get("MURAL_USER_ID") or "")
    if not user_id or user_id != active_user:
        raise ValueError("Studio font request does not belong to the active user")
    data_value = os.environ.get("MURAL_STUDIO_DATA_DIR") or os.environ.get("STUDIO_DATA_DIR")
    if not data_value:
        raise ValueError("Studio data root is required for a pinned font request")
    studio_data = Path(data_value).expanduser().resolve()
    private_root = (studio_data / "users" / user_id / "_shared" / "fonts").resolve()
    pinned = []
    for item in config.get("fonts") or []:
        if not isinstance(item, dict):
            raise ValueError("Studio custom font record must be an object")
        record = dict(item)
        record.pop("path", None)
        relative = Path(str(record.get("object_ref") or ""))
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise ValueError("Studio custom font object reference is invalid")
        source = (studio_data / relative).resolve()
        try:
            source.relative_to(private_root)
        except ValueError as exc:
            raise ValueError("Studio custom font object escapes the active user's store") from exc
        if not source.is_file() or source.is_symlink() or not source.stat().st_size:
            raise FileNotFoundError(f"Studio custom font object does not exist: {relative}")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if digest != str(record.get("sha256") or ""):
            raise ValueError(f"Studio custom font object hash mismatch: {relative}")
        if int(record.get("bytes") or 0) != source.stat().st_size:
            raise ValueError(f"Studio custom font object size mismatch: {relative}")
        record.pop("source_path", None)
        pinned.append(record)
    payload = {
        "schema": "mural.font-request.v1",
        "version": int(config.get("version") or 1),
        "deck_id": int(config.get("deck_id") or 0),
        "user_id": user_id,
        "license_acknowledged": bool(config.get("license_acknowledged")),
        "roles": dict(config.get("roles") or {}),
        "fonts": pinned,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _run_mural_v1(job):
    """Run one Studio job through the shared-runtime v1 native CLI."""
    pipe = job.get("pipeline") or {}
    root = Path(str(pipe.get("path") or "")).expanduser().resolve()
    entry = str(pipe.get("entry") or "infer.py")
    infer_path = root / entry
    if entry != "infer.py" or not infer_path.is_file():
        raise RuntimeError(f"MURAL v1 infer entry does not exist: {infer_path}")

    sample_id = job["sample_id"]
    seed = dict(job["seed"])
    run_dir = Path(job["run_dir"]).expanduser().resolve()
    started = time.time()
    if job.get("dry_run"):
        rec = {
            "sample_id": sample_id,
            "run_dir": str(run_dir),
            "status": "completed",
            "dry_run": True,
            "skill_name": (job.get("skill") or {}).get("name"),
            "skill_language": (job.get("skill") or {}).get("language"),
            "resume_workspace": bool(job.get("resume_workspace")),
        }
        return _write_result(str(run_dir), rec, job, started)

    run_dir.mkdir(parents=True, exist_ok=True)
    font_request_json = _build_mural_v1_font_request(seed)
    command = [
        sys.executable,
        str(infer_path),
        "--query",
        str(seed.get("query") or ""),
        "--workspace",
        str(run_dir),
        "--mode",
        str(seed.get("run_mode") or "inference"),
        "--authoring-profile",
        str(seed.get("authoring_profile") or "auto"),
        "--language",
        str(seed.get("lang") or "auto"),
        "--title",
        str(seed.get("title") or seed.get("query") or "MURAL Presentation")[:240],
    ]
    # inference 与 synthesis 都由用户按需导出；训练准入只依赖当前页面验收。
    command.extend(["--no-pdf", "--no-pptx", "--no-zip"])
    page_count = int(seed.get("slide_count") or 0)
    if page_count > 0:
        command.extend(["--page-count", str(page_count)])
    revision = seed.get("_revision")
    if isinstance(revision, dict):
        command.extend([
            "--revision-json",
            json.dumps(revision, ensure_ascii=False, sort_keys=True),
        ])
    staged_attachments = _mural_v1_attachment_paths(seed)
    # --attachment-intent is a parallel list; the harness requires it to align
    # one-to-one with --attachment when present. Emit it for every attachment
    # (empty string = unspecified) only when at least one carries an intent, so
    # decks without any intent keep the exact historical argument vector.
    emit_intents = any(intent for _path, intent in staged_attachments)
    for attachment, intent in staged_attachments:
        command.extend(["--attachment", str(attachment)])
        if emit_intents:
            command.extend(["--attachment-intent", str(intent or "")])

    child_env = os.environ.copy()
    result_path = run_dir / "result.json"
    prior_result = result_path.stat() if result_path.is_file() else None
    host_path = run_dir / "_state" / "skill-runtime.json"
    prior_host = host_path.stat() if host_path.is_file() else None
    if font_request_json is not None:
        child_env["MURAL_FONT_REQUEST_JSON"] = font_request_json
    process = subprocess.Popen(
        command,
        cwd=str(root),
        env=child_env,
        stdin=subprocess.DEVNULL,
    )
    previous_handlers = {}

    def forward_signal(signum, _frame):
        if process.poll() is None:
            process.send_signal(signum)

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, forward_signal)
    try:
        returncode = process.wait()
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    harness_result = {}
    result_stat = result_path.stat() if result_path.is_file() else None
    fresh_result = result_stat is not None and (prior_result is None or
        (result_stat.st_ino, result_stat.st_mtime_ns, result_stat.st_size) !=
        (prior_result.st_ino, prior_result.st_mtime_ns, prior_result.st_size))
    if fresh_result:
        try:
            value = json.loads(result_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                harness_result = value
        except (OSError, ValueError):
            harness_result = {}
    training_rejected = (
        harness_result.get("schema") == "mural.result.v1"
        and harness_result.get("status") == "completed"
        and harness_result.get("artifact_status") == "complete"
        and harness_result.get("exit_reason") == "training_rejected"
        and harness_result.get("training_admission") == "rejected"
    )
    critic_pending = (
        harness_result.get("schema") == "mural.result.v1"
        and harness_result.get("status") == "error"
        and harness_result.get("artifact_status") == "complete"
        and harness_result.get("exit_reason") == "critic_incomplete"
        and harness_result.get("training_admission") == "pending"
    )
    if (returncode and not (training_rejected or critic_pending)) or not harness_result:
        failure = {}
        host_stat = host_path.stat() if host_path.is_file() else None
        if host_stat is not None and (prior_host is None or
            (host_stat.st_ino, host_stat.st_mtime_ns, host_stat.st_size) !=
            (prior_host.st_ino, prior_host.st_mtime_ns, prior_host.st_size)):
            try:
                host = json.loads(host_path.read_text(encoding="utf-8"))
                if isinstance(host, dict) and host.get("status") == "failed":
                    failure = host.get("agent_failed") or {}
            except (OSError, ValueError):
                pass
        if not isinstance(failure, dict):
            failure = {}
        error_type = str(failure.get("error_type") or "")
        exit_reason = "process_interrupted" if returncode < 0 else {
            "RepeatedToolFailureError": "tool_circuit_open",
            "AgentTurnLimitError": "agent_turn_limit",
            "AgentProviderError": "api_failed",
            "AgentInterruptedError": "process_interrupted",
            "ResourceSlotTimeout": "resource_queue_timeout",
            "AgentProtocolError": "agent_protocol_error",
        }.get(error_type, "runtime_error")
        rec = {
            "sample_id": sample_id,
            "run_dir": str(run_dir),
            "status": "error",
            "exit_reason": exit_reason,
            "error_type": error_type,
            "failure": failure,
            "error": (
                f"MURAL v1 exited with code {returncode}" + (
                    ": " + str(failure["error_summary"]) if failure.get("error_summary") else "")
                if returncode
                else "MURAL v1 produced no valid result.json"
            ),
            "resume_workspace": bool(job.get("resume_workspace")),
        }
        return _write_result(str(run_dir), rec, job, started)

    page_count = int(harness_result.get("page_count") or 0)
    skill_ref = json.loads((run_dir / "_state" / "skill-ref.json").read_text(encoding="utf-8"))
    if skill_ref.get("schema") != "mural.skill-ref.v1" or not skill_ref.get("skill_name"):
        raise RuntimeError("MURAL v1 completed without a valid pinned Skill identity")
    rec = {
        **harness_result,
        "sample_id": sample_id,
        "run_dir": str(run_dir),
        "n_slides": page_count,
        "engine_returncode": returncode,
        "accept_detail": {
            "n_pages": page_count,
            "delivery_manifest": harness_result.get("delivery_manifest"),
        },
        "skill_name": skill_ref["skill_name"],
        "skill_language": (job.get("skill") or {}).get("language"),
        "selected_skill_name": job.get("selected_skill_name") or (job.get("skill") or {}).get("name"),
        "selected_skill_version": job.get("skill_version"),
        "resume_workspace": bool(job.get("resume_workspace")),
    }
    return _write_result(str(run_dir), rec, job, started)


def _install_subagent_turn_adapter(job):
    """Apply Studio's per-child turn limit to versioned external Harnesses.

    Current in-repo and Clean Harnesses have native child-limit fields.  Some
    immutable/versioned Harnesses still clone the orchestrator config for each
    child (and a few impose a role-specific constant afterwards).  Patch the
    loaded Agent class for this one worker process so every delegated Agent
    receives the account's explicit limit and writes it to its trace config.
    """
    raw_limit = os.environ.get("SUBAGENT_MAX_TURNS", "").strip()
    if not raw_limit:
        return
    try:
        limit = int(raw_limit)
    except ValueError:
        return
    if limit <= 0:
        return
    root = os.path.abspath((job.get("pipeline") or {}).get("path") or "")
    if not os.path.isfile(os.path.join(root, "core", "agent.py")):
        return
    try:
        agent_module = importlib.import_module("core.agent")
        agent_cls = agent_module.Agent
    except (ImportError, AttributeError):
        return
    original = agent_cls.__init__
    if getattr(original, "_studio_subagent_turn_adapter", False):
        return
    signature = inspect.signature(original)

    def wrapped(self, *args, **kwargs):
        bound = signature.bind(self, *args, **kwargs)
        role = bound.arguments.get("role")
        if role != "orchestrator":
            config = dict(bound.arguments.get("config") or {})
            config["max_turns"] = limit
            bound.arguments["config"] = config
        return original(*bound.args, **bound.kwargs)

    wrapped._studio_subagent_turn_adapter = True
    agent_cls.__init__ = wrapped


def _write_result(run_dir, rec, job, started):
    """Persist the common Studio completion contract for every Harness."""
    rec["duration_s"] = round(time.time() - started, 1)
    rec["pipeline_version"] = job.get("pipeline_version", "infer")
    rec["skill_version"] = job.get("skill_version")
    os.makedirs(run_dir, exist_ok=True)
    pending = os.path.join(run_dir, f".result-{os.getpid()}.pending")
    with open(pending, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(pending, os.path.join(run_dir, "result.json"))
    return rec


def _run_clean_infer(job):
    """Run one Studio job through static_ppt-clean-current/infer.py.

    infer.py owns the render broker and core.run_batch owns the one-deck worker.
    Studio supplies an exact run_dir, so calling the worker directly preserves
    the Web Demo workspace/result.json contract without creating an extra
    runs/<batch>/<sample> nesting layer.
    """
    pipe = job.get("pipeline") or {}
    root = os.path.abspath(pipe.get("path") or "")
    entry = pipe.get("entry") or "infer.py"
    if not root or entry != "infer.py":
        raise RuntimeError("clean infer pipeline is missing its infer.py entry")
    infer_path = os.path.join(root, entry)
    if not os.path.isfile(infer_path):
        raise RuntimeError(f"clean infer entry does not exist: {infer_path}")

    sample_id = job["sample_id"]
    seed = dict(job["seed"])
    run_dir = job["run_dir"]
    started = time.time()

    if job.get("dry_run"):
        os.makedirs(run_dir, exist_ok=True)
        rec = {
            "sample_id": sample_id,
            "run_dir": run_dir,
            "status": "completed",
            "dry_run": True,
            "skill_name": (job.get("skill") or {}).get("name"),
            "skill_language": (job.get("skill") or {}).get("language"),
        }
        return _write_result(run_dir, rec, job, started)

    infer = _load_module("pptagent_clean_infer", infer_path, root)
    from core import environment_doctor, model_call, run_batch  # noqa: E402

    if str(pipe.get("skill_mode") or "") == "mural-presenter-v1.0-adaptive":
        from core.release_guard import verify_runtime_release  # noqa: E402

        verify_runtime_release(
            str(seed.get("run_mode") or os.environ.get("MURAL_RUN_MODE", "inference"))
        )

    if os.environ.get("MODEL_BACKEND", "").lower() == "openai":
        import openai_backend  # noqa: E402

        shim = openai_backend.OpenAIShim(
            base=os.environ.get("STUDENT_BASE_URL", ""),
            model=os.environ.get("STUDENT_MODEL") or job.get("model"),
            key=os.environ.get("STUDENT_API_KEY", "EMPTY"),
            timeout=int(os.environ.get("CLEAN_MODEL_TIMEOUT", "600")),
        )
        # Clean Harness revisions may pass a per-call base_url into _client.
        # Studio already resolved the selected OpenAI endpoint into this shim,
        # so accept and intentionally ignore that optional compatibility arg.
        model_call._client = lambda _base_url=None: shim

    cfg_args = types.SimpleNamespace(
        batch=job.get("batch", "studio"),
        workers=1,
        max_attempts=1,
    )
    config = run_batch.build_config(cfg_args)
    config["model"] = job.get("model") or config["model"]
    config["requested_thinking"] = bool(
        seed.get("requested_thinking", seed.get("thinking"))
    )
    config["effective_thinking"] = bool(
        seed.get("effective_thinking", seed.get("thinking"))
    )
    config["thinking_transport"] = str(seed.get("thinking_transport") or "")
    config["thinking"] = config["effective_thinking"]
    config["requested_effort"] = str(
        seed.get("requested_effort") or ""
    ).strip().lower()
    config["effective_effort"] = str(
        seed.get("effective_effort", seed.get("thinking_effort") or "")
    ).strip().lower()
    config["effort"] = config["effective_effort"]
    skill = job.get("skill") or {}
    if skill.get("language") == "auto":
        config["skill_name"], config["skill_language"] = run_batch.select_skill(seed)
    else:
        config["skill_name"] = skill.get("name")
        config["skill_language"] = skill.get("language")
    # ``run_batch.worker`` only consumes a supervisor-owned Doctor report; it
    # must never launch provider probes per sample.  Studio is the supervisor
    # for this one-Deck job, so run its cheap local check once and inject the
    # resulting batch contract before entering the worker.  The worker copies
    # the same report into ``_trace`` after it atomically creates the workspace.
    doctor_report = environment_doctor.run_lightweight_environment_check(
        config,
        required_attachment_suffixes=run_batch.attachment_suffixes([seed]),
    )
    if doctor_report.get("checked") is not True or doctor_report.get("ready") is not True:
        raise RuntimeError(
            "Studio environment preflight blocked execution: "
            + "; ".join(
                str(item)
                for item in doctor_report.get("fatal_errors") or ["unknown failure"]
            )
        )
    config["_environment_doctor"] = doctor_report
    task = {
        "sample_id": sample_id,
        "seed": seed,
        "run_dir": run_dir,
        "config": config,
    }
    if bool(job.get("resume_workspace")):
        if not run_batch.run_dir_recoverable(run_dir):
            rec = {
                "sample_id": sample_id,
                "run_dir": run_dir,
                "status": "error",
                "error": "workspace does not satisfy the durable resume contract",
                "resume_workspace": True,
            }
            return _write_result(run_dir, rec, job, started)
        trace_source = run_batch._latest_orchestrator_trace(run_dir)
        task.update({
            "resume_workspace": True,
            "resume_trace_source": str(trace_source or ""),
            "resume_attempt": run_batch._next_resume_attempt(run_dir),
        })

    infer._load_render_environment()
    infer._configure_runtime_environment()
    broker_process, broker_dir = infer._start_render_broker()
    try:
        worker = run_batch.revision_worker if seed.get("_revision") else run_batch.worker
        rec = worker(task)
    except Exception as exc:
        rec = {
            "sample_id": sample_id,
            "run_dir": run_dir,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc()[-1500:],
        }
    finally:
        infer._stop_render_broker(broker_process, broker_dir)
    selected_name = rec.get("skill_name") or config.get("skill_name")
    selected_version = rec.get("skill_language") or config.get("skill_language")
    # clean run_batch.worker keeps its pre-Agent config fields. Auto's actual
    # first-turn choice is authoritative in the orchestrator trace config.
    try:
        selected_trace = (
            run_batch._latest_orchestrator_trace(run_dir)
            if bool(job.get("resume_workspace"))
            else None
        )
        selected_config = (
            selected_trace.parent / "config.json"
            if selected_trace is not None
            else os.path.join(run_dir, "_trace", "orchestrator", "config.json")
        )
        with open(selected_config, encoding="utf-8") as f:
            trace_config = json.load(f)
        selected_name = trace_config.get("skill_name") or selected_name
        selected_version = trace_config.get("skill_language") or selected_version
    except (OSError, ValueError, TypeError):
        pass
    rec["skill_name"] = selected_name
    rec["skill_language"] = selected_version
    rec["selected_skill_name"] = selected_name
    rec["selected_skill_version"] = selected_version
    rec["resume_workspace"] = bool(job.get("resume_workspace"))
    return _write_result(run_dir, rec, job, started)


def _patch_attachment_staging(pipe):
    """Wrap versioned pipeline run_sample so image attachments land in work_dir.

    A catalog harness can run on a local scratch work_dir before persisting back to the final
    run_dir, so staging has to happen inside run_sample rather than before worker().
    The current pipeline stages in runtime.worker; wrapping it here is harmless only
    when the module exposes run_sample directly.
    """
    run_sample = getattr(pipe, "run_sample", None)
    if not callable(run_sample) or getattr(pipe, "_studio_attachment_staging", False):
        return

    def wrapped(sample_id, seed, run_dir, config):
        stage_seed_attachments(seed, run_dir)
        return run_sample(sample_id, seed, run_dir, config)

    pipe.run_sample = wrapped
    pipe._studio_attachment_staging = True


def _patch_attachment_message_content(pipe):
    """Patch versioned pipelines so staged images become initial message image blocks."""
    seed_to_brief = getattr(pipe, "seed_to_brief", None)
    if not callable(seed_to_brief) or getattr(pipe, "_studio_attachment_message_content", False):
        return

    def wrapped(seed, *args, **kwargs):
        # Versioned Harnesses do not share one seed_to_brief signature.  The
        # long-horizon presenter passes its staged attachment manifest as a
        # second positional argument; preserve every argument while adding the
        # Studio image blocks around the returned brief.
        return build_initial_user_content(seed, seed_to_brief(seed, *args, **kwargs))

    pipe.seed_to_brief = wrapped
    pipe._studio_attachment_message_content = True


def _patch_optional_harness_dotenv(pipe):
    """Treat a version Harness .env as optional; Studio injects credentials."""
    original = getattr(pipe, "load_dotenv", None)
    if not callable(original) or getattr(pipe, "_studio_optional_dotenv", False):
        return

    def wrapped():
        try:
            return original()
        except PermissionError as exc:
            print(f"[serve_one] skip unreadable harness .env: {exc}", file=sys.stderr)
            return None

    pipe.load_dotenv = wrapped
    pipe._studio_optional_dotenv = True


def main():
    ap = argparse.ArgumentParser(description="单条 live PPT 生成(供 studio 调用)")
    ap.add_argument("--job", required=True, help="job spec json 路径")
    args = ap.parse_args()

    with open(args.job, encoding="utf-8") as f:
        job = json.load(f)

    _install_thinking_transport()

    sample_id = job["sample_id"]
    seed = job["seed"]
    run_dir = job["run_dir"]
    dry = bool(job.get("dry_run"))

    if not _uses_mural_v1_runner(job):
        raise RuntimeError("unsupported_harness: only mural-presenter-v1 is active")
    if _uses_mural_v1_runner(job):
        rec = _run_mural_v1(job)
        accept_detail = rec.get("accept_detail")
        accepted_pages = (
            accept_detail.get("n_pages")
            if isinstance(accept_detail, dict)
            else None
        )
        print(json.dumps({
            "status": rec.get("status"),
            "run_dir": run_dir,
            # n_renders includes incremental page retries and is not a slide
            # count. Prefer the accepted renderer manifest cardinality.
            "n_slides": rec.get("n_slides", accepted_pages),
        }, ensure_ascii=False))
        sys.exit(
            0
            if rec.get("status") in {"completed", "needs_improvement"}
            else 1
        )

    pipe = _select_pipeline(job)
    _install_subagent_turn_adapter(job)
    _patch_attachment_staging(pipe)
    _patch_attachment_message_content(pipe)
    _patch_optional_harness_dotenv(pipe)
    # Studio credentials live in this repo's inference/.env and are also
    # injected by jobs.py. Frozen/versioned Harness directories intentionally
    # do not own readable credentials, so their optional .env must not block a
    # run when the process environment is already complete.
    runtime_driver.load_dotenv()
    pipe.load_dotenv()

    # 复用 inference 的标准 config;dry_run 走 runtime.worker 里的 _dry_worker 分支。
    cfg_args = types.SimpleNamespace(batch=job.get("batch", "studio"), dry_run=dry)
    config = pipe.build_config(cfg_args)
    for k in ("model", "max_turns", "max_tokens"):   # 允许 job 覆盖少量参数
        if job.get(k) is not None and k in config:
            config[k] = job[k]
    config["pipeline_version"] = job.get("pipeline_version", "current")
    config["skill_version"] = job.get("skill_version")
    config["thinking"] = bool(seed.get("thinking"))

    task = {"sample_id": sample_id, "seed": seed, "run_dir": run_dir, "config": config}

    started = time.time()
    try:
        if seed.get("_revision"):
            worker = getattr(pipe, "revision_worker", None)
            if not callable(worker):
                raise RuntimeError("所选 Harness 尚未实现 revision_worker")
        else:
            worker = pipe.worker
        rec = worker(task)                    # ← pipeline 的单样本调用(隔离/容错都在里面)
    except Exception as e:                    # worker 已自捕获,这层只是最后兜底
        rec = {"sample_id": sample_id, "run_dir": run_dir, "status": "error",
               "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:]}
    rec["duration_s"] = round(time.time() - started, 1)
    rec["pipeline_version"] = job.get("pipeline_version", "current")
    rec["skill_version"] = job.get("skill_version")

    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "result.json"), "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)

    print(json.dumps({"status": rec.get("status"), "run_dir": run_dir,
                      "n_slides": rec.get("n_slides", rec.get("slides"))}, ensure_ascii=False))
    sys.exit(0 if rec.get("status") == "completed" else 1)


if __name__ == "__main__":
    main()
