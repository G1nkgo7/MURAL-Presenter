"""FEAT-2.5：薄批量合成入口的单元/集成测试。

批量层复用单 deck 执行路径,不复制 agent loop。这里既用可控替身 ``run_deck`` 覆盖
读 jsonl / resume / 重试 / summary 分类,也用真实单 deck kernel(mock backend/renderer/
analyzer,不打真模型)做一次端到端,证明批量确实走的是 ``run_presenter``。
"""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from PIL import Image

from export.training import TrainingExportError
from infer import RuntimeServices, run_presenter
from runtime.batch import (
    BatchArgs,
    BatchPaths,
    build_batch_summary,
    load_queries,
    load_manifest,
    make_sample_id,
    run_batch,
    _seed_page_count,
)
from runtime.config import RunMode, RunSpec
from runtime.agent import (
    AgentContextWindowError, AgentInterruptedError, AgentProtocolError,
    AgentProviderError, AgentTurnLimitError, COMPLETION_GAP_STALL_MESSAGE,
    OWNER_FAILURE_STALL_MESSAGE, RepeatedToolFailureError,
)
from runtime.recovery import ProviderFailure


_SUMMARY_REQUIRED_FIELDS = {
    "batch",
    "ready",
    "expected_samples",
    "terminal_samples",
    "successful_samples",
    "content_excluded_samples",
    "attempt_records",
    "training_accepted_samples",
    "quality_excluded_samples",
    "training_not_evaluated_samples",
    "status_counts",
    "training_rejection_reasons",
    "operational_failure_samples",
}


def _write_queries(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("owners,provider,expected", [(4, 64, 4), (12, 2, 2)])
def test_1k_profile_critic_workers_follow_existing_runtime_limits(tmp_path, monkeypatch, owners, provider, expected):
    import os
    import runpy
    import sys
    from types import SimpleNamespace
    from runtime import config
    from runtime.production import ProductionSettings

    limits = config.load_runtime_config(os.environ)
    limits["decks"]["owners"] = owners
    limits["resources"]["provider"] = provider
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(limits))
    monkeypatch.setenv("MURAL_RUNTIME_CONFIG", str(path))
    monkeypatch.setattr(config, "read_environment_file", lambda *_: {})
    selected = {"MODEL": "claude-opus-4-7-thinking", "MODEL_API_KEY": "unit-test-key",
                "CRITIC_CONCURRENCY": "1", "MURAL_RUNTIME_CONFIG": str(path)}
    engine = SimpleNamespace(MODELS={"opus-4-7": {}}, selection_env=lambda *_: selected,
        render_env=lambda env: env, external_visual_critic_env=lambda *_: {"ENABLE_EXTERNAL_VISUAL_CRITIC": "0"})
    monkeypatch.setitem(sys.modules, "app", SimpleNamespace(engine=engine,
        service_config=SimpleNamespace(system_runtime_env=lambda: {})))
    profile = runpy.run_path(str(Path(__file__).resolve().parents[3] /
        "configs/data_pipeline/opus47thinking-9b-1k/batch.py"))
    environment = profile["teacher_environment"]()
    assert environment["CRITIC_CONCURRENCY"] == str(expected)
    settings = ProductionSettings.from_environment(environment)
    assert settings.critic_concurrency == expected
    assert settings.model_concurrency == provider
    assert environment["ENABLE_EXTERNAL_VISUAL_CRITIC"] == "0"


@pytest.mark.parametrize("retry_limit", [None, 5])
@pytest.mark.parametrize("explicit_name", [False, True])
def test_1k_canary_rerun_keeps_inputs_and_uses_new_output(tmp_path, monkeypatch, retry_limit, explicit_name):
    import os
    import runpy
    import sys
    import subprocess
    from runtime import config, release_guard

    profile = runpy.run_path(str(Path(__file__).resolve().parents[3] /
        "configs/data_pipeline/opus47thinking-9b-1k/batch.py"))
    namespace = profile["main"].__globals__
    original = tmp_path / "original"
    original.joinpath("inputs").mkdir(parents=True)
    rows = [{"query_id": key, "query": f"Original {key}", "attachments": [{"path": "/input.pdf"}]}
            for key in profile["CANARY_IDS"]]
    source = original / "inputs/queries.jsonl"
    _write_queries(source, rows)
    before = source.read_bytes()
    namespace.update(OUTPUT=original, prepare=lambda: None,
                     teacher_environment=lambda: {"MODEL": "claude-opus-4-7-thinking"})
    monkeypatch.setattr(release_guard, "verify_runtime_release", lambda *_a, **_k: {"dirty_override": False, "policy": "prod"})
    monkeypatch.setattr(config, "load_runtime_config", lambda *_a: {"mineru": {"endpoints": []}})
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: None)
    captured = {}

    def execute(python, command, environment):
        captured.update(command=command, environment=environment)
        raise RuntimeError("exec intercepted")

    monkeypatch.setattr(os, "execve", execute)
    argv = ["batch.py", "run", "--canary", "--workers", "3"]
    expected_batch = f"{profile['BATCH']}-canary-{profile['RELEASE'].stem.rsplit('-', 1)[-1]}"
    if explicit_name:
        argv.extend(["--batch-name", "new-canary"])
        expected_batch = "new-canary"
    if retry_limit is not None:
        argv.extend(["--max-attempts", str(retry_limit)])
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(RuntimeError, match="exec intercepted"):
        profile["main"]()
    output = tmp_path / expected_batch
    assert profile["jsonl"](output / "inputs/canary.jsonl") == rows
    assert source.read_bytes() == before
    assert not (original / "runs").exists()
    assert captured["environment"]["CLEAN_RUNS_DIR"] == str(output / "runs")
    assert captured["environment"]["MODEL"] == "claude-opus-4-7-thinking"
    assert captured["environment"]["MODEL_THINKING"] == "1"
    assert captured["environment"]["MODEL_REASONING_EFFORT"] == "xhigh"
    assert captured["environment"]["MURAL_RELEASE_MANIFEST"] == str(profile["RELEASE"])
    assert captured["environment"]["MODEL_TIMEOUT_S"] == "0"
    from runtime.production import ProductionSettings
    from runtime.config import ProviderSettings
    from runtime.session import ModelRequest

    settings = ProductionSettings.from_environment(captured["environment"])
    provider = ProviderSettings.from_environment({**captured["environment"], "MODEL_API_FORMAT": "anthropic"})
    body = ModelRequest(model=provider.model, system="test", messages=(),
        max_tokens=settings.max_tokens, thinking=settings.thinking,
        effort=settings.reasoning_effort).anthropic_kwargs(provider)
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert body["output_config"] == {"effort": "xhigh"}
    command = captured["command"]
    assert command[command.index("--workers") + 1] == "3"
    assert command[command.index("--max-attempts") + 1] == str(retry_limit or 3)
    assert command[command.index("--queries") + 1] == str(output / "inputs/canary.jsonl")


@pytest.mark.parametrize("canary,canonical,existing_input", [
    (True, True, None), (True, False, "queries.jsonl"), (False, False, "canary.jsonl")])
def test_canary_and_production_batches_cannot_be_mixed(tmp_path, monkeypatch, capsys, canary, canonical, existing_input):
    import runpy
    import sys
    profile = runpy.run_path(str(Path(__file__).resolve().parents[3] /
        "configs/data_pipeline/opus47thinking-9b-1k/batch.py"))
    production = tmp_path / profile["BATCH"]
    batch = production.name if canonical else "prior-batch"
    output = tmp_path / batch
    if existing_input:
        output.joinpath("inputs").mkdir(parents=True)
        output.joinpath("inputs", existing_input).write_text("original immutable input\n")
    profile["main"].__globals__.update(OUTPUT=production,
        prepare=lambda: pytest.fail("reject mixed batch before preparing or starting any deck"))
    argv = ["batch.py", "run", "--batch-name", batch]
    if canary:
        argv.append("--canary")
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exc:
        profile["main"]()
    assert exc.value.code == 2
    assert "separate" in capsys.readouterr().err
    if existing_input:
        assert output.joinpath("inputs", existing_input).read_text() == "original immutable input\n"
    assert not output.joinpath("runs").exists()


def _args(queries: Path, **kw) -> BatchArgs:
    defaults = dict(
        queries=queries,
        batch="unit",
        workers=1,
        max_attempts=3,
        resume=False,
        limit=0,
        run_mode=RunMode.SYNTHESIS,
    )
    defaults.update(kw)
    return BatchArgs(**defaults)


def _paths(tmp_path: Path) -> BatchPaths:
    return BatchPaths(runs_dir=tmp_path / "runs", logs_dir=tmp_path / "logs")


class _Outcome:
    """RunOutcome-like 替身,只暴露批量层读取的 training_manifest。"""

    def __init__(self, admission: str, reasons: list[str] | None = None) -> None:
        if admission == "not_applicable":
            self.training_manifest = None
        else:
            self.training_manifest = {
                "training_admission": admission,
                "eligible_for_training": admission == "accepted",
                "rejection_reasons": list(reasons or []),
            }


# ---------------------------------------------------------------------------
# jsonl + schema mapping
# ---------------------------------------------------------------------------


def test_load_queries_rejects_missing_query(tmp_path: Path) -> None:
    path = tmp_path / "q.jsonl"
    path.write_text('{"query_id": "a"}\n', encoding="utf-8")
    try:
        load_queries(path)
    except SystemExit as exc:
        assert "缺少 query" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected SystemExit for missing query")


def test_sample_id_prefers_query_id(tmp_path: Path) -> None:
    seen: dict[str, int] = {}
    assert make_sample_id("b", {"query_id": "simple_persona_0007", "query": "x"}, seen) == (
        "b_simple_persona_0007"
    )
    # 无 query_id 时对内容稳定哈希,与位置无关。
    first = make_sample_id("b", {"query": "same"}, {})
    second = make_sample_id("b", {"query": "same"}, {})
    assert first == second


@pytest.mark.parametrize("unsafe", ["../escape", "x/../../../escaped", "x\\escape", ".", "..", "x\nline"])
def test_batch_identifiers_reject_paths(unsafe: str) -> None:
    with pytest.raises(ValueError, match="identifier"):
        make_sample_id("b", {"query_id": unsafe}, {})
    with pytest.raises(ValueError, match="identifier"):
        make_sample_id(unsafe, {"query_id": "safe"}, {})


def test_slide_count_mapping_and_conflicts() -> None:
    assert _seed_page_count({"slide_count": 7}) == 7
    assert _seed_page_count({"controls": {"slide_count": "3"}}) == 3
    assert _seed_page_count({"slide_count": 3, "pages_hint": 3}) == 3
    with pytest.raises(ValueError, match="conflicting"):
        _seed_page_count({"slide_count": 3, "controls": {"page_count": 4}})


def test_query_suffix_collision_never_starts_a_shared_workspace(tmp_path):
    path = tmp_path / "queries.jsonl"
    _write_queries(path, [{"query_id": qid, "query":"test"} for qid in ("a", "a", "a_2")])
    calls = []
    with pytest.raises(ValueError, match="same sample workspace"):
        run_batch(_args(path), paths=_paths(tmp_path), environment={}, run_deck=lambda *args: calls.append(args))
    assert not calls


def test_attempt_is_durable_before_retry_and_interruption(tmp_path: Path) -> None:
    queries = tmp_path / "q.jsonl"
    _write_queries(queries, [{"query_id": "a", "query": "一页"}])
    paths = _paths(tmp_path)
    manifest = paths.logs_dir / "unit.manifest.jsonl"
    calls = []

    def interrupted(_spec, _env):
        calls.append(1)
        latest, counts = load_manifest(manifest)
        assert latest["unit_a"]["status"] == "running"
        assert counts.get("unit_a", 0) == len(calls) - 1
        if len(calls) == 1:
            raise AgentProviderError("first failed", failure=ProviderFailure("timeout", "temporary", retryable=True))
        rows = [json.loads(line) for line in manifest.read_text().splitlines()]
        assert any(row.get("attempt_no") == 1 and row["status"] == "error" for row in rows)
        raise KeyboardInterrupt("second interrupted")

    with pytest.raises(KeyboardInterrupt):
        run_batch(_args(queries), run_deck=interrupted, environment={}, paths=paths)
    latest, counts = load_manifest(manifest)
    assert counts == {"unit_a": 2}
    assert latest["unit_a"]["reason"] == "worker_cancelled"
    assert latest["unit_a"]["retryable"] is False
    summary = json.loads((paths.logs_dir / "unit.summary.json").read_text())
    assert summary["operational_failure_samples"] == ["unit_a"]
    run_batch(_args(queries, resume=True), run_deck=lambda *_: _Outcome("accepted"), environment={}, paths=paths)
    latest, counts = load_manifest(manifest)
    assert counts == {"unit_a": 3}
    assert latest["unit_a"]["status"] == "completed"


def test_unexpected_worker_exit_is_published_while_unrelated_deck_runs(tmp_path: Path) -> None:
    import threading
    import time

    class FatalWorkerExit(BaseException):
        pass

    queries = tmp_path / "q.jsonl"
    _write_queries(queries, [
        {"query_id": "failed", "query": "one page"},
        {"query_id": "tail", "query": "one page"},
    ])
    paths = _paths(tmp_path)
    tail_started = threading.Event()
    release_tail = threading.Event()
    calls: list[str] = []
    raised: list[BaseException] = []

    def run_deck(spec, _environment):
        sid = Path(spec.output_workspace).name
        calls.append(sid)
        if sid.endswith("_failed"):
            raise FatalWorkerExit("worker boundary failed")
        tail_started.set()
        assert release_tail.wait(5)
        return _Outcome("accepted")

    def coordinate() -> None:
        try:
            run_batch(_args(queries, workers=2), paths=paths, environment={}, run_deck=run_deck)
        except BaseException as exc:  # noqa: BLE001 - assert coordinator propagation
            raised.append(exc)

    coordinator = threading.Thread(target=coordinate)
    coordinator.start()
    assert tail_started.wait(5)
    summary_path = paths.logs_dir / "unit.summary.json"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if summary_path.is_file():
            summary = json.loads(summary_path.read_text())
            if summary["status_counts"].get("error") == 1:
                break
        time.sleep(0.01)
    else:
        pytest.fail("worker exit was not published while the unrelated deck was running")
    assert coordinator.is_alive()
    assert summary["operational_failure_samples"] == ["unit_failed", "unit_tail"]

    release_tail.set()
    coordinator.join(5)
    assert not coordinator.is_alive()
    assert len(raised) == 1 and isinstance(raised[0], FatalWorkerExit)
    assert sorted(calls) == ["unit_failed", "unit_tail"]
    latest, counts = load_manifest(paths.logs_dir / "unit.manifest.jsonl")
    assert counts == {"unit_failed": 1, "unit_tail": 1}
    assert latest["unit_failed"]["reason"] == "unexpected_worker_exit"
    assert latest["unit_failed"]["retry_scheduled"] is False
    assert latest["unit_tail"]["status"] == "completed"
    final_summary = json.loads(summary_path.read_text())
    assert final_summary["successful_samples"] == 1
    assert final_summary["operational_failure_samples"] == ["unit_failed"]


def test_system_exit_has_distinct_nonretryable_classification(tmp_path: Path) -> None:
    queries = tmp_path / "q.jsonl"
    _write_queries(queries, [{"query_id": "a", "query": "one page"}])
    paths = _paths(tmp_path)

    with pytest.raises(SystemExit) as exc:
        run_batch(
            _args(queries), paths=paths, environment={},
            run_deck=lambda *_: (_ for _ in ()).throw(SystemExit(17)),
        )
    assert exc.value.code == 17
    latest, counts = load_manifest(paths.logs_dir / "unit.manifest.jsonl")
    assert counts == {"unit_a": 1}
    assert latest["unit_a"]["reason"] == "worker_system_exit"
    assert latest["unit_a"]["exit_code"] == "17"
    assert latest["unit_a"]["retryable"] is False


def test_enospc_closes_summary_best_effort_and_is_visible_on_stderr(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    import errno
    from runtime import batch

    queries = tmp_path / "q.jsonl"
    _write_queries(queries, [
        {"query_id": "a", "query": "one page"},
        {"query_id": "b", "query": "one page"},
    ])
    paths = _paths(tmp_path)
    real_append = batch.append_manifest
    calls = []

    def fail_terminal_append(path, record):
        if record.get("status") == "running":
            return real_append(path, record)
        raise OSError(errno.ENOSPC, "No space left on device")

    def enospc(_spec, _environment):
        calls.append(True)
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(batch, "append_manifest", fail_terminal_append)
    with pytest.raises(OSError) as exc:
        run_batch(_args(queries, workers=2), paths=paths, environment={}, run_deck=enospc)
    assert exc.value.errno == errno.ENOSPC
    assert calls == [True, True]
    latest, counts = load_manifest(paths.logs_dir / "unit.manifest.jsonl")
    assert counts == {"unit_a": 1, "unit_b": 1}
    assert {record["status"] for record in latest.values()} == {"running"}
    summary = json.loads((paths.logs_dir / "unit.summary.json").read_text())
    assert summary["ready"] is False
    assert summary["status_counts"] == {"error": 2}
    assert summary["operational_failure_samples"] == ["unit_a", "unit_b"]
    stderr = capsys.readouterr().err
    assert "No space left on device" in stderr
    assert "FAILED to durably publish worker exit sample=unit_a" in stderr
    assert "FAILED to durably publish worker exit sample=unit_b" in stderr


@pytest.mark.parametrize("fail_manifest", [False, True])
def test_unwritable_stderr_does_not_hide_worker_exit(tmp_path: Path, monkeypatch, fail_manifest) -> None:
    import errno
    from runtime import batch

    class BrokenLog:
        def write(self, _text):
            raise OSError(errno.ENOSPC, "log volume full")

        def flush(self):
            raise OSError(errno.ENOSPC, "log volume full")

    queries = tmp_path / "q.jsonl"
    _write_queries(queries, [{"query_id": "a", "query": "one page"}])
    paths = _paths(tmp_path)
    original_append = batch.append_manifest

    def append(path, record):
        if fail_manifest and record["status"] != "running":
            raise OSError(errno.ENOSPC, "manifest volume full")
        original_append(path, record)

    monkeypatch.setattr(batch, "append_manifest", append)
    with monkeypatch.context() as patch:
        patch.setattr(batch.sys, "stderr", BrokenLog())
        with pytest.raises(OSError if fail_manifest else SystemExit):
            run_batch(_args(queries), paths=paths, environment={},
                      run_deck=lambda *_: (_ for _ in ()).throw(SystemExit(17)))
    summary = json.loads((paths.logs_dir / "unit.summary.json").read_text())
    assert summary["status_counts"] == {"error": 1}
    assert summary["successful_samples"] == 0
    assert summary["operational_failure_samples"] == ["unit_a"]


def test_completed_attempt_is_not_overwritten_when_record_callback_raises(
    tmp_path: Path, monkeypatch,
) -> None:
    from runtime import batch

    class AfterDurableWrite(OSError):
        pass

    queries = tmp_path / "q.jsonl"
    _write_queries(queries, [{"query_id": "a", "query": "one page"}])
    paths = _paths(tmp_path)
    real_append = batch.append_manifest
    calls = []

    def append_then_fail(path, record):
        real_append(path, record)
        if record.get("status") == "completed":
            raise AfterDurableWrite("post-fsync callback failure")

    monkeypatch.setattr(batch, "append_manifest", append_then_fail)
    with pytest.raises(AfterDurableWrite):
        run_batch(
            _args(queries), paths=paths, environment={},
            run_deck=lambda *_: calls.append(True) or _Outcome("accepted"),
        )
    assert calls == [True]
    rows = [
        json.loads(line)
        for line in (paths.logs_dir / "unit.manifest.jsonl").read_text().splitlines()
    ]
    assert [row["status"] for row in rows] == ["running", "completed"]
    latest, counts = load_manifest(paths.logs_dir / "unit.manifest.jsonl")
    assert counts == {"unit_a": 1} and latest["unit_a"]["status"] == "completed"
    summary = json.loads((paths.logs_dir / "unit.summary.json").read_text())
    assert summary["ready"] is True and summary["successful_samples"] == 1


@pytest.mark.parametrize("fails", [True, False])
@pytest.mark.parametrize("workers", [1, 4])
def test_soft_stop_finishes_current_attempt_without_retry_or_new_sample(tmp_path, fails, workers):
    queries = tmp_path / "q.jsonl"
    _write_queries(queries, [{"query_id": key, "query": "one page"} for key in ("a", "b")])
    paths = _paths(tmp_path)
    manifest = paths.logs_dir / "unit.manifest.jsonl"
    control = paths.logs_dir / "unit.workers"
    control.parent.mkdir(parents=True)
    control.write_text("1\n")
    calls = []

    def stop_after_attempt(spec, _env):
        calls.append(Path(spec.output_workspace).name)
        Path(spec.output_workspace).mkdir(parents=True, exist_ok=True)
        (Path(spec.output_workspace) / "current-evidence.txt").write_text("preserve attempt")
        control.write_text("0\n")
        if fails:
            raise RuntimeError("controlled failure")
        return _Outcome("not_applicable")

    run_batch(_args(queries, workers=workers), paths=paths, environment={}, run_deck=stop_after_attempt)
    latest, counts = load_manifest(manifest)
    assert calls == ["unit_a"] and counts == {"unit_a": 1}
    assert latest["unit_a"]["status"] == ("error" if fails else "completed")
    assert latest["unit_a"]["retry_scheduled"] is False
    assert (paths.runs_dir / "unit/unit_a/current-evidence.txt").read_text() == "preserve attempt"
    assert not (paths.runs_dir / "unit/_archived_attempts").exists()
    control.write_text("1\n")
    resumed = []
    def succeed(spec, _env):
        resumed.append(Path(spec.output_workspace).name)
        return _Outcome("not_applicable")
    run_batch(_args(queries, resume=True, workers=workers), paths=paths, environment={}, run_deck=succeed)
    assert resumed == (["unit_a", "unit_b"] if fails else ["unit_b"])
    latest, counts = load_manifest(manifest)
    assert counts == {"unit_a": 2 if fails else 1, "unit_b": 1}
    assert all(row["status"] == "completed" for row in latest.values())


def test_shared_pool_gate_uses_paired_route_identities_and_ignores_cooldowns(tmp_path, monkeypatch):
    from dataclasses import replace
    from runtime import config, session
    from runtime.batch import _provider_pool_quarantined_check
    from runtime.provider_health import ProviderHealthStore

    environment = {"MODEL_API_FORMAT": "openai_responses", "MODEL": "gpt-6-astra/azure/sfa",
                   "MODEL_BASE_URL": "https://fixture.invalid"}
    channels = (session.ProviderChannel("fixture-key", "a", "gpt-6-astra/azure/sfa"),
                session.ProviderChannel("fixture-key", "b", "gpt-6-astra/azure_L/qwb"))
    monkeypatch.setattr(session, "provider_channels", lambda _env: channels)
    monkeypatch.setattr(config, "load_runtime_config",
                        lambda _env: {"resources": {"directory": str(tmp_path / "health")}})
    check = _provider_pool_quarantined_check(environment)
    store = ProviderHealthStore(tmp_path / "health")
    settings = config.ProviderSettings.from_environment(environment)
    first = store.identity(replace(settings, model=channels[0].model), channels[0].key)
    second = store.identity(replace(settings, model=channels[1].model), channels[1].key)
    assert first != second and check() is False
    store.record_failure(first, ProviderFailure("authentication", "fixture quota", status_code=403))
    assert check() is False
    store.record_failure(second, ProviderFailure("rate_limit", "fixture cooldown", status_code=429,
                                                retryable=True, retry_after_s=30))
    assert check() is False
    store.record_failure(second, ProviderFailure("authentication", "fixture quota", status_code=403))
    assert check() is True


def test_shared_pool_gate_leaves_known_unavailable_queries_unstarted(tmp_path, monkeypatch):
    from runtime import batch

    queries = tmp_path / "q.jsonl"
    _write_queries(queries, [{"query_id": key, "query": "one page"} for key in ("a", "b")])
    monkeypatch.setattr(batch, "_provider_pool_quarantined_check", lambda _env: lambda: True)
    paths = _paths(tmp_path)
    calls = []
    assert run_batch(_args(queries), paths=paths, environment={},
                     run_deck=lambda *_: calls.append(True) or _Outcome("not_applicable")) == 1
    assert calls == []
    assert load_manifest(paths.logs_dir / "unit.manifest.jsonl") == ({}, {})
    summary = json.loads((paths.logs_dir / "unit.summary.json").read_text())
    assert summary["pending_samples"] == ["unit_a", "unit_b"]
    assert summary["attempt_records"] == 0
    assert not (paths.runs_dir / "unit/unit_a").exists()
    assert not (paths.runs_dir / "unit/unit_b").exists()


def test_shared_pool_gate_drains_inflight_without_retry_or_refill(tmp_path, monkeypatch):
    import threading
    from runtime import batch

    queries = tmp_path / "q.jsonl"
    _write_queries(queries, [{"query_id": key, "query": "one page"} for key in ("a", "b", "c")])
    paths = _paths(tmp_path)
    held = threading.Event()
    second_started = threading.Event()
    monkeypatch.setattr(batch, "_provider_pool_quarantined_check", lambda _env: held.is_set)
    calls = []

    def run_inflight(spec, _env):
        run_dir = Path(spec.output_workspace)
        calls.append(run_dir.name)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "current-evidence.txt").write_text("preserved")
        if run_dir.name == "unit_a":
            assert second_started.wait(5)
            held.set()
            raise RuntimeError("controlled recoverable failure")
        second_started.set()
        assert held.wait(5)
        return _Outcome("not_applicable")

    assert run_batch(_args(queries, workers=2), paths=paths, environment={}, run_deck=run_inflight) == 1
    latest, counts = load_manifest(paths.logs_dir / "unit.manifest.jsonl")
    assert sorted(calls) == ["unit_a", "unit_b"]
    assert counts == {"unit_a": 1, "unit_b": 1}
    assert latest["unit_a"]["status"] == "error"
    assert latest["unit_a"]["retry_scheduled"] is False
    assert latest["unit_b"]["status"] == "completed"
    assert not (paths.runs_dir / "unit/unit_c").exists()
    assert all((paths.runs_dir / "unit" / key / "current-evidence.txt").read_text() == "preserved"
               for key in ("unit_a", "unit_b"))


def test_local_pool_exhaustion_does_not_stop_other_queries(tmp_path, monkeypatch):
    from runtime import batch

    queries = tmp_path / "q.jsonl"
    _write_queries(queries, [{"query_id": key, "query": "one page"} for key in ("a", "b")])
    paths = _paths(tmp_path)
    monkeypatch.setattr(batch, "_provider_pool_quarantined_check", lambda _env: lambda: False)
    calls = []

    def run_local_failure(spec, _env):
        calls.append(Path(spec.output_workspace).name)
        if calls[-1] == "unit_a":
            raise AgentProviderError("input-local exclusions", failure=ProviderFailure(
                "channel_pool_exhausted", "input-local exclusions"))
        return _Outcome("not_applicable")

    run_batch(_args(queries), paths=paths, environment={}, run_deck=run_local_failure)
    latest, counts = load_manifest(paths.logs_dir / "unit.manifest.jsonl")
    assert calls == ["unit_a", "unit_b"]
    assert counts == {"unit_a": 1, "unit_b": 1}
    assert latest["unit_b"]["status"] == "completed"


def test_resume_reconciles_terminal_result_with_manifest(tmp_path: Path) -> None:
    queries = tmp_path / "q.jsonl"
    _write_queries(queries, [{"query_id": "a", "query": "一页"}])
    paths = _paths(tmp_path)
    run_dir = paths.runs_dir / "unit" / "unit_a"
    run_dir.mkdir(parents=True)
    (run_dir / "result.json").write_text(json.dumps({"status": "completed", "training_manifest": {"training_admission": "accepted"}}))
    run_batch(_args(queries, resume=True), run_deck=lambda *_: pytest.fail("must not rerun"), environment={}, paths=paths)
    latest, counts = load_manifest(paths.logs_dir / "unit.manifest.jsonl")
    assert latest["unit_a"]["status"] == "completed"
    assert counts == {"unit_a": 1}
    summary = json.loads((paths.logs_dir / "unit.summary.json").read_text())
    assert summary["ready"] is True


def test_resume_current_result_pointer_preserves_pending_qc_without_regeneration(tmp_path):
    queries = tmp_path / "q.jsonl"
    _write_queries(queries, [{"query_id": "a", "query": "一页"}])
    paths = _paths(tmp_path)
    run_dir = paths.runs_dir / "unit" / "unit_a"
    run_dir.mkdir(parents=True)
    result = run_dir / "result.json"
    result.write_text(json.dumps({"status": "completed", "training_admission": "pending",
        "training_manifest": "_trace/training/training-manifest.json", "training_rejection_reasons": ["critic_incomplete"]}))
    before = result.read_bytes()
    run_batch(_args(queries, resume=True), run_deck=lambda *_: pytest.fail("must not rerun raw"), environment={}, paths=paths)
    latest, _ = load_manifest(paths.logs_dir / "unit.manifest.jsonl")
    assert latest["unit_a"]["training_admission"] == "pending"
    assert latest["unit_a"]["training_export"]["quality_rejection_reasons"] == []
    assert latest["unit_a"]["training_export"]["pending_reasons"] == ["critic_incomplete"]
    assert result.read_bytes() == before


def test_run_spec_maps_controls(tmp_path: Path) -> None:
    # 通过端到端捕获 run_deck 收到的 RunSpec,验证 controls 映射。
    queries = tmp_path / "q.jsonl"
    _write_queries(
        queries,
        [
            {
                "query_id": "z1",
                "query": "介绍量子计算",
                "controls": {"language": "en", "page_count_policy": 5},
            }
        ],
    )
    captured: list[RunSpec] = []

    def run_deck(run_spec: RunSpec, _env) -> _Outcome:
        captured.append(run_spec)
        return _Outcome("accepted")

    run_batch(_args(queries), run_deck=run_deck, environment={}, paths=_paths(tmp_path))
    assert len(captured) == 1
    spec = captured[0]
    assert spec.raw_user_query == "介绍量子计算"
    assert spec.language == "en"
    assert spec.page_count_hint == 5
    assert spec.run_mode == RunMode.SYNTHESIS


# ---------------------------------------------------------------------------
# summary fields + limit
# ---------------------------------------------------------------------------


def test_summary_has_all_required_fields_and_ready(tmp_path: Path) -> None:
    queries = tmp_path / "q.jsonl"
    _write_queries(
        queries,
        [
            {"query_id": "ok", "query": "a"},
            {"query_id": "reject", "query": "b"},
            {"query_id": "excluded", "query": "c"},
        ],
    )

    def run_deck(run_spec: RunSpec, _env) -> _Outcome:
        name = Path(run_spec.output_workspace).name
        if name.endswith("_reject"):
            return _Outcome("rejected", ["too_few_pages"])
        if name.endswith("_excluded"):
            raise TrainingExportError("synthesis QC incomplete", manifest={
                "training_admission": "pending", "rejection_reasons": ["critic_incomplete"]})
        return _Outcome("accepted")

    paths = _paths(tmp_path)
    code = run_batch(_args(queries), run_deck=run_deck, environment={}, paths=paths)
    summary_path = paths.logs_dir / "unit.summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    assert _SUMMARY_REQUIRED_FIELDS.issubset(summary)
    assert summary["expected_samples"] == 3
    # 生成均完成；QC 未决独立显示，不能作为质量负样本或重新生成。
    assert summary["ready"] is True
    assert code == 0
    assert summary["terminal_samples"] == 3
    assert summary["successful_samples"] == 3
    assert summary["content_excluded_samples"] == 0
    assert summary["training_pending_samples"] == 1
    assert summary["training_accepted_samples"] == 1
    assert summary["quality_excluded_samples"] == 1
    assert summary["training_rejection_reasons"].get("too_few_pages") == 1
    assert summary["operational_failure_samples"] == []


def test_limit_only_takes_first_n(tmp_path: Path) -> None:
    queries = tmp_path / "q.jsonl"
    _write_queries(queries, [{"query_id": f"q{i}", "query": str(i)} for i in range(5)])
    ran: list[str] = []

    def run_deck(run_spec: RunSpec, _env) -> _Outcome:
        ran.append(Path(run_spec.output_workspace).name)
        return _Outcome("accepted")

    paths = _paths(tmp_path)
    run_batch(_args(queries, limit=2), run_deck=run_deck, environment={}, paths=paths)
    assert len(ran) == 2
    summary = json.loads((paths.logs_dir / "unit.summary.json").read_text("utf-8"))
    assert summary["expected_samples"] == 2


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------


def test_resume_skips_completed_in_manifest(tmp_path: Path) -> None:
    queries = tmp_path / "q.jsonl"
    _write_queries(queries, [{"query_id": "a", "query": "1"}, {"query_id": "b", "query": "2"}])
    paths = _paths(tmp_path)
    # 预写 manifest:a 已 completed。
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    manifest = paths.logs_dir / "unit.manifest.jsonl"
    manifest.write_text(
        json.dumps(
            {"sample_id": "unit_a", "status": "completed", "training_admission": "accepted"}
        )
        + "\n",
        encoding="utf-8",
    )
    ran: list[str] = []

    def run_deck(run_spec: RunSpec, _env) -> _Outcome:
        ran.append(Path(run_spec.output_workspace).name)
        return _Outcome("accepted")

    run_batch(_args(queries, resume=True), run_deck=run_deck, environment={}, paths=paths)
    # 只应跑 b,a 被 resume 跳过。
    assert ran == ["unit_b"]


def test_resume_skips_completed_on_disk_result(tmp_path: Path) -> None:
    queries = tmp_path / "q.jsonl"
    _write_queries(queries, [{"query_id": "a", "query": "1"}])
    paths = _paths(tmp_path)
    run_dir = paths.runs_dir / "unit" / "unit_a"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "result.json").write_text(
        json.dumps({"schema": "mural.result.v1", "status": "completed"}),
        encoding="utf-8",
    )
    ran: list[str] = []

    def run_deck(run_spec: RunSpec, _env) -> _Outcome:
        ran.append(Path(run_spec.output_workspace).name)
        return _Outcome("accepted")

    run_batch(_args(queries, resume=True), run_deck=run_deck, environment={}, paths=paths)
    assert ran == []  # 磁盘 result.json completed → 跳过


def test_resume_does_not_count_killed_running_as_attempt(tmp_path: Path) -> None:
    queries = tmp_path / "q.jsonl"
    _write_queries(queries, [{"query_id": "a", "query": "1"}])
    paths = _paths(tmp_path)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    manifest = paths.logs_dir / "unit.manifest.jsonl"
    manifest.write_text(
        json.dumps({"sample_id": "unit_a", "attempt_no": 1, "status": "running"}) + "\n",
        encoding="utf-8",
    )
    ran: list[str] = []

    def run_deck(run_spec: RunSpec, _env) -> _Outcome:
        ran.append(Path(run_spec.output_workspace).name)
        return _Outcome("accepted")

    run_batch(
        _args(queries, resume=True, max_attempts=1),
        run_deck=run_deck,
        environment={},
        paths=paths,
    )
    assert ran == ["unit_a"]
    latest, counts = load_manifest(manifest)
    assert latest["unit_a"]["status"] == "completed"
    assert counts == {"unit_a": 1}


def test_resume_reruns_killed_third_running_without_exhausting(tmp_path: Path) -> None:
    queries = tmp_path / "q.jsonl"
    _write_queries(queries, [{"query_id": "a", "query": "1"}])
    paths = _paths(tmp_path)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    manifest = paths.logs_dir / "unit.manifest.jsonl"
    rows = [
        {"sample_id": "unit_a", "attempt_no": 1, "status": "running"},
        {"sample_id": "unit_a", "attempt_no": 1, "status": "error", "retryable": True},
        {"sample_id": "unit_a", "attempt_no": 2, "status": "running"},
        {"sample_id": "unit_a", "attempt_no": 2, "status": "error", "retryable": True},
        {"sample_id": "unit_a", "attempt_no": 3, "status": "running"},
    ]
    manifest.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    ran: list[int] = []

    def run_deck(_run_spec: RunSpec, _env) -> _Outcome:
        ran.append(1)
        return _Outcome("accepted")

    run_batch(
        _args(queries, resume=True, max_attempts=3),
        run_deck=run_deck,
        environment={},
        paths=paths,
    )
    assert ran == [1]
    latest, counts = load_manifest(manifest)
    assert latest["unit_a"]["status"] == "completed"
    assert counts == {"unit_a": 3}


# ---------------------------------------------------------------------------
# retry classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("error", [
    AgentContextWindowError("protected inputs still exceed the context budget"),
    AgentProtocolError("reasoning_only_no_action after targeted recovery"),
    AgentTurnLimitError("max_turns reached"),
    RepeatedToolFailureError("repeated reads", tool_name="read_file", error_code="no_progress_file_read_loop",
        arguments_summary="current evidence", repeat_count=9),
    RuntimeError("unclassified implementation failure"),
])
def test_hard_failures_do_not_receive_fresh_automatic_budgets(tmp_path, error):
    queries, paths = tmp_path / "q.jsonl", _paths(tmp_path)
    _write_queries(queries, [{"query_id": "a", "query": "one page"}])
    calls = []

    def failed(spec, _environment):
        calls.append(spec.output_workspace)
        spec.output_workspace.mkdir(parents=True, exist_ok=True)
        (spec.output_workspace / "completed-evidence.txt").write_text("preserve the original workspace")
        raise error

    assert run_batch(_args(queries, max_attempts=3), paths=paths, environment={}, run_deck=failed) != 0
    assert len(calls) == 1
    assert (calls[0] / "completed-evidence.txt").read_text() == "preserve the original workspace"
    latest, counts = load_manifest(paths.logs_dir / "unit.manifest.jsonl")
    assert counts == {"unit_a": 1}
    assert latest["unit_a"]["retryable"] is False and latest["unit_a"]["retry_scheduled"] is False


@pytest.mark.parametrize("checkpoint_state,fixed", [("missing", False), ("corrupt", False), ("closed", True), ("closed", False)])
def test_known_tool_contract_failure_has_at_most_one_checkpoint_backed_recovery(tmp_path, checkpoint_state, fixed):
    from owner.context import OwnerContextStore
    from runtime.recovery import SegmentCheckpoint

    queries, paths = tmp_path / "q.jsonl", _paths(tmp_path)
    _write_queries(queries, [{"query_id": "a", "query": "one page"}])
    calls = []

    def run_deck(spec, _environment):
        calls.append(spec.output_workspace)
        state = spec.output_workspace / "_state"
        artifact = spec.output_workspace / "saved-page.txt"
        if len(calls) == 1:
            state.mkdir(parents=True)
            (state / "run.json").write_text(json.dumps(spec.to_manifest()))
            (state / "workspace.json").write_text('{"pages":[1]}')
            artifact.write_text("actual saved page")
            if checkpoint_state != "missing":
                store = OwnerContextStore(spec.output_workspace)
                checkpoint = SegmentCheckpoint.initial((
                    {"role": "user", "content": "original task"},
                    {"role": "assistant", "content": [{"type": "tool_use", "id": "saved", "name": "write_file", "input": {"path": "deck.md"}}]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "saved", "content": "saved"}]},
                ))
                store.save_orchestrator(checkpoint, identity={}, artifact_hashes={}, live_cursor=0)
                if checkpoint_state == "corrupt":
                    payload = json.loads(store.path_for("orchestrator").read_text())
                    payload["segment_sha256"] = "not-the-saved-checksum"
                    store.path_for("orchestrator").write_text(json.dumps(payload))
        else:
            assert artifact.read_text() == "actual saved page"
            assert spec.output_workspace == calls[0]
            if fixed:
                return _Outcome("not_applicable")
        raise RepeatedToolFailureError("denied patches", tool_name="patch", error_code="write_scope_denied",
                                       arguments_summary="delegated page", repeat_count=3)

    result = run_batch(_args(queries, max_attempts=3), paths=paths, environment={}, run_deck=run_deck)
    expected_attempts = 2 if checkpoint_state == "closed" else 1
    assert len(calls) == expected_attempts and (result == 0) is fixed
    latest, counts = load_manifest(paths.logs_dir / "unit.manifest.jsonl")
    assert counts == {"unit_a": expected_attempts}
    assert latest["unit_a"]["retry_scheduled"] is False
    if not fixed:
        assert latest["unit_a"]["retryable"] is False
    assert not (paths.runs_dir / "unit/_archived_attempts").exists()


def test_provider_retry_resumes_same_workspace_and_preserves_completed_artifacts(tmp_path):
    queries, paths = tmp_path / "q.jsonl", _paths(tmp_path)
    _write_queries(queries, [{"query_id": "a", "query": "one page"}])
    calls = []

    def run_deck(spec, environment):
        calls.append(spec.output_workspace)
        state = spec.output_workspace / "_state"
        page = spec.output_workspace / "page-evidence.txt"
        if len(calls) == 1:
            state.mkdir(parents=True)
            (state / "run.json").write_text(json.dumps(spec.to_manifest()))
            (state / "workspace.json").write_text('{"pages":[1]}')
            (state / "owners.json").write_text('{"original-owner":[1]}')
            page.write_text("completed pixels and closed checkpoint")
            raise AgentProviderError("429: system memory limit", failure=ProviderFailure(
                "rate_limit", "system memory limit", status_code=429, retryable=True))
        assert page.read_text() == "completed pixels and closed checkpoint"
        assert json.loads((state / "owners.json").read_text()) == {"original-owner": [1]}
        return _Outcome("not_applicable")

    assert run_batch(_args(queries), paths=paths, environment={}, run_deck=run_deck) == 0
    assert len(calls) == 2 and calls[0] == calls[1]
    assert not (paths.runs_dir / "unit/_archived_attempts").exists()
    manifest = [json.loads(line) for line in (paths.logs_dir / "unit.manifest.jsonl").read_text().splitlines()]
    terminal = [row for row in manifest if row["status"] != "running"]
    assert [row["execution"] for row in terminal] == ["fresh", "resume"]
    assert [row["status"] for row in terminal] == ["error", "completed"]


def test_recoverable_main_compaction_resumes_workspace_without_repeating_successful_tools(tmp_path):
    queries, paths = tmp_path / "q.jsonl", _paths(tmp_path)
    _write_queries(queries, [{"query_id": "a", "query": "one page"}])
    calls = []
    successful_tool_calls = []

    def run_deck(spec, _environment):
        calls.append(spec.output_workspace)
        state = spec.output_workspace / "_state"
        artifact = spec.output_workspace / "completed-tool.txt"
        if len(calls) == 1:
            state.mkdir(parents=True)
            (state / "workspace.json").write_text('{"pages":[1]}')
            artifact.write_text("successful tool output")
            successful_tool_calls.append("write_file")
            raise AgentInterruptedError(
                "owner_interrupted: context_compaction_recoverable; checkpoint retained; "
                "restart this Owner or delegate a narrower task"
            )
        assert artifact.read_text() == "successful tool output"
        assert successful_tool_calls == ["write_file"]
        return _Outcome("accepted")

    assert run_batch(_args(queries, max_attempts=2), paths=paths,
                     environment={}, run_deck=run_deck) == 0
    assert len(calls) == 2 and calls[0] == calls[1]
    assert not (paths.runs_dir / "unit/_archived_attempts").exists()
    rows = [json.loads(line) for line in (paths.logs_dir / "unit.manifest.jsonl").read_text().splitlines()]
    terminal = [row for row in rows if row["status"] != "running"]
    assert [row["execution"] for row in terminal] == ["fresh", "resume"]
    assert terminal[0]["reason"] == "context_compaction_recoverable"
    assert [row["retryable"] for row in terminal] == [True, False]


@pytest.mark.parametrize("stall_message", [
    OWNER_FAILURE_STALL_MESSAGE,
    COMPLETION_GAP_STALL_MESSAGE,
])
def test_recoverable_terminal_owner_stall_resumes_same_workspace(tmp_path, stall_message):
    queries, paths = tmp_path / "q.jsonl", _paths(tmp_path)
    _write_queries(queries, [{"query_id": "a", "query": "one page"}])
    calls = []

    def run_deck(spec, _environment):
        calls.append(spec.output_workspace)
        state = spec.output_workspace / "_state"
        marker = spec.output_workspace / "completed-tool.txt"
        if len(calls) == 1:
            state.mkdir(parents=True)
            (state / "workspace.json").write_text('{"pages":[1]}')
            marker.write_text("preserved")
            raise AgentInterruptedError(stall_message)
        assert marker.read_text() == "preserved"
        return _Outcome("accepted")

    assert run_batch(_args(queries, max_attempts=2), paths=paths,
                     environment={}, run_deck=run_deck) == 0
    assert len(calls) == 2 and calls[0] == calls[1]
    rows = [json.loads(line) for line in
            (paths.logs_dir / "unit.manifest.jsonl").read_text().splitlines()]
    terminal = [row for row in rows if row["status"] != "running"]
    assert [row["execution"] for row in terminal] == ["fresh", "resume"]
    assert terminal[0]["reason"] == "owner_recovery_required"
    assert terminal[0]["retryable"] is True


@pytest.mark.parametrize("error", [
    InterruptedError("owner_interrupted: summary request cancelled"),
    InterruptedError("ordinary interruption"),
    AgentInterruptedError("owner_interrupted: resource queue cancelled"),
])
def test_non_compaction_interruptions_do_not_retry_batch(tmp_path, error):
    queries, paths = tmp_path / "q.jsonl", _paths(tmp_path)
    _write_queries(queries, [{"query_id": "a", "query": "one page"}])
    calls = []

    def run_deck(_spec, _environment):
        calls.append(True)
        raise error

    assert run_batch(_args(queries, max_attempts=3), paths=paths,
                     environment={}, run_deck=run_deck) != 0
    assert calls == [True]
    latest, counts = load_manifest(paths.logs_dir / "unit.manifest.jsonl")
    assert counts == {"unit_a": 1}
    assert latest["unit_a"]["retryable"] is False
    assert latest["unit_a"]["retry_scheduled"] is False


@pytest.mark.parametrize("revision", [None, {}, {"instruction": "Finish the existing pages"}])
def test_batch_resume_preserves_explicit_revision_input(tmp_path, revision):
    queries, paths = tmp_path / "q.jsonl", _paths(tmp_path)
    _write_queries(queries, [{"query_id": "a", "query": "original query", "revision_request": revision}])
    workspace = paths.runs_dir / "unit/unit_a"
    prior = RunSpec.from_inputs(raw_user_query="original query", output_workspace=workspace,
        revision_request=revision, run_mode="synthesis", authoring_profile="general", environment={})
    state = workspace / "_state"
    state.mkdir(parents=True)
    (state / "run.json").write_text(json.dumps(prior.to_manifest()))
    captured = []

    def run_deck(spec, environment):
        captured.append(spec.to_manifest()["revision_request"])
        return _Outcome("accepted")

    assert run_batch(_args(queries, resume=True), paths=paths, environment={}, run_deck=run_deck) == 0
    assert captured == [revision]
    assert json.loads((state / "run.json").read_text()) == prior.to_manifest()


def test_batch_resume_does_not_infer_or_overwrite_a_missing_revision_input(tmp_path):
    queries, paths = tmp_path / "q.jsonl", _paths(tmp_path)
    _write_queries(queries, [{"query_id": "a", "query": "original query"}])
    workspace = paths.runs_dir / "unit/unit_a"
    prior = RunSpec.from_inputs(raw_user_query="original query", output_workspace=workspace,
        revision_request={"instruction": "Preserve this original revision"}, run_mode="synthesis",
        authoring_profile="general", environment={})
    state = workspace / "_state"
    state.mkdir(parents=True)
    (state / "run.json").write_text(json.dumps(prior.to_manifest()))
    assert run_batch(_args(queries, resume=True), paths=paths, environment={},
                     run_deck=lambda *_: pytest.fail("changed input must fail before execution")) != 0
    latest, _ = load_manifest(paths.logs_dir / "unit.manifest.jsonl")
    assert "batch_resume_input_changed: revision_request" in latest["unit_a"]["error"]
    assert json.loads((state / "run.json").read_text()) == prior.to_manifest()


@pytest.mark.parametrize("change", ["query", "attachment"])
def test_batch_resume_rejects_changed_input_before_starting_runtime(tmp_path, change):
    import hashlib
    queries, paths = tmp_path / "q.jsonl", _paths(tmp_path)
    attachment = tmp_path / "input.txt"
    attachment.write_text("original attachment")
    _write_queries(queries, [{"query_id": "a", "query": "original query",
        "attachments": [{"path": str(attachment)}]}])
    calls = []

    def run_deck(spec, environment):
        calls.append(True)
        state = spec.output_workspace / "_state"
        state.mkdir(parents=True)
        prior = spec.to_manifest()
        if change == "query":
            prior["raw_user_query"] = "different query with the same ID"
        (state / "run.json").write_text(json.dumps(prior))
        (state / "skill-runtime.json").write_text(json.dumps({"checkpoint_attachments": [{
            "sha256": hashlib.sha256(attachment.read_bytes()).hexdigest(),
            "original_name": attachment.name, "intent": ""}]}))
        if change == "attachment":
            attachment.write_text("changed bytes at same path")
        raise AgentProviderError("429", failure=ProviderFailure("rate_limit", "busy", retryable=True))

    assert run_batch(_args(queries), paths=paths, environment={}, run_deck=run_deck) != 0
    assert len(calls) == 1
    rows = [json.loads(line) for line in (paths.logs_dir / "unit.manifest.jsonl").read_text().splitlines()]
    assert rows[-1]["reason"] == "batch_input_invalid"
    assert "batch_resume_input_changed" in rows[-1]["error"]
    assert rows[-1]["retryable"] is False


@pytest.mark.parametrize("kind", ["authentication", "access_disabled", "channel_pool_exhausted", "provider_protocol_error"])
def test_batch_does_not_restart_deck_for_nonrecoverable_provider_failure(tmp_path, kind):
    queries, paths = tmp_path / "q.jsonl", _paths(tmp_path)
    _write_queries(queries, [{"query_id": "a", "query": "one page"}])
    calls = []
    def run_deck(spec, environment):
        calls.append(True)
        raise AgentProviderError(kind, failure=ProviderFailure(
            kind, "unavailable", retryable=kind in {"authentication", "provider_protocol_error"}))
    assert run_batch(_args(queries), paths=paths, environment={}, run_deck=run_deck) != 0
    assert len(calls) == 1


@pytest.mark.parametrize("pause", [False, True])
def test_batch_cooldown_waits_without_model_calls_and_obeys_soft_stop(tmp_path, monkeypatch, pause):
    from runtime import batch
    queries, paths = tmp_path / "q.jsonl", _paths(tmp_path)
    _write_queries(queries, [{"query_id": "a", "query": "one page"}])
    args, calls, sleeps, clock = _args(queries), [], [], [0.0]
    sample = batch._Sample("unit_a", {"query": "one page"}, paths.runs_dir / "unit/unit_a")
    def run_deck(spec, environment):
        calls.append(clock[0])
        if len(calls) == 1:
            raise AgentProviderError("busy", failure=ProviderFailure("rate_limit", "busy", retryable=True), retry_after_s=3)
        return _Outcome("not_applicable")
    def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay
    monkeypatch.setattr(batch.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(batch.time, "sleep", sleep)
    records = batch.supervised_sample(sample, args, {}, run_deck,
        can_retry=lambda: not (pause and clock[0] >= 1))
    assert calls == ([0] if pause else [0, 3])
    assert sum(sleeps) == (1 if pause else 3)
    assert len(records) == (1 if pause else 2)


def test_transient_failure_retries_until_success(tmp_path: Path) -> None:
    queries = tmp_path / "q.jsonl"
    _write_queries(queries, [{"query_id": "a", "query": "1"}])
    attempts = {"n": 0}

    def run_deck(_run_spec: RunSpec, _env) -> _Outcome:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise AgentProviderError("provider timeout", failure=ProviderFailure("timeout", "temporary", retryable=True))
        return _Outcome("accepted")

    paths = _paths(tmp_path)
    code = run_batch(
        _args(queries, max_attempts=3), run_deck=run_deck, environment={}, paths=paths
    )
    assert attempts["n"] == 3
    assert code == 0
    summary = json.loads((paths.logs_dir / "unit.summary.json").read_text("utf-8"))
    assert summary["ready"] is True
    assert summary["successful_samples"] == 1
    assert summary["attempt_records"] == 3  # 2 error + 1 completed
    assert summary["retried_samples"] == 1


def test_completed_raw_with_rejected_training_is_terminal_no_retry(tmp_path: Path) -> None:
    queries = tmp_path / "q.jsonl"
    _write_queries(queries, [{"query_id": "a", "query": "1"}])
    attempts = {"n": 0}

    def run_deck(_run_spec: RunSpec, _env) -> _Outcome:
        attempts["n"] += 1
        raise TrainingExportError("synthesis trace admission rejected", manifest={
            "training_admission": "rejected", "rejection_reasons": ["content_qc_unresolved"]})

    paths = _paths(tmp_path)
    code = run_batch(
        _args(queries, max_attempts=3), run_deck=run_deck, environment={}, paths=paths
    )
    # 内容排除是终态,只跑一次,不重试。
    assert attempts["n"] == 1
    summary = json.loads((paths.logs_dir / "unit.summary.json").read_text("utf-8"))
    assert summary["ready"] is True  # 明确排除也算终态
    assert code == 0
    assert summary["content_excluded_samples"] == 0
    assert summary["quality_excluded_samples"] == 1
    assert summary["status_counts"].get("completed") == 1


def test_training_export_failure_is_operational_not_content_exclusion(tmp_path: Path) -> None:
    queries = tmp_path / "q.jsonl"
    _write_queries(queries, [{"query_id": "a", "query": "1"}])

    def run_deck(_run_spec, _env):
        raise TrainingExportError("message image blob is missing or corrupt")

    paths = _paths(tmp_path)
    code = run_batch(_args(queries, max_attempts=1), run_deck=run_deck, environment={}, paths=paths)
    summary = json.loads((paths.logs_dir / "unit.summary.json").read_text())
    assert code != 0 and not summary["ready"]
    assert summary["content_excluded_samples"] == summary["quality_excluded_samples"] == 0
    assert summary["operational_failure_samples"] == ["unit_a"]


def test_exhausted_operational_failure_not_ready(tmp_path: Path) -> None:
    queries = tmp_path / "q.jsonl"
    _write_queries(queries, [{"query_id": "a", "query": "1"}])

    def run_deck(_run_spec: RunSpec, _env) -> _Outcome:
        raise AgentProviderError("gateway 503", failure=ProviderFailure("transport_or_server", "503", retryable=True))

    paths = _paths(tmp_path)
    code = run_batch(
        _args(queries, max_attempts=2), run_deck=run_deck, environment={}, paths=paths
    )
    summary = json.loads((paths.logs_dir / "unit.summary.json").read_text("utf-8"))
    assert summary["ready"] is False  # 运维失败耗尽 attempts → 非终态
    assert code == 1
    assert summary["operational_failure_samples"] == ["unit_a"]
    assert summary["exhausted_samples"] == ["unit_a"]


# ---------------------------------------------------------------------------
# paths from environment (run.sh contract)
# ---------------------------------------------------------------------------


def test_batch_paths_from_clean_env(tmp_path: Path) -> None:
    env = {
        "CLEAN_RUNS_DIR": str(tmp_path / "R" / "runs"),
        "CLEAN_LOGS_DIR": str(tmp_path / "R" / "logs"),
    }
    paths = BatchPaths.from_environment(env)
    assert paths.runs_dir == tmp_path / "R" / "runs"
    assert paths.logs_dir == tmp_path / "R" / "logs"
    # 只给 artifact root 时按约定回退。
    fallback = BatchPaths.from_environment({"CLEAN_ARTIFACT_ROOT": str(tmp_path / "A")})
    assert fallback.runs_dir == tmp_path / "A" / "runs"
    assert fallback.logs_dir == tmp_path / "A" / "logs"


# ---------------------------------------------------------------------------
# end-to-end: batch reuses the REAL single-deck kernel (mock provider only)
# ---------------------------------------------------------------------------


def _renderer(_html: Path, png: Path, canvas: dict):
    png.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1600, 900), (30, 90, 160)).save(png)
    return {"geometry": canvas, "loaded_resources": [], "failed_resources": []}


def _analyzer(_path: Path, _question: str, _scope: str, _canvas: dict):
    return {"summary": "像素检查完成"}


def _backend(services: RuntimeServices):
    from test_shared_entry import _backend as shared_backend
    return shared_backend(services)


def test_batch_end_to_end_real_kernel(tmp_path: Path) -> None:
    queries = tmp_path / "q.jsonl"
    _write_queries(
        queries,
        [
            {"query_id": "d1", "query": "同一输入必须走同一生成内核。"},
            {"query_id": "d2", "query": "同一输入必须走同一生成内核。"},
        ],
    )

    def run_deck(run_spec: RunSpec, _env):
        return run_presenter(
            run_spec,
            _backend,
            renderer=_renderer,
            vision_analyzer=_analyzer,
            include_pdf=False,
            include_pptx=False,
        )

    paths = _paths(tmp_path)
    code = run_batch(
        _args(queries, workers=2, batch="e2e"),
        run_deck=run_deck,
        environment={},
        paths=paths,
    )
    assert code == 0
    summary = json.loads((paths.logs_dir / "e2e.summary.json").read_text("utf-8"))
    assert summary["ready"] is True
    assert summary["expected_samples"] == 2
    assert summary["successful_samples"] == 2
    assert summary["training_accepted_samples"] == 0
    assert summary["training_not_evaluated_samples"] == 2
    # 每个 deck 真的产出了 result.json(证明走的是 run_presenter 交付路径)。
    for sid in ("e2e_d1", "e2e_d2"):
        assert (paths.runs_dir / "e2e" / sid / "result.json").is_file()
        assert not (paths.runs_dir / "e2e" / sid / "_trace/training/training-manifest.json").exists()


def test_build_summary_pending_when_no_records(tmp_path: Path) -> None:
    summary = build_batch_summary(
        batch="x",
        expected_sample_ids=["x_a", "x_b"],
        manifest_path=tmp_path / "missing.manifest.jsonl",
        max_attempts=3,
    )
    assert summary["ready"] is False
    assert summary["pending_samples"] == ["x_a", "x_b"]
    assert summary["expected_samples"] == 2
