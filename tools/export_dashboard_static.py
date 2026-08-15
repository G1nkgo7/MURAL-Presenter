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
DEFAULT_TARGET = ROOT / "dist" / "dashboard-static"
DEFAULT_PUBLIC_PREFIX = "/static/mural"
DEFAULT_EXTERNAL_BASE = "http://localhost:8000/static/mural"

ROUTES = {
    "/": "index.html",
    "/zh": "zh.html",
    "/blog": "blog.html",
    "/zh/blog": "zh-blog.html",
    "/paper": "paper.html",
    "/zh/paper": "zh-paper.html",
}


def fetch(origin: str, route: str) -> str:
    with urllib.request.urlopen(origin.rstrip("/") + route, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(f"{route} returned HTTP {response.status}")
        return response.read().decode("utf-8")


def make_static(html: str, public_prefix: str, external_base: str) -> str:
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
        'href="/zh/paper"': f'href="{public_prefix}/zh-paper.html"',
        'href="/paper"': f'href="{public_prefix}/paper.html"',
        'href="/zh/blog"': f'href="{public_prefix}/zh-blog.html"',
        'href="/blog"': f'href="{public_prefix}/blog.html"',
        'href="/zh"': f'href="{public_prefix}/zh.html"',
        'href="/"': f'href="{public_prefix}/index.html"',
    }
    for source, target in route_links.items():
        html = html.replace(source, target)

    for asset_root in (
        "_next/",
        "fonts/",
        "mural-mark-v2.png",
        "mural-mark.png",
        "mural-mascot.png",
        "mural-blog-hero-source.png",
        "mural-paper.pdf",
        "mural-paper-zh.pdf",
        "mural-paper-cover-en.png",
        "mural-paper-cover-zh.png",
        "execution-topologies.png",
        "authoring-lifecycle.png",
        "favicon-v2.png",
        "favicon.png",
        "og.png",
    ):
        html = html.replace(f'"/{asset_root}', f'"{public_prefix}/{asset_root}')

    html = html.replace("http://localhost:3000/og.png", f"{external_base}/og.png")
    return html


def inline_handwriting_font(target: Path) -> None:
    font_path = CLIENT / "fonts" / "caveat-variable.ttf"
    font_data = base64.b64encode(font_path.read_bytes()).decode("ascii")
    data_url = f"data:font/ttf;base64,{font_data}"
    for css_path in (target / "_next" / "static" / "css").glob("*.css"):
        css = css_path.read_text(encoding="utf-8")
        css = css.replace("/fonts/caveat-variable.ttf", data_url)
        css_path.write_text(css, encoding="utf-8")


def export(
    origin: str,
    target: Path,
    public_prefix: str,
    external_base: str,
) -> None:
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
        (target / filename).write_text(
            make_static(fetch(origin, route), public_prefix, external_base),
            encoding="utf-8",
        )

    print(f"Exported {len(ROUTES)} pages to {target}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", default="http://127.0.0.1:3100")
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument(
        "--public-prefix",
        default=DEFAULT_PUBLIC_PREFIX,
        help="URL path at which the exported directory will be served",
    )
    parser.add_argument(
        "--external-base",
        default=DEFAULT_EXTERNAL_BASE,
        help="Absolute public URL base used by social metadata",
    )
    args = parser.parse_args()
    public_prefix = "/" + args.public_prefix.strip("/")
    external_base = args.external_base.rstrip("/")
    export(
        args.origin,
        args.target.resolve(),
        public_prefix,
        external_base,
    )


if __name__ == "__main__":
    main()
