import os
import json
import threading
import time

LEAVE_THREAD_STORE_FILE = "leave_thread_store.json"
leave_thread_lock = threading.Lock()


def _load_store() -> dict:
    if not os.path.exists(LEAVE_THREAD_STORE_FILE):
        return {}
    with open(LEAVE_THREAD_STORE_FILE, "r") as f:
        return json.load(f)


def _save_store(data: dict):
    temp = f"{LEAVE_THREAD_STORE_FILE}.tmp"
    with open(temp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(temp, LEAVE_THREAD_STORE_FILE)


def leave_thread(channel_id: str, thread_ts: str) -> str:
    """Mark a thread as left - bot will ignore future messages in this thread until mentioned."""
    key = f"{channel_id}:{thread_ts}"
    with leave_thread_lock:
        data = _load_store()
        data[key] = {"left_at": time.time()}
        _save_store(data)
    return f"Left thread {thread_ts} in channel {channel_id}. I'll ignore messages here until you @mention me again."


def is_thread_left(channel_id: str, thread_ts: str) -> bool:
    """Check if bot has left this thread."""
    key = f"{channel_id}:{thread_ts}"
    with leave_thread_lock:
        data = _load_store()
        return key in data


def rejoin_thread(channel_id: str, thread_ts: str) -> str:
    """Rejoin a previously left thread (called when bot is mentioned)."""
    key = f"{channel_id}:{thread_ts}"
    with leave_thread_lock:
        data = _load_store()
        if key in data:
            del data[key]
            _save_store(data)
            return f"Rejoined thread {thread_ts} in channel {channel_id}."
    return "Thread was not left."


def should_ignore_thread(channel_id: str, thread_ts: str, text: str) -> bool:
    """Check if bot should ignore this message (thread left AND no @mention)."""
    if not is_thread_left(channel_id, thread_ts):
        return False
    # Check if bot was mentioned
    return "<@" not in text