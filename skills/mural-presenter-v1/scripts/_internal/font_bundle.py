#!/usr/bin/env python3
"""Resolve shared authoring fonts and build portable WOFF2 export bundles.

Authoring reads the workspace's pinned source objects without copying them into
each workspace. Portable export resolves the same roles, subsets the deck
characters inside an isolated staging tree, and injects a bounded
``@font-face`` block into the exported ``base.css``.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from functools import lru_cache
import glob
import hashlib
from html.parser import HTMLParser
from html import unescape
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Iterator
from urllib.parse import unquote, urlsplit, urljoin


BUNDLE_START = "/* DECK_FONT_BUNDLE_START */"
BUNDLE_END = "/* DECK_FONT_BUNDLE_END */"
AUTHORING_START = "/* SHARED_AUTHORING_FONTS_START */"
AUTHORING_END = "/* SHARED_AUTHORING_FONTS_END */"
OFL_TEMPLATE = Path(__file__).resolve().parents[2] / "assets/licenses/OFL-1.1.txt"
GOOGLE_FONTS_REV = "2796410152d4f9524b68ed46e69c1b60f8e0f7c3"

# The single redistributable font directory shipped with a release, and the
# SHA-locked manifest that pins every vendored face to a known-good file.
RELEASE_FONT_DIR = Path(__file__).resolve().parents[4] / "fonts"
FONT_LOCK_PATH = Path(__file__).resolve().with_name("FONT-MANIFEST.lock.json")


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
        Face(("NotoSansSC[wght].ttf", "NotoSansSC.ttf"), "100 900"),
    ),
    "Noto Serif SC": (
        Face(("NotoSerifSC[wght].ttf", "NotoSerifSC.ttf"), "200 900"),
    ),
    "IBM Plex Mono": (
        Face(("IBMPlexMono-Regular.ttf",), "400"),
        Face(("IBMPlexMono-SemiBold.ttf",), "600"),
    ),
    "IBM Plex Sans": (
        Face(("IBMPlexSans_2.ttf",), "400"),
        Face(("IBMPlexSans_1.ttf",), "500"),
    ),
    "Archivo": (Face(("Archivo.ttf", "Archivo[wdth,wght].ttf"), "100 900"),),
    "Fraunces": (Face(("Fraunces.ttf", "Fraunces[SOFT,WONK,opsz,wght].ttf"), "100 900"),),
    "Spectral": (
        Face(("Spectral-Regular.ttf",), "400"),
        Face(("Spectral-Bold.ttf",), "700"),
    ),
    "Xiaolai": (Face(("Xiaolai-Regular.ttf",), "400"),),
    "LXGW WenKai": (
        Face(("LXGWWenKai-Regular.ttf",), "400"),
        # The official TTF release is a regular face; browsers may
        # synthesize bold while the portable bundle keeps the same glyph design.
        # Prefer the verified regular source here. Some historical authoring
        # images contain a malformed bold cmap; using it makes pyftsubset fail
        # even though the family passed a shallow availability check.
        Face(("LXGWWenKai-Regular.ttf", "LXGWWenKai-Bold.ttf"), "700"),
    ),
    # The official SmileySans-Oblique.ttf is a single static display face:
    # OS/2 weight 400, oblique, with no fvar axis.  Its glyph design already
    # looks heavy; declaring 100–900 falsely marks it as a variable font and
    # makes the strict source validator reject an otherwise valid file.
    "Smiley Sans": (Face(("SmileySans-Oblique.ttf",), "400", "oblique"),),
    "Ma Shan Zheng": (Face(("MaShanZheng-Regular.ttf",), "400"),),
    "ZCOOL KuaiLe": (Face(("ZCOOLKuaiLe-Regular.ttf",), "400"),),
    "ZCOOL QingKe HuangYou": (Face(("ZCOOLQingKeHuangYou-Regular.ttf",), "400"),),
    "Zhi Mang Xing": (Face(("ZhiMangXing-Regular.ttf",), "400"),),
    "Long Cang": (Face(("LongCang-Regular.ttf",), "400"),),
    "Liu Jian Mao Cao": (Face(("LiuJianMaoCao-Regular.ttf",), "400"),),
    "Patrick Hand": (Face(("PatrickHand-Regular.ttf",), "400"),),
    "Caveat": (Face(("Caveat.ttf", "Caveat[wght].ttf"), "400 700"),),
    "Architects Daughter": (Face(("ArchitectsDaughter-Regular.ttf",), "400"),),
    "Indie Flower": (Face(("IndieFlower-Regular.ttf",), "400"),),
    "Dancing Script": (Face(("DancingScript.ttf", "DancingScript[wght].ttf"), "400 700"),),
    "Kalam": (
        Face(("Kalam-Regular.ttf",), "400"),
        Face(("Kalam-Bold.ttf",), "700"),
    ),
    "Shadows Into Light": (Face(("ShadowsIntoLight.ttf",), "400"),),
    "Sacramento": (Face(("Sacramento-Regular.ttf",), "400"),),
    "Montserrat": (Face(("Montserrat.ttf", "Montserrat[wght].ttf"), "100 900"),),
    "Oswald": (Face(("Oswald.ttf", "Oswald[wght].ttf"), "200 700"),),
    "Bebas Neue": (Face(("BebasNeue-Regular.ttf",), "400"),),
    "Space Grotesk": (Face(("SpaceGrotesk.ttf", "SpaceGrotesk[wght].ttf"), "300 700"),),
    "Barlow Condensed": (
        Face(("BarlowCondensed-Regular.ttf",), "400"),
        Face(("BarlowCondensed-SemiBold.ttf",), "600"),
        Face(("BarlowCondensed-Bold.ttf",), "700"),
    ),
    "Playfair Display": (Face(("PlayfairDisplay.ttf", "PlayfairDisplay[wght].ttf"), "400 900"),),
    "Cormorant Garamond": (Face(("CormorantGaramond.ttf", "CormorantGaramond[wght].ttf"), "300 700"),),
    "DM Sans": (Face(("DMSans.ttf", "DMSans[opsz,wght].ttf"), "100 1000"),),
    "Manrope": (Face(("Manrope.ttf", "Manrope[wght].ttf"), "200 800"),),
    "Unbounded": (Face(("Unbounded.ttf", "Unbounded[wght].ttf"), "200 900"),),
    "Bungee": (Face(("Bungee-Regular.ttf",), "400"),),
    "ZCOOL XiaoWei": (Face(("ZCOOLXiaoWei-Regular.ttf",), "400"),),
    "League Gothic": (Face(("LeagueGothic.ttf", "LeagueGothic[wdth].ttf"), "400"),),
    "Syne": (Face(("Syne.ttf", "Syne[wght].ttf"), "400 800"),),
    "Sora": (Face(("Sora.ttf", "Sora[wght].ttf"), "100 800"),),
    # Vendored 2026-09 from google/fonts at GOOGLE_FONTS_REV; each declared
    # weight range matches the file's real wght axis (see FONT-MANIFEST.lock.json).
    "Inter": (Face(("Inter[opsz,wght].ttf",), "100 900"),),
    "Inter Tight": (Face(("InterTight[wght].ttf",), "100 900"),),
    "Geist": (Face(("Geist[wght].ttf",), "100 900"),),
    "Geist Mono": (Face(("GeistMono[wght].ttf",), "100 900"),),
    "JetBrains Mono": (Face(("JetBrainsMono[wght].ttf",), "100 800"),),
    "Poppins": (
        Face(("Poppins-Regular.ttf",), "400"),
        Face(("Poppins-Medium.ttf",), "500"),
        Face(("Poppins-SemiBold.ttf",), "600"),
        Face(("Poppins-Bold.ttf",), "700"),
    ),
    "Roboto": (Face(("Roboto[wdth,wght].ttf",), "100 900"),),
    "Roboto Mono": (Face(("RobotoMono[wght].ttf",), "100 700"),),
    "Open Sans": (Face(("OpenSans[wdth,wght].ttf",), "300 800"),),
    "Figtree": (Face(("Figtree[wght].ttf",), "300 900"),),
    "Plus Jakarta Sans": (Face(("PlusJakartaSans[wght].ttf",), "200 800"),),
    # ------------------------------------------------------------------
    # mixture-v2 parity (2026-09). These faces are shipped for parity with
    # the mixture-v2 corpus. They are NOT selected for OFL portability: the
    # set below deliberately includes non-OFL, internal-only and vendored
    # families (Alibaba/DingTalk free-commercial EULAs, an unlicensed
    # 爱给网 handwriting file, a paid pixel font). Honest source + usage-scope
    # metadata lives in FONT_METADATA; do not fabricate an OFL license for
    # them. Declared weight ranges below match each file's real wght axis /
    # OS/2 weight (see FONT-MANIFEST.lock.json).
    #
    # P0 — mixture-v2 public package families missing from the v1 allowlist.
    "Chakra Petch": (Face(("ChakraPetch.ttf",), "400"),),
    "Cinzel": (Face(("Cinzel.ttf",), "400 900"),),
    "Comic Neue": (Face(("ComicNeue-Regular.ttf",), "400"),),
    # Cormorant is a DISTINCT family from Cormorant Garamond above.
    "Cormorant": (Face(("Cormorant.ttf",), "300 700"),),
    "EB Garamond": (Face(("EBGaramond.ttf",), "400 800"),),
    "Marcellus": (Face(("Marcellus.ttf",), "400"),),
    "Zilla Slab": (Face(("ZillaSlab.ttf",), "400"),),
    "Lovely Little Jelly": (Face(("RuanMengXiaoGuoDong-2.ttf",), "400"),),
    "今年也要加油鸭": (Face(("JinNianYeYaoJiaYouYa-2.ttf",), "400"),),
    "叶根友钢笔行书简体": (Face(("叶根友钢笔行书简体_爱给网_aigei_com.ttf",), "400"),),
    "段宁硬笔楷书粗体": (Face(("DuanNingYingBiKaiShuCuTi-2.ttf",), "400"),),
    "迷你简硬笔行书": (Face(("迷你简硬笔行书字体下载_爱给网_aigei_com.ttf",), "400"),),
    # P1 — case-local design fonts that appeared in mixture-v2 final CSS.
    "Archivo Black": (Face(("ArchivoBlack-Regular.ttf",), "400"),),
    "IBM Plex Serif": (
        Face(("IBMPlexSerif-Regular.ttf",), "400"),
        Face(("IBMPlexSerif-SemiBold.ttf",), "600"),
    ),
    "Press Start 2P": (Face(("PressStart2P-Regular.ttf",), "400"),),
    "Orbitron": (Face(("Orbitron[wght].ttf",), "400 900"),),
    "DM Serif Display": (Face(("DMSerifDisplay-Regular.ttf",), "400"),),
    "Roboto Condensed": (Face(("RobotoCondensed[wght].ttf",), "100 900"),),
    "VT323": (Face(("VT323-Regular.ttf",), "400"),),
    "Baloo 2": (Face(("Baloo2[wght].ttf",), "400 800"),),
    "Bodoni Moda": (Face(("BodoniModa[opsz,wght].ttf",), "400 900"),),
    "Ark Pixel": (Face(("ArkPixel-12px-proportional-zh_cn.ttf",), "400"),),
    "Fusion Pixel": (Face(("FusionPixel-12px-proportional-zh_hans.ttf",), "400"),),
    "Alimama FangYuanTi VF": (Face(("AlimamaFangYuanTiVF.ttf",), "200 700"),),
    "Alibaba PuHuiTi 3.0": (Face(("AlibabaPuHuiTi-3-55-Regular.ttf",), "400"),),
    "DingTalk JinBuTi": (Face(("DingTalkJinBuTi-Regular.ttf",), "400"),),
    "Sarasa Mono SC": (Face(("SarasaMonoSC-Regular.ttf",), "400"),),
    # Alimama ShuHeiTi ships a single Bold weight only.
    "Alimama ShuHeiTi": (Face(("AlimamaShuHeiTi-Bold.ttf",), "700"),),
    "Zpix": (Face(("Zpix.ttf",), "400"),),
}


def _google_font_source(family: str) -> str:
    return f"https://github.com/google/fonts/tree/{GOOGLE_FONTS_REV}/ofl/{family}"


# Every built-in family must carry honest provenance: a non-empty official
# source and a usage-scope note.  This is deliberately narrower than "whatever
# happens to be installed on the authoring machine", but it is NOT an OFL gate:
# mixture-v2 parity fonts (free-commercial EULAs, an unlicensed handwriting
# file, a paid pixel font) are shipped with their true license recorded, never
# relabelled as OFL.  Entries without an explicit ``usage_scope`` are the
# original redistributable OFL set and default to ``redistributable-ofl``.
FONT_LICENSES: dict[str, dict[str, str]] = {
    "Noto Sans SC": {"license": "OFL-1.1", "source": _google_font_source("notosanssc")},
    "Noto Serif SC": {"license": "OFL-1.1", "source": _google_font_source("notoserifsc")},
    "IBM Plex Mono": {"license": "OFL-1.1", "source": _google_font_source("ibmplexmono")},
    "IBM Plex Sans": {"license": "OFL-1.1", "source": _google_font_source("ibmplexsans")},
    "Archivo": {"license": "OFL-1.1", "source": _google_font_source("archivo")},
    "Fraunces": {"license": "OFL-1.1", "source": _google_font_source("fraunces")},
    "Spectral": {"license": "OFL-1.1", "source": _google_font_source("spectral")},
    "Xiaolai": {"license": "OFL-1.1", "source": "https://github.com/lxgw/kose-font/tree/v3.126"},
    "LXGW WenKai": {"license": "OFL-1.1", "source": "https://github.com/lxgw/LxgwWenKai"},
    "Smiley Sans": {"license": "OFL-1.1", "source": "https://github.com/atelier-anchor/smiley-sans"},
    "Ma Shan Zheng": {"license": "OFL-1.1", "source": _google_font_source("mashanzheng")},
    "ZCOOL KuaiLe": {"license": "OFL-1.1", "source": _google_font_source("zcoolkuaile")},
    "ZCOOL QingKe HuangYou": {"license": "OFL-1.1", "source": _google_font_source("zcoolqingkehuangyou")},
    "Zhi Mang Xing": {"license": "OFL-1.1", "source": _google_font_source("zhimangxing")},
    "Long Cang": {"license": "OFL-1.1", "source": _google_font_source("longcang")},
    "Liu Jian Mao Cao": {"license": "OFL-1.1", "source": _google_font_source("liujianmaocao")},
    "Patrick Hand": {"license": "OFL-1.1", "source": _google_font_source("patrickhand")},
    "Caveat": {"license": "OFL-1.1", "source": _google_font_source("caveat")},
    "Architects Daughter": {"license": "OFL-1.1", "source": _google_font_source("architectsdaughter")},
    "Indie Flower": {"license": "OFL-1.1", "source": _google_font_source("indieflower")},
    "Dancing Script": {"license": "OFL-1.1", "source": _google_font_source("dancingscript")},
    "Kalam": {"license": "OFL-1.1", "source": _google_font_source("kalam")},
    "Shadows Into Light": {"license": "OFL-1.1", "source": _google_font_source("shadowsintolight")},
    "Sacramento": {"license": "OFL-1.1", "source": _google_font_source("sacramento")},
    "Montserrat": {"license": "OFL-1.1", "source": _google_font_source("montserrat")},
    "Oswald": {"license": "OFL-1.1", "source": _google_font_source("oswald")},
    "Bebas Neue": {"license": "OFL-1.1", "source": _google_font_source("bebasneue")},
    "Space Grotesk": {"license": "OFL-1.1", "source": _google_font_source("spacegrotesk")},
    "Barlow Condensed": {"license": "OFL-1.1", "source": _google_font_source("barlowcondensed")},
    "Playfair Display": {"license": "OFL-1.1", "source": _google_font_source("playfairdisplay")},
    "Cormorant Garamond": {"license": "OFL-1.1", "source": _google_font_source("cormorantgaramond")},
    "DM Sans": {"license": "OFL-1.1", "source": _google_font_source("dmsans")},
    "Manrope": {"license": "OFL-1.1", "source": _google_font_source("manrope")},
    "Unbounded": {"license": "OFL-1.1", "source": _google_font_source("unbounded")},
    "Bungee": {"license": "OFL-1.1", "source": _google_font_source("bungee")},
    "ZCOOL XiaoWei": {"license": "OFL-1.1", "source": _google_font_source("zcoolxiaowei")},
    "League Gothic": {"license": "OFL-1.1", "source": _google_font_source("leaguegothic")},
    "Syne": {"license": "OFL-1.1", "source": _google_font_source("syne")},
    "Sora": {"license": "OFL-1.1", "source": _google_font_source("sora")},
    "Inter": {"license": "OFL-1.1", "source": _google_font_source("inter")},
    "Inter Tight": {"license": "OFL-1.1", "source": _google_font_source("intertight")},
    "Geist": {"license": "OFL-1.1", "source": _google_font_source("geist")},
    "Geist Mono": {"license": "OFL-1.1", "source": _google_font_source("geistmono")},
    "JetBrains Mono": {"license": "OFL-1.1", "source": _google_font_source("jetbrainsmono")},
    "Poppins": {"license": "OFL-1.1", "source": _google_font_source("poppins")},
    "Roboto": {"license": "OFL-1.1", "source": _google_font_source("roboto")},
    "Roboto Mono": {"license": "OFL-1.1", "source": _google_font_source("robotomono")},
    "Open Sans": {"license": "OFL-1.1", "source": _google_font_source("opensans")},
    "Figtree": {"license": "OFL-1.1", "source": _google_font_source("figtree")},
    "Plus Jakarta Sans": {"license": "OFL-1.1", "source": _google_font_source("plusjakartasans")},
    # ------------------------------------------------------------------
    # mixture-v2 parity fonts (2026-09). ``usage_scope`` is mandatory here and
    # the license is recorded HONESTLY — non-OFL families keep their real terms.
    # P0 public-package families (all OFL on google/fonts at GOOGLE_FONTS_REV,
    # except where noted):
    "Chakra Petch": {"license": "OFL-1.1", "source": _google_font_source("chakrapetch"),
                     "usage_scope": "redistributable-ofl"},
    "Cinzel": {"license": "OFL-1.1", "source": _google_font_source("cinzel"),
               "usage_scope": "redistributable-ofl"},
    "Comic Neue": {"license": "OFL-1.1", "source": _google_font_source("comicneue"),
                   "usage_scope": "redistributable-ofl"},
    "Cormorant": {"license": "OFL-1.1", "source": _google_font_source("cormorant"),
                  "usage_scope": "redistributable-ofl"},
    "EB Garamond": {"license": "OFL-1.1", "source": _google_font_source("ebgaramond"),
                    "usage_scope": "redistributable-ofl"},
    "Marcellus": {"license": "OFL-1.1", "source": _google_font_source("marcellus"),
                  "usage_scope": "redistributable-ofl"},
    "Zilla Slab": {"license": "OFL-1.1", "source": _google_font_source("zillaslab"),
                   "usage_scope": "redistributable-ofl"},
    "Lovely Little Jelly": {
        "license": "OFL-1.1",
        "source": "https://www.100font.com (软萌小果冻体 / Lovely Little Jelly)",
        "usage_scope": "internal-parity; vendored from the mixture-v2 font package",
    },
    "今年也要加油鸭": {
        "license": "free-personal-commercial (原作者授权免费商用)",
        "source": "https://www.100font.com (今年也要加油鸭)",
        "usage_scope": "internal-parity; vendored from the mixture-v2 font package",
    },
    "叶根友钢笔行书简体": {
        "license": "unknown / unlicensed (爱给网 redistribution; treat as internal-only)",
        "source": "https://www.aigei.com (叶根友钢笔行书简体)",
        "usage_scope": "internal-only parity; not for external redistribution",
    },
    "段宁硬笔楷书粗体": {
        "license": "free-commercial (段宁 免费商用手写体)",
        "source": "https://www.100font.com (段宁硬笔楷书粗体)",
        "usage_scope": "internal-parity; vendored from the mixture-v2 font package",
    },
    "迷你简硬笔行书": {
        "license": "unknown / unlicensed (爱给网 redistribution; treat as internal-only)",
        "source": "https://www.aigei.com (迷你简硬笔行书)",
        "usage_scope": "internal-only parity; not for external redistribution",
    },
    # P1 case-local design fonts. OFL where sourced from google/fonts; the
    # Alibaba/DingTalk families ship under free-commercial EULAs and Zpix is a
    # paid font — none are relabelled OFL.
    "Archivo Black": {"license": "OFL-1.1", "source": _google_font_source("archivoblack"),
                      "usage_scope": "redistributable-ofl"},
    "IBM Plex Serif": {"license": "OFL-1.1", "source": _google_font_source("ibmplexserif"),
                       "usage_scope": "redistributable-ofl"},
    "Press Start 2P": {"license": "OFL-1.1", "source": _google_font_source("pressstart2p"),
                       "usage_scope": "redistributable-ofl"},
    "Orbitron": {"license": "OFL-1.1", "source": _google_font_source("orbitron"),
                 "usage_scope": "redistributable-ofl"},
    "DM Serif Display": {"license": "OFL-1.1", "source": _google_font_source("dmserifdisplay"),
                         "usage_scope": "redistributable-ofl"},
    "Roboto Condensed": {"license": "OFL-1.1", "source": _google_font_source("robotocondensed"),
                         "usage_scope": "redistributable-ofl"},
    "VT323": {"license": "OFL-1.1", "source": _google_font_source("vt323"),
              "usage_scope": "redistributable-ofl"},
    "Baloo 2": {"license": "OFL-1.1", "source": _google_font_source("baloo2"),
                "usage_scope": "redistributable-ofl"},
    "Bodoni Moda": {"license": "OFL-1.1", "source": _google_font_source("bodonimoda"),
                    "usage_scope": "redistributable-ofl"},
    "Ark Pixel": {
        "license": "OFL-1.1",
        "source": "https://github.com/TakWolf/ark-pixel-font (12px proportional zh_cn)",
        "usage_scope": "redistributable-ofl",
    },
    "Fusion Pixel": {
        "license": "OFL-1.1",
        "source": "https://github.com/TakWolf/fusion-pixel-font (12px proportional zh_hans)",
        "usage_scope": "redistributable-ofl",
    },
    "Alimama FangYuanTi VF": {
        "license": "free-commercial (Alibaba Design EULA; 永久免费商用, no resale/modification)",
        "source": "https://www.alibabafonts.com (阿里妈妈方圆体 VF)",
        "usage_scope": "internal-parity; free-commercial EULA, not OFL",
    },
    "Alibaba PuHuiTi 3.0": {
        "license": "free-commercial (Alibaba EULA; 全球永久免费商用, no resale/modification)",
        "source": "https://www.alibabafonts.com (阿里巴巴普惠体 3.0 · 55 Regular)",
        "usage_scope": "internal-parity; free-commercial EULA, not OFL",
    },
    "DingTalk JinBuTi": {
        "license": "free-commercial (DingTalk EULA; 免费商用, no resale/modification)",
        "source": "https://www.dingtalk.com (钉钉进步体)",
        "usage_scope": "internal-parity; free-commercial EULA, not OFL",
    },
    "Sarasa Mono SC": {
        "license": "OFL-1.1",
        "source": "https://github.com/be5invis/Sarasa-Gothic (更纱黑体 Mono SC)",
        "usage_scope": "redistributable-ofl",
    },
    "Alimama ShuHeiTi": {
        "license": "free-commercial (Alibaba Design EULA; 永久免费商用, no resale/modification)",
        "source": "https://www.alibabafonts.com (阿里妈妈数黑体 · Bold)",
        "usage_scope": "internal-parity; free-commercial EULA, not OFL",
    },
    "Zpix": {
        "license": "proprietary / paid (SolidZORO; freely downloadable binary, license is paid)",
        "source": "https://github.com/SolidZORO/zpix-pixel-font (最像素 Zpix)",
        "usage_scope": "internal-only parity; paid license, not for external redistribution",
    },
}

GENERIC_FAMILIES = {
    "serif", "sans-serif", "monospace", "cursive", "fantasy",
    "system-ui", "ui-sans-serif", "ui-serif", "ui-monospace",
}

DEFAULT_TOKENS = {
    "--font-sans", "--font-serif", "--font-body", "--font-title",
    "--font-display", "--font-display-serif", "--font-number",
    "--font-annotation", "--font-mono",
}
# Truncated role names that still mean a defined portable token.
TOKEN_ALIASES = {
    "--font-num": "--font-number",
}

CUSTOM_CONFIG = Path("_state/font-request.json")
FONT_INTENT_PATH = Path("font-intent.json")
FONT_INTENT_SCHEMA = "mural.font-intent.v1"
SUBSTITUTIONS_PATH = (
    Path(__file__).resolve().parents[2] / "references/font-substitutions.json"
)
SUBSTITUTIONS_SCHEMA = "mural.font-substitutions.v1"
# v2 roles are independent; ``display`` inherits from ``title`` only when a
# request omits it (compat with the v1 font-config where ``title`` covered both).
INTENT_ROLES = ("display", "title", "body", "number", "annotation", "mono")
ROLE_TOKENS = {
    "display": ("--font-display", "--font-title"),
    "title": ("--font-title", "--font-display"),
    "body": ("--font-body", "--font-sans"),
    "number": ("--font-number",),
    "annotation": ("--font-annotation", "--font-write", "--font-jotter"),
    "mono": ("--font-mono",),
}
# Which portable role token a structured font-intent role maps onto. ``display``
# and ``title`` are independent here; ``mono`` is its own token.
INTENT_ROLE_TOKENS = {
    "display": ("--font-display", "--font-title"),
    "title": ("--font-title", "--font-display"),
    "body": ("--font-body", "--font-sans"),
    "number": ("--font-number",),
    "annotation": ("--font-annotation", "--font-write", "--font-jotter"),
    "mono": ("--font-mono",),
}


_FONT_FAMILY_DECLARATION = re.compile(
    r"(?P<prefix>(?<![-\w])font-family\s*:\s*)"
    r"(?P<value>[^;{}]+)(?P<semicolon>;?)",
    flags=re.I,
)


def _normalize_stack_members(value: str) -> tuple[list[str], str]:
    """Split a font-family value into member names, stripping ``!important``.

    Returns ``(members, important_suffix)``. Each member keeps its original
    surface form minus surrounding quotes; ``var(...)`` references are kept so
    the caller can detect and preserve token stacks.
    """
    important = " !important" if re.search(r"!important\s*$", value, re.I) else ""
    body = re.sub(r"\s*!important\s*$", "", value, flags=re.I).strip()
    members: list[str] = []
    parts, start, depth, quote = [], 0, 0, ""
    for index, char in enumerate(body):
        if quote:
            if char == quote and (index == 0 or body[index - 1] != "\\"):
                quote = ""
        elif char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(body[start:index])
            start = index + 1
    parts.append(body[start:])
    for part in parts:
        item = part.strip()
        if not item:
            continue
        members.append(re.sub(r"^[\"']|[\"']$", "", item))
    return members, important


def _catalog_aliases() -> dict[str, str]:
    return dict(substitution_catalog().get("aliases") or {})


def _shipped_family_for(name: str) -> tuple[str | None, bool]:
    """Resolve ``name`` to a shipped family.

    Returns ``(family, is_exact)``:
    - exact built-in → ``(name, True)``;
    - known alias that resolves to a built-in → ``(canonical, False)``;
    - otherwise → ``(None, False)``.
    """
    if name in FAMILY_FACES:
        return name, True
    aliases = _catalog_aliases()
    if name in aliases:
        canonical = _resolve_alias(name, aliases)
        if canonical in FAMILY_FACES:
            return canonical, False
    return None, False


def _quote_family(name: str) -> str:
    """Quote a family name for CSS when it is not a bare generic keyword."""
    if name.lower() in GENERIC_FAMILIES or name.startswith("var("):
        return name
    return f'"{name}"'


def _font_face_names(family: str) -> list[str]:
    """同一份字体数据同时服务源码名称与确切别名，导出不改作者字形选择。"""
    aliases = _catalog_aliases()
    return [family, *(name for name in aliases if name != family and _resolve_alias(name, aliases) == family)]


def normalize_font_family_declarations(text: str) -> tuple[str, int]:
    """只规范确切别名，保留未知/用户字体、整个 fallback 栈与字重。"""
    replacements = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal replacements
        value = match.group("value")
        if "var(" in value:
            return match.group(0)
        members, important = _normalize_stack_members(value)
        canonical = [_shipped_family_for(member)[0] or member for member in members]
        if canonical == members:
            return match.group(0)
        replacements += 1
        rebuilt = ", ".join(_quote_family(name) for name in canonical)
        return f"{match.group('prefix')}{rebuilt}{important}{match.group('semicolon')}"

    return _FONT_FAMILY_DECLARATION.sub(replace, text), replacements


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


class _FontStyles(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.in_style = False

    def handle_starttag(self, tag, attrs) -> None:
        self.in_style = tag == "style" or self.in_style
        self.parts.extend(value for key, value in attrs if key == "style" and value)

    def handle_endtag(self, tag) -> None:
        if tag == "style":
            self.in_style = False

    def handle_data(self, data: str) -> None:
        if self.in_style:
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
    for start, end in (
        (BUNDLE_START, BUNDLE_END),
        (AUTHORING_START, AUTHORING_END),
    ):
        css = re.sub(
            re.escape(start) + r".*?" + re.escape(end) + r"\s*",
            "",
            css,
            flags=re.S,
        )
    return css.lstrip()


@contextmanager
def _workspace_bundle_lock(root: Path, *, timeout_s: float = 120.0) -> Iterator[None]:
    """Serialize bundle promotion for parallel Group Agents in one workspace.

    ``pyftsubset`` output can be byte-different across invocations. Without a
    process lock, two first renders may publish different manifests and delete
    each other's WOFF2 files even though every individual write is atomic.
    锁与子集临时文件只属于当前作者/导出 staging，不依赖全局环境切换。
    """
    import fcntl

    lock_root = root.resolve() / "_state/locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / "font-bundle.lock"
    with lock_path.open("a+b") as handle:
        started = time.monotonic()
        deadline = started + timeout_s
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                now = time.monotonic()
                if now >= deadline:
                    raise TimeoutError(
                        f"font_bundle_lock_timeout: waited_s={now - started:.3f}; lock_path={lock_path}"
                    ) from exc
                # quarkfs 的锁竞争可能直接返回 EAGAIN；仅重试竞争，不掩盖权限或 I/O 错误。
                time.sleep(min(0.05, deadline - now))
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _font_source_dirs() -> list[Path]:
    """Directories runtime font resolution may read, most-trusted first.

    The release ships a self-contained ``fonts/`` directory that covers every
    whitelisted family, so runtime resolution is hermetic: it never falls back
    to ``~/.fonts``, ``~/.local/share/fonts`` or system fontconfig.  Only an
    explicit ``PPT_FONT_SOURCE_DIRS`` override (used by tests and by an
    alternate release layout) and the in-tree ``fonts/`` directories are
    searched.  Anything "installed on this machine" is intentionally excluded.
    """
    configured = os.environ.get("PPT_FONT_SOURCE_DIRS", "").strip()
    values = [Path(item).expanduser() for item in configured.split(os.pathsep) if item]
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "fonts"
        if candidate.is_dir():
            values.append(candidate)
    result: list[Path] = []
    for value in values:
        resolved = value.resolve()
        if resolved not in result:
            result.append(resolved)
    return result


def _declared_weight_range(weight: str) -> tuple[float, float] | None:
    values = weight.split()
    if len(values) != 2:
        return None
    return float(values[0]), float(values[1])


@lru_cache(maxsize=None)
def _variable_weight_range(path: str) -> tuple[float, float] | None:
    """Return the real ``wght`` axis bounds, never a filename/CSS guess."""
    try:
        from fontTools.ttLib import TTFont

        font = TTFont(path, lazy=False)
        try:
            if "fvar" not in font:
                return None
            for axis in font["fvar"].axes:
                if axis.axisTag == "wght":
                    return float(axis.minValue), float(axis.maxValue)
        finally:
            font.close()
    except Exception:
        return None
    return None


def _font_supports_face(path: Path, face: Face) -> bool:
    if not _font_source_readable(str(path)):
        return False
    declared = _declared_weight_range(face.weight)
    if declared is None:
        return True
    actual = _variable_weight_range(str(path))
    return actual is not None and actual[0] <= declared[0] and actual[1] >= declared[1]


def _find_source(face: Face, source_dirs: list[Path]) -> Path:
    for directory in source_dirs:
        for name in face.source_names:
            candidate = directory / name
            if candidate.is_file() and candidate.stat().st_size and _font_supports_face(candidate, face):
                return candidate
    raise FileNotFoundError(
        f"missing compatible delivery font source {list(face.source_names)!r} "
        f"for weight {face.weight!r}; searched "
        + ", ".join(str(item) for item in source_dirs)
    )


def _pinned_font_manifest(root: Path, *, required: bool = False) -> dict | None:
    path = root / "_state/font-manifest.json"
    if not required and not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != "mural.font-manifest.v2":
        raise ValueError("unsupported pinned font manifest")
    return value


def _pinned_object(manifest: dict, row: dict, *, verify: bool = True) -> tuple[Path, str]:
    digest = str(row.get("sha256") or "")
    relative = Path(str(row.get("object_ref") or ""))
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("invalid pinned font object identity")
    root = Path(str(manifest.get("studio_data_root") or ""))
    if not root.is_absolute():
        raise ValueError("pinned font manifest lacks absolute studio_data_root")
    if row.get("scope") == "public":
        namespace = Path("shared/fonts/objects")
    elif row.get("scope") == "user" and re.fullmatch(r"[A-Za-z0-9_-]+", str(row.get("owner_user_id") or "")):
        namespace = Path("users") / row["owner_user_id"] / "_shared/fonts/objects"
        if os.environ.get("MURAL_USER_ID") and os.environ["MURAL_USER_ID"] != str(row["owner_user_id"]):
            raise ValueError("private font belongs to a different WebUI user")
    else:
        raise ValueError("unsupported pinned font object scope")
    if relative.parent != namespace or relative.suffix not in {".ttf", ".otf", ".woff", ".woff2"} or relative.stem != digest:
        raise ValueError("pinned font object identity escapes its scope")
    source = root / relative
    if source.is_symlink() or source.resolve() != root.resolve() / relative:
        raise ValueError("pinned font object escapes its scope")
    if not source.is_file():
        raise FileNotFoundError(f"pinned font object is missing: {relative}")
    if verify and hashlib.sha256(source.read_bytes()).hexdigest() != digest:
        raise ValueError(f"pinned font object hash mismatch: {relative}")
    return source.resolve(), digest


def resolve_shared_font_reference(root: Path, reference: str, *, verify: bool = True) -> tuple[Path, str]:
    """只解析本 Deck 固定字体；关闭预验仅供调用方校验即将发送的同一份字节。"""
    parsed = urlsplit(reference)
    if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("shared font reference must be a local file URI")
    target = Path(unquote(parsed.path))
    manifest = _pinned_font_manifest(root, required=True)
    store_root = Path(str(manifest.get("studio_data_root") or "")).resolve()
    for row in [*(manifest.get("source_faces") or []), *(manifest.get("custom_fonts") or [])]:
        # 先按 manifest 路径定位，再验证唯一命中对象；不为每条 URI stat 整个字体库。
        if target == store_root / str(row.get("object_ref") or ""):
            return _pinned_object(manifest, row, verify=verify)
    raise ValueError("shared font URI is not pinned by this workspace")


def _face_source(family: str, face: Face, source_dirs: list[Path], pinned: dict | None, *, verify: bool = True) -> Path:
    if pinned is None:
        return _find_source(face, source_dirs)
    for row in pinned.get("source_faces") or []:
        if (row.get("family"), str(row.get("weight")), row.get("style")) == (family, face.weight, face.style):
            return _pinned_object(pinned, row, verify=verify)[0]
    raise FileNotFoundError(f"font face is not pinned: {family} {face.weight} {face.style}")


def _family_available(family: str, source_dirs: list[Path], pinned: dict | None = None) -> bool:
    try:
        for face in FAMILY_FACES[family]:
            if not _font_source_readable(str(_face_source(family, face, source_dirs, pinned, verify=False))):
                return False
        return True
    except FileNotFoundError:
        return False


@lru_cache(maxsize=None)
def _font_source_readable(path: str) -> bool:
    """Reject corrupt installed faces before pyftsubset turns them into a hard failure."""
    try:
        from fontTools.ttLib import TTFont

        font = TTFont(path, lazy=False)
        try:
            for table in font["cmap"].tables:
                _ = table.cmap
        finally:
            font.close()
        return True
    except Exception:
        return False


@lru_cache(maxsize=None)
def _font_source_metadata(path: str) -> dict[str, str]:
    """Read human-facing copyright/license fields retained in the source font."""
    from fontTools.ttLib import TTFont

    font = TTFont(path, lazy=False)
    try:
        names = font["name"]
        return {
            "copyright": names.getDebugName(0) or "",
            "license_description": names.getDebugName(13) or "",
            "license_url": names.getDebugName(14) or "",
        }
    finally:
        font.close()


def _load_custom_config(root: Path) -> tuple[dict[str, dict], dict[str, str]]:
    path = root / CUSTOM_CONFIG
    pinned = _pinned_font_manifest(root)
    if pinned is not None:
        config = {"fonts": pinned.get("custom_fonts") or [], "roles": pinned.get("roles") or {},
                  "license_acknowledged": True}
    elif not path.is_file():
        return {}, {}
    else:
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"custom font config is unreadable: {exc}") from exc
    fonts = config.get("fonts") or []
    if fonts and not config.get("license_acknowledged"):
        raise ValueError("custom font config lacks user embedding authorization")
    registry: dict[str, dict] = {}
    for item in fonts:
        if not isinstance(item, dict) or not item.get("id") or not (item.get("object_ref") if pinned else item.get("source_path")):
            raise ValueError("custom font config contains an incomplete font record")
        font_id = str(item["id"])
        source_label = str(item.get("object_ref") or item.get("source_path"))
        if pinned:
            if item.get("scope") != "user":
                raise ValueError("custom font is not owner-scoped")
            source, _ = _pinned_object(pinned, item)
        else:
            source = (root / str(item["source_path"])).resolve()
            try:
                source.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"custom font escapes workspace: {source}") from exc
        if not source.is_file() or not source.stat().st_size:
            raise FileNotFoundError(f"custom font source is missing: {source_label}")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if digest != str(item.get("sha256") or ""):
            raise ValueError(f"custom font hash mismatch: {source_label}")
        if not _font_source_readable(str(source)):
            raise ValueError(f"custom font is unreadable: {source_label}")
        family_key = f"User::{font_id}"
        subfamily = str(item.get("subfamily") or "").lower()
        registry[family_key] = {
            **item,
            "source": source,
            "weight": str(int(item.get("weight") or 400)),
            "style": "italic" if "italic" in subfamily else "oblique" if "oblique" in subfamily else "normal",
        }
    role_overrides: dict[str, str] = {}
    for role, selection in (config.get("roles") or {}).items():
        if role not in ROLE_TOKENS or not isinstance(selection, dict):
            continue
        kind = selection.get("kind")
        if kind == "builtin":
            family = str(selection.get("family") or "")
            if family not in FAMILY_FACES:
                raise ValueError(f"custom font config references unapproved builtin family: {family}")
        elif kind == "custom":
            family = f"User::{selection.get('font_id') or ''}"
            if family not in registry:
                raise ValueError(f"custom font role {role} references a missing font")
        else:
            continue
        for token in ROLE_TOKENS[role]:
            role_overrides[token] = family
    return registry, role_overrides


def _font_usage_scope(family: str) -> str:
    """Effective usage-scope note for a builtin family.

    The original redistributable OFL set predates the ``usage_scope`` field and
    defaults to ``redistributable-ofl``; mixture-v2 parity fonts declare an
    explicit scope (internal-only, free-commercial, paid, ...).
    """
    metadata = FONT_LICENSES.get(family) or {}
    return str(metadata.get("usage_scope") or ("redistributable-ofl" if metadata.get("source") else ""))


def _validate_font_allowlist() -> None:
    missing = sorted(set(FAMILY_FACES) - set(FONT_LICENSES))
    extra = sorted(set(FONT_LICENSES) - set(FAMILY_FACES))
    if missing or extra:
        raise RuntimeError(f"font allowlist mismatch: missing={missing}, extra={extra}")
    # Every built-in font must carry honest provenance: a non-empty official
    # source AND a usage-scope note.  This is intentionally NOT an OFL gate —
    # mixture-v2 parity fonts (free-commercial / internal-only / paid) are
    # allowed as long as their real source and scope are recorded.  The license
    # string itself is only required to be non-empty; it is never coerced to OFL.
    unsupported = sorted(
        family for family, metadata in FONT_LICENSES.items()
        if not metadata.get("source")
        or not metadata.get("license")
        or not _font_usage_scope(family)
    )
    if unsupported:
        raise RuntimeError(
            "font allowlist requires non-empty source + license + usage-scope "
            "metadata for: " + ", ".join(unsupported)
        )
    if not OFL_TEMPLATE.is_file():
        raise FileNotFoundError(f"missing bundled license text: {OFL_TEMPLATE}")


# ---------------------------------------------------------------------------
# Phase 2 — alias + substitution catalog
# ---------------------------------------------------------------------------


@lru_cache(maxsize=None)
def _load_substitution_catalog(path_str: str) -> dict:
    path = Path(path_str)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"font substitution catalog is unreadable: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != SUBSTITUTIONS_SCHEMA:
        raise ValueError("font substitution catalog has an unsupported schema")
    _validate_substitution_catalog(payload)
    return payload


def substitution_catalog() -> dict:
    return _load_substitution_catalog(str(SUBSTITUTIONS_PATH))


def substitution_catalog_fingerprint() -> str:
    return hashlib.sha256(
        SUBSTITUTIONS_PATH.read_bytes()
    ).hexdigest() if SUBSTITUTIONS_PATH.is_file() else ""


def _resolve_alias(name: str, aliases: dict[str, str]) -> str:
    """Follow an alias chain to its canonical target, guarding against cycles."""
    seen: set[str] = set()
    current = name
    while current in aliases:
        if current in seen:
            raise ValueError(f"font alias cycle includes {current!r}")
        seen.add(current)
        current = aliases[current]
    return current


def _validate_substitution_catalog(payload: dict) -> None:
    aliases = payload.get("aliases") or {}
    mappings = payload.get("mappings") or []
    if not isinstance(aliases, dict) or not isinstance(mappings, list):
        raise ValueError("font substitution catalog has malformed sections")
    allow = set(FAMILY_FACES)
    mapped_names = {str(m.get("requested_family")) for m in mappings if isinstance(m, dict)}
    # A canonical allowlist family must never be an alias *source*.
    for source in aliases:
        if source in allow:
            raise ValueError(f"alias source shadows a canonical family: {source!r}")
    # Every alias must terminate on a canonical family (a spelling variant) or on
    # a commercial family that itself carries a mapping (so the resolver can
    # substitute it). Either way the chain must be acyclic and non-dangling.
    for source in aliases:
        target = _resolve_alias(source, aliases)
        if target not in allow and target not in mapped_names:
            raise ValueError(
                f"alias {source!r} does not resolve to a canonical family or a "
                f"mapped commercial family (ends at {target!r})"
            )
    seen_ids: set[str] = set()
    for mapping in mappings:
        if not isinstance(mapping, dict):
            raise ValueError("font substitution mapping is not an object")
        mapping_id = str(mapping.get("id") or "")
        requested = str(mapping.get("requested_family") or "")
        status = str(mapping.get("status") or "")
        candidates = mapping.get("candidates") or []
        if not mapping_id or not requested or not isinstance(candidates, list) or not candidates:
            raise ValueError(f"font substitution mapping is incomplete: {mapping_id or requested!r}")
        if mapping_id in seen_ids:
            raise ValueError(f"duplicate substitution mapping id: {mapping_id}")
        seen_ids.add(mapping_id)
        if status not in {"proposed", "audited"}:
            raise ValueError(f"mapping {mapping_id} has an invalid status: {status!r}")
        # Commercial entries carry name+metadata only — never a file path.
        if any(key in mapping for key in ("source_path", "path", "file", "url", "src")):
            raise ValueError(f"mapping {mapping_id} must not carry font file paths")
        if status == "audited":
            missing = [c for c in candidates if c not in allow]
            if missing:
                raise ValueError(
                    f"audited mapping {mapping_id} has candidates absent from the "
                    f"allowlist: {missing}"
                )
            if not mapping.get("specimen"):
                raise ValueError(f"audited mapping {mapping_id} lacks specimen evidence")




# ---------------------------------------------------------------------------
# Phase 3 — structured font intent (font-intent.json)
# ---------------------------------------------------------------------------


def _load_font_intent(root: Path) -> dict:
    """Read the optional structured font intent produced by the Orchestrator.

    The file is an agent work product; absence is normal and yields ``{}``.
    ``display`` inherits from ``title`` only when a role is absent (v1 compat).
    """
    path = root / FONT_INTENT_PATH
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"font intent is unreadable: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != FONT_INTENT_SCHEMA:
        raise ValueError("font intent has an unsupported schema")
    roles = payload.get("roles")
    result: dict[str, dict] = {}
    if isinstance(roles, dict):
        for role in INTENT_ROLES:
            spec = roles.get(role)
            if not isinstance(spec, dict):
                continue
            stack = spec.get("requested_stack")
            requested_stack = [str(item) for item in stack] if isinstance(stack, list) else []
            result[role] = {
                "requested_stack": requested_stack,
                "weight": spec.get("weight"),
                "style": str(spec.get("style") or "normal"),
            }
    if "display" not in result and "title" in result:
        result["display"] = dict(result["title"])
    return {"source": payload.get("source") or {}, "roles": result}


# ---------------------------------------------------------------------------
# Phase 4 — deterministic resolution layer (pure function)
# ---------------------------------------------------------------------------

# Ordered from strongest to weakest guarantee.
RESOLUTION_KINDS = (
    "authored_css",
    "exact_builtin",
    "exact_alias",
    "exact_user_upload",
    "portable_fallback",
    "unresolved",
)


@dataclass(frozen=True)
class Resolution:
    role: str
    token: str
    requested_family: str
    requested_stack: tuple[str, ...]
    normalized_family: str
    resolved_family: str
    resolution_kind: str
    mapping_id: str
    weight_requested: str
    weight_resolved: str
    style: str
    fallback_chain: tuple[str, ...]

    def as_record(self, *, license: str, redistributable: bool, user_authorized: bool) -> dict:
        raw = "|".join(
            (
                self.requested_family,
                ",".join(self.requested_stack),
                self.mapping_id,
                self.resolved_family,
                self.resolution_kind,
            )
        )
        return {
            "resolution_id": hashlib.sha256(
                f"{self.role}|{raw}".encode("utf-8")
            ).hexdigest()[:12],
            "role": self.role,
            "token": self.token,
            "requested_family": self.requested_family,
            "requested_stack": list(self.requested_stack),
            "normalized_family": self.normalized_family,
            "resolved_family": self.resolved_family,
            "resolution_kind": self.resolution_kind,
            "mapping_id": self.mapping_id,
            "weight_requested": self.weight_requested,
            "weight_resolved": self.weight_resolved,
            "style": self.style,
            "license": license,
            "redistributable": redistributable,
            "user_authorized": user_authorized,
            "fallback_chain": list(self.fallback_chain),
        }


def _weight_in_family_range(family: str, weight: str | None) -> tuple[bool, str]:
    """True + resolved weight string if ``weight`` fits a face of ``family``."""
    faces = FAMILY_FACES.get(family) or ()
    if not weight:
        return (bool(faces), faces[0].weight if faces else "")
    try:
        wanted = float(weight)
    except (TypeError, ValueError):
        return (bool(faces), faces[0].weight if faces else "")
    for face in faces:
        rng = _declared_weight_range(face.weight)
        if rng is None:
            if abs(float(face.weight) - wanted) < 1e-6:
                return True, face.weight
        elif rng[0] <= wanted <= rng[1]:
            return True, face.weight
    return (False, faces[0].weight if faces else "")


def resolve_font_request(
    *,
    role: str,
    token: str,
    requested_stack: list[str],
    weight: str | None,
    style: str,
    custom_registry: dict[str, dict],
    route_default_stack: list[str],
    source_available,
    catalog: dict,
    authored_families: set[str] = frozenset(),
) -> Resolution:
    """按显式栈顺序解析真实字体/确切别名；缺失时不另选风格相近的字体。"""
    aliases = catalog.get("aliases") or {}
    stack = requested_stack or route_default_stack
    style = (style or "normal").lower()
    requested_family = stack[0] if stack else ""
    fallback_chain = tuple(stack[1:])
    for name in stack:
        if name in authored_families:
            return Resolution(role, token, requested_family, tuple(stack), name, name,
                "authored_css", "", str(weight or ""), str(weight or ""), style, fallback_chain)
        if name in custom_registry:
            reg = custom_registry[name]
            return Resolution(
                role, token, requested_family, tuple(stack), name, name,
                "exact_user_upload", "", str(weight or reg.get("weight") or ""),
                str(reg.get("weight") or ""), style, fallback_chain,
            )
        canonical = _resolve_alias(name, aliases)
        kind = "exact_alias" if canonical != name else "exact_builtin"
        if name.lower() in GENERIC_FAMILIES:
            canonical = (catalog.get("unmapped_fallback") or {}).get(name.lower(), "")
            kind = "portable_fallback"
        if canonical not in FAMILY_FACES or not source_available(canonical):
            continue
        weight_ok, resolved_weight = _weight_in_family_range(canonical, weight)
        faces = FAMILY_FACES[canonical]
        if not weight_ok or (style == "normal" and not any(face.style == "normal" for face in faces)):
            continue
        return Resolution(
            role, token, requested_family, tuple(stack), canonical, canonical,
            kind, "", str(weight or ""), resolved_weight, style, fallback_chain,
        )
    return Resolution(
        role, token, requested_family, tuple(stack), requested_family, "",
        "unresolved", "", str(weight or ""), "", style, fallback_chain,
    )


def _token_definitions(css: str) -> dict[str, str]:
    """收集 CSS 自定义属性供 var() 解析；字体用途由使用位置决定，名称区分大小写。"""
    return {
        name: value.strip()
        for name, value in re.findall(
            r"(?<![\w-])(--[\w-]+)\s*:\s*([^;{}]+)(?=[;}]|\Z)", css
        )
    }


def _family_candidates(value: str) -> list[str]:
    result: list[str] = []
    for family in _normalize_stack_members(value)[0]:
        if not family or family.lower() in GENERIC_FAMILIES or family.startswith("var("):
            continue
        result.append(family)
    return result


def _canonical_font_token(name: str, definitions: dict[str, str]) -> str:
    """Map truncated role names onto defined tokens without inventing a family."""
    if name in definitions:
        return name
    alias = TOKEN_ALIASES.get(name)
    if alias and alias in definitions:
        return alias
    return name


def _resolve_token_stack(
    token: str,
    definitions: dict[str, str],
    seen: set[str] | None = None,
    *,
    allow_fallback: bool = True,
) -> list[str]:
    """展开显式 CSS 变量和 fallback；不根据变量名称选择字体。"""
    def expand(value: str, chain: set[str]) -> list[str]:
        stack: list[str] = []
        for family in _normalize_stack_members(value)[0]:
            reference = re.fullmatch(r"var\(\s*(--[\w-]+)\s*(?:,(.*))?\)", family, re.S)
            if reference:
                name, fallback = reference.groups()
                name = _canonical_font_token(name, definitions)
                if name in chain:
                    if not allow_fallback:
                        continue
                    raise ValueError(f"font token cycle includes {name}")
                for item in expand(definitions.get(name, fallback or ""), chain | {name}):
                    if item not in stack:
                        stack.append(item)
                continue
            if (not family or re.fullmatch(r"[-+]?[\d.]+(?:[a-z%]+)?", family, re.I)
                    or family.lower() in {"normal", "bold", "bolder", "lighter", "italic", "oblique", "small-caps",
                                          "condensed", "expanded", "inherit", "initial", "unset", "revert", "revert-layer"}
                    or re.match(r"(?:calc|min|max|clamp|env)\(", family, re.I)):
                continue
            candidate = _shipped_family_for(family)[0] or family
            if family.lower() in GENERIC_FAMILIES and allow_fallback:
                candidate = (substitution_catalog().get("unmapped_fallback") or {}).get(family.lower(), "")
            if candidate and candidate not in stack:
                stack.append(candidate)
        return stack

    token = _canonical_font_token(token, definitions)
    return expand(definitions.get(token, ""), set(seen or ()) | {token})


def _active_token_stacks(css: str, fragments: list[str], pinned: dict | None = None,
                         custom_fonts: dict | None = None, authored_families=frozenset()) -> dict[str, list[str]]:
    styles = _FontStyles()
    for fragment in fragments:
        styles.feed(fragment)
    declarations = re.sub(r"/\*.*?\*/", "", ";\n".join([css, *styles.parts]), flags=re.S)
    definitions = _token_definitions(declarations)
    tokens = set(DEFAULT_TOKENS).intersection(definitions)
    # 仅 font-family 的使用位置决定额外字体变量；字重、字号及规划提及不能改写为字体名。
    tokens.update(
        item
        for declaration in _FONT_FAMILY_DECLARATION.finditer(declarations)
        for item in re.findall(r"var\(\s*(--font-[a-z0-9-]+)", declaration.group("value"), flags=re.I)
        if item in definitions
    )
    # font 简写混合字重/字号/行高；只有可解析为字体族的变量才进入打包，不能套用默认 fallback 猜类型。
    for shorthand in re.findall(r"(?<![-\w])font\s*:\s*([^;{}]+)", declarations, flags=re.I):
        for token in re.findall(r"var\(\s*(--font-[a-z0-9-]+)", shorthand, flags=re.I):
            if _resolve_token_stack(token, definitions, allow_fallback=False):
                tokens.add(token)
    source_dirs = _font_source_dirs()
    if not _family_available("Noto Sans SC", source_dirs, pinned):
        raise FileNotFoundError("Noto Sans SC is required as the portable font fallback")
    result: dict[str, list[str]] = {}
    for token in sorted(tokens):
        available = [
            family
            for family in _resolve_token_stack(token, definitions)
            if family in authored_families or family in (custom_fonts or {}) or (family in FAMILY_FACES and _family_available(family, source_dirs, pinned))
        ]
        if not available:
            raise ValueError(f"font_stack_unavailable: {token}={definitions.get(token, '')!r}; "
                             f"choose available families from {sorted(FAMILY_FACES)} or an authorized user font")
        result[token] = _ensure_complete_cjk_fallback(available)
    return result


def _active_tokens(css: str, fragments: list[str]) -> dict[str, str]:
    return {
        token: stack[0]
        for token, stack in _active_token_stacks(css, fragments).items()
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
    if family.startswith("User::"):
        return "sans-serif"
    if family in {
        "IBM Plex Mono", "Geist Mono", "JetBrains Mono", "Roboto Mono",
        "Sarasa Mono SC",
    }:
        return "monospace"
    if family in {
        "Noto Serif SC",
        "Fraunces",
        "Spectral",
        "Playfair Display",
        "Cormorant Garamond",
        "ZCOOL XiaoWei",
        "LXGW WenKai",
        # mixture-v2 serif voices.
        "EB Garamond", "Cormorant", "Marcellus", "Zilla Slab", "Cinzel",
        "IBM Plex Serif", "DM Serif Display", "Bodoni Moda",
    }:
        return "serif"
    if family in {
        "Xiaolai", "Ma Shan Zheng", "Zhi Mang Xing", "Long Cang", "Liu Jian Mao Cao",
        "Patrick Hand", "Caveat", "Architects Daughter", "Indie Flower",
        "Dancing Script", "Kalam", "Shadows Into Light", "Sacramento",
        # mixture-v2 comic / handwritten voices.
        "Comic Neue", "Lovely Little Jelly", "今年也要加油鸭",
        "叶根友钢笔行书简体", "段宁硬笔楷书粗体", "迷你简硬笔行书",
    }:
        return "cursive"
    return "sans-serif"


# These two families are the only built-in sources treated as complete CJK
# delivery fallbacks. Display faces such as Smiley Sans, Xiaolai and ZCOOL do
# contain Chinese, but their character sets are deliberately smaller; treating
# them as complete lets rare characters escape to an arbitrary system font.
_COMPLETE_CJK_FALLBACKS = frozenset({"Noto Sans SC", "Noto Serif SC"})


def _ensure_complete_cjk_fallback(stack: list[str]) -> list[str]:
    result = list(dict.fromkeys(stack))
    if any(family in _COMPLETE_CJK_FALLBACKS for family in result):
        return result
    primary = result[0] if result else "Noto Sans SC"
    fallback = "Noto Serif SC" if _generic_for(primary) == "serif" else "Noto Sans SC"
    result.append(fallback)
    return result


def audit_typography_routes(routes_path: Path | None = None) -> dict[str, object]:
    """Verify every family selectable by the route catalog on this worker."""
    path = routes_path or Path(__file__).resolve().parents[2] / "references/typography-routes.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {
            "ready": False,
            "route_count": 0,
            "family_count": 0,
            "missing": [f"unreadable route catalog: {exc}"],
            "families": [],
            "source_fingerprint": "",
        }
    routes = payload.get("routes") if isinstance(payload, dict) else None
    if not isinstance(routes, dict) or not routes:
        return {
            "ready": False,
            "route_count": 0,
            "family_count": 0,
            "missing": ["route catalog does not define any typography routes"],
            "families": [],
            "source_fingerprint": "",
        }
    selected: list[str] = []
    for route in routes.values():
        tokens = route.get("tokens") if isinstance(route, dict) else None
        if not isinstance(tokens, dict):
            continue
        for token, value in tokens.items():
            if str(token).endswith("weight"):
                continue
            for family in _family_candidates(str(value)):
                if family not in selected:
                    selected.append(family)
    for defaults in payload.get("language_defaults", {}).values():
        for family in defaults.values():
            if family not in selected:
                selected.append(family)

    source_dirs = _font_source_dirs()
    missing: list[str] = []
    records: list[dict[str, object]] = []
    fingerprint_rows: list[str] = []
    for family in selected:
        if family not in FAMILY_FACES:
            missing.append(f"{family}: family is not in the portable allowlist")
            continue
        sources: list[dict[str, object]] = []
        try:
            for face in FAMILY_FACES[family]:
                source = _find_source(face, source_dirs)
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
                row = {
                    "file": source.name,
                    "path": str(source),
                    "bytes": source.stat().st_size,
                    "sha256": digest,
                    "weight": face.weight,
                    "style": face.style,
                }
                sources.append(row)
                fingerprint_rows.append(
                    "|".join((family, source.name, digest, face.weight, face.style))
                )
        except (FileNotFoundError, OSError) as exc:
            missing.append(f"{family}: {exc}")
        records.append({"family": family, "available": bool(sources), "sources": sources})
    fingerprint = hashlib.sha256(
        "\n".join(sorted(fingerprint_rows)).encode("utf-8")
    ).hexdigest() if fingerprint_rows else ""
    return {
        "ready": not missing and len(records) == len(selected),
        "route_count": len(routes),
        "family_count": len(selected),
        "missing": missing,
        "families": records,
        "source_fingerprint": fingerprint,
    }


def _required_glyphs(scripts: list[str]) -> str:
    """Minimal probe characters a face must cover for its declared scripts.

    Only the two scripts this deck system actually typesets are enforced:
    Latin (body/display) and Han (CJK).  Incidental Greek/Cyrillic coverage in
    a Latin display font is a bonus, not a delivery requirement, so a partial
    Greek subset must not fail the hermetic audit.
    """
    probes = ""
    if "latn" in scripts:
        probes += "AaGg09"
    if "hani" in scripts:
        probes += "字体中文"
    return probes or "A"


def audit_release_fonts(
    release_dir: Path | None = None,
    lock_path: Path | None = None,
) -> dict[str, object]:
    """Strict hermetic audit: every whitelisted family must resolve from the
    single release font directory, byte-for-byte matching the locked manifest.

    Unlike ``_font_source_dirs`` this deliberately ignores ``~/.fonts``, the
    user's ``$HOME``, and system fontconfig.  It is the gate that proves a
    release ships self-contained fonts rather than borrowing whatever the
    authoring machine happens to have installed.
    """
    release_dir = (release_dir or RELEASE_FONT_DIR).resolve()
    lock_path = lock_path or FONT_LOCK_PATH
    missing: list[str] = []
    checked = 0

    try:
        lock = json.loads(Path(lock_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {
            "ready": False,
            "release_dir": str(release_dir),
            "family_count": 0,
            "face_count": 0,
            "missing": [f"unreadable font lock: {exc}"],
        }
    locked = lock.get("families") if isinstance(lock, dict) else None
    if not isinstance(locked, dict):
        return {
            "ready": False,
            "release_dir": str(release_dir),
            "family_count": 0,
            "face_count": 0,
            "missing": ["font lock does not define any families"],
        }

    allowlist = set(FAMILY_FACES)
    for orphan in sorted(set(locked) - allowlist):
        missing.append(f"{orphan}: locked family is not in FAMILY_FACES")
    for uncovered in sorted(allowlist - set(locked)):
        missing.append(f"{uncovered}: whitelisted family is absent from the lock")

    for family in sorted(allowlist):
        entry = locked.get(family)
        locked_faces = {
            face.get("file"): face
            for face in (entry.get("faces") if isinstance(entry, dict) else []) or []
        }
        for face in FAMILY_FACES[family]:
            checked += 1
            # Resolve ONLY inside the release directory.
            source = None
            for name in face.source_names:
                candidate = release_dir / name
                if candidate.is_file() and candidate.stat().st_size:
                    source = candidate
                    break
            if source is None:
                missing.append(
                    f"{family} [{face.weight}]: no face in release dir "
                    f"({list(face.source_names)!r})"
                )
                continue
            locked_face = locked_faces.get(source.name)
            if not locked_face:
                missing.append(f"{family}: {source.name} is not pinned in the lock")
                continue
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            if digest != str(locked_face.get("sha256")):
                missing.append(f"{family}: {source.name} sha256 differs from the lock")
                continue
            # Every face must open, match declared weight/style, cover glyphs.
            if not _font_supports_face(source, face):
                missing.append(
                    f"{family}: {source.name} does not satisfy declared weight {face.weight!r}"
                )
                continue
            try:
                from fontTools.ttLib import TTFont

                font = TTFont(str(source), lazy=False)
                try:
                    covered: set[int] = set()
                    for table in font["cmap"].tables:
                        covered.update(table.cmap)
                finally:
                    font.close()
            except Exception as exc:  # pragma: no cover - corrupt file
                missing.append(f"{family}: {source.name} failed to open ({exc})")
                continue
            for glyph in _required_glyphs(list(locked_face.get("scripts") or [])):
                if ord(glyph) not in covered:
                    missing.append(
                        f"{family}: {source.name} lacks required glyph U+{ord(glyph):04X}"
                    )
                    break

    return {
        "ready": not missing,
        "release_dir": str(release_dir),
        "family_count": len(allowlist),
        "face_count": checked,
        "missing": missing,
    }


def _rename_subset_font(target: Path, delivery_family: str, weight: str, style: str) -> None:
    """Rename a modified subset so OFL Reserved Font Names are not reused."""
    from fontTools.ttLib import TTFont

    font = TTFont(target, lazy=False)
    try:
        subfamily = "Oblique" if style == "oblique" else "Italic" if style == "italic" else "Regular"
        postscript = re.sub(
            r"[^A-Za-z0-9-]", "-", f"{delivery_family}-{weight.replace(' ', '-')}-{subfamily}"
        )[:63]
        full_name = f"{delivery_family} {subfamily}"
        name_table = font["name"]
        rewritten = {1: delivery_family, 2: subfamily, 3: postscript, 4: full_name,
                     6: postscript, 16: delivery_family, 17: subfamily}
        name_table.names = [record for record in name_table.names if record.nameID not in rewritten]
        for name_id, value in rewritten.items():
            name_table.setName(value, name_id, 3, 1, 0x409)
            name_table.setName(value, name_id, 1, 0, 0)
        renamed = target.with_name(target.stem + ".renamed.woff2")
        font.save(renamed)
    finally:
        font.close()
    os.replace(renamed, target)


def _subset(
    tool: str,
    source: Path,
    target: Path,
    characters_file: Path,
    delivery_family: str,
    weight: str,
    style: str,
) -> None:
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
    _rename_subset_font(target, delivery_family, weight, style)
    declared = _declared_weight_range(weight)
    actual = _variable_weight_range(str(target)) if declared is not None else None
    if declared is not None and (
        actual is None or actual[0] > declared[0] or actual[1] < declared[1]
    ):
        raise RuntimeError(
            f"subset lost required variable weight range {weight} for {source.name}"
        )


def _authored_font_families(css: str, fragments: list[str], root: Path | None = None) -> set[str]:
    """只读实际 @font-face 定义（含远程 CSS），不按字体名猜设计或改写作者字体。"""
    try:
        from .remote_resources import fetch
    except ImportError:
        from remote_resources import fetch
    styles = _FontStyles()
    for fragment in fragments:
        styles.feed(fragment)
    pending = [("", "\n".join([css, *styles.parts]))]
    for fragment in fragments:
        for tag in re.findall(r"<link\b[^>]+>", fragment, re.I):
            if re.search(r"\brel\s*=\s*['\"]stylesheet['\"]", tag, re.I):
                match = re.search(r"\bhref\s*=\s*['\"](https?://[^'\"]+)['\"]", tag, re.I)
                if match:
                    pending.append((unescape(match.group(1)), None))
    visited, families = set(), set()
    while pending:
        base, text = pending.pop()
        if text is None:
            if base in visited:
                continue
            visited.add(base)
            result = fetch(base, root=root)
            if 300 <= result["status"] < 400 and result["headers"].get("location"):
                pending.append((urljoin(base, result["headers"]["location"]), None))
                continue
            if not 200 <= result["status"] < 300:
                raise ValueError(f"font_stylesheet_unavailable: HTTP {result['status']}: {base}")
            text = result["body"].decode("utf-8", errors="replace")
        for face in re.findall(r"@font-face\s*\{([^{}]*)\}", text, re.I):
            match = _FONT_FAMILY_DECLARATION.search(face)
            if match:
                families.update(_normalize_stack_members(match.group("value"))[0])
        for match in re.finditer(r"@import\s+(?:url\(\s*)?['\"]?([^'\"\s;)]+)", text, re.I):
            reference = urljoin(base, match.group(1))
            if urlsplit(reference).scheme in {"http", "https"}:
                pending.append((reference, None))
    return families


def _bundle_identity(
    root: Path,
    css: str,
    fragments: list[str],
    scan_named_families: bool = True,
) -> tuple[
    str,
    dict[str, str],
    dict[str, list[str]],
    dict[str, dict[str, str]],
    str,
    set[str],
    str,
    list[dict],
    dict[str, int],
    set[str],
]:
    """Return the semantic inputs that define one portable font bundle.

    ``pyftsubset`` may emit byte-different WOFF2 files for identical inputs.
    The delivery contract is semantic, so a bundle whose identity and audited
    files still match must be reused instead of regenerated.  Otherwise every
    no-op build changes ``base.css`` URLs and invalidates every page render.
    """
    root = root.resolve()
    _validate_font_allowlist()
    source_css = _without_bundle(css)
    pinned = _pinned_font_manifest(root)
    custom_fonts, role_overrides = _load_custom_config(root)
    authored_families = _authored_font_families(source_css, fragments, root)
    token_stacks = _active_token_stacks(source_css, fragments, pinned, custom_fonts, authored_families)
    for token, family in role_overrides.items():
        token_stacks[token] = _ensure_complete_cjk_fallback([family])
    resolutions, coverage = _compute_resolutions(root, token_stacks, custom_fonts, role_overrides, authored_families)
    source_dirs = _font_source_dirs()
    aliases = substitution_catalog().get("aliases") or {}
    # 同一份解析结果同时决定真实打包栈和审计回执，不能只在报告里宣称字体已生效。
    for row in resolutions:
        family = row["resolved_family"]
        if not family:
            raise ValueError(f"unresolved font intent for role {row['role']}: {row['requested_stack']}; "
                             f"choose available families from {sorted(FAMILY_FACES)} or an authorized user font")
        stack = [family]
        for requested in row["requested_stack"] + row["fallback_chain"]:
            candidate = requested if requested in custom_fonts else _resolve_alias(requested, aliases)
            if candidate not in stack and (candidate in authored_families or candidate in custom_fonts or (
                candidate in FAMILY_FACES and _family_available(candidate, source_dirs, pinned)
            )):
                stack.append(candidate)
        token_stacks[row["token"]] = _ensure_complete_cjk_fallback(stack)
    token_families = {token: stack[0] for token, stack in token_stacks.items()}
    families = {
        family
        for stack in token_stacks.values()
        for family in stack
    }
    # A portable CJK/symbol fallback is always bundled, including when a user
    # font is assigned to body text but does not cover every deck character.
    families.add("Noto Sans SC")
    if scan_named_families:
        styles = _FontStyles()
        for fragment in fragments:
            styles.feed(fragment)
        declarations = "\n".join([source_css, *styles.parts])
        definitions = _token_definitions(declarations)
        for declaration in _FONT_FAMILY_DECLARATION.finditer(declarations):
            value = declaration.group("value").strip()
            if re.sub(r"\s*!important\s*$", "", value, flags=re.I).lower() in {"inherit", "initial", "unset", "revert", "revert-layer"}:
                continue
            stack = _resolve_token_stack("__literal__", {**definitions, "__literal__": value})
            available = [family for family in stack if family in authored_families or family in custom_fonts or (
                family in FAMILY_FACES and _family_available(family, source_dirs, pinned))]
            if not available:
                raise ValueError(f"font_stack_unavailable: {value!r}; choose available families from "
                                 f"{sorted(FAMILY_FACES)} or an authorized user font")
            families.update(available)

    if pinned is not None:
        for family in families - custom_fonts.keys() - authored_families:
            for face in FAMILY_FACES[family]:
                _face_source(family, face, source_dirs, pinned)

    characters = _visible_characters(fragments)
    deck_id = hashlib.sha256(
        (characters + json.dumps(token_stacks, sort_keys=True, ensure_ascii=False)).encode("utf-8")
    ).hexdigest()[:10]
    return (
        source_css,
        token_families,
        token_stacks,
        custom_fonts,
        characters,
        families,
        deck_id,
        resolutions,
        coverage,
        authored_families,
    )


def _compute_resolutions(
    root: Path,
    token_stacks: dict[str, list[str]],
    custom_fonts: dict[str, dict],
    role_overrides: dict[str, str],
    authored_families=frozenset(),
) -> tuple[list[dict], dict[str, int]]:
    """Run the deterministic resolver over each intent role for the trace/manifest.

    Requested families come from ``font-intent.json`` when present;
    otherwise the resolved token stacks stand in as the request so that every
    deck records a coverage row per role.  ``source_available`` is bound to the
    hermetic release/source dirs here — the resolver itself stays pure.
    """
    catalog = substitution_catalog()
    source_dirs = _font_source_dirs()
    pinned = _pinned_font_manifest(root)

    def source_available(family: str) -> bool:
        return family in FAMILY_FACES and _family_available(family, source_dirs, pinned)

    intent = _load_font_intent(root)
    intent_roles = intent.get("roles") or {}
    resolutions: list[dict] = []
    coverage = {
        "request_count": 0,
        "exact_count": 0,
        "user_upload_count": 0,
        "fallback_count": 0,
        "unresolved_count": 0,
    }
    for role in INTENT_ROLES:
        token = INTENT_ROLE_TOKENS[role][0]
        route_stack = token_stacks.get(token) or token_stacks.get(INTENT_ROLE_TOKENS[role][-1]) or []
        spec = {} if token in role_overrides else intent_roles.get(role) or {}
        if not route_stack and not spec:
            continue
        requested_stack = spec.get("requested_stack") or list(route_stack)
        resolution = resolve_font_request(
            role=role,
            token=token,
            requested_stack=requested_stack,
            weight=spec.get("weight"),
            style=spec.get("style") or "normal",
            custom_registry=custom_fonts,
            route_default_stack=list(route_stack),
            source_available=source_available,
            catalog=catalog,
            authored_families=authored_families,
        )
        kind = resolution.resolution_kind
        family = resolution.resolved_family
        if kind == "exact_user_upload":
            license_kind, redistributable, user_authorized = "user-provided", False, True
        elif family in FAMILY_FACES and kind != "authored_css":
            metadata = FONT_LICENSES.get(family) or {}
            license_kind = str(metadata.get("license") or "")
            redistributable = _font_usage_scope(family) == "redistributable-ofl"
            user_authorized = False
        else:
            license_kind, redistributable, user_authorized = "", False, False
        resolutions.append(
            resolution.as_record(
                license=license_kind,
                redistributable=redistributable,
                user_authorized=user_authorized,
            )
        )
        coverage["request_count"] += 1
        if kind in {"exact_builtin", "exact_alias"}:
            coverage["exact_count"] += 1
        elif kind == "exact_user_upload":
            coverage["user_upload_count"] += 1
        elif kind == "portable_fallback":
            coverage["fallback_count"] += 1
        elif kind == "unresolved":
            coverage["unresolved_count"] += 1
    return resolutions, coverage


def _resolution_record(deck_id: str, resolutions: list[dict], coverage: dict) -> dict:
    catalog_fingerprint = substitution_catalog_fingerprint()
    fingerprint = hashlib.sha256(json.dumps({
        "catalog": catalog_fingerprint,
        "resolutions": [{key: row[key] for key in (
            "role", "requested_stack", "mapping_id", "resolved_family", "resolution_kind"
        )} for row in resolutions],
    }, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return {
        "deck_id": deck_id,
        "resolution_catalog": {"schema": SUBSTITUTIONS_SCHEMA,
                               "version": substitution_catalog().get("version"),
                               "sha256": catalog_fingerprint},
        "resolutions": resolutions, "coverage": coverage,
        "resolution_fingerprint": fingerprint,
    }


def _write_resolution_trace(root: Path, record: dict) -> None:
    _atomic_write(root / "_trace/font-resolution.json", json.dumps(record, ensure_ascii=False, indent=2) + "\n")


def prepare_authoring_fonts(root: Path, *, from_plans: bool = False, update_css: bool = True,
                            pages: tuple[int, ...] | None = None) -> dict:
    """作者引用固定字体对象；公共与用户私有字体都只在便携导出时子集化。"""
    _pinned_font_manifest(root, required=True)
    with _workspace_bundle_lock(root):
        return _prepare_authoring_fonts(root, from_plans=from_plans, update_css=update_css, pages=pages)


def _prepare_authoring_fonts(root: Path, *, from_plans: bool, update_css: bool = True,
                             pages: tuple[int, ...] | None = None) -> dict:
    root = root.resolve()
    css, fragments = _read_inputs(root, from_plans=from_plans, pages=pages)
    (
        source_css,
        token_families,
        token_stacks,
        custom_fonts,
        _characters,
        families,
        deck_id,
        resolutions,
        coverage,
        authored_families,
    ) = _bundle_identity(root, css, fragments, scan_named_families=True)
    source_dirs = _font_source_dirs()
    pinned = _pinned_font_manifest(root, required=True)
    rules: list[str] = []
    faces: list[dict[str, str]] = []
    for family in sorted(families - authored_families):
        custom = custom_fonts.get(family)
        selected = ((Face((str(custom["source"]),), custom["weight"], custom["style"]),)
                    if custom else FAMILY_FACES[family])
        for face in selected:
            source = (Path(custom["source"]) if custom else _face_source(family, face, source_dirs, pinned)).resolve()
            face_rule = (
                "@font-face {\n"
                f"  font-family: {json.dumps(family, ensure_ascii=False)};\n"
                f"  src: url({json.dumps(source.as_uri())}) format('truetype');\n"
                f"  font-style: {face.style};\n"
                f"  font-weight: {face.weight};\n"
                "  font-display: block;\n"
                "}"
            )
            rules.extend(face_rule.replace(json.dumps(family, ensure_ascii=False), json.dumps(name, ensure_ascii=False), 1)
                         for name in _font_face_names(family))
            faces.append({
                "family": family,
                "user_authorized": bool(custom),
                "weight": face.weight,
                "style": face.style,
                "source": str(source),
                "source_object_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            })
    overrides = "\n".join(
        f"  {token}: "
        + ", ".join(json.dumps(family, ensure_ascii=False) for family in stack)
        + f", {_generic_for(stack[0])};"
        for token, stack in sorted(token_stacks.items())
    )
    rules.append(":root {\n" + overrides + "\n}")
    block = AUTHORING_START + "\n" + "\n\n".join(rules) + "\n" + AUTHORING_END
    updated_css = source_css.rstrip() + "\n\n" + block + "\n"
    if updated_css != css:
        # Shared authoring faces must land in base.css even after page HTML exists.
        # Refusing this write left models with no recoverable tool after production started.
        _atomic_write(root / "base.css", updated_css)
    record = _resolution_record(deck_id, resolutions, coverage)
    record["token_stacks"] = token_stacks
    _write_resolution_trace(root, record)
    return {
        **record,
        "mode": "shared-authoring",
        "faces": faces,
        "token_families": token_families,
    }


def bundle_fonts(
    root: Path,
    css: str,
    fragments: list[str],
    scan_named_families: bool = True,
) -> str:
    (
        source_css,
        token_families,
        token_stacks,
        custom_fonts,
        characters,
        families,
        deck_id,
        resolutions,
        coverage,
        authored_families,
    ) = _bundle_identity(root.resolve(), css, fragments, scan_named_families)
    root = root.resolve()
    source_dirs = _font_source_dirs()
    pinned = _pinned_font_manifest(root)

    tool = shutil.which("pyftsubset")
    if not tool:
        raise FileNotFoundError("pyftsubset is required for portable deck fonts")
    delivery_families = {
        family: (family if family in authored_families else f"Deck-{deck_id}-{_slug(family) or 'font'}-{hashlib.sha256(family.encode('utf-8')).hexdigest()[:10]}")
        for family in sorted(families)
    }

    fonts_dir = root / "_runtime/tmp/fonts"
    fonts_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, str]] = []
    rules: list[str] = []
    notices: dict[str, dict[str, object]] = {}
    with tempfile.TemporaryDirectory(prefix="deck-fonts-", dir=root / "_runtime/tmp") as temporary:
        temporary_dir = Path(temporary)
        characters_file = temporary_dir / "characters.txt"
        characters_file.write_text(characters, encoding="utf-8")
        for family in sorted(families - authored_families):
            custom = custom_fonts.get(family)
            faces = (
                (Face((str(custom["source"]),), custom["weight"], custom["style"]),)
                if custom else FAMILY_FACES[family]
            )
            for face in faces:
                source = Path(face.source_names[0]) if custom else _face_source(family, face, source_dirs, pinned)
                source_metadata = _font_source_metadata(str(source))
                license_metadata = (
                    {"license": "user-provided", "source": "user upload"}
                    if custom else FONT_LICENSES[family]
                )
                # Redistributable OFL builtins point at the bundled OFL text;
                # non-OFL parity builtins (free-commercial / internal-only /
                # paid) point at the parity notice so their real terms are not
                # misrepresented as OFL.
                if custom:
                    license_file = "_runtime/tmp/fonts/USER-FONTS.txt"
                elif _font_usage_scope(family) == "redistributable-ofl":
                    license_file = "_runtime/tmp/fonts/OFL-1.1.txt"
                else:
                    license_file = "_runtime/tmp/fonts/PARITY-FONTS.txt"
                delivery = delivery_families[family]
                provisional = temporary_dir / f"{_slug(family)}-{face.weight.replace(' ', '-')}.woff2"
                _subset(
                    tool, source, provisional, characters_file,
                    delivery, face.weight, face.style,
                )
                data = provisional.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                # 源字体文件(子集化前)的 SHA-256:Harness 的 SharedFontStore
                # 用它把 runtime 子集回执匹配到已 pin 的 source object。custom 字体
                # 的 pin 记录键在 custom.sha256,builtin 则是源文件字节的哈希。
                if custom:
                    source_object_sha256 = str(custom.get("sha256") or "")
                else:
                    source_object_sha256 = hashlib.sha256(
                        source.read_bytes()
                    ).hexdigest()
                filename = f"{_slug(family)}-{face.weight.replace(' ', '-')}-{digest[:12]}.woff2"
                target = fonts_dir / filename
                _atomic_write(target, data)
                records.append(
                    {
                        "source_family": family,
                        "delivery_family": delivery,
                        "weight": face.weight,
                        "style": face.style,
                        "source": source.name,
                        "source_url": license_metadata["source"],
                        "license": license_metadata["license"],
                        "usage_scope": "" if custom else _font_usage_scope(family),
                        "license_file": license_file,
                        "copyright": source_metadata["copyright"],
                        "path": f"_runtime/tmp/fonts/{filename}",
                        "sha256": digest,
                        "source_object_sha256": source_object_sha256,
                        "user_authorized": bool(custom),
                        "original_sha256": str(custom.get("sha256") or "") if custom else "",
                        "source_path": str(custom.get("source_path") or "") if custom else "",
                        "source_object_ref": str(custom.get("object_ref") or "") if custom else "",
                    }
                )
                notice = notices.setdefault(
                    family,
                    {
                        "source_url": license_metadata["source"],
                        "license": license_metadata["license"],
                        "usage_scope": "" if custom else _font_usage_scope(family),
                        "copyright": set(),
                        "files": [],
                    },
                )
                if source_metadata["copyright"]:
                    notice["copyright"].add(source_metadata["copyright"])
                notice["files"].append(source.name)
                face_rule = (
                    "@font-face {\n"
                    f"  font-family: {json.dumps(delivery, ensure_ascii=False)};\n"
                    f"  src: url(\"./_runtime/tmp/fonts/{filename}\") format(\"woff2\");\n"
                    f"  font-style: {face.style};\n"
                    f"  font-weight: {face.weight};\n"
                    "  font-display: block;\n"
                    "}"
                )
                rules.extend(face_rule.replace(json.dumps(delivery, ensure_ascii=False), json.dumps(name, ensure_ascii=False), 1)
                             for name in [delivery, *_font_face_names(family)])

    _atomic_write(fonts_dir / "OFL-1.1.txt", OFL_TEMPLATE.read_bytes())
    _atomic_write(
        fonts_dir / "USER-FONTS.txt",
        "USER-PROVIDED FONT NOTICE\n\n"
        "Fonts marked user-provided were uploaded for this presentation. "
        "The user affirmed that they hold the rights required for presentation "
        "use and embedding. They are internally renamed and subset for this deck.\n",
    )
    _atomic_write(
        fonts_dir / "PARITY-FONTS.txt",
        "MIXTURE-V2 PARITY FONT NOTICE\n\n"
        "Fonts recorded with a non-OFL usage scope are vendored for parity with "
        "the mixture-v2 corpus. Their real license (free-commercial EULA, "
        "internal-only, or paid) is recorded per-face in manifest.json and "
        "LICENSES.txt; they are NOT SIL OFL and must not be treated as such. "
        "Internal-only faces are for this environment only, not external "
        "redistribution.\n",
    )
    notice_lines = [
        "FONT LICENSE NOTICES",
        "",
        "Redistributable built-in subsets use SIL Open Font License 1.1; see OFL-1.1.txt.",
        "Mixture-v2 parity subsets keep their real license; see PARITY-FONTS.txt.",
        "User-provided subsets rely on the user's authorization; see USER-FONTS.txt.",
    ]
    for family in sorted(notices):
        notice = notices[family]
        notice_lines.extend(
            [
                "",
                f"Family: {family}",
                f"License: {notice['license']}",
                f"Usage scope: {notice['usage_scope'] or 'user-provided'}",
                f"Official source: {notice['source_url']}",
                "Source files: " + ", ".join(sorted(set(notice["files"]))),
            ]
        )
        for copyright_line in sorted(notice["copyright"]):
            notice_lines.append("Copyright: " + copyright_line)
    _atomic_write(fonts_dir / "LICENSES.txt", "\n".join(notice_lines).rstrip() + "\n")
    active_font_files = {Path(record["path"]).name for record in records}
    for stale in fonts_dir.glob("*.woff2"):
        if stale.name not in active_font_files:
            stale.unlink()

    overrides = "\n".join(
        f"  {token}: "
        + ", ".join(json.dumps(delivery_families[family], ensure_ascii=False) for family in stack)
        + f", {_generic_for(stack[0])};"
        for token, stack in sorted(token_stacks.items())
    )
    rules.append(":root {\n" + overrides + "\n}")
    bundle = BUNDLE_START + "\n" + "\n\n".join(rules) + "\n" + BUNDLE_END
    resolution = _resolution_record(deck_id, resolutions, coverage)
    manifest = {
        "version": 5,
        "deck_id": deck_id,
        "character_count": len(characters),
        "token_families": token_families,
        "token_stacks": token_stacks,
        "delivery_families": delivery_families,
        "authored_css_families": sorted(authored_families),
        "faces": records,
        "license_files": [
            "_runtime/tmp/fonts/LICENSES.txt", "_runtime/tmp/fonts/OFL-1.1.txt",
            "_runtime/tmp/fonts/PARITY-FONTS.txt", "_runtime/tmp/fonts/USER-FONTS.txt",
        ],
        **resolution,
    }
    _atomic_write(fonts_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    _write_resolution_trace(root, resolution)
    # Keep role overrides last so the authoring :root declarations above cannot
    # overwrite the portable Deck family names in the CSS cascade.
    return source_css.rstrip() + "\n\n" + bundle + "\n"


def validate_font_bundle(root: Path) -> list[str]:
    root = root.resolve()
    manifest_path = root / "_runtime/tmp/fonts/manifest.json"
    css_path = root / "base.css"
    errors: list[str] = []
    if not manifest_path.is_file():
        return ["_runtime/tmp/fonts/manifest.json is missing"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"font manifest is unreadable: {exc}"]
    css = css_path.read_text(encoding="utf-8") if css_path.is_file() else ""
    if BUNDLE_START not in css or BUNDLE_END not in css:
        errors.append("base.css does not contain the deck font bundle")
    token_stacks = manifest.get("token_stacks")
    delivery_families = manifest.get("delivery_families") or {}
    if int(manifest.get("version") or 0) >= 4:
        if not isinstance(token_stacks, dict):
            errors.append("font manifest does not contain portable token stacks")
            token_stacks = {}
        else:
            for token, stack in token_stacks.items():
                if not isinstance(stack, list) or not stack:
                    errors.append(f"font token stack is empty: {token}")
                    continue
                if not any(family in _COMPLETE_CJK_FALLBACKS for family in stack):
                    errors.append(f"font token stack lacks a complete CJK fallback: {token}")
                for family in stack:
                    delivery = delivery_families.get(family)
                    if not delivery:
                        errors.append(f"font token stack references an unbundled family: {token} -> {family}")
                    elif json.dumps(delivery, ensure_ascii=False) not in css:
                        errors.append(f"font token delivery family is absent from base.css: {token} -> {family}")
    if int(manifest.get("version") or 0) >= 5:
        resolutions = manifest.get("resolutions")
        coverage = manifest.get("coverage") or {}
        if not isinstance(resolutions, list):
            errors.append("font manifest v5 lacks a resolution catalog")
        else:
            for row in resolutions:
                kind = row.get("resolution_kind")
                if kind not in RESOLUTION_KINDS:
                    errors.append(f"unknown resolution_kind: {kind!r}")
                if kind == "unresolved":
                    errors.append(f"font resolution reached {kind} for role {row.get('role')}")
                stack = token_stacks.get(row.get("token")) or []
                if not stack or stack[0] != row.get("resolved_family"):
                    errors.append(f"font resolution disagrees with delivered stack for role {row.get('role')}")
        if int(coverage.get("unresolved_count") or 0):
            errors.append("font coverage reports an unresolved role")
        if not manifest.get("resolution_fingerprint"):
            errors.append("font manifest v5 lacks a resolution_fingerprint")
    for relative in manifest.get("license_files", []):
        path = root / str(relative)
        if not path.is_file() or not path.stat().st_size:
            errors.append(f"bundled font license notice is missing or empty: {relative}")
    for record in manifest.get("faces", []):
        relative = str(record.get("path", ""))
        path = root / relative
        if not relative.startswith("_runtime/tmp/fonts/") or not path.is_file() or not path.stat().st_size:
            errors.append(f"bundled font is missing or empty: {relative or '<unset>'}")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != str(record.get("sha256", "")):
            errors.append(f"bundled font hash mismatch: {relative}")
        if str(record.get("delivery_family", "")) not in css:
            errors.append(f"delivery font family is absent from base.css: {record.get('delivery_family')}")
        license_kind = record.get("license")
        if license_kind == "user-provided":
            if not record.get("user_authorized") or not record.get("original_sha256") or not (record.get("source_path") or record.get("source_object_ref")):
                errors.append(f"user-provided font lacks authorization/provenance metadata: {relative}")
        elif license_kind:
            # Any built-in face (OFL or mixture-v2 parity) must carry an honest
            # source URL and a non-empty usage scope; the license string is not
            # required to be OFL, only present and truthful.
            if not record.get("source_url"):
                errors.append(f"bundled font lacks official source metadata: {relative}")
            if not record.get("usage_scope"):
                errors.append(f"bundled font lacks a usage-scope note: {relative}")
        else:
            errors.append(f"bundled font has unsupported license metadata: {relative}")
    declared = {Path(str(record.get("path", ""))).name for record in manifest.get("faces", [])}
    for stale in (root / "_runtime/tmp/fonts").glob("*.woff2"):
        if stale.name not in declared:
            errors.append(f"undeclared font file remains in bundle: _runtime/tmp/fonts/{stale.name}")
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
    """Re-render every slide with one shared Chromium process."""
    root = root.resolve()
    renderer = Path(__file__).resolve().with_name("render.py")
    result = subprocess.run(
        [sys.executable, str(renderer), "--batch", str(root)],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "batch render failed")[-1600:]
        raise RuntimeError(f"portable-font batch render failed: {detail}")
    print((result.stdout or "batch renders refreshed").strip())


def _read_inputs(root: Path, from_plans: bool = False, *, pages: tuple[int, ...] | None = None) -> tuple[str, list[str]]:
    css_path = root / "base.css"
    if not css_path.is_file():
        raise FileNotFoundError(f"missing {css_path}")
    files = ([root / "deck.md"] if from_plans and (root / "deck.md").is_file() else
             sorted(path for path in (root / "slides").glob("slide_*.html")
                    if ".bak." not in path.name) if not from_plans else [])
    if not files:
        source = root / ("deck.md" if from_plans else "slides")
        raise FileNotFoundError(f"no {'plans' if from_plans else 'slides'} found under {source}")
    if from_plans:
        from .deck_plan import read_deck
        _, plans = read_deck(root, pages=pages)
        files = list(dict.fromkeys([*files, *(root / page["path"] for page in plans)]))
        fragments = [_plan_font_fragment(path) for path in files]
    else:
        fragments = [path.read_text(encoding="utf-8") for path in files]
    return css_path.read_text(encoding="utf-8"), fragments


def _plan_font_fragment(path: Path) -> str:
    """仅分页屏显文本参与字形准备；字体选择来自实际 CSS 和显式字体请求。"""
    text = path.read_text(encoding="utf-8")
    if not path.name.startswith("slide_"):
        return ""
    match = re.search(
        r"(?ms)^##\s+(?:最终屏显文案|屏显文案|On-screen copy|Final on-screen copy)\s*$"
        r"(.*?)(?=^##\s+|\Z)",
        text,
    )
    screen_copy = match.group(1) if match else text
    return screen_copy


def bundle_workspace(root: Path, from_plans: bool = False) -> dict:
    """Bundle one workspace and return its validated manifest."""
    root = root.resolve()
    with _workspace_bundle_lock(root):
        # Re-read every input only after acquiring the lock. Another Group
        # Agent may have completed the identical bundle while this caller was
        # waiting, in which case the semantic-identity fast path returns it.
        css, fragments = _read_inputs(root, from_plans=from_plans)
        (
            _source_css,
            token_families,
            token_stacks,
            _custom_fonts,
            characters,
            families,
            deck_id,
            resolutions,
            _coverage,
            _authored_families,
        ) = _bundle_identity(
            root, css, fragments, scan_named_families=True
        )
        manifest_path = root / "_runtime/tmp/fonts/manifest.json"
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
        expected_families = sorted(families)
        if (
            existing.get("version") == 5
            and existing.get("deck_id") == deck_id
            and existing.get("character_count") == len(characters)
            and existing.get("token_families") == token_families
            and existing.get("token_stacks") == token_stacks
            and existing.get("resolutions") == resolutions
            and sorted((existing.get("delivery_families") or {}).keys())
            == expected_families
            and not validate_font_bundle(root)
        ):
            return existing
        _atomic_write(
            root / "base.css",
            bundle_fonts(root, css, fragments, scan_named_families=True),
        )
        errors = validate_font_bundle(root)
        if errors:
            raise RuntimeError("; ".join(errors))
        return json.loads(manifest_path.read_text(encoding="utf-8"))


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--audit-routes":
        report = audit_typography_routes()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ready"] else 1
    if argv and argv[0] == "--audit-release":
        report = audit_release_fonts()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ready"] else 1
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
    from_plans = "--from-plans" in argv[1:]
    manifest = bundle_workspace(root, from_plans=from_plans)
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
