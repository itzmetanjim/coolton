"""Stored 👍/👎 feedback on coolton's replies.

Every rating is saved the moment the button is clicked (so it's kept even if
the comment box is skipped), then the optional comment is added to the same
record on submit. One record per (channel, message, user): clicking again or
changing your mind overwrites the rating. The admin DM in
listeners/views/feedback_views.py is unchanged; this is the durable copy.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time

FEEDBACK_FILE = "feedback.json"
_RESPONSE_CHARS = 2000
_lock = threading.Lock()


def _load() -> dict:
    try:
        with open(FEEDBACK_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    directory = os.path.dirname(os.path.abspath(FEEDBACK_FILE)) or "."
    fd, temp = tempfile.mkstemp(prefix=".feedback-", suffix=".tmp", dir=directory)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(temp, FEEDBACK_FILE)


def _key(channel_id: str, message_ts: str, user_id: str) -> str:
    return f"{channel_id}:{message_ts}:{user_id}"


def record_rating(
    channel_id: str, message_ts: str, user_id: str, rating: str,
    thread_ts: str = "", response_text: str = "",
) -> None:
    """Save (or overwrite) a user's rating ("good"/"bad") of one reply."""
    now = time.time()
    with _lock:
        data = _load()
        key = _key(channel_id, message_ts, user_id)
        record = data.get(key) or {"created_at": now, "comment": ""}
        record.update({
            "channel_id": channel_id, "thread_ts": thread_ts, "message_ts": message_ts,
            "user_id": user_id, "rating": rating,
            "response_text": (response_text or record.get("response_text", ""))[:_RESPONSE_CHARS],
            "updated_at": now,
        })
        data[key] = record
        _save(data)


def add_comment(channel_id: str, message_ts: str, user_id: str, rating: str, comment: str) -> None:
    """Attach the optional comment from the feedback modal to the rating record."""
    with _lock:
        data = _load()
        key = _key(channel_id, message_ts, user_id)
        record = data.get(key) or {"channel_id": channel_id, "message_ts": message_ts, "user_id": user_id, "created_at": time.time()}
        record.update({"rating": rating, "comment": comment, "updated_at": time.time()})
        data[key] = record
        _save(data)


def all_feedback() -> list[dict]:
    with _lock:
        return list(_load().values())
