"""
Higher-level order helpers for Two Fifteen POD integration.

Sits on top of `twofifteen.client.TwoFifteenClient` and
`pipeline.pod_db` to give the rest of the codebase a single function to
call: "submit this listing's design as a 215 order".

Everything here is DB-logged and idempotent where it can be:
    - Intent is recorded in pod_db BEFORE we hit 215
    - On success, the 215 order id is written back to the same row
    - On failure, the error is recorded on the row and the row stays in
      `pending` so it can be retried (or marked `failed` after enough
      retries)

Higher-level callers will typically:

    with pod_db.connect() as conn:
        order = submit_mug_design(
            client, conn,
            sku="CERMUG-01",
            design_url="https://...",
            ship_to={...},
            listing_ref="ebay-12345",
            title="Hughes 10 Man Utd Mug",
            buffer_minutes=45,
        )
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from pipeline import pod_db

from . import schema
from .client import TwoFifteenClient, TwoFifteenError


# --------------------------------------------------------------------------- #
# Payload construction
# --------------------------------------------------------------------------- #

def build_mug_order(
    *,
    sku: str,
    design_url: str,
    ship_to: dict[str, Any],
    external_id: str,
    quantity: int = 1,
    title: Optional[str] = None,
    decoration_title: str = schema.DECORATION_CERAMIC_MUG_WRAP,
    brand: str = "KLH Autographs",
    channel: str = schema.CHANNEL_API,
    buyer_email: Optional[str] = None,
) -> dict[str, Any]:
    """
    Construct the JSON payload for POST /orders.php for a ceramic mug.

    Validated inputs (raises TwoFifteenError if something's obviously
    wrong before we waste an API call):
        - channel must be in schema.CHANNELS
        - ship_to must contain at minimum firstName + lastName
        - sku must be non-empty
        - design_url must be a plausible http(s) URL
    """
    if channel not in schema.CHANNELS:
        raise TwoFifteenError(
            f"invalid channel {channel!r} (must be one of {sorted(schema.CHANNELS)})"
        )
    if not sku:
        raise TwoFifteenError("sku is required")
    if not design_url or not design_url.startswith(("http://", "https://")):
        raise TwoFifteenError(
            f"design_url must be an http(s) URL, got {design_url!r}"
        )
    if not ship_to.get("firstName") or not ship_to.get("lastName"):
        raise TwoFifteenError(
            "ship_to must include firstName and lastName "
            "(see twofifteen.schema.ADDRESS_FIELDS for the full schema)"
        )

    payload: dict[str, Any] = {
        "external_id":  external_id,
        "brand":        brand,
        "channel":      channel,
        "shipping_address": dict(ship_to),
        "items": [
            {
                "pn":       sku,
                "quantity": quantity,
                "title":    title or f"KLH POD order ({sku})",
                "designs":  [{"title": decoration_title, "src": design_url}],
                "mockups":  [{"title": decoration_title, "src": design_url}],
            }
        ],
    }
    if buyer_email:
        payload["buyer_email"] = buyer_email
    return payload


# --------------------------------------------------------------------------- #
# Submission
# --------------------------------------------------------------------------- #

def _first_item(order: dict[str, Any]) -> dict[str, Any]:
    """Pull the first line item out of a 215 order response, tolerating shape."""
    inner = order.get("order") if isinstance(order.get("order"), dict) else order
    items = (inner or {}).get("items") or []
    return items[0] if items else {}


def _first_asset_url(assets: list[dict[str, Any]]) -> Optional[str]:
    for a in assets or []:
        if a.get("src"):
            return a["src"]
    return None


def submit_mug_design(
    client: TwoFifteenClient,
    conn: sqlite3.Connection,
    *,
    sku: str,
    design_url: str,
    ship_to: dict[str, Any],
    listing_ref: Optional[str] = None,
    title: Optional[str] = None,
    quantity: int = 1,
    buyer_email: Optional[str] = None,
    buffer_minutes: int = 0,
    decoration_title: str = schema.DECORATION_CERAMIC_MUG_WRAP,
    submit_now: bool = True,
) -> dict[str, Any]:
    """
    Log a POD order in pod_db and (optionally) submit it to 215 now.

    Returns a dict with at least `pod_id`, `status`, and (if submitted)
    `twofifteen_order_id`. Use submit_now=False to just record the intent
    without calling 215 — useful for testing, or for letting a scheduler
    pick it up after the buffer expires.

    On 215 error: the row is kept in `pending` state with the error
    recorded. It's the caller's decision whether to retry.
    """
    pod_id = pod_db.insert_pending(
        conn,
        sku=sku,
        design_url=design_url,
        decoration_title=decoration_title,
        ship_to=ship_to,
        quantity=quantity,
        title=title,
        listing_ref=listing_ref,
        buyer_email=buyer_email,
        buffer_minutes=buffer_minutes,
    )
    conn.commit()

    if not submit_now:
        row = pod_db.get_by_id(conn, pod_id)
        return {
            "pod_id":              pod_id,
            "status":               row["status"] if row else pod_db.STATUS_PENDING,
            "external_id":          row["external_id"] if row else None,
            "twofifteen_order_id":  None,
        }

    external_id = f"klh-pod-{pod_id}"
    payload = build_mug_order(
        sku=sku,
        design_url=design_url,
        ship_to=ship_to,
        external_id=external_id,
        quantity=quantity,
        title=title,
        decoration_title=decoration_title,
        buyer_email=buyer_email,
    )

    try:
        response = client.create_order(payload)
    except TwoFifteenError as e:
        pod_db.record_error(conn, pod_id, str(e), fatal=False)
        conn.commit()
        raise

    inner = response.get("order") if isinstance(response, dict) else None
    if not inner or not inner.get("id"):
        pod_db.record_error(
            conn, pod_id,
            f"unexpected 215 create response shape: {response!r}",
            fatal=True,
        )
        conn.commit()
        raise TwoFifteenError(f"unexpected 215 create response shape: {response!r}")

    first_item = _first_item(response)
    pod_db.mark_submitted(
        conn,
        pod_id,
        twofifteen_order_id=str(inner["id"]),
        twofifteen_status=inner.get("status"),
        design_url_215=_first_asset_url(first_item.get("designs") or []),
        mockup_url_215=_first_asset_url(first_item.get("mockups") or []),
        create_response=response,
    )
    conn.commit()

    return {
        "pod_id":              pod_id,
        "status":               pod_db.STATUS_SUBMITTED,
        "external_id":          external_id,
        "twofifteen_order_id":  str(inner["id"]),
        "twofifteen_status":    inner.get("status"),
        "design_url_215":       _first_asset_url(first_item.get("designs") or []),
        "mockup_url_215":       _first_asset_url(first_item.get("mockups") or []),
    }


# --------------------------------------------------------------------------- #
# Cancellation
# --------------------------------------------------------------------------- #

def cancel_pod_order(
    client: TwoFifteenClient,
    conn: sqlite3.Connection,
    pod_id: int,
) -> dict[str, Any]:
    """
    Cancel a POD order.

    Two cases:
        - Row is still in `pending` (never submitted to 215): just mark
          cancelled in the DB. No API call.
        - Row is `submitted` (215 has an order id): call DELETE
          /orders.php, then mark cancelled. If 215 refuses (e.g. order is
          already in production), we record the error on the row and
          leave its status as `submitted` so the caller knows it wasn't
          successfully cancelled.
    """
    row = pod_db.get_by_id(conn, pod_id)
    if row is None:
        raise TwoFifteenError(f"pod order #{pod_id} not found")

    status = row["status"]
    twofifteen_order_id = row["twofifteen_order_id"]

    if status == pod_db.STATUS_PENDING:
        pod_db.mark_cancelled(conn, pod_id)
        conn.commit()
        return {"pod_id": pod_id, "status": pod_db.STATUS_CANCELLED, "api_called": False}

    if status == pod_db.STATUS_SUBMITTED and twofifteen_order_id:
        try:
            client.delete_order(twofifteen_order_id)
        except TwoFifteenError as e:
            pod_db.record_error(conn, pod_id, f"delete failed: {e}", fatal=False)
            conn.commit()
            raise
        pod_db.mark_cancelled(conn, pod_id)
        conn.commit()
        return {"pod_id": pod_id, "status": pod_db.STATUS_CANCELLED, "api_called": True}

    raise TwoFifteenError(
        f"pod order #{pod_id} is in status {status!r}, not cancellable"
    )
