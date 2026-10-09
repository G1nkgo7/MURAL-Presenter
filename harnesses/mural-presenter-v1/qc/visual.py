"""规范视觉 Critic 候选、像素证据和逐项复核结果。

Harness 不判断布局是否“好看”。它只保存分析器返回的候选与证据，并保证所有 major
候选都有 confirmed、rejected 或 unresolved 结果；预算不足只能变成 unresolved。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Callable, Mapping, Sequence

from state.artifacts import AssetValidation
from state.workspace import PageSnapshot


VISUAL_SEVERITIES = frozenset({"advisory", "minor", "major", "critical"})
VISUAL_OWNER_HINTS = frozenset({"slide", "image", "orchestrator", "none"})


class ReviewVerdict(str, Enum):
    """列出 Critic 候选的完整复核结果。"""

    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class PixelEvidence:
    """保存问题位置、当前 PNG 哈希与可选测量值。"""

    png_sha256: str
    x: int
    y: int
    width: int
    height: int
    metric: str = ""
    measured: float | None = None
    threshold: float | None = None

    def __post_init__(self) -> None:
        """校验证据区域和哈希基本约束。"""
        if not self.png_sha256:
            raise ValueError("pixel evidence requires png_sha256")
        if min(self.x, self.y, self.width, self.height) < 0:
            raise ValueError("pixel evidence coordinates must be non-negative")
        if self.width == 0 or self.height == 0:
            raise ValueError("pixel evidence region must have positive size")

    def validate_canvas(self, canvas: Mapping[str, object]) -> None:
        """确认像素区域完整落在实际画布内。"""
        if self.x + self.width > canvas["width_px"] or self.y + self.height > canvas["height_px"]:
            raise ValueError("pixel evidence region exceeds actual canvas")

    def to_dict(self) -> dict[str, object]:
        """把像素证据转换为 JSON 对象。"""
        return {
            "png_sha256": self.png_sha256,
            "bbox": [self.x, self.y, self.width, self.height],
            "metric": self.metric,
            "measured": self.measured,
            "threshold": self.threshold,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "PixelEvidence":
        """从分析器对象恢复像素证据。"""
        bbox = value.get("bbox") or []
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError("pixel evidence bbox must contain four integers")
        measured = value.get("measured")
        threshold = value.get("threshold")
        return cls(
            png_sha256=str(value.get("png_sha256") or ""),
            x=int(bbox[0]),
            y=int(bbox[1]),
            width=int(bbox[2]),
            height=int(bbox[3]),
            metric=str(value.get("metric") or ""),
            measured=(float(measured) if measured is not None else None),
            threshold=(float(threshold) if threshold is not None else None),
        )


@dataclass(frozen=True)
class VisualCandidate:
    """表示 Critic 尚待复核的一条视觉问题候选。"""

    candidate_id: str
    kind: str
    severity: str
    page: int | None
    summary: str
    owner_hint: str
    objective: bool
    evidence: tuple[PixelEvidence, ...] = ()

    def __post_init__(self) -> None:
        """规范严重度、路由提示和证据集合。"""
        severity = str(self.severity).strip().lower()
        owner_hint = str(self.owner_hint).strip().lower()
        if severity not in VISUAL_SEVERITIES:
            raise ValueError(f"unsupported visual severity {severity!r}")
        if owner_hint not in VISUAL_OWNER_HINTS:
            raise ValueError(f"unsupported visual owner hint {owner_hint!r}")
        if not str(self.kind).strip() or not str(self.summary).strip():
            raise ValueError("visual candidate kind and summary must not be empty")
        object.__setattr__(self, "severity", severity)
        object.__setattr__(self, "owner_hint", owner_hint)
        object.__setattr__(self, "evidence", tuple(self.evidence))

    def to_dict(self) -> dict[str, object]:
        """把候选转换为 Issue Ledger 可消费对象。"""
        return {
            "candidate_id": self.candidate_id,
            "kind": self.kind,
            "severity": self.severity,
            "page": self.page,
            "summary": self.summary,
            "owner_hint": self.owner_hint,
            "objective": self.objective,
            "evidence": [item.to_dict() for item in self.evidence],
        }

    @classmethod
    def from_dict(
        cls, value: Mapping[str, object], *, canvas: Mapping[str, object] | None = None
    ) -> "VisualCandidate":
        """从分析器输出恢复候选，并可校验画布位置。"""
        raw_evidence = value.get("evidence") or []
        if not isinstance(raw_evidence, list) or not all(
            isinstance(item, dict) for item in raw_evidence
        ):
            raise ValueError("visual candidate evidence must be an array of objects")
        evidence = tuple(PixelEvidence.from_dict(item) for item in raw_evidence)
        if canvas is not None:
            for item in evidence:
                item.validate_canvas(canvas)
        raw_page = value.get("page")
        candidate_id = str(value.get("candidate_id") or "")
        if not candidate_id:
            candidate_id = _candidate_id(value)
        return cls(
            candidate_id=candidate_id,
            kind=str(value.get("kind") or ""),
            severity=str(value.get("severity") or ""),
            page=(int(raw_page) if raw_page is not None else None),
            summary=str(value.get("summary") or ""),
            owner_hint=str(value.get("owner_hint") or "none"),
            objective=bool(value.get("objective")),
            evidence=evidence,
        )


@dataclass(frozen=True)
class VisualReview:
    """保存一条候选的复核裁决与简短依据。"""

    candidate: VisualCandidate
    verdict: ReviewVerdict
    rationale: str

    def to_dict(self) -> dict[str, object]:
        """把复核结果转换为 Issue Ledger 输入。"""
        return {
            "candidate": self.candidate.to_dict(),
            "verdict": self.verdict.value,
            "rationale": self.rationale,
        }


VisualReviewer = Callable[[VisualCandidate], Mapping[str, object] | ReviewVerdict | str]
VisionAnalyzer = Callable[[Path, str, str, Mapping[str, object]], Mapping[str, object]]


def external_visual_candidate(
    *,
    inspection_id: str,
    png_sha256: str,
    page: int | None,
    body: str,
    actionable: bool,
) -> VisualCandidate:
    """把 Critic 的 REVISE 绑定为一条外部视觉候选（不解析自然语言问题字段）。

    整段 body 原样作为 summary；candidate_id 稳定绑定 inspection + 像素 hash，保证同一
    像素同一问题去重。actionable=True → major（有具体可见后果）；否则 advisory（主观润色）。
    """
    candidate_id = f"critic-{inspection_id}-{png_sha256[:12]}"
    return VisualCandidate(
        candidate_id=candidate_id,
        kind="external_visual_critic",
        severity="major" if actionable else "advisory",
        page=page,
        summary=(str(body).strip() or "external visual critic flagged a visible issue"),
        owner_hint="slide",
        objective=bool(actionable),
        evidence=(),
    )


class VisualQC:
    """构造机械候选，并确保 Critic 候选逐项闭合。"""

    @staticmethod
    def mechanical_candidates(
        page: PageSnapshot,
        assets: AssetValidation | None = None,
    ) -> tuple[VisualCandidate, ...]:
        """由 stale PNG 和破图等确定性事实生成候选。"""
        candidates: list[VisualCandidate] = []
        if page.stale_png:
            candidates.append(
                VisualCandidate(
                    candidate_id=_candidate_id(
                        {
                            "kind": "stale_png",
                            "page": page.page,
                            "png_sha256": page.png_sha256,
                            "html_sha256": page.html_sha256,
                        }
                    ),
                    kind="stale_png",
                    severity="major",
                    page=page.page,
                    summary="当前 PNG 与已提交 HTML 不是同一哈希版本。",
                    owner_hint="slide",
                    objective=True,
                )
            )
        if assets is not None:
            for kind, references in (
                ("missing_image_resource", assets.missing),
                ("broken_image_resource", assets.broken),
                ("external_image_unverified", assets.external_unverified),
            ):
                for reference in references:
                    candidates.append(
                        VisualCandidate(
                            candidate_id=_candidate_id(
                                {
                                    "kind": kind,
                                    "page": page.page,
                                    "reference": reference,
                                    "html_sha256": page.html_sha256,
                                }
                            ),
                            kind=kind,
                            severity="major",
                            page=page.page,
                            summary=f"图片资源未形成可验证像素：{reference}",
                            owner_hint="slide",
                            objective=True,
                        )
                    )
        return tuple(candidates)

    @staticmethod
    def review_candidates(
        candidates: Sequence[VisualCandidate],
        reviewer: VisualReviewer,
        *,
        max_reviews: int | None = None,
    ) -> tuple[VisualReview, ...]:
        """逐条复核候选；超过预算的候选明确保持 unresolved。"""
        limit = len(candidates) if max_reviews is None else max(0, int(max_reviews))
        reviews: list[VisualReview] = []
        for index, candidate in enumerate(candidates):
            if index >= limit:
                reviews.append(
                    VisualReview(
                        candidate=candidate,
                        verdict=ReviewVerdict.UNRESOLVED,
                        rationale="review_budget_exhausted",
                    )
                )
                continue
            raw = reviewer(candidate)
            if isinstance(raw, Mapping):
                verdict = ReviewVerdict(str(raw.get("verdict") or ""))
                rationale = str(raw.get("rationale") or "")
            else:
                verdict = raw if isinstance(raw, ReviewVerdict) else ReviewVerdict(str(raw))
                rationale = ""
            reviews.append(
                VisualReview(candidate=candidate, verdict=verdict, rationale=rationale)
            )
        return tuple(reviews)


def _candidate_id(value: Mapping[str, object]) -> str:
    """由候选的机械内容生成稳定 ID。"""
    payload = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, default=str)
    return f"candidate-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:20]}"
