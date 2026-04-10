#!/usr/bin/env python3
"""
eBay OAuth Token Manager for KLHAutographs.

Provides get_access_token() — the single entry point the rest of the
codebase uses. Handles refresh automatically and persists an absolute
expiry timestamp so we never guess.

Credentials and tokens live at ~/.klh/ (outside the repo, outside any
synced drive). The initial setup flow is still oauth_setup.py in the
original ebay-api/ folder — this module only handles the ongoing refresh.

Run directly for a status check:
    python -m ebay_api.token_manager           # status + auto-refresh if needed
    python -m ebay_api.token_manager --force   # force a refresh now
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

CONFIG_DIR = os.path.expanduser("~/.klh")
ENV_FILE = os.path.join(CONFIG_DIR, ".env")
TOKEN_FILE = os.path.join(CONFIG_DIR, "tokens.json")
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"

# Same scopes as oauth_setup.py — must match what the refresh token was issued for.
SCOPES = [
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.marketing",
    "https://api.ebay.com/oauth/api_scope/sell.account",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
    "https://api.ebay.com/oauth/api_scope/sell.finances",
    "https://api.ebay.com/oauth/api_scope/sell.inventory.readonly",
    "https://api.ebay.com/oauth/api_scope/sell.account.readonly",
]

# Refresh the access token when it's within this many seconds of expiry.
ACCESS_REFRESH_LEEWAY = 300  # 5 minutes

# Warn loudly when the refresh token itself is within this many days of expiry.
REFRESH_WARN_DAYS = 30


class TokenError(RuntimeError):
    pass


def _load_env():
    """Parse .env into a dict. Dependency-free — no python-dotenv needed."""
    if not os.path.exists(ENV_FILE):
        raise TokenError(
            f".env not found at {ENV_FILE}. "
            "Copy it from the initial oauth_setup.py run."
        )
    env = {}
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _load_tokens():
    if not os.path.exists(TOKEN_FILE):
        raise TokenError(
            f"tokens.json not found at {TOKEN_FILE}. "
            "Run oauth_setup.py to do the initial user consent flow."
        )
    with open(TOKEN_FILE) as f:
        return json.load(f)


def _save_tokens(tokens):
    with open(TOKEN_FILE, "w") as f:
        json.dump(tokens, f, indent=2)
    # Re-apply restrictive perms in case the file was recreated.
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s):
    # Accept either trailing Z or +00:00
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def _backfill_expiries(tokens, saved_mtime):
    """
    Old tokens.json files only have expires_in, not absolute timestamps.
    Derive them from the file mtime so the first run after an upgrade
    doesn't force a needless refresh.
    """
    changed = False
    issued_at = datetime.fromtimestamp(saved_mtime, tz=timezone.utc)

    if "access_expires_at" not in tokens and "expires_in" in tokens:
        tokens["access_expires_at"] = _iso(
            issued_at + timedelta(seconds=int(tokens["expires_in"]))
        )
        changed = True

    if (
        "refresh_expires_at" not in tokens
        and "refresh_token_expires_in" in tokens
    ):
        tokens["refresh_expires_at"] = _iso(
            issued_at + timedelta(seconds=int(tokens["refresh_token_expires_in"]))
        )
        changed = True

    return tokens, changed


def refresh_access_token(verbose=False):
    """
    Exchange the refresh token for a new access token. Writes the updated
    access_token + absolute access_expires_at back to tokens.json. The
    refresh token itself does not rotate on eBay — it stays valid for its
    original ~18 months.
    """
    env = _load_env()
    app_id = env.get("EBAY_APP_ID")
    cert_id = env.get("EBAY_CERT_ID")
    if not app_id or not cert_id:
        raise TokenError("EBAY_APP_ID or EBAY_CERT_ID missing from .env")

    tokens = _load_tokens()
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise TokenError("No refresh_token in tokens.json. Re-run oauth_setup.py.")

    credentials = base64.b64encode(f"{app_id}:{cert_id}".encode()).decode()

    data = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": " ".join(SCOPES),
        }
    ).encode()

    req = urllib.request.Request(TOKEN_URL, data=data)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Authorization", f"Basic {credentials}")

    if verbose:
        print("Refreshing access token...")

    try:
        with urllib.request.urlopen(req) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        raise TokenError(
            f"Refresh failed ({e.code}). eBay response:\n{err_body}"
        ) from e

    if "access_token" not in body:
        raise TokenError(f"Refresh response missing access_token: {body}")

    now = _now()
    tokens["access_token"] = body["access_token"]
    tokens["expires_in"] = body.get("expires_in", 7200)
    tokens["access_expires_at"] = _iso(
        now + timedelta(seconds=int(tokens["expires_in"]))
    )
    tokens["token_type"] = body.get("token_type", tokens.get("token_type"))
    tokens["last_refreshed_at"] = _iso(now)

    # eBay doesn't rotate refresh tokens on refresh, but defend against it
    # in case they ever start.
    if "refresh_token" in body and body["refresh_token"] != refresh_token:
        tokens["refresh_token"] = body["refresh_token"]
        if "refresh_token_expires_in" in body:
            tokens["refresh_token_expires_in"] = body["refresh_token_expires_in"]
            tokens["refresh_expires_at"] = _iso(
                now + timedelta(seconds=int(body["refresh_token_expires_in"]))
            )

    _save_tokens(tokens)

    if verbose:
        print(f"  New access token expires at {tokens['access_expires_at']}")

    return tokens


def get_access_token(force_refresh=False, verbose=False):
    """
    Returns a valid access token, refreshing if necessary. Call this from
    anywhere that needs to hit the eBay API.
    """
    tokens = _load_tokens()

    # First-run backfill for tokens.json files that predate this module.
    tokens, changed = _backfill_expiries(tokens, os.path.getmtime(TOKEN_FILE))
    if changed:
        _save_tokens(tokens)

    if force_refresh:
        tokens = refresh_access_token(verbose=verbose)
        return tokens["access_token"]

    expires_at_str = tokens.get("access_expires_at")
    if not expires_at_str:
        tokens = refresh_access_token(verbose=verbose)
        return tokens["access_token"]

    expires_at = _parse_iso(expires_at_str)
    if _now() + timedelta(seconds=ACCESS_REFRESH_LEEWAY) >= expires_at:
        tokens = refresh_access_token(verbose=verbose)

    # Warn (but don't fail) if refresh token itself is nearing expiry.
    refresh_expires_at_str = tokens.get("refresh_expires_at")
    if refresh_expires_at_str:
        refresh_expires_at = _parse_iso(refresh_expires_at_str)
        days_left = (refresh_expires_at - _now()).days
        if days_left <= REFRESH_WARN_DAYS:
            print(
                f"WARNING: eBay refresh token expires in {days_left} days "
                f"({refresh_expires_at_str}). Re-run oauth_setup.py soon.",
                file=sys.stderr,
            )

    return tokens["access_token"]


def _format_delta(seconds):
    seconds = int(seconds)
    if seconds < 0:
        return f"EXPIRED {-seconds}s ago"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def status():
    """Print current token status. Used by CLI mode."""
    tokens = _load_tokens()
    tokens, changed = _backfill_expiries(tokens, os.path.getmtime(TOKEN_FILE))
    if changed:
        _save_tokens(tokens)

    now = _now()
    print("=" * 60)
    print("KLHAutographs eBay Token Status")
    print("=" * 60)
    print(f"  Config dir:        {CONFIG_DIR}")
    print(f"  Now:               {_iso(now)}")

    access_expires_at = tokens.get("access_expires_at")
    if access_expires_at:
        delta = (_parse_iso(access_expires_at) - now).total_seconds()
        print(f"  Access expires:    {access_expires_at}  ({_format_delta(delta)})")
    else:
        print("  Access expires:    unknown")

    refresh_expires_at = tokens.get("refresh_expires_at")
    if refresh_expires_at:
        delta = (_parse_iso(refresh_expires_at) - now).total_seconds()
        print(f"  Refresh expires:   {refresh_expires_at}  ({_format_delta(delta)})")
    else:
        print("  Refresh expires:   unknown")

    last_refreshed = tokens.get("last_refreshed_at")
    if last_refreshed:
        print(f"  Last refreshed:    {last_refreshed}")
    print()


def main():
    force = "--force" in sys.argv[1:]
    try:
        status()
        token = get_access_token(force_refresh=force, verbose=True)
        print(f"  Access token OK:   {token[:40]}...")
        print()
    except TokenError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
