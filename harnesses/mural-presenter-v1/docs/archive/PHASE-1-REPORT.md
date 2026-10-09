# MURAL Presenter v1 Phase 1 Report

> 2026-09-02 审计说明：本报告只证明 Runtime Core 的局部合同，不代表正式 Skill Runtime 或 v1.0
> 能力等价；以 `V1.0-CAPABILITY-AUDIT.md` 的总体结论为准。

完成日期：2026-09-02
状态：Runtime Core 已完成；本文保留 Phase 1 当时快照，用户随后已授权自主推进 Phase 2–5

## 1. 交付范围

本阶段只实现并验证：

- `runtime/config.py`
- `runtime/session.py`
- `runtime/tools.py`
- `runtime/recovery.py`

同时增加最小单元/离线 replay 测试与架构说明。旧 `mural-presenter-v1.0` 入口、Skill、WebUI、
release pointer 均未由本阶段修改；新 Runtime 尚未接入生产。

## 2. 实现结果

### 2.1 Config

- 用不可变 `RunSpec` 固定 raw query、attachments、mode、authoring profile、可选 `canvas_hint`、language、
  Provider 与 output workspace；不再预先构造默认画布；
- Provider 环境参数统一为无角色前缀的 `MODEL_*`；删除新 Runtime 中的 `CLEAN_*`、`STUDENT_*` 和
  Anthropic 专属凭证命名；
- inference/synthesis 共用 generation/live-context profile，只在 complete-trace requirement 上不同；
- dotenv 解析不修改全局环境，启动参数覆盖文件默认值；
- manifest 不包含 API key/token。

### 2.2 Session 与 Provider

- Provider key 只用于 transport，Trace 使用稳定匿名 Channel ID；
- 两种 API 格式使用同一个 `ProviderSettings`、`MODEL_*` 凭证池、匿名 Channel、`ModelSession` 和恢复
  流程；模型池由显式 `MODEL_POOL_ENV_PATH` 指定，不再按教师/学生或模型名分叉；
- Anthropic streaming 区分 connect、first-byte、stream-idle、total-request；
- OpenAI Chat Completions 已支持 SSE streaming，聚合文本、reasoning、分片 tool calls、finish reason 和
  尾部 usage，并同样区分 first-byte、stream-idle、total-request；
- OpenAI 格式保留 DeepSeek thinking、Qwen/vLLM `enable_thinking`、OpenAI Chat `reasoning_effort`
  三类互斥 transport，未实现的 Responses API 明确失败；
- partial response 失败不修改 durable messages；attempt start 与 terminal event 可离线配对；
- signed history 保持 Channel affinity，跨 Channel 前要求 unsigned segment rollover。

### 2.3 Tool Runtime

- 统一 JSON Schema、角色权限、workspace-relative path 与 Owner write scope；
- 增加 Orchestrator 专用 `canvas_select`。Agent 显式选择后，Harness 只校验 320..8192 像素范围、格式/
  方向一致性、物理尺寸与安全区，并首次冻结 `plan/canvas.json`；同值重试幂等，异值覆盖被拒绝；
- 每个接受的 `tool_use` 恰好闭合一个 `tool_result`，包括 validation、permission 与 handler error；
- 相同 `tool_use_id` 的相同调用幂等，不重复副作用；
- 稳定动作现已按当前 Skill 收口为 `canvas_select`、`delegate_submit`、`delegate_next_event`、
  `delegate_collect`、`request_orchestrator`、`respond_owner_request`、`artifact_commit`、`render`、
  `vision_analyze`、`verification_submit`；
- `delegate_next_event` 不包含模型轮询 timeout；无 Plan Markdown、exact ready 或 issue prose gate。

### 2.4 Recovery

- retryable transport、HTTP、authentication/channel rejection 与 Invalid signature 分开分类；
- unsigned 请求先尝试未试过的其他 Channel，全部试过后才 bounded same-Channel backoff；
- authentication/channel rejection 没有替代 Channel 时不重复撞同一坏凭证；
- compact 只允许发生在闭合工具事务边界；
- rollover 从最后闭合事务恢复，删除 signed/redacted thinking、清空 affinity 并记录拒绝 Channel；
- Invalid signature 每源 segment 最多恢复一次；lineage 拒绝 duplicate/self/forward/missing predecessor。

## 3. 测试与 replay

### 3.1 自动测试

原先单列的 `test_openai_transport.py` 已合并为 `test_provider_transports.py`；其中同时覆盖 Anthropic
streaming 成功/stream-idle 与 OpenAI streaming、非流式、tool-call 分片、usage 和 stream-idle。

| Suite | 结果 |
| --- | --- |
| 新 Runtime Core | `47 passed in 7.17s` |
| 当前旧 Harness 全量 | `557 passed in 87.72s` |
| Python compileall | PASS |
| `git diff --check`（新目录） | PASS |
| Ruff | 当前环境未安装，未声称执行 |

### 3.2 真实完整 Trace replay

`replay_provider_events(..., allow_open=True)` 对失败任务保留未闭合 attempt，而不是补造 terminal result。

| Source | Attempts | Success | Failed | Failover | Timeout phases | Open at process end |
| --- | ---: | ---: | ---: | ---: | --- | ---: |
| Deck 189 archived full `events.jsonl` | 390 | 344 | 41 | 37 | `first_byte=23` | 5 |
| Deck 190 archived attempt A | 652 | 600 | 46 | 43 | `first_byte=35` | 6 |
| Deck 190 archived attempt B | 42 | 37 | 4 | 4 | `first_byte=1, stream_idle=1` | 1 |

Deck 189 的最小 committed replay projection 保留原始 source SHA-256
`c6ef3cf7724982dd9b83a76353daceee93fc31022bb23b543063215e1c5a7871`、event IDs 与原始计时，
验证一次 120.811s first-byte timeout 后立即从 `ch-a-*` 切到 `ch-k-*` 并成功；fixture 不含用户内容、
附件或凭证。

## 4. 独立内容哈希

哈希算法：对相对路径排序后的 `sha256sum` 行再次做 SHA-256，排除 `__pycache__` 与 `.pyc`。

| 内容 | SHA-256 |
| --- | --- |
| `runtime/` | `bed12703d70b6f719c08211bc1faaa173c58a8cc943e0a49d2a7c63c84ab6bc1` |
| `tests/` | `f982b4d809367db884815d7e3b93fe2c7959ba9519601a40d523f017006dc63a` |

## 5. 生产隔离复核

阶段结束前 release pointer 已由外部工作流从 R39 更新为
`mural-paper-v1.0-stable-20260902-r40`；R40 仍引用与 Phase 0 相同的 Harness、Skill、pair 和 integration
四个源码哈希。`verify_runtime_release("inference")` 返回零 mismatch；本阶段未写 release pointer。
新代码没有改变旧运行路径。没有把
旧 24,231 行 `agent_loop.py` 整体复制到新目录。

## 6. Phase 1 结束时的边界（历史记录）

以下内容未在 Phase 1 越界实现：

- Owner Registry/Packet 与 task-level resume；
- Wave dependency、条件变量事件等待和 Owner 唤醒；
- page/image Artifact lifecycle、render hash 与 Issue Ledger；
- visual/content QC 与修订路由；
- delivery/training export；
- infer/synthesize/WebUI 接入和 release 切换。

其中画布的未来消费者必须从同一 `plan/canvas.json` 加载规格：CSS/Renderer/WebUI 接线属于 Phase 3，
PPTX 接线属于 Phase 5。Phase 1 没有为证明“已接入”而修改当前生产入口。

这些在 Phase 1 结束时属于 Phase 2–6。用户随后明确要求不再逐阶段提问并自主推进；当前实现结果见
`PHASE-2-REPORT.md` 至 `PHASE-6-SHADOW-REPORT.md`。
