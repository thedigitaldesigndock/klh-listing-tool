"""
eBay Sell Marketing API helper (REST).

Minimal wrapper — only the endpoints we use for tiered Promoted Listings
Standard campaign management:

    * get_campaigns()                 → list existing campaigns
    * create_campaign(...)            → create a new PLS campaign
    * bulk_create_ads(...)            → add ads to a campaign by listing_id
    * end_campaign(campaign_id)       → mark a campaign ended

Auth: OAuth2 bearer (token_manager.get_access_token).
Marketplace: EBAY_GB (the only one KLHAutographs lists on today).
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from ebay_api import token_manager


BASE = "https://api.ebay.com/sell/marketing/v1"
MARKETPLACE = "EBAY_GB"


class MarketingError(RuntimeError):
    """Raised when an eBay Marketing API call returns a non-2xx."""


def _headers(token: str, content_type: bool = True) -> dict[str, str]:
    h = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE,
        "Accept": "application/json",
    }
    if content_type:
        h["Content-Type"] = "application/json"
    return h


def _request(
    method: str,
    url: str,
    *,
    body: Optional[dict] = None,
    token: Optional[str] = None,
) -> tuple[int, Optional[dict], dict]:
    """Low-level HTTP. Returns (status, parsed_body_or_None, response_headers)."""
    token = token or token_manager.get_access_token()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, headers=_headers(token, content_type=bool(data)),
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            hdrs = dict(resp.headers.items())
            parsed = json.loads(raw) if raw else None
            return resp.status, parsed, hdrs
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace") if e.fp else ""
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = {"raw": raw}
        raise MarketingError(
            f"{method} {url} → HTTP {e.code}: {json.dumps(parsed)[:500]}"
        ) from e


def get_campaigns(
    status: Optional[str] = None,
    *,
    limit: int = 50,
) -> list[dict]:
    """Return all campaigns matching optional status (e.g. 'RUNNING')."""
    q = f"?limit={limit}"
    if status:
        q += f"&campaign_status={status}"
    _, body, _ = _request("GET", f"{BASE}/ad_campaign{q}")
    return (body or {}).get("campaigns") or []


def create_campaign(
    *,
    campaign_name: str,
    ad_rate_cap_percent: float,
    marketplace_id: str = MARKETPLACE,
    funding_model: str = "COST_PER_SALE",
    ad_rate_strategy: str = "FIXED",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    auto_select_future_inventory: bool = False,
) -> str:
    """Create a PLS campaign. Returns the new campaign_id.

    FIXED rate strategy means the cap is also the actual rate applied to
    every ad. (DYNAMIC lets eBay adjust within a window, which we don't
    want for tiered strategy — defeats the point of per-tier caps.)

    Default auto_select_future_inventory=False: new listings don't
    auto-enrol, we route them in the daily housekeeping job so each
    lands in the right tier by price.
    """
    funding = {
        "fundingModel":     funding_model,
        "adRateStrategy":   ad_rate_strategy,
        "bidPercentage":    f"{ad_rate_cap_percent:.1f}",
    }
    # eBay requires a startDate — default to right now if caller didn't pass.
    # Format: ISO-8601 with ms + 'Z'. They reject fractional-seconds with
    # more than 3 digits so we truncate.
    if not start_date:
        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc)
        start_date = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    body: dict[str, Any] = {
        "campaignName":      campaign_name,
        "startDate":         start_date,
        "fundingStrategy":   funding,
        "marketplaceId":     marketplace_id,
        # campaignCriterion only matters when autoSelectFutureInventory is
        # true — it's the filter eBay uses to decide which new listings
        # auto-join. Since our tiered campaigns select inventory manually
        # via bulk_create_ads, we skip the criterion here and let eBay
        # default it. (When we later want auto-select on STANDARD we'll
        # pass categoryIds explicitly.)
    }
    if auto_select_future_inventory:
        # Placeholder — add a real categoryIds filter when this path is
        # wired up. For now every campaign we build has auto-select=False.
        body["campaignCriterion"] = {
            "criterionType":              "INVENTORY_PARTITION",
            "autoSelectFutureInventory":  True,
            "selectionRules":             [
                {"categoryIds": ["64482"]},  # Sports Memorabilia root (UK)
            ],
        }
    if end_date:
        body["endDate"] = end_date

    status, resp_body, headers = _request("POST", f"{BASE}/ad_campaign", body=body)
    # 201 Created, Location header carries the new campaign URI
    loc = headers.get("Location") or headers.get("location") or ""
    cid = loc.rsplit("/", 1)[-1] if loc else (resp_body or {}).get("campaignId")
    if not cid:
        raise MarketingError(f"createCampaign returned no id: status={status} body={resp_body}")
    return cid


def bulk_create_ads(
    campaign_id: str,
    listing_ids: Iterable[str],
    *,
    ad_rate_cap_percent: float,
    batch_size: int = 500,
    sleep_between: float = 1.0,
    progress: Optional[Any] = None,
) -> dict[str, Any]:
    """Add many listings to a campaign by listing_id, in batches.

    eBay bulk_create endpoint accepts up to 500 items per call. We
    serialise the batches with a small sleep to stay polite.

    Returns aggregate counts + the per-item statuses for anything that
    failed (success cases are summarised, failures listed).
    """
    ids = list(listing_ids)
    total = len(ids)
    aggregate = {"total": total, "ok": 0, "failed": 0, "failures": []}
    if not ids:
        return aggregate

    url = f"{BASE}/ad_campaign/{campaign_id}/bulk_create_ads_by_listing_id"
    bid_str = f"{ad_rate_cap_percent:.1f}"

    for i in range(0, total, batch_size):
        chunk = ids[i:i + batch_size]
        body = {
            "requests": [
                {"listingId": iid, "bidPercentage": bid_str}
                for iid in chunk
            ]
        }
        status, resp_body, _ = _request("POST", url, body=body)
        responses = (resp_body or {}).get("responses") or []
        for resp in responses:
            if (resp.get("statusCode") or 0) < 300:
                aggregate["ok"] += 1
            else:
                aggregate["failed"] += 1
                aggregate["failures"].append({
                    "listing_id": resp.get("listingId"),
                    "status":     resp.get("statusCode"),
                    "errors":     resp.get("errors"),
                })
        if progress is not None:
            progress(i + len(chunk), total, aggregate)
        if sleep_between:
            time.sleep(sleep_between)
    return aggregate


def end_campaign(campaign_id: str) -> dict[str, Any]:
    """Mark a campaign ENDED. Historical reports remain accessible."""
    url = f"{BASE}/ad_campaign/{campaign_id}/end"
    _, body, _ = _request("POST", url)
    return body or {}
