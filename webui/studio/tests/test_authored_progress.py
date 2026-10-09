"""实际 Harness 持久状态不含 lifecycle；进度仅确认当前已提交源码。"""
import json
import os
from pathlib import Path
import subprocess

import pytest

from app import engine, trace


@pytest.fixture
def harness_workspace(tmp_path):
    root = Path(__file__).resolve().parents[3]
    harness = root / "harnesses/mural-presenter-v1"
    workspace = tmp_path / "deck"
    script = '''
import json,sys
from pathlib import Path
from PIL import Image
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, str(Path(sys.argv[1]) / "tests"))
from geometry_fixture import geometry, write_geometry
from state.artifacts import ArtifactStore
from state.workspace import WorkspaceState
root = Path(sys.argv[2])
state = WorkspaceState(root)
write_geometry(root, geometry(width_px=800, height_px=600))
store = ArtifactStore(root, workspace_state=state)
state.register_pages([1, 2, 3, 4, 5])
for number in range(1, 6):
    state.paths.page_html(number).write_text("<section></section>")
    state.register_scaffold(number)
for number in (1, 3, 4):
    path = state.paths.page_html(number)
    path.write_text(f"<section>Authored page {number}</section>")
    name = f"page-html-{number:02d}"
    store.declare(artifact_id=name, kind="page_html", path=path, owner_id="slide", page=number)
    store.commit(owner_id="slide", artifact_ids=[name], source_hashes={})
def renderer(html, png, canvas):
    Image.new("RGB", (800, 600), "white").save(png)
    return {"geometry": canvas, "loaded_resources": [], "failed_resources": []}
store.render_pages([3], renderer)
store.record_inspection(3, inspection_id="inspected-page-3")
state.mark_ready(3)
state.paths.page_html(4).write_text("<section>Uncommitted changed source</section>")
state.paths.page_html(5).write_text("<section>Unregistered arbitrary source</section>")
Image.new("RGB", (800, 600), "white").save(state.paths.page_png(5))
print(json.dumps({row.page: row.lifecycle.value for row in state.pages()}))
'''
    result = subprocess.run([engine._engine_python(), "-c", script, str(harness), str(workspace)],
        cwd=root, env={**os.environ, "MURAL_STUDIO_DATA_DIR": str(tmp_path / "shared-data"),
            "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(tmp_path)},
        check=True, capture_output=True, text=True, timeout=60)
    assert json.loads(result.stdout) == {"1": "authored", "2": "scaffolded", "3": "ready",
                                        "4": "scaffolded", "5": "scaffolded"}
    return workspace


def test_actual_harness_state_projects_current_authored_and_ready_pages(harness_workspace):
    state = json.loads((harness_workspace / "_state/workspace.json").read_text())
    assert all("lifecycle" not in record for record in state["pages"].values())
    for status in ("running", "completed"):
        progress = trace.snapshot(str(harness_workspace), seed={"slide_count": 5}, status=status)
        assert progress["authored"] == [1, 3]
        assert progress["slides_authored"] == 2
        assert progress["slides_scaffolded"] == 3
        assert set(progress["atimes"]) == {"1", "3"}
        assert progress["rendered"] == [3, 5]  # PNG 存在不能把未提交页面变为 authored。


def test_progress_rechecks_current_bytes_without_stat_cache(harness_workspace):
    assert trace.snapshot(str(harness_workspace))["authored"] == [1, 3]
    html = harness_workspace / "slides/slide_01.html"
    before = html.stat()
    html.write_text(html.read_text().replace("Authored", "Modified"))
    os.utime(html, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert html.stat().st_size == before.st_size
    assert trace.snapshot(str(harness_workspace))["authored"] == [3]


def test_retired_or_missing_sources_are_not_authored(harness_workspace):
    state_path = harness_workspace / "_state/workspace.json"
    state = json.loads(state_path.read_text())
    state["pages"]["1"]["retired"] = True
    state_path.write_text(json.dumps(state))
    (harness_workspace / "slides/slide_03.html").unlink()
    assert trace.snapshot(str(harness_workspace))["authored"] == []
