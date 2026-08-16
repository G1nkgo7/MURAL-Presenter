# LH-PresentBenchmark｜当前评测方案

> 当前机制：Case-specific rubric v004 + 维度级重大缺陷扣分 + Final组级最弱维度衰减（α=0.3）  
> 本文档整理当前已经确定并落地的benchmark设计；原始`LH-PresentBenchmark.pdf`保留不动。

## 名称与定位

推荐名称：

**THREAD-Bench: Tracing Handoffs, Requirements, and Execution Across Deck Generation**

副标题：

**A Benchmark for Process–Artifact Consistency in Long-Horizon Presentation Agents**

这里不使用“End-to-End”，因为benchmark并不声称覆盖演示文稿生产系统的所有工程环节。它重点评价：

- Research → Plan → Image → Slide之间的关键handoff；
- Query要求在长程执行中的持续保持；
- 中间过程与最终Deck之间的一致性；
- 最终Deck的知识、全局组织与逐页呈现质量。

因此，它更准确地属于**process-aware、long-horizon、artifact-grounded**的演示文稿Agent benchmark。

---

# 第一部分：评测方案设计

## 一、为什么区分中间过程与最终产物

### 1. 中间过程与最终产物

中间过程评测关注Agent是否真正完成了长程任务所需的拆解、研究、规划、素材准备、页面生产和纠错。重点不是文件是否存在，而是阶段之间是否形成有效交接：

- Research中的证据是否进入Plan；
- Query要求是否被映射到明确页面和任务；
- 图片需求是否经过获取、检查、选择并最终进入页面；
- 页面是否经过真实渲染、检查、修正和复验。

最终产物评测关注交付出来的PPT是否真正可用。过程完整不等于最终质量高：计划可能未执行，内容可能遗漏，页面也可能渲染失败。因此，中间过程和最终产物分别评分，互不替代。

### 2. 知识质量与呈现质量

PPT既是知识载体，也是视觉与叙事产物：

- **知识质量**：核心事实、概念和结论是否被正确覆盖；
- **Deck-level呈现质量**：整套PPT是否适合受众，内容选择、叙事、节奏、跨页一致性和视觉系统是否成立；
- **Page-level呈现质量**：每一页的信息层级、图文关系、构图排版、可读性和技术质量是否成立。

三者分开评价，可以避免视觉精美掩盖事实错误，也避免知识丰富掩盖叙事、排版和素材问题。

## 二、评测模块

### 1. Intermediate：中间过程

Intermediate包含四个维度、七类case-specific criterion：

| 维度 | Criterion | 主要关注内容 |
|---|---|---|
| R. Material & Research | R01、R02 | 研究覆盖、证据进入规划、来源核验、冲突与不确定性处理 |
| P. Deck Planning | P01、P02 | Query硬要求覆盖、整册叙事和逐页规划可执行性 |
| I. Image Asset Preparation | I01、I02 | 图片任务定义、搜索/生成策略、筛选调整、页面映射和实际使用 |
| S. Slide Production & Local Verification | S01 | 页面制作、真实渲染、问题发现、修改、重渲染与复验 |

每个criterion可以进一步展开为语义单元：

- R01按核心研究主题展开；
- R02按预先标记的高风险事实展开；
- P01按Query Requirement Manifest逐项展开；
- P02按整册叙事和当前plan中的逐页任务展开；
- I01、I02按每个实际图片任务展开；
- S01按当前run实际生成的每一页展开。

展开只增加独立检查次数，不改变criterion本身的评分定义。

### 2. Final：最终产物

Final分为Deck和Page两组。

#### 2.1 Deck-level

Deck关注整套PPT，所有criterion必须映射到以下六个维度之一：

1. 受众与沟通目标适配；
2. 内容选择与信息密度；
3. 叙事结构与章节过渡；
4. 节奏篇幅与冗余控制；
5. 跨页语义一致性；
6. 全局视觉一致性。

知识checklist先形成一个独立的知识维度，再与上述六个Deck维度一起聚合。因此，当前Deck由**六个呈现维度 + 一个知识维度**组成，而不是把所有Deck criterion平铺后直接平均。

Deck必须检查当前run生成的全部页面，不允许抽样。若case要求固定页数，则页数要求由独立criterion检查；全局评测范围仍以实际生成页面为准。

#### 2.2 Page-level

Page关注单页表现，所有case-specific Page criterion必须映射到以下五个维度之一：

1. 信息层级与密度；
2. 图文关系与视觉编码；
3. 构图、对齐、排版、留白与平衡；
4. 字体、颜色、对比与可读性；
5. 技术完整性与素材质量。

Page采用“criterion × 实际页面”的独立请求：当前run生成多少个canonical pages，就为每个Page criterion执行多少次适用性判断和评分。每次只输入当前页证据，不能先形成整套印象再复制分数。

## 三、Rubric架构

采用“通用维度 + Case-specific rubric”的方式：

1. 通用维度定义benchmark要评价的能力边界；
2. 每个case根据Query、材料和任务特征编写具体criterion；
3. 每个criterion必须明确映射到一个通用维度；
4. 实际Judge只使用Case-specific rubric；
5. 聚合时先在维度内平均，再在维度之间聚合，不能把所有criterion跨维度平摊。

Case rubric只保留与本case评分有关的内容，不引用无关的外部rubric字段。

## 四、评测输入

评测器不接收无差别的完整证据树，而是按任务最小必要范围选择输入。这里减少的是**无关文件数量**，不是压缩高信息内容。

### 1. Intermediate输入

- R：Query、研究正文、必要研究trace和Plan；
- P：Query、Plan以及当前规划单元所需的完整研究/资产证据；
- I：当前图片任务对应的需求、搜索/生成trace、catalog、资产和实际使用页面；
- S：当前页面对应的Plan、Slide trace、HTML、渲染记录与诊断图。

### 2. Knowledge输入

知识层只输入：

- Query / instruction；
- 全部HTML提取内容。

知识层不输入渲染图。

### 3. Deck输入

| Deck维度 | 输入 |
|---|---|
| 受众与沟通目标适配 | 全部HTML提取内容 |
| 内容选择与信息密度 | 全部HTML提取内容 + 全部渲染图 |
| 叙事结构与章节过渡 | 全部HTML提取内容 |
| 节奏篇幅与冗余控制 | 全部HTML提取内容 |
| 跨页语义一致性 | 全部HTML提取内容 |
| 全局视觉一致性 | 全部渲染图 |

### 4. Page输入

每个逐页请求只输入：

- 当前页HTML提取内容；
- 当前页渲染图；
- 当前页面类型。

## 五、可选Preaudit

Final可以开启Preaudit：

1. Preaudit先检查整套最终产物；
2. 只记录检测到的内容或视觉缺陷；
3. 正式Judge只接收与当前criterion相关的缺陷，不接收完整预审分析；
4. Preaudit不直接决定分数，正式Judge仍须根据criterion证据和anchor完成判断。

Preaudit是可选项，不改变Rubric本身的评分定义。

## 六、Reason-derived scoring与N/A

### 1. Reason-derived scoring

Judge不先给一个分数再补理由，而是：

1. 阅读当前单元的完整证据；
2. 对每个评分anchor给出是否匹配及理由；
3. 形成证据和缺陷分析；
4. 代码根据唯一命中的anchor派生`0 / 0.5 / 1`或`0 / 1`。

评分由分析推导，而不是让分析迁就预先输出的分数。

### 2. N/A语义边界

`N/A`只表示Query语义上不要求当前对象或过程，例如没有科学示意图的页面不适用“科学示意图结构”criterion。

以下情况不能判为`N/A`：

- Query明确要求，但模型没有生成；
- 过程应该发生，但证据缺失；
- 资产任务规划后没有执行；
- 页面或中间产物遗漏。

适用单元缺失应按anchor评分。`N/A`单元不进入平均，也不触发缺陷扣分。

## 七、基础分与聚合顺序

### 1. 单元到criterion

对于展开型criterion (c)：

\[
C_c = \frac{1}{|U_c|}\sum_{u\in U_c} s_u
\]

其中，(U_c)只包含适用单元；`N/A`不进入分母。

### 2. Criterion到小维度

每个criterion只能属于一个维度。当前维度内criterion等权：

\[
D_d^{raw}=\frac{1}{|C_d|}\sum_{c\in C_d}C_c
\]

### 3. 整体顺序

> 原子单元/页面评分 → criterion平均 → 小维度原始分 → v004重大缺陷扣分 → 组内维度聚合 → Final组级衰减 → 输出两组总分

## 八、v004重大缺陷扣分

Case rubric可以给非知识criterion标记：

- `ordinary`：只参与原始平均，不额外扣分；
- `major`：低分原子单元会对其所属小维度触发额外扣分。

重大缺陷事件规则：

- 原子单元得`1`：不扣分；
- 原子单元得`0.5`：所属小维度扣`0.1`；
- 原子单元得`0`：所属小维度扣`0.2`；
- 同一小维度中的事件持续累加；
- 每个小维度最多额外扣`0.5`；
- `N/A`不触发扣分；
- 知识checklist不设置`defect_level`。

设维度(d)中的重大缺陷事件集合为(E_d)，则：

\[
D_d=\max\left(0,D_d^{raw}-\min\left(0.5,\sum_{e\in E_d}\delta_e\right)\right)
\]

其中(delta_e\in\{0,0.1,0.2\})。

Page的缺陷事件来自单个页面，Intermediate展开criterion的缺陷事件来自单个语义单元，非展开Deck criterion则以该criterion结果作为事件。

当前正式方案只使用v004普通/重大两档机制，不使用v005的致命标签和连续乘法。

## 九、Final分数

### 1. Knowledge

知识层由原子binary checklist等权平均：

\[
K=mean(K_1,\ldots,K_m)
\]

### 2. Deck基础分

设六个经过v004扣分后的Deck呈现维度为(D_1,\ldots,D_6)：

\[
Deck_{base}=\frac{K+D_1+D_2+D_3+D_4+D_5+D_6}{7}
\]

### 3. Page基础分

每个Page criterion先对当前run全部适用页面求平均，再在其所属维度内平均。设五个经过v004扣分后的Page维度为(P_1,\ldots,P_5)：

\[
Page_{base}=\frac{P_1+P_2+P_3+P_4+P_5}{5}
\]

### 4. Final组级最弱维度衰减

为避免一个明显短板被其他高分维度完全平均掉，Deck和Page各自只执行一次：

\[
G_{effective}=G_{base}\times[1-\alpha(1-G_{min})]
\]

当前：

\[
\alpha=0.3
\]

其中(G_{min})为当前组内最低小维度分。该衰减只用于Final的Deck和Page，Intermediate不执行。

### 5. Final总分

\[
final\_output\_case\_specific
=0.5\times Deck_{effective}+0.5\times Page_{effective}
\]

## 十、Intermediate分数

设四个经过v004扣分后的维度分为(R,P,I,S)：

\[
intermediate\_case\_specific=\frac{R+P+I+S}{4}
\]

正常情况下四个维度各占25%。若某一整组在Query语义上完全不适用，例如任务完全不需要图片，Image组排除，剩余适用维度重新归一化。

Intermediate不执行最弱维度乘法衰减。

## 十一、最终输出

benchmark最终只输出两组互相独立的`0–1`分数：

- `final_output_case_specific`
- `intermediate_case_specific`

同时保留criterion、维度、Deck/Page和缺陷调整明细，用于解释分数和定位问题；两组总分不再合并成一个总分。

## 十二、案例演示：动物环境罗盘

以“动物如何利用环境因素确定方向”的科学教学PPT为例：

- Intermediate检查研究是否覆盖三类罗盘及关键证据，Query要求是否进入规划，图片是否形成完整获取与使用链路，每页是否经过真实渲染闭环；
- Knowledge用原子checklist检查地磁、太阳和星辰罗盘的关键事实与机制；
- Deck检查受众、内容选择、教学主线、章节节奏、跨页语义与全局视觉系统；
- Page对当前run实际生成的每页分别检查信息层级、图文关系、构图、可读性和素材质量；
- case要求的页数、指定科学示意图、真实图片和六列三行对比表由独立criterion检查。

## 十三、方案价值

- **评测长程能力**：把阶段交接、状态保持、工具执行和纠错转化为可核验分数；
- **定位失败位置**：区分Research、Plan、Image、Slide、Knowledge、Deck和Page问题；
- **避免平均掩盖**：重大缺陷扣分和Final最弱维度衰减共同保留严重短板；
- **保证横向比较**：通用维度固定，case-specific criterion适应不同任务；
- **支持训练与诊断**：保留单元、criterion和维度明细，可用于奖励设计和定向优化。

## 十四、核心总结

这套benchmark不是让Judge对整套PPT凭印象打一个总分，而是：

1. 将Query要求、事实、图片任务和页面拆成可核验单元；
2. 依据证据和anchor独立评分；
3. 按criterion和维度逐级聚合；
4. 用v004机制惩罚重复出现的重大缺陷；
5. 用α=0.3的组级衰减防止Final明显短板被平均掉；
6. 分别输出中间过程分和最终产物分。

---

# 第二部分：Rubric维度设计

## 一、Process Rubric

最能体现long-horizon agentic ability的不是产物文件是否存在，而是以下handoff是否成立：

```text
Query / Material
  → Research evidence
  → Executable Deck Plan
  → Image tasks and assets
  → Slide construction
  → Real render inspection and repair
```

### 维度R：材料理解与研究

对应阶段：Material / Query → Research → Plan

目标是形成完整、可追溯、区分来源类型、保留冲突与不确定性、能被下游规划使用的知识底稿。

重点检查：

- Query中需要研究的主题是否覆盖；
- 关键事实、数据、年份、案例和引用是否有来源；
- 高风险事实是否经过权威或多源核验；
- 冲突和不确定性是否被记录并传递到Plan；
- Research证据是否真正进入Deck或逐页规划。

### 维度P：整册规划与逐页编排

对应阶段：Research / Query → Deck Planning → Plan Validation

目标是将Query和研究底稿转化为可执行的Deck生产计划。

重点检查：

- 语言、页数、受众和其他硬性要求；
- 整体叙事与章节关系；
- 每页功能、核心信息和视觉表达；
- 证据、数据和素材的逐页分配；
- 高风险页和特殊页；
- Image与Slide Agent的任务边界。

### 维度I：图片资产准备

对应阶段：Image Requirement → Search / Generation → Inspection → Selection → Usage

仅在Query或Plan需要外部图片、附件图片或生成图片时适用。

重点检查：

- 获取方式是否与主体和用途匹配；
- 搜索关键词或生成Prompt是否可执行；
- 具名真实对象是否优先采用真实素材；
- 结果是否被检查，不匹配时是否调整；
- 资产是否映射并实际进入目标页面；
- SVG、图片结构、清晰度和重复问题是否被识别；
- 来源、用途、页面与裁切方案是否可追溯。

### 维度S：逐页制作与真实渲染自检

对应阶段：Slide Construction → Render → Inspect → Fix → Re-render

目标是确认页面生产以真实像素结果为依据，而不是只检查HTML源码。

重点检查：

- 是否读取正式逐页计划和相关资产；
- 是否完成真实渲染；
- 是否检查内容纳入、遮挡、溢出、裁切、比例和构图；
- 是否对发现的问题进行修改；
- 修改后是否重新渲染并复验；
- 没有问题的页面是否也留下具体检查记录。

## 二、Final Rubric

### 1. Knowledge

知识维度将case知识要求拆成原子binary checklist，关注最终HTML中是否出现并正确表达必要知识。知识清单需要有明确来源，例如：

- 用户附件或材料；
- 权威网站、论文或数据源；
- 高质量参考PPT；
- 已有benchmark整理的Query与Rubric。

### 2. Deck-level六个维度

| 维度 | 评价重点 |
|---|---|
| 受众与沟通目标适配 | 内容深度、语言、术语和表达是否适合目标听众 |
| 内容选择与信息密度 | 内容是否服务目标、覆盖完整且分配合理 |
| 叙事结构与章节过渡 | 整体故事线、章节关系和过渡是否成立 |
| 节奏篇幅与冗余控制 | 篇幅分配、推进节奏和重复是否合理 |
| 跨页语义一致性 | 术语、分类、结论和跨页引用是否一致 |
| 全局视觉一致性 | 字体、颜色、网格、组件、配图和示意图规则是否统一 |

### 3. Page-level五个维度

| 维度 | 评价重点 |
|---|---|
| 信息层级与密度 | 页面中心、阅读顺序和信息量 |
| 图文关系与视觉编码 | 图文对应、解释作用及科学示意图编码 |
| 构图、对齐、排版、留白与平衡 | 视觉重心、分组、对齐、间距和留白 |
| 字体、颜色、对比与可读性 | 演示尺度下的文字、颜色和对比 |
| 技术完整性与素材质量 | 渲染完整性、照片位图质量、SVG和矢量图质量 |

---

# 第三部分：知识层复用与Case构建

## 一、知识来源复用

开放研究型任务可以复用已有DeepResearch benchmark中经过整理的Query和知识rubric，但需要重新映射到演示文稿场景：

- Knowledge检查最终PPT对必要知识的覆盖；
- Research检查Agent是否取得并核验这些知识；
- Plan检查知识是否进入页面规划；
- Deck/Page检查知识是否被有效转化和呈现。

附件材料型任务则以用户材料为主要知识锚点，重点评价忠实提取、完整覆盖、来源边界和最终呈现。

## 二、Case rubric构建步骤

1. 固定Query和材料边界；
2. 将硬性要求整理为Query Requirement Manifest；
3. 将必要知识拆成原子Knowledge checklist；
4. 根据任务内容编写Deck和Page criterion；
5. 将每个criterion映射到唯一通用维度；
6. 为真正重要的非知识criterion标记`ordinary`或`major`；
7. 明确逐页、逐资产、逐事实或逐要求的评测单元；
8. 明确每个criterion的证据输入、anchor和N/A条件；
9. 校验Markdown说明、JSON rubric和运行时聚合规则一致。

---

# 第四部分：仍需在更多Case上验证的问题

## 一、知识precision

当前原子Knowledge checklist主要衡量必要知识是否被覆盖。对于PPT额外生成、但不在checklist中的事实，其正确性仍需要通过：

- Research来源核验；
- Deck跨页语义一致性；
- case-specific事实criterion；
- 必要时增加专门的幻觉或无依据主张检查。

是否需要统一的knowledge precision模块，应在更多case上验证后决定。

## 二、混合型材料任务

当用户上传附件但没有要求“只使用附件”时，需要在case中明确：

- 附件是唯一知识边界、主要来源还是最低覆盖要求；
- 是否允许外部补充；
- 外部补充是否需要单独标记来源；
- 材料事实与外部事实冲突时如何处理。

这些边界应写入Query和case rubric，不能交给Judge临时猜测。

## 三、权重与参数的跨Case稳定性

当前采用：

- Deck内部七个维度等权；
- Page内部五个维度等权；
- Final中Deck/Page各50%；
- Intermediate四个维度等权；
- v004维度扣分上限0.5；
- Final组级衰减α=0.3。

这些是当前正式机制。后续若调整，应基于多个领域和任务类型的统一校准，而不是根据单个case或目标排名反向调参。

---

# 附录：相对原始方案的主要调整

1. 不再将Knowledge与Deck并列成第三个Final大组，而是作为Deck中的一个独立维度；
2. Deck由六个通用维度加一个知识维度组成，criterion必须先在维度内平均；
3. Page由五个通用维度组成，criterion必须映射到维度，不能平铺平均；
4. Page改为“criterion × 当前run实际页数”的独立请求；
5. Intermediate展开到逐要求、逐事实、逐图片任务和逐页单元；
6. Judge采用reason-derived scoring，由anchor分析派生分数；
7. 明确Query语义层面的N/A边界；
8. Preaudit改为可选，仅向正式Judge传递相关检测缺陷；
9. 当前正式缺陷策略固定为v004普通/重大两档，并作用于所属小维度；
10. Final的Deck和Page分别增加一次α=0.3的最弱维度衰减，Intermediate不衰减；
11. 最终只输出Final和Intermediate两组总分，不再合并。
