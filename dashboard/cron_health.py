"""
Cron health panel — last N runs of each scheduled job, status, duration.

Reads the optimization_log CRON_RUN rows produced by klh_cron.sh, plus
flags any run >= 25h ago as stale (cron should fire hourly).

Routes:
    GET  /cron-health             → static page shell
    GET  /api/cron-health/runs    → JSON of last 24 runs + degraded flag
"""
from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from pipeline import audit_db


STATIC_DIR = Path(__file__).resolve().parent / "static"

# Per-job pattern in the CRON_RUN details string. klh_cron.sh writes:
#   send_tier_offers=SUCCESS/142s daily_ad_housekeeping=SUCCESS/8s
_JOB_RE = re.compile(r"(?P<name>[a-z_]+)=(?P<status>SUCCESS|FAILED\([-\d]+\))/(?P<duration>\d+)s")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime:
    """Cron rows are stored as YYYY-MM-DDTHH:MM:SSZ."""
    return datetime.strptime(s.rstrip("Z"), "%Y-%m-%dT%H:%M:%S").replace(
        tzinfo=timezone.utc
    )


def register_cron_health_routes(app: FastAPI) -> None:

    @app.get("/cron-health", include_in_schema=False)
    def cron_health_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "cron_health.html")

    @app.get("/api/cron-health/runs")
    def cron_health_runs() -> JSONResponse:
        with audit_db.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT event_at, details FROM optimization_log "
                "WHERE event = 'CRON_RUN' "
                "ORDER BY event_at DESC LIMIT 24"
            ).fetchall()

        runs = []
        for r in rows:
            jobs = []
            for m in _JOB_RE.finditer(r["details"] or ""):
                jobs.append({
                    "name":     m.group("name"),
                    "status":   m.group("status"),
                    "ok":       m.group("status") == "SUCCESS",
                    "duration": int(m.group("duration")),
                })
            runs.append({
                "at":   r["event_at"],
                "jobs": jobs,
                "ok":   all(j["ok"] for j in jobs) if jobs else False,
            })

        # Degraded if last run > 90 min old (cron is hourly + slack)
        # or if last 2 runs both had a failed job
        now = datetime.now(timezone.utc)
        last_seen_at = _parse_iso(rows[0]["event_at"]) if rows else None
        stale_minutes = int((now - last_seen_at).total_seconds() / 60) if last_seen_at else None

        degraded = False
        degraded_reason = None
        if not rows:
            degraded = True
            degraded_reason = "no cron runs ever recorded"
        elif stale_minutes is not None and stale_minutes > 90:
            degraded = True
            degraded_reason = f"last run {stale_minutes} min ago (expected ≤60)"
        elif len(runs) >= 2 and not runs[0]["ok"] and not runs[1]["ok"]:
            degraded = True
            degraded_reason = "last 2 runs both had failed jobs"

        # Aggregate per-job pass-rate over last 24 runs
        by_job: dict[str, dict] = {}
        for run in runs:
            for j in run["jobs"]:
                slot = by_job.setdefault(j["name"], {"name": j["name"],
                                                     "ok_runs": 0, "total_runs": 0,
                                                     "avg_duration_s": 0})
                slot["total_runs"] += 1
                if j["ok"]:
                    slot["ok_runs"] += 1
                slot["avg_duration_s"] += j["duration"]
        for slot in by_job.values():
            if slot["total_runs"]:
                slot["avg_duration_s"] = round(
                    slot["avg_duration_s"] / slot["total_runs"]
                )
                slot["pass_pct"] = round(
                    slot["ok_runs"] / slot["total_runs"] * 100
                )
            else:
                slot["pass_pct"] = 0

        return JSONResponse({
            "runs":              runs,
            "by_job":            list(by_job.values()),
            "degraded":          degraded,
            "degraded_reason":   degraded_reason,
            "stale_minutes":     stale_minutes,
            "now":               _now_iso(),
        })
