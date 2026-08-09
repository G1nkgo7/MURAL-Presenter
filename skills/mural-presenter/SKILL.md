---
name: mural-presenter
description: 创建和编辑完整的 HTML 演示文稿。支持主题、提纲、文档、多附件和已有 deck，交付 1600×900 的逐页 HTML、渲染图、逐页讲稿与 present.html。新建时按需使用 Material、Research、Image、Slide Group 和 Review；编辑时根据影响范围选择局部快修或多 Agent 改造。适用于制作、续编、重排、统一风格、审校和修改 PPT、deck、slides 或 presentation。
---

# Mural Presenter

## 1. 定义与质量目标

Mural Presenter 把用户的主题、材料或已有演示稿转成一套可播放、可检查、可继续编辑的静态 HTML 演示文稿。

成品首先要好看，也要好讲、好读。每页应有清楚的视觉焦点和阅读顺序；图片、排印、图表或解释图至少有一种真正承担画面。信息不能挤成一团，也不能缩在角落、留下大片无职责空白。视觉冲击来自尺度、对比、构图和有意义的素材，不来自堆叠装饰或塞满卡片。

准确性和可读性是底线。在此基础上，优先选择更有表现力、更适合投影的方案。

正式文件就是阶段记忆：

- 事实以用户原文和 `plan/grounded-knowledge.md` 为准；
- 整册视觉以 `plan/design-brief.md` 为准；
- 页面内容以 `plan/slide_NN.md` 为准；
- 素材状态以 `assets/catalog.json` 为准；
- 最终质量以最新 PNG 为准。

已经确认的决定和未解决项要及时写入对应文件，不只留在对话或工具历史中。逐页计划只能细化上游决定，不能静默削弱整册方案；确有冲突时先修改上游文件。

## 2. 工作流与职责

按下面的顺序推进，不跳过真实依赖：

```text
Step 0  环境预检并读完输入
Step 1  判断新建或编辑，解析用户需求
Step 2  建立事实基础
Step 3  确定整册视觉方案
Step 4  完成逐页计划与页面组
Step 5  准备正式图片
Step 6  Slide Group 制作并自检页面
Step 7  Review 全册并锁定最终像素，再做无损构建与交付
```

| 角色 | 进入条件 | 唯一职责与正式产物 |
| --- | --- | --- |
| 编排器 | 始终存在 | 解析任务、整合证据、制定整册方案、划分页面组、委派和验收 |
| Material | 有附件 | 完整读取一个互不重叠的附件分片，写 `materials/summaries/<assignment_id>.md` |
| Research | 外部事实会影响结论，至多一个 | 核验事实缺口，写 `research/research.md` |
| Image | 计划需要真实图片或生成图 | 准备、检查并登记 `assets/catalog.json` 中的正式素材 |
| Slide Group | 新建或结构性编辑 | 制作一个页面组，并完成组内最终像素检查 |
| Review | 每个任务一个 | 审查全册、集中修复、锁定最终像素、同步讲稿并做无损构建 |

每个子 Agent 只读取自己的 `roles/` 角色卡、角色卡点名的文件和当前任务需要的输入。Research 和 Review 是任务级单例；失败或阻塞时如实结束，不用 `_r2`、`_r3` 绕过原结果。编排器不亲自制作页面，也不重复角色卡全文。

委派只使用一个工具形状：`delegate_task({"tasks":[{"goal":"..."}]})`。每项只写 `goal`，并使用稳定首行让运行时推导角色、轨迹名和工具白名单：

- `Material <assignment_id>:`
- `Research:`
- `Image <group_id>:`
- `Slide Group <group_id> [01,02,...]:`
- `Review:`

不要给任务附加额外控制字段；任务所需路径、语言、边界和交付要求直接写进 `goal`。

并行跟随依赖图：同一批互不重叠的 Material 分片、Image 组或已满足依赖的 Slide Group，在一次委派中同时启动；同一角色回合内，互不依赖的读取、检索、下载、生成和渲染应批量执行。有附件时，Material 分片可以彼此并行，但 Research 必须等全部 Material 返回并被编排器读完后再决定和启动。不得让多个 Agent 同时修改同一页面、计划文件或素材状态；每页始终只有一个 owner。

## 3. Step 0–1：开始与任务解析

### Step 0：预检并读完输入

环境预检属于运行环境，不属于演示文稿 Agent 的生产职责。托管运行设置
`MURAL_PREFLIGHT_DONE=1` 时，环境和当前工作区已经在模型调用前通过预检，直接读取输入，
不要再次运行预检、检查版本、搜索字体或修复环境。仅在脱离托管运行的人工开发会话中，
由操作者运行一次：

```bash
${MURAL_RUNTIME_PYTHON:-python} ${SKILL_DIR:-skills/mural-presenter}/scripts/deck.py preflight .
```

预检检查工作区读写、Python、便携字体依赖、Skill 内置资源，以及 Chromium/Playwright 的最小真实渲染；有附件时还检查原件和相应转换器。部署环境只通过 `scripts/install.sh` 一次性安装或修复；生产 Agent 永远不安装依赖。失败时由运行层在首个模型请求前终止并报告具体缺口，不把环境故障转交给 Agent。不要改变 `PATH` 或 Python 解释器，不要用 `which`、`--help`、全盘 `find /`、临时安装或多组版本命令替代统一预检。

然后建立输入清单：原始 query、用户提供的全部附件、明确给出的 URL，以及编辑任务中的既有 deck。每项都必须有去向：

- 保持原始 query 的含义，不把编排器假设改写成用户要求；
- 附件进入 Material coverage，长文档和多附件按互不重叠的分片并行读取；
- 承担内容或证据的 URL 交给 Research 实际访问，并记录成功、失败或未解决边界；
- 当前阶段选中的角色卡、reference 和文本文件必须读到末尾；看到续读 offset 就按该 offset 继续，直到不再出现截断提示；
- 不扫描无关工作区，也不通读当前阶段没有选择的 references。

编排器只用原始 query 与 `materials/attachments.json` 判断附件范围，不直接读取或通过终端查看 `materials/_raw/`、`materials/_work/` 的正文和页图。附件内容由 Material 完整解析；编排器随后只读取 `materials/summaries/` 的正式摘要。

所有附件必须取得 `coverage: complete`，或明确返回无法继续的缺口；输入未读完时，不开始写计划或页面。

### Step 1：解析任务

先确定任务模式：

- **新建**：从主题、brief、提纲、文档或模板创建新 deck，继续执行第 4、5 节；
- **编辑**：修改已有 deck，按第 6 节选择局部编辑或结构性编辑。

两种模式最终都进入第 7 节完成验收、构建和交付。

新建任务依次判断：

1. **设计方向**
   - 自主设计：用户没有偏好，或只给简单风格约束；
   - 设计系统：用户指定完整设计规范或明确点名本 Skill 的系统；
   - 使用模板：用户提供必须沿用的模板；
   - 风格迁移：用户提供图片、网页、PDF 等视觉参考，只迁移设计语言，不把参考内容当成用户事实。
2. **输入类型**
   - 仅主题；
   - 完整文档；
   - 大纲或讲稿；
   - 模板或风格参考。
3. **页数**
   - 用户明确指定时严格遵从；
   - 逐页大纲或讲稿默认与有效页面单元一致；
   - 完整文档或仅主题未指定页数时，结合结构、时长和复杂度给出建议；能够交互时确认，无法等待时自主确定并记录理由。
4. **语言、能力与证据路径**
   - `response_language` 跟随 query，控制过程说明和最终回复；
   - `deliverable_language` 控制屏显文案和讲稿；
   - 记录时长、附件、交付范围，以及 web、图片搜索、图片生成、渲染、Vision 和写文件能力；
   - `evidence_path: direct | grounded`：用户原文和明确假设足够时用 `direct`；附件需保真、外部事实影响结论或数据与实体需核验时用 `grounded`。

完整文档、大纲或讲稿没有明确禁止扩写时，可以用 Research 补充必要背景、案例和事实；补充内容不能覆盖用户原文、改变既定结构或伪装成用户提供的信息。用户明确要求不扩写时，只做结构化、编辑和视觉表达。

初始化 `plan/deck.md`，写入 `## 已解析任务`：演讲者、听众、场合、目标、时长、页数、核心结论、设计方向、输入类型、语言、能力、证据路径和必要假设。后续步骤在同一文件中补全结构与页面组，不再重新解释任务。

任务卡只服务内部规划。`受众：…`、`页面角色：…`、素材编号、文件路径和制作状态等内部信息不得直接出现在页面上。

**完成标志：**任务模式、设计方向、输入类型、语言、页数、附件、能力、事实风险和证据路径均已明确。

## 4. Step 2–4：新建演示文稿的规划

### Step 2：建立事实基础

先根据原始 query 建立附件分片范围，再在同一次委派中并行启动互不重叠的 Material。每个 goal 明确写入 `assignment_id`、附件路径和唯一输出 `materials/summaries/<assignment_id>.md`；`materials/_work/` 只存解析中间物，不另造 `plan/materials-*`、`research/materials-*` 或散落的 summary 文件。常见文本、PDF、Office、图片、音视频和压缩包统一通过 `stage_materials.py` 处理，具体方法见 `roles/material.md`。附件未读完、扫描页未查看或关键表格无法可靠还原时，不能把摘要当成完整材料。

全部 Material 返回后，编排器必须逐份把 `materials/summaries/*.md` 读到末尾，再把附件事实、冲突和缺口与原始 query 合并。只有这些缺口需要外部证据且会影响结论时，才委派唯一 Research；不得在有附件时把 Material 与 Research 放进同一波。Research goal 必须包含完整原始 query、正式材料摘要路径、待核对象和明确证据缺口。编排器新增解释只能标为假设。Research 唯一正式产物是 `research/research.md`，不要另起 `plan/research-brief.md` 等路径。

证据子任务返回后，编排器立即写 `plan/grounded-knowledge.md`，分别记录用户事实、附件事实、外部核验、编排器假设、冲突与使用边界。`direct` 路径且没有证据子任务时不必创建该文件。启动过 Material 或 Research 时，必须确认它存在且完整后才能继续。

**完成标志：**上屏事实均可追溯，未确认内容没有被写成确定结论。

### Step 3：确定整册视觉方案

先读取 `references/scenario-routing.md`，选择一个主场景，再只读取它链接的一个 `references/slide-categories/*.md`。分类指南负责听众任务、叙事、证据对象、页面家族和疏密节奏；视觉外观再按设计方向选择一条路线：

- **自主设计**：先读 `references/style-routing.md` 的导航，只进入一个相关的 `references/style-families/*.md` 风格簇，从中选择并转译一个主家族；不自动套用完整预设；
- **设计系统**：只读取用户指定的规范，或 `style-routing.md` 路由到的一个明确内置系统文件，不混入另一套系统；
- **使用模板**：沿用模板的颜色、字体、栅格和组件，仅修正可读性与交付风险；
- **风格迁移**：提取参考的颜色职责、字体角色、图片处理、密度和构图关系，不照搬其内容，也不再叠加预设。

只读取当前路线真正需要的 reference：页面需要选择图型、数据构图、流程、架构或复杂关系时读 `references/charts-and-diagrams.md`；需要设计节点、连接、判断、归组或几何母题时再读 `references/shape-grammar.md`；默认字体角色不足、用户指定字体或字体是风格主声部时读 `references/fonts.md`；通用排版、图片和密度原则按需读 `references/design-rules.md`。路线确定后停止横向比较。

按 `references/planning-contract.md` 在 `plan/design-brief.md` 写明主场景、分类 reference、已选择 references、视觉主张、空间原型、画布与栅格、字体角色、颜色语义、图片处理、图表语言、形状语法、页面家族、特殊页系统、疏密节奏、fallback 和应避免的习惯。已选择 reference 必须是实际读过并采用的文件；后续 Agent 消费设计合同，不重新浏览整个参考库。

整册方案约束的是设计语言，不是固定模板。页面可以改变重心、方向和媒介，但字体角色、颜色语义、图片处理和构图逻辑应属于同一个世界。

同时逐页判断配图机会：用户附件优先，其次是官方或可信来源、直接相关的搜索图片、概念生成图。真实人物、地点、产品、作品、事件、案例、界面和实验对象优先用真实图片或截图；氛围、隐喻、故事画面、空间体验和可以被一个具体画面讲清的过程切面优先生成图片。只有关系、方向、层级或数值必须精确时，才使用 Canvas + HTML 文字或 ECharts；不要为省事把本可成为主画面的内容降级成抽象几何。Logo、小图标、箭头、标记和局部装饰才使用小型 SVG，SVG 不承担半屏或全屏主体视觉。图片必须帮助识别、理解、举证或建立场景。

**完成标志：**整套 deck 能用一句视觉主张解释，且封面、章节峰值页和适合配图的内容页会真实兑现它。

### Step 4：完成逐页计划与页面组

按 `references/planning-contract.md` 完成：

1. `plan/deck.md`：补全目标、结构、节奏、视觉摘要、页面地图和页面组；
2. `plan/theme.css`：只写本册真正需要覆盖的全局 token、字体角色和少量共享组件；不要读取、复制或重写 `assets/base-template.css`，`prepare` 会确定性生成 `base.css`；
3. 全部 `plan/slide_NN.md`：标题、听众所得、屏显文案、证据、视觉实现、素材、验收和讲稿。

先在内存中完成连续页面批次的判断，再在一个工具回合写入多个互不冲突的逐页计划；不要按“写一页 → 读回 → 校验 → 再写一页”串行推进。全部计划写完后统一运行一次 `prepare`，只有失败项才局部修补。

先完成页面地图、页型、视觉媒介和素材依赖，再拆页面组。分组优先考虑制作方式与构图亲缘性，其次考虑共享素材、叙事连续和并行负载；章名相同不等于制作问题相同。合并真正需要共享设计记忆的页面，拆出图片叙事、数据图、复杂机制图和特殊页系统，并拆解会形成串行长尾的主导组。

只有一至三页、且制作方式确实统一时，单组通常才划算。四页以上的新建 deck 至少拆成两个可独立开工的设计单元；短 deck 常见拆法是“封面与结尾 / 核心内容”，或再把复杂数据、机制、图片叙事拆成第三组。不要按页数机械平均，也不为填满并发退化成单页 Agent。

为每组写明 `load_reason`、`parallel_with` 和 `boundary_handoff`。每页必须且只能有一个 owner。

冻结前做一次页面地图预检：

- 每页是否有不可替代的听众所得和足够的事实、机制、对比、案例、行动或边界；
- 信息是否疏密适中，主要视觉是否获得与职责相称的画布；
- 主图、视觉证据或 Hero 是否被降成角落小缩略图；
- 同一短语是否在标题、角标、标签、图片和页脚中无意义重复；
- 文案是否具体、自然、直接面向听众，且没有生产术语、内部信息或默认 AI 套话；
- 章节之间是否机械复制页面脚本，特殊页是否各司其职，参考文献与结尾是否分开；
- 防坑清单、规则集合和多组行动项是否留在内容页；结尾只选择“收束页”或“行动末页”一种身份；
- 需要图片的页面是否已经写明素材路线和裁切要求。

把结果写入 `plan/deck.md` 的 `## Repetition & rhythm preflight`，然后运行：

```bash
python ${SKILL_DIR:-skills/mural-presenter}/scripts/deck.py prepare . --expected <总页数>
```

**完成标志：**事实、页序、标题、屏显文案、素材 brief、页面组、初版讲稿和字体均已冻结。

## 5. Step 5–6：素材与页面制作

### Step 5：准备正式图片

按视觉口径把真实图片和生成图需求分组，在同一次委派中启动互不依赖的 Image 组。素材准备完成前不启动 Slide Group。

Image 下载、生成或复用本地图片；从论文页裁出真正的 Figure；为悬浮主体生成真实 Alpha 抠图；记录来源、派生关系和裁切要求；最后生成素材联系表并做一次整组检查。唯一正式状态是 `assets/catalog.json`，不要另写 manifest。

Image 不受固定搜索次数限制，但每轮“获取—检查”必须写回采用、淘汰、待处理或缺口；没有状态变化时不得继续扩大候选池。编排器只把 `ready` 的 `asset_id → path` 回填到逐页计划，候选图和待复核图不算正式素材。

**完成标志：**计划中的图片路径真实存在，来源、比例和裁切要求明确。

### Step 6：制作页面并完成组内检查

一个页面组委派一个 Slide Group。所有素材依赖满足后，在同一次 `delegate_task` 中派发互不重叠的页面组，让它们同时开工或按可用槽位排队。目标首行必须使用：

```text
Slide Group <group_id> [01,02,03]:
```

委派内容只传回复语言、交付语言、组 ID、页码、角色卡路径、`plan/deck.md` 和本组逐页计划路径，不重复整份视觉合同。

每个 Slide Group 执行同一闭环：

1. 一次完成组内全部页面的完整首稿；
2. 批量渲染整组；
3. 生成覆盖整组的联系表并做一次 Vision；
4. 对特殊页、复杂图表和联系表无法判断的页面查看全分辨率单页；
5. 合并修复，批量重渲变化页，再检查新鲜像素。

首轮集中解决同页全部已知问题。只要新鲜像素仍在明确改善且存在真实硬伤，可以继续合并修复；一轮带新像素的修复没有减少问题、只是移动缺陷或引入退化时，立即恢复已验证的最佳版本、改用更稳定的结构，或返回 `blocked`。不为凑轮次微调，也不因固定轮数耗尽而带着已知问题交付。最后一次修改没有新像素覆盖时不能返回 `ready`。

**完成标志：**每页最终 PNG 均有新鲜像素证据，且没有已知硬伤。

## 6. 编辑已有演示文稿

先完整读取 `references/editing-contract.md`，再做只读影响分析。

满足以下条件时属于**局部编辑**，由唯一 Review 以 `mode=simple_edit` 完成：影响页面明确，不改变核心论点、页序、跨页叙事、事实、素材或全局视觉，也不需要新的 Material、Research 或 Image。Review 只改目标页，批量重渲变化页，检查 focus 联系表；随后跳过第 7.2 节的全册 Review，直接按第 7.3 节重新构建和交付。

其余属于**结构性编辑**。编排器先记录影响范围，再按真实缺口启动必要的 Material、Research、Image 和 Slide Group；只重做受影响页面，保留仍有效的事实、素材和页面，最后进入第 7 节的唯一 Review。

## 7. Step 7：Review、构建与交付

### 7.1 统一的视觉检查方法

Material、Image、Slide Group 或 Review 需要检查一组附件页、素材或页面时，都使用：

1. **总览路由**：先看覆盖整组的联系表、总览图或缩略图索引，一次列出可疑项；
2. **全分辨率确认**：再打开特殊页、复杂图表、关键证据，以及总览无法判断的单页或单图；
3. **新鲜像素复验**：修改后重新生成总览，只复核变化项和受共同样式影响的内容；
4. **完整覆盖**：长 deck 自动拆成多张联系表，逐片看完，不设置全册累计图片上限。

总览只负责路由。表格文字、论文 Figure、数据映射、身份识别和精细裁切必须以全分辨率证据判断。若当前角色没有图片输入能力，只能做 DOM、边界、资源、对比度、长文本和结构检查，并明确记录未完成像素质检；需要最终像素证据的 Image、Slide Group 和 Review 不得因此返回 `ready`。

### 7.2 全册 Review

新建或结构性编辑的所有页面完成后，只委派一个 Review，使用 `mode=final_review`：

1. **先诊断**：按联系表批次逐步读取对应逐页计划和证据，看完全部联系表与必要单页，核对事实、视觉语义、可读性、特殊页和跨页一致性，写 `_trace/review-issues.md`；诊断完成前不修改；
2. **再修复**：按共同根因合并修改，批量重渲变化页，通过前后对比确认新版确实更好。

修改前记录旧版成立的焦点、尺度、阅读路径和语义关系。bbox、overflow 和 lint 只是诊断线索；新鲜像素没有真实问题时，不为清除告警而删元素、缩主体或压缩版面。

先做一轮集中修复；新鲜像素仍显示明确改善时继续处理残余硬伤。问题账本没有减少、缺陷只是移位或出现新退化时，恢复最佳版本、换稳定解法或返回 `blocked`。不得另派第二个 Review，也不得把未经像素验证的版本当作完成。

最后一次页面修改和重渲完成后，重新运行一次全册 `deck.py contact`，让联系表清单记录当前每页 PNG 的哈希；再检查受影响的联系表分片和必要单页。该像素证据一旦确认，后续不得再运行 `prepare`、修改 HTML/CSS、改素材或重渲。

### 7.3 构建、动效与交付

问题清零后，Review 根据最终页面同步 `speech.md`，再运行：

```bash
python ${SKILL_DIR:-skills/mural-presenter}/scripts/deck.py build . --expected <总页数>
```

便携字体和运行资源已经在 `prepare` 阶段冻结，最终联系表也已由 Review 生成。`build` 只校验已验收的 HTML、CSS、字体、素材、依赖、PNG 和联系表新鲜度，再写播放器并同步讲稿；它不得修改页面像素源、重做 Review 证据或自动重渲。若 build 报告 HTML/CSS/PNG、联系表或前置资源不完整，先修复并重渲变化页，重新生成联系表并完成像素验收，再次 build。build 成功后，交付像素与 Review 最终证据必须保持相同哈希。

构建器只在最终 `present.html` 中统一加入轻量动效：页面交叉淡入，页内按标题、正文一级内容和页脚的阅读顺序依次出现。背景、装饰和图表内部零件不逐个飞入；动效不改变布局、不写回逐页 HTML，也不影响 PNG 质检。确有特殊讲述顺序时，可给少量顶层内容添加 `data-reveal` 和 `data-reveal-order`。播放器必须尊重系统的“减少动态效果”设置。

自包含项目目录至少包括：

```text
materials/_raw/                   # 用户附件原件
materials/_work/<assignment_id>/  # Material 解析中间物
materials/summaries/<assignment_id>.md  # 正式附件摘要
research/research.md              # 启动 Research 时的外部核验结果
plan/grounded-knowledge.md       # 启动 Material / Research 时的事实与边界
plan/design-brief.md             # 整册视觉方案
plan/deck.md                     # 结构、节奏与页面组
plan/theme.css                   # 本册主题覆盖，不复制基础模板
plan/slide_NN.md                 # 逐页内容与设计交接
base.css                         # prepare 生成的完整全局样式
assets/                          # 正式素材、字体、脚本依赖与来源状态
slides/slide_NN.html             # 逐页 HTML
renders/slide_NN.png             # 最终渲染图
renders/contact-sheet.png        # 全册总览
speech.md                        # 逐页讲稿
present.html                     # 最终播放入口
```

交付前确认：

- 所有本地图片、字体、CSS、JS 和 ECharts 依赖真实存在；
- 图片没有拉伸、主体没有被错误裁切，生成图不伪造数据图表或承载必须准确的文字与数字；
- SVG 不承担半屏或全屏大型主视觉；
- 页面没有泄漏受众、页面角色、证据编号、来源路径或制作状态，也没有用 emoji 代替设计元素；
- `slides/` 只保留正式 `slide_NN.html`，所有最终修改均已重渲并检查；
- Review 覆盖全册最终像素并返回 `ready`；
- `speech.md` 与页面逐页对应，可由演讲者直接面对听众口述，来源和备注位于对应页讲稿之后；
- `present.html` 从通过验收的最新文件构建，并能加载全部本地依赖。

最终回复使用绝对路径，至少链接项目目录、`present.html`、`speech.md` 和全册联系表。任何一项未完成时，准确说明阻塞原因，不把半成品说成完成。

## 8. 按需参考与模板资产

| 文件 | 什么时候读 |
| --- | --- |
| `scenario-routing.md` | Step 3 选择任务场景和表达重心 |
| `slide-categories/*.md` | Step 3 只读主场景对应的一份分类指南 |
| `style-routing.md` | Step 3 选择一个风格簇或路由到明确系统 |
| `style-families/*.md` | 自主设计时只读当前风格簇对应的一份 |
| `style-systems/*.md` | 用户明确点名内置系统时只读对应的一份 |
| `design-rules.md` | Step 3 确定字体、颜色、图片、图表和密度原则 |
| `charts-and-diagrams.md` | 页面需要选择图型、数据构图、流程、架构或复杂图解时 |
| `shape-grammar.md` | 存在流程、层级、关系、标注或几何视觉语言时 |
| `planning-contract.md` | Step 4 写 deck、逐页计划和页面组 |
| `layout-patterns.md` | Step 6 为具体页型选择构图 |
| `quality-checklist.md` | Step 6 组内检查和 Step 7 全册检查 |
| `editing-contract.md` | 编辑已有 deck |
| `fonts.md` | 用户提供字体或默认字体不足时 |

只读当前阶段真正需要的文件。被选中的文件若出现截断提示，继续读取到文件末尾。

`assets/base-template.css` 只由 `deck.py prepare` 合并到 `base.css`，不是需要读取的设计 reference；`assets/vendor/` 和 `assets/licenses/` 同样只由确定性脚本复用。

## 9. 脚本

| 脚本 | 用途 |
| --- | --- |
| `deck.py preflight` | Step 0 检查环境、浏览器、字体和附件原件 |
| `stage_materials.py` | Step 2 解析附件并生成 coverage catalog |
| `bundle_fonts.py` | 打包开源字体和用户授权字体 |
| `cutout_image.py` | 检查透明通道并生成主体抠图 |
| `render.py` | 渲染页面并输出几何诊断 |
| `deck.py prepare` | Step 4 校验计划、准备字体并同步初版讲稿 |
| `deck.py asset-*` | Step 5 批量下载、登记、分组、检查和确认图片素材 |
| `deck.py contact` | Step 6–7 生成页面联系表 |
| `deck.py build` | Step 7 校验最终产物并生成带轻量动效的 `present.html` |

首次部署使用 `scripts/install.sh` 安装解析、字体和 Chromium 依赖。生产任务只运行统一 preflight；失败后再做定向诊断，不进行无目的环境探测。
