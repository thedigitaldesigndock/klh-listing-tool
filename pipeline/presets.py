"""
Listing presets loader and renderer.

Loads the four files that together define a listing's "shape":

    presets/defaults.yaml            — marketplace, shipping, returns, item
                                       specifics applied to every listing
    presets/products.yaml            — per-product overrides: template_id,
                                       title pattern, default price, size
                                       clause, variants, category lookup
    presets/knowledge.yaml           — per-category title rules, field1
                                       labels, keyword packs, club short→
                                       full aliases (fed to title rule +
                                       item-specifics enrichment)
    presets/description_template.html — HTML body with {size_clause}
                                       placeholder (only placeholder we
                                       require)

The pipeline uses this module at listing time to turn a product key + a
parsed filename (name + field1 + category) into a fully-rendered dict
ready to hand to the Trading API lister (pipeline/lister.py).

Design notes
------------
* Pure data: no network, no disk writes, no Pillow. Just YAML + string
  formatting + a lookup against the offers table. Trivially unit-testable.
* Defaults + products + knowledge are loaded once into a frozen
  PresetsBundle. The CLI / lister passes the bundle around rather than
  re-reading YAML on every listing.
* Rendering is additive, four layers deep:
    1. defaults.item_specifics                 (global floor)
    2. knowledge-derived specifics             (per-category enrichment)
    3. products.yaml `item_specifics` block    (per-product, optional)
    4. caller-supplied `item_specifics` kwarg  (wins)
  Then a free-form `overrides` deep-merge is the last stop. We keep the
  merge shallow-per-key for mappings and list-replace for lists; that
  matches how eBay Trading AddFixedPriceItem treats these fields in
  practice (e.g. you replace the ShippingServiceOptions list wholesale
  rather than merging individual entries).
* Best Offer acceptance/decline thresholds are pulled from
  pipeline/offers.py at build time and attached to the listing dict as
  a `best_offer` block. The lister turns that into XML.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from pipeline import offers
from pipeline.filename import ParsedFilename

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
    knowledge: dict = field(default_factory=dict)
    dashboard_order: list = field(default_factory=list)
    source_dir: Path = field(default=PRESETS_DIR)

    def product(self, key: str) -> ProductPreset:
        try:
            return self.products[key]
        except KeyError as e:
            raise PresetsError(
                f"Unknown product key {key!r}. "
                f"Known: {sorted(self.products)}"
            ) from e

    # ----- knowledge lookups --------------------------------------------- #

    def category_rule(self, category: Optional[str]) -> dict:
        """
        Return the knowledge.yaml entry for a category, or an empty
        dict if category is None / unknown. Fails open so a typo in
        Nicky's filename doesn't break the lister — you just lose the
        per-category title/specific enrichment for that one listing.
        """
        if not category:
            return {}
        cats = self.knowledge.get("categories") or {}
        return cats.get(category) or {}

    def expand_club(self, short_name: Optional[str]) -> Optional[str]:
        """
        Look up a short club/team name in knowledge.yaml `clubs:` and
        return the expanded form, or None if there's no alias. A None
        input returns None.
        """
        if not short_name:
            return None
        clubs = self.knowledge.get("clubs") or {}
        return clubs.get(short_name)

    def shrink_club(self, long_name: Optional[str]) -> Optional[str]:
        """
        Reverse of expand_club — if `long_name` matches a known full
        club name in knowledge.yaml `clubs:`, return the short form;
        otherwise None.

        Used at title-build time to silently substitute "Man Utd" when
        a filename arrives with "Manchester United" in it, saving title
        characters. The long form still goes into the "Club (Full Name)"
        IS so buyers searching the formal name still match.

        Case-insensitive match; whitespace-normalised; matches any entry
        whose value (long form) equals the input.
        """
        if not long_name:
            return None
        needle = " ".join(long_name.split()).lower()
        clubs = self.knowledge.get("clubs") or {}
        for short, full in clubs.items():
            if " ".join((full or "").split()).lower() == needle:
                return short
        return None


# --------------------------------------------------------------------------- #
# Loader
# --------------------------------------------------------------------------- #

def _read_yaml(path: Path) -> dict:
    # Force UTF-8 on read — on Windows, open() defaults to the system
    # code page (cp1252), which chokes on £, em-dashes, curly quotes,
    # etc. in the product YAMLs. Our files are UTF-8 on disk.
    if not path.exists():
        raise PresetsError(f"Missing presets file: {path}")
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise PresetsError(f"Expected a mapping at the top level of {path}")
    return data


def _read_yaml_optional(path: Path) -> dict:
    """Same as _read_yaml but returns {} if the file is missing."""
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise PresetsError(f"Expected a mapping at the top level of {path}")
    return data


def load(presets_dir: Path = PRESETS_DIR) -> PresetsBundle:
    """Load defaults.yaml, products.yaml, knowledge.yaml and the
    description template."""
    defaults_path  = presets_dir / "defaults.yaml"
    products_path  = presets_dir / "products.yaml"
    knowledge_path = presets_dir / "knowledge.yaml"

    defaults = _read_yaml(defaults_path)
    products_raw = _read_yaml(products_path)
    knowledge = _read_yaml_optional(knowledge_path)

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
        knowledge=knowledge,
        dashboard_order=products_raw.get("dashboard_order") or [],
        source_dir=presets_dir,
    )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

MAX_TITLE_LEN = 80  # eBay GB hard cap

# Kim's "Signed White Cards" store category. All odd_card listings
# route here regardless of subject — it's a single shared bucket for
# every odd-size signed card (tickets, index cards, autograph slips).
# Fetched live via GetStore on 2026-04-15.
_STORE_CATEGORY_WHITE_CARDS = 85843959013

# Optional trailing tokens that get appended to a rendered title IF they
# fit inside the 80-char budget. They are tried in order and each is
# added greedily (appending one never prevents the next from also being
# added). The goal is to fill dead space in short titles with extra
# customer-facing keywords — "Memorabilia" is a genuine Cassini search
# term, "COA" has no search value but is a reassuring eyeball marker
# ("Certificate of Authenticity Included" is already in item specifics).
#
# Order matters: Memorabilia first (more search juice), then COA, so
# the mid-budget case — space for only one — picks the better one.
TITLE_FILLER_TOKENS: tuple[str, ...] = (" Memorabilia", " COA")


def _compose_title(
    pattern: str,
    name: str,
    suffix: str,
    hand_prefix: str = "",
) -> str:
    """Plug name + team_suffix + hand_prefix into a product title pattern.

    Accepts both `{qualifier_suffix}` (legacy) and `{team_suffix}`
    (current) for the team slot. `{hand_prefix}` is optional — patterns
    that don't reference it silently ignore the kwarg. It renders as
    either " Hand" (if there's budget) or "" (dropped to make room for
    Field1). The space is inside the token so "{name}{hand_prefix}
    Signed …" works cleanly either way.
    """
    return pattern.format(
        name=name.strip(),
        team_suffix=suffix,
        qualifier_suffix=suffix,
        hand_prefix=hand_prefix,
    )


def _build_team_suffix(
    bundle: PresetsBundle,
    field1: Optional[str],
    category: Optional[str],
) -> list[str]:
    """
    Return a list of candidate title suffixes, longest first. The
    render_title loop tries each in order and picks the first that
    fits inside the 80-char budget.

    Policy: prefer the LONG form of a club ("Manchester United") in
    title — it's the higher-search-volume canonical term. Fall back to
    the short form ("Man Utd") only when the long form would bust the
    80-char budget. Works regardless of whether the caller passes the
    short or long form: we normalise both forms via expand_club/shrink_club.

    Candidate order for field1="Manchester United", category="Football" (in_title: False):
        [" Manchester United", " Man Utd", ""]

    For field1="Man Utd", category="Football" (same result — long form preferred):
        [" Manchester United", " Man Utd", ""]

    For field1="Leicester Tigers", category="Rugby" (in_title: True, no club alias):
        [" Leicester Tigers Rugby", " Leicester Tigers", ""]

    For field1=None:
        [""]
    """
    candidates: list[str] = []

    field1_clean = (field1 or "").strip()
    rule = bundle.category_rule(category)
    in_title = bool(rule.get("in_title"))

    if field1_clean:
        # Figure out short + long forms, regardless of which was passed in.
        short_form = bundle.shrink_club(field1_clean) or field1_clean
        long_form  = bundle.expand_club(short_form) or short_form

        if in_title and category:
            candidates.append(f" {long_form} {category.strip()}")
        candidates.append(f" {long_form}")
        if short_form != long_form:
            candidates.append(f" {short_form}")

    # Always include empty string as the last-resort fallback.
    candidates.append("")

    # De-dupe while preserving order.
    seen = set()
    out: list[str] = []
    for c in candidates:
        if c not in seen:
            out.append(c)
            seen.add(c)
    return out


def render_title(
    bundle: PresetsBundle,
    product_key: str,
    name: str,
    qualifier: Optional[str] = None,
    *,
    field1: Optional[str] = None,
    category: Optional[str] = None,
) -> str:
    """
    Apply the product's title pattern.

    title_pattern uses three placeholders:
        {name}         — signer name (required)
        {hand_prefix}  — " Hand" (if it fits after Field1) or ""
        {team_suffix}  — " <field1>" (+ " <Category>" if in_title) or ""

    Priority (highest → lowest, dropped first when over 80-char cap):
        1. Name                              (never dropped — error instead)
        2. Product descriptor (Signed Card / Photo Mount / …)
        3. Autograph
        4. Field1 (from Nicky's filename — Club / TV / Band / Keywords)
        5. Category keyword (if knowledge.yaml `in_title: true`)
        6. "Hand" (in front of "Signed")
        7. Memorabilia                       (appended filler if room)
        8. COA                               (appended filler if room)

    Build loop tries Field1 candidates longest-first; within each
    candidate it prefers the form WITH " Hand" and falls back to the
    form WITHOUT "Hand" before trimming Field1 further. Then Memorabilia
    and COA are greedily appended if budget remains.

    Example:
        pattern: "{name} Signed A4 Photo Mount Display{team_suffix} Autograph"
        name="Ellis Genge", field1="Leicester Tigers", category="Rugby"
            try 1: "Ellis Genge Signed A4 Photo Mount Display Leicester Tigers Rugby Autograph"
                   → over 80, reject
            try 2: "Ellis Genge Signed A4 Photo Mount Display Leicester Tigers Autograph"
                   → fits, pick this.

    `qualifier` is a legacy alias: if passed and `field1` is None, it
    fills field1. Kept so older callers (CLI flags, old dashboard code)
    still work unchanged.
    """
    product = bundle.product(product_key)

    # Legacy `qualifier` → field1 back-compat.
    if field1 is None and qualifier is not None and qualifier.strip():
        field1 = qualifier.strip()

    # Candidate generation now handles long/short form selection itself:
    # tries the long form first (higher search volume) and falls back to
    # the short form only when the 80-char budget forces it.
    candidates = _build_team_suffix(bundle, field1, category)

    # Field1 is prioritised ahead of "Hand" in "Hand Signed". For each
    # Field1 candidate (longest first), we try WITH " Hand" first; if
    # that overflows, we drop "Hand" from this Field1 candidate before
    # falling back to a shorter Field1. This means Field1 will only be
    # trimmed once even dropping "Hand" can't save the current form.
    chosen: Optional[str] = None
    for suffix in candidates:
        with_hand = _compose_title(product.title_pattern, name, suffix, " Hand")
        if len(with_hand) <= MAX_TITLE_LEN:
            chosen = with_hand
            break
        without_hand = _compose_title(product.title_pattern, name, suffix, "")
        if len(without_hand) <= MAX_TITLE_LEN:
            chosen = without_hand
            break

    if chosen is None:
        # Even the empty-suffix, no-Hand form is too long — name is too long.
        overflow = _compose_title(product.title_pattern, name, "", "")
        raise PresetsError(
            f"Rendered title is {len(overflow)} chars "
            f"(>{MAX_TITLE_LEN}, eBay max):\n  {overflow}"
        )

    # Greedily pack trailing filler tokens into any leftover budget.
    # "Memorabilia" has real search value, "COA" is just a reassuring
    # eyeball marker — both get tried independently so a mid-sized
    # title can fit one without needing room for the other.
    for token in TITLE_FILLER_TOKENS:
        if len(chosen) + len(token) <= MAX_TITLE_LEN:
            chosen += token

    return chosen


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
    photo_size: Optional[str] = None,
    variant: Optional[str] = None,
) -> Optional[str]:
    """
    Resolve the concrete template folder for a product.

    Resolution order (highest to lowest priority):
      1. explicit `variant` argument (e.g. "16x12-c-mount")
      2. compound key "{photo_size}_{orientation}" in variants
         (used by 16x12_cdef: "12x8_landscape" → "16x12-c-mount")
      3. plain `orientation` key in variants
         (used by 10x8: "landscape" → "10x8-mount-land")
      4. the product's default template_id from products.yaml

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

    # 2+3. Variant lookup — try compound key first, then plain orientation.
    variants_for_product = bundle.variants.get(product_key, {}) or {}
    if variants_for_product:
        if photo_size and orientation:
            compound = f"{photo_size}_{orientation}"
            if compound in variants_for_product:
                return variants_for_product[compound]
        if orientation and orientation in variants_for_product:
            return variants_for_product[orientation]

    # 4. Fall back to the product default.
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

def enrich_specifics_from_knowledge(
    bundle: PresetsBundle,
    field1: Optional[str],
    category: Optional[str],
) -> dict[str, str]:
    """
    Build the knowledge-derived item specifics for one listing.

    Fields emitted (all optional — only present if data is available):

      * <field1_label>           — e.g. "Club": "Man Utd"
      * <field1_label> (Full)    — e.g. "Club (Full)": "Manchester United"
      * Category Keywords        — Cassini SEO payload for that sport/
                                    music/TV bucket
      * Category                 — bare category word (e.g. "Rugby")

    Keys are chosen so every name stays ≤40 and value ≤65 chars. We use
    "(Full)" rather than "(Full Name)" because some labels like
    "Weight Class" push the total over 40 with " (Full Name)".

    Returns an empty dict for unknown categories, missing field1, or
    an empty knowledge bundle — enrichment fails open.
    """
    out: dict[str, str] = {}
    rule = bundle.category_rule(category)
    if not rule:
        return out

    field1_clean = (field1 or "").strip() or None
    label = (rule.get("field1_label") or "").strip() or None

    if field1_clean and label:
        # If Nicky typed the full form ("Manchester United"), normalise
        # to the short form for the primary specific so IS coverage
        # stays consistent regardless of which form he typed.
        shortform = bundle.shrink_club(field1_clean)
        if shortform:
            field1_clean = shortform

        # Main specific: the short form goes under the label.
        short_value = field1_clean[:65]
        out[label[:40]] = short_value

        # If the club dictionary has an expansion and it differs, also
        # emit the full form under a parallel "(Full)" label.
        full = bundle.expand_club(field1_clean)
        if full and full.strip() and full.strip() != field1_clean:
            full_label = f"{label} (Full)"
            if len(full_label) <= 40:
                out[full_label] = full.strip()[:65]

    kw = (rule.get("is_keywords") or "").strip()
    if kw:
        out["Category Keywords"] = kw[:65]

    if category and category.strip():
        out["Category"] = category.strip()[:65]

    return out


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


def _resolve_best_offer(price_gbp: float) -> Optional[dict]:
    """
    Look up BestOffer thresholds for `price_gbp` in the offers table.

    Returns a dict {list_price, min_offer, auto_accept} if BestOffer
    applies, or None for:
      * prices below £15.99 (no-BO band)
      * non-`.99` list prices (lookup raises — we swallow it and
        return None; the strict validation runs in offers.lookup
        when it matters, and a build_listing sanity-check caller
        can still invoke offers.lookup directly)

    We deliberately fail-soft here so tests and dry-runs that pass
    round numbers (£75.00) don't blow up. The ultimate "no dodgy
    prices hit eBay" guard lives higher up: the dashboard only shows
    suggested .99 chips, and Nicky can't submit a listing without a
    real lookup succeeding at XML-build time.
    """
    try:
        row = offers.lookup(float(price_gbp))
    except offers.OfferLookupError:
        return None
    if row is None:
        return None
    return {
        "list_price":  row.list_price,
        "min_offer":   row.min_offer,
        "auto_accept": row.auto_accept,
    }


def build_listing(
    bundle: PresetsBundle,
    *,
    product_key: str,
    name: Optional[str] = None,
    qualifier: Optional[str] = None,
    parsed: Optional[ParsedFilename] = None,
    field1: Optional[str] = None,
    category: Optional[str] = None,
    subject: Optional[str] = None,
    orientation: Optional[str] = None,
    photo_size: Optional[str] = None,
    variant: Optional[str] = None,
    price_gbp: Optional[float] = None,
    quantity: Optional[int] = None,
    sku: Optional[str] = None,
    item_specifics: Optional[dict[str, str]] = None,
    overrides: Optional[dict] = None,
) -> dict:
    """
    Render a full listing dict for a single item.

    Inputs:
      * `parsed` — a ParsedFilename (from pipeline.filename). If
        provided, its `name`/`field1`/`category` fill the matching
        kwargs (explicit kwargs still win).
      * `name`, `field1`, `category` — raw values. `qualifier` is a
        legacy alias for field1 and is honoured if field1 is None.
      * `subject` — eBay category subject slug (football_retired,
        music_pop, …). If None, auto-derived from knowledge.yaml's
        `subject` field for the listing's `category`, falling back
        to "default".

    Output shape (stable — this is what pipeline.lister consumes):

        {
          "product_key": str,
          "template_id": str | None,   # resolved with orientation/variant
          "title": str,
          "description_html": str,
          "price_gbp": float,
          "sku": str | None,
          "category_id": int,
          "marketplace":     {...},    # from defaults
          "listing":         {...},    # from defaults
          "seller_profiles": {...},    # from defaults
          "shipping":        {...},    # from defaults, documentation only
          "return_policy":   {...},    # from defaults, documentation only
          "item_specifics":  {...},    # layered: defaults ∪ knowledge
                                       #          ∪ product ∪ caller
          "best_offer":      {...}|None,  # from pipeline.offers lookup
        }

    `overrides` is deep-merged into the resulting dict last, so callers
    can patch anything (e.g. bump price, override a shipping service)
    without going through a dedicated kwarg.
    """
    product = bundle.product(product_key)

    # ---- Resolve the "what are we listing" inputs ----------------------
    if parsed is not None:
        if name is None:
            name = parsed.name
        if field1 is None:
            field1 = parsed.field1
        if category is None:
            category = parsed.category
    if field1 is None and qualifier is not None and qualifier.strip():
        field1 = qualifier.strip()
    if not name:
        raise PresetsError("build_listing: `name` is required")

    # Subject defaults: prefer explicit, then knowledge.yaml's
    # per-category subject slug, then the hard-coded "default".
    if subject is None:
        rule = bundle.category_rule(category)
        subject = rule.get("subject") or "default"

    # ---- Render the human-visible bits ---------------------------------
    title = render_title(
        bundle,
        product_key,
        name,
        field1=field1,
        category=category,
    )
    description_html = render_description(bundle, product_key)
    template_id = pick_template_id(
        bundle,
        product_key,
        orientation=orientation,
        photo_size=photo_size,
        variant=variant,
    )
    category_id = get_category_id(bundle, subject)

    # ---- Store category (Kim's eBay shop bucket) -----------------------
    # Order of precedence:
    #   1. odd_card layout → "Signed White Cards" regardless of category
    #   2. knowledge.yaml's per-category store_category_id
    #   3. None → eBay defaults to the store's top-level "Other" bucket.
    store_category_id: Optional[int] = None
    if product.raw.get("layout") == "odd_card":
        store_category_id = _STORE_CATEGORY_WHITE_CARDS
    else:
        rule = bundle.category_rule(category)
        raw_sc = rule.get("store_category_id")
        if raw_sc is not None:
            try:
                store_category_id = int(raw_sc)
            except (TypeError, ValueError):
                store_category_id = None

    effective_price = float(
        price_gbp if price_gbp is not None else product.default_price_gbp
    )

    # ---- Build the layered item-specifics block -----------------------
    specifics: dict[str, str] = copy.deepcopy(
        bundle.defaults.get("item_specifics", {})
    )
    # Layer 2: knowledge-derived (per-category enrichment)
    specifics.update(enrich_specifics_from_knowledge(bundle, field1, category))
    # Layer 3: per-product (optional — only a few products will need this)
    product_specifics = product.raw.get("item_specifics") or {}
    if isinstance(product_specifics, dict):
        specifics.update({str(k): str(v) for k, v in product_specifics.items()})
    # Layer 4: caller-supplied (wins)
    if item_specifics:
        specifics.update({str(k): str(v) for k, v in item_specifics.items()})

    # ---- Assemble the listing dict -------------------------------------
    # VAT — Kim is VAT-registered; every listing carries a 20% rate.
    # Held in defaults.yaml so it can be bumped if HMRC changes the rate.
    vat_percent = bundle.defaults.get("vat_percent")
    try:
        vat_percent = float(vat_percent) if vat_percent is not None else None
    except (TypeError, ValueError):
        vat_percent = None

    listing_cfg = copy.deepcopy(bundle.defaults.get("listing", {}))
    if quantity is not None:
        # Per-listing override beats the defaults.yaml quantity.
        listing_cfg["quantity"] = int(quantity)

    listing: dict = {
        "product_key": product_key,
        "template_id": template_id,
        "title": title,
        "description_html": description_html,
        "price_gbp": effective_price,
        "sku": sku,
        "category_id": category_id,
        "store_category_id": store_category_id,
        "vat_percent": vat_percent,
        "marketplace":      copy.deepcopy(bundle.defaults.get("marketplace", {})),
        "listing":          listing_cfg,
        "seller_profiles":  copy.deepcopy(bundle.defaults.get("seller_profiles", {})),
        # shipping + return_policy kept for documentation only; the lister
        # uses seller_profiles when the account is on Business Policies.
        "shipping":         copy.deepcopy(bundle.defaults.get("shipping", {})),
        "return_policy":    copy.deepcopy(bundle.defaults.get("return_policy", {})),
        "item_specifics":   specifics,
        "best_offer":       _resolve_best_offer(effective_price),
    }

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
    parser.add_argument("--field1", default=None,
                        help="club / band / show / nickname (field 1)")
    parser.add_argument("--category", default=None,
                        help="Football / Rugby / Music / ... (field 2)")
    parser.add_argument("--qualifier", default=None,
                        help="legacy alias for --field1")
    parser.add_argument("--subject", default=None,
                        help="override eBay category subject slug")
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
        field1=args.field1,
        category=args.category,
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
