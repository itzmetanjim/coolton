import threading
import time

_lock = threading.Lock()
_stop_times: dict[tuple[str, str], float] = {}

# A stop request only matters for runs that were already in flight when it was
# issued; nothing checks it after that. Anything this old can never affect a
# future run, so it's safe to drop — keeps this dict from growing forever.
_STOP_RECORD_RETENTION_SECONDS = 24 * 60 * 60


class HaltRun(Exception):
    """Raised to halt a running coolton turn immediately (skip or !stop)."""


def request_stop(channel_id: str, thread_ts: str) -> None:
    """Record a stop request for a thread. Only runs that STARTED before this
    timestamp will be halted, so a fresh message after !stop is unaffected."""
    with _lock:
        now = time.time()
        _stop_times[(channel_id, thread_ts)] = now
        stale = [
            k for k, ts in _stop_times.items()
            if now - ts > _STOP_RECORD_RETENTION_SECONDS
        ]
        for k in stale:
            del _stop_times[k]


def stop_requested_for(channel_id: str, thread_ts: str, run_started_at: float) -> bool:
    """True if a !stop was requested in this thread after this run started."""
    with _lock:
        ts = _stop_times.get((channel_id, thread_ts))
        return ts is not None and ts > run_started_at


def is_stop_command(text: str, bot_id: str = "") -> bool:
    """True if `text` IS a !stop command and nothing else — optionally preceded by an
    @-mention of the bot, with only whitespace anywhere else. A prompt that merely
    mentions "!stop" partway through a longer message (e.g. "what does !stop do?")
    must never trigger a halt; only a message whose entire content, once any leading
    mention is stripped, is exactly "!stop" counts."""
    stripped = text.strip()
    if bot_id:
        mention = f"<@{bot_id}>"
        if stripped.startswith(mention):
            stripped = stripped[len(mention):].strip()
    return stripped == "!stop"
