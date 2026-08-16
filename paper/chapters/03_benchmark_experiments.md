# 5. ThreadBench

## 5.1 过程–制品评测

一套视觉精美的 deck 可以掩盖断裂的交接：研究证据可能从未进入规划，规划中的图片可能从未使用，渲染中发现的缺陷也可能从未修复。仅看最终页面无法定位在哪一环断裂。ThreadBench 因此把 case-specific 的过程证据与最终 deck 分开评价。它是 process-aware、artifact-grounded benchmark，但不声称覆盖全部工程环节。

\begin{figure}[H]
\centering
\includegraphics[width=\linewidth]{figures/fig4_threadbench_anatomy.pdf}
\caption{\textbf{ThreadBench 的评测结构。} 每条任务专属需求定义一项跨越整册、可观察的 source-to-target 闭合规则。Intermediate 沿 Research、Planning、Image 与 Slide Production 检查锚定的过程证据；Final 则从整册和逐页两个粒度检查 canonical render。两层分别输出分数，不合并为单一指标。图中的动物导航需求链来自当前正式参考 case。}
\label{fig:threadbench-anatomy}
\end{figure}

## 5.2 需求链与双层 Rubric

Figure~\ref{fig:threadbench-anatomy} 明确了评测单元。需求链从 source requirement 或 anchor 出发，指定远处 target 与可观察的闭合规则，可包含事实、术语、叙事、设计或任务依赖。Intermediate 以 criterion-scoped packet 检查 Research、Planning、Image 和 Slide Production 四个维度下的七类 criterion，每次只暴露与当前单元相关的锚定证据。Final 面向 canonical artifact 分成两组：Deck 将 Knowledge checklist 与六个整册维度结合，Page 则对每个 canonical 页面评价五个视觉与技术维度。完整 criterion 清单和维度定义见附录~\ref{app:protocol}。

## 5.3 评分与发布边界

Judge 先将证据匹配到显式 anchor，再由代码派生 0/0.5/1（Knowledge 为 binary）。Deck 与 Page 各执行一次最弱维度衰减，Intermediate 不衰减：

\[
G_{\mathrm{eff}}=G_{\mathrm{base}}[1-0.3(1-G_{\min})],\quad
F=\tfrac12(Deck_{\mathrm{eff}}+Page_{\mathrm{eff}}),\quad
I=\operatorname{mean}(R,P,I_m,S).
\]

其中 \(I_m\) 表示 Image 维度，Intermediate 只计入适用维度。正式发布的 Intermediate \(I\) 与 Final \(F\) 字段相互独立，不合并为单一标量。当前 v004 可执行合约规定一个正式参考 case（\url{Education/Education-lh-Animal_navigation}）。N/A 语义、major 缺陷扣分、可选且不计分的 Preaudit 及人类–Judge 校准均移至附录~\ref{app:protocol}；多领域扩展与显式 span 诊断不在当前发布范围内。

# 6. Experiments

我们围绕四个研究问题组织实验：**RQ1（通用质量）** MuralPresenter 生成的完整 deck 在通用质量评测上处于什么水平？**RQ2（多轮修改）** 在没有专门编辑训练的情况下，它能否完成多轮修改？**RQ3（过程–制品一致性）** 生产链路是否将需求和证据从中间阶段保持到最终 deck？**RQ4（设计选择）** 委派边界、责任拓扑、两级视觉批判与轨迹监督各自贡献多少？

## 6.1 实验设置

**MuralPresenter-9B。** 我们以 DeepSeek-V4-Flash 为 rollout 主模型、Gemini-3.5-Flash 为视觉 critic，在 \resulttbd{[TBD:9B\_TASKS]} 个训练任务上采样轨迹（§4），经质量过滤后保留恰好 1,000 条通过 QC 的训练轨迹。我们使用 MS-SWIFT~\citep{zhao2024mswift} 对 Qwen-3.5-9B 做监督微调：\resulttbd{[TBD:9B\_EPOCH]} 个 epoch、全局批次 32、学习率 \(1\times10^{-5}\)、最大上下文 \resulttbd{[TBD:9B\_CTX]} tokens，并在 \resulttbd{[TBD:9B\_GPU]} 张 A800 上训练 \resulttbd{[TBD:9B\_GPUH]} GPU 小时。

**数据与训练（已核实的 27B）。** MuralPresenter-27B 使用 §4 所述闭源教师轨迹队列：293{,}436 条基础轨迹经重采样与长度过滤形成 318{,}422 条训练曝光、约 73.1 亿个内容 token 和约 56{,}458 条 128K packed 序列。用 MS-SWIFT 的 Megatron 后端对 Qwen-3.5-27B 做全参数监督微调：上下文 128K、1 个 epoch、全局批次 32、初始学习率 \(2\times10^{-5}\) 经 5\% 线性预热后以余弦策略衰减至 \(1\times10^{-6}\)；4 节点共 32 张 80GB GPU，配置 TP=4、CP=2、DP=4、micro-batch=1、梯度累积 8，冻结视觉编码器与模态对齐器。训练共 1{,}764 个优化步、约 50.3 小时、合计约 1{,}604 GPU 小时。Gemini-3.5-Flash 在此仅作 QC 阶段的质量评分。完整数据与模型台账见附录~\ref{app:protocol}。

**基线。** ThreadBench 保留六个强通用模型对照：Opus-4.7、Sonnet-5、GPT-5.6-luna、Gemini-3.5-Flash、Qwen-3.7-plus 与 Kimi-k3。已有 benchmark 的重跑采用精简五系统组：9B 合成管线（DeepSeek-V4-Flash + Gemini-3.5-Flash）、未微调 Qwen-3.5-9B 与 Qwen-3.6-27B 对照，以及我们的 9B 和 27B。五者共享同一 benchmark 输入与 adapter。为显示模型在各外部 benchmark 上的绝对位置，Tables~\ref{tab:general}--\ref{tab:edit} 另设 \emph{reported} 分块，列入原论文中的代表性领先系统。这些数值沿用各自来源协议，不是本文重跑结果，也不与统一重跑组混合统计或进入显著性检验；完整 reported 表见附录~\ref{app:protocol}。

**解释边界。** 9B 与 27B 在教师来源、数据规模、基座模型和训练配方上均不相同，因此二者只作为两种系统配置分别报告，不将分数差异解释为受控的模型扩展或数据效率结论。方法与组件消融（RQ4）在 9B 受控设置下进行，使用同一 1K 语料并固定其余训练变量。ThreadBench 中与闭源前沿系统的对比属端到端系统比较，不构成与教师无关的泛化性证据。

**评测口径。** PresentBench~\citep{chen2026presentbench} 与 SlidesGen-Bench~\citep{yang2026slidesgenbench} 衡量通用生成质量，DECKBench~\citep{jang2026deckbench} 衡量多轮修改，ThreadBench（§5）通过独立的 Intermediate 和 Final 分数及维度级诊断衡量过程–制品一致性。有界的质量、相似度、成功率与 ThreadBench 分数均按越高越好解读；SlidesGen-Bench PEI 保持为有序的可编辑性等级，带符号的 DECKBench 变化量则保留官方协议的方向，不与绝对质量分数合并。其余缩放与聚合均遵循官方定义；所有运行均冻结并记录指标版本、方向、聚合代码与哈希。完整维度与次要诊断见附录~\ref{app:protocol}。

## 6.2 主要结果

**通用生成质量（RQ1）。** Table~\ref{tab:general} 将五系统重跑组与原论文中的代表性领先系统并列，报告 PresentBench Overall 及 SlidesGen-Bench Content、Aesthetics 和 Editability（PEI）。完整子维度与 reported 系统见附录~\ref{app:protocol}。该表回答通用 deck 质量水平，不对长程一致性下结论。

\begin{table}[!htbp]
\centering
\small
\caption{RQ1：原论文代表性领先系统（按来源协议 reported）与本文五系统统一重跑在 PresentBench 和 SlidesGen-Bench 上的结果。完整 reported 系统与维度见附录~\ref{app:protocol}。}
\label{tab:general}
\begingroup
\let\texttt\resulttbd
\setlength{\tabcolsep}{4pt}
\begin{tabularx}{\linewidth}{@{}>{\raggedright\arraybackslash}Xcccc@{}}
\toprule
& PresentBench & \multicolumn{3}{c}{SlidesGen-Bench} \\
\cmidrule(lr){2-2}\cmidrule(lr){3-5}
System & Overall & Content & Aesthetics & Editability (PEI) \\
\midrule
\multicolumn{5}{l}{\emph{原论文参考（reported）}} \\
NotebookLM       & 62.5 & 74.21 & 22.82 & L0 \\
Zhipu            & ---  & 88.29 & 22.06 & L2 \\
Skywork-Banana   & ---  & 83.83 & 27.28 & L1 \\
Quark            & ---  & 81.40 & 16.86 & L3 \\
\midrule
\multicolumn{5}{l}{\emph{统一重跑（相同输入与 adapter）}} \\
9B 合成管线 (DS-V4 + Gemini-3.5) & \resulttbd{[TBD:G\_TC\_PB]} & \resulttbd{[TBD:G\_TC\_SGC]} & \resulttbd{[TBD:G\_TC\_SGA]} & \resulttbd{[TBD:G\_TC\_SGE]} \\
Qwen-3.5-9B（未微调）  & \resulttbd{[TBD:G\_Q9\_PB]}  & \resulttbd{[TBD:G\_Q9\_SGC]}  & \resulttbd{[TBD:G\_Q9\_SGA]}  & \resulttbd{[TBD:G\_Q9\_SGE]} \\
Qwen-3.6-27B（未微调） & \resulttbd{[TBD:G\_Q27\_PB]} & \resulttbd{[TBD:G\_Q27\_SGC]} & \resulttbd{[TBD:G\_Q27\_SGA]} & \resulttbd{[TBD:G\_Q27\_SGE]} \\
MuralPresenter-9B  & \resulttbd{[TBD:G\_M9\_PB]}  & \resulttbd{[TBD:G\_M9\_SGC]}  & \resulttbd{[TBD:G\_M9\_SGA]}  & \resulttbd{[TBD:G\_M9\_SGE]} \\
MuralPresenter-27B & \resulttbd{[TBD:G\_M27\_PB]} & \resulttbd{[TBD:G\_M27\_SGC]} & \resulttbd{[TBD:G\_M27\_SGA]} & \resulttbd{[TBD:G\_M27\_SGE]} \\
\bottomrule
\end{tabularx}
\endgroup
\end{table}

Reported 分块把公开前沿锚定为：PresentBench Overall 62.5（NotebookLM）、Content 88.29（Zhipu）、Aesthetics 27.28（Skywork-Banana）和 PEI L3（Quark）。MuralPresenter-9B 的 PresentBench Overall 为 \resulttbd{[TBD:G\_M9\_PB]}，MuralPresenter-27B 为 \resulttbd{[TBD:G\_M27\_PB]}，统一重跑组中的最强对照为 \resulttbd{[TBD:G\_BEST\_CONTROL\_PB]}。在 SlidesGen-Bench 上，两种模型的 Content/Aesthetics/PEI 三元组分别为 \resulttbd{[TBD:G\_M9\_TRIPLE]} 和 \resulttbd{[TBD:G\_M27\_TRIPLE]}。相对 reported 前沿，\resulttbd{[TBD:G\_REPORTED\_COMPARISON]}；在受控重跑组内，结果表明 \resulttbd{[TBD:G\_RQ1\_INTERPRETATION]}。两类比较均不用于推断长程一致性。

**多轮修改（RQ2）。** Table~\ref{tab:edit} 将同一五系统重跑组与 DECKBench 原论文中的代表性配置并列，报告 Deck Fidelity、Layout Quality、Transition Similarity、\(\Delta\)-DTW 与 \(\Delta\)-Transition Similarity。PPT-Eval~\citep{gandhi2026ppteval} 仍是补充迁移测试，并列出其 reported 领先参考。原论文完整字段见附录~\ref{app:protocol}。inspect--diagnose--patch 行为迁移是一项由 RQ4 检验的假设。

\begin{table}[!htbp]
\centering
\footnotesize
\caption{RQ2：DECKBench 原论文代表性配置与本文五系统统一重跑；PPT-Eval 仅作补充且保持独立协议。完整 reported 字段见附录~\ref{app:protocol}。}
\label{tab:edit}
\begingroup
\let\texttt\resulttbd
\setlength{\tabcolsep}{3pt}
\begin{tabularx}{\linewidth}{@{}>{\raggedright\arraybackslash}Xccccc@{}}
\toprule
& Deck & Slide & Deck & \multicolumn{2}{c}{Multi-turn} \\
\cmidrule(lr){2-2}\cmidrule(lr){3-3}\cmidrule(lr){4-4}\cmidrule(lr){5-6}
System & Fidelity & Layout Q. & Trans.\ Sim. & \(\Delta\)-DTW & \(\Delta\)-Trans.\ Sim. \\
\midrule
\multicolumn{6}{l}{\emph{原论文参考（reported）}} \\
Auto-Slides (GPT-4o) & 0.591 & 0.998 & 0.858 & --- & --- \\
DECKBench (GPT-4o)   & 0.584 & 1.000 & 0.879 & --- & --- \\
GPT-5.1 模拟器 + GPT-5-mini 编辑器 (High) & --- & --- & --- & 0.02278 & 0.03230 \\
Kimi K2 模拟器 + DeepSeek 编辑器 (High)   & --- & --- & --- & 0.01549 & 0.04287 \\
\midrule
\multicolumn{6}{l}{\emph{统一重跑（相同输入与 adapter）}} \\
9B 合成管线 (DS-V4 + Gemini-3.5) & \resulttbd{[TBD:E\_TC\_DFID]} & \resulttbd{[TBD:E\_TC\_SLQ]} & \resulttbd{[TBD:E\_TC\_DTSIM]} & \resulttbd{[TBD:E\_TC\_MDDTW]} & \resulttbd{[TBD:E\_TC\_MDTS]} \\
Qwen-3.5-9B（未微调）  & \resulttbd{[TBD:E\_Q9\_DFID]}  & \resulttbd{[TBD:E\_Q9\_SLQ]}  & \resulttbd{[TBD:E\_Q9\_DTSIM]}  & \resulttbd{[TBD:E\_Q9\_MDDTW]}  & \resulttbd{[TBD:E\_Q9\_MDTS]} \\
Qwen-3.6-27B（未微调） & \resulttbd{[TBD:E\_Q27\_DFID]} & \resulttbd{[TBD:E\_Q27\_SLQ]} & \resulttbd{[TBD:E\_Q27\_DTSIM]} & \resulttbd{[TBD:E\_Q27\_MDDTW]} & \resulttbd{[TBD:E\_Q27\_MDTS]} \\
MuralPresenter-9B  & \resulttbd{[TBD:E\_M9\_DFID]}  & \resulttbd{[TBD:E\_M9\_SLQ]}  & \resulttbd{[TBD:E\_M9\_DTSIM]}  & \resulttbd{[TBD:E\_M9\_MDDTW]}  & \resulttbd{[TBD:E\_M9\_MDTS]} \\
MuralPresenter-27B & \resulttbd{[TBD:E\_M27\_DFID]} & \resulttbd{[TBD:E\_M27\_SLQ]} & \resulttbd{[TBD:E\_M27\_DTSIM]} & \resulttbd{[TBD:E\_M27\_MDDTW]} & \resulttbd{[TBD:E\_M27\_MDTS]} \\
\midrule
\multicolumn{6}{l}{\emph{PPT-Eval（补充，官方口径，不与上表比较）}} \\
& \multicolumn{2}{c}{Success Rate} & \multicolumn{3}{c}{Avg.\ Partial Score} \\
Claude-4.5-Opus（reported） & \multicolumn{2}{c}{45\%} & \multicolumn{3}{c}{57\%} \\
MuralPresenter-27B & \multicolumn{2}{c}{\resulttbd{[TBD:E\_M27\_PESR]}} & \multicolumn{3}{c}{\resulttbd{[TBD:E\_M27\_PEPS]}} \\
\bottomrule
\end{tabularx}
\endgroup
\end{table}

DECKBench reported 分块直接保留真实系统与配置，而不拼接一个虚构的逐指标最优系统：Auto-Slides 给出表中最高 Fidelity，DECKBench（GPT-4o）给出最高 Layout 与 Transition Similarity，两组 High-granularity 模拟器/编辑器配置提供多轮参考。在统一重跑中，相较未微调 Qwen-3.5-9B，MuralPresenter-9B 的 Deck Fidelity 与 Layout Quality 变化为 \resulttbd{[TBD:E\_M9\_SUMMARY]}；相较 27B 对照，MuralPresenter-27B 的对应变化为 \resulttbd{[TBD:E\_M27\_SUMMARY]}。多轮变化量显示 \resulttbd{[TBD:E\_MULTITURN\_INTERPRETATION]}；相对 reported 外部参考，\resulttbd{[TBD:E\_REPORTED\_COMPARISON]}。PPT-Eval 相对 Claude-4.5-Opus 的 45\%/57\% reported 参考得到 \resulttbd{[TBD:E\_PPTEVAL\_INTERPRETATION]}。结合 RQ4，这些结果支持关于 inspect--diagnose--patch 监督迁移的结论：\resulttbd{[TBD:E\_RQ2\_CONCLUSION]}。

**过程–制品一致性（RQ3）。** Table~\ref{tab:thread} 报告 ThreadBench 的 Intermediate 和 Final 分数及可解释的组级分列（Deck 和 Page 有效分）。二者为 v004 评分合约（§5.3）的两个独立输出。所有系统的维度级诊断见附录~\ref{app:protocol}。

\begin{table}[!htbp]
\centering
\small
\caption{RQ3：ThreadBench 过程–制品一致性。Intermediate 和 Final 为独立 0–1 分；Deck 和 Page 为 Final 两子组经最弱维度衰减后的有效分。}
\label{tab:thread}
\begingroup
\let\texttt\resulttbd
\setlength{\tabcolsep}{5pt}
\begin{tabularx}{\linewidth}{@{}>{\raggedright\arraybackslash}Xcccc@{}}
\toprule
& \multicolumn{1}{c}{Intermediate} & \multicolumn{3}{c}{Final} \\
\cmidrule(lr){2-2}\cmidrule(lr){3-5}
System & Score & Score & Deck\(_{eff}\) & Page\(_{eff}\) \\
\midrule
Opus-4.7            & \resulttbd{[TBD:T\_OP\_INT]}  & \resulttbd{[TBD:T\_OP\_FIN]}  & \resulttbd{[TBD:T\_OP\_DECK]} & \resulttbd{[TBD:T\_OP\_PAGE]} \\
Sonnet-5            & \resulttbd{[TBD:T\_SN\_INT]}  & \resulttbd{[TBD:T\_SN\_FIN]}  & \resulttbd{[TBD:T\_SN\_DECK]} & \resulttbd{[TBD:T\_SN\_PAGE]} \\
GPT-5.6-luna        & \resulttbd{[TBD:T\_LU\_INT]}  & \resulttbd{[TBD:T\_LU\_FIN]}  & \resulttbd{[TBD:T\_LU\_DECK]} & \resulttbd{[TBD:T\_LU\_PAGE]} \\
Gemini-3.5-Flash    & \resulttbd{[TBD:T\_GM\_INT]}  & \resulttbd{[TBD:T\_GM\_FIN]}  & \resulttbd{[TBD:T\_GM\_DECK]} & \resulttbd{[TBD:T\_GM\_PAGE]} \\
Qwen-3.7-plus       & \resulttbd{[TBD:T\_QW\_INT]}  & \resulttbd{[TBD:T\_QW\_FIN]}  & \resulttbd{[TBD:T\_QW\_DECK]} & \resulttbd{[TBD:T\_QW\_PAGE]} \\
Kimi-k3             & \resulttbd{[TBD:T\_KM\_INT]}  & \resulttbd{[TBD:T\_KM\_FIN]}  & \resulttbd{[TBD:T\_KM\_DECK]} & \resulttbd{[TBD:T\_KM\_PAGE]} \\
\midrule
MuralPresenter-9B  & \resulttbd{[TBD:T\_M9\_INT]}  & \resulttbd{[TBD:T\_M9\_FIN]}  & \resulttbd{[TBD:T\_M9\_DECK]} & \resulttbd{[TBD:T\_M9\_PAGE]} \\
MuralPresenter-27B & \resulttbd{[TBD:T\_M27\_INT]} & \resulttbd{[TBD:T\_M27\_FIN]} & \resulttbd{[TBD:T\_M27\_DECK]} & \resulttbd{[TBD:T\_M27\_PAGE]} \\
\bottomrule
\end{tabularx}
\endgroup
\end{table}

MuralPresenter-9B 的 Intermediate/Final 为 \resulttbd{[TBD:T\_M9\_INT]}/\resulttbd{[TBD:T\_M9\_FIN]}，MuralPresenter-27B 为 \resulttbd{[TBD:T\_M27\_INT]}/\resulttbd{[TBD:T\_M27\_FIN]}。相对最强通用基线，两者的对应差值为 \resulttbd{[TBD:T\_M9\_DELTA]} 与 \resulttbd{[TBD:T\_M27\_DELTA]}。Deck/Page 分列进一步显示 \resulttbd{[TBD:T\_RQ3\_INTERPRETATION]}，据此判断主要瓶颈位于需求传递还是最终页面质量。

## 6.3 消融（RQ4）

Table~\ref{tab:ablation} 报告四组单因素 9B 受控实验：使用同一 1K 训练语料并固定其余训练变量，对照同一共享 Full reference。**委派边界**：单上下文或总是委派。**责任拓扑**：逐页或固定相邻分组，用于隔离依赖对齐分组相对一般任务拆分的贡献。**两级视觉批判**：分别移除组内或整册一级。**轨迹监督**：output-only SFT 或移除 inspect--diagnose--patch 步骤。主要指标为 ThreadBench Intermediate 与 Final，Deck 的跨页语义一致性维度作为拓扑敏感诊断。

\begin{table}[!htbp]
\centering
\footnotesize
\caption{RQ4 受控消融：相对同一 Full reference 单因素比较。Intermediate 与 Final 为聚合输出；Cross-page 为 Final Deck 的跨页语义一致性维度。}
\label{tab:ablation}
\begingroup
\let\texttt\resulttbd
\setlength{\tabcolsep}{3pt}
\begin{tabularx}{\linewidth}{@{}>{\raggedright\arraybackslash}p{0.19\linewidth}>{\raggedright\arraybackslash}Xccc@{}}
\toprule
Factor & Variant & Intermediate & Final & Cross-page \\
\midrule
Full reference (ours) & on-demand + dep.-aligned + both critique + full traj. & \resulttbd{[TBD:A\_FULL\_INT]} & \resulttbd{[TBD:A\_FULL\_FIN]} & \resulttbd{[TBD:A\_FULL\_XSEM]} \\
\midrule
Delegation boundary & Single-context      & \resulttbd{[TBD:A\_DEL\_SC\_INT]}  & \resulttbd{[TBD:A\_DEL\_SC\_FIN]} & \resulttbd{[TBD:A\_DEL\_SC\_XSEM]} \\
                    & Always-delegate     & \resulttbd{[TBD:A\_DEL\_AD\_INT]}  & \resulttbd{[TBD:A\_DEL\_AD\_FIN]} & \resulttbd{[TBD:A\_DEL\_AD\_XSEM]} \\
\midrule
Responsibility topology & Per-slide ownership & \resulttbd{[TBD:A\_TOP\_PS\_INT]}  & \resulttbd{[TBD:A\_TOP\_PS\_FIN]} & \resulttbd{[TBD:A\_TOP\_PS\_XSEM]} \\
                    & Fixed-adjacent      & \resulttbd{[TBD:A\_TOP\_FA\_INT]}  & \resulttbd{[TBD:A\_TOP\_FA\_FIN]} & \resulttbd{[TBD:A\_TOP\_FA\_XSEM]} \\
\midrule
Two-level visual critique & w/o group-level     & \resulttbd{[TBD:A\_VIS\_NG\_INT]}  & \resulttbd{[TBD:A\_VIS\_NG\_FIN]} & \resulttbd{[TBD:A\_VIS\_NG\_XSEM]} \\
                    & w/o whole-deck      & \resulttbd{[TBD:A\_VIS\_NW\_INT]}  & \resulttbd{[TBD:A\_VIS\_NW\_FIN]} & \resulttbd{[TBD:A\_VIS\_NW\_XSEM]} \\
\midrule
Trajectory supervision & Output-only SFT     & \resulttbd{[TBD:A\_SUP\_OO\_INT]}  & \resulttbd{[TBD:A\_SUP\_OO\_FIN]} & \resulttbd{[TBD:A\_SUP\_OO\_XSEM]} \\
                    & w/o inspect--patch  & \resulttbd{[TBD:A\_SUP\_NI\_INT]}  & \resulttbd{[TBD:A\_SUP\_NI\_FIN]} & \resulttbd{[TBD:A\_SUP\_NI\_XSEM]} \\
\bottomrule
\end{tabularx}
\endgroup
\end{table}

将依赖对齐分组替换为逐页或固定相邻责任后，Cross-page 一致性变化为 \resulttbd{[TBD:A\_TOPOLOGY\_EFFECT]}；改变委派边界后，Final 变化为 \resulttbd{[TBD:A\_DELEGATION\_EFFECT]}。移除组内或整册视觉批判得到 \resulttbd{[TBD:A\_CRITIQUE\_EFFECT]}，output-only 与移除 inspect--patch 的监督设置得到 \resulttbd{[TBD:A\_SUPERVISION\_EFFECT]}。组合结果形成结论 \resulttbd{[TBD:A\_RQ4\_INTERPRETATION]}，从而将责任拓扑效应与一般多 Agent 拆分区分开。
