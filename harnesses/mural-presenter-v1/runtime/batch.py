"""薄批量合成层：每行 query = 一个 deck，全部复用 infer.py 的单 deck 执行路径。

本模块**不复制** agent loop：它按行构造与单 deck 等价的 ``RunSpec``，直接调用注入的
``run_deck`` 回调(默认 = infer 的 ``run_presenter`` + SkillRuntime 装配),再把逐样本
attempt 记录聚合成 ``run.sh`` 消费的 ``<batch>.summary.json``。

返回 RunOutcome 即 raw 制作完成，不代表训练准入。导出已有质量决定时不重做 raw；
可恢复故障经冷却后在原工作区续接，受 max-attempts 约束。输入变化或不可重试的
Provider 拒绝保留现场并停止，不清空或自动归档工作区。
"""
from __future__ import annotations

from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import traceback
from typing import Callable, Mapping, Sequence

from export.training import TrainingExportError
from owner.context import OwnerContextStore
from runtime.agent import (
    AgentProviderError, AgentRuntimeError, COMPLETION_GAP_STALL_MESSAGE,
    FEEDBACK_ROUND_TOOL_ERRORS, OWNER_FAILURE_STALL_MESSAGE, RepeatedToolFailureError,
)
from runtime.config import RunMode, RunSpec, merge_environment, read_environment_file


BATCH_SUMMARY_SCHEMA = "mural.batch-summary.v1"

SUCCESS_STATUSES = {"completed"}
# 制作/内容终态与可重试性分开；失败必须显式声明恢复条件，不能仅因非终态就自动重试。
CONTENT_TERMINAL_STATUSES = {"content_excluded"}
TERMINAL = SUCCESS_STATUSES | CONTENT_TERMINAL_STATUSES
_RECOVERABLE_COMPACTION_PREFIX = (
    "owner_interrupted: context_compaction_recoverable; checkpoint retained;"
)
_RECOVERABLE_OWNER_STALL_PREFIXES = (
    OWNER_FAILURE_STALL_MESSAGE,
    COMPLETION_GAP_STALL_MESSAGE,
)


# run_deck 回调签名：给定 RunSpec 与已合并环境，执行一次单 deck，返回 RunOutcome-like
# 对象(至少有 .to_dict() 或 .training_manifest)。抛 TrainingExportError = 内容排除,
# 抛其它异常 = 运维失败。测试可注入替身,不打真模型。
RunDeck = Callable[[RunSpec, Mapping[str, str]], object]


@dataclass(frozen=True)
class BatchPaths:
    """批量层的输出根：runs/<batch>/<sid> 与 logs/<batch>.*。"""

    runs_dir: Path
    logs_dir: Path

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "BatchPaths":
        """从 run.sh 导出的 CLEAN_* 环境解析输出根,附带约定回退。"""
        artifact_root = str(environment.get("CLEAN_ARTIFACT_ROOT") or "").strip()
        runs = str(environment.get("CLEAN_RUNS_DIR") or "").strip()
        logs = str(environment.get("CLEAN_LOGS_DIR") or "").strip()
        if not runs:
            base = artifact_root or str(Path.cwd())
            runs = str(Path(base) / "runs")
        if not logs:
            base = artifact_root or str(Path(runs).parent)
            logs = str(Path(base) / "logs")
        return cls(runs_dir=Path(runs), logs_dir=Path(logs))


@dataclass
class BatchArgs:
    """批量子命令的显式输入(从 CLI 解析后传入)。"""

    queries: Path
    batch: str
    workers: int = 8
    max_attempts: int = 3
    resume: bool = False
    limit: int = 0
    run_mode: RunMode = RunMode.SYNTHESIS
    authoring_profile: str | None = None
    include_pdf: bool = False
    include_pptx: bool = False
    include_zip: bool = False
    title: str = "MURAL Presentation"


@dataclass
class _Sample:
    sample_id: str
    seed: Mapping[str, object]
    run_dir: Path


# ---------------------------------------------------------------------------
# JSONL / manifest I/O
# ---------------------------------------------------------------------------


def load_queries(path: str | os.PathLike[str]) -> list[dict]:
    """读 jsonl,每行一个 {query_id?, query, ...}。跳空行,坏行报行号。"""
    out: list[dict] = []
    with open(path, encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(
                    f"query 文件第 {line_number} 行不是合法 JSON: {exc}"
                )
            if not isinstance(obj, dict) or not str(obj.get("query") or "").strip():
                raise SystemExit(
                    f"query 文件第 {line_number} 行缺少 query 字段: {line[:120]}"
                )
            out.append(obj)
    return out


def make_sample_id(batch: str, seed: Mapping[str, object], seen: dict[str, int]) -> str:
    """优先用 query_id/qid;否则对内容做稳定哈希(与文件位置无关)。"""
    import hashlib

    batch = _identifier(batch)
    qid = seed.get("query_id") or seed.get("qid")
    if qid:
        base = f"{batch}_{_identifier(qid)}"
    else:
        digest = hashlib.sha1(
            json.dumps(seed, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:12]
        base = f"{batch}_{digest}"
    seen[base] = seen.get(base, 0) + 1
    return _identifier(base if seen[base] == 1 else f"{base}_{seen[base]}")


def _identifier(value: object) -> str:
    name = str(value)
    if not re.fullmatch(r"[\w-][\w.-]*", name) or len(name.encode("utf-8")) > 220 or name in {".", ".."}:
        raise ValueError(f"invalid batch/query identifier: {name!r}")
    return name


def _contained(root: Path, relative: str) -> Path:
    root = root.resolve()
    target = root / relative
    if not target.resolve().is_relative_to(root) or target.is_symlink():
        raise ValueError(f"batch path escapes its root: {target}")
    return target


def load_manifest(manifest_path: Path) -> tuple[dict[str, dict], dict[str, int]]:
    """按 attempt_no 聚合 start/end，同一次尝试不能被计算两次。

    未收尾的 ``running`` 只表示进程还在飞或被停掉时留下的开始标记，不占用
    max-attempts 次数。真正计数的是已关闭的 attempt。
    """
    return _aggregate_attempts(_manifest_records(manifest_path))


def _aggregate_attempts(records: Sequence[dict]) -> tuple[dict[str, dict], dict[str, int]]:
    latest: dict[str, dict] = {}
    attempts: dict[str, int] = {}
    for record in records:
        sid = str(record["sample_id"])
        number = int(record.get("attempt_no") or attempts.get(sid, 0) + 1)
        latest[sid] = record
        if str(record.get("status") or "") == "running":
            continue
        if number >= attempts.get(sid, 0):
            attempts[sid] = number
    return latest, attempts


def append_manifest(manifest_path: Path, record: Mapping[str, object]) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("a+b") as handle:
        _flock_exclusive(handle.fileno())
        # 只截掉进程中断留下的非完整尾行，已落盘的完整事件保持不可变。
        handle.seek(0, os.SEEK_END)
        end = handle.tell()
        while end:
            handle.seek(end - 1)
            if handle.read(1) == b"\n":
                break
            end -= 1
        handle.truncate(end)
        handle.write((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())


def _flock_exclusive(fd: int, *, attempts: int = 600, sleep_s: float = 0.05) -> None:
    """取排它 flock;在某些 FUSE/网络盘上并发 ``LOCK_EX`` 会返回 EAGAIN 而非阻塞,
    故用非阻塞 + 有界自旋重试兜住(afs 多 worker 并发写 manifest 的真实场景)。"""
    for _ in range(max(1, attempts)):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            time.sleep(sleep_s)
    # 最后一次直接阻塞式取(支持阻塞的盘会在此拿到;不支持的会再抛,交上层可见)。
    fcntl.flock(fd, fcntl.LOCK_EX)


def _manifest_records(manifest_path: Path) -> list[dict]:
    records: list[dict] = []
    if not manifest_path.is_file():
        return records
    with manifest_path.open(encoding="utf-8") as handle:
        for raw in handle:
            try:
                record = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(record, dict) and record.get("sample_id"):
                records.append(record)
    return records


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


# ---------------------------------------------------------------------------
# per-sample execution: reuse the single-deck path, classify the outcome
# ---------------------------------------------------------------------------


def _seed_language(seed: Mapping[str, object]) -> str:
    """controls.language(或顶层 language/lang) → RunSpec.from_inputs 的 language。"""
    controls = seed.get("controls")
    if isinstance(controls, Mapping):
        for key in ("language", "lang"):
            value = controls.get(key)
            if value:
                return str(value)
    for key in ("language", "lang"):
        value = seed.get(key)
        if value:
            return str(value)
    return "auto"


def _seed_page_count(seed: Mapping[str, object]) -> int | None:
    """统一 CLI/WebUI 页数字段；冲突的显式值不能静默覆盖。"""
    controls = seed.get("controls")
    counts = set()
    for source in (seed, controls if isinstance(controls, Mapping) else {}):
        for key in ("page_count_policy", "page_count", "pages_hint", "slide_count"):
            value = source.get(key)
            if isinstance(value, bool) or value is None:
                continue
            if str(value).strip().isdigit() and int(value) > 0:
                counts.add(int(value))
    if len(counts) > 1:
        raise ValueError(f"conflicting explicit page counts: {sorted(counts)}")
    return next(iter(counts), None)


def _seed_attachments(seed: Mapping[str, object]) -> tuple[list[str], list[str | None]]:
    """attachments 里的本地 path 列表与对齐的可选 intent(单 deck RunSpec 只吃本地文件)。"""
    raw = seed.get("attachments") or seed.get("materials") or []
    if isinstance(raw, (str, Mapping)):
        raw = [raw]
    paths: list[str] = []
    intents: list[str | None] = []
    for item in raw:
        if isinstance(item, Mapping):
            value = item.get("path")
            intent = item.get("intent")
        else:
            value = item
            intent = None
        if value:
            paths.append(str(value))
            intents.append(str(intent) if intent else None)
    return paths, intents


def _build_run_spec(
    sample: _Sample,
    args: BatchArgs,
    environment: Mapping[str, str],
) -> RunSpec:
    """把一行 query 编译成与单 deck 完全等价的 RunSpec。"""
    seed = sample.seed
    attachment_paths, attachment_intents = _seed_attachments(seed)
    return RunSpec.from_inputs(
        raw_user_query=str(seed.get("query") or ""),
        output_workspace=sample.run_dir,
        attachments=attachment_paths,
        attachment_intents=(
            attachment_intents if any(attachment_intents) else None
        ),
        run_mode=args.run_mode,
        authoring_profile=args.authoring_profile,
        page_count_hint=_seed_page_count(seed),
        revision_request=seed.get("revision_request"),
        language=_seed_language(seed),
        environment=environment,
    )


def _training_admission(outcome: object) -> str:
    """从 RunOutcome 的 training_manifest 读训练准入,不改运行终态。"""
    manifest = getattr(outcome, "training_manifest", None)
    if not isinstance(manifest, Mapping):
        return "not_evaluated"
    admission = manifest.get("training_admission")
    if admission in {"accepted", "rejected", "pending"}:
        return str(admission)
    if manifest.get("eligible_for_training") is True:
        return "accepted"
    if manifest.get("eligible_for_training") is False:
        return "rejected"
    return "not_evaluated"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_resume_inputs(spec: RunSpec) -> None:
    """批次续接沿用原输入，不把同 ID 的新 Query 或已覆盖附件写入旧现场。"""
    path = spec.output_workspace / "_state/run.json"
    if not path.is_file():
        return
    prior = json.loads(path.read_text(encoding="utf-8"))
    current = spec.to_manifest()
    for key in ("raw_user_query", "attachments", "attachment_intents", "run_mode",
                "authoring_profile", "page_count_hint", "revision_request", "language"):
        if prior.get(key) != current.get(key):
            raise ValueError(f"batch_resume_input_changed: {key}; original workspace preserved")
    host = spec.output_workspace / "_state/skill-runtime.json"
    if host.is_file():
        previous = json.loads(host.read_text(encoding="utf-8")).get("checkpoint_attachments")
        actual = []
        for index, attachment in enumerate(spec.attachments):
            digest = _file_sha256(attachment)
            actual.append({"sha256": digest, "original_name": attachment.name,
                           "intent": spec.attachment_intents[index] if spec.attachment_intents else ""})
        if previous is not None and previous != actual:
            raise ValueError("batch_resume_input_changed: attachment bytes or intent; original workspace preserved")


def run_one_attempt(
    sample: _Sample,
    args: BatchArgs,
    environment: Mapping[str, str],
    run_deck: RunDeck,
) -> dict:
    """执行一次单 deck 并把结果/异常分类成一个 manifest 记录。"""
    base: dict[str, object] = {
        "sample_id": sample.sample_id,
        "run_dir": str(sample.run_dir),
        "query": sample.seed.get("query"),
    }
    for key in (
        "query_id",
        "qid",
        "track",
        "subtrack",
        "domain",
        "style",
    ):
        if sample.seed.get(key) is not None:
            base[key] = sample.seed.get(key)
    try:
        spec = _build_run_spec(sample, args, environment)
        _validate_resume_inputs(spec)
    except (OSError, ValueError) as exc:
        return {**base, "status": "error", "retryable": False,
                "reason": "batch_input_invalid", "error": f"{type(exc).__name__}: {exc}"}
    try:
        outcome = run_deck(spec, environment)
    except AgentProviderError as exc:
        failure = exc.failure
        retryable = bool(failure and failure.retryable and failure.kind not in {
            "authentication", "access_disabled", "channel_pool_exhausted", "provider_protocol_error"})
        return {**base, "status": "error", "error": f"{type(exc).__name__}: {exc}",
                "error_kind": failure.kind if failure else "provider_error",
                "retryable": retryable, "retry_wait_s": exc.retry_after_s if retryable else 0}
    except InterruptedError as exc:
        detail = str(exc)
        compaction = detail.startswith(_RECOVERABLE_COMPACTION_PREFIX)
        owner_stall = detail.startswith(_RECOVERABLE_OWNER_STALL_PREFIXES)
        retryable = compaction or owner_stall
        return {**base, "status": "error", "error": f"{type(exc).__name__}: {exc}",
                "error_kind": type(exc).__name__, "retryable": retryable,
                "reason": ("context_compaction_recoverable" if compaction else
                           "owner_recovery_required" if owner_stall else "owner_interrupted"),
                "trace": traceback.format_exc()[-1500:]}
    except RepeatedToolFailureError as exc:
        saved = None
        if exc.error_code in FEEDBACK_ROUND_TOOL_ERRORS and (spec.output_workspace / "_state/workspace.json").is_file():
            try:
                saved = OwnerContextStore(spec.output_workspace).load_orchestrator()
            except (OSError, ValueError, KeyError, TypeError):
                saved = None
        return {**base, "status": "error", "error": f"{type(exc).__name__}: {exc}",
                "error_kind": type(exc).__name__, "retryable": saved is not None,
                "reason": "agent_recovery_required",
                "tool_recovery_policy": "same_workspace_tool_contract_once" if saved is not None else None,
                "recovery_target": {"tool": exc.tool_name, "error_code": exc.error_code},
                "trace": traceback.format_exc()[-1500:]}
    except AgentRuntimeError as exc:
        return {**base, "status": "error", "error": f"{type(exc).__name__}: {exc}",
                "error_kind": type(exc).__name__, "retryable": False,
                "reason": "agent_recovery_required", "trace": traceback.format_exc()[-1500:]}
    except TrainingExportError as exc:
        # 已生成的 raw 不因 QC 超时重跑；没有导出 manifest 才是运行/导出故障。
        if exc.manifest is None:
            return {**base, "status": "error", "reason": "training_export_failed", "error": str(exc)}
        manifest = exc.manifest
        admission = str(manifest.get("training_admission") or "not_evaluated")
    except Exception as exc:  # noqa: BLE001
        base.update(
            {
                "status": "error",
                "retryable": False,
                "error": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc()[-1500:],
            }
        )
        return base
    else:
        admission = _training_admission(outcome)
        manifest = getattr(outcome, "training_manifest", None)
    base.update(
        {
            "status": "completed",
            "training_admission": admission,
            "training_export": {
                "eligible_for_training": admission == "accepted",
                "quality_rejection_reasons": list(
                    (manifest or {}).get("rejection_reasons") or []
                )
                if isinstance(manifest, Mapping) and admission == "rejected"
                else [],
                "pending_reasons": list((manifest or {}).get("rejection_reasons") or [])
                if isinstance(manifest, Mapping) and admission == "pending" else [],
            },
        }
    )
    return base


def supervised_sample(
    sample: _Sample,
    args: BatchArgs,
    environment: Mapping[str, str],
    run_deck: RunDeck,
    *,
    attempts_used: int = 0,
    record_attempt: Callable[[dict], None] = lambda _record: None,
    can_retry: Callable[[], bool] = lambda: True,
) -> list[dict]:
    """把一个样本跑到终态,受 max-attempts 约束。返回本次命令内的所有 attempt 记录。

    运维/瞬态失败在原工作区续接，复用正式单册的 checkpoint/Owner 恢复入口。
    不归档或清空部分成果；无法安全恢复时保留现场并报告，不降级为整册重做。
    """
    records: list[dict] = []
    used = max(0, int(attempts_used))
    max_attempts = max(1, int(args.max_attempts))
    while used < max_attempts:
        if records and not can_retry():
            break
        attempt_no = used + 1
        started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        started = time.monotonic()
        identity = {"sample_id": sample.sample_id, "attempt_no": attempt_no,
                    "attempt_id": f"{sample.sample_id}:attempt-{attempt_no}",
                    "run_dir": str(sample.run_dir), "started_at": started_at,
                    "execution": "resume" if (sample.run_dir / "_state/workspace.json").is_file() else "fresh"}
        record_attempt({**identity, "status": "running"})
        record = run_one_attempt(sample, args, environment, run_deck)
        status = str(record.get("status") or "error")
        retryable = status not in TERMINAL and bool(record.get("retryable", False))
        if record.get("tool_recovery_policy") == "same_workspace_tool_contract_once":
            retryable = retryable and attempt_no == 1
        record.update(identity)
        record["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        record["elapsed_s"] = round(time.monotonic() - started, 3)
        record["retryable"] = retryable
        record["retry_scheduled"] = bool(retryable and attempt_no < max_attempts and can_retry())
        used = attempt_no
        records.append(record)
        record_attempt(record)
        if not record["retry_scheduled"]:
            break
        # 模型请求与资源槽均已释放；冷却不消耗模型轮次，软停止可立即保留 checkpoint。
        retry_at = time.monotonic() + max(0.0, float(record.get("retry_wait_s") or 0))
        while can_retry() and (remaining := retry_at - time.monotonic()) > 0:
            time.sleep(min(1.0, remaining))
    return records


# ---------------------------------------------------------------------------
# 动态并发调度:运行中可增减 workers,不打断在飞 deck
# ---------------------------------------------------------------------------


def _workers_control_path(args: "BatchArgs", manifest_path: Path) -> Path:
    """目标并发数的控制文件:``logs/<batch>.workers``。

    运行中 ``echo N > <此文件>`` 即可增减并发,已在飞的 deck 不受影响;调度循环
    每轮重读它决定是否再放新 deck 进来。文件不存在/内容非法则回退到 --workers。
    """
    return manifest_path.with_name(f"{args.batch}.workers")


def _resolve_target_workers(args: "BatchArgs", manifest_path: Path) -> int:
    """读当前目标并发:控制文件优先,回退 --workers。

    返回值 >=1 为正常并发;**0 是软刹车**:不再派新 deck,只等在飞的自然跑完
    (用于「只跑当前这批、跑完就停、不打断在飞」)。负数按 0 处理。
    """
    default = max(1, int(args.workers))
    path = _workers_control_path(args, manifest_path)
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return default
    if not raw:
        return default
    try:
        value = int(raw.split()[0])
    except (ValueError, IndexError):
        return default
    return max(0, value)


def _write_failure_notice(message: str, exc: BaseException) -> None:
    # Reporting must not prevent terminal-state publication when the log disk is full.
    try:
        print(message, file=sys.stderr, flush=True)
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
    except (OSError, ValueError):
        pass


def _provider_pool_quarantined_check(environment: Mapping[str, str]) -> Callable[[], bool]:
    """Use session health identities, not a single request's local exclusions."""
    from dataclasses import replace
    from runtime.config import ProviderSettings, load_runtime_config
    from runtime.provider_health import ProviderHealthStore
    from runtime.session import provider_channels

    try:
        channels = provider_channels(environment)
    except KeyError as exc:
        # Injected run_deck callbacks may intentionally have no provider credentials.
        if exc.args != ("MODEL_API_KEY",):
            raise
        return lambda: False
    settings = ProviderSettings.from_environment(environment)
    health = ProviderHealthStore(load_runtime_config(environment)["resources"]["directory"])
    identities = tuple(dict.fromkeys(
        health.identity(replace(settings, model=channel.model) if channel.model else settings,
                        channel.key)
        for channel in channels
    ))
    return lambda: bool(identities) and all(
        health.status(identity).quarantined for identity in identities
    )


def _run_dynamic_pool(
    samples: Sequence[tuple["_Sample", int]],
    args: "BatchArgs",
    env: Mapping[str, str],
    run_deck: RunDeck,
    record_attempt: Callable[[dict], None],
    publish_worker_exit: Callable[[dict | None], None],
    manifest_path: Path,
    *,
    initial_workers: int,
) -> None:
    """手动调度:维持在飞 deck 数 ≤ 目标 workers,目标每轮从控制文件重读。

    与固定 ``ThreadPoolExecutor(max_workers=N)`` 的区别:目标并发可运行中增减。
    调小 → 不再放新 deck、等在飞的自然收敛到新上限(绝不打断在飞);调大 → 立即
    补放新 deck。每个 deck 仍是一个线程,跑完即释放槽。
    """
    import threading

    # 写一份控制文件初值,方便 Master 直接改(不存在才写,resume 保留既有值)。
    control = _workers_control_path(args, manifest_path)
    try:
        if not control.exists():
            control.write_text(f"{initial_workers}\n", encoding="utf-8")
    except OSError:
        pass

    pending = list(samples)
    index = 0
    inflight: set[threading.Thread] = set()
    errors: list[BaseException] = []
    recording_errors: list[BaseException] = []
    lock = threading.Lock()
    pool_quarantined = _provider_pool_quarantined_check(env)
    provider_hold = threading.Event()

    def _provider_dispatch_available() -> bool:
        if provider_hold.is_set():
            return False
        if pool_quarantined():
            provider_hold.set()
            return False
        return True

    def _report_worker_exit(sample_id: str, reason: str, exc: BaseException) -> None:
        _write_failure_notice(
            f"[batch] deck worker exited sample={sample_id} reason={reason}: "
            f"{type(exc).__name__}: {exc}",
            exc,
        )

    def _exit_reason(exc: BaseException) -> str:
        if isinstance(exc, SystemExit):
            return "worker_system_exit"
        if isinstance(exc, (KeyboardInterrupt, GeneratorExit)):
            return "worker_cancelled"
        return "unexpected_worker_exit"

    def _worker(sample: "_Sample", prior: int) -> None:
        try:
            supervised_sample(
                sample, args, env, run_deck,
                attempts_used=prior, record_attempt=record_attempt,
                can_retry=lambda: (_resolve_target_workers(args, manifest_path) > 0
                                   and _provider_dispatch_available()),
            )
        except BaseException as exc:  # noqa: BLE001 - worker 边界必须关闭 running attempt
            with lock:
                errors.append(exc)
            reason = _exit_reason(exc)
            _report_worker_exit(sample.sample_id, reason, exc)
            try:
                latest, attempt_counts = load_manifest(manifest_path)
                current = latest.get(sample.sample_id, {})
                current_status = str(current.get("status") or "")
                closed_no = int(attempt_counts.get(sample.sample_id, 0) or 0)
                open_no = int(current.get("attempt_no") or 0) if current_status == "running" else 0
                current_no = max(closed_no, open_no)
                attempt_no = max(prior + 1, current_no)
                record: dict | None

                # A callback can fail after its append reached disk. Never replace a closed
                # attempt, especially completed work, with a synthetic worker error.
                if current_no >= prior + 1 and current_status in TERMINAL:
                    record = None
                else:
                    disk_result = _completed_result_on_disk(sample.run_dir)
                    if disk_result is not None:
                        record = _completed_record_from_result(sample, attempt_no, disk_result)
                    elif current_no >= prior + 1 and current_status not in {"", "running"}:
                        record = None
                    else:
                        record = {
                            "sample_id": sample.sample_id,
                            "attempt_no": attempt_no,
                            "attempt_id": f"{sample.sample_id}:attempt-{attempt_no}",
                            "run_dir": str(sample.run_dir),
                            "status": "error",
                            "reason": reason,
                            "error_kind": type(exc).__name__,
                            "error": f"{type(exc).__name__}: {exc}",
                            "finished_at": time.strftime(
                                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                            ),
                            "retryable": False,
                            "retry_scheduled": False,
                            "trace": "".join(
                                traceback.format_exception(type(exc), exc, exc.__traceback__)
                            )[-1500:],
                        }
                        if current_no == attempt_no:
                            for key in ("started_at", "execution"):
                                if current.get(key) is not None:
                                    record[key] = current[key]
                        if isinstance(exc, SystemExit):
                            record["exit_code"] = repr(exc.code)
                publish_worker_exit(record)
            except BaseException as recording_exc:  # noqa: BLE001
                _write_failure_notice(
                    f"[batch] FAILED to durably publish worker exit sample={sample.sample_id}: "
                    f"{type(recording_exc).__name__}: {recording_exc}",
                    recording_exc,
                )
                with lock:
                    recording_errors.append(recording_exc)

    last_target = -1
    provider_notice = False
    while index < len(pending) or inflight:
        # 清理已完成线程,释放槽。
        done = {t for t in inflight if not t.is_alive()}
        for t in done:
            t.join()
        inflight -= done

        target = (_resolve_target_workers(args, manifest_path)
                  if _provider_dispatch_available() else 0)
        if target != last_target:
            print(f"[batch] 目标并发 workers={target}(在飞={len(inflight)})", flush=True)
            last_target = target

        # 在不超过目标的前提下补放新 deck。
        while index < len(pending) and len(inflight) < target:
            if not _provider_dispatch_available():
                target = 0
                break
            sample, prior = pending[index]
            index += 1
            thread = threading.Thread(
                target=_worker, args=(sample, prior), daemon=False,
                name=f"deck-{sample.sample_id}",
            )
            thread.start()
            inflight.add(thread)

        if provider_hold.is_set() and not provider_notice:
            print(
                "[batch] All configured provider channels are shared-quarantined; "
                "new dispatch and retries paused, in-flight work preserved.",
                flush=True,
            )
            provider_notice = True

        if index >= len(pending) and not inflight:
            break
        # 软刹车:target=0 且在飞已清空 → 停止派发,提前收尾(剩余 pending 留给
        # 下次 --resume 续跑,已完成的 manifest 会跳过)。
        if target <= 0 and not inflight:
            remaining = len(pending) - index
            print(
                f"[batch] 软刹车生效(workers=0):已停止派发,剩余 {remaining} 个待 "
                f"--resume 续跑。",
                flush=True,
            )
            break
        # 轻量轮询:既响应控制文件变更,也回收完成线程。
        time.sleep(1.0)

    if errors:
        # Include completions that landed after the first worker exit before preserving the
        # original control-flow exception in the coordinator thread.
        if not recording_errors:
            try:
                publish_worker_exit(None)
            except BaseException as recording_exc:  # noqa: BLE001
                _write_failure_notice(
                    f"[batch] FAILED to refresh summary after worker exit: "
                    f"{type(recording_exc).__name__}: {recording_exc}",
                    recording_exc,
                )
                recording_errors.append(recording_exc)
        if recording_errors:
            raise recording_errors[0]
        raise errors[0]


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------


def build_batch_summary(
    *,
    batch: str,
    expected_sample_ids: Sequence[str],
    manifest_path: Path,
    max_attempts: int,
    additional_records: Sequence[Mapping[str, object]] = (),
) -> dict:
    """把 append-only manifest 聚合成 run.sh show_status 消费的严格 summary。"""
    records = _manifest_records(manifest_path)
    records.extend(dict(record) for record in additional_records)
    expected = list(dict.fromkeys(expected_sample_ids))
    expected_set = set(expected)
    records = [r for r in records if str(r.get("sample_id") or "") in expected_set]

    latest, attempt_counts = _aggregate_attempts(records)

    status_counts: dict[str, int] = {}
    pending: list[str] = []
    operational: list[str] = []
    exhausted: list[str] = []
    for sid in expected:
        record = latest.get(sid)
        if record is None:
            pending.append(sid)
            continue
        status = str(record.get("status") or "error")
        status_counts[status] = status_counts.get(status, 0) + 1
        if status not in TERMINAL:
            operational.append(sid)
            if attempt_counts.get(sid, 0) >= max_attempts:
                exhausted.append(sid)

    terminal_count = sum(
        1
        for sid in expected
        if str((latest.get(sid) or {}).get("status") or "") in TERMINAL
    )
    success_count = sum(
        1
        for sid in expected
        if str((latest.get(sid) or {}).get("status") or "") in SUCCESS_STATUSES
    )

    training_admission_counts = {"accepted": 0, "rejected": 0, "pending": 0, "not_evaluated": 0}
    training_rejection_reasons: dict[str, int] = {}
    for sid in expected:
        record = latest.get(sid) or {}
        if str(record.get("status") or "") not in SUCCESS_STATUSES:
            continue
        admission = str(record.get("training_admission") or "not_evaluated")
        if admission not in training_admission_counts:
            admission = "not_evaluated"
        training_admission_counts[admission] += 1
        if admission != "rejected":
            continue
        export = record.get("training_export") or {}
        for reason in export.get("quality_rejection_reasons") or []:
            key = str(reason or "unspecified").strip() or "unspecified"
            training_rejection_reasons[key] = training_rejection_reasons.get(key, 0) + 1

    training_accepted = training_admission_counts["accepted"]
    quality_excluded = training_admission_counts["rejected"]
    # ready = 每个期望样本都到达终态(成功或明确排除)。
    ready = not pending and not operational and terminal_count == len(expected)
    return {
        "schema": BATCH_SUMMARY_SCHEMA,
        "batch": batch,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ready": ready,
        "expected_samples": len(expected),
        "terminal_samples": terminal_count,
        "successful_samples": success_count,
        "content_excluded_samples": terminal_count - success_count,
        "training_accepted_samples": training_accepted,
        "quality_excluded_samples": quality_excluded,
        "training_pending_samples": training_admission_counts["pending"],
        "training_not_evaluated_samples": training_admission_counts["not_evaluated"],
        "training_admission_counts": training_admission_counts,
        "training_rejection_reasons": dict(sorted(training_rejection_reasons.items())),
        "status_counts": status_counts,
        "attempt_records": sum(attempt_counts.values()),
        "retried_samples": sum(1 for sid in expected if attempt_counts.get(sid, 0) > 1),
        "operational_failure_samples": operational,
        "exhausted_samples": exhausted,
        "pending_samples": pending,
        "manifest": str(manifest_path.resolve()),
    }


# ---------------------------------------------------------------------------
# resume detection
# ---------------------------------------------------------------------------


def _completed_result_on_disk(run_dir: Path) -> dict | None:
    """工作区已有终态 result.json(status=completed)时视为已完成,resume 跳过。"""
    result = run_dir / "result.json"
    if not result.is_file():
        return None
    try:
        value = json.loads(result.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) and value.get("status") == "completed" else None


def _completed_record_from_result(
    sample: _Sample,
    attempt_no: int,
    result: Mapping[str, object],
) -> dict:
    """把已落盘的 completed result 对账为 manifest 终态，不重新执行 deck。"""
    training = result.get("training_manifest") or {}
    if not isinstance(training, Mapping):
        training = {
            "training_admission": result.get("training_admission", "not_evaluated"),
            "rejection_reasons": result.get("training_rejection_reasons", []),
        }
    admission = training.get("training_admission", "not_evaluated")
    reasons = training.get("rejection_reasons", [])
    return {
        "sample_id": sample.sample_id,
        "attempt_no": max(1, int(attempt_no)),
        "attempt_id": f"{sample.sample_id}:attempt-{max(1, int(attempt_no))}",
        "run_dir": str(sample.run_dir),
        "status": "completed",
        "reconciled_from": "result.json",
        "reconciled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "training_admission": admission,
        "training_export": {
            "quality_rejection_reasons": reasons if admission == "rejected" else [],
            "pending_reasons": reasons if admission == "pending" else [],
        },
    }


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------


def run_batch(
    args: BatchArgs,
    *,
    run_deck: RunDeck,
    environment: Mapping[str, str] | None = None,
    paths: BatchPaths | None = None,
) -> int:
    env = dict(environment if environment is not None else os.environ)
    batch_paths = paths or BatchPaths.from_environment(env)
    lock_path = _contained(batch_paths.logs_dir, f"{_identifier(args.batch)}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("batch is already running") from exc
        return _run_batch_locked(args, run_deck=run_deck, environment=env, paths=batch_paths)


def _run_batch_locked(
    args: BatchArgs,
    *,
    run_deck: RunDeck,
    environment: Mapping[str, str] | None = None,
    paths: BatchPaths | None = None,
) -> int:
    """批量入口:读 jsonl → 逐样本(并发)复用单 deck 路径 → 写 summary。返回退出码。"""
    env = dict(environment if environment is not None else os.environ)
    batch_paths = paths or BatchPaths.from_environment(env)
    _identifier(args.batch)
    batch_dir = _contained(batch_paths.runs_dir, args.batch)
    manifest_path = _contained(batch_paths.logs_dir, f"{args.batch}.manifest.jsonl")
    summary_path = _contained(batch_paths.logs_dir, f"{args.batch}.summary.json")
    if manifest_path.exists() and manifest_path.stat().st_size and not args.resume:
        raise ValueError("batch already has a manifest; use --resume or a new batch identifier")
    batch_dir.mkdir(parents=True, exist_ok=True)
    batch_paths.logs_dir.mkdir(parents=True, exist_ok=True)

    queries = load_queries(args.queries)
    if args.limit and args.limit > 0:
        queries = queries[: args.limit]

    latest, attempt_history = ({}, {})
    if args.resume:
        latest, attempt_history = load_manifest(manifest_path)

    seen: dict[str, int] = {}
    expected_sample_ids: list[str] = []
    samples: list[tuple[_Sample, int]] = []
    skipped = 0
    exhausted = 0
    for seed in queries:
        sid = make_sample_id(args.batch, seed, seen)
        if sid in expected_sample_ids:
            raise ValueError(f"query identifiers produce the same sample workspace: {sid}")
        expected_sample_ids.append(sid)
        run_dir = _contained(batch_dir, sid)
        prev = latest.get(sid, {}) if args.resume else {}
        prior_attempts = int(attempt_history.get(sid, 0) or 0)
        # 已达终态(manifest 或磁盘 result.json)→ 跳过。
        if args.resume and str(prev.get("status") or "") in TERMINAL:
            skipped += 1
            continue
        disk_result = _completed_result_on_disk(run_dir) if args.resume else None
        if disk_result is not None:
            number = max(1, prior_attempts)
            append_manifest(
                manifest_path,
                _completed_record_from_result(_Sample(sid, seed, run_dir), number, disk_result),
            )
            skipped += 1
            continue
        if args.resume and prior_attempts >= args.max_attempts:
            exhausted += 1
            continue
        samples.append((_Sample(sid, seed, run_dir), prior_attempts))

    print(
        f"batch={args.batch} queries={len(queries)} 待跑={len(samples)} "
        f"已完成跳过={skipped} 达上限={exhausted} workers={args.workers}",
        flush=True,
    )

    import threading

    event_lock = threading.Lock()
    worker_exit_records: dict[tuple[str, int], dict] = {}

    def record_attempt(record: dict) -> None:
        with event_lock:
            append_manifest(manifest_path, record)

    def publish_worker_exit(record: dict | None) -> None:
        """Best-effort publish both truths; report either write failure to the caller."""
        with event_lock:
            manifest_error: BaseException | None = None
            if record is not None:
                key = (str(record["sample_id"]), int(record["attempt_no"]))
                worker_exit_records[key] = record
                try:
                    append_manifest(manifest_path, record)
                except BaseException as exc:  # noqa: BLE001 - still attempt summary
                    manifest_error = exc
            summary_error: BaseException | None = None
            try:
                summary = build_batch_summary(
                    batch=args.batch,
                    expected_sample_ids=expected_sample_ids,
                    manifest_path=manifest_path,
                    max_attempts=args.max_attempts,
                    additional_records=tuple(worker_exit_records.values()),
                )
                _atomic_json(summary_path, summary)
            except BaseException as exc:  # noqa: BLE001
                summary_error = exc
            if manifest_error is not None:
                if summary_error is not None:
                    _write_failure_notice(
                        f"[batch] FAILED to write worker-exit summary: "
                        f"{type(summary_error).__name__}: {summary_error}",
                        summary_error,
                    )
                raise manifest_error
            if summary_error is not None:
                raise summary_error

    if samples:
        workers = max(1, int(args.workers))
        _run_dynamic_pool(
            samples, args, env, run_deck, record_attempt, publish_worker_exit,
            manifest_path,
            initial_workers=workers,
        )

    summary = build_batch_summary(
        batch=args.batch,
        expected_sample_ids=expected_sample_ids,
        manifest_path=manifest_path,
        max_attempts=args.max_attempts,
    )
    _atomic_json(summary_path, summary)
    print(
        f"manifest: {manifest_path}\nsummary: {summary_path}\n"
        f"terminal={summary['terminal_samples']}/{summary['expected_samples']} "
        f"success={summary['successful_samples']} ready={summary['ready']}",
        flush=True,
    )
    return 0 if summary["ready"] else 1
