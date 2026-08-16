#!/usr/bin/env python3
"""build_player.py —— 把 slides/slide_*.html 串成一个可播放的 present.html。

在工作区根运行(slides/ 旁边):
    python ${SKILL_DIR:-skills/ppt-skill-html}/scripts/build_player.py [slides_dir=slides] [out=present.html]

每页是独立的自包含 HTML(各自的 base.css / ECharts / inline style),所以**不能内联拼接**
(CSS/JS 会打架)——用 `<iframe>` 逐页加载即可完美隔离。生成的 present.html:
- **每页一个 iframe、交叉淡入(crossfade),无白闪**:目标页加载好前保持旧页可见,
  已加载过的页切换是瞬时淡入;只创建当前页 + 相邻页(懒加载,省内存);
- **画布尺寸自适应**:build 时从 base.css 探测 `--canvas-w/--canvas-h`(横版 1600×900 /
  竖版 900×1600 都对),运行时再从实际加载的 `.slide` 复测兜底;按窗口等比缩放居中(letterbox);
- 键盘 ←/→ / 空格 / PageUp/Down 翻页、Home/End 首尾、F 全屏;点击右/左半屏翻页;触摸滑动;
- 顶部进度条、底部 HUD、`#N` URL 深链。
自包含、零依赖(只用 stdlib),浏览器直接打开 present.html 即可像 PPT 一样放映。
"""
import glob
import json
import os
import re
import sys


def _detect_canvas(base_dir):
    """从工作区的 base.css 探测画布尺寸(--canvas-w/--canvas-h)。
    找不到就回退默认横版 1600×900;运行时 JS 还会从 .slide 复测兜底。"""
    w, h = 1600, 900
    for cand in ("base.css", os.path.join("slides", "base.css")):
        path = os.path.join(base_dir, cand)
        if not os.path.isfile(path):
            continue
        try:
            css = open(path, encoding="utf-8").read()
        except Exception:
            continue
        mw = re.search(r"--canvas-w:\s*([0-9.]+)px", css)
        mh = re.search(r"--canvas-h:\s*([0-9.]+)px", css)
        if mw and mh:
            w, h = int(float(mw.group(1))), int(float(mh.group(1)))
            break
    return w, h


def main(a):
    sdir = a[0] if len(a) > 0 else "slides"
    out = a[1] if len(a) > 1 else "present.html"
    candidates = [f for f in glob.glob(os.path.join(sdir, "slide_*.html"))
                  if ".bak." not in os.path.basename(f)]
    if not candidates:
        print(f"没找到 {sdir}/slide_*.html", file=sys.stderr)
        return 1
    numbered = []
    invalid = []
    for path in candidates:
        match = re.fullmatch(r"slide_(\d+)\.html", os.path.basename(path))
        if not match:
            invalid.append(os.path.basename(path))
            continue
        numbered.append((int(match.group(1)), path))
    numbered.sort(key=lambda item: item[0])
    numbers = [number for number, _ in numbered]
    expected = list(range(1, len(numbered) + 1))
    expected_names = [f"slide_{number:02d}.html" for number in expected]
    actual_names = [os.path.basename(path) for _, path in numbered]
    if invalid or numbers != expected or actual_names != expected_names:
        print(
            "页号不连续或命名不规范: "
            f"actual={sorted(os.path.basename(f) for f in candidates)} "
            f"expected={expected_names}",
            file=sys.stderr,
        )
        return 1
    files = [path for _, path in numbered]
    base = os.path.dirname(os.path.abspath(out)) or "."
    rel = [os.path.relpath(f, base).replace(os.sep, "/") for f in files]
    cw, ch = _detect_canvas(base)
    html = (TPL.replace("__SLIDES__", json.dumps(rel, ensure_ascii=False))
               .replace("__CW__", str(cw)).replace("__CH__", str(ch)))
    try:
        with open(out, "w", encoding="utf-8") as f:
            f.write(html)
    except OSError as exc:
        print(f"写入 present.html 失败: {out}: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {out} ({len(rel)} slides, canvas {cw}x{ch})")
    return 0


TPL = r"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Presentation</title>
<style>html,body{margin:0;height:100%;background:#000;overflow:hidden;font-family:system-ui,sans-serif}
#stage{position:fixed;inset:0;display:flex;align-items:center;justify-content:center}
#wrap{position:relative;width:__CW__px;height:__CH__px;transform-origin:center;background:#000;box-shadow:0 0 40px rgba(0,0,0,.6)}
#wrap iframe{position:absolute;inset:0;width:100%;height:100%;border:0;background:transparent;
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
let CW=__CW__,CH=__CH__;   /* build 时从 base.css 探测的画布尺寸;运行时再从 .slide 复测兜底 */
const wrap=document.getElementById('wrap'),p=document.getElementById('p'),bar=document.getElementById('bar');
const fit=()=>{wrap.style.width=CW+'px';wrap.style.height=CH+'px';
wrap.style.transform='scale('+Math.min(innerWidth/CW,innerHeight/CH)+')';};
/* 从已加载的 iframe 里读 .slide 真实像素尺寸,校正画布(处理 base.css 未用 --canvas-* 或 build 探测失准的情况) */
/* 用 offsetWidth/Height(.slide 的 border-box = 画布真实尺寸),不用 scrollWidth/Height:后者会把画到
   画布外、被 .slide overflow:hidden 视觉裁掉的装饰(halftone 圆/斜切色块等,常伸出画布)也算进去,
   于是画布被撑成 1710/1950 之类 → 整册被多缩、右侧留黑边;且各页溢出量不同 + CW/CH 全局共享,
   相邻页懒加载先后会互相污染当前页缩放(偶发)。offset 只量 .slide 盒本身,横竖版都对。 */
function remeasure(e){try{const d=e.contentDocument;if(!d)return;
const el=d.querySelector('.slide')||d.body;if(!el)return;
const w=Math.round(el.offsetWidth||el.scrollWidth),h=Math.round(el.offsetHeight||el.scrollHeight);
if(w>50&&h>50&&(Math.abs(w-CW)>1||Math.abs(h-CH)>1)){CW=w;CH=h;fit();}}catch(err){}}
function ensure(n){if(n<0||n>=S.length)return null;if(fr[n])return fr[n];
const e=document.createElement('iframe');e.dataset.ok='0';e.title='slide '+(n+1);
e.addEventListener('load',()=>{e.dataset.ok='1';remeasure(e);
try{e.contentDocument.addEventListener('keydown',onKey);}catch(_){}   /* 焦点进 iframe(用户一点幻灯片)也能 ←/→ 翻页:同源,给页内文档挂同一监听 */
if(n===i)reveal();});
e.src=S[n];wrap.appendChild(e);fr[n]=e;return e;}
function reveal(){fr.forEach((e,k)=>{if(e)e.classList.toggle('cur',k===i);});}
function show(n,push){i=Math.max(0,Math.min(S.length-1,n));
const e=ensure(i);ensure(i-1);ensure(i+1);
if(e.dataset.ok==='1')reveal();   /* 已加载→立即交叉淡入;未加载→其 load 事件再 reveal,旧页保持可见,无白闪 */
p.textContent=(i+1)+' / '+S.length;bar.style.width=((i+1)/S.length*100)+'%';
if(push!==false)location.hash='#'+(i+1);
document.body.classList.add('hud');clearTimeout(show.t);show.t=setTimeout(()=>document.body.classList.remove('hud'),1500);}
const next=()=>show(i+1),prev=()=>show(i-1);
function onKey(e){const k=e.key;
if(['ArrowRight','PageDown',' ','Enter'].includes(k)){next();e.preventDefault();}
else if(['ArrowLeft','PageUp','Backspace'].includes(k)){prev();e.preventDefault();}
else if(k==='Home')show(0);else if(k==='End')show(S.length-1);
else if(k==='f'||k==='F'){document.fullscreenElement?document.exitFullscreen():document.documentElement.requestFullscreen();}}
addEventListener('keydown',onKey);   /* 外层窗口 + 每个 iframe contentDocument(在 ensure 的 load 里挂)都监听→焦点在哪都能翻页 */
addEventListener('resize',fit);
let x=null;addEventListener('touchstart',e=>x=e.touches[0].clientX,{passive:true});
addEventListener('touchend',e=>{if(x==null)return;const d=e.changedTouches[0].clientX-x;if(Math.abs(d)>40)d<0?next():prev();x=null;});
document.getElementById('stage').addEventListener('click',e=>e.clientX>innerWidth/2?next():prev());
fit();const s=parseInt((location.hash||'#1').slice(1),10);show(isNaN(s)?0:s-1,false);
</script></body></html>"""


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
