#!/usr/bin/env python3
"""
Streaming recat + IS correction loop.

For every listing in the cache:
  1. Fetch (GetItem) if category_id not in cache
  2. Detect signer → look up genre in presets/signer_genre.yaml
  3. Detect product_type from title keywords
  4. If product_type is a "leave-alone" type (DVD/Book/Record/…), skip
     the recat step but still apply IS updates to the current category.
  5. Look up target category from presets/category_map.yaml:
     map[genre][product_type]. If "??", skip and report.
  6. Merge target IS = existing + defaults.yaml universal block +
     category_specifics.yaml overlay for target cat + signer constants
     (Signed By, Player/Athlete or Actor/Personality).
  7. If target_cat != current_cat OR target_IS != current_IS: revise.
     (Include ConditionID=3000 when recat'ing — some cats require it.)

Signers not in signer_genre.yaml are skipped and reported at the end so
we can classify them and re-run.

Usage:
    python scripts/stream_recat_is.py --preview 50        # dry-run first 50
    python scripts/stream_recat_is.py --apply --limit 200  # live apply first 200
    python scripts/stream_recat_is.py --apply             # live apply everything
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import yaml
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pipeline import audit_db, lister, presets as pp
from ebay_api import trading


REPO_ROOT = Path(__file__).resolve().parent.parent
CATEGORY_MAP_YAML = REPO_ROOT / "presets" / "category_map.yaml"
SIGNER_GENRE_YAML = REPO_ROOT / "presets" / "signer_genre.yaml"


# ──────────────────────────────────────────────────────────────────────
# Product-type detection from title. First match wins. Order = specific
# to generic so "signed shirt" beats "signed photo".
# ──────────────────────────────────────────────────────────────────────
PRODUCT_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("Shirt",     re.compile(r"\b(shirt|jersey|kit|strip)\b", re.I)),
    ("DVD",       re.compile(r"\bdvd\b", re.I)),
    ("Book",      re.compile(r"\b(autobiography|hardback|paperback|book)\b", re.I)),
    ("Programme", re.compile(r"\b(programme|program\b)", re.I)),
    ("Glove",     re.compile(r"\bglove", re.I)),
    ("Boot",      re.compile(r"\bboots?\b", re.I)),
    ("Ball",      re.compile(r"\b(match\s*ball|signed\s*ball|mini\s*ball)\b", re.I)),
    ("Record",    re.compile(r"\b(vinyl|\brecord\b|\blp\b|album)\b", re.I)),
    ("Poster",    re.compile(r"\bposter\b", re.I)),
    ("Script",    re.compile(r"\bscript\b", re.I)),
    ("Flag",      re.compile(r"\bflag\b", re.I)),
    ("Cap",       re.compile(r"\bcap\b", re.I)),
    ("Postcard",  re.compile(r"\b(postcard|post\s*card)\b", re.I)),
    ("Card",      re.compile(r"\b(white\s*card|index\s*card|5x3|\bcard\b)", re.I)),
    # If none of the above match, it's a Photo.
]

# Products we never recat — stay in whatever existing category they're
# in. IS still gets merged against that current cat.
LEAVE_ALONE_PRODUCTS = {"DVD", "Book", "Record", "Script", "Programme",
                        "Poster", "Flag", "Cap", "Postcard"}


def _detect_product(title: str) -> str:
    for name, pat in PRODUCT_PATTERNS:
        if pat.search(title):
            return name
    return "Photo"


# ──────────────────────────────────────────────────────────────────────
# Signer detection. We support 2-word names (default) and explicit
# 3-word names from signer_genre.yaml (e.g. "Sir David Attenborough").
# ──────────────────────────────────────────────────────────────────────
# Accepts O'Neal, MacDonald, Al-Fayed, 3-word "Sir David X" etc.
# Each name component starts with a capital then mix of letters/-'./.
NAME_RE_2 = re.compile(r"^([A-Z][\w\-'.]+ [A-Z][\w\-'.]+)")
NAME_RE_3 = re.compile(r"^([A-Z][\w\-'.]+ [A-Z][\w\-'.]+ [A-Z][\w\-'.]+)")
SKIP_FIRST = {"Signed","Original","Genuine","Rare","Vintage","Hand","Mystery",
              "Limited","Framed","Authentic","The","Joblot","Multi","Multiple",
              "Auto","Autograph","Memorabilia","Photo","Mounted","Mount",
              "Display","Dedicated","NEW"}


TITLE_PREFIXES = {"sir", "dame", "lady", "dr", "prof", "rev", "lord"}


def _detect_signer(title: str, genre_keys: set[str]) -> Optional[str]:
    """3-word match ONLY if the first word is an honorific ("Sir David
    Attenborough"). Otherwise prefer 2-word to avoid grabbing
    "Bryan Robson Hand" as a 3-word name. Returns only if the matched
    lowercase key is in genre_keys."""
    if not title:
        return None
    # Try 3-word only for honorifics
    m3 = NAME_RE_3.match(title)
    if m3:
        parts = m3.group(1).split()
        if parts[0].lower() in TITLE_PREFIXES:
            key = m3.group(1).lower()
            if key in genre_keys:
                return key
    m2 = NAME_RE_2.match(title)
    if m2:
        name = m2.group(1)
        if name.split()[0] not in SKIP_FIRST:
            key = name.lower()
            if key in genre_keys:
                return key
    return None


# ──────────────────────────────────────────────────────────────────────
# IS merge
# ──────────────────────────────────────────────────────────────────────
TV_CATS = {"35027", "35028", "35030", "35031", "69536"}


def _merge_is(current: dict, defaults: dict, cat_overlay: dict,
              signer_title_case: str, target_cat: str) -> dict:
    merged = dict(current)
    merged.update(defaults)
    merged.update(cat_overlay)
    merged["Signed By"] = signer_title_case
    if target_cat in TV_CATS:
        merged["Actor/Personality"] = signer_title_case
        merged.pop("Player/Athlete", None)
    else:
        merged["Player/Athlete"] = signer_title_case
        merged.pop("Actor/Personality", None)
    merged["Country/Region of Manufacture"] = "United Kingdom"
    merged["Modified Item"] = merged.get("Modified Item", "No")
    merged["Signed"] = "Yes"
    merged["Original/Reproduction"] = "Original"
    return merged


# ──────────────────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_yaml(path: Path) -> dict:
    with path.open() as f:
        return yaml.safe_load(f) or {}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true",
                   help="Fire live revises. Without this, runs in preview mode.")
    p.add_argument("--recat", action="store_true",
                   help="Also change category when target differs. Default OFF "
                        "per Peter's 2026-04-20 decision — IS-only is safer.")
    p.add_argument("--preview", type=int, default=50,
                   help="In preview mode, show this many sample decisions.")
    p.add_argument("--limit", type=int, default=0,
                   help="Process at most N listings (0 = all).")
    p.add_argument("--sleep-fetch", type=float, default=0.5)
    p.add_argument("--sleep-revise", type=float, default=0.8)
    args = p.parse_args()

    # Load presets
    bundle   = pp.load()
    defaults = dict(bundle.defaults.get("item_specifics") or {})
    genre_cfg = _load_yaml(SIGNER_GENRE_YAML)
    cat_cfg   = _load_yaml(CATEGORY_MAP_YAML)
    signer_to_genre: dict[str, str] = {
        k.lower(): v for k, v in (genre_cfg.get("signers") or {}).items()
    }
    cat_map: dict[str, dict[str, dict]] = cat_cfg.get("map") or {}
    known_signers = set(signer_to_genre.keys())

    print(f"Loaded {len(signer_to_genre)} classified signers, "
          f"{len(cat_map)} genres in category_map.")

    # Fetch listing IDs to process
    with audit_db.connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT item_id, title, category_id, category_name, "
            "specifics_json, deep_fetched_at FROM listings "
            "ORDER BY item_id"
        ).fetchall()
    if args.limit:
        rows = rows[: args.limit]
    total = len(rows)
    mode = "PREVIEW" if not args.apply else "LIVE"
    print(f"\n=== Streaming {mode} over {total} listings ===\n")

    if args.apply:
        with audit_db.connect() as conn:
            conn.execute(
                "INSERT INTO optimization_log (event, event_at, details) "
                "VALUES (?, ?, ?)",
                ("STREAM_RECAT_IS_START", _now(),
                 f"Begin streaming recat+IS over {total} listings"),
            )
            conn.commit()

    stats = Counter()
    preview_samples: list[dict] = []
    unknown_signers: Counter = Counter()
    skipped_mapping: Counter = Counter()

    start = time.monotonic()
    for idx, r in enumerate(rows, 1):
        item_id = r["item_id"]
        title = r["title"] or ""
        cat_id = r["category_id"]
        cat_name = r["category_name"]
        specs = json.loads(r["specifics_json"]) if r["specifics_json"] else {}

        # Step 1 — fetch if no category info
        fetched_now = False
        if not cat_id:
            try:
                raw = trading.get_item(item_id, include_description=False)
                deep = trading._shape_deep_item(raw)
                with audit_db.connect() as conn:
                    audit_db.upsert_deep(conn, item_id, deep)
                cat_id   = deep.get("category_id") or cat_id
                cat_name = deep.get("category_name") or cat_name
                specs    = deep.get("item_specifics") or specs
                fetched_now = True
                stats["fetched"] += 1
                time.sleep(args.sleep_fetch)
            except Exception as e:
                stats["fetch_failed"] += 1
                print(f"  ✗ [{item_id}] fetch failed: {e}")
                continue

        # Step 2 — signer
        signer = _detect_signer(title, known_signers)
        if not signer:
            stats["unknown_signer"] += 1
            # Try to record the candidate name for a report
            for regex in (NAME_RE_3, NAME_RE_2):
                m = regex.match(title)
                if m and m.group(1).split()[0] not in SKIP_FIRST:
                    unknown_signers[m.group(1)] += 1
                    break
            continue
        genre = signer_to_genre[signer]

        # Step 3 — product
        product = _detect_product(title)

        # Skip blank / unsigned stock — they shouldn't get Signed=Yes applied.
        tl = title.lower()
        if "blank" in tl or "unsigned" in tl:
            stats["skip_blank_unsigned"] += 1
            continue

        # Step 4 — decide target cat (only if --recat was passed)
        if product in LEAVE_ALONE_PRODUCTS or not args.recat:
            target_cat = cat_id            # keep where it is
            recat = False
        else:
            genre_map = cat_map.get(genre) or {}
            mapping = genre_map.get(product)
            if not mapping or mapping.get("id") == "??":
                stats["no_mapping"] += 1
                skipped_mapping[f"{genre}:{product}"] += 1
                continue
            target_cat = mapping["id"]
            recat = target_cat != cat_id

        # Step 5 — build target IS
        cat_overlay = bundle.specifics_for_category(target_cat) or {}
        signer_title = " ".join(w.capitalize() if w not in {"de","van"} else w
                                for w in signer.split())
        # preserve true casing from knowledge.yaml-style entries:
        # e.g. "sir david attenborough" → "Sir David Attenborough"
        signer_title = " ".join(w[0].upper() + w[1:] for w in signer.split())
        target_is = _merge_is(specs, defaults, cat_overlay,
                              signer_title, target_cat)

        is_change = target_is != specs
        if not recat and not is_change:
            stats["nochange"] += 1
            continue

        decision = {
            "item_id":      item_id,
            "title":        title,
            "signer":       signer_title,
            "genre":        genre,
            "product":      product,
            "from_cat":     f"{cat_id} {cat_name or ''}".strip(),
            "to_cat":       target_cat,
            "recat":        recat,
            "is_add":       sorted(set(target_is) - set(specs)),
            "is_change":    sorted(k for k in set(target_is) & set(specs)
                                   if target_is[k] != specs[k]),
        }

        if not args.apply:
            if len(preview_samples) < args.preview:
                preview_samples.append(decision)
            stats["would_change"] += 1
            continue

        # Step 6 — live revise
        try:
            kwargs: dict = {"confirm": True}
            if recat:
                kwargs["new_category_id"] = target_cat
                kwargs["new_condition_id"] = 3000  # Used — safe across cats
            if is_change:
                kwargs["new_specifics_replace"] = target_is
            result = lister.revise_listing(item_id, **kwargs)
            if result.get("ack") in ("Success", "Warning"):
                stats["revised"] += 1
                with audit_db.connect() as conn:
                    conn.execute(
                        "UPDATE listings SET specifics_json = ?, "
                        "category_id = COALESCE(?, category_id) "
                        "WHERE item_id = ?",
                        (json.dumps(target_is), target_cat if recat else None,
                         item_id),
                    )
                    conn.commit()
            else:
                stats["revise_fail"] += 1
                warns = result.get("warnings") or []
                msg = "; ".join(w.get("long","") for w in warns if w.get("long"))
                print(f"  ✗ [{item_id}] ack={result.get('ack')} {msg}")
            time.sleep(args.sleep_revise)
        except Exception as e:
            stats["revise_exception"] += 1
            print(f"  ✗ [{item_id}] EXCEPTION: {e}")

        if idx % 50 == 0:
            elapsed = time.monotonic() - start
            rate = idx / max(elapsed, 1)
            remain = (total - idx) / max(rate, 0.01) / 60
            print(f"  {idx}/{total}  {rate:.2f}/s  revised={stats['revised']} "
                  f"nochange={stats['nochange']} unk={stats['unknown_signer']} "
                  f"fail={stats['revise_fail']+stats['fetch_failed']}  "
                  f"remaining ~{remain:.0f}m", flush=True)

    # ─── Reporting ───────────────────────────────────────────────────
    elapsed = time.monotonic() - start
    print(f"\nElapsed: {elapsed/60:.1f}m")
    print("\n=== Stats ===")
    for k, v in stats.most_common():
        print(f"  {k:<20} {v}")

    if unknown_signers:
        print(f"\n=== Unknown signers (top 15) — add to signer_genre.yaml ===")
        for name, n in unknown_signers.most_common(15):
            print(f"  {n:<4} {name}")

    if skipped_mapping:
        print(f"\n=== Missing (genre × product) mappings ===")
        for key, n in skipped_mapping.most_common():
            print(f"  {n:<4} {key}")

    if not args.apply and preview_samples:
        print(f"\n=== Preview samples (first {len(preview_samples)}) ===")
        for d in preview_samples:
            print(f"\n[{d['item_id']}] {d['signer']} ({d['genre']}) "
                  f"{d['product']}")
            print(f"  Title: {d['title'][:90]}")
            if d['recat']:
                print(f"  RECAT: {d['from_cat']}  →  {d['to_cat']}")
            else:
                print(f"  Cat:   {d['from_cat']}  (stays)")
            if d['is_add']:
                print(f"  IS add   ({len(d['is_add'])}): {', '.join(d['is_add'][:8])}"
                      + ("..." if len(d['is_add'])>8 else ""))
            if d['is_change']:
                print(f"  IS change({len(d['is_change'])}): {', '.join(d['is_change'][:8])}"
                      + ("..." if len(d['is_change'])>8 else ""))

    if args.apply:
        with audit_db.connect() as conn:
            conn.execute(
                "INSERT INTO optimization_log (event, event_at, details) "
                "VALUES (?, ?, ?)",
                ("STREAM_RECAT_IS_DONE", _now(),
                 f"Streaming recat+IS done: {dict(stats)}"),
            )
            conn.commit()

    return 0


if __name__ == "__main__":
    sys.exit(main())
