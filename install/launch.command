#!/bin/bash
# ============================================================
#  KLH Listing Tool - launcher (macOS)
# ============================================================
#  Pulls the latest code from GitHub, starts the dashboard
#  server, opens the browser. Close the Terminal window (or
#  press Ctrl-C) to stop the server.
#
#  Double-click from Finder to launch.
#  First time only: Right-click → Open (to bypass Gatekeeper),
#  or `chmod +x launch.command` from Terminal.
# ============================================================

# Resolve the repo root (the folder that contains this script's parent).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.." || exit 1

echo "Fetching latest version from GitHub..."
if ! git pull --quiet; then
    echo "WARNING: git pull failed - continuing with current local version."
    echo "         Check your internet connection. Press Return to continue."
    read -r
fi

# If an older dashboard process is still holding port 8765 (from a
# previous launch whose Terminal window got lost), kill it so the
# new server can bind. Otherwise we'd fail with Errno 48.
STALE_PIDS=$(lsof -ti tcp:8765 2>/dev/null)
if [ -n "$STALE_PIDS" ]; then
    echo "Killing stale dashboard process(es) holding port 8765: $STALE_PIDS"
    kill $STALE_PIDS 2>/dev/null
    sleep 1
    # Force-kill anything that refused to die cleanly
    STILL=$(lsof -ti tcp:8765 2>/dev/null)
    [ -n "$STILL" ] && kill -9 $STILL 2>/dev/null
fi

# Open browser after a brief delay so the server has time to bind.
# We pass --no-browser to the server so it doesn't also open a tab —
# otherwise we'd get two duplicate localhost tabs on every launch.
( sleep 3 && open http://localhost:8765 ) &

echo ""
echo "Starting dashboard on http://localhost:8765"
echo "(Close this window or press Ctrl-C to stop the server.)"
echo ""

# Activate venv and run the server. `exec` replaces the shell so Ctrl-C
# goes straight to uvicorn.
# shellcheck disable=SC1091
source .venv/bin/activate
exec python -m dashboard.server --port 8765 --no-browser
