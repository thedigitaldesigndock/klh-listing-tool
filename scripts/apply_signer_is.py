#!/usr/bin/env python3
"""
Apply the canonical Item Specifics template to every listing of a given signer.

This is the generalised version of add_br_item_specifics.py — drop any
signer name on the command line and it applies:

    Player/Athlete          <signer>
    Signed By               <signer>
    Sport                   <--sport, default: Football>
    Country/Region of Manufacture  <--country, default: United Kingdom>
    Modified Item           No

Plus the canonical Q&A specifics from defaults.yaml (Perfect For,
Autograph Type, Also Known As, COA Included, More In Our Shop,
Country of Origin, Signed, Original/Reproduction, Authenticity).

Plus per-listing derivations from the title:
    Team   — first match against knowledge.yaml clubs or known nations
    Size   — 6x4 / 10x8 / 12x8 / 16x12 / A4 / A3
    Type   — Framed Photo Display / Mounted Photo Display / Photo (+ DVD / Shirt edge cases)

Existing specifics are merged, not clobbered — we never delete a key.
Listings matching the signer filter but already carrying the full proposal
are skipped (no-op API calls).

Usage
-----
    # Dry-run (default) — prints summary + sample diffs, no API calls
    python scripts/apply_signer_is.py --signer "Teddy Sheringham"

    # Live
    python scripts/apply_signer_is.py --signer "Teddy Sheringham" --apply --yes

    # Non-football signers: override Sport (omit entirely if no sport)
    python scripts/apply_signer_is.py --signer "Jackie Chan" --sport ""

    # Manual country override (default United Kingdom)
    python scripts/apply_signer_is.py --signer "Mario Andretti" --country "Italy"
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from pipeline import audit_db, backlog, lister, presets as pp
from ebay_api import trading


SIZE_PATTERNS = [
    (re.compile(r"\b16x12\b", re.I), "16x12"),
    (re.compile(r"\b12x8\b",  re.I), "12x8"),
    (re.compile(r"\b10x8\b",  re.I), "10x8"),
    (re.compile(r"\b6x4\b",   re.I), "6x4"),
    (re.compile(r"\bA4\b",    re.I), "A4"),
    (re.compile(r"\bA3\b",    re.I), "A3"),
]

# Listings where photo-size shouldn't be set — non-photo products.
NON_PHOTO_RE = re.compile(r"\b(dvd|shirt|magazine)\b", re.I)

# National team keywords — used as Team fallback when no club appears in title.
NATION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bEngland\b", re.I),          "England"),
    (re.compile(r"\bScotland\b", re.I),         "Scotland"),
    (re.compile(r"\bWales\b", re.I),            "Wales"),
    (re.compile(r"\b(Northern Ireland|NI)\b", re.I), "Northern Ireland"),
    (re.compile(r"\bIreland\b", re.I),          "Ireland"),
    (re.compile(r"\bItaly\b", re.I),            "Italy"),
    (re.compile(r"\bGermany\b", re.I),          "Germany"),
    (re.compile(r"\bFrance\b", re.I),           "France"),
    (re.compile(r"\bSpain\b", re.I),            "Spain"),
    (re.compile(r"\bBrazil\b", re.I),           "Brazil"),
    (re.compile(r"\bArgentina\b", re.I),        "Argentina"),
    (re.compile(r"\bPortugal\b", re.I),         "Portugal"),
    (re.compile(r"\bNetherlands\b", re.I),      "Netherlands"),
    (re.compile(r"\bUSA\b", re.I),              "USA"),
]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_defaults_specifics() -> dict[str, str]:
    bundle = pp.load()
    return dict(bundle.defaults.get("item_specifics") or {})


def _build_club_patterns(bundle: pp.PresetsBundle) -> list[tuple[re.Pattern[str], str]]:
    """
    For every entry in knowledge.yaml `clubs:`, emit two regex patterns
    (short form + long form) both mapping to the short form. Patterns
    are sorted longest-first so "Manchester United" matches before
    "Manchester" gets confused with anything else.
    """
    clubs = bundle.knowledge.get("clubs") or {}
    pairs: list[tuple[str, str]] = []
    for short, full in clubs.items():
        if short:
            pairs.append((short, short))
        if full and full != short:
            pairs.append((full, short))
    # Longest match first — prevents "Man City" being eaten by "Man".
    pairs.sort(key=lambda p: -len(p[0]))
    return [(re.compile(rf"\b{re.escape(needle)}\b", re.I), target)
            for needle, target in pairs]


def _derive_size(title: str) -> Optional[str]:
    for pat, label in SIZE_PATTERNS:
        if pat.search(title):
            return label
    return None


def _derive_type(title: str) -> str:
    low = title.lower()
    if "shirt" in low:    return "Shirt"
    if "dvd" in low:      return "DVD"
    if "framed" in low:   return "Framed Photo Display"
    if "mount" in low:    return "Mounted Photo Display"
    return "Photo"


def _derive_team(
    title: str,
    club_patterns: list[tuple[re.Pattern[str], str]],
) -> Optional[str]:
    """Prefer a club match (any knowledge.yaml entry); else a nation; else None."""
    for pat, target in club_patterns:
        if pat.search(title):
            return target
    for pat, target in NATION_PATTERNS:
        if pat.search(title):
            return target
    return None


def _is_non_photo(title: str) -> bool:
    return bool(NON_PHOTO_RE.search(title))


def _propose_specifics(
    current: dict[str, str],
    title: str,
    *,
    defaults: dict[str, str],
    signer_constants: dict[str, str],
    club_patterns: list[tuple[re.Pattern[str], str]],
) -> dict[str, str]:
    merged: dict[str, str] = dict(current)
    merged.update(defaults)
    merged.update(signer_constants)
    team = _derive_team(title, club_patterns)
    if team:
        merged["Team"] = team
    if not _is_non_photo(title):
        size = _derive_size(title)
        if size:
            merged["Size"] = size
    merged["Type"] = _derive_type(title)
    return merged


def _diff(before: dict[str, str], after: dict[str, str]) -> list[str]:
    lines: list[str] = []
    for k in sorted(set(before) | set(after)):
        b, a = before.get(k), after.get(k)
        if b is None and a is not None:
            lines.append(f"    + {k}: {a}")
        elif b is not None and a is None:
            lines.append(f"    - {k}: {b}")
        elif b != a:
            lines.append(f"    ~ {k}: {b}  →  {a}")
    return lines


def _deep_fetch_missing(conn, signer_filter: str, rate_per_sec: float) -> int:
    """Deep-fetch any rows for this signer that lack item specifics. Return count fetched."""
    rows = conn.execute(
        "SELECT item_id FROM listings WHERE LOWER(title) LIKE ? "
        "AND deep_fetched_at IS NULL ORDER BY item_id",
        (signer_filter,),
    ).fetchall()
    candidates = [r["item_id"] for r in rows]
    if not candidates:
        return 0
    sleep = 1.0 / max(rate_per_sec, 0.1)
    print(f"Deep-fetching {len(candidates)} missing listings "
          f"(rate={rate_per_sec}/s, ETA ~{len(candidates) * sleep / 60:.1f}m)…")
    start = time.monotonic()
    fetched = 0
    for i, (item_id, deep) in enumerate(trading.get_items_bulk(
        candidates, sleep=sleep, progress=lambda iid, d, e: None
    ), 1):
        if deep is None:
            continue
        audit_db.upsert_deep(conn, item_id, deep)
        fetched += 1
        if i % 30 == 0:
            print(f"  {i}/{len(candidates)} ({i/(time.monotonic()-start):.1f}/s)")
            conn.commit()
    conn.commit()
    print(f"  deep-fetch done: {fetched} ok\n")
    return fetched


def _load_candidates(conn, signer_filter: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT item_id, title, watch_count, price_gbp, specifics_json
        FROM listings
        WHERE LOWER(title) LIKE ? AND deep_fetched_at IS NOT NULL
        ORDER BY item_id
        """,
        (signer_filter,),
    ).fetchall()
    out = []
    for r in rows:
        specifics = json.loads(r["specifics_json"]) if r["specifics_json"] else {}
        out.append({
            "item_id":     r["item_id"],
            "title":       r["title"],
            "watch_count": r["watch_count"] or 0,
            "price_gbp":   r["price_gbp"],
            "current":     specifics,
        })
    return out


def _print_dry_run(
    candidates: list[dict],
    *,
    defaults: dict[str, str],
    signer_constants: dict[str, str],
    club_patterns: list[tuple[re.Pattern[str], str]],
    sample_n: int,
) -> dict:
    stats = {"total": len(candidates), "no_change": 0, "to_write": 0,
             "skipped_no_size": 0, "net_additions": 0}
    shown = 0
    for c in candidates:
        proposed = _propose_specifics(
            c["current"], c["title"],
            defaults=defaults, signer_constants=signer_constants,
            club_patterns=club_patterns,
        )
        if proposed == c["current"]:
            stats["no_change"] += 1
            continue
        stats["to_write"] += 1
        stats["net_additions"] += len(set(proposed) - set(c["current"]))
        if "Size" not in proposed and not _is_non_photo(c["title"]):
            stats["skipped_no_size"] += 1
        if shown < sample_n:
            print(f"\n  [{c['item_id']}]  watch={c['watch_count']}  "
                  f"£{c['price_gbp']}  {c['title']}")
            for line in _diff(c["current"], proposed):
                print(line)
            shown += 1
    return stats


def _apply(
    conn,
    candidates: list[dict],
    *,
    signer: str,
    defaults: dict[str, str],
    signer_constants: dict[str, str],
    club_patterns: list[tuple[re.Pattern[str], str]],
    rate_per_sec: float,
) -> None:
    sleep = 1.0 / max(rate_per_sec, 0.1)
    targets = []
    for c in candidates:
        proposed = _propose_specifics(
            c["current"], c["title"],
            defaults=defaults, signer_constants=signer_constants,
            club_patterns=club_patterns,
        )
        if proposed != c["current"]:
            targets.append((c, proposed))

    print(f"\nApplying IS to {len(targets)} listings "
          f"(rate={rate_per_sec}/s, ETA ~{len(targets) * sleep / 60:.1f}m)\n")
    conn.execute(
        "INSERT INTO optimization_log (event, event_at, details) VALUES (?, ?, ?)",
        ("SIGNER_IS_START", _now(), f"{signer}: {len(targets)} listings"),
    )
    conn.commit()

    ok = fail = 0
    start = time.monotonic()
    for i, (c, proposed) in enumerate(targets, 1):
        try:
            result = lister.revise_listing(
                c["item_id"], new_specifics_replace=proposed, confirm=True,
            )
            if result.get("ack") in ("Success", "Warning"):
                ok += 1
                conn.execute(
                    "UPDATE listings SET specifics_json = ? WHERE item_id = ?",
                    (json.dumps(proposed), c["item_id"]),
                )
            else:
                fail += 1
                warnings = result.get("warnings") or []
                msgs = "; ".join(w.get("long", "") for w in warnings if w.get("long"))
                print(f"  ✗ [{c['item_id']}] ack={result.get('ack')}  {msgs}")
        except Exception as e:
            fail += 1
            print(f"  ✗ [{c['item_id']}] EXCEPTION: {e}")

        if i % 20 == 0:
            print(f"  {i}/{len(targets)} ({i/(time.monotonic()-start):.1f}/s, ok={ok} fail={fail})")
            conn.commit()
        time.sleep(sleep)

    conn.commit()
    elapsed = time.monotonic() - start
    print(f"\nDone: {ok} succeeded, {fail} failed in {elapsed:.0f}s")
    conn.execute(
        "INSERT INTO optimization_log (event, event_at, details) VALUES (?, ?, ?)",
        ("SIGNER_IS_DONE", _now(), f"{signer}: ok={ok} fail={fail}"),
    )
    # Log remaining backlog entry if anything still needs attention.
    if fail == 0 and ok > 0:
        backlog.resolve_if_matching = None  # placeholder — no auto-resolve yet
    conn.commit()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--signer", required=True,
                   help='Signer name, e.g. "Teddy Sheringham" (case-insensitive prefix match)')
    p.add_argument("--sport", default="Football",
                   help='Sport IS value (pass "" to skip). Default: Football')
    p.add_argument("--country", default="United Kingdom",
                   help='Country/Region of Manufacture. Default: United Kingdom')
    p.add_argument("--apply", action="store_true",
                   help="actually call ReviseFixedPriceItem (dry-run otherwise)")
    p.add_argument("--rate", type=float, default=1.0,
                   help="API calls per second (default 1.0)")
    p.add_argument("--yes", action="store_true",
                   help="skip interactive confirmation (for non-tty)")
    p.add_argument("--sample", type=int, default=3,
                   help="per-listing diffs to print in dry-run (default 3)")
    p.add_argument("--deep-fetch", action="store_true",
                   help="first deep-fetch any signer listings lacking specifics")
    args = p.parse_args()

    signer = args.signer.strip()
    if not signer:
        print("--signer is required"); return 1
    signer_filter = f"%{signer.lower()}%"

    signer_constants: dict[str, str] = {
        "Player/Athlete":                signer,
        "Signed By":                     signer,
        "Country/Region of Manufacture": args.country,
        "Modified Item":                 "No",
    }
    if args.sport:
        signer_constants["Sport"] = args.sport

    bundle = pp.load()
    defaults = dict(bundle.defaults.get("item_specifics") or {})
    club_patterns = _build_club_patterns(bundle)
    print(f"Loaded {len(defaults)} Q&A specifics from defaults.yaml, "
          f"{len(club_patterns)} club patterns")

    with audit_db.connect() as conn:
        if args.deep_fetch:
            _deep_fetch_missing(conn, signer_filter, rate_per_sec=2.0)

        candidates = _load_candidates(conn, signer_filter)
        if not candidates:
            print(f"No deep-fetched listings found for {signer!r}.")
            print("Pass --deep-fetch to pull them, or check the name spelling.")
            return 1

        print(f"\n=== {signer} IS proposal ({len(candidates)} candidates) ===")
        stats = _print_dry_run(
            candidates,
            defaults=defaults, signer_constants=signer_constants,
            club_patterns=club_patterns, sample_n=args.sample,
        )
        print(f"\n=== Summary ===")
        print(f"  Total listings:                  {stats['total']}")
        print(f"  Already fine, no change:         {stats['no_change']}")
        print(f"  Will be revised:                 {stats['to_write']}")
        print(f"  Net new specific key-values:     {stats['net_additions']}")
        if stats['skipped_no_size']:
            print(f"  ⚠ Photo listings without size token in title: {stats['skipped_no_size']}")

        if not args.apply:
            print("\n[DRY RUN] Pass --apply to write live.")
            return 0
        if not args.yes:
            confirm = input("\nProceed with live revisions? [yes/no] ").strip().lower()
            if confirm not in ("yes", "y"):
                print("Aborted."); return 1

        _apply(
            conn, candidates,
            signer=signer, defaults=defaults,
            signer_constants=signer_constants, club_patterns=club_patterns,
            rate_per_sec=args.rate,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
