"""
Self-populating backlog of improvement ideas for the KLH catalogue.

Design
------
Any audit/listing script can call `note()` when it spots something worth
tracking (missing alias, candidate for recategorisation, title cleanup
opportunity, etc.). Repeated observations of the same `(topic, key)`
pair just increment the `count` column so the signal doesn't dilute.

Human-raised items work the same way — call `note()` or use
`klh backlog add` for a one-off entry.

Topics used so far
------------------
    alias_discovery      — candidate short ↔ long form pair to add to
                           knowledge.yaml aliases
    title_cleanup        — specific title issues flagged by audit rules
    category_fix         — miscategorised-listing candidates
    deadwood             — stale listings that may want retirement
    is_rollout           — signers still needing the canonical IS pass
    ads                  — ad-tier / campaign work to plan
    roadmap              — human-tracked bigger pieces of work
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Optional


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def note(
    conn: sqlite3.Connection,
    *,
    topic: str,
    key: str,
    title: str,
    details: Optional[str] = None,
    source: Optional[str] = None,
) -> None:
    """Upsert a backlog row. Repeat observations bump `count`.

    Args:
        topic:   short category of suggestion (e.g. 'alias_discovery')
        key:     stable identifier within `topic` used for de-dup
                 (e.g. 'Football:Manchester United')
        title:   human-readable one-line summary
        details: optional JSON blob / free text
        source:  origin ('add_br_item_specifics.py' / 'human' / etc.)
    """
    now = _now()
    conn.execute(
        """
        INSERT INTO backlog
            (topic, key, title, details, source, status,
             count, first_seen_at, last_seen_at)
        VALUES
            (:topic, :key, :title, :details, :source, 'open',
             1, :now, :now)
        ON CONFLICT (topic, key) DO UPDATE SET
            count         = backlog.count + 1,
            last_seen_at  = excluded.last_seen_at,
            title         = excluded.title,
            details       = COALESCE(excluded.details, backlog.details),
            source        = COALESCE(excluded.source, backlog.source)
        """,
        {
            "topic":   topic,
            "key":     key,
            "title":   title,
            "details": details,
            "source":  source,
            "now":     now,
        },
    )


def list_open(
    conn: sqlite3.Connection,
    *,
    topic: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Return open backlog rows (optionally filtered by topic), newest count first."""
    sql = "SELECT * FROM backlog WHERE status = 'open'"
    params: list[Any] = []
    if topic:
        sql += " AND topic = ?"
        params.append(topic)
    sql += " ORDER BY count DESC, last_seen_at DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params)]


def resolve(conn: sqlite3.Connection, backlog_id: int) -> int:
    """Mark a backlog row as resolved. Returns rows affected (0 or 1)."""
    cur = conn.execute(
        "UPDATE backlog SET status = 'resolved', resolved_at = ? "
        "WHERE id = ? AND status = 'open'",
        (_now(), backlog_id),
    )
    return cur.rowcount


def ignore(conn: sqlite3.Connection, backlog_id: int) -> int:
    """Mark a backlog row as ignored (won't show up in open lists)."""
    cur = conn.execute(
        "UPDATE backlog SET status = 'ignored', resolved_at = ? "
        "WHERE id = ? AND status = 'open'",
        (_now(), backlog_id),
    )
    return cur.rowcount


def stats(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """Return counts by topic × status for a quick overview."""
    out: dict[str, dict[str, int]] = {}
    for r in conn.execute(
        "SELECT topic, status, COUNT(*) AS n FROM backlog "
        "GROUP BY topic, status ORDER BY topic, status"
    ):
        out.setdefault(r["topic"], {})[r["status"]] = int(r["n"])
    return out


# --------------------------------------------------------------------------- #
# Seed helpers
# --------------------------------------------------------------------------- #

def seed_initial_roadmap(conn: sqlite3.Connection) -> int:
    """Insert the starting roadmap entries known at project-start time.

    Idempotent — uses topic+key de-dup, so re-running this is safe.
    Returns count of rows INSERTED (not bumped).
    """
    entries: list[tuple[str, str, str, Optional[str]]] = [
        # (topic, key, title, details)
        ("roadmap", "per_category_aliases",
         "Generalise club-alias reverse-lookup to TV/Film/Music/F1/NFL/NBA",
         "Today shrink_club only covers football. Extend to per-category "
         "alias tables (aliases.TV: OFAH → Only Fools and Horses, aliases.Film: "
         "LOTR → Lord of the Rings, etc.). Seed from mining Field1 values in "
         "current catalogue. See commit 8039cde for the Football-only version."),

        ("roadmap", "is_rollout_top_50_signers",
         "Roll the BR IS template to top 50 signers",
         "~3,500 listings. Reuse add_br_item_specifics.py generalised. "
         "Priority order from signer-volume query: Jeremy Clarkson, "
         "Teddy Sheringham, Dan Castellaneta, Sir David Attenborough, "
         "Dan Aykroyd, Shaquille O'Neal, Joanna Lumley, Mario Andretti, "
         "Jackie Chan, …"),

        ("roadmap", "miscat_scan_catalogue",
         "Catalogue-wide miscategorisation scan",
         "Find every football-named listing parked in non-football cat "
         "(like the 38 BR ones we moved to 97085). Pure read query, "
         "no API calls. Suspect Nicky's 'sell similar' pattern has polluted "
         "many signers, not just BR."),

        ("roadmap", "deadwood_review",
         "Review D002 stale-1yr-no-watchers listings across catalogue",
         "BR alone had 10. Across catalogue probably hundreds. "
         "Options: retire, reprice, or pull from Promoted Listings entirely."),

        ("roadmap", "ads_tiered_campaigns",
         "Tiered PLS campaigns (Hot / Main / Long-tail)",
         "Current: 1 campaign, DYNAMIC cap 8.2%, 13,971 ads, 13x ROAS. "
         "Want: separate campaigns per performance tier, per-archetype caps, "
         "autoSelectFutureInventory stays on 'Main' only. Need Seller Hub "
         "CSV export to get per-listing ROAS first."),

        ("roadmap", "ads_pla_test",
         "PLA (PPC) test campaign on top keywords",
         "Start small — 5-10 high-intent keywords (e.g. 'bryan robson signed', "
         "'signed manchester united', 'rooney autograph'). Measure vs PLS."),

        ("roadmap", "ads_daily_housekeeping",
         "Daily ads housekeeping script",
         "Runs 6am. Re-tiers listings, moves ads between campaigns, logs to "
         "optimization_log. Dry-run default, --apply to go live."),

        ("roadmap", "category_id_backfill_all",
         "Backfill category_id for all 13,840 listings",
         "Today only BR (167) have it post-fix (commit 2d9a2e3). ~1.5 hours "
         "of GetItem calls at safe rate. Do on a day when no other API-heavy "
         "work is running."),

        ("title_cleanup", "BR_missing_coa_in_title",
         "12 BR listings missing 'COA' in title",
         "Flagged by T102_missing_coa. Same fix applicable to any signer."),

        ("title_cleanup", "BR_missing_size_in_title",
         "1 BR listing missing size token in title",
         "Flagged by T103_missing_size."),

        ("roadmap", "audit_rule_s003_bug",
         "S003_missing_authentication rule checks wrong key name",
         "Rule looks for 'Authentication' but we set 'Authenticity'. All 149 "
         "BR listings flagged incorrectly. Fix in pipeline/audit_rules.py."),

        ("roadmap", "audit_rule_t104_longform_team",
         "New audit rule: flag listings with known long-form team in title",
         "Catches legacy listings that have 'Manchester United' in title "
         "where 'Man Utd' would fit. Enabled by the new shrink_club() helper."),

        ("ads", "ebay_report_api_500s",
         "ad_report_task endpoint returns 500 on LISTING_PERFORMANCE_REPORT",
         "Tried 30d and 7d windows, always 500. Workaround: manual CSV "
         "export from Seller Hub until we figure out the working shape."),
    ]

    inserted = 0
    for topic, key, title, details in entries:
        before = conn.execute(
            "SELECT 1 FROM backlog WHERE topic = ? AND key = ?",
            (topic, key),
        ).fetchone()
        if before:
            continue
        note(conn, topic=topic, key=key, title=title, details=details,
             source="seed_initial_roadmap")
        inserted += 1
    return inserted
