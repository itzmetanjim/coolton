"""Messages sent into a thread while a coolton turn there is still running.

Every such message (from anyone in the thread, or a system note: a background job
finishing, a wait ending, with user_id "") is folded into that turn's next tool
result (agent.plan_block / agent.surfaces.web_hooks) instead of racing a second
turn. One from someone other than the person the turn is for is labelled as such
(plan_block.steering_note), so it isn't taken as theirs. Anything still queued when
the turn ends gets a turn of its own (listeners.events.turn).
"""
import threading
import time

_lock = threading.Lock()
_queued: dict[tuple[str, str], list[dict]] = {}


def queue_steering_message(
    channel_id: str, thread_ts: str, text: str, user_id: str, message_ts: str = ""
) -> None:
    """Record a message sent into a thread coolton is already working in."""
    with _lock:
        _queued.setdefault((channel_id, thread_ts), []).append({
            "text": text, "user_id": user_id, "message_ts": message_ts, "queued_at": time.time(),
        })


def peek_steering_messages(channel_id: str, thread_ts: str) -> list[dict]:
    """The queued messages, in order, without taking them."""
    with _lock:
        return list(_queued.get((channel_id, thread_ts), []))


def take_steering_messages(channel_id: str, thread_ts: str) -> list[dict]:
    """Remove and return every queued message, in order, for the running turn to fold in."""
    with _lock:
        return _queued.pop((channel_id, thread_ts), [])


def take_next_sender_messages(channel_id: str, thread_ts: str) -> list[dict]:
    """Remove and return the first queued message and every message right after it
    from the same sender (one turn's worth); the rest stay queued, in order."""
    with _lock:
        queued = _queued.get((channel_id, thread_ts), [])
        if not queued:
            return []
        sender = queued[0]["user_id"]
        count = next((i for i, m in enumerate(queued) if m["user_id"] != sender), len(queued))
        taken, left = queued[:count], queued[count:]
        if left:
            _queued[(channel_id, thread_ts)] = left
        else:
            _queued.pop((channel_id, thread_ts), None)
        return taken


def clear_steering_messages(channel_id: str, thread_ts: str) -> None:
    with _lock:
        _queued.pop((channel_id, thread_ts), None)
