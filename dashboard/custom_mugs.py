"""
Custom Mug review panel for the KLH dashboard.

Buyer orders a custom mug on eBay with personalisation (shirt name + number).
`process_mug_orders.py` detects the order, runs the headless nameset-generator
to produce a print-ready PNG, inserts a row into `pod.db` with
status=awaiting_review, and parks it.

This module serves the review UI:

    GET  /custom-mugs                          → static shell
    GET  /api/custom-mugs/pending              → list rows needing review
    GET  /api/custom-mugs/pending/count        → integer count (for nav badge)
    GET  /api/custom-mugs/{pod_id}/preview.png → the generated wrap PNG
    POST /api/custom-mugs/{pod_id}/approve     → mark for POD → 215
    POST /api/custom-mugs/{pod_id}/inhouse     → mark for in-house fulfil
    POST /api/custom-mugs/{pod_id}/reject      → reject, do not fulfil

Approving a row flips it from status=awaiting_review to status=pending,
which puts it back into the cron's normal 215-submission flow.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from pipeline import pod_db


# --------------------------------------------------------------------------- #
# R2 upload helper — invoked on approve to populate design_url for 215.
# Credentials + bucket config in ~/.klh/.env (same file as the rest of KLH).
# --------------------------------------------------------------------------- #

def _r2_config() -> dict[str, str]:
    env: dict[str, str] = {}
    env_path = os.path.expanduser("~/.klh/.env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _upload_png_to_r2(local_path: Path, key: str) -> str:
    """Upload PNG to R2, return public URL. Raises on failure."""
    import boto3
    env = _r2_config()
    required = ["R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
                "R2_BUCKET", "R2_PUBLIC_BASE"]
    for k in required:
        if not env.get(k):
            raise RuntimeError(f"R2 credential {k} missing from ~/.klh/.env")
    s3 = boto3.client(
        "s3",
        endpoint_url=env["R2_ENDPOINT"],
        aws_access_key_id=env["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    with open(local_path, "rb") as fh:
        s3.put_object(
            Bucket=env["R2_BUCKET"],
            Key=key,
            Body=fh,
            ContentType="image/png",
            CacheControl="public, max-age=31536000, immutable",
        )
    base = env["R2_PUBLIC_BASE"].rstrip("/")
    return f"{base}/{quote(key, safe='-._/')}"

STATIC_DIR = Path(__file__).resolve().parent / "static"


class ReviewDecision(BaseModel):
    notes: Optional[str] = None          # free-text, stored on row
    reviewed_by: Optional[str] = "peter" # default, set by UI if multi-user
    quantity: Optional[int] = None       # override (rare — normally 1)


def _pod_ids_file_path(pod_id: int, wrap_png_path: Optional[str]) -> Optional[Path]:
    if not wrap_png_path:
        return None
    p = Path(wrap_png_path)
    return p if p.exists() else None


def _row_to_json(row) -> dict:
    """Serialise a pod_orders row to a safe dict for the dashboard UI."""
    ship_to = {}
    try:
        ship_to = json.loads(row["ship_to_json"]) if row["ship_to_json"] else {}
    except Exception:
        ship_to = {"_error": "ship_to_json parse failed"}
    return {
        "pod_id":                row["id"],
        "listing_ref":           row["listing_ref"],
        "sku":                   row["sku"],
        "status":                row["status"],
        "title":                 row["title"],
        "quantity":              row["quantity"],
        "created_at":            row["created_at"],
        "buyer_email":           row["buyer_email"],
        "shipping_name":         f"{ship_to.get('firstName','')} {ship_to.get('lastName','')}".strip(),
        "shipping_postcode":     ship_to.get("postcode", ""),
        "shipping_country":      ship_to.get("country", ""),
        # custom-specific
        "is_custom":             bool(row["is_custom"]),
        "custom_team":           row["custom_team"],
        "custom_era":            row["custom_era"],
        "personalisation_name":  row["personalisation_name"],
        "personalisation_number": row["personalisation_number"],
        "fulfillment_mode":      row["fulfillment_mode"],
        "has_preview":           bool(row["wrap_png_path"]),
        "reviewed_at":           row["reviewed_at"],
        "reviewed_by":           row["reviewed_by"],
        "review_notes":          row["review_notes"],
    }


def register_custom_mugs_routes(app: FastAPI) -> None:

    @app.get("/custom-mugs", include_in_schema=False)
    def custom_mugs_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "custom_mugs.html")

    @app.get("/api/custom-mugs/pending")
    def list_pending() -> JSONResponse:
        with pod_db.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT * FROM pod_orders "
                "WHERE status = ? "
                "ORDER BY created_at ASC",
                (pod_db.STATUS_AWAITING_REVIEW,),
            ).fetchall()
        return JSONResponse({"items": [_row_to_json(r) for r in rows]})

    @app.get("/api/custom-mugs/pending/count")
    def pending_count() -> JSONResponse:
        with pod_db.connect(readonly=True) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM pod_orders WHERE status = ?",
                (pod_db.STATUS_AWAITING_REVIEW,),
            ).fetchone()[0]
        return JSONResponse({"count": n})

    @app.get("/api/custom-mugs/{pod_id}/preview.png")
    def preview_png(pod_id: int):
        with pod_db.connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT wrap_png_path FROM pod_orders WHERE id = ?",
                (pod_id,),
            ).fetchone()
        if not row:
            raise HTTPException(404, f"pod_id {pod_id} not found")
        png = _pod_ids_file_path(pod_id, row["wrap_png_path"])
        if not png:
            raise HTTPException(404, "preview PNG missing on disk")
        return FileResponse(
            str(png),
            media_type="image/png",
            # Don't cache — if re-rendered, we want latest
            headers={"Cache-Control": "no-cache"},
        )

    @app.post("/api/custom-mugs/{pod_id}/approve")
    def approve(pod_id: int, decision: ReviewDecision) -> JSONResponse:
        """
        Approve workflow:
          1. Upload the wrap PNG to R2
          2. Update design_url to the R2 public URL
          3. Flip status awaiting_review → pending (becomes eligible for 215)
          4. Set submit_after = now (cron's flush_buffer picks it up)
        """
        now = pod_db._now_iso()
        with pod_db.connect() as conn:
            row = conn.execute(
                "SELECT status, wrap_png_path, sku, personalisation_name, "
                "personalisation_number, custom_team, custom_era "
                "FROM pod_orders WHERE id = ?", (pod_id,),
            ).fetchone()
            if not row:
                raise HTTPException(404, f"pod_id {pod_id} not found")
            if row["status"] != pod_db.STATUS_AWAITING_REVIEW:
                raise HTTPException(
                    409,
                    f"pod_id {pod_id} is {row['status']!r}, not awaiting_review",
                )

            local_path = Path(row["wrap_png_path"] or "")
            if not local_path.exists():
                raise HTTPException(
                    500,
                    f"wrap PNG missing on disk: {local_path}",
                )

            # Build a human-readable R2 key
            name = (row["personalisation_name"] or "NONAME").replace(" ", "_")
            number = row["personalisation_number"] or "0"
            team = (row["custom_team"] or "UNKNOWN").replace(" ", "")
            era = row["custom_era"] or "0000-00"
            r2_key = f"custom/{team}_{era}_{name}_{number}_pod{pod_id}.png"

            try:
                public_url = _upload_png_to_r2(local_path, r2_key)
            except Exception as e:
                raise HTTPException(500, f"R2 upload failed: {e}")

            conn.execute(
                """
                UPDATE pod_orders
                SET status = ?,
                    design_url = ?,
                    fulfillment_mode = ?,
                    reviewed_at = ?,
                    reviewed_by = ?,
                    review_notes = ?,
                    submit_after = ?
                WHERE id = ?
                """,
                (
                    pod_db.STATUS_PENDING,
                    public_url,
                    pod_db.FULFILLMENT_POD,
                    now,
                    decision.reviewed_by,
                    decision.notes,
                    now,
                    pod_id,
                ),
            )
        return JSONResponse({
            "ok": True,
            "pod_id": pod_id,
            "status": pod_db.STATUS_PENDING,
            "design_url": public_url,
        })

    @app.post("/api/custom-mugs/{pod_id}/inhouse")
    def inhouse(pod_id: int, decision: ReviewDecision) -> JSONResponse:
        """Mark for in-house fulfilment — keeps status awaiting_review but flips mode."""
        now = pod_db._now_iso()
        with pod_db.connect() as conn:
            row = conn.execute(
                "SELECT status FROM pod_orders WHERE id = ?", (pod_id,),
            ).fetchone()
            if not row:
                raise HTTPException(404, f"pod_id {pod_id} not found")
            conn.execute(
                """
                UPDATE pod_orders
                SET status = ?,
                    fulfillment_mode = ?,
                    reviewed_at = ?,
                    reviewed_by = ?,
                    review_notes = ?
                WHERE id = ?
                """,
                (
                    pod_db.STATUS_SUBMITTED,        # treat like submitted (removes from review queue)
                    pod_db.FULFILLMENT_INHOUSE,
                    now,
                    decision.reviewed_by,
                    decision.notes,
                    pod_id,
                ),
            )
        return JSONResponse({"ok": True, "pod_id": pod_id, "status": "in-house"})

    @app.post("/api/custom-mugs/{pod_id}/reject")
    def reject(pod_id: int, decision: ReviewDecision) -> JSONResponse:
        now = pod_db._now_iso()
        with pod_db.connect() as conn:
            row = conn.execute(
                "SELECT status FROM pod_orders WHERE id = ?", (pod_id,),
            ).fetchone()
            if not row:
                raise HTTPException(404, f"pod_id {pod_id} not found")
            conn.execute(
                """
                UPDATE pod_orders
                SET status = ?,
                    reviewed_at = ?,
                    reviewed_by = ?,
                    review_notes = ?
                WHERE id = ?
                """,
                (
                    pod_db.STATUS_REJECTED,
                    now,
                    decision.reviewed_by,
                    decision.notes,
                    pod_id,
                ),
            )
        return JSONResponse({"ok": True, "pod_id": pod_id, "status": pod_db.STATUS_REJECTED})
