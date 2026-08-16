# MURALPRESENTER：面向长程演示文稿的多智能体统一推理与创作

## 摘要

一张幻灯片可以局部正确却与整册矛盾：前页定义的术语在远处被改写，视觉编码反转，或数值前提被引用却从未建立。这类失败源于创作决策跨阶段、跨页持续，而执行上下文并不持续。因此，缺失的设计变量是责任粒度：哪些页面必须由同一主体共同负责。\textbf{MuralPresenter} 外置持久整册状态，将制作委派给依赖对齐、互不重叠的 slide group，每组由同一 Group Agent 联合生成、渲染与修改。四阶段工作流依次完成内容接地、冻结整册计划、并行的分组制作与整册复审。配套数据管线向三条 Query Track 注入 Style profile 和任务条件化的长程依赖，再将依赖编译为轨迹过滤的 checklist。我们以 1,000 条经质量控制的轨迹训练 MuralPresenter-9B，并以更大的异构队列训练 MuralPresenter-27B；同时提出 \textbf{ThreadBench}，分开评估中间过程证据与最终制品质量。在 PresentBench 上，MuralPresenter-9B 与 MuralPresenter-27B 的 Overall 分别为 \resulttbd{[TBD:ABS\_M9\_PB]} 和 \resulttbd{[TBD:ABS\_M27\_PB]}；在 ThreadBench 上，二者 Final 分别为 \resulttbd{[TBD:ABS\_M9\_THREAD]} 和 \resulttbd{[TBD:ABS\_M27\_THREAD]}。受控消融进一步表明 \resulttbd{[TBD:ABS\_ABLATION\_SUMMARY]}。这些结果说明，显式责任与可审计交接能支撑长程演示创作。

## 1 Introduction

演示文稿是持久决策对象。作者固定术语、选定颜色编码、设定数值前提并建立叙事弧——均在生产之前完成。这些决策在远处的页面上被消费，并在跨版本修改中被重新审视 \citep{maheshwari-etal-2024-presentations,zheng2025pptagent,xu-etal-2025-pregenie}。当消费出错，单页看似成立却与全局矛盾：现有评测已揭示要求遗漏、无依据陈述、数值错误与跨页不一致 \citep{chen2026presentbench}。

这种持久性带来两难。单 Agent 保留统一上下文，却不断累积执行产物，逐步削弱对早期决策的利用 \citep{sun2025contextfolding}。按阶段或逐页拆分隔离了子任务却割裂了责任；拆分能否带来收益取决于任务结构、信息共享与验证位置——而非 Agent 数量 \citep{kim2025scalingagents}。两种极端都未触及关键问题：哪些页面应共同承担一条依赖？

\textbf{MuralPresenter} 以 **slide group** 作答。Orchestrator 按叙事目的、设计亲缘与跨页依赖，把全部页面划入完整且不重叠的组。同一 Group Agent 生成、检查并修改组内所有页面；独立组并行执行；整册 Review 兜底跨组关系。持久状态被外置，使每个阶段读取投影而非原始历史；确定性的解析、下载、渲染与构建始终作为工具。Figure~\ref{fig:page-topologies} 对比此拓扑与顺序、逐页方案。HTML 是创作源，图片、PDF 与 PPTX 转换属于交付边界。

\input{figures/page_topologies}

定义 slide group 的依赖同样支配训练任务构造。三条 Query Track 汇入共享增强层，由该层统一注入 Style、构造与任务匹配的长程依赖并编译成 QC checklist；候选轨迹据此接受过滤。我们以受控的 1K 轨迹语料训练 MuralPresenter-9B，并以六个经质量控制与重加权的异构历史批次训练 MuralPresenter-27B（§4）。\textbf{ThreadBench}（Tracing Handoffs, Requirements, and Execution Across Deck Generation）评价过程–制品一致性，并在 Final Deck rubric 中包含跨页语义一致性；PresentBench 与 SlidesGen-Bench 补充通用质量，DECKBench 评估多轮修改。本文贡献如下：

1. **覆盖完整生命周期的 Skill 驱动框架**，明确划分确定性工具与隔离 Agent 上下文。
2. **依赖对齐的责任拓扑**，由同一 Group Agent 联合负责相关页面，修改仅按必要范围重放。
3. **可控合成管线与两个已训练模型**：MuralPresenter-9B 使用三 Track 构造的 1,000 条 post-QC 轨迹，MuralPresenter-27B 使用六个经质量控制的历史批次。
4. **ThreadBench**：面向中间交接与最终 deck 质量的 process-aware、artifact-grounded 评测，并与通用生成、多轮修改 benchmark 配套。

后续结构如下：§2 综述相关工作，§§3–4 介绍创作系统与数据管线，§§5–6 定义 ThreadBench 与评测协议，§§7–8 通过可追踪案例、有效性边界与结论收束全文。

## 2 Related Work

**演示文稿生成。** 内容选择方法关注每页应放什么 \citep{bandyopadhyay-etal-2024-enhancing-presentation,maheshwari-etal-2024-presentations}；Agent 系统进一步引入整册规划 \citep{zheng2025pptagent}、基于渲染的复审 \citep{xu-etal-2025-pregenie}、专门角色 \citep{xie2026slidebot}、迭代视觉优化 \citep{zheng2026deeppresenter}、讲稿联合与交付 \citep{yang2026deepslide}，以及设计–代码解耦 \citep{cui2026designfirst}。讲稿联合与交付支持属既有工作；MuralPresenter 的区分点在于以 slide group 归属跨页 source-to-target 关系。

**长程状态与 Agent 粒度。** 增长的历史在上下文窗口耗尽之前已妨碍对早期信息的利用 \citep{sun2025contextfolding}；外置已验证状态并使用新鲜上下文可稳定长轨迹 \citep{ma2026longhorizonharness}。由于拆分收益取决于任务结构与信息流 \citep{kim2025scalingagents}，MuralPresenter 按依赖结构而非流水线阶段或页面序号划定组边界。

**Agentic 数据与优化。** 完整交互轨迹保留规划决策与工具使用 \citep{zeng2023agenttuning,chen2024agentflan}，迭代反馈教授诊断与修复 \citep{madaan2023selfrefine}。MuralPresenter 依据 rollout 前注入的可机核依赖接收轨迹，并以受控消融检验 inspect–diagnose–patch 轨迹能否迁移到修改任务。

**评测基准。** PresentBench 评估材料接地生成 \citep{chen2026presentbench}；SlidesGen-Bench 覆盖 Content、Aesthetics 与静态 Editability \citep{yang2026slidesgenbench}；DECKBench 研究生成与多轮修改 \citep{jang2026deckbench}。PPTArena 与 PPT-Eval 检验复合或 PowerPoint 原生修改 \citep{ofengenden2025pptarena,gandhi2026ppteval}，LH-Bench 以 Skill 接地 rubric 评价过程与输出 \citep{gupta2026lhbench}。ThreadBench 将 case-specific 的 Research、Planning、Image 与 Slide-production 证据同 Knowledge、Deck 和 Page 质量分开评分，补充仅看最终产物的评测。
