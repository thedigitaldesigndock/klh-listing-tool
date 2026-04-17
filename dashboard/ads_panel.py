"""
Ad-automation panel for the KLH dashboard.

Surfaces current tier distribution + recent housekeeping history so
Peter can see at a glance:

  * How many listings are in each tier campaign
  * When the daily housekeeping last ran + what it changed
  * Next steps (listings with no price, listings awaiting audit fetch)

Routes:
    GET  /ads                         → static page shell
    GET  /api/ads/summary             → tier counts + latest log events
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from pipeline import audit_db


STATIC_DIR = Path(__file__).resolve().parent / "static"


# Keep in sync with scripts/daily_ad_housekeeping.TIER_CAMPAIGNS.
# (If we rebuild campaigns, both this and the script need updating —
# the campaign IDs are the only live-eBay reference we hold.)
TIER_DEFS: list[dict[str, Any]] = [
    {"name": "BUDGET",       "rate": 5.0,  "lo": 10.0, "hi": 15.0,
     "cid": "162557282013"},
    {"name": "STANDARD",     "rate": 8.2,  "lo": 15.0, "hi": 30.0,
     "cid": "162557283013"},
    {"name": "PREMIUM",      "rate": 10.0, "lo": 30.0, "hi": 50.0,
     "cid": "162557285013"},
    {"name": "PREMIUM_PLUS", "rate": 12.0, "lo": 50.0, "hi": 1e12,
     "cid": "162557288013"},
]


def _tier_for_price(price, tiers=TIER_DEFS):
    if price is None or price < 10:
        return None
    for t in tiers:
        if t["lo"] <= price < t["hi"]:
            return t["name"]
    return None


def register_ads_routes(app: FastAPI) -> None:

    @app.get("/ads", include_in_schema=False)
    def ads_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "ads.html")

    @app.get("/api/ads/summary")
    def ads_summary() -> JSONResponse:
        """Tier distribution from the local audit DB + recent housekeeping
        events from the optimization_log."""
        tiers = {t["name"]: 0 for t in TIER_DEFS}
        tiers["EXCLUDED"] = 0
        tiers["NO_PRICE"] = 0

        with audit_db.connect(readonly=True) as conn:
            for r in conn.execute(
                "SELECT price_gbp FROM listings WHERE listing_type IS NOT NULL"
            ):
                p = r["price_gbp"]
                if p is None:
                    tiers["NO_PRICE"] += 1
                elif p < 10:
                    tiers["EXCLUDED"] += 1
                else:
                    t = _tier_for_price(p)
                    if t:
                        tiers[t] += 1
                    else:
                        tiers["NO_PRICE"] += 1

            # Recent housekeeping / campaign-build events
            events = []
            for r in conn.execute(
                "SELECT event, event_at, details FROM optimization_log "
                "WHERE event IN ('AD_HOUSEKEEPING', 'TIER_CAMPAIGNS_BUILT', "
                "'BR_RECAT_DONE', 'BR_IS_DONE', 'SIGNER_IS_DONE') "
                "ORDER BY event_at DESC LIMIT 20"
            ):
                events.append({
                    "event":    r["event"],
                    "event_at": r["event_at"],
                    "details":  r["details"],
                })

        return JSONResponse({
            "tiers":      tiers,
            "tier_defs":  [{k: v for k, v in t.items() if k != "cid"} for t in TIER_DEFS],
            "events":     events,
        })
