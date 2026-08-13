# 全局规划与逐页交接合同

本文件只在阶段 3 由 Orchestrator 读取。它定义“规划必须交付什么”，不替代按页型读取的 `layout-patterns.md` 或按主题读取的 `design-rules.md`。

## 阅读顺序

1. 先写 `plan/deck.md`，锁定叙事弧与 Production groups；
2. 再按统一字段一次写完全部 `plan/slide_NN.md`；
3. 最后过页型、素材、讲稿与冻结检查。

目录：§1 全局计划；§2 逐页合同；§3 页型选择；§4 素材与引用；§5 讲稿；§6 冻结门。

## 1. `plan/deck.md`

至少包含：

- response / deliverable language；
- Speaker、Audience、Occasion、Objective、Duration、Page count；
- 一句话核心命题与 1–3 条 takeaway；
- 叙事弧：开场、各幕、转折、结论、行动；
- 逐页表：页码、标题、页面职责、页型、核心证据、视觉媒介、节奏；
- Style Lock 摘要与全册设计读数：记录 `design_ambition`、完整读取的 `resolved_system_id`、`resolved_system_read: complete`、保留/转译/删除的系统层，以及稳定视觉语言和允许变化的构图变量；Style Lock 不是固定页面模板；
- `typography_recipe / palette_recipe`：完整落实 `aesthetic-recipes.md` 的标题/正文/数字角色、type-event map、中性/accent/语义/效果四层与对比计划，禁止只写字体名和主色 hex；
- 字体合同：显式记录 `title_voice / title_scale / title_treatment / body_voice / numeric_voice / font_roles`。普通演讲默认让短标题与正文形成舞台级反差；正式/严谨场景才收敛到 Noto Sans/Serif SC。严谨型 1–2 族、常规演讲 2–3 族、明确表达型演讲 3–4 族，且所有角色跨页稳定；
- `image_opportunity_map`：哪些页面需要真实证据、人物/场景识别、产品展示或情绪主画面，哪些页面更适合图表/Canvas/排印，以及判断理由；
- 有附件时的 `material_visual_mode` 与 reuse map：区分事实来源、可直接复用的图片/图表、可参考的视觉语言和需要重新设计的文档结构；
- 有附件时的 `attachment_priority_map`：逐项复制 `grounded-knowledge.md` 中的 `priority_id / screen_priority / source_locator / fidelity_form`，并为每个 `must_present` 写实际上屏页与可见载体；讲稿不得作为上屏页或可见载体；
- 有图片附件时的 `attachment_visual_map`：只列用户点名、不可替代证据和经过筛选的候选视觉；每项写 `source_path`、`decision`（must-show / reuse / reference-only / omit）、信息增量、投影可读性、`material_asset_type`、实际 `asset_path`、计划上屏页与理由。附件视觉是候选而非全用配额；低清、文字密集、重复或不适合页面职责的图可以 omit，并另取更合适图片或用 SVG/Canvas/ECharts 表达。论文 Figure 使用 `figure-crop`，并保留 `figure_id / source_pdf / source_page / visual_subject_box / render_source / pixel_size / body_text_fraction`；整页论文页面仅在页面原貌本身就是证据时使用 `page-facsimile`。`must_present` 内容不自动等于附件原图 `must-show`；事实可以由屏显文案、图表或图解履约；
- `background_system`：先说明本场景为何偏克制秩序或氛围表达，再定义 `base_canvas_family`（普通内容页共同的明暗/色温/材质家族）、局部色场/环境光、主题肌理、图片背景、允许整页使用的变体及其叙事用途，并写清整页变体的 `enter_from / exit_to`。表达型场景不能无理由把“整册同一纯色底”当作安全默认；需要下载或生成的背景进入 Image 素材计划；
- `visual_state_range`：在基础画布家族内允许变化的明度、色场、图片占比、信息密度、构图方向与章节状态；给出适用页面与前后承接，避免把统一误解为全册同一底色或同一几何；
- `motif_role`：列出母题作为 `primary / secondary / absent` 的页面范围及语义用途；同一个装饰母题不能同时承担封面、全部过渡页和多数内容页的主视觉；
- scene_register 与选择理由；
- `visual_thesis / signature_visual` 的兑现地图：落在哪些页面、以什么可见形式出现、Review 如何判断不是只写在 brief 里；
- `spatial_rhythm`：哪些页铺满、哪些页聚焦、哪些页留白，以及这些变化如何服务叙事；
- `special_page_system`：由 `bookend_system / divider_system` 组成的跨页视觉合同，记录单页生产的封面、结尾和各 divider 所共享的字体角色、标题锚点家族、图像处理与图形语法，以及各页不同的构图动作；
- Production groups；
- `Repetition & rhythm preflight`：规划冻结前对页面地图做一次重复率与节奏预检，记录需要调整的页面或 `pass`；
- 假设、风险和待核项。

所有“用户明确要求 / 用户原文 / 用户提供”的陈述必须能在原始 query 或 Material 原文中定位。Research goal、搜索 query 和编排器推断不构成用户事实；外部证据否定这些候选时，记录为“编排器假设未成立”，不得写成“更正用户”。

`deck.md` 中的 Speaker、Audience、Occasion、Objective、页面职责、节奏、Production groups、素材路线、假设和风险都是内部生产上下文。它们指导内容取舍与语气，但不自动成为 PPT 文案。除非用户明确要求展示，或页面主题本身正在讨论这些对象，否则不得把它们原样复制成“受众：… / 主体：… / 目标：… / 页面角色：… / 证据：… / 内部使用”等屏显标签。

每一页必须推进叙事。两页承担同一职责时，应合并或明确区分“结论”和“证据”。

### Production groups

采用**原子页优先、普通同构页小组化**。封面、每张 divider、closing、hero/视觉峰值、复杂图解、独立高密图表和重图像合成页必须各自成为单页 group。只有制作方式、信息结构和视觉语法真正同构的普通内容页才可合组，默认 2 页、最多 3 页；相邻、同章、同白底、同左右分屏或“共享 CSS”都不构成合组理由：

```text
## Production groups
### cover-01
- pages: 01
- purpose: 建立开场命题与视觉世界
- design_dna: 字体角色、标题锚点、图片处理、母题
- why_grouped: atomic-special-page
- anti_repetition_delta: 建立本册第一次尺度事件
- boundary_handoff: 从无到基础画布

### divider-04
- pages: 04
- purpose: 单独导演第一次章节换场
- design_dna: 章节编号、展示字阶、标题锚点、图像/色场处理
- why_grouped: atomic-divider
- anti_repetition_delta: 相对封面改变重心与裁切动作
- boundary_handoff: 写明前一页状态、换场动作、新章首两页如何承接

### content-05-06
- pages: 05,06
- purpose: 两页共享同一证据比较任务
- design_dna: 同一图像处理与比较语法
- why_grouped: 同媒介、同信息结构、同一比较动作
- anti_repetition_delta: 05 左侧结论+右侧证据；06 满宽证据+底部结论，不做左右互换
- boundary_handoff: 写明进入与退出状态
```

- cover、每张 divider、closing、hero、复杂图解和独立重制作页都是单页 group；它们通过 `design_dna / boundary_handoff` 保持亲缘，不通过同一个 Agent 批量制作；
- 普通同构内容组默认 2 页、最多 3 页，每组必须写 `why_grouped` 和逐页 `anti_repetition_delta`；“只是左右互换”不算差异；
- 每页只能属于一个组；全部页面必须被覆盖；
- 每组写 `boundary_handoff`：至少给出进入前一页、组内首尾页和离开后一页的画布家族、明度、色场/图片处理与应延续的母题。Slide 不读取组外页面，因此边界信息不能只隐含在全册逐页表中；
- `design_dna` 从 Style Lock 提取稳定语言，`anti_repetition_delta` 定义受控变化；二者共同保证“同一设计世界、不同最佳构图”，不得把上一页当模板只换文案。

### Repetition & rhythm preflight

在 Image 与 Slide 启动前，用逐页表做一次轻量预检，不创建新脚本或额外计划文件：

- 横向比较相邻页的 `base_canvas / visual_state / title_anchor / composition_direction / medium / image_ratio / density / motif_role`；连续多页若只是同一几何换文案，重新分组或改构图导演；
- 比较 cover、dividers、closing：字体角色、颜色语义和图像处理应有亲缘性，但主焦点、构图动作和章节状态不能机械复制；
- 母题可以建立识别，但应在一部分页面退为次要线索或主动缺席，让真实照片、数据、机制图和排印各自承担主视觉；
- 检查 anchor / dense / breathing 的起伏和章节边界的进入/退出，不允许全册同明度、同占比、同视觉重量；
- 检查每页职责是否独立；职责重复则合并或明确“结论 / 证据 / 应用”的差异。
- 纵向比较各章节的页面脚本；可以共享设计 DNA，但不能把同一套页序和同一层信息只换对象名重复生产。相似案例若拆开后各自内容很薄，优先合成真正的前后对比或让每页分别承担诊断、变化依据与行动决策。

预检只修正页面地图、Style Lock 与 Production groups，不替 Slide 预制固定模板。

## 2. `plan/slide_NN.md`

每页按以下顺序写：

```text
# Slide NN — <标题>
## 页面导演
- 页面职责：这页让受众理解 / 相信 / 决定什么
- 主焦点：第一眼看到什么
- 阅读路径：1 → 2 → 3
- 视觉验收：观众只看最终像素应能复述什么；对象/符号分别代表什么；方向或关系如何读；需要出现哪类领域证据
- 页型 / arch：<名称>
- 密度：anchor | dense | breathing
- production_group：<group id>
- attachment_priority_ids：<本页承接的 must_present/supporting priority_id；无附件重点则省略>

## 最终屏显文案
- 标题：<本页标题；保留该固定字段供讲稿同步>
- kicker / subtitle
- 正文、标签、数字、结论（逐字定稿）
- 屏显文案不使用 emoji 或 Unicode 图标（如 `👀 ✋ 💡 ✨ ★ ✦`）；需要图标时在视觉实现中规划本地 SVG / CSS 形状，不把装饰字符写进文案
- 可读性预演：指出哪些是听众必须读清的正文 / 数据 / 标签；这些内容应能直接使用 `--fs-body`、`--fs-h2` 或 `--fs-caption` 排下，不依赖额外缩字
- 内容充分性检查：写出本页不可替代的听众所得，以及由证据、机制、对比、案例、行动或边界承担的支撑层；没有新增支撑层时合并或重构页面，不用同义短语和空白把薄内容拉成一页
- 观众价值检查：每个可见文本节点分别说明它向听众增加了什么信息，并横向比较标题、kicker / subtitle、图片角标、badge、callout、图例与页脚；若只是复述主题、状态、页型、页码、制作状态或内部规划字段，删除或改写。同一状态词（如“整理前 / 整理后”“现状 / 目标”“问题 / 方案”）通常只保留一个主载体，其余区域补充对象、原因或结果

## 视觉实现
- medium：photo | generated image | SVG diagram | canvas+HTML labels | ECharts | typography | small SVG icon；同时点明本页的**主要视觉载体**。静态结构/机制/关系可用 `svg-diagram`，不受五类 archetype 白名单限制；真实主体、论文 Figure、实验影像或场景有可用图片时优先配图，不用 SVG 重画；`small SVG icon`、边框、空面板和装饰线只能辅助，不能冒充主要视觉
- image_opportunity：**只写一个机器可读枚举**，不带任何解释：`real_required` / `generated_ok` / `none` / `chart_only` / `canvas_only` / `typography_only`（`none` 及 `*_only` 判定为无位图）。启动 gate 与交付验收只解析这一行的枚举；理由另写在下一行的 `image_opportunity_reason`，绝不写进本行。
- image_opportunity_reason：人类可读理由（一句话）。判定内容：先写值得被看见的主体/场景，以及图片能增加的证据、识别、临场感或情绪价值，再决定媒介。没有项目实拍不等于没有图片机会；尚未建成的空间、虚构人物、服务场景与风格化主视觉可考虑统一风格的生成图。具名真实人物、主创、嘉宾或团队成员是默认的真实图片机会：应规划批量检索肖像、官方简介照、活动照或团队合影；“不生成假真人”意味着改走真图检索，不意味着 `none`。具名作品、软件/产品、制作流程和真实案例也应先检查官方画面、界面、幕后图、过程拆解、实物或现场照片，而不是直接退成小图标与空卡片。若为 `none`，理由须说明真实检索后仍不可得且位图为何不增加听众价值，或为何会比图表/Canvas/排印更含糊；“CSS 更可控”“没有实拍”“担心 AI 出错”“为了风格统一”不是单独成立的 none 理由
- presentation：位图的**展示/背景处理合同**，取值**只能是四枚举之一**：`subject-only` | `framed-scene` | `full-bleed` | `evidence-crop`。**有位图页必填、无位图页完全省略这一行**（不要写 `无` / `none` / `not-applicable` 占位）。⛔ `split-media` / `right-half` / `cards` / `分屏` / `左右` 等是**版式/构图（layout/arch）**，绝不能写进 presentation；它们放到 `layout` 行。角色、产品或物件需要悬浮、跨色场叠放或作为独立元素时必须选 `subject-only`，并在素材 brief 写 `subject_only: true`；其他三种必须把原图背景作为有意的画面、满幅或证据边界，不能偶然露出矩形底色。（例：夜间阅读实拍用于左右分屏 → `presentation: framed-scene`，分屏本身写在 `layout`。）
- asset_id：每个位图机会写一个稳定 `asset_id`（英数/下划线，跨页唯一）；Image 完成后由编排器把 `asset_id → 实际路径 + origin + crop_contract` 回填到本行。无位图页不写。
- layout：区域比例、对齐、视觉重心
- spatial_budget：主焦点、文字、证据视觉分别占用哪些区域；剩余空间是呼吸、动线还是待消除的死白。`dense` 页的主信息不得只挤在半张画布或一条窄带，否则先改空间分配而不是留给 Slide 猜
- background_treatment：本页如何使用全册背景系统；先声明沿用 `base_canvas_family`、局部色场/环境光、主题肌理、图片背景还是有叙事理由的整页变体，再说明与前后页的颜色/明度/肌理承接。普通内容页优先保留基础画布，把章节差异放进局部大色场、图片调色、条带或母题状态。表达型场景若选择纯色页，应说明它承担呼吸、对比或换场，而不是因为没有继续设计背景
- bold_action：本页唯一的主要设计动作；普通内容页可写 none，特殊页与峰值页必须明确
- resolved_system_action：本页具体落实所选系统的哪一层关系（字体 / 色场 / 母题 / 图表 / 图片处理），不得只写系统 ID
- image：主体、用途、比例、实际路径与 `origin`（downloaded / generated / material / derived）；下载图保留来源 URL，用户附件保留原路径，派生图保留 parent asset（如需要）
- crop_contract（存在位图时）：`fit`（cover / contain / cutout）、`focal_point`、`protected_parts`、`allowed_crop` 与推荐 `object_position`。`protected_parts` 只列承担识别或语义的部分，如人脸/头顶/双手、完整产品轮廓、Logo、作品主体、图表坐标轴/图例；允许背景边缘有意出血，但不能用“满幅更有冲击力”解释主体残缺
- material_asset_type：attachment-image | figure-crop | page-facsimile（仅复用附件视觉时）。页面称为 `Figure/Fig./图 N` 时必须是 `figure-crop`；记录其 `source_pdf / source_page / visual_subject_box / render_source / pixel_size / body_text_fraction`，只裁视觉主体，不含论文正文和长图注；不得把 PDF 整页截图当 Figure。
- 用户围绕附件图片提出“根据这张图讲解/制作”时，或图片本身是唯一产品、人物、场地、作品、证据、原始流程总图时，至少安排一次可辨认的原图或忠实裁切上屏；复杂图可以先展示原图全貌，再用 Canvas/HTML 重绘局部。只有重复、无关、不可读或存在用户明确排除理由时才 omit，并写明理由。
- chart：数据、单位、时间、编码、结论（如需要）
- diagram：节点、关系、方向、层级（如需要）

## Reference route
- layout-patterns.md：<只列本页命中的章节>
- design-rules.md：<只列本页命中的主题节>
- quality-checklist.md：单页检查

## 来源
- <内部溯源字段，仅供讲稿引用与自查，**绝不上屏**；写屏显事实对应来源，无外部来源则写 user-provided / none。文件路径 / 章节锚点（如 `research/research.md §二.4`）只能留在此处，绝不作为页面 source 印出>

## 口语讲稿
<可直接朗读的完整口语段落，推进解释或过渡；不机械复读屏显，不朗读来源，不套 Markdown 代码围栏，不写内部路径、编排器假设或生产备注>
```

逐页计划是 Slide 的内容合同。只有 `## 最终屏显文案` 中通过内容充分性与观众价值检查的内容可以进入 HTML；`页面导演`、`视觉实现`、`Reference route`、`来源` 和 `口语讲稿` 都是非屏显区。内部 `attachment_priority_ids` 不上屏，但其指向的 `must_present` 实质内容必须出现在最终屏显文案、图表、Figure 或可见图解中；讲稿提及不算履约。屏显文案、数据、节点关系和素材路径必须定稿；Slide 可以调整 `.slide-body` 内的比例和排法，并可省略计划中语义完全重复的低价值屏显节点，但不得删除 `must_present` 内容、改事实、自创文案或增加计划外的大型装饰图。`视觉验收` 写观众能从像素读出的结果，不写“做得高级”“有科技感”这类审美愿望；流程/机制/方法/数据/media-led 页需明确语义对象、关系方向与领域证据，纯排印页可用一句话说明焦点与阅读顺序。冻结前先用 Style Lock 的字阶做一次版面预演：如果一页只有把听众需要阅读的文字压到 `--fs-min` 以下才放得下，应在规划阶段缩短屏显、重组层级或拆分职责，不能把“靠小字塞下”交给 Slide 解决。

## 3. 页型选择

先按内容职责选页型，再去 `layout-patterns.md` 目录定位对应章节：

| 内容职责 | 首选页型 / 硬元素 |
| --- | --- |
| 开场命题 | cover / hero；一个统治性焦点 |
| 章节切换 | transition；与其他特殊页共享设计 DNA |
| 时间演进 | timeline；时间轴与关键转折 |
| 指标结论 | KPI / hero number；单位、时间、对比口径 |
| 方案对比 | comparison；统一维度和清晰取舍 |
| 过程机制 | 静态关系优先 SVG，动态/自动布局用 Canvas + HTML，空间隐喻用无文字图片 + HTML；方向和关系可读 |
| 数据趋势 | ECharts；一图一结论、来源与单位齐全 |
| 场景 / 具名人物 / 地点 | media-led；真实图片承担识别与证据，而非只用姓名卡、小图标或生成的相似面孔 |
| 引述 | quote；原话、身份与出处 |
| 结论 / 行动 | closing / decision；低密度、明确下一步 |

相邻页面避免连续使用同一构图族。全册应有 anchor / dense / breathing 的起伏，但“丰富”不等于每页都堆满元素。

规划重复卡片时先比较单卡信息量与分配面积。若每卡只有图标、标题和一行说明，不要规划成顶部对齐的高大空框；优先使用更紧凑的卡高、横向信息带，或明确为“稀疏等高卡，内容组垂直居中、文字左对齐”。页面外框虽然铺满、但每个卡片/侧栏内部大面积空置，同样属于空间计划失败。只有单个数字或极短状态适合整卡水平居中。不要为了填满卡片补写无依据内容；优先加入本页原本就应出现的图片、界面、图表或解释视觉，或收紧容器并重组阅读路径。

## 4. 图片、图表与概念图 brief

- 图片：写清主体、真实身份是否重要、用途、期望比例、构图安全区、色调和禁止项；同时给出 `crop_contract`，说明焦点、必须完整保留的主体/图内信息、可裁背景和推荐 fit / object-position。若要把人物、产品或物件作为悬浮/拼贴元素，写明 `subject_only: true`，由 Image 生成并验收透明 PNG；拿到素材后回填实际路径、来源类型与 Image 确认后的裁切合同，不丢弃 `assets/catalog.json` 中的下载 URL、生成模型或派生关系。
- 多人介绍页把人物名单作为一个可共同审查的 `portrait-set` 素材组：逐人保留规范姓名与可核验来源，优先选择视觉口径相近的真实照片，并允许用可信团队/机构场景图替代“六张质量参差的小头像”。只取得部分人物时不得用无关或生成面孔补齐；应调整版式与人物层级，或在计划中明确缺口。
- 先写图片在这页承担的工作，再选择真实图或生成图。不得用“怕 AI 味”“风格难统一”“代码更可控”作为整册不配图的理由；没有信息或情绪价值的装饰图也不进入 brief。虚构阵容、概念体验、未建成场地和情绪主画面是生成图的正常用例，不应自动降级为方块、图标或抽象 CSS 占位。
- 图表：写清数据表、单位、时间范围、系列、排序、标注和这张图必须传达的新闻句；不得只写“做一个柱状图”。对表格/消融/排行榜等高风险数据，逐页计划必须保留可直接实现的 `类别 × 系列 × 值` 映射及原始页码，不能让 Slide 从摘要散文中再次猜列。若摘要中的数字与正文结论互相冲突，回到原始表格页解决后再冻结计划。
- 大型概念图：默认写成 Canvas 几何 + HTML 标签，或无文字图片 + HTML 标注；写清节点、层级、方向、关系和强调路径，并补一条可复述的视觉验收句。线段、虚线、弧线若表达流向、因果、反馈或先后，必须在计划中声明方向；不得把普通要点列表包装成无意义连线图。
- 科学、技术、医疗、工业或方法说明页若以“方法如何工作/产出什么证据”为职责，优先规划领域可辨认的真实或忠实简化输出，例如光谱、显微图、CT 截面、检测界面或样本—仪器—结果链。通用圆/矩形/六边形只能承担抽象层级，不得冒充领域证据。
- SVG：只为 icon、logo、箭头、标记和小装饰写 brief，不得规划成半屏/全屏主视觉；用户明确要求矢量交付或复用准确矢量资产时才例外。

## 5. 特殊页合同

封面与结束页通过 `bookend_system` 建立“开场—终幕”关系；全部过渡页通过 `divider_system` 建立同一章节语言。它们是跨页视觉合同，不是多页 Production group：封面、结束页与每张过渡页仍分别由单页 Agent 制作。各页共享字体角色、章节标记语法、标题锚点家族、色彩/图像处理和图形语法，但不复制同一几何。每张过渡页写明不同 `chapter_state`、构图重心和视觉动作；变化后仍应一眼看出属于同一套系统。只换章节号与标题、其余完全复制，或每页另起字体与语法，都不算完成过渡设计。

过渡页不承担正文解释，但必须有完整构图。计划应说明章节号、标题和一句承诺如何组成主信息团，图片、主题母题、色场、裁切或超大排印中的哪一个形成视觉对重，以及主要留白用于聚焦、转向、制造纵深还是连接下一章。若只能描述成“左上角放标题，其余保持空白”，说明构图尚未完成；应放大并重组主信息、引入与章节有关的视觉对重，或让图片/色场真正参与构图，而不是补卡片、堆正文或添加无意义装饰。

特殊页必须使用全画布构图，不套用普通内容页的标题—正文—页脚家具。背景、图片、色场和装饰可铺到画布边缘；信息文字仍在安全区。封面与结尾不显示大页码，过渡页只显示有叙事意义的章节编号。章节进度、短标签、托板或色带不是禁用元素，但必须属于该特殊页从计划阶段就成立的整体构图；不要为了修复一小段文字的对比度，临时加入横跨图片或画布的大面积页脚家具。局部可读性问题优先通过移动文字、改变局部明度、贴合文字的托板或定向渐隐解决，并在修改前后比较主视觉完整性、视觉重心与阅读路径。

特殊页上的辅助文字执行“信息唯一性”检查：同一主题、作品名、章节标签、年份或场合不得在 kicker、技术注、元信息条和页脚中重复出现。技术注、坐标、场记、档案号、`SCENE 00 / COVER`、`END / NN`、`A VISUAL ESSAY` 等只有在它们是真实、准确且对听众有解释价值时才可使用；不能为了画册感、电影感或研究感编造。封面元信息只保留用户提供且观众确实需要的姓名、机构、日期或场合，页数、制作状态和内部时长默认不显示。装饰母题应由形状、裁切、色场、材质或排印承担，不靠堆叠伪标签。

结尾不是“最后一张总结内容页”。默认只保留一个收束主张或行动邀请、至多一条短支撑句和一个视觉锚点；三条以上 takeaway、详细回顾、来源和说明应放在前一张内容页。没有明确的非对称 Hero、图片或图形配重时，计划应让这组核心信息在安全画布中水平、垂直光学居中，而不是把信息钉在上半部、再用底部卡片或页脚填空。若有意采用非居中构图，必须写清视觉配重和视线终点，使不对称看起来是设计决定，而不是居中失败。

参考文献与结尾必须分开：来源需要屏显时使用独立 references 页，或在前一张内容页以克制形式呈现；不得把数十条来源、详细总结、三栏回顾与 closing 合并成高密度终页。

播放层统一使用播放器的克制 crossfade，不为各页随机配置飞入、旋转或缩放切换。章节转场主要由静态首帧的色场、构图阈值和视觉状态表达；离开动画也必须成立。

结束页必须回扣核心命题或明确行动，不使用“感谢聆听 / Thank you for listening”等套话，也不显示页码、进度、页脚家具或 `END` 等无叙事作用的运行标记。

## 6. 规划门核

冻结前逐项确认：

- 所有事实来自 user query、`grounded-knowledge.md` 或明确标注的示意；
- 页数、页序、标题和 speech 页码一一对应；
- 每页一个主焦点，屏显文案不依赖讲稿才能理解；
- 所有附件 `screen_priority: must_present` 均有 `attachment_priority_ids → 页面 → 可见载体` 映射，且重点内容真实进入屏显文案、图表、Figure 或图解；没有任何重点只存在于讲稿；
- 内容充分性与 screen-copy firewall 已通过：普通内容页有不可替代的听众所得和相应支撑层；非屏显区没有泄漏进 HTML；每个可见文本节点都有独立的观众价值，同一信息没有在标题、kicker / subtitle、图片角标、badge、callout、图例、技术注、元信息和页脚中重复；
- `plan/deck.md` 逐页表与 `slide_NN.md` 的页型、媒介和视觉职责一致；不得一处写 Canvas、另一处写纯表格/diagram none；
- 每页视觉验收可由最终像素直接判断；流程和关系图的对象、方向、图例与标题不自相矛盾，领域页的证据形态足够具体；
- 所有图表都有数据/单位/时间/来源；多系列图的类别数、每个系列的数据项数与图例语义一致，消融项不会因数组切片或类别不足而丢失；所有图片都有可执行 brief 或实际路径，承担识别/证据/主视觉职责的图片另有可执行 `crop_contract`；
- 每页都完成配图机会判断；有有效机会的页面已进入 Image 路由，纯代码视觉页写明其媒介优势；
- 背景处理来自同一 `background_system`：普通内容页共享基础画布家族；整页变体有用途、有进入/退出承接，不会突然形成数页“另一套 Deck”再无过渡切回；没有随机换色、无主题 glow 或通篇默认深藏青。表达型场景若 overview 仍退化为全册同一纯色底，已重新判断是否遗漏了主题环境光、肌理、色场、图片或生成背景；
- 所有页面都有 page-type 与 Reference route；
- Production groups 覆盖全部页面且无重复归属；封面、每张 divider、closing、hero、复杂图解和独立重制作页均为单页 group；普通内容组不超过 3 页，并写清 `why_grouped / anti_repetition_delta / boundary_handoff`；
- 普通内容组只合并真正同构的制作任务，没有仅按章节把真实照片、SVG/Canvas、图表和 cards 粗暴打包，也没有把连续左右分屏仅靠左右互换视为变化；
- `Repetition & rhythm preflight` 已完成：相邻页无机械复刻，各章没有照抄同一套页面脚本，母题有主次与缺席，画布状态和密度形成叙事起伏；
- 特殊页有亲缘性，普通页有足够构图变化；
- 封面和结尾没有页码、页脚家具、制作状态或伪场记；特殊页没有用重复英文标签和无意义技术注冒充设计层；
- references 与 closing 分页承担职责，结尾保持低密度收束；
- 每张过渡页都能说明主信息团、视觉对重和留白职责；没有文字缩成局部小块、其余画布既无视觉张力也无叙事用途的“空壳章节页”；
- base.css token 与 Style Lock 一致，没有通用安全模板回退；
- Style Lock 记录一套完整 `resolved_system_id` 的读取与转译，`typography_recipe / palette_recipe` 字段齐全；学术/严谨场景仍有明确字阶、证据视觉、构图动作和节奏，没有退化成文档式白底卡片墙；
- 不存在占位符、未解析路径、待定数字或“后续补图”等制作说明。
