# 版式范例库(.slide-body 内的可复用骨架)

给**难版式 / 高频版式**各一段最小骨架,统一对齐与防溢出。规则:

- 这些都嵌在 **`.slide-body`** 里,**框架(`.slide-title` / `.slide-footer` / 页码 / 边距)不动**;
- **只引 `base.css` 的 token**(颜色 / 间距 / 字号),不写裸 hex;
- 规划时每页**点名或改造一种**写进 `plan/slide_NN.md` 的「版式」行,说明为什么适合这页内容,**相邻两页不得用同一种**;
- 有对应骨架的页型优先借用其对齐 / 等距 / 防溢出原则,但按本 case 的内容密度、视觉媒介、图片比例、图表复杂度改造,别原样照抄(尤其时间轴)。

> 这些是结构起点,不是死板模板。若原骨架导致图片孤立、SVG/图表过小、大片死白、标签撞、底部溢出,必须调整比例 / 分栏 / 画布 / 图例位置,但保持对齐 / 等距 / 不溢出的底线。

## 目录

- 1. 时间轴 / 里程碑
- 2. 数字指标行
- 3. 左右对比
- 4. 流程 / 逻辑树
- 5. 左文右图 / 左图右文
- 6. 引文 / 金句
- 7. 满图封面 / 章节页
- 8. 破格 hero / 焦点页
- 9-18. 概念图 archetypes:三层架构、径向思维导图、路线图、雷达图、优劣对比、单一大数字、章节分隔、漏斗、循环、金字塔

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
.flow{ display:flex; align-items:stretch; gap:var(--gutter); }
.step{ flex:1; background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:24px; }
.idx{ font-size:var(--fs-h2); font-weight:800; display:block; margin-bottom:8px; }
/* 箭头在自己格里居中(align-self:center + flex 居中),对齐到卡片整体高度中线——别让裸箭头字符靠字形基线浮高/浮低 */
.arrow{ font-size:var(--fs-title); flex:0 0 auto; align-self:center; display:flex; align-items:center; line-height:1; }
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
  <div class="hero-num"><span class="num">68<span class="unit">%</span></span></div>
  <p class="hero-cap muted">一句话说清这个数字意味着什么(谁、相比什么、为何重要)</p>
</div>
<style>
.hero-figure{ height:100%; display:flex; flex-direction:column; justify-content:center; }
/* 巨数字复用 base.css 的 .num/.unit(单位对齐已在 .num 里锁死,别再私写 .hero-unit 裸 baseline);这里只放大字号 */
.hero-num .num{ font-size:clamp(180px, 34vw, 360px); font-weight:900; letter-spacing:-.02em; color:var(--accent); }
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

---

## 8.5 编辑设计动作库(治「封面 / hero 太素、太模板、重心平」——高设计感的具体杠杆)

模板感的根源是"每页都居中标题 + 一排卡片"。**封面 / 章节页 / hero / 重点页**主动选 **1 个**大胆版式动作(别叠 3 个,乱),就能从"PPT 模板"跳到"品牌 / 展览 / 杂志"质感。按气质挑一个:

- **巨型裁切标题跑出血**:超大中文标题被画布边裁掉一部分(`font-size` 极大 + 定位让首/末字压边),读着有张力。⚠️ 是**有意裁切**、不是 `⚠ OVERFLOW` 失控——裁的是装饰性大字的边缘笔画,不是正文信息。
- **竖排标题贯穿左/右缘**:中文主标题竖排(`writing-mode:vertical-rl`)顶天立地贴一侧安全线,主视觉占另一侧(见 culture 示例的「礦物顏料壁畫研究」)。
- **斜体衬线英文横切构图**:一行斜体衬线英文(`--font-display-serif` italic)斜穿或横贯画面当装饰层,中文信息压在其上/下。
- **技术注 / 标本框**:角落放 monospace 小字(页码 / 坐标 / 日期 / 材料标签 / `CATALOGUE NO.`)+ 细测量刻度线框(`.ph-frame` 或 hairline rect),把图裱成"标本 / 档案 / 研究系统"——高级感强、成系列好复用。
- **图字二分**:画面左右或上下**硬分两栏**——一栏是满幅图/材质,另一栏是高密度排版墙(标题 + 元信息 + 标签),对比强烈。
- **大留白 + 单点强碰撞**:大片安静留白(或纯色/纯黑),只放**一个**强烈的图或字,重心偏置但明确。
- **图窗 / 嵌套**:主图里再开一个小图窗(画中画 / 框景),或多个不同形状(`.ph-*`)的图按网格错落。

**纪律**:① 一页 **≤1 个**大动作 + 其余安静;② **全套成系列**(封面定的动作 + 母题,章节页/内页复现变体,不是每页各玩各的);③ 动作服务气质(党政/学术庄重题材克制用「技术注/图字二分」,别上斜切/裁血);④ **信息不被牺牲**——裁的是装饰、不是正文,标题别被图吞没(压图时走 §T3 scrim/托板)。这条直接治"封面素 / 重心平 / 模板味",配合 base.css 的 `.ph-*` 形状库和 `.slide--cover` 用。

---

# 概念图 archetypes(手写 SVG / hairline CSS / ECharts)—— 概念·科普·机制 deck 的主力杠杆

> **结构 / 机制 / 关系 / 流程 / 架构**这类页,靠**手写概念图**说清,不要用照片、也不要摆一张无信息的装饰图。这是科普 / 概念 / 科研 deck **最大的杠杆**。分工:**ECharts 只画数据图表**(柱 / 线 / 饼 / 散点),**概念图走 SVG / hairline CSS**(结构 / 关系 / 层级),**照片只给真实主体**(人 / 作品 / 产品 / 现场)。
> 下面 archetype **全部用本仓 base.css token 定义**(不写裸 hex、标签对比达标)。流程 / 逻辑树见上面 **#4 flow**、时间轴见 **#1 timeline**(已覆盖,不重复)。
> 和前面一样:**这些是构图起点,不是死模板**——鼓励据主题自行构图 / 组合 / 打破,但守住对齐 / 等距 / 不溢出 / 只点一个 accent 的底线。
>
> **★概念图清晰度语法(治「SVG 画得不清楚、传不达意」)**——一图只讲一个关系、沿一条主轴 / 一个方向:
> ① **图必须编码一个真关系**(流向 / 转化 / 层级 / 包含 / 对比)。自检:**把框和箭头去掉、信息若不丢,它就只是「装饰过的列表」**——那就别画图。**枚举 / 分类 / 并列要素 → 用分组列表或 2×2 矩阵**(`arch-matrix`),**别画成一摞几乎相同的带框矩形**(那是最常见的失败图);框 + 箭头**只留给有方向 / 转化的流程**。
> ② **节点里放专门的小图元**——迷你柱图 / 比特块 / 芯片 / 层叠块 / monoline 图标符号,**不是纯文字方块**。好机制图(`输入 → ①②③ → 输出`)之所以一眼读懂,就在每个节点自带可视意义、箭头承载真实变化。
> ③ **≤7 个节点**;标签**标在节点内、或用引线(leader line)引出**,别浮在半空;**图头 / 副标题与图之间留一条缝**(别贴住);**文字最后画**(SVG 无 z-index,只认绘制顺序)。
> ④ 标签重叠 = 硬伤:`render.py` 的 `⚠ OVERLAP` 会抓 SVG `<text>` 互撞;**数值标签也别贴压标记点 / 连线**(与标记留 ≥6px 气口,或引线引出)。坐标类图**出坐标轴刻度**,别只有轴线没有刻度值。
> ⑤ **体量硬门**:概念图必须套 `.arch-diagram` 或 `.diagram-canvas` + `svg.svg-diagram`,SVG 用 `width="100%" height="100%"` 和贴合内容的 `viewBox`;不要照抄固定 `width="480"`/`560"` 的小图。`render.py ⚠ SVG-SMALL` / `⚠ SVG-LABEL-OVERLAP` 命中就放大图框、重排标签、加 leader line。

## 9. 三层架构图(arch-diagram)

护栏:分层从上到下 = 接入 / 前台 → 服务 → 数据 / 后端,每层横排同级模块、层内等宽对齐;**只 1 个关键块染 accent**;直角 + hairline,**别做圆角彩条卡**。
```html
<div class="arch">
  <div class="tier"><span class="tier-k muted">接入层</span><div class="tier-cells">
    <div class="acell">Web</div><div class="acell">API</div><div class="acell">SDK</div></div></div>
  <div class="tier"><span class="tier-k muted">服务层</span><div class="tier-cells">
    <div class="acell">鉴权</div><div class="acell hot">编排</div><div class="acell">计费</div></div></div>
  <div class="tier"><span class="tier-k muted">数据层</span><div class="tier-cells">
    <div class="acell">对象存储</div><div class="acell">向量库</div></div></div>
</div>
<style>
.arch{ display:flex; flex-direction:column; gap:12px; }
.tier{ display:grid; grid-template-columns:120px 1fr; gap:16px; align-items:center; }
.tier-k{ text-align:right; }
.tier-cells{ display:flex; gap:12px; }
.acell{ flex:1; border:1px solid var(--line); background:var(--panel); padding:18px; text-align:center; }
.acell.hot{ border-color:var(--accent); color:var(--accent); }   /* 只点一个关键块 */
</style>
```

## 10. 径向思维导图(mindmap)—— 手写 SVG

护栏:分支 **4–6 条**大致径向均布;标签**紧贴线端**(左侧分支用 `text-anchor:end`);中心一处 accent、分支线用 `--line`;**⛔ 别做成发光粒子网 / 星座连线**(那是最泛滥的 AI 壁纸)。
> ⚠️ SVG 里 var() 要走 `style=`/CSS(`class`),**别写成 presentation 属性** `fill="var(--ink)"`(属性不解析 var());下面用一个 `<style>` 块挂 class,渲染最稳。
```html
<div class="diagram-canvas">
<svg class="svg-diagram diagram-mindmap" viewBox="0 0 820 520" width="100%" height="100%">
  <style>
    .mm-line{ stroke:var(--line); stroke-width:1.5; fill:none; }
    .mm-core{ stroke:var(--accent); stroke-width:2; fill:none; }
    .mm-t{ fill:var(--ink); font-family:var(--font-body); font-size:18px; }
    .mm-c{ fill:var(--ink); font-family:var(--font-title); font-size:22px; }
  </style>
  <path class="mm-line" d="M470,196 L650,120"/><path class="mm-line" d="M470,244 L650,330"/>
  <path class="mm-line" d="M350,196 L170,120"/><path class="mm-line" d="M350,244 L170,330"/>
  <circle class="mm-core" cx="410" cy="220" r="70"/>
  <text class="mm-c" x="410" y="227" text-anchor="middle">核心主题</text>
  <text class="mm-t" x="660" y="122">分支一</text><text class="mm-t" x="660" y="332">分支二</text>
  <text class="mm-t" x="160" y="122" text-anchor="end">分支三</text>
  <text class="mm-t" x="160" y="332" text-anchor="end">分支四</text>
</svg>
</div>
```

## 11. 路线图 NOW / NEXT / LATER(roadmap)

护栏:三等宽列(可加第四列 VISION),`gap:1px` 走 hairline 分隔;**只"NOW"列染 accent** 标"当下",别三列都染;各列条目左对齐、同结构。
```html
<div class="roadmap">
  <div class="lane"><h3 class="lane-h">NOW</h3><ul><li>进行中的事项</li><li>…</li></ul></div>
  <div class="lane"><h3 class="lane-h">NEXT</h3><ul><li>下一步</li></ul></div>
  <div class="lane"><h3 class="lane-h">LATER</h3><ul><li>远期</li></ul></div>
</div>
<style>
.roadmap{ display:grid; grid-template-columns:repeat(3,1fr); gap:1px; background:var(--line); }  /* gap 露底=hairline */
.lane{ background:var(--bg); padding:24px; }
.lane-h{ font-family:var(--font-title); letter-spacing:.06em; color:var(--ink-dim); margin-bottom:16px; }
.roadmap .lane:first-child .lane-h{ color:var(--accent); }   /* 只 NOW 染 accent */
.lane ul{ list-style:none; display:flex; flex-direction:column; gap:12px; }
.lane li{ border-left:2px solid var(--line); padding-left:12px; }
</style>
```

## 12. 雷达图(radar,ECharts)—— 多维对比

护栏:**5–6 轴**;最多 **2 个对象**叠比(多了糊);颜色取 `--series-*`(`getComputedStyle` 读**直接色值**,别喂 `var()`/`color-mix()`);轴名 / 网格用中性色。ECharts CDN 用 SKILL.md 钉的 `echarts@5.5.0`。
```html
<div id="radar" style="width:640px; height:520px; margin:auto"></div>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<script>
const css = getComputedStyle(document.documentElement); const g = n => css.getPropertyValue(n).trim();
echarts.init(document.getElementById('radar')).setOption({
  radar:{ indicator:[{name:'性能',max:100},{name:'成本',max:100},{name:'易用',max:100},{name:'生态',max:100},{name:'安全',max:100}],
          axisName:{color:g('--ink-dim')}, splitLine:{lineStyle:{color:g('--line')}}, splitArea:{show:false} },
  series:[{ type:'radar', data:[
    {value:[80,70,90,60,85], name:'方案 A', itemStyle:{color:g('--series-1')}},
    {value:[60,85,70,80,65], name:'方案 B', itemStyle:{color:g('--series-2')}} ]}]
});
</script>
```

## 13. 优劣对比(pros-cons)

护栏:两栏**同结构逐行对齐**;正负用 **+ / – 符号 + 深浅双通道**(别只靠红绿色相;守 base.css `--up` ≠ `--warn`、`--up`/`--down` 已和主色调和);**别做成一栏满绿一栏满红**。
```html
<div class="grid-2">
  <div class="panel"><h3>优势</h3><ul class="pc pro"><li>要点一</li><li>要点二</li></ul></div>
  <div class="panel"><h3 class="muted">代价</h3><ul class="pc con"><li>要点一</li><li>要点二</li></ul></div>
</div>
<style>
.pc{ list-style:none; display:flex; flex-direction:column; gap:10px; }
.pc li{ position:relative; padding-left:26px; }
.pc.pro li::before{ content:"+"; position:absolute; left:0; color:var(--up); font-family:var(--font-title); }
.pc.con li::before{ content:"–"; position:absolute; left:0; color:var(--down); }
</style>
```

## 14. 单一大数字(stat-highlight)

和 **#8(a) 破格 hero-num** 的区别:stat-highlight **保留常规标题区**,是**数据页的单焦点版**(一页只一个头条数字)。护栏:一个数字 + 一句上下文 + 同比/基准 + 来源;单位退一步(`.unit` 已 0.5×);同比涨跌用 `--up`/`--down` + 箭头双通道。
```html
<div class="stat">
  <div class="accent"><span class="num">2,523<span class="unit">亿</span></span></div>
  <p class="stat-sub">这个数字说明什么(一句)</p>
  <p class="muted">同比 ▲ +18% · 来源:research_02</p>
</div>
<style>
.stat{ height:100%; display:flex; flex-direction:column; justify-content:center; gap:12px; }
.stat .num{ font-size:clamp(96px,14vw,200px); }
.stat-sub{ font-size:var(--fs-h2); }
</style>
```

## 15. 大号章节分隔(section-divider)—— 过渡页专用

护栏:极简,只 **幕号 + 幕名 + 一句承诺**,几乎不放数据;属破格页家族,与内容页明显不同;**每幕换处理**(重心在左 / 中,或换巨号位置,别所有分隔页一个模子)。配合 SKILL.md 流程「长 deck 分幕」硬规(设计细节见 design-rules §3/§12)。
```html
<div class="sdiv">
  <span class="sd-no">02</span>
  <h2 class="sd-name">章节名</h2>
  <p class="sd-say muted">这一幕讲什么,一句话承诺。</p>
</div>
<style>
.sdiv{ height:100%; display:flex; flex-direction:column; justify-content:center; gap:14px; }
.sd-no{ font-family:var(--font-title); font-size:clamp(120px,18vw,240px); line-height:.85; color:var(--accent); letter-spacing:-.03em; }
.sd-name{ font-family:var(--font-title); font-size:var(--fs-display); }
.sd-say{ max-width:40ch; }
</style>
```

## 16. 漏斗(diagram-funnel)—— 转化 / 筛选 / 逐级收窄

护栏:3–5 层自上而下收窄的梯形,层间留缝;每层标名 + 值/占比;**只点 1 处 accent**(转化关键层 / 最窄层)。层色用 `--series-N` 同色系浓淡,非彩虹。手写 SVG(ECharts 不画漏斗结构)。套 `.arch-diagram` 居中。
```html
<div class="arch-diagram">
  <svg class="svg-diagram diagram diagram-funnel" viewBox="0 0 640 420" width="100%" height="100%">
    <polygon points="40,20 600,20 540,110 100,110"   fill="var(--series-1)"/>
    <polygon points="100,120 540,120 480,210 160,210" fill="var(--series-3)"/>
    <polygon points="160,220 480,220 420,310 220,310" fill="var(--series-2)"/>
    <polygon points="220,320 420,320 360,410 280,410" fill="var(--accent)"/>   <!-- 关键层点 accent -->
    <g fill="var(--ink)" font-size="22" text-anchor="middle" font-family="var(--font-sans)">
      <text x="320" y="72">曝光 100%</text><text x="320" y="172">点击 42%</text>
      <text x="320" y="272">加购 18%</text><text x="320" y="372">成交 6%</text>
    </g>
  </svg>
</div>
```
（层数 / 宽度递减 / 留缝按内容调;标签也可移到梯形右侧引线标注。）

## 17. 循环(diagram-cycle)—— 迭代 / 闭环 / 反馈回路

护栏:3–6 个节点沿圆周等距 + 弧线成闭环;节点用 `--panel` 圆/圆角块,环线 `--line`,**当前/关键节点点 `--accent`**。手写 SVG。套 `.arch-diagram` 居中。
```html
<div class="arch-diagram">
  <svg class="svg-diagram diagram diagram-cycle" viewBox="0 0 480 480" width="100%" height="100%">
    <circle cx="240" cy="240" r="150" fill="none" stroke="var(--line)" stroke-width="2" stroke-dasharray="6 10"/>
    <g stroke="var(--line)">
      <circle cx="240" cy="90" r="52" fill="var(--panel)"/><circle cx="390" cy="240" r="52" fill="var(--panel)"/>
      <circle cx="240" cy="390" r="52" fill="var(--panel)"/><circle cx="90" cy="240" r="52" fill="var(--accent)"/>
    </g>
    <g fill="var(--ink)" font-size="20" text-anchor="middle" font-family="var(--font-sans)">
      <text x="240" y="96">计划</text><text x="390" y="246">执行</text>
      <text x="240" y="396">检查</text><text x="90" y="246">改进</text>
    </g>
  </svg>
</div>
```
（节点数 / 位置 / 箭头方向按内容调;箭头头可用 `<path>` + `<marker>` 画三角。）

## 18. 金字塔(diagram-pyramid)—— 层级 / 优先级 / 基础→顶层

护栏:3–5 层横向色带自下而上收窄(底宽顶尖);层高等分、层名居中;层色用同色系**明度阶梯**(底深顶亮或反之),非彩虹。手写 SVG。套 `.arch-diagram` 居中。
```html
<div class="arch-diagram">
  <svg class="svg-diagram diagram diagram-pyramid" viewBox="0 0 560 420" width="100%" height="100%">
    <polygon points="280,20 340,120 220,120"          fill="var(--accent)"/>   <!-- 顶层点 accent -->
    <polygon points="220,130 340,130 400,230 160,230" fill="var(--series-3)"/>
    <polygon points="160,240 400,240 460,330 100,330" fill="var(--series-2)"/>
    <polygon points="100,340 460,340 520,410 40,410"  fill="var(--series-4)"/>  <!-- 底层最宽 -->
    <g fill="var(--ink)" font-size="22" text-anchor="middle" font-family="var(--font-sans)">
      <text x="280" y="95">愿景</text><text x="280" y="190">战略</text>
      <text x="280" y="295">战术</text><text x="280" y="385">执行基础</text>
    </g>
  </svg>
</div>
```
（层数 / 明度阶梯方向按内容调。矩阵 2×2 走 base.css `.arch-matrix`,不在此。）
