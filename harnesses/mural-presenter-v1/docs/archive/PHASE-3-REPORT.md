# MURAL Presenter v1 Phase 3 Report

> 2026-09-02 审计说明：本报告只证明 Workspace/Artifact 原语，Renderer、Skill 脚本、字体和素材
> 生产链尚未接入；以 `V1.0-CAPABILITY-AUDIT.md` 为准。

完成日期：2026-09-02
状态：Workspace 与 Artifact State 完成，保持 Shadow

## 1. 交付范围

- `state/workspace.py`
- `state/artifacts.py`

## 2. 核心结果

1. Workspace

   - 固定 `plan/`、`slides/`、`renders/`、`assets/`、`_state/`、`_trace/` 和 `delivery/` canonical paths；
   - 页面 UID 持久稳定，但语义 group 仍来自委派工具；
   - 页面状态由当前文件和交易哈希派生，scaffold 文件存在绝不等于 authored；
   - HTML 改动后立即降级并把旧 PNG 标为 stale。

2. Canvas 消费

   - Workspace 初始化不选择画布；生产动作缺少 `plan/canvas.json` 时明确失败；
   - `canvas.css`、Workspace/WebUI manifest 和 Renderer 都读取冻结的同一 `CanvasSpec`；
   - 页面开始 authored/rendered 后不能通过 Harness 默认值改变尺寸。

3. Artifact

   - Owner declaration、互斥 lease、transaction commit 和 source hash 校验；
   - 图片 available/unavailable 来自实际 fetch 回执与解码结果，不解析 handoff prose；
   - Catalog hash 随真实图片或来源变化更新；
   - Renderer receipt 保存 HTML、PNG、Canvas、成功加载资源和本地视觉资源哈希；
   - 破图、资源缺失、未验证外链、Renderer failed resource、共享 CSS/图片变化都会阻止继续闭合。

## 3. 验证

Phase 3 专项：`7 passed`，覆盖无默认 Canvas、scaffold/authored 区分、完整 lifecycle、stale PNG、破图、
Renderer 失败回执、图片修复/Catalog 变化、lease 和 Canvas manifest 传播。

旧 WebUI 尚未接线；新 manifest 已提供其未来唯一机械投影。
