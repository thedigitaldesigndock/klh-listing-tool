#!/usr/bin/env python3
"""
Discover the eBay PrimaryCategory IDs Kim uses for each sport / genre.

Walks Kim's active listings, classifies each by keyword (Cricket, Rugby,
Boxing, …) and tallies how often each <sport> → <eBay category ID> pair
appears. Prints a table so we can paste verified IDs into
presets/products.yaml → categories_by_subject.

We trust frequency: if 40/42 Cricket listings map to category 78301,
that's the right one for Cricket. If a sport is split across categories
we surface both counts so a human can decide.

Usage:
    python scripts/discover_sport_categories.py --pages 20
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

from ebay_api.trading import TradingError, get_my_ebay_selling, get_item

# --------------------------------------------------------------------------- #
# Sport classification by title keyword.
#
# Order matters — we take the first match, so put the more specific
# words first (e.g. "Rugby League" before "Rugby").
# All comparisons are lowercase.
# --------------------------------------------------------------------------- #
SPORT_KEYWORDS: list[tuple[str, list[str]]] = [
    ("rugby_league", ["rugby league", "super league", "rfl"]),
    ("rugby",        ["rugby", "six nations", "rfu", "harlequins", "saracens"]),
    ("cricket",      ["cricket", "ashes", "test match", "icc"]),
    ("snooker",      ["snooker", "crucible"]),
    ("boxing",       ["boxing", "heavyweight", "wba", "wbc", "ibf"]),
    ("darts",        ["darts", "pdc"]),
    ("golf",         ["golf", "pga", "ryder cup"]),
    ("tennis",       ["tennis", "wimbledon", "atp", "wta"]),
    ("f1",           ["f1", "formula 1", "grand prix", "motorsport", "mercedes amg", "ferrari"]),
    ("nfl",          ["nfl", "super bowl"]),
    ("nba",          ["nba", "basketball"]),
    ("mma",          ["mma", "ufc", "octagon"]),
    ("music",        ["music", "band", "album", "guitarist", "vocalist", "rock", "pop", "indie"]),
    ("tv",           ["tv", "sitcom", "series", "drama", "show"]),
    ("film",         ["film", "movie", "hollywood", "motion picture"]),
    ("football",     ["football", "premier league", "champions league", "fifa"]),
]


def classify_sport(title: str) -> str | None:
    t = title.lower()
    for sport, keywords in SPORT_KEYWORDS:
        if any(kw in t for kw in keywords):
            return sport
    return None


# --------------------------------------------------------------------------- #

def walk_active(max_pages: int, per_page: int, verbose: bool = True):
    """Yield (item_id, title, category_id) for every active listing."""
    for page_number in range(1, max_pages + 1):
        if verbose:
            print(f"[page {page_number}] fetching...", file=sys.stderr)
        page = get_my_ebay_selling(
            entries_per_page=per_page,
            page_number=page_number,
        )
        items = page.get("items") or []
        if not items:
            return
        for summary in items:
            yield (
                summary["item_id"],
                summary.get("title") or "",
                str(summary.get("category_id") or ""),
            )
        if page_number >= page.get("total_pages", 0):
            return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=int, default=20)
    parser.add_argument("--per-page", type=int, default=100)
    parser.add_argument("--limit", type=int, default=None,
                        help="stop after this many GetItem calls (cost control)")
    parser.add_argument("--per-sport", type=int, default=5,
                        help="GetItem calls per sport before moving on (default 5)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    verbose = not args.quiet

    # {sport: Counter({category_id: count})}
    tally: dict[str, Counter] = defaultdict(Counter)
    # Keep one sample title per (sport, category) pair for sanity checks.
    examples: dict[tuple[str, str], str] = {}

    classified = 0
    skipped = 0

    # Per-sport cap so we don't GetItem 6000 football listings.
    per_sport_cap = args.per_sport
    for item_id, title, _summary_cat in walk_active(args.pages, args.per_page, verbose):
        sport = classify_sport(title)
        if sport is None:
            skipped += 1
            continue
        if sum(tally[sport].values()) >= per_sport_cap:
            continue
        try:
            full = get_item(item_id, include_description=False)
        except TradingError as e:
            print(f"  ! GetItem {item_id} failed: {e}", file=sys.stderr)
            continue
        pc = full.get("PrimaryCategory") or {}
        cat_id = str(pc.get("CategoryID") or "")
        if not cat_id:
            continue
        tally[sport][cat_id] += 1
        examples.setdefault((sport, cat_id), title)
        classified += 1
        if verbose and classified % 10 == 0:
            print(f"  classified so far: {classified}", file=sys.stderr)
        if args.limit and classified >= args.limit:
            break

    # ---- Report -----------------------------------------------------------
    print()
    print(f"Classified {classified} listings across {len(tally)} sports "
          f"(skipped {skipped} with no sport keyword).")
    print()
    print(f"{'sport':<14} {'cat_id':<12} {'count':>6}   example")
    print("-" * 90)
    for sport in sorted(tally.keys()):
        counter = tally[sport]
        for cat_id, count in counter.most_common():
            example = examples.get((sport, cat_id), "")
            print(f"{sport:<14} {cat_id:<12} {count:>6}   {example[:60]}")
        print()

    # Suggested YAML block (winner per sport).
    print("# ── Suggested categories_by_subject entries ──")
    for sport in sorted(tally.keys()):
        (cat_id, count), *_ = tally[sport].most_common(1)
        print(f"  {sport:<18} {cat_id}   # from {count} listings")


if __name__ == "__main__":
    main()
