"""
Tests for pipeline.presets — loader + render helpers.

These run against the REAL presets/ directory (not a mock) because the
files are checked in, small, and stable. If defaults.yaml or
products.yaml gets reshaped, these tests should be the first thing to
break.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import presets
from pipeline.filename import ParsedFilename

REPO_ROOT = Path(__file__).resolve().parent.parent
PRESETS_DIR = REPO_ROOT / "presets"


# --------------------------------------------------------------------------- #
# Loader
# --------------------------------------------------------------------------- #

def test_load_bundle_has_expected_products():
    """All 16 products from the catalog should be present.

    The old C/D/E/F (8 products) were merged into CDEF (2 products):
      22 - 8 + 2 = 16.
    """
    bundle = presets.load(PRESETS_DIR)
    expected_mount_layouts = {
        "a4_mount_a", "a4_mount_b",
        "10x8_mount",
        "16x12_mount_a", "16x12_mount_b",
        "16x12_mount_cdef",
    }
    expected_frame_layouts = {
        "a4_frame_a", "a4_frame_b",
        "10x8_frame",
        "16x12_frame_a", "16x12_frame_b",
        "16x12_frame_cdef",
    }
    expected_photo_only = {
        "photo_6x4", "photo_10x8", "photo_12x8", "odd_card", "odd_photo",
    }
    expected = expected_mount_layouts | expected_frame_layouts | expected_photo_only
    assert expected <= set(bundle.products)
    assert len(bundle.products) == 17, \
        f"expected 17 products, got {len(bundle.products)}: {sorted(bundle.products)}"


def test_load_defaults_contain_core_sections():
    bundle = presets.load(PRESETS_DIR)
    for section in ("marketplace", "listing", "shipping", "return_policy",
                    "item_specifics"):
        assert section in bundle.defaults, f"missing defaults section: {section}"


# --------------------------------------------------------------------------- #
# defaults.yaml item_specifics — the "no product leak" contract
# --------------------------------------------------------------------------- #

# These are the forbidden substrings: words that name a specific product
# type and would leak into listings where that type doesn't apply. Casing
# is intentional — we compare case-insensitively below.
_LEAK_WORDS = (
    "a4", "10x8", "12x8", "16x12", "6x4",
    "mount", "mounted", "frame", "framed", "photo",
)


def test_defaults_specifics_have_required_ebay_fields():
    """Category-mandated fields must survive any rewrite."""
    bundle = presets.load(PRESETS_DIR)
    specifics = bundle.defaults["item_specifics"]
    assert specifics["Country of Origin"] == "United Kingdom"
    assert specifics["Signed"] == "Yes"
    assert specifics["Original/Reproduction"] == "Original"


def test_defaults_specifics_renamed_to_coa_included():
    """The old 'Certificate Included' field is gone, replaced by 'COA Included'."""
    bundle = presets.load(PRESETS_DIR)
    specifics = bundle.defaults["item_specifics"]
    assert "COA Included" in specifics
    assert "Certificate Included" not in specifics
    # Value keeps the COA / LOA / Cert keyword payload for Cassini.
    assert "COA" in specifics["COA Included"]
    assert "LOA" in specifics["COA Included"]


def test_defaults_specifics_have_other_styles_cross_sell():
    """Kim's #2 FAQ question gets its own specific."""
    bundle = presets.load(PRESETS_DIR)
    specifics = bundle.defaults["item_specifics"]
    assert "More In Our Shop" in specifics
    # "More In Our Shop" is a cross-sell advertising format availability;
    # it's the ONE intentional exception to the no-leak rule, so we
    # exempt it from the leak check below. Its job is to name every
    # format Kim does — so A4 / 10x8 / 16x12 / Mount / Frame etc. are
    # allowed to appear in this value only.


def test_defaults_specifics_have_no_product_leak():
    """
    Every NON-exempted default value must be free of product-type words
    so none of them falsely imply the current listing is any particular
    format. 'Other Styles' is exempt — its whole purpose is to list
    formats.
    """
    bundle = presets.load(PRESETS_DIR)
    specifics = bundle.defaults["item_specifics"]
    exempt = {"More In Our Shop"}
    for name, value in specifics.items():
        if name in exempt:
            continue
        lowered = str(value).lower()
        for word in _LEAK_WORDS:
            assert word not in lowered, (
                f"item_specifics[{name!r}] leaks product word "
                f"{word!r}: {value!r}"
            )


def test_defaults_specifics_within_ebay_char_limits():
    """
    eBay caps item-specific names at 40 chars and values at 65.
    Blowing past either is an instant listing rejection, and because
    these are shared defaults a single over-length value would tank
    EVERY listing. Lock it in with a test.
    """
    bundle = presets.load(PRESETS_DIR)
    for name, value in bundle.defaults["item_specifics"].items():
        assert len(name)  <= 40, f"specific name {name!r} is {len(name)} chars (>40)"
        assert len(str(value)) <= 65, (
            f"specific {name!r} value is {len(str(value))} chars (>65): {value!r}"
        )


def test_description_template_has_size_clause_placeholder():
    bundle = presets.load(PRESETS_DIR)
    assert "{size_clause}" in bundle.description_template


def test_product_entries_are_frozen_with_required_fields():
    bundle = presets.load(PRESETS_DIR)
    p = bundle.product("16x12_mount_a")
    assert p.template_id == "16x12-a-mount"
    assert p.default_price_gbp == pytest.approx(49.99)
    assert "16x12" in p.size_clause
    assert "{name}" in p.title_pattern
    # New title pattern uses {team_suffix}, not {qualifier_suffix}
    assert "{team_suffix}" in p.title_pattern


def test_unknown_product_key_raises():
    bundle = presets.load(PRESETS_DIR)
    with pytest.raises(presets.PresetsError):
        bundle.product("nonesuch_product")


# --------------------------------------------------------------------------- #
# render_title
# --------------------------------------------------------------------------- #

def test_render_title_basic():
    bundle = presets.load(PRESETS_DIR)
    title = presets.render_title(bundle, "photo_10x8", "Tim Allen")
    # photo_10x8 dropped its ambiguous "Display" word (commit aa4d294) —
    # photo-only products now match the 6x4 pattern.
    assert title.startswith("Tim Allen Hand Signed 10x8 Photo Autograph")
    assert "  " not in title  # no accidental double space from empty team_suffix


def test_render_title_with_qualifier():
    bundle = presets.load(PRESETS_DIR)
    title = presets.render_title(
        bundle, "photo_10x8", "Mel C", qualifier="Spice Girls"
    )
    assert " Spice Girls " in title


def test_render_title_empty_qualifier_is_same_as_none():
    bundle = presets.load(PRESETS_DIR)
    a = presets.render_title(bundle, "photo_10x8", "Tim Allen", qualifier="")
    b = presets.render_title(bundle, "photo_10x8", "Tim Allen", qualifier=None)
    assert a == b


def test_render_title_whitespace_qualifier_is_stripped():
    bundle = presets.load(PRESETS_DIR)
    a = presets.render_title(bundle, "photo_10x8", "Tim Allen", qualifier="   ")
    b = presets.render_title(bundle, "photo_10x8", "Tim Allen", qualifier=None)
    assert a == b


def test_render_title_raises_if_too_long():
    bundle = presets.load(PRESETS_DIR)
    with pytest.raises(presets.PresetsError):
        presets.render_title(
            bundle,
            "photo_10x8",
            "X" * 100,  # force over 80 chars
        )


# --------------------------------------------------------------------------- #
# render_description
# --------------------------------------------------------------------------- #

def test_render_description_substitutes_size_clause():
    bundle = presets.load(PRESETS_DIR)
    html = presets.render_description(bundle, "16x12_mount_a")
    assert "{size_clause}" not in html
    # The 16x12 mount size_clause mentions 16x12
    assert "16x12" in html


def test_render_description_supports_extra_placeholders():
    bundle = presets.load(PRESETS_DIR)
    # Hack: build a fake bundle with a {player} placeholder in the template
    hacked = presets.PresetsBundle(
        defaults=bundle.defaults,
        products=bundle.products,
        description_template=bundle.description_template + "<p>Player: {player}</p>",
        variants=bundle.variants,
        categories_by_subject=bundle.categories_by_subject,
    )
    html = presets.render_description(
        hacked, "photo_10x8", extra_placeholders={"player": "Mel C"}
    )
    assert "<p>Player: Mel C</p>" in html


# --------------------------------------------------------------------------- #
# pick_template_id
# --------------------------------------------------------------------------- #

def test_pick_template_id_plain_photo_returns_none():
    bundle = presets.load(PRESETS_DIR)
    assert presets.pick_template_id(bundle, "photo_6x4") is None
    assert presets.pick_template_id(bundle, "odd_card") is None
    assert presets.pick_template_id(bundle, "odd_photo") is None


def test_pick_template_id_uses_product_default():
    bundle = presets.load(PRESETS_DIR)
    # Each mount/frame variant has its own product key → template_id
    # maps 1:1 to the template folder. No variant-picking needed here.
    assert presets.pick_template_id(bundle, "16x12_mount_a") == "16x12-a-mount"
    assert presets.pick_template_id(bundle, "a4_mount_a") == "a4-a-mount"
    assert presets.pick_template_id(bundle, "a4_frame_b") == "a4-b-frame"
    # CDEF default is 16x12-c-mount (the first variant)
    assert presets.pick_template_id(bundle, "16x12_mount_cdef") == "16x12-c-mount"


def test_pick_template_id_orientation_landscape_portrait():
    """10x8 mount/frame use the variants block to pick land/port."""
    bundle = presets.load(PRESETS_DIR)
    assert presets.pick_template_id(
        bundle, "10x8_mount", orientation="landscape"
    ) == "10x8-mount-land"
    assert presets.pick_template_id(
        bundle, "10x8_mount", orientation="portrait"
    ) == "10x8-mount-port"
    assert presets.pick_template_id(
        bundle, "10x8_frame", orientation="portrait"
    ) == "10x8-frame-port"


def test_pick_template_id_cdef_compound_key():
    """16x12 CDEF uses compound keys: photo_size + orientation."""
    bundle = presets.load(PRESETS_DIR)
    # 12x8 landscape → C template
    assert presets.pick_template_id(
        bundle, "16x12_mount_cdef",
        orientation="landscape", photo_size="12x8",
    ) == "16x12-c-mount"
    # 12x8 portrait → D template
    assert presets.pick_template_id(
        bundle, "16x12_mount_cdef",
        orientation="portrait", photo_size="12x8",
    ) == "16x12-d-mount"
    # 10x8 landscape → E template
    assert presets.pick_template_id(
        bundle, "16x12_frame_cdef",
        orientation="landscape", photo_size="10x8",
    ) == "16x12-e-frame"
    # 10x8 portrait → F template
    assert presets.pick_template_id(
        bundle, "16x12_frame_cdef",
        orientation="portrait", photo_size="10x8",
    ) == "16x12-f-frame"


# --------------------------------------------------------------------------- #
# get_category_id
# --------------------------------------------------------------------------- #

def test_get_category_id_known_subject():
    bundle = presets.load(PRESETS_DIR)
    assert presets.get_category_id(bundle, "football_retired") == 97085


def test_get_category_id_falls_back_to_default():
    bundle = presets.load(PRESETS_DIR)
    # An unknown subject falls back to 'default'
    assert presets.get_category_id(bundle, "not_a_real_subject") \
        == bundle.categories_by_subject["default"]


# --------------------------------------------------------------------------- #
# build_listing — end-to-end render
# --------------------------------------------------------------------------- #

def test_build_listing_shape():
    bundle = presets.load(PRESETS_DIR)
    listing = presets.build_listing(
        bundle,
        product_key="photo_10x8",
        name="Tim Allen",
        subject="film_tv",
    )
    # Core fields
    assert listing["product_key"] == "photo_10x8"
    assert listing["template_id"] is None
    assert listing["title"].startswith("Tim Allen Hand Signed 10x8 Photo")
    assert "{size_clause}" not in listing["description_html"]
    assert listing["price_gbp"] == pytest.approx(19.99)
    assert listing["category_id"] == 2312  # film_tv
    # Defaults came through
    assert listing["marketplace"]["site"] == "EBAY_GB"
    assert listing["listing"]["listing_duration"] == "GTC"
    assert any(
        s["service"] == "UK_RoyalMailSecondClassStandard"
        for s in listing["shipping"]["services"]
    )
    assert listing["return_policy"]["returns_accepted"] is True
    assert listing["item_specifics"]["Signed"] == "Yes"


def test_build_listing_store_category_from_knowledge():
    """Non-card listings pick up store_category_id from knowledge.yaml."""
    bundle = presets.load(PRESETS_DIR)
    listing = presets.build_listing(
        bundle,
        product_key="photo_10x8",
        name="Wayne Rooney",
        field1="Man Utd",
        category="Football",
    )
    assert listing["store_category_id"] == 1954551013   # Football Autographs


def test_build_listing_store_category_music():
    bundle = presets.load(PRESETS_DIR)
    listing = presets.build_listing(
        bundle,
        product_key="a4_frame_a",
        name="Debbie Harry",
        field1="Blondie",
        category="Music",
    )
    assert listing["store_category_id"] == 1954554013   # Music Autographs


def test_build_listing_store_category_odd_card_forced_to_white_cards():
    """
    odd_card listings ALWAYS land in "Signed White Cards" regardless of
    what subject/category the signer has. Kim's convention: all odd-size
    cards share one shop bucket.
    """
    bundle = presets.load(PRESETS_DIR)
    listing = presets.build_listing(
        bundle,
        product_key="odd_card",
        name="Wayne Rooney",
        field1="Man Utd",
        category="Football",   # would normally route to Football Autographs
    )
    assert listing["store_category_id"] == 85843959013  # Signed White Cards


def test_build_listing_store_category_unknown_category_is_none():
    """Fails open for categories with no knowledge.yaml entry."""
    bundle = presets.load(PRESETS_DIR)
    listing = presets.build_listing(
        bundle,
        product_key="photo_10x8",
        name="Some Signer",
        field1="Something",
        category="Nonesuch",
    )
    assert listing["store_category_id"] is None


def test_build_listing_has_vat_20_percent():
    bundle = presets.load(PRESETS_DIR)
    listing = presets.build_listing(
        bundle,
        product_key="photo_10x8",
        name="Tim Allen",
    )
    assert listing["vat_percent"] == pytest.approx(20.0)


def test_build_listing_includes_seller_profiles():
    bundle = presets.load(PRESETS_DIR)
    listing = presets.build_listing(
        bundle,
        product_key="photo_10x8",
        name="Tim Allen",
    )
    sp = listing["seller_profiles"]
    assert sp["payment_profile_id"]  == "226381763024"
    assert sp["return_profile_id"]   == "226381757024"
    assert sp["shipping_profile_id"] == "226588406024"


def test_build_listing_has_postal_code():
    bundle = presets.load(PRESETS_DIR)
    listing = presets.build_listing(
        bundle,
        product_key="photo_10x8",
        name="Tim Allen",
    )
    assert listing["marketplace"]["postal_code"] == "M29 8DL"


def test_build_listing_price_override():
    bundle = presets.load(PRESETS_DIR)
    listing = presets.build_listing(
        bundle,
        product_key="16x12_mount_a",
        name="Alan Hansen",
        price_gbp=75.00,
    )
    assert listing["price_gbp"] == pytest.approx(75.00)


def test_build_listing_item_specifics_merged():
    bundle = presets.load(PRESETS_DIR)
    listing = presets.build_listing(
        bundle,
        product_key="16x12_mount_a",
        name="Alan Hansen",
        subject="football_retired",
        item_specifics={"Player": "Alan Hansen", "Team": "Liverpool"},
    )
    assert listing["item_specifics"]["Signed"] == "Yes"              # from defaults
    assert listing["item_specifics"]["Country of Origin"] == "United Kingdom"
    assert listing["item_specifics"]["Player"] == "Alan Hansen"      # from caller
    assert listing["item_specifics"]["Team"] == "Liverpool"


def test_build_listing_orientation_selects_variant_template():
    bundle = presets.load(PRESETS_DIR)
    listing = presets.build_listing(
        bundle,
        product_key="10x8_mount",
        name="Steve Bruce",
        orientation="portrait",
    )
    assert listing["template_id"] == "10x8-mount-port"


def test_build_listing_overrides_deep_merge():
    bundle = presets.load(PRESETS_DIR)
    listing = presets.build_listing(
        bundle,
        product_key="photo_10x8",
        name="Tim Allen",
        overrides={
            "listing": {"dispatch_time_max": 2},          # patch one field
            "marketplace": {"location": "London, UK"},    # patch one field
        },
    )
    # Overridden
    assert listing["listing"]["dispatch_time_max"] == 2
    assert listing["marketplace"]["location"] == "London, UK"
    # Still intact from defaults
    assert listing["listing"]["listing_duration"] == "GTC"
    assert listing["marketplace"]["site"] == "EBAY_GB"


# --------------------------------------------------------------------------- #
# Catalog metadata — new in the 22-product rewrite
# --------------------------------------------------------------------------- #

def test_layout_groups_pair_mounts_with_frames():
    """Every mount layout should have exactly one frame twin (same `layout`)."""
    bundle = presets.load(PRESETS_DIR)
    by_layout: dict[str, list[str]] = {}
    for key, prod in bundle.products.items():
        layout = prod.raw.get("layout")
        by_layout.setdefault(layout, []).append(key)

    # 6 mount/frame layouts should each have exactly 2 entries (mount + frame)
    mount_frame_layouts = [
        "a4_a", "a4_b", "10x8",
        "16x12_a", "16x12_b", "16x12_cdef",
    ]
    for layout in mount_frame_layouts:
        assert layout in by_layout, f"layout {layout!r} missing from catalog"
        entries = by_layout[layout]
        assert len(entries) == 2, \
            f"layout {layout!r} should have mount+frame twin, got {entries}"
        frames = [
            k for k in entries if bundle.products[k].raw.get("frame")
        ]
        mounts = [
            k for k in entries if not bundle.products[k].raw.get("frame")
        ]
        assert len(frames) == 1, f"{layout}: expected 1 frame, got {frames}"
        assert len(mounts) == 1, f"{layout}: expected 1 mount, got {mounts}"


def test_photo_only_products_have_no_template_and_no_secondary():
    bundle = presets.load(PRESETS_DIR)
    for key in ("photo_6x4", "photo_10x8", "photo_12x8", "odd_card", "odd_photo"):
        p = bundle.product(key)
        assert p.template_id is None, f"{key} should have template_id=None"
        assert p.raw.get("needs_secondary") is None


def test_products_carry_suggested_prices_list():
    bundle = presets.load(PRESETS_DIR)
    for key, prod in bundle.products.items():
        sp = prod.raw.get("suggested_prices")
        assert isinstance(sp, list) and len(sp) >= 3, \
            f"{key} missing suggested_prices (need ≥3)"
        # Default should be in the suggested list
        assert prod.default_price_gbp in sp, \
            f"{key}: default_price_gbp {prod.default_price_gbp} not in suggested_prices {sp}"


# --------------------------------------------------------------------------- #
# _deep_merge unit test
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# knowledge.yaml — loader + per-category rules
# --------------------------------------------------------------------------- #

def test_knowledge_loaded_into_bundle():
    bundle = presets.load(PRESETS_DIR)
    assert isinstance(bundle.knowledge, dict)
    # The real file ships with at least these two top-level blocks.
    assert "categories" in bundle.knowledge
    assert "clubs" in bundle.knowledge


def test_category_rule_known_and_unknown():
    bundle = presets.load(PRESETS_DIR)
    football = bundle.category_rule("Football")
    assert football["field1_label"] == "Club"
    assert football.get("in_title") is False
    rugby = bundle.category_rule("Rugby")
    assert rugby.get("in_title") is True
    # Fail-open: unknown and None both return empty dict.
    assert bundle.category_rule("Klingon Opera") == {}
    assert bundle.category_rule(None) == {}


def test_expand_club_short_to_full():
    bundle = presets.load(PRESETS_DIR)
    assert bundle.expand_club("Man Utd") == "Manchester United"
    assert bundle.expand_club("Spurs") == "Tottenham Hotspur"
    assert bundle.expand_club("Nowhere FC") is None
    assert bundle.expand_club(None) is None


def test_shrink_club_full_to_short():
    """Reverse lookup: if filename carries the long form, map to short for title."""
    bundle = presets.load(PRESETS_DIR)
    assert bundle.shrink_club("Manchester United") == "Man Utd"
    assert bundle.shrink_club("Tottenham Hotspur") == "Spurs"
    # Case and whitespace insensitive
    assert bundle.shrink_club("manchester united") == "Man Utd"
    assert bundle.shrink_club("  Manchester   United  ") == "Man Utd"
    # Known team with short==long: returns the key verbatim (harmless no-op).
    assert bundle.shrink_club("Arsenal") == "Arsenal"
    # Completely unknown team: None
    assert bundle.shrink_club("Nowhere FC") is None
    assert bundle.shrink_club("") is None
    assert bundle.shrink_club(None) is None


def test_render_title_shrinks_long_club_name_from_filename():
    """If Nicky types 'Manchester United' we still get 'Man Utd' in title."""
    bundle = presets.load(PRESETS_DIR)
    title_long = presets.render_title(
        bundle, "a4_mount_a", "Wayne Rooney",
        field1="Manchester United", category="Football",
    )
    title_short = presets.render_title(
        bundle, "a4_mount_a", "Wayne Rooney",
        field1="Man Utd", category="Football",
    )
    # Same outcome regardless of which form the filename used.
    assert title_long == title_short
    assert "Man Utd" in title_long
    assert "Manchester United" not in title_long


def test_enrich_specifics_normalises_longform_field1():
    """IS should still carry both forms even if filename had the long form."""
    bundle = presets.load(PRESETS_DIR)
    is_long = presets.enrich_specifics_from_knowledge(
        bundle, field1="Manchester United", category="Football",
    )
    is_short = presets.enrich_specifics_from_knowledge(
        bundle, field1="Man Utd", category="Football",
    )
    assert is_long == is_short
    assert is_long["Club"] == "Man Utd"
    assert is_long["Club (Full)"] == "Manchester United"


# --------------------------------------------------------------------------- #
# render_title — new knowledge-driven behaviour
# --------------------------------------------------------------------------- #

def test_render_title_with_field1_injects_team_suffix():
    bundle = presets.load(PRESETS_DIR)
    title = presets.render_title(
        bundle,
        "photo_10x8",
        "Wayne Rooney",
        field1="Man Utd",
        category="Football",
    )
    # Football has in_title: false → just the club short form.
    assert " Man Utd " in title
    # Category word must NOT appear when in_title is false.
    assert "Football" not in title


def test_render_title_category_in_title_appends_category_word():
    bundle = presets.load(PRESETS_DIR)
    # Short names so the full form fits in 80.
    title = presets.render_title(
        bundle,
        "photo_10x8",
        "Joe Root",
        field1="Leicester",  # short
        category="Rugby",    # in_title: true
    )
    assert "Leicester Rugby" in title


def test_render_title_drops_category_when_too_long():
    """
    Keane Lewis-Potter + 'Brentford FC' + Football on the A4 framed pattern:
    the full form overflows by 1 char so 'Football' is dropped, and the
    shorter " Brentford FC" form is picked.
    """
    bundle = presets.load(PRESETS_DIR)
    title = presets.render_title(
        bundle,
        "a4_frame_a",
        "Keane Lewis-Potter",
        field1="Brentford FC",
        category="Football",
    )
    assert "Brentford FC" in title
    # 'Football' must not appear as a standalone category suffix.
    assert not title.endswith("Football Autograph")
    assert len(title) <= 80


def test_render_title_drops_field1_when_still_too_long():
    """
    Extreme case: even the '<field1>' form blows the 80-char budget.
    The renderer should fall all the way back to an empty team_suffix
    rather than raising, so the listing still gets a valid (if less
    search-rich) title.
    """
    bundle = presets.load(PRESETS_DIR)
    # A4 Mount A + Keane Lewis-Potter (18) + ' Brighton and Hove Albion' (25)
    # overflows 80 even without a category.
    title = presets.render_title(
        bundle,
        "a4_mount_a",
        "Keane Lewis-Potter",
        field1="Brighton and Hove Albion",
        category="Football",
    )
    assert "Brighton and Hove Albion" not in title
    assert "Keane Lewis-Potter" in title
    assert len(title) <= 80


def test_render_title_appends_memorabilia_and_coa_when_room():
    """
    Short name + short (or no) team_suffix leaves lots of dead space in
    a 10x8 photo title. The filler loop should tack on ' Memorabilia'
    AND ' COA' because both fit inside the 80-char budget.
    """
    bundle = presets.load(PRESETS_DIR)
    title = presets.render_title(bundle, "photo_10x8", "Tim Allen")
    assert title.endswith("Memorabilia COA")
    assert len(title) <= 80


def test_render_title_packs_memorabilia_but_not_coa_when_tight():
    """
    Mid-budget case: Memorabilia fits but there's not enough room left
    for ' COA' as well. Memorabilia goes in (it's the higher-value
    token) and COA is dropped.
    """
    bundle = presets.load(PRESETS_DIR)
    # a4_mount_a base with Hand = 45 chars; " Memorabilia" = 12, " COA" = 4.
    # Name + team_suffix must leave room for Memorabilia (≤ 80-45-12 = 23)
    # but NOT also COA (> 80-45-12-4 = 19). Target name+team ∈ [20, 23].
    title = presets.render_title(
        bundle,
        "a4_mount_a",
        "Mel C",                        # 5
        field1="Rolling Stones",        # " Rolling Stones" = 15 → total 20
        category="Music",               # in_title: false → category dropped
    )
    assert "Rolling Stones" in title
    assert "Hand" in title               # Hand is included — Field1 fit leaves room
    assert "Memorabilia" in title
    assert " COA" not in title
    assert len(title) <= 80


def test_render_title_skips_filler_when_no_room():
    """
    Title already near the 80-char cap: neither filler token fits.
    The rendered title should come back unchanged from the pre-filler form.
    """
    bundle = presets.load(PRESETS_DIR)
    # photo_10x8 + long name + "Brighton and Hove Albion" leaves no room
    # for filler — exercises the trim logic with the post-Display-drop
    # pattern. Uses a signer long enough that both Memorabilia and COA
    # can't both fit.
    title = presets.render_title(
        bundle,
        "photo_10x8",
        "Keane Lewis-Potter",
        field1="Brighton and Hove Albion",
        category="Football",
    )
    assert "Brighton and Hove Albion" in title
    assert len(title) <= 80


def test_render_title_legacy_qualifier_still_works():
    """Back-compat: callers passing `qualifier=` keep working."""
    bundle = presets.load(PRESETS_DIR)
    a = presets.render_title(
        bundle, "photo_10x8", "Mel C", qualifier="Spice Girls"
    )
    b = presets.render_title(
        bundle, "photo_10x8", "Mel C", field1="Spice Girls"
    )
    assert a == b


# --------------------------------------------------------------------------- #
# enrich_specifics_from_knowledge
# --------------------------------------------------------------------------- #

def test_enrich_specifics_football_club_with_full_name():
    bundle = presets.load(PRESETS_DIR)
    out = presets.enrich_specifics_from_knowledge(
        bundle, field1="Man Utd", category="Football"
    )
    assert out["Club"] == "Man Utd"
    assert out["Club (Full)"] == "Manchester United"
    assert "Football" in out["Category Keywords"]
    assert out["Category"] == "Football"


def test_enrich_specifics_no_full_name_when_short_equals_full():
    bundle = presets.load(PRESETS_DIR)
    out = presets.enrich_specifics_from_knowledge(
        bundle, field1="Liverpool", category="Football"
    )
    assert out["Club"] == "Liverpool"
    # Liverpool has no short→full alias in knowledge.yaml, so no "(Full)" row.
    assert "Club (Full)" not in out


def test_enrich_specifics_unknown_category_empty():
    bundle = presets.load(PRESETS_DIR)
    assert presets.enrich_specifics_from_knowledge(
        bundle, field1="Whoever", category="Klingon Opera"
    ) == {}


def test_enrich_specifics_char_limits():
    """Enrichment must respect eBay's 40/65 char limits."""
    bundle = presets.load(PRESETS_DIR)
    out = presets.enrich_specifics_from_knowledge(
        bundle, field1="Man Utd", category="Football"
    )
    for name, value in out.items():
        assert len(name) <= 40, f"{name!r} is {len(name)} > 40"
        assert len(value) <= 65, f"{name}={value!r} is {len(value)} > 65"


# --------------------------------------------------------------------------- #
# build_listing — new knowledge + BestOffer + layered specifics
# --------------------------------------------------------------------------- #

def test_build_listing_from_parsed_filename():
    bundle = presets.load(PRESETS_DIR)
    parsed = ParsedFilename(
        name="Wayne Rooney",
        field1="Man Utd",
        category="Football",
    )
    listing = presets.build_listing(
        bundle,
        product_key="photo_10x8",
        parsed=parsed,
    )
    # Title picked up field1 + category rule from knowledge.
    assert "Wayne Rooney" in listing["title"]
    assert "Man Utd" in listing["title"]
    # Football has in_title:false, so "Football" stays out.
    assert "Football" not in listing["title"]
    # Subject auto-derived from knowledge → football_premier → its category id.
    assert listing["category_id"] == bundle.categories_by_subject["football_premier"]


def test_build_listing_layered_specifics_knowledge_then_caller():
    bundle = presets.load(PRESETS_DIR)
    listing = presets.build_listing(
        bundle,
        product_key="16x12_mount_a",
        name="Alan Hansen",
        field1="Liverpool",
        category="Football",
        item_specifics={
            "Player": "Alan Hansen",
            "Category": "Football Legend",   # caller override
        },
    )
    sp = listing["item_specifics"]
    # Layer 1: defaults.
    assert sp["Signed"] == "Yes"
    # Layer 2: knowledge enrichment.
    assert sp["Club"] == "Liverpool"
    assert "Football" in sp["Category Keywords"]
    # Layer 4: caller wins over knowledge on the "Category" key.
    assert sp["Category"] == "Football Legend"
    assert sp["Player"] == "Alan Hansen"


def test_build_listing_attaches_best_offer_for_99_prices():
    bundle = presets.load(PRESETS_DIR)
    listing = presets.build_listing(
        bundle,
        product_key="photo_10x8",
        name="Tim Allen",
    )
    # Default price is £19.99 — BO enabled, thresholds from the curve.
    bo = listing["best_offer"]
    assert bo is not None
    assert bo["list_price"] == pytest.approx(19.99)
    assert bo["auto_accept"] > 0
    assert bo["min_offer"] == pytest.approx(bo["auto_accept"] - 0.01)


def test_build_listing_no_best_offer_for_round_price_override():
    """A non-.99 price lookup fails soft → best_offer=None."""
    bundle = presets.load(PRESETS_DIR)
    listing = presets.build_listing(
        bundle,
        product_key="16x12_mount_a",
        name="Alan Hansen",
        price_gbp=75.00,
    )
    assert listing["best_offer"] is None
    # Listing still built successfully at the override price.
    assert listing["price_gbp"] == pytest.approx(75.00)


def test_build_listing_no_best_offer_below_threshold():
    """Listings at £14.99 are fixed-price only — no BO block."""
    bundle = presets.load(PRESETS_DIR)
    listing = presets.build_listing(
        bundle,
        product_key="odd_card",
        name="Somebody",
        price_gbp=14.99,
    )
    assert listing["best_offer"] is None


def test_build_listing_requires_name():
    bundle = presets.load(PRESETS_DIR)
    with pytest.raises(presets.PresetsError):
        presets.build_listing(bundle, product_key="photo_10x8")


def test_deep_merge_replaces_scalars_and_lists():
    base = {
        "a": 1,
        "b": {"x": 1, "y": 2},
        "c": [1, 2, 3],
    }
    override = {
        "a": 99,                   # scalar replace
        "b": {"y": 20, "z": 30},   # nested merge
        "c": [9],                  # list REPLACE, not extend
    }
    out = presets._deep_merge(base, override)
    assert out == {
        "a": 99,
        "b": {"x": 1, "y": 20, "z": 30},
        "c": [9],
    }
    # base must not have been mutated
    assert base["a"] == 1
    assert base["b"] == {"x": 1, "y": 2}
    assert base["c"] == [1, 2, 3]
