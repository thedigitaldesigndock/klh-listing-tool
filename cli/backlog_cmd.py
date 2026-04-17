"""
`klh backlog` — view / add / resolve the self-populating improvement queue.

Usage:
    klh backlog list               # open items, newest + most-seen first
    klh backlog list --topic ads   # filter by topic
    klh backlog stats              # counts by topic × status
    klh backlog seed               # ensure the starting roadmap entries exist
    klh backlog add                # manual entry (prompts for fields)
    klh backlog resolve <id>       # mark done
    klh backlog ignore  <id>       # hide (won't resurface in `list`)
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from typing import Optional

from pipeline import audit_db, backlog, presets as pp


# Lowercase tokens we expect to see in KLH titles — the "vocabulary".
# Anything capitalised in a title that lowercases to something NOT in this
# set, not in a team name, and not a signer name is a possible typo.
_TITLE_VOCAB = {
    # signing vocab
    "hand", "signed", "autograph", "autographed", "signature", "auto",
    # product nouns
    "photo", "photos", "photograph", "photography", "picture", "image",
    "print", "poster", "card", "cards", "memorabilia", "merch", "merchandise",
    "gift", "present",
    # shape
    "framed", "frame", "mount", "mounted", "display", "displayed",
    # COA variants
    "coa", "+coa", "w/coa", "+", "cert", "certificate", "loa",
    "letter", "authentic", "authenticity", "certified", "genuine",
    "original", "inc", "incl", "included", "includes",
    # sizes
    "6x4", "10x8", "12x8", "16x12", "a4", "a3", "a5", "inch", "inches",
    # joiners / filler
    "with", "&", "/", "-", "the", "and", "for", "of", "in", "on", "at",
    "real", "vintage", "very", "rare", "limited", "edition",
}

# Common nation names used in autograph titles.
_NATION_WORDS = {
    "england", "scotland", "wales", "ireland", "northern", "britain",
    "gb", "uk", "italy", "germany", "france", "spain", "brazil",
    "argentina", "portugal", "netherlands", "usa", "us", "canada",
}


def _cmd_list(args: argparse.Namespace) -> int:
    with audit_db.connect() as conn:
        rows = backlog.list_open(conn, topic=args.topic, limit=args.limit)

    if not rows:
        suffix = f" in topic={args.topic!r}" if args.topic else ""
        print(f"No open backlog items{suffix}.")
        return 0

    print(f"{'ID':>4}  {'TOPIC':<18}  {'CNT':>3}  TITLE")
    print("-" * 90)
    for r in rows:
        print(f"{r['id']:>4}  {r['topic']:<18}  {r['count']:>3}  {r['title']}")
        if args.verbose and r.get("details"):
            for line in (r["details"] or "").splitlines():
                print(f"         {line}")
    print(f"\n({len(rows)} open)")
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    with audit_db.connect() as conn:
        stats = backlog.stats(conn)
    if not stats:
        print("Backlog is empty. Run `klh backlog seed` to populate the starting roadmap.")
        return 0
    print(f"{'TOPIC':<22} {'OPEN':>6} {'RESOLVED':>10} {'IGNORED':>9}")
    print("-" * 52)
    for topic in sorted(stats):
        row = stats[topic]
        print(f"{topic:<22} {row.get('open',0):>6} "
              f"{row.get('resolved',0):>10} {row.get('ignored',0):>9}")
    return 0


def _cmd_seed(args: argparse.Namespace) -> int:
    with audit_db.connect() as conn:
        inserted = backlog.seed_initial_roadmap(conn)
        conn.commit()
    print(f"Seeded {inserted} new roadmap entries.")
    return 0


def _cmd_add(args: argparse.Namespace) -> int:
    topic = args.topic or input("Topic (e.g. roadmap, ads, title_cleanup): ").strip()
    if not topic:
        print("Aborted: topic is required.")
        return 1
    key = args.key or input("Key (short de-dup identifier): ").strip()
    if not key:
        print("Aborted: key is required.")
        return 1
    title = args.title or input("Title (one-line summary): ").strip()
    if not title:
        print("Aborted: title is required.")
        return 1
    details = args.details
    if details is None:
        details = input("Details (optional, blank to skip): ").strip() or None

    with audit_db.connect() as conn:
        backlog.note(conn, topic=topic, key=key, title=title,
                     details=details, source="human")
        conn.commit()
    print(f"Logged: [{topic}] {title}")
    return 0


def _cmd_resolve(args: argparse.Namespace) -> int:
    with audit_db.connect() as conn:
        n = backlog.resolve(conn, args.id)
        conn.commit()
    print(f"Resolved {n} row(s).")
    return 0


def _cmd_ignore(args: argparse.Namespace) -> int:
    with audit_db.connect() as conn:
        n = backlog.ignore(conn, args.id)
        conn.commit()
    print(f"Ignored {n} row(s).")
    return 0


# Categories known to be wrong for football signatures (used by the
# miscategorisation scan). Add to this list as we discover more sell-
# similar pollution patterns.
KNOWN_MISCAT_FOR_FOOTBALL = {
    "35030": "Films & TV: TV Memorabilia: Autographs: Male",
    "35028": "Films & TV: Film Memorabilia: Autographs: Female",
    "86984": "Sports Mem: Darts Memorabilia",
    "211":   "Collectables: Other Memorabilia",
    "27289": "Football Mem: Signed Shirts (wrong for signed photos)",
    "2885":  "Football Mem: Other Football Memorabilia (too generic)",
}


def _discover_alias_opportunities(conn, bundle, verbose: bool) -> int:
    """Pass 1: legacy titles with long-form club names that could be shortened."""
    clubs = bundle.knowledge.get("clubs") or {}
    found = 0
    for short_name, long_name in clubs.items():
        if not long_name or long_name == short_name:
            continue
        saving = len(long_name) - len(short_name)
        if saving <= 0:
            continue
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM listings "
            "WHERE INSTR(LOWER(title), LOWER(?)) > 0 "
            "  AND INSTR(LOWER(title), LOWER(?)) = 0",
            (long_name, short_name),
        ).fetchone()
        n = int(row["n"])
        if n <= 0:
            continue
        backlog.note(
            conn, topic="alias_discovery",
            key=f"longform_team:{long_name}",
            title=(f"{n} legacy titles contain '{long_name}' — "
                   f"'{short_name}' would save {saving} chars"),
            details=f"short='{short_name}'  long='{long_name}'  count={n}  char_saving={saving}",
            source="backlog discover",
        )
        found += 1
        if verbose:
            print(f"  alias: {long_name!r} × {n}  (→ {short_name!r}, saves {saving} chars)")
    return found


def _discover_football_miscats(conn, bundle, verbose: bool) -> int:
    """Pass 2: listings in wrong-for-football cats with team names in title.

    Only runs against listings we have `category_id` for (i.e.
    deep-fetched post commit 2d9a2e3). Until we backfill the full
    catalogue this only sees a slice — count grows as we fetch more.
    """
    clubs = bundle.knowledge.get("clubs") or {}
    club_terms: list[str] = []
    for short, full in clubs.items():
        if short: club_terms.append(short)
        if full and full != short: club_terms.append(full)
    # LIKE-friendly — just need ANY mention
    like_clauses = " OR ".join(
        ["INSTR(LOWER(title), LOWER(?)) > 0"] * len(club_terms)
    )

    found = 0
    for bad_cat, label in KNOWN_MISCAT_FOR_FOOTBALL.items():
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM listings "
            f"WHERE category_id = ? AND ({like_clauses})",
            [bad_cat, *club_terms],
        ).fetchone()
        n = int(row["n"])
        if n <= 0:
            continue
        backlog.note(
            conn, topic="category_fix",
            key=f"football_names_in_cat:{bad_cat}",
            title=(f"{n} listings in cat {bad_cat} ({label[:50]}) have "
                   f"football team names in title"),
            details=(f"Suggests sell-similar miscategorisation. Target cat "
                     f"depends on signer status (97085 for retired players, "
                     f"27290 for current Premiership). Source cat label: {label}"),
            source="backlog discover",
        )
        found += 1
        if verbose:
            print(f"  miscat: cat={bad_cat} ({label[:40]}) × {n} listings")
    return found


def _discover_title_typos(conn, bundle, verbose: bool) -> int:
    """Pass 3: scan every title for capitalised words that aren't in our
    known vocabulary (signing terms, sizes, teams, nations) or the signer
    names seen in the catalogue. Likely candidates for typos or teams
    we should add to knowledge.yaml.

    Logs rows grouped by the unknown word, each with a representative
    count of how many listings contain it. Low-count words (1-5) are the
    most likely typos. High-count words may be legitimate teams/events
    we've missed — still worth a human look.
    """
    clubs = bundle.knowledge.get("clubs") or {}
    known_team_words: set[str] = set()
    for short, full in clubs.items():
        for phrase in (short, full):
            if phrase:
                for word in phrase.lower().split():
                    known_team_words.add(word.strip("&/-"))

    # Build a "signer name" set from the catalogue — first two capitalised
    # words of each title. Not perfect but filters out the most common
    # signer tokens so we don't flag them as typos.
    signer_tokens: set[str] = set()
    for r in conn.execute("SELECT title FROM listings"):
        if not r["title"]:
            continue
        parts = r["title"].split()
        for p in parts[:2]:
            tok = p.strip(".,;:!?()[]-")
            if tok and tok[0].isupper():
                signer_tokens.add(tok.lower())

    unknown: Counter[str] = Counter()
    for r in conn.execute("SELECT title FROM listings"):
        title = r["title"] or ""
        for raw in title.split():
            tok = raw.strip(".,;:!?()[]").strip()
            if not tok or len(tok) < 2:
                continue
            low = tok.lower()
            if low in _TITLE_VOCAB or low in _NATION_WORDS:
                continue
            if low in known_team_words or low in signer_tokens:
                continue
            if re.fullmatch(r"\d+(x\d+)?", tok, re.I):
                continue
            # Capitalised? Count it.
            if tok[0].isupper():
                unknown[low] += 1

    # Only surface the top-N unknowns — everything else is long-tail noise.
    top = unknown.most_common(60)
    if verbose:
        print("  top unknown capitalised tokens (candidate typos / missing teams):")
        for word, n in top:
            marker = "  ← likely typo" if n <= 5 else ""
            print(f"    {word:<20} × {n}{marker}")

    logged = 0
    for word, n in top:
        # Skip extremely common ones (>200) — they're almost certainly
        # signer tokens that slipped through the signer-name filter.
        if n > 200:
            continue
        backlog.note(
            conn, topic="title_typos",
            key=f"unknown_token:{word}",
            title=(f"{n} titles contain unrecognised capitalised word "
                   f"'{word}' — typo, missing team alias, or rare signer?"),
            details=(f"token='{word}'  count={n}  (low count = likely typo, "
                     f"higher count = missing team or small signer cohort)"),
            source="backlog discover",
        )
        logged += 1
    return logged


def _cmd_discover(args: argparse.Namespace) -> int:
    """Scan cached catalogue for improvement opportunities.

    Passes:
      1. alias_discovery  — legacy titles with long-form teams (savable chars)
      2. category_fix     — listings in known-wrong cats with team names in title
      3. title_typos      — unrecognised capitalised tokens in titles

    Each pass uses note() so repeated runs bump `count` instead of
    duplicating. Resolved/ignored entries stay suppressed.
    """
    bundle = pp.load()
    with audit_db.connect() as conn:
        a = _discover_alias_opportunities(conn, bundle, args.verbose)
        m = _discover_football_miscats(conn, bundle, args.verbose)
        t = _discover_title_typos(conn, bundle, args.verbose)
        conn.commit()
    print(f"Discovery pass complete: alias={a}  miscat={m}  typos={t}  "
          f"(run `klh backlog list` to review)")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("backlog", help="self-populating improvement queue")
    psub = p.add_subparsers(dest="backlog_cmd", required=True)

    p_list = psub.add_parser("list", help="show open backlog items")
    p_list.add_argument("--topic", help="filter by topic (e.g. 'ads')")
    p_list.add_argument("--limit", type=int, help="max rows")
    p_list.add_argument("-v", "--verbose", action="store_true",
                        help="also print details for each row")
    p_list.set_defaults(func=_cmd_list)

    p_stats = psub.add_parser("stats", help="counts by topic × status")
    p_stats.set_defaults(func=_cmd_stats)

    p_seed = psub.add_parser("seed", help="insert starting roadmap entries (idempotent)")
    p_seed.set_defaults(func=_cmd_seed)

    p_add = psub.add_parser("add", help="add a manual entry (prompts for fields)")
    p_add.add_argument("--topic")
    p_add.add_argument("--key")
    p_add.add_argument("--title")
    p_add.add_argument("--details")
    p_add.set_defaults(func=_cmd_add)

    p_res = psub.add_parser("resolve", help="mark a row resolved")
    p_res.add_argument("id", type=int)
    p_res.set_defaults(func=_cmd_resolve)

    p_ign = psub.add_parser("ignore", help="hide a row from `list`")
    p_ign.add_argument("id", type=int)
    p_ign.set_defaults(func=_cmd_ignore)

    p_disc = psub.add_parser("discover",
                             help="scan catalogue for alias/cleanup opportunities")
    p_disc.add_argument("-v", "--verbose", action="store_true",
                        help="print each opportunity as it's found")
    p_disc.set_defaults(func=_cmd_discover)
