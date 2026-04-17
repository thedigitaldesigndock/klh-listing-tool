#!/usr/bin/env python3
"""
Apply a batch of manual Team corrections exported from the Team Review
dashboard.

The dashboard's "Show my choices" modal produces a block like:

    256922925143  →  Manchester United     [was: England]  "old title…"
    257050184284  →  Millwall               [was: (none)]  "old title…"
    ...

Save that block to a text file and point this script at it. For each
line we:

  1. Update Team IS to the chosen team (or clear it if team == CLEAR)
  2. If the old team appears verbatim in the current title AND the
     listing isn't a multipack, replace it with the new team
     (falling back to short form if long form overflows 80 chars).
     For the (none) → X case, no title replacement is attempted —
     title stays as-is, only Team IS gets set.
  3. Call ReviseFixedPriceItem once per listing (single round-trip).
  4. Update the local audit cache so re-runs don't redo the same work.

Usage:
    python scripts/apply_team_corrections.py /path/to/corrections.txt
    python scripts/apply_team_corrections.py /path/to/corrections.txt --dry-run
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Optional

from pipeline import audit_db, lister, presets as pp


MAX_TITLE_LEN = 80

# Example lines:
#   257050184284  →  Millwall     [was: (none)]  "..."
#   257198932462  →  Millwall     [was: Manchester United]  "..."
#   257199400136  →  CLEAR Team IS  [was: Manchester United]  "..."
LINE_RE = re.compile(
    r"^\s*(?P<item_id>\d+)\s*[→>]\s*"
    r"(?P<choice>CLEAR Team IS|[^\[]+?)\s*"
    r"\[was:\s*(?P<was>[^\]]+?)\s*\]"
    r"\s*\"(?P<title>.*)\"\s*$"
)

MULTIPACK_RE = re.compile(r"^\s*(\d+\s*x\b|lot\s+of\b|joblot\b)", re.I)


def _parse(path: Path) -> list[dict]:
    out = []
    for raw in path.read_text().splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        m = LINE_RE.match(raw)
        if not m:
            print(f"  skipped unparseable: {raw[:90]}")
            continue
        choice = m.group("choice").strip()
        was = m.group("was").strip()
        out.append({
            "item_id": m.group("item_id"),
            "clear":   choice == "CLEAR Team IS",
            "new_team": None if choice == "CLEAR Team IS" else choice,
            "old_team": None if was.lower() in ("(none)", "none", "") else was,
            "orig_title_preview": m.group("title"),
        })
    return out


def _rewrite_title(
    bundle: pp.PresetsBundle,
    current_title: str,
    old_team: Optional[str],
    new_team: Optional[str],
) -> Optional[str]:
    """Return new title string, or None to leave title alone.

    Policy: replace old_team in-place with new_team. Try long form
    first; if over 80 chars, fall back to the short form via
    shrink_club; if still over, skip the title rewrite.
    """
    if not new_team or not old_team:
        return None
    if MULTIPACK_RE.match(current_title or ""):
        return None
    if old_team not in (current_title or ""):
        return None
    candidate = current_title.replace(old_team, new_team)
    if len(candidate) <= MAX_TITLE_LEN:
        return candidate
    short = bundle.shrink_club(new_team) or new_team
    if short != new_team:
        candidate = current_title.replace(old_team, short)
        if len(candidate) <= MAX_TITLE_LEN:
            return candidate
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", type=Path, help="corrections text file")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rate", type=float, default=1.0,
                    help="API calls per second (default 1.0)")
    args = ap.parse_args()

    if not args.path.exists():
        print(f"File not found: {args.path}"); return 1

    entries = _parse(args.path)
    if not entries:
        print("No parseable entries."); return 1
    print(f"Parsed {len(entries)} corrections\n")

    bundle = pp.load()
    sleep = 1.0 / max(args.rate, 0.1)

    with audit_db.connect() as conn:
        plan: list[dict] = []
        for e in entries:
            row = conn.execute(
                "SELECT title, specifics_json FROM listings WHERE item_id = ?",
                (e["item_id"],)
            ).fetchone()
            if not row:
                print(f"  ! {e['item_id']} not in cache — skipping")
                continue
            current_specs = json.loads(row["specifics_json"]) if row["specifics_json"] else {}
            new_specs = dict(current_specs)
            if e["clear"]:
                new_specs.pop("Team", None)
            else:
                new_specs["Team"] = e["new_team"]

            new_title = _rewrite_title(
                bundle, row["title"] or "", e["old_team"], e["new_team"],
            )

            plan.append({
                "item_id":   e["item_id"],
                "old_title": row["title"],
                "new_title": new_title,
                "old_specs": current_specs,
                "new_specs": new_specs,
                "new_team":  None if e["clear"] else e["new_team"],
                "cleared":   e["clear"],
            })

        print("=== Plan ===")
        for p in plan:
            print(f"\n[{p['item_id']}]")
            print(f"  Team IS: {p['old_specs'].get('Team','(none)')}"
                  f"  →  {'CLEAR' if p['cleared'] else p['new_team']}")
            if p["new_title"]:
                print(f"  Title  : {p['old_title']}")
                print(f"       →   {p['new_title']}  ({len(p['new_title'])}/80)")
            else:
                print(f"  Title  : (unchanged) {p['old_title']}")

        print(f"\n{len(plan)} listings to revise.\n")
        if args.dry_run:
            print("[DRY RUN] pass without --dry-run to apply live.")
            return 0

        print(f"Applying (rate={args.rate}/s, ETA ~{len(plan) * sleep / 60:.1f}m)\n")
        ok = fail = 0
        start = time.monotonic()
        for i, p in enumerate(plan, 1):
            try:
                kwargs = {
                    "confirm": True,
                    "new_specifics_replace": p["new_specs"],
                }
                if p["new_title"]:
                    kwargs["new_title"] = p["new_title"]
                result = lister.revise_listing(p["item_id"], **kwargs)
                ack = result.get("ack")
                if ack in ("Success", "Warning"):
                    ok += 1
                    conn.execute(
                        "UPDATE listings SET specifics_json = ?, "
                        "title = COALESCE(?, title) WHERE item_id = ?",
                        (json.dumps(p["new_specs"]), p["new_title"], p["item_id"]),
                    )
                else:
                    fail += 1
                    warnings = result.get("warnings") or []
                    msgs = "; ".join(w.get("long", "") for w in warnings if w.get("long"))
                    print(f"  ✗ [{p['item_id']}] ack={ack}  {msgs}")
            except Exception as exc:
                fail += 1
                print(f"  ✗ [{p['item_id']}] EXCEPTION: {exc}")
            if i % 10 == 0:
                conn.commit()
                print(f"  {i}/{len(plan)} (ok={ok} fail={fail})")
            time.sleep(sleep)

        conn.commit()
        conn.execute(
            "INSERT INTO optimization_log (event, event_at, details) VALUES (?, ?, ?)",
            ("TEAM_CORRECTIONS",
             time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             f"Batch team corrections from dashboard review: ok={ok} fail={fail}"),
        )
        conn.commit()
        print(f"\nDone: {ok} ok, {fail} failed in {time.monotonic()-start:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
