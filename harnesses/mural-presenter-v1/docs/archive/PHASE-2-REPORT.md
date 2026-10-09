# MURAL Presenter v1 Phase 2 Report

> 2026-09-02 审计说明：Owner/事件原语随后已对齐现行 Skill 的整 wave 协议，但正式 Owner 模型执行
> 仍未接入；以 `V1.0-CAPABILITY-AUDIT.md` 为准。

完成日期：2026-09-02
状态：Owner 与事件调度完成，保持 Shadow

## 1. 交付范围

- `owner/registry.py`
- `owner/context.py`
- `orchestration/manager.py`

用户已明确授权后续阶段自主连续推进，因此本阶段验收后没有停下来询问，直接进入 Phase 3；生产边界
仍保持不变。

## 2. 核心结果

1. Ownership

   - 只接受 `delegate_submit` 的 `role / pages / group_id / depends_on`；
   - 相同 group 与成员复用逻辑 `owner_id`，新 attempt 不改变身份；
   - 成员变化创建新 epoch 并归档旧 Owner；
   - attempt 使用 compare-and-set，过期执行不能覆盖当前状态；
   - 不读取或解析 Plan Markdown。

2. Owner Context

   - OwnerPacket 只包含原始 Query、附件、画布、成员、依赖和必要 Artifact 引用；
   - checkpoint 固定在最后闭合工具边界，并去除大图 base64；
   - artifact hash 相同则 resume，变化则 checkpoint rebuild，逻辑 Owner 变化则 cold start；
   - 如果 payload compaction 会改变含 signed thinking 的历史，显式创建 unsigned 后继 segment，清空
     Channel affinity，避免修改签名后原地重放。

3. Event Manager

   - `delegate_submit(tasks=[...])` 一次提交完整 wave，并把全部 Owner 合同原子落盘；
   - 只有 `depends_on_image:true` 的任务等待同波 Image，其余任务立即异步运行；
   - `delegate_next_event` 在条件变量上等待，不调用模型轮询；
   - Slide `request_orchestrator` 会暂停原 task/tool turn，Orchestrator 用
     `respond_owner_request` 答复后在同一调用继续；
   - failure、dependency unblocked、wave terminal 都是持久 actionable event；
   - Manager 重启把 running/waiting 标为 interrupted，角色 runner 恢复后继续原 task_id。

## 3. AFS 并发修复

AFS 上同进程多个 Registry 实例并发 `flock` 时会直接返回 `EAGAIN`。实现使用进程内 path-level lock
串行同路径实例，同时保留跨进程非阻塞 `flock` 重试，12 路并发 accept 最终只产生一个 active Owner。

## 4. 验证

当前相关专项覆盖身份、整波原子接受、成员 epoch、stale attempt、并发原子性、checkpoint、signed
rollover、整 wave、图片依赖、Owner 求助、失败传播与 restart resume；总回归结果见能力审计。

本阶段没有修改旧 `mural-presenter-v1.0`、WebUI、Skill 或 release pointer。
