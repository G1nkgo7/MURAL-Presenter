# MURAL Presenter v1 Phase 4 Report

> 2026-09-02 审计说明：本报告只证明 QC 数据结构和局部路由，独立 Critic 与 Orchestrator 整册闭合
> 尚未接入；以 `V1.0-CAPABILITY-AUDIT.md` 为准。

完成日期：2026-09-02
状态：QC 与意图驱动修订完成，保持 Shadow

## 1. 交付范围

- `qc/visual.py`
- `qc/content.py`
- `qc/routing.py`

## 2. 核心结果

1. Visual QC

   - 候选保存 page、severity、owner hint、PNG hash、bbox 和可选测量阈值；
   - 所有 major 候选必须逐项得到 confirmed、rejected 或 unresolved；
   - `max_reviews` 耗尽后剩余候选明确写为 unresolved，不能因候选上限变成 pass；
   - `vision_analyze` 收到冻结 Canvas，并把 inspection ID 绑定到当前 HTML/PNG 哈希对。

2. Content QC

   - Reviewer 输入保留 raw query 原文、来源文本/哈希和最终 HTML 可见文本；
   - script、style、template 与 hidden 区域不作为可见内容；
   - Harness 不使用 Query 关键词或正则替 Reviewer 判断语义覆盖。

3. Issue Ledger 与 Routing

   - 同一问题按证据/像素指纹去重；未变化像素不会反复重开；
   - `pass` 在仍有 unresolved major 时拒绝；accepted degradation 必须有显式 rationale；
   - owner hint 与实际 pages/group 决定 Slide/Image/Orchestrator 路由；
   - 对图片/素材问题，Catalog 没有变化时不重复激活 Slide Owner；
   - repair/ready/help 由实际工具和 task terminal 事件识别，不要求精确自由文本句式。

## 3. 验证

Phase 4 专项：`7 passed`，覆盖候选预算、像素去重、raw query/可见文本、原 Owner 路由、Catalog
no-change、accepted degradation、工具意图和 Canvas-aware vision inspection。
