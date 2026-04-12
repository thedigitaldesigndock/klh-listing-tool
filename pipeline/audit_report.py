"""
Aggregate audit_rules flags across the cached catalogue and render
chat-friendly text reports.

The CLI entry points in cli/audit_cmd.py call:
    build_catalogue_report(conn)   -> CatalogueReport
    build_signer_report(conn, name) -> SignerReport
    render_catalogue(report)       -> str
    render_signer(report)           -> str

Report dataclasses hold raw counts + example rows so a --json output
can dump the whole thing without re-running the rules.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional

from pipeline import audit_db, audit_rules


# --------------------------------------------------------------------------- #
# Signer name extraction
# --------------------------------------------------------------------------- #

# KLH titles almost all follow "<Name> Signed <Product> ...". The signer
# name is whatever precedes the first "Signed" / "Hand Signed" / "Auto..."
# token. This is a pragmatic heuristic that works for the vast majority
# of the catalogue but isn't bulletproof — the audit tool uses it only
# for aggregation (top signers, signer filter), never for writes.

_NAME_BOUNDARY_RE = re.compile(
    r"""
    ^(?P<name>.+?)                 # non-greedy prefix
    \s+                             # whitespace
    (?:
        Hand\s+Signed |             # "Hand Signed"
        Signed |                    # "Signed"
        Autograph(?:ed)? |          # "Autograph" / "Autographed"
        AUTOGRAPH
    )
    \b
    """,
    re.IGNORECASE | re.VERBOSE,
)

_PERSON_WORD_RE = re.compile(r"[A-Za-z][A-Za-z.\-']*")


def signer_from_title(title: str) -> Optional[str]:
    """
    Best-effort 'signer' extraction. Returns the name substring before
    the first 'Signed' / 'Hand Signed' / 'Autograph' token, trimmed.
    """
    if not title:
        return None
    m = _NAME_BOUNDARY_RE.match(title)
    if not m:
        return None
    name = m.group("name").strip()
    # Reject obviously wrong matches (e.g. the whole title matches
    # because "Signed" never appears). The regex `^(.+?)\s+Signed` with
    # re.match won't match if 'Signed' is absent, so we only land here
    # on a real boundary. Still, guard against bizarre titles.
    if not name or len(name) > 60:
        return None
    # Strip a leading "Lot 12:" or similar prefix that sellers sometimes use.
    if ":" in name:
        _, _, tail = name.partition(":")
        tail = tail.strip()
        if tail:
            name = tail
    return name


# --------------------------------------------------------------------------- #
# Report dataclasses
# --------------------------------------------------------------------------- #

@dataclass
class CatalogueReport:
    total: int
    deep_fetched: int
    categories: Counter = field(default_factory=Counter)
    flag_counts: Counter = field(default_factory=Counter)
    severity_counts: Counter = field(default_factory=Counter)
    title_length_stats: dict[str, float] = field(default_factory=dict)
    price_stats: dict[str, float] = field(default_factory=dict)
    top_signers: list[tuple[str, int]] = field(default_factory=list)
    signer_listing_totals: int = 0  # how many listings had a recoverable signer
    flag_examples: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    oldest_start: Optional[str] = None
    newest_start: Optional[str] = None
    generated_at: Optional[str] = None


@dataclass
class SignerReport:
    signer: str
    total: int
    listings: list[dict[str, Any]]
    title_variants: Counter
    flag_counts: Counter
    specifics_coverage: dict[str, Counter]    # key → Counter of values
    dead_wood_ids: list[str]


# --------------------------------------------------------------------------- #
# Catalogue aggregator
# --------------------------------------------------------------------------- #

_EXAMPLES_PER_CODE = 5


def build_catalogue_report(conn) -> CatalogueReport:
    report = CatalogueReport(total=0, deep_fetched=0)
    titles_len: list[int] = []
    prices: list[float] = []
    signer_counter: Counter = Counter()
    starts: list[str] = []

    for row in audit_db.iter_rows(conn):
        d = audit_db.row_to_dict(row)
        report.total += 1
        if d.get("deep_fetched_at"):
            report.deep_fetched += 1
        if d.get("category_name"):
            report.categories[d["category_name"]] += 1
        title = d.get("title") or ""
        if title:
            titles_len.append(len(title))
        price = d.get("price_gbp")
        if price is not None:
            prices.append(float(price))
        start = d.get("start_time")
        if start:
            starts.append(start)

        signer = signer_from_title(title)
        if signer:
            signer_counter[signer] += 1

        flags = audit_rules.run_all(d)
        for f in flags:
            report.flag_counts[f.code] += 1
            report.severity_counts[f.severity] += 1
            examples = report.flag_examples.setdefault(f.code, [])
            if len(examples) < _EXAMPLES_PER_CODE:
                examples.append({
                    "item_id": d.get("item_id"),
                    "title": title,
                    "view_item_url": d.get("view_item_url"),
                    "message": f.message,
                    "suggested_fix": f.suggested_fix,
                })

    if titles_len:
        report.title_length_stats = {
            "min": float(min(titles_len)),
            "max": float(max(titles_len)),
            "mean": float(statistics.mean(titles_len)),
            "median": float(statistics.median(titles_len)),
        }
    if prices:
        report.price_stats = {
            "min": float(min(prices)),
            "max": float(max(prices)),
            "mean": float(statistics.mean(prices)),
            "median": float(statistics.median(prices)),
        }
    report.signer_listing_totals = sum(signer_counter.values())
    report.top_signers = signer_counter.most_common(20)
    if starts:
        report.oldest_start = min(starts)
        report.newest_start = max(starts)
    from datetime import datetime, timezone
    report.generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return report


# --------------------------------------------------------------------------- #
# Signer aggregator
# --------------------------------------------------------------------------- #

def build_signer_report(conn, name: str) -> SignerReport:
    name_lower = name.lower()
    listings: list[dict[str, Any]] = []
    title_variants: Counter = Counter()
    flag_counts: Counter = Counter()
    specifics_coverage: dict[str, Counter] = defaultdict(Counter)
    dead_wood_ids: list[str] = []

    for row in audit_db.iter_rows(conn, title_prefix=name):
        d = audit_db.row_to_dict(row)
        title = d.get("title") or ""
        extracted = signer_from_title(title)
        if not extracted or extracted.lower() != name_lower:
            # Title matched the LIKE prefix but the extracted signer is a
            # different person (e.g. "Alan Hansen" vs "Alan Hansen Jr").
            continue
        listings.append(d)
        title_variants[title] += 1
        flags = audit_rules.run_all(d)
        for f in flags:
            flag_counts[f.code] += 1
            if f.code == "D001_dead_wood":
                dead_wood_ids.append(d.get("item_id"))
        if d.get("deep_fetched_at"):
            for k, v in (d.get("specifics") or {}).items():
                specifics_coverage[k][v] += 1

    return SignerReport(
        signer=name,
        total=len(listings),
        listings=listings,
        title_variants=title_variants,
        flag_counts=flag_counts,
        specifics_coverage=specifics_coverage,
        dead_wood_ids=dead_wood_ids,
    )


# --------------------------------------------------------------------------- #
# Renderers
# --------------------------------------------------------------------------- #

def _pct(n: int, total: int) -> str:
    if total == 0:
        return "0%"
    return f"{100.0 * n / total:.1f}%"


def render_catalogue(report: CatalogueReport, *, full: bool = False) -> str:
    lines: list[str] = []
    lines.append("# KLH catalogue audit")
    lines.append("")
    lines.append(f"Generated: {report.generated_at}")
    lines.append(f"Total listings cached: **{report.total:,}**")
    lines.append(
        f"Deep-fetched (item specifics available): {report.deep_fetched:,} "
        f"({_pct(report.deep_fetched, report.total)})"
    )
    if report.oldest_start:
        lines.append(f"Oldest start: {report.oldest_start[:10]}   Newest start: {report.newest_start[:10]}")
    lines.append("")

    # -- Titles ----------------------------------------------------------
    if report.title_length_stats:
        ts = report.title_length_stats
        lines.append("## Title length")
        lines.append(
            f"min {int(ts['min'])}  median {int(ts['median'])}  "
            f"mean {ts['mean']:.1f}  max {int(ts['max'])}  (cap 80)"
        )
        lines.append("")

    # -- Prices ----------------------------------------------------------
    if report.price_stats:
        ps = report.price_stats
        lines.append("## Prices (GBP)")
        lines.append(
            f"min £{ps['min']:.2f}  median £{ps['median']:.2f}  "
            f"mean £{ps['mean']:.2f}  max £{ps['max']:.2f}"
        )
        lines.append("")

    # -- Categories ------------------------------------------------------
    if report.categories:
        lines.append("## Top categories")
        for cat, n in report.categories.most_common(10):
            lines.append(f"- {n:>5,}  {cat}")
        lines.append("")

    # -- Flags -----------------------------------------------------------
    if report.flag_counts:
        lines.append("## Flags (count → code)")
        sev_order = {"error": 0, "warning": 1, "info": 2}
        code_sev: dict[str, str] = {}
        for code, examples in report.flag_examples.items():
            for ex in examples:
                pass  # severity is stored with the rule, recover from code
        # Use the first example to recover severity — but we didn't store it.
        # Instead, ask run_all to give us severity by re-deriving from code.
        # Simpler: group by code alphabetically, the codes are self-describing.
        for code, count in sorted(
            report.flag_counts.items(), key=lambda kv: -kv[1]
        ):
            lines.append(f"- {count:>5,}  {code}  ({_pct(count, report.total)})")
        lines.append("")
        lines.append(
            f"Severity totals: "
            f"error={report.severity_counts.get('error', 0):,}  "
            f"warning={report.severity_counts.get('warning', 0):,}  "
            f"info={report.severity_counts.get('info', 0):,}"
        )
        lines.append("")

    # -- Top signers -----------------------------------------------------
    if report.top_signers:
        lines.append("## Top signers by listing count")
        for name, count in report.top_signers:
            lines.append(f"- {count:>5,}  {name}")
        lines.append("")
        lines.append(
            f"Signer extraction covered {report.signer_listing_totals:,} listings "
            f"({_pct(report.signer_listing_totals, report.total)})"
        )
        lines.append("")

    # -- Examples --------------------------------------------------------
    if full and report.flag_examples:
        lines.append("## Flag examples (up to 5 each)")
        for code in sorted(report.flag_examples):
            examples = report.flag_examples[code]
            lines.append(f"### {code}")
            for ex in examples:
                lines.append(f"- `{ex['item_id']}`  {ex['title']!r}")
                if ex.get("suggested_fix"):
                    lines.append(f"    fix → {ex['suggested_fix']!r}")
            lines.append("")

    return "\n".join(lines)


def render_signer(report: SignerReport) -> str:
    lines: list[str] = []
    lines.append(f"# Signer audit: {report.signer}")
    lines.append(f"Total listings: **{report.total:,}**")
    lines.append("")

    if report.flag_counts:
        lines.append("## Flags")
        for code, count in sorted(report.flag_counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"- {count:>4,}  {code}")
        lines.append("")

    if report.dead_wood_ids:
        lines.append(f"## Dead wood candidates ({len(report.dead_wood_ids)})")
        for iid in report.dead_wood_ids[:20]:
            lines.append(f"- {iid}")
        if len(report.dead_wood_ids) > 20:
            lines.append(f"  … and {len(report.dead_wood_ids) - 20} more")
        lines.append("")

    if report.specifics_coverage:
        lines.append("## Item specifics distribution")
        for key in sorted(report.specifics_coverage):
            values = report.specifics_coverage[key]
            dist = ", ".join(f"{v}={n}" for v, n in values.most_common(5))
            lines.append(f"- {key}: {dist}")
        lines.append("")

    if report.title_variants:
        lines.append(f"## Title variants ({len(report.title_variants)} unique)")
        for title, count in report.title_variants.most_common(10):
            lines.append(f"- {count:>3,}  {title}")
        if len(report.title_variants) > 10:
            lines.append(f"  … and {len(report.title_variants) - 10} more unique titles")
        lines.append("")

    return "\n".join(lines)
