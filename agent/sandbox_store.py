# agent/sandbox_store.py
import os
import json
import threading

SANDBOX_STORE_FILE = "thread_sandboxes.json"
store_lock = threading.Lock()

def get_thread_sandbox_id(channel_id: str, thread_ts: str) -> str | None:
    """Retrieve the active E2B sandbox ID for a specific Slack thread."""
    if not os.path.exists(SANDBOX_STORE_FILE):
        return None
    with store_lock:
        try:
            with open(SANDBOX_STORE_FILE, "r") as f:
                data = json.load(f)
                return data.get(f"{channel_id}:{thread_ts}")
        except Exception:
            return None

def save_thread_sandbox_id(channel_id: str, thread_ts: str, sandbox_id: str):
    """Save the active E2B sandbox ID for a specific Slack thread."""
    data = {}
    with store_lock:
        if os.path.exists(SANDBOX_STORE_FILE):
            try:
                with open(SANDBOX_STORE_FILE, "r") as f:
                    data = json.load(f)
            except Exception:
                pass
        data[f"{channel_id}:{thread_ts}"] = sandbox_id
        with open(SANDBOX_STORE_FILE, "w") as f:
            json.dump(data, f, indent=2)
