"""隔离模型内容审计员：对照用户原文/来源审每页可见文本的覆盖、一致性与是否跑题。

仅供 raw 完成后的后处理，不参与制作完成或 resume：

- 审计员使用一个全新空白 Provider segment，只喂固定版本化 Rubric + 一个由
  ``ContentQC.build_packet`` 编译的当前事实包。作者计划/讲稿与来源分开，当前像素绑定的
  视觉 Critic 观察只作为可核对证据；不注入 Owner 对话历史或过期审核结论。
- 输出是结构化 JSON findings（覆盖缺口/事实不符/跑题等，带 severity），由
  ``ContentQC.review`` 复用同一 ``ContentReviewer`` 契约校验为 ``ContentFinding``。
- 审计员不可用/超时时，``run_content_review`` 捕获异常并记 ``status="incomplete"``；
  后处理据此保留待审，不把失败当成 clean 或内容负样本。

审计员只读、只报告；它不写工作区、不改页面，不向创作 Issue Ledger 或训练消息注入 findings。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from qc.content import ContentFinding, ContentPacket, ContentQC


CONTENT_RUBRIC_VERSION = "content-audit-rubric.v5"

# §固定短 Rubric。只审可从事实包可靠判断的内容级问题：用户明确要求的关键信息是否在
# 可见文本里有覆盖、可见陈述是否与来源文档矛盾、整册是否明显跑题。风格/审美不在此列。
CONTENT_RUBRIC = (
    "你是独立内容审计员。给你一个 JSON 事实包，含用户原始请求 raw_user_query、来源文档 "
    "sources（每份带 path/sha256/text）、每页最终可见文本 pages（每页带 page/text）、"
    "作者计划 authored_plans、派生最终讲稿 derived_speech 和当前视觉审查记录 visual_evidence。"
    "只根据这些事实判断内容级问题，不评价视觉、排版或美观：\n"
    "1) 覆盖：用户原文明确要求的关键信息、主题或章节是否在可见文本里出现；\n"
    "2) 一致：具体事实/数字/名称是否与来源文档矛盾，或存在高置信的基础概念/逻辑错误。"
    "无来源也可指出明确错误的定义、关系或成立条件，即使全册一致写错；但不得凭记忆"
    "断言不确定、时效性强的外部事实，证据不足时说明核验需求而不是编造纠正值；\n"
    "3) 跑题：整册可见文本是否明显偏离用户请求的主题；\n"
    "4) 无据现实：可见文本是否把用户从未提供的、关于演示者自身真实场景的具体安排"
    "（下一节课/会议的时间、房间、日期、地点、联系方式等）当作已确认的现实呈现，且没有"
    "“示例/illustrative”之类的边界标注。判定基准是 raw_user_query 与 sources：这些来源里"
    "没有该具体值、可见文本却把它写成确定事实（而非泛指或显式示例），就报 kind=factual_conflict。"
    "注意区分：用户明确要求的教学/演示用虚构案例（如示范用的会议纪要示例、练习样本、假想数据）"
    "是被允许的，不要因为它们“查无来源”就报错——只针对把演示者自身的真实安排编成具体值这一类。\n"
    "5) 来源边界由用户原文决定，不因工具可用或未配置 profile 就允许补造材料之外的事实。"
    "用户要求与材料冲突时说明冲突，不把无据要求当已证实事实，也不把这种诚实边界报成遗漏。\n"
    "6) 引用：used_images 是实际渲染图片；有 source_document/source_page 时来自同哈希裁图回执。"
    "按 path 对照 catalog；用户要求上屏引用时，按其指定的对象、位置与形式核对，"
    "不把参考文献页要求扩大成每页、每图的来源脚注。确需可见署名时按具体条款核对。"
    "后台记录不能证明引用已上屏；未要求引用且无必需署名时不强加页脚。"
    "rendered_text 是浏览器文字采样，不等于完整像素事实；text 含 HTML 静态文本，"
    "隐藏区域不能证明已上屏，采样缺失也不能证明未上屏。不能仅因 text 中有而采样中没有"
    "就要求改正确的 DOM/图表；可见性疑点须以当前视觉观察核实，没有证据不得臆断。"
    "used_images/rendered_text 为 null 表示没有对应渲染证据，不能声称已核验图片引用或显示位置。\n"
    "7) 讲稿也是交付内容：逐页核对 authored_plans 中完整讲稿章节与 derived_speech，"
    "并与当前 pages 和 sources 对照。页面已经修正而讲稿仍沿用旧定义、因果、数字、读图方向、"
    "单位或引用时同样报告；不要因为错误只在讲稿中就忽略。作者计划不是事实来源，不能用它"
    "证明页面或讲稿正确；非上屏的详细讲解本身不算与精简页面矛盾。derived_speech 为 null"
    "表示未生成讲稿，不代表作者讲稿已审核。若作者计划与派生讲稿不一致，指出具体"
    "源路径、页码和矛盾短句，修复应回到作者计划再派生讲稿，owner_hint=orchestrator。\n"
    "8) 图文语义：used_images.rendered_instances 中 alt 是作者标签而非像素事实；"
    "同图在不同位置的标签都须结合当前页面文字、图注和来源核对。visual_evidence 只含当前"
    "HTML/PNG/inspection 绑定的独立视觉 Critic 结果；completed=false 表示未获得有效观察。"
    "已有观察指出对象、物理状态、坐标或图例与文字冲突时，核对它是否构成内容误导；"
    "READY 是已检查版本的收束状态，不代表 Critic 赞同或科学正确；需核对具体反馈与处置依据。你没有直接收到图片，不能"
    "从文件名、alt、计划描述或缺失的观察臆测图像实际内容，不能宣称重新完成了视觉核验。\n\n"
    "只输出一个 JSON 数组，数组每个元素是一条 finding，字段固定：\n"
    '{"kind": "<coverage_gap|factual_conflict|off_topic|internal_contradiction|citation_gap>", '
    '"severity": "<advisory|minor|major|critical>", "pages": [页码整数], '
    '"summary": "<说明页码、来源或讲稿路径与矛盾短句>", "owner_hint": "<slide|image|orchestrator|none>"}\n'
    "severity 用 major/critical 表示会误导读者或明显未满足用户要求的问题，minor/advisory "
    "表示轻微或可选改进。若没有任何内容级问题，输出空数组 []。"
    "不要输出数组以外的任何文字、解释或 Markdown 代码围栏。"
)


# 注入器契约：给定一个已编译的 ContentPacket，返回审计员原始文本（应为 JSON 数组）。
# 由 skill_runtime 用真实隔离 Provider 装配；测试可注入脚本化实现。
ContentReviewInvoker = Callable[[ContentPacket], str]


@dataclass(frozen=True)
class ContentReviewResult:
    """一次内容审计的可持久化结论（对齐 CriticResult 的 audit/display 拆分）。"""

    rubric_version: str
    reviewer_model: str
    status: str                    # completed / incomplete
    findings: tuple[ContentFinding, ...] = ()
    incomplete_reason: str = ""
    raw_text: str = ""
    page_count: int = 0
    source_count: int = 0

    @property
    def unresolved(self) -> bool:
        """存在未处置的高危 finding（major/critical）→ 内容 QC 未通过。"""
        return any(
            finding.severity in {"major", "critical"} for finding in self.findings
        )

    @property
    def passed(self) -> bool:
        """完成且无未处置高危 finding 才算通过。"""
        return self.status == "completed" and not self.unresolved

    def to_audit_dict(self) -> dict[str, object]:
        """返回可进入 raw trace 的完整记账字段（不含凭证/私有 thinking）。"""
        return {
            "schema": "mural.content-qc.v1",
            "rubric_version": self.rubric_version,
            "reviewer_model": self.reviewer_model,
            "status": self.status,
            "passed": self.passed,
            "unresolved": self.unresolved,
            "incomplete_reason": self.incomplete_reason,
            "page_count": self.page_count,
            "source_count": self.source_count,
            "findings": [finding.to_dict() for finding in self.findings],
        }

    def to_summary(self) -> dict[str, object]:
        """返回训练准入/导出层需要的紧凑汇总（对齐 external_visual_critic 汇总形状）。"""
        return {
            "effective": True,
            "status": self.status,
            "passed": self.passed,
            "content_qc_incomplete": self.status != "completed",
            "unresolved_high_severity": self.unresolved,
            "rubric_version": self.rubric_version,
            "reviewer_model": self.reviewer_model,
            "finding_count": len(self.findings),
            "incomplete_reason": self.incomplete_reason,
        }


def parse_content_findings(raw_text: str) -> list[Mapping[str, object]]:
    """从审计员原始文本机械解析出 finding 对象数组；容忍围栏与前后噪声。

    只做「找出 JSON 数组」这一步，不替审计员判断严重度或页码——字段校验交给
    ``ContentFinding.from_dict``（经 ``ContentQC.review``）。

    只有显式的 [] 才表示未发现问题；空响应不是完成审计的证据。
    """
    text = str(raw_text or "").strip()
    if not text:
        raise ValueError("content reviewer returned empty output")
    # 去掉常见的 ```json ... ``` 围栏。
    if text.startswith("```"):
        fence_body = text.split("```", 2)
        if len(fence_body) >= 2:
            candidate = fence_body[1]
            if candidate.lstrip().lower().startswith("json"):
                candidate = candidate.lstrip()[4:]
            text = candidate.strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise ValueError("content reviewer output has no JSON array")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, list):
        raise ValueError("content reviewer output is not a JSON array")
    findings: list[Mapping[str, object]] = []
    for item in parsed:
        if not isinstance(item, Mapping):
            raise ValueError("content reviewer findings must be objects")
        findings.append(dict(item))
    return findings


class IsolatedContentReviewer:
    """把一个隔离 Provider 注入器包装成 ``ContentQC`` 认识的 ``ContentReviewer``。

    ``ContentQC.review(packet, reviewer)`` 期望 ``reviewer(packet)`` 返回 finding 对象
    序列；本类调用隔离注入器拿原始文本、机械解析成 finding 对象数组交回给 ContentQC 校验。
    """

    def __init__(self, invoker: ContentReviewInvoker) -> None:
        self._invoke = invoker
        self.last_raw_text = ""

    def __call__(self, packet: ContentPacket) -> Sequence[Mapping[str, object]]:
        raw_text = self._invoke(packet)
        self.last_raw_text = str(raw_text or "")
        return parse_content_findings(self.last_raw_text)


def run_content_review(
    packet: ContentPacket,
    reviewer: ContentReviewInvoker,
    *,
    reviewer_model: str,
    rubric_version: str = CONTENT_RUBRIC_VERSION,
) -> ContentReviewResult:
    """跑一次内容审计并优雅降级：Provider/解析失败都记 incomplete，绝不抛。

    仅供 raw 完成后的独立 QC 使用，不回写创作工具事务或 Owner 的问题记录。
    incomplete 不代表内容失败，也不能作为 clean 准入通过。
    """
    isolated = IsolatedContentReviewer(reviewer)
    try:
        findings = ContentQC.review(packet, isolated)
    except Exception as exc:  # noqa: BLE001 - 任何 Provider/解析/校验错误都降级为 incomplete
        return ContentReviewResult(
            rubric_version=rubric_version,
            reviewer_model=str(reviewer_model),
            status="incomplete",
            incomplete_reason=f"{type(exc).__name__}: {str(exc)[:400]}",
            raw_text=isolated.last_raw_text,
            page_count=len(packet.pages),
            source_count=len(packet.sources),
        )
    return ContentReviewResult(
        rubric_version=rubric_version,
        reviewer_model=str(reviewer_model),
        status="completed",
        findings=findings,
        raw_text=isolated.last_raw_text,
        page_count=len(packet.pages),
        source_count=len(packet.sources),
    )


__all__ = [
    "CONTENT_RUBRIC",
    "CONTENT_RUBRIC_VERSION",
    "ContentReviewInvoker",
    "ContentReviewResult",
    "IsolatedContentReviewer",
    "parse_content_findings",
    "run_content_review",
]
