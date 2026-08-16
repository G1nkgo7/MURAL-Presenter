#!/usr/bin/env python3
"""Validate role-scoped reference ownership and read access."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path, PurePosixPath


REFERENCE_RE = re.compile(r"references/[A-Za-z0-9_./-]+(?:\.(?:md|css)|/\*)")
ALLOW_MARKER_RE = re.compile(r"<!--\s*reference-allow:\s*(.*?)\s*-->")
PATH_CONTRACT_RE = re.compile(r"<!--\s*path-contract:\s*dual-root-v1\s*-->")
ROLE_FILES = {
    "Material": "material.md",
    "Research": "research.md",
    "Designer": "designer.md",
    "Presenter": "presenter.md",
    "Image": "image.md",
    "Slide": "slide.md",
    "Audience": "audience.md",
}
LEGACY_PATHS = {
    "references/design-rules.md",
    "references/design-styles.md",
    "references/layout-patterns.md",
    "references/fonts.md",
    "references/fancy-effects.md",
    "references/base-template.css",
    "references/quality-checklist.md",
}


def _relative_reference_files(skill_root: Path) -> set[str]:
    refs_root = skill_root / "references"
    return {
        path.relative_to(skill_root).as_posix()
        for path in refs_root.rglob("*")
        if path.is_file() and path.suffix in {".md", ".css"}
    }


def _expand_reference_tokens(tokens: set[str], actual_files: set[str]) -> set[str]:
    expanded: set[str] = set()
    for token in tokens:
        if token.endswith("/*"):
            prefix = token[:-1]
            expanded.update(path for path in actual_files if path.startswith(prefix))
        else:
            expanded.add(token)
    return expanded


def _marker_tokens(text: str) -> set[str] | None:
    match = ALLOW_MARKER_RE.search(text)
    if not match:
        return None
    value = match.group(1).strip()
    if value.lower() == "none":
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


def _policy_reads(entries: dict[str, object], role: str) -> set[str]:
    return {
        path
        for path, rule in entries.items()
        if isinstance(rule, dict) and role in rule.get("consumers", [])
    }


def _valid_reference_id(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        value.startswith("references/")
        and not value.startswith("/")
        and "\\" not in value
        and ".." not in path.parts
        and path.as_posix() == value
    )


def _validate_skill_matrix(
    skill_root: Path,
    entries: dict[str, object],
    actual_files: set[str],
) -> list[str]:
    errors: list[str] = []
    skill_path = skill_root / "SKILL.md"
    if not skill_path.is_file():
        return ["missing SKILL.md"]
    text = skill_path.read_text(encoding="utf-8")
    if "## 双根路径与唯一交付契约" not in text:
        errors.append("missing dual-root contract in SKILL.md")
    for token in ("SKILL_ROOT", "WORKSPACE_ROOT", "ROLE_CARD", "allowed_reference_paths"):
        if token not in text:
            errors.append(f"missing dual-root token in SKILL.md: {token}")
    marker = "## Reference access matrix"
    if marker not in text:
        return ["missing Reference access matrix in SKILL.md"]
    section = text.split(marker, 1)[1]
    section = section.split("\n## ", 1)[0]
    rows: dict[str, str] = {}
    for line in section.splitlines():
        match = re.match(r"\|\s*([A-Za-z]+)\s*\|\s*(.*?)\s*\|\s*$", line)
        if match:
            rows[match.group(1)] = match.group(2)
    for role in [*ROLE_FILES, "Orchestrator"]:
        if role not in rows:
            errors.append(f"missing SKILL access-matrix row: {role}")
            continue
        tokens = set(REFERENCE_RE.findall(rows[role]))
        actual = _expand_reference_tokens(tokens, actual_files)
        expected = set() if role == "Orchestrator" else _policy_reads(entries, role)
        if actual != expected:
            errors.append(
                f"SKILL access matrix mismatch for {role}: "
                f"expected={sorted(expected)} actual={sorted(actual)}"
            )
    return errors


def validate(skill_root: Path) -> dict[str, object]:
    errors: list[str] = []
    policy_path = skill_root / "references" / "access-policy.json"
    if not policy_path.is_file():
        return {"ok": False, "errors": ["missing references/access-policy.json"]}

    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "errors": [f"invalid access policy: {exc}"]}

    if policy.get("schema_version") != 1:
        errors.append("access policy schema_version must be 1")

    roles = policy.get("roles")
    entries = policy.get("references")
    if not isinstance(roles, list) or set(roles) != set(ROLE_FILES):
        errors.append("access policy roles must match the seven role cards")
        roles = list(ROLE_FILES)
    if not isinstance(entries, dict):
        return {"ok": False, "errors": errors + ["access policy references must be an object"]}

    actual_files = _relative_reference_files(skill_root)
    declared_files = set(entries)
    for path in sorted(declared_files):
        if not _valid_reference_id(path):
            errors.append(f"non-canonical reference id in policy: {path!r}")
    for path in sorted(actual_files - declared_files):
        errors.append(f"unregistered reference: {path}")
    for path in sorted(declared_files - actual_files):
        errors.append(f"declared reference missing on disk: {path}")

    runtime_template = skill_root / "references" / "designer" / "base-template.css"
    if runtime_template.is_file():
        leaked = sorted(set(REFERENCE_RE.findall(runtime_template.read_text(encoding="utf-8"))))
        if leaked:
            errors.append(
                "runtime base-template.css leaks raw reference paths into copied base.css: "
                + ", ".join(leaked)
            )

    valid_owners = set(roles) | {"Shared"}
    for ref_path, rule in sorted(entries.items()):
        if not isinstance(rule, dict):
            errors.append(f"invalid policy entry: {ref_path}")
            continue
        owner = rule.get("owner")
        consumers = rule.get("consumers")
        if owner not in valid_owners:
            errors.append(f"invalid owner for {ref_path}: {owner!r}")
        if not isinstance(consumers, list) or not consumers:
            errors.append(f"consumers must be a non-empty list: {ref_path}")
            continue
        unknown = sorted(set(consumers) - set(roles))
        if unknown:
            errors.append(f"unknown consumers for {ref_path}: {unknown}")
        if owner != "Shared" and owner not in consumers:
            errors.append(f"owner must be an allowed consumer: {ref_path}")

        ref_file = skill_root / ref_path
        if ref_file.is_file():
            header = "\n".join(ref_file.read_text(encoding="utf-8").splitlines()[:6])
            if "Reference owner:" not in header or "Allowed consumers:" not in header:
                errors.append(f"missing owner/consumer header: {ref_path}")

    errors.extend(_validate_skill_matrix(skill_root, entries, actual_files))

    role_reads: dict[str, list[str]] = {}
    for role, filename in ROLE_FILES.items():
        card_path = skill_root / "subagents" / filename
        if not card_path.is_file():
            errors.append(f"missing role card: subagents/{filename}")
            continue
        text = card_path.read_text(encoding="utf-8")
        if not PATH_CONTRACT_RE.search(text):
            errors.append(f"missing dual-root path contract: subagents/{filename}")
        marker_tokens = _marker_tokens(text)
        if marker_tokens is None:
            errors.append(f"missing reference-allow marker: subagents/{filename}")
            marker_tokens = set()
        reads = _expand_reference_tokens(marker_tokens, actual_files)
        role_reads[role] = sorted(reads)
        expected_reads = _policy_reads(entries, role)
        if reads != expected_reads:
            errors.append(
                f"role-card allowlist mismatch for {role}: "
                f"expected={sorted(expected_reads)} actual={sorted(reads)}"
            )
        for ref_path in sorted(reads):
            if ref_path in LEGACY_PATHS:
                errors.append(f"legacy reference path in {filename}: {ref_path}")
                continue
            rule = entries.get(ref_path)
            if rule is None:
                errors.append(f"undeclared reference in {filename}: {ref_path}")
                continue
            if role not in rule.get("consumers", []):
                errors.append(f"unauthorized read: {role} -> {ref_path}")

        negative_cues = ("禁止", "不得", "不能", "不可", "绝不", "none")
        for line_no, line in enumerate(text.splitlines(), 1):
            if ALLOW_MARKER_RE.search(line) or any(cue in line for cue in negative_cues):
                continue
            for token in set(REFERENCE_RE.findall(line)):
                mentioned = _expand_reference_tokens({token}, actual_files)
                unauthorized = sorted(mentioned - expected_reads)
                if unauthorized:
                    errors.append(
                        f"unauthorized positive reference at {filename}:{line_no}: {unauthorized}"
                    )

    live_text_paths = [
        skill_root / "SKILL.md",
        *sorted((skill_root / "subagents").glob("*.md")),
        *sorted((skill_root / "references").rglob("*.md")),
    ]
    for path in live_text_paths:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if "${SKILL_DIR:-skills/ppt-skill-html}" in text:
            errors.append(
                f"cwd-relative skill fallback is forbidden in {path.relative_to(skill_root)}"
            )
        for legacy in sorted(LEGACY_PATHS):
            if legacy in text:
                errors.append(f"legacy reference path in {path.relative_to(skill_root)}: {legacy}")

    return {
        "ok": not errors,
        "errors": errors,
        "reference_files": len(actual_files),
        "declared_references": len(declared_files),
        "role_cards": len(role_reads),
        "role_reads": role_reads,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skill-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="ppt-skill-html root (default: parent of scripts/)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args()

    result = validate(args.skill_root.resolve())
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result["ok"]:
        print(
            "OK reference access: "
            f"{result['reference_files']} files, "
            f"{result['declared_references']} declared, "
            f"{result['role_cards']} role cards"
        )
    else:
        for error in result["errors"]:
            print(f"ERROR: {error}", file=sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
