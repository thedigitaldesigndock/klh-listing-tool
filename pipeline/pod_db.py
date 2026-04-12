"""
Local SQLite tracker for POD (Two Fifteen) orders.

Separate file from pipeline.audit_db so the two concerns don't share a
schema and so POD writes never contend with audit reads. Lives at:

    ~/.klh/pod.db

One table, wide row, same philosophy as audit_db:
    - No joins, no migrations
    - SCHEMA_VERSION + drop/recreate if the shape needs to change
    - Timestamps stored as ISO-8601 UTC strings for human readability

Lifecycle of a row:

    pending      ← we logged the intent, sitting in the 45-min buffer
        │
        ▼
    submitted    ← we POSTed to 215, got back an order id
        │
        ▼
    shipped      ← 215 webhook (or poll) says the order has shipped,
        │          tracking_number populated
        ▼
    synced       ← we pushed tracking back to eBay via CompleteSale

    cancelled    ← we deleted it (either inside the buffer window or via
                   DELETE /orders.php after submission)
    failed       ← permanent failure (retries exhausted, see last_error)

Supported transitions (enforced by the helpers in twofifteen.orders, not
by the DB itself — SQLite is happy with anything):

    pending   → submitted | cancelled | failed
    submitted → shipped   | cancelled | failed
    shipped   → synced    | failed
    synced    → (terminal)
    cancelled → (terminal)
    failed    → (terminal unless manually reset)
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

DB_PATH = Path(os.path.expanduser("~/.klh/pod.db"))
SCHEMA_VERSION = 1

# Valid values for the `status` column. Not enforced at DB level — just
# documentation-as-code that the helpers consult.
STATUS_PENDING = "pending"
STATUS_SUBMITTED = "submitted"
STATUS_SHIPPED = "shipped"
STATUS_SYNCED = "synced"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"

ALL_STATUSES = frozenset(
    {STATUS_PENDING, STATUS_SUBMITTED, STATUS_SHIPPED,
     STATUS_SYNCED, STATUS_CANCELLED, STATUS_FAILED}
)

TERMINAL_STATUSES = frozenset({STATUS_SYNCED, STATUS_CANCELLED, STATUS_FAILED})


_SCHEMA = """
CREATE TABLE IF NOT EXISTS pod_orders (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    -- identity
    listing_ref          TEXT,               -- KLH/eBay listing ref, nullable
    external_id          TEXT UNIQUE,        -- what we send as Order.external_id
                                             -- to 215, = "klh-pod-{id}" by default
    -- 215 side
    twofifteen_order_id  TEXT,               -- populated after POST succeeds
    twofifteen_status    TEXT,               -- 215's own status string
    -- product
    sku                  TEXT NOT NULL,      -- base product code (CERMUG-01 etc.)
    design_url           TEXT NOT NULL,      -- what we sent 215 to fetch
    decoration_title     TEXT NOT NULL,      -- canonical decoration position
    quantity             INTEGER NOT NULL DEFAULT 1,
    title                TEXT,               -- human label for this order item
    -- buyer
    ship_to_json         TEXT NOT NULL,      -- JSON of the shipping_address dict
    buyer_email          TEXT,
    -- lifecycle status
    status               TEXT NOT NULL,      -- one of ALL_STATUSES
    -- timing (all UTC ISO-8601)
    created_at           TEXT NOT NULL,
    submit_after         TEXT,               -- buffer deadline, pending → submitted
    submitted_at         TEXT,
    shipped_at           TEXT,
    tracking_synced_at   TEXT,
    cancelled_at         TEXT,
    -- fulfilment
    tracking_number      TEXT,
    tracking_carrier     TEXT,
    -- 215-hosted assets returned on create
    design_url_215       TEXT,               -- 215's own copy URL
    mockup_url_215       TEXT,               -- 215's auto-rendered mockup URL
    -- errors
    error_count          INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT,
    last_error_at        TEXT,
    -- raw response payloads (for debugging / replay)
    create_response_json TEXT,
    update_response_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_pod_status         ON pod_orders(status);
CREATE INDEX IF NOT EXISTS idx_pod_submit_after   ON pod_orders(submit_after);
CREATE INDEX IF NOT EXISTS idx_pod_215_id         ON pod_orders(twofifteen_order_id);
CREATE INDEX IF NOT EXISTS idx_pod_listing        ON pod_orders(listing_ref);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


# --------------------------------------------------------------------------- #
# Connection helpers
# --------------------------------------------------------------------------- #

def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def connect(
    path: Path = DB_PATH,
    *,
    readonly: bool = False,
) -> Iterator[sqlite3.Connection]:
    """
    Open the POD DB, creating file + schema if needed. WAL mode so the
    buffer scheduler can write while the dashboard reads.
    """
    _ensure_parent(path)
    if readonly:
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30)
    else:
        conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    if not readonly:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if (mode or "").lower() != "wal":
            conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    try:
        if not readonly:
            conn.executescript(_SCHEMA)
            set_meta(conn, "schema_version", str(SCHEMA_VERSION))
        yield conn
        if not readonly:
            conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# meta
# --------------------------------------------------------------------------- #

def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_meta(
    conn: sqlite3.Connection, key: str, default: Optional[str] = None
) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else default


# --------------------------------------------------------------------------- #
# Row CRUD
# --------------------------------------------------------------------------- #

def insert_pending(
    conn: sqlite3.Connection,
    *,
    sku: str,
    design_url: str,
    decoration_title: str,
    ship_to: dict[str, Any],
    quantity: int = 1,
    title: Optional[str] = None,
    listing_ref: Optional[str] = None,
    buyer_email: Optional[str] = None,
    buffer_minutes: int = 0,
) -> int:
    """
    Insert a new row in `pending` state and return its integer id.

    The 215 `external_id` is derived from this id (`klh-pod-<id>`) and
    written back in a second statement. Using the row id means the
    external_id is guaranteed unique without us having to generate UUIDs
    or worry about retries.
    """
    now = _now_iso()
    submit_after = None
    if buffer_minutes > 0:
        submit_after = (
            datetime.now(timezone.utc) + timedelta(minutes=buffer_minutes)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        # No buffer → ready to submit immediately.
        submit_after = now

    cur = conn.execute(
        """
        INSERT INTO pod_orders (
            listing_ref, sku, design_url, decoration_title, quantity,
            title, ship_to_json, buyer_email, status,
            created_at, submit_after
        ) VALUES (
            :listing_ref, :sku, :design_url, :decoration_title, :quantity,
            :title, :ship_to_json, :buyer_email, :status,
            :created_at, :submit_after
        )
        """,
        {
            "listing_ref":      listing_ref,
            "sku":              sku,
            "design_url":       design_url,
            "decoration_title": decoration_title,
            "quantity":         quantity,
            "title":            title,
            "ship_to_json":     json.dumps(ship_to, separators=(",", ":")),
            "buyer_email":      buyer_email,
            "status":           STATUS_PENDING,
            "created_at":       now,
            "submit_after":     submit_after,
        },
    )
    pod_id = int(cur.lastrowid)
    external_id = f"klh-pod-{pod_id}"
    conn.execute(
        "UPDATE pod_orders SET external_id = ? WHERE id = ?",
        (external_id, pod_id),
    )
    return pod_id


def mark_submitted(
    conn: sqlite3.Connection,
    pod_id: int,
    *,
    twofifteen_order_id: str,
    twofifteen_status: Optional[str],
    design_url_215: Optional[str],
    mockup_url_215: Optional[str],
    create_response: dict[str, Any],
) -> None:
    conn.execute(
        """
        UPDATE pod_orders SET
            status               = :status,
            submitted_at         = :submitted_at,
            twofifteen_order_id  = :twofifteen_order_id,
            twofifteen_status    = :twofifteen_status,
            design_url_215       = :design_url_215,
            mockup_url_215       = :mockup_url_215,
            create_response_json = :create_response_json,
            last_error           = NULL,
            last_error_at        = NULL
        WHERE id = :id
        """,
        {
            "id":                   pod_id,
            "status":               STATUS_SUBMITTED,
            "submitted_at":         _now_iso(),
            "twofifteen_order_id":  str(twofifteen_order_id),
            "twofifteen_status":    twofifteen_status,
            "design_url_215":       design_url_215,
            "mockup_url_215":       mockup_url_215,
            "create_response_json": json.dumps(create_response, default=str),
        },
    )


def mark_shipped(
    conn: sqlite3.Connection,
    pod_id: int,
    *,
    tracking_number: str,
    tracking_carrier: Optional[str] = None,
    shipped_at: Optional[str] = None,
    update_response: Optional[dict[str, Any]] = None,
) -> None:
    conn.execute(
        """
        UPDATE pod_orders SET
            status               = :status,
            shipped_at           = :shipped_at,
            tracking_number      = :tracking_number,
            tracking_carrier     = :tracking_carrier,
            update_response_json = :update_response_json
        WHERE id = :id
        """,
        {
            "id":                   pod_id,
            "status":               STATUS_SHIPPED,
            "shipped_at":           shipped_at or _now_iso(),
            "tracking_number":      tracking_number,
            "tracking_carrier":     tracking_carrier,
            "update_response_json": json.dumps(update_response, default=str)
                                    if update_response is not None else None,
        },
    )


def mark_synced(conn: sqlite3.Connection, pod_id: int) -> None:
    conn.execute(
        """
        UPDATE pod_orders
           SET status = :status, tracking_synced_at = :ts
         WHERE id = :id
        """,
        {
            "id":     pod_id,
            "status": STATUS_SYNCED,
            "ts":     _now_iso(),
        },
    )


def mark_cancelled(conn: sqlite3.Connection, pod_id: int) -> None:
    conn.execute(
        """
        UPDATE pod_orders
           SET status = :status, cancelled_at = :ts
         WHERE id = :id
        """,
        {
            "id":     pod_id,
            "status": STATUS_CANCELLED,
            "ts":     _now_iso(),
        },
    )


def record_error(
    conn: sqlite3.Connection,
    pod_id: int,
    error: str,
    *,
    fatal: bool = False,
) -> None:
    """
    Record an error on the row. If `fatal=True`, also transitions the row
    to `failed`. Otherwise increments error_count and leaves status alone
    so the caller can decide whether to retry.
    """
    now = _now_iso()
    conn.execute(
        """
        UPDATE pod_orders SET
            error_count    = error_count + 1,
            last_error     = :last_error,
            last_error_at  = :last_error_at,
            status         = CASE WHEN :fatal = 1 THEN :failed_status ELSE status END
        WHERE id = :id
        """,
        {
            "id":            pod_id,
            "last_error":    error[:4000],  # bounded
            "last_error_at": now,
            "fatal":         1 if fatal else 0,
            "failed_status": STATUS_FAILED,
        },
    )


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #

def get_by_id(
    conn: sqlite3.Connection, pod_id: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM pod_orders WHERE id = ?", (pod_id,)
    ).fetchone()


def get_by_twofifteen_id(
    conn: sqlite3.Connection, twofifteen_order_id: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM pod_orders WHERE twofifteen_order_id = ?",
        (str(twofifteen_order_id),),
    ).fetchone()


def get_by_external_id(
    conn: sqlite3.Connection, external_id: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM pod_orders WHERE external_id = ?", (external_id,)
    ).fetchone()


def iter_by_status(
    conn: sqlite3.Connection, status: str, *, limit: Optional[int] = None
) -> Iterator[sqlite3.Row]:
    sql = "SELECT * FROM pod_orders WHERE status = ? ORDER BY id"
    params: list[Any] = [status]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    for row in conn.execute(sql, params):
        yield row


def due_for_submission(
    conn: sqlite3.Connection, *, now: Optional[str] = None
) -> Iterator[sqlite3.Row]:
    """
    Rows in `pending` whose buffer deadline has passed and are ready to
    submit. Used by the buffer scheduler to pick up work.
    """
    now = now or _now_iso()
    for row in conn.execute(
        """
        SELECT * FROM pod_orders
         WHERE status = ? AND submit_after <= ?
         ORDER BY id
        """,
        (STATUS_PENDING, now),
    ):
        yield row


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    if d.get("ship_to_json"):
        try:
            d["ship_to"] = json.loads(d["ship_to_json"])
        except json.JSONDecodeError:
            d["ship_to"] = {}
    return d


def count_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM pod_orders GROUP BY status"
    ).fetchall()
    return {r["status"]: int(r["n"]) for r in rows}
