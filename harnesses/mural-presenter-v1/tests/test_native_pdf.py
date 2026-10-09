from __future__ import annotations

from io import BytesIO
import asyncio
import json
import time

import fitz
from PIL import Image, ImageChops, ImageStat
import pytest


def test_pdf_cli_preserves_error_text_without_keyword_redaction(monkeypatch, capsys):
    import json
    from export import delivery
    def fail(*_args):
        raise ValueError("native PDF page 3 failed Chromium rendering; api_key=private-sentinel-123")
    monkeypatch.setattr(delivery, "export_on_demand", fail)
    assert delivery.main(["--workspace", "unused", "--format", "pdf"]) == 1
    result = json.loads(capsys.readouterr().err)
    assert result["error"] == "delivery_export_failed"
    assert "native PDF page 3" in result["message"]
    assert "ValueError" in result["message"]
    assert "api_key=private-sentinel-123" in result["message"]

from export import delivery, native_pdf
from geometry_fixture import geometry
from test_editable_pptx import browser_environment, sample_html, websocket_server
from test_editable_pptx_delivery import completed, author_bytes
from test_v1_font_pipeline import pinned_font_sources


@pytest.mark.parametrize('size', [(960,540), (800,600), (720,960), (1013,577), (1122,1587)])
def test_native_pdf_text_geometry_pagination_and_paint(tmp_path, browser_environment, size):
    from playwright.sync_api import sync_playwright
    width,height=size
    first=sample_html(width,height)
    first=first.replace('</main>', '<div style="position:absolute;left:40px;bottom:0;font:16px/20px sans-serif">Bottom marker</div>'
        '<div style="position:absolute;right:0;bottom:0;width:8px;height:8px;background:#ff00ff"></div></main>')
    # 作者 print 规则不应改变屏幕画布的布局、颜色和分页。
    first=first.replace('</style>', '@media print { h1 { display:none } } @page { size:A4; margin:2cm }</style>')
    second=first.replace('Quarterly Results','Second Page')
    target=tmp_path/'native.pdf'
    native_pdf.write_native_pdf([(1,first),(2,second)], target, geometry(width_px=width,height_px=height),
        scratch=tmp_path/'scratch',deadline_monotonic=time.monotonic()+90)
    with fitz.open(target) as pdf:
        assert len(pdf)==2
        for i,expected in enumerate(('Quarterly Results','Second Page')):
            page=pdf[i]
            assert expected in page.get_text() and 'Revenue' in page.get_text() and 'Bottom marker' in page.get_text()
            assert page.rect.width==pytest.approx(width*.75,abs=.1)
            assert page.rect.height==pytest.approx(height*.75,abs=.1)
            assert page.get_fonts() and page.get_images()
        pixmap=pdf[0].get_pixmap(matrix=fitz.Matrix(96/72,96/72),alpha=False)
        actual=Image.frombytes('RGB',(pixmap.width,pixmap.height),pixmap.samples)
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path=browser_environment['PPT_SKILL_BROWSER_EXE'],
            env=browser_environment,headless=True,args=['--no-sandbox'])
        try:
            page=browser.new_page(viewport={'width':width,'height':height})
            page.set_content(first)
            page.evaluate('document.fonts.ready')
            reference=Image.open(BytesIO(page.screenshot())).convert('RGB')
        finally:
            browser.close()
    # PDF 点到像素的取整最多多一行/列，不缩放来掩盖几何偏移。
    assert 0<=actual.width-width<=1 and 0<=actual.height-height<=1
    difference=ImageChops.difference(actual.crop((0,0,width,height)),reference)
    assert sum(ImageStat.Stat(difference).mean)/3 < 5
    assert all(abs(a-b)<3 for a,b in zip(actual.getpixel((240,120)),(25,115,155)))
    assert all(abs(a-b)<3 for a,b in zip(actual.getpixel((80,120)),(24,101,128)))
    assert actual.getpixel((width-4,height-4)) == (255,0,255)
    actual.save(tmp_path/'native.png')
    reference.save(tmp_path/'reference.png')
    assert not list((tmp_path/'scratch').iterdir())


@pytest.mark.parametrize('network', ['fetch', 'websocket'])
def test_native_pdf_cannot_leave_snapshot(tmp_path,browser_environment,websocket_server,network):
    endpoint,requests=websocket_server
    script=f'new WebSocket({json.dumps(endpoint)})' if network=='websocket' else 'fetch("http://127.0.0.1:1/escape")'
    html=sample_html(960,540).replace('</body>',f'<script>{script}</script></body>')
    with pytest.raises(ValueError,match='outside its verified snapshot'):
        native_pdf.write_native_pdf([(1,html)],tmp_path/'no.pdf',geometry(width_px=960,height_px=540),
            scratch=tmp_path/'scratch',deadline_monotonic=time.monotonic()+60)
    assert requests==[] and not (tmp_path/'no.pdf').exists()
    assert not list((tmp_path/'scratch').iterdir())


def test_native_pdf_cache_tracks_converter_and_preserves_author(tmp_path,monkeypatch,completed,browser_environment):
    before=author_bytes(tmp_path)
    first=delivery.export_on_demand(tmp_path,'pdf')
    with fitz.open(tmp_path/first['path']) as pdf:
        assert 'P01' in pdf[0].get_text()
    assert delivery.export_on_demand(tmp_path,'pdf')['cached'] is True
    old=native_pdf.source_identity
    monkeypatch.setattr(native_pdf,'source_identity',lambda:{**old(),'adapter':'changed-test-version'})
    rebuilt=delivery.export_on_demand(tmp_path,'pdf')
    assert rebuilt['cached'] is False and rebuilt['source_sha256']!=first['source_sha256']
    assert author_bytes(tmp_path)==before


def test_native_pdf_expired_deadline_publishes_nothing(tmp_path,browser_environment):
    with pytest.raises(ValueError,match='timeout'):
        native_pdf.write_native_pdf([(1,sample_html(960,540))],tmp_path/'no.pdf',geometry(width_px=960,height_px=540),
            scratch=tmp_path/'scratch',deadline_monotonic=time.monotonic()-1)
    assert not (tmp_path/'no.pdf').exists()


@pytest.mark.parametrize('blocked_stage',['fonts','decode','print'])
def test_native_pdf_total_deadline_interrupts_and_closes_browser(tmp_path,monkeypatch,browser_environment,blocked_stage):
    from playwright.async_api import Browser, BrowserContext, Page
    closed=[]
    original_browser_close,original_context_close=Browser.close,BrowserContext.close
    async def close_browser(self,*args,**kwargs):
        await original_browser_close(self,*args,**kwargs)
        assert not self.is_connected()
        closed.append('browser')
    async def close_context(self,*args,**kwargs):
        pages=list(self.pages)
        await original_context_close(self,*args,**kwargs)
        assert all(page.is_closed() for page in pages)
        closed.append('context')
    monkeypatch.setattr(Browser,'close',close_browser)
    monkeypatch.setattr(BrowserContext,'close',close_context)
    reached=[]
    if blocked_stage=='print':
        async def blocked_pdf(self,**kwargs):
            reached.append(True)
            await asyncio.Event().wait()
        monkeypatch.setattr(Page,'pdf',blocked_pdf)
    html=sample_html(960,540)
    if blocked_stage=='fonts':
        html=html.replace('</head>', '<script>Object.defineProperty(document.fonts,"status",{get:()=>"loading"});</script></head>')
    if blocked_stage=='decode':
        html=html.replace('</head>','<script>HTMLImageElement.prototype.decode=()=>new Promise(()=>{});</script></head>')
    started=time.monotonic()
    with pytest.raises(ValueError,match='native PDF render timeout'):
        native_pdf.write_native_pdf([(1,html)],tmp_path/'no.pdf',geometry(width_px=960,height_px=540),
            scratch=tmp_path/'scratch',deadline_monotonic=started+15)
    elapsed=time.monotonic()-started
    assert 14<=elapsed<25
    assert closed==['context','browser']
    assert not (tmp_path/'no.pdf').exists() and not list((tmp_path/'scratch').iterdir())
    if blocked_stage=='print':
        assert reached


@pytest.mark.parametrize('css_motion',[False,True])
def test_native_pdf_waits_for_existing_final_sample(tmp_path,browser_environment,css_motion):
    html=sample_html(960,540)
    if css_motion:
        html=html.replace('</style>', '''
          .box { animation:move 1.5s linear forwards; }
          @keyframes move { from {left:40px} to {left:70px} }
        </style>''')
    html=html.replace('Native badge text','Pending chart').replace('</body>', '''
      <script>setTimeout(()=>document.querySelector('.badge span').textContent='Final chart evidence',1500)</script></body>''')
    images=[]
    for index in range(2):
        target=tmp_path/f'motion-{index}.pdf'
        native_pdf.write_native_pdf([(1,html)],target,geometry(width_px=960,height_px=540),
            scratch=tmp_path/'scratch',deadline_monotonic=time.monotonic()+60)
        with fitz.open(target) as pdf:
            text=' '.join(pdf[0].get_text().split())
            assert 'Final chart evidence' in text and 'Pending chart' not in text
            pix=pdf[0].get_pixmap(matrix=fitz.Matrix(96/72,96/72),alpha=False)
            images.append(Image.frombytes('RGB',(pix.width,pix.height),pix.samples))
    assert ImageChops.difference(*images).getbbox() is None
    if css_motion:
        assert images[0].getpixel((55,120))==(255,255,255)
    assert all(abs(a-b)<3 for a,b in zip(images[0].getpixel((85,120)),(24,101,128)))


def test_native_pdf_rejects_full_rasterized_html_text(tmp_path,browser_environment):
    html=sample_html(960,540).replace('.mural-slide{','.mural-slide{filter:blur(.2px);')
    with pytest.raises(ValueError,match='no searchable PDF text'):
        native_pdf.write_native_pdf([(1,html)],tmp_path/'no.pdf',geometry(width_px=960,height_px=540),
            scratch=tmp_path/'scratch',deadline_monotonic=time.monotonic()+60)
    assert not (tmp_path/'no.pdf').exists()


def test_native_pdf_does_not_require_hidden_or_screen_reader_text(tmp_path,browser_environment):
    html='''<style>html,body{margin:0;width:960px;height:540px}.slide{width:960px;height:540px;background:#186580}
      .sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
      </style><main class="slide"><span hidden>Hidden text</span><span style="opacity:0">Transparent text</span>
      <span class="sr-only">Screen reader text</span></main>'''
    target=tmp_path/'decoration.pdf'
    native_pdf.write_native_pdf([(1,html)],target,geometry(width_px=960,height_px=540),
        scratch=tmp_path/'scratch',deadline_monotonic=time.monotonic()+60)
    with fitz.open(target) as pdf:
        assert not pdf[0].get_text().strip()


def test_native_pdf_offset_canvas_ignores_zero_box_script_and_hidden_nodes(tmp_path, browser_environment):
    html = '''<style>html,body{margin:0;width:960px;height:540px;overflow:hidden}
      .slide{position:relative;top:32px;width:960px;height:540px;background:white} h1{font:48px sans-serif}</style>
      <main class="slide"><h1>Offset canvas</h1><div hidden>Invisible</div>
      <script>window.checked=true;</script><p>Visible content remains</p></main>'''
    target = tmp_path / "offset.pdf"
    native_pdf.write_native_pdf([(1, html)], target, geometry(width_px=960, height_px=540),
        scratch=tmp_path / "scratch", deadline_monotonic=time.monotonic() + 60)
    with fitz.open(target) as pdf:
        assert len(pdf) == 1
        assert "Offset canvas" in pdf[0].get_text()
        assert "Visible content remains" in pdf[0].get_text()


@pytest.mark.parametrize("dynamic", [False, True])
def test_single_file_html_checks_dynamic_dependency_even_if_same_image_is_static(tmp_path, browser_environment, dynamic):
    html = sample_html(960, 540)
    if dynamic:
        html = html.replace('</body>', "<script>document.querySelector('img').src=['assets','picture.png'].join('/');</script></body>")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    kwargs = dict(scratch=scratch, deadline_monotonic=time.monotonic() + 60, page_numbers=[1])
    if dynamic:
        with pytest.raises(ValueError, match="single-file HTML.*runtime resources"):
            native_pdf.validate_single_file_html(html, geometry(width_px=960, height_px=540), **kwargs)
    else:
        native_pdf.validate_single_file_html(html, geometry(width_px=960, height_px=540), **kwargs)
    assert not list(scratch.iterdir())
