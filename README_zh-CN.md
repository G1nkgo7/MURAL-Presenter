<p align="center">
  <img src="assets/logo/exports/mural-logo-lockup-light.png" width="1000" alt="MURAL Presenter：Multi-Agent Unified Revision-Aware Authoring for Long-Horizon Presentations">
</p>

<p align="center">
  <strong>面向可编辑长程演示文稿的完整生命周期创作框架。</strong>
</p>

<p align="center">
  <a href="README.md">English</a> ·
  <a href="README_zh-CN.md">简体中文</a> ·
  <a href="blog/introducing-mural.zh-CN.md">项目文章</a> ·
  <a href="site/public/mural-paper-zh.pdf">论文工作稿</a> ·
  <a href="docs/thread-bench_zh-CN.md">THREAD-Bench</a>
</p>

> [!NOTE]
> 本仓库目前处于 **research preview** 阶段，先公开项目叙事、系统图与品牌资产。
> 实现代码、MURAL Authoring Skill、THREAD-Bench case、训练数据、模型权重和正式实验结果，
> 会在版本与公开边界冻结后再发布。

## 名字就是方法

| 字母 | 对应含义 | 在方法中的落点 |
| :---: | --- | --- |
| **M** | **Multi-Agent** | 专业角色可以并行，但不会把每一页都变成彼此孤立的任务。 |
| **U** | **Unified** | 一份 shared deck blueprint 持续承载受众、证据、术语、叙事与设计决策。 |
| **R** | **Revision-Aware** | 后续修改按真实影响范围，从单页、页面组或整册规划处继续。 |
| **A** | **Authoring** | 真正可复用的是一条可执行生命周期，而不是一次性出图 prompt。 |
| **L** | **Long-Horizon Presentations** | 决策需要跨阶段、远距页面与后续修改轮次持续有效。 |

> **MURAL 不只是把一条长工作流拆成子任务，而是让 Agent 的责任边界匹配整册依赖结构，
> 并在并行制作后重新恢复整册闭合。**

## 一眼看懂 MURAL

MURAL 是一个面向可编辑 HTML 演示文稿完整生命周期的技能驱动多智能体框架。
它不把演示文稿创作视为一次性出图，而是一个持续的 authoring 过程：理解需求，整理材料与事实，
规划整册，准备素材，联合制作关联页面，检查渲染像素，复审整册，响应后续修改，并导出最终产物。

这里的 **long horizon** 指一项决策需要跨越阶段、页面和修改轮次持续有效的距离。
例如，开场确定的受众假设会影响整册表达；第 3 页给出的定义可能在第 18 页再次使用；
后续修改还需要更新真正受影响的页面，同时尽量保持其他页面不变。

- **共享状态、按责投影。** 全局决策只建立一次，再编译成 group/page briefs；下游不必从持续增长的对话中重建整场演讲。
- **页面组责任制。** 具有叙事或视觉依赖的页面由同一 Group Agent 负责，并共同完成 render–inspect–revise 循环，即使页面并不连续。
- **修改感知续作。** 后续请求沿用同一生命周期，只重新执行能够可靠保持整册决策的最小范围。

## 缺失的中间层：整册 → 页面组 → 单页

单 Agent 在一个连续上下文中完成整册，但其执行历史会随页数和后续修改不断增长。
逐页并行能够缩短单条轨迹，却把跨页关系转化为多个独立上下文之间的状态传递问题。
这首先是责任划分问题，而不只是调度问题。
MURAL 在整册和单页之间增加了一个执行与责任单元：**页面组（slide group）**。

具有共同叙事职责、设计系统、素材系列或显式依赖的页面，由同一个 Group Agent 联合生成和检查；
不同页面组仍可并行。组间汇合后，whole-deck Review 再恢复整册视角。

<p align="center">
  <img src="assets/figures/execution-topologies.png" width="100%" alt="顺序式、完整上下文并行与 MURAL 执行拓扑">
</p>

## 完整生命周期

MURAL 通过可复用的 **MURAL Authoring Skill** 串联四个阶段：

1. **Material and research**：整理可选附件，并补齐会改变结论的事实缺口。
2. **Plan and compile**：建立 shared deck blueprint、整册设计系统、页面地图与完整页面组。
3. **Group authoring**：先由统一 Image 阶段收口全册素材，再由多个 Group Agent 联合制作关联页面。
4. **Review, revise, and deliver**：装配与整册复审，按影响范围处理后续修改，并导出多种格式。

<p align="center">
  <img src="assets/figures/authoring-lifecycle.png" width="100%" alt="MURAL 完整创作与修改生命周期">
</p>

修改路由会选择能够可靠完成任务的最小范围：

- **Page patch**：精确定位到单页和元素的局部修改。
- **Group replay**：涉及一组关联页面或组内共同视觉语言的修改。
- **Deck replan**：只有当请求改变整册决策或结构时，才重新规划。

## 为什么使用 HTML？

MURAL 以 HTML/CSS/SVG 作为创作源，使文字、布局、图形、媒体与交互保持独立可寻址。
浏览器渲染提供像素级检查依据，结构化源码则支持定向修改。PPTX、PDF 和图片是交付适配层，
不同格式的导出保真度需要实际评估，而不是默认等价。

## 评测

我们正在构建 **THREAD-Bench**（*Tracking Holistic Requirements and End-to-End Alignment in
Decks*），同时检查过程证据与最终产物，覆盖知识准确性、整册与单页呈现质量，以及 case-specific
的长程依赖。PresentBench 与 SlidesGen-Bench 用于补充通用生成质量，DECKBench 用于多轮修改评测。

目前尚未公开 MURAL 的正式效果数值。证据边界见[发布状态](docs/release-status.md)，
评测设计见 [THREAD-Bench 简介](docs/thread-bench_zh-CN.md)。

## 仓库结构

| 路径 | 内容 |
| --- | --- |
| [`assets/logo/`](assets/logo/) | 主角色、紧凑标记、横版组合与可复现导出文件 |
| [`assets/figures/`](assets/figures/) | PNG 与 PDF 论文配图 |
| [`docs/`](docs/) | 方法、评测、品牌与发布说明 |
| [`blog/`](blog/) | 中英文、平台无关的宣传文章 |
| [`site/`](site/) | 可部署的双语项目主页与 Blog |
| [`tools/`](tools/) | 视觉资产构建、静态导出与公开边界检查脚本 |

## 当前状态

| 组件 | 状态 |
| --- | --- |
| 公开叙事与系统图 | 已提供 |
| 品牌系统 | 已提供 |
| 中英文论文工作稿 | 已提供；实验结果待补 |
| MURAL Authoring Skill | 等待版本冻结 |
| THREAD-Bench | 等待 schema 与 Judge 校准 |
| 训练数据与模型权重 | 等待复现与发布审查 |
| 正式实验结果 | 尚未公开 |

## 参与贡献

Research preview 阶段最有价值的贡献包括：公开描述勘误、可复现失败 case、Benchmark case 提案、
可访问性反馈和导出保真度报告。提交 Issue 或 Pull Request 前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 引用与许可证

当前[英文](site/public/mural-paper.pdf)与[中文](site/public/mural-paper-zh.pdf)论文均为工作稿，
并非归档版本。作者信息、正式论文链接、引用元数据和许可证会随对应产物公开。
在此之前，仓库暂未提供许可证，这并不表示可以重新分发或使用尚未公开的实现与数据。
