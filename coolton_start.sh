#!/usr/bin/env bash
set -e

REPO="/home/tanjim/coolton-private"
VENV="$REPO/.venv/bin/python"

# Tear down BOTH children on any exit path — a signal to this wrapper (systemd
# stop/restart), app.py exiting on its own (crash, unhandled exception), or
# the web helper exiting on its own. Empirically verified (see commit) against
# all three: without killing both PIDs here, a crash on either side leaves the
# other running as an orphan that the next restart piles another copy on top
# of — the exact bug class that cost a long debugging session on 2026-08-22.
cleanup() {
    kill "$WEB_PID" "$APP_PID" 2>/dev/null || true
}
trap cleanup EXIT TERM INT

"$VENV" "$REPO/coolton_web_helper.py" &
WEB_PID=$!

"$VENV" "$REPO/app.py" &
APP_PID=$!

# Wait for whichever exits first; cleanup (above) tears down the other.
wait -n "$WEB_PID" "$APP_PID" || true
