#!/usr/bin/env python3
"""
Extract a klh-listing-tool template from a Photoshop PSD/PSDT file.

Reads a template PSD and emits:
    templates/<slug>/
        spec.yaml      — canvas, slots, text definitions
        base.png       — flattened mount with PICTURE, CARD, and text layers hidden
        preview.jpg    — small thumbnail (for the future dashboard)
        source.json    — raw extraction metadata (debug)

Handles:
- PICTURE and CARD smart objects (bbox from transform_box)
- Templates with only PICTURE (10x8 Mount/Frame)
- "Text Box 1_Npt" and "Text Box 2_Npt" layer naming across pt variants
- TEXT BOX as shape OR smart object OR group (decorative plate)

Usage:
    python scripts/extract_template.py <psd-path> [<psd-path> ...]
    python scripts/extract_template.py --all
"""

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from psd_tools import PSDImage

from pipeline import config

# Slot-naming heuristics.
PICTURE_NAMES = {"PICTURE", "Picture", "picture", "PIC", "Pic", "pic"}
CARD_NAMES = {"CARD", "Card", "card"}

# Font used across all templates.
FONT_NAME = "Cambria"

# Size ranges for fit-to-box text (pixels at template's native DPI).
# Values calibrated against Kim's A4-A Mount goldens and scaled proportionally
# by the extractor for other templates.
SIGNED_BY_DEFAULT_SIZE = 62       # fixed size for the static label
NAME_SIZE_RANGE = (60, 133)       # min/max pixel size for the variable name


def _slugify(stem: str) -> str:
    return (
        stem.lower()
        .replace(" ", "-")
        .replace("_", "-")
        .replace(".psdt", "")
        .replace(".psd", "")
    )


def _so_bbox(smart_obj_layer) -> Optional[tuple[int, int, int, int]]:
    """
    Extract an axis-aligned bbox from a smart object layer's transform_box.
    transform_box is 4 corner points [x1,y1,x2,y2,x3,y3,x4,y4] in clockwise order.
    """
    tb = smart_obj_layer.smart_object.transform_box
    if not tb or len(tb) < 8:
        return None
    xs = tb[0::2]
    ys = tb[1::2]
    return (int(round(min(xs))), int(round(min(ys))),
            int(round(max(xs))), int(round(max(ys))))


@dataclass
class ExtractedLayer:
    """Intermediate record for a layer we care about."""
    name: str
    kind: str
    bbox: Optional[tuple[int, int, int, int]]
    text: Optional[str] = None
    layer_id: Optional[int] = None


def _scan(psd: PSDImage) -> dict:
    """Walk the PSD and pull out everything we might need."""
    picture_slot: Optional[ExtractedLayer] = None
    card_slot: Optional[ExtractedLayer] = None
    text_box_container: Optional[ExtractedLayer] = None  # the shape/SO background plate
    signed_by_layer: Optional[ExtractedLayer] = None
    name_layer: Optional[ExtractedLayer] = None
    text_group_layer_ids: set[int] = set()

    for layer in psd.descendants():
        if layer.kind == "smartobject":
            if layer.name in PICTURE_NAMES:
                bbox = _so_bbox(layer)
                picture_slot = ExtractedLayer(
                    name=layer.name, kind="smartobject", bbox=bbox,
                    layer_id=layer.layer_id,
                )
            elif layer.name in CARD_NAMES:
                bbox = _so_bbox(layer)
                card_slot = ExtractedLayer(
                    name=layer.name, kind="smartobject", bbox=bbox,
                    layer_id=layer.layer_id,
                )
            elif "TEXT" in layer.name.upper() and "BOX" in layer.name.upper():
                # Smart-object-based text plate (seen in A4-A Frame)
                bbox = _so_bbox(layer)
                text_box_container = ExtractedLayer(
                    name=layer.name, kind="smartobject", bbox=bbox,
                    layer_id=layer.layer_id,
                )

        elif layer.kind == "shape":
            if "TEXT" in layer.name.upper() and "BOX" in layer.name.upper():
                text_box_container = ExtractedLayer(
                    name=layer.name, kind="shape",
                    bbox=tuple(layer.bbox) if layer.bbox else None,
                    layer_id=layer.layer_id,
                )

        elif layer.kind == "type":
            try:
                text = layer.text or ""
            except Exception:
                text = ""
            bbox = tuple(layer.bbox) if layer.bbox else None
            entry = ExtractedLayer(
                name=layer.name, kind="type", bbox=bbox, text=text,
                layer_id=layer.layer_id,
            )
            if "signed by" in text.lower():
                # Always prefer the static-label layer with this text
                signed_by_layer = entry
            elif layer.name.startswith("Text Box 2") or layer.name.startswith("Text Box2"):
                # Name placeholder (current convention)
                name_layer = entry
            elif name_layer is None:
                # Fallback: first non-signed-by type layer
                name_layer = entry

    # NOTE: we deliberately do NOT hide entire "Text Boxes"-named groups —
    # some templates (e.g. 16x12-A Mount) nest the mount artwork ("Layer 2")
    # inside that group, so hiding the group wipes out the mount. We only
    # hide the individual type layers by layer_id in _render_base.

    return {
        "picture": picture_slot,
        "card": card_slot,
        "text_container": text_box_container,
        "signed_by": signed_by_layer,
        "name": name_layer,
        "text_group_ids": text_group_layer_ids,
    }


def _compute_text_regions(scan: dict) -> list[dict]:
    """
    Produce the spec's `text` list from the scan data.

    Strategy:
    - Use the TEXT BOX container (shape/SO) as the outer bounds if present
    - For signed_by: use current text layer's Y range, container X range
    - For name: same but with a size_range and no fixed pixel size
    """
    entries: list[dict] = []
    container = scan.get("text_container")
    signed_by = scan.get("signed_by")
    name_layer = scan.get("name")

    if not signed_by and not name_layer:
        return entries  # no text on this template (e.g. 10x8 Mount)

    # X bounds — prefer container, otherwise use the text layer's own X
    if container and container.bbox:
        x1, _, x2, _ = container.bbox
        # Inset slightly so text doesn't touch the plate edge
        pad = max(8, (x2 - x1) // 40)
        x_left = x1 + pad
        x_right = x2 - pad
    else:
        x_left = x_right = None  # will fall back per-layer

    if signed_by and signed_by.bbox:
        sx1, sy1, sx2, sy2 = signed_by.bbox
        entries.append({
            "id": "signed_by",
            "content": "Personally Signed By",
            "bbox": [
                x_left if x_left is not None else sx1,
                sy1,
                x_right if x_right is not None else sx2,
                sy2,
            ],
            "font": FONT_NAME,
            "size": SIGNED_BY_DEFAULT_SIZE if container is None
                    else max(24, (sy2 - sy1)),  # rough: use current height
            "align": "center",
            "anchor": "middle",
        })

    if name_layer and name_layer.bbox:
        nx1, ny1, nx2, ny2 = name_layer.bbox
        # The PSD's tight bbox on the "Text Box 2" layer is much narrower
        # than Kim's actual x-budget (measured in the goldens). Widen it
        # symmetrically around the text layer centre to match a4-a-mount's
        # 1200px budget (ratio-scaled for other templates).
        if container and container.bbox:
            cx1, _, cx2, _ = container.bbox
            cont_w = cx2 - cx1
            # On A4-A Mount, Kim's name x-budget is ~1200 out of container
            # width ~1477 ≈ 0.812. Apply same ratio on other templates.
            widen = int(round(cont_w * 0.812))
            mid_x = (nx1 + nx2) // 2
            nx1 = mid_x - widen // 2
            nx2 = mid_x + widen // 2
        entries.append({
            "id": "name",
            "content": "{name}",
            "bbox": [nx1, ny1, nx2, ny2],
            "font": FONT_NAME,
            "size_range": list(NAME_SIZE_RANGE),
            "align": "center",
            "anchor": "middle",
            "bold": True,
        })

    return entries


def _render_base(psd: PSDImage, scan: dict) -> "PIL.Image.Image":
    """
    Render the flattened base: mount with PICTURE, CARD, and text layers hidden.
    The TEXT BOX decorative plate stays visible (it's part of the mount).
    """
    # Collect layer_ids to hide
    hide_ids: set[int] = set()
    for key in ("picture", "card"):
        if scan[key] and scan[key].layer_id is not None:
            hide_ids.add(scan[key].layer_id)
    for key in ("signed_by", "name"):
        if scan[key] and scan[key].layer_id is not None:
            hide_ids.add(scan[key].layer_id)
    hide_ids |= scan.get("text_group_ids", set())

    # Save original visibility and toggle
    saved: dict[int, bool] = {}
    for layer in psd.descendants():
        saved[layer.layer_id] = layer.visible
        if layer.layer_id in hide_ids:
            layer.visible = False

    try:
        img = psd.composite()
    finally:
        for layer in psd.descendants():
            layer.visible = saved[layer.layer_id]

    return img


def _identify_overlay_layers(psd: PSDImage, scan: dict) -> list[int]:
    """
    Identify the "mount top" layers — raster layers that sit ABOVE the
    PICTURE / CARD smart objects in the PSD z-order. These are the mount
    edges / aperture cut-outs that should draw on TOP of our pasted
    picture and card in the final composite.

    Approach: walk psd.descendants() which yields layers bottom-to-top.
    Any raster ('pixel') layer whose index is greater than the PICTURE /
    CARD smart object layers is part of the overlay.

    Text (type), shape, and smartobject layers are deliberately excluded
    — the text is rendered by our own text_fit pass, the PICTURE/CARD
    smart objects are empty placeholders, and the TEXT BOX shape is part
    of the mount front which we DO want on the overlay (it sits above
    picture/card so the decorative plate overlays any card overflow).
    """
    # Find the highest z-order index of PICTURE / CARD.
    slot_ids = {
        scan[k].layer_id
        for k in ("picture", "card")
        if scan[k] and scan[k].layer_id is not None
    }
    if not slot_ids:
        return []

    all_layers = list(psd.descendants())
    # z_index: position in iteration order (bottom-to-top).
    max_slot_z = -1
    for i, layer in enumerate(all_layers):
        if layer.layer_id in slot_ids:
            max_slot_z = max(max_slot_z, i)
    if max_slot_z < 0:
        return []

    # Layers to include in the overlay = everything strictly above the
    # top slot layer, excluding text and the decorative TEXT BOX plate
    # (we render those ourselves via text_fit).
    hide_text_ids: set[int] = set()
    for key in ("signed_by", "name"):
        if scan[key] and scan[key].layer_id is not None:
            hide_text_ids.add(scan[key].layer_id)

    overlay_ids: list[int] = []
    for i, layer in enumerate(all_layers):
        if i <= max_slot_z:
            continue
        if layer.layer_id in hide_text_ids:
            continue
        if layer.kind == "type":
            continue
        # Group layers have no raster content of their own; their
        # children are already in the descendants() list.
        if layer.kind == "group":
            continue
        overlay_ids.append(layer.layer_id)
    return overlay_ids


def _render_overlay(psd: PSDImage, scan: dict) -> Optional["PIL.Image.Image"]:
    """
    Render an RGBA overlay image containing only the mount layers that
    sit above PICTURE/CARD in the PSD. Areas where the mount has its
    aperture cut-out will be transparent so the underlying pasted
    picture / card can show through when the compositor alpha-composites
    the overlay on top.

    Returns None if there are no overlay layers (e.g. templates where
    the mount sits entirely below the picture).
    """
    overlay_ids = _identify_overlay_layers(psd, scan)
    if not overlay_ids:
        return None

    keep = set(overlay_ids)
    saved: dict[int, bool] = {}
    for layer in psd.descendants():
        saved[layer.layer_id] = layer.visible
        layer.visible = layer.layer_id in keep

    try:
        img = psd.composite()
    finally:
        for layer in psd.descendants():
            layer.visible = saved[layer.layer_id]

    if img is None:
        return None
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    return img


def extract(psd_path: Path, out_root: Path) -> Path:
    """Extract one template into `<out_root>/<slug>/`. Returns the slug dir."""
    psd = PSDImage.open(psd_path)
    slug = _slugify(psd_path.stem)
    slug_dir = out_root / slug
    slug_dir.mkdir(parents=True, exist_ok=True)

    scan = _scan(psd)

    # Build spec
    slots: dict = {}
    if scan["picture"] and scan["picture"].bbox:
        slots["picture"] = {
            "bbox": list(scan["picture"].bbox),
            "scale_mode": "fit_width_center",
            "background": "#000000",
        }
    if scan["card"] and scan["card"].bbox:
        slots["card"] = {
            "bbox": list(scan["card"].bbox),
            "scale_mode": "fit_cover",
            "background": None,
        }

    text_entries = _compute_text_regions(scan)

    spec = {
        "id": slug,
        "name": psd_path.stem,
        "source_psd": psd_path.name,
        "canvas": [psd.width, psd.height],
        "slots": slots,
        "text": text_entries,
        "output": {
            "format": "jpg",
            "quality": 92,
            # Keep full canvas for now; matches current JSX behaviour.
            "max_dimension": None,
        },
    }

    spec_path = slug_dir / "spec.yaml"
    with open(spec_path, "w") as f:
        yaml.safe_dump(spec, f, sort_keys=False, default_flow_style=False)

    # Render base (mount with PICTURE/CARD/text layers hidden — this is
    # the background everything gets pasted onto).
    base = _render_base(psd, scan)
    base_path = slug_dir / "base.png"
    base.save(base_path)

    # Render overlay (mount layers that sit ABOVE picture/card in z-order;
    # these get alpha-composited OVER the pasted picture+card so the mount
    # edges tuck over the picture edges, as in Photoshop). Optional.
    overlay = _render_overlay(psd, scan)
    overlay_path = slug_dir / "overlay.png"
    if overlay is not None:
        overlay.save(overlay_path)
    elif overlay_path.exists():
        overlay_path.unlink()

    # Tiny preview
    preview = base.copy()
    preview.thumbnail((400, 400))
    preview_path = slug_dir / "preview.jpg"
    preview.convert("RGB").save(preview_path, quality=85)

    # Debug: dump raw scan
    debug_path = slug_dir / "source.json"
    with open(debug_path, "w") as f:
        json.dump(
            {
                "source_psd": str(psd_path),
                "canvas": [psd.width, psd.height],
                "picture_bbox": scan["picture"].bbox if scan["picture"] else None,
                "card_bbox": scan["card"].bbox if scan["card"] else None,
                "text_container": {
                    "name": scan["text_container"].name,
                    "kind": scan["text_container"].kind,
                    "bbox": scan["text_container"].bbox,
                } if scan["text_container"] else None,
                "signed_by": {
                    "bbox": scan["signed_by"].bbox,
                    "text": scan["signed_by"].text,
                } if scan["signed_by"] else None,
                "name_layer": {
                    "bbox": scan["name"].bbox,
                    "text": scan["name"].text,
                } if scan["name"] else None,
            },
            f,
            indent=2,
        )

    return slug_dir


def extract_many(psd_paths: list[Path], out_root: Path) -> list[tuple[Path, Path]]:
    results = []
    for p in psd_paths:
        try:
            slug_dir = extract(p, out_root)
            results.append((p, slug_dir))
            print(f"  ✓ {p.name} → {slug_dir.name}")
        except Exception as e:
            print(f"  ✗ {p.name} — {e}")
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path,
                        help="PSD files to extract (or none with --all)")
    parser.add_argument("--all", action="store_true",
                        help="extract every PSD/PSDT in products_dir")
    parser.add_argument("--out", type=Path, default=None,
                        help="output directory (default: <repo>/templates)")
    args = parser.parse_args()

    if args.all:
        cfg = config.load()
        products = cfg.paths.products_dir
        psd_paths = sorted(
            list(products.glob("*.psd")) + list(products.glob("*.psdt"))
        )
    else:
        psd_paths = args.paths
        if not psd_paths:
            parser.error("provide PSD paths or --all")

    if args.out is None:
        # templates/ alongside this script's parent
        out_root = Path(__file__).resolve().parent.parent / "templates"
    else:
        out_root = args.out
    out_root.mkdir(parents=True, exist_ok=True)

    print(f"Extracting {len(psd_paths)} template(s) → {out_root}")
    extract_many(psd_paths, out_root)


if __name__ == "__main__":
    main()
