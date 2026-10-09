"""HTTP 预览只投影 workspace 已固定字体，不引入可变全局字库。"""
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from app import main
from test_delivery_download import download_case, request


@pytest.fixture
def font_case(download_case, monkeypatch):
    root = download_case["root"]
    studio = root / "shared-data"
    digest = hashlib.sha256(b"synthetic-public-font").hexdigest()
    name = digest + ".woff2"
    target = studio / "shared/fonts/objects" / name
    target.parent.mkdir(parents=True)
    target.write_bytes(b"synthetic-public-font")
    manifest = root / "_state/font-manifest.json"
    manifest.write_text(json.dumps({"schema": "mural.font-manifest.v2", "studio_data_root": str(studio),
        "source_faces": [{"scope": "public", "object_ref": f"shared/fonts/objects/{name}", "sha256": digest}]}))
    css = f'@font-face {{ font-family: "Pinned"; src: url("{target.as_uri()}") format("woff2"); }}'
    (root / "base.css").write_text(css)
    (root / "present.html").write_text(f"<html><style>{css}</style><p>Preview</p></html>")
    calls = []
    def resolve(workspace, reference):
        calls.append((workspace, reference))
        return target, digest
    monkeypatch.setattr(main.jobs, "resolve_shared_font", resolve)
    monkeypatch.setattr(main.preview_fonts, "inject",
        lambda *a, **kw: pytest.fail("v1 must not inject mutable global fonts"))
    return {**download_case, "font": target, "font_manifest": manifest, "name": name,
            "digest": digest, "calls": calls, "css": css}


@pytest.mark.parametrize("relative", ["base.css", "present.html"])
def test_v1_preview_rewrites_only_response_and_uses_authorized_font_endpoint(font_case, relative):
    source = (font_case["root"] / relative).read_bytes()
    status, headers, body = request(f"/api/decks/1/files/{relative}")
    assert status == 200
    assert b"file://" not in body
    assert f'/api/decks/1/shared-fonts/{font_case["name"]}'.encode() in body
    assert (font_case["root"] / relative).read_bytes() == source
    status, headers, body = request(f'/api/decks/1/shared-fonts/{font_case["name"]}')
    assert status == 200 and body == b"synthetic-public-font"
    assert headers[b"cache-control"] == b"private, max-age=31536000, immutable"
    assert font_case["calls"] == [(font_case["root"], font_case["font"].as_uri())]


@pytest.mark.parametrize("condition,expected", [("foreign",404), ("anonymous",401), ("unlisted",404), ("private",404), ("traversal",404), ("hash",404), ("malformed",409), ("worker",409)])
def test_font_route_rejects_cross_owner_unpinned_and_invalid_references(font_case, monkeypatch, condition, expected):
    name = font_case["name"]
    if condition == "foreign": main.app.dependency_overrides[main.current_user] = lambda: {"id": 8}
    elif condition == "anonymous": main.app.dependency_overrides[main.current_user] = lambda: None
    elif condition == "unlisted": name = "0" * 64 + ".ttf"
    elif condition in {"private", "traversal", "hash"}:
        manifest = json.loads(font_case["font_manifest"].read_text())
        face = manifest["source_faces"][0]
        if condition == "private": face["scope"] = "private"
        if condition == "traversal": face["object_ref"] = f"../objects/{name}"
        if condition == "hash": face["sha256"] = "0" * 64
        font_case["font_manifest"].write_text(json.dumps(manifest))
    elif condition == "malformed": font_case["font_manifest"].write_text('["invalid"]')
    elif condition == "worker":
        def reject(*args): raise main.jobs.DeliveryExportError("固定字体未通过校验")
        monkeypatch.setattr(main.jobs, "resolve_shared_font", reject)
    status, headers, body = request(f"/api/decks/1/shared-fonts/{name}")
    assert status == expected and b"synthetic-public-font" not in body


def test_preview_rejects_unpinned_font_but_does_not_rewrite_other_file_urls(font_case):
    css = font_case["root"] / "base.css"
    css.write_text('.background {background-image:url("file:///example/picture.png")}')
    assert request("/api/decks/1/files/base.css")[0] == 200
    css.write_text('@font-face {src:url("file:///unrelated/private.ttf")}')
    assert request("/api/decks/1/files/base.css")[0] == 409


def test_font_worker_uses_formal_runtime_without_model_configuration(font_case, monkeypatch):
    python = Path(__file__).resolve().parents[2] / "inference/.venv/bin/python"
    monkeypatch.setattr(main.engine, "_engine_python", lambda: str(python))
    monkeypatch.setattr(main.engine, "render_env", lambda env: dict(env))
    monkeypatch.setenv("MODEL_API_KEY", "synthetic-not-inherited")
    calls = []
    def worker(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, json.dumps({"path": str(font_case["font"]), "sha256": font_case["digest"]}), "")
    monkeypatch.setattr(main.jobs, "_run_noninteractive", worker)
    result = _REAL_RESOLVE(font_case["root"], font_case["font"].as_uri())
    assert result == (font_case["font"], font_case["digest"])
    command, kwargs = calls[0]
    assert command[0] == str(python) and "resolve_font_reference" in command[2]
    assert "MODEL_API_KEY" not in kwargs["env"] and "doctor" not in command
    assert kwargs["timeout"] == 30


_REAL_RESOLVE = main.jobs.resolve_shared_font


def test_font_panel_prefers_authoring_resolution_trace(font_case):
    row = {"role":"body", "requested_family":"Noto Sans SC", "resolved_family":"Noto Sans SC", "resolution_kind":"exact_builtin"}
    (font_case["root"] / "_trace/font-resolution.json").write_text(json.dumps({"resolutions":[row], "coverage":{"ok":True}}))
    legacy = font_case["root"] / "_runtime/tmp/fonts/manifest.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({"resolutions":[{**row,"resolved_family":"outdated"}]}))
    status, _, body = request("/api/decks/1/font-resolution")
    value = json.loads(body)
    assert status == 200 and value["ready"]
    assert value["resolutions"][0]["resolved"] == "Noto Sans SC"
    assert b"studio_data_root" not in body and b"object_ref" not in body
