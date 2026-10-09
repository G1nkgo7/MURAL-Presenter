"""按需导出只运行离线 Harness，并由鉴权路由返回真实文件。"""
import hashlib
import json
from pathlib import Path
import re
import subprocess

import pytest

from app import main
from test_delivery_download import download_case, request


@pytest.fixture
def export_case(download_case, monkeypatch):
    calls = []
    root = download_case["root"]
    python = Path(__file__).resolve().parents[2] / "inference/.venv/bin/python"
    monkeypatch.setattr(main.engine, "_engine_python", lambda: str(python))
    monkeypatch.setattr(main.engine, "render_env", lambda env: dict(env))
    monkeypatch.setattr(main, "DATA_DIR", root / "studio-data")
    def worker(command, **kwargs):
        calls.append((command, kwargs))
        selected = command[command.index("--format") + 1]
        target = root / "delivery" / f"fixture.{selected}"
        data = f"verified {selected}".encode()
        target.write_bytes(data)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({
            "format": selected, "path": f"delivery/{target.name}", "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "source_sha256": "a" * 64,
            "cached": False,
        }), stderr="")
    monkeypatch.setattr(main.jobs, "_run_noninteractive", worker)
    return {**download_case, "calls": calls, "python": str(python)}


@pytest.mark.parametrize("selected,media", [
    ("html", b"text/html"), ("pdf", b"application/pdf"),
    ("pptx", b"application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    ("zip", b"application/zip"),
])
def test_export_route_generates_then_downloads_with_formal_python(export_case, monkeypatch, selected, media):
    monkeypatch.setenv("MODEL_API_KEY", "synthetic-secret-not-needed-for-export")
    status, headers, body = request(f"/api/decks/1/export/{selected}", method="POST")
    assert status == 200 and body == f"verified {selected}".encode()
    assert headers[b"content-type"].startswith(media)
    assert f"presentation_1.{selected}".encode() in headers[b"content-disposition"]
    assert len(export_case["calls"]) == 1
    command, options = export_case["calls"][0]
    assert command == [export_case["python"], "-m", "export.delivery", "--workspace", str(export_case["root"]), "--format", selected]
    assert options["cwd"] == main.engine.MURAL_V1_HARNESS_ROOT
    assert "MODEL_API_KEY" not in options["env"]
    assert options["env"]["MURAL_USER_ID"] == "7"
    assert str(options["env"]["TMPDIR"]).startswith(str(export_case["root"] / "_runtime"))
    assert "doctor" not in command and not (export_case["root"] / "deck_1.pptx").exists()


@pytest.mark.parametrize("endpoint,selected", [("pptx", "pptx"), ("download", "zip")])
def test_existing_links_use_same_on_demand_export(export_case, endpoint, selected):
    status, _, body = request(f"/api/decks/1/{endpoint}")
    assert status == 200 and body == f"verified {selected}".encode()
    assert export_case["calls"][0][0][-1] == selected
    assert not (export_case["root"] / "deck_1.pptx").exists()


@pytest.mark.parametrize("condition,expected", [("running", 409), ("failed", 409), ("foreign", 404), ("anonymous", 401), ("legacy", 409), ("format", 422)])
def test_on_demand_export_rejects_unauthorized_or_unready_requests(export_case, condition, expected):
    if condition in {"running", "failed"}:
        export_case["con"].execute("UPDATE decks SET status = ?", (condition,))
    elif condition == "foreign":
        main.app.dependency_overrides[main.current_user] = lambda: {"id": 8}
    elif condition == "anonymous":
        main.app.dependency_overrides[main.current_user] = lambda: None
    elif condition == "legacy":
        export_case["con"].execute("UPDATE decks SET skill_version = 'old-skill'")
    selected = "exe" if condition == "format" else "pdf"
    assert request(f"/api/decks/1/export/{selected}", method="POST")[0] == expected
    assert export_case["calls"] == []


@pytest.mark.parametrize("failure", ["timeout", "rejected", "malformed", "escape", "absolute", "symlink", "size", "hash", "format", "missing"])
def test_on_demand_export_never_downloads_failed_or_unsafe_output(export_case, monkeypatch, failure):
    def worker(command, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 1, stderr="SYNTHETIC PRIVATE STDERR")
        if failure == "rejected":
            return subprocess.CompletedProcess(command, 1, stdout="", stderr=json.dumps({
                "error": "delivery_export_failed", "message": "工作区未验收"}))
        if failure == "malformed":
            return subprocess.CompletedProcess(command, 0, stdout="SYNTHETIC PRIVATE STDOUT", stderr="")
        target = export_case["root"] / "delivery/fixture.pdf"
        target.write_bytes(b"verified pdf")
        value = {"format": "pdf", "path": "delivery/fixture.pdf", "bytes": 12,
                 "sha256": hashlib.sha256(b"verified pdf").hexdigest(), "cached": False}
        if failure == "escape": value["path"] = "../private.pdf"
        if failure == "absolute": value["path"] = str(target)
        if failure == "symlink":
            target.unlink()
            target.symlink_to(export_case["root"] / "_trace/private.json")
        if failure == "size": value["bytes"] = 999
        if failure == "hash": value["sha256"] = "0" * 64
        if failure == "format": value["format"] = "html"
        if failure == "missing": target.unlink()
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(value), stderr="")
    monkeypatch.setattr(main.jobs, "_run_noninteractive", worker)
    status, headers, body = request("/api/decks/1/export/pdf", method="POST")
    assert status == (504 if failure == "timeout" else 409)
    assert b"content-disposition" not in headers
    assert b"SYNTHETIC PRIVATE" not in body and b"not an export" not in body


@pytest.mark.parametrize("python", [None, "/usr/bin/python3"])
def test_export_requires_formal_inference_environment(export_case, monkeypatch, python):
    monkeypatch.setattr(main.engine, "_engine_python", lambda: python)
    assert request("/api/decks/1/export/pdf", method="POST")[0] == 503
    assert export_case["calls"] == []


def test_project_archive_stages_only_under_controlled_directory(export_case, monkeypatch):
    original = main.tempfile.TemporaryDirectory
    parents = []
    def controlled(**kwargs):
        parents.append(Path(kwargs["dir"]))
        return original(**kwargs)
    monkeypatch.setattr(main.tempfile, "TemporaryDirectory", controlled)
    assert request("/api/decks/1/project-download")[0] == 200
    assert parents == [main.DATA_DIR / "tmp"]


@pytest.mark.parametrize("prefix", ["", "<frozen runpy>:128: RuntimeWarning: SYNTHETIC PRIVATE WARNING\n"])
def test_export_failure_projects_only_the_final_known_error_message(export_case, monkeypatch, prefix):
    record = {"error": "delivery_export_failed", "code": "export_font_invalid",
        "message": "Pinned fonts are unavailable. sk-syntheticExportSecret1234567890",
        "api_key": "SYNTHETIC PRIVATE KEY", "thinking_signature": "SYNTHETIC PRIVATE SIGNATURE",
        "request": {"body": "SYNTHETIC PRIVATE REQUEST"}}
    monkeypatch.setattr(main.jobs, "_run_noninteractive", lambda command, **kwargs:
        subprocess.CompletedProcess(command, 1, "", prefix + json.dumps(record) + "\n"))
    status, headers, body = request("/api/decks/1/export/html", method="POST")
    detail = json.loads(body)["detail"]
    assert status == 409 and b"content-disposition" not in headers
    assert "Pinned fonts are unavailable." in detail and "sk-syntheticExportSecret1234567890" in detail
    assert "[REDACTED]" not in detail and b"SYNTHETIC PRIVATE" not in body
    assert b"thinking_signature" not in body and b"request" not in body


@pytest.mark.parametrize("stderr", ["", "SYNTHETIC PRIVATE STDERR", "[]", "null",
    '{"error":"unrecognized","message":"SYNTHETIC PRIVATE MESSAGE"}',
    '{"error":"delivery_export_failed","message":"do not read earlier lines"}\nSYNTHETIC PRIVATE TAIL'])
def test_export_failure_does_not_search_or_expose_arbitrary_stderr(export_case, monkeypatch, stderr):
    monkeypatch.setattr(main.jobs, "_run_noninteractive", lambda command, **kwargs:
        subprocess.CompletedProcess(command, 1, "", stderr))
    status, headers, body = request("/api/decks/1/export/html", method="POST")
    assert status == 409 and b"content-disposition" not in headers
    assert json.loads(body) == {"detail": "导出未通过工作区验收"}


@pytest.mark.parametrize("succeeded", [True, False])
def test_browser_export_checks_http_and_prevents_duplicate_download(succeeded):
    source = (Path(__file__).resolve().parents[1] / "static/app.js").read_text()
    start = re.search(r"^async function downloadDeckExport\(", source, re.M).start()
    end = re.search(r"^function ", source[start + 1:], re.M).start() + start + 1
    script = """
const ed = {id: 17, status:'completed'}, pendingDeckExports = new Set();
const notices=[], states=[], requests=[], downloads=[], blobs=[];
const renderActions = () => states.push(pendingDeckExports.has(String(ed.id)));
const showStudioNotice = (message, options) => (notices.push([message, options]), {});
const dismissStudioNotice = () => {};
let resolveResponse;
const fetch = (url, options) => { requests.push([url, options]); return new Promise(resolve => resolveResponse=resolve); };
const URL = {createObjectURL: blob => (blobs.push(blob), 'blob:download'), revokeObjectURL: () => {}};
const setTimeout = fn => fn();
const document = {body:{append:()=>{}},createElement:()=>({click(){downloads.push([this.href,this.download]);},remove(){}})};
""" + source[start:end] + """
(async()=>{
 const first=downloadDeckExport('pdf');
 await downloadDeckExport('pdf');
 resolveResponse({ok:SUCCEEDED,status:409,json:async()=>({detail:'工作区未验收'}),blob:async()=>new Uint8Array([1,2,3])});
 await first;
 console.log(JSON.stringify({notices,states,requests,downloads,blobCount:blobs.length,pending:pendingDeckExports.size}));
})();
""".replace("SUCCEEDED", str(succeeded).lower())
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    value = json.loads(result.stdout)
    assert value["requests"] == [["/api/decks/17/export/pdf", {"method": "POST"}]]
    assert value["states"] == [True, False] and value["pending"] == 0
    if succeeded:
        assert value["downloads"] == [["blob:download", "presentation_17.pdf"]]
        assert value["blobCount"] == 1
    else:
        assert value["downloads"] == [] and value["blobCount"] == 0
        assert value["notices"][-1][0] == "导出失败：工作区未验收"
