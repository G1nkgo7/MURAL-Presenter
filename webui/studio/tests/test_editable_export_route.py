"""可编辑导出仍走原鉴权与离线导出链，覆盖报告须与产物一同验收。"""
import hashlib
import json
from pathlib import Path
import re
import subprocess

import pytest

from app import main
from test_delivery_download import download_case, request
from test_on_demand_export import export_case


def editable_worker(root, defect=None):
    def receipt(path, payload):
        path.write_bytes(payload)
        return {"path": path.relative_to(root).as_posix(), "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest()}

    def worker(command, **_options):
        assert command[-1] == "pptx-editable"
        result = receipt(root / "delivery/presentation.editable.pptx", b"editable fixture")
        report = receipt(root / "delivery/presentation.editable.editability.json", b'{"pages":[]}')
        result.update(format="pptx-editable", editability_report=report, summary={
            "native_text": 4, "native_images": 2, "native_shapes": 1, "native_tables": 1,
            "local_raster_fallbacks": 1, "pages_with_warnings": 1,
        })
        if defect == "hash": report["sha256"] = "0" * 64
        if defect == "size": report["bytes"] += 1
        if defect == "escape": report["path"] = "../private.json"
        if defect == "missing": (root / report["path"]).unlink()
        if defect == "symlink":
            path = root / report["path"]
            path.unlink()
            path.symlink_to(root / "_trace/private.json")
        if defect == "summary": result["summary"]["native_text"] = "invented"
        return subprocess.CompletedProcess(command, 0, json.dumps(result), "")
    return worker


def test_editable_route_downloads_pptx_with_verified_report_headers(export_case, monkeypatch):
    monkeypatch.setattr(main.jobs, "_run_noninteractive", editable_worker(export_case["root"]))
    status, headers, body = request("/api/decks/1/export/pptx-editable", method="POST")
    assert status == 200 and body == b"editable fixture"
    assert b"presentation_1_editable.pptx" in headers[b"content-disposition"]
    assert headers[b"x-mural-editability-report"] == b"delivery%2Fpresentation.editable.editability.json"
    assert headers[b"x-mural-editable-text"] == b"4"
    assert headers[b"x-mural-raster-fallbacks"] == b"1"


@pytest.mark.parametrize("custom_slots", [False, True])
def test_offline_export_uses_generation_render_slots_without_credentials(export_case, monkeypatch, custom_slots):
    slots = export_case["root"] / "shared-render-slots"
    monkeypatch.setattr(main.engine, "DATA_DIR", export_case["root"] / "studio-data")
    monkeypatch.delenv("MURAL_RESOURCE_SLOTS_DIR", raising=False)
    if custom_slots:
        monkeypatch.setenv("MURAL_RESOURCE_SLOTS_DIR", str(slots))
    monkeypatch.setenv("RENDER_CONCURRENCY", "2")
    monkeypatch.setenv("MODEL_API_KEY", "test-not-exported")
    pointer = str(export_case["root"] / "runtime.json")
    monkeypatch.setenv("MURAL_RUNTIME_CONFIG", pointer)
    _, env = main.jobs._offline_runtime(export_case["root"])
    assert "MURAL_RESOURCE_SLOTS_DIR" not in env
    assert "RENDER_CONCURRENCY" not in env
    assert env["MURAL_RUNTIME_CONFIG"] == pointer
    assert env["SKILL_COMMAND_TIMEOUT_S"] == "600"
    assert "MODEL_API_KEY" not in env


@pytest.mark.parametrize("defect", ["hash", "size", "escape", "missing", "symlink", "summary"])
def test_invalid_editability_evidence_blocks_download(export_case, monkeypatch, defect):
    monkeypatch.setattr(main.jobs, "_run_noninteractive", editable_worker(export_case["root"], defect))
    status, headers, _body = request("/api/decks/1/export/pptx-editable", method="POST")
    assert status == 409 and b"content-disposition" not in headers


@pytest.mark.parametrize("owner,expected", [(8, 404), (None, 401)])
def test_editable_export_is_owner_scoped(export_case, monkeypatch, owner, expected):
    main.app.dependency_overrides[main.current_user] = lambda: {"id": owner} if owner else None
    monkeypatch.setattr(main.jobs, "_run_noninteractive", lambda *_a, **_k: pytest.fail("not authorized"))
    assert request("/api/decks/1/export/pptx-editable", method="POST")[0] == expected


def test_browser_editable_option_keeps_extension_and_exposes_fallback_report():
    source = (Path(__file__).resolve().parents[1] / "static/app.js").read_text()
    start = re.search(r"^async function downloadDeckExport\(", source, re.M).start()
    end = re.search(r"^function ", source[start + 1:], re.M).start() + start + 1
    script = """
const ed={id:17,status:'completed'},pendingDeckExports=new Set(), notices=[], downloads=[], links=[];
const renderActions=()=>{},dismissStudioNotice=()=>{};
const showStudioNotice=(message)=>{notices.push(message); return {querySelector:()=>({append:link=>links.push([link.href,link.textContent])})};};
const fetch=async()=>({ok:true,headers:{get:name=>({'X-Mural-Editability-Report':'delivery%2Freport.json','X-Mural-Editable-Text':'4','X-Mural-Raster-Fallbacks':'2'})[name]},blob:async()=>new Uint8Array([1])});
const URL={createObjectURL:()=> 'blob:pptx',revokeObjectURL:()=>{}},setTimeout=fn=>fn();
const document={body:{append:()=>{}},createElement:()=>({click(){downloads.push(this.download)},remove(){}})};
""" + source[start:end] + """
downloadDeckExport('pptx-editable').then(()=>console.log(JSON.stringify({notices,downloads,links})));
"""
    value = json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout)
    assert value["downloads"] == ["presentation_17_editable.pptx"]
    assert value["links"][0][0] == "/api/decks/17/file?rel=delivery%2Freport.json"
    assert "4 个原生文本对象" in value["notices"][-1]
    assert "2 处复杂效果保留为局部图片" in value["notices"][-1]
    assert '["pptx", "高保真 PPTX"' in source and '["pptx-editable", "可编辑 PPTX"' in source
