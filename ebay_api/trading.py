"""
eBay Trading API helper.

Thin wrapper around https://api.ebay.com/ws/api.dll — the classic XML
API that KLHAutographs' existing listings were created with. We stay on
Trading for V1 (rather than jumping to the modern REST Inventory API)
because Kim's entire active catalogue is on Trading and we want to be
able to read/edit those items, not just the ones we create.

Public surface:
    - TradingError: raised for non-2xx responses or eBay-reported errors
    - trading_call(verb, inner_xml, ...): low-level XML → dict
    - get_my_ebay_selling(...): list active listings, paginated
    - get_item(item_id, ...): fetch full item details including description

This module deliberately uses stdlib-only parsing (xml.etree) to avoid
a new dependency. The helpers flatten eBay's verbose nested structure
into plain dicts before returning.
"""

from __future__ import annotations

import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Iterable, Iterator, Optional

from ebay_api.token_manager import _load_env, get_access_token

TRADING_ENDPOINT = "https://api.ebay.com/ws/api.dll"
TRADING_VERSION = "1193"  # a recent compatibility level; safe for read ops

# eBay Trading responses use this namespace for every element.
NS = "urn:ebay:apis:eBLBaseComponents"
NS_MAP = {"e": NS}


class TradingError(RuntimeError):
    """Raised when a Trading API call fails or eBay returns an error."""


# --------------------------------------------------------------------------- #
# Low-level call
# --------------------------------------------------------------------------- #

def _build_request_xml(verb: str, inner_xml: str) -> bytes:
    """
    Wrap `inner_xml` in the full Trading API envelope. Authentication is
    done via headers (IAF OAuth), so no <RequesterCredentials> block.
    """
    return (
        f'<?xml version="1.0" encoding="utf-8"?>'
        f'<{verb}Request xmlns="{NS}">'
        f'{inner_xml}'
        f'</{verb}Request>'
    ).encode("utf-8")


def _site_id_for(env: dict) -> str:
    """
    Map EBAY_SITE env (e.g. EBAY_GB) to the numeric site ID used in the
    Trading API headers. We only need the sites Kim actually sells on.
    """
    site = (env.get("EBAY_SITE") or "EBAY_GB").upper()
    return {
        "EBAY_GB": "3",
        "EBAY_US": "0",
        "EBAY_IE": "205",
    }.get(site, "3")


def trading_call(
    verb: str,
    inner_xml: str,
    *,
    version: str = TRADING_VERSION,
    timeout: int = 30,
) -> ET.Element:
    """
    Make a Trading API call and return the parsed XML root element.
    Raises TradingError on HTTP failure or eBay Ack=Failure response.
    """
    env = _load_env()
    token = get_access_token()

    body = _build_request_xml(verb, inner_xml)

    req = urllib.request.Request(TRADING_ENDPOINT, data=body)
    req.add_header("X-EBAY-API-COMPATIBILITY-LEVEL", version)
    req.add_header("X-EBAY-API-CALL-NAME", verb)
    req.add_header("X-EBAY-API-SITEID", _site_id_for(env))
    req.add_header("X-EBAY-API-IAF-TOKEN", token)
    req.add_header("Content-Type", "text/xml; charset=utf-8")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        raise TradingError(
            f"{verb} HTTP {e.code}: {err_body[:500]}"
        ) from e

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise TradingError(f"{verb} returned invalid XML: {e}") from e

    ack = root.findtext("e:Ack", default="", namespaces=NS_MAP)
    if ack not in ("Success", "Warning"):
        # Extract the first error message for a clean failure report.
        err = root.find("e:Errors", NS_MAP)
        if err is not None:
            short = err.findtext("e:ShortMessage", default="", namespaces=NS_MAP)
            long = err.findtext("e:LongMessage", default="", namespaces=NS_MAP)
            raise TradingError(f"{verb} failed: {short} — {long}")
        raise TradingError(f"{verb} failed with Ack={ack!r}")

    return root


# --------------------------------------------------------------------------- #
# Small XML → dict helpers
# --------------------------------------------------------------------------- #

def _text(elem: Optional[ET.Element], path: str) -> Optional[str]:
    if elem is None:
        return None
    found = elem.find(path, NS_MAP)
    return found.text if found is not None else None


def _elem_to_dict(elem: ET.Element) -> dict[str, Any]:
    """
    Recursively convert an XML element into a nested dict.
    Repeated child tags become lists. Namespace prefixes are stripped.
    """
    def strip(tag: str) -> str:
        return tag.split("}", 1)[-1] if "}" in tag else tag

    result: dict[str, Any] = {}
    for child in elem:
        key = strip(child.tag)
        value: Any
        if len(child) == 0:
            value = (child.text or "").strip() or None
        else:
            value = _elem_to_dict(child)
        if key in result:
            existing = result[key]
            if isinstance(existing, list):
                existing.append(value)
            else:
                result[key] = [existing, value]
        else:
            result[key] = value
    return result


# --------------------------------------------------------------------------- #
# High-level calls
# --------------------------------------------------------------------------- #

def get_my_ebay_selling(
    *,
    entries_per_page: int = 25,
    page_number: int = 1,
    include_description: bool = False,
) -> dict[str, Any]:
    """
    Return a page of the seller's Active listings.

    Output shape:
        {
          "total_pages": int,
          "total_entries": int,
          "page_number": int,
          "items": [ {item_id, title, price, quantity, category_id,
                      category_name, start_time, listing_type, sku, ...}, ... ]
        }

    Note: GetMyeBaySelling returns a summary only — no description or
    item specifics. Call get_item() on specific items to drill in.
    """
    inner = (
        '<ActiveList>'
        '<Sort>TimeLeft</Sort>'
        f'<Pagination><EntriesPerPage>{int(entries_per_page)}</EntriesPerPage>'
        f'<PageNumber>{int(page_number)}</PageNumber></Pagination>'
        '</ActiveList>'
        '<DetailLevel>ReturnAll</DetailLevel>'
    )
    root = trading_call("GetMyeBaySelling", inner)

    active = root.find("e:ActiveList", NS_MAP)
    if active is None:
        return {"total_pages": 0, "total_entries": 0, "page_number": page_number, "items": []}

    pagination = active.find("e:PaginationResult", NS_MAP)
    total_pages = int(_text(pagination, "e:TotalNumberOfPages") or 0)
    total_entries = int(_text(pagination, "e:TotalNumberOfEntries") or 0)

    items: list[dict[str, Any]] = []
    item_array = active.find("e:ItemArray", NS_MAP)
    if item_array is not None:
        for item in item_array.findall("e:Item", NS_MAP):
            items.append({
                "item_id": _text(item, "e:ItemID"),
                "title": _text(item, "e:Title"),
                "sku": _text(item, "e:SKU"),
                "price": _text(item, "e:BuyItNowPrice") or _text(item, "e:SellingStatus/e:CurrentPrice"),
                "quantity": _text(item, "e:Quantity"),
                "quantity_available": _text(item, "e:QuantityAvailable"),
                "listing_type": _text(item, "e:ListingType"),
                "category_id": _text(item, "e:PrimaryCategoryID") or _text(item, "e:PrimaryCategory/e:CategoryID"),
                "category_name": _text(item, "e:PrimaryCategoryName") or _text(item, "e:PrimaryCategory/e:CategoryName"),
                "start_time": _text(item, "e:ListingDetails/e:StartTime"),
                "view_item_url": _text(item, "e:ListingDetails/e:ViewItemURL"),
                "watch_count": _text(item, "e:WatchCount"),
            })

    return {
        "total_pages": total_pages,
        "total_entries": total_entries,
        "page_number": page_number,
        "items": items,
    }


def get_item(item_id: str, include_description: bool = True) -> dict[str, Any]:
    """
    Fetch full details for a single item via GetItem, including the
    HTML description when include_description=True.
    """
    desc_flag = "true" if include_description else "false"
    inner = (
        f'<ItemID>{item_id}</ItemID>'
        '<DetailLevel>ReturnAll</DetailLevel>'
        f'<IncludeItemSpecifics>true</IncludeItemSpecifics>'
        f'<IncludeWatchCount>true</IncludeWatchCount>'
        f'<IncludeCrossPromotion>false</IncludeCrossPromotion>'
    )
    root = trading_call("GetItem", inner)
    item_elem = root.find("e:Item", NS_MAP)
    if item_elem is None:
        raise TradingError(f"GetItem returned no <Item> for {item_id}")
    return _elem_to_dict(item_elem)


# --------------------------------------------------------------------------- #
# Audit-oriented helpers: full active-catalogue sweep.
# --------------------------------------------------------------------------- #
#
# The audit tool needs every active listing in a consistent shape. These
# helpers are separate from get_my_ebay_selling() above (which returns a
# page at a time in a "good enough for humans" shape) because the audit
# cache wants stream-yielded rows + currency extracted from XML
# attributes + price normalised for SQLite.
#

def _first(elem: Optional[ET.Element], *paths: str) -> Optional[str]:
    """Return the text of the first matching path, or None."""
    if elem is None:
        return None
    for p in paths:
        found = elem.find(p, NS_MAP)
        if found is not None and found.text is not None:
            return found.text
    return None


def _price_and_currency(
    item: ET.Element,
) -> tuple[Optional[float], Optional[str]]:
    """
    Pull the buy-it-now price and currency from a listing <Item>.
    Falls back to the selling-status current price if BuyItNowPrice is
    missing (some legacy listings only have the Current/Start price).
    """
    for path in (
        "e:BuyItNowPrice",
        "e:SellingStatus/e:CurrentPrice",
        "e:StartPrice",
    ):
        el = item.find(path, NS_MAP)
        if el is not None and el.text:
            try:
                price = float(el.text)
            except ValueError:
                continue
            currency = el.attrib.get("currencyID")
            return price, currency
    return None, None


def _row_from_item_elem(item: ET.Element) -> dict[str, Any]:
    """Shape a <Item> element (as returned by GetMyeBaySelling) into an audit row."""
    price, currency = _price_and_currency(item)
    return {
        "item_id":            _first(item, "e:ItemID"),
        "title":              _first(item, "e:Title"),
        "sku":                _first(item, "e:SKU"),
        "category_id":        _first(
            item, "e:PrimaryCategoryID", "e:PrimaryCategory/e:CategoryID"
        ),
        "category_name":      _first(
            item, "e:PrimaryCategoryName", "e:PrimaryCategory/e:CategoryName"
        ),
        "price_gbp":          price,
        "currency":           currency,
        "quantity":           _as_int(_first(item, "e:Quantity")),
        "quantity_available": _as_int(_first(item, "e:QuantityAvailable")),
        "watch_count":        _as_int(_first(item, "e:WatchCount")),
        "start_time":         _first(item, "e:ListingDetails/e:StartTime"),
        "listing_type":       _first(item, "e:ListingType"),
        "view_item_url":      _first(item, "e:ListingDetails/e:ViewItemURL"),
    }


def _as_int(s: Optional[str]) -> Optional[int]:
    if s is None:
        return None
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


def iter_active_items_summary(
    *,
    page_size: int = 200,
    start_page: int = 1,
    progress: Optional[Any] = None,
) -> Iterator[dict[str, Any]]:
    """
    Stream every active listing's summary dict via GetMyeBaySelling.

    Yields one dict per listing with the audit-row shape:
        {item_id, title, sku, category_id, category_name,
         price_gbp, currency, quantity, quantity_available, watch_count,
         start_time, listing_type, view_item_url}

    Does NOT include item specifics — that requires a per-item GetItem
    call (see get_item()). The audit flow uses this function for the fast
    catalogue sweep and defers the deep fetch.

    `progress`, if given, is called as progress(page, total_pages,
    total_entries) once per page so the CLI can render a progress bar.
    """
    page = max(1, int(start_page))
    while True:
        inner = (
            "<ActiveList>"
            "<Sort>TimeLeft</Sort>"
            f"<Pagination><EntriesPerPage>{int(page_size)}</EntriesPerPage>"
            f"<PageNumber>{page}</PageNumber></Pagination>"
            "</ActiveList>"
            "<DetailLevel>ReturnAll</DetailLevel>"
        )
        root = trading_call("GetMyeBaySelling", inner)
        active = root.find("e:ActiveList", NS_MAP)
        if active is None:
            return

        pagination = active.find("e:PaginationResult", NS_MAP)
        total_pages = int(_text(pagination, "e:TotalNumberOfPages") or 0)
        total_entries = int(_text(pagination, "e:TotalNumberOfEntries") or 0)
        if progress is not None:
            progress(page, total_pages, total_entries)

        item_array = active.find("e:ItemArray", NS_MAP)
        if item_array is not None:
            for item in item_array.findall("e:Item", NS_MAP):
                row = _row_from_item_elem(item)
                if row["item_id"]:
                    yield row

        if page >= total_pages:
            return
        page += 1


def get_items_bulk(
    item_ids: Iterable[str],
    *,
    sleep: float = 0.5,
    progress: Optional[Any] = None,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """
    Rate-limited GetItem sweep. Yields `(item_id, deep_row)` tuples
    where deep_row has the audit-shape keys:

        {item_specifics: dict, hit_count, quantity_sold, end_time,
         condition_id}

    item_specifics is a flat {name: value} dict (multi-value specifics
    become a "|"-joined string, matching how ReviseFixedPriceItem wants
    them returned later). Any error on a single item is logged via
    `progress(item_id, error_str)` and skipped — the caller can choose
    to retry on the next run since deep_fetched_at stays NULL.
    """
    import time

    for item_id in item_ids:
        try:
            raw = get_item(item_id, include_description=False)
        except TradingError as e:
            if progress is not None:
                progress(item_id, None, str(e))
            continue

        deep = _shape_deep_item(raw)
        if progress is not None:
            progress(item_id, deep, None)
        yield item_id, deep
        if sleep > 0:
            time.sleep(sleep)


def _shape_deep_item(raw: dict[str, Any]) -> dict[str, Any]:
    """Pull the audit-relevant fields out of a GetItem response dict."""
    specifics: dict[str, str] = {}
    item_specs = raw.get("ItemSpecifics") or {}
    nvl = item_specs.get("NameValueList")
    if nvl:
        entries = nvl if isinstance(nvl, list) else [nvl]
        for entry in entries:
            name = entry.get("Name")
            value = entry.get("Value")
            if not name:
                continue
            if isinstance(value, list):
                specifics[name] = " | ".join(v for v in value if v)
            else:
                specifics[name] = value or ""

    selling = raw.get("SellingStatus") or {}
    primary_cat = raw.get("PrimaryCategory") if isinstance(raw.get("PrimaryCategory"), dict) else {}

    # Primary picture URL — used by the team-review dashboard panel to
    # display a thumbnail next to each listing so the user can eyeball
    # what team is actually in the photo. GetItem returns PictureURL as
    # either a single string or a list of strings depending on whether
    # the listing has multiple photos.
    pic_details = raw.get("PictureDetails") if isinstance(raw.get("PictureDetails"), dict) else {}
    pic_url_raw = pic_details.get("PictureURL")
    if isinstance(pic_url_raw, list):
        picture_url = pic_url_raw[0] if pic_url_raw else None
    else:
        picture_url = pic_url_raw

    return {
        "item_specifics": specifics,
        "hit_count":      _as_int(raw.get("HitCount")),
        "quantity_sold":  _as_int(selling.get("QuantitySold")),
        "end_time":       raw.get("ListingDetails", {}).get("EndTime") if isinstance(raw.get("ListingDetails"), dict) else None,
        "condition_id":   raw.get("ConditionID"),
        "category_id":    primary_cat.get("CategoryID"),
        "category_name":  primary_cat.get("CategoryName"),
        "picture_url":    picture_url,
    }
