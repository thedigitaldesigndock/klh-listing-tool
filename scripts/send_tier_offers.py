#!/usr/bin/env python3
"""
Send tier-aware SOTIB (Send Offer To Interested Buyers) offers.

For every eligible listing eBay returns (has watchers / recently viewed),
compute a price-band-based discount and fire sendOfferToInterestedBuyers.

Tier → discount mapping (mirrors the PLS ad-tier price bands):

    < £10                    SKIP (margin too thin)
    £10-£14.99   (BUDGET)     18%
    £15-£29.99   (STANDARD)   15%
    £30-£49.99   (PREMIUM)    12%
    £50+         (PREMIUM+)   10%

Offer duration: 2 days (eBay auto-extends under the hood).
quantity: 1 (matches the new unique-item inventory model).
allowBuyerCounterOffer: False.

Run ad-hoc or on a schedule. Idempotent at the eBay side — if an
offer is already live on a listing, eBay either replaces or ignores
the new one. Our script just logs what we tried.

Usage:
    python scripts/send_tier_offers.py              # dry-run
    python scripts/send_tier_offers.py --apply
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

from pipeline import audit_db
from ebay_api import negotiation


# Keep in sync with dashboard/ads_panel.TIER_DEFS + scripts/daily_ad_housekeeping.
TIER_OFFER_PCT: list[tuple[float, float, int, str]] = [
    # (price_lo, price_hi, discount_pct, tier_name)
    (10.0,    15.0,  18, "BUDGET"),
    (15.0,    30.0,  15, "STANDARD"),
    (30.0,    50.0,  12, "PREMIUM"),
    (50.0,   1e12,   10, "PREMIUM_PLUS"),
]


def _tier_for_price(p):
    if p is None or p < 10:
        return None
    for lo, hi, pct, name in TIER_OFFER_PCT:
        if lo <= p < hi:
            return (pct, name)
    return None


def _build_plan() -> list[dict]:
    """For each eligible listing, compute tier + offer. Returns ready-to-send list."""
    print("Fetching eligible listings from Negotiation API…")
    eligible = negotiation.find_eligible_items(limit=0)
    print(f"  {len(eligible)} eligible")

    # Cross-reference with audit DB for prices
    with audit_db.connect(readonly=True) as conn:
        prices: dict[str, float] = {}
        titles: dict[str, str] = {}
        rows = conn.execute(
            "SELECT item_id, price_gbp, title FROM listings WHERE item_id IN ("
            + ",".join("?" * len(eligible)) + ")",
            eligible,
        ).fetchall()
        for r in rows:
            prices[r["item_id"]] = r["price_gbp"]
            titles[r["item_id"]] = r["title"]

    plan: list[dict] = []
    skipped_under_10 = 0
    skipped_no_price = 0
    for lid in eligible:
        price = prices.get(lid)
        if price is None:
            skipped_no_price += 1
            continue
        if price < 10:
            skipped_under_10 += 1
            continue
        tier = _tier_for_price(price)
        if tier is None:
            continue
        pct, tier_name = tier
        plan.append({
            "listingId":          lid,
            "price":              price,
            "tier":               tier_name,
            "discountPercentage": str(pct),
            "title":              titles.get(lid, ""),
        })
    print(f"  under £10 (skipped): {skipped_under_10}")
    print(f"  no price in cache:   {skipped_no_price}")
    print(f"  will send offers:    {len(plan)}")
    return plan


def _print_breakdown(plan: list[dict]) -> None:
    print("\n=== Tier breakdown ===")
    from collections import Counter
    tiers = Counter((p["tier"], p["discountPercentage"]) for p in plan)
    for (t, pct), n in sorted(tiers.items()):
        print(f"  {t:<14} {pct}%:  {n}")

    # Sample
    print("\n=== Sample (first 5 per tier) ===")
    shown: dict[str, int] = {}
    for p in plan:
        if shown.get(p["tier"], 0) >= 5:
            continue
        shown[p["tier"]] = shown.get(p["tier"], 0) + 1
        print(f"  [{p['listingId']}] {p['tier']:<14} {p['discountPercentage']}% "
              f"£{p['price']:<7} {p['title'][:55]}")


def _apply(plan: list[dict], rate_per_sec: float = 2.0) -> None:
    print(f"\n=== Applying — sending {len(plan)} offers ===\n")
    offers = [
        {
            "listingId":              p["listingId"],
            "discountPercentage":     p["discountPercentage"],
            "duration":               "DAYS_2",
            "quantity":               1,
            "allowBuyerCounterOffer": False,
        }
        for p in plan
    ]

    def _progress(done, total, agg):
        print(f"  {done}/{total}  ok={agg['ok']}  failed={agg['failed']}")

    # Single-item calls: eBay's send_offer batch endpoint is atomic — if ANY
    # item in a batch has a pre-existing offer or other conflict, the whole
    # batch fails. One-at-a-time gives us per-listing error isolation.
    agg = negotiation.send_offers(
        offers, batch_size=1, sleep_between=1.0 / max(rate_per_sec, 0.1),
        progress=_progress,
    )
    print(f"\n=== Done — ok={agg['ok']} failed={agg['failed']} ===")
    if agg["failures"]:
        print("First 5 failures:")
        for f in agg["failures"][:5]:
            print(f"  {f}")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with audit_db.connect() as conn:
        conn.execute(
            "INSERT INTO optimization_log (event, event_at, details) VALUES (?, ?, ?)",
            ("SOTIB_BATCH_SENT", now,
             f"Tier-aware SOTIB: {agg['ok']} offers sent, {agg['failed']} failed. "
             f"Tiers: BUDGET 18% / STANDARD 15% / PREMIUM 12% / PREMIUM+ 10%."),
        )
        conn.commit()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--yes", action="store_true")
    args = p.parse_args()

    plan = _build_plan()
    _print_breakdown(plan)

    if not args.apply:
        print("\n[DRY RUN] pass --apply to send live.")
        return 0

    if not args.yes:
        confirm = input("\nSend offers to these listings? [yes/no] ").strip().lower()
        if confirm not in ("yes", "y"):
            print("Aborted.")
            return 1

    _apply(plan)
    return 0


if __name__ == "__main__":
    sys.exit(main())
