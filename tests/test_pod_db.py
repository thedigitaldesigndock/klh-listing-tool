"""
Unit tests for pipeline.pod_db — the POD (Two Fifteen) order tracker.

Uses a temporary DB file per test so there's no interference with the
real ~/.klh/pod.db.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import pod_db


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def tmp_pod_db(tmp_path: Path) -> Path:
    """Point pod_db at a fresh file for this test."""
    return tmp_path / "pod.db"


@pytest.fixture
def ship_to() -> dict:
    return {
        "firstName": "Kim",
        "lastName":  "Cowgill",
        "company":   "KLH Autographs",
        "address1":  "137 Dobb Brow Road",
        "address2":  "Westhoughton",
        "city":      "Bolton",
        "postcode":  "BL5 2BA",
        "country":   "GB",
        "phone1":    "07746137657",
    }


# --------------------------------------------------------------------------- #
# insert_pending
# --------------------------------------------------------------------------- #

def test_insert_pending_assigns_external_id(tmp_pod_db, ship_to):
    with pod_db.connect(tmp_pod_db) as conn:
        pod_id = pod_db.insert_pending(
            conn,
            sku="CERMUG-01",
            design_url="https://example.com/wrap.png",
            decoration_title="Printing Front Side",
            ship_to=ship_to,
            title="Test mug",
        )
        assert isinstance(pod_id, int) and pod_id > 0

        row = pod_db.get_by_id(conn, pod_id)
        assert row is not None
        assert row["status"] == pod_db.STATUS_PENDING
        assert row["external_id"] == f"klh-pod-{pod_id}"
        assert row["sku"] == "CERMUG-01"
        assert row["design_url"] == "https://example.com/wrap.png"
        assert row["decoration_title"] == "Printing Front Side"
        assert row["quantity"] == 1  # default
        assert row["error_count"] == 0
        assert row["twofifteen_order_id"] is None

        # ship_to is stored as JSON, round-trips correctly
        stored = json.loads(row["ship_to_json"])
        assert stored["postcode"] == "BL5 2BA"


def test_insert_pending_buffer_minutes_sets_submit_after(tmp_pod_db, ship_to):
    with pod_db.connect(tmp_pod_db) as conn:
        pod_id = pod_db.insert_pending(
            conn,
            sku="CERMUG-01",
            design_url="https://example.com/wrap.png",
            decoration_title="Printing Front Side",
            ship_to=ship_to,
            buffer_minutes=45,
        )
        row = pod_db.get_by_id(conn, pod_id)
        # submit_after > created_at by ~45 minutes (just check it's strictly greater)
        assert row["submit_after"] > row["created_at"]


def test_insert_pending_no_buffer_submits_immediately(tmp_pod_db, ship_to):
    with pod_db.connect(tmp_pod_db) as conn:
        pod_id = pod_db.insert_pending(
            conn,
            sku="CERMUG-01",
            design_url="https://example.com/wrap.png",
            decoration_title="Printing Front Side",
            ship_to=ship_to,
            buffer_minutes=0,
        )
        row = pod_db.get_by_id(conn, pod_id)
        # With buffer=0, submit_after == created_at so due_for_submission picks it up
        assert row["submit_after"] == row["created_at"]


# --------------------------------------------------------------------------- #
# state transitions
# --------------------------------------------------------------------------- #

def test_mark_submitted_transitions_status_and_saves_215_assets(tmp_pod_db, ship_to):
    with pod_db.connect(tmp_pod_db) as conn:
        pod_id = pod_db.insert_pending(
            conn, sku="CERMUG-01",
            design_url="https://example.com/wrap.png",
            decoration_title="Printing Front Side",
            ship_to=ship_to,
        )
        pod_db.mark_submitted(
            conn, pod_id,
            twofifteen_order_id="999001",
            twofifteen_status="Received",
            design_url_215="https://www.twofifteen.co.uk/images/pictures/artists/custom_user_x/wrap.png",
            mockup_url_215="https://www.twofifteen.co.uk/images/pictures/artists/custom_user_x/wrap-123.png",
            create_response={"order": {"id": "999001", "status": "Received"}},
        )
        row = pod_db.get_by_id(conn, pod_id)
        assert row["status"] == pod_db.STATUS_SUBMITTED
        assert row["twofifteen_order_id"] == "999001"
        assert row["twofifteen_status"] == "Received"
        assert row["design_url_215"].endswith("/wrap.png")
        assert row["mockup_url_215"].endswith("/wrap-123.png")
        assert row["submitted_at"] is not None
        assert row["last_error"] is None


def test_mark_shipped_sets_tracking(tmp_pod_db, ship_to):
    with pod_db.connect(tmp_pod_db) as conn:
        pod_id = pod_db.insert_pending(
            conn, sku="CERMUG-01",
            design_url="https://example.com/wrap.png",
            decoration_title="Printing Front Side",
            ship_to=ship_to,
        )
        pod_db.mark_submitted(
            conn, pod_id,
            twofifteen_order_id="999002",
            twofifteen_status="Received",
            design_url_215=None, mockup_url_215=None,
            create_response={},
        )
        pod_db.mark_shipped(
            conn, pod_id,
            tracking_number="AB123456789GB",
            tracking_carrier="Royal Mail",
        )
        row = pod_db.get_by_id(conn, pod_id)
        assert row["status"] == pod_db.STATUS_SHIPPED
        assert row["tracking_number"] == "AB123456789GB"
        assert row["tracking_carrier"] == "Royal Mail"
        assert row["shipped_at"] is not None


def test_mark_synced_and_mark_cancelled(tmp_pod_db, ship_to):
    with pod_db.connect(tmp_pod_db) as conn:
        pod_id = pod_db.insert_pending(
            conn, sku="CERMUG-01",
            design_url="https://example.com/wrap.png",
            decoration_title="Printing Front Side",
            ship_to=ship_to,
        )
        pod_db.mark_synced(conn, pod_id)
        row = pod_db.get_by_id(conn, pod_id)
        assert row["status"] == pod_db.STATUS_SYNCED
        assert row["tracking_synced_at"] is not None

        # cancel is terminal — we don't care that we've hopped straight from synced
        pod_db.mark_cancelled(conn, pod_id)
        row = pod_db.get_by_id(conn, pod_id)
        assert row["status"] == pod_db.STATUS_CANCELLED
        assert row["cancelled_at"] is not None


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #

def test_record_error_non_fatal_increments_count_but_keeps_status(tmp_pod_db, ship_to):
    with pod_db.connect(tmp_pod_db) as conn:
        pod_id = pod_db.insert_pending(
            conn, sku="CERMUG-01",
            design_url="https://example.com/wrap.png",
            decoration_title="Printing Front Side",
            ship_to=ship_to,
        )
        pod_db.record_error(conn, pod_id, "temporary fail", fatal=False)
        pod_db.record_error(conn, pod_id, "still failing", fatal=False)
        row = pod_db.get_by_id(conn, pod_id)
        assert row["error_count"] == 2
        assert row["last_error"] == "still failing"
        assert row["status"] == pod_db.STATUS_PENDING


def test_record_error_fatal_transitions_to_failed(tmp_pod_db, ship_to):
    with pod_db.connect(tmp_pod_db) as conn:
        pod_id = pod_db.insert_pending(
            conn, sku="CERMUG-01",
            design_url="https://example.com/wrap.png",
            decoration_title="Printing Front Side",
            ship_to=ship_to,
        )
        pod_db.record_error(conn, pod_id, "boom", fatal=True)
        row = pod_db.get_by_id(conn, pod_id)
        assert row["status"] == pod_db.STATUS_FAILED
        assert row["last_error"] == "boom"
        assert row["error_count"] == 1


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #

def test_iter_by_status_and_count_by_status(tmp_pod_db, ship_to):
    with pod_db.connect(tmp_pod_db) as conn:
        for _ in range(3):
            pod_db.insert_pending(
                conn, sku="CERMUG-01",
                design_url="https://example.com/wrap.png",
                decoration_title="Printing Front Side",
                ship_to=ship_to,
            )
        # submit one of them
        pod_db.mark_submitted(
            conn, 1,
            twofifteen_order_id="900001",
            twofifteen_status="Received",
            design_url_215=None, mockup_url_215=None,
            create_response={},
        )

        counts = pod_db.count_by_status(conn)
        assert counts.get(pod_db.STATUS_PENDING) == 2
        assert counts.get(pod_db.STATUS_SUBMITTED) == 1

        pending = list(pod_db.iter_by_status(conn, pod_db.STATUS_PENDING))
        assert len(pending) == 2
        assert all(r["status"] == pod_db.STATUS_PENDING for r in pending)


def test_due_for_submission_respects_buffer(tmp_pod_db, ship_to):
    with pod_db.connect(tmp_pod_db) as conn:
        # ready now
        pod_db.insert_pending(
            conn, sku="CERMUG-01",
            design_url="https://example.com/wrap.png",
            decoration_title="Printing Front Side",
            ship_to=ship_to,
            buffer_minutes=0,
        )
        # buffered for an hour — not yet due
        pod_db.insert_pending(
            conn, sku="CERMUG-01",
            design_url="https://example.com/wrap.png",
            decoration_title="Printing Front Side",
            ship_to=ship_to,
            buffer_minutes=60,
        )
        due = list(pod_db.due_for_submission(conn))
        assert len(due) == 1
        assert due[0]["id"] == 1


def test_get_by_twofifteen_id_and_external_id(tmp_pod_db, ship_to):
    with pod_db.connect(tmp_pod_db) as conn:
        pod_id = pod_db.insert_pending(
            conn, sku="CERMUG-01",
            design_url="https://example.com/wrap.png",
            decoration_title="Printing Front Side",
            ship_to=ship_to,
        )
        pod_db.mark_submitted(
            conn, pod_id,
            twofifteen_order_id="abc-777",
            twofifteen_status="Received",
            design_url_215=None, mockup_url_215=None,
            create_response={},
        )
        row_by_215 = pod_db.get_by_twofifteen_id(conn, "abc-777")
        assert row_by_215 is not None
        assert row_by_215["id"] == pod_id

        row_by_ext = pod_db.get_by_external_id(conn, f"klh-pod-{pod_id}")
        assert row_by_ext is not None
        assert row_by_ext["id"] == pod_id
