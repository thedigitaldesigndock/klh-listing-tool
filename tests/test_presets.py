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

REPO_ROOT = Path(__file__).resolve().parent.parent
PRESETS_DIR = REPO_ROOT / "presets"


# --------------------------------------------------------------------------- #
# Loader
# --------------------------------------------------------------------------- #

def test_load_bundle_has_expected_products():
    bundle = presets.load(PRESETS_DIR)
    # Every product type we care about should be present.
    expected = {
        "a4_photo", "10x8_photo", "12x8_photo", "6x4_photo",
        "a4_mount", "10x8_mount", "16x12_mount",
        "a4_frame", "10x8_frame", "16x12_frame",
    }
    assert expected <= set(bundle.products)


def test_load_defaults_contain_core_sections():
    bundle = presets.load(PRESETS_DIR)
    for section in ("marketplace", "listing", "shipping", "return_policy",
                    "item_specifics"):
        assert section in bundle.defaults, f"missing defaults section: {section}"


def test_description_template_has_size_clause_placeholder():
    bundle = presets.load(PRESETS_DIR)
    assert "{size_clause}" in bundle.description_template


def test_product_entries_are_frozen_with_required_fields():
    bundle = presets.load(PRESETS_DIR)
    p = bundle.product("16x12_mount")
    assert p.template_id == "16x12-a-mount"
    assert p.default_price_gbp == pytest.approx(54.99)
    assert "16x12" in p.size_clause
    assert "{name}" in p.title_pattern
    assert "{qualifier_suffix}" in p.title_pattern


def test_unknown_product_key_raises():
    bundle = presets.load(PRESETS_DIR)
    with pytest.raises(presets.PresetsError):
        bundle.product("nonesuch_product")


# --------------------------------------------------------------------------- #
# render_title
# --------------------------------------------------------------------------- #

def test_render_title_basic():
    bundle = presets.load(PRESETS_DIR)
    title = presets.render_title(bundle, "a4_photo", "Tim Allen")
    assert title.startswith("Tim Allen Signed A4 Photo Autograph")
    assert "  " not in title  # no accidental double space from empty qualifier


def test_render_title_with_qualifier():
    bundle = presets.load(PRESETS_DIR)
    title = presets.render_title(bundle, "a4_photo", "Mel C", qualifier="Spice Girls")
    assert " Spice Girls " in title


def test_render_title_empty_qualifier_is_same_as_none():
    bundle = presets.load(PRESETS_DIR)
    a = presets.render_title(bundle, "a4_photo", "Tim Allen", qualifier="")
    b = presets.render_title(bundle, "a4_photo", "Tim Allen", qualifier=None)
    assert a == b


def test_render_title_whitespace_qualifier_is_stripped():
    bundle = presets.load(PRESETS_DIR)
    a = presets.render_title(bundle, "a4_photo", "Tim Allen", qualifier="   ")
    b = presets.render_title(bundle, "a4_photo", "Tim Allen", qualifier=None)
    assert a == b


def test_render_title_raises_if_too_long():
    bundle = presets.load(PRESETS_DIR)
    with pytest.raises(presets.PresetsError):
        presets.render_title(
            bundle,
            "a4_photo",
            "X" * 100,  # force over 80 chars
        )


# --------------------------------------------------------------------------- #
# render_description
# --------------------------------------------------------------------------- #

def test_render_description_substitutes_size_clause():
    bundle = presets.load(PRESETS_DIR)
    html = presets.render_description(bundle, "16x12_mount")
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
        hacked, "a4_photo", extra_placeholders={"player": "Mel C"}
    )
    assert "<p>Player: Mel C</p>" in html


# --------------------------------------------------------------------------- #
# pick_template_id
# --------------------------------------------------------------------------- #

def test_pick_template_id_plain_photo_returns_none():
    bundle = presets.load(PRESETS_DIR)
    assert presets.pick_template_id(bundle, "a4_photo") is None


def test_pick_template_id_uses_product_default():
    bundle = presets.load(PRESETS_DIR)
    assert presets.pick_template_id(bundle, "16x12_mount") == "16x12-a-mount"
    assert presets.pick_template_id(bundle, "a4_mount") == "a4-a-mount"


def test_pick_template_id_orientation_landscape_portrait():
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


def test_pick_template_id_explicit_variant_wins():
    bundle = presets.load(PRESETS_DIR)
    picked = presets.pick_template_id(
        bundle, "16x12_mount", variant="16x12-c-mount"
    )
    assert picked == "16x12-c-mount"


def test_pick_template_id_rejects_unknown_variant():
    bundle = presets.load(PRESETS_DIR)
    with pytest.raises(presets.PresetsError):
        presets.pick_template_id(
            bundle, "16x12_mount", variant="16x12-z-mount"
        )


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
        product_key="a4_photo",
        name="Tim Allen",
        subject="film_tv",
    )
    # Core fields
    assert listing["product_key"] == "a4_photo"
    assert listing["template_id"] is None
    assert listing["title"].startswith("Tim Allen Signed A4 Photo")
    assert "{size_clause}" not in listing["description_html"]
    assert listing["price_gbp"] == pytest.approx(29.99)
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


def test_build_listing_price_override():
    bundle = presets.load(PRESETS_DIR)
    listing = presets.build_listing(
        bundle,
        product_key="16x12_mount",
        name="Alan Hansen",
        price_gbp=75.00,
    )
    assert listing["price_gbp"] == pytest.approx(75.00)


def test_build_listing_item_specifics_merged():
    bundle = presets.load(PRESETS_DIR)
    listing = presets.build_listing(
        bundle,
        product_key="16x12_mount",
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
        product_key="a4_photo",
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
# _deep_merge unit test
# --------------------------------------------------------------------------- #

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
