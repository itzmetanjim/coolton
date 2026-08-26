import threading
import time

_lock = threading.Lock()
_active_since: dict[tuple[str, str], float] = {}

# A run that "started" this long ago and never called mark_run_finished (e.g.
# it crashed hard enough to skip the finally block) is presumed dead — this
# stops a stuck entry from silently steering every future message in the
# thread into a queue nothing will ever drain.
_STALE_AFTER_SECONDS = 30 * 60


def mark_run_started(channel_id: str, thread_ts: str, started_at: float) -> None:
    with _lock:
        _active_since[(channel_id, thread_ts)] = started_at


def mark_run_finished(channel_id: str, thread_ts: str) -> None:
    with _lock:
        _active_since.pop((channel_id, thread_ts), None)


def is_run_active(channel_id: str, thread_ts: str) -> bool:
    """True if a coolton run is currently in flight for this thread."""
    with _lock:
        started_at = _active_since.get((channel_id, thread_ts))
        if started_at is None:
            return False
        if time.time() - started_at > _STALE_AFTER_SECONDS:
            del _active_since[(channel_id, thread_ts)]
            return False
        return True
