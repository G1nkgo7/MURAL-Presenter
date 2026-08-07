<p align="center">
  <img src="assets/logo/exports/mural-logo-lockup-light.png" width="780" alt="MURAL 标志">
</p>

<p align="center">
  <strong>面向长程演示文稿的多智能体统一、修改感知创作框架</strong>
</p>

<p align="center">
  <a href="README.md">English</a> · <a href="README_zh-CN.md">简体中文</a>
</p>

> [!NOTE]
> 本仓库目前处于 **research preview** 阶段，先公开项目叙事、系统图与品牌资产。
> 实现代码、MURAL Authoring Skill、THREAD-Bench case、训练数据、模型权重和正式实验结果，
> 会在版本与公开边界冻结后再发布。

## MURAL 是什么？

MURAL 是一个面向可编辑 HTML 演示文稿完整生命周期的技能驱动多智能体框架。
它不把演示文稿创作视为一次性出图，而是一个持续的 authoring 过程：理解需求，整理材料与事实，
规划整册，准备素材，联合制作关联页面，检查渲染像素，复审整册，响应后续修改，并导出最终产物。

这里的 **long horizon** 指一项决策需要跨越阶段、页面和修改轮次持续有效的距离。
例如，开场确定的受众假设会影响整册表达；第 3 页给出的定义可能在第 18 页再次使用；
后续修改还需要更新真正受影响的页面，同时尽量保持其他页面不变。

## 为什么需要页面组？

单 Agent 在一个连续上下文中完成整册，但其执行历史会随页数和后续修改不断增长。
逐页并行能够缩短单条轨迹，却把跨页关系转化为多个独立上下文之间的状态传递问题。
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
| [`tools/`](tools/) | 公开视觉资产的确定性构建脚本 |

## 当前状态

| 组件 | 状态 |
| --- | --- |
| 公开叙事与系统图 | 已提供 |
| 品牌系统 | 已提供 |
| 论文 | 撰写中 |
| MURAL Authoring Skill | 等待版本冻结 |
| THREAD-Bench | 等待 schema 与 Judge 校准 |
| 训练数据与模型权重 | 等待复现与发布审查 |
| 正式实验结果 | 尚未公开 |

## 参与贡献

Research preview 阶段最有价值的贡献包括：公开描述勘误、可复现失败 case、Benchmark case 提案、
可访问性反馈和导出保真度报告。提交 Issue 或 Pull Request 前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 引用与许可证

作者信息、论文链接、引用元数据和许可证会随对应产物正式发布。在此之前，仓库暂未提供许可证，
这并不表示可以重新分发或使用尚未公开的实现与数据。

