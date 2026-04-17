"""
Team Review panel for the KLH dashboard.

Human-in-the-loop review of auto-derived Team IS tags. The automation
can't see the photo — so for ambiguous listings (multi-team titles or
legacy stuffed titles), we surface them here with a thumbnail +
dropdown and let Peter pick the correct team. One click per listing
applies the fix via ReviseFixedPriceItem.

Routes registered on the main app:

    GET  /team-review                    → static shell (team_review.html)
    GET  /api/team-review/signers        → list of signers with a listing count
    GET  /api/team-review/{signer}       → per-listing review bundle
    POST /api/team-review/{item_id}      → apply a team choice

The signer filter is a prefix-match on title — same convention we've
been using for the IS apply scripts.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from pipeline import audit_db, lister, presets as pp


MAX_TITLE_LEN = 80  # eBay hard cap


STATIC_DIR = Path(__file__).resolve().parent / "static"


class TeamReviewChoice(BaseModel):
    team: Optional[str] = None        # None means "leave Team IS alone"
    clear: bool = False                # True → delete Team IS entirely


def _signer_filter(signer: str) -> str:
    return f"%{signer.strip().lower()}%"


def register_team_review_routes(app: FastAPI) -> None:

    @app.get("/team-review", include_in_schema=False)
    def team_review_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "team_review.html")

    @app.get("/api/team-review/signers")
    def team_review_signers() -> JSONResponse:
        """Return signers that have deep-fetched listings with picture URLs,
        ranked by count. Heuristic: first two capitalised words of title."""
        import re
        name_re = re.compile(r"^([A-Z][a-z]+ [A-Z][a-z\-']+)")
        skip = {"Signed", "Original", "Genuine", "Rare", "Vintage",
                "Hand", "Mystery", "Limited", "Framed", "Authentic",
                "The"}
        counts: Counter = Counter()
        with audit_db.connect(readonly=True) as conn:
            for r in conn.execute(
                "SELECT title FROM listings WHERE picture_url IS NOT NULL "
                "AND picture_url != ''"
            ):
                title = r["title"] or ""
                m = name_re.match(title)
                if not m:
                    continue
                name = m.group(1)
                if name.split()[0] in skip:
                    continue
                counts[name] += 1
        return JSONResponse({
            "signers": [{"name": n, "count": c}
                        for n, c in counts.most_common(50)],
        })

    @app.get("/api/team-review/{signer}")
    def team_review_signer(signer: str,
                           team_filter: Optional[str] = None) -> JSONResponse:
        """Return listings for a signer for review.

        If team_filter is passed, only return listings whose current Team
        IS matches that filter (e.g. audit 'only show England-tagged').
        """
        with audit_db.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT item_id, title, specifics_json, picture_url, "
                "view_item_url, price_gbp, watch_count "
                "FROM listings WHERE LOWER(title) LIKE ? "
                "AND picture_url IS NOT NULL AND picture_url != '' "
                "ORDER BY item_id",
                (_signer_filter(signer),),
            ).fetchall()

        # Team candidates drawn from the existing Team IS values seen on
        # this signer's listings + common nations, so the dropdown offers
        # relevant options first.
        team_seen: Counter = Counter()
        listings = []
        for r in rows:
            s = json.loads(r["specifics_json"]) if r["specifics_json"] else {}
            team = s.get("Team")
            if team:
                team_seen[team] += 1
            listings.append({
                "item_id":     r["item_id"],
                "title":       r["title"],
                "team_is":     team,
                "picture_url": r["picture_url"],
                "ebay_url":    r["view_item_url"],
                "price_gbp":   r["price_gbp"],
                "watch_count": r["watch_count"] or 0,
            })

        if team_filter:
            listings = [L for L in listings if L["team_is"] == team_filter]

        candidates = [t for t, _ in team_seen.most_common()] + [
            "England", "Scotland", "Wales", "Northern Ireland",
            "Manchester United", "Tottenham Hotspur", "Manchester City",
            "Nottingham Forest", "West Ham United", "West Bromwich Albion",
            "Middlesbrough", "Portsmouth",
        ]
        # Preserve order, de-dupe
        seen = set()
        deduped = []
        for c in candidates:
            if c and c not in seen:
                seen.add(c)
                deduped.append(c)

        return JSONResponse({
            "signer": signer,
            "total":  len(listings),
            "team_candidates": deduped,
            "team_breakdown": dict(team_seen),
            "listings": listings,
        })

    @app.post("/api/team-review/{item_id}")
    def team_review_apply(item_id: str, choice: TeamReviewChoice) -> JSONResponse:
        """Apply a reviewer's team choice to a single listing.

        Policy:
          * team=None + clear=False   → no-op (explicit skip)
          * team=None + clear=True    → remove Team IS from the listing
          * team=<str>                → set/replace Team IS, also rewrite
                                        any occurrence of the OLD team in
                                        the title with the new short form
                                        (if an alias exists).
        """
        with audit_db.connect() as conn:
            row = conn.execute(
                "SELECT title, specifics_json FROM listings WHERE item_id = ?",
                (item_id,)
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="item not in audit cache")
            current = json.loads(row["specifics_json"]) if row["specifics_json"] else {}
            old_team = current.get("Team")

            # Build new specifics (we always replace the whole block on revise
            # so we have to merge in the edit and keep everything else).
            new_specs = dict(current)
            if choice.team:
                new_specs["Team"] = choice.team
            elif choice.clear:
                new_specs.pop("Team", None)
            else:
                return JSONResponse({"item_id": item_id, "status": "no-op"})

            # Title rewrite: only if we're CHANGING to a known new team
            # AND the old team appears verbatim in current title. Try the
            # long form first; if that overflows 80 chars, fall back to
            # the short form via shrink_club; if that still overflows,
            # leave the title alone (Team IS is still updated).
            new_title: Optional[str] = None
            if choice.team and old_team and old_team != choice.team:
                cur_title = row["title"] or ""
                if old_team in cur_title:
                    bundle = pp.load()
                    candidate = cur_title.replace(old_team, choice.team)
                    if len(candidate) <= MAX_TITLE_LEN:
                        new_title = candidate
                    else:
                        # Try the short form of the chosen team.
                        short = bundle.shrink_club(choice.team) or choice.team
                        if short != choice.team:
                            candidate = cur_title.replace(old_team, short)
                            if len(candidate) <= MAX_TITLE_LEN:
                                new_title = candidate
                    # If still no fit, new_title stays None and we update
                    # Team IS only. The old team string stays in the title
                    # as a legacy artifact — user can edit on eBay if they
                    # care. This is better than the whole revise failing.

            try:
                kwargs: dict = {"confirm": True, "new_specifics_replace": new_specs}
                if new_title:
                    kwargs["new_title"] = new_title
                result = lister.revise_listing(item_id, **kwargs)
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"revise failed: {e}")

            ack = result.get("ack")
            if ack not in ("Success", "Warning"):
                warnings = result.get("warnings") or []
                msgs = "; ".join(w.get("long", "") for w in warnings if w.get("long"))
                raise HTTPException(
                    status_code=500,
                    detail=f"eBay ack={ack}  {msgs}"
                )

            # Update cache so re-opening the review doesn't re-show this.
            conn.execute(
                "UPDATE listings SET specifics_json = ?, title = COALESCE(?, title) "
                "WHERE item_id = ?",
                (json.dumps(new_specs), new_title, item_id),
            )
            conn.commit()

        return JSONResponse({
            "item_id":   item_id,
            "status":    "ok",
            "new_team":  new_specs.get("Team"),
            "new_title": new_title,
        })
