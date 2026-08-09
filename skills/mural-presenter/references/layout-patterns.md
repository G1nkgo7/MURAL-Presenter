# 版式范例库

本文件是页型索引，不是模板全集。先按页面职责从目录选择一个接近的骨架，再根据内容、媒介和整册视觉方案改造。复杂概念图另读第 9 节；需要设计节点、连接、判断、归组或标注语法时，再读 `shape-grammar.md`。

- 这些都嵌在 **`.slide-body`** 里,**框架(`.slide-title` / `.slide-footer` / 页码 / 边距)不动**;
- **只引 `base.css` 的 token**(颜色 / 间距 / 字号),不写裸 hex;
- 规划时在 `plan/slide_NN.md` 中写明这页的版式起点和选择原因；相邻页只在内容逻辑确有需要时重复同一构图；
- 有对应骨架的页型优先借用其对齐 / 等距 / 防溢出原则,但按本 case 的内容密度、视觉媒介、图片比例、图表复杂度改造,别原样照抄(尤其时间轴)。

> 如果原骨架让图片、Canvas 或图表过小，或造成无效留白、标签冲突和底部溢出，调整比例、分栏、画布或图例位置。骨架为内容服务。

## 0. 先选阅读路径，再选组件

首稿先决定观众从哪里进入、经过什么证据、在哪里得到结论。下面是阅读路径，不是固定 CSS 模板：

1. **Hero → 结论**：场景、对象、作品或决定性数字占主画面，标题与一句结论进入真实负空间；
2. **对象 → 解释**：人物、产品、器物、界面或 Figure 先被看见，解释围绕对象锚点展开；
3. **证据 → 推断**：图表、文件、地图或实验结果占主舞台，结论贴近关键证据位置；
4. **A ↔ B**：两个对象使用同一尺度、裁切或比较维度，差异通过位置、连线或强调直接可见；
5. **路径 → 决策**：流程、时间、空间或因果路径带动视线，决策落在终点、分支或瓶颈；
6. **主张 → 回响**：一句主张建立焦点，图像、对象、数字或短证据在另一处形成对重与回应。

不要先数“需要几张卡”。同一页可以有多个信息对象，但只能有一条主要阅读路径。逐页计划写明路径后，Slide 再决定精确网格、重叠、裁切和局部表面。

### 空间原型与起始关系

比例只作为首稿起点，不是验证阈值。文字量、图片比例、图表复杂度和整册节奏可以改变它。

| 空间原型 | 常见起始关系 | 适合内容 | 先决定什么 |
| --- | --- | --- | --- |
| 电影式 Hero | 主视觉约 65–100%，文字落在安全负空间 | 封面、事件、场景、作品、人物 | 主体、焦点、文字安全区 |
| 编辑分屏 | 主区约 55–70%，辅区约 30–45% | 图文解释、产品、人物、案例 | 谁是主角，分界是否参与构图 |
| 证据主舞台 | 图表/Figure/地图约 65–80% | 数据、论文、调研、机制结果 | 关键证据与结论锚点 |
| 同尺度比较 | 两侧同基线、同尺度；结论横跨或落在差异处 | 方案、前后、对象对比 | 公共维度与差异编码 |
| 连续路径 | 路径贯穿主要画布，节点与说明依附其上 | 流程、路线图、时间、因果 | 方向、分支、终点和反馈 |
| 编辑索引 | 大序号/短标题建立节奏，条目沿稳定轴展开 | 目录、章节地图、阶段概览 | 当前重点是否有观众可见理由 |
| 全局—局部 | 全局场约 55–70%，局部放大约 20–35% | 地图、器物、界面、复杂 Figure | 局部从哪里来、为何重要 |
| 主张—回响 | 主张约 35–55%，视觉回响承担另一侧或背景 | 金句、建议、结尾、转折 | 视觉锚点是否增加信息 |

### 构图预检

在写 HTML 前快速确认：

- 第一眼焦点是否明显强于辅助信息；
- 主要图片、图表或机制是否真的获得画布，而不是被困在半屏槽里的小框；
- 留白是否服务焦点、方向、停顿或非对称张力；
- 同级对象是否真的同级，内容量不同时是否仍被机械等宽等高；
- 标题、图像、图表和说明是否形成关系，而不是各自占一块；
- 与前后页相比，媒介、主辅尺度、方向或密度是否有叙事需要的变化。

若一整章连续使用 50/50 分屏、等分栏、卡片墙、矩阵或中央小岛，先回到页面地图调整证据媒介与视觉节拍，不要靠页级装饰制造“多样性”。

## 目录

- 1. 时间轴 / 里程碑
- 2. 数字指标行
- 3. 左右对比
- 4. 流程 / 逻辑树
- 5. 左文右图 / 左图右文
- 5.5 多人 / 主创介绍
- 6. 引文 / 金句
- 7. 满图封面 / 章节页
- 8. 破格 hero / 焦点页
- 8.6 收束页 / 行动末页
- 9. 概念图：媒介选择、Canvas 骨架、架构、关系网络、路线图、雷达、对比、漏斗、循环、金字塔、图片式概念图、小型 SVG 例外

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
  <img class="img-cover" src="../assets/img_03.png" style="height:520px; border-radius:12px; --img-focus-x:62%; --img-focus-y:42%;"><!-- 焦点值来自 crop_contract；src 用规划回填的真实路径 -->
</div>
```
（`.split-media` 已在 base.css。只有 `crop_contract` 允许裁背景时才用 `.img-cover`，并通过 `--img-focus-x/y` 对准主体；关键主体必须完整或比例差距大时用 `.img-contain` / 调整槽位，别拉伸或硬裁。）

### 5.5 多人 / 主创介绍（portrait ensemble）

人物身份本身是页面内容时，让真实照片承担识别，不把页面退化成 3×2 的纯姓名卡墙：

- 人数较少时，可用 2–4 张足够大的肖像与姓名、职责组成编辑式人物带；照片裁切、视线方向和色温保持亲缘，文字不压在人脸上。
- 人数较多时，可采用一张团队/机构场景图作为主视觉，再突出少量关键人物；或使用紧凑的 2×3 肖像网格，但头像必须仍可辨认，姓名和角色只出现一次。
- 只有部分人物有可靠图片时，按真实素材重新组织层级，不用无关照片、AI 相似脸或空头像占位补齐。
- 人物图不应缩成角落里的装饰邮票；若照片面积不足以建立识别，就减少同屏人物、拆分叙事或改用团队场景图。
- 纯排印是经过真实检索仍不可得后的降级，不是为了避免假真人而预先选择的默认版式。

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
<section class="slide slide--cover slide--bleed">
  <div class="bleed"><img class="bleed-cover" src="../assets/img_cover.png"></div><!-- src 用规划回填的真实路径;img_cover.png 只是占位示意 -->
  <header class="slide-title"><span class="kicker">章节 / 场合</span>大标题</header>
  <div class="slide-body"></div>
</section>
<style>
/* 图上叠字要保证对比:给标题区加一层渐变压暗 */
.slide--bleed .slide-title{ text-shadow:0 2px 16px rgba(0,0,0,.5); }
</style>
```
满铺背景**不算"出血"硬伤**;但图上文字对比不足要修(压暗层 / 色罩)。封面与结尾页默认没有页脚/页码；章节页只显示真实章节信息，不补 `SCENE / COVER / END` 或假档案编号。普通满图内容页若规划明确需要，才使用标准页脚家具。

满幅 `cover` 只代表画布铺满，不代表裁切正确。按 `crop_contract` 设置焦点，并在最终 PNG 中确认必须保留的人脸/头顶/手势、产品轮廓、作品主体或证据标签仍完整；若无法同时铺满与保主体，优先改构图或改用 `contain`，不要继续放大图片。

---

## 8. Hero / 焦点页

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
hero 页**不套常规标题区也行**（数字/金句本身就是标题）；它的作用是和普通内容页拉开反差。只在真正的叙事高点使用，重复到失去对比时就收回安静版式，不按固定页数判断。

---

## 8.5 编辑式设计动作

模板感的根源是"每页都居中标题 + 一排卡片"。**封面 / 章节页 / hero / 重点页**主动选 **1 个**大胆版式动作(别叠 3 个,乱),就能从"PPT 模板"跳到"品牌 / 展览 / 杂志"质感。按气质挑一个:

- **巨型裁切标题跑出血**:超大中文标题被画布边裁掉一部分(`font-size` 极大 + 定位让首/末字压边),读着有张力。⚠️ 是**有意裁切**、不是 `⚠ OVERFLOW` 失控——裁的是装饰性大字的边缘笔画,不是正文信息。
- **竖排标题贯穿左/右缘**:中文主标题竖排(`writing-mode:vertical-rl`)顶天立地贴一侧安全线,主视觉占另一侧(见 culture 示例的「礦物顏料壁畫研究」)。
- **斜体衬线英文横切构图**:一行斜体衬线英文(`--font-display-serif` italic)斜穿或横贯画面当装饰层,中文信息压在其上/下。
- **技术注 / 标本框**:只有页面确有真实图号、作品编号、采集时间、材料标签或用户提供的档案信息时，才把它们用 monospace 小字 + 细测量刻度线框组织成“标本 / 档案 / 研究系统”。不得为了显得高级而编造 `CATALOGUE NO.`、坐标、日期、`SCENE / COVER / END`、页数/时长或内部制作状态；没有真实技术信息时，用裁切、构图、色场、线条和材质建立系统感。
- **图字二分**:画面左右或上下**硬分两栏**——一栏是满幅图/材质，另一栏是标题 + 必要副题/真实身份信息组成的排版墙；不能用伪元数据和重复标签凑密度。
- **大留白 + 单点强碰撞**:大片安静留白(或纯色/纯黑),只放**一个**强烈的图或字,重心偏置但明确。
- **图窗 / 嵌套**:主图里再开一个小图窗(画中画 / 框景),或多个不同形状(`.ph-*`)的图按网格错落。

**使用原则：**一页选一个主动作，其他元素让位。关键页在字体、色彩、图片处理或图形语法上彼此回应，不每页另起一套。动作服务内容和场合；不牺牲正文、人脸、产品或证据信息。辅助文字必须对听众有用，同一语义不在眉签、角标、说明条和页脚重复。

## 8.6 收束页 / 行动末页

末页先在计划中确定身份，不能把两种页面混在一起：

- **收束页**：回应封面或核心命题；一个主张、至多一条短支撑和一个视觉锚点。默认光学居中，不使用内容页标题槽、页脚、页码或三栏总结。
- **行动末页**：CTA 本身就是交付重点时，可以保留一项明确行动和必要入口；防坑清单、规则集合和多组下一步应另占内容页，不能再叠加“收束金句”伪装成两种页型。

```html
<section class="slide slide--cover slide--closing slide--bleed">
  <div class="bleed"><img class="bleed-cover" src="../assets/closing-visual.png"></div>
  <div class="closing-core">
    <p class="kicker">与封面呼应的真实章节语义</p>
    <h1>一句能被带走的收束主张</h1>
    <p class="closing-support">至多一条必要支撑或下一步。</p>
  </div>
</section>
<style>
.slide--closing{ position:relative; display:grid; place-items:center; }
.closing-core{ position:relative; z-index:1; width:min(980px,78%); text-align:center; }
.closing-core h1{ margin:.18em 0; font-size:clamp(58px,6vw,96px); line-height:1.08; }
.closing-support{ margin:18px auto 0; max-width:760px; font-size:var(--fs-h2); }
</style>
```

图片只是视觉锚点时可降低明度或放到一侧形成对重；不要为了填满末页加入摘要卡、伪按钮、进度条和重复元信息。

---

## 9. 概念图 archetypes（Canvas / 图片优先）

> 大型机制、结构、关系和流程图默认使用 **Canvas 几何 + HTML 标签**，或“无文字图片 + HTML 标注”。SVG 只用于 icon、logo、箭头、标记和小装饰，不作为半屏/全屏主视觉。

### 9.1 大型示意图的媒介选择

| 表达对象 | 默认媒介 |
| --- | --- |
| 精确节点、连线、阶段、层级 | Canvas + HTML 标签 |
| 场景化机制、空间隐喻、视觉冲击 | 真实/生成图片 + HTML 标注 |
| 定量比较、趋势、分布 | ECharts |
| icon、logo、箭头、小标记 | 小型 SVG |
| 普通要点 | HTML/CSS 排版，不强行图解 |

大型图统一要求：一图一个关系，节点通常 ≤7，主方向明确，关键路径只点一个 accent，图占正文区主要面积。

### 9.2 Canvas diagram 通用骨架

Canvas 只画几何；准确文字、数字和来源用 HTML 覆盖层。这样既保留清晰结构，也避免 Canvas 文本模糊和 AI 图片乱码。

~~~html
<div class="diagram-canvas canvas-diagram">
  <canvas width="1200" height="560" aria-label="流程结构示意图"></canvas>
  <div class="diagram-label label-a">输入</div>
  <div class="diagram-label label-b">处理</div>
  <div class="diagram-label label-c">输出</div>
</div>
<script>
document.fonts.ready.then(() => {
  const root = document.querySelector('.canvas-diagram');
  const canvas = root.querySelector('canvas');
  const ctx = canvas.getContext('2d');
  const css = getComputedStyle(document.documentElement);
  const dpr = Math.max(1, window.devicePixelRatio || 1);
  const w = root.clientWidth, h = root.clientHeight;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  canvas.style.width = w + 'px';
  canvas.style.height = h + 'px';
  ctx.scale(dpr, dpr);
  ctx.strokeStyle = css.getPropertyValue('--line').trim();
  ctx.fillStyle = css.getPropertyValue('--panel').trim();
  ctx.lineWidth = 2;
  // 按规划绘制节点、连线和强调路径；文字留给 HTML 标签。
});
</script>
~~~

护栏：

- Canvas 容器显式给尺寸，不能依赖默认 300×150；
- 从 CSS token 读取颜色，不写裸 hex；
- 每次绘制前按 DPR 调整内部像素；
- 先画背景/连线，再画节点；HTML 标签在最上层；
- 标签位置由同一坐标系统换算，避免“图在一处、字漂到另一处”；
- 不做动画，页面加载后直接呈现最终态。

### 9.3 三层架构 / 层级图

- 3–5 层，沿单一轴排列；
- 同级节点同尺寸、同间距；
- Canvas 画容器、分隔和连接；HTML 标签写层名、职责和关键接口；
- 需要真实系统界面或空间隐喻时，改用图片底图 + HTML 标注。

### 9.4 思维导图 / 关系网络

- 中心概念 1 个，一级分支 3–6 个；
- 只有关系真实且主题确为网络/图谱时才使用网络形态；
- Canvas 画主干和节点，不画发光粒子星座；
- 标签放在节点附近的 HTML 层，不把长段文字塞进圆形节点。

### 9.5 路线图 NOW / NEXT / LATER

- 三段水平或垂直推进，每段只留 1–3 项；
- Canvas 负责主轴、节点和当前阶段强调；
- HTML 负责阶段标题、日期与行动；
- 若内容主要是任务清单，直接用 flow / CSS，不必启用 Canvas。

### 9.6 雷达 / 多维比较

使用 ECharts radar，不手画 Canvas/SVG。维度通常 4–7 个，量纲一致或已归一化；突出一个主系列，其余退为中性色。

### 9.7 优劣 / 方案对比

优先使用 arch-compare 的 HTML/CSS 双栏；只有需要连续权衡轴或关系连线时才用 Canvas。所有方案使用同一比较维度。

### 9.8 漏斗

- 3–5 层，从宽到窄；
- 精确比例有数据依据时用 ECharts；无定量含义时用 Canvas 几何；
- HTML 标签写层名、值和转化率；
- 不用彩虹分层，只用同色系明度阶梯。

### 9.9 循环 / 反馈回路

- 3–6 个节点沿环或圆角路径排列；
- Canvas 画环线、箭头和节点；HTML 标签放在节点外侧；
- 关键反馈路径只强调一处；
- 如果循环只是四个普通步骤，优先用 CSS flow，避免过度图解。

### 9.10 金字塔 / 优先级

- 3–5 层，底宽顶窄；
- Canvas 画层级面，HTML 负责层名和解释；
- 层色用同色系明度阶梯；
- 若每层解释较长，改用分层图片/色带 + 右侧 HTML 说明，不把文字挤进三角形。

### 9.11 图片式概念图

适用于场景、隐喻、空间关系和抽象机制：

1. Image brief 写清主体、构图、色调和留白位置；
2. prompt 明确 no text / no watermark；
3. 图片只承载视觉场，准确标签和数值放 HTML；
4. 用局部 scrim、text plate 或 leader line 建立图文关系；
5. 真实身份重要时必须取真图，不用生成图伪造。

### 9.12 小型 SVG 例外

允许使用 SVG 的范围：

- ≤96px 的 icon、logo、箭头、标记；
- 页码、品牌母题和小型装饰；
- 用户提供且必须保持准确的矢量资产；
- 用户明确要求矢量交付。

SVG 使用 currentColor/token、保持单一图形语言，不在一个页面混合多套 icon 风格。旧页面已有大型 SVG 时可以维护，但复杂改造优先迁移到 Canvas 或图片 + HTML 标签。
