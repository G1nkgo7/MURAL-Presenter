"""Drop inline binary payloads from text before they enter traces or training."""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
from typing import Mapping


_DATA_URL_RE = re.compile(
    r"data:(?P<meta>[^,\s\"'<>)]*;base64),(?P<data>[A-Za-z0-9+/]+={0,2})",
    re.IGNORECASE,
)
_BARE_BINARY_RE = re.compile(
    r"(?<![A-Za-z0-9+/=])(?P<data>[A-Za-z0-9+/]{4096,}={0,2})(?![A-Za-z0-9+/=])"
)


def _decode_base64(data: str) -> bytes | None:
    value = "".join(str(data).split())
    if not value:
        return None
    candidates = [value]
    padding = (-len(value)) % 4
    if padding:
        candidates.append(value + ("=" * padding))
    for candidate in candidates:
        try:
            return base64.b64decode(candidate, validate=True)
        except (binascii.Error, ValueError):
            continue
    return None


def _placeholder(data: str, *, mime: str | None = None) -> str:
    payload = _decode_base64(data)
    parts = ["inline-binary-omitted"]
    if mime:
        parts.append(f"mime={mime}")
    if payload is None:
        parts.append(f"chars={len(data)}")
    else:
        parts.append(f"sha256={hashlib.sha256(payload).hexdigest()}")
        parts.append(f"bytes={len(payload)}")
    return "[" + ";".join(parts) + "]"


def _looks_like_bare_binary(data: str) -> bool:
    """Avoid treating ordinary long alphabetic evidence as inline binary."""
    return any(char.isdigit() or char in "+/=" for char in data)


def scrub_inline_base64_text(text: str) -> str:
    """Remove data URLs and long bare binary-looking base64 chunks from text."""
    if not isinstance(text, str) or not text:
        return text

    def replace_data_url(match: re.Match[str]) -> str:
        meta = match.group("meta") or ""
        mime = meta.split(";", 1)[0].strip() or "application/octet-stream"
        return _placeholder(match.group("data"), mime=mime)

    scrubbed = _DATA_URL_RE.sub(replace_data_url, text)

    def replace_bare(match: re.Match[str]) -> str:
        data = match.group("data")
        if not _looks_like_bare_binary(data):
            return data
        if _decode_base64(data) is None:
            return data
        return _placeholder(data)

    return _BARE_BINARY_RE.sub(replace_bare, scrubbed)


def scrub_inline_base64(value: object) -> object:
    """Recursively scrub inline textual binary while preserving structure."""
    if isinstance(value, str):
        return scrub_inline_base64_text(value)
    if isinstance(value, Mapping):
        return {key: scrub_inline_base64(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [scrub_inline_base64(item) for item in value]
    if isinstance(value, list):
        return [scrub_inline_base64(item) for item in value]
    return value
