"""
Pure-function audit rules.

Each rule takes a single cached listing row (a dict, shaped like
`pipeline.audit_db.row_to_dict()`'s output) and returns a list of
`Flag`s. Rules are stateless and self-contained so they can be tested
offline with inline fixtures — see tests/test_audit_rules.py.

Rule naming:
    T0xx — title structure / typos
    T1xx — missing title keywords
    T2xx — title length
    S0xx — item specifics coverage
    D0xx — dead wood (age + engagement)

The `run_all()` helper runs every rule against a row and returns a
flat list of Flags, which audit_report.py then aggregates by code.

Severity:
    info     — informational only, not a defect
    warning  — should probably be fixed, safe to batch
    error    — definitely wrong

`suggested_fix` is a hint — it's a short human-readable phrase that
the report can surface. Actual fix application happens in Phase 2
(klh audit apply), which translates a code + row into an API call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional


# --------------------------------------------------------------------------- #
# Flag dataclass
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Flag:
    code: str
    severity: str        # "info" | "warning" | "error"
    message: str
    suggested_fix: Optional[str] = None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_SIZE_RE = re.compile(
    r"\b(?:A4|10\s*x\s*8|12\s*x\s*8|16\s*x\s*12|6\s*x\s*4|8\s*x\s*10)\b",
    re.IGNORECASE,
)


def _title(row: dict[str, Any]) -> str:
    return (row.get("title") or "")


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(v).astimezone(timezone.utc)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# T0xx — title structure / typos
# --------------------------------------------------------------------------- #

def rule_t001_double_space(row: dict[str, Any]) -> list[Flag]:
    title = _title(row)
    if "  " in title:
        return [Flag(
            code="T001_double_space",
            severity="warning",
            message="title contains double-space",
            suggested_fix=re.sub(r"\s+", " ", title).strip(),
        )]
    return []


def rule_t002_trim_whitespace(row: dict[str, Any]) -> list[Flag]:
    title = _title(row)
    if title != title.strip():
        return [Flag(
            code="T002_trim_whitespace",
            severity="warning",
            message="title has leading/trailing whitespace",
            suggested_fix=title.strip(),
        )]
    return []


def rule_t003_literal_underscore_fragment(row: dict[str, Any]) -> list[Flag]:
    """
    JSX quirk: the legacy Photoshop mockup scripts wrote filename stems
    that included `_Club` suffixes verbatim into the title. If a title
    contains `Word_Word` it's almost certainly the underscore showing
    through from the filename stem and needs a space.
    """
    title = _title(row)
    if re.search(r"[A-Za-z0-9]_[A-Za-z0-9]", title):
        return [Flag(
            code="T003_literal_underscore_fragment",
            severity="error",
            message="title contains a literal underscore between words (filename leak)",
            suggested_fix=re.sub(r"(?<=[A-Za-z0-9])_(?=[A-Za-z0-9])", " ", title),
        )]
    return []


def rule_t004_entity_or_mojibake(row: dict[str, Any]) -> list[Flag]:
    title = _title(row)
    if "&amp;" in title or "&quot;" in title or "&#" in title:
        return [Flag(
            code="T004_html_entity",
            severity="error",
            message="title contains a stray HTML entity",
        )]
    return []


def rule_t005_all_caps_word(row: dict[str, Any]) -> list[Flag]:
    """Flag SHOUTY all-caps words (usually SIGNED or PHOTO left from legacy)."""
    title = _title(row)
    shouty = [w for w in _WORD_RE.findall(title) if len(w) >= 4 and w.isupper()]
    if shouty:
        return [Flag(
            code="T005_allcaps_word",
            severity="info",
            message=f"all-caps word(s) in title: {', '.join(sorted(set(shouty)))}",
        )]
    return []


# --------------------------------------------------------------------------- #
# T1xx — missing title keywords
# --------------------------------------------------------------------------- #

def _title_lower(row: dict[str, Any]) -> str:
    return _title(row).lower()


def rule_t101_missing_signed(row: dict[str, Any]) -> list[Flag]:
    t = _title_lower(row)
    if "signed" not in t and "autograph" not in t:
        return [Flag(
            code="T101_missing_signed",
            severity="warning",
            message="title does not contain 'Signed' or 'Autograph'",
        )]
    return []


def rule_t102_missing_coa(row: dict[str, Any]) -> list[Flag]:
    t = _title_lower(row)
    if "coa" not in t and "certificate" not in t:
        return [Flag(
            code="T102_missing_coa",
            severity="info",
            message="title does not mention COA / Certificate",
        )]
    return []


def rule_t103_missing_size(row: dict[str, Any]) -> list[Flag]:
    title = _title(row)
    if not _SIZE_RE.search(title):
        return [Flag(
            code="T103_missing_size",
            severity="info",
            message="title does not mention a product size (A4/10x8/12x8/16x12/6x4)",
        )]
    return []


# --------------------------------------------------------------------------- #
# T2xx — title length
# --------------------------------------------------------------------------- #

def rule_t201_short_title(row: dict[str, Any]) -> list[Flag]:
    title = _title(row)
    n = len(title)
    if n == 0:
        return [Flag(
            code="T200_empty_title",
            severity="error",
            message="title is empty",
        )]
    if n <= 50:
        return [Flag(
            code="T201_short_title",
            severity="info",
            message=f"title is only {n}/80 chars — unused SEO space",
        )]
    return []


def rule_t202_long_title(row: dict[str, Any]) -> list[Flag]:
    title = _title(row)
    if len(title) > 80:
        return [Flag(
            code="T202_long_title",
            severity="error",
            message=f"title is {len(title)} chars (eBay cap is 80)",
        )]
    return []


# --------------------------------------------------------------------------- #
# S0xx — item specifics coverage (needs deep_fetched_at)
# --------------------------------------------------------------------------- #

def rule_s001_no_specifics(row: dict[str, Any]) -> list[Flag]:
    if row.get("deep_fetched_at") is None:
        return []  # not yet deep-fetched — can't say
    specifics = row.get("specifics") or {}
    if not specifics:
        return [Flag(
            code="S001_no_specifics",
            severity="warning",
            message="listing has no item specifics at all",
        )]
    return []


def rule_s002_missing_signed_specific(row: dict[str, Any]) -> list[Flag]:
    if row.get("deep_fetched_at") is None:
        return []
    specifics = row.get("specifics") or {}
    if specifics and not any(k.lower() == "signed" for k in specifics):
        return [Flag(
            code="S002_missing_signed",
            severity="warning",
            message="no 'Signed' item specific",
            suggested_fix="Signed=Yes",
        )]
    return []


def rule_s003_missing_authentication(row: dict[str, Any]) -> list[Flag]:
    if row.get("deep_fetched_at") is None:
        return []
    specifics = row.get("specifics") or {}
    if specifics and not any(
        "authentication" in k.lower() for k in specifics
    ):
        return [Flag(
            code="S003_missing_authentication",
            severity="info",
            message="no 'Autograph Authentication' item specific",
        )]
    return []


# --------------------------------------------------------------------------- #
# D0xx — dead wood
# --------------------------------------------------------------------------- #

def rule_d001_dead_wood(row: dict[str, Any], *, now: Optional[datetime] = None) -> list[Flag]:
    """
    Listings active > 2 years with zero watchers AND zero sales are
    dead wood — candidates to end & relist (or just end).
    """
    start = _parse_iso(row.get("start_time"))
    if start is None:
        return []
    now_utc = now or datetime.now(timezone.utc)
    age = now_utc - start
    if age < timedelta(days=2 * 365):
        return []
    watch = row.get("watch_count") or 0
    sold = row.get("quantity_sold") or 0
    if watch == 0 and sold == 0:
        days = age.days
        return [Flag(
            code="D001_dead_wood",
            severity="info",
            message=f"active for {days // 365}y {days % 365}d, 0 watchers, 0 sales",
            suggested_fix="review for end & relist",
        )]
    return []


def rule_d002_stale_1y_no_watchers(row: dict[str, Any], *, now: Optional[datetime] = None) -> list[Flag]:
    """Softer signal: 1y+ with 0 watchers (even if some sales happened)."""
    start = _parse_iso(row.get("start_time"))
    if start is None:
        return []
    now_utc = now or datetime.now(timezone.utc)
    age = now_utc - start
    if age < timedelta(days=365):
        return []
    watch = row.get("watch_count") or 0
    if watch == 0:
        return [Flag(
            code="D002_stale_1y_no_watchers",
            severity="info",
            message=f"{age.days} days old with 0 watchers",
        )]
    return []


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

ALL_RULES: tuple[Callable[[dict[str, Any]], list[Flag]], ...] = (
    rule_t001_double_space,
    rule_t002_trim_whitespace,
    rule_t003_literal_underscore_fragment,
    rule_t004_entity_or_mojibake,
    rule_t005_all_caps_word,
    rule_t101_missing_signed,
    rule_t102_missing_coa,
    rule_t103_missing_size,
    rule_t201_short_title,
    rule_t202_long_title,
    rule_s001_no_specifics,
    rule_s002_missing_signed_specific,
    rule_s003_missing_authentication,
    rule_d001_dead_wood,
    rule_d002_stale_1y_no_watchers,
)


def run_all(row: dict[str, Any]) -> list[Flag]:
    flags: list[Flag] = []
    for rule in ALL_RULES:
        flags.extend(rule(row))
    return flags
