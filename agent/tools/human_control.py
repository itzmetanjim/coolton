"""Handing a CAPTCHA or other human check to a person (ask_human_for_challenge_tool).

coolton doesn't solve those checks itself. Instead, the person who asked gets a private
link that controls the sandbox's desktop for a few minutes (desktop_helpers.start_control,
a VNC server separate from the view-only stream), gets past the check in coolton's
browser, and coolton carries on. Control ends when coolton calls the tool with
finished=True, after CONTROL_SECONDS, or at the end of the turn, whichever comes first.
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

CONTROL_SECONDS = 300

_lock = threading.Lock()
_timers: dict[tuple[str, str], threading.Timer] = {}


def start(channel_id: str, thread_ts: str) -> str:
    """Turn control on for this thread's desktop and return the link. Raises on failure."""
    from agent import desktop_helpers as dh
    from agent.sandbox_helpers import get_or_create_sandbox

    sandbox, proxy_info = get_or_create_sandbox(channel_id, thread_ts)
    url = dh.start_control(sandbox, proxy_info)
    timer = threading.Timer(CONTROL_SECONDS, stop, args=(channel_id, thread_ts))
    timer.daemon = True
    with _lock:
        old = _timers.pop((channel_id, thread_ts), None)
        _timers[(channel_id, thread_ts)] = timer
    if old is not None:
        old.cancel()
    timer.start()
    return url


def active(channel_id: str, thread_ts: str) -> bool:
    with _lock:
        return (channel_id, thread_ts) in _timers


def stop(channel_id: str, thread_ts: str) -> bool:
    """Turn control off (if it's on). Returns whether it was on. Never raises."""
    with _lock:
        timer = _timers.pop((channel_id, thread_ts), None)
    if timer is None:
        return False
    timer.cancel()
    try:
        from agent import desktop_helpers as dh
        from agent.sandbox_helpers import get_or_create_sandbox

        sandbox, proxy_info = get_or_create_sandbox(channel_id, thread_ts)
        dh.stop_control(sandbox, proxy_info)
    except Exception:
        logger.exception("Couldn't turn desktop control off in %s/%s", channel_id, thread_ts)
    return True
