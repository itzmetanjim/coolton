"""Hack Club AI (HCAI) balance check — warns a Slack thread when coolton's
primary provider is about to fall back to a much worse model.

Hits https://ai.hackclub.com/up at the start of every turn (see
listeners.events.turn.run_agent_turn) and, once `balanceRemaining` drops below
LOW_BALANCE_THRESHOLD, posts a heads-up into the thread so a degraded or
hallucinating reply doesn't look like coolton itself broke. The check runs on
a background thread so a slow/unreachable status endpoint never delays the
turn it was meant to warn about.
"""

from __future__ import annotations

import logging
import threading
import time

import requests

logger = logging.getLogger(__name__)

HCAI_STATUS_URL = "https://ai.hackclub.com/up"
LOW_BALANCE_THRESHOLD = 0.10
_REQUEST_TIMEOUT_SECONDS = 5

WARNING_TEXT = (
    "_HCAI is down, so coolton will fall back to significantly worse "
    "messages. Expect degraded responses and hallucinations._"
)

# Re-warn the same thread at most this often — without this, every single
# turn started while the balance stays low would repost the identical notice.
_REWARN_AFTER_SECONDS = 15 * 60

_lock = threading.Lock()
_last_warned: dict[tuple[str, str], float] = {}


def fetch_status() -> dict | None:
    """GET the HCAI /up endpoint. Returns the parsed JSON object, or None on
    any failure (network error, non-2xx, non-JSON/non-object body) — never
    raises, so a caller never has to guard this itself."""
    try:
        resp = requests.get(HCAI_STATUS_URL, timeout=_REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("HCAI status check failed: %s", e)
        return None
    return data if isinstance(data, dict) else None


def _should_warn(channel_id: str, thread_ts: str) -> bool:
    """True at most once per _REWARN_AFTER_SECONDS for a given thread."""
    key = (channel_id, thread_ts)
    now = time.time()
    with _lock:
        last = _last_warned.get(key)
        if last is not None and now - last < _REWARN_AFTER_SECONDS:
            return False
        _last_warned[key] = now
        return True


def _warn_low_balance(client, channel_id: str, thread_ts: str) -> None:
    status = fetch_status()
    if status is None:
        return
    balance = status.get("balanceRemaining")
    if not isinstance(balance, (int, float)) or balance >= LOW_BALANCE_THRESHOLD:
        return
    if not _should_warn(channel_id, thread_ts):
        return
    try:
        client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts or None, text=WARNING_TEXT,
        )
    except Exception:
        logger.exception(
            "Failed to post HCAI low-balance warning to %s/%s", channel_id, thread_ts,
        )


def check_and_warn_async(client, channel_id: str, thread_ts: str) -> None:
    """Fire-and-forget: check HCAI's balance and warn the thread if it's
    critically low. Runs on a daemon thread so a slow/down status endpoint
    never adds latency to the turn that triggered this check."""
    threading.Thread(
        target=_warn_low_balance,
        args=(client, channel_id, thread_ts),
        daemon=True,
        name="hcai-status-check",
    ).start()
