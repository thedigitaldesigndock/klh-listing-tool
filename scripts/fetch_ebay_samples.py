#!/usr/bin/env python3
"""
Fetch a representative sample of active KLHAutographs listings — one
per product type we can identify by title — and dump the full GetItem
response for each to tests/fixtures/ebay_samples/ as JSON.

These samples drive the Phase 5 design work: they show us the real
title patterns, HTML description structures, category IDs, item
specifics, shipping/returns, etc. that the lister needs to reproduce.

Usage:
    python scripts/fetch_ebay_samples.py                 # default: 5 pages
    python scripts/fetch_ebay_samples.py --pages 10      # scan more
    python scripts/fetch_ebay_samples.py --out <dir>     # override output
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

from ebay_api.trading import (
    TradingError,
    get_my_ebay_selling,
    get_item,
)

# --------------------------------------------------------------------------- #
# Title classification
# --------------------------------------------------------------------------- #
#
# We want ONE example of each product type so presets.yaml has concrete
# titles/descriptions to pattern-match. Classification is best-effort —
# titles aren't machine-generated and the words sometimes appear in
# surprising orders, so we score candidates and prefer the strongest
# positive signal.

# (product_key, required phrases (all lowercase, can be multi-word),
#  excluded phrases). Requirements and exclusions are matched case-
# insensitively against the title. The multi-word phrases ("mount display",
# "framed photo") are what stops false positives like the player "Mason
# Mount" being misread as a mount-display product.
CLASSIFIERS: list[tuple[str, list[str], list[str]]] = [
    # Primary mount/frame products — these are the templates we've extracted.
    ("a4_mount",      ["signed", "a4",    "mount display"],  ["framed"]),
    ("a4_frame",      ["signed", "a4",    "framed photo"],   []),
    ("10x8_mount",    ["signed", "10x8",  "mount display"],  ["framed"]),
    ("10x8_frame",    ["signed", "10x8",  "framed photo"],   []),
    ("16x12_mount",   ["signed", "16x12", "mount display"],  ["framed"]),
    ("16x12_frame",   ["signed", "16x12", "framed photo"],   []),
    # Plain photos (no mount/frame). We insist on "signed <size> photo" as
    # a contiguous substring to avoid grabbing framed/mount listings.
    ("a4_photo",      ["signed a4 photo"],                   ["mount", "framed", "frame"]),
    ("10x8_photo",    ["signed 10x8 photo"],                 ["mount", "framed", "frame"]),
    ("6x4_photo",     ["signed 6x4 photo"],                  ["mount", "framed", "frame"]),
    ("12x8_photo",    ["signed 12x8 photo"],                 ["mount", "framed", "frame"]),
]


def classify(title: str) -> Optional[str]:
    """
    Return the product_key for the first classifier whose required substrings
    all appear in the title AND whose excluded substrings do not.
    """
    t = title.lower()
    for key, required, excluded in CLASSIFIERS:
        if all(sub in t for sub in required) and not any(sub in t for sub in excluded):
            return key
    return None


# --------------------------------------------------------------------------- #
# Sampling walk
# --------------------------------------------------------------------------- #

def fetch_samples(
    *,
    max_pages: int,
    entries_per_page: int,
    verbose: bool = True,
) -> dict[str, dict]:
    """
    Walk ActiveList pages until we've captured one item of each product_key
    in CLASSIFIERS, or we hit max_pages.

    Returns {product_key: full_item_dict}.
    """
    wanted = {key for key, *_ in CLASSIFIERS}
    seen: dict[str, dict] = {}

    for page_number in range(1, max_pages + 1):
        if verbose:
            print(f"[page {page_number}] fetching {entries_per_page} listings...")
        page = get_my_ebay_selling(
            entries_per_page=entries_per_page,
            page_number=page_number,
        )
        if not page["items"]:
            if verbose:
                print(f"  (no items returned — stopping)")
            break

        for summary in page["items"]:
            title = summary.get("title") or ""
            key = classify(title)
            if key is None or key in seen:
                continue
            if verbose:
                print(f"  ✓ {key:14s} [{summary['item_id']}]  {title[:70]}")
            try:
                full = get_item(summary["item_id"], include_description=True)
            except TradingError as e:
                print(f"    ! GetItem failed: {e}", file=sys.stderr)
                continue
            seen[key] = {
                "product_key": key,
                "fetched_at_page": page_number,
                "summary": summary,
                "item": full,
            }
            if wanted <= set(seen):
                if verbose:
                    print(f"  [all {len(wanted)} types captured, stopping early]")
                return seen

        if page_number >= page.get("total_pages", 0):
            break

    return seen


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=int, default=5,
                        help="max ActiveList pages to scan (default 5)")
    parser.add_argument("--per-page", type=int, default=50,
                        help="items per page (max 200)")
    parser.add_argument("--out", type=Path, default=None,
                        help="output dir (default: tests/fixtures/ebay_samples)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    if args.out is None:
        repo = Path(__file__).resolve().parent.parent
        out_dir = repo / "tests" / "fixtures" / "ebay_samples"
    else:
        out_dir = args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Output: {out_dir}")
    samples = fetch_samples(
        max_pages=args.pages,
        entries_per_page=args.per_page,
        verbose=not args.quiet,
    )

    if not samples:
        print("No samples captured.", file=sys.stderr)
        sys.exit(1)

    # Write one JSON file per product type.
    for key, payload in samples.items():
        path = out_dir / f"{key}.json"
        with open(path, "w") as f:
            json.dump(payload, f, indent=2, sort_keys=False, ensure_ascii=False)
        print(f"  wrote {path.relative_to(out_dir.parent.parent)}")

    # Small summary index so we can see what we captured at a glance.
    index = {
        "captured": sorted(samples.keys()),
        "missing": sorted({k for k, *_ in CLASSIFIERS} - set(samples.keys())),
        "by_key": {
            k: {
                "item_id": v["summary"]["item_id"],
                "title": v["summary"]["title"],
                "price": v["summary"]["price"],
            }
            for k, v in samples.items()
        },
    }
    with open(out_dir / "_index.json", "w") as f:
        json.dump(index, f, indent=2)
    print(f"\n{len(samples)}/{len(CLASSIFIERS)} types captured")
    if index["missing"]:
        print(f"Missing: {', '.join(index['missing'])}")


if __name__ == "__main__":
    main()
