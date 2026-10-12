"""字体就绪与翻页、工作区切换、文档重载交错时，预览不能变成透明空白。"""
from pathlib import Path
import re
import subprocess


def test_static_preview_reveal_keeps_current_document_and_visible_layer():
    source = (Path(__file__).resolve().parents[1] / "static/app.js").read_text()
    functions = []
    for name in ("showStaticDeck", "revealStaticDeckWhenFontsReady", "promoteStaticDeck"):
        start = re.search(rf"^(?:async )?function {name}\(", source, re.M).start()
        end = re.search(r"^(?:async )?function ", source[start + 1:], re.M)
        functions.append(source[start:start + 1 + end.start()])
    setup = """
const assert = require('node:assert/strict');
const ed = {kind:'static',id:1,sel:1,workspaceView:'ppt',viewMode:'ppt',
  staticDeckFinal:true,staticDirtyPages:new Set(),rtimes:{},atimes:{},staticStamp:10};
const classes=new Set(['font-pending']);
const doc={querySelector:()=>null,fonts:[]};
const frame={dataset:{deckKind:'static',deckId:'1',deckStamp:'10',fontsReady:'0',pendingSlide:'1'},
  hidden:false,contentDocument:doc,contentWindow:{cleanDeck:{go:()=>{},fontsReady:Promise.resolve()}},
  classList:{add:c=>classes.add(c),remove:c=>classes.delete(c)},
  getBoundingClientRect:()=>({width:900,height:506})};
const img={hidden:false}, empty={hidden:true};
const $=s=>s==='#canvas-deck'?frame:s==='#canvas-img'?img:empty;
const editorPresentationKind=()=> 'static', staticHtmlCheckpointReady=()=>true,
  primeStaticPng=()=>{},playStaticSlide=()=>true,
  fitWorkspaceCanvas=()=>{},fitDeckViewport=()=>{},scheduleStaticPreviewFallback=()=>{};
const keepStaticPng=()=>{frame.hidden=true;img.hidden=false};
const callbacks=[];
const requestAnimationFrame=cb=>callbacks.push(cb);
const flush=()=>{while(callbacks.length) callbacks.shift()()};
(async()=>{
  // 字体已就绪，旧页的显示回调尚未执行，用户已经选择下一页。
  await revealStaticDeckWhenFontsReady(frame,frame.contentWindow);
  ed.sel=2;showStaticDeck(2);flush();
  assert(!frame.hidden && classes.has('font-ready') && !classes.has('font-pending'));
  assert(img.hidden);

  // 同一 WindowProxy 在 iframe 导航后仍保留，必须用 Document 身份排除旧回调。
  let resolve;
  frame.contentWindow.cleanDeck.fontsReady=new Promise(r=>resolve=r);
  frame.dataset.fontsReady='0';classes.clear();classes.add('font-pending');img.hidden=false;
  const stale=revealStaticDeckWhenFontsReady(frame,frame.contentWindow);
  frame.contentDocument={...doc};resolve();await stale;flush();
  assert.equal(frame.dataset.fontsReady,'0');assert(!img.hidden);

  // 等待字体期间回到 Workflow，旧预览不能隐藏备用图或强行显示播放器。
  frame.contentWindow.cleanDeck.fontsReady=new Promise(r=>resolve=r);
  const switched=revealStaticDeckWhenFontsReady(frame,frame.contentWindow);
  ed.workspaceView='workflow';frame.hidden=true;resolve();await switched;flush();
  assert(frame.hidden);assert(!img.hidden);

  // 布局还没有尺寸时保留 PNG，下一次布局完成后才交接。
  ed.workspaceView='ppt';ed.sel=2;
  frame.getBoundingClientRect=()=>({width:0,height:0});
  promoteStaticDeck(frame,2,frame.contentDocument);assert(!img.hidden);
  frame.getBoundingClientRect=()=>({width:900,height:506});
  promoteStaticDeck(frame,2,frame.contentDocument);assert(img.hidden);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    subprocess.run(["node", "-e", "\n".join(functions) + setup],
                   capture_output=True, text=True, check=True, timeout=20)
