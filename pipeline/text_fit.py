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
# common install paths on macOS, Linux (server), and Windows (Kim/Nicky's
# PCs). Fall back to PIL's bitmap default ONLY as a last resort —
# bitmaps ignore the `size` arg and render at ~8px, which is the
# "names are tiny on the mockup" bug we hit after the server cutover.
_CAMBRIA_PATHS = [
    # Linux / server (klh user's home — drop new font files here).
    Path.home() / ".fonts" / "Cambria-Font-For-MAC.ttf",
    Path.home() / ".fonts" / "Cambria.ttf",
    Path.home() / ".fonts" / "cambria.ttf",
    Path("/usr/share/fonts/truetype/cambria/Cambria.ttf"),
    Path("/usr/share/fonts/truetype/cambria/cambria.ttf"),
    # macOS user + system installs.
    Path("/Users/petercowgill/Library/Fonts/Cambria-Font-For-MAC.ttf"),
    Path.home() / "Library/Fonts/Cambria.ttf",
    Path.home() / "Library/Fonts/Cambria-Font-For-MAC.ttf",
    Path("/Library/Fonts/Cambria.ttf"),
    # Windows (Kim/Nicky's PCs).
    Path("C:/Windows/Fonts/cambria.ttc"),
    Path("C:/Windows/Fonts/cambria.ttf"),
]


def _find_font_file(family: str) -> Optional[Path]:
    """Return the first existing font file matching `family` we can find."""
    if family.lower() == "cambria":
        for p in _CAMBRIA_PATHS:
            if p.exists():
                return p
    # Search the usual font dirs as a fallback.
    for root in (
        Path.home() / ".fonts",            # Linux user fonts
        Path("/usr/share/fonts"),          # Linux system fonts
        Path.home() / "Library/Fonts",     # macOS user fonts
        Path("/Library/Fonts"),            # macOS system fonts
        Path("/System/Library/Fonts"),     # macOS bundled
        Path("C:/Windows/Fonts"),          # Windows
    ):
        if not root.exists():
            continue
        for p in root.rglob(f"*{family}*.ttf"):
            return p
        for p in root.rglob(f"*{family}*.otf"):
            return p
        for p in root.rglob(f"*{family}*.ttc"):
            return p
    return None


def load_font(family: str, size: int) -> ImageFont.FreeTypeFont:
    """
    Load a TTF/OTF font at the given pixel size. Raises if no match
    is found — `ImageFont.load_default()` returns a bitmap that
    ignores `size` and renders names at ~8px (the post-server-cutover
    'tiny names' bug). Failing loud is the right move: a missing font
    is a deployment problem, not something we should paper over.
    """
    path = _find_font_file(family)
    if path is None:
        raise FileNotFoundError(
            f"font family {family!r} not found in any known location. "
            f"On Linux, place the TTF in ~/.fonts/. On macOS, install "
            f"into ~/Library/Fonts/."
        )
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
    stroke_width: int = 0,
) -> tuple[int, ImageFont.FreeTypeFont]:
    """
    Find the largest integer font size in `size_range` where `text` fits
    in `(box_w - 2*w_pad) x (box_h - 2*h_pad)`. Returns (size, font).

    `stroke_width` is used when measuring so that fake-bold (stroke) text
    is still guaranteed to fit — each glyph grows by ~stroke_width px on
    every side when drawn with a stroke.
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
        # Measure with the same stroke the caller will draw with so our
        # fit calculation accounts for fake-bold growth.
        left, top, right, bottom = draw.textbbox(
            (0, 0), text, font=font, anchor="lt",
            stroke_width=stroke_width,
        )
        w = right - left
        h = bottom - top
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
    bold: bool = False,
) -> None:
    """
    Draw `text` into `bbox` on `img`.

    If `size` is given, use that fixed pixel size.
    If `size_range` is given, auto-fit within the range.
    `align` controls horizontal placement (left/center/right).
    `anchor` controls vertical placement (top/middle/bottom).
    `bold` uses PIL's stroke_width to fake-bold (since Cambria Bold
    isn't installed on the Mac used for authoring mockups).
    """
    x1, y1, x2, y2 = bbox
    box_w = x2 - x1
    box_h = y2 - y1

    # Fake-bold: stroke width scales with font size. A lighter stroke
    # (px/80) gives a semi-bold look that matches Kim's goldens without
    # going into heavy-black weight.
    def _stroke_for(px: int) -> int:
        if not bold:
            return 0
        return max(1, round(px / 80))

    if size_range is not None:
        # First fit without stroke to pick a starting size, then re-fit
        # with the stroke width implied by that size.
        px0, _ = fit_size(text, family, box_w, box_h, tuple(size_range))
        stroke = _stroke_for(px0)
        px, font = fit_size(
            text, family, box_w, box_h, tuple(size_range),
            stroke_width=stroke,
        )
        stroke = _stroke_for(px)
    else:
        assert size is not None, "Either size or size_range must be provided"
        px = int(size)
        font = load_font(family, px)
        stroke = _stroke_for(px)

    draw = ImageDraw.Draw(img)

    # Use PIL's native anchor system for placement. This gives us font-
    # metric-based vertical positioning (so descenders in one name like
    # "Zian Flemming" don't shift the cap top relative to another name
    # like "Seamus Coleman" at the same font size) AND consistent
    # horizontal behaviour with strokes.
    #
    # PIL anchor chars:
    #   horizontal: l=left, m=middle, r=right
    #   vertical:   a=ascender top, m=font middle, d=descender bottom,
    #               s=baseline, t=tight top, b=tight bottom
    h_char = {"left": "l", "right": "r"}.get(align, "m")
    v_char = {"top": "a", "bottom": "d"}.get(anchor, "m")
    pil_anchor = h_char + v_char

    if align == "left":
        ax = x1
    elif align == "right":
        ax = x2
    else:  # center
        ax = (x1 + x2) // 2

    if anchor == "top":
        ay = y1
    elif anchor == "bottom":
        ay = y2
    else:  # middle
        ay = (y1 + y2) // 2

    draw.text(
        (ax, ay), text, font=font, fill=fill,
        anchor=pil_anchor,
        stroke_width=stroke, stroke_fill=fill,
    )
