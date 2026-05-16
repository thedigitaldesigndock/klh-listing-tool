#!/usr/bin/env python3
"""
Consolidate every Jeremy Clarkson listing into the correct eBay category:

    35030  Films & TV → TV Memorabilia → Autographs (Original Certified) → Male

Today his 205 listings are spread across 9+ categories (see audit DB
category distribution), with the usual "sell similar" pollution:
40 in "Female", 14 in Football Memorabilia, and various Other buckets.

Usage
-----
    python scripts/recat_clarkson.py               # dry-run
    python scripts/recat_clarkson.py --apply --yes
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

from pipeline import audit_db, lister


TARGET_CATEGORY_ID = "35030"
TARGET_CATEGORY_NAME = (
    "Films & TV:TV Memorabilia:Autographs:Original (Certified):Male"
)
SIGNER_FILTER = "%jeremy clarkson%"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_candidates(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT item_id, title, category_id, category_name, watch_count, price_gbp
        FROM listings
        WHERE LOWER(title) LIKE ?
          AND category_id IS NOT NULL
        ORDER BY category_id, item_id
        """, (SIGNER_FILTER,)
    ).fetchall()
    return [dict(r) for r in rows]


def _print_plan(candidates: list[dict]) -> int:
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for c in candidates:
        by_cat[c["category_id"]].append(c)
    print(f"Target category: {TARGET_CATEGORY_ID}  {TARGET_CATEGORY_NAME}\n")
    print(f"=== Plan summary ({len(candidates)} Clarkson listings) ===")
    to_change = 0
    for cat_id, items in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        cat_name = items[0]["category_name"] or "(unknown)"
        if cat_id == TARGET_CATEGORY_ID:
            print(f"  ✓ {cat_id:>6}  {len(items):>3}  ALREADY in target — skip")
        else:
            to_change += len(items)
            print(f"  → {cat_id:>6}  {len(items):>3}  {cat_name}")
    print(f"\nWill revise: {to_change} listings\n")
    return to_change


def _apply(conn, candidates: list[dict], rate_per_sec: float) -> None:
    sleep = 1.0 / max(rate_per_sec, 0.1)
    targets = [c for c in candidates if c["category_id"] != TARGET_CATEGORY_ID]
    print(f"Applying category change to {len(targets)} listings "
          f"(rate={rate_per_sec}/s, ETA ~{len(targets) * sleep / 60:.1f}m)\n")
    conn.execute(
        "INSERT INTO optimization_log (event, event_at, details) VALUES (?, ?, ?)",
        ("CLARKSON_RECAT_START", _now(),
         f"Begin consolidating {len(targets)} Clarkson listings into cat {TARGET_CATEGORY_ID}")
    )
    conn.commit()
    ok = fail = 0
    start = time.monotonic()
    for i, c in enumerate(targets, 1):
        try:
            # 35030 (and the Films & TV autograph cats generally) require a
            # ConditionID. Signed memorabilia = 3000 (Used). Sending it
            # unconditionally is safe — source cats accept it too.
            result = lister.revise_listing(
                c["item_id"], new_category_id=TARGET_CATEGORY_ID,
                new_condition_id=3000, confirm=True,
            )
            if result.get("ack") in ("Success", "Warning"):
                ok += 1
                conn.execute(
                    "UPDATE listings SET category_id = ?, category_name = ? WHERE item_id = ?",
                    (TARGET_CATEGORY_ID, TARGET_CATEGORY_NAME, c["item_id"]),
                )
            else:
                fail += 1
                warnings = result.get("warnings") or []
                msgs = "; ".join(w.get("long","") for w in warnings if w.get("long"))
                print(f"  ✗ [{c['item_id']}]  Ack={result.get('ack')}  {msgs}")
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
        ("CLARKSON_RECAT_DONE", _now(),
         f"Consolidated {ok} Clarkson listings into cat {TARGET_CATEGORY_ID} ({fail} fails)")
    )
    conn.commit()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--rate", type=float, default=1.0)
    p.add_argument("--yes", action="store_true")
    args = p.parse_args()
    with audit_db.connect() as conn:
        candidates = _load_candidates(conn)
        if not candidates:
            print("No Clarkson listings in cache with category_id set.")
            return 1
        to_change = _print_plan(candidates)
        if to_change == 0:
            print("All listings already in target.")
            return 0
        if not args.apply:
            print("[DRY RUN] Pass --apply to revise live.")
            return 0
        if not args.yes:
            if input("Proceed? [yes/no] ").strip().lower() not in ("yes","y"):
                return 1
        _apply(conn, candidates, args.rate)
    return 0


if __name__ == "__main__":
    sys.exit(main())
