# agent/sandbox_store.py
import threading

from agent.json_file import load_json, save_json

SANDBOX_STORE_FILE = "thread_sandboxes.json"
store_lock = threading.Lock()

def get_thread_sandbox_id(channel_id: str, thread_ts: str) -> str | None:
    """Retrieve the active E2B sandbox ID for a specific Slack thread."""
    with store_lock:
        return load_json(SANDBOX_STORE_FILE).get(f"{channel_id}:{thread_ts}")


def save_thread_sandbox_id(channel_id: str, thread_ts: str, sandbox_id: str):
    """Save the active E2B sandbox ID for a specific Slack thread."""
    with store_lock:
        data = load_json(SANDBOX_STORE_FILE)
        data[f"{channel_id}:{thread_ts}"] = sandbox_id
        save_json(SANDBOX_STORE_FILE, data)


def delete_thread_sandbox_id(channel_id: str, thread_ts: str):
    """Remove the stored E2B sandbox ID for a specific Slack thread, if present."""
    with store_lock:
        data = load_json(SANDBOX_STORE_FILE)
        data.pop(f"{channel_id}:{thread_ts}", None)
        save_json(SANDBOX_STORE_FILE, data)
