"""
Local cache for the `klh audit` subcommand family.

We keep every active KLHAutographs listing as a row in a single SQLite
table so that `klh audit report` / `klh audit signer` are instant and
can be re-run any number of times without hitting the eBay API. The
Trading fetch side populates this; the read side is pure local SQL.

Location:
    ~/.klh/audit.db

Schema philosophy:
    * One table, wide row. No joins, no migrations. If the column set
      needs to change, bump SCHEMA_VERSION and drop/recreate.
    * `specifics_json` holds the ItemSpecifics dict as JSON. Left NULL
      until a per-item GetItem pass populates it — GetMyeBaySelling
      returns the summary only, so the summary fetch leaves this blank.
    * `fetched_at` is bumped on every successful refresh of a row. A
      separate `deep_fetched_at` tracks when specifics were last loaded
      so we can ask "which rows still need a per-item deep fetch?".
    * A tiny `meta` key/value table holds run-level bookkeeping
      (`last_summary_fetch`, `last_deep_fetch`) so the CLI can print
      "last refreshed 2h ago" style status without scanning listings.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

DB_PATH = Path(os.path.expanduser("~/.klh/audit.db"))
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    item_id            TEXT PRIMARY KEY,
    title              TEXT,
    sku                TEXT,
    category_id        TEXT,
    category_name      TEXT,
    price_gbp          REAL,
    currency           TEXT,
    quantity           INTEGER,
    quantity_available INTEGER,
    quantity_sold      INTEGER,
    watch_count        INTEGER,
    hit_count          INTEGER,
    start_time         TEXT,
    end_time           TEXT,
    listing_type       TEXT,
    condition_id       TEXT,
    view_item_url      TEXT,
    specifics_json     TEXT,
    fetched_at         TEXT,
    deep_fetched_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_listings_title      ON listings(title);
CREATE INDEX IF NOT EXISTS idx_listings_start_time ON listings(start_time);
CREATE INDEX IF NOT EXISTS idx_listings_category   ON listings(category_id);

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


@contextmanager
def connect(path: Path = DB_PATH, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    """
    Open the audit DB, creating file + schema if needed.

    We run in WAL mode so `klh audit report` / `klh audit apply --dry-run`
    can read while a long-running `klh audit fetch --deep` is writing in
    another process. busy_timeout handles the brief moments WAL still
    holds a lock (checkpoint).

    `readonly=True` opens a read-only URI connection — used by the
    dry-run path so it can never contend with the writer at all.
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
        # WAL only needs to be set once per DB file — it's persistent.
        # Trying to re-set it while another writer has a lock raises
        # "database is locked" even though it's already on.
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# meta
# --------------------------------------------------------------------------- #

def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else default


# --------------------------------------------------------------------------- #
# Row upsert
# --------------------------------------------------------------------------- #

_SUMMARY_COLS = (
    "item_id", "title", "sku", "category_id", "category_name",
    "price_gbp", "currency", "quantity", "quantity_available",
    "watch_count", "start_time", "listing_type", "view_item_url",
    "fetched_at",
)


def upsert_summary(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    """
    Upsert a row from a GetMyeBaySelling summary. Leaves specifics_json
    and deep_fetched_at alone so a subsequent deep pass can fill them in.
    """
    payload = {k: row.get(k) for k in _SUMMARY_COLS}
    payload["fetched_at"] = _now_iso()
    conn.execute(
        """
        INSERT INTO listings
            (item_id, title, sku, category_id, category_name,
             price_gbp, currency, quantity, quantity_available,
             watch_count, start_time, listing_type, view_item_url,
             fetched_at)
        VALUES
            (:item_id, :title, :sku, :category_id, :category_name,
             :price_gbp, :currency, :quantity, :quantity_available,
             :watch_count, :start_time, :listing_type, :view_item_url,
             :fetched_at)
        ON CONFLICT(item_id) DO UPDATE SET
            title              = excluded.title,
            sku                = excluded.sku,
            category_id        = excluded.category_id,
            category_name      = excluded.category_name,
            price_gbp          = excluded.price_gbp,
            currency           = excluded.currency,
            quantity           = excluded.quantity,
            quantity_available = excluded.quantity_available,
            watch_count        = excluded.watch_count,
            start_time         = excluded.start_time,
            listing_type       = excluded.listing_type,
            view_item_url      = excluded.view_item_url,
            fetched_at         = excluded.fetched_at
        """,
        payload,
    )


def upsert_deep(conn: sqlite3.Connection, item_id: str, deep: dict[str, Any]) -> None:
    """Fill in the fields that only GetItem returns (specifics + hit count
    + category, which GetMyeBaySelling doesn't always include)."""
    conn.execute(
        """
        UPDATE listings SET
            specifics_json   = :specifics_json,
            hit_count        = :hit_count,
            quantity_sold    = :quantity_sold,
            end_time         = :end_time,
            condition_id     = :condition_id,
            category_id      = COALESCE(:category_id, category_id),
            category_name    = COALESCE(:category_name, category_name),
            deep_fetched_at  = :deep_fetched_at
        WHERE item_id = :item_id
        """,
        {
            "item_id":         item_id,
            "specifics_json":  json.dumps(deep.get("item_specifics") or {}),
            "hit_count":       deep.get("hit_count"),
            "quantity_sold":   deep.get("quantity_sold"),
            "end_time":        deep.get("end_time"),
            "condition_id":    deep.get("condition_id"),
            "category_id":     deep.get("category_id"),
            "category_name":   deep.get("category_name"),
            "deep_fetched_at": _now_iso(),
        },
    )


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #

def count_rows(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM listings").fetchone()
    return int(row["n"])


def iter_rows(
    conn: sqlite3.Connection,
    *,
    title_prefix: Optional[str] = None,
    limit: Optional[int] = None,
) -> Iterator[sqlite3.Row]:
    sql = "SELECT * FROM listings"
    params: list[Any] = []
    if title_prefix:
        sql += " WHERE title LIKE ?"
        params.append(f"{title_prefix}%")
    sql += " ORDER BY item_id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    for row in conn.execute(sql, params):
        yield row


def get_row(conn: sqlite3.Connection, item_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM listings WHERE item_id = ?", (item_id,)
    ).fetchone()


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    if d.get("specifics_json"):
        try:
            d["specifics"] = json.loads(d["specifics_json"])
        except json.JSONDecodeError:
            d["specifics"] = {}
    else:
        d["specifics"] = {}
    return d
