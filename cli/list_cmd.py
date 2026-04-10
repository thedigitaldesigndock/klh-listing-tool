"""
`klh verify / list / schedule / unlist` command handlers.

Kept in a separate module from cli/klh.py because the implementation is
substantial (listing dict build, picture upload, VerifyAddFixedPriceItem,
AddFixedPriceItem, EndFixedPriceItem) and the main cli module is
already crowded.

Safety model (see pipeline.lister for the underlying guards):

    klh verify     — dry-run via VerifyAddFixedPriceItem. Never
                     creates a live listing. Default behaviour.
    klh schedule   — real AddFixedPriceItem with ScheduleTime. Hidden
                     until the scheduled time. Requires --confirm.
    klh list       — real AddFixedPriceItem. Goes live immediately.
                     Requires --confirm.
    klh unlist     — EndFixedPriceItem. Requires --confirm.

Picture handling: each --picture argument is either an existing EPS
URL (starts with http) or a local file path that we upload to EPS via
UploadSiteHostedPictures. Upload failures abort before we attempt the
list call so you never get a half-uploaded listing.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from pipeline import lister, presets


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def _parse_specifics(pairs: list[str]) -> dict[str, str]:
    """--specific "Player=Alan Hansen" --specific "Team=Liverpool" → dict."""
    out: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"bad --specific {pair!r}: expected Name=Value")
        name, value = pair.split("=", 1)
        out[name.strip()] = value.strip()
    return out


def _resolve_pictures(raw: list[str], *, allow_upload: bool) -> list[str]:
    """
    Turn a mixed list of URLs + local file paths into a list of EPS URLs.

    URLs pass through unchanged. Local paths are uploaded via
    UploadSiteHostedPictures. If `allow_upload` is False we refuse local
    paths — useful when you want a fail-fast "every picture must already
    be hosted" mode.
    """
    resolved: list[str] = []
    for entry in raw:
        if entry.startswith(("http://", "https://")):
            resolved.append(entry)
            continue
        if not allow_upload:
            raise SystemExit(
                f"refusing to upload {entry!r} (--no-upload set); "
                "pass an https:// URL or drop --no-upload"
            )
        path = Path(entry)
        if not path.exists():
            raise SystemExit(f"picture not found: {path}")
        print(f"  uploading {path.name} ...", flush=True)
        url = lister.upload_site_hosted_picture(path)
        print(f"    → {url}")
        resolved.append(url)
    return resolved


def _build_listing_from_args(args, bundle: presets.PresetsBundle) -> dict:
    specifics = _parse_specifics(getattr(args, "specific", []) or [])
    return presets.build_listing(
        bundle,
        product_key=args.product,
        name=args.name,
        qualifier=args.qualifier,
        subject=args.subject,
        orientation=args.orientation,
        variant=args.variant,
        price_gbp=args.price,
        sku=args.sku,
        item_specifics=specifics,
    )


def _print_listing_summary(listing: dict, pictures: list[str]) -> None:
    print("──────── Listing ────────")
    print(f"  product      {listing['product_key']}")
    print(f"  template     {listing['template_id'] or '(plain photo)'}")
    print(f"  title        {listing['title']}  ({len(listing['title'])}/80)")
    print(f"  category     {listing['category_id']}")
    print(f"  price        £{listing['price_gbp']:.2f}")
    print(f"  pictures     {len(pictures)}")
    for i, u in enumerate(pictures, 1):
        print(f"                {i}. {u}")
    print(f"  specifics    {len(listing['item_specifics'])} keys")
    for k, v in sorted(listing["item_specifics"].items()):
        print(f"                {k}: {v}")
    print()


def _print_api_result(result: dict, *, what: str) -> None:
    print(f"──────── {what} ────────")
    print(f"  ack      {result.get('ack')}")
    if result.get("item_id"):
        print(f"  item_id  {result['item_id']}")
    if result.get("start_time"):
        print(f"  start    {result['start_time']}")
    if result.get("end_time"):
        print(f"  end      {result['end_time']}")
    if result.get("fees"):
        print(f"  fees:")
        for fee in result["fees"]:
            amt = fee.get("amount") or "?"
            if float(amt or 0) > 0:
                print(f"    {fee['name']:24s} {amt} {fee.get('currency') or ''}")
    if result.get("warnings"):
        print(f"  warnings:")
        for w in result["warnings"]:
            print(f"    [{w.get('code')}] {w.get('short')}")
            if w.get("long"):
                print(f"      {w['long']}")
    print()


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def cmd_verify(args) -> int:
    bundle = presets.load()
    listing = _build_listing_from_args(args, bundle)
    pictures = _resolve_pictures(
        args.picture or [],
        allow_upload=not args.no_upload,
    )
    _print_listing_summary(listing, pictures)
    if args.dump_xml:
        inner = lister.build_add_item_xml(listing, pictures)
        print("──────── XML (first 4000 chars) ────────")
        print(inner[:4000])
        print()
    print("[verify] calling VerifyAddFixedPriceItem ...")
    result = lister.verify_listing(listing, pictures)
    _print_api_result(result, what="Verify result")
    if args.json:
        print(json.dumps(result, indent=2))
    return 0


def cmd_schedule(args) -> int:
    bundle = presets.load()
    listing = _build_listing_from_args(args, bundle)
    pictures = _resolve_pictures(
        args.picture or [],
        allow_upload=not args.no_upload,
    )
    _print_listing_summary(listing, pictures)

    try:
        schedule_at = datetime.fromisoformat(args.at)
    except ValueError:
        raise SystemExit(f"bad --at {args.at!r}: must be ISO 8601 (YYYY-MM-DDTHH:MM:SS)")

    if not args.confirm:
        print("[schedule] DRY RUN — pass --confirm to actually schedule")
        print(f"         would schedule at: {schedule_at.isoformat()}")
        # Fall back to a verify so the user still sees fees/warnings
        result = lister.verify_listing(listing, pictures)
        _print_api_result(result, what="Verify result (no listing created)")
        return 0

    print(f"[schedule] creating scheduled listing for {schedule_at.isoformat()} ...")
    result = lister.schedule_listing(
        listing, pictures, schedule_at, confirm=True
    )
    _print_api_result(result, what="Scheduled listing created")
    if args.json:
        print(json.dumps(result, indent=2))
    return 0


def cmd_list(args) -> int:
    bundle = presets.load()
    listing = _build_listing_from_args(args, bundle)
    pictures = _resolve_pictures(
        args.picture or [],
        allow_upload=not args.no_upload,
    )
    _print_listing_summary(listing, pictures)

    if not args.confirm:
        print("[list] DRY RUN — pass --confirm to go live NOW")
        result = lister.verify_listing(listing, pictures)
        _print_api_result(result, what="Verify result (no listing created)")
        return 0

    print("[list] calling AddFixedPriceItem — this creates a LIVE listing")
    result = lister.submit_listing(listing, pictures, confirm=True)
    _print_api_result(result, what="Live listing created")
    if args.json:
        print(json.dumps(result, indent=2))
    return 0


def cmd_unlist(args) -> int:
    if not args.confirm:
        print(f"[unlist] DRY RUN — pass --confirm to actually end {args.item_id}")
        return 0
    print(f"[unlist] ending item {args.item_id} (reason={args.reason}) ...")
    result = lister.end_listing(
        args.item_id, reason=args.reason, confirm=True
    )
    print(f"  ack      {result.get('ack')}")
    print(f"  end      {result.get('end_time')}")
    return 0


# --------------------------------------------------------------------------- #
# Out-of-stock control commands
# --------------------------------------------------------------------------- #

def cmd_preferences(args) -> int:
    """`klh preferences out-of-stock-control --enable/--disable/--status`."""
    if args.topic != "out-of-stock-control":
        raise SystemExit(f"unknown preferences topic: {args.topic!r}")

    if args.status or (not args.enable and not args.disable):
        current = lister.get_out_of_stock_control()
        print(f"OutOfStockControl: {'ENABLED' if current else 'disabled'}")
        return 0

    if args.enable and args.disable:
        raise SystemExit("cannot pass both --enable and --disable")

    enable = bool(args.enable)
    verb = "enabling" if enable else "disabling"
    print(f"[preferences] {verb} OutOfStockControl ...")
    result = lister.set_out_of_stock_control(enable)
    print(f"  ack      {result.get('ack')}")
    print(f"  enabled  {result.get('enabled')}")
    return 0


def cmd_outofstock(args) -> int:
    """`klh outofstock <item_id>` — set Quantity to 0 (listing stays active)."""
    if not args.confirm:
        print(f"[outofstock] DRY RUN — pass --confirm to zero out {args.item_id}")
        return 0
    print(f"[outofstock] setting Quantity=0 on {args.item_id} ...")
    result = lister.set_item_quantity(args.item_id, 0)
    print(f"  ack       {result.get('ack')}")
    print(f"  quantity  {result.get('quantity')}")
    return 0


def cmd_restock(args) -> int:
    """`klh restock <item_id> [--qty N]` — revive an out-of-stock listing."""
    if not args.confirm:
        print(f"[restock] DRY RUN — pass --confirm to set {args.item_id} to qty {args.qty}")
        return 0
    print(f"[restock] setting Quantity={args.qty} on {args.item_id} ...")
    result = lister.set_item_quantity(args.item_id, int(args.qty))
    print(f"  ack       {result.get('ack')}")
    print(f"  quantity  {result.get('quantity')}")
    return 0


# --------------------------------------------------------------------------- #
# Parser wiring (called from cli/klh.py)
# --------------------------------------------------------------------------- #

def add_listing_flags(parser):
    """Flags shared by verify / schedule / list."""
    parser.add_argument("--product", required=True,
                        help="product key (e.g. a4_photo, 16x12_mount)")
    parser.add_argument("--name", required=True,
                        help="signer name, e.g. 'Alan Hansen'")
    parser.add_argument("--qualifier", default=None,
                        help="optional trailing phrase, e.g. 'Liverpool'")
    parser.add_argument("--subject", default="default",
                        help="category subject key (football_retired, music_pop, …)")
    parser.add_argument("--orientation", default=None,
                        choices=["landscape", "portrait"],
                        help="for 10x8 mount/frame templates")
    parser.add_argument("--variant", default=None,
                        help="explicit template variant (e.g. 16x12-c-mount)")
    parser.add_argument("--price", type=float, default=None,
                        help="override default price")
    parser.add_argument("--sku", default=None)
    parser.add_argument("--specific", action="append", default=[],
                        metavar="Name=Value",
                        help="item specific (repeatable)")
    parser.add_argument("--picture", action="append", default=[],
                        metavar="PATH_OR_URL",
                        help="picture path or existing EPS URL (repeatable)")
    parser.add_argument("--no-upload", action="store_true",
                        help="refuse to upload local paths (URL-only)")
    parser.add_argument("--json", action="store_true",
                        help="print API response as JSON")
    parser.add_argument("--dump-xml", action="store_true",
                        help="print the first 4000 chars of the request XML")


def register(sub):
    """Install verify/schedule/list/unlist subparsers on `sub`."""
    p_verify = sub.add_parser(
        "verify",
        help="dry-run a listing (VerifyAddFixedPriceItem; nothing goes live)",
    )
    add_listing_flags(p_verify)
    p_verify.set_defaults(func=lambda a: sys.exit(cmd_verify(a)))

    p_sched = sub.add_parser(
        "schedule",
        help="create a scheduled listing (hidden until --at)",
    )
    add_listing_flags(p_sched)
    p_sched.add_argument("--at", required=True,
                         metavar="ISO_8601",
                         help="schedule time, e.g. 2026-04-12T20:00:00")
    p_sched.add_argument("--confirm", action="store_true",
                         help="actually schedule — without this it's a verify")
    p_sched.set_defaults(func=lambda a: sys.exit(cmd_schedule(a)))

    p_list = sub.add_parser(
        "list",
        help="create a live listing NOW (AddFixedPriceItem)",
    )
    add_listing_flags(p_list)
    p_list.add_argument("--confirm", action="store_true",
                        help="actually go live — without this it's a verify")
    p_list.set_defaults(func=lambda a: sys.exit(cmd_list(a)))

    p_unlist = sub.add_parser(
        "unlist",
        help="end an active listing (EndFixedPriceItem)",
    )
    p_unlist.add_argument("item_id")
    p_unlist.add_argument("--reason", default="NotAvailable",
                          help="Incorrect|LostOrBroken|NotAvailable|"
                               "OtherListingError|Sold (default NotAvailable)")
    p_unlist.add_argument("--confirm", action="store_true",
                          help="actually end the listing")
    p_unlist.set_defaults(func=lambda a: sys.exit(cmd_unlist(a)))

    # ── OutOfStockControl preferences & stock toggles ──────────────────
    p_prefs = sub.add_parser(
        "preferences",
        help="manage seller account preferences (OutOfStockControl, …)",
    )
    p_prefs.add_argument("topic",
                         choices=["out-of-stock-control"],
                         help="which preference to read/modify")
    p_prefs.add_argument("--enable", action="store_true",
                         help="turn the preference ON")
    p_prefs.add_argument("--disable", action="store_true",
                         help="turn the preference OFF")
    p_prefs.add_argument("--status", action="store_true",
                         help="just show current value (default if no flag)")
    p_prefs.set_defaults(func=lambda a: sys.exit(cmd_preferences(a)))

    p_oos = sub.add_parser(
        "outofstock",
        help="set Quantity=0 on a listing (stays ACTIVE but hidden; "
             "requires OutOfStockControl enabled)",
    )
    p_oos.add_argument("item_id")
    p_oos.add_argument("--confirm", action="store_true",
                       help="actually zero the stock")
    p_oos.set_defaults(func=lambda a: sys.exit(cmd_outofstock(a)))

    p_restock = sub.add_parser(
        "restock",
        help="set Quantity>0 on an out-of-stock listing (revives it "
             "in search results)",
    )
    p_restock.add_argument("item_id")
    p_restock.add_argument("--qty", type=int, default=1,
                           help="new quantity (default 1)")
    p_restock.add_argument("--confirm", action="store_true",
                           help="actually set the new quantity")
    p_restock.set_defaults(func=lambda a: sys.exit(cmd_restock(a)))
