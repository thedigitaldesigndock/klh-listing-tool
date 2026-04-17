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


# --------------------------------------------------------------------------- #
# PLA (Promoted Listings Advanced) — COST_PER_CLICK campaigns
# --------------------------------------------------------------------------- #
#
# PLA uses a different shape from PLS:
#   * fundingModel = COST_PER_CLICK (CPC), not COST_PER_SALE (CPS)
#   * budget.daily.amount controls the daily ad spend cap
#   * Campaign has ad_group(s) that carry a defaultBid
#   * Ads (listing targets) are created at the campaign level
#       (endpoint: /ad_campaign/{cid}/ad)
#   * Keywords are also at the campaign level, pointing to one ad_group
#       (endpoint: /ad_campaign/{cid}/keyword)
# When a buyer searches and matches a keyword, eBay charges the ad_group's
# bid per click on any of the ads (listings) in that group.


def create_pla_campaign(
    *,
    campaign_name: str,
    daily_budget_gbp: float,
    start_date: Optional[str] = None,
    marketplace_id: str = MARKETPLACE,
) -> str:
    """Create a Promoted Listings Advanced (CPC) campaign. Returns campaign_id.

    daily_budget_gbp is the hard daily spend ceiling — eBay stops serving
    once it's reached for the day.
    """
    if not start_date:
        import datetime as _dt
        start_date = _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
    body = {
        "campaignName":    campaign_name,
        "startDate":       start_date,
        "marketplaceId":   marketplace_id,
        "fundingStrategy": {"fundingModel": "COST_PER_CLICK"},
        "budget": {
            "daily": {"amount": {
                "value":    f"{daily_budget_gbp:.2f}",
                "currency": "GBP",
            }}
        },
    }
    _, _, headers = _request("POST", f"{BASE}/ad_campaign", body=body)
    loc = (headers or {}).get("Location", "") if headers else ""
    if loc:
        return loc.rstrip("/").rsplit("/", 1)[-1]
    # Fallback: list campaigns, find by name
    for c in get_campaigns() or []:
        if c.get("campaignName") == campaign_name:
            return str(c["campaignId"])
    raise MarketingError("PLA campaign created but campaign_id not in Location header")


def create_ad_group(
    campaign_id: str,
    *,
    name: str,
    default_bid_gbp: float,
) -> str:
    """Create an ad group under a PLA campaign. Returns ad_group_id."""
    body = {
        "name":       name,
        "defaultBid": {"value": f"{default_bid_gbp:.2f}", "currency": "GBP"},
    }
    _, _, headers = _request(
        "POST", f"{BASE}/ad_campaign/{campaign_id}/ad_group", body=body,
    )
    loc = (headers or {}).get("Location", "") if headers else ""
    if loc:
        return loc.rstrip("/").rsplit("/", 1)[-1]
    raise MarketingError(
        f"ad_group created under {campaign_id} but ad_group_id not in Location"
    )


def bulk_create_pla_ads(
    campaign_id: str,
    ad_group_id: str,
    listing_ids: Iterable[str],
    *,
    batch_size: int = 500,
    sleep_between: float = 1.0,
    progress: Optional[Any] = None,
) -> dict[str, Any]:
    """Bulk-add listings as ads under one ad_group in a PLA campaign.

    Returns same shape as bulk_create_ads (PLS). eBay returns 207 multi-
    status — per-item failures are captured in `failures` with status
    + errors per entry (for example 'An ad for listing ID X already exists'
    if the listing is still in another campaign).
    """
    ids = list(listing_ids)
    total = len(ids)
    aggregate: dict[str, Any] = {"total": total, "ok": 0, "failed": 0, "failures": []}
    if total == 0:
        return aggregate

    url = f"{BASE}/ad_campaign/{campaign_id}/bulk_create_ads_by_listing_id"
    for i in range(0, total, batch_size):
        chunk = ids[i:i + batch_size]
        body = {
            "requests": [
                {"listingId": str(lid), "adGroupId": ad_group_id}
                for lid in chunk
            ]
        }
        _, resp, _ = _request("POST", url, body=body)
        for entry in (resp or {}).get("responses") or []:
            if int(entry.get("statusCode", 500)) in (200, 201):
                aggregate["ok"] += 1
            else:
                aggregate["failed"] += 1
                aggregate["failures"].append({
                    "listing_id": entry.get("listingId"),
                    "status":     entry.get("statusCode"),
                    "errors":     entry.get("errors"),
                })
        if progress is not None:
            progress(i + len(chunk), total, aggregate)
        if sleep_between:
            time.sleep(sleep_between)
    return aggregate


def bulk_create_pla_keywords(
    campaign_id: str,
    requests: list[dict[str, Any]],
    *,
    sleep_between: float = 0.0,
) -> dict[str, Any]:
    """Create multiple keyword bids on one PLA campaign.

    Each request dict expects: adGroupId, keywordText, matchType
    (BROAD/PHRASE/EXACT), bid (dict with value+currency).

    Returns aggregate counts + failures (already-exists errors are common
    on re-runs and captured here rather than raised).
    """
    url = f"{BASE}/ad_campaign/{campaign_id}/bulk_create_keyword"
    aggregate: dict[str, Any] = {"total": len(requests), "ok": 0, "failed": 0, "failures": []}
    if not requests:
        return aggregate
    body = {"requests": requests}
    _, resp, _ = _request("POST", url, body=body)
    for entry in (resp or {}).get("responses") or []:
        if int(entry.get("statusCode", 500)) in (200, 201):
            aggregate["ok"] += 1
        else:
            aggregate["failed"] += 1
            aggregate["failures"].append({
                "status": entry.get("statusCode"),
                "errors": entry.get("errors"),
            })
    if sleep_between:
        time.sleep(sleep_between)
    return aggregate
