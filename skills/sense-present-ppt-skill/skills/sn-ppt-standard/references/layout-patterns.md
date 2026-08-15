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
  <!-- 再重复 2–5 个 tl-node(连首个共 3–6 个) -->
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
  <div><div class="kpi-num accent"><span class="num">68<span class="unit">%</span></span></div><div class="muted">指标说明</div></div>
  <div><div class="kpi-num"><span class="num">3.2<span class="unit">×</span></span></div><div class="muted">指标说明</div></div>
  <div><div class="kpi-num"><span class="num">4.1<span class="unit">亿元</span></span></div><div class="muted">指标说明</div></div>
</div>
<style>
.kpi-num{ font-size:var(--fs-display); line-height:1; }
</style>
```
（`.kpi-row`/`.num`/`.unit` 都已在 base.css。**数字 + 单位用 `<span class="num">数字<span class="unit">单位</span></span>`——单位自动降到 0.5× 数字、不抢戏**;数字用 `--fs-display`/`--fs-title`,说明用 `--fs-caption`,别都一样大。**一排里只一个数字染 `accent` 当头条**,其余中性色。）

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
  <img class="img-cover" src="../assets/img_03.png" style="height:520px; border-radius:12px;"><!-- src 用规划回填的真实路径;img_03.png 只是占位示意 -->
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
  <div class="bleed"><img class="bleed-cover" src="../assets/img_cover.png"></div><!-- src 用规划回填的真实路径;img_cover.png 只是占位示意 -->
  <header class="slide-title"><span class="kicker">章节 / 场合</span>大标题</header>
  <div class="slide-body"></div>
  <footer class="slide-footer"><span>页脚</span><span class="page-no">07 / 24</span></footer><!-- 页码手写「页号 / 总页」,总页取自 deck.md -->
</section>
<style>
/* 图上叠字要保证对比:给标题区加一层渐变压暗 */
.slide--bleed .slide-title{ text-shadow:0 2px 16px rgba(0,0,0,.5); }
</style>
```
满铺背景**不算"出血"硬伤**;但图上文字对比不足要修(压暗层 / 色罩)。

---

## 8. 破格 hero / 焦点页(每套 ≥1 页,打破栅格)——治"每页都一个样的模板味"

放在叙事高点(关键数字 / 核心论点),**刻意和其它页不一样**:一个意象占满画布,极少元素。两种常用骨架:

**(a) 巨数字 / 巨词**——一个数字或一句话顶天立地,其余只留一行注解:
```html
<div class="hero-figure">
  <div class="hero-num">68<span class="hero-unit">%</span></div>
  <p class="hero-cap muted">一句话说清这个数字意味着什么(谁、相比什么、为何重要)</p>
</div>
<style>
.hero-figure{ height:100%; display:flex; flex-direction:column; justify-content:center; }
.hero-num{ font-size:clamp(180px, 34vw, 360px); font-weight:900; line-height:.9; letter-spacing:-.02em; color:var(--accent); }
.hero-unit{ font-size:.4em; font-weight:700; }
.hero-cap{ font-size:var(--fs-h2); max-width:60%; margin-top:8px; }
</style>
```

**(b) 满幅金句**——一句话独占画面,衬线大字,别的什么都没有:
```html
<blockquote class="hero-quote">
  <p>“一句有分量、能被记住的话。”</p>
  <footer class="muted">— 出处(已核实)</footer>
</blockquote>
<style>
.hero-quote{ height:100%; display:flex; flex-direction:column; justify-content:center; max-width:80%; }
.hero-quote p{ font-family:var(--font-serif); font-size:clamp(48px, 6.5vw, 92px); line-height:1.12; }
.hero-quote footer{ font-size:var(--fs-h2); margin-top:24px; }
</style>
```
hero 页**不套常规标题区也行**(数字/金句本身就是标题);它的作用就是和"眉签→大标题→一排卡片"的内容页拉开反差。**全套别超过 1–2 个 hero**,多了就不"破格"了。
