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

# Signed-card detection. Cards don't have a single photo team — buyers know
# they're buying a signature only, so multiple clubs in title are fine.
# We skip Team IS derivation + title rebuild for cards.
CARD_RE = re.compile(r"\bSigned\s+Card\b", re.I)

# Lowercase tokens we know are title "furniture" — signing vocab, sizes,
# product shape words, fillers. Used to detect unknown capitalised words
# in a title (probably a typo'd team we'd be silently losing on rewrite).
_TITLE_SAFE_TOKENS = {
    # signing vocab
    "hand", "signed", "autograph", "autographed", "signature", "auto",
    # product nouns
    "photo", "photos", "photograph", "photography", "picture", "image",
    "print", "poster", "card", "cards", "memorabilia", "merch", "merchandise",
    "gift", "present",
    # shape
    "framed", "frame", "mount", "mounted", "display", "displayed",
    # COA variants
    "coa", "+coa", "coa,", "w/coa", "+", "cert", "certificate", "loa",
    "letter", "authentic", "authenticity", "certified", "genuine",
    "original", "authentic.",
    # size tokens (both forms)
    "6x4", "10x8", "12x8", "16x12", "a4", "a3", "inch", "inches",
    # joiners
    "with", "&", "/", "-", "the",
}


def _has_unknown_capitalised_word(title: str, signer: str) -> bool:
    """
    Return True if title contains a capitalised word (or token) that isn't:
      - part of the signer's name
      - a size token (10x8 etc.)
      - in _TITLE_SAFE_TOKENS
      - a recognised team mention (handled upstream by _derive_team)

    Used as a guard when _derive_team returns None: if we can't derive a
    team BUT the title has mystery capitalised words (e.g. "Hotpur" typo
    for "Hotspur"), skip the title rewrite to avoid silently dropping
    information the buyer might be searching for.
    """
    signer_parts = {p.lower() for p in signer.split() if p}
    # Tokenise on whitespace; strip trailing punctuation.
    for raw in title.split():
        tok = raw.strip(".,;:!?()[]").strip()
        if not tok:
            continue
        low = tok.lower()
        if low in signer_parts:
            continue
        if low in _TITLE_SAFE_TOKENS:
            continue
        # Ignore pure-numeric or size-like tokens we didn't catch above.
        if re.fullmatch(r"\d+", tok) or re.fullmatch(r"\d+x\d+", tok, re.I):
            continue
        # Capitalised? (first letter upper, rest of word present)
        if tok[0].isupper() and len(tok) > 1:
            return True
    return False

# Multi-pack/job-lot listings like "7x Bryan Robson Hand Signed 6x4..." or
# "Lot of 5 …" shouldn't be rewritten to singular form — those are genuinely
# different products. Guard: title starts with "<digit(s)>x" or contains
# the word "lot of" / "joblot" near the start.
MULTIPACK_RE = re.compile(
    r"^\s*(\d+\s*x\b|lot\s+of\b|joblot\b|job\s+lot\b)",
    re.I,
)

# Map (Size, Type) → products.yaml product_key. Used when we render a
# canonical title via pipeline.presets.render_title(). Non-standard
# shapes (DVD, Shirt, etc.) return None — title cleanup skips them.
SIZE_TYPE_TO_PRODUCT_KEY: dict[tuple[str, str], str] = {
    ("6x4",   "Photo"):                  "photo_6x4",
    ("10x8",  "Photo"):                  "photo_10x8",
    ("12x8",  "Photo"):                  "photo_12x8",
    ("10x8",  "Framed Photo Display"):   "10x8_frame",
    ("10x8",  "Mounted Photo Display"):  "10x8_mount",
    ("A4",    "Framed Photo Display"):   "a4_frame_a",
    ("A4",    "Mounted Photo Display"):  "a4_mount_a",
    ("16x12", "Framed Photo Display"):   "16x12_frame_a",
    ("16x12", "Mounted Photo Display"):  "16x12_mount_a",
}

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
    For every entry in knowledge.yaml `clubs:`, emit regex patterns
    matching either the short or long form in a title, both mapping to
    the LONG (formal) form. The long form is eBay's canonical Team
    value for the filter sidebar and has higher search volume than
    fan-slang short forms.

    Patterns are sorted longest-first so "Manchester United" matches
    before a shorter substring could incorrectly land.
    """
    clubs = bundle.knowledge.get("clubs") or {}
    pairs: list[tuple[str, str]] = []
    for short, full in clubs.items():
        canonical = full or short  # entries without a different full form
        if short:
            pairs.append((short, canonical))
        if full and full != short:
            pairs.append((full, canonical))
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
    """Derive Team from a title using the policy:

    1. If any nation name appears, use the FIRST nation found.
       Nicky's keyword stuffing often appended nations ("Man Utd England")
       but we've seen the photo is commonly the less-common/specific team
       (England) rather than the primary one. Nation wins over club when
       both appear.

    2. Else find the FIRST-occurring club in the title. The convention
       when multiple clubs are listed ("Spurs Man Utd ...") is that the
       first one is the photo subject — Nicky types the correct team for
       the current photo first, then appends secondary teams as keyword
       stuffing.

    3. Else None.
    """
    # 1. Any nation wins over any club.
    for pat, target in NATION_PATTERNS:
        if pat.search(title):
            return target

    # 2. Collect all club matches with their position, pick first.
    first_match_pos = None
    first_target: Optional[str] = None
    for pat, target in club_patterns:
        m = pat.search(title)
        if m is not None and (first_match_pos is None or m.start() < first_match_pos):
            first_match_pos = m.start()
            first_target = target
    return first_target


def _is_non_photo(title: str) -> bool:
    return bool(NON_PHOTO_RE.search(title))


def _is_card(title: str) -> bool:
    return bool(CARD_RE.search(title))


def _propose_title(
    bundle: pp.PresetsBundle,
    *,
    signer: str,
    title: str,
    category: str,
    club_patterns: list[tuple[re.Pattern[str], str]],
) -> Optional[str]:
    """
    Build the canonical title using the listing tool's render_title().
    Returns None for shapes we can't map to a product_key (DVD, Shirt,
    non-photo edge cases) — caller leaves the title alone.

    Uses the same builder as new listings from the dashboard, so a
    retrofitted title is byte-identical to what a fresh listing would
    get. Team is sourced from the same derivation we use for IS, which
    already routes through shrink_club for Man Utd vs Manchester United.
    """
    if _is_non_photo(title):
        return None
    if _is_card(title):
        return None  # cards can legitimately carry multiple clubs in title
    if MULTIPACK_RE.match(title):
        return None  # "7x Bryan Robson…" — leave multi-pack titles alone
    size = _derive_size(title)
    if not size:
        return None

    team = _derive_team(title, club_patterns)
    # Safeguard against typo'd team names (e.g. "Tottenham Hotpur" missing 's'):
    # if we can't derive a team but the title has a capitalised word we don't
    # recognise, skip the rewrite rather than silently drop the team text.
    if team is None and _has_unknown_capitalised_word(title, signer):
        return None
    ptype = _derive_type(title)
    key = SIZE_TYPE_TO_PRODUCT_KEY.get((size, ptype))
    if not key:
        return None

    try:
        return pp.render_title(bundle, key, signer, field1=team, category=category)
    except pp.PresetsError:
        # Name too long for this product + team combination — leave title alone.
        return None


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

    is_card = _is_card(title)
    # Cards legitimately carry multiple clubs in the title — the buyer is
    # buying the signature, not a team-specific photo. Don't force a
    # single Team onto them; leave whatever was there (or absent) alone.
    if not is_card:
        team = _derive_team(title, club_patterns)
        if team:
            merged["Team"] = team

    if not _is_non_photo(title) and not is_card:
        size = _derive_size(title)
        if size:
            merged["Size"] = size
    merged["Type"] = "Card" if is_card else _derive_type(title)
    # Media Type only for photo-shaped products. Cards, Shirts, DVDs skipped.
    if merged["Type"] in {"Photo", "Framed Photo Display", "Mounted Photo Display"}:
        merged["Media Type"] = "Photograph Photography Picture Image Original Print"
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
    bundle: pp.PresetsBundle,
    signer: str,
    category: str,
    defaults: dict[str, str],
    signer_constants: dict[str, str],
    club_patterns: list[tuple[re.Pattern[str], str]],
    update_titles: bool,
    sample_n: int,
) -> dict:
    stats = {"total": len(candidates), "no_change": 0, "is_only": 0,
             "title_only": 0, "both": 0,
             "skipped_no_size": 0, "net_additions": 0, "titles_unchanged": 0}
    shown = 0
    for c in candidates:
        proposed_is = _propose_specifics(
            c["current"], c["title"],
            defaults=defaults, signer_constants=signer_constants,
            club_patterns=club_patterns,
        )
        is_change = proposed_is != c["current"]

        title_change = False
        new_title = None
        if update_titles:
            new_title = _propose_title(
                bundle, signer=signer, title=c["title"], category=category,
                club_patterns=club_patterns,
            )
            if new_title and new_title != c["title"]:
                title_change = True
            elif new_title is None:
                stats["titles_unchanged"] += 1

        if not (is_change or title_change):
            stats["no_change"] += 1
            continue
        if is_change and title_change:
            stats["both"] += 1
        elif is_change:
            stats["is_only"] += 1
        else:
            stats["title_only"] += 1

        stats["net_additions"] += len(set(proposed_is) - set(c["current"]))
        if "Size" not in proposed_is and not _is_non_photo(c["title"]):
            stats["skipped_no_size"] += 1

        if shown < sample_n:
            print(f"\n  [{c['item_id']}]  watch={c['watch_count']}  £{c['price_gbp']}")
            if title_change:
                print(f"    title: {c['title']}")
                print(f"        →  {new_title}")
            else:
                print(f"    title: {c['title']}  (unchanged)")
            for line in _diff(c["current"], proposed_is):
                print(line)
            shown += 1
    return stats


def _apply(
    conn,
    candidates: list[dict],
    *,
    bundle: pp.PresetsBundle,
    signer: str,
    category: str,
    defaults: dict[str, str],
    signer_constants: dict[str, str],
    club_patterns: list[tuple[re.Pattern[str], str]],
    update_titles: bool,
    rate_per_sec: float,
) -> None:
    sleep = 1.0 / max(rate_per_sec, 0.1)
    # Build the target list: for each listing figure out what (if anything)
    # to revise. A listing is skipped only when BOTH the IS proposal and
    # the title proposal produce no change.
    targets: list[tuple[dict, dict, Optional[str]]] = []
    for c in candidates:
        proposed_is = _propose_specifics(
            c["current"], c["title"],
            defaults=defaults, signer_constants=signer_constants,
            club_patterns=club_patterns,
        )
        is_change = proposed_is != c["current"]

        new_title: Optional[str] = None
        if update_titles:
            candidate_title = _propose_title(
                bundle, signer=signer, title=c["title"], category=category,
                club_patterns=club_patterns,
            )
            if candidate_title and candidate_title != c["title"]:
                new_title = candidate_title

        if is_change or new_title is not None:
            targets.append((c, proposed_is if is_change else None, new_title))

    print(f"\nApplying to {len(targets)} listings "
          f"(rate={rate_per_sec}/s, ETA ~{len(targets) * sleep / 60:.1f}m)\n")
    conn.execute(
        "INSERT INTO optimization_log (event, event_at, details) VALUES (?, ?, ?)",
        ("SIGNER_IS_START", _now(),
         f"{signer}: {len(targets)} listings, titles={'on' if update_titles else 'off'}"),
    )
    conn.commit()

    ok = fail = 0
    start = time.monotonic()
    for i, (c, proposed_is, new_title) in enumerate(targets, 1):
        try:
            kwargs: dict = {"confirm": True}
            if proposed_is is not None:
                kwargs["new_specifics_replace"] = proposed_is
            if new_title is not None:
                kwargs["new_title"] = new_title
            result = lister.revise_listing(c["item_id"], **kwargs)
            if result.get("ack") in ("Success", "Warning"):
                ok += 1
                # Update local cache so re-runs know we already revised.
                if proposed_is is not None:
                    conn.execute(
                        "UPDATE listings SET specifics_json = ? WHERE item_id = ?",
                        (json.dumps(proposed_is), c["item_id"]),
                    )
                if new_title is not None:
                    conn.execute(
                        "UPDATE listings SET title = ? WHERE item_id = ?",
                        (new_title, c["item_id"]),
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
    p.add_argument("--update-titles", action="store_true",
                   help="also rewrite titles to the canonical form "
                        "(replaces legacy keyword-reorder titles)")
    p.add_argument("--category", default="Football",
                   help="category for title rendering + Team derivation (default: Football)")
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

        print(f"\n=== {signer} proposal ({len(candidates)} candidates, "
              f"titles={'ON' if args.update_titles else 'off'}) ===")
        stats = _print_dry_run(
            candidates,
            bundle=bundle, signer=signer, category=args.category,
            defaults=defaults, signer_constants=signer_constants,
            club_patterns=club_patterns, update_titles=args.update_titles,
            sample_n=args.sample,
        )
        print(f"\n=== Summary ===")
        print(f"  Total listings:                  {stats['total']}")
        print(f"  Already fine, no change:         {stats['no_change']}")
        print(f"  Will update IS only:             {stats['is_only']}")
        print(f"  Will update title only:          {stats['title_only']}")
        print(f"  Will update both:                {stats['both']}")
        print(f"  Net new specific key-values:     {stats['net_additions']}")
        if stats['skipped_no_size']:
            print(f"  ⚠ Photo listings without size token in title: {stats['skipped_no_size']}")
        if args.update_titles and stats['titles_unchanged']:
            print(f"  (non-photo / unmapped listings keeping current title: "
                  f"{stats['titles_unchanged']})")

        if not args.apply:
            print("\n[DRY RUN] Pass --apply to write live.")
            return 0
        if not args.yes:
            confirm = input("\nProceed with live revisions? [yes/no] ").strip().lower()
            if confirm not in ("yes", "y"):
                print("Aborted."); return 1

        _apply(
            conn, candidates,
            bundle=bundle, signer=signer, category=args.category,
            defaults=defaults, signer_constants=signer_constants,
            club_patterns=club_patterns, update_titles=args.update_titles,
            rate_per_sec=args.rate,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
