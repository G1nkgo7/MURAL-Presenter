# MURAL Presenter v1 Harness 重构 Handoff

更新时间：2026-09-02

> 状态注记：本文保留最初迁移交接与实施纪律；第 2 节和第 11 节是 Alpha 开工前语境。正式
> Skill-backed AgentRuntime、无附件 inference 和短 synthesis 已完成，当前事实与剩余 release 门禁以
> `README.md`、`ARCHITECTURE.md` 和 `PHASE-6-SHADOW-REPORT.md` 为准。

## 1. 任务目标

在不影响当前生产版本的前提下，将
`harnesses/mural-presenter-v1.0` 中已经验证的能力，分阶段迁移到新的
`harnesses/mural-presenter-v1`。

目标不是删掉业务能力，也不是重写 Skill，而是把当前巨型 Harness 收敛为：

- 少量稳定状态；
- 少量机械硬门；
- 事件驱动的多 Agent 运行时；
- 以显式工具动作和真实产物推进，而不是解析自然语言格式；
- inference、synthesis 和 WebUI 共用同一生成内核；
- QC、交付导出与训练导出彼此分离。

必须保留 MURAL 的论文方法：语义分组、Persistent Owner、原生视觉检查、独立 Critic、修订路由、断点恢复与可训练轨迹。

## 2. 当前状态与开始条件

> 审计更正（2026-09-02）：新 Harness 已完成一组机械内核原语，但尚未迁移正式 Skill loader、
> Orchestrator/Owner Agent loop、基础创作工具、附件处理、Renderer 宿主、批处理监督和 WebUI 接线。
> 先前的 `runtime/host.py` 没有使用 Skill，已删除；两条 27B 结果降级为历史 transport/kernel smoke，
> 不能作为正式 canary。当前 v1.0 能力等价尚未成立，详见 `V1.0-CAPABILITY-AUDIT.md`。

### 2.1 当前路径

- 当前生产 Harness：`harnesses/mural-presenter-v1.0`
- 当前 Skill：`skills/mural-presenter-v1`
- 新 Harness 目录：`harnesses/mural-presenter-v1`
- 详细计划：`harnesses/mural-presenter-v1/REFACTOR-PLAN.md`
- Release 指针：`configs/releases/mural-paper-v1.json`

### 2.2 当前源码仍在变化

编写本 Handoff 时，`mural-presenter-v1.0` 与 Skill 中仍存在大量未提交修改，且有另一 Agent 会话正在工作。

因此接手后第一件事不是写代码，而是：

1. 询问用户当前修改会话是否已经结束；
2. 检查相关目录最近修改时间和 Git 状态；
3. 在用户确认源码稳定前，不得开始迁移；
4. 不得用任何历史目录、冻结快照或归档版本覆盖当前工作树；
5. 不得执行 `git reset`、`git clean` 或整目录同步。

新的 `mural-presenter-v1` 在完成回归和 canary 前不能替换 WebUI、infer 或 synthesis 当前入口。

## 3. 最重要的架构边界

Harness 与 Skill 不是完全解耦，也不能与 Skill 文案完全耦合。正确关系是：

> 只在一组很小、稳定的角色、工具动作和生命周期事件上有意耦合。

### 3.1 Skill 负责

- 角色为什么存在；
- Orchestrator 如何规划、分组和委派；
- 内容、证据、叙事与视觉方法；
- 页面和整册应如何判断质量；
- Agent 何时认为应该修改、求助或结束。

### 3.2 Harness 负责

- 模型会话和 Provider 调用；
- 工具执行与权限；
- Owner 身份、任务状态和并发；
- 事件等待与唤醒；
- 工作区、文件和哈希；
- HTML/PNG/素材同源；
- checkpoint、compact、resume；
- QC 证据路由；
- 交付和训练导出。

### 3.3 两者之间的稳定接口

优先用显式工具动作表达决定，例如：

```text
canvas_select
delegate_submit
delegate_next_event
delegate_collect
request_orchestrator
respond_owner_request
artifact_commit
render
vision_analyze
verification_submit
```

已接受的工具动作才是运行时状态真源。Plan Markdown 继续服务于 Agent 语义协作，但不作为 Harness 数据库。

### 3.4 明确禁止的耦合

Harness 不得：

- 用 Query 正则或默认尺寸替 Orchestrator 决定画布；
- 依赖 `**ownership_topology**：grouped` 等 Markdown 写法；
- 要求 `PAGE DECISION P01: ready` 等固定句式；
- 根据句号、分隔符或问题描述列数判断是否允许继续；
- 解析自然语言中的页面分组来重建 Ownership；
- 根据 Skill 文案中的标题、换行或措辞决定运行状态；
- 判断封面风格、版式、色彩或主观审美；
- 为历史 Deck ID 增加特例。

精确的路径、页码、Owner ID、Tool ID、哈希和 Segment predecessor 也不能依赖模糊意图识别；它们应直接来自工具参数和文件状态。

## 4. 目标目录

第一版控制在约 15 个功能文件，不提前创建空抽象：

```text
harnesses/mural-presenter-v1/
├── README.md
├── ARCHITECTURE.md
├── REFACTOR-PLAN.md
├── HANDOFF.md
├── runtime.env
├── infer.py
├── synthesize.py
├── runtime/
│   ├── session.py
│   ├── tools.py
│   ├── recovery.py
│   └── config.py
├── owner/
│   ├── registry.py
│   └── context.py
├── orchestration/
│   └── manager.py
├── state/
│   ├── workspace.py
│   └── artifacts.py
├── qc/
│   ├── visual.py
│   ├── content.py
│   └── routing.py
└── export/
    ├── delivery.py
    └── training.py
```

`__init__.py` 不计入功能文件。

初版把 Wave 状态和 Coordinator 事件循环放在同一个
`orchestration/manager.py`。只有代码职责真实变得不可维护时，才拆成 `waves.py` 和 `coordinator.py`。

不要新增独立的 `metadata.py`。机械元数据由 RunSpec、工具动作、Owner Registry 和 Artifact Store 直接生成。

## 5. 关键模块职责

### 5.1 `runtime/session.py`

- 单次模型会话；
- messages 与 usage；
- context segment；
- compact 边界；
- 不包含 MURAL 页面规划或 QC 规则。

### 5.1a `runtime/config.py` 与画布

- `RunSpec` 保存 raw query 与可选 UI hint，不保存预确认画布；
- Orchestrator 在首轮规划内调用 `canvas_select`，不增加额外模型调用；
- Harness 只校验格式、方向、像素/物理尺寸与安全区，并一次性冻结 `plan/canvas.json`；
- 冻结后的 `CanvasSpec` 是 CSS、Renderer、WebUI 与 PPTX 的唯一机械合同；
- 页面生产前必须已有合同，生产开始后不得静默改变。

### 5.2 `runtime/tools.py`

- 工具 Schema 和执行；
- tool use/result 原子事务；
- 工具权限和 Owner 写入范围；
- Agent 可见的委派、等待、求助、渲染和检查接口。

### 5.3 `runtime/recovery.py`

- Provider 错误分类；
- 通道 failover；
- 从最后闭合工具事务恢复；
- checkpoint/resume；
- Thinking signature 不跨压缩后的新 segment。

### 5.4 `owner/registry.py`

只保存稳定的逻辑 Ownership：

```text
owner_id
role
pages/group
attempt
status
dependencies
```

Ownership 由 `delegate_submit` 的已接受参数产生，不从 Plan Markdown 反向解析。

### 5.5 `owner/context.py`

- 为 Owner 编译短 Owner Packet；
- 保存 compact/checkpoint；
- 管理 resume context；
- 提供必要的跨组只读视觉语法或产物快照；
- 避免 Owner 反复读取大体积 Skill、CSS 和无关页面。

### 5.6 `orchestration/manager.py`

它是非 AI 的事件管理器，负责：

- 接收 Orchestrator 的委派动作；
- 维护依赖和并发；
- 启动可运行 Owner；
- 在没有事件时挂起 Orchestrator，不调用模型轮询；
- 只在 Owner 的 `owner_request` 事件、失败、依赖解除或 wave terminal 时唤醒；
- 不决定页面如何设计，也不替 Orchestrator重分组。

Agent 看到的是工具；持久任务状态和事件循环必须在后端实现，不能只写成一次性工具函数。

### 5.7 `state/workspace.py` / `state/artifacts.py`

- canonical paths；
- 页面生命周期：`scaffolded → authored → rendered → inspected → ready`；
- HTML、PNG、素材和 Catalog 哈希；
- stale PNG、破图和缺失产物；
- Issue Ledger 与 accepted degradation；
- WebUI 状态不能再把 scaffold 当作 authored。

### 5.8 `qc/*`

- `visual.py`：产生问题、位置和像素证据；
- `content.py`：对照 raw query、材料/研究与最终可见文本；
- `routing.py`：决定返回 Slide、Image、Orchestrator 或接受降级。

Owner 的原生视觉上下文保持纯净。QC 可以提供独立证据，但不替 Agent 决定设计修改方案。

### 5.9 `export/*`

- `delivery.py`：最终 HTML、PNG、PDF/PPTX、result 和 manifest；
- `training.py`：bounded segment、lineage、checkpoint、image blob 和 admission。

Inference 与 synthesis 共用同一生成内核。Synthesis 只额外保存训练所需的可重建轨迹并执行 admission，不得维护第二套创作流程。

## 6. 硬门与软判断

### 6.1 保留的机械硬门

- Owner 只能写允许范围；
- tool transaction 必须闭合；
- 修改后必须重新 render；
- 最终 PNG 必须对应当前 HTML；
- 素材路径必须存在且成功加载；
- checkpoint、resume 和 segment lineage 必须合法；
- 导出必须完整；
- 未解决的确定性 major 不能进入正训练样本。

### 6.2 不再作为硬门

- Markdown 格式；
- ready 的精确说法；
- issue 描述的列数和分隔符；
- 主观美观建议；
- 固定修订轮数；
- Plan advisory；
- 页脚、配色、布局等设计决定。

对可恢复的机械错误，Harness 应返回短、具体、可执行的提示并继续同一任务，而不是结束整册。停滞保护只处理真实的无产物变化循环。

## 7. 分阶段执行与验收

用户明确要求每个阶段完成后先验收，未经确认不得进入下一阶段。

### Phase 0：冻结基线与迁移地图

只做读取、盘点和测试固化，不迁移运行代码。

产物：

- 当前源码 Git 状态与 tree hash；
- 入口、模块、状态和工具清单；
- 当前功能到目标模块的迁移表；
- 必须保留的回归能力清单；
- 真实失败 Trace 的 replay fixture 清单；
- 将 `REFACTOR-PLAN.md` 的目录更新为本 Handoff 中的精简结构。

验收后才能进入 Phase 1。

### Phase 1：Runtime Core

迁移并验证：

- `runtime/config.py`
- `runtime/session.py`
- `runtime/tools.py`
- `runtime/recovery.py`

要求：不改变当前生产入口；用现有 Trace 做离线 replay；Provider、tool transaction、compact 和恢复行为与基线一致或更好。
Anthropic Messages 与 OpenAI Chat Completions 使用同一 `MODEL_*` 配置、Channel 和执行流程；不得使用
教师/学生角色前缀。Runtime 每个函数/方法保留简短中文说明。

### Phase 2：Owner 与事件调度

迁移并验证：

- `owner/registry.py`
- `owner/context.py`
- `orchestration/manager.py`

要求：

- 委派工具动作成为 Ownership 真源；
- 取消模型轮询；
- Owner 请求能即时唤醒；
- 任务完成/失败能恢复；
- 语义 Group 不被吞吐调度重写。

### Phase 3：Workspace 与 Artifact State

迁移并验证：

- `state/workspace.py`
- `state/artifacts.py`

要求覆盖：破图、stale PNG、HTML/PNG 哈希、scaffold/authored 区分、素材 handoff/repair 真源一致。

### Phase 4：QC 与意图驱动修订

迁移并验证：

- `qc/visual.py`
- `qc/content.py`
- `qc/routing.py`

删除精确修订文案协议。以实际工具行为识别 repair/ready/help。所有 Critic major 候选必须被复核或保持 unresolved，不能因候选预算静默变成 pass。

### Phase 5：入口与导出

迁移并验证：

- `infer.py`
- `synthesize.py`
- `export/delivery.py`
- `export/training.py`

要求 inference/synthesis 生成行为一致；首段 lineage 为 null；禁止自环和逆向 predecessor；WebUI 暂时仍指向旧生产目录。

### Phase 6：Shadow、Canary 与切换

顺序：

1. 用保存的 Trace 做离线 replay；
2. 用同一输入对比旧/新状态变化和产物；
3. 跑一条短非附件 canary；
4. 跑一条附件 canary；
5. 检查 delivery、QC、training export 和 resume；
6. 记录 commit、Skill hash、Harness hash 和 WebUI 集成 hash；
7. 用户确认后才更新 release 指针和 WebUI 入口。

旧 Harness 在新版本正式切换并留有可回滚 release 前不得删除。

## 8. 必须覆盖的回归问题

至少为以下真实问题保留 replay 或集成测试：

- Validator 与 Delegate 对 Plan Markdown 解析不一致；
- exact `ready` / issue prose 导致停滞；
- Owner 等待期间模型轮询；
- Image `failed_pages` 与 repair guard 状态不一致；
- HTML scaffold 被 WebUI 显示为已完成；
- `../assets/...` 导致图片加载失败却 render PASS；
- HTML 修改后使用陈旧 PNG；
- refinement intent 握手导致 patch 被拒绝；
- Critic major 候选被候选上限截断后错误 pass；
- training segment 首段自环或逆向 lineage；
- Provider 超时重试同一坏通道；
- Opus Thinking signature 在 compact 后重放失败；
- API 中断后重复 Research、Plan 或已完成页面；
- 共享页脚/标题样式漂移只记录不路由；
- 投影可读性 major 被当作普通 advisory；
- 不可获得素材没有新输入却反复激活 Slide Owner。

## 9. 实施纪律

- 使用小步 `apply_patch`，不得整目录覆盖；
- 每阶段单独提交或至少记录独立 tree hash；
- 不修改与本阶段无关的 Skill 行为；
- 不为减少文件数量而把新代码重新堆成另一个巨型文件；
- 同样不为架构美观预建没有真实职责的模块；
- 不创建第二套状态数据库；
- 不在测试中只验证历史 magic strings；
- 不用 Mock 的“能运行”替代真实 Trace replay；
- 不在用户验收前切换 WebUI 或批量合成入口；
- 新增非敏感文件保持所有用户可读；
- 不修改 `/mnt/afs/gaozhenxi/` 下任何权限或所有权。

## 10. 完成定义

只有同时满足以下条件才算完成：

1. 当前 v1 已验证能力没有退化；
2. Skill 的自然语言改写不会导致 Harness 状态失败；
3. Owner/Wave 使用事件唤醒，不产生模型轮询；
4. 修订由实际工具动作推进，不依赖固定文案；
5. inference、synthesis 和 WebUI 使用同一运行内核；
6. QC 的确定性问题有闭合处置，主观建议不造成无效返工；
7. checkpoint/resume 与训练 lineage 可重建且无环；
8. WebUI 状态和真实产物生命周期一致；
9. 旧生产版本可以一键回滚；
10. 用户逐阶段验收通过。

## 11. 接手后的第一条回复

接手 Agent 应先回复用户：

> 我会先执行 Phase 0，只冻结当前真实基线、整理迁移地图和回归样例，不修改生产 Harness。当前另一会话仍可能在写 `mural-presenter-v1.0`；我会先确认它已经结束，再开始基线冻结。Phase 0 完成后先交给你验收，不会自动进入 Phase 1。
