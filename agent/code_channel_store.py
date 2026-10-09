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
import re
import threading
import time

CODE_CHANNEL_STORE_FILE = "code_channels.json"
CODE_CHANNEL_THREAD_TS = ""

_lock = threading.Lock()

# Origins (channel, ts) of code channels coolton created herself. Slack opens such a
# channel with a "Context from #origin" message quoting the origin, posted as the
# origin's author; coolton's handoff turn already carries that context, so the quote
# isn't answered (a channel someone made from Slack's UI still starts from it).
_linked_origins: set[tuple[str, str]] = set()
_ORIGIN_CONTEXT_RE = re.compile(r"^<https?://[^|>]+/archives/([A-Z0-9]+)/p(\d+)(\d{6})[^|>]*\|Context> from <#")


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


def unregister_code_channel(channel_id: str) -> None:
    """Forget `channel_id` (it was archived); a mention there later registers it again."""
    with _lock:
        data = _load()
        if data.pop(channel_id, None) is not None:
            _save(data)


def remember_canvas_view(channel_id: str, view_key: str, canvas_id: str, view_id: str, name: str = "") -> None:
    """Record a canvas tab coolton added. Slack keeps no view_key for canvas tabs and
    leaves them out of agents.conversations.listViews, so this is how coolton finds one
    again to update or read it (agent.tools.code_channel_tools)."""
    with _lock:
        data = _load()
        entry = data.get(channel_id)
        if entry is None:
            return
        entry.setdefault("canvas_views", {})[view_key] = {"canvas_id": canvas_id, "view_id": view_id, "name": name}
        _save(data)


def canvas_views(channel_id: str) -> dict:
    """view_key -> {"canvas_id", "view_id", "name"} for the canvas tabs coolton added here."""
    with _lock:
        return dict((_load().get(channel_id) or {}).get("canvas_views") or {})


def expect_origin_context(channel_id: str, message_ts: str) -> None:
    """Note that coolton is creating a code channel linked to this message."""
    _linked_origins.add((channel_id, message_ts))


def is_expected_origin_context(text: str) -> bool:
    """Whether `text` is Slack's "Context from" message for a code channel coolton
    created herself (see _linked_origins)."""
    match = _ORIGIN_CONTEXT_RE.match(text or "")
    return bool(match) and (match[1], f"{match[2]}.{match[3]}") in _linked_origins
