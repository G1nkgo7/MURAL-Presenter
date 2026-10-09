"""Shared font delivery for live static-deck previews.

Authoring workspaces intentionally stay free of per-deck font copies; portable
subsets are produced only in an isolated export tree.  A live browser preview,
however, cannot see fonts installed on the render worker.  This module exposes
the same approved source faces once, through a cacheable Studio endpoint.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
from functools import lru_cache
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SOURCE_DIRS = tuple(
    dict.fromkeys(
        Path(value).expanduser().resolve()
        for value in (
            *(
                item
                for item in os.environ.get("PPT_FONT_SOURCE_DIRS", "").split(os.pathsep)
                if item
            ),
            str(REPOSITORY_ROOT / "fonts"),
            str(Path.home() / ".fonts"),
            str(Path.home() / ".local/share/fonts"),
            "/usr/share/fonts/opentype/noto",
            "/usr/share/fonts/truetype/noto",
        )
    )
)

# 预览直接消费唯一 Skill 的字体目录，不再维护一份会漂移的名称/字重表。
_font_spec = importlib.util.spec_from_file_location(
    'mural_studio_preview_font_bundle',
    REPOSITORY_ROOT / 'skills/mural-presenter-v1/scripts/_internal/font_bundle.py',
)
_font_bundle = importlib.util.module_from_spec(_font_spec)
sys.modules[_font_spec.name] = _font_bundle
_font_spec.loader.exec_module(_font_bundle)
FACE_SPECS = tuple(
    (family, face.source_names, face.weight, face.style)
    for family, faces in _font_bundle.FAMILY_FACES.items()
    for face in faces
)


def _find(candidates: tuple[str, ...]) -> Path | None:
    for directory in SOURCE_DIRS:
        for name in candidates:
            candidate = directory / name
            try:
                if candidate.is_file() and candidate.stat().st_size:
                    return candidate
            except OSError:
                continue
    return None


@lru_cache(maxsize=1)
def catalog() -> dict[str, Path]:
    result: dict[str, Path] = {}
    for _family, candidates, _weight, _style in FACE_SPECS:
        source = _find(candidates)
        if source is not None:
            result.setdefault(source.name, source)
    return result


@lru_cache(maxsize=1)
def version() -> str:
    digest = hashlib.sha256()
    for name, path in sorted(catalog().items()):
        try:
            stat = path.stat()
        except OSError:
            continue
        digest.update(f"{name}:{stat.st_size}:{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()[:16]


@lru_cache(maxsize=1)
def stylesheet() -> str:
    available = catalog()
    rules: list[str] = []
    revision = version()
    for family, candidates, weight, style in FACE_SPECS:
        source = next((available.get(name) for name in candidates if name in available), None)
        if source is None:
            continue
        rules.append(
            "@font-face {\n"
            f"  font-family: {family!r};\n"
            f"  src: url('/api/preview-fonts/{source.name}?v={revision}') format('truetype');\n"
            f"  font-weight: {weight};\n"
            f"  font-style: {style};\n"
            "  font-display: block;\n"
            "}"
        )
    return "\n\n".join(rules) + "\n"


def inject(html: str) -> str:
    """Inject the shared preview stylesheet without mutating the deck file."""
    marker = 'name="studio-preview-fonts"'
    if marker in html:
        return html
    tag = (
        '<meta name="studio-preview-fonts" content="1">\n'
        f'<link rel="stylesheet" href="/api/preview-fonts.css?v={version()}">\n'
    )
    if "</head>" in html:
        return html.replace("</head>", tag + "</head>", 1)
    return tag + html
