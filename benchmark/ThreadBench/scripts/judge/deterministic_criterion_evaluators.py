#!/usr/bin/env python3
"""Read-only deterministic criterion checks for dimension judging.

The registry is deliberately explicit.  Natural-language rubric text is never
executed as code: a future case-specific deterministic rule is added by
registering ``(case_id, criterion_id)`` here.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any, Callable


class DeterministicEvaluationError(ValueError):
    """Raised when an evaluator cannot read or parse its declared inputs."""


Evaluator = Callable[[Path, Path, dict[str, Any]], dict[str, Any]]


def _relative(path: Path, run_dir: Path, case_dir: Path) -> str:
    for root, prefix in ((run_dir, "{RUN_DIR}"), (case_dir, "{CASE_DIR}")):
        try:
            return f"{prefix}/{path.resolve().relative_to(root.resolve()).as_posix()}"
        except ValueError:
            pass
    return str(path)


def _indexed(directory: Path, suffix: str) -> dict[int, Path]:
    if not directory.is_dir():
        return {}
    pattern = re.compile(rf"slide_(\d+)\.{re.escape(suffix)}$")
    found: dict[int, Path] = {}
    for path in directory.iterdir():
        if not path.is_file():
            continue
        match = pattern.fullmatch(path.name)
        if match:
            found[int(match.group(1))] = path
    return found


def _expected_page_count(criterion: dict[str, Any]) -> int:
    text = " ".join(
        str(criterion.get(key, ""))
        for key in ("title", "objective", "calculation", "deterministic_observation")
    )
    matches = re.findall(r"(?:恰好|exact(?:ly)?|N\s*=\s*)\s*(\d+)\s*(?:页|pages?)?", text, re.I)
    if not matches:
        raise DeterministicEvaluationError(
            f"cannot infer expected page count for {criterion.get('id')}"
        )
    return int(matches[0])


def exact_static_deck_delivery(
    run_dir: Path, case_dir: Path, criterion: dict[str, Any]
) -> dict[str, Any]:
    """Evaluate exact page count, pairing, PNG ratio, HTML validity and index."""
    expected_count = _expected_page_count(criterion)
    require_present_index = "present.html" in str(criterion.get("calculation", ""))
    htmls = _indexed(run_dir / "slides", "html")
    renders = _indexed(run_dir / "renders", "png")
    expected = set(range(1, expected_count + 1))
    html_indices = set(htmls)
    render_indices = set(renders)

    invalid_html: list[int] = []
    invalid_png: list[int] = []
    invalid_ratio: list[dict[str, Any]] = []
    try:
        from PIL import Image
    except ImportError as exc:
        raise DeterministicEvaluationError("Pillow is required for aspect-ratio checks") from exc

    for index, path in htmls.items():
        prefix = path.read_text(encoding="utf-8", errors="replace")[:4096].lower()
        if path.stat().st_size == 0 or "<html" not in prefix and "<!doctype html" not in prefix:
            invalid_html.append(index)
    for index, path in renders.items():
        try:
            with path.open("rb") as handle:
                signature = handle.read(8)
            if signature != b"\x89PNG\r\n\x1a\n":
                invalid_png.append(index)
                continue
            with Image.open(path) as image:
                width, height = image.size
            relative_error = abs(width / height - 16 / 9) / (16 / 9)
            if relative_error > 0.01:
                invalid_ratio.append(
                    {
                        "page": index,
                        "width": width,
                        "height": height,
                        "relative_error": round(relative_error, 6),
                    }
                )
        except (OSError, ZeroDivisionError) as exc:
            invalid_png.append(index)

    present_path = run_dir / "present.html"
    present_refs: list[int] = []
    if present_path.is_file():
        source = present_path.read_text(encoding="utf-8", errors="replace")
        present_refs = [int(value) for value in re.findall(r"slide_(\d+)\.html", source)]

    checks = {
        "expected_page_count": expected_count,
        "html_indices": sorted(html_indices),
        "render_indices": sorted(render_indices),
        "missing_html": sorted(expected - html_indices),
        "extra_html": sorted(html_indices - expected),
        "missing_renders": sorted(expected - render_indices),
        "extra_renders": sorted(render_indices - expected),
        "invalid_html": invalid_html,
        "invalid_png": invalid_png,
        "invalid_16_9_ratio": invalid_ratio,
        "present_html_exists": present_path.is_file(),
        "present_html_required": require_present_index,
        "present_html_slide_refs": present_refs,
        "present_html_complete": present_refs == list(range(1, expected_count + 1)),
    }
    passed = (
        html_indices == expected
        and render_indices == expected
        and not invalid_html
        and not invalid_png
        and not invalid_ratio
        and (not require_present_index or checks["present_html_complete"])
    )
    failures = [name for name, value in checks.items() if name.startswith(("missing_", "extra_", "invalid_")) and value]
    if require_present_index:
        if not checks["present_html_exists"]:
            failures.append("present_html_missing")
        elif not checks["present_html_complete"]:
            failures.append("present_html_incomplete")
    evidence = [
        f"{_relative(run_dir / 'slides', run_dir, case_dir)}: {len(htmls)} canonical HTML pages",
        f"{_relative(run_dir / 'renders', run_dir, case_dir)}: {len(renders)} canonical PNG pages",
    ]
    if require_present_index:
        evidence.append(
            f"{_relative(present_path, run_dir, case_dir)}: refs={len(present_refs)}"
        )
    return {
        "status": "pass" if passed else "fail",
        "score": 1.0 if passed else 0.0,
        "evidence": evidence,
        "reason": "所有结构检查通过。" if passed else "未通过确定性结构检查：" + ", ".join(failures),
        "raw_observation": checks,
    }


def exact_rendered_page_delivery(
    run_dir: Path, case_dir: Path, criterion: dict[str, Any]
) -> dict[str, Any]:
    """Evaluate exact, consecutive, readable PNG render delivery."""
    expected_count = _expected_page_count(criterion)
    renders = _indexed(run_dir / "renders", "png")
    expected = set(range(1, expected_count + 1))
    render_indices = set(renders)
    invalid_png: list[int] = []
    try:
        from PIL import Image
    except ImportError as exc:
        raise DeterministicEvaluationError(
            "Pillow is required for PNG validity checks"
        ) from exc

    for index, path in renders.items():
        try:
            if path.stat().st_size == 0:
                invalid_png.append(index)
                continue
            with path.open("rb") as handle:
                if handle.read(8) != b"\x89PNG\r\n\x1a\n":
                    invalid_png.append(index)
                    continue
            with Image.open(path) as image:
                image.verify()
        except OSError:
            invalid_png.append(index)

    checks = {
        "expected_page_count": expected_count,
        "render_indices": sorted(render_indices),
        "missing_renders": sorted(expected - render_indices),
        "extra_renders": sorted(render_indices - expected),
        "invalid_png": sorted(invalid_png),
    }
    passed = (
        render_indices == expected
        and not invalid_png
    )
    failures = [
        name
        for name, value in checks.items()
        if name.startswith(("missing_", "extra_", "invalid_")) and value
    ]
    evidence = [
        f"{_relative(run_dir / 'renders', run_dir, case_dir)}: "
        f"{len(renders)} canonical PNG pages"
    ]
    return {
        "status": "pass" if passed else "fail",
        "score": 1.0 if passed else 0.0,
        "evidence": evidence,
        "reason": (
            f"{expected_count}页渲染编号连续且PNG均有效。"
            if passed
            else "未通过确定性渲染页检查：" + ", ".join(failures)
        ),
        "raw_observation": checks,
    }


def speech_page_coverage(run_dir: Path, case_dir: Path, point: dict[str, Any]) -> dict[str, Any]:
    plan_pages = _indexed(run_dir / "plan", "md")
    speech_path = run_dir / "speech.md"
    if not speech_path.is_file():
        return {
            "status": "fail",
            "score": 0.0,
            "evidence": [f"{_relative(speech_path, run_dir, case_dir)}: missing"],
            "reason": "speech.md 缺失。",
            "raw_observation": {"plan_pages": sorted(plan_pages), "speech_sections": []},
        }
    source = speech_path.read_text(encoding="utf-8", errors="replace")
    matches = [int(value) for value in re.findall(r"(?im)^#{1,6}\s+slide_(\d+)\b", source)]
    counts = Counter(matches)
    plan_indices = set(plan_pages)
    speech_indices = set(matches)
    duplicate = sorted(index for index, count in counts.items() if count != 1)
    missing = sorted(plan_indices - speech_indices)
    extra = sorted(speech_indices - plan_indices)
    empty: list[int] = []
    sections = list(re.finditer(r"(?im)^#{1,6}\s+slide_(\d+)\b[^\n]*\n", source))
    for position, match in enumerate(sections):
        end = sections[position + 1].start() if position + 1 < len(sections) else len(source)
        if not source[match.end() : end].strip():
            empty.append(int(match.group(1)))
    passed = bool(plan_indices) and not duplicate and not missing and not extra and not empty
    observation = {
        "plan_page_count": len(plan_indices),
        "plan_pages": sorted(plan_indices),
        "speech_section_count": len(matches),
        "speech_pages": sorted(speech_indices),
        "missing_pages": missing,
        "extra_pages": extra,
        "duplicate_pages": duplicate,
        "empty_sections": empty,
    }
    return {
        "status": "pass" if passed else "fail",
        "score": 1.0 if passed else 0.0,
        "evidence": [
            f"{_relative(run_dir / 'plan', run_dir, case_dir)}: pages={len(plan_indices)}",
            f"{_relative(speech_path, run_dir, case_dir)}: sections={len(matches)}",
        ],
        "reason": "逐页讲稿覆盖完整且一一对应。" if passed else "讲稿页码覆盖或唯一性不满足。",
        "raw_observation": observation,
    }


def generic_hybrid_observation(
    run_dir: Path,
    case_dir: Path,
    criterion: dict[str, Any],
    evidence_files: list[Path],
) -> dict[str, Any]:
    """Produce reproducible facts for a hybrid judge without assigning a score."""
    suffix_counts = Counter(path.suffix.lower() or "<none>" for path in evidence_files)
    run_relative = [
        _relative(path, run_dir, case_dir)
        for path in evidence_files
    ]
    observation: dict[str, Any] = {
        "criterion_id": criterion.get("id"),
        "matched_file_count": len(evidence_files),
        "file_type_counts": dict(sorted(suffix_counts.items())),
        "matched_files": run_relative,
        "missing_declared_bundles": [],
    }

    criterion_id = str(criterion.get("id", ""))
    if criterion_id == "P01_query_contract_and_50_page_plan":
        pages = _indexed(run_dir / "plan", "md")
        deck_path = run_dir / "plan/deck.md"
        deck_source = deck_path.read_text(encoding="utf-8", errors="replace") if deck_path.is_file() else ""
        observation.update(
            {
                "canonical_plan_pages": sorted(pages),
                "canonical_plan_page_count": len(pages),
                "deck_plan_mentions_50_pages": bool(re.search(r"(?:50\s*页|50\s*pages?)", deck_source, re.I)),
            }
        )
    elif criterion_id.startswith("R") or criterion_id == "F09_method_sources_quant_and_credits_realization":
        combined = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in evidence_files
            if path.suffix.lower() in {".md", ".json", ".html"} and path.stat().st_size <= 8_000_000
        )
        observation.update(
            {
                "unique_http_urls": len(set(re.findall(r"https?://[^\s)\]>'\"]+", combined))),
                "year_mentions": dict(Counter(re.findall(r"\b20\d{2}\b", combined)).most_common(12)),
                "numeric_token_count": len(re.findall(r"(?<!\w)\d+(?:\.\d+)?%?", combined)),
            }
        )
    elif criterion_id.startswith("I") or criterion_id == "F10_required_frameworks_and_eight_critical_visuals":
        assets_dir = run_dir / "assets/by-id"
        assets = sorted(path for path in assets_dir.glob("*") if path.is_file()) if assets_dir.is_dir() else []
        observation.update(
            {
                "asset_file_count": len(assets),
                "asset_files": [_relative(path, run_dir, case_dir) for path in assets],
                "slide_html_count": len(_indexed(run_dir / "slides", "html")),
            }
        )
    elif criterion_id == "S01_speech_page_coverage_and_binding":
        coverage = speech_page_coverage(run_dir, case_dir, criterion)
        raw_coverage = dict(coverage["raw_observation"])
        expected_count = _expected_page_count(criterion)
        expected_pages = set(range(1, expected_count + 1))
        plan_pages = set(raw_coverage.get("plan_pages", []))
        speech_pages = set(raw_coverage.get("speech_pages", []))
        observation.update(
            {
                "expected_page_count": expected_count,
                "expected_pages": sorted(expected_pages),
                "plan_pages": sorted(plan_pages),
                "plan_missing_expected": sorted(expected_pages - plan_pages),
                "plan_extra_expected": sorted(plan_pages - expected_pages),
                "speech_pages": sorted(speech_pages),
                "speech_missing_expected": sorted(expected_pages - speech_pages),
                "speech_extra_expected": sorted(speech_pages - expected_pages),
                "duplicate_pages": raw_coverage.get("duplicate_pages", []),
                "empty_sections": raw_coverage.get("empty_sections", []),
            }
        )
    return {
        "status": "observed",
        "score": None,
        "evidence": run_relative[:20],
        "reason": "已生成只读结构观测；语义有效性由 Judge 判定。",
        "raw_observation": observation,
    }


CASE_EVALUATORS: dict[tuple[str, str], Evaluator] = {
    (
        "talk_public_communication_long_horizon_zh_1",
        "F01_exact_50_page_16_9_static_delivery",
    ): exact_static_deck_delivery,
    (
        "education_long_horizon_zh_1",
        "F01_exact_10_page_16_9_static_delivery",
    ): exact_static_deck_delivery,
    (
        "perplexity_ads_pitch_long_horizon_zh_1",
        "F01_exact_33_page_rendered_delivery",
    ): exact_rendered_page_delivery,
    (
        "archaeological_evidence_pseudoarchaeology_long_horizon_zh_2",
        "F01_exact_36_page_rendered_delivery",
    ): exact_rendered_page_delivery,
}


COMMON_EVALUATORS: dict[str, Evaluator] = {
    "P4_1_SPEECH_PAGE_COVERAGE": speech_page_coverage,
}


def evaluate_case_criterion(
    case_id: str,
    criterion: dict[str, Any],
    run_dir: Path,
    case_dir: Path,
) -> dict[str, Any]:
    key = (case_id, str(criterion.get("id", "")))
    evaluator = CASE_EVALUATORS.get(key)
    if evaluator is None:
        raise DeterministicEvaluationError(
            "no deterministic evaluator registered for "
            f"case_id={case_id!r}, criterion_id={criterion.get('id')!r}"
        )
    return evaluator(run_dir, case_dir, criterion)


def evaluate_common_point(
    point: dict[str, Any], run_dir: Path, case_dir: Path
) -> dict[str, Any]:
    point_id = str(point.get("id", ""))
    evaluator = COMMON_EVALUATORS.get(point_id)
    if evaluator is None:
        raise DeterministicEvaluationError(
            f"no deterministic evaluator registered for common point {point_id!r}"
        )
    return evaluator(run_dir, case_dir, point)
