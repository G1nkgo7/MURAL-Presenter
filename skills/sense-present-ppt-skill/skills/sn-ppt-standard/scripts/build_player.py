#!/usr/bin/env python3
"""build_player.py —— 把 slides/slide_*.html 串成一个可播放的 present.html。

使用绝对路径调用，不依赖当前工作目录:
    python "$SKILL_ROOT/scripts/build_player.py" "$DECK_DIR/slides" "$DECK_DIR/present.html"

每页是独立的自包含 HTML(各自的 base.css / ECharts / inline style),所以**不能内联拼接**
(CSS/JS 会打架)——用 `<iframe>` 逐页加载即可完美隔离。生成的 present.html:
- **每页一个 iframe、交叉淡入(crossfade),无白闪**:目标页加载好前保持旧页可见,
  已加载过的页切换是瞬时淡入;只创建当前页 + 相邻页(懒加载,省内存);
- 固定 1600×900 画布按窗口**等比缩放**居中(黑边 letterbox);
- 键盘 ←/→ / 空格 / PageUp/Down 翻页、Home/End 首尾、F 全屏;点击右/左半屏翻页;触摸滑动;
- 顶部进度条、底部 HUD、`#N` URL 深链。
自包含、零依赖(只用 stdlib),浏览器直接打开 present.html 即可像 PPT 一样放映。
"""
import glob
import json
import os
import sys


def main(a):
    sdir = a[0] if len(a) > 0 else "slides"
    out = a[1] if len(a) > 1 else "present.html"
    files = sorted(f for f in glob.glob(os.path.join(sdir, "slide_*.html"))
                   if ".bak." not in os.path.basename(f))
    if not files:
        print(f"没找到 {sdir}/slide_*.html", file=sys.stderr)
        return 1
    base = os.path.dirname(os.path.abspath(out)) or "."
    rel = [os.path.relpath(f, base).replace(os.sep, "/") for f in files]
    with open(out, "w", encoding="utf-8") as f:
        f.write(TPL.replace("__SLIDES__", json.dumps(rel, ensure_ascii=False)))
    print(f"wrote {out} ({len(rel)} slides)")
    return 0


TPL = r"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Presentation</title>
<style>html,body{margin:0;height:100%;background:#000;overflow:hidden;font-family:system-ui,sans-serif}
#stage{position:fixed;inset:0;display:flex;align-items:center;justify-content:center}
#wrap{position:relative;width:1600px;height:900px;transform-origin:center;background:#000;box-shadow:0 0 40px rgba(0,0,0,.6)}
#wrap iframe{position:absolute;inset:0;width:1600px;height:900px;border:0;background:transparent;
opacity:0;transition:opacity .28s ease;pointer-events:none}
#wrap iframe.cur{opacity:1;pointer-events:auto}
#bar{position:fixed;left:0;top:0;height:3px;background:#c9a227;width:0;transition:width .2s}
#hud{position:fixed;left:50%;bottom:14px;transform:translateX(-50%);color:#bbb;background:rgba(0,0,0,.55);
padding:4px 12px;border-radius:20px;font-size:13px;opacity:0;transition:opacity .3s;pointer-events:none}
body.hud #hud{opacity:1}</style></head>
<body><div id="stage"><div id="wrap"></div></div>
<div id="bar"></div><div id="hud"><span id="p"></span> · ←/→ 翻页 · F 全屏 · Home/End 首尾</div>
<script>
const S=__SLIDES__;let i=0;const fr=[];
const wrap=document.getElementById('wrap'),p=document.getElementById('p'),bar=document.getElementById('bar');
const fit=()=>wrap.style.transform='scale('+Math.min(innerWidth/1600,innerHeight/900)+')';
function ensure(n){if(n<0||n>=S.length)return null;if(fr[n])return fr[n];
const e=document.createElement('iframe');e.dataset.ok='0';e.title='slide '+(n+1);
e.addEventListener('load',()=>{e.dataset.ok='1';if(n===i)reveal();try{e.contentWindow.addEventListener('keydown',onkey);}catch(_){}});
e.src=S[n];wrap.appendChild(e);fr[n]=e;return e;}
function reveal(){fr.forEach((e,k)=>{if(e)e.classList.toggle('cur',k===i);});}
function show(n,push){i=Math.max(0,Math.min(S.length-1,n));
const e=ensure(i);ensure(i-1);ensure(i+1);
if(e.dataset.ok==='1')reveal();   /* 已加载→立即交叉淡入;未加载→其 load 事件再 reveal,旧页保持可见,无白闪 */
p.textContent=(i+1)+' / '+S.length;bar.style.width=((i+1)/S.length*100)+'%';
if(push!==false)location.hash='#'+(i+1);
document.body.classList.add('hud');clearTimeout(show.t);show.t=setTimeout(()=>document.body.classList.remove('hud'),1500);}
const next=()=>show(i+1),prev=()=>show(i-1);
function onkey(e){const k=e.key;
if(['ArrowRight','PageDown',' ','Enter'].includes(k)){next();e.preventDefault();}
else if(['ArrowLeft','PageUp','Backspace'].includes(k)){prev();e.preventDefault();}
else if(k==='Home')show(0);else if(k==='End')show(S.length-1);
else if(k==='f'||k==='F'){document.fullscreenElement?document.exitFullscreen():document.documentElement.requestFullscreen();}}
addEventListener('keydown',onkey);
addEventListener('resize',fit);
let x=null;addEventListener('touchstart',e=>x=e.touches[0].clientX,{passive:true});
addEventListener('touchend',e=>{if(x==null)return;const d=e.changedTouches[0].clientX-x;if(Math.abs(d)>40)d<0?next():prev();x=null;});
document.getElementById('stage').addEventListener('click',e=>e.clientX>innerWidth/2?next():prev());
fit();const s=parseInt((location.hash||'#1').slice(1),10);show(isNaN(s)?0:s-1,false);
</script></body></html>"""


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
