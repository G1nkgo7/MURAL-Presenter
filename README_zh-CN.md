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
  <a href="webui/README.md">SenseNova Present WebUI</a> ·
  <a href="docs/local-deployment_zh-CN.md">本地部署</a> ·
  <a href="docs/thread-bench_zh-CN.md">THREAD-Bench</a>
</p>

> [!NOTE]
> 本仓库目前处于 **research preview** 阶段，已公开项目叙事、系统图、品牌资产、代码目录骨架，
> 以及可直接启动的 **SenseNova Present** 创作 WebUI。当前 **MURAL-Presenter Skill 与配套
> Harness 已随仓库提供**；使用者只需配置兼容的模型端点，以及可选的搜索与生图服务。
> THREAD-Bench case、训练数据、模型权重和正式实验结果仍需等待版本与公开边界冻结。

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

**MURAL-Presenter**（下文简称 MURAL）是一个面向可编辑 HTML 演示文稿完整生命周期的技能驱动多智能体框架。
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

<p align="center"><em>图 1：MURAL 将具有依赖关系的页面交给同一个 Group Agent，并在并行创作后通过整册复审恢复全局闭合。</em></p>

## 完整生命周期

MURAL 通过可复用的 **MURAL Authoring Skill** 串联四个阶段：

1. **Material and research**：整理可选附件，并补齐会改变结论的事实缺口。
2. **Plan and compile**：建立 shared deck blueprint、整册设计系统、页面地图与完整页面组。
3. **Group authoring**：先由统一 Image 阶段收口全册素材，再由多个 Group Agent 联合制作关联页面。
4. **Review, revise, and deliver**：装配与整册复审，按影响范围处理后续修改，并导出多种格式。

<p align="center">
  <img src="assets/figures/authoring-lifecycle.png" width="100%" alt="MURAL 完整创作与修改生命周期">
</p>

<p align="center"><em>图 2：MURAL Authoring Skill 串联材料与研究、整册规划、分组创作、整册复审、按影响范围修改和多格式交付。</em></p>

修改路由会选择能够可靠完成任务的最小范围：

- **Page patch**：精确定位到单页和元素的局部修改。
- **Group replay**：涉及一组关联页面或组内共同视觉语言的修改。
- **Deck replan**：只有当请求改变整册决策或结构时，才重新规划。

## 冻结发布线的推理与合成模式（可选）

正式 Harness 使用同一个入口，并提供两个明确的运行画像：

- `--mode inference` 是默认的 WebUI/交付路径。已消费图片会从后续活跃上下文释放，
  早期历史超过阈值后会压缩，以降低重复 Token 与时延；调试 Trace 仍保留。
- `--mode synthesis` 是训练数据路径。它关闭上述有损上下文维护，为编排器和每个子 Agent
  保存可重放的消息/图片轨迹及图片 SHA-256；任何一条轨迹不完整都会拒收整条样本。

```bash
cd harnesses/mural-presenter

# 推理：快速交付
uv run python infer.py --query "制作一份 8 页演示" \
  --batch demo-infer --mode inference

# 合成：无损训练轨迹
uv run python infer.py --input /absolute/path/to/briefs.jsonl \
  --batch train-synthesis-v1 --workers 4 --mode synthesis
```

完整的无损边界、断点续跑规则、产物目录、完整性门和环境变量见
[Harness 运行说明](harnesses/mural-presenter/README.md)。

## 运行推理（默认：MURAL Presenter v0.2）

当前推荐的推理配套是：

- Skill：[`skills/mural-presenter-v0.2/`](skills/mural-presenter-v0.2/)，包含基于同一
  v0.2 合同的中英文 Single 与 Grouped 说明版；
- Harness：[`harnesses/mural-presenter-v0.2/`](harnesses/mural-presenter-v0.2/)，同时支持
  一页一个 `Slide NN` 和相邻 2–4 页一个 `SlideGroup`。两者只改变页面所有权拓扑。

Orchestrator 第一次读取某个 `SKILL.md` 后锁定该说明版，但说明版语言不决定成品语言；
成品语言由 query 或 JSONL 行中的 `lang` 字段决定。

v0.2 的两个模式使用完全相同的 Skill、模型参数、单页并行拓扑、工具和质量门。
`--mode inference` 是默认模式，可释放已消费图片并压缩旧活动上下文；`--mode synthesis`
关闭这些有损操作，要求根 Agent 和所有子 Agent 提供完整多模态轨迹及 SHA-256 图片清单，
缺失即拒收。DeepSeek 无签名 Thinking 会保存在轨迹中但不回灌 API；有签名 Thinking 可正常回放。

```bash
cd harnesses/mural-presenter-v0.2
cp .env.example .env                  # 填写模型凭据；不要提交 .env
uv venv && uv pip install -r requirements.txt
uv run --no-project playwright install chromium

# 单条任务
uv run --no-project python infer.py \
  --query "制作一份 8 页的 RAVE 论文演示" \
  --batch demo-v02 --workers 1 --mode inference

# JSONL 批量任务：每行至少包含 {"qid":"...","query":"..."}
uv run --no-project python infer.py \
  --queries /absolute/path/to/briefs.jsonl \
  --batch bench-v02 --workers 4

# 合成：无损多模态训练轨迹
uv run --no-project python infer.py \
  --queries /absolute/path/to/briefs.jsonl \
  --batch synthesis-v02 --workers 4 --mode synthesis
```

JSONL 还可提供 `lang`、`slide_count`、`materials`/`attachments`；附件值是绝对路径数组，
或 `{ "path": "..." }` 对象数组。结果写入 `runs/<batch>/<sample_id>/`，批次追加式摘要为
`logs/<batch>.manifest.jsonl`。中断后使用 `--resume`；`--overwrite` 会主动清除该批次已有的
可变 run 目录，应谨慎使用。

模型、生图、搜索、纯文本模型外挂 Vision、Thinking/运行上限、产物和续跑规则详见
[v0.2 Harness 运行说明](harnesses/mural-presenter-v0.2/README.md)。冻结发布线
`skills/mural-presenter/` + `harnesses/mural-presenter/` 仍作为兼容回退保留，但不再是默认推理路径。

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
| [`src/mural_presenter/`](src/mural_presenter/) | 预留 Python 核心：Query 合成、数据处理、调度、推理、渲染和 QC |
| [`configs/`](configs/) | 可版本化、无密钥的配置与 Query 池约定 |
| [`webui/`](webui/) | 当前使用的 SenseNova Present WebUI，直接引用仓库根的正式 Skill/Harness |
| [`skills/mural-presenter/`](skills/mural-presenter/) | 冻结的生命周期 Skill：编排协议、角色卡、参考资料、资源与确定性 Deck 工具 |
| [`harnesses/mural-presenter/`](harnesses/mural-presenter/) | SenseNova Present 使用的配套多智能体执行 Harness |
| [`fonts/`](fonts/) | 正式运行时与测试所需的 OFL/开源字体白名单 |
| [`services/api/`](services/api/) | WebUI 与可复用 MURAL 运行时之间的目标抽取边界 |
| [`scripts/`](scripts/) | 未来的轻量 CLI 入口；可复用逻辑统一放在 `src/` |
| [`tests/`](tests/) | 单元、集成、端到端测试与 Fixture 约定 |
| [`data/`](data/) | 数据目录及发布边界；生成数据不进入 Git |
| [`artifacts/`](artifacts/) | 单次运行的产物约定；生成的 run 与 export 默认忽略 |
| [`benchmarks/thread_bench/`](benchmarks/thread_bench/) | 公开 THREAD-Bench 接口约定 |
| [`assets/logo/`](assets/logo/) | 主角色、紧凑标记、横版组合与可复现导出文件 |
| [`assets/figures/`](assets/figures/) | PNG 与 PDF 论文配图 |
| [`docs/`](docs/) | 方法、评测、品牌与发布说明 |
| [`blog/`](blog/) | 中英文、平台无关的宣传文章 |
| [`site/`](site/) | 可部署的双语**项目主页与 Blog**，不等同于产品 Web UI |
| [`tools/`](tools/) | 视觉资产构建、静态导出与公开边界检查脚本 |

模块边界、输入输出以及单次运行的落盘约定，见[仓库结构说明](docs/repository-layout_zh-CN.md)。

## 当前状态

| 组件 | 状态 |
| --- | --- |
| 公开叙事与系统图 | 已提供 |
| 品牌系统 | 已提供 |
| SenseNova Present WebUI | 支持 UI-only 与 MURAL 生成模式；模型、搜索和生图服务由使用者配置 |
| MURAL-Presenter Skill + Harness | 已提供带来源凭据与测试的冻结快照 |
| 中英文论文工作稿 | 已提供；实验结果待补 |
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
