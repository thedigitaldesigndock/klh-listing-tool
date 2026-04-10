#!/usr/bin/env python3
"""
Pull title corpora from eBay for Kim's categories and rank keywords
by frequency so we can tune title boilerplate data-driven instead of
guessing.

Strategy:
    1. For each category Kim sells in (football autographs, music
       memorabilia, film & TV), query the Browse API for recently
       listed items matching "signed" — up to ~200 titles per category.
    2. For each set, tokenize, normalize, strip proper nouns (player /
       band / actor names — these are the content, not the boilerplate),
       count the rest.
    3. Print the top 40 keywords per category with frequency, and a
       cross-category "universal" list.

Usage:
    python scripts/keyword_research.py                     # all categories
    python scripts/keyword_research.py --category 27290    # just one
    python scripts/keyword_research.py --limit 50          # fewer per cat
    python scripts/keyword_research.py --json out.json     # machine-readable

No writes. Network-only. Intended to be run by hand before tuning
presets/products.yaml.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Optional

# The token module gives us an OAuth access token that already carries
# the api_scope needed for Browse API search.
from ebay_api.token_manager import get_access_token

BROWSE_ENDPOINT = "https://api.ebay.com/buy/browse/v1/item_summary/search"
MARKETPLACE_ID  = "EBAY_GB"

# The categories Kim cares about. Values come from
# presets/products.yaml `categories_by_subject` — kept in sync manually.
CATEGORIES = {
    "football_premier":  27290,
    "football_retired":  97085,
    "music_pop":        178898,
    "film_tv":            2312,
}

# Stop words + very common English connectives we don't want counted.
STOPWORDS = {
    "a", "an", "and", "or", "of", "in", "on", "at", "the", "to", "for",
    "with", "by", "from", "as", "is", "it", "this", "that", "be", "are",
    "was", "were", "he", "she", "his", "her", "&", "new", "rare", "very",
    "his", "hers", "ours", "theirs", "my", "your", "our", "their", "not",
    "but", "also", "see", "only", "all", "any", "many", "few", "some",
    # Numbers / punctuation artefacts we ignore
    "1", "2", "3", "4", "5", "6", "7", "8", "9", "0",
}

# Words we want to track even though they'd naturally rank high — these
# are the "boilerplate candidates" (everything a signed-memorabilia buyer
# might search for). Frequency among these words tells us which deserve
# a title slot.
TRACKED = {
    "signed", "hand", "autograph", "autographed", "signature", "auto",
    "authentic", "original", "genuine", "coa", "certificate", "cert",
    "loa", "letter", "authenticity", "authorisation", "photo",
    "photograph", "picture", "display", "mount", "mounted", "frame",
    "framed", "print", "poster", "memorabilia", "collectible", "rare",
    "gift", "present", "merch", "merchandise", "premier", "premiership",
    "league", "cup", "trophy", "champion", "winner", "legend", "icon",
    "star", "film", "movie", "actor", "actress", "tv", "series", "show",
    "cast", "character", "band", "singer", "musician", "album",
}

# Token splitter — any non-word boundary, minimum length 2.
TOKEN_RE = re.compile(r"[a-z]+")


def fetch_titles(
    access_token: str,
    *,
    category_id: int,
    limit: int = 100,
    query: str = "signed",
    marketplace_id: str = MARKETPLACE_ID,
) -> list[str]:
    """
    Call Browse API item_summary/search and return a list of titles.

    Browse API returns 50 items per page max. We paginate up to `limit`.
    """
    titles: list[str] = []
    offset = 0
    per_page = 50

    while len(titles) < limit:
        want = min(per_page, limit - len(titles))
        params = {
            "q":            query,
            "category_ids": str(category_id),
            "limit":        str(want),
            "offset":       str(offset),
            "filter":       "buyingOptions:{FIXED_PRICE}",
        }
        url = BROWSE_ENDPOINT + "?" + urllib.parse.urlencode(params)

        req = urllib.request.Request(url)
        req.add_header("Authorization",           f"Bearer {access_token}")
        req.add_header("X-EBAY-C-MARKETPLACE-ID", marketplace_id)
        req.add_header("Content-Type",            "application/json")

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            err = e.read().decode(errors="replace")
            print(f"  HTTP {e.code} for cat {category_id}: {err[:300]}",
                  file=sys.stderr)
            break

        items = body.get("itemSummaries") or []
        if not items:
            break
        for it in items:
            t = it.get("title")
            if t:
                titles.append(t)
        # If we got fewer than we asked for, there are no more pages.
        if len(items) < want:
            break
        offset += want

    return titles


def tokenize(title: str) -> list[str]:
    """Lowercase, strip punctuation, return words ≥ 2 chars that aren't stopwords."""
    return [
        w for w in TOKEN_RE.findall(title.lower())
        if len(w) >= 2 and w not in STOPWORDS
    ]


def rank_keywords(titles: list[str]) -> Counter:
    """
    Count every token across the corpus. Returns a Counter keyed by word.

    We don't strip proper nouns here — the caller can intersect with the
    TRACKED set to see only the SEO-relevant ones, or look at the full
    list to spot surprises.
    """
    counter: Counter = Counter()
    for t in titles:
        counter.update(tokenize(t))
    return counter


def summarise(
    per_cat: dict[str, list[str]],
    *,
    top_n: int = 40,
) -> dict:
    """Build a JSON-serialisable summary of the research."""
    result: dict = {"categories": {}, "universal": {}, "tracked_only": {}}
    # Per-category top keywords
    for name, titles in per_cat.items():
        counter = rank_keywords(titles)
        result["categories"][name] = {
            "sample_size": len(titles),
            "top": counter.most_common(top_n),
            "tracked": [
                (w, counter[w]) for w in
                sorted(TRACKED, key=lambda w: -counter[w])
                if counter[w] > 0
            ],
        }
    # Universal: every category pooled
    pooled: Counter = Counter()
    for titles in per_cat.values():
        pooled.update(rank_keywords(titles))
    result["universal"] = {
        "sample_size": sum(len(t) for t in per_cat.values()),
        "top": pooled.most_common(top_n),
        "tracked": [
            (w, pooled[w]) for w in
            sorted(TRACKED, key=lambda w: -pooled[w])
            if pooled[w] > 0
        ],
    }
    return result


def print_summary(summary: dict) -> None:
    for cat_name, data in summary["categories"].items():
        print(f"\n━━━━ {cat_name} (n={data['sample_size']}) ━━━━")
        print("  Top words (any):")
        for word, count in data["top"][:25]:
            print(f"    {count:4d}  {word}")
        print("  Tracked keywords (SEO-relevant):")
        for word, count in data["tracked"][:25]:
            print(f"    {count:4d}  {word}")
    print("\n━━━━ Universal (all pooled) ━━━━")
    print("  Top tracked keywords across all categories:")
    for word, count in summary["universal"]["tracked"][:30]:
        print(f"    {count:4d}  {word}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=100,
                        help="max titles per category (default 100)")
    parser.add_argument("--category", type=int, default=None,
                        help="just one category id")
    parser.add_argument("--query", default="signed",
                        help="search query (default: 'signed')")
    parser.add_argument("--json", type=Path, default=None,
                        help="write summary to JSON file")
    args = parser.parse_args()

    token = get_access_token(verbose=False)

    if args.category:
        targets = {f"cat_{args.category}": args.category}
    else:
        targets = CATEGORIES

    per_cat: dict[str, list[str]] = {}
    for name, cat_id in targets.items():
        print(f"[fetch] {name} ({cat_id})...")
        titles = fetch_titles(
            token,
            category_id=cat_id,
            limit=args.limit,
            query=args.query,
        )
        print(f"  got {len(titles)} titles")
        per_cat[name] = titles

    if not any(per_cat.values()):
        print("No titles returned.", file=sys.stderr)
        sys.exit(1)

    summary = summarise(per_cat)
    print_summary(summary)

    if args.json:
        args.json.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
        print(f"\n  wrote {args.json}")


if __name__ == "__main__":
    main()
