# MURAL Presenter v1 Phase 6 Shadow Report

完成日期：2026-09-03  
状态：**Phase 6 release gate 已通过；v1 Shared Runtime 已提升为 current**

> 本报告保留此前能力审计的更正：早期两条 27B 运行没有加载 MURAL Presenter Skill，只是历史
> transport/kernel smoke。最终结论由正式 Skill-backed inference/synthesis、真实附件与四类 Owner、
> Provider 故障恢复、同 workspace resume、WebUI 交付及 production release guard 的组合证据支撑。
> 完整能力清单见 `V1.0-CAPABILITY-AUDIT.md`。

## 1. 自动回归

| Suite | 结果 | 结论 |
| --- | --- | --- |
| 新 Harness Shared Runtime | `111 passed in 54.60s` | 机械内核、AgentRuntime、SkillRuntime、恢复、release guard 与训练准入合同通过 |
| Studio | `72 passed in 80.79s` | 新旧 profile、状态投影、resume 与服务配置通过 |
| WebUI repository | `50 passed in 44.77s` | runner、模型传输、release parity 与交付集成通过 |
| v1.0 release guard | `7 passed` | 独立 rollback manifest 选择与 fail-closed 校验通过 |

新增覆盖包括：文本/多工具/错误与图片回灌、Provider rollover、闭合 checkpoint、max-turn 失败、正式
Skill/角色卡 Prompt 来源、OwnerPacket 与权限、完整脚本化纵向链、Renderer normalization receipt、
页面 ownership transfer，以及被 repair 替代的历史 artifact revision 训练准入。

生产 pointer 已切换为 `mural-paper-v1-stable-20260903-r1`，指向
`harnesses/mural-presenter-v1`。`mural-presenter-v1.0` 保留为 WebUI 显式 rollback，使用独立的
`mural-paper-v1.0-rollback-20260903-r41` manifest；历史 R40 manifest 保持不变。

## 2. Provider Trace replay

Phase 1 冻结的真实 Provider Trace 仍可验证 transport/failover 原语：

| Source | Attempts | Success | Failed | Failover | Timeout phases | Open at end |
| --- | ---: | ---: | ---: | ---: | --- | ---: |
| Deck 189 | 390 | 344 | 41 | 37 | `first_byte=23` | 5 |
| Deck 190 A | 652 | 600 | 46 | 43 | `first_byte=35` | 6 |
| Deck 190 B | 42 | 37 | 4 | 4 | `first_byte=1, stream_idle=1` | 1 |

Open attempt 保持真实开放状态，没有补造 terminal event。该结果只覆盖 Provider 层，不覆盖 Skill、
Agent loop、Owner 角色或真实训练轨迹。

## 3. 已撤销的非 Skill smoke

已删除的 `runtime/host.py` 曾连接生产 OpenAI-compatible 27B 端点：

`pptagent-qwen35-27b-6batch-qcv2-128k-mv2weighted-ckpt1764`

它自行构造简化 prompt、私有页面工具和固定单页 HTML 壳，没有读取 Skill，也没有运行正式角色链。

| 运行 | 正确分类 | Canvas | Provider | 产物 |
| --- | --- | --- | --- | --- |
| `20260902-27b-no-attachment-r4` | transport/kernel smoke | `1280×720` | 3 calls / 0 failed | HTML + PNG + PDF + PPTX |
| `20260902-27b-attachment-r2` | transport/kernel smoke | `1280×720` | 3 calls / 0 failed | HTML + PNG + PDF + PPTX |

这些历史运行只能证明端点、部分工具、Chromium、画布传播和导出可以被串联；不能证明 Skill 方法、
Material/Research/Image/Slide 路由、完整 Trace、训练 checkpoint 或 v1.0 等价。

## 4. 已接通的正式纵向路径

```text
infer.py / synthesize.py
        → SkillSnapshot + attachment staging + Environment Doctor
        → PresenterKernel → shared AgentRuntime
        → Orchestrator（完整 SKILL.md）/ Owner（对应 role card）
        → write → render → vision → revise → ready
        → finalize → contact sheet / page reverify → verification_submit
        → HTML + PNG + PDF + PPTX + Trace
        → training checkpoints/blobs/admission（仅 synthesis）
```

CLI 已无需外部注入 generation backend。Inference 与 synthesis 使用相同的 Skill、Prompt、模型、工具、
Owner runner 和页面 QC；synthesis 只额外强制完整 Trace 并自动导出训练 segment。

## 5. 真实 Skill-backed canary

| 运行 | 结果 | 关键证据 |
| --- | --- | --- |
| `20260902-v1-alpha-inference-r1` | 失败诊断 | 最终逐页检查后页面被降级，暴露 verification 未重新提升当前 hash pair |
| `...-inference-r2` | 失败诊断 | Renderer 规范化 canonical HTML，暴露缺少显式 normalization receipt |
| `...-inference-r3` | 中止诊断 | Slide 读取完整 Design Brief 被正确拒绝，确认角色最小读取边界 |
| `...-inference-r4` | 中止诊断 | repair Owner 无法提交旧 Owner 的 canonical 页面，暴露 ownership handoff 缺口 |
| `...-inference-r5` | **通过** | 三页、92 次模型调用、111 个闭合工具事务、8 次真实 vision、三页 ready、PDF/PPTX 完整 |
| `...-synthesis-r1` | admission rejected | 真实 repair 使首段 artifact hash 成为历史版本，硬门阻断并暴露训练验证缺口 |
| `...-synthesis-r2` | **通过** | 一页同路径生成、42 次模型调用、50 个闭合工具事务、5 次 vision、2 个真实 segment、admission accepted |

`inference-r5` 的正式 Skill tree hash 为
`01164b0136c7db827b7d336a4bc0f3de99ed434d2b3f8793045118e4ef003289`。Canvas 在任何 plan 写入前冻结；
此前动作仅为读取 Skill 规定的设计参考。三页最终 HTML/render/inspection hash 对齐，PNG/inspection hash
对齐，Artifact lifecycle 均为 `ready`，Trace 没有开放工具事务，也没有访问 `mural-presenter-v1.0`。

`synthesis-r2` 自动导出 Orchestrator 与 Slide 两个 checkpoint；4 个去重图片 blob 均按内容 hash 校验，
checkpoint 不含内联 base64，所有 artifact validation 为 `current`。历史 revision 只有在同一闭合
`tool_result` 同时证明路径与旧 hash 时才标记为 `checkpoint_attested_revision`；无回执 mismatch 仍拒绝。

## 6. Phase 6 最终门禁

| 门禁 | 结果 | 证据 |
| --- | --- | --- |
| Opus 5 三页 | **通过** | `20260902-v1-alpha-opus5-inference-r1`：3/3 ready，PDF/PPTX 完整，真实同 Owner repair 闭合 |
| PDF / Office / OCR | **通过** | `20260903-phase6-attachments-r2` 预检与 `20260903-phase6-attachments-owners-r2` 正式运行；native PDF、OCR-only PDF、PPTX 均解析，`unresolved=[]` |
| 四类 Owner | **通过** | Material、Research、Image 均 attempt 1 完成；Slide attempt 2 完成真实修复；3/3 页 ready |
| Provider timeout / failover | **通过** | `20260903-provider-r1/report.json`：注入 first-byte timeout 后同 Channel 成功；无效 primary auth 后只 failover 一次并由健康 Channel 成功 |
| 同 workspace resume | **通过** | `20260903-phase6-same-workspace-resume-r2`：强制终止后保留同 task、Owner、attempt、segment，旧 attempt 以 `process_interrupted` 闭合，最终交付完成 |
| WebUI manifest 对照 | **通过** | `20260903-manifest-compare-r2`：v1 为 current；v1.0 为 rollback；Harness、pipeline、pairing、caps 与 release manifest 分离 |
| WebUI 真实交付 | **通过** | `20260903-live-new-r2`：production guard、真实 PDF、Material/Slide、repair、checkpoint continuation、状态投影与 PDF/PPTX 全链完成 |
| Release / rollback | **通过** | 当前与 rollback 的 Skill/Harness/pair/integration 四项 hash 均零 mismatch，`dirty_override=false`；launcher 默认 skill 为 v1 |

WebUI live 首轮在交付前发现同哈希的最终重复检查会把 `ready` 降回 `inspected`，导出因此正确
fail-closed。修复后，同一 HTML/PNG 对的重复检查保留 `ready`；18 个针对性生命周期/交付测试与最终
同 workspace 复跑均通过。另修正 Studio 结果中的 `selected_skill_version`，使其记录实际版本而非语言值。

结论：Phase 6 约定的附件、Owner、Provider 恢复、同 workspace resume、WebUI 对照/真实交付及
release pointer 门禁全部满足。`mural-presenter-v1` 可作为当前 production 路径；v1.0 rollback 与历史
R40 证据继续保留，不删除旧实现。
