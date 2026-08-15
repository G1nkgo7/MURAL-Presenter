---
name: mural-presenter
description: 创建或编辑完整的 HTML 幻灯片 deck；支持主题、提纲、文档和多附件输入，生成每页独立 HTML（1600×900）、渲染图、逐页讲稿与播放器。新建时编排 Research、Material、Image、Slide、Review；编辑现有 PPT 时按复杂度选择单 Review 快修或 Orchestrator 多 Agent 改造。适用于制作、改稿、续编、重排、统一风格或审校 PPT、deck、slides、presentation。
---

# MURAL Presenter

把本文件当作**路线图**，不要当作需要一次背完的规范全集。先判断任务模式，再只读取该路径要求的 reference 和职责卡。

运行时会按 query 的主要语言自动选择入口：中文读本文件和 `subagents/<role>.md`，英文读 `SKILL.en.md` 和 `subagents/<role>.en.md`。路由只决定 Agent 的工作语言；PPT 屏显与讲稿仍由用户的交付语言要求决定，不得用入口语言推断。

## 0. 先选择任务模式

| 用户目标 | 模式 | 执行入口 |
| --- | --- | --- |
| 从主题、brief、附件创建一套新 deck | 新建 | 走「2. 新建 PPT」 |
| 修改现有 deck，且只影响少量页面/局部表现 | 简单编辑 | 走「3.2 Review 快修」 |
| 修改会改变叙事、事实、素材、全局风格或多页结构 | 复杂编辑 | 走「3.3 Orchestrator 改造」 |

判断不清时先做只读影响分析。**不因用户说“简单改一下”就忽略实际影响范围，也不因改动页数少就把叙事级变化当成简单编辑。**

## 1. 所有模式共享的合同

### 1.1 Orchestrator 的职责与边界

Orchestrator 只负责：**判断模式、规划、委派、合并、验收和确定性收尾**。

- 可写：`plan/`、`base.css`、知识汇总和构建产物。
- 不直接写：`slides/slide_NN.html`；页面由 Slide 或 Review 修改。
- 不伪装工具能力，不把计划动作写成已完成动作。
- 每个 subagent 必须有显式 label；失败、超时或未自然收尾的结果不得当成完成品。
- `delegate_task` 返回的结构化 contract、artifact paths 与 `handoff_path` 是父级交接真相；Orchestrator 不读取子 Agent 的 `messages.json`、`tool_log.json` 或 system/tool 快照来轮询进度。Research 是任务级单例；Review 最多执行 3 次，后续实例只能用于修复后的受控复验，不能为了审美偏好反复重审。

### 1.2 角色

| 角色 | 数量与时机 | 唯一职责 |
| --- | --- | --- |
| Research | 至多 1 个 | 核验会改变结论的外部事实，写 `research/research.md` |
| Material | 按附件并行 | 每个实例只处理自己的附件分片，写 `research/materials/material_NN.md` |
| Image | 按素材量并行 | 获取或生成位图素材，返回实际路径 |
| Slide | 特殊/复杂页按单页、普通同构页按小组并行 | 只制作/重做自己的页面所有权并完成逐页像素闭环 |
| Review | 首次验收 1 个，修复后最多复验 2 次；简单编辑时也是执行者 | 先诊断、后集中修复、批量重渲和最终讲稿收口 |

所有角色开工前完整读取自己的 `subagents/<role>.md`。任何选中的文件或章节出现截断提示时，续读到结束；**未被路由命中的 reference 不读**。

### 1.3 语言与能力

开始前锁定：

- `response_language`：过程与最终回复语言；
- `deliverable_language`：屏显、规划和讲稿语言；
- attachments、web、真实图片获取、图片生成、渲染、vision、写文件等能力状态。

默认采用用户 query 的主要语言；用户明确指定交付语言时单独覆盖。只能规划实际可用的能力。
`vision_analyze` 由当前角色所用的同一个模型直接查看像素；视觉判断、问题账本和看图后的回复使用 `response_language`，不得因模型默认语言切换，屏显原文与专有名词可保留原语言。

### 1.4 工作区真相源

```text
research/research.md
research/materials/material_NN.md
plan/grounded-knowledge.md
plan/design-brief.md
plan/deck.md
plan/slide_NN.md
base.css
assets/
slides/slide_NN.html
renders/slide_NN.png
speech.md
present.html
```

- `grounded-knowledge.md` 是事实真相源；
- `design-brief.md` 是视觉真相源；
- `slide_NN.md` 是页面内容合同；
- `base.css` 是全局设计系统；
- 最终判断必须基于最新 PNG，而不是只看 HTML。

### 1.5 Reference 路由

| 场景 | 读取 |
| --- | --- |
| 场景定调 | `design-rules.md` §T1–T3、§1–3 + 命中的主题节；完整读取 `aesthetic-recipes.md`；`design-styles.md` 目录 + 一个风格群组 + 一套完整 `S1–S13 resolved system` |
| 全局与逐页规划 | `planning-contract.md`；先看 `layout-patterns.md` 目录，再读每页对应页型；复杂图解另完整读取 §9 的媒介与命中 archetype |
| 编辑现有 deck | `editing-contract.md` |
| Slide | 自己的逐页计划、`base.css`、`quality-checklist.md`“一、单页检查”与本页命中章节 |
| Review | 完整读取 `quality-checklist.md`：先用“一、单页检查”核对视觉语义，再做“二、整套检查”和“三、可机核 lint 项” |

字体只在默认角色不足或场合敏感时读 `fonts.md`。不扫描全部风格与版式库，但**不能跳过** `aesthetic-recipes.md` 和命中的完整 resolved system；“渐进式读取”用于减少无关分支，不得把艺术指导压缩成一个风格名与一个颜色。

无论是否读取 `fonts.md`，中文眉签、部门名、页脚、来源和元数据都不得使用等宽字体或拉丁 ALL CAPS 的疏字距：使用 `--font-sans` / `--font-serif`，字距保持 `0–0.03em`。`--font-mono`、`--tracking-caps`、`.is-latin-label` 只用于纯拉丁技术标识、代码、坐标和真实编号。

同一句中文标题、结论或标签必须使用同一字体家族；强调词只通过颜色、字重、字号或下划线建立层级，禁止把其中几个字换成卡通、手写或另一套展示字体。字体角色按场景收放并全册统一：严谨型 1–2 族，常规演讲 2–3 族，明确表达型演讲可用 3–4 族；同一角色不因页面题材临时换字体，第 4 族只能承担明确点缀。默认按“投影演讲”路由：普通演讲的短标题使用 `--font-title` / `--font-hei-heavy` 的 Smiley Sans 首选栈，正文保持 Noto Sans SC；正式/严谨场景在 Style Lock 中改回 Noto Sans/Serif SC。中文正文/表格/页脚/眉签一律 `--font-sans`（严谨衬线可 `--font-serif`），**中文绝不放进 `--font-mono`**；`--font-mono` 只服务纯拉丁代码、坐标、ID。除默认短标题中的 Smiley Sans 外，`playful`、圆趣、手写和书法字体只有在主题与受众真正支持时启用，并在对应元素加 `.is-expressive-type` 或 `data-type-intent="expressive"`。

### 1.6 视觉媒介优先级

按内容选择媒介：

1. 真实人物、地点、产品、事件：真实照片；
2. 氛围、隐喻、故事场景、视觉主画面：生成图或高质量位图；
3. 数据：ECharts；
4. 大型流程、架构、机制、关系示意：静态且关系明确时优先使用 `layout-patterns.md` §9 的 SVG；动态计算、自动布局或大量长标签用 **Canvas 几何 + HTML 文字层**，空间隐喻用“无文字图片 + HTML 标注”；
5. SVG：既可用于 icon、logo 与装饰，也可作为大型静态概念图主媒介；使用 `svg-diagram`、贴合内容的 viewBox、CSS token、明确方向和可读标签，不受五个 archetype 白名单限制。

普通内容页只要存在人物、地点、产品、作品、活动、体验、自然/城市环境、故事场景或情绪画面等可见主体，优先让一张有分量的真实/生成位图成为主视觉，而不是用彩色块、图标或抽象线框替代。配图数量服从叙事，不机械凑数；但能够增加识别、证据、临场感或情绪价值的位图机会不得静默放弃。

**不禁用大型 SVG。**静态结构该用就用，优先复用 `layout-patterns.md` §9 的网格、坐标与标签护栏，也允许按 case 设计新结构；复杂动态图、自动布局和空间隐喻再走 Canvas/图片 + HTML。真实人物、产品、论文 Figure、实验影像和场景能直接配图时，图片优先于 SVG 重画。详细实现见 `design-rules.md` §5。

## 2. 新建 PPT

### 阶段 0：解析任务

1. 锁定语言、能力、附件清单和交付范围。
2. 建立场景卡：Speaker、Audience、Occasion、Objective、Duration、Page count、Screen vs speech、Core takeaway、Assumptions。场景卡只用于内部规划，不是屏显文案来源。
3. 不向用户追问非阻塞偏好；在规划中显式记录合理假设。

### 阶段 1：按需接地

#### Material

有附件时，按附件拆成互不重叠的 Material 分片并行执行；默认一个附件一个分片。每个 goal 给出：

- `response_language` 与 `deliverable_language`；
- `assignment_id`；
- 确切附件路径；
- 独立目录 `materials/_work/material_NN/`；
- 独立输出 `research/materials/material_NN.md`。

所有格式都先走 Material 角色卡中的统一 `stage_materials.py` 入口：文本读全文，图片真实看图，PDF/Office 同时保留文本与页面/内嵌图视觉；音视频、压缩包或未知格式按 catalog 的建议动作使用环境已有转换能力。不能将文件元数据、压缩包成员名或媒体代表帧冒充语义内容。

全部返回后逐项核对 catalog：每个附件必须有唯一 `coverage_id`，状态为 `ok`，coverage 为 `complete`，文本 chunk 区间连续覆盖全文或扫描页覆盖全部页；各分片摘要的 Coverage ledger 必须包含对应 `coverage_id`。任何 `semantic_coverage: incomplete`、`truncated`、`unsupported`、`incomplete`、`failed` 或 `missing` 都阻塞下游，不得把非空摘要、元数据或代表帧视为读完材料。

每份 Material 摘要还必须写 `priority_ledger: complete`，并给附件中的每项重要结论、关键数字/关系、必须辨认的 Figure/产品/人物/流程分配稳定 `priority_id` 与 `screen_priority: must_present | supporting | speech_only`。`must_present` 表示听众若只看页面也必须获得，后续不能因为讲稿已解释而省略；`speech_only` 只允许背景、例证和口头展开，不能承载附件的主结论、关键证据、用户明确点名内容或决策所需信息。

#### Research

只有外部事实会改变结论时才派唯一 Research。具名真实产品、临床/经营统计、外部基准和会承担结论的具体数字都属于需要核验的外部事实，除非已经由用户或附件提供。goal 使用：

```text
Research:
Response language: <response_language>
Deliverable language: <deliverable_language>
Raw user query (verbatim): <原文>
Unresolved terms: <待核对象或 none>
Evidence needed: <会改变结论的缺口>
Parent interpretations are hypotheses, not user claims.
```

`Raw user query (verbatim)` 必须逐字复制完整用户消息，不摘要、不改写、不补充标点，也不能省略视觉要求。`Unresolved terms` 与 `Evidence needed` 可以加入编排器认为值得核验的候选，但新增项必须明确标为 `orchestrator hypothesis`，不得伪装成用户已声明的日期、数字、人物、地点或观点。只有某句话能在 Raw user query 或 Material 原文中逐字定位时，才允许称为“用户要求 / 用户 brief / 用户原文”；否则只能称为“委派假设”或“核验候选”。

Research 在一个工具回合并行提交首轮独立查询，在下一工具回合并行抽取最佳来源，最多再做一轮定向补搜。

#### Grounding gate

Material / Research 回收后，Orchestrator 的下一项动作必须是写唯一 `plan/grounded-knowledge.md`，随后用 `read_file` 验证文件存在且内容完整；完成前不得进入 `design-brief.md`、Style Lock 或逐页规划。文件区分用户事实、外部核验、编排器假设、示意、冲突和未确认项，不添加无来源的新事实；有附件时合并一份 `## Attachment priority ledger`，逐项保留 Material 的 `priority_id / screen_priority / source_locator / fidelity_form`。Research 若返回 `partial`，必须把合同中的 `unresolved` 原样写入 `## 未解决与使用边界`，并说明相关命题不得作为确定结论上屏；未传播该边界即视为 Grounding 未完成。Research 若把委派假设误称为用户原话，合并时必须按 Raw user query 纠正归因，不能把“假设被否定”写成“更正用户”。

附件提供的是**事实与候选视觉证据边界，不是默认设计上限，也不是图片配额**。合并时整理材料里的主结果 Figure、可复用图片、图表结构和品牌线索；随后在 `design-brief.md` 明确 `material_visual_mode`：`facts-only`、`visual-reuse`、`style-reference` 或用户明确要求的 `faithful-restyle`。对附件视觉逐项选择 must-show / reuse / reference-only / omit：只有用户点名、身份/结果不可替代或确实是最佳证据时才 must-show，不要求把附件里的图全部塞进 deck；低清、文字密集、重复或不适合投影的图可以 omit，并为页面另取真图、生成图、SVG/Canvas 或 ECharts。论文整页视觉与页内 Figure 必须区分为 `page-facsimile` 和 `figure-crop`。除 `faithful-restyle` 外，不继承附件的小字号、密集表格、普通文档排版或低质量视觉；课件、组会、学术答辩和论文解读仍按演讲场景重新定调。

### 阶段 2：场景定调与 Style Lock

1. 按 Reference 路由读取视觉规则：完整读 `aesthetic-recipes.md`，在 `design-styles.md` 中读目录、命中的一个风格群组，并从标题到下一标题**完整读一套** `S1–S13 resolved system`。不扫描无关系统，也不得只读名称、主色或摘要。
2. 先锁定 `scene_register`（庄重汇报 / 编辑叙事 / 产品发布 / 教学解释 / 文化体验等）、一个明确主风格和一个 `resolved_system_id`。风格必须能解释“为什么适合这个受众、场合与内容”，不能只写抽象形容词，也不要把多个风格编号拼成折中套餐。多个系统同样合题时，优先选择能形成更清楚标题声部、招牌母题、图表语言和节奏的高设计完成度系统，而不是最安全的白底卡片方案。允许借一种辅助 craft，但整册要能用一句视觉主张说清。
3. 写 `plan/design-brief.md#Style Lock`：
   - scene；
   - primary_style；
   - `design_ambition`：默认 `high`；用户明确要求朴素、法规式或极简时才用 `restrained`；
   - `resolved_system_id / resolved_system_read: complete`，并写 `kept_layers / translated_signature / discarded_cliches`，证明读的是整套底、墨、线、accent、字体、母题与图表关系，而不是一个颜色；
   - supporting_craft（最多一种）；
   - visual_thesis / signature_visual；
   - `palette_recipe / typography_recipe`：逐字段落实 `aesthetic-recipes.md`；字体合同必须显式写 `title_voice`、`title_scale`、`title_treatment`、`body_voice`、`numeric_voice`、`font_roles` 与 `type_event_map`，调色板必须写中性层、主次 accent、语义色、效果层和对比计划。普通演讲默认让短标题与正文形成明显字体和尺度反差；只有正式/严谨场景可让标题、正文收敛到同一 Noto 家族；
   - image_language / image_opportunity_map / composition_grammar；
   - background_system：先根据场景说明背景应偏“克制秩序”还是“氛围表达”，再定义一个贯穿内容页的 `base_canvas_family` 与允许变化的视觉状态（明度、色场、环境光、肌理、图片占比、密度和章节状态）；每种状态写清叙事用途、适用页面及进入/退出承接。学术、组会、合规、严肃评审等场景可以更安静，但仍需有排版和证据视觉；其他场景不要把整册同一纯色底当作安全默认。统一不等于全册同底色；变化也不能脱离同一画布家族；
   - motif_role：说明主题母题在哪些页作为主视觉、在哪些页只作次要线索、哪些页主动缺席。同一装饰母题不得承担封面、章节页和大多数内容页的主要视觉；一致性主要来自字体、颜色语义、图片处理和构图语法。技术注、坐标、场记、档案编号等只有在传递真实且有用的信息时才可成为母题，不能编造伪元数据营造“高级感”；
   - special_pages；
   - avoid；
   - spatial_rhythm：内容页如何铺开、呼吸页如何聚焦、峰值页在哪里；
   - special_page_system：封面、章节页、结尾页共享什么设计 DNA，各自用什么构图动作。
   - material_visual_mode（有附件时）：哪些只作为事实，哪些图片/图表可直接复用，哪些风格线索值得保留。
   - attachment_visual_map（有图片附件时）：原路径、must-show / reuse / reference-only / omit、`material_asset_type`、正式 asset 路径、上屏页、裁切/整图/抠图/调色与理由。论文 `Figure N` 必须记录 figure-crop 的来源页与边界，不能直接复用整页 PDF PNG。
4. 用户未指定风格时，按主题 × 受众 × 场合主动判断并保持 `design_ambition: high`。课件、组会、学术答辩、论文解读、科研、政策与正式汇报的高设计感来自编辑出版、证据展陈、科学可视化、字阶、非对称秩序和章节节奏；“严谨”只收敛装饰，不降低版式、媒介与视觉完成度，不得退成白底小标题、普通左右分栏和卡片墙。没有 Style Lock 不进入规划；没有完整 resolved system 的转译证据和可见的 `signature_visual` 兑现页，也不把通用配色和字体清单当作完成定调。

`image_language` 先说明哪些颜色本身承担识别、证据或教学信息，再决定统一处理。人物、动物、植物、作品、产品、场地、实验输出等真实主体默认保留有意义的原始色彩；统一感优先来自选图、裁切、色温、局部色罩、边框与背景。只有用户明确要求黑白/双色调，或本册视觉主张确实依赖该处理且不会损害辨认与证据价值时，才使用整图灰阶或 duotone；“学术感”“高级感”“为了统一”本身不构成把整册真实图片去色的理由。对承担识别、证据或主视觉职责的图片，同时定义轻量 `crop_contract`：焦点、必须保留的主体部位/图内信息、允许裁掉的背景与推荐 fit；不能只写宽高比后让 Slide 猜裁切。

Style Lock 锁定的是**视觉语言与判断边界**，不是一套固定 HTML 模板，也不锁死每页几何。必须明确区分：

- 稳定语言：字体角色、颜色语义、间距节奏、背景语法、图片裁切/调色、图形语法与特殊页亲缘关系；
- 受控变化：每页主焦点、构图方向、媒介比例、信息密度、留白位置和章节状态；
- 禁止项：临时引入新字体、新配色、无主题装饰或复制上一页几何只换文案。

它应当像可执行的 Art Direction：足够具体，使不同 Slide 能做出同一世界里的页面；又保留足够空间，让每页按内容选择最佳构图。无需另建模板文件或共享装饰素材。

`image_opportunity_map` 必须先做一次与实现方式无关的“可见主体扫描”：这页有没有值得被看见的人物、场地、产品、作品、活动、体验场景、虚构角色或情绪主画面；先说明图片能增加的证据、识别、临场感或情绪价值，再选择真图、生成图或代码视觉。不得先偏爱 CSS/Canvas，再倒推“没有图片机会”。

当页面的核心内容是一组**具名真实人物、主创、嘉宾或团队成员**时，默认把“人物可识别”视为真实图片机会，并交给 Image 批量检索人物肖像、官方简介照、活动照或团队合影。“不应生成假真人”只意味着不能用生成肖像冒充本人，不能据此把该页改判为 `image_opportunity: none`。若只能可靠取得部分人物照片，优先采用一张可信团队/机构场景图配少量关键人物肖像，或降低人物数量并重组叙事；不得用身份不明的相似面孔补齐。只有经过真实检索仍无可辨认、可下载且适合上屏的素材，且图片不会增加识别价值时，才使用纯排印，并在计划中记录缺口与降级理由。

同样审视具名作品、软件/产品、制作流程与案例：可检索的官方画面、界面、幕后制作图、过程拆解、实物或现场照片通常比小图标和空卡片更能建立识别与可信度。每张普通内容页都应有一个与内容相称的主要视觉载体——真实/生成图片、图表、解释性 Canvas，或真正能独立成立的排印主视觉。边框、空面板、微型图标和装饰线不算主要视觉载体；纯排印只有在文字本身被有意放大、组织并形成明确焦点时才成立。增加配图不等于增加散点：优先一张有分量的主图或一组视觉口径一致的素材，让其他元素安静地服务它。

有附件时同样执行完整扫描：优先复用其中真正有信息价值且清晰的图片；附件存在图片、内嵌媒体或 PDF 页视觉时，整册 `bitmap_exception` 属于需要复核的异常并由交付审计记录警告。附件没有实景、人物或品牌图，只说明“没有附件真图”，不等于“生成图会虚构所以禁止配图”。用于气氛、愿景、概念体验或非特定场景的生成图可以作为表达层使用，准确事实、数字和关系仍留在 HTML / 图表层。把事实保真与视觉想象分开，不把材料摘要机械搬成卡片墙。

附件图片既不能全部默认消失，也不能全部机械上屏。用户明确点名，或图片本身是不可替代的产品/人物/场地/作品/主结果 Figure/前后对比/总览证据时标为 `must-show`；其余按清晰度、信息价值、投影可读性和页面职责选择 reuse / reference-only / omit。附件图低清、含大段论文正文、与叙事重复或不适合投影时，应另取更合适的真实/生成图片，或用 SVG/Canvas/ECharts 重构关系；附件中的事实仍保持接地。复杂流程图可以“原图总览一次 + 后续分步图解”，但不要求展示每个 Figure。`image_opportunity: none` 只表示不新增外部/生成位图，不能覆盖真正的 attachment must-show。

真实对象的识别与证据、场景的临场感、人物与产品的可信度、故事与情绪的锚点，都属于有效配图机会。虚构人物、概念场景、未建成空间和风格化主视觉正是生成图的适用对象，不应因其不是真实对象而改用彩色方块、抽象符号或纯 CSS 占位。“CSS 更可控”“没有用户实拍”“担心 AI 生成错误”“为了风格统一”都不能单独成为 `none` 的理由；这些问题应通过真图/生成图分流、提示词约束、统一裁切与调色解决。只有当位图确实不增加听众价值，或会比图表、Canvas 或排印更含糊时，才选择 `none`。如果一册存在多个明显可见主体，却被整体判成无位图或仅封面一张图，在冻结计划前必须重做这次扫描。这里不设图片数量配额，也不为装饰而配图。

当搜图或生图能力可用时，**整册全部 `none` 或只有封面一张图属于需要证明的异常，不是默认安全路线**。数据、商业、技术、学术或代码题材也不能因此整册退回卡片墙：事实页可以用图表/Canvas，但封面、章节转场、案例、场景、愿景或结论中至少应选择两个真正能从图像获益的节点，给出可执行的搜索/生成 brief；短册则至少保证一个内容节点，而不只是封面。只有用户明确要求纯排印/纯图表，或逐页证明位图都会降低准确性与可读性时，才允许整册无位图，并在 `plan/deck.md` 写明逐页例外理由。这是防止误判的最低覆盖线，不是为了凑数；事实型真图与非证据性的氛围生成图必须明确分流。卡片、极简、学术、商务等风格描述不等于禁止位图，也不能作为免配图理由。只有用户原文明确要求“纯文字”“不要图片”或语义完全等价的限制时，才能把 `explicit_user_request` 作为图片豁免依据；不得根据风格标签自行推断用户拒绝图片。

可见主体扫描必须同步落盘为 `plan/image-strategy.json`，供生成流程在 Slide 委派前确定性验收。存在配图机会时写入 `status: images_required`、`visible_subject_scan_complete: true` 和完整的 `image_opportunity_pages`；整册无位图时必须写入 `status: bitmap_exception`、`visible_subject_scan_complete: true`、覆盖全部计划页的 `reviewed_pages`、不少于 20 字的 `exception_reason`，以及 `explicit_user_request | pure_typography | pure_chart | wireframe | accuracy_critical` 之一的 `exception_basis`。不能用自然语言总结替代该文件。

背景不等于一块纯色，也不等于每页随机换皮。学术、组会、合规、严肃评审等场景可用安静画布承托事实；产品、品牌、招商、文旅、文化、故事、课程导入、活动与大众传播等表达型场景，应主动考虑一层与主题相容的环境设计，而不是整册退回纯色：可以是有方向的柔和光场、局部光晕、低对比颗粒/网点/纸纹/地形等主题肌理、图片背景，或由 Image 统一生成的背景。光晕只有在能解释光源、主题和视觉焦点，且形状、位置与构图相关时才成立；标题后反射式复制的圆形模糊光斑仍属于无主题 glow。先确定贯穿普通内容页的基础画布家族，再选择少量相容手法形成背景语法。章节差异优先通过局部大色场、图片调色、条带或母题状态表达；只有章节页、hero、结尾或叙事确需整体换场时才更换整页画布，并在前一张或后一张保留颜色、肌理、图片处理或构图方向的承接。图片或生成背景必须进入 `image_opportunity_map` 与素材 brief，不能由 Slide 临时发明路径。避免出现数页突然像另一套 Deck、随后又无过渡切回，也避免把深藏青、霓虹蓝紫渐变或通用科技 glow 当作默认“高级感”。

后续主链只有一条：`Style Lock → 全册计划 + prepare → Image 分片并行 → 素材路径一次回填 → Slide 页组并行 → Review 诊断/有限返修 → 讲稿同步 + build → Review 查看 build 后最终像素并返回合同`。前一节点的真相源未冻结，不启动依赖它的下游；互不依赖的同层任务一次并行派出。`build` 可能裁剪字体、更新 `base.css` 并重渲全册，因此 build 前的 Vision 只能用于诊断，不能作为最终像素证据。

### 阶段 3：全局规划与字体前置

完整读取 `references/planning-contract.md`，然后按顺序：

若存在 `materials/font-config.json`，先读取一次并把其中 title/body/number/annotation 角色作为 Style Lock 的字体输入；用户上传字体优先于自动选型，未覆盖字符由交付字体包自动回退，禁止凭字体名改用未上传的本机字体。

1. 补全 `design-brief.md`；
2. 写 `plan/deck.md`；
3. 复制 `base-template.css` 为 `base.css` 并填写 token；
4. 一次写完全部 `plan/slide_NN.md`，每页附自己的 Reference route；
5. 在 `plan/deck.md` 定义 Production groups，采用**原子页优先**路由：封面、每一张章节/过渡页、结尾、hero/视觉峰值页、复杂图解页（SVG / Canvas / 图片式机制图）、独立高密图表与重图像合成页，各自建立单页 group，保证独立设计注意力。只有制作方式、信息结构和视觉语法真正同构的普通内容页才可组成小组，默认 2 页、最多 3 页；每组必须写 `why_grouped` 与逐页 `anti_repetition_delta`，明确焦点、方向、媒介占比、标题宽度或阅读动作至少一项不同。不得把连续章节、同样白底、都用左右分屏或“可以共享 CSS”当作分组理由。每组同时写 `boundary_handoff`，说明进入本组前与离开本组后的画布、明度、色场和母题状态；分组完成后按逐页表复核一次，确保每页恰好归属一个组。
6. 参考文献与结尾页分开承担职责：需要上屏的来源使用独立 references 页或前置内容页；closing 只负责收束命题、行动或提问，不与长参考文献、详细回顾或多栏总结合并。
7. 在启动 Image 或 Slide 前写一段简短的 `## Repetition & rhythm preflight`：逐页比较画布状态、标题锚点、构图方向、媒介、图片占比、信息密度与母题角色；同时纵向比较各章的页面脚本，不能把同一套“痛点—案例前—案例后—步骤—工具”机械复制到不同章节。共享节奏可以形成亲缘性，但每章仍应有自己的问题视角、证据任务与阅读动作；某页没有独立职责时合并或重构。发现重复或节奏扁平时先改页面地图、Style Lock 或 Production groups，再冻结计划。
8. 做一次**附件重点映射、内容充分性与屏显语义去重**：先把每个 `screen_priority: must_present` 映射到且只映射到至少一个逐页计划，在该页写 `attachment_priority_ids`，并把对应结论、数字、关系或证据真正放入 `## 最终屏显文案`、图表、Figure 或可见图解；讲稿只能解释和展开，不能作为映射终点。随后让每个普通内容页写清不可替代的听众所得，再用最适合该页的证据、机制、对比、案例、行动或边界继续解释；不设固定条数，但只有主题句、同义副题和状态角标的页面不算内容成立。若没有新的支撑层，合并页面、改变叙事职责或改成真正有单一焦点的过渡/呼吸页，不用大片无职责空白或重复标签把薄内容拉成一页。逐页确认主要视觉载体与 `spatial_budget` 相符；不能靠大边框、等高卡或空面板在几何上“占满”，却把短文字钉在边缘、留下大块未参与阅读的内部空白。逐页比较标题、kicker / subtitle、图片角标、badge、callout、图例和页脚；同一短语通常只选择一个最强载体，其他区域补充对象、原因、变化或结果。只有导航或同屏比较确有必要时才重复，且每次出现必须承担不同作用。`dense` 不是一句标签：若主内容只压在半张画布或一条窄带里、其余空间没有焦点或方向，必须重做空间计划。
9. 做一次 `screen-copy firewall`：逐页区分“观众必须看到”与“只供生产使用”。Speaker/Audience/Occasion/Objective、页面职责、production group、视觉验收、素材路线、内部 `priority_id`、证据编号、假设、文件名和 Research/Material 来源路径都留在计划或讲稿中，不得自动进入 `## 最终屏显文案`；但 `priority_id` 指向的 **must_present 实质内容**必须以听众可理解的文案或视觉证据上屏，不能连同内部编号一起被 firewall 删除。只有当页面主题本身确实讨论目标受众、项目目标或研究方法时，才把相关内容重新写成观众可理解的叙事，而不是显示 `受众：…`、`主体：…`、`页面角色：…` 等内部标签。屏显文案和 HTML 不使用 emoji / Unicode 图标（如 `👀 ✋ 💡 ✨ ★ ✦`）；需要图标时使用与 Style Lock 一致的本地小 SVG、CSS 形状或直接用文字表达。星芒、爱心、礼花等通用装饰不能作为“全册点缀”散布到多数页面，只在确有构图职责的页面出现。
10. 用一个确定性命令同步讲稿并从计划前置字体包：

```bash
python ${SKILL_DIR:-skills/mural-presenter}/scripts/deck.py prepare . --expected <总页数>
```

规划冻结条件：事实、页序、屏显文案、视觉媒介、逐页配图机会、素材 brief、背景处理、来源、讲稿、页型和字体全部确定，`plan/image-strategy.json` 已写入，并已通过内容充分性、屏显语义去重与 screen-copy firewall。冻结前专门反证所有 `image_opportunity: none`：若页面已经有可视化的主体或场景，不能只用“代码更可控”将它排除。屏显文案或字体 token 变化时，先同步计划再重跑 `deck.py prepare`。

逐页计划还必须完成一次空间预演与视觉验收预演：写清主视觉与文字各占哪块、主信息如何使用安全区、剩余空间为什么存在，以及观众从最终像素应读出哪些对象、方向、领域证据和结论。`deck.md` 与逐页计划的媒介不能互相矛盾。普通内容页若预演结果是“主体缩在中间、外围大片无归属空白”“只能靠小字塞下”或“只能用通用几何代替领域证据”，先改计划，不把问题留给 Slide。章节过渡页则预演“主信息团 + 视觉对重 + 留白职责”：内容保持简洁，但不能只在局部放一小团文字、让其余画布成为未设计的纯空白。

### 阶段 4：素材与页面制作

1. 汇总所有被判定为真实图或生成图的图片 brief，再启动 Image subagent；每个 goal 显式带上稳定 `group_id`、`response_language` 与 `deliverable_language`。**第一次 Image 委派前**，每个 `plan/slide_NN.md` 的唯一 `## 视觉实现` 都必须已有一条完整单行机器字段 `- image_opportunity: <枚举>`；有位图页另用同级独立行写 `- presentation: <四枚举之一>`，不得写成空的 `image_opportunity:` 父块，不得把 `full-bleed` / `framed-scene` 填进 `image_opportunity`，也不得把 `split-media` 等 layout 值填进 `presentation`。缺字段时直接修计划并重试，不搜索或修改运行时代码。只要计划中存在有效配图机会，就不能静默跳过 Image 阶段；若计划需要图片但当前没有 Image Worker，必须重新规划为真正成立的非位图表达，或补派 Image Agent，不能直接进入完成状态。同一视觉配方且能在一张联系表中共同审清的素材归入同一分片，多张生成图在同一工具回合并行提交。Image 与 Slide 不得在同一次 `delegate_task` 中派出：先完成并验收素材，再启动页面制作。
2. 先把 `attachment_visual_map` 中 must-show / reuse 的图片登记并交给对应 Image 分组；论文命名 Figure 由 Image 以整页 PNG 仅作定位上下文，调用 `deck.py material-figure --source-pdf ... --source-page ...` 直接从原 PDF 高分辨率重渲视觉主体。默认裁掉页眉、正文、页码和长图注；工具会拒绝正文占比过高、裁区过大或分辨率不足的结果，扫描 PDF 另传 `--ocr-json`。只有页面原貌本身就是证据时才允许 `page-facsimile`，且必须提供不少于 20 个字符的 `facsimile_justification`；build/audit 会复核理由并限制整册数量。每个 Image 分组将候选路径绑定到稳定 `asset_id`，由 `deck.py asset-contact` 生成一张带 ID 的素材联系表，默认只做一次整组 Vision；只有被标红、要求抠图、比例可疑或主体完整性无法从缩略图判断的素材才打开单图复核。Image 用 `asset-review` 写回最终状态后，Orchestrator 只按 `ready` 的 `asset_id → actual path + origin + crop_contract` 回填逐页计划；候选、被替换与废弃图片不算正式素材。`assets/catalog.json` 是唯一素材真相源，必须保留下载 URL、生成模型、用户附件路径和派生关系；Image 的自然语言总结不能代替 catalog。逐页图片先锁定 `presentation: subject-only | framed-scene | full-bleed | evidence-crop`（这是位图的展示/背景处理合同，**只允许这四个枚举**；`split-media`/`right-half`/`cards`/分屏等是版式不是 presentation，放到 `layout`；**无位图页完全省略 presentation**，不写 `无`/`none` 占位）：任何要悬浮、跨色场叠放或作为独立角色/物件的图都属于 `subject-only`，必须由 Image 完成透明检查、主体抠图、最终 Alpha 检查与必要的单图 Vision，再回填可用的 `*-cutout.png`；普通 RGB 图不得作为透明资产返回 `ready`。带背景图片只能作为有意的画框场景、满幅裁切或证据裁图，不能把其白底/奶油底矩形偶然贴到另一种画布上。Slide 不临时去背，也不用 CSS mask/multiply 冒充。映射确有问题时交回同一个 Image 复核。失败素材先换可行的真实图或生成图路线，确实不可得时才改为 SVG、Canvas 或排版，并写清原因，不留占位。Slide 启动前，Image 必须有 `status: ready` 的完成合同，catalog 中所有计划 `asset_id` 都必须为 `ready`、实际文件存在，且路径与裁切合同已经回填逐页计划。
3. 一个 Production group 委派一个 Slide，可并行执行；goal 的首行必须精确写成 `Slide Group <group_id> [NN,NN]:`。页码所有权以已冻结的 `production_group` 为准；不用“负责封面和结尾”、“第一组页面”等叙述取代组 ID 与标准页码头。显式带上 `response_language`、`deliverable_language`、`why_grouped`、逐页 `anti_repetition_delta` 与该组 `boundary_handoff`。封面、每张章节页、结尾、hero、复杂图解和独立重制作页必须按计划作为单页 group 委派；普通多页组不得超过 3 页。Grouping 只为普通同构页提供共享设计记忆，不是批量降精度。
4. Slide 先读取 Style Lock、完整 resolved system 转译与组合同，然后对每一页依次执行“完整首稿 → 单页渲染 → `vision_analyze`”。之后有两个彼此独立的预算：最多 **1 轮 hard/semantic repair**，只修真实裁切、不可读、错义、素材/附件遗漏等硬伤；再最多 **1 轮 aesthetic completion**，必须先写唯一 `aesthetic_completion_target`（标题张力、视觉焦点、主视觉体量、裁切、背景层、节奏或摆脱上一页同构之一），合并修改后重渲并与上一版比较。没有硬伤也可直接使用审美轮；审美轮不得重写事实、引入新素材路径或追逐微小 lint。每页总 refine 最多 2 轮，最后一次修改必须有新像素验证；若新版退化就恢复已看过的最佳版。当前页达到 ready 后才进入下一页。
5. 等待全部页面完成后再启动首次 Review。新建或复杂编辑过程中不得额外委派 `simple_edit` 或 `review-fix` 角色；Orchestrator 不得追逐 `cjkTypography`、`crowded`、bbox/contrast 候选、轻微换行/标点等 advisory，也不得在 Review 前开启审美清门循环。Review 发现有新鲜像素/DOM 证据的真实硬伤时，只交回原所属 Slide Group；每组最多返修 2 次，每次失败由运行时恢复该组最后一次已看过的版本。返修后才可启动下一次 Review，Review 总计最多 3 次。

### 阶段 5：全册 Review 与交付

每次 Review 的 goal 必须以以下语言合同开头，再写具体诊断范围：

```text
Review:
Response language: <response_language>
Deliverable language: <deliverable_language>
mode=final_review
```

不得只在父任务或 system 中隐含语言，也不得省略后让 Review 自行猜测。随后严格两段执行：

1. **完整诊断：**先看 overview，再按 `review-contact.json` 分批看完全部联系表和必要单页；每批将覆盖页码与发现记入同一 `_trace/review-issues.md`。全册覆盖并冻结账本前禁止修改或渲染；不因 Deck 页数较长而跳过后续批次。
2. **内容保真核验：**任务含附件或使用了 Research 时，在像素修改前把每页屏显事实与 `grounded-knowledge.md` 对照；有附件时再对照 Material 摘要及 coverage ledger，并写 `_trace/content-fidelity.md`。数字、名称、日期、单位、产品身份、原话或关系无法追溯、自相矛盾时修正或 blocked。生成图只能承担概念/氛围表达；若用于具名真实产品、人物或案例识别，页面必须明确标“概念示意”，不能作为事实证据。仅当既无附件、又无 Research 和高风险外部事实时，`content_fidelity` 才可为 `not-applicable`。
   Review 停滞收口时允许补齐或更新的正式产物只有 `_trace/review-issues.md` 与 `_trace/content-fidelity.md`；运行时不得禁止写入最终验收合同明确要求的这两份文件，也不得在收口阶段允许继续修改页面。
3. **集中修复：**Review 既诊断也直接修复本次边界内可安全解决的问题；当前文件与已有素材能解决的问题不得只上报给 Orchestrator。按共同根因先全局、后局部，全部修改结束后才统一批量渲染。这一整批“修改 → 批量渲染 → focus 复验”记为 Review 的 1 轮 refine。任何 HTML/`base.css` 修改都会使旧 PNG 失效，重渲前禁止再次调用 Vision；Canvas/SVG/HTML 叠加页必须同步修正 CSS 尺寸、Canvas 属性、SVG `viewBox`、JS 坐标与节点锚点，不能只放大外容器。机检中的 `boxoverflow`、bbox 相交和装饰相交仅为定位候选；若新鲜像素没有真实遮挡、裁切或不可读，不得为清除告警缩字、压缩主体或删除有构图作用的元素。
4. 改过 base.css/字体时全册 batch；只改局部时 page batch。该批渲染用于确认修复没有退化，不是最终交付证据。
5. 生成一次 focus 联系表确认变化页。单个 Review 只做 1 轮 refine；仍有可见硬伤时返回 `blocked`，由 Orchestrator 将有证据的硬伤交回原页组。原页组保留最后验证版、做一次合并修复并重渲复看；新版退化或仍未解决时恢复验证版。修复后启动新的 Review 复验，最多形成 3 次 Review，不新增审美目标。
6. 修复确认后先同步讲稿，再执行一次 `deck.py build`。随后重新生成 `renders/review-contact.json` 与最终联系表，并用 Vision 覆盖 build 后的全部最终像素；若 build 改变 `base.css`、字体或任一页面渲染，build 前看过的 PNG 全部视为过期。最终看图后只允许更新 `_trace/review-issues.md` / `_trace/content-fidelity.md` 与返回合同，不得再改页面、渲染或 build。
7. Review 返回 `ready` 后，Orchestrator 只读取其结构化结论并确认交付文件存在；不得再次渲染、build、查看同一 PNG/contact sheet 或重新诊断相同问题。只有 Review 后发生新的页面修改，才启动下一次受控 Review 复验。

Review 超时、返回 `blocked`、缺少最终像素复验或没有自然返回合同时，先进入有限恢复流程，而不是立即把整项任务判失败。达到 3 次 Review 或每个受影响页组 2 次返修预算后停止继续改页，保留 `_trace/review-issues.md`，恢复每组最后验证版并执行确定性 build。只要全部 slide、非空最终渲染、`speech.md` 与可打开的 `present.html`/交付包存在，任务以“完成（有待改进）”交付并携带 warnings；只有缺页、渲染缺失/空白、播放器或交付包无法构建/打开等不可用技术故障才判失败。

Review 不只查“有没有溢出”，还要比较全册设计兑现：封面是否具有统治性焦点和必要层级，章节页是否既有亲缘性又体现章节推进，结尾是否回应开场；普通页是否在投影字阶下充分使用画布并形成明确阅读路径；每个章节边界是否仍属于同一基础画布家族，整页换场是否有明确的进入与退出承接；屏显是否泄漏内部规划字段、来源、文件名或无听众价值的伪元数据。未实际调用 `vision_analyze` 查看最终像素时必须 blocked。

```bash
python ${SKILL_DIR:-skills/mural-presenter}/scripts/deck.py build . --expected <总页数>
```

Review ready 时交付为 `ready`。Review 最终仍有硬伤但全部页面、渲染、讲稿与 `present.html` 可用时，以 `needs_improvement` 交付并展示问题账本；不得因为 advisory、子 Agent 文本收尾或 Review 合同不完美丢弃可用成稿。

## 3. 编辑 PPT

先完整读取 `references/editing-contract.md`，只读检查现有 plan、HTML、素材、讲稿和渲染图，建立受影响文件/页面清单，再选择编辑路径。

### 3.1 简单与复杂的判定

**简单编辑**同时满足：

- 不改变核心论点、页序、页面职责或跨页叙事；
- 不需要新 Research、Material 或 Image；
- 不改变全局 Style Lock、字体系统或多个 arch；
- 可在少量页面内安全完成，且影响边界明确。

任一条件不满足即按复杂编辑处理。页数只是信号，不是唯一判据。

### 3.2 Review 快修

简单编辑只委派一个 Review，goal 标明 `mode=simple_edit`、用户原始修改要求、目标页和不可改变项。

Review：

1. 看现有 overview 与目标页最终 PNG；
2. 一次列完本次修改项；
3. 读取目标页计划与 HTML，集中修改；
4. 更新受影响的计划/讲稿；
5. 用 `render.py --batch --pages` 一次重渲并看 focus，确认修改不退化；
6. 同步讲稿并执行 `deck.py build`，再重新生成 focus、查看 build 后最终像素并返回 ready/blocked；最终看图后不再修改或 build。

不派 Slide、Image、Research 或第二个 Review。

### 3.3 Orchestrator 改造

复杂编辑由 Orchestrator：

1. 写影响图：事实、叙事、页序、Style Lock、base.css、素材、页面、讲稿分别受什么影响；
2. 只复用仍有效的既有成果，不从头覆盖无关页面；
3. 按缺口委派唯一 Research、多个 Material/Image、多个受影响 Slide 页组；互不依赖的任务并行；
4. 更新受影响计划并运行 `deck.py prepare`；
5. 只重做受影响页面；全局 token 变化时 batch 重渲全册；
6. 最后委派唯一 Review，以 `mode=final_review` 做全册一致性与讲稿收口。

复杂编辑不允许让 Review 独自重写叙事或凭空补素材，也不允许 Orchestrator 直接改页面 HTML。

### 3.4 编辑交付门

- 用户要求逐项可追踪到修改结果；
- 未受影响页面和素材保持不变；
- 新旧页面风格、页码、讲稿和播放器一致；
- 所有变更页面已看最终像素；
- 字体包、render freshness、`speech.md`、`present.html` 重新通过。

## 4. 硬红线

- 图表必须用 ECharts，不用生成图伪造数据图表。
- AI 生成图不承载需要准确呈现的文字；文字放 HTML 层。
- SVG 不禁用：静态结构、机制、关系和流程图可直接使用 `svg-diagram` 作为主视觉，不受五类配方白名单限制；仍须通过体量、方向、标签与像素检查。真实主体能配图时不以 SVG 代替，数据图仍用 ECharts。
- `slides/` 只保留正式 `slide_NN.html`，不放备份或临时页。
- 页面固定骨架、页脚安全区、最小字号、对比度与无溢出是硬门。听众阅读的正文不得低于 20px，注释、来源和辅助说明不得低于 18px；若字体 token 规定了更大值，以更大值为准。内容放不下时减少卡片数量、删减重复屏显文字、调整信息层级或拆页，不得继续缩字。
- 内部规划标签、来源、文件路径、制作状态和无听众价值的伪元数据不得出现在屏显内容中。
- 最终判断看 PNG；修改后未重渲、未看新像素，不得声称完成。
- Review 最多 3 个受控实例；只有页面实际修改并重渲后才允许复验。达到预算后停止返工并带 warnings 交付可用成稿。

## 5. 确定性脚本

| 脚本 | 用途 |
| --- | --- |
| `stage_materials.py` | 保留：统一处理文本、PDF、Office/ODF、图片、媒体、压缩包与未知格式的解析、视觉派生物和 coverage catalog |
| `font_bundle.py` | 保留：OFL 白名单、官方来源、许可证随包、字符裁剪、交付校验和 render freshness 属于独立高风险能力 |
| `render.py` | 保留：单页诊断；`--batch` 复用同一 Chromium 渲染整册或指定页 |
| `image_cutout.py` | 保留：检查 Alpha、清除烘焙棋盘格/纯色背景，并在需要时用 GrabCut 生成独立主体 PNG；不覆盖来源原图 |
| `deck.py` | 保留：`prepare` 一次完成计划讲稿与字体前置；`asset-register` 登记来源；`asset-assign / asset-contact / asset-review` 管理语义素材、分组联系表与最终状态；`contact` 生成页面联系表；`build` 校验来源并生成播放器；`sync` 仅供只改讲稿时使用 |
| `install.sh` | 保留：跨环境依赖、字体和 Chromium 安装无法由运行脚本可靠替代；依赖清单已内联 |

首次部署依赖解析 venv、PyMuPDF、可分发字体、FontTools/Brotli 和 Playwright Chromium；用 `scripts/install.sh` 安装。运行脚本时若 skill 挂载路径不同，使用实际 skill root。
