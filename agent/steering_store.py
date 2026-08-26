import threading
import time

_lock = threading.Lock()
_queued: dict[tuple[str, str], list[dict]] = {}

# Messages queued this long ago belong to a run that's already gone (crashed,
# or finished without ever draining the queue) — never resurface them into an
# unrelated future run.
_STALE_AFTER_SECONDS = 30 * 60


def queue_steering_message(channel_id: str, thread_ts: str, text: str, user_id: str) -> None:
    """Record a message sent into a thread coolton is already working in, so the
    in-flight run can pick it up and factor it in instead of a whole separate
    turn racing (or queuing behind) the one already running."""
    with _lock:
        key = (channel_id, thread_ts)
        _queued.setdefault(key, []).append({
            "text": text, "user_id": user_id, "queued_at": time.time(),
        })


def peek_steering_messages(channel_id: str, thread_ts: str) -> list[dict]:
    """Return queued messages without clearing them — used when the caller
    isn't sure yet it can actually deliver them this round (see
    plan_block.after_tool, which only clears once it has embedded them in a
    string tool result the model will actually see)."""
    with _lock:
        messages = _queued.get((channel_id, thread_ts), [])
        now = time.time()
        return [m for m in messages if now - m["queued_at"] <= _STALE_AFTER_SECONDS]


def clear_steering_messages(channel_id: str, thread_ts: str) -> None:
    with _lock:
        _queued.pop((channel_id, thread_ts), None)
