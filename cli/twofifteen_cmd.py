"""
`klh twofifteen` command family — Two Fifteen POD integration.

Subcommands (alias: `klh 215 ...` also works):

    klh twofifteen ping         — auth self-check against 215
    klh twofifteen list         — list recent 215 orders (raw)
    klh twofifteen submit       — log + submit a new POD order
    klh twofifteen cancel       — cancel a logged POD order
    klh twofifteen show         — show one pod_db row
    klh twofifteen status       — count pod_db rows by status

Phase 10 scope. Higher-level flows (eBay → 215, webhook-driven tracking
sync) will add more subcommands as they land.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from twofifteen import TwoFifteenClient, TwoFifteenError, schema


def _client(verbose: bool = False) -> TwoFifteenClient:
    try:
        return TwoFifteenClient(verbose=verbose)
    except TwoFifteenError as e:
        print(f"twofifteen: {e}", file=sys.stderr)
        sys.exit(1)


# --------------------------------------------------------------------------- #
# ping
# --------------------------------------------------------------------------- #

def cmd_ping(args) -> int:
    client = _client(verbose=args.verbose)
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
        return 1

    orders = (
        result.get("orders", []) if isinstance(result, dict) else (result or [])
    )
    print(f"  Auth OK. Orders on account: {len(orders)}")
    return 0


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #

def cmd_list(args) -> int:
    client = _client(verbose=args.verbose)
    try:
        result = client.list_orders(limit=args.limit)
    except TwoFifteenError as e:
        print(f"twofifteen list: {e}", file=sys.stderr)
        return 1

    orders = (
        result.get("orders", []) if isinstance(result, dict) else (result or [])
    )
    if not orders:
        print("(no orders on account)")
        return 0

    for o in orders:
        print(
            f"  #{o.get('id'):<8}  {str(o.get('status')):<14}  "
            f"ext_id={str(o.get('external_id')):<24}  "
            f"created={o.get('created_at')}"
        )
    return 0


# --------------------------------------------------------------------------- #
# submit
# --------------------------------------------------------------------------- #

def _resolve_ship_to(args) -> dict:
    """Load the shipping address from --ship-to (JSON file) or --ship-to-preset."""
    if args.ship_to and args.ship_to_preset:
        raise SystemExit("--ship-to and --ship-to-preset are mutually exclusive")
    if args.ship_to:
        path = Path(args.ship_to)
        if not path.exists():
            raise SystemExit(f"--ship-to file not found: {path}")
        return json.loads(path.read_text())
    if args.ship_to_preset:
        from twofifteen import addresses
        return addresses.get(args.ship_to_preset)
    raise SystemExit("either --ship-to or --ship-to-preset is required")


def cmd_submit(args) -> int:
    from pipeline import pod_db
    from twofifteen import orders

    ship_to = _resolve_ship_to(args)

    # Dry-run: build the payload, print it, don't touch anything.
    if args.dry_run:
        payload = orders.build_mug_order(
            sku=args.sku,
            design_url=args.design_url,
            ship_to=ship_to,
            external_id="klh-pod-DRYRUN",
            quantity=args.quantity,
            title=args.title,
            buyer_email=args.buyer_email,
        )
        print(json.dumps(payload, indent=2))
        return 0

    client = _client(verbose=args.verbose)

    with pod_db.connect() as conn:
        try:
            result = orders.submit_mug_design(
                client,
                conn,
                sku=args.sku,
                design_url=args.design_url,
                ship_to=ship_to,
                listing_ref=args.listing_ref,
                title=args.title,
                quantity=args.quantity,
                buyer_email=args.buyer_email,
                buffer_minutes=args.buffer_min,
                submit_now=args.buffer_min == 0,
            )
        except TwoFifteenError as e:
            print(f"twofifteen submit: {e}", file=sys.stderr)
            return 1

    print(f"pod_id:              {result['pod_id']}")
    print(f"status:              {result['status']}")
    print(f"external_id:         {result.get('external_id')}")
    if result.get("twofifteen_order_id"):
        print(f"twofifteen_order_id: {result['twofifteen_order_id']}")
        print(f"twofifteen_status:   {result.get('twofifteen_status')}")
        print(f"design_url_215:      {result.get('design_url_215')}")
        print(f"mockup_url_215:      {result.get('mockup_url_215')}")
    else:
        print(f"(queued in buffer, not yet POSTed to 215)")
    return 0


# --------------------------------------------------------------------------- #
# cancel
# --------------------------------------------------------------------------- #

def cmd_cancel(args) -> int:
    from pipeline import pod_db
    from twofifteen import orders

    client = _client(verbose=args.verbose)
    with pod_db.connect() as conn:
        try:
            result = orders.cancel_pod_order(client, conn, args.id)
        except TwoFifteenError as e:
            print(f"twofifteen cancel: {e}", file=sys.stderr)
            return 1
    print(f"pod_id:     {result['pod_id']}")
    print(f"status:     {result['status']}")
    print(f"api_called: {result['api_called']}")
    return 0


# --------------------------------------------------------------------------- #
# show
# --------------------------------------------------------------------------- #

def cmd_show(args) -> int:
    from pipeline import pod_db

    with pod_db.connect(readonly=True) as conn:
        row = pod_db.get_by_id(conn, args.id)
        if row is None:
            print(f"pod order #{args.id} not found", file=sys.stderr)
            return 1
        d = pod_db.row_to_dict(row)

    # Hide noisy JSON blobs by default — they're in the DB if you need them.
    if not args.full:
        for key in ("ship_to_json", "create_response_json", "update_response_json"):
            d.pop(key, None)

    print(json.dumps(d, indent=2, default=str))
    return 0


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #

def cmd_status(args) -> int:
    from pipeline import pod_db

    with pod_db.connect(readonly=True) as conn:
        counts = pod_db.count_by_status(conn)

    if not counts:
        print("(pod.db empty — no POD orders logged yet)")
        return 0

    width = max(len(s) for s in counts)
    total = sum(counts.values())
    for status in sorted(counts):
        print(f"  {status:<{width}}  {counts[status]:>5}")
    print(f"  {'total':<{width}}  {total:>5}")
    return 0


# --------------------------------------------------------------------------- #
# register
# --------------------------------------------------------------------------- #

def register(sub) -> None:
    """Install the `klh twofifteen ...` subparsers (alias: `klh 215 ...`)."""
    p_tf = sub.add_parser(
        "twofifteen",
        aliases=["215"],
        help="Two Fifteen POD integration",
    )
    tf_sub = p_tf.add_subparsers(dest="twofifteen_cmd", required=True)

    # ping --------------------------------------------------------------
    p_ping = tf_sub.add_parser(
        "ping",
        help="auth self-check against twofifteen.co.uk",
    )
    p_ping.add_argument("-v", "--verbose", action="store_true",
                        help="print the signed request URL")
    p_ping.set_defaults(func=lambda a: sys.exit(cmd_ping(a)))

    # list --------------------------------------------------------------
    p_list = tf_sub.add_parser("list", help="list recent orders on the account")
    p_list.add_argument("--limit", type=int, default=10,
                        help="max orders to fetch (default 10, max 250)")
    p_list.add_argument("-v", "--verbose", action="store_true")
    p_list.set_defaults(func=lambda a: sys.exit(cmd_list(a)))

    # submit ------------------------------------------------------------
    p_sub = tf_sub.add_parser(
        "submit",
        help="log and submit a POD order to 215",
    )
    p_sub.add_argument("--sku", default=schema.SKU_CERAMIC_MUG_11OZ,
                       help=f"215 base product code (default {schema.SKU_CERAMIC_MUG_11OZ})")
    p_sub.add_argument("--design-url", required=True,
                       help="publicly fetchable URL of the flat print PNG")
    p_sub.add_argument("--ship-to", help="path to a JSON file with a camelCase "
                                         "shipping_address dict")
    p_sub.add_argument("--ship-to-preset",
                       help="named address preset (e.g. 'kim')")
    p_sub.add_argument("--listing-ref",
                       help="external reference (eBay item id, KLH listing id, etc.)")
    p_sub.add_argument("--title",
                       help="human label for the order item (shows on dashboard)")
    p_sub.add_argument("--quantity", type=int, default=1)
    p_sub.add_argument("--buyer-email", help="optional buyer email for the order")
    p_sub.add_argument("--buffer-min", type=int, default=0,
                       help="delay before submitting (0 = submit immediately, "
                            "N = schedule for N minutes from now, to be picked "
                            "up by a scheduler — not yet wired)")
    p_sub.add_argument("--dry-run", action="store_true",
                       help="print the payload, don't touch pod.db or 215")
    p_sub.add_argument("-v", "--verbose", action="store_true")
    p_sub.set_defaults(func=lambda a: sys.exit(cmd_submit(a)))

    # cancel ------------------------------------------------------------
    p_can = tf_sub.add_parser("cancel", help="cancel a pod order by id")
    p_can.add_argument("--id", type=int, required=True, help="pod_id (pod.db primary key)")
    p_can.add_argument("-v", "--verbose", action="store_true")
    p_can.set_defaults(func=lambda a: sys.exit(cmd_cancel(a)))

    # show --------------------------------------------------------------
    p_show = tf_sub.add_parser("show", help="show one pod_db row")
    p_show.add_argument("--id", type=int, required=True)
    p_show.add_argument("--full", action="store_true",
                        help="include raw JSON blobs (ship_to, 215 responses)")
    p_show.set_defaults(func=lambda a: sys.exit(cmd_show(a)))

    # status ------------------------------------------------------------
    p_st = tf_sub.add_parser("status", help="count pod_db rows by status")
    p_st.set_defaults(func=lambda a: sys.exit(cmd_status(a)))
