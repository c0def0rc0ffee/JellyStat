# -*- coding: utf-8 -*-
"""
<summary>
Trim a Kodi window capture down to the settings dialog itself.
</summary>
<remarks>
The full window shows the skin's home screen behind the dialog, which on a
real box means somebody's library counts and the artwork of whatever was
last played. None of that belongs in a public README, so the dialog is cut
out of the capture rather than published with a blurred backdrop.

The dialog is found rather than hardcoded: it is the bright, flat rectangle
in the middle of a dimmed screen, so scanning in from each edge for the
first bright row and column locates it whatever the screen resolution.
</remarks>
"""

import glob
import os
import sys

from PIL import Image

# Kodi draws a bright accent bar along the top of the dialog. The panel
# itself is nearly as dark as the dimmed backdrop behind it, so that bar is
# the only edge worth looking for; everything else is measured from it.
ACCENT = 90
# The panel is dim but still lifts off the darkened home screen behind it.
PANEL = 23
# The dialog is never a sliver: reject a "find" that is implausibly small.
MIN_FRACTION = 0.35
PAD = 2


def _bounds(image):
    """
    <summary>
    Find the settings dialog in a Kodi window capture by its bright top edge and its panel colour.
    </summary>
    <param name="image">A PIL image.</param>
    <returns>(left, top, right, bottom), or None when no dialog sized panel is found.</returns>
    """
    grey = image.convert("L")
    width, height = grey.size
    pixels = grey.load()

    top = None
    for y in range(height):
        bright = [x for x in range(0, width, 4) if pixels[x, y] > ACCENT]
        if len(bright) > (width / 4) * MIN_FRACTION:
            top = y
            left, right = min(bright), max(bright) + 4
            break
    if top is None:
        return None

    # Walk down the inside of the panel until it gives way to the backdrop.
    probe = left + max(8, (right - left) // 12)
    bottom = top
    for y in range(top + 1, height):
        if pixels[probe, y] >= PANEL:
            bottom = y
    if (right - left) < width * MIN_FRACTION:
        return None
    if (bottom - top) < height * MIN_FRACTION:
        return None
    return (left, top, right, bottom + 1)


def crop(source, target):
    """
    <summary>
    Crop one capture to the dialog with padding, or save it uncropped when the dialog cannot be found.
    </summary>
    <param name="source">Input path.</param>
    <param name="target">Output path.</param>
    <returns>True when it was cropped.</returns>
    """
    image = Image.open(source).convert("RGB")
    box = _bounds(image)
    if not box:
        print("  ! could not find the dialog in %s; left uncropped"
              % os.path.basename(source))
        image.save(target)
        return False
    left, top, right, bottom = box
    box = (max(0, left - PAD), max(0, top - PAD),
           min(image.width, right + PAD), min(image.height, bottom + PAD))
    image.crop(box).save(target)
    print("  %s -> %s %s" % (os.path.basename(source),
                             os.path.basename(target),
                             (box[2] - box[0], box[3] - box[1])))
    return True


def main():
    """
    <summary>
    Crop every kodi-*.png in the source folder into the target folder.
    </summary>
    """
    if len(sys.argv) < 3:
        raise SystemExit("usage: crop_kodi.py <source-dir> <target-dir>")
    source_dir, target_dir = sys.argv[1], sys.argv[2]
    os.makedirs(target_dir, exist_ok=True)
    found = sorted(glob.glob(os.path.join(source_dir, "kodi-*.png")))
    if not found:
        raise SystemExit("no kodi-*.png in %s" % source_dir)
    for path in found:
        crop(path, os.path.join(target_dir, os.path.basename(path)))


if __name__ == "__main__":
    main()
