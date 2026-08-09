# 规划文件合同

本文件规定 `plan/design-brief.md`、`plan/deck.md` 和 `plan/slide_NN.md` 的结构。编排器在规划阶段读取；具体构图由 Slide 按逐页 `Reference route` 完成。

## 合同关系

用户要求和已核验事实决定“说什么”，`design-brief.md` 和 `plan/deck.md` 决定“整册怎样表达”，`plan/slide_NN.md` 决定“这一页怎样落地”。逐页计划可以把全册方案说得更具体，但不能反过来削弱它。

如果整册方案把图片定为主视觉、视觉证据或情绪锚点，逐页计划必须给它相称的版面角色。不能为了多放文字，把它改成“证明用过图片”的小缩略图。确实需要改变媒介或主次时，先修改整册方案并说明理由，再冻结逐页计划。

## 0. `plan/design-brief.md`

设计简报是整册视觉真相源，按以下结构写：

```text
## Scene & audience
- primary_scenario: <一个主场景>
- category_reference: references/slide-categories/<selected>.md
- category_baseline: <场景对可读性、证据和节奏的要求>
- audience_action: <听众离场时能判断、理解或完成什么>
- narrative_spine: <内容如何推进>
- evidence_objects: <最重要的证据对象>
- page_families: <本册主要页族>
- density_rhythm: <聚焦、解释、证据和呼吸如何交替>
- auxiliary_lens: none | <借用的一个辅助动作及用途>

## Design direction
- design_source: original | user-system | named-built-in | template | style-transfer
- visual_thesis: <一句能指导取舍的视觉主张>
- signature_visual: <最能被辨认、但不会机械重复的视觉动作>
- selected_references:
  - references/slide-categories/<selected>.md
  - references/style-routing.md              # 自主设计且实际采用其词汇时记录
  - references/style-families/<selected>.md  # 自主设计时只记录选中的一个风格簇
  - references/style-systems/<selected>.md   # 用户明确选择内置系统时记录
  - references/design-rules.md               # 实际读取时记录
  - references/charts-and-diagrams.md        # 需要图表或复杂图解时记录
  - references/shape-grammar.md              # 仅需要时记录
  - references/fonts.md                      # 仅需要时记录

## System
- base_canvas_family: <基础画布的明度、色温和材质>
- canvas_and_grid: <基础画布、栅格、安全区与状态变化>
- palette_roles: <底、墨、线、强调和状态色>
- typography_roles: <display、title、body、number、annotation、fallback>
- spatial_prototypes: <会使用的阅读路径、主辅关系和峰值页>
- image_treatment: <图片来源、裁切、色调、边缘和文字安全区>
- chart_language: <坐标、系列、标签和强调方式>
- shape_grammar: <基元、圆角、线宽、连接和注释语法>
- motif_role: <母题在何处为主、为辅或缺席>
- special_page_system: <封面、章节页和结尾页的共同语言与不同动作>
- visual_state_range: <允许的明暗、色场、材质和媒介变化>
- fallback: <招牌素材或能力不可得时的等价方案>
- avoid: <本任务最容易出现的退化>
```

`selected_references` 只记录真正读过并采用的文件。分类指南决定听众任务、证据和页面家族；风格系统决定视觉语言；形状和字体文件只在相关时进入。后续 Agent 读取设计简报和逐页路由，不自行扫描 reference 目录。

## 1. `plan/deck.md`

按以下顺序写：

### 1.1 已解析任务

- `task_mode: create | edit-local | edit-structural`；
- `design_direction: autonomous | design-system | template | style-transfer`；
- `input_type: topic | full-document | outline | template-reference | existing-deck`；
- `response_language` 和 `deliverable_language`；
- `page_count`、确定依据和用户明确的时长；
- 可用的 web、图片搜索、图片生成、渲染、Vision 和写文件能力；
- `evidence_path: direct | grounded` 及理由；
- 演讲者、听众、场合和目标；
- 核心命题；
- 用户明确要求、合理假设和未确认项。

这些字段只用于生产，不自动成为屏显文案。

### 1.2 叙事结构

- 开场如何建立问题；
- 各章分别完成什么认知任务；
- 关键转折和峰值页；
- 结尾如何回应开场；
- 逐页表：页码、标题、页面职责、页型、证据、媒介和节奏。

每页必须推进叙事。两页职责相同时，合并或明确区分“结论”和“证据”。

### 1.3 整册视觉方案摘要

从 `plan/design-brief.md` 摘录执行要点：

- `visual_thesis` 和 `signature_visual`；
- `design_source: original | user-system | named-built-in | template | style-transfer`；
- 字体角色、颜色语义、数字风格；
- `base_canvas_family`；
- 允许的 `visual_state_range`；
- 图片语言、裁切方式和调色方式；
- 构图与图形语法；
- `motif_role`：母题在何处为主、何处为辅、何处缺席；
- `density_rhythm`：聚焦、解释、证据和呼吸页如何交替；
- `special_page_system`：封面、章节页和结尾页的共同语言与不同动作。
- `category_reference`、`selected_references` 和 `page_families`；
- `shape_grammar`：基元、圆角、线宽、连接、注释和装饰形状的语义。

整册视觉方案不是固定页面模板。稳定的是字体、颜色语义、图片处理和构图逻辑；变化的是焦点、方向、媒介、密度和留白位置。

### 1.4 背景系统

写明：

- `base_canvas_family` 的明度、色温和材质；
- 局部色场、环境光、主题肌理和图片背景如何使用；
- 哪些页面允许整页变体；
- 每个变体的叙事用途；
- `enter_from / exit_to` 如何与前后页承接。

表达型场景不能无理由退化为整册同一纯色底。严肃场景可以安静，但仍需靠排版、证据视觉和适度材质建立层次。

### 1.5 配图与附件计划

`image_opportunity_map` 逐页写：值得被看见的主体或场景、图片为听众增加的价值、选择真实图/生成图/代码视觉的理由。

有附件时另写：

- `material_visual_mode`：`facts-only | visual-reuse | style-reference | faithful-restyle`；
- `attachment_visual_map`：`source_path`、`decision`、`material_asset_type`、`asset_path`、上屏页、处理方式和理由。

`decision` 使用 `must-show | reuse | reference-only | omit`。

论文图片区分：

- `figure-crop`：页内命名 Figure 的独立裁图；
- `page-facsimile`：整页论文原貌本身就是展示对象。

### 1.6 页面组

页面组与 `evidence_path` 无关。一个组应能在同一设计记忆中完成整组首稿、批量渲染和一次整组检查。同时考虑：

- 制作方式和构图亲缘：特殊页系统、真实图片叙事、复杂 Canvas、ECharts 等是否共享一套解法；
- 共享素材与叙事连续：组内是否需要看到前页才能正确设计后页；
- 工作负载：页数、复杂页比例、独立图片与渲染复核量；
- 并行价值：拆出的组能否独立开工，并显著缩短串行长尾。

不设每组固定上限。一至三页且制作方式统一时可用一个组；四页以上的新建 deck 至少形成两个能同时开工的设计单元。短 deck 优先保留“封面与结尾”的呼应，再把核心内容按真实制作问题拆成一至两个组。不要为了填满并发把连贯页面拆成单页 Agent。

拆组按下面的顺序判断：

1. 先把确实需要共享设计记忆、素材或前后文的页面放在一起；
2. 再把其中可以独立完成的制作问题拆开，例如图片叙事、数据图、复杂机制图和特殊页系统；
3. 检查是否存在一个明显吞掉大部分制作时间的主导组；若其他组会很早结束而它形成串行长尾，继续拆分；
4. 合并只有一页、又没有独立制作意义的碎片组，避免退化成单页 Agent 海洋。

最终追求的是若干负载相近、边界清楚、可以同时开工的设计单元，而不是固定页数，也不是组数越多越好。

```text
## Production groups
### <group_id>
- pages: 01,02,03
- purpose: <该组共同完成什么>
- design_dna: <稳定的字体、颜色、图片和图形语言>
- page_variations: <每页焦点、媒介和重心如何变化>
- visual_beat: <组内节奏>
- load_reason: <为什么这些页适合由同一 Agent 完成，负载为何可控>
- parallel_with: <可同时开工的 group_id，无则 none>
- boundary_handoff: <enter_from / exit_to>
```

每页必须且只能属于一个组。章名相同不构成分组理由；真实照片、复杂 Canvas、ECharts 和普通卡片若没有共同制作问题，不应粗暴放入同组。所有互不依赖的组在同一次 `delegate_task` 中派发。

### 1.7 重复与节奏预检

在同一个 `plan/deck.md` 中写 `## Repetition & rhythm preflight`，不创建新脚本或额外计划文件。

检查：

- 相邻页的画布、标题锚点、构图方向、媒介、图片占比和密度是否机械重复；
- 封面、章节页和结尾页是否有亲缘性，又各有明确动作；
- 母题是否有主次与缺席，而非全册重复粘贴；
- 各章是否照抄同一套页面脚本；
- 章节换场是否有进入和退出承接；
- 每页是否有独立职责。

发现问题先改页面地图、整册视觉方案或页面组，再冻结计划。

## 2. `plan/slide_NN.md`

每页使用以下结构：

```text
# Slide NN — <标题>

## 页面目标
- 页面职责：这页让听众理解 / 相信 / 决定什么
- 听众所得：本页不可替代的一句话
- 主焦点：第一眼看到什么
- 阅读路径：1 → 2 → 3
- 视觉验收：只看最终像素，听众应能复述什么
- 页型：<type / arch>
- 密度：anchor | dense | breathing
- production_group：<group_id>

## 最终屏显文案
- 标题：<逐字定稿>
- kicker / subtitle：<可选>
- 正文、标签、数字、结论：<逐字定稿>

## 证据
- 事实、数据、原话或用户提供信息
- 本页支撑层：证据 / 机制 / 对比 / 案例 / 行动 / 边界

## 视觉实现
- medium：photo | generated image | canvas+HTML labels | ECharts | typography | small SVG icon
- primary_visual：<主要视觉载体>
- image_opportunity：<主体或场景 + 图片价值 + 媒介判断>
- visual_weight：<主要视觉在构图中的主次、尺度与对重方式>
- spatial_prototype：<Hero→结论 / 对象→解释 / 证据→推断 / A↔B / 路径→决策 / 主张→回响>
- presentation：subject-only | framed-scene | full-bleed | evidence-crop
- layout：<区域比例、对齐和重心>
- spatial_budget：<焦点、文字、证据和留白如何分配>
- background_treatment：<基础画布或有理由的变体 + 前后承接>
- bold_action：<唯一主要设计动作；普通页可为 none>
- image：<asset_id、实际路径、origin、用途>
- crop_contract：fit / focal_point / protected_parts / allowed_crop / object_position
- material_asset_type：attachment-image | figure-crop | page-facsimile
- chart：<类别 × 系列 × 值、单位、时间、来源、结论>
- diagram：<节点、关系、方向、层级、领域证据>

## Reference route
- category：由 `plan/design-brief.md` 消费，不重复读取
- layout-patterns.md：<命中章节>
- design-rules.md：<命中章节>
- charts-and-diagrams.md：<本页需要图表、流程、架构或复杂关系时填写；否则 none>
- shape-grammar.md：<本页需要关系图、结构图或形状系统时填写；否则 none>
- fonts.md：<本页有特殊字体职责时填写；否则 none>
- quality-checklist.md：单页检查

## 来源
- <来源；无外部来源写 user-provided / none>

## 口语讲稿
<可直接面对听众朗读，不机械复读屏显>
```

### 2.1 屏显边界

只有 `## 最终屏显文案` 进入 HTML。页面目标、证据编号、素材路线、来源、文件路径、验收说明和讲稿都不直接上屏。

逐页做观众价值检查：标题、副标题、图片角标、badge、callout、图例和页脚中的每段文字都应增加新信息。同一状态或结论通常只保留一个最强载体，其余区域补充对象、原因、变化或结果。

最终屏显文案由编排器定稿，应当具体、自然、可直接说给听众。优先使用对象、动作、变化、原因和结果，避免用生僻比喻、行业黑话或生产术语代替信息。不要把“不是 X，而是 Y”“X 就是 Y”“关键结论”“N 条路径 / 战线”以及“为什么 / 凭什么 / 怎么做”当成自动标题模板；只有题材和真实语境确实需要时才使用。也不用“弹药投向哪里”“第 N 个东西”等过度口语化表达。

屏显文案不使用 emoji 或 Unicode 图标。需要图标时，在视觉实现中规划本地 SVG 或 CSS 形状。

### 2.2 内容充分性

普通内容页除了主题句，还需要一层有效支撑。只有同义标题、状态角标和大片空白时，合并页面、改变职责或补充已有证据，不用无意义装饰填空。

`dense` 页若主信息只占半张画布或一条窄带，先重做空间分配。卡片面积铺满但内部大面积空置，同样属于空间计划失败。

### 2.3 图片合同

- 先写图片在页面中承担的工作，再选择真实图或生成图。
- 来源优先级为：用户提供的图片；官方网站、官方报告和可信来源；与内容直接相关的搜索图片；用于概念表达或氛围的生成图片。
- Logo、图标、装饰纹理和微型缩略图不算实质性图片。实质性图片应帮助听众识别对象、理解机制、判断证据或进入场景。
- 图片若承担识别、证据、场景、情绪或 Hero 职责，必须在投影尺度下清楚可辨，并参与构图；不能只作为角落缩略图、空卡片里的点缀或“已配图”证明。
- `presentation`、`layout`、`spatial_budget` 和 `visual_weight` 必须共同落实整册方案中的图片职责，不能互相抵消。
- 具名人物、地点、作品、产品和案例默认先查真实图片。
- 产品、界面、实验对象、现场和技术案例优先使用真实图片、截图或可信证据图；分析型、技术型和学术型页面不能一律退化为文字、色块和通用形状。
- 虚构人物、概念场景、未建成空间、过程切面和情绪主画面优先生成；能由具体画面讲清的内容，不得为省事降级成抽象几何占位。
- 媒介顺序是：具体对象与场景用真实/生成图片，精确关系用 Canvas + HTML，定量数据用 ECharts，小型辅助符号才用 SVG。半屏或全屏主体视觉不得规划为 SVG。
- `subject-only` 必须由 Image 交付透明 PNG。
- `crop_contract` 只列真正承担识别或语义的保护区域。
- 用户围绕附件图片提出任务，或附件图本身不可替代时，至少安排一次可辨认的原图或忠实裁切上屏。

### 2.4 图表与解释图

- 先用 `charts-and-diagrams.md` 把页面问题映射到合适图型或图解；不要先从组件外观出发。
- 图表保留可直接实现的 `类别 × 系列 × 值`，以及单位、时间、来源和结论。
- 流程、机制和方法页写清节点、关系、方向和领域证据。
- 表达流向、因果或反馈的线必须声明方向。
- 科学、技术、医疗和工业方法页优先使用真实或忠实简化的输出，如光谱、显微图、CT、检测界面或样本—仪器—结果链。
- 通用圆形、矩形和六边形只能表达抽象层级，不能冒充领域证据。
- 解释图若不需要精确拓扑，应优先把过程或空间转译为真实/生成图片 + HTML 标注，而不是绘制抽象 Canvas 占位。

## 3. 页型选择

| 页面职责 | 首选方式 |
| --- | --- |
| 开场 | cover / hero：一个统治性焦点 |
| 章节切换 | transition：主信息团 + 视觉对重 |
| 时间演进 | timeline：时间和关键转折 |
| 指标结论 | KPI / hero number：单位、时间和对比口径 |
| 方案对比 | comparison：统一维度和清晰取舍 |
| 流程机制 | Canvas + HTML 标签：方向和关系可读 |
| 数据趋势 | ECharts：一图一结论 |
| 人物、地点、作品、产品 | media-led：真实图片承担识别 |
| 引述 | quote：原话、身份和出处 |
| 结论或行动 | closing / decision：低密度、明确下一步 |

相邻页面避免连续复制同一构图。全册应有 anchor、dense 和 breathing 的节奏变化。

## 4. 特殊页

### 封面

建立一个强焦点。标题、主图和必要信息围绕一个主要设计动作组织。辅助文字必须真实、有用且不重复；不得为了显得高级而编造场记、坐标、档案号或技术注。

### 章节页

章节号、标题和一句承诺组成主信息团；图片、色场、排印或母题形成视觉对重。过渡页内容简洁，但不能只在角落放一小团字，剩余画布没有职责。

多个章节页共享字体角色、编号语法、标题锚点家族、图片处理和颜色语义；构图方向、裁切和重心可以变化。

### 结尾页

结尾不是“最后一张总结内容页”。默认保留一个收束主张、至多一条短支撑和一个视觉锚点。没有明确非对称配重时，核心信息团水平、垂直光学居中。

参考文献与结尾必须分开。结尾不显示页码、进度、页脚、`END` 或“感谢聆听”等套话。

特殊页使用全画布结构，不套普通内容页的标题—正文—页脚家具。局部可读性问题优先移动文字、调整局部明度或使用贴合文字的小托板，不临时加横跨主图的大色带。

## 5. 冻结检查

计划冻结前确认：

- 所有事实来自用户、附件、`grounded-knowledge.md` 或明确标注的示意；
- 页数、页序、标题和讲稿页码一一对应；
- 每页有独立职责、主焦点、支撑层和可由像素判断的验收句；
- 屏显内容通过观众价值检查，没有内部字段和无增量重复；
- 逐页表与逐页计划的页型、媒介和视觉职责一致；
- 图表的类别、series、数值、单位和来源完整；
- 图片都有实际路径或可执行 brief，重要图片有 `crop_contract`；
- 背景来自同一画布家族，整页变体有用途和前后承接；
- 页面组完整、无重复归属，并写清 `boundary_handoff`；
- 重复与节奏预检已完成；
- 封面、章节页、结尾页和参考文献页各自承担正确职责；
- 不存在占位符、未解析路径、待定数字或“后续补图”。
