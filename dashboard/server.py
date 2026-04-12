"""
uvicorn runner for the KLH dashboard.

Invoked from `klh dashboard` (cli.klh). This is a thin wrapper around
`uvicorn.run(...)` so we can:

    * bind to 127.0.0.1 by default (no remote access)
    * pick a stable default port (8765) that won't clash with common
      dev servers
    * respect --host / --port overrides from the CLI
    * auto-open http://<host>:<port>/ in the user's browser (opt out
      with --no-browser)

We deliberately import `dashboard.app` lazily inside `run()` so that
`klh --help` stays snappy on machines without FastAPI installed.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import webbrowser
from typing import Sequence, Optional


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _default_port() -> int:
    """Respect $PORT if the environment sets one (preview tools, PaaS)."""
    raw = os.environ.get("PORT")
    if raw and raw.isdigit():
        return int(raw)
    return DEFAULT_PORT


def _open_browser_when_ready(url: str, delay: float = 0.8) -> None:
    """Give uvicorn a moment to bind, then pop the browser."""
    def _target() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass  # headless CI / missing browser — not fatal
    threading.Thread(target=_target, daemon=True).start()


def run(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    reload: bool = False,
    open_browser: bool = True,
) -> None:
    """Start the dashboard. Blocks until Ctrl-C."""
    try:
        import uvicorn
    except ImportError as e:
        raise SystemExit(
            "uvicorn is not installed. Install the dashboard extras:\n"
            "    pip install 'klh-listing-tool[dashboard]'"
        ) from e

    url = f"http://{host}:{port}/"
    print(f"KLH Dashboard → {url}")
    print("(Ctrl-C to stop)")

    if open_browser:
        _open_browser_when_ready(url)

    # When reload=True uvicorn needs an import string, not an app object,
    # so it can reboot the worker. For the normal path we pass the factory
    # via the import string too — keeps both modes consistent.
    uvicorn.run(
        "dashboard.app:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="klh dashboard",
        description="Run the KLH listing dashboard (local web UI).",
    )
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"bind address (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=_default_port(),
                        help=f"bind port (default: {DEFAULT_PORT} or $PORT)")
    parser.add_argument("--reload", action="store_true",
                        help="auto-reload on code changes (dev)")
    parser.add_argument("--no-browser", action="store_true",
                        help="don't auto-open the browser")
    args = parser.parse_args(argv)

    run(
        host=args.host,
        port=args.port,
        reload=args.reload,
        open_browser=not args.no_browser,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
