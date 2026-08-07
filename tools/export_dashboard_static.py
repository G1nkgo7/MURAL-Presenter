#!/usr/bin/env python3
"""Export the MURAL site as dependency-free pages for the local dashboard."""

from __future__ import annotations

import argparse
import base64
import re
import shutil
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
CLIENT = SITE / "dist" / "client"
DEFAULT_TARGET = Path(
    "/mnt/afs/hejiatong/multimodal_design/ppt-agent/dashboard/static/mural"
)
PUBLIC_PREFIX = "/static/mural"
EXTERNAL_BASE = "http://10.210.6.10:19117/static/mural"

ROUTES = {
    "/": "index.html",
    "/zh": "zh.html",
    "/blog": "blog.html",
    "/zh/blog": "zh-blog.html",
}


def fetch(origin: str, route: str) -> str:
    with urllib.request.urlopen(origin.rstrip("/") + route, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(f"{route} returned HTTP {response.status}")
        return response.read().decode("utf-8")


def make_static(html: str) -> str:
    # The pages contain no client-side interactions. Removing the RSC runtime
    # makes this export independent from a persistent Node process.
    html = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.I | re.S)
    html = re.sub(
        r"<link\b(?=[^>]*\brel=[\"']modulepreload[\"'])[^>]*>",
        "",
        html,
        flags=re.I,
    )

    route_links = {
        'href="/zh/blog"': f'href="{PUBLIC_PREFIX}/zh-blog.html"',
        'href="/blog"': f'href="{PUBLIC_PREFIX}/blog.html"',
        'href="/zh"': f'href="{PUBLIC_PREFIX}/zh.html"',
        'href="/"': f'href="{PUBLIC_PREFIX}/index.html"',
    }
    for source, target in route_links.items():
        html = html.replace(source, target)

    for asset_root in (
        "_next/",
        "fonts/",
        "mural-mark.png",
        "mural-mascot.png",
        "execution-topologies.png",
        "authoring-lifecycle.png",
        "favicon.png",
        "og.png",
    ):
        html = html.replace(f'"/{asset_root}', f'"{PUBLIC_PREFIX}/{asset_root}')

    html = html.replace("http://localhost:3000/og.png", f"{EXTERNAL_BASE}/og.png")
    return html


def inline_handwriting_font(target: Path) -> None:
    font_path = CLIENT / "fonts" / "caveat-variable.ttf"
    font_data = base64.b64encode(font_path.read_bytes()).decode("ascii")
    data_url = f"data:font/ttf;base64,{font_data}"
    for css_path in (target / "_next" / "static" / "css").glob("*.css"):
        css = css_path.read_text(encoding="utf-8")
        css = css.replace("/fonts/caveat-variable.ttf", data_url)
        css_path.write_text(css, encoding="utf-8")


def export(origin: str, target: Path) -> None:
    if not (CLIENT / "_next").is_dir():
        raise FileNotFoundError("Run the site build before exporting")

    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(CLIENT / "_next", target / "_next", dirs_exist_ok=True)
    for item in CLIENT.iterdir():
        if item.name.startswith(".") or item.name == "_next":
            continue
        destination = target / item.name
        if item.is_dir():
            shutil.copytree(item, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(item, destination)

    inline_handwriting_font(target)
    for route, filename in ROUTES.items():
        (target / filename).write_text(make_static(fetch(origin, route)), encoding="utf-8")

    print(f"Exported {len(ROUTES)} pages to {target}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", default="http://127.0.0.1:3100")
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    args = parser.parse_args()
    export(args.origin, args.target.resolve())


if __name__ == "__main__":
    main()
