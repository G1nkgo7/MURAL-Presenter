"""直接编辑的源码保真、权限、版本冲突和渲染回执边界。"""
import asyncio
import base64
import io
import json
from pathlib import Path
import subprocess

from PIL import Image
import pytest

from app import main, page_editor
from test_delivery_download import download_case


SOURCE = '''<!-- keep comment -->
<section class="slide" data-slide="01">
<h1 id="title" style="font-weight:700;background:url('data:image/svg+xml;a;b');">Old 标题</h1>
<div id="group"><p>one</p><span>two</span></div>
<img id="photo" src="../assets/photo.png" srcset="../assets/photo-2.png 2x">
<canvas id="chart" width="200" height="100"></canvas>
<script>const data = [2,5]; window.keep = "<fake>;&";</script>
</section>'''


def node(source, identity):
    return next(item["index"] for item in page_editor.SourceDocument(source).nodes if dict(item["attrs"]).get("id")==identity)


def test_incremental_save_preserves_scripts_sources_weight_and_other_elements():
    assert page_editor.apply_changes(SOURCE,[])==SOURCE
    updated=page_editor.apply_changes(SOURCE,[{"node":node(SOURCE,"title"),"text":"New <标题>","styles":{"font-size":"52px"}}])
    assert updated.split("</h1>",1)[1] == SOURCE.split("</h1>",1)[1]
    assert "New &lt;标题&gt;" in updated
    assert "font-weight:700" in updated
    assert "data:image/svg+xml;a;b" in updated
    assert "font-size: 52px !important" in updated
    assert "data-mural-source" not in updated
    again=page_editor.apply_changes(updated,[{"node":node(updated,"title"),"styles":{"font-size":"48px"}}])
    assert again.count("font-size:")==1 and "48px" in again


@pytest.mark.parametrize("changes",[
    [{"node":True,"text":"x"}], [{"node":999,"text":"x"}],
    [{"node":node(SOURCE,"group"),"text":"delete descendants"}],
    [{"node":node(SOURCE,"chart"),"src":"data:image/png;base64,AA=="}],
    [{"node":node(SOURCE,"title"),"styles":{"background":"url(https://evil.test)"}}],
    [{"node":node(SOURCE,"title"),"styles":{"transform":"url(https://evil.test)"}}],
    [{"node":node(SOURCE,"title"),"styles":{"width":"-2px"}}],
    [{"node":node(SOURCE,"title"),"styles":{"opacity":"2"}}],
    [{"node":node(SOURCE,"photo"),"src":"https://evil.test/image.png"}],
    [{"node":node(SOURCE,"group"),"delete":True},{"node":3,"styles":{"width":"200px"}}],
])
def test_invalid_edits_are_rejected_without_rewriting(changes):
    with pytest.raises(page_editor.EditError):
        page_editor.apply_changes(SOURCE,changes)


def test_metadata_is_preview_only_and_script_is_not_editable():
    document=page_editor.SourceDocument(SOURCE)
    annotated=document.annotated()
    assert 'data-mural-source="' in annotated
    assert document.source==SOURCE
    index=next(item["index"] for item in document.nodes if item["tag"]=="script")
    with pytest.raises(page_editor.EditError):
        page_editor.apply_changes(SOURCE,[{"node":index,"delete":True}])
    assert not any('data-mural-source' in part for part in [annotated.split('<script>')[1].split('</script>')[0]])


def test_image_replacement_checks_real_mime_and_preserves_other_sources():
    buffer=io.BytesIO()
    Image.new("RGB",(32,24),"red").save(buffer,"JPEG")
    encoded=base64.b64encode(buffer.getvalue()).decode()
    updated=page_editor.apply_changes(SOURCE,[{"node":node(SOURCE,"photo"),"src":"data:image/jpeg;base64,"+encoded}])
    assert 'srcset=' not in updated and 'data:image/jpeg;base64,' in updated
    assert '<canvas id="chart"' in updated and '<script>' in updated
    with pytest.raises(page_editor.EditError):
        page_editor.apply_changes(SOURCE,[{"node":node(SOURCE,"photo"),"src":"data:image/png;base64,"+encoded}])


@pytest.fixture
def edit_case(download_case, monkeypatch):
    root=download_case["root"]
    (root/"slides").mkdir()
    (root/"renders").mkdir()
    (root/"base.css").write_text(":root{--canvas-w:800px;--canvas-h:450px}")
    (root/"slides/slide_01.html").write_text(SOURCE)
    Image.new("RGB",(800,450),"white").save(root/"renders/slide_01.png")
    monkeypatch.setattr(main.jobs,"_engine_alive",lambda *_:False)
    return download_case


def request(path, method="GET", payload=None, origin=None, content_type="application/json"):
    async def run():
        messages=[]
        async def receive():
            return {"type":"http.request","body":json.dumps(payload).encode() if payload is not None else b"","more_body":False}
        async def send(message):
            messages.append(message)
        headers=[(b"host",b"editor.test"),(b"content-type",content_type.encode())]
        if origin:
            headers.append((b"origin",origin.encode()))
        await main.app({"type":"http","asgi":{"version":"3.0"},"method":method,"path":path,"raw_path":path.encode(),
                        "query_string":b"","headers":headers,"server":("editor.test",80),"client":("127.0.0.1",1),"scheme":"http","root_path":""},receive,send)
        status=next(item["status"] for item in messages if item["type"]=="http.response.start")
        return status,json.loads(b"".join(item.get("body",b"") for item in messages if item["type"]=="http.response.body"))
    return asyncio.run(run())


def test_source_endpoint_is_fresh_and_read_only(edit_case):
    root=edit_case["root"]
    before={path:path.read_bytes() for path in root.rglob("*") if path.is_file()}
    status,result=request("/api/decks/1/pages/1/edit")
    assert status==200 and 'data-mural-source="' in result["html"]
    assert '<base href="/api/decks/1/files/">' in result["html"]
    assert 'src="assets/photo.png"' in result["html"]
    assert result["version"]==page_editor.version(SOURCE,(root/"base.css").read_text())
    assert before=={path:path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_only_owner_completed_deck_and_safe_requests_can_edit(edit_case, monkeypatch):
    assert request("/api/decks/2/pages/1/edit")[0]==404
    assert request("/api/decks/1/pages/0/edit")[0]==400
    assert request("/api/decks/1/pages/2/edit")[0]==404
    assert request("/api/decks/1/pages/1/edit","POST",{},origin="http://evil.test")[0]==403
    assert request("/api/decks/1/pages/1/edit","POST",{},content_type="text/plain")[0]==415
    edit_case["con"].execute("UPDATE decks SET status='running'")
    assert request("/api/decks/1/pages/1/edit")[0]==409
    edit_case["con"].execute("UPDATE decks SET status='completed',user_id=88")
    assert request("/api/decks/1/pages/1/edit")[0]==404


def test_stale_or_invalid_save_does_not_start_renderer(edit_case, monkeypatch):
    def forbidden(*args,**kwargs):
        pytest.fail("invalid request must not invoke rendering")
    monkeypatch.setattr(page_editor.subprocess,"run",forbidden)
    assert request("/api/decks/1/pages/1/edit","POST",{"version":"old","changes":[]})[0]==409
    _,source=request("/api/decks/1/pages/1/edit")
    assert request("/api/decks/1/pages/1/edit","POST",{"version":source["version"],"changes":[{"node":999,"text":"x"}]})[0]==400
    (edit_case["root"]/"base.css").write_text("changed shared style")
    assert request("/api/decks/1/pages/1/edit","POST",{"version":source["version"],"changes":[]})[0]==409


def test_symlink_to_another_page_is_not_writable(edit_case):
    root=edit_case["root"]
    (root/"slides/slide_02.html").symlink_to(root/"slides/slide_01.html")
    assert request("/api/decks/1/pages/2/edit")[0]==409


def test_save_reports_render_failure_without_claiming_new_png(edit_case, monkeypatch):
    _,source=request("/api/decks/1/pages/1/edit")
    monkeypatch.setattr(main.jobs,"_offline_runtime",lambda root:("python",{}))
    called=[]
    def renderer(command,**kwargs):
        called.append(json.loads(kwargs["input"]))
        return subprocess.CompletedProcess(command,0,json.dumps({"saved":True,"rendered":False,"error":"render failed","page":1}),"")
    monkeypatch.setattr(page_editor.subprocess,"run",renderer)
    status,result=request("/api/decks/1/pages/1/edit","POST",{"version":source["version"],"changes":[{"node":node(SOURCE,"title"),"text":"Changed"}]})
    assert status==200 and result["saved"] is True and result["rendered"] is False
    assert called[0]["user_id"]==7 and called[0]["page"]==1
    assert called[0]["workspace"]==str(edit_case["root"])
    page_editor.require_idle(1)


def test_busy_deck_rejects_other_editors_and_exports(edit_case):
    with page_editor.saving(1):
        assert request("/api/decks/1/pages/1/edit")[0]==409
        assert request("/api/decks/1/pages/1/edit","POST",{})[0]==409
        with pytest.raises(page_editor.EditError):
            main._export_delivery({"id":1},"pdf")
        assert request('/api/decks/1/continue','POST',{"message":"edit"})[0]==409
        assert request('/api/decks/1','DELETE')[0]==409
    assert request("/api/decks/1/pages/1/edit")[0]==200
