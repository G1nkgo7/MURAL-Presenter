"""编辑画布保持隔离；保存只发送原源码对象的增量，不上传扁平化的截图 DOM。"""
import base64
import io
import json
import os
from pathlib import Path
import re
import subprocess

import pytest
from PIL import Image
from playwright.sync_api import sync_playwright
from app.page_editor import SourceDocument

STUDIO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def browser(tmp_path_factory):
    from app import engine
    scratch = tmp_path_factory.mktemp("page-editor-browser")
    with pytest.MonkeyPatch.context() as patch:
        for name in ("TMPDIR", "TMP", "TEMP"):
            patch.setenv(name, str(scratch))
        env = engine.render_env(dict(os.environ))
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                executable_path=env["PPT_SKILL_BROWSER_EXE"], env=env,
                headless=True, args=["--no-sandbox"], timeout=10000,
                downloads_path=str(scratch),
            )
            yield browser
            browser.close()


@pytest.fixture
def editor(browser):
    context = browser.new_context(viewport={"width": 1380, "height": 920}, accept_downloads=True)
    page = context.new_page()
    page.route("http://editor.test/",lambda route:route.fulfill(body="<html><body></body></html>",content_type="text/html"))
    page.goto("http://editor.test/")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    png = io.BytesIO()
    Image.new("RGBA", (60, 40), (0, 155, 115, 255)).save(png, "PNG")
    image = "data:image/png;base64," + base64.b64encode(png.getvalue()).decode()
    source = f"""<!doctype html><html><head><style>
      body{{margin:0}} .slide{{position:relative;width:800px;height:450px;background:#fff4dd}}
      h1{{position:absolute;left:60px;top:35px;margin:0;color:#254732;font:700 38px Arial}}
      h1::before{{content:'●';margin-right:12px;color:#006644}}
      #photo{{position:absolute;left:420px;top:130px;width:240px;height:160px;transform:rotate(5deg)}}
      #group{{position:absolute;left:60px;top:140px;width:260px;display:flex;gap:12px}}
      #group p{{margin:0;font:20px Arial}} #chart{{position:absolute;left:60px;top:260px}}
      </style></head><body><section class="slide active" data-slide="1" id="s1">
      <h1 id="heading" onclick="window.bad=true">低碳生活</h1><div id="group"><p id="left">步行</p><p id="right">骑行</p></div>
      <img id="photo" src="{image}" alt="透明图片"><canvas id="chart" width="100" height="60"></canvas>
      <script>document.getElementById('chart').getContext('2d').fillRect(0,0,60,40)</script>
      </section></body></html>"""
    page.route("**/api/decks/41/pages/1/edit",lambda route:route.fulfill(
        json={"version":"source-version", "html":SourceDocument(source).annotated()}
        if route.request.method=="GET" else {"saved":True,"rendered":True,"page":1,"stamp":123}))
    page.set_content('<style>:root{--ui-scale:1.08;--surface:white;--fg:#202020;--muted:#666;--acc:#7652d7;--hairline:#ddd;--sans:Arial}body{zoom:var(--ui-scale);margin:0}</style><button id="open">编辑</button><iframe id="source" style="width:800px;height:450px"></iframe>')
    page.add_style_tag(path=str(STUDIO / "static/page-editor.css"))
    page.add_script_tag(path=str(STUDIO / "static/page-editor.js"))
    page.locator("#source").evaluate("(frame,html)=>frame.srcdoc=html", source)
    page.frame_locator("#source").locator("#photo").wait_for()
    page.wait_for_function("document.querySelector('#source').contentDocument.readyState==='complete'")
    page.evaluate("MuralPageEditor.open({page:1,deckId:41,trigger:document.querySelector('#open'),onSaved:r=>window.savedPage=r})")
    canvas = page.frame_locator(".page-edit-dialog iframe")
    canvas.locator("[data-mural-page]").wait_for()
    page.wait_for_function("document.querySelector('[data-editor-layers]').children.length>0")
    yield page, canvas, errors
    context.close()


def edit_field(page, name, value):
    field = page.locator(f'[data-prop="{name}"]')
    field.fill(str(value))
    field.dispatch_event("change")


def test_text_edit_undo_redo_preserves_source_and_inline_children(editor):
    page, canvas, errors = editor
    original = page.locator("#source").evaluate("f=>f.contentDocument.documentElement.outerHTML")
    canvas.locator("#heading").dblclick()
    assert page.locator("[data-prop=text]").evaluate("e=>e===document.activeElement")
    edit_field(page, "text", "一天少开一次车")
    assert canvas.locator("#heading").inner_text() == "一天少开一次车"
    page.locator("[data-editor-undo]").click()
    assert canvas.locator("#heading").inner_text() == "低碳生活"
    page.locator("[data-editor-redo]").click()
    assert canvas.locator("#heading").inner_text() == "一天少开一次车"
    edit_field(page, "text", "")
    page.locator("[data-editor-undo]").click()
    page.locator("[data-editor-redo]").click()
    assert canvas.locator("#heading").inner_text() == ""
    canvas.locator("#left").click()
    page.locator("[data-editor-parent]").click()
    assert page.locator("[data-editor-text]").is_hidden()
    assert canvas.locator("#group p").count() == 2
    assert page.locator("#source").evaluate("f=>f.contentDocument.documentElement.outerHTML") == original
    assert not errors


def test_drag_scaling_rotation_resize_color_and_delete(editor):
    page, canvas, errors = editor
    canvas.locator("#photo").click()
    before = canvas.locator("#photo").evaluate("e=>e.getBoundingClientRect().toJSON()")
    box = canvas.locator("#photo").bounding_box()
    scale = page.locator(".page-edit-dialog iframe").evaluate("e=>e.getBoundingClientRect().width/e.clientWidth")
    x, y = box["x"]+box["width"]/2, box["y"]+box["height"]/2
    page.mouse.move(x,y)
    page.mouse.down()
    page.mouse.move(x+45,y+24,steps=4)
    page.mouse.up()
    after = canvas.locator("#photo").evaluate("e=>e.getBoundingClientRect().toJSON()")
    assert after["left"]-before["left"] == pytest.approx(45/scale,abs=2)
    assert after["top"]-before["top"] == pytest.approx(24/scale,abs=2)
    assert "matrix(" in canvas.locator("#photo").evaluate("e=>e.style.transform")
    page.locator("[data-editor-undo]").click()
    assert canvas.locator("#photo").evaluate("e=>e.getBoundingClientRect().left") == pytest.approx(before["left"], abs=.1)
    edit_field(page,"width",300)
    edit_field(page,"r",12)
    edit_field(page,"opacity",.4)
    assert canvas.locator("#photo").evaluate("e=>getComputedStyle(e).width") == "300px"
    assert canvas.locator("#photo").evaluate("e=>getComputedStyle(e).opacity") == "0.4"
    canvas.locator("#heading").click()
    edit_field(page,"color","#cc3300")
    assert canvas.locator("#heading").evaluate("e=>getComputedStyle(e).webkitTextFillColor") == "rgb(204, 51, 0)"
    page.locator("[data-editor-delete]").click()
    assert canvas.locator("#heading").count() == 0
    page.locator("[data-editor-undo]").click()
    assert canvas.locator("#heading").count() == 1
    assert not errors


def test_snapshot_keeps_canvas_pseudo_elements_and_disables_scripts(editor):
    page, canvas, errors = editor
    assert canvas.locator("canvas,script,[onclick]").count() == 0
    assert canvas.locator("#chart").get_attribute("src").startswith("data:image/png;base64,")
    assert canvas.locator("#heading").evaluate("e=>getComputedStyle(e,'::before').content") == '"●"'
    assert page.locator(".page-edit-dialog iframe").get_attribute("sandbox") == "allow-same-origin"
    assert "1 个画布图表" in page.locator("[data-editor-status]").inner_text()
    assert not errors


def test_download_is_offline_current_page_without_mutating_deck(editor, tmp_path):
    page, canvas, errors = editor
    requests = []
    page.on("request", lambda request: requests.append((request.method,request.url)))
    canvas.locator("#heading").click()
    edit_field(page,"text","编辑后的页面")
    with page.expect_download() as downloaded:
        page.locator("[data-editor-export]").click()
    download=downloaded.value
    assert download.suggested_filename == "deck-41-page-1-edited.html"
    output=tmp_path / download.suggested_filename
    download.save_as(output)
    html=output.read_text()
    assert "编辑后的页面" in html and "data:image/png;base64," in html
    assert "data-mural-page" not in html and "<script" not in html and "onclick=" not in html
    assert not [url for method,url in requests if method != "GET"]
    assert not [url for _,url in requests if "/api/" in url]
    preview=page.context.new_page()
    preview.route("**/*", lambda route: route.abort() if not route.request.url.startswith("file:") else route.continue_())
    preview.goto(output.as_uri())
    assert preview.locator("#heading").inner_text() == "编辑后的页面"
    assert preview.locator("#s1").evaluate("e=>[e.offsetWidth,e.offsetHeight]") == [800,450]
    assert preview.locator("#chart").evaluate("e=>e.complete&&e.naturalWidth>0")
    assert "尚未保存的修改" in page.locator("[data-editor-status]").inner_text()
    assert "未保存" in page.locator("[data-editor-changed]").inner_text()
    preview.close()
    assert not errors


def test_dirty_close_and_invalid_numeric_inputs(editor):
    page,canvas,errors=editor
    canvas.locator("#heading").click()
    edit_field(page,"width","")
    assert not page.locator("[data-editor-undo]").is_enabled()
    edit_field(page,"x",50)
    assert page.locator("[data-editor-undo]").is_enabled()
    page.once("dialog", lambda dialog: dialog.dismiss())
    page.locator("[data-editor-close]").click()
    assert page.locator(".page-edit-dialog").is_visible()
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("[data-editor-close]").click()
    assert page.locator(".page-edit-dialog").count()==0
    assert page.locator("#open").evaluate("e=>e===document.activeElement")
    assert not errors


def test_image_replacement_and_missing_resource_export_error(editor):
    page,canvas,errors=editor
    canvas.locator("#photo").click()
    before=canvas.locator("#photo").get_attribute("src")
    image=io.BytesIO()
    Image.new("RGB",(80,50),(240,50,20)).save(image,"JPEG")
    page.locator("[data-editor-image]").set_input_files({"name":"replacement.jpg","mimeType":"image/jpeg","buffer":image.getvalue()})
    page.wait_for_function("document.querySelector('[data-editor-status]').textContent.includes('已替换编辑画布')")
    assert canvas.locator("#photo").get_attribute("src").startswith("data:image/jpeg;")
    page.locator("[data-editor-undo]").click()
    assert canvas.locator("#photo").get_attribute("src")==before
    page.route("https://missing.test/image.png",lambda route:route.fulfill(status=404,headers={"Access-Control-Allow-Origin":"*"}))
    canvas.locator("#photo").evaluate("e=>e.src='https://missing.test/image.png'")
    page.locator("[data-editor-export]").click()
    page.wait_for_function("document.querySelector('[data-editor-status]').textContent.includes('资源读取失败')")
    assert page.locator("[data-editor-export]").is_enabled()
    assert not errors


def test_close_commits_text_even_before_field_blurs(editor):
    page,canvas,errors=editor
    canvas.locator("#heading").dblclick()
    page.locator("[data-prop=text]").fill("还没有失焦的修改")
    page.once("dialog",lambda dialog:dialog.dismiss())
    assert page.evaluate("MuralPageEditor.close()") is False
    assert canvas.locator("#heading").inner_text()=="还没有失焦的修改"
    assert page.locator(".page-edit-dialog").is_visible()
    assert not errors


def test_mobile_dialog_stays_inside_viewport(editor, tmp_path):
    page,canvas,errors=editor
    page.set_viewport_size({"width":420,"height":820})
    page.wait_for_timeout(100)
    box=page.locator(".page-edit-dialog").bounding_box()
    assert box["x"]>=0 and box["x"]+box["width"]<=421
    assert box["y"]>=0 and box["y"]+box["height"]<=821
    assert page.locator(".page-edit-dialog").evaluate("e=>e.scrollWidth<=e.clientWidth")
    page.screenshot(path=str(tmp_path / "page-editor-mobile.png"))
    assert not errors


def test_frontend_wiring_and_source_syntax():
    script=(STUDIO / "static/app.js").read_text()
    template=(STUDIO / "templates/app.html").read_text()
    assert 'id="page-edit-open"' in template
    assert "page-editor.js?v={{ asset_ver }}" in template
    assert "page-editor.css?v={{ asset_ver }}" in template
    assert 'window.MuralPageEditor.open({page, deckId, trigger:button, onSaved:' in script
    assert 'pageEdit.disabled = trajectoryMode || ed.kind !== "static" || ed.status !== "completed"' in script
    editor=(STUDIO / "static/page-editor.js").read_text()
    assert 'method:"POST",credentials:"same-origin"' in editor
    assert 'version:current.capture.version,changes:updates' in editor
    subprocess.run(["node","--check",str(STUDIO / "static/page-editor.js")],check=True,capture_output=True)


def test_save_sends_only_source_changes_and_receives_new_png(editor):
    page,canvas,errors=editor
    canvas.locator("#heading").click()
    edit_field(page,"text","原 HTML 中的新标题")
    edit_field(page,"font-size",44)
    canvas.locator("#photo").click()
    edit_field(page,"x",24)
    with page.expect_request(lambda request:request.method=="POST") as posted:
        page.locator("[data-editor-save]").click()
    payload=posted.value.post_data_json
    assert payload["version"]=="source-version"
    assert len(payload["changes"])==2
    assert payload["changes"][0]["text"]=="原 HTML 中的新标题"
    assert payload["changes"][0]["styles"]["font-size"]=="44px"
    assert "transform" in payload["changes"][1]["styles"]
    assert "html" not in payload and "css" not in payload
    assert "data:image" not in json.dumps(payload)
    page.wait_for_function("window.savedPage?.rendered===true")
    assert page.locator(".page-edit-dialog").count()==0
    assert not errors


def test_delete_group_is_one_patch_and_chart_cannot_be_replaced(editor):
    page,canvas,errors=editor
    canvas.locator("#chart").click()
    assert page.locator("[data-editor-replace]").is_hidden()
    canvas.locator("#left").click()
    page.locator("[data-editor-parent]").click()
    page.locator("[data-editor-delete]").click()
    with page.expect_request(lambda request:request.method=="POST") as posted:
        page.locator("[data-editor-save]").click()
    assert len(posted.value.post_data_json["changes"])==1
    assert posted.value.post_data_json["changes"][0]["delete"] is True
    assert not errors


def test_source_conflict_keeps_unsaved_work(editor):
    page,canvas,errors=editor
    page.route("**/api/decks/41/pages/1/edit",lambda route:route.fulfill(status=409,json={"detail":"原页面已改变，请重新打开"}))
    canvas.locator("#heading").click()
    edit_field(page,"text","保留待处理修改")
    page.locator("[data-editor-save]").click()
    page.wait_for_function("document.querySelector('[data-editor-status]').textContent.includes('原页面已改变')")
    assert canvas.locator("#heading").inner_text()=="保留待处理修改"
    assert page.locator("[data-editor-save]").is_enabled()
    assert "未保存" in page.locator("[data-editor-changed]").inner_text()
    assert not errors


def test_save_can_retry_render_without_fabricating_a_source_change(editor):
    page,canvas,errors=editor
    with page.expect_request(lambda request:request.method=="POST") as posted:
        page.locator("[data-editor-save]").click()
    assert posted.value.post_data_json=={"version":"source-version","changes":[]}
    page.wait_for_function("window.savedPage?.rendered===true")
    assert not errors
