# Phase 7：实时轨迹与 WebUI 可观测性实施审计

审计日期：2026-09-03

审计范围：

- 旧运行时：`harnesses/mural-presenter-v1.0`；
- 新运行时：`harnesses/mural-presenter-v1`；
- 消费端：`webui/studio/app/trace.py`、`webui/studio/app/main.py`、
  `webui/studio/static/app.js`；
- 真实样本：旧 v1.0 历史工作区和 v1 Phase 6 / Opus 5 / 27B canary；
- 本阶段只复用旧版的用户体验目标，不复制旧版的多文件拼接架构。

## 1. 旧 v1.0 的真实轨迹合同

旧版并不存在一条可按全局游标消费的 Agent 事务流。WebUI 同时读取以下来源，再在请求时
重建一个近似时间线：

| 来源 | 写入方式 | 主要字段/内容 | WebUI 用途 | 已确认限制 |
| --- | --- | --- | --- | --- |
| `_trace/events.jsonl` | `O_APPEND` 逐行写入 | `epoch`、`clock`、`event_id`、`event_type`、`label`、`trace_label`、`role`、`message/event` 与 Owner lineage | runner 过程行、跨 Agent 粗粒度顺序 | Agent 各自维护局部 `trace_sequence`；没有 workspace 全局 sequence；写入后未 `fsync` |
| `_trace/{orchestrator,subagents/*}/messages.json` | 每个工具回合后整体覆盖 | Provider wire messages；Assistant 的 `text/thinking/tool_use`，User envelope 的 `tool_result` | 完整角色文字、工具调用、工具返回；替换 runner 日志中的截断文字 | 不是 append-only；一批工具全部完成后才刷新；原始 `thinking` 与 signature 同处一份消息 |
| `_trace/{orchestrator,subagents/*}/tool_log.json` | 每个工具回合后整体覆盖 | `turn/time/elapsed_s/tool_use_id/name/args` | 工具统计、参数提示、检索与素材活动 | 没有工具结果、错误、单次耗时；运行中只能看到已完成批次 |
| `_trace/{orchestrator,subagents/*}/config.json` | 生命周期节点覆盖 | `role/label/model/segment/owner/start/finish/exit` | Agent 身份、状态和耗时 | 状态与消息分离，需按目录名/任务文本推断别名 |
| `live-deltas.jsonl`（可选） | 流式增量 | 当前 response 的 text/tool-start delta | 首个完整 Assistant turn 落盘前的临时预览 | 不是训练真源；完整消息出现后被替换 |
| runner job log | stdout 追加 | `[sid/label]`、工具名、截断文字、相对耗时 | `/livefeed` 的初始全局顺序 | 不是结构化事务；工具结果不可见；换行和本地化会影响匹配 |

旧版 `write_trace()` 同时承担可恢复 Provider transcript 与 UI 消息来源，因此 WebUI 为了得到
“完整文字 + 全局顺序”需要把 `messages.json` 反向匹配到 runner 日志；匹配不到时会生成
小数 sequence。该行为可维持旧工作区浏览，但不适合作为 v1 的新实时协议。

## 2. 旧 WebUI 的消费和展示行为

1. `/api/decks/{id}/livefeed` 每 750 ms 被轮询。
2. 后端先解析 runner log，按 `orch/material/research/image/review/slide_*` 分组；再读取各目录
   `messages.json`，用文字或工具名匹配回 runner log 的位置。
3. 过程页显示角色名称、推断状态、Assistant 文本和工具调用聚合；工具成功/失败、耗时及
   一一配对不会进入 livefeed。
4. `/api/decks/{id}/agent-trace/{agent}` 每次读取整份 JSON，最多 16 MiB，没有 cursor。
   Trace 面板能展开 `tool_result`，也会把原始 `thinking` 标为“推理过程”。后端只对常见凭证键
   和 token 字符串脱敏，未把“只允许 summarized thinking”作为源端白名单。
5. 角色卡主要来自旧 trace 目录和 runner log。共享 v1 当前仅把 `_state/orchestration.json`、
   `_state/owners.json` 与最终 `_trace/tool-events.jsonl` 投影为阶段、Worker 数和最后工具；因此
   运行期间既没有角色消息，也没有工具结果。

## 3. 新 v1 已有的真实事件源

| v1 来源 | 已有事实 | 当前缺口 |
| --- | --- | --- |
| `AgentRuntime` | `agent_started`、`agent_turn_completed`、`agent_completed`；真实 response blocks；当前 segment | 异常路径没有 `agent_failed`；可见 text/允许展示的 summary 未投影 |
| `ModelSession` | 每次 `provider_attempt_started/succeeded/failed`；匿名 Channel、attempt、超时阶段、恢复决定 | 没有独立的 UI `provider_failover`；事件没有统一 workspace sequence/timestamp |
| `ToolRuntime` | 接受调用时追加 `tool_use`，handler 返回时追加唯一 `tool_result`；`tool_use_id` 已强配对 | 事件仅在内存；结果耗时缺失；PresenterKernel `finally` 才整体写 `tool-events.jsonl` |
| Owner/Orchestration 状态 | 真实 `owner_id/role/group/pages/task/wave/status`，支持同 workspace resume | 尚未与角色消息和工具事务合流 |
| checkpoint | 闭合工具边界、segment 与 Provider affinity | 实时流没有恢复 sequence 和事件去重 |

真实 v1 canary 证明现有三个兼容文件的行为：

- Opus 5 三页工作区：`agent-events` 93 行、`provider-events` 170 行、最终 `tool-events`
  204 行；`agent-events` 只有生命周期/回合元数据。
- 27B 全 Owner 工作区：六次真实 Agent 运行、245 次 Provider attempt、292 对工具事务；
  运行期间 WebUI 仍只能在任务结束后的 `tool-events.jsonl` 看到工具事件。
- 同 workspace resume 工作区：Provider 开放 attempt 会被追加一个
  `process_interrupted` 失败闭合，但新的 `ToolRuntime` 最终文件只反映本进程内存日志，不能作为
  跨进程实时游标。

## 4. 新旧字段映射

目标新协议为 `_trace/live-events.jsonl`，Schema 为 `mural.live-event.v1`。所有记录都使用同一
append-only journal 和 workspace 全局递增 sequence。

| 旧 v1.0 信息 | 当前 v1 信息 | 新实时字段 | 映射规则 |
| --- | --- | --- | --- |
| runner 行号、局部 `trace_sequence`、WebUI 小数补位 | 各组件局部顺序 | `sequence` | journal 在文件锁内读取最后合法 sequence 后分配 `+1`；resume 延续，不由前端推断 |
| `epoch` / `clock` / tool `time` / config start | Provider/Tool 调用时刻尚未统一 | `timestamp` | journal append 当下的 UTC RFC 3339 时间；不把无时区旧 clock 当新时间 |
| old canonical `event_id`；messages/tool_log 无 ID | Provider `attempt_id`、Tool `tool_use_id`、Agent/segment | `event_id` | 对真实源身份生成确定性事件 ID；重复 append 返回已有事件，不分配新 sequence |
| `sid`、`label`、`owner_id` | `agent_id` / ToolContext `owner_id` | `agent_id` | Orchestrator 固定为 `orchestrator`；Owner 使用 registry 的真实 ID |
| config `role` / event `role` | Agent/ToolContext `role` | `role` | 只写实际运行角色；不为未委派角色建事件或 UI 卡片 |
| tool_log `turn`、Assistant 数量推断 | Agent loop 本地 turn | `turn` | 从 checkpoint 中已有 Assistant 数量恢复逻辑回合；ToolContext 继承当前逻辑回合 |
| config `segment_id` | `SegmentCheckpoint.segment_id` | `segment_id` | Provider、message、tool 事件均绑定实际当前 segment |
| `message`、Assistant `text` | response `text` block | `event_type=assistant_message` + `payload.message` | 只投影用户可见 text，不记录 system/user prompt 或完整请求 |
| Assistant `thinking` | Anthropic summarized-thinking response；OpenAI `reasoning_content` | `event_type=assistant_analysis_summary` + `payload.message` | 仅在 Provider 请求明确为 `display=summarized` 且返回 summary 时投影；OpenAI reasoning、signature、redacted thinking 永不进入实时流 |
| messages `tool_use` + tool_log args | ToolRuntime 接受的 ToolCall | `event_type=tool_use` + `payload.tool` | 参数做结构化脱敏、截断和摘要；append、flush、fsync 后才执行 handler |
| messages `tool_result` | ToolRuntime 的唯一 ToolResult | `event_type=tool_result` + `payload.tool` | 同一 `tool_use_id`，记录 ok、结果摘要、错误码和 monotonic duration；handler 返回后立即 fsync |
| config/runner 的开始结束 | AgentRuntime lifecycle | `agent_started/completed/failed` | 真实入口、自然结束或异常处各写一次；status 从事件折叠，不猜测角色 |
| old provider structured event | ModelSession attempt event | `provider_started/succeeded/failed/failover` | attempt 字段白名单投影；失败决策确实选择另一 Channel 时追加 failover，不记录 key、请求正文或响应原文 |

## 5. 新架构边界

1. 新增一个 Provider/Agent 中立的 `LiveEventJournal`，只负责安全投影、全局排序、去重和同步
   落盘；它不成为 Owner、checkpoint、replay 或 synthesis admission 的真源。
2. `agent-events.jsonl`、`provider-events.jsonl` 和最终 `tool-events.jsonl` 保持兼容。
   实时流是附加索引，离线训练继续读取原有闭合消息/checkpoint。
3. ToolRuntime 在真实执行边界调用 journal sink；不从 stdout、最终文件或 WebUI 反向伪造事务。
4. WebUI 新增 cursor API，直接读取 `sequence > cursor` 的完整行；livefeed 可由同一事件投影
   兼容旧 UI，但不再为 v1 拼接 runner log 和 Agent-local 文件。
5. journal 只允许明确字段。任何名为 credential/token/password/authorization/signature 的值、
   Bearer/sk token、base64 图片和完整 Provider 请求均在写盘前删除或脱敏。
6. 文件尾若因进程硬中断留下不完整行，reader 跳过该行；writer 先追加换行边界，再从最后合法
   sequence 继续。已经完整 fsync 的事件不重写、不改号。

## 6. 验收证据方案

- 单元：journal append/fsync、恢复 sequence、确定性去重、尾部破损恢复、凭证与 signature
  白名单。
- 事务：用阻塞 handler 证明任务尚未结束时已能经 WebUI cursor 读到 `tool_use`，释放 handler
  后立即读到配对 `tool_result` 与耗时。
- 角色：真实 Orchestrator + Slide Owner，验证 Agent 状态、消息、工具事务均按真实 ID/role
  分组，Material/Research/Image 不出现。
- 恢复：中断开放 Provider attempt、同 workspace resume、真实 failover 决策，验证 sequence
  单调、event_id 唯一且旧事件仍可读取。
- WebUI：隔离端口和隔离 Studio data 运行 27B canary；在任务未结束、工具事务已出现时截图，
  保存旧/新 manifest、实时事件流、cursor 响应和截图对照。现有 WebUI 服务与既有 Opus
  workspace 不重启、不发信号。
- 发布：对照未全部通过前，公开 pointer/default 保持 v1.0；候选 canary 只使用显式开发策略。

## 7. 实施结果

1. `runtime/live_trace.py` 已提供 workspace 级 append-only journal。每行包含 schema、全局递增
   `sequence`、UTC `timestamp`、稳定 `event_id`、Agent/Owner 身份、逻辑回合和 segment；写入在
   文件锁内完成，并在事件边界执行 `fsync`。
2. Agent、Provider 和 Tool 运行时直接在真实事务边界投影事件。`tool_use` 在 handler 开始前
   落盘，`tool_result` 在 handler 返回后立即落盘；兼容 `agent-events.jsonl`、
   `provider-events.jsonl`、`tool-events.jsonl`、checkpoint 和训练导出保持不变。
3. WebUI 新增 cursor 增量接口，前端按 `event_id` 合并，按真实角色展示状态、Assistant 消息、
   Provider 显式 summarized analysis、工具参数摘要、结果、耗时和错误。旧 workspace 没有新
   journal 时仍走旧版投影。
4. 恢复入口会闭合上个进程留下的开放工具和 Agent 事务，再开始新 segment。稳定事件 ID 防止
   重放重复；损坏的文件尾会被隔离，后续 sequence 从最后一个完整事件继续。
5. 安全投影使用允许字段和递归脱敏，拒绝 thinking/reasoning/signature 字段、嵌入普通字符串的
   signature 文本、凭证、Bearer/token、完整请求正文、代码正文和 base64 载荷。

## 8. 验收结果

| 门禁 | 结果 | 证据 |
| --- | --- | --- |
| 运行中工具可见 | 通过 | 阻塞 handler 测试在任务结束前由 cursor API 读取到 `tool_use`，释放后读取到同 ID `tool_result`；真实 27B 运行中截图保存为 `canary/webui/20260903-live-trace-27b-r1/evidence/deck-2/01-live-tool-running.png` |
| 角色分流与事务配对 | 通过 | 真实 27B 工作区只有 Orchestrator、Slide Owner；最终 208 个事件，53 个 `tool_use` 与 53 个 `tool_result` 全部 exactly-once 配对 |
| 中断与同 workspace resume | 通过 | 真实中断边界 sequence 1051–1054 依次为开放调用、`process_interrupted` 结果、Agent 失败、新 segment 启动；工具 ID 保持且 sequence 连续 |
| Provider 恢复与 failover | 通过 | 真实 Opus 5 注入首字节超时后同 Channel 成功；注入失效 Channel 后仅 failover 一次并由健康 Channel 成功；开放 attempt 为零 |
| Trace 安全 | 通过 | 完成的真实 27B 工作区四份 trace 共 459 个 JSON 对象，凭证命中 0、thinking/signature 命中 0、解析错误 0；另有嵌入字符串回归测试 |
| 新旧 WebUI 对照 | 通过 | 旧 v1.0 无 cursor endpoint（保留旧 livefeed），新 v1 可一次读取 sequence 1–208 并增量续读；旧、新 manifest、API 响应、事件流与四张关键截图均保存在 `canary/webui/20260903-live-trace-27b-r1/evidence/comparison/` 及其上级目录 |

回归套件最终通过：v1 Harness 113 项、Studio 75 项、WebUI 仓库 50 项、v1 release guard
8 项、v1.0 release guard 7 项。真实 27B 工作区状态为 `completed`，1/1 页面完成并产出
PDF、PPTX 和静态 HTML。既有 Opus canary 与旧 WebUI 服务在整个实施和验收过程中均未被
重启或发送信号。
