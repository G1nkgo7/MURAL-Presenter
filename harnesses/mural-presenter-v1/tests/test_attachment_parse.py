"""固定 3.4.5 协议的真实 HTTP mock；不访问外部服务或用户附件。"""
import base64
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import runpy
import threading

import pymupdf
from PIL import Image
import pytest
import requests

from runtime import attachments, mineru, office


@pytest.fixture(scope="session")
def corpus(tmp_path_factory):
    root = Path(__file__).resolve().parents[3]
    make = runpy.run_path(str(root / "configs/mineru/verify.py"))["corpus"]
    return {path.suffix[1:]: path for path in make(tmp_path_factory.mktemp("mineru-corpus"))}


def stage(tmp_path, source):
    root = tmp_path / "workspace"
    attachments.AttachmentStager(root).stage([source])
    return root, attachments.resolve_attachment_records(root)[0]


def response(filename, kind):
    suffix = Path(filename).suffix[1:]
    office = suffix in {"docx", "pptx", "xlsx"}
    count = 2 if suffix in {"pdf", "pptx", "xlsx"} else 1
    middle = {"_version_name": "3.4.5", "_backend": "office" if office else "hybrid" if kind == "gpu" else "pipeline",
              "pdf_info": [{"page_idx": i, "para_blocks": [{"type": "text"}],
                            **({} if office else {"page_size": [595, 842]})} for i in range(count)]}
    content = [{"type": "text", "page_idx": i, "text": "Invoice 12345"} for i in range(count)]
    content.extend({"type": "table", "page_idx": i, "table_body": "<table><tr><td>678.90</td></tr></table>"}
                   for i in (range(count) if suffix in {"pptx", "xlsx"} else (0,)))
    if suffix == "xlsx":
        content.extend({"type": "text", "page_idx": i, "text_level": 1, "text": title}
                       for i, title in enumerate(("Sales", "Summary")))
    pixels = BytesIO()
    Image.new("RGB", (12, 16), "white").save(pixels, format="PNG")
    content.append({"type": "image", "page_idx": 0, "img_path": "images/chart.png"})
    result = {"md_content": "# Invoice 12345\n\nTotal 678.90\n\n![Chart](images/chart.png)\n",
              "middle_json": json.dumps(middle), "content_list": json.dumps(content),
              "images": {"chart.png": "data:image/png;base64," + base64.b64encode(pixels.getvalue()).decode()}}
    return {"version": "3.4.5", "backend": "hybrid-engine" if kind == "gpu" else "pipeline",
            "results": {Path(filename).stem: result}}


@pytest.fixture
def api(monkeypatch, tmp_path):
    servers, workers, states = [], [], []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status, body):
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            state = self.server.state
            state["gets"] += 1
            self.send(state.get("health_http", 200), json.dumps({
                "status": state.get("health", "healthy"), "version": state.get("version", "3.4.5"),
                "protocol_version": state.get("protocol", 2), "queued_tasks": state.get("queue", 0)}).encode())

        def do_POST(self):
            state = self.server.state
            state["posts"] += 1
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            mime = BytesParser(policy=default).parsebytes(
                b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + raw)
            parts = {part.get_param("name", header="Content-Disposition"): part for part in mime.iter_parts()}
            file = parts["files"]
            state["uploads"].append((file.get_filename(), file.get_content_type(), file.get_payload(decode=True)))
            assert parts["backend"].get_payload(decode=True).decode() == mineru.BACKENDS[0 if state["kind"] == "gpu" else 1]
            value = response(file.get_filename(), state["kind"])
            if state.get("mutate"):
                state["mutate"](value)
            if state.get("hold"):
                state["entered"].set()
                assert state["hold"].wait(30)
            body = state.get("body", json.dumps(value).encode())
            self.send(state.get("http", 200), body)

    for kind in ("gpu", "cpu"):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.state = {"kind": kind, "gets": 0, "posts": 0, "uploads": []}
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        worker.start()
        states.append(server.state)
        servers.append(server)
        workers.append(worker)
    from runtime.config import load_runtime_config
    config = load_runtime_config({})
    config["mineru"]["endpoints"] = [f"http://127.0.0.1:{s.server_port}" for s in servers]
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(config))
    monkeypatch.setenv("MURAL_RUNTIME_CONFIG", str(path))
    derive = office.derive
    def office_result(source, temporary_root, info, **kwargs):
        if states[0].get("derive_office"):
            return derive(source, temporary_root, info, **kwargs)
        return {"derivation_version": office.DERIVATION_VERSION, "files": {}, "charts": [],
                "formulas": info.get("formulas", []), "unresolved_items": []}
    monkeypatch.setattr(office, "derive", office_result)
    try:
        yield states
    finally:
        for server, worker in zip(servers, workers):
            server.shutdown()
            server.server_close()
            worker.join(5)


@pytest.mark.parametrize("ext,granularity,count", [
    ("pdf", "page", 2), ("png", "image", 1), ("jpg", "image", 1),
    ("docx", "document_block", 1), ("pptx", "slide", 2), ("xlsx", "worksheet", 2),
])
def test_formats_upload_real_name_mime_bytes_and_format_locations(tmp_path, corpus, api, ext, granularity, count):
    source = corpus[ext]
    root, record = stage(tmp_path, source)
    assert record.text_path is None
    result = attachments.parse_attachment(root, record.attachment_id)
    assert result["status"] == ("partial" if ext == "xlsx" else "completed")
    assert result["backend"] == "mineru" and result["service_kind"] == "gpu"
    assert result["parser_backend"] == "hybrid-engine" and result["selected_endpoint_index"] == 0
    assert result["coverage"]["granularity"] == granularity
    assert len(result["coverage"]["returned_units"]) == count
    assert not result["failover"] and not result["cached"]
    filename, mime, body = api[0]["uploads"][0]
    assert filename == source.name and body == source.read_bytes()
    assert mime == mineru.source_info(source)["media_type"]
    assert api[1]["gets"] == api[1]["posts"] == 0
    assert "12345" in (root / result["text_path"]).read_text()
    assert json.loads((root / result["content_list_path"]).read_text())
    assert result["media_count"] == len(result["media_paths"])
    assert result["content_list_view"]["path"] == result["content_list_path"]
    view = attachments.attachment_content_view(root, result["content_list_path"])
    rows = [json.loads(line) for line in view["content"].splitlines()]
    first = rows[0]["source"]
    assert first["original_path"] == record.original_path and first["content_pointer"] == "/0"
    if ext == "pdf":
        assert first["page"] == 1 and first["page_image_path"] == record.page_image_paths[0]
    elif ext in {"png", "jpg"}:
        assert first["page_image_path"] == record.original_path and "page" not in first
    else:
        assert "page" not in first and "page_image_path" not in first
        assert first[granularity] == ("Sales" if ext == "xlsx" else 1)
    assert all(row["crop_box"] is None for row in rows)
    assert rows[-1]["img_path"] == result["media_paths"][0]
    markdown = (root / result["text_path"]).read_text()
    assert attachments.attachment_excerpt_media(
        root, result["text_path"], selected_indices=(), excerpt=markdown
    ) == [{"path": result["media_paths"][0]}]
    content_media = attachments.attachment_excerpt_media(
        root, result["content_list_path"], selected_indices=(0,), excerpt=view["content"].splitlines()[0]
    )
    assert content_media == [{"path": result["media_paths"][0], "page": 1}]
    with Image.open(root / result["media_paths"][0]) as image:
        assert image.size == (12, 16)
    attachments.AttachmentStager(root).stage([source])
    resumed = attachments.resolve_attachment_records(root)[0]
    assert resumed.text_path == result["text_path"] and tuple(result["media_paths"]) == resumed.media_paths
    assert attachments.parse_attachment(root, record.attachment_id)["cached"]
    assert api[0]["posts"] == 1


def test_content_view_keeps_raw_parse_and_uses_physical_page_full_paths_and_single_line_box(tmp_path, corpus, api, monkeypatch):
    root, record = stage(tmp_path, corpus["pdf"])
    def mutate(value):
        data = value["results"]["synthetic"]
        items = json.loads(data["content_list"])
        items[-1].update(page_idx=1, type="chart", bbox=[322, 176, 671, 732])
        data["content_list"] = json.dumps(items, indent=4)
    api[0]["mutate"] = mutate
    receipt = attachments.parse_attachment(root, record.attachment_id)
    before = {path: (root / path).read_bytes() for path in
              [receipt["content_list_path"], receipt["middle_json_path"], "_trace/attachment-manifest.json"]}
    view = attachments.attachment_content_view(root, receipt["content_list_path"])
    row = json.loads(view["content"].splitlines()[-1])
    assert row["type"] == "chart" and row["img_path"] == receipt["media_paths"][0]
    assert row["source"]["page"] == 2 and row["source"]["page_image_path"] == record.page_image_paths[1]
    assert row["crop_box"] == [0.322, 0.176, 0.671, 0.732]
    assert row["crop_source_path"] == record.page_image_paths[1] != row["img_path"]
    assert "bbox" not in row and "page_idx" not in row
    assert all((root / path).read_bytes() == raw for path, raw in before.items())
    assert not list((root / record.extracted_root).rglob("*.jsonl"))
    monkeypatch.setattr(mineru, "_request", lambda *_a, **_kw: pytest.fail("presentation must not reparse"))
    resumed = attachments.parse_attachment(root, record.attachment_id)
    assert resumed["cached"] and resumed["content_list_view"] == receipt["content_list_view"]
    assert resumed["coverage"]["locations"] == receipt["coverage"]["locations"]
    assert attachments.attachment_content_view(root, receipt["content_list_path"]) == view


@pytest.mark.parametrize("bbox", [None, [1, 2, 3], [322, 10, 176, 11, 671, 12, 732],
                                  [True, 0, 1, 2], [0, 0, float("nan"), 2],
                                  [-1, 0, 20, 30], [20, 0, 10, 30], [0, 0, 1200, 900], [0, 0, 10**400, 1000]])
def test_unreliable_crop_box_keeps_source_without_guessing(tmp_path, corpus, api, bbox):
    root, record = stage(tmp_path, corpus["pdf"])
    def mutate(value):
        data = value["results"]["synthetic"]
        items = json.loads(data["content_list"])
        items[-1]["bbox"] = bbox
        data["content_list"] = json.dumps(items)
        data["images"] = {}
    api[0]["mutate"] = mutate
    receipt = attachments.parse_attachment(root, record.attachment_id)
    view = attachments.attachment_content_view(root, receipt["content_list_path"])
    row = json.loads(view["content"].splitlines()[-1])
    assert receipt["status"] == "partial"
    assert row["img_path"] is None and row["crop_box"] is None and row["crop_source_path"] is None
    assert row["source"]["original_path"] == record.original_path
    assert row["source"]["page_image_path"] == record.page_image_paths[0]
    assert api[1]["gets"] == api[1]["posts"] == 0


def test_bound_content_view_rejects_changed_result_without_rewriting_it(tmp_path, corpus, api):
    root, record = stage(tmp_path, corpus["pdf"])
    receipt = attachments.parse_attachment(root, record.attachment_id)
    path = root / receipt["content_list_path"]
    path.write_text("[]")
    with pytest.raises(attachments.AttachmentError, match="attachment_content_changed"):
        attachments.attachment_content_view(root, receipt["content_list_path"])
    assert path.read_text() == "[]" and api[0]["posts"] == 1


def test_content_view_resolves_image_path_basename_like_the_parser(tmp_path, corpus, api):
    root, record = stage(tmp_path, corpus["pdf"])
    def mutate(value):
        data = value["results"]["synthetic"]
        items = json.loads(data["content_list"])
        items[-1].pop("img_path")
        items[-1]["image_path"] = "chart.png"
        data["content_list"] = json.dumps(items)
    api[0]["mutate"] = mutate
    receipt = attachments.parse_attachment(root, record.attachment_id)
    view = attachments.attachment_content_view(root, receipt["content_list_path"])
    row = json.loads(view["content"].split("\n")[-1])
    assert row["image_path"] == receipt["media_paths"][0]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500])
def test_input_auth_protocol_http_failures_do_not_switch(tmp_path, corpus, api, status):
    root, record = stage(tmp_path, corpus["pdf"])
    api[0].update(http=status, body=b"sk-secret-token sensitive document text")
    result = attachments.parse_attachment(root, record.attachment_id)
    assert result["status"] == "failed" and not result["failover"]
    assert result["unresolved_items"] == [{"reason": f"mineru_http_{status}"}]
    assert len(result["attempts"]) == 1 and api[1]["gets"] == 0
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("fault", ["unhealthy", 429, 502, 503, 504, "connection", "timeout"])
def test_infrastructure_switches_once_then_resume_cpu_without_reparse(tmp_path, corpus, api, monkeypatch, fault):
    root, record = stage(tmp_path, corpus["pdf"])
    request = mineru._request
    if fault == "unhealthy":
        api[0]["health"] = "unhealthy"
    elif type(fault) is int:
        api[0]["http"] = fault
    else:
        from runtime.config import load_runtime_config
        endpoint = load_runtime_config()["mineru"]["endpoints"][0]
        def fail(method, url, **kwargs):
            if url.startswith(endpoint):
                raise mineru.MinerUError("mineru_transport_" + fault, infrastructure=True)
            return request(method, url, **kwargs)
        monkeypatch.setattr(mineru, "_request", fail)
    events = []
    result = attachments.parse_attachment(root, record.attachment_id, event_sink=events.append)
    assert result["status"] == "completed" and result["service_kind"] == "cpu" and result["failover"]
    assert len(result["attempts"]) == 2 and api[1]["posts"] == 1
    assert [e["event_type"] for e in events if e["event_type"].startswith("mineru_")] == [
        "mineru_started", "mineru_failed", "mineru_failover", "mineru_started", "mineru_succeeded"]
    serialized = json.dumps(events)
    assert "http" not in serialized.replace("mineru_http_", "") and "Invoice" not in serialized
    first_posts = [s["posts"] for s in api]
    monkeypatch.setattr(mineru, "_request", lambda *_a, **_kw: pytest.fail("resume must reuse"))
    attachments.AttachmentStager(root).stage([corpus["pdf"]])
    again = attachments.parse_attachment(root, record.attachment_id)
    assert again["cached"] and again["text_path"] == result["text_path"]
    assert [s["posts"] for s in api] == first_posts


def test_both_failures_aggregate_safely_without_loop(tmp_path, corpus, api):
    root, record = stage(tmp_path, corpus["pdf"])
    api[0].update(http=503, body=b"secret GPU body")
    api[1].update(http=504, body=b"secret CPU body")
    result = attachments.parse_attachment(root, record.attachment_id)
    assert result["status"] == "failed" and result["failover"]
    assert [a["reason"] for a in result["attempts"]] == ["mineru_http_503", "mineru_http_504"]
    assert [s["posts"] for s in api] == [1, 1] and "secret" not in json.dumps(result)


@pytest.mark.parametrize("fault", ["json", "schema", "version", "protocol", "mime", "bytes", "path", "binding", "location"])
def test_invalid_remote_contract_is_not_infrastructure(tmp_path, corpus, api, fault):
    root, record = stage(tmp_path, corpus["pdf"])
    if fault == "json":
        api[0]["body"] = b"invalid response secret"
    elif fault in {"version", "protocol"}:
        api[0][fault] = "unexpected"
    else:
        def mutate(value):
            data = value["results"]["synthetic"]
            if fault == "schema":
                data["middle_json"] = "[]"
            elif fault == "mime":
                data["images"]["chart.png"] = data["images"]["chart.png"].replace("image/png", "image/jpeg")
            elif fault == "bytes":
                data["images"]["chart.png"] = "data:image/png;base64,YWJj"
            elif fault == "path":
                data["images"]["../../escape.png"] = data["images"].pop("chart.png")
            elif fault == "binding":
                value["results"] = {"wrong": data}
            else:
                items = json.loads(data["content_list"])
                items[0]["page_idx"] = 99
                data["content_list"] = json.dumps(items)
        api[0]["mutate"] = mutate
    result = attachments.parse_attachment(root, record.attachment_id)
    assert result["status"] == "failed" and not result["failover"] and api[1]["gets"] == 0


def test_partial_missing_image_is_saved_and_original_page_still_available(tmp_path, corpus, api):
    root, record = stage(tmp_path, corpus["pdf"])
    api[0]["mutate"] = lambda value: value["results"]["synthetic"].update(images={})
    result = attachments.parse_attachment(root, record.attachment_id)
    assert result["status"] == "partial" and api[1]["gets"] == 0
    assert result["unresolved_items"] == [{"reason": "mineru_referenced_image_missing", "path": "images/chart.png"}]
    page = attachments.materialize_page_image(root, result["page_image_paths"][0])
    assert page.is_file() and (root / record.original_path).read_bytes() == corpus["pdf"].read_bytes()
    again = attachments.parse_attachment(root, record.attachment_id)
    assert again["cached"] and again["status"] == "partial" and api[0]["posts"] == 1


def test_cache_hash_parameters_and_backend_identity(tmp_path, corpus, api, monkeypatch):
    root, record = stage(tmp_path, corpus["pdf"])
    first = attachments.parse_attachment(root, record.attachment_id)
    (root / first["content_list_path"]).write_text("damaged")
    second = attachments.parse_attachment(root, record.attachment_id)
    assert not second["cached"] and second["text_path"] != first["text_path"]
    assert (root / first["middle_json_path"]).is_file()
    monkeypatch.setattr(mineru, "PARAMETERS", {**mineru.PARAMETERS, "table_enable": "false"})
    api[0]["http"] = 503
    third = attachments.parse_attachment(root, record.attachment_id)
    assert third["service_kind"] == "cpu" and third["text_path"] != second["text_path"]
    manifests = json.loads((root / "_trace/attachment-manifest.json").read_text())["records"][0]["parse"]
    identities = [entry["identity"] for entry in manifests["results"].values()]
    assert len(identities) == 2 and {i["service_kind"] for i in identities} == {"gpu", "cpu"}
    api[0]["http"] = 200
    fresh_root, fresh = stage(tmp_path / "new", corpus["pdf"])
    assert attachments.parse_attachment(fresh_root, fresh.attachment_id)["service_kind"] == "gpu"


def test_healthy_queue_is_not_failure(tmp_path, corpus, api):
    root, record = stage(tmp_path, corpus["pdf"])
    api[0]["queue"] = 9
    assert attachments.parse_attachment(root, record.attachment_id)["service_kind"] == "gpu"
    assert api[1]["gets"] == 0


@pytest.mark.parametrize("suffix", sorted(attachments.DIRECT_TEXT_SUFFIXES))
def test_direct_text_remains_byte_exact_and_never_calls_service(tmp_path, monkeypatch, suffix):
    source = tmp_path / ("original" + suffix)
    raw = b"\xef\xbb\xbfHeader\r\n12345,\xe6\xb5\x8b\xe8\xaf\x95\r\n"
    source.write_bytes(raw)
    root, record = stage(tmp_path, source)
    monkeypatch.setattr(mineru, "_request", lambda *_a, **_kw: pytest.fail("direct text must not call MinerU"))
    receipt = attachments.parse_attachment(root, record.attachment_id)
    assert receipt["backend"] == "direct_text" and receipt["status"] == "completed"
    assert (root / receipt["text_path"]).read_bytes() == raw


@pytest.mark.parametrize("suffix", [".doc", ".ppt", ".xls", ".html", ".webp"])
def test_unverified_formats_not_silently_converted(tmp_path, monkeypatch, suffix):
    source = tmp_path / ("input" + suffix)
    source.write_bytes(b"unsupported input")
    root, record = stage(tmp_path, source)
    monkeypatch.setattr(mineru, "_request", lambda *_a, **_kw: pytest.fail("unsupported"))
    result = attachments.parse_attachment(root, record.attachment_id)
    assert result["status"] == "failed" and result["unresolved_items"] == [{"reason": "unsupported_format"}]


def test_mime_mismatch_and_changed_original_rejected_before_health(tmp_path, corpus, api):
    source = tmp_path / "mislabeled.png"
    source.write_bytes(corpus["jpg"].read_bytes())
    root, record = stage(tmp_path, source)
    result = attachments.parse_attachment(root, record.attachment_id)
    assert result["status"] == "failed" and api[0]["gets"] == 0
    (root / record.original_path).write_bytes(b"mutated")
    with pytest.raises(attachments.AttachmentError, match="身份"):
        attachments.parse_attachment(root, record.attachment_id)


@pytest.mark.parametrize("value", [
    "", "http://gpu", "http://gpu,http://gpu/", "ftp://gpu,http://cpu",
    "http://user:pass@gpu,http://cpu", "http://gpu?key=secret,http://cpu",
    "http://gpu#,http://cpu", "http://gpu:wrong,http://cpu", "http://g pu,http://cpu",
    "http://gpu,http://cpu,http://third",
])
def test_strict_endpoints(value, tmp_path):
    from runtime.config import load_runtime_config
    config = load_runtime_config({})
    config["mineru"]["endpoints"] = value.split(",")
    path = tmp_path / "invalid-runtime.json"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        load_runtime_config({"MURAL_RUNTIME_CONFIG": str(path)})


def test_endpoint_validation_delegates_to_shared_service_url():
    from runtime.config import service_url
    assert mineru._endpoint("http://gpu/") == service_url("http://gpu/")


@pytest.mark.parametrize("failure,infrastructure", [
    (requests.ConnectTimeout("secret"), True), (requests.ReadTimeout("secret"), True),
    (requests.ConnectionError("secret"), True), (requests.exceptions.SSLError("secret"), False),
])
def test_transport_classifier_never_echoes_exception(monkeypatch, failure, infrastructure):
    def fail(*_args, **_kwargs):
        raise failure
    monkeypatch.setattr(requests.Session, "request", fail)
    with pytest.raises(mineru.MinerUError) as caught:
        mineru._request("GET", "http://private.invalid")
    assert caught.value.infrastructure == infrastructure and "secret" not in str(caught.value)


def test_no_full_extraction_at_stage(tmp_path, corpus, monkeypatch):
    probes = []
    get_text = pymupdf.Page.get_text
    def probe_text_layer(page, option="text", **kwargs):
        assert option == "text" and not kwargs
        probes.append(page.number)
        return get_text(page, option)
    monkeypatch.setattr(pymupdf.Page, "get_text", probe_text_layer)
    monkeypatch.setattr(pymupdf.Page, "get_textpage_ocr", lambda *_a, **_kw: pytest.fail("startup OCR"))
    monkeypatch.setattr(pymupdf.Page, "get_image_info", lambda *_a, **_kw: pytest.fail("startup image layout"))
    monkeypatch.setattr(pymupdf.Page, "get_drawings", lambda *_a, **_kw: pytest.fail("startup drawing layout"))
    monkeypatch.setattr(pymupdf.Page, "get_pixmap", lambda *_a, **_kw: pytest.fail("startup rasterization"))
    monkeypatch.setattr(office, "derive", lambda *_a, **_kw: pytest.fail("startup conversion"))
    monkeypatch.setattr(mineru, "_request", lambda *_a, **_kw: pytest.fail("startup parser"))
    for ext, source in corpus.items():
        root, record = stage(tmp_path / ext, source)
        assert not record.text_path and not list(root.rglob("page_*.png"))
        if ext == "pdf":
            for relative, present in zip(record.page_image_paths, (True, False)):
                descriptor = json.loads((root / relative).with_suffix(".json").read_text())
                assert descriptor["native_text_present"] is present
                assert "native_text" not in descriptor and descriptor["text_blocks"] == []
        before = probes[:]
        attachments.AttachmentStager(root).stage([source])
        assert probes == before
    assert probes == [0, 1]


def test_real_child_trace_is_visible_before_parse_returns(tmp_path, api, corpus):
    import os
    import subprocess
    import sys
    root, record = stage(tmp_path, corpus["pdf"])
    api[0]["http"] = 503
    api[1].update(entered=threading.Event(), hold=threading.Event())
    code = '''import json,sys
from pathlib import Path
from runtime.attachments import parse_attachment
from runtime.live_trace import LiveEventJournal
journal=LiveEventJournal(Path(sys.argv[1]) / "_trace/live-events.jsonl")
def emit(event):
    if event["event_type"].startswith("mineru_"):
        journal.append(event_type=event["event_type"],agent_id="harness-attachments",role="harness",payload=event)
print(json.dumps(parse_attachment(sys.argv[1],sys.argv[2],event_sink=emit)))
'''
    command = [sys.executable, "-c", code, str(root), record.attachment_id]
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    child = subprocess.Popen(command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    duplicate = None
    try:
        assert api[1]["entered"].wait(20)
        assert child.poll() is None
        rows = [json.loads(line) for line in (root / "_trace/live-events.jsonl").read_text().splitlines()]
        assert [row["event_type"] for row in rows] == [
            "mineru_started", "mineru_failed", "mineru_failover", "mineru_started"]
        assert all(row["agent_id"] == "harness-attachments" and row["role"] == "harness" for row in rows)
        duplicate = subprocess.Popen(command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        api[1]["hold"].set()
        stdout, stderr = child.communicate(timeout=20)
        assert child.returncode == 0, stderr
        result = json.loads(stdout)
        stdout, stderr = duplicate.communicate(timeout=20)
        assert duplicate.returncode == 0, stderr
        resumed = json.loads(stdout)
        assert resumed["cached"] and resumed["text_path"] == result["text_path"]
    finally:
        api[1]["hold"].set()
        for process in (child, duplicate):
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(5)
    assert result["service_kind"] == "cpu" and result["failover"]
    rows = [json.loads(line) for line in (root / "_trace/live-events.jsonl").read_text().splitlines()]
    assert rows[-1]["event_type"] == "mineru_succeeded"
    assert [row["sequence"] for row in rows] == list(range(1, 6))
    assert len({row["event_id"] for row in rows}) == 5
    assert api[0]["posts"] == api[1]["posts"] == 1
    assert all("http://" not in json.dumps(row) and "Invoice" not in json.dumps(row) for row in rows)


@pytest.mark.parametrize("override", [{"endpoint": "http://unauthorized.invalid"}, {"workers": 4}])
def test_internal_parse_does_not_expose_endpoint_or_worker_overrides(tmp_path, override):
    with pytest.raises(TypeError):
        attachments.parse_attachment(tmp_path, "att-000000000000", **override)


def test_production_does_not_keep_old_variable_or_local_rich_parser():
    root = Path(__file__).resolve().parents[3]
    sources = [* (root / "harnesses/mural-presenter-v1/runtime").glob("*.py"),
               root / "webui/studio/app/service_config.py",
               * (root / "skills/mural-presenter-v1").rglob("*.md")]
    combined = "\n".join(path.read_text() for path in sources)
    for forbidden in ("MURAL_MINERU_" + "URL", "def parse_" + "pdf(", "native+" + "rapidocr",
                      "OCR is pending — read the page images", "material.py " + "ocr", "--pages " + "all"):
        assert forbidden not in combined
    assert not hasattr(attachments, "ocr_attachment_pages")
