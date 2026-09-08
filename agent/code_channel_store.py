"""Registry of Slack "code channels" — see agent.tools.code_channel.

A code channel is treated as ONE coolton conversation at the whole-channel
level: every message in it (outside a thread) is answered as if coolton were
mentioned, and replies go to the channel rather than a thread. That
conversation is keyed exactly the way every other conversation in this repo
is keyed — (channel_id, thread_ts) — using CODE_CHANNEL_THREAD_TS ("") as the
thread_ts. That empty string round-trips fine through thread_context.store's
"channel_id:thread_ts" string keys and is also falsy, which is what makes
posting-site code naturally treat it as "channel level, no thread" with a
plain `or None`.

Same shape as agent.leave_thread_store: a module-level lock, a tolerant
_load (missing file, corrupt JSON, or a JSON value that isn't even a dict all
degrade to "no code channels" instead of raising), and an atomic write.
"""

import json
import os
import threading
import time

CODE_CHANNEL_STORE_FILE = "code_channels.json"
CODE_CHANNEL_THREAD_TS = ""

_lock = threading.Lock()


def _load() -> dict:
    try:
        with open(CODE_CHANNEL_STORE_FILE, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    temp = f"{CODE_CHANNEL_STORE_FILE}.tmp"
    with open(temp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(temp, CODE_CHANNEL_STORE_FILE)


def register_code_channel(
    channel_id: str, name: str, owner_id: str,
    source_channel_id: str, source_thread_ts: str,
) -> None:
    """Record that `channel_id` is a code channel, spawned by `owner_id` out
    of the conversation at (source_channel_id, source_thread_ts)."""
    with _lock:
        data = _load()
        data[channel_id] = {
            "name": name,
            "owner_id": owner_id,
            "source_channel_id": source_channel_id,
            "source_thread_ts": source_thread_ts,
            "created_at": time.time(),
        }
        _save(data)


def is_code_channel(channel_id: str) -> bool:
    with _lock:
        return channel_id in _load()


def get_code_channel(channel_id: str) -> dict | None:
    with _lock:
        return _load().get(channel_id)
