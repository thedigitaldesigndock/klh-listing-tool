"""Unit tests for pipeline.audit_rules. Offline-only."""

from datetime import datetime, timedelta, timezone

from pipeline import audit_rules
from pipeline.audit_report import signer_from_title


def _row(**overrides):
    base = {
        "item_id": "123",
        "title": "Alan Hansen Signed A4 Photo Mount Display Liverpool Autograph Memorabilia +COA",
        "price_gbp": 39.99,
        "watch_count": 1,
        "quantity_sold": 0,
        "start_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "specifics": {"Signed": "Yes", "Sport": "Football"},
        "deep_fetched_at": "2026-04-11T00:00:00Z",
    }
    base.update(overrides)
    return base


def test_clean_row_has_no_errors_or_warnings():
    flags = audit_rules.run_all(_row())
    for f in flags:
        assert f.severity != "error", f
        assert f.severity != "warning", f


def test_double_space_flagged():
    r = _row(title="Alan Hansen  Signed A4 Photo +COA")
    codes = {f.code for f in audit_rules.run_all(r)}
    assert "T001_double_space" in codes


def test_underscore_fragment_flagged():
    r = _row(title="Alan Hansen_Liverpool Signed A4 Photo +COA")
    codes = {f.code for f in audit_rules.run_all(r)}
    assert "T003_literal_underscore_fragment" in codes


def test_missing_signed_flagged():
    r = _row(title="Alan Hansen A4 Photo Mount Liverpool Memorabilia")
    codes = {f.code for f in audit_rules.run_all(r)}
    assert "T101_missing_signed" in codes


def test_long_title_flagged():
    r = _row(title="X" * 85)
    codes = {f.code for f in audit_rules.run_all(r)}
    assert "T202_long_title" in codes


def test_dead_wood_flagged():
    old = (datetime.now(timezone.utc) - timedelta(days=800)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = _row(start_time=old, watch_count=0, quantity_sold=0)
    codes = {f.code for f in audit_rules.run_all(r)}
    assert "D001_dead_wood" in codes


def test_no_specifics_flagged_only_when_deep_fetched():
    r = _row(specifics={}, deep_fetched_at=None)
    codes = {f.code for f in audit_rules.run_all(r)}
    assert "S001_no_specifics" not in codes  # not deep-fetched → can't say

    r2 = _row(specifics={}, deep_fetched_at="2026-04-11T00:00:00Z")
    codes2 = {f.code for f in audit_rules.run_all(r2)}
    assert "S001_no_specifics" in codes2


def test_signer_from_title():
    assert signer_from_title("Alan Hansen Signed A4 Photo +COA") == "Alan Hansen"
    assert signer_from_title("Paul Scholes Hand Signed Card +COA") == "Paul Scholes"
    assert signer_from_title("Some Person Autograph 10x8") == "Some Person"
    assert signer_from_title("") is None
    assert signer_from_title("no match here") is None
