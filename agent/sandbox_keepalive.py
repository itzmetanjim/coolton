# agent/sandbox_keepalive.py
"""Delayed sandbox pause for live VNC streams.

Without this, a sandbox action (e.g. `run_linux_command`) pauses the sandbox the
instant it returns — fine normally, but with a VNC stream open that means the viewer
watches for the ~2 seconds a command takes and then goes dark, which defeats the
point of a *live* view. While a stream is active (AgentDeps.sandbox_keepalive_seconds
> 0), sandbox-touching tools call `arm()` instead of pausing immediately: it (re)starts
a countdown to auto-pause, so the sandbox only actually pauses after that many seconds
of no further activity. Every action resets the countdown.

Always canceled by `cancel()` at the end of a turn (agent.agent.run_agent's finally
block already force-pauses unconditionally there) so a stray timer never outlives the
turn that armed it or pauses a sandbox a later turn is still using.
"""

import logging
import threading

from e2b import Sandbox
from agent.sandbox_store import get_thread_sandbox_id

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_timers: dict[tuple[str, str], threading.Timer] = {}


def _pause(channel_id: str, thread_ts: str) -> None:
    with _lock:
        _timers.pop((channel_id, thread_ts), None)
    try:
        sandbox_id = get_thread_sandbox_id(channel_id, thread_ts)
        if sandbox_id:
            Sandbox.connect(sandbox_id).pause()
    except Exception as e:
        logger.warning(f"sandbox keepalive auto-pause failed for {channel_id}/{thread_ts}: {e}")


def arm(channel_id: str, thread_ts: str, seconds: float) -> None:
    """(Re)start the countdown to auto-pause, canceling any countdown already running
    for this thread first — call this from every sandbox action instead of pausing
    immediately, and it behaves as a reset each time."""
    key = (channel_id, thread_ts)
    with _lock:
        old = _timers.pop(key, None)
        if old:
            old.cancel()
        if seconds > 0:
            t = threading.Timer(seconds, _pause, args=(channel_id, thread_ts))
            t.daemon = True
            _timers[key] = t
            t.start()


def cancel(channel_id: str, thread_ts: str) -> None:
    """Stop any pending countdown without pausing — the caller is responsible for
    pausing itself right after (e.g. end-of-turn cleanup)."""
    key = (channel_id, thread_ts)
    with _lock:
        old = _timers.pop(key, None)
        if old:
            old.cancel()
