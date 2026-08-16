#!/usr/bin/env python3
"""Judge long-horizon PPT rubrics one independent scoring criterion at a time."""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import copy
import hashlib
import json
import math
import os
import re
import shutil
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from judge.deterministic_criterion_evaluators import (
    DeterministicEvaluationError,
    evaluate_case_criterion,
    generic_hybrid_observation,
)
from judge.defect_penalty_scoring import (
    apply_defect_policies,
    apply_group_minimum_decay,
)
from judge.final_output_preaudit import (
    FINAL_PREAUDIT_CONTRACT_VERSION,
    mock_final_preaudit,
    validate_final_preaudit,
)
from judge.dimension_rubric_contracts import (
    RUN_CONTRACT_VERSION,
    SCORE_CONTRACT_VERSION,
    DimensionContractError,
    aggregate_page_case_unit_results,
    aggregate_intermediate_unit_results,
    aggregate_scores,
    build_evidence_packet,
    case_profile,
    display_path,
    expand_evidence_patterns,
    load_json,
    load_yaml,
    nest_final_knowledge_under_deck,
    patterns_for_bundles,
    validate_case_result,
    validate_case_result_with_na,
    validate_final_case_result_from_anchors,
    validate_final_common_result,
    validate_intermediate_case_result,
    validate_page_case_unit_result,
    write_json,
)
from judge.judge_transport_and_artifact_runtime import (
    JUDGE_IMAGE_JPEG_QUALITY,
    JUDGE_IMAGE_MAX_DIMENSION,
    JudgeTransportError,
    RunnerError,
    aggregate_judge_token_usage,
    assess_multi_generation_eligibility,
    call_judge,
    choose_run,
    discover_slide_artifacts,
    parse_json_response,
    response_indicates_output_truncation,
    sanitized_component,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT = ROOT / "prompts/judges/dimension_judge.md"
DEFAULT_INTERMEDIATE_MODEL = "gemini-3.5-flash"
DEFAULT_FINAL_MODEL = "gemini-3.5-flash"
DEFAULT_PREAUDIT_MODEL = "gemini-3.1-pro-preview"
DEFAULT_ANTHROPIC_BASE_URL = "https://tokenhub.sensetime.com"
DEFAULT_OPENAI_BASE_URL = "https://tokenhub.sensetime.com/v1"
CASE_SCHEMA_VERSION = "long_horizon_case_specific_rubric_v2"
CASE_SCHEMA_VERSION_WITH_DEFECT_POLICY = "long_horizon_case_specific_rubric_v3"
SUPPORTED_CASE_SCHEMA_VERSIONS = {
    CASE_SCHEMA_VERSION,
    CASE_SCHEMA_VERSION_WITH_DEFECT_POLICY,
}
CHECKPOINT_CONTRACT_VERSION = "dimension_rubric_checkpoint_v1"
JUDGE_IMAGE_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".webp"}
PREAUDIT_SYSTEM_PROMPT = """You are a strict pre-scoring auditor for a rendered presentation deck.
Do not assign scores, levels, pass/fail decisions, or speculate about later rubric anchors.
Audit every slide in canonical order before any scoring occurs. Record concrete strengths,
defects, and unverifiable points using the required JSON schema. Use Simplified Chinese for
human-readable values and return JSON only. Preserve every JSON type exactly: arrays of
objects must contain native JSON objects, never JSON-encoded strings; arrays of strings must
contain strings only. Do not add, rename, or omit schema fields."""
FINAL_HTML_CONTENT_DIRNAME = "derived_html_content"
FINAL_PAGE_INDEX_TYPES = {"cover", "transition", "content", "ending"}


def final_html_content_output_dir(run_dir: Path) -> Path:
    """Return the producer-independent final HTML evidence directory."""
    return run_dir / FINAL_HTML_CONTENT_DIRNAME


def load_reused_final_knowledge_results(
    source_path: Path,
    final_rubric: dict[str, Any],
    *,
    generation_model: str,
    generation_run: str,
) -> list[dict[str, Any]]:
    """Load an already judged knowledge checklist for the identical deck."""
    require_file(source_path, "reused final knowledge score result")
    source = load_json(source_path)
    if source.get("generation_model") != generation_model:
        raise RunnerError(
            "reused final knowledge generation model does not match: "
            f"expected={generation_model!r}, actual={source.get('generation_model')!r}"
        )
    if source.get("generation_run") != generation_run:
        raise RunnerError(
            "reused final knowledge generation run does not match: "
            f"expected={generation_run!r}, actual={source.get('generation_run')!r}"
        )
    details = source.get("details", {}).get("final_output_case_specific")
    if not isinstance(details, list):
        raise RunnerError(
            "reused final knowledge source has no final case-specific details"
        )
    expected_ids = {
        str(criterion["id"])
        for criterion in final_rubric.get("criteria", [])
        if criterion.get("group") == "final_knowledge"
    }
    dimension_by_criterion = {
        str(criterion["id"]): str(criterion["dimension_id"])
        for criterion in final_rubric.get("criteria", [])
        if criterion.get("group") == "final_knowledge"
        and "dimension_id" in criterion
    }
    reused_by_id = {
        str(item.get("criterion_id")): item
        for item in details
        if isinstance(item, dict) and item.get("group") == "final_knowledge"
    }
    if set(reused_by_id) != expected_ids:
        raise RunnerError(
            "reused final knowledge criteria do not match the current rubric: "
            f"missing={sorted(expected_ids - set(reused_by_id))}, "
            f"extra={sorted(set(reused_by_id) - expected_ids)}"
        )
    allowed_scores = {0.0, 0.5, 1.0}
    for criterion_id, item in reused_by_id.items():
        score = item.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise RunnerError(
                f"reused final knowledge criterion {criterion_id} has invalid score"
            )
        if float(score) not in allowed_scores:
            raise RunnerError(
                f"reused final knowledge criterion {criterion_id} score must be "
                "one of 0, 0.5, or 1"
            )
    return [
        {
            **copy.deepcopy(reused_by_id[criterion_id]),
            **(
                {"dimension_id": dimension_by_criterion[criterion_id]}
                if criterion_id in dimension_by_criterion
                else {}
            ),
            "reused_from_score_result": str(source_path),
            "reused_from_score_contract": source.get("contract_version"),
        }
        for criterion_id in sorted(expected_ids)
    ]


def load_reused_unchanged_criterion_results(
    source_path: Path,
    current_rubrics: tuple[dict[str, Any], ...],
    *,
    case_dir: Path,
    generation_model: str,
    generation_run: str,
    judge_models: dict[str, str],
    criterion_filter: frozenset[str],
    force_rejudge_criterion_ids: frozenset[str] = frozenset(),
    preaudit_enabled: bool = False,
) -> tuple[list[dict[str, Any]], frozenset[str]]:
    """仅复用完整 rubric 定义和评测输入均未发生变化的 criterion 结果。"""
    require_file(source_path, "reused unchanged-criterion score result")
    source = load_json(source_path)
    if source.get("generation_model") != generation_model:
        raise RunnerError(
            "reused criterion generation model does not match: "
            f"expected={generation_model!r}, actual={source.get('generation_model')!r}"
        )
    if source.get("generation_run") != generation_run:
        raise RunnerError(
            "reused criterion generation run does not match: "
            f"expected={generation_run!r}, actual={source.get('generation_run')!r}"
        )
    source_judges = source.get("judge_models", {})
    for role, model in judge_models.items():
        if source_judges.get(role) != model:
            raise RunnerError(
                f"reused criterion {role} Judge does not match: "
                f"expected={model!r}, actual={source_judges.get(role)!r}"
            )
    if source_judges.get("preaudit") is not None:
        raise RunnerError("reused unchanged criteria must come from a no-preaudit result")

    source_revision = str(source.get("rubric_revision", ""))
    source_rubric_dir = case_dir / "rubric_versions" / source_revision
    source_rubrics = {
        role: load_json(source_rubric_dir / filename)
        for role, filename in (
            ("intermediate_case_specific", "intermediate_case_rubric.json"),
            ("final_case_specific", "final_case_rubric.json"),
        )
        if any(rubric.get("rubric_role") == role for rubric in current_rubrics)
    }
    current_criteria: dict[str, dict[str, Any]] = {}
    preaudit_independent_ids: set[str] = set()
    for rubric in current_rubrics:
        role = str(rubric["rubric_role"])
        for criterion in rubric["criteria"]:
            criterion_id = str(criterion["id"])
            current_criteria[criterion_id] = criterion
            if (
                role == "intermediate_case_specific"
                or criterion.get("group") == "final_knowledge"
            ):
                preaudit_independent_ids.add(criterion_id)
    source_criteria = {
        str(criterion["id"]): criterion
        for rubric in source_rubrics.values()
        for criterion in rubric["criteria"]
    }
    reusable_ids = frozenset(
        criterion_id
        for criterion_id, criterion in current_criteria.items()
        if source_criteria.get(criterion_id) == criterion
        and criterion_id not in force_rejudge_criterion_ids
        and (not preaudit_enabled or criterion_id in preaudit_independent_ids)
        and (not criterion_filter or criterion_id in criterion_filter)
    )

    source_details = source.get("details", {})
    source_results = {
        str(item.get("criterion_id")): item
        for role in (
            "intermediate_case_specific",
            "final_output_case_specific",
        )
        for item in source_details.get(role, [])
        if isinstance(item, dict)
    }
    missing = sorted(reusable_ids - set(source_results))
    if missing:
        raise RunnerError(
            "reused unchanged-criterion source is missing results: " + ", ".join(missing)
        )
    return (
        [
            {
                **copy.deepcopy(source_results[criterion_id]),
                "group": current_criteria[criterion_id]["group"],
                **(
                    {"dimension_id": current_criteria[criterion_id]["dimension_id"]}
                    if "dimension_id" in current_criteria[criterion_id]
                    else {}
                ),
                **(
                    {"defect_level": current_criteria[criterion_id]["defect_level"]}
                    if "defect_level" in current_criteria[criterion_id]
                    else {}
                ),
                "mode": current_criteria[criterion_id]["mode"],
                "reused_from_score_result": str(source_path),
                "reused_from_score_contract": source.get("contract_version"),
            }
            for criterion_id in sorted(reusable_ids)
        ],
        reusable_ids,
    )


def validate_final_html_content_markdown(
    value: str,
    *,
    page_numbers: list[int],
) -> str:
    """Apply only the Markdown structure checks required by this consumer."""
    if not value.strip():
        raise RunnerError("final HTML semantic Markdown extraction is empty")
    if not value.startswith(("# PPT 内容提取\n", "# PPT Content Extraction\n")):
        raise RunnerError("final HTML semantic Markdown extraction has an invalid title")
    chinese_markers = re.findall(r"(?m)^## 第 (\d+) 页：", value)
    english_markers = re.findall(r"(?m)^## Page (\d+):", value)
    observed = [int(item) for item in chinese_markers or english_markers]
    if observed != page_numbers:
        raise RunnerError(
            "final HTML semantic Markdown extraction has the wrong canonical page order"
        )
    return value


def split_final_html_content_by_page(
    value: str,
    *,
    page_numbers: list[int],
) -> dict[int, str]:
    """Split the validated semantic extraction into page-local evidence."""
    marker = re.compile(r"(?m)^(?:## 第 (\d+) 页：|## Page (\d+):)")
    matches = list(marker.finditer(value))
    observed = [int(match.group(1) or match.group(2)) for match in matches]
    if observed != page_numbers:
        raise RunnerError(
            "final HTML semantic Markdown extraction has the wrong canonical page order"
        )
    sections: dict[int, str] = {}
    for index, (page, match) in enumerate(zip(page_numbers, matches, strict=True)):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        sections[page] = value[match.start() : end].strip()
    return sections


def validate_final_page_index(
    value: Any,
    *,
    page_numbers: list[int],
) -> dict[str, dict[str, str]]:
    """Validate the minimal page routing artifact without producer internals."""
    if not isinstance(value, dict):
        raise RunnerError("final page index must be a JSON object")
    expected_keys = [str(page_number) for page_number in page_numbers]
    if set(value) != set(expected_keys):
        raise RunnerError(
            "final page index keys must exactly match canonical page numbers"
        )
    normalized: dict[str, dict[str, str]] = {}
    for key in expected_keys:
        item = value.get(key)
        if not isinstance(item, dict) or set(item) != {
            "page_type",
            "content_abstract",
        }:
            raise RunnerError(
                f"final page index page {key} must contain only "
                "page_type and content_abstract"
            )
        page_type = item.get("page_type")
        content_abstract = item.get("content_abstract")
        if page_type not in FINAL_PAGE_INDEX_TYPES:
            raise RunnerError(
                f"final page index page {key} has unsupported page_type "
                f"{page_type!r}"
            )
        if (
            not isinstance(content_abstract, str)
            or not content_abstract.strip()
            or len(content_abstract) > 500
        ):
            raise RunnerError(
                f"final page index page {key} content_abstract must be "
                "non-empty and concise"
            )
        normalized[key] = {
            "page_type": page_type,
            "content_abstract": content_abstract.strip(),
        }
    return normalized


@dataclass(frozen=True)
class JudgeTask:
    task_id: str
    kind: str
    judge_role: str
    user_prompt: str
    attachment: Path | list[Path] | None
    validate: Callable[[dict[str, Any]], dict[str, Any]]
    mock_response: dict[str, Any]
    criterion_id: str | None = None
    unit_id: str | None = None
    unit_label: str | None = None
    unit_instruction: str | None = None
    page_number: int | None = None
    response_json_schema: dict[str, Any] | None = None


@dataclass(frozen=True)
class JudgeEndpoint:
    provider: str
    model: str
    base_url: str
    api_key: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def resolve(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()


def require_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise RunnerError(f"{label} does not exist or is not a file: {path}")


def prepare_output(path: Path, overwrite: bool, resume: bool) -> None:
    if path.exists() and any(path.iterdir()):
        if overwrite:
            shutil.rmtree(path)
        elif not resume:
            raise RunnerError(f"output directory is not empty; use --overwrite: {path}")
    for name in (
        "requests",
        "raw_responses",
        "judge_results",
        "deterministic",
        "preaudits",
    ):
        (path / name).mkdir(parents=True, exist_ok=True)


def attachment_manifest(attachment: Path | list[Path] | None) -> str | list[str] | None:
    if attachment is None:
        return None
    if isinstance(attachment, Path):
        return str(attachment)
    return [str(path) for path in attachment]


def checkpoint_value_for_validation(
    task: JudgeTask, value: dict[str, Any]
) -> dict[str, Any]:
    """还原 Judge 原始结构，供当前 task 的 validator 重新校验。"""
    if "anchor_analysis" not in value:
        return value
    return {
        "criterion_id": value.get("criterion_id"),
        "evidence": value.get("evidence", []),
        "reason": value.get("reason"),
        "missing_or_unverifiable": value.get("missing_or_unverifiable", []),
        "anchor_analysis": value.get("anchor_analysis"),
        **(
            {"applicable": value.get("applicable")}
            if "applicable" in value
            else {}
        ),
        **(
            {"full_scope_check": value.get("full_scope_check")}
            if task.judge_role == "intermediate"
            else {}
        ),
        **(
            {
                "page": value.get("page"),
                "page_type": value.get("page_type"),
            }
            if task.page_number is not None
            else {}
        ),
        **(
            {"pages_reviewed": value.get("pages_reviewed")}
            if "pages_reviewed" in value
            else {}
        ),
        **(
            {
                "hard_boundary_assessments": [
                    {
                        key: item[key]
                        for key in ("id", "triggered", "evidence", "reason")
                    }
                    for item in value.get("hard_boundary_assessments", [])
                    if isinstance(item, dict)
                ]
            }
            if "hard_boundary_assessments" in value
            else {}
        ),
    }


def load_reused_unchanged_task_results(
    source_score_path: Path,
    tasks: list[JudgeTask],
    endpoints: dict[str, JudgeEndpoint],
    *,
    system_prompt: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """复用与 no-preaudit 请求逐字段相同的 Judge unit。"""
    source_dir = source_score_path.parent
    checkpoint_path = source_dir / ".checkpoint.json"
    require_file(checkpoint_path, "reused unchanged-task checkpoint")
    checkpoint = load_json(checkpoint_path)
    if checkpoint.get("contract_version") != CHECKPOINT_CONTRACT_VERSION:
        raise RunnerError("reused unchanged-task checkpoint contract is unsupported")
    completed = checkpoint.get("completed")
    if not isinstance(completed, dict):
        raise RunnerError("reused unchanged-task checkpoint completed field is invalid")

    reused_results: dict[str, dict[str, Any]] = {}
    reused_metadata: dict[str, dict[str, Any]] = {}
    for task in tasks:
        cached = completed.get(task.task_id)
        request_path = source_dir / "requests" / f"{task.task_id}.json"
        if not isinstance(cached, dict) or not request_path.is_file():
            continue
        endpoint = endpoints[task.judge_role]
        source_request = load_json(request_path)
        expected_request = {
            "task_id": task.task_id,
            "kind": task.kind,
            "judge_role": task.judge_role,
            "model": endpoint.model,
            "provider": endpoint.provider,
            "attachment": attachment_manifest(task.attachment),
            "system_prompt": system_prompt,
            "user_prompt": task.user_prompt,
        }
        if any(
            source_request.get(key) != expected
            for key, expected in expected_request.items()
        ):
            continue
        value = cached.get("result")
        if not isinstance(value, dict):
            continue
        reused_results[task.task_id] = task.validate(
            checkpoint_value_for_validation(task, value)
        )
        task_metadata = cached.get("metadata")
        reused_metadata[task.task_id] = {
            **(task_metadata if isinstance(task_metadata, dict) else {}),
            "reused_from_score_result": str(source_score_path),
            "reused_unchanged_request": True,
        }
    return reused_results, reused_metadata


def intermediate_visual_attachments(files: list[Path]) -> list[Path]:
    """Return criterion-scoped raster evidence that the Judge can inspect visually."""
    return [
        path
        for path in files
        if path.suffix.lower() in JUDGE_IMAGE_SUFFIXES
    ]


def visual_attachment_prompt_manifest(
    files: list[Path],
    case_dir: Path,
    run_dir: Path,
) -> list[dict[str, Any]]:
    """Map transport attachment indices back to rubric evidence paths."""
    width = max(2, len(str(len(files))))
    return [
        {
            "attachment_index": index,
            "transport_label": f"SLIDE_{index:0{width}d}_OF_{len(files):0{width}d}",
            "path": display_path(path, case_dir, run_dir),
        }
        for index, path in enumerate(files, start=1)
    ]


def enforce_final_llm_evidence_isolation(
    files: list[Path],
    case_dir: Path,
    run_dir: Path,
) -> None:
    instruction = (case_dir / "instruction.md").resolve()
    renders_dir = (run_dir / "renders").resolve()
    semantic_content = (
        final_html_content_output_dir(run_dir) / "content.md"
    ).resolve()
    frozen_reference_root = (case_dir / "rubric_versions").resolve()
    forbidden: list[str] = []
    for path in files:
        resolved = path.resolve()
        is_instruction = resolved == instruction
        is_semantic_content = resolved == semantic_content
        is_frozen_reference = (
            resolved.name == "frozen_reference_facts.json"
            and frozen_reference_root in resolved.parents
        )
        is_render = (
            resolved.parent == renders_dir
            and resolved.suffix.lower() == ".png"
            and resolved.name.startswith("slide_")
        )
        if (
            not is_instruction
            and not is_render
            and not is_semantic_content
            and not is_frozen_reference
        ):
            forbidden.append(display_path(resolved, case_dir, run_dir))
    if forbidden:
        raise RunnerError(
            "final-output LLM evidence isolation permits only instruction.md, "
            "canonical rendered PNGs, the validated final HTML semantic extraction, "
            "and rubric-versioned frozen_reference_facts.json; "
            f"forbidden evidence: {forbidden}"
        )


def criterion_preaudit_selection(criterion: dict[str, Any]) -> str | list[str]:
    """Return the explicit or legacy-inferred final pre-audit routing."""
    mode = str(criterion.get("mode", ""))
    return criterion.get(
        "preaudit",
        (
            "none"
            if mode == "deterministic"
            else (
                "visual"
                if criterion.get("group") == "final_aesthetic"
                else "content"
            )
        ),
    )


def normalize_preaudit_kinds(
    value: Any,
    *,
    label: str,
) -> tuple[str, ...]:
    """Normalize one pre-audit or the canonical content+visual pair."""
    if isinstance(value, str):
        if value not in {"none", "content", "visual"}:
            raise DimensionContractError(
                f"{label} must be none, content, visual, or "
                '["content", "visual"]'
            )
        return (value,)
    if isinstance(value, list):
        if (
            len(value) != 2
            or any(not isinstance(item, str) for item in value)
            or set(value) != {"content", "visual"}
        ):
            raise DimensionContractError(
                f"{label} array must contain content and visual exactly once"
            )
        return ("content", "visual")
    raise DimensionContractError(
        f'{label} must be a string or the array ["content", "visual"]'
    )


def generation_status(run_dir: Path) -> dict[str, Any]:
    metadata_path = run_dir / "generation_metadata.json"
    recovery_path = run_dir / "recovery.json"
    if metadata_path.is_file():
        metadata = load_json(metadata_path)
        success = metadata.get("success")
        passed = bool(
            isinstance(success, dict)
            and success.get("artifact_contract_satisfied") is True
        ) or metadata.get("generation_contract_satisfied") is True
        return {
            "source": str(metadata_path),
            "benchmark_pass": passed,
            "raw_status": metadata.get("status"),
        }
    if recovery_path.is_file():
        recovery = load_json(recovery_path)
        return {
            "source": str(recovery_path),
            "benchmark_pass": recovery.get("benchmark_pass") is True,
            "raw_status": recovery.get("raw_status"),
            "visual_artifact_available": recovery.get("visual_artifact_available"),
        }
    return {"source": None, "benchmark_pass": False, "raw_status": "missing_metadata"}


def _validate_adjustments(scoring: dict[str, Any], criterion_id: str) -> None:
    adjustments = scoring.get("adjustments")
    if adjustments is None:
        return
    if not isinstance(adjustments, list):
        raise DimensionContractError(
            f"criterion {criterion_id} scoring.adjustments must be an array"
        )
    seen: set[str] = set()
    expected = {
        "id",
        "condition",
        "delta",
        "max_applications",
        "evidence_required",
    }
    for index, adjustment in enumerate(adjustments):
        location = f"criterion {criterion_id} scoring.adjustments[{index}]"
        if not isinstance(adjustment, dict) or set(adjustment) != expected:
            raise DimensionContractError(
                f"{location} must contain exactly {sorted(expected)}"
            )
        adjustment_id = adjustment["id"]
        if not isinstance(adjustment_id, str) or not adjustment_id.strip():
            raise DimensionContractError(f"{location}.id must be non-empty")
        if adjustment_id in seen:
            raise DimensionContractError(
                f"criterion {criterion_id} repeats adjustment id {adjustment_id}"
            )
        seen.add(adjustment_id)
        if not isinstance(adjustment["condition"], str) or not adjustment[
            "condition"
        ].strip():
            raise DimensionContractError(f"{location}.condition must be non-empty")
        delta = adjustment["delta"]
        if (
            isinstance(delta, bool)
            or not isinstance(delta, (int, float))
            or float(delta) == 0
            or not -1 <= float(delta) <= 1
        ):
            raise DimensionContractError(
                f"{location}.delta must be a non-zero number in [-1, 1]"
            )
        maximum = adjustment["max_applications"]
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1:
            raise DimensionContractError(
                f"{location}.max_applications must be a positive integer"
            )
        if not isinstance(adjustment["evidence_required"], bool):
            raise DimensionContractError(
                f"{location}.evidence_required must be boolean"
            )


def validate_defect_policy(policy: Any, *, location: str) -> tuple[str, ...]:
    if not isinstance(policy, dict):
        raise DimensionContractError(f"{location} must be an object")
    levels = policy.get("levels")
    allowed_levels = {
        ("ordinary", "major"),
        ("ordinary", "major", "fatal"),
    }
    if not isinstance(levels, list) or tuple(levels) not in allowed_levels:
        raise DimensionContractError(
            f"{location}.levels must be ordinary/major or "
            "ordinary/major/fatal in that order"
        )
    level_tuple = tuple(str(level) for level in levels)
    expected_fields = {
        "levels",
        "ordinary",
        "major",
        "application_order",
        "na_policy",
    }
    if "fatal" in level_tuple:
        expected_fields.add("fatal")
    if set(policy) != expected_fields:
        raise DimensionContractError(
            f"{location} fields must be exactly {sorted(expected_fields)}"
        )
    if policy.get("ordinary") != {"mode": "raw_mean_only"}:
        raise DimensionContractError(
            f"{location}.ordinary must use raw_mean_only"
        )
    expected_major_common = {
        "mode": "additive_deduction",
        "deductions": {"1": 0, "0.5": 0.1, "0": 0.2},
    }
    major = policy.get("major")
    allowed_major = {
        **expected_major_common,
        "max_criterion_deduction": 0.5,
    }
    legacy_major = {
        **expected_major_common,
        "max_dimension_deduction": 0.5,
    }
    if major != allowed_major and major != legacy_major:
        raise DimensionContractError(
            f"{location}.major must use 1→0, 0.5→0.1, 0→0.2 "
            "with a 0.5 criterion cap"
        )
    criterion_scoped = major == allowed_major
    expected_order = [
        "raw_criterion_mean" if criterion_scoped else "raw_dimension_mean",
        "major_additive_deduction",
        *( ["fatal_multiplicative_decay"] if "fatal" in level_tuple else [] ),
        "clamp_0_1",
    ]
    if policy.get("application_order") != expected_order:
        raise DimensionContractError(
            f"{location}.application_order must be {expected_order}"
        )
    if not isinstance(policy.get("na_policy"), str) or not policy["na_policy"].strip():
        raise DimensionContractError(f"{location}.na_policy must be non-empty")
    if "fatal" in level_tuple:
        expected_fatal = {
            "mode": "multiplicative_decay",
            "multipliers": {"1": 1, "0.5": 0.65, "0": 0.3},
            "combine": "product",
        }
        if policy.get("fatal") != expected_fatal:
            raise DimensionContractError(
                f"{location}.fatal must use 1→1, 0.5→0.65, 0→0.3 "
                "with product combination"
            )
    return level_tuple


def validate_case_rubric(
    rubric: dict[str, Any], expected_role: str
) -> None:
    shared_top_fields = {
        "schema_version",
        "rubric_role",
        "rubric_id",
        "case_id",
        "language",
        "title",
        "dimensions",
        "score_contract",
        "evidence_bundles",
        "query_requirement_map",
        "criteria",
    }
    legacy_top_fields = {
        "common_rubric",
        "purpose",
        "fact_checking_policy",
        "path_variables",
        "evaluator_protocols",
    }
    expected_top_fields = shared_top_fields
    accepted_top_field_sets = {
        frozenset(shared_top_fields),
        frozenset(shared_top_fields - {"dimensions"}),
        # Keep existing case files readable while current rubrics use the lean,
        # self-contained dimension contract.
        frozenset((shared_top_fields - {"dimensions"}) | legacy_top_fields),
    }
    if frozenset(rubric) not in accepted_top_field_sets:
        raise DimensionContractError(
            "case rubric top-level fields mismatch: "
            f"missing={sorted(expected_top_fields - set(rubric))}, "
            f"extra={sorted(set(rubric) - expected_top_fields)}"
        )
    schema_version = rubric.get("schema_version")
    if schema_version not in SUPPORTED_CASE_SCHEMA_VERSIONS:
        raise DimensionContractError(
            "case rubric schema_version must be one of "
            f"{sorted(SUPPORTED_CASE_SCHEMA_VERSIONS)}"
        )
    uses_defect_policy = schema_version == CASE_SCHEMA_VERSION_WITH_DEFECT_POLICY
    if rubric.get("rubric_role") != expected_role:
        raise DimensionContractError(
            f"case rubric role must be {expected_role}, got {rubric.get('rubric_role')!r}"
        )
    for field in ("rubric_id", "case_id", "language", "title"):
        value = rubric.get(field)
        if not isinstance(value, str) or not value.strip():
            raise DimensionContractError(
                f"case rubric {field} must be a non-empty string"
            )
    group_prefix = (
        "intermediate_"
        if expected_role.startswith("intermediate")
        else "final_"
    )
    score_contract = rubric.get("score_contract")
    expected_score_contract_fields = {
        "range",
        "round_to",
        "independence",
        "profile_rule",
        "profiles",
        "required_judge_output",
        "aggregation",
    }
    if uses_defect_policy:
        expected_score_contract_fields.add("defect_policy")
    if (
        not isinstance(score_contract, dict)
        or set(score_contract) != expected_score_contract_fields
    ):
        raise DimensionContractError(
            "case rubric score_contract fields mismatch"
        )
    if score_contract.get("range") != [0, 1]:
        raise DimensionContractError("case rubric score_contract.range must be [0, 1]")
    round_to = score_contract.get("round_to")
    if isinstance(round_to, bool) or not isinstance(round_to, int) or round_to < 0:
        raise DimensionContractError(
            "case rubric score_contract.round_to must be a non-negative integer"
        )
    defect_levels: tuple[str, ...] = ()
    if uses_defect_policy:
        defect_levels = validate_defect_policy(
            score_contract.get("defect_policy"),
            location="case rubric score_contract.defect_policy",
        )
    for field in ("independence", "profile_rule"):
        value = score_contract.get(field)
        if not isinstance(value, str) or not value.strip():
            raise DimensionContractError(
                f"case rubric score_contract.{field} must be non-empty"
            )
    required_output = score_contract.get("required_judge_output")
    required_output_fields = {
        "score",
        "evidence",
        "reason",
        "missing_or_unverifiable",
    }
    if (
        not isinstance(required_output, dict)
        or not required_output_fields <= set(required_output)
        or not all(
            isinstance(required_output.get(field), str)
            and required_output[field].strip()
            for field in required_output_fields
        )
    ):
        raise DimensionContractError(
            "case rubric score_contract.required_judge_output is incomplete"
        )
    dimensions = rubric.get("dimensions")
    uses_dimension_hierarchy = dimensions is not None
    if uses_dimension_hierarchy:
        if not isinstance(dimensions, list) or not dimensions:
            raise DimensionContractError(
                "case rubric dimensions must be a non-empty array"
            )
        dimension_ids: set[str] = set()
        for dimension in dimensions:
            if not isinstance(dimension, dict) or set(dimension) != {
                "id",
                "title",
                "group",
                "weight",
            }:
                raise DimensionContractError(
                    "case rubric dimension fields must be exactly "
                    "id/title/group/weight"
                )
            dimension_id = dimension.get("id")
            title = dimension.get("title")
            group = dimension.get("group")
            weight = dimension.get("weight")
            if (
                not isinstance(dimension_id, str)
                or not dimension_id.strip()
                or dimension_id in dimension_ids
            ):
                raise DimensionContractError(
                    f"case rubric dimension id is empty or repeated: {dimension_id!r}"
                )
            if not isinstance(title, str) or not title.strip():
                raise DimensionContractError(
                    f"case rubric dimension {dimension_id} title must be non-empty"
                )
            if not isinstance(group, str) or not group.startswith(group_prefix):
                raise DimensionContractError(
                    f"case rubric dimension {dimension_id} group must start with "
                    f"{group_prefix!r}"
                )
            if (
                isinstance(weight, bool)
                or not isinstance(weight, (int, float))
                or not math.isfinite(float(weight))
                or float(weight) <= 0
            ):
                raise DimensionContractError(
                    f"case rubric dimension {dimension_id} weight must be a "
                    "positive finite number"
                )
            dimension_ids.add(dimension_id)
    else:
        dimension_ids = set()

    aggregation = score_contract.get("aggregation")
    base_aggregation_fields = {
        "criterion_to_group",
        "group_to_role",
        "criterion_weighting",
        "combined_total",
    }
    aggregation_fields = (
        frozenset(aggregation)
        if isinstance(aggregation, dict)
        else frozenset()
    )
    legacy_aggregation_fields = {
        frozenset(base_aggregation_fields),
        frozenset(base_aggregation_fields | {"group_weights"}),
        frozenset(
            base_aggregation_fields
            | {"group_weights", "knowledge_as_deck_dimension"}
        ),
    }
    if uses_dimension_hierarchy:
        expected_dimension_aggregation = (
            {
                "criterion_to_dimension": "arithmetic_mean",
                "criterion_weighting": "equal",
                "dimension_to_group": "weighted_mean",
                "group_to_role": "weighted_mean",
                "group_weights": {
                    "final_deck_level": 0.5,
                    "final_page_level": 0.5,
                },
                "combined_total": None,
            }
            if expected_role == "final_case_specific"
            else (
                {
                    "criterion_to_dimension": "arithmetic_mean",
                    "criterion_weighting": "equal",
                    "dimension_to_role": "weighted_mean",
                    "combined_total": None,
                },
                {
                    "criterion_to_dimension": "arithmetic_mean",
                    "criterion_weighting": "equal",
                    "dimension_to_group": "weighted_mean",
                    "group_to_role": "weighted_mean",
                    "group_weights": {
                        "intermediate_research": 0.25,
                        "intermediate_plan": 0.25,
                        "intermediate_image": 0.25,
                        "intermediate_slide": 0.25,
                    },
                    "combined_total": None,
                },
            )
        )
        aggregation_matches = (
            aggregation == expected_dimension_aggregation
            if isinstance(expected_dimension_aggregation, dict)
            else aggregation in expected_dimension_aggregation
        )
        if not aggregation_matches:
            raise DimensionContractError(
                "case rubric score_contract.aggregation does not match the "
                "dimension-first aggregation contract"
            )
    elif (
        not isinstance(aggregation, dict)
        or aggregation_fields not in legacy_aggregation_fields
        or aggregation.get("criterion_to_group") != "arithmetic_mean"
        or aggregation.get("criterion_weighting") != "equal"
        or aggregation.get("group_to_role")
        not in {"arithmetic_mean", "weighted_mean"}
        or aggregation.get("combined_total") is not None
        or (
            aggregation.get("group_to_role") == "weighted_mean"
            and "group_weights" not in aggregation
        )
        or (
            aggregation.get("group_to_role") == "arithmetic_mean"
            and "group_weights" in aggregation
        )
        or (
            "knowledge_as_deck_dimension" in aggregation
            and (
                expected_role != "final_case_specific"
                or aggregation.get("knowledge_as_deck_dimension") is not True
            )
        )
    ):
        raise DimensionContractError(
            "legacy case rubric aggregation must use arithmetic criterion means, "
            "arithmetic or declared weighted group means, equal criterion "
            "weighting, and no combined total"
        )
    group_weights = aggregation.get("group_weights")
    if group_weights is not None and (
        not isinstance(group_weights, dict)
        or not group_weights
        or any(
            not isinstance(group, str)
            or not group.startswith(group_prefix)
            or isinstance(weight, bool)
            or not isinstance(weight, (int, float))
            or not math.isfinite(float(weight))
            or float(weight) <= 0
            for group, weight in group_weights.items()
        )
    ):
        raise DimensionContractError(
            "case rubric aggregation.group_weights must map same-role groups "
            "to positive finite numbers"
        )
    profiles = score_contract.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise DimensionContractError(
            "case rubric score_contract.profiles must be a non-empty object"
        )
    if expected_role == "intermediate_case_specific" and "common_rubric" not in rubric:
        allowed_profile_definitions = {
            "binary": [0, 1],
            "ternary_3": [0, 0.5, 1],
        }
        invalid_profiles = {
            profile_id: profile.get("allowed_scores")
            for profile_id, profile in profiles.items()
            if (
                profile_id not in allowed_profile_definitions
                or not isinstance(profile, dict)
                or profile.get("allowed_scores")
                != allowed_profile_definitions.get(profile_id)
            )
        }
        if invalid_profiles:
            raise DimensionContractError(
                "lean intermediate rubric profiles must be a non-empty subset "
                "of binary=[0,1] and ternary_3=[0,0.5,1]"
            )
    criteria = rubric.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        raise DimensionContractError("case rubric criteria must be a non-empty array")
    profile_ids = set(rubric.get("score_contract", {}).get("profiles", {}))
    bundle_ids = set(rubric.get("evidence_bundles", {}))
    for bundle_id, bundle in rubric.get("evidence_bundles", {}).items():
        if not isinstance(bundle_id, str) or not bundle_id.strip():
            raise DimensionContractError("evidence bundle ID must be non-empty")
        if not isinstance(bundle, dict):
            raise DimensionContractError(
                f"evidence bundle {bundle_id} must be an object"
            )
        unknown_bundle_fields = set(bundle) - {"files", "use"}
        if unknown_bundle_fields:
            raise DimensionContractError(
                f"evidence bundle {bundle_id} has unknown fields: "
                f"{sorted(unknown_bundle_fields)}"
            )
        patterns = bundle.get("files")
        if (
            not isinstance(patterns, list)
            or not all(
                isinstance(pattern, str) and pattern.strip()
                for pattern in patterns
            )
        ):
            raise DimensionContractError(
                f"evidence bundle {bundle_id}.files must be an array "
                "of non-empty path patterns"
            )
        if not bundle.get("files"):
            raise DimensionContractError(
                f"evidence bundle {bundle_id}.files must not be empty"
            )
        if not isinstance(bundle.get("use"), str) or not bundle["use"].strip():
            raise DimensionContractError(
                f"evidence bundle {bundle_id}.use must be non-empty"
            )
        if (
            expected_role == "final_case_specific"
            and bundle_id == "final_html_content"
            and patterns
            != ["{RUN_DIR}/derived_html_content/content.md"]
        ):
            raise DimensionContractError(
                "final_html_content evidence bundle must point exactly to "
                "{RUN_DIR}/derived_html_content/content.md"
            )
        if (
            expected_role == "final_case_specific"
            and bundle_id == "frozen_reference_facts"
            and (
                len(patterns) != 1
                or re.fullmatch(
                    r"\{CASE_DIR\}/rubric_versions/v\d+/frozen_reference_facts\.json",
                    patterns[0],
                )
                is None
            )
        ):
            raise DimensionContractError(
                "frozen_reference_facts evidence bundle must point exactly to "
                "{CASE_DIR}/rubric_versions/vNNN/frozen_reference_facts.json"
            )
    requirement_map = rubric.get("query_requirement_map", {})
    requirement_ids = set(requirement_map)
    if not requirement_ids:
        raise DimensionContractError(
            "case rubric query_requirement_map must be a non-empty object"
        )
    for requirement_id, requirement in requirement_map.items():
        if not isinstance(requirement_id, str) or not requirement_id.strip():
            raise DimensionContractError(
                "case rubric query requirement ID must be non-empty"
            )
        if not isinstance(requirement, dict):
            raise DimensionContractError(
                f"query requirement {requirement_id} must be an object"
            )
        allowed_requirement_fields = {"requirement"}
        if expected_role == "final_case_specific" or "common_rubric" in rubric:
            # Existing v2 files may still carry the redundant reverse criteria
            # list. New lean intermediate files have one source of truth only.
            allowed_requirement_fields.add("criteria")
        if set(requirement) - allowed_requirement_fields:
            raise DimensionContractError(
                f"query requirement {requirement_id} has unknown fields: "
                f"{sorted(set(requirement) - allowed_requirement_fields)}"
            )
        text = requirement.get("requirement")
        if not isinstance(text, str) or not text.strip():
            raise DimensionContractError(
                f"query requirement {requirement_id}.requirement must be non-empty"
            )
    seen: set[str] = set()
    referenced_requirement_ids: set[str] = set()
    for criterion in criteria:
        if not isinstance(criterion, dict):
            raise DimensionContractError("case rubric criterion must be an object")
        criterion_id = str(criterion.get("id", ""))
        if not criterion_id or criterion_id in seen:
            raise DimensionContractError(
                f"case rubric criterion id is empty or repeated: {criterion_id!r}"
            )
        seen.add(criterion_id)
        mode = criterion.get("mode")
        mode_fields = {
            "llm_judge": {"judge_instruction"},
            "deterministic": {"calculation"},
            "hybrid": {"deterministic_observation", "judge_instruction"},
        }
        if mode not in mode_fields:
            raise DimensionContractError(
                f"criterion {criterion_id} has unsupported mode {mode!r}"
            )
        shared_criterion_fields = {
            "id",
            "group",
            "title",
            "objective",
            "mode",
            "input_bundles",
            "query_requirements",
            "scoring",
            *mode_fields[mode],
        }
        legacy_criterion_fields = {"common_dimension", "why"}
        expected_criterion_fields = (
            shared_criterion_fields | {"dimension_id"}
            if uses_dimension_hierarchy
            else (
                shared_criterion_fields
                if expected_role == "intermediate_case_specific"
                else shared_criterion_fields
                | legacy_criterion_fields
                | {"preaudit"}
            )
        )
        if uses_dimension_hierarchy and expected_role == "final_case_specific":
            expected_criterion_fields.add("preaudit")
        if (
            uses_dimension_hierarchy
            and expected_role == "intermediate_case_specific"
            and mode == "llm_judge"
            and "unit_evaluation" in criterion
        ):
            expected_criterion_fields.add("unit_evaluation")
        accepted_criterion_field_sets = {frozenset(expected_criterion_fields)}
        if expected_role == "intermediate_case_specific":
            accepted_criterion_field_sets.add(
                frozenset(shared_criterion_fields | legacy_criterion_fields)
            )
            if mode == "llm_judge":
                accepted_criterion_field_sets.add(
                    frozenset(shared_criterion_fields | {"unit_evaluation"})
                )
                accepted_criterion_field_sets.add(
                    frozenset(
                        shared_criterion_fields
                        | legacy_criterion_fields
                        | {"unit_evaluation"}
                    )
                )
                if "unit_evaluation" in criterion:
                    # Intermediate N/A is an explicit rubric opt-in. By
                    # default, a missing required process remains applicable
                    # and must be scored instead of leaving the denominator.
                    accepted_criterion_field_sets |= {
                        frozenset(fields | {"supports_na"})
                        for fields in tuple(accepted_criterion_field_sets)
                        if "unit_evaluation" in fields
                    }
        elif expected_role == "final_case_specific":
            # Older split-v2 final rubrics predate explicit pre-audit routing.
            # They remain readable; runtime infers content vs visual by group.
            accepted_criterion_field_sets.add(
                frozenset(shared_criterion_fields | legacy_criterion_fields)
            )
            # A page-level case criterion may explicitly allow per-page N/A.
            # This is criterion-scoped so all other page criteria remain strict.
            accepted_criterion_field_sets.add(
                frozenset(expected_criterion_fields | {"supports_na"})
            )
        if uses_defect_policy:
            accepted_criterion_field_sets |= {
                frozenset(fields | {"defect_level"})
                for fields in tuple(accepted_criterion_field_sets)
            }
        if frozenset(criterion) not in accepted_criterion_field_sets:
            raise DimensionContractError(
                f"criterion {criterion_id} fields mismatch: "
                f"missing={sorted(expected_criterion_fields - set(criterion))}, "
                f"extra={sorted(set(criterion) - expected_criterion_fields)}"
            )
        if uses_dimension_hierarchy:
            dimension_id = criterion.get("dimension_id")
            if dimension_id not in dimension_ids:
                raise DimensionContractError(
                    f"criterion {criterion_id} references unknown dimension "
                    f"{dimension_id!r}"
                )
        if uses_defect_policy:
            is_knowledge = (
                expected_role == "final_case_specific"
                and criterion.get("group") == "final_knowledge"
            )
            if is_knowledge and "defect_level" in criterion:
                raise DimensionContractError(
                    f"criterion {criterion_id} final knowledge checklist must "
                    "not declare defect_level"
                )
            if not is_knowledge and criterion.get("defect_level") not in defect_levels:
                raise DimensionContractError(
                    f"criterion {criterion_id} defect_level must be one of "
                    f"{list(defect_levels)}"
                )
        if (
            expected_role == "intermediate_case_specific"
            and mode == "llm_judge"
            and "unit_evaluation" in criterion
        ):
            unit_evaluation = criterion.get("unit_evaluation")
            if not isinstance(unit_evaluation, dict):
                raise DimensionContractError(
                    f"criterion {criterion_id} unit_evaluation must be an object"
                )
            source = unit_evaluation.get("source")
            allowed_sources = {
                "fixed",
                "slide_pages",
                "plan_pages_plus_deck",
                "image_assets",
            }
            expected_unit_fields = {"source", "aggregation", "unit_instruction"}
            if source == "fixed":
                expected_unit_fields.add("units")
            elif source == "image_assets" and "required_units" in unit_evaluation:
                expected_unit_fields.add("required_units")
            if set(unit_evaluation) != expected_unit_fields:
                raise DimensionContractError(
                    f"criterion {criterion_id} unit_evaluation fields must be "
                    f"exactly {sorted(expected_unit_fields)}"
                )
            if source not in allowed_sources:
                raise DimensionContractError(
                    f"criterion {criterion_id} has unsupported unit source {source!r}"
                )
            if unit_evaluation.get("aggregation") != "arithmetic_mean":
                raise DimensionContractError(
                    f"criterion {criterion_id} unit aggregation must be arithmetic_mean"
                )
            if not isinstance(unit_evaluation.get("unit_instruction"), str) or not (
                unit_evaluation["unit_instruction"].strip()
            ):
                raise DimensionContractError(
                    f"criterion {criterion_id} unit_instruction must be non-empty"
                )
            if source == "fixed":
                units = unit_evaluation.get("units")
                if not isinstance(units, list) or not units:
                    raise DimensionContractError(
                        f"criterion {criterion_id} fixed units must be non-empty"
                    )
                unit_ids: set[str] = set()
                for unit in units:
                    if not isinstance(unit, dict) or set(unit) != {
                        "id",
                        "label",
                        "instruction",
                    }:
                        raise DimensionContractError(
                            f"criterion {criterion_id} fixed unit fields must be "
                            "exactly id, label, and instruction"
                        )
                    unit_id = unit.get("id")
                    if (
                        not isinstance(unit_id, str)
                        or not re.fullmatch(r"[A-Za-z0-9_-]+", unit_id)
                        or unit_id in unit_ids
                    ):
                        raise DimensionContractError(
                            f"criterion {criterion_id} fixed unit id is invalid or repeated"
                        )
                    unit_ids.add(unit_id)
                    for field in ("label", "instruction"):
                        if not isinstance(unit.get(field), str) or not unit[field].strip():
                            raise DimensionContractError(
                                f"criterion {criterion_id} fixed unit {unit_id} "
                                f"{field} must be non-empty"
                            )
            elif source == "image_assets" and "required_units" in unit_evaluation:
                required_units = unit_evaluation.get("required_units")
                if not isinstance(required_units, list) or not required_units:
                    raise DimensionContractError(
                        f"criterion {criterion_id} image required_units must be a non-empty array"
                    )
                unit_ids: set[str] = set()
                for unit in required_units:
                    if not isinstance(unit, dict) or set(unit) != {
                        "id",
                        "label",
                        "instruction",
                    }:
                        raise DimensionContractError(
                            f"criterion {criterion_id} image required-unit fields must be "
                            "exactly id, label, and instruction"
                        )
                    unit_id = unit.get("id")
                    if (
                        not isinstance(unit_id, str)
                        or not unit_id.strip()
                        or unit_id in unit_ids
                    ):
                        raise DimensionContractError(
                            f"criterion {criterion_id} image required-unit id is empty or repeated"
                        )
                    if any(
                        not isinstance(unit.get(field), str)
                        or not unit[field].strip()
                        for field in ("label", "instruction")
                    ):
                        raise DimensionContractError(
                            f"criterion {criterion_id} image required-unit text must be non-empty"
                        )
                    unit_ids.add(unit_id)
        if not str(criterion.get("group", "")).startswith(group_prefix):
            raise DimensionContractError(
                f"criterion {criterion_id} is outside rubric role {expected_role}"
            )
        supports_na_is_valid = (
            expected_role == "final_case_specific"
            and criterion.get("group") == "final_page_level"
            and mode == "llm_judge"
        ) or (
            expected_role == "intermediate_case_specific"
            and mode == "llm_judge"
            and isinstance(criterion.get("unit_evaluation"), dict)
        )
        if "supports_na" in criterion and (
            not supports_na_is_valid or criterion.get("supports_na") is not True
        ):
            raise DimensionContractError(
                f"criterion {criterion_id} supports_na is only valid as true "
                "for final page-level or expanded Intermediate LLM criteria"
            )
        if expected_role == "final_case_specific":
            preaudit = criterion_preaudit_selection(criterion)
            preaudit_kinds = normalize_preaudit_kinds(
                preaudit,
                label=f"final criterion {criterion_id} preaudit",
            )
            if mode == "deterministic" and preaudit_kinds != ("none",):
                raise DimensionContractError(
                    f"deterministic final criterion {criterion_id} must use preaudit=none"
                )
            if mode != "deterministic" and "none" in preaudit_kinds:
                raise DimensionContractError(
                    f"LLM/hybrid final criterion {criterion_id} must use a frozen preaudit"
                )
        scoring = criterion.get("scoring")
        if not isinstance(scoring, dict) or scoring.get("profile") not in profile_ids:
            raise DimensionContractError(
                f"criterion {criterion_id} references an unknown scoring profile"
            )
        allowed_scoring_fields = {
            "profile",
            "anchors",
            "hard_boundaries",
            "adjustments",
        }
        unknown_scoring_fields = set(scoring) - allowed_scoring_fields
        if unknown_scoring_fields:
            raise DimensionContractError(
                f"criterion {criterion_id} has unknown scoring fields: "
                f"{sorted(unknown_scoring_fields)}"
            )
        anchors = scoring.get("anchors")
        if anchors is not None and (
            not isinstance(anchors, dict)
            or not anchors
            or not all(
                isinstance(key, str)
                and isinstance(value, str)
                and value.strip()
                for key, value in anchors.items()
            )
        ):
            raise DimensionContractError(
                f"criterion {criterion_id} scoring.anchors must be a non-empty object"
            )
        if anchors is not None:
            profile = rubric["score_contract"]["profiles"][scoring["profile"]]
            expected_anchor_scores = {
                float(score) for score in profile.get("allowed_scores", [])
            }
            try:
                actual_anchor_scores = {float(score) for score in anchors}
            except (TypeError, ValueError) as exc:
                raise DimensionContractError(
                    f"criterion {criterion_id} scoring.anchors keys must be scores"
                ) from exc
            if actual_anchor_scores != expected_anchor_scores:
                raise DimensionContractError(
                    f"criterion {criterion_id} scoring.anchors must cover every "
                    "profile score exactly"
                )
        boundaries = scoring.get("hard_boundaries")
        if boundaries is not None:
            if not isinstance(boundaries, list) or not boundaries:
                raise DimensionContractError(
                    f"criterion {criterion_id} scoring.hard_boundaries must be "
                    "a non-empty array"
                )
            structured_ids: set[str] = set()
            profile_scores = {
                float(score)
                for score in rubric["score_contract"]["profiles"][
                    scoring["profile"]
                ]["allowed_scores"]
            }
            for boundary in boundaries:
                if isinstance(boundary, str) and boundary.strip():
                    continue
                base_boundary_fields = {
                    "id",
                    "condition",
                    "max_score",
                    "scope",
                    "trigger",
                }
                if (
                    not isinstance(boundary, dict)
                    or frozenset(boundary)
                    not in {
                        frozenset(base_boundary_fields),
                        frozenset(base_boundary_fields | {"target"}),
                    }
                ):
                    raise DimensionContractError(
                        f"criterion {criterion_id} hard boundary must be a "
                        "non-empty legacy string or a structured object"
                    )
                boundary_id = boundary.get("id")
                if (
                    not isinstance(boundary_id, str)
                    or not boundary_id.strip()
                    or boundary_id in structured_ids
                ):
                    raise DimensionContractError(
                        f"criterion {criterion_id} hard-boundary id is empty or repeated"
                    )
                structured_ids.add(boundary_id)
                condition = boundary.get("condition")
                if not isinstance(condition, str) or not condition.strip():
                    raise DimensionContractError(
                        f"criterion {criterion_id} hard-boundary condition must be non-empty"
                    )
                max_score = boundary.get("max_score")
                if (
                    isinstance(max_score, bool)
                    or not isinstance(max_score, (int, float))
                    or float(max_score) not in profile_scores
                ):
                    raise DimensionContractError(
                        f"criterion {criterion_id} hard-boundary max_score must "
                        "belong to the criterion score profile"
                    )
                scope = boundary.get("scope")
                if scope not in {"criterion", "group"}:
                    raise DimensionContractError(
                        f"criterion {criterion_id} hard-boundary scope must be "
                        "criterion or group"
                    )
                if scope == "group":
                    target = boundary.get("target")
                    if (
                        not isinstance(target, str)
                        or not target.startswith(group_prefix)
                    ):
                        raise DimensionContractError(
                            f"criterion {criterion_id} group hard-boundary target "
                            "must name a group in the same rubric role"
                        )
                elif "target" in boundary:
                    raise DimensionContractError(
                        f"criterion {criterion_id} criterion hard-boundary "
                        "must not declare target"
                    )
                trigger = boundary.get("trigger")
                if not isinstance(trigger, dict):
                    raise DimensionContractError(
                        f"criterion {criterion_id} hard-boundary trigger must be an object"
                    )
                trigger_type = trigger.get("type")
                if trigger_type == "judge_assessment":
                    if set(trigger) != {"type"}:
                        raise DimensionContractError(
                            f"criterion {criterion_id} judge hard-boundary trigger "
                            "must contain only type"
                        )
                elif trigger_type == "visual_preaudit_metric":
                    if expected_role != "final_case_specific":
                        raise DimensionContractError(
                            f"criterion {criterion_id} visual pre-audit boundary "
                            "is only valid for final criteria"
                        )
                    if set(trigger) != {
                        "type",
                        "metric",
                        "operator",
                        "value",
                    }:
                        raise DimensionContractError(
                            f"criterion {criterion_id} visual pre-audit trigger "
                            "fields mismatch"
                        )
                    if trigger.get("metric") not in {
                        "real_scene_or_artifact_photo_page_count",
                        "generic_or_low_detail_core_visual_substitution_page_count",
                        "max_repeated_layout_skeleton_page_count",
                        "unreadable_key_label_page_count",
                    }:
                        raise DimensionContractError(
                            f"criterion {criterion_id} visual pre-audit metric is unsupported"
                        )
                    if trigger.get("operator") not in {
                        "lte",
                        "lt",
                        "gte",
                        "gt",
                        "eq",
                    }:
                        raise DimensionContractError(
                            f"criterion {criterion_id} hard-boundary operator is unsupported"
                        )
                    trigger_value = trigger.get("value")
                    if (
                        isinstance(trigger_value, bool)
                        or not isinstance(trigger_value, int)
                        or trigger_value < 0
                    ):
                        raise DimensionContractError(
                            f"criterion {criterion_id} hard-boundary value must "
                            "be a non-negative integer"
                        )
                else:
                    raise DimensionContractError(
                        f"criterion {criterion_id} hard-boundary trigger type is unsupported"
                    )
        _validate_adjustments(scoring, criterion_id)
        input_bundle_ids = set(criterion.get("input_bundles", []))
        unknown_bundles = input_bundle_ids - bundle_ids
        if unknown_bundles:
            raise DimensionContractError(
                f"criterion {criterion_id} references unknown bundles: "
                f"{sorted(unknown_bundles)}"
            )
        if (
            expected_role == "final_case_specific"
            and "final_html_content" in bundle_ids
        ):
            preaudit_kinds = normalize_preaudit_kinds(
                criterion_preaudit_selection(criterion),
                label=f"final criterion {criterion_id} preaudit",
            )
            semantic_content_declared = (
                "final_html_content" in input_bundle_ids
            )
            semantic_content_required = "content" in preaudit_kinds
            if semantic_content_declared != semantic_content_required:
                raise DimensionContractError(
                    f"final criterion {criterion_id} input_bundles must "
                    f"{'include' if semantic_content_required else 'exclude'} "
                    "final_html_content to match its preaudit routing"
                )
        criterion_requirements = set(criterion.get("query_requirements", []))
        if not criterion_requirements:
            raise DimensionContractError(
                f"criterion {criterion_id} must reference at least one query requirement"
            )
        unknown_requirements = criterion_requirements - requirement_ids
        if unknown_requirements:
            raise DimensionContractError(
                f"criterion {criterion_id} references unknown requirements: "
                f"{sorted(unknown_requirements)}"
            )
        referenced_requirement_ids.update(criterion_requirements)
    unreferenced_requirements = requirement_ids - referenced_requirement_ids
    if unreferenced_requirements:
        raise DimensionContractError(
            "query requirements are not referenced by any criterion: "
            f"{sorted(unreferenced_requirements)}"
        )
    criterion_groups = {str(criterion["group"]) for criterion in criteria}
    weighted_groups = (
        criterion_groups - {"final_knowledge"}
        if aggregation.get("knowledge_as_deck_dimension") is True
        else criterion_groups
    )
    if (
        not uses_dimension_hierarchy
        and group_weights is not None
        and set(group_weights) != weighted_groups
    ):
        raise DimensionContractError(
            "case rubric aggregation.group_weights must exactly match "
            f"scored groups: expected={sorted(weighted_groups)}, "
            f"actual={sorted(group_weights)}"
        )
    if uses_dimension_hierarchy:
        dimension_by_id = {
            str(dimension["id"]): dimension for dimension in dimensions
        }
        referenced_dimensions = {
            str(criterion["dimension_id"]) for criterion in criteria
        }
        if referenced_dimensions != dimension_ids:
            raise DimensionContractError(
                "case rubric dimensions must all be referenced by criteria: "
                f"unreferenced={sorted(dimension_ids - referenced_dimensions)}"
            )
        for criterion in criteria:
            criterion_group = str(criterion["group"])
            dimension_group = str(
                dimension_by_id[str(criterion["dimension_id"])]["group"]
            )
            if (
                criterion_group != dimension_group
                and not (
                    expected_role == "final_case_specific"
                    and criterion_group == "final_knowledge"
                    and dimension_group == "final_deck_level"
                )
            ):
                raise DimensionContractError(
                    f"criterion {criterion['id']} group {criterion_group!r} does "
                    f"not match dimension group {dimension_group!r}"
                )
        if group_weights is not None:
            dimension_groups = {
                str(dimension["group"]) for dimension in dimensions
            }
            if expected_role == "final_case_specific":
                dimension_groups.discard("final_knowledge")
            if set(group_weights) != dimension_groups:
                raise DimensionContractError(
                    "case rubric aggregation.group_weights must exactly match "
                    "dimension groups: "
                    f"expected={sorted(dimension_groups)}, "
                    f"actual={sorted(group_weights)}"
                )
    used_profile_ids = {
        str(criterion["scoring"]["profile"]) for criterion in criteria
    }
    unused_profile_ids = profile_ids - used_profile_ids
    if (
        expected_role == "intermediate_case_specific"
        and "common_rubric" not in rubric
        and unused_profile_ids
    ):
        raise DimensionContractError(
            f"case rubric declares unused score profiles: {sorted(unused_profile_ids)}"
        )


def validate_split_case_rubrics(
    intermediate: dict[str, Any], final: dict[str, Any]
) -> None:
    for field in ("schema_version", "case_id", "language"):
        if intermediate.get(field) != final.get(field):
            raise DimensionContractError(
                f"split case rubrics disagree on shared field {field}"
            )
    intermediate_ids = {
        str(criterion["id"]) for criterion in intermediate["criteria"]
    }
    final_ids = {str(criterion["id"]) for criterion in final["criteria"]}
    repeated = intermediate_ids & final_ids
    if repeated:
        raise DimensionContractError(
            f"split case rubrics repeat criterion IDs: {sorted(repeated)}"
        )


def validate_final_common_hard_boundaries(rubric: dict[str, Any]) -> None:
    boundaries = rubric.get("global_hard_boundaries")
    if not isinstance(boundaries, list) or not boundaries:
        raise DimensionContractError(
            "final common rubric global_hard_boundaries must be a non-empty array"
        )
    seen: set[str] = set()
    for boundary in boundaries:
        if not isinstance(boundary, dict) or set(boundary) != {
            "id",
            "condition",
            "max_score",
            "trigger",
        }:
            raise DimensionContractError(
                "final common global hard-boundary fields mismatch"
            )
        boundary_id = boundary.get("id")
        if (
            not isinstance(boundary_id, str)
            or not boundary_id.strip()
            or boundary_id in seen
        ):
            raise DimensionContractError(
                "final common global hard-boundary id is empty or repeated"
            )
        seen.add(boundary_id)
        if (
            not isinstance(boundary.get("condition"), str)
            or not boundary["condition"].strip()
        ):
            raise DimensionContractError(
                f"final common hard-boundary {boundary_id} condition must be non-empty"
            )
        max_score = boundary.get("max_score")
        if (
            isinstance(max_score, bool)
            or not isinstance(max_score, int)
            or not 0 <= max_score <= 5
        ):
            raise DimensionContractError(
                f"final common hard-boundary {boundary_id} max_score must be 0..5"
            )
        if boundary.get("trigger") != {
            "type": "relevant_preaudit_non_local_defect"
        }:
            raise DimensionContractError(
                f"final common hard-boundary {boundary_id} trigger is unsupported"
            )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def query_requirements(case_rubric: dict[str, Any], criterion: dict[str, Any]) -> dict[str, Any]:
    mapping = case_rubric.get("query_requirement_map", {})
    if not isinstance(mapping, dict):
        return {}
    return {
        requirement_id: mapping.get(requirement_id)
        for requirement_id in criterion.get("query_requirements", [])
    }


def _json_prompt(title: str, payload: dict[str, Any], expected: dict[str, Any]) -> str:
    return (
        f"{title}\n\n"
        "Return exactly one JSON object matching EXPECTED OUTPUT.\n\n"
        "EXPECTED OUTPUT\n"
        + json.dumps(expected, ensure_ascii=False, indent=2)
        + "\n\nTASK AND EVIDENCE\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def final_preaudit_task(
    audit_type: str,
    *,
    instruction_packet: dict[str, Any],
    deck_images: list[Path],
    html_content: str | None,
) -> JudgeTask:
    page_count = len(deck_images)
    if audit_type == "content":
        title = "Audit final extracted content before scoring. Do not score."
        audit_rules = [
            "Inspect every page in canonical order.",
            "Identify missing task requirements, weak or duplicated content, narrative gaps, factual or cross-page conflicts, and unsupported claims.",
            "Classify a deck defect as non_local when one substantive improvement must change two or more pages, a section, or the deck-level narrative; otherwise classify it as local.",
            "Do not use external generation artifacts or infer content from implementation intent.",
        ]
        if html_content is not None:
            audit_rules[1:1] = [
                "Use only the source-grounded extraction of final HTML content for presentation content.",
            ]
        else:
            audit_rules.insert(
                1,
                "No final HTML content is available; report this as unverifiable.",
            )
        evidence = {
            "instruction": instruction_packet,
        }
        if html_content is not None:
            evidence["final_html_content"] = html_content
    elif audit_type == "visual":
        title = "Audit final visual defects before scoring. Do not score."
        audit_rules = [
            "Inspect every page in canonical order and then inspect deck-level consistency.",
            "Identify clipping, overlap, illegibility, weak hierarchy, accidental empty space, poor composition, crude or generic diagrams, weak image treatment, repetitive layouts, style drift, and rendering defects.",
            "Count real scene/artifact photo pages conservatively: decorative textures, icons, silhouettes, maps, SVG diagrams, and low-detail drawings are not real photos.",
            "Count a page as generic_or_low_detail_core_visual_substitution only when a generic icon or low-detail schematic replaces the page's core spatial, historical, architectural, or artifact visual.",
            "Report repeated_layout_skeleton_groups when pages share the same dominant composition skeleton, not merely the same design system.",
            "Report unreadable_key_label_pages when a key label cannot be read reliably at normal presentation scale.",
            "Classify a deck defect as non_local when one substantive improvement must change two or more pages, a section, or the deck-level visual system; otherwise classify it as local.",
            "Distinguish intentional consistency from monotonous repetition.",
            "Do not inspect HTML, plans, research, asset catalogs, traces, or any other generation artifact.",
        ]
        evidence = {
            "instruction": instruction_packet,
            "attachment_note": (
                "Every final rendered slide is attached in canonical order with an "
                "interleaved SLIDE_XX_OF_YY label."
            ),
        }
    else:
        raise RunnerError(f"unsupported final pre-audit type: {audit_type}")
    expected: dict[str, Any] = {
        "contract_version": FINAL_PREAUDIT_CONTRACT_VERSION,
        "audit_type": audit_type,
        "pages": [
            {
                "page": f"integer 1 through {page_count}; return every page exactly once",
                "summary": "concise page summary",
                "strengths": ["observable strength"],
                "defects": ["observable defect; empty only when genuinely none"],
                "unverifiable": ["claim or detail that cannot be verified; otherwise empty"],
            }
        ],
        "deck_strengths": ["cross-page strength"],
        "deck_defects": [
            {
                "id": "stable unique defect id",
                "description": "observable deck-level defect",
                "impact_scope": "local or non_local",
                "pages": ["integer page numbers; non_local requires at least two"],
                "improvement": "specific change needed to resolve the defect",
            }
        ],
        "deck_unverifiable": ["deck-level unverifiable point"],
    }
    if audit_type == "visual":
        expected["visual_metrics"] = {
            "real_scene_or_artifact_photo_pages": [
                "page numbers containing a real scene or artifact photo"
            ],
            "generic_or_low_detail_core_visual_substitution_pages": [
                "page numbers where generic/low-detail art replaces the core visual"
            ],
            "repeated_layout_skeleton_groups": [
                {
                    "pages": ["at least two page numbers"],
                    "shared_skeleton": "observable dominant layout skeleton",
                }
            ],
            "unreadable_key_label_pages": [
                "page numbers with a key label unreadable at normal presentation scale"
            ],
        }
    payload = {
        "evaluation_scope": "final_output_pre_scoring_audit",
        "audit_rules": audit_rules,
        "scoring_information_withheld": True,
        "evidence_isolation": (
            "Only instruction.md is always available. "
            + (
                "For this content audit, source-grounded content extracted from the final "
                "HTML by the configured LLM is also supplied; no rendered images are supplied. "
                if audit_type == "content" and html_content is not None
                else (
                    "No final HTML content or rendered images are supplied for this content audit. "
                    if audit_type == "content"
                    else "Final rendered images are supplied for this visual audit. "
                )
            )
            + "No raw HTML or generation intermediate artifact is supplied."
        ),
        "evidence": evidence,
    }
    string_array = {"type": "array", "items": {"type": "string"}}
    page_schema = {
        "type": "object",
        "properties": {
            "page": {"type": "integer"},
            "summary": {"type": "string"},
            "strengths": string_array,
            "defects": string_array,
            "unverifiable": string_array,
        },
        "required": ["page", "summary", "strengths", "defects", "unverifiable"],
        "additionalProperties": False,
    }
    deck_defect_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "description": {"type": "string"},
            "impact_scope": {"type": "string"},
            "pages": {"type": "array", "items": {"type": "integer"}},
            "improvement": {"type": "string"},
        },
        "required": ["id", "description", "impact_scope", "pages", "improvement"],
        "additionalProperties": False,
    }
    properties: dict[str, Any] = {
        "contract_version": {"type": "string"},
        "audit_type": {"type": "string"},
        "pages": {
            "type": "array",
            "items": page_schema,
            "minItems": page_count,
            "maxItems": page_count,
        },
        "deck_strengths": string_array,
        "deck_defects": {"type": "array", "items": deck_defect_schema},
        "deck_unverifiable": string_array,
    }
    if audit_type == "visual":
        properties["visual_metrics"] = {
            "type": "object",
            "properties": {
                "real_scene_or_artifact_photo_pages": {
                    "type": "array",
                    "items": {"type": "integer"},
                },
                "generic_or_low_detail_core_visual_substitution_pages": {
                    "type": "array",
                    "items": {"type": "integer"},
                },
                "repeated_layout_skeleton_groups": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "pages": {"type": "array", "items": {"type": "integer"}},
                            "shared_skeleton": {"type": "string"},
                        },
                        "required": ["pages", "shared_skeleton"],
                        "additionalProperties": False,
                    },
                },
                "unreadable_key_label_pages": {
                    "type": "array",
                    "items": {"type": "integer"},
                },
            },
            "required": [
                "real_scene_or_artifact_photo_pages",
                "generic_or_low_detail_core_visual_substitution_pages",
                "repeated_layout_skeleton_groups",
                "unreadable_key_label_pages",
            ],
            "additionalProperties": False,
        }
    response_json_schema = {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }
    return JudgeTask(
        task_id=f"final_preaudit.{audit_type}",
        kind=f"final_{audit_type}_preaudit",
        judge_role="final",
        user_prompt=_json_prompt(title, payload, expected),
        attachment=deck_images if audit_type == "visual" else None,
        validate=lambda value, kind=audit_type, count=page_count: validate_final_preaudit(
            value,
            audit_type=kind,
            page_count=count,
        ),
        mock_response=mock_final_preaudit(audit_type, page_count),
        response_json_schema=response_json_schema,
    )


def detected_defects_only(
    audit: dict[str, Any],
    *,
    page_number: int | None = None,
) -> dict[str, Any]:
    """Project a full pre-audit to defects only for a scoring Judge."""
    return {
        "audit_type": audit.get("audit_type"),
        "page_defects": [
            {
                "page": page.get("page"),
                "defects": list(page.get("defects", [])),
            }
            for page in audit.get("pages", [])
            if isinstance(page, dict)
            and page.get("defects")
            and (page_number is None or page.get("page") == page_number)
        ],
        "deck_defects": (
            list(audit.get("deck_defects", []))
            if page_number is None
            else []
        ),
    }


def _collect_asset_paths(value: Any) -> set[str]:
    paths: set[str] = set()
    if isinstance(value, dict):
        for child in value.values():
            paths.update(_collect_asset_paths(child))
    elif isinstance(value, list):
        for child in value:
            paths.update(_collect_asset_paths(child))
    elif isinstance(value, str):
        paths.update(
            match.replace("\\", "/")
            for match in re.findall(
                r"assets/[A-Za-z0-9_./-]+\.(?:png|jpe?g|webp|gif|svg)",
                value,
                flags=re.IGNORECASE,
            )
        )
    return paths


def intermediate_evaluation_units(
    criterion: dict[str, Any],
    run_dir: Path,
    page_numbers: list[int],
) -> list[dict[str, Any]]:
    config = criterion.get("unit_evaluation")
    if not isinstance(config, dict):
        return []
    source = str(config["source"])
    template = str(config["unit_instruction"])
    if source == "fixed":
        return [
            {
                "unit_id": str(unit["id"]),
                "unit_label": str(unit["label"]),
                "unit_instruction": str(unit["instruction"]),
                "unit_source": source,
            }
            for unit in config["units"]
        ]
    if source == "slide_pages":
        return [
            {
                "unit_id": f"page_{page:02d}",
                "unit_label": f"第{page}页过程",
                "unit_instruction": template.format(
                    page=page, unit_label=f"第{page}页过程"
                ),
                "unit_source": source,
                "page": page,
                "required_paths": [
                    f"{{RUN_DIR}}/plan/slide_{page:02d}.md",
                    f"{{RUN_DIR}}/_trace/subagents/slide_{page:02d}/tool_log.json",
                    f"{{RUN_DIR}}/slides/slide_{page:02d}.html",
                    f"{{RUN_DIR}}/renders/.page_{page:02d}/render.json",
                    f"{{RUN_DIR}}/renders/slide_{page:02d}.png",
                ],
            }
            for page in page_numbers
        ]
    if source == "plan_pages_plus_deck":
        units = [
            {
                "unit_id": "deck_narrative",
                "unit_label": "整册叙事与章节组织",
                "unit_instruction": template.format(
                    page="deck", unit_label="整册叙事与章节组织"
                ),
                "unit_source": source,
                "scope": "deck",
                "required_paths": ["{RUN_DIR}/plan/deck.md"],
            }
        ]
        units.extend(
            {
                "unit_id": f"page_{page:02d}",
                "unit_label": f"第{page}页规划可执行性",
                "unit_instruction": template.format(
                    page=page, unit_label=f"第{page}页规划可执行性"
                ),
                "unit_source": source,
                "scope": "page",
                "page": page,
                "required_paths": [f"{{RUN_DIR}}/plan/slide_{page:02d}.md"],
            }
            for page in page_numbers
        )
        return units
    if source == "image_assets":
        required_units = [
            {
                "unit_id": str(unit["id"]),
                "unit_label": str(unit["label"]),
                "unit_instruction": str(unit["instruction"]),
                "unit_source": "fixed",
            }
            for unit in config.get("required_units", [])
        ]
        planned_pages: dict[str, list[int]] = {}
        pages_path = run_dir / "plan" / "pages.json"
        if pages_path.is_file():
            pages_value = load_json(pages_path)
            for page in pages_value.get("pages", []):
                if not isinstance(page, dict) or not isinstance(page.get("page"), int):
                    continue
                for asset in _collect_asset_paths(page.get("assets", [])):
                    planned_pages.setdefault(asset, []).append(int(page["page"]))
        catalog_assets: set[str] = set()
        for catalog in sorted((run_dir / "assets").glob("catalog*.md")):
            catalog_assets.update(
                _collect_asset_paths(catalog.read_text(encoding="utf-8"))
            )
        asset_paths = sorted(set(planned_pages) | catalog_assets)
        if not asset_paths:
            if required_units:
                return required_units
            supports_na = criterion.get("supports_na") is True
            unit_id = (
                "no_image_tasks"
                if supports_na
                else "missing_required_image_tasks"
            )
            unit_label = (
                "无外部、附件或生成图片任务"
                if supports_na
                else "缺失Query要求的图片任务"
            )
            unit_instruction = (
                template.format(
                    asset="N/A",
                    unit_label=unit_label,
                    planned_pages=[],
                )
                if supports_na
                else (
                    "Query与rubric要求执行本图片过程criterion，但规划、资产目录"
                    "和现有产物中未发现可展开的外部、附件或生成图片任务。"
                    "该缺失仍然适用，必须依据现有评分anchor评判，不得返回N/A。"
                )
            )
            return [
                {
                    "unit_id": unit_id,
                    "unit_label": unit_label,
                    "unit_instruction": unit_instruction,
                    "unit_source": source,
                    "asset": "",
                    "planned_pages": [],
                    "required_paths": [],
                }
            ]
        return required_units + [
            {
                "unit_id": "asset_" + re.sub(r"[^A-Za-z0-9]+", "_", asset).strip("_"),
                "unit_label": asset,
                "unit_instruction": template.format(
                    asset=asset,
                    unit_label=asset,
                    planned_pages=planned_pages.get(asset, []),
                ),
                "unit_source": source,
                "asset": asset,
                "planned_pages": planned_pages.get(asset, []),
                "required_paths": (
                    [f"{{RUN_DIR}}/{asset}"]
                    if str(criterion["id"]).startswith("I02_")
                    else []
                ),
            }
            for asset in asset_paths
        ]
    raise DimensionContractError(
        f"criterion {criterion['id']} has unsupported unit source {source!r}"
    )


def scope_intermediate_unit_files(
    files: list[Path],
    unit: dict[str, Any],
    case_dir: Path,
    run_dir: Path,
) -> list[Path]:
    source = str(unit["unit_source"])
    if source == "fixed":
        return files

    def relative(path: Path) -> str:
        try:
            return path.resolve().relative_to(run_dir.resolve()).as_posix()
        except ValueError:
            return display_path(path, case_dir, run_dir)

    if source == "slide_pages":
        page = int(unit["page"])
        trace = f"_trace/subagents/slide_{page:02d}/tool_log.json"
        render = f"renders/.page_{page:02d}/render.json"
        page_plan = f"plan/slide_{page:02d}.md"
        slide_html = f"slides/slide_{page:02d}.html"
        render_image = f"renders/slide_{page:02d}.png"
        return [
            path
            for path in files
            if relative(path) in {trace, render, page_plan, slide_html, render_image}
            or relative(path).startswith("assets/catalog")
        ]
    if source == "plan_pages_plus_deck":
        common = lambda rel: (
            rel == "plan/deck.md"
            or rel.startswith("assets/catalog")
            or rel.startswith("research/")
        )
        if unit.get("scope") == "deck":
            return [path for path in files if common(relative(path))]
        page = int(unit["page"])
        page_plan = f"plan/slide_{page:02d}.md"
        return [
            path
            for path in files
            if common(relative(path)) or relative(path) == page_plan
        ]
    if source == "image_assets":
        asset = str(unit["asset"])
        basename = Path(asset).name
        relevant_catalogs: list[Path] = []
        for path in files:
            rel = relative(path)
            if rel.startswith("assets/catalog") and path.suffix.lower() == ".md":
                if asset in path.read_text(encoding="utf-8"):
                    relevant_catalogs.append(path)
        image_agents = {
            match.group(1)
            for path in relevant_catalogs
            if (match := re.search(r"catalog_(image_[0-9]+)\.md$", path.name))
        }
        selected: list[Path] = []
        for path in files:
            rel = relative(path)
            keep = (
                rel.startswith("{CASE_DIR}/")
                or rel in {"plan/deck.md", "plan/pages.json", asset}
                or path in relevant_catalogs
                or rel == "assets/catalog.md"
                or (
                    rel.startswith("_trace/subagents/image_")
                    and (
                        not image_agents
                        or any(f"/{agent}/" in f"/{rel}" for agent in image_agents)
                    )
                )
            )
            if rel.startswith("slides/slide_") and path.suffix.lower() == ".html":
                text = path.read_text(encoding="utf-8", errors="replace")
                keep = asset in text or basename in text
            if keep:
                selected.append(path)
        return selected
    return files


def case_task(
    case_rubric: dict[str, Any],
    criterion: dict[str, Any],
    case_dir: Path,
    run_dir: Path,
    deck_images: list[Path],
    final_preaudits: dict[str, dict[str, Any]],
    final_html_content: str | None = None,
    final_page_index: dict[str, dict[str, str]] | None = None,
    evaluation_unit: dict[str, Any] | None = None,
    page_number: int | None = None,
    final_page_html_content: str | None = None,
) -> tuple[JudgeTask, dict[str, Any] | None]:
    profile = case_profile(case_rubric, criterion)
    patterns, bundle_uses = patterns_for_bundles(
        case_rubric,
        criterion.get("input_bundles", []),
    )
    files, unmatched = expand_evidence_patterns(patterns, case_dir, run_dir)
    if evaluation_unit is not None:
        files = scope_intermediate_unit_files(
            files, evaluation_unit, case_dir, run_dir
        )
    unmatched = [
        pattern
        for pattern in unmatched
        if not any(character in pattern for character in ("*", "?", "["))
    ]
    criterion_group = str(criterion["group"])
    is_final = criterion_group.startswith("final_")
    is_knowledge = criterion_group == "final_knowledge"
    is_deck_level = criterion_group == "final_deck_level"
    is_page_level = criterion_group == "final_page_level"
    if is_page_level and page_number is None:
        raise RunnerError(
            f"final page criterion {criterion['id']} requires one page_number"
        )
    if not is_page_level and page_number is not None:
        raise RunnerError("page_number is only valid for final page criteria")
    criterion_supports_na = (
        not is_final and criterion.get("supports_na") is True
    )
    page_supports_na = is_page_level and criterion.get("supports_na") is True
    input_bundles = tuple(str(item) for item in criterion.get("input_bundles", []))
    asset_reference_only_html = (
        not is_final
        and str(criterion["id"]).startswith("I02_")
        and "slide_outputs" in input_bundles
    )
    needs_final_html = is_final and (
        "final_html_content" in input_bundles or is_page_level
    )
    needs_final_visual = is_final and "final_deck_visual" in input_bundles
    preaudit_kinds: tuple[str, ...] = ()
    if is_final:
        enforce_final_llm_evidence_isolation(files, case_dir, run_dir)
    if is_final and not is_knowledge and final_preaudits:
        preaudit_kinds = normalize_preaudit_kinds(
            criterion_preaudit_selection(criterion),
            label=f"final criterion {criterion['id']} preaudit",
        )
    packet_files = files
    if is_final:
        semantic_content_path = (
            final_html_content_output_dir(run_dir) / "content.md"
        ).resolve()
        packet_files = [
            path
            for path in files
            if path.resolve() != semantic_content_path
        ]
        if is_page_level:
            # Page requests receive no deck-wide final evidence. The criterion,
            # query requirements, current page extraction, current render, and
            # current page type are supplied explicitly below.
            packet_files = []
            unmatched = []
    visual_attachments = (
        [deck_images[page_number - 1]]
        if is_page_level and page_number is not None
        else deck_images
        if needs_final_visual
        else intermediate_visual_attachments(files)
        if not is_final
        else []
    )
    packet = build_evidence_packet(
        packet_files,
        unmatched,
        case_dir,
        run_dir,
        total_chars=(
            420_000
            if not is_final and criterion_group == "intermediate_slide"
            else 180_000
        ),
        per_file_chars=40_000 if not is_final else 24_000,
        include_final_html=not is_final and not asset_reference_only_html,
        summarize_html_assets=asset_reference_only_html,
        trace_string_chars=1_200 if not is_final else 4_000,
        trace_mode=(
            "workflow"
            if not is_final and criterion_group == "intermediate_slide"
            else "audit"
            if not is_final
            and criterion_group in {"intermediate_research", "intermediate_image"}
            else "detailed"
        ),
        preserve_full_text=not is_final,
    )
    required_evidence_units = (
        list(
            dict.fromkeys(
                [display_path(path, case_dir, run_dir) for path in files]
                + (
                    list(evaluation_unit.get("required_paths", []))
                    if evaluation_unit is not None
                    else []
                )
            )
        )
        if not is_final
        else []
    )
    observation = None
    if criterion.get("mode") == "hybrid":
        observation = generic_hybrid_observation(run_dir, case_dir, criterion, files)
    payload = {
        "evaluation_scope": "final_output" if is_final else "intermediate_artifacts",
        "case_id": case_rubric["case_id"],
        "rubric": {
            "rubric_id": case_rubric["rubric_id"],
            "rubric_role": case_rubric["rubric_role"],
            "title": case_rubric["title"],
            "language": case_rubric["language"],
        },
        # defect_level belongs exclusively to deterministic post-processing;
        # exposing it here could bias the Judge's unchanged base score.
        "criterion": {
            key: value
            for key, value in criterion.items()
            if key != "defect_level"
        },
        "score_profile": profile,
        "query_requirements": query_requirements(case_rubric, criterion),
        "bundle_uses": bundle_uses,
        "deterministic_observation": observation,
        "evidence_packet": packet,
    }
    if evaluation_unit is not None:
        payload["evaluation_unit"] = {
            "unit_id": evaluation_unit["unit_id"],
            "unit_label": evaluation_unit["unit_label"],
            "instruction": evaluation_unit["unit_instruction"],
            **(
                {"page": evaluation_unit["page"]}
                if "page" in evaluation_unit
                else {}
            ),
            **(
                {
                    "asset": evaluation_unit["asset"],
                    "planned_pages": evaluation_unit.get("planned_pages", []),
                    "asset_exists": (run_dir / evaluation_unit["asset"]).is_file(),
                }
                if "asset" in evaluation_unit
                else {}
            ),
        }
        payload["evaluation_unit"]["required_evidence_paths"] = list(
            evaluation_unit.get("required_paths", [])
        )
        payload["unit_scoring_protocol"] = (
            "Judge only this evaluation_unit. Apply the criterion's unchanged "
            "anchors locally to this unit. Universal words in an anchor refer "
            "to every requirement inside this unit, not to units handled by "
            "other requests. Do not infer other units. Runtime computes the "
            "criterion arithmetic mean after all units are judged."
        )
        if evaluation_unit.get("unit_id") == "missing_required_image_tasks":
            payload["unit_scoring_protocol"] += (
                " This unit represents a required image process that is absent, "
                "not a semantically inapplicable process. It must remain "
                "applicable and match one existing score anchor; N/A is forbidden."
            )
    if not is_final:
        payload["full_scope_review_protocol"] = {
            "required_evidence_units": required_evidence_units,
            "rules": [
                "Inspect every required_evidence_unit; sampling or checking only representative files is forbidden.",
                "Expand every universal requirement in the criterion and instruction (for example all requirements, all planned pages, all image tasks, or all slide traces) into its complete set of semantic units and check every unit.",
                "A concise evidence list may cite representative locators only after the complete review; representative evidence never proves that all units passed.",
                "If any one semantic unit is not satisfied, list it in not_satisfied and do not select the maximum score.",
                "If any one semantic unit or required artifact cannot be verified from supplied evidence, list it in unverifiable and do not select the maximum score.",
            ],
        }
    payload["score_derivation_protocol"] = (
        "Analyze every declared score anchor before scoring. Return the exact "
        "anchor text, a concrete match decision, reason, and evidence for every "
        "allowed score. Mark exactly one anchor as matches=true when applicable, "
        "or no anchor only when the expected output explicitly permits "
        "applicable=false. Do not return score or raw_score; "
        "runtime derives the raw score from the uniquely matching anchor."
    )
    if not is_final and visual_attachments:
        payload["visual_attachment_manifest"] = visual_attachment_prompt_manifest(
            visual_attachments,
            case_dir,
            run_dir,
        )
        payload["visual_attachment_usage"] = (
            "The images listed above are attached to this Judge request as base64 "
            "visual inputs in the same order. Transport labels use SLIDE_XX_OF_YY "
            "for compatibility even when a file is an intermediate asset rather "
            "than a final slide. Use the mapped run-relative path in evidence."
        )
    if is_final:
        selected_preaudits: dict[str, dict[str, Any]] = {}
        for preaudit_kind in preaudit_kinds:
            frozen_preaudit = final_preaudits.get(preaudit_kind)
            if frozen_preaudit is None:
                raise RunnerError(
                    f"final criterion {criterion['id']} requires missing "
                    f"{preaudit_kind!r} pre-audit"
                )
            selected_preaudits[preaudit_kind] = frozen_preaudit
        if needs_final_html:
            html_content = (
                final_page_html_content if is_page_level else final_html_content
            )
            if html_content is None:
                raise RunnerError(
                    f"final criterion {criterion['id']} requires missing "
                    "final HTML semantic content"
                )
            payload["final_html_content"] = html_content
        if is_page_level:
            if final_page_index is None:
                raise RunnerError(
                    f"final page criterion {criterion['id']} requires missing "
                    "final page index"
                )
            assert page_number is not None
            page_metadata = final_page_index[str(page_number)]
            payload["page_context"] = {
                "page": page_number,
                "page_type": page_metadata["page_type"],
            }
            payload["page_scoring_protocol"] = (
                "Score only the current page with the criterion's 0/0.5/1 "
                "profile. Do not infer or discuss other pages. Runtime computes "
                "the criterion arithmetic mean after every page is judged in an "
                "independent request."
                + (
                    " When this criterion declares supports_na, a page with no "
                    "object evaluated by the criterion must still be returned with "
                    "applicable=false and score=null; runtime excludes only those "
                    "pages from the mean."
                    if page_supports_na
                    else ""
                )
            )
        if is_deck_level:
            payload["deck_review_protocol"] = (
                (
                    "All final rendered pages are attached in canonical order. "
                    if visual_attachments
                    else "The complete extracted HTML content covers every page. "
                )
                + "Inspect every page before selecting the deck score and return "
                "the complete canonical page list in pages_reviewed. Evidence "
                "examples may be representative, but they do not replace the "
                "full-deck review."
            )
        payload["evidence_isolation"] = (
            (
                (
                    "The current rendered page is authoritative for actual visibility, "
                    "legibility, clipping, and occlusion. "
                )
                if is_page_level
                else (
                    "The ordered final rendered images are authoritative for actual visibility, "
                    "legibility, clipping, and occlusion. "
                )
                if visual_attachments
                else "No final rendered images are supplied for this criterion. "
            )
            + (
                "The validated semantic extraction of final HTML content is supplied "
                + (
                    "for content grounding; it must not override conflicting rendered evidence. "
                    if visual_attachments
                    else "as the authoritative content evidence. "
                )
                if needs_final_html
                else "No HTML-derived content extraction is supplied. "
            )
            + "A rubric-versioned frozen reference fact file may be supplied only for "
            "case-specific factual verification; its URLs are provenance, not live browsing. "
            "No raw HTML, plan, research, knowledge brief, asset catalog, trace, speech, "
            "or other generation intermediate artifact may be used."
        )
        if visual_attachments:
            payload["attachment_note"] = (
                "Only the current rendered page is attached."
                if is_page_level
                else "Every final rendered slide is attached in canonical order "
                "with an interleaved SLIDE_XX_OF_YY label."
            )
        if selected_preaudits:
            detected_final_defects: dict[str, dict[str, Any]] = {}
            for kind, audit in selected_preaudits.items():
                defects = detected_defects_only(
                    audit,
                    page_number=page_number if is_page_level else None,
                )
                if defects["page_defects"] or defects["deck_defects"]:
                    detected_final_defects[kind] = defects
            if detected_final_defects:
                payload["detected_final_defects"] = detected_final_defects
                payload["preaudit_usage"] = (
                    "Only detected defects are supplied. Use defects relevant to this "
                    "criterion; do not import unrelated defects into the score. "
                    "This is a non-exhaustive defect hint. "
                    "Absence of a reported defect is not evidence that the criterion "
                    "is satisfied. Evaluate every anchor independently from the "
                    "original evidence."
                )
    allowed = [float(item) for item in profile["allowed_scores"]]
    mock_score = allowed[len(allowed) // 2]
    declared_anchors = criterion.get("scoring", {}).get("anchors", {})

    def anchor_text(score: float) -> str:
        key = str(int(score)) if score.is_integer() else str(score)
        value = declared_anchors.get(key)
        if not isinstance(value, str):
            raise DimensionContractError(
                f"criterion {criterion['id']} is missing anchor {key}"
            )
        return value

    expected_anchor_analysis = [
        {
            "score": score,
            "anchor": anchor_text(score),
            "matches": (
                "boolean; exactly one true when applicable, none only when "
                "the expected output permits applicable=false"
                if criterion_supports_na or page_supports_na
                else "boolean; exactly one true"
            ),
            "reason": "criterion-specific explanation of why this anchor matches or does not match",
            "evidence": ["concrete locators supporting this anchor assessment"],
        }
        for score in allowed
    ]
    mock_anchor_analysis = [
        {
            "score": score,
            "anchor": anchor_text(score),
            "matches": score == mock_score,
            "reason": "mock anchor assessment for pipeline validation",
            "evidence": ["mock://anchor/evidence/1", "mock://anchor/evidence/2"],
        }
        for score in allowed
    ]
    canonical_page_numbers = list(range(1, len(deck_images) + 1))
    if is_page_level:
        assert final_page_index is not None
        assert page_number is not None
        page_type = final_page_index[str(page_number)]["page_type"]
        expected = {
            "criterion_id": criterion["id"],
            "page": page_number,
            "page_type": page_type,
            **(
                {
                    "applicable": (
                        "boolean; false only when this page has no object "
                        "evaluated by the criterion"
                    )
                }
                if page_supports_na
                else {}
            ),
            "evidence": ["at least one current-page concrete locator"],
            "reason": "concise current-page scoring reason",
            "missing_or_unverifiable": [],
            "anchor_analysis": expected_anchor_analysis,
        }
        mock = {
            "criterion_id": criterion["id"],
            "page": page_number,
            "page_type": page_type,
            **({"applicable": True} if page_supports_na else {}),
            "evidence": [f"mock://page/{page_number}/1", f"mock://page/{page_number}/2"],
            "reason": "mock page-level judge response",
            "missing_or_unverifiable": [],
            "anchor_analysis": mock_anchor_analysis,
        }
    else:
        expected = {
            "criterion_id": criterion["id"],
            "evidence": [
                "at least two concrete locators for a non-zero score; may be empty only for score 0 when missing_or_unverifiable names the absent requirement",
                "a second concrete locator",
            ],
            "reason": "concise scoring reason",
            "missing_or_unverifiable": [],
        }
        mock = {
            "criterion_id": criterion["id"],
            "evidence": ["mock://evidence/1", "mock://evidence/2"],
            "reason": "mock judge response for pipeline validation",
            "missing_or_unverifiable": [],
        }
        expected["anchor_analysis"] = expected_anchor_analysis
        mock["anchor_analysis"] = mock_anchor_analysis
        if not is_final:
            expected["full_scope_check"] = {
                "checked_evidence_units": required_evidence_units,
                "not_satisfied": [],
                "unverifiable": [],
            }
            mock["full_scope_check"] = {
                "checked_evidence_units": required_evidence_units,
                "not_satisfied": [],
                "unverifiable": [],
            }
        if is_deck_level:
            expected["pages_reviewed"] = canonical_page_numbers
            mock["pages_reviewed"] = canonical_page_numbers
        if criterion_supports_na:
            expected["applicable"] = (
                "boolean; false only when the task requires no external, "
                "attachment, or generated imagery"
            )
            no_image_unit = (
                evaluation_unit is not None
                and evaluation_unit.get("unit_id") == "no_image_tasks"
            )
            mock["applicable"] = not no_image_unit
            if no_image_unit:
                for entry in mock.get("anchor_analysis", []):
                    entry["matches"] = False
    structured_boundaries = [
        item
        for item in criterion.get("scoring", {}).get("hard_boundaries", [])
        if isinstance(item, dict)
    ]
    judge_boundaries = [
        item
        for item in structured_boundaries
        if item.get("trigger", {}).get("type") == "judge_assessment"
    ]
    if is_page_level and structured_boundaries:
        raise DimensionContractError(
            f"page criterion {criterion['id']} does not support hard boundaries"
        )
    if judge_boundaries:
        expected["hard_boundary_assessments"] = [
            {
                "id": item["id"],
                "triggered": "boolean",
                "evidence": [
                    "at least one concrete locator when triggered; otherwise may be empty"
                ],
                "reason": "concise trigger decision",
            }
            for item in judge_boundaries
        ]
        mock["hard_boundary_assessments"] = [
            {
                "id": item["id"],
                "triggered": False,
                "evidence": [],
                "reason": "mock hard-boundary assessment",
            }
            for item in judge_boundaries
        ]
    elif structured_boundaries:
        expected["hard_boundary_assessments"] = []
        mock["hard_boundary_assessments"] = []
    adjustments = criterion.get("scoring", {}).get("adjustments", [])
    if is_page_level and adjustments:
        raise DimensionContractError(
            f"page criterion {criterion['id']} does not support adjustments"
        )
    if adjustments:
        expected["base_score"] = f"one of {profile['allowed_scores']}"
        expected["score"] = (
            "clamp(base_score + sum(delta * applications), 0, 1)"
        )
        expected["applied_adjustments"] = [
            {
                "id": "one declared adjustment id",
                "applications": "integer within max_applications",
                "evidence": ["required locator when evidence_required is true"],
            }
        ]
        mock["base_score"] = mock_score
        mock["applied_adjustments"] = []
    task_id = "case_specific." + str(criterion["id"])
    if evaluation_unit is not None:
        task_id += ".unit_" + str(evaluation_unit["unit_id"])
    if page_number is not None:
        task_id += f".page_{page_number:02d}"
    if not is_final:
        validator = (
            lambda value, c=criterion, p=profile, units=required_evidence_units, supports_na=criterion_supports_na: validate_intermediate_case_result(
                value,
                c,
                p,
                units,
                supports_na=supports_na,
            )
        )
    elif is_page_level:
        assert final_page_index is not None
        assert page_number is not None
        validator = (
            lambda value, c=criterion, p=profile, page=page_number, page_type=final_page_index[str(page_number)]["page_type"]: validate_page_case_unit_result(
                value,
                c,
                p,
                page=page,
                page_type=page_type,
                supports_na=page_supports_na,
            )
        )
    elif is_deck_level:
        validator = (
            lambda value, c=criterion, p=profile, pages=canonical_page_numbers, audits=final_preaudits: validate_final_case_result_from_anchors(
                value,
                c,
                p,
                audits,
                page_numbers=pages,
            )
        )
    else:
        validator = (
            lambda value, c=criterion, p=profile, audits=final_preaudits: validate_final_case_result_from_anchors(
                value,
                c,
                p,
                audits,
            )
        )
    return (
        JudgeTask(
            task_id=task_id,
            kind=(
                "final_case_specific" if is_final else "intermediate_case_specific"
            ),
            judge_role="final" if is_final else "intermediate",
            user_prompt=_json_prompt(
                (
                    "Evaluate only the supplied evaluation unit for this one "
                    "case-specific criterion."
                    if evaluation_unit is not None
                    else "Evaluate this one case-specific criterion only."
                ),
                payload,
                expected,
            ),
            attachment=visual_attachments or None,
            validate=validator,
            mock_response=mock,
            criterion_id=str(criterion["id"]),
            unit_id=(
                str(evaluation_unit["unit_id"])
                if evaluation_unit is not None
                else None
            ),
            unit_label=(
                str(evaluation_unit["unit_label"])
                if evaluation_unit is not None
                else None
            ),
            unit_instruction=(
                str(evaluation_unit["unit_instruction"])
                if evaluation_unit is not None
                else None
            ),
            page_number=page_number,
        ),
        observation,
    )


def final_common_task(
    dimension: dict[str, Any],
    instruction_packet: dict[str, Any],
    final_rubric: dict[str, Any],
    deck_images: list[Path],
    final_preaudits: dict[str, dict[str, Any]],
    final_html_content: str,
) -> JudgeTask:
    module = str(dimension.get("module"))
    preaudit_kind = "content" if module == "content" else "visual"
    frozen_preaudit = final_preaudits.get(preaudit_kind)
    if frozen_preaudit is None:
        raise RunnerError(
            f"final common dimension {dimension.get('id')} requires missing "
            f"{preaudit_kind!r} pre-audit"
        )
    payload = {
        "evaluation_scope": "full rendered deck",
        "rubric_rules": {
            "validity_precheck": final_rubric.get("validity_precheck"),
            "dimension_scale": final_rubric.get("dimension_scale"),
            "strict_scoring_rules": final_rubric.get("strict_scoring_rules"),
            "global_hard_boundaries": final_rubric.get(
                "global_hard_boundaries", []
            ),
        },
        "dimension": dimension,
        "task_context": instruction_packet,
        "evidence_isolation": (
            "The ordered final rendered images are authoritative for actual visibility, "
            "legibility, clipping, and occlusion. "
            + (
                "The validated semantic extraction of final HTML content is supplied "
                "for content grounding; it must not override conflicting rendered evidence. "
                if module == "content"
                else "No HTML-derived content extraction is supplied. "
            )
            + "No raw HTML, plan, research, knowledge brief, asset catalog, trace, "
            "speech, or other generation intermediate artifact may be used."
        ),
        "attachment_note": (
            "Every rendered slide is attached as an individual image. Text labels "
            "SLIDE_XX_OF_YY are interleaved immediately before their images in canonical order."
        ),
        "frozen_final_preaudit": frozen_preaudit,
        "preaudit_usage": (
            "This audit was completed before rubric scoring with scores and anchors "
            "withheld. Address every listed defect and unverifiable point when selecting "
            "the highest fully satisfied anchor; do not silently discard frozen findings."
        ),
    }
    if module == "content":
        payload["final_html_content"] = final_html_content
    expected = {
        "dimension_id": dimension["id"],
        "score": "integer 0, 1, 2, 3, 4, or 5",
        "evidence": ["page/section locator 1", "page/section locator 2"],
        "rationale": "concise highest-fully-satisfied-anchor explanation",
        "defects": [],
    }
    mock = {
        "dimension_id": dimension["id"],
        "score": 3,
        "evidence": ["mock://page/1", "mock://page/2"],
        "rationale": "mock judge response for pipeline validation",
        "defects": [],
    }
    return JudgeTask(
        task_id="final_common." + str(dimension["id"]),
        kind="final_common",
        judge_role="final",
        user_prompt=_json_prompt("Evaluate this one final-output common dimension only.", payload, expected),
        attachment=deck_images,
        validate=lambda value, d=dimension, audit=frozen_preaudit, boundaries=final_rubric.get(
            "global_hard_boundaries", []
        ): validate_final_common_result(
            value,
            d,
            preaudit=audit,
            global_hard_boundaries=boundaries,
        ),
        mock_response=mock,
    )


def run_task(
    task: JudgeTask,
    *,
    output_dir: Path,
    system_prompt: str,
    endpoint: JudgeEndpoint,
    max_tokens: int,
    timeout: int,
    retries: int,
    mock_judge: bool,
    semaphore: threading.Semaphore,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    request_path = output_dir / "requests" / f"{task.task_id}.json"
    write_json(
        request_path,
        {
            "task_id": task.task_id,
            "kind": task.kind,
            "judge_role": task.judge_role,
            "model": endpoint.model,
            "provider": endpoint.provider,
            "attachment": attachment_manifest(task.attachment),
            "system_prompt": system_prompt,
            "user_prompt": task.user_prompt,
            "mock_judge": mock_judge,
        },
    )
    attempts: list[dict[str, Any]] = []
    last_error: Exception | None = None
    validation_feedback = ""
    for attempt_number in range(1, retries + 2):
        started = time.monotonic()
        response_meta: dict[str, Any] = {}
        try:
            if mock_judge:
                raw = json.dumps(task.mock_response, ensure_ascii=False)
                response_meta = {"mock": True, "usage": None}
            else:
                attempt_prompt = task.user_prompt
                if validation_feedback:
                    attempt_prompt += (
                        "\n\nRETRY_VALIDATION_FEEDBACK\n"
                        "The previous response was rejected by the deterministic "
                        "response validator. Correct the response while applying "
                        "the same rubric and evidence; do not defend or repeat the "
                        "invalid structure or inconsistent anchor decision.\n"
                        f"Validator error: {validation_feedback}"
                    )
                with semaphore:
                    raw, response_meta = call_judge(
                        judge_id="dimension",
                        provider=endpoint.provider,
                        model=endpoint.model,
                        base_url=endpoint.base_url,
                        api_key=endpoint.api_key,
                        system_prompt=system_prompt,
                        user_prompt=attempt_prompt,
                        image_path=task.attachment,
                        max_tokens=max_tokens,
                        timeout=timeout,
                        response_schema_template=task.mock_response,
                        response_json_schema=task.response_json_schema,
                    )
            raw_path = output_dir / "raw_responses" / f"{task.task_id}.attempt_{attempt_number}.txt"
            raw_path.write_text(raw, encoding="utf-8")
            try:
                parsed = parse_json_response(raw, task.task_id)
                normalized = task.validate(parsed)
            except (RunnerError, DimensionContractError) as exc:
                if response_indicates_output_truncation(response_meta, max_tokens):
                    raise RunnerError(
                        f"{task.task_id} exhausted the Judge output-token budget"
                    ) from exc
                raise
            attempts.append(
                {
                    "attempt": attempt_number,
                    "duration_seconds": round(time.monotonic() - started, 3),
                    **response_meta,
                }
            )
            write_json(output_dir / "judge_results" / f"{task.task_id}.json", normalized)
            return task.task_id, normalized, {"attempts": attempts, "status": "completed"}
        except (RunnerError, DimensionContractError, JudgeTransportError) as exc:
            last_error = exc
            if isinstance(exc, (RunnerError, DimensionContractError)):
                validation_feedback = str(exc)[:600]
            attempts.append(
                {
                    "attempt": attempt_number,
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "error": str(exc),
                    **response_meta,
                }
            )
            if attempt_number > retries:
                break
            time.sleep(min(2 ** (attempt_number - 1), 8))
    assert last_error is not None
    raise RunnerError(f"{task.task_id} failed after {len(attempts)} attempts: {last_error}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-dir", required=True)
    parser.add_argument("--gen-model", required=True)
    parser.add_argument("--gen-run")
    parser.add_argument("--rubric-revision", required=True)
    parser.add_argument("--evaluation-revision")
    parser.add_argument(
        "--evaluation-scope",
        choices=("all", "final", "intermediate"),
        default="all",
        help="Run both score roles, final-only, or intermediate-only.",
    )
    parser.add_argument(
        "--criterion-id",
        action="append",
        default=[],
        help=(
            "Evaluate only the named criterion. Repeat to select multiple criteria; "
            "omitting this option preserves the full rubric flow."
        ),
    )
    parser.add_argument(
        "--force-rejudge-criterion-id",
        action="append",
        default=[],
        help=(
            "Exclude the named criterion from --reuse-unchanged-evaluation-revision "
            "and evaluate it again. Repeat to select multiple criteria."
        ),
    )
    parser.add_argument("--intermediate-case-rubric")
    parser.add_argument("--final-case-rubric")
    parser.add_argument("--prompt", default=str(DEFAULT_PROMPT))
    parser.add_argument("--output-dir")
    parser.add_argument(
        "--judge-model",
        help=(
            "Default model for both intermediate and final Judges. "
            "Role-specific model options take precedence."
        ),
    )
    parser.add_argument(
        "--intermediate-judge-provider",
        choices=("openai", "anthropic"),
        default="openai",
    )
    parser.add_argument("--intermediate-judge-model")
    parser.add_argument(
        "--intermediate-judge-base-url", default=DEFAULT_OPENAI_BASE_URL
    )
    parser.add_argument("--intermediate-judge-api-key", default="")
    parser.add_argument(
        "--final-judge-provider",
        choices=("openai", "anthropic"),
        default="openai",
    )
    parser.add_argument("--final-judge-model")
    parser.add_argument(
        "--preaudit-judge-provider",
        choices=("openai", "anthropic"),
        default=None,
        help="Provider used only by the optional final pre-audits.",
    )
    parser.add_argument(
        "--preaudit-model",
        "--preaudit-judge-model",
        dest="preaudit_judge_model",
        metavar="PREAUDIT_MODEL",
        default=DEFAULT_PREAUDIT_MODEL,
        help=(
            "Model used only for frozen final content/visual pre-audits."
        ),
    )
    parser.add_argument(
        "--preaudit-judge-base-url",
        default="",
        help="Base URL used only by the optional final pre-audits.",
    )
    parser.add_argument(
        "--use-final-preaudit",
        action="store_true",
        help="Run optional final pre-audits and pass only detected defects to scoring Judges.",
    )
    parser.add_argument(
        "--reuse-final-knowledge-evaluation-revision",
        help=(
            "Skip final_knowledge Judge tasks and reuse their criterion results "
            "from the same model/run/Judge profile under this evaluation revision."
        ),
    )
    parser.add_argument(
        "--reuse-unchanged-evaluation-revision",
        help=(
            "Reuse criterion results from the same model/run/Judge profile only "
            "when the complete criterion definition is identical in the source "
            "and current rubric revisions. With final preaudit enabled, reuse is "
            "limited to Intermediate and final Knowledge criteria."
        ),
    )
    parser.add_argument(
        "--final-judge-base-url", default=DEFAULT_OPENAI_BASE_URL
    )
    parser.add_argument("--final-judge-api-key", default="")
    parser.add_argument("--max-concurrent-judge-requests", type=int, default=4)
    parser.add_argument(
        "--group-min-decay-alpha",
        type=float,
        default=0.3,
        help=(
            "Apply one weakest-small-dimension multiplicative decay to Deck, "
            "and Page. Intermediate is not decayed. 0 disables it; 1 uses "
            "G_min directly."
        ),
    )
    parser.add_argument("--task-retries", type=int, default=3)
    parser.add_argument("--max-tokens", type=int, default=8000)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--mock-judge", action="store_true")
    parser.add_argument(
        "--preaudit-only",
        action="store_true",
        help="Run final content/visual pre-audits, then stop before scoring.",
    )
    parser.add_argument("--allow-recovered-run-for-testing", action="store_true")
    args = parser.parse_args()
    args.intermediate_judge_model = (
        args.intermediate_judge_model
        or args.judge_model
        or DEFAULT_INTERMEDIATE_MODEL
    )
    args.final_judge_model = (
        args.final_judge_model or args.judge_model or DEFAULT_FINAL_MODEL
    )
    return args


def main() -> int:
    args = parse_args()
    include_intermediate = args.evaluation_scope in {"all", "intermediate"}
    include_final = args.evaluation_scope in {"all", "final"}
    if not 0 <= args.group_min_decay_alpha <= 1:
        raise RunnerError("--group-min-decay-alpha must be in [0, 1]")
    if args.preaudit_only and not include_final:
        raise RunnerError("--preaudit-only requires --evaluation-scope all or final")
    if args.use_final_preaudit and not include_final:
        raise RunnerError(
            "--use-final-preaudit requires --evaluation-scope all or final"
        )
    if args.reuse_final_knowledge_evaluation_revision and not include_final:
        raise RunnerError(
            "--reuse-final-knowledge-evaluation-revision requires "
            "--evaluation-scope all or final"
        )
    if (
        args.reuse_final_knowledge_evaluation_revision
        and args.reuse_unchanged_evaluation_revision
    ):
        raise RunnerError(
            "--reuse-final-knowledge-evaluation-revision and "
            "--reuse-unchanged-evaluation-revision are mutually exclusive"
        )
    if args.reuse_unchanged_evaluation_revision and args.preaudit_only:
        raise RunnerError(
            "--reuse-unchanged-evaluation-revision cannot be used with --preaudit-only"
        )
    use_final_preaudit = args.use_final_preaudit or args.preaudit_only
    os.environ["DIMENSION_JUDGE_BOUNDED_IMAGE_MODE"] = "1"
    if (
        args.max_concurrent_judge_requests < 1
        or args.task_retries < 0
        or args.max_tokens < 1
    ):
        raise RunnerError("concurrency/max tokens must be positive and retries non-negative")
    case_dir = resolve(args.case_dir)
    model_dir = case_dir / "outputs" / args.gen_model
    run_dir = choose_run(model_dir, args.gen_run)
    evaluation_revision = args.evaluation_revision or args.rubric_revision
    rubric_dir = case_dir / "rubric_versions" / args.rubric_revision
    intermediate_case_path = (
        resolve(args.intermediate_case_rubric)
        if args.intermediate_case_rubric
        else rubric_dir / "intermediate_case_rubric.json"
    )
    final_case_path = (
        resolve(args.final_case_rubric)
        if args.final_case_rubric
        else rubric_dir / "final_case_rubric.json"
    )
    prompt_path = resolve(args.prompt)
    for path, label in (
        (intermediate_case_path, "intermediate case rubric"),
        (final_case_path, "final case rubric"),
        (prompt_path, "Judge prompt"),
    ):
        require_file(path, label)

    intermediate_case_rubric = load_json(intermediate_case_path)
    final_case_rubric = load_json(final_case_path)
    validate_case_rubric(
        intermediate_case_rubric, "intermediate_case_specific"
    )
    validate_case_rubric(final_case_rubric, "final_case_specific")
    validate_split_case_rubrics(
        intermediate_case_rubric, final_case_rubric
    )
    criterion_filter = frozenset(str(value) for value in args.criterion_id)
    force_rejudge_criterion_ids = frozenset(
        str(value) for value in args.force_rejudge_criterion_id
    )
    if criterion_filter:
        available_criterion_ids = {
            str(criterion["id"])
            for rubric, enabled in (
                (intermediate_case_rubric, include_intermediate),
                (final_case_rubric, include_final),
            )
            if enabled
            for criterion in rubric["criteria"]
        }
        unknown_criterion_ids = sorted(
            criterion_filter - available_criterion_ids
        )
        if unknown_criterion_ids:
            raise RunnerError(
                "--criterion-id is outside the selected evaluation scope: "
                + ", ".join(unknown_criterion_ids)
            )
        if args.preaudit_only:
            raise RunnerError("--criterion-id cannot be combined with --preaudit-only")
    if force_rejudge_criterion_ids:
        if not args.reuse_unchanged_evaluation_revision:
            raise RunnerError(
                "--force-rejudge-criterion-id requires "
                "--reuse-unchanged-evaluation-revision"
            )
        available_criterion_ids = {
            str(criterion["id"])
            for rubric, enabled in (
                (intermediate_case_rubric, include_intermediate),
                (final_case_rubric, include_final),
            )
            if enabled
            for criterion in rubric["criteria"]
        }
        unknown_criterion_ids = sorted(
            force_rejudge_criterion_ids - available_criterion_ids
        )
        if unknown_criterion_ids:
            raise RunnerError(
                "--force-rejudge-criterion-id is outside the selected evaluation "
                "scope: " + ", ".join(unknown_criterion_ids)
            )
    generation = generation_status(run_dir)
    benchmark_valid = generation["benchmark_pass"] is True
    if not benchmark_valid and not args.allow_recovered_run_for_testing:
        raise RunnerError(
            "generation run is not a successful benchmark run; refusing to judge. "
            "Use --allow-recovered-run-for-testing only for pipeline validation. "
            f"status={generation}"
        )
    if args.allow_recovered_run_for_testing and benchmark_valid:
        args.allow_recovered_run_for_testing = False

    slides = discover_slide_artifacts(run_dir)
    visual_eligibility = assess_multi_generation_eligibility(run_dir, slides)
    if visual_eligibility.get("eligible") is not True:
        raise RunnerError(f"multi-page visual artifacts are not eligible: {visual_eligibility}")

    if args.evaluation_scope == "all":
        judge_component = (
            sanitized_component(args.intermediate_judge_model)
            + "__"
            + sanitized_component(args.final_judge_model)
            + "__"
            + (
                sanitized_component(args.preaudit_judge_model)
                if use_final_preaudit
                else "no-preaudit"
            )
        )
    else:
        selected_model = (
            args.intermediate_judge_model
            if include_intermediate
            else args.final_judge_model
        )
        judge_component = (
            sanitized_component(selected_model)
            + "__"
            + args.evaluation_scope
            + "-only"
            + (
                "__" + sanitized_component(args.preaudit_judge_model)
                if use_final_preaudit
                else ""
            )
        )
    if args.output_dir:
        output_dir = resolve(args.output_dir)
    else:
        output_dir = (
            case_dir
            / "evaluations"
            / evaluation_revision
            / args.gen_model
            / run_dir.name
            / judge_component
        )
    reused_final_knowledge_source: Path | None = None
    if args.reuse_final_knowledge_evaluation_revision:
        reuse_judge_component = judge_component
        if use_final_preaudit:
            if args.evaluation_scope == "all":
                reuse_judge_component = (
                    sanitized_component(args.intermediate_judge_model)
                    + "__"
                    + sanitized_component(args.final_judge_model)
                    + "__no-preaudit"
                )
            else:
                selected_model = (
                    args.intermediate_judge_model
                    if include_intermediate
                    else args.final_judge_model
                )
                reuse_judge_component = (
                    sanitized_component(selected_model)
                    + "__"
                    + args.evaluation_scope
                    + "-only"
                )
        reused_final_knowledge_source = (
            case_dir
            / "evaluations"
            / args.reuse_final_knowledge_evaluation_revision
            / args.gen_model
            / run_dir.name
            / reuse_judge_component
            / "score_result.json"
        )
    reused_unchanged_source: Path | None = None
    if args.reuse_unchanged_evaluation_revision:
        reuse_judge_component = judge_component
        if use_final_preaudit:
            if args.evaluation_scope == "all":
                reuse_judge_component = (
                    sanitized_component(args.intermediate_judge_model)
                    + "__"
                    + sanitized_component(args.final_judge_model)
                    + "__no-preaudit"
                )
            else:
                selected_model = (
                    args.intermediate_judge_model
                    if include_intermediate
                    else args.final_judge_model
                )
                reuse_judge_component = (
                    sanitized_component(selected_model)
                    + "__"
                    + args.evaluation_scope
                    + "-only"
                )
        reused_unchanged_source = (
            case_dir
            / "evaluations"
            / args.reuse_unchanged_evaluation_revision
            / args.gen_model
            / run_dir.name
            / reuse_judge_component
            / "score_result.json"
        )
    protected_paths = {
        ROOT.resolve(),
        case_dir.resolve(),
        model_dir.resolve(),
        run_dir.resolve(),
        (case_dir / "outputs").resolve(),
        (case_dir / "evaluations").resolve(),
    }
    if output_dir.resolve() in protected_paths:
        raise RunnerError(f"refusing to use a protected directory as --output-dir: {output_dir}")
    if args.overwrite and args.resume:
        raise RunnerError("--overwrite and --resume are mutually exclusive")
    prepare_output(output_dir, args.overwrite, args.resume)
    started_at = utc_now()
    started = time.monotonic()
    system_prompt = prompt_path.read_text(encoding="utf-8")

    deck_images = [slide.image_path for slide in slides]

    instruction_files, instruction_missing = expand_evidence_patterns(
        ["{CASE_DIR}/instruction.md"], case_dir, run_dir
    )
    instruction_packet = build_evidence_packet(
        instruction_files, instruction_missing, case_dir, run_dir, total_chars=80_000
    )

    page_numbers = [int(slide.index) for slide in slides]
    final_html_content_dir = final_html_content_output_dir(run_dir)
    final_html_content_file = final_html_content_dir / "content.md"
    final_page_index_file = final_html_content_dir / "page_index.json"
    final_html_content: str | None = None
    final_html_pages: dict[int, str] | None = None
    final_page_index: dict[str, dict[str, str]] | None = None
    final_html_content_path: str | None = None
    final_page_index_path: str | None = None
    if include_final:
        require_file(final_html_content_file, "shared final HTML content extraction")
        require_file(final_page_index_file, "shared final HTML page index")
        final_html_content = validate_final_html_content_markdown(
            final_html_content_file.read_text(encoding="utf-8"),
            page_numbers=page_numbers,
        )
        final_html_pages = split_final_html_content_by_page(
            final_html_content,
            page_numbers=page_numbers,
        )
        final_page_index = validate_final_page_index(
            load_json(final_page_index_file),
            page_numbers=page_numbers,
        )
        final_html_content_path = str(final_html_content_file)
        final_page_index_path = str(final_page_index_file)

    reused_final_knowledge_results: list[dict[str, Any]] = []
    if reused_final_knowledge_source is not None:
        reused_final_knowledge_results = load_reused_final_knowledge_results(
            reused_final_knowledge_source,
            final_case_rubric,
            generation_model=args.gen_model,
            generation_run=run_dir.name,
        )

    intermediate_api_key = (
        args.intermediate_judge_api_key
        or os.environ.get("INTERMEDIATE_JUDGE_API_KEY", "")
        or (
            os.environ.get("ANTHROPIC_API_KEY", "")
            if args.intermediate_judge_provider == "anthropic"
            else (
                os.environ.get("GEMINI_API_KEY", "")
                or os.environ.get("OPENAI_API_KEY", "")
            )
        )
    )
    final_api_key = (
        args.final_judge_api_key
        or os.environ.get("FINAL_JUDGE_API_KEY", "")
        or (
            os.environ.get("ANTHROPIC_API_KEY", "")
            if args.final_judge_provider == "anthropic"
            else (
                os.environ.get("GEMINI_API_KEY", "")
                or os.environ.get("OPENAI_API_KEY", "")
            )
        )
    )
    endpoints = {
        "intermediate": JudgeEndpoint(
            provider=args.intermediate_judge_provider,
            model=args.intermediate_judge_model,
            base_url=args.intermediate_judge_base_url,
            api_key=intermediate_api_key,
        ),
        "final": JudgeEndpoint(
            provider=args.final_judge_provider,
            model=args.final_judge_model,
            base_url=args.final_judge_base_url,
            api_key=final_api_key,
        ),
    }
    preaudit_api_key = (
        os.environ.get("PREAUDIT_JUDGE_API_KEY", "") or final_api_key
    )
    preaudit_endpoint = JudgeEndpoint(
        provider=args.preaudit_judge_provider or args.final_judge_provider,
        model=args.preaudit_judge_model,
        base_url=args.preaudit_judge_base_url or args.final_judge_base_url,
        api_key=preaudit_api_key,
    )
    selected_roles = tuple(
        role
        for role, enabled in (
            ("intermediate", include_intermediate),
            ("final", include_final),
        )
        if enabled
    )
    judge_descriptions = {
        role: endpoints[role]
        for role in selected_roles
    }
    if use_final_preaudit:
        judge_descriptions["preaudit"] = preaudit_endpoint
    if not args.mock_judge and not args.dry_run:
        required_roles = ("final",) if args.preaudit_only else selected_roles
        missing_roles = [
            role for role in required_roles if not endpoints[role].api_key
        ]
        if missing_roles:
            raise RunnerError(
                f"Judge API key is missing for roles: {', '.join(missing_roles)}"
            )

    preaudit_tasks = (
        [
            final_preaudit_task(
                audit_type,
                instruction_packet=instruction_packet,
                deck_images=deck_images,
                html_content=(
                    final_html_content if audit_type == "content" else None
                ),
            )
            for audit_type in ("content", "visual")
        ]
        if use_final_preaudit
        else []
    )
    final_preaudits: dict[str, dict[str, Any]] = {}
    preaudit_config = (
        {
            "html_content_sha256": sha256_file(final_html_content_file),
            "page_index_sha256": sha256_file(final_page_index_file),
            "page_index_page_count": len(final_page_index or {}),
            "judge_provider": preaudit_endpoint.provider,
            "judge_model": preaudit_endpoint.model,
            "judge_base_url": preaudit_endpoint.base_url,
        }
        if use_final_preaudit
        else {}
    )
    preaudit_metadata: dict[str, Any] = (
        {"_config": preaudit_config} if use_final_preaudit else {}
    )
    preaudit_metadata_path = output_dir / "preaudits" / "preaudit_metadata.json"
    reusable_preaudit_config = False
    if use_final_preaudit and args.resume and preaudit_metadata_path.is_file():
        loaded_preaudit_metadata = load_json(preaudit_metadata_path)
        if loaded_preaudit_metadata.get("_config") == preaudit_config:
            reusable_preaudit_config = True
            preaudit_metadata.update(loaded_preaudit_metadata)
    preaudit_semaphore = threading.Semaphore(
        min(2, args.max_concurrent_judge_requests)
    )
    if args.dry_run:
        for task in preaudit_tasks:
            write_json(
                output_dir / "requests" / f"{task.task_id}.json",
                {
                    "task_id": task.task_id,
                    "kind": task.kind,
                    "judge_role": task.judge_role,
                    "model": preaudit_endpoint.model,
                    "provider": preaudit_endpoint.provider,
                    "attachment": attachment_manifest(task.attachment),
                    "system_prompt": PREAUDIT_SYSTEM_PROMPT,
                    "user_prompt": task.user_prompt,
                    "dry_run": True,
                },
            )
            audit_type = task.task_id.rsplit(".", 1)[1]
            result = task.validate(task.mock_response)
            final_preaudits[audit_type] = result
            write_json(
                output_dir / "preaudits" / f"final_{audit_type}_audit.mock.json",
                result,
            )
    else:
        reusable_preaudits: dict[str, dict[str, Any]] = {}
        if args.resume and reusable_preaudit_config:
            for task in preaudit_tasks:
                audit_type = task.task_id.rsplit(".", 1)[1]
                result_path = (
                    output_dir / "preaudits" / f"final_{audit_type}_audit.json"
                )
                if result_path.is_file():
                    reusable_preaudits[task.task_id] = task.validate(
                        load_json(result_path)
                    )
        pending_preaudits = [
            task for task in preaudit_tasks if task.task_id not in reusable_preaudits
        ]
        final_preaudits.update(
            {
                task_id.rsplit(".", 1)[1]: result
                for task_id, result in reusable_preaudits.items()
            }
        )
        if reusable_preaudits:
            print(f"resume: restored {len(reusable_preaudits)}/2 final pre-audits")
        with cf.ThreadPoolExecutor(max_workers=max(1, len(pending_preaudits))) as executor:
            future_map = {
                executor.submit(
                    run_task,
                    task,
                    output_dir=output_dir,
                    system_prompt=PREAUDIT_SYSTEM_PROMPT,
                    endpoint=preaudit_endpoint,
                    max_tokens=args.max_tokens,
                    timeout=args.timeout,
                    retries=args.task_retries,
                    mock_judge=args.mock_judge,
                    semaphore=preaudit_semaphore,
                ): task
                for task in pending_preaudits
            }
            for future in cf.as_completed(future_map):
                task = future_map[future]
                returned_id, result, task_meta = future.result()
                audit_type = returned_id.rsplit(".", 1)[1]
                final_preaudits[audit_type] = result
                preaudit_metadata[returned_id] = task_meta
                write_json(
                    output_dir / "preaudits" / f"final_{audit_type}_audit.json",
                    result,
                )
                print(f"completed: {returned_id}")
        if use_final_preaudit:
            write_json(preaudit_metadata_path, preaudit_metadata)

    if args.preaudit_only:
        summary = {
            "contract_version": FINAL_PREAUDIT_CONTRACT_VERSION,
            "case_id": final_case_rubric["case_id"],
            "generation_model": args.gen_model,
            "generation_run": run_dir.name,
            "judge_model": args.preaudit_judge_model,
            "html_content": final_html_content_path,
            "page_index": final_page_index_path,
            "content_audit": (
                "preaudits/final_content_audit.mock.json"
                if args.dry_run
                else "preaudits/final_content_audit.json"
            ),
            "visual_audit": (
                "preaudits/final_visual_audit.mock.json"
                if args.dry_run
                else "preaudits/final_visual_audit.json"
            ),
            "test_only": args.mock_judge or args.dry_run,
        }
        write_json(output_dir / "preaudits" / "preaudit_result.json", summary)
        print(f"preaudit result: {output_dir / 'preaudits' / 'preaudit_result.json'}")
        return 0

    deterministic_case: list[dict[str, Any]] = []
    hybrid_observations: dict[str, Any] = {}
    tasks: list[JudgeTask] = []
    selected_rubrics = tuple(
        rubric
        for rubric, enabled in (
            (intermediate_case_rubric, include_intermediate),
            (final_case_rubric, include_final),
        )
        if enabled
    )
    reused_unchanged_results: list[dict[str, Any]] = []
    reused_unchanged_ids: frozenset[str] = frozenset()
    if reused_unchanged_source is not None:
        reused_unchanged_results, reused_unchanged_ids = (
            load_reused_unchanged_criterion_results(
                reused_unchanged_source,
                selected_rubrics,
                case_dir=case_dir,
                generation_model=args.gen_model,
                generation_run=run_dir.name,
                judge_models={
                    **(
                        {"intermediate": args.intermediate_judge_model}
                        if include_intermediate
                        else {}
                    ),
                    **(
                        {"final": args.final_judge_model}
                        if include_final
                        else {}
                    ),
                },
                criterion_filter=criterion_filter,
                force_rejudge_criterion_ids=force_rejudge_criterion_ids,
                preaudit_enabled=use_final_preaudit,
            )
        )
        print(
            "reuse: restored "
            f"{len(reused_unchanged_results)} unchanged criterion results from "
            f"{args.reuse_unchanged_evaluation_revision}"
        )
    for role_rubric in selected_rubrics:
        for criterion in role_rubric["criteria"]:
            if criterion_filter and str(criterion["id"]) not in criterion_filter:
                continue
            if (
                reused_final_knowledge_source is not None
                and criterion.get("group") == "final_knowledge"
            ):
                continue
            if str(criterion["id"]) in reused_unchanged_ids:
                continue
            if criterion.get("mode") == "deterministic":
                try:
                    result = evaluate_case_criterion(
                        str(role_rubric["case_id"]), criterion, run_dir, case_dir
                    )
                except DeterministicEvaluationError as exc:
                    raise RunnerError(str(exc)) from exc
                normalized = {
                    "criterion_id": criterion["id"],
                    "score": result["score"],
                    "score_profile": criterion["scoring"]["profile"],
                    "evidence": result["evidence"],
                    "reason": result["reason"],
                    "missing_or_unverifiable": [],
                    "evaluator": "deterministic",
                    "raw_observation": result["raw_observation"],
                    "group": criterion["group"],
                    **(
                        {"dimension_id": criterion["dimension_id"]}
                        if "dimension_id" in criterion
                        else {}
                    ),
                    **(
                        {"defect_level": criterion["defect_level"]}
                        if "defect_level" in criterion
                        else {}
                    ),
                    "preaudit": (
                        criterion_preaudit_selection(criterion)
                        if use_final_preaudit
                        and str(criterion.get("group", "")) != "final_knowledge"
                        else None
                    ),
                }
                deterministic_case.append(normalized)
                write_json(
                    output_dir
                    / "deterministic"
                    / f"case_specific.{criterion['id']}.json",
                    normalized,
                )
                continue
            evaluation_units = (
                intermediate_evaluation_units(
                    criterion, run_dir, page_numbers
                )
                if role_rubric["rubric_role"] == "intermediate_case_specific"
                else []
            )
            units_to_build: list[tuple[dict[str, Any] | None, int | None]] = (
                [(unit, None) for unit in evaluation_units]
                if evaluation_units
                else (
                    [(None, page) for page in page_numbers]
                    if criterion.get("group") == "final_page_level"
                    else [(None, None)]
                )
            )
            for evaluation_unit, page_number in units_to_build:
                task, observation = case_task(
                    role_rubric,
                    criterion,
                    case_dir,
                    run_dir,
                    deck_images,
                    final_preaudits,
                    final_html_content,
                    final_page_index,
                    evaluation_unit,
                    page_number,
                    (
                        final_html_pages[page_number]
                        if final_html_pages is not None
                        and page_number is not None
                        else None
                    ),
                )
                tasks.append(task)
                if observation is not None:
                    hybrid_observations[str(criterion["id"])] = observation
                    write_json(
                        output_dir
                        / "deterministic"
                        / f"observation.{criterion['id']}.json",
                        observation,
                    )
    reused_task_results: dict[str, dict[str, Any]] = {}
    reused_task_metadata: dict[str, dict[str, Any]] = {}
    if reused_unchanged_source is not None and use_final_preaudit:
        reused_task_results, reused_task_metadata = (
            load_reused_unchanged_task_results(
                reused_unchanged_source,
                tasks,
                endpoints,
                system_prompt=system_prompt,
            )
        )
        print(
            "reuse: restored "
            f"{len(reused_task_results)}/{len(tasks)} unchanged Judge units"
        )
    fingerprint_payload = {
        "contract_version": RUN_CONTRACT_VERSION,
        "evaluation_scope": args.evaluation_scope,
        "criterion_filter": sorted(criterion_filter),
        "force_rejudge_criterion_ids": sorted(force_rejudge_criterion_ids),
        "case_id": intermediate_case_rubric["case_id"],
        "generation_run": str(run_dir.resolve()),
        "rubrics": {
            str(path.resolve()): sha256_file(path)
            for path in (
                *(
                    (intermediate_case_path,)
                    if include_intermediate
                    else ()
                ),
                *((final_case_path,) if include_final else ()),
                prompt_path,
            )
        },
        "judges": {
            role: {
                "provider": endpoint.provider,
                "model": endpoint.model,
                "base_url": endpoint.base_url,
            }
            for role, endpoint in judge_descriptions.items()
        },
        "visual_encoding": {
            "format": "JPEG",
            "max_dimension": JUDGE_IMAGE_MAX_DIMENSION,
            "quality": JUDGE_IMAGE_JPEG_QUALITY,
        },
        "group_minimum_decay": {
            "alpha": args.group_min_decay_alpha,
            "formula": "1 - alpha * (1 - G_min)",
            "combine": "once_per_large_dimension",
            "applied_to": ["Deck", "Page"],
        },
        "final_html_content_sha256": (
            hashlib.sha256(
                json.dumps(
                    final_html_content,
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            if include_final
            else None
        ),
        "final_page_index_sha256": (
            hashlib.sha256(
                json.dumps(
                    final_page_index,
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            if include_final
            else None
        ),
        "reused_final_knowledge": (
            {
                "source": str(reused_final_knowledge_source),
                "sha256": sha256_file(reused_final_knowledge_source),
                "criterion_count": len(reused_final_knowledge_results),
            }
            if reused_final_knowledge_source is not None
            else None
        ),
        "reused_unchanged_criteria": (
            {
                "source": str(reused_unchanged_source),
                "sha256": sha256_file(reused_unchanged_source),
                "criterion_ids": sorted(reused_unchanged_ids),
            }
            if reused_unchanged_source is not None
            else None
        ),
        "reused_unchanged_tasks": (
            {
                "source_checkpoint_sha256": sha256_file(
                    reused_unchanged_source.parent / ".checkpoint.json"
                ),
                "task_ids": sorted(reused_task_results),
            }
            if reused_task_results
            else None
        ),
        "final_preaudits": {
            audit_type: hashlib.sha256(
                json.dumps(
                    audit,
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            for audit_type, audit in sorted(final_preaudits.items())
        },
        "tasks": [
            {
                "task_id": task.task_id,
                "judge_role": task.judge_role,
                "prompt_sha256": hashlib.sha256(
                    task.user_prompt.encode("utf-8")
                ).hexdigest(),
            }
            for task in tasks
        ],
    }
    evaluation_fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
    ).hexdigest()
    run_config = {
        "contract_version": RUN_CONTRACT_VERSION,
        "evaluation_fingerprint": evaluation_fingerprint,
        "evaluation_scope": args.evaluation_scope,
        "criterion_filter": sorted(criterion_filter),
        "force_rejudge_criterion_ids": sorted(force_rejudge_criterion_ids),
        "case_id": intermediate_case_rubric["case_id"],
        "case_dir": str(case_dir),
        "generation_model": args.gen_model,
        "generation_run": run_dir.name,
        "generation_status": generation,
        "benchmark_valid": benchmark_valid,
        "test_only_recovered_run": not benchmark_valid,
        "rubric_revision": args.rubric_revision,
        "evaluation_revision": evaluation_revision,
        "rubric_ids": {
            **(
                {
                    "intermediate_case_specific": intermediate_case_rubric["rubric_id"]
                }
                if include_intermediate
                else {}
            ),
            **(
                {"final_case_specific": final_case_rubric["rubric_id"]}
                if include_final
                else {}
            ),
        },
        "intermediate_case_rubric": str(intermediate_case_path),
        "final_case_rubric": str(final_case_path),
        "judges": {
            role: {
                "provider": endpoint.provider,
                "model": endpoint.model,
                "base_url": endpoint.base_url,
            }
            for role, endpoint in judge_descriptions.items()
        },
        "mock_judge": args.mock_judge,
        "resume": args.resume,
        "max_concurrent_judge_requests": args.max_concurrent_judge_requests,
        "group_minimum_decay": {
            "alpha": args.group_min_decay_alpha,
            "formula": "1 - alpha * (1 - G_min)",
            "combine": "once_per_large_dimension",
            "applied_to": ["Deck", "Page"],
        },
        "visual_encoding": {
            "format": "JPEG",
            "max_dimension": JUDGE_IMAGE_MAX_DIMENSION,
            "quality": JUDGE_IMAGE_JPEG_QUALITY,
        },
        "preaudit_count": len(preaudit_tasks),
        "preaudit_ids": [task.task_id for task in preaudit_tasks],
        "scoring_task_count": len(tasks),
        "task_count": len(preaudit_tasks) + len(tasks),
        "task_ids": [task.task_id for task in tasks],
        "final_preaudits": {
            "enabled": use_final_preaudit,
            "judge_model": (
                args.preaudit_judge_model if use_final_preaudit else None
            ),
            "html_content": final_html_content_path,
            "page_index": final_page_index_path,
            "content": (
                (
                    "preaudits/final_content_audit.mock.json"
                    if args.dry_run
                    else "preaudits/final_content_audit.json"
                )
                if use_final_preaudit
                else None
            ),
            "visual": (
                (
                    "preaudits/final_visual_audit.mock.json"
                    if args.dry_run
                    else "preaudits/final_visual_audit.json"
                )
                if use_final_preaudit
                else None
            ),
            "scoring_information_withheld": use_final_preaudit,
            "frozen_for_downstream_final_scoring": use_final_preaudit,
        },
        "reused_final_knowledge": (
            {
                "source": str(reused_final_knowledge_source),
                "criterion_count": len(reused_final_knowledge_results),
            }
            if reused_final_knowledge_source is not None
            else None
        ),
        "reused_unchanged_task_count": len(reused_task_results),
        "score_roles": list(selected_roles),
        "two_scores_only": args.evaluation_scope == "all",
    }
    write_json(output_dir / "run_config.json", run_config)
    if args.dry_run:
        for task in tasks:
            write_json(
                output_dir / "requests" / f"{task.task_id}.json",
                {
                    "task_id": task.task_id,
                    "kind": task.kind,
                    "judge_role": task.judge_role,
                    "model": endpoints[task.judge_role].model,
                    "provider": endpoints[task.judge_role].provider,
                    "attachment": attachment_manifest(task.attachment),
                    "system_prompt": system_prompt,
                    "user_prompt": task.user_prompt,
                },
            )
        print(
            f"dry-run prepared {len(preaudit_tasks)} pre-audits and "
            f"{len(tasks)} scoring tasks in {output_dir}"
        )
        return 0

    results: dict[str, dict[str, Any]] = dict(reused_task_results)
    metadata: dict[str, Any] = {
        **preaudit_metadata,
        **reused_task_metadata,
    }
    checkpoint_path = output_dir / ".checkpoint.json"
    checkpoint_lock = threading.Lock()
    if args.resume and checkpoint_path.is_file():
        checkpoint = load_json(checkpoint_path)
        if checkpoint.get("contract_version") != CHECKPOINT_CONTRACT_VERSION:
            raise RunnerError("checkpoint contract version is unsupported")
        if checkpoint.get("evaluation_fingerprint") != evaluation_fingerprint:
            raise RunnerError(
                "checkpoint does not match the current run, rubrics, prompts, or judges; "
                "use --overwrite for a fresh evaluation"
            )
        completed = checkpoint.get("completed", {})
        if not isinstance(completed, dict):
            raise RunnerError("checkpoint completed field must be an object")
        task_by_id = {task.task_id: task for task in tasks}
        for task_id, cached in completed.items():
            task = task_by_id.get(task_id)
            if task is None or not isinstance(cached, dict):
                raise RunnerError(f"checkpoint contains invalid task {task_id}")
            value = cached.get("result")
            if not isinstance(value, dict):
                raise RunnerError(f"checkpoint task {task_id} has no result")
            results[task_id] = task.validate(
                checkpoint_value_for_validation(task, value)
            )
            task_meta = cached.get("metadata", {})
            metadata[task_id] = (
                task_meta if isinstance(task_meta, dict) else {}
            )
        print(
            f"resume: restored {len(results)}/{len(tasks)} completed Judge tasks"
        )

    def save_checkpoint(status: str) -> None:
        with checkpoint_lock:
            write_json(
                checkpoint_path,
                {
                    "contract_version": CHECKPOINT_CONTRACT_VERSION,
                    "evaluation_fingerprint": evaluation_fingerprint,
                    "status": status,
                    "updated_at": utc_now(),
                    "completed": {
                        task_id: {
                            "result": results[task_id],
                            "metadata": metadata.get(task_id, {}),
                        }
                        for task_id in sorted(results)
                    },
                },
            )

    pending_tasks = [task for task in tasks if task.task_id not in results]
    if args.resume:
        save_checkpoint("running")
    semaphore = threading.Semaphore(args.max_concurrent_judge_requests)
    with cf.ThreadPoolExecutor(max_workers=args.max_concurrent_judge_requests) as executor:
        future_map = {
            executor.submit(
                run_task,
                task,
                output_dir=output_dir,
                system_prompt=system_prompt,
                endpoint=endpoints[task.judge_role],
                max_tokens=args.max_tokens,
                timeout=args.timeout,
                retries=args.task_retries,
                mock_judge=args.mock_judge,
                semaphore=semaphore,
            ): task.task_id
            for task in pending_tasks
        }
        for future in cf.as_completed(future_map):
            task_id = future_map[future]
            try:
                returned_id, result, task_meta = future.result()
            except Exception as exc:
                for pending in future_map:
                    pending.cancel()
                raise RunnerError(f"Judge task failed: {task_id}: {exc}") from exc
            results[returned_id] = result
            metadata[returned_id] = task_meta
            if args.resume:
                save_checkpoint("running")
            print(f"completed: {returned_id}")

    case_results: list[dict[str, Any]] = [
        *deterministic_case,
        *reused_final_knowledge_results,
        *reused_unchanged_results,
    ]
    criterion_by_id = {
        str(item["id"]): item
        for rubric in selected_rubrics
        for item in rubric["criteria"]
    }
    rubric_by_criterion_id = {
        str(item["id"]): rubric
        for rubric in selected_rubrics
        for item in rubric["criteria"]
    }
    task_groups: dict[str, list[JudgeTask]] = defaultdict(list)
    for task in tasks:
        if task.kind in {"intermediate_case_specific", "final_case_specific"}:
            if task.criterion_id is None:
                raise RunnerError(f"case task {task.task_id} has no criterion id")
            task_groups[task.criterion_id].append(task)
    for criterion_id, criterion_tasks in task_groups.items():
        criterion = criterion_by_id[criterion_id]
        if any(task.page_number is not None for task in criterion_tasks):
            if not all(task.page_number is not None for task in criterion_tasks):
                raise RunnerError(
                    f"criterion {criterion_id} mixes page and whole-criterion tasks"
                )
            if final_page_index is None:
                raise RunnerError(
                    f"criterion {criterion_id} cannot aggregate without page index"
                )
            result = aggregate_page_case_unit_results(
                criterion,
                case_profile(rubric_by_criterion_id[criterion_id], criterion),
                final_page_index,
                [results[task.task_id] for task in criterion_tasks],
                round_to=int(
                    rubric_by_criterion_id[criterion_id]["score_contract"][
                        "round_to"
                    ]
                ),
                supports_na=criterion.get("supports_na") is True,
            )
        elif any(task.unit_id is not None for task in criterion_tasks):
            if not all(task.unit_id is not None for task in criterion_tasks):
                raise RunnerError(
                    f"criterion {criterion_id} mixes unit and whole-criterion tasks"
                )
            unit_entries = [
                {
                    "unit_id": str(task.unit_id),
                    "unit_label": str(task.unit_label),
                    "unit_instruction": str(task.unit_instruction),
                    "result": results[task.task_id],
                }
                for task in criterion_tasks
            ]
            result = aggregate_intermediate_unit_results(
                criterion,
                unit_entries,
                allowed_scores=case_profile(
                    rubric_by_criterion_id[criterion_id], criterion
                )["allowed_scores"],
                round_to=int(
                    rubric_by_criterion_id[criterion_id]["score_contract"][
                        "round_to"
                    ]
                ),
            )
        else:
            if len(criterion_tasks) != 1:
                raise RunnerError(
                    f"criterion {criterion_id} expected one Judge task"
                )
            result = results[criterion_tasks[0].task_id]
        case_results.append(
            {
                **result,
                "group": criterion["group"],
                **(
                    {"dimension_id": criterion["dimension_id"]}
                    if "dimension_id" in criterion
                    else {}
                ),
                **(
                    {"defect_level": criterion["defect_level"]}
                    if "defect_level" in criterion
                    else {}
                ),
                "mode": criterion["mode"],
                "preaudit": (
                    criterion_preaudit_selection(criterion)
                    if final_preaudits
                    and str(criterion.get("group", "")).startswith("final_")
                    and str(criterion.get("group", "")) != "final_knowledge"
                    else None
                ),
                "deterministic_observation": hybrid_observations.get(criterion_id),
            }
        )
    case_results.sort(key=lambda item: item["criterion_id"])
    scores = aggregate_scores(
        case_results,
        final_aggregation=final_case_rubric["score_contract"]["aggregation"],
        intermediate_aggregation=(
            intermediate_case_rubric["score_contract"]["aggregation"]
        ),
        final_dimensions=final_case_rubric.get("dimensions"),
        intermediate_dimensions=intermediate_case_rubric.get("dimensions"),
        round_to=int(intermediate_case_rubric["score_contract"]["round_to"]),
    )
    scores = apply_defect_policies(
        scores,
        case_results,
        selected_rubrics,
        round_to=int(intermediate_case_rubric["score_contract"]["round_to"]),
    )
    scores = nest_final_knowledge_under_deck(
        scores,
        round_to=int(intermediate_case_rubric["score_contract"]["round_to"]),
    )
    scores = apply_group_minimum_decay(
        scores,
        selected_rubrics,
        alpha=args.group_min_decay_alpha,
        round_to=int(intermediate_case_rubric["score_contract"]["round_to"]),
    )
    finished_at = utc_now()
    duration = time.monotonic() - started
    score_result = {
        "contract_version": SCORE_CONTRACT_VERSION,
        "evaluation_scope": args.evaluation_scope,
        "evaluation_status": (
            "test_only"
            if not benchmark_valid or args.mock_judge
            else "completed"
        ),
        "benchmark_eligible": benchmark_valid and not args.mock_judge,
        "case_id": intermediate_case_rubric["case_id"],
        "generation_model": args.gen_model,
        "generation_run": run_dir.name,
        "rubric_revision": args.rubric_revision,
        "evaluation_revision": evaluation_revision,
        "rubric_ids": run_config["rubric_ids"],
        "judge_models": {
            **(
                {"intermediate": args.intermediate_judge_model}
                if include_intermediate
                else {}
            ),
            **({"final": args.final_judge_model} if include_final else {}),
            **(
                {
                    "preaudit": (
                        args.preaudit_judge_model
                        if use_final_preaudit
                        else None
                    )
                }
                if include_final
                else {}
            ),
        },
        "scores": scores,
        "final_preaudits": final_preaudits,
        "details": {
            **(
                {
                    "final_output_case_specific": [
                        item
                        for item in case_results
                        if item["group"].startswith("final_")
                    ]
                }
                if include_final
                else {}
            ),
            **(
                {
                    "intermediate_case_specific": [
                        item
                        for item in case_results
                        if item["group"].startswith("intermediate_")
                    ]
                }
                if include_intermediate
                else {}
            ),
        },
        "metrics": {
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": round(duration, 3),
            "token_usage": aggregate_judge_token_usage(metadata),
        },
    }
    write_json(output_dir / "judge_metadata.json", metadata)
    write_json(output_dir / "score_result.json", score_result)
    if args.resume:
        save_checkpoint("completed")
    print(json.dumps(scores, ensure_ascii=False, indent=2))
    print(f"result: {output_dir / 'score_result.json'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RunnerError, DimensionContractError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
