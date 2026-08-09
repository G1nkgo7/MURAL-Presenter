# 版式范例库(.slide-body 内的可复用骨架)

给**难版式 / 高频版式**各一段最小骨架,统一对齐与防溢出。规则:

- 这些都嵌在 **`.slide-body`** 里,**框架(`.slide-title` / `.slide-footer` / 页码 / 边距)不动**;
- **只引 `base.css` 的 token**(颜色 / 间距 / 字号),不写裸 hex;
- 规划时每页**点名一种**写进 `plan/slide_NN.md` 的「版式」行,**相邻两页不得用同一种**;
- 有对应骨架的页型直接套、只改内容,别从零手搓(尤其时间轴)。

> 这些是结构起点,不是死板模板——可微调,但保持对齐 / 等距 / 不溢出的底线。

---

## 1. 时间轴 / 里程碑(timeline)

护栏:**统一轴线**(横或竖,全页一致)、**节点等距**、每节点「阶段名 + 时间/里程碑 + 产出」三件套对齐、节点 **3–6 个**(更多就拆页或改阶段)。

```html
<div class="tl">
  <div class="tl-node">
    <div class="tl-when accent">2021</div>
    <div class="tl-dot"></div>
    <div class="tl-name">阶段名</div>
    <div class="tl-desc muted">这一阶段的产出 / 一句话</div>
  </div>
  <!-- 再重复 3–5 个 tl-node -->
</div>
<style>
.tl{ display:grid; grid-auto-flow:column; grid-auto-columns:1fr; align-items:start; gap:var(--gutter);
     position:relative; }
.tl::before{ content:""; position:absolute; left:0; right:0; top:64px; height:2px; background:var(--line); } /* 统一轴线 */
.tl-node{ display:grid; grid-template-rows:auto 24px auto auto; gap:8px; text-align:center; }
.tl-when{ font-size:var(--fs-h2); font-weight:700; }
.tl-dot{ width:14px; height:14px; border-radius:50%; background:var(--accent); justify-self:center; }
.tl-name{ font-weight:600; }
</style>
```

---

## 2. 数字指标行(KPI row)—— 一排关键数字,**不是卡片堆叠**

```html
<div class="kpi-row">
  <div><div class="kpi-num accent">68%</div><div class="muted">指标说明</div></div>
  <div><div class="kpi-num accent">3.2×</div><div class="muted">指标说明</div></div>
  <div><div class="kpi-num accent">¥4.1B</div><div class="muted">指标说明</div></div>
</div>
<style>
.kpi-num{ font-size:var(--fs-display); font-weight:900; line-height:1; }
</style>
```
（`.kpi-row` 已在 base.css;数字用 `--fs-display`/`--fs-title`,说明用 `--fs-caption`，别都一样大。）

---

## 3. 左右对比(two-col compare)—— 统一维度并列

```html
<div class="grid-2">
  <div class="panel"><h3 class="accent">方案 A</h3><ul>…</ul></div>
  <div class="panel"><h3 class="muted">方案 B</h3><ul>…</ul></div>
</div>
```
对比项**逐行对齐**(两栏同结构、同顺序);差异处用 `--accent` 点出,别两栏都铺满弱化重点。

---

## 4. 流程 / 逻辑树(process / logic)

```html
<div class="flow">
  <div class="step"><span class="idx accent">01</span><div>步骤一</div></div>
  <div class="arrow muted">→</div>
  <div class="step"><span class="idx accent">02</span><div>步骤二</div></div>
  <div class="arrow muted">→</div>
  <div class="step"><span class="idx accent">03</span><div>步骤三</div></div>
</div>
<style>
.flow{ display:flex; align-items:center; gap:var(--gutter); }
.step{ flex:1; background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:24px; }
.idx{ font-size:var(--fs-h2); font-weight:800; display:block; margin-bottom:8px; }
.arrow{ font-size:var(--fs-title); flex:0 0 auto; }
</style>
```

---

## 5. 左文右图 / 左图右文(split-media)

```html
<div class="split-media">
  <div><h2>论点标题</h2><p class="muted">论据 / 说明,短句。</p></div>
  <img class="img-cover" src="../assets/img_03.png" style="height:520px; border-radius:12px;">
</div>
```
（`.split-media` 已在 base.css;图给**显式高度** + `object-fit:cover`,比例对不上用 `.img-contain`，别拉伸。）

---

## 6. 引文 / 金句(quote)

```html
<blockquote class="quote">
  <p>“一句有分量的话。”</p>
  <footer class="muted">— 出处 / 人物（已核实）</footer>
</blockquote>
<style>
.quote p{ font-family:var(--font-serif); font-size:var(--fs-title); line-height:1.3; }
.quote footer{ margin-top:16px; font-size:var(--fs-caption); }
</style>
```

---

## 7. 满图封面 / 章节页(full-bleed)—— 见 base.css `.slide--bleed`

```html
<section class="slide slide--bleed">
  <div class="bleed"><img class="bleed-cover" src="../assets/img_cover.png"></div>
  <header class="slide-title"><span class="kicker">章节 / 场合</span>大标题</header>
  <div class="slide-body"></div>
  <footer class="slide-footer"><span>页脚</span><span class="page-no">01</span></footer>
</section>
<style>
/* 图上叠字要保证对比:给标题区加一层渐变压暗 */
.slide--bleed .slide-title{ text-shadow:0 2px 16px rgba(0,0,0,.5); }
</style>
```
满铺背景**不算"出血"硬伤**;但图上文字对比不足要修(压暗层 / 色罩)。
