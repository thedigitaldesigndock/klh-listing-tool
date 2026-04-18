"""
eBay Sell Negotiation API helper (REST).

Handles SOTIB (Send Offer To Interested Buyers) — seller-initiated
discount offers to buyers who have watched or recently viewed a listing.

Endpoints wrapped:
    * find_eligible_items()             — listings eligible to receive offers
    * send_offer_to_interested_buyers() — send offers in bulk
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Iterable, Optional

from ebay_api import token_manager


BASE = "https://api.ebay.com/sell/negotiation/v1"
MARKETPLACE = "EBAY_GB"


class NegotiationError(RuntimeError):
    pass


def _headers(token: str, content_type: bool = True) -> dict[str, str]:
    h = {
        "Authorization":              f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID":    MARKETPLACE,
        "Accept":                     "application/json",
    }
    if content_type:
        h["Content-Type"] = "application/json"
    return h


def _request(method: str, url: str, body: Optional[dict] = None) -> tuple[int, Optional[dict]]:
    token = token_manager.get_access_token()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, headers=_headers(token, bool(data)), method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            parsed = json.loads(raw) if raw else None
            return resp.status, parsed
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace") if e.fp else ""
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = {"raw": raw}
        raise NegotiationError(
            f"{method} {url} → HTTP {e.code}: {json.dumps(parsed)[:500]}"
        ) from e


def find_eligible_items(*, limit: int = 500) -> list[str]:
    """Return all listing_ids currently eligible to receive offers.

    Paginates through all pages (100 items/page hard cap per eBay).
    """
    out: list[str] = []
    offset = 0
    page = 100
    while True:
        url = f"{BASE}/find_eligible_items?limit={page}&offset={offset}"
        _, body = _request("GET", url)
        items = (body or {}).get("eligibleItems") or []
        for it in items:
            lid = it.get("listingId")
            if lid:
                out.append(str(lid))
        if len(items) < page:
            break
        offset += page
        if limit and len(out) >= limit:
            break
    return out[:limit] if limit else out


def send_offers(
    offers: list[dict[str, Any]],
    *,
    batch_size: int = 10,
    sleep_between: float = 0.5,
    progress: Optional[Any] = None,
) -> dict[str, Any]:
    """Bulk-send SOTIB offers. Each offer dict should have:

        listingId               str (required)
        discountPercentage      str, e.g. "15" (required)
        duration                "DAYS_2" | "HOURS_48" etc. (default DAYS_2)
        quantity                int, default 1
        allowBuyerCounterOffer  bool, default False
        message                 str (optional)

    eBay can accept multiple offeredItems per call. We batch in groups
    of `batch_size` and stream through.

    Returns aggregate: {total, ok, failed, failures}.
    """
    aggregate: dict[str, Any] = {"total": len(offers), "ok": 0, "failed": 0, "failures": []}
    url = f"{BASE}/send_offer_to_interested_buyers"
    for i in range(0, len(offers), batch_size):
        batch = offers[i:i + batch_size]
        body = {"offeredItems": [
            {
                "listingId":              str(o["listingId"]),
                "discountPercentage":     str(o.get("discountPercentage", "15")),
                "duration":               o.get("duration", "DAYS_2"),
                "quantity":               int(o.get("quantity", 1)),
                "allowBuyerCounterOffer": bool(o.get("allowBuyerCounterOffer", False)),
                **({"message": o["message"]} if o.get("message") else {}),
            }
            for o in batch
        ]}
        try:
            _, resp = _request("POST", url, body=body)
            returned_offers = (resp or {}).get("offers") or []
            # Pair input items with returned offers by listingId for per-item tracking
            returned_by_lid: dict[str, dict] = {}
            for r in returned_offers:
                for oi in r.get("offeredItems") or []:
                    returned_by_lid[str(oi.get("listingId"))] = r
            for o in batch:
                lid = str(o["listingId"])
                got = returned_by_lid.get(lid)
                if got and got.get("offerStatus") in ("PENDING", "SENT"):
                    aggregate["ok"] += 1
                else:
                    aggregate["failed"] += 1
                    aggregate["failures"].append(
                        {"listing_id": lid, "reason": "no_offer_in_response"}
                    )
        except NegotiationError as e:
            aggregate["failed"] += len(batch)
            for o in batch:
                aggregate["failures"].append({
                    "listing_id": o["listingId"], "error": str(e)[:200],
                })
        if progress is not None:
            progress(min(i + batch_size, len(offers)), len(offers), aggregate)
        if sleep_between:
            time.sleep(sleep_between)
    return aggregate
