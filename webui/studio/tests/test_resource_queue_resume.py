"""本地模型排队失败须穿过 Agent/入口/Studio，保留真实首次请求的安全重进。"""
import inspect
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from app import engine, jobs


REPOSITORY = Path(__file__).resolve().parents[3]


def _queue_child(repository, workspace, allowed_calls, resume, interruption="0"):
    """仅在正式 inference 子进程调用，避免与 Studio 的同名 runtime 模块互相污染。"""
    import ast
    from contextlib import contextmanager
    import json
    import os
    from pathlib import Path
    import signal
    import sys
    import time
    from types import SimpleNamespace

    repository, root = Path(repository), Path(workspace)
    harness = repository / "harnesses/mural-presenter-v1"
    sys.path.insert(0, str(harness))
    from infer import PresenterKernel
    from runtime.agent import AgentInterruptedError
    from runtime.config import RunSpec
    from runtime.production import ProductionSettings
    from runtime.recovery import ResourceSlotTimeout
    from runtime.session import ProviderChannel, TransportResponse
    from runtime.skill_runtime import SkillRuntime

    allowed_calls = int(allowed_calls)
    environment = {"MODEL_API_FORMAT": "openai", "MODEL": "queue-test",
                   "MURAL_STUDIO_DATA_DIR": str(root.parent / "shared")}
    source = root.parent / "source.txt"
    spec = RunSpec.from_inputs(raw_user_query="本地排队恢复测试", output_workspace=root,
                               attachments=[source] if source.is_file() else [],
                               environment=environment)
    calls = []

    class Transport:
        def complete(self, _channel, request, **_kwargs):
            calls.append(request)
            turn = sum(block.get("type") == "tool_use" for message in request.messages
                       for block in message.get("content", []) if isinstance(block, dict)) + 1
            name, arguments = (
                ("read_file", {"path": "base.css"})
                if turn == 1 else ("write_file", {"path": "deck.md" if turn == 2 else "speech.md",
                                                  "content": "# 已完成写入" if turn == 2 else "# 从闭合上下文继续"})
            )
            response = SimpleNamespace(
                content=[{"type": "thinking", "thinking": "private test thinking", "signature": "private-test-signature"},
                         {"type": "tool_use", "id": f"work-{turn}", "name": name,
                          "input": arguments}], stop_reason="tool_use", analysis_summaries=(),
                usage=SimpleNamespace(input_tokens=1, output_tokens=1))
            return TransportResponse(response, first_byte_s=0.01)

    def build():
        runtime = SkillRuntime(spec, environment,
                               settings=ProductionSettings(skill_source=repository / "skills/mural-presenter-v1"),
                               transport=Transport(), channels=(ProviderChannel("test-only", "test"),),
                               validate_environment=False)
        kernel = PresenterKernel(spec)

        @contextmanager
        def slot(_resource, **_kwargs):
            if len(calls) >= allowed_calls:
                if interruption == "1":
                    raise AgentInterruptedError("font_integrity_failed: fixed font environment needs repair")
                raise ResourceSlotTimeout("local test queue is full")
            yield

        runtime.prepared.slots.acquire = slot
        return kernel, runtime

    # 入口/结果持久化使用真实函数；仅将它的子进程边界替换为当前隔离进程中的 Kernel。
    source = repository / "webui/inference/serve_one.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    definitions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                   and node.name in {"_run_mural_v1", "_write_result"}]
    observed = []

    class Process:
        def __init__(self, *_args, **_kwargs):
            pass

        def wait(self):
            try:
                kernel, runtime = build()
                kernel.execute(runtime.backend, include_pdf=False, include_pptx=False)
            except Exception as exc:
                observed.append(type(exc).__name__)
                return 1
            raise AssertionError("queue test unexpectedly produced a completed deck")

    namespace = {"Path": Path, "json": json, "os": os, "sys": sys, "time": time,
                 "subprocess": SimpleNamespace(Popen=Process, DEVNULL=-3),
                 "signal": SimpleNamespace(SIGINT=signal.SIGINT, SIGTERM=signal.SIGTERM,
                                           getsignal=lambda _: None, signal=lambda *_: None),
                 "_build_mural_v1_font_request": lambda _: None,
                 "_mural_v1_attachment_paths": lambda _: []}
    exec(compile(ast.Module(definitions, type_ignores=[]), str(source), "exec"), namespace)
    result = namespace["_run_mural_v1"]({
        "sample_id": "queue-test", "seed": {"query": "本地排队恢复测试"},
        "run_dir": str(root), "skill_version": "mural-presenter-v1",
        "resume_workspace": resume == "1",
        "pipeline": {"path": str(harness), "entry": "infer.py"},
    })
    return json.dumps({"result": result, "observed": observed, "provider_calls": len(calls),
                       "query": json.loads(calls[0].messages[0]["content"])["raw_user_query"] if calls else None})


@pytest.fixture
def queue_case(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "log_path", lambda _: tmp_path / "unused.log")
    return SimpleNamespace(root=tmp_path / "workspace")


def _run_entry(case, *, allowed_calls=0, resume=False, interruption=False):
    script = inspect.getsource(_queue_child) + "\nimport sys\nprint(_queue_child(*sys.argv[1:]))\n"
    completed = subprocess.run(
        [str(REPOSITORY / "webui/inference/.venv/bin/python"), "-c", script,
         str(REPOSITORY), str(case.root), str(allowed_calls), "1" if resume else "0", "1" if interruption else "0"],
        cwd=REPOSITORY, check=True, capture_output=True, text=True, timeout=60,
    )
    return json.loads(completed.stdout)


def test_first_queue_failure_is_resumable_without_any_provider_request(queue_case):
    case = queue_case
    first = _run_entry(case)
    assert first["observed"] == ["ResourceSlotTimeout"]
    assert first["result"]["exit_reason"] == "resource_queue_timeout"
    assert first["provider_calls"] == 0
    assert not (case.root / "_trace/provider-events.jsonl").exists()
    assert json.loads((case.root / "_state/orchestration.json").read_text())["tasks"] == {}
    assert not (case.root / "_state/owners.json").exists()
    info = jobs.resume_info(1, case.root)
    assert info["eligible"] and info["durable"]
    assert info["exit_reason"] == "resource_queue_timeout"
    (case.root / "_trace/agent-events.jsonl").unlink()
    assert jobs.resume_info(1, case.root)["eligible"]  # 恢复不依赖收尾派生文件。
    before = (case.root / "_trace/live-events.jsonl").read_bytes()
    identity = json.loads((case.root / "_state/workspace.json").read_text())["deck_id"]
    # 放行两个真实模型/工具回合，再次排队失败；证明安全重进原 query、未清日志，不伪称全 Deck 完成。
    later = _run_entry(case, allowed_calls=2, resume=True)
    assert later["result"]["exit_reason"] == "resource_queue_timeout"
    assert later["result"]["resume_workspace"] is True
    assert later["provider_calls"] == 2
    assert later["query"] == "本地排队恢复测试"
    assert (case.root / "deck.md").read_text() == "# 已完成写入"
    assert (case.root / "_trace/live-events.jsonl").read_bytes().startswith(before)
    assert json.loads((case.root / "_state/workspace.json").read_text())["deck_id"] == identity
    assert jobs.resume_info(1, case.root)["eligible"]  # 已有真实闭合上下文，不再冒充首次安全重进。


@pytest.mark.parametrize("defect", ["missing_run", "foreign_workspace", "empty_agent_trace",
                                    "missing_started", "unpaired_segment", "missing_workspace"])
def test_first_queue_resume_rejects_incomplete_or_mismatched_state(queue_case, defect):
    case = queue_case
    _run_entry(case)
    run = case.root / "_state/run.json"
    agent = case.root / "_trace/live-events.jsonl"
    if defect == "missing_run":
        run.unlink()
    elif defect == "foreign_workspace":
        value = json.loads(run.read_text())
        value["output_workspace"] = str(case.root.parent / "other")
        run.write_text(json.dumps(value))
    elif defect == "empty_agent_trace":
        agent.write_text("")
    elif defect == "missing_workspace":
        (case.root / "_state/workspace.json").unlink()
    else:
        events = [json.loads(line) for line in agent.read_text().splitlines()]
        if defect == "missing_started":
            events = [event for event in events if event["event_type"] != "agent_started"]
        else:
            events[-1]["segment_id"] = "another-segment"
        agent.write_text("".join(json.dumps(event) + "\n" for event in events))
    assert not jobs.resume_info(1, case.root)["eligible"]


def test_queue_pause_does_not_blame_provider_or_automatically_retry(monkeypatch):
    calls = []
    monkeypatch.setattr(jobs, "_set_status", lambda *args, **kwargs: calls.append((args, kwargs)))
    jobs._pause_recoverable_deck(1, {"exit_reason": "resource_queue_timeout"})
    assert calls[0][0] == (1, "paused_recoverable")
    assert "排队" in calls[0][1]["error"]
    assert "上游模型通道暂时不可用" not in calls[0][1]["error"]


@pytest.mark.parametrize("defect", [None, "orchestration", "owners", "skill-runtime", "missing_agent"])
def test_existing_shared_tasks_do_not_require_provider_events_but_keep_state_gates(tmp_path, monkeypatch, defect):
    from test_mural_profiles import _write_shared_v1_workspace
    _write_shared_v1_workspace(tmp_path)
    assert not (tmp_path / "_trace/provider-events.jsonl").exists()
    (tmp_path / "result.json").write_text(json.dumps({"exit_reason": "resource_queue_timeout"}))
    monkeypatch.setattr(engine, "log_path", lambda _: tmp_path / "unused.log")
    if defect == "missing_agent":
        (tmp_path / "_trace/live-events.jsonl").unlink()
    elif defect:
        path = tmp_path / f"_state/{defect}.json"
        value = json.loads(path.read_text())
        value["schema"] = "unsupported"
        path.write_text(json.dumps(value))
    info = jobs.resume_info(1, tmp_path)
    assert info["eligible"] is (defect is None)
    assert info["durable"] is (defect is None)


@pytest.mark.parametrize("resuming,latest_error,expected", [
    (True, "RuntimeError: frozen MURAL release hash mismatch: harness expected=a actual=b", True),
    (False, "RuntimeError: frozen MURAL release hash mismatch: harness expected=a actual=b", False),
    (True, "RuntimeError: another startup failure", False),
])
def test_release_rejection_does_not_orphan_durable_resume(tmp_path, monkeypatch, resuming, latest_error, expected):
    from test_mural_profiles import _write_shared_v1_workspace
    _write_shared_v1_workspace(tmp_path)
    (tmp_path / "result.json").write_text(json.dumps({
        "status": "error", "exit_reason": "runtime_error", "resume_workspace": resuming,
    }))
    log = tmp_path / "job.log"
    log.write_text("Traceback (most recent call last):\n"
                   "RuntimeError: frozen MURAL release hash mismatch: old episode\n"
                   "=== MURAL task resume ===\nTraceback (most recent call last):\n"
                   + latest_error + "\n{\"status\":\"error\"}\n")
    monkeypatch.setattr(engine, "log_path", lambda _: log)
    info = jobs.resume_info(1, tmp_path)
    assert info["eligible"] is expected
    assert info["root_exit_reason"] == "runtime_error"
    assert info["exit_reason"] == ("release_verification_failed" if expected else "runtime_error")


def test_release_rejection_pause_is_not_reported_as_provider_outage(monkeypatch):
    calls = []
    monkeypatch.setattr(jobs, "_set_status", lambda *args, **kwargs: calls.append((args, kwargs)))
    jobs._pause_recoverable_deck(1, {"exit_reason": "release_verification_failed"})
    message = calls[0][1]["error"]
    assert "发布校验" in message and "检查点" in message
    assert "上游模型通道" not in message
