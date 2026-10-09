"""历史画面复用已有 blob；只读、按像素 hash 校验，不公开任意 Raw Trace。"""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

from PIL import Image
import pytest

from app import main, trace, trajectory_monitor
from test_delivery_download import download_case, request
from test_owner_workflow import append, case, graph


@pytest.fixture
def raw_case(case):
    root, digest = case["root"], case["digest"]
    raw = root / "_trace/raw/blobs"
    raw.mkdir(parents=True)
    (root / f"_trace/training/blobs/{digest}.png").rename(raw / digest)
    return case


def image_url(digest):
    return f"/api/decks/1/trace-images/{digest}"


def test_inference_history_and_http_use_exact_existing_blob_without_writes(raw_case):
    root, digest = raw_case["root"], raw_case["digest"]
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    rendered = next(node for node in graph(raw_case)["nodes"] if node["kind"] == "render")
    assert rendered["images"][0]["rel"] == f"_trace/raw/blobs/{digest}"
    status, headers, body = request(image_url(digest))
    assert status == 200 and headers[b"content-type"] == b"image/png"
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert headers[b"cache-control"].startswith(b"private,")
    assert body == (root / f"_trace/raw/blobs/{digest}").read_bytes()
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    assert request(f"/api/decks/1/files/_trace/raw/blobs/{digest}")[0] == 403


def test_image_route_preserves_deck_authentication(raw_case):
    main.app.dependency_overrides[main.current_user] = lambda: {"id": 8}
    assert request(image_url(raw_case["digest"]))[0] == 404
    main.app.dependency_overrides[main.current_user] = lambda: None
    assert request(image_url(raw_case["digest"]))[0] == 401


@pytest.mark.parametrize("defect", ["missing", "tampered", "external_symlink", "private_symlink"])
def test_missing_or_changed_history_never_uses_current_render(raw_case, defect):
    root, digest = raw_case["root"], raw_case["digest"]
    blob = root / f"_trace/raw/blobs/{digest}"
    content = blob.read_bytes()
    blob.unlink()
    if defect == "tampered":
        blob.write_bytes(b"changed")
    elif defect.endswith("symlink"):
        target = root.parent / (root.name + "-external.png") if defect == "external_symlink" else root / "_state/private.png"
        target.write_bytes(content)
        blob.symlink_to(target)
    (root / "renders").mkdir()
    (root / "renders/slide_01.png").write_bytes(content)
    assert request(image_url(digest))[0] == 404
    rendered = next(node for node in graph(raw_case)["nodes"] if node["kind"] == "render")
    assert rendered["images"][0]["rel"] == ""


@pytest.mark.parametrize("private_reference", ["none", "agent_context", "assistant_message", "unrelated_tool", "partial_receipt"])
def test_raw_image_requires_public_render_or_inspect_reference(raw_case, private_reference):
    journal = raw_case["root"] / "_trace/live-events.jsonl"
    journal.write_text("")
    digest = raw_case["digest"]
    kind, payload = private_reference, {"png_sha256": digest}
    if private_reference == "unrelated_tool":
        kind, payload = "tool_result", {"name": "read_file", "result": {"png_sha256": digest}}
    elif private_reference == "partial_receipt":
        kind, payload = "tool_result", {"name": "inspect", "result": {"png_sha256": digest}}
    append(raw_case["root"], 1, kind, payload)
    if private_reference == "partial_receipt":
        journal.write_bytes(journal.read_bytes().rstrip(b"\n"))
    assert request(image_url(digest))[0] == 404


def test_known_hash_cannot_download_non_image_or_raw_provider_json(raw_case):
    root = raw_case["root"]
    body = b'{"signature":"private-signature","api_key":"private-key"}'
    digest = hashlib.sha256(body).hexdigest()
    (root / f"_trace/raw/blobs/{digest}").write_bytes(body)
    append(root, 8, "tool_result", {"name": "render", "tool_use_id": "bad", "ok": True,
                                  "result": {"page": 1, "png_sha256": digest}})
    status, _, response = request(image_url(digest))
    assert status == 404 and b"private-signature" not in response
    (root / "_trace/raw/request.json").write_bytes(body)
    assert request("/api/decks/1/files/_trace/raw/request.json")[0] == 403
    assert request(image_url("request.json"))[0] == 404
    assert request(image_url("F" * 64))[0] == 404


@pytest.mark.parametrize("encoding,media_type", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")])
def test_group_receipt_and_real_image_encoding(raw_case, encoding, media_type):
    image = io.BytesIO()
    Image.new("RGB", (16, 9), "blue").save(image, format=encoding)
    body = image.getvalue()
    digest = hashlib.sha256(body).hexdigest()
    (raw_case["root"] / f"_trace/raw/blobs/{digest}").write_bytes(body)
    append(raw_case["root"], 8, "tool_result", {"name": "inspect_group", "ok": True, "tool_use_id": "group",
        "result": {"pending_inspection": {"members": [{"page": 2, "png_sha256": digest}]}}})
    status, headers, response = request(image_url(digest))
    assert status == 200 and headers[b"content-type"].decode() == media_type and response == body


@pytest.mark.parametrize("kind", ["group", "asset"])
def test_unified_inspect_projects_actual_image_not_individual_member_views(raw_case, kind):
    root = raw_case["root"]
    image = io.BytesIO()
    Image.new("RGB", (32, 18), "blue").save(image, format="PNG")
    digest = hashlib.sha256(image.getvalue()).hexdigest()
    (root / f"_trace/raw/blobs/{digest}").write_bytes(image.getvalue())
    members = [{"page": p, "png_sha256": raw_case["digest"]} for p in (1, 2)] if kind == "group" else []
    args = {"page": [1, 2]} if kind == "group" else {"path": "assets/figure.png"}
    feedback = {"inspection_id": "joint-view", "png_sha256": digest, "kind": kind,
                "completed": True, "body": "仅当前联系表 / 原图可见信息，不代表逐页全分辨率检查。"}
    append(root, 8, "tool_use", {"name": "inspect", "tool_use_id": "joint", "arguments": args})
    append(root, 9, "tool_result", {"name": "inspect", "tool_use_id": "joint", "ok": True,
        "result": {"kind": kind, "sha256": digest, "inspection_id": "joint-view", "native_multimodal": True,
                   "pending_inspection": {"kind": kind, "members": members}, "critic_feedback": feedback}})
    nodes = graph(raw_case)["nodes"]
    viewed, = [row for row in nodes if row.get("tool_use_id") == "joint" and row["kind"] == "render"]
    reviewed, = [row for row in nodes if row.get("tool_use_id") == "joint" and row["kind"] == "critic"]
    assert viewed["pages"] == ([1, 2] if kind == "group" else [])
    assert len(viewed["images"]) == 1 and viewed["images"][0]["sha256"] == digest
    assert viewed["title"] == ("多页联系表检查" if kind == "group" else "素材 / 附件图片检查")
    assert reviewed["feedback"] == feedback["body"] and reviewed["render_id"] == viewed["id"]
    assert reviewed["png_sha256"] == digest and reviewed["pages"] == viewed["pages"]
    assert request(image_url(digest))[0] == 200


def test_critic_can_reference_image_before_inspect_finishes(raw_case):
    journal = raw_case["root"] / "_trace/live-events.jsonl"
    rows = journal.read_text().splitlines()
    journal.write_text("\n".join(rows[:3]) + "\n")
    assert request(image_url(raw_case["digest"]))[0] == 200


def test_trajectory_uses_same_verified_image_reader(raw_case, monkeypatch):
    monkeypatch.setattr(trajectory_monitor, "_resolve_deck", lambda deck_id: (None, raw_case["root"], {}))
    status, headers, body = request(f'/api/trajectory-monitor/decks/1/trace-images/{raw_case["digest"]}')
    assert status == 200 and headers[b"content-type"] == b"image/png"
    assert hashlib.sha256(body).hexdigest() == raw_case["digest"]


@pytest.mark.parametrize("trajectory", [False, True])
def test_frontend_history_url_uses_image_endpoint_without_changing_other_artifacts(raw_case, trajectory):
    source = (Path(__file__).resolve().parents[1] / "static/app.js").read_text()
    function = re.search(r"^function workflowArtifactUrl\(file\) \{.*?^\}", source, re.M | re.S).group()
    relatives = [f'_trace/raw/blobs/{raw_case["digest"]}', f'_trace/training/blobs/{raw_case["digest"]}.png',
                 "slides/slide_01.html", "_trace/raw/request.json"]
    script = (f"const ed={{id:1}}, trajectoryMode={json.dumps(trajectory)}; "
              "const trajectoryDeckApi=(id,path)=>`/api/trajectory-monitor/decks/${id}${path}`;\n"
              + function + f"\nconsole.log(JSON.stringify({json.dumps(relatives)}.map(rel=>workflowArtifactUrl({{rel}}))));")
    urls = json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True, timeout=20).stdout)
    prefix = "/api/trajectory-monitor/decks/1" if trajectory else "/api/decks/1"
    assert urls == [f'{prefix}/trace-images/{raw_case["digest"]}'] + [f"{prefix}/files/{rel}" for rel in relatives[1:]]


def test_real_browser_loads_all_three_pixel_versions_with_current_url_builder(raw_case, monkeypatch):
    from playwright.sync_api import sync_playwright

    root = raw_case["root"]
    for sequence, color in [(8, "blue"), (10, "green")]:
        image = io.BytesIO()
        Image.new("RGB", (160, 90), color).save(image, format="PNG")
        body = image.getvalue()
        digest = hashlib.sha256(body).hexdigest()
        (root / f"_trace/raw/blobs/{digest}").write_bytes(body)
        append(root, sequence, "tool_use", {"name": "inspect", "tool_use_id": str(sequence), "arguments": {"page": 1}})
        append(root, sequence + 1, "tool_result", {"name": "inspect", "tool_use_id": str(sequence), "ok": True,
            "result": {"page": 1, "png_sha256": digest, "native_multimodal": True}})
    frames = [node["images"][0] for node in graph(raw_case)["nodes"] if node["kind"] == "render"]
    assert len(frames) == 3 and len({frame["sha256"] for frame in frames}) == 3
    responses = {image_url(frame["sha256"]): request(image_url(frame["sha256"])) for frame in frames}
    source = (Path(__file__).resolve().parents[1] / "static/app.js").read_text()
    function = re.search(r"^function workflowArtifactUrl\(file\) \{.*?^\}", source, re.M | re.S).group()
    browser_tmp = root.parent / (root.name + "-browser")
    browser_tmp.mkdir()
    env = main.engine.render_env(dict(os.environ))
    for key in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(key, str(browser_tmp))
        env[key] = str(browser_tmp)
    executable = env.get("PPT_SKILL_BROWSER_EXE")
    assert executable, "formal Chromium runtime is required"
    requested = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=executable, env=env, headless=True, args=["--no-sandbox"])
        try:
            page = browser.new_page()
            def serve(route):
                path = urlsplit(route.request.url).path
                requested.append(path)
                if path == "/":
                    route.fulfill(content_type="text/html", body="<body></body>")
                elif path in responses:
                    status, headers, body = responses[path]
                    route.fulfill(status=status, content_type=headers[b"content-type"].decode(), body=body)
                else:
                    route.fulfill(status=404, body="missing")
            page.route("**/*", serve)
            page.goto("http://history.test/")
            page.add_script_tag(content="const ed={id:1}; let trajectoryMode=false; "
                "const trajectoryDeckApi=(id,path)=>`/api/trajectory-monitor/decks/${id}${path}`;\n" + function)
            page.evaluate("frames => {for (const frame of frames) {const img=new Image(); "
                "img.src=workflowArtifactUrl(frame); document.body.append(img);}}", frames)
            page.wait_for_function("document.images.length===3 && [...document.images].every(i=>i.complete && i.naturalWidth===160 && i.naturalHeight===90)")
            assert all(url in requested for url in responses)
            assert page.evaluate("workflowArtifactUrl({rel:'slides/slide_01.html'})") == "/api/decks/1/files/slides/slide_01.html"
            page.evaluate("trajectoryMode=true")
            assert page.evaluate("file=>workflowArtifactUrl(file)", frames[0]) == f'/api/trajectory-monitor/decks/1/trace-images/{frames[0]["sha256"]}'
        finally:
            browser.close()
