#!/usr/bin/env python3
"""Apply rubric-declared defect penalties to dimension scores.

This module intentionally runs after Judge validation and base aggregation.
The defect label and trigger are criterion-scoped, while the rubric policy
selects whether atomic events adjust the containing criterion or dimension.
The current v004 policy uses ``max_dimension_deduction``, so events accumulate
on the containing dimension. Individual Judge outputs remain unchanged.
"""

from __future__ import annotations

import copy
import math
from collections import defaultdict
from typing import Any, Iterable


class DefectPenaltyError(ValueError):
    """Raised when a validated rubric/result pair cannot be post-processed."""


def _score_key(score: float) -> str:
    if abs(score - 1.0) < 1e-9:
        return "1"
    if abs(score - 0.5) < 1e-9:
        return "0.5"
    if abs(score) < 1e-9:
        return "0"
    raise DefectPenaltyError(f"unsupported defect score {score!r}")


def _atomic_scores(result: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Yield the smallest independently scored points for one criterion."""
    unit_scores = result.get("unit_scores")
    if isinstance(unit_scores, list):
        for unit in unit_scores:
            score = unit.get("score")
            if score is None:
                continue
            yield {
                "score": float(score),
                "unit_type": "unit",
                "unit_id": str(unit.get("unit_id", "")),
                "unit_label": str(unit.get("unit_label", "")),
            }
        return

    page_scores = result.get("page_scores")
    if isinstance(page_scores, list):
        for page in page_scores:
            score = page.get("score")
            if score is None:
                continue
            yield {
                "score": float(score),
                "unit_type": "page",
                "page": int(page["page"]),
                "unit_label": f"第{int(page['page'])}页",
            }
        return

    score = result.get("score")
    if score is not None:
        yield {
            "score": float(score),
            "unit_type": "criterion",
            "unit_label": str(result.get("criterion_id", "")),
        }


def _weighted_role_score(
    group_scores: dict[str, float],
    group_weights: dict[str, float],
    *,
    round_to: int,
) -> float | None:
    if not group_scores:
        return None
    if set(group_scores) != set(group_weights):
        raise DefectPenaltyError(
            "defect post-processing group weights do not match scored groups"
        )
    total = sum(float(group_weights[group]) for group in group_scores)
    if not math.isfinite(total) or total <= 0:
        raise DefectPenaltyError("defect post-processing weights must be positive")
    return round(
        sum(
            float(group_scores[group]) * float(group_weights[group])
            for group in group_scores
        )
        / total,
        round_to,
    )


def apply_group_minimum_decay(
    scores: dict[str, dict[str, Any]],
    rubrics: Iterable[dict[str, Any]],
    *,
    alpha: float = 0.5,
    round_to: int = 6,
) -> dict[str, dict[str, Any]]:
    """Apply one weakest-dimension decay to Final Deck and Page only.

    This is deliberately separate from criterion judging and the v004
    additive defect policy.  It consumes the already-adjusted small-dimension
    scores and applies exactly one multiplier to each large dimension:

        multiplier = 1 - alpha * (1 - minimum_small_dimension_score)

    Intermediate keeps the additive-defect-adjusted arithmetic mean of its
    dimensions and is intentionally not multiplied by a weakest-dimension
    factor. Missing/N/A Final dimensions are absent from ``dimension_scores``
    and therefore do not participate in the minimum.
    """
    if (
        isinstance(alpha, bool)
        or not isinstance(alpha, (int, float))
        or not math.isfinite(float(alpha))
        or not 0 <= float(alpha) <= 1
    ):
        raise DefectPenaltyError("group minimum decay alpha must be in [0, 1]")

    alpha = float(alpha)
    output = copy.deepcopy(scores)
    rubric_by_role = {
        str(rubric.get("rubric_role", "")): rubric for rubric in rubrics
    }
    policy = {
        "mode": "minimum_dimension_multiplicative_decay",
        "alpha": alpha,
        "multiplier_formula": "1 - alpha * (1 - G_min)",
        "combine": "once_per_large_dimension",
        "na_policy": "N/A dimensions are excluded from G_min.",
    }

    final_role = output.get("final_output_case_specific")
    final_rubric = rubric_by_role.get("final_case_specific")
    if isinstance(final_role, dict) and isinstance(final_rubric, dict):
        dimension_scores = {
            str(key): float(value)
            for key, value in final_role.get("dimension_scores", {}).items()
        }
        current_group_scores = {
            str(key): float(value)
            for key, value in final_role.get("group_scores", {}).items()
        }
        stored_base_group_scores = final_role.get(
            "pre_group_decay_group_scores"
        )
        group_scores = (
            {
                str(key): float(value)
                for key, value in stored_base_group_scores.items()
            }
            if isinstance(stored_base_group_scores, dict)
            and set(stored_base_group_scores) == set(current_group_scores)
            else current_group_scores
        )
        grouped_dimensions: dict[str, list[str]] = defaultdict(list)
        for dimension in final_rubric.get("dimensions", []):
            dimension_id = str(dimension.get("id", ""))
            group = str(dimension.get("group", ""))
            if dimension_id in dimension_scores and group in group_scores:
                grouped_dimensions[group].append(dimension_id)

        adjusted_groups: dict[str, float] = {}
        adjustments: dict[str, dict[str, Any]] = {}
        for group, base_score in group_scores.items():
            dimension_ids = grouped_dimensions.get(group, [])
            if not dimension_ids:
                adjusted_groups[group] = round(base_score, round_to)
                continue
            minimum_score = min(dimension_scores[item] for item in dimension_ids)
            minimum_ids = [
                item
                for item in dimension_ids
                if abs(dimension_scores[item] - minimum_score) < 1e-9
            ]
            multiplier = 1.0 - alpha * (1.0 - minimum_score)
            effective_score = round(base_score * multiplier, round_to)
            adjusted_groups[group] = effective_score
            adjustments[group] = {
                "base_score": round(base_score, round_to),
                "minimum_dimension_score": round(minimum_score, round_to),
                "minimum_dimension_ids": minimum_ids,
                "multiplier": round(multiplier, round_to),
                "effective_score": effective_score,
            }

        final_role["score_before_group_decay"] = final_role.get(
            "score_before_group_decay", final_role.get("score")
        )
        final_role["pre_group_decay_group_scores"] = {
            group: round(score, round_to) for group, score in group_scores.items()
        }
        final_role["group_scores"] = adjusted_groups
        final_role["group_decay_policy"] = copy.deepcopy(policy)
        final_role["group_decay_adjustments"] = adjustments
        final_role["score"] = _weighted_role_score(
            adjusted_groups,
            {
                str(group): float(weight)
                for group, weight in final_role.get("group_weights", {}).items()
            },
            round_to=round_to,
        )

    return output


def apply_defect_policies(
    scores: dict[str, dict[str, Any]],
    case_results: list[dict[str, Any]],
    rubrics: Iterable[dict[str, Any]],
    *,
    round_to: int = 6,
) -> dict[str, dict[str, Any]]:
    """Apply v3 defect policies without altering the base scoring pipeline."""
    output = copy.deepcopy(scores)
    role_score_keys = {
        "final_case_specific": "final_output_case_specific",
        "intermediate_case_specific": "intermediate_case_specific",
    }

    for rubric in rubrics:
        policy = rubric.get("score_contract", {}).get("defect_policy")
        if not isinstance(policy, dict):
            continue
        role_key = role_score_keys.get(str(rubric.get("rubric_role", "")))
        if role_key is None or role_key not in output:
            continue

        role_score = output[role_key]
        base_group_scores = {
            str(group): float(score)
            for group, score in role_score.get("group_scores", {}).items()
        }
        dimensions = rubric.get("dimensions")
        uses_dimension_hierarchy = isinstance(dimensions, list) and bool(dimensions)
        dimension_definitions = (
            {str(dimension["id"]): dimension for dimension in dimensions}
            if uses_dimension_hierarchy
            else {}
        )
        base_dimension_scores = {
            str(dimension_id): float(score)
            for dimension_id, score in role_score.get(
                "dimension_scores", {}
            ).items()
        }
        base_target_scores = (
            base_dimension_scores if uses_dimension_hierarchy else base_group_scores
        )
        criteria = {
            str(criterion["id"]): criterion
            for criterion in rubric.get("criteria", [])
        }
        criterion_scoped = (
            "max_criterion_deduction" in policy.get("major", {})
        )
        if uses_dimension_hierarchy and criterion_scoped:
            raw_criterion_scores: dict[str, float] = {}
            criterion_results: dict[str, dict[str, Any]] = {}
            for result in case_results:
                criterion_id = str(result.get("criterion_id", ""))
                if criterion_id not in criteria or result.get("score") is None:
                    continue
                raw_criterion_scores[criterion_id] = float(result["score"])
                criterion_results[criterion_id] = result

            adjusted_criterion_scores: dict[str, float] = {}
            criterion_adjustments: dict[str, dict[str, Any]] = {}
            major_cap = float(
                policy.get("major", {}).get(
                    "max_criterion_deduction",
                    policy.get("major", {}).get("max_dimension_deduction", 0),
                )
            )
            for criterion_id, base_score in raw_criterion_scores.items():
                criterion = criteria[criterion_id]
                defect_level = criterion.get("defect_level")
                major_events: list[dict[str, Any]] = []
                fatal_events: list[dict[str, Any]] = []
                if defect_level in {"major", "fatal"}:
                    for atomic in _atomic_scores(criterion_results[criterion_id]):
                        score = float(atomic["score"])
                        score_key = _score_key(score)
                        base_event = {
                            "criterion_id": criterion_id,
                            "criterion_title": str(criterion.get("title", "")),
                            "dimension_id": str(criterion["dimension_id"]),
                            "defect_level": defect_level,
                            **atomic,
                        }
                        if defect_level == "major":
                            deduction = float(
                                policy["major"]["deductions"][score_key]
                            )
                            if deduction > 0:
                                major_events.append(
                                    {**base_event, "deduction": deduction}
                                )
                        else:
                            multiplier = float(
                                policy["fatal"]["multipliers"][score_key]
                            )
                            if multiplier < 1:
                                fatal_events.append(
                                    {**base_event, "multiplier": multiplier}
                                )

                raw_major_deduction = sum(
                    float(event["deduction"]) for event in major_events
                )
                applied_major_deduction = min(raw_major_deduction, major_cap)
                score_after_major = max(
                    0.0, base_score - applied_major_deduction
                )
                fatal_multiplier = 1.0
                for event in fatal_events:
                    fatal_multiplier *= float(event["multiplier"])
                adjusted_score = round(
                    min(
                        1.0,
                        max(0.0, score_after_major * fatal_multiplier),
                    ),
                    round_to,
                )
                adjusted_criterion_scores[criterion_id] = adjusted_score
                if defect_level in {"major", "fatal"}:
                    criterion_adjustments[criterion_id] = {
                        "criterion_title": str(criterion.get("title", "")),
                        "dimension_id": str(criterion["dimension_id"]),
                        "defect_level": defect_level,
                        "base_score": round(base_score, round_to),
                        "major": {
                            "events": major_events,
                            "raw_deduction": round(
                                raw_major_deduction, round_to
                            ),
                            "max_criterion_deduction": major_cap,
                            "applied_deduction": round(
                                applied_major_deduction, round_to
                            ),
                            "score_after_deduction": round(
                                score_after_major, round_to
                            ),
                        },
                        "fatal": {
                            "events": fatal_events,
                            "combined_multiplier": round(
                                fatal_multiplier, round_to
                            ),
                        },
                        "effective_score": adjusted_score,
                    }

            dimension_criterion_scores: dict[str, list[float]] = defaultdict(list)
            for criterion_id, score in adjusted_criterion_scores.items():
                dimension_criterion_scores[
                    str(criteria[criterion_id]["dimension_id"])
                ].append(score)
            adjusted_dimension_scores = {
                dimension_id: round(sum(values) / len(values), round_to)
                for dimension_id, values in sorted(
                    dimension_criterion_scores.items()
                )
            }

            role_score["score_before_defect_penalty"] = role_score.get("score")
            role_score["raw_criterion_scores"] = {
                criterion_id: round(score, round_to)
                for criterion_id, score in sorted(raw_criterion_scores.items())
            }
            role_score["criterion_scores"] = {
                criterion_id: score
                for criterion_id, score in sorted(
                    adjusted_criterion_scores.items()
                )
            }
            role_score["pre_defect_dimension_scores"] = {
                dimension_id: round(score, round_to)
                for dimension_id, score in base_dimension_scores.items()
            }
            role_score["dimension_scores"] = adjusted_dimension_scores
            role_score["defect_policy"] = copy.deepcopy(policy)
            role_score["defect_adjustments"] = criterion_adjustments

            if (
                role_key == "final_output_case_specific"
                or isinstance(role_score.get("group_dimension_weights"), dict)
            ):
                grouped_dimensions: dict[str, list[str]] = defaultdict(list)
                for dimension_id in adjusted_dimension_scores:
                    grouped_dimensions[
                        str(dimension_definitions[dimension_id]["group"])
                    ].append(dimension_id)
                adjusted_group_scores: dict[str, float] = {}
                for group, dimension_ids in grouped_dimensions.items():
                    weights = {
                        dimension_id: float(
                            dimension_definitions[dimension_id]["weight"]
                        )
                        for dimension_id in dimension_ids
                    }
                    weight_total = sum(weights.values())
                    adjusted_group_scores[group] = round(
                        sum(
                            adjusted_dimension_scores[dimension_id]
                            * weights[dimension_id]
                            for dimension_id in dimension_ids
                        )
                        / weight_total,
                        round_to,
                    )
                role_score["pre_defect_group_scores"] = {
                    group: round(score, round_to)
                    for group, score in base_group_scores.items()
                }
                role_score["group_scores"] = adjusted_group_scores
                role_score["score"] = _weighted_role_score(
                    adjusted_group_scores,
                    {
                        str(group): float(weight)
                        for group, weight in role_score.get(
                            "group_weights", {}
                        ).items()
                    },
                    round_to=round_to,
                )
                if isinstance(role_score.get("deck_dimension_scores"), list):
                    for dimension_score in role_score["deck_dimension_scores"]:
                        dimension_id = str(
                            dimension_score.get("dimension_id", "")
                        )
                        if dimension_id in adjusted_dimension_scores:
                            dimension_score["score"] = adjusted_dimension_scores[
                                dimension_id
                            ]
            else:
                role_score["pre_defect_group_scores"] = {
                    dimension_id: round(score, round_to)
                    for dimension_id, score in base_dimension_scores.items()
                }
                role_score["group_scores"] = adjusted_dimension_scores
                role_score["score"] = _weighted_role_score(
                    adjusted_dimension_scores,
                    {
                        str(dimension_id): float(weight)
                        for dimension_id, weight in role_score.get(
                            "dimension_weights", {}
                        ).items()
                    },
                    round_to=round_to,
                )
            continue

        major_events: dict[str, list[dict[str, Any]]] = defaultdict(list)
        fatal_events: dict[str, list[dict[str, Any]]] = defaultdict(list)

        for result in case_results:
            criterion_id = str(result.get("criterion_id", ""))
            criterion = criteria.get(criterion_id)
            if criterion is None:
                continue
            defect_level = criterion.get("defect_level")
            if defect_level not in {"ordinary", "major", "fatal"}:
                continue
            target = (
                str(criterion.get("dimension_id", ""))
                if uses_dimension_hierarchy
                else str(criterion["group"])
            )
            if target not in base_target_scores:
                continue
            for atomic in _atomic_scores(result):
                score = float(atomic["score"])
                score_key = _score_key(score)
                base_event = {
                    "criterion_id": criterion_id,
                    "criterion_title": str(criterion.get("title", "")),
                    "defect_level": defect_level,
                    **atomic,
                }
                if defect_level == "major":
                    deduction = float(
                        policy["major"]["deductions"][score_key]
                    )
                    if deduction > 0:
                        major_events[target].append(
                            {**base_event, "deduction": deduction}
                        )
                elif defect_level == "fatal":
                    multiplier = float(
                        policy["fatal"]["multipliers"][score_key]
                    )
                    if multiplier < 1:
                        fatal_events[target].append(
                            {**base_event, "multiplier": multiplier}
                        )

        adjusted_target_scores: dict[str, float] = {}
        adjustment_details: dict[str, dict[str, Any]] = {}
        major_cap = float(
            policy.get("major", {}).get("max_dimension_deduction", 0)
        )
        for target, base_score in base_target_scores.items():
            target_major_events = major_events.get(target, [])
            raw_major_deduction = sum(
                float(event["deduction"]) for event in target_major_events
            )
            applied_major_deduction = min(raw_major_deduction, major_cap)
            score_after_major = max(0.0, base_score - applied_major_deduction)

            target_fatal_events = fatal_events.get(target, [])
            fatal_multiplier = 1.0
            for event in target_fatal_events:
                fatal_multiplier *= float(event["multiplier"])
            adjusted_score = round(
                min(1.0, max(0.0, score_after_major * fatal_multiplier)),
                round_to,
            )
            adjusted_target_scores[target] = adjusted_score
            adjustment_details[target] = {
                "base_score": round(base_score, round_to),
                "major": {
                    "events": target_major_events,
                    "raw_deduction": round(raw_major_deduction, round_to),
                    "max_dimension_deduction": major_cap,
                    "applied_deduction": round(
                        applied_major_deduction, round_to
                    ),
                    "score_after_deduction": round(
                        score_after_major, round_to
                    ),
                },
                "fatal": {
                    "events": target_fatal_events,
                    "combined_multiplier": round(fatal_multiplier, round_to),
                },
                "effective_score": adjusted_score,
            }

        role_score["score_before_defect_penalty"] = role_score.get("score")
        role_score["defect_policy"] = copy.deepcopy(policy)
        role_score["defect_adjustments"] = adjustment_details
        if uses_dimension_hierarchy:
            role_score["pre_defect_dimension_scores"] = {
                dimension_id: round(score, round_to)
                for dimension_id, score in base_dimension_scores.items()
            }
            role_score["dimension_scores"] = adjusted_target_scores
            if (
                role_key == "final_output_case_specific"
                or isinstance(role_score.get("group_dimension_weights"), dict)
            ):
                grouped_dimensions: dict[str, list[str]] = defaultdict(list)
                for dimension_id in adjusted_target_scores:
                    grouped_dimensions[
                        str(dimension_definitions[dimension_id]["group"])
                    ].append(dimension_id)
                adjusted_group_scores: dict[str, float] = {}
                for group, dimension_ids in grouped_dimensions.items():
                    weights = {
                        dimension_id: float(
                            dimension_definitions[dimension_id]["weight"]
                        )
                        for dimension_id in dimension_ids
                    }
                    weight_total = sum(weights.values())
                    adjusted_group_scores[group] = round(
                        sum(
                            adjusted_target_scores[dimension_id]
                            * weights[dimension_id]
                            for dimension_id in dimension_ids
                        )
                        / weight_total,
                        round_to,
                    )
                role_score["pre_defect_group_scores"] = {
                    group: round(score, round_to)
                    for group, score in base_group_scores.items()
                }
                role_score["group_scores"] = adjusted_group_scores
                role_score["score"] = _weighted_role_score(
                    adjusted_group_scores,
                    {
                        str(group): float(weight)
                        for group, weight in role_score.get(
                            "group_weights", {}
                        ).items()
                    },
                    round_to=round_to,
                )
                if isinstance(role_score.get("deck_dimension_scores"), list):
                    for dimension_score in role_score["deck_dimension_scores"]:
                        dimension_id = str(dimension_score.get("dimension_id", ""))
                        if dimension_id in adjusted_target_scores:
                            dimension_score["score"] = adjusted_target_scores[
                                dimension_id
                            ]
            else:
                role_score["pre_defect_group_scores"] = {
                    dimension_id: round(score, round_to)
                    for dimension_id, score in base_dimension_scores.items()
                }
                role_score["group_scores"] = adjusted_target_scores
                role_score["score"] = _weighted_role_score(
                    adjusted_target_scores,
                    {
                        str(dimension_id): float(weight)
                        for dimension_id, weight in role_score.get(
                            "dimension_weights", {}
                        ).items()
                    },
                    round_to=round_to,
                )
        else:
            role_score["pre_defect_group_scores"] = {
                group: round(score, round_to)
                for group, score in base_group_scores.items()
            }
            role_score["group_scores"] = adjusted_target_scores
            role_score["score"] = _weighted_role_score(
                adjusted_target_scores,
                {
                    str(group): float(weight)
                    for group, weight in role_score.get("group_weights", {}).items()
                },
                round_to=round_to,
            )

    return output
