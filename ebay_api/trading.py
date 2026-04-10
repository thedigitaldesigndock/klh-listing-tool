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
from typing import Any, Optional

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
