"""DM the admin (lily, U0B2VTYER33) about things that need a human's eyes:
secret leaks (see agent/redact.py's separate notifier), the Slack MCP Server
going down (agent/mcp_health.py), and human feedback on responses
(listeners/views/feedback_views.py).

Runs the actual Slack call on a background thread so a caller (an action
handler that must ack() fast, or a scheduler job) never blocks on it.
"""

import logging
import os
import threading
import time

import requests

logger = logging.getLogger(__name__)

ADMIN_USER_ID = "U0B2VTYER33"

_alert_lock = threading.Lock()
_last_alert_at: dict[str, float] = {}


def notify_admin(text: str, *, dedupe_key: str | None = None, min_interval_seconds: float = 0.0) -> None:
    """DM ADMIN_USER_ID with `text`.

    By default every call sends (feedback DMs should never be dropped). Pass
    `dedupe_key` + `min_interval_seconds` to rate-limit a noisy/repeating
    condition (e.g. a health check that fires every few minutes) to at most
    one DM per window.
    """
    if dedupe_key is not None and min_interval_seconds > 0:
        now = time.time()
        with _alert_lock:
            last = _last_alert_at.get(dedupe_key, 0.0)
            if now - last < min_interval_seconds:
                return
            _last_alert_at[dedupe_key] = now

    def _send():
        bot_token = os.environ.get("SLACK_BOT_TOKEN")
        if not bot_token:
            logger.warning("Cannot send admin alert, SLACK_BOT_TOKEN not configured: %s", text)
            return
        try:
            resp = requests.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"},
                json={"channel": ADMIN_USER_ID, "text": text},
                timeout=10,
            )
            res_json = resp.json()
            if not res_json.get("ok"):
                logger.error("Admin alert DM rejected by Slack: %s", res_json.get("error", "unknown"))
        except Exception:
            logger.exception("Failed to send admin alert DM")

    threading.Thread(target=_send, daemon=True, name="admin-alert").start()
