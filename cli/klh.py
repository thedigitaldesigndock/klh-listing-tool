"""
klh — main command-line entry point for the KLH listing tool.

Subcommands dispatched here:
    klh config       — show resolved config (debug)
    klh token        — show eBay token status / force refresh
    klh match        — (Phase 1) pair pictures with cards, report issues
    klh normalize    — (Phase 2) convert non-JPG sources to JPEG in-place
    klh mockup       — (Phase 3) render a mockup from a template
    klh verify       — (Phase 6) dry-run a listing via VerifyAddFixedPriceItem
    klh schedule     — (Phase 6) create a scheduled listing (--at ISO8601)
    klh list         — (Phase 6) create a live listing now (--confirm)
    klh unlist       — (Phase 6) end an active listing
"""

import argparse
import sys
from pathlib import Path


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


def _cmd_normalize(args):
    from pipeline import normalize
    argv: list[str] = []
    if args.picture_dir:
        argv += ["--picture-dir", str(args.picture_dir)]
    if args.card_dir:
        argv += ["--card-dir", str(args.card_dir)]
    if args.only:
        argv += ["--only", args.only]
    if args.quality is not None:
        argv += ["--quality", str(args.quality)]
    if args.keep_originals:
        argv.append("--keep-originals")
    if args.dry_run:
        argv.append("--dry-run")
    if args.no_color:
        argv.append("--no-color")
    sys.exit(normalize.main(argv))


def _cmd_mockup(args):
    from pipeline import compositor
    argv = ["--template", args.template, "--out", str(args.out)]
    if args.picture:
        argv += ["--picture", str(args.picture)]
    if args.card:
        argv += ["--card", str(args.card)]
    if args.name:
        argv += ["--name", args.name]
    sys.exit(compositor.main(argv))


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

    p_norm = sub.add_parser("normalize",
                            help="convert non-JPG sources to JPEG in-place")
    p_norm.add_argument("--picture-dir", type=Path)
    p_norm.add_argument("--card-dir", type=Path)
    p_norm.add_argument("--only", choices=("picture", "card"))
    p_norm.add_argument("--quality", type=int, default=None)
    p_norm.add_argument("--keep-originals", action="store_true")
    p_norm.add_argument("--dry-run", action="store_true")
    p_norm.add_argument("--no-color", action="store_true")
    p_norm.set_defaults(func=_cmd_normalize)

    p_mockup = sub.add_parser("mockup", help="render a mockup from a template")
    p_mockup.add_argument("--template", required=True, help="template id (slug)")
    p_mockup.add_argument("--picture", type=Path, help="picture source path")
    p_mockup.add_argument("--card", type=Path, help="card source path")
    p_mockup.add_argument("--name", help="display name (defaults to picture stem)")
    p_mockup.add_argument("--out", type=Path, required=True, help="output file path")
    p_mockup.set_defaults(func=_cmd_mockup)

    # Phase 6: verify / schedule / list / unlist — wired from cli.list_cmd
    from cli import list_cmd
    list_cmd.register(sub)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
