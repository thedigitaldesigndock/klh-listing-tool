#!/usr/bin/env python3
"""
Fill in the canonical Item Specifics set on every Bryan Robson listing.

Today 154 of 167 BR listings only have "Country of Origin: UK" set —
eBay's search filter sidebar and relevance ranker both weight IS heavily,
so this is a big easy win.

Default specifics applied to every BR listing:

    Player/Athlete          Bryan Robson
    Signed By               Bryan Robson
    Sport                   Football
    Team                    Manchester United
    Original/Reproduction   Original
    Signed                  Yes
    Authenticity            Hand-Signed
    Country/Region of Manufacture  United Kingdom
    Modified Item           No

Per-listing (derived from title):

    Size                    6x4 / 10x8 / 12x8 / 16x12 / A4
    Type                    Photo (default)
                            Mounted Photo Display  if title has "Mount"
                            Framed Photo Display   if title has "Framed"

Existing specifics are merged, not clobbered — we never delete a key.
If a specific is already set, our value REPLACES it (so we can fix any
wrong values that were there, e.g. Team: "Chelsea" gets corrected).

Usage
-----
    python scripts/add_br_item_specifics.py          # dry-run (default)
    python scripts/add_br_item_specifics.py --apply  # live revisions
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone

from pipeline import audit_db, lister


SIGNER_FILTER = "%bryan robson%"

CONSTANTS: dict[str, str] = {
    "Player/Athlete":                "Bryan Robson",
    "Signed By":                     "Bryan Robson",
    "Sport":                         "Football",
    "Original/Reproduction":         "Original",
    "Signed":                        "Yes",
    "Authenticity":                  "Hand-Signed",
    "Country/Region of Manufacture": "United Kingdom",
    "Modified Item":                 "No",
}

SIZE_PATTERNS = [
    (re.compile(r"\b16x12\b", re.I), "16x12"),
    (re.compile(r"\b12x8\b",  re.I), "12x8"),
    (re.compile(r"\b10x8\b",  re.I), "10x8"),
    (re.compile(r"\b6x4\b",   re.I), "6x4"),
    (re.compile(r"\bA4\b",    re.I), "A4"),
    (re.compile(r"\bA3\b",    re.I), "A3"),
]

# Team derivation. Priority order: Man Utd > WBA > Middlesbrough > England.
# Man Utd wins even when title mentions England too (his primary identity).
# Returns None if no team match — better to leave Team unset than guess wrong.
TEAM_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(manchester united|manchester utd|man utd|man united)\b", re.I),
     "Manchester United"),
    (re.compile(r"\b(west bromwich albion|west brom|wba)\b", re.I),
     "West Bromwich Albion"),
    (re.compile(r"\b(middlesbrough|boro)\b", re.I),
     "Middlesbrough"),
    (re.compile(r"\bengland\b", re.I),
     "England"),
]

# Listings where Size shouldn't be set from our photo-sizes — non-photo products.
# Word-boundary regex so "book" doesn't swallow "booklet" (a 6x4 multi-photo product).
NON_PHOTO_RE = re.compile(r"\b(dvd|shirt|magazine)\b", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _derive_size(title: str) -> str | None:
    for pat, label in SIZE_PATTERNS:
        if pat.search(title):
            return label
    return None


def _derive_type(title: str) -> str:
    """Framed wins over Mount wins over plain Photo. Handles non-photo edge cases."""
    low = title.lower()
    if "shirt" in low:
        return "Shirt"
    if "dvd" in low:
        return "DVD"
    if "framed" in low:
        return "Framed Photo Display"
    if "mount" in low:
        return "Mounted Photo Display"
    return "Photo"


def _derive_team(title: str) -> str | None:
    """First match in priority order wins. None if no team mentioned."""
    for pat, team in TEAM_PATTERNS:
        if pat.search(title):
            return team
    return None


def _is_non_photo(title: str) -> bool:
    return bool(NON_PHOTO_RE.search(title))


def _propose_specifics(current: dict[str, str], title: str) -> dict[str, str]:
    """Merge: start from current, overlay constants, overlay derived."""
    merged: dict[str, str] = dict(current)  # keep everything already set
    merged.update(CONSTANTS)                # constants always win
    team = _derive_team(title)
    if team:
        merged["Team"] = team
    # Size only for photo products (skip DVD / shirt / etc.)
    if not _is_non_photo(title):
        size = _derive_size(title)
        if size:
            merged["Size"] = size
    merged["Type"] = _derive_type(title)
    return merged


def _diff(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Return per-key lines describing what changed."""
    lines: list[str] = []
    all_keys = sorted(set(before) | set(after))
    for k in all_keys:
        b = before.get(k)
        a = after.get(k)
        if b is None and a is not None:
            lines.append(f"    + {k}: {a}")
        elif b is not None and a is None:
            lines.append(f"    - {k}: {b}")
        elif b != a:
            lines.append(f"    ~ {k}: {b}  →  {a}")
    return lines


def _load_candidates(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT item_id, title, watch_count, price_gbp, specifics_json
        FROM listings
        WHERE LOWER(title) LIKE ? AND deep_fetched_at IS NOT NULL
        ORDER BY item_id
        """,
        (SIGNER_FILTER,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        specifics = json.loads(r["specifics_json"]) if r["specifics_json"] else {}
        out.append({
            "item_id":      r["item_id"],
            "title":        r["title"],
            "watch_count":  r["watch_count"] or 0,
            "price_gbp":    r["price_gbp"],
            "current":      specifics,
        })
    return out


def _print_dry_run(candidates: list[dict], sample_n: int = 5) -> dict:
    """Print summary + sample diffs. Return stats."""
    stats = {"total": len(candidates), "no_change": 0, "added": 0, "changed": 0,
             "skipped_no_size": 0, "net_additions": 0}
    samples_shown = 0
    for c in candidates:
        proposed = _propose_specifics(c["current"], c["title"])
        if proposed == c["current"]:
            stats["no_change"] += 1
            continue
        added = set(proposed) - set(c["current"])
        changed = {k for k in set(c["current"]) & set(proposed)
                   if c["current"][k] != proposed[k]}
        if added:
            stats["added"] += 1
        if changed:
            stats["changed"] += 1
        stats["net_additions"] += len(added)
        if "Size" not in proposed:
            stats["skipped_no_size"] += 1
        if samples_shown < sample_n:
            print(f"\n  [{c['item_id']}]  watch={c['watch_count']}  "
                  f"£{c['price_gbp']}  {c['title']}")
            for line in _diff(c["current"], proposed):
                print(line)
            samples_shown += 1
    return stats


def _apply(conn, candidates: list[dict], rate_per_sec: float) -> None:
    sleep = 1.0 / max(rate_per_sec, 0.1)
    targets = [
        c for c in candidates
        if _propose_specifics(c["current"], c["title"]) != c["current"]
    ]
    print(f"\nApplying IS to {len(targets)} listings "
          f"(rate={rate_per_sec}/s, ETA ~{len(targets) * sleep / 60:.1f}m)\n")

    conn.execute(
        "INSERT INTO optimization_log (event, event_at, details) VALUES (?, ?, ?)",
        ("BR_IS_START", _now(),
         f"Begin applying canonical IS template to {len(targets)} BR listings")
    )
    conn.commit()

    ok = 0
    fail = 0
    start = time.monotonic()
    for i, c in enumerate(targets, 1):
        proposed = _propose_specifics(c["current"], c["title"])
        try:
            result = lister.revise_listing(
                c["item_id"],
                new_specifics_replace=proposed,
                confirm=True,
            )
            ack = result.get("ack")
            warnings = result.get("warnings") or []
            if ack in ("Success", "Warning"):
                ok += 1
                # Update cache so re-runs know the new state.
                conn.execute(
                    "UPDATE listings SET specifics_json = ? WHERE item_id = ?",
                    (json.dumps(proposed), c["item_id"]),
                )
            else:
                fail += 1
                msgs = "; ".join(w.get("long", "") for w in warnings if w.get("long"))
                print(f"  ✗ [{c['item_id']}] ack={ack}  {msgs}")
        except Exception as e:
            fail += 1
            print(f"  ✗ [{c['item_id']}] EXCEPTION: {e}")

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
        ("BR_IS_DONE", _now(),
         f"Applied canonical IS template: {ok} ok, {fail} failed")
    )
    conn.commit()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true",
                   help="actually call ReviseFixedPriceItem (dry-run otherwise)")
    p.add_argument("--rate", type=float, default=1.0,
                   help="API calls per second (default 1.0)")
    p.add_argument("--yes", action="store_true",
                   help="skip interactive 'are you sure?' prompt")
    p.add_argument("--sample", type=int, default=5,
                   help="how many per-listing diffs to print in dry-run (default 5)")
    args = p.parse_args()

    with audit_db.connect() as conn:
        candidates = _load_candidates(conn)
        if not candidates:
            print("No deep-fetched BR listings found.")
            return 1

        print(f"=== BR IS proposal ({len(candidates)} candidates) ===")
        stats = _print_dry_run(candidates, sample_n=args.sample)
        print("\n=== Summary ===")
        print(f"  Total BR listings (deep-fetched): {stats['total']}")
        print(f"  Already fine, no change:          {stats['no_change']}")
        print(f"  Will add new specifics:           {stats['added']}")
        print(f"  Will correct existing values:     {stats['changed']}")
        print(f"  Net new specific key-values:      {stats['net_additions']}")
        if stats['skipped_no_size']:
            print(f"  ⚠ Could not derive Size from title: {stats['skipped_no_size']}")

        if not args.apply:
            print("\n[DRY RUN] Pass --apply to write live.")
            return 0

        if not args.yes:
            confirm = input("\nProceed with live revisions? [yes/no] ").strip().lower()
            if confirm not in ("yes", "y"):
                print("Aborted.")
                return 1

        _apply(conn, candidates, args.rate)
    return 0


if __name__ == "__main__":
    sys.exit(main())
