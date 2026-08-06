import threading
import time

_lock = threading.Lock()
_stop_times: dict[str, float] = {}


class HaltRun(Exception):
    """Raised to halt a running coolton turn immediately (skip or !stop)."""


def request_stop(user_id: str) -> None:
    """Record a stop request for a user. Only runs that STARTED before this
    timestamp will be halted, so a fresh message after !stop is unaffected."""
    with _lock:
        _stop_times[user_id] = time.time()


def stop_requested_for(user_id: str, run_started_at: float) -> bool:
    """True if the user requested a stop after this run started."""
    with _lock:
        ts = _stop_times.get(user_id)
        return ts is not None and ts > run_started_at
