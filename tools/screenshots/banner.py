# -*- coding: utf-8 -*-
"""
<summary>
Draws the README banner (1280 by 320) and the GitHub social preview
(1280 by 640) into docs/screenshots/.
</summary>
<remarks>
Flat navy background, the name and a one line tagline on the left, and three
rounded tiles on the right showing things the dashboard really draws: the
hours watched this month, the watching calendar and the genre bars. The
figures are the demo library's (see catalogue.py and generate.py), never
anybody's real viewing.

Tile colours are the dashboard's own: purple for films and green for
episodes, the --movie and --episode values in resources/web/dashboard.html.
Change them there and here together.

Drawn at twice the size and scaled down, because Pillow does not smooth the
edges of rounded rectangles. Needs Pillow and the Noto Sans Bold font, with
DejaVu Sans Bold as the fallback. The PNGs are saved with no metadata
chunks, which the publish scan checks for.

Usage, from the project root:
    python3 tools/screenshots/banner.py [OUT_DIR]
Existing files are refused rather than overwritten; move them aside first.
</remarks>
"""

import os
import random
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(HERE))

BACKGROUND = (0x0D, 0x16, 0x2B)
TILE = (0x08, 0x0F, 0x1F)
NAME_COLOUR = (0xF0, 0xF4, 0xFA)
TAGLINE_COLOUR = (0x94, 0xA3, 0xB8)
FILMS = (0x7C, 0x5C, 0xFF)
EPISODES = (0x35, 0xC2, 0xA5)

NAME = "JellyStat"
TAGLINE = "Watch statistics from Jellyfin, inside Kodi"

FONT_PATHS = (
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)
FONT_REGULAR_PATHS = (
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)

SCALE = 2
LEFT = 80


def font(paths, size):
    """
    <summary>Load the first font on the list that exists.</summary>
    <param name="paths">Candidate .ttf paths, best first.</param>
    <param name="size">Pixel size, already multiplied by SCALE.</param>
    <returns>A FreeType font.</returns>
    <exception cref="SystemExit">When none of the fonts is installed.</exception>
    """
    for path in paths:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    raise SystemExit("no usable font: install fonts-noto-core or fonts-dejavu")


def mix(colour, amount):
    """
    <summary>Blend an accent towards the tile colour, for the quieter squares.</summary>
    <param name="colour">RGB accent.</param>
    <param name="amount">0 gives the tile colour, 1 the full accent.</param>
    <returns>An RGB tuple.</returns>
    """
    return tuple(int(t + (c - t) * amount) for c, t in zip(colour, TILE))


def hours_tile(draw, x, y, size):
    """
    <summary>The headline tile: hours watched this month, in the films purple.</summary>
    <param name="draw">ImageDraw on the doubled canvas.</param>
    <param name="x">Left edge, doubled pixels.</param>
    <param name="y">Top edge, doubled pixels.</param>
    <param name="size">Side of the square, doubled pixels.</param>
    <remarks>"1d 21h" is the demo library's This month figure on Home.</remarks>
    """
    pad = size // 9
    cx, cy, r = x + pad + size // 10, y + pad + size // 10, size // 10
    width = max(2, size // 45)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=FILMS, width=width)
    draw.line((cx, cy, cx, cy - r * 0.6), fill=FILMS, width=width)
    draw.line((cx, cy, cx + r * 0.5, cy), fill=FILMS, width=width)
    big = font(FONT_PATHS, int(size * 0.25))
    small = font(FONT_REGULAR_PATHS, int(size * 0.095))
    draw.text((x + pad, y + size * 0.42), "1d 21h", font=big, fill=FILMS)
    draw.text((x + pad, y + size * 0.74), "this month", font=small,
              fill=TAGLINE_COLOUR)


def calendar_tile(draw, x, y, size):
    """
    <summary>The watching calendar: a block of daily squares in the episodes green.</summary>
    <param name="draw">ImageDraw on the doubled canvas.</param>
    <param name="x">Left edge, doubled pixels.</param>
    <param name="y">Top edge, doubled pixels.</param>
    <param name="size">Side of the square, doubled pixels.</param>
    <remarks>
    A fixed seed, so every run draws the same pattern. Weekends and the last
    fortnight are busier, as the demo timeline is.
    </remarks>
    """
    rng = random.Random(7)
    cols, rows = 8, 7
    pad = size // 9
    gap = max(2, size // 60)
    cell = (size - 2 * pad - (cols - 1) * gap) // cols
    top = y + (size - (rows * cell + (rows - 1) * gap)) // 2
    for col in range(cols):
        for row in range(rows):
            busy = 0.25 + (0.35 if row >= 5 else 0) + (0.3 if col >= 6 else 0)
            roll = rng.random()
            level = 0.12 if roll > busy else rng.choice((0.45, 0.7, 1.0))
            left = x + pad + col * (cell + gap)
            upper = top + row * (cell + gap)
            draw.rounded_rectangle((left, upper, left + cell, upper + cell),
                                   radius=max(2, cell // 6),
                                   fill=mix(EPISODES, level))


def genre_tile(draw, x, y, size):
    """
    <summary>The genre bars: five shares of the demo library's films, in purple.</summary>
    <param name="draw">ImageDraw on the doubled canvas.</param>
    <param name="x">Left edge, doubled pixels.</param>
    <param name="y">Top edge, doubled pixels.</param>
    <param name="size">Side of the square, doubled pixels.</param>
    """
    shares = (1.0, 0.6, 0.42, 0.4, 0.28)
    pad = size // 9
    track = size - 2 * pad
    bar = size // 14
    gap = (size - 2 * pad - len(shares) * bar) // (len(shares) - 1)
    for i, share in enumerate(shares):
        upper = y + pad + i * (bar + gap)
        draw.rounded_rectangle((x + pad, upper, x + pad + track, upper + bar),
                               radius=bar // 2, fill=mix(FILMS, 0.18))
        draw.rounded_rectangle((x + pad, upper, x + pad + int(track * share),
                                upper + bar), radius=bar // 2, fill=FILMS)


def render(width, height, tile, name_size):
    """
    <summary>Draw one image: text block on the left, three tiles on the right.</summary>
    <param name="width">Final width in pixels.</param>
    <param name="height">Final height in pixels.</param>
    <param name="tile">Final tile side in pixels.</param>
    <param name="name_size">Final pixel size of the name.</param>
    <returns>An RGB image at the final size.</returns>
    <remarks>
    The tagline starts at a third of the name's size and shrinks only if it
    would run into the tiles, so it always stays on one line.
    </remarks>
    """
    w, h, t = width * SCALE, height * SCALE, tile * SCALE
    image = Image.new("RGB", (w, h), BACKGROUND)
    draw = ImageDraw.Draw(image)

    gap = 20 * SCALE
    right_margin = 64 * SCALE
    tiles_left = w - right_margin - 3 * t - 2 * gap
    tiles_top = (h - t) // 2
    for i, painter in enumerate((hours_tile, calendar_tile, genre_tile)):
        left = tiles_left + i * (t + gap)
        draw.rounded_rectangle((left, tiles_top, left + t, tiles_top + t),
                               radius=t // 9, fill=TILE)
        painter(draw, left, tiles_top, t)

    name_font = font(FONT_PATHS, name_size * SCALE)
    room = tiles_left - LEFT * SCALE - 40 * SCALE
    tag_size = name_size * SCALE // 3
    tag_font = font(FONT_REGULAR_PATHS, tag_size)
    while draw.textlength(TAGLINE, font=tag_font) > room and tag_size > 10:
        tag_size -= 1
        tag_font = font(FONT_REGULAR_PATHS, tag_size)

    name_box = draw.textbbox((0, 0), NAME, font=name_font)
    tag_box = draw.textbbox((0, 0), TAGLINE, font=tag_font)
    name_h = name_box[3] - name_box[1]
    tag_h = tag_box[3] - tag_box[1]
    spacing = int(name_size * SCALE * 0.22)
    top = (h - (name_h + spacing + tag_h)) // 2
    draw.text((LEFT * SCALE - name_box[0], top - name_box[1]), NAME,
              font=name_font, fill=NAME_COLOUR)
    draw.text((LEFT * SCALE - tag_box[0], top + name_h + spacing - tag_box[1]),
              TAGLINE, font=tag_font, fill=TAGLINE_COLOUR)
    return image.resize((width, height), Image.LANCZOS)


def save(image, path):
    """
    <summary>Write a PNG with no metadata chunks, refusing to overwrite.</summary>
    <param name="image">The finished image.</param>
    <param name="path">Destination file.</param>
    <exception cref="SystemExit">When the file already exists.</exception>
    """
    if os.path.exists(path):
        raise SystemExit("%s exists; move it aside first" % path)
    clean = Image.frombytes("RGB", image.size, image.convert("RGB").tobytes())
    clean.save(path, "PNG", optimize=True)
    print("%s  %dx%d  %d bytes" % (path, image.size[0], image.size[1],
                                   os.path.getsize(path)))


def main():
    """
    <summary>Draw the banner and the social preview into the output folder.</summary>
    """
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        PROJECT, "docs", "screenshots")
    os.makedirs(out, exist_ok=True)
    save(render(1280, 320, 176, 104), os.path.join(out, "banner.png"))
    save(render(1280, 640, 176, 112), os.path.join(out, "social.png"))


if __name__ == "__main__":
    main()
