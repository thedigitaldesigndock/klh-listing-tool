#!/usr/bin/env python3
"""
Two Fifteen (twofifteen.co.uk) Brick API client for KLH Autographs.

Dependency-free — uses only the stdlib, matching the style of the sibling
ebay_api.token_manager module. Credentials are read from ~/.klh/.env:

    TWOFIFTEEN_APP_ID=APP-xxxxxxxx
    TWOFIFTEEN_SECRET_KEY=<secret>

Auth model (from twofifteen.co.uk/api/openapi.yml):
    - Every request needs ?AppId=APP-xxxxxxxx&Signature=<sha1>
    - POST: Signature = SHA1(raw_body + SECRET_KEY)
    - GET/DELETE: Signature = SHA1(query_string_without_signature + SECRET_KEY)

Typical usage:
    from twofifteen import TwoFifteenClient
    client = TwoFifteenClient()                   # reads ~/.klh/.env
    order  = client.create_order({...})           # POST /orders.php
    latest = client.get_order(order["order"]["id"])
    listing = client.list_orders(status=3, limit=50)  # shipped orders

Run directly for a quick self-check:
    python -m twofifteen.client              # prints AppID + auth status
    python -m twofifteen.client --list       # lists recent orders

Phase 1 scope: low-level signed request wrapper + thin endpoint helpers.
Higher-level helpers (build_mug_order, submit_pod_for_listing, etc.) live
in twofifteen.orders once that module is added.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable, Optional

from . import schema

CONFIG_DIR = os.path.expanduser("~/.klh")
ENV_FILE = os.path.join(CONFIG_DIR, ".env")


class TwoFifteenError(RuntimeError):
    """Raised for any Two Fifteen API or configuration error."""


def _load_env(path: str = ENV_FILE) -> dict[str, str]:
    """
    Parse ~/.klh/.env into a dict. Dependency-free — matches the helper
    in ebay_api.token_manager exactly so both modules stay interchangeable.
    """
    if not os.path.exists(path):
        raise TwoFifteenError(
            f".env not found at {path}. Add TWOFIFTEEN_APP_ID and "
            f"TWOFIFTEEN_SECRET_KEY to ~/.klh/.env."
        )
    env: dict[str, str] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


class TwoFifteenClient:
    """
    Thin wrapper around 215's Brick API.

    Intentionally narrow: five endpoints, one signing helper, no retry
    policy. Retry / rate-limit / circuit-breaker behaviour belongs one
    layer up in twofifteen.orders when we build it.
    """

    def __init__(
        self,
        app_id: Optional[str] = None,
        secret_key: Optional[str] = None,
        base_url: str = schema.BASE_URL,
        verbose: bool = False,
    ) -> None:
        if app_id is None or secret_key is None:
            env = _load_env()
            app_id = app_id or env.get("TWOFIFTEEN_APP_ID")
            secret_key = secret_key or env.get("TWOFIFTEEN_SECRET_KEY")

        if not app_id or not app_id.startswith("APP-"):
            raise TwoFifteenError(
                "TWOFIFTEEN_APP_ID missing or malformed "
                "(expected APP-xxxxxxxx)."
            )
        if not secret_key:
            raise TwoFifteenError("TWOFIFTEEN_SECRET_KEY missing from ~/.klh/.env.")

        self.app_id = app_id
        self._secret_key = secret_key
        self.base_url = base_url.rstrip("/")
        self.verbose = verbose

    # ---------- signature helpers ----------

    @staticmethod
    def _sha1(message: str) -> str:
        return hashlib.sha1(message.encode("utf-8")).hexdigest()

    def _sign_body(self, raw_body: str) -> str:
        """POST/PUT: SHA1(body + secret)."""
        return self._sha1(raw_body + self._secret_key)

    def _sign_query(self, params_without_signature: Iterable[tuple[str, Any]]) -> tuple[str, str]:
        """
        GET/DELETE: SHA1(query_string_without_signature + secret).

        215's spec is explicit: the signed string is the query string as
        it appears in the URL after '?' with the Signature param removed.
        We build the query ourselves from an ordered list of (key, value)
        pairs so the signed bytes are exactly the bytes we send.
        """
        qs = urllib.parse.urlencode(list(params_without_signature))
        return qs, self._sha1(qs + self._secret_key)

    # ---------- low-level request ----------

    def _request(
        self,
        method: str,
        path: str,
        query: Optional[list[tuple[str, Any]]] = None,
        body_obj: Optional[dict] = None,
    ) -> Any:
        query = list(query or [])
        # AppId always goes into the query string for every request.
        query.append(("AppId", self.app_id))

        raw_body = b""
        headers: dict[str, str] = {"Accept": "application/json"}

        if method == "POST":
            if body_obj is None:
                body_obj = {}
            raw_body_str = json.dumps(body_obj, separators=(",", ":"))
            signature = self._sign_body(raw_body_str)
            query.append(("Signature", signature))
            raw_body = raw_body_str.encode("utf-8")
            headers["Content-Type"] = "application/json"
            qs = urllib.parse.urlencode(query)
        else:
            qs, signature = self._sign_query(query)
            qs = qs + "&Signature=" + signature

        url = f"{self.base_url}{path}?{qs}"

        if self.verbose:
            print(f"{method} {url}")
            if raw_body:
                preview = raw_body.decode("utf-8", errors="replace")[:300]
                print(f"  body: {preview}...")

        req = urllib.request.Request(
            url, data=raw_body if raw_body else None, method=method
        )
        for k, v in headers.items():
            req.add_header(k, v)

        try:
            with urllib.request.urlopen(req) as resp:
                status = resp.status
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise TwoFifteenError(
                f"{method} {path} failed ({e.code}): {err_body}"
            ) from e
        except urllib.error.URLError as e:
            raise TwoFifteenError(f"{method} {path} network error: {e}") from e

        if not body:
            return {"_status": status}
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            raise TwoFifteenError(
                f"{method} {path} returned non-JSON: {body[:300]}"
            )

    # ---------- public API ----------

    def create_order(self, order: dict) -> dict:
        """
        POST /orders.php — create a new order.

        `order` must match 215's Order schema (note: address fields are
        camelCase — firstName, lastName, address1, etc. See schema.py for
        the full list.). Returns `{"order": {...}}` with server-assigned id.
        """
        return self._request("POST", "/orders.php", body_obj=order)

    def get_order(self, order_id: int | str) -> dict:
        """GET /order.php?id=... — returns `{"order": {...}}`."""
        return self._request("GET", "/order.php", query=[("id", order_id)])

    def list_orders(self, **filters: Any) -> dict:
        """
        GET /orders.php with optional filters:
            ids, since_id, created_at_min, created_at_max, status, page, limit

        Returns `{"orders": [...]}`. Use `status=3` to filter for shipped
        orders (this is the primary fulfilment-sync query).
        """
        query = [(k, v) for k, v in filters.items() if v is not None]
        return self._request("GET", "/orders.php", query=query)

    def count_orders(self, **filters: Any) -> dict:
        """
        GET /orders/count.php — returns `{"count": int}`.

        WARNING: 215's server returns HTTP 500 on this endpoint even for
        successful requests. The body is still valid JSON with the correct
        count, but the status code is broken. Prefer list_orders for an
        auth probe; this method is only useful if you suppress the error
        and parse the body manually.
        """
        query = [(k, v) for k, v in filters.items() if v is not None]
        return self._request("GET", "/orders/count.php", query=query)

    def delete_order(self, order_id: int | str) -> dict:
        """
        DELETE /orders.php?id=... — cancel an order.

        Confirmed to work on `Received` status orders. The window after
        which delete stops working (production started) is an open question
        — ask 215 support for the exact cutoff. See docs/ (or Peter's
        running email thread with 215) for the latest word.
        """
        return self._request("DELETE", "/orders.php", query=[("id", order_id)])


# ---------- CLI self-check ----------

def _cli() -> None:
    """`python -m twofifteen.client` — quick sanity probe."""
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    client = TwoFifteenClient(verbose=verbose)

    print("=" * 60)
    print("Two Fifteen API self-check")
    print("=" * 60)
    print(f"  AppID:    {client.app_id}")
    print(f"  Base URL: {client.base_url}")
    print()

    try:
        result = client.list_orders(limit=1)
    except TwoFifteenError as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    # 215 returns {"orders": [...]}; tolerate a bare list just in case.
    if isinstance(result, dict):
        orders = result.get("orders", [])
    else:
        orders = result or []
    print(f"  Auth OK. Orders on account: {len(orders)}")

    if "--list" in sys.argv:
        result = client.list_orders(limit=10)
        orders = result.get("orders", []) if isinstance(result, dict) else (result or [])
        print(f"\n  Last {len(orders)} orders:")
        for o in orders:
            print(
                f"    #{o.get('id')}  status={o.get('status')}  "
                f"ext_id={o.get('external_id')}  "
                f"created={o.get('created_at')}"
            )


if __name__ == "__main__":
    _cli()
