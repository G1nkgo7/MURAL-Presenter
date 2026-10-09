# HTML 与实际画布合同

制作首稿、改变骨架的局部构图或排查几何问题时读取本文件。它规定能正确渲染、组合和修订的接口，
不是版式模板。页面怎样承载论点，见 [页型索引](../page-index.md) 与所属分页计划。

## 画布、坐标与字体

`base.css` 中 `--canvas-w`、`--canvas-h` 决定整册作者画布；用户明确尺寸时采用该尺寸，未指定时为
1600px × 900px。渲染读取实际 CSS/DOM，PNG、预览与导出沿用实际几何。不要建立 Canvas JSON、
在单页覆盖画布宽高，或用黑边和 `transform: scale(...)` 把不同尺寸伪装成同一画布。

统一 `.page-stage` 覆盖完整画布，默认以 `--safe-y / --safe-x` padding 为正常流内容施加一次安全边距；不要再套一层同样的边距。
全出血图像可放 `.page-background`，或由作者在本页舞台中明确安排全幅图层与文字安全区。全册安全区、画布或字体变更由 Orchestrator 统一处理，
不要让同一组里的各页形成不同坐标体系。高清输出倍率不改变作者坐标。

字体使用 `--font-display`、`--font-title`、`--font-body`、`--font-number`、`--font-annotation` 等共享角色。
整册字体由共享设计确定；可使用实际可加载的公网字体 CSS / `@font-face`，不读取机器任意字体。字体选择、合法缺字回退和实际应用回执见
[字体与排印](../typography/fonts-and-type.md)；共享字体引用由执行代码准备，不手写机器绝对路径。

`base.css` 提供运行底座、五个无视觉皮肤的结构原语和本册实际共享设计，不预载卡片、表格、媒体形状或标题字数分档。
整体规范在 deck.md，共享实现由 Orchestrator 落到 base.css；仅本页使用的样式写在所属 HTML 内。结构原语只解决安全尺寸、基本间距与网格起点，组件外观仍须明确实现。

共享 CSS 与使用结构一起交接。若本册需要“章节在左、页码在右”的导航，可在 deck.md 说明适用页与以下最小用例，CSS 写在 base.css：

```html
<nav class="deck-nav">
  <span class="deck-nav__topic">当前章节</span>
  <span class="deck-nav__page">02 / 10</span>
</nav>
```

这是可改的用法示例，不是预载组件或新的必填协议。说明它属于页面舞台的正常流还是以画布为参照定位，各页沿用同一元素顺序与坐标归属，只替换内容；不要在别处再加一份页号。
同职责标题、正文、图注与导航的字号/字重由共享样式提供。标题换行、主图比例等局部表达可以不同；封面或转场的排印例外由整体规范说明，不因每页作者不同而改变字号。

## 编辑的是现有 fragment

每页 canonical 文件是 `slides/slide_NN.html`。Harness 在页面分配或 Orchestrator 首次写入前自动准备最小空容器。先读取当前文件，再用 `patch` 一次完成可评价的首稿：
填入正文、视觉和本页 CSS，而不是先反复检查空容器。已有文件优先局部编辑；文件确实缺失或需要整体恢复时，
才用 `write_file` 重建完整合法 fragment。不要另建 `_r2`、备用 HTML 或修改拼装后的 `present.html`。

保留以下技术身份与共享输入：

- 唯一根 `<section>`，以及一致的 `id="slide-NN"`、`data-slide="NN"` 与页面责任相关 metadata；不需要声明页型或 frame。
- 计划中的标题链原文、适用时的章节编号，以及按设计规范实际制作的页脚；标题可加不改字的 `<span>` 或 `<br>` 调整排印。
- 唯一一对 `SLIDE_AGENT_FILL_START / SLIDE_AGENT_FILL_END` 注释与所在结构层。
- 统一背景层与内容舞台；内部结构按本页构图决定，不规定封面、内容页分别使用哪种外壳。

marker 内承载本页主体内容，正文 DOM 可以重排。`page_body_unfilled` 只说明这一区域仍为空，不证明整页没有内容；已有主体写在区域外时移动现有 DOM，不重新制作，也不填隐藏文字来满足协议。
本页 CSS 用 `#slide-NN` 限定，不能让 `.card`、`h1`、`figure` 等裸选择器影响其他页。
共享 `.deck-*` 家具沿用整册定义，不以页面局部 Footer 补丁修饰共同坐标问题。

## 自动准备的最小运行容器

后台页面准备不放可见标题、页眉、列布局或素材槽，也不产生模型工具动作。先理解 brief 中的叙事职责，再按[页型索引](../page-index.md)选读相关制作方法：

```html
<section class="slide" id="slide-01" data-slide="01" data-layout-contract="page-owned">
  <div class="page-background" data-slide-owned="background" aria-hidden="true"></div>
  <div class="page-stage" data-slide-owned="body">
    <!-- SLIDE_AGENT_FILL_START -->
    <!-- SLIDE_AGENT_FILL_END -->
  </div>
</section>
```

上例是空运行结构，不是成品。marker 内由作者实际写入标题、正文、图像、图表与必要导航；可按表达关系组织 grid、flex 或明确的图层，不必先建立传统的“页眉＋正文”。
`.page-background` 与 `.page-stage` 都覆盖完整作者画布；背景不挤占安全区。舞台默认 padding 不等于必须把全幅媒体限制在文字框里，
但用绝对定位时应明确坐标相对哪个容器，不重复计算安全边距。嵌套容器、图片和父容器都须有真实的尺寸依据。
只把本册确实共享的规则放 base.css；本页标题位置、图文比例、裁切和局部层次使用 `#slide-NN` 作用域 CSS，字号沿用对应职责的共享层级，有必要的例外再明确调整。
标题只保留一份真实实现，不附加重复的小页眉，不用隐藏副本满足不存在的模板要求。
普通内容页也可用全幅图，封面也可用正常流；方法服务本页任务，不由程序读取“cover”等词决定。
已有正确构图按需局部修改；改变本页职责时同步相关计划和当前 HTML，不能只改一句计划就宣称画面已更新。

## 可组合的结构原语

以下五个类可以直接与 `.page-stage` 组合，也可放在其内部的唯一布局容器上。它们不携带颜色、卡片、页眉、页脚或页型语义；根据 brief 调整局部比例和内部层级，不为了使用类名硬套内容。

```html
<!-- 主视觉或证据场 + 简短标题/问题区 -->
<div class="page-stage deck-layout deck-layout--hero">...</div>

<!-- 稳定标题锚点 + 大尺度证据区 -->
<div class="page-stage deck-layout deck-layout--focus">...</div>

<!-- 主次明确的并置 -->
<div class="page-stage deck-layout deck-layout--split">...</div>

<!-- 共同尺度下的双向比较 -->
<div class="page-stage deck-layout deck-layout--compare">...</div>

<!-- 步骤、时间或因果推进 -->
<div class="page-stage deck-layout deck-layout--sequence">...</div>
```

`hero` 和 `focus` 都要求一个真正承重的视觉对象；`split` 不是两张无关卡片；`compare` 仍需作者实现共同基线、标签和口径；`sequence` 仍需明确方向与连接关系。某页需要不同结构时使用本页作用域 CSS，不新增全册模板名称。

## 图像、图表与运行依赖

本地素材沿 Catalog 的真实路径使用。HTML 位于 `slides/` 时，素材引用通常相对为 `../assets/...`；
按当前文件位置解析，不猜生成文件名，不将来源网页 URL 当图片地址。禁止 `<img src>`、`srcset` 或 CSS `url()` 指向 `inputs/`、`inputs/extracted/`、`../inputs/` 或任何附件解析目录；那些路径只供 Image 裁切/登记的 source，画面引用必须是 Catalog 已登记的 `assets/...`。直接引用附件路径会触发 `render dependency escapes workspace`。也可直接使用公网图片、字体、CSS 和脚本，
以实际加载、解码和像素回执确认可用性，不因没有本地副本或远程 hash 单独拒绝制作。禁网或离线交付要求仍优先；
远程资源可能变化或失效，保留它们的 HTML/ZIP 需要联网，不能宣称离线可重现。来源定位仍落盘而非自动上屏。
ECharts 来自当前固定 Skill，使用 `assets/vendor/echarts.min.js`（从单页 HTML 引用时为 `../assets/vendor/echarts.min.js`），局部图表脚本只作用于本页唯一容器。
首次使用前工作区 `assets/vendor/` 可能尚不存在；写好引用后，渲染准备会在浏览器启动前提供库，不因 `ls` 查询提前创建。
若固定 Skill 真正缺库，如实说明；选择可用的公网库时明确改引用并验证加载，不把原依赖失败伪报为成功。Canvas 绘制文字需从当前 PNG 核对，源码字号不等于实际屏显字号。

全出血不是一句 `object-fit: cover`：媒体及其父容器必须有明确边界，主体须进入实际裁切窗口。
使用 `<figure>` 时明确处理默认 margin 和高度；父容器没有有效高度时，子图的 `height:100%` 与 `object-position`
可能完全不起作用。裁切反复修改却 `pixel_unchanged` 时，先检查祖先 bbox、高度链、overflow 与选择器命中，
不要继续盲改百分比。原始 Figure 保留论证必需的轴、图例、标签与面板关系。

文字需要底板时，把图片、底板、文字安排成明确的叠放关系；底板放在图片下方不会改善文字对比。检查父级形成的层叠上下文，不能只比较不同父级中的 z-index 数值。
连线与目标共享坐标依据：SVG 中端点与对象用同一映射，HTML 标注按实际容器位置计算；文字换行或读数块移动后同步核对路径、端点与文字避让，以实际坐标与像素为准。

数值、图形端点与刻度来自同一数据和绘图区。HTML/CSS 条图的标签区与数据区分开，条轨和轴共享宽度与零点；
比例不能依靠目测。复杂主视觉优先合适的位图，SVG 留给精确数据、简单关系和局部标注，见
[图表索引](../chart-index.md)。

来源定位和制作信息默认不进入可见文案，上屏例外见 [Slide 角色卡](../../roles/slide.md)；
这不影响 HTML 的正常素材路径引用，也不删除帮助理解的图注、单位、时间范围、口径和事实限定。

## 检查与修改

单页或 Group 的真实像素检查与修订入口见[执行参考](commands.md)。`inspect` 会在需要时自动渲染当前页面或联系表。patch 未命中时先读取实际片段，再提交一次针对当前文本的修改；HTML、共享 CSS 或引用素材变化后检查当前产物，相同且已有有效检查的像素不机械重复检查。PNG 存在或后台渲染成功只证明产物生成，不能代替对实际画面与计划的判断。
