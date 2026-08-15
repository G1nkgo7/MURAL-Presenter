---
name: sn-ppt-standard
version: 1.4
description: Use when sn-ppt-entry and sn-ppt-story have produced a current outline.md and the deck should now be generated as editable per-page Static HTML, rendered PNG previews, present.html, and a default HTML-derived PPTX compatibility copy. Also use for visual or copy edits to an existing Static HTML deck.
---

# sn-ppt-standard

生成 HTML 幻灯片演示文稿。每一页是一个独立 HTML 文件,画布 **1600×900(16:9)**。

## 目录边界（最高优先级）

先确定并始终区分两个绝对路径：

- `SKILL_ROOT`：当前 `SKILL.md` 所在的 `sn-ppt-standard/` 目录。这里只读，用于读取 `references/`、`subagents/` 和 `scripts/`。
- `DECK_DIR`：Entry 传入的 deck 输出目录。未显式传入时，只能取当前 `outline.md` 的父目录。
- `PPT_TOOLS_DIR`：`SKILL_ROOT` 的同级 `sn-ppt-tools/`。这里只读，提供可选媒体工具。

硬约束：

- `outline.md`、`research/`、`plan/`、`base.css`、`assets/`、`slides/`、`renders/`、`present.html` 和导出的 PPTX 全部写入 `DECK_DIR`。
- 禁止把任何 deck 产物写入 `SKILL_ROOT`、用户 home 根目录、工作区根目录或其他 Skill 目录。
- 所有相对产物路径都以 `DECK_DIR` 为根；所有配套文件路径都以 `SKILL_ROOT` 为根。
- 调用 subagent 时必须同时传入绝对 `DECK_DIR` 和 `SKILL_ROOT`，要求它遵守同一边界。
- 需要图片时先读取 `$PPT_TOOLS_DIR/references/capability-policy.md`；不扫描其他 Skill 或
  仓库寻找替代工具。
- 如果 `DECK_DIR` 不明确或不包含当前 `outline.md`，停止生产，不要自行选择新目录。

## Story 与材料输入契约（最高优先级）

开始生产前，必须读取 `<DECK_DIR>/task_pack.json`、`<DECK_DIR>/info_pack.json` 和
`<DECK_DIR>/outline.md`。没有这三份文件时停止页面生产，返回 Entry 补齐。

若 `info_pack.research_report` 非空，读取它指向的 Research 主报告；若
`task_pack.state.research.required == true` 但报告缺失，停止并返回 Entry。Static HTML
在任何档位都不搜索、不查证、不生成新的 Research 文件。

`outline.md` 是用户可编辑的叙事真相：

- “整体思路”决定目标、受众、核心观点和叙事弧。
- `## 第 N 页` 的顺序决定最终页序与页数。
- 页面标题和“这一页要说明什么”决定该页标题与一句话总结。
- “内容与依据”决定必须保留的事实、数据、案例和来源。
- “页面表达”约束信息关系与视觉重点。

你要把它扩展成蒸馏流程使用的 `plan/deck.md` 和 `plan/slide_NN.md`，但不得重新取得叙事控制权。

可以补充：

- 页面生产所需的逐字文案、来源、素材和版式细节。
- 不改变结论的语言压缩和视觉表达。

不得：

- 调整 outline 已确定的页序或页数。
- 改变页面标题和核心结论的含义。
- 恢复用户从 outline 删除的页面或观点。
- 因为蒸馏流程偏好某种页型而替换 Story 的页面任务。
- 自动追加 outline 中不存在的目录页、过渡页、行动页或结束页。
- 搜索网页、补做 Research 或用新事实静默改写 outline。

如果现有材料不足以完成某页，保留页面位置和用户意图，停止该页生产并明确缺口，交回
Entry/Story。不得在出口内自行补事实，也不得暗中改成另一套叙事。

## 执行强度

从 Entry 取得 Draft、Standard 或 Deep。未明确时按 Standard 执行。本节只控制生产投入、
视觉自检和复审轮次，不改变 Research 边界。

| 档位 | 素材与生产投入 | 单页自检 | 整套复审 |
|---|---|---|---|
| Draft | 不新增事实研究；仍主动搜图 / 生图，只降低自检与返工投入 | 最多 1 轮修正 | 只做一轮硬伤检查；除缺页、空白、溢出等硬伤外不返工 |
| Standard | 不新增事实研究；主动取得与内容匹配的视觉素材并完成页面表达 | 最多 2 轮修正 | 一轮整套复审；最多一轮定向返工 |
| Deep | 充分使用已完成的 Research，主动补齐视觉素材并做更完整的视觉复审 | 最多 3 轮修正 | 完整整套复审；最多两轮定向返工 |

任何档位都不为内部轨迹状态重派任务。只有必要产物缺失、工具调用失败或页面存在明确硬伤时才重试，同一任务最多重试一次；仍失败就保留可用产物并如实报告。

## 设计丰富度

读取 `task_pack.choices.design_richness`，只在 Static 内解释：

- `restrained`：减少装饰，使用更稳定的栅格和较低的页型变化；仍主动选择有内容价值的图片。
- `rich`：默认，主动使用图片、页型变化、数据重点和视觉节奏形成完整表达。
- `high_creative`：允许更强的 hero 页、构图变化和视觉母题，但仍使用可编辑 HTML，
  不牺牲事实、可读性和 outline 遵循。

设计丰富度不改变页面数量、结论和 Research 边界。

## 视觉资产策略（不设图片配额）

图片不是完成文字排版后的可选装饰，而是页面规划时就要决定的内容载体。规划**每一页**时，
必须先确定一个主视觉锚点：真实图片（含真实产品 UI / 界面截图）、生成图片（仅示意 / 艺术重构 /
概念化表达）、数据图表、结构图，或有明确
理由的强排版。这里不规定每套必须有几张图，但默认倾向是**能通过真实视觉对象增强理解和
第一印象时，就按真实性类别主动搜图或生图**，不能因为 CSS 更省事而把整套做成文字、卡片和
几何图形。

主视觉锚点必须先经过一次**全册图片机会扫描**，再逐页落到规划中。全册判断不能被拆成一组
彼此独立的“本页可用结构图 / 强排版”免责理由：人物、真实产品、地点、作品、案例、历史现场、
行业 / 生活场景、真实产品 UI / 界面截图，以及适合用定制插画快速建立理解的概念，都应先形成
图片 brief；只有数据、顺序、层级、关系、示意 UI / 界面结构 / 组件关系或文字本身确实是页面
主体时，才保留代码视觉或强排版。

- 人物、真实产品、地点、作品、案例、历史现场、行业 / 生活场景，以及真实产品 UI / 界面截图：
  只能使用用户材料中的真实素材或搜真图并下载到本地。搜索失败直接进入 `none`，不得生成
  仿真照片、仿真产品图或仿真截图兜底。
- 概念隐喻、主题 hero、无法直接拍摄的抽象场景和统一风格插画：只有规划明确写明“示意 / 艺术
  重构 / 概念化表达”时才可生图；生成结果不得冒充真实案例、现场、产品或界面证据。
- 数据图表使用 ECharts；流程、架构、关系、图标，以及示意 UI / 界面结构 / 组件关系继续使用
  HTML / CSS / SVG，且流程、
  架构和关系结构只有在**连接关系本身就是页面内容**时才使用。SVG 不得用来替代人物、产品、
  场景、作品、案例画面、概念隐喻或插画。
- 除上述代码视觉外，同一内容既能用有效真实图片 / 生成图片表达，也能画成 SVG 时，默认使用
  图片。只有图片能力不可用、结果明确不合格，或用户明确要求完全可编辑的结构图时才选择 SVG。
- 只有图表、结构图或强排版本身明显比照片 / 插画更有解释力时，才采用无图页，并在页规划中
  写明无图理由和替代视觉锚点。
- 图片搜索只为取得 outline 已确定主题的视觉素材，不把图片页面中的新信息当成事实依据，
  因此不违反 Static 禁止新增 Research 的边界。
- 图片默认进入前景内容槽，不改变 `base.css` 的统一背景：可用左右图文、局部大图、裁切图、
  图片带、案例缩略图或局部蒙版。只有封面和 outline 已明确的章节页可以规划满幅图片。
- 图片能力按同类链路使用。真实素材链是**可用且相关的用户已有素材 → native image search →
  bundled image search → none**；生成链是 **native generation → bundled generation → none**，
  且只接受明确的示意 / 艺术重构 / 概念化表达。两条链路不得互相兜底。进入 `none` 时记录规范
  失败状态和原因，改成不依赖图片的页面并继续；单张图片不得阻塞整套。
- Draft / Standard / Deep 的区别是复审和重试投入，不是“是否值得找图”；Draft 也不能默认
  退化成纯文字 deck。

这是**多 Agent 工作流**:你是**编排器**,**专注"任务规划"**——吃透现有材料和
outline、做全局规划与设计系统(`base.css`)、把每一页拆成自包含的页规划。生产环节都
**委派给专门的 subagent**:

- **image subagent**(前置,**可并行多个**)—— 按 brief 集中生成全套配图、逐张核对配色,返回确切路径;图多时按图分组、一次派多个并行;
- **slide subagent**(**并行**,每页一个)—— 写、渲、自纠该页 HTML;
- **review subagent**(收尾)—— 横扫整套截图,返回问题清单。

你基于这些 subagent 的返回**做决策**(规划、回填素材、判断返工);**自己不调研、不出图、不写页、不渲染、不看图。**

> 不同框架里启动 subagent 的工具名不一样(`delegate_task`、`task`、`spawn`、`run_subagent`…)。本文档统一叫它「调用 subagent」——用你当前环境实际提供的那个即可。三个角色(image / slide / review)不是预定义类型，而是同一个 subagent 工具传入不同的 `goal` + `toolsets` 现拼出来的。

## 配套文件(按需 `read_file`,别一股脑全载)

| 文件 | 是什么 | 谁、什么时候读 |
|---|---|---|
| `$SKILL_ROOT/references/design-styles.md` | 风格库**参考**(74 风格 / 22 色调 / 29 主色;非强制,灵感用) | 编排器**风格决策**时参考(阶段一·3) |
| `$SKILL_ROOT/references/base-template.css` | `base.css` 起手模板:token 占位 + **固定版式骨架**(画布 / 安全边距 / 标题区 / 页脚 / 页码 / 字号阶梯含最小字号 / 系列色 / 满铺类) | 编排器写 `base.css` 时**复制**(阶段一·4) |
| `$SKILL_ROOT/references/layout-patterns.md` | 难版式范例(时间轴 / KPI 行 / 对比 / 流程 / 左文右图 / 引文 / 满图;只引 token、嵌 `.slide-body`) | 编排器规划版式、slide 排正文时参照 |
| `$SKILL_ROOT/references/quality-checklist.md` | 质量判据(**单页 + 整套**),deck 质量单一真相 | 编排器规划自检;slide / review subagent 自检与复核 |
| `$SKILL_ROOT/subagents/image.md` | image subagent 职责卡 | image subagent 开工前 |
| `$SKILL_ROOT/subagents/slide.md` | slide subagent 职责卡(写页要求 / 自检判据 / 循环纪律) | slide subagent 开工前 |
| `$SKILL_ROOT/subagents/review.md` | review subagent 职责卡 | review subagent 开工前 |
| `$SKILL_ROOT/scripts/render.py` | HTML→PNG 渲染脚本(自包含,不依赖宿主 render 工具) | slide subagent 用 `terminal` 跑 |
| `$SKILL_ROOT/scripts/build_player.py` | 把 `slides/*.html` 串成可播放的 `present.html`(iframe 隔离 + 交叉淡入翻页 + 全屏 + 等比缩放) | 阶段四收尾**必做**(player subagent 用 `terminal` 跑) |
| `$PPT_TOOLS_DIR/references/capability-policy.md` | 原生媒体工具优先、PPT 内置工具回退和无图路径 | 编排器与 image subagent 需要配图时 |

**委派任一 subagent 时,在 goal 里写明当前 Draft / Standard / Deep、设计丰富度、绝对
`DECK_DIR`、绝对 `SKILL_ROOT` 和绝对 `PPT_TOOLS_DIR`，并让它先读取
`$SKILL_ROOT/subagents/<role>.md`**。所有读写产物都显式放在 `DECK_DIR` 下。label 使用
image_NN、slide_NN、review 和 player。

## 边界(先读这个)

- **你只规划与委派,不做生产。** 出图、写页 HTML、渲染、看截图交给 subagent；你的工作是吃透现有材料和 outline、规划、写 `base.css`、写页规划，再根据返回做决策。
- **整个 Static 出口禁止 Research。** 编排器和所有 subagent 都不得搜索事实、数据、引述或案例。图片搜索只用于取得 outline 已经要求的真实视觉素材，不得把搜索结果当作新增事实来源。
- **自主推进,不提问。** 除非硬性阻塞(权限 / 登录 / 缺输入文件),否则不向用户提问、不等确认;自行补齐合理假设,做有品味的决定,继续执行。
- **失败按产物处理。** subagent 超时、截断或异常时，先检查目标文件是否实际存在且可用；缺少必要产物才重试一次。不要为了追求特定轨迹状态反复重派。
- **页面 HTML 由 slide subagent 写,你绝不自己写或改。** 你只写规划文件(`plan/`)和 `base.css`;`slides/slide_NN.html` 一律经 subagent 产出。**即使是跨页统一的小改动也不例外**:跨页**视觉**统一(配色 / 字体 / `.slide-title`·`.kicker` 等骨架样式)改 `base.css`,跨页**内容**修正带 note 重新委派受影响的页。你对 `slides/` 的 `write_file`/`patch` 会被直接拒绝。
- **图表是代码,不是图片。** 数据图表一律用 ECharts 在 HTML 里手写;image subagent 的
  `image_generate` 只用于规划明确的示意 / 艺术重构 / 概念化插画，绝不用它伪造图表、真实
  照片、真实产品或真实界面截图。

## 角色与工具

**编排器(你)用——只规划 + 委派 + 决策:**

- `read_file` —— 读 task/info pack、outline、已有 Research 主报告、配套文件和规划。
- `write_file` / `patch` —— 写 `plan/` 规划文件和 `base.css`(**写 `slides/` 会被直接拒**)。
- **subagent 工具** —— 委派下面三类 subagent;每类传对 `toolsets`(见表)+ 在 goal 里让它先读职责卡。

> 编排器**不渲染、不执行命令、不看图**(没有 `terminal` / `vision_analyze`),所以只能靠 subagent 的**文字返回**来"看见"页面的**视觉效果**——要刷新某页视觉,改 `base.css` 后**重新委派**那一页,绝不自己写/改页。但编排器**可以 `read_file` 读 `slides/` 的 HTML 文本**做完整性 / 内容核对(见阶段三·第 0 步):它对 `slides/` 是**只读**,受限的是「不写 slides」「看不到渲染图」,不是「碰不到文件」。

**三类 subagent(类型由 `toolsets` 现拼;执行细节看各自职责卡):**

| 角色 | `toolsets` | 实得工具(含自动并入的 `read_file`) | 干什么 | 返回给编排器 |
|---|---|---|---|---|
| image | 至少 `["file","terminal"]`；只追加当前 Agent 实际存在的 `web` / `image_gen` / `vision` | read / terminal + 可用原生媒体工具 | 按真实性类别配图；真实素材先用相关用户素材、再走 native/bundled 搜索，明确示意素材走 native/bundled 生成，都失败则返回 `unavailable` | 本地路径 + `ready` 类别 + `image_source`，或规范的 `unavailable` 原因 |
| slide | `["file","terminal","vision"]` | read / write / edit / bash / vision_analyze | 写 `slides/slide_NN.html`、渲染、看图自纠 | 简短总结(状态 + 截图路径) |
| review | `["vision"]` | read / vision_analyze | 横扫整套截图,诊断全局问题(**只诊断不改**) | 问题清单(哪页 / 什么问题 / 建议) |

> **怎么渲染**(slide subagent 用 `terminal` 跑 skill 自带脚本):把某页 HTML 渲成 PNG——
> ```
> python "$SKILL_ROOT/scripts/render.py" "$DECK_DIR/slides/slide_NN.html" "$DECK_DIR/renders/slide_NN.png"
> ```
> 脚本自处理依赖(无头 Chromium、LD_LIBRARY_PATH、等字体就绪防豆腐块),成功后把 PNG 路径打到 stdout。**渲完用 `vision_analyze` 看那张 PNG**(`read_file` 看不了图)——这是 subagent"看见"页面的唯一方式。

## 工作区布局

```
research/             # Entry 在 Story 前完成的只读 Research 产物（如有）
outline.md            # Story 生成、用户可直接修改的叙事真相
plan/
  deck.md            # 全局规划
  slide_01.md        # 每页一份规划
  slide_02.md ...
base.css             # 设计系统:所有颜色 / 字体 / 间距 token
assets/              # 配图:原生/内置工具产物或已有素材
slides/
  slide_01.html      # 每页一个 HTML 文件(由 slide subagent 写)
renders/
  slide_01.png       # 渲染截图,文件名对齐页号
  slide_02.png ...
present.html         # 可播放文件:全套页串成浏览器放映入口(阶段四 build_player.py 生成,必做)
<DECK_ID>.pptx       # Static HTML 的默认兼容交付版；用户明确只要 HTML 时跳过
```

上面所有产物路径都相对于 `DECK_DIR`。Skill 配套文件只从 `SKILL_ROOT` 读取。

**渲染产物统一放 `$DECK_DIR/renders/`**,文件名与页号对齐(`slide_NN.png`),每次重渲覆盖同名文件——不要把截图散落在 `slides/`、工作区根目录或 Skill 目录。`render.py` 的输出路径就指向这里,`vision_analyze` 用它。

## 设计规范

- **整体服从选定的风格。** 全套一致地执行你在规划里定的 风格 + 色调 + 主色(`$SKILL_ROOT/references/design-styles.md` 可作参考,非必须)。无论哪种风格,通用底线都成立:层级清晰、每页只讲一件事、留白与重心有节奏、调色板收敛(用主色 + 少量辅助,别失控);用色强弱跟着风格走(简约就克制,波普 / 国潮就大胆,但仍守住选定的调色板)。
- **`base.css` = 设计系统 + 版式骨架,slide 只用 token、套骨架。** 复制 `$SKILL_ROOT/references/base-template.css` 起手:① **token** —— `--bg` / `--accent`(= 选定主色 hex)/ `--ink` / 辅助色 / 状态色 / 字体 / **字号阶梯(含最小字号硬下限 `--fs-min`)** / 间距;slide HTML 只引用 token(`var(--ink)` …),**绝不写裸 hex**。② **版式骨架** —— 固定的 `.slide` 画布(1600×900 + 安全边距 + 栅格)、`.slide-title` / `.slide-body` / `.slide-footer` / 页码 的**固定位置与体例**;每页都套这套骨架,**框架类不改位置 / 字号 / 配色**,只在 `.slide-body` 内按页型变布局。**这是治"标题 / 装饰线 / 页码每页乱动、对齐错位、重心偏移、出血"的关键:框架全套一致,只有正文区按页型变化。** 章节名 / 过渡页小标签用骨架自带的 `.slide-title .kicker`,别另造结构;封面 / 章节满图页用 `.slide--bleed` 满铺(见 `$SKILL_ROOT/references/layout-patterns.md`)——这种**合法满铺不算出血**。
- **⚠️ 奶油 / 米黄 / 暖白底是被严重滥用的"安全牌",默认就别用它。** 数据里近一半 deck 都偷懒用了暖奶油底 —— 这是头号雷同。**动笔前先问:这个主题最该是什么底?** 然后让背景跟着**风格 + 色调**走:
  - **商务 / 科技 / 数据 / 金融 / 政务 / 奢侈 / 暗黑极简 / 赛博** → 优先**深底**(深蓝黑 / 近黑 / 深墨绿),利落有力;
  - **医疗 / 科研 / 法律 / 冷色调** → 冷白蓝 / 冷灰;
  - **高对比 / 包豪斯 / 杂志** → 纯白或纯黑;
  - **奶油 / 暖白只留给真正偏暖的**:生活 / 文教 / 手作 / 婚庆 / 母婴 / 餐饮等;别的主题用到暖白要有明确理由。
  目标是让背景在「暗 / 冷 / 高对比 / 暖白」之间**真正铺开**,而不是清一色奶油。
- **全套只用一个背景模式。** 要么亮底、要么暗底:封面 / 过渡页可以用**同一色系的更深 / 更浓变体**做戏剧感,但**绝不在亮底和暗底之间来回跳**——暗底封面配亮底正文那种"两个 deck"的割裂感,就是这么来的。
- **⚠️ 声明一句"色彩故事",拒绝默认科技色 + 强调色要"配给"。** 这是把 deck 从"能用"推到"高级"的关键一步。
  - **写一句贴主题的色彩故事**(如「档案纸暖白 + 墨黑 + 一味赭石」「深酒红 + 暖骨白 + 黄铜」「深松石绿 + 沙色」),`base.css` 照它定 token,记进 `plan/deck.md`。
  - **除了奶油底,第二大雷同是"深藏青 + 霓虹青 / 电光蓝"这套"AI 默认科技色"——默认禁用。** 暗底要选**带明确色温的深色**(暖炭 / 墨绿蓝 / 深普蓝 / 深褐 / 深墨绿),别一律深藏青;`--accent` 要**降饱和**(饱和度 ≤ ~60%,**避开 180–195° 那段霓虹青/电光蓝做主强调色**),不要 `#6366f1` 靛蓝、不要满屏电光青配纯黑。
  - **⛔ "深色 + 琥珀金 / 暖金"= 第二个 AI 默认色,做数据 / 商务 deck 时和 navy+霓虹青一样**默认禁用**。** 数据 / 报告题最容易无脑套"暗底配金"——**别**。色彩故事**从主题本身派生**,给几个非金方向参考:暖纸白 + 墨 + 一味绛红 / 冷石板灰 + 暖白 + 一味钢蓝 / 深松绿 + 沙 / 深酒红 + 骨白……主色**不是金**。(真要暖金,得是主题本身就金属/奢侈/能源,且全套只此一味。)
  - **⚠️ accent 配给是"数页"硬规,不是口号**:**任一页被 accent 染色的元素 ≤ 2 个**(图表里那一根高亮柱 / 那一个头条数字算 1 个;标题装饰线、眉签、每个 KPI 数字、每根柱都染 accent = 严重超配)。规划自检时**对着每页数一遍 accent 元素**,>2 就砍到只留最重要的 1–2 处,其余降回中性墨色。
  - **数据页强调色"只点一处"(治金色到处刷)**:图表系列走**中性灰 / 同色系浓淡**,**只把那一根关键柱 / 那个头条数字染 `--accent`**;页标题、坐标轴、其余数字、分隔线**一律中性墨色**。一页里被 accent 染的元素**超过 2 处**就是刷过头,留最重要那一处、其余降回中性。
  - **强调色配给制**:`--accent` 在**任一页最多占约 10% 的着墨**,且**一页只担一个强调角色**(要么那个关键数字、要么图里被点出的那一组——别同时把标题线、眉签、正文要点、数字全染成 accent)。正文 / 眉签 / 分隔线 / 标签一律用**中性墨色的浓淡**(暗底=白色不同透明度 90/70/50%,亮底=墨色不同透明度),accent 只留给真正要喊的那一处。**到处强调 = 没有强调。**
  - **面板别做成"扁平深色盒子"**:卡片 / 面板靠**色调分层**出体积——页底→面板→面板内元素三级,每级与父级差 ≥4% 明度;暗底面板 = 4–6% 白填充 + 8–12% 白描边(可加 1px 顶部内高光假装受光),亮底面板 = 1–2% 墨填充 + 6–10% 墨描边;**不要硬投影、不要在标题后摆一团泛用的径向"光晕"假装景深**(那是 AI 味填充),要氛围就用 ≤8% 明度行程的定向线性渐变。
- **调色板封闭:任何颜色都必须来自调色板,包括状态色。** `base.css` 要把**这套 deck 会用到的所有颜色**一次定全——含**涨跌 / 正负 / 警示 / 提示**这类状态色(如 `--up`/`--down`/`--warn`/`--note`),且这些状态色要**和选定主色调和**(从主色 / 辅助色派生的深浅、或同色系的暖冷偏移),而不是直觉里的大红大绿大粉。**严禁**为了"负数 / 风险 / 提示"就硬塞一个调色板外的红 / 粉 / 绿(例:导航金 deck 里冒出 `#FBEFEF` 粉底免责条、或鲜红的 `-12.5%`,就是出界)。表达涨跌优先用:箭头 / 正负号 / 位置 + 调色板内的深浅或单一强调色;免责 / 提示条用主色的淡色调(如金色 deck 用米金淡底),不要粉不要红。**状态色硬规:`--up` ≠ `--warn`(涨色不能等于警示色);涨跌一律"颜色深浅 + 箭头 / 正负号"双通道,别只靠色相分辨。**
- **图表是代码。** 数据图表用 **ECharts** 在 HTML 里手写,**整套配色(系列色、坐标轴、网格线、文字、标注)全部取自 base.css 的 token**——多系列从 `--series-1…6` 取(JS 里 `getComputedStyle` 读**直接色值**;别用 `var()` / `color-mix()` 喂 ECharts,它解析不了),系列不够就基于主色派生深浅;**绝不用 ECharts 默认调色板,也不混入调色板外的颜色**;容器给显式宽高;不要用图片伪装图表。**ECharts 统一从固定 CDN 引入**:`<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>`(**固定这个源 + 版本,别换别的 CDN / 版本、也别引本地 `vendor/`**;渲染环境联网,这是最干净的方式)。图表没加载出来(破图)按硬伤处理——slide 自检会抓到并重渲。
- **图表即新闻图(让图表从"学生作业"升到"FT / 经济学人"级):**
  - **页标题写结论,不写题目**:用「GenAI 融资 2024 近乎翻倍」这种**发现句**,而不是「AI 融资规模 2023→2024」这种题目。
  - **直接在数据上标值,别让人来回找**:柱端 / 线尾直接标数 + 类目名;**柱子 ≤6 根且已标值,就砍掉数值轴和网格线**(冗余的墨);线图把**系列名标在线的末端**,别堆顶部图例;一页最多一个图例,且仅在没法直接标注时才用。
  - **一图一个"so what"标注,且标注不碰撞**:点到关键那根柱 / 那个拐点;**callout / 标签别压在数据线 / 柱上、别互相重叠挤一处**(折线峰值附近最爱堆好几个标签撞成一团)。**一张图只留一个主标注**,其余次要信息压到轴外 / 图例 / 正文,别全堆图上(整套图表共用一套注释体例)。
  - **数字体例统一**:同一指标族用**同一单位 / 精度 / 范围写法**(别一页里 亿/万亿/B/T/M 混用),范围用统一连接号(`2,790–6,382 亿`),估算 / 预测用同一个标记;一行里的数字**小数对齐**。
  - **KPI 大数字要有层级**:一排数字里**只让一个**当头条(最大 + accent),其余次级(更小 / 中性色);每个数字给"值 + 一句上下文 + 同比/基准(如 ▲+80%)",来源压一行放页脚、别挤进数字旁。
  - **⛔ 一页最多一个图表;绝不一页两个图表。** "一个点睛大数字 + 一个主图表"同页**是好数据页**(FT 常这么干),允许;**禁的是**:一页**两个图表**、或"图表 + 多组密集数字 + 长段文字"三样全堆。三样全堆就**拆成两页**(页数不够就合并别的轻页腾位)。一页围绕**一个发现**展开。
  - **⚠️ 数据页版式别都一个样**(治"大数字左 + 图表右"连用多页):轮换不同数据页型——纯图表满版页 / 纯大数字 hero 页 / 对比表 / 左图右文 / 时间轴 / 排序条……同一种"大数字+图"骨架**全套别超过 2 页**。
  - **示意值与真实值分清**:同一张图里**别把"示意/插值"数据和有源真实值混在一起又不区分**;要么这张图**整体标"示意"**(只用来表达趋势/结构)、要么**只画有源的真值**。别让看的人把编的数当成真数据。
  - **图配数据**:近似的几个份额别用饼/环(看不出差),用排序条;`示意`数据别画成带精确小数的饼图。
- **主动配图 + 精确适配。** 每页先按“视觉资产策略”选择主视觉锚点；只要主题存在具体视觉
  对象或适合插画化的概念，就主动写 image brief 并交给 image subagent。每张图仍必须说得出
  **用途 + 对应页内哪段内容**，不堆重复、无关或纯装饰素材。封面和满图章节过渡页默认使用
  主题相关的 hero 大图（真实对象只用真实素材；只有明确的示意 / 艺术重构 / 概念化表达才
  生图）；只有纯数据、强排版或明确极简概念确实更合适时才无图，并在规划中说明原因。装饰、
  图标、底纹和几何结构继续用 CSS / SVG。
  **不要用 SVG 重画本可由照片或生成插画承担的语义内容**；SVG 只表达连接、顺序、层级、
  数据结构及简单装饰，不承担“画一个人物 / 产品 / 场景 / 概念画面”的任务。
  - **适配(治裁切 + 不匹配)**:编排器写 brief 时指定**画面主体 + 宽高比(匹配版式槽位)** + deck 主色 / 色调 / 情绪(如 "navy and muted gold, editorial, low-saturation"),让出图就贴着要用的比例,避免上屏被大面积裁切。
  - **统一**:全套用**一个视觉配方**(同媒介 + 同调色 / 处理,如统一低饱和 / duotone),别风格色调乱飞;配色必须进调色板。
  - 生成 / 核对细节见 `$SKILL_ROOT/subagents/image.md`;原生工具可能自动命名，内置工具
    必须显式写入 `$DECK_DIR/assets/`。编排器只把该目录下真实存在的确切路径回填进页规划。
    **别让一张五颜六色的图破坏整套**。
- **文字精炼** —— 用短语不要整段;1600×900 装不下密集长文,会溢出。
- **中文字体必须显式命名。** 渲染环境**已本地安装 Noto Sans SC / Noto Serif SC**,所以中文 deck 的正文 / 标题 `font-family` 必须显式带上 `"Noto Sans SC"`(或 `"Noto Serif SC"`),如 `font-family: "Noto Sans SC", sans-serif;`——否则可能落到无中文字形的字体上渲染成豆腐块(□□□)。`base.css` 里再加一条 Google Fonts 的 CDN `@import` 作可移植兜底(本地已有字体,不联网也能渲染,所以别把 CDN 当唯一依赖)。
- **避免 AI 味(AI-slop),要有"美术指导"**:不用 Inter / Roboto / 系统默认显示字体,不用千篇一律的靛蓝 `#6366f1` / 紫配白 / 深藏青配霓虹青,不要什么都居中,不要无意义毛玻璃。**重点治"每页都是『眉签→大标题→一排等权卡片』的模板病"——这是头号 AI 味:**
  - **卡片宫格限量**:整套**最多 2 页**用"等权卡片行 / 宫格"版式,**2×2 田字格全套最多 1 次**;已经用过两页宫格,后面的页一律换**别的版式族**(满铺左右分栏 / 带分隔线的清单 / 单焦点大图 / 时间轴 / 图上叠字 / 大数字)。**相邻两页不复用同一种版式。**
  - **每套必有 1 个"破格 hero 页"**:在叙事高点(关键数字 / 核心论点)做一页**刻意打破全套栅格**——一个特大数字 / 词占满画布、或一句满幅金句独占、或一张占画面 >70% 的主图 / 主图表;它要**和其它页明显不同**(不是"内容更少"而已)。
  - **写一句"设计概念",让形式服从它**:动笔前给这套 deck 写一句设计概念并写进 `plan/deck.md`;讲"简约"的主题自己就要简约(更多留白 / 更少元素),别用塞满的宫格去讲简约(**形式别和内容打架**)。封面引入的视觉母题(motif:某根线 / 某个形 / 某种边缘处理)要在**≥3 个内页**以变体复现,别只在封面用一次就丢;一个背景装饰若删掉也不丢东西,它就不是美术指导,删。**⛔ 尤其"节点星座图 / 粒子网络 / 发光连线"这类科技感背景——除非主题本身就是网络 / 图谱 / 关系,否则一律禁用**(它是最泛滥的"AI deck"壁纸,等于直接盖个"AI 生成"戳;科技 / AI / 数据题尤其爱踩,换成贴主题的真母题或干脆干净留白)。

## 流程

### 阶段一 —— 规划(编排器的主战场)

进入阶段一时把 `task_pack.state.current_stage` 写为 `output.static_html.plan`，状态写为
`generating`。阶段完成后把 `plan/deck.md`、`base.css` 和页规划路径写入 artifacts。

当 `task_pack.choices.static_postprocess` 包含 `pptx`（Static 默认值）时，规划前读取
`$SKILL_ROOT/references/html_constraints.md`，把其中确实影响转换的机械边界交给每个
slide subagent。它们只用于避免硬转换错误，不改变 outline、设计丰富度或页面
Story，也不得为追求 PPTX 一致而主动降低 HTML 效果。`present.html` 始终是视觉效果基准。

**顺序很重要：读取 task/info pack、当前 outline 和已有 Research → 全册图片机会扫描 →
风格决策 + 设计系统 → 写逐页规划 → 委派 image、回填路径。** 这里不新增 Research，也不重新
决定叙事。

1. **读取并锁定输入**：从 task/info pack、当前 `outline.md`、`raw_documents.json` 和已有
   Research 主报告中取得主题、受众、目的、语言、页数、页序、标题、核心结论、内容依据
   和页面表达。后续所有规划逐页映射这份 outline，不再自行定义另一套页序。发现事实缺口
   时停止相关页面并返回 Entry/Story，不搜索补齐。
2. **全册图片机会扫描（先于逐页规划，不另建产物）**：逐页先判断主视觉媒介是**真实图片
   （含真实产品 UI / 界面截图）、生成图片（示意 / 艺术重构 / 概念化表达）、图表、结构图或
   强排版**。封面和 outline 已有的章节转折默认标记为**图片机会**，但不预设真实图片；先看
   画面主体，再分流：真实对象 / 场景建立真实图片 brief，主题 hero / 概念隐喻 / 风格化表达在
   明确标为“示意 / 艺术重构 / 概念化表达”后建立生成图片 brief。人物、真实产品、地点、作品、
   案例、历史现场、行业 / 生活场景和真实产品 UI / 界面截图页建立真实图片 brief。图表、流程、
   架构、关系、图标和示意 UI / 界面结构 / 组件关系继续由 ECharts /
   HTML / CSS / SVG 表达。只有后者本身是页面内容时才判为无图机会；不得把
   “更可编辑”“实现更快”“CSS 更稳定”当作不配图理由。扫描结论在第 6 步逐页写入
   `visual_anchor:`，不改变 outline 的页数、页序或结论。
3. **风格决策**:给这套 deck 定一个**明确、连贯、贴合主题受众**的视觉方向——三件事都要**定死**:
   - **设计风格**(整体气质 / 版式语言,如「暗黑极简」「编辑杂志」「党政庄重」);
   - **色调**(明暗 + 冷暖 + 饱和,**直接决定背景**——按主题选暗 / 冷 / 高对比等,别滑回奶油 / 暖白默认);
   - **主色**(核心强调色,**给确切 hex**,如 `#002FA7`);`base.css` 的 `--accent` 就用它。

   `$SKILL_ROOT/references/design-styles.md`(74 风格 / 22 色调 / 29 主色)是参考素材 / 灵感库,不是必选项。重点不在“从库里挑”,而在定死且贯彻全套。
4. **写 `$DECK_DIR/base.css`(设计系统 + 版式骨架)**:把 `$SKILL_ROOT/references/base-template.css` 复制为 `$DECK_DIR/base.css`,只填 token。结构类保持不动，保证全套框架一致。
5. **写 `$DECK_DIR/plan/deck.md`**:先原样映射 `$DECK_DIR/outline.md` 的整体思路、总页数与页序，再补充主题 / 受众 / 目的 / 语言和**风格方向**(风格气质 + 色调 + 主色确切 hex);一句话情绪、设计规格摘要(`--bg` / `--accent` hex / `--ink` / 字体);**页脚体例 / 页码**;**页序**逐页写成 `slide_NN:页型 — 一句话总结`。页型由你为生产选择，但页面位置和一句话总结必须服从 outline。
   - **长 deck 的分幕只解释、不增页**:如果 outline 已有章节或过渡页，在 `deck.md` 中明确分幕；如果没有，不得自行插入过渡页改变页数。
   - **叙事是"搭建"不是"清单"**(尤其数据/报告 deck):第一张内容页立一个**统领全篇的论点**,之后每页都要**可见地回扣它**(用页的眉签或一句 takeaway 说清"这条事实如何推进/复杂化了那个论点"),别每页只换个指标重说一遍"AI 很大"。两页讲同一个宏观点就**合并**,腾出的位子放过渡页或真正的新点。
   - **密度有节奏**:**任一内容页最多 3 个独立信息点**(一个数字+它的同比+一句解读=1 个点;双栏两栏都算);**别把两张满密页或两张极简 hero 页背靠背**,让密→喘→密交替。
   - **视觉方向确认**：Draft 跳过。Standard / Deep 用一句话展示风格、明暗冷暖、主色、
     字体气质和设计丰富度。若用户已经在大纲确认时批准同一视觉方向，或 query 已给出
     明确完整的视觉要求，则直接继续；否则在这里等待一次确认。用户修改时只更新
     `plan/deck.md` 和 `base.css`，不重写 Story。
6. **从 outline 逐页写 `$DECK_DIR/plan/slide_NN.md`**(slide subagent 只照它执行,所以要"写死"不留发挥空间)。一份 outline 页面只能映射一份同序号规划，不能合并、拆分或重排。开头必须两行:**页型 / role**(从词表选一个;**deck 语言为英文时用词表「英文 role」列的英文名**)+ **一句话总结**(忠实采用 outline 的该页结论);然后:
   - **最终文案(逐字)**:标题、正文、每个标签 / 数据项的**确切上屏文字**——slide subagent **逐字照用、不改写不扩写不编造**,所以这里就写成最终该显示的样子(短语化、别留"待润色"或占位)。**每条事实 / 数字标已有来源**；靠推断或材料标“待核”的内容随文标“示意”，让 slide 分得清真实事实与占位。
   - **版式**:从 `$SKILL_ROOT/references/layout-patterns.md` 点名一种 body 版式写进来，相邻两页不得用同一种；用 `$DECK_DIR/base.css` 骨架 + helper 排，只排 `.slide-body`。
   - **必含要素**:按页型补硬性要素(见词表后的清单),缺了这页不算完成。
   - **视觉锚点（每页必填且可审计）**：每份页规划都写 `visual_anchor:`，值明确为真实图片
     （含真实产品 UI / 界面截图）、生成图片（示意 / 艺术重构 / 概念化表达）、图表、结构图或
     强排版之一。真实对象不得把生成图片列为后备；示意 UI / 界面结构 / 组件关系归结构图 / 代码
     视觉。选择无图片时必须写明为什么图片不适合，不能只写“无图”，也不能以“更可编辑 /
     更容易实现”为由改画 SVG。判定有图片机会时同时写 `image:` brief：**素材真实性类别 +
     画面主体 + 用途（配合哪段内容）+ 宽高比（匹配版式槽位）+ 主色 / 色调 / 情绪**，并写
     `image_status: planned`。同时写一行精简 `source_hint:`：真实图片机会先检查 `info_pack.json`
     中的用户上传图片、文档继承图片，以及 `DECK_DIR` 内已有本地素材；有相关候选时只写稳定
     定位符：直接上传图片写 `source_hint: info_pack.user_assets.reference_images[0]`，文档继承图片
     写 `source_hint: raw_documents.documents[0].inherited_images[2]`（数字分别是 `doc_index` /
     `image_index`），已有 deck 素材写 `source_hint: deck_asset:assets/<filename>`。不得把用户素材
     原始绝对路径写入规划。没有相关候选时写
     `source_hint: search — no relevant user material`，证明已检查后再走 native / bundled 搜索；
     生成图片机会写 `source_hint: generate`。此时不伪造 `image_source:`；取得合格素材后才由
     第 7 步回填最终来源。
     生成类 brief 必须显式包含“示意 / 艺术重构 / 概念化表达”；如果
     画面可能被误认为真实案例或产品证据，最终文案也必须包含相应示意标识。确切路径等第 7 步
     回填。使用图表时写 `chart:`（ECharts 类型 +
     实际数据）。需要数字而 brief / fact pack 都没给时，改成概念图 / 流程图 / 对比结构，或
     明确标注“示意”。
7. **委派 image subagent(前置)**:把所有页规划里的 image brief 汇成一份清单委派出去。
   toolsets 至少给 `["file","terminal"]`，只追加当前 Agent 确实存在的 `web`、
   `image_gen` 和 `vision`；不得因为某个原生 toolset 不存在而让委派失败。让它先读
   `$SKILL_ROOT/subagents/image.md` 和
   `$PPT_TOOLS_DIR/references/capability-policy.md`，同时传入绝对 `DECK_DIR`、
   `SKILL_ROOT` 和 `PPT_TOOLS_DIR`，要求最终图片全部位于 `$DECK_DIR/assets/`。brief 已写
   在各页规划的 `image:` / `source_hint:` 行里，goal 只写当前档位、设计丰富度、规划文件绝对
   路径、统一视觉配方和失败上限。image subagent 必须读取提示后再执行：`user_material` 候选先
   核对并归档，不合格再搜索；`search` 直接走 native / bundled 搜索；`generate` 只走生成链。
   图片搜索只用于取得已经确定的视觉对象，不用于补充事实。各
   subagent 返回确切绝对路径及对应页。图片确认可用后，编排器必须把真实路径回填到对应
   `image:` 行，并把状态改为 `image_status: ready — authentic user_material`、
   `image_status: ready — authentic search` 或 `image_status: ready — generated illustrative`；
   保留原 `source_hint:`，同时回填一行精简的 `image_source:`：用户素材写同一个稳定定位符；搜图
   写去除凭据、鉴权和签名 query 的公开来源页面 URL（必要时注明它是下载 referrer），不得只写
   图片 CDN URL；无法安全保留完整 URL 时只写 `来源域名 + redacted`。生成图写 `generated — native
   <tool>` 或 `generated — bundled <tool>`，不记录 token、密钥或其他 secret。`image:` 本地路径
   是 slide 的**强制消费项**，`image_source:` 只用于审计，不得当作远程图片引用。若同类 native
   与 bundled 能力均不可用、搜索 / 生成均失败或没有合格
   结果，则删除不存在的 `image:` 路径，保留 `visual_anchor:`，并写
   `image_status: unavailable — <native/bundled 的真实失败原因>`，再改成不依赖图片的结构化
   版式。真实素材搜索失败不得转生图；`image_status: planned` 不得进入页面生产。
   > **⛔ 上下文护栏(踩过坑·必守):搜真图后绝不另开一个"汇总选图 / 映射"子代理去 `vision_analyze` 全部候选。** 真图一多(十几张候选),让单个子代理把所有候选逐张 `vision_analyze` 看一遍来产出"哪页配哪张"的映射表,会**撑爆上下文(ContextWindowExceeded 400)→ 该子代理 api_failed → 整条 deck 报废**。正确做法:**选图↔页 的映射在各 image 子代理自己的逐槽位流程内就地产出**——每个图位:搜候选→靠 `web_search` 返回的标题/来源/缩略信息**粗筛出最像的 1 张**→`fetch_image` 落地→`vision_analyze` **只看选定这一张**→合用即定、不合用再换下一候选;返回路径清单时**每张顺带写明配哪一页**,编排器直接拿来回填,**绝不事后再开一个 agent 统一看所有候选**。配套硬上限:**单个 image 子代理负责的图位 ≤4–5、累计 `vision_analyze` ≲6–8 次、且只 vision 最终选定的那张(不把每个候选都 vision)**;超额就按上面"拆多个并行委派"分组,别堆进一个 context。
   >
   > 只有完成第 2 步全册图片机会扫描，且逐页 `visual_anchor:` 都已说明为什么图表、结构图或
   > 强排版更能承载内容时，才可整套跳过 image subagent；“没有硬性图片数量”不能被解释成
   > “默认不找图”，也不能把全册判断拆成逐页免责。

**页型词表(规划时每页必须从中选一个):**

> **语言一致:页型(role)跟着 deck 语言走。** deck 用中文就写中文页型名;**deck 用英文(brief / 目标输出语言为英文)时,页型 role 也用英文**——用下表「英文 role」列的名字,别中英混填。这条同时管 `plan/deck.md` 的页序行(`slide_NN: <role> — 一句话总结`)和每页 `plan/slide_NN.md` 开头的「页型 / role」行;其余语言比照(deck 是哪种语言,role 就写哪种语言的对应名)。
>
> **页型名是「类型标识」,跨语言等价。** 下文「必含要素 / 硬约束」以及各职责卡 / `quality-checklist.md` 里出现的中文页型名(结束页 / 案例页 / 数据页 / 方案页 …)都指**页型**,英文 deck 里对应其「英文 role」(结束页=Closing、案例页=Case Study、数据页=Data、方案页=Solution …)。下游 slide / review 在 plan 里看到的是英文 role 时,**按页型语义匹配这些约束**——别因为 plan 里没有字面「结束页」三个字就判定缺页或跳过检查。

| 页型 | 英文 role | 页面任务 |
|---|---|---|
| 封面页 | Cover | 呈现主题、演讲者、场合与日期,确立整体基调并建立第一印象 |
| 过渡页 | Section Divider | 标示新章节或叙事阶段的开始,承上启下,提示听众当前所处位置(版式从简,仅含章节名/主题) |
| 开场页 / 引入页 / 背景页 | Opening / Context | 借由问题、事实、数据、场景或趋势引入主题,阐明其重要性与听讲理由 |
| 目录页 | Agenda | 列示内容结构与各部分顺序,建立整体框架与听讲预期 |
| 问题页 / 痛点页 | Problem | 界定当前的矛盾、困难、机会或挑战,为后续观点与方案确立必要性(仅陈述问题,不涉及解决方案) |
| 观点页 / 结论页 | Point / Argument | 直接提出核心判断、主张或结论,以一句话明确表达立场 |
| 逻辑分析页 | Analysis | 拆解原因、结构、机制或层级关系(流程图、因果、矩阵、架构、逻辑树等),阐明"为何如此"或"如何运作" |
| 数据页 | Data | 以图表与数据佐证结论、支撑判断(ECharts),重在论证而非罗列 |
| 案例页 | Case Study | 借助具体案例(成功、失败、竞品或用户故事)增强理解与说服力,并提炼其说明价值 |
| 方案页 | Solution | 阐明具体做法——策略、方法、路径、功能或行动,并回应前述问题与目标 |
| 计划页 / 时间轴页 | Plan / Timeline | 呈现执行节奏:阶段划分、里程碑与时间节点,明确各阶段的产出 |
| 对比页 | Comparison | 在统一维度下进行比较(方案、前后、自身与竞品),提炼差异并给出判断或建议 |
| 总结页 | Summary | 将核心观点凝练为数句,突出重点,形成明确的认知落点与记忆点(**内容**回顾,不是封底) |
| 行动页 | Call to Action | 提出明确的下一步行动、决策事项或号召(做什么 / 谁做 / 何时),把演示导向行动——**有真实 CTA 时才用**,别拿套话凑 |
| 结束页 | Closing | 当 outline 规划了封底时，用一句愿景 / 主张回扣 + 致谢 / 联系方式 / 答疑收尾，视觉呼应封面、保持低密度 |

页型决定该页的版式与视觉重心(数据页以图表为主、对比页采用并列结构、过渡页力求简洁),相邻页应尽量切换页型,避免连续使用同一种。

**页型必含要素(硬性,缺了这页不算完成,规划时就要落实):**

- **案例页**:必须有**具体案例**(真实的人 / 事 / 品牌 / 数据 / 画面),不能只讲抽象道理;
- **数据页**:必须有**真实数据**(或明确标注"示意"),图表带轴 / 图例 / 标签;
- **介绍某人 / 某风格 / 某作品 / 某产品**:必须出现其**代表作品 / 实例 / 画面**(光有文字描述不够);
- **方案页**:必须有**具体做法**(策略 / 步骤 / 功能),不能停在口号;
- **问题页 / 观点页 / 总结页**:必须有**明确的一句话主张 / 结论**;
- **行动页**:必须有**具体的下一步 / 决策项 / 号召**(做什么、谁做、何时),不能停在空泛口号;
- **结束页**:当 outline 中存在结束页时，做成**封底式收束**——含一句愿景 / 主张回扣**或**致谢 / 联系方式 / 答疑;**低密度、视觉呼应封面**(同一主色 / 基调,可用满铺 `.slide--bleed`);**版式必须与总结页 / 行动页明显区分**(不是又一张要点罗列页),不得含"聆听"字样;
- **时间轴 / 计划页**:统一轴线(横竖全页一致)、节点等距、每节点「阶段名 + 时间/里程碑 + 产出」三件套对齐、节点 3–6 个(套 `$SKILL_ROOT/references/layout-patterns.md` 的时间轴范例,别从零手搓)。

**收尾约束服从 outline。** outline 的最后一页就是 deck 的最后一页，不得自动追加结束页。若 outline 最后一页是结束页(英文 deck 即 `Closing`)，它应采用封底式收束，并与总结页 / 行动页明显区分；最后一页不得出现任何含“聆听”的字样。

一份 slide 规划必须**自包含**:slide subagent 只凭 `plan/deck.md` +
`plan/slide_NN.md` + `base.css` 就能做出这一页。把这一页需要的一切——含要用到的事实 /
数字、回填好的配图路径——都写进它的规划。

8. **规划自检(委派 slide 前过一遍,有问题先改规划再委派)** —— 委派后每页要渲染自纠,代价高;规划期的问题在这步一次性堵掉:
   - **设计系统**:`base.css` 的 token 定全了吗(`--bg`/`--accent`/`--ink`/辅助色 + **状态色 `--up`/`--down`/`--warn`/`--note`**,且都和选定主色调和)?中文字体引入了吗?主色 / 背景 / 文字对比够吗?
   - **调色板 / 风格**:风格 + 色调 + 主色是不是**定死了、贴合主题、且全套统一**(主色有确切 hex、背景明暗冷暖已定)?`base.css` 的 `--accent` 用的就是定的那个 hex 吗?状态色没有出界的红 / 粉 / 绿?
   - **背景**:背景是不是**严格跟着选定的色调走**?有没有**又滑回奶油 / 暖白默认**(选了暗 / 冷 / 高对比色调却还是亮奶油底,就是没执行)?整套是不是**一个背景模式**(没有暗底封面 + 亮底正文那种明暗乱跳)?
   - **叙事遵循**:逐页对照 `outline.md`，页数、页序、标题、核心结论和用户明确保留的事实是否一致？没有恢复用户删除的内容，也没有自行追加封面、过渡页或结束页？在不改变 outline 的前提下，页型与阅读节奏是否有变化？
   - **美术指导(治模板味,规划期就定死)**:① 写了一句**设计概念 + 色彩故事**进 `deck.md`、主色避开霓虹青/电光蓝默认了吗?② **安排了 1 个破格 hero 页**(大数字/满幅金句/主图占画布)在叙事高点吗?③ **卡片宫格 ≤2 页、2×2 ≤1 次**,不是页页"眉签→大标题→一排卡片"?④ 没有"节点星座/粒子网"这类泛用科技背景(除非主题就是网络)?哪条没满足,改规划再委派。
   - **⚠️ 数据 / 商务 / 报告 deck 额外必查(它们最爱退回公司安全套路,这步专门拦)**:① 主色是不是**又"暗底+金"默认**?是就换成主题派生的非金配色。② **对每页数 accent 元素,有没有页 >2 处**(标题线+眉签+每个数字+每根柱都染金=超配)?超了就砍。③ **有没有页塞了两个图表 / 或"图+一堆数字+长文"挤一页**?有就拆成"一页一个主信息块"。④ **分幕过渡页是不是都用同一个模板**(都"大数字+标题+进度条")?换不同处理。⑤ **"大数字 + 三栏 stat"这种骨架是不是连用了 ≥2 页**?只留 1 页,其余换版式族。⑥ 一排 KPI / 卡片**规划时就标明用 `.col`+`.foot` 等高对齐**。
   - **每页规划**:每份 `plan/slide_NN.md` 有"页型 + 一句话总结"两行,**而且写了最终文案(逐字上屏文字,不是待润色的要点)+ 版式(套骨架 + 哪种 body 布局)**?要用的事实 / 数字已写进规划(subagent 看不到 fact pack 原文)?视觉方案只用 CSS / SVG / ECharts / 已备素材?缺数字处已标"示意"?
   - **页型必含要素**:每页满足其页型的硬性要素吗(案例页有具体案例、介绍类有代表作品、数据页有真实 / 标注数据、方案页有具体做法——见词表后清单)?
   - **图片机会覆盖（逐页审计 + 全册复判，不设数量）**：逐页读取 `visual_anchor:`，不得缺页。
     判定为真实图片 / 生成图片机会的页必须先有合规 `source_hint:`，最终再二选一：①
     `image_status: ready — <authentic
     user_material | authentic search | generated illustrative>` + `$DECK_DIR/assets/` 内真实存在的
     本地 `image:` 路径 + 合规 `image_source:`；② `image_status: unavailable — <真实
     失败原因>` + 不依赖图片的回退版式。`planned` 残留、无状态回退或泛写“没图”都不通过。
     完成逐页账本后，再检查纯文字、卡片、结构图或抽象 SVG 连续页，防止漏判图片机会；连续页
     不是唯一触发条件。真实产品 UI / 界面截图归真实图片，示意 UI / 界面结构 / 组件关系保留代码
     表达。这是定性覆盖检查，不换算固定图片数量、比例或“每 N 页一张”。
   - **素材**:每条 image brief 都写明了**真实性类别 + 主体 + 用途 + 宽高比 + 主色 / 色调 /
     情绪**?每个图片机会都有且只有一行合规 `source_hint:`：真实图片是
     `info_pack.user_assets.reference_images[<n>]`、
     `raw_documents.documents[<doc_index>].inherited_images[<image_index>]`、
     `deck_asset:assets/<filename>` 或 `search — no relevant user material`，生成图片是 `generate`？
     真实素材按“相关用户素材 → native search → bundled search → none”执行，生成素材已
     明确标注示意 / 艺术重构 / 概念化表达且不冒充证据?`ready` 路径都已原样回填并经
     `$DECK_DIR/assets/` 实证存在，且 `image_source:` 与状态一致：search 是来源页面 URL 而非仅
     CDN 图片 URL，user_material 与 deck_asset 使用稳定定位符，generated 只记录 native/bundled
     工具来源且无 secret，来源 URL 已去除鉴权 / 签名 query 或降为 `来源域名 + redacted`？不得把
     `image_source:` 的远程 URL、用户 home 或 Skill 目录路径当作 deck 图片
     引用。`unavailable` 已记录 native/bundled 的具体失败原因并把「版式」改成不依赖图的布局;
     绝不引用不存在的图、留空槽或删除审计状态。
   - **事实保真(页面生产的最后关口)**:抄进规划的每条事实 / 数字，回
     `raw_documents.json`、`info_pack.json` 和已有 Research 主报告逐项核对来源与数值
     (尤其性别 / 称谓 / 归属 / 单位 / 时间)。发现缺口或叙事级冲突时停止并交回
     Entry/Story，不在规划层自行查证或改结论。
   - **自包含**:随手抽一份 `slide_NN.md`,只凭它 + `deck.md` + `base.css` 能不能独立做出这一页?
   - **质量判据**:内容逻辑 / 叙事经得起 `$SKILL_ROOT/references/quality-checklist.md` 的「单页检查·表达与内容逻辑」「整套检查·全篇结构」吗?

   哪条不达标,就先 `patch` 规划文件 / `base.css`、或重新委派 image subagent,**改好再进入阶段二**。(可选加固:规划复杂、拿不准完整性时,派一个只读 `plan/`+`base.css`(`toolsets:["file"]`)的轻量 subagent 做"规划完整性体检",再进阶段二。)

### 阶段二 —— 并行委派(slide subagents)

进入阶段二时把 `task_pack.state.current_stage` 写为 `output.static_html.pages`。每完成一页，
更新 artifacts 中的已完成页清单和 `updated_at`，使 Workbench 或继续任务能看到真实进度。

规划文件就绪后,调用 subagent 工具把全部页并行委派出去。每个 goal 都传绝对 `DECK_DIR`、`SKILL_ROOT`、页号和当前档位，要求先读 `$SKILL_ROOT/subagents/slide.md`。

每个 slide subagent 各自写 `$DECK_DIR/slides/slide_NN.html`、渲染到 `$DECK_DIR/renders/slide_NN.png`、看图自检(对照 `$SKILL_ROOT/references/quality-checklist.md`)，返回最终截图的绝对路径。

收尾总结必须**如实报告**该页状态(通过 / 还剩哪些硬伤)与最终截图绝对路径 `$DECK_DIR/renders/slide_NN.png`,别谎报通过——编排器与 review subagent 靠这个判断哪页需返工。

### 阶段三 —— 复审(review subagent 诊断 + 编排器决策)

进入阶段三时把 `task_pack.state.current_stage` 写为 `output.static_html.review`。

**第 0 步:落盘文件完整性核对 + 补齐失败页(委派 review 前,编排器亲自做、不靠自报)。** 这步是**实证总检**——你按 `plan/deck.md` 定的总页数 N,自己 `read_file` 列目录 / 读文件逐项核对,别只信 slide subagent 的文字返回:

- **文件齐全(按页号逐一对)**:`plan/slide_01..NN.md`、`slides/slide_01..NN.html`、`renders/slide_01..NN.png` 三处**每页一份、页号连续不缺号、不多不少**;`base.css` 存在。(列目录用 `read_file`,和阶段一·8 `read('assets')` 同一招;读 `slides/` 是**只读**,不违反「不写 slides」的边界。)
- **⚠️ `slides/` 无杂物(必查,治返工备份拖垮验收)**:`slides/` 里应**只有** `slide_01.html..slide_NN.html` 这 N 个正式页,**绝不能有多余的 `slide_*.html`**(返工备份 / 副本 / 临时文件,如 `slide_NN.bak.html`)。落盘验收按 `slides/slide_*.html` 数页——**多一个没渲染图的杂物,整份 deck 就被判废**(已实测:一份合格 deck 因 `slides/` 里残留一个 `slide_04.bak.html` 被误废)。发现杂物时:你(编排器)**没有删除/执行工具**,所以**带 note 重新委派对应页的 slide subagent,让它用 terminal `rm` 掉那个杂物文件**(它有 terminal),清净再进复审。
- **非空 / 非占位**:`read_file` 抽查 HTML 与规划文件不是空文件 / 截断 / 占位残留(0 字节、半截、只有骨架没内容都算坏);`renders/*.png` 在目录里**存在即可**(`render.py` 已保证渲染成功才落非空 PNG,编排器看不了图,完整性只核存在性,质量交给 review)。
- **图片机会与消费账本**：逐页读取 `visual_anchor:`、`source_hint:`、`image_status:`、
  `image_source:`、`image:` 和对应 HTML。
  图片机会页若为 `ready`，本地路径必须存在且 HTML 必须通过 `<img>` 或 `background-image`
  引用同一素材，且来源字段必须与 ready 类别匹配；路径未引用、来源缺失或被 SVG / CSS 替换，
  按硬伤重新委派。若为 `unavailable`，必须有
  native/bundled 的真实失败原因、没有伪造路径，且 HTML 使用无图回退版式。`planned` 残留、
  图片机会页无状态、真实素材失败后改用生成仿真素材，均不得进入复审。
- **产物与自报对齐**:结合 slide subagent 的返回判断页面状态。自报仍有硬伤、状态异常或文件缺失时标记；如果调用异常但 HTML 与渲染图已经完整可用，不要只为修复轨迹状态重派。

凡**缺文件 / 空文件 / 页号断号 / 自报存在硬伤**的页，带着具体问题重新委派一次。仍失败时保留其余可用页面，在最终总结中明确缺失或未解决的问题，不要无限重派。

slide subagent 全部返回后，按执行强度委派 review subagent(`toolsets:["vision"]`,让它先读 `$SKILL_ROOT/subagents/review.md`)检查 `$DECK_DIR/renders/` 下的整套截图，返回问题清单。goal 同时要求它读取逐页规划和 HTML，按 `$SKILL_ROOT/references/quality-checklist.md` 核对 `visual_anchor` / `source_hint` / `image_status` / `image_source` / 本地路径 / 实际引用，不得只凭截图猜图片是否合规。Draft 只找硬伤；Standard 和 Deep 再检查整套一致性。

**编排器据清单做决策**(自己不渲染、不看图、绝不动 `slides/` 的 HTML,会被拒),按问题类型走:

- **跨页视觉统一**(配色 / 字体 / 间距等 token 漂移,或状态色没定全 / 定得不对)→ 改 `base.css` 补齐和谐的 token,然后**重新委派**受影响的页(slide subagent 会基于更新后的 `base.css` 重渲并复核);你自己不渲染、不改页;
- **某页内容 / 结构 / 局部问题** → 调用一个 slide subagent 重做那一页,带 note 传入具体修改点(注意:重新委派会让 subagent 按规划**重写**该页 HTML,只在确有内容问题时才用);
- **个别页配色出界** → 若是 `base.css` 的状态色没定全 / 定得不对,走上面第一条改 `base.css`;若是某页乱用调色板外的颜色,带 note 重新委派那一页;
- **同类问题系统性复发**(多页同一个毛病)→ 多半是 `base.css` / 规划层根因,改 `base.css` 或对应规划,而不是逐页修。

重委派的页同样走自检并返回新截图。**收尾纪律:**
- Draft 只修复硬伤，不做审美返工。
- Standard 最多一轮定向返工；Deep 最多两轮。
- 重写前保留旧版；新版不优于旧版时恢复旧版。
- 改过 `base.css` 时再做一次整套检查，但总轮次仍受档位上限约束。

### 阶段四 —— 收尾

**第 0 步:最终产物检查。** 逐项确认:
- `outline.md`、`plan/deck.md`、`base.css` 存在。
- outline 的每一页都有同序号的 `plan/slide_NN.md`、`slides/slide_NN.html` 和 `renders/slide_NN.png`。
- 页面标题、顺序和核心结论仍与当前 outline 一致。
- 某张图片始终生成失败时，改成不依赖该图的布局，不让单张素材阻塞整套。
- 缺失必要产物时按本档位规则最多重试一次；仍失败则如实报告。

**第 1 步:生成可播放文件 `present.html`(必做,不可省)。** 全套页定稿(复审通过 + 第 0 步门槛过了)后,委派一个轻量 subagent(`toolsets:["file","terminal"]`,`label` `player`,无需职责卡)运行:
```
python "$SKILL_ROOT/scripts/build_player.py" "$DECK_DIR/slides" "$DECK_DIR/present.html"
```
它扫描 `$DECK_DIR/slides/slide_*.html`(自动跳过 `.bak.`)，在 `$DECK_DIR/present.html` 生成播放入口。没有该文件不算交付完成。

**第 2 步:默认导出 HTML -> PPTX 兼容版。** 除非用户明确要求只要 HTML，并且
`task_pack.choices.static_postprocess` 已记录为 `[]`，否则必须执行以下内置转换命令：

```bash
node "$SKILL_ROOT/scripts/export_pptx/html_to_pptx.mjs" \
  --deck-dir "$DECK_DIR" \
  --pages-dir "$DECK_DIR/slides" \
  --output "$DECK_ID.pptx" \
  --force
```

这是 Static HTML 的唯一 PPTX 后处理路径。禁止改用 `sn-ppt-pptx`、宿主 Agent 的
原生演示工具或手工重建 PPTX。用户在任务中明确说“需要 PPTX”时，也仍然直接执行
上述脚本。导出失败时保留并交付全部 HTML、渲染图和 `present.html`，如实报告
原因，不删除或重跑页面。

完成后把 `present.html`、`slides/`、`renders/` 和 PPTX 的真实路径写入
`task_pack.state.artifacts`，把 `static_html` 加入 completed_stages；成功时状态写
`completed`，局部失败时写 `partial` 和 `last_error`。

然后以一段简短消息总结这个 deck:页数、各页 HTML 和渲染图、复审结论，并固定包含：

```text
HTML 放映版（效果最完整）：<DECK_DIR 绝对路径>/present.html
PPTX 兼容版：<DECK_DIR 绝对路径>/<DECK_ID>.pptx
说明：PPTX 由 HTML 转换生成，复杂 CSS、字体、渐变、遮罩及部分图表效果可能存在视觉损失，请以 HTML 版为效果基准。
```

若 PPTX 导出失败，不得伪造 PPTX 路径；把第二行替换为“PPTX 兼容版：导出失败 -
<简短原因>”，HTML 放映版仍正常交付。**指出 `outline.md` 是用户可编辑的叙事源**：改页序、观点或增删页面时先改它；`plan/` 是 Agent 维护的页面生产细节。`present.html` 浏览器打开即播。

Draft 完成后，基于实际产物补充最多 3 条具体升级建议，例如哪一页仍是示意数据、哪一页可
补真实素材、哪一组页面需要更完整复审。不要给通用套话，不自动升级到 Standard/Deep，也
不为建议另建计划文件。
