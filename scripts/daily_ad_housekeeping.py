#!/usr/bin/env python3
"""
Daily re-tier the Promoted Listings ad set.

Runs idempotently — every day reconciles the live campaigns against the
audit DB's current price_gbp. Handles:

  ADD     — listing with price >= £10 not in any tier campaign → add to
            the tier matching its price band
  MOVE    — listing's price changed band since last run → delete ad from
            old tier, add to new tier
  REMOVE  — listing now priced <£10 (or ended) but still in a tier
            campaign → delete ad

All ad writes are batched through the Marketing API. Dry-run by default;
pass --apply to write live.

Scheduling: wire up via cron (on your Mac) or Windows Task Scheduler
(on Kim/Nicky's PCs, once we port) at, say, 06:00 daily. The audit DB
needs to be reasonably fresh — run `klh audit fetch` weekly to pick up
brand new listings eBay added since the last tier run.

Usage:
    python scripts/daily_ad_housekeeping.py               # dry-run
    python scripts/daily_ad_housekeeping.py --apply       # live
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from pipeline import audit_db, backlog
from ebay_api import marketing, token_manager


# Tier campaigns created 2026-04-17. Caps per Peter's confirmed tier design.
TIER_CAMPAIGNS: dict[str, tuple[str, float, float, float]] = {
    # tier_name    → (campaign_id,    rate%,  price_lo, price_hi)
    "BUDGET":       ("162557282013",  5.0,   10.0,  15.0),
    "STANDARD":     ("162557283013",  8.2,   15.0,  30.0),
    "PREMIUM":      ("162557285013", 10.0,   30.0,  50.0),
    "PREMIUM_PLUS": ("162557288013", 12.0,   50.0,  1e9),
}


def _tier_for_price(p: Optional[float]) -> Optional[str]:
    if p is None or p < 10.0:
        return None
    for name, (_cid, _rate, lo, hi) in TIER_CAMPAIGNS.items():
        if lo <= p < hi:
            return name
    return None


# --------------------------------------------------------------------------- #
# eBay calls — fetch current state and remove ads
# --------------------------------------------------------------------------- #

def _get_ads_in_campaign(cid: str) -> dict[str, str]:
    """Return {listing_id: ad_id} for every active ad in a campaign."""
    token = token_manager.get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB",
        "Accept": "application/json",
    }
    out: dict[str, str] = {}
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
            lid = str(a.get("listingId") or "")
            aid = str(a.get("adId") or "")
            if lid and aid:
                out[lid] = aid
        if len(ads) < limit:
            break
        offset += limit
    return out


def _delete_ad(cid: str, ad_id: str) -> None:
    token = token_manager.get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB",
        "Accept": "application/json",
    }
    url = f"https://api.ebay.com/sell/marketing/v1/ad_campaign/{cid}/ad/{ad_id}"
    req = urllib.request.Request(url, headers=headers, method="DELETE")
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()  # 204 No Content


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #

def _plan() -> dict:
    """Build the plan: per-listing expected tier vs current tier."""
    # Load expected tiers from audit DB
    expected: dict[str, Optional[str]] = {}  # item_id → tier or None (exclude)
    with audit_db.connect(readonly=True) as conn:
        for r in conn.execute(
            "SELECT item_id, price_gbp FROM listings WHERE listing_type IS NOT NULL"
        ):
            expected[r["item_id"]] = _tier_for_price(r["price_gbp"])

    # Load current tiers from eBay (ad_id needed for deletes)
    current: dict[str, tuple[str, str]] = {}  # item_id → (tier, ad_id)
    for tier, (cid, _, _, _) in TIER_CAMPAIGNS.items():
        print(f"  fetching ads in {tier} (cid={cid})…")
        for listing_id, ad_id in _get_ads_in_campaign(cid).items():
            if listing_id in current:
                # Listing is in multiple tiers — shouldn't happen but be defensive
                print(f"    ⚠ {listing_id} in multiple campaigns "
                      f"({current[listing_id][0]} + {tier})")
            current[listing_id] = (tier, ad_id)

    actions = {"add": defaultdict(list), "remove": [], "move_from_to": []}
    #   add       → {tier: [listing_ids]}  – listings NOT in any ad campaign
    #   remove    → [(tier, listing_id, ad_id)] – listings no longer eligible
    #   move      → [(listing_id, from_tier, from_ad_id, to_tier)]
    stats = {"ok": 0, "add": 0, "move": 0, "remove": 0, "unknown": 0}

    all_ids = set(expected) | set(current)
    for lid in all_ids:
        exp = expected.get(lid)
        cur = current.get(lid)  # (tier, ad_id) or None
        if exp is None and cur is None:
            # Not in audit DB and not in ads — nothing to do
            continue
        if exp and cur is None:
            actions["add"][exp].append(lid)
            stats["add"] += 1
        elif exp is None and cur is not None:
            actions["remove"].append((cur[0], lid, cur[1]))
            stats["remove"] += 1
        elif exp and cur and exp == cur[0]:
            stats["ok"] += 1
        elif exp and cur and exp != cur[0]:
            actions["move_from_to"].append((lid, cur[0], cur[1], exp))
            stats["move"] += 1
        else:
            stats["unknown"] += 1

    return {"actions": actions, "stats": stats,
            "expected": expected, "current": current}


def _apply_plan(plan: dict) -> None:
    actions = plan["actions"]

    # 1. REMOVE (listings excluded now) — delete ads
    if actions["remove"]:
        print(f"\nRemoving {len(actions['remove']):,} ads "
              f"(listings no longer eligible)…")
        for i, (tier, lid, aid) in enumerate(actions["remove"], 1):
            try:
                _delete_ad(TIER_CAMPAIGNS[tier][0], aid)
                print(f"  {i}/{len(actions['remove'])} deleted ad={aid} "
                      f"(listing={lid}, tier={tier})")
            except Exception as e:
                print(f"  ✗ {lid}: {e}")
            time.sleep(0.7)

    # 2. MOVE — delete from old tier, queue for add-to-new
    moved_additions: dict[str, list[str]] = defaultdict(list)
    if actions["move_from_to"]:
        print(f"\nMoving {len(actions['move_from_to']):,} listings "
              f"between tiers (price band changed)…")
        for i, (lid, from_tier, aid, to_tier) in enumerate(actions["move_from_to"], 1):
            try:
                _delete_ad(TIER_CAMPAIGNS[from_tier][0], aid)
                moved_additions[to_tier].append(lid)
                print(f"  {i}/{len(actions['move_from_to'])} {lid}: "
                      f"{from_tier} → {to_tier}")
            except Exception as e:
                print(f"  ✗ {lid}: {e}")
            time.sleep(0.7)
        if moved_additions:
            print("  Sleeping 30s for propagation before adding to new tiers…")
            time.sleep(30)

    # 3. ADD — new listings + the move-adds
    all_adds = dict(actions["add"])
    for tier, lids in moved_additions.items():
        all_adds.setdefault(tier, []).extend(lids)

    for tier, listing_ids in all_adds.items():
        if not listing_ids:
            continue
        cid, rate, _, _ = TIER_CAMPAIGNS[tier]
        print(f"\nAdding {len(listing_ids):,} listings to {tier} "
              f"(cid={cid}, bid={rate}%)…")

        def _progress(done, total, agg):
            print(f"  {done:>5}/{total:<5}  ok={agg['ok']}  failed={agg['failed']}")

        agg = marketing.bulk_create_ads(
            cid, listing_ids, ad_rate_cap_percent=rate,
            batch_size=500, sleep_between=1.0, progress=_progress,
        )
        print(f"  done: ok={agg['ok']} failed={agg['failed']}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()

    print("Building reconciliation plan…")
    plan = _plan()
    s = plan["stats"]

    print("\n=== Plan ===")
    print(f"  Already correct:                 {s['ok']:,}")
    print(f"  Need ADD (new listings):         {s['add']:,}")
    print(f"  Need MOVE (price band changed):  {s['move']:,}")
    print(f"  Need REMOVE (now excluded):      {s['remove']:,}")
    if s["unknown"]:
        print(f"  unknown state:                   {s['unknown']:,}")

    # Per-tier adds breakdown
    adds = plan["actions"]["add"]
    if any(adds.values()):
        print("\nADDs by tier:")
        for tier, lids in sorted(adds.items()):
            print(f"  {tier:<14}  {len(lids):,}")

    if not args.apply:
        print("\n[DRY RUN] pass --apply to write.")
        return 0

    _apply_plan(plan)

    # Log to optimization_log for tracking
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    detail = (f"daily housekeeping: ok={s['ok']} add={s['add']} "
              f"move={s['move']} remove={s['remove']}")
    with audit_db.connect() as conn:
        conn.execute(
            "INSERT INTO optimization_log (event, event_at, details) VALUES (?, ?, ?)",
            ("AD_HOUSEKEEPING", now, detail),
        )
        conn.commit()
    print(f"\nLogged: {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
