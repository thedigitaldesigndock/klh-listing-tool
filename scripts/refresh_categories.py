#!/usr/bin/env python3
"""
One-shot category refresh: pull category_id + category_name for every
listing in the audit cache that's missing it. Uses GetItem (same code
path as deep_fetch_signer.py) so we get specifics + picture_url as a
side benefit.

Idempotent — skips listings that already have category_id unless --force.

Usage:
    python scripts/refresh_categories.py
    python scripts/refresh_categories.py --sleep 0.5   # 2 req/sec
    python scripts/refresh_categories.py --limit 500   # small test batch
"""
from __future__ import annotations

import argparse
import sys
import time

from pipeline import audit_db
from ebay_api import trading


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sleep", type=float, default=0.5,
                   help="Seconds between GetItem calls (default 0.5 = 2/s).")
    p.add_argument("--limit", type=int, default=0,
                   help="Stop after N items (0 = all).")
    p.add_argument("--force", action="store_true",
                   help="Refetch items that already have category_id.")
    args = p.parse_args()

    with audit_db.connect() as conn:
        sql = "SELECT item_id FROM listings WHERE 1=1"
        if not args.force:
            sql += " AND category_id IS NULL"
        sql += " ORDER BY item_id"
        rows = conn.execute(sql).fetchall()
        ids = [r["item_id"] for r in rows]
        if args.limit:
            ids = ids[: args.limit]

    total = len(ids)
    if not total:
        print("Nothing to refresh.")
        return 0

    eta_min = total * args.sleep / 60
    print(f"Refreshing category for {total} listings "
          f"(rate={1/max(args.sleep,0.01):.1f}/s, ETA ~{eta_min:.0f}m)\n")

    ok = fail = 0
    start = time.monotonic()
    for i, (item_id, deep) in enumerate(
        trading.get_items_bulk(ids, sleep=args.sleep), 1
    ):
        try:
            with audit_db.connect() as conn:
                audit_db.upsert_deep(conn, item_id, deep)
            ok += 1
        except Exception as e:
            fail += 1
            print(f"  ✗ {item_id}: {e}", flush=True)
        if i % 100 == 0:
            elapsed = time.monotonic() - start
            rate = i / max(elapsed, 1)
            remain = (total - i) / max(rate, 0.01) / 60
            print(f"  {i}/{total}  rate={rate:.1f}/s  ok={ok} fail={fail}  "
                  f"remaining~{remain:.0f}m", flush=True)

    elapsed = time.monotonic() - start
    print(f"\nDone: {ok} ok, {fail} fail in {elapsed/60:.1f}m")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
