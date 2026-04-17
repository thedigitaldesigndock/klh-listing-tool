#!/usr/bin/env python3
"""
Retry the listings that failed in the first tier-campaign build with
'An ad for listing ID X already exists'. Those failures were caused
by eBay's end-campaign propagation delay — Campaign 1 was still
holding zombie ad associations for a few minutes after our 'end'
call returned. Now that time has passed they should take the new ads
cleanly.

Reads the per-tier lists from the audit DB by price band, queries
each new tier campaign for what's already there, and bulk-adds only
the listings still missing.
"""
from __future__ import annotations

import time
from pipeline import audit_db
from ebay_api import marketing


# Created by build_tier_campaigns.py run 4 (2026-04-17).
TIER_CAMPAIGNS = {
    "BUDGET":       ("162557282013",  5.0,  10.0,  15.0),
    "STANDARD":     ("162557283013",  8.2,  15.0,  30.0),
    "PREMIUM":      ("162557285013", 10.0,  30.0,  50.0),
    "PREMIUM_PLUS": ("162557288013", 12.0,  50.0,  1e9),
}


def _listings_already_in_campaign(cid: str) -> set[str]:
    """Paginate through /ad to collect every listing_id currently in a campaign."""
    import json, urllib.request
    from ebay_api import token_manager
    token = token_manager.get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB",
        "Accept": "application/json",
    }
    out: set[str] = set()
    offset = 0
    limit = 500
    while True:
        url = (f"https://api.ebay.com/sell/marketing/v1/ad_campaign/{cid}/ad"
               f"?limit={limit}&offset={offset}")
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read())
        ads = body.get("ads") or []
        if not ads:
            break
        for a in ads:
            lid = a.get("listingId")
            if lid:
                out.add(str(lid))
        if len(ads) < limit:
            break
        offset += limit
    return out


def main() -> int:
    with audit_db.connect(readonly=True) as conn:
        all_rows = conn.execute(
            "SELECT item_id, price_gbp FROM listings WHERE listing_type IS NOT NULL"
        ).fetchall()
    listings_by_tier: dict[str, list[str]] = {t: [] for t in TIER_CAMPAIGNS}
    for r in all_rows:
        p = r["price_gbp"]
        if p is None or p < 10:
            continue
        for tier, (_, _, lo, hi) in TIER_CAMPAIGNS.items():
            if lo <= p < hi:
                listings_by_tier[tier].append(r["item_id"])
                break

    total_planned = sum(len(v) for v in listings_by_tier.values())
    print(f"Planned total: {total_planned:,} across 4 tiers\n")

    grand_ok = grand_already = grand_failed = 0
    for tier, (cid, rate, _, _) in TIER_CAMPAIGNS.items():
        planned = listings_by_tier[tier]
        print(f"=== {tier} (cid={cid}, bid={rate}%) ===")
        print(f"  Planned: {len(planned):,} listings")
        already = _listings_already_in_campaign(cid)
        print(f"  Already in campaign: {len(already):,}")
        missing = [i for i in planned if i not in already]
        print(f"  To retry: {len(missing):,}")
        grand_already += len(already)
        if not missing:
            print("  nothing to do\n")
            continue

        def _progress(done, total, agg):
            print(f"    {done:>5}/{total:<5}  ok={agg['ok']}  failed={agg['failed']}")

        agg = marketing.bulk_create_ads(
            cid, missing, ad_rate_cap_percent=rate,
            batch_size=500, sleep_between=1.5, progress=_progress,
        )
        grand_ok += agg["ok"]
        grand_failed += agg["failed"]
        print(f"  done: ok={agg['ok']} failed={agg['failed']}")
        if agg["failures"]:
            # Count unique error codes
            code_counts: dict[str, int] = {}
            for f in agg["failures"]:
                errs = f.get("errors") or []
                if errs:
                    code = errs[0].get("errorId", "?")
                else:
                    code = str(f.get("status", "?"))
                code_counts[str(code)] = code_counts.get(str(code), 0) + 1
            print(f"  failure codes: {code_counts}")
        print()
        time.sleep(1)

    print(f"=== Grand total ===")
    print(f"  Already in campaigns: {grand_already:,}")
    print(f"  Newly added this run: {grand_ok:,}")
    print(f"  Failed this run:      {grand_failed:,}")
    print(f"  Effective coverage:   {grand_already + grand_ok:,} / {total_planned:,}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
