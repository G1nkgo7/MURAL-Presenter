"""Deck-language inference independent from the selected Skill edition."""
from __future__ import annotations

import re


_LANGUAGE_ALIASES = {
    "zh": "zh",
    "zh-cn": "zh",
    "zh-hans": "zh",
    "cn": "zh",
    "chinese": "zh",
    "中文": "zh",
    "汉语": "zh",
    "漢語": "zh",
    "en": "en",
    "en-us": "en",
    "en-gb": "en",
    "english": "en",
    "英文": "en",
    "英语": "en",
    "英語": "en",
}

_EXPLICIT_ZH_PATTERNS = (
    r"(?:用|以|请用|使用|输出|生成|制作|撰写|呈现).{0,8}(?:中文|汉语|漢語)",
    r"(?:中文|汉语|漢語).{0,8}(?:版|输出|生成|制作|撰写|呈现|演示|幻灯片)",
    r"\b(?:in|using)\s+chinese\b",
    r"\bchinese[-\s]language\b",
)
_EXPLICIT_EN_PATTERNS = (
    r"(?:用|以|请用|使用|输出|生成|制作|撰写|呈现).{0,8}(?:英文|英语|英語)",
    r"(?:英文|英语|英語).{0,8}(?:版|输出|生成|制作|撰写|呈现|演示|幻灯片)",
    r"\b(?:in|using)\s+english\b",
    r"\benglish[-\s]language\b",
)


def normalize_language(value: object) -> str:
    """Return ``zh``/``en`` for a known label, otherwise an empty string."""
    raw = str(value or "").strip().lower().replace("_", "-")
    return _LANGUAGE_ALIASES.get(raw, "")


def infer_deck_language(seed: dict) -> str:
    """Infer the presentation language from delivery intent and query text.

    Skill language is deliberately not an input. Explicit delivery fields win,
    then an explicit instruction inside the query, then the query's dominant
    script. Legacy ``lang`` metadata is only a fallback for ambiguous or nearly
    empty query text, because it may have been supplied merely to select an
    instruction edition.
    """
    for field in ("deck_language", "output_language"):
        language = normalize_language(seed.get(field))
        if language:
            return language

    query = str(seed.get("query") or "")
    for pattern in _EXPLICIT_ZH_PATTERNS:
        if re.search(pattern, query, flags=re.I):
            return "zh"
    for pattern in _EXPLICIT_EN_PATTERNS:
        if re.search(pattern, query, flags=re.I):
            return "en"

    han_count = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", query))
    latin_count = len(re.findall(r"[A-Za-z]", query))
    # Compare Han characters with an approximate Latin-word mass. This lets a
    # Chinese brief contain product names without becoming English, while an
    # English brief that mentions one Chinese name remains English.
    latin_mass = latin_count / 5.0
    if han_count >= 2 and han_count >= latin_mass * 1.2:
        return "zh"
    if latin_count >= 3 and latin_mass > han_count * 1.2:
        return "en"

    language = normalize_language(seed.get("lang"))
    if language:
        return language

    if han_count:
        return "zh"
    if latin_count:
        return "en"
    return "auto"
