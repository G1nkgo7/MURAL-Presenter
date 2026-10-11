<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo/exports/street-v2/mural-banner-dark.png">
    <img src="assets/logo/exports/street-v2/mural-banner-light.png" width="1000" alt="MURAL Presenter — Multi-Agent Units of Responsibility for Authoring Long-Horizon Presentations">
  </picture>
</p>

<h1 align="center"><img src="assets/logo/exports/street-v2/mural-avatar-circle.png" width="40" height="40" align="absmiddle" alt=""> MuralPresenter</h1>
<p align="center"><strong>Multi-Agent Units of Responsibility for Authoring Long-Horizon Presentations</strong></p>
<p align="center">可编辑 HTML 演示文稿 · 持续负责的页面 Owner · 视觉检查与修订</p>

<p align="center">
  <a href="https://github.com/G1nkgo7/MURAL-Presenter#readme-en">English</a> · <strong>简体中文</strong>
</p>
<p align="center">
  <a href="#生成与修改示例">🖼️ 效果示例</a> ·
  <a href="#安装与启动">🚀 快速开始</a> ·
  <a href="webui/README.md"><img src="webui/studio/static/logo.svg" width="18" height="18" align="absmiddle" alt=""> WebUI</a>
</p>

**MuralPresenter 根据需求和来源材料制作可编辑 HTML 演示文稿，支持证据整理、视觉检查和后续修改。**
系统持续维护整册状态，让页面 Owner 在制作与修改中保留明确的责任范围。

本仓库配套论文，提供创作 **Skill**、执行 **Harness** 和 **WebUI**；
各项制品的当前可用范围见[发布状态](#发布状态)。

## 项目概览

- **有依据的内容。** 将研究结论、来源与适用条件纳入共享状态，为页面创作提供依据。
- **自适应责任分配。** 独立页面交给 Single Owner，需要共同决策的页面交给 Group Owner。
- **持续视觉修订。** Owner 渲染、检查和修改自己负责的页面，并继续承担后续修改。

## 生成与修改示例

### 动物罗盘 · 论文贯穿案例

> **需求摘要：** 为生命科学高年级本科生制作 40 页教学演示，讲解地磁、太阳与星辰罗盘，
> 包含科学示意图、真实照片和对比表。

**实际输出 · MuralPresenter-27B（Global Selective，2 epoch／checkpoint-64）。**
下方是同一次已完成的 40 页中文演示中的第 19、37 页，点击图片可查看原始渲染。

<table>
  <tr><th width="50%">第 19 页 · 太阳罗盘机制</th><th width="50%">第 37 页 · 相互校准与冗余</th></tr>
  <tr>
    <td><a href="assets/figures/animal-compass/slide-19.png"><img src="assets/figures/animal-compass/slide-19.png" width="100%" alt="实际生成的第 19 页：时间补偿太阳罗盘的机制图与讲解"></a></td>
    <td><a href="assets/figures/animal-compass/slide-37.png"><img src="assets/figures/animal-compass/slide-37.png" width="100%" alt="实际生成的第 37 页：三种罗盘的校准与冗余关系"></a></td>
  </tr>
</table>

[查看三张代表页及来源，含三种罗盘对比表](assets/figures/animal-compass/README.md) ·
直接采用归档批次的 1600 × 900 原始渲染，未进行图片修饰。

<details>
<summary>这个任务如何体现共同决策与持续负责的 Owner</summary>

- **改一处，影响多页。** 将航向参照从正北改为太阳，导航图的角度标注与对比表中的解释就需要一起调整。
- **让共同决策有负责人。** 校准页与对比页的约定尚未确定时，由一个 Group Owner 协同制作；依据已确定的页面可交给 Single Owner 独立推进。
- **修改仍找原 Owner。** 后续反馈交回相应 Owner，修改后再次进行视觉检查。

<p align="center">
  <a href="assets/figures/ownership-workflow.png"><img src="assets/figures/ownership-workflow.png" width="100%" alt="动物罗盘方法示意：共享证据与计划、单页和多页责任分配、视觉检查与持续修改"></a>
</p>

以上文字与流程图用于解释论文方法，不代表归档批次的具体 Owner 分组，也不代表该批次执行过“正北改为太阳”的修改。

</details>

### 从论文到演示 · 实际修改对照

**同一份演示文稿，由原有 Owner 继续修改。** 论文中的定性案例先将介绍本论文的演示文稿从
10 页扩展到 12 页，再从黑板风格改为浅蓝白色，并调整解释性图示。下方展示第 3 页的前后对照，完整画廊保留六组。

<table>
  <tr><th width="50%">修改前 · 黑板风格</th><th width="50%">修改后 · 浅蓝白色</th></tr>
  <tr>
    <td><a href="assets/figures/revision-case/before-03.png"><img src="assets/figures/revision-case/before-03.png" width="100%" alt="修改前：第 3 页解释 Single Owner 与 Group Owner"></a></td>
    <td><a href="assets/figures/revision-case/after-03.png"><img src="assets/figures/revision-case/after-03.png" width="100%" alt="修改后：第 3 页采用蓝白风格并更新责任单元图示"></a></td>
  </tr>
</table>

[查看全部六组前后对照](assets/figures/revision-case/README.md) · [配图来源](assets/figures/README.md)。
这些图片直接导出自论文附录，不是在线 WebUI 演示；该定性案例不计入 benchmark 均值。

## 安装与启动

需要 Python 3.12、uv，以及自己的模型服务和凭据。

```bash
git clone https://github.com/G1nkgo7/MURAL-Presenter.git
cd MURAL-Presenter
uv sync --project webui/studio --frozen
uv sync --project webui/inference --frozen
test -f webui/.env || cp webui/.env.example webui/.env
bash webui/start.sh
```

打开启动器打印的地址，默认 `http://127.0.0.1:8001`。
填写 API 地址、模型名和 API key，添加一个多模态模型。

<details>
<summary>配置、附件解析与可选服务</summary>

仅首次部署复制示例配置；已有 `.env` 应保留。Windows 使用 `webui/start.ps1`。
首次启动可能下载 Chromium。启动器生成被忽略的 `configs/runtime.local.json`，不会覆盖已有配置。
在其中设置并发及按顺序排列的 MinerU GPU/CPU 服务地址，或用 `MURAL_RUNTIME_CONFIG` 指定已有文件。
PDF、Office、图片附件解析需要 MinerU；纯文本任务不需要附件解析服务。搜索和生图服务分别配置。

</details>

**先试一个短任务。** 创建 case，输入：

> 制作一份六页教学演示，介绍动物如何使用地磁、太阳和星辰罗盘，包含解释性图示与一张对比表。

**再继续修改。** 生成完成后，在同一 case 中发送：

> 减少文字，统一示意图与对比表中的方向约定，保持六页。

完成后预览并下载结果。部署、数据路径与排错见 [WebUI 使用说明](webui/README.md)。

<a name="命令行推理"></a>

<details>
<summary>命令行推理</summary>

```bash
webui/inference/.venv/bin/python scripts/run_inference.py \
  --query "制作一份六页的研究介绍演示" \
  --workspace "$PWD/artifacts/example"
```

使用 `--help` 查看参数，或用 `--doctor --json artifacts/doctor.json` 生成诊断报告。
新任务使用新工作区；保留历史工作区及其固定的运行版本。

</details>

## 方法详解

<details>
<summary>责任单元与跨页依赖</summary>

一个 Owner 制作整册时，其制作历史会随页面与修改不断增长。逐页分配 Owner 可以分散制作任务，
跨页决策则需要在独立上下文之间协调。MuralPresenter 在同一份演示文稿中同时支持单页和多页单元。
分组依据是内容或设计决策是否仍需共同调整；仅仅主题相同或风格相似，并不足以决定分组。

<p align="center">
  <img src="assets/figures/cross-page-dependencies.png" width="880" alt="跨页事实、视觉和叙事依赖，以及整册历史增长与逐页上下文分散的取舍">
</p>

<p align="center"><em>跨页依赖要求制作责任同时具备共享整册状态与明确的局部范围。</em></p>

</details>

<details>
<summary>Agent 角色、工具与任务交接</summary>

| 角色 | 职责 |
| --- | --- |
| **Orchestrator** | 维护共享知识与蓝图，分配责任单元，复审整册并路由修改；也可处理轻量研究、常规素材与尚未分配的页面。 |
| **Research** | 检索和综合证据，保留来源、事实成立条件与未解决问题。 |
| **Image** | 获取或生成素材，进行视觉检查，交接路径、用途、限制和目标页面。 |
| **Slide** | 制作 HTML、检查渲染并修复所负责页面；Single Owner 和 Group Owner 使用同一套创作能力。 |

工具执行检索、文件操作、素材处理和渲染，Agent 决定内容与设计。任务通过文件路径和使用说明交接。
已经确认可用的素材可以先交给页面 Owner，无需等待全批次素材完成。

<p align="center">
  <img src="assets/figures/roles-and-tools.png" width="680" alt="Orchestrator、Research、Image、Slide 的职责、工具与共享整册状态">
</p>

</details>

<details>
<summary>创作、责任分配与修改流程</summary>

当前流程图展示六项可重叠、可重复进入的活动：

1. **Knowledge Grounding（知识奠基）**：解决事实问题，记录采纳的证据、成立条件与开放问题。
2. **Deck Planning（整册规划）**：确定叙事、页面 brief、讲稿、设计规则与视觉需求。
3. **Asset Preparation（素材准备）**：获取或生成视觉素材，检查并记录使用方式。
4. **Delegate Page Units（分配页面单元）**：将独立页面或需要共同调整的页面组交给对应 Owner。
5. **Parallel Authoring（并行创作）**：各独立单元在输入就绪后开始制作，其他页面的准备工作可以继续。
6. **Iterative Refinement（迭代修订）**：渲染、检查和修复页面，复审整册，并将发现的问题交给对应 Owner。

内容修改由相关 Owner 继续处理；结构修改可以调整责任单元和归属；共享风格修改先更新公共状态，
再修改受影响页面。修改后的页面重新渲染和检查。交付阶段按整册顺序汇总已检查的页面、素材和讲稿，
验证文件完整性，并导出所需格式。

</details>

<details>
<summary>可编辑 HTML 与导出格式</summary>

MURAL 以 HTML/CSS/SVG 作为创作源，使文字、布局、图形、媒体与交互保持独立可寻址。
浏览器渲染提供像素级检查依据，结构化源码则支持定向修改。PPTX、PDF 和图片是交付适配层，
不同格式的导出保真度需要实际评估，而不是默认等价。

</details>

## 模型与轨迹训练

论文基于 **686 条原生源记录中的 9,000 个质量筛选后执行片段**，训练
**MuralPresenter-9B** 与 **MuralPresenter-27B**。这些数字表示片段和源记录数量，
并不表示生成了 9,000 份完整演示文稿。每个片段保留此前历史作为输入上下文。

公开 benchmark 主实验使用 **Global Selective** checkpoint：

| 模型 | 基座 | 评测 checkpoint |
| --- | --- | --- |
| MuralPresenter-9B | Qwen-3.5-9B | 4 epochs，128 个优化步 |
| MuralPresenter-27B | Qwen-3.8-27B | 2 epochs，64 个优化步 |

本次源码发布不附带模型权重。

<details>
<summary>训练设置与监督策略</summary>

| 监督策略 | 参与训练损失计算的 Assistant 动作 |
| --- | --- |
| Full | 历史动作和当前片段动作。 |
| Global Selective | 所有角色都只监督当前片段动作。 |
| Role-wise Selective | Slide 只监督当前片段动作，其他角色采用 Full。 |

训练使用 MS-SWIFT、Megatron 和 BF16，冻结视觉编码器与 merger，最大序列长度为 131,072 tokens，
batch size 为 64。监督策略对比使用训练 4 轮的 27B 模型，与主实验的 2 轮 27B checkpoint 不同。
以上是论文实验设置，不是 WebUI 的生成默认值。本次源码发布不附带模型权重。

</details>

## 实验结果速览

下表摘自当前论文的公共 benchmark 结果，并非本次更新重新运行所得。四行均使用 MuralPresenter 创作框架。

| 基座／训练后模型 | PresentBench Overall ↑ | SlidesGen 内容准确率 (%) ↑ | SlidesGen 美学总分 ↑ |
| --- | ---: | ---: | ---: |
| Qwen-3.5-9B | 41.1 | 74.71 | 14.46 |
| **MuralPresenter-9B** | **47.3** | **90.21** | **18.38** |
| Qwen-3.8-27B | 69.4 | 93.33 | 18.58 |
| **MuralPresenter-27B** | **72.6** | **94.92** | **19.12** |

PresentBench 覆盖 238 个任务，SlidesGen-Bench 覆盖 189 个任务，各项指标的量纲不同。
理解结果时需结合 checkpoint 选择与生成条件，详见[评测协议](#评测)。

<details>
<summary><strong>分析配图 · 责任分配工作量与选择性监督</strong></summary>

**责任分配工作量。** 35 个匹配任务中，每个任务制作阶段记录到的输入与输出 token 均值，不含共享准备阶段。
统计值是下界，不能据此推断实际耗时加速。

<p align="center">
  <a href="assets/figures/responsibility-workload.png"><img src="assets/figures/responsibility-workload.png" width="800" alt="Whole-deck、All-Single 与 Unit-adaptive 三种策略按角色划分的制作 token"></a>
</p>

**选择性监督。** Global 相对 Full 的质量变化及配对 95% bootstrap 区间，以及各角色输入 token。
这些探索性结果来自工具条件存在差异的运行，不能单独归因为训练策略。

<p align="center">
  <a href="assets/figures/supervision-ablation.png"><img src="assets/figures/supervision-ablation.png" width="640" alt="Global 与 Full 监督的质量差异及各角色输入 token 对比"></a>
</p>

</details>

## 评测

<details>
<summary>评测覆盖范围、指标与结论适用范围</summary>

| 评测 | 当前论文覆盖范围 | 评测内容 |
| --- | --- | --- |
| PresentBench | 238 个任务 | 按任务专属 rubric 评估交付演示文稿的质量。 |
| SlidesGen-Bench | 189 个任务 | 内容准确率与计算美学。 |
| DECKBench | 294 个任务，每个任务修改 5 轮 | 使用 benchmark 定义的相似度变化衡量多轮编辑结果。 |
| 内部长演示任务 | 6 个领域的 35 个中英文请求，每个要求 20–40 页 | 分别评估长演示文稿的最终质量与过程证据。 |

内部评测分别报告质量与过程。**Overall = 70% Deck + 30% Page**，Knowledge 属于 Deck。
**Process** 独立评估研究支持与采纳（**R**）、可执行规划（**P**）、素材准备与实际使用交接（**I**），
以及计划落地、渲染检查和讲稿与页面对应（**S**）。Process 不计入 Overall。当前过程分析排除了
依赖创作轨迹的评分条目，包含正式和诊断记录；缺少兼容动作记录，本身不能证明 Agent 没有执行对应动作。

**实验协议与结论适用范围**

实验结论应结合论文记录的协议理解：

- PresentBench 的 10 个任务及内部长演示任务中的 10 个任务参与了 checkpoint 选择，并保留在各自报告均值中；完整选择历史尚未重建。
- DECKBench 每轮把上一轮产物导入新工作区，测试产物延续性，没有测试 Owner 历史的持续保留。
- 自适应与逐页分配的 Overall 均值接近，二者记录到的制作工作量都高于整册单 Owner；实验未建立耗时加速结论。
- 监督策略实验中的搜索可用性、生图服务和配额存在差异，限制了将结果变化单独归因于训练策略。

</details>

## 品牌素材

独立标识、头像与画家吉祥物沿用顶部横幅的壁画风格。点击图片可打开原始 PNG。

<table>
  <tr><th width="33%">主标识 · 透明背景</th><th width="33%">头像 · 彩色圆形徽章</th><th width="33%">画家吉祥物 · 透明背景</th></tr>
  <tr><td align="center"><a href="assets/logo/exports/street-v2/mural-mark.png"><img src="assets/logo/exports/street-v2/mural-mark.png" width="160" alt="主标识 · 透明背景"></a></td><td align="center"><a href="assets/logo/exports/street-v2/mural-avatar-circle.png"><img src="assets/logo/exports/street-v2/mural-avatar-circle.png" width="160" alt="头像 · 彩色圆形徽章"></a></td><td align="center"><a href="assets/logo/exports/street-v2/mural-mascot.png"><img src="assets/logo/exports/street-v2/mural-mascot.png" width="160" alt="画家吉祥物 · 透明背景"></a></td></tr>
</table>

<details>
<summary>字标与论文分享图</summary>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo/exports/street-v2/mural-wordmark-dark.png">
    <img src="assets/logo/exports/street-v2/mural-wordmark-light.png" width="440" alt="MURAL Presenter">
  </picture>
</p>

字标随阅读器深浅色主题切换。

<p align="center">
  <a href="assets/logo/exports/street-v2/mural-social-card.png"><img src="assets/logo/exports/street-v2/mural-social-card.png" width="640" alt="MuralPresenter — Multi-Agent Units of Responsibility for Authoring Long-Horizon Presentations"></a>
</p>

[浅色字标](assets/logo/exports/street-v2/mural-wordmark-light.png) · [深色字标](assets/logo/exports/street-v2/mural-wordmark-dark.png) · [论文分享图](assets/logo/exports/street-v2/mural-social-card.png)

</details>

完整素材、使用说明与版本记录见[品牌指南](assets/logo/exports/street-v2/README.md)。

## 发布状态

| 制品 | 当前状态 |
| --- | --- |
| 创作 Skill 与 Harness | 已提供：0919 冻结源码，按原始清单校验。 |
| WebUI 与启动器 | 已提供：便携配置、命令行入口与 `/studio` 部署支持。 |
| 正式 logo 与方法配图 | 已提供：[街头壁画品牌素材](assets/logo/exports/street-v2/README.md)及[论文配图来源](assets/figures/README.md)。 |
| 9B / 27B 模型权重 | 本仓库未附带，尚无公开 checkpoint 下载链接。 |
| 训练数据、内部评测案例与评测流水线 | 未纳入本次运行系统源码发布。 |
| 论文链接与引用信息 | 等待作者提供最终公开元数据。 |

运行时源码 pin 与训练模型 checkpoint 分别维护。0919 源码 pin 不能证明后续每组论文实验
都使用完全相同的运行时与配置。

<details>
<summary>复现论文分数还需要什么？</summary>

复现分数还需要精确输入及校验信息、权重和对话模板、评测适配器、字体补丁、Judge 配置，
以及结果选择与覆盖记录。原评测字体库大于本仓库附带字体库。
程序运行成功或源码校验通过，不等于论文分数已复现。

</details>

## 公开目录

```text
MURAL-Presenter/
├── README.md / README_zh-CN.md
├── skills/mural-presenter-v1/       # 冻结的创作 Skill 与工具
├── harnesses/mural-presenter-v1/    # 冻结的推理与交付运行时
├── webui/                          # 前端、服务端、依赖锁和启动器
├── assets/                         # 正式 logo、图标与方法配图
├── configs/                        # 运行配置模板、MinerU、版本来源清单
├── fonts/                          # 渲染所需字体
├── scripts/                        # 便携命令行入口和配置适配
├── tools/                          # 源码校验和集成版本冻结
├── tests/                          # 公开集成回归测试
└── .github/                        # 持续集成与贡献模板
```

Skill/Harness 的目录名及其内部 assets、references、docs、tests 都属于冻结版本，保持完整。
正式品牌素材位于 `assets/logo/exports/`，方法配图位于 `assets/figures/`；
[配图来源记录](assets/figures/README.md)说明当前论文配图与历史版本。设计草稿保留本地。
根目录 `benchmark/`、`docs/`、`examples/`、`paper/`、训练材料和独立
`site/` 不纳入本次发布。密钥、环境、数据库和生成 case 也不上传。必要说明已合并到 README。
本机保留这些目录不代表它们会进入 Git 提交。

## 版本维护与验证

254 个核心文件保留 0919 源码快照
`mural-presentbench-qwen35-9b-20260919T163556Z`，部署路径在启动时解析；
便携集成改动通过 `configs/releases/mural-paper-v1.json` 独立记录。

```bash
python tools/check_paper_release.py
```

源码哈希、历史测试结果与已知失败统一记录在[贡献说明](CONTRIBUTING.md#发布验证记录)；
检查范围见[打包 CI](.github/workflows/public-surface.yml)。

## 许可与引用

项目整体许可证等待权利人确认。第三方原有条款继续适用，参见
[第三方声明](webui/THIRD_PARTY_NOTICES.md)及[字体记录](fonts/FONTS.md)。
最终作者、论文 URL/DOI、引用信息和固定论文 tag 待确认，不填写虚构元数据。
