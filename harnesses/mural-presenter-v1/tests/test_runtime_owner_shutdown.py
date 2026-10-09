"""Orchestrator 失败后，正在闭合的 Owner 仍能使用 Critic。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
from PIL import Image
from geometry_fixture import geometry, write_geometry

from infer import PresenterKernel
from test_orchestration_manager import drain
from orchestration.manager import OrchestrationManager
from qc.extrinsic import CriticClient, ExternalVisualCritic
from qc.review_hook import InspectionClosed
from runtime.agent import AgentRuntime
from runtime.config import RunSpec
from runtime.production import ProductionSettings
from runtime.session import ProviderChannel
from runtime.skill_runtime import SkillRuntime
from state.workspace import WorkspaceState, file_sha256


SKILL = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1"


def test_failed_orchestrator_drains_owner_before_closing_critic(tmp_path, monkeypatch):
    write_geometry(tmp_path, geometry(width_px=1600, height_px=900))
    spec = RunSpec.from_inputs(raw_user_query="one page", output_workspace=tmp_path,
                               environment={"MODEL_API_FORMAT": "openai", "MODEL": "fake"})
    runtime = SkillRuntime(spec, {}, settings=ProductionSettings(skill_source=SKILL),
                           transport=SimpleNamespace(), channels=(ProviderChannel("fake", "fake"),),
                           validate_environment=False)
    kernel = PresenterKernel(spec)
    entered, draining = threading.Event(), threading.Event()
    checked = []
    submitted = []
    png = tmp_path / "renders/slide_01.png"

    def coordinator(_services):
        runtime._critic_executor = ThreadPoolExecutor(max_workers=1)
        client = CriticClient(invoker=lambda *_: checked.append("completed") or {"body": "检查完成"},
                              critic_model="fake")
        runtime._critic_coordinator = ExternalVisualCritic(
            client=client, executor=runtime._critic_executor)

    monkeypatch.setattr(runtime, "_build_review_coordinator", coordinator)
    close = kernel.orchestration.close

    def drain(*, wait=True):
        draining.set()
        close(wait=wait)

    monkeypatch.setattr(kernel.orchestration, "close", drain)

    def owner(task, _manager):
        entered.set()
        assert draining.wait(5), "Owner was never drained"
        Image.new("RGB", (8, 8), "navy").save(png)
        runtime._critic_coordinator.review(InspectionClosed(
            task.owner_id, "slide", "inspection-last", "current-html", file_sha256(png),
            "renders/slide_01.png", str(tmp_path), page=1))
        runtime._critic_coordinator.settle_pending()
        return {"status": "completed"}

    def run(_self, **kwargs):
        assert kwargs["role"] == "orchestrator"
        kernel.workspace.register_pages([1])
        kernel.workspace.paths.page_html(1).write_text('<section data-slide="01"></section>', encoding="utf-8")
        kernel.workspace.register_scaffold(1)
        kernel.orchestration.register_runner("slide", owner)
        submitted.append(kernel.orchestration.submit_tasks([{"task_id": "page", "role": "slide", "pages": [1]}])["tasks"][0])
        assert entered.wait(5)
        raise RuntimeError("scripted orchestrator provider failure")

    monkeypatch.setattr(AgentRuntime, "run", run)
    with pytest.raises(RuntimeError, match="scripted orchestrator provider failure"):
        kernel.execute(runtime.backend, include_pdf=False, include_pptx=False)
    task = kernel.orchestration.task(submitted[0]["task_id"])
    assert task.status.value == "succeeded", task.error
    assert checked == ["completed"]
    with pytest.raises(RuntimeError, match="shutdown"):
        runtime._critic_executor.submit(lambda: None)
    assert (tmp_path / "_trace/tool-events.jsonl").is_file()


def test_draining_keeps_newly_unblocked_tasks_pending_for_resume(tmp_path):
    WorkspaceState(tmp_path)
    manager = OrchestrationManager(tmp_path, deck_id="test", max_workers=1)
    entered, finish = threading.Event(), threading.Event()
    started = []

    def image(*_):
        entered.set()
        assert finish.wait(5)
        return "ready"

    manager.register_runner("image", image)
    manager.register_runner("slide", lambda task, _: started.append(task.task_id))
    source = manager.submit_tasks([{"task_id": "image", "role": "image"}])["tasks"][0]
    dependent = manager.submit_tasks([{"task_id": "page", "role": "slide", "pages": [1], "depends_on": [source["task_id"]]}])["tasks"][0]
    assert entered.wait(5)
    closer = threading.Thread(target=manager.close)
    closer.start()
    with manager._condition:
        assert manager._condition.wait_for(lambda: manager._closed, timeout=5)
    finish.set()
    closer.join(timeout=5)
    assert not closer.is_alive()
    assert manager.task(source["task_id"]).status.value == "succeeded"
    assert manager.task(dependent["task_id"]).status.value == "waiting_dependency"
    assert not started
    recovered = OrchestrationManager(tmp_path, deck_id="test", max_workers=1)
    try:
        recovered.register_runner("slide", lambda task, _: started.append(task.task_id))
        drain(recovered)
        assert recovered.task("page").status.value == "succeeded"
        assert started == [dependent["task_id"]]
    finally:
        recovered.close()
