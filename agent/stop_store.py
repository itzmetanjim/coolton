import threading
import time

_lock = threading.Lock()
_stop_times: dict[tuple[str, str], float] = {}


class HaltRun(Exception):
    """Raised to halt a running coolton turn immediately (skip or !stop)."""


def request_stop(channel_id: str, thread_ts: str) -> None:
    """Record a stop request for a thread. Only runs that STARTED before this
    timestamp will be halted, so a fresh message after !stop is unaffected."""
    with _lock:
        _stop_times[(channel_id, thread_ts)] = time.time()


def stop_requested_for(channel_id: str, thread_ts: str, run_started_at: float) -> bool:
    """True if a !stop was requested in this thread after this run started."""
    with _lock:
        ts = _stop_times.get((channel_id, thread_ts))
        return ts is not None and ts > run_started_at
