---
name: mural-presenter-v0-2-zh
description: 面向听众制作 1600×900 静态 HTML 演示文稿的中文说明；成品语言由用户 query 决定，不由说明版本决定。
---

# Static HTML Presentation

制作适合现场讲述、证据清楚、整册统一但不显得机械套模板的演示文稿。

## 角色与唯一产物

| 角色 | 正式产物 |
|---|---|
| Orchestrator | `plan/deck.md`、全部 `plan/slide_NN.md`、委派与交付 |
| Material | 有附件时唯一的 `research/material.md` |
| Research | 唯一的 `research/knowledge-brief.md` |
| Image | 本地图片、唯一 `assets/catalog.md` 与素材联系表 |
| Slide | 一页 `slides/slide_NN.html` 及基于 PNG 的修订 |
| Review | 整册最终像素与讲稿一致性结论 |

## 流程

```text
有附件时 Material
→ 单个 Research brief
→ deck.md + slide_NN.md
→ validate-plans
→ scaffold-from-plans + sync-speech
→ Image 与 code_only / none Slide 并行
→ preferred 在 Image 给出“已解析/已跳过”决定后启动
→ required 在素材就绪后启动
→ finalize
→ Review；若有改动，再 finalize 并复看
→ 交付
```

长 Deck 可以按连续的小组写逐页 Markdown；中断后直接检查缺哪一页、补哪一页。
Research 只维护一份规范 brief：证据跨多个主题时尽早建立稳定章节，只补未完成章节，
不在最后反复重写一份巨型文件，也不另建重复证据账本。
生产阶段始终是一页一个 Slide Agent；全部 `Slide NN:` 可在同一个 wave 并行，但不得
合并成 SlideGroup。v0.2 刻意与单页训练分布保持一致。
除纯改写/虚构、用户禁止联网或附件已提供完整证据外，Research 首轮应实际并行检索，
不能用模型记忆代替证据获取。

## 简短请求

一两句话的 query 是信息尚未展开的 brief，不等于要求做一套单薄的 Deck。先遵守所有
明确约束，再根据主题与 Research 补齐受众语境、汇报目的、需要推动的判断和合理范围，
并把这些选择写进任务解析。内容通过相关事实、案例、对比、机制、影响和下一步变得
充实，而不是靠通用填充或无边界百科式扩写。

视觉丰富度应跟随信息任务：具名真实对象使用可信真图，概念与氛围可用统一方向的
生成图，数据与关系使用 HTML/CSS/SVG。纯排版停顿可以是有意识的节奏；不能因为原始
query 很短，就默认连续使用文字块或卡片宫格。query 长度本身不应降低证据深度和视觉
表达意愿。

不要继承一套安全默认色，而要明确本册的配色命题与视觉性格。颜色可来自主题的地点、
材质、时代、受众、真实图片或情绪温度；内容画布可以是有彩色、深色、柔和染色或纸张
质感。米白/白色配深蓝只是一个答案，不是“专业”的默认定义。选择一套连贯的视觉性格，
例如高彩编辑、纪实、触感印刷、发光技术、插画或强排版，再在这套语言内部建立页面变化。

## 规划边界

`plan/deck.md` 是唯一全局契约，记录受众、目标、交付语言、页数、叙事地图、
页面节奏、视觉语法、特殊页系统、图像方向、反默认项与短 `Theme Tokens` 区块。

说明版语言不决定 PPT 语言。Research 后，Orchestrator 根据原始请求中明确或隐含的
语言、页数、受众和视觉媒介意图作出解析，并连同一行理由写进
`## Resolved deck brief`；之后所有页面和角色都沿用这份解析，不再漂移。

每个 `plan/slide_NN.md` 定版：

- 页面职责、页型/页族、标题链与前后衔接；
- 普通页可改写的 `composition` 起点及第一眼焦点、阅读顺序、主辅区域职责；
- 封面、过渡或结尾页的结构起点 `special_layout`；
- 本页要让观众理解或决定什么；
- 可用证据与明确假设；
- 视觉需求为 `required`、`preferred`、`code_only` 或 `none`；
- 语义视觉需求、必要约束与讲稿节拍。

Orchestrator 定版标题，因为标题序列承载整册逻辑；但不写组件树、精确坐标、
装饰配方或完整正文。Slide 根据轻骨架和证据包完成真正的屏显文案与构图。
逐页计划和委派必须自包含；跨页连续性写成共享语义或短锚点，不能要求 Slide 查看
或沿用另一页 HTML。

附件中的核心定义、方法、发现、数字与结论若承担本页论证，必须进入屏显内容；不能因为
`speech.md` 已经解释就从页面删除。讲稿补上下文与来源，不替代观众需要当场看见的重点。
学术、课程和论文附件不意味着降低版式、排印或视觉表达，只意味着不能虚构证据。

Markdown 结构见 [plan-contract.md](references/plan-contract.md)。全局规则只写一次，
不要复制进每一页计划。

## 面向听众

屏幕上的每一行都应帮助听众理解、比较、记忆或行动。不要显示生产备注、证据编号、
文件名、模板标签、无意义机密字样、设计说明，或“呼应封面”一类内部语言。同一意思
不要在标题、正文、图片标签和页脚重复出现。

完整来源、URL、机构年份和引用编号只进入 `speech.md`。仅当来源身份会改变听众对
结论的理解时，才在页面上用自然语言简短归因。

重点色必须有可见意义：当前状态、关键决策、风险、结论或章节身份。普通内容页共享
Deck 级 `--content-canvas`；局部色块、拼贴纸张、图表、图片和强调色仍可自由变化。
只有逐页计划明确声明 `canvas_variant` 时，才有意改变整页画布。共享画布不等于白色
卡片；应使用本册配色中的同色阶色场、图片裁切、描边、材质和字体对比。

## 特殊页

封面、过渡页、结尾页使用独立的全画布结构：

```text
special-canvas
├─ special-background
├─ special-overlay
└─ special-safe
```

背景图片、纹理、SVG 和色场可以触达四边；可读文字留在 `special-safe`。特殊页
不再复用内容页的 `slide-inner → page-frame → page-body` 链路。

封面通常需要一个有意义的 Hero 图片或证据对象；可检索的真实主体优先真图，非特定
氛围优先生成统一风格的位图。只有页面本身要解释机制、关系或数据时，才把原创 SVG
作为主 Hero。标题能凝练时就凝练；
内容确实较长时拆出层级并选择能容纳它的构图。不要让所有 Deck 都是顶部标题，也不要
让短标题只剩空旷的居中画面。

同一 Deck 的过渡页共享编号语法、字体角色、母题处理、色彩关系和空间气质；可以在
少量同源构图间变化。`special_layout` 只选择稳定的页头网格，`page_family` 继续承载
属于本主题的自由艺术方向。`section_index` 只写数字（如 `02`），完整章节标签只出现
一次。过渡页只承担章名、一句过渡与一个母题，不承担正文论证或 KPI。

除非用户明确不要，否则保留结尾页。内容应简洁并明确面向现场听众：结论、感谢、
Q&A 或真实下一步；不要增加新论点、虚构联系方式、生产备注或突出的物理页码。

## 图片

Orchestrator 在逐页计划中说明视觉目的与真实性要求；Image 决定搜图、生图，或把
`code_only` 需求交给 Slide。v0.2 暂不把附件中的任何像素、页图或裁图作为交付素材；
附件只通过原生文本/OCR 为事实、数据、caption、标签与视觉语义提供 grounding：

- 真实人物、地点、产品、文件、作品、事件和案例：优先检索并下载真图；
- 非特定氛围或明确需要统一艺术风格的概念画面：允许生成；
- 数据、流程、架构与关系：HTML/CSS/SVG；
- 没有表达价值的装饰图：不做。

先做位图机会判断，再决定是否使用 SVG。人物、地点、产品、作品、物体、真实场景、
案例氛围或叙事转场，只要位图能增加身份、空间、情绪或记忆点，就优先搜图/生图；
不能因为 SVG 更快、更可控而把它们改画成通用矢量插图。SVG 的首要用途是精确表达
数据、流程、架构、机制和关系。封面、结尾、章节转场与内容页都适用这一判断；允许
有意识的纯排印停顿，但不能连续用 code_only/none 回避可成立的配图机会。

论文或课程附件中的 Figure/Table 不做裁图，也不能直接引用 `inputs/` 页图。先根据
OCR/提取稿记录的题名、caption、对象、标签和数字选择路径：能从论文官网、项目页、
作者页或其他可信来源精确找回同一视觉时检索；概念性主体可生成新的说明性画面；
数据、实验结果和机制关系必须由 Slide 按已核实数据用 HTML/CSS/SVG 重绘。生成图
不得冒充原论文 Figure、实验截图或精确数据图，无法忠实重建时应改用文字/代码视觉并
保留事实边界。

当可信位图能明显增强身份、场景、证据或氛围时，把 `preferred` 视为积极的视觉委托。
Image 应先尝试最可信的实现路径，只有存在具体理由时才跳过；随后 Slide 使用计划中的
降级方案。这样增强配图积极性，但不把素材数量变成硬配额。

一个 Image Agent 批量处理整册。先在唯一 `assets/catalog.md` 中登记选中的来源、
直接下载地址与目标路径，运行一次 `fetch-images` 批量取得真图，再运行
`assets-finalize` 并只看一张素材联系表。原图横竖方向不是硬门，只要目标 16:9
裁切仍然成立即可。已有本地路径直接复用，不要为了改文件名反复下载。
catalog 路径固定为工作区根目录相对的 `assets/NAME.ext`；被分配到页面的素材必须在
最终 HTML 中按该精确路径真实显示，不能添加 `../`、隐藏加载或只保留 asset ID。

`required` 页面若没有已解析的本地位图路径，或 HTML 没有真实引用该路径，不能 build
通过。透明背景先检查；只有脚本确认是烘焙进去的浅色棋盘格时才使用保守去除脚本。

## Slide 修订

Slide 先一次完成页面，再渲染真实 PNG，集中做一轮修复。首次成功 render 后必须先
看 PNG，修复产生的新 PNG 也必须复核。正常只使用这两次检查；渲染器保留最多八种
不同状态作为异常恢复上限，不是审美探索预算。预检失败的不同状态也计入，未变化状态
复用旧 PNG。仍有硬伤时回退到稳定构图或上报，让后续修复仍有合法入口。
Slide 只使用 `render . --page NN`；整册 build、渲染与渲染器诊断属于最终收口和
Review。单页预览正确、整册截图却不一致时，应上报截图链问题，不能为了适配错误帧而
改写页面。

只有可见缺陷才值得进入下一轮：裁切、重叠、溢出、媒体损坏、严重且非设计意图的空洞、
层级/对比不可读，或数据关系表达错误。页面已经清楚正确后，不再做纯审美探索。若多张
特殊页出现同一共享问题，应在 `finalize` 前上报并统一修一次，不由各 Slide 分别探索。
首次视觉检查先汇总一份 `must_fix` 清单，再用一次协调修改集中解决；复核通过就立即
结束，不以重复读取、搜索参考或另起构图绕开止损线。

全册默认使用同一基础入场与页面转场。GSAP 只用于用户明确要求的隔离 live-demo，
且静态首帧必须独立成立。

## Review

Review 是最终像素负责人：整册联系表与特殊页联系表各检查一次，之后只打开在这两次
检查或其他明确审计中被标记的页面。检查内容画布连续性、标题逻辑、听众口吻、图片
真实显示、页脚位置、遮挡裁切与讲稿对应。动手修改前先形成一份完整问题清单，把安全
修复作为一个批次完成，再统一 finalize 一次。Review 完成后，Orchestrator 不重复看
未变化的像素。未解决问题要先归类为页面创作、共享系统或截图链；`static` 元数据本身
不是缺陷，截图不一致也不能成为修改正确页面 HTML 的理由。
短 Deck 也不能因此逐页全开；联系表与确定性审计没有标红页时直接通过。

## 命令

```bash
python skills/mural-presenter-v0-2-zh/scripts/deck.py validate-plans . --expected N
python skills/mural-presenter-v0-2-zh/scripts/deck.py scaffold-from-plans . --expected N
python skills/mural-presenter-v0-2-zh/scripts/deck.py sync-speech . --expected N
python skills/mural-presenter-v0-2-zh/scripts/deck.py fetch-images .
python skills/mural-presenter-v0-2-zh/scripts/deck.py assets-finalize .
python skills/mural-presenter-v0-2-zh/scripts/deck.py render . --page NN
python skills/mural-presenter-v0-2-zh/scripts/deck.py finalize . --expected N
python skills/mural-presenter-v0-2-zh/scripts/deck.py audit .
```

只按需要读取参考：

- 规划：[plan-contract.md](references/plan-contract.md)
- 特殊页与内容页选择：[page-patterns.md](references/page-patterns.md)
- 字体：[fonts-and-type.md](references/fonts-and-type.md)
- 图片：[materials-and-images.md](references/materials-and-images.md)
- 图表与关系图：[charts-and-diagrams.md](references/charts-and-diagrams.md)
- DOM 契约：[html-contract.md](references/html-contract.md)
