---
name: mural-presenter-creative
description: MURAL Presenter 的强模型创作入口；保留附件、字体、素材、渲染与交付审计，同时把主题视觉命题、正文措辞和逐页构图权交给 Orchestrator 与单页 Slide Agent。
---

# MURAL Presenter · Creative / Teacher Profile

这是面向高能力模型的作者模式。目标不是减少质量要求，而是把约束放在确定性边界，把创意判断留给模型：**运行时管事实、来源、文件、像素新鲜度和交付；Agent 管叙事、艺术指导、正文措辞与构图。**

## 1. 不变的工程底盘

必须交付：`plan/grounded-knowledge.md`、`plan/design-brief.md`、`plan/deck.md`、全部 `plan/slide_NN.md`、`base.css`、逐页 HTML/PNG、`speech.md` 和 `present.html`。

- 有附件时按附件派 Material；完整读取 `subagents/material.md`，使用 `stage_materials.py`，保留全文/OCR、coverage、来源定位和 priority ledger。
- 外部事实确会改变结论时只派一个 Research，完整读取 `subagents/research.md`。
- 位图统一进入 `assets/catalog.json`，由 Image 获取或生成并完成素材审查；Slide 不自行搜图、生图或伪造路径。
- 数据用 ECharts；准确文字留在 HTML；论文 Figure 从原 PDF 高分辨率裁取，不把整页论文截图冒充主图。
- 最终页面必须由新鲜 PNG 验收；字体、渲染、播放器、附件来源和内容保真继续通过当前 `deck.py build/audit`。
- 修改已有 Deck 时仍遵循 `references/editing-contract.md`，不重做无关页面。

## 2. 创作授权

Orchestrator 是本册 Art Director，不先去风格库找一个安全模板。先完整读取 `references/creative-direction.md`，再按需要读取：

- 规划字段：`references/planning-contract.md`；
- 数据/机制/大型 SVG：`references/design-rules.md` §4–5 与 `references/layout-patterns.md` §9；
- 字体确需核验时：`references/fonts.md`；
- 最终验收：`references/quality-checklist.md` 中与当前页面直接相关的章节。

`design-styles.md` 与 resolved systems 是候选词汇，不是必填表。只有当一套系统真能增强当前主题时才读一套并转译；不得为了合规填写 `resolved_system_id`，也不得让风格编号替代原创视觉命题。

先从主题、附件、受众、场合和真实图像中提出一句 `visual_thesis`，再建立：

- 有来源的 `palette_anchor`，而不是默认白/米白、深蓝或霓虹科技；
- 2–4 个稳定页族和少量视觉峰值；
- 标题、正文、数字、注释的字体声音；
- 一种可跨页变化的 signature motif；
- 图片、数据图、机制图、强排印和呼吸页的节奏。

Style Lock 只冻结这套设计 DNA 与禁止项，不填写冗长设计表单，不锁死每页几何。

## 3. 内容与附件

Material/Research 完成后立即写 `plan/grounded-knowledge.md`。有附件时保留每个 `priority_id / screen_priority / source_locator / fidelity_form`；用户点名内容、主结论、关键数字/关系和决策证据标为 `must_present`。

每个 `must_present` 必须映射到至少一个逐页计划，并在最终页面以可读文案、图表、Figure 或图解出现。讲稿只能展开，不能代替上屏。

附件是事实边界和候选视觉，不是设计上限或图片配额：

- 清晰、不可替代的 Figure/人物/产品/作品/实验输出优先复用；
- 文字密集、低清或重复的附件图可以不用，但其中的重要事实不能消失；
- 附件没有好图时，仍可搜索真实图片、生成非事实性的氛围图，或用 SVG/Canvas/ECharts 重构；
- 学术、课件、组会和论文解读仍按现场演讲设计，不复刻论文排版。

## 4. 规划：冻结事实，不冻结页面生命力

Orchestrator 一次完成全册标题链、证据分配、视觉故事板和逐页计划。每个 `plan/slide_NN.md` 必须包含当前运行时所需机器字段，同时把创作边界写成轻量页面包：

- 页面职责与听众所得；
- 定版标题；
- `must_show` 事实、数字、关系、引用边界与 `attachment_priority_ids`；
- 可删减的 supporting 内容；
- 一个语义视觉目标、真实素材路径/asset ID、真实性和裁切保护；
- 建议的第一眼焦点与阅读动作；
- 讲稿节拍；
- 唯一 `production_group`。

正文区只写内容不变量和候选措辞，不把段落、卡片数、组件树或完整最终句式冻结成像素指令。Slide 可以压缩、拆分、合并和重写正文，只要不改变标题、事实、数字、限定条件、来源边界和 must-show 语义。

**每页必须拥有独立 production group，一页一个 Slide Agent。**一致性来自设计 DNA、scaffold、颜色/字体角色和页面锚点，不靠一个 Agent 批量复制几何。一次 `delegate_task` 可以并行派多页，但每个任务只拥有一页。

运行：

```bash
python ${SKILL_DIR:-skills/mural-presenter}/scripts/deck.py prepare . --expected <N>
```

## 5. 图片与页面调度

逐页先判断最准确的表达媒介：真实主体→真实图；概念/氛围→生成图；数据→ECharts；静态机制/关系→大型 SVG；自动布局/大量长标签→Canvas + HTML；纯排印只在文字本身能成为视觉事件时使用。

有位图需求时先完成 Image 与 catalog，再启动消费者页面。互不依赖的 Image 工作可并行；不为凑数配图，但不能把具名人物、产品、作品、场景或主结果 Figure 静默降级成微型图标和白卡片。

每个 Slide Agent 完整读取 `subagents/slide.creative.md`。它获得正文措辞和正文区构图权；只拥有一页，不读取兄弟 HTML。流程为：完整首稿 → render → Vision → 一轮合并修复；若页面技术正确但艺术指导明显未完成，可再做一次有明确目标的 aesthetic completion。最后保留像素更好的验证版本。

## 6. Review

所有页面完成后只保留一个 Review owner，读取 `subagents/review.creative.md`。首次 Review 若返回 blocked，运行时可在有限恢复预算内让同一 owner 复验；不得并行派多个 Review。Review 先看全册联系表，检查：

- 视觉命题是否真的可见；
- 页面是否属于同一世界但不复制同一几何；
- 封面、章节、峰值和结尾是否形成演讲节奏；
- 主视觉是否有体量，附件重点和证据是否真实上屏；
- 是否退回白底小标题、等权卡片墙、文档截图或无职责留白；
- 裁切、溢出、字体、图表、来源和最终像素是否正确。

Review 维护 `_trace/review-issues.md` 与附件任务的 `_trace/content-fidelity.md`，只做一轮集中修复。需要新事实或新素材才能解决时返回 blocked，交回原单页 Agent；不另起无边界审美循环。

最终执行：

```bash
python ${SKILL_DIR:-skills/mural-presenter}/scripts/deck.py build . --expected <N>
```

build 后重新查看最终联系表。完成后只返回简短交付总结。

## 7. 红线

- 不伪造数字、引用、人物、产品、论文结果或素材来源。
- 不因讲稿存在而删除附件主结论和用户点名内容。
- 不把内部 ID、路径、production group、设计说明或状态标签显示给听众。
- 不用生成图伪造数据图表或含准确文字的证据图。
- 不为通过 lint 缩小主视觉、删除信息或压低字号；先以最终像素确认真实缺陷。
- 不把“专业、学术、极简”解释成低设计完成度。
