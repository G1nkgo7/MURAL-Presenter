#!/usr/bin/env python3
"""Build the raster MURAL logo system from the approved brand characters."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
LOGO_DIR = ROOT / "assets" / "logo"
EXPORT_DIR = LOGO_DIR / "exports"
SOCIAL_DIR = ROOT / "assets" / "social"
SITE_PUBLIC_DIR = ROOT / "site" / "public"
PRIMARY_SOURCE = LOGO_DIR / "mural-logo-mark-v3.png"
COMPACT_SOURCE = LOGO_DIR / "mural-logo-mark-v1.png"
FONT = (
    ROOT.parent
    / "paper"
    / "figures"
    / "architecture"
    / "assets"
    / "Archivo.ttf"
)
HAND_FONT = LOGO_DIR / "fonts" / "Caveat-VariableFont_wght.ttf"

OFF_WHITE = "#F2F8F6"
TEAL = "#137F7B"
INK = "#263238"
MINT = "#B8DDD7"
AMBER = "#D89A45"
DARK = "#172428"
SLATE = "#536168"


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


def draw_segmented_wordmark(
    canvas: Image.Image,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    *,
    top_color: str,
    lower_color: str,
    revision_color: str,
    background: str,
    spacing: int,
) -> None:
    """Render a mural-like wordmark with one shared lower band and an amber R."""
    start_x, y = xy
    probe = ImageDraw.Draw(canvas)
    positions: list[tuple[int, int]] = []
    cursor = start_x
    for char in text:
        width = int(probe.textlength(char, font=font))
        positions.append((cursor, width))
        cursor += width + spacing

    mask = Image.new("L", canvas.size, 0)
    mask_draw = ImageDraw.Draw(mask)
    draw_letterspaced(mask_draw, xy, text, font, 255, spacing)

    fill = Image.new("RGB", canvas.size, top_color)
    fill_draw = ImageDraw.Draw(fill)
    split_y = y + int(font.size * 0.73)
    fill_draw.rectangle((start_x, split_y, cursor, y + font.size + 20), fill=lower_color)

    # R owns the revision accent while every glyph remains on the same shared band.
    if len(positions) >= 3:
        r_x, r_width = positions[2]
        fill_draw.rectangle((r_x, split_y, r_x + r_width, y + font.size + 20), fill=revision_color)

    # The canvas-colored seam turns the letters into a shared five-tile mural.
    seam = max(5, font.size // 30)
    fill_draw.rectangle((start_x, split_y - seam // 2, cursor, split_y + seam // 2), fill=background)
    canvas.paste(fill, (0, 0), mask)


def draw_acronym_row(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    terms: tuple[tuple[str, str, str], ...],
    *,
    text_font: ImageFont.FreeTypeFont,
    initial_font: ImageFont.FreeTypeFont,
    text_color: str,
    separator_color: str,
    connector: str | None = None,
) -> None:
    """Draw a compact editorial signature with explicit acronym initials."""
    x, y = xy
    line_mid = y + 25
    term_gap = 30
    if connector:
        draw.text((x, line_mid), connector, font=text_font, fill=text_color, anchor="lm")
        x += int(draw.textlength(connector, font=text_font)) + 14

    for index, (initial, remainder, initial_color) in enumerate(terms):
        initial_x = x
        draw.text((x, line_mid), initial, font=initial_font, fill=initial_color, anchor="lm")
        initial_width = int(draw.textlength(initial, font=initial_font))
        draw.rounded_rectangle(
            (initial_x, y + 46, initial_x + initial_width, y + 50),
            radius=2,
            fill=initial_color,
        )
        x += initial_width + 4
        draw.text(
            (x, line_mid),
            remainder,
            font=text_font,
            fill=text_color,
            anchor="lm",
        )
        x += int(draw.textlength(remainder, font=text_font))
        if index < len(terms) - 1:
            dot_x = x + term_gap // 2
            draw.ellipse((dot_x - 3, line_mid - 3, dot_x + 3, line_mid + 3), fill=separator_color)
            x += term_gap


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
    width, height = 1800, 620
    background = DARK if dark else OFF_WHITE
    canvas = Image.new("RGB", (width, height), background)
    mark_fit = contain(mark, (490, 490))
    canvas.paste(mark_fit, (54, (height - mark_fit.height) // 2 + 8), mark_fit)

    draw = ImageDraw.Draw(canvas)
    word_font = ImageFont.truetype(str(FONT), 210)
    kicker_font = ImageFont.truetype(str(FONT), 18)
    badge_font = ImageFont.truetype(str(FONT), 21)
    initial_font = ImageFont.truetype(str(FONT), 54)
    label_font = ImageFont.truetype(str(FONT), 20)
    long_label_font = ImageFont.truetype(str(FONT), 17)
    word_color = OFF_WHITE if dark else INK
    secondary = MINT if dark else SLATE
    panel = "#203238" if dark else "#E8F2EF"
    panel_alt = "#3C3020" if dark else "#FFF1D6"
    panel_rule = "#476167" if dark else "#C6DAD6"
    start_x = 565
    word_y = 48
    draw_segmented_wordmark(
        canvas,
        (start_x, word_y),
        "MURAL",
        word_font,
        top_color=word_color,
        lower_color=MINT if dark else TEAL,
        revision_color=AMBER,
        background=background,
        spacing=12,
    )

    # PRESENTER behaves as an editorial edition mark rather than a loose suffix.
    badge_w = 214
    badge_x = width - badge_w - 72
    draw.rounded_rectangle(
        (badge_x, 83, badge_x + badge_w, 129),
        radius=8,
        fill=TEAL if dark else INK,
    )
    badge_text = "PRESENTER"
    badge_text_w = letterspaced_width(draw, badge_text, badge_font, 3)
    draw_letterspaced(
        draw,
        (badge_x + (badge_w - badge_text_w) // 2, 93),
        badge_text,
        badge_font,
        OFF_WHITE,
        3,
    )

    decoder_y = 316
    draw.text(
        (start_x, decoder_y),
        "THE NAME IS THE METHOD",
        font=kicker_font,
        fill=TEAL if dark else TEAL,
    )
    kicker_w = int(draw.textlength("THE NAME IS THE METHOD", font=kicker_font))
    draw.line(
        (start_x + kicker_w + 22, decoder_y + 12, width - 72, decoder_y + 12),
        fill=panel_rule,
        width=2,
    )

    cards = (
        ("M", ("MULTI-AGENT",), False),
        ("U", ("UNIFIED",), False),
        ("R", ("REVISION-AWARE",), True),
        ("A", ("AUTHORING",), False),
        ("L", ("LONG-HORIZON", "PRESENTATIONS"), False),
    )
    card_y = 354
    card_h = 132
    gap = 8
    available = width - 72 - start_x
    card_w = (available - gap * 4) // 5
    for index, (initial, labels, is_revision) in enumerate(cards):
        x = start_x + index * (card_w + gap)
        fill = panel_alt if is_revision else panel
        initial_color = AMBER if is_revision else (MINT if dark else TEAL)
        draw.rounded_rectangle(
            (x, card_y, x + card_w, card_y + card_h),
            radius=10,
            fill=fill,
            outline=panel_rule,
            width=1,
        )
        draw.text((x + 18, card_y + 12), initial, font=initial_font, fill=initial_color)
        draw.rectangle((x + 18, card_y + 80, x + 58, card_y + 84), fill=initial_color)
        active_font = long_label_font if initial in {"R", "L"} else label_font
        label_y = card_y + 91
        for row, label in enumerate(labels):
            draw.text(
                (x + 18, label_y + row * 20),
                label,
                font=active_font,
                fill=word_color,
            )

    draw.text(
        (start_x, 520),
        "ONE SHARED DECK STATE  /  BRIEF → GROUPS → REVIEW → REVISION",
        font=kicker_font,
        fill=secondary,
    )
    return canvas


def build_brand_sheet(
    primary: Image.Image, compact: Image.Image, light: Image.Image, dark: Image.Image
) -> None:
    canvas = Image.new("RGB", (2000, 1500), "#FFFFFF")
    draw = ImageDraw.Draw(canvas)
    title_font = ImageFont.truetype(str(FONT), 72)
    label_font = ImageFont.truetype(str(FONT), 34)
    draw.text((90, 62), "MURAL Logo System", font=title_font, fill=INK)
    draw.text((92, 150), "Multi-agent muralists + living M · primary identity", font=label_font, fill=TEAL)

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

    light_small = light.resize((1280, 441), Image.Resampling.LANCZOS)
    canvas.paste(light_small, (620, 230))
    draw.text((620, 695), "Horizontal lockup · light", font=label_font, fill=INK)

    dark_small = dark.resize((1280, 441), Image.Resampling.LANCZOS)
    canvas.paste(dark_small, (620, 805))
    draw.text((620, 1270), "Horizontal lockup · dark", font=label_font, fill=INK)

    swatches = [(TEAL, "Teal"), (INK, "Ink"), (AMBER, "Revision"), (OFF_WHITE, "Canvas")]
    for index, (color, label) in enumerate(swatches):
        column = index % 2
        row = index // 2
        x = 90 + column * 220
        y = 1090 + row * 92
        draw.rounded_rectangle((x, y, x + 62, y + 62), radius=12, fill=color, outline="#D4DDDA")
        draw.text((x + 76, y + 16), label, font=small_label_font, fill=INK)

    canvas.save(EXPORT_DIR / "mural-logo-system-sheet.png", optimize=True)


def build_social_card(mark: Image.Image) -> None:
    """Build the release social card with exact typography and the approved mark."""
    width, height = 1200, 630
    canvas = Image.new("RGB", (width, height), OFF_WHITE)
    draw = ImageDraw.Draw(canvas)

    # Quiet registration lines echo the grid used by the paper figures.
    grid = "#CDE2DE"
    draw.line((35, 0, 35, height), fill=TEAL, width=1)
    draw.line((0, 582, width, 582), fill=TEAL, width=1)
    draw.ellipse((31, 578, 39, 586), fill=TEAL)

    brand_font = ImageFont.truetype(str(FONT), 46)
    headline_font = ImageFont.truetype(str(FONT), 75)
    badge_font = ImageFont.truetype(str(FONT), 22)
    draw_letterspaced(draw, (72, 50), "MURAL", brand_font, TEAL, 2)

    headline = ("A presentation", "is not a stack", "of slides.")
    for row, text in enumerate(headline):
        draw.text((74, 154 + row * 82), text, font=headline_font, fill=INK)

    draw.rounded_rectangle((78, 446, 342, 500), radius=13, fill="#F2A51A")
    draw.text((100, 459), "RESEARCH PREVIEW", font=badge_font, fill=INK)

    # A subtle page trail links the headline to the shared mural.
    for index in range(5):
        x = 420 + index * 35
        y = 356 + index * 14
        draw.rounded_rectangle(
            (x, y, x + 105, y + 76),
            radius=6,
            fill="#F8FBFA",
            outline=grid,
            width=2,
        )
        draw.rectangle((x + 65, y + 18, x + 92, y + 25), fill=TEAL)

    mark_fit = contain(mark, (570, 570))
    canvas.paste(mark_fit, (610, (height - mark_fit.height) // 2), mark_fit)

    SOCIAL_DIR.mkdir(parents=True, exist_ok=True)
    SITE_PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    canvas.save(SOCIAL_DIR / "mural-og-fallback-1200x630.png", optimize=True)

    # The public OG image may be an art-directed release asset. Keep the deterministic
    # composition as a clean-clone fallback without overwriting an approved social card.
    public_card = SITE_PUBLIC_DIR / "og.png"
    if not public_card.exists():
        canvas.save(public_card, optimize=True)


def main() -> None:
    if not PRIMARY_SOURCE.exists():
        raise FileNotFoundError(PRIMARY_SOURCE)
    if not COMPACT_SOURCE.exists():
        raise FileNotFoundError(COMPACT_SOURCE)
    if not FONT.exists():
        raise FileNotFoundError(FONT)
    if not HAND_FONT.exists():
        raise FileNotFoundError(HAND_FONT)

    primary = trim_alpha(Image.open(PRIMARY_SOURCE), pad=8)
    compact = trim_alpha(Image.open(COMPACT_SOURCE), pad=8)
    build_mark_exports(primary, compact)
    light = build_lockup(primary, dark=False)
    dark = build_lockup(primary, dark=True)
    light.save(EXPORT_DIR / "mural-logo-lockup-light.png", optimize=True)
    dark.save(EXPORT_DIR / "mural-logo-lockup-dark.png", optimize=True)
    build_brand_sheet(primary, compact, light, dark)
    build_social_card(primary)

    print(f"Built MURAL logo exports in {EXPORT_DIR}")


if __name__ == "__main__":
    main()
