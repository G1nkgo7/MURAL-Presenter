# THREAD-Bench 简介

**THREAD-Bench** 的全称是 **Tracking Holistic Requirements and End-to-End Alignment in Decks**，
是一个正在构建的 50-case 长程演示文稿创作诊断评测。

## 评测层级

THREAD-Bench 将经常被压缩为单一总分的证据拆成五层：

1. **过程证据**：Research、Planning、Image 与 Group Authoring 的交接是否完整。
2. **知识检查**：事实、数值和定义级准确性。
3. **整册呈现检查**：叙事、设计语言、术语和任务要求。
4. **单页呈现检查**：页面职责、布局、可读性与视觉表达。
5. **长程探针**：把一个 anchor 决策与远处目标页面或后续修改显式连接起来。

## 依赖记录

每个长程探针需要明确：决策在哪个阶段或页面建立；哪些目标必须消费、保持或回答它；
可观察的关系判定条件；依赖跨度与关系类型；Judge 使用的证据来源。

只有所有必要目标都满足关系判定条件时，该依赖才算闭合。这样可以避免较高的逐页平均分掩盖
一个失败的跨页要求。

## Case 构造

Case 会覆盖不同领域、受众、演讲者视角、目的、语言、页数、附件条件、风格方向与依赖结构。
依赖可以表现为首尾呼应、远距离定义复用、对比口径一致、稳定视觉编码，或一次需要保持未修改页面的
后续 edit。

## 发布门槛

THREAD-Bench 目前还不是 canonical release。公开前需要完成：

- 冻结 50 个 case identifier 与可再分发输入；
- 发布带版本的 schema 与校验脚本；
- 冻结 Judge prompt 与分数聚合规则；
- 完成 Judge 校准与分歧分析；
- 进行 contamination/overlap 检查；
- 提供示例轨迹与缺失证据处理策略。

在这些门槛完成前，文稿应使用进行时或计划时态描述 THREAD-Bench，也不应报告最终比较分数。

