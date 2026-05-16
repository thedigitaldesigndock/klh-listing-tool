#!/usr/bin/env python3
"""
Deep-fetch all listings for a signer (GetItem sweep) to populate
picture_url + full specifics_json in the audit cache. Required before
/revisions can show thumbnails or the planner can read Team IS.

Usage:
    python scripts/deep_fetch_signer.py --signer "Pat Jennings"
    python scripts/deep_fetch_signer.py --signer "Pat Jennings" --force  # refetch even if deep_fetched_at set
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
    p.add_argument("--signer", required=True)
    p.add_argument("--force", action="store_true",
                   help="Refetch even items with deep_fetched_at already set.")
    p.add_argument("--sleep", type=float, default=0.5)
    args = p.parse_args()

    filt = f"%{args.signer.lower()}%"
    with audit_db.connect() as conn:
        sql = "SELECT item_id FROM listings WHERE LOWER(title) LIKE ?"
        if not args.force:
            sql += " AND (deep_fetched_at IS NULL OR picture_url IS NULL OR picture_url = '')"
        rows = conn.execute(sql, (filt,)).fetchall()
        ids = [r["item_id"] for r in rows]

    print(f"Deep-fetching {len(ids)} {args.signer} listings "
          f"(rate={1/max(args.sleep,0.01):.1f}/s, "
          f"ETA ~{len(ids)*args.sleep/60:.1f}m)\n")

    ok = fail = 0
    for i, (item_id, deep) in enumerate(
        trading.get_items_bulk(ids, sleep=args.sleep), 1
    ):
        try:
            with audit_db.connect() as conn:
                audit_db.upsert_deep(conn, item_id, deep)
            ok += 1
        except Exception as e:
            fail += 1
            print(f"  ✗ {item_id}: {e}")
        if i % 20 == 0:
            print(f"  {i}/{len(ids)} (ok={ok} fail={fail})")

    print(f"\nDone: {ok} ok, {fail} fail")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
