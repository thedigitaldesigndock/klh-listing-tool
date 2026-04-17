#!/usr/bin/env python3
"""
Build the 4-tier Promoted Listings structure from scratch.

Creates BUDGET / STANDARD / PREMIUM / PREMIUM_PLUS campaigns, migrates
every active listing into the correct price-band campaign, removes
under-£10 listings from ads entirely, and ends the legacy "Campaign 1"
so measurement windows start fresh.

Tier bands + caps (confirmed by Peter 2026-04-17):

    UNDER £10         → EXCLUDED from ads (too thin a margin)
    £10.00 - £14.99   → BUDGET           (5.0%)
    £15.00 - £29.99   → STANDARD         (8.2%)
    £30.00 - £49.99   → PREMIUM          (10.0%)
    £50.00+           → PREMIUM_PLUS     (12.0%)

Usage:
    python scripts/build_tier_campaigns.py              # dry-run (default)
    python scripts/build_tier_campaigns.py --apply      # live writes
    python scripts/build_tier_campaigns.py --apply --yes   # skip prompt
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

from pipeline import audit_db
from ebay_api import marketing


# ---- Tier definitions ------------------------------------------------------ #

TIERS = [
    ("BUDGET",       10.0,  15.0,   5.0),
    ("STANDARD",     15.0,  30.0,   8.2),
    ("PREMIUM",      30.0,  50.0,  10.0),
    ("PREMIUM_PLUS", 50.0,  1e9,   12.0),
]
TIER_NAME_PREFIX = "KLH Tier "


def _tier_for_price(price_gbp):
    if price_gbp is None or price_gbp < 10.0:
        return None
    for name, lo, hi, _rate in TIERS:
        if lo <= price_gbp < hi:
            return name
    return None


# ---- Planning ------------------------------------------------------------- #

def _classify_listings() -> dict[str, list[str]]:
    """Return {tier_name: [item_id, ...], '_EXCLUDED': [...], '_UNKNOWN': [...]}."""
    buckets = {name: [] for name, _, _, _ in TIERS}
    buckets["_EXCLUDED"] = []
    buckets["_UNKNOWN"] = []
    with audit_db.connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT item_id, price_gbp FROM listings WHERE listing_type IS NOT NULL"
        ).fetchall()
    for r in rows:
        price = r["price_gbp"]
        if price is None:
            buckets["_UNKNOWN"].append(r["item_id"])
        elif price < 10.0:
            buckets["_EXCLUDED"].append(r["item_id"])
        else:
            tier = _tier_for_price(price)
            if tier:
                buckets[tier].append(r["item_id"])
            else:
                buckets["_UNKNOWN"].append(r["item_id"])
    return buckets


def _print_plan(buckets: dict[str, list[str]]) -> None:
    total = sum(len(v) for v in buckets.values())
    print(f"=== Plan ({total:,} listings classified) ===\n")
    for name, _, _, rate in TIERS:
        n = len(buckets.get(name, []))
        print(f"  {name:<13}  {n:>6,}  → new campaign at {rate}%")
    print(f"  {'EXCLUDED':<13}  {len(buckets['_EXCLUDED']):>6,}  → removed from ads (under £10)")
    if buckets["_UNKNOWN"]:
        print(f"  {'(no price)':<13}  {len(buckets['_UNKNOWN']):>6,}  → left alone, not added to any new campaign")
    print()


# ---- Execution ------------------------------------------------------------ #

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _end_legacy_campaigns(skip: set[str]) -> list[str]:
    """End every RUNNING campaign not in skip. Returns list of ended IDs."""
    ended: list[str] = []
    running = marketing.get_campaigns(status="RUNNING")
    for c in running:
        cid = c.get("campaignId")
        name = c.get("campaignName")
        if cid in skip:
            continue
        print(f"  Ending legacy campaign [{cid}] {name!r}…")
        marketing.end_campaign(cid)
        ended.append(cid)
        time.sleep(1)
    return ended


def _progress(done, total, agg):
    print(f"    {done:>5}/{total:<5}  ok={agg['ok']}  failed={agg['failed']}")


def _apply(buckets: dict[str, list[str]]) -> None:
    print("\n=== Applying live ===\n")

    # 1. End all existing RUNNING campaigns FIRST.
    # eBay only allows a listing to be in one campaign at a time, so we
    # can't create new campaigns containing these listings while the
    # legacy Campaign 1 still has them. End-then-create is the only
    # path — ~5-10 min coverage gap during migration.
    print("  Ending existing RUNNING campaigns before migration…")
    ended = _end_legacy_campaigns(skip=set())
    print(f"    ended {len(ended)} campaign(s)")
    # Give eBay a moment to propagate the end before we reassign ads.
    print("  Sleeping 15s to let eBay propagate the campaign end…")
    time.sleep(15)

    # 2. Create four campaigns
    created: dict[str, str] = {}
    for name, lo, hi, rate in TIERS:
        campaign_name = f"{TIER_NAME_PREFIX}{name}"
        print(f"  Creating campaign {campaign_name!r} at {rate}%…")
        cid = marketing.create_campaign(
            campaign_name=campaign_name,
            ad_rate_cap_percent=rate,
            ad_rate_strategy="FIXED",
            auto_select_future_inventory=False,
        )
        created[name] = cid
        print(f"    created campaign_id={cid}")
        time.sleep(1)

    # 3. Bulk-add listings into each new campaign
    for name, _, _, rate in TIERS:
        cid = created[name]
        ids = buckets.get(name, [])
        if not ids:
            continue
        print(f"\n  Adding {len(ids):,} listings to {name} (cid={cid}, bid={rate}%)…")
        agg = marketing.bulk_create_ads(
            cid, ids, ad_rate_cap_percent=rate,
            batch_size=500, sleep_between=1.0, progress=_progress,
        )
        print(f"    done: ok={agg['ok']} failed={agg['failed']}")
        if agg["failures"]:
            print(f"    first 5 failures:")
            for f in agg["failures"][:5]:
                print(f"      {f}")

    # 4. Log to optimization_log
    with audit_db.connect() as conn:
        conn.execute(
            "INSERT INTO optimization_log (event, event_at, details) VALUES (?, ?, ?)",
            ("TIER_CAMPAIGNS_BUILT", _now_iso(),
             f"Created {len(TIERS)} tier campaigns + migrated listings. "
             f"cids={created}. "
             f"counts=" + ", ".join(f"{n}:{len(buckets.get(n, []))}"
                                    for n, _, _, _ in TIERS)),
        )
        conn.commit()
    print("\n=== Done ===")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--yes", action="store_true",
                   help="skip the interactive confirmation prompt")
    args = p.parse_args()

    buckets = _classify_listings()
    _print_plan(buckets)

    if not args.apply:
        print("[DRY RUN] pass --apply to build campaigns live.")
        return 0

    if not args.yes:
        print(f"This will:")
        print(f"  • Create {len(TIERS)} new campaigns")
        total_ads = sum(len(buckets[n]) for n, _, _, _ in TIERS)
        print(f"  • Add {total_ads:,} ads across them "
              f"(~{total_ads // 500 + 1} bulk API calls)")
        print(f"  • End your existing 'Campaign 1'")
        print(f"  • Leave {len(buckets['_EXCLUDED']):,} under-£10 listings out of ads")
        confirm = input("\nProceed? [yes/no] ").strip().lower()
        if confirm not in ("yes", "y"):
            print("Aborted."); return 1

    _apply(buckets)
    return 0


if __name__ == "__main__":
    sys.exit(main())
