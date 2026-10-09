from __future__ import annotations

from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
import json
import os
import subprocess
import sys
import pytest

from PIL import Image

from export.training import TrainingSegmentInput
from infer import RuntimeServices, run_presenter
from runtime.agent import AgentRuntime
from runtime.config import RunMode, RunSpec
from geometry_fixture import geometry, write_geometry
from runtime.recovery import SegmentCheckpoint
from runtime.session import ProviderChannel, TransportResponse
from runtime.tools import ToolCall, ToolContext


def _run_spec(root: Path, mode: RunMode) -> RunSpec:
    return RunSpec.from_inputs(
        raw_user_query="同一输入必须走同一生成内核。",
        output_workspace=root,
        run_mode=mode,
        environment={
            "MODEL_API_FORMAT": "openai",
            "MODEL": "test-model",
            "MODEL_BASE_URL": "https://example.test",
        },
    )


def _renderer(_html: Path, png: Path, canvas: dict):
    assert (canvas["width_px"], canvas["height_px"]) == (1600, 900)
    png.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1600, 900), (30, 90, 160)).save(png)
    return {"geometry": canvas, "loaded_resources": [], "failed_resources": []}


def _analyzer(_path: Path, _question: str, _scope: str, canvas: dict):
    assert canvas["aspect_ratio"] == 16 / 9
    return {"summary": "像素检查完成"}


def _backend(services: RuntimeServices):
    orchestrator = services.orchestrator_tool_context()
    services.workspace.paths.deck.write_text("# 共享内核\n## Brief\n- language: zh\n")
    (services.workspace.paths.root / "briefs").mkdir(exist_ok=True)
    (services.workspace.paths.root / "briefs/slide_01.md").write_text(
        "# P01 — 共享内核\n- page_type: cover\n- job: 介绍共享生成路径\n"
        "- must_show: 共享内核\n- medium: typography\n- speaker_note: 两种模式复用相同的制作与核验路径。\n")
    write_geometry(services.workspace.paths.root, geometry(width_px=1600, height_px=900))
    services.workspace.register_pages([1])
    services.owners.claim_orchestrator_page(1)
    html_path = services.workspace.paths.page_html(1)
    html_path.write_text(
        "<!doctype html><html><head>"
        '<link rel="stylesheet" href="../base.css">'
        "</head><body><main class=\"mural-slide\">共享内核</main></body></html>",
        encoding="utf-8",
    )
    services.workspace.register_scaffold(1)
    services.artifacts.declare(
        artifact_id="page-html-01",
        kind="page_html",
        path=html_path,
        owner_id="orchestrator",
        page=1,
    )
    committed = services.tools.execute(
        orchestrator,
        ToolCall(
            "commit-1",
            "artifact_commit",
            {"artifact_ids": ["page-html-01"], "source_hashes": {}},
        ),
    )
    assert committed.ok
    rendered = services.tools.execute(
        orchestrator,
        ToolCall("render-1", "render", {"pages": [1]}),
    )
    assert rendered.ok
    class ConsumeImage:
        calls = 0

        def complete(self, channel, request, *, total_timeout_s):
            self.calls += 1
            if self.calls == 1:
                block = SimpleNamespace(type="tool_use", id="vision-1", name="inspect",
                    input={"page": [1], "question": "检查确定性像素问题"})
            else:
                assert self.calls == 2
                assert services.workspace.page(1).lifecycle.value == "rendered"
                result = request.messages[-1]["content"][0]
                assert result["type"] == "tool_result" and not result.get("is_error")
                assert [part["type"] for part in result["content"]] == ["text", "image"]
                block = SimpleNamespace(type="text", text="已消费当前页面像素")
            return TransportResponse(SimpleNamespace(content=[block], analysis_summaries=(),
                stop_reason="tool_use" if self.calls == 1 else "end_turn",
                usage=SimpleNamespace(input_tokens=1, output_tokens=1,
                    cache_read_input_tokens=0, cache_creation_input_tokens=0)), first_byte_s=.01)

    transport = ConsumeImage()
    AgentRuntime(settings=services.run_spec.provider,
        channels=(ProviderChannel("fixture", "scripted"),), transport=transport, tools=services.tools).run(
            agent_id="orchestrator", role="orchestrator", system="共享内核视觉检查测试",
            tool_context=replace(orchestrator, metadata={"workspace_root": str(services.workspace.paths.root)}),
            initial_messages=({"role": "user", "content": "检查页面"},),
            max_turns=2, max_tokens=128, thinking=False, effort="low")
    assert transport.calls == 2
    assert services.workspace.page(1).lifecycle.value == "inspected"
    assert services.complete_deck()["status"] == "ready"
    assert services.workspace.page(1).lifecycle.value == "ready"
    services.capture_training_segment("orchestrator", "orchestrator", SegmentCheckpoint.initial(
        ({"role": "user", "content": "共享轨迹"},), segment_id="segment-shared"),
        artifact_paths=("slides/slide_01.html", "renders/slide_01.png"))
    return {"generation_marker": "same-path"}


def test_inference_and_synthesis_share_generation_behavior(tmp_path: Path) -> None:
    inference = run_presenter(
        _run_spec(tmp_path / "inference", RunMode.INFERENCE),
        _backend,
        renderer=_renderer,
        vision_analyzer=_analyzer,
        include_pdf=False,
        include_pptx=False,
    )
    synthesis = run_presenter(
        _run_spec(tmp_path / "synthesis", RunMode.SYNTHESIS),
        _backend,
        renderer=_renderer,
        vision_analyzer=_analyzer,
        include_pdf=False,
        include_pptx=False,
    )

    assert inference.backend_result == synthesis.backend_result == {
        "generation_marker": "same-path"
    }
    assert inference.delivery.canvas == synthesis.delivery.canvas
    assert inference.delivery.pages[0]["html_sha256"] == synthesis.delivery.pages[0][
        "html_sha256"
    ]
    assert inference.delivery.pages[0]["png_sha256"] == synthesis.delivery.pages[0][
        "png_sha256"
    ]
    assert inference.training_manifest is None
    assert synthesis.training_manifest is None
    assert not (synthesis.workspace / "_trace/training").exists()
    assert list((synthesis.workspace / "_trace/raw/segments").glob("*.json"))


def test_pending_critic_is_execution_incomplete_without_online_training_admission(tmp_path, monkeypatch):
    from export.training import TrainingExporter
    from runtime.skill_runtime import SkillRuntimeError
    monkeypatch.setattr(TrainingExporter, "export", lambda *_a, **_kw: pytest.fail("no online training export"))
    def backend(services):
        return {**_backend(services), "external_visual_critic": {
            "effective": True, "critic_incomplete": True}}
    with pytest.raises(SkillRuntimeError, match="Critic incomplete"):
        run_presenter(_run_spec(tmp_path, RunMode.SYNTHESIS), backend,
            renderer=_renderer, vision_analyzer=_analyzer, include_pdf=False, include_pptx=False)
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["status"] == "error" and result["artifact_status"] == "complete"
    assert result["exit_reason"] == "critic_incomplete"
    assert "training_admission" not in result and "training_manifest" not in result
    assert not (tmp_path / "_trace/training").exists()
    assert result["generation"]["status"] == "complete"
    assert not (tmp_path / "delivery").exists()


@pytest.mark.parametrize("mode", [RunMode.INFERENCE, RunMode.SYNTHESIS])
def test_shared_entry_can_defer_binary_exports_without_skipping_validation(tmp_path, mode):
    outcome = run_presenter(_run_spec(tmp_path, mode), _backend,
        renderer=_renderer, vision_analyzer=_analyzer,
        include_pdf=False, include_pptx=False, include_zip=False)
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["status"] == "completed"
    assert (tmp_path / "present.html").is_file()
    assert outcome.delivery.pages[0]["png_sha256"]
    assert not (tmp_path / "delivery").exists()
    assert not list((tmp_path / "delivery").glob("*.pdf"))
    assert not list((tmp_path / "delivery").glob("*.pptx"))
    assert not list((tmp_path / "delivery").glob("*.zip"))
    assert outcome.training_manifest is None
    assert not (tmp_path / "_trace/training").exists()


def test_kernel_resume_restores_completed_training_segments_once(tmp_path):
    from infer import PresenterKernel
    script = """
import os, sys
from pathlib import Path
from test_shared_entry import _run_spec
from infer import PresenterKernel
from runtime.config import RunMode
from runtime.recovery import SegmentCheckpoint
kernel = PresenterKernel(_run_spec(Path(sys.argv[1]), RunMode.SYNTHESIS))
closed = SegmentCheckpoint.initial([{'role':'user','content':'real closed input'}], segment_id='persisted')
kernel.services.capture_training_segment('finished-material', 'material', closed)
os._exit(27)
"""
    child = subprocess.run([sys.executable, "-c", script, str(tmp_path)], timeout=30,
        env={**os.environ, "PYTHONPATH": os.pathsep.join([str(Path(__file__).parent), str(Path(__file__).parents[1])])})
    assert child.returncode == 27
    closed = SegmentCheckpoint.initial([{"role": "user", "content": "real closed input"}], segment_id="persisted")
    resumed = PresenterKernel(_run_spec(tmp_path, RunMode.SYNTHESIS))
    try:
        assert len(resumed.services.training_snapshot()) == 1
        resumed.services.capture_training_segment("finished-material", "material", closed)
        assert len(resumed.services.training_snapshot()) == 1
    finally:
        resumed.orchestration.close(wait=True)


def test_kernel_revision_retry_identity_is_json_safe_and_stable(tmp_path):
    from dataclasses import replace
    from infer import PresenterKernel
    base = _run_spec(tmp_path, RunMode.SYNTHESIS)
    revision = {"revision_no": 1, "instruction": "移动页面", "in_place": True}
    keys = []
    for request in (None, revision, dict(reversed(list(revision.items()))),
                    {**revision, "revision_no": 2}):
        kernel = PresenterKernel(replace(base, revision_request=request))
        try:
            keys.append(kernel.orchestration.request_key)
        finally:
            kernel.orchestration.close(wait=True)
    assert keys[1] == keys[2]
    assert len(set(keys)) == 3
