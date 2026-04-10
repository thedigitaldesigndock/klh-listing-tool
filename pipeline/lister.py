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
      * Trading's UploadSiteHostedPictures accepts two transport modes
        — a base64 <PictureData> element, or a multipart/form-data POST.
        Base64 in XML is simpler and uses exactly the same endpoint /
        headers as every other Trading call, so we use that.
      * Files over ~12MB should be resized before calling this —
        bigger than that and eBay throttles.
    """
    path = Path(path)
    if not path.exists():
        raise ListerError(f"picture not found: {path}")

    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    inner = (
        f"{_el('PictureSet', picture_set)}"
        f"<PictureUploadPolicy>Add</PictureUploadPolicy>"
        f"<PictureData contentType=\"{_guess_mime(path)}\">{encoded}</PictureData>"
    )
    root = trading_call("UploadSiteHostedPictures", inner)
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
