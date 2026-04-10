"""
Listing presets loader and renderer.

Loads the three files that together define a listing's "shape":

    presets/defaults.yaml            — marketplace, shipping, returns, item
                                       specifics applied to every listing
    presets/products.yaml            — per-product overrides: template_id,
                                       title pattern, default price, size
                                       clause, variants, category lookup
    presets/description_template.html — HTML body with {size_clause}
                                       placeholder (only placeholder we
                                       require)

The pipeline uses this module at listing time to turn a product key + a
few per-listing fields (name, qualifier, orientation, subject, price,
item specifics) into a fully-rendered dict ready to hand to the Trading
API lister (pipeline/lister.py, TBD Phase 6).

Design notes
------------
* Pure data: no network, no disk writes, no Pillow. Just YAML + string
  formatting. Trivially unit-testable.
* Defaults + products are loaded once into a frozen PresetsBundle. The
  CLI / lister passes the bundle around rather than re-reading YAML on
  every listing.
* Rendering is additive: defaults dict → deep-merged with the product's
  entry → deep-merged with any per-listing overrides. We keep the merge
  shallow-per-key for mappings and list-replace for lists; that matches
  how eBay Trading AddFixedPriceItem treats these fields in practice
  (e.g. you replace the ShippingServiceOptions list wholesale rather
  than merging individual entries).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

# Default location: <repo_root>/presets/
PRESETS_DIR = Path(__file__).resolve().parent.parent / "presets"


class PresetsError(RuntimeError):
    """Raised for anything wrong with a presets file or render call."""


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ProductPreset:
    """One entry from products.yaml.

    `template_id` is None for plain-photo products (no Pillow composite —
    the scan IS the image). All four other fields are required.
    """
    key: str
    template_id: Optional[str]
    title_pattern: str
    default_price_gbp: float
    size_clause: str
    raw: dict = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class PresetsBundle:
    defaults: dict
    products: dict[str, ProductPreset]
    description_template: str
    variants: dict
    categories_by_subject: dict
    source_dir: Path = field(default=PRESETS_DIR)

    def product(self, key: str) -> ProductPreset:
        try:
            return self.products[key]
        except KeyError as e:
            raise PresetsError(
                f"Unknown product key {key!r}. "
                f"Known: {sorted(self.products)}"
            ) from e


# --------------------------------------------------------------------------- #
# Loader
# --------------------------------------------------------------------------- #

def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise PresetsError(f"Missing presets file: {path}")
    with open(path) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise PresetsError(f"Expected a mapping at the top level of {path}")
    return data


def load(presets_dir: Path = PRESETS_DIR) -> PresetsBundle:
    """Load defaults.yaml, products.yaml and description_template.html."""
    defaults_path  = presets_dir / "defaults.yaml"
    products_path  = presets_dir / "products.yaml"

    defaults = _read_yaml(defaults_path)
    products_raw = _read_yaml(products_path)

    # description template filename is named inside defaults.yaml, but
    # we only hard-require `{size_clause}`. Fall back to the conventional
    # filename if the defaults block is missing it.
    desc_block = defaults.get("description", {}) or {}
    tmpl_file  = desc_block.get("template_file", "description_template.html")
    tmpl_path  = presets_dir / tmpl_file
    if not tmpl_path.exists():
        raise PresetsError(f"Missing description template: {tmpl_path}")
    description_template = tmpl_path.read_text(encoding="utf-8")

    required_ph = desc_block.get("required_placeholders") or ["size_clause"]
    for ph in required_ph:
        if ("{" + ph + "}") not in description_template:
            raise PresetsError(
                f"description template {tmpl_path.name} is missing required "
                f"placeholder {{{ph}}}"
            )

    # Build product entries.
    products_block = products_raw.get("products") or {}
    if not products_block:
        raise PresetsError(f"{products_path} has no `products:` section")

    products: dict[str, ProductPreset] = {}
    for key, entry in products_block.items():
        if not isinstance(entry, dict):
            raise PresetsError(f"Product {key!r} is not a mapping")
        missing = [
            k for k in ("title_pattern", "default_price_gbp", "size_clause")
            if k not in entry
        ]
        if missing:
            raise PresetsError(
                f"Product {key!r} missing fields: {', '.join(missing)}"
            )
        products[key] = ProductPreset(
            key=key,
            template_id=entry.get("template_id"),
            title_pattern=entry["title_pattern"],
            default_price_gbp=float(entry["default_price_gbp"]),
            size_clause=entry["size_clause"],
            raw=entry,
        )

    return PresetsBundle(
        defaults=defaults,
        products=products,
        description_template=description_template,
        variants=products_raw.get("variants") or {},
        categories_by_subject=products_raw.get("categories_by_subject") or {},
        source_dir=presets_dir,
    )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def render_title(
    bundle: PresetsBundle,
    product_key: str,
    name: str,
    qualifier: Optional[str] = None,
) -> str:
    """
    Apply the product's title pattern.

    title_pattern uses two placeholders:
        {name}              — signer name (required)
        {qualifier_suffix}  — " <qualifier>" or empty string

    Example:
        pattern: "{name} Signed A4 Photo{qualifier_suffix} Autograph + COA"
        name="Tim Allen", qualifier=None
            → "Tim Allen Signed A4 Photo Autograph + COA"
        name="Mel C",      qualifier="Spice Girls"
            → "Mel C Signed A4 Photo Spice Girls Autograph + COA"

    eBay GB titles are capped at 80 chars — we raise if we blow past.
    """
    product = bundle.product(product_key)
    qs = f" {qualifier.strip()}" if qualifier and qualifier.strip() else ""
    title = product.title_pattern.format(
        name=name.strip(),
        qualifier_suffix=qs,
    )
    # eBay caps at 80. Warn loudly — the caller can truncate/rephrase.
    if len(title) > 80:
        raise PresetsError(
            f"Rendered title is {len(title)} chars (>80, eBay max):\n  {title}"
        )
    return title


def render_description(
    bundle: PresetsBundle,
    product_key: str,
    extra_placeholders: Optional[dict[str, str]] = None,
) -> str:
    """
    Substitute {size_clause} (and any extras) into the HTML template.

    We deliberately use `.replace()` rather than `.format()` so that
    all the stray `{` `}` in the HTML / CSS body don't blow up the
    substitution. Only the specific placeholders we know about get
    replaced.
    """
    product = bundle.product(product_key)
    html = bundle.description_template
    html = html.replace("{size_clause}", product.size_clause)
    if extra_placeholders:
        for key, value in extra_placeholders.items():
            html = html.replace("{" + key + "}", value)
    return html


def pick_template_id(
    bundle: PresetsBundle,
    product_key: str,
    *,
    orientation: Optional[str] = None,
    variant: Optional[str] = None,
) -> Optional[str]:
    """
    Resolve the concrete template folder for a product.

    Resolution order (highest to lowest priority):
      1. explicit `variant` argument (e.g. "16x12-c-mount")
      2. `orientation` lookup in bundle.variants[product_key] ("landscape"/"portrait")
      3. the product's default template_id from products.yaml

    Returns None for plain-photo products (no template).
    """
    product = bundle.product(product_key)

    # 1. Explicit variant — validate it's in the allowed list if we know one.
    if variant:
        variants_for_product = bundle.variants.get(product_key, {}) or {}
        allowed = variants_for_product.get("variants")
        if allowed and variant not in allowed:
            raise PresetsError(
                f"Variant {variant!r} not in allowed list for {product_key}: "
                f"{allowed}"
            )
        return variant

    # 2. Orientation-based lookup (10x8 mount/frame have land/port templates).
    if orientation:
        variants_for_product = bundle.variants.get(product_key, {}) or {}
        if orientation in variants_for_product:
            return variants_for_product[orientation]

    # 3. Fall back to the product default.
    return product.template_id


def get_category_id(
    bundle: PresetsBundle,
    subject: str = "default",
) -> int:
    """
    Look up the eBay category ID for a subject key (football_premier,
    music_pop, etc.). Falls back to `default` if unknown.
    """
    cats = bundle.categories_by_subject
    if subject in cats:
        return int(cats[subject])
    if "default" in cats:
        return int(cats["default"])
    raise PresetsError(
        f"No category mapping for subject {subject!r} and no default set"
    )


# --------------------------------------------------------------------------- #
# Merge + full build
# --------------------------------------------------------------------------- #

def _deep_merge(base: dict, override: dict) -> dict:
    """
    Return a new dict: `override` merged into `base`.

    Rules:
      - Nested dicts merged recursively
      - Scalars / lists in `override` replace the same key in `base`
      - Keys only in `base` are preserved unchanged
    """
    out = copy.deepcopy(base)
    for key, value in override.items():
        if (
            key in out
            and isinstance(out[key], dict)
            and isinstance(value, dict)
        ):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def build_listing(
    bundle: PresetsBundle,
    *,
    product_key: str,
    name: str,
    qualifier: Optional[str] = None,
    subject: str = "default",
    orientation: Optional[str] = None,
    variant: Optional[str] = None,
    price_gbp: Optional[float] = None,
    sku: Optional[str] = None,
    item_specifics: Optional[dict[str, str]] = None,
    overrides: Optional[dict] = None,
) -> dict:
    """
    Render a full listing dict for a single item.

    Output shape (stable — this is what the Phase 6 lister will consume):

        {
          "product_key": str,
          "template_id": str | None,   # resolved with orientation/variant
          "title": str,
          "description_html": str,
          "price_gbp": float,
          "sku": str | None,
          "category_id": int,
          "marketplace": {...},        # from defaults
          "listing": {...},            # from defaults
          "shipping": {...},           # from defaults
          "return_policy": {...},      # from defaults
          "item_specifics": {...},     # defaults ∪ caller
        }

    `overrides` is deep-merged into the resulting dict last, so callers
    can patch anything (e.g. bump price, override a shipping service)
    without going through a dedicated kwarg.
    """
    product = bundle.product(product_key)

    title = render_title(bundle, product_key, name, qualifier)
    description_html = render_description(bundle, product_key)
    template_id = pick_template_id(
        bundle,
        product_key,
        orientation=orientation,
        variant=variant,
    )
    category_id = get_category_id(bundle, subject)

    # Start with the defaults, then layer caller-specific stuff on top.
    listing: dict = {
        "product_key": product_key,
        "template_id": template_id,
        "title": title,
        "description_html": description_html,
        "price_gbp": float(price_gbp if price_gbp is not None else product.default_price_gbp),
        "sku": sku,
        "category_id": category_id,
        "marketplace":   copy.deepcopy(bundle.defaults.get("marketplace", {})),
        "listing":       copy.deepcopy(bundle.defaults.get("listing", {})),
        "shipping":      copy.deepcopy(bundle.defaults.get("shipping", {})),
        "return_policy": copy.deepcopy(bundle.defaults.get("return_policy", {})),
        "item_specifics": copy.deepcopy(bundle.defaults.get("item_specifics", {})),
    }

    # Merge in caller-supplied item specifics (e.g. Player, Team, Year Signed).
    if item_specifics:
        listing["item_specifics"].update(item_specifics)

    # Last stop: apply free-form overrides.
    if overrides:
        listing = _deep_merge(listing, overrides)

    return listing


# --------------------------------------------------------------------------- #
# CLI (dev utility — print a rendered listing for eyeballing)
# --------------------------------------------------------------------------- #

def main():
    import argparse
    import json

    parser = argparse.ArgumentParser(
        description="Render a presets listing for debugging"
    )
    parser.add_argument("product_key")
    parser.add_argument("name")
    parser.add_argument("--qualifier", default=None)
    parser.add_argument("--subject", default="default")
    parser.add_argument("--orientation", default=None,
                        choices=[None, "landscape", "portrait"])
    parser.add_argument("--variant", default=None)
    parser.add_argument("--price", type=float, default=None)
    parser.add_argument("--no-description", action="store_true",
                        help="omit description_html from output (it's huge)")
    args = parser.parse_args()

    bundle = load()
    listing = build_listing(
        bundle,
        product_key=args.product_key,
        name=args.name,
        qualifier=args.qualifier,
        subject=args.subject,
        orientation=args.orientation,
        variant=args.variant,
        price_gbp=args.price,
    )
    if args.no_description:
        listing["description_html"] = f"<{len(listing['description_html'])} chars>"
    print(json.dumps(listing, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
