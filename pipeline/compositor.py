"""
Mockup compositor.

Takes a template spec (yaml) + base.png + a picture file + optional card
file + a display name, and produces a composited JPEG mockup.

This is Phase 3 of klh-listing-tool — the replacement for Kim's legacy
Photoshop JSX scripts. Everything is driven by the template spec so we
don't need one script per product type.

CLI:
    klh mockup --template a4-a-mount \\
               --picture ~/Desktop/Mounts/Picture/"Seamus Coleman.jpg" \\
               --card    ~/Desktop/Mounts/Card/"Seamus Coleman.jpg" \\
               --name    "Seamus Coleman" \\
               --out     /tmp/seamus.jpg
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml
from PIL import Image

from pipeline import config
from pipeline.text_fit import draw_text_in_box


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


# --------------------------------------------------------------------------- #
# Spec loading
# --------------------------------------------------------------------------- #

@dataclass
class Slot:
    bbox: tuple[int, int, int, int]
    scale_mode: str
    background: Optional[str]


@dataclass
class TextEntry:
    id: str
    content: str
    bbox: tuple[int, int, int, int]
    font: str
    align: str
    anchor: str
    size: Optional[int] = None
    size_range: Optional[tuple[int, int]] = None
    bold: bool = False


@dataclass
class TemplateSpec:
    id: str
    name: str
    canvas: tuple[int, int]
    slots: dict[str, Slot]
    text: list[TextEntry]
    output_format: str = "jpg"
    output_quality: int = 92
    output_max_dim: Optional[int] = None
    base_png: Optional[Path] = None
    overlay_png: Optional[Path] = None


def load_spec(template_id: str, templates_dir: Path = TEMPLATES_DIR) -> TemplateSpec:
    """Load and validate a template spec from `templates/<id>/spec.yaml`."""
    spec_path = templates_dir / template_id / "spec.yaml"
    if not spec_path.exists():
        raise FileNotFoundError(
            f"Template spec not found: {spec_path}\n"
            f"Run `python scripts/extract_template.py <psd>` to create it."
        )

    with open(spec_path) as f:
        raw = yaml.safe_load(f)

    slots = {}
    for key, val in (raw.get("slots") or {}).items():
        slots[key] = Slot(
            bbox=tuple(val["bbox"]),
            scale_mode=val.get("scale_mode", "fit_cover"),
            background=val.get("background"),
        )

    text_entries: list[TextEntry] = []
    for entry in raw.get("text") or []:
        text_entries.append(TextEntry(
            id=entry["id"],
            content=entry["content"],
            bbox=tuple(entry["bbox"]),
            font=entry.get("font", "Cambria"),
            align=entry.get("align", "center"),
            anchor=entry.get("anchor", "middle"),
            size=entry.get("size"),
            size_range=tuple(entry["size_range"]) if entry.get("size_range") else None,
            bold=bool(entry.get("bold", False)),
        ))

    out = raw.get("output") or {}
    template_dir = spec_path.parent
    base_png = template_dir / "base.png"
    overlay_png = template_dir / "overlay.png"

    return TemplateSpec(
        id=raw["id"],
        name=raw.get("name", raw["id"]),
        canvas=tuple(raw["canvas"]),
        slots=slots,
        text=text_entries,
        output_format=out.get("format", "jpg"),
        output_quality=int(out.get("quality", 92)),
        output_max_dim=out.get("max_dimension"),
        base_png=base_png if base_png.exists() else None,
        overlay_png=overlay_png if overlay_png.exists() else None,
    )


# --------------------------------------------------------------------------- #
# Image placement helpers
# --------------------------------------------------------------------------- #

def _fit_cover(src: Image.Image, box_w: int, box_h: int) -> Image.Image:
    """Scale + center-crop `src` to exactly fill (box_w, box_h)."""
    src_ratio = src.width / src.height
    dst_ratio = box_w / box_h
    if src_ratio > dst_ratio:
        # Source is wider than box — fit height, crop sides.
        new_h = box_h
        new_w = int(round(src.width * (box_h / src.height)))
    else:
        new_w = box_w
        new_h = int(round(src.height * (box_w / src.width)))
    resized = src.resize((new_w, new_h), Image.LANCZOS)
    # Center-crop
    left = (new_w - box_w) // 2
    top = (new_h - box_h) // 2
    return resized.crop((left, top, left + box_w, top + box_h))


def _fit_width_center(src: Image.Image, box_w: int, box_h: int) -> Image.Image:
    """
    Scale `src` so its width matches `box_w`, keeping aspect ratio.
    If the resulting height is taller than `box_h`, crop vertically centered.
    If shorter, return the unpadded image (caller handles the background fill).
    """
    scale = box_w / src.width
    new_w = box_w
    new_h = int(round(src.height * scale))
    resized = src.resize((new_w, new_h), Image.LANCZOS)
    if new_h > box_h:
        # Crop vertically centered so the middle of the photo stays in view.
        top = (new_h - box_h) // 2
        return resized.crop((0, top, box_w, top + box_h))
    return resized


def _paste_slot(
    canvas: Image.Image, slot: Slot, src_path: Path
) -> None:
    """Place a source image into a slot on the canvas."""
    x1, y1, x2, y2 = slot.bbox
    box_w = x2 - x1
    box_h = y2 - y1

    # Fill background first (safety net for cropped scans, etc.)
    if slot.background:
        bg = Image.new("RGB", (box_w, box_h), slot.background)
        canvas.paste(bg, (x1, y1))

    src = Image.open(src_path).convert("RGB")

    if slot.scale_mode == "fit_cover":
        placed = _fit_cover(src, box_w, box_h)
        canvas.paste(placed, (x1, y1))
    elif slot.scale_mode == "fit_width_center":
        placed = _fit_width_center(src, box_w, box_h)
        # Center the placed image within the slot (it may be shorter than box_h)
        px = x1 + (box_w - placed.width) // 2
        py = y1 + (box_h - placed.height) // 2
        canvas.paste(placed, (px, py))
    else:
        raise ValueError(f"Unknown scale_mode: {slot.scale_mode}")


# --------------------------------------------------------------------------- #
# Compositor entry point
# --------------------------------------------------------------------------- #

def _display_name(raw_name: str) -> str:
    """
    Convert a filename stem into the display name shown on the mockup.
    Strips anything after the first underscore (the future qualifier).
    """
    return raw_name.split("_", 1)[0].strip()


def composite(
    spec: TemplateSpec,
    picture_path: Optional[Path],
    card_path: Optional[Path],
    name: str,
    secondary_path: Optional[Path] = None,
) -> Image.Image:
    """Run the full composite and return a PIL Image."""
    if spec.base_png is None:
        raise FileNotFoundError(
            f"Template {spec.id} has no base.png — re-run the extractor."
        )

    canvas = Image.open(spec.base_png).convert("RGB")
    # Sanity check — the base should match the declared canvas size.
    if canvas.size != tuple(spec.canvas):
        canvas = canvas.resize(tuple(spec.canvas), Image.LANCZOS)

    if "picture" in spec.slots and picture_path:
        _paste_slot(canvas, spec.slots["picture"], picture_path)
    if "card" in spec.slots and card_path:
        _paste_slot(canvas, spec.slots["card"], card_path)
    if "secondary" in spec.slots and secondary_path:
        _paste_slot(canvas, spec.slots["secondary"], secondary_path)

    # Optional overlay (mount border etc. that sits above picture/card)
    if spec.overlay_png is not None:
        overlay = Image.open(spec.overlay_png).convert("RGBA")
        canvas_rgba = canvas.convert("RGBA")
        canvas_rgba.alpha_composite(overlay)
        canvas = canvas_rgba.convert("RGB")

    display = _display_name(name)
    for entry in spec.text:
        content = entry.content.replace("{name}", display)
        draw_text_in_box(
            canvas,
            content,
            entry.bbox,
            family=entry.font,
            size=entry.size,
            size_range=entry.size_range,
            fill=(0, 0, 0),
            align=entry.align,
            anchor=entry.anchor,
            bold=entry.bold,
        )

    if spec.output_max_dim:
        canvas.thumbnail((spec.output_max_dim, spec.output_max_dim), Image.LANCZOS)

    return canvas


def save_mockup(img: Image.Image, out_path: Path, spec: TemplateSpec) -> None:
    """Save according to spec.output settings."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = spec.output_format.lower()
    if fmt in ("jpg", "jpeg"):
        img.save(out_path, "JPEG", quality=spec.output_quality, optimize=True)
    else:
        img.save(out_path)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="klh mockup",
        description="Render a mockup from a template + picture + card + name",
    )
    parser.add_argument("--template", required=True, help="template id (slug)")
    parser.add_argument("--picture", type=Path, help="picture source path")
    parser.add_argument("--card", type=Path, help="card source path")
    parser.add_argument("--secondary", type=Path, help="secondary picture path (A4-B etc.)")
    parser.add_argument("--name", help="display name (defaults to picture stem)")
    parser.add_argument("--out", type=Path, required=True, help="output file path")
    args = parser.parse_args(argv)

    spec = load_spec(args.template)

    name = args.name
    if not name and args.picture:
        name = args.picture.stem
    if not name:
        name = ""

    img = composite(spec, args.picture, args.card, name, secondary_path=args.secondary)
    save_mockup(img, args.out, spec)
    print(f"✓ wrote {args.out}  ({img.size[0]}x{img.size[1]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
