"""
Text fitting utilities.

Finds the largest font size that makes a string fit inside a target bbox,
and provides a helper to draw centered/middle-anchored text into that bbox.

Used by compositor.py for the "Personally Signed By" (fixed size) and the
variable name label (size_range fit-to-box).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont


# Where to look for system fonts, in priority order.
# Cambria isn't always available under the exact name, so we try several
# common install paths on macOS and fall back to PIL's default.
_CAMBRIA_PATHS = [
    Path("/Users/petercowgill/Library/Fonts/Cambria-Font-For-MAC.ttf"),
    Path.home() / "Library/Fonts/Cambria.ttf",
    Path.home() / "Library/Fonts/Cambria-Font-For-MAC.ttf",
    Path("/Library/Fonts/Cambria.ttf"),
    Path("C:/Windows/Fonts/cambria.ttc"),  # Kim's PC later
]


def _find_font_file(family: str) -> Optional[Path]:
    """Return the first existing font file matching `family` we can find."""
    if family.lower() == "cambria":
        for p in _CAMBRIA_PATHS:
            if p.exists():
                return p
    # Search the usual font dirs as a fallback.
    for root in (
        Path.home() / "Library/Fonts",
        Path("/Library/Fonts"),
        Path("/System/Library/Fonts"),
    ):
        if not root.exists():
            continue
        for p in root.rglob(f"*{family}*.ttf"):
            return p
        for p in root.rglob(f"*{family}*.otf"):
            return p
    return None


def load_font(family: str, size: int) -> ImageFont.FreeTypeFont:
    """Load a TTF/OTF font at the given pixel size."""
    path = _find_font_file(family)
    if path is None:
        # Last resort — use PIL default (bitmap). Mockups will look wrong
        # but the pipeline won't crash during development.
        return ImageFont.load_default()
    return ImageFont.truetype(str(path), size=size)


def _text_extent(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont
) -> tuple[int, int]:
    """Return (width, height) of the rendered text ignoring leading whitespace."""
    # textbbox gives us a tight box around the pixels — much more accurate
    # than getlength/getsize for non-monospaced fonts.
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font, anchor="lt")
    return right - left, bottom - top


def fit_size(
    text: str,
    family: str,
    box_w: int,
    box_h: int,
    size_range: tuple[int, int],
    w_pad: int = 0,
    h_pad: int = 0,
    step: int = 2,
) -> tuple[int, ImageFont.FreeTypeFont]:
    """
    Find the largest integer font size in `size_range` where `text` fits
    in `(box_w - 2*w_pad) x (box_h - 2*h_pad)`. Returns (size, font).
    """
    lo, hi = size_range
    target_w = box_w - 2 * w_pad
    target_h = box_h - 2 * h_pad

    # Scratch canvas for measuring.
    scratch = Image.new("RGB", (max(box_w * 2, 1), max(box_h * 2, 1)))
    draw = ImageDraw.Draw(scratch)

    best = lo
    best_font = load_font(family, lo)
    # Coarse descending scan — start at hi and walk down until it fits.
    size = hi
    while size >= lo:
        font = load_font(family, size)
        w, h = _text_extent(draw, text, font)
        if w <= target_w and h <= target_h:
            best = size
            best_font = font
            break
        size -= step

    return best, best_font


def draw_text_in_box(
    img: Image.Image,
    text: str,
    bbox: tuple[int, int, int, int],
    family: str,
    size: Optional[int] = None,
    size_range: Optional[tuple[int, int]] = None,
    fill: tuple[int, int, int] = (0, 0, 0),
    align: str = "center",
    anchor: str = "middle",
) -> None:
    """
    Draw `text` into `bbox` on `img`.

    If `size` is given, use that fixed pixel size.
    If `size_range` is given, auto-fit within the range.
    `align` controls horizontal placement (left/center/right).
    `anchor` controls vertical placement (top/middle/bottom).
    """
    x1, y1, x2, y2 = bbox
    box_w = x2 - x1
    box_h = y2 - y1

    if size_range is not None:
        px, font = fit_size(text, family, box_w, box_h, tuple(size_range))
    else:
        assert size is not None, "Either size or size_range must be provided"
        font = load_font(family, int(size))

    draw = ImageDraw.Draw(img)

    # Measure the actual pixel extent so we can position precisely.
    left, top, right, bottom = draw.textbbox(
        (0, 0), text, font=font, anchor="lt"
    )
    w = right - left
    h = bottom - top

    if align == "left":
        tx = x1 - left
    elif align == "right":
        tx = x2 - w - left
    else:  # center
        tx = x1 + (box_w - w) // 2 - left

    if anchor == "top":
        ty = y1 - top
    elif anchor == "bottom":
        ty = y2 - h - top
    else:  # middle
        ty = y1 + (box_h - h) // 2 - top

    draw.text((tx, ty), text, font=font, fill=fill)
