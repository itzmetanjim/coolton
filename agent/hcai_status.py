"""Hack Club AI (HCAI) balance check — warns a Slack thread when coolton's
primary provider is about to fall back to a much worse model.

Two ways HCAI stops working for coolton, each with its own notice in the thread so
a degraded or hallucinating reply doesn't look like coolton itself broke:

- HCAI as a whole is down (its global credits ran out): checked against
  https://ai.hackclub.com/up at the start of every turn (see
  listeners.events.turn.run_agent_turn), warning once `balanceRemaining` drops
  below LOW_BALANCE_THRESHOLD (WARNING_TEXT).
- coolton's own HCAI keys ran out of credits or hit a spending limit: when one key
  does, HCAI switches to coolton's next key (agent.provider_config.on_outage) with
  no notice; once every key has, the whole HCAI family is marked dead and the
  thread is warned right then (warn_hcai_outage, CREDITS_WARNING_TEXT). An error
  that means HCAI is out for everyone gets WARNING_TEXT the same way. Later turns
  warn at their start while HCAI stays marked dead.

The turn-start check runs on a background thread so a slow/unreachable status
endpoint never delays the turn it was meant to warn about. Each thread is warned at
most once per _REWARN_AFTER_SECONDS, whichever notice it is.
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

HCAI_FAMILY = "hcai"

WARNING_TEXT = (
    "_HCAI is down, so coolton will fall back to significantly worse "
    "models. Expect degraded responses and hallucinations._"
)
CREDITS_WARNING_TEXT = (
    "_coolton's HCAI credits ran out or hit a spending limit, so coolton will fall back "
    "to significantly worse models until they're topped up. Expect degraded responses "
    "and hallucinations._"
)

# Re-warn the same thread at most this often — without this, every single
# turn started while the balance stays low would repost the identical notice.
_REWARN_AFTER_SECONDS = 15 * 60

_lock = threading.Lock()
_last_warned: dict[tuple[str, str], float] = {}


def fetch_status() -> dict | None:
    """GET the HCAI /up endpoint. Returns the parsed JSON object, or None on
    any failure (network error, non-JSON/non-object body) — never raises, so
    a caller never has to guard this itself.

    Deliberately does NOT raise_for_status(): the endpoint itself responds
    with HTTP 503 whenever `status` is "down" (confirmed live —
    `{"status":"down","balanceRemaining":-0.27,...}` arrives on a 503, not a
    200), so treating a non-2xx as failure discarded the payload exactly when
    it mattered most and this check silently never fired.
    """
    try:
        resp = requests.get(HCAI_STATUS_URL, timeout=_REQUEST_TIMEOUT_SECONDS)
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


def _outage_text(reason: str) -> str | None:
    """The notice for HCAI being marked dead for `reason`, or None if it's not an outage."""
    from agent.fallback_cache import outage_kind

    kind = outage_kind(reason)
    return {"provider": WARNING_TEXT, "key": CREDITS_WARNING_TEXT}.get(kind or "")


def marked_out_text() -> str | None:
    """The notice for HCAI while it's marked dead for an outage, else None."""
    from agent.fallback_cache import get_dead_families

    reason = get_dead_families().get(HCAI_FAMILY)
    return _outage_text(reason) if reason else None


def _post(client, channel_id: str, thread_ts: str, text: str) -> None:
    try:
        client.chat_postMessage(channel=channel_id, thread_ts=thread_ts or None, text=text)
    except Exception:
        logger.exception("Failed to post HCAI warning to %s/%s", channel_id, thread_ts)


def _warn_low_balance(client, channel_id: str, thread_ts: str) -> None:
    status = fetch_status()
    balance = status.get("balanceRemaining") if status else None
    if isinstance(balance, (int, float)) and balance < LOW_BALANCE_THRESHOLD:
        text = WARNING_TEXT
    else:
        text = marked_out_text()
    if not text:
        return
    if _should_warn(channel_id, thread_ts):
        _post(client, channel_id, thread_ts, text)


def warn_hcai_outage(deps, reason: str) -> None:
    """Warn this run's thread the moment the provider chain finds HCAI out (every
    coolton key out of credits or at a limit, or HCAI down for everyone). Never raises."""
    text = _outage_text(reason)
    channel_id = getattr(deps, "channel_id", "") or ""
    thread_ts = getattr(deps, "thread_ts", "") or ""
    if not text or not channel_id or not _should_warn(channel_id, thread_ts):
        return
    try:
        deps.get_surface().post_text(text)
    except Exception:
        logger.exception("Failed to post the HCAI outage warning to %s/%s", channel_id, thread_ts)


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
