# MURAL Presenter v1 Architecture

## 1. 完整 Shadow 数据流

```text
CLI 原始输入 ──► runtime.config.RunSpec
                         │
                         ▼
              Production Skill Runtime
              ├── 冻结 SkillSnapshot / 附件 manifest
              ├── Environment Doctor / Renderer / ResourceSlots
              └── 注册正式角色工具与权限
                         │
                         ▼
               infer.PresenterKernel  ◄── synthesize.py（薄入口）
                         │
                         ▼
                  Generic AgentRuntime
              ┌──────────┴───────────┐
              │                      │
       Orchestrator Agent       Owner Agents
       完整 SKILL.md            四张 role card
              └──────────┬───────────┘
                         ▼
       orchestration / workspace / artifacts / QC / delivery
                                             └── training（仅 synthesis）
```

Skill 和 Agent 负责理解用户、规划、分组、设计与判断修法。Harness 只负责显式工具动作、状态、权限、
文件、哈希、事件、恢复、证据、交付和训练准入。

## 2. 状态真源

| 事实 | 唯一真源 | 明确不作为真源 |
| --- | --- | --- |
| 原始要求、附件、UI 提示 | `RunSpec` | Agent 转述 |
| 正式 Skill 与角色 Prompt | 工作区 SkillSnapshot + `_trace/skill-provenance.json` | 私有简化 Prompt |
| 已确认画布 | `plan/canvas.json` | Query 正则、Harness 默认值 |
| Owner 与 pages/group | 已接受 `delegate_submit` | Plan Markdown |
| 任务依赖和终态 | `_state/orchestration.json` | 模型轮询文本 |
| Owner resume | `_state/owner-context/*.json` | 仅内存 messages |
| 页面生命周期 | 文件 + Artifact transaction + hash | HTML 文件存在 |
| 图片可用性 | 实际解码结果 + Catalog hash | handoff 中的 ready/failed 文案 |
| QC 问题 | `_state/issues.json` | Critic 临时复述 |
| 训练准入 | `export/training.py` manifest | delivery completed 单一字段 |

## 3. 模块职责

### 3.1 Runtime

- `runtime/config.py`：不可变 `RunSpec`、统一 `MODEL_*`、完整 `CanvasSpec`；
- `runtime/session.py`：Anthropic/OpenAI 对等 transport、SSE streaming、Channel、usage 与 failover；
- `runtime/tools.py`：Schema、角色权限、闭合 tool transaction 与首动作 Canvas gate；
- `runtime/recovery.py`：失败分类、最后闭合事务、unsigned rollover 和 lineage；
- `runtime/agent.py`：唯一通用模型循环，统一响应分块、工具执行与结果回灌、真实图片回灌、checkpoint 和
  max-turn 终止；
- `runtime/production.py`：SkillSnapshot、附件/环境准备、Renderer、资源槽和角色受限生产工具；
- `runtime/skill_runtime.py`：只做生产装配，以完整 `SKILL.md` 运行 Orchestrator，以对应 role card 运行
  Material / Research / Image / Slide，共用同一个 `AgentRuntime`。

### 3.2 Owner 与调度

- `owner/registry.py`：由委派参数创建逻辑 Owner；同成员复用身份，新成员进入新 epoch；
- `owner/context.py`：短 OwnerPacket、有界 checkpoint、artifact hash 与 resume mode；修改含签名历史前会显式
  rollover 为 unsigned segment；
- `orchestration/manager.py`：异步任务、显式依赖、Owner 求助握手、actionable event cursor 与 restart resume。

`delegate_submit` 一次接受完整责任集合；Owner Registry 对该 wave 的全部合同只落盘一次。
`delegate_next_event` 没有模型轮询 timeout，它在条件变量上等待，只在 `owner_request` 事件、失败、
依赖解除或 wave terminal 时返回；`delegate_collect` 在 Harness 内收齐整波终态。

Slide Owner 只读冻结的 `plan/canvas.json`、本页 plan、编译后的共享视觉原语和交接结果，不重新读取完整
`plan/design-brief.md`。修订任务复用逻辑 Owner 时保留上下文；换 epoch/Owner 时显式转移页面 Artifact
ownership，旧 Owner 不能再提交同一 canonical 页面。

### 3.3 Workspace 与 Artifact

- `state/workspace.py`：canonical paths、稳定 page UID、页面生命周期和 Canvas CSS/WebUI manifest；
- `state/artifacts.py`：Owner lease、Artifact Catalog、图片解码、Renderer receipt、HTML/PNG/资源哈希。

Renderer 必须在调用前看到已提交的 canonical HTML。若冻结 Skill 在渲染时执行确定性 HTML 规范化，
Renderer 必须返回包含前后 hash 的 normalization receipt；没有该收据的渲染期源文件修改直接拒绝。

有效页面状态始终由当前文件重新派生：

```text
HTML 缺失                         → missing
只有 scaffold / 未提交新 HTML      → scaffolded
当前 HTML 已提交但无同源 PNG        → authored
当前 HTML/PNG 同源但未消费视觉证据   → rendered
当前哈希对已检查                    → inspected
检查后无新改动且 Owner 正常闭合      → ready
```

任何 HTML、PNG、Canvas CSS、页面引用的本地 CSS/图片变化都会使 render receipt 失效。

### 3.4 QC 与修订

- `qc/visual.py`：候选、位置、像素哈希、测量和逐项复核；
- `qc/content.py`：原始 Query、来源哈希与最终可见文本事实包，不用关键词替 Reviewer 判断；
- `qc/routing.py`：Issue Ledger、verification、accepted degradation 与原 Owner 路由。

同一问题用证据/像素指纹去重；像素或 Catalog 没有变化时不会重复激活 Owner。主观建议可以 advisory，
但 stale PNG、破图、资源失败和 unresolved deterministic major 保持硬门。

### 3.5 入口与导出

- `infer.py`：唯一 `PresenterKernel` 与依赖注入边界，CLI 默认创建正式 `SkillRuntime`；
- `synthesize.py`：只把 mode 固定为 synthesis；
- `export/delivery.py`：HTML/PDF/PPTX/result/manifest，不调用模型；
- `export/training.py`：单 Owner bounded segment、checkpoint、blob、lineage 和 admission。

Inference 和 synthesis 调用同一个 Skill backend 与 AgentRuntime。Synthesis 不复制创作循环，只在
delivery 之后强制完整 Trace 并增加训练 export。当前产物必须匹配工作区；被后续 repair 替代的历史
revision 只有在本 segment 的闭合工具结果同时证明路径与旧 hash 时才可准入。

| 行为 | inference | synthesis |
| --- | --- | --- |
| Skill、Prompt、模型、工具、页面 QC | 相同 | 相同 |
| 有界 Agent / Provider / Tool Trace | 是 | 是 |
| 完整可重放轨迹硬门 | 否 | 是 |
| 自动 Training segment / admission | 不导出 | 导出且必须 accepted |

## 4. Canvas 传播

```text
raw Query / UI hint
       │  仅作为 Agent 决策依据
       ▼
Orchestrator canvas_select
       │
       ▼
plan/canvas.json（首次冻结，唯一合同）
       ├── assets/canvas.css
       ├── Renderer(canvas: CanvasSpec)
       ├── Workspace/WebUI manifest
       ├── PDF physical DPI/size
       └── PPTX slide_width / slide_height
```

Harness 只校验正尺寸、范围、格式/方向、物理比例和安全区。用户即使已经写明 16:9、4:3 或 A1，仍由
Orchestrator 在首轮规划中确认，不增加额外模型调用。

## 5. 稳定控制动作

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

`verification_submit` 只接受 `ready` 或带结构化问题的 `issues_found`。Owner 角色只包含 Material、
Research、Image 和 Slide；整册闭合由同一个 Orchestrator 完成，不创建 Review Agent。自然语言 Trace
保留，但 exact ready 文案、Issue 分隔符和 Ownership Markdown 不参与状态推进。

## 6. 生产边界

当前新目录仍为 Shadow，但正式 Skill loader、Orchestrator/Owner generation backend、真实 Renderer /
vision、交付与 synthesis 训练轨迹已形成并通过 Alpha Vertical Slice canary。先前的非 Skill 单页 smoke
旁路已删除，仍不属于正式 canary。

尚未通过的 release 条件包括复杂附件、Material/Research/Image 真实 canary、Provider 中断后的同
workspace resume、WebUI 新旧状态/交付物对照及完整 v1.0 能力配对。旧 `mural-presenter-v1.0`、正式
Skill 内容、WebUI 和 release pointer 均未修改；完成这些缺口并经用户验收前不得切换或删除旧 Harness。
