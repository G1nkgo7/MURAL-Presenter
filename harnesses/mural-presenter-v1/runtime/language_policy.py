"""Runtime language policy helpers for MURAL agents."""
from __future__ import annotations

import re
from typing import Any, Mapping


LANGUAGE_POLICY_VERSION = "mural-language-policy-v1"

LANGUAGE_REMINDER_TOOLS = frozenset({
    "delegate_submit",
    "delegate_wait",
    "inspect",
    "request_orchestrator",
    "respond_owner_request",
    "verification_submit",
})


def _language_value(config: Mapping[str, object] | None, key: str) -> str:
    if not isinstance(config, Mapping):
        return ""
    value = config.get(key)
    if isinstance(value, str):
        return value.strip()
    return ""


def _base_language(value: str) -> str:
    return str(value or "").strip().lower().replace("_", "-").split("-")[0]


def _language_label(value: str) -> str:
    base = _base_language(value)
    if base == "zh":
        return "Chinese"
    if base == "en":
        return "English"
    return value or "unset"


def language_policy_metadata(config: Mapping[str, object] | None) -> dict[str, str]:
    """Return stable trace/context metadata for language enforcement."""
    working = _language_value(config, "working_language")
    presentation = _language_value(config, "presentation_language")
    return {
        "language_policy_version": LANGUAGE_POLICY_VERSION,
        "working_language": working,
        "presentation_language": presentation,
    }


def language_policy_prompt(
    config: Mapping[str, object] | None,
    *,
    audience: str = "agent",
) -> str:
    """Build the prompt section injected into agent, critic, and compact requests."""
    meta = language_policy_metadata(config)
    working = meta["working_language"]
    presentation = meta["presentation_language"]
    working_label = _language_label(working)
    presentation_label = _language_label(presentation)
    lines = [
        "## Runtime language policy",
        f"- policy_version: {LANGUAGE_POLICY_VERSION}",
        f"- working_language: {working or 'unset'} ({working_label})",
        f"- presentation_language: {presentation or 'unset'} ({presentation_label})",
        "- Write all visible collaboration/process output in working_language: progress summaries, verification judgements, handoffs, final_text, requests for help, and interpretations of tool results.",
        "- This includes short status updates after delegate_wait/orchestration_update; use labels and prose in working_language, not inherited templates such as 'Status update', 'complete', or 'open items' unless working_language is English.",
        "- Page and audience-facing slide text must use presentation_language.",
        "- Preserve original strings for proper nouns, scientific names, code, file paths, CSS classes, tool names, quoted source snippets, URLs, and source titles when needed.",
        "- Do not treat older English assistant prose in history as the language policy; continue in working_language from now on.",
    ]
    if not working or not presentation:
        lines.append("- If a language is unset, follow the explicit creation config once available; otherwise mirror the user's language.")
    kind = str(audience or "agent").strip().lower()
    if kind == "critic":
        lines.extend([
            "- Visual Critic output must use working_language.",
            "- Inspect visible page text against presentation_language, while preserving literal page labels or quoted problem text when necessary.",
            "- Report only must-fix blockers and validation-relevant facts that cannot be confirmed from pixels in working_language; do not list optional polish suggestions.",
        ])
    elif kind == "compact":
        lines.extend([
            "- The compacted handoff must be written in working_language.",
            "- Historical English process prose in compacted history is not a policy signal.",
        ])
    return "\n".join(lines)


def visible_output_language_reminder(
    config_or_metadata: Mapping[str, object] | None,
    *,
    tool_names: tuple[str, ...] | list[str] | set[str] = (),
    trigger: str = "tool_result",
) -> str:
    """Return a short user-visible-output reminder after high-risk runtime events."""
    metadata = config_or_metadata if isinstance(config_or_metadata, Mapping) else {}
    if isinstance(metadata.get("creation_config"), Mapping):
        config = metadata["creation_config"]
    else:
        config = metadata
    working = _language_value(config, "working_language") or _language_value(metadata, "working_language")
    presentation = (
        _language_value(config, "presentation_language")
        or _language_value(metadata, "presentation_language")
    )
    if not working:
        return ""
    names = {str(name or "") for name in tool_names}
    normalized_trigger = str(trigger or "").strip()
    if normalized_trigger == "tool_result" and names and not (names & LANGUAGE_REMINDER_TOOLS):
        return ""
    base = _base_language(working)
    if base == "zh":
        return (
            f"[Harness language reminder] 当前 working_language={working}；下一条可见协作输出"
            "（状态更新、工具结果解释、验收判断、handoff、final_text）必须使用中文。"
            "不要沿用历史或工具回执里的英文状态模板，例如 “Status update / complete / open items”。"
            f"幻灯片上屏文字仍按 presentation_language={presentation or 'unset'}；"
            "专名、路径、代码、工具名和来源原文可保留原文。"
        )
    if base == "en":
        return (
            f"[Harness language reminder] working_language={working}; the next visible collaboration output "
            "(status update, tool-result interpretation, verification judgement, handoff, or final_text) "
            "must be in English. Do not switch languages because tool results, briefs, or history use another language. "
            f"Audience-facing slide text still follows presentation_language={presentation or 'unset'}; "
            "preserve proper nouns, paths, code, tool names, and quoted source text as written."
        )
    return (
        f"[Harness language reminder] working_language={working}; write the next visible collaboration output "
        f"in that language. Audience-facing slide text follows presentation_language={presentation or 'unset'}; "
        "preserve proper nouns, paths, code, tool names, and quoted source text as written."
    )


def append_language_reminder(messages: list[dict[str, Any]], reminder: str, *, force: bool = False) -> None:
    """Keep one unchanged policy reminder per live context, including after compaction."""
    if not reminder:
        return
    if force:
        messages.append({"role": "user", "content": [{"type": "text", "text": reminder}]})
        return
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        parts = content if isinstance(content, list) else [{"type": "text", "text": content}]
        for part in reversed(parts):
            if not isinstance(part, Mapping) or part.get("type") != "text":
                continue
            text = str(part.get("text") or "")
            marker = text.find("[Harness language reminder]")
            if marker >= 0:
                if text[marker:] == reminder:
                    return
                messages.append({"role": "user", "content": [{"type": "text", "text": reminder}]})
                return
    messages.append({"role": "user", "content": [{"type": "text", "text": reminder}]})


def extract_language_policy(system: str) -> str:
    """Extract the injected policy section from a full system prompt."""
    text = str(system or "")
    marker = "## Runtime language policy"
    start = text.find(marker)
    if start < 0:
        return ""
    lines = text[start:].splitlines()
    collected: list[str] = []
    for index, line in enumerate(lines):
        if index and line.startswith("## "):
            break
        collected.append(line)
    return "\n".join(collected).strip()


def _strip_exempt_text(value: str) -> str:
    text = re.sub(r"```.*?```", " ", value, flags=re.DOTALL)
    text = re.sub(r"`[^`]*`", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\b[\w./-]+\.(?:html|css|js|json|md|png|jpg|jpeg|webp|svg)\b", " ", text)
    return text


def language_drift_warning(
    text: str,
    config_or_metadata: Mapping[str, object] | None,
) -> dict[str, object] | None:
    """Detect large English visible prose when the configured working language is Chinese."""
    metadata = config_or_metadata if isinstance(config_or_metadata, Mapping) else {}
    working = str(metadata.get("working_language") or "").strip()
    presentation = str(metadata.get("presentation_language") or "").strip()
    if not working and isinstance(metadata.get("creation_config"), Mapping):
        creation_config = metadata["creation_config"]
        working = str(creation_config.get("working_language") or "").strip()
        presentation = str(creation_config.get("presentation_language") or presentation).strip()
    if _base_language(working) != "zh":
        return None
    cleaned = _strip_exempt_text(str(text or "")).strip()
    if len(cleaned) < 160:
        return None
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", cleaned))
    english_words = re.findall(r"\b[A-Za-z][A-Za-z'-]{2,}\b", cleaned)
    alpha_chars = len(re.findall(r"[A-Za-z]", cleaned))
    visible_chars = len(re.sub(r"\s", "", cleaned))
    alpha_ratio = (alpha_chars / visible_chars) if visible_chars else 0.0
    if cjk_count >= 20:
        return None
    if len(english_words) < 25 and alpha_ratio < 0.55:
        return None
    return {
        "schema": "mural.language-drift.v1",
        "code": "language_drift",
        "language_policy_version": LANGUAGE_POLICY_VERSION,
        "working_language": working,
        "presentation_language": presentation,
        "english_word_count": len(english_words),
        "cjk_char_count": cjk_count,
        "alpha_ratio": round(alpha_ratio, 3),
        "message": "visible assistant prose appears to drift away from working_language=zh",
        "sample": cleaned[:240],
    }
