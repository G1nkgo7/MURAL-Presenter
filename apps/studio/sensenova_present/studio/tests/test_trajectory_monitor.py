import json
import os
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import trajectory_monitor


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture()
def trajectory_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    root = tmp_path / "trajectory-runs"
    run = root / "smoke-v1"
    sample_id = "sample-001"
    sample = run / "work" / "batch-000" / sample_id

    _write_json(
        sample / "_trace/orchestrator/config.json",
        {
            "sample_id": sample_id,
            "task": "为设计团队制作一份高视觉冲击力的品牌复盘",
            "model": "claude-opus-5",
            "generation_preferences": {"page_count": 4},
        },
    )
    _write_json(
        sample / "_trace/subagents/image-1/config.json",
        {"label": "image-1", "role": "image"},
    )
    _write_json(
        sample / "_trace/subagents/image-1/usage.json",
        {"n_turns": 3, "sum_model_wall_seconds": 12.5},
    )
    _write_json(
        sample / "_trace/subagents/image-1/messages.json",
        [{"role": "assistant", "content": [
            {"type": "text", "text": "正在准备视觉素材。"},
            {"type": "tool_use", "name": "image_generate", "input": {"aspect_ratio": "16:9"}},
        ]}],
    )
    _write_json(
        sample / "assets/catalog.json",
        {"assets": [{"status": "ready"}, {"status": "failed"}]},
    )
    (sample / "plan").mkdir(parents=True)
    (sample / "plan/slide_01.md").write_text("# title", encoding="utf-8")
    (sample / "renders").mkdir(parents=True)
    (sample / "renders/slide_01.png").write_bytes(b"fake-png")
    (sample / "slides").mkdir(parents=True)
    (sample / "slides/slide_01.html").write_text("<title>品牌复盘</title>", encoding="utf-8")
    (sample / "present.html").write_text("<html>deck</html>", encoding="utf-8")
    (run / "run.log").write_text(
        "unrelated noise\n[sample-001] planning done\n[sample-001] image ready\n",
        encoding="utf-8",
    )
    (run / "launcher.pid").write_text(str(os.getpid()), encoding="utf-8")

    catalog = tmp_path / "queries.jsonl"
    catalog.write_text(
        "\n".join([
            json.dumps({
                "id": "query-started",
                "query": "为设计团队制作一份高视觉冲击力的品牌复盘",
                "slide_count": 4,
                "metadata": {"speaker": "品牌负责人", "audience": "设计团队"},
            }, ensure_ascii=False),
            json.dumps({
                "id": "query-pending",
                "query": "制作一份尚未开始的新品发布提案",
                "slide_count": 12,
                "metadata": {"speaker": "产品负责人", "audience": "管理层"},
            }, ensure_ascii=False),
        ]) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("STUDIO_TRAJECTORY_ROOTS", str(root))
    monkeypatch.setenv("STUDIO_TRAJECTORY_QUERY_CATALOG", str(catalog))
    return run, sample_id


def test_monitor_projects_run_sample_agents_and_artifacts(trajectory_run: tuple[Path, str]) -> None:
    run, sample_id = trajectory_run

    runs = trajectory_monitor.runs_payload()
    assert runs["runs"][0]["name"] == "smoke-v1"
    assert runs["runs"][0]["running"] is True
    assert runs["runs"][0]["sample_count"] == 1

    selected = trajectory_monitor.run_payload(run)
    assert selected["total"] == 1
    assert selected["samples"][0]["stage"] == "completed"
    assert selected["samples"][0]["ready_assets"] == 1

    detail = trajectory_monitor.sample_payload(run, sample_id)
    assert detail["task"].startswith("为设计团队")
    assert detail["agents"][1]["status"] == "complete"
    assert detail["agents"][1]["turns"] == 3
    assert detail["agents"][1]["events"][-1]["name"] == "image_generate"
    assert [line for line in detail["recent_log"] if "unrelated" in line] == []
    assert "slide_01.png" in {item["name"] for item in detail["artifacts"]}
    assert detail["pages"][0]["title"] == "品牌复盘"
    assert detail["pages"][0]["html_url"].endswith("/slides/slide_01.html")
    assert detail["presentation_url"].endswith("/files/present.html")

    virtual = trajectory_monitor.decks_payload()["decks"]
    assert len(virtual) == 2
    assert [row["status"] for row in virtual] == ["completed", "not_started"]
    deck_id = virtual[0]["id"]
    resolved_run, resolved_sample, resolved_config = trajectory_monitor._resolve_deck(deck_id)
    progress = trajectory_monitor.deck_progress_payload(resolved_run, resolved_sample, resolved_config)
    assert progress["rendered"] == [1]
    assert progress["html_ready"] is True
    assert progress["html_final"] is True
    assert progress["elapsed_s"] is not None
    assert progress["elapsed_s"] >= 0
    feed = trajectory_monitor._feed_payload(resolved_sample, complete=True)
    assert feed["agents"]["image-1"][-1]["tool"] == "image_generate"
    assert feed["agent_timings"]["image-1"]["duration_s"] is not None
    assert feed["agent_timings"]["image-1"]["finished_at"].endswith("Z")
    assert feed["overall_timing"]["duration_s"] is not None

    pending = trajectory_monitor._resolve_catalog_deck(virtual[1]["id"])
    pending_progress = trajectory_monitor.not_started_progress_payload(pending)
    assert pending_progress["status"] == "not_started"
    assert pending_progress["slides_total"] == 12


def test_incomplete_sample_is_running_only_while_a_process_is_alive(
    trajectory_run: tuple[Path, str],
) -> None:
    run, sample_id = trajectory_run
    sample = trajectory_monitor._resolve_sample(run, sample_id)
    config = trajectory_monitor._json(sample / "_trace/orchestrator/config.json", {})
    (sample / "present.html").unlink()

    live = trajectory_monitor._sample_summary(sample, config, run=run)
    assert live["status"] == "running"

    (run / "launcher.pid").unlink()
    stopped = trajectory_monitor._sample_summary(sample, config, run=run)
    assert stopped["status"] == "stopped"

    deck = trajectory_monitor.deck_progress_payload(run, sample, config)
    assert deck["status"] == "stopped"
    assert deck["elapsed_s"] is not None


def test_artifact_paths_cannot_escape_run(trajectory_run: tuple[Path, str], tmp_path: Path) -> None:
    run, _ = trajectory_run
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")

    with pytest.raises(HTTPException) as exc:
        trajectory_monitor._safe_artifact(run, "../../secret.txt")

    assert exc.value.status_code == 400

    sample = trajectory_monitor._resolve_sample(run, trajectory_run[1])
    with pytest.raises(HTTPException) as sample_exc:
        trajectory_monitor._safe_sample_file(sample, "../../../../secret.txt")
    assert sample_exc.value.status_code == 400


def test_thinking_enabled_supports_current_and_legacy_harness_configs() -> None:
    assert trajectory_monitor._thinking_enabled({
        "thinking": {"type": "adaptive", "effort": "high"},
        "requested_thinking": True,
        "effective_thinking": True,
    }) is True
    assert trajectory_monitor._thinking_enabled({"reasoning_effort": "medium"}) is True
    assert trajectory_monitor._thinking_enabled({
        "thinking": {"type": "adaptive", "effort": "high"},
        "effective_thinking": False,
    }) is False


def test_search_filters_samples(trajectory_run: tuple[Path, str]) -> None:
    run, _ = trajectory_run
    assert trajectory_monitor.run_payload(run, search="品牌")["total"] == 1
    assert trajectory_monitor.run_payload(run, search="不存在")["total"] == 0
    paged = trajectory_monitor.run_payload(run, offset=1, limit=1)
    assert paged["total"] == 1
    assert paged["samples"] == []


def test_livefeed_reads_in_progress_nova_orchestrator_attempts(
    trajectory_run: tuple[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, sample_id = trajectory_run
    sample = trajectory_monitor._resolve_sample(run, sample_id)
    config = trajectory_monitor._json(sample / "_trace/orchestrator/config.json", {})
    nova_root = tmp_path / "external-nova-raw"
    monkeypatch.setenv("STUDIO_TRAJECTORY_NOVA_RAW_ROOTS", str(nova_root))
    task_dir = nova_root / sample.parent.name / "tasks/sample-001-orch-a1"
    _write_json(
        task_dir / "task_meta.json",
        {
            "sample_id": sample_id,
            "role": "orchestrator",
            "label": "orch",
            "created_at_epoch": 100.0,
        },
    )
    first_response = task_dir / "attempts/main/call-1/attempt-1/response.json"
    second_response = task_dir / "attempts/main/call-2/attempt-1/response.json"
    _write_json(first_response, {"content": [
        {"type": "tool_use", "name": "read_file", "input": {"path": "skills/mural-presenter/SKILL.md"}},
    ]})
    _write_json(second_response, {"content": [
        {"type": "text", "text": "内部过程不展示</think公开的阶段结论"},
        {"type": "tool_use", "name": "delegate_task", "input": {"label": "research", "goal": "核验公开事实\n不要编造"}},
    ]})
    attempts = [
        {
            "selected": True,
            "status": "ok",
            "recorded_at_epoch": 101.0,
            "response": first_response.relative_to(task_dir).as_posix(),
        },
        {
            "selected": True,
            "status": "ok",
            "recorded_at_epoch": 102.0,
            "response": second_response.relative_to(task_dir).as_posix(),
        },
    ]
    (task_dir / "attempts.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in attempts) + "\n",
        encoding="utf-8",
    )

    feed = trajectory_monitor._feed_payload(
        sample,
        complete=False,
        run=run,
        config=config,
    )

    assert [event["tool"] for event in feed["agents"]["orch"] if event["k"] == "tool"] == ["read_file", "delegate_task"]
    assert feed["agents"]["orch"][0]["hint"] == "skills/mural-presenter/SKILL.md"
    assert feed["agents"]["orch"][1]["s"] == "公开的阶段结论"
    assert feed["agents"]["orch"][2]["hint"].startswith("research: 核验公开事实")
    assert feed["agents"]["orch"][0]["seq"] < feed["agents"]["orch"][2]["seq"]


def test_monitor_frontend_renders_overall_and_per_agent_wall_time() -> None:
    root = Path(__file__).resolve().parents[1]
    script = (root / "static/app.js").read_text(encoding="utf-8")
    css = (root / "static/app.css").read_text(encoding="utf-8")
    assert "function orchestrationTimingMarkup" in script
    assert 'class="orchestration-timing-card"' in script
    assert "payload.agent_timings || {}" in script
    assert "payload.overall_timing || null" in script
    assert ".orchestration-timing-agent" in css
    assert ".orch-duration" in css
    assert 'type: "orchestrator-action"' in script
    assert 'entry.type === "orchestrator-action"' in script
