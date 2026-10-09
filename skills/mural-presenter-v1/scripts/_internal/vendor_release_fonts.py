#!/usr/bin/env python3
"""Materialize the release ``fonts/`` directory from the SHA-locked manifest.

The repository ignores ``/fonts/`` (fonts are large, redistributable, and
distributed out of band).  ``FONT-MANIFEST.lock.json`` is the tracked source of
truth: it pins every whitelisted face to a google/fonts commit + SHA256.  This
script re-downloads exactly those files from the pinned revision and verifies
each byte-for-byte, so the hermetic release dir can be rebuilt anywhere.

Usage:
    python vendor_release_fonts.py [--dest DIR] [--check-only]

``--check-only`` verifies existing files against the lock without downloading.
This is the ONLY font script that touches the network; the audit and the
bundler never do.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path


HERE = Path(__file__).resolve().parent
LOCK_PATH = HERE / "FONT-MANIFEST.lock.json"
# HERE is .../skills/mural-presenter-v1/scripts/_internal; repo root is 4 up.
DEFAULT_DEST = HERE.parents[3] / "fonts"
RAW_BASE = "https://raw.githubusercontent.com/google/fonts/{rev}/ofl/{slug}/{name}"

# google/fonts ``ofl/<slug>`` directory for each family whose source is a
# google/fonts tree URL.  Families sourced elsewhere (verified separately) are
# listed here with their upstream raw URL template instead of a google slug.
_GOOGLE_SLUGS = {
    "Architects Daughter": "architectsdaughter", "Archivo": "archivo",
    "Barlow Condensed": "barlowcondensed", "Bebas Neue": "bebasneue",
    "Bungee": "bungee", "Caveat": "caveat", "Cormorant Garamond": "cormorantgaramond",
    "DM Sans": "dmsans", "Dancing Script": "dancingscript", "Figtree": "figtree",
    "Fraunces": "fraunces", "Geist": "geist", "Geist Mono": "geistmono",
    "IBM Plex Mono": "ibmplexmono", "IBM Plex Sans": "ibmplexsans",
    "Indie Flower": "indieflower", "Inter": "inter", "Inter Tight": "intertight",
    "JetBrains Mono": "jetbrainsmono", "Kalam": "kalam", "League Gothic": "leaguegothic",
    "Liu Jian Mao Cao": "liujianmaocao", "Long Cang": "longcang",
    "Ma Shan Zheng": "mashanzheng", "Manrope": "manrope", "Montserrat": "montserrat",
    "Open Sans": "opensans", "Oswald": "oswald", "Patrick Hand": "patrickhand",
    "Playfair Display": "playfairdisplay", "Plus Jakarta Sans": "plusjakartasans",
    "Poppins": "poppins", "Roboto": "roboto", "Roboto Mono": "robotomono",
    "Sacramento": "sacramento", "Shadows Into Light": "shadowsintolight",
    "Sora": "sora", "Space Grotesk": "spacegrotesk", "Spectral": "spectral",
    "Syne": "syne", "Unbounded": "unbounded", "ZCOOL KuaiLe": "zcoolkuaile",
    "ZCOOL QingKe HuangYou": "zcoolqingkehuangyou", "ZCOOL XiaoWei": "zcoolxiaowei",
    "Zhi Mang Xing": "zhimangxing",
    "Noto Sans SC": "notosanssc", "Noto Serif SC": "notoserifsc",
    # mixture-v2 parity families sourced from google/fonts.
    "Chakra Petch": "chakrapetch", "Cinzel": "cinzel", "Comic Neue": "comicneue",
    "Cormorant": "cormorant", "EB Garamond": "ebgaramond", "Marcellus": "marcellus",
    "Zilla Slab": "zillaslab", "Archivo Black": "archivoblack",
    "IBM Plex Serif": "ibmplexserif", "Press Start 2P": "pressstart2p",
    "Orbitron": "orbitron", "DM Serif Display": "dmserifdisplay",
    "Roboto Condensed": "robotocondensed", "VT323": "vt323", "Baloo 2": "baloo2",
    "Bodoni Moda": "bodonimoda",
}

# Families whose upstream is NOT google/fonts.  These were vendored from the
# sources below and are verified by ``--check-only``; re-downloading them needs
# the project's own release assets, so the script reports them for manual
# refresh rather than guessing a fragile release-asset URL.
_NON_GOOGLE_SOURCES = {
    "LXGW WenKai": "https://github.com/lxgw/LxgwWenKai/releases",
    "Smiley Sans": "https://github.com/atelier-anchor/smiley-sans/releases",
    "Xiaolai": "https://github.com/lxgw/kose-font/releases/tag/v3.126",
    # mixture-v2 parity families vendored out of band (OSS releases, npm mirrors
    # of free-commercial Alibaba/DingTalk fonts, 100font/爱给网 uploads, and a
    # paid pixel font). Their real license/scope is recorded in the lock; the
    # vendor script only reports where to re-fetch them.
    "Ark Pixel": "https://github.com/TakWolf/ark-pixel-font/releases",
    "Fusion Pixel": "https://github.com/TakWolf/fusion-pixel-font/releases",
    "Sarasa Mono SC": "https://github.com/be5invis/Sarasa-Gothic/releases",
    "Zpix": "https://github.com/SolidZORO/zpix-pixel-font/releases",
    "Alimama FangYuanTi VF": "https://www.alibabafonts.com (阿里妈妈方圆体 VF; free-commercial EULA)",
    "Alibaba PuHuiTi 3.0": "https://www.alibabafonts.com (阿里巴巴普惠体 3.0; free-commercial EULA)",
    "Alimama ShuHeiTi": "https://www.alibabafonts.com (阿里妈妈数黑体; free-commercial EULA)",
    "DingTalk JinBuTi": "https://www.dingtalk.com (钉钉进步体; free-commercial EULA)",
    "Lovely Little Jelly": "https://www.100font.com (软萌小果冻体)",
    "今年也要加油鸭": "https://www.100font.com (今年也要加油鸭)",
    "段宁硬笔楷书粗体": "https://www.100font.com (段宁硬笔楷书粗体)",
    "叶根友钢笔行书简体": "https://www.aigei.com (叶根友钢笔行书简体; internal-only)",
    "迷你简硬笔行书": "https://www.aigei.com (迷你简硬笔行书; internal-only)",
}

# Some families ship upstream under a bracketed variable-axis filename but are
# vendored locally under a plain name.  When the locked filename 404s upstream,
# the resolver falls back to listing the slug directory and matching by SHA, so
# this map is only a fast-path hint and never needs to be exhaustive.
_UPSTREAM_NAME = {
    "CormorantGaramond.ttf": "CormorantGaramond[wght].ttf",
    "Unbounded.ttf": "Unbounded[wght].ttf",
    "LeagueGothic.ttf": "LeagueGothic[wdth].ttf",
    "Syne.ttf": "Syne[wght].ttf",
    "Sora.ttf": "Sora[wght].ttf",
    "SpaceGrotesk.ttf": "SpaceGrotesk[wght].ttf",
    "Montserrat.ttf": "Montserrat[wght].ttf",
    "Oswald.ttf": "Oswald[wght].ttf",
    "DMSans.ttf": "DMSans[opsz,wght].ttf",
    "Manrope.ttf": "Manrope[wght].ttf",
    "Archivo.ttf": "Archivo[wdth,wght].ttf",
    "Fraunces.ttf": "Fraunces[SOFT,WONK,opsz,wght].ttf",
    "PlayfairDisplay.ttf": "PlayfairDisplay[wght].ttf",
    "DancingScript.ttf": "DancingScript[wght].ttf",
    "Caveat.ttf": "Caveat[wght].ttf",
    "Kalam-Regular.ttf": "Kalam-Regular.ttf",
    # mixture-v2 parity: files vendored under a plain name but bracketed upstream.
    "Cinzel.ttf": "Cinzel[wght].ttf",
    "EBGaramond.ttf": "EBGaramond[wght].ttf",
    "Cormorant.ttf": "Cormorant[wght].ttf",
    "ZillaSlab.ttf": "ZillaSlab-Regular.ttf",
    "Marcellus.ttf": "Marcellus-Regular.ttf",
    "ChakraPetch.ttf": "ChakraPetch-Regular.ttf",
}

_API_BASE = "https://api.github.com/repos/google/fonts/contents/ofl/{slug}?ref={rev}"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fetch(url: str) -> bytes:
    return urllib.request.urlopen(url, timeout=60).read()


def _list_slug_ttfs(slug: str, rev: str) -> list[str]:
    """List every ``.ttf`` under a google/fonts ``ofl/<slug>`` at ``rev``."""
    try:
        payload = json.loads(_fetch(_API_BASE.format(slug=slug, rev=rev)).decode("utf-8"))
    except Exception:  # pragma: no cover - network path
        return []
    return [item["name"] for item in payload if str(item.get("name", "")).endswith(".ttf")]


def _download_matching(slug: str, rev: str, locked_name: str, want: str) -> bytes | None:
    """Download the upstream file whose SHA matches the lock, robust to renames."""
    tried = []
    for candidate in (locked_name, _UPSTREAM_NAME.get(locked_name)):
        if not candidate or candidate in tried:
            continue
        tried.append(candidate)
        url = RAW_BASE.format(rev=rev, slug=slug, name=urllib.parse.quote(candidate))
        try:
            data = _fetch(url)
        except Exception:  # pragma: no cover - network path
            continue
        if _sha256(data) == want:
            return data
    # Fall back to listing the slug and matching any .ttf by SHA.
    for name in _list_slug_ttfs(slug, rev):
        if name in tried:
            continue
        url = RAW_BASE.format(rev=rev, slug=slug, name=urllib.parse.quote(name))
        try:
            data = _fetch(url)
        except Exception:  # pragma: no cover - network path
            continue
        if _sha256(data) == want:
            return data
    return None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)

    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    rev = lock["google_fonts_rev"]
    dest = args.dest
    dest.mkdir(parents=True, exist_ok=True)

    ok, failed = 0, []
    for family, entry in lock["families"].items():
        slug = _GOOGLE_SLUGS.get(family)
        for face in entry["faces"]:
            name = face["file"]
            target = dest / name
            want = face["sha256"]
            if target.is_file() and _sha256(target.read_bytes()) == want:
                ok += 1
                continue
            if args.check_only:
                failed.append(f"{family}: {name} missing or sha mismatch")
                continue
            if not slug:
                hint = _NON_GOOGLE_SOURCES.get(family, "unknown upstream")
                failed.append(f"{family}: {name} is not on google/fonts; fetch from {hint}")
                continue
            data = _download_matching(slug, rev, name, want)
            if data is None:
                failed.append(f"{family}: {name} no upstream file matched the locked sha")
                continue
            target.write_bytes(data)
            ok += 1

    print(f"fonts verified/vendored: {ok}")
    for line in failed:
        print(f"  FAIL {line}", file=sys.stderr)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
