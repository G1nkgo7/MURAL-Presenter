#!/usr/bin/env python3
"""Audit structural and invariant parity across the Chinese and English paper editions."""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_PAIRS = (
    ("chapters/01_introduction_related.md", "chapters_en/01_introduction_related.md"),
    ("chapters/02_method.md", "chapters_en/02_method.md"),
    ("chapters/03_benchmark_experiments.md", "chapters_en/03_benchmark_experiments.md"),
    ("chapters/04_discussion_conclusion.md", "chapters_en/04_discussion_conclusion.md"),
    ("plan/experiment-protocol_zh.md", "plan/experiment-protocol_en.md"),
)
OUTLINE_PAIR = ("plan/outline.md", "plan/outline_en.md")
TEX_PAIRS = (
    ("manuscript/chapters/05_appendix.tex", "manuscript/chapters_en/05_appendix.tex"),
    ("manuscript/figures/page_topologies.tex", "manuscript/figures/page_topologies_en.tex"),
    ("manuscript/figures/release_workflow.tex", "manuscript/figures/release_workflow_en.tex"),
)


def normalized_math(text: str) -> Counter[str]:
    blocks = re.findall(r"\\\[(.*?)\\\]", text, flags=re.DOTALL)
    inline = re.findall(r"\\\((.*?)\\\)", text, flags=re.DOTALL)
    return Counter(re.sub(r"\s+", "", item) for item in blocks + inline)


def citation_keys(text: str) -> Counter[str]:
    keys: list[str] = []
    for group in re.findall(r"\\cite[pt]?\{([^}]+)\}", text):
        keys.extend(key.strip() for key in group.split(","))
    return Counter(keys)


def markdown_signature(text: str) -> dict[str, object]:
    lines = text.splitlines()
    code = Counter(re.findall(r"`([^`\n]+)`", text))
    without_code = re.sub(r"`[^`\n]+`", "", text)
    return {
        "heading levels": [len(match.group(1)) for line in lines if (match := re.match(r"^(#{1,6})\s+", line))],
        "table shape": [line.count("|") for line in lines if line.strip().startswith("|")],
        "list shape": [
            (len(match.group(1)), match.group(2))
            for line in lines
            if (match := re.match(r"^(\s*)([-+*]|\d+[.)])\s+", line))
        ],
        "citations": citation_keys(text),
        "TBD markers": Counter(re.findall(r"TBD-E[0-9]+[A-Za-z]?", text)),
        "release hashes": Counter(re.findall(r"\b[0-9a-f]{7,40}(?:…)?\b", text)),
        "numbers": Counter(re.findall(r"(?<![A-Za-z_])\d+(?:\.\d+)?(?:M)?", text)),
        "math": normalized_math(text),
        # Language-specific caption inputs use an ``_en`` suffix while
        # referring to the same figure and label.
        "references": Counter(
            re.sub(r"_en(?=\})", "", item)
            for item in re.findall(r"\\(?:ref|autoref|input)\{[^}]+\}", text)
        ),
        "code spans": code,
        "urls": Counter(re.findall(r"https?://[^\s)>]+", text)),
        "english Han residue": len(re.findall(r"[\u3400-\u9fff]", without_code)),
    }


def tex_signature(text: str) -> dict[str, object]:
    return {
        "labels": Counter(re.findall(r"\\label\{[^}]+\}", text)),
        "graphics": Counter(re.findall(r"\\includegraphics(?:\[[^]]*\])?\{[^}]+\}", text)),
        "code/path": Counter(re.findall(r"\\(?:texttt|path)\{[^}]+\}", text)),
        "numbers": Counter(re.findall(r"(?<![A-Za-z_])\d+(?:\.\d+)?(?:M)?", text)),
        "release hashes": Counter(re.findall(r"\b[0-9a-f]{7,40}(?:…)?\b", text)),
    }


def compare_pair(zh_rel: str, en_rel: str, tex: bool = False) -> list[str]:
    zh_path = ROOT / zh_rel
    en_path = ROOT / en_rel
    failures: list[str] = []
    if not zh_path.is_file():
        return [f"missing Chinese source: {zh_rel}"]
    if not en_path.is_file():
        return [f"missing English peer: {en_rel}"]
    zh = zh_path.read_text(encoding="utf-8")
    en = en_path.read_text(encoding="utf-8")
    zh_sig = tex_signature(zh) if tex else markdown_signature(zh)
    en_sig = tex_signature(en) if tex else markdown_signature(en)
    for key, zh_value in zh_sig.items():
        en_value = en_sig[key]
        if key == "english Han residue":
            if en_value:
                failures.append(f"{en_rel}: {en_value} Han characters remain outside code spans")
        elif zh_value != en_value:
            failures.append(f"{en_rel}: {key} differs\n  zh={zh_value}\n  en={en_value}")
    return failures


def main() -> int:
    failures: list[str] = []
    checks = 0
    for zh_rel, en_rel in MARKDOWN_PAIRS:
        pair_failures = compare_pair(zh_rel, en_rel)
        checks += len(markdown_signature((ROOT / zh_rel).read_text(encoding="utf-8"))) if (ROOT / zh_rel).is_file() else 0
        failures.extend(pair_failures)

    # The Chinese author outline intentionally carries operational detail,
    # while the English peer is a compact narrative outline. Audit their shared
    # public contract rather than requiring identical list and number shapes.
    zh_outline = (ROOT / OUTLINE_PAIR[0]).read_text(encoding="utf-8")
    en_outline = (ROOT / OUTLINE_PAIR[1]).read_text(encoding="utf-8")
    outline_markers = (
        "MURALPRESENTER: Multi-Agent Unified Reasoning and Authoring for Long-Horizon Presentations",
        "MuralPresenter Authoring Skill",
        "THREAD-Bench: Tracking Holistic Requirements and End-to-End Alignment in Decks",
        "PresentBench",
        "SlidesGen-Bench",
        "DECKBench",
        "9.00",
    )
    for marker in outline_markers:
        if marker not in zh_outline or marker not in en_outline:
            failures.append(f"outline pair: shared marker missing or asymmetric: {marker}")
    for section_number in range(9):
        marker = f"## {section_number}."
        if marker not in zh_outline or marker not in en_outline:
            failures.append(f"outline pair: section {section_number} missing in one language")
    en_without_code = re.sub(r"`[^`\n]+`", "", en_outline)
    if re.search(r"[\u3400-\u9fff]", en_without_code):
        failures.append("plan/outline_en.md: Han characters remain outside code spans")
    checks += 1
    for zh_rel, en_rel in TEX_PAIRS:
        pair_failures = compare_pair(zh_rel, en_rel, tex=True)
        checks += len(tex_signature((ROOT / zh_rel).read_text(encoding="utf-8"))) if (ROOT / zh_rel).is_file() else 0
        failures.extend(pair_failures)
    if failures:
        print("Bilingual audit: FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print(f"Bilingual audit: PASS ({checks} invariant groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
