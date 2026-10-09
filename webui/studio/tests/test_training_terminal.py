"""真实结果契约经过 runner、终态分类和前端投影；不请求 Provider。"""
import ast
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from app import jobs, trace
from workspace_fixture import write_workspace_manifest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize('returncode,error_type,expected', [
    (1, 'RepeatedToolFailureError', 'tool_circuit_open'),
    (-9, 'RepeatedToolFailureError', 'process_interrupted'),
    (1, 'AgentInterruptedError', 'process_interrupted'),
])
def test_runner_distinguishes_tool_circuit_from_real_interruption(tmp_path, returncode, error_type, expected):
    source = ROOT / 'webui/inference/serve_one.py'
    functions = [node for node in ast.parse(source.read_text()).body if isinstance(node, ast.FunctionDef)
        and node.name in {'_run_mural_v1', '_write_result'}]
    def wait():
        state = tmp_path/'_state'
        state.mkdir()
        (state/'skill-runtime.json').write_text(json.dumps({'status': 'failed', 'agent_failed': {
            'error_type': error_type, 'error_summary': 'finalize scratch audit failed',
            'last_failed_tool': 'terminal', 'last_failed_error_code': 'skill_command_failed'}}))
        return returncode
    namespace = dict(Path=Path, json=json, os=os, sys=sys, time=time, signal=signal,
        subprocess=SimpleNamespace(DEVNULL=subprocess.DEVNULL, Popen=lambda *a, **kw: SimpleNamespace(wait=wait)),
        _build_mural_v1_font_request=lambda seed: None, _mural_v1_attachment_paths=lambda seed: [])
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), 'exec'), namespace)
    job = {'sample_id': 'test', 'seed': {'query': 'input'}, 'run_dir': str(tmp_path),
        'pipeline': {'path': str(ROOT/'harnesses/mural-presenter-v1'), 'entry': 'infer.py'},
        'skill': {'name': 'mural-presenter-v1'}, 'skill_version': 'mural-presenter-v1'}
    result = namespace['_run_mural_v1'](job)
    assert result['exit_reason'] == expected
    assert 'finalize scratch audit failed' in result['error']


@pytest.mark.parametrize("stale,returncode", [(False, 1), (True, 1), (False, 0)])
@pytest.mark.parametrize("pending", [False, True])
def test_runner_preserves_fresh_training_rejection_but_not_previous_result(tmp_path, stale, returncode, pending):
    source = ROOT / "webui/inference/serve_one.py"
    tree = ast.parse(source.read_text())
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
        and node.name in {"_run_mural_v1", "_write_result"}]
    result_path = tmp_path / "result.json"
    result = {"schema": "mural.result.v1", "status": "completed", "artifact_status": "complete",
        "page_count": 3, "exit_reason": "training_rejected", "training_admission": "rejected",
        "training_rejection_reasons": ["unresolved_valid_revise"], "delivery_manifest": "delivery/manifest.json"}
    if pending:
        result.update(status="error", exit_reason="critic_incomplete", training_admission="pending",
            training_rejection_reasons=["critic_incomplete"], error="Critic incomplete: P3 empty_critic_response")
    if stale:
        result_path.write_text(json.dumps(result))
        os.utime(result_path, (1, 1))
    def wait():
        if not stale:
            result_path.write_text(json.dumps(result))
            (tmp_path / "_state").mkdir(exist_ok=True)
            (tmp_path / "_state/skill-ref.json").write_text(json.dumps({
                "schema": "mural.skill-ref.v1", "skill_name": "original-pinned-skill"}))
        return returncode
    namespace = dict(Path=Path, json=json, os=os, sys=sys, time=time, signal=signal,
        subprocess=SimpleNamespace(DEVNULL=subprocess.DEVNULL, Popen=lambda *a, **kw: SimpleNamespace(wait=wait)),
        _build_mural_v1_font_request=lambda seed: None, _mural_v1_attachment_paths=lambda seed: [])
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)
    job = {"sample_id": "test", "seed": {"query": "input"}, "run_dir": str(tmp_path),
        "pipeline": {"path": str(ROOT / "harnesses/mural-presenter-v1"), "entry": "infer.py"},
        "skill": {"name": "descriptor-name"}, "skill_version": "mural-presenter-v1",
        "selected_skill_name": "missing-new-source", "resume_workspace": True}
    rec = namespace["_run_mural_v1"](job)
    if stale:
        assert rec["status"] == "error" and "exited with code 1" in rec["error"]
    else:
        assert rec["status"] == result["status"] and rec["training_admission"] == result["training_admission"]
        assert rec["training_rejection_reasons"] == result["training_rejection_reasons"]
        if pending:
            assert rec["error"] == result["error"] and rec["exit_reason"] == "critic_incomplete"
        assert rec["n_slides"] == 3 and rec["engine_returncode"] == returncode
        assert rec["skill_name"] == "original-pinned-skill"
        assert rec["selected_skill_name"] == "missing-new-source"


def test_incomplete_critic_offers_same_workspace_resume_not_false_completion(tmp_path, monkeypatch):
    from test_mural_profiles import _write_shared_v1_workspace
    _write_shared_v1_workspace(tmp_path)
    (tmp_path / "result.json").write_text(json.dumps({"status": "error", "exit_reason": "critic_incomplete",
        "training_admission": "pending", "error": "P3 empty_critic_response"}))
    monkeypatch.setattr(jobs.engine, "log_path", lambda _: tmp_path / "missing.log")
    assert jobs.resume_info(99998, tmp_path)["eligible"]
    monkeypatch.setattr(jobs, "_load_deck", lambda _: {"seed_json": "{}"})
    statuses = []
    monkeypatch.setattr(jobs, "_set_status", lambda _id, status, **kw: statuses.append((status, kw)))
    jobs._finalize(99998, tmp_path, rc=1)
    assert statuses[0][0] == "paused_recoverable"
    assert "补齐缺失评审" in statuses[0][1]["error"]


@pytest.mark.parametrize("mode", ["inference", "synthesis"])
def test_runner_defers_all_download_formats_only_for_normal_inference(tmp_path, mode):
    source = ROOT / "webui/inference/serve_one.py"
    definitions = [node for node in ast.parse(source.read_text()).body
        if isinstance(node, ast.FunctionDef) and node.name in {"_run_mural_v1", "_write_result"}]
    commands = []
    def start(command, **kwargs):
        commands.append(command)
        def wait():
            (tmp_path / "result.json").write_text(json.dumps({
                "schema": "mural.result.v1", "status": "completed", "page_count": 1}))
            (tmp_path / "_state").mkdir(exist_ok=True)
            (tmp_path / "_state/skill-ref.json").write_text(json.dumps({
                "schema": "mural.skill-ref.v1", "skill_name": "mural-presenter-v1"}))
            return 0
        return SimpleNamespace(wait=wait)
    namespace = dict(Path=Path, json=json, os=os, sys=sys, time=time, signal=signal,
        subprocess=SimpleNamespace(DEVNULL=subprocess.DEVNULL, Popen=start),
        _build_mural_v1_font_request=lambda seed: None, _mural_v1_attachment_paths=lambda seed: [])
    exec(compile(ast.Module(definitions, type_ignores=[]), str(source), "exec"), namespace)
    result = namespace["_run_mural_v1"]({
        "sample_id": "deferred", "seed": {"query": "input", "run_mode": mode}, "run_dir": str(tmp_path),
        "pipeline": {"path": str(ROOT / "harnesses/mural-presenter-v1"), "entry": "infer.py"}})
    assert result["status"] == "completed"
    assert len(commands) == 1
    flags = {"--no-pdf", "--no-pptx", "--no-zip"}
    assert flags <= set(commands[0])


def test_training_rejection_is_not_provider_pause_and_remains_visible(tmp_path, monkeypatch):
    write_workspace_manifest(tmp_path)
    result = {"status": "completed", "artifact_status": "complete", "exit_reason": "training_rejected",
        "training_admission": "rejected", "training_rejection_reasons": ["artifact_snapshot_invalid=material.md"]}
    (tmp_path / "result.json").write_text(json.dumps(result))
    # 即使旧运行存在可恢复 checkpoint，明确的终态也不应被改成 Provider 暂停。
    monkeypatch.setattr(jobs, "resume_info", lambda *a: {"eligible": True})
    monkeypatch.setattr(jobs, "_load_deck", lambda _: {"seed_json": "{}"})
    monkeypatch.setattr(jobs, "_ensure_static_delivery", lambda *a: None)
    monkeypatch.setattr(jobs, "_settle_waiting_revisions", lambda *a: None)
    statuses = []
    monkeypatch.setattr(jobs, "_set_status", lambda _id, status, **kw: statuses.append(status))
    jobs._finalize(99999, tmp_path, rc=1)
    assert statuses == ["completed"]
    progress = trace.snapshot(str(tmp_path), status="completed")
    assert progress["training_admission"] == "rejected"
    assert progress["training_rejection_reasons"] == result["training_rejection_reasons"]
    script = (ROOT / "webui/studio/static/app.js").read_text()
    tree = script[script.index("function renderStatus("):script.index("function currentElapsedSeconds(")]
    setup = "const host={dataset:{},innerHTML:''}; const $=()=>host; const syncTaskComposerState=()=>{}; const PHASE_LABEL={done:'完成'}; const STATUS_LABEL={};"
    process = subprocess.run(["node", "-e", setup + tree + "\nrenderStatus(" + json.dumps(progress) + ",'done'); console.log(host.innerHTML);"],
        capture_output=True, text=True, check=True)
    assert "交付完成 · 训练未准入" in process.stdout
