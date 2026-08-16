# Current PPT Pipeline 中间产物 Agentic 通用 Rubric v2

> 历史说明：本通用中间产物 Rubric 已退出当前 long-horizon 正式计分。
> 当前 runner 只计算 `intermediate_case_specific`，并与
> `final_output_common`、`final_output_case_specific` 一起输出三组独立分数。
> 新 case 请使用
> `examples/long_horizon_intermediate_case_rubric_v2.template.json`；
> 本文和对应 YAML/XLSX 仅保留作旧实验与设计参考。

以下内容为退出正式计分前的历史设计说明。

## 边界

本Rubric只评价Current Pipeline的中间产物与跨产物传递，输出`agentic_intermediate_score`。

最终deck继续使用：

- `multi_page_image_rubric_v2.yaml`
- `multi_page_image_rubric_v2.xlsx`

本Rubric不评价最终内容效果、最终叙事、最终视觉美学、HTML实现质量，也不评价Agent数量、并发、token、耗时、成本或Harness工程架构。

## 为什么改成6维12点

Current Pipeline的主要中间产物集中在：

1. Plan；
2. Research；
3. Image请求；
4. Speech；
5. base.css设计词元与Visual System；
6. 上述产物之间的语义传递。

因此通用层从抽象的9维20点压缩为6维12点：

| ID | 维度 | 权重 | 评分点 |
|---|---|---:|---:|
| P1 | Plan任务建模、叙事与逐页规格 | 22 | 2 |
| P2 | Research、材料处理与知识综合 | 18 | 2 |
| P3 | Image语义需求、搜图keyword与生图prompt | 20 | 3 |
| P4 | Speech讲稿与页面叙事对齐 | 15 | 2 |
| P5 | base.css设计词元内容与Image一致性 | 15 | 2 |
| P6 | 跨中间产物语义一致性与消费闭环 | 10 | 1 |

共1个代码0/1点、3个Judge 0/1点、8个Judge分档点。唯一代码点只检查`speech.md`是否与canonical pages逐页一一对应；不评价CSS骨架、文件所有权、Harness状态或一般工程质量。

## Current产物合同

Rubric当前使用以下产物：

| 逻辑产物 | Current路径或来源 |
|---|---|
| deck plan | `plan/deck.md` |
| page plans | `plan/slide_NN.md` |
| asset plan | `plan/assets.json` |
| raw research | `research/research_NN.md`、`research/materials_NN.md` |
| knowledge synthesis | `research/knowledge-brief.md` |
| image requests | page plan的semantic visual need、实际search query/generation prompt、asset catalog |
| speaker notes | `speech.md` |
| visual system spec | `plan/visual-system.json` |
| visual system projection | `base.css` |

Rubric不要求Current合同之外的历史文件。未来Current再发生路径合并或删除时，由attempt合同adapter映射到逻辑产物；合同明确不再要求的能力点记N/A，不得因历史文件缺失扣分。

## Attempt级合同冻结

`versions/current`会变化，因此Judge不能在评测时读取后来变化的Current来推断历史attempt要求。每次生成必须冻结：

```yaml
skill_version_or_dev_identity: v6-dev
skill_or_system_prompt_hash: <hash>
generation_attempt_id: <id>
artifact_contract_snapshot:
  deck_plan: {required: true, paths: [plan/deck.md]}
  page_plans: {required: true, paths: [plan/slide_NN.md]}
  speaker_notes: {required: true, paths: [speech.md]}
  visual_system_spec: {required: true, paths: [plan/visual-system.json]}
```

缺失状态严格区分：

- `model_missing`：attempt合同要求但模型未生成，记能力失败；
- `not_applicable`：attempt合同不要求，剔除分母；
- `collector_error`：评测侧解析失败，不算模型失败；
- `contract_unknown`：无法绑定生成时Skill身份，评测证据不足。

## 评分方式

每个维度：

```text
dimension_score = Σ(point_score × point_weight) / Σ(applicable point_weight)
```

总分：

```text
100 × Σ(dimension_score × dimension_weight) / Σ(applicable dimension_weight)
```

评测方式与分数档位是两条独立轴：

| 评测方式 | `0/1`绝对判定 | 多档质量判定 |
|---|---|---|
| 代码 | P4_1 Speech逐页section覆盖 | 不使用 |
| LLM Judge | P1_1 query硬约束、P2_1 Research可信底线、P3_1 Image工具决策 | 其余8点 |

代码只判断真实产物中可无歧义解析的结构事实，不尝试用字符串规则代替语义判断。LLM Judge不等于必须打连续质量分；对于不可折中的硬条件，Judge只能输出`0/1`：

- P1_1：任一核心任务误读或显式硬约束违反即为0；全部满足才为1；
- P2_1：任一关键伪造、无依据强断言、口径反转或必需附件失察即为0；
- P3_1：任一核心Image slot漏请求、主体/消费者错误、mode根本错误或真实身份被生图伪造即为0；
- P4_1：代码只能检查逐页section是否存在、非空、无重复和无额外页，不能判断讲稿语义。

其余Judge点使用各自声明的固定档位。所有评分点均声明：

- `required_intermediate_artifacts`；
- `required_reference_context`；
- `pass_if`或`evaluate`；
- Judge点的`judge_subcriteria`。

其中二者边界固定：

- `required_intermediate_artifacts`是每个run目录中的真实中间产物文件，例如`plan/deck.md`、`speech.md`或`base.css`；
- `required_reference_context`只允许放不属于run中间产物的外部参照，例如原始query、原始附件、case-specific要求、生成时冻结的合同或工具能力；
- 不再把`deck_plan_summary`、`relevant_page_plan_context`、`knowledge_synthesis_summary`等由真实产物派生的摘要列为参照输入，避免与实际路径重复，也避免评测侧摘要替代原始证据。

## Image评价

P3区分两种能力：

- 搜图：实际keyword是否准确表达主体、身份和必要场景限定；
- 生图：实际prompt/caption是否表达主体、构图、安全区和比例，并服从Visual System。

Judge直接读取run中的真实路径：`plan/slide_*.md`、`plan/assets.json`、`assets/catalog_*.md`和`_trace/subagents/image_*/tool_log.json`。评测程序只截取当前asset对应的Semantic visual need、keyword或prompt，不生成替代性的“中间产物索引”。无search或generate request时，对应语义评分点单独N/A。

P3不读取最终图片像素；实际图片与最终页面效果由final-output rubric评价。

## base.css与Visual System评价

Current的Agentic视觉决策证据是`plan/visual-system.json`和`base.css`中的实际design tokens/art-direction片段。因此：

- Judge评价base.css实际配色、字体角色、字号层级、间距节奏、圆角/线条/面板等形状语言、art direction和image treatment是否适合该PPT；
- Judge只截取`base.css`的`:root`词元与相关视觉片段，不评价CSS工程结构；
- 另一个Judge点评价Visual System与搜图/生图keyword/prompt是否一致。

## Judge分包

6个维度分别调用Judge：

- P1：每次最多3份page plans；
- P2：每次1份Research/材料产物；
- P3：每次最多4条image requests；
- P4：每次最多4页speech sections；
- P5：Visual System加最多8条image requests；
- P6：按canonical page分片，读取该页在Research、Plan、Speech、Image和Visual System中的对应真实片段。

任何Judge都不读取完整messages、完整tool log、完整workspace、最终HTML或最终渲染。

## Common与Case-specific边界

Common评价：

- Plan是否准确、完整、可执行；
- Research是否可靠并被Plan消费；
- keyword/prompt是否准确；
- Speech是否与页面及事实对齐；
- Visual System是否自洽并约束Image请求；
- 跨产物传递是否发生语义漂移。

Case-specific声明：

- 必须回答的Research问题和claims；
- 必须覆盖的章节、页面和信息；
- 搜图必须表达的主体、身份与场景限定；
- 生图必须表达或禁止的具体语义；
- Speech必须强调的信息；
- 品牌色、禁用色、语气和其他风格约束。

建议独立报告：

```text
multi_page_final_output_score
agentic_intermediate_score
case_specific_score
```
