---
name: ppt-skill
description: 把主题 / brief / 提纲 / 文档转成完整的 HTML 幻灯片演示文稿——每页一个独立 HTML 文件,画布 1600×900(16:9),视觉风格按主题量身定。多 Agent 全流程:编排器做调研、全局与每页规划、基于 token 的设计系统(base.css),并行委派 subagent 写页、渲染、自检与全局复审,交付成套可演示的幻灯片。当用户要做 PPT / 演示文稿 / deck / slides / 幻灯片 / 演示 / presentation,或要把主题、提纲、文档转成幻灯片时使用。
---

# ppt-skill

生成 HTML 幻灯片演示文稿。每一页是一个独立 HTML 文件,画布 **1600×900(16:9)**。

这是**多 Agent 工作流**:你是**编排器**,**专注"任务规划"**——吃透 brief、做全局规划与设计系统(`base.css`)、把每一页拆成自包含的页规划。生产环节都**委派给专门的 subagent**:

- **research subagent**(前置,**可并行多个**)—— 搜事实 / 数据 / 引述,fact pack 落盘 `research/`;主题跨多块时按子主题分工、一次派多个并行;
- **image subagent**(前置,**可并行多个**)—— 按 brief 集中生成全套配图、逐张核对配色,各写独占 Manifest;图多时按图分组、一次派多个并行;
- **slide subagent**(**并行**,每页一个)—— 写、渲、自纠该页 HTML;
- **review subagent**(收尾)—— 横扫整套截图,返回问题清单。

你基于这些 subagent 的返回与正式文件**做决策**(规划、校验素材、判断返工);**自己不调研、不出图、不写页、不渲染、不看图。**

> 不同框架里启动 subagent 的工具名不一样(`delegate_task`、`task`、`spawn`、`run_subagent`…)。本文档统一叫它「调用 subagent」——用你当前环境实际提供的那个即可。四个"角色"(research / image / slide / review)**不是预定义类型**,而是**同一个 subagent 工具传入不同的 `goal` + `toolsets`** 现拼出来的。

## 配套文件(按需 `read`,别一股脑全载)

| 文件 | 是什么 | 谁、什么时候读 |
|---|---|---|
| `references/design-styles.md` | 风格库**参考**(74 风格 / 22 色调 / 29 主色;非强制,灵感用) | 编排器**风格决策**时参考(阶段一·3) |
| `references/base-template.css` | `base.css` 起手模板:token 占位 + **固定版式骨架**(画布 / 安全边距 / 标题区 / 页脚 / 页码 / 字号阶梯含最小字号 / 系列色 / 满铺类) | 编排器写 `base.css` 时**复制**(阶段一·4) |
| `references/layout-patterns.md` | 难版式范例(时间轴 / KPI 行 / 对比 / 流程 / 左文右图 / 引文 / 满图;只引 token、嵌 `.slide-body`) | 编排器规划版式、slide 排正文时参照 |
| `references/quality-checklist.md` | 质量判据(**单页 + 整套**),deck 质量单一真相 | 编排器规划自检;slide / review subagent 自检与复核 |
| `subagents/research.md` | research subagent 职责卡 | research subagent 开工前 |
| `subagents/image.md` | image subagent 职责卡 | image subagent 开工前 |
| `subagents/slide.md` | slide subagent 职责卡(写页要求 / 自检判据 / 循环纪律) | slide subagent 开工前 |
| `subagents/review.md` | review subagent 职责卡 | review subagent 开工前 |
| `scripts/render.py` | HTML→PNG 渲染脚本(自包含,不依赖宿主 render 工具) | slide subagent 用 `bash` 跑 |
| `scripts/merge_image_manifests.py` | 校验各 Image fragment 并原子生成唯一 `assets/image-manifest.json` | 编排器在 Image 全部返回后运行 |

**委派任一 subagent 时,在 goal 里让它先 `read` 自己的职责卡**——它据此干活,你不必把职责细节重复进 goal,只给它具体任务(子主题 / image brief / 页号 / 截图路径)和该用的 `toolsets`。

## 边界(先读这个)

- **你只规划与委派,不做生产。** 调研、出图、写页 HTML、渲染、看截图——这些**生产活全部交给 subagent**;你的工作是吃透 brief、规划、写 `base.css`、写页规划,再根据 subagent 的正式产物和返回做决策(校验素材 / 判断返工 / 改设计系统)。
- **自主推进,不提问。** 除非硬性阻塞(权限 / 登录 / 缺输入文件),否则不向用户提问、不等确认;自行补齐合理假设,做有品味的决定,继续执行。
- **页面 HTML 由 slide subagent 写,你绝不自己写或改。** 你只写规划文件(`plan/`)和 `base.css`;`slides/slide_NN.html` 一律经 subagent 产出。**即使是跨页统一的小改动也不例外**:跨页**视觉**统一(配色 / 字体 / `.slide-title`·`.kicker` 等骨架样式)改 `base.css`,跨页**内容**修正带 note 重新委派受影响的页。你对 `slides/` 的 `write`/`edit` 会被直接拒绝。
- **图表是代码,不是图片。** 数据图表一律用 ECharts 在 HTML 里手写;image subagent 的 `image_generate` 只用来生成照片 / 插画类视觉,绝不用它伪造图表。

## 角色与工具

**编排器(你)用——只规划 + 委派 + 决策:**

- `read` —— 读 brief / 配套文件 / 落盘的 `research/` / 规划(只读文本)。
- `write` / `edit` —— 写 `plan/` 规划文件和 `base.css`(**写 `slides/` 会被直接拒**)。
- **subagent 工具** —— 委派下面四类 subagent;每类传对 `toolsets`(见表)+ 在 goal 里让它先读职责卡。

> 编排器**不渲染、不执行命令、不看图**(没有 `bash` / `vision_analyze`),所以碰不到 `slides/`,也只能靠 subagent 的**文字返回**来"看见"页面。要刷新某页视觉:改 `base.css` 后**重新委派**那一页。

**四类 subagent(类型由 `toolsets` 现拼;执行细节看各自职责卡):**

| 角色 | `toolsets` | 实得工具(含自动并入的 `read`) | 干什么 | 返回给编排器 |
|---|---|---|---|---|
| research | `["web","file"]` | read / web_search / web_fetch / write | 查事实数据,fact pack 落盘 `research/research_NN.md`(**可并行多个**,按子主题分工) | 简短指引(写到哪个文件 + 关键结论) |
| image | `["file","image_gen","vision"]` | read / write / image_generate / vision_analyze | 按 brief 生成全套照片 / 插画,核对配色(**可并行多个**,按图分组)，各写独占 Manifest | 完成状态 + `assets/image-manifests/<label>.json` |
| slide | `["file","terminal","vision"]` | read / write / edit / bash / vision_analyze | 写 `slides/slide_NN.html`、渲染、看图自纠 | 简短总结(状态 + 截图路径) |
| review | `["vision"]` | read / vision_analyze | 横扫整套截图,诊断全局问题(**只诊断不改**) | 问题清单(哪页 / 什么问题 / 建议) |

> **怎么渲染**(slide subagent 用 `bash` 跑 skill 自带脚本):把某页 HTML 渲成 PNG——
> ```
> python skills/ppt-skill/scripts/render.py slides/slide_NN.html renders/slide_NN.png
> ```
> 脚本自处理依赖(无头 Chromium、LD_LIBRARY_PATH、等字体就绪防豆腐块),成功后把 PNG 路径打到 stdout。**渲完用 `vision_analyze` 看那张 PNG**(`read` 看不了图)——这是 subagent"看见"页面的唯一方式。

## 工作区布局

```
research/
  research_01.md     # fact pack(research subagent 落盘;并行调研各写一份,文件名由编排器指定)
  research_02.md ...
plan/
  deck.md            # 全局规划
  slide_01.md        # 每页一份规划
  slide_02.md ...
base.css             # 设计系统:所有颜色 / 字体 / 间距 token
assets/              # 配图:image subagent 的 image_generate 产物或已有素材
  image-manifests/   # 各 Image Agent 独占的映射 fragment
  image-manifest.json # 编排器确定性合并后的唯一位图映射
slides/
  slide_01.html      # 每页一个 HTML 文件(由 slide subagent 写)
renders/
  slide_01.png       # 渲染截图,文件名对齐页号
  slide_02.png ...
```

所有路径都是**相对路径**,根落在本次运行的工作区,按此布局读写即可(`skills/ppt-skill/...` 例外:那是本 skill 的配套文件,只读)。

**渲染产物统一放 `renders/`**,文件名与页号对齐(`slide_NN.png`),每次重渲覆盖同名文件——不要把截图散落在 `slides/` 或工作区根目录。`render.py` 的输出路径就指向这里,`vision_analyze` 用它。

## 设计规范

- **整体服从选定的风格。** 全套一致地执行你在规划里定的 风格 + 色调 + 主色(`references/design-styles.md` 可作参考,非必须)。无论哪种风格,通用底线都成立:层级清晰、每页只讲一件事、留白与重心有节奏、调色板收敛(用主色 + 少量辅助,别失控);用色强弱跟着风格走(简约就克制,波普 / 国潮就大胆,但仍守住选定的调色板)。
- **`base.css` = 设计系统 + 版式骨架,slide 只用 token、套骨架。** 复制 `references/base-template.css` 起手:① **token** —— `--bg` / `--accent`(= 选定主色 hex)/ `--ink` / 辅助色 / 状态色 / 字体 / **字号阶梯(含最小字号硬下限 `--fs-min`)** / 间距;slide HTML 只引用 token(`var(--ink)` …),**绝不写裸 hex**。② **版式骨架** —— 固定的 `.slide` 画布(1600×900 + 安全边距 + 栅格)、`.slide-title` / `.slide-body` / `.slide-footer` / 页码 的**固定位置与体例**;每页都套这套骨架,**框架类不改位置 / 字号 / 配色**,只在 `.slide-body` 内按页型变布局。**这是治"标题 / 装饰线 / 页码每页乱动、对齐错位、重心偏移、出血"的关键:框架全套一致,只有正文区按页型变化。** 章节名 / 过渡页小标签用骨架自带的 `.slide-title .kicker`,别另造结构;封面 / 章节满图页用 `.slide--bleed` 满铺(见 `references/layout-patterns.md`)——这种**合法满铺不算出血**。
- **⚠️ 奶油 / 米黄 / 暖白底是被严重滥用的"安全牌",默认就别用它。** 数据里近一半 deck 都偷懒用了暖奶油底 —— 这是头号雷同。**动笔前先问:这个主题最该是什么底?** 然后让背景跟着**风格 + 色调**走:
  - **商务 / 科技 / 数据 / 金融 / 政务 / 奢侈 / 暗黑极简 / 赛博** → 优先**深底**(深蓝黑 / 近黑 / 深墨绿),利落有力;
  - **医疗 / 科研 / 法律 / 冷色调** → 冷白蓝 / 冷灰;
  - **高对比 / 包豪斯 / 杂志** → 纯白或纯黑;
  - **奶油 / 暖白只留给真正偏暖的**:生活 / 文教 / 手作 / 婚庆 / 母婴 / 餐饮等;别的主题用到暖白要有明确理由。
  目标是让背景在「暗 / 冷 / 高对比 / 暖白」之间**真正铺开**,而不是清一色奶油。
- **全套只用一个背景模式。** 要么亮底、要么暗底:封面 / 过渡页可以用**同一色系的更深 / 更浓变体**做戏剧感,但**绝不在亮底和暗底之间来回跳**——暗底封面配亮底正文那种"两个 deck"的割裂感,就是这么来的。
- **调色板封闭:任何颜色都必须来自调色板,包括状态色。** `base.css` 要把**这套 deck 会用到的所有颜色**一次定全——含**涨跌 / 正负 / 警示 / 提示**这类状态色(如 `--up`/`--down`/`--warn`/`--note`),且这些状态色要**和选定主色调和**(从主色 / 辅助色派生的深浅、或同色系的暖冷偏移),而不是直觉里的大红大绿大粉。**严禁**为了"负数 / 风险 / 提示"就硬塞一个调色板外的红 / 粉 / 绿(例:导航金 deck 里冒出 `#FBEFEF` 粉底免责条、或鲜红的 `-12.5%`,就是出界)。表达涨跌优先用:箭头 / 正负号 / 位置 + 调色板内的深浅或单一强调色;免责 / 提示条用主色的淡色调(如金色 deck 用米金淡底),不要粉不要红。**状态色硬规:`--up` ≠ `--warn`(涨色不能等于警示色);涨跌一律"颜色深浅 + 箭头 / 正负号"双通道,别只靠色相分辨。**
- **图表是代码。** 数据图表用 **ECharts** 在 HTML 里手写,**整套配色(系列色、坐标轴、网格线、文字、标注)全部取自 base.css 的 token**——多系列从 `--series-1…6` 取(JS 里 `getComputedStyle` 读**直接色值**;别用 `var()` / `color-mix()` 喂 ECharts,它解析不了),系列不够就基于主色派生深浅;**绝不用 ECharts 默认调色板,也不混入调色板外的颜色**;容器给显式宽高;不要用图片伪装图表。
- **配图克制 + 适配:能不配就不配,配就配准。**
  - **门槛(治多余素材 + 提速)**:只在**照片 / 插画确实承载内容**(真实场景 / 作品 / 人物 / 案例画面)时才配;装饰、图标、底纹、抽象、几何一律 **CSS / SVG**,不生成。每张图必须说得出**用途 + 对应页内哪段内容**——说不出就别生成。
  - **适配(治裁切 + 不匹配)**:编排器写 brief 时指定**画面主体 + 宽高比(匹配版式槽位)** + deck 主色 / 色调 / 情绪(如 "navy and muted gold, editorial, low-saturation"),让出图就贴着要用的比例,避免上屏被大面积裁切。
  - **统一**:全套用**一个视觉配方**(同媒介 + 同调色 / 处理,如统一低饱和 / duotone),别风格色调乱飞;配色必须进调色板。
  - **真实性**:生成的照片 / 插画统一标注"示意 / 插画"。
  - 生成 / 核对细节见 `subagents/image.md`;每页规划保留稳定 `asset_id`，Slide 只从 `assets/image-manifest.json` 解析实际路径。**别让一张五颜六色的图破坏整套**。
- **文字精炼** —— 用短语不要整段;1600×900 装不下密集长文,会溢出。
- **中文字体必须显式命名。** 渲染环境**已本地安装 Noto Sans SC / Noto Serif SC**,所以中文 deck 的正文 / 标题 `font-family` 必须显式带上 `"Noto Sans SC"`(或 `"Noto Serif SC"`),如 `font-family: "Noto Sans SC", sans-serif;`——否则可能落到无中文字形的字体上渲染成豆腐块(□□□)。`base.css` 里再加一条 Google Fonts 的 CDN `@import` 作可移植兜底(本地已有字体,不联网也能渲染,所以别把 CDN 当唯一依赖)。
- **避免 AI 味(AI-slop)**:不用 Inter / Roboto / 系统默认显示字体,不用千篇一律的靛蓝 `#6366f1` / 紫配白,不要什么都居中,不要一排排一模一样的卡片,不要无意义毛玻璃;相邻两页不复用同一种版式。

## 流程

### 阶段一 —— 规划(编排器的主战场)

**顺序很重要:先把材料备齐,再动笔规划——`委派 research` → `风格决策 + 设计系统` → `写规划` → `委派 image、合并唯一 Manifest`。** 这样规划才能引用你**真正拿到**的事实与素材,而不是先写死再回头凑。

1. **吃透 brief**:主题、受众、目的、语言、调性、页数(若未给,根据用户的描述自定义页数)。
2. **委派 research subagent(前置)**:需要时事 / 数据 / 事实时,把调研需求委派给 research subagent(给它 `toolsets:["web","file"]`,让它先 `read skills/ppt-skill/subagents/research.md`);它 `web_search`(搜文)→ `web_fetch` 最佳来源,把整理好的 **fact pack 落盘到 `research/research_NN.md`**(在委派 goal 里**给它指定唯一文件名**),只回一段简短指引;你再 `read` 该文件取用正文。**主题跨多个子领域、或要查的料多时,一次并行委派多个 research subagent**(`delegate_task` 的 `tasks` 数组,每条一个,和并行委派 slide 同一套机制),按**互不重叠的子主题 / 角度**分工(如 现状数据 / 案例 / 竞品 / 趋势 / 视觉参考),**各自落盘到不同的 `research_NN.md`**,你 `read` 全部、**合并去重**后用于规划;子主题单一、料不多时一个就够。每条 research goal 必须**自包含、范围不重叠**,免得查回重复或互相矛盾的事实。**含案例页、或要呈现具体真实人物 / 作品 / 品牌 / 产品时,不得跳过 research**——这些实体的代表作 / 性别 / 称谓 / 归属必须经核验再写进规划,**禁凭记忆直填**(无支撑就按"示意 / 泛例"处理)。**只有 brief 自带足够信息、又不涉及上述具体实体时,才可跳过这步。**
3. **风格决策**:给这套 deck 定一个**明确、连贯、贴合主题受众**的视觉方向——三件事都要**定死**:
   - **设计风格**(整体气质 / 版式语言,如「暗黑极简」「编辑杂志」「党政庄重」);
   - **色调**(明暗 + 冷暖 + 饱和,**直接决定背景**——按主题选暗 / 冷 / 高对比等,别滑回奶油 / 暖白默认);
   - **主色**(核心强调色,**给确切 hex**,如 `#002FA7`);`base.css` 的 `--accent` 就用它。

   `references/design-styles.md`(74 风格 / 22 色调 / 29 主色)是**参考素材 / 灵感库,不是必选项**——可以从里点名挑,也可以据它启发、或完全自定;读不读随你,拿不准想拓宽思路时翻一翻。重点不在"从库里挑",而在**定死且贯彻全套**:背景明暗、主色 hex 一旦定下,`base.css` 和每页都照着来。
4. **写 `base.css`(设计系统 + 版式骨架)**:**把 `references/base-template.css` 复制成工作区的 `base.css`**,只填 token——`--bg`(按选定色调)、`--accent`(= 选定主色 hex)、`--ink` / 辅助色 / **状态色**(和主色调和)、标题字体、**字号阶梯(含最小字号 `--fs-min` 下限)**。**结构类(`.slide` / `.slide-title` / `.slide-body` / `.slide-footer` / 安全边距 / 栅格)保持不动**——它保证全套每页框架一致。模板已带中文字体 + CDN `@import` 兜底。
5. **写 `plan/deck.md`**:主题 / 受众 / 目的 / 语言;**风格方向**(写下第 3 步定的 风格气质 + 色调(背景明暗冷暖)+ 主色确切 hex;若参考了 design-styles.md 可顺带记名字 / ID);一句话情绪、设计规格摘要(`--bg` / `--accent` hex / `--ink` / 字体);**页脚体例 / 页码**(页脚放什么、是否显示页码——全套照用;页码用骨架 `.page-no` 按页号自动填,属骨架白名单、不算"自创页脚");**页序** —— 每页一行 `slide_NN:页型 — 一句话总结`(页型从下面词表选),构成清晰叙事弧线;总页数;关键事实或假设。
6. **每页写一份 `plan/slide_NN.md`**(slide subagent 只照它执行,所以要"写死"不留发挥空间)。开头必须两行:**页型**(从词表选一个)+ **一句话总结**(这页讲的一件事);然后:
   - **最终文案(逐字)**:标题、正文、每个标签 / 数据项的**确切上屏文字**——slide subagent **逐字照用、不改写不扩写不编造**,所以这里就写成最终该显示的样子(短语化、别留"待润色"或占位)。**每条事实 / 数字标来源**(如"来源:research_02");靠推断或 research 标"待核"的,随文标"示意",让 slide 分得清哪些是真实事实、哪些是占位。
   - **版式**:从 `references/layout-patterns.md` **点名一种** body 版式写进来(时间轴 / KPI 行 / 对比 / 流程 / 左文右图 / 引文 / 满图 / 居中大字…),**相邻两页不得用同一种**;用 `base.css` 骨架 + helper 排,**框架(标题位 / 页脚 / 页码 / 边距)不动**,只排 `.slide-body`。
   - **必含要素**:按页型补硬性要素(见词表后的清单),缺了这页不算完成。
   - **视觉**:`image:`(需照片 / 插画时写稳定 `asset_id` + **画面主体 + 用途(配合哪段内容)+ 宽高比(匹配版式槽位)** + 主色 / 色调 / 情绪；实际路径由第 7 步总 Manifest 解析)或 `chart:`(ECharts 类型 + 实际数据)。需要数字而 brief / fact pack 都没给时,改成概念图 / 流程图 / 对比结构,或明确标注"示意"。
7. **委派 image subagent(前置)**:每条 image brief 先声明稳定 `asset_id`，再按不重叠的 ID 分组委派(给它 `toolsets:["file","image_gen","vision"]`,让它先 `read skills/ppt-skill/subagents/image.md`)。各 Image Agent 集中生成、审校并独立写 `assets/image-manifests/<实际 label>.json`；自然语言总结不承担路径映射。图多时可一次并行委派多个 Image Agent，每组仍使用同一主色 / 色调 / 情绪。全部返回后运行：

   编排器不直接执行命令，委派一个轻量 `image_manifest` 子 Agent（`toolsets:["file","terminal"]`）只运行：

   ```bash
   python skills/ppt-skill/scripts/merge_image_manifests.py . --expected image_01,image_02
   ```

   编排器读取结果与总 Manifest，确认命令 `status:PASS`；后续 Slide 只按计划 `asset_id` 读取总 Manifest。fragment 缺失时先读委派返回的 `summary_path`，再读该 Agent 的 `tool_log.json` 恢复原映射；两者都没有真实身份记录时才允许重新 Vision，禁止直接派 `image_map*` 猜用途。**全套无照片 / 插画需求(纯 CSS / SVG / 图表视觉)时,可跳过这步。**

**页型词表(规划时每页必须从中选一个):**

| 页型 | 页面任务 |
|---|---|
| 封面页 | 呈现主题、演讲者、场合与日期,确立整体基调并建立第一印象 |
| 过渡页 | 标示新章节或叙事阶段的开始,承上启下,提示听众当前所处位置(版式从简,仅含章节名/主题) |
| 开场页 / 引入页 / 背景页 | 借由问题、事实、数据、场景或趋势引入主题,阐明其重要性与听讲理由 |
| 目录页 | 列示内容结构与各部分顺序,建立整体框架与听讲预期 |
| 问题页 / 痛点页 | 界定当前的矛盾、困难、机会或挑战,为后续观点与方案确立必要性(仅陈述问题,不涉及解决方案) |
| 观点页 / 结论页 | 直接提出核心判断、主张或结论,以一句话明确表达立场 |
| 逻辑分析页 | 拆解原因、结构、机制或层级关系(流程图、因果、矩阵、架构、逻辑树等),阐明"为何如此"或"如何运作" |
| 数据页 | 以图表与数据佐证结论、支撑判断(ECharts),重在论证而非罗列 |
| 案例页 | 借助具体案例(成功、失败、竞品或用户故事)增强理解与说服力,并提炼其说明价值 |
| 方案页 | 阐明具体做法——策略、方法、路径、功能或行动,并回应前述问题与目标 |
| 计划页 / 时间轴页 | 呈现执行节奏:阶段划分、里程碑与时间节点,明确各阶段的产出 |
| 对比页 | 在统一维度下进行比较(方案、前后、自身与竞品),提炼差异并给出判断或建议 |
| 总结页 | 将核心观点凝练为数句,突出重点,形成明确的认知落点与记忆点 |
| 行动页 / 结束页 | 提出下一步行动或决策事项,或以呼吁、愿景、致谢、答疑、联系方式收束全篇 |

页型决定该页的版式与视觉重心(数据页以图表为主、对比页采用并列结构、过渡页力求简洁),相邻页应尽量切换页型,避免连续使用同一种。

**页型必含要素(硬性,缺了这页不算完成,规划时就要落实):**

- **案例页**:必须有**具体案例**(真实的人 / 事 / 品牌 / 数据 / 画面),不能只讲抽象道理;
- **数据页**:必须有**真实数据**(或明确标注"示意"),图表带轴 / 图例 / 标签;
- **介绍某人 / 某风格 / 某作品 / 某产品**:必须出现其**代表作品 / 实例 / 画面**(光有文字描述不够);
- **方案页**:必须有**具体做法**(策略 / 步骤 / 功能),不能停在口号;
- **问题页 / 观点页 / 总结页**:必须有**明确的一句话主张 / 结论**;
- **时间轴 / 计划页**:统一轴线(横竖全页一致)、节点等距、每节点「阶段名 + 时间/里程碑 + 产出」三件套对齐、节点 3–6 个(套 `references/layout-patterns.md` 的时间轴范例,别从零手搓)。

**硬约束:最后一页不得出现任何含"聆听"的字样。**

一份 slide 规划必须**自包含**:slide subagent 只凭 `plan/deck.md` + `plan/slide_NN.md` + `base.css`，以及本页有 `asset_id` 时的 `assets/image-manifest.json`，就能做出这一页(它看不到兄弟页,也看不到 `research/` 的 fact pack 原文)。事实 / 数字写进规划；位图只通过稳定 ID 解析。

8. **规划自检(委派 slide 前过一遍,有问题先改规划再委派)** —— 委派后每页要渲染自纠,代价高;规划期的问题在这步一次性堵掉:
   - **设计系统**:`base.css` 的 token 定全了吗(`--bg`/`--accent`/`--ink`/辅助色 + **状态色 `--up`/`--down`/`--warn`/`--note`**,且都和选定主色调和)?中文字体引入了吗?主色 / 背景 / 文字对比够吗?
   - **调色板 / 风格**:风格 + 色调 + 主色是不是**定死了、贴合主题、且全套统一**(主色有确切 hex、背景明暗冷暖已定)?`base.css` 的 `--accent` 用的就是定的那个 hex 吗?状态色没有出界的红 / 粉 / 绿?
   - **背景**:背景是不是**严格跟着选定的色调走**?有没有**又滑回奶油 / 暖白默认**(选了暗 / 冷 / 高对比色调却还是亮奶油底,就是没执行)?整套是不是**一个背景模式**(没有暗底封面 + 亮底正文那种明暗乱跳)?
   - **叙事**:页序构成清晰弧线、首尾完整(有封面、有总结 / 行动)?**页型有变化、相邻页不重复同一种**?
   - **每页规划**:每份 `plan/slide_NN.md` 有"页型 + 一句话总结"两行,**而且写了最终文案(逐字上屏文字,不是待润色的要点)+ 版式(套骨架 + 哪种 body 布局)**?要用的事实 / 数字已写进规划(subagent 看不到 fact pack 原文)?视觉方案只用 CSS / SVG / ECharts / 已备素材?缺数字处已标"示意"?
   - **页型必含要素**:每页满足其页型的硬性要素吗(案例页有具体案例、介绍类有代表作品、数据页有真实 / 标注数据、方案页有具体做法——见词表后清单)?
   - **素材**:每条 image brief 都有唯一 `asset_id` 并写明**主体 + 用途 + 宽高比 + 主色 / 色调 / 情绪**? `assets/image-manifest.json` 中对应条目的路径都真实存在(没出的图改 CSS / SVG 替代,绝不引用不存在的图)?
   - **事实保真(你是全链路唯一关口)**:抄进规划的每条事实 / 数字,回 `research/research_NN.md` **逐项核对**来源与数值(尤其性别 / 称谓 / 归属 / 单位 / 时间)——下游 slide / review 都看不到 research,你抄错没人能发现。
   - **自包含**:随手抽一份 `slide_NN.md`,只凭它 + `deck.md` + `base.css`，以及必要时的总 Manifest，能不能独立做出这一页?
   - **质量判据**:内容逻辑 / 叙事经得起 `references/quality-checklist.md` 的「单页检查·表达与内容逻辑」「整套检查·全篇结构」吗?规划期堵掉比渲染后返工便宜。

   哪条不达标,就先 `edit` 规划文件 / `base.css`、或重新委派 image subagent,**改好再进入阶段二**。(可选加固:规划复杂、拿不准完整性时,派一个只读 `plan/`+`base.css`(`toolsets:["file"]`)的轻量 subagent 做"规划完整性体检",再进阶段二。)

### 阶段二 —— 并行委派(slide subagents)

规划文件与 `assets/image-manifest.json` 就绪后,调用 subagent 工具把全部页**并行**委派出去(尽量一次委派全部,每个 slide subagent 负责一页——在 goal 里写明它负责**第 NN 页**(没有独立"页号"参数),`label` 设 `slide_NN`,`toolsets:["file","terminal","vision"]`,并让它先 `read skills/ppt-skill/subagents/slide.md`)。Slide 只从总 Manifest 解析位图，不读取 Image 摘要。

每个 slide subagent 各自写 `slides/slide_NN.html`、用 `bash` 跑 `render.py` 渲染、`vision_analyze` **看图自检**(对照 `references/quality-checklist.md`「单页检查」,硬伤清零优先,`edit→渲→看` 最多 3 轮),返回一段含截图路径的简短总结。**写页要求、自检判据、循环纪律、返回契约都在职责卡 `subagents/slide.md` 里——这里不再展开。**

收尾总结必须**如实报告**该页状态(通过 / 还剩哪些硬伤)与最终截图路径 `renders/slide_NN.png`,别谎报通过——编排器与 review subagent 靠这个判断哪页需返工。

### 阶段三 —— 复审(review subagent 诊断 + 编排器决策)

**第 0 步:先补齐失败页(委派 review 前)。** 逐条核对每页 slide subagent 的返回:凡**自报还剩硬伤 / 状态≠通过 / 缺 `renders/slide_NN.png` / 总结异常**的页,立即带 note **重新委派**,直到拿到「通过」自报 + 新截图。别让带病页进复审或收尾——harness 的验收只看子 agent 是否正常收尾、**不看页面质量**,带病页会混进数据集。

slide subagent 全部返回后,**委派 review subagent**(`toolsets:["vision"]`,让它先 `read skills/ppt-skill/subagents/review.md`)横扫整套截图,返回一份**问题清单**(哪页 / 什么问题 / 建议怎么修)。它对照 `references/quality-checklist.md` 逐页过「单页检查」+ 横向过「整套检查」,大体覆盖:配色 / 字体漂移、背景是否全套统一、配色出界、两页雷同、叙事断层、页码错乱、整套风格一致性。

**编排器据清单做决策**(自己不渲染、不看图、绝不动 `slides/` 的 HTML,会被拒),按问题类型走:

- **跨页视觉统一**(配色 / 字体 / 间距等 token 漂移,或状态色没定全 / 定得不对)→ 改 `base.css` 补齐和谐的 token,然后**重新委派**受影响的页(slide subagent 会基于更新后的 `base.css` 重渲并复核);你自己不渲染、不改页;
- **某页内容 / 结构 / 局部问题** → 调用一个 slide subagent 重做那一页,带 note 传入具体修改点(注意:重新委派会让 subagent 按规划**重写**该页 HTML,只在确有内容问题时才用);
- **个别页配色出界** → 若是 `base.css` 的状态色没定全 / 定得不对,走上面第一条改 `base.css`;若是某页乱用调色板外的颜色,带 note 重新委派那一页;
- **同类问题系统性复发**(多页同一个毛病)→ 多半是 `base.css` / 规划层根因,改 `base.css` 或对应规划,而不是逐页修。

重委派的页同样走自检并返回新截图。**收尾纪律:**
- ① 凡**重派过任何页、或改过 `base.css`**,收尾前**必须再委派一次 review**(改 base.css 要整套横扫——全局改动可能修好 A 页却带歪 B 页);
- ② 重派 note 里要求 subagent 重写后自评,**新版若不优于规划目标就报"返工失败"**,别让重试越改越差还顶替掉好版;
- ③ `review → 重派` 至多 **1–2 轮**,仍不达标就**保留当前最好的一版**并在收尾如实说明,不无限返工。

### 阶段四 —— 收尾

以一段简短消息总结这个 deck:页数、生成了哪些文件、复审结论。**并指出 `plan/deck.md` + `plan/slide_NN.md`(逐字大纲)是「可编辑大纲源」**——HTML 成品不可直接编辑,用户要改内容就从这套规划入手(改完重新委派对应页)。
