#!/usr/bin/env python3
"""Validate a supported case-rubric JSON file and render it as YAML."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml


CONTRACT_VERSION_V1 = "case_special_rubric_v1"
CONTRACT_VERSION_V2 = "case_special_rubric_v2"
AGENTIC_SCHEMA_VERSION_V1 = "long_horizon_agentic_case_rubric_v1"
# Kept as the v1 alias for callers that import the historical constant.
CONTRACT_VERSION = CONTRACT_VERSION_V1
CONTRACT_VERSIONS = {CONTRACT_VERSION_V1, CONTRACT_VERSION_V2}
TOP_LEVEL_FIELDS = {
    "contract_version",
    "case_revision",
    "case_id",
    "instruction",
    "case_scale",
    "review",
    "criteria",
}
CRITERION_REQUIRED_FIELDS = {"name", "evaluation_mode", "requirement", "evaluation", "anchors"}
CRITERION_OPTIONAL_FIELDS = {"criticality"}
V2_CRITERION_REQUIRED_FIELDS = CRITERION_REQUIRED_FIELDS | {"evaluation_scope"}
V2_CRITERION_OPTIONAL_FIELDS = CRITERION_OPTIONAL_FIELDS | {"page_selector", "requirement_refs"}
ANCHOR_LEVELS = ("not_met", "weak", "partial", "mostly", "fully")
EVALUATION_MODES = {"visual", "html"}
EVALUATION_SCOPES = {"page", "deck"}
PAGE_SELECTOR_MODES = {"first", "last", "all", "indices"}
AGENTIC_TOP_LEVEL_FIELDS = {
    "schema_version",
    "case_id",
    "language",
    "title",
    "purpose",
    "evaluation_layers",
    "score_contract",
    "fact_checking_policy",
    "path_variables",
    "evidence_bundles",
    "evaluator_protocols",
    "criteria",
    "query_requirement_map",
}
AGENTIC_EVALUATOR_MODES = {"llm_judge", "deterministic", "hybrid"}
AGENTIC_INTERMEDIATE_GROUPS = {
    "intermediate_plan",
    "intermediate_research",
    "intermediate_image",
    "intermediate_speech",
    "intermediate_visual_system",
    "intermediate_cross_artifact",
}
AGENTIC_FINAL_GROUPS = {"final_content", "final_aesthetic"}
AGENTIC_GROUPS = AGENTIC_INTERMEDIATE_GROUPS | AGENTIC_FINAL_GROUPS
EXPECTED_BANDS = (
    ("not_met", 0, 19),
    ("weak", 20, 39),
    ("partial", 40, 59),
    ("mostly", 60, 79),
    ("fully", 80, 100),
)
ANCHOR_SCORE_PREFIXES = {
    level: f"{minimum}–{maximum}" for level, minimum, maximum in EXPECTED_BANDS
}


class ConversionError(ValueError):
    """Raised when the source rubric cannot be converted safely."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="Source case_rubric JSON path.")
    parser.add_argument(
        "--output",
        type=Path,
        help="Output YAML path. Defaults to case_rubric.yaml beside a *.source.json input.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify that the output already matches the rendered YAML without writing it.",
    )
    return parser.parse_args()


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConversionError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle, object_pairs_hook=_object_without_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise ConversionError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConversionError("rubric root must be a JSON object")
    return value


def require_exact_fields(value: dict[str, Any], expected: set[str], location: str) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        raise ConversionError(f"{location} fields mismatch: missing={missing}, extra={extra}")


def require_nonempty_string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConversionError(f"{location} must be a non-empty string")
    return value


def normalize_anchor_prefixes(rubric: dict[str, Any]) -> int:
    """Canonicalize common punctuation variants in recognized score-band prefixes."""
    normalized = 0
    criteria = rubric.get("criteria")
    if not isinstance(criteria, list):
        return normalized
    for criterion in criteria:
        if not isinstance(criterion, dict):
            continue
        anchors = criterion.get("anchors")
        if not isinstance(anchors, dict):
            continue
        for level, score_prefix in ANCHOR_SCORE_PREFIXES.items():
            anchor = anchors.get(level)
            if not isinstance(anchor, str):
                continue
            minimum, maximum = score_prefix.split("–", maxsplit=1)
            prefix_match = re.match(
                rf"^{re.escape(minimum)}[-–—－]{re.escape(maximum)}[:：]",
                anchor,
            )
            if prefix_match is None:
                continue
            canonical_prefix = f"{score_prefix}:"
            if prefix_match.group(0) != canonical_prefix:
                description = anchor[prefix_match.end() :].lstrip()
                anchors[level] = f"{canonical_prefix} {description}"
                normalized += 1
    return normalized


def validate_case_scale(value: Any) -> None:
    if not isinstance(value, dict):
        raise ConversionError("case_scale must be an object")
    require_exact_fields(value, {"min", "max", "aggregation", "bands"}, "case_scale")
    if value["min"] != 0 or value["max"] != 100:
        raise ConversionError("case_scale must span 0 through 100")
    if value["aggregation"] != "arithmetic_mean":
        raise ConversionError("case_scale.aggregation must be arithmetic_mean for equal criteria")
    bands = value["bands"]
    if not isinstance(bands, list) or len(bands) != len(EXPECTED_BANDS):
        raise ConversionError("case_scale.bands must contain the five standard bands")
    for index, (band, expected) in enumerate(zip(bands, EXPECTED_BANDS, strict=True)):
        if not isinstance(band, dict):
            raise ConversionError(f"case_scale.bands[{index}] must be an object")
        require_exact_fields(band, {"level", "min", "max", "description"}, f"case_scale.bands[{index}]")
        expected_level, expected_min, expected_max = expected
        if (band["level"], band["min"], band["max"]) != expected:
            raise ConversionError(
                f"case_scale.bands[{index}] must be {expected_level} [{expected_min}, {expected_max}]"
            )
        require_nonempty_string(band["description"], f"case_scale.bands[{index}].description")


def validate_review(value: Any) -> None:
    if not isinstance(value, dict):
        raise ConversionError("review must be an object")
    require_exact_fields(value, {"confidence_threshold"}, "review")
    threshold = value["confidence_threshold"]
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 <= threshold < 1:
        raise ConversionError("review.confidence_threshold must be a number in [0, 1)")


def validate_page_selector(value: Any, location: str) -> None:
    if not isinstance(value, dict):
        raise ConversionError(f"{location} must be an object")
    mode = value.get("mode")
    if mode not in PAGE_SELECTOR_MODES:
        raise ConversionError(f"{location}.mode must be first, last, all, or indices")
    expected_fields = {"mode", "values"} if mode == "indices" else {"mode"}
    require_exact_fields(value, expected_fields, location)
    if mode == "indices":
        values = value["values"]
        if (
            not isinstance(values, list)
            or not values
            or any(isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in values)
        ):
            raise ConversionError(f"{location}.values must be a non-empty array of positive integers")
        if len(values) != len(set(values)):
            raise ConversionError(f"{location}.values must be unique")


def validate_requirement_refs(value: Any, location: str) -> None:
    if not isinstance(value, list):
        raise ConversionError(f"{location} must be an array")
    refs = [require_nonempty_string(item, f"{location}[{index}]") for index, item in enumerate(value)]
    if len(refs) != len(set(refs)):
        raise ConversionError(f"{location} must contain unique requirement IDs")


def validate_criterion(
    value: Any,
    index: int,
    contract_version: str = CONTRACT_VERSION_V1,
) -> str:
    location = f"criteria[{index}]"
    if not isinstance(value, dict):
        raise ConversionError(f"{location} must be an object")
    actual_fields = set(value)
    if contract_version == CONTRACT_VERSION_V2:
        required_fields = V2_CRITERION_REQUIRED_FIELDS
        optional_fields = V2_CRITERION_OPTIONAL_FIELDS
    else:
        required_fields = CRITERION_REQUIRED_FIELDS
        optional_fields = CRITERION_OPTIONAL_FIELDS
    missing = sorted(required_fields - actual_fields)
    extra = sorted(actual_fields - required_fields - optional_fields)
    if missing or extra:
        raise ConversionError(f"{location} fields mismatch: missing={missing}, extra={extra}")
    name = require_nonempty_string(value["name"], f"{location}.name")
    mode = value["evaluation_mode"]
    if mode not in EVALUATION_MODES:
        raise ConversionError(f"{location}.evaluation_mode must be visual or html")
    if contract_version == CONTRACT_VERSION_V2:
        scope = value["evaluation_scope"]
        if scope not in EVALUATION_SCOPES:
            raise ConversionError(f"{location}.evaluation_scope must be page or deck")
        selector = value.get("page_selector")
        if scope == "page":
            if selector is None:
                raise ConversionError(f"{location}.page_selector is required for page scope")
            validate_page_selector(selector, f"{location}.page_selector")
        elif selector is not None:
            raise ConversionError(f"{location}.page_selector is not allowed for deck scope")
        validate_requirement_refs(value.get("requirement_refs", []), f"{location}.requirement_refs")
    require_nonempty_string(value["requirement"], f"{location}.requirement")
    require_nonempty_string(value["evaluation"], f"{location}.evaluation")
    anchors = value["anchors"]
    if not isinstance(anchors, dict):
        raise ConversionError(f"{location}.anchors must be an object")
    require_exact_fields(anchors, set(ANCHOR_LEVELS), f"{location}.anchors")
    for level in ANCHOR_LEVELS:
        anchor = require_nonempty_string(anchors[level], f"{location}.anchors.{level}")
        expected_prefix = f"{ANCHOR_SCORE_PREFIXES[level]}:"
        if not anchor.startswith(expected_prefix):
            raise ConversionError(f"{location}.anchors.{level} must start with {expected_prefix!r}")
    criticality = value.get("criticality", "standard")
    if criticality not in {"standard", "critical"}:
        raise ConversionError(f"{location}.criticality must be standard or critical")
    return name


def validate_legacy_rubric(rubric: dict[str, Any]) -> None:
    require_exact_fields(rubric, TOP_LEVEL_FIELDS, "rubric")
    contract_version = rubric["contract_version"]
    if contract_version not in CONTRACT_VERSIONS:
        raise ConversionError(f"contract_version must be one of {sorted(CONTRACT_VERSIONS)}")
    revision = require_nonempty_string(rubric["case_revision"], "case_revision")
    if not re.fullmatch(r"v[0-9]{3}", revision):
        raise ConversionError("case_revision must match vNNN")
    case_id = require_nonempty_string(rubric["case_id"], "case_id")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", case_id):
        raise ConversionError("case_id must contain only lowercase letters, digits, underscores, and hyphens")
    require_nonempty_string(rubric["instruction"], "instruction")
    validate_case_scale(rubric["case_scale"])
    validate_review(rubric["review"])
    criteria = rubric["criteria"]
    if not isinstance(criteria, list) or not criteria:
        raise ConversionError("criteria must contain at least one entry")
    names = [
        validate_criterion(criterion, index, contract_version)
        for index, criterion in enumerate(criteria)
    ]
    if len(names) != len(set(names)):
        raise ConversionError("criterion names must be unique")


def require_nonempty_string_array(value: Any, location: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ConversionError(f"{location} must be a non-empty array")
    normalized = [
        require_nonempty_string(item, f"{location}[{index}]")
        for index, item in enumerate(value)
    ]
    if len(normalized) != len(set(normalized)):
        raise ConversionError(f"{location} must contain unique values")
    return normalized


def validate_agentic_score_contract(value: Any) -> set[str]:
    if not isinstance(value, dict):
        raise ConversionError("score_contract must be an object")
    require_exact_fields(
        value,
        {"range", "round_to", "independence", "profile_rule", "profiles", "required_judge_output"},
        "score_contract",
    )
    if value["range"] != [0, 1]:
        raise ConversionError("score_contract.range must be [0, 1]")
    round_to = value["round_to"]
    if isinstance(round_to, bool) or not isinstance(round_to, int) or round_to < 0:
        raise ConversionError("score_contract.round_to must be a non-negative integer")
    require_nonempty_string(value["independence"], "score_contract.independence")
    require_nonempty_string(value["profile_rule"], "score_contract.profile_rule")

    profiles = value["profiles"]
    if not isinstance(profiles, dict) or not profiles:
        raise ConversionError("score_contract.profiles must be a non-empty object")
    for profile_name, profile in profiles.items():
        require_nonempty_string(profile_name, "score_contract profile name")
        location = f"score_contract.profiles.{profile_name}"
        if not isinstance(profile, dict):
            raise ConversionError(f"{location} must be an object")
        require_exact_fields(profile, {"allowed_scores", "anchors"}, location)
        allowed_scores = profile["allowed_scores"]
        if (
            not isinstance(allowed_scores, list)
            or not allowed_scores
            or any(
                isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not 0 <= score <= 1
                for score in allowed_scores
            )
        ):
            raise ConversionError(f"{location}.allowed_scores must contain numbers in [0, 1]")
        numeric_scores = [float(score) for score in allowed_scores]
        if numeric_scores != sorted(numeric_scores) or len(numeric_scores) != len(set(numeric_scores)):
            raise ConversionError(f"{location}.allowed_scores must be unique and ascending")
        anchors = profile["anchors"]
        if not isinstance(anchors, dict):
            raise ConversionError(f"{location}.anchors must be an object")
        try:
            numeric_anchor_keys = [float(key) for key in anchors]
        except (TypeError, ValueError) as exc:
            raise ConversionError(
                f"{location}.anchors keys must be numeric score strings"
            ) from exc
        if (
            len(numeric_anchor_keys) != len(allowed_scores)
            or len(set(numeric_anchor_keys)) != len(numeric_anchor_keys)
            or set(numeric_anchor_keys) != set(numeric_scores)
        ):
            raise ConversionError(
                f"{location}.anchors must contain exactly one anchor for every allowed score"
            )
        for anchor_key, anchor in anchors.items():
            require_nonempty_string(anchor, f"{location}.anchors.{anchor_key}")

    required_output = value["required_judge_output"]
    if not isinstance(required_output, dict):
        raise ConversionError("score_contract.required_judge_output must be an object")
    require_exact_fields(
        required_output,
        {"score", "evidence", "reason", "missing_or_unverifiable"},
        "score_contract.required_judge_output",
    )
    for field, description in required_output.items():
        require_nonempty_string(description, f"score_contract.required_judge_output.{field}")
    return set(profiles)


def validate_agentic_evaluation_layers(value: Any) -> tuple[dict[str, set[str]], dict[str, int]]:
    if not isinstance(value, dict):
        raise ConversionError("evaluation_layers must be an object")
    require_exact_fields(
        value,
        {"intermediate_primary", "final_secondary", "reporting"},
        "evaluation_layers",
    )
    require_nonempty_string(value["reporting"], "evaluation_layers.reporting")
    layer_groups: dict[str, set[str]] = {}
    layer_counts: dict[str, int] = {}
    expected_groups = {
        "intermediate_primary": AGENTIC_INTERMEDIATE_GROUPS,
        "final_secondary": AGENTIC_FINAL_GROUPS,
    }
    for layer_name, allowed_groups in expected_groups.items():
        layer = value[layer_name]
        location = f"evaluation_layers.{layer_name}"
        if not isinstance(layer, dict):
            raise ConversionError(f"{location} must be an object")
        require_exact_fields(
            layer,
            {"criterion_count", "common_dimension_reference", "groups"},
            location,
        )
        count = layer["criterion_count"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ConversionError(f"{location}.criterion_count must be a positive integer")
        require_nonempty_string(
            layer["common_dimension_reference"], f"{location}.common_dimension_reference"
        )
        groups = set(require_nonempty_string_array(layer["groups"], f"{location}.groups"))
        unknown = groups - allowed_groups
        if unknown:
            raise ConversionError(f"{location}.groups contains unsupported values: {sorted(unknown)}")
        layer_groups[layer_name] = groups
        layer_counts[layer_name] = count
    if layer_groups["intermediate_primary"] & layer_groups["final_secondary"]:
        raise ConversionError("intermediate and final evaluation-layer groups must not overlap")
    return layer_groups, layer_counts


def validate_agentic_criterion(
    value: Any,
    index: int,
    *,
    evidence_bundle_ids: set[str],
    profile_ids: set[str],
    query_requirement_ids: set[str],
) -> tuple[str, str]:
    location = f"criteria[{index}]"
    if not isinstance(value, dict):
        raise ConversionError(f"{location} must be an object")
    base_fields = {
        "id",
        "group",
        "common_dimension",
        "title",
        "objective",
        "mode",
        "input_bundles",
        "scoring",
        "query_requirements",
        "why",
    }
    mode = value.get("mode")
    if mode not in AGENTIC_EVALUATOR_MODES:
        raise ConversionError(
            f"{location}.mode must be one of {sorted(AGENTIC_EVALUATOR_MODES)}"
        )
    mode_fields = {
        "llm_judge": {"judge_instruction"},
        "deterministic": {"calculation"},
        "hybrid": {"deterministic_observation", "judge_instruction"},
    }[mode]
    require_exact_fields(value, base_fields | mode_fields, location)

    criterion_id = require_nonempty_string(value["id"], f"{location}.id")
    if not re.fullmatch(r"[A-Z][A-Z0-9]*_[A-Za-z0-9_]+", criterion_id):
        raise ConversionError(f"{location}.id must be a stable uppercase-prefixed identifier")
    group = value["group"]
    if group not in AGENTIC_GROUPS:
        raise ConversionError(f"{location}.group must be one of {sorted(AGENTIC_GROUPS)}")
    common_dimension = require_nonempty_string(
        value["common_dimension"], f"{location}.common_dimension"
    )
    if group in AGENTIC_INTERMEDIATE_GROUPS and not re.fullmatch(r"P[1-6]", common_dimension):
        raise ConversionError(f"{location}.common_dimension must be P1 through P6")
    if group in AGENTIC_FINAL_GROUPS and not re.fullmatch(r"[CA][0-9][A-Za-z0-9_]*", common_dimension):
        raise ConversionError(f"{location}.common_dimension must reference a final C/A dimension")
    for field in ("title", "objective", "why"):
        require_nonempty_string(value[field], f"{location}.{field}")
    for field in mode_fields:
        require_nonempty_string(value[field], f"{location}.{field}")

    input_bundles = set(
        require_nonempty_string_array(value["input_bundles"], f"{location}.input_bundles")
    )
    unknown_bundles = input_bundles - evidence_bundle_ids
    if unknown_bundles:
        raise ConversionError(
            f"{location}.input_bundles contains unknown bundles: {sorted(unknown_bundles)}"
        )
    query_requirements = set(
        require_nonempty_string_array(
            value["query_requirements"], f"{location}.query_requirements"
        )
    )
    unknown_requirements = query_requirements - query_requirement_ids
    if unknown_requirements:
        raise ConversionError(
            f"{location}.query_requirements contains unknown IDs: {sorted(unknown_requirements)}"
        )

    scoring = value["scoring"]
    if not isinstance(scoring, dict):
        raise ConversionError(f"{location}.scoring must be an object")
    required_scoring_fields = {"profile"}
    optional_scoring_fields = {"hard_boundaries"}
    missing = sorted(required_scoring_fields - set(scoring))
    extra = sorted(set(scoring) - required_scoring_fields - optional_scoring_fields)
    if missing or extra:
        raise ConversionError(
            f"{location}.scoring fields mismatch: missing={missing}, extra={extra}"
        )
    profile = scoring["profile"]
    if profile not in profile_ids:
        raise ConversionError(f"{location}.scoring.profile references unknown profile {profile!r}")
    if "hard_boundaries" in scoring:
        require_nonempty_string_array(
            scoring["hard_boundaries"], f"{location}.scoring.hard_boundaries"
        )
    return criterion_id, group


def validate_agentic_rubric(rubric: dict[str, Any]) -> None:
    require_exact_fields(rubric, AGENTIC_TOP_LEVEL_FIELDS, "rubric")
    if rubric["schema_version"] != AGENTIC_SCHEMA_VERSION_V1:
        raise ConversionError(
            f"schema_version must be {AGENTIC_SCHEMA_VERSION_V1!r}"
        )
    case_id = require_nonempty_string(rubric["case_id"], "case_id")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", case_id):
        raise ConversionError("case_id must contain only lowercase letters, digits, underscores, and hyphens")
    for field in ("language", "title", "purpose"):
        require_nonempty_string(rubric[field], field)

    layer_groups, layer_counts = validate_agentic_evaluation_layers(rubric["evaluation_layers"])
    profile_ids = validate_agentic_score_contract(rubric["score_contract"])

    path_variables = rubric["path_variables"]
    if not isinstance(path_variables, dict) or not path_variables:
        raise ConversionError("path_variables must be a non-empty object")
    for variable, replacement in path_variables.items():
        if not re.fullmatch(r"\{[A-Z][A-Z0-9_]*\}", variable):
            raise ConversionError(f"invalid path variable name: {variable!r}")
        require_nonempty_string(replacement, f"path_variables.{variable}")

    evidence_bundles = rubric["evidence_bundles"]
    if not isinstance(evidence_bundles, dict) or not evidence_bundles:
        raise ConversionError("evidence_bundles must be a non-empty object")
    for bundle_id, bundle in evidence_bundles.items():
        require_nonempty_string(bundle_id, "evidence bundle ID")
        location = f"evidence_bundles.{bundle_id}"
        if not isinstance(bundle, dict):
            raise ConversionError(f"{location} must be an object")
        require_exact_fields(bundle, {"files", "use"}, location)
        require_nonempty_string_array(bundle["files"], f"{location}.files")
        require_nonempty_string(bundle["use"], f"{location}.use")

    evaluator_protocols = rubric["evaluator_protocols"]
    if not isinstance(evaluator_protocols, dict):
        raise ConversionError("evaluator_protocols must be an object")
    require_exact_fields(
        evaluator_protocols, AGENTIC_EVALUATOR_MODES, "evaluator_protocols"
    )
    for mode, rules in evaluator_protocols.items():
        require_nonempty_string_array(rules, f"evaluator_protocols.{mode}")

    fact_checking = rubric["fact_checking_policy"]
    if not isinstance(fact_checking, dict):
        raise ConversionError("fact_checking_policy must be an object")
    require_exact_fields(
        fact_checking,
        {"requirements_authority", "claim_authority", "forbidden", "epistemic_labels"},
        "fact_checking_policy",
    )
    require_nonempty_string(
        fact_checking["requirements_authority"],
        "fact_checking_policy.requirements_authority",
    )
    require_nonempty_string(fact_checking["claim_authority"], "fact_checking_policy.claim_authority")
    require_nonempty_string_array(fact_checking["forbidden"], "fact_checking_policy.forbidden")
    require_nonempty_string_array(
        fact_checking["epistemic_labels"], "fact_checking_policy.epistemic_labels"
    )

    query_map = rubric["query_requirement_map"]
    if not isinstance(query_map, dict) or not query_map:
        raise ConversionError("query_requirement_map must be a non-empty object")
    for requirement_id, requirement in query_map.items():
        if not re.fullmatch(r"Q[0-9]{2}", requirement_id):
            raise ConversionError(f"invalid query requirement ID: {requirement_id!r}")
        location = f"query_requirement_map.{requirement_id}"
        if not isinstance(requirement, dict):
            raise ConversionError(f"{location} must be an object")
        require_exact_fields(requirement, {"requirement", "criteria"}, location)
        require_nonempty_string(requirement["requirement"], f"{location}.requirement")
        require_nonempty_string_array(requirement["criteria"], f"{location}.criteria")

    criteria = rubric["criteria"]
    if not isinstance(criteria, list) or not criteria:
        raise ConversionError("criteria must contain at least one entry")
    validated = [
        validate_agentic_criterion(
            criterion,
            index,
            evidence_bundle_ids=set(evidence_bundles),
            profile_ids=profile_ids,
            query_requirement_ids=set(query_map),
        )
        for index, criterion in enumerate(criteria)
    ]
    criterion_ids = [criterion_id for criterion_id, _group in validated]
    if len(criterion_ids) != len(set(criterion_ids)):
        raise ConversionError("criterion IDs must be unique")
    criterion_id_set = set(criterion_ids)
    for requirement_id, requirement in query_map.items():
        unknown = set(requirement["criteria"]) - criterion_id_set
        if unknown:
            raise ConversionError(
                f"query_requirement_map.{requirement_id}.criteria contains unknown IDs: {sorted(unknown)}"
            )

    intermediate_groups = layer_groups["intermediate_primary"]
    final_groups = layer_groups["final_secondary"]
    actual_intermediate = sum(group in intermediate_groups for _criterion_id, group in validated)
    actual_final = sum(group in final_groups for _criterion_id, group in validated)
    if actual_intermediate != layer_counts["intermediate_primary"]:
        raise ConversionError(
            "evaluation_layers.intermediate_primary.criterion_count does not match criteria"
        )
    if actual_final != layer_counts["final_secondary"]:
        raise ConversionError(
            "evaluation_layers.final_secondary.criterion_count does not match criteria"
        )
    undeclared_groups = {group for _criterion_id, group in validated} - intermediate_groups - final_groups
    if undeclared_groups:
        raise ConversionError(
            f"criteria use groups not declared by evaluation_layers: {sorted(undeclared_groups)}"
        )


def validate_rubric(rubric: dict[str, Any]) -> None:
    schema_id = rubric.get("schema_version") or rubric.get("contract_version")
    if schema_id in CONTRACT_VERSIONS:
        validate_legacy_rubric(rubric)
        return
    if schema_id == AGENTIC_SCHEMA_VERSION_V1:
        validate_agentic_rubric(rubric)
        return
    raise ConversionError(
        "unsupported rubric schema; expected contract_version in "
        f"{sorted(CONTRACT_VERSIONS)} or schema_version={AGENTIC_SCHEMA_VERSION_V1!r}"
    )


def default_output_path(source: Path) -> Path:
    if source.name.endswith(".source.json"):
        return source.with_name(source.name.removesuffix(".source.json") + ".yaml")
    return source.with_suffix(".yaml")


def render_yaml(rubric: dict[str, Any]) -> str:
    return yaml.safe_dump(
        rubric,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=120,
    )


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    args = parse_args()
    source = args.source.resolve()
    output = (args.output or default_output_path(source)).resolve()
    try:
        if not source.is_file():
            raise ConversionError(f"source file not found: {source}")
        if source == output:
            raise ConversionError("source and output paths must differ")
        rubric = load_json(source)
        normalized_anchor_prefixes = normalize_anchor_prefixes(rubric)
        validate_rubric(rubric)
        rendered = render_yaml(rubric)
        if args.check:
            if not output.is_file() or output.read_text(encoding="utf-8") != rendered:
                raise ConversionError(f"YAML output is missing or stale: {output}")
            print(f"checked {output}")
            return 0
        atomic_write(output, rendered)
        if normalized_anchor_prefixes:
            print(f"normalized {normalized_anchor_prefixes} anchor score prefix(es)")
        print(f"converted {source} -> {output}")
        return 0
    except (ConversionError, OSError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
