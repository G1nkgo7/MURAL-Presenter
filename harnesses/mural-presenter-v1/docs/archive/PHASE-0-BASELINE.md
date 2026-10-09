# MURAL Presenter v1 Phase 0 Baseline

> 本文冻结 Phase 0 当时的迁移映射；其中把现行工具映射为 `delegate_await/owner_request` 的提案已在
> 2026-09-02 后续 Skill 对齐中撤销。当前接口以 `runtime/tools.py` 和 `V1.0-CAPABILITY-AUDIT.md` 为准。

冻结日期：2026-09-02
迁移源：`mural-paper-v1.0-stable-20260902-r39`

## 1. 范围与结论

Phase 0 只读取、盘点和测试旧实现，没有迁移或切换生产运行代码。用户随后明确授权进入 Phase 1。

冻结过程中，上一修改会话在初次 R38 盘点后完成了最后一次发布落盘。最终迁移基线因此重新钉在 R39；
R39 的 Harness、Skill、pair 与 WebUI integration 实际哈希均与冻结指针一致。新 Harness 不使用初次观察到
的 R38 哈希作为实现依据。

独立复跑结果：

- 旧 Harness：`557 passed in 75.95s`；
- 新目录在 Phase 0 时没有运行时代码；
- 生产入口、WebUI 路由和 release pointer 均未由本次重构改写。

## 2. Git 与源码快照

### 2.1 Git 身份

| 项目 | 值 |
| --- | --- |
| Branch | `agent/mural-visual-priors-v2` |
| HEAD commit | `4f37419cbb187ba9738e9c522d5919da404c177b` |
| HEAD tree | `ceed8dc09fcaa074e3751c1db3b5429cfaa177bf` |
| Release | `mural-paper-v1.0-stable-20260902-r39` |
| Release updated_at | `2026-09-02T07:01:26Z` |
| Release pointer SHA-256 | `146b54fd2eb0d40dae6b875c76c91a59845a347efb529117b013cb9707aa8ed2` |

工作树包含上一会话尚未提交的真实生产修改；不能用 HEAD tree 代替当前源码。迁移和回归以以下工作树
内容哈希为准，也不得执行 `git reset`、`git clean` 或历史目录覆盖。

### 2.2 R39 工作树哈希

| Surface | Actual SHA-256 | R39 pointer | 结果 |
| --- | --- | --- | --- |
| Harness `harnesses/mural-presenter-v1.0` | `b0b8c4ba41867dd77edcd4fc16977773b985539da1ec70d1b6d0e4d16ed1a49b` | 同左 | MATCH |
| Skill `skills/mural-presenter-v1` | `83d14495e34c159b0781b6818cddad9a321bed72c43adb5f212409dd3e8bb0fc` | 同左 | MATCH |
| Harness + Skill pair | `93f98cb66dcfe45aecff995ff4d0013494bcb656251acdee54a9906f9e2c4d5c` | 同左 | MATCH |
| WebUI integration | `eaf8f92fa0992763207fbda8d38205ced32620da5d19da49c246eecbf6832268` | 同左 | MATCH |

相关 Git 状态不是干净提交：旧 Harness、Skill 和 release pointer 有 tracked modifications，并有
`owner_packet.py`、`plan_metadata.py`、`runtime_accounting.py` 及对应测试等 untracked 源文件。这些均属于
R39 实际迁移源，不能遗漏，也不属于本次重构可清理范围。

## 3. 入口与集成面

### 3.1 当前入口

1. `harnesses/mural-presenter-v1.0/infer.py`
   - 读取同目录 `runtime.env`；
   - 校验冻结 release；
   - 启动共享 render broker；
   - 进入 `core.run_batch.main`。
2. `core/run_batch.py`
   - 同时承载 inference/synthesis mode、batch、附件 staging、Environment Doctor、workspace、resume 与
     result closure；当前没有独立的 `synthesize.py` 生成内核。
3. WebUI
   - 通过 release pointer 的 20 个 integration files 进入同一旧 Harness；切换前必须继续如此。

### 3.2 当前 WebUI integration files

`webui/scripts/launch.py`、`webui/inference/openai_backend.py`、`webui/inference/serve_one.py`、
`webui/studio/app/{custom_models,engine,environment_checks,jobs,main,pptx_export,service_config,trace,trajectory_monitor}.py`、
Studio static/templates，以及 `data/curation/adapters/clean_harness.py`。

Phase 0 发现 `trajectory_monitor.py` 仍以 slide HTML URL 的存在推导 `authored`。这会把 scaffold 误显示为
已创作页面，后续必须改为消费 Artifact lifecycle，但不属于 Runtime Core 阶段。

## 4. 当前模块清单

旧 `core/*.py + infer.py` 共约 48,250 行，其中：

| 模块 | 行数 | 当前主要职责 | 风险 |
| --- | ---: | --- | --- |
| `agent_loop.py` | 24,231 | 会话、角色循环、委派、恢复、QC、闭合与大量 MURAL 特例 | 多个状态机和语义门混合 |
| `tools.py` | 7,446 | 基础工具、权限、Artifact、render、vision、web、Image 与 QC | 机械执行和业务判断混合 |
| `run_batch.py` | 2,523 | 输入、batch、workspace、resume、结果 | 入口与生成内核混合 |
| `model_call.py` | 1,635 | Anthropic/OpenAI、Channel、timeout、failover、usage | Provider 与 Agent 全局状态耦合 |
| `environment_doctor.py` | 1,634 | 环境、字体、浏览器和附件预检 | 配置与业务预检混合 |
| `training_export.py` | 1,532 | segment、lineage、training admission | 与运行 Trace 状态交叉 |
| `artifact_transactions.py` | 1,216 | page lifecycle、HTML/PNG hash、checkpoint | 与 page index/Agent 字段重复 |
| `nova_raw.py` | 981 | 原始 Provider 请求/响应与内部 Trace | 需要保留完整证据顺序 |
| `wave_manager.py` | 921 | Wave、依赖、等待、状态恢复 | 后续迁入事件管理器 |

其余运行模块包括：`canvas_spec`、`config`、`content_qc`、`deck_state`、`issue_ledger`、`language`、
`model_slots`、`openai_shim`、`owner_context`、`owner_packet`、`owner_registry`、`plan_metadata`、
`release_guard`、`render_broker`、`revision_router`、`run_profiles`、`runtime_accounting`、
`runtime_capabilities`、`trace_mode` 与 `trace_schema`。

## 5. 当前工具面

### 5.1 基础工具

`read_file`、`write_file`、`patch`、`terminal`、`vision_analyze`、`image_generate`、`web_search`、
`web_extract`。

### 5.2 当前控制面工具

`delegate_task`、`delegate_submit`、`delegate_next_event`、`delegate_collect`、`request_orchestrator`、
`respond_owner_request`、`verification_submit`。

### 5.3 目标稳定动作

| 当前 | 目标 | 说明 |
| --- | --- | --- |
| Harness 预解析 Query/seed | `canvas_select` | Orchestrator 首轮确认，Harness 只校验并冻结 |
| `delegate_submit` | `delegate_submit` | 已接受参数成为 Ownership 真源 |
| `delegate_next_event` | `delegate_await` | 后端事件等待；Schema 不暴露轮询 timeout |
| `delegate_collect` | `delegate_collect` | 收集 terminal 结果 |
| `request_orchestrator` / `respond_owner_request` | `owner_request` | 一个显式 Owner→Orchestrator 事件 |
| 隐含在写入/Agent 文本中 | `artifact_commit` | 显式提交产物与源 hash |
| 隐含在 `terminal` 命令中 | `render` | 显式 render receipt |
| `vision_analyze` | `vision_analyze` | 保留原生视觉输入 |
| `verification_submit` | `verification_submit` | 显式 verdict/issue IDs |

目标工具动作不能携带或解析 `PAGE DECISION ...`、`ownership_topology`、固定 issue 列数等魔法文本。

## 6. 状态真源与重复状态

| 状态类别 | 当前持久位置 | 当前重复/矛盾面 | 目标真源 |
| --- | --- | --- | --- |
| 原始输入 | seed/config、raw query | Orchestrator 转述 | immutable `RunSpec` + optional UI hint |
| 已确认画布 | `plan/canvas.json` | Query 正则、Harness 默认尺寸 | frozen `CanvasSpec` |
| 页面与 Deck | `_state/page-index.json`、`deck-graph.json` | HTML 存在、Agent flags、artifact lifecycle | Artifact transaction + 文件/hash |
| Ownership | `_state/owners.json` | Plan Markdown、page index、Wave task、Agent 字段 | accepted `delegate_submit` + Owner Registry |
| Wave | `_state/delegation-waves.json` | 模型等待/轮询文本 | event manager checkpoint |
| Owner context | owner context/packet、messages、segment boundary | 大上下文重复读取 | bounded Owner checkpoint |
| Artifact | `_state/artifact-transactions.json`、checkpoints | page index、render manifest、Agent ready 文本 | Artifact Store |
| Issue | `_state/issues.json`、independent QC JSON | Agent flags/result/QC prose | Issue Ledger |
| Image | catalog、image-handoff、failed_pages、repair guards | 多个 failed/ready 真源 | image transaction + Catalog hash |
| Render evidence | PNG、render.json、slide-render-state、cache | stale HTML/PNG pair | current source/render hash pair |
| Trace | events.jsonl、messages、tool_log、segment/checkpoint、blobs | resume attempt 分散 | immutable session events + validated lineage |
| Training | runtime summary、control events、segments、manifest | 从多个目录二次猜测 lineage | `export/training.py` projection |

必须保留的页面生命周期为 `scaffolded → authored → rendered → inspected → ready`；WebUI 不得再把第一步
显示成第二步。

## 7. Current → Target 迁移表

| 目标模块 | 当前来源 | Phase |
| --- | --- | --- |
| `runtime/config.py` | `config.py`、`run_profiles.py`、`runtime_capabilities.py`、RunSpec/Canvas/Language 输入边界 | 1 |
| `runtime/session.py` | `model_call.py`、`openai_shim.py`、`nova_raw.py`、`trace_mode.py` 的会话部分 | 1 |
| `runtime/tools.py` | `tools.py` 的 Schema/dispatch/权限及 `agent_loop.py` 控制面工具 | 1 |
| `runtime/recovery.py` | Provider 分类/failover、compact、闭合工具事务、checkpoint/resume | 1 |
| `owner/registry.py` | `owner_registry.py`、当前 Ownership 写入/刷新逻辑 | 2 |
| `owner/context.py` | `owner_context.py`、`owner_packet.py`、Owner checkpoint/segment context | 2 |
| `orchestration/manager.py` | `wave_manager.py`、`agent_loop.py`/`run_batch.py` 调度与等待 | 2 |
| `state/workspace.py` | `deck_state.py`、`canvas_spec.py`、workspace/attachment paths | 3 |
| `state/artifacts.py` | `artifact_transactions.py`、render integrity、Catalog/Issue lifecycle | 3 |
| `qc/visual.py` | native/independent visual evidence | 4 |
| `qc/content.py` | `content_qc.py` | 4 |
| `qc/routing.py` | `revision_router.py`、`issue_ledger.py`、verification routing | 4 |
| `export/delivery.py` | `run_batch.py`/Skill delivery closure | 5 |
| `export/training.py` | `training_export.py`、trace lineage/admission | 5 |
| `infer.py` / `synthesize.py` | 当前 `infer.py` + `run_batch.py --mode` | 5 |

## 8. 不可退化回归清单

1. Provider Channel ID 匿名、稳定，不写入 key；新 unsigned segment 可均衡分配。
2. connect、first-byte、stream-idle、total-request timeout 分开记录。
3. 未提交的 partial stream 丢弃；只从最后闭合工具事务重放。
4. signed segment 固定 Channel；跨 Channel 前先建立 unsigned segment。
5. Invalid Thinking signature 每个源 segment 最多恢复一次，不重做已提交工作。
6. compact 等待 open tool transaction 闭合；首段 predecessor 为 `null`，无自环/前向边。
7. inference/synthesis 使用同一 live-context 与生成路径，仅 Trace retention/admission 不同。
8. Owner 等待不调用模型轮询；owner request、failure、dependency release、terminal 才唤醒。
9. 委派动作建立 Ownership；不得从 Plan Markdown 重建或用标点阻止委派。
10. Group Owner 保留组内记忆，但每页独立完成 write→render→inspect→commit。
11. 写入 lease、路径、tool use/result、HTML/PNG/Catalog hash 机械硬门保持。
12. broken image `naturalWidth=0` 不得发布 PASS；`../assets/...` 可机械归一化。
13. 修改 HTML 后不能复用 stale PNG；未验证 dirty page 不能 ready。
14. Image `failed_pages`、Catalog 与 repair guard 只有一个状态真源。
15. exact ready/issue prose、Markdown 包装与标点不再成为 runtime gate。
16. Critic major 候选全部 full-resolution 复核或保持 unresolved，不能因预算静默 pass。
17. 投影可读性、共享样式漂移和确定性 major 必须路由；主观 polish 保持 advisory。
18. delivery-ready 数只统计 current-hash ready receipts，不把中间 HTML/PNG 计为完成。
19. API 中断 resume 不重复 Research、Plan、Image 或已提交页面。
20. 不可获得素材在没有新 Catalog/hash 输入时不能反复激活 Slide Owner。

旧测试中的关键证据包括 `test_provider_failover.py`、`test_dual_mode.py`、
`test_artifact_transactions.py`、`test_postfreeze_lifecycle.py`、`test_wave_manager.py`、
`test_review_issue_routing.py`、`test_training_export.py` 与 `test_v10_plan_recovery.py`。

## 9. 真实失败 Trace replay 索引

以下组合哈希由排序后的 `sha256sum  relative/path` 行再次做 SHA-256 得出。选择范围为 result、Plan、
`_state/*.json`、events、messages、tool log、segment boundaries、runtime/training summary 与 image handoff；
不复制图片、附件、凭证或大体积原始 blob。Studio 可把 canonical workspace 移入 `_archived_attempts`，
因此 replay 以内容哈希与 fixture projection 为准，不能硬编码当前 Deck 路径作为长期 API。

| Deck | 失败与预期 replay 断言 | Phase 0 观察位置 | 选择集哈希 |
| --- | --- | --- | --- |
| 181 | whole-deck contact sheet 已打开却被 guard 反复要求“先打开当前 contact sheet”；同像素重复 view 必须停止 | `webui/studio/data/workspaces/1/decks/181`（观察后被 Studio rotation 清理） | `bb6f880122c5e3a5ea056eb09eb6faecda4e569ff560c0c6404b370b3b8a06a9` |
| 186 | page brief 含自然 `production_group`，Delegate 仍报 `ownership_contract_required`；显式 delegate 必须成功 | `_archived_attempts/186/fresh-retry-20260901T114721Z-1788263241258146006` | `8f293fdf247a8f329e91080c46c2fa45b6d5dbbf060d67e7ef6d58bb6873ec0c` |
| 189 | first-byte/transport failure后跨 Channel 成功；partial response 不进入 history | `_archived_attempts/189/fresh-retry-20260902T010921Z-1788311361412958552` | `fbf1d7fb69675578e07713511764649fc92bcef5698b6697b764c8c9ef76e671` |
| 190 | Provider outage、resume、Channel failover；保留已完成责任和 lineage | `_archived_attempts/190/fresh-retry-20260902T000431Z-1788307471783197329` | `ab4f49d4cd94a9e83b53db56ba7e48624fdfcf4fe12b7473b98ee83cd4c71ae9` |
| 191 | `ownership_topology` 使用全角标点后 Validator/Delegate 不一致；14 个 scaffold 仍为 `planned`，不能显示 authored | `webui/studio/data/workspaces/1/decks/191` | `ef73512cb8c1dcc7778ddc4ea42b72f22371c4a7c9ed0b3b44f2623d0113f5ea` |
| 192 | 14 页 rendered、仅 12 页 delivery-ready；P01/P10 repair 与 Image failed-pages/handoff 冲突不能靠 prose 修复 | `webui/studio/data/workspaces/1/decks/192` | `5493ea08e63aa381d26e09a01f1504b5e26b9a154d951ceeb0d40e3e08444cc3` |

Phase 1 已从 Deck 189 原始 `events.jsonl` 生成一个最小、无用户内容的 Provider failover projection，保留
原始 source SHA-256 与 event IDs；它不是手写 Mock。

## 10. Phase 0 验收边界

Phase 0 完成时：

- 未修改旧 Harness 或 Skill；
- 未修改 release pointer 或 WebUI；
- 未创建第二套 Plan/Ownership/Artifact 状态；
- 已把目标目录收敛为 Handoff 的 `orchestration/manager.py` 单管理器布局；
- 用户已于 2026-09-02 明确要求开始重构，因此允许进入 Runtime Core；
- Phase 2 及以后仍需在 Phase 1 验收后再开始。
