# 3. MuralPresenter

Figure~\ref{fig:release-workflow} 给出系统总览。MuralPresenter 先外置任务与整册状态，再仅把能独立交付与验收的工作交给新鲜 Agent 上下文，将存在依赖的页面绑定给共享 Group Agent，最后以渲染接地的整册复审闭合剩余关系。§§3.1–3.3 依次形式化任务与状态投影、规定角色边界，并定义分组、视觉批判与按范围修改。

## 3.1 长程演示创作任务

我们把演示文稿创作建模为一个交互式智能体任务。给定用户请求 \(q\)、可选附件 \(\mathcal{M}\)、语言 \(\ell\) 与目标页数 \(N\)，系统在工具库 \(\mathcal{T}\) 与持久状态 \(\mathcal{S}\) 构成的环境中运行，产出演示文稿 \(D=(s_1,\ldots,s_N)\) 连同逐页讲稿与渲染结果。

生成过程是一条多步轨迹 \(\tau\)，按创作生命周期分解为四个阶段：
\[
\tau \;=\; \tau^{\mathrm{ground}} \circ \tau^{\mathrm{plan}} \circ \tau^{\mathrm{gen}} \circ \tau^{\mathrm{review}},
\]
分别对应内容接地、整册规划、分组生成与整册复审。前一阶段的输出未在 \(\mathcal{S}\) 中就绪时，依赖它的下游不启动；生成阶段进一步分解为并行的页面组轨迹 \(\tau^{\mathrm{gen}}=\{\tau^{G_1},\ldots,\tau^{G_m}\}\)，其中 \(\{G_1,\ldots,G_m\}\) 是 \(\{1,\ldots,N\}\) 的完整划分（§3.3）。

状态外化为上述阶段排序提供依据。每个被接受的产物 \(d\) 以固定的产生阶段与归属角色进入 \(\mathcal{S}\)。角色 \(r\) 只读取其声明的投影：
\[
\pi_r(\mathcal{S}) \;\triangleq\; \{d \in \mathcal{S} \mid d \text{ 属于 } r\text{ 声明的读集}\},
\]
模型生成的写入仅面向其声明的写集。在实现中，\(\mathcal{S}\) 对应 deck 持久状态目录，每个 Skill 的角色卡声明 \(\pi_r\)，委派拓扑决定新上下文接收哪些产物；确定性脚本仍可代任何角色派生或更新共享状态。因此，该投影形式化的是委派契约——每个角色负责读取和产出什么——而非硬性文件系统隔离。

该任务是**长程**的：开篇主线须在结尾回收，前文定义的术语与视觉编码须在后续沿用。我们把这种关系记为跨页依赖 \(g_k=(a_k,T_k,\Phi_k)\)：源页面 \(a_k\) 建立决策，目标集合 \(T_k\) 消费它，判据 \(\Phi_k\) 检验关系是否成立。整册一致性由这些依赖是否闭合来刻画（§5）；MuralPresenter 的设计核心在于在哪些生命周期边界让哪些角色对跨页依赖共同负责。

## 3.2 Skill 驱动的多 Agent 协作

MuralPresenter 把职责表达为可插拔的 Skill，规定每个角色何时触发、读取哪部分状态投影、产出什么以及交回的判据。是否为一项工作单独开 Agent，取决于该工作能否被单独交付与验收；达不到门槛时，Orchestrator 直接以工具完成。

**Orchestrator.** 贯穿全程，解析用户意图、协调接地、规划整册、委派生产并验收结果，但不搜图、不写页面、不判像素。确定性操作（解析、下载、渲染、构建）始终作为工具直接调用。每页讲稿在规划阶段确定，并由确定性步骤在生产启动前编译为统一演讲产物。Group Agent 仅实现已冻结的可见内容契约，不改写讲稿。经渲染接地的整册 Review 后，最终确定性步骤重新同步演讲产物。

**Material \& Research.** Material 把附件并行解析为结构化摘要；Research 只在外部事实会改变结论时启动核验，两者共同汇成供全局引用的接地知识。无附件且无待核事实时，两者均不出现。

**Image.** 仅在页面计划确需照片或生成图时启动，将检索、生成、候选比对与像素核查从全局轨迹中隔离。

**Group Agent.** 每个 Group Agent 负责一个页面组，在有界轨迹 \(\tau^{G_j}\) 内联合生成并检查组内所有页面（§3.3），维护组内关系而无需与其他组协商。互不依赖的组并行执行。

**Review.** 生成汇合后，未参与生成的 Review 在隔离上下文中对整册做独立复审，以避免自评偏袒（§3.3）。之后构建工具负责打包与一致性校验。Figure~\ref{fig:release-workflow} 将这些责任边界串联为完整的创作、修改与交付生命周期。

\input{figures/release_workflow}

## 3.3 分组、视觉批判与修改闭环

**依赖对齐的分组。** MuralPresenter 按叙事、设计与素材亲缘，把页面集合划分为完整、不重叠的若干组 \(\{G_1,\ldots,G_m\}\)。整册依赖闭合率分解为：
\[
\mathrm{DC}(D)=\underbrace{\frac{1}{K}\!\!\sum_{k:\,\exists j,\,\{a_k\}\cup T_k\subseteq G_j}\!\!\mathbf{1}[\Phi_k\ \text{闭合}]}_{\text{组内，由单一 Group Agent 闭合}}
\;+\;
\underbrace{\frac{1}{K}\!\!\sum_{k:\,\nexists j,\,\{a_k\}\cup T_k\subseteq G_j}\!\!\mathbf{1}[\Phi_k\ \text{闭合}]}_{\text{跨组，由整册复审兜底}}.
\]
分组目标是让尽可能多的强依赖落入组内，只留少数跨组关系给整册复审；组内与跨组闭合的差异在实验中直接测量（§6）。互不依赖的组并行生成。

**两级视觉批判。** 演示的许多毛病只在渲染时现形。MuralPresenter 把查看渲染像素设为一等操作，分两级执行。组内级：Group Agent 每写一页就渲染、修改，直到页面在像素上成立，再以联系表检查节奏与跨页重复。整册级：Review 在隔离上下文里重看整册渲染；同一独立批判思路也贯穿数据合成（§4）。复审刻意克制：确定性渲染只对 CJK 字体崩坏设硬门，其余信号均作为建议交回。

**按范围的修改闭环。** 生成后的修改复用同一持久状态。给定一次编辑，路由保守推断影响集 \(I\)：直接触及的页面加上经已知跨页关系牵连的页面。设声明的责任单元为 \(\mathcal{U} = \bigl\{\{i\} \mid i \in [N]\bigr\} \cup \{G_1,\ldots,G_m\} \cup \{[N]\}\)，以包含关系为序；路由策略选择：
\[
\mathrm{scope}(I) \;=\; \min_{U \in \mathcal{U}}\; U \quad \text{s.t.} \quad I \subseteq U.
\]
此公式形式化的是已实现的路由策略，而非学习型优化器：系统不保证全局最小代价或完备的依赖知识，影响不明时向更大单元升级。具体而言：单页修改由 Orchestrator 直接处理；触及组内关系则重新激活对应 Group Agent \(\tau^{G_j}\)；需要新事实或素材则按需重启 Research 或 Image；增删页、改动页序或标题链则更新划分并触发整册复审。修改质量与未涉及页面的保持均由多轮实验检验（§6）。最终产物是结构化 HTML，可导出为图片、PDF 与 PPTX。

# 4. 长程 Agentic 数据合成

本节介绍数据流水线：任务构建、以外部验证激发反思行为的轨迹合成，以及多阶段过滤（Figure~\ref{fig:data-synthesis}）。下文区分受控的 9B 语料与规模更大的 27B 异构训练队列。

\begin{figure}[H]
\centering
\begin{tikzpicture}
\node[inner sep=0] (datafig) {\includegraphics[width=\linewidth]{figures/fig3_long_horizon_agentic_data_synthesis.png}};
\node[fill=white,draw=auditorange,rounded corners=1.5pt,inner xsep=4pt,inner ysep=2pt,font=\scriptsize\sffamily,text=auditorange] at ($(datafig.south west)!0.493!(datafig.south east)+(0,0.43cm)$) {MuralPresenter-9B corpus};
\end{tikzpicture}
\caption{\textbf{长程 Agentic 数据合成。} 三条 Query Track 汇入共享的任务增强层：统一注入 Style profile，构造与任务内容相匹配的长程依赖，并将依赖编译为后续 QC 使用的 checklist。Skill 引导的 rollout 保留规划、分组制作与复审过程；隔离的视觉 critic 以真实渲染触发修改；分层过滤仅保留结构完整且逐项闭合 checklist 的轨迹。该受控管线产出 MuralPresenter-9B 语料；27B 队列具有不同的教师来源与数据规模。}
\label{fig:data-synthesis}
\end{figure}

## 4.1 Query 构建

高质量长程训练要求任务在整册范围内具有可检查的结构。对于 MuralPresenter-9B，我们先从三条互补 Track 构造候选 query，再进入共享的任务增强层。系统从统一风格 taxonomy 中采样 Style profile，将主色、字体气质、装饰母题与留白策略等具体指令织入需求；随后根据任务内容构造长程依赖，包括分节配额、叙事承接、跨页数值一致、视觉母题延续、首尾闭环与显式跨页引用等。本文不以固定数量的约束定义长程性：每项依赖均保留类型、具体要求和闭合条件，并进一步编译成一条 \texttt{longhorizon\_checklist}，同时写明具体要求与后续 QC 的核验方法。

**Track 1：领域种子扩展。** 从层级化领域池出发，扩展出覆盖广泛学科与场景的任务，侧重广度与多样性。

**Track 2：人格驱动的真实场景（PersonaHub \(\times\) O*NET）。** 按 O*NET 职业体系匹配演讲者人格并生成 query，分两档：日常通用场景与专家精英场景，后者带有更强领域深度与更长依赖跨度。试点从 84 条初始组合出发，清洗后保留覆盖 65 个职业的 82 条，其中 20 条形成端到端 query。

**Track 3：附件驱动的反向合成。** 该 Track 从真实文档反向构造"带附件"任务，要求系统接地、引用与重组材料。

三条 Track 合流后，共用同一套 Style taxonomy、输出 schema、长程依赖表示、checklist 编译器与去重策略。最终任务包同时包含面向用户的 query、注入后的 Style profile、显式长程依赖及其 QC checklist；rollout 与过滤因此沿用同一份依赖合约，而不在生成结束后重新猜测验收条件。

## 4.2 Skill 引导的轨迹合成

给定任务，教师模型在与 §3 完全相同的 MuralPresenter 流程下自主生成整套演示，把规划、委派、渲染检查、诊断、补丁与重渲全部保留在轨迹里——监督对象是完整的可执行创作过程，而非仅仅是最终 HTML。

为避免自我验证偏差，我们引入外部验证：独立视觉 critic 在隔离上下文中评判渲染产物，指出溢出、破图、低对比等像素级缺陷，并给出可执行的修改建议注入轨迹。由此，轨迹便带有以真实观察为依据的反思行为。

**受控的 9B 语料。** 为检验小规模、可明确复现的轨迹数据能否教授完整工作流，我们构建受控实验。DeepSeek-V4-Flash 执行创作流程作为 rollout 主模型，Gemini-3.5-Flash 基于渲染结果提供视觉批判。经确定性校验与质量过滤后，从 \resulttbd{[TBD:9B\_TASKS]} 个任务中保留恰好 1,000 条通过 QC 的训练轨迹。我们以 Qwen-3.5-9B 为基座训练 \resulttbd{[TBD:9B\_EPOCH]} 轮，上下文长度为 \resulttbd{[TBD:9B\_CTX]}，使用 \resulttbd{[TBD:9B\_GPU]} GPU，共消耗 \resulttbd{[TBD:9B\_GPUH]} GPU 小时。

**已核验的 27B 异构队列。** MuralPresenter-27B 通过全参数监督微调获得，训练数据是在同一创作流程下主要由更高能力的闭源前沿教师模型生成、并经质量过滤的完整智能体轨迹。轨迹保留规划、工具调用、渲染检查、诊断、修改与重渲染过程，而非仅保留最终 HTML。受商业保密约束，本文不披露教师模型的具体身份，但完整报告数据处理流程、语料统计、优化配置与训练算力。该队列来自六个异构历史批次，经 QC-v2 过滤后含 17,171 条任务与 304,275 条有效轨迹；其中 16,073 条任务 / 293,846 条轨迹通过结构核验进入 SFT 候选集，剔除异常与不完整样本后保留 293,436 条基础轨迹；经 Mixture-v2 重采样后训练暴露量 318,422 次，内容 token 数 7.3127B，packed 序列数 56,458 条。Gemini-3.5-Flash 在该队列中仅用于事后质量评分，而非 rollout 教师或 critic。该队列不与 9B 目标集共享教师来源。

## 4.3 轨迹过滤

多阶段过滤保证数据质量：确定性检查核验必需产物、页面覆盖、构建与渲染；结构与约束检查逐条核验跨页要求与用户约束；质量评分对内容与视觉一致性打分；轨迹级清洗去除异常、不完整与近重复样本。失败或不可核验的轨迹仍计入统计，但不会被改写为可用数据。我们区分 query、rollout、完成与被接收四类计数，在数据卡中报告各阶段留存与拒绝原因，并在 §6 汇总过滤后规模。
