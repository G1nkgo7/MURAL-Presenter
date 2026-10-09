"""Human-readable labels for internal provider request kinds."""
from __future__ import annotations


_REQUEST_KIND_LABELS = {
    "hermes_main": (
        "主 Agent 工作轮",
        "Orchestrator 或 Owner 的普通模型工作请求；hermes_main 是兼容保留的内部日志名。",
    ),
    "critic_visual": (
        "独立视觉 Critic",
        "外部视觉复核请求；只检查对应像素和用途，不是 Owner 自述。",
    ),
    "context_compaction": (
        "上下文压缩摘要",
        "Harness 的内部交接摘要请求；不执行创作工具，不代表任务输出。",
    ),
    "phase6_provider_canary": (
        "Provider 恢复探针",
        "运行时恢复/故障转移的协议探针。",
    ),
}


def request_kind_public(kind: object) -> dict[str, str]:
    """Return display metadata while preserving the original internal kind."""
    internal = str(kind or "").strip() or "unknown"
    label, note = _REQUEST_KIND_LABELS.get(
        internal,
        (
            internal,
            "未注册展示名的内部请求分类；按原始 request_kind 保留以便调试。",
        ),
    )
    return {
        "request_kind": internal,
        "request_kind_label": label,
        "request_kind_note": note,
    }
