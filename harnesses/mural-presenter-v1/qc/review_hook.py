"""inspect 的同步外部反馈协议；传递检查版本及作者原始补充问题，不猜测意图。"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping, Protocol


DEFAULT_INSPECTION_QUESTIONS = {
    "page": "先检查整页基础版式：四边安全区尤其底部、页脚/页码/说明文字/卡片是否贴边或触边、主要内容是否挤到画布边缘、内容块之间是否缺少呼吸感；如果是封面/开场页，还要检查它是否像内容页、小结页或信息仪表盘，标题与主视觉是否形成一张完整画面而非两个互不相关的矩形，是否包含表格、完整图表、多段解释、三条以上事实说明，或需要观众停留较久才能读完；图片没有圆角本身不是缺陷，只在边界、裁切或色调确实造成贴图感或破坏本册视觉语言时报告；再检查图文可读性、遮挡与裁切、图表关系、字号层级及空间分配；只指出有可见依据的问题。",
    "group": "比较带页号联系表中的共享排印、页码与导航、图像处理、色彩角色和背景变化是否仍属于同一设计系统；不同页不必完全同色，但相同语义角色应稳定，无明确叙事或信息层级作用的新主色、新背景体系或新明暗关系不能被当作节奏，ground_map 的存在也不能证明变化合理。检查页面是否因此像来自不同模板；看不清的细节指出需单页放大的页号，不声称已完成逐页全分辨率检查。",
    "asset": "检查素材或附件图中的主体与细节、文字与图例是否清楚，裁切是否损伤内容；用途或事实无法从图片确认时说明限制。",
}

CRITIC_PROMPT_VERSION = "visual-critic-question.v7"


def inspection_question(kind: str, supplemental: str = "") -> str:
    default = DEFAULT_INSPECTION_QUESTIONS[kind]
    return default + ("\n本次补充问题：" + supplemental.strip() if supplemental.strip() else "")


@dataclass(frozen=True)
class InspectionClosed:
    """当前 inspect 冻结的实际像素与页身份；不是额外的模型上下文。"""

    agent_id: str
    role: str
    inspection_id: str
    html_sha256: str
    png_sha256: str
    png_path: str
    workspace_root: str
    page: int | None = None
    segment_id: str = ""
    page_uid: str = ""
    scope_key: str = ""
    tool_use_id: str = ""
    question: str = ""
    owner_question: str = ""
    critic_question: str = ""
    critic_prompt_version: str = ""
    kind: str = "page"
    members: tuple[Mapping[str, object], ...] = ()


def build_critic_inspection_question(closed: InspectionClosed) -> str:
    """把 Owner 的 inspect 目标包装成独立、保守的视觉验收问题。"""
    owner_question = (closed.owner_question or closed.question).strip()
    if closed.kind == "group":
        pages = ", ".join(
            "P" + str(member.get("page"))
            for member in closed.members
            if member.get("page") is not None
        )
        target = f"带页号联系表；成员页：{pages or '未知'}。"
        intended = "比较这些页面的共享排印、导航、图像处理、色彩角色与背景变化，判断它们是否仍属于同一设计系统，并识别像不同模板拼接的明显跨页不一致。"
    elif closed.kind == "asset":
        target = "素材或附件图片；文件名、路径和来源不作为可见事实。"
        intended = "判断该图片作为素材是否清楚、可用，并指出无法仅由像素确认的用途或事实。"
    else:
        page = f"P{closed.page}" if closed.page is not None else "当前单页"
        target = f"{page}。"
        intended = "验收当前页面像素是否能支撑页面语义与投影阅读。"
    return (
        "用途："
        + intended
        + "\n检查对象："
        + target
        + "\n固定整页检查：如果检查对象是单页或页面截图，无论 Owner 关注点是否局部，先复核四边安全区、底部余量、页脚/页码/说明文字/卡片贴边、主要内容触碰画布边缘、内容块过满缺少呼吸感、以及投影时下边缘文字阅读风险；如果是封面/开场页，同时检查它是否像内容页、小结页或信息仪表盘，标题与主视觉是否形成一张完整画面而非两个互不相关的矩形，是否包含表格、完整图表、多段解释、三条以上事实说明，或需要观众停留较久才能读完。图片没有圆角本身不是缺陷；只在边界、裁切或色调有可见依据地造成贴图感或破坏本册视觉语言时报告。局部修改验收不得跳过整页基础版式检查。"
        + "\nOwner 关注点："
        + (owner_question or "未提供额外关注点；按默认目标检查。")
        + "\n独立验收：只基于可见像素判断；不要把文件名、来源、路径、Owner 说法或历史上下文当成已核验事实。"
        + "\n请检查：主体识别与清晰度、四边安全区、裁切/遮挡、噪声或压缩伪影、文字/图例/连线可读性、投影阅读风险、"
        "图文或科学语义误导、以及该图是否支撑上述用途。"
        + "\n输出：只报告必须修的阻塞问题和无法从像素确认但会影响验收的事实；每条问题给出可见依据、位置和影响。"
        "没有阻塞问题时一句话说明未发现阻塞问题；不要输出可选润色或非阻塞建议。"
    )


def inspection_with_critic_question(closed: InspectionClosed) -> InspectionClosed:
    """补齐 critic 专用问题；保持原 question 作为 Owner/缓存历史身份。"""
    owner_question = (closed.owner_question or closed.question).strip()
    prompt_version = closed.critic_prompt_version or CRITIC_PROMPT_VERSION
    prepared = replace(closed, owner_question=owner_question, critic_prompt_version=prompt_version)
    critic_question = closed.critic_question or build_critic_inspection_question(prepared)
    return replace(prepared, critic_question=critic_question)


class ReviewCoordinator(Protocol):
    def review(self, closed: InspectionClosed) -> dict[str, object]:
        """等待该检查的真实外部反馈，供同一 inspect tool_result 与 PNG 一起返回。"""


__all__ = [
    "CRITIC_PROMPT_VERSION",
    "InspectionClosed",
    "ReviewCoordinator",
    "build_critic_inspection_question",
    "inspection_question",
    "inspection_with_critic_question",
]
