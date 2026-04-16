#!/usr/bin/env python3
"""
Consolidate every Bryan Robson listing into the correct eBay category:

    97085  Sports Memorabilia → Football Mem → Autographs (Original)
                              → Signed Photos: Retired Players

BR retired in 1994 so "Retired Players" is the natural home. Today his
167 listings are spread across 8 categories — many wrong (Films & TV,
Darts, Signed Shirts) due to "sell similar" propagating bad sources.

Usage
-----
    # Dry-run (default) — prints the per-listing plan, no API calls
    python scripts/recat_bryan_robson.py

    # Live — actually call ReviseFixedPriceItem
    python scripts/recat_bryan_robson.py --apply

    # Live with rate limit override (default 1 call/sec)
    python scripts/recat_bryan_robson.py --apply --rate 0.8
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

from pipeline import audit_db, lister


TARGET_CATEGORY_ID = "97085"
TARGET_CATEGORY_NAME = (
    "Sports Memorabilia:Football Memorabilia:Autographs (Original):"
    "Signed Photos:Retired Players"
)
SIGNER_FILTER = "%bryan robson%"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_candidates(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT item_id, title, category_id, category_name,
               watch_count, price_gbp
        FROM listings
        WHERE LOWER(title) LIKE ?
          AND category_id IS NOT NULL
        ORDER BY category_id, item_id
        """,
        (SIGNER_FILTER,),
    ).fetchall()
    return [dict(r) for r in rows]


def _summarise(candidates: list[dict]) -> dict[str, list[dict]]:
    """Group by current category for the summary block."""
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for c in candidates:
        by_cat[c["category_id"]].append(c)
    return by_cat


def _print_plan(candidates: list[dict]) -> int:
    """Print the dry-run plan. Returns count of listings to revise."""
    by_cat = _summarise(candidates)
    print(f"Target category: {TARGET_CATEGORY_ID}  {TARGET_CATEGORY_NAME}\n")
    print(f"=== Plan summary ({len(candidates)} BR listings) ===")

    to_change = 0
    for cat_id, items in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        cat_name = items[0]["category_name"] or "(unknown)"
        if cat_id == TARGET_CATEGORY_ID:
            print(f"  ✓ {cat_id:>6}  {len(items):>3}  ALREADY in target — skip")
        else:
            to_change += len(items)
            print(f"  → {cat_id:>6}  {len(items):>3}  {cat_name}")

    print(f"\nWill revise: {to_change} listings  "
          f"(skip: {len(candidates) - to_change} already in target)\n")
    return to_change


def _print_first_n_per_cat(candidates: list[dict], n: int = 3) -> None:
    by_cat = _summarise(candidates)
    print("=== Sample of items being moved (first 3 per source cat) ===")
    for cat_id, items in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        if cat_id == TARGET_CATEGORY_ID:
            continue
        print(f"\nFrom cat {cat_id} ({items[0]['category_name'] or '?'}):")
        for c in items[:n]:
            wc = c["watch_count"] or 0
            price = f"£{c['price_gbp']}" if c["price_gbp"] else "?"
            print(f"  [{c['item_id']}] {price:>6}  watch={wc:<3}  {c['title']}")
        if len(items) > n:
            print(f"  …+ {len(items) - n} more")


def _apply(conn, candidates: list[dict], rate_per_sec: float) -> None:
    """Call ReviseFixedPriceItem for each non-target listing."""
    sleep = 1.0 / max(rate_per_sec, 0.1)
    targets = [c for c in candidates if c["category_id"] != TARGET_CATEGORY_ID]
    print(f"Applying category change to {len(targets)} listings "
          f"(rate={rate_per_sec}/s, ETA ~{len(targets) * sleep / 60:.1f}m)\n")

    audit_db.set_meta(conn, "br_recat_started_at", _now())
    conn.execute(
        "INSERT INTO optimization_log (event, event_at, details) VALUES (?, ?, ?)",
        ("BR_RECAT_START", _now(),
         f"Begin consolidating {len(targets)} BR listings into cat {TARGET_CATEGORY_ID}")
    )
    conn.commit()

    ok = 0
    fail = 0
    start = time.monotonic()
    for i, c in enumerate(targets, 1):
        try:
            result = lister.revise_listing(
                c["item_id"],
                new_category_id=TARGET_CATEGORY_ID,
                confirm=True,
            )
            ack = result.get("ack")
            warnings = result.get("warnings") or []
            if ack in ("Success", "Warning"):
                ok += 1
                # Update local cache so re-runs don't re-attempt this row.
                conn.execute(
                    "UPDATE listings SET category_id = ?, category_name = ? "
                    "WHERE item_id = ?",
                    (TARGET_CATEGORY_ID, TARGET_CATEGORY_NAME, c["item_id"]),
                )
                if warnings:
                    msgs = "; ".join(w.get("short", "") for w in warnings if w.get("short"))
                    print(f"  ⚠ [{c['item_id']}]  {ack}: {msgs}")
            else:
                fail += 1
                msgs = "; ".join(w.get("long", "") for w in warnings if w.get("long"))
                print(f"  ✗ [{c['item_id']}]  Ack={ack}  {msgs}")
        except Exception as e:
            fail += 1
            print(f"  ✗ [{c['item_id']}]  EXCEPTION: {e}")

        if i % 20 == 0:
            elapsed = time.monotonic() - start
            print(f"  {i}/{len(targets)} ({i/elapsed:.1f}/s, ok={ok} fail={fail})")
            conn.commit()
        time.sleep(sleep)

    conn.commit()
    elapsed = time.monotonic() - start
    print(f"\nDone: {ok} succeeded, {fail} failed in {elapsed:.0f}s")
    conn.execute(
        "INSERT INTO optimization_log (event, event_at, details) VALUES (?, ?, ?)",
        ("BR_RECAT_DONE", _now(),
         f"Consolidated {ok} BR listings into cat {TARGET_CATEGORY_ID} "
         f"({fail} failures)")
    )
    conn.commit()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true",
                   help="actually call ReviseFixedPriceItem (without this it's a dry-run)")
    p.add_argument("--rate", type=float, default=1.0,
                   help="API calls per second (default 1.0 — gentle)")
    p.add_argument("--yes", action="store_true",
                   help="skip the interactive 'are you sure?' prompt (for non-tty)")
    args = p.parse_args()

    with audit_db.connect() as conn:
        candidates = _load_candidates(conn)
        if not candidates:
            print("No BR listings found in cache. Run a deep fetch first.")
            return 1

        to_change = _print_plan(candidates)
        if to_change == 0:
            print("Nothing to do — all BR listings already in target category.")
            return 0

        _print_first_n_per_cat(candidates)

        if not args.apply:
            print("\n[DRY RUN] Pass --apply to revise these listings live.")
            return 0

        if not args.yes:
            print()
            confirm = input("Proceed with live revisions? [yes/no] ").strip().lower()
            if confirm not in ("yes", "y"):
                print("Aborted.")
                return 1

        _apply(conn, candidates, args.rate)

    return 0


if __name__ == "__main__":
    sys.exit(main())
