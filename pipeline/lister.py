"""
eBay Trading API lister — turns a `pipeline.presets.build_listing()` dict
into `AddFixedPriceItem` / `VerifyAddFixedPriceItem` XML and drives the
full list-a-thing workflow.

Design constraints (set by the business):

* Kim's account is opted into Business Policies, so every item MUST
  reference payment/return/shipping profile IDs. We do NOT send inline
  ShippingDetails or ReturnPolicy — eBay rejects that when profiles
  are active. Profile IDs live in presets/defaults.yaml under
  `seller_profiles:`.
* First-run safety: nothing goes live by accident. The CLI defaults to
  `verify` (dry-run via VerifyAddFixedPriceItem). Actually creating a
  listing requires either `submit --confirm` (live immediately) or
  `schedule --at <iso>` (hidden until the ScheduleTime).
* Description HTML is wrapped in CDATA so Kim's formatting survives
  intact — no need to re-escape every `<span>`/`<font>` tag.

Flow:
    1.  pipeline.presets.build_listing(...) → dict
    2.  pipeline.lister.upload_site_hosted_picture(path) → EPS URL
        (repeat for every picture you want on the listing)
    3a. pipeline.lister.verify_listing(listing, urls) → fees/warnings dict
    3b. pipeline.lister.submit_listing(listing, urls, confirm=True) →
            {item_id, fees, warnings}
    3c. pipeline.lister.schedule_listing(listing, urls, schedule_time)

`end_listing(item_id)` ends an active listing via EndFixedPriceItem —
the "oh crap unlist that" escape hatch.

Everything here is stdlib-only; the XML payload is built as a raw
string because eBay Trading expects very specific element ordering and
xml.etree doesn't do CDATA.
"""

from __future__ import annotations

import base64
import mimetypes
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import xml.etree.ElementTree as ET

from ebay_api.trading import NS, NS_MAP, TradingError, trading_call

# --------------------------------------------------------------------------- #
# Limits / constants
# --------------------------------------------------------------------------- #

MAX_TITLE_LEN = 80               # eBay hard cap
MAX_PICTURES = 24                # Trading API hard cap
MIN_SCHEDULE_MINUTES = 15        # earliest ScheduleTime offset eBay allows
MAX_SCHEDULE_DAYS = 21           # latest


class ListerError(RuntimeError):
    """Raised when the listing dict / picture URLs are unusable."""


# --------------------------------------------------------------------------- #
# XML helpers
# --------------------------------------------------------------------------- #

def _xml_escape(text: str) -> str:
    """Minimal XML text escape — used for title, item specifics, etc."""
    return (
        text.replace("&",  "&amp;")
            .replace("<",  "&lt;")
            .replace(">",  "&gt;")
            .replace('"',  "&quot;")
            .replace("'",  "&apos;")
    )


def _cdata(html: str) -> str:
    """Wrap HTML in a CDATA section, escaping any embedded `]]>`."""
    safe = html.replace("]]>", "]]]]><![CDATA[>")
    return f"<![CDATA[{safe}]]>"


def _el(tag: str, value: Any) -> str:
    """<tag>escaped-value</tag>"""
    return f"<{tag}>{_xml_escape(str(value))}</{tag}>"


# --------------------------------------------------------------------------- #
# Sub-block builders
# --------------------------------------------------------------------------- #

def _picture_details_xml(urls: list[str]) -> str:
    if not urls:
        raise ListerError("at least one picture URL is required")
    if len(urls) > MAX_PICTURES:
        raise ListerError(
            f"{len(urls)} pictures exceeds eBay's cap of {MAX_PICTURES}"
        )
    parts = ["<PictureDetails>", "<GalleryType>Gallery</GalleryType>"]
    for u in urls:
        parts.append(_el("PictureURL", u))
    parts.append("</PictureDetails>")
    return "".join(parts)


def _item_specifics_xml(specifics: dict[str, str]) -> str:
    if not specifics:
        return ""
    parts = ["<ItemSpecifics>"]
    # Sort for determinism — eBay doesn't care about order but tests do.
    for name, value in sorted(specifics.items()):
        parts.append(
            "<NameValueList>"
            f"{_el('Name', name)}"
            f"{_el('Value', value)}"
            "</NameValueList>"
        )
    parts.append("</ItemSpecifics>")
    return "".join(parts)


def _best_offer_xml(bo: Optional[dict], currency: str) -> str:
    """
    Emit the `<BestOfferDetails>` + `<ListingDetails>` pair for a listing
    that has BestOffer thresholds attached. Returns an empty string for
    listings with `best_offer=None` (no-BO fixed price).

    Shape — matches pipeline.offers.build_best_offer_xml so we stay
    consistent with the reference implementation:

        <BestOfferDetails>
          <BestOfferEnabled>true</BestOfferEnabled>
        </BestOfferDetails>
        <ListingDetails>
          <BestOfferAutoAcceptPrice currencyID="GBP">15.00</BestOfferAutoAcceptPrice>
          <MinimumBestOfferPrice    currencyID="GBP">14.99</MinimumBestOfferPrice>
        </ListingDetails>
    """
    if not bo:
        return ""
    try:
        accept = float(bo["auto_accept"])
        min_offer = float(bo["min_offer"])
    except (KeyError, TypeError, ValueError) as e:
        raise ListerError(
            f"best_offer block is malformed: {bo!r} ({e})"
        ) from e
    return (
        "<BestOfferDetails>"
        "<BestOfferEnabled>true</BestOfferEnabled>"
        "</BestOfferDetails>"
        "<ListingDetails>"
        f'<BestOfferAutoAcceptPrice currencyID="{currency}">'
        f"{accept:.2f}"
        f"</BestOfferAutoAcceptPrice>"
        f'<MinimumBestOfferPrice currencyID="{currency}">'
        f"{min_offer:.2f}"
        f"</MinimumBestOfferPrice>"
        "</ListingDetails>"
    )


def _seller_profiles_xml(profiles: dict[str, str]) -> str:
    """
    Emit the SellerProfiles block. All three profile IDs are required
    when Business Policies are on — missing any one means the listing
    will be rejected.
    """
    missing = [
        k for k in ("payment_profile_id", "return_profile_id", "shipping_profile_id")
        if not profiles.get(k)
    ]
    if missing:
        raise ListerError(
            f"seller_profiles missing required IDs: {', '.join(missing)}. "
            "Check presets/defaults.yaml."
        )
    return (
        "<SellerProfiles>"
        "<SellerPaymentProfile>"
        f"{_el('PaymentProfileID', profiles['payment_profile_id'])}"
        "</SellerPaymentProfile>"
        "<SellerReturnProfile>"
        f"{_el('ReturnProfileID', profiles['return_profile_id'])}"
        "</SellerReturnProfile>"
        "<SellerShippingProfile>"
        f"{_el('ShippingProfileID', profiles['shipping_profile_id'])}"
        "</SellerShippingProfile>"
        "</SellerProfiles>"
    )


def _format_schedule_time(dt: datetime) -> str:
    """
    eBay ScheduleTime must be ISO 8601 UTC with trailing 'Z' and
    between +15 min and +21 days from now. Naive datetimes are assumed
    to be in local time and converted.
    """
    if dt.tzinfo is None:
        dt = dt.astimezone()             # assume local wall time
    dt_utc = dt.astimezone(timezone.utc)

    now_utc = datetime.now(timezone.utc)
    delta = dt_utc - now_utc
    if delta < timedelta(minutes=MIN_SCHEDULE_MINUTES):
        raise ListerError(
            f"ScheduleTime must be at least {MIN_SCHEDULE_MINUTES} minutes "
            f"in the future (got {delta})"
        )
    if delta > timedelta(days=MAX_SCHEDULE_DAYS):
        raise ListerError(
            f"ScheduleTime must be at most {MAX_SCHEDULE_DAYS} days "
            f"in the future (got {delta})"
        )
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")


# --------------------------------------------------------------------------- #
# Full AddFixedPriceItem builder
# --------------------------------------------------------------------------- #

def build_add_item_xml(
    listing: dict,
    picture_urls: list[str],
    *,
    schedule_time: Optional[datetime] = None,
) -> str:
    """
    Build the inner XML for AddFixedPriceItem / VerifyAddFixedPriceItem.

    Returns the `<Item>...</Item>` payload ready to pass to
    ebay_api.trading.trading_call(). The wrapping
    `<AddFixedPriceItemRequest>` envelope is added by trading_call.
    """
    # ---- Sanity checks (fail loud and early) ---------------------------
    if not listing.get("title"):
        raise ListerError("listing has no title")
    if len(listing["title"]) > MAX_TITLE_LEN:
        raise ListerError(
            f"title is {len(listing['title'])} chars (>{MAX_TITLE_LEN})"
        )
    if not listing.get("description_html"):
        raise ListerError("listing has no description_html")
    if not listing.get("category_id"):
        raise ListerError("listing has no category_id")
    if listing.get("price_gbp") is None:
        raise ListerError("listing has no price_gbp")

    marketplace = listing.get("marketplace") or {}
    listing_cfg = listing.get("listing") or {}
    profiles    = listing.get("seller_profiles") or {}
    specifics   = listing.get("item_specifics") or {}

    country  = marketplace.get("country")  or "GB"
    currency = marketplace.get("currency") or "GBP"
    location = marketplace.get("location") or ""
    postal   = marketplace.get("postal_code")

    site           = marketplace.get("site") or "EBAY_GB"  # informational only
    listing_type   = listing_cfg.get("listing_type") or "FixedPriceItem"
    duration       = listing_cfg.get("listing_duration") or "GTC"
    condition_id   = listing_cfg.get("condition_id") or 1000
    quantity       = listing_cfg.get("quantity") or 1
    dispatch_max   = listing_cfg.get("dispatch_time_max") or 1

    parts: list[str] = ["<Item>"]
    parts.append(_el("Title", listing["title"]))
    parts.append(f"<Description>{_cdata(listing['description_html'])}</Description>")
    parts.append("<PrimaryCategory>")
    parts.append(_el("CategoryID", listing["category_id"]))
    parts.append("</PrimaryCategory>")
    parts.append(_el("StartPrice", f"{float(listing['price_gbp']):.2f}"))
    parts.append(_el("ConditionID", condition_id))
    parts.append(_el("Country", country))
    parts.append(_el("Currency", currency))
    parts.append(_el("ListingDuration", duration))
    parts.append(_el("ListingType", listing_type))
    if location:
        parts.append(_el("Location", location))
    if postal:
        parts.append(_el("PostalCode", postal))
    parts.append(_el("Quantity", quantity))
    parts.append(_el("DispatchTimeMax", dispatch_max))
    if listing.get("sku"):
        parts.append(_el("SKU", listing["sku"]))
    parts.append(_el("Site", _site_to_trading_name(site)))

    # Pictures (hosted on EPS, URLs from upload_site_hosted_picture)
    parts.append(_picture_details_xml(picture_urls))

    # Item specifics
    parts.append(_item_specifics_xml(specifics))

    # Best Offer thresholds (optional — skipped for no-BO listings)
    parts.append(_best_offer_xml(listing.get("best_offer"), currency))

    # Business Policies
    parts.append(_seller_profiles_xml(profiles))

    # Scheduled start (optional)
    if schedule_time is not None:
        parts.append(_el("ScheduleTime", _format_schedule_time(schedule_time)))

    parts.append("</Item>")
    return "".join(parts)


def _site_to_trading_name(site_env: str) -> str:
    """
    Map the env-style site name (EBAY_GB, EBAY_US) used in config.yaml
    to the Trading API's Site enum (UK, US, Ireland).
    """
    return {
        "EBAY_GB": "UK",
        "EBAY_US": "US",
        "EBAY_IE": "Ireland",
    }.get(site_env.upper(), "UK")


# --------------------------------------------------------------------------- #
# Response parsing
# --------------------------------------------------------------------------- #

def _text(elem: Optional[ET.Element], path: str) -> Optional[str]:
    if elem is None:
        return None
    found = elem.find(path, NS_MAP)
    return found.text if found is not None else None


def _parse_add_item_response(root: ET.Element) -> dict[str, Any]:
    """
    Extract the fields we care about from an AddFixedPriceItem /
    VerifyAddFixedPriceItem response.

    Shape:
        {
          "item_id": "123456789" | None,   # None for Verify
          "start_time": "2026-04-10T..." | None,
          "end_time":   "..." | None,
          "fees": [ {name, amount, currency}, ... ],
          "warnings": [ {short, long, code}, ... ],
          "ack": "Success" | "Warning",
        }
    """
    out: dict[str, Any] = {
        "item_id":    _text(root, "e:ItemID"),
        "start_time": _text(root, "e:StartTime"),
        "end_time":   _text(root, "e:EndTime"),
        "ack":        _text(root, "e:Ack"),
        "fees":       [],
        "warnings":   [],
    }

    fees_el = root.find("e:Fees", NS_MAP)
    if fees_el is not None:
        for fee in fees_el.findall("e:Fee", NS_MAP):
            out["fees"].append({
                "name":     _text(fee, "e:Name"),
                "amount":   _text(fee, "e:Fee"),
                "currency": fee.find("e:Fee", NS_MAP).attrib.get("currencyID")
                            if fee.find("e:Fee", NS_MAP) is not None else None,
            })

    # Ack == "Warning" means the listing WAS created/verified but eBay
    # has notes (e.g. "title too similar to existing listing").
    for err in root.findall("e:Errors", NS_MAP):
        out["warnings"].append({
            "short":    _text(err, "e:ShortMessage"),
            "long":     _text(err, "e:LongMessage"),
            "code":     _text(err, "e:ErrorCode"),
            "severity": _text(err, "e:SeverityCode"),
        })
    return out


# --------------------------------------------------------------------------- #
# High-level actions
# --------------------------------------------------------------------------- #

def verify_listing(
    listing: dict,
    picture_urls: list[str],
) -> dict[str, Any]:
    """
    Dry-run a listing via VerifyAddFixedPriceItem.

    This runs every validation eBay would run on AddFixedPriceItem and
    returns the exact same fees/warnings — without creating anything.
    Use it before submit_listing() to catch missing specifics, bad
    categories, or pictures that failed to upload.
    """
    inner = build_add_item_xml(listing, picture_urls)
    root = trading_call("VerifyAddFixedPriceItem", inner)
    return _parse_add_item_response(root)


def submit_listing(
    listing: dict,
    picture_urls: list[str],
    *,
    confirm: bool = False,
) -> dict[str, Any]:
    """
    Actually create the listing via AddFixedPriceItem.

    `confirm=True` is required — without it this function raises. Keeps
    accidental `pipeline.lister.submit_listing(...)` calls in the REPL
    from pushing live listings by mistake.
    """
    if not confirm:
        raise ListerError(
            "submit_listing requires confirm=True. "
            "Did you mean verify_listing()?"
        )
    inner = build_add_item_xml(listing, picture_urls)
    root = trading_call("AddFixedPriceItem", inner)
    return _parse_add_item_response(root)


def schedule_listing(
    listing: dict,
    picture_urls: list[str],
    schedule_time: datetime,
    *,
    confirm: bool = False,
) -> dict[str, Any]:
    """
    Create a scheduled listing (hidden until schedule_time).

    Same safety rule as submit_listing — `confirm=True` is required.
    schedule_time must be between +15 min and +21 days from now.
    """
    if not confirm:
        raise ListerError(
            "schedule_listing requires confirm=True. "
            "Did you mean verify_listing()?"
        )
    inner = build_add_item_xml(
        listing, picture_urls, schedule_time=schedule_time
    )
    root = trading_call("AddFixedPriceItem", inner)
    return _parse_add_item_response(root)


def end_listing(
    item_id: str,
    *,
    reason: str = "NotAvailable",
    confirm: bool = False,
) -> dict[str, Any]:
    """
    End an active fixed-price listing via EndFixedPriceItem.

    Valid reasons: Incorrect, LostOrBroken, NotAvailable, OtherListingError,
    SellToHighBidder, Sold. 'NotAvailable' is the safe default.
    """
    if not confirm:
        raise ListerError("end_listing requires confirm=True")
    inner = (
        f"{_el('ItemID', item_id)}"
        f"{_el('EndingReason', reason)}"
    )
    root = trading_call("EndFixedPriceItem", inner)
    return {
        "item_id":  item_id,
        "end_time": _text(root, "e:EndTime"),
        "ack":      _text(root, "e:Ack"),
    }


# --------------------------------------------------------------------------- #
# Revise (audit tool) — ReviseFixedPriceItem
# --------------------------------------------------------------------------- #
#
# Used by `klh audit apply` to patch existing listings in place. The
# golden rule: send the MINIMUM payload possible.
#
#   * Title is independent — send <Title> only.
#   * Item specifics REPLACE the whole block — if you send <ItemSpecifics>,
#     eBay blows away the existing specifics and writes what you sent.
#     Callers must therefore merge `current` + `changes` before calling.
#     We expose a `new_specifics_replace` param that takes the full merged
#     dict, and an ergonomic `new_specifics_merge` helper that does the
#     merge for you given the current specifics from the cache.
#   * Pictures / Description / Price are out of scope for audit edits.
#     Touching them risks search-ranking resets. The function refuses.
#

def _revise_specifics_xml(specifics: dict[str, str]) -> str:
    """
    Build the <ItemSpecifics> block for a Revise call. Unlike the
    Add-side _item_specifics_xml() above, this always emits the block
    (even if empty) because "empty = clear all specifics" is a valid
    intent. Callers that want a no-op should pass new_specifics_replace=None.
    """
    parts = ["<ItemSpecifics>"]
    for name, value in sorted(specifics.items()):
        parts.append(
            "<NameValueList>"
            f"{_el('Name', name)}"
            f"{_el('Value', value)}"
            "</NameValueList>"
        )
    parts.append("</ItemSpecifics>")
    return "".join(parts)


def build_revise_item_xml(
    item_id: str,
    *,
    new_title: Optional[str] = None,
    new_specifics_replace: Optional[dict[str, str]] = None,
) -> str:
    """
    Build the inner XML for ReviseFixedPriceItem. Only fields explicitly
    provided are included — eBay treats unsent elements as "leave alone".

    Exactly one of new_title / new_specifics_replace must be truthy
    (otherwise there's nothing to revise).
    """
    if not item_id:
        raise ListerError("revise: item_id is required")
    if new_title is None and new_specifics_replace is None:
        raise ListerError(
            "revise: nothing to change — pass new_title or new_specifics_replace"
        )
    if new_title is not None:
        if not new_title:
            raise ListerError("revise: new_title must be non-empty")
        if len(new_title) > MAX_TITLE_LEN:
            raise ListerError(
                f"revise: new_title is {len(new_title)} chars (>{MAX_TITLE_LEN})"
            )

    parts: list[str] = ["<Item>"]
    parts.append(_el("ItemID", item_id))
    if new_title is not None:
        parts.append(_el("Title", new_title))
    if new_specifics_replace is not None:
        parts.append(_revise_specifics_xml(new_specifics_replace))
    parts.append("</Item>")
    return "".join(parts)


def merge_specifics(
    current: dict[str, str],
    changes: dict[str, Optional[str]],
) -> dict[str, str]:
    """
    Merge a map of proposed changes into a current specifics dict.
    A value of None in `changes` deletes the key; any other value
    overwrites or adds it. Returns a new dict (doesn't mutate input).
    """
    merged = dict(current)
    for k, v in changes.items():
        if v is None:
            merged.pop(k, None)
        else:
            merged[k] = str(v)
    return merged


def revise_listing(
    item_id: str,
    *,
    new_title: Optional[str] = None,
    new_specifics_replace: Optional[dict[str, str]] = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """
    Revise an existing fixed-price listing via ReviseFixedPriceItem.

    `confirm=True` is required — same safety pattern as submit_listing().
    Trading has no VerifyReviseFixedPriceItem, so the caller's "dry run"
    is just building the XML and printing a diff without calling this.

    Returns {item_id, ack, warnings, fees}.
    """
    if not confirm:
        raise ListerError(
            "revise_listing requires confirm=True. "
            "For a dry-run, build the XML with build_revise_item_xml() "
            "and print it without invoking this function."
        )
    inner = build_revise_item_xml(
        item_id,
        new_title=new_title,
        new_specifics_replace=new_specifics_replace,
    )
    root = trading_call("ReviseFixedPriceItem", inner)
    return {
        "item_id":  item_id,
        "ack":      _text(root, "e:Ack"),
        "fees":     [],  # Revise returns fees but they're almost always zero
        "warnings": [
            {
                "short": _text(err, "e:ShortMessage"),
                "long":  _text(err, "e:LongMessage"),
                "code":  _text(err, "e:ErrorCode"),
            }
            for err in root.findall("e:Errors", NS_MAP)
        ],
    }


# --------------------------------------------------------------------------- #
# Out-of-stock control (SetUserPreferences + ReviseInventoryStatus)
# --------------------------------------------------------------------------- #
#
# Kim's business goal: when a 1-of-1 signed item sells, we want the
# listing to STAY (hidden from search) rather than end, so we can
# simply restock it with a newly-signed item of the same person and
# keep all the accrued search history, watchers, and item ID. This
# requires two things:
#
#   1. Seller account opt-in to OutOfStockControl via SetUserPreferences.
#      Done once per account; persistent.
#   2. ReviseInventoryStatus to change Quantity on a specific item
#      without having to call ReviseFixedPriceItem (which is heavier
#      and can re-verify / throw away search ranking).
#
# CLI:
#   klh preferences out-of-stock-control --enable
#   klh preferences out-of-stock-control --status
#   klh outofstock <item_id>          → sets Quantity=0
#   klh restock    <item_id> [--qty N]   → sets Quantity=N (default 1)
#

def set_out_of_stock_control(enabled: bool) -> dict[str, Any]:
    """
    Enable (or disable) the seller-account OutOfStockControl preference.

    When enabled, zero-quantity fixed-price listings stay ACTIVE but are
    hidden from search/browse. Sellers can then call ReviseInventoryStatus
    to put Quantity back up and the listing reappears. Persists across
    sessions.
    """
    flag = "true" if enabled else "false"
    inner = (
        f"<OutOfStockControlPreference>{flag}</OutOfStockControlPreference>"
    )
    root = trading_call("SetUserPreferences", inner)
    return {"ack": _text(root, "e:Ack"), "enabled": enabled}


def get_out_of_stock_control() -> bool:
    """
    Read the current OutOfStockControl preference via GetUserPreferences.
    Returns True if enabled.
    """
    inner = "<ShowOutOfStockControlPreference>true</ShowOutOfStockControlPreference>"
    root = trading_call("GetUserPreferences", inner)
    val = _text(root, "e:OutOfStockControlPreference")
    return (val or "").lower() == "true"


def set_item_quantity(item_id: str, quantity: int) -> dict[str, Any]:
    """
    Change a fixed-price item's Quantity via ReviseInventoryStatus.

    Much lighter than ReviseFixedPriceItem — no re-verification, no
    search-ranking churn. This is the correct verb for routine
    stock / out-of-stock toggles.
    """
    if quantity < 0:
        raise ListerError("quantity must be ≥ 0")
    inner = (
        f"<InventoryStatus>"
        f"{_el('ItemID', item_id)}"
        f"{_el('Quantity', str(quantity))}"
        f"</InventoryStatus>"
    )
    root = trading_call("ReviseInventoryStatus", inner)
    inv = root.find("e:InventoryStatus", NS_MAP)
    return {
        "ack":      _text(root, "e:Ack"),
        "item_id":  item_id,
        "quantity": int(_text(inv, "e:Quantity") or quantity) if inv is not None else quantity,
        "fees":     [f.tag for f in root.findall("e:Fees/e:Fee", NS_MAP)],
    }


# --------------------------------------------------------------------------- #
# Picture upload (UploadSiteHostedPictures)
# --------------------------------------------------------------------------- #

def upload_site_hosted_picture(
    path: Path,
    *,
    picture_set: str = "Supersize",
) -> str:
    """
    Upload a local JPEG to eBay's Picture Services and return the
    hosted URL (`FullURL`).

    `picture_set`: Standard | Supersize | Large. Supersize is what
    Kim's current listings use and gives the best zoom.

    Implementation notes:
      * UploadSiteHostedPictures is the only Trading verb that expects
        multipart/form-data rather than a text/xml envelope. The body
        has two parts: the XML request (with a placeholder PictureData
        element), and the binary image bytes. Inline base64 is allowed
        by the API but eBay's picture service rejects it as "corrupt"
        in practice — multipart is the documented correct transport.
      * Files over ~12MB should be resized before calling this —
        bigger than that and eBay throttles.
    """
    import urllib.error
    import urllib.request
    from ebay_api.trading import TRADING_ENDPOINT, TRADING_VERSION, _site_id_for
    from ebay_api.token_manager import _load_env, get_access_token

    path = Path(path)
    if not path.exists():
        raise ListerError(f"picture not found: {path}")

    # XML request part — element order matters (eBay's schema is strict).
    # The binary bytes are sent as the second MIME part; no PictureData
    # element here for multipart uploads.
    xml_body = (
        f'<?xml version="1.0" encoding="utf-8"?>'
        f'<UploadSiteHostedPicturesRequest xmlns="{NS}">'
        f"{_el('PictureSet', picture_set)}"
        f"<PictureUploadPolicy>Add</PictureUploadPolicy>"
        f'</UploadSiteHostedPicturesRequest>'
    ).encode("utf-8")

    image_bytes = path.read_bytes()

    boundary = "MIME_boundary_klh_upload_pictures"
    crlf = b"\r\n"
    body = b"".join([
        b"--", boundary.encode(), crlf,
        b'Content-Disposition: form-data; name="XML Payload"', crlf,
        b"Content-Type: text/xml; charset=utf-8", crlf,
        crlf,
        xml_body, crlf,
        b"--", boundary.encode(), crlf,
        b'Content-Disposition: form-data; name="image"; filename="',
        path.name.encode(), b'"', crlf,
        f"Content-Type: {_guess_mime(path)}".encode(), crlf,
        b"Content-Transfer-Encoding: binary", crlf,
        crlf,
        image_bytes, crlf,
        b"--", boundary.encode(), b"--", crlf,
    ])

    env = _load_env()
    token = get_access_token()
    req = urllib.request.Request(TRADING_ENDPOINT, data=body)
    req.add_header("X-EBAY-API-COMPATIBILITY-LEVEL", TRADING_VERSION)
    req.add_header("X-EBAY-API-CALL-NAME", "UploadSiteHostedPictures")
    req.add_header("X-EBAY-API-SITEID", _site_id_for(env))
    req.add_header("X-EBAY-API-IAF-TOKEN", token)
    # Unquoted boundary — eBay's multipart parser trips on quoted forms.
    req.add_header(
        "Content-Type",
        f"multipart/form-data; boundary={boundary}",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        raise TradingError(
            f"UploadSiteHostedPictures HTTP {e.code}: {err_body[:500]}"
        ) from e

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise TradingError(f"UploadSiteHostedPictures returned invalid XML: {e}") from e

    ack = root.findtext("e:Ack", default="", namespaces=NS_MAP)
    if ack not in ("Success", "Warning"):
        err = root.find("e:Errors", NS_MAP)
        if err is not None:
            short = err.findtext("e:ShortMessage", default="", namespaces=NS_MAP)
            long = err.findtext("e:LongMessage", default="", namespaces=NS_MAP)
            raise TradingError(f"UploadSiteHostedPictures failed: {short} — {long}")
        raise TradingError(f"UploadSiteHostedPictures failed with Ack={ack!r}")

    site_hosted = root.find("e:SiteHostedPictureDetails", NS_MAP)
    if site_hosted is None:
        raise TradingError("UploadSiteHostedPictures returned no SiteHostedPictureDetails")
    full_url = _text(site_hosted, "e:FullURL")
    if not full_url:
        raise TradingError("UploadSiteHostedPictures returned no FullURL")
    return full_url


def _guess_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "image/jpeg"
