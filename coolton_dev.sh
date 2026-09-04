#!/usr/bin/env bash
# Local dev runner for coolton's web UI — NOT the production launcher
# (that's coolton_start.sh, which runs on the actual host). Starts/stops the
# three local processes the web UI needs:
#   - web app        :8000  (web/server.py, via `python3 -c ...` so we can
#                             load_dotenv(override=False) before importing)
#   - github proxy    :29054/:29055  (github_proxy.py — per-sandbox GitHub
#                             token issuance; only needed for tools that spin
#                             up a sandbox with GitHub access)
#   - web64 helper    :2389  (coolton_web_helper.py — file host for
#                             upload_file_from_sandbox / embeds)
#
# The Slack `-u` unsets below exist because this machine's ~/.bashrc exports
# SLACK_*_TOKEN for a different, unrelated Slack app permanently — those would
# otherwise shadow .env's real coolton tokens (load_dotenv is override=False).
#
# Usage: ./coolton_dev.sh {up|down|restart|status|logs}

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

PIDDIR=".local/pids"
mkdir -p "$PIDDIR" .local/web64_files

WEB_LOG=/tmp/coolton_web_local.log
PROXY_LOG=/tmp/coolton_github_proxy.log
HELPER_LOG=/tmp/coolton_web_helper.log

SLACK_UNSETS=(-u SLACK_BOT_TOKEN -u SLACK_USER_TOKEN -u SLACK_APP_TOKEN -u SLACK_SIGNING_SECRET
              -u SLACK_SCRAPED_CLIENT_TOKEN -u SLACK_SCRAPED_DASHBOARD_TOKEN)

pid_alive() {
    local pidfile="$1"
    [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null
}

stop_one() {
    local name="$1" pidfile="$2"
    if pid_alive "$pidfile"; then
        kill "$(cat "$pidfile")" 2>/dev/null || true
        rm -f "$pidfile"
        echo "stopped $name"
    else
        rm -f "$pidfile"
    fi
}

start_web() {
    if pid_alive "$PIDDIR/web.pid"; then echo "web app already running (pid $(cat "$PIDDIR/web.pid"))"; return; fi
    env "${SLACK_UNSETS[@]}" \
        WEB_HELPER_TOKEN_FILE="$PWD/.local/web64_token" \
        WEB_HELPER_UPLOAD_URL="http://127.0.0.1:2389/upload" \
        nohup python3 -c "
from dotenv import load_dotenv
load_dotenv(dotenv_path='.env', override=False)
from web.server import run
run(host='127.0.0.1', port=8000)
" > "$WEB_LOG" 2>&1 &
    echo $! > "$PIDDIR/web.pid"
    echo "started web app (pid $!) -> $WEB_LOG"
}

start_proxy() {
    if pid_alive "$PIDDIR/proxy.pid"; then echo "github proxy already running (pid $(cat "$PIDDIR/proxy.pid"))"; return; fi
    env "${SLACK_UNSETS[@]}" \
        nohup python3 -c "
from dotenv import load_dotenv
load_dotenv(dotenv_path='.env', override=False)
from github_proxy import start_proxy
start_proxy()
import time
while True: time.sleep(3600)
" > "$PROXY_LOG" 2>&1 &
    echo $! > "$PIDDIR/proxy.pid"
    echo "started github proxy (pid $!) -> $PROXY_LOG"
}

start_helper() {
    if pid_alive "$PIDDIR/helper.pid"; then echo "web64 helper already running (pid $(cat "$PIDDIR/helper.pid"))"; return; fi
    if [ ! -f .local/web64_token ]; then
        python3 -c "import secrets; open('.local/web64_token','w').write(secrets.token_hex(32)+chr(10))"
        chmod 600 .local/web64_token
        echo "generated .local/web64_token"
    fi
    env WEB_HELPER_FILES_DIR="$PWD/.local/web64_files" \
        WEB_HELPER_TOKEN_FILE="$PWD/.local/web64_token" \
        WEB_HELPER_BASE_URL="http://127.0.0.1:2389" \
        nohup python3 coolton_web_helper.py > "$HELPER_LOG" 2>&1 &
    echo $! > "$PIDDIR/helper.pid"
    echo "started web64 helper (pid $!) -> $HELPER_LOG"
}

cmd_up() {
    start_helper
    start_proxy
    start_web
    sleep 2
    cmd_status
}

cmd_down() {
    stop_one "web app" "$PIDDIR/web.pid"
    stop_one "github proxy" "$PIDDIR/proxy.pid"
    stop_one "web64 helper" "$PIDDIR/helper.pid"
}

cmd_status() {
    printf '%-14s %-6s %-8s %s\n' "service" "port" "pid" "status"
    for row in "web app:8000:$PIDDIR/web.pid" "github proxy:29055:$PIDDIR/proxy.pid" "web64 helper:2389:$PIDDIR/helper.pid"; do
        IFS=: read -r name port pidfile <<< "$row"
        if pid_alive "$pidfile"; then
            code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 "http://127.0.0.1:$port/" || echo "??")
            printf '%-14s %-6s %-8s up (http %s)\n' "$name" "$port" "$(cat "$pidfile")" "$code"
        else
            printf '%-14s %-6s %-8s down\n' "$name" "$port" "-"
        fi
    done
}

cmd_logs() {
    tail -f "$WEB_LOG" "$PROXY_LOG" "$HELPER_LOG"
}

case "${1:-}" in
    up) cmd_up ;;
    down) cmd_down ;;
    restart) cmd_down; sleep 1; cmd_up ;;
    status) cmd_status ;;
    logs) cmd_logs ;;
    *) echo "usage: $0 {up|down|restart|status|logs}"; exit 1 ;;
esac
