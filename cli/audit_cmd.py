"""
`klh audit` command family — read-only catalogue inspection.

Phase 1 subcommands (all read from ~/.klh/audit.db; only `fetch` and
`peek` hit the eBay API, and both are read-only):

    klh audit fetch    — populate/refresh the local cache via
                         GetMyeBaySelling (summary) or GetItem (deep).
    klh audit report   — catalogue-wide "easy wins" summary.
    klh audit signer   — per-signer drill-down.
    klh audit peek     — raw GetItem dump of a single item for debugging.

Phase 2 (`klh audit apply`) is not part of this module — it will live
next to the Revise helpers in pipeline/lister.py and will only land
after Phase 1 has produced a real report we've reviewed together.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Optional

from ebay_api import trading
from pipeline import audit_db, audit_report, audit_rules, lister


# --------------------------------------------------------------------------- #
# fetch
# --------------------------------------------------------------------------- #

def cmd_fetch(args) -> int:
    if args.deep and args.summary_only:
        raise SystemExit("--deep and --summary-only are mutually exclusive")

    with audit_db.connect() as conn:
        existing = audit_db.count_rows(conn)
        if args.dry_run:
            print(f"[audit fetch] dry-run: {existing:,} rows in cache")
            if not args.deep:
                print("           would call GetMyeBaySelling paginated")
            else:
                print("           would call GetItem per row needing deep fetch")
            return 0

        if not args.deep:
            _run_summary_fetch(conn, args)
        else:
            _run_deep_fetch(conn, args)

        final = audit_db.count_rows(conn)
        print(f"[audit fetch] done: {final:,} rows in cache")
        return 0


def _run_summary_fetch(conn, args) -> None:
    start = time.monotonic()
    fetched = 0
    limit = args.limit

    def progress(page, total_pages, total_entries):
        print(
            f"  page {page}/{total_pages}  "
            f"(~{total_entries:,} total listings, {fetched:,} fetched so far)",
            flush=True,
        )

    print("[audit fetch] sweeping active listings via GetMyeBaySelling ...")
    try:
        for row in trading.iter_active_items_summary(
            page_size=args.page_size,
            progress=progress,
        ):
            audit_db.upsert_summary(conn, row)
            fetched += 1
            if fetched % 500 == 0:
                conn.commit()
                elapsed = time.monotonic() - start
                print(f"  committed {fetched:,} rows  ({elapsed:.1f}s elapsed)")
            if limit and fetched >= limit:
                print(f"  --limit {limit} reached — stopping early")
                break
    except trading.TradingError as e:
        print(f"[audit fetch] ERROR from eBay: {e}")
        return

    conn.commit()
    audit_db.set_meta(conn, "last_summary_fetch", audit_db._now_iso())
    elapsed = time.monotonic() - start
    print(f"[audit fetch] summary pass complete: {fetched:,} rows in {elapsed:.1f}s")


def _run_deep_fetch(conn, args) -> None:
    """
    Per-item GetItem sweep to populate specifics_json. Resumable:
    skips rows where deep_fetched_at is already set.
    """
    sql = "SELECT item_id FROM listings"
    params: list = []
    if not args.force:
        sql += " WHERE deep_fetched_at IS NULL"
    sql += " ORDER BY item_id"
    if args.limit:
        sql += " LIMIT ?"
        params.append(int(args.limit))

    candidates = [r["item_id"] for r in conn.execute(sql, params)]
    if not candidates:
        print("[audit fetch] deep: nothing to fetch (all rows already deep-fetched)")
        return

    print(f"[audit fetch] deep: {len(candidates):,} items to fetch "
          f"(rate={args.rate}/s)")
    sleep = 1.0 / max(args.rate, 0.1)
    fetched = 0
    start = time.monotonic()

    def progress(item_id, deep, err):
        nonlocal fetched
        if err:
            print(f"  ! {item_id}  {err[:120]}")
            return
        fetched += 1
        if fetched % 50 == 0:
            elapsed = time.monotonic() - start
            per_s = fetched / elapsed if elapsed else 0
            remaining = len(candidates) - fetched
            eta = remaining / per_s if per_s else 0
            print(
                f"  {fetched:,}/{len(candidates):,}  "
                f"({per_s:.1f}/s, ETA {eta/60:.1f}m)"
            )

    for item_id, deep in trading.get_items_bulk(
        candidates, sleep=sleep, progress=progress
    ):
        audit_db.upsert_deep(conn, item_id, deep)
        if fetched % 100 == 0:
            conn.commit()

    conn.commit()
    audit_db.set_meta(conn, "last_deep_fetch", audit_db._now_iso())
    print(f"[audit fetch] deep pass complete: {fetched:,} rows")


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #

def cmd_report(args) -> int:
    with audit_db.connect(readonly=True) as conn:
        total = audit_db.count_rows(conn)
        if total == 0:
            print("[audit report] cache is empty — run `klh audit fetch` first")
            return 1
        report = audit_report.build_catalogue_report(conn)

    if args.json:
        # Render a JSON-safe version.
        payload = {
            "generated_at": report.generated_at,
            "total": report.total,
            "deep_fetched": report.deep_fetched,
            "oldest_start": report.oldest_start,
            "newest_start": report.newest_start,
            "title_length_stats": report.title_length_stats,
            "price_stats": report.price_stats,
            "categories": dict(report.categories.most_common()),
            "flag_counts": dict(report.flag_counts),
            "severity_counts": dict(report.severity_counts),
            "top_signers": report.top_signers,
            "flag_examples": report.flag_examples,
        }
        print(json.dumps(payload, indent=2))
        return 0

    text = audit_report.render_catalogue(report, full=args.full)
    print(text)
    return 0


# --------------------------------------------------------------------------- #
# signer
# --------------------------------------------------------------------------- #

def cmd_signer(args) -> int:
    with audit_db.connect(readonly=True) as conn:
        report = audit_report.build_signer_report(conn, args.name)
        if report.total == 0:
            print(f"[audit signer] no listings found with title prefix {args.name!r}")
            return 1

    if args.json:
        payload = {
            "signer": report.signer,
            "total": report.total,
            "flag_counts": dict(report.flag_counts),
            "dead_wood_ids": report.dead_wood_ids,
            "title_variants": report.title_variants.most_common(),
            "specifics_coverage": {
                k: dict(v) for k, v in report.specifics_coverage.items()
            },
            "listings": [
                {
                    "item_id": r.get("item_id"),
                    "title": r.get("title"),
                    "price_gbp": r.get("price_gbp"),
                    "start_time": r.get("start_time"),
                    "watch_count": r.get("watch_count"),
                    "view_item_url": r.get("view_item_url"),
                }
                for r in report.listings
            ],
        }
        print(json.dumps(payload, indent=2))
        return 0

    print(audit_report.render_signer(report))
    return 0


# --------------------------------------------------------------------------- #
# apply — the Phase 2 write path
# --------------------------------------------------------------------------- #

# Which rule codes produce an auto-suggested title fix. If a rule's
# `suggested_fix` is a full replacement title, it can be applied
# mechanically. Rules without a deterministic fix (T005 all-caps,
# T102/T103 missing keyword) need a manual decision per listing and
# are not in this set.
_AUTO_TITLE_RULES = {
    "T001_double_space",
    "T002_trim_whitespace",
    "T003_literal_underscore_fragment",
}


def _diff_title(old: str, new: str) -> str:
    return f"-  {old!r}\n+  {new!r}"


def _collect_title_proposals(conn, *, rule_code: str, limit: Optional[int]):
    """
    Walk the cache and return a list of (row_dict, new_title) tuples
    for every listing flagged by `rule_code` where the rule provides a
    deterministic suggested_fix.
    """
    proposals: list[tuple[dict, str]] = []
    for row in audit_db.iter_rows(conn):
        d = audit_db.row_to_dict(row)
        flags = audit_rules.run_all(d)
        for f in flags:
            if f.code != rule_code:
                continue
            if not f.suggested_fix:
                continue
            if f.suggested_fix == d.get("title"):
                continue  # already correct somehow
            proposals.append((d, f.suggested_fix))
            break
        if limit and len(proposals) >= limit:
            break
    return proposals


def cmd_apply(args) -> int:
    if args.rule not in _AUTO_TITLE_RULES:
        print(
            f"[audit apply] rule {args.rule!r} has no auto-applicable fix.\n"
            f"              supported rules: {', '.join(sorted(_AUTO_TITLE_RULES))}"
        )
        return 2

    # Dry-run uses a read-only connection so it never contends with a
    # running `klh audit fetch --deep` writer in another process.
    with audit_db.connect(readonly=not args.confirm) as conn:
        proposals = _collect_title_proposals(
            conn, rule_code=args.rule, limit=args.limit
        )
        if not proposals:
            print(f"[audit apply] no listings match rule {args.rule!r}")
            return 0

        print(
            f"[audit apply] rule={args.rule}  proposals={len(proposals)}  "
            f"{'(DRY RUN)' if not args.confirm else '(LIVE — will hit eBay)'}"
        )
        print()

        # Validate every new title up-front so we fail before touching
        # the API if any proposed fix would exceed 80 chars.
        for d, new_title in proposals:
            if len(new_title) > lister.MAX_TITLE_LEN:
                print(
                    f"[audit apply] ABORT: proposed title for {d['item_id']} "
                    f"is {len(new_title)} chars (>{lister.MAX_TITLE_LEN})"
                )
                return 2

        # Print every diff we're about to make.
        for i, (d, new_title) in enumerate(proposals, 1):
            print(f"──── {i}/{len(proposals)}  item {d['item_id']}  ({d.get('view_item_url') or ''})")
            print(_diff_title(d.get("title") or "", new_title))
            print()

        if not args.confirm:
            print(
                f"[audit apply] DRY RUN complete — pass --confirm to apply "
                f"all {len(proposals)} fixes via ReviseFixedPriceItem."
            )
            return 0

        # LIVE — rate-limited revise loop.
        import time
        sleep = 1.0 / max(args.rate, 0.1)
        ok = 0
        failed = 0
        for i, (d, new_title) in enumerate(proposals, 1):
            item_id = d["item_id"]
            try:
                result = lister.revise_listing(
                    item_id, new_title=new_title, confirm=True,
                )
            except Exception as e:
                failed += 1
                print(f"  [{i}/{len(proposals)}] {item_id}  FAIL: {e}")
                continue
            ok += 1
            ack = result.get("ack", "?")
            warn = len(result.get("warnings") or [])
            warn_s = f"  ({warn} warnings)" if warn else ""
            print(f"  [{i}/{len(proposals)}] {item_id}  ack={ack}{warn_s}")
            # Update the cache row with the new title so a subsequent
            # report doesn't re-flag this listing.
            conn.execute(
                "UPDATE listings SET title = ?, fetched_at = ? WHERE item_id = ?",
                (new_title, audit_db._now_iso(), item_id),
            )
            conn.commit()
            if sleep > 0 and i < len(proposals):
                time.sleep(sleep)

        print()
        print(f"[audit apply] done: {ok} succeeded, {failed} failed")
        return 0 if failed == 0 else 1


# --------------------------------------------------------------------------- #
# peek
# --------------------------------------------------------------------------- #

def cmd_peek(args) -> int:
    try:
        item = trading.get_item(args.item_id)
    except trading.TradingError as e:
        print(f"[audit peek] ERROR: {e}")
        return 1
    print(json.dumps(item, indent=2, default=str))
    return 0


# --------------------------------------------------------------------------- #
# parser wiring
# --------------------------------------------------------------------------- #

def register(sub):
    """Install the `klh audit ...` subparsers."""
    p_audit = sub.add_parser(
        "audit",
        help="inspect existing KLHAutographs listings (read-only)",
    )
    audit_sub = p_audit.add_subparsers(dest="audit_cmd", required=True)

    # fetch --------------------------------------------------------------
    p_fetch = audit_sub.add_parser(
        "fetch",
        help="populate ~/.klh/audit.db from eBay (summary + optional deep)",
    )
    p_fetch.add_argument(
        "--deep", action="store_true",
        help="per-item GetItem pass for item specifics "
             "(slow — ~1/s rate limited by default)",
    )
    p_fetch.add_argument(
        "--summary-only", action="store_true",
        help="(default) GetMyeBaySelling only — no per-item calls",
    )
    p_fetch.add_argument(
        "--page-size", type=int, default=200,
        help="entries per GetMyeBaySelling page (max 200, default 200)",
    )
    p_fetch.add_argument(
        "--limit", type=int, default=None,
        help="cap number of listings fetched this run (testing)",
    )
    p_fetch.add_argument(
        "--rate", type=float, default=2.0,
        help="deep-fetch rate (items/sec), default 2.0",
    )
    p_fetch.add_argument(
        "--force", action="store_true",
        help="deep-refetch rows that already have specifics",
    )
    p_fetch.add_argument(
        "--dry-run", action="store_true",
        help="print the plan without calling the API",
    )
    p_fetch.set_defaults(func=lambda a: sys.exit(cmd_fetch(a)))

    # report -------------------------------------------------------------
    p_report = audit_sub.add_parser(
        "report",
        help="catalogue-wide easy-wins summary from the local cache",
    )
    p_report.add_argument(
        "--full", action="store_true",
        help="include example rows for each flag code",
    )
    p_report.add_argument(
        "--json", action="store_true",
        help="print the report as JSON instead of text",
    )
    p_report.set_defaults(func=lambda a: sys.exit(cmd_report(a)))

    # signer -------------------------------------------------------------
    p_signer = audit_sub.add_parser(
        "signer",
        help="per-signer drill-down from the local cache",
    )
    p_signer.add_argument("name", help="signer name prefix, e.g. \"Paul Scholes\"")
    p_signer.add_argument(
        "--json", action="store_true",
        help="JSON output (feeds Phase 2 apply step)",
    )
    p_signer.set_defaults(func=lambda a: sys.exit(cmd_signer(a)))

    # apply --------------------------------------------------------------
    p_apply = audit_sub.add_parser(
        "apply",
        help="apply a deterministic rule's fix to matching listings "
             "(ReviseFixedPriceItem). Default is DRY RUN.",
    )
    p_apply.add_argument(
        "--rule", required=True,
        help="rule code to apply (e.g. T001_double_space, T002_trim_whitespace, "
             "T003_literal_underscore_fragment)",
    )
    p_apply.add_argument(
        "--limit", type=int, default=None,
        help="cap the number of listings processed this run",
    )
    p_apply.add_argument(
        "--confirm", action="store_true",
        help="actually call ReviseFixedPriceItem — without this it's a dry-run",
    )
    p_apply.add_argument(
        "--rate", type=float, default=1.5,
        help="max revise calls per second (default 1.5)",
    )
    p_apply.set_defaults(func=lambda a: sys.exit(cmd_apply(a)))

    # peek ---------------------------------------------------------------
    p_peek = audit_sub.add_parser(
        "peek",
        help="raw GetItem dump of a single item (debug)",
    )
    p_peek.add_argument("item_id")
    p_peek.set_defaults(func=lambda a: sys.exit(cmd_peek(a)))
