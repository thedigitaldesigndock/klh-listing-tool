#!/usr/bin/env python3
"""
Set every currently-live listing's Quantity to 1.

Rationale (Peter): move from 'N qty per listing with one photo' to
'1 qty per listing with THE actual photo'. When a listing sells, it
goes out of stock — next one gets freshly listed with the real photo
of the item being shipped. More honest, lets buyers see what they're
actually getting.

Only touches listings with quantity_available > 1. Skips:
  - qty_available == 0 (out-of-stock, intentional)
  - qty_available == 1 (already correct)
  - non-FixedPriceItem listings (auctions)
  - excluded item_ids (supply/stock products)

Uses ReviseInventoryStatus batched 4-at-a-time (eBay's per-call cap
for this verb). That's lighter than ReviseFixedPriceItem — no search-
rank churn, no re-verification.

Usage:
    python scripts/set_all_live_qty_to_one.py              # dry-run
    python scripts/set_all_live_qty_to_one.py --apply
    python scripts/set_all_live_qty_to_one.py --apply --exclude 267399614833,...
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

from pipeline import audit_db
from ebay_api.trading import trading_call, _text, NS_MAP
from pipeline.lister import _el


BATCH_SIZE = 4   # eBay cap for ReviseInventoryStatus


def _build_revise_xml(items: list[str]) -> str:
    """Compose the inner XML for a batched ReviseInventoryStatus call."""
    parts: list[str] = []
    for iid in items:
        parts.append(
            f"<InventoryStatus>"
            f"{_el('ItemID', iid)}"
            f"{_el('Quantity', '1')}"
            f"</InventoryStatus>"
        )
    return "".join(parts)


def _bulk_set_qty_one(item_ids: list[str]) -> tuple[int, int, list[dict]]:
    """Revise up to 4 items per call. Returns (ok_count, fail_count, errors)."""
    ok = fail = 0
    errors: list[dict] = []
    for i in range(0, len(item_ids), BATCH_SIZE):
        batch = item_ids[i:i + BATCH_SIZE]
        try:
            root = trading_call("ReviseInventoryStatus", _build_revise_xml(batch))
            ack = _text(root, "e:Ack") or ""
            # eBay returns per-item InventoryStatus + possible top-level Errors.
            # For our purposes Success/Warning on the whole call = all good;
            # top-level errorCode = all failed. Mid-granularity (some items
            # ok, some not) is rare but defensive-handled.
            if ack in ("Success", "Warning"):
                ok += len(batch)
            else:
                fail += len(batch)
                for err in root.findall("e:Errors", NS_MAP):
                    errors.append({
                        "batch":   batch,
                        "code":    _text(err, "e:ErrorCode"),
                        "message": _text(err, "e:LongMessage"),
                    })
        except Exception as e:
            fail += len(batch)
            errors.append({"batch": batch, "exception": str(e)})
    return ok, fail, errors


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--exclude", default="",
                   help="Comma-separated item_ids to skip (e.g. supply products)")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap number of listings touched (testing)")
    p.add_argument("--rate", type=float, default=2.0,
                   help="Batches per second (default 2.0 = ~8 items/sec)")
    args = p.parse_args()

    excluded = {x.strip() for x in args.exclude.split(",") if x.strip()}
    if excluded:
        print(f"Excluding {len(excluded)} item_ids: {sorted(excluded)}")

    with audit_db.connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT item_id, title, quantity_available FROM listings "
            "WHERE listing_type = 'FixedPriceItem' AND quantity_available > 1 "
            "ORDER BY item_id"
        ).fetchall()

    targets = [r["item_id"] for r in rows if r["item_id"] not in excluded]
    skipped = [r for r in rows if r["item_id"] in excluded]
    if args.limit:
        targets = targets[:args.limit]

    print(f"\n=== Plan ===")
    print(f"  Live listings with qty>1: {len(rows):,}")
    print(f"  Excluded:                 {len(skipped):,}")
    print(f"  To revise to qty=1:       {len(targets):,}")
    print(f"  Batches (4 per call):     {(len(targets) + BATCH_SIZE - 1) // BATCH_SIZE:,}")
    eta_s = len(targets) / (BATCH_SIZE * args.rate)
    print(f"  ETA @ {args.rate}/s batches: ~{eta_s / 60:.1f} min")

    # Show sample of what we'll change
    print(f"\nSample first 5:")
    for r in rows[:5]:
        if r["item_id"] in excluded:
            continue
        print(f"  [{r['item_id']}] qty={r['quantity_available']:>3}  {r['title'][:70]}")
    if skipped:
        print(f"\nSample excluded:")
        for r in skipped[:5]:
            print(f"  [{r['item_id']}] qty={r['quantity_available']:>3}  {r['title'][:70]}")

    if not args.apply:
        print("\n[DRY RUN] pass --apply to run live.")
        return 0

    print(f"\n=== Applying live ===\n")
    start = time.monotonic()
    total_ok = 0
    total_fail = 0
    all_errors: list[dict] = []
    sleep_per_batch = 1.0 / max(args.rate, 0.1)

    for i in range(0, len(targets), BATCH_SIZE):
        batch = targets[i:i + BATCH_SIZE]
        ok, fail, errors = _bulk_set_qty_one(batch)
        total_ok += ok
        total_fail += fail
        all_errors.extend(errors)
        if fail and errors:
            for err in errors[:2]:
                print(f"  ✗ {err.get('batch')}: {err.get('code','')} {err.get('message','')[:100]}")
        if (i // BATCH_SIZE) % 25 == 24:
            elapsed = time.monotonic() - start
            done = i + len(batch)
            print(f"  {done}/{len(targets)}  ok={total_ok} fail={total_fail}  ({done/elapsed:.1f} items/s)")
        time.sleep(sleep_per_batch)

    elapsed = time.monotonic() - start
    print(f"\n=== Done ===")
    print(f"  ok:     {total_ok}")
    print(f"  failed: {total_fail}")
    print(f"  time:   {elapsed:.0f}s")

    # Update local cache for successful items. We assume the whole batch
    # succeeded if ack was Success — fine for bulk-quantity updates.
    if total_ok > 0:
        with audit_db.connect() as conn:
            # Optimistic: mark all targets as qty=1 except those in errors' batches
            failed_ids: set[str] = set()
            for err in all_errors:
                for iid in err.get("batch") or []:
                    failed_ids.add(iid)
            updated = 0
            for iid in targets:
                if iid in failed_ids:
                    continue
                conn.execute(
                    "UPDATE listings SET quantity = 1, quantity_available = 1 "
                    "WHERE item_id = ?", (iid,)
                )
                updated += 1
            conn.commit()
            print(f"  local cache updated: {updated}")

            # Log to optimization_log
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                "INSERT INTO optimization_log (event, event_at, details) VALUES (?, ?, ?)",
                ("SET_LIVE_QTY_1", now,
                 f"Set quantity=1 on {total_ok} live listings (was: mix 2-35). "
                 f"Shifts inventory model to 1-photo-per-real-item. Failed={total_fail}.")
            )
            conn.commit()
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
