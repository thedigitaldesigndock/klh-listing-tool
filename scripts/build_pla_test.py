#!/usr/bin/env python3
"""
Build the Promoted Listings Advanced (PLA / pay-per-click) test campaign.

Per Peter's plan:
  * 25 signers, each with a dedicated ad group
  * Keyword per signer: "<Signer Name> signed" (PHRASE match)
  * Ads = that signer's LIVE listings priced >= £15 (no cheap cards)
  * Daily budget: £5
  * Starter bid: £0.08 per click

One API sequence end-to-end:
  1. Create PLA campaign (CPC, £5/day ceiling)
  2. For each signer:
     a. Create ad_group with defaultBid £0.08
     b. Bulk-add that signer's ≥£15 listings as ads
     c. Create PHRASE-match keyword pointing to the ad_group

Idempotent-ish: if a campaign with the same name already exists you'll
see failures. Delete via eBay or Seller Hub and re-run, or use --name
to change.

Usage:
    python scripts/build_pla_test.py                 # dry-run
    python scripts/build_pla_test.py --apply --yes
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone

from pipeline import audit_db
from ebay_api import marketing


# --------------------------------------------------------------------------- #
# Config — edit this list to tune the 25 signers targeted.
# Each entry: (signer_canonical_name, title_search_substring)
# The search substring is used to pull their listings from audit DB.
# --------------------------------------------------------------------------- #

SIGNERS = [
    # Football
    ("Teddy Sheringham",   "teddy sheringham"),
    ("Bryan Robson",       "bryan robson"),
    ("Kevin Keegan",       "kevin keegan"),
    ("Pat Jennings",       "pat jennings"),
    ("Frank Lampard",      "frank lampard"),
    ("Billy Sharp",        "billy sharp"),
    ("Matt Le Tissier",    "matt le tissier"),
    ("Henrik Larsson",     "henrik larsson"),
    ("Javier Zanetti",     "javier zanetti"),
    ("Giorgio Chiellini",  "giorgio chiellini"),

    # TV / Film
    ("Jackie Chan",        "jackie chan"),
    ("Jeremy Clarkson",    "jeremy clarkson"),
    ("Dan Aykroyd",        "dan aykroyd"),
    ("Dan Castellaneta",   "dan castellaneta"),
    ("Joanna Lumley",      "joanna lumley"),
    ("David Naughton",     "david naughton"),
    ("Tim Allen",          "tim allen"),
    ("Martin Clunes",      "martin clunes"),
    ("Ricky Gervais",      "ricky gervais"),
    ("Sophia Loren",       "sophia loren"),

    # Sports (non-football)
    ("Mario Andretti",     "mario andretti"),
    ("Shaquille O'Neal",   "shaquille o'neal"),
    ("Ian Botham",         "ian botham"),

    # Misc
    ("Pete Best",          "pete best"),
    ("Erin Gray",          "erin gray"),
]

MIN_PRICE_GBP     = 15.0    # only target listings ≥ this for PLA
DEFAULT_BID_GBP   = 0.08    # per click
DAILY_BUDGET_GBP  = 5.0
CAMPAIGN_NAME     = "KLH PLA Test 2026-04"
MATCH_TYPE        = "PHRASE"


# --------------------------------------------------------------------------- #
# Plan — read-only, fast
# --------------------------------------------------------------------------- #

def _build_plan() -> list[dict]:
    """For each signer, pull their ≥£15 live listings from audit DB."""
    plan: list[dict] = []
    with audit_db.connect(readonly=True) as conn:
        for signer, needle in SIGNERS:
            rows = conn.execute(
                "SELECT item_id, price_gbp FROM listings "
                "WHERE listing_type='FixedPriceItem' "
                "  AND (quantity_available IS NULL OR quantity_available > 0) "
                "  AND price_gbp >= ? "
                "  AND LOWER(title) LIKE ?",
                (MIN_PRICE_GBP, f"%{needle}%"),
            ).fetchall()
            plan.append({
                "signer":    signer,
                "keyword":   f"{signer} signed",
                "listings":  [r["item_id"] for r in rows],
                "min_price": min((r["price_gbp"] for r in rows), default=None),
                "max_price": max((r["price_gbp"] for r in rows), default=None),
            })
    return plan


def _print_plan(plan: list[dict]) -> None:
    print(f"\n=== PLA Plan ===")
    print(f"  Campaign:      {CAMPAIGN_NAME!r}")
    print(f"  Daily budget:  £{DAILY_BUDGET_GBP:.2f}")
    print(f"  Starter bid:   £{DEFAULT_BID_GBP:.2f} per click")
    print(f"  Match type:    {MATCH_TYPE}")
    print(f"  Min price:     £{MIN_PRICE_GBP:.2f}\n")
    print(f"{'Signer':<28} {'Keyword':<34} {'Ads':>5} {'£ range':>14}")
    print("-" * 86)
    total_ads = 0
    empty_groups = []
    for p in plan:
        n = len(p["listings"])
        total_ads += n
        rng = "-"
        if n:
            rng = f"£{p['min_price']:.0f}-£{p['max_price']:.0f}"
        print(f"{p['signer']:<28} {p['keyword']:<34} {n:>5} {rng:>14}")
        if n == 0:
            empty_groups.append(p["signer"])
    print(f"\n  Ad groups:     {len([p for p in plan if p['listings']])}")
    print(f"  Total ads:     {total_ads}")
    if empty_groups:
        print(f"  ⚠ Empty groups (no eligible listings): {empty_groups}")


# --------------------------------------------------------------------------- #
# Apply — live eBay writes
# --------------------------------------------------------------------------- #

def _apply(plan: list[dict]) -> None:
    print(f"\n=== Applying live ===\n")
    cid = marketing.create_pla_campaign(
        campaign_name=CAMPAIGN_NAME,
        daily_budget_gbp=DAILY_BUDGET_GBP,
    )
    print(f"Created PLA campaign {cid} ({CAMPAIGN_NAME!r}, £{DAILY_BUDGET_GBP}/day)\n")
    time.sleep(1)

    total_ads_ok = total_ads_fail = 0
    total_kw_ok = total_kw_fail = 0

    for p in plan:
        if not p["listings"]:
            print(f"  skip {p['signer']} (no eligible listings)")
            continue

        # 1. Create ad_group
        ag_id = marketing.create_ad_group(
            cid,
            name=p["signer"],
            default_bid_gbp=DEFAULT_BID_GBP,
        )
        print(f"  {p['signer']:<28} ag={ag_id}  listings={len(p['listings'])}")

        # 2. Bulk-add ads
        agg = marketing.bulk_create_pla_ads(
            cid, ag_id, p["listings"],
            batch_size=500, sleep_between=0.5,
        )
        total_ads_ok += agg["ok"]
        total_ads_fail += agg["failed"]
        if agg["failed"]:
            # Common failure: listing already in another campaign (e.g., our
            # tier campaigns). That blocks PLA from re-using it. Log + move on.
            first = agg["failures"][:2]
            for f in first:
                code = (f.get("errors") or [{}])[0].get("errorId")
                print(f"    ⚠ ad fail: listing={f['listing_id']} code={code}")

        # 3. Create keyword pointing at this ad_group
        kw_req = [{
            "adGroupId":   ag_id,
            "keywordText": p["keyword"],
            "matchType":   MATCH_TYPE,
            "bid":         {"value": f"{DEFAULT_BID_GBP:.2f}", "currency": "GBP"},
        }]
        kw_agg = marketing.bulk_create_pla_keywords(cid, kw_req)
        total_kw_ok += kw_agg["ok"]
        total_kw_fail += kw_agg["failed"]
        if kw_agg["failed"]:
            for f in kw_agg["failures"]:
                print(f"    ⚠ kw fail: {f}")
        time.sleep(0.5)

    print(f"\n=== Done ===")
    print(f"  Ads:       ok={total_ads_ok} failed={total_ads_fail}")
    print(f"  Keywords:  ok={total_kw_ok} failed={total_kw_fail}")

    # Log to optimization_log
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with audit_db.connect() as conn:
        conn.execute(
            "INSERT INTO optimization_log (event, event_at, details) VALUES (?, ?, ?)",
            ("PLA_CAMPAIGN_BUILT", now,
             f"Created PLA test campaign {cid}. ads_ok={total_ads_ok} "
             f"kw_ok={total_kw_ok}. Budget £{DAILY_BUDGET_GBP}/day, bid £{DEFAULT_BID_GBP}/click."),
        )
        conn.commit()
    print(f"\nCampaign ID: {cid}  (end via Seller Hub or marketing.end_campaign(cid) if needed)")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--yes", action="store_true", help="skip interactive confirmation")
    args = p.parse_args()

    plan = _build_plan()
    _print_plan(plan)

    if not args.apply:
        print("\n[DRY RUN] pass --apply to create the campaign.")
        return 0

    if not args.yes:
        confirm = input("\nProceed with live campaign creation? [yes/no] ").strip().lower()
        if confirm not in ("yes", "y"):
            print("Aborted.")
            return 1

    _apply(plan)
    return 0


if __name__ == "__main__":
    sys.exit(main())
