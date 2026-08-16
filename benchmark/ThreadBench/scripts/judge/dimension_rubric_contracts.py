#!/usr/bin/env python3
"""Rubric contracts, evidence adapters, and dimension score aggregation."""

from __future__ import annotations

import glob
import json
import math
import re
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import yaml


SCORE_CONTRACT_VERSION = "dimension_rubric_score_v15"
RUN_CONTRACT_VERSION = "dimension_rubric_judge_run_v15"
TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".css", ".html", ".csv", ".tsv"}


class DimensionContractError(ValueError):
    """Raised when rubric, evidence, or Judge output violates the contract."""


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DimensionContractError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DimensionContractError(f"JSON root must be an object: {path}")
    return value


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise DimensionContractError(f"cannot read YAML {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DimensionContractError(f"YAML root must be an object: {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _ensure_within(path: Path, roots: tuple[Path, ...]) -> None:
    resolved = path.resolve()
    if not any(resolved == root.resolve() or root.resolve() in resolved.parents for root in roots):
        raise DimensionContractError(f"evidence path escapes case/run roots: {path}")


def display_path(path: Path, case_dir: Path, run_dir: Path) -> str:
    for root, variable in ((run_dir, "{RUN_DIR}"), (case_dir, "{CASE_DIR}")):
        try:
            return f"{variable}/{path.resolve().relative_to(root.resolve()).as_posix()}"
        except ValueError:
            continue
    return str(path)


def expand_evidence_patterns(
    patterns: Iterable[str], case_dir: Path, run_dir: Path
) -> tuple[list[Path], list[str]]:
    files: dict[Path, None] = {}
    unmatched: list[str] = []
    for raw_pattern in patterns:
        pattern = str(raw_pattern).replace("{CASE_DIR}", str(case_dir)).replace("{RUN_DIR}", str(run_dir))
        candidate = Path(pattern)
        if not candidate.is_absolute():
            candidate = run_dir / candidate
        matches = [Path(item) for item in glob.glob(str(candidate))]
        matched_files: list[Path] = []
        for path in matches:
            if path.is_dir():
                matched_files.extend(item for item in path.rglob("*") if item.is_file())
            elif path.is_file():
                matched_files.append(path)
        if not matched_files:
            unmatched.append(str(raw_pattern))
            continue
        for path in matched_files:
            _ensure_within(path, (case_dir, run_dir))
            files[path.resolve()] = None
    return sorted(files, key=lambda path: display_path(path, case_dir, run_dir)), unmatched


def patterns_for_bundles(
    case_rubric: dict[str, Any], bundle_names: Iterable[str]
) -> tuple[list[str], dict[str, str]]:
    bundles = case_rubric.get("evidence_bundles")
    if not isinstance(bundles, dict):
        raise DimensionContractError("case rubric evidence_bundles must be an object")
    patterns: list[str] = []
    uses: dict[str, str] = {}
    for name in bundle_names:
        spec = bundles.get(name)
        if not isinstance(spec, dict) or not isinstance(spec.get("files"), list):
            raise DimensionContractError(f"unknown or invalid evidence bundle: {name}")
        patterns.extend(str(item) for item in spec["files"])
        uses[str(name)] = str(spec.get("use", ""))
    return patterns, uses


def _bounded_text(source: str, limit: int) -> tuple[str, bool]:
    if len(source) <= limit:
        return source, False
    head = limit * 2 // 3
    tail = limit - head
    return source[:head] + f"\n\n[... {len(source) - limit} characters omitted ...]\n\n" + source[-tail:], True


@lru_cache(maxsize=2048)
def _cached_read_text(path: str, mtime_ns: int, size: int) -> str:
    """Avoid repeatedly reading the same large artifact for independent tasks."""
    del mtime_ns, size
    return Path(path).read_text(encoding="utf-8", errors="replace")


def _compact_trace_value(
    value: Any,
    *,
    key: str = "",
    preserve_tool_payload: bool = False,
    max_string_chars: int = 4_000,
) -> Any:
    preserve_tool_payload = preserve_tool_payload or key in {
        "result",
        "output",
        "error",
    }
    if isinstance(value, dict):
        return {
            str(child_key): _compact_trace_value(
                child_value,
                key=str(child_key),
                preserve_tool_payload=preserve_tool_payload,
                max_string_chars=max_string_chars,
            )
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [
            _compact_trace_value(
                item,
                key=key,
                preserve_tool_payload=preserve_tool_payload,
                max_string_chars=max_string_chars,
            )
            for item in value
        ]
    if isinstance(value, str):
        if key in {"context", "goal", "prompt"}:
            return f"[omitted delegated {key}; original_characters={len(value)}]"
        if key in {
            "content",
            "html",
            "source",
        } and not preserve_tool_payload and len(value) > 400:
            return f"[omitted large {key}; original_characters={len(value)}]"
        if len(value) > max_string_chars:
            head = max_string_chars * 2 // 3
            tail = max_string_chars - head
            return (
                value[:head]
                + f"\n[... {len(value) - max_string_chars} characters omitted ...]\n"
                + value[-tail:]
            )
    return value


def _tool_results_from_messages(
    source: str,
    *,
    web_only: bool = True,
) -> list[dict[str, Any]]:
    """Recover tool return payloads that older tool_log.json files omit."""
    try:
        messages = json.loads(source)
    except json.JSONDecodeError:
        return []
    if not isinstance(messages, list):
        return []
    calls: dict[str, dict[str, Any]] = {}
    ordered_ids: list[str] = []
    for message_index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                tool_id = block.get("id")
                if not isinstance(tool_id, str) or not tool_id:
                    continue
                tool_name = block.get("name")
                if not isinstance(tool_name, str) or not tool_name:
                    continue
                if web_only and not tool_name.startswith("web_"):
                    continue
                calls[tool_id] = {
                    "message_index": message_index,
                    "name": tool_name,
                    "args": block.get("input", {}),
                }
                ordered_ids.append(tool_id)
            elif block.get("type") == "tool_result":
                tool_id = block.get("tool_use_id")
                if not isinstance(tool_id, str) or tool_id not in calls:
                    continue
                is_error = bool(block.get("is_error", False))
                calls[tool_id]["status"] = "error" if is_error else "success"
                if is_error:
                    calls[tool_id]["error"] = block.get("content")
                else:
                    calls[tool_id]["output"] = block.get("content")
    return [calls[tool_id] for tool_id in ordered_ids if tool_id in calls]


def _complete_tool_log(
    source: str,
    messages_source: str | None = None,
) -> str:
    """Keep the complete tool log and merge unabridged sibling tool results."""
    try:
        value = json.loads(source)
    except json.JSONDecodeError:
        return source
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        return source
    recovered = (
        _tool_results_from_messages(messages_source, web_only=False)
        if messages_source is not None
        else []
    )
    recovered_by_signature: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in recovered:
        signature = json.dumps(
            [item.get("name"), item.get("args", {})],
            ensure_ascii=False,
            sort_keys=True,
        )
        recovered_by_signature[signature].append(item)

    completed: list[dict[str, Any]] = []
    for item in value:
        merged = dict(item)
        signature = json.dumps(
            [item.get("name"), item.get("args", {})],
            ensure_ascii=False,
            sort_keys=True,
        )
        matches = recovered_by_signature.get(signature, [])
        recovered_item = matches.pop(0) if matches else None
        if recovered_item is not None:
            for key in ("result", "output", "status", "error"):
                if key not in merged and key in recovered_item:
                    merged[key] = recovered_item[key]
        completed.append(merged)
    return json.dumps(completed, ensure_ascii=False, separators=(",", ":"))


def _compact_tool_log(
    source: str,
    messages_source: str | None = None,
    *,
    max_string_chars: int = 4_000,
) -> str:
    """Keep tool calls and their return/status fields without unbounded payloads."""
    try:
        value = json.loads(source)
    except json.JSONDecodeError:
        return source
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        return source
    recovered = (
        _tool_results_from_messages(messages_source)
        if messages_source is not None
        else []
    )
    recovered_by_signature: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in recovered:
        signature = json.dumps(
            [item.get("name"), item.get("args", {})],
            ensure_ascii=False,
            sort_keys=True,
        )
        recovered_by_signature[signature].append(item)

    compacted: list[dict[str, Any]] = []
    for item in value:
        if item.get("name") == "delegate_task":
            continue
        merged = dict(item)
        signature = json.dumps(
            [item.get("name"), item.get("args", {})],
            ensure_ascii=False,
            sort_keys=True,
        )
        matches = recovered_by_signature.get(signature, [])
        recovered_item = matches.pop(0) if matches else None
        if recovered_item is not None:
            for key in ("result", "output", "status", "error"):
                if key not in merged and key in recovered_item:
                    merged[key] = recovered_item[key]
        compacted.append(
            {
                key: _compact_trace_value(
                    merged[key],
                    key=key,
                    max_string_chars=max_string_chars,
                )
                for key in (
                    "turn",
                    "name",
                    "args",
                    "result",
                    "output",
                    "status",
                    "error",
                )
                if key in merged
            }
        )
    return json.dumps(compacted, ensure_ascii=False, separators=(",", ":"))


def _compact_workflow_tool_log(source: str) -> str:
    """Summarize every relevant page workflow call without source-sized payloads."""
    try:
        value = json.loads(source)
    except json.JSONDecodeError:
        return source
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        return source
    read_paths: list[str] = []
    sequence: list[str] = []
    edit_details: list[str] = []
    failures: list[str] = []
    for item in value:
        name = str(item.get("name", ""))
        args = item.get("args")
        args = args if isinstance(args, dict) else {}
        turn = item.get("turn")
        path = str(args.get("path", ""))
        short_path = "/".join(Path(path).parts[-3:]) if path else ""
        lowered = name.lower()
        if lowered == "read_file" and path and path not in read_paths:
            read_paths.append(path)
        elif lowered in {"write_file", "write"}:
            sequence.append(
                f"{turn}:write:{short_path}:chars={len(str(args.get('content', '')))}"
            )
        elif "edit" in lowered or "patch" in lowered:
            old_text = str(args.get("old_string", ""))
            new_text = str(args.get("new_string", ""))
            sequence.append(f"{turn}:edit:{short_path}")
            edit_details.append(
                f"{turn}:{short_path}:{len(old_text)}->{len(new_text)}:"
                f"{_bounded_text(old_text, 40)[0]}=>{_bounded_text(new_text, 40)[0]}"
            )
        elif "vision" in lowered:
            sequence.append(f"{turn}:vision_check:{short_path}")
        elif lowered == "bash" and "render" in str(args.get("command", "")).lower():
            command = _bounded_text(str(args.get("command", "")), 80)[0]
            sequence.append(f"{turn}:render:{command}")
        if item.get("status") == "error" or item.get("error"):
            failures.append(
                f"{turn}:{name}:{_bounded_text(str(item.get('error', '')), 300)[0]}"
            )
    return json.dumps(
        {
            "call_count": len(value),
            "read_paths": read_paths,
            "workflow_sequence": sequence,
            "edit_details": edit_details,
            "failures": failures,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _compact_audit_tool_log(
    source: str,
    messages_source: str | None = None,
) -> str:
    """Preserve every research/image call as a compact audit ledger."""
    try:
        value = json.loads(source)
    except json.JSONDecodeError:
        return source
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        return source
    recovered = (
        _tool_results_from_messages(messages_source)
        if messages_source is not None
        else []
    )
    recovered_by_signature: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in recovered:
        signature = json.dumps(
            [item.get("name"), item.get("args", {})],
            ensure_ascii=False,
            sort_keys=True,
        )
        recovered_by_signature[signature].append(item)

    ledger: list[dict[str, Any]] = []
    for item in value:
        name = str(item.get("name", ""))
        args = item.get("args")
        args = args if isinstance(args, dict) else {}
        signature = json.dumps([name, args], ensure_ascii=False, sort_keys=True)
        matches = recovered_by_signature.get(signature, [])
        recovered_item = matches.pop(0) if matches else {}
        compact_args: dict[str, Any] = {}
        for key in (
            "query",
            "url",
            "out_path",
            "path",
            "search_type",
            "aspect_ratio",
            "max_chars",
            "limit",
        ):
            if key in args:
                compact_args[key] = args[key]
        if "prompt" in args:
            limit = 800 if "generate" in name.lower() else 240
            compact_args["prompt"] = _bounded_text(str(args["prompt"]), limit)[0]
        if "content" in args:
            compact_args["content_characters"] = len(str(args["content"]))
        entry: dict[str, Any] = {
            "turn": item.get("turn"),
            "name": name,
            **({"args": compact_args} if compact_args else {}),
        }
        status = item.get("status", recovered_item.get("status"))
        if status is not None:
            entry["status"] = status
        error = item.get("error", recovered_item.get("error"))
        if error:
            entry["error"] = _bounded_text(str(error), 400)[0]
        ledger.append(entry)
    return json.dumps(ledger, ensure_ascii=False, separators=(",", ":"))


def _evidence_text(
    path: Path,
    source: str,
    *,
    trace_string_chars: int = 4_000,
    trace_mode: str = "detailed",
    preserve_full_text: bool = False,
) -> str:
    if path.name == "tool_log.json":
        messages_path = path.with_name("messages.json")
        messages_source = None
        if messages_path.is_file():
            stat = messages_path.stat()
            messages_source = _cached_read_text(
                str(messages_path),
                stat.st_mtime_ns,
                stat.st_size,
            )
        if preserve_full_text:
            return _complete_tool_log(source, messages_source)
        if trace_mode == "workflow":
            return _compact_workflow_tool_log(source)
        if trace_mode == "audit":
            return _compact_audit_tool_log(source, messages_source)
        return _compact_tool_log(
            source,
            messages_source,
            max_string_chars=trace_string_chars,
        )
    if (
        not preserve_full_text
        and path.name == "pages.json"
        and path.parent.name == "plan"
    ):
        try:
            value = json.loads(source)
        except json.JSONDecodeError:
            return source

        def without_speech(item: Any) -> Any:
            if isinstance(item, dict):
                return {
                    key: without_speech(child)
                    for key, child in item.items()
                    if key != "speech"
                }
            if isinstance(item, list):
                return [without_speech(child) for child in item]
            return item

        return json.dumps(
            without_speech(value),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return source


def _html_asset_references(source: str) -> list[str]:
    """Extract compact local asset usage from slide HTML without sending source."""
    candidates = re.findall(
        r'''(?:src|href)\s*=\s*["']([^"']+)["']|url\(\s*["']?([^\)"']+)["']?\s*\)''',
        source,
        flags=re.IGNORECASE,
    )
    references: list[str] = []
    for pair in candidates:
        value = next((item.strip() for item in pair if item.strip()), "")
        if not value or value.startswith(("data:", "http://", "https://", "#")):
            continue
        if value not in references:
            references.append(value)
    return references


def _evidence_weight(label: str) -> int:
    """Give contract/summary artifacts more room while preserving every file shard."""
    if label.endswith("/instruction.md"):
        return 4
    if label.endswith("/tool_log.json"):
        return 3
    if "/research/research_" in label and label.endswith(".md"):
        return 4
    if any(
        label.endswith(suffix)
        for suffix in (
            "/plan/deck.md",
            "/plan/assets.json",
            "/research/knowledge-brief.md",
            "/speech.md",
            "/plan/visual-system.json",
            "/base.css",
        )
    ):
        return 2
    return 1


def build_evidence_packet(
    files: list[Path],
    unmatched_patterns: list[str],
    case_dir: Path,
    run_dir: Path,
    *,
    per_file_chars: int = 24_000,
    total_chars: int = 180_000,
    include_final_html: bool = False,
    summarize_html_assets: bool = False,
    trace_string_chars: int = 4_000,
    trace_mode: str = "detailed",
    preserve_full_text: bool = False,
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    consumed = 0
    omitted_text_files: list[str] = []
    readable: list[tuple[Path, str]] = []
    readable_sources: dict[Path, str] = {}
    for path in files:
        label = display_path(path, case_dir, run_dir)
        is_final_html = "/slides/slide_" in label and path.suffix.lower() == ".html"
        if path.suffix.lower() in TEXT_SUFFIXES and (include_final_html or not is_final_html):
            readable.append((path, label))
            stat = path.stat()
            source = _cached_read_text(str(path), stat.st_mtime_ns, stat.st_size)
            readable_sources[path] = _evidence_text(
                path,
                source,
                trace_string_chars=trace_string_chars,
                trace_mode=trace_mode,
                preserve_full_text=preserve_full_text,
            )
    if preserve_full_text:
        budgets = {path: len(readable_sources[path]) for path, _label in readable}
    else:
        total_weight = sum(_evidence_weight(label) for _path, label in readable)
        budgets = {
            path: min(
                per_file_chars,
                max(1, total_chars * _evidence_weight(label) // max(total_weight, 1)),
            )
            for path, label in readable
        }
        used = sum(
            min(len(readable_sources[path]), budgets[path])
            for path, _label in readable
        )
        remaining = max(0, total_chars - used)
        while remaining:
            pending = [
                path
                for path, _label in readable
                if budgets[path] < min(len(readable_sources[path]), per_file_chars)
            ]
            if not pending:
                break
            share = max(1, remaining // len(pending))
            added = 0
            for path in pending:
                increase = min(
                    share,
                    remaining - added,
                    min(len(readable_sources[path]), per_file_chars) - budgets[path],
                )
                budgets[path] += increase
                added += increase
                if added >= remaining:
                    break
            if added <= 0:
                break
            remaining -= added
    for path in files:
        label = display_path(path, case_dir, run_dir)
        entry: dict[str, Any] = {
            "path": label,
            "bytes": path.stat().st_size,
            "suffix": path.suffix.lower(),
        }
        is_final_html = "/slides/slide_" in label and path.suffix.lower() == ".html"
        if path.suffix.lower() in TEXT_SUFFIXES and (include_final_html or not is_final_html):
            file_budget = budgets[path]
            if file_budget > 0:
                source = readable_sources[path]
                text, truncated = (
                    (source, False)
                    if preserve_full_text
                    else _bounded_text(source, file_budget)
                )
                entry["text"] = text
                entry["truncated"] = truncated or len(text) < len(source)
                entry["allocated_characters"] = file_budget
                consumed += len(text)
            else:
                entry["text_omitted"] = "task evidence text budget is too small for all files"
                omitted_text_files.append(label)
        elif summarize_html_assets and is_final_html:
            stat = path.stat()
            source = _cached_read_text(str(path), stat.st_mtime_ns, stat.st_size)
            entry["asset_references"] = _html_asset_references(source)
            entry["content"] = (
                "full slide HTML omitted; local asset references extracted deterministically"
            )
        else:
            entry["content"] = "binary/final HTML content omitted; use inventory or attached rendered deck"
        entries.append(entry)
    return {
        "files": entries,
        "unmatched_patterns": unmatched_patterns,
        "text_characters_included": consumed,
        "omitted_text_files": omitted_text_files,
        "limits": (
            {"mode": "full_text", "per_file_chars": None, "total_chars": None}
            if preserve_full_text
            else {"per_file_chars": per_file_chars, "total_chars": total_chars}
        ),
    }


def case_profile(case_rubric: dict[str, Any], criterion: dict[str, Any]) -> dict[str, Any]:
    scoring = criterion.get("scoring")
    profile_name = scoring.get("profile") if isinstance(scoring, dict) else None
    profiles = case_rubric.get("score_contract", {}).get("profiles", {})
    profile = profiles.get(profile_name) if isinstance(profiles, dict) else None
    if not isinstance(profile, dict):
        raise DimensionContractError(
            f"criterion {criterion.get('id')} references unknown score profile {profile_name!r}"
        )
    return {"name": profile_name, **profile}


def common_profiles(common_rubric: dict[str, Any]) -> dict[str, Any]:
    profiles = common_rubric.get("score_scale", {}).get("profiles")
    if not isinstance(profiles, dict):
        raise DimensionContractError("common intermediate rubric is missing score_scale.profiles")
    return profiles


def _allowed_number(value: Any, allowed: Iterable[Any], label: str) -> float:
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            pass
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise DimensionContractError(f"{label} score must be a finite number")
    score = float(value)
    normalized = [float(item) for item in allowed]
    if not any(abs(score - candidate) < 1e-9 for candidate in normalized):
        raise DimensionContractError(f"{label} score {score} is not allowed; expected one of {normalized}")
    return score


def _validate_evidence(value: Any, label: str, minimum: int = 2) -> list[str]:
    if not isinstance(value, list) or len(value) < minimum or not all(isinstance(item, str) and item.strip() for item in value):
        raise DimensionContractError(f"{label} evidence must contain at least {minimum} non-empty locators")
    return [str(item) for item in value]


def _structured_hard_boundaries(
    criterion: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        item
        for item in criterion.get("scoring", {}).get("hard_boundaries", [])
        if isinstance(item, dict)
    ]


def _visual_metric_value(
    metric: str,
    final_preaudits: dict[str, dict[str, Any]],
) -> int:
    visual = final_preaudits.get("visual", {})
    metrics = visual.get("visual_metrics", {})
    if metric == "real_scene_or_artifact_photo_page_count":
        return len(metrics.get("real_scene_or_artifact_photo_pages", []))
    if metric == "generic_or_low_detail_core_visual_substitution_page_count":
        return len(
            metrics.get(
                "generic_or_low_detail_core_visual_substitution_pages", []
            )
        )
    if metric == "max_repeated_layout_skeleton_page_count":
        groups = metrics.get("repeated_layout_skeleton_groups", [])
        return max(
            (
                len(group.get("pages", []))
                for group in groups
                if isinstance(group, dict)
            ),
            default=0,
        )
    if metric == "unreadable_key_label_page_count":
        return len(metrics.get("unreadable_key_label_pages", []))
    raise DimensionContractError(f"unsupported visual pre-audit metric {metric!r}")


def _compare_metric(actual: int, operator: str, expected: int) -> bool:
    if operator == "lte":
        return actual <= expected
    if operator == "lt":
        return actual < expected
    if operator == "gte":
        return actual >= expected
    if operator == "gt":
        return actual > expected
    if operator == "eq":
        return actual == expected
    raise DimensionContractError(f"unsupported hard-boundary operator {operator!r}")


def _hard_boundary_assessments(
    value: dict[str, Any],
    criterion: dict[str, Any],
    final_preaudits: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    criterion_id = str(criterion["id"])
    specs = _structured_hard_boundaries(criterion)
    judge_specs = {
        str(spec["id"]): spec
        for spec in specs
        if spec.get("trigger", {}).get("type") == "judge_assessment"
    }
    raw_assessments = value.get("hard_boundary_assessments", [])
    if not isinstance(raw_assessments, list):
        raise DimensionContractError(
            f"{criterion_id} hard_boundary_assessments must be an array"
        )
    raw_by_id: dict[str, dict[str, Any]] = {}
    for item in raw_assessments:
        raw_fields = {
            "id",
            "triggered",
            "evidence",
            "reason",
        }
        normalized_fields = raw_fields | {
            "scope",
            "target",
            "max_score",
            "condition",
        }
        if (
            not isinstance(item, dict)
            or frozenset(item)
            not in {frozenset(raw_fields), frozenset(normalized_fields)}
        ):
            raise DimensionContractError(
                f"{criterion_id} hard-boundary assessment fields mismatch"
            )
        boundary_id = str(item.get("id", ""))
        if boundary_id not in {
            str(spec["id"]) for spec in specs
        } or boundary_id in raw_by_id:
            raise DimensionContractError(
                f"{criterion_id} has unknown or repeated hard-boundary assessment "
                f"{boundary_id!r}"
            )
        if boundary_id not in judge_specs:
            # Runtime-derived assessments are recomputed from the frozen
            # pre-audit when a normalized checkpoint result is revalidated.
            continue
        if not isinstance(item.get("triggered"), bool):
            raise DimensionContractError(
                f"{criterion_id} hard-boundary {boundary_id} triggered must be boolean"
            )
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or not all(
            isinstance(locator, str) and locator.strip() for locator in evidence
        ):
            raise DimensionContractError(
                f"{criterion_id} hard-boundary {boundary_id} evidence must be a string array"
            )
        if item["triggered"] and not evidence:
            raise DimensionContractError(
                f"{criterion_id} triggered hard-boundary {boundary_id} requires evidence"
            )
        reason = item.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise DimensionContractError(
                f"{criterion_id} hard-boundary {boundary_id} reason must be non-empty"
            )
        raw_by_id[boundary_id] = {
            "id": boundary_id,
            "triggered": item["triggered"],
            "evidence": evidence,
            "reason": reason.strip(),
        }
    missing = sorted(set(judge_specs) - set(raw_by_id))
    if missing:
        raise DimensionContractError(
            f"{criterion_id} is missing hard-boundary assessments: {missing}"
        )

    assessments: list[dict[str, Any]] = []
    for spec in specs:
        boundary_id = str(spec["id"])
        trigger = spec["trigger"]
        if trigger["type"] == "judge_assessment":
            assessment = raw_by_id[boundary_id]
        elif trigger["type"] == "visual_preaudit_metric":
            actual = _visual_metric_value(
                str(trigger["metric"]), final_preaudits
            )
            expected = int(trigger["value"])
            operator = str(trigger["operator"])
            triggered = _compare_metric(actual, operator, expected)
            assessment = {
                "id": boundary_id,
                "triggered": triggered,
                "evidence": [
                    f"final_visual_audit.visual_metrics.{trigger['metric']}={actual}"
                ],
                "reason": (
                    f"运行时比较 {actual} {operator} {expected}，"
                    f"{'触发' if triggered else '未触发'}硬边界。"
                ),
            }
        else:
            raise DimensionContractError(
                f"{criterion_id} hard-boundary {boundary_id} has unsupported trigger"
            )
        assessments.append(
            {
                **assessment,
                "scope": spec["scope"],
                "target": spec.get("target"),
                "max_score": float(spec["max_score"]),
                "condition": spec["condition"],
            }
        )
    return assessments


def validate_case_result(
    value: dict[str, Any],
    criterion: dict[str, Any],
    profile: dict[str, Any],
    final_preaudits: dict[str, dict[str, Any]] | None = None,
    *,
    minimum_evidence: int = 2,
) -> dict[str, Any]:
    criterion_id = str(criterion["id"])
    if value.get("criterion_id") != criterion_id:
        raise DimensionContractError(
            f"case result criterion_id mismatch: expected {criterion_id!r}, got {value.get('criterion_id')!r}"
        )
    adjustment_specs = criterion.get("scoring", {}).get("adjustments", [])
    applied_adjustments: list[dict[str, Any]] = []
    base_score: float | None = None
    if adjustment_specs:
        base_score = _allowed_number(
            value.get("base_score"),
            profile.get("allowed_scores", []),
            f"{criterion_id} base",
        )
        raw_applied = value.get("applied_adjustments", [])
        if not isinstance(raw_applied, list):
            raise DimensionContractError(
                f"{criterion_id} applied_adjustments must be an array"
            )
        specs_by_id = {str(item["id"]): item for item in adjustment_specs}
        delta_total = 0.0
        seen: set[str] = set()
        for item in raw_applied:
            if not isinstance(item, dict):
                raise DimensionContractError(
                    f"{criterion_id} applied adjustment must be an object"
                )
            adjustment_id = str(item.get("id", ""))
            spec = specs_by_id.get(adjustment_id)
            if spec is None or adjustment_id in seen:
                raise DimensionContractError(
                    f"{criterion_id} has unknown or repeated applied adjustment "
                    f"{adjustment_id!r}"
                )
            seen.add(adjustment_id)
            applications = item.get("applications")
            if (
                isinstance(applications, bool)
                or not isinstance(applications, int)
                or not 1 <= applications <= int(spec["max_applications"])
            ):
                raise DimensionContractError(
                    f"{criterion_id} adjustment {adjustment_id} applications is invalid"
                )
            adjustment_evidence = item.get("evidence", [])
            if spec.get("evidence_required"):
                adjustment_evidence = _validate_evidence(
                    adjustment_evidence,
                    f"{criterion_id} adjustment {adjustment_id}",
                    minimum=1,
                )
            elif not isinstance(adjustment_evidence, list) or not all(
                isinstance(evidence, str) for evidence in adjustment_evidence
            ):
                raise DimensionContractError(
                    f"{criterion_id} adjustment {adjustment_id} evidence must be an array"
                )
            delta_total += float(spec["delta"]) * applications
            applied_adjustments.append(
                {
                    "id": adjustment_id,
                    "applications": applications,
                    "evidence": adjustment_evidence,
                }
            )
        expected_score = round(min(1.0, max(0.0, base_score + delta_total)), 6)
        raw_score = value.get("score")
        if (
            isinstance(raw_score, bool)
            or not isinstance(raw_score, (int, float))
            or not math.isfinite(float(raw_score))
            or abs(float(raw_score) - expected_score) > 1e-6
        ):
            raise DimensionContractError(
                f"{criterion_id} score must equal clamped base plus adjustments "
                f"({expected_score})"
            )
        score = expected_score
    else:
        score = _allowed_number(
            value.get("raw_score", value.get("score")),
            profile.get("allowed_scores", []),
            criterion_id,
        )
    reason = value.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise DimensionContractError(f"{criterion_id} reason must be non-empty")
    missing = value.get("missing_or_unverifiable", [])
    if not isinstance(missing, list) or not all(isinstance(item, str) for item in missing):
        raise DimensionContractError(f"{criterion_id} missing_or_unverifiable must be a string array")
    # A zero score may describe the absence of the required content. In that
    # case there need not be two positive evidence locators, but the Judge must
    # explicitly identify what is missing. Non-zero scores keep the strict
    # two-locator evidence contract.
    evidence = _validate_evidence(
        value.get("evidence"),
        criterion_id,
        minimum=0 if score == 0 and missing else minimum_evidence,
    )
    raw_score = score
    assessments = _hard_boundary_assessments(
        value,
        criterion,
        final_preaudits or {},
    )
    criterion_caps = [
        float(item["max_score"])
        for item in assessments
        if item["triggered"] and item["scope"] == "criterion"
    ]
    hard_boundary_cap = min(criterion_caps) if criterion_caps else None
    if hard_boundary_cap is not None:
        score = min(score, hard_boundary_cap)
    result = {
        "criterion_id": criterion_id,
        "score": score,
        "raw_score": raw_score,
        "score_profile": profile["name"],
        "evidence": evidence,
        "reason": reason.strip(),
        "missing_or_unverifiable": missing,
        "hard_boundary_cap": hard_boundary_cap,
        "hard_boundary_assessments": assessments,
    }
    if adjustment_specs:
        result["base_score"] = base_score
        result["applied_adjustments"] = applied_adjustments
    return result


def validate_case_result_with_na(
    value: dict[str, Any],
    criterion: dict[str, Any],
    profile: dict[str, Any],
    *,
    minimum_evidence: int = 2,
) -> dict[str, Any]:
    """Validate an Intermediate criterion that may be excluded as N/A."""
    criterion_id = str(criterion["id"])
    applicable = value.get("applicable")
    if not isinstance(applicable, bool):
        raise DimensionContractError(
            f"{criterion_id} applicable must be a boolean"
        )
    score_value = dict(value)
    score_value.pop("applicable", None)
    if applicable:
        result = validate_case_result(
            score_value,
            criterion,
            profile,
            minimum_evidence=minimum_evidence,
        )
        result["applicable"] = True
        return result
    if value.get("criterion_id") != criterion_id:
        raise DimensionContractError(
            f"case result criterion_id mismatch: expected {criterion_id!r}, "
            f"got {value.get('criterion_id')!r}"
        )
    if value.get("score") is not None:
        raise DimensionContractError(
            f"{criterion_id} score must be null when applicable is false"
        )
    evidence = _validate_evidence(
        value.get("evidence"),
        criterion_id,
        minimum=0,
    )
    reason = value.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise DimensionContractError(
            f"{criterion_id} reason must be non-empty"
        )
    missing = value.get("missing_or_unverifiable", [])
    if not isinstance(missing, list) or not all(
        isinstance(item, str) for item in missing
    ):
        raise DimensionContractError(
            f"{criterion_id} missing_or_unverifiable must be a string array"
        )
    return {
        "criterion_id": criterion_id,
        "applicable": False,
        "score": None,
        "raw_score": None,
        "score_profile": profile["name"],
        "evidence": evidence,
        "reason": reason.strip(),
        "missing_or_unverifiable": missing,
        "hard_boundary_cap": None,
        "hard_boundary_assessments": [],
    }


def validate_final_case_result_from_anchors(
    value: dict[str, Any],
    criterion: dict[str, Any],
    profile: dict[str, Any],
    final_preaudits: dict[str, dict[str, Any]] | None = None,
    *,
    page_numbers: list[int] | None = None,
    supports_na: bool = False,
    minimum_evidence: int = 2,
) -> dict[str, Any]:
    """Derive a final-output score from the uniquely matched rubric anchor."""
    criterion_id = str(criterion["id"])
    if "score" in value or "raw_score" in value:
        raise DimensionContractError(
            f"{criterion_id} final Judge output must not contain score or "
            "raw_score; runtime derives score from anchor_analysis"
        )
    anchor_analysis = value.get("anchor_analysis")
    allowed_scores = [float(item) for item in profile.get("allowed_scores", [])]
    anchors = criterion.get("scoring", {}).get("anchors", {})
    if not isinstance(anchor_analysis, list) or len(anchor_analysis) != len(
        allowed_scores
    ):
        raise DimensionContractError(
            f"{criterion_id} anchor_analysis must contain one entry for every "
            "allowed score"
        )
    matched_scores: list[float] = []
    normalized_anchor_analysis: list[dict[str, Any]] = []
    for index, (entry, allowed_score) in enumerate(
        zip(anchor_analysis, allowed_scores, strict=True)
    ):
        if not isinstance(entry, dict) or set(entry) != {
            "score",
            "anchor",
            "matches",
            "reason",
            "evidence",
        }:
            raise DimensionContractError(
                f"{criterion_id} anchor_analysis[{index}] must contain exactly "
                "score, anchor, matches, reason, and evidence"
            )
        entry_score = _allowed_number(
            entry.get("score"),
            [allowed_score],
            f"{criterion_id} anchor_analysis[{index}]",
        )
        anchor_key = (
            str(int(allowed_score))
            if allowed_score.is_integer()
            else str(allowed_score)
        )
        expected_anchor = anchors.get(anchor_key)
        if not isinstance(expected_anchor, str):
            raise DimensionContractError(
                f"{criterion_id} anchor_analysis[{index}] has no rubric anchor"
            )
        if not isinstance(entry.get("anchor"), str) or not entry["anchor"].strip():
            raise DimensionContractError(
                f"{criterion_id} anchor_analysis[{index}].anchor must be non-empty"
            )
        matches = entry.get("matches")
        if not isinstance(matches, bool):
            raise DimensionContractError(
                f"{criterion_id} anchor_analysis[{index}].matches must be boolean"
            )
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise DimensionContractError(
                f"{criterion_id} anchor_analysis[{index}].reason must be non-empty"
            )
        evidence = entry.get("evidence")
        if not isinstance(evidence, list) or not all(
            isinstance(item, str) for item in evidence
        ):
            raise DimensionContractError(
                f"{criterion_id} anchor_analysis[{index}].evidence must be a "
                "string array"
            )
        if matches:
            matched_scores.append(entry_score)
        normalized_anchor_analysis.append(
            {
                "score": entry_score,
                "anchor": expected_anchor,
                "matches": matches,
                "reason": reason.strip(),
                "evidence": list(evidence),
            }
        )
    applicable = value.get("applicable", True) if supports_na else True
    if not isinstance(applicable, bool):
        raise DimensionContractError(f"{criterion_id} applicable must be boolean")
    if applicable and len(matched_scores) != 1:
        raise DimensionContractError(
            f"{criterion_id} anchor_analysis must mark exactly one matching anchor"
        )
    if not applicable and matched_scores:
        raise DimensionContractError(
            f"{criterion_id} anchor_analysis must not match an anchor when N/A"
        )

    score_value = dict(value)
    score_value.pop("anchor_analysis", None)
    score_value["score"] = matched_scores[0] if applicable else None
    result = (
        validate_deck_case_result(
            score_value,
            criterion,
            profile,
            page_numbers,
            final_preaudits,
        )
        if page_numbers is not None
        else validate_case_result_with_na(
            score_value,
            criterion,
            profile,
            minimum_evidence=minimum_evidence,
        )
        if supports_na
        else validate_case_result(
            score_value,
            criterion,
            profile,
            final_preaudits,
            minimum_evidence=minimum_evidence,
        )
    )
    result["anchor_analysis"] = normalized_anchor_analysis
    return result


def validate_intermediate_case_result(
    value: dict[str, Any],
    criterion: dict[str, Any],
    profile: dict[str, Any],
    required_evidence_units: list[str],
    *,
    supports_na: bool = False,
) -> dict[str, Any]:
    """Validate exhaustive Intermediate review before accepting its score.

    Evidence locators in the compact score explanation may remain representative,
    but the Judge must explicitly confirm that it inspected every criterion-scoped
    artifact.  The Judge analyzes every declared anchor; runtime derives the raw
    score from the uniquely matching anchor instead of trusting a free-standing
    model-authored score.  A maximum score is invalid when any required unit failed
    or could not be verified.
    """
    criterion_id = str(criterion["id"])
    full_scope = value.get("full_scope_check")
    if not isinstance(full_scope, dict) or set(full_scope) != {
        "checked_evidence_units",
        "not_satisfied",
        "unverifiable",
    }:
        raise DimensionContractError(
            f"{criterion_id} full_scope_check must contain exactly "
            "checked_evidence_units, not_satisfied, and unverifiable"
        )
    checked = full_scope.get("checked_evidence_units")
    if checked != required_evidence_units:
        raise DimensionContractError(
            f"{criterion_id} checked_evidence_units must list every required "
            "evidence unit exactly once in canonical order"
        )
    not_satisfied = full_scope.get("not_satisfied")
    unverifiable = full_scope.get("unverifiable")
    for field, entries in (
        ("not_satisfied", not_satisfied),
        ("unverifiable", unverifiable),
    ):
        if not isinstance(entries, list) or not all(
            isinstance(item, str) and item.strip() for item in entries
        ):
            raise DimensionContractError(
                f"{criterion_id} full_scope_check.{field} must be a string array"
            )

    if "score" in value or "raw_score" in value:
        raise DimensionContractError(
            f"{criterion_id} Intermediate Judge output must not contain score or "
            "raw_score; runtime derives score from anchor_analysis"
        )
    anchor_analysis = value.get("anchor_analysis")
    allowed_scores = [float(item) for item in profile.get("allowed_scores", [])]
    anchors = criterion.get("scoring", {}).get("anchors", {})
    if not isinstance(anchor_analysis, list) or len(anchor_analysis) != len(
        allowed_scores
    ):
        raise DimensionContractError(
            f"{criterion_id} anchor_analysis must contain one entry for every "
            "allowed score"
        )
    matched_scores: list[float] = []
    normalized_anchor_analysis: list[dict[str, Any]] = []
    for index, (entry, allowed_score) in enumerate(
        zip(anchor_analysis, allowed_scores, strict=True)
    ):
        if not isinstance(entry, dict) or set(entry) != {
            "score",
            "anchor",
            "matches",
            "reason",
            "evidence",
        }:
            raise DimensionContractError(
                f"{criterion_id} anchor_analysis[{index}] must contain exactly "
                "score, anchor, matches, reason, and evidence"
            )
        entry_score = _allowed_number(
            entry.get("score"),
            [allowed_score],
            f"{criterion_id} anchor_analysis[{index}]",
        )
        anchor_key = str(int(allowed_score)) if allowed_score.is_integer() else str(allowed_score)
        expected_anchor = anchors.get(anchor_key)
        if not isinstance(expected_anchor, str):
            raise DimensionContractError(
                f"{criterion_id} anchor_analysis[{index}] has no rubric anchor"
            )
        if not isinstance(entry.get("anchor"), str) or not entry["anchor"].strip():
            raise DimensionContractError(
                f"{criterion_id} anchor_analysis[{index}].anchor must be non-empty"
            )
        matches = entry.get("matches")
        if not isinstance(matches, bool):
            raise DimensionContractError(
                f"{criterion_id} anchor_analysis[{index}].matches must be boolean"
            )
        anchor_reason = entry.get("reason")
        if not isinstance(anchor_reason, str) or not anchor_reason.strip():
            raise DimensionContractError(
                f"{criterion_id} anchor_analysis[{index}].reason must be non-empty"
            )
        anchor_evidence = entry.get("evidence")
        if not isinstance(anchor_evidence, list) or not all(
            isinstance(item, str) for item in anchor_evidence
        ):
            raise DimensionContractError(
                f"{criterion_id} anchor_analysis[{index}].evidence must be a "
                "string array"
            )
        if matches:
            matched_scores.append(entry_score)
        normalized_anchor_analysis.append(
            {
                "score": entry_score,
                "anchor": expected_anchor,
                "matches": matches,
                "reason": anchor_reason.strip(),
                "evidence": list(anchor_evidence),
            }
        )

    applicable = value.get("applicable", True) if supports_na else True
    if applicable:
        if len(matched_scores) != 1:
            raise DimensionContractError(
                f"{criterion_id} anchor_analysis must mark exactly one matching "
                "anchor when applicable"
            )
        derived_score: float | None = matched_scores[0]
    else:
        if matched_scores:
            raise DimensionContractError(
                f"{criterion_id} anchor_analysis must not match an anchor when "
                "applicable is false"
            )
        derived_score = None

    score_value = dict(value)
    score_value.pop("full_scope_check", None)
    score_value.pop("anchor_analysis", None)
    score_value["score"] = derived_score
    result = (
        validate_case_result_with_na(score_value, criterion, profile)
        if supports_na
        else validate_case_result(score_value, criterion, profile)
    )
    maximum = max(float(item) for item in profile.get("allowed_scores", []))
    score = result.get("score")
    if score is not None and abs(float(score) - maximum) < 1e-9:
        if not_satisfied or unverifiable or result["missing_or_unverifiable"]:
            raise DimensionContractError(
                f"{criterion_id} cannot receive the maximum score while any "
                "required unit is not satisfied, unverifiable, or missing"
            )
    result["full_scope_check"] = {
        "checked_evidence_units": list(checked),
        "not_satisfied": list(not_satisfied),
        "unverifiable": list(unverifiable),
    }
    result["anchor_analysis"] = normalized_anchor_analysis
    return result


def validate_deck_case_result(
    value: dict[str, Any],
    criterion: dict[str, Any],
    profile: dict[str, Any],
    page_numbers: list[int],
    final_preaudits: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate a deck criterion and require an explicit full-deck review."""
    pages_reviewed = value.get("pages_reviewed")
    if pages_reviewed != page_numbers:
        raise DimensionContractError(
            f"{criterion['id']} pages_reviewed must list every canonical page "
            "exactly once in order"
        )
    result = validate_case_result(
        value,
        criterion,
        profile,
        final_preaudits,
    )
    result["pages_reviewed"] = list(page_numbers)
    return result


def validate_page_case_result(
    value: dict[str, Any],
    criterion: dict[str, Any],
    profile: dict[str, Any],
    page_index: dict[str, dict[str, str]],
    final_preaudits: dict[str, dict[str, Any]] | None = None,
    *,
    round_to: int = 6,
    supports_na: bool = False,
) -> dict[str, Any]:
    """Validate each page and average numeric scores over applicable pages."""
    criterion_id = str(criterion["id"])
    if set(value) != {"criterion_id", "page_scores"}:
        raise DimensionContractError(
            f"{criterion_id} page result must contain only criterion_id and "
            "page_scores"
        )
    if value.get("criterion_id") != criterion_id:
        raise DimensionContractError(
            f"case result criterion_id mismatch: expected {criterion_id!r}, "
            f"got {value.get('criterion_id')!r}"
        )
    if criterion.get("scoring", {}).get("adjustments"):
        raise DimensionContractError(
            f"page criterion {criterion_id} does not support score adjustments"
        )
    raw_page_scores = value.get("page_scores")
    expected_pages = [int(page) for page in page_index]
    if not isinstance(raw_page_scores, list) or len(raw_page_scores) != len(
        expected_pages
    ):
        raise DimensionContractError(
            f"{criterion_id} page_scores must contain every canonical page"
        )

    normalized_pages: list[dict[str, Any]] = []
    observed_pages: list[int] = []
    aggregate_evidence: list[str] = []
    aggregate_missing: list[str] = []
    for item in raw_page_scores:
        expected_fields = {
            "page",
            "page_type",
            "score",
            "evidence",
            "reason",
            "missing_or_unverifiable",
        }
        if supports_na:
            expected_fields.add("applicable")
        if not isinstance(item, dict) or set(item) != expected_fields:
            raise DimensionContractError(
                f"{criterion_id} page score fields must be exactly "
                f"{sorted(expected_fields)}"
            )
        page = item.get("page")
        if isinstance(page, bool) or not isinstance(page, int):
            raise DimensionContractError(
                f"{criterion_id} page score page must be an integer"
            )
        page_key = str(page)
        page_metadata = page_index.get(page_key)
        if page_metadata is None:
            raise DimensionContractError(
                f"{criterion_id} page {page} is not a canonical page"
            )
        applicable = True
        if supports_na:
            applicable = item.get("applicable")
            if not isinstance(applicable, bool):
                raise DimensionContractError(
                    f"{criterion_id} page {page} applicable must be boolean"
                )
        if applicable:
            score = _allowed_number(
                item.get("score"),
                profile.get("allowed_scores", []),
                f"{criterion_id} page {page}",
            )
        else:
            if item.get("score") is not None:
                raise DimensionContractError(
                    f"{criterion_id} page {page} score must be null when not applicable"
                )
            score = None
        evidence = _validate_evidence(
            item.get("evidence"),
            f"{criterion_id} page {page}",
            minimum=1 if applicable else 0,
        )
        reason = item.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise DimensionContractError(
                f"{criterion_id} page {page} reason must be non-empty"
            )
        missing = item.get("missing_or_unverifiable", [])
        if not isinstance(missing, list) or not all(
            isinstance(entry, str) for entry in missing
        ):
            raise DimensionContractError(
                f"{criterion_id} page {page} missing_or_unverifiable must "
                "be a string array"
            )
        observed_pages.append(page)
        aggregate_evidence.extend(
            f"page {page}: {locator}" for locator in evidence
        )
        aggregate_missing.extend(
            f"page {page}: {entry}" for entry in missing
        )
        normalized_pages.append(
            {
                "page": page,
                # page_type is runtime routing metadata, not a Judge finding.
                # Normalize it from page_index even if the Judge supplies a
                # reasonable finer-grained label such as "data".
                "page_type": str(page_metadata["page_type"]),
                **({"applicable": applicable} if supports_na else {}),
                "score": score,
                "evidence": evidence,
                "reason": reason.strip(),
                "missing_or_unverifiable": missing,
            }
        )
    if observed_pages != expected_pages:
        raise DimensionContractError(
            f"{criterion_id} page_scores must list every canonical page "
            "exactly once in order"
        )

    applicable_pages = [
        item for item in normalized_pages if item["score"] is not None
    ]
    raw_score = (
        round(
            sum(float(item["score"]) for item in applicable_pages)
            / len(applicable_pages),
            round_to,
        )
        if applicable_pages
        else None
    )
    assessments = _hard_boundary_assessments(
        value,
        criterion,
        final_preaudits or {},
    )
    criterion_caps = [
        float(item["max_score"])
        for item in assessments
        if item["triggered"] and item["scope"] == "criterion"
    ]
    hard_boundary_cap = min(criterion_caps) if criterion_caps else None
    score = raw_score
    if score is not None and hard_boundary_cap is not None:
        score = min(score, hard_boundary_cap)
    distribution = {
        str(candidate): sum(
            1
            for item in normalized_pages
            if item["score"] is not None
            and abs(float(item["score"]) - float(candidate)) < 1e-9
        )
        for candidate in profile.get("allowed_scores", [])
    }
    if supports_na:
        distribution["N/A"] = len(normalized_pages) - len(applicable_pages)
    return {
        "criterion_id": criterion_id,
        "score": score,
        "raw_score": raw_score,
        "score_profile": f"{profile['name']}_per_page_mean",
        "page_scores": normalized_pages,
        "page_score_distribution": distribution,
        **({"applicable": bool(applicable_pages)} if supports_na else {}),
        "evidence": aggregate_evidence,
        "reason": (
            f"Arithmetic mean of {len(applicable_pages)} applicable pages "
            f"out of {len(normalized_pages)} independently reviewed pages; "
            f"distribution={distribution}."
        ),
        "missing_or_unverifiable": aggregate_missing,
        "hard_boundary_cap": hard_boundary_cap,
        "hard_boundary_assessments": assessments,
        "group_hard_boundary_assessments": [
            item for item in assessments if item["scope"] == "group"
        ],
        "applied_adjustments": [],
    }


def validate_page_case_unit_result(
    value: dict[str, Any],
    criterion: dict[str, Any],
    profile: dict[str, Any],
    *,
    page: int,
    page_type: str,
    supports_na: bool = False,
) -> dict[str, Any]:
    """Validate one Page criterion on one page and derive its anchor score."""
    criterion_id = str(criterion["id"])
    expected_fields = {
        "criterion_id",
        "page",
        "page_type",
        "evidence",
        "reason",
        "missing_or_unverifiable",
        "anchor_analysis",
    }
    if supports_na:
        expected_fields.add("applicable")
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise DimensionContractError(
            f"{criterion_id} page {page} result fields must be exactly "
            f"{sorted(expected_fields)}"
        )
    if value.get("criterion_id") != criterion_id:
        raise DimensionContractError(
            f"case result criterion_id mismatch: expected {criterion_id!r}, "
            f"got {value.get('criterion_id')!r}"
        )
    if value.get("page") != page:
        raise DimensionContractError(
            f"{criterion_id} page result must be for canonical page {page}"
        )
    if not isinstance(value.get("page_type"), str) or not value["page_type"].strip():
        raise DimensionContractError(
            f"{criterion_id} page {page} page_type must be non-empty"
        )

    applicable = value.get("applicable", True) if supports_na else True
    if not isinstance(applicable, bool):
        raise DimensionContractError(
            f"{criterion_id} page {page} applicable must be boolean"
        )
    anchor_value = {
        "criterion_id": criterion_id,
        "evidence": value.get("evidence"),
        "reason": value.get("reason"),
        "missing_or_unverifiable": value.get("missing_or_unverifiable"),
        "anchor_analysis": value.get("anchor_analysis"),
    }
    checked = validate_final_case_result_from_anchors(
        {**anchor_value, **({"applicable": applicable} if supports_na else {})},
        criterion,
        profile,
        supports_na=supports_na,
        minimum_evidence=1,
    )
    return {
        "criterion_id": criterion_id,
        "page": page,
        "page_type": page_type,
        **({"applicable": applicable} if supports_na else {}),
        "score": checked["score"],
        "evidence": checked["evidence"],
        "reason": checked["reason"],
        "missing_or_unverifiable": checked["missing_or_unverifiable"],
        "anchor_analysis": checked["anchor_analysis"],
    }


def aggregate_page_case_unit_results(
    criterion: dict[str, Any],
    profile: dict[str, Any],
    page_index: dict[str, dict[str, str]],
    unit_results: list[dict[str, Any]],
    *,
    round_to: int = 6,
    supports_na: bool = False,
) -> dict[str, Any]:
    """Aggregate independently judged dimension-by-page requests."""
    page_scores = []
    analyses: dict[int, list[dict[str, Any]]] = {}
    for result in unit_results:
        page = result.get("page")
        if isinstance(page, bool) or not isinstance(page, int):
            raise DimensionContractError("page unit result has an invalid page")
        analyses[page] = result.get("anchor_analysis", [])
        page_scores.append(
            {
                key: value
                for key, value in result.items()
                if key
                in {
                    "page",
                    "page_type",
                    "applicable",
                    "score",
                    "evidence",
                    "reason",
                    "missing_or_unverifiable",
                }
                and (supports_na or key != "applicable")
            }
        )
    aggregated = validate_page_case_result(
        {
            "criterion_id": criterion["id"],
            "page_scores": page_scores,
        },
        criterion,
        profile,
        page_index,
        round_to=round_to,
        supports_na=supports_na,
    )
    for item in aggregated["page_scores"]:
        item["anchor_analysis"] = analyses[int(item["page"])]
    return aggregated


def validate_common_point_result(
    value: dict[str, Any],
    point: dict[str, Any],
    profile: dict[str, Any],
) -> dict[str, Any]:
    point_id = str(point["id"])
    if value.get("criterion_id") != point_id:
        raise DimensionContractError(
            f"common point criterion_id mismatch: expected {point_id!r}, "
            f"got {value.get('criterion_id')!r}"
        )
    score = _allowed_number(
        value.get("score"), profile.get("allowed_scores", []), point_id
    )
    evidence = _validate_evidence(value.get("evidence"), point_id)
    reason = value.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise DimensionContractError(f"{point_id} reason must be non-empty")
    missing = value.get("missing_or_unverifiable", [])
    if not isinstance(missing, list) or not all(
        isinstance(item, str) for item in missing
    ):
        raise DimensionContractError(
            f"{point_id} missing_or_unverifiable must be a string array"
        )
    return {
        "criterion_id": point_id,
        "score": score,
        "score_profile": str(point["scale"]),
        "evidence": evidence,
        "reason": reason.strip(),
        "missing_or_unverifiable": missing,
    }


def validate_final_common_result(
    value: dict[str, Any],
    dimension: dict[str, Any],
    *,
    preaudit: dict[str, Any] | None = None,
    global_hard_boundaries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    dimension_id = str(dimension["id"])
    if value.get("dimension_id") != dimension_id:
        raise DimensionContractError(
            f"final common dimension_id mismatch: expected {dimension_id!r}, got {value.get('dimension_id')!r}"
        )
    score = _allowed_number(
        value.get("raw_score", value.get("score")),
        range(6),
        dimension_id,
    )
    evidence = _validate_evidence(value.get("evidence"), dimension_id)
    rationale = value.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise DimensionContractError(f"{dimension_id} rationale must be non-empty")
    defects = value.get("defects", [])
    if not isinstance(defects, list) or not all(isinstance(item, str) for item in defects):
        raise DimensionContractError(f"{dimension_id} defects must be a string array")
    raw_score = score
    applied_hard_boundaries: list[dict[str, Any]] = []
    hard_boundary_cap: float | None = None
    for boundary in global_hard_boundaries or []:
        trigger = boundary.get("trigger", {})
        if trigger.get("type") != "relevant_preaudit_non_local_defect":
            continue
        matching_defects = [
            defect
            for defect in (preaudit or {}).get("deck_defects", [])
            if isinstance(defect, dict)
            and defect.get("impact_scope") == "non_local"
        ]
        if not matching_defects:
            continue
        cap = float(boundary["max_score"])
        hard_boundary_cap = (
            cap
            if hard_boundary_cap is None
            else min(hard_boundary_cap, cap)
        )
        applied_hard_boundaries.append(
            {
                "id": str(boundary["id"]),
                "max_score": cap,
                "condition": str(boundary["condition"]),
                "evidence": [
                    f"{defect['id']}: pages {defect['pages']}"
                    for defect in matching_defects
                ],
            }
        )
    if hard_boundary_cap is not None:
        score = min(score, hard_boundary_cap)
    return {
        "dimension_id": dimension_id,
        "score": score,
        "raw_score": raw_score,
        "normalized_score": score / 5.0,
        "evidence": evidence,
        "rationale": rationale.strip(),
        "defects": defects,
        "hard_boundary_cap": hard_boundary_cap,
        "applied_hard_boundaries": applied_hard_boundaries,
    }


def validate_common_intermediate_result(
    value: dict[str, Any],
    dimension: dict[str, Any],
    profiles: dict[str, Any],
    deterministic_results: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    dimension_id = str(dimension["id"])
    if value.get("dimension_id") != dimension_id:
        raise DimensionContractError(
            f"common intermediate dimension_id mismatch: expected {dimension_id!r}, got {value.get('dimension_id')!r}"
        )
    raw_points = value.get("point_scores")
    if not isinstance(raw_points, list):
        raise DimensionContractError(f"{dimension_id} point_scores must be an array")
    by_id: dict[str, dict[str, Any]] = {}
    for item in raw_points:
        if not isinstance(item, dict) or not isinstance(item.get("point_id"), str):
            raise DimensionContractError(f"{dimension_id} contains invalid point result")
        if item["point_id"] in by_id:
            raise DimensionContractError(f"{dimension_id} repeats point_id {item['point_id']}")
        by_id[item["point_id"]] = item

    results: list[dict[str, Any]] = []
    expected_judge_ids: set[str] = set()
    for point in dimension.get("scoring_points", []):
        point_id = str(point["id"])
        if point.get("evaluator") == "deterministic":
            deterministic = deterministic_results.get(point_id)
            if deterministic is None:
                raise DimensionContractError(f"missing deterministic result for {point_id}")
            results.append(
                {
                    "point_id": point_id,
                    "evaluator": "deterministic",
                    "point_weight": float(point["point_weight"]),
                    **deterministic,
                }
            )
            continue
        expected_judge_ids.add(point_id)
        item = by_id.get(point_id)
        if item is None:
            raise DimensionContractError(f"missing Judge result for {point_id}")
        profile_name = str(point["scale"])
        profile = profiles.get(profile_name)
        if not isinstance(profile, dict):
            raise DimensionContractError(f"unknown common score profile {profile_name!r}")
        score = _allowed_number(item.get("score"), profile.get("allowed_scores", []), point_id)
        results.append(
            {
                "point_id": point_id,
                "evaluator": "judge",
                "point_weight": float(point["point_weight"]),
                "score": score,
                "score_profile": profile_name,
                "evidence": _validate_evidence(item.get("evidence"), point_id),
                "reason": str(item.get("reason", "")).strip(),
                "missing_or_unverifiable": item.get("missing_or_unverifiable", []),
            }
        )
        if not results[-1]["reason"]:
            raise DimensionContractError(f"{point_id} reason must be non-empty")
    unexpected = set(by_id) - expected_judge_ids
    if unexpected:
        raise DimensionContractError(f"{dimension_id} returned unexpected point IDs: {sorted(unexpected)}")

    denominator = sum(item["point_weight"] for item in results if item.get("score") is not None)
    if denominator <= 0:
        raise DimensionContractError(f"{dimension_id} has no applicable scored points")
    dimension_score = sum(item["score"] * item["point_weight"] for item in results if item.get("score") is not None) / denominator
    return {
        "dimension_id": dimension_id,
        "score": round(dimension_score, 6),
        "weight": float(dimension["weight"]),
        "point_scores": results,
        "strengths": value.get("strengths", []),
        "defects": value.get("defects", []),
    }


def aggregate_common_intermediate_dimension(
    dimension: dict[str, Any],
    point_results: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    expected_ids: set[str] = set()
    for point in dimension.get("scoring_points", []):
        point_id = str(point["id"])
        expected_ids.add(point_id)
        result = point_results.get(point_id)
        if result is None:
            raise DimensionContractError(f"missing common point result for {point_id}")
        results.append(
            {
                "point_id": point_id,
                "evaluator": (
                    "deterministic"
                    if point.get("evaluator") == "deterministic"
                    else "judge"
                ),
                "point_weight": float(point["point_weight"]),
                **result,
            }
        )
    unexpected = set(point_results) - expected_ids
    if unexpected:
        raise DimensionContractError(
            f"dimension {dimension.get('id')} received unexpected point IDs: "
            f"{sorted(unexpected)}"
        )
    denominator = sum(
        item["point_weight"] for item in results if item.get("score") is not None
    )
    if denominator <= 0:
        raise DimensionContractError(
            f"dimension {dimension.get('id')} has no applicable scored points"
        )
    dimension_score = sum(
        float(item["score"]) * item["point_weight"]
        for item in results
        if item.get("score") is not None
    ) / denominator
    return {
        "dimension_id": str(dimension["id"]),
        "score": round(dimension_score, 6),
        "weight": float(dimension["weight"]),
        "point_scores": results,
    }


def aggregate_intermediate_unit_results(
    criterion: dict[str, Any],
    units: list[dict[str, Any]],
    *,
    allowed_scores: Iterable[float] | None = None,
    round_to: int = 6,
) -> dict[str, Any]:
    """Average independently judged semantic units into one criterion score."""
    criterion_id = str(criterion["id"])
    if not units:
        raise DimensionContractError(
            f"criterion {criterion_id} has no evaluation units"
        )
    normalized_units: list[dict[str, Any]] = []
    seen: set[str] = set()
    aggregate_evidence: list[str] = []
    aggregate_missing: list[str] = []
    checked_units: list[str] = []
    not_satisfied: list[str] = []
    unverifiable: list[str] = []
    for unit in units:
        if not isinstance(unit, dict) or set(unit) != {
            "unit_id",
            "unit_label",
            "unit_instruction",
            "result",
        }:
            raise DimensionContractError(
                f"criterion {criterion_id} unit aggregate entry fields mismatch"
            )
        unit_id = unit.get("unit_id")
        unit_label = unit.get("unit_label")
        unit_instruction = unit.get("unit_instruction")
        result = unit.get("result")
        if (
            not isinstance(unit_id, str)
            or not unit_id
            or unit_id in seen
            or not isinstance(unit_label, str)
            or not unit_label.strip()
            or not isinstance(unit_instruction, str)
            or not unit_instruction.strip()
            or not isinstance(result, dict)
        ):
            raise DimensionContractError(
                f"criterion {criterion_id} has an invalid or repeated unit"
            )
        if result.get("criterion_id") != criterion_id:
            raise DimensionContractError(
                f"criterion {criterion_id} unit {unit_id} result mismatch"
            )
        seen.add(unit_id)
        prefix = f"unit {unit_id}"
        aggregate_evidence.extend(
            f"{prefix}: {item}" for item in result.get("evidence", [])
        )
        aggregate_missing.extend(
            f"{prefix}: {item}"
            for item in result.get("missing_or_unverifiable", [])
        )
        full_scope = result.get("full_scope_check", {})
        checked_units.extend(
            f"{prefix}: {item}"
            for item in full_scope.get("checked_evidence_units", [])
        )
        not_satisfied.extend(
            f"{prefix}: {item}"
            for item in full_scope.get("not_satisfied", [])
        )
        unverifiable.extend(
            f"{prefix}: {item}"
            for item in full_scope.get("unverifiable", [])
        )
        normalized_units.append(
            {
                "unit_id": unit_id,
                "unit_label": unit_label.strip(),
                "unit_instruction": unit_instruction.strip(),
                **result,
            }
        )
    applicable = [
        item for item in normalized_units if item.get("score") is not None
    ]
    score = (
        round(
            sum(float(item["score"]) for item in applicable) / len(applicable),
            round_to,
        )
        if applicable
        else None
    )
    profile_name = str(criterion["scoring"]["profile"])
    candidates = sorted(
        float(item)
        for item in (
            allowed_scores
            if allowed_scores is not None
            else {float(item["score"]) for item in applicable}
        )
    )
    distribution = {
        str(candidate): sum(
            1
            for item in applicable
            if abs(float(item["score"]) - candidate) < 1e-9
        )
        for candidate in candidates
    }
    if len(applicable) != len(normalized_units):
        distribution["N/A"] = len(normalized_units) - len(applicable)
    return {
        "criterion_id": criterion_id,
        "score": score,
        "raw_score": score,
        "score_profile": f"{profile_name}_per_unit_mean",
        "unit_scores": normalized_units,
        "unit_score_distribution": distribution,
        "unit_count": len(normalized_units),
        "applicable_unit_count": len(applicable),
        "applicable": bool(applicable),
        "evidence": aggregate_evidence,
        "reason": (
            f"Arithmetic mean of {len(applicable)} applicable independently "
            f"judged units out of {len(normalized_units)} units; "
            f"distribution={distribution}."
        ),
        "missing_or_unverifiable": aggregate_missing,
        "full_scope_check": {
            "checked_evidence_units": checked_units,
            "not_satisfied": not_satisfied,
            "unverifiable": unverifiable,
        },
        "hard_boundary_cap": None,
        "hard_boundary_assessments": [],
        "group_hard_boundary_assessments": [],
        "applied_adjustments": [],
    }


def aggregate_scores(
    case_results: list[dict[str, Any]],
    *,
    final_aggregation: dict[str, Any] | None = None,
    intermediate_aggregation: dict[str, Any] | None = None,
    final_dimensions: list[dict[str, Any]] | None = None,
    intermediate_dimensions: list[dict[str, Any]] | None = None,
    round_to: int = 6,
) -> dict[str, Any]:
    final_case = [
        item
        for item in case_results
        if str(item["group"]).startswith("final_")
    ]
    intermediate_case = [
        item
        for item in case_results
        if str(item["group"]).startswith("intermediate_")
    ]

    def mean(items: list[float]) -> float | None:
        return round(sum(items) / len(items), round_to) if items else None

    def grouped_role(
        items: list[dict[str, Any]],
        aggregation: dict[str, Any] | None,
    ) -> dict[str, Any]:
        groups: dict[str, list[float]] = defaultdict(list)
        for item in items:
            if item.get("score") is None:
                continue
            groups[str(item["group"])].append(float(item["score"]))
        raw_group_scores = {
            group: round(sum(values) / len(values), round_to)
            for group, values in sorted(groups.items())
        }
        group_caps: dict[str, float] = {}
        applied: list[dict[str, Any]] = []
        for item in items:
            for assessment in item.get("hard_boundary_assessments", []):
                if (
                    not isinstance(assessment, dict)
                    or not assessment.get("triggered")
                    or assessment.get("scope") != "group"
                ):
                    continue
                target = str(assessment.get("target", ""))
                if target not in raw_group_scores:
                    raise DimensionContractError(
                        f"group hard-boundary target {target!r} does not exist"
                    )
                cap = float(assessment["max_score"])
                group_caps[target] = min(group_caps.get(target, cap), cap)
                applied.append(
                    {
                        "criterion_id": item.get("criterion_id"),
                        **assessment,
                    }
                )
        group_scores = {
            group: round(
                min(score, group_caps.get(group, score)),
                round_to,
            )
            for group, score in raw_group_scores.items()
        }
        declared_weights = (
            aggregation.get("group_weights")
            if isinstance(aggregation, dict)
            else None
        )
        if declared_weights is None:
            role_score = mean(list(group_scores.values()))
            effective_weights = (
                {
                    group: round(1.0 / len(group_scores), round_to)
                    for group in group_scores
                }
                if group_scores
                else {}
            )
        else:
            if not isinstance(declared_weights, dict) or set(
                declared_weights
            ) != set(group_scores):
                raise DimensionContractError(
                    "aggregation group_weights must exactly match scored groups"
                )
            numeric_weights: dict[str, float] = {}
            for group, value in declared_weights.items():
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or float(value) <= 0
                ):
                    raise DimensionContractError(
                        f"aggregation weight for {group} must be a positive "
                        "finite number"
                    )
                numeric_weights[group] = float(value)
            weight_total = sum(numeric_weights.values())
            effective_weights = {
                group: round(value / weight_total, round_to)
                for group, value in sorted(numeric_weights.items())
            }
            role_score = round(
                sum(
                    group_scores[group] * weight
                    for group, weight in numeric_weights.items()
                )
                / weight_total,
                round_to,
            )
        return {
            "score": role_score,
            "group_scores": group_scores,
            "raw_group_scores": raw_group_scores,
            "group_weights": effective_weights,
            "group_hard_boundary_caps": group_caps,
            "applied_group_hard_boundaries": applied,
        }

    def dimension_first_role(
        items: list[dict[str, Any]],
        dimensions: list[dict[str, Any]],
        aggregation: dict[str, Any],
        *,
        final_role: bool,
    ) -> dict[str, Any]:
        definitions = {
            str(dimension["id"]): dimension for dimension in dimensions
        }
        criterion_scores: dict[str, list[float]] = defaultdict(list)
        for item in items:
            score = item.get("score")
            if score is None:
                continue
            dimension_id = str(item.get("dimension_id", ""))
            if dimension_id not in definitions:
                raise DimensionContractError(
                    f"result {item.get('criterion_id')} references unknown "
                    f"dimension {dimension_id!r}"
                )
            criterion_scores[dimension_id].append(float(score))

        raw_dimension_scores = {
            dimension_id: round(sum(values) / len(values), round_to)
            for dimension_id, values in sorted(criterion_scores.items())
        }
        declared_dimension_weights = {
            dimension_id: float(definition["weight"])
            for dimension_id, definition in definitions.items()
            if dimension_id in raw_dimension_scores
        }

        group_dimensions = (
            final_role
            or aggregation.get("dimension_to_group") == "weighted_mean"
        )
        if group_dimensions:
            grouped_dimensions: dict[str, list[str]] = defaultdict(list)
            for dimension_id in raw_dimension_scores:
                grouped_dimensions[str(definitions[dimension_id]["group"])].append(
                    dimension_id
                )
            raw_group_scores: dict[str, float] = {}
            group_dimension_weights: dict[str, dict[str, float]] = {}
            for group, dimension_ids in sorted(grouped_dimensions.items()):
                weight_total = sum(
                    declared_dimension_weights[dimension_id]
                    for dimension_id in dimension_ids
                )
                raw_group_scores[group] = round(
                    sum(
                        raw_dimension_scores[dimension_id]
                        * declared_dimension_weights[dimension_id]
                        for dimension_id in dimension_ids
                    )
                    / weight_total,
                    round_to,
                )
                group_dimension_weights[group] = {
                    dimension_id: round(
                        declared_dimension_weights[dimension_id] / weight_total,
                        round_to,
                    )
                    for dimension_id in sorted(dimension_ids)
                }

            group_caps: dict[str, float] = {}
            applied: list[dict[str, Any]] = []
            for item in items:
                for assessment in item.get("hard_boundary_assessments", []):
                    if (
                        not isinstance(assessment, dict)
                        or not assessment.get("triggered")
                        or assessment.get("scope") != "group"
                    ):
                        continue
                    target = str(assessment.get("target", ""))
                    if target not in raw_group_scores:
                        raise DimensionContractError(
                            f"group hard-boundary target {target!r} does not exist"
                        )
                    cap = float(assessment["max_score"])
                    group_caps[target] = min(group_caps.get(target, cap), cap)
                    applied.append({"criterion_id": item.get("criterion_id"), **assessment})
            group_scores = {
                group: round(min(score, group_caps.get(group, score)), round_to)
                for group, score in raw_group_scores.items()
            }
            declared_group_weights = aggregation.get("group_weights")
            if not isinstance(declared_group_weights, dict) or set(
                declared_group_weights
            ) != set(group_scores):
                raise DimensionContractError(
                    "dimension-first group weights must exactly match scored groups"
                )
            group_weight_total = sum(
                float(weight) for weight in declared_group_weights.values()
            )
            effective_group_weights = {
                group: round(float(weight) / group_weight_total, round_to)
                for group, weight in sorted(declared_group_weights.items())
            }
            role_score = round(
                sum(
                    group_scores[group] * float(declared_group_weights[group])
                    for group in group_scores
                )
                / group_weight_total,
                round_to,
            )
            return {
                "score": role_score,
                "dimension_scores": raw_dimension_scores,
                "raw_dimension_scores": raw_dimension_scores,
                "dimension_weights": {
                    dimension_id: round(weight, round_to)
                    for dimension_id, weight in sorted(
                        declared_dimension_weights.items()
                    )
                },
                "group_dimension_weights": group_dimension_weights,
                "group_scores": group_scores,
                "raw_group_scores": raw_group_scores,
                "group_weights": effective_group_weights,
                "group_hard_boundary_caps": group_caps,
                "applied_group_hard_boundaries": applied,
            }

        weight_total = sum(declared_dimension_weights.values())
        effective_dimension_weights = {
            dimension_id: round(weight / weight_total, round_to)
            for dimension_id, weight in sorted(declared_dimension_weights.items())
        }
        role_score = round(
            sum(
                raw_dimension_scores[dimension_id] * weight
                for dimension_id, weight in declared_dimension_weights.items()
            )
            / weight_total,
            round_to,
        )
        return {
            "score": role_score,
            "dimension_scores": raw_dimension_scores,
            "raw_dimension_scores": raw_dimension_scores,
            "dimension_weights": effective_dimension_weights,
            # Legacy Intermediate rubrics use their four historical groups as dimensions.
            "group_scores": raw_dimension_scores,
            "raw_group_scores": raw_dimension_scores,
            "group_weights": effective_dimension_weights,
            "group_hard_boundary_caps": {},
            "applied_group_hard_boundaries": [],
        }

    scores: dict[str, dict[str, Any]] = {}
    if final_case:
        if final_dimensions is not None:
            if not isinstance(final_aggregation, dict):
                raise DimensionContractError(
                    "dimension-first final aggregation must be declared"
                )
            final_case_role = dimension_first_role(
                final_case,
                final_dimensions,
                final_aggregation,
                final_role=True,
            )
            knowledge_checklist_score = final_case_role[
                "dimension_scores"
            ].get("final_knowledge")
            deck_dimension_scores = [
                {
                    "dimension_id": str(dimension["id"]),
                    "title": str(dimension["title"]),
                    "score": final_case_role["dimension_scores"].get(
                        str(dimension["id"])
                    ),
                    "source": (
                        "knowledge_checklist_mean"
                        if dimension["id"] == "final_knowledge"
                        else "deck_dimension_criterion_mean"
                    ),
                }
                for dimension in final_dimensions
                if dimension["group"] == "final_deck_level"
                and str(dimension["id"]) in final_case_role["dimension_scores"]
            ]
            scores["final_output_case_specific"] = {
                **final_case_role,
                "criterion_count": len(final_case),
                "deck_dimension_scores": deck_dimension_scores,
                "deck_dimension_count": len(deck_dimension_scores),
                "knowledge_checklist_score": knowledge_checklist_score,
            }
            final_case = []
    if final_case:
        final_items = final_case
        deck_dimension_scores: list[dict[str, Any]] | None = None
        knowledge_checklist_score: float | None = None
        if (
            isinstance(final_aggregation, dict)
            and final_aggregation.get("knowledge_as_deck_dimension") is True
        ):
            knowledge_scores = [
                float(item["score"])
                for item in final_case
                if item.get("group") == "final_knowledge"
                and item.get("score") is not None
            ]
            deck_items = [
                item
                for item in final_case
                if item.get("group") == "final_deck_level"
                and item.get("score") is not None
            ]
            if not knowledge_scores or not deck_items:
                raise DimensionContractError(
                    "knowledge_as_deck_dimension requires scored Knowledge and "
                    "Deck criteria"
                )
            knowledge_checklist_score = mean(knowledge_scores)
            assert knowledge_checklist_score is not None
            knowledge_dimension = {
                "criterion_id": "final_knowledge_checklist_mean",
                "group": "final_deck_level",
                "score": knowledge_checklist_score,
                "hard_boundary_assessments": [],
            }
            final_items = [
                item
                for item in final_case
                if item.get("group") != "final_knowledge"
            ] + [knowledge_dimension]
            deck_dimension_scores = [
                {
                    "dimension_id": str(item.get("criterion_id")),
                    "score": float(item["score"]),
                    "source": "deck_criterion",
                }
                for item in deck_items
            ] + [
                {
                    "dimension_id": "final_knowledge",
                    "score": knowledge_checklist_score,
                    "source": "knowledge_checklist_mean",
                    "checklist_count": len(knowledge_scores),
                }
            ]
        final_case_role = grouped_role(final_items, final_aggregation)
        scores["final_output_case_specific"] = {
            **final_case_role,
            "criterion_count": len(final_case),
            **(
                {
                    "deck_dimension_scores": deck_dimension_scores,
                    "deck_dimension_count": len(deck_dimension_scores),
                    "knowledge_checklist_score": knowledge_checklist_score,
                }
                if deck_dimension_scores is not None
                else {}
            ),
        }
    if intermediate_case:
        intermediate_case_role = (
            dimension_first_role(
                intermediate_case,
                intermediate_dimensions,
                intermediate_aggregation,
                final_role=False,
            )
            if intermediate_dimensions is not None
            and isinstance(intermediate_aggregation, dict)
            else grouped_role(intermediate_case, intermediate_aggregation)
        )
        scores["intermediate_case_specific"] = {
            **intermediate_case_role,
            "criterion_count": len(intermediate_case),
        }
    return scores


def nest_final_knowledge_under_deck(
    scores: dict[str, dict[str, Any]],
    *,
    round_to: int = 6,
) -> dict[str, dict[str, Any]]:
    """Expose Knowledge as a weighted component of Deck, not a peer group."""
    role = scores.get("final_output_case_specific")
    if not isinstance(role, dict):
        return scores
    group_scores = role.get("group_scores")
    group_weights = role.get("group_weights")
    if not isinstance(group_scores, dict) or not isinstance(group_weights, dict):
        return scores
    deck_group = "final_deck_level"
    knowledge_group = "final_knowledge"
    if deck_group not in group_scores or knowledge_group not in group_scores:
        return scores

    deck_weight = float(group_weights[deck_group])
    knowledge_weight = float(group_weights[knowledge_group])
    combined_weight = deck_weight + knowledge_weight
    if combined_weight <= 0:
        raise DimensionContractError("Deck and Knowledge weights must be positive")

    component_weights = {
        deck_group: round(deck_weight / combined_weight, round_to),
        knowledge_group: round(knowledge_weight / combined_weight, round_to),
    }

    def fold_score_map(field: str) -> None:
        score_map = role.get(field)
        if not isinstance(score_map, dict):
            return
        if deck_group not in score_map or knowledge_group not in score_map:
            return
        components = {
            deck_group: score_map[deck_group],
            knowledge_group: score_map[knowledge_group],
        }
        combined_score = round(
            (
                float(components[deck_group]) * deck_weight
                + float(components[knowledge_group]) * knowledge_weight
            )
            / combined_weight,
            round_to,
        )
        role[f"deck_component_{field}"] = components
        role[field] = {
            group: value
            for group, value in score_map.items()
            if group != knowledge_group
        }
        role[field][deck_group] = combined_score

    for field in (
        "group_scores",
        "raw_group_scores",
        "pre_defect_group_scores",
    ):
        fold_score_map(field)

    role["deck_component_weights"] = component_weights
    role["group_weights"] = {
        group: weight
        for group, weight in group_weights.items()
        if group != knowledge_group
    }
    role["group_weights"][deck_group] = round(combined_weight, round_to)
    weight_total = sum(float(value) for value in role["group_weights"].values())
    role["group_weights"] = {
        group: round(float(weight) / weight_total, round_to)
        for group, weight in sorted(role["group_weights"].items())
    }
    role["score"] = round(
        sum(
            float(role["group_scores"][group]) * float(weight)
            for group, weight in role["group_weights"].items()
        ),
        round_to,
    )
    return scores
