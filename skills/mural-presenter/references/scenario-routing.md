# 场景路由

本文件只负责路由，不负责给出整套模板。规划时先读本页，选择一个主场景，再读取对应的一个分类文件。跨场景任务可以借用一个辅助视角，但不要把两套分类指南混成一套。

## 渐进阅读顺序

1. 根据听众要完成的动作选择一个主场景；
2. 只读取该场景对应的分类文件；
3. 从分类文件得到叙事骨架、证据对象、页面家族和节奏；
4. 再从 `style-routing.md` 路由到一个风格家族簇；有数据、流程、层级或关系时按需读取 `charts-and-diagrams.md`、`shape-grammar.md`；字体是主声部时再读 `fonts.md`；
5. 把选择后的结论写入 `plan/design-brief.md`，后续 Agent 消费设计合同，不重新浏览整套 references。

用户明确指定模板、设计系统或风格参考时，场景分类仍负责内容与证据组织；视觉外观服从用户指定的来源。

## 主场景目录

| 主场景 | 听众的主要任务 | 详细指南 |
| --- | --- | --- |
| 决策与经营分析 | 判断现状、取舍、风险与下一步 | [`slide-categories/analysis-decision.md`](slide-categories/analysis-decision.md) |
| 商业计划、招商与提案 | 相信机会成立并看清合作价值 | [`slide-categories/business-proposal.md`](slide-categories/business-proposal.md) |
| 管理汇报与组织沟通 | 对齐事实、责任、进度与行动 | [`slide-categories/management-report.md`](slide-categories/management-report.md) |
| 学术研究与严肃评审 | 判断问题、方法、证据、限制和贡献 | [`slide-categories/academic-research.md`](slide-categories/academic-research.md) |
| 教学与培训 | 建立直觉、完成理解并迁移应用 | [`slide-categories/education-training.md`](slide-categories/education-training.md) |
| 产品发布与技术工程 | 理解对象、机制、能力、边界和可信证据 | [`slide-categories/tech-engineering.md`](slide-categories/tech-engineering.md) |
| 品牌、文化与创意叙事 | 感受主题、记住主张并形成情绪认同 | [`slide-categories/brand-creative.md`](slide-categories/brand-creative.md) |

公益、公共议题和政策传播通常以“决策与经营分析”组织证据，或以“品牌、文化与创意叙事”建立共情；主场景取决于听众最终是要决策、理解还是行动。

## 选择问题

- 听众离场时要做出判断、学会方法、理解系统，还是记住故事？
- 什么证据最有说服力：数据、原始 Figure、真实场景、产品、案例、过程还是人物？
- 哪种页面最容易成为叙事峰值，哪种页面负责解释和承接？
- 现场是快速决策、投影讲授、正式评审、招商洽谈，还是沉浸式叙事？

## 写入设计合同

在 `plan/design-brief.md` 中记录：

```text
primary_scenario: <一个主场景>
category_reference: references/slide-categories/<selected>.md
audience_action: <听众看完能判断、理解或完成什么>
narrative_spine: <本册具体推进方式>
evidence_objects: <本册最重要的证据对象>
page_families: <会反复出现但不机械同构的页族>
density_rhythm: <聚焦、解释、证据和呼吸如何交替>
auxiliary_lens: none | <只借用一个动作及其用途>
```

这些字段只服务生产，不直接成为屏显文案。
