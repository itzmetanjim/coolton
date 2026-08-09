import os
import json
import threading
import time

LEAVE_THREAD_STORE_FILE = "leave_thread_store.json"
leave_thread_lock = threading.Lock()


def _load_store() -> dict:
    try:
        with open(LEAVE_THREAD_STORE_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_store(data: dict):
    temp = f"{LEAVE_THREAD_STORE_FILE}.tmp"
    with open(temp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(temp, LEAVE_THREAD_STORE_FILE)


def leave_thread(channel_id: str, thread_ts: str) -> str:
    """Mark a thread as left - bot will ignore future messages here until mentioned again."""
    key = f"{channel_id}:{thread_ts}"
    with leave_thread_lock:
        data = _load_store()
        data[key] = {"engaged": False, "updated_at": time.time()}
        _save_store(data)
    return f"Left thread {thread_ts} in channel {channel_id}. I'll ignore messages here until you @mention me again."


def join_thread(channel_id: str, thread_ts: str) -> str:
    """Mark a thread as joined - bot will respond to every message here until told to leave."""
    key = f"{channel_id}:{thread_ts}"
    with leave_thread_lock:
        data = _load_store()
        data[key] = {"engaged": True, "updated_at": time.time()}
        _save_store(data)
    return f"Joined thread {thread_ts} in channel {channel_id}. I'll respond to every message here until you tell me to leave."


def is_thread_engaged(channel_id: str, thread_ts: str, is_dm: bool = False) -> bool:
    """Whether the bot responds to every (non-mentioned) message in this thread.

    DMs are engaged by default. Channel threads are only engaged once joined:
    auto-joined by a mention on the thread's starter message, or explicitly via
    the join_thread tool. A mid-thread mention answers once but does not join.
    """
    key = f"{channel_id}:{thread_ts}"
    with leave_thread_lock:
        data = _load_store()
        entry = data.get(key)
        if entry is None:
            return is_dm
        return bool(entry.get("engaged", is_dm))
