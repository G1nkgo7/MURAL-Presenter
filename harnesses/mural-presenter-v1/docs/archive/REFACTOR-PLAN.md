# MURAL Presenter v1 Harness Refactor Plan

> 审计进度（2026-09-02）：Phase 1–5 只完成了机械内核原语，正式 Skill Runtime 和多项 v1.0
> 生产能力尚未迁移；Phase 6 未通过。详见 `V1.0-CAPABILITY-AUDIT.md`。旧
> `mural-presenter-v1.0` 继续承担生产与回滚用途。

## 1. 目标

将当前 `harnesses/mural-presenter-v1.0` 中累积的运行能力迁移到新的
`harnesses/mural-presenter-v1`，保留已验证能力，同时把 Harness 收敛为少状态、少硬门、
以 Agent 行为意图和真实产物推进的多 Agent 运行时。

本次重构不是重写 Skill，也不是改变论文方法。Skill 继续负责内容、视觉和 Agent 决策；Harness
只负责模型会话、工具执行、Owner/Wave 调度、产物完整性、恢复、QC 路由和导出。

当前生产目录 `harnesses/mural-presenter-v1.0` 在新实现通过回归和 canary 前保持可运行，不能被新目录
的中间状态覆盖。

# 2. 非目标

- 不为每个历史 Case 增加专用规则。
- 不引入新的视觉模板、Style Lens 或页面类型。
- 不让 Harness 判断主观审美或替 Agent 决定修改方案。
- 不维护第二份 Plan、Requirement Map 或 Ownership Schema。
- 不同时维护 inference 和 synthesis 两套生成循环。
- 不在迁移阶段顺手修改 Skill 行为。
- 不以固定修订轮数、固定页面密度或固定字号作为通用硬门。

## 3. 目标目录

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

除 `__init__.py` 外，第一版控制在约 15 个功能文件。测试与阶段基线文档属于验证材料，不计入功能
文件。迁移时不预建未来可能用到的空模块；只有真实职责无法继续放在现有模块中时才增加文件。

## 4. 生命周期与模块职责

### 4.1 接收任务

入口：`infer.py` 或 `synthesize.py`。

产生只读 `RunSpec`：

```text
raw_user_query
attachments
run_mode
authoring_profile
optional canvas_hint
language
model/provider settings
output workspace
```

`RunSpec` 由 WebUI/API/CLI 输入生成，不从模型编写的 Plan Markdown 中恢复。
它不创建默认画布，也不把 Query 中的 16:9、4:3、A1 文案转换成规格。

涉及模块：

- `runtime/config.py`：统一环境和 Profile。
- `runtime/session.py`：创建根模型会话。
- `state/workspace.py`：初始化工作区和 canonical paths。

### 4.2 Material、Research 与 Plan

Orchestrator 在首轮规划中先根据 raw query、附件和可选 UI hint 调用 `canvas_select`；该动作与首轮规划
同属一次模型调用，不增加额外模型轮次。Harness 校验后冻结 `plan/canvas.json`，随后 Orchestrator
自主决定是否启动 Material/Research，并写自然语言 Plan 与 Design Brief。

Harness 只负责：

- 启动并等待对应 Owner；
- 限制工具权限；
- 保存研究、材料和 Plan 产物；
- 将完成/失败事件返回 Orchestrator。

Harness 不解析页面的视觉意图、完成标准、构图或审美字段。Plan Markdown 是 Agent 间的语义交流产物，
不是 Harness 的状态数据库。

### 4.3 Owner 与 Production Wave

Orchestrator 通过工具动作表达委派意图：

```json
{
  "role": "slide",
  "pages": [2, 5],
  "group_id": "experiment-to-model",
  "depends_on": ["image"]
}
```

这个已接受的工具动作是运行时 Ownership 真源。Harness 不再从 `deck.md` 中反向解析
`ownership_topology` 或 `production_group` 来决定是否允许委派。

涉及模块：

- `owner/registry.py`：Owner 身份、role、pages、group、attempt 和状态。
- `owner/context.py`：Owner Packet、compact、checkpoint 和 resume。
- `orchestration/manager.py`：任务依赖、并发、事件等待和唤醒。

Plan 与实际委派不一致时记录 advisory，不因 Markdown 格式差异中断整册。若 Orchestrator改变分组，
以后一次明确的委派动作和新的 Deck version 为准。

### 4.4 Image 与 Slide 生产

Harness 仅维护机械事务：

```text
write artifact
→ render/fetch
→ record hashes
→ inspect
→ commit or keep issue open
```

涉及模块：

- `runtime/tools.py`：工具 Schema、调用、并行和 tool transaction。
- `state/artifacts.py`：页面、图片、哈希、状态、Issue Ledger 和 accepted degradation。

Image 的 `ready/failed/unavailable/fallback` 由实际获取结果和工具回执生成，不依赖自由文本 handoff 的
正则解析。Slide 的 `scaffolded/authored/rendered/inspected/ready` 由真实文件与工具事件推导。

### 4.5 原生视觉修订

Owner 查看当前 PNG 后：

```text
调用 patch/write_file     → repair intent
进入下一页或正常返回       → ready intent
调用 request_owner         → upstream help intent
```

Harness 不再要求：

```text
PAGE DECISION P04: ready
DELIVERY ISSUE P04: type | location | consequence
```

保留的机械规则：

- 只能修改自身 Ownership 范围；
- 修改必须基于当前 HTML/PNG 检查点；
- 修改后必须重新 render；
- 最终版本必须重新 inspect；
- stale PNG 不能作为当前证据；
- 同一未变化像素不能反复打开。

模型的自然语言说明完整保留在 Trace 中，但不作为工具执行权限门。

### 4.6 整册 QC 与修订路由

涉及模块：

- `qc/visual.py`：视觉 Critic，输出问题、位置和像素证据。
- `qc/content.py`：对照 raw query、材料/调研、Plan 和最终可见文本。
- `qc/routing.py`：路由 Slide Owner、Image Owner、Orchestrator 或 accepted degradation。

QC 不修改页面，也不覆盖 Agent 设计判断。确定性的陈旧 PNG、破图、截断、遮挡或主体小字可产生
客观 issue；主观审美建议保持 advisory。

需要页面修订时，Harness 创建内部 repair event 并复用原 Owner，不要求 Orchestrator手写复杂 revision
JSON。需要素材修复时，只有实际的新素材来源、Catalog 或哈希发生变化才重新激活 Slide Owner。

### 4.7 闭合与导出

涉及模块：

- `export/delivery.py`：present.html、PNG、PDF/PPTX、result 和 delivery manifest。
- `export/training.py`：训练 segment、lineage、checkpoint、image blob 和 admission。

Export 不调用模型、不修改 Plan、不激活 Owner。

Inference 与 synthesis 共用完全相同的生成、工具、修订和 QC 路径。区别只在：

- inference 可释放已经消费的在线图片字节，保留诊断 Trace；
- synthesis 保存可重建 segment/checkpoint，并执行 training admission。

### 4.8 Provider 与任务恢复

涉及模块：

- `runtime/session.py`：单次模型会话、messages、usage、context segment。
- `runtime/recovery.py`：Provider 错误分类、Channel failover、checkpoint 和 resume。

恢复点始终位于最后闭合的工具事务。旧 Thinking signature 不跨新 segment；原始完整 Trace 单独保留。
API 中断不触发重新 Research、重新 Plan 或重做已提交页面。

Anthropic Messages 与 OpenAI Chat Completions 只是两种平等的 API 格式：共同使用 `MODEL_*` 参数、
Channel pool、streaming、usage、failover 与恢复状态机，不按教师/学生角色选择执行流程。

## 5. 状态真源


| 状态                 | 唯一真源                               | 不再作为真源的内容           |
| -------------------- | -------------------------------------- | ---------------------------- |
| 用户要求、附件、UI 提示 | RunSpec                              | Orchestrator 转述            |
| 已确认画布             | `plan/canvas.json`                    | Query 正则、Harness 默认值   |
| Owner 与 pages/group | 已接受的 delegate 工具动作             | Plan Markdown 格式           |
| Wave 依赖和完成状态  | `orchestration/manager.py` checkpoint  | 模型轮询文本                 |
| Owner 上下文         | Owner checkpoint                       | 仅在线 messages              |
| 页面生命周期         | 文件、hash 与 artifact transaction     | WebUI 看到 HTML 文件即判完成 |
| 图片状态             | 获取结果、Catalog 与 image transaction | 自由文本 handoff             |
| QC 问题              | Issue Ledger                           | Orchestrator 临时复述        |
| 训练准入             | `export/training.py`                   | Deck 是否显示 completed      |

运行对象可以缓存这些状态，但缓存必须能从上述真源重建，不能形成第二份相互矛盾的状态机。

## 6. 硬门与软判断

### 6.1 保留的硬门

1. 工作区路径和 Owner 写权限。
2. 页面生产前已有经 `canvas_select` 冻结的合法画布，之后不得静默改变。
3. tool use/result 事务完整。
4. HTML/PNG 与图片 Catalog 的哈希同源。
5. 修改后重新 render/inspect。
6. checkpoint、resume 和 segment lineage 有效。
7. 最终导出产物完整。
8. 未解决的确定性 major 不进入正训练样本。

### 6.2 不再作为硬门

- Markdown 标题、字段顺序和标点。
- 固定 `ready` 句式。
- 固定 Issue Type 枚举。
- 主观留白、风格或细节润色。
- 固定修订轮数。
- 固定页面数量或每页条目数量。
- Plan advisory 的反复重跑。

## 7. 迁移阶段

### Phase 0：冻结与盘点

状态：已于 2026-09-02 完成；证据见 `PHASE-0-BASELINE.md`。用户已确认进入 Phase 1。

产物：

- 当前 v1.0 的 Skill/Harness/WebUI hash；
- 现有能力清单；
- 关键状态真源和重复状态清单；
- 真实回归 Trace 索引；
- 不得退化清单。

该阶段不改变运行行为。

### Phase 1：Runtime

状态：已完成，证据见 `PHASE-1-REPORT.md`。旧生产入口与 release pointer 保持不变；等待用户验收后
才能进入 Phase 2。

迁移：

- config；
- model/provider call；
- tool transaction；
- trace session；
- checkpoint/recovery。

旧入口继续调用旧 v1.0，新目录只运行单元测试。

### Phase 2：Owner 与 Wave

迁移：

- Owner Registry；
- Owner Context；
- Wave/Event wait；
- delegate/collect；
- task-level resume。

验收重点：无模型轮询、无 Owner 身份漂移、恢复后不重做已完成责任。

### Phase 3：Artifact State

迁移并收敛：

- page lifecycle；
- artifact transactions；
- Image handoff；
- Issue Ledger；
- HTML/PNG/Catalog hash。

删除重复的 Agent 字段真源，仅保留可重建缓存。

### Phase 4：QC 与意图交接

迁移：

- visual/content QC；
- repair routing；
- training admission。

移除自然语言格式硬门，用工具动作和 artifact event 推导 repair/ready intent。

### Phase 5：Inference、Synthesis 与 Export

建立共享 Coordinator，分别接入：

- `infer.py`；
- `synthesize.py`；
- WebUI engine；
- delivery export；
- training export。

生成行为必须一致，只有 Trace 保留策略与训练导出不同。

### Phase 6：回归、Canary 与切换

1. 单元测试。
2. 旧 Trace replay，不调用教师模型。
3. WebUI/inference/synthesis 集成测试。
4. 一条短 inference 与一条短 synthesis 配对 canary。
5. 一条附件 canary。
6. Provider 中断/resume canary。
7. 更新 release manifest。
8. 将 v1.0 设为只读归档。

## 8. 必须覆盖的真实回归


| 回归                           | 预期                                              |
| ------------------------------ | ------------------------------------------------- |
| #191 自然 Markdown Ownership   | 不因字段格式失败；委派动作正常建立 Ownership      |
| #192 自然语言修订              | 看图后直接 patch 可以执行，不要求固定 Issue 句式  |
| #192 Image failed-pages repair | Image 状态只有一个真源，不发生 handoff/guard 矛盾 |
| 破图`naturalWidth=0`           | render 不得返回 PASS，Owner 获得可恢复提示        |
| stale PNG                      | 修改后不能复用旧视觉检查                          |
| Provider Invalid signature     | 从闭合事务开启新 segment，不重做 Plan/Image       |
| 429/524/stream timeout         | 记录 timeout phase，换 Channel，checkpoint 可恢复 |
| Group Owner                    | 每页独立 write-render-inspect 事务，保留组内记忆  |
| Critic major candidate         | 全部分辨率复核或保持 unresolved，不静默 pass      |
| Training lineage               | 首段 predecessor 为 null，无自环或逆向引用        |
| inference/synthesis            | 同输入与同模型下生成执行路径一致                  |

## 9. 验收指标

### 9.1 架构

- 不再存在约 24,000 行的单体 Agent Loop。
- `orchestration/manager.py` 只负责事件推进，不包含 Provider、QC、导出和大段角色特例。
- 单个功能文件原则上控制在约 1,000–1,500 行以内；超过时必须有单一、清晰职责。
- 不存在第二份 Plan/Ownership/Artifact 状态机。

### 9.2 稳定性

- 可恢复格式差异不会导致整册失败。
- Provider 中断后不重做已提交阶段。
- 无模型层定时轮询。
- 无 unresolved deterministic major 的正训练样本。
- 旧 workspace 能通过显式 migration/resume adapter 恢复，或得到明确不可恢复说明。

### 9.3 效率

- 不再因修订口令格式拒绝 patch。
- 减少重复读取 Plan/Design Brief/Base CSS。
- ready 页面不因 minor polish 自动重开。
- Image 无新增输入时不触发无效 Slide 大改。
- Trace 能分解 Provider、模型、工具、渲染、等待和恢复耗时。

### 9.4 成品质量

- Skill 现有内容、设计、字体、图片、特殊页和 Persistent Owner 能力不退化。
- QC 仍能识别遮挡、截断、破图、主体小字、错误素材和内容漂移。
- Harness 提供证据和路由，但不替 Agent 选择审美修法。

## 10. 切换与回滚

切换前，正式 release manifest 继续指向 v1.0。新 Harness 通过全部回归后：

1. 冻结新目录 tree hash；
2. 更新 WebUI、infer、synthesis 指向 `harnesses/mural-presenter-v1`；
3. 保留 v1.0 release 指针用于回滚；
4. 新任务只使用 v1；
5. 旧任务 Resume 根据其原 release hash 选择旧实现或迁移 adapter；
6. 至少完成一轮短 canary 后才开放批量合成。

若新实现发生阻塞性问题，只回滚 release 指针，不用历史目录覆盖新源码。

## 11. 执行纪律

- 每个 Phase 单独提交和记录 hash。
- 迁移模块时同时迁移对应测试。
- 不从旧快照整目录覆盖当前源码。
- 不在同一提交同时改变架构与 Skill 行为。
- 不通过增加正则兼容表解决语义问题。
- 不在未完成回归前删除 v1.0。
- 每次切换前验证 WebUI、inference、synthesis 实际加载路径与 tree hash。
