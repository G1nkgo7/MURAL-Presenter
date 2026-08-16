# Long-horizon Static HTML PPT 中间产物 Agentic 通用 Rubric v3

## 定位

v3 与 v2 的运行时定位相同：它是设计 `intermediate_case_specific`
rubric 的通用参考，不参与正式计分，也不产生额外的 common intermediate score。

当前正式计分仍由以下三层组成：

1. `intermediate_case_specific`；
2. `final_output_common`；
3. `final_output_case_specific`。

通用 v3 的作用是让不同 case 的中间 rubric 使用相同能力边界、证据合同和
评分粒度。真正传给 runner 的仍是 case 目录中的
`rubric_versions/<revision>/intermediate_case_rubric.json`。

## 为什么收缩成三个维度

Plan、内容覆盖、叙事结构、视觉系统和一般跨产物一致性最终都会在渲染后的
Deck 中体现。继续从 Plan 或 Visual System 再评一次，会把“意图写得好”和
“最终做得好”重复计分。

v3 只保留最终 Deck 无法完整替代的三类 agentic 证据：

| ID | 维度 | 通用评分点 |
|---|---|---|
| R | Research来源权威性与证据完整性 | 来源权威与适用；真实访问与可追溯 |
| I | Image真实搜图keyword与生图caption | 实际search keyword；实际generate caption/prompt |
| S | Speech逐页讲稿与口头表达价值 | 逐页覆盖与绑定；口头价值与事实边界 |

每个维度默认两个评分点。case-specific 作者只保留冻结合同和本case真正适用的点。

## Research边界

Research只评价“能不能信”和“能不能复核”：

- 来源层级是否适合当前claim；
- 是否真实访问过来源正文；
- claim是否保留URL或文件、标题/机构、原文位置或可复核摘录；
- 未知、冲突、推断和二手转述是否被诚实标记；
- 是否存在伪造来源、伪造引文或伪造访问。

Research不再逐主题评价内容覆盖，也不再次判断最终Deck中的所有年代、数据和
结论是否正确。这些由final rubric在Deck级别检查。

“权威来源”不是固定网站白名单。case-specific rubric应根据claim类型声明来源层级：

- 制度、标准、统计口径、世界遗产身份等高风险事实，优先原始机构或官方来源；
- 历史解释和学术争议可使用可信学术出版物、博物馆或专业研究机构；
- 搜索摘要、聚合站、营销软文、自媒体和无署名页面不能成为关键事实的唯一依据。

## Image边界

Image维度必须同时覆盖搜图和生图，但两者独立评分、独立判断适用性。

### 搜图keyword

评分对象是实际传给图片搜索工具的`query`、`keyword`或等价参数。

重点检查：

- 主体和身份是否准确；
- 地点、时期、事件、视角和场景等必要限定是否充分；
- query是否与目标页面和用途一致；
- 是否过宽泛、错实体、堆砌无效风格词或写成不可检索的长句。

Plan中的建议搜索词不能替代真实调用。`plan/assets.json`、page plan和asset
catalog只用于确认这个query本来要服务哪个`asset_id`和`slide_id`。

### 生图caption/prompt

这里的caption指实际传给生图工具的自然语言生成指令，也包括工具把该字段命名为
`prompt`的情况；它不是最终页面显示的图片图注。

重点检查：

- 主体和页面用途；
- 构图、视角、景别、比例和安全区；
- 少量关键视觉基因及必要禁止项；
- 身份敏感真实对象的生成边界；
- 是否错误要求图片生成精确文字、年代、数据、地图或图表；
- 是否混入分析过程、互相冲突的风格或不可执行噪声。

本维度不读取最终图片像素，也不评价图片在Deck中的裁切、排版或最终美学。

### 适用性

适用性必须在看模型实际行为之前，根据query、case要求和attempt启动时冻结的
工具/产物合同确定：

- 需要search但模型没调用，search criterion记0；
- 需要generate但没有真实caption/prompt，generation criterion记0；
- 本case确实没有某一种图片需求时，在case-specific rubric中删除对应criterion；
- 不能因为模型漏做而临时改成不适用。

## Speech边界

Speech是独立交付物，因此保留两个能力点：

1. 每个canonical page恰有一个非空section，且无漏页、重复、额外或错绑；
2. 讲稿适合口头表达，能够解释页面和视觉、提供有依据的增量并形成自然转场。

Speech不再次评价最终Deck是否完成全部章节，也不因字数多或时长长加分。
Research和Plan在这里仅作为事实与页面绑定参照。

## Common与case-specific分工

通用v3定义：

- 固定的三个维度和六个候选评分点；
- 每个评分点允许读取的证据；
- 搜图keyword、生图caption和页面图注之间的术语边界；
- 不适用、模型漏做和collector错误的区分；
- `0/0.5/1`质量档及`0/1`硬条件。

case-specific rubric定义：

- 哪些claim需要最高层级来源；
- 可接受与不可接受的来源类型；
- 搜图必须命中的具体主体、身份、地点和场景；
- 生图允许承担的内容、具体构图和禁止生成的对象；
- Speech的受众、语气、关键视觉解释和高风险事实边界；
- 每条criterion自己的可观察anchors和hard boundaries。

最终Deck负责：

- 完整回应query；
- 内容覆盖和叙事顺序；
- 最终事实准确性；
- 页面视觉、图片效果、可读性和执行稳定性。

## Template使用

使用：

`examples/long_horizon_intermediate_case_rubric_v3.template.json`

注意：

- template版本是v3，但当前runner的JSON schema仍为
  `long_horizon_case_specific_rubric_v2`，两者是独立版本轴；
- 删除不适用的search或generate criterion时，也要删除未使用的evidence bundle
  和query requirement；
- intermediate的`query_requirement_map`只保留这三个维度真正需要引用的要求；
- 不要为了让intermediate覆盖全部query而重新加入Plan、最终内容或视觉系统评分；
- 每条criterion必须有自己的完整anchors，不能只引用通用档位。

