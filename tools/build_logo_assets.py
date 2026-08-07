#!/usr/bin/env python3
"""Build the raster MURAL logo system from the approved brand characters."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
LOGO_DIR = ROOT / "assets" / "logo"
EXPORT_DIR = LOGO_DIR / "exports"
PRIMARY_SOURCE = LOGO_DIR / "mural-logo-mark-v2.png"
COMPACT_SOURCE = LOGO_DIR / "mural-logo-mark-v1.png"
FONT = (
    ROOT.parent
    / "paper"
    / "figures"
    / "architecture"
    / "assets"
    / "Archivo.ttf"
)

OFF_WHITE = "#F2F8F6"
TEAL = "#137F7B"
INK = "#263238"
MINT = "#B8DDD7"
AMBER = "#D89A45"
DARK = "#172428"


def trim_alpha(image: Image.Image, pad: int = 0) -> Image.Image:
    image = image.convert("RGBA")
    bbox = image.getchannel("A").getbbox()
    if not bbox:
        raise ValueError("Logo source has no opaque pixels")
    cropped = image.crop(bbox)
    if pad <= 0:
        return cropped
    canvas = Image.new(
        "RGBA", (cropped.width + 2 * pad, cropped.height + 2 * pad), (0, 0, 0, 0)
    )
    canvas.alpha_composite(cropped, (pad, pad))
    return canvas


def contain(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    result = image.copy()
    result.thumbnail(size, Image.Resampling.LANCZOS)
    return result


def letterspaced_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, spacing: int) -> int:
    widths = [draw.textlength(char, font=font) for char in text]
    return int(sum(widths) + spacing * max(0, len(text) - 1))


def draw_letterspaced(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: str,
    spacing: int,
) -> None:
    x, y = xy
    for char in text:
        draw.text((x, y), char, font=font, fill=fill, anchor="la")
        x += int(draw.textlength(char, font=font)) + spacing


def export_square(mark: Image.Image, name: str, sizes: tuple[int, ...]) -> None:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    for size in sizes:
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        fitted = contain(mark, (int(size * 0.88), int(size * 0.88)))
        x = (size - fitted.width) // 2
        y = (size - fitted.height) // 2
        canvas.alpha_composite(fitted, (x, y))
        canvas.save(EXPORT_DIR / f"{name}-{size}.png", optimize=True)


def build_mark_exports(primary: Image.Image, compact: Image.Image) -> None:
    export_square(primary, "mural-mascot", (1024, 512, 256, 128))
    export_square(compact, "mural-mark", (1024, 512, 256, 128, 64, 32))

    avatar = Image.new("RGB", (1024, 1024), OFF_WHITE)
    fitted = contain(primary, (900, 900))
    avatar.paste(fitted, ((1024 - fitted.width) // 2, (1024 - fitted.height) // 2), fitted)
    avatar.save(EXPORT_DIR / "mural-github-avatar-1024.png", optimize=True)


def build_lockup(mark: Image.Image, *, dark: bool) -> Image.Image:
    width, height = 1800, 560
    background = DARK if dark else OFF_WHITE
    canvas = Image.new("RGB", (width, height), background)
    mark_fit = contain(mark, (430, 430))
    canvas.paste(mark_fit, (72, (height - mark_fit.height) // 2), mark_fit)

    draw = ImageDraw.Draw(canvas)
    word_font = ImageFont.truetype(str(FONT), 224)
    tag_font = ImageFont.truetype(str(FONT), 47)
    word_color = OFF_WHITE if dark else INK
    tag_color = MINT if dark else TEAL
    start_x = 565
    word_y = 92
    draw_letterspaced(draw, (start_x, word_y), "MURAL", word_font, word_color, 12)
    draw.rounded_rectangle((start_x, 357, start_x + 96, 369), radius=6, fill=AMBER)
    draw.text(
        (start_x + 122, 333),
        "Multi-Agent Unified Revision-Aware Authoring",
        font=tag_font,
        fill=tag_color,
    )
    return canvas


def build_brand_sheet(
    primary: Image.Image, compact: Image.Image, light: Image.Image, dark: Image.Image
) -> None:
    canvas = Image.new("RGB", (2000, 1320), "#FFFFFF")
    draw = ImageDraw.Draw(canvas)
    title_font = ImageFont.truetype(str(FONT), 72)
    label_font = ImageFont.truetype(str(FONT), 34)
    draw.text((90, 62), "MURAL Logo System", font=title_font, fill=INK)
    draw.text((92, 150), "Muralist robot + living M · release candidate v2", font=label_font, fill=TEAL)

    avatar = Image.open(EXPORT_DIR / "mural-github-avatar-1024.png").resize(
        (430, 430), Image.Resampling.LANCZOS
    )
    canvas.paste(avatar, (90, 240))
    draw.text((90, 692), "Primary mascot / GitHub avatar", font=label_font, fill=INK)

    compact_fit = contain(compact, (210, 210))
    compact_card = Image.new("RGB", (430, 260), OFF_WHITE)
    compact_card.paste(
        compact_fit,
        ((430 - compact_fit.width) // 2, (230 - compact_fit.height) // 2),
        compact_fit,
    )
    canvas.paste(compact_card, (90, 760))
    small_label_font = ImageFont.truetype(str(FONT), 27)
    draw.text((90, 1035), "Compact mark / favicon", font=small_label_font, fill=INK)

    light_small = light.resize((1280, 398), Image.Resampling.LANCZOS)
    canvas.paste(light_small, (620, 230))
    draw.text((620, 650), "Horizontal lockup · light", font=label_font, fill=INK)

    dark_small = dark.resize((1280, 398), Image.Resampling.LANCZOS)
    canvas.paste(dark_small, (620, 760))
    draw.text((620, 1180), "Horizontal lockup · dark", font=label_font, fill=INK)

    swatches = [(TEAL, "Teal"), (INK, "Ink"), (AMBER, "Revision"), (OFF_WHITE, "Canvas")]
    for index, (color, label) in enumerate(swatches):
        column = index % 2
        row = index // 2
        x = 90 + column * 220
        y = 1090 + row * 92
        draw.rounded_rectangle((x, y, x + 62, y + 62), radius=12, fill=color, outline="#D4DDDA")
        draw.text((x + 76, y + 16), label, font=small_label_font, fill=INK)

    canvas.save(EXPORT_DIR / "mural-logo-system-sheet.png", optimize=True)


def main() -> None:
    if not PRIMARY_SOURCE.exists():
        raise FileNotFoundError(PRIMARY_SOURCE)
    if not COMPACT_SOURCE.exists():
        raise FileNotFoundError(COMPACT_SOURCE)
    if not FONT.exists():
        raise FileNotFoundError(FONT)

    primary = trim_alpha(Image.open(PRIMARY_SOURCE), pad=8)
    compact = trim_alpha(Image.open(COMPACT_SOURCE), pad=8)
    build_mark_exports(primary, compact)
    light = build_lockup(primary, dark=False)
    dark = build_lockup(primary, dark=True)
    light.save(EXPORT_DIR / "mural-logo-lockup-light.png", optimize=True)
    dark.save(EXPORT_DIR / "mural-logo-lockup-dark.png", optimize=True)
    build_brand_sheet(primary, compact, light, dark)

    print(f"Built MURAL logo exports in {EXPORT_DIR}")


if __name__ == "__main__":
    main()
