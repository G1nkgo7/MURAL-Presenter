"""按需下载路由与历史交付读取器、独立作者工程的边界。"""
import asyncio
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import zipfile

import pytest

from app import main
from app.workspace_layout import WorkspaceLayout


@pytest.fixture
def download_case(tmp_path, monkeypatch):
    mapping = {
        "deck": "deck.md",
        "knowledge": "knowledge.md",
        "speech": "speech.md", "base_css": "base.css", "present": "present.html",
        "result": "result.json", "assets": "assets", "asset_catalog": "assets/catalog.md",
        "slides": "slides", "slide_pattern": "slides/slide_{page:02d}.html",
        "renders": "renders", "render_pattern": "renders/slide_{page:02d}.png",
        "delivery": "delivery", "delivery_manifest": "delivery/manifest.json",
        "portable_zip": "delivery/presentation.zip",
        "runtime": "_runtime", "runtime_tmp": "_runtime/tmp",
    }
    (tmp_path / "_state").mkdir()
    state = tmp_path / "_state/workspace.json"
    state.write_text(json.dumps({
        "layout_version": 2, "paths_schema": "mural.workspace-paths.v2", "paths": mapping,
    }))
    (tmp_path / "delivery").mkdir()
    archive = tmp_path / "delivery/presentation.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("present.html", "<main>verified delivery</main>")
        handle.writestr("assets/fonts/example.woff2", b"frozen-font")
    payload = archive.read_bytes()
    manifest = tmp_path / "delivery/manifest.json"
    manifest.write_text(json.dumps({
        "schema": "mural.delivery.v1", "status": "complete",
        "outputs": {"portable_zip": {
            "path": "delivery/presentation.zip", "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }},
    }))
    (tmp_path / "deck.md").write_text("# Author plan")
    (tmp_path / "knowledge.md").write_text("# Author evidence")
    (tmp_path / "present.html").write_text("<main>author player</main>")
    (tmp_path / "_trace").mkdir()
    (tmp_path / "_trace/private.json").write_text("not an export")
    con = sqlite3.connect(":memory:", check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE decks (id INTEGER, user_id INTEGER, status TEXT, "
                "skill_version TEXT, run_dir TEXT, seed_json TEXT)")
    con.execute("INSERT INTO decks VALUES (1, 7, 'completed', 'mural-presenter-v1', ?, '{}')",
                (str(tmp_path),))
    con.commit()
    overrides = main.app.dependency_overrides.copy()
    main.app.dependency_overrides[main.get_db] = lambda: con
    main.app.dependency_overrides[main.current_user] = lambda: {"id": 7}

    def forbidden(*args, **kwargs):
        pytest.fail("v1 download must not finalize, stage fonts, or rebuild delivery")

    monkeypatch.setattr(main.jobs, "_ensure_static_delivery", forbidden)
    monkeypatch.setattr(main, "DATA_DIR", tmp_path / "studio-data")
    try:
        yield {"root": tmp_path, "state": state, "manifest": manifest,
               "archive": archive, "bytes": payload, "con": con}
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(overrides)
        con.close()


def request(path, *, method="GET"):
    async def run():
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await main.app({
            "type": "http", "asgi": {"version": "3.0"}, "method": method,
            "path": path, "raw_path": path.encode(), "root_path": "",
            "scheme": "http", "query_string": b"", "headers": [],
            "client": ("test", 1), "server": ("test", 80), "http_version": "1.1",
        }, receive, send)
        start = next(message for message in messages if message["type"] == "http.response.start")
        body = b"".join(message.get("body", b"") for message in messages
                        if message["type"] == "http.response.body")
        return start["status"], dict(start["headers"]), body

    return asyncio.run(run())


def test_v1_download_returns_only_the_on_demand_exporters_verified_bytes(download_case, monkeypatch):
    monkeypatch.setattr(main.tempfile, "TemporaryDirectory",
                        lambda **kwargs: pytest.fail("portable download must not stage files"))
    monkeypatch.setattr(main.jobs, "export_delivery", lambda row, selected: {
        "path": download_case["archive"], "media_type": "application/zip", "filename": "presentation_1.zip",
    } if selected == "zip" else pytest.fail("wrong format"))
    status, headers, body = request("/api/decks/1/download")
    assert status == 200
    assert body == download_case["bytes"]
    assert headers[b"content-type"] == b"application/zip"
    assert b"presentation_1.zip" in headers[b"content-disposition"]
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        assert "deck.md" not in archive.namelist()
        assert "assets/fonts/example.woff2" in archive.namelist()


@pytest.mark.parametrize("endpoint", ["download", "project-download"])
@pytest.mark.parametrize("status", ["running", "failed", "rejected", "interrupted"])
def test_download_requires_completed_status(download_case, endpoint, status):
    download_case["con"].execute("UPDATE decks SET status = ?", (status,))
    assert request(f"/api/decks/1/{endpoint}")[0] == 409


@pytest.mark.parametrize("endpoint", ["download", "project-download"])
def test_download_checks_owner_and_authentication(download_case, endpoint):
    main.app.dependency_overrides[main.current_user] = lambda: {"id": 8}
    assert request(f"/api/decks/1/{endpoint}")[0] == 404
    main.app.dependency_overrides[main.current_user] = lambda: None
    assert request(f"/api/decks/1/{endpoint}")[0] == 401


@pytest.mark.parametrize("defect", [
    "missing_layout", "missing_manifest", "missing_zip", "empty_zip", "changed_zip",
    "invalid_json", "bad_schema", "incomplete", "missing_record", "wrong_path", "wrong_size",
])
def test_historical_portable_reader_rejects_invalid_delivery_without_rebuilding(download_case, defect):
    case = download_case
    if defect in {"missing_layout", "missing_manifest", "missing_zip"}:
        case[{"missing_layout": "state", "missing_manifest": "manifest", "missing_zip": "archive"}[defect]].unlink()
    elif defect in {"empty_zip", "changed_zip"}:
        case["archive"].write_bytes(b"" if defect == "empty_zip" else b"changed")
    elif defect == "invalid_json":
        case["manifest"].write_text("{")
    else:
        manifest = json.loads(case["manifest"].read_text())
        if defect == "bad_schema":
            manifest["schema"] = "unknown"
        elif defect == "incomplete":
            manifest["status"] = "failed"
        elif defect == "missing_record":
            manifest["outputs"] = {}
        elif defect == "wrong_path":
            manifest["outputs"]["portable_zip"]["path"] = "../outside.zip"
        elif defect == "wrong_size":
            manifest["outputs"]["portable_zip"]["bytes"] += 1
        case["manifest"].write_text(json.dumps(manifest))
    with pytest.raises((OSError, ValueError)):
        WorkspaceLayout.load(case["root"]).portable_download()


def test_author_project_is_separate_and_keeps_workspace_unchanged(download_case):
    root = download_case["root"]
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    status, headers, body = request("/api/decks/1/project-download")
    assert status == 200
    assert b"deck_1_project.zip" in headers[b"content-disposition"]
    assert body != download_case["bytes"]
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        assert {"deck.md", "knowledge.md", "present.html"} <= set(archive.namelist())
        assert all(not name.startswith(("_trace/", "_state/", "delivery/")) for name in archive.namelist())
    after = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert after == before


def test_author_project_does_not_stage_unused_original_attachments(download_case, monkeypatch):
    materials = download_case["root"] / "inputs"
    materials.mkdir()
    (materials / "source.pdf").write_bytes(b"synthetic original attachment")
    monkeypatch.setattr(main.shutil, "copytree",
                        lambda *args, **kwargs: pytest.fail("unused attachment directory must not be staged"))
    status, _, body = request("/api/decks/1/project-download")
    assert status == 200
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        assert not any(name.startswith("inputs/") for name in archive.namelist())


@pytest.mark.parametrize("source", ["missing", "file", "empty", "null"])
def test_author_project_requires_an_existing_workspace(download_case, monkeypatch, source):
    root = download_case["root"]
    selected = {"missing": str(root / "missing"), "file": str(root / "deck.md"),
                "empty": "", "null": None}[source]
    download_case["con"].execute("UPDATE decks SET run_dir = ?", (selected,))
    monkeypatch.setattr(main.tempfile, "TemporaryDirectory",
                        lambda **kwargs: pytest.fail("missing workspace must not create staging"))
    status, _, body = request("/api/decks/1/project-download")
    assert status == 409
    assert "作者工程目录不存在" in body.decode()


@pytest.mark.parametrize("linked_path", [
    "present.html", "assets", "assets/outside.txt", "assets/nested/outside.txt",
    "assets/nested/directory",
])
def test_author_project_rejects_links_outside_workspace(download_case, tmp_path_factory, linked_path):
    outside = tmp_path_factory.mktemp("other-workspace")
    marker = "SYNTHETIC OUTSIDE WORKSPACE MARKER"
    external_file = outside / "unrelated.txt"
    external_file.write_text(marker)
    link = download_case["root"] / linked_path
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.exists():
        link.unlink()
    link.symlink_to(outside if linked_path in {"assets", "assets/nested/directory"} else external_file)
    status, _, body = request("/api/decks/1/project-download")
    assert status == 409
    assert any(message in body.decode() for message in ("不支持软链接", "path mapping escapes workspace"))
    assert marker.encode() not in body
    assert external_file.read_text() == marker


def test_author_project_rejects_a_broken_source_link(download_case):
    link = download_case["root"] / "present.html"
    link.unlink()
    link.symlink_to(download_case["root"] / "missing.html")
    status, _, body = request("/api/decks/1/project-download")
    assert status == 409
    assert "不支持软链接" in body.decode()


def test_author_project_rejects_links_within_workspace(download_case):
    assets = download_case["root"] / "assets"
    assets.mkdir()
    (assets / "source.txt").write_text("author asset")
    (assets / "alias.txt").symlink_to(assets / "source.txt")
    status, _, body = request("/api/decks/1/project-download")
    assert status == 409
    assert "不支持软链接" in body.decode()


def test_author_project_rejects_a_directory_cycle_before_recursing(download_case, monkeypatch):
    assets = download_case["root"] / "assets"
    assets.mkdir()
    (assets / "loop").symlink_to(assets)
    original_copytree = main.shutil.copytree
    calls = []

    def copytree_once(*args, **kwargs):
        calls.append(args[0])
        assert len(calls) == 1, "directory cycle must be rejected before recursion"
        return original_copytree(*args, **kwargs)

    monkeypatch.setattr(main.shutil, "copytree", copytree_once)
    status, _, body = request("/api/decks/1/project-download")
    assert status == 409
    assert "不支持软链接" in body.decode()
    assert len(calls) == 1


def test_portable_download_follows_the_workspace_path_mapping(download_case):
    archive = download_case["root"] / "delivery/final.zip"
    download_case["archive"].rename(archive)
    state = json.loads(download_case["state"].read_text())
    state["paths"]["portable_zip"] = "delivery/final.zip"
    download_case["state"].write_text(json.dumps(state))
    manifest = json.loads(download_case["manifest"].read_text())
    manifest["outputs"]["portable_zip"]["path"] = "delivery/final.zip"
    download_case["manifest"].write_text(json.dumps(manifest))
    assert WorkspaceLayout.load(download_case["root"]).portable_download().read_bytes() == download_case["bytes"]


def test_portable_download_rejects_a_symlink_outside_the_workspace(download_case, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "unrelated.zip"
    outside.write_bytes(download_case["bytes"])
    download_case["archive"].unlink()
    download_case["archive"].symlink_to(outside)
    with pytest.raises(ValueError):
        WorkspaceLayout.load(download_case["root"]).portable_download()


def test_non_v1_download_is_rejected_without_rebuilding(download_case, monkeypatch):
    download_case["con"].execute("UPDATE decks SET skill_version = 'mural-presenter-v0.4'")
    calls = []
    monkeypatch.setattr(main.jobs, "_ensure_static_delivery", lambda *args: calls.append("ensure"))
    status, headers, body = request("/api/decks/1/download")
    assert status == 409 and calls == []


@pytest.mark.parametrize("skill", ["mural-presenter-v1", "old-skill"])
@pytest.mark.parametrize("endpoint", ["progress", "events"])
def test_progress_advertises_verified_download_capability(download_case, monkeypatch, skill, endpoint):
    row = dict(download_case["con"].execute("SELECT * FROM decks").fetchone()) | {
        "skill_version": skill, "title": "Example", "title_source": "manual", "model": None, "pipeline": None,
        "started_at": None, "finished_at": None, "error": None,
    }

    class Connection:
        def execute(self, *args):
            return self

        def fetchone(self):
            return row

        def close(self):
            pass

    class Request:
        async def is_disconnected(self):
            return False

    monkeypatch.setattr(main, "connect", Connection)
    monkeypatch.setattr(main, "_own_deck", lambda *args: row)
    monkeypatch.setattr(main, "_model_label", lambda *args: None)
    monkeypatch.setattr(main, "_deck_conversation_turns", lambda *args: [])
    monkeypatch.setattr(main.trace, "snapshot", lambda *args, **kwargs: {"status": "completed"})
    if endpoint == "progress":
        payload = main.deck_progress(1, user={"id": 7}, con=Connection())
    else:
        async def first_event():
            response = await main.deck_events(1, request=Request(), user={"id": 7})
            try:
                return await anext(response.body_iterator)
            finally:
                await response.body_iterator.aclose()

        payload = json.loads(asyncio.run(first_event()).removeprefix("data: "))
    assert payload["verified_delivery_download"] is (skill == "mural-presenter-v1")


def test_export_menu_names_both_v1_packages_and_preserves_other_downloads():
    source = (Path(__file__).resolve().parents[1] / "static/app.js").read_text()
    render = source[source.index("function renderActions("):source.index("function closeExportMenu(")]
    script = """
const host = {dataset: {}, innerHTML: ''};
const $ = selector => selector === '#ed-actions' ? host : null;
const ed = {id: 1, kind: 'static', briefMeta: {skill_version: 'mural-presenter-v1'}};
const bindUtilityActions=()=>{}, bindExportMenu=()=>{}, bindFontResolution=()=>{}, bindPresentationFullscreen=()=>{};
""" + render + """
renderActions('completed'); const oldServer = host.innerHTML;
ed.briefMeta.verified_delivery_download=true;
renderActions('completed'); const current = host.innerHTML;
ed.briefMeta.skill_version='old-skill'; renderActions('completed'); const previous = host.innerHTML;
renderActions('failed'); const failed = host.innerHTML;
console.log(JSON.stringify({oldServer, current, previous, failed}));
"""
    completed = subprocess.run(["node", "-e", script], check=True, text=True, capture_output=True)
    menus = json.loads(completed.stdout)
    assert "project-download" not in menus["oldServer"]
    assert "已验收" not in menus["oldServer"]
    assert '/api/decks/1/download' in menus["oldServer"]
    assert '/api/decks/1/project-download' in menus["current"]
    assert "下载作者工程包" in menus["current"]
    for selected in ("html", "pdf", "pptx", "zip"):
        assert f'data-export-format="{selected}"' in menus["current"]
    assert "按需生成" in menus["current"]
    assert '/api/decks/1/pptx' not in menus["current"]
    assert '/api/decks/1/download' in menus["previous"]
    assert "project-download" not in menus["previous"]
    assert "下载完整工程与资源" in menus["previous"]
    assert "project-download" not in menus["failed"]
    assert '/api/decks/1/download' not in menus["failed"]
