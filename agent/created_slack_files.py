"""Canvases and lists coolton created, and who for. A new one is shared nowhere, so the
usual file rule (agent.slack_access) would refuse to edit it even right after creating
it. Its creator's conversation and the person who asked for it can keep using it."""

from __future__ import annotations

import logging
import re
import threading
import time

from agent.json_file import load_json, save_json

logger = logging.getLogger(__name__)

CREATED_SLACK_FILES_FILE = "created_slack_files.json"

_lock = threading.Lock()
_ID_FIELD = re.compile(r"""(?:canvas_id|list_id|file_id)['"]?\s*[:=]\s*['"]?(F[A-Z0-9]{6,})""")
_ID_IN_URL = re.compile(r"/(?:docs|lists)\\?/T[A-Z0-9]+\\?/(F[A-Z0-9]{6,})")


def created_file_id(result) -> str | None:
    """The new file's id in a create tool's result, whatever shape that came back in."""
    text = str(result)
    match = _ID_FIELD.search(text) or _ID_IN_URL.search(text)
    return match.group(1) if match else None


def remember(file_id: str, channel_id: str, user_id: str) -> None:
    with _lock:
        files = load_json(CREATED_SLACK_FILES_FILE)
        files[file_id] = {"channel_id": channel_id, "user_id": user_id, "created_at": time.time()}
        save_json(CREATED_SLACK_FILES_FILE, files)


def created_for(file_id: str, channel_id: str, user_id: str) -> bool:
    """Whether coolton created this file in `channel_id` or for `user_id`."""
    entry = load_json(CREATED_SLACK_FILES_FILE).get(file_id)
    if not entry:
        return False
    return bool(channel_id and entry.get("channel_id") == channel_id) or bool(user_id and entry.get("user_id") == user_id)
