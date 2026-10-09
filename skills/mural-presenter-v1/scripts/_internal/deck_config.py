"""工作区当前创作配置；启动输入只用于新工作区的一次初始化。"""
from __future__ import annotations

import json
from pathlib import Path
import re


_REQUIRED = {"working_language", "presentation_language", "target_page_count"}
_OPTIONAL = {"speech_language"}
_LANGUAGE_TAG = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")


def validate_config(value: object, *, require_complete: bool = False) -> dict:
    if not isinstance(value, dict):
        raise ValueError("config.json must be a JSON object")
    missing = _REQUIRED - value.keys()
    unknown = value.keys() - _REQUIRED - _OPTIONAL
    if missing or unknown:
        raise ValueError(f"config.json fields: missing={sorted(missing)}, unsupported={sorted(unknown)}")
    for key in ("working_language", "presentation_language", "speech_language"):
        language = value.get(key)
        if language is None:
            if key == "speech_language" and key in value:
                raise ValueError("omit speech_language to follow presentation_language")
            if require_complete and key in _REQUIRED:
                raise ValueError(f"config.json {key} must be chosen from the user request before production")
            continue
        if not isinstance(language, str) or not _LANGUAGE_TAG.fullmatch(language) or language.lower() == "auto":
            raise ValueError(f"config.json {key} must be an explicit language tag such as zh or en, or null while undecided")
    count = value["target_page_count"]
    if count is not None and (type(count) is not int or count < 1):
        raise ValueError("config.json target_page_count must be a positive integer or null")
    return dict(value)


def read_config(root: Path, *, require_complete: bool = False) -> dict:
    path = Path(root) / "config.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("config.json is missing; use a new workspace or an explicitly migrated workspace, not Markdown fallback") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"config.json is not valid UTF-8 JSON: {exc}") from exc
    return validate_config(value, require_complete=require_complete)


def initialize_config(
    root: Path, *, presentation_language: str | None = None,
    target_page_count: int | None = None,
) -> dict:
    path = Path(root) / "config.json"
    value = validate_config({
        "working_language": None,
        "presentation_language": presentation_language,
        "target_page_count": target_page_count,
    })
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    except FileExistsError:
        return read_config(root)
    return value
