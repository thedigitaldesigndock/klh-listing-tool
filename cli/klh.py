"""
klh — main command-line entry point for the KLH listing tool.

Subcommands dispatched here:
    klh config       — show resolved config (debug)
    klh token        — show eBay token status / force refresh
    klh match        — (Phase 1) pair pictures with cards, report issues
    klh normalize    — (Phase 2) not yet implemented
    klh mockup       — (Phase 3) not yet implemented
    klh list         — (Phase 6) not yet implemented
"""

import argparse
import sys


def _cmd_config(args):
    from pipeline import config
    config.main()


def _cmd_token(args):
    from ebay_api import token_manager
    sys.argv = ["token_manager"] + (["--force"] if args.force else [])
    token_manager.main()


def _cmd_match(args):
    from pipeline import matcher
    argv = []
    if args.picture_dir:
        argv += ["--picture-dir", str(args.picture_dir)]
    if args.card_dir:
        argv += ["--card-dir", str(args.card_dir)]
    if args.json:
        argv.append("--json")
    if args.fix:
        argv.append("--fix")
    if args.no_color:
        argv.append("--no-color")
    sys.exit(matcher.main(argv))


def _cmd_stub(args):
    print(f"{args.which}: not yet implemented")
    sys.exit(2)


def main():
    parser = argparse.ArgumentParser(prog="klh", description="KLH listing tool")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("config", help="show resolved per-machine config").set_defaults(
        func=_cmd_config
    )

    p_token = sub.add_parser("token", help="show eBay token status / force refresh")
    p_token.add_argument("--force", action="store_true", help="force a refresh now")
    p_token.set_defaults(func=_cmd_token)

    p_match = sub.add_parser("match", help="pair pictures with cards")
    p_match.add_argument("--picture-dir", help="override picture_dir from config")
    p_match.add_argument("--card-dir", help="override card_dir from config")
    p_match.add_argument("--json", action="store_true", help="JSON output")
    p_match.add_argument("--fix", action="store_true",
                         help="interactively apply rename suggestions")
    p_match.add_argument("--no-color", action="store_true")
    p_match.set_defaults(func=_cmd_match)

    for name in ("normalize", "mockup", "list"):
        p = sub.add_parser(name, help=f"{name} (not yet implemented)")
        p.set_defaults(func=_cmd_stub, which=name)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
