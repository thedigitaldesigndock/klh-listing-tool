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


def _get_markdown_listing_ids() -> set[str]:
    """Return the set of listing_ids covered by any RUNNING markdown sale.
    Used to exclude already-discounted items from SOTIB so we don't
    double-discount on top of Kim's own sale events.
    """
    from ebay_api.marketing import _request, BASE
    out: set[str] = set()
    try:
        _, body, _ = _request(
            "GET",
            f"{BASE}/promotion?marketplace_id=EBAY_GB&promotion_status=RUNNING&limit=50&offset=0",
        )
        promos = (body or {}).get("promotions") or []
    except Exception as e:
        print(f"  WARN: couldn't fetch promotions: {str(e)[:120]}")
        return out
    for p in promos:
        if p.get("promotionType") != "MARKDOWN_SALE":
            continue
        pid = p.get("promotionId")
        if not pid:
            continue
        try:
            _, detail, _ = _request("GET", f"{BASE}/item_price_markdown/{pid}")
        except Exception as e:
            print(f"  WARN: couldn't fetch markdown {pid}: {str(e)[:120]}")
            continue
        for sid in detail.get("selectedInventoryDiscounts") or []:
            crit = sid.get("inventoryCriterion") or {}
            for lid in crit.get("listingIds") or []:
                out.add(str(lid))
    return out


def _signer_from_title(title: str) -> str:
    """Extract the shortest stable signer-name form from a listing title.
    "Sir David Attenborough Hand Signed A4 Photo …" → "david attenborough"
    Stripping the honorific lets the substring-match catch BOTH variants
    in eligible titles (with and without the title).
    """
    if not title:
        return ""
    # Everything before the first "Signed" is the signer (plus maybe " Hand").
    head = title.split("Signed", 1)[0].rstrip().lower()
    if head.endswith(" hand"):
        head = head[:-5].rstrip()
    # Strip leading honorific so the substring match catches both forms.
    for honor in ("sir ", "dame ", "lord ", "lady ", "dr ", "rev "):
        if head.startswith(honor):
            head = head[len(honor):]
            break
    return head.strip()


def _signers_in_active_markdowns(markdown_ids: set[str]) -> set[str]:
    """Auto-derive signer-name exclusions from titles of listings in
    active markdown sales. Replaces the old manually-managed
    sotib_exclusions.yaml — the YAML kept catching Kim a week after a
    sale ended because nobody (me included) cleared the entry. Now the
    source of truth is "what's actually in a RUNNING markdown promo
    right now," so the exclusion auto-clears the second the sale ends.
    """
    if not markdown_ids:
        return set()
    signers: set[str] = set()
    with audit_db.connect(readonly=True) as conn:
        placeholders = ",".join("?" * len(markdown_ids))
        rows = conn.execute(
            f"SELECT title FROM listings WHERE item_id IN ({placeholders})",
            list(markdown_ids),
        ).fetchall()
        for r in rows:
            s = _signer_from_title(r["title"] or "")
            if s:
                signers.add(s)
    return signers


def _build_plan() -> list[dict]:
    """For each eligible listing, compute tier + offer. Returns ready-to-send list."""
    print("Fetching eligible listings from Negotiation API…")
    eligible = negotiation.find_eligible_items(limit=0)
    print(f"  {len(eligible)} eligible")

    # Exclude listings already in an active markdown — sending SOTIB on
    # top of an existing sale double-discounts and undercuts the seller.
    on_markdown = _get_markdown_listing_ids()
    print(f"  {len(on_markdown)} already in active markdown sales (will be skipped)")

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

    # Backfill missing prices via GetItem (cache miss = recently listed).
    # Pre-50k quota this would have been too costly; with the lifted cap
    # we can afford it and stop silently dropping ~95% of eligible items.
    missing = [lid for lid in eligible if lid not in prices]
    if missing:
        from ebay_api import trading
        print(f"  fetching {len(missing)} prices not in audit cache…")
        for lid in missing:
            try:
                raw = trading.get_item(lid, include_description=False)
                price_str = (raw.get("StartPrice")
                             or raw.get("BuyItNowPrice")
                             or raw.get("SellingStatus", {}).get("CurrentPrice"))
                if isinstance(price_str, dict):
                    price_str = price_str.get("_text") or price_str.get("text")
                if price_str:
                    prices[lid] = float(str(price_str).replace(",", ""))
                titles.setdefault(lid, raw.get("Title", ""))
            except Exception as e:
                print(f"    GetItem {lid}: {str(e)[:80]}")

    # Per-signer exclusions — auto-derived from titles of listings in active
    # markdown sales. When Kim runs a markdown promo on Attenborough, all of
    # his listings in the promo carry his name in the title; we extract the
    # set of names and exclude any eligible listing matching one of them.
    # Auto-clears the second the markdown ends — no YAML to forget about.
    excluded_signers = sorted(_signers_in_active_markdowns(on_markdown))
    if excluded_signers:
        print(f"  excluding {len(excluded_signers)} signer(s) auto-derived "
              f"from active markdown listings: {', '.join(excluded_signers)}")

    # Manual override fallback (deprecated). Honoured ONLY for entries with
    # an `until: YYYY-MM-DD` value that hasn't passed — entries without a date
    # are ignored and warned about. This is the escape hatch for sales Kim
    # runs WITHOUT using eBay's markdown promo (e.g. hand-edited prices).
    import yaml as _yaml
    from pathlib import Path as _Path
    from datetime import date as _date
    excl_path = _Path(__file__).resolve().parent.parent / "presets" / "sotib_exclusions.yaml"
    if excl_path.exists():
        try:
            cfg = _yaml.safe_load(excl_path.read_text()) or {}
            today = _date.today()
            manual: list[str] = []
            for entry in (cfg.get("excluded_signers") or []):
                if isinstance(entry, dict):
                    name = (entry.get("name") or "").lower().strip()
                    until = entry.get("until")
                    if not name:
                        continue
                    if until and _date.fromisoformat(str(until)) >= today:
                        manual.append(name)
                    else:
                        print(f"  WARN: manual exclusion {name!r} has expired "
                              f"or has no `until:` date — ignoring")
                else:
                    print(f"  WARN: manual exclusion {entry!r} has no "
                          f"`until: YYYY-MM-DD` — ignoring (use the new "
                          f"`- {{name: ..., until: ...}}` format)")
            if manual:
                print(f"  + {len(manual)} manual override(s): {', '.join(manual)}")
                for m in manual:
                    if m not in excluded_signers:
                        excluded_signers.append(m)
        except Exception as e:
            print(f"  WARN: couldn't parse sotib_exclusions.yaml: {str(e)[:80]}")

    plan: list[dict] = []
    skipped_under_10 = 0
    skipped_no_price = 0
    skipped_excluded = 0
    skipped_markdown = 0
    mug_capped = 0
    for lid in eligible:
        # Active markdown sale on this listing — never SOTIB on top, or
        # we double-discount and undercut Kim's own sale price.
        # (See 2026-05-06 Attenborough sale incident: SOTIB 10% on top
        # of the 26%-off £48.09 price = buyer demanded £4.81 refund.)
        if lid in on_markdown:
            skipped_markdown += 1
            continue
        price = prices.get(lid)
        if price is None:
            skipped_no_price += 1
            continue
        if price < 10:
            skipped_under_10 += 1
            continue
        title_lc = (titles.get(lid) or "").lower()
        # Excluded signer (sale-event running) — no offer at all.
        if any(s in title_lc for s in excluded_signers):
            skipped_excluded += 1
            continue
        # Mugs have tight margins — cap at 5% off regardless of tier.
        if "mug" in title_lc:
            mug_capped += 1
            plan.append({
                "listingId":          lid,
                "price":              price,
                "tier":               "MUG",
                "discountPercentage": "5",
                "title":              titles.get(lid, ""),
            })
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
    print(f"  excluded signers:    {skipped_excluded}")
    print(f"  on markdown sale:    {skipped_markdown}")
    print(f"  mugs (5% cap):       {mug_capped}")
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
