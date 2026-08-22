#!/usr/bin/env bash
set -e

REPO="/home/tanjim/coolton-private"
VENV="$REPO/.venv/bin/python"

cleanup() { kill "$WEB_PID" 2>/dev/null; }
trap cleanup TERM

"$VENV" "$REPO/coolton_web_helper.py" &
WEB_PID=$!

"$VENV" "$REPO/app.py" &
APP_PID=$!

wait "$APP_PID"
