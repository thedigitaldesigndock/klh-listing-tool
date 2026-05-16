#!/usr/bin/env python3
"""
Pull performance reports for KLH's eBay ad campaigns.

Covers:
  * 4 tiered PLS campaigns (BUDGET/STANDARD/PREMIUM/PREMIUM_PLUS)
  * 1 PLA test campaign (25 keywords, £5/day CPC)

eBay's Marketing API report flow is asynchronous:
  1. POST /ad_report_task       → submit a report job
  2. GET  /ad_report_task/{id}  → poll until reportStatus=SUCCESS
  3. GET  /ad_report/{id}       → download the CSV

We create separate tasks for PLS (COST_PER_SALE) and PLA (COST_PER_CLICK)
because eBay requires reports to be single-funding-model.

Results land in:
  * stdout summary (per-campaign 7-day and 30-day rows)
  * optimization_log event AD_REPORT_PULLED
  * ad_reports table in audit.db (history so we can trend over time)

Usage:
    python scripts/pull_ad_reports.py                # 7-day + 30-day
    python scripts/pull_ad_reports.py --days 90      # custom window
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from pipeline import audit_db
from ebay_api import token_manager
from ebay_api.marketing import _request, BASE, MarketingError


# --------------------------------------------------------------------------
# Campaign IDs (kept in sync with dashboard/ads_panel.py and
# scripts/build_pla_test.py).
# --------------------------------------------------------------------------
PLS_CAMPAIGNS: list[dict] = [
    {"name": "BUDGET",       "id": "162557282013", "funding": "COST_PER_SALE"},
    {"name": "STANDARD",     "id": "162557283013", "funding": "COST_PER_SALE"},
    {"name": "PREMIUM",      "id": "162557285013", "funding": "COST_PER_SALE"},
    {"name": "PREMIUM_PLUS", "id": "162557288013", "funding": "COST_PER_SALE"},
]

PLA_CAMPAIGNS: list[dict] = [
    {"name": "PLA_TEST_25KW",  "id": "162564894013", "funding": "COST_PER_CLICK"},
]

# Columns we ask eBay for. Report column names vary by funding model — these
# are the common ones both flows return.
# Metric names per eBay's ad_report_metadata endpoint — lowercase.
# PLS uses cost-per-sale metrics; PLA uses the cpc_* namespace.
PLS_METRICS = [
    "impressions", "clicks", "ad_fees", "sales", "sale_amount",
    "ctr", "avg_cost_per_sale",
]
PLA_METRICS = [
    "cpc_impressions", "cpc_clicks", "cpc_ad_fees_listingsite_currency",
    "cpc_attributed_sales", "cpc_sale_amount_listingsite_currency",
    "cpc_ctr", "cost_per_click", "cpc_conversion_rate",
    "cpc_return_on_ad_spend",
]


# --------------------------------------------------------------------------
# Cache table
# --------------------------------------------------------------------------
def _ensure_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ad_reports (
            pulled_at    TEXT NOT NULL,
            window_start TEXT NOT NULL,
            window_end   TEXT NOT NULL,
            campaign_id  TEXT NOT NULL,
            campaign     TEXT NOT NULL,
            funding      TEXT NOT NULL,
            impressions  INTEGER,
            clicks       INTEGER,
            ad_fees_gbp  REAL,
            sale_amount_gbp REAL,
            qty_sold     INTEGER,
            ctr_pct      REAL,
            cpc_gbp      REAL,
            acos_pct     REAL,
            roas         REAL,
            raw_json     TEXT,
            PRIMARY KEY (pulled_at, window_start, window_end, campaign_id)
        )
    """)
    conn.commit()


# --------------------------------------------------------------------------
# Report lifecycle
# --------------------------------------------------------------------------
def _create_report_task(
    campaign_ids: list[str],
    start_date: str,
    end_date: str,
    metrics: list[str],
    funding_model: str,
) -> str:
    """Submit a CAMPAIGN_PERFORMANCE_REPORT task. Returns task_id."""
    # eBay wants flat dateFrom/dateTo as ISO YYYY-MM-DD, lowercase metric
    # keys, and a single dimensionKey — use campaign_id to get one row per
    # campaign. CAMPAIGN_PERFORMANCE_SUMMARY_REPORT rolls up the full
    # window (no per-day breakdown, which is what we want).
    body = {
        "campaignIds":    campaign_ids,
        "reportType":     "CAMPAIGN_PERFORMANCE_SUMMARY_REPORT",
        "reportFormat":   "TSV_GZIP",
        "dateFrom":       start_date,
        "dateTo":         end_date,
        # PLS min = [day, campaign_id], PLA min = [ad_group_id, day, campaign_id].
        # We aggregate per campaign below.
        "dimensions": (
            [{"dimensionKey": "ad_group_id"},
             {"dimensionKey": "day"},
             {"dimensionKey": "campaign_id"}]
            if funding_model == "COST_PER_CLICK"
            else [{"dimensionKey": "day"},
                  {"dimensionKey": "campaign_id"}]
        ),
        "metricKeys":     metrics,
        "fundingModels":  [funding_model],
        "marketplaceIds": ["EBAY_GB"],
    }
    status, resp, headers = _request(
        "POST", f"{BASE}/ad_report_task", body=body,
    )
    loc = headers.get("Location") or headers.get("location") or ""
    task_id = loc.rsplit("/", 1)[-1] if loc else (resp or {}).get("reportTaskId")
    if not task_id:
        raise MarketingError(f"ad_report_task: no id in {headers} / {resp}")
    return task_id


def _poll_task(task_id: str, timeout_s: int = 180) -> dict:
    """Poll until reportStatus is SUCCESS/FAILED/EXPIRED, then return body."""
    deadline = time.monotonic() + timeout_s
    while True:
        _, body, _ = _request(
            "GET", f"{BASE}/ad_report_task/{task_id}",
        )
        status = (body or {}).get("reportTaskStatus")
        if status in ("SUCCESS", "FAILED", "EXPIRED"):
            return body or {}
        if time.monotonic() > deadline:
            raise MarketingError(
                f"ad_report_task/{task_id} still {status} after {timeout_s}s"
            )
        time.sleep(3)


def _download_report(report_id: str) -> str:
    """Download the CSV body. The ad_report endpoint returns a redirect to
    a signed URL for the CSV; we follow it."""
    url = f"{BASE}/ad_report/{report_id}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization":          f"Bearer {token_manager.get_access_token()}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB",
            "Accept":                  "text/csv,application/zip",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read()
    # Decompress gzip → TSV text
    try:
        return gzip.decompress(raw).decode(errors="replace")
    except OSError:
        # Not actually gzipped (edge case) — return as-is.
        return raw.decode(errors="replace")


def _parse_csv(text: str) -> list[dict]:
    """Parse the TSV eBay returns (format is TSV despite our 'csv' helper
    name). First row is column headers."""
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    return [row for row in reader]


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def _fnum(s) -> float:
    try:
        return float(str(s).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return 0.0


def _fint(s) -> int:
    try:
        return int(_fnum(s))
    except (TypeError, ValueError):
        return 0


def _pull(campaigns: list[dict], start: str, end: str, metrics: list[str]) -> list[dict]:
    """Run one report for a list of same-funding-model campaigns.
    Returns list of dicts keyed by campaign_id."""
    if not campaigns:
        return []
    funding = campaigns[0]["funding"]
    ids = [c["id"] for c in campaigns]
    print(f"  Submitting report ({funding})  ids={ids}  {start} → {end}")
    task_id = _create_report_task(ids, start, end, metrics, funding)
    print(f"    task_id={task_id}  polling…")
    task = _poll_task(task_id)
    if task.get("reportTaskStatus") != "SUCCESS":
        raise MarketingError(f"report task failed: {task}")
    report_id = task.get("reportId")
    if not report_id:
        raise MarketingError(f"no reportId in task body: {task}")
    csv_text = _download_report(report_id)
    rows = _parse_csv(csv_text)

    # Rows come per-day (and per-ad-group for CPC). Aggregate to one row
    # per campaign by summing the numeric cols.
    from collections import defaultdict
    id_to_meta = {c["id"]: c for c in campaigns}
    is_cpc = funding == "COST_PER_CLICK"
    agg: dict[str, dict[str, float]] = defaultdict(
        lambda: {"impressions": 0, "clicks": 0, "ad_fees": 0.0,
                 "sale_amt": 0.0, "qty_sold": 0}
    )
    for r in rows:
        cid = (r.get("campaign_id") or "").strip()
        if cid not in id_to_meta:
            continue
        if is_cpc:
            agg[cid]["impressions"] += _fint(r.get("cpc_impressions"))
            agg[cid]["clicks"]      += _fint(r.get("cpc_clicks"))
            agg[cid]["ad_fees"]     += _fnum(r.get("cpc_ad_fees_listingsite_currency"))
            agg[cid]["sale_amt"]    += _fnum(r.get("cpc_sale_amount_listingsite_currency"))
            agg[cid]["qty_sold"]    += _fint(r.get("cpc_attributed_sales"))
        else:
            agg[cid]["impressions"] += _fint(r.get("impressions"))
            agg[cid]["clicks"]      += _fint(r.get("clicks"))
            agg[cid]["ad_fees"]     += _fnum(r.get("ad_fees"))
            agg[cid]["sale_amt"]    += _fnum(r.get("sale_amount"))
            agg[cid]["qty_sold"]    += _fint(r.get("sales"))

    out = []
    for cid, meta in id_to_meta.items():
        a = agg[cid]
        impressions, clicks, ad_fees, sale_amt, qty_sold = (
            int(a["impressions"]), int(a["clicks"]),
            a["ad_fees"], a["sale_amt"], int(a["qty_sold"]),
        )
        ctr_pct  = (clicks / impressions * 100) if impressions else 0.0
        cpc      = (ad_fees / clicks) if clicks else 0.0
        acos_pct = (ad_fees / sale_amt * 100) if sale_amt else 0.0
        roas     = (sale_amt / ad_fees) if ad_fees else 0.0
        out.append({
            "campaign_id":     cid,
            "campaign":        meta["name"],
            "funding":         funding,
            "impressions":     impressions,
            "clicks":          clicks,
            "ad_fees_gbp":     round(ad_fees, 2),
            "sale_amount_gbp": round(sale_amt, 2),
            "qty_sold":        qty_sold,
            "ctr_pct":         round(ctr_pct, 3),
            "cpc_gbp":         round(cpc, 3),
            "acos_pct":        round(acos_pct, 2),
            "roas":            round(roas, 2),
            "raw":             dict(a),
        })
    return out


def _print_table(rows: list[dict], title: str) -> None:
    print(f"\n=== {title} ===")
    print(f"{'Campaign':<14} {'Imp':>7} {'Clk':>5} {'CTR%':>6} "
          f"{'CPC':>6} {'AdFees£':>8} {'Sales£':>9} {'Qty':>4} "
          f"{'ACOS%':>6} {'ROAS':>5}")
    print("-" * 82)
    for r in rows:
        print(f"{r['campaign']:<14} "
              f"{r['impressions']:>7} {r['clicks']:>5} {r['ctr_pct']:>6.2f} "
              f"{r['cpc_gbp']:>6.2f} {r['ad_fees_gbp']:>8.2f} "
              f"{r['sale_amount_gbp']:>9.2f} {r['qty_sold']:>4} "
              f"{r['acos_pct']:>6.2f} {r['roas']:>5.2f}")


def _save(rows: list[dict], pulled_at: str, start: str, end: str) -> None:
    with audit_db.connect() as conn:
        _ensure_table(conn)
        for r in rows:
            conn.execute(
                "INSERT OR REPLACE INTO ad_reports ("
                "pulled_at, window_start, window_end, campaign_id, campaign, "
                "funding, impressions, clicks, ad_fees_gbp, sale_amount_gbp, "
                "qty_sold, ctr_pct, cpc_gbp, acos_pct, roas, raw_json"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pulled_at, start, end, r["campaign_id"], r["campaign"],
                 r["funding"], r["impressions"], r["clicks"],
                 r["ad_fees_gbp"], r["sale_amount_gbp"], r["qty_sold"],
                 r["ctr_pct"], r["cpc_gbp"], r["acos_pct"], r["roas"],
                 json.dumps(r["raw"])),
            )
        conn.execute(
            "INSERT INTO optimization_log (event, event_at, details) "
            "VALUES (?, ?, ?)",
            ("AD_REPORT_PULLED", pulled_at,
             f"Pulled {len(rows)} campaign rows for {start}..{end}"),
        )
        conn.commit()


def _window(days: int) -> tuple[str, str]:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=0,
                   help="Single window in days (default: pull both 7d and 30d).")
    args = p.parse_args()

    windows = [(args.days, f"Last {args.days}d")] if args.days else [
        (7,  "Last 7 days"),
        (30, "Last 30 days"),
    ]

    pulled_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    all_rows: list[dict] = []
    for days, label in windows:
        start, end = _window(days)
        print(f"\n### {label}  ({start} → {end}) ###")
        try:
            pls = _pull(PLS_CAMPAIGNS, start, end, PLS_METRICS)
            _print_table(pls, f"{label} — PLS tier campaigns")
            _save(pls, pulled_at, start, end)
            all_rows.extend(pls)
        except MarketingError as e:
            print(f"  PLS pull failed: {e}")
        try:
            pla = _pull(PLA_CAMPAIGNS, start, end, PLA_METRICS)
            _print_table(pla, f"{label} — PLA CPC campaigns")
            _save(pla, pulled_at, start, end)
            all_rows.extend(pla)
        except MarketingError as e:
            print(f"  PLA pull failed: {e}")

    print(f"\nDone. {len(all_rows)} rows written to ad_reports table.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
