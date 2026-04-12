"""
Catalog view-model for the dashboard UI.

Turns the 22 products in presets/products.yaml into a JSON structure
the frontend can render directly:

    {
      "tiles": [                     # dashboard_order, one entry per tile
        {
          "layout": "a4_a",
          "has_toggle": true,        # mount/frame swap
          "mount": { product_key, button_label, default_price_gbp, ... },
          "frame": { product_key, button_label, default_price_gbp, ... }
        },
        ...
        {
          "layout": "photo_6x4",
          "has_toggle": false,       # photo-only, no frame twin
          "mount": { ... },          # the only product for this tile
          "frame": null
        }
      ],
      "products": { key: full_view, ... },   # flat index for the frontend
      "layout_twins": { layout: {mount, frame}, ... }
    }

The frontend keeps a global mount/frame toggle state and uses it to
pick between tile.mount and tile.frame for each toggleable tile.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pipeline import presets as pp


# Location of the template folders (…/templates/<template_id>/preview.jpg).
# Computed once at import time so every catalog build is a cheap dict lookup.
_REPO_ROOT     = Path(__file__).resolve().parent.parent
_TEMPLATES_DIR = _REPO_ROOT / "templates"


_STATIC_DIR = Path(__file__).resolve().parent / "static"


def _preview_url_for(template_id: Optional[str], product_key: str = "") -> Optional[str]:
    """
    Return the dashboard URL that serves a template's preview.jpg, or
    a static placeholder image for photo-only / card-only products, or
    None if no preview is available.

    Resolution order:
      1. templates/<template_id>/preview.jpg   (direct hit)
      2. templates/<template_id>-land/preview.jpg  (orientation fallback)
      3. static/placeholders/<product_key>.png  (photo/card placeholder)
    """
    if template_id:
        # Direct hit — preferred.
        if (_TEMPLATES_DIR / template_id / "preview.jpg").exists():
            return f"/api/template-preview/{template_id}"

        # Orientation fallback (10x8-mount → 10x8-mount-land).
        land_id = f"{template_id}-land"
        if (_TEMPLATES_DIR / land_id / "preview.jpg").exists():
            return f"/api/template-preview/{land_id}"

    # Static placeholder for photo-only / card-only products.
    if product_key:
        ph = _STATIC_DIR / "placeholders" / f"{product_key}.png"
        if ph.exists():
            return f"/static/placeholders/{product_key}.png"

    return None


def _product_view(prod: pp.ProductPreset) -> dict:
    """Serialize a single ProductPreset to the shape the frontend wants."""
    raw = prod.raw
    return {
        "product_key":        prod.key,
        "button_label":       raw.get("button_label", prod.key),
        "layout":             raw.get("layout"),
        "frame":              bool(raw.get("frame", False)),
        "template_id":        prod.template_id,
        "preview_url":        _preview_url_for(prod.template_id, prod.key),
        "main_size":          raw.get("main_size"),
        "needs_secondary":    raw.get("needs_secondary"),
        "orientation_lock":   raw.get("orientation_lock"),
        "default_price_gbp":  prod.default_price_gbp,
        "suggested_prices":   list(raw.get("suggested_prices") or []),
        "title_pattern":      prod.title_pattern,
        "size_clause":        prod.size_clause,
    }


def _group_by_layout(bundle: pp.PresetsBundle) -> dict[str, dict]:
    """
    Group every product under its `layout`. Each layout entry carries
    optional `mount` and `frame` product keys; photo-only tiles have
    `mount` set to the one product key and `frame` = None.
    """
    twins: dict[str, dict] = {}
    for key, prod in bundle.products.items():
        layout = prod.raw.get("layout")
        if layout is None:
            # Product without a layout tag is a config bug — skip with a
            # warning rather than crash the whole dashboard.
            continue
        slot = "frame" if prod.raw.get("frame") else "mount"
        twins.setdefault(layout, {"mount": None, "frame": None})
        twins[layout][slot] = key
    return twins


def _tile_for(
    layout: str,
    twins: dict[str, dict],
    products_view: dict[str, dict],
) -> Optional[dict]:
    """Build one tile entry for the given layout key."""
    pair = twins.get(layout)
    if not pair:
        return None

    mount_key = pair.get("mount")
    frame_key = pair.get("frame")

    # Photo-only layouts have only a mount entry; mount/frame layouts
    # have both. We consider has_toggle = True iff BOTH slots filled.
    has_toggle = bool(mount_key and frame_key)

    return {
        "layout":     layout,
        "has_toggle": has_toggle,
        "mount":      products_view[mount_key] if mount_key else None,
        "frame":      products_view[frame_key] if frame_key else None,
    }


def _parse_dashboard_order(raw_order: list) -> list[dict]:
    """
    Parse dashboard_order into a normalised list of groups.

    Supports two formats:
      - Old flat list:  ["a4_a", "photo_6x4", ...]
        → single unnamed group containing all layouts
      - New grouped list:
        [{"label": "16x12", "layouts": ["16x12_a", ...]}, ...]
        → one group per entry
    """
    if not raw_order:
        return []

    # Detect format by first element type.
    if isinstance(raw_order[0], str):
        # Legacy flat list — wrap in one unnamed group.
        return [{"label": None, "layouts": raw_order}]

    groups: list[dict] = []
    for entry in raw_order:
        if isinstance(entry, dict):
            groups.append({
                "label": entry.get("label"),
                "layouts": entry.get("layouts") or [],
            })
        elif isinstance(entry, str):
            # Mixed format — treat bare strings as an unnamed group.
            groups.append({"label": None, "layouts": [entry]})
    return groups


def build_catalog(bundle: pp.PresetsBundle) -> dict:
    """
    Build the full view model for /api/products.

    Called once per dashboard request — the bundle itself is cached at
    FastAPI startup so this is pure in-memory work and runs in <1ms.
    """
    products_view = {
        key: _product_view(prod) for key, prod in bundle.products.items()
    }
    twins = _group_by_layout(bundle)

    # Dashboard order is authoritative; anything not listed is hidden.
    groups = _parse_dashboard_order(bundle.dashboard_order)

    tile_groups: list[dict] = []
    all_tiles: list[dict] = []
    seen_layouts: set[str] = set()

    for group in groups:
        group_tiles: list[dict] = []
        for layout in group["layouts"]:
            tile = _tile_for(layout, twins, products_view)
            if tile is not None:
                group_tiles.append(tile)
                all_tiles.append(tile)
                seen_layouts.add(layout)
        if group_tiles:
            tile_groups.append({
                "label": group["label"],
                "tiles": group_tiles,
            })

    # Any layouts that exist in products.yaml but aren't in
    # dashboard_order are considered hidden — log them but don't add.
    orphan_layouts = sorted(set(twins) - seen_layouts)

    return {
        "tile_groups":     tile_groups,
        "tiles":           all_tiles,       # flat list for backward compat
        "products":        products_view,
        "layout_twins":    twins,
        "orphan_layouts":  orphan_layouts,   # frontend can warn about these
        "total_tiles":     len(all_tiles),
        "total_products":  len(products_view),
    }
