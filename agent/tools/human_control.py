"""Handing a CAPTCHA or other human check to a person (ask_human_for_challenge_tool).

coolton doesn't solve those checks itself. Instead, the person who asked gets a private
link that controls the sandbox's desktop for a few minutes (desktop_helpers.start_control,
a VNC server separate from the view-only stream), gets past the check in coolton's
browser, and coolton carries on. Control ends when coolton calls the tool with
finished=True or after CONTROL_SECONDS. It outlives the turn that started it on purpose:
coolton waits for the person with wait_tool, which ends the turn and resumes later, so the
end of a turn leaves the sandbox running while control is on (agent.agent.run_agent), and
stop() pauses it once no turn is running in the thread.
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
    try:
        from agent.active_runs import is_run_active
        from agent.sandbox_helpers import pause_if_running

        if not is_run_active(channel_id, thread_ts):  # a running turn pauses it when it ends
            pause_if_running(channel_id, thread_ts)
    except Exception:
        logger.exception("Couldn't pause the sandbox after desktop control in %s/%s", channel_id, thread_ts)
    return True
