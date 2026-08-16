#!/usr/bin/env python3
"""Build and validate a portable, deck-local WOFF2 font bundle.

Authoring renders can use fonts installed on the worker, but ``present.html``
must not depend on the viewer having those fonts.  This script resolves the
font roles actually used by one deck to redistributable families, subsets the
deck characters, writes ``assets/fonts/``, and injects a bounded ``@font-face``
block into the deck's root ``base.css``.
"""
from __future__ import annotations

from dataclasses import dataclass
import glob
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


BUNDLE_START = "/* DECK_FONT_BUNDLE_START */"
BUNDLE_END = "/* DECK_FONT_BUNDLE_END */"


@dataclass(frozen=True)
class Face:
    source_names: tuple[str, ...]
    weight: str
    style: str = "normal"


# Delivery is intentionally limited to families whose font files can be
# redistributed with the deck.  Local display fonts outside this list fall
# through to the next allowed family in their CSS stack.
FAMILY_FACES: dict[str, tuple[Face, ...]] = {
    "Noto Sans SC": (
        Face(("NotoSansSC.ttf", "NotoSansSC-Regular.ttf", "NotoSansCJKsc-Regular.otf"), "400"),
        Face(("NotoSansSC-Bold.ttf", "NotoSansSC-Bold.otf", "NotoSansSC.ttf"), "700"),
        Face(("NotoSansSC-900.ttf", "NotoSansSC.ttf", "NotoSansCJKsc-Black.otf"), "900"),
    ),
    "Noto Serif SC": (
        Face(("NotoSerifSC.ttf", "NotoSerifSC-Regular.otf", "NotoSerifCJKsc-Regular.otf"), "400"),
        Face(("NotoSerifSC-Bold.otf", "NotoSerifSC.ttf", "NotoSerifCJKsc-Bold.otf"), "700"),
    ),
    "IBM Plex Mono": (
        Face(("IBMPlexMono-Regular.ttf",), "400"),
        Face(("IBMPlexMono-SemiBold.ttf",), "600"),
    ),
    "Archivo": (Face(("Archivo.ttf",), "100 900"),),
    "Fraunces": (Face(("Fraunces.ttf",), "100 900"),),
    "LXGW WenKai": (
        Face(("LXGWWenKai-Regular.ttf",), "400 700"),
    ),
    "Smiley Sans": (Face(("SmileySans-Oblique.ttf",), "100 900", "oblique"),),
    "Ma Shan Zheng": (Face(("MaShanZheng-Regular.ttf",), "400"),),
    "ZCOOL KuaiLe": (Face(("ZCOOLKuaiLe-Regular.ttf",), "400"),),
    "Patrick Hand": (Face(("PatrickHand-Regular.ttf",), "400"),),
    "Caveat": (Face(("Caveat.ttf",), "400 700"),),
    "Architects Daughter": (Face(("ArchitectsDaughter-Regular.ttf",), "400"),),
    "Indie Flower": (Face(("IndieFlower-Regular.ttf",), "400"),),
}

GENERIC_FAMILIES = {
    "serif", "sans-serif", "monospace", "cursive", "fantasy",
    "system-ui", "ui-sans-serif", "ui-serif", "ui-monospace",
}

DEFAULT_TOKENS = {
    "--font-sans", "--font-body", "--font-title", "--font-display",
    "--font-number", "--font-mono",
}

TOKEN_FALLBACKS = {
    "--font-write": "LXGW WenKai",
    "--font-write-cursive": "LXGW WenKai",
    "--font-round": "ZCOOL KuaiLe",
    "--font-jotter": "ZCOOL KuaiLe",
}


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[no-untyped-def]
        if tag.lower() in {"style", "script", "template"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"style", "script", "template"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _atomic_write(path: Path, data: str | bytes) -> None:
    encoded = data if isinstance(data, bytes) else data.encode("utf-8")
    if path.is_file() and path.read_bytes() == encoded:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    kwargs = {} if isinstance(data, bytes) else {"encoding": "utf-8"}
    with tempfile.NamedTemporaryFile(mode=mode, dir=path.parent, delete=False, **kwargs) as handle:
        temporary = Path(handle.name)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _without_bundle(css: str) -> str:
    pattern = re.compile(
        re.escape(BUNDLE_START) + r".*?" + re.escape(BUNDLE_END) + r"\s*",
        flags=re.S,
    )
    return pattern.sub("", css).lstrip()


def _font_source_dirs() -> list[Path]:
    configured = os.environ.get("PPT_FONT_SOURCE_DIRS", "").strip()
    values = [Path(item).expanduser() for item in configured.split(os.pathsep) if item]
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "fonts"
        if candidate.is_dir():
            values.append(candidate)
    values.extend(
        (
            Path.home() / ".fonts",
            Path.home() / ".local/share/fonts",
            Path("/usr/share/fonts/opentype/noto"),
            Path("/usr/share/fonts/truetype/noto"),
        )
    )
    result: list[Path] = []
    for value in values:
        resolved = value.resolve()
        if resolved not in result:
            result.append(resolved)
    return result


def _find_source(face: Face, source_dirs: list[Path]) -> Path:
    for directory in source_dirs:
        for name in face.source_names:
            candidate = directory / name
            if candidate.is_file() and candidate.stat().st_size:
                return candidate
    raise FileNotFoundError(
        f"missing delivery font source {list(face.source_names)!r}; searched "
        + ", ".join(str(item) for item in source_dirs)
    )


def _family_available(family: str, source_dirs: list[Path]) -> bool:
    try:
        for face in FAMILY_FACES[family]:
            _find_source(face, source_dirs)
        return True
    except FileNotFoundError:
        return False


def _token_definitions(css: str) -> dict[str, str]:
    return {
        name.lower(): value.strip()
        for name, value in re.findall(
            r"(?m)(--font-[a-z0-9-]+)\s*:\s*([^;{}]+);", css, flags=re.I
        )
    }


def _family_candidates(value: str) -> list[str]:
    result: list[str] = []
    for part in value.split(","):
        family = re.sub(r"^[\"']|[\"']$", "", part.strip())
        if not family or family.lower() in GENERIC_FAMILIES or family.startswith("var("):
            continue
        result.append(family)
    return result


def _resolve_token(
    token: str,
    definitions: dict[str, str],
    seen: set[str] | None = None,
) -> str:
    token = token.lower()
    seen = set(seen or ())
    if token in seen:
        raise ValueError(f"font token cycle includes {token}")
    seen.add(token)
    value = definitions.get(token, "")
    for family in _family_candidates(value):
        if family in FAMILY_FACES:
            return family
    for reference in re.findall(r"var\((--font-[a-z0-9-]+)", value, flags=re.I):
        family = _resolve_token(reference, definitions, seen)
        if family:
            return family
    if token in TOKEN_FALLBACKS:
        return TOKEN_FALLBACKS[token]
    if "mono" in token or "number" in token:
        return "IBM Plex Mono"
    if "serif" in token:
        return "Noto Serif SC"
    return "Noto Sans SC"


def _active_tokens(css: str, fragments: list[str]) -> dict[str, str]:
    markup = "\n".join(fragments)
    definitions = _token_definitions(css)
    tokens = set(DEFAULT_TOKENS)
    tokens.update(
        item.lower()
        for item in re.findall(r"var\((--font-[a-z0-9-]+)", markup, flags=re.I)
    )
    source_dirs = _font_source_dirs()
    fallback = "Noto Sans SC"
    if not _family_available(fallback, source_dirs):
        raise FileNotFoundError("Noto Sans SC is required as the portable font fallback")
    resolved = {token: _resolve_token(token, definitions) for token in sorted(tokens)}
    return {
        token: family if _family_available(family, source_dirs) else fallback
        for token, family in resolved.items()
    }


def _visible_characters(fragments: list[str]) -> str:
    parser = _VisibleText()
    for fragment in fragments:
        parser.feed(fragment)
    baseline = "".join(chr(code) for code in range(0x20, 0x7F))
    baseline += " ·—–→←≈≤≥℃°％：；，。！？（）【】《》“”‘’…"
    return "".join(sorted(set("".join(parser.parts) + baseline)))


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _generic_for(family: str) -> str:
    if family == "IBM Plex Mono":
        return "monospace"
    if family in {"Noto Serif SC", "Fraunces", "LXGW WenKai", "Ma Shan Zheng"}:
        return "serif"
    return "sans-serif"


def _subset(tool: str, source: Path, target: Path, characters_file: Path) -> None:
    result = subprocess.run(
        [
            tool,
            str(source),
            f"--output-file={target}",
            "--flavor=woff2",
            f"--text-file={characters_file}",
            "--layout-features=*",
            "--name-IDs=*",
            "--name-legacy",
            "--name-languages=*",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    if result.returncode or not target.is_file() or not target.stat().st_size:
        detail = (result.stderr or result.stdout or "font subset failed")[-1000:]
        raise RuntimeError(f"pyftsubset failed for {source.name}: {detail}")


def bundle_fonts(root: Path, css: str, fragments: list[str]) -> str:
    root = root.resolve()
    source_css = _without_bundle(css)
    token_families = _active_tokens(source_css, fragments)
    markup = "\n".join(fragments)
    families = set(token_families.values())
    source_dirs = _font_source_dirs()
    for family in FAMILY_FACES:
        if _family_available(family, source_dirs) and re.search(
            rf"(?<![\w-]){re.escape(family)}(?![\w-])", markup, flags=re.I
        ):
            families.add(family)

    tool = shutil.which("pyftsubset")
    if not tool:
        raise FileNotFoundError("pyftsubset is required for portable deck fonts")
    characters = _visible_characters(fragments)
    deck_id = hashlib.sha256(
        (characters + json.dumps(token_families, sort_keys=True, ensure_ascii=False)).encode("utf-8")
    ).hexdigest()[:10]
    delivery_families = {
        family: f"Deck-{deck_id}-{_slug(family)}" for family in sorted(families)
    }

    fonts_dir = root / "assets/fonts"
    fonts_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, str]] = []
    rules: list[str] = []
    with tempfile.TemporaryDirectory(prefix="deck-fonts-") as temporary:
        temporary_dir = Path(temporary)
        characters_file = temporary_dir / "characters.txt"
        characters_file.write_text(characters, encoding="utf-8")
        for family in sorted(families):
            for face in FAMILY_FACES[family]:
                source = _find_source(face, source_dirs)
                provisional = temporary_dir / f"{_slug(family)}-{face.weight.replace(' ', '-')}.woff2"
                _subset(tool, source, provisional, characters_file)
                data = provisional.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                filename = f"{_slug(family)}-{face.weight.replace(' ', '-')}-{digest[:12]}.woff2"
                target = fonts_dir / filename
                _atomic_write(target, data)
                delivery = delivery_families[family]
                records.append(
                    {
                        "source_family": family,
                        "delivery_family": delivery,
                        "weight": face.weight,
                        "style": face.style,
                        "source": source.name,
                        "path": f"assets/fonts/{filename}",
                        "sha256": digest,
                    }
                )
                rules.append(
                    "@font-face {\n"
                    f"  font-family: {json.dumps(delivery)};\n"
                    f"  src: url(\"./assets/fonts/{filename}\") format(\"woff2\");\n"
                    f"  font-style: {face.style};\n"
                    f"  font-weight: {face.weight};\n"
                    "  font-display: block;\n"
                    "}"
                )

    overrides = "\n".join(
        f"  {token}: {json.dumps(delivery_families[family])}, {_generic_for(family)};"
        for token, family in sorted(token_families.items())
    )
    rules.append(":root {\n" + overrides + "\n}")
    bundle = BUNDLE_START + "\n" + "\n\n".join(rules) + "\n" + BUNDLE_END
    manifest = {
        "version": 1,
        "deck_id": deck_id,
        "character_count": len(characters),
        "token_families": token_families,
        "delivery_families": delivery_families,
        "faces": records,
    }
    _atomic_write(fonts_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    # Keep role overrides last so the authoring :root declarations above cannot
    # overwrite the portable Deck family names in the CSS cascade.
    return source_css.rstrip() + "\n\n" + bundle + "\n"


def validate_font_bundle(root: Path) -> list[str]:
    root = root.resolve()
    manifest_path = root / "assets/fonts/manifest.json"
    css_path = root / "base.css"
    errors: list[str] = []
    if not manifest_path.is_file():
        return ["assets/fonts/manifest.json is missing"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"font manifest is unreadable: {exc}"]
    css = css_path.read_text(encoding="utf-8") if css_path.is_file() else ""
    if BUNDLE_START not in css or BUNDLE_END not in css:
        errors.append("base.css does not contain the deck font bundle")
    for record in manifest.get("faces", []):
        relative = str(record.get("path", ""))
        path = root / relative
        if not relative.startswith("assets/fonts/") or not path.is_file() or not path.stat().st_size:
            errors.append(f"bundled font is missing or empty: {relative or '<unset>'}")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != str(record.get("sha256", "")):
            errors.append(f"bundled font hash mismatch: {relative}")
        if str(record.get("delivery_family", "")) not in css:
            errors.append(f"delivery font family is absent from base.css: {record.get('delivery_family')}")
    return errors


def validate_render_freshness(root: Path) -> list[str]:
    """Reject PNGs rendered before their slide HTML or bundled base.css."""
    root = root.resolve()
    css_path = root / "base.css"
    errors: list[str] = []
    for slide in sorted(root.glob("slides/slide_*.html")):
        if ".bak." in slide.name:
            continue
        match = re.search(r"(\d+)", slide.stem)
        if not match:
            continue
        render = root / "renders" / f"slide_{int(match.group(1)):02d}.png"
        if not render.is_file() or not render.stat().st_size:
            errors.append(f"missing render for {slide.name}")
            continue
        newest_source = max(slide.stat().st_mtime, css_path.stat().st_mtime)
        if render.stat().st_mtime + 0.001 < newest_source:
            errors.append(f"stale render: {render.name} predates slide HTML or bundled base.css")
    return errors


def render_all(root: Path) -> None:
    """Re-render every slide after font bundling, using the delivery fonts."""
    root = root.resolve()
    renderer = Path(__file__).resolve().with_name("render.py")
    for slide in sorted(root.glob("slides/slide_*.html")):
        if ".bak." in slide.name:
            continue
        match = re.search(r"(\d+)", slide.stem)
        if not match:
            continue
        target = root / "renders" / f"slide_{int(match.group(1)):02d}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [sys.executable, str(renderer), str(slide), str(target)],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
            timeout=240,
        )
        if result.returncode:
            detail = (result.stderr or result.stdout or "render failed")[-1200:]
            raise RuntimeError(f"portable-font render failed for {slide.name}: {detail}")
        print((result.stdout or f"rendered {target}").strip())


def _read_inputs(root: Path) -> tuple[str, list[str]]:
    css_path = root / "base.css"
    if not css_path.is_file():
        raise FileNotFoundError(f"missing {css_path}")
    files = sorted(
        Path(path) for path in glob.glob(str(root / "slides/slide_*.html"))
        if ".bak." not in Path(path).name
    )
    if not files:
        raise FileNotFoundError(f"no slides found under {root / 'slides'}")
    return css_path.read_text(encoding="utf-8"), [path.read_text(encoding="utf-8") for path in files]


def bundle_workspace(root: Path) -> dict:
    """Bundle one workspace and return its validated manifest."""
    root = root.resolve()
    css, fragments = _read_inputs(root)
    _atomic_write(root / "base.css", bundle_fonts(root, css, fragments))
    errors = validate_font_bundle(root)
    if errors:
        raise RuntimeError("; ".join(errors))
    return json.loads((root / "assets/fonts/manifest.json").read_text(encoding="utf-8"))


def main(argv: list[str]) -> int:
    root = Path(argv[0] if argv else ".").resolve()
    if len(argv) > 1 and argv[1] == "--validate":
        errors = validate_font_bundle(root)
        if errors:
            print("font bundle: FAIL", file=sys.stderr)
            for error in errors:
                print(f"- {error}", file=sys.stderr)
            return 1
        print("font bundle: PASS")
        return 0
    manifest = bundle_workspace(root)
    if "--render" in argv[1:]:
        render_all(root)
        freshness_errors = validate_render_freshness(root)
        if freshness_errors:
            raise RuntimeError("; ".join(freshness_errors))
    print(
        f"font bundle: PASS ({len(manifest['faces'])} faces, "
        f"{manifest['character_count']} characters"
        + (", renders refreshed)" if "--render" in argv[1:] else ")")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
