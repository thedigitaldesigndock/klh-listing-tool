"""
Tests for pipeline.lister — XML builder and safety guards.

These are ALL offline tests. Anything that would hit the Trading API
(upload_site_hosted_picture, verify_listing, submit_listing,
schedule_listing, end_listing) is NOT exercised here — those live in
integration tests you run by hand against the sandbox / production
when you're ready.

What IS tested:
    - build_add_item_xml produces well-formed XML that parses
    - required fields surface as <tag>value</tag>
    - seller profiles, picture URLs, item specifics render correctly
    - description HTML is wrapped in CDATA (not escaped)
    - XML-unsafe characters in title / specifics are escaped
    - schedule_time must be +15min..+21days
    - submit_listing / schedule_listing / end_listing refuse without confirm
    - cap violations (title length, picture count) raise
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import xml.etree.ElementTree as ET
import pytest

from pipeline import lister, presets

REPO_ROOT = Path(__file__).resolve().parent.parent
PRESETS_DIR = REPO_ROOT / "presets"

NS = "urn:ebay:apis:eBLBaseComponents"
NS_MAP = {"e": NS}


def _wrap(inner_xml: str) -> ET.Element:
    """Parse the inner XML by wrapping in the AddFixedPriceItemRequest envelope."""
    envelope = (
        '<?xml version="1.0"?>'
        f'<AddFixedPriceItemRequest xmlns="{NS}">{inner_xml}</AddFixedPriceItemRequest>'
    )
    return ET.fromstring(envelope)


def _simple_listing(**overrides):
    bundle = presets.load(PRESETS_DIR)
    return presets.build_listing(
        bundle,
        product_key="16x12_mount_a",
        name="Alan Hansen",
        subject="football_retired",
        item_specifics={"Player": "Alan Hansen", "Team": "Liverpool"},
        **overrides,
    )


# --------------------------------------------------------------------------- #
# Core XML shape
# --------------------------------------------------------------------------- #

def test_build_add_item_xml_parses_and_has_item_root():
    listing = _simple_listing()
    inner = lister.build_add_item_xml(
        listing, ["https://i.ebayimg.com/fake1.jpg"]
    )
    root = _wrap(inner)
    item = root.find("e:Item", NS_MAP)
    assert item is not None


def test_build_add_item_xml_required_fields_present():
    listing = _simple_listing()
    inner = lister.build_add_item_xml(listing, ["https://i.ebayimg.com/x.jpg"])
    item = _wrap(inner).find("e:Item", NS_MAP)

    assert item.findtext("e:Title", namespaces=NS_MAP).startswith("Alan Hansen")
    assert item.findtext("e:PrimaryCategory/e:CategoryID", namespaces=NS_MAP) == "97085"
    assert item.findtext("e:StartPrice", namespaces=NS_MAP) == "49.99"
    assert item.findtext("e:ConditionID", namespaces=NS_MAP) == "1000"
    assert item.findtext("e:Country", namespaces=NS_MAP) == "GB"
    assert item.findtext("e:Currency", namespaces=NS_MAP) == "GBP"
    assert item.findtext("e:ListingDuration", namespaces=NS_MAP) == "GTC"
    assert item.findtext("e:ListingType", namespaces=NS_MAP) == "FixedPriceItem"
    assert item.findtext("e:Location", namespaces=NS_MAP) == "Manchester, Lancashire"
    assert item.findtext("e:PostalCode", namespaces=NS_MAP) == "M29 8DL"
    assert item.findtext("e:Quantity", namespaces=NS_MAP) == "1"
    assert item.findtext("e:DispatchTimeMax", namespaces=NS_MAP) == "1"
    assert item.findtext("e:Site", namespaces=NS_MAP) == "UK"


def test_seller_profiles_block_rendered():
    listing = _simple_listing()
    inner = lister.build_add_item_xml(listing, ["https://x/1.jpg"])
    item = _wrap(inner).find("e:Item", NS_MAP)
    sp = item.find("e:SellerProfiles", NS_MAP)
    assert sp is not None
    assert sp.findtext("e:SellerPaymentProfile/e:PaymentProfileID",
                      namespaces=NS_MAP) == "226381763024"
    assert sp.findtext("e:SellerReturnProfile/e:ReturnProfileID",
                      namespaces=NS_MAP) == "226381757024"
    assert sp.findtext("e:SellerShippingProfile/e:ShippingProfileID",
                      namespaces=NS_MAP) == "226588406024"


def test_picture_details_rendered_in_order():
    listing = _simple_listing()
    urls = [f"https://i.ebayimg.com/pic{i}.jpg" for i in range(1, 5)]
    inner = lister.build_add_item_xml(listing, urls)
    item = _wrap(inner).find("e:Item", NS_MAP)
    pd = item.find("e:PictureDetails", NS_MAP)
    assert pd.findtext("e:GalleryType", namespaces=NS_MAP) == "Gallery"
    rendered = [e.text for e in pd.findall("e:PictureURL", NS_MAP)]
    assert rendered == urls


def test_item_specifics_merged_and_sorted():
    listing = _simple_listing()
    inner = lister.build_add_item_xml(listing, ["https://x/1.jpg"])
    item = _wrap(inner).find("e:Item", NS_MAP)
    specifics_el = item.find("e:ItemSpecifics", NS_MAP)
    names = [nvl.findtext("e:Name", namespaces=NS_MAP)
             for nvl in specifics_el.findall("e:NameValueList", NS_MAP)]
    # Defaults + callers = both present
    assert "Country of Origin" in names
    assert "Signed" in names
    assert "Player" in names
    assert "Team" in names
    # Sorted alphabetically
    assert names == sorted(names)


def test_description_wrapped_in_cdata_not_escaped():
    listing = _simple_listing()
    inner = lister.build_add_item_xml(listing, ["https://x/1.jpg"])
    # The HTML body contains <font>, <p> etc. which must NOT be escaped
    # to &lt;font&gt; — it must be inside a <![CDATA[...]]> block.
    assert "<![CDATA[" in inner
    assert "]]>" in inner
    assert "&lt;font" not in inner       # not double-escaped
    assert "&lt;p " not in inner


def test_unsafe_characters_in_title_are_escaped():
    # We build a listing manually with a title containing <, >, &.
    listing = _simple_listing()
    listing["title"] = "Bob & Co <signed> 'photo' \"wow\""
    inner = lister.build_add_item_xml(listing, ["https://x/1.jpg"])
    # Must still parse
    item = _wrap(inner).find("e:Item", NS_MAP)
    # And when parsed, the text comes back out exactly as we set it
    assert item.findtext("e:Title", namespaces=NS_MAP) == \
        "Bob & Co <signed> 'photo' \"wow\""


def test_storefront_store_category_rendered_when_set():
    """StoreCategoryID routes listings into Kim's shop bucket."""
    listing = _simple_listing()  # 16x12_mount_a + football_retired
    # football_retired subject doesn't map a store category via knowledge
    # lookup (lookup keys by *category name* not subject), so poke it
    # directly to simulate the happy-path plumbing.
    listing["store_category_id"] = 1954551013
    inner = lister.build_add_item_xml(listing, ["https://x/1.jpg"])
    item = _wrap(inner).find("e:Item", NS_MAP)
    sf = item.find("e:Storefront", NS_MAP)
    assert sf is not None
    assert sf.findtext("e:StoreCategoryID", namespaces=NS_MAP) == "1954551013"


def test_storefront_omitted_when_store_category_none():
    listing = _simple_listing()
    listing["store_category_id"] = None
    inner = lister.build_add_item_xml(listing, ["https://x/1.jpg"])
    item = _wrap(inner).find("e:Item", NS_MAP)
    assert item.find("e:Storefront", NS_MAP) is None


def test_vat_details_rendered_at_20_percent():
    """Kim is VAT-registered; every listing carries VATPercent=20.0."""
    listing = _simple_listing()
    inner = lister.build_add_item_xml(listing, ["https://x/1.jpg"])
    item = _wrap(inner).find("e:Item", NS_MAP)
    vd = item.find("e:VATDetails", NS_MAP)
    assert vd is not None
    assert vd.findtext("e:VATPercent", namespaces=NS_MAP) == "20.0"


def test_vat_details_omitted_when_vat_percent_none():
    listing = _simple_listing()
    listing["vat_percent"] = None
    inner = lister.build_add_item_xml(listing, ["https://x/1.jpg"])
    item = _wrap(inner).find("e:Item", NS_MAP)
    assert item.find("e:VATDetails", NS_MAP) is None


def test_sku_rendered_when_set():
    listing = _simple_listing(sku="KLH-AH-16X12-001")
    inner = lister.build_add_item_xml(listing, ["https://x/1.jpg"])
    item = _wrap(inner).find("e:Item", NS_MAP)
    assert item.findtext("e:SKU", namespaces=NS_MAP) == "KLH-AH-16X12-001"


def test_quantity_override_rendered():
    """
    build_listing(quantity=N) must override defaults.yaml's quantity:1
    and surface in the XML — Kim uses this when she has multiples of
    the same signed item ready to ship (e.g. 3 duplicate photos).
    """
    listing = _simple_listing(quantity=3)
    inner = lister.build_add_item_xml(listing, ["https://x/1.jpg"])
    item = _wrap(inner).find("e:Item", NS_MAP)
    assert item.findtext("e:Quantity", namespaces=NS_MAP) == "3"


def test_quantity_defaults_to_one_when_not_specified():
    listing = _simple_listing()
    inner = lister.build_add_item_xml(listing, ["https://x/1.jpg"])
    item = _wrap(inner).find("e:Item", NS_MAP)
    assert item.findtext("e:Quantity", namespaces=NS_MAP) == "1"


def test_sku_omitted_when_not_set():
    listing = _simple_listing()
    assert listing.get("sku") is None
    inner = lister.build_add_item_xml(listing, ["https://x/1.jpg"])
    assert "<SKU>" not in inner


# --------------------------------------------------------------------------- #
# Best Offer details
# --------------------------------------------------------------------------- #

def test_best_offer_block_rendered_for_enabled_listing():
    """
    A listing built from presets with a .99 price above £15.99 carries
    a best_offer dict. build_add_item_xml must emit BestOfferDetails +
    ListingDetails with the auto-accept / minimum thresholds.
    """
    # 16x12_mount_a defaults to £54.99 → BO enabled, thresholds from curve
    listing = _simple_listing()
    assert listing["best_offer"] is not None  # sanity

    inner = lister.build_add_item_xml(listing, ["https://x/1.jpg"])
    item = _wrap(inner).find("e:Item", NS_MAP)

    bo = item.find("e:BestOfferDetails", NS_MAP)
    assert bo is not None
    assert bo.findtext("e:BestOfferEnabled", namespaces=NS_MAP) == "true"

    ld = item.find("e:ListingDetails", NS_MAP)
    assert ld is not None
    accept_el = ld.find("e:BestOfferAutoAcceptPrice", NS_MAP)
    min_el = ld.find("e:MinimumBestOfferPrice", NS_MAP)
    assert accept_el is not None and min_el is not None
    assert accept_el.attrib["currencyID"] == "GBP"
    assert min_el.attrib["currencyID"] == "GBP"
    # Values come from the attached best_offer dict.
    assert float(accept_el.text) == pytest.approx(listing["best_offer"]["auto_accept"])
    assert float(min_el.text)    == pytest.approx(listing["best_offer"]["min_offer"])


def test_best_offer_block_omitted_when_none():
    """
    A listing with best_offer=None (e.g. sub-£14.99 card) must NOT emit
    a BestOfferDetails block — eBay's default for fixed-price items is
    BO disabled, so silence is correct.
    """
    listing = _simple_listing()
    listing["best_offer"] = None
    inner = lister.build_add_item_xml(listing, ["https://x/1.jpg"])
    assert "<BestOfferDetails>" not in inner
    assert "<BestOfferAutoAcceptPrice" not in inner


# --------------------------------------------------------------------------- #
# Schedule time
# --------------------------------------------------------------------------- #

def test_schedule_time_rendered_in_future():
    listing = _simple_listing()
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    inner = lister.build_add_item_xml(
        listing, ["https://x/1.jpg"], schedule_time=future
    )
    item = _wrap(inner).find("e:Item", NS_MAP)
    sched = item.findtext("e:ScheduleTime", namespaces=NS_MAP)
    assert sched is not None
    assert sched.endswith("Z")
    assert "T" in sched


def test_schedule_time_too_soon_raises():
    listing = _simple_listing()
    soon = datetime.now(timezone.utc) + timedelta(minutes=5)
    with pytest.raises(lister.ListerError, match="at least"):
        lister.build_add_item_xml(
            listing, ["https://x/1.jpg"], schedule_time=soon
        )


def test_schedule_time_too_far_raises():
    listing = _simple_listing()
    far = datetime.now(timezone.utc) + timedelta(days=30)
    with pytest.raises(lister.ListerError, match="at most"):
        lister.build_add_item_xml(
            listing, ["https://x/1.jpg"], schedule_time=far
        )


# --------------------------------------------------------------------------- #
# Validation / caps
# --------------------------------------------------------------------------- #

def test_title_over_80_chars_raises():
    listing = _simple_listing()
    listing["title"] = "X" * 81
    with pytest.raises(lister.ListerError, match="80"):
        lister.build_add_item_xml(listing, ["https://x/1.jpg"])


def test_missing_pictures_raises():
    listing = _simple_listing()
    with pytest.raises(lister.ListerError, match="picture URL"):
        lister.build_add_item_xml(listing, [])


def test_too_many_pictures_raises():
    listing = _simple_listing()
    urls = [f"https://x/{i}.jpg" for i in range(lister.MAX_PICTURES + 1)]
    with pytest.raises(lister.ListerError, match="cap"):
        lister.build_add_item_xml(listing, urls)


def test_missing_seller_profile_ids_raises():
    listing = _simple_listing()
    listing["seller_profiles"] = {
        "payment_profile_id": "123",
        # return + shipping missing
    }
    with pytest.raises(lister.ListerError, match="seller_profiles missing"):
        lister.build_add_item_xml(listing, ["https://x/1.jpg"])


def test_missing_category_raises():
    listing = _simple_listing()
    listing["category_id"] = None
    with pytest.raises(lister.ListerError, match="category_id"):
        lister.build_add_item_xml(listing, ["https://x/1.jpg"])


# --------------------------------------------------------------------------- #
# Safety guards on live-write helpers
# --------------------------------------------------------------------------- #

def test_submit_listing_refuses_without_confirm():
    listing = _simple_listing()
    with pytest.raises(lister.ListerError, match="confirm=True"):
        lister.submit_listing(listing, ["https://x/1.jpg"])


def test_schedule_listing_refuses_without_confirm():
    listing = _simple_listing()
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    with pytest.raises(lister.ListerError, match="confirm=True"):
        lister.schedule_listing(listing, ["https://x/1.jpg"], future)


def test_end_listing_refuses_without_confirm():
    with pytest.raises(lister.ListerError, match="confirm=True"):
        lister.end_listing("267507152141")
