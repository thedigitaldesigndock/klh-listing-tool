"""
Ruler-background compositor for odd-size cards and photos.

Kim signs a lot of memorabilia in non-standard sizes (tickets, index
cards, unusually-cropped photos, autograph slips). Rather than force
every scan into one of the fixed product layouts, we composite the
scan onto a matching "Kim Ruler" background — a printed ruler sheet
that gives eBay buyers an unambiguous sense of scale.

Design
------
* Rulers live in templates/rulers/ as 300 DPI JPEGs. Filenames follow
  "Kim Ruler {W}x{H}.jpg" (landscape) and "Kim Ruler {H}x{W}.jpg"
  (portrait) — i.e. the dimensions in the filename always tell you
  the printed size. A4 is a special case: "Ruler A4.jpg".
* All scans are 300 DPI (Kim's scanner + Peter's files are both
  confirmed 300 DPI). Compositing is therefore just "paste the scan
  at its original pixel size, centered, onto the chosen ruler". No
  resizing of the content — the ruler marks show the true size.
* The picker chooses the smallest ruler whose inner canvas fully
  contains the scan's content size (auto-cropped if a white border is
  detected). Ties break landscape-before-portrait when the scan is
  close to square.

This module is used by two product keys:
    odd_card   → "Odd Size Card"   (dashboard)
    odd_photo  → "Odd Size Photo"  (dashboard)

Both route through the same compositor — the split is purely for
title/description copy (card vs photo wording).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PIL import Image

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
RULERS_DIR = TEMPLATES_DIR / "rulers"

DPI = 300  # All scans + rulers are 300 DPI.


# --------------------------------------------------------------------------- #
# Ruler registry
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Ruler:
    """One ruler background.

    `width_in` / `height_in` are the PRINTED scale of the ruler — i.e.
    the size of the usable area inside the ruler strips (a "5x3 ruler"
    has 5 inches of horizontal tick-marks and 3 inches of vertical).
    The sheet itself is a bit bigger than this to accommodate the
    ruler rails (left + bottom) and header (top).

    The ruler 0,0 tick-mark origin sits at `(origin_left_in,
    sheet_height - origin_bottom_in)` in pixel space. Both offsets
    were measured from Kim's ruler PSDs and are identical across all
    sizes (the rails use the same template).
    """
    name: str               # "Kim Ruler 6x4", "Ruler A4"
    path: Path
    width_in: float
    height_in: float

    # Sheet-edge → 0-tick distances, measured from Kim's ruler files.
    # Same for every ruler because they share the same rail template.
    origin_left_in: float   = 0.61
    origin_bottom_in: float = 0.63

    @property
    def inner_w_in(self) -> float:
        """Usable width for the scan — equals the labelled ruler size."""
        return self.width_in

    @property
    def inner_h_in(self) -> float:
        return self.height_in

    @property
    def is_landscape(self) -> bool:
        return self.width_in >= self.height_in


# Ordered by area, smallest first. The picker scans this list in order
# and returns the first ruler that fits. If nothing fits, it falls back
# to the largest (A4 / 10x8) so we at least produce *some* mockup.
_RULER_SPECS: tuple[tuple[str, float, float], ...] = (
    # filename-stem                printed_w,  printed_h
    ("Kim Ruler 5x3",              5.0,   3.0),
    ("Kim Ruler 3x5",              3.0,   5.0),
    ("Kim Ruler 6x4",              6.0,   4.0),
    ("Kim Ruler 4x6",              4.0,   6.0),
    ("Kim Ruler 7x5",              7.0,   5.0),
    ("Kim Ruler 5x7",              5.0,   7.0),
    ("Kim Ruler 8x6",              8.0,   6.0),
    ("Kim Ruler 6x8",              6.0,   8.0),
    ("Kim Ruler 10x8",            10.0,   8.0),
    ("Kim Ruler 8x10",             8.0,  10.0),
    ("Ruler A4",                   8.27, 11.69),  # portrait A4
)


def _ruler_path(stem: str) -> Path:
    return RULERS_DIR / f"{stem}.jpg"


def load_rulers(rulers_dir: Path = RULERS_DIR) -> list[Ruler]:
    """Return the list of Ruler objects whose JPEG exists on disk.

    Missing files are silently skipped so a partially-deployed install
    still works for the sizes it does have.
    """
    out: list[Ruler] = []
    for stem, w, h in _RULER_SPECS:
        p = rulers_dir / f"{stem}.jpg"
        if p.exists():
            out.append(Ruler(name=stem, path=p, width_in=w, height_in=h))
    return out


# --------------------------------------------------------------------------- #
# Content detection
# --------------------------------------------------------------------------- #

def detect_content_bbox(
    img: Image.Image,
    *,
    white_threshold: int = 245,
    min_border_ratio: float = 0.02,
) -> tuple[int, int, int, int]:
    """Return the bbox of non-white content in `img`.

    If the scan has a real white border (at least `min_border_ratio` of
    the total width on at least one side), the content bbox is
    returned — everything outside it is treated as scanner paper. If
    no meaningful border is detected we return the full image bbox,
    i.e. the scan IS the content.
    """
    gray = img.convert("L")
    # Mask where dark-enough pixels are "content" (True).
    mask = gray.point(lambda p: 255 if p < white_threshold else 0)
    bbox = mask.getbbox()
    if bbox is None:
        return (0, 0, img.width, img.height)

    left, top, right, bottom = bbox
    # How much border did we actually trim?
    w, h = img.width, img.height
    trimmed = max(left, top, w - right, h - bottom)
    if trimmed < min_border_ratio * min(w, h):
        # Barely any border — treat the whole scan as content. Stops
        # us from over-cropping a full-bleed scan that happened to
        # have a slightly-lighter corner.
        return (0, 0, w, h)
    return bbox


def content_size_inches(img: Image.Image, dpi: int = DPI) -> tuple[float, float]:
    """Detect the actual content size of a scan in inches."""
    bbox = detect_content_bbox(img)
    cw = bbox[2] - bbox[0]
    ch = bbox[3] - bbox[1]
    return (cw / dpi, ch / dpi)


# --------------------------------------------------------------------------- #
# Picker
# --------------------------------------------------------------------------- #

def pick_ruler(
    content_w_in: float,
    content_h_in: float,
    rulers: Optional[list[Ruler]] = None,
    *,
    slack_in: float = 0.10,
) -> Optional[Ruler]:
    """Pick the smallest ruler whose inner area fits the content.

    `slack_in` is a small tolerance that lets a 5.05x3.02in card fit
    the 5x3 ruler instead of jumping up to 7x5.

    Orientation-aware: a landscape scan goes on a landscape ruler,
    portrait on portrait. If nothing fits, returns the largest ruler
    matching the scan's orientation, or the outright largest as a
    last resort. Returns None only if the ruler list is empty.
    """
    if rulers is None:
        rulers = load_rulers()
    if not rulers:
        return None

    scan_landscape = content_w_in >= content_h_in

    # Separate rulers by orientation to respect the scan's shape.
    same_orient = [r for r in rulers if r.is_landscape == scan_landscape]
    candidates = same_orient or rulers

    # Sort by area (smallest first) then prefer the tightest fit.
    candidates = sorted(candidates, key=lambda r: r.inner_w_in * r.inner_h_in)

    for r in candidates:
        if (content_w_in <= r.inner_w_in + slack_in
                and content_h_in <= r.inner_h_in + slack_in):
            return r

    # Nothing fit — fall back to the biggest same-orient ruler (or the
    # biggest ruler overall).
    return candidates[-1]


# --------------------------------------------------------------------------- #
# Compositor
# --------------------------------------------------------------------------- #

def composite_on_ruler(
    scan_path: Path,
    ruler: Ruler,
    *,
    dpi: int = DPI,
    output_max_dim: Optional[int] = 2000,
    jpeg_quality: int = 90,
    anchor: str = "bottom-left",
    margin_mm: float = 2.0,
) -> Image.Image:
    """Paste `scan_path` onto `ruler`, returning a PIL Image.

    The scan is auto-cropped to its content bbox first (if a white
    border is detected) and then pasted at its original pixel size —
    NO resizing. Because both the scan and the ruler are at the same
    DPI, the pasted content is shown at true real-world size, which
    is the whole point of the ruler background.

    Placement is anchored to the bottom-left corner of the ruler's
    printed area by default, offset by `margin_mm` (a couple of mm)
    so the signed item sits neatly inside the zero-tick corner
    instead of floating in the middle of the sheet. Pass
    ``anchor="center"`` to restore centered placement.

    If the scan is bigger than the ruler (shouldn't happen if the
    picker did its job), we scale it down proportionally to fit.
    """
    scan = Image.open(scan_path).convert("RGB")
    bbox = detect_content_bbox(scan)
    content = scan.crop(bbox)

    bg = Image.open(ruler.path).convert("RGB")

    # If the content is larger than the ruler (fallback case), scale
    # down to fit inside the inner area.
    inner_w_px = int(ruler.inner_w_in * dpi)
    inner_h_px = int(ruler.inner_h_in * dpi)
    if content.width > inner_w_px or content.height > inner_h_px:
        ratio = min(inner_w_px / content.width, inner_h_px / content.height)
        new_size = (int(content.width * ratio), int(content.height * ratio))
        content = content.resize(new_size, Image.LANCZOS)

    # Compute paste position.
    margin_px = int(round((margin_mm / 25.4) * dpi))
    origin_x = int(round(ruler.origin_left_in * dpi))
    origin_y_from_bottom = int(round(ruler.origin_bottom_in * dpi))

    if anchor == "center":
        cx = (bg.width - content.width) // 2
        cy = (bg.height - content.height) // 2
    else:  # bottom-left — nestle the scan into the ruler's 0,0 corner.
        # Bottom-left of content sits exactly on the 0 tick-mark, plus
        # an optional `margin_mm` offset (so the signature isn't hidden
        # by the tick line itself).
        cx = origin_x + margin_px
        cy = bg.height - origin_y_from_bottom - content.height - margin_px

    bg.paste(content, (cx, cy))

    if output_max_dim:
        bg.thumbnail((output_max_dim, output_max_dim), Image.LANCZOS)

    return bg


def render_odd_size_mockup(
    scan_path: Path,
    *,
    rulers_dir: Path = RULERS_DIR,
    output_max_dim: Optional[int] = 2000,
) -> tuple[Image.Image, Ruler]:
    """High-level entry point: pick a ruler and composite.

    Returns the finished image plus the ruler that was chosen (so the
    caller can log it / include it in the mockup filename).
    """
    scan = Image.open(scan_path)
    w_in, h_in = content_size_inches(scan)
    scan.close()

    rulers = load_rulers(rulers_dir)
    ruler = pick_ruler(w_in, h_in, rulers)
    if ruler is None:
        raise RuntimeError(
            f"No ruler templates found in {rulers_dir} — "
            f"add 'Kim Ruler Nx{'{size}'}.jpg' files."
        )

    img = composite_on_ruler(scan_path, ruler, output_max_dim=output_max_dim)
    return img, ruler


# --------------------------------------------------------------------------- #
# CLI (dev utility)
# --------------------------------------------------------------------------- #

def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="Composite a scan onto the best-fit Kim Ruler background."
    )
    parser.add_argument("scan", type=Path, help="input scan (jpg/png)")
    parser.add_argument("--out", type=Path, required=True, help="output jpg path")
    args = parser.parse_args()

    img, ruler = render_odd_size_mockup(args.scan)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    img.save(args.out, "JPEG", quality=90, optimize=True)
    print(f"✓ {args.scan.name} → {ruler.name} → {args.out} ({img.size[0]}x{img.size[1]})")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
